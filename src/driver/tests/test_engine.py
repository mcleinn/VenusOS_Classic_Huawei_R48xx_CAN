"""End-to-end on the virtual bus: engine + fake D-Bus backend against two simulated
rectifiers, driven by a fake clock."""
import os
import tempfile
import unittest

import context
from context import FakeClock
from huawei import protocol as P
from huawei import modules as M
from huawei.canbus import VirtualBus, CandumpWriter, read_candump
from huawei.control import DvccWatchdog, make_commander
from huawei.dbus_services import FakeBackend
from huawei.engine import Engine
from psu_sim import PsuSim

A1, B2 = '0000000000A1', '0000000000B2'


class Bench(object):
    """Two PSUs on a virtual bus, the driver on a third link, a tap on a fourth."""

    def __init__(self, scales=None, expected=2, capture=None, dcsource=False, sims=2,
                 quiet_start_s=0.0, voltage_in_0x85=False, model='R4850G2', control=False):
        # control=False: telemetry-only, as the Cerbo runs without /data/huawei/control-enabled
        # (the gate used to be protocol.VERIFIED, True since 2026-11-04; ControlBench asks for control)
        self.clock = FakeClock(1000.0)
        self.bus = VirtualBus()
        self.link = self.bus.attach('driver')
        self.tap = self.bus.attach('tap')
        self.sims = []
        for n, serial in list(enumerate([A1, B2], 1))[:sims]:
            # commissioned units: offline default current 0 A (brief section 6), so a unit never
            # charges unless commanded; the uncommanded-output test overrides this
            sim = PsuSim(self.bus.attach('psu%d' % n), self.clock, addr=n, serial_hex=serial, battery_v=50.0,
                         default_current_a=0.0, voltage_in_0x85=voltage_in_0x85, model=model)
            self.sims.append(sim)
        self.backend = FakeBackend()
        self.marker = os.path.join(tempfile.mkdtemp(), 'dvcc_unsupported')
        self.wd = DvccWatchdog(self.clock, marker_path=self.marker)
        self.wd.feed_control_flag(1)
        self.engine = Engine(self.link, self.clock, M.Registry(self.clock, scales or {A1: 'ratio1024', B2: 'ratio1024'}),
                             self.wd, make_commander(self.clock, control), backend=self.backend,
                             expected_units=expected, control_enabled=control, capture=capture, dcsource=dcsource,
                             quiet_start_s=quiet_start_s)
        self.tap_frames = []

    def pump(self):
        """Deliver every pending frame to its receivers until the bus is quiet."""
        moved = True
        while moved:
            moved = False
            for sim in self.sims:
                f = sim.link.recv()
                while f is not None:
                    sim.handle(f)
                    moved = True
                    f = sim.link.recv()
            f = self.link.recv()
            while f is not None:
                self.engine.on_frame(f)
                moved = True
                f = self.link.recv()
            f = self.tap.recv()
            while f is not None:
                self.tap_frames.append(f)
                f = self.tap.recv()

    def run(self, seconds, step=0.2):
        n = int(round(seconds / step))
        for _ in range(n):
            self.clock.advance(step)
            for sim in self.sims:
                sim.tick()
            self.pump()
            self.engine.tick()
            self.pump()

    def service(self, serial):
        return self.backend.services['com.victronenergy.charger.huawei_%s' % serial]


class TestDiscoveryAndTelemetry(unittest.TestCase):
    def test_two_units_discovered_and_published(self):
        b = Bench()
        b.run(3.0)
        reg = b.engine.registry
        self.assertEqual(sorted(reg.by_addr.items()), [(1, A1), (2, B2)])
        self.assertEqual(reg.modules[A1].nominal_a, 52.0)
        s1 = b.service(A1)
        self.assertEqual(s1['/Serial'], A1)
        self.assertEqual(s1['/Mgmt/ProcessName'], 'huawei-charger')
        self.assertEqual(s1['/Huawei/State'], 'Inhibited')
        self.assertEqual(s1['/State'], 0)                       # never available pre-Phase-B
        self.assertAlmostEqual(s1['/Dc/0/Voltage'], 50.0, places=1)     # 0 A limit: output sits at battery voltage
        self.assertEqual(s1['/Ac/In/L1/V'], 230.0)
        self.assertEqual(s1['/Huawei/CurrentScale'], 'ratio1024')
        self.assertEqual(s1['/Huawei/Address'], 1)
        self.assertEqual(s1['/Connected'], 1)
        self.assertTrue(s1['/Huawei/LimitingReason'].startswith('control disabled'))

    def test_no_set_frame_ever_leaves_the_driver(self):
        b = Bench()
        s1 = None
        b.run(2.0)
        s1 = b.service(A1)
        s1.set_value_from_bus('/Mode', 1)
        s1.set_value_from_bus('/Settings/ChargeCurrentLimit', 40.0)
        for _ in range(10):
            s1.set_value_from_bus('/Huawei/Heartbeat', b.wd.beats + 1)
            s1.set_value_from_bus('/Link/ChargeCurrent', 25.0)
            s1.set_value_from_bus('/Link/ChargeVoltage', 52.4)
            b.run(1.0)
        sets = [f for f in b.tap_frames if P.unpack_id(f.can_id)['cmd'] in (P.CMD_SET, P.CMD_CFG)
                and P.unpack_id(f.can_id)['to_psu']]
        self.assertEqual(sets, [])
        self.assertEqual(sum(s.stats['sets'] for s in b.sims), 0)
        self.assertEqual(b.engine.commander.name, 'telemetry-only')
        self.assertEqual(s1['/Huawei/RequestedCurrent'], 0.0)
        self.assertIsNone(s1['/Huawei/SentCurrent'])
        self.assertTrue(b.wd.alive())
        self.assertIsNotNone(s1['/Huawei/CommandAgeSeconds'])

    def test_polling_is_per_address_and_about_one_hz(self):
        b = Bench()
        b.run(5.0)
        reqs = [f for f in b.tap_frames if P.unpack_id(f.can_id)['cmd'] == P.CMD_DATA and P.unpack_id(f.can_id)['to_psu']]
        addrs = set(P.unpack_id(f.can_id)['addr'] for f in reqs)
        self.assertEqual(addrs, {1, 2})               # never broadcast status
        self.assertTrue(8 <= len(reqs) <= 12, len(reqs))
        self.assertTrue(b.engine.registry.modules[A1].status_fresh())

    def test_model_from_elabel_requested_once(self):
        b = Bench(model='R4875G5')
        b.run(3.0)
        self.assertEqual(b.engine.registry.modules[A1].model, 'R4875G5')
        self.assertEqual(b.service(A1)['/Huawei/Model'], 'R4875G5')
        self.assertEqual(b.service(A1)['/ProductName'], 'Huawei R4875G5 rectifier')
        b.run(20.0)
        self.assertEqual(sum(s.stats['elabel'] for s in b.sims), 2)       # once per unit, no retries

    def test_voltage_from_0x85_on_an_r4875g5(self):
        b = Bench(voltage_in_0x85=True, model='R4875G5')
        b.run(3.0)
        s1 = b.service(A1)
        self.assertAlmostEqual(s1['/Dc/0/Voltage'], 50.0, places=1)
        self.assertEqual(s1['/Huawei/VoltageSource'], '0x85')
        self.assertIsNone(s1['/Huawei/MeasuredLimit'])                     # 0x76 not a readback here
        self.assertIsNotNone(s1['/Huawei/LimitRatio'])
        gets = [f for f in b.tap_frames if P.unpack_id(f.can_id)['cmd'] == P.CMD_GET and P.unpack_id(f.can_id)['to_psu']]
        self.assertTrue(all(f.data[1] == 0x85 for f in gets))
        self.assertTrue(len(gets) >= 4)                                    # one per completed burst
        b2 = Bench()                                                       # a unit with a live 0x75: no GETs
        b2.run(3.0)
        self.assertEqual([f for f in b2.tap_frames if P.unpack_id(f.can_id)['cmd'] == P.CMD_GET and P.unpack_id(f.can_id)['to_psu']], [])
        self.assertEqual(b2.service(A1)['/Huawei/VoltageSource'], '0x75')

    def test_quiet_start_holds_every_request_until_the_fallback_window_passed(self):
        # B3.4: polling keeps a unit on-line. A previous master left 10 A on-line 5 s ago; the
        # driver must stay silent until the unit has fallen back, then poll.
        b = Bench(quiet_start_s=20.0)
        sim = b.sims[0]
        sim.online = True
        sim.online_i_raw = 197                  # 10 A under ratio1024 at 52 A
        sim.last_cmd_t = b.clock()              # last master frame right now
        sim.timeout_s = 12.0
        def from_driver():
            return [f for f in b.tap_frames if P.unpack_id(f.can_id)['to_psu']]
        b.run(10.0)
        self.assertEqual(from_driver(), [])     # nothing at all on the bus from the driver
        self.assertTrue(sim.online)             # it would have been kept on-line by any request
        self.assertTrue(b.engine.held_requests > 0)
        self.assertEqual(b.backend.services, {})
        b.run(5.0)                              # t = 15 s: fallback happened on its own at 12 s
        self.assertFalse(sim.online)
        self.assertEqual(sim.stats['reverts'], 1)
        self.assertEqual(from_driver(), [])
        b.run(8.0)                              # t = 23 s: quiet window over, discovery + polling
        self.assertTrue(len(from_driver()) > 0)
        self.assertEqual(sorted(b.engine.registry.by_addr.items()), [(1, A1), (2, B2)])
        self.assertEqual(sim.online_i_raw, sim.default_i_raw)      # on-line = the zero default
        self.assertTrue(b.engine.registry.modules[A1].status_fresh())
        self.assertEqual(b.engine.summary()['quiet_start_s'], 20.0)

    def test_capture_file_written(self):
        path = os.path.join(tempfile.mkdtemp(), 'cap.log')
        cap = CandumpWriter(path)
        b = Bench(capture=cap)
        b.run(2.0)
        cap.close()
        frames = list(read_candump(path))
        self.assertTrue(len(frames) > 20)
        self.assertTrue(any(P.unpack_id(f.can_id)['cmd'] == P.CMD_INFO for f in frames))

    def test_dcsource_mirror(self):
        b = Bench(dcsource=True)
        b.run(2.0)
        d = b.backend.services['com.victronenergy.dcsource.huawei_%s' % A1]
        self.assertEqual(d['/Settings/MonitorMode'], -2)
        self.assertEqual(d['/Dc/0/Voltage'], b.service(A1)['/Dc/0/Voltage'])


class TestFailureBehaviour(unittest.TestCase):
    def test_unit_disconnect_and_return(self):
        b = Bench()
        b.run(2.0)
        s2 = b.service(B2)
        self.assertEqual(s2['/Huawei/State'], 'Inhibited')
        gone = b.sims.pop()                 # unit 2 unplugged
        b.run(M.DISCONNECT_S + 1.0)
        self.assertEqual(s2['/Huawei/State'], 'Disconnected')
        self.assertEqual(s2['/Connected'], 0)
        self.assertEqual(s2['/Alarms/CommunicationLost'], 2)
        self.assertIsNone(s2['/Dc/0/Voltage'])
        b.sims.append(gone)
        b.run(2.0)
        self.assertEqual(s2['/Huawei/State'], 'Inhibited')
        self.assertEqual(s2['/Connected'], 1)

    def test_ac_loss_shows_standby(self):
        b = Bench()
        b.run(2.0)
        b.sims[0].set_ac(False)
        b.run(1.5)
        self.assertEqual(b.service(A1)['/Huawei/State'], 'Standby')
        self.assertEqual(b.service(A1)['/Dc/0/Current'], 0.0)
        b.sims[0].set_ac(True)
        b.run(1.5)
        self.assertEqual(b.service(A1)['/Huawei/State'], 'Inhibited')

    def test_uncommanded_output_alarm(self):
        # the simulated unit sits on a non-zero offline default and the battery is low:
        # it charges although the driver never asked. That is the B3 FAIL case.
        b = Bench()
        b.sims[0].battery_v = 48.0
        b.sims[0].default_i_raw = 500          # offline default NOT zero: the B3.0 FAIL case
        b.run(2.0)
        s1 = b.service(A1)
        self.assertEqual(s1['/Huawei/State'], 'Faulted')
        self.assertEqual(s1['/Alarms/UncommandedOutput'], 2)
        self.assertEqual(s1['/ErrorCode'], 0)
        self.assertTrue(s1['/Dc/0/Current'] > 1.0)
        self.assertEqual(s1['/State'], 0)

    def test_bench_window_suspends_the_uncommanded_check_for_telemetry_only(self):
        # same situation as above, but a bench tool keeps the marker fresh: expected output
        age = [1.0]
        b = Bench()
        b.engine.commander = make_commander(b.clock, False)     # telemetry-only, as on the Cerbo
        b.engine.bench_age = lambda: age[0]
        b.sims[0].battery_v = 48.0
        b.sims[0].default_i_raw = 500
        b.run(2.0)
        s1 = b.service(A1)
        self.assertEqual(s1['/Huawei/BenchWindow'], 1)
        self.assertEqual(s1['/Alarms/UncommandedOutput'], 0)
        self.assertNotEqual(s1['/Huawei/State'], 'Faulted')
        self.assertTrue(s1['/Dc/0/Current'] > 1.0)
        self.assertTrue(b.engine.summary()['bench_open'])
        # the marker expires (bench tool killed): the check comes back
        age[0] = 90.0
        b.run(2.0)
        self.assertEqual(s1['/Huawei/BenchWindow'], 0)
        self.assertEqual(s1['/Huawei/State'], 'Faulted')
        self.assertEqual(s1['/Alarms/UncommandedOutput'], 2)

    def test_bench_window_is_ignored_in_control_mode(self):
        # the Bench fixture runs the CanCommander (flag forced); the marker must change nothing
        b = Bench()
        old = P.VERIFIED
        P.VERIFIED = True
        try:
            b.engine.commander = make_commander(b.clock, True)
            self.assertEqual(b.engine.commander.name, 'can')
            b.engine.bench_age = lambda: 1.0
            b.sims[0].battery_v = 48.0
            b.sims[0].default_i_raw = 500
            b.run(2.0)
        finally:
            P.VERIFIED = old
        s1 = b.service(A1)
        self.assertEqual(s1['/Huawei/BenchWindow'], 0)
        self.assertEqual(s1['/Huawei/State'], 'Faulted')
        self.assertFalse(b.engine.summary()['bench_open'])

    def test_address_swap_after_restart_is_followed(self):
        b = Bench()
        b.run(2.0)
        reg = b.engine.registry
        self.assertEqual(reg.by_addr, {1: A1, 2: B2})
        # both units renegotiate: B2 becomes 1, A1 becomes 2
        b.sims[0].set_addr(2)
        b.sims[1].set_addr(1)
        b.run(4.0)
        self.assertEqual(reg.by_addr, {1: B2, 2: A1})
        self.assertEqual(b.service(A1)['/Huawei/Address'], 2)
        self.assertEqual(b.service(B2)['/Huawei/Address'], 1)
        self.assertEqual(len(b.backend.services), 2)       # no duplicate services
        self.assertTrue(any('ADDR-TAKEN' in e[1] for e in reg.events))

    def test_bus_off_triggers_link_restart(self):
        calls = []

        class LM(object):
            def ensure_up(self):
                calls.append('restart')
                return {}
        b = Bench()
        b.engine.link_manager = LM()
        b.run(1.0)
        b.link.inject_error(0x40)
        b.pump()
        self.assertEqual(calls, ['restart'])
        self.assertEqual(b.engine.stats['bus_off'], 1)

    def test_single_unit_keeps_broadcasting_info_until_expected_count(self):
        b = Bench(sims=1)
        b.run(2.0)
        self.assertEqual(len(b.engine.registry.identified()), 1)
        n0 = len([f for f in b.tap_frames if f.can_id == P.request_id(P.CMD_INFO, 0)])
        b.run(31.0)
        n1 = len([f for f in b.tap_frames if f.can_id == P.request_id(P.CMD_INFO, 0)])
        self.assertTrue(n1 > n0)

    def test_frames_are_restamped_into_the_engine_clock(self):
        # the real link stamps with wall time (~1.79e9); freshness must use the engine clock
        b = Bench()
        b.run(2.0)
        m = b.engine.registry.modules[A1]
        from huawei.canbus import CanFrame
        f = CanFrame(P.unsolicited_id(1), bytes([0, 1, 0, 0, 0, 0, 0, 0]), ts=1792712567.3)
        b.engine.on_frame(f)
        self.assertEqual(m.last_unsol_t, b.clock())
        b.clock.advance(M.DISCONNECT_S + 1)
        self.assertEqual(m.evaluate(), M.DISCONNECTED)

    def test_stop_marks_services_offline(self):
        b = Bench()
        b.run(2.0)
        b.engine.stop()
        self.assertEqual(b.service(A1)['/Connected'], 0)
        self.assertEqual(sum(s.stats['sets'] for s in b.sims), 0)


class TestOperatorInputs(unittest.TestCase):
    def test_mode_and_ccl_validation(self):
        b = Bench()
        b.run(2.0)
        s1 = b.service(A1)
        self.assertFalse(s1.set_value_from_bus('/Mode', 2))
        self.assertTrue(s1.set_value_from_bus('/Mode', 1))
        self.assertTrue(b.engine.inputs[A1].mode_on)
        self.assertFalse(s1.set_value_from_bus('/Settings/ChargeCurrentLimit', 99.0))
        self.assertTrue(s1.set_value_from_bus('/Settings/ChargeCurrentLimit', 45.0))
        self.assertEqual(b.engine.inputs[A1].ccl, 45.0)
        self.assertFalse(s1.set_value_from_bus('/Serial', 'x'))     # read-only


if __name__ == '__main__':
    unittest.main()
