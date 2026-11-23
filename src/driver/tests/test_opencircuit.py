"""Logic of src/deploy/huawei-opencircuit-test.py (live test of an unloaded rectifier) on the PSU simulator:
guard, voltage steps with keep-alive, standby probe, the always-executed safe end, aborts. The CAN socket and the
Venus D-Bus parts run only on the Pi."""
import importlib.util
import os
import types
import unittest

import context  # noqa: F401
from huawei import protocol as P
from huawei.canbus import VirtualBus

from psu_sim import PsuSim

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(os.path.dirname(HERE)), 'deploy', 'huawei-opencircuit-test.py')
spec = importlib.util.spec_from_file_location('huawei_oc_test', SCRIPT)
OC = importlib.util.module_from_spec(spec)
spec.loader.exec_module(OC)


class Clock(object):
    def __init__(self):
        import time
        self.t0 = time.time()

    def __call__(self):
        import time
        return time.time()


class SimLink(object):
    """Our side of the bus: every frame we send is handed to the simulator, its replies queue up for recv()."""

    def __init__(self, sim_factory):
        self.bus = VirtualBus()
        self.ours = self.bus.attach('test')
        self.psu_link = self.bus.attach('psu')
        self.sim = sim_factory(self.psu_link)
        self.sent = []
        self.fail_after = None

    def send(self, frame):
        self.sent.append(frame)
        self.ours.send(frame)
        while True:
            f = self.psu_link.recv(0)
            if f is None:
                break
            self.sim.handle(f)

    def recv(self, timeout=0.0):
        return self.ours.recv(0)

    def close(self):
        pass


def make_args(**kw):
    a = dict(interface='gs_usb:1', addr=1, steps=(52.4, 54.0, 56.0, 58.0), hold=0.25, amps=1.0, keepalive=0.05,
             nominal=52.0, guard_v=10.0, max_current=0.5, max_temp=65.0, no_standby_probe=False, dry_run=False,
             log=os.devnull, guard_s=0.1, poll_wait=0.01, standby_hold=0.25)
    a.update(kw)
    return types.SimpleNamespace(**a)


class ColdStartSim(PsuSim):
    """A unit as found on the Pi on 2026-11-18: powered, nothing connected, output stage off, the terminals
    show a residual ~2.85 V. Per the project's probe notes the first non-zero on-line current limit switches the
    output stage on (status bit 27) and the open terminals then show the on-line voltage; a 0 A limit switches it
    off again."""
    RESIDUAL_V = 2.85

    def __init__(self, *a, **kw):
        PsuSim.__init__(self, *a, **kw)
        self.out_on = False

    def _handle_set(self, data):
        PsuSim._handle_set(self, data)
        d = bytes(data).ljust(8, b'\0')
        if P.SET_REG_BY_NUM.get(d[1]) == 'online_current':
            self.out_on = int.from_bytes(d[4:8], 'big') > 0

    def measure(self):
        if not self.out_on:
            return self.RESIDUAL_V, 0.0
        return round(self.setpoint_v(), 3), 0.0

    def _status_frames(self):
        out = PsuSim._status_frames(self)
        if self.out_on:
            cid, data = out[-1]
            bits = int.from_bytes(data[4:8], 'big') | (1 << 27)
            out[-1] = (cid, data[:4] + bits.to_bytes(4, 'big'))
        return out


def run_test(sim_kwargs=None, sim_cls=PsuSim, sim_attrs=None, **args):
    kw = dict(addr=1, serial_hex='0C0C0C0C0C03', nominal_a=52.0, voltage_in_0x85=False, standby=True,
              default_v=52.4, default_current_a=0.0)
    kw.update(sim_kwargs or {})
    holder = {}

    def factory(link):
        holder['sim'] = sim_cls(link, Clock(), **kw)
        for k, v in (sim_attrs or {}).items():
            setattr(holder['sim'], k, v)
        return holder['sim']
    ramp = OC.load_ramp()
    t = OC.Test(make_args(**args), ramp)
    t.link = SimLink(factory)
    t.lst = ramp.Listener(t.link)
    t.setup = lambda: None              # the real link/profile/driver checks are Pi-only
    rc = t.run()
    return t, holder['sim'], rc


class TestPureLogic(unittest.TestCase):
    def test_guard_refuses_a_connected_output_and_silence(self):
        self.assertIn('no status', OC.connection_guard(None, None))
        self.assertIn('connected', OC.connection_guard({'out_voltage_v': 51.9}, 1.0))
        self.assertIn('connected', OC.connection_guard({'out_voltage_v': 2.8, 'v85': 50.2}, 1.0))
        self.assertIsNone(OC.connection_guard({'out_voltage_v': 2.85, 'v85': 2.67}, 1.0))

    def test_only_volatile_registers_can_be_written(self):
        for name in ('online_voltage', 'online_current', 'standby'):
            OC.check_write_allowed(name)
        for name in ('default_voltage', 'default_current', 'ovp_voltage', 'fan_max', 'fan_duty'):
            with self.assertRaises(SystemExit):
                OC.check_write_allowed(name)

    def test_output_on_by_bit_27_or_voltage(self):
        self.assertTrue(OC.output_is_on({'registers': {0x83: 1 << 27}}))
        self.assertTrue(OC.output_is_on({'out_voltage_v': 57.9}))
        self.assertFalse(OC.output_is_on({'registers': {0x83: 1 << 9}, 'out_voltage_v': 2.85, 'v85': 2.67}))

    def test_output_off_needs_a_reading_and_uses_whichever_voltage_register_exists(self):
        self.assertTrue(OC.output_is_off({'out_voltage_v': 2.85}))
        self.assertTrue(OC.output_is_off({'out_voltage_v': 2.85, 'v85': 2.67}))
        self.assertFalse(OC.output_is_off({}))                                  # no reading is not 'off'
        self.assertFalse(OC.output_is_off(None))
        self.assertFalse(OC.output_is_off({'out_voltage_v': 2.85, 'v85': 55.0}))
        self.assertFalse(OC.output_is_off({'out_voltage_v': 12.0}))
        self.assertFalse(OC.output_is_off({'registers': {0x83: 1 << 27}, 'out_voltage_v': 2.85}))

    def test_abort_rules(self):
        ramp = OC.load_ramp()
        ok = {'out_voltage_v': 54.0, 'out_current_a': 0.0, 'pfc_temp_c': 30.0, 'out_temp_c': 30.0, 'in_temp_c': 28.0}
        self.assertIsNone(OC.unloaded_abort_reason(ramp, 54.0, 1.0, ok, 0.5))
        self.assertIn('unexpected current', OC.unloaded_abort_reason(ramp, 54.0, 1.0, dict(ok, out_current_a=0.8), 0.5))
        self.assertIn('above target', OC.unloaded_abort_reason(ramp, 54.0, 1.0, dict(ok, out_voltage_v=55.5), 0.5))
        self.assertIn('C above', OC.unloaded_abort_reason(ramp, 54.0, 1.0, dict(ok, pfc_temp_c=70.0), 0.5))
        self.assertIn('no status', OC.unloaded_abort_reason(ramp, 54.0, 1.0, ok, 9.0))


class TestSimulatedRuns(unittest.TestCase):
    def test_unit_that_switches_on_with_the_first_current_limit(self):
        """Cold unit: ~2.85 V residual, the first non-zero on-line current switches the output stage on."""
        t, sim, rc = run_test(sim_cls=ColdStartSim, sim_kwargs=dict(standby=False))
        self.assertEqual(rc, 0, t.aborted)
        self.assertTrue(t.end_acks_ok)
        self.assertIsNotNone(t.switched_on_at)
        self.assertAlmostEqual(t.max_v, 58.0, delta=0.1)
        self.assertEqual(sim.online_i_raw, 0)                       # ended at 0 A
        self.assertFalse(sim.out_on)                                # output off again
        self.assertEqual(t.final_snap.get('flags'), [])
        self.assertAlmostEqual(sim.online_v, 52.4, places=1)        # and 52.4 V
        names = [n for n, raw, err in sim.sets]
        self.assertTrue(set(names) <= {'online_voltage', 'online_current', 'standby'}, names)
        self.assertEqual(sim.stats['set_errors'], 0)
        self.assertEqual(names[:2], ['online_voltage', 'online_current'])      # voltage first, then current

    def test_standby_probe_runs_when_the_output_stays_off_and_standby_is_restored(self):
        t, sim, rc = run_test(sim_kwargs=dict(standby=True))
        names = [n for n, raw, err in sim.sets]
        self.assertIn('standby', names)
        self.assertEqual(t.initial_standby, True)
        self.assertIsNotNone(t.switched_on_at)                      # standby=0 switched the sim on
        self.assertTrue(sim.standby)                                # restored to the initial value (flag set)
        self.assertEqual(sim.online_i_raw, 0)
        self.assertTrue(t.end_acks_ok)
        self.assertEqual(sim.stats['set_errors'], 0)

    def test_no_ack_aborts_and_still_ends_at_zero(self):
        t, sim, rc = run_test(sim_cls=ColdStartSim, sim_kwargs=dict(standby=False), sim_attrs=dict(drop_acks=True))
        self.assertEqual(rc, 2)
        self.assertIn('ack', t.aborted)
        self.assertFalse(t.end_acks_ok)                             # the unit never acked, and we report that
        self.assertEqual(sim.online_i_raw, 0)                       # but the zero was still sent (sim applies it)
        self.assertFalse(sim.out_on)
        self.assertAlmostEqual(sim.online_v, 52.4, places=1)

    def test_connected_battery_is_refused_before_any_write(self):
        t, sim, rc = run_test(sim_kwargs=dict(standby=False, battery_v=51.0, default_v=51.0))
        self.assertEqual(sim.sets, [])                              # nothing written at all
        self.assertIn('GUARD', t.aborted)

    def test_dry_run_writes_nothing(self):
        t, sim, rc = run_test(sim_cls=ColdStartSim, sim_kwargs=dict(standby=False), dry_run=True)
        self.assertEqual(sim.sets, [])
        self.assertEqual(rc, 0)


if __name__ == '__main__':
    unittest.main()
