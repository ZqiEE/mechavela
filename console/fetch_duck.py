"""Fetch the Open Duck Mini v2 benchmark assets from the upstream repositories onto this computer.

RLSOK does not redistribute them (Open_Duck_Playground has no licence; the robot replicates a Disney
character). This script downloads them for the user from upstream at the pinned commits, verifies the
policy against the hash used in RLSOK's verified benchmark, and records where every file came from.

    python fetch_duck.py <user_assets_dir>
"""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

PLAYGROUND = "b9be205ac64488c23504ca42e5ec790337adeec3"
MINI = "b23317a485b3cec7d8417f352478778b3475173c"
POLICY_SHA = "cb61453a8bcb547ccfdeb4f03ba0fa67ebcf767dcf4aa6e5c9a0d92b302f9b23"   # BEST_WALK_ONNX.onnx in RLSOK's benchmark
UA = {"User-Agent": "RLSOK/0.1 (+https://rlsok.com)"}


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def main() -> int:
    out = Path(sys.argv[1]) / "INTERNAL_open_duck_mini_v2"
    tmp = out.with_name(out.name + ".partial")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    rec = {"fetched_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "files": {}}
    print("step 1/3 Open_Duck_Playground source (zip, about 1 MB)", flush=True)
    z = zipfile.ZipFile(io.BytesIO(get(f"https://codeload.github.com/apirrone/Open_Duck_Playground/zip/{PLAYGROUND}")))
    prefix = f"Open_Duck_Playground-{PLAYGROUND}/"
    for name in z.namelist():
        rel = name[len(prefix):]
        if rel.startswith("playground/open_duck_mini_v2/xmls/") and not name.endswith("/"):
            dst = tmp / "xmls" / rel[len("playground/open_duck_mini_v2/xmls/"):]
        elif rel == "playground/open_duck_mini_v2/data/polynomial_coefficients.pkl":
            dst = tmp / "polynomial_coefficients.pkl"
        else:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(z.read(name))
    print("step 2/3 Open_Duck_Mini policy + licence", flush=True)
    for src, dst in (("BEST_WALK_ONNX.onnx", "BEST_WALK_ONNX.onnx"), ("LICENSE", "LICENSE.Open_Duck_Mini")):
        (tmp / dst).write_bytes(get(f"https://raw.githubusercontent.com/apirrone/Open_Duck_Mini/{MINI}/{src}"))
    got = hashlib.sha256((tmp / "BEST_WALK_ONNX.onnx").read_bytes()).hexdigest()
    if got != POLICY_SHA:
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"POLICY_HASH_MISMATCH {got}", flush=True)
        return 2
    print("step 3/3 verify and record", flush=True)
    for p in sorted(tmp.rglob("*")):
        if p.is_file():
            rec["files"][p.relative_to(tmp).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    rec["sources"] = {"Open_Duck_Playground": {"url": "https://github.com/apirrone/Open_Duck_Playground", "commit": PLAYGROUND,
                                              "license": "none in repository (all rights reserved by default)"},
                      "Open_Duck_Mini": {"url": "https://github.com/apirrone/Open_Duck_Mini", "commit": MINI, "license": "Apache-2.0 (code)"}}
    (tmp / "SOURCE.json").write_text(json.dumps(rec, indent=1), encoding="utf-8")
    (tmp / "INTERNAL_ONLY.txt").write_text(
        "Downloaded by you from the upstream repositories for research / internal benchmarking.\n"
        "Open_Duck_Playground has no licence file; Open Duck Mini replicates a Disney character.\n"
        "Do not redistribute or use for promotion.\n", encoding="utf-8")
    shutil.rmtree(out, ignore_errors=True)
    tmp.rename(out)
    print(f"DONE {len(rec['files'])} files -> {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
