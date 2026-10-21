"""Process helpers for source-pinned owned-router regressions."""
from pathlib import Path
import subprocess
import os
import hashlib
import shutil

ROOT = Path(__file__).resolve().parents[1]

def tool(name):
    paths = [ROOT / "py_router" / name, ROOT / "py_tools" / name]
    return str(next(path for path in paths if path.is_file()))

def evidence(path, label):
    source = Path(path)
    assert source.is_file() and source.stat().st_size > 0, label
    retained = os.environ.get("KICAD_ROUTE_REGRESSION_OUT")
    if retained:
        destination = Path(retained) / source.parent.name / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        print(f"EVIDENCE {label}: {destination} sha256={hashlib.sha256(source.read_bytes()).hexdigest()}")
    return path

def check(argv, *, accept=False, env=None):
    assert accept, "these regressions only launch expected-success processes"
    result = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=300)
    assert result.returncode == 0, result.stdout[-8000:] + result.stderr[-8000:]
    assert "Traceback (most recent call last)" not in result.stdout + result.stderr
    return result
