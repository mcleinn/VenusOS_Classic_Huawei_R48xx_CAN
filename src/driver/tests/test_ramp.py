"""The pure decision logic of src/deploy/huawei-ramp.py (the operator-paced first-load test):
the ceiling mirrors the live driver, the ramp rule is the governor's, the scale inference and
the abort rules. The D-Bus and CAN parts run only on the Cerbo."""
import importlib.util
import os
import unittest

import context  # noqa: F401
from huawei import protocol as P
from huawei import control as C

HERE = os.path.dirname(os.path.abspath(__file__))
RAMP = os.path.join(os.path.dirname(os.path.dirname(HERE)), 'deploy', 'huawei-ramp.py')
spec = importlib.util.spec_from_file_location('huawei_ramp', RAMP)
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)


class TestCeiling(unittest.TestCase):
    def test_minimum_of_every_live_term(self):
        a, why = R.ceiling(50.0, 60.0, 148.0, 148.0, 69.0, True, True)
        self.assertEqual((a, why), (50.0, '--max'))
        a, why = R.ceiling(50.0, 30.0, 148.0, 148.0, 69.0, True, True)
        self.assertEqual((a, why), (30.0, 'unit /Settings/ChargeCurrentLimit'))
        a, why = R.ceiling(50.0, 60.0, 12.5, 148.0, 69.0, True, True)
        self.assertEqual((a, why), (12.5, 'DVCC /Dvcc/Chargers/Budget'))
        a, why = R.ceiling(50.0, 60.0, 148.0, 20.0, 69.0, True, True)
        self.assertEqual((a, why), (20.0, 'BMS /Info/MaxChargeCurrent'))
        a, why = R.ceiling(50.0, 60.0, 148.0, 148.0, 30.0, True, True)
        self.assertEqual(why, 'nominal x 1.22')
        self.assertAlmostEqual(a, 30.0 * P.CURRENT_RATIO_MAX, places=1)

    def test_soc_inhibit_budget_zero_gives_zero(self):
        self.assertEqual(R.ceiling(50.0, 60.0, 0.0, 148.0, 69.0, True, True)[0], 0.0)

    def test_unknown_terms_are_zero_not_unlimited(self):
        self.assertEqual(R.ceiling(50.0, None, 148.0, 148.0, 69.0, True, True)[0], 0.0)
        self.assertEqual(R.ceiling(50.0, 60.0, None, 148.0, 69.0, True, True)[0], 0.0)
        self.assertEqual(R.ceiling(50.0, 60.0, 148.0, None, 69.0, True, True)[0], 0.0)
        self.assertEqual(R.ceiling(50.0, 60.0, 148.0, 148.0, None, True, True)[0], 0.0)

    def test_operator_switch_and_dvcc_control_gate(self):
        self.assertEqual(R.ceiling(50.0, 60.0, 148.0, 148.0, 69.0, False, True), (0.0, 'operator switch /Mode off'))
        self.assertEqual(R.ceiling(50.0, 60.0, 148.0, 148.0, 69.0, True, False), (0.0, 'patched DVCC not in control'))

    def test_voltage_target_follows_dvcc_within_bms_and_clamp(self):
        self.assertEqual(R.voltage_target(52.4, 52.8), 52.4)
        self.assertEqual(R.voltage_target(53.5, 52.8), 52.8)
        self.assertEqual(R.voltage_target(None, None), 52.4)
        self.assertEqual(R.voltage_target(60.0, None), C.VOLTAGE_MAX_V)
        self.assertEqual(R.voltage_target(30.0, None), C.VOLTAGE_MIN_V)


class TestRampAndInference(unittest.TestCase):
    def test_ramp_up_is_rate_limited_down_is_immediate(self):
        self.assertEqual(R.ramp(0.0, 20.0, 1.0), C.RAMP_UP_A_PER_S)
        self.assertEqual(R.ramp(10.0, 12.0, 1.0), 12.0)
        self.assertEqual(R.ramp(40.0, 0.0, 1.0), 0.0)
        self.assertEqual(R.ramp(10.0, 20.0, 0.0), 10.0)

    def test_conservative_raw_for_5a_tells_the_reading_apart(self):
        raw = P.CURRENT_SCALES['conservative'].encode(5.0, 69.0)
        self.assertEqual(raw, min(P.CURRENT_SCALES[n].encode(5.0, 69.0) for n in P.READINGS))
        self.assertEqual(R.infer_reading(raw, 5.0, 69.0)[0], 'ratio1024')
        self.assertEqual(R.infer_reading(raw, raw / 20.0, 69.0)[0], 'x20')
        self.assertEqual(R.infer_reading(raw, raw / 30.0, 69.0)[0], 'x30')
        self.assertIsNone(R.infer_reading(raw, 40.0, 69.0)[0])
        self.assertIsNone(R.infer_reading(0, 0.0, 69.0)[0])
        self.assertIsNone(R.infer_reading(raw, None, 69.0)[0])


class TestListenerAcks(unittest.TestCase):
    def test_acks_are_found_by_time_after_the_list_is_trimmed(self):
        lst = R.Listener(link=None)
        for i in range(R.ACK_KEEP + 30):
            lst.acks.append((float(i), {'addr': 2, 'name': 'online_current', 'ok': True}))
            del lst.acks[:-R.ACK_KEEP]
        self.assertEqual(len(lst.acks), R.ACK_KEEP)
        t0 = float(R.ACK_KEEP + 29)
        found = lst.acks_since(t0)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][0], t0)
        self.assertEqual(lst.acks_since(t0 + 1.0), [])


class TestAbort(unittest.TestCase):
    def snap(self, **kw):
        s = {'out_current_a': 4.9, 'out_voltage_v': 50.1, 'pfc_temp_c': 40.0, 'out_temp_c': 41.0,
             'in_temp_c': 30.0, 'complete': True}
        s.update(kw)
        return s

    def test_quiet_when_everything_is_in_range(self):
        self.assertIsNone(R.abort_reason(5.0, self.snap(), 0.5, 52.4, 65.0, 0.0, 50.1, 52.8))

    def test_stale_status_means_driver_gone(self):
        self.assertIn('no status', R.abort_reason(5.0, self.snap(), 6.0, 52.4, 65.0, 0.0, 50.1, 52.8))
        self.assertIn('no status', R.abort_reason(5.0, {}, None, 52.4, 65.0, 0.0, 50.1, 52.8))

    def test_overshoot_temperature_voltage_other_unit_and_bms(self):
        self.assertIn('overshoot', R.abort_reason(5.0, self.snap(out_current_a=7.5), 0.5, 52.4, 65.0, 0.0, 50.1, 52.8))
        self.assertIsNone(R.abort_reason(5.0, self.snap(out_current_a=6.5), 0.5, 52.4, 65.0, 0.0, 50.1, 52.8))
        self.assertIn('out_temp_c', R.abort_reason(5.0, self.snap(out_temp_c=66.0), 0.5, 52.4, 65.0, 0.0, 50.1, 52.8))
        self.assertIn('voltage', R.abort_reason(5.0, self.snap(out_voltage_v=53.6), 0.5, 52.4, 65.0, 0.0, 50.1, 52.8))
        self.assertIn('OTHER', R.abort_reason(5.0, self.snap(), 0.5, 52.4, 65.0, 1.5, 50.1, 52.8))
        self.assertIn('CVL', R.abort_reason(5.0, self.snap(), 0.5, 52.4, 65.0, 0.0, 53.1, 52.8))

    def test_r4875g5_voltage_comes_from_0x85(self):
        s = self.snap(out_voltage_v=0.0, v85=53.9)
        self.assertIn('voltage', R.abort_reason(5.0, s, 0.5, 52.4, 65.0, 0.0, 50.1, 52.8))


if __name__ == '__main__':
    unittest.main()
