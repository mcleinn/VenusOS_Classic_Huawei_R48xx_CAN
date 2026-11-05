class GLib:
    @staticmethod
    def timeout_add(ms, cb, *a): return 1
    @staticmethod
    def timeout_add_seconds(s, cb, *a): return 1
    @staticmethod
    def idle_add(cb, *a): return 1
    @staticmethod
    def source_remove(i): return True
    class MainLoop:
        def run(self): pass
        def quit(self): pass
