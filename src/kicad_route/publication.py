"""Owned, atomically replaced public receipt bundles (Linux renameat2)."""
import contextlib
import ctypes
import fcntl
import os
from pathlib import Path
import shutil
import tempfile

from . import common, contract

MANIFEST = "publication.json"
SCHEMA = "kicad-route/publication/1"


def destination(path, kind, protected=()):
    path = Path(os.path.abspath(path))
    if any(p.is_symlink() for p in (path, *path.parents)):
        contract.refuse("input", f"publication destination has a symlink: {path}")
    protected = tuple(Path(root).resolve() for root in protected)
    for root in (Path.home(), Path('/'), common.TOOL_ROOT.resolve(), *protected):
        if path == root or path in root.parents or (root in protected and root in path.parents):
            contract.refuse("input", f"publication overlaps protected path {root}: {path}")
    if path.exists():
        if not path.is_dir():
            contract.refuse("input", f"publication is not a directory: {path}")
        if list(path.glob('*.kicad_pcb')) or any(p.is_symlink() for p in path.rglob('*')):
            contract.refuse("input", f"publication contains project data or symlinks: {path}")
        if any(path.iterdir()):
            manifest = common.load_json(path / MANIFEST) or {}
            if not isinstance(manifest.get('files'), dict):
                contract.refuse('input', f'refusing publication without an ownership inventory: {path}')
            expected = set(manifest['files']) | {MANIFEST}
            expected |= {str(parent) for name in manifest.get('files', {})
                         for parent in Path(name).parents if str(parent) != '.'}
            actual = {str(p.relative_to(path)) for p in path.rglob('*')}
            if manifest.get('schema') != SCHEMA or manifest.get('kind') != kind or actual != expected:
                contract.refuse("input", f"refusing unowned or modified publication: {path}")
            if any(common.sha256(path / name) != digest for name, digest in manifest['files'].items()):
                contract.refuse("input", f"publication contents changed: {path}")
    return path


def seal(stage, kind, **metadata):
    files = {str(p.relative_to(stage)): common.sha256(p) for p in sorted(stage.rglob('*')) if p.is_file()}
    common.write_json(stage / MANIFEST, {'schema': SCHEMA, 'kind': kind, 'files': files, **metadata})


def _exchange(left, right):
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(left), -100, os.fsencode(right), 2):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(right))


@contextlib.contextmanager
def staging(dest, kind, protected=()):
    dest = destination(dest, kind, protected)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Serialize cooperating publishers without leaving a lock file in the checkout.
    with contextlib.ExitStack() as stack:
        parent_fd = os.open(dest.parent, os.O_RDONLY | os.O_DIRECTORY)
        stack.callback(os.close, parent_fd)
        fcntl.flock(parent_fd, fcntl.LOCK_EX)
        destination(dest, kind, protected)
        stage = Path(tempfile.mkdtemp(prefix=f'.{dest.name}-', dir=dest.parent))
        try:
            yield stage
            if not (stage / MANIFEST).is_file():
                raise RuntimeError('publication must be sealed before activation')
            destination(dest, kind, protected)
            if dest.exists():
                _exchange(stage, dest)
            else:
                os.rename(stage, dest)
        finally:
            if stage.exists():
                shutil.rmtree(stage)


def receipts(directory):
    if not directory.is_dir() or any(p.is_symlink() for p in (directory, *directory.parents)):
        return []
    return sorted(p for p in directory.iterdir() if p.is_file() and not p.is_symlink()
                  and (p.suffix == '.json' or p.name == 'RESULT.md'))


def copy_run(run, stage):
    run = Path(run).resolve()
    mappings = {}
    selected = []
    for sub in ('', 'validate', 'facts', 'spokes'):
        for source in receipts(run / sub):
            target = Path(source.name) if sub in ('', 'validate') else Path(sub) / source.name
            if target == Path(MANIFEST):
                target = Path('receipts') / MANIFEST
            if (stage / target).exists():
                target = Path(sub) / source.name
            selected.append((source, target))
            (stage / target).parent.mkdir(parents=True, exist_ok=True)
            (stage / target).write_bytes(source.read_bytes())
            mappings[str(source.relative_to(run))] = str(target)
    for sub, name in {'revalidate-origin': 'against-origin', 'revalidate': 'revalidate'}.items():
        stamps = sorted(p for p in (run / sub).glob('2*') if p.is_dir() and not p.is_symlink())
        if stamps:
            for source in receipts(stamps[-1]):
                target = Path(name) / source.name
                (stage / target).parent.mkdir(parents=True, exist_ok=True)
                (stage / target).write_bytes(source.read_bytes())
                selected.append((source, target))
                mappings[str(source.relative_to(run))] = str(target)
    evidence = {}
    for source, target in selected:
        if source.name != 'result.json':
            continue
        result = common.load_json(source) or {}
        block = result.get('evidence') or {}
        records = {}
        for locator, digest in block.get('records', {}).items():
            original = os.path.relpath(source.parent / locator, run)
            published = mappings.get(original)
            if published and common.sha256(stage / published) != digest:
                raise ValueError(f'evidence hash mismatch: {source}: {locator}')
            records[locator] = {'path': published, 'sha256': digest,
                                'availability': 'included' if published else 'external'}
        evidence[str(target)] = {'records': records, 'external': {
            key: value for key, value in block.items() if key != 'records'}}
    seal(stage, 'run', locators=mappings, evidence=evidence)


def publish_run(run, dest):
    protected = (common.out_dir().resolve(), Path(run).resolve())
    with staging(dest, 'run', protected) as stage:
        copy_run(run, stage)


def verify(dest):
    """Check all included bytes and evidence hashes using only the bundle."""
    dest = Path(dest)
    manifest = common.load_json(dest / MANIFEST)
    if not manifest or manifest.get('schema') != SCHEMA:
        raise ValueError('missing publication manifest')
    for name, digest in manifest['files'].items():
        path = Path(name)
        if path.is_absolute() or '..' in path.parts or any(
                p.is_symlink() for p in (dest / path, *(dest / path).parents)):
            raise ValueError(f'unsafe publication path: {name}')
        if common.sha256(dest / path) != digest:
            raise ValueError(f'publication hash mismatch: {name}')
    for block in manifest.get('evidence', {}).values():
        for record in block['records'].values():
            if record['availability'] == 'included':
                if manifest['files'].get(record['path']) != record['sha256']:
                    raise ValueError(f"evidence hash mismatch: {record['path']}")
    return manifest
