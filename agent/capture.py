"""遊戲視窗定位與截圖。

只處理「用戶區」(client area) —— 不含 Windows 標題列與邊框。
這很重要:若截圖含標題列,模型會把視窗本身的 ✕ 當成遊戲內的關閉鈕而關掉遊戲。
座標系統因此一律以用戶區左上角為原點。

截圖用 PrintWindow(要求視窗自己畫一份給我們)而不是抓螢幕像素,原因有二:
  1. **不需要焦點** —— 大部分步驟(尤其戰鬥中的連續 wait)因此完全不干擾使用者。
  2. 被其他視窗蓋住時也拿得到正確畫面(舊版曾拍到 Windows Task View)。
實測 PrintWindow 裁切後與抓螢幕的結果差異為 0.00,可以直接替換。

注意:輸入仍然需要焦點 —— 實測這款遊戲在背景時完全不處理 PostMessage
(同座標同送法:前景畫面差異 91,背景 0.00)。那是遊戲自己的設計,繞不過去。
"""

import ctypes
import ctypes.wintypes as wt
import io
import time

import pygetwindow as gw
from PIL import Image

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
    ]


def set_dpi_aware():
    """程式啟動時呼叫一次,避免 Windows 顯示縮放造成座標錯位。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        user32.SetProcessDPIAware()


class GameWindow:
    def __init__(self, title: str):
        self.title = title
        self._hwnd = None

    # --- 視窗定位 ---

    def locate(self) -> int:
        wins = [w for w in gw.getWindowsWithTitle(self.title) if w.title == self.title]
        if not wins:
            raise RuntimeError(f"找不到標題為「{self.title}」的視窗,請先開啟遊戲")
        self._hwnd = wins[0]._hWnd
        return self._hwnd

    @property
    def hwnd(self) -> int:
        """取得有效 hwnd;若視窗已關閉/重開則重新尋找。"""
        if self._hwnd is None or not user32.IsWindow(self._hwnd):
            return self.locate()
        return self._hwnd

    @property
    def pid(self) -> int:
        p = ctypes.c_uint(0)
        user32.GetWindowThreadProcessId(self.hwnd, ctypes.byref(p))
        return p.value

    @property
    def is_foreground(self) -> bool:
        fg = user32.GetForegroundWindow()
        hwnd = self.hwnd
        return fg == hwnd or _same_process(fg, hwnd)

    def focus(self, tries: int = 4):
        """把遊戲帶到前景。**只有要送輸入前才需要呼叫** —— 截圖不需要焦點。

        這款遊戲在背景時完全不處理輸入(實測),所以點擊前非搶焦點不可。
        """
        hwnd = self.hwnd
        for _ in range(tries):
            if self.is_foreground:
                time.sleep(0.2)
                return
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            # 按一下 Alt 解除 Windows 的前景視窗鎖,再要求前景
            user32.keybd_event(0x12, 0, 0, 0)
            user32.keybd_event(0x12, 0, 2, 0)
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.4)
        if not self.is_foreground:
            raise RuntimeError(
                f"無法將「{self.title}」視窗帶到前景(前景 hwnd={user32.GetForegroundWindow()})。"
                "請確認遊戲未被其他全螢幕程式遮蔽,且本腳本與遊戲權限相同。"
            )

    # --- 座標與截圖(用戶區) ---

    @property
    def rect(self) -> tuple[int, int, int, int]:
        """用戶區在螢幕上的 (left, top, width, height),不含標題列與邊框。"""
        hwnd = self.hwnd
        r = wt.RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(r)):
            raise RuntimeError("GetClientRect 失敗")
        pt = wt.POINT(0, 0)
        if not user32.ClientToScreen(hwnd, ctypes.byref(pt)):
            raise RuntimeError("ClientToScreen 失敗")
        return pt.x, pt.y, r.right, r.bottom

    def screenshot(self) -> Image.Image:
        """擷取用戶區。**不需要焦點、不干擾使用者、被遮住也拿得到。**"""
        hwnd = self.hwnd
        wr = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(wr))
        ww, wh = wr.right - wr.left, wr.bottom - wr.top
        _, _, cw, ch = self.rect
        pt = wt.POINT(0, 0)
        user32.ClientToScreen(hwnd, ctypes.byref(pt))
        off_x, off_y = pt.x - wr.left, pt.y - wr.top  # 用戶區在整個視窗內的位移

        hdc = user32.GetDC(0)
        mem_dc = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, ww, wh)
        old = gdi32.SelectObject(mem_dc, bmp)
        try:
            # PrintWindow 畫的是「整個視窗」(含標題列),所以下面要裁成用戶區。
            # 旗標 2 = PW_RENDERFULLCONTENT,DWM 合成的視窗需要它才畫得出內容。
            if not user32.PrintWindow(hwnd, mem_dc, 2):
                raise RuntimeError("PrintWindow 失敗,無法擷取遊戲畫面")
            bi = _BITMAPINFOHEADER()
            bi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
            bi.biWidth, bi.biHeight = ww, -wh  # 負高度 = 由上而下
            bi.biPlanes, bi.biBitCount, bi.biCompression = 1, 32, 0
            buf = ctypes.create_string_buffer(ww * wh * 4)
            gdi32.GetDIBits(mem_dc, bmp, 0, wh, buf, ctypes.byref(bi), 0)
            full = Image.frombuffer("RGB", (ww, wh), buf, "raw", "BGRX", 0, 1)
            return full.crop((off_x, off_y, off_x + cw, off_y + ch))
        finally:
            gdi32.SelectObject(mem_dc, old)
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(0, hdc)

    def to_client(self, rx: float, ry: float) -> tuple[int, int]:
        """0~1000 相對座標 → 用戶區像素座標。"""
        _, _, width, height = self.rect
        rx = min(max(rx, 0), 1000)
        ry = min(max(ry, 0), 1000)
        x = int(rx * width / 1000)
        y = int(ry * height / 1000)
        # 夾在用戶區內、且離邊緣至少 margin 像素。真正的按鈕不會貼在 1px 邊上,
        # 但定位偶爾會回極端值(如 0,0);若剛好視窗貼著螢幕角落,游標移過去會誤觸
        # pyautogui 的 fail-safe(移到螢幕角落即中止)導致整輪崩潰。留白就能避開。
        margin = 4
        x = min(max(x, margin), width - 1 - margin)
        y = min(max(y, margin), height - 1 - margin)
        return x, y

    def to_screen(self, rx: float, ry: float) -> tuple[int, int]:
        """0~1000 用戶區相對座標 → 螢幕絕對座標(夾在用戶區內,避免點到視窗邊框)。"""
        left, top, _, _ = self.rect
        x, y = self.to_client(rx, ry)
        return left + x, top + y


def _same_process(hwnd_a: int, hwnd_b: int) -> bool:
    """前景是否為同一程序的視窗(遊戲彈出子視窗時仍算已聚焦)。"""
    if not hwnd_a or not hwnd_b:
        return False
    pa, pb = ctypes.c_uint(0), ctypes.c_uint(0)
    user32.GetWindowThreadProcessId(hwnd_a, ctypes.byref(pa))
    user32.GetWindowThreadProcessId(hwnd_b, ctypes.byref(pb))
    return pa.value == pb.value and pa.value != 0


def png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
