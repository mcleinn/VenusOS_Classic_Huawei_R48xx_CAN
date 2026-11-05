"""D-Bus side of the Huawei driver: one com.victronenergy.charger.huawei_<serial> service
per identified unit, plus an optional display-only com.victronenergy.dcsource mirror
(what VRM draws, Phase A report section 8f).

Grown out of huawei_charger_stub.py, which verified on the live Cerbo: one private bus
connection per service, instance allocation per serial through localsettings, the
writable /Mode switch, the /Link/* paths the patched DVCC writes, and /Huawei/Heartbeat
as the liveness counter.

The real D-Bus objects are created by a backend (`VenusBackend` on the Cerbo); tests use
`FakeBackend` with the same three calls, so everything in `ChargerService` runs on Windows.

/State mapping (what the patched DVCC and the overview tile read): 252 (external control)
while the unit is READY (commanded output) or INHIBITED only for lack of a DVCC budget
(modules.Module.able, set by the governor); 0 in every other state. The patched DVCC hands
a budget only to chargers with /State != 0, so a unit must advertise itself before it has a
budget, otherwise neither side ever moves (deadlock seen 2026-11-04 on the first run through
the driver). Not identified, no AC (STANDBY), faulted, degraded, operator off, excluded, not
commissioned or DVCC heartbeat lost all keep /State at 0: DVCC allocates nothing and the
tile stays hidden.
"""
import logging
import os

from . import modules as M
from . import VERSION

log = logging.getLogger('huawei.dbus')

PRODUCT_ID = 0xB0FF              # placeholder, design-freeze item
DEFAULT_INSTANCE = 290
DCSOURCE_INSTANCE = 300
MONITOR_MODE_AC_CHARGER = -2
MONITOR_MODE_TEXT = {-1: 'Generic source', -2: 'AC charger', -3: 'DC/DC charger', -4: 'Water generator',
                     -7: 'Shaft generator', -8: 'Wind charger'}
CCL_MAX_A = 60.0
HIGH_TEMP_C = 75.0
STATE_TEXT = {M.DISCOVERING: 'Discovering', M.READY: 'Ready', M.STANDBY: 'Standby',
              M.INHIBITED: 'Inhibited', M.DEGRADED: 'Degraded', M.FAULTED: 'Faulted',
              M.DISCONNECTED: 'Disconnected'}
STATE_EXTERNAL_CONTROL = 252
ALARM_OK, ALARM_WARNING, ALARM_ALARM = 0, 1, 2


# ------------------------------------------------------------------ backends
def _amps_text(path, value):
    try:
        return '%.1f A' % float(value)
    except (TypeError, ValueError):
        return str(value)


class VenusBackend(object):
    """Real objects on the Cerbo (velib_python)."""

    def __init__(self, bus):
        import dbus
        from vedbus import VeDbusService
        from settingsdevice import SettingsDevice
        self._dbus = dbus
        self._VeDbusService = VeDbusService
        self._SettingsDevice = SettingsDevice
        self.bus = bus

    def make_service(self, name):
        conn = self._dbus.SystemBus(private=True)
        return self._VeDbusService(name, conn)

    def make_settings(self, supported, callback):
        return self._SettingsDevice(self.bus, supported, callback, timeout=30)


class FakeService(object):
    """Minimal stand-in for VeDbusService: a dict of paths with callbacks."""

    def __init__(self, name):
        self.name = name
        self.values = {}
        self.writeable = {}
        self.callbacks = {}
        self.text = {}

    def add_mandatory_paths(self, processname, processversion, connection, deviceinstance,
                            productid, productname, firmwareversion, hardwareversion, connected):
        self.values.update({'/Mgmt/ProcessName': processname, '/Mgmt/ProcessVersion': processversion,
                            '/Mgmt/Connection': connection, '/DeviceInstance': deviceinstance,
                            '/ProductId': productid, '/ProductName': productname,
                            '/FirmwareVersion': firmwareversion, '/HardwareVersion': hardwareversion,
                            '/Connected': connected})

    def add_path(self, path, value, writeable=False, onchangecallback=None, gettextcallback=None):
        self.values[path] = value
        self.writeable[path] = writeable
        if onchangecallback:
            self.callbacks[path] = onchangecallback
        if gettextcallback:
            self.text[path] = gettextcallback

    def __getitem__(self, path):
        return self.values[path]

    def __setitem__(self, path, value):
        self.values[path] = value

    def set_value_from_bus(self, path, value):
        """What a SetValue from another process does: callback decides, then store."""
        if not self.writeable.get(path):
            return False
        cb = self.callbacks.get(path)
        if cb is not None and not cb(path, value):
            return False
        self.values[path] = value
        return True


class FakeSettings(dict):
    def __init__(self, supported, callback):
        dict.__init__(self)
        self.supported = supported
        for k, (path, default, lo, hi) in supported.items():
            self[k] = default


class FakeBackend(object):
    def __init__(self):
        self.services = {}

    def make_service(self, name):
        s = FakeService(name)
        self.services[name] = s
        return s

    def make_settings(self, supported, callback):
        return FakeSettings(supported, callback)


def _product_name(module):
    return 'Huawei %s rectifier' % (module.model or 'R48xx')


# ------------------------------------------------------------ charger service
class ChargerService(object):
    def __init__(self, module, inputs, watchdog, backend, custom_name=None, dcsource=False,
                 process_name='huawei-charger', connection='VE.Can socket can0', display_port=None):
        self.module = module
        self.inputs = inputs
        self.watchdog = watchdog
        self.backend = backend
        self.display_port = display_port
        serial = module.serial
        key = 'huawei_%s' % serial
        self.settings = backend.make_settings({
            'instance': ['/Settings/Devices/%s/ClassAndVrmInstance' % key, 'charger:%d' % DEFAULT_INSTANCE, 0, 0],
            'customname': ['/Settings/Devices/%s/CustomName' % key, custom_name or 'Huawei %s' % serial[-4:], 0, 0],
            'ccl': ['/Settings/Devices/%s/ChargeCurrentLimit' % key, 0.0, 0.0, CCL_MAX_A],
            'mode': ['/Settings/Devices/%s/Mode' % key, 4, 1, 4],           # operator switch, persisted (2026-11-04)
        }, self._setting_changed)
        instance = int(str(self.settings['instance']).split(':')[1])
        inputs.ccl = float(self.settings['ccl'])
        # /Mode used to reset to off at every driver restart or Cerbo reboot, which would leave a
        # production charger silent until someone flipped it; it is a persisted setting now.
        try:
            mode = int(self.settings['mode'])
        except (TypeError, ValueError):
            mode = 4
        if mode not in (1, 4):
            mode = 4
        inputs.mode_on = (mode == 1)

        self.svc = backend.make_service('com.victronenergy.charger.huawei_%s' % serial)
        fw = 'dcdc %s pfc %s' % (module.versions.get('dcdc_sw'), module.versions.get('pfc_sw')) if module.versions else 'unknown'
        self.svc.add_mandatory_paths(
            processname=process_name, processversion=VERSION, connection=connection,
            deviceinstance=instance, productid=PRODUCT_ID, productname=_product_name(module),
            firmwareversion=fw, hardwareversion=str(module.versions.get('hw', 'unknown')), connected=1)
        add = self.svc.add_path
        add('/Serial', serial)
        add('/CustomName', self.settings['customname'], writeable=True, onchangecallback=self._customname_changed)
        add('/NrOfOutputs', 1)
        add('/Dc/0/Voltage', None, gettextcallback=lambda p, v: '%.2f V' % v)
        add('/Dc/0/Current', None, gettextcallback=lambda p, v: '%.1f A' % v)
        add('/Dc/0/Power', None, gettextcallback=lambda p, v: '%.0f W' % v)
        add('/Dc/0/Temperature', None, gettextcallback=lambda p, v: '%.0f C' % v)
        add('/Ac/In/L1/V', None, gettextcallback=lambda p, v: '%.0f V' % v)
        add('/Ac/In/L1/I', None, gettextcallback=lambda p, v: '%.1f A' % v)
        add('/Ac/In/L1/P', None, gettextcallback=lambda p, v: '%.0f W' % v)
        add('/Ac/In/L1/F', None, gettextcallback=lambda p, v: '%.1f Hz' % v)
        add('/State', 0)
        add('/ErrorCode', 0)
        add('/Mode', mode, writeable=True, onchangecallback=self._mode_write)    # 1 on, 4 off; persisted
        add('/Settings/ChargeCurrentLimit', inputs.ccl, writeable=True,
            onchangecallback=self._ccl_write, gettextcallback=_amps_text)
        add('/Link/ChargeCurrent', None, writeable=True, onchangecallback=self._link_current)
        add('/Link/ChargeVoltage', None, writeable=True, onchangecallback=self._link_voltage)
        add('/Link/NetworkMode', None, writeable=True, onchangecallback=self._link_networkmode)
        add('/Huawei/Heartbeat', None, writeable=True, onchangecallback=self._heartbeat)
        add('/Huawei/CommandAgeSeconds', None)
        add('/Huawei/State', STATE_TEXT[module.state])
        add('/Huawei/LimitingReason', module.inhibit_reason)
        add('/Huawei/Address', module.addr)
        add('/Huawei/NominalCurrent', module.nominal_a)
        add('/Huawei/CurrentScale', module.scale or 'unbound')
        add('/Huawei/RequestedCurrent', None)
        add('/Huawei/SentCurrent', None)
        add('/Huawei/MeasuredLimit', None)
        add('/Huawei/AckOk', None)
        add('/Huawei/OperatingHours', None)
        add('/Huawei/Model', module.model)
        add('/Huawei/VoltageSource', None)
        add('/Huawei/LimitRatio', None)
        add('/Huawei/DefaultLimitRatio', None)
        add('/Huawei/DefaultLimitOk', None)
        add('/Huawei/AckTimeouts', 0)
        add('/Huawei/Fault', None)
        add('/Huawei/OutputOn', None)
        add('/Huawei/BenchWindow', 0)
        add('/Huawei/Display/Interface', display_port.ifname if display_port else None)
        add('/Huawei/Display/Alive', None)
        add('/Huawei/Display/Forwarded', None)
        add('/Huawei/Display/Absorbed', None)
        add('/Alarms/CommunicationLost', ALARM_OK)
        add('/Alarms/HighTemperature', ALARM_OK)
        add('/Alarms/UncommandedOutput', ALARM_OK)
        add('/Alarms/SetpointError', ALARM_OK)
        add('/Alarms/LowVoltage', ALARM_OK)
        add('/Alarms/HighVoltage', ALARM_OK)
        self.dcsvc = None
        if dcsource:
            self._make_dcsource(serial)
        log.info('%s: charger service up, instance %d', serial, instance)

    def _make_dcsource(self, serial):
        dkey = 'huawei_%s_dcsource' % serial
        self.dcsettings = self.backend.make_settings({
            'instance': ['/Settings/Devices/%s/ClassAndVrmInstance' % dkey, 'dcsource:%d' % DCSOURCE_INSTANCE, 0, 0],
        }, self._setting_changed)
        dinstance = int(str(self.dcsettings['instance']).split(':')[1])
        self.dcsvc = self.backend.make_service('com.victronenergy.dcsource.huawei_%s' % serial)
        self.dcsvc.add_mandatory_paths(
            processname='huawei-charger', processversion=VERSION, connection='VE.Can socket can0',
            deviceinstance=dinstance, productid=PRODUCT_ID, productname=_product_name(self.module) + ' (DC source view)',
            firmwareversion='-', hardwareversion='-', connected=1)
        dadd = self.dcsvc.add_path
        dadd('/Serial', serial)
        dadd('/CustomName', self.settings['customname'] + ' DC')
        dadd('/Settings/MonitorMode', MONITOR_MODE_AC_CHARGER,
             gettextcallback=lambda p, v: MONITOR_MODE_TEXT.get(v, str(v)))
        dadd('/Dc/0/Voltage', None, gettextcallback=lambda p, v: '%.2f V' % v)
        dadd('/Dc/0/Current', None, gettextcallback=lambda p, v: '%.1f A' % v)
        dadd('/Dc/0/Power', None, gettextcallback=lambda p, v: '%.0f W' % v)
        dadd('/Dc/0/Temperature', None)
        dadd('/Alarms/LowVoltage', ALARM_OK)
        dadd('/Alarms/HighVoltage', ALARM_OK)

    # ------------------------------------------------------------ callbacks
    def _setting_changed(self, setting, old, new):
        log.info('%s: setting %s %r -> %r', self.module.serial, setting, old, new)

    def _customname_changed(self, path, value):
        self.settings['customname'] = value
        if self.dcsvc is not None:
            self.dcsvc['/CustomName'] = '%s DC' % value
        return True

    def _mode_write(self, path, value):
        try:
            value = int(value)              # the Venus dbus tool sends a bare 1 as the string '1'
        except (TypeError, ValueError):
            return False
        if value not in (1, 4):
            return False
        self.inputs.mode_on = (value == 1)
        self.settings['mode'] = value
        log.info('%s: operator /Mode -> %s (persisted)', self.module.serial, 'on' if value == 1 else 'off')
        return True

    def _ccl_write(self, path, value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return False
        if not 0.0 <= value <= CCL_MAX_A:
            return False
        self.settings['ccl'] = value
        self.inputs.ccl = value
        return True

    def _link_current(self, path, value):
        try:
            self.inputs.link_current = None if value is None else float(value)
        except (TypeError, ValueError):
            return False
        self.inputs.link_current_t = self.module.clock()
        return True

    def _link_voltage(self, path, value):
        try:
            self.inputs.link_voltage = None if value is None else float(value)
        except (TypeError, ValueError):
            return False
        return True

    def _link_networkmode(self, path, value):
        self.inputs.network_mode = value
        return True

    def _heartbeat(self, path, value):
        self.watchdog.feed_heartbeat(value)
        return True

    # --------------------------------------------------------------- update
    def update(self, now):
        m = self.module
        s = self.svc
        st = m.status if m.status_fresh(now) else {}
        v = m.measured_voltage(now)
        i = st.get('out_current_a')
        s['/Dc/0/Voltage'] = v
        s['/Dc/0/Current'] = i
        s['/Dc/0/Power'] = round(v * i, 1) if v is not None and i is not None else None
        s['/Dc/0/Temperature'] = st.get('out_temp_c')
        s['/Ac/In/L1/V'] = st.get('ac_in_voltage_v')
        s['/Ac/In/L1/I'] = st.get('ac_in_current_a')
        s['/Ac/In/L1/P'] = st.get('ac_in_power_w')
        s['/Ac/In/L1/F'] = st.get('ac_in_freq_hz')
        s['/Huawei/OperatingHours'] = st.get('operating_hours')
        s['/Huawei/VoltageSource'] = m.voltage_source(now)
        s['/Huawei/LimitRatio'] = m.limit_ratio if st else None
        s['/Huawei/DefaultLimitRatio'] = m.default_ratio
        s['/Huawei/DefaultLimitOk'] = None if m.default_ok is None else int(m.default_ok)
        s['/Huawei/AckTimeouts'] = m.counters['ack_timeout']
        s['/Huawei/Fault'] = m.fault
        oo = m.output_on()
        s['/Huawei/OutputOn'] = None if oo is None else int(oo)
        s['/Huawei/BenchWindow'] = int(m.external_master)
        if self.display_port is not None:
            p = self.display_port
            s['/Huawei/Display/Alive'] = int(p.alive(now))
            s['/Huawei/Display/Forwarded'] = p.stats['forwarded']
            s['/Huawei/Display/Absorbed'] = p.stats['absorbed_writes'] + p.stats['absorbed_requests'] + \
                p.stats['absorbed_cfg'] + p.stats['absorbed_other']
        if m.model and s['/Huawei/Model'] != m.model:
            s['/Huawei/Model'] = m.model
            s['/ProductName'] = _product_name(m)
            if self.dcsvc is not None:
                self.dcsvc['/ProductName'] = _product_name(m) + ' (DC source view)'
        s['/Connected'] = 1 if m.alive(now) else 0
        s['/State'] = STATE_EXTERNAL_CONTROL if (m.state == M.READY or (m.state == M.INHIBITED and m.able)) else 0
        # /ErrorCode stays 0: Venus renders the VE.Direct charger codes (1 = "battery temperature too
        # high", seen on the Cerbo 2026-11-03 for a driver fault); the fault is in /Huawei/State,
        # /Huawei/Fault and the /Alarms/* paths.
        s['/ErrorCode'] = 0
        s['/Huawei/State'] = STATE_TEXT.get(m.state, m.state)
        s['/Huawei/LimitingReason'] = m.inhibit_reason
        s['/Huawei/Address'] = m.addr
        s['/Huawei/NominalCurrent'] = m.nominal_a
        s['/Huawei/CurrentScale'] = m.scale or 'unbound'
        s['/Huawei/RequestedCurrent'] = m.current.requested
        s['/Huawei/SentCurrent'] = m.current.sent_value
        s['/Huawei/MeasuredLimit'] = m.current.measured
        s['/Huawei/AckOk'] = None if m.current.ack_ok is None else int(m.current.ack_ok)
        age = self.watchdog.age(now)
        s['/Huawei/CommandAgeSeconds'] = None if age is None else round(age, 1)
        if m.versions:
            s['/FirmwareVersion'] = 'dcdc %s pfc %s' % (m.versions.get('dcdc_sw'), m.versions.get('pfc_sw'))
            s['/HardwareVersion'] = str(m.versions.get('hw'))
        s['/Alarms/CommunicationLost'] = ALARM_ALARM if m.state == M.DISCONNECTED else \
            ALARM_WARNING if m.state == M.DEGRADED else ALARM_OK
        temp = st.get('out_temp_c')
        s['/Alarms/HighTemperature'] = ALARM_WARNING if temp is not None and temp > HIGH_TEMP_C else ALARM_OK
        s['/Alarms/UncommandedOutput'] = ALARM_ALARM if m.fault and m.fault.startswith('uncommanded') else ALARM_OK
        s['/Alarms/SetpointError'] = ALARM_ALARM if m.fault and (
            m.fault.startswith('no ack') or m.fault.startswith('ack error') or m.fault.startswith('overshoot')) else ALARM_OK
        if self.dcsvc is not None:
            d = self.dcsvc
            d['/Dc/0/Voltage'] = v
            d['/Dc/0/Current'] = i
            d['/Dc/0/Power'] = s['/Dc/0/Power']
            d['/Dc/0/Temperature'] = s['/Dc/0/Temperature']
            d['/Connected'] = s['/Connected']

    def mark_offline(self):
        self.svc['/Connected'] = 0
        self.svc['/State'] = 0
        if self.dcsvc is not None:
            self.dcsvc['/Connected'] = 0
