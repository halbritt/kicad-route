"""Opaque-box process drivers, boundary audit hook, and RE-08 metrics."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import time
from typing import Any


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def parse_last_json_line(text: str) -> dict[str, Any] | None:
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


@dataclass(frozen=True)
class TimeMetrics:
    wall_s: float
    user_s: float
    sys_s: float
    cpu_s: float
    max_rss_kib: int
    exit_status: int | None
    signal_num: int | None


def _parse_elapsed_seconds(raw: str) -> float:
    parts = [float(p) for p in raw.strip().split(":")]
    if len(parts) == 2:
        return parts[0] * 60.0 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600.0 + parts[1] * 60.0 + parts[2]
    return float(raw)


def parse_gnu_time_v(metrics_path: Path) -> TimeMetrics | None:
    if not metrics_path.exists():
        return None
    text = metrics_path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return None
    user_s: float | None = None
    sys_s: float | None = None
    wall_s: float | None = None
    max_rss_kib: int | None = None
    exit_status: int | None = None
    signal_num: int | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Command terminated by signal "):
            try:
                signal_num = int(stripped.rsplit(None, 1)[-1])
            except ValueError:
                pass
            continue
        if ": " not in line:
            continue
        key, val = line.split(": ", 1)
        key = key.strip()
        val = val.strip()
        if key == "User time (seconds)":
            user_s = float(val)
        elif key == "System time (seconds)":
            sys_s = float(val)
        elif key == "Elapsed (wall clock) time (h:mm:ss or m:ss)":
            wall_s = _parse_elapsed_seconds(val)
        elif key == "Maximum resident set size (kbytes)":
            max_rss_kib = int(val)
        elif key == "Exit status":
            exit_status = int(val)
        elif key == "Command terminated by signal":
            signal_num = int(val)
    if (
        user_s is None
        or sys_s is None
        or wall_s is None
        or max_rss_kib is None
        or (exit_status is None and signal_num is None)
    ):
        return None
    return TimeMetrics(
        wall_s=wall_s,
        user_s=user_s,
        sys_s=sys_s,
        cpu_s=user_s + sys_s,
        max_rss_kib=max_rss_kib,
        exit_status=exit_status,
        signal_num=signal_num,
    )


@dataclass
class MeasuredProcessResult:
    cmd: list[str]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    wall_measured_s: float
    metrics: TimeMetrics | None
    audit_log_path: Path | None = None
    audit_rows: list[dict[str, Any]] = field(default_factory=list)


def _terminate_process_group(
    pgid: int, proc: subprocess.Popen[str] | None = None
) -> None:
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if proc is not None:
            proc.poll()
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.02)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


_PGID_LAUNCHER = (
    "import os, sys; "
    "os.setpgrp(); "
    "tmp = sys.argv[1] + '.tmp'; "
    "open(tmp, 'w', encoding='utf-8').write(str(os.getpid())); "
    "os.replace(tmp, sys.argv[1]); "
    "os.execvp(sys.argv[2], sys.argv[2:])"
)


def run_measured(
    cmd: list[str],
    *,
    cwd: Path,
    work_dir: Path,
    env: dict[str, str] | None = None,
    timeout_s: float = 60.0,
    rlimit_as_bytes: int | None = None,
    cancel_after_s: float | None = None,
    cancel_signal: int = signal.SIGTERM,
    metrics_name: str | None = None,
) -> MeasuredProcessResult:
    work_dir.mkdir(parents=True, exist_ok=True)
    tag = metrics_name or f"metrics-{time.time_ns()}"
    metrics_path = work_dir / f"{tag}.time"
    pgid_path = work_dir / f"{tag}.pgid"
    inner_cmd: list[str] = []
    if rlimit_as_bytes is not None:
        inner_cmd.extend(["prlimit", f"--as={int(rlimit_as_bytes)}", "--"])
    inner_cmd.extend(str(c) for c in cmd)

    wrapped: list[str] = ["/usr/bin/time", "-v", "-o", str(metrics_path)]
    if cancel_after_s is not None:
        pgid_path.unlink(missing_ok=True)
        wrapped.extend(
            ["/usr/bin/python3", "-S", "-c", _PGID_LAUNCHER, str(pgid_path), *inner_cmd]
        )
    else:
        wrapped.extend(inner_cmd)

    t0 = time.monotonic()
    proc = subprocess.Popen(
        wrapped,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    timed_out = False
    stdout = ""
    stderr = ""
    child_pgid: int | None = None
    try:
        if cancel_after_s is not None:
            try:
                stdout, stderr = proc.communicate(timeout=cancel_after_s)
            except subprocess.TimeoutExpired:
                wait_dl = time.monotonic() + 0.5
                while (
                    not pgid_path.is_file()
                    and proc.poll() is None
                    and time.monotonic() < wait_dl
                ):
                    time.sleep(0.005)
                if pgid_path.is_file():
                    try:
                        child_pgid = int(pgid_path.read_text(encoding="utf-8").strip())
                    except ValueError:
                        child_pgid = None
                target_pgid = child_pgid if child_pgid is not None else proc.pid
                try:
                    os.killpg(target_pgid, cancel_signal)
                except ProcessLookupError:
                    pass
                try:
                    stdout, stderr = proc.communicate(timeout=timeout_s)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    if child_pgid is not None and child_pgid != proc.pid:
                        _terminate_process_group(child_pgid)
                    _terminate_process_group(proc.pid, proc=proc)
                    stdout, stderr = proc.communicate()
        else:
            stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process_group(proc.pid, proc=proc)
        stdout, stderr = proc.communicate()
    finally:
        if child_pgid is not None and child_pgid != proc.pid:
            _terminate_process_group(child_pgid)
        if proc.poll() is None:
            _terminate_process_group(proc.pid, proc=proc)
            proc.wait()
        pgid_path.unlink(missing_ok=True)
        Path(f"{pgid_path}.tmp").unlink(missing_ok=True)

    wall_measured_s = time.monotonic() - t0
    return MeasuredProcessResult(
        cmd=[str(c) for c in cmd],
        returncode=int(proc.returncode if proc.returncode is not None else -1),
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        wall_measured_s=wall_measured_s,
        metrics=parse_gnu_time_v(metrics_path),
    )


@dataclass(frozen=True)
class BuildProvenance:
    repo_root: Path
    git_head: str
    git_dirty: bool
    cargo_lock_sha256: str
    binary_path: Path
    binary_sha256: str
    kicad_cli_version: str
    pcbnew_version: str


def _git_output(repo_root: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return res.stdout.strip()


def ensure_engine_binary(
    repo_root: Path,
    *,
    binary_override: Path | None = None,
    profile: str = "debug",
) -> BuildProvenance:
    repo_root = repo_root.resolve()
    manifest = repo_root / "rust-engine" / "Cargo.toml"
    lock_file = repo_root / "rust-engine" / "Cargo.lock"
    override_env = os.environ.get("KICAD_ROUTE_ENGINE_BIN")
    explicit_bin = binary_override or (Path(override_env) if override_env else None)

    if explicit_bin is not None:
        bin_path = explicit_bin.resolve()
        if not bin_path.is_file() or not os.access(bin_path, os.X_OK):
            raise RuntimeError(f"engine binary override is not executable: {bin_path}")
    else:
        build_cmd = [
            "cargo",
            "build",
            "--locked",
            "--offline",
            "--manifest-path",
            str(manifest),
        ]
        if profile == "release":
            build_cmd.append("--release")
        build_res = subprocess.run(
            build_cmd,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=300,
        )
        if build_res.returncode != 0:
            raise RuntimeError(
                f"cargo build failed (exit {build_res.returncode}):\n{build_res.stderr.strip()}"
            )
        meta_res = subprocess.run(
            [
                "cargo",
                "metadata",
                "--no-deps",
                "--offline",
                "--manifest-path",
                str(manifest),
                "--format-version",
                "1",
            ],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=60,
        )
        if meta_res.returncode != 0:
            raise RuntimeError(f"cargo metadata failed:\n{meta_res.stderr.strip()}")
        meta = json.loads(meta_res.stdout)
        target_dir = Path(meta["target_directory"])
        bin_path = (target_dir / profile / "kicad-route-engine").resolve()
        if not bin_path.is_file() or not os.access(bin_path, os.X_OK):
            raise RuntimeError(f"built engine binary missing or not executable: {bin_path}")

    kicad_cli_res = subprocess.run(
        ["kicad-cli", "--version"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if kicad_cli_res.returncode != 0:
        raise RuntimeError(f"kicad-cli --version failed: {kicad_cli_res.stderr.strip()}")

    import pcbnew

    git_head = _git_output(repo_root, "rev-parse", "HEAD")
    git_dirty = bool(_git_output(repo_root, "status", "--porcelain"))
    return BuildProvenance(
        repo_root=repo_root,
        git_head=git_head,
        git_dirty=git_dirty,
        cargo_lock_sha256=sha256_file(lock_file),
        binary_path=bin_path,
        binary_sha256=sha256_file(bin_path),
        kicad_cli_version=kicad_cli_res.stdout.strip(),
        pcbnew_version=str(pcbnew.Version()),
    )


class GuardedEnv:
    """Disposable sitecustomize.py sys.addaudithook blocking legacy Python router code."""

    def __init__(self, repo_root: Path, guard_dir: Path) -> None:
        self.repo_root = repo_root.resolve()
        self.guard_dir = guard_dir.resolve()
        self.guard_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.guard_dir / "audit.jsonl"
        self.nonce = secrets.token_hex(16)
        site_py = self.guard_dir / "sitecustomize.py"
        site_py.write_text(
            f"""import json, pathlib, sys
root = pathlib.Path({str(self.repo_root)!r}).resolve()
allowed = (root / 'tools' / 'rust_engine_kicad.py').resolve()
log = pathlib.Path({str(self.log_path)!r})
nonce = {self.nonce!r}
def record(row):
    with log.open('a', encoding='utf-8') as out:
        out.write(json.dumps(row) + '\\n')
def check(event, args):
    if event != 'exec':
        return
    code_obj = args[0]
    filename = code_obj.co_filename
    if filename.startswith('<'):
        if filename == '<string>' or (
            filename.startswith('<frozen ') and filename.endswith('>') and '\\n' not in filename
        ):
            return
        record({{'path': filename, 'co_name': code_obj.co_name, 'allowed': False}})
        raise RuntimeError('ARCHITECTURE_BOUNDARY: disallowed synthetic code object: ' + filename)
    path = pathlib.Path(filename).resolve()
    if path.is_relative_to(root) or '/src/kicad_route/' in path.as_posix():
        ok = (path == allowed)
        record({{'path': str(path), 'allowed': ok}})
        if not ok:
            raise RuntimeError('ARCHITECTURE_BOUNDARY: project Python policy is forbidden: ' + str(path))
record({{'guard_started': True, 'nonce': nonce}})
sys.addaudithook(check)
""",
            encoding="utf-8",
        )

    def build_env(self, base_env: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ if base_env is None else base_env)
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(self.guard_dir) + (os.pathsep + existing if existing else "")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env

    def read_rows_since(self, start_line: int = 0) -> list[dict[str, Any]]:
        if not self.log_path.exists():
            return []
        lines = self.log_path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines[start_line:] if line.strip()]

    def line_count(self) -> int:
        if not self.log_path.exists():
            return 0
        return len(self.log_path.read_text(encoding="utf-8").splitlines())

    def assert_clean(self, rows: list[dict[str, Any]], *, require_started: bool = True) -> None:
        if require_started:
            started = sum(
                1
                for r in rows
                if r.get("guard_started") is True and r.get("nonce") == self.nonce
            )
            if started < 1:
                raise AssertionError(
                    f"boundary guard did not start with valid nonce in child Python process: {rows}"
                )
        disallowed = [r for r in rows if r.get("allowed") is False]
        if disallowed:
            raise AssertionError(f"ARCHITECTURE_BOUNDARY violation detected: {disallowed}")


@dataclass
class WireCallResult:
    proc: MeasuredProcessResult
    stderr_json: dict[str, Any] | None
    output_path: Path | None
    output_bytes: bytes | None
    output_json: dict[str, Any] | None


@dataclass
class RouteE2EResult:
    export_res: WireCallResult
    engine_res: WireCallResult | None
    apply_res: WireCallResult | None
    request_path: Path
    plan_path: Path
    output_pcb: Path
    stage_failed: str | None = None


class JsonWireDriver:
    """Opaque-box driver for tools/rust_engine_kicad.py + kicad-route-engine."""

    def __init__(self, repo_root: Path, binary_path: Path, work_dir: Path) -> None:
        self.repo_root = repo_root.resolve()
        self.binary_path = binary_path.resolve()
        self.work_dir = work_dir.resolve()
        self.bridge_script = (self.repo_root / "tools" / "rust_engine_kicad.py").resolve()
        self.guard = GuardedEnv(self.repo_root, self.work_dir / "boundary-guard")

    def _run_guarded_python(
        self,
        cmd: list[str],
        *,
        timeout_s: float = 60.0,
        rlimit_as_bytes: int | None = None,
        metrics_name: str | None = None,
        require_guard_started: bool = True,
        assert_boundary_clean: bool = True,
    ) -> MeasuredProcessResult:
        start_idx = self.guard.line_count()
        res = run_measured(
            cmd,
            cwd=self.repo_root,
            work_dir=self.work_dir / "metrics",
            env=self.guard.build_env(),
            timeout_s=timeout_s,
            rlimit_as_bytes=rlimit_as_bytes,
            metrics_name=metrics_name,
        )
        rows = self.guard.read_rows_since(start_idx)
        res.audit_log_path = self.guard.log_path
        res.audit_rows = rows
        if assert_boundary_clean:
            self.guard.assert_clean(rows, require_started=require_guard_started)
        return res

    def export_board(
        self,
        board_path: Path,
        request_out: Path,
        rules_path: Path,
        *,
        max_expansions: int = 100_000,
        limits_path: Path | None = None,
        timeout_s: float = 60.0,
    ) -> WireCallResult:
        cmd = [
            "/usr/bin/python3",
            str(self.bridge_script),
            "export",
            str(board_path),
            str(request_out),
            "--rules",
            str(rules_path),
            "--max-expansions",
            str(max_expansions),
        ]
        if limits_path is not None:
            cmd.extend(["--limits", str(limits_path)])
        proc = self._run_guarded_python(cmd, timeout_s=timeout_s)
        out_bytes = request_out.read_bytes() if request_out.exists() else None
        out_json = None
        if out_bytes is not None:
            try:
                out_json = json.loads(out_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                out_json = None
        return WireCallResult(
            proc=proc,
            stderr_json=parse_last_json_line(proc.stderr),
            output_path=request_out,
            output_bytes=out_bytes,
            output_json=out_json,
        )

    def run_engine(
        self,
        request_path: Path,
        plan_out: Path,
        *,
        timeout_s: float = 60.0,
        rlimit_as_bytes: int | None = None,
        metrics_name: str | None = None,
    ) -> WireCallResult:
        proc = run_measured(
            [str(self.binary_path), str(request_path), str(plan_out)],
            cwd=self.repo_root,
            work_dir=self.work_dir / "metrics",
            timeout_s=timeout_s,
            rlimit_as_bytes=rlimit_as_bytes,
            metrics_name=metrics_name,
        )
        out_bytes = plan_out.read_bytes() if plan_out.exists() else None
        out_json = None
        if out_bytes is not None:
            try:
                out_json = json.loads(out_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                out_json = None
        return WireCallResult(
            proc=proc,
            stderr_json=parse_last_json_line(proc.stderr),
            output_path=plan_out,
            output_bytes=out_bytes,
            output_json=out_json,
        )

    def apply_plan(
        self,
        board_path: Path,
        plan_path: Path,
        output_pcb: Path,
        request_path: Path,
        *,
        timeout_s: float = 60.0,
    ) -> WireCallResult:
        cmd = [
            "/usr/bin/python3",
            str(self.bridge_script),
            "apply",
            str(board_path),
            str(plan_path),
            str(output_pcb),
            "--request",
            str(request_path),
        ]
        proc = self._run_guarded_python(cmd, timeout_s=timeout_s)
        out_bytes = output_pcb.read_bytes() if output_pcb.exists() else None
        return WireCallResult(
            proc=proc,
            stderr_json=parse_last_json_line(proc.stderr),
            output_path=output_pcb,
            output_bytes=out_bytes,
            output_json=None,
        )

    def route_board_e2e(
        self,
        board_path: Path,
        output_pcb: Path,
        rules_path: Path,
        *,
        work_dir: Path | None = None,
        max_expansions: int = 100_000,
        limits_path: Path | None = None,
        timeout_s: float = 60.0,
    ) -> RouteE2EResult:
        stage_dir = (work_dir or self.work_dir)
        stage_dir.mkdir(parents=True, exist_ok=True)
        request_path = stage_dir / "request.json"
        plan_path = stage_dir / "plan.json"

        export_res = self.export_board(
            board_path,
            request_path,
            rules_path,
            max_expansions=max_expansions,
            limits_path=limits_path,
            timeout_s=timeout_s,
        )
        if export_res.proc.returncode != 0:
            return RouteE2EResult(
                export_res=export_res,
                engine_res=None,
                apply_res=None,
                request_path=request_path,
                plan_path=plan_path,
                output_pcb=output_pcb,
                stage_failed="export",
            )

        engine_res = self.run_engine(request_path, plan_path, timeout_s=timeout_s)
        if engine_res.proc.returncode != 0:
            return RouteE2EResult(
                export_res=export_res,
                engine_res=engine_res,
                apply_res=None,
                request_path=request_path,
                plan_path=plan_path,
                output_pcb=output_pcb,
                stage_failed="engine",
            )

        apply_res = self.apply_plan(
            board_path,
            plan_path,
            output_pcb,
            request_path,
            timeout_s=timeout_s,
        )
        stage_failed = "apply" if apply_res.proc.returncode != 0 else None
        return RouteE2EResult(
            export_res=export_res,
            engine_res=engine_res,
            apply_res=apply_res,
            request_path=request_path,
            plan_path=plan_path,
            output_pcb=output_pcb,
            stage_failed=stage_failed,
        )


@dataclass(frozen=True)
class NativeOrchestrationProbe:
    supported: bool
    subcommand: str | None
    reason: str
    help_stdout: str = ""
    help_stderr: str = ""


class NativeOrchestrationDriver:
    """Adapter isolating M2+ Lane 4 native Rust orchestration CLI subcommand."""

    def __init__(self, repo_root: Path, binary_path: Path, work_dir: Path) -> None:
        self.repo_root = repo_root.resolve()
        self.binary_path = binary_path.resolve()
        self.work_dir = work_dir.resolve()
        self.guard = GuardedEnv(self.repo_root, self.work_dir / "native-boundary-guard")
        self._cached_probe: NativeOrchestrationProbe | None = None

    def probe(self) -> NativeOrchestrationProbe:
        if self._cached_probe is not None:
            return self._cached_probe
        help_res = subprocess.run(
            [str(self.binary_path), "--help"],
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
            timeout=15,
        )
        combined = f"{help_res.stdout}\n{help_res.stderr}"
        if help_res.returncode == 0:
            for candidate in ("route-native", "native"):
                if re.search(rf"\b{re.escape(candidate)}\b", combined):
                    sub_res = subprocess.run(
                        [str(self.binary_path), candidate, "--help"],
                        cwd=str(self.work_dir),
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                    if sub_res.returncode == 0:
                        self._cached_probe = NativeOrchestrationProbe(
                            supported=True,
                            subcommand=candidate,
                            reason=f"detected subcommand {candidate}",
                            help_stdout=sub_res.stdout,
                            help_stderr=sub_res.stderr,
                        )
                        return self._cached_probe
        self._cached_probe = NativeOrchestrationProbe(
            supported=False,
            subcommand=None,
            reason=(
                "Lane 4 native orchestration subcommand not present in CLI "
                f"(exit {help_res.returncode}: {help_res.stderr.strip() or help_res.stdout.strip()})"
            ),
            help_stdout=help_res.stdout,
            help_stderr=help_res.stderr,
        )
        return self._cached_probe

    def run_native(
        self,
        board_path: Path,
        output_pcb: Path,
        *,
        rules_path: Path | None = None,
        max_expansions: int | None = None,
        limits_path: Path | None = None,
        timeout_s: float = 60.0,
        rlimit_as_bytes: int | None = None,
        cancel_after_s: float | None = None,
        cancel_signal: int = signal.SIGTERM,
        extra_args: list[str] | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> MeasuredProcessResult:
        from tests.e2e_rust_router.runner import PendingCapability

        probe = self.probe()
        if not probe.supported or probe.subcommand is None:
            raise PendingCapability(probe.reason, evidence={"probe": probe.reason})

        cmd = [str(self.binary_path), probe.subcommand, str(board_path), str(output_pcb)]
        if rules_path is not None:
            cmd.extend(["--rules", str(rules_path)])
        if max_expansions is not None:
            cmd.extend(["--max-expansions", str(max_expansions)])
        if limits_path is not None:
            cmd.extend(["--limits", str(limits_path)])
        if extra_args:
            cmd.extend(extra_args)

        start_idx = self.guard.line_count()
        env = self.guard.build_env()
        if env_overrides:
            env.update(env_overrides)
        res = run_measured(
            cmd,
            cwd=self.repo_root,
            work_dir=self.work_dir / "metrics",
            env=env,
            timeout_s=timeout_s,
            rlimit_as_bytes=rlimit_as_bytes,
            cancel_after_s=cancel_after_s,
            cancel_signal=cancel_signal,
        )
        rows = self.guard.read_rows_since(start_idx)
        res.audit_log_path = self.guard.log_path
        res.audit_rows = rows
        self.guard.assert_clean(rows, require_started=False)
        return res
