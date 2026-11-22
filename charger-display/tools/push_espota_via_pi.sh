#!/bin/sh
# Push a display firmware with the espota protocol (ArduinoOTA, UDP 3232) from the Venus Pi.
# Needed for a display whose firmware predates the HTTP /update endpoint (display 2 ran the
# 2026-11-08 build until 2026-11-22). espota makes the board connect BACK to the sender, which the
# notebook's firewall blocks; the Pi on the same LAN has no such rule. After this the display has
# /update and tools/push_via_pi.sh or the d*_http envs do the job.
# Usage: tools/push_espota_via_pi.sh d1|d2 <display-ip> [--no-build]
set -eu
cd "$(dirname "$0")/.."
env="${1:?d1 or d2}"; ip="${2:?display ip}"
pw=$(grep -oE '^password = .*' secrets.ini | cut -d' ' -f3)
if [ "${3:-}" != "--no-build" ]; then
	PYTHONIOENCODING=utf-8 "$USERPROFILE/.platformio/penv/Scripts/pio.exe" run -e "$env" >/dev/null || { echo "build failed"; exit 1; }
fi
bin=".pio/build/$env/firmware.bin"
espota="$USERPROFILE/.platformio/packages/framework-arduinoespressif32/tools/espota.py"
echo "copying $(stat -c %s "$bin") bytes and espota.py to the Pi"
scp -q "$bin" cerbo:/tmp/cd-$env.bin
scp -q "$espota" cerbo:/tmp/espota.py
# -I: the Pi's own LAN address the board must call back; -P: the port the Pi listens on
ssh -o BatchMode=yes cerbo "python3 /tmp/espota.py -i $ip -I 192.168.1.14 -p 3232 -P 3232 -a '$pw' -f /tmp/cd-$env.bin -r 2>&1 | tail -n 5; rm -f /tmp/cd-$env.bin"
echo "waiting 40 s for the reboot"; sleep 40
curl -s -m 10 -u "ota:$pw" "http://$ip/status"; echo
