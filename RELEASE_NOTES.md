# Mechavela 0.3.2-alpha.1 — Your AI robotics workspace

**Your AI. Real CAD. Real simulation.**

First public Mechavela source technology preview, based on the existing RLSOK dev.24 workbench. It provides project/code management, project-scoped AI tool interfaces, real CAD assembly inspection/display integration and external physics interfaces.

## Try it

1. Install normal Python 3.12+ on Windows.
2. Download `Mechavela-0.3.2-alpha.1-source.zip` and extract it.
3. Double-click `Launch-Mechavela.cmd`, or run `python launch.py`.
4. Create a blank project. See `docs/GETTING_STARTED.md` to configure your own AI and engineering tools.

**This download is source code, not an all-in-one runtime or signed installer.** Basic project management runs with the Python standard library. CAD, physics and MCP need separate supported environments; the developer's native profiles and private projects are not distributed.

Default data: `%LOCALAPPDATA%/Mechavela/data`. Default port:8890; choose another with `--port` if occupied. Old RLSOK data/installations are preserved. Internal protocol names remain compatible.

## Checked for this release

- 19 local tests passed: session persistence, CAD-host boundaries, actual HTTP launch/reuse/restart/export and data/port protection.
- Actual browser: new brand, create blank project, Saved state and requirements display.
- Public source secret/size scan and known-private-identifier scan clean. Release archive file hashes and ZIP CRC checked during packaging.

See `docs/VALIDATION.md`. Existing real engineering evidence and its limits are documented in `docs/CAPABILITIES.md`; it is not a new-machine out-of-box claim.

## Known boundaries

External CAD environment setup still requires engineering configuration. No second clean PC or fresh AI account has passed full acceptance for this source release. Local execution is trusted user code, not a file/network sandbox. Native Windows signing/distribution remains separate. Some inherited reference panels need assets not shipped here.

Original mechanical-spider work demonstrated a 465-solid editable assembly and a scoped 10s fixed-base folding test. It did not establish ground transformation, walking, rolling, manufacturing or hardware safety.

No new open-source license is selected by this release; read LICENSE-STATUS.md. Third-party notices are retained.

---

**中文：Mechavela 首个源码技术预览发布。** 连接自己的 AI，管理机器人项目代码，集成真实 CAD、装配检查、精细 3D 和物理仿真。基础工作区可运行，完整工程能力需另配工具。提供中英文入门、真实验证范围和问题反馈入口；没有上传原用户工程或替换旧安装。欢迎体验并提交脱敏反馈。
