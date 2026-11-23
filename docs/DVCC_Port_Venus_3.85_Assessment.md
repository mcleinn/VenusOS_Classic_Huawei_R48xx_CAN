# Assessment: porting the DVCC extension from Venus v2.92 to v3.85 (2026-11-19)

**Update 2026-11-23: ported and installed.** The faithful port described below is `src/patch/2.257-1/`
(`managedcharger.py` as a `BaseCharger`/`Networkable` subclass, 15 hunks in `apply_hunks.py`, hash-pinned `install.sh`).
33/33 managed-charger tests pass inside the 2.257-1 harness (upstream commit `4112bf9`, "v2.257", whose files hash-match
the Pi), the upstream suite shows the same single environment failure on the pristine and the patched tree. Installed on
the Pi 12:47 UTC: `/Control/ChargerChargeCurrent` 1, managed charger added, heartbeat running, driver after a restart
"Inhibited: operator switch off" only (SOC ceiling 90/85 at SOC 97 % gives budget 0). Effort: about three hours
including the harness, less than the estimate below because the stage is self-contained and the anchors moved but
kept their shape. Two deliberate deviations from 2.103-1: the Huawei budget subtracts the whole stock charger stage
(alternators and DC gensets included), and a Dynamic-ESS maximum charge power caps the Huawei budget too.

Original assessment (2026-11-19) follows.

Status then: **assessment only, nothing applied.** The Huawei driver itself runs on both firmwares (see the README
"Platforms" section). What it cannot do on v3.85 yet is *control*: the budget that tells it how many amps it may
deliver comes from the DVCC extension, which exists for `dbus-systemcalc-py` 2.103-1 (Venus v2.92) only.
Without it the driver stays in telemetry mode (`/Control/ChargerChargeCurrent` is missing, the DVCC watchdog never
sees control, `/State` stays 0).

## What was measured

Venus v3.85 on a Raspberry Pi 5: `/opt/victronenergy/dbus-systemcalc-py/delegates/dvcc.py`, 1,747 lines, sha256
`8c18880c240961fa5dfc5ea5a712aead24b7f3670161687db01dbf680fcbea64` (copy:
`backup-raspi5-venus3.85-2026-11-19/huawei/dvcc-port/pi-v3.85-dvcc.py` in the CerboGX project). The pinned base of the
extension is 2.103-1, 1,245 lines.

`patch --dry-run -p0 dvcc.py < huawei-dvcc-2.103-1.patch`: **10 of 13 hunks fail**; hunks 5, 6 and 10 apply only with
fuzz and offsets of 253-275 lines. The file has been restructured: charger handling moved into classes
(`BaseCharger`, `SolarCharger`, `Alternator`, `InverterCharger`, `DcGenset`) and a `ChargerSubsystem` that owns
`_set_charge_current` and `_distribute_current`; a separate `delegates/chargecontrol.py` arbitrates who controls
charging with hub4control. There is no class for `com.victronenergy.charger` services: stock DVCC never controls AC
chargers on v3.85 either, same as on v2.92.

## Size of a faithful port

Three parts:

1. `managedcharger.py` (285 lines): its `ManagedCharger` is modelled on the old `SolarCharger`; it should become a
   `BaseCharger` subclass of the new hierarchy (same `/Link/ChargeCurrent`, `/Huawei/Heartbeat`, `/State`
   rules), and its subsystem should plug into `ChargerSubsystem` rather than sit next to `Dvcc._on_timer`.
   Imports to re-check: `LowPassFilter`, `safeadd`, the lazy import of dvcc.
2. The 13 hunks in `dvcc.py`: 4 of them are plumbing (import, constructor, service discovery, new
   `/Control/ChargerChargeCurrent`, `/Dvcc/Chargers/*` paths, `/Settings/Dvcc/Charger*` settings), 5 are the
   budget split (BMS limit minus solar current minus charger current for the Multi), 4 are the control flags and
   the user-limit/ESS-state handling. On v3.85 those places moved into `ChargerSubsystem`, `_on_timer` and the
   new charge-control arbitration.
3. Verification: the 33 tests of `src/patch/tests/run_tests.py` run inside Victron's own test harness; the 3.85
   tree (it ships `CLAUDE.md`, `Makefile`, `patches/series`) would need the same harness copy, then a live test
   with a powered, commissioned unit on the bus.

Estimate: one to two working days including a live test, if the 3.85 DVCC internals behave as read. The installer
must become per-version (hash-pinned `dvcc.py` for 2.103-1 and for 3.85, anything else leaves the driver inhibited),
exactly as today with a second table row.

## A smaller approach (not recommended as is)

Publish each rectifier as a `com.victronenergy.solarcharger` instead of `charger`. Stock v3.85 DVCC would then write
`/Link/ChargeCurrent` and `/Link/ChargeVoltage` and subtract the charger's current from the Multi's limit with no
patch at all. Not recommended: the service would be counted as PV (statistics, VRM, "PV" tile), and DVCC's
solar-charger logic *maximises* solar current whenever ESS feeds into the grid, which would switch a grid-fed
charger on during exactly the wrong moments. It would need a careful design before any real use.

## Recommendation

Keep the driver in telemetry mode on the Pi (no setpoint is ever sent). If the Huawei chargers must charge on the Pi,
do the faithful port; until then the Quattro (which already carries ~66 A) does the charging on both platforms.
