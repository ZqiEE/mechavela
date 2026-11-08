# Incremental synthesis engine

Product execution capability, not an external AI implementation exercise.
The existing CLI now has synthesis-create/run/link/status. Owner authorization
is required. Inputs are frozen elaborated RTLIL and its JSON companion with
SHA256 identities; this engine never edits or re-elaborates GPU RTL.

create freezes source/role/tool/library/config receipt, exact recursive class
identity and a user-supplied CPU/wall ceiling (up to7200/3600 in this release),
4GiB/task, concurrency1, spend0. It traces passive SV interface outputs and
removes only redundant partition pins with no local driver/consumer and a
real driver in every parent invocation. The original HDL and all original
net connections/drivers stay frozen; this is an IR boundary transformation.

run stages: map/proof/export/resume. Map uses bounded opt-fast-noff, preserved
architectural boundaries, memory lowering, standard-cell FF mapping and the
native supported ABC strash/get/nf/put script. Checkpoints allow resuming only
the uncompleted ABC part with exact key/source hashes. Cached successes are
not rerun. Old mapped assets require independent exact binding checks.

proof keeps original state names and initialization metadata, makes child
outputs common free inputs and compares child inputs as well as local outputs.
PASS means local conditional equivalence; full behavior requires all child
certificates and real boundary models. No reset/protocol assumptions added.

link refuses missing artifacts and uses only read/hierarchy/check/write;
no complete GPU flatten/global opt/ABC. Physical SRAM placeholders are listed
separately; source-exact mapped logic is not full GPU behavior, PPA or silicon.

Per-batch full reservation is checked before launch using existing Sandbox
limits. Actual CPU includes cgroup execution plus driver CPU; active wall time
includes preprocessing and artifact collection. Events, raw logs, exits,
memory peaks, cache manifests and rejected operations are inspectable in the
session directory. Failures/timeouts remain separate from past successes.

CLI arguments are JSON with directory and relevant Session method arguments.
Paths inside JSON use execution-host paths (/mnt/d or /opt on this machine).
Examples of existing session control:

    atoms.ps1 synthesis-status --args-file examples/synthesis-status.json
    atoms.ps1 synthesis-run --args-file examples/synthesis-batch.json

The example batch is a bounded map request; it reuses prior successes and
does not authorize an increase to the session's frozen resource ceiling.
