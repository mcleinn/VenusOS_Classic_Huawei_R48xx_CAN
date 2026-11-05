"""Synchronous simulator of the chargers' add-on control board ("front-end"), ported from
the user's rsh harness (`rs485harness/simulators/huawei_frontend_sim.py`) to the driver's
CanFrame / VirtualLink and a fake clock, like psu_sim.py.

Measured behaviour it reproduces (harness docs/CHARGER-HUAWEI-UNITS.md 3.3 and the Cerbo
capture of 2026-11-01): every `period_s` (0.62 s) a status request to its module address,
then its writes to 0x80 (default voltage, default current raw, on-line voltage twice,
standby, on-line current raw), each waiting up to `ack_timeout_s` for the protocol ack;
every `cfg_period_s` (3 s) the 0x81 register-0x13 broadcast; voltages encoded V x 1023.6.
Optional quirks: `disguised_standby` (the standby payload once under the 0x50 info id, seen
2026-10-25), `info_request` (the board alone on the Cerbo also sent 0x108150FE each cycle).
`last_status` is what the board's screen would show (the last complete burst it received).
"""
from huawei import protocol as P
from huawei.canbus import CanFrame

BOARD_VOLT_SCALE = 1023.6          # measured: 48.0 V -> raw 0xBFEE


def enc_v(volts):
    return int(round(volts * BOARD_VOLT_SCALE))


class FrontEndSim(object):
    def __init__(self, link, clock, addr=1, period_s=0.62, cfg_period_s=3.0, ack_timeout_s=0.3,
                 default_v=50.0, default_i_raw=210, online_v=48.0, online_i_raw=70, standby=0,
                 disguised_standby=False, info_request=False):
        self.link = link
        self.clock = clock
        self.addr = addr
        self.period_s = period_s
        self.cfg_period_s = cfg_period_s
        self.ack_timeout_s = ack_timeout_s
        self.disguised_standby = disguised_standby
        self.info_request = info_request
        self.writes = [
            ('default_voltage', bytes([1, 0x01, 0, 0]) + enc_v(default_v).to_bytes(4, 'big')),
            ('default_current', bytes([1, 0x04, 0, 0]) + int(default_i_raw).to_bytes(4, 'big')),
            ('online_voltage', bytes([1, 0x00, 0, 0]) + enc_v(online_v).to_bytes(4, 'big')),
            ('online_voltage', bytes([1, 0x00, 0, 0]) + enc_v(online_v).to_bytes(4, 'big')),
            ('standby', bytes([1, 0x32, 0, 1 if standby else 0, 0, 0, 0, 0])),
            ('online_current', bytes([1, 0x03, 0, 0]) + int(online_i_raw).to_bytes(4, 'big')),
        ]
        self.stats = {'cycles': 0, 'requests': 0, 'writes': 0, 'acks_ok': 0, 'acks_error': 0,
                      'acks_missing': 0, 'cfg_frames': 0, 'cfg_acks': 0, 'status_frames': 0,
                      'bursts_complete': 0, 'unsolicited': 0, 'frames_rx': 0, 'acks_unexpected': 0,
                      'requests_seen': 0, 'foreign_addr': 0, 'tx': 0}
        self.last_status = {}            # reg -> raw of the last complete burst
        self.seen_ids = set()
        self._burst = {}
        t = clock()
        self._next_cycle = t
        self._next_cfg = t + 0.1
        self._queue = []
        self._pending = None             # (name, reg, deadline)
        self._disguised_sent = False

    # ----------------------------------------------------------------- helpers
    def _tx(self, can_id, data):
        self.stats['tx'] += 1
        self.link.send(CanFrame(can_id, data, ext=True, ts=self.clock()))

    def _send_next(self, now):
        if self._pending is not None or not self._queue:
            return
        name, data = self._queue.pop(0)
        self.stats['writes'] += 1
        self._tx(P.request_id(P.CMD_SET, self.addr), data)
        self._pending = (name, data[1], now + self.ack_timeout_s)

    def displayed(self):
        """What the screen shows: output V / A / W and temperatures from the last burst."""
        s = self.last_status
        if not s:
            return None
        return {'voltage_v': s.get(0x75, 0) / 1024.0, 'current_a': s.get(0x81, 0) / 1024.0,
                'power_w': s.get(0x73, 0) / 1024.0, 'in_temp_c': s.get(0x80, 0) / 1024.0,
                'out_temp_c': s.get(0x7F, 0) / 1024.0}

    # ------------------------------------------------------------------ driving
    def tick(self, now=None):
        now = self.clock() if now is None else now
        if self._pending is not None and now >= self._pending[2]:
            self.stats['acks_missing'] += 1
            self._pending = None
            self._send_next(now)
        if self.disguised_standby and not self._disguised_sent:
            self._disguised_sent = True
            self._tx(P.request_id(P.CMD_INFO, self.addr), bytes([1, 0x32, 0, 0, 0, 0, 0, 0]))
        if now >= self._next_cycle:
            self._next_cycle += self.period_s
            self.stats['cycles'] += 1
            self.stats['requests'] += 1
            self._tx(P.request_id(P.CMD_DATA, self.addr), bytes(8))
            if self.info_request:
                self._tx(P.request_id(P.CMD_INFO, self.addr), bytes(8))
            self._queue = list(self.writes)
            self._send_next(now)
        if now >= self._next_cfg:
            self._next_cfg += self.cfg_period_s
            self.stats['cfg_frames'] += 1
            self._tx(P.request_id(P.CMD_CFG, 0), bytes([1, 0x13, 0, 1, 0, 0, 0, 0]))

    def handle(self, frame):
        if frame.err or frame.echo or not frame.ext:
            return
        self.stats['frames_rx'] += 1
        self.seen_ids.add(frame.can_id)
        dec = P.decode(frame)
        if dec is None:
            return
        h = P.unpack_id(frame.can_id)
        if h['addr'] not in (0, self.addr):
            self.stats['foreign_addr'] += 1
            return
        kind = dec['kind']
        if kind == 'status':
            self.stats['status_frames'] += 1
            self._burst[dec['reg']] = dec['raw']
            if not dec['cont']:
                self.last_status = dict(self._burst)
                self._burst = {}
                self.stats['bursts_complete'] += 1
        elif kind == 'ack':
            if dec.get('reg') == 0x13:
                self.stats['cfg_acks'] += 1
            elif self._pending is not None and dec.get('reg') == self._pending[1]:
                self.stats['acks_ok' if dec.get('ok') else 'acks_error'] += 1
                self._pending = None
                self._send_next(self.clock())
            else:
                self.stats['acks_unexpected'] += 1
        elif kind == 'unsolicited':
            self.stats['unsolicited'] += 1
        elif kind in ('status_request', 'info_request', 'elabel_request', 'get', 'set'):
            self.stats['requests_seen'] += 1
