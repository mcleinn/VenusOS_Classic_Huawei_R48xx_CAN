#!/usr/bin/env python3
"""bench-noload: commissioning check of ONE Huawei rectifier whose OUTPUT IS NOT CONNECTED.

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
"""
import argparse, os, sys, time
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
    ap.add_argument('--enable', default='none', choices=['none', 'standby0', 'standby1'],
                    help="output-enable policy: standby0 writes 0x32=0 to enable (1 to disable), standby1 the opposite")
    a = ap.parse_args()
    if not (41.0 <= a.volts <= 58.0):
        sys.exit('volts outside 41..58 V')
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

    def touch():
        with open(BENCH_MARKER, 'a'):
            os.utime(BENCH_MARKER, None)

    def status_line(tag):
        return ('%s | out %.2f V  I %.2f A  lim %.3f  avail %s | T out/in %s/%s C | fan %s | flags %s | AC %s V %s W | acks ok %d err %d (%s)' % (
            tag, last.get('V', last.get('V85', 0.0)) or 0.0, last.get('I', 0.0) or 0.0, last.get('lim', 0.0) or 0.0,
            last.get('avail', '-'), last.get('Tout', '-'), last.get('Tin', '-'),
            last.get('fan_raw', '-'), last.get('flags', '-'), last.get('ACV', '-'), last.get('ACW', '-'), acks['ok'], acks['err'], last.get('last_ack', '-')))

    out('bench-noload on %s, unit addr %d: %.2f V / %.1f A (%s, nominal %.0f A) for %.0f s, stop above %.1f V' % (
        a.interface, a.addr, a.volts, a.amps, a.scale, a.nominal, a.hold, max_v))
    for r in GET_REGS:
        get(r); pump(0.2)
    pump(3.0)
    out(status_line('before'))
    if (last.get('I') or 0) > 1.0:
        sys.exit('unit already delivers current: this is not an unloaded unit')

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
        finally:
            pump(1.0)
            link.close()
            log.close()


if __name__ == '__main__':
    main()
