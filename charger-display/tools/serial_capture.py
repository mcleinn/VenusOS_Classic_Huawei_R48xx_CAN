"""Capture serial output from the display for N seconds, masking credential-looking lines.
Usage: python tools/serial_capture.py [port] [seconds]"""
import re, sys, time
import serial

port_name = sys.argv[1] if len(sys.argv) > 1 else "COM11"
seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 45
port = None
for _ in range(40):
    try:
        port = serial.Serial(port_name, 115200, timeout=0.5)
        break
    except Exception:
        time.sleep(0.5)
if not port:
    print(f"could not open {port_name}")
    sys.exit(1)
print(f"--- serial {port_name}, {seconds:.0f} s ---", flush=True)
t_end = time.time() + seconds
buf = b""
while time.time() < t_end:
    buf += port.read(4096)
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        s = line.decode("utf-8", "replace").rstrip()
        if re.search(r"(?i)ssid|passw|psk", s):
            s = "[credential line masked]"
        s = re.sub(r"\x1b\[[0-9;]*m", "", s)   # strip ANSI colours (IDF logs)
        s = s.encode("ascii", "replace").decode()
        try:
            print(s, flush=True)
        except OSError:        # downstream pipe closed (e.g. | head)
            port.close()
            sys.exit(0)
port.close()
