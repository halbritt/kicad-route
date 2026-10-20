#!/usr/bin/env python3
"""Build the owned Rust source into the routing environment.

Cargo may acquire locked crate dependencies. It never acquires KRT source or
prebuilt routing binaries. Build output is kept outside the source tree.
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import tomllib


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-dir", type=Path, required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parent / "rust_router"
    manifest = source / "Cargo.toml"
    target = args.target_dir.resolve()
    if target.is_relative_to(Path(__file__).resolve().parent):
        parser.error("--target-dir must be outside the routing source tree")
    if sys.prefix == sys.base_prefix:
        parser.error("run with the routing virtual environment's Python")
    subprocess.run([
        "cargo", "build", "--release", "--locked", "--manifest-path", str(manifest),
        "--target-dir", str(target),
    ], env=dict(os.environ, PYO3_PYTHON=sys.executable), check=True)
    library = target / "release" / "libgrid_router.so"
    destination = Path(sysconfig.get_path("platlib")) / "grid_router.so"
    version = tomllib.loads(manifest.read_text())["package"]["version"]
    actual = subprocess.check_output([
        sys.executable, "-c",
        "import importlib.util,sys; "
        "spec=importlib.util.spec_from_file_location('grid_router',sys.argv[1]); "
        "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
        "print(module.__version__)", str(library),
    ], text=True).strip()
    if actual != version:
        raise RuntimeError(f"built router version {actual!r} does not match {version!r}")
    # Existing workers may still map the previous extension. Atomic replacement
    # keeps those mappings valid and preserves the old module on build failure.
    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".grid-router-",
                                     suffix=".so", delete=False) as output:
        temporary = Path(output.name)
    try:
        shutil.copyfile(library, temporary)
        shutil.copymode(library, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Built owned grid_router {version}: {destination}")


if __name__ == "__main__":
    main()
