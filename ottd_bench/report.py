"""Aggregate finished runs into a leaderboard."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from .config import RUNS_DIR

def _money(v: int | None) -> str:
    if v is None:
        return "n/a"
    return f"-£{-v:,}" if v < 0 else f"£{v:,}"


COLUMNS = ["scenario", "model", "harness_version", "outcome", "score_net_worth", "vehicles", "vehicles_in_profit", "stations",
           "game_days", "turns", "tool_calls", "tool_error_rate", "look_calls", "view_map_calls",
           "input_tokens", "output_tokens", "llm_seconds", "wall_seconds", "run"]


def load_runs(runs_dir: Path = RUNS_DIR, scenario: str | None = None) -> list[dict]:
    rows = []
    for path in sorted(runs_dir.glob("*/metrics.json")):
        m = json.loads(path.read_text())
        if scenario and m.get("scenario") != scenario:
            continue
        if "harness_version" not in m:   # runs made before the version was copied into metrics.json
            run_json = path.with_name("run.json")
            m["harness_version"] = json.loads(run_json.read_text()).get("harness_version", "?") if run_json.exists() else "?"
        rows.append(m)
    # Best score first within each scenario; runs without a score (aborted, bankrupt) last.
    rows.sort(key=lambda m: (m["scenario"], m["score_net_worth"] is None, -(m["score_net_worth"] or 0)))
    return rows


def write_leaderboard(rows: list[dict], runs_dir: Path = RUNS_DIR) -> tuple[Path, Path]:
    csv_path = runs_dir / "leaderboard.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    md = ["# Leaderboard", "",
          "Score is net worth at the end of the episode: cash - loan + resale value of vehicles. It starts at 0, and a company that does nothing ends just below zero.",
          "", "Only compare rows with the same harness version: prompts, tools and game rules change between versions.", ""]
    for scenario in sorted({m["scenario"] for m in rows}):
        md += [f"## {scenario}", "",
               "| model | harness | outcome | score | vehicles (in profit) | turns | tool errors | looks | tokens in/out | model time | run |",
               "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|"]
        for m in (r for r in rows if r["scenario"] == scenario):
            score = _money(m["score_net_worth"])
            err = "n/a" if m["tool_error_rate"] is None else f"{m['tool_error_rate']:.0%}"
            md.append(f"| {m['model']} | {m['harness_version']} | {m['outcome']} | {score} | {m['vehicles']} ({m['vehicles_in_profit']}) | {m['turns']} | {err} | "
                      f"{m['look_calls'] + m['view_map_calls']} | {m['input_tokens']:,}/{m['output_tokens']:,} | {m['llm_seconds']:.0f}s | {m['run']} |")
        md.append("")
    md_path = runs_dir / "leaderboard.md"
    md_path.write_text("\n".join(md))
    return md_path, csv_path


def print_table(rows: list[dict]) -> None:
    if not rows:
        print("no finished runs yet")
        return
    print(f"{'scenario':<14}{'model':<20}{'ver':<7}{'outcome':<12}{'score':>10}{'veh':>5}{'turns':>7}{'err%':>6}{'looks':>7}  run")
    for m in rows:
        score = _money(m["score_net_worth"])
        err = "-" if m["tool_error_rate"] is None else f"{m['tool_error_rate'] * 100:.0f}"
        print(f"{m['scenario']:<14}{m['model']:<20}{m['harness_version']:<7}{m['outcome']:<12}{score:>10}{m['vehicles']:>5}{m['turns']:>7}{err:>6}"
              f"{m['look_calls'] + m['view_map_calls']:>7}  {m['run']}")
