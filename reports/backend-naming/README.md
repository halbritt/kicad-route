# Built-in backend naming (KICADROUTE-82)

The canonical backend is now `builtin`. It selects the engine built from the routing source maintained in kicad-route. `krt` remains a deprecated CLI alias and warns on stderr. New results identify either spelling as `builtin`; Freerouting keeps its existing name.

Implementation is pushed in `8bafe2d563317764e3a7de7b41bcf7d8eb369bf2`. The installed CLI points to main, generated skill headers are refreshed, and installed offline/no-canary doctor passed. The qualified engine generation and state manifest are unchanged. Historical state keys, identity fingerprints, receipts, log paths and `krt-*` skill names remain compatible.

[Verification](verification.json) records the 156-test landing check, separate interface/evidence/pair checks and two successful native default/alias routes. These are bounded checks, not a full-suite or new performance qualification. Original local raw runs and corrected test-environment history are retained under `build/agent497-builtin-qualification/`.

[Deployment](deployment.json) records the installed command, generation, source digest and unchanged manifest. [Plane readback](plane-readback.json) confirms the item is Done. After deployment, the advisory was published to all 21 other live agents; publication does not establish that each recipient has read it.

The [decision](decision.json) retains the compatibility boundary and [doctrine packet](doctrine-packet.json). All seven remaining generic obligations have explicit nonmaterial dispositions in [the obligation ledger](obligation-disposition.json).
