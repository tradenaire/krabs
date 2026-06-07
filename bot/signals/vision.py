from __future__ import annotations

import base64
import json
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path

from bot.signals.model import ParsedSignal, TpTarget, default_tp_shares
from bot.signals.parser import SignalParseError


VISION_SYSTEM_PROMPT = """You extract trading signal variables from a screenshot.
Return JSON only. Do not do market analysis. Do not recommend trades.
If a field is uncertain, still return the best visible value and put the concern
in warning. Required variables: symbol, side, entry_min/entry_max or entry,
sl/stop, tps, leverage, confidence, warning."""


@dataclass(frozen=True)
class VisionSignalResult:
    signal: ParsedSignal
    warning: str = ""
    raw_json: dict | None = None


def _extract_json(raw: str) -> dict:
    text = (raw or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.IGNORECASE | re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise SignalParseError(f"Vision model did not return valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise SignalParseError("Vision model JSON must be an object.")
    return data


def _num(data: dict, *keys: str) -> float | None:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return float(str(value).replace(",", ".").replace("$", "").strip())
    return None


def _intish(value) -> int | None:
    if value in (None, ""):
        return None
    match = re.search(r"\d+", str(value))
    return int(match.group(0)) if match else None


def _entry_range(data: dict) -> tuple[float, float]:
    entry = data.get("entry")
    if isinstance(entry, list) and entry:
        values = [float(str(v).replace(",", ".").replace("$", "").strip()) for v in entry[:2]]
        if len(values) == 1:
            values.append(values[0])
        return min(values), max(values)
    if isinstance(entry, dict):
        values = [
            float(str(entry.get("min")).replace(",", ".").replace("$", "").strip()),
            float(str(entry.get("max", entry.get("min"))).replace(",", ".").replace("$", "").strip()),
        ]
        return min(values), max(values)
    first = _num(data, "entry_min", "entry_low", "entry_from")
    second = _num(data, "entry_max", "entry_high", "entry_to")
    if first is None:
        single = _num(data, "entry")
        if single is None:
            raise SignalParseError("Vision JSON is missing entry.")
        first = second = single
    if second is None:
        second = first
    return min(first, second), max(first, second)


def _tp_targets(data: dict) -> tuple[TpTarget, ...]:
    raw_tps = data.get("tps") or data.get("tp") or []
    prices: list[float] = []
    shares: list[float | None] = []
    if isinstance(raw_tps, dict):
        raw_tps = [raw_tps[key] for key in sorted(raw_tps)]
    for item in raw_tps:
        if isinstance(item, dict):
            price = item.get("price") or item.get("target")
            if price in (None, ""):
                continue
            prices.append(float(str(price).replace(",", ".").replace("$", "").strip()))
            share = item.get("share_pct") or item.get("share") or item.get("percent")
            shares.append(float(str(share).replace("%", "").strip()) if share not in (None, "") else None)
        else:
            prices.append(float(str(item).replace(",", ".").replace("$", "").strip()))
            shares.append(None)
    if not prices:
        raise SignalParseError("Vision JSON is missing TP targets.")
    defaults = default_tp_shares(len(prices))
    return tuple(
        TpTarget(price=price, share_pct=shares[idx] if shares[idx] is not None else defaults[idx])
        for idx, price in enumerate(prices)
    )


def parse_vision_signal_json(raw: str) -> VisionSignalResult:
    data = _extract_json(raw)
    entry_min, entry_max = _entry_range(data)
    stop = _num(data, "sl", "stop", "stop_loss")
    if stop is None:
        raise SignalParseError("Vision JSON is missing SL.")
    signal = ParsedSignal(
        symbol=str(data.get("symbol", "")).upper().replace("/USDT", "").replace("USDT", "").strip(),
        side=str(data.get("side", "")).lower(),
        entry_min=entry_min,
        entry_max=entry_max,
        stop=stop,
        tps=_tp_targets(data),
        leverage=_intish(data.get("leverage")),
        confidence=_intish(data.get("confidence")),
        source_text=json.dumps(data, ensure_ascii=False),
    )
    return VisionSignalResult(
        signal=signal.validate(),
        warning=str(data.get("warning") or "").strip(),
        raw_json=data,
    )


def build_signal_variables(signal: ParsedSignal, margin: float | None = None) -> dict:
    variables = {
        "symbol": signal.symbol,
        "side": signal.side,
        "order_side": signal.order_side,
        "close_side": signal.close_side,
        "entry_min": signal.entry_min,
        "entry_max": signal.entry_max,
        "leverage": signal.leverage,
        "take_profit_orders": [
            {"price": tp.price, "share_pct": tp.share_pct}
            for tp in signal.tps
        ],
        "stop_loss_order": {"price": signal.stop, "close_position": True},
    }
    if margin is not None:
        variables["margin"] = margin
    return variables


def _data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{payload}"


async def decode_signal_image(path: Path, api_key: str, model: str = "openai/gpt-5.5") -> VisionSignalResult:
    if not api_key:
        raise RuntimeError("openrouter_api_key is not configured.")
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key, base_url="https://openrouter.ai/api/v1")
    result = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": VISION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Extract the signal variables from this screenshot. "
                            "Return strict JSON only. Use warning for protection/uncertainty."
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": _data_url(path)}},
                ],
            },
        ],
        max_tokens=700,
        temperature=0,
        extra_body={
            "reasoning": {"exclude": True},
            "include_reasoning": False,
            "reasoning_effort": "none",
        },
    )
    content = result.choices[0].message.content or ""
    return parse_vision_signal_json(content)
