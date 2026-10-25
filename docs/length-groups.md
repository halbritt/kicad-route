# Single-ended length groups

`run --length-match-group DQ0 DQ1 DQ2 DQ3` requests matching of exact net names
with the builtin engine. Repeat the flag for multiple groups. Optional
`--group-match-tolerance MM`, `--group-meander-amplitude MM` and
`--group-meander-spacing WIDTHS` default to 0.1 mm, 1 mm and 2 track widths.
Spacing is meander arm center spacing. Controls must be finite and positive;
spacing must be at least one width. They are separate from intra-pair controls.

Every member must exist inside the requested routing scope, have exactly two
native pad terminals, and be outside detected coupled pairs. Zone and graphical
copper on a member are refused because this operation measures trace paths.
Terminal reference and pad-number tokens must contain only letters, digits,
underscores or periods so they are exact native `fromTo` selectors. Duplicate
members, orphan controls and conflicting raw backend matching flags refuse
before routing. Other native geometry and rule checks remain active.

`settings.length_match` records the explicit groups, `tolerance_mm`,
`amplitude_mm` and `spacing_widths`. The maintained engine adds meanders.
`validation.length_matching` independently checks the saved/refilled native
board through KiCad `fromTo` paths, with the board hash, requirement, resolved
terminals, every measured member and native skew findings. A matching failure
makes validation fail even when routing has zero opens. `validate` checks the
saved board again.

Each group is checked in its own verification-only project copy so overlapping
groups cannot mask each other through native rule precedence. An added rule
uses a deliberately impossible zero-length maximum to obtain one native
measurement finding per required path, plus the requested skew constraint.
The measurement findings prove coverage; they are not defects in the original
design. Native skew grading uses full precision. Missing or duplicate path
measurements, ignored required length/skew checks, or open connections fail.
The original project and authored rules remain unchanged and are graded
separately. This prevents branch/loop copper from substituting for useful
terminal-to-terminal length.

The executable article is [fixtures/group_matching](../fixtures/group_matching).
This capability covers geometric two-terminal single-ended length matching.
Propagation-time matching, inter-pair matching, AC-composed paths and multipoint
path selection require separate qualification; this operation makes no claim
for them or for signal integrity or manufacturing readiness.
