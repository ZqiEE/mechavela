"""Refuse to commit secrets or large files. Runs from .githooks/pre-commit and by hand.

    python3 tools/scan_secrets.py            # files staged for commit
    python3 tools/scan_secrets.py --all      # every tracked + untracked (not ignored) file

Exit 1 with a list of findings; nothing is printed of the matched secret itself beyond 6 characters.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

MAX_BYTES = 10 * 1024 * 1024          # larger files belong in GitHub Releases
PATTERNS = {
    "private key block": re.compile(rb"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),
    "GitHub token": re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    "AWS access key": re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    "OpenAI/Anthropic-style key": re.compile(rb"\bsk-(?:ant-)?[A-Za-z0-9_\-]{24,}"),
    "Google API key": re.compile(rb"\bAIza[0-9A-Za-z_\-]{35}\b"),
    "Slack token": re.compile(rb"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    "JWT": re.compile(rb"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    "assigned password/secret/key": re.compile(
        rb"""(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)\b\s*[:=]\s*["'][^"'\s]{8,}["']"""),
    "URL with credentials": re.compile(rb"\b[a-z][a-z0-9+.\-]*://[^/\s:@]+:[^/\s@]{6,}@"),
}
BAD_NAMES = re.compile(r"(?i)(^|/)(\.env(\..*)?|id_(rsa|ecdsa|ed25519).*|.*\.(pem|key|p12|pfx|kdbx)|.*_ed25519(\.pub)?|known_hosts.*)$")


def files(all_files: bool) -> list[str]:
    if all_files:
        out = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], capture_output=True, check=True).stdout
    else:
        out = subprocess.run(["git", "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR"], capture_output=True, check=True).stdout
    return [f for f in out.decode().split("\0") if f]


def main() -> int:
    findings = []
    for f in files("--all" in sys.argv):
        p = Path(f)
        if not p.is_file():
            continue
        if BAD_NAMES.search(f):
            findings.append((f, "file name looks like a key or secret file"))
            continue
        size = p.stat().st_size
        if size > MAX_BYTES:
            findings.append((f, f"{size / 1e6:.1f} MB > 10 MB: publish it as a GitHub Release asset"))
            continue
        data = p.read_bytes()
        for name, rx in PATTERNS.items():
            m = rx.search(data)
            if m:
                line = data.count(b"\n", 0, m.start()) + 1
                findings.append((f, f"{name} at line {line} ({m.group(0)[:6].decode(errors='replace')}...)"))
    if findings:
        print("SECRET/SIZE SCAN FAILED:")
        for f, why in findings:
            print(f"  {f}: {why}")
        return 1
    print("secret/size scan: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
