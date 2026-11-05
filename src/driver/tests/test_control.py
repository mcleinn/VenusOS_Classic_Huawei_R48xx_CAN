import os
import tempfile
import unittest

import context
from context import FakeClock
from huawei import protocol as P
from huawei import modules as M
from huawei import control as C
from huawei.canbus import VirtualBus


class TestWatchdog(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(0.0)
        self.marker = os.path.join(tempfile.mkdtemp(), 'dvcc_unsupported')
        self.wd = C.DvccWatchdog(self.clock, marker_path=self.marker)

    def test_alive_only_with_changing_heartbeat_and_control_flag(self):
        self.assertFalse(self.wd.alive())
        self.assertEqual(self.wd.reason(), 'patched DVCC not in control (/Control/ChargerChargeCurrent != 1)')
        self.wd.feed_control_flag(1)
        self.assertEqual(self.wd.reason(), 'no DVCC heartbeat yet')
        self.wd.feed_heartbeat(1)
        self.assertTrue(self.wd.alive())
        self.clock.advance(3.0)
        self.wd.feed_heartbeat(1)              # unchanged: does not count
        self.clock.advance(8.0)
        self.assertFalse(self.wd.alive())
        self.assertIn('stale', self.wd.reason())
        self.wd.feed_heartbeat(2)
        self.assertTrue(self.wd.alive())
        self.assertEqual(self.wd.beats, 2)

    def test_marker_file_blocks(self):
        self.wd.feed_control_flag(1)
        self.wd.feed_heartbeat(5)
        self.assertTrue(self.wd.alive())
        open(self.marker, 'w').close()
        self.assertFalse(self.wd.alive())
        self.assertIn('marker', self.wd.reason())


class TestGovernor(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(0.0)
        self.wd = C.DvccWatchdog(self.clock, marker_path=os.path.join(tempfile.mkdtemp(), 'x'))
        self.wd.feed_control_flag(1)
        self.wd.feed_heartbeat(1)
        self.m = M.Module('0000000000A1', self.clock)
        self.m.addr = 1
        self.m.nominal_a = 52.0
        self.m.scale = 'ratio1024'
        self.m.last_frame_t = 0.0
        self.m.state = M.INHIBITED
        self.m.default_ok = True            # stored default seen as zero
        self.inputs = C.Inputs()
        self.inputs.mode_on = True
        self.inputs.ccl = 40.0
        self.inputs.link_current = 30.0
        self.inputs.link_current_t = 0.0
        self.inputs.link_voltage = 52.4

    def test_unverified_protocol_always_yields_zero(self):
        old = P.VERIFIED
        P.VERIFIED = False              # the gate as it was before 2026-11-04
        try:
            gov = C.Governor(self.clock, control_enabled=True, watchdog=self.wd)
            req = gov.compute(self.m, self.inputs)
        finally:
            P.VERIFIED = old
        self.assertEqual(req.current_a, 0.0)
        self.assertFalse(req.enable)
        self.assertIn('protocol unverified (Phase B pending)', req.reasons)
        self.assertTrue(self.m.inhibit)

    def test_with_verified_protocol_the_other_gates_apply(self):
        old = P.VERIFIED
        P.VERIFIED = True
        try:
            gov = C.Governor(self.clock, control_enabled=True, watchdog=self.wd, ramp_a_per_s=1000)
            req = gov.compute(self.m, self.inputs)
            self.assertTrue(req.enable)
            self.assertEqual(req.current_a, 30.0)
            self.assertEqual(req.voltage_v, 52.4)
            self.assertFalse(self.m.inhibit)
            # per-unit cap
            self.inputs.ccl = 20.0
            req = gov.compute(self.m, self.inputs)
            self.assertEqual(req.current_a, 20.0)
            self.assertIn('capped at 20.0 A', req.reasons)
            self.assertTrue(req.enable)
            # operator off
            self.inputs.mode_on = False
            req = gov.compute(self.m, self.inputs)
            self.assertEqual(req.current_a, 0.0)
            self.assertIn('operator switch off', req.reasons)
            self.inputs.mode_on = True
            # watchdog stale
            self.clock.advance(20.0)
            self.inputs.link_current_t = self.clock()
            req = gov.compute(self.m, self.inputs)
            self.assertEqual(req.current_a, 0.0)
            self.assertTrue(any('stale' in r for r in req.reasons))
            self.wd.feed_heartbeat(2)
            # no scale bound
            self.m.scale = None
            req = gov.compute(self.m, self.inputs)
            self.assertEqual(req.current_a, 0.0)
            self.m.scale = 'ratio1024'
            # unit not ready
            self.m.state = M.STANDBY
            req = gov.compute(self.m, self.inputs)
            self.assertIn('unit standby', req.reasons)
            self.m.state = M.INHIBITED
            # voltage clamp (advance the clock so the ramp allows current again)
            self.clock.advance(1.0)
            self.inputs.link_current_t = self.clock()
            self.inputs.link_voltage = 60.0
            req = gov.compute(self.m, self.inputs)
            self.assertEqual(req.voltage_v, C.VOLTAGE_MAX_V)
            self.assertTrue(req.enable)
        finally:
            P.VERIFIED = old

    def test_ramp_up_limited_down_immediate(self):
        old = P.VERIFIED
        P.VERIFIED = True
        try:
            gov = C.Governor(self.clock, control_enabled=True, watchdog=self.wd, ramp_a_per_s=5.0)
            req = gov.compute(self.m, self.inputs)
            self.assertEqual(req.current_a, 30.0)      # first tick: no history, no ramp
            self.inputs.link_current = 0.0
            req = gov.compute(self.m, self.inputs)
            self.assertEqual(req.current_a, 0.0)
            self.inputs.link_current = 30.0
            self.clock.advance(1.0)
            self.inputs.link_current_t = self.clock()
            req = gov.compute(self.m, self.inputs)
            self.assertEqual(req.current_a, 5.0)
            self.assertIn('ramping', req.reasons)
            self.clock.advance(1.0)
            self.inputs.link_current_t = self.clock()
            self.assertEqual(gov.compute(self.m, self.inputs).current_a, 10.0)
        finally:
            P.VERIFIED = old

    def test_control_disabled_is_a_reason_even_when_verified(self):
        old = P.VERIFIED
        P.VERIFIED = True
        try:
            gov = C.Governor(self.clock, control_enabled=False, watchdog=self.wd)
            req = gov.compute(self.m, self.inputs)
            self.assertEqual(req.current_a, 0.0)
            self.assertIn('control disabled', req.reasons)
        finally:
            P.VERIFIED = old


class TestCommanders(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(0.0)
        self.bus = VirtualBus()
        self.link = self.bus.attach('driver')
        self.tap = self.bus.attach('tap')
        self.m = M.Module('0000000000A1', self.clock)
        self.m.addr = 1
        self.m.nominal_a = 52.0
        self.m.scale = 'ratio1024'
        self.m.default_ok = True
        self.m.last_frame_t = self.clock()      # alive

    def test_can_commander_writes_nothing_without_the_zero_default_verdict(self):
        old = P.VERIFIED
        P.VERIFIED = True
        try:
            cmd = C.CanCommander(self.clock)
            for verdict in (None, False):
                self.m.default_ok = verdict
                self.assertEqual(cmd.apply(self.m, C.Request(0.0, 52.4, False, []), self.link), [])
                self.assertEqual(cmd.apply(self.m, C.Request(10.0, 52.4, True, []), self.link), [])
                self.assertEqual(cmd.shutdown([self.m], self.link), [])
                self.assertIsNone(self.tap.recv())
            gov = C.Governor(self.clock, control_enabled=True, watchdog=self.wd_for_gov())
            self.m.default_ok = False
            req = gov.compute(self.m, self.inputs_for_gov())
            self.assertTrue(any('NOT zero' in r for r in req.reasons))
        finally:
            P.VERIFIED = old

    def wd_for_gov(self):
        wd = C.DvccWatchdog(self.clock, marker_path=os.path.join(tempfile.mkdtemp(), 'x'))
        wd.feed_control_flag(1)
        wd.feed_heartbeat(1)
        return wd

    def inputs_for_gov(self):
        i = C.Inputs()
        i.mode_on, i.ccl, i.link_current, i.link_current_t, i.link_voltage = True, 40.0, 30.0, self.clock(), 52.4
        self.m.last_frame_t = self.clock()
        self.m.state = M.INHIBITED
        return i

    def test_make_commander_is_telemetry_only_while_unverified(self):
        old = P.VERIFIED
        P.VERIFIED = False              # the gate as it was before 2026-11-04
        try:
            self.assertEqual(C.make_commander(self.clock, control_enabled=True).name, 'telemetry-only')
            self.assertEqual(C.make_commander(self.clock, control_enabled=False).name, 'telemetry-only')
            with self.assertRaises(RuntimeError):
                C.CanCommander(self.clock)
        finally:
            P.VERIFIED = old
        # verified: control only when requested
        self.assertEqual(C.make_commander(self.clock, control_enabled=False).name, 'telemetry-only')
        self.assertEqual(C.make_commander(self.clock, control_enabled=True).name, 'can')

    def test_telemetry_only_never_transmits(self):
        cmd = C.TelemetryOnlyCommander(self.clock)
        req = C.Request(10.0, 52.4, True, [])
        self.assertEqual(cmd.apply(self.m, req, self.link), [])
        self.assertEqual(cmd.shutdown([self.m], self.link), [])
        self.assertIsNone(self.tap.recv())
        self.assertEqual(self.m.current.requested, 10.0)
        self.assertIsNone(self.m.current.sent_value)
        self.assertEqual(len(cmd.would_send), 1)

    def test_can_commander_when_verified(self):
        old = P.VERIFIED
        P.VERIFIED = True
        try:
            cmd = C.CanCommander(self.clock)
            self.assertEqual(C.make_commander(self.clock, True).name, 'can')
            frames = cmd.apply(self.m, C.Request(10.0, 52.4, True, []), self.link)
            self.assertEqual(len(frames), 2)
            f = self.tap.recv()
            self.assertEqual(f.can_id, P.request_id(P.CMD_SET, 1))
            self.assertEqual(f.data[1], 0x00)                              # voltage first
            f = self.tap.recv()
            self.assertEqual(f.data[1], 0x03)
            self.assertEqual(int.from_bytes(f.data[4:8], 'big'), 197)      # 10/52 x 1024
            self.assertEqual(self.m.current.sent_value, 10.0)
            self.assertIsNone(self.m.current.ack_ok)
            # unchanged request within RESEND_S: nothing
            self.clock.advance(1.0)
            self.m.last_frame_t = self.clock()
            self.assertEqual(cmd.apply(self.m, C.Request(10.0, 52.4, True, []), self.link), [])
            self.clock.advance(C.RESEND_S)
            self.m.last_frame_t = self.clock()
            self.assertEqual(len(cmd.apply(self.m, C.Request(10.0, 52.4, True, []), self.link)), 2)
            # out-of-range voltage is not sent
            self.clock.advance(C.RESEND_S)
            self.m.last_frame_t = self.clock()
            frames = cmd.apply(self.m, C.Request(10.0, 60.0, True, []), self.link)
            self.assertEqual([f.data[1] for f in frames], [0x03])
            # shutdown writes zero
            z = cmd.shutdown([self.m], self.link)
            self.assertEqual(int.from_bytes(z[0].data[4:8], 'big'), 0)
            # never a non-volatile register
            self.assertTrue(all(f.data[1] in (0x00, 0x03) for f in cmd.sent))
        finally:
            P.VERIFIED = old


if __name__ == '__main__':
    unittest.main()
