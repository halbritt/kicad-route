"""Net names as backend command lines must spell them (found by the canary).

KRT's --nets takes fnmatch patterns through argparse, so a scope given as raw
net names goes wrong three ways:

- a name starting with '-' (`-12V`, `-VIN(1)`) is read as an option and KRT
  exits 2 ("unrecognized arguments");
- `*`, `?` and `[` in a name are wildcards;
- a name without a sheet path (`GND`) also matches the leaf of every
  sheet-qualified net (`/Analog/GND`), by KRT's design (#292/#493).

krt_patterns() turns an exact scope into patterns that select exactly it: glob
characters and a leading '-' become one-character classes (`[*]`, `[-]`), a
leading '!' becomes KRT's escaped literal `\\!`, and sheet-qualified nets that
an unqualified in-scope name would also catch are excluded by exact name.
Ordinary names (`/SIG_A`, `GND` on a board without a `/x/GND`) are unchanged.
"""
import re


def exact(name):
    p = re.sub(r"([*?\[])", r"[\1]", name)
    if p.startswith("-"):
        p = "[-]" + p[1:]
    elif p.startswith("!"):
        p = "\\" + p
    return p


def krt_patterns(scope, all_nets):
    scope = sorted(scope)
    inside = set(scope)
    bare = {n for n in scope if "/" not in n}
    caught = sorted(n for n in set(all_nets) - inside
                    if "/" in n and n.rsplit("/", 1)[-1] in bare and not n.startswith("!"))
    # an exclusion carries a '/', so KRT full-matches it (no leaf rule)
    return [exact(n) for n in scope] + ["!" + exact(n) for n in caught]


# ------------------------------------------------- net names in DRC item text

NO_NET = "<no net>"  # what KiCad prints between the brackets for unconnected copper


def _closes(text, j):
    """A ']' at j that KiCad could have written after a net name: the end of the
    text or a separator follows it (`[/A] of C1`, `[/A] on F.Cu`, `[/A],`)."""
    return j + 1 == len(text) or text[j + 1] in " ,;:)"


def nets_in_description(text, known=None):
    """Net names KiCad wrapped in square brackets in a DRC item description
    (`Pad 1 [/BUS[0]] of C1 on F.Cu` -> ['/BUS[0]']), in order.

    A net name may itself carry brackets (`/BUS[0]`, `/D[7..0]`), so the first ']'
    does not end it. With `known` (the board's net names), the longest known name
    that sits exactly between '[' and ']' wins, whatever brackets it carries.
    Otherwise the bracket group is read balanced, and a group that is unbalanced
    (`/A]b`, `/B[c`) ends at the first ']' a separator or the end of the text
    follows. A name neither rule recovers can still come back wrong; callers that
    grade with it check the result against the board's nets (see cli.item_nets)."""
    out, i, n = [], 0, len(text)
    known = known if isinstance(known, (set, frozenset, dict)) else set(known or ())
    while i < n:
        if text[i] != "[":
            i += 1
            continue
        hit = next((j for j in range(n - 1, i, -1) if text[j] == "]" and text[i + 1:j] in known), None)
        if hit is not None:
            out.append(text[i + 1:hit])
            i = hit + 1
            continue
        depth, j, end = 0, i, None
        while j < n:
            if text[j] == "[":
                depth += 1
            elif text[j] == "]":
                depth -= 1
                if depth == 0:
                    end = j
                    break
            j += 1
        if end is None or not _closes(text, end):
            end = next((j for j in range(i + 1, n) if text[j] == "]" and _closes(text, j)), end)
        if end is None:
            break
        if end > i + 1:
            out.append(text[i + 1:end])
        i = end + 1
    return out
