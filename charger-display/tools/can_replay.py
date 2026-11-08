"""Replay recorded Huawei CAN frames into the display over USB serial and compare the display's
decoder with the Venus driver's reference decoder (src/driver/huawei/protocol.py).

Usage: python tools/can_replay.py COM11 capture.jsonl [capture2.jsonl ...]

Capture format: the rsh harness jsonl (one JSON object per line, keys id, hex, ext, dir, kind).
Frames of both directions are injected, exactly as the listener would hear them on the bus.
"""
import json, re, sys, time
from pathlib import Path

import serial

DRIVER = Path(r"C:\Dev-Free\CerboGX_R4850G2_CAN\src\driver")
sys.path.insert(0, str(DRIVER))
from huawei import protocol as P  # noqa: E402


class Frame:
    def __init__(self, can_id, data):
        self.can_id = can_id; self.data = data; self.ext = True; self.err = False; self.echo = False; self.ts = 0


def load_frames(paths):
    frames = []
    for path in paths:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("kind") not in ("can", "status", "ack", "set", "status_request", "unsolicited", "info", "get_reply", "get") and "id" not in r:
                continue
            if "id" not in r or not r.get("ext", True):
                continue
            frames.append((int(r["id"], 16), bytes.fromhex(r.get("hex", "") or "")))
    return frames


def expected(frames):
    """Run the reference decoder over the frames and keep the last value per field."""
    e = {}
    for can_id, data in frames:
        d = P.decode(Frame(can_id, data))
        if not d:
            continue
        k = d.get("kind")
        if k in ("status", "get_reply"):
            reg = d["reg"]; v = d.get("value")
            m = {0x70: "ac_w", 0x71: "ac_f", 0x72: "ac_a", 0x73: "out_w", 0x75: "out_v", 0x85: "out_v", 0x76: "limit",
                 0x78: "ac_v", 0x7F: "t_out", 0x80: "t_in", 0x81: "out_a", 0x88: "rated"}
            if reg in m and v is not None:
                if reg == 0x75 and d["raw"] == 0:
                    continue
                e[m[reg]] = float(v)
            if reg == 0x83:
                e["out_on"] = 1 if (d["raw"] >> 27) & 1 else 0
                e["ac_present"] = 0 if (d["raw"] >> 29) & 1 else 1   # display uses bit 29 of the bytes-4-7 word
        elif k == "set":
            if d.get("name") == "online_voltage": e["set_v"] = d["raw"] / 1024.0
            elif d.get("name") == "online_current": e["set_ratio"] = d["raw"] / 1024.0
            elif d.get("name") == "standby": e["standby"] = int(d["raw"])
        elif k == "ack":
            e["ack_ok"] = 1 if d["ok"] else 0
        elif k == "unsolicited":
            e["load"] = d["load_ratio"]
    return e


def main():
    port, paths = sys.argv[1], sys.argv[2:]
    frames = load_frames(paths)
    print(f"{len(frames)} frames from {len(paths)} file(s)")
    exp = expected(frames)
    with serial.Serial(port, 115200, timeout=2) as s:
        time.sleep(0.5)
        s.reset_input_buffer()
        for can_id, data in frames:
            s.write(f"can {can_id:08X} {data.hex()}\n".encode())
            time.sleep(0.004)
        time.sleep(0.5)
        s.write(b"dump\n")
        deadline = time.time() + 5
        got = None
        while time.time() < deadline:
            line = s.readline().decode("utf-8", "replace").strip()
            if line.startswith("dump "):
                got = line; break
    if not got:
        print("no dump line from the device"); sys.exit(1)
    dev = dict(kv.split("=", 1) for kv in got[5:].split())
    print(got)
    bad = 0
    for key, want in sorted(exp.items()):
        have = dev.get(key)
        if have is None:
            print(f"  {key:10s} missing on device"); bad += 1; continue
        hv = float(have)
        ok = abs(hv - float(want)) <= max(0.002, abs(float(want)) * 0.001)
        print(f"  {key:10s} device {hv:10.4f}   reference {float(want):10.4f}   {'ok' if ok else 'MISMATCH'}")
        bad += 0 if ok else 1
    print(f"frames counted by device: {dev.get('frames')} (sent {len(frames)}, Huawei-filtered by the device)")
    print("RESULT:", "all match" if bad == 0 else f"{bad} mismatch(es)")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
