import hashlib, json, os, runpy, shutil, sys, zipfile
from pathlib import Path, PurePosixPath
sys.dont_write_bytecode = True
for key in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[key] = "1"
for name in ("run.json", "invocation.json", "runtime-lock.json", "admission.json"):
    shutil.copyfile("/input/" + name, name)
shutil.copyfile("/solver/worker.py", "verification-recipe.py")
def file_sha(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1048576), b""):
            value.update(block)
    return value.hexdigest()
lock = json.loads(Path("runtime-lock.json").read_text(encoding="utf-8-sig"))
runtime = Path("/work/runtime")
runtime.mkdir()
expanded = 0
for index, (source, expected) in enumerate(sorted(lock["archives"].items())):
    archive_path = Path("/archives") / (str(index) + ".whl")
    if file_sha(archive_path) != expected:
        raise ValueError("NATIVE_ARCHIVE_HASH_MISMATCH")
    with zipfile.ZipFile(archive_path) as archive:
        for item in archive.infolist():
            path = PurePosixPath(item.filename)
            if path.is_absolute() or ".." in path.parts or chr(92) in item.filename or ":" in item.filename:
                raise ValueError("UNSAFE_WHEEL_PATH")
            if item.is_dir():
                continue
            if (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("WHEEL_SYMLINK_REJECTED")
            parts = list(path.parts)
            if parts[0].endswith(".data"):
                if parts[1] not in ("purelib", "platlib"):
                    continue
                parts = parts[2:]
            target = runtime.joinpath(*parts)
            if not target.is_relative_to(runtime) or target.exists():
                raise ValueError("WHEEL_PATH_COLLISION")
            expanded += item.file_size
            if expanded > 536870912:
                raise ValueError("EXPANSION_DISK_LIMIT")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as source_file, target.open("xb") as destination:
                shutil.copyfileobj(source_file, destination, 1048576)
expected_files = {PurePosixPath(name).relative_to(lock["runtime"]["directory"]).as_posix(): value for name, value in lock["runtime"]["files"].items()}
actual_files = {p.relative_to(runtime).as_posix(): file_sha(p) for p in runtime.rglob("*") if p.is_file()}
if actual_files != expected_files:
    raise ValueError("EXACT_NATIVE_RUNTIME_FILE_HASHES_REQUIRED")
library = runtime / PurePosixPath(lock["runtime"]["library"]).relative_to(lock["runtime"]["directory"])
verification = {"schema": "rlsok.native-runtime-validation/1", "status": "FROZEN_RUNTIME_EXACTLY_VERIFIED", "file_count": len(actual_files),
                "expanded_bytes": expanded, "library_sha256": file_sha(library), "archives": lock["archives"],
                "runtime_lock_sha256": file_sha("runtime-lock.json")}
Path("runtime-validation.json").write_text(json.dumps(verification, indent=2), encoding="utf-8")
sys.argv = ["/solver/worker.py", "/input/invocation.json", "/project"]
try:
    runpy.run_path("/solver/worker.py", run_name="__main__")
finally:
    final_files = {p.relative_to(runtime).as_posix(): file_sha(p) for p in runtime.rglob("*") if p.is_file()}
    if final_files != expected_files:
        raise ValueError("NATIVE_RUNTIME_CHANGED_DURING_EXECUTION")
    verification["post_execution_verified"] = True
    Path("runtime-validation.json").write_text(json.dumps(verification, indent=2), encoding="utf-8")
