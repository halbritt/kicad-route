#!/usr/bin/env python3
"""Prove the fast gate rejects cache, tie-order and via-clearance defects."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'src/kicad_route/router/rust_router'
MUTANTS = (
    ('bitmap-clear', 'obstacle_map.rs',
     'self.blocked_bitmap.clear(gx, gy, layer);', '/* mutant: stale cached bit */'),
    ('lane-tie-order', 'lane_search.rs',
     '.then_with(|| o.s.cmp(&self.s))', '.then_with(|| self.s.cmp(&o.s))'),
    ('pair-via-clearance', 'pair_search.rs',
     '!vbad[[du % 4, iu, ju]]', 'true'),
)


def main():
    build = ROOT / 'build'
    build.mkdir(exist_ok=True)
    for name, filename, before, after in MUTANTS:
        with tempfile.TemporaryDirectory(prefix='kernel-mutant-', dir=build) as directory:
            source = Path(directory) / 'source'
            shutil.copytree(SOURCE, source)
            path = source / 'src' / filename
            content = path.read_text()
            if before not in content:
                raise RuntimeError(f'mutation site missing: {name}')
            path.write_text(content.replace(before, after))
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/kernel-tests.py'),
                                     '--source', str(source), '--target', str(build / 'kernel-mutants')],
                                    capture_output=True, text=True, timeout=180)
            (build / f'kernel-mutant-{name}.log').write_text(result.stdout + result.stderr)
            # A compiler/import error does not prove invariant protection.
            if result.returncode == 0 or 'AssertionError' not in result.stderr:
                raise RuntimeError(f'{name}: gate did not reject the mutant with an assertion; inspect its log')
            print(f'{name}: rejected by invariant assertion')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
