"""Every eval capture in this repo has to come from the synthetic bed.

A capture holds whatever the tools returned, so against a real export it is
that person's health data. The repo's own guard (`scripts/pre-commit` and the
`no-health-data` CI job) refuses health-shaped files everywhere but
`tests/fixtures/`, and `tests/eval_captures/` is the second exemption it
carries. An exemption by path alone would wave through a capture taken against
a real export, which is the one thing the guard exists to stop, so the
exemption is gated here instead: a committed capture has to name the files its
index was built from, and every one of those files has to be a file committed
under `tests/fixtures/`.

The hash is what makes this a check rather than a label. `run_agent_eval.py`
copies `source_file.sha256` straight out of the index, so a run against a real
export records hashes that match nothing in the tree and fails this test no
matter what the capture is called or where it sits.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CAPTURES = ROOT / "tests" / "eval_captures"

# Captures written before the stamp existed. Their beds are gone, so the hashes
# cannot be recovered after the fact, and inventing them would turn a check
# into a label. The set is frozen: a capture without a stamp fails unless
# somebody deliberately edits this line, which is the review the exemption
# needs. Run 15 was built by `scripts/build-eval-bed.sh` over the committed
# fixtures, which is recorded in its write-up in tests/eval_results.md.
UNSTAMPED = {"run15.json"}


def _fixture_hashes() -> dict[str, str]:
    """sha256 -> path, for every file committed under tests/fixtures/.

    Read from the index rather than the working tree: a file that is only
    sitting on disk is not something a reviewer of this repo can see.
    """
    listed = subprocess.run(["git", "-C", str(ROOT), "ls-files",
                             "tests/fixtures"],
                            capture_output=True, text=True, check=True)
    hashes: dict[str, str] = {}
    for line in listed.stdout.splitlines():
        path = ROOT / line
        if not path.is_file():
            continue
        hashes[hashlib.sha256(path.read_bytes()).hexdigest()] = line
    return hashes


def _captures() -> list[Path]:
    return sorted(CAPTURES.glob("run*.json"))


def test_the_unstamped_list_does_not_grow():
    """The one exemption inside the exemption, named and fixed.

    Without this, a later capture could be added to UNSTAMPED in the same
    commit that adds the capture, and the stamp would never be checked again.
    """
    assert UNSTAMPED == {"run15.json"}
    assert all((CAPTURES / name).exists() for name in UNSTAMPED), (
        "UNSTAMPED names a capture that is not in the repo")


def test_there_is_something_to_check():
    """Guards the glob. A test that passes over an empty folder measures
    nothing, and this folder is the whole reason the CI exemption exists."""
    assert _captures(), "no captures found, so nothing was checked"


@pytest.mark.parametrize("capture", _captures(), ids=lambda p: p.name)
def test_every_committed_capture_came_from_the_committed_fixtures(capture):
    run = json.loads(capture.read_text()).get("run") or {}
    sources = run.get("sources")

    if capture.name in UNSTAMPED:
        assert not sources, (
            f"{capture.name} carries a stamp now, so take it out of UNSTAMPED "
            f"and let it be checked")
        return

    assert sources, (
        f"{capture.name} does not say what its index was built from. A capture "
        f"in this repo has to carry the source stamp "
        f"`run_agent_eval.py --capture` writes, so it can be shown to come "
        f"from the fixtures rather than from somebody's own export.")

    fixtures = _fixture_hashes()
    strangers = [source for source in sources
                 if source.get("sha256") not in fixtures]
    assert not strangers, (
        f"{capture.name} was built from files that are not committed under "
        f"tests/fixtures/: "
        f"{[Path(str(s.get('path'))).name for s in strangers]}. If this came "
        f"from a real export it is health data and must not be in this repo.")


def test_the_stamp_reader_reports_what_the_index_says(tmp_path):
    """The stamp is only as good as the read behind it.

    An index whose source_file table cannot be read has to come back empty,
    which the test above treats as unstamped, rather than coming back as a
    clean list of nothing.
    """
    import sqlite3
    import sys
    sys.path.insert(0, str(ROOT / "tests"))
    import run_agent_eval

    conn = sqlite3.connect(tmp_path / "empty.db")
    assert run_agent_eval._sources(conn) == []
    conn.execute("CREATE TABLE source_file(path TEXT, kind TEXT, sha256 TEXT)")
    conn.execute("INSERT INTO source_file VALUES('/b.xml', 'healthkit', 'bb')")
    conn.execute("INSERT INTO source_file VALUES('/a.pdf', 'pdf', 'aa')")
    assert run_agent_eval._sources(conn) == [
        {"path": "/a.pdf", "kind": "pdf", "sha256": "aa"},
        {"path": "/b.xml", "kind": "healthkit", "sha256": "bb"},
    ]
    conn.close()
