#!/bin/sh
# Huawei integration boot hook. Called once at late boot from /data/rc.local. Works on the Cerbo GX
# (Venus v2.92) and on a Raspberry Pi (Venus v3.x): every step runs only if its files exist.
# 1. DVCC patch guard (both platforms; /data/huawei/patch holds the installer of the device's systemcalc
#    version, src/patch/<version>/): re-apply only if /opt holds the pristine file of that version,
#    never touch an unknown version (marker file tells the driver to stay inhibited).
# 2. GUI tile guard (Cerbo): same idea for the two overview qml files of the classic GUI (/data/huawei/classic-gui).
# 3. USB CAN adapter guard (Cerbo, 2026-11-03): /etc/modprobe.d/huawei-usbcan.conf blacklists gs_usb so the
#    display-board adapter can never take can0/can1 from the built-in VE.Can and BMS-Can ports at boot;
#    the service run script loads gs_usb itself once the built-in ports are in place. /etc is on the
#    root file system, which a firmware update replaces, so the file is re-installed here when missing
#    (too late for the boot in progress, in place for the next one). Not used on the Raspberry Pi.
# 4. Register the supervised driver service with daemontools (/service is tmpfs,
#    rebuilt every boot, so the symlink has to be made here).
LOG=/data/huawei/boot.log
{
	echo "$(date -u '+%Y-%m-%d %H:%M:%S') boot.sh start"
	[ -x /data/huawei/patch/install.sh ] && /data/huawei/patch/install.sh guard
	[ -x /data/huawei/classic-gui/install-classic-gui.sh ] && /data/huawei/classic-gui/install-classic-gui.sh guard
	if [ -f /data/huawei/huawei-usbcan.conf ] && ! cmp -s /data/huawei/huawei-usbcan.conf /etc/modprobe.d/huawei-usbcan.conf; then
		cp /data/huawei/huawei-usbcan.conf /etc/modprobe.d/huawei-usbcan.conf && echo "installed /etc/modprobe.d/huawei-usbcan.conf (gs_usb blacklist)"
	fi
	ln -sfn /data/huawei/service /service/huawei-charger
	echo "$(date -u '+%Y-%m-%d %H:%M:%S') boot.sh done"
} >> "$LOG" 2>&1
