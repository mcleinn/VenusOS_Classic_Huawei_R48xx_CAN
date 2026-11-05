import os
if not hasattr(os, 'statvfs'):
    class _SV:
        f_bsize = 4096; f_frsize = 4096; f_blocks = 1; f_bfree = 1; f_bavail = 1
    os.statvfs = lambda p: _SV()
import collections, collections.abc
for _n in ('Mapping', 'MutableMapping', 'Sequence', 'Iterable', 'Callable'):
    if not hasattr(collections, _n):
        setattr(collections, _n, getattr(collections.abc, _n))
