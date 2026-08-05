# Copyright 2026 touma-tw
# SPDX-License-Identifier: Apache-2.0

"""嚴謹版:遊戲**不在前景**時,PostMessage 還吃不吃?

前一版的瑕疵是測試當下遊戲仍在前景,證明不了背景操作。
這一版會主動把記事本切到前景,再送訊息給遊戲。

順便修好 PrintWindow 的裁切:PrintWindow 畫的是整個視窗(含標題列),
要自己裁成用戶區才能和現有的座標系統對齊。

用法:遊戲請停在「名片」頁(前一個測試打開的),本測試會點返回箭頭關掉它。
    python tools/bg_probe2.py
"""

import ctypes
import ctypes.wintypes as wt
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from PIL import Image, ImageChops

from agent.capture import GameWindow, set_dpi_aware

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MOUSEMOVE = 0x0201, 0x0202, 0x0200
MK_LBUTTON = 0x0001


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
    ]


def print_window_client(hwnd: int) -> Image.Image | None:
    """PrintWindow 整個視窗,再裁成用戶區(與現有座標系統一致)。"""
    wr = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(wr))
    ww, wh = wr.right - wr.left, wr.bottom - wr.top

    cr = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(cr))
    cw, ch = cr.right, cr.bottom

    pt = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    off_x, off_y = pt.x - wr.left, pt.y - wr.top  # 用戶區在視窗內的位移

    hdc = user32.GetDC(0)
    mem_dc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, ww, wh)
    gdi32.SelectObject(mem_dc, bmp)
    ok = user32.PrintWindow(hwnd, mem_dc, 2)  # 2 = PW_RENDERFULLCONTENT

    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth, bi.biHeight = ww, -wh
    bi.biPlanes, bi.biBitCount, bi.biCompression = 1, 32, 0
    buf = ctypes.create_string_buffer(ww * wh * 4)
    gdi32.GetDIBits(mem_dc, bmp, 0, wh, buf, ctypes.byref(bi), 0)

    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem_dc)
    user32.ReleaseDC(0, hdc)
    if not ok:
        return None
    full = Image.frombuffer("RGB", (ww, wh), buf, "raw", "BGRX", 0, 1)
    return full.crop((off_x, off_y, off_x + cw, off_y + ch))


def post_click(hwnd: int, x: int, y: int):
    lp = (y << 16) | (x & 0xFFFF)
    user32.PostMessageW(hwnd, WM_MOUSEMOVE, 0, lp)
    time.sleep(0.05)
    user32.PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lp)
    time.sleep(0.08)
    user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, lp)


def diff(a, b) -> float:
    if a.size != b.size:
        return 999.0
    h = ImageChops.difference(a.convert("L"), b.convert("L")).histogram()
    return sum(i * c for i, c in enumerate(h)) / sum(h)


def main():
    set_dpi_aware()
    win = GameWindow("鈴蘭之劍")
    hwnd = win.locate()
    _, _, w, h = win.rect
    print(f"遊戲 hwnd={hwnd}, 用戶區 {w}x{h}")

    # --- PrintWindow 裁切是否與 mss 對齊 ---
    print("\n=== 測試 1:PrintWindow 裁切後與 mss 是否一致 ===")
    win.focus()
    time.sleep(0.5)
    pw = print_window_client(hwnd)
    ms = win.screenshot()
    print(f"  PrintWindow {pw.size} / mss {ms.size}  平均差異 {diff(pw, ms):.2f}", end="  ")
    print("→ ✓ 對齊" if diff(pw, ms) < 15 else "→ ✗ 仍未對齊")
    pw.save("bg2_printwindow_client.png")

    # --- 把記事本推到前景,遊戲退居背景 ---
    print("\n=== 測試 2:遊戲退到背景後,PostMessage 還吃嗎? ===")
    np_proc = subprocess.Popen(["notepad.exe"])
    time.sleep(1.5)
    fg = user32.GetForegroundWindow()
    print(f"  前景視窗 hwnd={fg},遊戲 hwnd={hwnd} → " +
          ("✓ 遊戲確實不在前景" if fg != hwnd else "✗ 遊戲仍在前景,測試無效"))

    before = print_window_client(hwnd)
    before.save("bg2_before.png")
    print("  送出點擊(左上角返回箭頭)…")
    post_click(hwnd, int(w * 0.06), int(h * 0.05))
    time.sleep(2.0)

    fg2 = user32.GetForegroundWindow()
    after = print_window_client(hwnd)
    after.save("bg2_after.png")
    d = diff(before, after)
    print(f"  點擊後前景仍是 hwnd={fg2} → " +
          ("✓ 焦點沒有被搶走" if fg2 != hwnd else "✗ 焦點被搶了"))
    print(f"  畫面差異 {d:.2f}", end="  ")
    print("→ ✓ 遊戲在背景仍然吃 PostMessage!" if d > 15 else "→ ✗ 背景時沒反應")

    np_proc.terminate()
    print("\n請看 bg2_before.png / bg2_after.png 確認畫面真的變了")


if __name__ == "__main__":
    main()
