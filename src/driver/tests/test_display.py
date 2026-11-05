"""Display repeater on the virtual buses: the engine with two simulated rectifiers on the
module bus, two DisplayPorts on two separate board buses, a front-end simulator on each
(both boards expect address 1, unit 2 lives at module address 2). Test names are the ones
Display_Repeater_Task.md section 6 asks for."""
import json
import os
import tempfile
import unittest

import context
from huawei import protocol as P
from huawei import modules as M
from huawei.canbus import VirtualBus, CanFrame
from huawei.display import DisplayPort, DisplayRepeater
from huawei.main import parse_display_spec, load_config
from frontend_sim import FrontEndSim
from test_engine import Bench, A1, B2
from test_modules import status_burst, unsol, info_serial
from test_control_path import VerifiedCase


class DisplayBench(Bench):
    def __init__(self, ports=(('can3', A1, 1), ('can2', B2, 1)), board_kw=None, **kw):
        kw.setdefault('quiet_start_s', 0.0)
        Bench.__init__(self, **kw)
        self.boards = {}
        objs = {}
        for ifname, serial, baddr in ports:
            bus = VirtualBus()
            plink = bus.attach('port-' + ifname)
            blink = bus.attach('board-' + ifname)
            objs[ifname] = DisplayPort(plink, ifname, serial, baddr, clock=self.clock, module_link=self.link)
            self.boards[ifname] = FrontEndSim(blink, self.clock, addr=baddr, **(board_kw or {}))
        self.display = DisplayRepeater(objs, self.clock)
        self.engine.display = self.display

    def pump(self):
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
            for ifname, board in self.boards.items():
                f = board.link.recv()
                while f is not None:
                    board.handle(f)
                    moved = True
                    f = board.link.recv()
                if self.display.on_board_readable(ifname):
                    moved = True

    def run(self, seconds, step=0.2):
        n = int(round(seconds / step))
        for _ in range(n):
            self.clock.advance(step)
            for sim in self.sims:
                sim.tick()
            for board in self.boards.values():
                board.tick()
            self.pump()
            self.engine.tick()
            self.pump()


def driver_frames(bench):
    return [f for f in bench.tap_frames if P.unpack_id(f.can_id)['to_psu']]


class TestDisplay(unittest.TestCase):
    def test_display_forwards_only_psu_frames_of_its_unit_with_rewrite(self):
        b = DisplayBench()
        b.run(4.0)
        can3, can2 = b.display.ports['can3'], b.display.ports['can2']
        board1, board2 = b.boards['can3'], b.boards['can2']
        # board 2 sees module 2's bursts as address 1; module 1's bursts never reach it
        self.assertTrue(board2.stats['bursts_complete'] >= 3, board2.stats)
        self.assertEqual(board2.stats['foreign_addr'], 0)
        self.assertTrue(all(P.unpack_id(i)['addr'] in (0, 1) for i in board2.seen_ids))
        self.assertTrue(can2.stats['rewritten'] > 0)
        self.assertEqual(can3.stats['rewritten'], 0)              # unit 1 is address 1 already
        self.assertEqual(board1.stats['bursts_complete'], can3.stats['forwarded'] and board1.stats['bursts_complete'])
        self.assertTrue(board1.stats['bursts_complete'] >= 3)
        # the driver's own requests never reach a board; heartbeats do
        self.assertEqual(board1.stats['requests_seen'], 0)
        self.assertEqual(board2.stats['requests_seen'], 0)
        self.assertTrue(board1.stats['unsolicited'] > 0)
        # the boards' displayed values are the modules' values
        self.assertAlmostEqual(board1.displayed()['voltage_v'], 50.0, places=1)
        self.assertAlmostEqual(board2.displayed()['voltage_v'], 50.0, places=1)
        self.assertEqual(b.engine.summary()['display']['can2']['serial'], B2)

    def test_display_absorbs_board_writes_and_synthesizes_acks(self):
        b = DisplayBench(board_kw={'disguised_standby': True})
        b.run(5.0)
        for ifname, board in b.boards.items():
            port = b.display.ports[ifname]
            self.assertTrue(board.stats['writes'] >= 6 * 7, board.stats)
            self.assertEqual(board.stats['acks_ok'], board.stats['writes'])
            self.assertEqual(board.stats['acks_missing'], 0)
            self.assertEqual(board.stats['acks_error'], 0)
            self.assertEqual(port.stats['acks_synthesized'], board.stats['writes'])
            self.assertTrue(board.stats['cfg_frames'] >= 1)
            self.assertEqual(board.stats['cfg_acks'], 0)               # never acked
            self.assertEqual(port.stats['absorbed_cfg'], board.stats['cfg_frames'])
            self.assertAlmostEqual(port.wanted['default_voltage']['value_v'], 49.98, places=2)
            self.assertEqual(port.wanted['default_current']['raw'], 210)
            self.assertEqual(port.wanted['online_current']['raw'], 70)
            self.assertTrue(port.stats['absorbed_requests'] >= board.stats['requests'] + 1)   # + disguised standby
        # nothing the boards sent reached a module
        self.assertEqual(sum(s.stats['sets'] for s in b.sims), 0)
        self.assertEqual([f for f in driver_frames(b) if P.unpack_id(f.can_id)['cmd'] in (P.CMD_SET, P.CMD_CFG)], [])
        self.assertTrue(b.service(A1)['/Huawei/Display/Alive'] == 1)
        self.assertEqual(b.service(A1)['/Huawei/Display/Interface'], 'can3')
        self.assertTrue(b.service(A1)['/Huawei/Display/Forwarded'] > 0)
        self.assertTrue(b.service(A1)['/Huawei/Display/Absorbed'] > 0)

    def test_display_never_transmits_on_the_module_link(self):
        b = DisplayBench()
        with self.assertRaises(ValueError):
            DisplayPort(b.link, 'can3', A1, module_link=b.link)
        with self.assertRaises(ValueError):
            DisplayPort(VirtualBus().attach('x'), 'can0', A1, module_link=b.link)
        b.run(5.0)
        # every controller-side frame on the module bus came from the engine
        self.assertEqual(len(driver_frames(b)), b.engine.stats['tx'])
        board_ids = set()
        for board in b.boards.values():
            board_ids |= {P.request_id(P.CMD_SET, board.addr), P.request_id(P.CMD_CFG, 0)}
        self.assertEqual([f for f in b.tap_frames if f.can_id in board_ids], [])

    def test_display_never_forwards_the_drivers_acks(self):
        old = P.VERIFIED
        P.VERIFIED = True
        try:
            b = DisplayBench(control=True)              # CanCommander: explicit zeros are written and acked
            b.run(4.0)
            self.assertTrue(sum(s.stats['sets'] for s in b.sims) > 0)
            for board in b.boards.values():
                self.assertEqual(board.stats['acks_unexpected'], 0)
            self.assertTrue(b.display.ports['can3'].stats['acks_dropped'] > 0)
        finally:
            P.VERIFIED = old

    def test_display_board_sees_live_values(self):
        b = DisplayBench()
        b.run(3.0)
        board1 = b.boards['can3']
        self.assertAlmostEqual(board1.displayed()['voltage_v'], 50.0, places=1)
        b.sims[0].battery_v = 48.0                  # the module's terminals follow the battery at 0 A
        b.run(2.0)
        self.assertAlmostEqual(board1.displayed()['voltage_v'], 48.0, places=1)
        # R4875G5: the burst's 0x75 is 0; the board gets the driver's 0x85 reading instead
        b2 = DisplayBench(voltage_in_0x85=True, model='R4875G5')
        b2.run(3.0)
        self.assertAlmostEqual(b2.boards['can3'].displayed()['voltage_v'], 50.0, places=1)
        self.assertTrue(b2.display.ports['can3'].stats['voltage_substituted'] >= 2)
        self.assertTrue(b2.display.ports['can3'].stats['get85_consumed'] >= 2)
        self.assertEqual(b2.service(A1)['/Huawei/VoltageSource'], '0x85')

    def test_display_unidentified_unit_forwards_nothing(self):
        clock = context.FakeClock(100.0)
        reg = M.Registry(clock, scales={A1: 'ratio1024'})
        bus = VirtualBus()
        port = DisplayPort(bus.attach('port'), 'can3', A1, 1, clock=clock)
        board = bus.attach('board')
        rep = DisplayRepeater({'can3': port}, clock)

        def feed(frames):
            for f in frames:
                dec = P.decode(f)
                m = reg.feed(dec)
                rep.on_module_frame(f, dec, m)
        feed([unsol(1, 100.0)] + status_burst(1, 100.1))
        self.assertIsNone(board.recv())                 # address known, serial not: nothing
        self.assertEqual(port.stats['forwarded'], 0)
        feed([info_serial(1, 100.2, A1)])
        self.assertIsNotNone(board.recv())              # the info frame itself is relayed
        feed(status_burst(1, 100.3))
        n = 0
        while board.recv() is not None:
            n += 1
        self.assertEqual(n, len(status_burst(1, 0)))
        self.assertTrue(port.stats['forwarded'] > 1)

    def test_display_port_failure_is_isolated(self):
        calls = []

        class LM(object):
            def ensure_up(self):
                calls.append('restart')
                return {}

            def bounce(self):
                calls.append('bounce')
                return {}
        b = DisplayBench()
        can3 = b.display.ports['can3']
        can3.link_manager = LM()
        can3.link.fail_tx = True
        b.run(3.0)
        self.assertTrue(can3.stats['tx_fail'] > 0)
        self.assertNotIn('bounce', calls)                                   # not yet: 10 s of failures first
        b.run(12.0)
        self.assertEqual(calls.count('bounce'), 1)                           # bounced once, cooldown 30 s
        self.assertEqual(can3.stats['bounces'], 1)
        self.assertEqual(can3.stats['forwarded'], 0)
        self.assertTrue(b.display.ports['can2'].stats['forwarded'] > 0)      # the other board unaffected
        self.assertTrue(b.engine.registry.modules[A1].status_fresh())        # the engine unaffected
        self.assertEqual(b.service(A1)['/Huawei/State'], 'Inhibited')
        can3.link.fail_tx = False
        can3.link.inject_error(0x40)
        b.pump()
        self.assertEqual(can3.stats['bus_off'], 1)
        self.assertEqual(calls[-1], 'restart')                              # bus-off -> ensure_up (after the earlier bounce)
        self.assertEqual(b.engine.stats['bus_off'], 0)
        self.assertIsNone(b.engine.failed)

    def test_display_port_recovers_a_recreated_or_missing_interface(self):
        # 2026-11-02: the USB adapter re-enumerated while re-cabling; both board interfaces
        # came back as new netdevs and the driver's sockets were dead ("No such device")
        clock = context.FakeClock(100.0)
        buses = {'n': 0}
        ifindex = {}
        relinked = []
        ups = []

        class LM(object):
            def ensure_up(self):
                ups.append(clock())
                return {}
        current = {}

        def factory():
            buses['n'] += 1
            bus = VirtualBus()
            current['bus'] = bus
            return bus.attach('port%d' % buses['n'])

        def getter(name):
            if name not in ifindex:
                raise OSError('no such interface')
            return ifindex[name]
        # (a) missing at start: link None, nothing crashes, nothing forwarded
        port = DisplayPort(None, 'can2', B2, 1, clock=clock, link_manager=LM(), link_factory=factory,
                           ifindex_getter=getter, on_relink=relinked.append)
        rep = DisplayRepeater({'can2': port}, clock)
        reg = M.Registry(clock, scales={B2: 'ratio1024'})

        def feed(frames):
            for f in frames:
                dec = P.decode(f)
                rep.on_module_frame(f, dec, reg.feed(dec))
        feed([info_serial(2, 100.0, B2)] + status_burst(2, 100.1))
        self.assertEqual(port.stats['no_link'], 1 + len(status_burst(2, 0)))
        self.assertEqual(rep.on_board_readable('can2'), 0)
        for _ in range(3):
            clock.advance(2.0)
            rep.tick(clock())
        self.assertEqual(port.stats['relinks'], 0)
        self.assertEqual(ups, [])                                       # missing: ip not even tried
        # (b) the interface appears: raised, socket opened, watch re-armed
        ifindex['can2'] = 11
        clock.advance(5.0)
        rep.tick(clock())
        self.assertEqual(port.stats['relinks'], 1)
        self.assertEqual(len(ups), 1)
        self.assertEqual(relinked, [port])
        board = current['bus'].attach('board')
        feed([info_serial(2, clock(), B2)] + status_burst(2, clock()))   # unit re-identifies after the gap
        n = 0
        while board.recv() is not None:
            n += 1
        self.assertEqual(n, 1 + len(status_burst(2, 0)))
        self.assertTrue(port.summary()['link'])
        # (c) board alive: no checks at all
        board.send(CanFrame(P.request_id(P.CMD_DATA, 1), bytes(8), ext=True, ts=clock()))
        rep.on_board_readable('can2')
        self.assertTrue(port.alive())
        for _ in range(10):
            clock.advance(1.0)
            rep.tick(clock())
            board.send(CanFrame(P.request_id(P.CMD_DATA, 1), bytes(8), ext=True, ts=clock()))
            rep.on_board_readable('can2')
        self.assertEqual(len(ups), 1)
        # (d) interface re-created (new ifindex) while the board is silent: new socket within
        #     RECOVER_AFTER_S + RECOVER_INTERVAL_S; the old socket's failures are tolerated
        ifindex['can2'] = 12
        port.link.fail_tx = True
        old_link = port.link
        feed([info_serial(2, clock(), B2)] + status_burst(2, clock()))
        self.assertTrue(port.stats['tx_fail'] > 0)
        self.assertFalse(port.summary()['link'])
        for _ in range(3):
            clock.advance(4.0)
            rep.tick(clock())
        self.assertEqual(port.stats['relinks'], 2)
        self.assertIsNot(port.link, old_link)
        self.assertEqual(len(relinked), 2)
        board2 = current['bus'].attach('board2')
        feed([info_serial(2, clock(), B2)] + status_burst(2, clock()))
        self.assertIsNotNone(board2.recv())
        # (e) same ifindex, healthy socket, board merely off: ip checked, socket kept
        clock.advance(30.0)
        rep.tick(clock())
        self.assertEqual(port.stats['relinks'], 2)
        self.assertTrue(len(ups) >= 3)

    def test_display_config_parsing_and_cli(self):
        self.assertEqual(parse_display_spec('can3=708a85b00972'), ('can3', {'serial': '708A85B00972', 'board_addr': 1}))
        self.assertEqual(parse_display_spec('can2=0000000000B2:2'), ('can2', {'serial': '0000000000B2', 'board_addr': 2}))
        for bad in ('can3', 'can3=XYZ', 'can3=708A85B0097', 'can0=708A85B00972', 'can3=708A85B00972:0',
                    'can3=708A85B00972:x', '=708A85B00972'):
            with self.assertRaises(SystemExit):
                parse_display_spec(bad)
        path = os.path.join(tempfile.mkdtemp(), 'driver.json')
        with open(path, 'w') as fh:
            json.dump({'scales': {'708a85b00972': 'conservative'},
                       'display': {'can3': {'serial': '708A85B00972', 'board_addr': 1},
                                   'can2': {'serial': '0000000000B2'}}}, fh)
        cfg = load_config(path)
        self.assertEqual(cfg['scales'], {'708A85B00972': 'conservative'})
        self.assertEqual(cfg['display']['can3'], {'serial': '708A85B00972', 'board_addr': 1})
        self.assertEqual(cfg['display']['can2'], {'serial': '0000000000B2', 'board_addr': 1})
        cfg = load_config(path, ['can2=0000000000B2:2'])                 # CLI overrides per interface
        self.assertEqual(cfg['display']['can2']['board_addr'], 2)
        self.assertEqual(load_config(None)['display'], {})
        with open(path, 'w') as fh:
            json.dump({'display': {'can3': 'nonsense'}}, fh)
        with self.assertRaises(SystemExit):
            load_config(path)


if __name__ == '__main__':
    unittest.main()
