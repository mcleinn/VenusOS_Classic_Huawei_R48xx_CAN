#!/bin/sh
# Huawei managed-charger extension for DVCC: installer for dbus-systemcalc-py
# 2.257-1 as shipped in Venus OS v3.85 (Raspberry Pi; any GX with that version). Runs on the device as root.
#
#   install.sh install [--no-restart]   back up, patch, restart systemcalc, verify
#   install.sh check                    print the state (pristine | patched | unknown)
#   install.sh rollback                 restore the pristine file, restart, verify
#   install.sh guard                    boot-time use: re-apply only if the file is
#                                       the pristine 2.257-1 one (rootfs swapped back
#                                       to the identical version); never touch anything
#                                       else, but leave a marker the driver can read
#
# Only delegates/dvcc.py is modified, by whole-file replacement with a copy whose
# hash is pinned here; delegates/managedcharger.py is added. All hashes are SHA-256.
set -u

PKG=/opt/victronenergy/dbus-systemcalc-py
TARGET=$PKG/delegates/dvcc.py
MODULE=$PKG/delegates/managedcharger.py
SERVICE=/service/dbus-systemcalc-py
HERE=$(dirname "$(readlink -f "$0")")
BACKUP=/data/huawei/backup
MANIFEST=/data/huawei/patch.manifest
MARKER=/data/huawei/dvcc_unsupported

PRISTINE_SHA=8c18880c240961fa5dfc5ea5a712aead24b7f3670161687db01dbf680fcbea64
PATCHED_SHA=0e9ed738c18f3e548148f98960050da9b13d3be80a821779dc63cbdd836fa8ac
# Hashes of earlier patched builds of dvcc.py; a file matching one of these
# is replaced by the current build (the pristine backup already exists).
OLD_PATCHED_SHAS=""
MODULE_SHA=1900a5b90f710a6d03f160473258ef60ce6fd79e7f39660565316e46ad8b8e04

sha() { sha256sum "$1" 2>/dev/null | cut -d' ' -f1; }
log() { echo "$(date -u '+%Y-%m-%d %H:%M:%S') install.sh: $*"; }
die() { log "ABORT: $*"; exit 2; }

state() {
	s=$(sha "$TARGET")
	if [ "$s" = "$PRISTINE_SHA" ]; then
		echo pristine
	elif [ "$s" = "$PATCHED_SHA" ] && [ "$(sha "$MODULE")" = "$MODULE_SHA" ]; then
		echo patched
	else
		for o in $OLD_PATCHED_SHAS; do
			[ "$s" = "$o" ] && { echo patched-old; return; }
		done
		[ "$s" = "$PATCHED_SHA" ] && { echo patched-old; return; }
		echo unknown
	fi
}

restart_systemcalc() {
	log "restarting dbus-systemcalc-py"
	svc -t "$SERVICE"
}

verify_running() {
	# Wait up to 40 s for the patched delegate to publish its control flag.
	i=0
	while [ $i -lt 40 ]; do
		v=$(dbus -y com.victronenergy.system /Control/ChargerChargeCurrent GetValue 2>/dev/null)
		case "$v" in
			0|1) log "verify: /Control/ChargerChargeCurrent = $v (patch active)"; return 0 ;;
		esac
		sleep 1
		i=$((i + 1))
	done
	log "verify: FAILED, /Control/ChargerChargeCurrent not published within 40 s"
	return 1
}

verify_pristine_running() {
	i=0
	while [ $i -lt 40 ]; do
		v=$(dbus -y com.victronenergy.system /Control/Dvcc GetValue 2>/dev/null)
		case "$v" in
			0|1) log "verify: systemcalc running, /Control/Dvcc = $v"; return 0 ;;
		esac
		sleep 1
		i=$((i + 1))
	done
	log "verify: FAILED, systemcalc did not come back within 40 s"
	return 1
}

clear_pycache() {
	rm -f "$PKG"/delegates/__pycache__/dvcc.*.pyc "$PKG"/delegates/__pycache__/managedcharger.*.pyc
}

do_install() {
	norestart=${1:-}
	case "$(state)" in
		patched)
			log "already patched (dvcc.py $PATCHED_SHA)"; rm -f "$MARKER"; return 0 ;;
		patched-old)
			log "earlier patched build found, upgrading" ;;
		unknown)
			touch "$MARKER"
			die "installed dvcc.py has hash $(sha "$TARGET"), not the pinned 2.257-1 file; nothing touched, marker $MARKER written" ;;
	esac
	[ "$(sha "$HERE/patched-dvcc.py")" = "$PATCHED_SHA" ] || die "shipped patched-dvcc.py hash mismatch"
	[ "$(sha "$HERE/managedcharger.py")" = "$MODULE_SHA" ] || die "shipped managedcharger.py hash mismatch"

	mkdir -p "$BACKUP"
	if [ ! -f "$BACKUP/dvcc.py.2.257-1.orig" ]; then
		cp -p "$TARGET" "$BACKUP/dvcc.py.2.257-1.orig" || die "backup copy failed"
	fi
	[ "$(sha "$BACKUP/dvcc.py.2.257-1.orig")" = "$PRISTINE_SHA" ] || die "backup hash mismatch"
	log "backup verified: $BACKUP/dvcc.py.2.257-1.orig"

	cp "$HERE/managedcharger.py" "$MODULE.new" && mv "$MODULE.new" "$MODULE" || die "module copy failed"
	chmod 644 "$MODULE"
	cp "$HERE/patched-dvcc.py" "$TARGET.new" && mv "$TARGET.new" "$TARGET" || die "dvcc.py replace failed"
	chmod 644 "$TARGET"
	clear_pycache
	if ! python3 -m py_compile "$TARGET" "$MODULE"; then
		log "py_compile failed, rolling back"
		cp "$BACKUP/dvcc.py.2.257-1.orig" "$TARGET"; rm -f "$MODULE"; clear_pycache
		die "compile failure after patch; pristine file restored"
	fi
	[ "$(state)" = "patched" ] || die "post-install state is $(state)"
	{
		echo "installed=$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
		echo "package=dbus-systemcalc-py 2.257-1 (Venus OS v3.85)"
		echo "dvcc.py.pristine=$PRISTINE_SHA"
		echo "dvcc.py.patched=$PATCHED_SHA"
		echo "managedcharger.py=$MODULE_SHA"
		echo "backup=$BACKUP/dvcc.py.2.257-1.orig"
	} > "$MANIFEST"
	rm -f "$MARKER"
	log "patch installed"
	if [ "$norestart" != "--no-restart" ]; then
		restart_systemcalc
		verify_running
	fi
}

do_rollback() {
	[ -f "$BACKUP/dvcc.py.2.257-1.orig" ] || die "no backup at $BACKUP/dvcc.py.2.257-1.orig"
	[ "$(sha "$BACKUP/dvcc.py.2.257-1.orig")" = "$PRISTINE_SHA" ] || die "backup hash mismatch, refusing to restore"
	cp "$BACKUP/dvcc.py.2.257-1.orig" "$TARGET.new" && mv "$TARGET.new" "$TARGET" || die "restore failed"
	chmod 644 "$TARGET"
	rm -f "$MODULE"
	clear_pycache
	[ "$(state)" = "pristine" ] || die "post-rollback state is $(state)"
	echo "rolledback=$(date -u '+%Y-%m-%dT%H:%M:%SZ')" >> "$MANIFEST"
	log "pristine dvcc.py restored"
	restart_systemcalc
	verify_pristine_running
}

do_guard() {
	case "$(state)" in
		patched)  log "guard: patched, nothing to do"; rm -f "$MARKER" ;;
		pristine) log "guard: pristine 2.257-1 found (rootfs replaced?), re-applying"; do_install ;;
		patched-old) log "guard: earlier patched build found, upgrading"; do_install ;;
		unknown)  touch "$MARKER"; log "guard: unsupported dvcc.py $(sha "$TARGET"); not touched, marker written" ;;
	esac
}

case "${1:-}" in
	install)  do_install "${2:-}" ;;
	check)    echo "state: $(state)"; echo "dvcc.py: $(sha "$TARGET")"; echo "managedcharger.py: $(sha "$MODULE")"; [ -f "$MARKER" ] && echo "marker: $MARKER present" ;;
	rollback) do_rollback ;;
	guard)    do_guard ;;
	*) echo "usage: $0 install [--no-restart] | check | rollback | guard"; exit 1 ;;
esac
