"""Control logic for the Huawei driver: DVCC liveness watchdog, per-unit governor,
and the commanders that turn a request into frames (or refuse to).

Output gate (brief section 6, design record): no 0x80 set frame leaves this process
unless ALL of the following hold, checked on every tick:
  1. protocol.VERIFIED is True (Phase B evidence; a source-code constant, not a setting),
  2. the engine was started with control enabled,
  3. the unit's serial has a current-scale policy bound in the configuration,
  4. the DVCC watchdog is alive (heartbeat counter changing, /Control/ChargerChargeCurrent
     == 1, no dvcc_unsupported marker),
  5. the operator switch (/Mode) is on and the unit is READY,
  6. the unit's stored default limit was seen to be zero (modules.Module.default_ok): the
     CanCommander sends nothing at all, not even a zero, to a unit without that verdict,
  7. the unit's serial is not in the configuration's "excluded" list (2026-11-04: unit 1 caps
     itself at ~1.65 A; an excluded unit is polled and published but never written to, and the
     bench window applies to it even in control mode so the ramp tool can still drive it).
Any failed condition yields a zero request with a reason.
Module.able (set here) tells the D-Bus side whether the unit may be
advertised to the DVCC as a charger that can deliver (/State 252): true when every condition
on our side holds and only the DVCC budget is missing or zero. Without that the patched DVCC,
which allocates only to chargers with /State != 0, would never hand out a budget (2026-11-04).
Zero is also "a request": in
control mode it is sent (so the unit's on-line limit is zero while the controller is
present). In telemetry-only mode nothing is sent; the unit sits on its stored offline
default (0 A on unit 1 since B3.0, 2026-10-25) because the engine's quiet start lets a
previous master's on-line limit expire before the first request (measured B3.4: polling
keeps a unit on-line, so the fallback is a dead-controller protection, not something a
polling driver can rely on while it runs).

Output-enable sequence (OUTPUT_ENABLE_POLICIES, configured as "output_enable" in
driver.json, default "none"): a unit powered up with no controller leaves its output stage
off (2.6 V on the terminals, measured 2026-11-01) and it is not yet known whether a
setpoint write alone switches it on or whether the standby register 0x32 must be written,
nor which value of 0x32 means "on" (the sources disagree). Until the bench answers this the
policy is "none" and the commander writes only 0x00 / 0x03. With "standby0" (on = 0, off = 1)
or "standby1" (on = 1, off = 0) the commander writes the "on" value before the first
non-zero current limit (order: voltage, standby, current), repeats it with the keep-alive,
and writes the "off" value when the request drops to zero and on shutdown. The 0x32 write
is acknowledged like every other write (modules.Module.output).

Numbers:
  HEARTBEAT_TIMEOUT_S  DVCC writes /Huawei/Heartbeat every ADJUST (3 s); 10 s is three
                       missed ticks.
  RAMP_UP_A_PER_S      limit on how fast the requested current may rise (unverified
                       control-loop figure; Phase B B2.x sets the final value).
  VOLTAGE_MIN/MAX_V    hard clamp independent of DVCC (Pylontech CVL 52.4 V; 55.2 V is
                       the harness's own cap).
"""
import logging
import os

from . import protocol as P
from . import modules as M

log = logging.getLogger('huawei.control')

HEARTBEAT_TIMEOUT_S = 10.0
RAMP_UP_A_PER_S = 5.0
VOLTAGE_MIN_V = 41.0
VOLTAGE_MAX_V = 55.2
RESEND_S = 5.0                 # repeat the live setpoints at least this often (keep-alive)
OUTPUT_ENABLE_POLICIES = {     # name -> (value meaning ON, value meaning OFF) for register 0x32
    'none': None,
    'standby0': (0, 1),        # community tables: standby = 0 means output on
    'standby1': (1, 0),        # the opposite reading, in case the bench shows it
}


class DvccWatchdog(object):
    """Liveness of the patched DVCC as seen from the charger service(s)."""

    def __init__(self, clock, marker_path='/data/huawei/dvcc_unsupported', timeout=HEARTBEAT_TIMEOUT_S):
        self.clock = clock
        self.marker_path = marker_path
        self.timeout = timeout
        self.last_value = None
        self.last_change_t = None
        self.control_flag = None
        self.beats = 0

    def feed_heartbeat(self, value):
        """Called from the /Huawei/Heartbeat write callback. Only a changed value counts
        (vedbus swallows unchanged SetValues anyway)."""
        if value != self.last_value:
            self.last_value = value
            self.last_change_t = self.clock()
            self.beats += 1

    def feed_control_flag(self, value):
        self.control_flag = value

    def marker_present(self):
        return os.path.exists(self.marker_path)

    def age(self, now=None):
        now = self.clock() if now is None else now
        return None if self.last_change_t is None else now - self.last_change_t

    def alive(self, now=None):
        now = self.clock() if now is None else now
        age = self.age(now)
        return (age is not None and age <= self.timeout and self.control_flag == 1
                and not self.marker_present())

    def reason(self, now=None):
        now = self.clock() if now is None else now
        if self.marker_present():
            return 'dvcc_unsupported marker present'
        if self.control_flag != 1:
            return 'patched DVCC not in control (/Control/ChargerChargeCurrent != 1)'
        age = self.age(now)
        if age is None:
            return 'no DVCC heartbeat yet'
        if age > self.timeout:
            return 'DVCC heartbeat stale (%.0f s)' % age
        return None


class Inputs(object):
    """What the D-Bus side knows for one unit (written by dbus_services callbacks)."""

    def __init__(self):
        self.link_current = None    # /Link/ChargeCurrent (A) from DVCC
        self.link_current_t = None
        self.link_voltage = None    # /Link/ChargeVoltage (V)
        self.network_mode = None
        self.mode_on = False        # /Mode 1 = on, 4 = off (operator)
        self.ccl = 0.0              # /Settings/ChargeCurrentLimit (per-unit cap, A)


class Request(object):
    def __init__(self, current_a, voltage_v, enable, reasons):
        self.current_a = current_a
        self.voltage_v = voltage_v
        self.enable = enable
        self.reasons = reasons

    def __repr__(self):
        return 'Request(%.1f A, %s V, enable=%s, %s)' % (
            self.current_a, 'None' if self.voltage_v is None else '%.2f' % self.voltage_v,
            self.enable, '; '.join(self.reasons) or 'ok')


class Governor(object):
    """Turns inputs into a bounded request for one unit. Pure: no I/O."""

    def __init__(self, clock, control_enabled, watchdog, ramp_a_per_s=RAMP_UP_A_PER_S):
        self.clock = clock
        self.control_enabled = control_enabled
        self.watchdog = watchdog
        self.ramp = ramp_a_per_s
        self.last_current = 0.0
        self.last_t = None

    def compute(self, module, inputs, now=None):
        now = self.clock() if now is None else now
        reasons = []
        if not P.VERIFIED:
            reasons.append('protocol unverified (Phase B pending)')
        if not self.control_enabled:
            reasons.append('control disabled')
        if module.excluded:
            reasons.append('excluded by configuration (driver.json "excluded")')
        if module.scale is None:
            reasons.append('no current scale bound to serial %s' % module.serial)
        elif P.CURRENT_SCALES[module.scale].needs_nominal and not module.nominal_a:
            reasons.append('nominal current unknown')
        wd = self.watchdog.reason(now)
        if wd:
            reasons.append(wd)
        if not inputs.mode_on:
            reasons.append('operator switch off')
        if module.default_ok is None:
            reasons.append('stored default limit not seen yet')
        elif module.default_ok is False:
            reasons.append('stored default limit NOT zero (ratio %s): unit not commissioned' % module.default_ratio)
        if module.state not in (M.READY, M.INHIBITED):
            reasons.append('unit %s' % module.state)
        # The unit is "able" when everything on our side allows output and only the DVCC's
        # budget is missing or zero. The patched DVCC hands a budget only to chargers whose
        # /State is not 0 and writes an explicit 0 to the others, so a unit that reported
        # itself unable while waiting for a budget would never receive one (deadlock seen on
        # the Cerbo 2026-11-04 17:41, first run through the driver with the generator).
        able = not reasons
        if inputs.link_current is None:
            reasons.append('no DVCC current limit')
        # No freshness verdict on /Link/ChargeCurrent itself: vedbus swallows a SetValue
        # that repeats the current value, so an unchanged limit never reaches the callback.
        # The DVCC's liveness is the heartbeat counter (watchdog above), written on every
        # tick in which it also writes the limits.

        if reasons:
            target = 0.0
        else:
            target = max(0.0, min(float(inputs.link_current), float(inputs.ccl or 0.0)))
            if module.nominal_a:
                target = min(target, module.nominal_a * P.CURRENT_RATIO_MAX)
            if target < inputs.link_current:
                reasons.append('capped at %.1f A' % target)
        # ramp up only; down is immediate
        if self.last_t is not None and target > self.last_current:
            allowed = self.last_current + self.ramp * max(0.0, now - self.last_t)
            if target > allowed:
                reasons.append('ramping')
                target = allowed
        self.last_current = target
        self.last_t = now

        voltage = None
        if inputs.link_voltage is not None:
            voltage = max(VOLTAGE_MIN_V, min(VOLTAGE_MAX_V, float(inputs.link_voltage)))
            if voltage != inputs.link_voltage:
                reasons.append('voltage clamped to %.2f V' % voltage)
        enable = target > 0.0 and not [r for r in reasons if r not in ('ramping',) and not r.startswith('capped')
                                       and not r.startswith('voltage clamped')]
        module.inhibit = not enable
        module.able = able
        module.inhibit_reason = '; '.join(reasons) if reasons else None
        return Request(round(target, 1), voltage, enable, reasons)


class TelemetryOnlyCommander(object):
    """Never transmits a set frame. Records what a control commander would have done so
    the log and the tests can show the decision path."""

    name = 'telemetry-only'

    def __init__(self, clock):
        self.clock = clock
        self.would_send = []        # (t, serial, request)

    def apply(self, module, request, link, now=None):
        now = self.clock() if now is None else now
        module.current.requested = request.current_a
        module.current.requested_t = now
        module.voltage.requested = request.voltage_v
        module.voltage.requested_t = now
        self.would_send.append((now, module.serial, request))
        return []                   # frames sent: none, ever

    def shutdown(self, modules, link):
        return []


class CanCommander(object):
    """Sends live setpoints (0x03 current, 0x00 voltage) to one unit by address and
    tracks acknowledgements. Construction is refused unless protocol.VERIFIED is True.
    Non-volatile registers (0x01, 0x04, 0x02) are never written here: that is a
    commissioning action done with the rsh harness, with readback by measurement."""

    name = 'can'

    def __init__(self, clock, output_enable='none'):
        if not P.VERIFIED:
            raise RuntimeError('CanCommander refused: protocol not verified (Phase B pending)')
        if output_enable not in OUTPUT_ENABLE_POLICIES:
            raise ValueError('unknown output_enable policy %r' % (output_enable,))
        self.clock = clock
        self.output_enable = output_enable
        self.output_values = OUTPUT_ENABLE_POLICIES[output_enable]
        self.sent = []

    def apply(self, module, request, link, now=None):
        now = self.clock() if now is None else now
        frames = []
        module.current.requested = request.current_a
        module.current.requested_t = now
        module.voltage.requested = request.voltage_v
        module.voltage.requested_t = now
        if module.excluded:
            return frames           # telemetry only for this unit (config), whatever the request
        if module.addr is None or module.scale is None or module.default_ok is not True:
            return frames           # nothing, not even zero: the first burst must show the stored default
        if not module.alive(now):
            return frames           # a unit that is gone gets nothing (its own fallback applies)
        due = module.current.sent_t is None or now - module.current.sent_t >= RESEND_S
        if request.voltage_v is not None and (request.voltage_v != module.voltage.sent_value or due):
            if P.voltage_in_range('online_voltage', request.voltage_v):
                data, raw = P.encode_set_data('online_voltage', request.voltage_v)
                frames.append(self._tx(link, P.request_id(P.CMD_SET, module.addr), data, now))
                module.voltage.sent_raw, module.voltage.sent_value, module.voltage.sent_t = raw, request.voltage_v, now
                module.voltage.ack_ok = None
        if self.output_values is not None:
            want = self.output_values[0] if request.current_a > 0 else self.output_values[1]
            if want != module.output.sent_value or (due and request.current_a > 0):
                frames.append(self._output(link, module, want, now))
        if request.current_a != module.current.sent_value or due:
            data, raw = P.encode_set_data('online_current', request.current_a, module.nominal_a, module.scale)
            frames.append(self._tx(link, P.request_id(P.CMD_SET, module.addr), data, now))
            if module.current.sent_value is not None and request.current_a < module.current.sent_value:
                module.current.down_t = now     # the unit winds down slowly: no overshoot verdict for a while
            module.current.sent_raw, module.current.sent_value, module.current.sent_t = raw, request.current_a, now
            module.current.ack_ok = None
        return frames

    def _output(self, link, module, value, now):
        data, raw = P.encode_set_data('standby', value)
        f = self._tx(link, P.request_id(P.CMD_SET, module.addr), data, now)
        module.output.sent_raw, module.output.sent_value, module.output.sent_t = raw, value, now
        module.output.ack_ok = None
        return f

    def _tx(self, link, can_id, data, now):
        from .canbus import CanFrame
        f = CanFrame(can_id, data, ext=True, ts=now)
        link.send(f)
        self.sent.append(f)
        return f

    def shutdown(self, modules, link):
        """Zero current to every addressed unit, once, on exit."""
        out = []
        for m in modules:
            if m.default_ok is not True or m.excluded:
                continue            # never written to, nothing to undo
            if m.addr is not None and m.scale is not None and (m.nominal_a or not P.CURRENT_SCALES[m.scale].needs_nominal):
                data, raw = P.encode_set_data('online_current', 0.0, m.nominal_a, m.scale)
                out.append(self._tx(link, P.request_id(P.CMD_SET, m.addr), data, self.clock()))
                if self.output_values is not None and m.output.sent_value == self.output_values[0]:
                    out.append(self._output(link, m, self.output_values[1], self.clock()))
        return out


def make_commander(clock, control_enabled, output_enable='none'):
    """The only place a commander is chosen. Control is granted solely when the
    protocol is verified AND control was requested; otherwise telemetry-only."""
    if output_enable not in OUTPUT_ENABLE_POLICIES:
        raise ValueError('unknown output_enable policy %r' % (output_enable,))
    if control_enabled and P.VERIFIED:
        return CanCommander(clock, output_enable)
    if control_enabled:
        log.warning('control requested but protocol.VERIFIED is False: running telemetry-only')
    return TelemetryOnlyCommander(clock)
