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


def test_browser_and_status_use_recorded_actual_port(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    settings = tmp_path / "settings.json"
    monkeypatch.setenv("ARB_SETTINGS_FILE", str(settings))
    manager.write_json(settings, {"data_root": str(data), "proxy": ""})
    manager.write_json(manager.control_dir(data) / "paper.json", {"port": 49321})
    opened = []
    monkeypatch.setattr(manager.webbrowser, "open", opened.append)
    monkeypatch.setattr(sys, "argv", ["manager", "open", "paper"])
    assert manager.main() == 0
    assert opened == ["http://127.0.0.1:49321/"]
    monkeypatch.setattr(manager, "is_running", lambda _, name: name == "paper")
    manager.status(manager.load_settings())
    assert "http://127.0.0.1:49321/" in capsys.readouterr().out


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


@pytest.mark.parametrize(("value", "expected"), [
    ("7890", "http://127.0.0.1:7890"), ("10808", "http://127.0.0.1:10808"),
    ("0", ""), ("none", ""), ("", ""), ("socks5://127.0.0.1:1080", "socks5://127.0.0.1:1080"),
])
def test_proxy_port_or_explicit_direct(value, expected):
    assert manager.parse_proxy(value) == expected


@pytest.mark.parametrize("value", ["65536", "-1", "abc", "http://localhost", "http://localhost:0",
                                       "http://localhost:7890/api", "http://localhost:7890?q=1"])
def test_invalid_proxy_never_saved(value):
    with pytest.raises(ValueError):
        manager.parse_proxy(value)


def test_first_run_saves_external_data_and_port_once(tmp_path, monkeypatch):
    config = tmp_path / "launcher.json"
    monkeypatch.setenv("ARB_SETTINGS_FILE", str(config))
    monkeypatch.delenv("ARB_SETUP_PROXY_SET", raising=False)
    answers = iter([str(tmp_path / "数据"), "wrong", "10808"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    manager.configure()
    saved = manager.load_settings()
    assert saved["proxy"] == "http://127.0.0.1:10808"
    assert Path(saved["data_root"]) == tmp_path / "数据"
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("saved configuration must not prompt"))
    assert manager.load_settings() == saved


def test_setup_direct_choice_is_reused_without_second_proxy_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("ARB_SETTINGS_FILE", str(tmp_path / "launcher.json"))
    monkeypatch.setenv("ARB_SETUP_PROXY_SET", "1")
    monkeypatch.delenv("ARB_SETUP_PROXY", raising=False)
    prompts = []
    monkeypatch.setattr("builtins.input", lambda question: prompts.append(question) or "")
    manager.configure()
    assert len(prompts) == 1
    assert manager.load_settings()["proxy"] == ""


def test_closed_proxy_can_be_corrected_and_persisted(tmp_path, monkeypatch):
    monkeypatch.setenv("ARB_SETTINGS_FILE", str(tmp_path / "launcher.json"))
    config = {"data_root": str(tmp_path / "data"), "proxy": "http://127.0.0.1:7890"}
    def refused(*args, **kwargs):
        raise ConnectionRefusedError()
    monkeypatch.setattr(manager.socket, "create_connection", refused)
    monkeypatch.setattr("builtins.input", lambda _: "0")
    assert manager.check_local_proxy(config)["proxy"] == ""
    assert manager.load_settings()["proxy"] == ""


def test_proxy_change_never_mixes_settings_with_running_worker(tmp_path, monkeypatch):
    config = {"data_root": str(tmp_path), "proxy": "http://127.0.0.1:7890"}
    def refused(*args, **kwargs):
        raise ConnectionRefusedError()
    monkeypatch.setattr(manager.socket, "create_connection", refused)
    monkeypatch.setattr(manager, "is_running", lambda *_: True)
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("cannot reconfigure active services"))
    with pytest.raises(ValueError, match="停止"):
        manager.check_local_proxy(config)


def test_invalid_reconfiguration_preserves_previous_file(tmp_path, monkeypatch):
    config = tmp_path / "launcher.json"
    monkeypatch.setenv("ARB_SETTINGS_FILE", str(config))
    monkeypatch.delenv("ARB_SETUP_PROXY_SET", raising=False)
    manager.write_json(config, {"data_root": str(tmp_path / "data"), "proxy": ""})
    before = config.read_bytes()
    answers = iter(["relative", "0"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    with pytest.raises(ValueError):
        manager.configure()
    assert config.read_bytes() == before


def test_stop_before_first_run_does_not_prompt_or_create_settings(tmp_path, monkeypatch):
    path = tmp_path / "launcher.json"
    monkeypatch.setenv("ARB_SETTINGS_FILE", str(path))
    monkeypatch.setattr(sys, "argv", ["manager", "stop", "all"])
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("stop must never configure"))
    assert manager.main() == 0
    assert not path.exists()


def test_stop_waits_for_process_exit_after_writer_releases_lock(tmp_path, monkeypatch):
    from contextlib import contextmanager
    polls = []
    @contextmanager
    def witness(*args):
        def exited():
            polls.append(True)
            return len(polls) >= 3
        yield exited
    monkeypatch.setattr(manager, "worker_exit_witness", witness)
    monkeypatch.setattr(manager, "is_running", lambda *_: False)
    monkeypatch.setattr(manager.time, "sleep", lambda _: None)
    assert manager.stop_one(tmp_path, "paper")
    assert len(polls) == 3


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process handle identity")
def test_windows_witness_waits_for_exact_process_and_rejects_wrong_birth():
    import subprocess
    import os
    with manager.windows_process_witness(os.getpid()) as (birth, exited):
        assert birth is not None
        assert not exited()
    with manager.windows_process_witness(os.getpid(), birth + 1) as (matched, exited):
        assert matched is None
        assert exited()  # PID without matching creation time is not our worker.
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.2)"])
    try:
        with manager.windows_process_witness(process.pid) as (child_birth, exited):
            assert child_birth is not None
            process.wait(timeout=10)
            assert exited()
    finally:
        process.wait(timeout=10)
