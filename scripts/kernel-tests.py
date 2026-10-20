#!/usr/bin/env python3
"""Build and test owned routing kernels without touching the installed runtime."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / 'src/kicad_route/router'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROUTER / 'rust_router')
    parser.add_argument('--target', type=Path, default=ROOT / 'build/kernel')
    args = parser.parse_args()
    source, target = args.source.resolve(), args.target.resolve()
    subprocess.run(['cargo', 'build', '--release', '--locked', '--offline',
                    '--manifest-path', str(source / 'Cargo.toml'), '--target-dir', str(target)], check=True)
    environment = dict(os.environ, KERNEL_EXTENSION=str(target / 'release/libgrid_router.so'))
    # Load the just-built library once, then let retained scripts import it by
    # name. An installed extension with the same version is insufficient proof.
    bootstrap = ("import importlib.util,os,runpy,sys; "
                 "s=importlib.util.spec_from_file_location('grid_router',os.environ['KERNEL_EXTENSION']); "
                 "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);sys.modules['grid_router']=m; "
                 "sys.argv=sys.argv[1:];runpy.run_path(sys.argv[0],run_name='__main__')")
    for name in ('test_kernel.py', 'test_obstacle_addremove_parity.py', 'test_writer_quotes.py'):
        subprocess.run([sys.executable, '-c', bootstrap, str(ROUTER / 'tests' / name)],
                       env=environment, timeout=60, check=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
