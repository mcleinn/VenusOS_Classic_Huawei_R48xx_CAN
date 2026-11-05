"""Apply the Huawei managed-charger hunks to a pristine dvcc.py (dbus-systemcalc-py 2.103-1).

Usage: python apply_hunks.py <path/to/dvcc.py>
Every anchor must match exactly once, otherwise nothing is written.
This is the source of truth for huawei-dvcc-2.103-1.patch (generated with diff -u).
"""
import sys
import io

HUNKS = [
# H1 import
("""from delegates.base import SystemCalcDelegate
""",
"""from delegates.base import SystemCalcDelegate
from delegates.managedcharger import ManagedChargerSubsystem
"""),
# H2 __init__
("""		self._solarsystem = None
		self._vecan_services = []
""",
"""		self._solarsystem = None
		self._chargersystem = None
		self._vecan_services = []
"""),
# H3 get_input
("""			('com.victronenergy.vecan',	[
""",
"""			('com.victronenergy.charger', [
				'/ProductId',
				'/Connected',
				'/State',
				'/Dc/0/Current',
				'/Dc/0/Voltage',
				'/Dc/0/Power',
				'/Link/NetworkMode',
				'/Link/ChargeVoltage',
				'/Link/ChargeCurrent',
				'/Settings/ChargeCurrentLimit',
				'/Huawei/Heartbeat',
				'/Mgmt/Connection',
				'/Mgmt/ProcessName']),
			('com.victronenergy.vecan',	[
"""),
# H4 get_settings
("""			('bol', '/Settings/Services/Bol', 0, 0, 7)
		]
""",
"""			('bol', '/Settings/Services/Bol', 0, 0, 7),
			# Managed (Huawei) chargers: 0 = solar, chargers, Multi; 1 = solar, Multi, chargers
			('chargerpriority', '/Settings/Dvcc/ChargerPriority', 0, 0, 1),
			# 1 = charger budget includes Multi discharge current (DC-coupled generator path).
			# Default 0: chargers never exceed the battery's own charge limit.
			('chargercompensatevebus', '/Settings/Dvcc/ChargerCompensateVebus', 0, 0, 1),
			# SOC ceiling for the managed chargers (100 = off), with resume hysteresis
			('chargermaxsoc', '/Settings/Dvcc/ChargerMaxSoc', 90, 0, 100),
			('chargerresumesoc', '/Settings/Dvcc/ChargerResumeSoc', 85, 0, 100)
		]
"""),
# H5a set_sources: subsystem
("""		self._multi = Multi(dbusmonitor, dbusservice)

""",
"""		self._multi = Multi(dbusmonitor, dbusservice)
		self._chargersystem = ManagedChargerSubsystem(dbusmonitor)

"""),
# H5b set_sources: paths
("""		self._dbusservice.add_path('/Dvcc/Alarms/MultipleBatteries', value=0)
""",
"""		self._dbusservice.add_path('/Dvcc/Alarms/MultipleBatteries', value=0)
		# Managed (Huawei) charger stage
		self._dbusservice.add_path('/Control/ChargerChargeVoltage', value=0)
		self._dbusservice.add_path('/Control/ChargerChargeCurrent', value=0)
		self._dbusservice.add_path('/Dvcc/Chargers/Budget', value=None)
		self._dbusservice.add_path('/Dvcc/Chargers/Count', value=0)
		self._dbusservice.add_path('/Dvcc/Chargers/SocInhibit', value=0)
		self._dbusservice.add_path('/Dvcc/Chargers/Active', value=0)
		# Per-unit power for the overview tile (two units; None when absent)
		self._dbusservice.add_path('/Dvcc/Chargers/0/Power', value=None)
		self._dbusservice.add_path('/Dvcc/Chargers/1/Power', value=None)
"""),
# H5c per-unit power helper (method on the delegate, placed before device_added)
("""	def device_added(self, service, instance, do_service_change=True):
""",
"""	def _update_charger_powers(self):
		powers = self._chargersystem.powers
		for n in range(2):
			self._dbusservice['/Dvcc/Chargers/%d/Power' % n] = powers[n] if n < len(powers) else None

	def device_added(self, service, instance, do_service_change=True):
"""),
# H6 device_added
("""		elif service_type == 'vecan':
			self._vecan_services.append(service)
""",
"""		elif service_type == 'charger':
			if not ManagedChargerSubsystem.is_eligible(self._dbusmonitor, service):
				return
			self._chargersystem.add_charger(service)
			self._dbusservice['/Dvcc/Chargers/Count'] = len(self._chargersystem)
		elif service_type == 'vecan':
			self._vecan_services.append(service)
"""),
# H7a device_removed
("""		elif service in self._batterysystem:
			self._batterysystem.remove_battery(service)
""",
"""		elif service in self._chargersystem:
			self._chargersystem.remove_charger(service)
			self._dbusservice['/Dvcc/Chargers/Count'] = len(self._chargersystem)
			self._dbusservice['/Dvcc/Chargers/Active'] = self._chargersystem.active_count
			self._update_charger_powers()
		elif service in self._batterysystem:
			self._batterysystem.remove_battery(service)
"""),
# H7b device_removed timer stop
("""			len(self._batterysystem) == 0 and self._timer is not None:
""",
"""			len(self._batterysystem) == 0 and len(self._chargersystem) == 0 and \\
			self._timer is not None:
"""),
# H8a helper
("""			self._dbusservice['/Control/EffectiveChargeVoltage'] = chargevoltage

""",
"""			self._dbusservice['/Control/EffectiveChargeVoltage'] = chargevoltage

		def update_managedcharger_control_flags(voltage_written, current_written, budget):
			self._dbusservice['/Control/ChargerChargeVoltage'] = voltage_written
			self._dbusservice['/Control/ChargerChargeCurrent'] = current_written
			self._dbusservice['/Dvcc/Chargers/Budget'] = budget

"""),
# H8b legacy branch
("""			update_solarcharger_control_flags(voltage_written, current_written, None) # Not tracking for non-DVCC case
""",
"""			update_solarcharger_control_flags(voltage_written, current_written, None) # Not tracking for non-DVCC case
			self._chargersystem.shutdown()
			update_managedcharger_control_flags(0, 0, None)
"""),
# H8c update_values
("""		self._solarsystem.update_values()
		self._multi.update_values(self._solarsystem.capacity)
""",
"""		self._solarsystem.update_values()
		self._chargersystem.update_values()
		self._dbusservice['/Dvcc/Chargers/Active'] = self._chargersystem.active_count
		self._update_charger_powers()
		self._multi.update_values(safeadd(self._solarsystem.capacity, self._chargersystem.capacity))
"""),
# H8d BMS lost
("""			# BMS is lost
			update_solarcharger_control_flags(0, 0, None)
			return True
""",
"""			# BMS is lost
			update_solarcharger_control_flags(0, 0, None)
			self._chargersystem.shutdown()
			update_managedcharger_control_flags(0, 0, None)
			return True
"""),
# H8e charger stage + Multi remainder
("""		update_solarcharger_control_flags(voltage_written, current_written, effective_charge_voltage)

		# The Multi gets the remainder after subtracting what the solar chargers made
		if max_charge_current is not None:
			max_charge_current = max(0.0, round(max_charge_current - self._solarsystem.smoothed_current))
""",
"""		update_solarcharger_control_flags(voltage_written, current_written, effective_charge_voltage)

		# Managed (Huawei) chargers get what is left after the solar chargers.
		# They are never maximised, and an unknown budget means zero.
		charger_budget = _max_charge_current if self._settings['chargercompensatevebus'] else max_charge_current
		if max_charge_current is not None and max_charge_current <= 0:
			# The battery may not be charged at all: no compensation for the
			# Multi either, the chargers stay off.
			charger_budget = 0.0
		# SOC ceiling, independent of the BMS limits (unknown SOC = no budget)
		if not self._chargersystem.soc_allows(self._dbusservice['/Dc/Battery/Soc'],
				self._settings['chargermaxsoc'], self._settings['chargerresumesoc']):
			charger_budget = 0.0
		self._dbusservice['/Dvcc/Chargers/SocInhibit'] = int(self._chargersystem.soc_inhibited)
		if charger_budget is not None:
			charger_budget -= self._solarsystem.smoothed_current
			if self._settings['chargerpriority'] == 1:
				# Multi before chargers: also subtract what the Multi is charging with
				charger_budget -= max(0.0, self._multi.dc_current)
			charger_budget = max(0.0, round(charger_budget, 1))
		c_voltage_written, c_current_written = self._chargersystem.set_networked(
			has_bms, effective_charge_voltage, charger_budget)
		update_managedcharger_control_flags(c_voltage_written, c_current_written, charger_budget)

		# The Multi gets the remainder after subtracting what the solar chargers
		# (and, by default, the managed chargers) made
		if max_charge_current is not None:
			max_charge_current -= self._solarsystem.smoothed_current
			if self._settings['chargerpriority'] == 0:
				max_charge_current -= self._chargersystem.smoothed_current
			max_charge_current = max(0.0, round(max_charge_current))
"""),
]


def main():
	path = sys.argv[1]
	src = io.open(path, encoding='utf-8', newline='').read()
	out = src
	for i, (old, new) in enumerate(HUNKS, 1):
		n = out.count(old)
		if n != 1:
			sys.exit('hunk %d: anchor found %d times, expected 1' % (i, n))
		out = out.replace(old, new)
	io.open(path, 'w', encoding='utf-8', newline='').write(out)
	print('applied %d hunks to %s' % (len(HUNKS), path))


if __name__ == '__main__':
	main()
