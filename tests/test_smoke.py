"""Smoke test proving the pytest scaffolding can import the package and hit SQLite."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from paperbase.core.db import Database

from tests.conftest import make_paper


@pytest.mark.skipif(importlib.util.find_spec("torch") is None, reason="torch not installed")
def test_torch_imports_after_pyqt6() -> None:
    """The application imports PyQt6 long before the categoriser imports torch. Without
    paperbase/__init__.py's runtime preload, PyQt6's bundled msvcp140.dll makes torch's
    c10.dll fail its initialisation. A fresh interpreter, since this one may already
    hold either library."""
    code = "import paperbase; import PyQt6.QtCore; import torch"
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert result.returncode == 0, result.stderr[-2000:]


def test_insert_and_round_trip(db: Database) -> None:
    paper_one = make_paper(title="First paper")
    paper_two = make_paper(title="Second paper")

    pid = db.insert_paper(paper_one)
    db.insert_paper(paper_two)

    assert db.get_paper_count() == 2
    fetched = db.get_paper(pid)
    assert fetched is not None
    assert fetched.title == "First paper"
