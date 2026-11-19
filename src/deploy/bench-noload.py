#!/usr/bin/env python3
"""bench-noload: commissioning check of ONE Huawei rectifier whose OUTPUT IS NOT CONNECTED,
and (--load) the first small current test of a new unit on the battery.

Writes an on-line voltage (0x00) and a small on-line current limit (0x03) every 5 s, beside the
running driver (telemetry-only, it keeps polling), and reports per second what the unit answers:
acknowledgements, the open output voltage (0x75), the limit readback (0x76), current (0x81),
temperatures (0x7F/0x80), fan status (0x87) and the available-power ratio (0x77) if the unit sends
it. After --hold seconds (or on any ack error, measured current, or over-voltage) it writes 0 A,
checks the ack and leaves; the unit then falls back to its stored defaults within about a minute.

Nothing can flow without a load; what this proves is the command path (acks), the voltage control
loop (open output follows the setpoint) and the unit's self-reported fan/temperature state. The fan
derating question of the modified units needs a load test.

Usage (on the Venus Pi, bus of the gs_usb channel 2 = can1 there):
  python3 /data/huawei/bench-noload.py --interface can1 --addr 1 --volts 50.0 --amps 5 --hold 60

--load: unit ON the battery, setpoint above the bank voltage so current flows, e.g.
  python3 /data/huawei/bench-noload.py --interface can1 --addr 1 --load --volts 52.6 --amps 3 --hold 30
Refuses unless the battery service reports a charge current limit (CCL) >= --amps and a charge voltage
limit (CVL) above --volts; prints every second; writes 0 A and stops on: current above the limit + 30 %
(scale wrong), bank voltage within 0.05 V of the CVL or above --max-v, output or inlet temperature above
--max-temp, any ack error, no status from the unit for 5 s, BMS CCL falling below the sent limit. The
measured current against the raw value sent tells the current scale (ratio1024 x nominal expected).

Output stays off although everything is acked (standby flag, 0x83 bit 9, set and 0x32 writes ignored)?
The slot-detect pins (R4875G1 manual: pins 11 and 12, AC_D1/AC_D2) must be tied to OUTPUT-; open pins hold
the module in standby by hardware. Pins 9-10 bridged lift the R4875G1's limit from 53 A to 75 A (community).
"""
import argparse, os, subprocess, sys, time
sys.path.insert(0, '/data/huawei/src/driver')
from huawei import protocol as P                     # noqa: E402
from huawei.canbus import SocketCanLink, CanFrame    # noqa: E402

BENCH_MARKER = '/data/huawei/bench'                  # suspends the driver's "uncommanded output" fault
REGS = {0x75: 'V', 0x85: 'V85', 0x76: 'lim', 0x77: 'avail', 0x81: 'I', 0x7F: 'Tout', 0x80: 'Tin',
        0x87: 'fan', 0x78: 'ACV', 0x70: 'ACW', 0x7C: 'bus', 0x83: 'bits'}
GET_REGS = (0x87, 0x77, 0x83)                     # asked for explicitly each cycle (not all units send them)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--interface', default='can1')
    ap.add_argument('--addr', type=int, default=1)
    ap.add_argument('--volts', type=float, default=50.0)
    ap.add_argument('--amps', type=float, default=5.0)
    ap.add_argument('--nominal', type=float, default=52.0)
    ap.add_argument('--scale', default='ratio1024', choices=sorted(P.CURRENT_SCALES))
    ap.add_argument('--hold', type=float, default=60.0)
    ap.add_argument('--max-v', type=float, default=None, help='stop above this open voltage (default volts + 1.5)')
    ap.add_argument('--ac-limit', type=float, default=None, metavar='AMPS',
                    help='first store the AC input current limit 0x09 (ACTIVE, non-volatile, 5..20 A); a unit whose '
                         'stored limit is 0 keeps its output off. Commissioning step for every new unit (16 A here)')
    ap.add_argument('--load', action='store_true', help='unit on the battery: let current flow (see module doc)')
    ap.add_argument('--max-temp', type=float, default=65.0)
    ap.add_argument('--battery', default='com.victronenergy.battery.socketcan_can0', help='battery D-Bus service for CCL/CVL/SOC')
    ap.add_argument('--enable', default='none', choices=['none', 'standby0', 'standby1'],
                    help="output-enable policy: standby0 writes 0x32=0 to enable (1 to disable), standby1 the opposite")
    a = ap.parse_args()
    if not (41.0 <= a.volts <= 58.0):
        sys.exit('volts outside 41..58 V')
    if a.ac_limit is not None and not (5.0 <= a.ac_limit <= 20.0):
        sys.exit('AC limit outside 5..20 A (a low stored value can stop the unit from starting)')
    max_v = a.max_v if a.max_v is not None else a.volts + 1.5

    link = SocketCanLink(a.interface)
    last = {}
    acks = {'ok': 0, 'err': 0}
    log = open('/data/huawei/log/bench-noload-%s.log' % time.strftime('%Y%m%d-%H%M%S'), 'a')

    def out(s):
        line = time.strftime('%H:%M:%S') + ' ' + s
        print(line, flush=True)
        log.write(line + '\n'); log.flush()

    def send(name, value):
        data, raw = P.encode_set_data(name, value, a.nominal, a.scale)
        link.send(CanFrame(P.request_id(P.CMD_SET, a.addr), data, ext=True))
        return raw

    def pump(seconds):
        end = time.time() + seconds
        while time.time() < end:
            f = link.recv(0.05)
            if f is None:
                continue
            d = P.decode(f)
            if not d or d['addr'] != a.addr:
                continue
            if d['kind'] in ('status', 'get_reply') and d.get('reg') in REGS and 'value' in d:
                last[REGS[d['reg']]] = d['value']
                if d['kind'] == 'status':
                    last['_status_t'] = time.time()
                if d['reg'] == 0x87:
                    last['fan_raw'] = d['raw']
                if d['reg'] == 0x83:
                    last['flags'] = ','.join(d.get('flags') or []) or '-'
                    last['ac_present'] = d.get('ac_present')
            elif d['kind'] == 'ack':
                acks['ok' if d['ok'] else 'err'] += 1
                last['last_ack'] = '%s %s' % (d['name'], 'ok' if d['ok'] else 'ERROR')

    def get(reg):
        link.send(CanFrame(P.request_id(P.CMD_GET, a.addr), P.get_data(reg), ext=True))

    def enable(on):
        if a.enable == 'none':
            return None
        val = (0 if on else 1) if a.enable == 'standby0' else (1 if on else 0)
        return send('standby', val)

    def dbus_get(path):
        try:
            o = subprocess.run(['dbus', '-y', a.battery, path, 'GetValue'], capture_output=True, text=True, timeout=3).stdout
            return float(o.strip().split('=')[-1])
        except Exception:
            return None

    def battery():
        return {k: dbus_get(p) for k, p in (('soc', '/Soc'), ('v', '/Dc/0/Voltage'), ('i', '/Dc/0/Current'),
                                             ('ccl', '/Info/MaxChargeCurrent'), ('cvl', '/Info/MaxChargeVoltage'))}

    def touch():
        with open(BENCH_MARKER, 'a'):
            os.utime(BENCH_MARKER, None)

    def status_line(tag):
        return ('%s | out %.2f V  I %.2f A  lim %.3f  avail %s | T out/in %s/%s C | fan %s | flags %s | AC %s V %s W | acks ok %d err %d (%s)' % (
            tag, last.get('V', last.get('V85', 0.0)) or 0.0, last.get('I', 0.0) or 0.0, last.get('lim', 0.0) or 0.0,
            last.get('avail', '-'), last.get('Tout', '-'), last.get('Tin', '-'),
            last.get('fan_raw', '-'), last.get('flags', '-'), last.get('ACV', '-'), last.get('ACW', '-'), acks['ok'], acks['err'], last.get('last_ack', '-')))

    def write_ac_limit(amps):
        # same layout as huawei-ramp.py write_ac_limit: byte 3 = active flag, bytes 4-7 = A x 1024 (persists)
        raw = int(round(amps * 1024))
        data = bytes([0x01, 0x09, 0x00, 0x01]) + raw.to_bytes(4, 'big')
        before = dict(acks)
        link.send(CanFrame(P.request_id(P.CMD_SET, a.addr), data, ext=True))
        pump(1.5)
        out('AC LIMIT %.1f A (raw %d, active) addr %d: data %s: %s' % (
            amps, raw, a.addr, data.hex(), 'acked' if acks != before else 'NO ACK') + (' (%s)' % last.get('last_ack', '-')))
        if acks['err']:
            sys.exit('AC limit write returned an error ack; not continuing')

    out('bench-noload on %s, unit addr %d: %.2f V / %.1f A (%s, nominal %.0f A) for %.0f s, stop above %.1f V' % (
        a.interface, a.addr, a.volts, a.amps, a.scale, a.nominal, a.hold, max_v))
    for r in GET_REGS:
        get(r); pump(0.2)
    pump(3.0)
    out(status_line('before'))
    if (last.get('I') or 0) > 1.0:
        sys.exit('unit already delivers current: this is not an unloaded unit')
    bat = {}
    if a.load:
        bat = battery()
        out('battery: SOC %s %%, %s V, %s A, CCL %s A, CVL %s V' % tuple(bat[k] for k in ('soc', 'v', 'i', 'ccl', 'cvl')))
        if None in bat.values():
            sys.exit('battery service %s not readable; --load needs CCL/CVL' % a.battery)
        if bat['ccl'] < a.amps:
            sys.exit('BMS charge current limit %.1f A is below the %.1f A test limit: the battery does not want this charge now' % (bat['ccl'], a.amps))
        if bat['cvl'] <= a.volts + 0.05:
            sys.exit('setpoint %.2f V is not below the BMS charge voltage limit %.2f V' % (a.volts, bat['cvl']))
        if bat['v'] < 40.0 or abs((last.get('V') or 0) - bat['v']) > 2.0:
            sys.exit('unit voltage %.2f V vs battery %.2f V: the unit is not on this battery' % (last.get('V') or 0, bat['v']))
        if a.max_v is None:
            max_v = min(a.volts + 1.0, bat['cvl'] - 0.05)
        out('LOAD MODE: %.2f V / %.1f A for %.0f s; stops: I > %.1f A, V > %.2f V, T > %.0f C, ack error, CCL < limit, unit silent 5 s' % (
            a.volts, a.amps, a.hold, a.amps * 1.3 + 0.5, max_v, a.max_temp))
    if a.ac_limit is not None:
        write_ac_limit(a.ac_limit)
        pump(3.0)
        out(status_line('after AC limit'))

    reason = 'hold complete'
    t0 = time.time()
    try:
        while time.time() - t0 < a.hold:
            touch()
            if a.enable != 'none':
                enable(True); pump(0.1)
            rv = send('online_voltage', a.volts)
            pump(0.1)
            ri = send('online_current', a.amps)
            if a.load:
                stop = None
                for sec in range(5):
                    pump(0.6)
                    for r in GET_REGS:
                        get(r); pump(0.13)
                    bat = battery()
                    cur = last.get('I') or 0.0
                    scale = (cur / (ri / 1024.0 * a.nominal)) if ri else 0.0
                    out('+%3.0fs raw V %d I %d | unit %.2f A %.2f V (x%.2f of ratio1024) lim %.3f avail %s fan %s flags %s | T %s/%s C | AC %s V %s W | bat %s A %s V SOC %s%% CCL %s CVL %s | acks %d/%d' % (
                        time.time() - t0, rv, ri, cur, last.get('V') or 0, scale, last.get('lim') or 0, last.get('avail', '-'),
                        last.get('fan_raw', '-'), last.get('flags', '-'), last.get('Tout', '-'), last.get('Tin', '-'),
                        last.get('ACV', '-'), last.get('ACW', '-'), bat['i'], bat['v'], bat['soc'], bat['ccl'], bat['cvl'], acks['ok'], acks['err']))
                    if acks['err']: stop = 'ack error'
                    elif cur > a.amps * 1.3 + 0.5: stop = 'current %.2f A above the %.1f A limit: scale is not ratio1024 x nominal' % (cur, a.amps)
                    elif (last.get('V') or 0) > max_v or (bat['v'] or 0) > max_v: stop = 'voltage %.2f / %.2f V above %.2f V' % (last.get('V') or 0, bat['v'] or 0, max_v)
                    elif max(float(last.get('Tout') or 0), float(last.get('Tin') or 0)) > a.max_temp: stop = 'temperature above %.0f C' % a.max_temp
                    elif bat['ccl'] is not None and bat['ccl'] < a.amps: stop = 'BMS CCL fell to %.1f A' % bat['ccl']
                    elif time.time() - last.get('_status_t', time.time()) > 5: stop = 'no status from the unit for 5 s'
                    if stop: break
                if stop:
                    reason = stop; break
                continue
            pump(1.5)
            for r in GET_REGS:
                get(r); pump(0.2)
            pump(2.8)
            out(status_line('+%3.0fs sent V raw %d, I raw %d' % (time.time() - t0, rv, ri)))
            if acks['err']:
                reason = 'ack error'; break
            if (last.get('I') or 0) > 1.0:
                reason = 'unexpected current %.2f A' % last['I']; break
            v = last.get('V') or 0
            if v > max_v:
                reason = 'open voltage %.2f V above %.1f V' % (v, max_v); break
    except KeyboardInterrupt:
        reason = 'interrupted'
    finally:
        out('ending: %s. Writing 0 A' % reason)
        try:
            send('online_current', 0.0)
            pump(1.0)
            if a.enable != 'none':
                enable(False); pump(1.0)
            for r in GET_REGS:
                get(r); pump(0.2)
            pump(2.0)
            out(status_line('after 0 A%s' % (' + disable' if a.enable != 'none' else '')))
            if a.load:
                for _ in range(10):
                    if (last.get('I') or 0) < 1.0: break
                    pump(1.0)
                out('current after 0 A: %.2f A (%s)' % (last.get('I') or 0, 'fell below 1 A' if (last.get('I') or 0) < 1.0 else 'STILL FLOWING: check the unit'))
        finally:
            pump(1.0)
            link.close()
            log.close()


if __name__ == '__main__':
    main()
