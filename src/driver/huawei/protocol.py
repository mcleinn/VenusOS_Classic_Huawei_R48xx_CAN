"""Huawei R48xx CAN codec for the Venus driver (R4875G5 units measured, R4850G2 sources).

Ported 2026-10-23 from the user's rsh harness (`rs485harness/protocols/huawei_r48xx/`,
ids.py + regs.py) and its source-cited notes (`docs/refs/huawei/NOTES.md`); updated
2026-11-01 with what the bench (harness docs/PHASE-B-RUNBOOK.md, 2026-10-25) and the
Cerbo's first contact (evidence_cerbo_usb_adapter_first_contact.txt) measured on unit 1,
an R4875G5, serial 708A85B00972. Pure functions: no state, no I/O.

VERIFICATION STATUS: `VERIFIED = False`. `MEASURED` lists what unit 1 confirmed,
`UNVERIFIED` what still rests on community sources. The flag stays False until the
current scale has been calibrated with a load (rsh huawei calibrate-current) and the
zero-current fallback has been measured with a load on both units. While VERIFIED is
False the engine never transmits a 0x80 set frame (see control.py).

Identifier layout (29 bit), confirmed identical in five independent implementations:
    bits 28-23 protocol (0x21 controller<->PSU, 0x20 PSU<->PSU) | 22-16 address
    (0 broadcast, 1..) | 15-8 command | 7 to_psu | 6-2 group (0x1F) | 1 sw_addr | 0 cont
"""
# 2026-11-04: True since the first load test of 2026-11-03 (evidence_first_load_test_2026-11-03.txt):
# unit 2 (R4875G1) obeyed ratio1024 x nominal from 5 to 40 A with the clamp within 0.6 A of 0x81, every
# write acked in 4-82 ms, no overshoot, joints and temperatures measured. Unit 1 (R4875G5) obeys the
# same scale (raw 15 -> 1.0 A) but caps itself at ~1.65 A for reasons inside the unit; it is excluded
# from control by configuration (driver.json "excluded"), not by this flag.
VERIFIED = True
MEASURED = [
    'unit 2 = R4875G1 E4308598A003, 75.0 A (GET 0x88); 0x75 carries the output voltage on this model',
    'R4875G1: 0x76 = on-line raw/1024 while polled (68 -> 0.066), stored default after fallback (0 after B3.0 2026-11-02)',
    'R4875G1: on-line 0 A with no load keeps the output regulated at the set voltage; after 60-90 s silence with default 0 A the output is off and the terminals bleed',
    'two unterminated stubs from the Cerbo sockets (terminator removed) = bit-stuffing error storm, no frame for two hours; terminate at the charger ends',
    'unit 1 = R4875G5 (e-label), nominal 69.0 A from info 0x0001 = (raw >> 16 & 0x3FF) / 2',
    'voltage scale V x 1024: set 51.0 -> 0x85 reads 50.966, 41.0..52.8 V all reached within 2 s',
    'register 0x85 carries the output voltage on the R4875G5; 0x75 reads 0 in every burst',
    '0x76 is NOT a readback of the on-line current limit on the R4875G5 (210 / 127 / 0 seen)',
    'unsolicited byte 3 = 1 was seen with 49.9 V on the terminals: not "output disabled" here',
    'ack byte 0 = 0x01 on success for 0x00/0x01/0x03/0x04/0x32 writes',
    'persistent defaults 0x01/0x04 accept 52.4 V / raw 0; fallback to them after 75 s silence',
    'status polling alone keeps the unit on-line (B3.4): no fallback while polled',
    'R4875G5 2026-11-04: stored 52.0 V / 10 A survive a cold start; polled from boot the open output sits at 46.1 V with 0x76 = the stored ratio, the stored voltage applies after the no-command timeout',
    'module itself sends 0x108081FE 01 13 00 01 and 0x100011FE 00 02 every 3 s',
    'reply timing: request to first frame 2.4 / 6.1 / 8.1 ms, 14-frame burst in 26 ms',
    'cold start, no controller: output stage off (2.4 V residual); standby 0x32 only toggles 0x83 bit 9;',
    'the first NON-ZERO on-line current limit switches the output on (0x83 bit 27); 0 A cannot charge the caps',
    'while on-line 0x76 = on-line raw / 1024 (0.029 for raw 30); off-line = stored default (0 after B3.0)',
    'LOAD TEST 2026-11-03: unit 2 R4875G1 obeys ratio1024 x 75 A (clamp 5.0/10.0/15.1/20.2/30.3/40.2 A for 5/10/15/20/30/40 A)',
    'unit 1 R4875G5 obeys ratio1024 x 69 A (raw 15 -> 1.0 A) but caps at ~1.65 A whatever the limit (not fan, voltage, AC limit, restart)',
    'no fan tacho feedback (0x87 bytes 4-7 zero) -> module derates to ~10 % power; 0x77 power-available 0.10 -> 0.81-0.86 with the fan turning',
    'AC input current limit 0x09 works on both models (byte 3 flag, bytes 4-7 A x 1024; value persists); 0 A active stops the output; 16 A active stored on both',
    'R4875G5 winds down slowly after a lower limit (1.7 A still reported ~20 s after 1 A); R4875G1 settles within ~3 s',
    'set-frame acks 4-82 ms on both units; 0x82 GET of a status register answers within 1 s, config registers (0x09) answer byte0 0x21',
]
UNVERIFIED = [
    'accepted voltage ranges 41.0-58.6 V online, 48.0-58.4 V default (41.0-52.8 on-line measured)',
    'ack error marker byte 0 = 0x21 / low nibble 2 (never provoked)',
    'exact no-command timeout (<= 75 s measured; ~60 s per sources)',
    'status register 0x83 bit meanings (0x8000 and 0x08000004 seen, neither in a source)',
    'unsolicited bytes 6-7 = load ratio x 1024',
    'address negotiation 30 01 x3 / 30 02 x8 and lowest serial = address 1 (two units never on one bus)',
    'residual current after fallback with a load (B3.1 under load not done: driver killed while charging)',
    'what holds unit 1 (R4875G5) at ~1.65 A: AC cord / rear-connector pin comparison with unit 2 pending',
]

PROTO_CTL = 0x21
PROTO_PSU = 0x20
CMD_ADDR = 0x10
CMD_UNSOL = 0x11
CMD_DATA = 0x40
CMD_INFO = 0x50
CMD_SET = 0x80
CMD_CFG = 0x81
CMD_GET = 0x82
CMD_ELABEL = 0xD2
CMD_NAMES = {CMD_ADDR: 'addr-negotiation', CMD_UNSOL: 'unsolicited', CMD_DATA: 'data',
             CMD_INFO: 'info', CMD_SET: 'set', CMD_CFG: 'config', CMD_GET: 'get',
             CMD_ELABEL: 'elabel'}
GROUP_ALL = 0x1F
BITRATE = 125000
ONLINE_TIMEOUT_S = 60.0        # unverified, see UNVERIFIED
SCALE_1024 = 1024
ACK_ERROR_BYTE = 0x21


# ------------------------------------------------------------- identifiers
def pack_id(proto, addr, cmd, to_psu, cont=False, group=GROUP_ALL, sw_addr=True):
    return ((proto & 0x3F) << 23 | (addr & 0x7F) << 16 | (cmd & 0xFF) << 8 |
            int(bool(to_psu)) << 7 | (group & 0x1F) << 2 | int(bool(sw_addr)) << 1 | int(bool(cont)))


def unpack_id(can_id):
    """dict(proto, addr, cmd, to_psu, cont, group, sw_addr)."""
    return {'proto': (can_id >> 23) & 0x3F, 'addr': (can_id >> 16) & 0x7F,
            'cmd': (can_id >> 8) & 0xFF, 'to_psu': bool(can_id & 0x80),
            'cont': bool(can_id & 1), 'group': (can_id >> 2) & 0x1F, 'sw_addr': bool(can_id & 2)}


def is_huawei_id(can_id, ext=True):
    if not ext:
        return False
    h = unpack_id(can_id)
    return h['proto'] in (PROTO_CTL, PROTO_PSU) and h['group'] == GROUP_ALL


def request_id(cmd, addr=0):
    """Controller -> PSU (addr 0 = broadcast)."""
    return pack_id(PROTO_CTL, addr, cmd, True)


def reply_id(cmd, addr, cont):
    return pack_id(PROTO_CTL, addr, cmd, False, cont)


def unsolicited_id(addr):
    return pack_id(PROTO_PSU, addr, CMD_UNSOL, False)


ID_STATUS_REQ_ALL = request_id(CMD_DATA, 0)     # 0x108040FE
ID_SET_PSU1 = request_id(CMD_SET, 1)            # 0x108180FE
ID_SET_ALL = request_id(CMD_SET, 0)             # 0x108080FE
ID_INFO_PSU1 = request_id(CMD_INFO, 1)          # 0x108150FE
ID_UNSOL_PSU1 = unsolicited_id(1)               # 0x1001117E


# --------------------------------------------------------------- registers
# status burst (0x40): reg -> (name, unit, divisor)
STATUS_REGS = {
    0x0E: ('operating_hours', 'h', 1),
    0x70: ('ac_in_power_w', 'W', SCALE_1024),
    0x71: ('ac_in_freq_hz', 'Hz', SCALE_1024),
    0x72: ('ac_in_current_a', 'A', SCALE_1024),
    0x73: ('dc_out_power_w', 'W', SCALE_1024),
    0x74: ('efficiency', '', SCALE_1024),
    0x75: ('out_voltage_v', 'V', SCALE_1024),
    0x76: ('current_limit_ratio', '', SCALE_1024),    # ratio of nominal (unverified)
    0x77: ('power_available_ratio', '', SCALE_1024),
    0x78: ('ac_in_voltage_v', 'V', SCALE_1024),
    0x7C: ('dc_bus_voltage_v', 'V', SCALE_1024),
    0x7E: ('pfc_temp_c', 'degC', SCALE_1024),
    0x7F: ('out_temp_c', 'degC', SCALE_1024),
    0x80: ('in_temp_c', 'degC', SCALE_1024),
    0x81: ('out_current_a', 'A', SCALE_1024),           # fast / unfiltered
    0x82: ('out_current_filtered_a', 'A', SCALE_1024),
    0x83: ('status_bits', '', 1),
    # measured 2026-10-25/11-01 on the R4875G5 (not in any community source): the burst's 0x75
    # is 0 while the terminals carry the voltage; GET 0x85 returns it. 0x88 bytes 2-3 = rating
    # in hundredths of an ampere (6900 = 69.00 A, matches info 0x0001). 0x7C = PFC bus 407 V.
    0x85: ('out_voltage_alt_v', 'V', SCALE_1024),
    0x87: ('fan_status', '', 1),
    0x88: ('rated_current_a', 'A', 'u16@2/100'),
}
STATUS_LAST_REG = 0x83
OUT_VOLTAGE_ALT_REG = 0x85
LIMIT_READBACK_MODELS = ('R4850G2', 'R4875G1')   # community: 0x76 echoes the on-line raw; measured on the R4875G1 2026-11-02; NOT the R4875G5
CFG_REGS = {0x13: 'module_announce'}      # 0x81 register 0x13: sent by the module itself every 3 s
STATUS_BITS = {1 << 9: 'standby-flag', 1 << 20: 'fan-error?', 1 << 27: 'output-on',
               1 << 29: 'input-power-failure'}   # bytes 4-7 word; bit 29 measured 2026-11-04 (AC off)
# bit 9 follows the 0x32 standby write (1 = flag set), bit 27 = output stage on (both measured 2026-11-01);
# bit 23 (0x800000) and bits 2/4 seen set on unit 1, meaning unknown
OUTPUT_ON_BIT = 1 << 27
STATUS_BITS_HI = {1 << 29: 'input-power-failure'}                        # bytes 2-5 word

# set registers (0x80): name -> (reg, layout, unit)
SET_REGS = {
    'online_voltage': (0x00, 'u32', 'V'),
    'default_voltage': (0x01, 'u32', 'V'),
    'ovp_voltage': (0x02, 'u32', 'V'),
    'online_current': (0x03, 'u32', 'A'),
    'default_current': (0x04, 'u32', 'A'),      # non-volatile: commissioning only
    'ac_current_limit': (0x09, 'u32', 'A'),     # not on R4850G2
    'fan_duty': (0x14, 'u16@2', '%'),           # not on R4850G2
    'standby': (0x32, 'flag3', ''),
    'fan_max': (0x34, 'flag3', ''),
}
SET_REG_BY_NUM = dict((v[0], k) for k, v in SET_REGS.items())
NONVOLATILE_REGS = ('default_voltage', 'default_current', 'ovp_voltage', 'fan_max')
VOLTAGE_RANGES = {'online_voltage': (41.0, 58.6), 'default_voltage': (48.0, 58.4),
                  'ovp_voltage': (56.5, 60.5)}
CURRENT_RATIO_MAX = 1250 / 1024.0


# ------------------------------------------------------------ current scale
class CurrentScale(object):
    """Named policy for the current registers. Never a constant: Phase B establishes
    which one each unit obeys (rsh huawei calibrate-current) and the engine binds it
    to the unit's serial, never to its address."""

    def __init__(self, name, needs_nominal, source):
        self.name = name
        self.needs_nominal = needs_nominal
        self.source = source

    def encode(self, amps, nominal_a=None):
        if self.name == 'ratio1024':
            if not nominal_a:
                raise ValueError('ratio1024 scale needs the nominal current (info reg 0x0001)')
            return int(round(amps / nominal_a * 1024))
        if self.name == 'x20':
            return int(round(amps * 20))
        if self.name == 'x30':
            return int(round(amps * 30))
        if self.name == 'conservative':
            # the SMALLEST raw any reading would produce: whichever reading the unit obeys,
            # the resulting limit is at or below the intended amps (harness policy, 2026-10-25)
            if not nominal_a:
                raise ValueError('conservative scale needs the nominal current (info reg 0x0001)')
            return min(CURRENT_SCALES[n].encode(amps, nominal_a) for n in READINGS)
        raise ValueError(self.name)

    def decode(self, raw, nominal_a=None):
        if self.name == 'ratio1024':
            if not nominal_a:
                raise ValueError('ratio1024 scale needs the nominal current')
            return raw / 1024.0 * nominal_a
        if self.name == 'conservative':
            # the LARGEST amps any reading would make of this raw (worst case)
            if not nominal_a:
                raise ValueError('conservative scale needs the nominal current')
            return max(CURRENT_SCALES[n].decode(raw, nominal_a) for n in READINGS)
        return raw / (20.0 if self.name == 'x20' else 30.0)


READINGS = ('ratio1024', 'x20', 'x30')     # the community readings; 'conservative' is a policy
CURRENT_SCALES = {
    'ratio1024': CurrentScale('ratio1024', True, 'patagonaa (3 models), esphome, Huawei R100030G1 guide'),
    'x20': CurrentScale('x20', False, 'haklein, ele-dev, mjpalmowski R4850G2.YAML'),
    'x30': CurrentScale('x30', False, 'BotoX, craigpeacock Protocol.xlsx'),
    # not a reading of the unit but a policy for the time before calibrate-current has been
    # run with a load: send the smallest raw of all readings, report the worst case back.
    # Bound to serial 708A85B00972 by the harness on 2026-10-25 (set --current 10 -> raw 148 acked).
    'conservative': CurrentScale('conservative', True, 'harness policy: min raw of ratio1024/x20/x30'),
}


# ------------------------------------------------------------------ encode
def encode_set_data(name, value, nominal_a=None, scale='ratio1024'):
    """Data bytes of a 0x80 set frame and the raw value. Volts in V, amps in A (via the
    named scale), flags 0/1, fan duty in percent."""
    reg, layout, unit = SET_REGS[name]
    if unit == 'V':
        raw = int(round(value * SCALE_1024))
    elif unit == 'A':
        raw = CURRENT_SCALES[scale].encode(value, nominal_a)
    elif layout == 'u16@2':
        raw = int(round(value / 100.0 * 25600))
    else:
        raw = 1 if value else 0
    if raw < 0:
        raise ValueError('negative raw value for %s' % name)
    if layout == 'u32':
        data = bytes([0x01, reg, 0, 0]) + raw.to_bytes(4, 'big')
    elif layout == 'flag3':
        data = bytes([0x01, reg, 0, raw, 0, 0, 0, 0])
    else:
        data = bytes([0x01, reg]) + raw.to_bytes(2, 'big') + bytes(4)
    return data, raw


def status_request_data():
    return bytes(8)


def get_data(reg):
    return bytes([0x01, reg & 0xFF, 0, 0, 0, 0, 0, 0])


def voltage_in_range(name, volts):
    lo, hi = VOLTAGE_RANGES.get(name, (0.0, 1e9))
    return lo <= volts <= hi


# ------------------------------------------------------------------ decode
def _u32(d, off=4):
    return int.from_bytes(d[off:off + 4], 'big')


def _pad(data):
    return bytes(data).ljust(8, b'\0')


def decode_status_data(data):
    d = _pad(data)
    reg = d[1]
    raw = _u32(d)
    out = {'reg': reg, 'raw': raw}
    if d[0] != 0x01:
        out['note'] = 'unexpected byte0 0x%02X' % d[0]
    spec = STATUS_REGS.get(reg)
    if spec is None:
        out['name'] = 'unknown_%02X' % reg
        return out
    name, unit, div = spec
    out['name'] = name
    if div == 'u16@2/100':
        out['value'] = int.from_bytes(d[2:4], 'big') / 100.0
    elif reg == STATUS_LAST_REG:
        hi = int.from_bytes(d[2:6], 'big')
        flags = [n for bit, n in STATUS_BITS.items() if raw & bit]
        flags += [n for bit, n in STATUS_BITS_HI.items() if hi & bit]
        out.update({'value': raw, 'flags': flags, 'bits_2_5': hi,
                    # the R4875G1 sets input-power-failure in the bytes-4-7 word (measured 2026-11-04,
                    # 00 00 20 02 02 08 with the plug pulled); the community tables put it in the
                    # bytes-2-5 word. Either one means no AC.
                    'ac_present': not (bool(raw & (1 << 29)) or bool(hi & (1 << 29)))})
    else:
        out['value'] = round(raw / float(div), 3) if div != 1 else raw
    if unit:
        out['unit'] = unit
    return out


def decode_ack_data(data):
    d = _pad(data)
    reg = d[1]
    err = d[0] == ACK_ERROR_BYTE or bool(d[0] & 0x20) or (d[0] & 0x0F) == 2
    name = SET_REG_BY_NUM.get(reg, 'unknown_%02X' % reg)
    layout = SET_REGS.get(name, (None, 'u32', ''))[1]
    raw = d[3] if layout == 'flag3' else _u32(d)
    out = {'reg': reg, 'name': name, 'ok': not err, 'raw': raw, 'status_byte': d[0]}
    if err:
        out['error'] = 'parameter error / value out of range (0x21)'
    if SET_REGS.get(name, (None, None, ''))[2] == 'V':
        out['value_v'] = round(raw / float(SCALE_1024), 3)
    return out


def decode_info_data(data):
    d = _pad(data)
    sub = d[1]
    out = {'sub': sub}
    if sub == 0x01:
        val = _u32(d)
        out.update({'name': 'characteristics', 'nominal_current_a': ((val >> 16) & 0x3FF) / 2.0})
    elif sub == 0x02:
        out.update({'name': 'serial', 'serial_hex': d[2:8].hex().upper()})
    elif sub in (0x03, 0x04):
        out.update({'name': 'barcode', 'ascii': d[2:8].decode('ascii', 'replace')})
    elif sub == 0x05:
        out.update({'name': 'versions', 'hw_version': int.from_bytes(d[2:4], 'big'),
                    'dcdc_sw': int.from_bytes(d[4:6], 'big'), 'pfc_sw': int.from_bytes(d[6:8], 'big')})
    elif sub == 0x06:
        out.update({'name': 'slot_id', 'slot_top': d[2], 'slot_bottom': d[3]})
    else:
        out['name'] = 'info_%02X' % sub
    return out


def decode_unsolicited_data(data, nominal_a=None):
    d = _pad(data)
    sub = d[1]
    load_raw = int.from_bytes(d[6:8], 'big')
    out = {'sub': sub, 'load_raw': load_raw, 'load_ratio': round(load_raw / 1024.0, 4)}
    if nominal_a:
        out['load_a_ratio1024'] = round(load_raw / 1024.0 * nominal_a, 2)
    if sub == 0x01:
        out['output_disabled'] = bool(d[3])
    elif sub == 0x03:
        out['output_active'] = bool(d[5])
    return out


def decode_addr_data(data):
    d = _pad(data)
    return {'phase': '%02X%02X' % (d[0], d[1]), 'serial_hex': d[2:8].hex().upper()}


def decode(frame, nominal_a=None):
    """Decode any Huawei frame into a dict; None for non-Huawei, error and echo frames.
    Keys always present: addr, cmd, cont, kind, flow."""
    if frame.err or frame.echo or not frame.ext or not is_huawei_id(frame.can_id):
        return None
    h = unpack_id(frame.can_id)
    cmd = h['cmd']
    out = {'addr': h['addr'], 'cmd': cmd, 'cmd_name': CMD_NAMES.get(cmd, 'cmd-%02X' % cmd),
           'cont': h['cont'], 'ts': frame.ts}
    from_psu = not h['to_psu'] or cmd in (CMD_UNSOL, CMD_ADDR)
    data = frame.data
    if cmd == CMD_DATA and from_psu:
        out['kind'] = 'status'
        out.update(decode_status_data(data))
    elif cmd == CMD_DATA:
        out['kind'] = 'status_request'
    elif cmd == CMD_SET and from_psu:
        out['kind'] = 'ack'
        out.update(decode_ack_data(data))
    elif cmd in (CMD_SET, CMD_CFG):
        out['kind'] = 'set'
        d = _pad(data)
        name = (CFG_REGS if cmd == CMD_CFG else SET_REG_BY_NUM).get(d[1], 'unknown_%02X' % d[1])
        layout = SET_REGS.get(name, (None, 'u32', ''))[1]
        out.update({'name': name, 'raw': d[3] if layout == 'flag3' else _u32(d)})
    elif cmd == CMD_INFO and from_psu:
        out['kind'] = 'info'
        out.update(decode_info_data(data))
    elif cmd == CMD_INFO:
        out['kind'] = 'info_request'
    elif cmd == CMD_ELABEL and from_psu:
        out['kind'] = 'elabel'
        out['ascii'] = _pad(data)[2:8].decode('ascii', 'replace')
    elif cmd == CMD_ELABEL:
        out['kind'] = 'elabel_request'
    elif cmd == CMD_GET and from_psu:
        out['kind'] = 'get_reply'
        out.update(decode_status_data(data))
    elif cmd == CMD_GET:
        out['kind'] = 'get'
        out['reg'] = _pad(data)[1]
    elif cmd == CMD_UNSOL:
        out['kind'] = 'unsolicited'
        out.update(decode_unsolicited_data(data, nominal_a))
    elif cmd == CMD_ADDR:
        out['kind'] = 'addr_negotiation'
        out.update(decode_addr_data(data))
    else:
        out['kind'] = 'other'
    out['flow'] = 'PSU->CTL' if from_psu else 'CTL->PSU'
    return out


# ------------------------------------------------------------- aggregation
class StatusBurst(object):
    """Collects one unit's 0x40 reply frames until the last one (cont=0) and yields a
    snapshot: {name: value} plus 'registers' (reg -> raw), 'flags', 'ac_present',
    'complete', 'frames'. A burst that never completes is reported incomplete."""

    def __init__(self, addr):
        self.addr = addr
        self.frames = []
        self.complete = False
        self.started = None

    def feed(self, dec):
        if dec.get('kind') != 'status' or dec.get('addr') != self.addr:
            return False
        if self.started is None:
            self.started = dec.get('ts')
        self.frames.append(dec)
        if not dec['cont']:
            self.complete = True
        return True

    def snapshot(self):
        snap = {'registers': {}, 'unknown': [], 'flags': [], 'addr': self.addr}
        for f in self.frames:
            if f['name'].startswith('unknown'):
                snap['unknown'].append((f['reg'], f['raw']))
                continue
            snap[f['name']] = f.get('value')
            snap['registers'][f['reg']] = f['raw']
            if f['name'] == 'status_bits':
                snap['flags'] = f['flags']
                snap['ac_present'] = f['ac_present']
        snap['frames'] = len(self.frames)
        snap['complete'] = self.complete
        snap['ts'] = self.frames[-1]['ts'] if self.frames else None
        return snap


class ELabel(object):
    def __init__(self):
        self.text = ''
        self.complete = False

    def feed(self, dec):
        if dec.get('kind') != 'elabel':
            return False
        self.text += dec['ascii']
        if not dec['cont']:
            self.complete = True
        return True

    def fields(self):
        out = {}
        for line in self.text.replace('\r', '\n').split('\n'):
            if '=' in line:
                k, v = line.split('=', 1)
                out[k.strip()] = v.strip().rstrip('\x00')
        return out
