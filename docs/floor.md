# Rule floor: high-water mark and declared floor

`kicad-route audit` compares a commit with its parent only. Suppose commit N loosens
a rule and commit N+1 routes into the space that opens up. Each audit then looks
clean for N+1: N+1 changed no rule, and its board passes the rules it carries. A
real board went through this pairwise check with `min_resolved_spokes` lowered
from 2 to 1, which hid 24 starved thermals. Nothing recorded the rules the project
meant to keep.

The rule floor records them in two ways:

- **High-water mark.** For each ordered rule setting, the strictest value ever
  committed for the board, and the commit that set it. It comes from git history
  and nobody maintains it.
- **Declared floor.** `<board>.rules-floor.json`, committed next to the board. Its
  entries map a setting path to `{value, reason, owner}`. It is the owner's
  statement of the minimum.

Neither one changes grading. An attempt is still graded under the input's own
rules. The floor only adds findings, and nothing in it relaxes a rule.

## Which way is stricter

`src/kicad_route/floor.py` has an explicit `ORDER` table. The first match wins.
Settings that are not in the table are never ordered: `compare`, `stricter_of` and
`rule_tightness` return `None` for them.

| Setting path | Stricter |
|---|---|
| `rules.min_*` (clearance, track width, via diameter, annular width, hole clearance, hole-to-hole, copper-edge, through-hole and microvia sizes, silk, text, `min_resolved_spokes`, `min_connection`, `min_groove_width`), `rules.solder_mask_to_copper_clearance` | larger |
| `rules.max_*` | smaller |
| `rules.allow_*` (KiCad 6 projects) | `false` |
| `rule_severities.<type>` | error > warning > ignore |
| `drc_exclusions` | fewer: set inclusion, so two sets can be incomparable. A waiver outside the floor's set is looser. |
| `net_settings.classes.<name>.clearance` | larger |
| `net_settings.classes.<name>.{track_width, via_*, microvia_*, diff_pair_*}` | larger, but **router defaults** (below) |
| `dru.<rule>.<constraint>.min` / `.max`, `dru.<rule>.min_resolved_spokes.value` | larger / smaller / larger |
| `dru.<rule>.severity` | error > warning > ignore |

These settings are not ordered:

- `rules.max_error`: the accuracy of arc approximation for fills and shapes. It is
  not a DRC limit.
- `rules.use_height_for_length_calcs`: a calculation mode.
- `opt` values in custom rules.
- Net-class colours, bus and wire widths.
- `netclass_patterns` and `netclass_assignments`.
- The whole-file `.kicad_dru` entry that `audit` lists.

Values in `.kicad_dru` are read per constraint. Distances are converted to mm
(`mm`, `um`, `mil`, `in`) and angles stay in `deg`. A rule's condition and layer are
recorded as its *scope*. When a rule meets its floor value but now applies to a
different scope, a note says so, because the floor compares values and not scopes.

**Router defaults are not ratcheted by default.** A net class's track width, via
size and diff-pair geometry are the values the router and the interactive tools
use. KiCad's DRC does not enforce them as minimums; it enforces `rules.min_*` and
custom rules. They change for design reasons, for example a class widened for one
revision and then put back. So `floor` and `audit` leave them out of the high-water
mark. `floor --netclass-sizes` includes them. A person who wants one held can
declare it, and declared entries are always honoured. Routing attempts still check
new copper against these values (`new_copper_netclass_geometry`).

## `kicad-route floor`

```sh
kicad-route floor [--dir D] [--board B] [--since REV] [--write-declared] [--netclass-sizes]
```

The command reads every commit that touched the board's `.kicad_pro` or `.kicad_dru`,
oldest first, with `git show` and without DRC. It computes the high-water mark from
them and compares the working tree's settings with the mark and with the declared
floor. With `--since REV`, the history starts at REV's state.

- **Output:** `OUT/floor/<project>/floor.json`. It holds `high_water` (value,
  commit, subject and date for each setting), `current`, `declared`,
  `below_declared` and `below_high_water`. A table of the settings that are
  currently looser is printed.
- **Exit status:** 3 when a setting is below the declared floor or the declared
  floor file is invalid. Otherwise 0. Being below the mark is reported, not failed.
- **`--write-declared`:** writes a draft `<board>.rules-floor.json` under
  `OUT/floor/<project>/`, never into the project. The draft is the high-water mark,
  and it never goes below an entry that is already declared. Existing reasons and
  owners are kept, and new entries name the commit that set them. Review the draft,
  fill in owners and reasons, copy it next to the board and commit it.

A setting that the project no longer states is reported as `absent`, for example a
removed net class or custom rule. KiCad then applies a default that the floor
cannot verify.

## Declared floor file

```json
{
  "version": 1,
  "board": "route_demo.kicad_pcb",
  "settings": {
    "rules.min_resolved_spokes": {"value": 2, "reason": "thermal relief on every plane pad", "owner": "hw-lead"},
    "rules.min_track_width": {"value": 0.15, "reason": "fab minimum", "owner": "hw-lead"},
    "dru.power_to_signal_clearance.clearance.min": {"value": 0.4, "reason": "24 V rail", "owner": "hw-lead"}
  }
}
```

A bare mapping of setting path to entry, without the `settings` wrapper, is also
accepted. An entry for an unordered setting, or one with a value of the wrong
kind, cannot be enforced. It is reported, it fails `floor`, it is a finding in
`audit`, and it fails validation in `run`.

Declaring an entry is how an owner accepts a value below the high-water mark.
Once a setting is declared, the declared value is its floor, and the mark still
covers every undeclared setting. The commit that declares a value below the mark
lowers the effective floor, so `audit` reports it (below).

## `audit` integration

`audit --old A --new B` adds `floor` to `audit.json`:

- **`floor_lowered`** (a finding, exit 3): an entry that is in the floor at A and
  `lowered` or `removed` at B, or `added_below_high_water` at B (declared below the
  mark at A). This is never accepted automatically. Audit only reports it, with
  the new owner, for a person to decide.
- **`below_declared`** (a finding): a setting of B below the declared floor. The
  floor used is the stricter of A's and B's, so lowering the floor in the same
  commit does not hide this.
- **`below_high_water`** and **`notes`** (not findings): a setting of B looser than
  its high-water mark in the history up to B, but not below a declared floor. The
  note names the commit that set the mark.
- **`floor_project`** and **`hidden_by_floor`**: when any floor value is stricter
  than B's, audit copies B's project files and merges the stricter values in. The
  value is the declared one where a value is declared, else the mark. The merge
  covers `.kicad_pro` rules, severities, exclusions, net-class values and
  `.kicad_dru` values in place. Audit then grades B's board under this copy.
  `hidden_by_floor` lists what appears only there, like `hidden_by_rule_changes`,
  and a non-empty list is a finding. Floor values that cannot be merged, because
  the class or custom rule no longer exists, are listed under `unapplied` and in
  the notes.

In the two-step case, the audit of N+1 against N shows no rule change. The floor
names the commit that set `min_resolved_spokes` = 2, grades N+1 under 2 and lists
the starved thermals as `hidden_by_floor`.

## `run` pre-flight

Before routing, `run` checks the input's rules against its declared floor. The
check uses `git show` and JSON only, with no history walk and no DRC.

The declared floor is read from these places:

- **Working tree input:** the file in `--dir` and the file committed at `HEAD`.
  For each setting, the stricter value holds, so a floor that is lowered but not
  yet committed and audited still applies.
- **`--rev` input:** the file at that revision.

When the input is below its declared floor, `run` still routes and grades under the
input's own rules. The result records the violation in `floor` and
`validation.floor_violated`, and validation fails with a reason that names the
setting and the owner (exit 3). The record is stored in `attempt.json`, so
`kicad-route validate` applies it again. `--floor-history` also compares the
input with its git high-water mark, and the result is reported as notes only.

## Limitations

- Scope changes are not compared: a custom rule's condition or layer, and net-class
  membership (`netclass_patterns`), are outside the floor. A note names a custom
  rule whose scope changed since its mark, and `audit` lists every pattern change as
  a pairwise rule change.
- A custom rule that has been deleted cannot be merged back into the floor project.
  It is reported as `absent` (a finding against a declared floor, a note against
  the mark) and as `unapplied`.
- KiCad writes its defaults into the `.kicad_pro` when it re-saves it, and those
  defaults join the history. A default stricter than what the project meant becomes
  part of the mark until someone declares the intended value.
- The history follows the board's current file names and does not follow renames.
- A `--dir` chain that starts from a routing candidate has no declared floor file.
  The check applies to a chain through `validate --against <first run>`, which
  uses the first run's floor record.
