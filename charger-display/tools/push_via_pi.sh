#!/bin/sh
# Push a display firmware over Wi-Fi with the Venus Pi as jump host (the displays sit behind the Pi's
# access point "charger-net", NATed to the LAN, so curl must run on the Pi).
# Usage: tools/push_via_pi.sh d1|d2 [--no-build]
#   d1 = board 28:84:85:8d:4a:0c (display at charger "Huawei 1"), d2 = board ..4a:d8 ("Huawei 2")
# Needs: ssh alias "cerbo" (key installed on the Pi), secrets.ini with ota password, PlatformIO.
set -eu
cd "$(dirname "$0")/.."
env="${1:?d1 or d2}"
case "$env" in d1) mac="28:84:85:8d:4a:0c";; d2) mac="28:84:85:8d:4a:d8";; *) echo "d1 or d2"; exit 2;; esac
pw=$(grep -oE '^password = .*' secrets.ini | cut -d' ' -f3)
if [ "${2:-}" != "--no-build" ]; then
	PYTHONIOENCODING=utf-8 "$USERPROFILE/.platformio/penv/Scripts/pio.exe" run -e "$env" >/dev/null || { echo "build failed"; exit 1; }
fi
bin=".pio/build/$env/firmware.bin"
ip=$(ssh -o BatchMode=yes cerbo "ip neigh show dev tether | grep -i '$mac' | awk '{print \$1}' | head -n 1")
[ -n "$ip" ] || { echo "board $mac not seen on the Pi's access point (ip neigh show dev tether)"; exit 1; }
echo "board $mac is at $ip on charger-net; copying $(stat -c %s "$bin") bytes to the Pi"
scp -q "$bin" cerbo:/tmp/cd-$env.bin
ssh -o BatchMode=yes cerbo "curl -s -S --max-time 120 -u 'ota:$pw' -F 'firmware=@/tmp/cd-$env.bin' http://$ip/update; echo; sleep 35; curl -s -m 10 -u 'ota:$pw' http://$ip/status; echo; rm -f /tmp/cd-$env.bin"
