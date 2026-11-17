"""Stage-aware opaque-box E2E test registry, execution engine, and CLI."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback
from typing import Any, Callable, Literal

from tests.e2e_rust_router.drivers import (
    BuildProvenance,
    JsonWireDriver,
    NativeOrchestrationDriver,
    ensure_engine_binary,
)

STAGES: tuple[str, ...] = ("BASELINE", "M1", "M2", "M3", "M4", "M5")
TIERS: tuple[int, ...] = (1, 2, 3, 4)
FEATURES: tuple[str, ...] = tuple(f"F{i}" for i in range(1, 12))
FEATURE_TITLES: dict[str, str] = {
    "F1": "Lane 1: M7 Multi-Terminal",
    "F2": "Lane 3: Native Oval/RoundRect",
    "F3": "Lane 2: Spatial Index",
    "F4": "Lane 4: Native Orchestration",
    "F5": "Extended Pad/Drill/Geometry Parity",
    "F6": "Rules, Multilayer, Vias & Search State",
    "F7": "Zones, Planes, Thermals & Keepouts",
    "F8": "Global Multi-Net & Rip-Up/Reroute",
    "F9": "Differential Pairs, Length & Delay",
    "F10": "agent-ee Integration & Provenance",
    "F11": "E2E Acceptance & RE-08 Attribution",
}

_TIER_MODULES: tuple[str, ...] = (
    "tests.e2e_rust_router.tier1_core",
    "tests.e2e_rust_router.tier2_core",
    "tests.e2e_rust_router.tier1_advanced",
    "tests.e2e_rust_router.tier2_advanced",
    "tests.e2e_rust_router.tier3_pairwise",
    "tests.e2e_rust_router.tier4_scenarios",
)


class Status:
    PASSED: Literal["PASSED"] = "PASSED"
    PENDING_CAPABILITY: Literal["PENDING_CAPABILITY"] = "PENDING_CAPABILITY"
    FAILED: Literal["FAILED"] = "FAILED"


class PendingCapability(Exception):
    """Raised when an opaque-box probe confirms a future stage is not yet landed."""

    def __init__(self, reason: str, *, evidence: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.evidence = dict(evidence or {})


@dataclass(frozen=True)
class DriverContext:
    test_id: str
    work_dir: Path
    repo_root: Path
    target_stage: str
    provenance: BuildProvenance
    json_driver: JsonWireDriver
    native_driver: NativeOrchestrationDriver

    def stage_at_least(self, stage: str) -> bool:
        return stage_index(self.target_stage) >= stage_index(stage)


@dataclass(frozen=True)
class TestSpec:
    test_id: str
    name: str
    tier: int
    feature: str
    min_stage: str
    fn: Callable[[DriverContext], dict[str, Any] | None]
    description: str = ""


@dataclass
class TestResult:
    test_id: str
    name: str
    tier: int
    feature: str
    min_stage: str
    status: str
    duration_s: float
    reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


_REGISTRY: list[TestSpec] = []
_DISCOVERED = False


def stage_index(stage: str) -> int:
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}; expected one of {STAGES}")
    return STAGES.index(stage)


def register_test(
    *,
    test_id: str,
    tier: int,
    feature: str,
    min_stage: str,
    name: str,
    description: str = "",
) -> Callable[[Callable[[DriverContext], dict[str, Any] | None]], Callable[[DriverContext], dict[str, Any] | None]]:
    if tier not in TIERS:
        raise ValueError(f"{test_id}: invalid tier {tier!r}")
    if feature not in FEATURES:
        raise ValueError(f"{test_id}: invalid feature {feature!r}")
    if min_stage not in STAGES:
        raise ValueError(f"{test_id}: invalid min_stage {min_stage!r}")

    def decorator(
        fn: Callable[[DriverContext], dict[str, Any] | None],
    ) -> Callable[[DriverContext], dict[str, Any] | None]:
        if any(spec.test_id == test_id for spec in _REGISTRY):
            raise ValueError(f"duplicate test_id registered: {test_id}")
        _REGISTRY.append(
            TestSpec(
                test_id=test_id,
                name=name,
                tier=tier,
                feature=feature,
                min_stage=min_stage,
                fn=fn,
                description=description,
            )
        )
        return fn

    return decorator


def _ensure_modules_imported() -> None:
    global _DISCOVERED
    if _DISCOVERED:
        return
    for mod_name in _TIER_MODULES:
        try:
            importlib.import_module(mod_name)
        except ModuleNotFoundError as exc:
            if exc.name == mod_name:
                continue
            raise
    _DISCOVERED = True


def discover_tests(
    *,
    tiers: tuple[int, ...] | list[int] | None = None,
    features: tuple[str, ...] | list[str] | None = None,
) -> list[TestSpec]:
    _ensure_modules_imported()
    tier_set = set(tiers) if tiers else None
    feature_set = set(features) if features else None
    return [
        spec
        for spec in _REGISTRY
        if (tier_set is None or spec.tier in tier_set)
        and (feature_set is None or spec.feature in feature_set)
    ]


def _run_one(
    spec: TestSpec,
    *,
    repo_root: Path,
    target_stage: str,
    provenance: BuildProvenance,
    keep_tmp: bool,
) -> TestResult:
    t0 = time.monotonic()
    ctx_dir_mgr = None if keep_tmp else tempfile.TemporaryDirectory(prefix=f"e2e-{spec.test_id}-")
    if keep_tmp:
        work_dir = Path(tempfile.mkdtemp(prefix=f"e2e-{spec.test_id}-"))
    else:
        assert ctx_dir_mgr is not None
        work_dir = Path(ctx_dir_mgr.name)

    try:
        ctx = DriverContext(
            test_id=spec.test_id,
            work_dir=work_dir,
            repo_root=repo_root,
            target_stage=target_stage,
            provenance=provenance,
            json_driver=JsonWireDriver(repo_root, provenance.binary_path, work_dir / "json-driver"),
            native_driver=NativeOrchestrationDriver(
                repo_root, provenance.binary_path, work_dir / "native-driver"
            ),
        )
        ret = spec.fn(ctx)
        details = dict(ret) if isinstance(ret, dict) else {}
        if keep_tmp:
            details.setdefault("work_dir", str(work_dir))
        return TestResult(
            test_id=spec.test_id,
            name=spec.name,
            tier=spec.tier,
            feature=spec.feature,
            min_stage=spec.min_stage,
            status=Status.PASSED,
            duration_s=round(time.monotonic() - t0, 4),
            reason=None,
            details=details,
        )
    except PendingCapability as exc:
        duration_s = round(time.monotonic() - t0, 4)
        details = dict(exc.evidence)
        if keep_tmp:
            details.setdefault("work_dir", str(work_dir))
        if stage_index(spec.min_stage) > stage_index(target_stage):
            return TestResult(
                test_id=spec.test_id,
                name=spec.name,
                tier=spec.tier,
                feature=spec.feature,
                min_stage=spec.min_stage,
                status=Status.PENDING_CAPABILITY,
                duration_s=duration_s,
                reason=exc.reason,
                details=details,
            )
        return TestResult(
            test_id=spec.test_id,
            name=spec.name,
            tier=spec.tier,
            feature=spec.feature,
            min_stage=spec.min_stage,
            status=Status.FAILED,
            duration_s=duration_s,
            reason=f"expected capability for {target_stage} missing: {exc.reason}",
            details=details,
        )
    except Exception as exc:
        duration_s = round(time.monotonic() - t0, 4)
        return TestResult(
            test_id=spec.test_id,
            name=spec.name,
            tier=spec.tier,
            feature=spec.feature,
            min_stage=spec.min_stage,
            status=Status.FAILED,
            duration_s=duration_s,
            reason=f"{type(exc).__name__}: {exc}",
            details={
                "traceback": traceback.format_exc(),
                **({"work_dir": str(work_dir)} if keep_tmp else {}),
            },
        )
    finally:
        if ctx_dir_mgr is not None:
            ctx_dir_mgr.cleanup()


def _summarize(results: list[TestResult]) -> dict[str, Any]:
    by_status = {
        Status.PASSED: sum(1 for r in results if r.status == Status.PASSED),
        Status.PENDING_CAPABILITY: sum(1 for r in results if r.status == Status.PENDING_CAPABILITY),
        Status.FAILED: sum(1 for r in results if r.status == Status.FAILED),
    }
    by_tier: dict[str, dict[str, int]] = {}
    by_feature: dict[str, dict[str, int]] = {}
    by_stage: dict[str, dict[str, int]] = {}
    for r in results:
        for bucket, key in (
            (by_tier, f"T{r.tier}"),
            (by_feature, r.feature),
            (by_stage, r.min_stage),
        ):
            entry = bucket.setdefault(
                key,
                {Status.PASSED: 0, Status.PENDING_CAPABILITY: 0, Status.FAILED: 0, "total": 0},
            )
            entry[r.status] += 1
            entry["total"] += 1
    return {
        "total": len(results),
        "by_status": by_status,
        "by_tier": by_tier,
        "by_feature": by_feature,
        "by_min_stage": by_stage,
    }


def run_suite(
    *,
    repo_root: Path,
    tiers: list[int] | None = None,
    features: list[str] | None = None,
    target_stage: str = "BASELINE",
    binary_override: Path | None = None,
    profile: str = "debug",
    verbose: bool = False,
    fail_fast: bool = False,
    keep_tmp: bool = False,
) -> tuple[dict[str, Any], list[TestResult]]:
    specs = discover_tests(tiers=tiers, features=features)
    if not specs:
        raise ValueError(f"no tests matched filters tiers={tiers!r} features={features!r}")
    provenance = ensure_engine_binary(
        repo_root, binary_override=binary_override, profile=profile
    )
    results: list[TestResult] = []
    for spec in specs:
        res = _run_one(
            spec,
            repo_root=repo_root,
            target_stage=target_stage,
            provenance=provenance,
            keep_tmp=keep_tmp,
        )
        results.append(res)
        if verbose:
            tag = {"PASSED": "PASS", "PENDING_CAPABILITY": "PEND", "FAILED": "FAIL"}[res.status]
            suffix = f" - {res.reason}" if res.reason else ""
            print(
                f"[{tag}] {res.test_id} ({res.feature}/T{res.tier}/{res.min_stage}) "
                f"{res.duration_s:.3f}s {res.name}{suffix}",
                flush=True,
            )
        if fail_fast and res.status == Status.FAILED:
            break

    report = {
        "schema_version": "e2e-rust-router/1",
        "target_stage": target_stage,
        "provenance": {
            "repo_root": str(provenance.repo_root),
            "git_head": provenance.git_head,
            "git_dirty": provenance.git_dirty,
            "cargo_lock_sha256": provenance.cargo_lock_sha256,
            "binary_path": str(provenance.binary_path),
            "binary_sha256": provenance.binary_sha256,
            "kicad_cli_version": provenance.kicad_cli_version,
            "pcbnew_version": provenance.pcbnew_version,
        },
        "summary": _summarize(results),
        "results": [asdict(r) for r in results],
    }
    return report, results


def _parse_tier_tokens(tokens: list[str] | None) -> list[int] | None:
    if tokens is None:
        return None
    selected: list[int] = []
    for raw in tokens:
        for part in raw.split(","):
            tok = part.strip().lower()
            if not tok:
                raise argparse.ArgumentTypeError(f"empty --tier token in {raw!r}")
            if tok == "all":
                return list(TIERS)
            if tok not in {"1", "2", "3", "4"}:
                raise argparse.ArgumentTypeError(f"invalid --tier value: {part!r}")
            val = int(tok)
            if val not in selected:
                selected.append(val)
    return selected


def _parse_feature_tokens(tokens: list[str] | None) -> list[str] | None:
    if tokens is None:
        return None
    selected: list[str] = []
    for raw in tokens:
        for part in raw.split(","):
            tok = part.strip().upper()
            if not tok:
                raise argparse.ArgumentTypeError(f"empty --feature token in {raw!r}")
            if tok not in FEATURES:
                raise argparse.ArgumentTypeError(f"invalid --feature value: {part!r}")
            if tok not in selected:
                selected.append(tok)
    return selected


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m tests.e2e_rust_router",
        description="Opaque-box E2E test suite for the greenfield Rust PCB router.",
    )
    parser.add_argument(
        "--tier",
        action="append",
        help="Tier(s) to run: 1, 2, 3, 4, or all (repeatable or comma-separated).",
    )
    parser.add_argument(
        "--feature",
        action="append",
        help="Feature(s) to run: F1..F11 (repeatable or comma-separated).",
    )
    parser.add_argument(
        "--target-stage",
        choices=STAGES,
        default="BASELINE",
        help="Target milestone stage for capability gating (default: BASELINE).",
    )
    parser.add_argument("--json-out", type=Path, help="Write structured JSON report to PATH.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print per-test status lines.")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="Repository/worktree root (default: parent of tests/).",
    )
    parser.add_argument("--binary", type=Path, help="Override path to kicad-route-engine.")
    parser.add_argument("--profile", choices=("debug", "release"), default="debug")
    parser.add_argument("--fail-fast", action="store_true", help="Stop on first FAILED test.")
    parser.add_argument("--keep-tmp", action="store_true", help="Retain per-test temp dirs.")
    parser.add_argument("--list", action="store_true", help="List matching tests and exit.")
    return parser


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, indent=2, sort_keys=False) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        tiers = _parse_tier_tokens(args.tier)
        features = _parse_feature_tokens(args.feature)
    except argparse.ArgumentTypeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.list:
        specs = discover_tests(tiers=tiers, features=features)
        if not specs:
            print("error: no tests matched filters", file=sys.stderr)
            return 2
        for s in specs:
            print(f"{s.test_id}\tT{s.tier}\t{s.feature}\t{s.min_stage}\t{s.name}")
        return 0

    try:
        report, results = run_suite(
            repo_root=args.repo_root.resolve(),
            tiers=tiers,
            features=features,
            target_stage=args.target_stage,
            binary_override=args.binary,
            profile=args.profile,
            verbose=args.verbose,
            fail_fast=args.fail_fast,
            keep_tmp=args.keep_tmp,
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json_out is not None:
        try:
            _write_json_atomic(args.json_out, report)
        except OSError as exc:
            print(f"error: failed to write --json-out {args.json_out}: {exc}", file=sys.stderr)
            return 2

    counts = report["summary"]["by_status"]
    print(
        f"E2E Summary (target_stage={args.target_stage}): "
        f"total={report['summary']['total']} "
        f"PASSED={counts[Status.PASSED]} "
        f"PENDING_CAPABILITY={counts[Status.PENDING_CAPABILITY]} "
        f"FAILED={counts[Status.FAILED]}",
        flush=True,
    )
    if counts[Status.FAILED] > 0:
        for r in results:
            if r.status == Status.FAILED:
                print(f"  FAILED {r.test_id}: {r.reason}", file=sys.stderr)
        return 1
    return 0
