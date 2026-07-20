# VisionDailyQuest

**[English](#english)** | **[中文](#中文)**

---

<a id="english"></a>
## English

An **agentic vision-LLM pilot** for automating a game's daily chores. You don't script steps —
you give a local vision-language model (via [Ollama](https://ollama.com)) a high-level goal and a
small set of tools. It looks at the screen, thinks, calls a tool (tap / swipe / read / wait),
observes the result, and self-corrects — in a continuous conversation — until the goal is done
or it gives up.

This repo is a **template**: the agent core (`agent/pilot.py`) is game-agnostic. The included
`tasks/daily_goals.yaml` and `config.yaml` are a fully worked example for one specific game
(a Windows client titled 鈴蘭之劍 / "Lily Sword") — copy the pattern, swap in your own game's
window title, knowledge, and task list.

### Why agentic instead of a scripted bot

A traditional macro/bot hardcodes every click. That breaks the moment a popup, A/B-tested layout,
or unexpected dialog shows up — exactly the kind of thing daily-chore UIs are full of. Here the
model is only given:

- `game_knowledge` — how the game's screens work and what the traps are (shared across all tasks)
- per-task `goal` — what to accomplish, where to start, how to tell it's done

...and it plans the actual steps itself, using the tools below. When a tap has no visible effect,
the tool says so honestly instead of pretending it worked — that's what lets the model notice a
mistake and try something else instead of hammering the same wrong spot.

### Requirements

- A Windows game client running in windowed mode
- Local [Ollama](https://ollama.com) with a vision-capable model (developed against `qwen3.6:35b`;
  verify your model supports image grounding — many "small" local models don't)
- Python venv: `pip install -r requirements.txt` (requests, pillow, pyautogui, opencv, numpy, pyyaml, mss, pygetwindow)

### Setup for your own game

The easiest way to get started: clone this repo locally, then have an LLM read through it once
and tell it which game you want to target and what daily tasks you do — it will adapt the config
and task list for you automatically.

Depending on your GPU's VRAM, you'll also need to pick which local model to run. Ask your LLM to
recommend a vision-capable model that fits your hardware — one that also supports thinking mode
is even better — then run a basic smoke test to verify it works before relying on it.

This tool is designed to be **self-learning**: on the very first run, every single step requires
actual vision grounding, so it will feel slow. But after each successful action, the local model
builds a screen-template cache for you, so the same target doesn't need to be re-recognized next
time. As you keep using it, the cache hit rate climbs and execution speed improves noticeably.

For screens where conditions are too strict or ambiguous for the local model to reliably handle,
have your LLM wire up a coordinate-macro set on your behalf. This repo includes one example of
that pattern, `exchange()` (also mentioned again under "Tools available to the model" below) —
running it prompts you to manually click through the tricky steps in-game once, and it saves
those clicks as a coordinate set the local model can replay later. In practice, the local model
handles tapping and visual judgment correctly in most cases, so you may not end up needing this
escape hatch at all.

Once you're set up:

1. Copy `config.yaml`: set `window_title` to your game's window title, point `ollama.model` at
   a vision-capable local model you've pulled.
2. Rewrite `tasks/daily_goals.yaml`: describe your game's `game_knowledge` (menus, traps, ambiguous
   buttons) and list your daily `tasks` as high-level goals — not click-by-click steps.
3. If the game runs elevated (common for anti-cheat), you must launch elevated too, or Windows UIPI
   will silently swallow your synthetic mouse events (symptom: the model's coordinates look correct
   but nothing happens in-game). `run_admin.bat` handles the UAC prompt for you.

### Usage

```bat
run_admin.bat            :: run the full daily list (tasks already done today are skipped)
run_admin.bat <task_name> :: run a single named task (always runs, even if already done today)
```

Already in an elevated shell? Run directly: `python -m agent.pilot [task_name]`.

Progress is written to `runs/progress-<date>.json` — durable across multiple runs, crashes, and
restarts within the same day. Full console output (including the model's reasoning) is logged to
`runs/log-<date>.txt`.

### Tools available to the model

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

### UI template cache (skip the VLM on repeat taps)

The first tap on a given target calls the vision model to locate it; once it lands, the framework
crops a small template around that button into `ui_cache/` (gitignored — it's screenshots of your
game). Future taps at the same target try `cv2.matchTemplate` against the current screen first
(color-aware, scale-normalized) and only fall back to the VLM on a low-confidence match (UI
changed, wrong screen, etc). Since most game UI is static, this cuts out the ~2s vision call on
most steps. Toggle: `pilot.ui_cache` in `config.yaml`.

### Design notes

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

### Why it can't run in the background (tested, don't retry this)

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

### Debug tools

- `runs/log-<date>.txt` / `runs/progress-<date>.json` — full output and progress per run.
- `tools/ground_test.py` — re-test the model's grounding accuracy, dumps an annotated image
  (`--live` tests the current screen, `--model` swaps models).
- `tools/click_diag.py` — diagnoses whether clicks are actually reaching the game, checks
  elevation on both sides.
- `tools/bg_probe.py` / `bg_probe2.py` — background-input feasibility probes (conclusion: not
  feasible, see above).

### Switching to a cloud model

`vision.py` exposes `OllamaProvider` (default) and `GeminiProvider` (cloud fallback, stronger
grounding) behind the same interface. To use Gemini: set the `GEMINI_API_KEY` environment variable
(from Google AI Studio), `pip install google-genai`, and swap the provider construction in
`pilot.py` to `GeminiProvider(cfg["gemini"])`.

---

<a id="中文"></a>
## 中文

一個 **agentic 視覺 LLM 代理**,用來自動化遊戲的每日雜務。你不用寫死步驟——
給本地視覺語言模型(透過 [Ollama](https://ollama.com))一個高層目標和一組小工具就好。
它會看畫面、思考、呼叫工具(點擊/捲動/讀值/等待)、觀察結果、自我糾錯——在一段連續對話裡——
直到達成目標或放棄。

這個 repo 是一個**模板**:代理核心(`agent/pilot.py`)是遊戲無關的。內附的
`tasks/daily_goals.yaml` 和 `config.yaml` 是針對一款特定遊戲(Windows 客戶端,標題「鈴蘭之劍」)
的完整範例——照著這個模式,換成你自己遊戲的視窗標題、知識和任務清單即可。

### 為什麼用 agentic 而不是寫死巨集

傳統巨集/機器人把每一下點擊都寫死。只要跳出一個彈窗、UI 做了 A/B 測試改版、或出現意料外的
對話框,就會壞掉——而每日雜務類 UI 恰好最常出現這些狀況。這裡的模型只拿到:

- `game_knowledge`——這個遊戲的畫面怎麼運作、有哪些陷阱(所有任務共用)
- 每個任務的 `goal`——要達成什麼、從哪裡開始、怎麼判斷做完了

……實際怎麼做,由它自己用下面的工具規劃。當一次點擊沒有任何效果時,工具會誠實回報,
而不是假裝成功——這正是讓模型能發現自己做錯、換個做法,而不是對著同一個錯的地方猛點的關鍵。

### 需求

- 一款以視窗模式執行的 Windows 遊戲客戶端
- 本地 [Ollama](https://ollama.com) + 有視覺能力的模型(開發時用 `qwen3.6:35b` 驗證過;
  請先確認你的模型支援影像定位——很多「小型」本地模型其實不支援)
- Python venv:`pip install -r requirements.txt`(requests、pillow、pyautogui、opencv、numpy、pyyaml、mss、pygetwindow)

### 幫你的遊戲設定

最簡單的方式就是將本 repo git clone 到你的本地端,然後請大語言模型看一次,接著告訴它
你要改成在你的哪個遊戲中使用,以及你每天會做哪些任務,AI 就會自動幫你調整好。

根據你的顯存大小,還需要選擇使用哪個本地模型,可以請 AI 推薦適合你硬體、且具備 vision 能力的
模型,有 thinking 能力的更好,接下來做完基礎測試與驗證再正式依賴它。

本工具設計是**自我學習型**的:第一次執行時,由於每個步驟都需要做視覺辨識,你會覺得速度很慢,
但每次正確的動作之後,本地 AI 會幫你建立遊戲畫面快取,這樣下次就不用再重新辨識。隨著使用次數
增加,快取命中率會越來越高,執行速度也會顯著提高。

在某些條件嚴苛、本地 AI 無法處理好的地方,可以請你的大語言模型幫你調用座標 macro 集。
本工具中有一個名為 `exchange()` 的範例(下面「模型可用的工具」也會再次提到)——執行後,
它會請你依提示在遊戲中親手點好那些複雜的步驟,並存成座標集,之後就能讓本地 AI 呼叫重播。
實測中大部分情況下本地 AI 都能自己處理好點擊與視覺判斷,所以上述工具不一定會用到。

準備好之後:

1. 複製 `config.yaml`:把 `window_title` 改成你遊戲的視窗標題,`ollama.model` 指到你已經
   pull 下來、有視覺能力的本地模型。
2. 改寫 `tasks/daily_goals.yaml`:描述你遊戲的 `game_knowledge`(選單、陷阱、容易混淆的按鈕),
   並把每日任務列成高層目標——不要寫「點第幾個」這種死步驟。
3. 若遊戲以系統管理員權限執行(反外掛遊戲很常見),腳本也必須提權,否則 Windows UIPI 會
   靜默擋掉合成滑鼠事件(症狀:模型算出來的座標看起來是對的,但遊戲毫無反應)。
   `run_admin.bat` 會幫你處理 UAC 提示。

### 使用方式

```bat
run_admin.bat            :: 跑整套每日任務(今天已完成的自動跳過)
run_admin.bat <任務名>   :: 只跑單一任務(照跑,不因今天做過而跳過)
```

已經在提權終端機裡?直接執行:`python -m agent.pilot [任務名]`。

進度會寫進 `runs/progress-<日期>.json`——同一天內跨多次執行、當機重開都記得做過什麼。
完整 console 輸出(含模型的推理過程)記錄在 `runs/log-<日期>.txt`。

### 模型可用的工具

| 工具 | 用途 |
|---|---|
| `tap(target)` | 點擊某個東西。用自然語言描述(有文字用文字、純圖示描述外觀)——框架內部自己定位 |
| `swipe(direction)` | 捲動。給「你想看的方向」,不是要往哪拖——實際拖曳向量由 Python 換算 |
| `read(question)` | 專注讀畫面上某個數值/狀態(比在決策時順便讀更準) |
| `wait(seconds)` | 等待戰鬥/載入動畫 |
| `note(text)` | 記一條進度筆記;之後每一步都會回顯所有筆記——多子項任務用的外部工作記憶 |
| `finish(summary)` / `give_up(reason)` | 宣告完成 / 宣告做不到 |

隨附範例裡的 `exchange()` 是遊戲專屬的工具(針對一個商店 UI 做校準座標重播——那裡模板比對
分不出「可以安全點」和「不可逆的稀有兌換」)。大多數遊戲不需要類似的東西,但如果你的遊戲
剛好有這種模式,可以照著抄。

### UI 範本快取(讓重複的點擊免呼叫 VLM)

第一次點某個目標時,會呼叫視覺模型定位;成功之後,框架會把該按鈕周圍剪一小塊模板存進
`ui_cache/`(已被 `.gitignore` 排除——那是你遊戲畫面的截圖)。之後同一個目標的點擊,
會先用 `cv2.matchTemplate` 跟目前畫面比對(支援顏色、自動正規化尺度),只有信心不足時
(換頁、UI 改了等)才會退回呼叫 VLM。由於大多數遊戲 UI 是靜態的,這砍掉了大部分步驟裡
約 2 秒的視覺呼叫。開關在 `config.yaml` 的 `pilot.ui_cache`。

### 設計要點

**決策與定位分成兩次獨立的模型呼叫——這是這裡最重要的架構決定。** 模型從不輸出座標;
它只講出「要點什麼」(`target`),再由專門的 `locate()` 呼叫把它轉成座標框。同一個模型、
同一張截圖實測:合併成一次呼叫 → 想點某個按鈕,結果點到 265px 外的另一顆;拆成兩次呼叫 →
誤差只剩 7.2px。模型不是定位不準,是同時要它推理又要它算座標,思考把座標計算擠掉了。
捲動也是同樣原則:模型只講方向,拖曳向量的計算交給 Python(就算給它寫死的左右規則,
叫它自己算它還是會弄反)。

**截圖只擷取用戶區**(不含標題列/邊框)——早期版本含標題列,模型把系統視窗的 ✕
誤認成遊戲內的關閉鈕,把遊戲關掉了。座標一律用相對值(0~1000),每一步都重新讀取視窗矩形,
所以視窗移動、換螢幕、重開(PID/hwnd 改變)都不需要重新校準。

**思考模式的算術陷阱**:每一步都叫模型即時算一個跟時間有關的東西(例如「到午夜前還能做
幾次」),很容易讓它陷入迴圈——實測過任務描述只要有一點內部矛盾,單一步驟就能燒到 166 秒、
思考文字超過 4 萬字。修法有兩個方向:(1) 任何跟真實時間有關的計算都先用 Python 算好,
當成「已知事實」注入,不要叫模型自己重算;(2) 如果某個判斷會在多次工具呼叫間反覆出現,
把它寫成「單步判斷 + 附上一個算好的數字範例」(例如「讀 P,若 P+R>240 就做 X,否則
finish」),而不是「先一次算出總共要做幾次」——逐步判斷會收斂,但要模型每一步重新
自證的全域最適化不會。

**絕對不能讓一個任務看到自己上一輪的成敗紀錄,當成背景資訊。** 每日執行器會把「今天其他
任務做了什麼」秀給後面的任務看,讓它們知道前面的任務已經跑過了——但如果一個任務看到的是
**自己**上一輪的「✗ 超過步數上限」,它會推論成「我已經失敗過了、沒什麼好做的了」,直接放棄,
即使現有資源明明還做得完。組合背景摘要時,務必排除任務自己的名字。

**自動卡死恢復**:單一次 Ollama 呼叫可能卡住遠比任何一個步驟該花的時間更久(context
變長,或模型在某個含糊指令上開始鑽牛角尖)。`pilot.py` 對每次呼叫都設了可設定的
timeout(`pilot.request_timeout`);一旦超時就把模型卸載(`keep_alive: 0`,清掉 KV cache、
釋放顯存)再用較寬鬆的冷載入 timeout 重試。`pilot.num_predict` 也對單次「思考+回答」
設了 token 上限,即使 timeout 還沒觸發,思考爆量也不會無限跑下去。

### 為什麼不能在背景執行(已實測,別再試)

兩道牆都是遊戲自己的設計,換任何注入方式都一樣繞不過去:

1. 遊戲在非焦點狀態下完全不處理輸入(同座標、同送法:前景畫面差異 91、背景差異 0.00)。
2. 遊戲讀的是**真實游標位置**(`GetCursorPos`),`PostMessage` 訊息裡帶的座標會被忽略。

所以點擊非得「搶焦點 + 移動真實游標」不可,沒有繞過的方法。但**截圖不需要焦點**:
`capture.py` 用 `PrintWindow` 要求視窗自己在背景畫一份,裁掉標題列後跟真實抓螢幕的差異
是 0.00,被其他視窗蓋住也拿得到。所以一場長戰鬥裡數十次的 `wait` 完全不會碰到你的滑鼠或
焦點——只有真正點擊的那一瞬間才會搶(而且事後會把游標放回原處)。

**手動急停**:把滑鼠甩到螢幕角落(pyautogui 的 failsafe)可以乾淨地中止當前執行。

### 除錯工具

- `runs/log-<日期>.txt` / `runs/progress-<日期>.json`——每次執行的完整輸出與進度。
- `tools/ground_test.py`——重新測試模型的定位精度,輸出標記過的圖片
  (`--live` 測試當下畫面、`--model` 可切換模型)。
- `tools/click_diag.py`——診斷點擊是否真的送達遊戲,檢查雙方的提權狀態。
- `tools/bg_probe.py` / `bg_probe2.py`——背景輸入可行性探測(結論:不可行,見上)。

### 切換雲端模型

`vision.py` 提供 `OllamaProvider`(預設)與 `GeminiProvider`(雲端後備,定位能力更強)
同一套介面。要改用 Gemini:設定環境變數 `GEMINI_API_KEY`(從 Google AI Studio 申請)、
`pip install google-genai`,並把 `pilot.py` 裡建立 provider 的地方換成
`GeminiProvider(cfg["gemini"])`。
