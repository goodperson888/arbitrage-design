"""Copy a collector runtime from a fresh pip target, leaving the source intact."""
from pathlib import Path
import shutil
import stat


def include_dependency_file(relative: Path) -> bool:
    parts = relative.parts
    # Distribution metadata includes licenses and integrity/provenance records.
    if parts[0].endswith((".dist-info", ".egg-info")):
        return True
    if any(part in {"tests", "__pycache__"} for part in parts[:-1]):
        return False
    # Arrow headers and Cython sources are for compiling extensions, not running
    # the supplied wheels. Keep runtime data, .pyd/.dll files and Python modules.
    if parts[0] == "pyarrow" and len(parts) > 2 and parts[1] in {"include", "src"}:
        return False
    return True


def copy_runtime_packages(source: Path, destination: Path) -> dict:
    source = source.resolve(strict=True)
    destination = destination.resolve()
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError("Runtime package source and destination must be separate")
    paths = sorted(source.rglob("*"))
    # Reject links/junctions before copying anything from the fresh pip target.
    for path in paths:
        info = path.lstat()
        if path.is_symlink() or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise ValueError(f"Runtime package links are not supported: {path.name}")
        if path.is_file() and include_dependency_file(path.relative_to(source)):
            target = destination / path.relative_to(source)
            if target.exists() or target.is_symlink():
                raise ValueError(f"Refusing to overwrite a runtime file: {target.name}")
    # Automatic setup already has pip here. Merge disjoint files, never replace.
    destination.mkdir(parents=True, exist_ok=True)
    report = {"profile": "collector", "keptFiles": 0, "keptBytes": 0,
              "omittedFiles": 0, "omittedBytes": 0}
    for path in paths:
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        keep = include_dependency_file(relative)
        prefix = "kept" if keep else "omitted"
        report[prefix + "Files"] += 1
        report[prefix + "Bytes"] += path.stat().st_size
        if keep:
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    return report
