import os
import tempfile
import unittest

import context  # noqa: F401
from huawei import canbus as C


class TestFrame(unittest.TestCase):
    def test_pack_unpack_extended(self):
        f = C.CanFrame(0x108040FE, bytes(8), ext=True)
        g = C.CanFrame.unpack(f.pack())
        self.assertEqual(f, g)
        self.assertTrue(g.ext)

    def test_pack_unpack_standard_and_short(self):
        f = C.CanFrame(0x18F, b'\x01\x02', ext=False)
        g = C.CanFrame.unpack(f.pack())
        self.assertEqual(g.data, b'\x01\x02')
        self.assertFalse(g.ext)

    def test_error_frame(self):
        buf = C.CanFrame(0x40, bytes(8), ext=True, err=True).pack()
        g = C.CanFrame.unpack(buf)
        self.assertTrue(g.err)
        self.assertEqual(g.can_id, 0x40)
        self.assertEqual(g.id_str, 'ERR')


class TestVirtualBus(unittest.TestCase):
    def test_delivery_and_echo(self):
        bus = C.VirtualBus()
        a = bus.attach('a', echo=True)
        b = bus.attach('b')
        a.send(C.CanFrame(0x108040FE, bytes(8)))
        got = b.recv()
        self.assertEqual(got.can_id, 0x108040FE)
        self.assertFalse(got.echo)
        e = a.recv()
        self.assertTrue(e.echo)
        self.assertIsNone(b.recv())

    def test_tx_failure_and_error_injection(self):
        bus = C.VirtualBus()
        a = bus.attach('a')
        a.fail_tx = True
        with self.assertRaises(C.CanError):
            a.send(C.CanFrame(0x108040FE, bytes(8)))
        a.inject_error(0x40)
        f = a.recv()
        self.assertTrue(f.err)
        self.assertEqual(a.stats['err'], 1)


class TestLinkManager(unittest.TestCase):
    def runner_factory(self, up=True, state='ERROR-ACTIVE', bitrate=125000, fail=None):
        calls = []

        def run(argv):
            calls.append(argv)
            if fail and argv[:4] == fail:
                return 1, 'RTNETLINK answers: Operation not permitted'
            if argv[:3] == ['ip', '-details', 'link']:
                flags = '<NOARP,UP,LOWER_UP,ECHO>' if up else '<NOARP,ECHO>'
                return 0, ('3: can0: %s mtu 16 qdisc pfifo_fast state %s\n    link/can\n    can state %s '
                           'restart-ms 100\n    bitrate %d sample-point 0.875' % (flags, 'UP' if up else 'DOWN', state, bitrate))
            return 0, ''
        return run, calls

    def test_refuses_when_profile_not_disabled(self):
        run, calls = self.runner_factory()
        lm = C.LinkManager('can0', profile_getter=lambda: 1, runner=run)
        self.assertFalse(lm.profile_ok())
        with self.assertRaises(C.CanError):
            lm.ensure_up()
        self.assertEqual(calls, [])

    def test_bounce_takes_the_link_down_first(self):
        run, calls = self.runner_factory()
        lm = C.LinkManager('can2', profile_getter=lambda: 0, runner=run)
        lm.bounce()
        self.assertEqual(calls[0], ['ip', 'link', 'set', 'can2', 'down'])

    def test_status_parsing(self):
        run, _ = self.runner_factory(up=True, state='BUS-OFF', bitrate=250000)
        lm = C.LinkManager('can0', profile_getter=lambda: 0, runner=run)
        st = lm.status()
        self.assertTrue(st['up'])
        self.assertEqual(st['state'], 'BUS-OFF')
        self.assertEqual(st['bitrate'], 250000)

    def test_ensure_up_configures_when_down(self):
        run, calls = self.runner_factory(up=False, state='STOPPED')
        lm = C.LinkManager('can0', profile_getter=lambda: 0, runner=run)
        # the fake always reports down, so ensure_up ends with an error after configuring
        with self.assertRaises(C.CanError):
            lm.ensure_up()
        argvs = [' '.join(c) for c in calls]
        self.assertIn('ip link set can0 down', argvs)
        self.assertIn('ip link set can0 type can bitrate 125000 restart-ms 100 listen-only off', argvs)
        self.assertIn('ip link set can0 up', argvs)

    def test_stale_listen_only_flag_is_detected_and_cleared(self):
        """2026-11-19: a passive capture left `listen-only on` on the interface; the link was up at
        125 kbit/s so ensure_up() returned without touching it, the controller never ACKed and the
        unit repeated its frame 780 times a second."""
        calls = []

        def run(argv):
            calls.append(' '.join(argv))
            if argv[:3] == ['ip', '-details', 'link']:
                listen = '<LISTEN-ONLY> ' if not any('listen-only off' in c for c in calls) else ''
                return 0, ('6: can1: <NOARP,UP,LOWER_UP,ECHO> mtu 16 state UP\n    link/can\n    can %sstate '
                           'ERROR-ACTIVE restart-ms 100\n    bitrate 125000 sample-point 0.875' % listen)
            return 0, ''
        lm = C.LinkManager('can1', profile_getter=lambda: 0, runner=run)
        self.assertTrue(lm.status()['listen_only'])
        lm.ensure_up()
        self.assertIn('ip link set can1 type can bitrate 125000 restart-ms 100 listen-only off', calls)
        self.assertFalse(lm.status()['listen_only'])

    def test_ensure_up_noop_when_already_correct(self):
        run, calls = self.runner_factory(up=True, state='ERROR-ACTIVE', bitrate=125000)
        lm = C.LinkManager('can0', profile_getter=lambda: 0, runner=run)
        st = lm.ensure_up()
        self.assertEqual(st['bitrate'], 125000)
        self.assertEqual(len(calls), 1)      # only the status query

    def test_ip_failure_reported(self):
        run, _ = self.runner_factory(up=False, fail=['ip', 'link', 'set', 'can0'])
        lm = C.LinkManager('can0', profile_getter=lambda: '0', runner=run)
        with self.assertRaises(C.CanError) as cm:
            lm.ensure_up()
        self.assertIn('not permitted', str(cm.exception))


class TestInterfaceIdentity(unittest.TestCase):
    LINKS = {
        '/sys/class/net/can0/device': '../../../1c2bc00.can',
        '/sys/class/net/can0/device/driver': '../../../../bus/platform/drivers/sun4i_can',
        '/sys/class/net/can2/device': '../../../../../../../../bus/usb/devices/1-1.1:1.0',
        '/sys/class/net/can2/device/driver': '../../../../../../../../bus/usb/drivers/gs_usb',
    }

    def readlink(self, path):
        try:
            return self.LINKS[path.replace('\\', '/')]
        except KeyError:
            raise OSError(2, 'no such link', path)

    def test_builtin_and_usb_ports_are_told_apart(self):
        self.assertEqual(C.iface_device('can0', self.readlink), C.VECAN_DEVICE)
        self.assertEqual(C.iface_driver('can0', self.readlink), 'sun4i_can')
        self.assertEqual(C.iface_device('can2', self.readlink), '1-1.1:1.0')
        self.assertEqual(C.iface_driver('can2', self.readlink), C.USB_ADAPTER_DRIVER)

    def test_missing_interface_or_slcan_port_is_none(self):
        self.assertIsNone(C.iface_device('can1', self.readlink))      # slcan: no device link
        self.assertIsNone(C.iface_driver('can1', self.readlink))
        self.assertIsNone(C.iface_device('can9', self.readlink))


class TestCaptures(unittest.TestCase):
    def test_candump_roundtrip(self):
        path = os.path.join(tempfile.mkdtemp(), 'cap.log')
        w = C.CandumpWriter(path, 'can0')
        frames = [C.CanFrame(0x108040FE, bytes(8), ts=1697000000.123456),
                  C.CanFrame(0x1081407F, bytes([1, 0x75, 0, 0, 0, 0, 0xC8, 0]), ts=1697000000.2),
                  C.CanFrame(0x18F, b'\x01', ext=False, ts=1697000000.3),
                  C.CanFrame(0x40, bytes(8), ext=True, err=True, ts=1697000000.4)]
        for f in frames:
            w.write(f, 'rx')
        w.close()
        with open(path) as fh:
            first = fh.readline().strip()
        self.assertTrue(first.startswith('(1697000000.123456) can0 108040FE#0000000000000000'))
        back = list(C.read_candump(path))
        self.assertEqual(back, frames)
        self.assertTrue(back[3].err)
        # writer with its own clock stamps at write time
        path2 = os.path.join(tempfile.mkdtemp(), 'cap2.log')
        w2 = C.CandumpWriter(path2, 'can0', clock=lambda: 42.5)
        w2.write(frames[0]); w2.close()
        self.assertEqual(list(C.read_candump(path2))[0].ts, 42.5)

    def test_jsonl_reader_tolerant(self):
        path = os.path.join(tempfile.mkdtemp(), 'cap.jsonl')
        with open(path, 'w') as fh:
            fh.write('{"kind": "note", "text": "session start"}\n')
            fh.write('{"ts": 1.5, "can_id": 276840702, "data": "0000000000000000", "ext": true}\n')
            fh.write('{"t": 1.6, "id": "0x1081407F", "data_hex": "01 75 00 00 00 00 C8 00"}\n')
            fh.write('not json\n')
            fh.write('{"ts": 1.7, "can_id": 399, "data": [1, 2], "ext": false}\n')
        back = list(C.read_jsonl(path))
        self.assertEqual([f.can_id for f in back], [0x108040FE, 0x1081407F, 0x18F])
        self.assertEqual(back[1].data[1], 0x75)
        self.assertFalse(back[2].ext)


if __name__ == '__main__':
    unittest.main()


class TestResolveInterface(unittest.TestCase):
    """`gs_usb:<dev_id>` picks a CAN interface by driver and channel index, not by the
    can0/can1 name (Raspberry Pi stand-in, 2026-11-19: Pylontech on channel 0, Huawei on 1)."""

    def make(self, ifaces):
        """ifaces: {name: (driver, dev_id)} -> kwargs for resolve_interface"""
        links, ids = {}, {}
        for name, (drv, dev_id) in ifaces.items():
            links['/sys/class/net/%s/device/driver' % name] = '../../../../bus/usb/drivers/%s' % drv
            ids['/sys/class/net/%s/dev_id' % name] = '0x%x\n' % dev_id

        def readlink(path):
            try:
                return links[path.replace('\\', '/')]
            except KeyError:
                raise OSError(2, 'no such link', path)

        def read(path):
            try:
                return ids[path.replace('\\', '/')]
            except KeyError:
                raise OSError(2, 'no such file', path)
        return dict(root='/sys/class/net', listdir=lambda root: sorted(ifaces) + ['lo', 'eth0'],
                    readlink=readlink, read=read)

    def test_plain_name_is_passed_through(self):
        self.assertEqual(C.resolve_interface('can0', **self.make({})), 'can0')

    def test_channel_two_of_gs_usb(self):
        kw = self.make({'can0': ('gs_usb', 0), 'can1': ('gs_usb', 1)})
        self.assertEqual(C.resolve_interface('gs_usb:1', **kw), 'can1')
        self.assertEqual(C.resolve_interface('gs_usb:0', **kw), 'can0')

    def test_follows_the_channel_when_the_names_swap(self):
        kw = self.make({'can0': ('gs_usb', 1), 'can1': ('gs_usb', 0)})
        self.assertEqual(C.resolve_interface('gs_usb:1', **kw), 'can0')

    def test_builtin_port_is_never_a_gs_usb_channel(self):
        kw = self.make({'can0': ('sun4i_can', 1), 'can1': ('gs_usb', 0)})
        with self.assertRaises(C.CanError):
            C.resolve_interface('gs_usb:1', **kw)

    def test_no_match_and_ambiguous_are_errors(self):
        with self.assertRaises(C.CanError) as cm:
            C.resolve_interface('gs_usb:1', **self.make({'can0': ('gs_usb', 0)}))
        self.assertIn('no CAN interface', str(cm.exception))
        with self.assertRaises(C.CanError) as cm:
            C.resolve_interface('gs_usb:1', **self.make({'can0': ('gs_usb', 1), 'can2': ('gs_usb', 1)}))
        self.assertIn('ambiguous', str(cm.exception))

    def test_bad_spec(self):
        with self.assertRaises(C.CanError):
            C.resolve_interface('gs_usb:x', **self.make({}))

    def test_dev_id_reads_hex_and_decimal(self):
        kw = self.make({'can0': ('gs_usb', 1)})
        self.assertEqual(C.iface_dev_id('can0', kw['read'], '/sys/class/net'), 1)
        self.assertIsNone(C.iface_dev_id('can9', kw['read'], '/sys/class/net'))
