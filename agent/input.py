# Copyright 2026 touma-tw
# SPDX-License-Identifier: Apache-2.0

"""滑鼠動作:移動真實游標 + 真實點擊(pyautogui)。

**為什麼非得動真實游標?** 實測這款遊戲讀的是 GetCursorPos(真實游標位置),
完全忽略 PostMessage 訊息裡的座標:
    游標放別處、訊息送「出航」 → 畫面差異 0.45(沒反應)
    游標放「出航」上、訊息送空白處 → 差異 63.07(出航被打開)
而且遊戲在背景時完全不處理輸入(前景 91 / 背景 0.00)。
兩者都是遊戲自己的設計,換任何注入方式都一樣 —— 背景操作不可行。

能做的是把干擾**壓到最小**:
  截圖用 PrintWindow,不需要焦點(見 capture.py)。戰鬥中連續數十次的 wait
  完全不碰你的滑鼠和焦點;只有真正要點的那一瞬間才會搶,而且事後把游標放回原處。
"""

import ctypes
import time

import pyautogui

from .capture import GameWindow

user32 = ctypes.windll.user32

pyautogui.FAILSAFE = True  # 滑鼠甩到螢幕左上角即拋例外中止(手動急停)
pyautogui.PAUSE = 0.05

# 捲動方向 → 拖曳的起點與終點(0~1000 相對座標)。
# key 是「想看的方向」,值是實際的手指動作 —— 兩者相反,因為拖曳是抓住內容拖動。
# 這個換算刻意放在 Python:模型連寫死規則都會把左右用反(實測)。
SCROLL_DRAGS = {
    "left": (250, 500, 900, 500),   # 想看左邊 → 內容往右拉
    "right": (900, 500, 250, 500),  # 想看右邊 → 內容往左拉
    "up": (500, 300, 500, 800),     # 想看上面 → 內容往下拉
    "down": (500, 800, 500, 300),   # 想看下面 → 內容往上拉
}


class _Borrow:
    """借用焦點與游標,動作做完就還回去。

    截圖不需要焦點,所以使用者只在「真正點擊的那一瞬間」被打斷;
    每步之間的推論(約 3~5 秒)和戰鬥中的連續等待,焦點都留在使用者手上。
    """

    def __init__(self, win: GameWindow):
        self.win = win

    def __enter__(self):
        p = ctypes.wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(p))
        self.cursor = (p.x, p.y)
        fg = user32.GetForegroundWindow()
        # 原本就是遊戲(或它的子視窗)的話,不必還
        self.prev_fg = fg if fg and not self.win.is_foreground else None
        self.win.focus()  # 遊戲在背景不吃輸入,非搶不可
        return self

    def __exit__(self, *exc):
        try:
            user32.SetCursorPos(*self.cursor)
        except Exception:
            pass
        if self.prev_fg and user32.IsWindow(self.prev_fg):
            time.sleep(0.15)  # 讓遊戲先處理完這次點擊
            # 按一下 Alt 解除 Windows 的前景視窗鎖,再把焦點還回去
            user32.keybd_event(0x12, 0, 0, 0)
            user32.keybd_event(0x12, 0, 2, 0)
            user32.SetForegroundWindow(self.prev_fg)
        return False


def click(win: GameWindow, rx: float, ry: float):
    with _Borrow(win):
        x, y = win.to_screen(rx, ry)
        pyautogui.moveTo(x, y, duration=0.15)
        pyautogui.click()
        time.sleep(0.05)


def drag(win: GameWindow, rx: float, ry: float, rx2: float, ry2: float):
    with _Borrow(win):
        x1, y1 = win.to_screen(rx, ry)
        x2, y2 = win.to_screen(rx2, ry2)
        pyautogui.moveTo(x1, y1, duration=0.15)
        pyautogui.mouseDown()
        time.sleep(0.15)
        pyautogui.moveTo(x2, y2, duration=0.6)  # 平滑移動,遊戲才吃得到拖曳
        time.sleep(0.15)
        pyautogui.mouseUp()


def scroll(win: GameWindow, direction: str):
    """direction 是「想看的方向」(left/right/up/down),實際拖曳方向由此換算。"""
    if direction not in SCROLL_DRAGS:
        raise ValueError(f"未知的捲動方向:{direction!r}")
    drag(win, *SCROLL_DRAGS[direction])
