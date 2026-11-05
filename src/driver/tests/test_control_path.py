"""The control path end to end on the virtual bus, with protocol.VERIFIED forced True by the
fixture (item 8 of the 2026-11-01 control build). Nothing here runs against hardware; the
simulated units model commissioned R48xx modules (zero stored default) unless a test says
otherwise. Every test that flips the flag restores it; `test_gate_closed_without_the_flag`
proves the same stimulus produces no set frame while the flag is False."""
import unittest

import context
from context import FakeClock
from huawei import protocol as P
from huawei import modules as M
from huawei import control as C
from huawei.engine import EngineFailure
from test_engine import Bench, A1, B2


def set_frames(bench, addr=None, reg=None):
    out = []
    for f in bench.tap_frames:
        h = P.unpack_id(f.can_id)
        if h['cmd'] != P.CMD_SET or not h['to_psu']:
            continue
        if addr is not None and h['addr'] != addr:
            continue
        if reg is not None and f.data[1] != reg:
            continue
        out.append(f)
    return out


def raw_of(frame):
    return int.from_bytes(frame.data[4:8], 'big')


class ControlBench(Bench):
    """Bench plus a simulated patched DVCC: every simulated second it writes the heartbeat
    counter, /Link/ChargeCurrent and /Link/ChargeVoltage to the services named in `dvcc`.
    Modelled on delegates/managedcharger.py and vedbus as they behave on the Cerbo:
    the budget goes only to a charger whose /State is not 0, every other charger gets an
    explicit 0, and a SetValue that repeats the current value never reaches the driver's
    callback (vedbus returns early on an unchanged value). Both were missing from this
    bench until 2026-11-04, which is why the budget deadlock was first seen on the Cerbo."""

    def __init__(self, **kw):
        kw.setdefault('quiet_start_s', 0.0)
        kw.setdefault('control', True)
        Bench.__init__(self, **kw)
        self.dvcc = {}              # serial -> (current_a, voltage_v)
        self.hb = 0
        self.freeze_heartbeat = False
        self.ignore_state = False   # True: the pre-2026-11-04 bench (budget regardless of /State)
        self._last_dvcc = None

    def _write(self, s, path, value):
        if s[path] != value:
            s.set_value_from_bus(path, value)

    def run(self, seconds, step=0.2):
        n = int(round(seconds / step))
        for _ in range(n):
            self.clock.advance(step)
            if self.dvcc and (self._last_dvcc is None or self.clock() - self._last_dvcc >= 1.0 - 1e-9):
                self._last_dvcc = self.clock()
                if not self.freeze_heartbeat:
                    self.hb += 1
                for serial, (cur, volt) in self.dvcc.items():
                    s = self.backend.services.get('com.victronenergy.charger.huawei_%s' % serial)
                    if s is None:
                        continue
                    self._write(s, '/Huawei/Heartbeat', self.hb)
                    if not self.ignore_state and s['/State'] == 0:
                        cur = 0.0
                    self._write(s, '/Link/ChargeCurrent', cur)
                    self._write(s, '/Link/ChargeVoltage', volt)
            for sim in self.sims:
                sim.tick()
            self.pump()
            self.engine.tick()
            self.pump()

    def enable(self, serial, ccl=40.0):
        s = self.service(serial)
        assert s.set_value_from_bus('/Mode', 1)
        assert s.set_value_from_bus('/Settings/ChargeCurrentLimit', ccl)


class VerifiedCase(unittest.TestCase):
    def setUp(self):
        self._old = P.VERIFIED
        P.VERIFIED = True

    def tearDown(self):
        P.VERIFIED = self._old


class TestGate(unittest.TestCase):
    def setUp(self):
        self._old = P.VERIFIED
        P.VERIFIED = False              # the flag as it was before 2026-11-04

    def tearDown(self):
        P.VERIFIED = self._old

    def test_gate_closed_without_the_flag(self):
        self.assertFalse(P.VERIFIED)
        b = ControlBench()
        b.run(2.0)
        b.enable(A1)
        b.dvcc = {A1: (25.0, 52.4)}
        b.run(10.0)
        self.assertEqual(set_frames(b), [])
        self.assertEqual(b.engine.commander.name, 'telemetry-only')
        self.assertEqual(b.service(A1)['/State'], 0)


class TestChargingPath(VerifiedCase):
    def test_follows_dvcc_with_ramp_keepalive_and_shutdown_to_zero(self):
        b = ControlBench()
        self.assertEqual(b.engine.commander.name, 'can')
        b.run(2.0)
        m = b.engine.registry.modules[A1]
        self.assertTrue(m.default_ok)                                   # first burst: stored default 0
        self.assertEqual(b.service(A1)['/Huawei/DefaultLimitOk'], 1)
        # before any operator/DVCC request the only writes are explicit zeros (on-line limit 0)
        self.assertTrue(len(set_frames(b)) > 0)
        self.assertTrue(all(f.data[1] == 0x03 and raw_of(f) == 0 for f in set_frames(b)))
        b.enable(A1)
        b.dvcc = {A1: (25.0, 52.4)}
        b.run(1.0)
        first = set_frames(b, addr=1, reg=0x03)
        self.assertTrue(len(first) >= 1)
        self.assertTrue(raw_of(first[0]) < P.CURRENT_SCALES['ratio1024'].encode(25.0, 52.0))   # ramp
        b.run(8.0)
        sim = b.sims[0]
        self.assertEqual(sim.online_i_raw, 492)                         # 25 / 52 x 1024
        self.assertAlmostEqual(sim.online_v, 52.4, places=2)
        self.assertEqual(m.current.sent_value, 25.0)
        self.assertTrue(m.current.ack_ok)
        self.assertEqual(m.state, M.READY)
        s1 = b.service(A1)
        self.assertEqual(s1['/State'], 252)
        self.assertAlmostEqual(s1['/Dc/0/Current'], 25.0, places=0)
        self.assertEqual(s1['/Huawei/SentCurrent'], 25.0)
        self.assertEqual(s1['/Huawei/AckOk'], 1)
        self.assertIsNone(s1['/Huawei/LimitingReason'])
        # keep-alive: an unchanged request is re-sent every RESEND_S
        n0 = len(set_frames(b, addr=1, reg=0x03))
        b.run(12.0)
        n1 = len(set_frames(b, addr=1, reg=0x03))
        self.assertTrue(2 <= n1 - n0 <= 3, n1 - n0)
        self.assertTrue(sim.online)
        # unit 2 never asked: only ever held at zero
        self.assertTrue(all(raw_of(f) == 0 for f in set_frames(b, addr=2, reg=0x03)))
        self.assertEqual(b.sims[1].online_i_raw, 0)
        # shutdown: zero to every commanded unit, last frame on the bus
        b.engine.stop()
        b.pump()
        for sim_ in b.sims:
            b.pump()
        last = set_frames(b, addr=1, reg=0x03)[-1]
        self.assertEqual(raw_of(last), 0)
        self.assertEqual(b.sims[0].online_i_raw, 0)
        self.assertEqual(b.sims[0].measure()[1], 0.0)
        self.assertEqual(s1['/State'], 0)
        self.assertEqual(s1['/Connected'], 0)

    def test_two_units_allocated_independently(self):
        b = ControlBench()
        b.run(2.0)
        b.enable(A1)
        b.enable(B2)
        b.dvcc = {A1: (25.0, 52.4), B2: (10.0, 52.4)}
        b.run(10.0)
        self.assertEqual(b.sims[0].online_i_raw, 492)
        self.assertEqual(b.sims[1].online_i_raw, 197)
        self.assertEqual(b.service(A1)['/State'], 252)
        self.assertEqual(b.service(B2)['/State'], 252)
        # unit 2 unplugged: unit 1 keeps its allocation
        gone = b.sims.pop()
        del b.dvcc[B2]
        b.run(M.DISCONNECT_S + 2.0)
        self.assertEqual(b.service(B2)['/Huawei/State'], 'Disconnected')
        self.assertEqual(b.sims[0].online_i_raw, 492)
        self.assertEqual(b.service(A1)['/State'], 252)
        # DVCC takes the budget away (SOC ceiling): zero at once, no ramp down
        b.dvcc = {A1: (0.0, 52.4)}
        b.run(1.2)
        self.assertEqual(b.sims[0].online_i_raw, 0)
        # ... but the unit stays advertised (/State 252, able): the DVCC allocates only to
        # chargers with /State != 0, so a 0 here would be the deadlock of 2026-11-04
        self.assertEqual(b.service(A1)['/State'], 252)
        self.assertEqual(b.service(A1)['/Huawei/State'], 'Inhibited')
        self.assertTrue(b.engine.registry.modules[A1].able)
        # unit 2 returns (restarted, off-line default): verdict kept, allocation resumes
        b.sims.append(gone)
        gone.power_cycle()
        b.dvcc = {A1: (0.0, 52.4), B2: (10.0, 52.4)}
        b.run(8.0)
        self.assertEqual(gone.online_i_raw, 197)
        self.assertEqual(b.service(B2)['/State'], 252)
        # budget back for unit 1: resumes without any help
        b.dvcc = {A1: (25.0, 52.4), B2: (10.0, 52.4)}
        b.run(8.0)
        self.assertEqual(b.sims[0].online_i_raw, 492)
        # operator off: not able, /State 0, and the DVCC's explicit 0 changes nothing
        b.service(A1).set_value_from_bus('/Mode', 4)
        b.run(1.2)
        self.assertEqual(b.service(A1)['/State'], 0)
        self.assertFalse(b.engine.registry.modules[A1].able)

    def test_budget_deadlock_of_2026_11_04_and_unchanged_limit_is_not_stale(self):
        """Cerbo 2026-11-04 17:41: driver restarted in control mode with the generator running,
        DVCC in control, budget 180 A. The DVCC wrote 0 to the charger because its /State was 0,
        the driver kept /State 0 because it had no budget, and after 10 s it also called the
        unchanged 0 "stale". Neither side ever moved."""
        b = ControlBench()
        b.run(2.0)
        b.enable(A1, ccl=50.0)
        b.dvcc = {A1: (50.0, 52.4)}                     # budget available, but only for /State != 0
        b.run(15.0)
        s1 = b.service(A1)
        self.assertEqual(s1['/State'], 252)
        self.assertEqual(b.sims[0].online_i_raw, P.CURRENT_SCALES['ratio1024'].encode(50.0, 52.0))
        self.assertIsNone(s1['/Huawei/LimitingReason'])
        # the limit stays 50 A unchanged (vedbus delivers no repeat): minutes later still charging
        b.run(300.0)
        self.assertEqual(b.sims[0].online_i_raw, P.CURRENT_SCALES['ratio1024'].encode(50.0, 52.0))
        self.assertNotIn('stale', s1['/Huawei/LimitingReason'] or '')
        # the heartbeat, not the limit, is the liveness: freeze it and the unit is zeroed
        b.freeze_heartbeat = True
        b.run(C.HEARTBEAT_TIMEOUT_S + 1.5)
        self.assertEqual(b.sims[0].online_i_raw, 0)
        self.assertIn('heartbeat stale', s1['/Huawei/LimitingReason'])
        self.assertEqual(s1['/State'], 0)
        b.freeze_heartbeat = False
        b.run(12.0)
        self.assertEqual(b.sims[0].online_i_raw, P.CURRENT_SCALES['ratio1024'].encode(50.0, 52.0))


class TestZeroDefaultCheck(VerifiedCase):
    def test_uncommissioned_unit_is_never_written_to(self):
        b = ControlBench()
        b.sims[0].default_i_raw = 500           # unit 1 was never commissioned (B3.0 not done)
        b.run(2.0)
        m = b.engine.registry.modules[A1]
        self.assertFalse(m.default_ok)
        self.assertAlmostEqual(m.default_ratio, 500 / 1024.0, places=3)
        s1 = b.service(A1)
        self.assertEqual(s1['/Huawei/DefaultLimitOk'], 0)
        b.enable(A1)
        b.enable(B2)
        b.dvcc = {A1: (25.0, 52.4), B2: (10.0, 52.4)}
        b.run(10.0)
        self.assertEqual(set_frames(b, addr=1), [])                 # not even a zero
        self.assertTrue(len(set_frames(b, addr=2)) > 0)             # the commissioned one charges
        self.assertEqual(b.sims[1].online_i_raw, 197)
        self.assertIn('NOT zero', s1['/Huawei/LimitingReason'])
        self.assertEqual(s1['/State'], 0)
        b.engine.stop()
        b.pump()
        self.assertEqual(set_frames(b, addr=1), [])                 # shutdown writes nothing to it either


class TestAcknowledgement(VerifiedCase):
    def test_missing_ack_faults_zeroes_and_recovers(self):
        b = ControlBench()
        b.run(2.0)
        b.enable(A1)
        b.dvcc = {A1: (25.0, 52.4)}
        b.run(6.0)
        self.assertEqual(b.sims[0].online_i_raw, 492)
        b.sims[0].drop_acks = True
        b.run(C.RESEND_S + M.ACK_TIMEOUT_S + 1.0)    # next keep-alive goes unacknowledged
        m = b.engine.registry.modules[A1]
        s1 = b.service(A1)
        self.assertEqual(m.state, M.FAULTED)
        self.assertTrue(m.fault.startswith('no ack'))
        self.assertEqual(s1['/Alarms/SetpointError'], 2)
        self.assertEqual(s1['/ErrorCode'], 0)              # never a VE.Direct code (1 = battery temperature)
        self.assertEqual(s1['/State'], 0)
        self.assertTrue(m.counters['ack_timeout'] >= 1)
        self.assertEqual(raw_of(set_frames(b, addr=1, reg=0x03)[-1]), 0)    # driven to zero
        self.assertEqual(b.sims[0].online_i_raw, 0)
        # acks come back: the fault is held FAULT_HOLD_S after the last timeout, then charging resumes
        b.sims[0].drop_acks = False
        b.run(M.FAULT_HOLD_S + C.RESEND_S + 8.0)
        self.assertEqual(m.state, M.READY)
        self.assertEqual(b.sims[0].online_i_raw, 492)
        self.assertEqual(s1['/Alarms/SetpointError'], 0)

    def test_error_ack_faults_and_the_other_unit_is_unaffected(self):
        b = ControlBench()
        b.run(2.0)
        b.enable(A1)
        b.enable(B2)
        b.dvcc = {A1: (25.0, 52.4), B2: (10.0, 52.4)}
        b.run(6.0)
        b.sims[0].reject_online_current = True
        b.run(C.RESEND_S + 2.0)
        m = b.engine.registry.modules[A1]
        self.assertEqual(m.state, M.FAULTED)
        self.assertTrue(m.fault.startswith('ack error'))
        self.assertFalse(m.current.ack_ok)
        self.assertEqual(b.service(A1)['/Alarms/SetpointError'], 2)
        self.assertEqual(b.service(A1)['/State'], 0)
        self.assertEqual(raw_of(set_frames(b, addr=1, reg=0x03)[-1]), 0)
        self.assertEqual(b.sims[1].online_i_raw, 197)
        self.assertEqual(b.service(B2)['/State'], 252)


class TestVerificationByMeasurement(VerifiedCase):
    def test_wrong_scale_shows_as_overshoot_and_is_zeroed(self):
        # the driver believes x30, the unit obeys ratio1024: a 10 A request becomes 15.2 A
        b = ControlBench(scales={A1: 'x30', B2: 'ratio1024'})
        b.run(2.0)
        b.enable(A1)
        b.dvcc = {A1: (10.0, 52.4)}
        b.run(3.0)                                  # ramp to 10 A (5 A/s) and send
        self.assertEqual(raw_of(set_frames(b, addr=1, reg=0x03)[-1]), 300)
        self.assertAlmostEqual(b.sims[0].measure()[1], 15.23, places=1)
        b.run(M.SETTLE_S + 1.5)
        m = b.engine.registry.modules[A1]
        self.assertEqual(m.state, M.FAULTED)
        self.assertTrue(m.fault.startswith('overshoot'), m.fault)
        self.assertEqual(m.counters['overshoot'], 1)
        s1 = b.service(A1)
        self.assertEqual(s1['/Alarms/SetpointError'], 2)
        self.assertEqual(s1['/State'], 0)
        self.assertEqual(raw_of(set_frames(b, addr=1, reg=0x03)[-1]), 0)
        b.run(1.0)
        self.assertEqual(b.sims[0].measure()[1], 0.0)

    def test_current_below_the_limit_is_normal(self):
        b = ControlBench()
        b.run(2.0)
        b.sims[0].battery_v = 52.3                  # nearly full: takes 2 A at 52.4 V
        b.enable(A1)
        b.dvcc = {A1: (25.0, 52.4)}
        b.run(12.0)
        m = b.engine.registry.modules[A1]
        self.assertEqual(m.state, M.READY)
        self.assertTrue(b.service(A1)['/Dc/0/Current'] < 3.0)
        self.assertEqual(m.counters['overshoot'], 0)


class TestFailsafe(VerifiedCase):
    def test_exception_in_the_loop_zeroes_everything_and_stops(self):
        b = ControlBench()
        b.run(2.0)
        b.enable(A1)
        b.dvcc = {A1: (25.0, 52.4)}
        b.run(8.0)
        self.assertEqual(b.sims[0].online_i_raw, 492)

        def boom(*a, **k):
            raise RuntimeError('simulated bug in the governor')
        b.engine.governors[A1].compute = boom
        with self.assertRaises(EngineFailure):
            b.run(0.2)
        b.pump()
        self.assertEqual(b.engine.stats['failsafe'], 1)
        self.assertTrue(b.engine.summary()['failed'].startswith('tick'))
        self.assertEqual(raw_of(set_frames(b, addr=1, reg=0x03)[-1]), 0)
        self.assertEqual(b.sims[0].online_i_raw, 0)
        s1 = b.service(A1)
        self.assertEqual(s1['/Connected'], 0)
        self.assertEqual(s1['/State'], 0)
        # afterwards the engine is inert: no polling, no requests, so the unit's own fallback applies
        n = len([f for f in b.tap_frames if P.unpack_id(f.can_id)['to_psu']])
        b.run(5.0)
        self.assertEqual(len([f for f in b.tap_frames if P.unpack_id(f.can_id)['to_psu']]), n)
        self.assertEqual(b.engine.stats['failsafe'], 1)            # idempotent

    def test_exception_in_on_frame_is_handled_the_same_way(self):
        b = ControlBench()
        b.run(2.0)
        b.engine.registry.feed = lambda dec: (_ for _ in ()).throw(RuntimeError('decoder bug'))
        with self.assertRaises(EngineFailure):
            b.run(0.2)
        self.assertEqual(b.engine.stats['failsafe'], 1)
        self.assertEqual(b.service(A1)['/Connected'], 0)


class TestOutputEnable(VerifiedCase):
    """Cold-started unit: output stage off until told otherwise (policy pending the bench)."""

    def bench(self, policy):
        from huawei.control import make_commander
        b = ControlBench()
        b.engine.commander = make_commander(b.clock, True, policy)
        for sim in b.sims:
            sim.standby = True                   # cold start: output off
        return b

    def test_policy_none_writes_only_setpoints(self):
        b = self.bench('none')
        b.run(2.0)
        b.enable(A1)
        b.dvcc = {A1: (25.0, 52.4)}
        b.run(8.0)
        self.assertEqual(set_frames(b, addr=1, reg=0x32), [])
        self.assertEqual(b.sims[0].online_i_raw, 492)              # limit accepted ...
        self.assertEqual(b.sims[0].measure()[1], 0.0)              # ... but the output stays off
        m = b.engine.registry.modules[A1]
        self.assertEqual(m.state, M.READY)                         # below the limit is normal
        self.assertIsNone(m.output.sent_value)

    def test_policy_standby0_enables_before_the_first_limit_and_disables_on_zero(self):
        b = self.bench('standby0')
        b.run(2.0)
        b.enable(A1)
        b.dvcc = {A1: (25.0, 52.4)}
        b.run(8.0)
        sim = b.sims[0]
        frames = set_frames(b, addr=1)
        regs = [f.data[1] for f in frames]
        first_nonzero_current = next(i for i, f in enumerate(frames) if f.data[1] == 0x03 and raw_of(f) > 0)
        first_on = next(i for i, f in enumerate(frames) if f.data[1] == 0x32 and f.data[3] == 0)
        self.assertTrue(first_on < first_nonzero_current)
        self.assertTrue(regs.index(0x00) < first_on)                # voltage first
        self.assertFalse(sim.standby)
        self.assertAlmostEqual(sim.measure()[1], 25.0, places=0)
        m = b.engine.registry.modules[A1]
        self.assertEqual(m.output.sent_value, 0)
        self.assertTrue(m.output.ack_ok)
        # keep-alive repeats the "on" value
        n0 = len([f for f in set_frames(b, addr=1, reg=0x32) if f.data[3] == 0])
        b.run(12.0)
        self.assertTrue(len([f for f in set_frames(b, addr=1, reg=0x32) if f.data[3] == 0]) > n0)
        # request to zero: current 0 and standby "off"
        b.dvcc = {A1: (0.0, 52.4)}
        b.run(1.2)
        self.assertEqual(sim.online_i_raw, 0)
        self.assertTrue(sim.standby)
        self.assertEqual(m.output.sent_value, 1)
        self.assertEqual(sim.measure()[1], 0.0)
        # charging again, then shutdown writes zero and "off"
        b.dvcc = {A1: (25.0, 52.4)}
        b.run(8.0)
        self.assertFalse(sim.standby)
        b.engine.stop()
        b.pump()
        tail = [(f.data[1], f.data[3] if f.data[1] == 0x32 else raw_of(f)) for f in set_frames(b, addr=1)[-2:]]
        self.assertEqual(tail, [(0x03, 0), (0x32, 1)])
        self.assertTrue(sim.standby)

    def test_policy_standby1_uses_the_opposite_values(self):
        b = self.bench('standby1')
        b.run(2.0)
        b.enable(A1)
        b.dvcc = {A1: (25.0, 52.4)}
        b.run(8.0)
        writes = [f.data[3] for f in set_frames(b, addr=1, reg=0x32)]
        self.assertEqual(writes[0], 0)                             # the explicit zero at start: "off" = 0
        self.assertEqual(writes[-1], 1)                            # charging: "on" = 1 (the sim models the other polarity)
        self.assertEqual(b.engine.registry.modules[A1].output.sent_value, 1)

    def test_unknown_policy_refused(self):
        from huawei.control import make_commander, CanCommander
        with self.assertRaises(ValueError):
            make_commander(FakeClock(0.0), True, 'standby2')
        with self.assertRaises(ValueError):
            CanCommander(FakeClock(0.0), 'yes')


class TestOperatorPaths(VerifiedCase):
    def test_mode_off_cap_and_heartbeat_loss(self):
        b = ControlBench()
        b.run(2.0)
        b.enable(A1)
        b.dvcc = {A1: (25.0, 52.4)}
        b.run(8.0)
        sim = b.sims[0]
        s1 = b.service(A1)
        self.assertEqual(sim.online_i_raw, 492)
        # operator off: zero within a tick, no ramp down
        s1.set_value_from_bus('/Mode', 4)
        b.run(0.4)
        self.assertEqual(sim.online_i_raw, 0)
        self.assertIn('operator switch off', s1['/Huawei/LimitingReason'])
        self.assertEqual(s1['/State'], 0)
        # operator on again: ramps back up
        s1.set_value_from_bus('/Mode', 1)
        b.run(1.0)
        self.assertTrue(0 < sim.online_i_raw < 492)
        b.run(6.0)
        self.assertEqual(sim.online_i_raw, 492)
        # per-unit cap lowered while charging: limit follows at once
        s1.set_value_from_bus('/Settings/ChargeCurrentLimit', 15.0)
        b.run(0.4)
        self.assertEqual(sim.online_i_raw, 295)                           # 15 / 52 x 1024
        self.assertIn('capped at 15.0 A', s1['/Huawei/LimitingReason'])
        self.assertEqual(s1['/State'], 252)
        # DVCC heartbeat stops (DVCC still writes the limit, the counter does not change)
        b.freeze_heartbeat = True
        b.run(C.HEARTBEAT_TIMEOUT_S + 1.5)
        self.assertEqual(sim.online_i_raw, 0)
        self.assertIn('stale', s1['/Huawei/LimitingReason'])
        self.assertEqual(s1['/State'], 0)
        b.freeze_heartbeat = False
        b.run(5.0)
        self.assertTrue(sim.online_i_raw > 0)


if __name__ == '__main__':
    unittest.main()


class TestExcludedUnit(VerifiedCase):
    """driver.json "excluded" (2026-11-04): unit 1 caps itself at ~1.65 A and must not take part."""

    def test_excluded_unit_gets_no_frames_while_the_other_is_driven(self):
        b = ControlBench()
        b.engine.registry.excluded = {A1}
        b.run(2.0)
        m1 = b.engine.registry.modules[A1]
        self.assertTrue(m1.excluded)
        b.enable(A1)
        b.enable(B2)
        b.dvcc = {A1: (25.0, 52.4), B2: (25.0, 52.4)}
        b.run(10.0)
        self.assertEqual(set_frames(b, addr=1), [])                     # nothing, not even a zero
        self.assertEqual(b.sims[1].online_i_raw, 492)                   # the other unit is driven
        s1 = b.service(A1)
        self.assertIn('excluded', s1['/Huawei/LimitingReason'])
        self.assertEqual(s1['/State'], 0)
        self.assertEqual(s1['/Huawei/RequestedCurrent'], 0.0)
        self.assertEqual(b.service(B2)['/State'], 252)
        # the bench window opens for the excluded unit even in control mode (ramp tool on unit 1)
        b.engine.bench_age = lambda: 1.0
        b.run(1.0)
        self.assertEqual(s1['/Huawei/BenchWindow'], 1)
        self.assertEqual(b.service(B2)['/Huawei/BenchWindow'], 0)
        self.assertFalse(b.engine.summary()['bench_open'])
        # shutdown: the excluded unit is not written to either
        n = len(set_frames(b, addr=1))
        b.engine.stop()
        b.pump()
        self.assertEqual(len(set_frames(b, addr=1)), n)
        self.assertEqual(b.sims[1].online_i_raw, 0)


class TestPersistedMode(VerifiedCase):
    def test_mode_is_stored_in_settings_and_restored_at_start(self):
        from huawei.dbus_services import ChargerService, FakeBackend
        from huawei.control import Inputs
        b = ControlBench()
        b.run(2.0)
        s1 = b.service(A1)
        svc = b.engine.services[A1]
        self.assertEqual(s1['/Mode'], 4)                                 # default: off
        self.assertEqual(svc.settings['mode'], 4)
        self.assertTrue(s1.set_value_from_bus('/Mode', 1))
        self.assertEqual(svc.settings['mode'], 1)                        # persisted
        self.assertTrue(s1.set_value_from_bus('/Mode', '4'))              # the dbus tool sends strings
        self.assertEqual(svc.settings['mode'], 4)
        self.assertFalse(s1.set_value_from_bus('/Mode', 2))

        class Stored(FakeBackend):
            def make_settings(self, supported, callback):
                fs = FakeBackend.make_settings(self, supported, callback)
                if 'mode' in fs:
                    fs['mode'] = 1                                       # what localsettings hands back
                return fs
        inputs = Inputs()
        cs = ChargerService(b.engine.registry.modules[A1], inputs, b.wd, Stored())
        self.assertTrue(inputs.mode_on)
        self.assertEqual(cs.svc['/Mode'], 1)


class TestSlowWindDown(VerifiedCase):
    def test_no_overshoot_verdict_while_a_lower_limit_takes_effect(self):
        # the R4875G5 reported 1.7 A for ~20 s after a 1 A limit (2026-11-03); the sim is told to
        # wind down slowly as well
        b = ControlBench()
        b.run(2.0)
        b.enable(A1)
        b.dvcc = {A1: (30.0, 52.4)}
        b.run(10.0)
        m = b.engine.registry.modules[A1]
        self.assertEqual(m.current.sent_value, 30.0)
        sim = b.sims[0]
        sim.wind_down_s = 20.0
        b.dvcc = {A1: (10.0, 52.4)}
        b.run(5.0)
        self.assertEqual(m.current.sent_value, 10.0)
        self.assertIsNotNone(m.current.down_t)
        self.assertTrue(b.service(A1)['/Dc/0/Current'] > 12.0)          # still winding down
        self.assertIsNone(m.fault)                                       # no "overshoot" for it
        b.run(30.0)
        self.assertIsNone(m.fault)
        self.assertAlmostEqual(b.service(A1)['/Dc/0/Current'], 10.0, places=0)
