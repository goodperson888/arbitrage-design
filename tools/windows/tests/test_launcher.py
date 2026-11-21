import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("windows_manager", ROOT / "tools/windows/manager.py")
manager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manager)


def test_external_data_survives_configuration_and_commands(tmp_path, monkeypatch):
    config = tmp_path / "launcher.json"
    root = tmp_path / "独立数据"
    monkeypatch.setenv("ARB_SETTINGS_FILE", str(config))
    config.write_text(json.dumps({"data_root": str(root), "proxy": "http://127.0.0.1:7890"}), encoding="utf-8")
    loaded = manager.load_settings()
    assert Path(loaded["data_root"]) == root.resolve()
    ledger = root / "accounts.ndjson"
    ledger.write_bytes(b"existing account")
    paper, cwd = manager.command_for("paper", root, loaded["proxy"], root / ".control/paper.stop")
    assert str(root / "live_monitor") in paper
    assert "--data-root" in paper
    assert paper[paper.index("--data-root") + 1] == str(root)
    for service in ("cross", "cross15", "live", "realtime"):
        command, _ = manager.command_for(service, root, loaded["proxy"], root / f"{service}.stop")
        assert "--stop-file" in command
        assert not any(str(ROOT / "data") in arg for arg in command)
    assert ledger.read_bytes() == b"existing account"


@pytest.mark.parametrize("bad", [str(ROOT / "data"), str(Path(ROOT.anchor)), "relative/data"])
def test_rejects_data_inside_app_or_ambiguous_root(tmp_path, monkeypatch, bad):
    config = tmp_path / "launcher.json"
    config.write_text(json.dumps({"data_root": bad}), encoding="utf-8")
    monkeypatch.setenv("ARB_SETTINGS_FILE", str(config))
    with pytest.raises(ValueError):
        manager.load_settings()


def test_duplicate_service_detection_uses_lock_not_pid_file(tmp_path):
    control = manager.control_dir(tmp_path)
    # A stale PID, including a reused system PID, is not proof of a running service.
    manager.write_json(control / "paper.json", {"pid": 1, "status": "running"})
    assert not manager.is_running(tmp_path, "paper")
    with manager.exclusive_lock(control / "paper.lock"):
        assert manager.is_running(tmp_path, "paper")
    assert not manager.is_running(tmp_path, "paper")


def test_stop_only_creates_request_never_signals_foreign_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(manager, "is_running", lambda *_: True)
    monkeypatch.setattr(manager.os, "kill", lambda *_: pytest.fail("must not signal a PID"))
    assert not manager.stop_one(tmp_path, "paper", timeout=0)
    assert (tmp_path / ".control/paper.stop").exists()


def test_data_file_prefix_and_traversal_are_not_bundle_entries(tmp_path):
    spec = importlib.util.spec_from_file_location("windows_build", ROOT / "tools/windows/build.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    import zipfile
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr("../account.txt", "must not escape")
    with pytest.raises(ValueError):
        builder.extract(archive, tmp_path / "runtime")
    assert not (tmp_path / "account.txt").exists()
