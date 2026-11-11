"""The builtin routing engine: upstream KRT at a recorded commit plus local changes.

UPSTREAM.json names the upstream commit and lists every local change;
docs/upstream-engine.md has the command that moves the engine to a newer one.
The worker scripts run in separate processes because their flat module names
and mutable routing state must not leak into the CLI or another attempt.
Upstream attribution is retained in LICENSE and UPSTREAM.json.
"""
