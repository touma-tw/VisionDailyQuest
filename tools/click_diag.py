# Copyright 2026 touma-tw
# SPDX-License-Identifier: Apache-2.0

"""診斷:合成滑鼠事件是否能送達遊戲。

1. 找到遊戲視窗,印出 PID 與是否提權(提權程序會因 UIPI 擋掉低權限合成輸入)
2. 對「活動」按鈕(相對座標 705,220)做一次真實點擊,前後截圖存檔比較
"""

import ctypes
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.capture import GameWindow, set_dpi_aware
from agent import input as game_input


def check_elevation(pid: int):
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    TOKEN_QUERY = 0x8
    TokenElevation = 20
    kernel32 = ctypes.windll.kernel32
    advapi32 = ctypes.windll.advapi32

    hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not hproc:
        return "無法開啟程序(極可能已提權)"
    token = ctypes.c_void_p()
    if not advapi32.OpenProcessToken(hproc, TOKEN_QUERY, ctypes.byref(token)):
        return "無法開啟 token(極可能已提權)"
    elev = ctypes.c_int(0)
    retlen = ctypes.c_int(0)
    advapi32.GetTokenInformation(
        token, TokenElevation, ctypes.byref(elev), 4, ctypes.byref(retlen)
    )
    return "已提權 (elevated)" if elev.value else "未提權 (normal)"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    set_dpi_aware()
    win = GameWindow("鈴蘭之劍")
    hwnd = win.locate()
    print(f"視窗 hwnd={hwnd}, PID={win.pid}, 遊戲程序:{check_elevation(win.pid)}")
    print(f"用戶區(螢幕座標) left,top,w,h = {win.rect}")
    me = ctypes.windll.shell32.IsUserAnAdmin()
    print(f"本腳本:{'已提權' if me else '未提權'}")

    print("截圖…")
    before = win.screenshot()
    before.save("diag_before.png")
    print(f"截圖尺寸 {before.size} — 應不含標題列")
    if "--click" not in sys.argv:
        print("(加上 --click 才會實際點擊測試)")
        return
    print("點擊「活動」(705,220)…")
    game_input.click(win, 705, 220)
    time.sleep(2.0)
    after = win.screenshot()
    after.save("diag_after.png")
    print("完成,已存 diag_before.png / diag_after.png")


if __name__ == "__main__":
    main()
