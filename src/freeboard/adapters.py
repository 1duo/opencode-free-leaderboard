from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .config import ZEN_BASE
from .discovery import excluded


class Completion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    usage: dict | None
    truncated: bool
    output_tokens: int | None = Field(ge=0, strict=True)
    reasoning_tokens: int | None = Field(ge=0, strict=True)

    def bounded(self, cap: int) -> bool:
        return (self.output_tokens is not None and self.output_tokens <= cap and
                (self.reasoning_tokens is None or self.reasoning_tokens <= self.output_tokens))


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
    if not isinstance(data, dict):
        raise ValueError('Non-object completion response')
    usage = data.get("usage") or None
    if usage is not None and not isinstance(usage, dict):
        raise ValueError('Non-object usage')
    if protocol == "chat":
        choices = data.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError("Expected one completion")
        if not isinstance(choices[0], dict) or not isinstance(choices[0].get('message'), dict):
            raise ValueError('Malformed chat message')
        message = choices[0]['message']
        text = message.get("content") or message.get("refusal") or ""
        if not isinstance(text, str):
            raise ValueError("Non-text completion")
        output = (usage or {}).get("completion_tokens")
        details = (usage or {}).get('completion_tokens_details') or {}
        if not isinstance(details, dict):
            raise ValueError('Malformed token details')
        reasoning = details.get('reasoning_tokens')
        truncated = choices[0].get("finish_reason") == "length"
    elif protocol == "responses":
        outputs = data.get('output', [])
        if not isinstance(outputs, list) or any(not isinstance(i, dict) or not isinstance(i.get('content', []), list)
                or any(not isinstance(p, dict) for p in i.get('content', [])) for i in outputs):
            raise ValueError('Malformed Responses output')
        text = "".join(part.get("text", part.get("refusal", ""))
                       for item in data.get("output", []) if item.get("type") == "message"
                       for part in item.get("content", [])
                       if part.get("type") in {"output_text", "refusal"})
        output = (usage or {}).get("output_tokens")
        details = (usage or {}).get('output_tokens_details') or {}
        if not isinstance(details, dict):
            raise ValueError('Malformed token details')
        reasoning = details.get('reasoning_tokens')
        truncated = data.get("incomplete_details", {}).get("reason") == "max_output_tokens" if data.get("incomplete_details") else False
        if data.get("status") not in {"completed", "incomplete"}:
            raise ValueError("Unexpected Responses status")
    elif protocol == 'opencode':
        # Read-only recovery of archived responses; no OpenCode generation path exists.
        events = data['events']
        if not isinstance(events, list) or any(not isinstance(e, dict) for e in events):
            raise ValueError('Malformed native event stream')
        finishes = [e['part'] for e in events if e.get('type') == 'step_finish']
        if len(finishes) != 1 or any(e.get('type') in {'tool_use', 'error'} for e in events):
            raise ValueError('Native pilot requires exactly one completion and no tools')
        finish = finishes[0]
        if not isinstance(finish, dict):
            raise ValueError('Malformed native completion')
        if type(finish.get('cost')) not in {int, float} or finish['cost'] != 0:
            raise ValueError('Native pilot did not report zero cost')
        parts = [e.get('part') for e in events if e.get('type') == 'text']
        if any(not isinstance(p, dict) or not isinstance(p.get('text'), str) for p in parts):
            raise ValueError('Malformed native text')
        text = ''.join(e['part']['text'] for e in events if e.get('type') == 'text'
                       and e['part'].get('messageID') == finish.get('messageID'))
        tokens = finish['tokens']
        if not isinstance(tokens, dict) or not isinstance(tokens.get('cache', {}), dict):
            raise ValueError('Malformed native token accounting')
        values = [tokens.get(k) for k in ['input', 'output', 'reasoning']]
        cache = tokens.get('cache', {})
        values += [cache.get('read', 0), cache.get('write', 0)]
        if any(type(v) is not int or v < 0 for v in values):
            raise ValueError('Native token accounting unavailable')
        # OpenCode separates visible output, reasoning, and cached input.
        reasoning = tokens['reasoning']
        output = tokens['output'] + reasoning
        usage = {'prompt_tokens': tokens['input'] + cache.get('read', 0) + cache.get('write', 0),
                 'completion_tokens': output, 'total_tokens': tokens.get('total', sum(values)),
                 'source': 'opencode-normalized'}
        truncated = finish['reason'] == 'length'
        if finish['reason'] not in {'stop', 'length'}:
            raise ValueError('Unexpected native completion reason')
    else:
        raise ValueError("Unsupported protocol")
    return Completion(text=text, usage=usage, truncated=truncated,
                      output_tokens=output, reasoning_tokens=reasoning)
