"""Apply the Huawei managed-charger hunks to a pristine dvcc.py (dbus-systemcalc-py 2.257-1,
Venus OS v3.85).

Usage: python apply_hunks.py <path/to/dvcc.py>
Every anchor must match exactly once, otherwise nothing is written.
This is the source of truth for huawei-dvcc-2.257-1.patch (generated with diff -u) and for
patched-dvcc.py. Port of ../2.103-1/apply_hunks.py: same stage, same D-Bus paths and settings,
anchored to the 2.257-1 code (charger classes, ChargerSubsystem, BatteryService, DESS charge power).
"""
import sys
import io

HUNKS = [
# H1 import: after the charger classes, right before the delegate, so that
# managedcharger.py can subclass BaseCharger while dvcc is still importing.
("""class Dvcc(SystemCalcDelegate):
	\"\"\" This is the main DVCC delegate object. \"\"\"
""",
"""from delegates.managedcharger import ManagedChargerSubsystem  # Huawei: needs the classes above

class Dvcc(SystemCalcDelegate):
	\"\"\" This is the main DVCC delegate object. \"\"\"
"""),
# H2 __init__
("""		self._chargesystem = None
		self._vecan_services = []
""",
"""		self._chargesystem = None
		self._managedchargers = None
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
("""			('bolsecondary', '/Settings/SystemSetup/DvccControlAllMultis', 0, 0, 1)
		]
""",
"""			('bolsecondary', '/Settings/SystemSetup/DvccControlAllMultis', 0, 0, 1),
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

		self._dbusservice.add_path('/Control/SolarChargeVoltage', value=0)
""",
"""		self._multi = Multi(dbusmonitor, dbusservice)
		self._managedchargers = ManagedChargerSubsystem(dbusmonitor)

		self._dbusservice.add_path('/Control/SolarChargeVoltage', value=0)
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
("""	def device_added(self, service, instance, *args, **kwargs):
		service_type = service.split('.')[2]
		if service_type == 'solarcharger':
""",
"""	def _update_charger_powers(self):
		powers = self._managedchargers.powers
		for n in range(2):
			self._dbusservice['/Dvcc/Chargers/%d/Power' % n] = powers[n] if n < len(powers) else None

	def device_added(self, service, instance, *args, **kwargs):
		service_type = service.split('.')[2]
		if service_type == 'solarcharger':
"""),
# H6 device_added: managed chargers install the timer like any charger
("""		elif service_type == 'vecan':
			self._vecan_services.append(service)
		elif service_type == 'alternator':
""",
"""		elif service_type == 'charger':
			if not ManagedChargerSubsystem.is_eligible(self._dbusmonitor, service):
				return
			self._managedchargers.add_charger(service)
			self._dbusservice['/Dvcc/Chargers/Count'] = len(self._managedchargers)
		elif service_type == 'vecan':
			self._vecan_services.append(service)
		elif service_type == 'alternator':
"""),
# H7a device_removed
("""		elif service in self._inverters:
			self._inverters.remove_inverter(service)
		elif self._chargesystem.remove_acsystem(service):
			pass
""",
"""		elif service in self._inverters:
			self._inverters.remove_inverter(service)
		elif service in self._managedchargers:
			self._managedchargers.remove_charger(service)
			self._dbusservice['/Dvcc/Chargers/Count'] = len(self._managedchargers)
			self._dbusservice['/Dvcc/Chargers/Active'] = self._managedchargers.active_count
			self._update_charger_powers()
		elif self._chargesystem.remove_acsystem(service):
			pass
"""),
# H7b device_removed timer stop
("""		if len(self._chargesystem) == 0 and len(self._vecan_services) == 0 and \\
			len(BatteryService.instance.batteries) == 0 and self._timer is not None:
""",
"""		if len(self._chargesystem) == 0 and len(self._vecan_services) == 0 and \\
			len(self._managedchargers) == 0 and \\
			len(BatteryService.instance.batteries) == 0 and self._timer is not None:
"""),
# H8a helper
("""			self._dbusservice['/Control/EffectiveChargeVoltage'] = chargevoltage

		self._tickcount -= 1; self._tickcount %= ADJUST
""",
"""			self._dbusservice['/Control/EffectiveChargeVoltage'] = chargevoltage

		def update_managedcharger_control_flags(voltage_written, current_written, budget):
			self._dbusservice['/Control/ChargerChargeVoltage'] = voltage_written
			self._dbusservice['/Control/ChargerChargeCurrent'] = current_written
			self._dbusservice['/Dvcc/Chargers/Budget'] = budget

		self._tickcount -= 1; self._tickcount %= ADJUST
"""),
# H8b legacy (DVCC off) branch
("""			update_solarcharger_control_flags(voltage_written, current_written, None) # Not tracking for non-DVCC case
			self._dbusservice['/Control/BmsParameters'] = 0
""",
"""			update_solarcharger_control_flags(voltage_written, current_written, None) # Not tracking for non-DVCC case
			self._managedchargers.shutdown()
			update_managedcharger_control_flags(0, 0, None)
			self._dbusservice['/Control/BmsParameters'] = 0
"""),
# H8c update_values
("""		self._chargesystem.update_values()
		self._multi.update_values(self._chargesystem.totalcapacity)
""",
"""		self._chargesystem.update_values()
		self._managedchargers.update_values()
		self._dbusservice['/Dvcc/Chargers/Active'] = self._managedchargers.active_count
		self._update_charger_powers()
		self._multi.update_values(safeadd(self._chargesystem.totalcapacity, self._managedchargers.capacity))
"""),
# H8d BMS lost
("""			# BMS is lost
			update_solarcharger_control_flags(0, 0, None)
			self._dbusservice['/Dc/Battery/ChargeVoltage'] = None
			return True
""",
"""			# BMS is lost
			update_solarcharger_control_flags(0, 0, None)
			self._managedchargers.shutdown()
			update_managedcharger_control_flags(0, 0, None)
			self._dbusservice['/Dc/Battery/ChargeVoltage'] = None
			return True
"""),
# H8e charger stage + Multi remainder
("""		update_solarcharger_control_flags(voltage_written, current_written, effective_charge_voltage)

		# The Multi gets the remainder after subtracting what the solar
		# chargers made. If there is a maximum charge power from another
		# delegate (dynamicess), apply that here.
		if max_charge_current is not None:
			max_charge_current = max(0.0, round(max_charge_current - self._chargesystem.smoothed_current))

		try:
			internal_mcc = self.internal_maxchargepower / self._dbusservice['/Dc/Battery/Voltage']
		except (TypeError, ZeroDivisionError, ValueError):
			pass
		else:
			try:
				max_charge_current = min(x for x in (ceil(internal_mcc), max_charge_current) if x is not None)
			except ValueError:
				pass
""",
"""		update_solarcharger_control_flags(voltage_written, current_written, effective_charge_voltage)

		# A maximum charge power from another delegate (dynamicess) caps the
		# managed chargers and the Multi alike.
		try:
			internal_mcc = ceil(self.internal_maxchargepower / self._dbusservice['/Dc/Battery/Voltage'])
		except (TypeError, ZeroDivisionError, ValueError):
			internal_mcc = None

		# Managed (Huawei) chargers get what is left after the stock chargers.
		# They are never maximised, and an unknown budget means zero.
		charger_budget = _max_charge_current if self._settings['chargercompensatevebus'] else max_charge_current
		if max_charge_current is not None and max_charge_current <= 0:
			# The battery may not be charged at all: no compensation for the
			# Multi either, the chargers stay off.
			charger_budget = 0.0
		# SOC ceiling, independent of the BMS limits (unknown SOC = no budget)
		if not self._managedchargers.soc_allows(self._dbusservice['/Dc/Battery/Soc'],
				self._settings['chargermaxsoc'], self._settings['chargerresumesoc']):
			charger_budget = 0.0
		self._dbusservice['/Dvcc/Chargers/SocInhibit'] = int(self._managedchargers.soc_inhibited)
		if charger_budget is not None:
			if internal_mcc is not None:
				charger_budget = min(charger_budget, internal_mcc)
			charger_budget -= self._chargesystem.smoothed_current
			if self._settings['chargerpriority'] == 1:
				# Multi before chargers: also subtract what the Multi is charging with
				charger_budget -= max(0.0, self._multi.dc_current)
			charger_budget = max(0.0, round(charger_budget, 1))
		c_voltage_written, c_current_written = self._managedchargers.set_networked(
			has_bms, effective_charge_voltage, charger_budget)
		update_managedcharger_control_flags(c_voltage_written, c_current_written, charger_budget)

		# The Multi gets the remainder after subtracting what the solar
		# chargers (and, by default, the managed chargers) made.
		if max_charge_current is not None:
			max_charge_current -= self._chargesystem.smoothed_current
			if self._settings['chargerpriority'] == 0:
				max_charge_current -= self._managedchargers.smoothed_current
			max_charge_current = max(0.0, round(max_charge_current))

		if internal_mcc is not None:
			try:
				max_charge_current = min(x for x in (internal_mcc, max_charge_current) if x is not None)
			except ValueError:
				pass
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
