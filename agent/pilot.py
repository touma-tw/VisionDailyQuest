# Copyright 2026 touma-tw
# SPDX-License-Identifier: Apache-2.0

"""Agentic pilot:讓模型在連續對話裡自己規劃、操作、糾錯。

和 workflow 版(loop.py)的根本差別:
  loop.py  每步無狀態,重新問「下一步點什麼」,由框架用大量規則約束模型。
  pilot.py 連續對話 + 開思考 + 給高層目標和工具,模型自己維護計畫、觀察結果、
           自己糾錯、自己判斷完成。像一個真正的 agent。

結構:一天的每日任務 = 一連串 episode,每個任務一段獨立對話(fresh),
  跑到 finish/give_up 後把結果摘要串給下一個任務。這樣 context 天然分段
  (實測整套每日約 20k tokens,32768 綽綽有餘;ollama 不重放歷史 thinking)。

工具(能力,不是約束):
  tap(target)    點擊(內部自己定位)
  swipe(dir)     捲動
  read(question) 專注讀畫面上某個資訊(比在長對話裡順便看準)
  wait(seconds)  等待(戰鬥/載入)
  finish(summary) / give_up(reason)
只保留最新一張截圖;歷史靠對話裡的動作紀錄(tool_call)保留。
"""

import base64
import ctypes
import ctypes.wintypes as wt
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pyautogui
import requests
from PIL import ImageChops

from . import input as game_input
from .capture import GameWindow, png_bytes, set_dpi_aware
from .ui_cache import TemplateCache
from .vision import OllamaProvider, WEEKDAYS

# 固定的「代理身分與通用操作常識」。遊戲專屬知識放在任務檔的 game_knowledge。
ROLE = """你是一個操作 Windows 遊戲《鈴蘭之劍》的自主代理。你透過呼叫工具操作遊戲,達成交代的目標。

工作方式:看當前畫面 → 思考該做什麼 → 呼叫一個工具 → 看操作後的新畫面 → 繼續,直到達成目標。
你是一個會自己規劃、自己觀察、自己糾錯的代理:
- 點錯了、進到不對的頁面,自己退回去重試,不要硬幹。
- 離開一個頁面通常點畫面左上角的返回箭頭;但**有些頁面(例如「代行記錄」、部分彈窗)沒有返回箭頭**,
  這種時候點畫面**右側**的空白處(人物立繪旁邊、沒有按鈕的暗色區域)就能關閉離開。
- **工具會誠實回報結果**。如果它說「畫面幾乎沒有變化」,代表你剛才那一下沒生效 ——
  不要用同樣的方法再試一次,換個目標或換個離開方式。
- 需要精確數字(例如存在之力、剩餘次數)來做決定時,用 read 工具讀,不要用猜的。
- 需要計算時,在思考裡一步一步算清楚。
- **任務要依序做/檢查好幾件事時(例如「派遣、日常、兌換各做一次」),每確認或完成一件,就用 note 記一條**。
  你的筆記每一步都會回顯給你 —— 靠它記住哪幾件已經做完了,不要每一步都從頭重查,做完的就跳過。
  當筆記顯示該做的都做完了,就 finish。
- 一次呼叫一個工具。達成目標呼叫 finish;確定做不到(例如資源不足)呼叫 give_up。
- 如果迷路了不知道自己在哪,連續點左上角返回箭頭退回「大廳」(遊戲主畫面),再重新出發。"""

TOOLS = [
    {"type": "function", "function": {
        "name": "tap",
        "description": "點擊畫面上的某個東西。用自然語言精確描述目標(有文字就用文字,"
                       "純圖示就描述外觀),系統會自己定位並點擊。",
        "parameters": {"type": "object", "properties": {
            "target": {"type": "string", "description": "要點的東西,例如「右下角的『代行』按鈕」"},
        }, "required": ["target"]}}},
    {"type": "function", "function": {
        "name": "swipe",
        "description": "捲動畫面,把還沒露出的內容捲進來。direction = **你想看的方向**:"
                       "想看右邊還沒出現的卡片就 right、想看左邊就 left、想看下面就 down、想看上面就 up。"
                       "只要說你想往哪個方向看,**不要自己去推手指往哪滑、或內容會往哪移,那只會弄反**。",
        "parameters": {"type": "object", "properties": {
            "direction": {"type": "string", "enum": ["up", "down", "left", "right"],
                          "description": "你想看的方向(右邊的東西就 right)"},
        }, "required": ["direction"]}}},
    {"type": "function", "function": {
        "name": "read",
        "description": "專注地讀出畫面上某個資訊(數字、狀態、標題)。需要精確數值時用這個,"
                       "比你自己順便看更準。看不到會告訴你看不到。",
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string", "description": "要讀什麼,例如「右上角的存在之力是多少?」"},
        }, "required": ["question"]}}},
    {"type": "function", "function": {
        "name": "wait",
        "description": "等待畫面變化(戰鬥進行中、載入中、動畫播放中)。",
        "parameters": {"type": "object", "properties": {
            "seconds": {"type": "number", "description": "等幾秒,通常 3"},
        }, "required": ["seconds"]}}},
    {"type": "function", "function": {
        "name": "exchange",
        "description": "完成遠航『交易行』的每日兌換(自動:切到交易行分頁→捲到頂兌最頂端大項→捲到底兌最底端各項)。"
                       "**只有當你已經在遠航的兌換頁面(左側看得到「商店」「交易行」兩個分頁)時才呼叫。**"
                       "呼叫這一個工具就會把整個兌換做完,你不需要、也不要自己手動捲動或逐顆點兌換 —— "
                       "捲動到頂/到底的判斷交給它做比你準。呼叫後看新畫面確認,兌換這一項就算完成。",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "note",
        "description": "記下一條進度筆記(例如「派遣已確認做完」「兌換全已兌完」「已代行1次,存在之力剩184」)。"
                       "你記下的筆記之後每一步都會回顯給你看,是你的外部記憶。"
                       "**多步驟、要依序檢查好幾件事的任務,每確認/完成一件就記一條** —— "
                       "這樣你才不會忘記做到哪、才不會把同一件事重做或重查。這個動作不會改變畫面。",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "要記的一句話"},
        }, "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "finish",
        "description": "確認目標已達成時呼叫。",
        "parameters": {"type": "object", "properties": {
            "summary": {"type": "string", "description": "做了什麼、結果如何"},
        }, "required": ["summary"]}}},
    {"type": "function", "function": {
        "name": "give_up",
        "description": "確定無法達成目標時呼叫(例如資源不足、反覆卡住)。",
        "parameters": {"type": "object", "properties": {
            "reason": {"type": "string"},
        }, "required": ["reason"]}}},
]


def _screen_changed(a, b, threshold: float = 6.0) -> bool:
    if a.size != b.size:
        return True
    diff = ImageChops.difference(a.convert("L"), b.convert("L")).histogram()
    total = sum(diff)
    return (sum(i * c for i, c in enumerate(diff)) / total if total else 0) >= threshold


def _fingerprint(img):
    """畫面指紋(縮圖量化),用來偵測「在少數畫面之間語意繞圈」——
    這種繞圈每步畫面都在變,_screen_changed 抓不到,但畫面會反覆回到同幾個狀態。"""
    small = img.convert("L").resize((12, 9))
    return tuple(p // 24 for p in small.tobytes())


def _fp_close(a, b, tol: int = 6) -> bool:
    """兩個指紋是不是幾乎同一個畫面。容忍少數格子差異——體力數字、倒數計時在跳
    只會動到一兩格,不同的頁面則會差很多格。"""
    return a is not None and b is not None and sum(1 for x, y in zip(a, b) if x != y) <= tol


def _loop_hint(prints, window=10, revisit=3) -> str:
    """偵測繞圈:最近 window 步內,和「當前畫面」幾乎相同的畫面出現 >= revisit 次,
    代表在原地反覆回到同一狀態、沒有往前推進。回一段感官提示(不強制停,讓 agent 自己判斷)。

    比舊版(數 window 內有幾種畫面)更 robust:遠航那種「派遣頁↔兌換頁↔大廳」的大圈,
    畫面種類雖多,但只要一直回到同幾個畫面,就會被抓到。"""
    if len(prints) < 6:
        return ""
    recent = prints[-window:]
    cur = recent[-1]
    seen = sum(1 for fp in recent if _fp_close(fp, cur))
    if seen >= revisit:
        return ("\n\n⚠ 停一下:你已經第 %d 次回到這個畫面了,最近好幾步都在原地繞圈、"
                "沒有任何實質進展。**這幾乎可以確定代表:你要做的事其實都已經做完了**"
                "(該領的領了、該派的派了、已兌完、次數用光、沒有可做的了)。"
                "請不要再重複剛才的檢查——立刻重新判斷整個任務是不是已經達成:"
                "是的話**現在就呼叫 finish**回報做完;只有你能明確講出「還有具體哪一件事沒做、"
                "而且知道怎麼做」時,才換一個完全不同的做法,否則就 finish 或 give_up。") % seen
    return ""


user32 = ctypes.windll.user32

# 交易行兌換:每次兌換 = 點一下「兌換」+ 再點一下關掉「收穫一覽」通知(此遊戲彈窗任點即消)。
# 要兌哪些座標由「互動校準」(calibrate_exchange)人工指出並存檔,exchange 只是重播那份座標。
# 中間兩兩並列的稀有兌換絕不能碰(浪費稀有貨幣);因為它們的座標根本沒被記進校準檔,重播不可能點到。
EX_TOP_ROUNDS = 5    # 頂端大項最多兌幾次(每次 2 下:兌換 + 關窗);點到灰色再多點也無害
EX_INTERVAL = 0.7    # 每下點擊之間的間隔秒數:等收穫一覽彈窗跳出/消失(使用者實測需要微小間隔)


def _notes_block(notes) -> str:
    """把 agent 記下的進度筆記組成一段,每步回顯給它看(外部工作記憶)。"""
    if not notes:
        return ""
    return ("\n\n【你的進度筆記】(已確認/已完成的事,不要重做也不要重查):\n"
            + "\n".join(f"- {n}" for n in notes))


class Pilot:
    def __init__(self, cfg: dict, knowledge: str = "", verbose=True):
        oc = cfg["ollama"]
        self.base = oc["url"].rstrip("/")
        self.url = self.base + "/api/chat"
        self.model = oc["model"]
        pc = cfg.get("pilot", {})
        self.num_ctx = pc.get("num_ctx", 65536)
        self.think = pc.get("think", True)
        # 單次生成(思考+回答)的 token 上限:防止思考爆量(實測壞指令會讓思考膨脹到 4 萬字、
        # 166 秒還吐不出答案),把它硬性截斷,絕大多數正常步驟遠用不到這麼多。
        self.num_predict = pc.get("num_predict", 4096)
        self.keep_alive = pc.get("keep_alive", "30m")
        # 卡死判定:單次呼叫超過這麼多秒沒回應,就判定本地模型卡住/變慢 → 重啟模型後重試。
        self.request_timeout = pc.get("request_timeout", 60)
        # 重啟模型後那一次呼叫要冷載入,給比較寬鬆的 timeout,免得剛重載又被判成卡死。
        self.reload_timeout = pc.get("reload_timeout", 180)
        self.max_retries = pc.get("max_retries", 2)
        # 借它的 locate/read 專用呼叫。關鍵:把 pilot 的 num_ctx/keep_alive 併進去,
        # 讓定位/讀值和決策對話用同一組載入參數 —— 否則 ollama 會把同一個模型當兩個
        # runner,在 chat↔定位之間每步反覆卸載/重載(VRAM 每幾秒滿載↔基線來回跳,超慢)。
        self.provider = OllamaProvider({**oc, "num_ctx": self.num_ctx,
                                        "keep_alive": self.keep_alive})
        self.win = GameWindow(cfg["window_title"])
        self.knowledge = knowledge
        self.verbose = verbose
        # UI 座標快取:讓 tap 的定位在重複時免呼叫 VLM(見 ui_cache.py)。
        self.cache = None
        if pc.get("ui_cache", True):
            self.cache = TemplateCache(Path(pc.get("cache_dir", "ui_cache")),
                                       threshold=pc.get("cache_threshold", 0.90),
                                       verbose=verbose)
        set_dpi_aware()
        self.win.locate()

    def _capture(self) -> tuple[str, tuple]:
        """截圖 → (base64, 畫面指紋)。"""
        img = self.win.screenshot()
        return base64.b64encode(png_bytes(img)).decode(), _fingerprint(img)

    def _reload_model(self):
        """把模型從 VRAM 卸載(keep_alive=0):清掉累積的 KV cache、釋放顯存,下一次呼叫時
        ollama 會重新載入模型 —— 藉此把「跑久了越來越慢/卡死」的本地模型恢復到剛載入的速度。
        這就是設定裡 request_timeout 觸發時所謂的『重啟本地 AI』。"""
        try:
            requests.post(self.base + "/api/generate",
                          json={"model": self.model, "keep_alive": 0}, timeout=30)
            time.sleep(1.5)  # 給 ollama 一點時間真正把模型放掉
        except requests.exceptions.RequestException:
            pass  # 連卸載都連不上,交給下一次呼叫的重試/報錯處理

    def _chat(self, messages):
        body = {
            "model": self.model, "messages": messages, "tools": TOOLS,
            "think": self.think, "stream": False,
            "options": {"temperature": 0, "num_ctx": self.num_ctx,
                        "num_predict": self.num_predict},
            "keep_alive": self.keep_alive,
        }
        last_err = None
        for attempt in range(self.max_retries + 1):
            # 首次用正常 timeout;重試時剛重啟過模型、要冷載入,給較長 timeout 免得又被誤判卡死。
            timeout = self.request_timeout if attempt == 0 else self.reload_timeout
            try:
                r = requests.post(self.url, json=body, timeout=(10, timeout))
                r.raise_for_status()
                return r.json()["message"]
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError,
                    requests.exceptions.HTTPError) as e:
                last_err = e
                if attempt < self.max_retries:
                    print(f"  ⚠ 本地模型 {timeout}s 內沒有回應({type(e).__name__});"
                          f"重啟模型後重試({attempt + 1}/{self.max_retries})…")
                    self._reload_model()
                else:
                    print(f"  ⚠ 本地模型連續無回應,放棄這一步({type(e).__name__})")
        raise last_err

    @staticmethod
    def _drop_old_images(messages):
        # 只保留最新一張圖:歷史靠對話裡的動作紀錄(文字)記得。thinking 也順手丟(ollama 本來就不重放)。
        for m in messages:
            if m.get("images"):
                m.pop("images")
                if m["role"] == "user" and "（截圖已省略" not in (m.get("content") or ""):
                    m["content"] = (m.get("content") or "") + "（截圖已省略,見最新畫面）"
            m.pop("thinking", None)

    def _resolve(self, before, target: str):
        """定位 target:先快取模板比對(免 VLM),沒命中才走 VLM 定位。回 (pos or None, from_cache)。"""
        hit = self.cache.lookup(before, target) if self.cache else None
        if hit is not None:
            return (hit[0], hit[1]), True
        return self.provider.locate(png_bytes(before), target), False

    def _swipe_to_end(self, direction: str, max_swipes: int = 6) -> int:
        """朝某方向捲到硬停(捲不動了)= 確定性地到頂/到底,不靠模型判斷。回實際捲了幾次。

        模型判斷不出「是否到頂/到底」(遠航兌換卡死的主因),但框架有可靠訊號:
        捲一下後畫面若完全沒變,就是已到邊界。到底就是到底,不會誤判。"""
        n = 0
        for _ in range(max_swipes):
            before = self.win.screenshot()
            game_input.scroll(self.win, direction)
            time.sleep(0.5)
            n += 1
            if not _screen_changed(before, self.win.screenshot()):
                break  # 畫面沒動 = 到邊界了
        return n

    def _tap_and_dismiss(self, rx: float, ry: float):
        """一次兌換 = 點一下(觸發兌換,跳出「收穫一覽」)+ 再點一下(此遊戲彈窗任點即消)。
        點的是同一個位置:第一下打在兌換鈕上;第二下時彈窗在最上層,任點都只是把它關掉。"""
        game_input.click(self.win, rx, ry)
        time.sleep(EX_INTERVAL)
        game_input.click(self.win, 900, 900)
        time.sleep(EX_INTERVAL)

    def _exchange_coords_path(self) -> Path:
        base = self.cache.dir if self.cache else Path("ui_cache")
        return base / "exchange_coords.json"

    def _capture_points(self, max_pts: int) -> list[tuple[float, float]]:
        """互動記錄座標:使用者把滑鼠移到目標上按 F8 記一點、按 Esc 結束這一段。
        讀真實游標位置換算成 0~1000 相對座標(全域偵測按鍵,終端機或遊戲有沒有焦點都行)。"""
        VK_F8, VK_ESC = 0x77, 0x1B
        pts: list[tuple[float, float]] = []
        while len(pts) < max_pts:
            if user32.GetAsyncKeyState(VK_ESC) & 0x8000:
                break
            if user32.GetAsyncKeyState(VK_F8) & 0x8000:
                pt = wt.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                left, top, w, h = self.win.rect
                rx = min(max((pt.x - left) / w * 1000, 0), 1000)
                ry = min(max((pt.y - top) / h * 1000, 0), 1000)
                pts.append((rx, ry))
                print(f"    記錄 #{len(pts)}: ry={ry:.0f} rx={rx:.0f}")
                time.sleep(0.45)  # 防手震重複記錄
            time.sleep(0.03)
        return pts

    def calibrate_exchange(self):
        """互動校準:自動捲到頂/底當錨點,由使用者用滑鼠 + F8 指出要兌的按鈕座標,存成校準檔。
        完全不點兌換、不靠 VLM/模板 —— 不管按鈕現在是亮是灰都能記;之後 exchange 用這份座標重播。"""
        print("\n=== 兌換座標校準(互動式;不會替你點任何兌換鈕)===")
        print("請先手動把遊戲切到『遠航 → 兌換』頁(左側看得到商店/交易行分頁)。3 秒後開始…")
        time.sleep(3)
        # 切到交易行 + 捲到頂,和 exchange 重播用同一個錨點
        before = self.win.screenshot()
        pos, _ = self._resolve(before, "左側下方的『交易行』分頁(錢袋圖示)")
        if pos is not None:
            game_input.click(self.win, *pos)
            time.sleep(0.9)
        self._swipe_to_end("up")
        print("\n[頂端] 已捲到最頂。把滑鼠移到『最頂端大項的兌換鈕』上按 F8 記一點;按 Esc 繼續。")
        top_pts = self._capture_points(max_pts=1)
        top = top_pts[0] if top_pts else None

        self._swipe_to_end("down")
        print("\n[最底] 已捲到最底。**依序**把滑鼠移到每一個要兌的最底端兌換鈕上,各按 F8;")
        print("       全部記完按 Esc 結束。(中間禁碰的稀有項不要記)")
        bottom = self._capture_points(max_pts=12)

        data = {"top": list(top) if top else None, "bottom": [list(b) for b in bottom]}
        p = self._exchange_coords_path()
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print("\n--- 校準結果 ---")
        print(f"頂端大項: {('ry=%.0f rx=%.0f' % (top[1], top[0])) if top else '未記(略過)'}")
        print(f"最底端  : {len(bottom)} 顆 " + ", ".join(f"(ry={ry:.0f})" for _, ry in bottom))
        print(f"已寫入:{p}")
        print("之後 `遠航` 跑到兌換會用這份座標重播:捲到頂點大項 5 輪、捲到底每個座標各一次,不再搜尋。")

    def _do_exchange(self) -> str:
        """完成遠航『交易行』每日兌換 —— 用校準好的座標**重播**(不靠模板搜尋、不會跑去點中間禁碰項)。

        安全關鍵:要點哪些座標是校準時人工核對過的(只有最頂大項 + 最底端允許項),中間禁碰的
        稀有項根本沒存進來、永遠不可能被點到。捲到硬停當錨點,每次座標都對得上;點到已兌完的
        灰鈕也無害(不會兌、不跳彈窗)。校準檔不存在時安全略過,不盲點。
        """
        p = self._exchange_coords_path()
        if not p.exists():
            return ("兌換未執行(安全略過):還沒有校準檔。請先在兌換頁跑一次 "
                    "`python -m agent.pilot 兌換校準`(只偵測不點),人工核對座標後,exchange 才會動作。")
        data = json.loads(p.read_text(encoding="utf-8"))
        steps = []
        before = self.win.screenshot()
        pos, _ = self._resolve(before, "左側下方的『交易行』分頁(錢袋圖示)")
        if pos is None:
            return "兌換未執行:找不到『交易行』分頁,你可能還沒進到兌換頁。請先點左下角『兌換』進入兌換頁再呼叫。"
        game_input.click(self.win, *pos)
        time.sleep(0.9)
        steps.append("已確認在交易行")

        self._swipe_to_end("up")
        if data.get("top"):
            rx, ry = data["top"]
            for _ in range(EX_TOP_ROUNDS):
                self._tap_and_dismiss(rx, ry)
            steps.append(f"頂端大項(ry={ry:.0f}):兌 {EX_TOP_ROUNDS} 輪")
        else:
            steps.append("頂端大項:校準檔標記為無 → 略過")

        self._swipe_to_end("down")
        bottom = data.get("bottom") or []
        for rx, ry in bottom:
            self._tap_and_dismiss(rx, ry)
        steps.append(f"最底端:重播 {len(bottom)} 顆座標")
        return "兌換完成(座標重播)—— " + "；".join(steps) + "。看新畫面確認。"

    def _exec(self, name, args) -> str:
        if name == "tap":
            target = args.get("target", "")
            before = self.win.screenshot()
            # 先試快取的模板比對(免 VLM);沒命中才走 VLM 定位。
            pos, from_cache = self._resolve(before, target)
            if pos is None:
                return (f"找不到「{target}」——它可能不在當前畫面上,或描述不夠精確。"
                        "看新畫面,換個更明確的描述或換個做法。")
            game_input.click(self.win, *pos)
            time.sleep(0.8)
            after = self.win.screenshot()
            if not _screen_changed(before, after):
                return (f"點了「{target}」,但畫面幾乎沒有變化。這通常代表:那個位置點不動、"
                        "或你要點的東西其實不在那裡、或你描述的目標被定位到了別的東西上。"
                        "**不要再用同樣的描述重點**,看清楚新畫面,換個目標或換個離開方式。")
            # 點了畫面真的有變 = 這次定位是對的。若剛才是走 VLM,把位置學進快取,下次免 VLM。
            if not from_cache and self.cache:
                self.cache.store(before, target, pos[0], pos[1])
            return f"已點擊「{target}」,畫面有變化。"
        if name == "exchange":
            return self._do_exchange()
        if name == "swipe":
            d = args.get("direction", "")
            before = self.win.screenshot()
            game_input.scroll(self.win, d)
            time.sleep(0.5)
            after = self.win.screenshot()
            where = {"left": "左邊", "right": "右邊", "up": "上方", "down": "下方"}.get(d, d)
            if _screen_changed(before, after):
                return f"已捲動,現在畫面顯示的是更靠{where}的內容了。"
            return (f"往{where}捲動了,但畫面幾乎沒變 —— 這個方向可能已經到底了(沒有更多內容),"
                    "換個方向、或改用別的做法,不要再往同方向硬捲。")
        if name == "read":
            q = args.get("question", "")
            return f"讀取結果:{self.provider.read(png_bytes(self.win.screenshot()), q)}"
        if name == "wait":
            time.sleep(float(args.get("seconds", 3)))
            return "已等待。看新畫面。"
        return f"未知工具 {name}"

    def run_episode(self, name: str, goal: str, done_summary: str = "",
                    max_steps: int = 80) -> tuple[bool, str]:
        """跑一個任務(一段獨立對話)。回傳 (是否成功, 結果摘要)。"""
        now = datetime.now()
        midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        mins = int((midnight - now).total_seconds() // 60)
        # 到午夜前存在之力自然恢復量(每 4 分鐘 1 點),先用 Python 算好注入 ——
        # 時間差運算交給模型它會想到爆(星辰試煉曾為了「該做幾次」反覆重算 877/4 卡死)。
        recover = mins // 4

        system = ROLE + ("\n\n# 這個遊戲的背景知識\n" + self.knowledge if self.knowledge else "")
        head = (f"現在:{now:%Y-%m-%d %H:%M}(星期{WEEKDAYS[now.weekday()]}),距離午夜還有 {mins} 分鐘"
                f"(到午夜前存在之力還會自然恢復約 {recover} 點,需要用到就直接引用這個數字,不要自己重算時間)。\n")
        if done_summary:
            head += f"今天目前為止的進度:{done_summary}\n"
        head += f"\n# 本次任務「{name}」\n{goal}\n\n這是當前畫面:"

        prints = []
        notes: list[str] = []
        b64, fp = self._capture()
        prints.append(fp)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": head, "images": [b64]},
        ]

        for step in range(1, max_steps + 1):
            msg = self._chat(messages)
            messages.append(msg)
            calls = msg.get("tool_calls") or []
            if self.verbose:
                think = (msg.get("thinking") or "").strip()
                if think:
                    print(f"  [{name}][{step}] 想:{think[:150]}")

            if not calls:
                messages.append({"role": "user",
                                 "content": "請呼叫一個工具來實際操作,或呼叫 finish/give_up。"})
                continue

            fn = calls[0]["function"]
            tname, args = fn["name"], fn.get("arguments") or {}
            if self.verbose:
                print(f"  [{name}][{step}] 做:{tname}({args})")

            if tname == "finish":
                s = args.get("summary", "")
                print(f"  [{name}] ✓ {s}")
                return True, s
            if tname == "give_up":
                s = args.get("reason", "")
                print(f"  [{name}] ✗ 放棄:{s}")
                return False, s
            if tname == "note":
                notes.append(args.get("text", ""))
                if self.verbose:
                    print(f"        → 已記下筆記(共 {len(notes)} 條)")
                # note 不改變畫面:回顯目前筆記,不重新截圖(最新畫面仍在對話裡)
                messages.append({"role": "tool",
                                 "content": "已記下。" + _notes_block(notes)})
                continue

            result = self._exec(tname, args)
            if self.verbose:
                print(f"        → {result}")
            messages.append({"role": "tool", "content": result})
            time.sleep(1.0)
            self._drop_old_images(messages)
            b64, fp = self._capture()
            prints.append(fp)
            messages.append({"role": "user",
                             "content": "操作後的新畫面:" + _notes_block(notes) + _loop_hint(prints),
                             "images": [b64]})

        print(f"  [{name}] ✗ 超過 {max_steps} 步仍未完成")
        return False, "超過步數上限"


# 每天的執行紀錄存在這裡:跨「多次執行/單獨跑某任務/當機重開」都記得今天做過什麼。
RUNS_DIR = Path("runs")


def _progress_path() -> Path:
    return RUNS_DIR / f"progress-{date.today():%Y-%m-%d}.json"


def _load_progress() -> dict:
    """讀今天的進度檔:{任務名: {ok, summary, time}}。沒有就回空 dict。"""
    p = _progress_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_progress(prog: dict):
    RUNS_DIR.mkdir(exist_ok=True)
    _progress_path().write_text(json.dumps(prog, ensure_ascii=False, indent=2), encoding="utf-8")


def run_daily(cfg: dict, doc: dict, only: str | None = None):
    """依任務清單逐個 episode 執行。進度寫進 runs/progress-<日期>.json,跨執行都記得。"""
    pilot = Pilot(cfg, knowledge=doc.get("game_knowledge", ""))
    tasks = doc["tasks"]
    if only:
        tasks = [t for t in tasks if t["name"] == only]
        if not tasks:
            sys.exit(f"找不到任務「{only}」。可用:{'、'.join(t['name'] for t in doc['tasks'])}")

    prog = _load_progress()  # 今天到目前為止的紀錄(可能來自稍早的執行)
    if prog:
        seen = "、".join(f"{n}{'✓' if v.get('ok') else '✗'}" for n, v in prog.items())
        print(f"（讀到今天已有的進度:{seen}）")
    # 把「別的任務的結果」串成給 agent 看的背景。key 用任務名,因為給某任務看背景時要
    # **排除它自己的紀錄** —— 否則重跑時,它會讀到自己上一輪的「✗(超過步數上限)」當成
    # 「本任務已失敗/已沒步數」而直接 give_up(實測星辰試煉就是這樣自我否定放棄的)。
    done = {n: f"{'✓' if v.get('ok') else '✗'}({(v.get('summary','') or '')[:20]})"
            for n, v in prog.items()}

    for t in tasks:
        name = t["name"]
        # 跑整套時,今天已成功的任務直接跳過(單獨指定某任務則一定照跑)。
        if not only and prog.get(name, {}).get("ok"):
            print(f"\n▶ {name} —— 今天已完成,跳過")
            continue
        print(f"\n▶ {name}")
        # 背景只給「別的」任務的結果,絕不含本任務自己(避免自我否定的邏輯陷阱)。
        others = "；".join(f"{n}={s}" for n, s in done.items() if n != name)
        try:
            ok, summary = pilot.run_episode(
                name, t["goal"], others, t.get("max_steps", 80))
        except pyautogui.FailSafeException:
            # 滑鼠移到螢幕角落 = pyautogui 手動急停(使用者急停或誤觸)。乾淨中止整輪,別噴 traceback。
            print("\n🛑 偵測到滑鼠被移到螢幕角落(pyautogui 手動急停),已中止今天的自動流程。")
            break
        except Exception as e:
            # 單一任務出錯不該拖垮整輪:記下來,繼續下一個。
            print(f"  [{name}] ✗ 發生未預期錯誤:{type(e).__name__}: {e}")
            ok, summary = False, f"錯誤:{type(e).__name__}"
        prog[name] = {"ok": ok, "summary": summary, "time": datetime.now().strftime("%H:%M")}
        _save_progress(prog)  # 每做完一個就存,當機重開也記得
        done[name] = f"{'✓' if ok else '✗'}({(summary or '')[:30]})"

    print("\n=== 今日結果 ===")
    for n, v in prog.items():
        print(f"  {n}={'✓' if v.get('ok') else '✗'} ({v.get('time','')}) {v.get('summary','')[:40]}")
    if pilot.cache:
        print(f"  {pilot.cache.stats_line()}")
    print(f"（完整紀錄:{_progress_path()}）")


class _Tee:
    """把 console 輸出同時寫到終端機和 log 檔,方便事後回看今天跑了什麼。"""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, s):
        for st in self.streams:
            st.write(s)

    def flush(self):
        for st in self.streams:
            st.flush()


if __name__ == "__main__":
    import yaml

    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    RUNS_DIR.mkdir(exist_ok=True)
    _logf = open(RUNS_DIR / f"log-{date.today():%Y-%m-%d}.txt", "a", encoding="utf-8", buffering=1)
    _logf.write(f"\n\n===== 開始執行 {datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
    sys.stdout = _Tee(sys.stdout, _logf)

    cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    doc = yaml.safe_load(Path("tasks/daily_goals.yaml").read_text(encoding="utf-8"))
    # python -m agent.pilot            → 跑整套每日(今天已完成的會自動跳過)
    # python -m agent.pilot 星辰試煉   → 只跑一個任務(照跑,不因今天做過而跳過)
    # python -m agent.pilot 兌換校準   → (先手動切到兌換頁)只偵測兌換座標、不點,寫校準檔供人工核對
    only = sys.argv[1] if len(sys.argv) > 1 else None
    if only == "兌換校準":
        Pilot(cfg, knowledge=doc.get("game_knowledge", "")).calibrate_exchange()
    else:
        run_daily(cfg, doc, only)
