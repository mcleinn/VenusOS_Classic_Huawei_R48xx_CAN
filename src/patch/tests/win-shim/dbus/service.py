class BusName:
    def __init__(self, name, bus=None, *a, **k): self._name = name
    def get_name(self): return self._name
class Object:
    def __init__(self, *a, **k): pass
    def remove_from_connection(self): pass
def method(*a, **k):
    return lambda f: f
def signal(*a, **k):
    return lambda f: f
