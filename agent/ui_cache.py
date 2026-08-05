# Copyright 2026 touma-tw
# SPDX-License-Identifier: Apache-2.0

"""UI 座標快取:讓「找某個按鈕在哪」在重複時免呼叫 VLM。

背景(和 pilot.py / vision.py 的分工):
  tap(target) 現在每一步都呼叫 provider.locate() —— 那是一次 ~2s 的 VLM 視覺辨識。
  但這遊戲的 UI 幾乎固定不動:同一顆按鈕、同一個返回箭頭,天天長一樣、位置只隨
  視窗大小等比例變。對「固定不動的東西」每步都重問 VLM,是這個系統最大的浪費。

做法(「會自我訂正的按鍵精靈」的感知層):
  第一次遇到某個 target → 照舊用 VLM 定位;**只有在點下去、畫面真的有變化(=定位正確)**
  時,才把該處周圍剪一小塊「模板 patch」存起來。
  之後再要找同一個 target → 先用 cv2.matchTemplate 拿這塊 patch 去比對當前畫面:
    - 信心夠高 → 直接用「比對到的位置」當點擊點(注意:是比對到的、不是存起來的座標,
      所以視窗移動/縮放都會自動跟著修正),**完全不呼叫 VLM**。
    - 信心不足(換頁了、UI 改版了、根本不在這個畫面)→ 回報 miss,讓呼叫端 fallback 回 VLM,
      並在成功後重新學一份。

為什麼比對是在「正規化畫布」上做:
  matchTemplate 本身不是尺度不變的。所以截圖一律先縮到固定寬度 CANON_W 再比對/剪裁,
  patch 也存在這個正規化尺度上。只要遊戲 UI 隨視窗等比例縮放(而非置中留黑邊),
  不同視窗大小就都能命中。若哪天發現是留黑邊式縮放,信心會掉下來、自動 fallback 回 VLM,
  不會點錯 —— 這層永遠只是「快而已」,錯了有 VLM 和 agent 的誠實回饋兜底。

刻意不做的事(v1 保持小):
  - 不因「點了沒變化」就把 entry 踢掉:很多合法按鈕(全部領取、已兌完)點了本來就沒反應,
    踢掉會誤傷。UI 真的變了,信心自然掉 < 門檻 → 自動走 VLM 重學,不需要主動淘汰。
  - 不做整段序列重播 / 畫面圖:那是下一階段,會複用這裡的 matchTemplate 機制。
"""

import hashlib
import json
import re
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

CANON_W = 960          # 正規化畫布寬度(比對/剪裁都在這個尺度上做)
PATCH_HALF_W = 0.060   # patch 半寬 = 正規化畫布寬度的比例(整塊約 12% 寬)
PATCH_HALF_H = 0.032   # patch 半高 = 正規化畫布高度的比例(整塊約 6.4% 高)
MATCH_THRESHOLD = 0.90  # TM_CCOEFF_NORMED 命中門檻;文字按鈕紋理豐富,0.9 夠穩又不易誤命中
MIN_PATCH_STD = 8.0    # patch 太平(近乎純色)時比對不可靠,不存
MAX_CANDIDATES = 6     # 同一個 target 最多留幾份 patch(不同畫面/外觀變體)


def _canon(img: Image.Image) -> np.ndarray:
    """PIL 圖 → 正規化寬度的**彩色** numpy 陣列(H, W, 3) uint8。

    用彩色而非灰階,是因為要分辨「亮色可兌換 vs 灰色已兌完」這種同形狀不同狀態的按鈕 ——
    顏色是最強的區分訊號(灰階下形狀相關性會把灰鈕也匹配到)。彩色也讓相似圖示更好分。"""
    w, h = img.size
    ch = max(1, round(CANON_W * h / w))
    a = img.convert("RGB").resize((CANON_W, ch))
    return np.asarray(a, dtype=np.uint8)


# 抽取「最內層引號」內的內容(不含巢狀引號)。模型常把真正的按鈕標籤用引號框起來
# (『代行』、「全部領取」),而外層可能還有一圈裝飾(「右下角標著⚡40的『代行』按鈕」)。
_INNER_QUOTE = re.compile(r"[「『]([^「」『』]+)[」』]")
# 無引號時要脫掉的裝飾:位置詞 + 泛稱詞。長的排前面先脫(右上角 先於 右上)。
_STRIP = sorted((
    "畫面上", "畫面", "最上排", "上排", "下排", "最上方", "正上方", "最上", "最下方", "最下",
    "右上角", "左上角", "右下角", "左下角", "正中央", "中間", "右上", "左上", "右下", "左下",
    "右側", "左側", "上方", "下方", "旁邊", "那邊", "這邊",
    "按鈕", "圖示", "圖案", "那顆", "那個", "這個", "一個", "符號", "的",
), key=len, reverse=True)


def _canon_target(target: str) -> str:
    """把 target 正規化成穩定的鑰匙:同一顆按鈕不同措辭要對映到同一個 key。

    模型每次描述同一顆按鈕的措辭會飄(『全部領取』按鈕 / 右上角全部領取 / 全部領取那顆),
    但引號裡的**標籤**是穩定的核心。取引號內最短的那段當 key;沒有引號(純圖示,如獎盃、
    返回箭頭)才退而求其次:去掉標點、位置詞與泛稱詞,留下核心名稱。視覺比對仍會再把關,
    所以就算兩顆按鈕正規化後同名,也靠模板比對分辨畫面、不會點錯。"""
    quoted = _INNER_QUOTE.findall(target)
    if quoted:
        return min((q.strip() for q in quoted), key=len)
    s = re.sub(r"[\s「」『』（）()【】\[\]·、,，。.!！?？]", "", target)
    for f in _STRIP:
        s = s.replace(f, "")
    return s.strip() or target.strip()


def _key(target: str) -> str:
    return hashlib.sha1(_canon_target(target).encode("utf-8")).hexdigest()[:16]


class TemplateCache:
    """target 字串 → 若干「模板 patch + 點擊點在 patch 內的位移」。座標一律走 0~1000 相對。"""

    def __init__(self, cache_dir: Path, threshold: float = MATCH_THRESHOLD, verbose: bool = True):
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.dir / "index.json"
        self.threshold = threshold
        self.verbose = verbose
        self.hits = 0        # 這次執行:命中快取、省下的 VLM 定位次數
        self.misses = 0      # 這次執行:沒命中、走了 VLM 的次數
        self.learned = 0     # 這次執行:新學進快取的 patch 數
        self._index: dict[str, list[dict]] = {}
        self._patches: dict[str, np.ndarray] = {}   # patch_file → 彩色陣列(lazy 載入)
        self._load()

    # --- 持久化 ---

    def _load(self):
        if self.index_path.exists():
            try:
                self._index = json.loads(self.index_path.read_text(encoding="utf-8"))
            except Exception:
                self._index = {}

    def _save(self):
        self.index_path.write_text(
            json.dumps(self._index, ensure_ascii=False, indent=2), encoding="utf-8")

    def _patch_img(self, fname: str) -> np.ndarray | None:
        if fname not in self._patches:
            p = self.dir / fname
            if not p.exists():
                return None
            arr = cv2.imread(str(p), cv2.IMREAD_COLOR)
            self._patches[fname] = arr
        return self._patches[fname]

    # --- 查詢(fast path)---

    def lookup(self, img: Image.Image, target: str) -> tuple[float, float, float] | None:
        """在當前畫面上找 target。命中回 (rx, ry, confidence);沒有夠好的比對回 None。"""
        cands = self._index.get(_key(target))
        if not cands:
            self.misses += 1   # 這個 target 還沒學過 → 會走 VLM,誠實記成一次 miss
            return None
        canon = _canon(img)
        ch, cw = canon.shape[:2]
        best = None  # (conf, rx, ry)
        for c in cands:
            patch = self._patch_img(c["patch"])
            if patch is None or patch.shape[0] > ch or patch.shape[1] > cw:
                continue
            res = cv2.matchTemplate(canon, patch, cv2.TM_CCOEFF_NORMED)
            _, maxv, _, maxloc = cv2.minMaxLoc(res)
            if best is None or maxv > best[0]:
                px = maxloc[0] + c["offx"]
                py = maxloc[1] + c["offy"]
                best = (maxv, px / cw * 1000.0, py / ch * 1000.0)
        if best and best[0] >= self.threshold:
            self.hits += 1
            if self.verbose:
                print(f"        · 快取命中「{target}」(信心 {best[0]:.3f}),免 VLM 定位")
            return best[1], best[2], best[0]
        self.misses += 1
        return None

    def find_all(self, img: Image.Image, target: str, threshold: float | None = None,
                 max_matches: int = 8, min_dist: float = 35.0) -> list[tuple[float, float, float]]:
        """找出 target 在畫面上的**所有**出現位置(多重比對 + 去重)。

        用於「一頁有多顆同款按鈕」的情況(交易行最底端好幾顆『兌換』)。
        灰色「已兌完」和亮色「兌換」外觀不同 → 不會被同一份 patch 匹配到,天然只點得到還能兌的。
        回 [(rx, ry, conf), ...],由上而下排序。沒學過這個 target 回 []。
        min_dist 是 0~1000 空間裡兩個匹配視為「不同顆」的最小距離(去掉同一顆的鄰近峰)。"""
        cands = self._index.get(_key(target))
        if not cands:
            return []
        canon = _canon(img)
        ch, cw = canon.shape[:2]
        thr = self.threshold if threshold is None else threshold
        raw: list[tuple[float, float, float]] = []  # (conf, rx, ry)
        for c in cands:
            patch = self._patch_img(c["patch"])
            if patch is None or patch.shape[0] > ch or patch.shape[1] > cw:
                continue
            res = cv2.matchTemplate(canon, patch, cv2.TM_CCOEFF_NORMED)
            ys, xs = np.where(res >= thr)
            for y, x in zip(ys.tolist(), xs.tolist()):
                raw.append((float(res[y, x]),
                            (x + c["offx"]) / cw * 1000.0, (y + c["offy"]) / ch * 1000.0))
        # 依信心高到低,做非極大值抑制:離已保留的點太近就丟(同一顆的重複峰)
        raw.sort(reverse=True)
        kept: list[tuple[float, float, float]] = []
        for conf, rx, ry in raw:
            if all((rx - kx) ** 2 + (ry - ky) ** 2 > min_dist ** 2 for _, kx, ky in kept):
                kept.append((conf, rx, ry))
            if len(kept) >= max_matches:
                break
        kept.sort(key=lambda t: t[2])  # 由上而下
        return [(rx, ry, conf) for conf, rx, ry in kept]

    # --- 學習(只在 VLM 定位且驗證有效後呼叫)---

    def store(self, img: Image.Image, target: str, rx: float, ry: float):
        """把 (rx, ry) 周圍剪一塊 patch 存起來。呼叫端要保證這個位置是驗證過正確的。"""
        canon = _canon(img)
        ch, cw = canon.shape[:2]
        cx = int(rx / 1000.0 * cw)
        cy = int(ry / 1000.0 * ch)
        hw = max(6, int(PATCH_HALF_W * cw))
        hh = max(6, int(PATCH_HALF_H * ch))
        x1, y1 = max(0, cx - hw), max(0, cy - hh)
        x2, y2 = min(cw, cx + hw), min(ch, cy + hh)
        patch = canon[y1:y2, x1:x2]
        if patch.size == 0 or float(patch.std()) < MIN_PATCH_STD:
            # 太平的區塊(純色背景)比對不可靠,不值得存
            return
        offx, offy = cx - x1, cy - y1  # 點擊點在 patch 內的位移
        key = _key(target)
        fname = f"{key}_{int(time.time()*1000) % 100000}.png"
        cv2.imwrite(str(self.dir / fname), patch)
        self._patches[fname] = patch
        entry = {"patch": fname, "offx": offx, "offy": offy,
                 "target": target, "created": time.strftime("%Y-%m-%d %H:%M:%S")}
        cands = self._index.setdefault(key, [])
        cands.append(entry)
        if len(cands) > MAX_CANDIDATES:  # 太多變體 → 丟掉最舊的
            old = cands.pop(0)
            try:
                (self.dir / old["patch"]).unlink(missing_ok=True)
            except Exception:
                pass
        self._save()
        self.learned += 1
        if self.verbose:
            print(f"        · 已把「{target}」的位置學進快取(patch {patch.shape[1]}×{patch.shape[0]})")

    def stats_line(self) -> str:
        total = self.hits + self.misses
        rate = (self.hits / total * 100) if total else 0.0
        return (f"快取:命中 {self.hits}/{total}({rate:.0f}%,省下 {self.hits} 次 VLM 定位),"
                f"新學 {self.learned} 份")
