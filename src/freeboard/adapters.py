from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .config import ZEN_BASE
from .discovery import excluded


class Completion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    usage: dict | None
    truncated: bool
    output_tokens: int | None
    reasoning_tokens: int | None


def payload(model: dict, messages: list[dict], cap: int) -> dict:
    if model["status"] != "eligible" or excluded(model["id"]):
        raise ValueError("Model lacks current free eligibility")
    endpoint = model["endpoint"]
    profile = model["effective_profile"]
    expected = {"chat": f"{ZEN_BASE}/chat/completions", "responses": f"{ZEN_BASE}/responses"}
    if endpoint != expected.get(profile["protocol"]):
        raise ValueError("Unsupported or untrusted generation endpoint")
    body = {"model": model["id"], "stream": False, profile["cap_parameter"]: cap}
    if profile.get("temperature") is not None:
        body["temperature"] = profile["temperature"]
    body["messages" if profile["protocol"] == "chat" else "input"] = messages
    return body


def parse(protocol: str, data: dict) -> Completion:
    usage = data.get("usage") or None
    if protocol == "chat":
        choices = data.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError("Expected one completion")
        message = choices[0].get("message", {})
        text = message.get("content") or message.get("refusal") or ""
        if not isinstance(text, str):
            raise ValueError("Non-text completion")
        output = (usage or {}).get("completion_tokens")
        reasoning = (usage or {}).get("completion_tokens_details", {}).get("reasoning_tokens")
        truncated = choices[0].get("finish_reason") == "length"
    elif protocol == "responses":
        text = "".join(part.get("text", part.get("refusal", ""))
                       for item in data.get("output", []) if item.get("type") == "message"
                       for part in item.get("content", [])
                       if part.get("type") in {"output_text", "refusal"})
        output = (usage or {}).get("output_tokens")
        reasoning = (usage or {}).get("output_tokens_details", {}).get("reasoning_tokens")
        truncated = data.get("incomplete_details", {}).get("reason") == "max_output_tokens" if data.get("incomplete_details") else False
        if data.get("status") not in {"completed", "incomplete"}:
            raise ValueError("Unexpected Responses status")
    else:
        raise ValueError("Unsupported protocol")
    return Completion(text=text, usage=usage, truncated=truncated,
                      output_tokens=output, reasoning_tokens=reasoning)

