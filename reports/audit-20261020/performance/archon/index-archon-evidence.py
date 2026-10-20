#!/usr/bin/env python3
"""Index retained Archon evidence without rewriting original absolute JSON locators.

Run only after matrix terminal and host-after attestation has been copied into the
mounted evidence directory. Copies explicit protocol prerequisites, never venvs.
The completed directory can then be copied intact and verified by its index SHA.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import stat


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def create_index(root,output,protocol):
    root=Path(root).resolve();output=Path(output)
    if output.exists():raise ValueError('Refusing to replace an existing evidence index')
    output.relative_to(root)
    items=[];links=[];sources=set()
    def add(source,retained):
        source=str(source);retained=Path(retained)
        relative=retained.relative_to(root).as_posix()
        if source in sources:raise ValueError('Duplicate original source identity: '+source)
        sources.add(source)
        items.append({'source':source,'retained':relative,'sha256':digest(retained),'bytes':retained.stat().st_size})
    # A hash-bound prerequisite may live outside the evidence mount. Copy only
    # that explicit regular file into a content-addressed blob, retaining its
    # exact original identity. No source-tree or environment traversal occurs.
    for entry in [*protocol['hash_prerequisites'],protocol['host_before_attestation']]:
        original=Path(entry['path'])
        if not original.is_absolute():raise ValueError('Prerequisite must have absolute identity')
        if not original.is_file():raise ValueError('Missing regular prerequisite: '+str(original))
        actual=digest(original)
        if actual!=entry['sha256']:raise ValueError('Prerequisite drift: '+str(original))
        try:original.relative_to(root)
        except ValueError:
            target=root/'prerequisite-by-sha'/actual/original.name
            target.parent.mkdir(parents=True,exist_ok=True)
            if target.exists():
                if digest(target)!=actual:raise ValueError('Retained prerequisite blob collision')
            else:shutil.copyfile(original,target)
            add(original,target)
    # Do not follow directory symlinks. Their spelling/target is explicit in a
    # separate table; consumers cannot silently use them as file substitutions.
    for path in sorted(root.rglob('*')):
        if path==output:continue
        mode=path.lstat().st_mode
        if stat.S_ISLNK(mode):
            links.append({'source':str(path),'retained':path.relative_to(root).as_posix(),'target':str(path.readlink())})
        elif stat.S_ISREG(mode):
            if path.name.endswith('.writing'):raise ValueError('Incomplete evidence write: '+str(path))
            add(path,path)
    value={'schema':'kicad-route/retained-evidence-index/1','host':'archon','original_root':str(root),
           'files':sorted(items,key=lambda r:r['source']),'symlinks':links,
           'limitations':'Exact regular-file lookup only. Symlinks are recorded but never traversed. Index excludes itself; retain its independently observed SHA256.'}
    output.write_text(json.dumps(value,indent=2)+'\n')
    return value


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path('/evidence'))
    parser.add_argument('--protocol',type=Path,default=Path('/evidence/archon-protocol.json'))
    parser.add_argument('--output',type=Path,default=Path('/evidence/archon-evidence-index.json'))
    args=parser.parse_args();protocol=json.loads(args.protocol.read_text())
    matrix=json.loads((Path(protocol['metadata_output'])/'matrix.json').read_text())
    if matrix['status'] not in ('PASS','FAIL','EXECUTION_ERROR','INTERRUPTED'):
        raise ValueError('Matrix must be terminal before evidence export')
    after=protocol['host_after_attestation_path']
    if not Path(after).is_file():raise ValueError('Missing host-after attestation')
    create_index(args.root,args.output,protocol)
    print(json.dumps({'index':str(args.output),'sha256':digest(args.output)}))


if __name__=='__main__':main()
