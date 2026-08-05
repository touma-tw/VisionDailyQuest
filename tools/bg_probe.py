# Copyright 2026 touma-tw
# SPDX-License-Identifier: Apache-2.0

"""探測這款遊戲能不能「背景操作」—— 不搶焦點、不動真實游標。

兩件事分開測,結果可能不對稱:
  1. 截圖:PrintWindow(要求視窗自己畫一份給我們)能否在被遮住時仍拍到正確畫面
  2. 點擊:PostMessage(直接送滑鼠訊息給視窗)遊戲吃不吃

用法(遊戲請停在大廳):
    python tools/bg_probe.py
測試會點左上角玩家頭像(打開名片頁),再點返回箭頭關掉,不影響任何資源。
"""

import ctypes
import ctypes.wintypes as wt
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


def _lparam(x: int, y: int) -> int:
    return (y << 16) | (x & 0xFFFF)


def print_window(hwnd: int) -> Image.Image | None:
    """要求視窗把自己畫進我們的 DC —— 被遮住也能拿到內容(理論上)。"""
    r = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    w, h = r.right, r.bottom
    if w <= 0 or h <= 0:
        return None

    hdc = user32.GetDC(hwnd)
    mem_dc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mem_dc, bmp)

    # 2 = PW_RENDERFULLCONTENT,DWM 合成的視窗需要它
    ok = user32.PrintWindow(hwnd, mem_dc, 2)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
            ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
            ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
            ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
        ]

    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth, bi.biHeight = w, -h  # 負高度 = 由上而下
    bi.biPlanes, bi.biBitCount, bi.biCompression = 1, 32, 0

    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mem_dc, bmp, 0, h, buf, ctypes.byref(bi), 0)

    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem_dc)
    user32.ReleaseDC(hwnd, hdc)
    if not ok:
        return None
    return Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)


def post_click(hwnd: int, x: int, y: int):
    """把滑鼠訊息直接送給視窗 —— 不動真實游標、不搶焦點。"""
    lp = _lparam(x, y)
    user32.PostMessageW(hwnd, WM_MOUSEMOVE, 0, lp)
    time.sleep(0.05)
    user32.PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lp)
    time.sleep(0.08)
    user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, lp)


def diff(a: Image.Image, b: Image.Image) -> float:
    if a.size != b.size:
        return 999.0
    h = ImageChops.difference(a.convert("L"), b.convert("L")).histogram()
    return sum(i * c for i, c in enumerate(h)) / sum(h)


def main():
    set_dpi_aware()
    win = GameWindow("鈴蘭之劍")
    hwnd = win.locate()
    _, _, w, h = win.rect
    print(f"遊戲視窗 hwnd={hwnd}, 用戶區 {w}x{h}\n")

    # --- 1. PrintWindow 能不能拍到東西 ---
    print("=== 測試 1:PrintWindow 截圖 ===")
    pw = print_window(hwnd)
    if pw is None:
        print("  ✗ PrintWindow 失敗(回傳 0 或無效尺寸)")
        return
    pw.save("bg_printwindow.png")
    print(f"  PrintWindow 取得 {pw.size} → 存成 bg_printwindow.png")

    win.focus()
    mss_img = win.screenshot()
    mss_img.save("bg_mss.png")
    d = diff(pw, mss_img)
    print(f"  與 mss 截圖的平均差異: {d:.2f}", end="  ")
    print("→ ✓ 內容一致" if d < 15 else "→ ✗ 內容不同(PrintWindow 可能拿到黑畫面)")

    # --- 2. PostMessage 遊戲吃不吃 ---
    print("\n=== 測試 2:PostMessage 點擊(不搶焦點) ===")
    print("  3 秒後點擊左上角玩家頭像。請在這段時間內把別的視窗切到最前面,")
    print("  這樣才能驗證『遊戲不在前景也能操作』。")
    for i in range(3, 0, -1):
        print(f"    {i}…", end="\r")
        time.sleep(1)

    fg = user32.GetForegroundWindow()
    print(f"  目前前景視窗 hwnd={fg}{'(就是遊戲本身,測不出背景效果)' if fg == hwnd else '(不是遊戲 → 真正的背景測試)'}")

    before = print_window(hwnd)
    post_click(hwnd, int(w * 0.05), int(h * 0.10))  # 左上角頭像
    time.sleep(2.0)
    after = print_window(hwnd)
    d2 = diff(before, after)
    print(f"  點擊前後畫面差異: {d2:.2f}", end="  ")
    if d2 > 15:
        print("→ ✓ 遊戲有反應,PostMessage 可用!")
        after.save("bg_after_click.png")
    else:
        print("→ ✗ 遊戲沒反應,PostMessage 無效(這款遊戲應該是用 RawInput/DirectInput)")
    print("\n(畫面若被打開,請自行按返回關閉)")


if __name__ == "__main__":
    main()
