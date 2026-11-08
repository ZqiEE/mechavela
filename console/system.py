"""Local machine facts for the console: hardware detection, what this PC can do, local-only secrets,
desktop notifications and plain-language error explanations. Standard library only."""

from __future__ import annotations

import ctypes
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

WIN = os.name == "nt"
NO_WINDOW = 0x08000000 if WIN else 0
DISTRO = "Ubuntu-22.04"


def _run(argv, timeout=20, utf16=False) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as e:
        return -1, str(e)
    raw = p.stdout + p.stderr
    for enc in (("utf-16-le", "utf-8") if utf16 else ("utf-8", "gbk")):
        try:
            return p.returncode, raw.decode(enc).replace("\x00", "")
        except UnicodeDecodeError:
            continue
    return p.returncode, raw.decode("utf-8", "replace")


# ----------------------------------------------------------------------------- hardware
def memory_gb() -> float | None:
    if WIN:
        class MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = MS(); m.dwLength = ctypes.sizeof(MS)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            return round(m.ullTotalPhys / 2**30, 1)
        return None
    try:
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30, 1)
    except (ValueError, OSError, AttributeError):
        return None


def gpu() -> dict:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return {"present": False}
    rc, out = _run([exe, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"])
    if rc != 0 or not out.strip():
        return {"present": False, "error": out.strip()[:200]}
    name, mem, drv = [x.strip() for x in out.strip().splitlines()[0].split(",")[:3]]
    return {"present": True, "name": name, "vram_gb": round(int(float(mem)) / 1024, 1), "driver": drv}


def wsl() -> dict:
    if not WIN or not shutil.which("wsl"):
        return {"available": False, "reason": "no_wsl"}
    rc, out = _run(["wsl", "-l", "-q"], utf16=True)
    distros = [x.strip() for x in out.splitlines() if x.strip()] if rc == 0 else []
    info = {"available": rc == 0, "distros": distros, "has_ubuntu": DISTRO in distros}
    if info["has_ubuntu"]:
        rc2, out2 = _run(["wsl", "-d", DISTRO, "-u", "root", "--", "bash", "-lc",
                          "test -x ~/rlsok/venvs/train/bin/python && echo DUCK; test -x ~/rlsok/venvs/mjlab/bin/python && echo MJLAB; true"],
                         timeout=60)
        info["train_env"] = "DUCK" in out2
        info["mjlab_env"] = "MJLAB" in out2
    return info


def cpu_name() -> str:
    if WIN:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                return winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
        except OSError:
            pass
    return platform.processor() or platform.machine()


def smart_app_control() -> str:
    """Windows 11 Smart App Control: 'on' | 'evaluation' | 'off' | 'unknown'. It blocks unsigned DLLs (seen 2026-10-21)."""
    if not WIN:
        return "off"
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\CI\Policy") as k:
            v = winreg.QueryValueEx(k, "VerifiedAndReputablePolicyState")[0]
        return {0: "off", 1: "on", 2: "evaluation"}.get(int(v), "unknown")
    except OSError:
        return "unknown"


_cache: dict = {}


def detect(data_dir: Path, refresh: bool = False) -> dict:
    if _cache.get("t") and not refresh and time.time() - _cache["t"] < 120:
        return _cache["v"]
    du = shutil.disk_usage(data_dir if data_dir.exists() else Path.home())
    v = {"os": f"{platform.system()} {platform.release()}", "machine": platform.machine(),
         "cpu": cpu_name(), "cores": os.cpu_count(),
         "ram_gb": memory_gb(), "disk_free_gb": round(du.free / 2**30, 1), "gpu": gpu(), "wsl": wsl(),
         "smart_app_control": smart_app_control(), "signed": SIGNED}
    v["can"] = capabilities(v)
    _cache.update(t=time.time(), v=v)
    return v


def capabilities(d: dict) -> list[dict]:
    """What this PC can and cannot do, each with the reason and the one-click fix (if any)."""
    g, w = d["gpu"], d["wsl"]
    out = [
        {"id": "circuit", "ok": True, "need": "cpu"},
        {"id": "robot_bench", "ok": (d["ram_gb"] or 8) >= 4, "need": "cpu", "why": None if (d["ram_gb"] or 8) >= 4 else "ram"},
    ]
    if not g.get("present"):
        out.append({"id": "train_local", "ok": False, "why": "no_gpu", "fix": "cloud"})
    elif g["vram_gb"] < 3.5:
        out.append({"id": "train_local", "ok": False, "why": "vram_small", "fix": "cloud"})
    elif not w.get("available") or not w.get("has_ubuntu"):
        out.append({"id": "train_local", "ok": False, "why": "no_wsl", "fix": "install_wsl"})
    elif not w.get("train_env"):
        out.append({"id": "train_local", "ok": False, "why": "no_train_env", "fix": "install_train"})
    else:
        out.append({"id": "train_local", "ok": True, "note": "duck_ok"})
    # G1 (unitree_rl_mjlab, 4096 envs) measured on a GTX 1650 Ti 4 GB: peak 2.7 GB, ~11.6k steps/s
    # (usage/g1-bench-1650ti-20261021/summary.json); a 4 GB card is enough, it just takes about a day.
    if not g.get("present") or g["vram_gb"] < 3.5:
        out.append({"id": "train_humanoid", "ok": False, "why": "vram_humanoid", "fix": "cloud"})
    elif not w.get("available") or not w.get("has_ubuntu"):
        out.append({"id": "train_humanoid", "ok": False, "why": "no_wsl", "fix": "install_wsl"})
    elif not w.get("mjlab_env"):
        out.append({"id": "train_humanoid", "ok": False, "why": "no_mjlab_env", "fix": "install_mjlab"})
    else:
        out.append({"id": "train_humanoid", "ok": True})
    if (d["disk_free_gb"] or 0) < 15:
        out.append({"id": "disk", "ok": False, "why": "disk_low"})
    if d.get("smart_app_control") in ("on", "evaluation") and not d.get("signed"):
        # put it first: it decides whether anything that loads numpy/MuJoCo can run at all
        out.insert(0, {"id": "sac", "ok": False, "why": "sac_on", "fix": "sac_help"})
        for c in out:   # robot features load unsigned numpy/MuJoCo DLLs: blocked on this PC on 2026-10-21
            if c["id"] in ("robot_bench", "train_local", "train_humanoid") and c.get("ok"):
                c.update(ok=False, why="sac_robot", fix="sac_help")
    return out


# This build is not code-signed yet (release/CODE_SIGNING.md); flip when every binary is signed.
SIGNED = False


# ----------------------------------------------------------------------------- secrets (local only)
class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in, blob_out = _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), _BLOB()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(blob_in), None, None, None, None, 0x1, ctypes.byref(blob_out))   # 0x1 = UI_FORBIDDEN
    if not ok:
        raise OSError("DPAPI failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def secrets_path(data_dir: Path) -> Path:
    return data_dir / "secrets.bin"


def save_secret(data_dir: Path, name: str, value: str) -> dict:
    cur = load_secrets(data_dir)
    if value:
        cur[name] = value
    else:
        cur.pop(name, None)
    raw = json.dumps(cur).encode()
    p = secrets_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    if WIN:
        p.write_bytes(b"DPAPI1" + _dpapi(raw, True))
        how = "Windows DPAPI（只有当前 Windows 用户能解密）"
    else:
        p.write_bytes(b"PLAIN1" + raw)
        os.chmod(p, 0o600)
        how = "file mode 600"
    return {"stored_at": str(p), "protection": how}


def load_secrets(data_dir: Path) -> dict:
    p = secrets_path(data_dir)
    if not p.exists():
        return {}
    b = p.read_bytes()
    try:
        if b.startswith(b"DPAPI1"):
            return json.loads(_dpapi(b[6:], False))
        if b.startswith(b"PLAIN1"):
            return json.loads(b[6:])
    except (OSError, ValueError):
        return {}
    return {}


# ----------------------------------------------------------------------------- notifications
def notify(title: str, body: str) -> None:
    """Windows toast (no extra modules); silently ignored elsewhere or on failure."""
    if not WIN:
        return
    t = title.replace("'", "''")[:80]
    b = body.replace("'", "''")[:200]
    ps = ("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
          "$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
          f"$t = $x.GetElementsByTagName('text'); $t.Item(0).AppendChild($x.CreateTextNode('{t}')) > $null; $t.Item(1).AppendChild($x.CreateTextNode('{b}')) > $null;"
          "$n = [Windows.UI.Notifications.ToastNotification]::new($x);"
          "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('RLSOK').Show($n)")
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps], creationflags=NO_WINDOW,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


# ----------------------------------------------------------------------------- plain-language errors
# Each rule: (regex on the job log, code). The UI renders what / why / how-to-fix and the action button per code.
ERROR_RULES = [
    (r"应用程序控制策略已阻止|Application Control policy has blocked|DLL load failed while importing|"
     r"0x800711C7|did not meet the Enterprise signing level", "blocked_by_windows"),
    (r"MemoryError|Unable to allocate|insufficient memory|not enough memory|内存不足|paging file is too small", "ram_low"),
    # before files_missing: these are also "module/file not found", but each has a one-click fix
    (r"No module named 'build123d'|No module named 'OCP'", "cad_missing"),
    # Python prints "No such file or directory: '<path>'", so the path comes after the message
    (r"Open_Duck_Playground source not found|INTERNAL_open_duck_mini_v2.*(No such file|cannot find)|"
     r"(No such file|cannot find)[^\n]*INTERNAL_open_duck_mini_v2", "duck_assets_missing"),
    (r"ModuleNotFoundError|FileNotFoundError|No such file or directory|cannot find the file|系统找不到指定的", "files_missing"),
    (r"NO_JSON_OBJECT_IN_AI_RESPONSE|JSONDecodeError|SPEC_MISSING_FIELDS|SPEC_MUST_BE_OBJECT", "ai_format"),
    (r"ANTHROPIC_API_KEY not set|OPENAI_API_KEY not set|--agent-command required", "no_ai"),
    (r"AGENT_API_ERROR: HTTP Error 401|authentication_error|invalid x-api-key", "ai_key_invalid"),
    (r"AGENT_API_ERROR: HTTP Error 429|rate_limit", "ai_rate_limited"),
    (r"AGENT_API_ERROR: HTTP Error 529|overloaded", "ai_overloaded"),
    (r"AGENT_API_ERROR|AGENT_COMMAND_FAILED|AGENT_COMMAND_NOT_STARTED|getaddrinfo failed|urlopen error", "ai_unreachable"),
    (r"NGSPICE_LIBRARY_NOT_FOUND|Locked Windows Python is unavailable", "solver_missing"),
    (r"CUDA_ERROR_OUT_OF_MEMORY|RESOURCE_EXHAUSTED|out of memory", "gpu_oom"),
    (r"WSL2 is not available|wsl.exe.*not recognized|WslRegisterDistribution failed", "no_wsl"),
    (r"No space left on device|There is not enough space", "disk_full"),
]


def explain(log: str) -> dict:
    """Always returns something the UI can show: a known code, else 'unknown' with the last meaningful log line."""
    log = log or ""
    if re.search(ERROR_RULES[0][0], log, re.I):          # a blocked DLL also produces ImportError noise: check it first
        return {"code": "blocked_by_windows"}
    for rx, code in ERROR_RULES[1:]:
        if re.search(rx, log, re.I):
            return {"code": code}
    lines = [x.strip() for x in log.splitlines() if x.strip() and not x.strip().startswith(("File ", "^", "~", "Traceback"))]
    return {"code": "unknown", "last": lines[-1][:300] if lines else ""}


def open_settings(what: str) -> bool:
    """One-click actions that open a Windows settings page (local machine only)."""
    uri = {"sac_help": "windowsdefender://appbrowser", "storage": "ms-settings:storagesense"}.get(what)
    if not (WIN and uri):
        return False
    try:
        os.startfile(uri)  # noqa: S606 - fixed URIs only
        return True
    except OSError:
        return False
