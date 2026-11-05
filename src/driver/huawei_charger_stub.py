#!/usr/bin/env python3
"""Stub Huawei charger services for Venus OS v2.92 (Cerbo GX).

Publishes one com.victronenergy.charger.huawei_<serial> service per stub
unit with the D-Bus contract from the project brief (section 9), fixed
telemetry (0.0 A, battery voltage copied from the system service) and no
CAN access at all. Purpose: verify on the live system that

  - systemcalc picks the services up and includes them in /Dc/Charger/Power,
  - the Cerbo GUI and VRM show them as chargers with the given names,
  - device instances are allocated and kept per serial via localsettings,
  - value changes on the /Link/* paths are logged (stock DVCC writes nothing;
    the patched DVCC writes them and a changing /Huawei/Heartbeat every 3 s,
    which is the liveness signal; CommandAgeSeconds is derived from it).

Run on the Cerbo:   python3 /data/huawei/src/driver/huawei_charger_stub.py [--available]
                    --available publishes /State = 252 (external control) so the
                    patched DVCC allocates current to the stub; without it the
                    stub reports /State = 0 and receives heartbeat writes of 0 A.
                    --dcsource additionally publishes a display-only
                    com.victronenergy.dcsource.huawei_<serial> service per unit
                    (monitor mode "AC charger") to test how VRM draws it. Not
                    used by DVCC, systemcalc or the kWh accounting on v2.92.
Stop:               Ctrl+C (services disappear, nothing is installed).
Supervised:         /data/huawei/service/run (daemontools, see src/deploy).
Leaves behind only the /Settings/Devices/huawei_<serial>/ClassAndVrmInstance
entries in localsettings, which are the real driver's instances anyway.
"""
import logging
import os
import signal
import sys
import time

sys.path.insert(1, '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python')

import dbus  # noqa: E402
from dbus.mainloop.glib import DBusGMainLoop  # noqa: E402
from gi.repository import GLib  # noqa: E402
from vedbus import VeDbusService, VeDbusItemImport  # noqa: E402
from settingsdevice import SettingsDevice  # noqa: E402

VERSION = '0.1-stub'
PRODUCT_ID = 0xB0FF          # placeholder; final ProductId policy is a design-freeze item
STUB_UNITS = [               # serial -> name; replace by the real serials after Phase B
    ('STUB000001', 'Huawei 1 (stub)'),
    ('STUB000002', 'Huawei 2 (stub)'),
]
DEFAULT_INSTANCE = 290       # first free charger instance; localsettings resolves collisions
DCSOURCE_INSTANCE = 300      # instance for the optional display-only dcsource services
MONITOR_MODE_AC_CHARGER = -2 # dcsource monitor modes (Venus dbus wiki): -1 Generic source, -2 AC charger,
                             # -3 DC/DC charger, -4 Water generator, -7 Shaft generator, -8 Wind charger
MONITOR_MODE_TEXT = {-1: 'Generic source', -2: 'AC charger', -3: 'DC/DC charger', -4: 'Water generator',
                     -7: 'Shaft generator', -8: 'Wind charger'}

log = logging.getLogger('huawei-stub')


class StubCharger:
    def __init__(self, bus, serial, name, system_voltage, available=False, dcsource=False):
        self.serial = serial
        self.name = name
        self.system_voltage = system_voltage
        self.writes = []
        self.last = {}
        self.window = 0
        self.beats = []
        self.dcsvc = None

        # Device instance via localsettings, keyed by serial (stable identity)
        key = 'huawei_%s' % serial
        self.settings = SettingsDevice(bus, {
            'instance': ['/Settings/Devices/%s/ClassAndVrmInstance' % key,
                         'charger:%d' % DEFAULT_INSTANCE, 0, 0],
            'customname': ['/Settings/Devices/%s/CustomName' % key, name, 0, 0],
            'ccl': ['/Settings/Devices/%s/ChargeCurrentLimit' % key, 50.0, 0.0, 60.0],
        }, self._setting_changed, timeout=30)
        instance = int(self.settings['instance'].split(':')[1])

        # dbus-python allows one exported root object per connection, so every
        # service gets its own private connection (same approach as Victron's
        # multi-device drivers). The shared bus is only used for localsettings.
        self.conn = dbus.SystemBus(private=True)
        self.svc = VeDbusService('com.victronenergy.charger.huawei_%s' % serial, self.conn)
        self.svc.add_mandatory_paths(
            processname=os.path.basename(__file__), processversion=VERSION,
            connection='CAN stub (no bus)', deviceinstance=instance,
            productid=PRODUCT_ID, productname='Huawei R4850G2 rectifier (stub)',
            firmwareversion='stub', hardwareversion='stub', connected=1)
        add = self.svc.add_path
        add('/Serial', serial)
        add('/CustomName', self.settings['customname'], writeable=True,
            onchangecallback=self._customname_changed)
        add('/NrOfOutputs', 1)
        add('/Dc/0/Voltage', None, gettextcallback=lambda p, v: '%.2f V' % v)
        add('/Dc/0/Current', 0.0, gettextcallback=lambda p, v: '%.1f A' % v)
        add('/Dc/0/Temperature', None)
        add('/State', 252 if available else 0)   # 0 = off (not available to DVCC), 252 = external control
        add('/ErrorCode', 0)
        add('/Mode', 4, writeable=True, onchangecallback=self._link_write)   # 1 = on, 4 = off
        add('/Settings/ChargeCurrentLimit', self.settings['ccl'], writeable=True,
            onchangecallback=self._ccl_write, gettextcallback=lambda p, v: '%.1f A' % v)
        add('/Link/ChargeCurrent', None, writeable=True, onchangecallback=self._link_write)
        add('/Link/ChargeVoltage', None, writeable=True, onchangecallback=self._link_write)
        add('/Link/NetworkMode', None, writeable=True, onchangecallback=self._link_write)
        add('/Huawei/Heartbeat', None, writeable=True, onchangecallback=self._heartbeat)
        add('/Huawei/CommandAgeSeconds', None)
        add('/Huawei/LimitingReason', 'stub: no hardware')
        log.info('%s: service up, instance %d, name %r', serial, instance, name)

        if dcsource:
            # Display-only DC source (what VRM draws as a DC-side block). Same
            # measurements as the charger service; its own instance and connection.
            dkey = 'huawei_%s_dcsource' % serial
            self.dcsettings = SettingsDevice(bus, {
                'instance': ['/Settings/Devices/%s/ClassAndVrmInstance' % dkey,
                             'dcsource:%d' % DCSOURCE_INSTANCE, 0, 0],
            }, self._setting_changed, timeout=30)
            dinstance = int(self.dcsettings['instance'].split(':')[1])
            self.dcconn = dbus.SystemBus(private=True)
            self.dcsvc = VeDbusService('com.victronenergy.dcsource.huawei_%s' % serial, self.dcconn)
            self.dcsvc.add_mandatory_paths(
                processname=os.path.basename(__file__), processversion=VERSION,
                connection='CAN stub (no bus)', deviceinstance=dinstance,
                productid=PRODUCT_ID, productname='Huawei R4850G2 (DC source view, stub)',
                firmwareversion='stub', hardwareversion='stub', connected=1)
            dadd = self.dcsvc.add_path
            dadd('/Serial', serial)
            dadd('/CustomName', name + ' DC')
            dadd('/Settings/MonitorMode', MONITOR_MODE_AC_CHARGER,
                 gettextcallback=lambda p, v: MONITOR_MODE_TEXT.get(v, str(v)))
            dadd('/Dc/0/Voltage', None, gettextcallback=lambda p, v: '%.2f V' % v)
            dadd('/Dc/0/Current', 0.0, gettextcallback=lambda p, v: '%.1f A' % v)
            dadd('/Dc/0/Power', 0.0, gettextcallback=lambda p, v: '%.0f W' % v)
            dadd('/Dc/0/Temperature', None)
            dadd('/History/EnergyOut', 0.0, gettextcallback=lambda p, v: '%.2f kWh' % v)
            dadd('/Alarms/LowVoltage', 0)
            dadd('/Alarms/HighVoltage', 0)
            log.info('%s: dcsource service up, instance %d (monitor mode %d)', serial, dinstance,
                     MONITOR_MODE_AC_CHARGER)

    # --- callbacks ------------------------------------------------------
    def _setting_changed(self, setting, old, new):
        log.info('%s: setting %s changed %r -> %r', self.serial, setting, old, new)

    def _customname_changed(self, path, value):
        self.settings['customname'] = value
        log.info('%s: CustomName -> %r', self.serial, value)
        return True

    def _ccl_write(self, path, value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return False
        if not 0.0 <= value <= 60.0:
            return False
        self.settings['ccl'] = value
        log.info('%s: ChargeCurrentLimit -> %.1f A', self.serial, value)
        return True

    def _heartbeat(self, path, value):
        # DVCC writes a changing counter every ADJUST tick while it is in
        # control; an unchanged SetValue never reaches a vedbus callback, so
        # this, not /Link/ChargeCurrent, is the liveness signal.
        self.beats.append(time.time())
        self.window += 1
        return True

    def _link_write(self, path, value):
        now = time.time()
        self.writes.append((now, path, value))
        self.window += 1
        if self.last.get(path) != value:
            log.info('%s: SetValue %s = %r (previous %r)', self.serial, path, value, self.last.get(path))
            self.last[path] = value
        return True

    # --- periodic -------------------------------------------------------
    def tick(self):
        v = self.system_voltage.get_value()
        self.svc['/Dc/0/Voltage'] = float(v) if v is not None else None
        self.svc['/Dc/0/Current'] = 0.0
        if self.dcsvc is not None:
            self.dcsvc['/Dc/0/Voltage'] = float(v) if v is not None else None
            self.dcsvc['/Dc/0/Current'] = 0.0
            self.dcsvc['/Dc/0/Power'] = 0.0
        if self.beats:
            self.svc['/Huawei/CommandAgeSeconds'] = round(time.time() - self.beats[-1], 1)
        return True

    def minute(self):
        age = round(time.time() - self.beats[-1], 1) if self.beats else None
        recent = self.beats[-21:]
        gaps = [b - a for a, b in zip(recent, recent[1:])]
        log.info('%s: %d SetValue in last 60 s, heartbeat %d total, last %s s ago, max gap %.1fs, ChargeCurrent=%r',
                 self.serial, self.window, len(self.beats), age, max(gaps) if gaps else 0.0,
                 self.last.get('/Link/ChargeCurrent'))
        self.window = 0
        return True


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    available = '--available' in sys.argv[1:]
    dcsource = '--dcsource' in sys.argv[1:]
    system_voltage = VeDbusItemImport(bus, 'com.victronenergy.system', '/Dc/Battery/Voltage')
    units = [StubCharger(bus, serial, name, system_voltage, available, dcsource) for serial, name in STUB_UNITS]
    for u in units:
        u.tick()
        GLib.timeout_add(1000, u.tick)
        GLib.timeout_add(60000, u.minute)

    loop = GLib.MainLoop()

    def stop(*_):
        log.info('stopping; %s', ', '.join('%s: %d writes' % (u.serial, len(u.writes)) for u in units))
        loop.quit()
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    log.info('stub %s running with %d charger services (State=%d); Ctrl+C to stop',
             VERSION, len(units), 252 if available else 0)
    loop.run()


if __name__ == '__main__':
    main()
