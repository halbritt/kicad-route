#!/usr/bin/env python3
"""KiCad <-> Specctra bridge for Freerouting, through KiCad's own pcbnew module.

    specctra.py export BOARD.kicad_pcb OUT.dsn [--skip-nets NETS.json] [--default-clearance UM]
                                                     [--dru RULES.kicad_dru] [--clearance-margin UM]
                                                     [--only KEYS.json]
    specctra.py import BOARD.kicad_pcb SESSION.ses OUT.kicad_pcb [--only KEYS.json]

kicad-cli 10 has no DSN export or SES import, so this uses the two-argument
SWIG functions pcbnew.ExportSpecctraDSN(board, path) and
pcbnew.ImportSpecctraSES(board, path), which work headless (the one-argument
forms need a running PCB editor).

export --skip-nets: Freerouting selects work by net class only. Nets outside
the routing scope are moved into a copy of their class (same width, clearance
and via rules) named `<class>__skip`; the caller passes those names to
--router.autorouter.ignore_net_classes. Clearances are unchanged. Nets are
matched by exact KiCad name in class lists read as KiCad writes them (see
read_class); a list that cannot be read unambiguously fails the export.

export --default-clearance: raise the Default net class (`kicad_default`) and
the board-level clearance rule to UM micrometres when they are lower. It only
ever tightens, matching KRT's --clearance (which sets the Default class).

export --dru: KiCad's DSN export ignores custom rules. The subset Specctra can
express -- a minimum clearance between net classes, with no layer, item-type or
area condition -- is added as (class_class ...) rules, which Freerouting reads
into its clearance matrix. Supported conditions:
    A.NetClass == 'X' && B.NetClass == 'Y'   (X against Y)
    A.NetClass == 'X' && B.NetClass != 'X'   (X against every other class)
    A.NetClass == 'X'                        (X against every class)
Every other rule is reported as untranslated; KiCad DRC still grades all rules.

export --clearance-margin: add UM micrometres to every untyped clearance in the
DSN (board rule, class rules, translated class_class rules). Freerouting
approximates pad shapes, so it can land a few micrometres inside a clearance
that KiCad then grades exactly; a margin trades routing room for that.

Pre-existing copper is locked for the round trip. KiCad exports locked items
as (type fix), which Freerouting freezes and never writes back, and its SES
import leaves locked items untouched. Unlocked items would instead be deleted
and rebuilt from the session, where Freerouting may have collapsed short
segments or re-normalised junctions (upstream #958, #960, #987). After import
the original lock flags are restored, so input copper comes back bit-identical.

--only KEYS.json (export and import alike; keys as `locks.py keys` writes them):
lock only the unlocked items whose key is listed (multiset). `--protect human`
uses it so copper an earlier kicad-route step placed exports as (type route)
and is rebuilt from the session, while the design's own copper stays locked.

import: the session's (placement ...) section is dropped before KiCad reads
it, so a session can never move footprints. Zones are not refilled here; the
caller grades with a refill.
"""
import collections
import heapq
import json
import re
import sys
from pathlib import Path


def block_span(text, start):
    """End index (exclusive) of the parenthesised block starting at `start`."""
    depth, i, in_str = 0, start, False
    while i < len(text):
        c = text[i]
        if in_str:
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("unbalanced s-expression")


# ------------------------------------------------- KiCad's DSN net names
#
# KiCad's Specctra writer (OUTPUTFORMATTER::GetQuoteChar, checked against KiCad
# 10 exports) wraps a name in `"` when it is empty, starts with `#`, or holds
# whitespace or one of `(){}%`; it never escapes anything. A (net ...)
# definition first replaces each `"` in the name by `''`, so definitions are
# plain tokens. A class member list writes the name raw: the net `"foo"` is
# `"foo"`, the net `x y"z` is `"x y"z"` and `foo "(bar)"` is `"foo "(bar)""`.
# Those lists are therefore not tokenizable by quote counting; read_class()
# enumerates the spellings KiCad could have produced, prefers readings whose
# members are defined nets, and refuses (ValueError) when the reading is not
# unique. Members are identified by their exact KiCad name, never unquoted.

QUOTED = re.compile(r"[\s(){}%]")
RAW = re.compile(r"[^\s()]+")
DEF_TOKEN = re.compile(r'"[^"]*"|[^\s()"]+')
WS = " \t\r\n"


def needs_quote(name):
    """True when KiCad's DSN writer wraps `name` in quotes."""
    return not name or name.startswith("#") or bool(QUOTED.search(name))


def kicad_token(name):
    """`name` as KiCad writes it in a class member list (raw, quoted when needed)."""
    return f'"{name}"' if needs_quote(name) else name


def dsn_net_token(name):
    """A KiCad net name as KiCad's DSN exporter writes it in (net ...) definitions:
    `"` becomes `''`, then quoted as kicad_token() does."""
    return kicad_token(name.replace('"', "''"))


def unquote(a):
    return a[1:-1] if len(a) >= 2 and a.startswith('"') and a.endswith('"') else a


def skip_ws(text, i):
    while i < len(text) and text[i] in WS:
        i += 1
    return i


def spellings(text, i, strict=True, defined=None, longest=None):
    """[(end, name)]: each way KiCad's class writer could have written a name that
    starts at text[i] and ends at whitespace or `)`. strict: only spellings that
    obey needs_quote() (raw iff the name needs no quotes). With `defined` (and
    `longest`, its longest entry's length), a quoted spelling is offered only when
    it is a defined net or the shortest one, which keeps long lines linear."""
    out = []
    m = RAW.match(text, i)
    if m and (m.end() == len(text) or text[m.end()] in WS + ")") and not (strict and needs_quote(m.group())):
        out.append((m.end(), m.group()))
    if i < len(text) and text[i] == '"':
        j, shortest = i + 1, True
        while (j := text.find('"', j)) >= 0:
            inner = text[i + 1:j]
            if "\n" in inner or "\r" in inner:    # KiCad wraps lines only between names
                break
            if defined is not None and not shortest and len(inner) > longest:
                break
            if (j + 1 == len(text) or text[j + 1] in WS + ")") and (needs_quote(inner) or not strict):
                if defined is None or shortest or inner.replace('"', "''") in defined:
                    out.append((j + 1, inner))
                    shortest = False
            j += 1
    return out


def class_tail(text, i):
    """End (exclusive) of a class block whose sub-expressions start at text[i]
    (`(` ... then the closing `)`), or None when that is not what follows."""
    try:
        while i < len(text) and text[i] == "(":
            i = skip_ws(text, block_span(text, i))
    except ValueError:
        return None
    return i + 1 if i < len(text) and text[i] == ")" else None


ClassBlock = collections.namedtuple("ClassBlock", "start end name name_token members subs")
# members: [(kicad name, token start, token end)]; subs: index of the first sub-expression
# (or of the closing paren when the class has none)


def _readings(text, start, defined, whole, strict):
    """Best readings of the (class ...) at text[start]: fewest members that are not
    defined nets (and class names holding `"`), ties counted (capped at 2).
    Returns (count, ClassBlock when count == 1 else None, {first sub-expression: end} of
    the tied readings)."""
    i = start + len("(class")
    if not text.startswith("(class", start) or i >= len(text) or text[i] not in WS:
        raise ValueError(f"no (class ...) at offset {start}")
    i = skip_ws(text, i)
    longest = max((len(d) for d in defined), default=0) if defined is not None else None
    # node: position of the next member; state: (cost, count, back pointer)
    best = {}                     # (pos, is_name) -> [cost, count, pred, token]
    heap = []

    def reach(node, cost, count, pred, tok):
        cur = best.get(node)
        if cur is None or cost < cur[0]:
            if cur is None:
                heapq.heappush(heap, node)
            best[node] = [cost, count, pred, tok]
        elif cost == cur[0]:
            cur[1] = min(2, cur[1] + count)

    reach((i, True), 0, 1, None, None)
    finals = []                    # (cost, count, node, end)
    while heap:
        node = heapq.heappop(heap)
        pos, is_name = node
        cost, count = best[node][:2]
        if not is_name and pos < len(text) and text[pos] in "()":
            end = class_tail(text, pos)
            if end is not None and (not whole or not text[end:].strip()):
                finals.append((cost, count, node, end))
            continue
        for end, name in spellings(text, pos, strict and not is_name, None if is_name else defined, longest):
            if is_name:  # this bridge writes `"<class>__skip"`; a name holding `"` is the unlikely reading
                extra = int('"' in name)
            else:
                extra = 0 if defined is None or name.replace('"', "''") in defined else 1
            reach((skip_ws(text, end), False), cost + extra, count, node, (name, pos, end))
    if not finals:
        return 0, None, {}
    low = min(f[0] for f in finals)
    finals = [f for f in finals if f[0] == low]
    count = min(2, sum(f[1] for f in finals))
    tails = {f[2][0]: f[3] for f in finals}
    if count != 1:
        return count, None, tails
    _, _, node, end = finals[0]
    toks = []
    while best[node][2] is not None:
        toks.append(best[node][3])
        node = best[node][2]
    toks.reverse()
    (cname, cs, ce), members = toks[0], toks[1:]
    subs = finals[0][2][0]
    return 1, ClassBlock(start, end, cname, text[cs:ce], members, subs), tails


def read_class(text, start, defined=None, whole=False):
    """The (class ...) block at text[start] as KiCad writes it (see above).

    defined: the network's net definitions, unquoted (`''` kept); readings with
    more undefined members lose. whole: the block must end at the end of `text`.
    Falls back to non-strict spellings when no strict reading exists. Raises
    ValueError when no reading, or more than one equally good reading, exists."""
    for strict in (True, False):
        count, res, _ = _readings(text, start, defined, whole, strict)
        if count == 1:
            return res
        if count > 1:
            raise ValueError(f"ambiguous net class member list at offset {start}: {text[start:start + 200]!r}")
    raise ValueError(f"unreadable net class member list at offset {start}: {text[start:start + 200]!r}")


def find_open(text, start=1):
    """Index of the first sub-expression of the (class ...) block `text` (the whole
    block, as KiCad writes it), at or after `start`; -1 when it has none. Member
    names are skipped as KiCad spells them: `"Net-(BT1-+)"` and `"foo "(bar)""`
    carry parentheses that do not open anything."""
    for strict in (True, False):
        count, _, tails = _readings(text, 0, None, True, strict)
        if len(tails) == 1:  # the names may split more than one way; the sub-expressions do not
            subs = next(iter(tails))
            return subs if text[subs] == "(" and subs >= start else -1
        if count:
            raise ValueError(f"net class member list ends in {len(tails)} places: {text[:200]!r}")
    raise ValueError(f"unreadable net class member list: {text[:200]!r}")


Network = collections.namedtuple("Network", "start end defined classes")


def read_network(dsn):
    """The (network ...) section: its span, net definitions ({unquoted: token}) and
    classes (ClassBlock, in order). KiCad writes every (net ...) before the classes."""
    m = re.search(r"\(network\b", dsn)
    if not m:
        raise ValueError("no (network ...) section")
    i, defined, classes = m.end(), {}, []
    while True:
        i = skip_ws(dsn, i)
        if i >= len(dsn):
            raise ValueError("unbalanced (network ...) section")
        if dsn[i] == ")":
            return Network(m.start(), i + 1, defined, classes)
        kw = re.compile(r"\(([^\s()\"]+)").match(dsn, i)
        if not kw:
            raise ValueError(f"unexpected text in (network ...) at offset {i}: {dsn[i:i + 80]!r}")
        if kw.group(1) == "class":
            c = read_class(dsn, i, set(defined) or None)
            classes.append(c)
            i = c.end
            continue
        if kw.group(1) == "net":
            t = DEF_TOKEN.match(dsn, skip_ws(dsn, kw.end()))
            if t:
                defined[unquote(t.group())] = t.group()
        i = block_span(dsn, i)


def split_classes(dsn, skip):
    """Move nets in `skip` (exact KiCad net names) into `<class>__skip` copies of
    their DSN class.

    Members are identified by exact name, as read_class() reads KiCad's raw class
    list: the nets `foo` and `"foo"` never collide. A kept member keeps its token
    as KiCad wrote it; a moved member is written in its (net ...) definition's
    form (`''q''` for the net `"q"`) so Freerouting can match it to its net."""
    net = read_network(dsn)
    out, pos, ignored = [], 0, []
    for c in net.classes:
        drop = [n for n, _, _ in c.members if n in skip]
        if not drop:
            continue
        keep = [dsn[s:e] for n, s, e in c.members if n not in skip]
        moved = [net.defined.get(n.replace('"', "''"), dsn_net_token(n)) for n in drop]
        body = dsn[c.subs:c.end - 1].strip()
        skip_name = f'"{c.name}__skip"'
        new = f"(class {c.name_token} {' '.join(keep)}\n      {body})" if keep else ""
        new += f"\n    (class {skip_name} {' '.join(moved)}\n      {body})"
        out.append(dsn[pos:c.start])
        out.append(new.strip())
        pos = c.end
        ignored.append(f"{c.name}__skip")
    out.append(dsn[pos:])
    return "".join(out), ignored


def raise_default_clearance(dsn, floor_um):
    """Raise the structure rule and the kicad_default class clearance (never lower)."""
    changes = []

    def bump_rule(rule_text, where):
        def bump(m):
            if float(m.group(1)) < floor_um:
                changes.append({"where": where, "from_um": float(m.group(1)), "to_um": floor_um})
                return f"(clearance {floor_um:g})"
            return m.group(0)
        return re.sub(r"\(clearance\s+([0-9.]+)\s*\)", bump, rule_text)

    # board-level rule inside (structure ...)
    m = re.search(r"\(structure\b", dsn)
    if m:
        end = block_span(dsn, m.start())
        struct = dsn[m.start():end]
        r = re.search(r"\n\s*\(rule\b", struct)
        if r:
            rs = r.start() + struct[r.start():].index("(")
            re_ = block_span(struct, rs)
            struct = struct[:rs] + bump_rule(struct[rs:re_], "structure rule") + struct[re_:]
        dsn = dsn[:m.start()] + struct + dsn[end:]
    c = next((c for c in read_network(dsn).classes if c.name == "kicad_default"), None)
    if c:  # only its rules, never its member names
        dsn = dsn[:c.subs] + bump_rule(dsn[c.subs:c.end], "class kicad_default") + dsn[c.end:]
    return dsn, changes


UNIT_MM = {"mm": 1.0, "mil": 0.0254, "in": 25.4, "um": 0.001, None: 1.0}
NC_TERM = re.compile(r"([AB])\.NetClass\s*(==|!=)\s*'([^']+)'")


def sexpr(text):
    tokens = re.findall(r'"(?:\\.|[^"\\])*"|\(|\)|[^\s()"]+', re.sub(r"(?m)^\s*#.*$", "", text))
    stack, cur = [], []
    for t in tokens:
        if t == "(":
            stack.append(cur)
            cur = []
        elif t == ")":
            done, cur = cur, stack.pop()
            cur.append(done)
        else:
            cur.append(t[1:-1] if t.startswith('"') else t)
    return cur


def dru_class_clearances(text):
    """[(name, class_a, class_b|'*other'|'*all', clearance_mm)], [(name, reason)]"""
    done, skipped = [], []
    for rule in sexpr(text):
        if not (isinstance(rule, list) and rule and rule[0] == "rule"):
            continue
        name = rule[1] if len(rule) > 1 and isinstance(rule[1], str) else "?"
        parts = {r[0]: r for r in rule[2:] if isinstance(r, list) and r}
        cons = [r for r in rule[2:] if isinstance(r, list) and r and r[0] == "constraint"]
        extra = set(parts) - {"constraint", "condition", "severity"}
        if extra or len(cons) != 1 or cons[0][1] != "clearance":
            skipped.append((name, "only a single clearance constraint without layer clauses is translatable"))
            continue
        mins = [c for c in cons[0][2:] if isinstance(c, list) and c and c[0] == "min"]
        m = re.fullmatch(r"([0-9.]+)\s*(mm|mil|in|um)?", mins[0][1]) if mins else None
        if not m or len(cons[0]) != 3:
            skipped.append((name, "clearance without a plain min value"))
            continue
        mm = float(m.group(1)) * UNIT_MM[m.group(2)]
        cond = parts.get("condition", [None, ""])[1]
        terms = NC_TERM.findall(cond)
        residue = NC_TERM.sub("", cond).replace("&&", "").strip()
        if residue or not terms:
            skipped.append((name, f"condition {cond!r} is not purely net-class based"))
            continue
        eq = [(side, c) for side, op, c in terms if op == "=="]
        ne = [(side, c) for side, op, c in terms if op == "!="]
        if len(eq) == 2 and not ne and eq[0][0] != eq[1][0]:
            done.append((name, eq[0][1], eq[1][1], mm))
        elif len(eq) == 1 and len(ne) == 1 and ne[0][0] != eq[0][0] and ne[0][1] == eq[0][1]:
            done.append((name, eq[0][1], "*other", mm))
        elif len(eq) == 1 and not ne:
            done.append((name, eq[0][1], "*all", mm))
        else:
            skipped.append((name, f"condition {cond!r} is not a supported net-class pairing"))
    return done, skipped


def add_class_clearances(dsn, rules):
    """Append (class_class ...) rules to (network ...) for translated .kicad_dru rules."""
    if not rules:
        return dsn, []
    net = read_network(dsn)
    classes = [c.name for c in net.classes]

    def dsn_names(kicad_class):  # Default is exported as kicad_default; __skip copies share rules
        base = "kicad_default" if kicad_class == "Default" else kicad_class
        return [c for c in classes if c == base or c == f"{base}__skip"]

    blocks, applied = [], []
    for name, a, b, mm in rules:
        first = dsn_names(a)
        if not first:
            continue
        if b == "*other":
            second = [c for c in classes if c not in first]
        elif b == "*all":
            second = classes
        else:
            second = dsn_names(b)
        um = mm * 1000
        for x in first:
            for y in second:
                blocks.append(f'    (class_class (classes "{x}" "{y}")\n      (rule (clearance {um:g})))')
        applied.append({"rule": name, "classes": [a, b], "clearance_mm": mm, "pairs": len(first) * len(second)})
    if not blocks:
        return dsn, applied
    end = net.end
    return dsn[:end - 1] + "\n".join(blocks) + "\n  " + dsn[end - 1:], applied


def add_margin(dsn, margin_um):
    count = [0]

    def bump(m):
        count[0] += 1
        return f"(clearance {float(m.group(1)) + margin_um:g})"
    return re.sub(r"\(clearance\s+([0-9.]+)\s*\)", bump, dsn), count[0]


def drop_placement(ses):
    m = re.search(r"\(placement\b", ses)
    if not m:
        return ses, False
    end = block_span(ses, m.start())
    return ses[:m.start()] + ses[end:], True


def rewrite_dsn(text, opts, info):
    """The export options applied to KiCad's DSN text, in order; details go to `info`."""
    if "--skip-nets" in opts:
        text, info["ignored_classes"] = split_classes(text, set(json.loads(Path(opts["--skip-nets"]).read_text())))
    if "--dru" in opts:
        rules, skipped = dru_class_clearances(Path(opts["--dru"]).read_text())
        text, info["dru_translated"] = add_class_clearances(text, rules)
        info["dru_untranslated"] = [{"rule": n, "reason": r} for n, r in skipped]
    if "--default-clearance" in opts:
        text, info["clearance_raised"] = raise_default_clearance(text, float(opts["--default-clearance"]))
    if "--clearance-margin" in opts:
        text, n = add_margin(text, float(opts["--clearance-margin"]))
        info["clearance_margin"] = {"um": float(opts["--clearance-margin"]), "rules": n}
    return text


def main(argv):
    import pcbnew

    from locks import key, multiset  # same directory; the same keys lock and restore use

    def lock_existing(board, only=None):
        unlocked = [t for t in board.GetTracks() if not t.IsLocked()]
        if only is not None:
            wanted, chosen = multiset(json.loads(Path(only).read_text())), []
            for t in unlocked:
                k = json.dumps(key(t, pcbnew))
                if wanted.get(k):
                    wanted[k] -= 1
                    chosen.append(t)
            unlocked = chosen
        for t in unlocked:
            t.SetLocked(True)
        return unlocked

    if len(argv) >= 3 and argv[0] == "export":
        board = pcbnew.LoadBoard(argv[1])
        opts = dict(zip(argv[3::2], argv[4::2]))
        temporarily_locked = len(lock_existing(board, opts.get("--only")))
        out = Path(argv[2])
        if not pcbnew.ExportSpecctraDSN(board, str(out)):
            sys.exit("ExportSpecctraDSN failed")
        info = {"dsn": str(out), "ignored_classes": [], "clearance_raised": [],
                "existing_copper_locked_for_export": temporarily_locked}
        if "--only" in opts:
            info["existing_copper_left_unlocked"] = sum(1 for t in board.GetTracks() if not t.IsLocked())
        text = out.read_text()
        try:
            text = rewrite_dsn(text, opts, info)
        except ValueError as e:  # fail closed: never hand a backend a DSN we could not read
            sys.exit(f"DSN rewrite failed: {e}")
        out.write_text(text)
        print("BRIDGE_JSON: " + json.dumps(info))
        return 0
    if len(argv) in (4, 6) and argv[0] == "import" and (len(argv) == 4 or argv[4] == "--only"):
        board = pcbnew.LoadBoard(argv[1])
        ses_path = Path(argv[2])
        text, dropped = drop_placement(ses_path.read_text())
        clean = ses_path.with_suffix(".noplacement.ses")
        clean.write_text(text)
        unlocked = {t.m_Uuid.AsString() for t in lock_existing(board, argv[5] if len(argv) == 6 else None)}
        if not pcbnew.ImportSpecctraSES(board, str(clean)):
            sys.exit("ImportSpecctraSES failed")
        restored = 0
        for t in board.GetTracks():
            if t.m_Uuid.AsString() in unlocked:
                t.SetLocked(False)
                restored += 1
        board.Save(argv[3])
        print("BRIDGE_JSON: " + json.dumps({"placement_dropped": dropped, "session": str(clean),
                                            "existing_copper_locked": len(unlocked),
                                            "lock_flags_restored": restored}))
        return 0
    sys.exit(__doc__)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
