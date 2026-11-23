# delegates/managedcharger.py
#
# Managed-charger stage for the DVCC delegate of dbus-systemcalc-py 2.103-1
# (Venus OS v2.92). Added by the Huawei R4850G2 integration; not part of the
# Victron package. Licensed under the same terms as dbus-systemcalc-py (MIT).
#
# A "managed charger" is a com.victronenergy.charger service that opts in to
# DVCC control by publishing /Link/ChargeCurrent and /Settings/ChargeCurrentLimit
# and whose /Mgmt/ProcessName identifies the Huawei driver. The stage sits
# between the solar-charger stage and the Multi stage in Dvcc._on_timer:
#
#   budget     = min(BMS CCL, user limit) [+ measured DC load]
#   solar      : unchanged (may be maximised under ESS feed-in)
#   chargers   : budget - solar.smoothed_current, distributed by capacity
#   multi      : budget - solar.smoothed_current - chargers.smoothed_current
#
# Rules: chargers are never maximised; an unknown budget (None) means zero;
# every tracked charger receives a changing /Huawei/Heartbeat value on every
# ADJUST tick in which DVCC is in control (the driver inhibits when it stops);
# chargers that are not able to deliver (State == 0 or not connected) receive 0.

from math import pi, ceil
import logging

from sc_utils import safeadd

HUAWEI_PROCESS_TAG = 'huawei'

logger = logging.getLogger(__name__)


class ManagedCharger(object):
	""" Thin accessor for one managed charger service, modelled on
	    delegates.dvcc.SolarCharger so that the existing distribution code can
	    treat both alike (currentlimit, maxchargecurrent, smoothed_current). """

	def __init__(self, monitor, service):
		from delegates.dvcc import LowPassFilter  # lazy: dvcc imports this module
		self.monitor = monitor
		self.service = service
		self._smoothed_current = LowPassFilter((2 * pi)/20, self.chargecurrent or 0)

	def _get_path(self, path):
		return self.monitor.get_value(self.service, path)

	def _set_path(self, path, v):
		# Only write paths the monitor has actually seen on the service.
		if self.monitor.seen(self.service, path):
			self.monitor.set_value_async(self.service, path, v)

	@property
	def connected(self):
		return self._get_path('/Connected') == 1

	@property
	def state(self):
		return self._get_path('/State')

	@property
	def available(self):
		""" True when the charger reports it can deliver current. The driver
		    publishes State 0 while inhibited (no AC, no heartbeat, fault). """
		s = self.state
		return self.connected and s is not None and s != 0

	@property
	def chargecurrent(self):
		return self._get_path('/Dc/0/Current')

	@property
	def power(self):
		""" DC output power in W: /Dc/0/Power if the driver publishes it,
		    otherwise voltage x current. None if unknown. """
		p = self._get_path('/Dc/0/Power')
		if p is not None:
			return p
		v = self._get_path('/Dc/0/Voltage')
		i = self.chargecurrent
		if v is None or i is None:
			return None
		return v * i

	@property
	def currentlimit(self):
		""" Per-charger ceiling in A, owned by the driver. """
		return self._get_path('/Settings/ChargeCurrentLimit')

	@property
	def maxchargecurrent(self):
		v = self._get_path('/Link/ChargeCurrent')
		return v if v is not None else self.currentlimit

	@maxchargecurrent.setter
	def maxchargecurrent(self, v):
		limit = self.currentlimit
		if limit is None or v is None:
			v = 0
		else:
			v = max(0, min(v, limit))
		self._set_path('/Link/ChargeCurrent', v)

	@property
	def chargevoltage(self):
		return self._get_path('/Link/ChargeVoltage')

	@chargevoltage.setter
	def chargevoltage(self, v):
		self._set_path('/Link/ChargeVoltage', v)

	@property
	def networkmode(self):
		return self._get_path('/Link/NetworkMode')

	@networkmode.setter
	def networkmode(self, v):
		self._set_path('/Link/NetworkMode', v)

	@property
	def heartbeat(self):
		return self._get_path('/Huawei/Heartbeat')

	@heartbeat.setter
	def heartbeat(self, v):
		# The driver's liveness signal. A plain SetValue with an unchanged
		# value never reaches the driver's change callback (vedbus returns
		# early when the value is equal), so this is a counter that changes
		# on every ADJUST tick while DVCC is alive and in control.
		self._set_path('/Huawei/Heartbeat', v)

	@property
	def smoothed_current(self):
		return self._smoothed_current.value

	def update_values(self):
		v = self.chargecurrent
		if v is not None:
			self._smoothed_current.update(v)


class ManagedChargerSubsystem(object):
	""" Collection of managed chargers; the DVCC delegate talks to this. """

	def __init__(self, monitor):
		self.monitor = monitor
		self._chargers = {}
		self._heartbeat = 0
		self._soc_inhibit = False

	def soc_allows(self, soc, max_soc, resume_soc):
		""" SOC ceiling with hysteresis, independent of the BMS limits: the
		    managed chargers get no budget at or above max_soc until the SOC
		    has fallen below resume_soc. An unknown SOC never allows charging.
		    max_soc >= 100 disables the ceiling. Returns True if allowed. """
		if max_soc is None or max_soc >= 100:
			self._soc_inhibit = False
			return True
		if soc is None:
			self._soc_inhibit = True
			return False
		if resume_soc is None or resume_soc > max_soc:
			resume_soc = max_soc
		if self._soc_inhibit:
			if soc < resume_soc:
				self._soc_inhibit = False
		elif soc >= max_soc:
			self._soc_inhibit = True
		return not self._soc_inhibit

	@property
	def soc_inhibited(self):
		return self._soc_inhibit

	@staticmethod
	def is_eligible(monitor, service):
		""" A charger service is managed only if it opted in: it publishes the
		    link path and a per-device limit, and it is the Huawei driver. """
		if not (monitor.seen(service, '/Link/ChargeCurrent') and
				monitor.seen(service, '/Settings/ChargeCurrentLimit')):
			return False
		name = monitor.get_value(service, '/Mgmt/ProcessName') or ''
		return HUAWEI_PROCESS_TAG in str(name).lower()

	def add_charger(self, service):
		self._chargers[service] = charger = ManagedCharger(self.monitor, service)
		logger.info('DVCC: managed charger added: %s', service)
		return charger

	def remove_charger(self, service):
		del self._chargers[service]
		logger.info('DVCC: managed charger removed: %s', service)

	def __iter__(self):
		return iter(self._chargers.values())

	def __len__(self):
		return len(self._chargers)

	def __contains__(self, k):
		return k in self._chargers

	@property
	def capacity(self):
		""" Sum of the per-charger limits (None if no charger has one). """
		return safeadd(*(c.currentlimit for c in self._chargers.values()))

	@property
	def smoothed_current(self):
		return safeadd(*(c.smoothed_current for c in self._chargers.values())) or 0

	@property
	def powers(self):
		""" Per-charger DC power, ordered by service name (i.e. by serial),
		    for the GUI overview tile. """
		return [self._chargers[k].power for k in sorted(self._chargers)]

	@property
	def active_count(self):
		""" Number of chargers that report they can deliver (connected and
		    State != 0). Published for the GUI overview tile. """
		return sum(1 for c in self._chargers.values() if c.available)

	def update_values(self):
		for charger in self._chargers.values():
			charger.update_values()

	def shutdown(self):
		""" Write 0 A to every charger. Used when DVCC is off, the BMS is lost
		    or the budget is unknown. Still a write, so still a heartbeat. """
		for charger in self._chargers.values():
			charger.maxchargecurrent = 0

	def set_networked(self, has_bms, charge_voltage, budget):
		""" Main entry point, called every ADJUST seconds. Writes network mode
		    and charge voltage to all chargers, then distributes `budget`
		    (amperes, may be None) over the chargers that are available.
		    Returns (voltage_written, current_written). """
		if not self._chargers:
			return 0, 0

		# Heartbeat first: a changing value on every tick in which DVCC is in
		# control. Not written by shutdown(), so BMS lost / DVCC off / process
		# death all look the same to the driver: heartbeat stops.
		self._heartbeat = (self._heartbeat + 1) % 1000000
		for charger in self._chargers.values():
			charger.heartbeat = self._heartbeat

		# bit 0: networked, bit 2: remote control of voltage/current,
		# bit 3: BMS present (same encoding as the solar chargers)
		network_mode = 1 | 4 | (8 if has_bms else 0)
		for charger in self._chargers.values():
			charger.networkmode = network_mode

		voltage_written = 0
		if charge_voltage is not None:
			voltage_written = 1
			for charger in self._chargers.values():
				charger.chargevoltage = charge_voltage

		available = [c for c in self._chargers.values()
			if c.available and c.currentlimit is not None]
		idle = [c for c in self._chargers.values() if c not in available]

		# Chargers that cannot deliver get an explicit 0 (heartbeat only).
		for charger in idle:
			charger.maxchargecurrent = 0

		if budget is None or budget <= 0 or not available:
			# Unknown or no budget: zero means zero, never unlimited.
			for charger in available:
				charger.maxchargecurrent = 0
			return voltage_written, 1

		if len(available) == 1:
			sc = available[0]
			sc.maxchargecurrent = min(ceil(budget), sc.currentlimit)
		else:
			capacity = sum(c.currentlimit for c in available)
			if budget >= capacity:
				for charger in available:
					charger.maxchargecurrent = charger.currentlimit
			else:
				from delegates.dvcc import SolarChargerSubsystem  # lazy, see top
				SolarChargerSubsystem._distribute_current(available, budget)

		return voltage_written, 1
