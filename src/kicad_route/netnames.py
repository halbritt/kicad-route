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
