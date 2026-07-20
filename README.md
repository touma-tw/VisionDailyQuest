# VisionDailyQuest

An **agentic vision-LLM pilot** for automating a game's daily chores. You don't script steps —
you give a local vision-language model (via [Ollama](https://ollama.com)) a high-level goal and a
small set of tools. It looks at the screen, thinks, calls a tool (tap / swipe / read / wait),
observes the result, and self-corrects — in a continuous conversation — until the goal is done
or it gives up.

This repo is a **template**: the agent core (`agent/pilot.py`) is game-agnostic. The included
`tasks/daily_goals.yaml` and `config.yaml` are a fully worked example for one specific game
(a Windows client titled 鈴蘭之劍 / "Lily Sword") — copy the pattern, swap in your own game's
window title, knowledge, and task list.

## Why agentic instead of a scripted bot

A traditional macro/bot hardcodes every click. That breaks the moment a popup, A/B-tested layout,
or unexpected dialog shows up — exactly the kind of thing daily-chore UIs are full of. Here the
model is only given:

- `game_knowledge` — how the game's screens work and what the traps are (shared across all tasks)
- per-task `goal` — what to accomplish, where to start, how to tell it's done

...and it plans the actual steps itself, using the tools below. When a tap has no visible effect,
the tool says so honestly instead of pretending it worked — that's what lets the model notice a
mistake and try something else instead of hammering the same wrong spot.

## Requirements

- A Windows game client running in windowed mode
- Local [Ollama](https://ollama.com) with a vision-capable model (developed against `qwen3.6:35b`;
  verify your model supports image grounding — many "small" local models don't)
- Python venv: `pip install -r requirements.txt` (requests, pillow, pyautogui, opencv, numpy, pyyaml, mss, pygetwindow)

## Setup for your own game

1. Copy `config.yaml`: set `window_title` to your game's window title, point `ollama.model` at
   a vision-capable local model you've pulled.
2. Rewrite `tasks/daily_goals.yaml`: describe your game's `game_knowledge` (menus, traps, ambiguous
   buttons) and list your daily `tasks` as high-level goals — not click-by-click steps.
3. If the game runs elevated (common for anti-cheat), you must launch elevated too, or Windows UIPI
   will silently swallow your synthetic mouse events (symptom: the model's coordinates look correct
   but nothing happens in-game). `run_admin.bat` handles the UAC prompt for you.

## Usage

```bat
run_admin.bat            :: run the full daily list (tasks already done today are skipped)
run_admin.bat <任務名>   :: run a single named task (always runs, even if already done today)
```

Already in an elevated shell? Run directly: `python -m agent.pilot [task_name]`.

Progress is written to `runs/progress-<date>.json` — durable across multiple runs, crashes, and
restarts within the same day. Full console output (including the model's reasoning) is logged to
`runs/log-<date>.txt`.

## Tools available to the model

| Tool | Purpose |
|---|---|
| `tap(target)` | Click something. Describe it in natural language (text if there's text, visual description if it's an icon) — the framework locates it for you |
| `swipe(direction)` | Scroll. Give the direction **you want to see**, not which way to drag — Python handles the conversion |
| `read(question)` | Read one specific value/state off the screen (more accurate than reading it inline while deciding) |
| `wait(seconds)` | Wait out a battle/loading animation |
| `note(text)` | Record a progress note; every subsequent step echoes all notes back — external working memory for multi-part tasks |
| `finish(summary)` / `give_up(reason)` | Declare success / declare it can't be done |

`exchange()` in the bundled example is a game-specific tool (calibrated-coordinate replay for a
shop UI where a template-match can't reliably tell "safe to click" apart from "irreversible rare
purchase") — most games won't need an equivalent, but it's a useful pattern to crib from if yours
does.

## UI template cache (skip the VLM on repeat taps)

The first tap on a given target calls the vision model to locate it; once it lands, the framework
crops a small template around that button into `ui_cache/` (gitignored — it's screenshots of your
game). Future taps at the same target try `cv2.matchTemplate` against the current screen first
(color-aware, scale-normalized) and only fall back to the VLM on a low-confidence match (UI
changed, wrong screen, etc). Since most game UI is static, this cuts out the ~2s vision call on
most steps. Toggle: `pilot.ui_cache` in `config.yaml`.

## Design notes

**Decision and localization are two separate model calls — this is the most important
architectural choice here.** The model never outputs coordinates; it only names *what* to tap
(`target`), and a dedicated `locate()` call is what turns that into a bbox. Measured on the same
model, same screenshot: combined into one call → aiming for one button, landing 265px off on a
different one. Split into two calls → 7.2px error. The model isn't bad at grounding — asking it to
reason *and* compute coordinates in the same breath crowds out the coordinate math. Scrolling
follows the same principle: the model only names a direction, Python does the drag-vector math
(it gets left/right backwards even with an explicit hardcoded rule if asked to compute it itself).

**Screenshots are client-area only** (no title bar/borders) — an early version included the title
bar and the model mistook the OS window's ✕ for an in-game close button and closed the game.
Coordinates are always relative (0–1000), re-read from the window rect every step, so window
moves, monitor changes, and restarts (new PID/hwnd) don't require recalibration.

**Thinking-mode arithmetic pitfall**: asking the model to compute something time-dependent
("how many more times before midnight") inline, every step, is a reliable way to make it spiral —
observed 166s / 40k+ tokens of thinking on a single step when the task description had even a
minor internal contradiction. Fix that goes two ways: (1) precompute anything involving real time
in Python and inject it as a fact, never ask the model to re-derive it; (2) if a decision has to
recur across several tool calls, phrase it as a single-step check with a worked numeric example
("read P, if P+R>240 do X, else finish") rather than "figure out the total count up front" —
a per-iteration test converges, a global optimization the model has to re-justify each turn does not.

**A task must never be shown its own prior failure/success as background context.** The daily
runner surfaces "here's what other tasks did today" so later tasks know earlier ones already ran —
but if a task sees *its own* previous "✗ ran out of steps" entry, it will reason "I already failed
this, there's nothing left to do" and immediately give up, even with resources still available to
finish the job. Exclude a task's own name when building the background summary handed to it.

**Automatic stall recovery**: a single Ollama call can hang far longer than any single step should
take (context growth, or a model that starts spiraling on an ambiguous instruction). `pilot.py`
enforces a configurable per-call timeout (`pilot.request_timeout`); on timeout it unloads the model
(`keep_alive: 0`, clearing KV cache and freeing VRAM) and retries with a longer cold-load timeout.
`pilot.num_predict` also caps how many tokens a single think+respond turn may emit, so a spiral
can't run unbounded even before the timeout fires.

## Why it can't run in the background (tested, don't retry this)

Two walls, both by the game's own design — no injection method gets around either:

1. The game ignores input entirely while not focused (same coordinates, same send method:
   foreground screen delta 91, background delta 0.00).
2. The game reads the *real* cursor position (`GetCursorPos`); coordinates embedded in a
   `PostMessage` are ignored.

So a click requires stealing focus and moving the real cursor — there's no way around it. But
**screenshots don't need focus**: `capture.py` uses `PrintWindow` to ask the window to render
itself off-screen; after cropping the title bar this matches a real desktop capture with 0.00
difference, and works even when another window covers it. So a long battle's dozens of `wait`
calls never touch your mouse or focus — only the instant of an actual click steals it (and the
cursor is restored afterward).

**Manual kill switch**: flinging the mouse to a screen corner (pyautogui's failsafe) cleanly
aborts the current run.

## Debug tools

- `runs/log-<date>.txt` / `runs/progress-<date>.json` — full output and progress per run.
- `tools/ground_test.py` — re-test the model's grounding accuracy, dumps an annotated image
  (`--live` tests the current screen, `--model` swaps models).
- `tools/click_diag.py` — diagnoses whether clicks are actually reaching the game, checks
  elevation on both sides.
- `tools/bg_probe.py` / `bg_probe2.py` — background-input feasibility probes (conclusion: not
  feasible, see above).

## Switching to a cloud model

`vision.py` exposes `OllamaProvider` (default) and `GeminiProvider` (cloud fallback, stronger
grounding) behind the same interface. To use Gemini: set the `GEMINI_API_KEY` environment variable
(from Google AI Studio), `pip install google-genai`, and swap the provider construction in
`pilot.py` to `GeminiProvider(cfg["gemini"])`.
