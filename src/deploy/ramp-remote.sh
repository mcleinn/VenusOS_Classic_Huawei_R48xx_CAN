#!/bin/sh
# ramp-remote.sh: drive huawei-ramp.py from another ssh session (stdin via FIFO, output to a file).
#   ramp-remote.sh start --unit 1 [--dry-run] [more huawei-ramp options]
#   ramp-remote.sh send ""          # Enter = next step;  send "c 11.8" / "v 4.2 3.8" / "n text" / "z" / "q"
#   ramp-remote.sh tail [N]         # last N lines of the output (default 12)
#   ramp-remote.sh status
#   ramp-remote.sh stop             # q, then SIGTERM after 15 s if still running (both end in a 0 A write)
# Nothing started here keeps the caller's ssh session open: every background process has its
# stdin/stdout/stderr redirected. Writing to the FIFO is only attempted while the tool is alive
# (an open for writing on a FIFO without a reader blocks forever).
FIFO=/tmp/ramp.in
OUT=/tmp/ramp.out
PIDF=/tmp/ramp.pid
HOLDF=/tmp/ramp.holder

alive() { [ -f "$PIDF" ] && kill -0 "$(cat $PIDF)" 2>/dev/null; }

case "$1" in
  start)
    shift
    if alive; then echo "already running (pid $(cat $PIDF))"; exit 1; fi
    rm -f "$FIFO"; mkfifo "$FIFO"
    : > "$OUT"
    # holder keeps the FIFO's write end open so the tool never sees EOF
    sleep 86400 > "$FIFO" 2>/dev/null < /dev/null &
    echo $! > "$HOLDF"
    setsid python3 /data/huawei/huawei-ramp.py --idle-timeout 300 "$@" < "$FIFO" > "$OUT" 2>&1 &
    echo $! > "$PIDF"
    sleep 4
    tail -n 12 "$OUT"
    ;;
  send)
    if ! alive; then echo "tool not running"; tail -n 3 "$OUT"; exit 1; fi
    printf '%s\n' "$2" > "$FIFO"
    sleep 1.5
    tail -n 4 "$OUT"
    ;;
  tail)
    tail -n "${2:-12}" "$OUT"
    ;;
  status)
    if alive; then echo "running (pid $(cat $PIDF))"; else echo "not running"; fi
    ls -la /data/huawei/bench 2>/dev/null || echo "no bench marker"
    ;;
  stop)
    if alive; then printf 'q\n' > "$FIFO"; fi
    n=0
    while alive && [ $n -lt 15 ]; do sleep 1; n=$((n+1)); done
    if alive; then kill -TERM "$(cat $PIDF)"; sleep 8; fi
    [ -f "$HOLDF" ] && kill "$(cat $HOLDF)" 2>/dev/null
    rm -f "$PIDF" "$HOLDF"
    tail -n 6 "$OUT"
    ;;
  *)
    echo "usage: $0 start|send|tail|status|stop"; exit 2;;
esac
