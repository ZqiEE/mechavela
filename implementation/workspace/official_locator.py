"""Find the existing official CLI; no installation, login or credential inspection."""
import os,shutil,subprocess
from pathlib import Path
_FOUND=None

def codex():
    global _FOUND
    path=shutil.which('codex')
    if path:return path
    if _FOUND and Path(_FOUND).is_file():return _FOUND
    if os.name!='nt':return None
    shell=Path(os.environ.get('SYSTEMROOT','C:/Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
    # Windows package metadata, not a recursive scan of user data or auth files.
    command="[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); Get-AppxPackage -Name OpenAI.Codex | Sort-Object Version -Descending | Select-Object -First 1 -ExpandProperty InstallLocation"
    try:
        r=subprocess.run([str(shell),'-NoProfile','-NonInteractive','-Command',command],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,encoding='utf-8',timeout=10,creationflags=0x08000000)
        location=r.stdout.strip()
        if r.returncode or not location or '\n' in location:return None
        candidate=Path(location)/'app/resources/codex.exe'
        if candidate.is_absolute() and candidate.is_file():_FOUND=str(candidate);return _FOUND
    except (OSError,ValueError,subprocess.TimeoutExpired):pass
    return None
