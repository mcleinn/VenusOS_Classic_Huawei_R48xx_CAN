#!/bin/sh
# Huawei "AC Charger" overview tile: installer for the two files of the classic (Qt4/QML)
# GUI on Venus OS
# v2.92 (Cerbo GX). Runs on the Cerbo as root. Companion of ../patch/install.sh.
#
#   install-classic-gui.sh install [--no-restart]   back up, install both files, restart the GUI
#   install-classic-gui.sh check                    print the state per file (before | installed | pristine | unknown)
#   install-classic-gui.sh rollback                 restore the backups, restart the GUI
#   install-classic-gui.sh guard                    boot-time use: re-install only if both files are
#                                           exactly the pre-tile versions; never touch anything else
#
# Base ("before") versions: HubData.qml = pristine v2.92; OverviewHub.qml = the user's 2023
# modification (PV text "x+yW"). The pristine OverviewHub.qml is recognised but NOT patched:
# the user's six modifications (C:\Dev-Free\CerboGX\Venus2.9.2-patch) must be re-applied first.
# Files are replaced whole, with pinned SHA-256 hashes. A GUI restart blanks the display for a
# few seconds and has no effect on control.
set -u

QML=/opt/victronenergy/gui/qml
SERVICE=/service/gui
HERE=$(dirname "$(readlink -f "$0")")
BACKUP=/data/huawei/backup/gui
SUFFIX=.2026-10-22.orig
MANIFEST=/data/huawei/classic-gui.manifest
FILES="HubData.qml OverviewHub.qml"

BEFORE_HubData=e8858b7654a544464e578d21b5c7e6bf342716728fd1ca9e10ec40b5b54be933
BEFORE_OverviewHub=9f553549bde9a2fa4bc33fe6429368e77f9bc931d30d436f43c6f5897177f723
PRISTINE_OverviewHub=UNKNOWN   # set from Venus2.9.2-patch/pristine-v2.92 if ever needed
INSTALLED_HubData=828b6e9bf6210cc2182f75da6482b9b00adea7ba16edd91a267c9829ed798d04
INSTALLED_OverviewHub=26024cb7d75d0dfd7085de296940ca5c2cb2984b6eea87f47467eb144405691f
# earlier installed builds (same evening); upgraded in place
OLD_INSTALLED="b4bead65 e4504910 797762ff 900d3f73 23c0ec06 f7e5895a"

sha() { sha256sum "$1" 2>/dev/null | cut -d' ' -f1; }
md5() { md5sum "$1" 2>/dev/null | cut -c1-8; }
log() { echo "$(date -u '+%Y-%m-%d %H:%M:%S') install-classic-gui.sh: $*"; }
die() { log "ABORT: $*"; exit 2; }
var() { eval "echo \"\$${1}_${2%.qml}\""; }

state_of() {   # state_of <file>
	f=$1; s=$(sha "$QML/$f")
	if [ "$s" = "$(var INSTALLED "$f")" ]; then echo installed
	elif [ "$s" = "$(var BEFORE "$f")" ]; then echo before
	elif [ "$s" = "$(var PRISTINE "$f" 2>/dev/null)" ]; then echo pristine
	else
		m=$(md5 "$QML/$f")
		for o in $OLD_INSTALLED; do [ "$m" = "$o" ] && { echo installed-old; return; }; done
		echo unknown
	fi
}

do_check() {
	for f in $FILES; do echo "$f: $(state_of "$f") ($(sha "$QML/$f"))"; done
	for f in $FILES; do
		b="$BACKUP/$f$SUFFIX"
		if [ -f "$b" ]; then
			[ "$(sha "$b")" = "$(var BEFORE "$f")" ] && echo "backup $b: ok" || echo "backup $b: HASH MISMATCH"
		else echo "backup $b: missing"; fi
	done
}

restart_gui() { log "restarting gui"; svc -t "$SERVICE"; }

do_install() {
	norestart=${1:-}
	all_installed=1
	for f in $FILES; do
		case "$(state_of "$f")" in
			installed) ;;
			before|installed-old) all_installed=0 ;;
			pristine) die "$f is the pristine v2.92 file; re-apply the user's GUI modifications first (Venus2.9.2-patch), then run install again" ;;
			unknown) die "$f has hash $(sha "$QML/$f"), neither the expected base nor a known build; nothing touched" ;;
		esac
	done
	[ $all_installed = 1 ] && { log "already installed"; return 0; }
	for f in $FILES; do
		[ "$(sha "$HERE/$f")" = "$(var INSTALLED "$f")" ] || die "shipped $f hash mismatch"
	done
	mkdir -p "$BACKUP"
	for f in $FILES; do
		b="$BACKUP/$f$SUFFIX"
		if [ "$(state_of "$f")" = "before" ] && [ ! -f "$b" ]; then
			cp -p "$QML/$f" "$b" || die "backup of $f failed"
		fi
		[ -f "$b" ] || die "no backup for $f at $b and the installed file is not the base version; refusing"
		[ "$(sha "$b")" = "$(var BEFORE "$f")" ] || die "backup $b hash mismatch"
		log "backup verified: $b"
	done
	for f in $FILES; do
		cp "$HERE/$f" "$QML/$f.new" && mv "$QML/$f.new" "$QML/$f" || die "install of $f failed"
		chmod 644 "$QML/$f"
		[ "$(state_of "$f")" = "installed" ] || die "post-install state of $f is $(state_of "$f")"
	done
	{
		echo "installed=$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
		for f in $FILES; do echo "$f=$(var INSTALLED "$f")"; echo "$f.before=$(var BEFORE "$f")"; done
		echo "backup=$BACKUP/*$SUFFIX"
	} > "$MANIFEST"
	log "tile files installed"
	[ "$norestart" = "--no-restart" ] || restart_gui
}

do_rollback() {
	for f in $FILES; do
		b="$BACKUP/$f$SUFFIX"
		[ -f "$b" ] || die "no backup at $b"
		[ "$(sha "$b")" = "$(var BEFORE "$f")" ] || die "backup $b hash mismatch, refusing to restore"
	done
	for f in $FILES; do
		cp "$BACKUP/$f$SUFFIX" "$QML/$f.new" && mv "$QML/$f.new" "$QML/$f" || die "restore of $f failed"
		chmod 644 "$QML/$f"
		[ "$(state_of "$f")" = "before" ] || die "post-rollback state of $f is $(state_of "$f")"
	done
	echo "rolledback=$(date -u '+%Y-%m-%dT%H:%M:%SZ')" >> "$MANIFEST"
	log "pre-tile files restored"
	restart_gui
}

do_guard() {
	s1=$(state_of HubData.qml); s2=$(state_of OverviewHub.qml)
	if [ "$s1" = installed ] && [ "$s2" = installed ]; then log "guard: installed, nothing to do"
	elif { [ "$s1" = before ] || [ "$s1" = installed-old ]; } && { [ "$s2" = before ] || [ "$s2" = installed-old ]; }; then
		log "guard: base versions found (rootfs replaced?), re-installing"; do_install
	else log "guard: HubData.qml=$s1 OverviewHub.qml=$s2; not touched"; fi
}

case "${1:-}" in
	install)  do_install "${2:-}" ;;
	check)    do_check ;;
	rollback) do_rollback ;;
	guard)    do_guard ;;
	*) echo "usage: $0 install [--no-restart] | check | rollback | guard"; exit 1 ;;
esac
