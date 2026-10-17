"""Isolated snapshot of a KiCad project: the board, project and rule files, every
schematic sheet reachable from the root, library tables and the local libraries
they point at. The source tree is never modified; `--rev` snapshots a committed
revision with `git archive` instead of the working tree.
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .common import git, sha256, write_json


def expand(path):
    return Path(os.path.expanduser(str(path)))


KICAD_SUFFIXES = {".kicad_sch", ".kicad_pcb", ".kicad_pro", ".kicad_dru", ".kicad_wks",
                  ".kicad_sym", ".lib", ".dcm", ".mod"}
LIB_DIR_SUFFIXES = {".pretty", ".3dshapes"}
URI_RE = re.compile(r'\(uri\s+"?([^")]+?)"?\)')
SHEETFILE_RE = re.compile(r'\(property\s+"Sheet ?file"\s+"([^"]+)"')


def resolve_sheet(project_dir, name, by=None):
    """Exact sheet file, or a unique case-insensitive match (what KiCad sees on
    the case-insensitive filesystems it is often authored on)."""
    candidates = [Path(name)]
    if by and Path(by).parent != Path("."):
        candidates.insert(0, Path(by).parent / name)
    for cand in candidates:
        if (project_dir / cand).exists():
            return str(cand), None
        parent = (project_dir / cand).parent
        if parent.is_dir():
            hits = [p.name for p in parent.iterdir() if p.name.lower() == cand.name.lower()]
            if len(hits) == 1:
                rel_hit = str(cand.parent / hits[0]) if cand.parent != Path(".") else hits[0]
                return rel_hit, hits[0]
    return None, None


def sheet_files(project_dir, root):
    """Schematic files reachable from the root, the sheet-instance count, and
    references whose exact filename does not exist."""
    files, instances, problems = [], 1, []
    seen = {}

    def walk(path, multiplicity):
        nonlocal instances
        if path not in seen:
            files.append(path)
            seen[path] = SHEETFILE_RE.findall((project_dir / path).read_text(errors="replace"))
        for child in seen[path]:
            instances += multiplicity
            actual, case_match = resolve_sheet(project_dir, child, by=path)
            if actual is None:
                problems.append({"referenced": child, "by": path, "resolved": None})
                continue
            if case_match:
                problems.append({"referenced": child, "by": path, "resolved": actual})
            walk(actual, multiplicity)

    walk(root, 1)
    return files, instances, problems


def export_revision(cfg, run_dir):
    """Materialise the project directory as committed at cfg['rev'] (git archive)."""
    src = cfg["path"]
    top = Path(git(src, "rev-parse", "--show-toplevel"))
    prefix = src.relative_to(top)
    commit = git(top, "rev-parse", "--verify", f"{cfg['rev']}^{{commit}}")
    if not commit:
        sys.exit(f"{cfg['rev']!r} is not a commit in {top}")
    out = run_dir / "source" / src.name
    out.mkdir(parents=True)
    archive = subprocess.run(["git", "-C", str(top), "archive", "--format=tar", commit, "--", str(prefix) if str(prefix) != "." else "."],
                             capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(out), *(["--strip-components", str(len(prefix.parts))] if prefix.parts else [])],
                   input=archive, check=True)
    return out, commit


def make_snapshot(cfg, run_dir):
    """Copy the KiCad-relevant project files; never touch the source tree."""
    src = cfg["path"]
    origin_parent = src.parent       # where ${KIPRJMOD}/../<dir> really lives
    rev_commit = None
    if cfg.get("rev"):
        src, rev_commit = export_revision(cfg, run_dir)
    snap_root = run_dir / "snapshot"
    dst = snap_root / src.name
    dst.mkdir(parents=True)
    issues = []

    def copy(relpath):
        s, d = src / relpath, dst / relpath
        if d.exists() or d.is_symlink():
            return
        d.parent.mkdir(parents=True, exist_ok=True)
        if s.is_dir():
            shutil.copytree(s, d, symlinks=True)
        elif s.exists():
            shutil.copy2(s, d)

    for entry in sorted(src.iterdir()):
        if entry.is_file() and (entry.suffix in KICAD_SUFFIXES or entry.name in ("sym-lib-table", "fp-lib-table")):
            copy(entry.name)
        elif entry.is_dir() and entry.suffix in LIB_DIR_SUFFIXES:
            copy(entry.name)
    sch_files, sheet_instances, sheet_problems = sheet_files(src, cfg["schematic"])
    for f in sch_files:
        copy(f)
    case_aliases = []
    for prob in sheet_problems:
        if prob["resolved"] is None:
            issues.append(f"sheet file {prob['referenced']!r} (referenced by {prob['by']}) does not exist")
            continue
        alias = dst / prob["referenced"]
        if not alias.exists():
            alias.parent.mkdir(parents=True, exist_ok=True)
            alias.symlink_to(Path(prob["resolved"]).name)
        case_aliases.append(prob)
        issues.append(f"sheet file {prob['referenced']!r} (referenced by {prob['by']}) exists only as "
                      f"{prob['resolved']!r}; a case-sensitive filesystem cannot open it. The snapshot "
                      "adds a case alias so the checks cover that sheet; the source repo is unchanged.")

    # Library directories mapped to the shared FOME library repository.
    library_links = []
    for dirname, lib in cfg.get("library_dirs", {}).items():
        target = expand(lib)
        here = src / dirname
        if here.is_dir() and any(here.iterdir()):
            copy(dirname)
            continue
        link = dst / dirname
        if link.exists():
            shutil.rmtree(link)
        link.symlink_to(target)
        pinned = None
        tree = git(cfg["path"], "ls-tree", rev_commit or "HEAD", dirname)
        if tree and " commit " in tree:
            pinned = tree.split()[2]
        actual = git(target, "rev-parse", "HEAD")
        entry = {"path": dirname, "target": str(target), "submodule_pin": pinned, "library_head": actual}
        if pinned and actual and pinned != actual:
            issues.append(f"{dirname}: submodule pins {pinned} but {target} is at {actual}")
        library_links.append(entry)

    # Resolve ${KIPRJMOD} references from the library tables.
    for table in ("sym-lib-table", "fp-lib-table"):
        tpath = src / table
        if not tpath.exists():
            continue
        for uri in URI_RE.findall(tpath.read_text()):
            if not uri.startswith("${KIPRJMOD}/"):
                if "${KIPRJMOD}" not in uri and not uri.startswith("${KICAD"):
                    if not expand(uri).exists():
                        issues.append(f"{table}: {uri} does not exist")
                continue
            relpath = Path(uri[len("${KIPRJMOD}/"):])
            original = Path(os.path.normpath(src / relpath))
            if not original.is_relative_to(src):
                original = Path(os.path.normpath(origin_parent / src.name / relpath))
            if original.is_relative_to(src):
                inner = original.relative_to(src)
                if not (src / inner).exists() and not (dst / inner).exists():
                    issues.append(f"{table}: {uri} does not exist")
                elif inner.parts and not (dst / inner.parts[0]).exists():
                    copy(inner.parts[0])
                continue
            up = 0
            for part in relpath.parts:
                if part != "..":
                    break
                up += 1
            if up != 1 or len(relpath.parts) < 2:
                issues.append(f"{table}: {uri} leaves the project more than one level; unsupported")
                continue
            first = relpath.parts[1]
            link, target = snap_root / first, origin_parent / first
            if not target.exists():
                issues.append(f"{table}: {uri} does not exist")
            elif not link.exists():
                link.symlink_to(target)
                library_links.append({"path": f"../{first}", "target": str(target),
                                      "library_head": git(target, "rev-parse", "HEAD")})
            if not original.exists():
                issues.append(f"{table}: {uri} does not exist")

    files = {}
    for root, dirs, names in os.walk(dst):  # does not descend into symlinked dirs
        for n in names:
            p = Path(root) / n
            if not p.is_symlink():
                files[str(p.relative_to(dst))] = sha256(p)
    files = dict(sorted(files.items()))
    if rev_commit:
        head, dirty = rev_commit, None
    else:
        head = git(src, "rev-parse", "HEAD")
        dirty = git(src, "status", "--porcelain", "--", *[f for f in files if (src / f).exists()]) if head else None
    manifest = {
        "project": cfg["name"],
        "source_dir": str(cfg["path"]),
        "source": f"git revision {cfg['rev']} ({rev_commit})" if rev_commit else "working tree",
        "source_git_head": head,
        "source_dirty_files": dirty.splitlines() if dirty else [],
        "schematic": cfg["schematic"],
        "pcb": cfg.get("pcb"),
        "schematic_files": sch_files,
        "sheet_instances": sheet_instances,
        "sheet_case_aliases": case_aliases,
        "missing_sheets": [p for p in sheet_problems if p["resolved"] is None],
        "library_links": library_links,
        "library_issues": issues,
        "files": files,
    }
    write_json(run_dir / "snapshot.json", manifest)
    return dst, manifest
