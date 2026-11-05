class BusConnection:
    TYPE_SYSTEM = 0; TYPE_SESSION = 1
    def __new__(cls, *a, **k): return object.__new__(cls)
