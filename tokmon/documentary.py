"""Attenborough Mode: turn analytics facts into a nature-documentary narration.

Fully local. Uses Ollama when reachable, else a built-in template engine.
"""
from __future__ import annotations

import json
import os
import random
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from . import analytics as A

OLLAMA_URL = os.environ.get("TOKMON_OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("TOKMON_OLLAMA_MODEL")
DOC_ENGINE = os.environ.get("TOKMON_DOC_ENGINE", "auto")
DISPLAY_TZ = "America/Los_Angeles"


def ollama_status(url: str | None = None) -> dict:
    """Probe the local Ollama server. Never raises."""
    base = (url or OLLAMA_URL).rstrip("/")
    try:
        with urllib.request.urlopen(base + "/api/tags", timeout=1.5) as r:
            data = json.loads(r.read().decode("utf-8"))
        models = [m["name"] for m in data.get("models", [])]
    except Exception:
        return {"available": False, "url": base, "models": [], "model": None}
    model = OLLAMA_MODEL if OLLAMA_MODEL in models else (models[0] if models else None)
    return {"available": bool(models), "url": base, "models": models, "model": model}


@dataclass
class DocBrief:
    since: str
    host: str | None
    turns: int
    sessions: int
    projects: int
    total_usd: float
    dominant_model: str | None
    dominant_model_usd: float
    busiest_project: str | None
    busiest_project_usd: float
    busiest_project_turns: int
    biggest_turn_model: str | None
    biggest_turn_project: str | None
    biggest_turn_usd: float
    biggest_turn_hour: int | None
    top_tool: str | None
    top_tool_calls: int
    cache_saved_usd: float
    cache_savings_pct: float
    burn_per_hour_usd: float
    projected_eom_usd: float
    month_to_date_usd: float

    @property
    def empty(self) -> bool:
        return self.turns == 0


def build_brief(conn, since: str = "all", host: str | None = None,
                tz: str = DISPLAY_TZ) -> DocBrief:
    s = A.summary(conn, since=since, host=host)
    models = A.spend_by(conn, "model", since=since, host=host, limit=1)
    projects = A.spend_by(conn, "project", since=since, host=host, limit=1)
    tools = A.spend_by(conn, "tool", since=since, host=host, limit=1)
    biggest = A.top_turns(conn, metric="cost", n=1, since=since, host=host)
    cache = A.cache_savings(conn, since=since, host=host)
    burn = A.burn_rate(conn, window_minutes=60, host=host)
    forecast = A.monthly_forecast(conn, host=host)

    proj = projects[0] if projects else None
    tool = tools[0] if tools else None
    bt = biggest[0] if biggest else None
    bt_hour = None
    if bt is not None and isinstance(bt[1], datetime):
        local = bt[1].replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(tz))
        bt_hour = local.hour

    return DocBrief(
        since=since, host=host,
        turns=int(s["turns"]), sessions=int(s["sessions"]),
        projects=int(s["projects"]), total_usd=float(s["total_usd"]),
        dominant_model=(models[0][0] if models else None),
        dominant_model_usd=(float(models[0][3]) if models else 0.0),
        busiest_project=(proj[0] if proj else None),
        busiest_project_usd=(float(proj[4]) if proj else 0.0),
        busiest_project_turns=(int(proj[2]) if proj else 0),
        biggest_turn_model=(bt[4] if bt else None),
        biggest_turn_project=(bt[2] if bt else None),
        biggest_turn_usd=(float(bt[9]) if bt else 0.0),
        biggest_turn_hour=bt_hour,
        top_tool=(tool[0] if tool else None),
        top_tool_calls=(int(tool[1]) if tool else 0),
        cache_saved_usd=float(cache["counterfactual_extra_usd"]),
        cache_savings_pct=float(cache["savings_pct"]),
        burn_per_hour_usd=float(burn["rate_per_hour_usd"]),
        projected_eom_usd=float(forecast["projected_eom_usd"]),
        month_to_date_usd=float(forecast["month_to_date_usd"]),
    )


def _usd(x: float) -> str:
    return f"${x:,.2f}"


def render_template(brief: DocBrief, seed: int = 0) -> str:
    rnd = random.Random(seed)
    model = brief.dominant_model or "an unidentified model"
    project = brief.busiest_project or "an unnamed project"
    beats: list[str] = []

    beats.append(rnd.choice([
        f"Here, in the pale glow of the terminal, we find the developer in its "
        f"natural habitat. Across this period it produced {brief.turns} turns "
        f"over {brief.sessions} sessions and {brief.projects} projects.",
        f"Observe the developer at work. In this window it has generated "
        f"{brief.turns} turns, spread across {brief.sessions} sessions and "
        f"{brief.projects} distinct projects.",
    ]))

    beats.append(rnd.choice([
        f"Its companion of choice is {model}, summoned again and again. The "
        f"project {project} consumed the most of its attention, at "
        f"{_usd(brief.busiest_project_usd)}.",
        f"The developer favors {model} above all others. Most of its energy "
        f"flows into {project}, which alone accounts for "
        f"{_usd(brief.busiest_project_usd)}.",
    ]))

    if brief.biggest_turn_model:
        hour = brief.biggest_turn_hour
        when = ""
        if hour is not None:
            phase = "in the small hours" if hour < 6 else (
                "under cover of night" if hour >= 22 else f"at the {hour}:00 hour")
            when = f", {phase}"
        beats.append(
            f"Then comes the feeding frenzy. A single turn on "
            f"{brief.biggest_turn_model}, in {brief.biggest_turn_project or 'the wild'}"
            f"{when}, cost {_usd(brief.biggest_turn_usd)}. A remarkable display of appetite."
        )

    beats.append(
        f"The reckoning. Over this window the developer spent "
        f"{_usd(brief.total_usd)}. Caching spared it a further "
        f"{_usd(brief.cache_saved_usd)}, some {brief.cache_savings_pct:.0f} percent "
        f"of what it might have paid. At the current pace, the month will close "
        f"near {_usd(brief.projected_eom_usd)}."
    )

    beats.append(rnd.choice([
        "And so the cycle continues, as it does every day, in terminals the world over.",
        "The sun sets on the workspace. Tomorrow, the developer will hunt again.",
    ]))

    return "\n\n".join(beats).replace("—", ", ")
