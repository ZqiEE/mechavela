"""Product layer of the console: settings, connected AI, built-in examples, one-click installers,
training location (this PC / the user's own cloud GPU). Everything runs on the user's computer;
API keys are stored locally only (see system.save_secret)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import system as SYS

ROOT = Path(__file__).resolve().parents[1]
APP_PY = os.environ.get("RLSOK_APP_PYTHON", sys.executable)
EXAMPLES = Path(os.environ.get("RLSOK_EXAMPLES", ROOT / "examples_builtin"))

MODELS = {}  # Users supply exact provider model IDs; availability is not inferred.
DEFAULT_SETTINGS = {
    "onboarded": False,
    "ai": {"provider": None, "model": "", "base_url": "", "command": ""},
    "train": {"where": None, "cloud": {"host": "", "user": "root", "port": 22, "key_path": ""}},
}


# ----------------------------------------------------------------------------- settings
def settings_path(data: Path) -> Path:
    return data / "settings.json"


def load_settings(data: Path) -> dict:
    s = json.loads(json.dumps(DEFAULT_SETTINGS))
    try:
        cur = json.loads(settings_path(data).read_text(encoding="utf-8"))
        for k, v in cur.items():
            if isinstance(v, dict) and isinstance(s.get(k), dict):
                s[k].update(v)
            else:
                s[k] = v
    except (OSError, ValueError):
        pass
    return s


def save_settings(data: Path, patch: dict) -> dict:
    s = load_settings(data)
    for k, v in (patch or {}).items():
        if k in ("ai", "train") and isinstance(v, dict):
            for kk, vv in v.items():
                if kk == "cloud" and isinstance(vv, dict):
                    s["train"]["cloud"].update({x: vv[x] for x in ("host", "user", "port", "key_path") if x in vv})
                elif kk in s[k]:
                    s[k][kk] = vv
        elif k in ("onboarded",):
            s[k] = bool(v)
    data.mkdir(parents=True, exist_ok=True)
    settings_path(data).write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    return s


def public_settings(data: Path) -> dict:
    s = load_settings(data)
    sec = SYS.load_secrets(data)
    s["ai"]["has_key"] = bool(sec.get(s["ai"].get("provider") or "", ""))
    s["secrets_file"] = str(SYS.secrets_path(data))
    s["data_dir"] = str(data)
    return s


def ai_ready(data: Path) -> bool:
    s = load_settings(data)
    p = s["ai"].get("provider")
    if p in ("anthropic", "openai"):
        return bool(SYS.load_secrets(data).get(p))
    if p == "command":
        return bool(s["ai"].get("command"))
    return p == "exchange"


def agent_args(data: Path) -> tuple[list[str], dict]:
    """CLI args + environment for the circuit loop. Keys go through the environment, never argv."""
    s = load_settings(data)
    ai, sec = s["ai"], SYS.load_secrets(data)
    p = ai.get("provider") or "exchange"
    if p == "anthropic":
        if not ai.get('model'):
            raise ValueError('An explicit provider model ID is required')
        return ["--agent", "anthropic", "--model", ai["model"]], {"ANTHROPIC_API_KEY": sec.get("anthropic", "")}
    if p == "openai":
        args = ["--agent", "openai", "--model", ai.get("model") or ""]
        if ai.get("base_url"):
            args += ["--base-url", ai["base_url"]]
        return args, {"OPENAI_API_KEY": sec.get("openai", "")}
    if p == "command":
        return ["--agent", "command", "--agent-command", ai.get("command", "")], {}
    return ["--agent", "exchange"], {}


def test_ai(data: Path) -> dict:
    """One tiny real request with the user's own key (only when the user clicks 'test')."""
    s = load_settings(data)
    ai, sec = s["ai"], SYS.load_secrets(data)
    p = ai.get("provider")
    if p in ("anthropic", "openai") and not sec.get(p):
        return {"ok": False, "code": "ai_key_empty"}       # nothing typed yet: do not call the API
    if p in ('anthropic', 'openai') and not ai.get('model'):
        return {'ok': False, 'code': 'ai_model_missing'}
    if p in ('openai','local'):
        from implementation.workspace.providers import configured_provider,ProviderError
        try:
            provider=configured_provider(ai,sec.get(p,''))
            connection=provider.check_connection(data);result=connection['response']
            return {'ok':True,'scope':'Authenticated API response only; structured tools and engineering NOT_TESTED',
                    'model':result.get('model'),'usage':result.get('usage'),'journal':connection['journal']}
        except ValueError:
            return {'ok':False,'code':'ai_configuration_invalid'}
        except ProviderError as error:
            return {'ok':False,'code':error.code,'http':error.http_status,'journal':getattr(error,'journal',None)}
    try:
        if p == "anthropic":
            req = urllib.request.Request("https://api.anthropic.com/v1/messages", method="POST",
                                         data=json.dumps({"model": ai["model"], "max_tokens": 8,
                                                          "messages": [{"role": "user", "content": "Reply with OK"}]}).encode(),
                                         headers={"content-type": "application/json", "x-api-key": sec.get("anthropic", ""),
                                                  "anthropic-version": "2023-06-01"})
        elif p == "openai":
            base = (ai.get("base_url") or "https://api.openai.com/v1").rstrip("/")
            req = urllib.request.Request(base + "/chat/completions", method="POST",
                                         data=json.dumps({"model": ai.get("model"), "max_tokens": 8,
                                                          "messages": [{"role": "user", "content": "Reply with OK"}]}).encode(),
                                         headers={"content-type": "application/json", "authorization": "Bearer " + sec.get("openai", "")})
        elif p == "command":
            return test_command(ai.get("command") or "")
        elif p == "exchange":
            return {"ok": True}
        else:
            return {"ok": False, "code": "no_ai"}
        with urllib.request.urlopen(req, timeout=30) as r:
            return {"ok": 200 <= r.status < 300}
    except urllib.error.HTTPError as e:
        return {"ok": False, "code": {401: "ai_key_invalid", 403: "ai_key_invalid", 404: "ai_model_missing",
                                      429: "ai_rate_limited", 529: "ai_overloaded"}.get(e.code, "ai_unreachable"), "http": e.code}
    except (OSError, ValueError):
        return {"ok": False, "code": "ai_unreachable"}


# Command-line AIs the setup offers when they are on PATH. Each command reads the prompt on stdin.
AI_CLIS = [("claude", "claude -p --output-format json"), ("codex", "codex exec -")]


def detect_ai_clis() -> list[dict]:
    return [{"id": name, "command": cmd, "path": path} for name, cmd in AI_CLIS if (path := shutil.which(name))]


def test_command(command: str, timeout: int = 120) -> dict:
    """One real round trip through the same CommandAgent the design loops use (only when the user clicks 'test')."""
    import tempfile
    sys.path.insert(0, str(ROOT))
    from implementation.circuit.agents import AgentUnavailable, CommandAgent
    if not command.strip():
        return {"ok": False, "code": "ai_cmd_missing"}
    agent = CommandAgent(command, timeout=timeout)
    if not shutil.which(agent.argv[0]):
        return {"ok": False, "code": "ai_cmd_missing"}
    with tempfile.TemporaryDirectory() as tmp:
        try:
            reply = agent.complete("Reply with the single word OK.", tmp)
        except AgentUnavailable as e:
            return {"ok": False, "code": "ai_cmd_failed", "detail": str(e)[:300]}
    return {"ok": bool(reply.strip()), "code": None if reply.strip() else "ai_cmd_failed", "reply": reply.strip()[:80]}


# ----------------------------------------------------------------------------- cloud GPU (user's own)
def ssh_argv(data: Path, *remote: str) -> list[str]:
    c = load_settings(data)["train"]["cloud"]
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "-o", "StrictHostKeyChecking=accept-new", "-p", str(c.get("port") or 22)]
    if c.get("key_path"):
        argv += ["-i", c["key_path"]]
    return argv + [f"{c.get('user') or 'root'}@{c['host']}", *remote]


def test_cloud(data: Path) -> dict:
    c = load_settings(data)["train"]["cloud"]
    if not c.get("host"):
        return {"ok": False, "code": "cloud_no_host"}
    rc, out = SYS._run(ssh_argv(data, "nvidia-smi --query-gpu=name,memory.total --format=csv,noheader; nproc; free -g | awk 'NR==2{print $2}'"), timeout=60)
    if rc != 0:
        return {"ok": False, "code": "cloud_ssh_failed", "detail": out.strip()[-300:]}
    lines = [x.strip() for x in out.strip().splitlines() if x.strip()]
    has_gpu = bool(lines) and "," in lines[0]
    return {"ok": has_gpu, "code": None if has_gpu else "cloud_no_gpu", "gpu": lines[0] if has_gpu else None,
            "cpus": lines[1] if len(lines) > 1 else None, "ram_gb": lines[2] if len(lines) > 2 else None}


# ----------------------------------------------------------------------------- built-in examples
# The full R2 build package (print files for every part, templates): public GitHub Release, checked against this hash.
BIRD_R2_PACKAGE = {"url": "https://github.com/ZqiEE/rlsok-engineering/releases/download/bird-r2-3/RLSOK-bird-R2-build-package.zip",
                   "bytes": 2992830, "sha256": "69b4b587bc777b2f88e541dad7155a7fe23d8a8d99707eefa012190bd549cfc5"}


def examples(data: Path, user_assets: Path, cad_ok: bool = True) -> list[dict]:
    duck_ok = (user_assets / "INTERNAL_open_duck_mini_v2" / "xmls").exists()
    return [
        {"id": "bird_r2", "kind": "bird", "minutes": 3, "needs": ["ai"] + ([] if cad_ok else ["cad"]),
         "replay": str(EXAMPLES / "bird" / "r2"), "package": {"dir": str(EXAMPLES / "bird" / "r2" / "package"), **BIRD_R2_PACKAGE},
         "run": {"pipeline": "bird", "sentence": "设计一只外形像真鸟（海鸥/鸢的比例）的两翼扑翼机，翼展约 1.4 米，带摄像头和 GPS，手抛起飞，能爬升、平飞、转弯、降落，续航至少 10 分钟"}},
        {"id": "g1", "kind": "robot", "minutes": 1, "needs": [], "replay": str(EXAMPLES / "robot" / "g1_replay"),
         "run": {"pipeline": "robot_bench", "opts": {"robot": "g1_velocity_flat"}}},
        {"id": "buck", "kind": "circuit", "minutes": 8, "needs": ["ai"],
         "replay": str(EXAMPLES / "circuit" / "r1-buck-12v-5v"),
         "run": {"pipeline": "circuit", "sentence": "把 12V 降到 5V，负载电流 500mA"}},
        {"id": "r4", "kind": "circuit", "minutes": 10, "needs": ["ai"],
         "replay": str(EXAMPLES / "circuit" / "r4-boost-impossible"),
         "run": {"pipeline": "circuit", "sentence": "输入 3.3V 电源，给 10Ω 负载输出 12V 直流（约 1.2A），同时输入电源的平均电流不得超过 2A"}},
        {"id": "duck", "kind": "robot", "minutes": 2, "needs": [] if duck_ok else ["duck_assets"],
         "replay": str(EXAMPLES / "robot" / "open_duck_training"),
         "run": {"pipeline": "robot_bench", "opts": {"robot": "open_duck_mini_v2"}}},
    ]


def duck_training_record() -> dict | None:
    d = EXAMPLES / "robot" / "open_duck_training"
    out = {}
    for name in ("attempt1_150m", "attempt2_300m"):
        l2 = d / name / "level2.json"
        if not l2.exists():
            continue
        r = json.loads(l2.read_text(encoding="utf-8"))
        rows = []
        prog = d / name / "progress.jsonl"
        if prog.exists():
            rows = [json.loads(x) for x in prog.read_text(encoding="utf-8").splitlines() if x]
        out[name] = {"label": r["label"], "steps": r["trained_steps"], "checks": r["checks"],
                     "curve": [[x["steps"], x.get("eval/episode_reward")] for x in rows]}
    return out or None
