"""Source-bound existing connector qualification, without a cohort worker claim."""
import json
import os
from pathlib import Path
import subprocess
import time

import pcbnew
import pytest

from helpers import OUT, ROOT
from kicad_route import cli, common, procgroup


@pytest.mark.parametrize('variant', ['baseline', 'multipin', 'unequal'])
def test_existing_connector_preserves_native_contract(tmp_path, variant):
    python = Path(os.environ.get('KERNEL_PYTHON') or cli.route_python())
    if not python.is_file():
        pytest.skip('required owned numeric interpreter/kernel extension is unavailable')
    # The numeric runtime supplies the extension; current checkout supplies all
    # routing Python. KERNEL_EXTENSION is the existing kernel gate override.
    runner = ('import runpy,site,sys; site.addsitedir(sys.argv[1]); '
              'sys.argv=sys.argv[2:]; runpy.run_path(sys.argv[0],run_name="__main__")')
    command = [str(python), '-c', runner, str(Path(pcbnew.__file__).resolve().parent),
               str(ROOT / 'tests/repair_connector_fixture.py'), str(tmp_path), variant]
    start = time.monotonic()
    process = subprocess.Popen(command, env=common.tool_env(), stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, **procgroup.POPEN_KW)
    try:
        stdout, stderr = process.communicate(timeout=20)
    finally:
        if process.poll() is None:
            procgroup.terminate(process, term_timeout=1)
        procgroup.reap_leftovers(process, term_timeout=1)
    log = stdout + stderr
    (tmp_path / 'connector.log').write_text(log)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f'repair-connector-{variant}.log').write_text(log)
    (OUT / f'repair-connector-{variant}-command.json').write_text(json.dumps({
        'argv': command, 'runtime_s': time.monotonic()-start, 'exit_code': process.returncode,
        'kernel_extension': os.environ.get('KERNEL_EXTENSION')}, indent=2) + '\n')
    if process.returncode == 77:
        pytest.skip(log.strip())
    assert process.returncode == 0, log[-8000:]
    result = json.loads((tmp_path / 'receipt.json').read_text())
    assert [step['components_after'] for step in result['steps']] == [1, 1]
    assert result['steps'][-1]['native_opens'] == 0
    assert [step['width_mm'] for step in result['steps']] == [.2, .25 if variant == 'unequal' else .2]
