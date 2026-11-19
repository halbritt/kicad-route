"""Opaque-box E2E test suite for the greenfield Rust PCB router."""

from __future__ import annotations

from tests.e2e_rust_router.runner import (
    FEATURES,
    FEATURE_TITLES,
    STAGES,
    TIERS,
    PendingCapability,
    Status,
    TestResult,
    TestSpec,
    discover_tests,
    register_test,
    run_suite,
)

__all__ = [
    "FEATURES",
    "FEATURE_TITLES",
    "PendingCapability",
    "STAGES",
    "Status",
    "TIERS",
    "TestResult",
    "TestSpec",
    "discover_tests",
    "register_test",
    "run_suite",
]
