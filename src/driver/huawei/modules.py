"""Per-unit model for the Huawei driver: identity by serial, address map, freshness,
state machine, and the strict separation of requested / sent / acknowledged / measured.

Rules (brief sections 12 and 16, Phase A/B findings):
- A unit is identified by its serial (info 0x0002 or the address-negotiation frames).
  The bus address is a transient attribute that is relearned from the bus; a status frame
  from an address with no known serial is kept aside until the serial is known.
- Nothing is ever assumed: a value is unknown (None) until a frame carried it, and stale
  once older than its freshness window. Stale control inputs mean "inhibit", never "keep".
- Measured output above a small threshold while the driver believes the unit is inhibited
  is an "uncommanded output" fault: the unit is reported faulted until the condition clears.
  Exception: while the engine reports a bench window (huawei-ramp.py keeps the marker file
  /data/huawei/bench fresh) another master is expected to command output and the check is
  suspended; the engine grants that only to the telemetry-only commander.
- Model facts (R4875G5, measured 2026-10-25/11-01): the output voltage comes from GET 0x85
  when the burst's 0x75 reads 0; 0x76 is not a readback of the on-line limit (only reported
  as a ratio, never as a measured limit unless the model is in protocol.LIMIT_READBACK_MODELS);
  unsolicited byte 3 is informational and never drives the state. The model name comes from
  the e-label (BoardType/Model), requested once by the engine.
- Zero-default check (brief section 6, item 3 of the 2026-11-01 control build): while a unit
  is off-line, 0x76 reports its STORED default limit (measured on the R4875G5: 210 before
  B3.0, 0 after). The first complete burst a Module ever sees arrives before this process
  has written anything (quiet start, and the commander writes nothing until the verdict), so
  `default_ok` = (that 0x76 == 0) is a per-serial, per-process verdict: False means the unit
  is not commissioned and is never commanded. A stored default can only change through a
  0x04 write, which this process never sends, so the verdict holds for the process lifetime.
- Acknowledgement: every set frame must be acked within ACK_TIMEOUT_S, with byte 0 != 0x21.
  A timeout or an error ack is a fault (the unit is driven to zero and held faulted).
- Verification by measurement: SETTLE_S after an acked non-zero limit, a measured current
  above the limit by more than OVERSHOOT_A or OVERSHOOT_FRAC is a fault ("overshoot"): a wrong
  scale shows up here on the first ampere. Below the limit is normal (the battery decides).

States:
    discovering   address seen, serial not yet known
    ready         identified, telemetry fresh, AC present, output not disabled by the unit
    standby       identified and alive, but the unit reports output disabled or AC absent
    inhibited     identified and alive, driver holds it at zero (set by control.py)
    degraded      unsolicited frames alive, but no complete status burst within the window
    faulted       ack error, uncommanded output, or fault flags
    disconnected  nothing from this unit for DISCONNECT_S
"""
import logging

from . import protocol as P

log = logging.getLogger('huawei.modules')

DISCOVERING = 'discovering'
READY = 'ready'
STANDBY = 'standby'
INHIBITED = 'inhibited'
DEGRADED = 'degraded'
FAULTED = 'faulted'
DISCONNECTED = 'disconnected'

STATUS_STALE_S = 5.0          # a complete status burst older than this is stale (poll is 1 s)
UNSOL_STALE_S = 3.0           # unsolicited frames come every ~377 ms
DISCONNECT_S = 10.0
UNCOMMANDED_A = 1.0           # measured current above this while inhibited = fault
FAULT_HOLD_S = 30.0           # a fault is held at least this long after the cause clears
ACK_TIMEOUT_S = 1.0           # harness measured replies within 10 ms; 1 s is generous
SETTLE_S = 3.0                # after an acked limit, give the loop this long before judging
DOWN_SETTLE_S = 30.0          # ... and this long after a LOWER limit (R4875G5 winds down over ~20 s, 2026-11-03)
OVERSHOOT_A = 2.0             # measured above the sent limit by more than this ...
OVERSHOOT_FRAC = 0.10         # ... or by more than 10 % of it = fault


class Setpoint(object):
    """One controllable quantity, keeping the four views apart."""

    def __init__(self, name):
        self.name = name
        self.requested = None       # what control wants (engineering units)
        self.requested_t = None
        self.sent_raw = None        # what was actually transmitted
        self.sent_value = None
        self.sent_t = None
        self.ack_ok = None          # last acknowledgement result
        self.ack_t = None
        self.down_t = None          # time of the last write that LOWERED the value (slow wind-down)
        self.measured = None        # what the unit reports (e.g. 0x76 limit ratio)
        self.measured_t = None

    def as_dict(self):
        return dict((k, getattr(self, k)) for k in
                    ('requested', 'requested_t', 'sent_value', 'sent_raw', 'sent_t',
                     'ack_ok', 'ack_t', 'measured', 'measured_t'))


class Module(object):
    def __init__(self, serial, clock):
        self.serial = serial
        self.clock = clock
        self.addr = None
        self.prev_addr = None       # last address before it was taken by another unit
        self.addr_t = None
        self.nominal_a = None       # from info 0x0001; needed by the ratio1024 scale
        self.scale = None           # current-scale policy name, bound per serial by config
        self.versions = {}
        self.barcode = ''
        self.slot_id = None
        self.model = None           # from the e-label (Model= / BoardType=), e.g. 'R4875G5'
        self.elabel = P.ELabel()
        self.rated_a = None         # GET 0x88 bytes 2-3 / 100 (69.0 on unit 1)
        self.limit_ratio = None     # 0x76 as reported; a readback only on LIMIT_READBACK_MODELS
        self.default_ratio = None   # 0x76 of the first burst ever (unit off-line) = stored default
        self.default_ok = None      # True only if that was 0 (commissioned); None = not seen yet
        self.v85 = None             # last GET 0x85 output voltage
        self.v85_t = None
        self.first_seen = clock()
        self.last_frame_t = None
        self.last_unsol_t = None
        self.last_status_t = None
        self.status = {}            # last complete snapshot (protocol.StatusBurst.snapshot)
        self.unsol = {}             # last unsolicited decode
        self.current = Setpoint('current')
        self.voltage = Setpoint('voltage')
        self.output = Setpoint('output')       # standby register 0x32 writes (enable sequence)
        self.inhibit = True         # control.py clears this only when it may command output
        self.able = False           # control.py: everything on our side allows output (DVCC budget aside)
        self.external_master = False  # engine: bench window open, another master writes to the bus
        self.excluded = False       # config "excluded": telemetry only, never commanded (unit 1, 2026-11-04)
        self.inhibit_reason = 'startup'
        self.state = DISCOVERING
        self.state_since = clock()
        self.fault = None
        self.fault_t = None
        self.counters = {'status': 0, 'unsol': 0, 'ack': 0, 'ack_err': 0, 'ack_foreign': 0, 'ack_timeout': 0,
                         'overshoot': 0, 'addr_changes': 0}
        self.events = []

    # ------------------------------------------------------------ feeding
    def feed_status(self, snap):
        self.counters['status'] += 1
        self.status = snap
        self.last_status_t = snap.get('ts', self.clock())
        self.last_frame_t = self.last_status_t
        if 'current_limit_ratio' in snap:
            self.limit_ratio = snap['current_limit_ratio']
            self.current.measured_t = self.last_status_t
            self.current.measured = self._limit_from_ratio(snap)
            if self.default_ok is None and self.current.sent_t is None and self.voltage.sent_t is None:
                self.default_ratio = self.limit_ratio
                self.default_ok = (snap['registers'].get(0x76, 1) == 0)
                self._event(self.last_status_t, 'stored default limit ratio %.4f -> %s' % (
                    self.limit_ratio, 'zero, commissioned' if self.default_ok else 'NOT ZERO: never commanded'))
        if 'out_voltage_v' in snap:
            self.voltage.measured = self.measured_voltage()
            self.voltage.measured_t = self.last_status_t

    def _limit_from_ratio(self, snap):
        """0x76 as a measured limit: only on models known to echo the on-line raw there.
        The R4875G5 does not (210 / 127 / 0 seen for the same on-line limit)."""
        if not self.model or not any(self.model.startswith(x) for x in P.LIMIT_READBACK_MODELS):
            return None
        if self.scale == 'ratio1024' and self.nominal_a:
            return round(snap['current_limit_ratio'] * self.nominal_a, 2)
        if self.scale in ('x20', 'x30'):
            raw = snap['registers'].get(0x76)
            return round(P.CURRENT_SCALES[self.scale].decode(raw), 2) if raw is not None else None
        return None

    def feed_get_reply(self, dec):
        self.last_frame_t = dec.get('ts', self.clock())
        if dec.get('reg') == P.OUT_VOLTAGE_ALT_REG:
            self.v85 = dec.get('value')
            self.v85_t = self.last_frame_t
            self.voltage.measured = self.measured_voltage()
            self.voltage.measured_t = self.last_frame_t
        elif dec.get('name') == 'rated_current_a':
            self.rated_a = dec.get('value')

    def feed_elabel(self, dec):
        self.last_frame_t = dec.get('ts', self.clock())
        if self.elabel.complete:
            self.elabel = P.ELabel()
        self.elabel.feed(dec)
        if self.elabel.complete:
            fields = self.elabel.fields()
            model = fields.get('Model') or fields.get('BoardType')
            if model and model != self.model:
                self.model = model
                self._event(self.last_frame_t, 'model %s (%s)' % (model, fields.get('Description', '')[:40]))

    def feed_unsolicited(self, dec):
        self.counters['unsol'] += 1
        self.unsol = dec
        self.last_unsol_t = dec.get('ts', self.clock())
        self.last_frame_t = self.last_unsol_t

    def feed_info(self, dec):
        self.last_frame_t = dec.get('ts', self.clock())
        name = dec.get('name')
        if name == 'characteristics':
            self.nominal_a = dec['nominal_current_a']
        elif name == 'barcode':
            self.barcode += dec['ascii'].rstrip('\x00')
        elif name == 'versions':
            self.versions = {'hw': dec['hw_version'], 'dcdc_sw': dec['dcdc_sw'], 'pfc_sw': dec['pfc_sw']}
        elif name == 'slot_id':
            self.slot_id = (dec['slot_top'], dec['slot_bottom'])

    def feed_ack(self, dec):
        self.counters['ack'] += 1
        self.last_frame_t = dec.get('ts', self.clock())
        sp = self.current if dec.get('name') in ('online_current', 'default_current') else \
            self.voltage if dec.get('name') in ('online_voltage', 'default_voltage') else \
            self.output if dec.get('name') == 'standby' else None
        # An ack answers a write. Only a write of THIS process (setpoint sent, ack pending) is judged;
        # everything else on the bus (bench tool writes, the error ack of its fan_duty write that
        # faulted unit 2 on 2026-11-03, acks for registers this driver never writes) is counted.
        if sp is None or sp.sent_t is None or sp.ack_ok is not None:
            self.counters['ack_foreign'] += 1
            return
        sp.ack_ok = dec.get('ok')
        sp.ack_t = self.last_frame_t
        if not dec.get('ok'):
            self.counters['ack_err'] += 1
            self.set_fault('ack error on %s (raw %s)' % (dec.get('name'), dec.get('raw')))

    def set_addr(self, addr, t):
        old = self.addr if self.addr is not None else self.prev_addr
        if old is not None and old != addr:
            self.counters['addr_changes'] += 1
            self._event(t, 'addr %d -> %d' % (old, addr))
        self.addr = addr
        self.prev_addr = None
        self.addr_t = t

    def set_fault(self, text):
        """The first cause is kept as the fault text until it clears; later causes while
        faulted are logged and extend the hold."""
        now = self.clock()
        if self.fault is None:
            self._event(now, 'FAULT ' + text)
            self.fault = text
        elif self.fault != text:
            self._event(now, 'while faulted: ' + text)
        self.fault_t = now

    # ------------------------------------------------------------ queries
    def measured_current(self):
        """Fast output current (0x81) if fresh, else None."""
        if self.status_fresh():
            return self.status.get('out_current_a')
        return None

    def measured_voltage(self, now=None):
        """Output voltage: 0x75 from the burst when it carries a value, else the fresh GET
        0x85 reading (R4875G5), else what 0x75 said (0 or None)."""
        now = self.clock() if now is None else now
        if not self.status_fresh(now):
            return None
        v = self.status.get('out_voltage_v')
        if v:
            return v
        if self.v85_t is not None and now - self.v85_t <= STATUS_STALE_S:
            return self.v85
        return v

    def voltage_source(self, now=None):
        now = self.clock() if now is None else now
        if not self.status_fresh(now):
            return None
        if self.status.get('out_voltage_v'):
            return '0x75'
        if self.v85_t is not None and now - self.v85_t <= STATUS_STALE_S:
            return '0x85'
        return '0x75'

    def ac_present(self):
        if self.status_fresh() and 'ac_present' in self.status:
            return self.status['ac_present']
        return None

    def output_on(self):
        """0x83 bit 27 (measured 2026-11-01): the output stage is on. Informational: set by the
        first non-zero on-line limit, stays set under a 0 A limit."""
        if self.status_fresh() and 'status_bits' in self.status:
            return bool(self.status['status_bits'] & P.OUTPUT_ON_BIT)
        return None

    def unsol_byte3(self):
        """Unsolicited byte 3, informational only: the community tables call it "output
        disabled", unit 1 showed 1 with 49.9 V on the terminals."""
        if self.unsol_fresh() and 'output_disabled' in self.unsol:
            return self.unsol['output_disabled']
        return None

    def status_fresh(self, now=None):
        now = self.clock() if now is None else now
        return self.last_status_t is not None and now - self.last_status_t <= STATUS_STALE_S \
            and self.status.get('complete', False)

    def unsol_fresh(self, now=None):
        now = self.clock() if now is None else now
        return self.last_unsol_t is not None and now - self.last_unsol_t <= UNSOL_STALE_S

    def alive(self, now=None):
        now = self.clock() if now is None else now
        return self.last_frame_t is not None and now - self.last_frame_t <= DISCONNECT_S

    def identified(self):
        return not self.serial.startswith('addr')

    # ------------------------------------------------------ state machine
    def evaluate(self, now=None):
        now = self.clock() if now is None else now
        new = self._compute_state(now)
        if new != self.state:
            self._event(now, 'state %s -> %s' % (self.state, new))
            if self.state == DISCONNECTED:
                # whatever was sent before the gap is not known to be in effect any more (the
                # unit fell back or restarted): forget the sent view, the commander starts over;
                # a fault raised by the gap itself (missing acks) is cleared, a real one re-arms
                for sp in (self.current, self.voltage, self.output):
                    sp.sent_raw = sp.sent_value = sp.sent_t = sp.ack_ok = sp.ack_t = None
                if self.fault is not None:
                    self._event(now, 'fault cleared by reconnect: ' + self.fault)
                    self.fault = None
                    self.fault_t = None
                    new = self._compute_state(now)
            self.state = new
            self.state_since = now
        return self.state

    def _compute_state(self, now):
        if not self.alive(now):
            return DISCONNECTED
        if not self.identified():
            return DISCOVERING
        # uncommanded output: measured current while we hold the unit at zero. In control mode
        # the zero must have been sent and had SETTLE_S to take effect; in telemetry-only mode
        # (nothing ever sent) any current is uncommanded at once.
        i = self.measured_current()
        zero_settled = (self.current.sent_t is None or
                        (not self.current.sent_value and now - self.current.sent_t >= DOWN_SETTLE_S))
        if (self.inhibit and zero_settled and not self.external_master
                and i is not None and i > UNCOMMANDED_A):
            self.set_fault('uncommanded output %.1f A while inhibited' % i)
        # acknowledgement timeout on either setpoint
        for sp in (self.current, self.voltage, self.output):
            if sp.sent_t is not None and sp.ack_ok is None and now - sp.sent_t > ACK_TIMEOUT_S:
                sp.ack_ok = False
                self.counters['ack_timeout'] += 1
                self.set_fault('no ack for %s setpoint %s' % (sp.name, sp.sent_value))
        # verification by measurement: more current than the acked limit allows
        sv = self.current.sent_value
        if (self.fault is None and sv is not None and sv > 0 and self.current.ack_ok and i is not None
                and now - self.current.sent_t >= SETTLE_S
                and (self.current.down_t is None or now - self.current.down_t >= DOWN_SETTLE_S)
                and i > sv + max(OVERSHOOT_A, OVERSHOOT_FRAC * sv)):
            self.counters['overshoot'] += 1
            self.set_fault('overshoot: measured %.1f A above the sent limit %.1f A (scale?)' % (i, sv))
        if self.fault is not None:
            if now - self.fault_t < FAULT_HOLD_S:
                return FAULTED
            self._event(now, 'fault cleared: ' + self.fault)
            self.fault = None
        if not self.status_fresh(now):
            return DEGRADED
        if self.ac_present() is False:
            return STANDBY
        if self.inhibit:
            return INHIBITED
        return READY

    def _event(self, t, text):
        self.events.append((t, text))
        log.info('%s: %s', self.serial, text)

    def summary(self):
        return {'serial': self.serial, 'addr': self.addr, 'state': self.state, 'model': self.model,
                'nominal_a': self.nominal_a, 'scale': self.scale, 'fault': self.fault,
                'voltage_source': self.voltage_source(), 'limit_ratio': self.limit_ratio,
                'default_ratio': self.default_ratio, 'default_ok': self.default_ok,
                'inhibit': self.inhibit, 'able': self.able, 'inhibit_reason': self.inhibit_reason,
                'excluded': self.excluded,
                'current': self.current.as_dict(), 'voltage': self.voltage.as_dict(),
                'output': self.output.as_dict(),
                'counters': dict(self.counters)}


class Registry(object):
    """All units seen on the bus. Frames are attributed to a serial through the address
    map; frames from an address whose serial is unknown are parked in a placeholder
    module named 'addr<n>' so the engine knows to send an info request."""

    def __init__(self, clock, scales=None, excluded=None):
        self.clock = clock
        self.modules = {}           # serial -> Module (placeholders: 'addr<n>')
        self.by_addr = {}           # addr -> serial
        self.scales = scales or {}  # serial -> scale policy name (from config)
        self.excluded = set(str(x).upper() for x in (excluded or ()))   # serials never commanded (config)
        self.bursts = {}            # addr -> protocol.StatusBurst in progress
        self.events = []

    # ------------------------------------------------------------ helpers
    def module_for_addr(self, addr):
        serial = self.by_addr.get(addr)
        if serial is not None:
            m = self.modules.get(serial)
            if m is not None and m.alive():
                return m
            # The bound unit has been silent longer than DISCONNECT_S: whatever talks at this
            # address now may be another unit (2026-11-02: unit 2, plugged in alone, took
            # address 1 of the unplugged unit 1 and its bursts were booked under unit 1's
            # serial until its info frame arrived). Drop the binding; frames park in a
            # placeholder until an info frame or the negotiation names the serial.
            del self.by_addr[addr]
            if m is not None and m.addr == addr:
                m.prev_addr = m.addr
                m.addr = None
            self._event('addr %d: bound unit %s silent > %.0f s, binding dropped until re-identified'
                        % (addr, serial, DISCONNECT_S))
            serial = None
        if serial is None:
            serial = 'addr%d' % addr
            if serial not in self.modules:
                m = Module(serial, self.clock)
                m.set_addr(addr, self.clock())
                self.modules[serial] = m
                self._event('new address %d, serial unknown' % addr)
        return self.modules[serial]

    def identified(self):
        return [m for m in self.modules.values() if m.identified()]

    def unidentified_addrs(self):
        return [m.addr for m in self.modules.values() if not m.identified() and m.alive()]

    def known_addrs(self):
        return sorted(set(m.addr for m in self.modules.values() if m.addr is not None and m.alive()))

    def _event(self, text):
        t = self.clock()
        self.events.append((t, text))
        log.info('registry: %s', text)

    # --------------------------------------------------------------- learn
    def learn(self, serial, addr, source):
        serial = serial.upper()
        t = self.clock()
        m = self.modules.get(serial)
        if m is None:
            placeholder = self.modules.pop('addr%d' % addr, None)
            if placeholder is not None and self.by_addr.get(addr) is None:
                # promote the placeholder: keep its telemetry
                placeholder.serial = serial
                m = placeholder
            else:
                m = Module(serial, self.clock)
            m.scale = self.scales.get(serial)
            m.excluded = serial in self.excluded
            self.modules[serial] = m
            self._event('learned serial %s at addr %d (%s)' % (serial, addr, source))
        else:
            if m.addr != addr:
                self._event('serial %s moved addr %s -> %d (%s)' % (serial, m.addr, addr, source))
            if self.modules.pop('addr%d' % addr, None) is not None:
                # frames parked while the address was unbound: the known unit's own record
                # stands, the parked copy is not merged (its burst may have been another unit's)
                self._event('addr %d placeholder discarded, %s re-identified' % (addr, serial))
        m.set_addr(addr, t)
        prev = self.by_addr.get(addr)
        if prev and prev != serial:
            self._event('ADDR-TAKEN addr %d now %s (was %s): commands to %d would reach another unit'
                        % (addr, serial, prev, addr))
            old = self.modules.get(prev)
            if old is not None and old.addr == addr:
                old.prev_addr = old.addr
                old.addr = None
        self.by_addr[addr] = serial
        for a, s in list(self.by_addr.items()):
            if s == serial and a != addr:
                del self.by_addr[a]
        return m

    # ---------------------------------------------------------------- feed
    def feed(self, dec):
        """One decoded Huawei frame. Returns the Module it was attributed to, or None."""
        if dec is None:
            return None
        kind = dec['kind']
        addr = dec['addr']
        if kind == 'addr_negotiation':
            if dec['phase'] == '3002' and addr:
                return self.learn(dec['serial_hex'], addr, 'negotiation')
            self._event('address negotiation phase %s from addr %d serial %s'
                        % (dec['phase'], addr, dec['serial_hex']))
            return None
        if kind == 'info':
            if dec.get('name') == 'serial':
                m = self.learn(dec['serial_hex'], addr, 'info')
            else:
                m = self.module_for_addr(addr)
            m.feed_info(dec)
            return m
        if kind == 'status':
            burst = self.bursts.get(addr)
            if burst is None or burst.complete:
                burst = self.bursts[addr] = P.StatusBurst(addr)
            burst.feed(dec)
            m = self.module_for_addr(addr)
            m.last_frame_t = dec['ts']
            if burst.complete:
                m.feed_status(burst.snapshot())
            return m
        if kind == 'unsolicited':
            if addr == 0:
                return None             # 0x100011FE-style broadcast without address
            m = self.module_for_addr(addr)
            m.feed_unsolicited(dec)
            return m
        if kind == 'ack':
            m = self.module_for_addr(addr)
            m.feed_ack(dec)
            return m
        if kind == 'get_reply':
            m = self.module_for_addr(addr)
            m.feed_get_reply(dec)
            return m
        if kind == 'elabel':
            m = self.module_for_addr(addr)
            m.feed_elabel(dec)
            return m
        return None

    def evaluate(self, now=None):
        now = self.clock() if now is None else now
        for m in self.modules.values():
            m.evaluate(now)
        return dict((s, m.state) for s, m in self.modules.items())
