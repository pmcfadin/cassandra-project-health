"""The `project-health label` local labeling tool (DECISIONS.md D18, D22;
COMMUNITY-HEALTH.md §1.2; issue #46).

See `server.py` (the stdlib `http.server` app, bound to 127.0.0.1 only),
`store.py` (corpus reading + append-only label JSONL), `label_set.py` (the
versioned, verbatim copy of the §1.2 label reference table), and
`safety.py` (the "never run against the public repo checkout" guard).
"""
