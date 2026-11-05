"""Display repeater: keeps a charger's add-on control board alive on its own CAN interface
with the real module's data, while this driver stays the sole master of the module bus.

Specification: Display_Repeater_Task.md (sections 2 and 3, plus the 2026-11-01 findings in
section 0a). Measured basis: harness docs/CHARGER-HUAWEI-UNITS.md 3.3 (the board's cycle),
docs/board1-cycle-can3-2026-11-01.candump (the board alone on the Cerbo's adapter).

Rules enforced here:
- A DisplayPort never holds the module link: construction refuses the module link object
  and the module interface name. Nothing in this module has a path to the module bus; it
  receives decoded frames from the engine and writes only to its board link.
- Forwarded to a board: PSU->CTL frames of ITS unit (matched by serial, never by address),
  with the address field rewritten from the module's current address to the address the
  board expects; address-0 frames the modules originate (0x100011FE heartbeat, the
  0x108081FE 01 13 announce) unchanged, to every port. Never: acknowledgements (they
  answer this driver's writes), this driver's own requests and writes, GET 0x85 replies
  (the driver asked for them, not the board; their value is used for the substitution).
- 0x75 substitution: the R4875G5 reports 0 in the burst's 0x75 and the terminal voltage in
  0x85. The board renders 0x75, so the relayed 0x75 frame carries the latest 0x85 reading
  if it is younger than V85_MAX_AGE_S; otherwise the original 0 goes through.
- Everything the board sends is absorbed. Its 0x80 writes are answered with the protocol
  acknowledgement (the write's data with byte 0 = 0x01) so its firmware stays content; what
  it wanted is recorded in `wanted`. Requests, the 0x81 config broadcast, commands hidden
  under a request id, anything else: absorbed and counted, no ack. Nothing synthesized is
  ever based on stale module data: only acks, only in direct reply.
- A failed send to a board is counted and logged at most once a minute per port. An error
  frame on a board interface is counted; a bus-off asks that port's own LinkManager to
  restart the interface. None of it reaches the engine.
"""
import logging

from . import protocol as P
from .canbus import CanFrame, CanError

log = logging.getLogger('huawei.display')

BOARD_STALE_S = 3.0          # the board sends every 0.62 s; silent for 3 s = not alive
V85_MAX_AGE_S = 2.0          # the engine GETs 0x85 after every 1 Hz burst
TX_FAIL_LOG_S = 60.0
BOUNCE_AFTER_S = 10.0        # sends failing continuously this long: bounce the interface (down/up)
BOUNCE_COOLDOWN_S = 30.0     # ... at most this often
MUTE = -1                    # overrides key: forward nothing to the board
RECOVER_AFTER_S = 5.0        # board silent this long: check the interface ...
RECOVER_INTERVAL_S = 5.0     # ... and keep checking at this interval (one `ip` call each)
BROKEN_RELINK_S = 60.0       # a socket marked broken by send failures on an unchanged interface is re-opened at most this often
BUS_OFF_CLASS = 0x40
FORWARD_KINDS = ('status', 'info', 'elabel', 'unsolicited', 'addr_negotiation', 'get_reply')
REQUEST_KINDS = ('status_request', 'info_request', 'elabel_request', 'get')


class DisplayPort(object):
    """One board on one SocketCAN interface."""

    def __init__(self, link, ifname, serial, board_addr=1, clock=None, capture=None,
                 link_manager=None, module_link=None, module_ifname='can0',
                 link_factory=None, ifindex_getter=None, on_relink=None):
        """`link` may be None when the interface is not there yet. With `link_factory`
        (callable -> new link) and `ifindex_getter` (ifname -> int, OSError when missing)
        the port recovers by itself: while the board is silent it checks every
        RECOVER_INTERVAL_S whether the interface exists, raises it through `link_manager`
        and re-opens its socket when the interface was re-created (USB re-enumeration:
        2026-11-02, both boards lost after the adapter re-plugged) or missing at start.
        `on_relink(port)` lets the owner re-arm its read watch on the new fileno."""
        if module_link is not None and link is not None and link is module_link:
            raise ValueError('display port %s: refusing the module link' % ifname)
        if ifname == module_ifname or getattr(link, 'ifname', None) == module_ifname:
            raise ValueError('display port %s: refusing the module interface %s' % (ifname, module_ifname))
        self.link = link
        self.link_factory = link_factory
        self.ifindex_getter = ifindex_getter
        self.on_relink = on_relink
        self._ifindex = None
        self._last_relink_t = None  # clock time of the last re-open on an unchanged interface
        self.overrides = {}         # status reg -> raw presented to the board instead of the module's value
        self._link_broken = False
        self._tx_fail_since = None  # clock time of the first of the current run of send failures
        self._last_bounce_t = None
        self._last_recover_t = None
        self._recover_logged = -1e9
        if link is not None and ifindex_getter is not None:
            try:
                self._ifindex = ifindex_getter(ifname)
            except OSError:
                pass
        self.ifname = ifname
        self.serial = serial.upper()
        self.board_addr = int(board_addr)
        self.clock = clock or (lambda: 0.0)
        self.capture = capture
        self.link_manager = link_manager
        self.stats = {'forwarded': 0, 'rewritten': 0, 'voltage_substituted': 0, 'acks_dropped': 0,
                      'get85_consumed': 0, 'absorbed_requests': 0, 'absorbed_writes': 0,
                      'absorbed_cfg': 0, 'absorbed_other': 0, 'acks_synthesized': 0, 'bounces': 0, 'overridden': 0, 'muted': 0,
                      'tx_fail': 0, 'err': 0, 'bus_off': 0, 'board_frames': 0, 'relinks': 0,
                      'no_link': 0}
        self.wanted = {}                 # register name -> {'raw', 'value_v'?, 't'}
        self.last_board_frame_t = None
        self.v85_raw = None
        self.v85_t = None
        self._tx_fail_logged = -1e9
        self._alive = None

    # ---------------------------------------------------------------- module side
    def note_get85(self, dec):
        """Called for every GET reply the engine attributes to this unit."""
        if dec.get('reg') == P.OUT_VOLTAGE_ALT_REG:
            self.v85_raw = dec.get('raw')
            self.v85_t = self.clock()
            self.stats['get85_consumed'] += 1
            return True
        return False

    def forward(self, frame, dec, current_addr):
        """Relay one PSU->CTL frame of this port's unit (or an address-0 module frame)."""
        kind = dec.get('kind')
        if kind == 'ack':
            self.stats['acks_dropped'] += 1
            return False
        if kind == 'get_reply' and self.note_get85(dec):
            return False
        h = P.unpack_id(frame.can_id)
        module_originated_cfg = (h['cmd'] == P.CMD_CFG and h['addr'] == 0)
        if h['to_psu'] and not module_originated_cfg and h['cmd'] not in (P.CMD_UNSOL, P.CMD_ADDR):
            return False                                 # this driver's own traffic
        if kind not in FORWARD_KINDS and not module_originated_cfg:
            return False
        data = bytes(frame.data)
        if (kind == 'status' and dec.get('reg') == 0x75 and dec.get('raw') == 0
                and self.v85_raw and self.v85_t is not None
                and self.clock() - self.v85_t <= V85_MAX_AGE_S):
            data = data[:4] + int(self.v85_raw).to_bytes(4, 'big')
            self.stats['voltage_substituted'] += 1
        if MUTE in self.overrides:
            self.stats['muted'] += 1
            return False                                 # the board gets nothing (fan experiments)
        if kind == 'status' and dec.get('reg') in self.overrides:
            data = data[:4] + int(self.overrides[dec['reg']]).to_bytes(4, 'big')
            self.stats['overridden'] += 1
        can_id = frame.can_id
        if h['addr'] != 0 and h['addr'] == current_addr and h['addr'] != self.board_addr:
            can_id = P.pack_id(h['proto'], self.board_addr, h['cmd'], h['to_psu'], h['cont'],
                               h['group'], h['sw_addr'])
            self.stats['rewritten'] += 1
        return self._send(CanFrame(can_id, data, ext=True, ts=self.clock()), 'fwd')

    # ----------------------------------------------------------------- board side
    def on_board_frame(self, frame):
        now = self.clock()
        if self.capture:
            self.capture.write(frame, 'rx-' + self.ifname, ifname=self.ifname)
        if frame.err:
            self.stats['err'] += 1
            if frame.can_id & BUS_OFF_CLASS:
                self.stats['bus_off'] += 1
                log.error('%s: bus-off on the board interface', self.ifname)
                if self.link_manager is not None:
                    try:
                        self.link_manager.ensure_up()
                    except CanError as e:
                        log.error('%s: restart failed: %s', self.ifname, e)
            return
        if frame.echo:
            return
        self.stats['board_frames'] += 1
        self.last_board_frame_t = now
        dec = P.decode(frame)
        if dec is None:
            self.stats['absorbed_other'] += 1
            return
        h = P.unpack_id(frame.can_id)
        kind = dec.get('kind')
        if h['cmd'] == P.CMD_SET and h['to_psu']:
            self.stats['absorbed_writes'] += 1
            d = bytes(frame.data).ljust(8, b'\0')[:8]
            name = dec.get('name', 'unknown')
            entry = {'raw': dec.get('raw'), 't': round(now, 3)}
            if P.SET_REGS.get(name, (None, None, ''))[2] == 'V':
                entry['value_v'] = round(dec['raw'] / float(P.SCALE_1024), 3)
            self.wanted[name] = entry
            ack = bytes([0x01]) + d[1:]
            if self._send(CanFrame(P.reply_id(P.CMD_SET, h['addr'], False), ack, ext=True, ts=now), 'ack'):
                self.stats['acks_synthesized'] += 1
            return
        if h['cmd'] == P.CMD_CFG:
            self.stats['absorbed_cfg'] += 1
            return
        if kind in REQUEST_KINDS:
            self.stats['absorbed_requests'] += 1
            if bytes(frame.data).rstrip(b'\0') and kind != 'get':
                log.warning('%s: non-zero payload under a request id (%s): absorbed', self.ifname, frame.data.hex())
            return
        self.stats['absorbed_other'] += 1

    def alive(self, now=None):
        now = self.clock() if now is None else now
        return self.last_board_frame_t is not None and now - self.last_board_frame_t <= BOARD_STALE_S

    def tick(self, now):
        alive = self.alive(now)
        if alive != self._alive:
            log.info('%s: board %s', self.ifname, 'alive' if alive else 'silent')
            self._alive = alive
        if alive:
            self._last_recover_t = None
            return
        silent_for = now - self.last_board_frame_t if self.last_board_frame_t is not None else None
        if silent_for is not None and silent_for < RECOVER_AFTER_S:
            return
        if self._last_recover_t is not None and now - self._last_recover_t < RECOVER_INTERVAL_S:
            return
        self._last_recover_t = now
        self.recover(now)

    def _log_recover(self, now, text):
        if now - self._recover_logged >= TX_FAIL_LOG_S:
            self._recover_logged = now
            log.warning('%s: %s', self.ifname, text)

    def recover(self, now=None):
        """Check the interface, raise it, re-open the socket if it was re-created. Returns
        True when a new link was opened."""
        now = self.clock() if now is None else now
        if self.ifindex_getter is None or self.link_factory is None:
            return False
        try:
            idx = self.ifindex_getter(self.ifname)
        except OSError:
            self._log_recover(now, 'interface missing (adapter unplugged?); will keep checking')
            return False
        if self.link_manager is not None:
            try:
                self.link_manager.ensure_up()
            except CanError as e:
                self._log_recover(now, 'cannot raise the interface: %s' % e)
                return False
        if self.link is not None and not self._link_broken and idx == self._ifindex:
            return False                              # same interface, socket still valid
        if self.link is not None and idx == self._ifindex and self._link_broken:
            # same interface, socket only 'broken' by send failures (no node acknowledges, 2026-11-03
            # board 1): re-opening every 5 s changes nothing and floods the log; retry rarely
            if self._last_relink_t is not None and now - self._last_relink_t < BROKEN_RELINK_S:
                return False
        old = self.link
        try:
            self.link = self.link_factory()
        except (CanError, OSError) as e:
            self.link = old
            self._log_recover(now, 'cannot open the socket: %s' % e)
            return False
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        same = (idx == self._ifindex)
        self._ifindex = idx
        self._link_broken = False
        self._last_relink_t = now
        self.stats['relinks'] += 1
        if same:
            self._log_recover(now, 'socket re-opened after send failures (ifindex %s unchanged): nothing acknowledges on this channel?' % idx)
        else:
            log.info('%s: interface %s, socket re-opened (ifindex %s)', self.ifname,
                     'back' if old is None else 're-created', idx)
        if self.on_relink is not None:
            self.on_relink(self)
        return True

    def _send(self, frame, what):
        if self.link is None:
            self.stats['no_link'] += 1
            return False
        try:
            self.link.send(frame)
        except CanError as e:
            self._link_broken = True
            self.stats['tx_fail'] += 1
            now = self.clock()
            if now - self._tx_fail_logged >= TX_FAIL_LOG_S:
                self._tx_fail_logged = now
                log.warning('%s: send failed (%s): %s', self.ifname, what, e)
            if self._tx_fail_since is None:
                self._tx_fail_since = now
            elif (now - self._tx_fail_since >= BOUNCE_AFTER_S and self.link_manager is not None
                  and hasattr(self.link_manager, 'bounce')
                  and (self._last_bounce_t is None or now - self._last_bounce_t >= BOUNCE_COOLDOWN_S)):
                self._last_bounce_t = now
                failing_for = now - self._tx_fail_since
                self._tx_fail_since = now
                try:
                    self.link_manager.bounce()
                    self.stats['bounces'] += 1
                    log.warning('%s: sends failing for %.0f s (%s): interface bounced (down/up)', self.ifname, failing_for, e)
                except CanError as e2:
                    log.error('%s: bounce failed: %s', self.ifname, e2)
            return False
        self._tx_fail_since = None
        if what == 'fwd':
            self.stats['forwarded'] += 1
        if self.capture:
            self.capture.write(frame, 'tx-' + self.ifname, ifname=self.ifname)
        return True

    def summary(self):
        return {'interface': self.ifname, 'serial': self.serial, 'board_addr': self.board_addr,
                'alive': self.alive(), 'link': self.link is not None and not self._link_broken,
                'stats': dict(self.stats), 'wanted': dict(self.wanted),
                'v85': None if self.v85_raw is None else round(self.v85_raw / float(P.SCALE_1024), 3)}


class DisplayRepeater(object):
    """All ports. Fed by the engine; never touches the module link."""

    def __init__(self, ports, clock=None):
        self.ports = dict(ports)             # ifname -> DisplayPort
        self.clock = clock or (lambda: 0.0)
        self.by_serial = {}
        for p in self.ports.values():
            self.by_serial.setdefault(p.serial, []).append(p)

    def port_for(self, serial):
        ports = self.by_serial.get(serial.upper())
        return ports[0] if ports else None

    def on_module_frame(self, frame, dec, module):
        """From Engine._on_frame after registry.feed(). `module` is None for frames the
        registry could not attribute (address 0)."""
        if dec is None:
            return
        if module is None:
            if dec.get('addr') == 0 and (dec.get('flow') == 'PSU->CTL' or dec.get('cmd') == P.CMD_CFG):
                for p in self.ports.values():
                    p.forward(frame, dec, 0)
            return
        if not module.identified():
            return
        for p in self.by_serial.get(module.serial, ()):
            p.forward(frame, dec, module.addr)

    def on_board_readable(self, ifname, limit=64):
        p = self.ports.get(ifname)
        if p is None:
            return 0
        n = 0
        if p.link is None:
            return 0
        for _ in range(limit):
            try:
                f = p.link.recv(0)
            except CanError as e:
                p._link_broken = True
                log.error('%s: recv: %s', ifname, e)
                return n
            if f is None:
                break
            p.on_board_frame(f)
            n += 1
        return n

    def tick(self, now):
        for p in self.ports.values():
            p.tick(now)

    def summary(self):
        return dict((ifname, p.summary()) for ifname, p in self.ports.items())
