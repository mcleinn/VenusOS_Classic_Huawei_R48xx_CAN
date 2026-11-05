#!/usr/bin/env python3
"""Huawei R4850G2 charger driver, process entry for Venus OS v2.92 (Cerbo GX).

    python3 -m huawei.main [--interface can0] [--expected 2] [--dcsource]
                           [--capture /data/huawei/log/can.log] [--config /data/huawei/driver.json]
                           [--control] [--no-link-setup] [--quiet-start 75]
                           [--display can3=<serial>[:<board_addr>] ...]

Start sequence:
  1. read the config (per-serial current scale policy and custom names; nothing else is
     configurable from a file: caps live in D-Bus settings, the output gate in code),
  2. refuse to run unless /Settings/Canbus/<if>/Profile is 0 (Disabled),
  3. bring the link up at 125 kbit/s (restart-ms 100),
  4. open the raw socket, start the engine under GLib: socket readable -> on_frame,
     200 ms -> tick,
  5. watch /Control/ChargerChargeCurrent on com.victronenergy.system for the watchdog,
  6. on SIGTERM/SIGINT: commander shutdown (zero current if a CanCommander ever exists),
     services marked offline, exit 0.

--display can3=708A85B00972 attaches the display repeater (display.py) for that unit's
control board on that interface; repeatable; the JSON config's "display" section is the
same thing. A display interface that is owned by Venus is logged and skipped; one that is
missing or down is retried every few seconds for as long as the driver runs, and a port
whose interface is re-created (USB adapter re-plugged) re-opens its socket by itself: the
chargers are controlled with or without their screens.
--control asks for the CanCommander; it is granted only if protocol.VERIFIED is True (True
since 2026-11-04; the service run script passes --control when /data/huawei/control-enabled
exists). "excluded" in the config lists serials that are polled and published but never
written to (unit 1 while it caps itself at ~1.65 A). --quiet-start is the
number of seconds after a unit's first frame before it is addressed at all (engine.py:
polling keeps a unit on-line, so a previous master's on-line limit must expire first).
"""
import argparse
import json
import socket
import logging
import os
import signal
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(1, '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python')

from huawei import VERSION                                  # noqa: E402
from huawei import protocol as P                            # noqa: E402
from huawei.canbus import SocketCanLink, LinkManager, CandumpWriter, CanError   # noqa: E402
from huawei.canbus import iface_device, iface_driver, USB_ADAPTER_DRIVER     # noqa: E402
from huawei.modules import Registry                         # noqa: E402
from huawei.control import DvccWatchdog, make_commander     # noqa: E402
from huawei.engine import Engine, EngineFailure, QUIET_START_S as Engine_QUIET, BENCH_MARKER   # noqa: E402

log = logging.getLogger('huawei.main')
DEFAULT_CONFIG = '/data/huawei/driver.json'
SYSTEM_SERVICE_WAIT_S = 180.0     # boot: how long to wait for dbus-systemcalc
FANKICK_FILE = '/data/huawei/fankick'   # experiment knob: lines 'ifname REGHEX RAW' -> board sees RAW for that status reg


def marker_age(path):
    """Seconds since the file was last touched, None if it does not exist."""
    try:
        return time.time() - os.stat(path).st_mtime
    except OSError:
        return None


def parse_display_spec(spec):
    """'can3=708A85B00972[:1]' -> ('can3', {'serial': '708A85B00972', 'board_addr': 1})"""
    if '=' not in spec:
        raise SystemExit('--display: expected <interface>=<serial>[:<board_addr>], got %r' % spec)
    ifname, rest = spec.split('=', 1)
    serial, _, addr = rest.partition(':')
    serial = serial.strip().upper()
    if len(serial) != 12 or any(ch not in '0123456789ABCDEF' for ch in serial):
        raise SystemExit('--display %s: serial must be 12 hex digits, got %r' % (ifname, serial))
    try:
        board_addr = int(addr) if addr else 1
    except ValueError:
        raise SystemExit('--display %s: board address must be an integer, got %r' % (ifname, addr))
    if not 1 <= board_addr <= 0x7F:
        raise SystemExit('--display %s: board address out of range: %d' % (ifname, board_addr))
    ifname = ifname.strip()
    if not ifname or ifname == 'can0':
        raise SystemExit('--display: %r is not a board interface' % ifname)
    return ifname, {'serial': serial, 'board_addr': board_addr}


def load_config(path, display_specs=()):
    """{"scales": {"<serial_hex>": "ratio1024"}, "names": {"<serial_hex>": "Huawei links"},
       "display": {"can3": {"serial": "<serial_hex>", "board_addr": 1}}}
    --display arguments override the file's "display" section per interface."""
    cfg = {'scales': {}, 'names': {}, 'display': {}, 'output_enable': 'none', 'excluded': []}
    if path and os.path.exists(path):
        with open(path) as fh:
            data = json.load(fh)
        cfg['output_enable'] = str(data.get('output_enable', 'none'))
        from huawei.control import OUTPUT_ENABLE_POLICIES
        if cfg['output_enable'] not in OUTPUT_ENABLE_POLICIES:
            raise SystemExit('config: unknown output_enable policy %r (one of %s)'
                             % (cfg['output_enable'], ', '.join(sorted(OUTPUT_ENABLE_POLICIES))))
        for k in ('scales', 'names'):
            cfg[k] = dict((str(s).upper(), v) for s, v in (data.get(k) or {}).items())
        for serial, scale in cfg['scales'].items():
            if scale not in P.CURRENT_SCALES:
                raise SystemExit('config: unknown current scale %r for %s' % (scale, serial))
        for serial in (data.get('excluded') or []):
            serial = str(serial).upper()
            if len(serial) != 12 or any(ch not in '0123456789ABCDEF' for ch in serial):
                raise SystemExit('config: excluded: serial must be 12 hex digits, got %r' % serial)
            cfg['excluded'].append(serial)
        for ifname, ent in (data.get('display') or {}).items():
            if not isinstance(ent, dict) or 'serial' not in ent:
                raise SystemExit('config: display.%s needs {"serial": ..., "board_addr": ...}' % ifname)
            name, parsed = parse_display_spec('%s=%s:%s' % (ifname, ent['serial'], ent.get('board_addr', 1)))
            cfg['display'][name] = parsed
    for spec in display_specs or ():
        name, parsed = parse_display_spec(spec)
        cfg['display'][name] = parsed
    return cfg


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--interface', default='can0')
    ap.add_argument('--device', default=None, metavar='SYSFS_DEV',
                    help='refuse to start unless --interface is backed by this device (e.g. 1c2bc00.can, '
                         'the Cerbo VE.Can controller); interface names follow probe order and can shift')
    ap.add_argument('--expected', type=int, default=2, help='number of units to discover')
    ap.add_argument('--dcsource', action='store_true', help='also publish the dcsource mirror for VRM')
    ap.add_argument('--capture', default=None, help='candump-format log of all frames')
    ap.add_argument('--config', default=DEFAULT_CONFIG)
    ap.add_argument('--control', action='store_true', help='request control (granted only when protocol.VERIFIED)')
    ap.add_argument('--no-link-setup', action='store_true', help='do not touch ip link (interface already up)')
    ap.add_argument('--quiet-start', type=float, default=Engine_QUIET, metavar='S',
                    help='seconds of silence towards a newly seen unit before the first request (default %(default)s)')
    ap.add_argument('--display', action='append', default=[], metavar='IF=SERIAL[:ADDR]',
                    help='display repeater: board interface and the serial of its unit (repeatable)')
    ap.add_argument('--log-level', default='INFO')
    args = ap.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format='%(asctime)s %(levelname)s %(name)s %(message)s')
    log.info('huawei driver %s starting on %s (protocol verified: %s)', VERSION, args.interface, P.VERIFIED)
    cfg = load_config(args.config, args.display)

    import dbus
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib
    from vedbus import VeDbusItemImport
    from huawei.dbus_services import VenusBackend
    DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()

    if args.device:
        have = iface_device(args.interface)
        if have != args.device:
            log.error('%s is backed by device %r (driver %r), expected %r: interface names shifted, refusing to start',
                      args.interface, have, iface_driver(args.interface), args.device)
            return 5
        log.info('%s is %s (driver %s)', args.interface, have, iface_driver(args.interface))
    profile = VeDbusItemImport(bus, 'com.victronenergy.settings', '/Settings/Canbus/%s/Profile' % args.interface)
    lm = LinkManager(args.interface, P.BITRATE, profile_getter=profile.get_value)
    if not lm.profile_ok():
        log.error('VE.Can profile for %s is %r, not Disabled (0); refusing to start', args.interface, profile.get_value())
        return 3
    if not args.no_link_setup:
        try:
            st = lm.ensure_up()
            log.info('%s up: %s', args.interface, {k: st[k] for k in ('state', 'bitrate')})
        except CanError as e:
            log.error('%s', e)
            return 4
    try:
        link = SocketCanLink(args.interface)
    except (CanError, OSError) as e:
        log.error('cannot open %s: %s', args.interface, e)
        return 4

    # at boot this service can start before dbus-systemcalc owns com.victronenergy.system; the
    # VeDbusItemImport below would raise ServiceUnknown and the supervisor would restart us with
    # a traceback in the log (seen 13:02 and 13:39 on 2026-11-03). Wait for it instead.
    t_wait = time.monotonic()
    while not bus.name_has_owner('com.victronenergy.system'):
        if time.monotonic() - t_wait > SYSTEM_SERVICE_WAIT_S:
            log.error('com.victronenergy.system not on the bus after %.0f s; giving up', SYSTEM_SERVICE_WAIT_S)
            return 6
        if int(time.monotonic() - t_wait) % 10 == 0:
            log.info('waiting for com.victronenergy.system (dbus-systemcalc) ...')
        time.sleep(1.0)

    clock = time.monotonic
    watchdog = DvccWatchdog(clock)
    control_flag = VeDbusItemImport(bus, 'com.victronenergy.system', '/Control/ChargerChargeCurrent',
                                    eventCallback=lambda svc, path, changes: watchdog.feed_control_flag(changes.get('Value')))
    watchdog.feed_control_flag(control_flag.get_value())
    commander = make_commander(clock, args.control, cfg['output_enable'])
    capture = CandumpWriter(args.capture, args.interface, clock=time.time) if args.capture else None

    # display repeater ports: each on its own interface, each failure isolated
    from huawei.display import DisplayPort, DisplayRepeater
    from huawei import display as display_mod
    ports = {}
    watches = {}                        # ifname -> GLib source id of the read watch

    def watch_port(port):
        old = watches.pop(port.ifname, None)
        if old is not None:
            GLib.source_remove(old)
        if port.link is None:
            return

        def on_board(fd, cond, ifname=port.ifname):
            try:
                display.on_board_readable(ifname)
            except Exception as e:          # a board port never takes the engine down
                log.error('display port %s: %r', ifname, e)
            return True
        watches[port.ifname] = GLib.io_add_watch(port.link.fileno(), GLib.IO_IN, on_board)

    def board_link(ifname):
        """Open a board port only when the interface really is a USB adapter channel: a
        built-in port that inherited the name must never be driven as a board bus."""
        drv = iface_driver(ifname)
        if drv != USB_ADAPTER_DRIVER:
            raise CanError('%s is driver %r, not %s (device %r)' % (ifname, drv, USB_ADAPTER_DRIVER, iface_device(ifname)))
        return SocketCanLink(ifname)

    for ifname, ent in sorted(cfg['display'].items()):
        pprof = VeDbusItemImport(bus, 'com.victronenergy.settings', '/Settings/Canbus/%s/Profile' % ifname)
        plm = LinkManager(ifname, P.BITRATE, profile_getter=pprof.get_value)
        if not plm.profile_ok():
            log.error('display port %s skipped: VE.Can profile is %r, not Disabled (0)', ifname, pprof.get_value())
            continue
        plink = None
        try:
            if not args.no_link_setup:
                st = plm.ensure_up()
                log.info('%s up: %s', ifname, {k: st[k] for k in ('state', 'bitrate')})
            plink = board_link(ifname)
        except (CanError, OSError) as e:
            log.error('display port %s not available yet (%s); retrying every %.0f s',
                      ifname, e, display_mod.RECOVER_INTERVAL_S)
        ports[ifname] = DisplayPort(plink, ifname, ent['serial'], ent['board_addr'], clock=clock,
                                    capture=capture, link_manager=None if args.no_link_setup else plm,
                                    module_link=link, module_ifname=args.interface,
                                    link_factory=lambda ifname=ifname: board_link(ifname),
                                    ifindex_getter=socket.if_nametoindex, on_relink=watch_port)
        log.info('display port %s: unit %s, board address %d', ifname, ent['serial'], ent['board_addr'])
    display = DisplayRepeater(ports, clock) if ports else None

    engine = Engine(link, clock, Registry(clock, cfg['scales'], excluded=cfg['excluded']), watchdog, commander,
                    backend=VenusBackend(bus), expected_units=args.expected, control_enabled=args.control,
                    capture=capture, link_manager=lm, dcsource=args.dcsource, names=cfg['names'],
                    quiet_start_s=args.quiet_start, display=display,
                    bench_age=lambda: marker_age(BENCH_MARKER))
    log.info('commander: %s, quiet start %.0f s, scales %s, output_enable %s, excluded %s',
             commander.name, engine.quiet_start_s, cfg['scales'], cfg['output_enable'], cfg['excluded'] or 'none')

    rc = [0]

    def failed(where, exc):
        # engine.failsafe() already ran (zero written, services off-line); leave the loop so
        # daemontools restarts us into a fresh quiet start
        log.error('engine failure in %s: %s', where, exc)
        rc[0] = 5
        loop.quit()
        return False

    def on_readable(fd, cond):
        for _ in range(64):
            try:
                f = link.recv(0)
            except CanError as e:
                log.error('recv: %s', e)
                return True
            if f is None:
                break
            try:
                engine.on_frame(f)
            except EngineFailure as e:
                return failed('on_frame', e)
        return True

    def on_tick():
        try:
            engine.tick()
        except EngineFailure as e:
            return failed('tick', e)
        except Exception as e:          # cannot happen (engine wraps), belt and braces
            engine.failsafe('tick: %r' % (e,))
            return failed('tick', e)
        return True

    GLib.io_add_watch(link.fileno(), GLib.IO_IN, on_readable)
    GLib.timeout_add(200, on_tick)

    fankick_mtime = [None]

    def on_fankick():
        if display is None:
            return True
        try:
            mtime = os.stat(FANKICK_FILE).st_mtime
        except OSError:
            mtime = None
        if mtime == fankick_mtime[0]:
            return True
        fankick_mtime[0] = mtime
        new = {}
        if mtime is not None:
            try:
                for line in open(FANKICK_FILE):
                    parts = line.split()
                    if len(parts) == 3 and not line.startswith('#'):
                        reg = -1 if parts[1] == 'mute' else int(parts[1], 16)
                        new.setdefault(parts[0], {})[reg] = int(parts[2])
            except (OSError, ValueError) as e:
                log.error('fankick: %s', e)
        for ifname, port in display.ports.items():
            port.overrides = new.get(ifname, {})
        log.warning('fankick: overrides now %s', {k: {hex(r): v for r, v in d.items()} for k, d in new.items()} or 'none')
        return True
    GLib.timeout_add(2000, on_fankick)
    if display is not None:
        for port in display.ports.values():
            watch_port(port)
    GLib.timeout_add(60000, lambda: (log.info('summary %s', json.dumps(engine.summary(), default=str)), True)[1])
    loop = GLib.MainLoop()

    def stop(*_):
        log.info('stopping')
        engine.stop()
        loop.quit()
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    loop.run()
    if capture:
        capture.close()
    link.close()
    if display is not None:
        for port in display.ports.values():
            if port.link is not None:
                port.link.close()
    return rc[0]


if __name__ == '__main__':
    sys.exit(main())
