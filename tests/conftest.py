"""Shared fixtures.

Tests are split by what they need:

  - Pure-logic tests import only the modules under test and run anywhere.
  - Tests touching Lakebase are marked `integration` and skip cleanly when no
    credentials are configured, so `pytest tests/` is always runnable.

That split matters here. Every bug this suite covers was found by running the
system, not by unit-testing it - so the tests exist to stop regressions, and
they must not require the whole platform to be up in order to do that.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Order matters, and getting it wrong is not hypothetical. mcp_server/ contains
# a GENERATED copy of f1lake (build_app.sh puts it there so a Databricks App can
# import it), and that copy carries only the runtime modules - no load.py. Put
# mcp_server first on sys.path and `import f1lake.load` resolves to the copy and
# fails. The repo root must win; mcp_server is appended only so f1_broker is
# importable.
sys.path.insert(0, ROOT)
sys.path.append(os.path.join(ROOT, "mcp_server"))


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: needs a live Lakebase connection")


@pytest.fixture(scope="session")
def lakebase():
    """Yield the schema module, or skip if Lakebase is not reachable."""
    try:
        from f1lake import schema
        schema.query("SELECT 1")
        return schema
    except Exception as exc:
        pytest.skip(f"Lakebase unavailable: {str(exc)[:80]}")
