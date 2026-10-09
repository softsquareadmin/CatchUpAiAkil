"""Thin OpenRouter chat-completions client (see docs/provider-notes.md)."""
import time

import httpx

URL = "https://openrouter.ai/api/v1/chat/completions"
DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"


class ProviderError(RuntimeError):
    """kind: rate_limited | server | timeout | auth | bad_request | other (M10a). Only the first three are retried."""

    def __init__(self, message: str, kind: str = "other", status: int | None = None, retry_after_s: float | None = None):
        super().__init__(message)
        self.kind, self.status, self.retry_after_s = kind, status, retry_after_s


MESSAGES = {"auth": "OpenRouter rejected the key or the account (check OPENROUTER_API_KEY and credits)",
            "bad_request": "OpenRouter rejected the request as invalid"}


def classify(status: int | None) -> str:
    if status == 429:
        return "rate_limited"
    if status in (401, 402, 403):
        return "auth"
    if status in (400, 404, 413, 422):
        return "bad_request"
    if status in (408, 504):
        return "timeout"
    if status is not None and status >= 500:
        return "server"
    return "other"


def retry_after(headers) -> float | None:
    v = (headers or {}).get("retry-after")
    try:
        return max(0.0, float(v)) if v is not None else None
    except ValueError:
        return None  # an HTTP date: fall back to backoff


def error_from_response(r: httpx.Response, what: str) -> ProviderError:
    kind = classify(r.status_code)
    lead = MESSAGES.get(kind, f"OpenRouter{what} HTTP {r.status_code}")
    return ProviderError(f"{lead}: HTTP {r.status_code}: {r.text[:300]}", kind, r.status_code, retry_after(r.headers))


def error_from_body(err, what: str) -> ProviderError:
    """OpenRouter can answer 200 with an error object (for example an upstream 429)."""
    code = err.get("code") if isinstance(err, dict) else None
    status = code if isinstance(code, int) else None
    return ProviderError(f"OpenRouter{what} error: {str(err)[:300]}", classify(status), status)


def error_from_transport(e: httpx.HTTPError, what: str) -> ProviderError:
    kind = "timeout" if isinstance(e, httpx.TimeoutException) else "server"
    return ProviderError(f"OpenRouter{what} request failed: {e!r}", kind)


class OpenRouterClient:
    def __init__(self, api_key: str, timeout_s: float = 30.0, transport: httpx.AsyncBaseTransport | None = None):
        self._http = httpx.AsyncClient(timeout=timeout_s, transport=transport,
                                       headers={"Authorization": f"Bearer {api_key}"})

    async def chat_json(self, model: str, messages: list[dict], schema_name: str, schema: dict,
                        reasoning_effort: str | None = None, max_tokens: int = 2000,
                        temperature: float | None = None) -> dict:
        """Return {content, input_tokens, output_tokens, cost_usd (or None), latency_ms}."""
        body = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": schema_name, "strict": True, "schema": schema}},
            "provider": {"require_parameters": True},
        }
        if reasoning_effort:
            body["reasoning"] = {"effort": reasoning_effort}
        if temperature is not None:
            body["temperature"] = temperature
        t0 = time.monotonic()
        try:
            r = await self._http.post(URL, json=body)
        except httpx.HTTPError as e:
            raise error_from_transport(e, "") from None
        latency_ms = int((time.monotonic() - t0) * 1000)
        if r.status_code != 200:
            raise error_from_response(r, "")
        data = r.json()
        if "error" in data:
            raise error_from_body(data["error"], "")
        usage = data.get("usage") or {}
        return {
            "content": data["choices"][0]["message"].get("content") or "",
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "cost_usd": usage.get("cost"),
            "latency_ms": latency_ms,
        }

    async def decide(self, model: str, state: dict[str, str], questions: dict) -> dict:
        """Decisions API (alpha, used by Jev). Returns {answers, input_tokens, output_tokens, cost_usd, latency_ms}."""
        t0 = time.monotonic()
        try:
            r = await self._http.post(DECISIONS_URL, json={"model": model, "state": state, "questions": questions})
        except httpx.HTTPError as e:
            raise error_from_transport(e, " decisions") from None
        latency_ms = int((time.monotonic() - t0) * 1000)
        if r.status_code != 200:
            raise error_from_response(r, " decisions")
        data = r.json()
        if "error" in data or "answers" not in data:
            raise error_from_body(data.get("error", data), " decisions")
        usage = data.get("usage") or {}
        return {"answers": data["answers"], "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0), "cost_usd": usage.get("cost"),
                "latency_ms": latency_ms}

    async def close(self) -> None:
        await self._http.aclose()
