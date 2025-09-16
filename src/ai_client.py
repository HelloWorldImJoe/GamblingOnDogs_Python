"""AI 决策客户端

提供两种实现：
- OpenAICompatClient：通过 OpenAI 兼容的 /chat/completions 接口调用任意兼容服务；
- HeuristicAIClient：简单的动量启发式，适用于无 API Key 或干跑。

约定：
- 输入 K 线数据 candles 的 schema 为 OKX 历史 K 线格式：
    [timestamp, open, high, low, close, vol, volCcy, volCcyQuote, confirm, ...]
- decide_direction 返回 "long" 或 "short" 单词之一。
"""
from __future__ import annotations
import json
from typing import List, Literal
import httpx

Decision = Literal["long", "short"]

class AIClient:
    def decide_direction(self, inst_id: str, candles: List[List[str]]) -> Decision:
        raise NotImplementedError


class OpenAICompatClient(AIClient):
    def __init__(self, api_key: str, base_url: str, model: str):
        """构造 OpenAI 兼容客户端。

        参数：
            api_key: OpenAI 或兼容服务的 API Key；
            base_url: 兼容服务的 base URL，例如 https://api.openai.com/v1 或自建代理；
            model: 模型名称，比如 gpt-4o-mini 等。
        """
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model

    def _build_messages(self, inst_id: str, candles: List[List[str]]):
        """组装 Chat Completions 消息列表。

        仅要求模型输出一个单词：long 或 short。
        """
        sys = (
            "你是一个加密衍生品量化策略助理。给出下一根K线方向上的合约方向决策: long(做多) 或 short(做空)。"
            "请仅输出一个单词 long 或 short。考虑趋势、动量、波动率和最近的止盈/止损阈值影响。"
        )
        user = {
            "inst_id": inst_id,
            "candles_schema": "[timestamp, open, high, low, close, vol, volCcy, volCcyQuote, confirm, ...]",
            "recent_candles": candles[:120],
        }
        return [
            {"role": "system", "content": sys},
            {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
        ]

    def decide_direction(self, inst_id: str, candles: List[List[str]]) -> Decision:
        """请求 OpenAI 兼容服务获取 long/short 决策，失败时回退到启发式。"""
        messages = self._build_messages(inst_id, candles)
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": 4,
        }
        # 短超时，避免交易循环被长时间阻塞
        with httpx.Client(timeout=30) as client:
            r = client.post(url, headers=headers, json=payload)
            r.raise_for_status()
            data = r.json()
            content: str = data["choices"][0]["message"]["content"].strip().lower()
            if "long" in content and "short" not in content:
                return "long"
            if "short" in content and "long" not in content:
                return "short"
        # 回退：使用最近若干根收盘价的简单动量判断
        try:
            closes = [float(c[4]) for c in candles[:10]]
            if len(closes) >= 2 and closes[-1] > closes[0]:
                return "long"
        except Exception:
            pass
        return "short"


class HeuristicAIClient(AIClient):
    """简单动量启发式，用于干跑或无 API Key 情况。

    策略：取最近若干根收盘价，若上升则偏多，否则偏空。
    """

    def decide_direction(self, inst_id: str, candles: List[List[str]]) -> Decision:
        try:
            closes = [float(c[4]) for c in candles[:30]]
            if len(closes) >= 2 and closes[-1] > closes[0]:
                return "long"
        except Exception:
            pass
        return "short"
