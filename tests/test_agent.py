"""Agent wiring and the write path.

The most valuable test here is the round trip: a write that reports success but
never commits looks identical to one that worked, from the caller's side. Only
reading back afterwards distinguishes them - and that bug shipped once already.
"""
import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def broker(lakebase):
    import f1_broker
    return f1_broker


class TestToolWiring:
    def test_every_advertised_tool_is_callable(self):
        """A tool in the schema list with no dispatch entry is advertised to the
        model and then fails at call time."""
        import sys, os
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
        import agent
        advertised = {name for name, _, _, _ in agent.TOOLS}
        assert advertised == set(agent.DISPATCH), (
            advertised.symmetric_difference(set(agent.DISPATCH)))

    def test_write_tools_are_declared(self):
        import agent
        assert agent.WRITE_TOOLS <= set(agent.DISPATCH)
        assert len(agent.WRITE_TOOLS) == 3

    def test_mcp_server_exposes_the_same_tools(self):
        import asyncio
        from f1_mcp_server import mcp
        import agent
        names = {t.name for t in asyncio.run(mcp.list_tools())}
        assert {n for n, _, _, _ in agent.TOOLS} <= names


class TestWritesPersist:
    """Reading back is the whole point.

    INSERT ... RETURNING through a helper that never commits returns a real row
    with a real id, so the write looks entirely successful and then rolls back
    on connection close. The agent said "saved" and the database disagreed.
    """

    def test_watchlist_write_survives_the_connection(self, broker, marker):
        ref = marker("test")
        written = broker.add_watchlist("constructor", ref, note="round-trip test")
        assert written["written"] is True

        found = [i for i in broker.get_watchlist()["items"]
                 if i["entity_ref"] == ref]
        assert found, "write reported success but the row is not readable"

    def test_note_write_survives_the_connection(self, broker, marker):
        ref = marker("note")
        written = broker.save_note(2024, "Sao Paulo", ref)
        assert written["written"] is True
        assert written["row"]["race_name"]  # resolution reported back

        assert any(n["note"] == ref for n in broker.get_notes(2024)["notes"])

    def test_prediction_requires_valid_confidence(self, broker):
        with pytest.raises(ValueError):
            broker.log_prediction(2024, 1, "something", confidence="certain")

    def test_write_to_unknown_race_is_refused(self, broker):
        with pytest.raises(broker.UnknownRaceError):
            broker.save_note(2024, "Atlantis", "should never be stored")

    def test_watchlist_add_is_idempotent(self, broker, marker):
        ref = marker("idem")
        broker.add_watchlist("circuit", ref)
        broker.add_watchlist("circuit", ref)
        matches = [i for i in broker.get_watchlist()["items"]
                   if i["entity_ref"] == ref]
        assert len(matches) == 1, "adding twice created a duplicate"


class TestTelemetry:
    def test_tool_call_logging_never_raises(self, lakebase):
        """Telemetry must not break the thing it observes. By the time this
        runs the tool has already succeeded."""
        lakebase.log_tool_call(
            tool_name="test_tool", arguments={"k": "v"}, outcome="ok",
            is_write=False, summary="unit test", duration_ms=1)

    def test_logging_failure_is_swallowed(self, lakebase, monkeypatch):
        def explode(*a, **k):
            raise RuntimeError("database on fire")
        monkeypatch.setattr(lakebase, "connection", explode)
        lakebase.log_tool_call("t", {}, "ok")   # must not raise
