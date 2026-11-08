"""Final deliverables: report.json, report.html, final netlist, BOM.

All numbers come from round.json / evaluation.json / tolerance.json written
from native runs. Rendering only formats them.
"""
from __future__ import annotations

import gzip
import html
import json
import re
from pathlib import Path

from implementation import solver_interface as SI
from implementation.circuit import bom as B
from implementation.circuit import netlist as N

STATUS_TEXT = {
    "ACHIEVED": "已实现：标称工况与 ±公差扫描全部通过冻结验收标准",
    "NOT_ACHIEVED_ITERATION_LIMIT": "无法实现（在迭代上限内）：没有任何一版设计同时通过标称与公差验收",
    "NOT_ACHIEVED_AI_DECLARED_INFEASIBLE": "无法实现：外部 AI 根据仿真结果判断该规格在当前规则下不可实现",
    "NOT_ACHIEVED_SPEC_REJECTED": "无法开始：外部 AI 起草的验收规格多次未通过平台校验",
    "NOT_ACHIEVED_SIMULATION_BUDGET": "无法实现（在仿真次数预算内）：预算用尽前没有设计通过全部验收",
}
ROUND_TEXT = {"PASS": "通过", "FAIL_NOMINAL": "标称不达标", "FAIL_TOLERANCE": "公差不达标",
              "INPUT_REJECTED": "设计被平台拒绝", "MODEL_REJECTED": "ngspice 拒绝模型",
              "SIMULATION_FAILED": "仿真未收敛/中止", "TOOL_ERROR": "工具错误", "RESOURCE_LIMIT": "资源超限",
              "AI_DECLARED_INFEASIBLE": "AI 判定不可实现"}


# English wording of the same statuses (render_html(lang="en")); report.json keeps the Chinese text it always had
STATUS_TEXT_EN = {
    "ACHIEVED": "Achieved: the nominal case and the +/- tolerance sweep all pass the frozen acceptance criteria",
    "NOT_ACHIEVED_ITERATION_LIMIT": "Not achieved (within the iteration limit): no revision passed both nominal and tolerance checks",
    "NOT_ACHIEVED_AI_DECLARED_INFEASIBLE": "Not achievable: the external AI concluded from the simulation results that the spec cannot be met under these rules",
    "NOT_ACHIEVED_SPEC_REJECTED": "Could not start: the acceptance spec drafted by the external AI failed the platform checks repeatedly",
    "NOT_ACHIEVED_SIMULATION_BUDGET": "Not achieved (within the simulation budget): no design passed every check before the budget ran out",
}
ROUND_TEXT_EN = {"PASS": "Pass", "FAIL_NOMINAL": "Nominal case failed", "FAIL_TOLERANCE": "Tolerance sweep failed",
                 "INPUT_REJECTED": "Design rejected by the platform", "MODEL_REJECTED": "ngspice rejected a model",
                 "SIMULATION_FAILED": "Simulation did not converge / aborted", "TOOL_ERROR": "Tool error", "RESOURCE_LIMIT": "Resource limit",
                 "AI_DECLARED_INFEASIBLE": "AI declared it infeasible"}
# platform-written (not AI-written) English strings of report.json, in Chinese for the zh rendering
TEMPLATE_ZH = {
    "Simulation of SPICE models only; generic device models chosen by the AI, not vendor-qualified models.":
        "只仿真了 SPICE 模型；器件模型是 AI 选的通用模型，不是厂家认证模型。",
    "Tolerance is sampled (OFAT, corners, Monte Carlo) on R/L/C values only; semiconductor model spread, temperature drift, aging and parasitics are not varied.":
        "公差只对 R/L/C 取样（逐个、角点、蒙特卡洛）；半导体参数分散、温漂、老化和寄生参数都没有变化。",
    "No PCB layout, thermal, EMI, safety or hardware measurement; DIGITAL_DESIGN level evidence only.":
        "没有 PCB 布局、散热、电磁兼容、安全或实物测量；只是数字设计层面的证据。",
    "Acceptance thresholds drafted by AI from the requirement must be confirmed by a human (see premises.spec_review_note).":
        "验收阈值由 AI 根据需求起草，需要人工确认（见前提条件里的复核提示）。",
    "No op-amp/IC vendor macromodels are available to the AI in this loop; designs use discrete devices with generic model cards.":
        "这个流程里 AI 拿不到运放/芯片的厂家宏模型；设计只用分立器件和通用模型。",
    "DIGITAL_DESIGN (simulation evidence only; not ENGINEERING_VERIFIED or MANUFACTURING_READY)":
        "数字设计（只有仿真证据；不是工程验证，也不能直接生产）",
    "files.json at run root seals every file with sha256; verify with `circuit verify`":
        "运行目录下的封存清单用 sha256 记录了每个文件，可以用命令行工具核对",
    "Acceptance thresholds were drafted by the external AI from the requirement text; a human should confirm them before treating ACHIEVED as accepted.":
        "验收阈值由外部 AI 根据需求文字起草；在把“已实现”当作验收通过之前，应由人确认。",
}
TEMPLATE_ZH.update({
    "OFAT ±tol for each R/L/C + 2 global corners + 20 seeded uniform Monte Carlo":
        "每个 R/L/C 逐个 ±公差 + 2 个整体角点 + 20 次固定种子的均匀蒙特卡洛",
    "Semiconductor .model parameters, switch models, coupling K, internal signal sources and all testbench elements":
        "半导体模型参数、开关模型、耦合系数 K、内部信号源，以及测试台里的所有元件",
    "not varied": "不变化", "model parameters not varied": "模型参数不变化",
    "ideal (not a purchasable part; replace by controller/driver)": "理想元件（买不到；实际要换成控制器或驱动电路）",
})
_LANG = "zh"


def _t(zh, en):
    return en if _LANG == "en" else zh


def _tpl(text):
    """Platform template text in the page language (AI-written text is never translated)."""
    if text is None:
        return None
    if _LANG == "zh":
        if text in TEMPLATE_ZH:
            return TEMPLATE_ZH[text]
        m = re.match(r"1 tool probe \+ (\d+) nominal \+ (\d+) tolerance runs", text)
        if m:
            return "1 次工具探测 + %s 次标称 + %s 次公差仿真；每次都有 execution.json、标准输出和封存的数据向量" % m.groups()
    return text


def _bom_type(text):
    """bom.py writes the part kind in both languages ("电阻 resistor"); keep the page language only."""
    m = re.match(r"^寄生参数 parasitic of (.+)$", text or "")
    if m:
        return _t("寄生参数（%s）" % m.group(1), "parasitic of %s" % m.group(1))
    m = re.match(r"^(\S*[\u4e00-\u9fff]\S*) (.+)$", text or "")
    return (_t(m.group(1), m.group(2)) if m else text)


def _ai(text, tag="div"):
    """AI- or user-written text, shown verbatim and marked as a quotation (not page language)."""
    return "<%s class='data'>%s</%s>" % (tag, _e(text), tag)


def _rounds(directory):
    rows = []
    for path in sorted((directory / "rounds").glob("round-*/round.json")):
        rows.append(SI.read_json(path))
    return rows


def _rank(record):
    status = record["status"]
    if status == "PASS":
        return (3, 0, 0)
    if status == "FAIL_TOLERANCE":
        tol = record["tolerance_summary"]
        return (2, tol["passed_cases"] / max(1, tol["total_cases"]), 0)
    if status == "FAIL_NOMINAL":
        checks = record.get("checks", [])
        passed = sum(1 for c in checks if c["passed"])
        deficit = sum(min(0, c.get("relative_margin") or 0) for c in checks)
        return (1, passed, deficit)
    return (0, 0, 0)


def build_report(loop, status):
    directory = loop.dir
    info = loop.run_info
    spec_path = directory / "spec" / "spec.json"
    spec = SI.read_json(spec_path) if spec_path.is_file() else None
    rounds = _rounds(directory)
    chosen = None
    candidates = [r for r in rounds if r.get("nominal_run")]
    if candidates:
        chosen = max(candidates, key=lambda r: (_rank(r), r["round"])) if status != "ACHIEVED" else \
            next(r for r in reversed(rounds) if r["status"] == "PASS")
    final = directory / "final"
    final.mkdir(exist_ok=True)
    deliverables, bom_rows = {}, []
    if chosen:
        folder = directory / "rounds" / ("round-%02d" % chosen["round"])
        accepted = status == "ACHIEVED"
        stem = "final" if accepted else "best_attempt_NOT_ACCEPTED"
        netlist = (folder / "netlist.cir").read_text(encoding="utf-8")
        header = "* RLSOK %s netlist - round %d - %s\n" % (stem, chosen["round"], status)
        (final / (stem + "_netlist.cir")).write_text(header + netlist, encoding="utf-8")
        (final / (stem + "_dut.cir")).write_text((folder / "dut.cir").read_text(encoding="utf-8"), encoding="utf-8")
        dut = N.validate_dut_body(chosen["dut_body"], spec["ports"])
        bom_rows = B.build_bom(dut, spec["tolerance"]["relative"], chosen.get("part_suggestions"))
        SI.write_json(final / (stem + "_bom.json"), {"schema": "rlsok.circuit-bom/1", "accepted": accepted,
                                                     "round": chosen["round"], "items": bom_rows})
        (final / (stem + "_bom.csv")).write_text(B.bom_csv(bom_rows), encoding="utf-8-sig")
        for name in (stem + "_netlist.cir", stem + "_dut.cir", stem + "_bom.json", stem + "_bom.csv"):
            deliverables[name] = {"path": "final/" + name, "sha256": SI.sha256_file(final / name)}
    iterations = []
    for record in rounds:
        item = {k: record.get(k) for k in ("round", "status", "author", "diagnosis", "changes", "reason", "dut_body",
                                           "metrics", "checks", "audit", "tolerance_summary", "nominal_run",
                                           "started_utc", "finished_utc")}
        folder = directory / "rounds" / ("round-%02d" % record["round"])
        item["evidence"] = {name: {"path": (folder / name).relative_to(directory).as_posix(),
                                   "sha256": SI.sha256_file(folder / name)}
                            for name in ("ai/prompt.md", "ai/response.md", "netlist.cir", "evaluation.json", "tolerance.json")
                            if (folder / name).is_file()}
        iterations.append(item)
    analysis = _infeasibility(rounds, spec) if status != "ACHIEVED" else None
    conclusion = STATUS_TEXT[status]
    if status == "ACHIEVED":
        tol = chosen["tolerance_summary"]
        conclusion += "（第 %d 版设计；公差扫描 %d/%d 例通过）。" % (chosen["round"], tol["passed_cases"], tol["total_cases"])
    elif chosen:
        conclusion += "。最接近的一版是第 %d 版（%s），已作为“未达标最佳尝试”输出，不可当作合格设计。" % (
            chosen["round"], ROUND_TEXT.get(chosen["status"], chosen["status"]))
    simulations = [r["nominal_run"] for r in rounds if r.get("nominal_run")]
    tolerance_runs = loop.native_simulation_count() - len(simulations)
    approval_path = directory / "spec" / "approval.json"
    approval = SI.read_json(approval_path) if approval_path.is_file() else None
    report = {
        "schema": "rlsok.circuit-report/1",
        "run_id": info["run_id"],
        "requirement": info["requirement"],
        "status": status,
        "achieved": status == "ACHIEVED",
        "conclusion": conclusion,
        "final_round": chosen["round"] if chosen else None,
        "deliverables": deliverables,
        "bom": bom_rows,
        "premises": {
            "interpretation": spec["interpretation"] if spec else None,
            "operating_conditions": spec["operating_conditions"] if spec else None,
            "testbench": spec["testbench"] if spec else None,
            "analysis": spec["analysis"] if spec else None,
            "temperature_celsius": spec["temperature_celsius"] if spec else None,
            "component_parameters": "see bom (values) and final DUT netlist (.model parameters)",
            "tolerance": spec["tolerance"] if spec else None,
            "assumptions": spec["assumptions"] if spec else None,
            "acceptance_checks": spec["checks"] if spec else None,
            "spec_source": spec["source"] if spec else None,
            "spec_review_note": spec["review_note"] if spec else None,
            "spec_sha256": SI.sha256_file(spec_path) if spec else None,
            "spec_human_approval": approval,
        },
        "tools": info["tools"],
        "agent": info["agent"],
        "settings": info["settings"],
        "iterations": iterations,
        "infeasibility_analysis": analysis,
        "native_simulation_count": len(simulations) + tolerance_runs + 1,
        "delivery_level": "DIGITAL_DESIGN (simulation evidence only; not ENGINEERING_VERIFIED or MANUFACTURING_READY)",
        "native_simulation_note": "1 tool probe + %d nominal + %d tolerance runs; each has execution.json, stdout/stderr and sealed vectors"
                                  % (len(simulations), tolerance_runs),
        "limitations": [
            "Simulation of SPICE models only; generic device models chosen by the AI, not vendor-qualified models.",
            "Tolerance is sampled (OFAT, corners, Monte Carlo) on R/L/C values only; semiconductor model spread, temperature drift, aging and parasitics are not varied.",
            "No PCB layout, thermal, EMI, safety or hardware measurement; DIGITAL_DESIGN level evidence only.",
            "Acceptance thresholds drafted by AI from the requirement must be confirmed by a human (see premises.spec_review_note)."
            if not approval else "Acceptance spec approved by %s (recorded statement, not cryptographic identity)." % approval["approved_by"],
            "No op-amp/IC vendor macromodels are available to the AI in this loop; designs use discrete devices with generic model cards.",
        ],
        "evidence_index": "files.json at run root seals every file with sha256; verify with `circuit verify`",
    }
    return report


def _infeasibility(rounds, spec):
    if not spec:
        return {"reason": "verification spec could not be frozen", "check_history": {}}
    history = {}
    for record in rounds:
        for check in record.get("checks") or []:
            entry = history.setdefault(check["id"], {"ever_passed_nominal": False, "best_actual": None, "best_margin": None,
                                                     "limit": check.get("value") if check["op"] != "between" else [check.get("min"), check.get("max")]})
            if check["passed"]:
                entry["ever_passed_nominal"] = True
            if check.get("margin") is not None and (entry["best_margin"] is None or check["margin"] > entry["best_margin"]):
                entry["best_margin"], entry["best_actual"] = check["margin"], check["actual"]
    statuses = [r["status"] for r in rounds]
    last = rounds[-1] if rounds else {}
    return {"round_statuses": statuses,
            "never_passed_checks": [k for k, v in history.items() if not v["ever_passed_nominal"]],
            "check_history": history,
            "tolerance_failures": [{"round": r["round"], "failed_examples": r["tolerance_summary"]["failed_examples"][:5]}
                                   for r in rounds if r["status"] == "FAIL_TOLERANCE"],
            "last_ai_diagnosis": last.get("diagnosis"),
            "ai_infeasible_reason": last.get("reason") if last.get("status") == "AI_DECLARED_INFEASIBLE" else None}


# ---------------------------------------------------------------- HTML render

CSS = """
:root{--bg:#fcfcfb;--panel:#ffffff;--ink:#0b0b0b;--ink2:#52514e;--muted:#8a8984;--line:#e4e3df;--accent:#2a78d6;
--good:#137a13;--bad:#b52a2a;--warn:#8a5d00;--code:#f4f3f0}
@media (prefers-color-scheme:dark){:root{--bg:#1a1a19;--panel:#222221;--ink:#f0efec;--ink2:#c3c2b7;--muted:#8f8e86;--line:#383835;
--accent:#3987e5;--good:#3fbf3f;--bad:#e66767;--warn:#e0a52a;--code:#2a2a28}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
main{max-width:1100px;margin:0 auto;padding:32px 16px 64px}h1{font-size:26px;margin:0 0 4px}h2{font-size:19px;margin:36px 0 12px;border-bottom:1px solid var(--line);padding-bottom:6px}
h3{font-size:16px;margin:20px 0 8px}.sub{color:var(--ink2)}.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin:12px 0}
.verdict{font-size:17px;font-weight:600}.pill{display:inline-block;white-space:nowrap;border-radius:999px;padding:1px 10px;font-size:15px;font-weight:600;border:1px solid currentColor}
.ok{color:var(--good)}.no{color:var(--bad)}.mid{color:var(--warn)}table{border-collapse:collapse;width:100%;font-size:15px}
th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}th{color:var(--ink2);font-weight:600}
td.num{font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}pre,code{font-family:ui-monospace,Consolas,monospace;font-size:14px}
pre{background:var(--code);padding:10px 12px;border-radius:8px;overflow:auto;max-height:420px}.scroll{overflow-x:auto}
details{margin:8px 0}summary{cursor:pointer;font-weight:600}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px}
.chart{position:relative}.chart svg{width:100%;height:auto;display:block}.chart .tip{position:absolute;pointer-events:none;background:var(--panel);border:1px solid var(--line);
border-radius:6px;padding:2px 8px;font-size:15px;display:none;white-space:nowrap}.kv td:first-child{color:var(--ink2);width:30%}
.data{background:var(--code);border-radius:4px;padding:0 4px}div.data{padding:8px 10px;display:block}
"""

JS = """
document.querySelectorAll('.chart').forEach(function(box){
 var svg=box.querySelector('svg'),tip=box.querySelector('.tip'),pts=JSON.parse(box.dataset.pts),
 x0=+box.dataset.x0,x1=+box.dataset.x1,y0=+box.dataset.y0,y1=+box.dataset.y1,L=48,R=12,T=10,B=26,W=520,H=200,
 cross=svg.querySelector('.cross'),dot=svg.querySelector('.dot');
 function sx(x){return L+(x-x0)/(x1-x0||1)*(W-L-R)} function sy(y){return T+(1-(y-y0)/(y1-y0||1))*(H-T-B)}
 svg.addEventListener('mousemove',function(e){var r=svg.getBoundingClientRect(),px=(e.clientX-r.left)/r.width*W,
  t=x0+(px-L)/(W-L-R)*(x1-x0),best=pts[0];for(var i=0;i<pts.length;i++){if(Math.abs(pts[i][0]-t)<Math.abs(best[0]-t))best=pts[i]}
  cross.setAttribute('x1',sx(best[0]));cross.setAttribute('x2',sx(best[0]));cross.style.display='';dot.setAttribute('cx',sx(best[0]));dot.setAttribute('cy',sy(best[1]));dot.style.display='';
  tip.style.display='block';tip.textContent=(box.dataset.logx==='1'?'f='+Math.pow(10,best[0]).toPrecision(4)+' Hz   ':'t='+best[0].toPrecision(4)+' s   ')+box.dataset.name+'='+best[1].toPrecision(5);
  tip.style.left=Math.min(r.width-160,(sx(best[0])/W*r.width)+8)+'px';tip.style.top='4px'});
 svg.addEventListener('mouseleave',function(){tip.style.display='none';cross.style.display='none';dot.style.display='none'})});
"""


def _e(value):
    return html.escape("" if value is None else str(value))


def _n(value):
    if value is None:
        return "—"
    if isinstance(value, (int, float)):
        return "{:.6g}".format(value)
    return _e(value)


def _pill(ok, text):
    return '<span class="pill %s">%s %s</span>' % ("ok" if ok else "no", "✓" if ok else "✗", _e(text))


def _limit(check):
    if check["op"] == "between":
        return "[%s, %s]" % (_n(check.get("min")), _n(check.get("max")))
    return ("≥ " if check["op"] == "ge" else "≤ ") + _n(check.get("value"))


def _checks_table(checks):
    rows = ["<div class='scroll'><table><tr><th>%s</th><th>%s</th><th>%s</th><th>%s</th><th>%s</th><th>%s</th></tr>" % (_t("检查项", "Check"), _t("说明（AI 原文）", "Description (AI wording)"), _t("实测", "Measured"), _t("限值", "Limit"), _t("余量", "Margin"), _t("结果", "Result"))]
    for c in checks or []:
        rows.append("<tr><td><code>%s</code></td><td>%s</td><td class='num'>%s %s</td><td class='num'>%s</td><td class='num'>%s</td><td>%s</td></tr>" % (
            _e(c["id"]), _ai(c.get("description"), "span"), _n(c.get("actual")), _e(c.get("unit")), _limit(c), _n(c.get("margin")),
            _pill(c["passed"], _t("通过", "pass") if c["passed"] else _t("不通过", "fail"))))
    rows.append("</table></div>")
    return "".join(rows)


def _chart_block(name, xs, ys, xlabel, unit, log_x=False):
    import math
    if log_x:
        xs = [math.log10(x) for x in xs]
    step = max(1, len(xs) // 700)
    pts = [[xs[i], ys[i]] for i in range(0, len(xs), step)] + [[xs[-1], ys[-1]]]
    x0, x1 = xs[0], xs[-1]
    lo, hi = min(ys), max(ys)
    pad = (hi - lo) * 0.08 or abs(hi) * 0.05 or 1.0
    y0, y1 = lo - pad, hi + pad
    L, R, T, Bm, W, H = 48, 12, 10, 26, 520, 200
    sx = lambda x: L + (x - x0) / ((x1 - x0) or 1) * (W - L - R)
    sy = lambda v: T + (1 - (v - y0) / ((y1 - y0) or 1)) * (H - T - Bm)
    path = " ".join("%.1f,%.1f" % (sx(a), sy(b)) for a, b in pts)
    ticks = "".join("<line x1='%d' x2='%d' y1='%.1f' y2='%.1f' stroke='var(--line)'/><text x='%d' y='%.1f' font-size='15' fill='var(--muted)' text-anchor='end'>%s</text>" % (
        L, W - R, sy(v), sy(v), L - 4, sy(v) + 3, "{:.4g}".format(v)) for v in (y0 + (y1 - y0) * k / 4 for k in range(5)))
    label = (lambda v: "{:.3g}".format(10 ** v)) if log_x else (lambda v: "{:.3g}".format(v))
    xt = "".join("<text x='%.1f' y='%d' font-size='15' fill='var(--muted)' text-anchor='middle'>%s</text>" % (
        sx(x0 + (x1 - x0) * k / 4), H - 8, label(x0 + (x1 - x0) * k / 4)) for k in range(5))
    return ("<div class='card chart' data-name='%s' data-pts='%s' data-x0='%r' data-x1='%r' data-y0='%r' data-y1='%r' data-logx='%d'>"
            "<div><b><code>%s</code></b> <span class='sub'>(%s, %s)</span></div><div class='tip'></div>"
            "<svg viewBox='0 0 %d %d' role='img' aria-label='%s'>%s%s"
            "<polyline fill='none' stroke='var(--accent)' stroke-width='2' points='%s'/>"
            "<line class='cross' y1='%d' y2='%d' stroke='var(--muted)' style='display:none'/>"
            "<circle class='dot' r='4' fill='var(--accent)' stroke='var(--panel)' stroke-width='2' style='display:none'/></svg></div>" % (
                _e(name), _e(json.dumps(pts)), x0, x1, y0, y1, 1 if log_x else 0, _e(name), unit, xlabel,
                W, H, _e(name), ticks, xt, path, T, H - Bm))


def _charts(directory, report):
    import math
    if not report.get("final_round"):
        return ""
    run = directory / "rounds" / ("round-%02d" % report["final_round"]) / "nominal" / "vectors.json.gz"
    if not run.is_file():
        return ""
    with gzip.open(run, "rt", encoding="utf-8") as stream:
        data = json.load(stream)
    vectors, complex_vectors = data["vectors"], data.get("complex_vectors", {})
    spec_vectors = SI.read_json(directory / "spec" / "spec.json")["vectors"]
    blocks = []
    if "time" in vectors:
        for name in spec_vectors:
            if name in vectors:
                blocks.append(_chart_block(name, vectors["time"], vectors[name], _t("时间 s", "time s"), "A" if "#branch" in name else "V"))
    elif "frequency" in vectors:
        for name in spec_vectors:
            if name in complex_vectors:
                c = complex_vectors[name]
                db = [20 * math.log10(max(math.hypot(a, b), 1e-20)) for a, b in zip(c["re"], c["im"])]
                blocks.append(_chart_block(name + " |dB|", vectors["frequency"], db, _t("频率 Hz（对数）", "frequency Hz (log)"), "dB", log_x=True))
    if not blocks:
        return ""
    return "<h2>%s</h2><p class='sub'>%s</p><div class='grid'>%s</div>" % (_t("最终（或最佳）设计的标称波形", "Nominal waveforms of the final (or best) design"), _t("直接读取 ngspice 输出向量（显示时抽稀，原始数据在 <code>vectors.json.gz</code>）。", "Read directly from the ngspice output vectors (thinned for display; raw data in <code>vectors.json.gz</code>)."), "".join(blocks))


def _conclusion_en(report):
    status = report["status"]
    text = STATUS_TEXT_EN.get(status, status)
    final = next((i for i in report["iterations"] if i["round"] == report.get("final_round")), None)
    if status == "ACHIEVED" and final and final.get("tolerance_summary"):
        tol = final["tolerance_summary"]
        return text + " (revision %d; tolerance sweep %d/%d cases pass)." % (final["round"], tol["passed_cases"], tol["total_cases"])
    if final:
        return text + ". The closest revision is %d (%s); it is output as the best attempt that did NOT pass and must not be used as a valid design." % (
            final["round"], ROUND_TEXT_EN.get(final["status"], final["status"]))
    return text + "."


def render_html(report, directory=None, lang="zh"):
    global _LANG
    _LANG = "en" if lang == "en" else "zh"
    RT = ROUND_TEXT_EN if _LANG == "en" else ROUND_TEXT
    directory = Path(directory) if directory else None
    ok = report["achieved"]
    p = report["premises"]
    title = _t("RLSOK 电路闭环报告", "RLSOK circuit loop report")
    conclusion = _conclusion_en(report) if _LANG == "en" else report["conclusion"]
    parts = ["<!doctype html><html lang='%s'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>" % _t("zh-CN", "en"),
             "<title>%s</title><style>%s</style></head><body><main>" % (title, CSS),
             "<h1>%s</h1><div class='sub'><code>run %s · %s</code></div>" % (title, _e(report["run_id"]), _e(report["tools"]["solver"]["binary"])),
             "<div class='card'><div class='sub'>%s</div>%s</div>" % (_t("需求（用户原文）", "Requirement (the user's own words)"), _ai(report["requirement"], "div")),
             "<div class='card'><div>%s</div><p class='verdict'>%s</p></div>" % (
                 _pill(ok, _t("已实现", "Achieved") if ok else _t("无法实现 / 未达标", "Not achievable / not met")), _e(conclusion))]
    parts.append("<h2>%s</h2><div class='card'><p class='sub'>%s</p><table class='kv'>" % (
        _t("前提条件", "Premises"), _t("下面带底色的文字是外部 AI 写的原文，没有翻译。", "Shaded text below is the external AI's original wording, not translated.")))
    approval = p.get("spec_human_approval")
    for label, value, ai in ((_t("需求理解", "Interpretation"), p.get("interpretation"), True),
                             (_t("工况", "Operating conditions"), json.dumps(p.get("operating_conditions"), ensure_ascii=False), True),
                             (_t("温度", "Temperature"), "%s °C" % _n(p.get("temperature_celsius")), False),
                             (_t("分析", "Analysis"), json.dumps(p.get("analysis")), True),
                             (_t("公差", "Tolerance"), json.dumps(p.get("tolerance"), ensure_ascii=False), True),
                             (_t("规格来源", "Spec source"), "%s (sha256 %s)" % (p.get("spec_source"), p.get("spec_sha256")), True),
                             (_t("复核提示", "Review note"), _tpl(p.get("spec_review_note")), False),
                             (_t("人工确认规格", "Spec confirmed by a person"), ("%s (%s)" % (approval["approved_by"], approval["approved_utc"]))
                              if approval else _t("未确认（阈值由外部 AI 起草）", "Not confirmed (thresholds drafted by the external AI)"), False),
                             (_t("交付层级", "Delivery level"), _tpl(report.get("delivery_level")), False)):
        parts.append("<tr><td>%s</td><td>%s</td></tr>" % (_e(label), _ai(value, "span") if ai else _e(value)))
    parts.append("</table><h3>%s</h3><ul>%s</ul><h3>%s</h3><pre>%s</pre></div>" % (
        _t("假设（AI 原文）", "Assumptions (AI wording)"),
        "".join("<li>%s</li>" % _ai(a, "span") for a in p.get("assumptions") or []),
        _t("冻结测试台（AI 不可修改）", "Frozen testbench (the AI cannot change it)"), _e("\n".join(p.get("testbench") or []))))
    if report["final_round"]:
        final_round = next(i for i in report["iterations"] if i["round"] == report["final_round"])
        parts.append("<h2>%s</h2>" % (_t("最终设计（第 %d 版）", "Final design (revision %d)") if ok else
                                      _t("未达标的最佳尝试（第 %d 版，不可用于制造）", "Best attempt that did not pass (revision %d, not for manufacturing)")) % report["final_round"])
        parts.append(_checks_table(final_round.get("checks")))
        tol = final_round.get("tolerance_summary")
        if tol:
            parts.append("<div class='card'><b>%s ±%g%%</b>: %s, %s<br><span class='sub'>%s</span>" % (
                _t("公差检查", "Tolerance check"), tol["relative"] * 100, _pill(tol["passed_cases"] == tol["total_cases"], _t("公差", "tolerance")),
                _t("%d/%d 例通过", "%d/%d cases pass") % (tol["passed_cases"], tol["total_cases"]),
                _t("方法：%s；种子 %s。未变化：%s", "Method: %s; seed %s. Not varied: %s") % (_e(_tpl(tol["method"])), _e(tol["seed"]), _e(_tpl(tol["not_varied"])))))
            parts.append("<div class='scroll'><table><tr><th>%s</th><th>%s</th><th>%s</th><th>%s</th><th>%s</th></tr>" % (
                _t("检查项", "Check"), _t("最差工况", "Worst case"), _t("最差实测", "Worst measured"), _t("余量", "Margin"), _t("结果", "Result")))
            for cid, worst in tol["worst"].items():
                parts.append("<tr><td><code>%s</code></td><td><code>%s</code></td><td class='num'>%s</td><td class='num'>%s</td><td>%s</td></tr>" % (
                    _e(cid), _e(worst["case"]), _n(worst["actual"]), _n(worst["margin"]),
                    _pill(worst["passed"], _t("通过", "pass") if worst["passed"] else _t("不通过", "fail"))))
            parts.append("</table></div></div>")
        parts.append("<h3>%s</h3><div class='scroll'><table><tr>%s</tr>" % (_t("元件清单", "Bill of materials"), "".join("<th>%s</th>" % h for h in (
            _t("位号", "Ref"), _t("类型", "Type"), _t("值 / 模型", "Value / model"), _t("公差", "Tolerance"), _t("可采购", "Buyable"),
            _t("连接", "Nodes"), _t("AI 建议型号（未核实）", "AI part suggestion (not checked)")))))
        for row in report["bom"]:
            buy = row.get("purchasable")
            parts.append("<tr><td>%s</td><td>%s</td><td>%s%s</td><td>%s</td><td>%s</td><td><code>%s</code></td><td>%s</td></tr>" % (
                "<code>%s</code>" % _e(row["refdes"]), _e(_bom_type(row["type"])), "<code>%s</code>" % _e(row.get("value")),
                ("<br><code>%s</code>" % _e(row["model"])) if row.get("model") else "", _e(_tpl(row.get("tolerance"))),
                "—" if buy is None else (_t("是", "yes") if buy else _t("否", "no")),
                _e(row["nodes"]), _ai(row.get("ai_part_suggestion_unverified", ""), "span")))
        parts.append("</table></div>")
        parts.append("<h3>%s</h3><ul>%s</ul>" % (_t("交付文件", "Deliverables"), "".join("<li><code>%s</code> <span class='sub'><code>sha256 %s</code></span></li>" % (
            _e(v["path"]), _e(v["sha256"])) for v in report["deliverables"].values())))
    if directory:
        parts.append(_charts(directory, report))
    parts.append("<h2>%s</h2>" % _t("每一版的仿真结果", "Simulation results of every revision"))
    for item in report["iterations"]:
        good = item["status"] == "PASS"
        parts.append("<details %s><summary>%s · %s %s</summary><div class='card'>" % (
            "open" if item["round"] == report.get("final_round") else "", _t("第 %d 版", "Revision %d") % item["round"],
            _pill(good, RT.get(item["status"], item["status"])), _ai(item.get("reason") or "", "span")))
        if item.get("diagnosis"):
            parts.append("<p><b>%s</b>%s</p>" % (_t("AI 诊断（原文）：", "AI diagnosis (original): "), _ai(item["diagnosis"], "span")))
        if item.get("changes"):
            parts.append("<p><b>%s</b>%s</p>" % (_t("AI 修改（原文）：", "AI changes (original): "), _ai(item["changes"], "span")))
        if item.get("dut_body"):
            parts.append("<pre>%s</pre>" % _e("\n".join(item["dut_body"])))
        if item.get("checks"):
            parts.append(_checks_table(item["checks"]))
        tol = item.get("tolerance_summary")
        if tol:
            parts.append("<p>%s</p>" % (_t("公差扫描：%d/%d 例通过", "Tolerance sweep: %d/%d cases pass") % (tol["passed_cases"], tol["total_cases"])))
        run = item.get("nominal_run")
        if run:
            parts.append("<p class='sub'>%s</p>" % (_t("标称仿真：%s，退出码 %s，%.3f 秒；证据 <code>%s</code>", "Nominal simulation: %s, exit %s, %.3f s; evidence <code>%s</code>") % (
                _e(_t({"SUCCEEDED": "成功"}.get(run["status"], run["status"]), run["status"])), _e(run["exit_code"]), run["wall_seconds"] or 0, _e(run["directory"]))))
        parts.append("<p class='sub'>%s</p></div></details>" % " · ".join(
            "<code>%s %s…</code>" % (_e(k), _e(v["sha256"][:12])) for k, v in (item.get("evidence") or {}).items()))
    analysis = report.get("infeasibility_analysis")
    if analysis:
        parts.append("<h2>%s</h2><div class='card'><p>%s%s</p>" % (_t("无法实现的原因分析", "Why it cannot be achieved"), _t("各版状态：", "Status of each revision: "),
                                                                  _e(" → ".join(RT.get(x, x) for x in analysis.get("round_statuses", [])))))
        if analysis.get("never_passed_checks"):
            parts.append("<p><b>%s</b>%s</p>" % (_t("从未在标称工况下通过的检查：", "Checks that never passed in the nominal case: "),
                                                 _t("、", ", ").join("<code>%s</code>" % _e(x) for x in analysis["never_passed_checks"])))
        parts.append("<div class='scroll'><table><tr><th>%s</th><th>%s</th><th>%s</th><th>%s</th></tr>" % (
            _t("检查项", "Check"), _t("最好的一次实测", "Best measured"), _t("最好余量", "Best margin"), _t("限值", "Limit")))
        for cid, h in analysis.get("check_history", {}).items():
            parts.append("<tr><td><code>%s</code></td><td class='num'>%s</td><td class='num'>%s</td><td class='num'>%s</td></tr>" % (
                _e(cid), _n(h["best_actual"]), _n(h["best_margin"]), _e(h["limit"])))
        parts.append("</table></div>")
        for key, label in (("ai_infeasible_reason", _t("AI 给出的不可实现理由（原文）", "The AI's reason for infeasibility (original)")),
                           ("last_ai_diagnosis", _t("最后一轮 AI 诊断（原文）", "Last AI diagnosis (original)"))):
            if analysis.get(key):
                parts.append("<p><b>%s: </b>%s</p>" % (label, _ai(analysis[key], "span")))
        parts.append("</div>")
    tools = report["tools"]
    parts.append("<h2>%s</h2><div class='card'><table class='kv'>" % _t("工具与版本", "Tools and versions"))
    for label, value in ((_t("求解器", "Solver"), "ngspice %s (%s)" % (tools["solver"]["version"], tools["solver"]["backend"])),
                         (_t("求解器文件", "Solver binary"), tools["solver"]["binary"]), (_t("求解器 sha256", "Solver sha256"), tools["solver"]["binary_sha256"]),
                         ("Python / " + _t("平台", "platform"), "%s / %s" % (tools["python"], tools["platform"])),
                         (_t("外部 AI", "External AI"), json.dumps(report["agent"], ensure_ascii=False)),
                         (_t("原生仿真次数", "Native simulations"), "%s (%s)" % (report["native_simulation_count"], _tpl(report["native_simulation_note"]))),
                         (_t("迭代上限", "Iteration limit"), report["settings"]["max_iterations"])):
        parts.append("<tr><td>%s</td><td><code>%s</code></td></tr>" % (_e(label), _e(value)))
    parts.append("</table></div><h2>%s</h2><ul>%s</ul>" % (_t("局限", "Limitations"), "".join("<li>%s</li>" % _e(_tpl(x)) for x in report["limitations"])))
    parts.append("<p class='sub'>%s</p></main><script>%s</script></body></html>" % (_e(_tpl(report["evidence_index"])), JS))
    return "".join(parts)
