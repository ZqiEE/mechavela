"use strict";
const FAQ = {
  zh: [
    ["RLSOK 是什么？", "一个在你电脑上运行的工程软件：你的 AI 负责设计，RLSOK 用成熟的求解器（ngspice、MuJoCo）算出真实数字，按事先冻结的标准判定通过或失败。"],
    ["需要显卡吗？", "电路和机器人行走基准不需要。只有“训练机器人”需要 NVIDIA 显卡；没有的话，可以用你自己租的云 GPU。"],
    ["我的 API key 安全吗？", "key 只保存在你这台电脑（设置里会显示具体路径），用 Windows DPAPI 加密，只有你当前的 Windows 账户能解开。RLSOK 不会上传它，也不会写进日志或任务记录。"],
    ["状态是什么意思？", "只有四种：真机验证通过（造出实物并实测达标）、仿真通过，未经真机验证（只在仿真里达到事先登记的标准）、进行中、未通过。标准在运行前登记，运行后不改；没通过就写“未通过”。"],
    ["“看真实记录（回放）”和“运行”有什么区别？", "回放是之前已经完成的一次真实运行，原样展示；运行是现在在你电脑上重新跑一次。回放会明确标出“回放”。"],
    ["没连接 AI 能用吗？", "能。G1 机器人示例不需要 AI。电路任务可以选“手动转交”：RLSOK 给你提示词，你复制给任意 AI，再把回复粘贴回来。"],
    ["关掉窗口会怎样？", "正在运行的任务会在后台继续，完成后 Windows 会通知你。要彻底退出，点右上角“退出”。"],
    ["训练要多久？", "Open Duck 小型双足在 GTX 1650 Ti 上约 6 小时（3 亿步，实测）。大型人形 G1 实测显存峰值 2.7 GB，按原版配置完整训练估计约 24 小时（未跑完，属估算）；想更快可以用云 GPU。"],
    ["Open Duck 为什么要单独下载？", "它作者的训练仓库没有许可证，外形仿制 Disney 角色，所以 RLSOK 不分发它的文件；你从作者的仓库下载到自己电脑，仅供研究。"],
    ["Windows 提示“已保护你的电脑”怎么办？", "RLSOK 安装器目前没有代码签名证书。点“更多信息”→“仍要运行”。你也可以先核对下载页给出的 SHA256。"],
    ["我装了 Claude Code 或 Codex，怎么连上？", "打开“设置”，RLSOK 会自己找到它们，点“用 Claude Code”就行，不需要 API key。它会马上发一句话测试；如果说“没有正常回答”，先在终端里运行一次 claude（或 codex）登录，再点“测试连接”。"],
    ["没有 API key，也没装命令行 AI？", "选“先不连”。需要 AI 时，RLSOK 给你一段提示词，复制到任意聊天 AI（Claude、ChatGPT、DeepSeek 都行），再把回复粘贴回来。"],
    ["“智能应用控制”是什么？会影响我吗？", "Windows 11 的一项保护：它会拦截没有代码签名的程序文件。RLSOK 还没有签名，所以开着它时机器人功能可能被拦（电路功能实测不受影响）。被拦时 RLSOK 会写明原因。要不要改这个设置由你决定（先看微软对它的说明），RLSOK 不会替你改。"],
    ["双击 RLSOK 后弹出“RLSOK 没能启动”", "提示里写了原因和日志位置（%LOCALAPPDATA%\\RLSOK\\data\\console.log）。最常见的原因是智能应用控制拦截了文件，见上一条。如果 8765 端口被别的程序占用，RLSOK 会自动换一个端口，不用你处理。"],
    ["自己跑仿生鸟时提示要装 CAD 组件", "仿生鸟设计要运行 AI 写的结构代码，需要 CAD 组件：约 100 MB，只下载一次，下载后会核对 SHA256。只看示例不需要它。"],
    ["训练需要 WSL2，怎么装？", "在“设置 → 这台电脑”里点“安装 WSL2”。Windows 会请你确认管理员权限，装完可能要重启。之后再点“一键安装训练环境”（约 20 分钟）。"],
    ["怎么卸载？", "Windows 设置 → 应用 → 已安装的应用 → RLSOK → 卸载。卸载时可以选择是否保留你的项目数据。"],
    ["数据存在哪里？", "%LOCALAPPDATA%\\RLSOK\\data（任务、结果、设置、加密的 key）。程序本身在 %LOCALAPPDATA%\\Programs\\RLSOK。"],
  ],
  en: [
    ["What is RLSOK?", "Engineering software that runs on your computer: your AI designs, RLSOK computes real numbers with mature solvers (ngspice, MuJoCo) and judges them against criteria frozen in advance."],
    ["Do I need a GPU?", "Not for circuits or the robot walking benchmark. Only robot training needs an NVIDIA GPU; without one, use a cloud GPU you rent yourself."],
    ["Is my API key safe?", "It stays on this computer (Settings shows the path), encrypted with Windows DPAPI so only your Windows account can read it. RLSOK never uploads it or writes it into logs or job records."],
    ["What do the states mean?", "There are only four: verified on hardware (built and measured to pass), passed in simulation (met the pre-registered criteria in simulation only), in progress, and did not pass. Criteria are registered before a run and never changed after it; a miss is shown as “did not pass”."],
    ["Replay vs run?", "A replay shows a real run that already finished, unchanged; run executes it again on your computer now. Replays are clearly marked."],
    ["Can I use it without an AI?", "Yes. The G1 robot example needs no AI. For circuits choose “manual”: RLSOK shows a prompt, you copy it to any AI and paste the reply back."],
    ["What if I close the window?", "Running jobs continue in the background and Windows notifies you when they finish. To quit completely, use “Quit” at the top right."],
    ["How long does training take?", "Open Duck (small biped) takes about 6 h on a GTX 1650 Ti (300M steps, measured). The G1 humanoid peaks at 2.7 GB GPU memory; a full run with the original settings is estimated at about 24 h (not run to completion); a cloud GPU is faster."],
    ["Why is Open Duck a separate download?", "Its author’s training repository has no licence and the robot replicates a Disney character, so RLSOK does not distribute it; you download it from the author’s repository for research."],
    ["Windows says “Windows protected your PC”", "The RLSOK installer has no code-signing certificate yet. Click “More info” → “Run anyway”. You can check the SHA256 on the download page first."],
    ["I have Claude Code or Codex. How do I connect it?", "Open Settings: RLSOK finds them on its own; click “Use Claude Code” and you are done, no API key needed. It sends one test message right away. If it says the AI did not answer properly, run claude (or codex) once in a terminal and sign in, then press Test."],
    ["No API key and no command-line AI?", "Choose “Not now”. When an AI is needed, RLSOK shows a prompt; paste it into any chat AI (Claude, ChatGPT, DeepSeek) and paste the reply back."],
    ["What is Smart App Control? Does it affect me?", "A Windows 11 protection that blocks program files without a code signature. RLSOK is not signed yet, so with it on, robot features may be blocked (circuits were not, in our test). RLSOK says so when it happens. Whether to change that setting is your decision (read Microsoft’s notes on it first); RLSOK never changes it."],
    ["RLSOK shows “RLSOK did not start”", "The message gives the reason and the log location (%LOCALAPPDATA%\\RLSOK\\data\\console.log). The most common cause is Smart App Control blocking a file; see above. If port 8765 is taken by another program, RLSOK picks another port by itself."],
    ["Running the bird myself asks for the CAD component", "Bird design runs the AI-written structure code and needs the CAD component: about 100 MB, downloaded once and checked against its SHA256. Viewing the example does not need it."],
    ["Training needs WSL2. How do I install it?", "In Settings → This PC, click “Install WSL2”. Windows asks for administrator approval and may need a restart. Then click “Install training environment” (about 20 minutes)."],
    ["How do I uninstall?", "Windows Settings → Apps → Installed apps → RLSOK → Uninstall. You can choose to keep your project data."],
    ["Where is my data?", "%LOCALAPPDATA%\\RLSOK\\data (jobs, results, settings, the encrypted key). The program is in %LOCALAPPDATA%\\Programs\\RLSOK."],
  ],
};
let lang = "zh";
try { lang = localStorage.getItem("rlsok.lang") || (navigator.language.startsWith("zh") ? "zh" : "en"); } catch (e) { /* ignore */ }
function apply() {
  document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  document.querySelectorAll("[data-zh]").forEach((e) => { e.textContent = e.dataset[lang]; });
  document.getElementById("lang").textContent = lang === "zh" ? "EN" : "中文";
  const faq = document.getElementById("faq");
  faq.replaceChildren(...FAQ[lang].map(([q, a]) => {
    const d = document.createElement("details"), s = document.createElement("summary"), p = document.createElement("p");
    s.textContent = q; p.textContent = a; d.append(s, p); return d;
  }));
}
document.getElementById("lang").addEventListener("click", () => { lang = lang === "zh" ? "en" : "zh"; try { localStorage.setItem("rlsok.lang", lang); } catch (e) { /* ignore */ } apply(); });
apply();
