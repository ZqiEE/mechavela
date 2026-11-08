"use strict";
// 2026-10-21 customer walkthrough fixes: new and changed strings, deep-merged over i18n.js.
(function () {
  const X = {
    zh: {
      askPh: "例如：12V 转 3.3V，负载 1A，效率 85% 以上",
      scope: "能做：电路设计与验证（你的 AI 设计，ngspice 仿真）· 仿生鸟设计（结果未验证）· 机器人行走测试和训练（不需要 AI）。还不能：用一句话设计走路的机器人。",
      robotLimit: "注意：机器人目前只能运行行走测试和训练，还不能设计。这会跑一次 {r} 的行走测试。",
      exSub: "每个示例都是真实运行。G1 约 1 分钟；电路约 8 分钟（含 30 例公差仿真）。",
      ex: {
        buck: ["12V 转 5V 电源", "你的 AI 设计电路 → ngspice 仿真 → 公差扫描 → 判定。", "需要连接 AI"],
      },
      p_bird: "仿生鸟",
      rs: { FAIL: "未通过" }, waveBird: "飞行仿真 · 高度随时间（60 秒，含一次阵风；未验证）",
      birdNote: "仿生鸟的设计流程可以用，但结果都标“未验证”：鸟的气动和姿态模型没有通过对应构型的公开数据验证。",
      simUnv: { title: "仿真中满足设计规格（未验证）", next: "可以下载零件清单、打印文件和飞控参数先做样机；真机试飞数据可以上传到“真机数据”，用来校准和验证引擎。" },
      birdChecks: true,
      robots: { g1_velocity_flat: "G1 人形机器人", open_duck_mini_v2: "Open Duck 小双足" },
      pipe: { robot_bench: "机器人行走测试", bird: "仿生鸟设计" },
      state: { stopped: "已停止" },
      v: { STOPPED: "已停止", IDEAL_ONLY: "仅理想仿真通过", SIM_UNVERIFIED: "仿真通过（未验证）" },
      checkName: {
        vout_regulation: "输出电压", vout_ripple: "输出纹波", ripple: "纹波", efficiency: "效率", load_current: "负载电流",
        startup_overshoot: "启动过冲", load_step: "负载跳变", input_current_limit: "输入电流",
        platform_internal_source_power: "理想信号源功率（平台检查）",
        mass: "飞行重量", endurance: "续航", trim_feasible: "能否平飞（力平衡残差）", motor_speed_margin: "电机转速余量",
        motor_power_margin: "电机功率余量", flight_60s: "60 秒闭环飞行", pitch_lift_reserve: "失速余量（迎角 +3° 的升力倍数）", mission_sweep: "完整航线仿真（起飞·平飞·转弯·降落，16 组）", printable: "打印件放不下的数量", required_parts: "缺少的必需配件",
        budget_reference: "参考价合计",
      },
      cap: { sac: ["Windows 智能应用控制", ""] },
      why: { sac_on: "这台电脑开着“智能应用控制”。RLSOK 还没有代码签名，Windows 可能拦截机器人功能用到的文件（电路功能实测未被拦）。被拦时 RLSOK 会告诉你原因和办法。",
             sac_robot: "可能被 Windows 智能应用控制拦截（这台电脑实测被拦）。电路功能可以正常用。" },
      fix: { sac_help: "打开 Windows 安全设置" }, duckReady: "已下载到本机，不需要 AI",
      elapsed: "已运行 {s} 秒（实测通常约 {n} 分钟）",
      progNominal: "第 {r} 轮：正在仿真标称工况", progTol: "第 {r} 轮：公差仿真 {d}/{t}",
      stop: "停止", stopping: "正在停止…", stopConfirm: "停止这个任务？已经算完的结果会保留，没算完的这一轮会丢掉。",
      noteT: "给 AI 留言 / 追加要求", noteHint: "从下一轮设计开始交给 AI。已冻结的验收标准不会因此改变；要改标准，等结束后用“基于这版继续修改”。",
      notePh: "例如：请改用 MP2315 这类同步降压芯片的思路；或：尽量减少元件数量", noteAdd: "交给 AI", noteAdded: "已记下，第 {n} 轮设计会看到",
      notesList: "你的留言",
      ideal: {
        title: "仅理想仿真通过，不能直接制作",
        lead: "所有冻结的检查都通过了，但下面这些情况说明它还不是能照着做的电路：",
        ideal_sources: "电路里有理想信号源（{d}），相当于开环、理想驱动；真实电路要换成控制器芯片和驱动。",
        no_startup_overshoot_check: "没有检查启动过冲：通电瞬间输出可能远超额定值，会损坏负载。",
        no_load_step_check: "没有检查负载跳变：负载突然变化时输出可能跌出范围。",
        next: "用“基于这版继续修改”，写上“换成真实的控制器芯片，加启动过冲和负载跳变检查”。",
      },
      dl: { title: "下载", netlist: "网表 (.cir)", bom: "元件清单 (.csv)", report: "报告 (.html)", bundle: "全部记录 (.zip)", stl: "打印文件 STL/STEP (.zip)", params: "飞控参数 (.json)" },
      cont: { title: "基于这版继续修改", ph: "写下要改什么，例如：输出改成 3.3V；加软启动；效率提高到 90%", go: "开始新一轮", hint: "会新开一个任务：原需求 + 你的修改 + 这一版电路，交给 AI 重新起草标准并设计。" },
      manual: {
        what: "为什么是英文：提示词是写给 AI 的，里面有仿真规则和必须遵守的回复格式。你不用看懂，原样复制即可。",
        open: "打开 AI 网页：", tip: "把 AI 的整段回复粘贴进来就行，RLSOK 会自动找出里面的 JSON。",
      },
      noJson: "回复里没找到 JSON。请让 AI“只回复一个 ```json 代码块”，再粘贴整段回复。这次不算一轮。",
      err: {
        blocked_by_windows: ["Windows 拦截了 RLSOK 的一个文件。", "这台电脑开着“智能应用控制”，它会拦下没有代码签名的程序文件，而 RLSOK 目前没有代码签名。", "现在可以：① 先用电路功能（实测未被拦）；② 用一台没开智能应用控制的电脑；③ 如果你确定要在这台电脑上用，可以在 Windows 安全中心关掉智能应用控制——注意：在部分 Windows 版本上，关掉后不能再打开。", "sac_help"],
        ram_low: ["内存不够。", "这个任务需要的内存超过了电脑当前可用的内存。", "关掉浏览器标签和其他大程序后重试。", "retry"],
        files_missing: ["RLSOK 缺少文件。", "安装不完整，或者被杀毒软件删除了。", "从官网重新下载安装（你的数据会保留）。", "reinstall"],
        ai_format: ["AI 的回复格式不对。", "RLSOK 需要 AI 回复一个 JSON 代码块，这次没有找到。", "请让 AI“只回复一个 ```json 代码块”后再试。", "retry"],
        ai_key_empty: ["还没填 API key。", "", "先在上面的框里粘贴 key，再点“测试连接”。没有 key？点下面的链接申请，或者选“先不连”。", null],
        unknown: ["任务没有完成。", "RLSOK 还不认识这个错误。最后一条信息：{last}", "点“重试”；如果还失败，点“复制错误信息”发给我们。", "retry"],
      },
      act: { sac_help: "打开 Windows 安全设置", reinstall: "打开下载页", copy_log: "复制错误信息" },
      keyHelp: "没有 API key？", keyLinks: "Claude：console.anthropic.com · OpenAI：platform.openai.com · 也可以选“先不连”",
      failedRec: "没完成的任务", openJob: "打开",
      failedChecks: "没通过的检查：",
      rej: {
        title: "上一次 AI 的回复没被接受（不算设计失败）",
        spec: "这是起草验收标准的第 {a} 次机会（共 {n} 次）。原因已经写进下面的新提示词，把它再交给 AI 即可。",
        round: "这一轮算一次尝试。原因已经写进下面的新提示词，把它再交给 AI 即可。",
        other: "平台检查没通过：{e}",
        codes: {
          POWER_SPEC_MISSING_CHECKS: "这是电源类需求，验收标准必须包含“启动过冲”和“负载跳变”两项检查，上一份标准里缺了。",
          NO_JSON_OBJECT_IN_AI_RESPONSE: "回复里没找到 JSON 代码块。",
          SPEC_MISSING_FIELDS: "验收标准缺少必填项。",
        },
      },
      stoppedT: "你停止了这个任务。", stoppedP: "已经算完的轮次和文件都保留在记录里；点“重试”会用同样的需求重新开始一个任务。",
    },
    en: {
      askPh: "e.g. 12 V to 3.3 V, 1 A load, efficiency above 85%",
      scope: "Can do: circuit design and verification (your AI designs, ngspice simulates) · bionic bird design (results unverified) · robot walking tests and training (no AI needed). Cannot yet: design a walking robot from a sentence.",
      robotLimit: "Note: robots can only run walking tests and training for now; RLSOK cannot design robots yet. This runs a walking test of {r}.",
      exSub: "Every example is a real run. G1 about 1 min; circuits about 8 min (incl. 30 tolerance simulations).",
      p_bird: "Bionic bird",
      rs: { FAIL: "Failed" }, waveBird: "Flight simulation · altitude over 60 s with one gust (unverified)",
      birdNote: "The bird design flow works, but every result is labelled unverified: the bird aerodynamics and attitude models are not validated on public data for this configuration.",
      simUnv: { title: "Meets the design spec in simulation (unverified)", next: "Download the parts list, print files and controller parameters to build a prototype; upload flight data under “real-machine data” to calibrate and validate the engines." },
      birdChecks: true,
      robots: { g1_velocity_flat: "G1 humanoid", open_duck_mini_v2: "Open Duck mini biped" },
      pipe: { robot_bench: "Robot walking test", bird: "Bionic bird design" },
      state: { stopped: "stopped" },
      v: { STOPPED: "Stopped", IDEAL_ONLY: "Ideal simulation only", SIM_UNVERIFIED: "Simulation pass (unverified)" },
      checkName: {
        vout_regulation: "Output voltage", vout_ripple: "Output ripple", ripple: "Ripple", efficiency: "Efficiency", load_current: "Load current",
        startup_overshoot: "Start-up overshoot", load_step: "Load step", input_current_limit: "Input current",
        platform_internal_source_power: "Ideal source power (platform check)",
        mass: "Flying mass", endurance: "Endurance", trim_feasible: "Level flight possible (force residual)", motor_speed_margin: "Motor speed margin",
        motor_power_margin: "Motor power margin", flight_60s: "60 s closed-loop flight", pitch_lift_reserve: "Stall margin (lift ratio at alpha +3°)", mission_sweep: "Full mission sim (launch, cruise, turn, landing; 16 runs)", printable: "Printed parts that do not fit", required_parts: "Missing required parts",
        budget_reference: "Reference price total",
      },
      cap: { sac: ["Windows Smart App Control", ""] },
      why: { sac_on: "Smart App Control is on. RLSOK is not code-signed yet, so Windows may block files the robot features use (circuits were not blocked in our test). When it happens, RLSOK explains why and what you can do.",
             sac_robot: "May be blocked by Windows Smart App Control (blocked on our test PC). Circuits work." },
      fix: { sac_help: "Open Windows Security" }, duckReady: "Downloaded to this PC, no AI needed",
      elapsed: "Running for {s} s (measured: usually about {n} min)",
      progNominal: "Round {r}: simulating the nominal case", progTol: "Round {r}: tolerance simulation {d}/{t}",
      stop: "Stop", stopping: "Stopping…", stopConfirm: "Stop this job? Finished results are kept; the round in progress is lost.",
      noteT: "Message to the AI / extra requirement", noteHint: "Given to the AI from the next design round. The frozen acceptance criteria do not change; to change them, use “continue from this version” when it ends.",
      notePh: "e.g. use a synchronous buck controller like MP2315; or: use fewer parts", noteAdd: "Send to AI", noteAdded: "Noted; round {n} will see it",
      notesList: "Your messages",
      ideal: {
        title: "Ideal simulation only — do not build as is",
        lead: "Every frozen check passed, but these points mean it is not yet a circuit you can build:",
        ideal_sources: "It uses ideal signal sources ({d}): open loop with ideal drivers. A real circuit needs a controller IC and drivers.",
        no_startup_overshoot_check: "Start-up overshoot was not checked: the output may spike far above its rating at power-on.",
        no_load_step_check: "A load step was not checked: the output may drop out of range when the load changes.",
        next: "Use “continue from this version” with: use a real controller IC; add start-up overshoot and load-step checks.",
      },
      dl: { title: "Download", netlist: "Netlist (.cir)", bom: "Parts list (.csv)", report: "Report (.html)", bundle: "All records (.zip)", stl: "Print files STL/STEP (.zip)", params: "Controller parameters (.json)" },
      cont: { title: "Continue from this version", ph: "What to change, e.g. output 3.3 V; add soft start; efficiency 90%", go: "Start a new run", hint: "Starts a new job: original request + your change + this circuit, sent to the AI to draft criteria and design again." },
      manual: {
        what: "Why English: the prompt is written for the AI and contains simulation rules and a required reply format. Copy it as is.",
        open: "Open an AI website:", tip: "Paste the AI's whole reply; RLSOK finds the JSON in it.",
      },
      noJson: "No JSON found in the reply. Ask the AI to “reply with one ```json code block only”, then paste the whole reply. This did not count as a round.",
      err: {
        blocked_by_windows: ["Windows blocked one of RLSOK's files.", "Smart App Control is on; it blocks program files without a code signature, and RLSOK has none for now.", "For now: ① use circuits (not blocked in our test); ② use a PC without Smart App Control; ③ if you must use this PC, turn Smart App Control off in Windows Security — on some Windows versions it cannot be turned back on.", "sac_help"],
        ram_low: ["Not enough memory.", "This job needs more memory than is free.", "Close browser tabs and other large programs, then retry.", "retry"],
        files_missing: ["RLSOK is missing files.", "The installation is incomplete or antivirus removed files.", "Download and install again from the website (your data is kept).", "reinstall"],
        ai_format: ["The AI's reply has the wrong format.", "RLSOK needs one JSON code block in the reply and found none.", "Ask the AI to “reply with one ```json code block only”, then retry.", "retry"],
        ai_key_empty: ["No API key yet.", "", "Paste a key in the box above, then press Test. No key? Use the links below, or choose “not now”.", null],
        unknown: ["The job did not finish.", "RLSOK does not recognise this error yet. Last message: {last}", "Press Retry; if it fails again, press “copy error” and send it to us.", "retry"],
      },
      act: { sac_help: "Open Windows Security", reinstall: "Open download page", copy_log: "Copy error" },
      keyHelp: "No API key?", keyLinks: "Claude: console.anthropic.com · OpenAI: platform.openai.com · or choose “not now”",
      failedRec: "Jobs that did not finish", openJob: "Open",
      failedChecks: "Failed checks: ",
      rej: {
        title: "The AI's last reply was not accepted (not a design failure)",
        spec: "This is attempt {a} of {n} to draft the acceptance criteria. The reason is in the new prompt below; give it to the AI again.",
        round: "This round counts as an attempt. The reason is in the new prompt below; give it to the AI again.",
        other: "Platform check failed: {e}",
        codes: {
          POWER_SPEC_MISSING_CHECKS: "This is a power supply: the criteria must include start-up overshoot and load-step checks, and the last draft had neither.",
          NO_JSON_OBJECT_IN_AI_RESPONSE: "No JSON code block in the reply.",
          SPEC_MISSING_FIELDS: "The criteria are missing required fields.",
        },
      },
      stoppedT: "You stopped this job.", stoppedP: "Finished rounds and files stay in the records; Retry starts a new job with the same request.",
    },
  };
  const merge = (a, b) => { for (const [k, v] of Object.entries(b)) { if (v && typeof v === "object" && !Array.isArray(v) && a[k] && typeof a[k] === "object" && !Array.isArray(a[k])) merge(a[k], v); else a[k] = v; } };
  merge(window.RLSOK_TEXT.zh, X.zh);
  merge(window.RLSOK_TEXT.en, X.en);

  // CAD add-on strings for bird design (separate object: these keys already exist above and must be merged, not replaced)
  const B = {
    zh: {
      cap: { bird_cad: ["仿生鸟设计（需要 CAD 组件）", "可以运行 AI 写的结构代码，导出 STL/STEP 打印文件。"] },
      why: { no_cad: "还没安装 CAD 组件（约 100 MB 下载，一次性）。只有仿生鸟设计需要它。" },
      fix: { install_cad: "一键安装 CAD 组件" },
      act: { install_cad: "安装 CAD 组件" },
      pipe: { install_cad: "安装 CAD 组件" },
      err: { cad_missing: ["缺少 CAD 组件。", "仿生鸟设计要运行 AI 写的结构代码，需要 CAD 组件（约 100 MB，一次性下载，下载后会核对 SHA256）。", "点下面的按钮安装，装好后点“重试”。", "install_cad"] },
    },
    en: {
      cap: { bird_cad: ["Bionic bird design (needs the CAD component)", "Runs the AI-written structure code and exports STL/STEP print files."] },
      why: { no_cad: "The CAD component is not installed (about 100 MB, once). Only bird design needs it." },
      fix: { install_cad: "Install the CAD component" },
      act: { install_cad: "Install CAD component" },
      pipe: { install_cad: "Install CAD component" },
      err: { cad_missing: ["The CAD component is missing.", "Bird design runs the AI-written structure code and needs the CAD component (about 100 MB, downloaded once and checked against its SHA256).", "Install it with the button below, then press Retry.", "install_cad"] },
    },
  };
  merge(window.RLSOK_TEXT.zh, B.zh);
  merge(window.RLSOK_TEXT.en, B.en);

  // simulation viewer strings + bird R1 example
  const V = {
    zh: { replaySim: "看仿真回放（3D）", replaySimBadge: "回放：这是一次已经完成的仿真记录，不是现在运行的，也不是真机画面", vizWhat: "这段回放是仿真，不是真机", ex: { bird_r2: ["仿生鸟 R2 · 航线仿真回放", "像真鸟的原创外形（海鸥/鸢比例，翼尖初级飞羽、扇形尾），翼展 1.46 m：手抛、爬升、平飞、360° 转弯、降落，24 组仿真全过。3D 回放可拖动视角、切换鸟的视角。", "仿真 · 未验证"] } },
    en: { replaySim: "Watch the 3-D simulation replay", replaySimBadge: "Replay: a finished simulation record, not running now and not footage of real hardware", vizWhat: "This replay is a simulation, not footage of real hardware", ex: { bird_r2: ["Bionic bird R2 · mission replay", "An original bird-like shape (gull / kite proportions, separated primaries, fan tail), 1.46 m span: hand launch, climb, cruise, 360° turn, landing; all 24 simulated runs pass. Drag to orbit, switch to the bird's view.", "Simulation · unverified"] } },
  };
  merge(window.RLSOK_TEXT.zh, V.zh);
  merge(window.RLSOK_TEXT.en, V.en);
})();
