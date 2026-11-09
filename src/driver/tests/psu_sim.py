"""Synchronous Huawei R48xx PSU simulator for the driver tests, ported from the user's
rsh harness (`rs485harness/simulators/huawei_psu_sim.py`) without threads: the test
calls `handle(frame)` for every frame it takes off the virtual bus and `tick(now)` with a
fake clock. Same protocol behaviour and the same Phase B fidelity switches:
polls_refresh_timeout, reject_zero_default_current, on_ac_loss, power_cycle(), set_addr(),
plus voltage_in_0x85 (R4875G5: 0x75 reads 0, GET 0x85 carries the voltage) and an e-label
reply with the model name.
"""
from huawei import protocol as P
from huawei.canbus import CanFrame

STATUS_ORDER = [0x0E, 0x70, 0x71, 0x72, 0x73, 0x74, 0x75, 0x76, 0x78, 0x7F, 0x80, 0x81, 0x82]
NEG_PHASE1 = (3, 0.13)
NEG_PHASE2 = (8, 0.35)


class PsuSim(object):
    def __init__(self, link, clock, addr=1, serial_hex='0000000000A1', nominal_a=52.0, scale='ratio1024',
                 battery_v=None, load_ohm=None, timeout_s=P.ONLINE_TIMEOUT_S, unsol_period_s=0.377,
                 default_v=53.5, default_current_a=None, polls_refresh_timeout=True,
                 reject_zero_default_current=False, on_ac_loss='reset', negotiate_on_start=False,
                 voltage_in_0x85=False, model='R4850G2', standby=False):
        self.link = link
        self.clock = clock
        self.addr = addr
        self.serial_hex = serial_hex.upper()
        self.nominal_a = nominal_a
        self.scale = scale
        self.battery_v = battery_v
        self.load_ohm = load_ohm
        self.timeout_s = timeout_s
        self.unsol_period_s = unsol_period_s
        self.polls_refresh_timeout = polls_refresh_timeout
        self.reject_zero_default_current = reject_zero_default_current
        self.on_ac_loss = on_ac_loss
        self.voltage_in_0x85 = voltage_in_0x85
        self.model = model
        self.drop_acks = False                  # fidelity: unit answers writes with no ack at all
        self.reject_online_current = False      # fidelity: unit answers 0x03 writes with 0x21
        self.wind_down_s = 0.0                  # fidelity: a LOWER limit takes effect linearly over this long (R4875G5 ~20 s)
        self._eff_limit = None
        self._down_from = None
        self._down_t = None
        sc = P.CURRENT_SCALES[scale]
        self.default_v = default_v
        self.default_i_raw = sc.encode(nominal_a if default_current_a is None else default_current_a, nominal_a)
        self.online_v = default_v
        self.online_i_raw = self.default_i_raw
        self.online = False
        self.standby = standby          # True = output stage off (as after a cold start, 2026-11-01)
        self.ac_on = True
        self.last_cmd_t = -1e9
        self._last_unsol = -1e9
        self.hours = 1234
        self.stats = {'requests': 0, 'sets': 0, 'set_errors': 0, 'unsol': 0, 'tx': 0, 'reverts': 0,
                      'info': 0, 'gets': 0, 'elabel': 0}
        self.sets = []
        self._neg_queue = []
        if negotiate_on_start:
            self._schedule_negotiation(True)

    # ------------------------------------------------------------- model
    def current_limit_a(self):
        raw = self.online_i_raw if self.online else self.default_i_raw
        return min(P.CURRENT_SCALES[self.scale].decode(raw, self.nominal_a), self.nominal_a * 1.17)

    def setpoint_v(self):
        return self.online_v if self.online else self.default_v

    def _effective_limit(self, limit):
        """The limit the output loop is at: follows a rise at once, a drop over wind_down_s."""
        now = self.clock()
        if self._eff_limit is None or not self.wind_down_s:
            self._eff_limit = limit
            return limit
        if limit >= self._eff_limit:
            self._eff_limit, self._down_from = limit, None
            return limit
        if self._down_from is None or limit < self._eff_limit and self._down_from < self._eff_limit:
            self._down_from, self._down_t = self._eff_limit, now
        eff = max(limit, self._down_from - (now - self._down_t) * self._down_from / self.wind_down_s)
        self._eff_limit = eff
        return eff

    def measure(self):
        if self.standby or not self.ac_on:
            return 0.0, 0.0
        v = self.setpoint_v()
        limit = self._effective_limit(self.current_limit_a())
        if self.load_ohm:
            i = v / self.load_ohm
            if i > limit:
                i = limit
                v = i * self.load_ohm
        elif self.battery_v is not None:
            r = 0.05
            i = max(0.0, (v - self.battery_v) / r)
            if i > limit:
                i = limit
                v = self.battery_v + i * r
        else:
            i = 0.0
        return round(v, 3), round(i, 3)

    def _go_offline(self):
        self.online = False
        self.online_v, self.online_i_raw = self.default_v, self.default_i_raw

    def power_cycle(self):
        self.standby = False
        self.last_cmd_t = -1e9
        self.ac_on = True
        self._go_offline()

    def set_ac(self, on):
        if on == self.ac_on:
            return
        self.ac_on = on
        if not on and self.on_ac_loss == 'reset':
            self._go_offline()

    def set_addr(self, addr):
        self.addr = addr
        self._schedule_negotiation(False)

    def _schedule_negotiation(self, power_up):
        serial = bytes.fromhex(self.serial_hex)
        t = self.clock()
        if power_up:
            n, gap = NEG_PHASE1
            for i in range(n):
                self._neg_queue.append((t + i * gap, P.pack_id(P.PROTO_PSU, 0, P.CMD_ADDR, False),
                                        bytes([0x30, 0x01]) + serial))
            t += n * gap
        n, gap = NEG_PHASE2
        for i in range(n):
            self._neg_queue.append((t + i * gap, P.pack_id(P.PROTO_PSU, self.addr, P.CMD_ADDR, False),
                                    bytes([0x30, 0x02]) + serial))

    # ---------------------------------------------------------------- io
    def _tx(self, can_id, data):
        self.stats['tx'] += 1
        self.link.send(CanFrame(can_id, data, ext=True, ts=self.clock()))

    def _status_frames(self):
        v, i = self.measure()
        limit_ratio = self.current_limit_a() / self.nominal_a
        p_out = v * i
        eff = 0.95 if p_out > 100 else 0.80
        vals = {0x0E: self.hours, 0x70: p_out / eff, 0x71: 50.0, 0x72: (p_out / eff) / 230.0,
                0x73: p_out, 0x74: eff, 0x75: v, 0x76: limit_ratio, 0x78: 230.0 if self.ac_on else 0.0,
                0x7F: 35.0 + p_out / 150, 0x80: 28.0, 0x81: i, 0x82: i}
        if self.voltage_in_0x85:
            vals[0x85] = vals[0x75]
            vals[0x75] = 0.0
        out = []
        for reg in STATUS_ORDER:
            raw = int(round(vals[reg] * (1 if reg == 0x0E else 1024)))
            out.append((P.reply_id(P.CMD_DATA, self.addr, True), bytes([0x01, reg, 0, 0]) + raw.to_bytes(4, 'big')))
        status = (1 << 9) if self.standby else 0
        if not self.ac_on:
            status |= 1 << 29                       # input-power-failure, as the R4875G1 reports it
        data = bytes([0x01, 0x83, 0, 0]) + status.to_bytes(4, 'big')
        out.append((P.reply_id(P.CMD_DATA, self.addr, False), data))
        return out

    def _info_frames(self):
        nominal_raw = int(self.nominal_a * 2) & 0x3FF
        char = bytes([0, 1, 0, 0]) + ((0x40 << 24) | (nominal_raw << 16) | 0x0E27).to_bytes(4, 'big')
        frames = [char, bytes([0, 2]) + bytes.fromhex(self.serial_hex), bytes([0, 3]) + b'210231',
                  bytes([0, 4]) + b'SIM001', bytes([0, 5, 0x01, 0x00, 0x03, 0x02, 0x02, 0x01]),
                  bytes([0, 6, 0x01, 0x01, 0, 0, 0, 0])]
        return [(P.reply_id(P.CMD_INFO, self.addr, i < len(frames) - 1), f) for i, f in enumerate(frames)]

    def _elabel_frames(self):
        text = ('/$[ArchivesInfo Version]\r\nBoardType=%s\r\nModel=%s\r\nDescription=Function Module,%s\r\n'
                % (self.model, self.model, self.model)).encode('ascii')
        chunks = [text[i:i + 6] for i in range(0, len(text), 6)]
        return [(P.reply_id(P.CMD_ELABEL, self.addr, i < len(chunks) - 1),
                 bytes([0, i + 1]) + c.ljust(6, b'\0')) for i, c in enumerate(chunks)]

    def _handle_set(self, data):
        d = bytes(data).ljust(8, b'\0')
        name = P.SET_REG_BY_NUM.get(d[1])
        raw = int.from_bytes(d[4:8], 'big')
        err = False
        if name in ('online_voltage', 'default_voltage', 'ovp_voltage'):
            v = raw / 1024.0
            err = not P.voltage_in_range(name, v)
            if not err:
                setattr(self, {'online_voltage': 'online_v', 'default_voltage': 'default_v',
                               'ovp_voltage': 'ovp_v'}[name], v)
        elif name in ('online_current', 'default_current'):
            err = raw > {'ratio1024': 1250, 'x20': 1250, 'x30': 1875}[self.scale]
            if name == 'online_current' and self.reject_online_current:
                err = True
            if name == 'default_current' and raw == 0 and self.reject_zero_default_current:
                err = True
            if not err:
                if name == 'online_current':
                    self.online_i_raw = raw
                else:
                    self.default_i_raw = raw
        elif name == 'standby':
            self.standby = bool(d[3])
        elif name == 'fan_max':
            pass
        else:
            err = True
        self.stats['sets'] += 1
        self.sets.append((name, raw, err))
        if err:
            self.stats['set_errors'] += 1
        ack = (bytes([P.ACK_ERROR_BYTE]) if err else bytes([d[0]])) + d[1:]
        if not self.drop_acks:
            self._tx(P.reply_id(P.CMD_SET, self.addr, False), ack)

    def handle(self, frame):
        if frame.err or frame.echo or not frame.ext:
            return
        h = P.unpack_id(frame.can_id)
        if not P.is_huawei_id(frame.can_id) or not h['to_psu'] or h['cmd'] == P.CMD_UNSOL:
            return
        if h['addr'] not in (0, self.addr):
            return
        if h['cmd'] == P.CMD_SET or self.polls_refresh_timeout:
            self.last_cmd_t = self.clock()
            if not self.online:
                self.online = True
                self.online_v, self.online_i_raw = self.default_v, self.default_i_raw
        if h['cmd'] == P.CMD_DATA:
            self.stats['requests'] += 1
            for cid, data in self._status_frames():
                self._tx(cid, data)
        elif h['cmd'] == P.CMD_INFO:
            self.stats['info'] += 1
            for cid, data in self._info_frames():
                self._tx(cid, data)
        elif h['cmd'] == P.CMD_SET:
            self._handle_set(frame.data)
        elif h['cmd'] == P.CMD_GET:
            self.stats['gets'] += 1
            reg = frame.data[1] if len(frame.data) > 1 else 0
            answered = False
            for cid, data in self._status_frames():
                if data[1] == reg:
                    self._tx(P.reply_id(P.CMD_GET, self.addr, False), data)
                    answered = True
            if not answered and reg == 0x85 and self.voltage_in_0x85:
                v, _ = self.measure()
                self._tx(P.reply_id(P.CMD_GET, self.addr, False),
                         bytes([0x01, 0x85, 0, 0]) + int(round(v * 1024)).to_bytes(4, 'big'))
            elif not answered and reg == 0x88:
                self._tx(P.reply_id(P.CMD_GET, self.addr, False),
                         bytes([0x01, 0x88]) + int(self.nominal_a * 100).to_bytes(2, 'big') + bytes(4))
        elif h['cmd'] == P.CMD_ELABEL:
            self.stats['elabel'] += 1
            for cid, data in self._elabel_frames():
                self._tx(cid, data)

    def tick(self, now=None):
        now = self.clock() if now is None else now
        if self.online and now - self.last_cmd_t > self.timeout_s:
            self.stats['reverts'] += 1
            self._go_offline()
        due = [q for q in self._neg_queue if q[0] <= now]
        self._neg_queue = [q for q in self._neg_queue if q[0] > now]
        for _, cid, data in due:
            self._tx(cid, data)
        if now - self._last_unsol >= self.unsol_period_s:
            self._last_unsol = now
            v, i = self.measure()
            load_raw = int(round(i / self.nominal_a * 1024))
            data = bytes([0, 1, 0, 1 if (self.standby or not self.ac_on) else 0, 0, 0]) + load_raw.to_bytes(2, 'big')
            self.stats['unsol'] += 1
            self._tx(P.unsolicited_id(self.addr), data)
