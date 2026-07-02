"""Guard: the browser terminal must run exactly the code this suite tests.

``public/py/src`` is a synced copy of ``src`` (see
``scripts/sync_web_assets.py``); the Pyodide runtime on the deployed site
imports those copies. These tests fail the build whenever the copies drift
from the originals, and sanity-check the bundled Fama-French snapshot the
in-browser FF3 model depends on.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
WEB = ROOT / "public" / "py" / "src"
MANIFEST = ROOT / "public" / "py" / "manifest.json"
SNAPSHOT = ROOT / "public" / "data" / "ff_factors.csv"

RESYNC = "run `python scripts/sync_web_assets.py` and commit the result"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_every_src_module_is_synced_byte_identical():
    for src_file in sorted(SRC.glob("*.py")):
        web_file = WEB / src_file.name
        assert web_file.exists(), f"{web_file} missing — {RESYNC}"
        assert _sha(src_file) == _sha(web_file), f"{src_file.name} drifted — {RESYNC}"


def test_no_stale_files_in_web_copy():
    stale = {p.name for p in WEB.glob("*.py")} - {p.name for p in SRC.glob("*.py")}
    assert not stale, f"stale synced files {stale} — {RESYNC}"


def test_manifest_matches_sources():
    manifest = json.loads(MANIFEST.read_text())
    listed = {f["path"]: f["sha256"] for f in manifest["files"]}
    expected = {f"src/{p.name}": _sha(p) for p in SRC.glob("*.py")}
    assert listed == expected, f"manifest out of date — {RESYNC}"


def test_web_bridge_covers_all_ten_models():
    import re

    bridge = (ROOT / "public" / "py" / "web_bridge.py").read_text()
    match = re.search(r"BUILDERS[^}]+}", bridge)
    assert match, "BUILDERS registry not found in web_bridge.py"
    for mnemonic in ("DCF", "GG", "MPT", "VAR", "CAPM", "FF3", "BSM", "CRR", "MC", "HES"):
        assert f'"{mnemonic}"' in match.group(0), f"{mnemonic} missing from BUILDERS"


def test_factor_snapshot_is_parseable_and_recent():
    frame = pd.read_csv(SNAPSHOT, index_col=0)
    assert list(frame.columns) == ["Mkt-RF", "SMB", "HML", "RF"]
    assert len(frame) > 1100                      # 1926 -> present, monthly
    assert frame.index.min() == 192607
    assert frame.index.max() >= 202400            # snapshot not ancient
    assert frame.abs().max().max() < 1.0          # decimals, not percent
