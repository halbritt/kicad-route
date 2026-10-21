"""Exercise public runtime commands with only a built wheel on Python's path."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from helpers import ROOT


@pytest.fixture(scope="module")
def wheel_environment(tmp_path_factory):
    directory = tmp_path_factory.mktemp("installed-wheel")
    wheels = directory / "wheels"
    subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
                    "--wheel-dir", wheels, ROOT], check=True, capture_output=True, text=True, timeout=180)
    venv = directory / "venv"
    subprocess.run(["uv", "venv", venv, "--python", sys.executable], check=True, capture_output=True, timeout=60)
    subprocess.run(["uv", "pip", "install", "--python", venv / "bin/python", *wheels.glob("*.whl")],
                   check=True, capture_output=True, timeout=60)
    env = {key: value for key, value in os.environ.items()
           if key not in {"PYTHONPATH", "VIRTUAL_ENV", "KICAD_ROUTE_STATE", "KICAD_ROUTE_OUT"}}
    env.update(PATH=f"{venv / 'bin'}:/usr/bin:/bin", XDG_STATE_HOME=str(directory / "xdg-state"),
               KICAD_ROUTE_LEDGER="off", PYTHONDONTWRITEBYTECODE="1")
    return directory, venv, env


def invoke(wheel_environment, *args, env=None, timeout=300):
    directory, venv, defaults = wheel_environment
    return subprocess.run([venv / "bin/kicad-route", "--out", directory / "out", *args],
                          cwd=directory, env=env or defaults, capture_output=True, text=True, timeout=timeout)


def test_installed_resources_writable_state_help_and_smoke_refusal(wheel_environment):
    directory, venv, env = wheel_environment
    result = subprocess.run([venv / "bin/python", "-c",
        "import json; from kicad_route import common,contract,cli; "
        "print(json.dumps({'package':str(common.PACKAGE_ROOT),'checkout':str(common.CHECKOUT_ROOT),"
        "'state':str(common.STATE),'fixture':str(cli.FIXTURE),'schemas':str(contract.SCHEMAS_DIR)}))"],
        cwd=directory, env=env, capture_output=True, text=True, check=True)
    paths = json.loads(result.stdout)
    assert paths["checkout"] == "None"
    assert Path(paths["package"]).is_relative_to(venv)
    assert paths["state"] == str(directory / "xdg-state/kicad-route")
    assert Path(paths["fixture"], "route_demo.kicad_pcb").is_file()
    assert Path(paths["schemas"], "result-1.json").is_file()
    assert invoke(wheel_environment, "--help").returncode == 0
    assert invoke(wheel_environment, "campaign", "--help").returncode == 0
    smoke = invoke(wheel_environment, "smoke")
    assert smoke.returncode == 4
    assert "development checkout" in smoke.stdout + smoke.stderr
    assert not (directory / "out/smoke").exists()


def test_installed_doctor_route_and_campaign(wheel_environment):
    runtime = os.environ.get("KICAD_ROUTE_WHEEL_RUNTIME")
    if not runtime:
        pytest.skip("set KICAD_ROUTE_WHEEL_RUNTIME to a built native routing venv")
    directory, venv, defaults = wheel_environment
    state = directory / "native-state"
    state.mkdir()
    (state / ".venv-krt").symlink_to(Path(runtime).resolve())
    env = dict(defaults, KICAD_ROUTE_STATE=str(state))
    doctor = invoke(wheel_environment, "doctor", "--offline", "--no-canary", "--json", env=env)
    record = json.loads(doctor.stdout)
    assert record["kind"] == "doctor"
    checks = {check["check"]: check for check in record["checks"]}
    for name in ("Builtin routing venv", "Builtin Rust router module", "Builtin startup checks", "Builtin controls used by the adapter"):
        assert checks[name]["status"] == "PASS", checks[name]
    assert "No module named" not in doctor.stderr
    info = subprocess.check_output([venv / "bin/python", "-c",
                                  "from kicad_route.cli import FIXTURE; print(FIXTURE)"],
                                  cwd=directory, env=env, text=True).strip()
    routed = invoke(wheel_environment, "run", "--backend", "krt", "--dir", info,
                    "--budget", "20", "--default-clearance", "0.4", "--json", env=env)
    result = json.loads(routed.stdout)
    assert routed.returncode == 0, routed.stderr[-4000:]
    assert result["validation"]["status"] == "PASS" and result["routing"]["status"] == "COMPLETE"
    campaign = invoke(wheel_environment, "campaign", "--backend", "krt", "--dir", info,
                      "--rounds", "0", "--budget", "20", "--total-budget", "90",
                      "--default-clearance", "0.4", "--json", env=env)
    record = json.loads(campaign.stdout)
    assert campaign.returncode == 0, campaign.stderr[-5000:]
    assert record["kind"] == "route-campaign"
