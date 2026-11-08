"""RLSOK local web console (Python standard library only; binds 127.0.0.1).

    python console/server.py [--port 8765]

It never designs anything itself: a one-sentence request is routed by keywords to
an existing pipeline (circuit closed loop, robot benchmark, Open Duck training on
the local GPU), which runs as a subprocess and writes the usual evidence files.
The console only starts those processes and reads their files.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).resolve().parent / "static"
sys.path.insert(0,str(ROOT))
from implementation.workspace.product import storage_root
DATA = storage_root()
RUNS_EXTRA = Path(os.environ.get("RLSOK_RUNS", r"D:\RLSOK\runs"))
MAX_UPLOAD = 50 * 1024 * 1024
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "implementation"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import product as P  # noqa: E402
import system as SYS  # noqa: E402
from implementation.workspace.projects import Projects
from implementation.workspace.runtime import Runtime as RobotRuntime, capabilities as workspace_capabilities
from implementation.workspace.agent import EngineeringAgent, CompatibleProvider
from implementation.workspace.reference import create as create_reference

EXAMPLES = P.EXAMPLES
USER_ASSETS = DATA / "user_assets"
os.environ.setdefault("RLSOK_USER_ASSETS", str(USER_ASSETS))   # robot adapters look here for user-fetched assets
READ_ROOTS = [ROOT / "usage", ROOT / "release", RUNS_EXTRA, DATA, EXAMPLES]
APP_PY = P.APP_PY
DUCK_WORDS = re.compile(r"duck|鸭", re.I)

CIRCUIT_WORDS = re.compile(r"电路|滤波|电压|电源|降压|升压|振荡|放大|电阻|电容|电感|稳压|circuit|filter|volt|buck|boost|"
                           r"oscillat|amplif|regulat|resist|capacit|inductor|ngspice|"
                           r"\d\s*(?:[mkuμ]?V|[mμ]?A|[kKmM]?Ω|k?ohm|[kKmM]?Hz|[mk]?W)(?![a-zA-Z/])", re.I)
TRAIN_WORDS = re.compile(r"训练|复现|train|reproduc", re.I)
ROBOT_WORDS = re.compile(r"机器人|人形|双足|行走|走路|步态|鸭|duck|g1|unitree|robot|humanoid|biped|walk|gait", re.I)
G1_WORDS = re.compile(r"g1|unitree|宇树|人形|humanoid", re.I)
BIRD_WORDS = re.compile(r"鸟|扑翼|飞鸟|ornithopter|flapping|bird", re.I)
LOOPS = {"circuit": "implementation.circuit.cli", "bird": "implementation.bird.cli"}   # AI design loops


def loop_argv(pipeline: str, *args: str) -> list[str]:
    code = ("import sys; sys.path.insert(0, sys.argv.pop(1)); "
            f"from {LOOPS[pipeline]} import main; raise SystemExit(main())")
    py = circuit_python() if pipeline == "circuit" else APP_PY     # the bird loop needs build123d (app Python)
    return [py, "-I", "-B", "-c", code, str(ROOT), *args]


def cad_addon_path():
    import fetch_addon
    return fetch_addon.installed_path(DATA)


def cad_ok() -> bool:
    """Bird design runs AI-written build123d code: the CAD add-on (or a build123d already importable) is required."""
    return bool(cad_addon_path()) or importlib.util.find_spec("build123d") is not None


def route(sentence: str) -> dict:
    """Keyword routing only - stated as such to the user; it is not language understanding."""
    s = sentence.strip()
    if not s:
        return {"pipeline": None, "reason": "empty"}
    if BIRD_WORDS.search(s):
        return {"pipeline": "bird", "reason": "matched bird keywords"}
    if CIRCUIT_WORDS.search(s):
        return {"pipeline": "circuit", "reason": "matched circuit keywords"}
    if TRAIN_WORDS.search(s) and ROBOT_WORDS.search(s):
        return {"pipeline": "train_measure", "robot": "open_duck_mini_v2",
                "reason": "matched training + robot keywords (only Open Duck trains on this GPU)"}
    if ROBOT_WORDS.search(s):
        # G1 ships with RLSOK; Open Duck only when asked for (the user fetches it from upstream)
        robot = "open_duck_mini_v2" if DUCK_WORDS.search(s) and not G1_WORDS.search(s) else "g1_velocity_flat"
        return {"pipeline": "robot_bench", "robot": robot, "reason": "matched robot keywords"}
    return {"pipeline": None, "reason": "no keyword matched; choose a pipeline"}


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _json(p: Path, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def safe_path(raw: str) -> Path | None:
    try:
        p = Path(unquote(raw)).resolve()
    except (OSError, ValueError):
        return None
    for r in READ_ROOTS:
        try:
            p.relative_to(r.resolve())
            return p if p.is_file() else None
        except ValueError:
            continue
    return None


def safe_dir(raw: str) -> Path | None:
    try:
        p = Path(unquote(raw)).resolve()
    except (OSError, ValueError):
        return None
    for r in READ_ROOTS:
        try:
            p.relative_to(r.resolve())
            return p if p.is_dir() else None
        except ValueError:
            continue
    return None


def circuit_python() -> str:
    if os.environ.get("RLSOK_PYTHON"):
        return os.environ["RLSOK_PYTHON"]
    lock = _json(ROOT / "toolchain-cache" / "reality-engines.lock.json", {})
    return lock.get("python") or sys.executable


# ----------------------------------------------------------------------------- jobs
class Jobs:
    def __init__(self):
        self.lock = threading.Lock()
        self.jobs: dict[str, dict] = {}
        self.procs: dict[str, subprocess.Popen] = {}
        (DATA / "jobs").mkdir(parents=True, exist_ok=True)
        for f in (DATA / "jobs").glob("*.json"):
            j = _json(f)
            if j:
                if j.get("state") == "running":
                    j["state"] = "interrupted"
                self.jobs[j["id"]] = j

    def _save(self, j):
        (DATA / "jobs" / f"{j['id']}.json").write_text(json.dumps(j, ensure_ascii=False, indent=1), encoding="utf-8")

    def start(self, pipeline: str, sentence: str, opts: dict) -> dict:
        jid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        out = DATA / "jobs" / jid
        out.mkdir(parents=True)
        env_extra = {}
        if pipeline in LOOPS:
            run_dir = out / "run"
            if opts.get("agent"):           # explicit choice from the request (tests / power users)
                agent = ["--agent", opts["agent"]] + (["--agent-command", opts["agent_command"]]
                                                      if opts.get("agent") == "command" and opts.get("agent_command") else [])
            else:                           # the AI the user connected in the first-run setup
                agent, env_extra = P.agent_args(DATA)
            argv = loop_argv(pipeline, "run", "--requirement", sentence, "--out", str(run_dir)) + agent
            if pipeline == "bird" and (DATA / "bird_parts_local.json").exists():
                argv += ["--local-parts", str(DATA / "bird_parts_local.json")]
        elif pipeline == "robot_bench":
            robot = opts.get("robot", "g1_velocity_flat")
            run_dir = out / robot
            ref = {"open_duck_mini_v2": ROOT / "usage/robot-bench-win-20261020/open_duck_mini_v2/upstream_reference.json",
                   "g1_velocity_flat": ROOT / "usage/robot-bench-20261020/g1_velocity_flat/upstream_reference.json"}[robot]
            argv = [APP_PY, str(ROOT / "scripts/robot_run_bench.py"), robot, str(run_dir)]
            if ref.exists():
                argv += ["--reference", str(ref)]
        elif pipeline in ("train_measure", "train_full"):
            run_dir = RUNS_EXTRA
            if shutil.which("wsl") is None:
                raise RuntimeError("WSL2 is not available on this machine")
            argv = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                    str(ROOT / "train_open_duck.ps1"), "measure" if pipeline == "train_measure" else "full"]
        elif pipeline in ("install_train", "install_mjlab"):
            run_dir = out
            script = "wsl_setup_train_env.sh" if pipeline == "install_train" else "wsl_setup_mjlab_env.sh"
            setup = "/mnt/" + str(ROOT / "scripts" / script).replace(":", "").replace("\\", "/")
            setup = setup[:5] + setup[5].lower() + setup[6:]
            argv = ["wsl", "-d", SYS.DISTRO, "-u", "root", "--", "bash", "-c", f"tr -d '\\r' < '{setup}' | bash"]
        elif pipeline == "install_wsl":
            run_dir = out
            # elevation prompt is shown to the user by Windows; we only start it
            argv = ["powershell", "-NoProfile", "-Command",
                    f"Start-Process wsl -ArgumentList '--install','-d','{SYS.DISTRO}' -Verb RunAs -Wait"]
        elif pipeline == "install_cad":
            run_dir = DATA / "addons"
            argv = [APP_PY, str(Path(__file__).resolve().parent / "fetch_addon.py"), str(DATA)]
        elif pipeline == "duck_assets":
            run_dir = USER_ASSETS
            argv = [APP_PY, str(Path(__file__).resolve().parent / "fetch_duck.py"), str(USER_ASSETS)]
        else:
            raise ValueError(f"unknown pipeline {pipeline}")
        j = {"id": jid, "pipeline": pipeline, "sentence": sentence, "opts": opts, "argv": argv,
             "run_dir": str(run_dir), "state": "running", "started": time.time(), "log": str(out / "log.txt")}
        with self.lock:
            self.jobs[jid] = j
            self._save(j)
        # secrets travel only in the child's environment; they are never written into the job record
        threading.Thread(target=self._run, args=(j, env_extra), daemon=True).start()
        return j

    def _run(self, j, env_extra=None):
        with open(j["log"], "a", encoding="utf-8", errors="replace") as log:
            env = dict(os.environ, PYTHONIOENCODING="utf-8", **(env_extra or {}))
            cad = cad_addon_path()
            if cad:
                env["RLSOK_CAD_PATH"] = str(cad)
            try:
                p = subprocess.Popen(j["argv"], stdout=log, stderr=subprocess.STDOUT, cwd=str(ROOT), env=env,
                                     creationflags=SYS.NO_WINDOW)
                with self.lock:
                    self.procs[j["id"]] = p
                rc = p.wait()
            except OSError as e:
                log.write(f"\nNOT STARTED: {e}\n")
                rc = -1
            finally:
                with self.lock:
                    self.procs.pop(j["id"], None)
        try:
            tail = Path(j["log"]).read_text(encoding="utf-8", errors="replace")[-20000:]
        except OSError:
            tail = ""
        with self.lock:
            j["returncode"] = rc
            j["ended"] = time.time()
            j["state"] = "stopped" if j.pop("stop_requested", False) else {0: "finished", 3: "awaiting_ai"}.get(rc, "failed")
            j["error"] = SYS.explain(tail) if j["state"] == "failed" else None
            self._save(j)
        if j["state"] == "stopped":
            return
        if j["pipeline"] not in ("install_wsl",):
            mins = max(1, round((j["ended"] - j["started"]) / 60))
            SYS.notify("RLSOK", {"finished": f"任务完成（{mins} 分钟）", "awaiting_ai": "等待你的 AI 回复",
                                 "failed": "任务失败：打开 RLSOK 查看原因和解决办法"}[j["state"]])

    def stop(self, jid: str) -> dict:
        """Stop a running job and everything it started (process tree). Results so far are kept on disk."""
        j = self.jobs[jid]
        with self.lock:
            p = self.procs.get(jid)
            if j["state"] == "awaiting_ai" and not p:       # nothing running: just close it
                j["state"], j["ended"] = "stopped", time.time()
                self._save(j)
                return j
            if not p:
                raise ValueError("this job is not running")
            j["stop_requested"] = True
        if SYS.WIN:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True, creationflags=SYS.NO_WINDOW)
        else:
            p.kill()
        return j

    def add_note(self, jid: str, text: str) -> dict:
        """User guidance for the AI between rounds (circuit). The frozen spec and checks are not changed."""
        j = self.jobs[jid]
        if j["pipeline"] not in LOOPS:
            raise ValueError("notes are for AI design jobs (circuit, bird)")
        if j["state"] in ("finished", "failed", "stopped"):
            raise ValueError("this job has ended; use 'continue from this version' to start a new one")
        if j["pipeline"] == "bird":
            from implementation.bird.loop import BirdLoop as CircuitLoop
        else:
            from implementation.circuit.loop import CircuitLoop
        loop = CircuitLoop.__new__(CircuitLoop)
        loop.dir = Path(j["run_dir"]).resolve()
        return loop.add_user_note(text)

    def continue_from(self, jid: str, change: str) -> dict:
        """New circuit job that starts from this job's last passing (or last simulated) design plus the user's change."""
        j = self.jobs[jid]
        change = (change or "").strip()
        if j["pipeline"] not in LOOPS or not change:
            raise ValueError("describe what to change")
        run = Path(j["run_dir"])
        rounds = sorted((run / "rounds").glob("round-*/round.json")) if (run / "rounds").exists() else []
        records = [_json(r) or {} for r in rounds]
        base = next((r for r in reversed(records) if r.get("status") == "PASS"), None) or \
            next((r for r in reversed(records) if r.get("dut_body")), None)
        original = (_json(run / "run.json") or {}).get("requirement") or j["sentence"]
        text = f"{original}\n\n在上一版的基础上修改：{change}"
        if j["pipeline"] == "bird":
            designs = sorted((run / "rounds").glob("round-*/design.json")) if (run / "rounds").exists() else []
            passed = [d for d, r in zip(designs, records) if r.get("status") == "PASS"] or designs
            if passed:
                text += "\n\n(Previous design JSON - start from it and apply the change):\n" + passed[-1].read_text(encoding="utf-8")[:20000]
            return self.start("bird", text, {"base_job": jid})
        if base and base.get("dut_body"):
            text += "\n\n(Previous design, round %s, status %s - start from it and apply the change):\n%s" % (
                base.get("round"), base.get("status"), "\n".join(base["dut_body"]))
        return self.start("circuit", text, {"base_job": jid})

    def resume_with_agent(self, jid: str) -> dict:
        """Continue an exchange-mode circuit job automatically with the AI the user connected."""
        j = self.jobs[jid]
        if j["pipeline"] not in LOOPS:
            raise ValueError("only AI design jobs")
        agent, env_extra = P.agent_args(DATA)
        j["argv"] = loop_argv(j["pipeline"], "resume", "--out", j["run_dir"]) + agent
        j["state"] = "running"
        self._save(j)
        threading.Thread(target=self._run, args=(j, env_extra), daemon=True).start()
        return j

    def resume_circuit(self, jid: str, response_text: str) -> dict:
        j = self.jobs[jid]
        if j["pipeline"] not in LOOPS:
            raise ValueError("only AI design jobs wait for an AI reply")
        st = self.circuit_state(j)
        waiting = (st or {}).get("awaiting")
        if not waiting:
            raise ValueError("this job is not waiting for an AI reply")
        from implementation.circuit.agents import extract_json
        try:
            extract_json(response_text)          # check before it counts as an attempt
        except ValueError:
            raise ValueError("NO_JSON_IN_REPLY") from None
        Path(waiting["response"]).write_text(response_text, encoding="utf-8")
        j["argv"] = loop_argv(j["pipeline"], "resume", "--out", j["run_dir"])
        j["state"] = "running"
        self._save(j)
        threading.Thread(target=self._run, args=(j,), daemon=True).start()
        return j

    @staticmethod
    def circuit_state(j) -> dict | None:
        run = Path(j["run_dir"])
        if not run.exists():
            return None
        prompts = sorted(run.glob("**/prompt.md"), key=lambda p: p.stat().st_mtime)
        pending = [p for p in prompts if not (p.parent / "response.md").exists()]
        rep = _json(run / "final" / "report.json")
        return {"awaiting": {"prompt": str(pending[-1]), "response": str(pending[-1].parent / "response.md"),
                             "prompt_text": pending[-1].read_text(encoding="utf-8")} if pending and not rep else None,
                "report": rep and {k: rep.get(k) for k in ("status", "achieved", "conclusion", "final_round",
                                                            "native_simulation_count", "iterations", "bom")},
                "report_html": str(run / "final" / "report.html") if rep else None,
            "report_html_en": str(run / "final" / "report.en.html") if rep and (run / "final" / "report.en.html").is_file() else None}

    def view(self, jid: str) -> dict:
        j = dict(self.jobs[jid])
        try:
            j["log_tail"] = Path(j["log"]).read_text(encoding="utf-8", errors="replace")[-6000:]
        except OSError:
            j["log_tail"] = ""
        if j["state"] == "failed":          # explain with the current rules (older records said only "unknown")
            j["error"] = SYS.explain(j["log_tail"])
        if j["pipeline"] in LOOPS:
            j["circuit"] = self.circuit_state(j)
        elif j["pipeline"] == "robot_bench":
            j["result"] = robot_run_summary(Path(j["run_dir"]))
        return j


# ----------------------------------------------------------------------------- per-round timeline
def _minmax(xs, ys, buckets=500):
    n = len(xs)
    step = max(1, n // buckets)
    ox, oy = [], []
    for i in range(0, n, step):
        seg = range(i, min(i + step, n))
        lo = min(seg, key=lambda k: ys[k])
        hi = max(seg, key=lambda k: ys[k])
        for k in sorted({lo, hi}):
            ox.append(xs[k])
            oy.append(ys[k])
    return ox, oy


def _check_view(c: dict) -> dict:
    return {k: c.get(k) for k in ("id", "metric", "op", "unit", "description", "min", "max", "value", "actual", "passed", "margin")}


def circuit_timeline(run: Path) -> dict:
    """Structured view of a circuit loop run directory: spec, every round, final verdict."""
    out = {"kind": "circuit", "spec": None, "rounds": [], "final": None, "awaiting": None}
    spec = _json(run / "spec" / "spec.json")
    if spec:
        out["spec"] = {"checks": [_check_view(c) for c in spec.get("checks", [])],
                       "analysis": spec.get("analysis"), "assumptions": spec.get("assumptions")}
    for rd in sorted((run / "rounds").glob("round-*")) if (run / "rounds").exists() else []:
        r = _json(rd / "round.json") or {}
        d = _json(rd / "design.json") or {}
        ts = r.get("tolerance_summary") or {}
        worst_fail = [{"check": k, **v} for k, v in (ts.get("worst") or {}).items() if v and v.get("passed") is False]
        out["rounds"].append({
            "round": int(rd.name.split("-")[1]), "author": r.get("author") or d.get("author"),
            "status": r.get("status") or ("AWAITING_SIMULATION" if d else "AWAITING_AI"),
            "reason": r.get("reason"), "diagnosis": r.get("diagnosis") or d.get("diagnosis"),
            "changes": r.get("changes") or d.get("changes"),
            "declare_infeasible": d.get("declare_infeasible"), "infeasible_reason": d.get("infeasible_reason"),
            "checks": [_check_view(c) for c in (r.get("checks") or [])],
            "failed_checks": [_check_view(c) for c in (r.get("checks") or []) if c.get("passed") is False],
            "tolerance": {"passed": ts.get("passed_cases"), "total": ts.get("total_cases"), "worst_fail": worst_fail}
            if ts else None,
            "has_wave": (rd / "nominal" / "vectors.json.gz").exists() or (rd / "eval" / "nominal" / "vectors.json.gz").exists()})
    out["progress"] = _json(run / "progress.json")
    notes = run / "notes.jsonl"
    out["notes"] = [json.loads(x) for x in notes.read_text(encoding="utf-8").splitlines() if x.strip()] if notes.exists() else []
    rep = _json(run / "final" / "report.json")
    if rep:
        out["final"] = {"status": rep.get("status"), "achieved": rep.get("achieved"), "conclusion": rep.get("conclusion"),
                        "label": rep.get("label"),
                        "native_simulations": rep.get("native_simulation_count"),
                        "report_html": str(run / "final" / "report.html"),
                        "report_html_en": str(run / "final" / "report.en.html") if (run / "final" / "report.en.html").is_file() else None}
        if rep.get("achieved") and spec and "testbench" in spec:
            from implementation.circuit import buildability as BLD
            final = _json(run / "rounds" / ("round-%02d" % int(rep.get("final_round") or 0)) / "round.json") or {}
            out["final"]["buildability"] = BLD.assess(spec, final.get("dut_body"))
        out["final"]["downloads"] = [k for k, _ in download_files(run)]
    else:
        prompts = sorted(run.glob("**/prompt.md"), key=lambda p: p.stat().st_mtime)
        pending = [p for p in prompts if not (p.parent / "response.md").exists()]
        if pending:
            p = pending[-1]
            where = "spec" if "spec" in p.parts else "round"
            out["awaiting"] = {"stage": where, "prompt": p.read_text(encoding="utf-8"), "response": str(p.parent / "response.md")}
            # tell the user (not only the AI) why the previous reply was not accepted
            if where == "spec":
                rejected = [(_json(v) or {}) for v in sorted((run / "spec").glob("attempt-*/validation.json"))]
                rejected = [r for r in rejected if r.get("ok") is False]
                if rejected:
                    out["awaiting"]["previous_rejection"] = {"error": rejected[-1].get("error", ""), "attempt": len(rejected) + 1,
                                                             "of": 3}
            elif out["rounds"] and out["rounds"][-2:-1] and out["rounds"][-2]["status"] == "INPUT_REJECTED":
                out["awaiting"]["previous_rejection"] = {"error": out["rounds"][-2].get("reason") or "", "round": out["rounds"][-2]["round"]}
    return out


def download_files(run: Path) -> list[tuple[str, Path]]:
    """What the result page offers to download: the final netlist, a BOM table, the report, all evidence (zip)."""
    rep = _json(run / "final" / "report.json")
    if not rep:
        return []
    out = []
    rnd = run / "rounds" / ("round-%02d" % int(rep.get("final_round") or 0))
    if (rnd / "netlist.cir").exists():
        out.append(("netlist", rnd / "netlist.cir"))
    if (rnd / "eval" / "cad").exists():                         # bird: printed parts and controller parameters
        out.append(("stl", rnd / "eval" / "cad"))
    if (rnd / "eval" / "flight_params.json").exists():
        out.append(("params", rnd / "eval" / "flight_params.json"))
    if rep.get("bom"):
        out.append(("bom", run / "final" / "report.json"))      # rendered as CSV on the fly
    out.append(("report", run / "final" / "report.html"))
    out.append(("bundle", run))
    return out


def bom_csv(report: dict) -> bytes:
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    if (report.get("schema") or "").startswith("rlsok.bird"):
        w.writerow(["part", "category", "qty", "mass_g_each", "reference_price", "currency", "retrieved", "source",
                    "参考价，以购买时为准"])
        for b in report.get("bom") or []:
            w.writerow([b.get("name"), b.get("category"), b.get("qty"), b.get("mass_g_each"), b.get("price_each"),
                        b.get("currency"), b.get("retrieved"), b.get("source") or "估算，无来源", ""])
        return ("﻿" + buf.getvalue()).encode("utf-8")
    w.writerow(["refdes", "type", "value", "buy", "tolerance", "AI part suggestion (unverified)", "model / note"])
    for b in report.get("bom") or []:
        w.writerow([b.get("refdes"), b.get("type"), b.get("value"), "yes" if b.get("purchasable") else "no",
                    b.get("tolerance"), b.get("ai_part_suggestion_unverified", ""), b.get("model") or b.get("note", "")])
    return ("﻿" + buf.getvalue()).encode("utf-8")      # BOM so Excel opens the Chinese text correctly


def bundle_zip(run: Path) -> bytes:
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(run.rglob("*")):
            if p.is_file() and p.stat().st_size < 20 * 1024 * 1024:
                z.write(p, Path(run.name) / p.relative_to(run))
    return buf.getvalue()


def replay_index(run: Path, rnd: int | None = None) -> dict | None:
    """Simulation-viewer index (rlsok.replay/1) of a run folder.
    Bird design runs keep one per round (rounds/round-NN/eval/replay); robot runs keep one in replay/.
    Returns the index plus "base" (folder for /file URLs) and the rounds that have one."""
    cands = {}
    for rd in sorted((run / "rounds").glob("round-*")) if (run / "rounds").exists() else []:
        if (rd / "eval" / "replay" / "index.json").exists():
            cands[int(rd.name.split("-")[1])] = rd / "eval" / "replay"
    if (run / "replay" / "index.json").exists():
        cands[0] = run / "replay"
    if not cands:
        return None
    k = rnd if rnd in cands else max(cands)
    try:
        idx = json.loads((cands[k] / "index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None                      # being replaced right now; the viewer polls again
    idx["base"] = str(cands[k])
    idx["round"] = k
    idx["rounds_available"] = sorted(cands)
    return idx


def circuit_wave(run: Path, rnd: int) -> dict | None:
    import gzip
    p = run / "rounds" / f"round-{rnd:02d}" / "nominal" / "vectors.json.gz"
    if not p.exists():
        p = run / "rounds" / f"round-{rnd:02d}" / "eval" / "nominal" / "vectors.json.gz"   # bird flight log
    if not p.exists():
        return None
    v = json.loads(gzip.decompress(p.read_bytes()))
    spec = _json(run / "spec" / "spec.json") or {}
    series = []
    if "time" in v["vectors"]:
        x = v["vectors"]["time"]
        for name, ys in v["vectors"].items():
            if name != "time" and not name.endswith("#branch"):
                sx, sy = _minmax(x, ys)
                bird = isinstance(v.get("units"), dict)          # circuit files carry "units" as a plain string
                series.append({"name": name, "x": list(sx) if bird else [t * 1e3 for t in sx], "y": sy,
                               "unit": v["units"].get(name, "") if bird else "V"})
        xunit = "s" if isinstance(v.get("units"), dict) else "ms"      # circuits: x converted to ms above
    else:
        import math
        x = v["vectors"].get("frequency", [])
        for name, c in (v.get("complex_vectors") or {}).items():
            db = [20 * math.log10(max(math.hypot(a, b), 1e-30)) for a, b in zip(c["re"], c["im"])]
            series.append({"name": name + " (dB)", "x": x, "y": db, "unit": "dB"})
        xunit = "Hz"
    limits = [c for c in spec.get("checks", []) if c.get("unit") in ("V", "dB")]
    return {"round": rnd, "analysis": v.get("analysis_command"), "xunit": xunit, "series": series,
            "limits": [_check_view(c) for c in limits], "source_sha256": _sha(p)}


def train_progress(since: float) -> dict | None:
    if not RUNS_EXTRA.exists():
        return None
    dirs = [d for d in RUNS_EXTRA.glob("open_duck_*") if d.is_dir() and d.stat().st_mtime >= since - 5]
    if not dirs:
        return None
    d = max(dirs, key=lambda x: x.stat().st_mtime)
    rows = []
    try:
        rows = [json.loads(x) for x in (d / "progress.jsonl").read_text(encoding="utf-8").splitlines() if x]
    except OSError:
        pass
    return {"kind": "train", "dir": str(d), "status": _json(d / "status.json"), "estimate": _json(d / "throughput_estimate.json"),
            "level2": _json(d / "level2.json"), "record": _json(d / "train_record.json"),
            "curve": [[r["steps"], r.get("eval/episode_reward"), r.get("eval/avg_episode_length")] for r in rows]}


# ----------------------------------------------------------------------------- evidence index
def robot_run_summary(d: Path) -> dict | None:
    r = _json(d / "run.json")
    if not r or "sections" not in r or "benchmark" not in r:   # e.g. a circuit loop's own run.json
        return None
    S = r["sections"]
    files = {k: str(d / k) for k in r.get("files", {})}
    return {"dir": str(d), "robot": r["robot"]["key"], "title": r["robot"]["title"], "internal_only": r["internal_only"],
            "platform": r["environment"]["platform"], "level1": r["level1"], "level2": r["level2"],
            "labels": {k: v.get("label") for k, v in S.items() if isinstance(v, dict) and "label" in v},
            "robustness": {k: {"survived": v["survived"], "episodes": v["episodes"], "label": v["label"]}
                           for k, v in S["robustness"].items()},
            "tracking": S["nominal"]["tracking"], "thermal": (S.get("thermal") or {}).get("thermal"),
            "files": files}


def evidence_index() -> dict:
    robots = []
    for run_json in sorted((ROOT / "usage").glob("robot-bench-*/*/run.json")) + sorted(DATA.glob("jobs/*/*/run.json")):
        try:
            s = robot_run_summary(run_json.parent)
        except (KeyError, TypeError, ValueError):
            s = None   # one malformed record must not take down the whole index
        if s:
            robots.append(s)
    circuits = []
    for rep in sorted((ROOT / "usage").glob("circuit-loop*/runs/*/final/report.json")) + sorted(DATA.glob("jobs/*/run/final/report.json")):
        r = _json(rep, {})
        circuits.append({"dir": str(rep.parent.parent), "requirement": r.get("requirement"), "status": r.get("status"),
                         "achieved": r.get("achieved"), "conclusion": r.get("conclusion"),
                         "final_round": r.get("final_round"), "native_simulations": r.get("native_simulation_count"),
                         "platform": ((r.get("tools") or {}).get("solver") or {}).get("platform"),
                         "report_html": str(rep.parent / "report.html")})
    trains = []
    if RUNS_EXTRA.exists():
        for st in sorted(RUNS_EXTRA.glob("open_duck_*/status.json")):
            d = st.parent
            prog = []
            try:
                prog = [json.loads(x) for x in (d / "progress.jsonl").read_text(encoding="utf-8").splitlines() if x]
            except OSError:
                pass
            trains.append({"dir": str(d), "status": _json(st), "record": _json(d / "train_record.json"),
                           "estimate": _json(d / "throughput_estimate.json"), "level2": _json(d / "level2.json"),
                           "curve": [[p["steps"], p.get("eval/episode_reward")] for p in prog]})
    uploads = [_json(p) for p in sorted((DATA / "uploads").glob("*.json"))] if (DATA / "uploads").exists() else []
    unfinished = [{"id": j["id"], "sentence": j.get("sentence"), "pipeline": j["pipeline"], "opts": j.get("opts"),
                   "state": j["state"], "code": (j.get("error") or {}).get("code"), "started": j["started"]}
                  for j in (JOBS.jobs.values() if JOBS else []) if j["state"] in ("failed", "stopped", "interrupted")]
    return {"robots": robots, "circuits": circuits, "trains": trains, "uploads": [u for u in uploads if u],
            "unfinished": sorted(unfinished, key=lambda x: -x["started"])}


def capabilities() -> dict:
    from robot.train import backends as B
    out = {"backends": {}, "ngspice": None, "wsl": shutil.which("wsl") is not None}
    for k, b in B.BACKENDS.items():
        try:
            av = b.availability()
        except Exception as e:  # noqa: BLE001
            av = {"available": False, "missing": [str(e)]}
        out["backends"][k] = {"title": b.title, "internal_only": b.internal_only, "available": av.get("available"),
                              "missing": av.get("missing")}
    lock = _json(ROOT / "toolchain-cache" / "reality-engines.lock.json", {})
    out["ngspice"] = {"locked_python": lock.get("python"), "present": Path(lock.get("python", "")).exists()}
    return out


# ----------------------------------------------------------------------------- uploads
UPLOAD_KINDS = {"temperature_log": "温度日志（CSV：t_s, temp_C, power_W）",
                "motor_params": "电机/舵机参数（JSON）", "trajectory": "真机轨迹或 IMU 记录", "other": "其他"}


def save_upload(name: str, kind: str, body: bytes, note: str) -> dict:
    if kind not in UPLOAD_KINDS:
        raise ValueError(f"unknown kind {kind}")
    if len(body) > MAX_UPLOAD:
        raise ValueError("file larger than 50 MB")
    d = DATA / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(body).hexdigest()
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", Path(name).name)[:80] or "upload.bin"
    f = d / f"{digest[:16]}_{safe}"
    f.write_bytes(body)
    meta = {"file": str(f), "name": name, "kind": kind, "note": note[:500], "sha256": digest, "bytes": len(body),
            "uploaded_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "label": {"status": "UNVERIFIED", "zh": "未校准（用户上传，未验证）", "en": "Uncalibrated (user upload)",
                      "conditions": ["平台只记录了 sha256；数据来源与测量方法由上传者负责"]}}
    if kind == "temperature_log":
        meta["thermal_fit"] = fit_temperature_log(f)
    (d / f"{digest[:16]}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return meta


def fit_temperature_log(path: Path) -> dict:
    import numpy as np

    from robot import thermal
    try:
        arr = np.genfromtxt(path, delimiter=",", names=True, dtype=float, encoding="utf-8")
        t, temp, power = (np.asarray(arr[c], float) for c in ("t_s", "temp_C", "power_W"))
    except Exception as e:  # noqa: BLE001
        return {"status": "NOT_RUN", "reason": f"需要 CSV 表头 t_s,temp_C,power_W：{e}"}
    dts = np.diff(t)
    if len(t) < 10 or not np.all(dts > 0) or np.ptp(dts) > 0.01 * np.mean(dts):
        return {"status": "NOT_RUN", "reason": "需要至少 10 行、等间隔采样的 t_s"}
    try:
        # each CSV row holds T[k] and the power applied from t[k] to t[k+1]: N+1 temps, N powers
        fit = thermal.fit(float(np.mean(dts)), power[:-1], temp)
    except Exception as e:  # noqa: BLE001
        return {"status": "FAILED", "reason": str(e)}
    return {"status": "UNVERIFIED", "result": fit, "note": "用用户数据拟合的热参数；数据本身未经平台核实"}


# ----------------------------------------------------------------------------- HTTP
JOBS: Jobs | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = "RLSOK-console/1"

    def log_message(self, fmt, *args):  # quiet
        pass

    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))

    def _err(self, code, msg):
        self._json({"error": msg}, code)

    def _local_only(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            self._err(HTTPStatus.FORBIDDEN, "console only answers on localhost")
            return False
        return True

    def do_GET(self):  # noqa: N802
        if not self._local_only():
            return
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path.startswith('/api/workspace/'):
            try:
                pid = q.get('project', [''])[0]
                if u.path=='/api/workspace/play-state':return self._json(PLAY.state(pid,q.get('session',[''])[0]))
                if u.path.startswith('/api/workspace/hub'):
                    from implementation.workspace.hub import Hub
                    hub=Hub(DATA,WORKSPACE)
                    if u.path=='/api/workspace/hub':return self._json(hub.list())
                    if u.path=='/api/workspace/hub-geometry':return self._json(hub.geometry(q.get('item',[''])[0]))
                    if u.path=='/api/workspace/hub-mesh':return self._send(200,hub.bird_mesh(q.get('name',[''])[0]),'application/octet-stream')
                    if u.path=='/api/workspace/hub-package':
                        raw=hub.package(q.get('item',[''])[0],q.get('version',[None])[0])
                        return self._send(200,raw,'application/zip',{'Content-Disposition':'attachment; filename="robot-share.zip"'})
                if u.path=='/api/workspace/session':
                    from implementation.workspace.workspace_session import session
                    return self._json(session(DATA,WORKSPACE))
                if u.path=='/api/workspace/design-evidence':
                    p=WORKSPACE.get(pid)
                    candidates=[r for r in p['runs'] if r['adapter']=='project.design/1' and r['revision']==p['revision']]
                    r=max(candidates,key=lambda r:r['sequence']) if candidates else None
                    return self._json({'project':pid,'revision':p['revision'],'inspection':r,'requirements':'NOT_EVALUATED'})
                if u.path=='/api/workspace/ui-preferences':
                    from implementation.workspace.ui_preferences import preferences
                    return self._json(preferences(DATA))
                if u.path=='/api/workspace/connections':
                    from implementation.workspace.mcp_access import public_grants
                    project=WORKSPACE.get(pid) if pid else None
                    from implementation.workspace.official_identity import status as official_status
                    codex_status=official_status()
                    chat_index=DATA/'mcp/task-history'/(pid+'.json') if pid else None
                    chat={}
                    if chat_index and chat_index.is_file():
                        from implementation.workspace.projects import read as read_chat_index
                        chat=read_chat_index(chat_index)
                    chat_available=bool(pid and codex_status.get('identity') and chat.get('project')==pid and chat.get('identity')==codex_status['identity'] and re.fullmatch('[a-f0-9-]{36}',chat.get('thread_id','')))
                    return self._json({'workspace_chat_available':chat_available,'project':{'id':project['id'],'name':project['name']} if project else None,'codex':{**codex_status,'auth':'Login and credential refresh stay inside your official Codex client; network inference not established by metadata'},
                        'claude':{'installed':bool(shutil.which('claude')),'auth':'Sign in inside unmodified Claude Code; no subscription token enters RLSOK'},
                        'embedded_chatgpt':{'status':'ELIGIBILITY_UNVERIFIED','reason':'No confirmed OSS license/application registration or commercial approval; use official client + MCP'},
                        'grants':public_grants(DATA,pid) if pid else []})
                if u.path=='/api/workspace/official-status':
                    from implementation.workspace.official_identity import status as official_status
                    return self._json(official_status(force=True))
                if u.path=='/api/workspace/connection-setup':
                    from implementation.workspace.mcp_access import connection_setup
                    return self._json(connection_setup(DATA,WORKSPACE,pid,q.get('connection',[''])[0]))
                if u.path=='/api/workspace/agent-journal':
                    sid=q.get('session',[''])[0]
                    if not re.fullmatch('[a-f0-9]{32}',sid):raise ValueError('Invalid session id')
                    path=WORKSPACE.path(pid)/'agent'/'sessions'/(sid+'.json')
                    if path.is_symlink():raise ValueError('Session symlinks refused')
                    return self._send(200,path.read_bytes(),'application/json',{'Content-Disposition':'attachment; filename="agent-session.json"'})
                if u.path=='/api/workspace/product':
                    from implementation.workspace.product import VERSION
                    return self._json({'name':'Mechavela Robotics Workspace','version':VERSION,'data_dir':str(DATA.resolve()),
                        'project_dir':str(WORKSPACE.root),'distribution':'packaged release candidate' if (ROOT.parent/'BUILD.json').exists() else 'development source checkout','python':sys.version.split()[0],'os':sys.platform,
                        'library_context':os.environ.get('RLSOK_PROJECT_CONTEXT','personal'),'agent_protocol':'chat-completions-tools/1'})
                if u.path=='/api/workspace/attachment':
                    name=q.get('name',[''])[0]
                    return self._send(200,WORKSPACE.attachment(pid,name),'application/octet-stream',
                        {'Content-Disposition':'attachment; filename="'+Path(name).name+'"'})
                if u.path=='/api/workspace/replay':
                    rid=q.get('run',[''])[0]
                    if rid=='historical':
                        idx=json.loads(WORKSPACE.attachment(pid,'r2/replay/index.json'))
                        idx['base']=str(WORKSPACE.path(pid)/'attachments/r2/replay')
                        idx['provenance']='HISTORICAL_NOT_REEXECUTED'
                    else:
                        if not re.fullmatch('[a-f0-9]{32}',rid):raise ValueError('Invalid run id')
                        folder=WORKSPACE.path(pid)/'runs'/rid/'replay'
                        idx=json.loads((folder/'index.json').read_text(encoding='utf-8'));idx['base']=str(folder)
                    return self._json(idx)
                if u.path=='/api/workspace/verify':
                    from implementation.workspace.evidence import verify
                    return self._json(verify(WORKSPACE,pid))
                if u.path=='/api/workspace/hardware':
                    from implementation.hardware.__main__ import capabilities as hardware_capabilities
                    return self._json(hardware_capabilities())
                if u.path == '/api/workspace/projects':
                    return self._json(WORKSPACE.list())
                if u.path == '/api/workspace/capabilities':
                    return self._json(workspace_capabilities())
                if u.path == '/api/workspace/project':
                    p=WORKSPACE.get(pid)
                    from implementation.workspace.tools import available
                    c={};p['tool_input_warnings']=[]
                    if 'controller.json' in p['assets']:
                        try:
                            c=json.loads(WORKSPACE.asset(pid,'controller.json'))
                            if not isinstance(c,dict):raise ValueError('Controller must be an object')
                        except (ValueError,TypeError):
                            c={};p['tool_input_warnings'].append('controller.json must be a valid JSON object before a robot simulation adapter can be selected. Your file remains editable.')
                    p['tools']=available(p,c)
                    from implementation.workspace.code_workspace import inspect as code_inspect
                    p['code_workspace']=code_inspect(WORKSPACE,pid)
                    if 'design.json' in p['assets'] and 'controller.json' in p['assets']:
                        if c.get('kind')=='bird-mission/1':
                            from implementation.workspace.bird import coverage
                            p['coverage']=coverage(WORKSPACE,pid)
                    return self._json(p)
                if u.path == '/api/workspace/asset':
                    return self._json({'text': WORKSPACE.asset(pid, q.get('name', [''])[0], q.get('revision', [None])[0])})
                if u.path == '/api/workspace/export':
                    return self._send(200, WORKSPACE.export(pid), 'application/zip',
                                      {'Content-Disposition': 'attachment; filename="robot-project.zip"'})
                if u.path == '/api/workspace/run-file':
                    rid = q.get('run', [''])[0]
                    if not re.fullmatch('[a-f0-9]{32}', rid):
                        raise ValueError('Invalid run id')
                    name = q.get('name', [''])[0]
                    record=next((r for r in WORKSPACE.get(pid)['runs'] if r['id']==rid),None)
                    if record is None or (name not in record.get('outputs',{}) and name not in ('stdout.log','stderr.log')):
                        raise ValueError('Unsupported run file')
                    f = WORKSPACE.path(pid) / 'runs' / rid
                    f = f / 'execution' / name if name in ('stdout.log','stderr.log') else f / name
                    if f.is_symlink():
                        raise ValueError('Symlinks refused')
                    raw=f.read_bytes()
                    expected=record.get('outputs',{}).get(name)
                    if expected and hashlib.sha256(raw).hexdigest()!=expected:raise ValueError('Run artifact changed after recording')
                    return self._send(200,raw, 'model/gltf-binary' if f.suffix=='.glb' else 'application/json' if f.suffix=='.json' else 'application/octet-stream' if f.suffix in ('.step','.stl','.gz') else 'text/plain; charset=utf-8')
            except (OSError, ValueError, KeyError) as e:
                return self._err(400, str(e))
        if u.path == "/api/capabilities":
            return self._json(capabilities())
        if u.path == "/api/system":
            d = dict(SYS.detect(DATA, refresh=q.get("refresh", ["0"])[0] == "1"))
            ok = cad_ok()
            d["can"] = list(d["can"]) + [{"id": "bird_cad", "ok": ok, "why": None if ok else "no_cad", "fix": None if ok else "install_cad"}]
            return self._json(d)
        if u.path == "/api/settings":
            return self._json(P.public_settings(DATA))
        if u.path == "/api/ai/detect":
            return self._json({"clis": P.detect_ai_clis()})
        if u.path == "/api/examples":
            return self._json({"examples": P.examples(DATA, USER_ASSETS, cad_ok()), "ai_ready": P.ai_ready(DATA)})
        if u.path == "/api/examples/download":
            # read-only files of a built-in example circuit record (netlist, parts list, report)
            run = safe_dir(q.get("dir", [""])[0])
            if run is None or EXAMPLES.resolve() not in run.parents:
                return self._err(404, "not a built-in example")
            files = dict(download_files(run))
            what = q.get("what", [""])[0]
            if what not in ("netlist", "bom", "report"):
                return self._err(404, "nothing to download")
            if what != "report" and not (_json(run / "final" / "report.json") or {}).get("achieved"):
                return self._err(404, "this example did not pass; only its report is offered")
            if what not in files:
                return self._err(404, "this example has no " + what)
            stem = f"rlsok-example-{run.name}"
            if what == "netlist":
                body, name, ctype = files[what].read_bytes(), f"{stem}-netlist.cir", "text/plain; charset=utf-8"
            elif what == "bom":
                body, name, ctype = bom_csv(_json(files[what]) or {}), f"{stem}-bom.csv", "text/csv; charset=utf-8"
            else:
                en = files[what].with_name("report.en.html")
                p = en if q.get("lang", [""])[0] == "en" and en.is_file() else files[what]
                body, name, ctype = p.read_bytes(), f"{stem}-report.html", "text/html; charset=utf-8"
            return self._send(200, body, ctype, {"Content-Disposition": f'attachment; filename="{name}"'})
        if u.path == "/api/duck-training":
            return self._json(P.duck_training_record() or {})
        if u.path == "/api/evidence":
            return self._json(evidence_index())
        if u.path == "/api/jobs":
            return self._json(sorted(JOBS.jobs.values(), key=lambda j: -j["started"]))
        if u.path.startswith("/api/jobs/"):
            parts = u.path.split("/")          # ['', 'api', 'jobs', <id>, (timeline|wave)?]
            jid = parts[3] if len(parts) > 3 else ""
            if jid not in JOBS.jobs:
                return self._err(404, "no such job")
            j = JOBS.jobs[jid]
            sub = parts[4] if len(parts) > 4 else ""
            if sub == "timeline":
                if j["pipeline"] in LOOPS:
                    return self._json(circuit_timeline(Path(j["run_dir"])))
                if j["pipeline"] == "train_measure":
                    return self._json(train_progress(j["started"]) or {"kind": "train"})
                return self._json({"kind": "robot", "result": robot_run_summary(Path(j["run_dir"]))})
            if sub == "download" and j["pipeline"] in LOOPS:
                run = Path(j["run_dir"])
                files = dict(download_files(run))
                what = q.get("what", [""])[0]
                if what not in files:
                    return self._err(404, "nothing to download yet")
                stem = f"rlsok-{jid}"
                if what == "netlist":
                    body, name, ctype = files[what].read_bytes(), f"{stem}-netlist.cir", "text/plain; charset=utf-8"
                elif what == "bom":
                    body, name, ctype = bom_csv(_json(files[what]) or {}), f"{stem}-bom.csv", "text/csv; charset=utf-8"
                elif what == "report":
                    en = files[what].with_name("report.en.html")          # circuit runs since 0.2.0 write both languages
                    p = en if q.get("lang", [""])[0] == "en" and en.is_file() else files[what]
                    body, name, ctype = p.read_bytes(), f"{stem}-report.html", "text/html; charset=utf-8"
                elif what == "stl":
                    body, name, ctype = bundle_zip(files[what]), f"{stem}-printed-parts.zip", "application/zip"
                elif what == "params":
                    body, name, ctype = files[what].read_bytes(), f"{stem}-flight-params.json", "application/json"
                else:
                    body, name, ctype = bundle_zip(run), f"{stem}-all.zip", "application/zip"
                return self._send(200, body, ctype, {"Content-Disposition": f'attachment; filename="{name}"'})
            if sub == "replay":
                r = q.get("round", [""])[0]
                idx = replay_index(Path(j["run_dir"]), int(r) if r.isdigit() else None)
                if idx is None:
                    return self._err(404, "no simulation replay yet")
                idx["job_state"] = j["state"]
                return self._json(idx)
            if sub == "wave":
                try:
                    rnd = int(q.get("round", ["1"])[0])
                except ValueError:
                    return self._err(400, "bad round")
                w = circuit_wave(Path(j["run_dir"]), rnd) if j["pipeline"] in LOOPS else None
                return self._json(w) if w else self._err(404, "no waveform for this round")
            return self._json(JOBS.view(jid))
        if u.path == "/api/run/replay":
            run = safe_dir(q.get("dir", [""])[0])
            r = q.get("round", [""])[0]
            idx = replay_index(run, int(r) if r.isdigit() else None) if run else None
            return self._json(idx) if idx else self._err(404, "no simulation replay in this record")
        if u.path in ("/api/run/timeline", "/api/run/wave"):
            # read-only view of a past circuit run (library), restricted to the evidence folders
            run = safe_dir(q.get("dir", [""])[0])
            if run is None or not (run / "rounds").exists():
                return self._err(404, "not a circuit run inside the evidence folders")
            if u.path.endswith("timeline"):
                return self._json(circuit_timeline(run))
            try:
                w = circuit_wave(run, int(q.get("round", ["1"])[0]))
            except ValueError:
                return self._err(400, "bad round")
            return self._json(w) if w else self._err(404, "no waveform for this round")
        if u.path == "/api/route":
            return self._json(route(q.get("s", [""])[0]))
        if u.path == "/file":
            p = safe_path(q.get("path", [""])[0])
            if p is None:
                return self._err(404, "not found or outside the evidence folders")
            ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype == "application/json":
                ctype += "; charset=utf-8"
            return self._send(200, p.read_bytes(), ctype)
        rel = "workspace.html" if u.path in ("/", "") else "index.html" if u.path == '/tools.html' else u.path.lstrip("/")
        f = (STATIC / rel).resolve()
        if STATIC.resolve() not in f.parents or not f.is_file():
            return self._err(404, "not found")
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self._send(200, f.read_bytes(), ctype)

    def do_POST(self):  # noqa: N802
        if not self._local_only():
            return
        # CSRF guard: another website open in the browser could otherwise POST to 127.0.0.1.
        # A custom header forces a CORS preflight that this server never approves; Origin must be ours.
        origin = self.headers.get("Origin")
        port = self.server.server_address[1]
        if self.headers.get("X-RLSOK") != "1" or (
                origin and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")):
            # Draining a small denied POST prevents Windows from resetting the socket
            # before the client receives its 403. No denied action is executed.
            try:
                denied_length=int(self.headers.get('Content-Length') or 0)
                if 0<denied_length<=4096:
                    self.connection.settimeout(1)
                    self.rfile.read(denied_length)
            except (ValueError,OSError):pass
            self.close_connection=True
            return self._err(HTTPStatus.FORBIDDEN, "cross-site request refused")
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_UPLOAD + 1024:
            return self._err(413, "too large")
        body = self.rfile.read(n)
        u = urlparse(self.path)
        try:
            if u.path == "/api/upload":
                q = parse_qs(u.query)
                return self._json(save_upload(q.get("name", ["upload.bin"])[0], q.get("kind", ["other"])[0], body,
                                              q.get("note", [""])[0]))
            data = json.loads(body or b"{}")
            if u.path.startswith('/api/workspace/'):
                pid = data.get('project', '')
                if u.path=='/api/workspace/play-start':return self._json(PLAY.start(pid,data.get('seconds',24)))
                if u.path=='/api/workspace/play-control':return self._json(PLAY.control(pid,data['session'],data['seq'],data['command']))
                if u.path=='/api/workspace/play-stop':return self._json(PLAY.stop(pid,data['session']))
                if u.path.startswith('/api/workspace/hub-'):
                    from implementation.workspace.hub import Hub
                    hub=Hub(DATA,WORKSPACE)
                    try:
                        if u.path=='/api/workspace/hub-remix':return self._json(hub.create(data.get('item'),data.get('parameters')))
                        if u.path=='/api/workspace/hub-use-asset':return self._json(hub.use_asset(data.get('item'),pid,data['expected']))
                        if u.path=='/api/workspace/hub-experiment':return self._json({'revision':hub.experiment(pid,data['expected'],data['parameters'])})
                        if u.path in ('/api/workspace/hub-review','/api/workspace/hub-publish'):
                            fields={k:data.get(k) for k in ('expected','title','description','author','license','assets')};fields['pid']=pid
                            if u.path.endswith('review'):return self._json(hub.review(**fields)[0])
                            return self._json(hub.publish(**fields,rights_confirmed=data.get('rights_confirmed'),review=data.get('review')))
                        if u.path=='/api/workspace/hub-import':return self._json(hub.import_package(base64.b64decode(data['archive'],validate=True)))
                    except PermissionError as error:return self._err(403,str(error))
                if u.path=='/api/workspace/official-login':
                    from implementation.workspace.official_client import login
                    return self._json(login())
                if u.path=='/api/workspace/official-task':
                    from implementation.workspace.official_tasks import start
                    return self._json(start(DATA,WORKSPACE,pid,data['connection'],data.get('goal'),resume=data.get('resume',True)))
                if u.path=='/api/workspace/inspect-design':
                    return self._json(ROBOT_RUNTIME.submit_design(pid,data.get('source_run')))
                if u.path=='/api/workspace/official-open':
                    from implementation.workspace.official_client import open_client
                    return self._json(open_client(DATA,WORKSPACE,pid,data['connection'],resume=data.get('resume',False),goal=data.get('goal')))
                if u.path=='/api/workspace/session':
                    from implementation.workspace.workspace_session import session
                    return self._json(session(DATA,WORKSPACE,data))
                if u.path=='/api/workspace/ui-preferences':
                    from implementation.workspace.ui_preferences import preferences
                    if set(data)!={'language'}:raise ValueError('Choose a software language')
                    return self._json(preferences(DATA,data['language']))
                if u.path=='/api/workspace/mcp-connect':
                    from implementation.workspace.mcp_access import grant
                    return self._json(grant(DATA,WORKSPACE,pid,data.get('permissions',['read_asset']),
                        'http://127.0.0.1:'+str(self.server.server_address[1]),SYS._dpapi,remember=data.get('remember',False),client=data.get('client','codex'),daily_limits=data.get('daily_limits'),enable_trusted_code=data.get('enable_trusted_code',False),expected_revision=data.get('expected_revision')))
                if u.path=='/api/workspace/code-environment':
                    from implementation.workspace.code_workspace import approve
                    return self._json(approve(WORKSPACE,pid,data['expected'],data.get('confirm_trusted_code'),data.get('python'),data.get('python_paths'),data.get('limits')))
                if u.path=='/api/workspace/run-code':
                    return self._json(ROBOT_RUNTIME.submit_code(pid,data['tool'],data.get('arguments','[]')))
                if u.path=='/api/workspace/mcp-revoke':
                    from implementation.workspace.mcp_access import revoke
                    return self._json(revoke(DATA,data['connection'],WORKSPACE,ROBOT_RUNTIME))
                if u.path=='/api/workspace/mcp-refresh':
                    from implementation.workspace.mcp_access import refresh
                    try:return self._json(refresh(DATA,WORKSPACE,data.get('connection'),self.headers.get('X-RLSOK-Refresh',''),SYS._dpapi))
                    except PermissionError as error:return self._err(403,str(error))
                if u.path=='/api/workspace/mcp-call':
                    from implementation.workspace.mcp_access import execute
                    try:return self._json(execute(DATA,WORKSPACE,ROBOT_RUNTIME,data.get('connection'),self.headers.get('X-RLSOK-MCP',''),data.get('tool'),data.get('arguments',{})))
                    except PermissionError as error:return self._err(403,str(error))
                if u.path=='/api/workspace/restore':
                    return self._json({'revision':WORKSPACE.restore(pid,data['revision'],data['expected'])})
                if u.path=='/api/workspace/agent-cancel':
                    sid=data.get('session','')
                    if not re.fullmatch('[a-f0-9]{32}',sid):raise ValueError('Invalid session')
                    path=WORKSPACE.path(pid)/'agent/sessions'/(sid+'.json')
                    if not path.is_file():raise ValueError('Unknown session')
                    path.with_suffix('.cancel').write_text('User requested stop',encoding='utf-8')
                    for run in WORKSPACE.get(pid)['runs']:
                        if run['state'] in ('QUEUED','RUNNING'):ROBOT_RUNTIME.cancel(pid,run['id'])
                    return self._json({'state':'CANCEL_REQUESTED','note':'Current model request can finish before stopping; no subsequent actions will start'})
                if u.path == '/api/workspace/create':
                    if data.get('bird_reference'):
                        from implementation.workspace.bird import create as create_bird
                        return self._json(create_bird(WORKSPACE))
                    if data.get('mobile_reference'):
                        from implementation.workspace.mobile import create as create_mobile
                        return self._json(create_mobile(WORKSPACE))
                    if data.get('component_reference'):
                        from implementation.workspace.components import create as create_components
                        return self._json(create_components(WORKSPACE))
                    if data.get('reference'):
                        return self._json(create_reference(WORKSPACE))
                    return self._json(WORKSPACE.create(data.get('name', ''), data.get('intent', ''),
                                      data.get('morphology', 'custom'), data.get('assets')))
                if u.path == '/api/workspace/save':
                    return self._json({'revision': WORKSPACE.revise(pid, data['assets'], data.get('reason', 'User edit'), data['expected'])})
                if u.path == '/api/workspace/build':
                    from implementation.workspace.build import save_design
                    for option in ('confirm_criteria','overwrite_native'):
                        if type(data.get(option,False)) is not bool:raise ValueError('Boolean builder review options required')
                    return self._json(save_design(WORKSPACE,pid,data['design'],data['expected'],data.get('confirm_criteria',False),data.get('overwrite_native',False)))
                if u.path == '/api/workspace/import':
                    # Import never executes model-provided Python or inherits validation passes.
                    from implementation.workspace.hub import Hub
                    try:return self._json(Hub(DATA,WORKSPACE).import_project(base64.b64decode(data['archive'], validate=True)))
                    except PermissionError as error:return self._err(403,str(error))
                if u.path == '/api/workspace/build-brief':
                    from implementation.workspace.build import prepare_brief
                    if data.get('confirm_criteria') is not True:raise ValueError('Confirm owner targets and acceptance criteria before preparing an AI brief')
                    return self._json(prepare_brief(WORKSPACE,pid,data['criteria'],data['target_rad'],data['expected'],data.get('schema','rlsok.primitive-builder/1')))
                if u.path == '/api/workspace/fork':
                    return self._json(WORKSPACE.fork(pid, data.get('name', 'Robot fork')))
                if u.path == '/api/workspace/run':
                    return self._json(ROBOT_RUNTIME.submit(pid,data.get('adapter')))
                if u.path == '/api/workspace/motion-brief':
                    if data.get('confirm_criteria') is not True:raise ValueError('Explicit owner confirmation required')
                    from implementation.workspace.motion_authoring import prepare
                    return self._json(prepare(WORKSPACE,pid,data['criteria'],data['joint_count'],data['support_count'],data['expected']))
                if u.path == '/api/workspace/system-brief':
                    if data.get('confirm_criteria') is not True:raise ValueError('Explicit owner confirmation required')
                    from implementation.workspace.system_authoring import prepare
                    return self._json(prepare(WORKSPACE,pid,data['criteria'],data['position_count'],data['velocity_count'],data['expected']))
                if u.path == '/api/workspace/enable-tool':
                    from implementation.workspace import cad,electrical
                    kind=data.get('tool')
                    if kind not in ('cad','electrical'):raise ValueError('Unknown tool template')
                    assets=(cad if kind=='cad' else electrical).defaults()
                    p=WORKSPACE.get(pid)
                    if set(assets)&set(p['assets']):raise ValueError('Tool assets already present; originals will not be overwritten')
                    WORKSPACE.revise(pid,assets,'User enabled reference '+kind+' engineering module',data['expected'])
                    return self._json(WORKSPACE.get(pid))
                if u.path == '/api/workspace/cancel':
                    return self._json(ROBOT_RUNTIME.cancel(pid, data['run']))
                agent = EngineeringAgent(WORKSPACE, ROBOT_RUNTIME)
                if u.path == '/api/workspace/prompt':
                    return self._json({'prompt': agent.prompt(pid, data.get('goal', ''))})
                if u.path == '/api/workspace/respond':
                    return self._json(agent.apply(pid, data['reply'], data.get('permissions', ['read_asset'])))
                if u.path == '/api/workspace/agent':
                    settings=P.load_settings(DATA)['ai']
                    from implementation.workspace.providers import configured_provider
                    provider=configured_provider(settings,SYS.load_secrets(DATA).get(settings.get('provider') or '',''))
                    return self._json(agent.iterate(pid,data.get('goal',''),provider,
                                      data.get('permissions',['read_asset']),data.get('turns',6)))
            if u.path == "/api/start":
                pipeline = data.get("pipeline") or route(data.get("sentence", "")).get("pipeline")
                if not pipeline:
                    return self._err(400, "no pipeline matched; choose one")
                if pipeline in LOOPS and len(data.get("sentence", "").strip()) < 4:
                    return self._err(400, "describe the circuit requirement")
                if pipeline == "bird" and not cad_ok():      # would fail minutes later inside the CAD step
                    return self._json({"blocked": "install_cad"})
                return self._json(JOBS.start(pipeline, data.get("sentence", ""), data.get("opts") or {}))
            if u.path == "/api/respond":
                return self._json(JOBS.resume_circuit(data["job"], data["text"]))
            if u.path == "/api/continue-with-ai":
                return self._json(JOBS.resume_with_agent(data["job"]))
            if u.path == "/api/settings":
                return self._json(P.save_settings(DATA, data) and P.public_settings(DATA))
            if u.path == "/api/secret":
                name = data.get("name")
                if name not in ("anthropic", "openai", "local"):
                    return self._err(400, "unknown secret")
                return self._json(SYS.save_secret(DATA, name, (data.get("value") or "").strip()))
            if u.path == "/api/ai/test":
                return self._json(P.test_ai(DATA))
            if u.path == "/api/cloud/test":
                return self._json(P.test_cloud(DATA))
            if u.path == "/api/examples/run":
                ex = {e["id"]: e for e in P.examples(DATA, USER_ASSETS, cad_ok())}.get(data.get("id"))
                if not ex:
                    return self._err(404, "no such example")
                if "duck_assets" in ex["needs"]:
                    return self._json({"blocked": "duck_assets"})
                if "ai" in ex["needs"] and not P.ai_ready(DATA):
                    return self._json({"blocked": "no_ai"})
                if "cad" in ex["needs"]:
                    return self._json({"blocked": "install_cad"})
                r = ex["run"]
                return self._json(JOBS.start(r["pipeline"], r.get("sentence", ""), r.get("opts") or {}))
            if u.path == "/api/install":
                what = data.get("what")
                if what not in ("install_train", "install_mjlab", "install_cad", "install_wsl", "duck_assets", "train_measure", "train_full"):
                    return self._err(400, "unknown installer")
                return self._json(JOBS.start(what, "", {}))
            m = re.fullmatch(r"/api/jobs/([A-Za-z0-9-]+)/(stop|note|continue)", u.path)
            if m:
                jid, act = m.groups()
                if jid not in JOBS.jobs:
                    return self._err(404, "no such job")
                if act == "stop":
                    return self._json(JOBS.stop(jid))
                if act == "note":
                    return self._json(JOBS.add_note(jid, data.get("text", "")))
                return self._json(JOBS.continue_from(jid, data.get("text", "")))
            if u.path == "/api/open":
                return self._json({"ok": SYS.open_settings(data.get("what", ""))})
            if u.path == "/api/shutdown":
                threading.Thread(target=lambda: (time.sleep(0.5), os._exit(0)), daemon=True).start()
                return self._json({"ok": True})
        except (ValueError, KeyError, RuntimeError, OSError, zipfile.BadZipFile) as e:
            return self._err(400, str(e))
        self._err(404, "not found")


def make_server(port: int = 8765) -> ThreadingHTTPServer:
    global JOBS, WORKSPACE, ROBOT_RUNTIME, PLAY
    JOBS = Jobs()
    WORKSPACE = Projects(DATA / 'robot_projects')
    ROBOT_RUNTIME = RobotRuntime(WORKSPACE)
    from implementation.workspace.official_tasks import recover as recover_official_tasks
    recover_official_tasks(DATA,WORKSPACE)
    from implementation.workspace.play import Play
    PLAY=Play(WORKSPACE)
    class Server(ThreadingHTTPServer):
        def server_close(self):
            PLAY.close()
            ROBOT_RUNTIME.close()
            super().server_close()
    return Server(("127.0.0.1", port), Handler)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    srv = make_server(a.port)
    print(f"RLSOK console: http://127.0.0.1:{a.port}/  (Ctrl+C to stop)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
