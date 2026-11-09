"""Huawei R48xx (R4875G5) charger driver for Venus OS v2.92 (Cerbo GX).

Package layout (see README.md in src/driver):

    canbus.py         SocketCAN transport (no python-can), virtual bus, capture files
    protocol.py       Huawei R48xx codec, ported from the user's rsh harness; VERIFIED 2026-11-04
    modules.py        per-unit manager keyed by serial, address map, freshness, state machine
    control.py        DVCC watchdog, per-unit governor (clamp/ramp), commanders (output gate)
    dbus_services.py  one com.victronenergy.charger service per unit (+ optional dcsource mirror)
    engine.py         ties the above together without GLib (testable with a fake clock)
    display.py        display repeater: the chargers' control boards on their own interfaces
    main.py           process entry on the Cerbo: profile check, link up, GLib loop, services

Status 2026-11-01: telemetry against the real R4875G5 (0x85 voltage, e-label model, quiet
start, conservative current scale); control path complete and tested on the virtual bus
with the verification flag forced in the test fixture (ack timeout, overshoot by measurement,
zero-default check, fail-safe tick, keep-alive and shutdown, two-unit allocation, operator
paths).
Status 2026-11-04: protocol.VERIFIED is True after the load rounds of 2026-11-03 (unit 2 to
40 A, scale ratio1024 confirmed by clamp). The CanCommander is used only when the process is
started with --control (service run script: marker file /data/huawei/control-enabled); the
operator switch /Mode is persisted; serials in driver.json "excluded" are never written to.
"""
VERSION = '0.9.2-acpresence'
