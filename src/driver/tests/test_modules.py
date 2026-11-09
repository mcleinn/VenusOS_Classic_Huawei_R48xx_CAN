import unittest

import context
from context import FakeClock
from huawei import protocol as P
from huawei import modules as M
from huawei.canbus import CanFrame


def status_burst(addr, ts, voltage=50.0, current=0.0, ac=True, limit_ratio=0.5, ac_voltage=None):
    regs = [(0x75, int(voltage * 1024)), (0x81, int(current * 1024)), (0x76, int(limit_ratio * 1024)),
            (0x7F, 40 * 1024)]
    if ac_voltage is not None:
        regs.append((0x78, int(ac_voltage * 1024)))
    frames = [CanFrame(P.reply_id(P.CMD_DATA, addr, True), bytes([1, r, 0, 0]) + v.to_bytes(4, 'big'), ts=ts)
              for r, v in regs]
    frames.append(CanFrame(P.reply_id(P.CMD_DATA, addr, False),
                           bytes([1, 0x83, 0 if ac else 0x20, 0, 0, 0, 0, 0]), ts=ts))
    return frames


def unsol(addr, ts, disabled=False, load_raw=0):
    return CanFrame(P.unsolicited_id(addr), bytes([0, 1, 0, 1 if disabled else 0, 0, 0]) + load_raw.to_bytes(2, 'big'), ts=ts)


def info_serial(addr, ts, serial_hex):
    return CanFrame(P.reply_id(P.CMD_INFO, addr, True), bytes([0, 2]) + bytes.fromhex(serial_hex), ts=ts)


def info_char(addr, ts, nominal=52.0):
    raw = (0x40 << 24) | ((int(nominal * 2) & 0x3FF) << 16) | 0x0E27
    return CanFrame(P.reply_id(P.CMD_INFO, addr, True), bytes([0, 1, 0, 0]) + raw.to_bytes(4, 'big'), ts=ts)


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(100.0)
        self.reg = M.Registry(self.clock, scales={'0000000000A1': 'ratio1024'})

    def feed(self, frames):
        out = None
        for f in frames:
            out = self.reg.feed(P.decode(f))
        return out

    def test_unknown_address_gets_placeholder_then_promoted(self):
        self.feed([unsol(1, 100.0)])
        self.assertEqual(self.reg.unidentified_addrs(), [1])
        self.assertIn('addr1', self.reg.modules)
        self.feed(status_burst(1, 100.1, voltage=51.0))
        m = self.feed([info_serial(1, 100.2, '0000000000A1')])
        self.assertEqual(m.serial, '0000000000A1')
        self.assertNotIn('addr1', self.reg.modules)
        self.assertEqual(m.status['out_voltage_v'], 51.0)       # telemetry kept
        self.assertEqual(m.scale, 'ratio1024')
        self.assertEqual(self.reg.by_addr[1], '0000000000A1')
        self.assertEqual(self.reg.unidentified_addrs(), [])

    def test_learn_from_negotiation(self):
        f = CanFrame(P.pack_id(P.PROTO_PSU, 2, P.CMD_ADDR, False), bytes([0x30, 0x02]) + bytes.fromhex('0000000000B2'), ts=100.0)
        m = self.feed([f])
        self.assertEqual(m.serial, '0000000000B2')
        self.assertEqual(m.addr, 2)
        self.assertIsNone(m.scale)        # no policy configured for this serial

    def test_address_change_and_takeover(self):
        self.feed([info_serial(1, 100.0, '0000000000A1'), info_serial(2, 100.0, '0000000000B2')])
        # B2 restarts and ends up on address 1, A1 moves to 2
        f = CanFrame(P.pack_id(P.PROTO_PSU, 1, P.CMD_ADDR, False), bytes([0x30, 0x02]) + bytes.fromhex('0000000000B2'), ts=101.0)
        self.feed([f])
        self.assertEqual(self.reg.by_addr[1], '0000000000B2')
        self.assertIsNone(self.reg.modules['0000000000A1'].addr)       # no longer trusted on 1
        self.assertTrue(any('ADDR-TAKEN' in e[1] for e in self.reg.events))
        self.feed([info_serial(2, 101.5, '0000000000A1')])
        self.assertEqual(self.reg.by_addr[2], '0000000000A1')
        self.assertEqual(self.reg.modules['0000000000A1'].counters['addr_changes'], 1)
        self.assertEqual(self.reg.known_addrs(), [1, 2])

    def test_silent_units_address_is_not_trusted(self):
        # 2026-11-02 on the Cerbo: unit A1 unplugged, another unit plugged in alone took
        # address 1; its bursts must not be booked under A1 before its serial is known
        self.feed([info_serial(1, 100.0, '0000000000A1')] + status_burst(1, 100.1, voltage=51.0))
        a1 = self.reg.modules['0000000000A1']
        self.clock.t = 100.0 + M.DISCONNECT_S + 1.0
        t = self.clock.t
        self.assertFalse(a1.alive())
        m = self.feed(status_burst(1, t, voltage=47.0, limit_ratio=0.205))
        self.assertEqual(m.serial, 'addr1')
        self.assertIsNone(a1.addr)
        self.assertEqual(a1.status['out_voltage_v'], 51.0)         # untouched
        self.assertEqual(self.reg.unidentified_addrs(), [1])
        self.assertTrue(any('binding dropped' in e[1] for e in self.reg.events))
        m = self.feed([info_serial(1, t + 0.5, '0000000000B2')])
        self.assertEqual(m.serial, '0000000000B2')
        self.assertEqual(m.status['out_voltage_v'], 47.0)          # parked telemetry promoted
        self.assertEqual(self.reg.by_addr[1], '0000000000B2')
        self.assertNotIn('addr1', self.reg.modules)
        # B2 goes, A1 returns on address 1: parked frames are discarded, A1's record stands
        self.clock.t = t + M.DISCONNECT_S + 2.0
        t = self.clock.t
        self.feed(status_burst(1, t, voltage=52.0))
        self.assertIn('addr1', self.reg.modules)
        m = self.feed([info_serial(1, t + 0.1, '0000000000A1')])
        self.assertIs(m, a1)
        self.assertEqual(a1.addr, 1)
        self.assertEqual(self.reg.by_addr[1], '0000000000A1')
        self.assertNotIn('addr1', self.reg.modules)
        self.assertEqual(a1.status['out_voltage_v'], 51.0)         # not merged
        self.assertIsNone(self.reg.modules['0000000000B2'].addr)
        self.feed(status_burst(1, t + 0.2, voltage=52.0))
        self.assertEqual(a1.status['out_voltage_v'], 52.0)

    def test_incomplete_burst_is_not_fresh(self):
        self.feed([info_serial(1, 100.0, '0000000000A1')])
        m = self.reg.modules['0000000000A1']
        self.feed(status_burst(1, 100.1)[:-1])          # no 0x83 last frame
        self.assertFalse(m.status_fresh())
        self.feed(status_burst(1, 100.2))
        self.assertTrue(m.status_fresh())


class TestModuleStates(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(100.0)
        self.reg = M.Registry(self.clock, scales={'0000000000A1': 'ratio1024'})
        for f in [info_char(1, 100.0), info_serial(1, 100.0, '0000000000A1')]:
            self.reg.feed(P.decode(f))
        self.m = self.reg.modules['0000000000A1']

    def feed(self, frames):
        for f in frames:
            self.reg.feed(P.decode(f))

    def test_inhibited_ready_standby(self):
        self.feed(status_burst(1, 100.1) + [unsol(1, 100.1)])
        self.assertEqual(self.m.evaluate(), M.INHIBITED)         # inhibit is the default
        self.m.inhibit = False
        self.assertEqual(self.m.evaluate(), M.READY)
        # unsolicited byte 3 is informational on the R4875G5 (1 seen with 49.9 V out): no state change
        self.feed([unsol(1, 100.2, disabled=True)])
        self.assertEqual(self.m.evaluate(), M.READY)
        self.assertTrue(self.m.unsol_byte3())
        self.feed([unsol(1, 100.3)] + status_burst(1, 100.3, ac=False))
        self.assertEqual(self.m.evaluate(), M.STANDBY)

    def test_degraded_then_disconnected(self):
        self.feed(status_burst(1, 100.0) + [unsol(1, 100.0)])
        self.clock.advance(6.0)
        self.feed([unsol(1, 106.0)])
        self.assertEqual(self.m.evaluate(), M.DEGRADED)
        self.clock.advance(11.0)
        self.assertEqual(self.m.evaluate(), M.DISCONNECTED)
        self.assertFalse(self.m.alive())

    def test_uncommanded_output_is_a_fault(self):
        self.feed(status_burst(1, 100.0, current=12.0))
        self.assertEqual(self.m.evaluate(), M.FAULTED)
        self.assertIn('uncommanded', self.m.fault)
        # fault is held after the current stops
        self.clock.advance(1.0)
        self.feed(status_burst(1, 101.0, current=0.0))
        self.assertEqual(self.m.evaluate(), M.FAULTED)
        self.clock.advance(M.FAULT_HOLD_S)
        # silent longer than DISCONNECT_S: the unit has to re-identify before a burst counts
        self.feed([info_serial(1, self.clock(), '0000000000A1')] + status_burst(1, self.clock(), current=0.0))
        self.assertEqual(self.m.evaluate(), M.INHIBITED)
        self.assertIsNone(self.m.fault)

    def test_ack_error_faults_and_setpoint_views_stay_apart(self):
        self.m.model = 'R4850G2'                                 # a model whose 0x76 echoes the limit
        self.feed(status_burst(1, 100.0, limit_ratio=0.25))
        self.assertEqual(self.m.current.measured, 13.0)          # 0.25 x 52 A
        self.assertIsNone(self.m.current.requested)
        self.assertIsNone(self.m.current.sent_value)
        ack = CanFrame(P.reply_id(P.CMD_SET, 1, False), bytes([0x21, 0x03, 0, 0, 0, 0, 0x04, 0xE3]), ts=100.1)
        self.feed([ack])                                         # nothing of ours pending: foreign, no fault
        self.assertIsNone(self.m.fault)
        self.assertEqual(self.m.counters['ack_foreign'], 1)
        self.m.current.sent_value, self.m.current.sent_raw, self.m.current.sent_t, self.m.current.ack_ok = 13.0, 0x4E3, 100.05, None
        self.feed([CanFrame(P.reply_id(P.CMD_SET, 1, False), bytes([0x21, 0x03, 0, 0, 0, 0, 0x04, 0xE3]), ts=100.2)])
        self.assertFalse(self.m.current.ack_ok)
        self.assertEqual(self.m.evaluate(), M.FAULTED)
        self.assertEqual(self.m.counters['ack_err'], 1)

    def test_measured_limit_needs_scale_and_a_readback_model(self):
        self.m.model = 'R4850G2'
        self.m.scale = None
        self.feed(status_burst(1, 100.0, limit_ratio=0.25))
        self.assertIsNone(self.m.current.measured)
        self.m.scale = 'x20'
        self.feed(status_burst(1, 100.1, limit_ratio=0.25))
        self.assertEqual(self.m.current.measured, 12.8)          # 256 / 20
        # R4875G5: 0x76 is not a readback (measured 2026-10-25); the ratio is still reported
        self.m.model = 'R4875G5'
        self.feed(status_burst(1, 100.2, limit_ratio=0.25))
        self.assertIsNone(self.m.current.measured)
        self.assertEqual(self.m.limit_ratio, 0.25)
        self.m.model = None
        self.feed(status_burst(1, 100.3, limit_ratio=0.25))
        self.assertIsNone(self.m.current.measured)

    def test_voltage_from_0x85_when_0x75_is_dead(self):
        # R4875G5 burst: 0x75 = 0; the engine's GET 0x85 reply carries the terminal voltage
        self.feed(status_burst(1, 100.0, voltage=0.0))
        self.assertEqual(self.m.measured_voltage(), 0.0)
        self.assertEqual(self.m.voltage_source(), '0x75')
        get85 = CanFrame(P.reply_id(P.CMD_GET, 1, False), bytes([1, 0x85, 0, 0, 0, 0, 0x09, 0xA6]), ts=100.1)
        self.feed([get85])
        self.assertEqual(self.m.measured_voltage(), 2.412)
        self.assertEqual(self.m.voltage_source(), '0x85')
        self.assertEqual(self.m.voltage.measured, 2.412)
        # a later burst with a real 0x75 wins
        self.feed(status_burst(1, 100.2, voltage=52.5))
        self.assertEqual(self.m.measured_voltage(), 52.5)
        self.assertEqual(self.m.voltage_source(), '0x75')
        # the 0x85 reading has its own freshness: stale, the burst's 0 is reported; then None
        self.feed(status_burst(1, 100.3, voltage=0.0))
        self.clock.advance(M.STATUS_STALE_S - 0.2)             # 104.8: both fresh
        self.assertEqual(self.m.measured_voltage(), 2.412)
        self.clock.advance(0.4)                                # 105.2: 0x85 (100.1) stale, status (100.3) fresh
        self.assertEqual(self.m.measured_voltage(), 0.0)
        self.clock.advance(0.3)                                # 105.5: status stale too
        self.assertIsNone(self.m.measured_voltage())

    def test_elabel_gives_the_model(self):
        text = b'/$[ArchivesInfo Version]\r\nBoardType=R4875G5\r\nModel=R4875G5\r\nDescription=Function Module\r\n'
        chunks = [text[i:i + 6] for i in range(0, len(text), 6)]
        frames = [CanFrame(P.reply_id(P.CMD_ELABEL, 1, i < len(chunks) - 1), bytes([0, i + 1]) + c.ljust(6, b'\0'), ts=100.0)
                  for i, c in enumerate(chunks)]
        self.assertIsNone(self.m.model)
        self.feed(frames[:-1])
        self.assertIsNone(self.m.model)
        self.feed(frames[-1:])
        self.assertEqual(self.m.model, 'R4875G5')
        self.assertEqual(self.m.summary()['model'], 'R4875G5')

    def test_output_on_bit(self):
        self.feed(status_burst(1, 100.0))
        self.assertFalse(self.m.output_on())
        f = CanFrame(P.reply_id(P.CMD_DATA, 1, False), bytes([1, 0x83, 0, 0, 0x08, 0x80, 0, 0x14]), ts=100.1)
        self.feed(status_burst(1, 100.1)[:-1] + [f])
        self.assertTrue(self.m.output_on())

    def test_get_0x88_rating(self):
        self.feed([CanFrame(P.reply_id(P.CMD_GET, 1, False), bytes([1, 0x88, 0x1A, 0xF4, 0, 0, 0, 0]), ts=100.0)])
        self.assertEqual(self.m.rated_a, 69.0)


if __name__ == '__main__':
    unittest.main()


class TestAcks(unittest.TestCase):
    def test_foreign_ack_errors_are_counted_not_faulted(self):
        clock = FakeClock(100.0)
        m = M.Module('0000000000A1', clock)
        m.set_addr(1, 100.0)
        # the bench tool's fan_duty write was rejected by the unit (2026-11-03): not our write
        m.feed_ack({'kind': 'ack', 'addr': 1, 'name': 'fan_duty', 'ok': False, 'raw': 0x21, 'ts': 100.0})
        self.assertIsNone(m.fault)
        self.assertEqual(m.counters['ack_foreign'], 1)
        # an error ack for a register we use but did not write (bench tool) is foreign as well
        m.feed_ack({'kind': 'ack', 'addr': 1, 'name': 'online_current', 'ok': False, 'raw': 0x21, 'ts': 100.1})
        self.assertIsNone(m.fault)
        self.assertEqual(m.counters['ack_foreign'], 2)
        self.assertEqual(m.counters['ack_err'], 0)
        # our own pending write is judged
        m.current.sent_value, m.current.sent_raw, m.current.sent_t, m.current.ack_ok = 5.0, 68, 100.2, None
        m.feed_ack({'kind': 'ack', 'addr': 1, 'name': 'online_current', 'ok': False, 'raw': 0x21, 'ts': 100.3})
        self.assertEqual(m.counters['ack_err'], 1)
        self.assertTrue(m.fault.startswith('ack error on online_current'))
        self.assertFalse(m.current.ack_ok)


class TestAcPresence(unittest.TestCase):
    """0x78 (AC input voltage) decides; the 0x83 bit is the fallback when 0x78 is not in the burst."""

    def setUp(self):
        self.clock = FakeClock(100.0)
        self.reg = M.Registry(self.clock, scales={'0000000000A1': 'ratio1024'})
        for f in [info_serial(1, 100.0, '0000000000A1'), info_char(1, 100.0)]:
            self.reg.feed(P.decode(f))

    def state_after(self, frames):
        for f in frames:
            self.reg.feed(P.decode(f))
        m = self.reg.modules['0000000000A1']
        m.evaluate(100.2)
        return m

    def test_plug_pulled_reads_0v_and_the_unit_goes_to_standby(self):
        m = self.state_after(status_burst(1, 100.1, ac=True, ac_voltage=0.0))   # bit says ok, voltage says no
        self.assertIs(m.ac_present(), False)
        self.assertEqual(m.state, M.STANDBY)

    def test_mains_present_reads_230v(self):
        m = self.state_after(status_burst(1, 100.1, ac=True, ac_voltage=231.5))
        self.assertIs(m.ac_present(), True)
        self.assertIn(m.state, (M.READY, M.INHIBITED))

    def test_voltage_wins_over_a_stale_looking_bit(self):
        m = self.state_after(status_burst(1, 100.1, ac=False, ac_voltage=228.0))
        self.assertIs(m.ac_present(), True)

    def test_without_0x78_the_bit_decides(self):
        m = self.state_after(status_burst(1, 100.1, ac=False))
        self.assertIs(m.ac_present(), False)
        self.assertEqual(m.state, M.STANDBY)
