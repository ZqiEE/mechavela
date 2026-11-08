# Public source preview validation — 2026-11-08

Executed locally on Windows with installed CPython 3.15. No pip install, native CAD DLL load, private AI login change or OS protection change was required for these checks.

Command:

```sh
python -m unittest scripts.test_workspace_session scripts.test_freecad_host scripts.test_preview -v
```

**19 tests passed in 13.725 seconds.** Five project-session tests, ten FreeCAD host-boundary/geometry-adapter tests and four actual source-launcher HTTP tests. Host-boundary unit tests use explicitly non-executable fake native files; they are not represented as real CAD functional validation.

Actual source process checks covered brand identity, isolated data, reuse of the same service, refusal to replace an occupied port, refusal of the old personal data root, create project → save session → stop service → restart → restore project → export a real project ZIP.

Actual browser check at a separate local port covered new Mechavela title/brand, empty private library, creation of `My Mechavela Robot`, Saved state, requirements file and visible workspace. The included screenshot comes from that new test project, not an old user project. Browser startup had no captured warning/error console messages at the time checked.

An initial attempt to run unittest using the legacy embedded CPython failed because its isolated `_pth` excludes the source checkout. Re-run with the installed normal Python passed. The documented source launch uses a normal Python installation; no embedded interpreter is shipped.

Port8890 was already occupied during browser setup. The launcher refused it and the browser test used port8898; the existing listener was not stopped.

The source secret/size scanner passed before publication. A separate identifier search found none of the developer's known personal account/path/server identifiers in the public snapshot. No private run archive, owner CAD profile, API credential, grant/approval file, data library, embedded Python or native tool runtime is included. This is a bounded scan, not a promise that arbitrary future content is safe to publish.

Full AI/CAD/MuJoCo workflows were not rerun after branding. Earlier development evidence is summarized separately in CAPABILITIES.md, with its exact scope and limitations. The source release is not the previous signed/native installer release gate and does not claim that gate passed.
