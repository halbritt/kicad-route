"""Owned routing implementation, initially extracted from the patched KRT fork.

The worker scripts run in separate processes because their flat module names
and mutable routing state must not leak into the CLI or another attempt.
Upstream attribution is retained in LICENSE and UPSTREAM.json.
"""
