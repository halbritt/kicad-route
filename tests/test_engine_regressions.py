"""Retain the two motivating fork regressions in the normal integration suite."""
from pathlib import Path
import subprocess

import pcbnew
import pytest

from helpers import OUT
from kicad_route import cli as route, common, engine


@pytest.mark.parametrize("script", [
    "test_krt64_tap_grid.py", "test_pour_launch_forbidden_layer_welds.py",
])
def test_known_fork_regression_passes_with_owned_engine(script):
    # The worker needs its numeric dependencies and extension; only the test's
    # board generator also needs the system interpreter's pcbnew module.
    runner = (
        "import runpy,site,sys; site.addsitedir(sys.argv[1]); "
        "sys.argv=sys.argv[2:]; runpy.run_path(sys.argv[0],run_name='__main__')"
    )
    result = subprocess.run([
        str(route.route_python()), "-c", runner,
        str(Path(pcbnew.__file__).resolve().parent),
        str(engine.ROOT / "tests" / script),
    ], env=common.tool_env(), capture_output=True, text=True, timeout=300)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / (script + ".log")).write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout[-5000:] + result.stderr[-5000:]


@pytest.mark.parametrize("section", ["outline", "edge-map", "reweld", "plane-passes"])
def test_owned_rule_floors(section):
    runner = (
        "import runpy,site,sys; site.addsitedir(sys.argv[1]); "
        "sys.argv=sys.argv[2:]; runpy.run_path(sys.argv[0],run_name='__main__')"
    )
    script = engine.SOURCE_ROOT / "tests" / "test_pass_rule_floors.py"
    result = subprocess.run([
        str(route.route_python()), "-c", runner,
        str(Path(pcbnew.__file__).resolve().parent), str(script), section,
    ], env=common.tool_env(), capture_output=True, text=True, timeout=600)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / ("pass-rule-floors-" + section + ".log")).write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout[-8000:] + result.stderr[-8000:]
