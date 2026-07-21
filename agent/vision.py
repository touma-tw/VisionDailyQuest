"""VLM provider 抽象層 —— 只做 agentic pilot 需要的兩件事:

    locate()  截圖 + 目標描述 → bbox_2d → 中心座標(0~1000 相對)
    read()    截圖 + 問題 → 一句話答案(讀畫面上某個數值/狀態)

**決策與定位分開**是本專案最重要的架構決定:模型不輸出座標(同時思考又算座標,思考會把
定位擠掉 —— 實測想點「代行」卻點到 265px 外的「出擊」)。pilot 只讓模型輸出「要點什麼」的
描述,再用專用的 locate() 拿座標。read() 同理:專注讀一個值,比在長對話裡順便看準。

provider 可抽換:OllamaProvider(本地,預設)與 GeminiProvider(雲端後備,grounding 更強)
同介面。pilot 目前直接用 OllamaProvider;要切 Gemini 就換成 GeminiProvider(config.yaml 的
gemini 區塊提供 api key 與 model)。
"""

import base64
import json
import re

import requests

WEEKDAYS = "一二三四五六日"


GROUND_PROMPT = """這是遊戲《鈴蘭之劍》的畫面截圖。請找出畫面上的:{target}

只輸出一個 JSON 物件,不要其他文字:
{{"bbox_2d": [x1, y1, x2, y2]}}
座標用 0~1000 的相對座標(畫面左上角為 0,0;右下角為 1000,1000)。
如果畫面上確實沒有這個東西,就輸出 {{"found": false}}。"""


FACT_PROMPT = """請看這張遊戲《鈴蘭之劍》的截圖,只回答這一個問題,不要做其他判斷:

{question}

用一句話回答。看不到就直接說看不到,不要猜。"""


def _center(box) -> tuple[float, float]:
    x1, y1, x2, y2 = (float(v) for v in box)
    return (x1 + x2) / 2, (y1 + y2) / 2


def parse_bbox(text: str) -> tuple[float, float] | None:
    """解析定位呼叫的回應 → 中心點;模型說找不到或無法解析則回 None。

    模型的原生 grounding 格式是 bbox_2d,但它也可能回 point 或 x/y,全部接受。
    """
    for raw in re.findall(r"\{.*?\}", text, re.DOTALL):
        try:
            o = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(o, dict):
            continue
        if o.get("found") is False:
            return None
        for key in ("bbox_2d", "bbox", "box"):
            v = o.get(key)
            if isinstance(v, (list, tuple)) and len(v) == 4:
                return _center(v)
        v = o.get("point")
        if isinstance(v, (list, tuple)) and len(v) == 2:
            return float(v[0]), float(v[1])
        if o.get("x") is not None and o.get("y") is not None:
            try:
                return float(o["x"]), float(o["y"])
            except (TypeError, ValueError):
                pass
    return None


class OllamaProvider:
    def __init__(self, cfg: dict):
        self.url = cfg["url"].rstrip("/") + "/api/chat"
        self.model = cfg["model"]
        self.think = cfg.get("think", False)
        # 定位/讀值必須和 pilot 決策對話用**同一組載入參數**(num_ctx、keep_alive),
        # 否則 ollama 會把同一個模型當成不同 runner,在兩者之間反覆卸載/重載(VRAM 每幾秒
        # 從滿載掉到基線再爬回來)。pilot 建構時會把它的 num_ctx/keep_alive 傳進來對齊。
        self.num_ctx = cfg.get("num_ctx")
        self.keep_alive = cfg.get("keep_alive")

    def _ask(self, image_png: bytes, prompt: str, system: str | None = None) -> str:
        msgs = ([{"role": "system", "content": system}] if system else []) + [
            {
                "role": "user",
                "content": prompt,
                "images": [base64.b64encode(image_png).decode()],
            }
        ]
        options = {"temperature": 0}
        if self.num_ctx is not None:
            options["num_ctx"] = self.num_ctx
        body = {
            "model": self.model,
            "think": self.think,
            "messages": msgs,
            "stream": False,
            "options": options,
        }
        if self.keep_alive is not None:
            body["keep_alive"] = self.keep_alive
        resp = requests.post(self.url, json=body, timeout=600)
        resp.raise_for_status()
        return resp.json()["message"]["content"]

    def locate(self, image_png: bytes, target: str) -> tuple[float, float] | None:
        """只做定位、不做決策 —— 這是模型最強的模式,誤差實測 4~30px。"""
        return parse_bbox(self._ask(image_png, GROUND_PROMPT.format(target=target)))

    def read(self, image_png: bytes, question: str) -> str:
        """只讀一個值、不做決策。同樣是「一次只做一件事」才準:
        決策迴圈裡它把同一張圖的「高額獎勵」在 0/1 之間反覆橫跳;
        專用呼叫問 5 次,5 次都是 1。"""
        return self._ask(image_png, FACT_PROMPT.format(question=question)).strip()


class GeminiProvider:
    """雲端後備(grounding 更強)。介面與 OllamaProvider 相同,pilot 需要時可直接換用。"""

    def __init__(self, cfg: dict):
        import os

        from google import genai  # 延遲載入,未安裝時不影響 ollama 路徑

        key = os.environ.get(cfg["api_key_env"], "")
        if not key:
            raise RuntimeError(f"環境變數 {cfg['api_key_env']} 未設定 Gemini API key")
        self.client = genai.Client(api_key=key)
        self.model = cfg["model"]

    def _ask(self, image_png: bytes, prompt: str, system: str | None = None) -> str:
        from google.genai import types

        resp = self.client.models.generate_content(
            model=self.model,
            contents=[
                types.Part.from_bytes(data=image_png, mime_type="image/png"),
                prompt,
            ],
            config=types.GenerateContentConfig(system_instruction=system, temperature=0),
        )
        return resp.text

    def locate(self, image_png: bytes, target: str) -> tuple[float, float] | None:
        return parse_bbox(self._ask(image_png, GROUND_PROMPT.format(target=target)))

    def read(self, image_png: bytes, question: str) -> str:
        return self._ask(image_png, FACT_PROMPT.format(question=question)).strip()
