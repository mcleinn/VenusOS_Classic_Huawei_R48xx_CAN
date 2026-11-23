#!/usr/bin/env python3
"""Run the managed-charger tests inside the pinned dbus-systemcalc-py harness of one or both
supported versions.

For each version folder (``../2.103-1`` = Venus OS v2.92, ``../2.257-1`` = Venus OS v3.85) a
throw-away copy of its pristine ``upstream`` tree is assembled with ``managedcharger.py`` and
``patched-dvcc.py`` laid over it, and ``managedcharger_test.py`` (shared by both versions) is run
there, so the checked-in upstream trees stay pristine. On Windows (no dbus-python, no GLib) the
stand-ins in ``win-shim/`` are put on the path; they only satisfy the imports, the harness's own
mock D-Bus (``tests/base.py``) does the work.

    python src/patch/tests/run_tests.py             # both versions
    python src/patch/tests/run_tests.py 2.257-1     # one version
    python src/patch/tests/run_tests.py 2.257-1 --upstream   # also the version's own upstream tests
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PATCH = os.path.dirname(HERE)
VERSIONS = ('2.103-1', '2.257-1')


def run(version, upstream_too=False):
    vdir = os.path.join(PATCH, version)
    work = tempfile.mkdtemp(prefix='huawei-dvcc-tests-%s-' % version)
    try:
        tree = os.path.join(work, 'systemcalc')
        shutil.copytree(os.path.join(vdir, 'upstream'), tree, ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copy(os.path.join(vdir, 'managedcharger.py'), os.path.join(tree, 'delegates', 'managedcharger.py'))
        shutil.copy(os.path.join(vdir, 'patched-dvcc.py'), os.path.join(tree, 'delegates', 'dvcc.py'))
        shutil.copy(os.path.join(HERE, 'managedcharger_test.py'), os.path.join(tree, 'tests', 'managedcharger_test.py'))
        env = dict(os.environ)
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        if os.name == 'nt':
            env['PYTHONPATH'] = os.path.join(HERE, 'win-shim') + os.pathsep + env.get('PYTHONPATH', '')
        targets = ['tests/managedcharger_test.py']
        if upstream_too:
            targets = ['tests']   # the whole upstream suite on top of the patched tree
        try:
            import pytest  # noqa: F401
            cmd = [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider'] + targets
            cwd = tree
        except ImportError:
            cmd = [sys.executable, '-m', 'unittest', '-v', 'managedcharger_test']
            cwd = os.path.join(tree, 'tests')
        print('== %s (%s)' % (version, 'patched tree, upstream suite included' if upstream_too else 'managed-charger tests'))
        return subprocess.call(cmd, cwd=cwd, env=env)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv):
    upstream_too = '--upstream' in argv
    versions = [a for a in argv if a in VERSIONS] or list(VERSIONS)
    rc = 0
    for v in versions:
        rc |= run(v, upstream_too)
    return rc


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
