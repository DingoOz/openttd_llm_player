# ottd-bench

A repeatable harness for testing how well vision LLMs, local or cloud, play
[OpenTTD](https://www.openttd.org). A model runs a transport company for a fixed stretch of
game time and is scored on the net worth it ends with.

Every model gets the same three channels:

| Channel | What it is | Used for |
|---|---|---|
| **Vision** | Screenshots from the game's own renderer with tile coordinates drawn on them | Terrain, roads, free land: spatial facts are *only* available as images |
| **RPC** | Tool calls executed inside the game by a GameScript, over OpenTTD's admin port | Building, buying, routing, exact numbers |
| **Text files** | A per-run workspace: `guide.md`, `notes.md`, `towns.txt`, `industries.txt`, `action_log.txt` | Reference manual and the model's persistent memory |

Vision is mandatory. A run starts with a vision check and stops if the model cannot read an image.

## Setup

```bash
scripts/setup.sh          # no sudo: pinned OpenTTD 15.3 + OpenGFX into third_party/, venv into .venv/
.venv/bin/ottd-bench doctor
```

Requirements: Linux x86-64, `curl`, `unzip`, [`uv`](https://docs.astral.sh/uv/). OpenTTD runs headless as
a dedicated server; no display, X server or GPU is needed for the game. For local models, a running
[Ollama](https://ollama.com) with a vision model pulled.

## Running

```bash
.venv/bin/ottd-bench list                                        # scenarios and models
.venv/bin/ottd-bench probe --model qwen3.5-9b                    # vision check only
.venv/bin/ottd-bench run --model qwen3.5-9b --scenario smoke     # quick end-to-end check
.venv/bin/ottd-bench run --model qwen3.5-9b --scenario bus_starter
.venv/bin/ottd-bench run --model scripted --scenario bus_starter # non-LLM reference player
.venv/bin/ottd-bench run --model claude-opus-5-5 --scenario bus_starter --repeat 3
.venv/bin/ottd-bench run --model qwen3.5-9b --scenario smoke --video   # also film it to video.mp4
.venv/bin/ottd-bench report                                      # leaderboard over all runs
```

Useful flags for `run`: `--steps`, `--days`, `--seed` override the scenario; `--no-hints` stops the
game from suggesting valid tiles when a build fails, so sites must be found from the images alone;
`--repeat N` plays the episode N times; `--video` films the episode.

### Video

`--video` writes `video.mp4` (1280x720) into the run folder. For every turn it shows the images the
model was given, then what the model said and did over the game view (green and red outlines mark
the tiles it acted on, successfully or not), then a timelapse of the game while time passes. The
model's thinking time is not in the video, because the game is paused then. Frames are rendered by
the game itself, so no display or screen capture is involved; `ffmpeg` with libx264 must be on the
PATH. Filming slows the game clock by roughly half but does not change the game: a filmed run of
the scripted player ends with exactly the same result as an unfilmed one.

Cloud models read their key from the environment variable named in `models.yaml`
(`OPENAI_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`). Claude uses the Anthropic SDK's normal
credential lookup (`ANTHROPIC_API_KEY`, or an `ant auth login` profile).

## What a run produces

```
runs/<timestamp>_<scenario>_<model>/
  summary.md         one-screen result
  metrics.json       score, counts, tokens, timings, vision probe results
  timeline.csv       cash, loan, net worth, vehicles per turn
  transcript.jsonl   everything the model was shown and everything it did
  images/            every image sent to the model
  workspace/         the text files the model could read and write
  final.sav          the finished game; open it in OpenTTD to look around
  video.mp4          only with --video
  ottd/              the server's config and log
```

**Score** is net worth at the end date: cash − loan + resale value of vehicles. It starts at 0 and a
company that does nothing ends slightly negative (loan interest). Roads and stations are worth
nothing by themselves. The other metrics describe *how* the model played: tool error rate,
malformed calls, how often it looked, tokens and time, and two counts for turns where it replied
without calling a tool: `nudges` (it was reminded once, within the turn) and `idle_turns` (it still
did nothing). Set `episode.nudge_idle_turns: false` in a scenario to drop the reminder.

## Why results are comparable

- **Pinned game.** `scripts/setup.sh` installs one OpenTTD version by checksum. A scenario's seed
  gives the same map every time on that version.
- **Thinking is free.** The game is paused whenever the model is thinking or its tools are running.
  Every turn is followed by a fixed number of game days, and `wait(days)` adds more. The pause and
  unpause happen inside the game on exact ticks, so a slow CPU model and a fast cloud model play the
  same game. Two runs of the scripted player give identical results to the pound.
- **Same end date.** If a model runs out of turns early, the clock runs to the scenario's end date
  before scoring.
- **Isolated.** Each run has its own OpenTTD instance, ports, config and home directory. Runs can
  execute in parallel.

What is *not* deterministic is the model. Use `--repeat` and compare distributions, not single runs.

## How it works

```
 model  <-- images, text -->  agent loop  --tool calls-->  admin port  -->  LLMBridge (GameScript)
                              (Python)    <--results----   (TCP)            acts as the company,
                                 |                                          pauses/unpauses time
                                 +-- rcon "screenshot" --> OpenTTD renders the map to PNG
```

- `ottd_scripts/game/LLMBridge` is a GameScript that turns JSON requests into game commands issued
  as the player company, and keeps the game paused between turns. It includes a road pathfinder,
  so `build_road` connects two tiles the way a human would drag a road.
- `ottd_scripts/ai/LLMIdle` is a do-nothing AI whose only job is to make the company exist.
- `ottd_bench/vision.py` cuts views out of whole-map screenshots and overlays tile coordinates. The
  projection mirrors OpenTTD's own, so a label is always on the tile it names.
- `ottd_bench/providers/` adapts Ollama, any OpenAI-compatible endpoint, and Anthropic to one
  append-only conversation interface. Models without function calling can use `tool_mode: json`.
- `ottd_bench/episode.py` is the loop: observe, let the model act, advance time, record.
- `ottd_bench/video.py` films an episode from whole-map screenshots taken while the clock runs.

When a conversation outgrows the model's context budget it is restarted from a fresh briefing that
includes `notes.md` and the recent action log. Nothing is ever edited in place, which keeps prompt
caches warm and is what models with conversation-bound reasoning require.

## Tests

```bash
uv pip install --python .venv/bin/python -e '.[dev]'
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest tests/        # offline: no game, no model
.venv/bin/ottd-bench run --model scripted --scenario smoke               # full pipeline, no model (2 min)
```

## Adding things

- **A model:** add an entry to `models.yaml`. For anything OpenAI-compatible that is all there is to it.
- **A scenario:** copy a file in `scenarios/`. `game` controls map generation (`seed`, `map_size`,
  `start_year`, `towns`, `industries`, `terrain`, `sea`, and any raw OpenTTD setting through
  `extra_settings`); `episode` controls turns, days per turn and length; `transport` enables
  aircraft tools when it contains `air`.
- **A tool:** add a `Cmd_<name>` handler to `ottd_scripts/game/LLMBridge/main.nut` and register it in
  `ottd_bench/tools.py`.

## Limits worth knowing

- **Game time is real time.** A stock dedicated server cannot fast-forward: one game day takes about
  2 seconds, a game year about 12 minutes, regardless of the model. Runs are parallelisable.
- **Road vehicles and aircraft only.** No trains or ships, and roads cannot bridge or tunnel.
- **Maps up to 256x256 are practical.** Views are cut from a whole-map screenshot, which grows with
  map area.
- **Claude refusals are not rerouted.** The Anthropic adapter does not enable server-side fallback
  to another model, because a benchmark must not silently swap the model under test. A refusal is
  recorded and the turn is lost.

## License

Copyright (C) 2026 Nigel H-S

This program is free software: you can redistribute it and/or modify it under the terms of the
GNU General Public License, version 3, as published by the Free Software Foundation. It is
distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied
warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See [LICENSE](LICENSE) for the
full text.

This project is not affiliated with the OpenTTD project. OpenTTD and OpenGFX are not included here:
`scripts/setup.sh` downloads them from the official OpenTTD servers, and both are licensed under the
GNU GPL v2.
