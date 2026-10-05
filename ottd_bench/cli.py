"""Command line: ottd-bench doctor | list | run | probe | report."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from .config import MODELS_FILE, OPENTTD_BIN, OPENTTD_DIR, RUNS_DIR, SCENARIOS_DIR, SCRIPTS_DIR, ModelConfig, Scenario


def cmd_doctor(args) -> int:
    ok = True

    def check(label: str, good: bool, detail: str = "", required: bool = True) -> None:
        nonlocal ok
        mark = "ok  " if good else ("FAIL" if required else "warn")
        print(f"  [{mark}] {label}" + (f" - {detail}" if detail else ""))
        if required and not good:
            ok = False

    print("OpenTTD")
    check("binary", OPENTTD_BIN.exists(), str(OPENTTD_BIN) if OPENTTD_BIN.exists() else "run scripts/setup.sh")
    if OPENTTD_BIN.exists():
        libs = subprocess.run(["ldd", str(OPENTTD_BIN)], capture_output=True, text=True).stdout
        missing = [line.split()[0] for line in libs.splitlines() if "not found" in line]
        check("shared libraries", not missing, ", ".join(missing))
        gfx = list((OPENTTD_DIR / "baseset").glob("opengfx*"))
        check("OpenGFX graphics", bool(gfx), gfx[0].name if gfx else "run scripts/setup.sh")
    check("game script + placeholder AI", (SCRIPTS_DIR / "game/LLMBridge/main.nut").exists() and (SCRIPTS_DIR / "ai/LLMIdle/main.nut").exists())

    print("Models")
    check("models.yaml", MODELS_FILE.exists())
    try:
        import httpx
        version = httpx.get("http://localhost:11434/api/version", timeout=3).json().get("version")
        check("Ollama server", True, f"version {version}", required=False)
        installed = {m["name"] for m in httpx.get("http://localhost:11434/api/tags", timeout=5).json().get("models", [])}
        for key in ModelConfig.all_keys():
            cfg = ModelConfig.load(key)
            if cfg.provider != "ollama":
                continue
            if cfg.model not in installed:
                check(f"{key}", False, f"'{cfg.model}' is not pulled (ollama pull {cfg.model})", required=False)
                continue
            caps = httpx.post("http://localhost:11434/api/show", json={"model": cfg.model}, timeout=10).json().get("capabilities", [])
            check(f"{key}", "vision" in caps, f"capabilities: {', '.join(caps)}", required=False)
    except Exception as e:
        check("Ollama server", False, f"not reachable ({type(e).__name__}); local models unavailable", required=False)

    print("Scenarios")
    for path in sorted(SCENARIOS_DIR.glob("*.yaml")):
        try:
            Scenario.load(str(path))
            check(path.stem, True)
        except Exception as e:
            check(path.stem, False, str(e))
    print("ready" if ok else "not ready: fix the FAIL lines above")
    return 0 if ok else 1


def cmd_list(args) -> int:
    print("Scenarios:")
    for path in sorted(SCENARIOS_DIR.glob("*.yaml")):
        sc = Scenario.load(str(path))
        print(f"  {sc.name:<14} {sc.game.map_size}x{sc.game.map_size}, {sc.episode.max_days} days, {sc.episode.max_steps} turns - {sc.description}")
    print("Models:")
    for key in ModelConfig.all_keys():
        cfg = ModelConfig.load(key)
        print(f"  {key:<26} {cfg.provider:<10} {cfg.model}")
    return 0


def _run_dir(scenario: Scenario, model: ModelConfig, name: str | None) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in model.key)
    return RUNS_DIR / (name or f"{stamp}_{scenario.name}_{safe}")


def cmd_run(args) -> int:
    from .episode import Episode

    scenario = Scenario.load(args.scenario)
    model = ModelConfig.load(args.model)
    if args.steps is not None:
        scenario.episode.max_steps = args.steps
    if args.days is not None:
        scenario.episode.max_days = args.days
    if args.seed is not None:
        scenario.game.seed = args.seed
    if args.no_hints:
        scenario.episode.spatial_hints = False

    exit_code = 0
    for i in range(args.repeat):
        run_dir = _run_dir(scenario, model, args.name if args.repeat == 1 else None)
        if run_dir.exists():
            print(f"{run_dir} already exists", file=sys.stderr)
            return 2
        print(f"run {i + 1}/{args.repeat}: {model.key} on {scenario.name} -> {run_dir}")
        episode = Episode(scenario, model, run_dir, skip_probe=args.skip_probe or model.provider == "scripted",
                          record=args.video)
        metrics = episode.run()
        print((run_dir / "summary.md").read_text())
        if metrics["outcome"] == "aborted":
            exit_code = 1
        if args.repeat > 1:
            time.sleep(1)   # distinct timestamps for run directories
    return exit_code


def cmd_probe(args) -> int:
    """Run only the vision gate against a model."""
    from .episode import Episode
    from .providers import ProviderError

    model = ModelConfig.load(args.model)
    scenario = Scenario(name="probe")
    run_dir = RUNS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}_probe_{model.key}"
    episode = Episode(scenario, model, run_dir)
    for d in (run_dir, episode.images_dir):
        d.mkdir(parents=True, exist_ok=True)
    episode._transcript = open(run_dir / "transcript.jsonl", "w")
    try:
        episode._probe_number()
    except ProviderError as e:
        print(f"FAILED: {e}")
        return 1
    finally:
        episode._transcript.close()
    c = episode.counters
    print(f"passed: {c['input_tokens']} tokens in, {c['output_tokens']} out, {c['llm_seconds']:.1f}s")
    return 0


def cmd_report(args) -> int:
    from .report import load_runs, print_table, write_leaderboard

    rows = load_runs(scenario=args.scenario)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    print_table(rows)
    if rows:
        md, csv_path = write_leaderboard(rows)
        print(f"\nwritten: {md} and {csv_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ottd-bench", description="Benchmark vision LLMs playing OpenTTD.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check the installation").set_defaults(fn=cmd_doctor)
    sub.add_parser("list", help="list scenarios and models").set_defaults(fn=cmd_list)

    run = sub.add_parser("run", help="play one episode")
    run.add_argument("--model", required=True, help="key from models.yaml")
    run.add_argument("--scenario", default="bus_starter", help="name in scenarios/ or path to a YAML file")
    run.add_argument("--steps", type=int, help="override the scenario's turn limit")
    run.add_argument("--days", type=int, help="override the scenario's length in game days")
    run.add_argument("--seed", type=int, help="override the scenario's map seed")
    run.add_argument("--repeat", type=int, default=1, help="run the episode this many times")
    run.add_argument("--name", help="run directory name (default: timestamp_scenario_model)")
    run.add_argument("--no-hints", action="store_true", help="do not suggest valid tiles when a build fails")
    run.add_argument("--video", action="store_true", help="film the episode to video.mp4 (needs ffmpeg; the game runs slower)")
    run.add_argument("--skip-probe", action="store_true", help="skip the vision check (debugging only)")
    run.set_defaults(fn=cmd_run)

    probe = sub.add_parser("probe", help="vision check only")
    probe.add_argument("--model", required=True)
    probe.set_defaults(fn=cmd_probe)

    report = sub.add_parser("report", help="leaderboard over finished runs")
    report.add_argument("--scenario")
    report.add_argument("--json", action="store_true")
    report.set_defaults(fn=cmd_report)

    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except (FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
