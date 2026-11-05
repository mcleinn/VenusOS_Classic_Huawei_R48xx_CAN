#!/usr/bin/env python3
"""huawei-status: one column per Huawei rectifier the driver publishes on D-Bus.

Runs on the Cerbo (Python 3.8, python-dbus as shipped with Venus OS):
    python3 /data/huawei/huawei-status.py            # table
    python3 /data/huawei/huawei-status.py --json     # the same as JSON
    python3 /data/huawei/huawei-status.py --watch 5  # redraw every 5 s
From Windows:  ssh cerbo python3 /data/huawei/huawei-status.py

Reads only (GetValue on the root of every com.victronenergy.charger.huawei_* service).
Deployed to /data/huawei/huawei-status.py from src/deploy/huawei-status.py.
"""
import argparse
import json
import sys
import time

STATE_NAMES = {0: 'Off', 1: 'Low power', 2: 'Fault', 3: 'Bulk', 4: 'Absorption', 5: 'Float',
               6: 'Storage', 7: 'Equalize', 8: 'Passthru', 9: 'Inverting', 10: 'Assisting',
               11: 'Power supply', 252: 'External control'}
MODE_NAMES = {1: 'On', 4: 'Off'}


def fmt_num(v, unit='', digits=1):
    if v is None or v == [] or v == '':
        return '-'
    try:
        return ('%.' + str(digits) + 'f%s') % (float(v), unit)
    except (TypeError, ValueError):
        return str(v)


def yes_no(v):
    if v is None or v == []:
        return '-'
    return 'yes' if int(v) else 'no'


def plain(v):
    if v is None or v == []:
        return '-'
    return str(v)


# (row label, path, formatter)
ROWS = [
    ('Serial', 'Serial', plain),
    ('Model', 'Huawei/Model', plain),
    ('Product name', 'ProductName', plain),
    ('Rated current', 'Huawei/NominalCurrent', lambda v: fmt_num(v, ' A', 0)),
    ('Hardware version', 'HardwareVersion', plain),
    ('Firmware', 'FirmwareVersion', plain),
    ('Operating hours', 'Huawei/OperatingHours', lambda v: fmt_num(v, ' h', 0)),
    ('Bus address', 'Huawei/Address', plain),
    ('D-Bus instance', 'DeviceInstance', plain),
    ('Display name', 'CustomName', plain),
    ('Connected', 'Connected', yes_no),
    ('Driver state', 'Huawei/State', plain),
    ('Victron state', 'State', lambda v: '-' if v is None else '%s (%s)' % (STATE_NAMES.get(int(v), '?'), v)),
    ('Operator switch', 'Mode', lambda v: '-' if v is None else '%s (%s)' % (MODE_NAMES.get(int(v), '?'), v)),
    ('Voltage register', 'Huawei/VoltageSource', plain),
    ('Current scale', 'Huawei/CurrentScale', plain),
    ('Stored default limit (ratio)', 'Huawei/DefaultLimitRatio', lambda v: fmt_num(v, '', 3)),
    ('Stored default is zero', 'Huawei/DefaultLimitOk', yes_no),
    ('Current limit ratio (0x76)', 'Huawei/LimitRatio', lambda v: fmt_num(v, '', 3)),
    ('Output stage on', 'Huawei/OutputOn', yes_no),
    ('Bench window open', 'Huawei/BenchWindow', yes_no),
    ('DC voltage', 'Dc/0/Voltage', lambda v: fmt_num(v, ' V', 2)),
    ('DC current', 'Dc/0/Current', lambda v: fmt_num(v, ' A', 1)),
    ('DC power', 'Dc/0/Power', lambda v: fmt_num(v, ' W', 0)),
    ('Temperature', 'Dc/0/Temperature', lambda v: fmt_num(v, ' C', 1)),
    ('AC input voltage', 'Ac/In/L1/V', lambda v: fmt_num(v, ' V', 0)),
    ('AC input frequency', 'Ac/In/L1/F', lambda v: fmt_num(v, ' Hz', 1)),
    ('AC input power', 'Ac/In/L1/P', lambda v: fmt_num(v, ' W', 0)),
    ('DVCC voltage request', 'Link/ChargeVoltage', lambda v: fmt_num(v, ' V', 1)),
    ('DVCC current request', 'Link/ChargeCurrent', lambda v: fmt_num(v, ' A', 1)),
    ('Requested current', 'Huawei/RequestedCurrent', lambda v: fmt_num(v, ' A', 1)),
    ('Sent current', 'Huawei/SentCurrent', lambda v: fmt_num(v, ' A', 1)),
    ('Measured limit', 'Huawei/MeasuredLimit', lambda v: fmt_num(v, ' A', 1)),
    ('Ack timeouts', 'Huawei/AckTimeouts', plain),
    ('Fault', 'Huawei/Fault', plain),
    ('Limiting reason', 'Huawei/LimitingReason', plain),
    ('Command age', 'Huawei/CommandAgeSeconds', lambda v: fmt_num(v, ' s', 1)),
    ('Board interface', 'Huawei/Display/Interface', plain),
    ('Board alive', 'Huawei/Display/Alive', yes_no),
    ('Frames to board', 'Huawei/Display/Forwarded', plain),
    ('Board writes absorbed', 'Huawei/Display/Absorbed', plain),
    ('Alarm: communication lost', 'Alarms/CommunicationLost', yes_no),
    ('Alarm: setpoint error', 'Alarms/SetpointError', yes_no),
    ('Alarm: uncommanded output', 'Alarms/UncommandedOutput', yes_no),
    ('Alarm: high temperature', 'Alarms/HighTemperature', yes_no),
    ('Alarm: high voltage', 'Alarms/HighVoltage', yes_no),
    ('Alarm: low voltage', 'Alarms/LowVoltage', yes_no),
    ('Driver version', 'Mgmt/ProcessVersion', plain),
]


def unwrap(v):
    """dbus types -> plain Python."""
    try:
        import dbus
    except ImportError:
        return v
    if isinstance(v, dbus.Dictionary):
        return dict((str(k), unwrap(x)) for k, x in v.items())
    if isinstance(v, dbus.Array):
        return [unwrap(x) for x in v]
    if isinstance(v, (dbus.String, dbus.ObjectPath)):
        return str(v)
    if isinstance(v, (dbus.Boolean,)):
        return bool(v)
    if isinstance(v, (dbus.Int16, dbus.Int32, dbus.Int64, dbus.UInt16, dbus.UInt32, dbus.UInt64, dbus.Byte)):
        return int(v)
    if isinstance(v, dbus.Double):
        return float(v)
    return v


def read_chargers():
    """{service name: {path: value}} for every huawei charger service on the system bus."""
    import dbus
    bus = dbus.SystemBus()
    names = [str(n) for n in bus.list_names() if str(n).startswith('com.victronenergy.charger.huawei_')]
    out = {}
    for name in sorted(names):
        try:
            obj = bus.get_object(name, '/')
            tree = unwrap(obj.GetValue(dbus_interface='com.victronenergy.BusItem'))
        except dbus.DBusException as e:
            tree = {'_error': str(e)}
        out[name] = tree if isinstance(tree, dict) else {'_error': 'no tree: %r' % (tree,)}
    return out


def wrap(text, width):
    words, lines, cur = str(text).split(), [], ''
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + ' ' + w).strip()
    if cur or not lines:
        lines.append(cur)
    return lines


def render(chargers, width=34):
    if not chargers:
        return 'no com.victronenergy.charger.huawei_* service on the bus (driver not running?)'
    cols = list(chargers.keys())
    heads = [c.split('huawei_')[-1] for c in cols]
    label_w = max(len(r[0]) for r in ROWS)
    line = '+' + '-' * (label_w + 2) + ('+' + '-' * (width + 2)) * len(cols) + '+'
    out = [line, '| ' + ''.ljust(label_w) + ' |' + ''.join(' ' + h.ljust(width) + ' |' for h in heads), line]
    for label, path, f in ROWS:
        cells = []
        for c in cols:
            tree = chargers[c]
            v = tree.get(path) if '_error' not in tree else tree['_error']
            cells.append(wrap(f(v) if '_error' not in tree else v, width))
        height = max(len(x) for x in cells)
        for i in range(height):
            out.append('| ' + (label if i == 0 else '').ljust(label_w) + ' |'
                       + ''.join(' ' + (x[i] if i < len(x) else '').ljust(width) + ' |' for x in cells))
    out.append(line)
    return '\n'.join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--json', action='store_true', help='print the raw trees as JSON')
    ap.add_argument('--watch', type=float, metavar='SECONDS', help='redraw every SECONDS')
    ap.add_argument('--width', type=int, default=34, help='column width (default 34)')
    args = ap.parse_args(argv)
    while True:
        chargers = read_chargers()
        if args.json:
            print(json.dumps(chargers, indent=1, sort_keys=True, default=str))
        else:
            if args.watch:
                sys.stdout.write('\033[2J\033[H')
            print(time.strftime('%Y-%m-%d %H:%M:%S %Z'))
            print(render(chargers, args.width))
        if not args.watch:
            return 0
        sys.stdout.flush()
        time.sleep(args.watch)


if __name__ == '__main__':
    sys.exit(main())
