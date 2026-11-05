# Minimal dbus-python stand-in for running the systemcalc unit tests on Windows (mock D-Bus only).
class DBusException(Exception):
    pass
class _Num:
    def __new__(cls, value=0, variant_level=0):
        return cls._base.__new__(cls, value)
def _mk(name, base):
    return type(name, (base,), {'__new__': lambda cls, value=base(), variant_level=0: base.__new__(cls, value)})
Int16 = _mk('Int16', int); Int32 = _mk('Int32', int); Int64 = _mk('Int64', int)
UInt16 = _mk('UInt16', int); UInt32 = _mk('UInt32', int); UInt64 = _mk('UInt64', int)
Byte = _mk('Byte', int); Double = _mk('Double', float); String = _mk('String', str)
Signature = _mk('Signature', str); ObjectPath = _mk('ObjectPath', str)
class Boolean(int):
    def __new__(cls, value=False, variant_level=0): return int.__new__(cls, bool(value))
class Array(list):
    def __init__(self, seq=(), signature=None, variant_level=0): list.__init__(self, seq)
class Dictionary(dict):
    def __init__(self, d=None, signature=None, variant_level=0): dict.__init__(self, d or {})
class ByteArray(bytes):
    def __new__(cls, value=b'', variant_level=0): return bytes.__new__(cls, value)
class _Bus:
    def __init__(self, *a, **k): pass
    def add_signal_receiver(self, *a, **k): pass
    def get_object(self, *a, **k): raise DBusException('no bus')
    def call_async(self, *a, **k): pass
    def call_blocking(self, *a, **k): raise DBusException('no bus')
def SystemBus(*a, **k): return _Bus()
def SessionBus(*a, **k): return _Bus()
from . import exceptions, service, bus, mainloop
