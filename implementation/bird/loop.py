"""Bird design closed loop (resumable): requirement -> AI spec (frozen) -> AI design (CAD code + parts) ->
platform evaluation (CAD sandbox, mass, power, endurance, trim, 60 s flight) -> numbers back to the AI -> ...

Same run-directory layout and exit codes as the circuit loop, so the console renders it the same way.
Every result carries the UNVERIFIED label from implementation/bird/design.py.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import uuid

from implementation import solver_interface as SI
from implementation.circuit import agents as A
from implementation.bird import cad_sandbox as C
from implementation.bird import design as D
from implementation.bird import prompts as P

SCHEMA_RUN = "rlsok.bird-loop-run/1"
SPEC_ATTEMPTS = 3


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class BirdLoop:
    def __init__(self, directory, agent: A.Agent, local_parts: Path | None = None):
        self.dir = Path(directory).resolve()
        self.agent = agent
        self.run_info = SI.read_json(self.dir / "run.json")
        self.lib = D.load_library(local_parts)

    @classmethod
    def create(cls, directory, requirement, agent, max_iterations=6, agent_config=None, local_parts=None):
        directory = Path(directory).resolve()
        if directory.exists() and any(directory.iterdir()):
            raise ValueError("RUN_DIRECTORY_NOT_EMPTY: " + str(directory))
        if not requirement.strip():
            raise ValueError("REQUIREMENT_TEXT_REQUIRED")
        directory.mkdir(parents=True, exist_ok=True)
        SI.write_json(directory / "run.json", {"schema": SCHEMA_RUN, "run_id": uuid.uuid4().hex, "created_utc": now(),
                                               "requirement": requirement, "settings": {"max_iterations": max_iterations},
                                               "agent": agent.describe(), "agent_config": agent_config or {"kind": agent.kind},
                                               "label": D.LABEL})
        SI.write_json(directory / "state.json", {"phase": "spec", "updated_utc": now()})
        return cls(directory, agent, local_parts)

    # -- shared with the circuit loop's console features
    def next_open_round(self):
        n = 1
        while (self.dir / "rounds" / ("round-%02d" % n) / "ai" / "prompt.md").is_file():
            n += 1
        return n

    def add_user_note(self, text):
        text = str(text or "").strip()
        if not text:
            raise ValueError("EMPTY_NOTE")
        if SI.read_json(self.dir / "state.json")["phase"] == "done":
            raise ValueError("RUN_FINISHED")
        note = {"utc": now(), "for_round": self.next_open_round(), "text": text[:2000]}
        with (self.dir / "notes.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(note, ensure_ascii=False) + "\n")
        return note

    def user_notes(self, upto):
        p = self.dir / "notes.jsonl"
        if not p.is_file():
            return []
        return [n for n in (json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()) if n["for_round"] <= upto]

    def _ask(self, folder: Path, prompt: str) -> str:
        folder.mkdir(parents=True, exist_ok=True)
        pp = folder / "prompt.md"
        if pp.is_file():
            prompt = pp.read_text(encoding="utf-8")          # never replace what the AI was asked
        else:
            pp.write_text(prompt, encoding="utf-8")
        text = self.agent.complete(prompt, folder)
        rp = folder / "response.md"
        if not rp.is_file():
            rp.write_text(text, encoding="utf-8")
        return text

    # -- driver
    def run(self):
        state = SI.read_json(self.dir / "state.json")
        if state["phase"] == "done":
            return SI.read_json(self.dir / "final" / "report.json")
        try:
            if state["phase"] == "spec":
                if not self._spec_phase():
                    return self._finish("NOT_ACHIEVED_SPEC_REJECTED")
                SI.write_json(self.dir / "state.json", {"phase": "design", "updated_utc": now()})
            spec = SI.read_json(self.dir / "spec" / "spec.json")
            for n in range(1, self.run_info["settings"]["max_iterations"] + 1):
                rec = self._round(spec, n)
                if rec["status"] == "PASS":
                    return self._finish("ACHIEVED")
            return self._finish("NOT_ACHIEVED_ITERATION_LIMIT")
        except A.AwaitingAI as w:
            return {"status": "AWAITING_AI", "prompt": w.prompt_path, "response": w.response_path}
        except A.AgentUnavailable as e:
            return {"status": "AWAITING_AI_UNAVAILABLE", "reason": str(e)}

    def _spec_phase(self):
        err = None
        for a in range(1, SPEC_ATTEMPTS + 1):
            folder = self.dir / "spec" / ("attempt-%02d" % a)
            v = folder / "validation.json"
            if v.is_file():
                r = SI.read_json(v)
                if r["ok"]:
                    return True
                err = r["error"]; continue
            text = self._ask(folder, P.spec_prompt(self.run_info["requirement"], err))
            try:
                body = D.validate_spec(A.extract_json(text))
                body["checks"] = [{"id": c, "metric": c, "op": op, "value": val, "unit": u, "description": d} for c, op, val, u, d in (
                    ("mass", "le", body["max_mass_g"], "g", "flying mass"),
                    ("endurance", "ge", body["min_endurance_min"], "min", "endurance at cruise"),
                    ("budget_reference", "le", body["budget"]["value"], body["budget"].get("currency", ""), "reference prices"),
                    ("trim_feasible", "le", 0.03, "×weight", "level flight possible"),
                    ("flight_60s", "ge", 1.0, "", "60 s closed-loop flight"),
                    ("printable", "le", 0, "parts", "printed parts fit the bed"))]
                SI.write_json(self.dir / "spec" / "spec.json", {"schema": "rlsok.bird-spec/1", "frozen_utc": now(),
                                                                "requirement_text": self.run_info["requirement"], **body})
                SI.write_json(v, {"ok": True})
                return True
            except (ValueError, KeyError, TypeError) as e:
                err = str(e)
                SI.write_json(v, {"ok": False, "error": err})
        return False

    def _history(self, upto):
        out = []
        for n in range(1, upto):
            p = self.dir / "rounds" / ("round-%02d" % n) / "round.json"
            if p.is_file():
                out.append(SI.read_json(p))
        return out

    def _round(self, spec, n):
        folder = self.dir / "rounds" / ("round-%02d" % n)
        if (folder / "round.json").is_file():
            return SI.read_json(folder / "round.json")
        prompt = P.design_prompt(self.run_info["requirement"], {k: v for k, v in spec.items() if k != "checks"}, self.lib, n,
                                 self.run_info["settings"]["max_iterations"], self._history(n), self.user_notes(n))
        text = self._ask(folder / "ai", prompt)
        rec = {"round": n, "started_utc": now()}
        try:
            design = A.extract_json(text)
        except ValueError as e:
            return self._close(folder, rec | {"status": "INPUT_REJECTED", "reason": "AI response: " + str(e)})
        SI.write_json(folder / "design.json", design)
        rec.update(author=str(design.get("author", ""))[:200], diagnosis=str(design.get("diagnosis", ""))[:4000],
                   changes=str(design.get("changes", ""))[:4000])
        SI.write_json(self.dir / "progress.json", {"round": n, "stage": "evaluating", "done": 0, "total": None, "updated_utc": now()})
        try:
            r = D.evaluate(spec, design, folder / "eval", self.lib)
        except C.CadRejected as e:
            return self._close(folder, rec | {"status": "INPUT_REJECTED", "reason": str(e)})
        except (ValueError, KeyError, TypeError) as e:
            return self._close(folder, rec | {"status": "INPUT_REJECTED", "reason": f"{type(e).__name__}: {e}"})
        SI.write_json(folder / "evaluation.json", {k: v for k, v in r.items() if k != "cad"} | {"cad": r["cad"]})
        failed = [c["id"] for c in r["checks"] if not c["passed"]]
        numbers = {k: r[k] for k in ("aero_model", "mass_g", "mass_breakdown_g", "shaft_power_w", "electrical_power_w",
                                     "endurance_min", "rpm_needed", "rpm_available", "price_by_currency", "printed_too_big")}
        numbers["trim"] = r["trim"]
        return self._close(folder, rec | {"status": "PASS" if not failed else "FAIL", "checks": r["checks"], "numbers": numbers,
                                          "reason": None if not failed else "failed checks: " + ", ".join(failed)})

    def _close(self, folder, rec):
        rec["finished_utc"] = now()
        folder.mkdir(parents=True, exist_ok=True)
        SI.write_json(folder / "round.json", rec)
        (self.dir / "progress.json").unlink(missing_ok=True)
        return rec

    def _finish(self, status):
        rounds = self._history(self.run_info["settings"]["max_iterations"] + 1)
        final = next((r for r in reversed(rounds) if r["status"] == "PASS"), None)
        ev = SI.read_json(self.dir / "rounds" / ("round-%02d" % final["round"]) / "evaluation.json") if final else None
        achieved = status == "ACHIEVED"
        concl = ("仿真中满足冻结的设计规格（第 %d 版）。所有结果未验证：鸟的气动和姿态模型没有通过对应构型的公开数据验证。" % final["round"]
                 if achieved else {"NOT_ACHIEVED_ITERATION_LIMIT": "到达轮数上限，仍有检查没通过。",
                                   "NOT_ACHIEVED_SPEC_REJECTED": "AI 起草的设计规格三次都没通过平台校验。"}.get(status, status))
        rep = {"schema": "rlsok.bird-report/1", "status": status, "achieved": achieved, "conclusion": concl,
               "label": D.LABEL, "requirement": self.run_info["requirement"], "final_round": final["round"] if final else None,
               "iterations": [{"round": r["round"], "status": r["status"], "reason": r.get("reason")} for r in rounds],
               "native_simulation_count": len(rounds),
               "bom": ev["bom"] if ev else None, "price_policy": ev["price_policy"] if ev else None,
               "numbers": final.get("numbers") if final else None}
        (self.dir / "final").mkdir(exist_ok=True)
        SI.write_json(self.dir / "final" / "report.json", rep)
        (self.dir / "final" / "report.html").write_text(_html(rep), encoding="utf-8")
        SI.write_json(self.dir / "state.json", {"phase": "done", "updated_utc": now()})
        return rep


def _html(rep):
    import html as H
    rows = "".join(f"<tr><td>{H.escape(b['name'])}</td><td>{b['qty']}</td><td>{b['mass_g_each']}</td><td>{b['price_each'] if b['price_each'] is not None else '—'} {H.escape(str(b['currency'] or ''))}</td>"
                   f"<td>{H.escape(str(b['retrieved'] or ''))}</td><td>{H.escape(str(b['source'] or '估算，无来源'))}</td></tr>" for b in rep.get("bom") or [])
    return f"""<!doctype html><meta charset="utf-8"><title>RLSOK bird report</title>
<style>body{{font:15px/1.6 system-ui,'Microsoft YaHei';max-width:960px;margin:24px auto;padding:0 16px}}td,th{{border-bottom:1px solid #ddd;padding:6px}}.u{{background:#fff4d6;padding:10px}}</style>
<h1>{H.escape(rep['requirement'])}</h1><p class="u"><b>未验证。</b>{H.escape('；'.join(rep['label']['reasons']))}</p>
<p>{H.escape(rep['conclusion'])}</p><h2>零件清单（参考价，以购买时为准）</h2>
<table><tr><th>零件</th><th>数量</th><th>g/个</th><th>参考价</th><th>抓取日期</th><th>来源</th></tr>{rows}</table>
<h2>计算结果</h2><pre>{H.escape(json.dumps(rep.get('numbers'), ensure_ascii=False, indent=1))}</pre>"""
