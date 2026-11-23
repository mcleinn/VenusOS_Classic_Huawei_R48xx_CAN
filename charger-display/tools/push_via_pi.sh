#!/bin/sh
# Push a display firmware over Wi-Fi with the Venus Pi as jump host (the displays sit behind the Pi's
# access point, NATed to the LAN, so curl must run on the Pi).
# Usage: tools/push_via_pi.sh d1|d2 [--no-build]
#   d1 = board mac1, d2 = board mac2 (secrets.ini)
# Needs: ssh alias "cerbo" (key installed on the Pi), secrets.ini with ota password, PlatformIO.
set -eu
cd "$(dirname "$0")/.."
env="${1:?d1 or d2}"
case "$env" in d1) key=mac1;; d2) key=mac2;; *) echo "d1 or d2"; exit 2;; esac
mac=$(grep -oE "^$key = .*" secrets.ini | cut -d" " -f3)
[ -n "$mac" ] || { echo "$key missing in secrets.ini"; exit 2; }
pw=$(grep -oE '^password = .*' secrets.ini | cut -d' ' -f3)
if [ "${2:-}" != "--no-build" ]; then
	PYTHONIOENCODING=utf-8 "$USERPROFILE/.platformio/penv/Scripts/pio.exe" run -e "$env" >/dev/null || { echo "build failed"; exit 1; }
fi
bin=".pio/build/$env/firmware.bin"
ip=$(ssh -o BatchMode=yes cerbo "ip neigh show dev tether | grep -i '$mac' | awk '{print \$1}' | head -n 1")
[ -n "$ip" ] || { echo "board $mac not seen on the Pi's access point (ip neigh show dev tether)"; exit 1; }
echo "board $mac is at $ip on the Pi's access point; copying $(stat -c %s "$bin") bytes to the Pi"
scp -q "$bin" cerbo:/tmp/cd-$env.bin
ssh -o BatchMode=yes cerbo "curl -s -S --max-time 120 -u 'ota:$pw' -F 'firmware=@/tmp/cd-$env.bin' http://$ip/update; echo; sleep 35; curl -s -m 10 -u 'ota:$pw' http://$ip/status; echo; rm -f /tmp/cd-$env.bin"
