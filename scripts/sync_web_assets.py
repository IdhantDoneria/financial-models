"""Sync the Python model sources into the static site for in-browser execution.

The deployed terminal (``public/index.html``) runs the *actual* model files via
Pyodide. To guarantee the browser executes exactly the code the test suite
validates, this script copies ``src/*.py`` into ``public/py/src/`` and writes a
manifest (paths + SHA-256) that the front end uses to load them and that
``tests/test_web_assets.py`` uses to detect drift.

It also snapshots the Ken French factor history to ``public/data/`` so the
Fama-French model resolves its data offline in the browser (the loader's
designed cache-fallback path).

Usage::

    python scripts/sync_web_assets.py            # sync + refresh data snapshot
    python scripts/sync_web_assets.py --no-data  # sync sources only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DEST = ROOT / "public" / "py" / "src"
DATA_DEST = ROOT / "public" / "data" / "ff_factors.csv"
MANIFEST = ROOT / "public" / "py" / "manifest.json"

#: Source files shipped to the browser: every top-level model module plus the
#: PDF-analyzer pipeline package (whose PDF/export backends import lazily, so
#: the WASM runtime installs only the pure-Python ones it can support).
INCLUDE = sorted(
    p.relative_to(SRC).as_posix()
    for p in list(SRC.glob("*.py")) + list(SRC.glob("pipeline/*.py"))
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sync_sources() -> dict:
    """Copy the included sources to ``public/py/src`` and return the manifest."""
    DEST.mkdir(parents=True, exist_ok=True)
    # Remove stale copies so deletions in src/ propagate.
    for old in DEST.rglob("*.py"):
        if old.relative_to(DEST).as_posix() not in INCLUDE:
            old.unlink()
    files = []
    for name in INCLUDE:
        (DEST / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SRC / name, DEST / name)
        files.append({"path": f"src/{name}", "sha256": _sha256(SRC / name)})
    manifest = {
        "synced_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "files": files,
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def snapshot_factors() -> bool:
    """Write the Fama-French monthly factor snapshot; True on success."""
    sys.path.insert(0, str(ROOT))
    from src.fama_french import FamaFrenchModel

    try:
        factors = FamaFrenchModel.load_factors()
    except Exception as exc:  # network + cache both unavailable
        print(f"!! factor snapshot skipped: {exc}")
        return False
    DATA_DEST.parent.mkdir(parents=True, exist_ok=True)
    factors.to_csv(DATA_DEST)
    print(f"ok factor snapshot: {len(factors)} rows -> {DATA_DEST.relative_to(ROOT)}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-data", action="store_true",
                        help="skip the Fama-French data snapshot")
    args = parser.parse_args()

    manifest = sync_sources()
    print(f"ok synced {len(manifest['files'])} files -> {DEST.relative_to(ROOT)}")
    if not args.no_data:
        if not snapshot_factors() and not DATA_DEST.exists():
            print("!! no existing snapshot present either — FF3 demo needs one")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
