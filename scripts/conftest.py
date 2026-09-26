"""conftest.py — suite-wide runtime-dir isolation (TASK-FIX-008).

Every test gets fresh baselines/decisions/context dirs under pytest tmp,
so no test run can pollute the real .agent/ tree. Tests that set their
own tmp paths keep working (they overwrite after this fixture).
Uses monkeypatch (auto-undo) + stdlib only.
"""
import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_runtime_dirs(monkeypatch, tmp_path):
    import manager as mgr_mod
    for attr, sub in (("BASELINE_DIR", "baselines"),
                      ("DECISIONS_DIR", "decisions"),
                      ("CONTEXT_DIR", "context")):
        if hasattr(mgr_mod, attr):
            monkeypatch.setattr(mgr_mod, attr,
                                os.path.join(str(tmp_path), sub))
    yield
