"""Run short-pair policy and native geometry controls against owned source."""
import subprocess
import pytest

from kicad_route import cli, engine


@pytest.mark.parametrize('name', ['test_required_coupling.py', 'test_pad_body_dangles.py'])
def test_owned_short_pair_policy_and_native_controls(name):
    script = engine.SOURCE_ROOT / 'tests' / name
    done = subprocess.run([str(cli.route_python()), str(script), '-v'],
                          capture_output=True, text=True, timeout=180, env=cli.common.tool_env())
    assert done.returncode == 0, done.stdout + done.stderr
