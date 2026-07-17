---
description: Conventions for adding or modifying tests.
applyTo: "tests/**/*.py"
---

# Test conventions

- **Pin behavior, not code shape.** A test that asserts "input X → output Y" survives refactors. A test that asserts "function foo is called" breaks on unrelated changes.
- **Monkeypatch external calls.** subprocess.run, gh_api, urllib.request.urlopen, Claude CLI. Grep existing tests for `monkeypatch.setattr` for patterns.
- **One file per concern.** `test_bot_token_default.py`, `test_backend_rates.py`, `test_inline_refinement_chain.py` — mirror the src/ area they cover.
- **Fixtures at module level.** Not conftest.py unless truly cross-cutting; keeps the test file self-contained.
- **Full suite must stay green.** `python3 -m pytest tests/ -q` before every commit.
