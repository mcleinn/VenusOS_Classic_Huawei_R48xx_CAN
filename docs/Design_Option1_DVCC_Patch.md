# Design record — Option 1: version-pinned DVCC extension for Huawei chargers

Status: implemented 2026-10-22 (see §11 and §12); installation on the Cerbo authorised by the user the same day. Based on the code installed on the target on 2026-10-22 (Venus OS v2.92, dbus-systemcalc-py 2.103-1). Evidence tags as in the Phase A report: **[V]** verified on device, **[D]** from installed source, **[P]** proposal.

## 1. Goal

Make the installed DVCC the single owner of the charging-current budget for the two Huawei rectifiers, so that:

- the Pylontech limits (CVL/CCL) and the user limit are honoured as a **total** across solar chargers, Huaweis and the Quattro;
- the Huaweis receive an allocated current and the effective charge-voltage target every control tick, like the Multi does today;
- the Multi's limit is reduced by what the Huaweis actually deliver, so there is no double-booking;
- the Huaweis are never "maximised" the way solar chargers are under ESS feed-in;
- loss of DVCC (process death, patch missing, stale writes) is detectable by the Huawei driver and results in inhibition.

## 2. Pinned baseline

The patch targets exactly these files. Any other content means "unsupported version": do not apply, inhibit Huawei charging.

| File (under `/opt/victronenergy/dbus-systemcalc-py/`) | SHA-256 | Role |
| --- | --- | --- |
| `delegates/dvcc.py` (1245 lines) | `923a1c1ab24b8125ed4685fb8e8f449710f5fe2bd44b697d1f3ca1b0c2e5b495` | **the only file patched** |
| `dbus_systemcalc.py` | `9ce9c1a14d4f501b53fee6b918f845a4dddd7991a80f297f1b8b8251d7176e4b` | unchanged, compatibility-checked |
| `delegates/__init__.py` | `5e397f73b6fca566db7be0b77eae6ba90ba0f50b84060bb4c78736081440742f` | unchanged |
| `delegates/base.py` | `bda6e20937567f6d233c1ff1a08c0c435d9b41650d775d0da366373cf1607da0` | unchanged |
| `sc_utils.py` | `ae61f5d62a4f0a6621770c53b444afe310f83c63261d035ab4577977511149b6` | unchanged |
| `ext/velib_python/dbusmonitor.py` | `3fbf56b43bc861172ca9848cfbbb344f77462fe9cccf60fe8f14be4c03b80a38` | unchanged, semantics relied upon (§6) |
| `ext/velib_python/vedbus.py` | `9d0170d84ad1cbb2072870810c33647c492002b775fa81ce2d4d295224fc584c` | unchanged, used by the driver |

Victron already applies a patch series to this package (`patches/series` → `FreedomWON.patch -p1`), so modifying it by patch is an established mechanism on this image. [V]

## 3. How the installed allocation works today [D]

`Dvcc._on_timer` runs every second; allocation happens every `ADJUST = 3` seconds:

1. **Inputs.** BMS CVL/CCL from the battery service (`/Info/MaxChargeVoltage`, `/Info/MaxChargeCurrent`); Pylontech quirk clamps CVL to 52.4 V and leaves CCL unchanged; user `/Settings/SystemSetup/MaxChargeCurrent` (180 A here) is min-ed in; measured DC-system current is added only if a real DC meter exists (none here, `HasDcSystem = 0`).
2. **VE.Bus compensation.** If the Multi is discharging (negative DC current), the budget offered to solar chargers is raised by that amount (`_max_charge_current`). The uncompensated value (`max_charge_current`) is kept for the Multi stage.
3. **Solar stage.** `SolarChargerSubsystem.set_networked(...)`: writes `/Link/NetworkMode` and `/Link/ChargeVoltage` to every solar charger. Then, because this system has ESS + `OvervoltageFeedIn = 1`, `feedback_allowed` is true and **all solar chargers are maximised** (`/Link/ChargeCurrent := /Settings/ChargeCurrentLimit`). Regulation then happens through the charge voltage, which in ESS is the Multi's `/Hub/ChargeVoltage` (lowered by the Multi when ESS wants the battery to discharge).
4. **Multi stage.** `max_charge_current - solarsystem.smoothed_current` is written to `/BatteryOperationalLimits/MaxChargeCurrent` on the vebus service, together with CVL, DCL and low-voltage limit.
5. **Flags.** `/Control/SolarChargeVoltage`, `/Control/SolarChargeCurrent`, `/Control/EffectiveChargeVoltage`, `/Control/BmsParameters`, `/Control/MaxChargeCurrent` on `com.victronenergy.system`.
6. **BMS lost.** If a BMS was ever seen and is now gone, DVCC stops writing to solar chargers (they time out into their own BMS-lost error) and returns early. The Multi keeps its last BOL values.

`com.victronenergy.charger` services are not part of this loop at all. systemcalc only sums their V×I into `/Dc/Charger/Power`. [D]

## 4. Target behaviour after the patch [P]

Insert a **managed-charger stage** between the solar stage and the Multi stage:

```
budget      = min(BMS CCL after quirk, user limit) [+ measured DC load if any]
solar       : unchanged (maximised under feed-in, else distributed)
chargers    : charger_budget = budget - solar.smoothed_current
              distributed across eligible Huawei services with distribute()
              charge voltage := same effective voltage the solar stage used
multi       : BOL.MaxChargeCurrent = budget - solar.smoothed_current - chargers.smoothed_current
```

Design choices embedded in that flow:

- **Compensated budget for the Huaweis (generator-fed).** The user confirmed on 2026-10-22 that the Huaweis are fed from a generator, not from the grid. Generator → rectifier → battery → Multi → loads is then the intended DC-coupled generator path, so the Huawei stage uses the same VE.Bus-compensated budget as the solar stage: when the Multi is inverting, the Huaweis may cover that discharge in addition to the battery's own charge limit. A policy setting (§7) can restrict them to the uncompensated budget if ever needed; it defaults to compensated.
- **Never maximise.** `feedback_allowed` has no effect on the charger stage. A grid-fed rectifier is always budget-limited.
- **Priority order is a setting**, default "solar, then chargers, then Multi". The alternative "solar, Multi, then chargers" is implemented by swapping which measured current is subtracted from which stage; both variants use measured (smoothed) output of the higher-priority stage, exactly as the existing Multi stage does.
- **Voltage target follows ESS.** The Huaweis receive the same effective charge voltage as the solar chargers, which in ESS is the Multi's hub voltage. When ESS lowers that voltage to discharge the battery, the Huaweis back off through their own voltage loop, in addition to the current budget. This is how the existing system coordinates intent with DC-coupled chargers and it is reused unchanged.
- **Zero means zero.** If the BMS limit is 0, or `stop_on_mcc0` applies, or the budget is unknown (`None`), the charger stage writes `/Link/ChargeCurrent = 0`. Unknown is never treated as unlimited.
- **BMS lost.** The existing early return is extended: before returning, write 0 to every managed charger once, then stop writing. The driver's own freshness timeout provides the second layer.

Numeric example with the values seen on 2026-10-22 (CCL 40 A at 99 % SOC, no solar at night, Multi idle): Huaweis get 40 A total, 20 A each if equally capable; the Multi's BOL becomes 40 A minus measured Huawei output, trending to 0 A as they ramp up.

## 5. Files and what changes in each

### 5.1 `delegates/dvcc.py` — patched (the only Victron file touched)

Hunks, in file order:

| Location (v2.92 line) | Change |
| --- | --- |
| imports (~line 12) | `from delegates.managedcharger import ManagedChargerSubsystem` |
| `Dvcc.__init__` (740) | add `self._chargersystem = None` |
| `Dvcc.get_input` (750) | add a `('com.victronenergy.charger', [...])` entry listing: `/ProductId`, `/Connected`, `/State`, `/Dc/0/Current`, `/Link/NetworkMode`, `/Link/ChargeVoltage`, `/Link/ChargeCurrent`, `/Settings/ChargeCurrentLimit`, `/Mgmt/Connection`, `/Mgmt/ProcessName`. systemcalc merges these into the monitor tree automatically (`dbus_systemcalc.py` lines 208–211). |
| `Dvcc.get_settings` (811) | add `('chargerpriority', '/Settings/Dvcc/ChargerPriority', 0, 0, 1)` and `('chargerstopwheninverting', '/Settings/Dvcc/ChargerStopWhenInverting', 1, 0, 1)`. localsettings creates and persists them in `/data/conf/settings.xml`; no file edit needed for configuration. |
| `Dvcc.set_sources` (817) | create `self._chargersystem = ManagedChargerSubsystem(dbusmonitor)`; add output paths `/Control/ChargerChargeCurrent` (0/1), `/Control/ChargerChargeVoltage` (0/1), `/Dvcc/Chargers/Budget` (A), `/Dvcc/Chargers/Count`. |
| `Dvcc.device_added` (837) | `elif service_type == 'charger' and ManagedChargerSubsystem.is_eligible(dbusmonitor, service): self._chargersystem.add_charger(service)`; start the timer as for other types. |
| `Dvcc.device_removed` (855) | remove from `_chargersystem`; include it in the "nothing left, stop timer" test. |
| `Dvcc._on_timer` (882) | (a) call `self._chargersystem.update_values()` next to the solar one; (b) pass `self._solarsystem.capacity + self._chargersystem.capacity` to `self._multi.update_values(...)` so the Multi discharge filter cap reflects total charger capacity; (c) in the BMS-lost branch, call `self._chargersystem.shutdown()` before returning; (d) after `_update_solarchargers`, compute `charger_budget` and call `self._chargersystem.set_networked(has_bms, effective_charge_voltage, charger_budget, stop_on_mcc0, multi_inverting)`; (e) subtract `self._chargersystem.smoothed_current` in the Multi remainder; (f) set the new control flags. |

Estimated diff size: 40–60 changed or added lines. All class logic lives in the new module so the diff against Victron's file stays small and reviewable.

### 5.2 `delegates/managedcharger.py` — new file, ours

Contains:

- `ManagedCharger` — a thin D-Bus accessor like `SolarCharger` (reuse `LowPassFilter` and `_set_path` semantics, i.e. only write to paths the monitor has seen). Properties: `chargecurrent`, `currentlimit` (`/Settings/ChargeCurrentLimit`), `maxchargecurrent` (clamped `0 ≤ v ≤ currentlimit`), `chargevoltage`, `networkmode`, `state`, `connected`, `smoothed_current`.
- `ManagedChargerSubsystem` — collection with `capacity`, `smoothed_current`, `update_values()`, `shutdown()` (write 0 to all), and `set_networked(...)` that: writes NetworkMode `1 | 4 | (8 if has_bms)`; writes the charge voltage; then if `budget is None or budget <= 0 or stop_on_mcc0 or (multi_inverting and stop_setting)` writes 0 to all, elif one charger: `min(ceil(budget), limit)`, else uses the existing `distribute()` from dvcc.py exactly as `_distribute_current` does, with the same 0.1 A per charger dead-band.
- `is_eligible(monitor, service)` — a charger is managed only if it publishes `/Link/ChargeCurrent` **and** `/Settings/ChargeCurrentLimit` **and** its `/Mgmt/ProcessName` matches the Huawei driver name. This keeps a future Skylla-i or other generic charger out of the loop unless explicitly supported.

### 5.3 `dbus_systemcalc.py` — not patched

Already monitors `com.victronenergy.charger` for `/Dc/0/Voltage`, `/Dc/0/Current`, `/Connected`, `/ProductName`, `/Mgmt/Connection`, sums `/Dc/Charger/Power`, and calls every delegate's `device_added` for any monitored service type. No change required. [D]

### 5.4 `delegates/__init__.py` — not patched

The new module is imported from `dvcc.py`, not registered as a delegate, because it must run inside the DVCC tick between the solar and Multi stages. A separate delegate cannot interleave there.

### 5.5 GUI — not patched (optional later)

`com.victronenergy.charger` services already have a device page (`PageAcCharger.qml`, used for Skylla-i) that shows voltage, current and state. Custom paths such as command age will not appear unless a page is added. Optional, out of scope for the first version. [V]

### 5.6 Driver side (new, under `/data`, not a patch)

`com.victronenergy.charger.huawei_<serial>` services as in the brief §9, created with the bundled `VeDbusService(servicename)` (older constructor, no `register` keyword) [D]. The driver must publish the four paths from §5.2 eligibility and treat `/Link/ChargeCurrent` writes as the heartbeat (§6).

## 6. Heartbeat and failure detection [V] (corrected 2026-10-22 after the first live run)

Original assumption: the driver sees DVCC's `SetValue` on `/Link/ChargeCurrent` every 3 s, because `DbusMonitor.set_value_async` sends it on every tick even when the value is unchanged (dbusmonitor.py 410-420) [D]. **That is true on the sending side but not at the receiver:** `VeDbusItemExport.SetValue` returns early when the new value equals the current one and never invokes the driver's change callback [D, vedbus.py]. The first supervised run of the stub showed exactly this: one callback, then `CommandAgeSeconds` growing to 20 s while DVCC was alive and writing.

Implemented instead: an explicit heartbeat. The driver publishes a writeable `/Huawei/Heartbeat` path; the managed-charger stage writes a counter that changes on every ADJUST tick in which DVCC is in control (`ManagedChargerSubsystem.set_networked`). It is **not** written by `shutdown()`, so BMS lost, DVCC switched off, patch missing and systemcalc death all look the same to the driver: the counter freezes. Driver rule: no change for more than N seconds (proposed N = 10, to be confirmed against measured Huawei fallback timing in Phase B) means live current 0 and inhibited state. `/Huawei/CommandAgeSeconds` is derived from the heartbeat. The persistent zero-current offline default on the rectifier (register 0x0104) remains the hardware-level layer behind this. A driver without the path is still managed, just without heartbeat (test `test_charger_without_heartbeat_path_still_managed`).

Secondary check: the driver reads `/Control/ChargerChargeCurrent` on `com.victronenergy.system`. Missing path means patch not active, inhibit and report "DVCC extension not present".

## 7. Settings introduced

| Setting | Default | Meaning |
| --- | --- | --- |
| `/Settings/Dvcc/ChargerPriority` | 0 | 0 = solar → chargers → Multi; 1 = solar → Multi → chargers. **Set to 1 on this installation since 2026-11-09** (user decision after the first on-grid run: when the BMS lowers the charge limit near full, the Huawei units drop first and the Quattro keeps charging) |
| `/Settings/Dvcc/ChargerCompensateVebus` | **0** | 0 = Huawei budget is the battery's own charge limit only (default, chosen 2026-10-22 for the full-battery safety review in §11); 1 = budget also covers the Multi's discharge current (DC-coupled generator path, opt-in after commissioning). With either value the chargers get 0 when the battery limit is 0. |
| `/Settings/Dvcc/ChargerMaxSoc` | **90** | SOC ceiling for the managed chargers, added 2026-10-22 after the user's full-battery question: at or above this SOC the charger budget is 0 regardless of BMS limits. 100 = off. Unknown SOC = 0. |
| `/Settings/Dvcc/ChargerResumeSoc` | **85** | Hysteresis: once inhibited, the chargers get a budget again only below this SOC. `/Dvcc/Chargers/SocInhibit` on `com.victronenergy.system` shows the state. |
| per device `/Settings/ChargeCurrentLimit` | from driver config | Per-rectifier ceiling in A, owned by the driver, persisted via settingsdevice |

An AC-power ceiling and margins belong to the driver, not DVCC, because DVCC only sees DC amperes. The AC source is a Hyundai HY8500LEK petrol generator. Handbook technical data (page 8, confirmed by the user as the single-phase HY8500LEK, not the -T) [V]: rated 8.0 kW, maximum 8.5 kW, power factor 1, 230 V 50 Hz, rated current 34.8 A, outputs 2 × 230 V 16 A and 1 × 230 V 32 A, engine R500-V 500 cc, fuel 2.59–4.46 L/h at 50–100 % load, 28 L tank, 10.5–6.0 h runtime. Two R4850G2 at full output draw about 6.2 kW, 78 % of rated. Proposed driver caps: group 100 A DC (about 5.5 kW AC, 69 % of rated, roughly 3.3 L/h and 8 h per tank), 50 A per unit, to be adjusted after the Phase B generator run. Both rectifiers on the 32 A outlet through the transfer switch.

SOC policy (driver settings, not DVCC): stop generator charging at a configurable SOC, proposed 90 %, resume below a lower threshold, proposed 80 %. Reasons: the Pylontech halves its charge limit near full (40 A at 99 % observed), so the last few percent cost a long low-load generator run for little energy; and the user has an open Pylontech high-voltage alarm investigation (cell logger in /data since 2026-10-17), which argues for letting solar or grid do the top-end balancing rather than a 5 kW source. The BMS limits remain the safety layer regardless of the chosen stop value. A manual transfer switch will make the generator feed either the Quattro's AC input 1 or the Huaweis, so no subtraction for the Quattro is needed; the full generator rating is the Huawei ceiling.

## 8. Installation procedure (for an authorised window) [P]

1. Verify `sha256sum delegates/dvcc.py` equals the pristine hash in §2. Abort otherwise.
2. Copy the pristine file to `/data/huawei/backup/dvcc.py.v2.92.orig`.
3. Copy `managedcharger.py` into `delegates/`; apply the patch with `patch -p1 --dry-run` then for real; remove `delegates/__pycache__/dvcc.*.pyc`.
4. Record the post-patch hash in `/data/huawei/patch.manifest`.
5. Restart systemcalc: `svc -t /service/dbus-systemcalc-py`. Expected gap: a few seconds in which `/Control/*` disappears; the Multi keeps its last BOL values, solar chargers keep their last link values. Victron restarts this service on every firmware update, so the system tolerates it, but do it with the Huaweis still inhibited.
6. Verify: `/Control/ChargerChargeCurrent` exists; existing behaviour unchanged (solar still maximised, Multi BOL unchanged with no Huawei present).

Rollback: copy the `.orig` back, remove `managedcharger.py`, restart systemcalc. One file, one restart.

Boot-time guard (from `/data/rc.local`): if the installed `dvcc.py` hash equals the pristine v2.92 hash, the rootfs was replaced (firmware update) and the patch may be re-applied automatically because the version is identical; if it equals the patched hash, nothing to do; anything else ⇒ do not touch, write a marker the driver reads to stay inhibited.

## 9. Testing [P]

- The package ships its test harness on the device (`tests/`, with a mock D-Bus in `tests/base.py` and `hub_test.py` covering the DVCC loop). Copy the package read-only to the development machine, apply the patch, run the existing suite to prove nothing regressed, and add `managedcharger_test.py` covering: single/dual charger split; CCL reduction; CCL = 0; BMS lost; `feedback_allowed` true does not maximise chargers; Multi remainder reduced by charger output; `ChargerStopWhenInverting`; ineligible charger ignored; charger removal mid-run.
- On-device verification stays read-only until the window: first run with the driver publishing services but with its own output hard-inhibited, and observe the allocated values on D-Bus only.

## 10. Residual risks and open decisions

- **ESS grid-meter interplay: resolved.** The Huaweis are generator-fed and never pass through the ET112 grid meter, so ESS does not see them as load. The generator itself becomes the independent shutdown path required by the brief §8: stopping it (or an AC contactor in its feed) removes rectifier output regardless of CAN state. The driver should treat "generator running" as an enable precondition and ramp down before a planned generator stop.
- **Generator topology: resolved.** The Quattro's AC input 1 is configured as "Generator" (40 A input limit) and a digital input is labelled "Grid back - Please switch off generator". The user will install a manual transfer switch (MTS) so the generator feeds **either** the Quattro's AC input 1 **or** the Huaweis, never both. Consequences: the Huawei AC ceiling is the full generator continuous rating with margin, with no subtraction for the Quattro; the Huaweis' own AC-input telemetry is the "generator present" enable signal; and the driver should raise a consistency alarm if the Huaweis report AC present while the vebus service reports AC input 1 active (`/Ac/ActiveIn/ActiveInput == 0`), since the MTS should make that impossible. Generator stop remains the independent shutdown path.
- **Two chargers, one bus.** Both VE.Can RJ45 sockets are one interface (`can0`); one rectifier per socket, both on the same bus. One driver process, one interface, two modules keyed by serial, two D-Bus services. Terminate at both rectifier ends, nothing at the Cerbo. RJ45 pins 7/8 = CAN-H/CAN-L per Victron VE.Can pinout, to be confirmed on the bench.
- **CAN port ownership: resolved 2026-10-22.** VE.Can profile set to Disabled: venus-platform stops vecan-dbus and brings `can0` down; a manual `ip link set can0 up type can bitrate 125000` is left alone for at least 60 s with no platform reaction. The driver's start script therefore does `ip link set can0 down; ip link set can0 up type can bitrate 125000 restart-ms 100` and checks the profile is still 0 before transmitting; if the profile is not 0 it must not touch the link and must inhibit. Reboot persistence still to be observed once.
- **Firmware updates** remove the patch. Mitigated by hash guard and inhibit-by-default; the user's policy of not updating removes the practical risk.
- **Restarting systemcalc** is the one disruptive step. Needs a window.
- **Allocation lag.** Smoothed currents use the same 20 s low-pass as the solar stage; the Multi will briefly over-book while Huaweis ramp. Bounded ramp-up in the driver (brief §11) keeps the transient small; the Pylontech CCL is the backstop.
- **Priority between Multi and Huaweis.** With the generator feeding the Huaweis exclusively, the Multi is on grid or islanded while the Huaweis run. Default stays solar → Huaweis → Multi: generator energy should go into the battery first, the Multi charges from grid surplus if any. Generator is started by hand, so there is no software ramp-down before a stop; Phase B test B7.4 measures the AC-loss behaviour that this relies on.

## 11. Side-effect review before installation (2026-10-22, requested by the user)

Question asked: can the patch damage the system, in particular by charging into a full battery stack? Reviewed against the installed code and the unit tests; evidence tags as above.

**What the patch can and cannot write.** The new stage writes only to `com.victronenergy.charger` services that opt in (publish `/Link/ChargeCurrent` and `/Settings/ChargeCurrentLimit`, and have a `/Mgmt/ProcessName` containing "huawei"). There is no other charger service on this system [V]. The existing writes to solar chargers, the vecan service and the Multi's BatteryOperationalLimits are untouched in code path and value when no managed charger is present (test `test_no_charger_leaves_solar_and_multi_unchanged`; the full upstream suite, 257 tests, gives the same result before and after the patch, only the two hardware buzzer tests fail on Windows in both runs) [V on the development machine].

**No hardware can be driven today.** The only charger services are the stub's; the stub has no CAN access and reports 0 A. The real driver does not exist yet, and its own rules (brief §6, §11: zero-current offline default, inhibit without heartbeat, caps) are additional to everything below.

**Full-battery case, layer by layer.** The Pylontech does not report CCL = 0 when full; the installed quirk comments say 48 V Pylontechs halve the CCL when full and that the protection is the charge-voltage clamp to 52.4 V [D]. Therefore:

1. *Voltage.* The managed chargers receive exactly the effective charge voltage the solar chargers receive, which in this ESS system is the Multi's hub voltage after the Pylontech clamp (52.4 V observed on the MPPT on 2026-10-22) [V]. A charger at 52.4 V cannot push current into a battery that already sits at 52.4 V. Test `test_pylontech_voltage_clamp_reaches_chargers`.
2. *Current.* Budget = min(BMS CCL, user limit) minus measured solar output, distributed over the chargers, never maximised, never above a charger's own limit. CCL 0 gives 0 (`test_ccl_zero_gives_zero`); unknown budget gives 0, not unlimited (`test_no_bms_no_user_limit_gives_zero`).
3. *VE.Bus compensation.* Changed to default **off** in this review: the chargers never exceed the battery's own limit unless the user opts in by setting `/Settings/Dvcc/ChargerCompensateVebus` = 1. Even with it on, a battery limit of 0 forces the charger budget to 0 regardless of Multi discharge (`test_ccl_zero_with_compensation_on_still_zero`). The transient risk that motivated this (Multi load dropping while chargers cover it, up to the 30 s low-pass) is thereby excluded by default.
4. *Loss of control.* BMS lost, DVCC switched off, or systemcalc not running: the chargers are written 0 once and then receive no heartbeat, which the driver turns into inhibit (`test_bms_lost_shuts_chargers_down`, `test_dvcc_off_shuts_chargers_down`). A charger that reports State 0 (driver inhibited) gets 0 and the others get the budget (`test_idle_charger_gets_zero_and_other_gets_all`).
5. *SOC ceiling in DVCC* (added after this review, see §7): at 99 % SOC on 2026-10-22 the Pylontech still reported CCL 118–148 A, so the current budget alone did not stop the chargers (the stub was allocated its full 50 A per unit in the allocation test). With `ChargerMaxSoc` = 90 the budget is 0 from 90 % up and resumes below 85 %; verified live: `/Dvcc/Chargers/SocInhibit` = 1 and budget 0.0 at SOC 99. The user's own notes (`C:\Dev-Free\CerboGX\high-voltage-alarms-2026-10-17.md`) show one cell overshooting to 3.65 V at 97–99 % SOC under ~100 A, which is exactly the region this ceiling keeps the generator chargers out of. Tests `test_soc_ceiling_*`, `test_soc_unknown_gives_zero`, `test_soc_ceiling_does_not_touch_solar_or_multi`.
6. *Driver-side SOC stop* remains available as a further, independent layer.

**Other side effects, listed so they are known.**

- Two new settings in localsettings (`/Settings/Dvcc/ChargerPriority`, `/Settings/Dvcc/ChargerCompensateVebus`) and four new paths on `com.victronenergy.system` (`/Control/ChargerChargeCurrent`, `/Control/ChargerChargeVoltage`, `/Dvcc/Chargers/Budget`, `/Dvcc/Chargers/Count`). vrmlogger ignores unknown paths; MQTT publishes them. Harmless.
- The DVCC timer now also keeps running while only managed chargers (no solar charger, battery or vecan service) exist. Required for the heartbeat, no other effect.
- The Multi's discharge low-pass cap now includes the managed chargers' capacity. With no charger present the value is unchanged (`safeadd` ignores None).
- Restarting systemcalc once at install: a few seconds without `/Control/*`; the Multi keeps its BOL values, MPPTs keep their link values. Same as every firmware update.
- Pristine behaviour of systemcalc, unrelated to the patch but present as soon as any charger service exists: if the battery service were auto-selected and no battery service existed, systemcalc would select none rather than falling back to the VE.Bus SOC (`_autoselect_battery_service`). The Pylontech is present, so this does not apply; the `BatteryService` setting was recorded at install.
- Firmware update would remove the patch (rootfs replaced). The boot guard re-applies only when the identical pristine file is found; anything else leaves a marker and the driver stays inhibited.

Conclusion: no path by which the patch itself delivers current; for the future driver, the voltage clamp is the primary full-battery protection, the current budget the second, the default-off compensation and zero-on-unknown rules the third, and the driver's own inhibit and SOC stop the fourth.

## 11b. VRM visibility (verified 2026-10-22, Phase A report §8f)

VRM has no dashboard element for `com.victronenergy.charger` services. Options analysed: feeding the charge power into the DC-system estimate (would corrupt the kWh accounting and the DC-system box), presenting it as a generator meter (creates a phantom AC load on AC-in 1), presenting it as solar (misattributes energy; rejected), or publishing a display-only `com.victronenergy.dcsource` service per unit. The last was tested with the stub: VRM draws it and labels it "AC charger" (monitor mode −2). The user accepted that label as accurate for an AC-to-DC rectifier. This is a driver-side feature; the DVCC patch is not involved and ignores dcsource services.

## 11c. GUI tile support (2026-10-22)

The patch publishes `/Dvcc/Chargers/Active` (managed chargers that are connected and not in State 0) and the per-unit powers `/Dvcc/Chargers/0/Power`, `/1/Power` (ordered by service name, from `/Dc/0/Power` or voltage x current) for the Remote Console overview tile (`src/classic-gui/`). Display aids only: no control path reads them. Patched file hash `bdcccd1e…`, module `dabc4c12…`; the previous patched hashes are in `OLD_PATCHED_SHAS` so `install.sh` upgrades in place.

## 12. Implementation record

- `src/patch/apply_hunks.py`: source of truth for the 14 hunks; `src/patch/huawei-dvcc-2.103-1.patch`: generated unified diff (77 lines added, 5 removed); `src/patch/patched-dvcc.py`: the shipped whole file; `src/patch/managedcharger.py`: the new module; `src/patch/tests/managedcharger_test.py`: 31 tests in the upstream harness (run on Windows with the shims described in the Phase A report).
- `src/patch/install.sh`: `install | check | rollback | guard`, pins SHA-256 of pristine, patched and module file, backs up to `/data/huawei/backup/dvcc.py.2.103-1.orig`, replaces the file atomically, clears `__pycache__`, compiles, writes `/data/huawei/patch.manifest`, restarts systemcalc with `svc -t`, waits for `/Control/ChargerChargeCurrent`.
- `src/deploy/boot.sh` (from `/data/rc.local`): runs the guard and links `/data/huawei/service` into `/service/huawei-charger`; `src/deploy/service/{run,log/run}`: daemontools service, currently running the stub.
- Installed builds on the Cerbo on 2026-10-22: 19:48 first build, 19:53 heartbeat build, 20:03 SOC-ceiling build (current, dvcc.py `8ae731a2…ea46ca`, managedcharger.py `f9144ae9…54adce`). Each upgrade went through `install.sh install` (state `patched-old` → `patched`), one systemcalc restart each, pre-existing control values unchanged every time (`evidence_patch_*.txt`).
- Interaction noted from the user's Node-RED notes: the "Grid return staggering" tab writes the DVCC user limit (5 A → ramp → 180 A after a grid return). That limit is the total budget, so the Huaweis are ramped with it. No conflict, documented in `C:\Dev-Free\CerboGX\grid-return-staggering-plan.md`.
