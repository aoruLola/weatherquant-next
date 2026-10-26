import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("rss, hwm, psi", [(8000, 8000, 0), (100, 8000, 0), (100, 100, 90)])
def test_backup_resource_limit_cannot_pass_with_a_clean_collection(tmp_path, monkeypatch, rss, hwm, psi):
    source = Path(__file__).resolve().parents[1] / "scripts/soak_memory.py"
    spec = importlib.util.spec_from_file_location("soak_memory_gate", source)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    collection = module.PhaseReport("collection", samples=[module.Sample(1, 100, 100, 0, 0, "healthy", 1, 1)])
    backup = module.PhaseReport("backup-cycle", samples=[module.Sample(1, rss, hwm, psi, psi, "writing", 1, 1)])
    monkeypatch.setattr(module, "read_heartbeat", lambda path: {})
    monkeypatch.setattr(
        "weatherquant.data.heartbeat.evaluate_health",
        lambda path: SimpleNamespace(status=SimpleNamespace(value="stopped"), exit_code=0),
    )
    assert (
        module._report(
            collection,
            backup,
            {"verified": True, "hwm_mb": hwm},
            tmp_path,
            {"max_rss_mb": 3072, "max_slope_mb_per_min": 8, "max_psi_full": 2},
        )
        == 1
    )
