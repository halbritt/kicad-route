#!/usr/bin/env python3
"""Post-terminal artifact transport only; never run a native benchmark or alter inputs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import uuid


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def below(path, prefix):
    return path == prefix or path.is_relative_to(prefix)


def omission_inventory(path):
    files = size = links = 0
    for parent, dirs, names in os.walk(path, followlinks=False):
        parent = Path(parent)
        for name in list(dirs):
            if (parent/name).is_symlink():
                links += 1
                dirs.remove(name)
        for name in names:
            child = parent/name
            if child.is_symlink():
                links += 1
            elif child.is_file():
                files += 1
                size += child.stat().st_size
    return files, size, links


def leaves(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from leaves(item)
    elif isinstance(value, str):
        yield value


def create_export(root, output, protocol_path, policy_path):
    root, output, protocol_path, policy_path = (Path(p).resolve() for p in (root, output, protocol_path, policy_path))
    if output.parent != root or output.name != 'archon-filtered-export' or output.exists():
        raise ValueError('Export must be a new direct-child archon-filtered-export directory')
    policy, protocol = read(policy_path), read(protocol_path)
    if digest(protocol_path) != policy['profile_sha256']:
        raise ValueError('Native profile differs from transport policy binding')
    matrix = read(Path(protocol['metadata_output'])/'matrix.json')
    if matrix['status'] not in ('PASS', 'FAIL', 'EXECUTION_ERROR', 'INTERRUPTED'):
        raise ValueError('Matrix must be terminal before any export or traversal')
    after = Path(protocol['host_after_attestation_path'])
    if not after.is_file():
        raise ValueError('Host-after attestation must precede export')
    original_indexer = Path(protocol['retention_index']['script'])
    frozen_indexer = next(item['sha256'] for item in protocol['hash_prerequisites'] if item['path'] == str(original_indexer))
    if digest(original_indexer) != frozen_indexer or frozen_indexer != policy['original_indexer_sha256']:
        raise ValueError('Original frozen indexer provenance differs')
    helper = root/'export-archon-evidence.py'
    if not helper.is_file() or policy_path.parent != root:
        raise ValueError('Transport helper and policy must be explicit evidence-root files')
    prefixes = []
    for spelling in policy['allowed_omission_relative_prefixes']:
        relative = Path(spelling)
        if relative.is_absolute() or '..' in relative.parts or relative.name != '.home':
            raise ValueError('Only explicit contained HOME prefixes may be omitted')
        prefix = root/relative
        if prefix.is_symlink() or prefix.resolve() != prefix:
            raise ValueError('Omission prefix may not be a symlink or escape evidence root')
        if any(below(prefix, prior) or below(prior, prefix) for prior in prefixes):
            raise ValueError('Duplicate or overlapping omission prefixes')
        prefixes.append(prefix)
    requirements = [Path(item['path']) for item in protocol['hash_prerequisites']]
    requirements += [Path(protocol['host_before_attestation']['path']), after]
    requirements += [Path(protocol['metadata_output']), protocol_path, policy_path, helper]
    if any(below(required, prefix) or below(prefix, required) for required in requirements for prefix in prefixes):
        raise ValueError('Omission policy overlaps an explicit required file or evidence root')
    omissions = []
    for prefix in prefixes:
        if not prefix.exists():
            continue
        if not prefix.is_dir():
            raise ValueError('Declared HOME omission is not a directory')
        count, size, links = omission_inventory(prefix)
        omissions.append({'source': str(prefix), 'relative_prefix': prefix.relative_to(root).as_posix(),
                          'files': count, 'bytes': size, 'symlinks': links,
                          'reason': 'Task HOME/download cache only; no required regular-file JSON locator may point inside'})
    temporary = root/('.archon-filtered-export-'+uuid.uuid4().hex+'.writing')
    temporary.mkdir()
    entries, symlinks, sources = [], [], set()

    def add(source, relative):
        source, relative = Path(source), Path(relative)
        original = str(source)
        if original in sources:
            raise ValueError('Duplicate original source identity: '+original)
        if relative.is_absolute() or '..' in relative.parts or source.is_symlink() or not source.is_file():
            raise ValueError('Unsafe or nonregular evidence source: '+original)
        target = temporary/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        before = digest(source)
        if target.exists():
            if digest(target) != before:
                raise ValueError('Retained blob collision')
        else:
            shutil.copyfile(source, target)
        if digest(source) != before or digest(target) != before:
            raise ValueError('Evidence changed during artifact copy: '+original)
        entries.append({'source': original, 'retained': relative.as_posix(), 'sha256': before,
                        'bytes': target.stat().st_size})
        sources.add(original)

    try:
        for parent, dirs, names in os.walk(root, followlinks=False):
            parent = Path(parent)
            for name in list(dirs):
                path = parent/name
                if path == temporary or any(below(path, prefix) for prefix in prefixes):
                    dirs.remove(name)
                elif path.is_symlink():
                    symlinks.append({'source': str(path), 'retained': path.relative_to(root).as_posix(), 'target': str(path.readlink())})
                    dirs.remove(name)
            for name in sorted(names):
                path = parent/name
                if path.is_symlink():
                    symlinks.append({'source': str(path), 'retained': path.relative_to(root).as_posix(), 'target': str(path.readlink())})
                    continue
                if not stat.S_ISREG(path.stat().st_mode):
                    continue
                if path.name.endswith('.writing'):
                    raise ValueError('Incomplete evidence write: '+str(path))
                if path.suffix == '.json':
                    for value in leaves(read(path)):
                        locator = Path(value)
                        if locator.is_absolute() and any(below(locator, prefix) for prefix in prefixes) and locator.is_file():
                            raise ValueError('Required retained JSON regular-file locator would be omitted: '+value)
                add(path, path.relative_to(root))
        # Copy only exact hash-pinned external prerequisites (including two native
        # extensions); never traverse source trees, environments or package caches.
        for claim in [*protocol['hash_prerequisites'], protocol['host_before_attestation']]:
            original = Path(claim['path'])
            if not original.is_absolute() or original.is_symlink() or digest(original) != claim['sha256']:
                raise ValueError('Required prerequisite is missing/nonregular/drifted: '+str(original))
            if str(original) not in sources:
                add(original, Path('prerequisite-by-sha')/claim['sha256']/original.name)
        omission_path = temporary/'transport-omissions.json'
        omission_value = {'schema': 'kicad-route/transport-omissions/1', 'policy_sha256': digest(policy_path),
                          'omissions': omissions, 'totals': {key: sum(row[key] for row in omissions) for key in ('files','bytes','symlinks')},
                          'required_regular_file_json_locators_checked': True,
                          'native_results_timing_metadata_boards_work_baselinecache_ledger_buildlogs_retained': True}
        omission_path.write_text(json.dumps(omission_value,indent=2)+'\n')
        entries.append({'source': str(output/'transport-omissions.json'), 'retained': 'transport-omissions.json',
                        'sha256': digest(omission_path), 'bytes': omission_path.stat().st_size})
        transport = {'schema': 'kicad-route/filtered-evidence-transport/1',
                     'helper': {'source': str(helper), 'sha256': digest(helper)},
                     'policy': {'source': str(policy_path), 'sha256': digest(policy_path)},
                     'omissions': {'source': str(output/'transport-omissions.json'), 'sha256': digest(omission_path)},
                     'original_indexer': {'source': str(original_indexer), 'sha256': frozen_indexer},
                     'original_indexer_executed': False}
        index = {'schema': 'kicad-route/retained-evidence-index/1', 'host': 'archon', 'original_root': str(root),
                 'files': sorted(entries,key=lambda row:row['source']), 'symlinks': symlinks, 'transport': transport,
                 'limitations': 'Artifact transport only; HOME omissions explicit. Exact regular-file lookup only; no symlinks traversed. Index excludes itself; externally observed SHA required.'}
        index_path = temporary/'archon-evidence-index.json'
        index_path.write_text(json.dumps(index,indent=2)+'\n')
        temporary.rename(output)
        return {'index': str(output/'archon-evidence-index.json'), 'sha256': digest(output/'archon-evidence-index.json'),
                'files': len(entries), 'bytes': sum(row['bytes'] for row in entries), 'omissions': omission_value['totals']}
    except BaseException:
        shutil.rmtree(temporary)
        raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path('/evidence'))
    parser.add_argument('--protocol',type=Path,default=Path('/evidence/archon-protocol.json'))
    parser.add_argument('--policy',type=Path,default=Path('/evidence/archon-export-policy.json'))
    parser.add_argument('--output',type=Path,default=Path('/evidence/archon-filtered-export'))
    args=parser.parse_args()
    print(json.dumps(create_export(args.root,args.output,args.protocol,args.policy),indent=2))


if __name__=='__main__':main()
