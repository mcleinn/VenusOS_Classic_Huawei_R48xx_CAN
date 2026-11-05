"""The driver's core loop without any event framework: `on_frame()` for every received
CAN frame and `tick()` every ~200 ms. main.py drives it from GLib on the Cerbo; the tests
drive it with a fake clock on a virtual bus.

Per tick:
  1. bus health: error frames are counted; a BUS-OFF error frame asks the link manager to
     restart the interface (restart-ms also does it in the kernel) and marks all units stale.
  2. discovery: a broadcast info request at start and every INFO_BROADCAST_S while fewer
     than `expected_units` serials are known; a per-address info request for every address
     that sends frames but has no serial yet.
  3. polling: one status request per known address every POLL_S (addresses are polled
     individually, never broadcast, so bursts do not interleave). After every complete
     burst whose 0x75 reads 0 a GET 0x85 fetches the output voltage (R4875G5).
     Once per identified unit an e-label request fetches the model name.
  4. state: registry.evaluate(); a service is created the moment a serial is learned.
  5. control: governor.compute() + commander.apply() per identified unit, then
     service.update().
Everything transmitted or received can be written to a candump log (`capture`).

Quiet start (measured B3.4, 2026-10-25: status polling alone keeps a unit on-line, so a
polled unit never falls back to its stored zero-current default). In telemetry-only mode
the engine therefore sends NOTHING to a unit, not even an info or status request, until
QUIET_START_S after that unit's first frame: whatever on-line limit a previous master
left behind has then expired (fallback measured after 75 s of silence) and the unit sits
on its stored defaults before this process starts keeping it on-line. The CanCommander
needs it as well: the first burst after the quiet start shows the stored default in 0x76
(modules.Module.default_ok), which is what allows any write at all.

Display repeater (display.py, optional): after registry.feed() every decoded module-bus
frame is handed to it with the module it was attributed to; it forwards to the boards and
never touches this link. Its ports are drained by on_board_readable() from main's GLib
watches (tests call it directly).

Bench window (2026-11-03): a bench tool that writes on-line limits beside this process
(src/deploy/huawei-ramp.py, the stepped first-load test) keeps the marker file
BENCH_MARKER touched; while the marker is younger than BENCH_STALE_S and the commander is
telemetry-only, every module's `external_master` flag is set so modules.py does not raise
the uncommanded-output fault for the current the bench commands. A marker that stops
being touched (bench tool killed) expires by itself. With the CanCommander the marker is
ignored: two masters never share the bus in control mode.

Fail-safe (item 4 of the 2026-11-01 control build): an exception anywhere in tick() or
on_frame() calls failsafe(): zero to every commanded unit, every service marked off-line,
capture flushed, then EngineFailure is raised so main exits non-zero and daemontools
restarts the process into a fresh quiet start. A stalled process must never keep polling.
"""
import logging

from . import protocol as P
from . import modules as M
from .canbus import CanFrame, CanError
from .control import Governor, Inputs

log = logging.getLogger('huawei.engine')

POLL_S = 1.0
INFO_RETRY_S = 2.0
INFO_BROADCAST_S = 30.0
ELABEL_RETRY_S = 15.0
QUIET_START_S = 75.0        # > the 60 s timeout of the sources; 75 s silence measured sufficient
BUS_OFF_CLASS = 0x40
BENCH_MARKER = '/data/huawei/bench'
BENCH_STALE_S = 60.0        # a marker older than this no longer opens the window
ERR_RATE_WARN = 20          # error frames per tick window worth a log line


class EngineFailure(Exception):
    """Raised after failsafe() ran; main.py exits on it."""


class Engine(object):
    def __init__(self, link, clock, registry, watchdog, commander, backend=None,
                 expected_units=2, control_enabled=False, capture=None, link_manager=None,
                 dcsource=False, names=None, quiet_start_s=QUIET_START_S, display=None,
                 bench_age=None):
        self.link = link
        self.clock = clock
        self.registry = registry
        self.watchdog = watchdog
        self.commander = commander
        self.backend = backend
        self.expected_units = expected_units
        self.control_enabled = control_enabled
        self.capture = capture
        self.link_manager = link_manager
        self.dcsource = dcsource
        self.names = names or {}
        self.display = display      # display.DisplayRepeater or None
        self.bench_age = bench_age  # callable -> age of the bench marker in s, or None
        self.bench_open = False
        self.services = {}          # serial -> ChargerService
        self.inputs = {}            # serial -> Inputs
        self.governors = {}         # serial -> Governor
        self.last_poll = {}         # addr -> t
        self.last_info = {}         # addr -> t
        self.last_elabel = {}       # serial -> t
        self.last_broadcast_info = None
        self.quiet_start_s = float(quiet_start_s)
        self.held_requests = 0      # requests not sent because of the quiet start
        self.failed = None          # failsafe reason once it ran
        self.started = clock()
        self.stats = {'rx': 0, 'tx': 0, 'err': 0, 'bus_off': 0, 'non_huawei': 0, 'tx_fail': 0, 'failsafe': 0}
        self.err_window = 0
        self.stopped = False

    # ------------------------------------------------------------------ io
    def _send(self, can_id, data):
        f = CanFrame(can_id, data, ext=True, ts=self.clock())
        try:
            self.link.send(f)
        except CanError as e:
            self.stats['tx_fail'] += 1
            log.warning('tx failed: %s', e)
            return None
        self.stats['tx'] += 1
        if self.capture:
            self.capture.write(f, 'tx')
        return f

    def on_frame(self, frame):
        if self.stopped:
            return None
        try:
            return self._on_frame(frame)
        except EngineFailure:
            raise
        except Exception as e:
            self.failsafe('on_frame: %r' % (e,))
            raise EngineFailure(str(e))

    def _on_frame(self, frame):
        # One clock domain for everything inside the engine: the link stamps frames with
        # wall time, freshness windows use self.clock (monotonic on the Cerbo).
        frame.ts = self.clock()
        if self.capture:
            self.capture.write(frame, 'rx')
        if frame.err:
            self.stats['err'] += 1
            self.err_window += 1
            if frame.can_id & BUS_OFF_CLASS:
                self.stats['bus_off'] += 1
                log.error('bus-off reported by the controller')
                if self.link_manager is not None:
                    try:
                        self.link_manager.ensure_up()
                    except CanError as e:
                        log.error('link restart failed: %s', e)
            return None
        if frame.echo:
            return None
        self.stats['rx'] += 1
        dec = P.decode(frame)
        if dec is None:
            self.stats['non_huawei'] += 1
            return None
        before = set(self.registry.by_addr.values())
        module = self.registry.feed(dec)
        for serial in set(self.registry.by_addr.values()) - before:
            self._ensure_service(self.registry.modules[serial])
        if self.display is not None:
            self.display.on_module_frame(frame, dec, module)
        if (module is not None and dec['kind'] == 'status' and not dec['cont']
                and module.addr is not None and not module.status.get('out_voltage_v')):
            # burst complete and 0x75 empty: fetch the voltage from 0x85 (R4875G5)
            self._send(P.request_id(P.CMD_GET, module.addr), P.get_data(P.OUT_VOLTAGE_ALT_REG))
        return module

    def _quiet(self, module, now):
        """True while this unit must not be addressed yet (see the module docstring)."""
        if now - module.first_seen < self.quiet_start_s:
            self.held_requests += 1
            return True
        return False

    # ---------------------------------------------------------------- tick
    def tick(self, now=None):
        if self.stopped:
            return
        try:
            self._tick(now)
        except EngineFailure:
            raise
        except Exception as e:
            self.failsafe('tick: %r' % (e,))
            raise EngineFailure(str(e))

    def _tick(self, now=None):
        now = self.clock() if now is None else now
        if self.err_window >= ERR_RATE_WARN:
            log.warning('%d CAN error frames since last tick', self.err_window)
        self.err_window = 0

        # discovery (a broadcast reaches every unit: held while any unit is in its quiet window
        # or while the process itself is younger than the window)
        known = len(self.registry.identified())
        quiet_any = (now - self.started < self.quiet_start_s or
                     any(self._quiet(m, now) for m in self.registry.modules.values() if m.alive(now)))
        if known < self.expected_units and not quiet_any and (
                self.last_broadcast_info is None or now - self.last_broadcast_info >= INFO_BROADCAST_S):
            self._send(P.request_id(P.CMD_INFO, 0), bytes(8))
            self.last_broadcast_info = now
        for m in list(self.registry.modules.values()):
            if m.identified() or m.addr is None or not m.alive(now) or self._quiet(m, now):
                continue
            if now - self.last_info.get(m.addr, -1e9) >= INFO_RETRY_S:
                self._send(P.request_id(P.CMD_INFO, m.addr), bytes(8))
                self.last_info[m.addr] = now

        # polling, one address at a time; e-label once per identified unit
        for m in list(self.registry.modules.values()):
            addr = m.addr
            if addr is None or not m.alive(now) or self._quiet(m, now):
                continue
            if now - self.last_poll.get(addr, -1e9) >= POLL_S:
                self._send(P.request_id(P.CMD_DATA, addr), P.status_request_data())
                self.last_poll[addr] = now
            if m.identified() and m.model is None and now - self.last_elabel.get(m.serial, -1e9) >= ELABEL_RETRY_S:
                self._send(P.request_id(P.CMD_ELABEL, addr), bytes(8))
                self.last_elabel[m.serial] = now

        # bench window: suspend the uncommanded-output check while a bench tool is at work
        age = self.bench_age() if self.bench_age is not None else None
        fresh = age is not None and 0 <= age < BENCH_STALE_S
        bench = fresh and self.commander.name == 'telemetry-only'
        if bench != self.bench_open:
            self.bench_open = bench
            if bench:
                log.warning('bench window open (%s, age %.0f s): an external master is expected to '
                            'command output, the uncommanded-output check is suspended', BENCH_MARKER, age)
            else:
                log.warning('bench window closed: uncommanded-output check active again')
        for m in self.registry.modules.values():
            # an excluded unit is never written to by this process, so a fresh marker opens the
            # window for it in control mode as well (the ramp tool drives unit 1 beside the driver)
            m.external_master = bench or (fresh and m.excluded)

        # state and control
        self.registry.evaluate(now)
        if self.display is not None:
            self.display.tick(now)
        for m in self.registry.identified():
            self._ensure_service(m)
            inputs = self.inputs[m.serial]
            gov = self.governors[m.serial]
            req = gov.compute(m, inputs, now)
            m.evaluate(now)        # inhibit flag may have changed
            self.commander.apply(m, req, self.link, now)
            self.services[m.serial].update(now)

    def _ensure_service(self, module):
        if module.serial in self.services or not module.identified():
            return
        inputs = Inputs()
        self.inputs[module.serial] = inputs
        self.governors[module.serial] = Governor(self.clock, self.control_enabled, self.watchdog)
        if self.backend is not None:
            from .dbus_services import ChargerService
            self.services[module.serial] = ChargerService(
                module, inputs, self.watchdog, self.backend,
                custom_name=self.names.get(module.serial), dcsource=self.dcsource,
                display_port=self.display.port_for(module.serial) if self.display is not None else None)
        else:
            self.services[module.serial] = _NullService()
        log.info('service created for %s at addr %s', module.serial, module.addr)

    # ------------------------------------------------------------------ end
    def failsafe(self, reason):
        """Last action of a failing engine: zero to every commanded unit, services off-line,
        no further polling (a polled unit would keep its on-line limit). Idempotent."""
        if self.failed is not None:
            return
        self.failed = reason
        self.stats['failsafe'] += 1
        log.error('FAILSAFE: %s', reason)
        self.stop()

    def stop(self):
        if self.stopped:
            return
        self.stopped = True
        try:
            self.commander.shutdown(self.registry.identified(), self.link)
        except CanError as e:
            log.warning('shutdown tx failed: %s', e)
        for s in self.services.values():
            s.mark_offline()
        if self.capture:
            self.capture.flush()

    def summary(self):
        return {'stats': dict(self.stats), 'commander': self.commander.name,
                'control_enabled': self.control_enabled, 'verified': P.VERIFIED,
                'quiet_start_s': self.quiet_start_s, 'held_requests': self.held_requests,
                'failed': self.failed, 'bench_open': self.bench_open,
                'display': self.display.summary() if self.display is not None else None,
                'units': [m.summary() for m in self.registry.modules.values()],
                'watchdog': {'alive': self.watchdog.alive(), 'reason': self.watchdog.reason(),
                             'beats': self.watchdog.beats}}


class _NullService(object):
    def update(self, now):
        pass

    def mark_offline(self):
        pass
