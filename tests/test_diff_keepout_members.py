"""The pair members, rather than only their centreline, respect keepouts."""
import os
from pathlib import Path
import subprocess

import pcbnew
import pytest

from helpers import OUT, ROOT
from kicad_route import cli, common, engine


@pytest.mark.parametrize("case", [
    "test_both_passes_block_their_band",
    "test_base_map_threads_the_extra_clearance",
    "test_members_stay_out_of_a_native_keepout",
    "test_members_stay_out_of_a_user_keepout",
])
def test_diff_pair_keepout_members(case):
    source = engine.SOURCE_ROOT
    script = source / "tests/test_diff_keepout_member_clearance.py"
    runner = ("import runpy,site,sys; site.addsitedir(sys.argv[1]); "
              "sys.argv=sys.argv[2:]; runpy.run_path(sys.argv[0],run_name='__main__')")
    env = dict(common.tool_env(), PYTHONDONTWRITEBYTECODE="1",
               KICAD_ROUTE_KEEPOUT_OUT=str(OUT / "diff-keepout-artifacts"),
               KICAD_ROUTE_KEEPOUT_FIXTURE=str(ROOT / "fixtures/diff_keepouts/lvds_converter_dualclk.kicad_pcb"))
    result = subprocess.run([str(cli.route_python()), "-c", runner,
                             str(Path(pcbnew.__file__).resolve().parent), str(script), case],
                            env=env, capture_output=True, text=True, timeout=1000)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / (case + ".log")).write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout[-6000:] + result.stderr[-3000:]
    assert f"PASS: {case}" in result.stdout
