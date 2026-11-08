"""External AI providers. The platform never designs circuits itself.

All providers take a prompt text and return the AI's raw text. The raw prompt
and response are stored and hashed by the loop.

* exchange  - file hand-off for an AI that operates RLSOK from a terminal
              (Codex, Claude Code, ...). The loop writes prompt.md and stops
              with AWAITING_AI; the AI writes response.md and runs `resume`.
* command   - spawn any CLI (e.g. `codex exec -`, `claude -p`); prompt on stdin.
* anthropic - Anthropic Messages API (ANTHROPIC_API_KEY).
* openai    - any OpenAI-compatible chat endpoint (OPENAI_API_KEY, --base-url).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import urllib.request


class AwaitingAI(Exception):
    def __init__(self, prompt_path, response_path):
        super().__init__("AWAITING_AI: write %s then run resume" % response_path)
        self.prompt_path, self.response_path = str(prompt_path), str(response_path)


class AgentUnavailable(RuntimeError):
    """The external AI could not answer (quota, network, crash). Nothing was decided; resume later."""


class Agent:
    kind = "abstract"
    last_meta = None

    def describe(self) -> dict:
        return {"provider": self.kind}

    def complete(self, prompt: str, exchange_dir: Path) -> str:
        raise NotImplementedError


class ExchangeAgent(Agent):
    kind = "exchange"

    def __init__(self, label="external AI via file exchange"):
        self.label = label

    def describe(self):
        return {"provider": self.kind, "label": self.label,
                "note": "Response authored by whichever external AI read prompt.md; identity is self-declared in response metadata."}

    def complete(self, prompt, exchange_dir):
        exchange_dir = Path(exchange_dir)
        response = exchange_dir / "response.md"
        if response.is_file() and response.stat().st_size > 0:
            return response.read_text(encoding="utf-8-sig")
        raise AwaitingAI(exchange_dir / "prompt.md", response)


class CommandAgent(Agent):
    kind = "command"

    def __init__(self, command, timeout=900):
        self.argv = shlex.split(command, posix=os.name != "nt") if isinstance(command, str) else list(command)
        # npm installs CLIs on Windows as name.cmd, which CreateProcess cannot start by bare name; which() can find it
        if self.argv and os.name == "nt" and not os.path.dirname(self.argv[0]):
            self.argv[0] = shutil.which(self.argv[0]) or self.argv[0]
        self.timeout = timeout

    def describe(self):
        return {"provider": self.kind, "argv": self.argv}

    def complete(self, prompt, exchange_dir):
        self.last_meta = None
        try:
            result = subprocess.run(self.argv, input=prompt, capture_output=True, text=True, encoding="utf-8",
                                    timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise AgentUnavailable("AGENT_COMMAND_TIMEOUT after %ss" % self.timeout)
        except OSError as error:
            raise AgentUnavailable("AGENT_COMMAND_NOT_STARTED: %s" % error)
        (Path(exchange_dir) / "agent-stderr.log").write_text(result.stderr or "", encoding="utf-8")
        if result.returncode != 0:
            (Path(exchange_dir) / "agent-stdout.log").write_text(result.stdout or "", encoding="utf-8")
            detail = ""
            try:
                data = json.loads(result.stdout)
                if isinstance(data, dict):
                    detail = ": %s %s" % (data.get("api_error") or "", str(data.get("result") or "")[:300])
            except (json.JSONDecodeError, ValueError):
                detail = ": " + (result.stderr or result.stdout or "")[-300:]
            raise AgentUnavailable("AGENT_COMMAND_FAILED exit %s%s" % (result.returncode, detail))
        return self._unwrap(result.stdout, exchange_dir)

    def _unwrap(self, stdout, exchange_dir):
        """CLIs such as `claude -p --output-format json` wrap the text with usage metadata."""
        try:
            data = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            return stdout
        if isinstance(data, dict) and data.get("type") == "result" and isinstance(data.get("result"), str):
            (Path(exchange_dir) / "agent-output.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            if data.get("is_error"):
                raise AgentUnavailable("AGENT_REPORTED_ERROR: " + data["result"][:500])
            self.last_meta = {"models": sorted((data.get("modelUsage") or {}).keys()),
                              "cost_usd_reported_by_cli": data.get("total_cost_usd"),
                              "duration_ms": data.get("duration_ms"), "session_id": data.get("session_id")}
            return data["result"]
        return stdout


def _post(url, headers, body, timeout):
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                     headers={"content-type": "application/json", **headers}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError) as error:  # URLError/HTTPError/timeouts are OSError subclasses
        raise AgentUnavailable("AGENT_API_ERROR: %s" % error)


class AnthropicAgent(Agent):
    kind = "anthropic"

    def __init__(self, model, base_url=None, timeout=600, max_tokens=8000):
        self.model, self.timeout, self.max_tokens = model, timeout, max_tokens
        self.base_url = (base_url or "https://api.anthropic.com").rstrip("/")
        self.key = os.environ.get("ANTHROPIC_API_KEY")
        if not self.key:
            raise ValueError("ANTHROPIC_API_KEY not set")

    def describe(self):
        return {"provider": self.kind, "model": self.model, "base_url": self.base_url}

    def complete(self, prompt, exchange_dir):
        data = _post(self.base_url + "/v1/messages", {"x-api-key": self.key, "anthropic-version": "2023-06-01"},
                     {"model": self.model, "max_tokens": self.max_tokens,
                      "messages": [{"role": "user", "content": prompt}]}, self.timeout)
        (Path(exchange_dir) / "api-response.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")


class OpenAICompatibleAgent(Agent):
    kind = "openai"

    def __init__(self, model, base_url=None, timeout=600, key_env="OPENAI_API_KEY"):
        self.model, self.timeout = model, timeout
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self.key = os.environ.get(key_env)
        if not self.key:
            raise ValueError(key_env + " not set")

    def describe(self):
        return {"provider": self.kind, "model": self.model, "base_url": self.base_url}

    def complete(self, prompt, exchange_dir):
        data = _post(self.base_url + "/chat/completions", {"authorization": "Bearer " + self.key},
                     {"model": self.model, "messages": [{"role": "user", "content": prompt}]}, self.timeout)
        (Path(exchange_dir) / "api-response.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return data["choices"][0]["message"]["content"]


def make_agent(kind, model=None, command=None, base_url=None):
    if kind == "exchange":
        return ExchangeAgent()
    if kind == "command":
        if not command:
            raise ValueError("--agent-command required")
        return CommandAgent(command)
    if kind == "anthropic":
        if not model:
            raise ValueError('--model required for anthropic provider')
        return AnthropicAgent(model, base_url)
    if kind == "openai":
        if not model:
            raise ValueError("--model required for openai provider")
        return OpenAICompatibleAgent(model, base_url)
    raise ValueError("UNKNOWN_AGENT: " + str(kind))


def extract_json(text: str) -> dict:
    """First JSON object in a ```json fence, else the outermost {...}."""
    import re
    fenced = re.findall(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    for block in fenced:
        try:
            value = json.loads(block)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            continue
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(text[start:end + 1])
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
    # Chat replies pasted by hand: prose around the object, `//` comments copied from the format example,
    # trailing commas. Clean those (outside strings only) and take the first object that parses.
    cleaned = _strip_json_noise(text)
    decoder = json.JSONDecoder()
    for i, ch in enumerate(cleaned):
        if ch == "{":
            try:
                value, _ = decoder.raw_decode(cleaned, i)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and len(value) >= 2:
                return value
    raise ValueError("NO_JSON_OBJECT_IN_AI_RESPONSE")


def _strip_json_noise(text: str) -> str:
    """Remove // line comments and trailing commas outside JSON strings; keep everything else."""
    import re
    out, i, in_str, esc = [], 0, False, False
    while i < len(text):
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str, esc = True, False
            out.append(c)
        elif c == "/" and text.startswith("//", i):
            j = text.find("\n", i)
            i = len(text) if j < 0 else j
            continue
        else:
            out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))
