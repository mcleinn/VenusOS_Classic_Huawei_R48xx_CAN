#!/usr/bin/env python3
"""huawei-opencircuit-test: ONE-OFF live test of a rectifier whose DC side is NOT connected to anything.

NOT part of the normal deployment (not copied by the install steps in the README). Written 2026-11-19 for the
the replacement unit (DMA-48-75) on the Raspberry Pi: does an ON-LINE voltage + ON-LINE current command switch the
output stage on, and which open-circuit voltage does the unit reach? Same codec, listener, writer and abort
logic as huawei-ramp.py (imported from the same directory), but no driver, no DVCC, no battery.

    python3 huawei-opencircuit-test.py --dry-run     # reads only, prints the plan, sends no set frame
    python3 huawei-opencircuit-test.py               # LIVE (driver must be stopped: svc -d /service/huawei-charger)

Sequence
  0. refuses when /data/huawei/control-enabled exists, when the driver service is up, when the Venus CAN profile
     of the interface is not Disabled, or when the unit is not seen;
  1. guard: --guard-s (8 s) of status reads; aborts when any output voltage register (0x75, 0x85) is above --guard-v (10 V:
     something is connected) or no frames from the unit arrive;
  2. for each voltage step (default 52.4 54.0 56.0 58.0 V, --hold s each): write online_voltage, then
     online_current (--amps, default 1.0 A, scale 'conservative' as in the ramp tool), check both acks, then
     resend both every --keepalive s and read 0x75/0x85/0x81/0x76/0x77/0x83 and the temperatures once a second;
  3. if the output never switched on (status bit 27 'output-on', or 0x85 > 20 V): standby register 0x32, one
     polarity at a time (0 then 1), --standby-hold (10) s each, logged; the register is put back to the value it had;
  4. ALWAYS (also after an abort, Ctrl-C, SIGTERM, any exception): online_current 0 A and online_voltage 52.4 V,
     acks checked, then up to 25 s of reads until the output is off.

Aborts (then step 4): NO ACK or an ack error, any output current above --max-current (the unit is unloaded, 0.5 A),
temperature above --max-temp, output voltage above the commanded value + 1 V, no status for 5 s.
Only the volatile registers online_voltage (0x00), online_current (0x03) and standby (0x32) can be written; the
non-volatile defaults (0x01 / 0x02 / 0x04) are refused in code.

Log: --log (default /tmp/oc-test/opencircuit-<time>.log).
"""
import argparse
import importlib.util
import os
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, '/data/huawei/src/driver')
sys.path.insert(1, os.path.join(HERE, '..', 'driver'))
from huawei import protocol as P                                    # noqa: E402
from huawei.canbus import SocketCanLink, LinkManager, CanFrame, CanError, resolve_interface   # noqa: E402

ALLOWED_WRITES = ('online_voltage', 'online_current', 'standby')    # volatile only
DEFAULT_STEPS = (52.4, 54.0, 56.0, 58.0)
SAFE_VOLTAGE = 52.4
GUARD_V = 10.0
MAX_CURRENT_A = 0.5
MAX_TEMP_C = 65.0
VOLTAGE_OVER_V = 1.0
STATUS_STALE_S = 5.0
SWITCHED_ON_V = 20.0
SERVICE = '/service/huawei-charger'
CONTROL_MARKER = '/data/huawei/control-enabled'
BIT_OUTPUT_ON = 1 << 27
BIT_STANDBY = 1 << 9


# ------------------------------------------------------------------ pure logic
def load_ramp():
    """huawei-ramp.py (dash in the name): Listener, Writer, abort_reason are reused unchanged."""
    path = os.path.join(HERE, 'huawei-ramp.py')
    spec = importlib.util.spec_from_file_location('huawei_ramp', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def out_voltages(snap):
    """(0x75, 0x85) output voltage readings of a status snapshot, None where missing."""
    return snap.get('out_voltage_v'), snap.get('v85', snap.get('out_voltage_alt_v'))


def connection_guard(snap, age, guard_v=GUARD_V):
    """Reason not to start, or None. Something connected to the DC side shows as a voltage far above the
    ~2.8 V residual of an open output stage; no frames at all means no unit to test."""
    if snap is None or age is None:
        return 'no status frames from the unit'
    for name, v in zip(('0x75', '0x85'), out_voltages(snap)):
        if v is not None and v > guard_v:
            return 'output voltage register %s reads %.2f V (> %.0f V): something is connected, refusing' % (name, v, guard_v)
    return None


def unloaded_abort_reason(ramp, v_cmd, i_cmd, snap, age, max_current=MAX_CURRENT_A, max_temp=MAX_TEMP_C):
    """Abort condition for the unloaded test, or None. The ramp tool's rules (overshoot, temperature, voltage
    above the commanded value + 1 V, stale status) plus: an unloaded unit must not deliver current."""
    r = ramp.abort_reason(i_cmd, snap or {}, age, v_cmd, max_temp, None, None, None)
    if r:
        return r
    cur = (snap or {}).get('out_current_a')
    if cur is not None and cur > max_current:
        return 'unexpected current %.2f A on an unloaded output (limit %.2f A)' % (cur, max_current)
    return None


def output_is_on(snap):
    bits = (snap or {}).get('registers', {}).get(0x83)
    on_bit = bool(bits is not None and bits & BIT_OUTPUT_ON)
    v85 = out_voltages(snap or {})[1]
    v75 = out_voltages(snap or {})[0]
    on_v = any(v is not None and v > SWITCHED_ON_V for v in (v75, v85))
    return on_bit or on_v


def output_is_off(snap):
    """True when the output stage is off AND every voltage register that is present reads below the guard
    voltage (0x85 is not always answered, then 0x75 alone decides; no reading at all is not 'off')."""
    if not snap or output_is_on(snap):
        return False
    vs = [v for v in out_voltages(snap) if v is not None]
    return bool(vs) and max(vs) < GUARD_V


def standby_bit(snap):
    bits = (snap or {}).get('registers', {}).get(0x83)
    return None if bits is None else bool(bits & BIT_STANDBY)


def check_write_allowed(name):
    if name not in ALLOWED_WRITES:
        raise SystemExit('refusing to write %r: only %s are allowed in this test' % (name, ', '.join(ALLOWED_WRITES)))


# --------------------------------------------------------------------- session
class Test(object):
    def __init__(self, args, ramp):
        self.a, self.ramp = args, ramp
        self.log_f = None
        self.link = None
        self.lst = None
        self.wr = None
        self.addr = args.addr
        self.max_v = 0.0
        self.max_v_step = None
        self.switched_on_at = None          # (step description, time)
        self.aborted = None
        self.acks = []                      # (what, ok, info)
        self.nominal = args.nominal
        self.standby_touched = False

    # logging ----------------------------------------------------------------
    def log(self, line):
        out = '%s %s' % (time.strftime('%H:%M:%S'), line)
        print(out, flush=True)
        if self.log_f:
            self.log_f.write(out + '\n')
            self.log_f.flush()

    # link -------------------------------------------------------------------
    def setup(self):
        a = self.a
        if os.path.exists(CONTROL_MARKER):
            raise SystemExit('%s exists: control must stay off, refusing' % CONTROL_MARKER)
        st = subprocess.run(['svstat', SERVICE], stdout=subprocess.PIPE, stderr=subprocess.STDOUT).stdout.decode()
        self.log('driver service: %s' % st.strip())
        if ': up ' in st and not a.dry_run:
            raise SystemExit('driver service is up: stop it first (svc -d %s); two masters must not share the bus' % SERVICE)
        ifname = resolve_interface(a.interface)
        prof = subprocess.run(['dbus', '-y', 'com.victronenergy.settings', '/Settings/Canbus/%s/Profile' % ifname,
                               'GetValue'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT).stdout.decode()
        digits = ''.join(ch for ch in prof.split('=')[-1] if ch.isdigit() or ch == '-')
        lm = LinkManager(ifname, P.BITRATE, profile_getter=lambda: int(digits) if digits else None)
        s = lm.ensure_up()
        self.log('interface %s (from %s): %s, bitrate %s, listen_only %s' % (ifname, a.interface, s['state'], s['bitrate'], s.get('listen_only')))
        self.link = SocketCanLink(ifname)
        self.lst = self.ramp.Listener(self.link)

    def poll(self, wait=None):
        """One status request + GET 0x85 to the unit, then listen (--poll-wait seconds)."""
        wait = self.a.poll_wait if wait is None else wait
        self.link.send(CanFrame(P.request_id(P.CMD_DATA, self.addr), P.status_request_data(), ext=True))
        self.link.send(CanFrame(P.request_id(P.CMD_GET, self.addr), P.get_data(P.OUT_VOLTAGE_ALT_REG), ext=True))
        self.lst.drain(wait)

    def snap(self):
        return self.lst.status.get(self.addr), self.lst.age(self.addr)

    def write(self, name, value):
        check_write_allowed(name)
        ok, raw, info = self.wr.write(name, value)
        what = '%s=%s (raw %s)' % (name, value, raw)
        self.acks.append((what, ok, info))
        self.log('  write %-28s %s' % (what, info))
        return ok

    def status_line(self, tag, v_cmd, i_cmd):
        snap, age = self.snap()
        if not snap:
            self.log('%s no status yet' % tag)
            return snap, age
        v75, v85 = out_voltages(snap)
        regs = snap.get('registers', {})
        bits = regs.get(0x83)
        self.log('%s cmd %.1f V/%.1f A | 0x75 %s V 0x85 %s V | I(0x81) %s A | 0x76 %s 0x77 %s | AC %s V | PFC bus %s V | '
                 'T pfc %s out %s in %s | 0x83 %s %s | age %s s' % (
                     tag, v_cmd, i_cmd, _f(v75, 2), _f(v85, 2), _f(snap.get('out_current_a'), 2),
                     _f(snap.get('current_limit_ratio'), 3), _f(snap.get('power_available_ratio'), 3),
                     _f(snap.get('ac_in_voltage_v'), 0), _f(snap.get('dc_bus_voltage_v'), 0),
                     _f(snap.get('pfc_temp_c'), 1), _f(snap.get('out_temp_c'), 1), _f(snap.get('in_temp_c'), 1),
                     '-' if bits is None else '0x%X' % bits, snap.get('flags'), _f(age, 1)))
        for v in (v75, v85):
            if v is not None and v > self.max_v:
                self.max_v, self.max_v_step = v, '%.1f V commanded' % v_cmd
        if output_is_on(snap) and self.switched_on_at is None:
            self.switched_on_at = ('%s (%.1f V / %.1f A commanded)' % (tag, v_cmd, i_cmd), time.time())
            self.log('  >>> OUTPUT SWITCHED ON <<< %s' % self.switched_on_at[0])
        return snap, age

    # phases -----------------------------------------------------------------
    def guard(self):
        t_end = time.time() + self.a.guard_s
        while time.time() < t_end:
            self.poll()
        snap, age = self.status_line('guard', 0.0, 0.0)
        if snap and 'rated_current_a' in snap and snap['rated_current_a']:
            self.nominal = float(snap['rated_current_a'])
        self.log('nominal current used for the current scale: %.1f A' % self.nominal)
        why = connection_guard(snap, age, self.a.guard_v)
        if why:
            raise SystemExit('GUARD: ' + why)
        self.log('guard passed: output registers %s, unit alive (age %.1f s)' % (out_voltages(snap), age))
        self.initial_standby = standby_bit(snap)
        self.log('initial status bits: %s (standby flag %s)' % (snap.get('flags'), self.initial_standby))
        self.wr = self.ramp.Writer(self.link, self.lst, self.addr, self.nominal, 'conservative', self.a.dry_run)

    def hold(self, tag, v_cmd, i_cmd, seconds, rewrite=True):
        """Keep-alive: resend voltage then current every --keepalive s, poll every second. Aborts return the reason."""
        t_end = time.time() + seconds
        last_write = time.time()
        while time.time() < t_end:
            self.poll()
            snap, age = self.status_line(tag, v_cmd, i_cmd)
            why = unloaded_abort_reason(self.ramp, v_cmd, i_cmd, snap, age, self.a.max_current, self.a.max_temp)
            if why and not self.a.dry_run:
                return why
            if rewrite and time.time() - last_write >= self.a.keepalive:
                ok1 = self.write('online_voltage', v_cmd)
                ok2 = self.write('online_current', i_cmd)
                last_write = time.time()
                if not (ok1 and ok2):
                    return 'ack problem on a keep-alive write'
        return None

    def apply(self, v, i):
        ok1 = self.write('online_voltage', v)
        ok2 = self.write('online_current', i)
        return None if (ok1 and ok2) else 'no/failed ack for the first write at %.1f V / %.1f A' % (v, i)

    def voltage_steps(self):
        for v in self.a.steps:
            tag = 'step %.1f V' % v
            self.log('--- %s, current limit %.1f A, hold %d s' % (tag, self.a.amps, self.a.hold))
            why = self.apply(v, self.a.amps) or self.hold(tag, v, self.a.amps, self.a.hold)
            if why:
                return why
        return None

    def standby_probe(self):
        base_v = self.a.steps[-1] if self.a.steps else SAFE_VOLTAGE
        for pol in (0, 1):
            self.log('--- standby probe: register 0x32 = %d, online %.1f V / %.1f A kept alive, %d s' % (pol, base_v, self.a.amps, self.a.standby_hold))
            self.standby_touched = True
            ok = self.write('standby', pol)
            if not ok:
                return 'standby write %d not acknowledged' % pol
            why = self.hold('standby=%d' % pol, base_v, self.a.amps, self.a.standby_hold)
            if why:
                return why
            if self.switched_on_at:
                self.log('  output on after standby=%d' % pol)
                return None
        return None

    def restore_standby(self):
        if self.initial_standby is None:
            return
        want = 1 if self.initial_standby else 0
        self.log('restoring standby register 0x32 to %d (initial flag %s)' % (want, self.initial_standby))
        self.write('standby', want)

    def safe_end(self):
        """Always: 0 A and 52.4 V, acks checked, then wait for the output to be off."""
        if self.wr is None or self.link is None:
            return
        self.log('=== ENDING: online current 0 A, online voltage %.1f V' % SAFE_VOLTAGE)
        ok_i = self.write('online_current', 0.0)
        ok_v = self.write('online_voltage', SAFE_VOLTAGE)
        if not (ok_i and ok_v):
            self.log('!!! end writes not (all) acknowledged, retrying once')
            ok_i = self.write('online_current', 0.0)
            ok_v = self.write('online_voltage', SAFE_VOLTAGE)
        self.end_acks_ok = bool(ok_i and ok_v)
        t_end = time.time() + 25.0
        last_write = time.time()
        self.final_snap = None
        while time.time() < t_end:
            self.poll()
            snap, age = self.status_line('end', SAFE_VOLTAGE, 0.0)
            self.final_snap = snap
            if output_is_off(snap):
                break
            if time.time() - last_write >= 5.0:         # keep the 0 A limit alive until the output is down
                self.write('online_current', 0.0)
                last_write = time.time()

    def run(self):
        a = self.a
        self.initial_standby = None
        self.end_acks_ok = None
        self.final_snap = None
        log_dir = os.path.dirname(a.log)
        if log_dir and not os.path.isdir(log_dir):
            os.makedirs(log_dir)
        self.log_f = open(a.log, 'a')
        self.log('huawei-opencircuit-test %s, addr %d, steps %s V, %.1f A, hold %d s, keepalive %.1f s' % (
            'DRY-RUN' if a.dry_run else 'LIVE', a.addr, list(a.steps), a.amps, a.hold, a.keepalive))
        try:
            self.setup()
            self.guard()
            for name, vv in (('online_voltage', a.steps[0]), ('online_current', a.amps)):
                self.log('plan: %s %s -> %s' % (name, vv, P.encode_set_data(name, vv, self.nominal, 'conservative')))
            self.aborted = self.voltage_steps()
            if not self.aborted and not self.switched_on_at and not a.no_standby_probe:
                self.aborted = self.standby_probe()
            if self.aborted:
                self.log('!!! ABORT: %s' % self.aborted)
        except SystemExit as e:
            self.aborted = 'refused/guard: %s' % e
            self.log('!!! %s' % self.aborted)
        except BaseException as e:                                  # noqa: BLE001 (Ctrl-C, SIGTERM, anything)
            self.aborted = 'exception %r' % (e,)
            self.log('!!! %s' % self.aborted)
        finally:
            try:
                if not a.dry_run:
                    if self.standby_touched:
                        self.restore_standby()
                    self.safe_end()
            finally:
                self.summary()
                if self.link:
                    self.link.close()
        return 0 if (self.aborted is None or a.dry_run) else 2

    def summary(self):
        fin = self.final_snap
        self.log('=== SUMMARY')
        self.log('  highest output voltage seen (0x75/0x85): %.2f V%s' % (self.max_v, ' at %s' % self.max_v_step if self.max_v_step else ''))
        self.log('  output switched on: %s' % (self.switched_on_at[0] if self.switched_on_at else 'NO'))
        self.log('  abort/refusal: %s' % (self.aborted or 'none'))
        bad = [x for x in self.acks if not x[1]]
        self.log('  writes: %d, not acknowledged / errors: %d' % (len(self.acks), len(bad)))
        if not self.a.dry_run:
            self.log('  end writes acknowledged: %s' % self.end_acks_ok)
            if fin:
                self.log('  final: 0x75 %s V, 0x85 %s V, I %s A, output on: %s, flags %s' % (
                    _f(out_voltages(fin)[0], 2), _f(out_voltages(fin)[1], 2), _f(fin.get('out_current_a'), 2),
                    output_is_on(fin), fin.get('flags')))


def _f(v, digits=1):
    return '-' if v is None else ('%.' + str(digits) + 'f') % float(v)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--interface', default='gs_usb:1')
    ap.add_argument('--addr', type=int, default=1)
    ap.add_argument('--steps', default=','.join('%g' % v for v in DEFAULT_STEPS), help='online voltages, comma separated')
    ap.add_argument('--hold', type=float, default=10.0, help='seconds per step')
    ap.add_argument('--amps', type=float, default=1.0, help='online current limit (A, scale conservative)')
    ap.add_argument('--keepalive', type=float, default=1.5)
    ap.add_argument('--standby-hold', type=float, default=10.0, help='seconds per standby polarity in the probe')
    ap.add_argument('--nominal', type=float, default=52.0, help='rated current if the unit does not report it')
    ap.add_argument('--guard-v', type=float, default=GUARD_V)
    ap.add_argument('--max-current', type=float, default=MAX_CURRENT_A)
    ap.add_argument('--max-temp', type=float, default=MAX_TEMP_C)
    ap.add_argument('--guard-s', type=float, default=8.0, help='seconds of status reads before anything is written')
    ap.add_argument('--poll-wait', type=float, default=1.0, help='seconds listened after each status request')
    ap.add_argument('--no-standby-probe', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--log', default='/tmp/oc-test/opencircuit-%s.log' % time.strftime('%Y%m%d-%H%M%S'))
    args = ap.parse_args(argv)
    args.steps = tuple(float(x) for x in args.steps.split(',') if x.strip())
    if not 0.0 < args.amps <= 2.0:
        raise SystemExit('--amps must be in (0, 2]: this is an unloaded-output test')
    for v in args.steps:
        if not P.voltage_in_range('online_voltage', v):
            raise SystemExit('step %.1f V is outside the protocol range for online_voltage' % v)
    t = Test(args, load_ramp())

    def on_term(signum, frame):
        raise KeyboardInterrupt('signal %d' % signum)
    signal.signal(signal.SIGTERM, on_term)
    return t.run()


if __name__ == '__main__':
    sys.exit(main())
