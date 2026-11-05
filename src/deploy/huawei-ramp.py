#!/usr/bin/env python3
"""huawei-ramp: operator-paced first-load test of ONE Huawei rectifier, beside the running driver.

    ssh -t cerbo python3 /data/huawei/huawei-ramp.py --unit 1            # charger "Huawei 1"
    ssh -t cerbo python3 /data/huawei/huawei-ramp.py --unit 2 --steps 2,5,10,20,30,40,50
    ssh -t cerbo python3 /data/huawei/huawei-ramp.py --unit 1 --dry-run  # reads only, no set frame

What it does
  * Writes the on-line voltage and an on-line current limit to the unit under test, in ROUNDS:
    each step is held for as long as the operator wants (at least --min-hold seconds), all
    readings are taken, and only then does the operator release the next step (Enter). Nothing
    moves on its own. The limit never exceeds the ceiling the live driver would obey at that
    moment (see ceiling()); the transition between two rounds rises at the governor's
    RAMP_UP_A_PER_S, a lower target or a dropping ceiling is written at once. Every write is
    checked for its acknowledgement.
  * Prints one line per second: limit sent, the unit's own current (0x81), voltage, temperatures,
    AC input, the battery's view (BMS current, SOC, charge limits) and which term binds.
  * Infers the current scale the unit obeys from 0x81 against the raw value sent (the
    `conservative` policy sends the smallest raw of the three community readings, so the
    measured current tells which one the unit uses); clamp readings typed by the operator go
    into the log next to it.
  * Stops the output (0 A) on: any ack error or missing ack, overshoot above the sent limit,
    temperature above --max-temp, voltage above the target + 1 V, the unit's status older than
    5 s (driver gone), the OTHER unit showing current, the battery above its CVL, Ctrl-C, any
    exception. The last action of the process is always a 0 A write with its ack, then a check
    that the unit's current has fallen below 1 A.
  * Keeps the driver's bench marker (/data/huawei/bench) touched so the driver does not raise
    "uncommanded output" for the current this tool commands; the marker expires 60 s after
    the tool dies. The driver keeps polling and keeps the boards (fans) fed.

What it refuses (unless --dry-run, which never writes)
  driver not running or older than 0.7-bench; unit not identified or faulted; stored default
  limit not seen as zero; DC side not connected (unit voltage register < 40 V or more than 2 V
  from the battery voltage); operator switch (/Mode) off; per-unit /Settings/ChargeCurrentLimit
  zero; patched DVCC not in control.

Operator commands (type, then Enter)
  <Enter>  next step            d  previous step        z  zero and hold
  s 12.5   go to 12.5 A (within the ceiling and --max)   c 11.8  log a clamp reading (A)
  v 4.2 3.8  log the drop across the output joints, plus and minus, in mV (resistance and heat computed)
  n text   log a note           f  fallback test: stop the driver and go silent (B3.1)
  q        zero and quit

Remote-driven session (operator at the hardware, commands typed by someone else over ssh): see
ramp-remote.sh (stdin from a FIFO, output to a file). Then --idle-timeout S zeroes the output when no
command arrived for S seconds (lost contact), and SIGTERM ends like Ctrl-C: 0 A written, acked, verified.

Standalone unit (--standalone): the unit is powered but OFF the DC bus (its voltage register reads
the open output, < 40 V) and the operator switch may be off. Only `defaults`, `g`, `n`, `z` and `q`
are accepted; the DC-connected, /Mode, per-unit limit, DVCC and stored-default checks are waived
because nothing can flow. Used to store (and later clear) the hand-over defaults of a unit that
goes to an engineer: `defaults 52.0 10 persist`, `q`; about 60 s after the last online write the
unit falls back to the stored values and its open output rises to the default voltage.

Log: /data/huawei/log/ramp-<date>-<serial>.log (everything printed plus the operator's entries).
Deployed to /data/huawei/huawei-ramp.py from src/deploy/huawei-ramp.py.
"""
import argparse
import os
import select
import signal
import subprocess
import sys
import time

sys.path.insert(0, '/data/huawei/src/driver')
from huawei import protocol as P                    # noqa: E402
from huawei import control as C                     # noqa: E402
from huawei.canbus import CanFrame, CanError        # noqa: E402

BENCH_MARKER = '/data/huawei/bench'
LOG_DIR = '/data/huawei/log'
SERVICE_DIR = '/service/huawei-charger'
DEFAULT_STEPS = '5,10,15,20,25,30,35,40,45,50'
DEFAULT_MIN_HOLD_S = 120.0
DEFAULT_MAX_A = 50.0
ABSOLUTE_MAX_A = 60.0           # = dbus_services.CCL_MAX_A, the most the driver's own setting allows
MAX_TEMP_C = 65.0
DC_CONNECTED_MIN_V = 40.0
DC_MATCH_V = 2.0
STATUS_STALE_S = 5.0
OTHER_UNIT_MAX_A = 1.0
OVERSHOOT_A = 2.0
OVERSHOOT_FRAC = 0.15
VOLTAGE_OVER_V = 1.0
ZERO_SETTLE_S = 20.0         # R4875G5 winds down over > 6 s (2026-11-03); the window stays open meanwhile
DOWN_SETTLE_S = 15.0        # the R4875G5 takes >6 s to follow a downward write; no overshoot verdict meanwhile
ACK_WAIT_S = 1.0
ACK_KEEP = 50               # acks remembered by the listener
MIN_DRIVER_VERSION_TAG = 'bench'      # informational only; the real check is /Huawei/BenchWindow being published
CHARGER_PREFIX = 'com.victronenergy.charger.huawei_'


# ------------------------------------------------------------------- pure logic
def _i(v):
    return '-' if v is None else str(int(v))


def _f(v, digits=1):
    return '-' if v is None else ('%.' + str(digits) + 'f') % float(v)


def ceiling(max_a, unit_ccl, dvcc_budget, bms_ccl, nominal_a, mode_on, dvcc_in_control):
    """The most the live driver would send right now, and the term that binds.

    Mirrors control.Governor.compute plus the patched DVCC's charger stage: the governor sends
    min(/Link/ChargeCurrent, /Settings/ChargeCurrentLimit) capped at nominal x 1.22, only with
    the operator switch on and DVCC in control; /Link/ChargeCurrent for a single active charger
    is min(budget, its limit), and the budget (/Dvcc/Chargers/Budget) already carries the BMS
    charge limit, the user's DVCC limit, the solar subtraction and the SOC ceiling. Unknown
    terms count as 0, never as unlimited."""
    terms = [('--max', max_a),
             ('unit /Settings/ChargeCurrentLimit', unit_ccl),
             ('DVCC /Dvcc/Chargers/Budget', dvcc_budget),
             ('BMS /Info/MaxChargeCurrent', bms_ccl),
             ('nominal x 1.22', nominal_a * P.CURRENT_RATIO_MAX if nominal_a else None)]
    if not mode_on:
        return 0.0, 'operator switch /Mode off'
    if not dvcc_in_control:
        return 0.0, 'patched DVCC not in control'
    best, name = None, None
    for n, v in terms:
        v = 0.0 if v is None else float(v)
        if best is None or v < best:
            best, name = v, n
    return max(0.0, round(best, 1)), name


def voltage_target(link_voltage, bms_cvl):
    """The charge voltage the live driver would send: DVCC's /Link/ChargeVoltage (the ESS hub
    voltage), never above the BMS CVL, clamped like control.Governor."""
    v = 52.4 if link_voltage is None else float(link_voltage)
    if bms_cvl is not None:
        v = min(v, float(bms_cvl))
    return max(C.VOLTAGE_MIN_V, min(C.VOLTAGE_MAX_V, v))


def ramp(sent, want, dt, rate=C.RAMP_UP_A_PER_S):
    """Governor rule: up at `rate` A/s at most, down at once."""
    if want <= sent:
        return want
    return min(want, sent + rate * max(0.0, dt))


def infer_reading(raw, measured_a, nominal_a, tol=0.15):
    """Which community reading explains the measured current for the raw value sent?
    Returns (name or None, {name: predicted A})."""
    pred = {}
    for n in P.READINGS:
        try:
            pred[n] = round(P.CURRENT_SCALES[n].decode(raw, nominal_a), 2)
        except ValueError:
            pass
    if measured_a is None or raw == 0:
        return None, pred
    best = None
    for n, a in pred.items():
        err = abs(a - measured_a)
        if err <= max(0.5, tol * a) and (best is None or err < best[1]):
            best = (n, err)
    return (best[0] if best else None), pred


def abort_reason(sent_a, status, status_age, v_target, max_temp, other_current, bms_v, bms_cvl):
    """First condition that must stop the output, or None. `status` is the unit's last complete
    burst snapshot (protocol.StatusBurst.snapshot), `other_current` the other unit's 0x81."""
    if status_age is None or status_age > STATUS_STALE_S:
        return 'no status from the unit for %s s (driver stopped?)' % ('?' if status_age is None else '%.0f' % status_age)
    i = status.get('out_current_a')
    if i is not None and sent_a is not None and i > sent_a + max(OVERSHOOT_A, OVERSHOOT_FRAC * sent_a):
        return 'overshoot: unit reports %.1f A for a %.1f A limit (wrong scale?)' % (i, sent_a)
    for reg in ('pfc_temp_c', 'out_temp_c', 'in_temp_c'):
        t = status.get(reg)
        if t is not None and t > max_temp:
            return '%s %.1f C above %.0f C' % (reg, t, max_temp)
    v = status.get('out_voltage_v') or status.get('v85')
    if v is not None and v > v_target + VOLTAGE_OVER_V:
        return 'unit voltage %.2f V above target %.2f V + %.0f V' % (v, v_target, VOLTAGE_OVER_V)
    if other_current is not None and other_current > OTHER_UNIT_MAX_A:
        return 'the OTHER unit reports %.1f A (write reached both?)' % other_current
    if bms_v is not None and bms_cvl is not None and bms_v > bms_cvl + 0.2:
        return 'battery %.2f V above its CVL %.2f V' % (bms_v, bms_cvl)
    return None


# ------------------------------------------------------------------ D-Bus side
class Venus(object):
    """Reads only. Every getter returns None when the service or path is missing."""

    def __init__(self):
        import dbus
        self.dbus = dbus
        self.bus = dbus.SystemBus()

    def get(self, service, path):
        try:
            obj = self.bus.get_object(service, path)
            v = obj.GetValue(dbus_interface='com.victronenergy.BusItem')
        except self.dbus.DBusException:
            return None
        if isinstance(v, self.dbus.Array) and len(v) == 0:
            return None
        try:
            return float(v) if isinstance(v, (self.dbus.Double, self.dbus.Int32, self.dbus.Int64,
                                               self.dbus.UInt32, self.dbus.Int16, self.dbus.UInt16, self.dbus.Byte)) else str(v)
        except (TypeError, ValueError):
            return None

    def names(self, prefix):
        return sorted(str(n) for n in self.bus.list_names() if str(n).startswith(prefix))

    def charger_service(self, unit):
        """--unit: a serial, or the number N of the display name 'Huawei N'."""
        svcs = self.names(CHARGER_PREFIX)
        unit = str(unit).strip()
        if len(unit) >= 4:                      # (the tail of) a serial
            hits = [s for s in svcs if s.split('huawei_')[-1].endswith(unit.upper())]
            if len(hits) == 1:
                return hits[0]
        else:                                   # the N of the display name "Huawei N"
            for s in svcs:
                if self.get(s, '/CustomName') == 'Huawei %s' % unit:
                    return s
        raise SystemExit('no charger service for --unit %r (have: %s)' % (unit, ', '.join(svcs) or 'none'))

    def battery_service(self):
        b = self.names('com.victronenergy.battery.')
        return b[0] if b else None


def _float_or_none(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def snapshot(venus, svc, other_svc, bat):
    g = venus.get
    d = {
        'addr': g(svc, '/Huawei/Address'), 'nominal': g(svc, '/Huawei/NominalCurrent'),
        'model': g(svc, '/Huawei/Model'), 'serial': g(svc, '/Serial'),
        'default_ok': g(svc, '/Huawei/DefaultLimitOk'), 'state': g(svc, '/Huawei/State'),
        'fault': g(svc, '/Huawei/Fault'), 'mode': g(svc, '/Mode'),
        'link_v': g(svc, '/Link/ChargeVoltage'), 'link_i': g(svc, '/Link/ChargeCurrent'),
        'cmd_age': g(svc, '/Huawei/CommandAgeSeconds'), 'bench': g(svc, '/Huawei/BenchWindow'),
        'dc_v': g(svc, '/Dc/0/Voltage'), 'dc_i': g(svc, '/Dc/0/Current'),
        'version': g(svc, '/Mgmt/ProcessVersion'), 'vsource': g(svc, '/Huawei/VoltageSource'),
        'other_addr': g(other_svc, '/Huawei/Address') if other_svc else None,
        # a limit written with the Venus dbus tool without the % prefix arrives as a string ('50'):
        # the driver accepts it (float()), the published item keeps the string until restart
        'ccl': _float_or_none(g(svc, '/Settings/ChargeCurrentLimit')),
        'budget': g('com.victronenergy.system', '/Dvcc/Chargers/Budget'),
        'soc_inhibit': g('com.victronenergy.system', '/Dvcc/Chargers/SocInhibit'),
        'dvcc_control': g('com.victronenergy.system', '/Control/ChargerChargeCurrent'),
        'bms_v': g(bat, '/Dc/0/Voltage') if bat else None, 'bms_i': g(bat, '/Dc/0/Current') if bat else None,
        'soc': g(bat, '/Soc') if bat else None, 'bms_ccl': g(bat, '/Info/MaxChargeCurrent') if bat else None,
        'bms_cvl': g(bat, '/Info/MaxChargeVoltage') if bat else None,
    }
    return d


# -------------------------------------------------------------------- CAN side
class Listener(object):
    """Decodes everything on the module bus; keeps the last complete burst per address, the
    unit's GET 0x85 and unsolicited frames, and the acks of our own writes."""

    def __init__(self, link):
        self.link = link
        self.bursts = {}        # addr -> StatusBurst in progress
        self.status = {}        # addr -> last complete snapshot
        self.status_t = {}      # addr -> wall time of it
        self.v85 = {}           # addr -> (value, t)
        self.unsol = {}         # addr -> (decode, t)
        self.acks = []          # (t, decode), the last ACK_KEEP of them; match by time, never by index
        self.get_replies = []   # (t, addr, decode, data bytes) of every 0x82 reply, last ACK_KEEP
        self.rx = 0

    def drain(self, wait=0.0):
        t_end = time.time() + wait
        while True:
            f = self.link.recv(0.05 if wait else 0.0)
            if f is None:
                if time.time() >= t_end:
                    return
                continue
            if f.err or f.echo:
                continue
            d = P.decode(f)
            if not d:
                continue
            self.rx += 1
            now = time.time()
            k, a = d.get('kind'), d.get('addr')
            if k == 'status':
                b = self.bursts.get(a)
                if b is None or b.complete:
                    b = self.bursts[a] = P.StatusBurst(a)
                b.feed(d)
                if b.complete:
                    snap = b.snapshot()
                    v85 = self.v85.get(a)
                    if v85 and now - v85[1] < STATUS_STALE_S:
                        snap['v85'] = v85[0]
                    self.status[a], self.status_t[a] = snap, now
            if k == 'get_reply':
                self.get_replies.append((now, a, d, bytes(f.data)))
                del self.get_replies[:-ACK_KEEP]
            if k == 'get_reply' and d.get('reg') == P.OUT_VOLTAGE_ALT_REG:
                self.v85[a] = (d.get('value'), now)
                if a in self.status:
                    self.status[a]['v85'] = d.get('value')
            elif k == 'unsolicited':
                self.unsol[a] = (d, now)
            elif k == 'ack':
                self.acks.append((now, d))
                del self.acks[:-ACK_KEEP]

    def acks_since(self, t0):
        """Acks received at or after t0. (2026-11-03: matching by list index failed after the
        20th ack of a run because the list is trimmed; round 1 on unit 2 stopped on a phantom
        'NO ACK' while the unit had acknowledged every write.)"""
        return [(t, d) for t, d in self.acks if t >= t0]

    def age(self, addr):
        t = self.status_t.get(addr)
        return None if t is None else time.time() - t


class Writer(object):
    def __init__(self, link, listener, addr, nominal, scale, dry):
        self.link, self.lst, self.addr, self.nominal, self.scale, self.dry = link, listener, addr, nominal, scale, dry
        self.writes = 0

    def write(self, name, value):
        """Send one set frame to the unit and wait for its ack. Returns (ok, raw, info)."""
        data, raw = P.encode_set_data(name, value, self.nominal, self.scale)
        if self.dry:
            return True, raw, 'dry-run, not sent'
        t0 = time.time()
        try:
            self.link.send(CanFrame(P.request_id(P.CMD_SET, self.addr), data, ext=True))
        except CanError as e:
            return False, raw, 'send failed: %s' % e
        self.writes += 1
        while time.time() - t0 < ACK_WAIT_S:
            self.lst.drain(0.05)
            for t, d in self.lst.acks_since(t0):
                if d.get('addr') == self.addr and d.get('name') == name:
                    return bool(d.get('ok')), raw, 'ack %s in %.0f ms (byte0 %s)' % (
                        'ok' if d.get('ok') else 'ERROR', (t - t0) * 1000, d.get('status_byte'))
        return False, raw, 'NO ACK within %.1f s' % ACK_WAIT_S


# -------------------------------------------------------------------- session
class Session(object):
    def __init__(self, args):
        self.args = args
        self.t0 = time.time()
        self.logf = None
        self.venus = Venus()
        self.svc = self.venus.charger_service(args.unit)
        others = [s for s in self.venus.names(CHARGER_PREFIX) if s != self.svc]
        self.other_svc = others[0] if others else None
        self.bat = self.venus.battery_service()
        self.link = None
        self.lst = None
        self.wr = None
        self.steps = [float(x) for x in args.steps.split(',') if x.strip()]
        self.step_i = -1            # -1 = 0 A
        self.step_t = time.time()   # when the present round began
        self.target = 0.0
        self.sent = None            # last acked on-line current, None = nothing written yet
        self.sent_raw = 0
        self.sent_t = None
        self.down_t = None          # wall time of the last downward write
        self.prev_sent = None       # the limit before that write
        self.v_sent = None
        self.v_sent_t = None
        self.v_override = None      # u VOLTS: on-line voltage to send instead of the DVCC/CVL target
        self.last_tick = time.time()
        self.marker_t = 0.0
        self.live = None
        self.ceil = (0.0, 'not evaluated')
        self.stop_reason = None
        self.clamp = None           # (A, t) last clamp reading typed by the operator
        self.last_cmd_t = time.time()

    # ------------------------------------------------------------- logging
    def log(self, line):
        stamp = time.strftime('%H:%M:%S') + ' %+7.1fs ' % (time.time() - self.t0)
        text = stamp + line
        print(text)
        sys.stdout.flush()
        if self.logf:
            self.logf.write(text + '\n')
            self.logf.flush()

    # ---------------------------------------------------------- precondition
    def check_preconditions(self):
        a = self.args
        L = self.live = snapshot(self.venus, self.svc, self.other_svc, self.bat)
        self.log('unit %s (%s, nominal %s A) at address %s; other unit at address %s; battery service %s' % (
            L['serial'], L['model'], L['nominal'], _i(L['addr']), _i(L['other_addr']), self.bat))
        problems = []
        if L['version'] is None:
            problems.append('driver service not on D-Bus (svstat /service/huawei-charger)')
        elif L['bench'] is None:
            problems.append('driver %s publishes no /Huawei/BenchWindow; deploy 0.7-bench or newer and restart it' % L['version'])
        if L['addr'] is None:
            problems.append('unit has no bus address (not identified)')
        if L['nominal'] is None:
            problems.append('nominal current unknown')
        if L['default_ok'] != 1 and not a.standalone:
            problems.append('stored default limit not seen as zero (DefaultLimitOk=%s): commission first' % L['default_ok'])
        if L['fault']:
            problems.append('unit faulted: %s' % L['fault'])
        if a.standalone:
            # off the DC bus: the output must read OPEN, otherwise this is not a standalone unit
            if L['dc_v'] is not None and L['dc_v'] >= DC_CONNECTED_MIN_V:
                problems.append('--standalone but the unit voltage register reads %.2f V (>= %.0f V): DC side connected' % (
                    L['dc_v'], DC_CONNECTED_MIN_V))
            self.log('STANDALONE: unit off the DC bus (voltage register %s V, DefaultLimitOk=%s, /Mode=%s); '
                     'only defaults/g/n/z/q accepted, no current will be commanded' % (_f(L['dc_v'], 2), L['default_ok'], L['mode']))
        else:
            if L['dc_v'] is None or L['dc_v'] < DC_CONNECTED_MIN_V:
                problems.append('DC side not connected: unit voltage register reads %s V (< %.0f V)' % (L['dc_v'], DC_CONNECTED_MIN_V))
            elif L['bms_v'] is not None and abs(L['dc_v'] - L['bms_v']) > DC_MATCH_V:
                problems.append('unit voltage %.2f V differs from battery %.2f V by more than %.0f V' % (L['dc_v'], L['bms_v'], DC_MATCH_V))
            if L['mode'] != 1:
                problems.append('operator switch off (/Mode=%s): dbus -y %s /Mode SetValue 1' % (L['mode'], self.svc))
            if not L['ccl']:
                problems.append('per-unit limit is 0: dbus -y %s /Settings/ChargeCurrentLimit SetValue %.0f' % (self.svc, a.max))
            if L['dvcc_control'] != 1:
                problems.append('patched DVCC not in control (/Control/ChargerChargeCurrent=%s)' % L['dvcc_control'])
            if L['bms_ccl'] is None:
                problems.append('no BMS charge current limit on D-Bus')
        if a.max > ABSOLUTE_MAX_A:
            problems.append('--max %.0f above the absolute %.0f A of this tool' % (a.max, ABSOLUTE_MAX_A))
        if any(s > a.max for s in self.steps) or self.steps != sorted(self.steps) or any(s <= 0 for s in self.steps):
            problems.append('--steps must be ascending, positive and <= --max')
        c = self.ceil = ceiling(a.max, L['ccl'], L['budget'], L['bms_ccl'], L['nominal'], L['mode'] == 1, L['dvcc_control'] == 1)
        self.log('ceiling now %.1f A (binding: %s); DVCC budget %s A, SOC %s %% (SocInhibit %s), BMS CCL %s A, '
                 'unit limit %s A, voltage target %.2f V (link %s V, BMS CVL %s V)' % (
                     c[0], c[1], _f(L['budget']), _f(L['soc'], 0), _i(L['soc_inhibit']), _f(L['bms_ccl']), _f(L['ccl']),
                     voltage_target(L['link_v'], L['bms_cvl']), _f(L['link_v'], 2), _f(L['bms_cvl'], 2)))
        if c[0] <= 0:
            self.log('NOTE: the live driver would send 0 A right now (%s); the output stays off until that changes' % c[1])
        for p in problems:
            self.log('PRECONDITION: ' + p)
        if problems and not a.dry_run:
            raise SystemExit('refusing to write (%d precondition(s) failed)' % len(problems))
        if problems:
            self.log('dry-run: continuing read-only despite the above')

    # ---------------------------------------------------------------- marker
    def touch_marker(self):
        if self.args.dry_run or time.time() - self.marker_t < 5.0:
            return
        with open(BENCH_MARKER, 'a'):
            pass
        os.utime(BENCH_MARKER, None)
        self.marker_t = time.time()

    def remove_marker(self):
        try:
            os.remove(BENCH_MARKER)
        except OSError:
            pass

    # ------------------------------------------------------------------ io
    def read_command(self):
        r, _, _ = select.select([sys.stdin], [], [], 0)
        if not r:
            return None
        line = sys.stdin.readline()
        if line == '':
            return 'q'          # stdin closed
        return line.strip()

    def send_limit(self, amps, why):
        v = voltage_target(self.live['link_v'], self.live['bms_cvl'])
        if self.v_override is not None:
            v = self.v_override
        now = time.time()
        if self.v_sent != v or self.v_sent_t is None or now - self.v_sent_t >= C.RESEND_S:
            ok, raw, info = self.wr.write('online_voltage', v)
            self.log('>> voltage %.2f V raw %d: %s' % (v, raw, info))
            if not ok:
                return self.stop('voltage write failed (%s)' % info)
            self.v_sent, self.v_sent_t = v, now
        ok, raw, info = self.wr.write('online_current', amps)
        self.log('>> current %.1f A raw %d (%s): %s' % (amps, raw, why, info))
        if not ok:
            self.sent_t = now
            return self.stop('current write failed (%s)' % info)
        if self.sent is not None and amps < self.sent:
            self.down_t = now
            self.prev_sent = self.sent
        self.sent, self.sent_raw, self.sent_t = amps, raw, now
        return True

    def stop(self, reason):
        if self.stop_reason is None:
            self.stop_reason = reason
            self.log('STOP: ' + reason)
        self.target = 0.0
        return False

    # ----------------------------------------------------------------- loop
    def status_line(self):
        L, a = self.live, self.live['addr']
        st = self.lst.status.get(a, {})
        other = self.lst.status.get(L['other_addr'], {}) if L['other_addr'] is not None else {}
        v = st.get('out_voltage_v') or st.get('v85')
        i = st.get('out_current_a')
        reading, pred = infer_reading(self.sent_raw, i, L['nominal'])
        return ('round %s (%.0f s) target %.1f sent %s A | unit %s A %s V 0x76 %s 0x77 %s | T pfc/out/in %s/%s/%s C | AC %s W %s V %s A | '
                'bat %s A %s V SOC %s%% | other %s A | ceil %.1f A (%s) | %s' % (
                    self.step_i + 1 if self.step_i >= 0 else '0', time.time() - self.step_t, self.target,
                    '-' if self.sent is None else '%.1f' % self.sent,
                    '-' if i is None else '%.1f' % i, '-' if v is None else '%.2f' % v, st.get('current_limit_ratio', '-'),
                    st.get('power_available_ratio', '-'),
                    st.get('pfc_temp_c', '-'), st.get('out_temp_c', '-'), st.get('in_temp_c', '-'),
                    st.get('ac_in_power_w', '-'), _f(st.get('ac_in_voltage_v'), 0), _f(st.get('ac_in_current_a'), 2),
                    _f(L['bms_i']), _f(L['bms_v'], 2), _f(L['soc'], 0),
                    '-' if other.get('out_current_a') is None else '%.1f' % other['out_current_a'],
                    self.ceil[0], self.ceil[1],
                    ('obeys %s' % reading) if reading else ('scale? %s' % pred if self.sent_raw else 'off')))

    def handle_command(self, cmd):
        if cmd is None:
            return
        self.log('operator: %r' % cmd)
        self.last_cmd_t = time.time()
        if self.args.standalone and not (cmd in ('q', 'z') or cmd.split(' ')[0] in ('defaults', 'g', 'n')):
            self.log('-- standalone unit: only defaults / g / n / z / q are accepted (no current is commanded off the DC bus)')
            return
        if cmd == '':
            held = time.time() - self.step_t
            if self.step_i >= 0 and held < self.args.min_hold:
                self.log('-- round %d (%.1f A) held only %.0f s of the minimum %.0f s: not released' % (
                    self.step_i + 1, self.target, held, self.args.min_hold))
            elif self.step_i + 1 < len(self.steps):
                self.step_i += 1
                self.target = self.steps[self.step_i]
                self.step_t = time.time()
                self.log('-- round %d: target %.1f A (hold at least %.0f s before the next)' % (
                    self.step_i + 1, self.target, self.args.min_hold))
            else:
                self.log('-- already at the last step (%.1f A); q to finish' % self.target)
        elif cmd == 'd':
            self.step_i = max(-1, self.step_i - 1)
            self.target = self.steps[self.step_i] if self.step_i >= 0 else 0.0
            self.log('-- down to %.1f A' % self.target)
        elif cmd == 'z':
            self.step_i = -1
            self.target = 0.0
            self.log('-- zero, holding')
        elif cmd == 'q':
            self.stop('operator quit')
        elif cmd.startswith('s '):
            try:
                a = float(cmd[2:])
            except ValueError:
                return self.log('?? s needs a number')
            if a < 0 or a > self.args.max:
                return self.log('?? %.1f A outside 0..%.1f' % (a, self.args.max))
            self.target = a
            self.step_i = max([k for k, s in enumerate(self.steps) if s <= a] or [-1])
            self.step_t = time.time()
            self.log('-- target %.1f A' % a)
        elif cmd.startswith('c '):
            try:
                clamp = float(cmd[2:])
            except ValueError:
                return self.log('?? c needs a number')
            st = self.lst.status.get(self.live['addr'], {})
            i = st.get('out_current_a')
            reading, pred = infer_reading(self.sent_raw, clamp, self.live['nominal'])
            self.clamp = (clamp, time.time())
            self.log('CLAMP %.1f A at limit %s A raw %d: unit 0x81 says %s A (diff %s); clamp fits reading %s of %s' % (
                clamp, self.sent, self.sent_raw, i, '-' if i is None else '%+.1f' % (i - clamp), reading, pred))
        elif cmd.startswith('v '):
            try:
                mv = [float(x) for x in cmd[2:].split()]
                assert 1 <= len(mv) <= 2
            except (ValueError, AssertionError):
                return self.log('?? v needs one or two numbers (mV across the plus joint, the minus joint)')
            if self.clamp and time.time() - self.clamp[1] < 60:
                amps, src = self.clamp[0], 'clamp'
            else:
                amps, src = self.lst.status.get(self.live['addr'], {}).get('out_current_a'), 'unit 0x81'
            parts = []
            for name, v in zip(('plus', 'minus'), mv):
                if amps:
                    parts.append('%s %.1f mV = %.2f mOhm, %.0f mW' % (name, v, v / amps, v * amps))
                else:
                    parts.append('%s %.1f mV (no current to divide by)' % (name, v))
            self.log('JOINT DROP at %s A (%s), limit %s A: %s' % ('-' if amps is None else '%.1f' % amps, src, self.sent, '; '.join(parts)))
        elif cmd.startswith('n '):
            self.log('NOTE ' + cmd[2:])
        elif cmd.startswith('u '):
            self.set_voltage_override(cmd[2:])
        elif cmd.startswith('defaults '):
            self.write_defaults(cmd[9:])
        elif cmd.startswith('g '):
            self.read_register(cmd[2:])
        elif cmd.startswith('a '):
            self.write_ac_limit(cmd[2:])
        elif cmd.startswith('fd '):
            self.write_fan_duty(cmd[3:])
        elif cmd.startswith('w '):
            self.write_raw(cmd[2:])
        elif cmd == 'f':
            self.fallback_test()
        else:
            self.log('?? unknown command %r (Enter, d, z, s A, u V|-, c A, v mV mV, n text, f, defaults V A persist, q)' % cmd)

    def tick(self):
        now = time.time()
        dt = now - self.last_tick
        self.last_tick = now
        self.touch_marker()
        self.lst.drain(0.0)
        self.live = snapshot(self.venus, self.svc, self.other_svc, self.bat)
        L = self.live
        self.ceil = ceiling(self.args.max, L['ccl'], L['budget'], L['bms_ccl'], L['nominal'], L['mode'] == 1, L['dvcc_control'] == 1)
        if L['fault']:
            self.stop('driver reports unit fault: %s' % L['fault'])
        if self.args.idle_timeout and now - self.last_cmd_t > self.args.idle_timeout and (self.sent or 0) > 0:
            self.stop('no operator command for %.0f s (idle timeout)' % (now - self.last_cmd_t))
        a = L['addr']
        st = self.lst.status.get(a)
        other = self.lst.status.get(L['other_addr']) if L['other_addr'] is not None else None
        settling = self.down_t is not None and now - self.down_t < DOWN_SETTLE_S
        if self.sent is not None:
            # while the unit follows a downward write its current is still above the new limit:
            # judge against the previous (higher) limit for DOWN_SETTLE_S (2026-11-03: a phantom
            # overshoot ended the unit 2 run 3 s after 'z', the unit was at 3.6 A on its way to 0)
            judged = max(self.sent, self.prev_sent or 0.0) if settling else self.sent
            reason = abort_reason(judged, st or {}, self.lst.age(a), voltage_target(L['link_v'], L['bms_cvl']),
                                  self.args.max_temp, other.get('out_current_a') if other else None, L['bms_v'], L['bms_cvl'])
            if reason:
                self.stop(reason)
        want = min(self.target, self.ceil[0])
        cur = 0.0 if self.sent is None else self.sent
        want = ramp(cur, want, dt)
        if self.sent is None or abs(want - self.sent) >= 0.05:
            why = 'down' if want < cur else ('ramp' if want < min(self.target, self.ceil[0]) else 'target')
            if want < self.target and self.ceil[0] < self.target and want <= self.ceil[0] + 0.05:
                why = 'capped by %s' % self.ceil[1]
            self.send_limit(want, why)
        elif self.sent_t is not None and now - self.sent_t >= C.RESEND_S:
            self.send_limit(want, 'keep-alive')
        self.log(self.status_line())

    def write_ac_limit(self, spec):
        """a AMPS [addr]: set register 0x09, the module's AC input current limit, ACTIVE at AMPS.
        Layout (community, PG/PGE/MJ): byte 3 = active flag, bytes 4-7 = A x 1024 (not a ratio).
        The value persists in the module. Never written with a low value: a module left with a
        low stored limit cannot start (PG). 2026-11-03: both units showed ~10 % power available
        at idle and unit 2 capped at 8.1 A DC = 2.0 A AC; one 16 A RCBO feeds each unit."""
        parts = spec.split()
        try:
            amps = float(parts[0])
            addr = int(parts[1]) if len(parts) > 1 else int(self.live['addr'])
        except (ValueError, IndexError, TypeError):
            self.log('usage: a AMPS [addr]')
            return
        if not 5.0 <= amps <= 20.0:
            self.log('AC limit %.1f A refused: allowed 5..20 A (a low stored value can brick the start-up)' % amps)
            return
        if self.args.dry_run:
            self.log('AC LIMIT %.1f A addr %d: dry-run, not sent' % (amps, addr))
            return
        raw = int(round(amps * 1024))
        data = bytes([0x01, 0x09, 0x00, 0x01]) + raw.to_bytes(4, 'big')
        t0 = time.time()
        try:
            self.lst.link.send(CanFrame(P.request_id(P.CMD_SET, addr), data, ext=True))
        except CanError as e:
            self.log('AC LIMIT %.1f A addr %d: send failed: %s' % (amps, addr, e))
            return
        while time.time() - t0 < ACK_WAIT_S:
            self.lst.drain(0.05)
            for t, d in self.lst.acks_since(t0):
                if d.get('addr') == addr and d.get('name') == 'ac_current_limit':
                    self.log('AC LIMIT %.1f A (raw %d, flag active) addr %d: data %s: ack %s in %.0f ms (byte0 %s)' % (
                        amps, raw, addr, data.hex(), 'ok' if d.get('ok') else 'ERROR', (t - t0) * 1000, d.get('status_byte')))
                    return
        self.log('AC LIMIT %.1f A addr %d: data %s: NO ACK within %.1f s' % (amps, addr, data.hex(), ACK_WAIT_S))

    def write_fan_duty(self, spec):
        """fd PERCENT [addr]: register 0x14, fan duty cycle (volatile; 0 = automatic). 2026-11-03:
        unit 2 capped at 8.1 A with fan feedback 4 (unit 1: 47) in register 0x87; the boards
        drive the fans, a module whose fan does not turn derates."""
        parts = spec.split()
        try:
            pct = float(parts[0])
            addr = int(parts[1]) if len(parts) > 1 else int(self.live['addr'])
        except (ValueError, IndexError, TypeError):
            self.log('usage: fd PERCENT [addr]')
            return
        if not 0.0 <= pct <= 100.0:
            self.log('fan duty %.0f %% refused' % pct)
            return
        if self.args.dry_run:
            self.log('FAN DUTY %.0f %% addr %d: dry-run, not sent' % (pct, addr))
            return
        data, raw = P.encode_set_data('fan_duty', pct)
        t0 = time.time()
        try:
            self.lst.link.send(CanFrame(P.request_id(P.CMD_SET, addr), data, ext=True))
        except CanError as e:
            self.log('FAN DUTY %.0f %% addr %d: send failed: %s' % (pct, addr, e))
            return
        while time.time() - t0 < ACK_WAIT_S:
            self.lst.drain(0.05)
            for t, d in self.lst.acks_since(t0):
                if d.get('addr') == addr and d.get('name') == 'fan_duty':
                    self.log('FAN DUTY %.0f %% (raw %d) addr %d: data %s: ack %s in %.0f ms (byte0 %s)' % (
                        pct, raw, addr, data.hex(), 'ok' if d.get('ok') else 'ERROR', (t - t0) * 1000, d.get('status_byte')))
                    return
        self.log('FAN DUTY %.0f %% addr %d: data %s: NO ACK within %.1f s' % (pct, addr, data.hex(), ACK_WAIT_S))

    def write_defaults(self, spec):
        """defaults VOLTS AMPS persist: write the NON-VOLATILE default voltage (0x01) and default
        current (0x04) of this tool's unit. With a non-zero default the unit delivers that current on
        its own after every power-up, without any controller: this is how a unit is handed to an
        engineer for a bench check outside the system (unit 1, 2026-11-04: a healthy unit shows the
        amps on a clamp right after boot, unit 1 shows ~1.65 A). `defaults 52.4 0 persist` puts the
        commissioning value back (B3.0); the driver refuses to command a unit whose stored default is
        not zero and this tool refuses unless the unit is at 0 A and the word `persist` is given."""
        parts = spec.split()
        if len(parts) != 3 or parts[2] != 'persist':
            return self.log('usage: defaults VOLTS AMPS persist   (non-volatile 0x01 / 0x04 of unit %s)' % self.live.get('addr'))
        try:
            volts, amps = float(parts[0]), float(parts[1])
        except ValueError:
            return self.log('?? defaults needs two numbers')
        if not P.voltage_in_range('default_voltage', volts):
            return self.log('?? default voltage %.2f V outside %s' % (volts, P.VOLTAGE_RANGES['default_voltage']))
        if not 0.0 <= amps <= float(self.live['nominal']):
            return self.log('?? default current %.1f A outside 0..%.1f (nominal)' % (amps, self.live['nominal']))
        if self.target or (self.sent or 0) > 0:
            return self.log('?? unit must be at 0 A first (z)')
        if self.args.dry_run:
            return self.log('DEFAULTS %.2f V / %.1f A: dry-run, not sent' % (volts, amps))
        ok, raw, info = self.wr.write('default_voltage', volts)
        self.log('DEFAULTS >> default voltage 0x01 %.2f V raw %d: %s' % (volts, raw, info))
        if not ok:
            return
        ok, raw, info = self.wr.write('default_current', amps)
        self.log('DEFAULTS >> default current 0x04 %.1f A raw %d (scale %s x %.1f A): %s' % (
            amps, raw, self.wr.scale, self.live['nominal'], info))
        if ok:
            self.log('DEFAULTS stored: the unit will deliver up to %.1f A at %.2f V on its own after every power-up '
                     '(no controller needed); verify: power cycle it with the driver stopped and read 0x76, or clamp it. '
                     'Put 0 A back (defaults %.2f 0 persist) before the unit rejoins the system.' % (amps, volts, volts))

    def write_raw(self, spec):
        """w HEX16 [addr]: one 0x80 set frame with the 8 data bytes given in hex, for layout
        experiments on registers 0x09 (AC limit), 0x14 (fan duty) and 0x34 (fan mode) only;
        the non-volatile registers 0x01/0x02/0x04 and the output registers are refused."""
        parts = spec.split()
        try:
            data = bytes.fromhex(parts[0])
            addr = int(parts[1]) if len(parts) > 1 else int(self.live['addr'])
        except (ValueError, IndexError, TypeError):
            self.log('usage: w HEX16 [addr]')
            return
        if len(data) != 8 or data[0] != 0x01 or data[1] not in (0x09, 0x14, 0x32, 0x34):
            self.log('raw write refused: 8 bytes, byte0 0x01, register 0x09/0x14/0x32/0x34 only')
            return
        if self.args.dry_run:
            self.log('RAW %s addr %d: dry-run, not sent' % (data.hex(), addr))
            return
        t0 = time.time()
        try:
            self.lst.link.send(CanFrame(P.request_id(P.CMD_SET, addr), data, ext=True))
        except CanError as e:
            self.log('RAW %s addr %d: send failed: %s' % (data.hex(), addr, e))
            return
        while time.time() - t0 < ACK_WAIT_S:
            self.lst.drain(0.05)
            for t, d in self.lst.acks_since(t0):
                if d.get('addr') == addr and d.get('reg') == data[1]:
                    self.log('RAW %s addr %d: ack %s in %.0f ms (byte0 %s, raw %s)' % (
                        data.hex(), addr, 'ok' if d.get('ok') else 'ERROR', (t - t0) * 1000, d.get('status_byte'), d.get('raw')))
                    return
        self.log('RAW %s addr %d: NO ACK within %.1f s' % (data.hex(), addr, ACK_WAIT_S))

    def set_voltage_override(self, arg):
        """u VOLTS: send this on-line voltage instead of the DVCC/CVL target (2026-11-03 unit 1 test:
        is the 1.7 A a voltage-mode limit?). Never above the BMS CVL. u - clears it."""
        arg = arg.strip()
        if arg == '-':
            self.v_override = None
            self.v_sent = None
            return self.log('-- voltage override cleared: back to %.2f V' % voltage_target(self.live['link_v'], self.live['bms_cvl']))
        try:
            v = float(arg)
        except ValueError:
            return self.log('?? u needs a voltage or -')
        hi = C.VOLTAGE_MAX_V if self.live['bms_cvl'] is None else min(C.VOLTAGE_MAX_V, float(self.live['bms_cvl']))
        if v < C.VOLTAGE_MIN_V or v > hi:
            return self.log('?? %.2f V outside %.1f..%.2f (BMS CVL)' % (v, C.VOLTAGE_MIN_V, hi))
        self.v_override = v
        self.v_sent = None
        self.log('-- voltage override %.2f V (sent with the next write)' % v)

    def read_register(self, spec):
        """g REGHEX [addr]: one 0x82 single-register read, answer printed raw (hex, byte 3, u32).
        Read-only on the bus; community notes say config registers may not answer."""
        parts = spec.split()
        try:
            reg = int(parts[0], 16)
            addr = int(parts[1]) if len(parts) > 1 else int(self.live['addr'])
        except (ValueError, IndexError, TypeError):
            self.log('usage: g REGHEX [addr]')
            return
        if self.args.dry_run:
            self.log('GET 0x%02X addr %d: dry-run, not sent' % (reg, addr))
            return
        t0 = time.time()
        try:
            self.lst.link.send(CanFrame(P.request_id(P.CMD_GET, addr), P.get_data(reg), ext=True))
        except CanError as e:
            self.log('GET 0x%02X addr %d: send failed: %s' % (reg, addr, e))
            return
        while time.time() - t0 < 1.0:
            self.lst.drain(0.05)
            for t, a, d, data in self.lst.get_replies:
                if t >= t0 and a == addr and d.get('reg') == reg:
                    raw = d.get('raw') or 0
                    self.log('GET 0x%02X addr %d: data %s | byte3 %d | u32 %d = %.3f (/1024) | %s%s' % (
                        reg, addr, data.hex(), data[3] if len(data) > 3 else -1, raw, raw / 1024.0,
                        d.get('name', '?'), (' = %s' % d['value']) if 'value' in d else ''))
                    return
        self.log('GET 0x%02X addr %d: no reply within 1 s' % (reg, addr))

    def zero_and_verify(self):
        """The last thing this process does on the bus."""
        if self.wr is None or self.args.dry_run:
            return
        for attempt in range(3):
            ok, raw, info = self.wr.write('online_current', 0.0)
            self.log('>> FINAL current 0 A raw %d: %s' % (raw, info))
            if ok:
                break
            time.sleep(0.5)
        self.sent, self.sent_raw = 0.0, 0
        t0 = time.time()
        a = self.live['addr'] if self.live else None
        while time.time() - t0 < ZERO_SETTLE_S:
            self.lst.drain(0.5)
            st = self.lst.status.get(a, {})
            i = st.get('out_current_a')
            if i is not None and i < 0.3 and self.lst.age(a) is not None and self.lst.age(a) < 2.0:
                self.log('verified: unit reports %.1f A, 0x76 %s, output stage %s' % (
                    i, st.get('current_limit_ratio'),
                    'on' if st.get('status_bits', 0) & P.OUTPUT_ON_BIT else 'off'))
                # 2026-11-03: the R4875G5 winds down over several seconds; the driver faulted
                # "uncommanded output 1.7 A while inhibited" when the marker vanished before its own
                # poll had seen the zero. Keep the window open until the driver has had time to see it.
                self.touch_marker()
                self.lst.drain(3.0)
                return
        st = self.lst.status.get(a, {})
        self.log('WARNING: could not verify zero current within %.0f s (last 0x81 %s A, status age %s)' % (
            ZERO_SETTLE_S, st.get('out_current_a'), self.lst.age(a)))

    def fallback_test(self):
        """B3.1: hold the present limit, stop the driver, go silent, watch the unit fall back."""
        S = self.args.silence
        self.log('FALLBACK TEST: the driver will be stopped (boards unfed, fans up) and this tool goes silent '
                 'for %d s from a %.1f A limit. Watch the clamp. Type "yes" + Enter within 20 s to proceed.' % (S, self.sent or 0))
        r, _, _ = select.select([sys.stdin], [], [], 20)
        if not r or sys.stdin.readline().strip() != 'yes':
            return self.log('fallback test not confirmed')
        if self.args.dry_run:
            return self.log('dry-run: fallback test skipped')
        subprocess.call(['svc', '-d', SERVICE_DIR])
        self.log('driver stopped; silence starts now (last write %.1f s ago)' % (time.time() - (self.sent_t or time.time())))
        t_silent = time.time()
        a = self.live['addr']
        seen_off = None
        try:
            while time.time() - t_silent < S:
                self.lst.drain(1.0)
                un = self.lst.unsol.get(a)
                u = un[0] if un and time.time() - un[1] < 3 else {}
                ratio = u.get('load_ratio')
                line = 'silent %4.0f s | unsolicited load_ratio %s (%s A if ratio1024) output_disabled %s' % (
                    time.time() - t_silent, ratio, u.get('load_a_ratio1024'), u.get('output_disabled'))
                if u.get('output_disabled') and seen_off is None:
                    seen_off = time.time() - t_silent
                    line += ' <- byte 3 set'
                self.log(line)
                cmd = self.read_command()
                if cmd and cmd.startswith('c '):
                    self.log('CLAMP during silence at %.0f s: %s A' % (time.time() - t_silent, cmd[2:]))
                elif cmd == 'q':
                    break
        finally:
            subprocess.call(['svc', '-u', SERVICE_DIR])
            self.log('driver started again (quiet start %.0f s before it polls); byte-3 set at %s s; '
                     'record the clamp readings against the silence clock' % (75, seen_off))
            self.stop('fallback test done')

    # ------------------------------------------------------------------ run
    def run(self):
        from huawei.canbus import SocketCanLink

        def on_term(signum, frame):
            raise KeyboardInterrupt('SIGTERM')
        signal.signal(signal.SIGTERM, on_term)       # from the first line on: every exit path ends in zero_and_verify
        os.makedirs(LOG_DIR, exist_ok=True)
        serial = self.svc.split('_')[-1]
        path = os.path.join(LOG_DIR, 'ramp-%s-%s.log' % (time.strftime('%Y%m%d-%H%M%S'), serial))
        self.logf = open(path, 'a')
        self.log('huawei-ramp on %s: unit %s, steps %s, max %.1f A, max temp %.0f C, scale %s%s' % (
            self.args.interface, self.args.unit, self.steps, self.args.max, self.args.max_temp, self.args.scale,
            ' DRY-RUN' if self.args.dry_run else ''))
        try:
            self.check_preconditions()
            self.link = SocketCanLink(self.args.interface)
            self.lst = Listener(self.link)
            self.wr = Writer(self.link, self.lst, int(self.live['addr']), self.live['nominal'], self.args.scale, self.args.dry_run)
            self.lst.drain(2.5)
            if self.lst.age(self.live['addr']) is None:
                raise SystemExit('no status burst from address %s in 2.5 s: the driver is not polling it' % self.live['addr'])
            self.log('listening: %d frames in 2.5 s, unit status age %.1f s' % (self.lst.rx, self.lst.age(self.live['addr'])))
            self.log('ready at 0 A. Enter = next step (%.1f A), q = quit. Every rise is limited to %.0f A/s and to the ceiling.' % (
                self.steps[0], C.RAMP_UP_A_PER_S))
            while self.stop_reason is None:
                self.tick()
                # wait up to 1 s for the operator, draining the bus meanwhile
                t_end = time.time() + 1.0
                while time.time() < t_end and self.stop_reason is None:
                    r, _, _ = select.select([sys.stdin, self.link], [], [], 0.1)
                    if sys.stdin in r:
                        self.handle_command(self.read_command())
                    if self.link in r:
                        self.lst.drain(0.0)
        except KeyboardInterrupt as e:
            self.stop('Ctrl-C' if not e.args else str(e.args[0]))
        except SystemExit as e:
            self.stop(str(e))
            raise
        except Exception as e:
            self.stop('exception %r' % (e,))
            raise
        finally:
            try:
                if self.wr is not None:
                    self.zero_and_verify()
            finally:
                self.remove_marker()
                self.log('end: %s; log %s' % (self.stop_reason, path))
                if self.link is not None:
                    self.link.close()
                self.logf.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0], formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__)
    ap.add_argument('--unit', required=True, help='serial, or N of the display name "Huawei N"')
    ap.add_argument('--steps', default=DEFAULT_STEPS, help='ascending current steps in A (default %(default)s)')
    ap.add_argument('--max', type=float, default=DEFAULT_MAX_A, help='never send more than this (default %(default)s A)')
    ap.add_argument('--min-hold', type=float, default=DEFAULT_MIN_HOLD_S, metavar='S',
                    help='a round must be held this long before the next can be released (default %(default)s s)')
    ap.add_argument('--max-temp', type=float, default=MAX_TEMP_C, help='stop above this unit temperature (default %(default)s C)')
    ap.add_argument('--scale', default='conservative', choices=sorted(P.CURRENT_SCALES),
                    help='current scale policy for the raw value (default %(default)s)')
    ap.add_argument('--interface', default='can0')
    ap.add_argument('--silence', type=int, default=120, help='seconds of silence in the fallback test (default %(default)s)')
    ap.add_argument('--dry-run', action='store_true', help='never send a set frame; reads, listens, evaluates')
    ap.add_argument('--standalone', action='store_true',
                    help='unit powered but off the DC bus: only defaults/g/n/z/q, DC and operator checks waived')
    ap.add_argument('--idle-timeout', type=float, default=0.0, metavar='S',
                    help='zero the output when no operator command arrived for S seconds (0 = off)')
    args = ap.parse_args(argv)
    Session(args).run()
    return 0


if __name__ == '__main__':
    sys.exit(main())
