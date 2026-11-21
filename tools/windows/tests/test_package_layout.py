import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("package_layout", Path(__file__).resolve().parents[1] / "package_layout.py")
layout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(layout)


def test_slim_copy_preserves_runtime_data_native_libraries_and_licenses(tmp_path):
    source, target = tmp_path / "installed", tmp_path / "runtime"
    keep = ["pyarrow/lib.pyd", "pyarrow/arrow.dll", "tzdata/zoneinfo/Asia/Shanghai",
            "package/data/lookup.json", "package/examples.py", "package.pyi",
            "package-1.dist-info/licenses/LICENSE", "package-1.dist-info/RECORD"]
    omit = ["pyarrow/tests/data/file.arrow", "pyarrow/include/arrow/api.h",
            "pyarrow/src/arrow/python.cc", "package/tests/test_api.py"]
    for name in keep + omit:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())
    report = layout.copy_runtime_packages(source, target)
    assert report["keptFiles"] == len(keep)
    assert report["omittedFiles"] == len(omit)
    for name in keep:
        assert (target / name).read_bytes() == name.encode()
    for name in omit:
        assert not (target / name).exists()
        assert (source / name).is_file()


def test_copy_preserves_bootstrap_pip_and_rejects_overwrites(tmp_path):
    source, target = tmp_path / "installed", tmp_path / "runtime"
    source.mkdir()
    target.mkdir()
    (source / "library.py").write_text("library")
    (target / "pip.py").write_text("installer")
    layout.copy_runtime_packages(source, target)
    assert (target / "pip.py").read_text() == "installer"
    with pytest.raises(ValueError, match="overwrite"):
        layout.copy_runtime_packages(source, target)
    assert (target / "library.py").read_text() == "library"


def test_copy_rejects_links_before_creating_destination(tmp_path):
    source = tmp_path / "installed"
    source.mkdir()
    (source / "original").write_text("original")
    try:
        (source / "link").symlink_to(source / "original")
    except OSError:
        pytest.skip("Symlink privilege not available")
    with pytest.raises(ValueError, match="links"):
        layout.copy_runtime_packages(source, tmp_path / "runtime")
    assert not (tmp_path / "runtime").exists()
