import unittest

import context  # noqa: F401
from huawei import protocol as P
from huawei.canbus import CanFrame


class TestIdentifiers(unittest.TestCase):
    def test_well_known_ids_match_the_notes(self):
        self.assertEqual(P.ID_STATUS_REQ_ALL, 0x108040FE)
        self.assertEqual(P.ID_SET_PSU1, 0x108180FE)
        self.assertEqual(P.ID_SET_ALL, 0x108080FE)
        self.assertEqual(P.ID_INFO_PSU1, 0x108150FE)
        self.assertEqual(P.ID_UNSOL_PSU1, 0x1001117E)
        self.assertEqual(P.reply_id(P.CMD_DATA, 1, True), 0x1081407F)
        self.assertEqual(P.reply_id(P.CMD_DATA, 1, False), 0x1081407E)
        self.assertEqual(P.reply_id(P.CMD_SET, 1, False), 0x1081807E)
        self.assertEqual(P.request_id(P.CMD_DATA, 2), 0x108240FE)

    def test_pack_unpack_roundtrip(self):
        for cid in (0x108040FE, 0x1081407F, 0x1001117E, 0x1000107E, 0x1082807E):
            h = P.unpack_id(cid)
            self.assertEqual(P.pack_id(h['proto'], h['addr'], h['cmd'], h['to_psu'], h['cont'],
                                       h['group'], h['sw_addr']), cid)

    def test_non_huawei_ids_rejected(self):
        self.assertFalse(P.is_huawei_id(0x18F, ext=False))
        self.assertFalse(P.is_huawei_id(0x0CF00400))      # J1939, group bits differ
        self.assertTrue(P.is_huawei_id(0x108040FE))

    def test_verification_flag_is_on_since_the_load_test(self):
        self.assertTrue(P.VERIFIED)
        self.assertTrue(any('ratio1024' in x and 'clamp' in x for x in P.MEASURED))
        self.assertTrue(len(P.UNVERIFIED) >= 5)
        self.assertTrue(any('0x85' in x for x in P.MEASURED))
        self.assertTrue(any('polling' in x for x in P.MEASURED))


class TestMeasuredOnUnit1(unittest.TestCase):
    """Frames exactly as charger 1 (R4875G5, 708A85B00972) sent them to the Cerbo on 2026-11-01."""

    def test_conservative_scale_matches_the_harness(self):
        sc = P.CURRENT_SCALES['conservative']
        self.assertEqual(sc.encode(10.0, 69.0), 148)            # rsh: set --current 10 -> raw 148
        self.assertEqual(round(sc.decode(148, 69.0), 2), 9.97)  # worst case of the three readings
        self.assertEqual(sc.encode(0.0, 69.0), 0)
        with self.assertRaises(ValueError):
            sc.encode(10.0, None)
        data, raw = P.encode_set_data('online_current', 10.0, 69.0, 'conservative')
        self.assertEqual(raw, 148)
        self.assertEqual(data, bytes([1, 3, 0, 0, 0, 0, 0, 148]))

    def test_get_0x85_reply(self):
        d = P.decode(CanFrame(0x1081827E, bytes([0x01, 0x85, 0, 0, 0, 0, 0x09, 0xA6])))
        self.assertEqual(d['kind'], 'get_reply')
        self.assertEqual(d['name'], 'out_voltage_alt_v')
        self.assertEqual(d['value'], 2.412)

    def test_get_0x88_reply_is_the_rating_in_hundredths(self):
        d = P.decode(CanFrame(0x1081827E, bytes([0x01, 0x88, 0x1A, 0xF4, 0, 0, 0, 0])))
        self.assertEqual(d['name'], 'rated_current_a')
        self.assertEqual(d['value'], 69.0)

    def test_info_characteristics_of_unit_1(self):
        d = P.decode(CanFrame(0x1081507F, bytes.fromhex('00010000408a10b3')))
        self.assertEqual(d['nominal_current_a'], 69.0)
        d = P.decode(CanFrame(0x1081507F, bytes.fromhex('0002708a85b00972')))
        self.assertEqual(d['serial_hex'], '708A85B00972')

    def test_module_announce_frame_is_named(self):
        d = P.decode(CanFrame(0x108081FE, bytes([1, 0x13, 0, 1, 0, 0, 0, 0])))
        self.assertEqual(d['kind'], 'set')
        self.assertEqual(d['name'], 'module_announce')
        self.assertEqual(d['addr'], 0)

    def test_status_bits_as_seen_on_the_cerbo(self):
        d = P.decode(CanFrame(0x1081407E, bytes([1, 0x83, 0, 0, 0, 0, 0x80, 0])))
        self.assertEqual(d['value'], 0x8000)
        self.assertTrue(d['ac_present'])
        # output-enable probe 2026-11-01: standby flag (bit 9) and output-on (bit 27)
        d = P.decode(CanFrame(0x1081407E, bytes([1, 0x83, 0, 0, 0x00, 0x80, 0x02, 0x14])))
        self.assertEqual(d['flags'], ['standby-flag'])
        d = P.decode(CanFrame(0x1081407E, bytes([1, 0x83, 0, 0, 0x08, 0x80, 0x00, 0x14])))
        self.assertEqual(d['flags'], ['output-on'])


class TestEncode(unittest.TestCase):
    def test_voltage_encoding(self):
        data, raw = P.encode_set_data('online_voltage', 50.0)
        self.assertEqual(raw, 51200)
        self.assertEqual(data, bytes([0x01, 0x00, 0, 0, 0, 0, 0xC8, 0x00]))

    def test_current_ratio1024_example_from_notes(self):
        # 10 A on a 35 A unit = 10/35 x 1024 = 293 = 0x0125
        data, raw = P.encode_set_data('online_current', 10.0, nominal_a=35.0, scale='ratio1024')
        self.assertEqual(raw, 293)
        self.assertEqual(data[1], 0x03)
        self.assertEqual(data[6:8], bytes([0x01, 0x25]))

    def test_current_other_scales(self):
        self.assertEqual(P.encode_set_data('online_current', 10.0, scale='x20')[1], 200)
        self.assertEqual(P.encode_set_data('online_current', 10.0, scale='x30')[1], 300)

    def test_ratio_scale_needs_nominal(self):
        with self.assertRaises(ValueError):
            P.encode_set_data('online_current', 10.0, scale='ratio1024')

    def test_flag_register(self):
        data, raw = P.encode_set_data('standby', 1)
        self.assertEqual(data, bytes([0x01, 0x32, 0, 1, 0, 0, 0, 0]))

    def test_default_current_zero_encodes(self):
        data, raw = P.encode_set_data('default_current', 0.0, nominal_a=52.0)
        self.assertEqual(raw, 0)
        self.assertEqual(data, bytes([0x01, 0x04, 0, 0, 0, 0, 0, 0]))

    def test_negative_refused(self):
        with self.assertRaises(ValueError):
            P.encode_set_data('online_current', -1.0, nominal_a=52.0)


class TestDecode(unittest.TestCase):
    def frame(self, cid, data):
        return CanFrame(cid, bytes(data), ext=True, ts=1.0)

    def test_status_frame(self):
        d = P.decode(self.frame(0x1081407F, [0x01, 0x75, 0, 0, 0, 0, 0xC8, 0x00]))
        self.assertEqual(d['kind'], 'status')
        self.assertEqual(d['name'], 'out_voltage_v')
        self.assertEqual(d['value'], 50.0)
        self.assertTrue(d['cont'])
        self.assertEqual(d['flow'], 'PSU->CTL')

    def test_status_bits_last_frame(self):
        d = P.decode(self.frame(0x1081407E, [0x01, 0x83, 0x20, 0, 0, 0, 0x02, 0x00]))
        self.assertEqual(d['name'], 'status_bits')
        self.assertFalse(d['cont'])
        self.assertFalse(d['ac_present'])
        self.assertIn('standby-flag', d['flags'])
        self.assertIn('input-power-failure', d['flags'])

    def test_ack_ok_and_error(self):
        ok = P.decode(self.frame(0x1081807E, [0x01, 0x03, 0, 0, 0, 0, 0x01, 0x25]))
        self.assertEqual(ok['kind'], 'ack')
        self.assertTrue(ok['ok'])
        self.assertEqual(ok['name'], 'online_current')
        err = P.decode(self.frame(0x1081807E, [0x21, 0x03, 0, 0, 0, 0, 0x01, 0x25]))
        self.assertFalse(err['ok'])
        self.assertIn('error', err)

    def test_info_frames(self):
        char = P.decode(self.frame(0x1081507F, [0, 1, 0, 0, 0x40, 0x68, 0x0E, 0x27]))
        self.assertEqual(char['name'], 'characteristics')
        self.assertEqual(char['nominal_current_a'], 52.0)
        ser = P.decode(self.frame(0x1081507F, [0, 2, 0x21, 0x02, 0x31, 0x1A, 0xBC, 0xDE]))
        self.assertEqual(ser['serial_hex'], '2102311ABCDE')
        ver = P.decode(self.frame(0x1081507E, [0, 5, 0x01, 0x00, 0x03, 0x02, 0x02, 0x01]))
        self.assertEqual(ver['dcdc_sw'], 0x0302)

    def test_unsolicited(self):
        d = P.decode(self.frame(0x1001117E, [0, 1, 0, 1, 0, 0, 0x02, 0x00]), nominal_a=52.0)
        self.assertEqual(d['kind'], 'unsolicited')
        self.assertTrue(d['output_disabled'])
        self.assertEqual(d['load_ratio'], 0.5)
        self.assertEqual(d['load_a_ratio1024'], 26.0)

    def test_addr_negotiation(self):
        d = P.decode(self.frame(0x1000107E, [0x30, 0x01, 1, 2, 3, 4, 5, 6]))
        self.assertEqual(d['kind'], 'addr_negotiation')
        self.assertEqual(d['phase'], '3001')
        self.assertEqual(d['serial_hex'], '010203040506')
        self.assertEqual(d['addr'], 0)

    def test_requests_from_controller_are_classified(self):
        self.assertEqual(P.decode(self.frame(0x108040FE, bytes(8)))['kind'], 'status_request')
        self.assertEqual(P.decode(self.frame(0x108150FE, bytes(8)))['kind'], 'info_request')
        s = P.decode(self.frame(0x108180FE, [0x01, 0x03, 0, 0, 0, 0, 0x01, 0x25]))
        self.assertEqual(s['kind'], 'set')
        self.assertEqual(s['name'], 'online_current')
        self.assertEqual(s['raw'], 293)

    def test_truncated_and_foreign_frames(self):
        self.assertIsNone(P.decode(CanFrame(0x18F, b'\x01', ext=False)))
        self.assertIsNone(P.decode(CanFrame(0x40, b'', ext=True, err=True)))
        self.assertIsNone(P.decode(CanFrame(0x108040FE, bytes(8), echo=True)))
        short = P.decode(self.frame(0x1081407F, [0x01, 0x75]))       # dlc 2: padded, value 0
        self.assertEqual(short['value'], 0.0)
        empty = P.decode(self.frame(0x1081407F, []))
        self.assertEqual(empty['reg'], 0)
        self.assertIn('note', empty)


class TestBurst(unittest.TestCase):
    def test_burst_assembles_and_completes(self):
        b = P.StatusBurst(1)
        f1 = P.decode(CanFrame(0x1081407F, bytes([1, 0x75, 0, 0, 0, 0, 0xC8, 0]), ts=1.0))
        f2 = P.decode(CanFrame(0x1081407F, bytes([1, 0x81, 0, 0, 0, 0, 0x28, 0]), ts=1.01))
        f3 = P.decode(CanFrame(0x1081407E, bytes([1, 0x83, 0, 0, 0, 0, 0, 0]), ts=1.02))
        other = P.decode(CanFrame(0x1082407F, bytes([1, 0x75, 0, 0, 0, 0, 0xC8, 0]), ts=1.0))
        self.assertTrue(b.feed(f1))
        self.assertFalse(b.feed(other))          # another address
        self.assertTrue(b.feed(f2))
        self.assertFalse(b.complete)
        self.assertTrue(b.feed(f3))
        self.assertTrue(b.complete)
        s = b.snapshot()
        self.assertEqual(s['out_voltage_v'], 50.0)
        self.assertEqual(s['out_current_a'], 10.0)
        self.assertTrue(s['ac_present'])
        self.assertEqual(s['frames'], 3)
        self.assertEqual(s['ts'], 1.02)


if __name__ == '__main__':
    unittest.main()
