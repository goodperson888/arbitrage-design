import gzip
import json
from pathlib import Path
import zlib

import pytest

from arb_research.live.candidates.engine import CandidateEngine
from arb_research.live.candidates.observation_repair import recover_rows, repair_observations
from arb_research.live.log_storage import read_partition_rows


def observation(at):
    return {"type": "OBSERVATION", "atUs": at, "fingerprint": "test", "text": "中文"}


def interrupted(raw):
    compressor = zlib.compressobj(wbits=31)
    return compressor.compress(raw) + compressor.flush(zlib.Z_SYNC_FLUSH)


def test_restart_never_appends_behind_interrupted_member_and_live_read_is_locked(tmp_path):
    at = 1_800_000_001_000_000
    first = CandidateEngine(tmp_path, at)
    first._append("OBSERVATION", at, value=1)
    path = next((tmp_path / "observations").glob("*/*.ndjson.gz"))
    assert [row["value"] for row in read_partition_rows(path)] == [1]
    first.close()
    # Simulate the trailer missing after an unclean shutdown.
    damaged = path.read_bytes()[:-8]
    path.write_bytes(damaged)
    second = CandidateEngine(tmp_path, at + 1)
    try:
        second._append("OBSERVATION", at + 1, value=2)
    finally:
        second.close()
    assert path.read_bytes() == damaged
    fresh_paths = [p for p in (tmp_path / "observations").glob("*/*.ndjson.gz") if p != path]
    assert len(fresh_paths) == 1
    assert [row["value"] for row in read_partition_rows(fresh_paths[0])] == [2]


def test_offline_recovery_preserves_complete_rows_originals_and_account_journal(tmp_path):
    directory = tmp_path / "observations/2026-11-21"
    directory.mkdir(parents=True)
    lines = [(json.dumps(observation(at), ensure_ascii=False) + "\n").encode() for at in range(3)]
    original = gzip.compress(lines[0]) + interrupted(lines[1] + b'{"partial":') + gzip.compress(lines[2])
    path = directory / "08.ndjson.gz"
    path.write_bytes(original)
    journal = tmp_path / "accounts.ndjson"
    journal.write_bytes(b"preserve-account-statistics\n")
    preview = repair_observations(tmp_path)
    assert preview[0]["rows"] == 3 and preview[0]["unfinishedMembers"] == 1
    assert preview[0]["partialRowBytes"] == len(b'{"partial":')
    assert path.read_bytes() == original
    result = repair_observations(tmp_path, apply=True)[0]
    assert gzip.decompress(path.read_bytes()) == b"".join(lines)
    assert (tmp_path / result["backup"]).read_bytes() == original
    assert journal.read_bytes() == b"preserve-account-statistics\n"
    assert repair_observations(tmp_path, apply=True) == []


def test_repair_refuses_active_candidate_writer(tmp_path):
    engine = CandidateEngine(tmp_path, 1)
    try:
        with pytest.raises(BlockingIOError):
            repair_observations(tmp_path, apply=True)
    finally:
        engine.close()


@pytest.mark.parametrize("raw", [b'not-json\n', b'{"type":"SETTLED","atUs":1}\n'])
def test_recovery_never_silently_skips_invalid_complete_rows(raw):
    with pytest.raises((ValueError, json.JSONDecodeError)):
        recover_rows(interrupted(raw))


def test_repair_refuses_symbolic_partitions_and_keeps_valid_files(tmp_path):
    directory = tmp_path / "observations/2026-11-21"
    directory.mkdir(parents=True)
    path = directory / "08.ndjson.gz"
    original = gzip.compress(b'{}\n')
    path.write_bytes(original)
    assert repair_observations(tmp_path, apply=True) == []
    assert path.read_bytes() == original
    link = directory / "09.ndjson.gz"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="regular"):
        repair_observations(tmp_path, apply=True)
