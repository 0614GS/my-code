"""Run the canonical Tach architecture check inside the pytest suite."""

import subprocess
import sys

from .dependency_rules import REPOSITORY_ROOT


def test_tach_module_boundaries() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "tach", "check"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
