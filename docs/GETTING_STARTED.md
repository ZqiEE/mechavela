# Getting started / 开始使用

## 1. Open the workspace

Install Python 3.12+ from [python.org](https://www.python.org/downloads/). Download and extract this repository and run `python launch.py`. No automatic installer downloads or administrator rights are requested.

Default port: 8890. Use `python launch.py --port 8891` if occupied. Existing services are never stopped automatically.

Default data: `%LOCALAPPDATA%/Mechavela/data` on Windows. Use `--data-dir <new-directory>` for another library. Old RLSOK personal data is explicitly refused as the default launch target. To stop a headless test instance, POST `{}` to `/api/shutdown` with `X-RLSOK: 1` from the same local origin; ordinary users can close/stop the local process. Closing a browser tab alone does not stop the service.

Create a **blank custom project**, enter your requirements, save, and reopen it. This portion needs no CAD engine or model subscription.

## 2. Connect your own AI

Install and sign in to the official [Codex client](https://developers.openai.com/codex/cli/) separately. Use **Connect AI → ChatGPT / Codex** in an open project and review its project permissions. Official login remains in the official client; this app does not provide a subscription or embedded ChatGPT login.

The MCP bridge needs the official Python SDK available in the selected Python environment. Install the SDK from [modelcontextprotocol/python-sdk](https://github.com/modelcontextprotocol/python-sdk) following its documentation. Python/API compatibility depends on the SDK version; the full fresh-user workflow has not been retested in this rebranded source release. No consumer AI credentials or developer connection files are supplied.

API models need an exact model ID, a compatible structured-tool endpoint and your own API credential; a ChatGPT subscription is not an API key. Local inference requires an explicit loopback endpoint.

## 3. Enable engineering tools

**FreeCAD:** obtain an OS-approved official FreeCAD distribution. This code supports an owner-selected external `freecadcmd.exe` host, with pinned native-file identities and a separate functional qualification receipt. The schema/catalog is in `release/cad-runtimes/`; the implementation is `implementation/workspace/freecad_host.py`. The development host was FreeCAD weekly-2026.11.04 / OCCT 8.0.1, but that developer-specific host profile is intentionally not shipped. **A beginner-friendly environment setup wizard is not available in this preview.** Do not forge qualification receipts or turn off OS protections; a successful executable launch alone does not qualify CAD operations.

**Physics:** install a supported official MuJoCo environment separately. The developer's MuJoCo WASM test was a project-level integration, not a bundled universal physics runtime or a pass for the native Python adapter. Missing or blocked dependencies must remain unverified.

**Code execution:** choose the actual local Python/CAD environment and explicitly authorize this project. Project tools are described by `code/tools.json`. Treat generated code like code received from another developer: it has your Windows-user file/network access. Never import someone else's approval profile.

## 4. Ask for an observable result

For a small CAD project, ask your AI to create editable parts, execute the CAD script, save a native file and STEP, declare assembly checks and inspect the actual exported assets. Require it to read tool failures and fix the source. A script without executed CAD output is not a finished design.

Default validation badges only cover their declared engine and checks. A static non-interference result is not strength, motion, manufacturing or physical safety certification.

中文提示：首次可以先体验建项目、保存、重新打开及源码编辑；完整 CAD/AI 流程需要工程工具配置。没有宣传为下载即包含所有运行环境。遇到问题请提交版本、步骤和脱敏错误，不要上传密钥或个人数据。
