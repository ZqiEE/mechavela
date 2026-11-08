"""Run AI-written build123d code safely and export the parts.

The code must assign `parts = {"name": <build123d Part or Solid>, ...}` (millimetres). Before running, an AST
whitelist rejects anything except build123d / math imports and plain modelling code: no file, network, process,
reflection or dunder access. It then runs in a separate Python process with a wall-clock limit; the parent reads only
the exported files and a JSON of measured properties. Same approach as the training reward API (robot/train/reward_api.py).
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

ALLOWED_IMPORTS = {"build123d", "math"}
FORBIDDEN_NAMES = {"open", "exec", "eval", "compile", "__import__", "globals", "locals", "vars", "getattr", "setattr",
                   "delattr", "input", "breakpoint", "help", "memoryview", "exit", "quit", "export_stl", "export_step",
                   "import_step", "import_stl", "import_svg", "Path"}
MAX_CODE_BYTES = 40_000


class CadRejected(ValueError):
    pass


def check(code: str) -> None:
    if len(code.encode()) > MAX_CODE_BYTES:
        raise CadRejected("CAD_CODE_TOO_LONG")
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise CadRejected(f"CAD_SYNTAX_ERROR: line {e.lineno}: {e.msg}") from None
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] not in ALLOWED_IMPORTS:
                    raise CadRejected(f"CAD_IMPORT_NOT_ALLOWED: {a.name}")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] not in ALLOWED_IMPORTS or node.level:
                raise CadRejected(f"CAD_IMPORT_NOT_ALLOWED: {node.module}")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            raise CadRejected(f"CAD_NAME_NOT_ALLOWED: {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise CadRejected(f"CAD_PRIVATE_ATTRIBUTE: {node.attr}")
        elif isinstance(node, (ast.Global, ast.Nonlocal, ast.AsyncFunctionDef, ast.Await, ast.Lambda)) and isinstance(node, (ast.Global, ast.Nonlocal)):
            raise CadRejected("CAD_GLOBAL_NOT_ALLOWED")
        elif isinstance(node, (ast.While,)):
            raise CadRejected("CAD_WHILE_NOT_ALLOWED: use for-loops over range()")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and ("/" in node.value or "\\" in node.value) and len(node.value) > 2:
            raise CadRejected("CAD_PATH_LIKE_STRING_NOT_ALLOWED")
    if not any(isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "parts" for t in n.targets)
               for n in ast.walk(tree)):
        raise CadRejected("CAD_MUST_ASSIGN_parts_DICT")


RUNNER = r'''
import json, sys
from pathlib import Path
if len(sys.argv) > 3 and sys.argv[3]:
    sys.path.insert(0, sys.argv[3])            # optional CAD add-on folder (packaged app)
out = Path(sys.argv[2])
code = Path(sys.argv[1]).read_text(encoding="utf-8")
safe_builtins = {k: __builtins__[k] if isinstance(__builtins__, dict) else getattr(__builtins__, k)
                 for k in ("range", "len", "min", "max", "abs", "round", "float", "int", "list", "dict", "tuple",
                           "enumerate", "zip", "sum", "sorted", "print", "bool", "str", "isinstance", "ValueError")}
safe_builtins["__import__"] = __import__   # imports were already whitelisted by the AST check
ns = {"__builtins__": safe_builtins}
exec(compile(code, "cad.py", "exec"), ns)
import build123d as bd
parts = ns.get("parts")
if not isinstance(parts, dict) or not parts:
    raise SystemExit("CAD_parts_MUST_BE_NONEMPTY_DICT")
res = {}
for name, shape in parts.items():
    if not str(name).replace("_", "").isalnum():
        raise SystemExit("CAD_BAD_PART_NAME: " + str(name))
    vol = float(shape.volume)                      # mm^3
    if not vol > 0:
        raise SystemExit("CAD_PART_HAS_NO_VOLUME: " + name)
    bb = shape.bounding_box()
    com = shape.center()
    bd.export_stl(shape, str(out / f"{name}.stl"))
    bd.export_step(shape, str(out / f"{name}.step"))
    res[name] = {"volume_mm3": vol, "bbox_mm": [bb.size.X, bb.size.Y, bb.size.Z], "center_mm": [com.X, com.Y, com.Z]}
(out / "cad_result.json").write_text(json.dumps(res, indent=1))
'''


def run(code: str, out_dir: Path, python: str | None = None, timeout_s: int = 120) -> dict:
    """Check, then execute in a child process. Returns {"parts": {...}} or raises CadRejected with the reason."""
    check(code)
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    if python is None:
        import os
        venv_py = Path(sys.prefix) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        python = os.environ.get("RLSOK_APP_PYTHON") or (str(venv_py) if sys.prefix != sys.base_prefix and venv_py.exists() else sys.executable)
    (out_dir / "cad.py").write_text(code, encoding="utf-8")
    (out_dir / "runner.py").write_text(RUNNER, encoding="utf-8")
    try:
        import os
        p = subprocess.run([python, "-I", str(out_dir / "runner.py"), str(out_dir / "cad.py"), str(out_dir),
                            os.environ.get("RLSOK_CAD_PATH", "")],
                           capture_output=True, text=True, timeout=timeout_s, cwd=str(out_dir),
                           # ezdxf's own switch for its pure-Python path: Windows application control can block the
                           # compiled extension DLL (seen 2026-10-23); the pure-Python path loads no such binary
                           env={**os.environ, "EZDXF_DISABLE_C_EXT": "1"})
    except subprocess.TimeoutExpired:
        raise CadRejected(f"CAD_TIMEOUT_{timeout_s}s") from None
    if p.returncode != 0 or not (out_dir / "cad_result.json").exists():
        tail = (p.stderr or p.stdout or "").strip().splitlines()[-3:]
        raise CadRejected("CAD_RUN_FAILED: " + " | ".join(tail)[:600])
    return {"parts": json.loads((out_dir / "cad_result.json").read_text())}
