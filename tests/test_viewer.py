"""Lecteurs de la visionneuse 3D, executes par Node.

Le code de la visionneuse est du JavaScript : pytest se contente de lancer le
harnais Node et de rapporter son verdict, pour que la suite reste unique.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HARNAIS = Path(__file__).parent / "test_viewer.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js absent")
def test_lecteurs_ply_et_obj():
    resultat = subprocess.run(
        ["node", str(HARNAIS)], capture_output=True, text=True, timeout=60
    )
    assert resultat.returncode == 0, resultat.stdout + resultat.stderr
