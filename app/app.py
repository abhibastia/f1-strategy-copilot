"""
AI Formula 1 Race Companion — frontend.

A Databricks App that presents the project's central finding and lets a user
explore the corpus the agent reasons over: race results from the Spark pipeline,
measured race-day weather, and the narrative of race reports.

READ-ONLY BY DESIGN
-------------------
Nothing here writes. Every write in this project goes through the agent's MCP
tools, which is what makes the "agent takes real actions" requirement
demonstrable: the watchlist, predictions and notes on this page were all created
by the agent, not by a form on this page.

Serves entirely from Lakebase, so rendering a page costs no Databricks compute.

Run locally:
    python app.py
"""

import logging
import os

from flask import Flask, jsonify, render_template, request

import ui_data

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("f1-companion-ui")

app = Flask(__name__)

MCP_URL = os.environ.get(
    "MCP_SERVER_URL",
    "https://mcp-f1-race-companion-7474646797973312.aws.databricksapps.com",
)


@app.route("/healthz")
def healthz():
    """Liveness probe. Deliberately does not touch Lakebase, so a database
    problem cannot make the platform conclude the container is dead."""
    return jsonify({"status": "ok", "app": "f1-race-companion-ui"})


@app.route("/")
def index():
    season = request.args.get("season", type=int)
    available = ui_data.seasons()
    if season not in available:
        season = available[0] if available else None
    try:
        return render_template(
            "index.html",
            stats=ui_data.corpus_stats(),
            thresholds=ui_data.rain_vs_chaos(),
            thesis=ui_data.thesis_races(),
            seasons=available,
            season=season,
            races=ui_data.season_races(season) if season else [],
            standings=ui_data.standings(season) if season else [],
            activity=ui_data.agent_activity(),
            analytics=ui_data.agent_analytics(),
            strategy=ui_data.strategy_races(season) if season else [],
            mcp_url=MCP_URL,
            error=None,
        )
    except Exception as exc:
        # Render an explanation rather than a 500. The most likely cause is a
        # missing secret ACL on this app's service principal, and a stack trace
        # in the browser would not say so.
        logger.exception("Could not render the dashboard")
        return render_template(
            "index.html", stats={}, thresholds=[], thesis=[], seasons=[],
            season=None, races=[], standings=[], activity={},
            analytics={"tools": [], "totals": {}}, strategy=[],
            mcp_url=MCP_URL, error=str(exc),
        )


@app.route("/api/search")
def api_search():
    """Semantic search over race reports. Backs the search box."""
    query = (request.args.get("q") or "").strip()
    if not query:
        return jsonify({"error": "Enter something to search for."}), 400
    season = request.args.get("season", type=int)
    try:
        results = ui_data.search(query, top_k=request.args.get("k", 6, type=int),
                                 season=season)
        return jsonify({"query": query, "count": len(results), "results": results})
    except Exception as exc:
        logger.exception("Search failed")
        return jsonify({"error": str(exc)}), 503


if __name__ == "__main__":
    port = int(os.getenv("DATABRICKS_APP_PORT", os.getenv("PORT", 8001)))
    # debug defaults off: app.yaml runs this same entrypoint in the deployed
    # app, and Flask's debug mode exposes the Werkzeug console to anyone who can
    # trigger a 500.
    debug = os.getenv("FLASK_DEBUG", "").lower() in {"1", "true", "yes"}
    app.run(debug=debug, host="0.0.0.0", port=port)
