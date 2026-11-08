"""Download and install the optional CAD add-on (build123d + OpenCascade) used by bird design.

    python console/fetch_addon.py <data dir>

Reads release/bird/cad_addon.json (url, sha256), downloads to a temporary file, refuses it unless the SHA-256 matches,
then extracts into <data>/addons/cad-<version>/ and writes READY. The console passes that site-packages folder to the
CAD sandbox through RLSOK_CAD_PATH. Nothing outside <data>/addons is touched.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The CDN in front of rlsok.com answers 403 to the default "Python-urllib" agent (found 2026-10-21).
UA = {"User-Agent": "RLSOK/0.1 (+https://rlsok.com)"}


def main(data: Path) -> int:
    meta = json.loads((ROOT / "release" / "bird" / "cad_addon.json").read_text(encoding="utf-8"))
    dest = data / "addons" / f"cad-{meta['version']}"
    if (dest / "READY").exists():
        print("CAD add-on already installed:", dest)
        return 0
    print(f"downloading {meta['url']} ({meta['bytes'] / 1e6:.0f} MB)", flush=True)
    tmp = Path(tempfile.mkdtemp()) / meta["file"]
    h = hashlib.sha256()
    with urllib.request.urlopen(urllib.request.Request(meta["url"], headers=UA), timeout=60) as r, open(tmp, "wb") as f:
        got = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk); h.update(chunk); got += len(chunk)
            if got % (10 << 20) < (1 << 20):
                print(f"  {got / 1e6:.0f} MB", flush=True)
    if h.hexdigest() != meta["sha256"]:
        print(f"SHA256 MISMATCH: got {h.hexdigest()}, expected {meta['sha256']}; file discarded")
        tmp.unlink(missing_ok=True)
        return 2
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    with zipfile.ZipFile(tmp) as z:
        for n in z.namelist():                      # no absolute paths or '..' escapes
            if n.startswith(("/", "\\")) or ".." in Path(n).parts:
                print("unsafe path in add-on:", n)
                return 2
        z.extractall(dest)
    tmp.unlink(missing_ok=True)
    (dest / "READY").write_text(meta["sha256"], encoding="ascii")
    print("CAD add-on installed:", dest)
    return 0


def installed_path(data: Path) -> Path | None:
    try:
        meta = json.loads((ROOT / "release" / "bird" / "cad_addon.json").read_text(encoding="utf-8"))
    except OSError:
        return None
    d = data / "addons" / f"cad-{meta['version']}"
    return d / "site-packages" if (d / "READY").exists() else None


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1])))
