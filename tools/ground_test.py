# Copyright 2026 touma-tw
# SPDX-License-Identifier: Apache-2.0

"""Grounding 精度驗證:把主畫面截圖丟給 VLM,要求回報 UI 元素座標,
將標記畫回圖片存檔,供人工目視檢查誤差。

用法:
    python tools/ground_test.py                    # 用 ref_home.png 參考圖
    python tools/ground_test.py --live             # 改用當下遊戲畫面(需遊戲開著)
    python tools/ground_test.py --model gemma4:26b-a4b-it-qat
輸出:
    ground_result_<model>.png  (標記圖)

注意:預期座標是對「用戶區」畫面(不含 Windows 標題列)量測的,
與 agent 實際送給模型的截圖同一座標系。soc1.png 含標題列,不適用此判準。
"""

import argparse
import base64
import json
import re
import sys
import time
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

OLLAMA_URL = "http://localhost:11434/api/chat"

# 要求模型定位的元素,及人工量測的預期位置(ref_home.png 用戶區像素,1561x878)
# 判準:誤差落在按鈕範圍內即可,約 ±35px
TARGETS = {
    "商店": (711, 788),
    "倉庫": (430, 785),
    "出航": (1310, 800),
    "郵件": (290, 141),
    "活動": (1100, 133),
    "角色": (289, 793),
    "告示板": (1470, 608),
}
TOLERANCE = 35

PROMPT = """這是一張遊戲《鈴蘭之劍》的畫面截圖。請找出以下 UI 元素在圖中的位置:
{targets}

以 JSON 陣列回答,每個元素一筆,座標使用 0~1000 的相對座標(x 為橫向、y 為縱向,左上角為 0,0、右下角為 1000,1000),格式:
[{{"name": "元素名", "x": 123, "y": 456}}]

只輸出 JSON,不要其他文字。找不到的元素請省略。"""


def query_model(model: str, image_path: Path, prompt: str, think: bool = False) -> tuple[str, float]:
    """回傳 (回應內容, 耗時秒數)。"""
    b64 = base64.b64encode(image_path.read_bytes()).decode()
    t0 = time.time()
    resp = requests.post(
        OLLAMA_URL,
        json={
            "model": model,
            "think": think,
            "messages": [{"role": "user", "content": prompt, "images": [b64]}],
            "stream": False,
            "options": {"temperature": 0},
            "keep_alive": "10m",
        },
        timeout=1800,
    )
    resp.raise_for_status()
    return resp.json()["message"]["content"], time.time() - t0


def parse_points(text: str) -> list[dict]:
    """抓出 JSON 陣列並正規化成 {name, x, y}。

    模型會依心情回不同格式:{"name","x","y"}、{"label","point":[x,y]}、
    或 Qwen VL 原生的 {"label","bbox_2d":[x1,y1,x2,y2]} —— 全部接受。
    """
    m = re.search(r"\[\s*\{.*\}\s*\]", text, re.DOTALL)
    if not m:
        raise ValueError(f"回應中找不到 JSON 陣列:\n{text}")

    out = []
    for raw in json.loads(m.group(0)):
        p = {"name": raw.get("name") or raw.get("label") or "?"}
        if "bbox_2d" in raw and len(raw["bbox_2d"]) == 4:
            x1, y1, x2, y2 = (float(v) for v in raw["bbox_2d"])
            p["x"], p["y"] = (x1 + x2) / 2, (y1 + y2) / 2  # 取框中心
        elif "point" in raw and len(raw["point"]) == 2:
            p["x"], p["y"] = (float(v) for v in raw["point"])
        elif "x" in raw and "y" in raw:
            p["x"], p["y"] = float(raw["x"]), float(raw["y"])
        else:
            continue
        out.append(p)
    if not out:
        raise ValueError(f"陣列裡沒有可用的座標:\n{text[:400]}")
    return out


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen3.6:35b")
    ap.add_argument("--image", default="ref_home.png")
    ap.add_argument("--live", action="store_true", help="改用當下遊戲畫面(需遊戲開著)")
    ap.add_argument("--think", action="store_true", help="開啟思考模式(較慢,但定位可能較準)")
    args = ap.parse_args()

    image_path = Path(args.image)
    if args.live:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from agent.capture import GameWindow, set_dpi_aware

        set_dpi_aware()
        image_path = Path("ground_live.png")
        GameWindow("鈴蘭之劍").screenshot().save(image_path)
        print(f"已擷取當下畫面 → {image_path}")

    img = Image.open(image_path).convert("RGB")
    w, h = img.size
    print(f"圖片尺寸: {w}x{h},模型: {args.model}")

    prompt = PROMPT.format(targets="、".join(TARGETS))
    print(f"查詢模型中…(思考模式:{'開' if args.think else '關'})")
    raw, elapsed = query_model(args.model, image_path, prompt, args.think)
    print("--- 模型原始回應 ---")
    print(raw[:1500])
    print(f"-------------------- 耗時 {elapsed:.1f}s")

    points = parse_points(raw)

    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/msjh.ttc", 22)
    except OSError:
        font = ImageFont.load_default()

    print(f"{'元素':<8}{'模型(px)':<16}{'預期(px)':<16}{'誤差(px)':<10}")
    for p in points:
        name = p.get("name", "?")
        x = p["x"] * w / 1000
        y = p["y"] * h / 1000
        r = 12
        draw.ellipse([x - r, y - r, x + r, y + r], outline="red", width=3)
        draw.line([x - r * 2, y, x + r * 2, y], fill="red", width=2)
        draw.line([x, y - r * 2, x, y + r * 2], fill="red", width=2)
        draw.text((x + r + 4, y - r - 4), name, fill="red", font=font)

        if name in TARGETS and not args.live:
            ex, ey = TARGETS[name]
            err = ((x - ex) ** 2 + (y - ey) ** 2) ** 0.5
            flag = "✓" if err <= TOLERANCE else "✗"
            print(f"{name:<8}({x:5.0f},{y:5.0f})   ({ex:4d},{ey:4d})     {err:6.1f} {flag}")
        else:
            print(f"{name:<8}({x:5.0f},{y:5.0f})   (未知)")

    out = image_path.with_name(f"ground_result_{args.model.replace(':', '_').replace('/', '_')}.png")
    img.save(out)
    print(f"\n標記圖已存至: {out}")


if __name__ == "__main__":
    main()
