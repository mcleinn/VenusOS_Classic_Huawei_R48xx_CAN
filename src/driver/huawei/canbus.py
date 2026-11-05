"""CAN transport for the Huawei driver on Venus OS v2.92 (Python 3.8, no python-can).

Three things live here:

1. `CanFrame`, the one message type used everywhere (timestamp, 29-bit id, data, flags).
2. Links: `SocketCanLink` (raw AF_CAN socket on the Cerbo, error frames enabled) and
   `VirtualBus` / `VirtualLink` (in-process bus for tests and the simulator). Both offer
   `send(frame)`, `recv(timeout)` and `fileno()` where applicable.
3. `LinkManager`: brings `can0` up at 125 kbit/s and refuses to do so unless the Venus
   VE.Can profile for the interface is Disabled (0); reads the kernel CAN state
   (ERROR-ACTIVE / ERROR-PASSIVE / BUS-OFF) from `ip -details link show`.

Capture files: `CandumpWriter` writes the standard candump log format
(`(ts) can0 1081407F#0170000000000000`) so captures can be read by can-utils and by
`read_candump()`; `read_jsonl()` reads the rsh harness's jsonl records tolerantly.
Every shell command is run through an injectable runner so the logic is testable on Windows.

Interface identity (2026-11-03): the kernel names CAN interfaces by probe order, so a name
says nothing about the hardware behind it (a USB adapter present at boot took can0/can1 from
the Cerbo's built-in ports once). `iface_device()` and `iface_driver()` read the sysfs links
behind an interface name so the driver can refuse an interface that is not the hardware it
expects: the VE.Can port is the SoC controller `1c2bc00.can` (driver sun4i_can), the display
boards' USB adapter channels are driver `gs_usb`, the BMS-Can port (slcan over ttyS3) has no
device link at all.
"""
import json
import os
import re
import select
import socket
import struct
import subprocess
import threading
import time
import queue

SYSFS_NET = '/sys/class/net'
VECAN_DEVICE = '1c2bc00.can'      # the Cerbo GX built-in VE.Can controller
USB_ADAPTER_DRIVER = 'gs_usb'     # the display boards' USB CAN adapter


def iface_device(ifname, readlink=os.readlink, root=SYSFS_NET):
    """Basename of the device behind a network interface ('1c2bc00.can', '1-1.1:1.0'),
    None when the interface does not exist or has no device (slcan)."""
    try:
        return os.path.basename(readlink(os.path.join(root, ifname, 'device')).replace('\\', '/'))
    except OSError:
        return None


def iface_driver(ifname, readlink=os.readlink, root=SYSFS_NET):
    """Basename of the kernel driver bound to the interface's device ('sun4i_can', 'gs_usb'),
    None when unknown."""
    try:
        return os.path.basename(readlink(os.path.join(root, ifname, 'device', 'driver')).replace('\\', '/'))
    except OSError:
        return None


CAN_EFF_FLAG = 0x80000000
CAN_RTR_FLAG = 0x40000000
CAN_ERR_FLAG = 0x20000000
CAN_EFF_MASK = 0x1FFFFFFF
CAN_SFF_MASK = 0x7FF
_FRAME_FMT = '=IB3x8s'          # struct can_frame: id, dlc, pad, data[8]
_FRAME_SIZE = struct.calcsize(_FRAME_FMT)
SOL_CAN_RAW = 101
CAN_RAW_ERR_FILTER = 2
CAN_ERR_MASK = 0x1FFFFFFF


class CanFrame(object):
    __slots__ = ('ts', 'can_id', 'data', 'ext', 'err', 'echo')

    def __init__(self, can_id, data=b'', ext=True, ts=None, err=False, echo=False):
        self.ts = time.time() if ts is None else ts
        self.can_id = can_id
        self.data = bytes(data)
        self.ext = ext
        self.err = err
        self.echo = echo

    @property
    def id_str(self):
        if self.err:
            return 'ERR'
        return '%08X' % self.can_id if self.ext else '%03X' % self.can_id

    def raw_id(self):
        if self.err:
            return (self.can_id & CAN_ERR_MASK) | CAN_ERR_FLAG
        v = self.can_id & (CAN_EFF_MASK if self.ext else CAN_SFF_MASK)
        return v | CAN_EFF_FLAG if self.ext else v

    def pack(self):
        return struct.pack(_FRAME_FMT, self.raw_id(), len(self.data), self.data.ljust(8, b'\0'))

    @classmethod
    def unpack(cls, buf, ts=None):
        raw, dlc, data = struct.unpack(_FRAME_FMT, buf[:_FRAME_SIZE])
        if raw & CAN_ERR_FLAG:
            return cls(raw & CAN_ERR_MASK, data[:dlc], ext=True, ts=ts, err=True)
        ext = bool(raw & CAN_EFF_FLAG)
        return cls(raw & (CAN_EFF_MASK if ext else CAN_SFF_MASK), data[:dlc], ext=ext, ts=ts)

    def __repr__(self):
        return 'CanFrame(%s#%s%s)' % (self.id_str, self.data.hex().upper(), ' echo' if self.echo else '')

    def __eq__(self, other):
        return (isinstance(other, CanFrame) and self.can_id == other.can_id and
                self.data == other.data and self.ext == other.ext and self.err == other.err)


class CanError(Exception):
    pass


# ---------------------------------------------------------------- SocketCAN
class SocketCanLink(object):
    """Raw CAN socket on one interface. Error frames are delivered as CanFrame(err=True)
    so the engine can count them and detect bus-off without polling `ip`."""

    def __init__(self, ifname='can0'):
        if not hasattr(socket, 'AF_CAN'):
            raise CanError('this Python has no AF_CAN support (not Linux?)')
        self.ifname = ifname
        self.sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        self.sock.setsockopt(SOL_CAN_RAW, CAN_RAW_ERR_FILTER, struct.pack('=I', CAN_ERR_MASK))
        self.sock.setblocking(False)
        self.sock.bind((ifname,))
        self.stats = {'tx': 0, 'rx': 0, 'err': 0, 'tx_fail': 0}

    def fileno(self):
        return self.sock.fileno()

    def send(self, frame):
        try:
            self.sock.send(frame.pack())
            self.stats['tx'] += 1
        except OSError as e:
            self.stats['tx_fail'] += 1
            raise CanError('send failed on %s: %s' % (self.ifname, e))

    def recv(self, timeout=0.0):
        """One frame or None. timeout 0 = non-blocking."""
        if timeout:
            r, _, _ = select.select([self.sock], [], [], timeout)
            if not r:
                return None
        try:
            buf = self.sock.recv(_FRAME_SIZE)
        except BlockingIOError:
            return None
        except OSError as e:
            raise CanError('recv failed on %s: %s' % (self.ifname, e))
        f = CanFrame.unpack(buf, ts=time.time())
        self.stats['err' if f.err else 'rx'] += 1
        return f

    def close(self):
        self.sock.close()


# -------------------------------------------------------------- virtual bus
class VirtualBus(object):
    """In-process bus: a frame sent on one link is queued on every other link
    (and echoed on the sender with echo=True when requested). Thread-safe."""

    def __init__(self, name='virtual'):
        self.name = name
        self.links = []
        self.lock = threading.Lock()
        self.frames = []            # everything that crossed the bus, for assertions

    def attach(self, name='link', echo=False):
        link = VirtualLink(self, name, echo)
        with self.lock:
            self.links.append(link)
        return link

    def _broadcast(self, sender, frame):
        with self.lock:
            self.frames.append((sender.name, frame))
            for link in self.links:
                if link is sender:
                    if link.echo:
                        link.queue.put(CanFrame(frame.can_id, frame.data, frame.ext, frame.ts, echo=True))
                else:
                    link.queue.put(CanFrame(frame.can_id, frame.data, frame.ext, frame.ts))


class VirtualLink(object):
    def __init__(self, bus, name, echo):
        self.bus = bus
        self.name = name
        self.echo = echo
        self.queue = queue.Queue()
        self.stats = {'tx': 0, 'rx': 0, 'err': 0, 'tx_fail': 0}
        self.fail_tx = False

    def send(self, frame):
        if self.fail_tx:
            self.stats['tx_fail'] += 1
            raise CanError('virtual link %s: tx disabled' % self.name)
        self.stats['tx'] += 1
        self.bus._broadcast(self, frame)

    def recv(self, timeout=0.0):
        try:
            f = self.queue.get(timeout=timeout) if timeout else self.queue.get_nowait()
        except queue.Empty:
            return None
        self.stats['err' if f.err else 'rx'] += 1
        return f

    def inject_error(self, err_class=0x40):
        """Deliver an error frame to this link only (bus-off = 0x40)."""
        self.queue.put(CanFrame(err_class, b'\0' * 8, ext=True, err=True))

    def close(self):
        pass


# ------------------------------------------------------------- link manager
class LinkManager(object):
    """Owns the interface configuration. `profile_getter()` must return the Venus
    setting /Settings/Canbus/<if>/Profile (0 = Disabled); the manager refuses to touch
    an interface another Venus service owns. `runner(argv)` runs a command and returns
    (returncode, stdout) so tests can fake `ip`."""

    BUS_STATES = ('ERROR-ACTIVE', 'ERROR-WARNING', 'ERROR-PASSIVE', 'BUS-OFF', 'STOPPED', 'SLEEPING')

    def __init__(self, ifname='can0', bitrate=125000, restart_ms=100, profile_getter=None, runner=None):
        self.ifname = ifname
        self.bitrate = bitrate
        self.restart_ms = restart_ms
        self.profile_getter = profile_getter or (lambda: None)
        self.runner = runner or self._run

    @staticmethod
    def _run(argv):
        p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return p.returncode, p.stdout.decode('utf-8', 'replace')

    def profile_ok(self):
        """True only when the Venus profile for this interface is Disabled (0)."""
        try:
            return int(self.profile_getter()) == 0
        except (TypeError, ValueError):
            return False

    def status(self):
        """dict(up, state, bitrate) parsed from `ip -details link show`."""
        rc, out = self.runner(['ip', '-details', 'link', 'show', self.ifname])
        if rc != 0:
            return {'up': False, 'state': 'MISSING', 'bitrate': None, 'raw': out}
        state = None
        for s in self.BUS_STATES:
            if re.search(r'\bstate %s\b' % s, out):
                state = s
                break
        m = re.search(r'bitrate (\d+)', out)
        up = bool(re.search(r'<[^>]*\bUP\b', out))
        return {'up': up, 'state': state, 'bitrate': int(m.group(1)) if m else None, 'raw': out}

    def ensure_up(self):
        """Configure and raise the link. Raises CanError when the profile is not
        Disabled or any `ip` step fails. Idempotent."""
        if not self.profile_ok():
            raise CanError('%s is owned by Venus (VE.Can profile is not Disabled); refusing' % self.ifname)
        st = self.status()
        if st['state'] == 'MISSING':
            raise CanError('interface %s not found' % self.ifname)
        if st['up'] and st['bitrate'] == self.bitrate and st['state'] not in ('BUS-OFF', 'STOPPED'):
            return st
        steps = [
            ['ip', 'link', 'set', self.ifname, 'down'],
            ['ip', 'link', 'set', self.ifname, 'type', 'can', 'bitrate', str(self.bitrate),
             'restart-ms', str(self.restart_ms)],
            ['ip', 'link', 'set', self.ifname, 'up'],
        ]
        for argv in steps:
            rc, out = self.runner(argv)
            if rc != 0:
                raise CanError('%s failed: %s' % (' '.join(argv), out.strip()))
        st = self.status()
        if not st['up']:
            raise CanError('%s did not come up: %s' % (self.ifname, st['raw'].strip()))
        return st

    def bounce(self):
        """Take the link down and raise it again. 2026-11-03: when the display board on a gs_usb
        channel reboots, the channel goes error-passive with its TX queue jammed and every later
        send fails with ENOBUFS even after the board is back; the adapter does not recover by
        itself (only bus-off triggers restart-ms) and re-opening the socket does nothing. Down/up
        clears it (both boards fed again within seconds, fans back to normal)."""
        self.runner(['ip', 'link', 'set', self.ifname, 'down'])
        return self.ensure_up()

    def down(self):
        self.runner(['ip', 'link', 'set', self.ifname, 'down'])


# ----------------------------------------------------------------- captures
class CandumpWriter(object):
    """candump -L compatible log: `(1697000000.123456) can0 1081407F#01700000...`
    Error frames are written with the ERR flag id like can-utils does."""

    def __init__(self, path, ifname='can0', clock=None):
        """`clock` (e.g. time.time) stamps every line at write time; None uses frame.ts."""
        self.path = path
        self.ifname = ifname
        self.clock = clock
        self.fh = open(path, 'a')
        self.count = 0

    def write(self, frame, direction=None, ifname=None):
        if frame.err:
            ident = '%08X' % frame.raw_id()
        else:
            ident = frame.id_str
        ts = self.clock() if self.clock is not None else frame.ts
        line = '(%.6f) %s %s#%s' % (ts, ifname or self.ifname, ident, frame.data.hex().upper())
        if direction:
            line += '  ; ' + direction
        self.fh.write(line + '\n')
        self.count += 1

    def flush(self):
        self.fh.flush()

    def close(self):
        self.fh.close()


_CANDUMP_RE = re.compile(r'^\((?P<ts>[\d.]+)\)\s+(?P<if>\S+)\s+(?P<id>[0-9A-Fa-f]+)#(?P<data>[0-9A-Fa-f]*)')


def read_candump(path):
    """Yield CanFrame from a candump -L log (ids with 8 hex digits are extended)."""
    with open(path) as fh:
        for line in fh:
            m = _CANDUMP_RE.match(line.strip())
            if not m:
                continue
            raw = int(m.group('id'), 16)
            data = bytes.fromhex(m.group('data')) if m.group('data') else b''
            if raw & CAN_ERR_FLAG and len(m.group('id')) == 8:
                yield CanFrame(raw & CAN_ERR_MASK, data, ext=True, ts=float(m.group('ts')), err=True)
            else:
                ext = len(m.group('id')) == 8
                yield CanFrame(raw & (CAN_EFF_MASK if ext else CAN_SFF_MASK), data, ext=ext,
                               ts=float(m.group('ts')))


def read_jsonl(path):
    """Yield CanFrame from jsonl records as the rsh harness writes them. Tolerant to key
    names: id in 'can_id'/'id' (int or hex string), data in 'data'/'data_hex' (hex string
    or list of ints), time in 'ts'/'t'/'time', extended flag in 'ext' (default True).
    Records without an id (notes, audit lines) are skipped."""
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            cid = rec.get('can_id', rec.get('id'))
            if cid is None:
                continue
            if isinstance(cid, str):
                cid = int(cid, 16)
            data = rec.get('data', rec.get('data_hex', ''))
            if isinstance(data, list):
                data = bytes(data)
            elif isinstance(data, str):
                data = bytes.fromhex(data.replace(' ', ''))
            ts = rec.get('ts', rec.get('t', rec.get('time', 0.0)))
            yield CanFrame(int(cid) & CAN_EFF_MASK, data, ext=bool(rec.get('ext', True)),
                           ts=float(ts), err=bool(rec.get('err', False)))
