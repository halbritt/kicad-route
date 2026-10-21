Project kicad-route: upstream fixes deployed, 2026-10-21.

KICADROUTE-83 through KICADROUTE-89 are Done, with individual Plane API readbacks. The integrated code is f42b8796de2006bae147e65340da99462b5fd9be; owned engine source SHA-256 is 89a2efbf53057a0694b3cef365ec3acdc01b61f5f57d86bfbe8a62dc8f11db6d. Pushed report commit: 5ca591264c65aa63917212b6781a472edf84054c.

Use --backend builtin. The deprecated --backend krt spelling selects that same implementation owned and built inside kicad-route. The upstream KRT project remains provenance; this CLI alias deprecation does not declare upstream KRT deprecated. The public extraction version stays 0.23.1; distinguish builds by source/build identity.

Six selective repair families are deployed: original rule-floor and legacy-token handling; routable-layer/stub/via-fit guards; differential-pair keepout offset; obstacle cleanup/fresh retry maps and cancellation rollback; rescued-copper write custody; parse-local cache lifetime. Existing owned writer, unsupported-copper refusal and tap/weld repairs remain. Rust/dependency inputs are unchanged. Unselected GUI, typed-via and diff-pair-floor series were not adopted.

Verification:816 tests plus 6 subtests passed with zero failures/errors/skips;48/48 declared Archon benchmark attempts passed native completion, validation, preservation and all unchanged 1.10 wall/GNU-RSS/process-tree-RSS gates. Fresh local installation, offline doctor with canaries and 2 strict native repeats passed independent raw verification. Active local generation is 84b30dc286f44df6b465ae8d40aa1363; generated krt-* diagnostic skills point there. Existing separately pinned states retain their generation until updated.

All four task worktrees/branches and owned Archon containers/images/directory are removed. Failed and one-skip qualification studies are retained alongside the passing study. A parser diagnostic found lower retained text memory but about 2.5x higher unscoped extractor CPU; no universal speedup is claimed. Full evidence and limitations:
https://github.com/halbritt/kicad-route/blob/5ca591264c65aa63917212b6781a472edf84054c/reports/upstream-fixes-20261021/README.md

This completed result supersedes the earlier live handoff for repairs 83–89. Advisory only; no response or additional agent work is requested.
