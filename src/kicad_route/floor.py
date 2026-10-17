"""Rule floor: the strictest rule values a project has committed (its high-water
mark) and the floor a person declares in `<board>.rules-floor.json`.

A pairwise audit compares a commit only with its parent, so a rule loosened in
one commit and used in the next looks clean each time. The floor closes that gap:

- `rule_tightness` / `compare` / `stricter_of` order each rule setting by the
  explicit ORDER table (which way is stricter; unordered settings give None);
- `history` reads the rule settings of every commit that touched the board's
  `.kicad_pro`/`.kicad_dru` (git show, no DRC) and `high_water` keeps, per
  setting, the strictest value ever committed and the commit that set it;
- the declared floor is committed beside the board, setting path ->
  {value, reason, owner}. Lowering it is never accepted automatically: audit
  reports `floor_lowered` and exits 3 so a person decides;
- `audit_floor` checks an audited revision against both and builds a "floor
  project" (the new revision's project files with the floor's stricter values
  merged in) for grading the new board, so findings the looser rules hide are
  reported (`hidden_by_floor`);
- `preflight` checks a routing input against its declared floor (cheap: no
  history walk unless asked); a violation fails the attempt's validation.

Grading itself never changes: an attempt is still graded under the input's own
rules. The floor only adds findings; nothing here relaxes a rule.
"""
import fnmatch
import json
import re
import shutil
import subprocess
from pathlib import Path

from . import common

FLOOR_SUFFIX = ".rules-floor.json"
EPS = 1e-9  # mm values come from JSON and the rule file; equal within float noise

SEVERITY_RANK = {"ignore": 0, "warning": 1, "error": 2}

# Which way is stricter, per setting path (first match wins). Paths are the ones
# audit's project_rule_changes emits, plus per-constraint paths into the custom
# rule file (dru.<rule>.<constraint>.min|max, dru.<rule>.severity).
#   larger          larger value is stricter (DRC minimums)
#   smaller         smaller value is stricter (DRC maximums)
#   severity        error > warning > ignore
#   subset          fewer DRC exclusions is stricter (set inclusion; a partial order)
#   false           a disallowed permission (False) is stricter
#   router_default  larger is stricter, but the value is what the router and the
#                   interactive tools use, not a DRC minimum: declared floors may
#                   name it, the high-water mark only ratchets it on request
#   None            not a constraint, or no direction: never ordered
ORDER = [
    ("rules.max_error", None, "arc-approximation accuracy for zone fills and shapes, not a limit"),
    ("rules.use_height_for_length_calcs", None, "a length-calculation mode, not a limit"),
    ("rules.min_*", "larger", "board DRC minimum"),
    ("rules.solder_mask_to_copper_clearance", "larger", "board DRC minimum"),
    ("rules.max_*", "smaller", "board DRC maximum"),
    ("rules.allow_*", "false", "board permission (KiCad 6 projects)"),
    ("rule_severities.*", "severity", "DRC severity"),
    ("drc_exclusions", "subset", "DRC waivers"),
    ("net_settings.classes.*.clearance", "larger", "net-class clearance, enforced by DRC"),
    ("net_settings.classes.*.track_width", "router_default", "net-class width, a router default"),
    ("net_settings.classes.*.via_diameter", "router_default", "net-class via, a router default"),
    ("net_settings.classes.*.via_drill", "router_default", "net-class via, a router default"),
    ("net_settings.classes.*.microvia_diameter", "router_default", "net-class microvia, a router default"),
    ("net_settings.classes.*.microvia_drill", "router_default", "net-class microvia, a router default"),
    ("net_settings.classes.*.diff_pair_width", "router_default", "net-class pair width, a router default"),
    ("net_settings.classes.*.diff_pair_gap", "router_default", "net-class pair gap, a router default"),
    ("net_settings.classes.*.diff_pair_via_gap", "router_default", "net-class pair via gap, a router default"),
    ("dru.*.severity", "severity", "custom rule severity"),
    ("dru.*.min_resolved_spokes.value", "larger", "custom rule minimum"),
    ("dru.*.min", "larger", "custom rule minimum"),
    ("dru.*.max", "smaller", "custom rule maximum"),
]


def order_of(setting):
    """The ORDER family of a setting path, or None when it is not ordered."""
    for pattern, family, _ in ORDER:
        if fnmatch.fnmatchcase(setting, pattern):
            return family
    return None


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def exclusion_keys(entries):
    """KiCad stores an exclusion as a string, or (KiCad 9+) as [string, comment]."""
    keys = set()
    for e in entries or []:
        k = e[0] if isinstance(e, (list, tuple)) and e else e
        if isinstance(k, str):
            keys.add(k)
    return sorted(keys)


def rule_tightness(setting, value):
    """A key where larger means stricter, comparable between values of one setting;
    None when the setting is unordered or the value is not of its kind. For
    `drc_exclusions` the key is the set of waivers, ordered by inclusion (fewer is
    stricter), so two keys may be incomparable: use compare()."""
    family = order_of(setting)
    if family in ("larger", "router_default"):
        return _number(value)
    if family == "smaller":
        n = _number(value)
        return None if n is None else -n
    if family == "severity":
        return SEVERITY_RANK.get(value) if isinstance(value, str) else None
    if family == "false":
        return (0 if value else 1) if isinstance(value, bool) else None
    if family == "subset":
        return frozenset(exclusion_keys(value)) if isinstance(value, list) else None
    return None


def compare(setting, a, b):
    """1 when a is stricter than b, 0 when equally strict, -1 when looser, None when
    the setting is unordered, a value is missing or invalid, or (exclusions) neither
    set contains the other."""
    ka, kb = rule_tightness(setting, a), rule_tightness(setting, b)
    if ka is None or kb is None:
        return None
    if isinstance(ka, frozenset):
        return 0 if ka == kb else 1 if ka < kb else -1 if ka > kb else None
    return 0 if abs(ka - kb) <= EPS else (1 if ka > kb else -1)


def at_least(setting, value, floor):
    """True when value is at least as strict as floor, False when it is looser
    (for exclusions: any waiver outside the floor's), None when it cannot be told."""
    if order_of(setting) == "subset":
        kv, kf = rule_tightness(setting, value), rule_tightness(setting, floor)
        return None if kv is None or kf is None else kv <= kf
    c = compare(setting, value, floor)
    return None if c is None else c >= 0


def stricter_of(setting, a, b):
    """The stricter of two values (for exclusions, the waivers both allow). A missing
    side yields the other; None for unordered settings or invalid values."""
    if a is None or b is None:
        return b if a is None else a
    if order_of(setting) == "subset":
        ka, kb = rule_tightness(setting, a), rule_tightness(setting, b)
        return None if ka is None or kb is None else sorted(ka & kb)
    c = compare(setting, a, b)
    return None if c is None else (a if c >= 0 else b)


# ------------------------------------------------------------ the custom rules

DRU_TOKEN = re.compile(r'\s+|#[^\n]*|\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+')
NUMBER = re.compile(r'^([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*([A-Za-z]*)$')
MM_PER = {"mm": 1.0, "um": 0.001, "mil": 0.0254, "mils": 0.0254, "in": 25.4, "": 1.0}


class Atom(str):
    """A token of the rule file with its span, so values can be rewritten in place."""
    def __new__(cls, text, start, end):
        a = str.__new__(cls, text[1:-1] if text.startswith('"') else text)
        a.start, a.end, a.quoted = start, end, text.startswith('"')
        return a


def parse_sexpr(text):
    """The rule file as nested lists of Atoms (comments dropped)."""
    stack, top = [], []
    cur = top
    for m in DRU_TOKEN.finditer(text):
        tok = m.group()
        if tok.isspace() or tok.startswith("#"):
            continue
        if tok == "(":
            stack.append(cur)
            cur.append([])
            cur = cur[-1]
        elif tok == ")":
            if not stack:
                raise ValueError("unbalanced ')' in rule file")
            cur = stack.pop()
        else:
            cur.append(Atom(tok, m.start(), m.end()))
    if stack:
        raise ValueError("unbalanced '(' in rule file")
    return top


def dru_value(atom):
    """(value, unit kind) of a rule-file number: distances in mm, angles in deg,
    plain numbers as written; None when it is not a number."""
    m = NUMBER.match(str(atom))
    if not m:
        return None
    n, unit = float(m.group(1)), m.group(2).lower()
    if unit in MM_PER:
        return n * MM_PER[unit], ("mm" if unit else "")
    if unit == "deg":
        return n, "deg"
    return None


def dru_rules(text):
    """{setting path: (value, atom)} and {rule name: scope text} of a rule file.
    Ordered paths: dru.<rule>.<constraint>.min|max, dru.<rule>.min_resolved_spokes.value,
    dru.<rule>.severity. The scope (condition and layer) is kept so a floor can say
    when a rule it compares now applies elsewhere."""
    values, scopes, seen = {}, {}, {}
    try:
        tree = parse_sexpr(text or "")
    except ValueError:
        return values, scopes
    for node in tree:
        if not (isinstance(node, list) and len(node) >= 2 and node[0] == "rule" and isinstance(node[1], Atom)):
            continue
        name = str(node[1])
        seen[name] = seen.get(name, 0) + 1
        if seen[name] > 1:
            name = f"{name}#{seen[name]}"
        scope = []
        for child in node[2:]:
            if not (isinstance(child, list) and child and isinstance(child[0], Atom)):
                continue
            head = child[0]
            if head in ("condition", "layer"):
                scope.append(f"{head} {' '.join(str(a) for a in child[1:] if isinstance(a, Atom))}")
            elif head == "severity" and len(child) > 1 and isinstance(child[1], Atom):
                values[f"dru.{name}.severity"] = (str(child[1]), child[1])
            elif head == "constraint" and len(child) > 1 and isinstance(child[1], Atom):
                ctype = str(child[1])
                for part in child[2:]:
                    if isinstance(part, list) and len(part) == 2 and part[0] in ("min", "max") \
                            and isinstance(part[1], Atom) and dru_value(part[1]):
                        values[f"dru.{name}.{ctype}.{part[0]}"] = (dru_value(part[1])[0], part[1])
                    elif isinstance(part, Atom) and dru_value(part):
                        values[f"dru.{name}.{ctype}.value"] = (dru_value(part)[0], part)
        scopes[name] = "; ".join(scope)
    return values, scopes


def dru_rule_of(setting):
    return setting[len("dru."):].rsplit(".", 1 if setting.endswith(".severity") else 2)[0]


# ------------------------------------------------------- settings of a revision

def project_settings(pro, dru_text):
    """Flat {setting path: value} of every ordered rule setting of one revision,
    plus the custom rules' scopes."""
    out = {}
    ds = (pro or {}).get("board", {}).get("design_settings", {})
    for key in ("rules", "rule_severities"):
        for k, v in (ds.get(key) or {}).items():
            out[f"{key}.{k}"] = v
    if isinstance(ds.get("drc_exclusions"), list):
        out["drc_exclusions"] = exclusion_keys(ds["drc_exclusions"])
    for c in (pro or {}).get("net_settings", {}).get("classes") or []:
        for k, v in c.items():
            if k != "name":
                out[f"net_settings.classes.{c.get('name')}.{k}"] = v
    dru, scopes = dru_rules(dru_text)
    out.update({k: v for k, (v, _) in dru.items()})
    return {k: v for k, v in out.items() if rule_tightness(k, v) is not None}, scopes


def board_files(board):
    stem = Path(board).stem
    return f"{stem}.kicad_pro", f"{stem}.kicad_dru", f"{stem}{FLOOR_SUFFIX}"


def dir_settings(project_dir, board):
    pro, dru, _ = board_files(board)
    p, d = Path(project_dir) / pro, Path(project_dir) / dru
    return project_settings(common.load_json(p), d.read_text() if d.exists() else None)


class Repo:
    """The git repository holding a project directory, for reading old revisions."""
    def __init__(self, project_dir):
        self.dir = Path(project_dir)
        self.top = common.git(self.dir, "rev-parse", "--show-toplevel")
        self.prefix = common.git(self.dir, "rev-parse", "--show-prefix") or ""

    def __bool__(self):
        return bool(self.top)

    def commit(self, rev):
        return common.git(self.dir, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}") if self else None

    def show(self, commit, name):
        """A file of the project directory as committed, or None."""
        r = subprocess.run(["git", "-C", self.top, "show", f"{commit}:{self.prefix}{name}"],
                           capture_output=True, text=True)
        return r.stdout if r.returncode == 0 else None

    def settings(self, commit, board):
        pro, dru, _ = board_files(board)
        text = self.show(commit, pro)
        try:
            data = json.loads(text) if text else None
        except json.JSONDecodeError:
            data = None
        return project_settings(data, self.show(commit, dru))

    def log(self, rev, board, since=None):
        """Commits (oldest first) up to rev that touched the board's rule files; with
        since, the state at since is the starting point and older commits are ignored."""
        pro, dru, _ = board_files(board)
        span = f"{since}..{rev}" if since else rev
        out = common.git(self.top, "log", "--reverse", "--format=%H%x00%cI%x00%s", span, "--",
                         f"{self.prefix}{pro}", f"{self.prefix}{dru}") or ""
        commits = [dict(zip(("commit", "date", "subject"), line.split("\0", 2))) for line in out.splitlines() if line]
        if since:
            c = self.commit(since)
            info = common.git(self.top, "show", "-s", "--format=%H%x00%cI%x00%s", c).split("\0", 2)
            commits.insert(0, dict(zip(("commit", "date", "subject"), info)))
        return commits


def history(repo, rev, board, since=None):
    revs = []
    for c in repo.log(rev, board, since):
        values, scopes = repo.settings(c["commit"], board)
        revs.append({**c, "values": values, "scopes": scopes})
    return revs


def high_water(revs, netclass_sizes=False):
    """Per ordered setting, the strictest value ever committed and the (oldest)
    commit that set it. Net-class sizes (router defaults) only with netclass_sizes."""
    marks = {}
    for r in revs:
        for setting, value in r["values"].items():
            if order_of(setting) == "router_default" and not netclass_sizes:
                continue
            m = marks.get(setting)
            stricter = stricter_of(setting, m["value"] if m else None, value)
            if stricter is None or (m and compare(setting, stricter, m["value"]) != 1):
                continue  # ties keep the oldest commit
            marks[setting] = {"value": stricter, "commit": r["commit"], "date": r["date"],
                              "subject": r["subject"]}
            if setting.startswith("dru."):
                marks[setting]["scope"] = r["scopes"].get(dru_rule_of(setting))
    return dict(sorted(marks.items()))


# --------------------------------------------------------------- declared floor

def parse_declared(data, source):
    """{setting: {value, reason, owner}} from a declared floor file's JSON, and the
    entries that cannot be enforced (unordered setting, wrong value kind)."""
    if not isinstance(data, dict):
        return {}, [{"setting": None, "source": source, "problem": "not a JSON object"}]
    entries = data["settings"] if isinstance(data.get("settings"), dict) else \
        {k: v for k, v in data.items() if k not in ("version", "board", "note")}
    floor, bad = {}, []
    for setting, e in entries.items():
        e = e if isinstance(e, dict) else {"value": e}
        if order_of(setting) is None:
            bad.append({"setting": setting, "source": source, "problem": "not an ordered rule setting"})
        elif rule_tightness(setting, e.get("value")) is None:
            bad.append({"setting": setting, "source": source, "problem": f"value {e.get('value')!r} is not valid"})
        else:
            value = exclusion_keys(e["value"]) if order_of(setting) == "subset" else e["value"]
            floor[setting] = {"value": value, "reason": e.get("reason"), "owner": e.get("owner"),
                              "source": source}
    return floor, bad


def load_declared_text(text, source):
    if text is None:
        return None, []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return {}, [{"setting": None, "source": source, "problem": f"invalid JSON: {e}"}]
    return parse_declared(data, source)


def combine(*floors):
    """The stricter value per setting across declared floors (fail closed: a floor
    lowered in one place still holds where it was not)."""
    out = {}
    for f in floors:
        for setting, e in (f or {}).items():
            cur = out.get(setting)
            if cur is None or not at_least(setting, cur["value"], e["value"]):
                out[setting] = dict(e, value=stricter_of(setting, cur["value"], e["value"]) if cur else e["value"])
    return out


LOWERING = ("lowered", "removed", "added_below_high_water")


def floor_changes(old, new, marks=None):
    """Changes between two declared floors. Kinds in LOWERING lower the effective
    floor and need an owner decision: an entry lowered or removed, or one added
    below the high-water mark it replaces (marks: the mark at the old revision)."""
    rows = []
    for setting in sorted(set(old or {}) | set(new or {})):
        a, b = (old or {}).get(setting), (new or {}).get(setting)
        m = (marks or {}).get(setting)
        if a and not b:
            kind = "removed"
        elif b and not a:
            kind = "added_below_high_water" if m and at_least(setting, b["value"], m["value"]) is False else "added"
        else:
            kind = "lowered" if at_least(setting, b["value"], a["value"]) is False else \
                "raised" if compare(setting, b["value"], a["value"]) == 1 else None
        if kind:
            rows.append({"setting": setting, "kind": kind, "old": a["value"] if a else m and m["value"],
                         "new": b and b["value"],
                         "old_owner": a and a.get("owner"), "new_owner": b and b.get("owner"),
                         "new_reason": b and b.get("reason")})
    return rows


# ---------------------------------------------------------------- comparisons

def below(values, floor, scopes=None):
    """Rows for every floor setting the values do not meet: looser, or absent (a rule
    a revision no longer states cannot be shown to meet the floor)."""
    rows = []
    for setting, f in sorted(floor.items()):
        cur = values.get(setting)
        ok = at_least(setting, cur, f["value"]) if cur is not None else None
        if ok:
            continue
        row = {"setting": setting, "current": cur, "floor": f["value"],
               "status": "absent" if cur is None else "looser" if ok is False else "invalid"}
        row.update({k: f[k] for k in ("commit", "subject", "date", "reason", "owner", "source") if k in f})
        if setting.startswith("dru.") and scopes is not None and "scope" in f:
            now = scopes.get(dru_rule_of(setting))
            if now is not None and now != f["scope"]:
                row["scope_changed"] = {"at_floor": f["scope"], "now": now}
        rows.append(row)
    return rows


def scope_notes(values, floor, scopes):
    """Custom rules that meet their floor value but now apply to a different scope."""
    notes = []
    for setting, f in sorted(floor.items()):
        if setting.startswith("dru.") and "scope" in f and values.get(setting) is not None:
            now = scopes.get(dru_rule_of(setting))
            if now is not None and now != f["scope"]:
                notes.append(f"{setting}: rule scope changed since {f['commit'][:12]} "
                             f"({f['scope']!r} -> {now!r}); the floor compares values, not scopes")
    return sorted(set(notes))


def describe(row, against):
    cur = row["status"] if row["status"] != "looser" else repr(row["current"])
    if against == "declared":
        who = f" (owner {row['owner']})" if row.get("owner") else ""
        return f"{row['setting']} {cur} is below the declared floor {row['floor']!r}{who}"
    return (f"{row['setting']} {cur} is looser than its high-water mark {row['floor']!r} set by "
            f"{row['commit'][:12]} ({row['subject']})")


def effective_floor(declared, marks):
    """Per setting, the declared value where a person declared one (it may accept a
    value below the high-water mark: that is what declaring is for, and adding or
    lowering such an entry is itself a finding), else the high-water mark."""
    floor = {s: m["value"] for s, m in marks.items()}
    floor.update({s: d["value"] for s, d in declared.items()})
    return floor


def write_floor_project(src, dst, board, floor, values):
    """dst: a copy of the project at src with every floor value stricter than the
    project's own merged into its .kicad_pro and .kicad_dru. Returns the settings
    applied and the ones that could not be (class or custom rule no longer there)."""
    pro_name, dru_name, _ = board_files(board)
    shutil.copytree(src, dst, symlinks=True)
    pro = common.load_json(dst / pro_name) or {}
    dru_path = dst / dru_name
    dru_text = dru_path.read_text() if dru_path.exists() else ""
    dru_atoms, _ = dru_rules(dru_text)
    ds = pro.setdefault("board", {}).setdefault("design_settings", {})
    classes = {c.get("name"): c for c in pro.get("net_settings", {}).get("classes") or []}
    applied, unapplied, edits = [], [], []
    for setting, value in sorted(floor.items()):
        if value is None or at_least(setting, values.get(setting), value):
            continue
        ok = True
        if setting.startswith(("rules.", "rule_severities.")):
            key, name = setting.split(".", 1)
            ds.setdefault(key, {})
            if ds[key] is None:
                ds[key] = {}
            ds[key][name] = value
        elif setting == "drc_exclusions":
            allowed = set(value)
            ds["drc_exclusions"] = [e for e in ds.get("drc_exclusions") or [] if exclusion_keys([e])
                                    and exclusion_keys([e])[0] in allowed]
        elif setting.startswith("net_settings.classes."):
            name, key = setting[len("net_settings.classes."):].rsplit(".", 1)
            ok = name in classes
            if ok:
                classes[name][key] = value
        elif setting.startswith("dru.") and setting in dru_atoms:
            atom = dru_atoms[setting][1]
            if setting.endswith(".severity"):
                text = value
            else:
                text = f"{value:.6g}{dru_value(atom)[1]}"  # mm, deg, or unitless as written
            edits.append((atom.start, atom.end, text))
        else:
            ok = False
        (applied if ok else unapplied).append({"setting": setting, "project": values.get(setting), "floor": value})
    for start, end, text in sorted(edits, reverse=True):
        dru_text = dru_text[:start] + text + dru_text[end:]
    (dst / pro_name).write_text(json.dumps(pro, indent=2) + "\n")
    if edits:
        dru_path.write_text(dru_text)
    return applied, unapplied


# ---------------------------------------------------------------------- audit

def audit_floor(project_dir, board, old_commit, new_commit, new_ref, work):
    """Floor checks for `audit`: the declared floor at --old and --new (lowering is
    a finding), the high-water mark of the history up to --new, and where the new
    revision falls below either. When any floor value is stricter than the new
    revision, a floor project for grading the new board is written under work."""
    repo = Repo(project_dir)
    _, _, floor_name = board_files(board)
    old_decl, old_bad = load_declared_text(repo.show(old_commit, floor_name), f"{old_commit[:12]}:{floor_name}")
    new_decl, new_bad = load_declared_text(repo.show(new_commit, floor_name), f"{new_commit[:12]}:{floor_name}")
    declared = combine(old_decl, new_decl)
    revs = history(repo, new_commit, board)
    marks = high_water(revs)
    old_marks = high_water(history(repo, old_commit, board))
    changes = floor_changes(old_decl, new_decl, old_marks)
    values, scopes = dir_settings(new_ref, board)
    below_declared = below(values, declared, scopes)
    flagged = {r["setting"] for r in below_declared}
    below_mark = [r for r in below(values, marks, scopes) if r["setting"] not in flagged]
    floor = effective_floor(declared, marks)
    project = None
    applied = unapplied = []
    if any(not at_least(s, values.get(s), v) for s, v in floor.items() if v is not None):
        project = Path(work) / Path(new_ref).name
        applied, unapplied = write_floor_project(new_ref, project, board, floor, values)
        if not applied:
            project = None
    return {
        "declared_file": floor_name,
        "declared": {"old": old_decl, "new": new_decl},
        "declared_problems": old_bad + new_bad,
        "floor_lowered": [c for c in changes if c["kind"] in LOWERING],
        "floor_raised": [c for c in changes if c["kind"] not in LOWERING],
        "history": {"commits": len(revs), "first": revs[0]["commit"] if revs else None,
                    "high_water": marks},
        "below_declared": below_declared,
        "below_high_water": below_mark,
        "scope_notes": scope_notes(values, {**marks, **{k: v for k, v in declared.items() if k not in marks}}, scopes),
        "floor_project": {"dir": common.rel(project) if project else None, "applied": applied,
                          "unapplied": unapplied},
        "project_dir": project,
    }


def audit_report(fl, hidden):
    """(record for audit.json, findings, notes) from audit_floor and the findings the
    new board shows only under the floor project."""
    findings, notes = [], []
    if fl["floor_lowered"]:
        findings.append(f"declared rule floor lowered ({len(fl['floor_lowered'])}; needs an owner decision): "
                        + ", ".join(f"{c['setting']} {c['old']!r} -> {c['new']!r} ({c['kind']}"
                                    f"{', owner ' + str(c['new_owner']) if c.get('new_owner') else ''})"
                                    for c in fl["floor_lowered"][:6]))
    if fl["below_declared"]:
        findings.append(f"{len(fl['below_declared'])} setting(s) below the declared floor: "
                        + "; ".join(describe(r, "declared") for r in fl["below_declared"][:6]))
    if hidden:
        findings.append(f"{len(hidden)} finding(s) appear only under the rule floor: "
                        + ", ".join(f"{k} {n}" for k, n in common.count_by(hidden, "type").items()))
    for p in fl["declared_problems"]:
        findings.append(f"declared floor {p['source']}: {p['setting'] or 'file'}: {p['problem']}")
    notes += [describe(r, "high_water") for r in fl["below_high_water"]]
    notes += fl["scope_notes"]
    if fl["floor_project"]["unapplied"]:
        notes.append("floor values not applied to the floor project (class or custom rule no longer exists): "
                     + ", ".join(u["setting"] for u in fl["floor_project"]["unapplied"]))
    record = {k: v for k, v in fl.items() if k != "project_dir"}
    record["hidden_by_floor"] = {"count": len(hidden), "by_type": common.count_by(hidden, "type"),
                                 "items": hidden[:200]}
    record["notes"] = notes
    return record, findings, notes


# -------------------------------------------------------------------- routing

def declared_for_input(cfg, board):
    """The declared floor that governs a routing input: the file in the working tree
    and the committed one at HEAD (or the file at --rev), stricter value per setting,
    so a floor lowered but not yet committed (and audited) still holds."""
    _, _, name = board_files(board)
    repo = Repo(cfg["path"])
    floors, problems, sources = [], [], []
    texts = []
    if cfg.get("rev"):
        c = repo.commit(cfg["rev"])
        texts.append((repo.show(c, name) if c else None, f"{cfg['rev']}:{name}"))
    else:
        p = Path(cfg["path"]) / name
        texts.append((p.read_text() if p.exists() else None, str(p)))
        head = repo.commit("HEAD")
        if head:
            texts.append((repo.show(head, name), f"HEAD:{name}"))
    for text, source in texts:
        f, bad = load_declared_text(text, source)
        if f is not None:
            floors.append(f)
            sources.append(source)
        problems += bad
    return combine(*floors), sources, problems, repo


def preflight(cfg, ref_dir, board, with_history=False):
    """Check a routing input's rules against its declared floor (and, with
    with_history, against its git high-water mark). Cheap: git show and JSON only.
    None when the input has no declared floor and no history was asked for."""
    declared, sources, problems, repo = declared_for_input(cfg, board)
    if not sources and not problems and not with_history:
        return None
    values, scopes = dir_settings(ref_dir, board)
    violated = below(values, declared, scopes)
    rec = {"declared_sources": sources, "declared": declared, "declared_problems": problems,
           "floor_violated": violated, "notes": []}
    if with_history:
        rev = repo.commit(cfg.get("rev") or "HEAD")
        marks = high_water(history(repo, rev, board)) if rev else {}
        flagged = {r["setting"] for r in violated}
        rec["below_high_water"] = [r for r in below(values, marks, scopes) if r["setting"] not in flagged]
        rec["notes"] = [describe(r, "high_water") for r in rec["below_high_water"]]
        rec["history"] = {"rev": rev, "high_water": marks}
    return rec


def apply_preflight(rec, validation):
    """Fail validation when the input is below its declared floor; the grading
    itself still uses the input's own rules."""
    if not rec:
        return
    validation["floor_violated"] = rec["floor_violated"]
    if rec["floor_violated"]:
        validation["reasons"].append(
            f"input rules below the declared rule floor ({len(rec['floor_violated'])}): "
            + "; ".join(describe(r, "declared") for r in rec["floor_violated"][:4]))
    for p in rec.get("declared_problems") or []:
        validation["reasons"].append(f"declared rule floor {p['source']}: {p['setting'] or 'file'}: {p['problem']}")


# ---------------------------------------------------------------- the command

def floor_command(cfg, since=None, write_declared=False, netclass_sizes=False):
    """`kicad-route floor`: the high-water mark of the board's rule history, the
    declared floor, and the current (working tree) settings below either."""
    board = cfg["pcb"]
    repo = Repo(cfg["path"])
    if not repo:
        print(f"[floor] {cfg['path']} is not in a git repository: no history to read")
        return 1
    head = repo.commit("HEAD")
    if since and not repo.commit(since):
        print(f"[floor] {since!r} is not a commit")
        return 1
    revs = history(repo, head, board, since) if head else []
    marks = high_water(revs, netclass_sizes)
    values, scopes = dir_settings(cfg["path"], board)
    declared, sources, problems, _ = declared_for_input(dict(cfg, rev=None), board)
    below_declared = below(values, declared, scopes)
    flagged = {r["setting"] for r in below_declared}
    below_mark = [r for r in below(values, marks, scopes) if r["setting"] not in flagged]
    out = common.out_dir() / "floor" / cfg["name"]
    out.mkdir(parents=True, exist_ok=True)
    rec = {"kind": "floor", "project": cfg["name"], "board": board, "head": head, "since": since,
           "netclass_sizes": netclass_sizes,
           "commits": [{k: r[k] for k in ("commit", "date", "subject")} for r in revs],
           "high_water": marks, "current": values,
           "declared_sources": sources, "declared": declared, "declared_problems": problems,
           "below_declared": below_declared, "below_high_water": below_mark,
           "scope_notes": scope_notes(values, marks, scopes)}
    rec["exit_code"] = 3 if below_declared or problems else 0
    if write_declared:
        draft = draft_declared(declared, marks, board)
        path = out / board_files(board)[2]
        path.write_text(json.dumps(draft, indent=2) + "\n")
        rec["draft_declared"] = common.rel(path)
    common.write_json(out / "floor.json", rec)
    print(f"[floor] {len(revs)} commit(s) touched the rules of {board}; {len(marks)} ordered setting(s) tracked")
    rows = [("below declared", r) for r in below_declared] + [("below high-water", r) for r in below_mark]
    if rows:
        print("| status | setting | current | floor | set by |")
        print("|---|---|---|---|---|")
        for label, r in rows:
            by = f"declared ({r.get('owner') or 'no owner'})" if label == "below declared" else \
                f"{r['commit'][:12]} {r['subject']}"
            cur = r["status"] if r["status"] != "looser" else repr(r["current"])
            print(f"| {label} | {r['setting']} | {cur} | {r['floor']!r} | {by} |")
    else:
        print("[floor] no setting is looser than its high-water mark or declared floor")
    for n in rec["scope_notes"]:
        print(f"[floor] NOTE: {n}")
    for p in problems:
        print(f"[floor] declared floor {p['source']}: {p['setting'] or 'file'}: {p['problem']}")
    if write_declared:
        print(f"[floor] draft declared floor: {rec['draft_declared']} -- review it, set reason and owner, then "
              f"copy it to {Path(cfg['path']) / board_files(board)[2]} and commit it")
    print(f"[floor] {common.rel(out / 'floor.json')}  (exit {rec['exit_code']})")
    return rec["exit_code"]


def draft_declared(declared, marks, board):
    """A starting declared floor: the high-water mark, never below what is already
    declared. Existing reasons and owners are kept; new entries name their commit."""
    settings = {}
    for setting in sorted(set(declared) | set(marks)):
        d, m = declared.get(setting), marks.get(setting)
        value = stricter_of(setting, d and d["value"], m and m["value"])
        if d and at_least(setting, d["value"], value):
            settings[setting] = {k: d.get(k) for k in ("value", "reason", "owner")}
        else:
            settings[setting] = {"value": value,
                                 "reason": f"high-water mark set by {m['commit'][:12]} ({m['subject']})",
                                 "owner": d.get("owner") if d else None}
    return {"version": 1, "board": board,
            "note": "Declared rule floor: the board's rules may not go below these values. "
                    "Lowering an entry is an owner decision (kicad-route audit reports floor_lowered).",
            "settings": settings}
