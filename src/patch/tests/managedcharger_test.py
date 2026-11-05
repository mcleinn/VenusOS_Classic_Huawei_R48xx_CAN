# Tests for the managed (Huawei) charger stage added to delegates/dvcc.py.
# Runs inside the dbus-systemcalc-py test harness (tests/base.py, mock D-Bus).
import unittest

# This adapts sys.path so mock modules can be found
import context
from base import TestSystemCalcBase
from delegates.managedcharger import ManagedChargerSubsystem

# Monkey patching for unit tests
import patches

VEBUS = 'com.victronenergy.vebus.ttyO1'
BATTERY = 'com.victronenergy.battery.ttyO2'
SOLAR = 'com.victronenergy.solarcharger.ttyO3'
HUAWEI1 = 'com.victronenergy.charger.huawei_A'
HUAWEI2 = 'com.victronenergy.charger.huawei_B'


class TestManagedCharger(TestSystemCalcBase):
	def __init__(self, methodName='runTest'):
		TestSystemCalcBase.__init__(self, methodName)

	def setUp(self):
		TestSystemCalcBase.setUp(self)
		# ESS Multi with BOL support, idle on DC
		self._add_device(VEBUS, product_name='Multi', values={
			'/Ac/ActiveIn/L1/P': 123,
			'/Ac/ActiveIn/ActiveInput': 0,
			'/Ac/ActiveIn/Connected': 1,
			'/Ac/Out/L1/P': 100,
			'/Dc/0/Voltage': 52.0,
			'/Dc/0/Current': 0,
			'/Dc/0/Temperature': 24,
			'/DeviceInstance': 0,
			'/Devices/0/Assistants': [0x55, 0x1] + (26 * [0]),
			'/Devices/Bms/Version': None,
			'/Dc/0/MaxChargeCurrent': 999,
			'/ExtraBatteryCurrent': 0,
			'/Soc': 53.2,
			'/State': 3,
			'/BatteryOperationalLimits/MaxChargeVoltage': None,
			'/BatteryOperationalLimits/MaxChargeCurrent': None,
			'/BatteryOperationalLimits/MaxDischargeCurrent': None,
			'/BatteryOperationalLimits/BatteryLowVoltage': None,
			'/BatterySense/Voltage': None,
			'/Bms/AllowToCharge': 1,
			'/Bms/AllowToDischarge': 1,
			'/Bms/BmsType': 0,
			'/FirmwareFeatures/BolFrame': 1,
			'/FirmwareFeatures/BolUBatAndTBatSense': 1,
			'/FirmwareVersion': 0x456,
			'/Hub4/AssistantId': 5,
			'/Hub/ChargeVoltage': 52.4,
			'/Hub4/L1/DoNotFeedInOvervoltage': 0,
			'/Interfaces/Mk2/ProductName': 'MK3'})
		self._add_device('com.victronenergy.settings', values={
			'/Settings/SystemSetup/AcInput1': 2,
			'/Settings/SystemSetup/AcInput2': 1,
			'/Settings/CGwacs/OvervoltageFeedIn': 1})
		self._set_setting('/Settings/Services/Bol', 1)
		self._set_setting('/Settings/SystemSetup/MaxChargeCurrent', -1)

	def _add_bms(self, ccl=100, cvl=52.8, soc=50):
		self._add_device(BATTERY, product_name='battery', values={
			'/Dc/0/Voltage': 52.0,
			'/Dc/0/Current': 5.3,
			'/Dc/0/Power': 275,
			'/Soc': soc,
			'/DeviceInstance': 2,
			'/Info/BatteryLowVoltage': 47,
			'/Info/MaxChargeCurrent': ccl,
			'/Info/MaxChargeVoltage': cvl,
			'/Info/MaxDischargeCurrent': 100})

	def _add_huawei(self, service, limit=50.0, current=0.0, state=252, process='huawei-charger'):
		self._add_device(service, {
			'/ProductId': 0xB0FF,
			'/State': state,
			'/Dc/0/Voltage': 52.0,
			'/Dc/0/Current': current,
			'/Link/NetworkMode': None,
			'/Link/ChargeVoltage': None,
			'/Link/ChargeCurrent': None,
			'/Settings/ChargeCurrentLimit': limit,
			'/Huawei/Heartbeat': None,
			'/Mgmt/ProcessName': process,
			'/DeviceInstance': 290}, connection='CAN')

	def _add_solar(self, limit=35, current=0):
		self._add_device(SOLAR, {
			'/State': 3,
			'/Settings/ChargeCurrentLimit': limit,
			'/Link/NetworkMode': 0,
			'/Link/ChargeVoltage': None,
			'/Link/ChargeCurrent': None,
			'/Link/VoltageSense': None,
			'/Dc/0/Voltage': 52.0,
			'/Dc/0/Current': current,
			'/FirmwareVersion': 0x0129}, connection='VE.Direct')

	# --- eligibility -----------------------------------------------------

	def test_not_eligible_without_link_path(self):
		self._add_bms()
		self._add_device(HUAWEI1, {
			'/State': 3, '/Dc/0/Voltage': 52.0, '/Dc/0/Current': 0,
			'/Settings/ChargeCurrentLimit': 50, '/Mgmt/ProcessName': 'huawei-charger',
			'/DeviceInstance': 290})
		self._update_values(interval=6000)
		self._check_values({'/Dvcc/Chargers/Count': 0, '/Control/ChargerChargeCurrent': 0})

	def test_not_eligible_foreign_process(self):
		self._add_bms()
		self._add_huawei(HUAWEI1, process='dbus-skylla')
		self._update_values(interval=6000)
		self._check_values({'/Dvcc/Chargers/Count': 0, '/Control/ChargerChargeCurrent': 0})
		self.assertIsNone(self._monitor.get_value(HUAWEI1, '/Link/ChargeCurrent'))

	def test_eligible_stub_processname(self):
		self._add_bms()
		self._add_huawei(HUAWEI1, process='huawei_charger_stub.py')
		self._update_values(interval=6000)
		self._check_values({'/Dvcc/Chargers/Count': 1})

	# --- allocation ------------------------------------------------------

	def test_single_charger_gets_bms_limit(self):
		self._add_bms(ccl=40)
		self._add_huawei(HUAWEI1)
		self._update_values(interval=6000)
		self._check_external_values({HUAWEI1: {
			'/Link/NetworkMode': 13,
			'/Link/ChargeVoltage': 52.4,
			'/Link/ChargeCurrent': 40}})
		self._check_values({
			'/Control/ChargerChargeCurrent': 1,
			'/Control/ChargerChargeVoltage': 1,
			'/Dvcc/Chargers/Budget': 40,
			'/Dvcc/Chargers/Count': 1})

	def test_single_charger_capped_by_own_limit(self):
		self._add_bms(ccl=100)
		self._add_huawei(HUAWEI1, limit=50)
		self._update_values(interval=6000)
		self._check_external_values({HUAWEI1: {'/Link/ChargeCurrent': 50}})

	def test_user_limit_applies(self):
		self._add_bms(ccl=100)
		self._set_setting('/Settings/SystemSetup/MaxChargeCurrent', 30)
		self._add_huawei(HUAWEI1)
		self._update_values(interval=6000)
		self._check_external_values({HUAWEI1: {'/Link/ChargeCurrent': 30}})

	def test_two_chargers_split(self):
		self._add_bms(ccl=60)
		self._add_huawei(HUAWEI1, limit=50)
		self._add_huawei(HUAWEI2, limit=50)
		self._update_values(interval=6000)
		a = self._monitor.get_value(HUAWEI1, '/Link/ChargeCurrent')
		b = self._monitor.get_value(HUAWEI2, '/Link/ChargeCurrent')
		self.assertAlmostEqual(a + b, 60, delta=0.2)
		self.assertAlmostEqual(a, 30, delta=0.2)

	def test_two_chargers_budget_above_capacity(self):
		self._add_bms(ccl=150)
		self._add_huawei(HUAWEI1, limit=50)
		self._add_huawei(HUAWEI2, limit=40)
		self._update_values(interval=6000)
		self._check_external_values({
			HUAWEI1: {'/Link/ChargeCurrent': 50},
			HUAWEI2: {'/Link/ChargeCurrent': 40}})

	def test_idle_charger_gets_zero_and_other_gets_all(self):
		self._add_bms(ccl=60)
		self._add_huawei(HUAWEI1, limit=50, state=252)
		self._add_huawei(HUAWEI2, limit=50, state=0)
		self._update_values(interval=6000)
		self._check_external_values({
			HUAWEI1: {'/Link/ChargeCurrent': 50},
			HUAWEI2: {'/Link/ChargeCurrent': 0}})

	def test_feedback_allowed_does_not_maximise_chargers(self):
		# ESS with feed-in: solar is maximised, the Huawei stays on budget
		self._add_bms(ccl=40)
		self._add_solar(limit=35, current=0)
		self._add_huawei(HUAWEI1, limit=50)
		self._update_values(interval=6000)
		self._check_external_values({
			SOLAR: {'/Link/ChargeCurrent': 35},
			HUAWEI1: {'/Link/ChargeCurrent': 40}})

	def test_solar_output_reduces_charger_budget(self):
		self._add_bms(ccl=40)
		self._add_solar(limit=35, current=20)
		self._add_huawei(HUAWEI1, limit=50)
		self._update_values(interval=60000)   # let the 20 s low-pass settle
		v = self._monitor.get_value(HUAWEI1, '/Link/ChargeCurrent')
		self.assertAlmostEqual(v, 20, delta=1)

	def test_multi_remainder_reduced_by_charger_output(self):
		self._add_bms(ccl=40)
		self._add_huawei(HUAWEI1, limit=50, current=30)
		self._update_values(interval=60000)
		self._check_external_values({
			VEBUS: {'/BatteryOperationalLimits/MaxChargeCurrent': 10}})

	def test_priority_multi_first(self):
		self._set_setting('/Settings/Dvcc/ChargerPriority', 1)
		self._add_bms(ccl=40)
		self._monitor.set_value(VEBUS, '/Dc/0/Current', 25)   # Multi charging with 25 A
		self._add_huawei(HUAWEI1, limit=50, current=10)
		self._update_values(interval=60000)
		v = self._monitor.get_value(HUAWEI1, '/Link/ChargeCurrent')
		self.assertAlmostEqual(v, 15, delta=1)
		# Multi keeps the whole budget in this mode
		self._check_external_values({
			VEBUS: {'/BatteryOperationalLimits/MaxChargeCurrent': 40}})

	def test_vebus_compensation_default_off(self):
		# Multi inverting 20 A: by default the chargers stay at the CCL
		self._add_bms(ccl=40)
		self._monitor.set_value(VEBUS, '/Dc/0/Current', -20)
		self._add_huawei(HUAWEI1, limit=100)
		self._update_values(interval=60000)
		self._check_external_values({HUAWEI1: {'/Link/ChargeCurrent': 40}})

	def test_vebus_compensation_on(self):
		# Opt-in: chargers may cover the Multi's discharge on top of the CCL
		self._set_setting('/Settings/Dvcc/ChargerCompensateVebus', 1)
		self._add_bms(ccl=40)
		self._monitor.set_value(VEBUS, '/Dc/0/Current', -20)
		self._add_huawei(HUAWEI1, limit=100)
		self._update_values(interval=60000)
		v = self._monitor.get_value(HUAWEI1, '/Link/ChargeCurrent')
		self.assertAlmostEqual(v, 60, delta=1)

	def test_ccl_zero_with_compensation_on_still_zero(self):
		# Battery may not be charged: no compensation even if the Multi inverts
		self._set_setting('/Settings/Dvcc/ChargerCompensateVebus', 1)
		self._add_bms(ccl=0)
		self._monitor.set_value(VEBUS, '/Dc/0/Current', -20)
		self._add_huawei(HUAWEI1, limit=100)
		self._update_values(interval=60000)
		self._check_external_values({HUAWEI1: {'/Link/ChargeCurrent': 0}})
		self._check_values({'/Dvcc/Chargers/Budget': 0})

	def test_pylontech_voltage_clamp_reaches_chargers(self):
		# Pylontech quirk: CVL clamped to 52.4 V; ESS hub voltage is what the
		# solar chargers get and the managed chargers get the same value
		self._monitor.set_value(VEBUS, '/Hub/ChargeVoltage', 53.2)
		self._add_bms(ccl=40, cvl=53.2)
		self._monitor.add_value(BATTERY, '/ProductId', 0xB009)   # Pylontech
		self._add_solar()
		self._add_huawei(HUAWEI1)
		self._update_values(interval=6000)
		solar_v = self._monitor.get_value(SOLAR, '/Link/ChargeVoltage')
		huawei_v = self._monitor.get_value(HUAWEI1, '/Link/ChargeVoltage')
		self.assertEqual(solar_v, huawei_v)
		self._check_external_values({VEBUS: {'/BatteryOperationalLimits/MaxChargeVoltage': 52.4}})

	# --- zero / unknown / lost -------------------------------------------

	def test_ccl_zero_gives_zero(self):
		self._add_bms(ccl=0)
		self._add_huawei(HUAWEI1)
		self._update_values(interval=6000)
		self._check_external_values({HUAWEI1: {'/Link/ChargeCurrent': 0}})
		self._check_values({'/Control/ChargerChargeCurrent': 1, '/Dvcc/Chargers/Budget': 0})

	def test_no_bms_no_user_limit_gives_zero(self):
		# Unknown budget is never unlimited (SOC ceiling off to isolate the budget rule)
		self._set_setting('/Settings/Dvcc/ChargerMaxSoc', 100)
		self._add_huawei(HUAWEI1)
		self._update_values(interval=6000)
		self._check_external_values({HUAWEI1: {'/Link/ChargeCurrent': 0}})
		self._check_values({'/Dvcc/Chargers/Budget': None})

	def test_no_bms_user_limit_applies(self):
		# No battery service: SOC unknown, so with the default ceiling the
		# chargers stay at 0; with the ceiling off the user limit applies.
		self._set_setting('/Settings/SystemSetup/MaxChargeCurrent', 25)
		self._add_huawei(HUAWEI1)
		self._update_values(interval=6000)
		self._check_external_values({HUAWEI1: {'/Link/ChargeCurrent': 0}})
		self._check_values({'/Dvcc/Chargers/SocInhibit': 1})
		self._set_setting('/Settings/Dvcc/ChargerMaxSoc', 100)
		self._update_values(interval=6000)
		self._check_external_values({HUAWEI1: {'/Link/ChargeCurrent': 25}})

	def test_bms_lost_shuts_chargers_down(self):
		self._add_bms(ccl=40)
		self._add_solar()
		self._monitor.add_value(SOLAR, '/Settings/BmsPresent', 1)
		self._add_huawei(HUAWEI1)
		self._update_values(interval=6000)
		self._check_external_values({HUAWEI1: {'/Link/ChargeCurrent': 40}})
		self._remove_device(BATTERY)
		self._update_values(interval=6000)
		self._check_external_values({HUAWEI1: {'/Link/ChargeCurrent': 0}})
		self._check_values({'/Control/ChargerChargeCurrent': 0})

	def test_dvcc_off_shuts_chargers_down(self):
		self._add_bms(ccl=40)
		self._add_huawei(HUAWEI1)
		self._update_values(interval=6000)
		self._check_external_values({HUAWEI1: {'/Link/ChargeCurrent': 40}})
		self._set_setting('/Settings/Services/Bol', 0)
		self._update_values(interval=6000)
		self._check_external_values({HUAWEI1: {'/Link/ChargeCurrent': 0}})
		self._check_values({'/Control/ChargerChargeCurrent': 0})

	def test_charger_removed(self):
		self._add_bms(ccl=40)
		self._add_huawei(HUAWEI1)
		self._add_huawei(HUAWEI2)
		self._update_values(interval=6000)
		self._check_values({'/Dvcc/Chargers/Count': 2})
		self._remove_device(HUAWEI2)
		self._update_values(interval=6000)
		self._check_values({'/Dvcc/Chargers/Count': 1})
		self._check_external_values({HUAWEI1: {'/Link/ChargeCurrent': 40}})

	def test_heartbeat_changes_every_adjust_tick(self):
		# The heartbeat value changes on every 3 s tick even when the budget
		# does not; it stops (value frozen) when the BMS is lost.
		self._add_bms(ccl=40)
		self._add_huawei(HUAWEI1)
		self._add_huawei(HUAWEI2)
		self._update_values(interval=3000)
		h1 = self._monitor.get_value(HUAWEI1, '/Huawei/Heartbeat')
		self.assertIsNotNone(h1)
		self._update_values(interval=3000)
		h2 = self._monitor.get_value(HUAWEI1, '/Huawei/Heartbeat')
		self.assertNotEqual(h1, h2)
		self.assertEqual(h2, self._monitor.get_value(HUAWEI2, '/Huawei/Heartbeat'))
		self._update_values(interval=30000)
		h3 = self._monitor.get_value(HUAWEI1, '/Huawei/Heartbeat')
		self.assertEqual(h3 - h2, 10)
		# BMS lost: zero written, heartbeat frozen from that tick on
		self._add_solar()
		self._monitor.add_value(SOLAR, '/Settings/BmsPresent', 1)
		self._remove_device(BATTERY)
		self._update_values(interval=30000)
		self._check_external_values({HUAWEI1: {'/Link/ChargeCurrent': 0}})
		self.assertEqual(self._monitor.get_value(HUAWEI1, '/Huawei/Heartbeat'), h3)

	def test_charger_without_heartbeat_path_still_managed(self):
		# Older driver without the path: no heartbeat write, everything else works
		self._add_bms(ccl=40)
		self._add_device(HUAWEI1, {
			'/ProductId': 0xB0FF, '/State': 252, '/Dc/0/Voltage': 52.0, '/Dc/0/Current': 0,
			'/Link/NetworkMode': None, '/Link/ChargeVoltage': None, '/Link/ChargeCurrent': None,
			'/Settings/ChargeCurrentLimit': 50, '/Mgmt/ProcessName': 'huawei-charger',
			'/DeviceInstance': 290}, connection='CAN')
		self._update_values(interval=6000)
		self._check_external_values({HUAWEI1: {'/Link/ChargeCurrent': 40}})
		self.assertIsNone(self._monitor.get_value(HUAWEI1, '/Huawei/Heartbeat'))

	# --- SOC ceiling ----------------------------------------------------

	def test_soc_ceiling_default_90(self):
		self._add_bms(ccl=100, soc=95)
		self._add_huawei(HUAWEI1)
		self._update_values(interval=6000)
		self._check_external_values({HUAWEI1: {'/Link/ChargeCurrent': 0}})
		self._check_values({'/Dvcc/Chargers/SocInhibit': 1, '/Dvcc/Chargers/Budget': 0})
		self._check_values({'/Dc/Battery/Soc': 95})

	def test_soc_ceiling_hysteresis(self):
		self._add_bms(ccl=100, soc=80)
		self._add_huawei(HUAWEI1)
		self._update_values(interval=6000)
		self._check_external_values({HUAWEI1: {'/Link/ChargeCurrent': 50}})
		self._monitor.set_value(BATTERY, '/Soc', 90)
		self._update_values(interval=6000)
		self._check_external_values({HUAWEI1: {'/Link/ChargeCurrent': 0}})
		self._check_values({'/Dvcc/Chargers/SocInhibit': 1})
		self._monitor.set_value(BATTERY, '/Soc', 87)      # between resume and max: stays off
		self._update_values(interval=6000)
		self._check_external_values({HUAWEI1: {'/Link/ChargeCurrent': 0}})
		self._monitor.set_value(BATTERY, '/Soc', 84)      # below resume: on again
		self._update_values(interval=6000)
		self._check_external_values({HUAWEI1: {'/Link/ChargeCurrent': 50}})
		self._check_values({'/Dvcc/Chargers/SocInhibit': 0})

	def test_soc_ceiling_off_at_100(self):
		self._set_setting('/Settings/Dvcc/ChargerMaxSoc', 100)
		self._add_bms(ccl=100, soc=99)
		self._add_huawei(HUAWEI1)
		self._update_values(interval=6000)
		self._check_external_values({HUAWEI1: {'/Link/ChargeCurrent': 50}})

	def test_soc_unknown_gives_zero(self):
		self._add_bms(ccl=100, soc=None)
		self._add_huawei(HUAWEI1)
		self._update_values(interval=6000)
		self._check_external_values({HUAWEI1: {'/Link/ChargeCurrent': 0}})
		self._check_values({'/Dvcc/Chargers/SocInhibit': 1})

	def test_soc_ceiling_does_not_touch_solar_or_multi(self):
		self._add_bms(ccl=40, soc=95)
		self._add_solar(limit=35, current=0)
		self._add_huawei(HUAWEI1)
		self._update_values(interval=6000)
		self._check_external_values({
			SOLAR: {'/Link/ChargeCurrent': 35},
			HUAWEI1: {'/Link/ChargeCurrent': 0},
			VEBUS: {'/BatteryOperationalLimits/MaxChargeCurrent': 40}})

	# --- regression: existing behaviour with no managed charger ----------

	def test_active_count_follows_state(self):
		# /Dvcc/Chargers/Active feeds the GUI overview tile: chargers that are
		# connected and not in State 0 (inhibited / off).
		self._add_bms()
		self._add_huawei(HUAWEI1, state=0)
		self._add_huawei(HUAWEI2, state=252)
		self._update_values(3000)
		self._check_values({'/Dvcc/Chargers/Count': 2, '/Dvcc/Chargers/Active': 1})
		self._monitor.set_value(HUAWEI1, '/State', 252)
		self._update_values(3000)
		self._check_values({'/Dvcc/Chargers/Active': 2})
		self._monitor.set_value(HUAWEI1, '/Connected', 0)
		self._update_values(3000)
		self._check_values({'/Dvcc/Chargers/Active': 1})
		self._remove_device(HUAWEI2)
		self._update_values(3000)
		self._check_values({'/Dvcc/Chargers/Count': 1, '/Dvcc/Chargers/Active': 0})

	def test_per_unit_power_for_tile(self):
		# /Dvcc/Chargers/0|1/Power: per unit, ordered by service name, V x I
		# when the driver publishes no /Dc/0/Power; None for a missing unit.
		self._add_bms()
		self._check_values({'/Dvcc/Chargers/0/Power': None, '/Dvcc/Chargers/1/Power': None})
		self._add_huawei(HUAWEI2, current=20.0)
		self._update_values(3000)
		self._check_values({'/Dvcc/Chargers/0/Power': 1040.0, '/Dvcc/Chargers/1/Power': None})
		self._add_huawei(HUAWEI1, current=10.0)
		self._update_values(3000)
		self._check_values({'/Dvcc/Chargers/0/Power': 520.0, '/Dvcc/Chargers/1/Power': 1040.0})
		self._monitor.set_value(HUAWEI2, '/Dc/0/Current', 30.0)
		self._update_values(3000)
		self._check_values({'/Dvcc/Chargers/1/Power': 1560.0})
		self._remove_device(HUAWEI1)
		self._update_values(3000)
		self._check_values({'/Dvcc/Chargers/0/Power': 1560.0, '/Dvcc/Chargers/1/Power': None})

	def test_no_charger_leaves_solar_and_multi_unchanged(self):
		self._add_bms(ccl=40)
		self._add_solar(limit=35, current=0)
		self._update_values(interval=6000)
		self._check_external_values({
			SOLAR: {'/Link/NetworkMode': 13, '/Link/ChargeCurrent': 35, '/Link/ChargeVoltage': 52.4},
			VEBUS: {'/BatteryOperationalLimits/MaxChargeCurrent': 40}})
		self._check_values({
			'/Control/ChargerChargeCurrent': 0,
			'/Control/ChargerChargeVoltage': 0,
			'/Dvcc/Chargers/Count': 0})


if __name__ == '__main__':
	unittest.main()
