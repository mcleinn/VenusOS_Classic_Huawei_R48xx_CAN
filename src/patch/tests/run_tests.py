#!/usr/bin/env python3
"""Run the managed-charger tests inside the pinned dbus-systemcalc-py 2.103-1 harness.

Assembles a throw-away copy of ``upstream-2.103-1`` with ``managedcharger.py`` and
``patched-dvcc.py`` laid over it and runs ``managedcharger_test.py`` there, so the
checked-in upstream tree stays pristine. On Windows (no dbus-python, no GLib) the
stand-ins in ``win-shim/`` are put on the path; they only satisfy the imports, the
harness's own mock D-Bus (``tests/base.py``) does the work.

    python src/patch/tests/run_tests.py            # pytest if installed, else unittest
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PATCH = os.path.dirname(HERE)
UPSTREAM = os.path.join(PATCH, 'upstream-2.103-1')


def main():
    work = tempfile.mkdtemp(prefix='huawei-dvcc-tests-')
    try:
        tree = os.path.join(work, 'systemcalc')
        shutil.copytree(UPSTREAM, tree, ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copy(os.path.join(PATCH, 'managedcharger.py'), os.path.join(tree, 'delegates', 'managedcharger.py'))
        shutil.copy(os.path.join(PATCH, 'patched-dvcc.py'), os.path.join(tree, 'delegates', 'dvcc.py'))
        shutil.copy(os.path.join(HERE, 'managedcharger_test.py'), os.path.join(tree, 'tests', 'managedcharger_test.py'))
        env = dict(os.environ)
        if os.name == 'nt':
            env['PYTHONPATH'] = os.path.join(HERE, 'win-shim') + os.pathsep + env.get('PYTHONPATH', '')
        try:
            import pytest  # noqa: F401
            cmd = [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', 'tests/managedcharger_test.py']
        except ImportError:
            cmd = [sys.executable, '-m', 'unittest', '-v', 'managedcharger_test']
            tree = os.path.join(tree, 'tests')
        return subprocess.call(cmd, cwd=tree, env=env)
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
