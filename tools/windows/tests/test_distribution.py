import importlib.util
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("windows_distribution", ROOT / "tools/windows/distribution.py")
distribution = importlib.util.module_from_spec(spec)
spec.loader.exec_module(distribution)


def test_customer_boundary_excludes_current_and_future_research_documents():
    names = list(distribution.FILES) + [
        "00-总设计文档.md", "README.md", "docs/Windows部署与数据迁移.md",
        "arbitrage-research/docs/new-private-research.md",
        "arbitrage-live/design-system/console/MASTER.md",
        "arbitrage-research/scripts/analysis/new_strategy.py",
        "arbitrage-research/configs/research/new_experiment.json",
        "tools/windows/build.py", "tools/windows/requirements-test.txt",
        "arbitrage-research/src/arb_research/new-design.md",
        "arbitrage-research/src/arb_research/docs/design.py",
        "arbitrage-research/src/arb_research/tests/test_new.py",
        "arbitrage-research/src/arb_research/live/dashboard.py",
        "arbitrage-research/src/arb_research/live/static/monitor.js",
    ]
    selected = dict(distribution.select_files(names))
    assert set(selected) == set(distribution.FILES) | {
        "arbitrage-research/src/arb_research/live/dashboard.py",
        "arbitrage-research/src/arb_research/live/static/monitor.js",
    }
    assert [target for target in selected.values() if target.endswith(".md")] == ["使用说明.md"]


def test_missing_runtime_entry_point_aborts():
    names = list(distribution.FILES)
    names.remove("arbitrage-research/scripts/monitor/run_gate_low_frequency_monitor.mjs")
    with pytest.raises(ValueError, match="Missing required"):
        distribution.select_files(names)


@pytest.mark.parametrize("name", ["../escape.py", "/absolute.py", "tools\\escape.py",
                                  "arbitrage-live/.env", "secret.key", "data/history.json"])
def test_unsafe_index_entries_are_rejected_even_outside_allowlist(name):
    with pytest.raises(ValueError):
        distribution.select_files(list(distribution.FILES) + [name])


def test_application_copy_from_actual_repository_preserves_sources(tmp_path):
    # Include newly added tracked files during local pre-commit validation too.
    names = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    names = sorted(set(filter(None, names)) | set(distribution.FILES))
    original = (ROOT / "00-总设计文档.md").read_bytes()
    app = tmp_path / "customer"
    report = distribution.copy_application(ROOT, app, names)
    actual = {path.relative_to(app).as_posix() for path in app.rglob("*") if path.is_file()}
    assert actual == set(report["files"])
    assert report["keptFiles"] == len(actual)
    assert report["omittedFiles"] > 200
    assert {name for name in actual if name.endswith(".md")} == {"使用说明.md"}
    assert (app / "使用说明.md").read_bytes() == (ROOT / "tools/windows/customer-guide.md").read_bytes()
    assert (ROOT / "00-总设计文档.md").read_bytes() == original
    for page in ("arbitrage-realtime/web/index.html",
                 "arbitrage-live/src/live_trader/static/index.html",
                 "arbitrage-research/src/arb_research/live/static/index.html"):
        assert b"<html" in (app / page).read_bytes().lower()
    # Every Python package and static resource is available at the same path.
    for source, target in distribution.select_files(names):
        assert (app / target).read_bytes() == (ROOT / source).read_bytes()


def test_link_source_aborts_before_copying(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for name in distribution.FILES:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("example", encoding="utf-8")
    link = source / "arbitrage-research/src/arb_research/linked.py"
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(source / "开启.cmd")
    except OSError:
        pytest.skip("Symlink privilege not available")
    with pytest.raises(ValueError, match="source link"):
        distribution.copy_application(source, tmp_path / "target", list(distribution.FILES) + [link.relative_to(source).as_posix()])
    assert not (tmp_path / "target").exists()
