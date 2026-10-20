"""A failed native build must leave the installed extension intact."""
import os
from pathlib import Path
import subprocess

from helpers import ROOT


def test_invalid_build_does_not_replace_installed_extension(tmp_path):
    venv = tmp_path / "env"
    subprocess.run(["uv", "venv", str(venv), "--python", "/usr/bin/python3"],
                   check=True, capture_output=True)
    python = venv / "bin" / "python"
    site = Path(subprocess.check_output([
        str(python), "-c", "import sysconfig; print(sysconfig.get_path('platlib'))",
    ], text=True).strip())
    installed = site / "grid_router.so"
    installed.write_bytes(b"previous installed extension")
    commands = tmp_path / "commands"
    commands.mkdir()
    cargo = commands / "cargo"
    cargo.write_text(
        "#!/usr/bin/python3\nimport sys\nfrom pathlib import Path\n"
        "target=Path(sys.argv[sys.argv.index('--target-dir')+1])/'release'\n"
        "target.mkdir(parents=True)\n"
        "(target/'libgrid_router.so').write_bytes(b'invalid new extension')\n"
    )
    cargo.chmod(0o755)
    result = subprocess.run([
        str(python), str(ROOT / "src/kicad_route/router/build_router.py"),
        "--target-dir", str(tmp_path / "target"),
    ], env=dict(os.environ, PATH=str(commands) + os.pathsep + os.environ["PATH"]),
       capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert "file too short" in result.stderr
    assert installed.read_bytes() == b"previous installed extension"
