# Huawei tile on the classic GUI overview (Venus OS v2.92)

"Classic GUI" = the Qt4/QML Remote Console GUI of Venus OS v2.x, superseded in later Venus versions
by gui-v2; everything here targets the classic one that this Cerbo runs.

Installed on the Cerbo on 2026-10-22 (23:04 UTC) at the user's request ("I want a tile in the
graphical view"). Verified live on the Remote Console with the stub driver the same evening.

## What it shows

A 135 x 45 purple box titled "AC Charger" in the bottom-centre slot of `OverviewHub.qml` (the slot
Victron reserves for the "DC Power" box, which this system does not use). With two units it shows
"x+yW" like the user's PV inverter tile, from `com.victronenergy.system /Dvcc/Chargers/0/Power` and
`/1/Power` (per unit, ordered by serial, published by the DVCC patch from `/Dc/0/Power` or V x I); with
one unit it shows the total `/Dc/Charger/Power` (stock systemcalc). A connection from the battery's right
edge into the tile is drawn like Victron's battery-to-DC-system connection; the ESS reason tag moves
above the battery while the box is visible, exactly as Victron does for the DC Power box.

Visible only while `/Dvcc/Chargers/Active` > 0, a path added to the Huawei DVCC patch: the number of
managed chargers that are connected and not in State 0. The driver publishes State 0 while a unit is
switched off, inhibited or faulted, so the tile disappears when the Huaweis are off. If a DC system is
ever enabled (`/Dc/System/Power` valid), the DC Power box wins the slot and the Huawei tile hides.

## Files

| Path | Content |
| --- | --- |
| `device-before/` | the two files as they were on the device before the change (HubData pristine; OverviewHub with the user's 2023 modification) |
| `modified/` | the files now installed in `/opt/victronenergy/gui/qml/` |
| `diffs/` | unified diffs before -> modified (`huawei-tile-all.diff` = both in one) |

Device backups: `/data/huawei/backup/gui/HubData.qml.2026-10-22.orig`, `OverviewHub.qml.2026-10-22.orig`.
This folder is the only copy of the tile change; the user's own six GUI modifications stay in
`C:\Dev-Free\CerboGX\background-patch\` (renamed from `Venus2.9.2-patch` 2026-11-06; which only carries a pointer here). This folder is also reachable as the junction `C:\Dev-Free\CerboGX\ac-tile-patch`. `diffs/huawei-tile-all.diff`
is both diffs in one. After a firmware update re-apply the six modifications first, then these.

## Installer (`install-classic-gui.sh`, deployed to `/data/huawei/classic-gui/`)

Same scheme as the DVCC patch installer: whole-file replacement with pinned SHA-256 hashes, backups
verified before anything is written, nothing touched when a file is unknown.

    /data/huawei/classic-gui/install-classic-gui.sh check        # state per file + backup hashes
    /data/huawei/classic-gui/install-classic-gui.sh install      # back up, install both files, restart the GUI
    /data/huawei/classic-gui/install-classic-gui.sh rollback     # restore the two backups, restart the GUI
    /data/huawei/classic-gui/install-classic-gui.sh guard        # boot-time: re-install only if both files are exactly the pre-tile versions

States: `installed`, `before` (pre-tile base: pristine HubData, the user's 2023 OverviewHub),
`installed-old` (an earlier build of the same evening, upgraded in place), `pristine` (OverviewHub as
shipped by Victron: refused, the user's six modifications must be re-applied first from
`C:\Dev-Free\CerboGX\background-patch`), `unknown` (refused). The guard runs from
`/data/huawei/boot.sh` at every boot. Manifest: `/data/huawei/classic-gui.manifest`.

Deploy a new build from the PC: copy `install-classic-gui.sh` and the two files in `modified/` to
`/data/huawei/classic-gui/` (update the hashes in the script first), then run `install`.
`svc -t /service/gui` blanks the display for a few seconds; it has no effect on control.
