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

Literal active-low names and glob characters are escaped for matching's
case-sensitive, whole-name shell-glob semantics. Matching does not inherit
the routing filter's leaf-name expansion or `!` exclusion syntax. The maintained
backend's raw matching patterns use standard `*`, `?` and bracket classes;
`[*]`, `[?]` and `[[]` select literal glob characters. The public operation
performs that encoding for exact names automatically.

Use `--length-match-group-json '["-DQ0", "--help"]'` when a name begins with
a hyphen, or when serializing a request. It accepts one JSON array per group;
repeat it for multiple groups. Both input forms share validation and may be
mixed in order. Saved campaign/corpus commands use the JSON form so group
boundaries and exact names survive replay.

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

## Nominal propagation delay

Use `--group-delay-tolerance PS` instead of `--group-match-tolerance MM` to
match the explicit group against its authored KiCad 10 tuning profiles. For example:

```sh
kicad-route run --dir fixtures/delay_matching \
  --length-match-group-json '["DQ0","DQ1"]' \
  --group-delay-tolerance 1 --group-meander-amplitude 3 --grid-step .1
```

Every member must resolve to an enabled single-ended native tuning profile.
Declare the complete native stackup, including every dielectric sheet's thickness
and permittivity, positive per-layer widths/delays, and via delay. Native via-height
calculation must remain enabled. The profile's delay fields use KiCad internal
attoseconds/mm; the public tolerance is in picoseconds. The native project schema
must retain the assignments when loaded: KiCad migrates old net-settings schemas
by clearing tuning-profile names. Missing or ineffective inputs refuse before
routing. Saved tracks on unmodelled layers or with different widths fail.

The meander engine uses these rates and the signal's used via span, including
native endpoint-copper thickness semantics and declared transition overrides.
Already connected in-scope members set the target and retain their valid geometry.
Final native `fromTo` delay and skew checks independently grade useful paths.
Every member must produce a positive delay measurement; absent/ignored checks,
opens and excessive skew fail. The result binds both PCB and project hashes.

This operation matches nominal authored delays. The designer must substantiate
profile values against the actual stackup and signal geometry; it does not
calibrate a physical model, establish impedance, account for process variation,
or prove signal integrity. KiCad does not automatically refresh delay values
when the stackup changes. Existing geometric matching is unchanged; AC-composed,
inter-pair and package-delay qualification remain separate work.
