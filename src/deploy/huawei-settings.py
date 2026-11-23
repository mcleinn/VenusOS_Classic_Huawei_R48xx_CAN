#!/usr/bin/env python3
"""Apply the installation's charger settings from driver.json ("settings" block) to Venus.

These four things are Venus *settings*, persisted on com.victronenergy.settings, not files: the
DVCC SOC ceiling for the managed chargers, the charger priority, the per-unit current limit and
the per-unit operator switch. A fresh installation starts with the safe defaults (ceiling 90/85,
priority 0, limit 0 A, switch off), so nothing charges until they are set. Until 2026-11-23 they
were set by hand after every installation (and forgotten on the Raspberry Pi: the DVCC extension
budgeted the unit, the tile appeared, and it delivered 0 A because its limit was still 0). Now the
values live in driver.json next to the serials and this script applies them, idempotently:

    python3 /data/huawei/huawei-settings.py                 # apply /data/huawei/driver.json
    python3 /data/huawei/huawei-settings.py --check         # only show current vs wanted
    python3 /data/huawei/huawei-settings.py --config x.json

driver.json block (all keys optional; serials upper-case, 12 hex digits):

    "settings": {
        "charger_max_soc": 100,          /Settings/Dvcc/ChargerMaxSoc    (100 = no SOC ceiling)
        "charger_resume_soc": 98,        /Settings/Dvcc/ChargerResumeSoc
        "charger_priority": 1,           /Settings/Dvcc/ChargerPriority  (0 chargers first, 1 Multi first)
        "charger_compensate_vebus": 0,   /Settings/Dvcc/ChargerCompensateVebus
        "charge_current_limit": {"AAAAAAAAAAAA": 50},   per unit, A
        "mode_on": ["AAAAAAAAAAAA"]      per unit operator switch: listed = on (1), others untouched
    }

Per-unit values go to the running charger service (com.victronenergy.charger.huawei_<serial>
/Settings/ChargeCurrentLimit and /Mode), which the driver persists; a unit that is not on the bus
yet is reported and skipped (run again once the driver shows it). The DVCC settings need the DVCC
extension (src/patch/<version>/) installed: without it the paths do not exist and are skipped.
The driver ignores the "settings" block itself. Uses the Venus `dbus` tool; BusyBox-safe.
"""
import json
import subprocess
import sys

SETTINGS = 'com.victronenergy.settings'
DVCC = [
    ('charger_max_soc', '/Settings/Dvcc/ChargerMaxSoc'),
    ('charger_resume_soc', '/Settings/Dvcc/ChargerResumeSoc'),
    ('charger_priority', '/Settings/Dvcc/ChargerPriority'),
    ('charger_compensate_vebus', '/Settings/Dvcc/ChargerCompensateVebus'),
]


def get(service, path):
    r = subprocess.run(['dbus', '-y', service, path, 'GetValue'], capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip() or 'Error' in r.stdout:
        return None
    v = r.stdout.strip()
    if v.startswith('value = '):
        v = v[8:]
    try:
        return float(v)
    except ValueError:
        return v


def put(service, path, value):
    # '%' makes the Venus dbus tool send a number instead of a string
    r = subprocess.run(['dbus', '-y', service, path, 'SetValue', '%%%s' % value], capture_output=True, text=True)
    # the Venus dbus tool prints the SetValue return code: 0 = accepted
    return r.returncode == 0 and r.stdout.strip() in ('0', 'retval = 0')


def services():
    r = subprocess.run(['dbus', '-y'], capture_output=True, text=True)
    return [s for s in r.stdout.split() if s.startswith('com.victronenergy.charger.huawei_')]


def same(a, b):
    try:
        return abs(float(a) - float(b)) < 1e-6
    except (TypeError, ValueError):
        return False


def apply(service, path, wanted, check, label):
    cur = get(service, path)
    if cur is None:
        print('%-46s path missing, skipped (DVCC extension / driver not up?)' % label)
        return False
    if same(cur, wanted):
        print('%-46s %s (already)' % (label, wanted))
        return True
    if check:
        print('%-46s %s -> %s (would set)' % (label, cur, wanted))
        return False
    ok = put(service, path, wanted)
    print('%-46s %s -> %s %s' % (label, cur, wanted, 'ok' if ok else 'FAILED'))
    return ok


def main(argv):
    check = '--check' in argv
    cfg_path = '/data/huawei/driver.json'
    if '--config' in argv:
        cfg_path = argv[argv.index('--config') + 1]
    with open(cfg_path) as fh:
        cfg = json.load(fh).get('settings') or {}
    if not cfg:
        print('no "settings" block in %s, nothing to do' % cfg_path)
        return 0
    rc = 0
    for key, path in DVCC:
        if key in cfg:
            rc |= 0 if apply(SETTINGS, path, cfg[key], check, path) else 1
    present = services()
    limits = {str(k).upper(): v for k, v in (cfg.get('charge_current_limit') or {}).items()}
    mode_on = [str(s).upper() for s in (cfg.get('mode_on') or [])]
    for serial in sorted(set(limits) | set(mode_on)):
        svc = 'com.victronenergy.charger.huawei_%s' % serial
        if svc not in present:
            print('%-46s not on the bus yet, skipped (run again later)' % svc)
            rc |= 1
            continue
        if serial in limits:
            rc |= 0 if apply(svc, '/Settings/ChargeCurrentLimit', limits[serial], check,
                             '%s /Settings/ChargeCurrentLimit' % serial) else 1
        if serial in mode_on:
            rc |= 0 if apply(svc, '/Mode', 1, check, '%s /Mode' % serial) else 1
    return rc


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
