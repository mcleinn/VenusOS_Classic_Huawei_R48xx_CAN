# Huawei R48xx rectifiers as DVCC-managed chargers on Venus OS (tested with a Cerbo GX)

A CAN driver and a small DVCC extension that let Huawei R4875 (R48xx family) telecom rectifiers
charge a Victron ESS battery under the control of a Victron GX device. The battery's BMS limits, the
user's charge-current limit and the ESS state decide how much current the rectifiers may deliver;
the driver turns that into on-line setpoints on the Huawei CAN bus, keeps the units alive and
stops them when anything goes stale. The chargers appear as `com.victronenergy.charger` services
in the Device List, the Remote Console and VRM.

Tested with a Cerbo GX. The driver uses only SocketCAN, D-Bus and the services every Venus OS
device has, so other GX devices with a VE.Can port (Venus GX, Ekrano GX, a Raspberry Pi with a CAN
hat) should run it too; expect to adapt the interface name and the service `run` script, and
re-check the DVCC extension against your `dbus-systemcalc-py` version.

What you need for the core setup: a GX device, one or two rectifiers wired to its VE.Can port, a
battery with DVCC in force. Nothing else. The two optional add-ons at the end (a repeater for the
rectifiers' control boards and an overview tile) are for installations that have those parts.

Built for and tested on one installation; everything here is tied to the Venus OS version and the
hardware listed below. Read the status section before relying on it.

## Tested with

| Component | Exact device / version |
| --- | --- |
| GX device | Victron Cerbo GX (original revision, SoC CAN controller `1c2bc00.can` on the VE.Can port) |
| Venus OS | **v2.92** with the classic (Qt4/QML) GUI; `dbus-systemcalc-py` **2.103-1** is the only version the DVCC extension installs on (hash-pinned) |
| Rectifier 1 | Huawei **R4875G5**, 69 A nominal (output voltage read from register 0x85) |
| Rectifier 2 | Huawei **R4875G1**, 75 A nominal (output voltage in 0x75, on-line limit echoed in 0x76) |
| Battery | Pylontech lithium bank with its CAN BMS on the Cerbo's BMS-Can port (DVCC on, BMS charge limits in force) |
| Inverter/charger | Victron Quattro in an ESS; the rectifiers' DC output goes straight to the battery bus |
| AC source for the rectifiers | Hyundai HY8500LEK petrol generator, 8 kW, through a 16 A RCBO (2.6 kW drawn at 50 A) |
| Huawei bus | Both rectifiers on the Cerbo's VE.Can port (its two RJ45 sockets are one interface, `can0`), 125 kbit/s, 29-bit identifiers, 120 Ω terminators at the rectifier ends, nothing at the Cerbo |
| Development machine | Windows 11, Python 3.12 for the test suites; the Cerbo runs Python 3.8 without `python-can` |
| Optional add-on hardware | The ESP32-based control/display boards fitted to the two rectifiers, fed through a CANable 2.5 two-channel USB CAN adapter (`gs_usb`, `can2`/`can3`). Not needed for the driver |

The project folder is still named `R4850G2`, the model assumed before the e-labels were read. The
protocol is the same family; every register and scale the driver relies on was measured on the two
units above. Other R48xx models should work with the `conservative` current scale until a load test
confirms theirs.

## Status (2026-11-05)

Verified on hardware:

- The R4875G1 charges the bank at the full 50 A per-unit limit through the driver under the patched
  DVCC: setpoints acknowledged in 4 to 34 ms, current confirmed by a clamp meter (2026-11-03) and by
  the Pylontech BMS current (2026-11-04), 0 to 50 A ramp at 5 A/s with 1 V of generator sag at most.
- The R4875G5: identification, telemetry, voltage control, acknowledgements and the persistent
  zero-current default with its fallback, verified on the bench and from the Cerbo.
- Persistent default of 0 A on both units, so a unit that loses its controller stops charging within
  about 75 s (measured) instead of charging on with a stale limit.
- Operator switch (`/Mode`), per-unit limit, DVCC heartbeat loss, charger AC off and on while the
  driver runs, driver restart, Cerbo reboot in telemetry mode.
- 125 driver tests on a simulated bus and 33 DVCC-extension tests in Victron's own test harness.

Not yet verified on hardware:

- A unit that loses its AC while its DC side stays on the bank keeps its CAN alive and is still
  reported as available to the DVCC. Harmless with one unit, with two it would hold half the budget.
  The fix (AC presence from the AC input voltage register 0x78) is designed, not built.
- Two units charging together, a cold start of a unit while the driver runs, a Cerbo reboot in
  control mode, the fallback test under load.

## How it works

```
                 BMS limits, ESS state, user limit
                            |
   dbus-systemcalc-py  +----v---------------------+   /Link/ChargeCurrent, /Huawei/Heartbeat
   (patched DVCC)      | managedcharger.py budget |-----------------------------------------+
                       +--------------------------+                                         |
                                                                                            v
   /data/huawei         +-----------+   +-----------+   +--------------+   +----------------------+
   one process, one bus | canbus.py |<->| engine.py |<->| modules.py   |<->| dbus_services.py     |
                        | SocketCAN |   | poll/tick |   | per serial   |   | charger + dcsource   |
                        +-----+-----+   +-----+-----+   +--------------+   +----------------------+
                              |               |  control.py: DVCC watchdog, governor, commander
                        can0 125 kbit/s       |
                              |               |
                   +----------+----------+    |
                   | R4875G5  | R4875G1  |    |
                   +---------------------+    |
```

**Driver** (`src/driver/huawei/`): raw SocketCAN without `python-can`, a codec for the Huawei R48xx
protocol, one `Module` per serial with requested, sent, acknowledged and measured values kept apart,
a governor that clamps every request (DVCC budget, per-unit limit, 1.22 × nominal, BMS charge
voltage, ramp 5 A/s) and refuses with a reason, a commander that only writes to units that answered
a status poll and carry a zero stored default, keep-alive resend every 5 s, zero as the last frame on
exit, fail-safe exit on any exception. Addresses are relearned from the bus, identity is the serial.
Publishes one `com.victronenergy.charger.huawei_<serial>` service per unit plus an optional
`dcsource` mirror that VRM draws. Supervised by daemontools from `/data`, survives firmware updates.

**DVCC extension** (`src/patch/`): a version-pinned, whole-file replacement of `delegates/dvcc.py`
(14 hunks) and a new `delegates/managedcharger.py`. It splits a budget (BMS charge current limit,
user limit, solar, optional SOC ceiling) between the Huawei chargers and the Multi, writes
`/Link/ChargeCurrent` and a heartbeat counter to each charger every 3 s, counts chargers with
`/State != 0` as active and writes an explicit 0 to the rest. Installer with hash pinning, rollback
and a boot-time guard.

**Operator tools** (`src/deploy/`): `huawei-status.py` (one column per unit, read from D-Bus) and
`huawei-ramp.py` (operator-paced load tests and commissioning beside the running driver).

The design and the measured facts it rests on are written up in the documents listed at the end.

## Repository layout

| Path | Content |
| --- | --- |
| `src/driver/huawei/` | The driver package (`canbus`, `protocol`, `modules`, `control`, `engine`, `dbus_services`, `main`; `display` is the optional repeater) |
| `src/driver/tests/` | 125 tests with a synchronous PSU simulator and a control-board simulator on an in-process bus |
| `src/driver/README.md` | Dated engineering notes: what each unit taught the driver, every build since 0.7 |
| `src/deploy/` | What goes to `/data/huawei` on the Cerbo: service scripts, `boot.sh`, `driver.example.json`, the operator tools; plus the test installation's own `driver.json`, `run` and `huawei-usbcan.conf` (with the optional boards) |
| `src/patch/` | DVCC extension: `managedcharger.py`, `patched-dvcc.py`, the unified diff, `apply_hunks.py` (source of the hunks), `install.sh`, tests, the pristine upstream 2.103-1 tree (MIT) |
| `src/classic-gui/` | Optional overview tile: modified QML, diffs, the files as found on the device, installer |
| `docs/Design_Option1_DVCC_Patch.md` | Design and implementation record of the DVCC extension |

The dated evidence files, bench logs, CAN captures and project notes behind the status section are
kept outside the repository.

## Installation (core: driver and DVCC extension)

The steps below are what was done on the test installation, in order. Everything lives under
`/data/huawei` on the GX device, which firmware updates leave alone. You need root SSH access to
it. **Nothing below enables charging**; that is step 6 and stays off until you turn it on.

### 0. Before you start

- Venus OS v2.92. The DVCC installer checks the SHA-256 of `delegates/dvcc.py` and refuses any other
  version; on a different version port the hunks in `src/patch/apply_hunks.py` first.
- Wire the rectifiers to the VE.Can port (RJ45 pin 7 CAN-H, pin 8 CAN-L, pin 3 GND; on the Huawei
  edge connector the top/white pin was CAN-L and the bottom/black pin CAN-H on the tested units).
  Terminate at the rectifier ends, not at the Cerbo. Verify the pinout on your units first.
- Settings → Services → VE.Can port → **Disabled**. The driver refuses to start while a Victron
  process owns `can0` and configures the link itself (125 kbit/s, `restart-ms 100`).
- Each rectifier must hold a **persistent default of 0 A** (register 0x04). The driver never writes
  to a unit whose first status burst reports a non-zero stored default (`/Huawei/DefaultLimitOk` = 0).
  The commissioning command is in step 5.
- Keep a backup of the Cerbo's `/data` and note the installed version of `dbus-systemcalc-py`.

### 1. Copy the files

From the repository root on your PC (replace `cerbo` with your GX device's host or SSH alias):

```sh
ssh root@cerbo 'mkdir -p /data/huawei/src /data/huawei/log /data/huawei/patch /data/huawei/service/log'
scp -r src/driver root@cerbo:/data/huawei/src/
scp src/deploy/service/run-core root@cerbo:/data/huawei/service/run
scp src/deploy/service/log/run   root@cerbo:/data/huawei/service/log/run
scp src/deploy/driver.example.json root@cerbo:/data/huawei/driver.json
scp src/deploy/boot.sh src/deploy/huawei-status.py src/deploy/huawei-ramp.py src/deploy/ramp-remote.sh root@cerbo:/data/huawei/
scp src/patch/install.sh src/patch/patched-dvcc.py src/patch/managedcharger.py root@cerbo:/data/huawei/patch/
ssh root@cerbo 'chmod +x /data/huawei/boot.sh /data/huawei/service/run /data/huawei/service/log/run \
    /data/huawei/patch/install.sh /data/huawei/ramp-remote.sh'
```

`run-core` starts the driver on `can0` with nothing else. (The test installation runs the shipped
`run`, which additionally gates the USB adapter of the optional control boards; see the add-ons.)

### 2. Describe your units in `/data/huawei/driver.json`

Edit the copied template: replace the two placeholder serials (12 hex digits, printed by
`huawei-status.py` once the driver runs, or read from the unit's e-label).

- `scales`: current-scale policy per serial. `ratio1024` (raw = A / nominal × 1024) is confirmed by
  clamp meter on both tested models. Use `conservative` for an unknown model until a load test
  confirms the scale.
- `names`: the `/CustomName` shown in the Device List and VRM.
- `excluded`: serials that are polled and shown but never written to; empty for normal use.
- `output_enable`: leave `"none"`. On the tested models the first non-zero current limit switches
  the output stage on; the standby register does not.
- `display`: only with the optional control-board repeater (add-on A).

With one rectifier, change `--expected 2` to `1` in `/data/huawei/service/run`.

### 3. Install the DVCC extension

```sh
/data/huawei/patch/install.sh check      # must say: state: pristine
/data/huawei/patch/install.sh install    # backs up, replaces dvcc.py, adds managedcharger.py, restarts systemcalc, verifies
dbus -y com.victronenergy.system /Control/ChargerChargeCurrent GetValue   # 1 = extension running
```

`install.sh rollback` restores the pristine file. The boot guard (step 4) re-applies the extension
after a firmware update only if the new root file system carries the identical 2.103-1 file;
anything else leaves `/data/huawei/dvcc_unsupported` behind and the driver keeps `/State` 0, so
nothing charges on an unknown DVCC.

New settings on `com.victronenergy.settings`: `/Settings/Dvcc/ChargerMaxSoc` and
`/Settings/Dvcc/ChargerResumeSoc` (optional SOC ceiling for the Huawei budget; 100/98 on the test
installation means no ceiling, the BMS charge limit ends the charge) and
`/Settings/Dvcc/ChargerStopWhenInverting`.

### 4. Autostart

`boot.sh` runs the DVCC guard, links `/data/huawei/service` into `/service` so daemontools
supervises the driver (restart on crash; `svc -d|-u|-t /service/huawei-charger` for stop, start,
restart) and, only if `/data/huawei/huawei-usbcan.conf` exists (add-on A), installs that modprobe
file. Append it to `/data/rc.local` (create the file with `#!/bin/sh` and `chmod +x` if it does not
exist) and run it once:

```sh
echo '/data/huawei/boot.sh' >> /data/rc.local
/data/huawei/boot.sh
svstat /service/huawei-charger /service/huawei-charger/log
```

Logs go to `/data/huawei/log` (`tail -f /data/huawei/log/current | tai64nlocal`).

### 5. First run: telemetry only

Without the marker file of step 6 the driver polls, identifies and publishes the units but sends no
setpoint, whatever the DVCC asks. Watch:

```sh
python3 /data/huawei/huawei-status.py --watch 5
```

Each unit should show its serial, model, firmware, address, `DefaultLimitOk 1`, voltage, current and
temperatures, and appear in the Device List as "Huawei 1" / "Huawei 2". A unit with
`DefaultLimitOk 0` needs commissioning: with its DC side disconnected (open output, below 40 V) run
`python3 /data/huawei/huawei-ramp.py --standalone <serial>`, enter `defaults 52.4 0 persist`, then
let the unit fall back (driver stopped for about 100 s) and check that the readback is 0.

### 6. Enable control

Per unit, set the limit and the operator switch (the switch is also on the charger's page in the
Device List; `%` makes the Venus `dbus` tool send a number instead of a string):

```sh
dbus -y com.victronenergy.charger.huawei_<serial> /Settings/ChargeCurrentLimit SetValue %50
dbus -y com.victronenergy.charger.huawei_<serial> /Mode SetValue %1
touch /data/huawei/control-enabled
svc -t /service/huawei-charger
```

Within about 10 s the unit shows `/State` 252, receives its share of the budget on
`/Link/ChargeCurrent` and ramps at 5 A/s. `rm /data/huawei/control-enabled; svc -t
/service/huawei-charger` returns to telemetry only; `/Mode` 4 switches a unit off at once (zero
written within a tick). Both settings persist across restarts and reboots.

Do the first run as a load test rather than in production: `huawei-ramp.py` steps one unit through
20/40/50 A beside the driver, never above what the driver itself would allow at that moment, and
stops to 0 A on any anomaly.

## Optional add-ons

### A. Repeater for the rectifiers' control boards (`huawei/display.py`)

Only for rectifiers fitted with the add-on ESP32 control/display boards. Those boards are CAN
masters: left on the module bus they rewrite the defaults every 0.62 s and fight the driver, so the
test installation cuts them from the bus and feeds each board from the driver through its own USB
CAN channel. The repeater forwards each unit's frames to its board with the address rewritten,
answers the board's writes with the protocol acknowledgement and never holds the module link. A
plain rectifier without such a board needs none of this.

Hardware: a `gs_usb` adapter (CANable 2.5 two-channel on the test installation). Because the Cerbo
names CAN interfaces by probe order, the adapter must never take `can0`/`can1` from the built-in
ports: `src/deploy/huawei-usbcan.conf` blacklists `gs_usb` and the shipped `src/deploy/service/run`
loads it only after the built-in ports are in place, so the channels land on `can2`/`can3`. Their
Venus profiles must be Disabled.

```sh
scp src/deploy/service/run root@cerbo:/data/huawei/service/run     # replaces run-core
scp src/deploy/huawei-usbcan.conf root@cerbo:/data/huawei/
# add the "display" block to /data/huawei/driver.json (see src/deploy/driver.json), then
ssh root@cerbo '/data/huawei/boot.sh; svc -t /service/huawei-charger'
```

Details and the measurements behind it: `src/driver/README.md`.

### B. Overview tile on the classic GUI (`src/classic-gui/`)

A purple "AC Charger" box on the overview page of the classic GUI (Venus OS v2.x) while a Huawei
charger is active (`/Dvcc/Chargers/Active` > 0). The installer is hash-pinned to the test
installation's files (a pristine `HubData.qml` and an already-modified `OverviewHub.qml`), so on
another device it reports `unknown` or `pristine` and refuses. Apply the portable form instead,
`src/classic-gui/diffs/huawei-tile-all.diff`, to `/opt/victronenergy/gui/qml/`, then
`svc -t /service/gui`. The GUI lives in `/opt`, so re-apply after a firmware update. The driver and
the DVCC extension do not need it; without it the chargers still show in the Device List and VRM.

## Operation

| What | Where |
| --- | --- |
| Per-unit status, one column per charger | `python3 /data/huawei/huawei-status.py` (`--watch 5`, `--json`) |
| Driver log | `/data/huawei/log/current` (multilog, rotated) |
| Stop / start / restart the driver | `svc -d`, `svc -u`, `svc -t /service/huawei-charger` |
| Budget and allocation | `com.victronenergy.system /Dvcc/Chargers/Budget`, `/Count`, `/Active`, `/<n>/Power`; `/Control/ChargerChargeCurrent` |
| Per-charger service | `com.victronenergy.charger.huawei_<serial>`: `/State` (252 = under DVCC control, 0 = must not deliver), `/Dc/0/*`, `/Ac/In/L1/*`, `/Link/*`, `/Mode`, `/Settings/ChargeCurrentLimit`, `/Alarms/*`, `/Huawei/*` (limiting reason, sent/acked/measured limit, output on, default verdict, heartbeat, command age) |
| Why a unit is not charging | `/Huawei/LimitingReason` on its service, in plain words |
| Return to a stock Cerbo | `svc -d /service/huawei-charger`, remove the `boot.sh` line from `/data/rc.local`, `install.sh rollback`, GUI rollback if installed, VE.Can profile back as it was |

## Safety rules the code follows

- Missing or stale data never means unlimited charging. No DVCC heartbeat for 10 s, no budget,
  no acknowledgement within 1 s, a measured current above the limit, an uncommanded output or any
  exception in the main loop ends in a 0 A write and `/State` 0.
- A unit is written to only after it answered a status poll in this process, carries a zero stored
  default and is not excluded; the last frame on exit is always 0 A.
- Setpoints are clamped to the DVCC budget, the per-unit limit, 1.22 × nominal and the BMS charge
  voltage, and rise at 5 A/s.
- The Cerbo's own ESS logic is never touched: no change to the ESS mode, the minimum SOC or the
  battery's limits.

## Development and tests

```sh
cd src/driver && python -m pytest tests -q        # 125 tests, simulated units and boards, no CAN hardware
python src/patch/tests/run_tests.py               # 33 tests inside Victron's dbus-systemcalc-py harness
```

The driver tests run on Windows, Linux and the Cerbo (`python3 -m unittest discover -s tests -p
"test_*.py"`). The DVCC tests assemble a throw-away copy of the pinned upstream tree; on Windows the
stand-ins in `src/patch/tests/win-shim/` satisfy the `dbus`, `gi` and `fcntl` imports.

Deploying a driver change: copy the changed files under `/data/huawei/src/driver/huawei/`, bump
`VERSION` in `huawei/__init__.py`, `svc -t /service/huawei-charger`, and watch the log for
"inhibited -> ready". Deploying a DVCC change: update the hashes in `install.sh`, copy the three
files to `/data/huawei/patch/`, `install.sh install`.

## Documents

- `docs/Design_Option1_DVCC_Patch.md`: design and implementation record of the DVCC extension,
  including the D-Bus facts of Venus OS v2.92 it rests on.
- `src/driver/README.md`: the driver's engineering notes, what each unit taught it, build by build.
- `src/classic-gui/README.md`: the optional overview tile.

## Acknowledgements

- Protocol groundwork: [craigpeacock/Huawei_R4850G2_CAN](https://github.com/craigpeacock/Huawei_R4850G2_CAN)
  and [patagonaa/huawei-r48xx](https://github.com/patagonaa/huawei-r48xx).
- [victronenergy/dbus-systemcalc-py](https://github.com/victronenergy/dbus-systemcalc-py) (MIT), whose
  2.103-1 tree is kept pristine under `src/patch/upstream-2.103-1/` as the patch base and test harness.

## License

No licence has been chosen for this project's own files yet. The Victron code under
`src/patch/upstream-2.103-1/` keeps its MIT licence.
