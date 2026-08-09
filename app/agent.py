"""
The assistant, running inside the frontend.

Same tools as the MCP server, exercised through the same `f1_broker` functions -
including the three that write. The MCP server exposes them over the protocol so
a Databricks agent can call them; this module calls them in-process so the app
itself is a working demo rather than a page that tells you to go and use one.

WHY IN-PROCESS AND NOT OVER MCP
--------------------------------
The MCP server is a separate Databricks App behind its own OAuth. Calling it
from here would mean this app authenticating to that app on every turn - a
second network hop and a second identity to get wrong - to reach Python
functions that are already importable. The MCP surface exists for external
agents; a shared broker means both paths run identical code, so a bug in one is
a bug in both rather than a divergence.

TOOL RESULTS ARE TRUNCATED BEFORE THEY REACH THE MODEL
-------------------------------------------------------
`get_driver_season` returns 24 races of detail. Feeding that back verbatim
burns the context window and buys nothing - the model needs the summary, not
every row. Each result is trimmed to what is answerable from.
"""

import json
import logging
import time
import uuid

import requests
from databricks.sdk import WorkspaceClient

import f1_broker
from f1lake import schema

logger = logging.getLogger("agent")

MODEL = "databricks-meta-llama-3-3-70b-instruct"
MAX_TURNS = 5          # tool round-trips before we stop and answer with what we have
TIMEOUT = 90

SYSTEM_PROMPT = """You are an F1 race analyst assistant. You answer questions about \
Formula 1 using ONLY the tools provided. You have no F1 knowledge of your own.

WHAT YOU HAVE
- Race results and championship standings for 2024-2026 (from a data pipeline).
- The narrative of 58 Wikipedia race reports, searchable by meaning.
- MEASURED race-day weather from a weather archive, per circuit per race date.

TOOL USE
- Call the most specific tool for the question. Do not call them all.
- For "what happened in race X", use search_race_reports - the narrative is
  there, the results table is not a story.
- For "why did X win/lose" or anything about pit stops, tyres or strategy, call
  get_race_strategy AND search_race_reports. The numbers say what the strategy
  WAS; the report says why it was chosen and whether it worked.
- Ask search_race_reports for at least 5 passages. One or two is not enough to
  find the sentence that answers a question, and a thin search is how you end
  up saying "the report does not mention it" when it does.
- For "was it wet", use get_race_weather. That is measurement, not opinion.
- When a user asks you to track, predict, or note something, actually call the
  write tool. Then confirm what you saved, quoting the row you got back.

THE ONE THING THAT MATTERS MOST
Measured rainfall is a DAILY total. It cannot tell you whether rain fell during
the race or overnight. If someone asks whether a race was wet, check both the
weather AND the race report - the report says whether the track was actually
wet while they were racing. Monza 2024 had the most rain of any race here and
ran dry. Say so when it applies; never treat rainfall alone as proof.

WHEN THE QUESTION IS WRONG
Users ask loaded questions - "why did Ferrari lose Monza?" when Ferrari won it.
Correct the premise FIRST, in the opening sentence, then answer what actually
happened. Never say "it is not possible to determine why they lost" - that
reads as a failure to answer when you in fact have the answer. Say "Ferrari did
not lose - Leclerc won on a one-stop" and carry on.

GUARDRAILS
- Never state a figure you did not get from a tool call in this conversation.
- If a tool returns an "error" key, follow its "suggestion". Ask the user to
  clarify rather than guessing.
- Every result tells you what it resolved - "resolved_driver", "resolved_race".
  If that does not match what the user meant, say which one you used.
- Be brief. Lead with the answer, then the numbers that support it.
- Never open with what you could not find. Open with what you know.
- When strategy explains a result, say so concretely: how many stops each
  driver made, and who did something different from the field."""


# Tool schemas. Kept beside the dispatch table so a tool cannot be advertised
# without being callable.
TOOLS = [
    ("get_driver_season", "A driver's race-by-race results and season totals.",
     {"driver": ("string", "Driver name, surname or code, e.g. 'Verstappen'"),
      "season": ("integer", "Championship year, e.g. 2024")}, ["driver", "season"]),
    ("compare_constructors", "Compare two teams' form across a season.",
     {"constructor_a": ("string", "First team, e.g. 'Ferrari'"),
      "constructor_b": ("string", "Second team, e.g. 'McLaren'"),
      "season": ("integer", "Championship year")}, ["constructor_a", "constructor_b", "season"]),
    ("get_championship_standings", "Drivers' championship standings after a round.",
     {"season": ("integer", "Championship year"),
      "round": ("integer", "Round number; omit for the latest")}, ["season"]),
    ("get_race_weather", "MEASURED race-day weather for one race.",
     {"season": ("integer", "Championship year"),
      "race": ("string", "Round number or race name, e.g. 'Sao Paulo'")}, ["season", "race"]),
    ("find_wet_races", "Races where measured rainfall crossed the wet threshold.",
     {"season": ("integer", "Restrict to one year; omit for all"),
      "limit": ("integer", "Max races, 1-25")}, []),
    ("search_race_reports", "Search race-report narrative by meaning.",
     {"query": ("string", "What to look for, in plain language"),
      "top_k": ("integer", "How many passages, 1-10"),
      "season": ("integer", "Restrict to one year")}, ["query"]),
    ("get_race_strategy", "Pit stops and stints for one race, per driver - how a result was won.",
     {"season": ("integer", "Championship year"),
      "race": ("string", "Round number, race name or circuit, e.g. 'Suzuka'")},
     ["season", "race"]),
    ("find_strategy_races", "Races where teams disagreed most about strategy.",
     {"season": ("integer", "Championship year"),
      "limit": ("integer", "Max races, 1-25")}, ["season"]),
    ("get_watchlist", "List tracked drivers, constructors and circuits.", {}, []),
    ("get_predictions", "List previously logged predictions.",
     {"season": ("integer", "Restrict to one year")}, []),
    ("get_race_notes", "List saved race notes.",
     {"season": ("integer", "Restrict to one year")}, []),
    # --- writes ---
    ("add_to_watchlist", "WRITE. Track a driver, constructor or circuit.",
     {"entity_type": ("string", "'driver', 'constructor' or 'circuit'"),
      "entity_ref": ("string", "Who or what to track"),
      "note": ("string", "Optional reason")}, ["entity_type", "entity_ref"]),
    ("log_prediction", "WRITE. Record a prediction against a race.",
     {"season": ("integer", "Championship year"),
      "race": ("string", "Round number or race name"),
      "prediction": ("string", "What you predict, in plain language"),
      "confidence": ("string", "'low', 'medium' or 'high'"),
      "rationale": ("string", "Why")}, ["season", "race", "prediction"]),
    ("save_race_note", "WRITE. Save an analyst note against a race.",
     {"season": ("integer", "Championship year"),
      "race": ("string", "Round number or race name"),
      "note": ("string", "The note text")}, ["season", "race", "note"]),
]

WRITE_TOOLS = {"add_to_watchlist", "log_prediction", "save_race_note"}

DISPATCH = {
    "get_driver_season": lambda a: f1_broker.driver_season(a["driver"], a["season"]),
    "compare_constructors": lambda a: f1_broker.compare_constructors(
        a["constructor_a"], a["constructor_b"], a["season"]),
    "get_championship_standings": lambda a: f1_broker.championship_standings(
        a["season"], a.get("round")),
    "get_race_weather": lambda a: f1_broker.race_weather(a["season"], a["race"]),
    "find_wet_races": lambda a: f1_broker.wet_races(a.get("season"), a.get("limit", 10)),
    "search_race_reports": lambda a: f1_broker.search_reports(
        a["query"], a.get("top_k", 5), a.get("season")),
    "get_race_strategy": lambda a: f1_broker.race_strategy(a["season"], a["race"]),
    "find_strategy_races": lambda a: f1_broker.strategy_spread(a["season"], a.get("limit", 8)),
    "get_watchlist": lambda a: f1_broker.get_watchlist(),
    "get_predictions": lambda a: f1_broker.get_predictions(a.get("season")),
    "get_race_notes": lambda a: f1_broker.get_notes(a.get("season")),
    "add_to_watchlist": lambda a: f1_broker.add_watchlist(
        a["entity_type"], a["entity_ref"], a.get("note")),
    "log_prediction": lambda a: f1_broker.log_prediction(
        a["season"], a["race"], a["prediction"],
        a.get("confidence", "medium"), a.get("rationale")),
    "save_race_note": lambda a: f1_broker.save_note(a["season"], a["race"], a["note"]),
}


def _schemas() -> list[dict]:
    out = []
    for name, desc, props, required in TOOLS:
        out.append({"type": "function", "function": {
            "name": name, "description": desc,
            "parameters": {
                "type": "object",
                "properties": {k: {"type": t, "description": d}
                               for k, (t, d) in props.items()},
                "required": required,
            }}})
    return out


def _trim(name: str, result) -> dict:
    """Shrink a tool result to what the model can actually answer from.

    get_driver_season returns 24 races of detail and search returns full
    passages; sending those back verbatim floods the context and degrades the
    answer. The fields dropped here are the ones the UI shows, not the ones the
    model reasons over.
    """
    if not isinstance(result, dict):
        return {"result": result}
    out = dict(result)
    if name == "get_driver_season" and "results" in out:
        out["results"] = [
            {k: r.get(k) for k in ("round", "race_name", "grid_position",
                                   "finish_position", "total_points", "status")}
            for r in out["results"]
        ]
    if name == "search_race_reports" and "results" in out:
        out["results"] = [
            {"season": r.get("season"), "round": r.get("round"),
             "race_name": r.get("race_name"), "section": r.get("section"),
             "similarity": round(float(r.get("similarity") or 0), 3),
             "was_wet": r.get("was_wet"), "precipitation_mm": r.get("precipitation_mm"),
             "text": (r.get("chunk_text") or "")[:700]}
            for r in out["results"]
        ]
    if name == "get_race_strategy" and "by_driver" in out:
        out["by_driver"] = out["by_driver"][:12]
    if name == "get_championship_standings" and "standings" in out:
        out["standings"] = out["standings"][:12]
    return out


def _post(messages: list[dict]) -> dict:
    w = WorkspaceClient()
    headers = w.config.authenticate()
    headers["Content-Type"] = "application/json"
    response = requests.post(
        f"{w.config.host.rstrip('/')}/serving-endpoints/{MODEL}/invocations",
        headers=headers,
        json={"messages": messages, "tools": _schemas(), "tool_choice": "auto",
              "max_tokens": 900, "temperature": 0.1},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]


def _summarise(name: str, result) -> str | None:
    """One readable line per call, so the analytics table shows what happened
    rather than a JSON blob."""
    if not isinstance(result, dict):
        return None
    if result.get("error"):
        return f"{result['error']}: {str(result.get('message',''))[:120]}"
    for key in ("resolved_driver", "resolved_race", "ahead_on_points", "leader"):
        if result.get(key):
            return f"{key}={result[key]}"
    if result.get("written"):
        return f"wrote {result.get('action')}"
    if "results" in result:
        return f"{len(result['results'])} passages"
    if "races" in result:
        return f"{len(result['races'])} races"
    return None


def ask(question: str, history: list[dict] | None = None,
        session_id: str | None = None) -> dict:
    """Answer a question, calling tools as needed.

    Returns the answer plus the full trace of tool calls, because a demo that
    only shows the prose gives no way to tell whether the agent used its tools
    or made the answer up.
    """
    session_id = session_id or uuid.uuid4().hex[:12]
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages += history or []
    messages.append({"role": "user", "content": question})

    trace = []
    for _ in range(MAX_TURNS):
        message = _post(messages)
        calls = message.get("tool_calls") or []
        if not calls:
            return {"answer": _text(message.get("content")), "trace": trace,
                    "wrote": any(t["tool"] in WRITE_TOOLS for t in trace)}

        messages.append({"role": "assistant", "content": message.get("content") or "",
                         "tool_calls": calls})
        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            started = time.perf_counter()
            try:
                result = DISPATCH[name](args) if name in DISPATCH else {
                    "error": "unknown_tool", "message": f"No tool named {name}"}
            except Exception as exc:
                logger.exception("Tool %s failed", name)
                result = {"error": "tool_failed", "message": str(exc),
                          "suggestion": "Tell the user the lookup failed. Do not guess."}
            duration_ms = int((time.perf_counter() - started) * 1000)
            trimmed = _trim(name, result)

            # Feeds the Change Data Feed analytics loop. Best effort by design.
            schema.log_tool_call(
                tool_name=name, arguments=args,
                outcome=(result.get("error", "ok") if isinstance(result, dict) else "ok"),
                is_write=name in WRITE_TOOLS,
                summary=_summarise(name, result),
                duration_ms=duration_ms, session_id=session_id)
            trace.append({"tool": name, "arguments": args, "result": trimmed,
                          "is_write": name in WRITE_TOOLS})
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": json.dumps(trimmed, default=str)[:6000]})

    # Ran out of turns. Answer from what we have rather than looping forever.
    messages.append({"role": "user",
                     "content": "Answer now using what the tools already returned."})
    message = _post(messages)
    return {"answer": _text(message.get("content")), "trace": trace,
            "wrote": any(t["tool"] in WRITE_TOOLS for t in trace),
            "note": "Stopped after the maximum number of tool calls."}


def _text(content) -> str:
    """Normalise a message payload to plain text.

    Reasoning models return a list of typed blocks including chain-of-thought
    rather than a string; returning that verbatim would leak the model's
    internal deliberation into the UI.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        if parts:
            return "\n".join(p for p in parts if p).strip()
    return str(content or "")
