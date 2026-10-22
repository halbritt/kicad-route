# Native copper custody geometry correction

The native-policy producer added `annulus_geometry` and `layer_diameters` to via
facts. The custody reader's exact field set rejected every such via, so native
saved-twin removal failed before preparing a disposable board. This is a source
contract mismatch already present on the K91 prerequisite baseline, not evidence
that all broad-suite failures are environmental.

Production commit `30899505ccda4f7f49864ef33cdc0b2d4ae7e602` requires and retains
the exact current via fields. It checks circular annuli, complete ordered coverage
of the physical layer span, positive integer diameters, and F.Cu size consistency.
Unknown fields, duplicate UUIDs, missing geometry and unsupported annuli refuse.
Native re-extraction and serialized survivor/non-track checks remain in force.
Older facts without complete native geometry cannot qualify with this reader.

Verification:

- Native saved-twin regression before repair: **FAIL**, 3.88 seconds, at the exact
  inventory field check. New native inventory controls: **1 failed, 21 passed**.
- Full focused native custody and inventory suite: **55 passed, zero skips**, in
  117.80 seconds. No unraisable-exception suppression was used.
- Explicit matching-generation public CLI route and disposable native removal:
  **1 passed, zero skips**, in 36.47 seconds. The generation's router source hash
  is `9cfc5d5e9369fd044ac6555afe57bdc4ccc829421e5496d42599c2d4a71951b2`.
- Independent review: **READY within this correction's scope**; 25 focused tests
  passed in 10.65 seconds. Independent saved four-layer through/blind vias retained
  varying native per-layer annuli. Five malformed controls refused; changing an
  existing inner annulus made generated copper nonremovable. See the retained
  independent summary, test log and native reproduction script.

The required `git-land --check` gate runs native policy, the 22 inventory cases,
and the real public CLI custody path on the isolated correction. Its result is
recorded in the integration follow-up. This report does not certify the failing
broad suite, another host, the experiment receipt, or K91 release.

The separate experiment candidate `6e41a13` passed its 57 tests and real eight-run
fixture comparison, but independent review found EI2–EI7: configuration evidence
requalification, origin cycles, malformed JSON shapes, obstacle-only layers,
reference-terminal custody and budget validation. Those findings remain **HOLD**
in the existing continuation lane. This correction does not integrate that code.
No target grading/routing, installation, pin or default changed.

Doctrine provenance: packet `pkt-57aa160ac7217ab8`, content SHA-256
`57aa160ac7217ab8d1dde9eaf63839b9f3196b1244b84d7f6aa1f717053f7ebc`, corpus
`corpus-2026-07-12-a11702cc9217`. The retained packet, typed evidence, verified
decision receipt and citation observations apply repository contract precedence,
evidence before intervention, behavior preservation and separate semantic
checkpoints. All 31 remaining generic obligations have explicit nonmaterial
classifications for this bounded correction; no full K91 claim follows from them.
Reopen if producer geometry changes or exact native preservation fails.
