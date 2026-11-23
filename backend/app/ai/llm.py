"""The language-model client.

Everything above this file talks to a model through `BaseLLM`:
    generate(system, messages, tools)      -> LLMReply (text and/or tool calls)
    generate_json(system, messages, schema) -> (dict, LLMReply)   structured output that matches a JSON schema

Messages are plain dicts, so they can be stored in the agent's checkpoints:
    {"role": "user",  "text": "..."}
    {"role": "model", "text": "...", "calls": [{"id": "...", "name": "...", "args": {...}, "sig": "<base64>|None"}]}
    {"role": "tool",  "results": [{"id": "...", "name": "...", "response": {...}}]}

GeminiLLM is the real one (google-genai). Every call is retried on rate limits and server errors and is written
to the llm_calls table with its token counts, so the cost of a run can always be seen.
"""
from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tenacity import RetryError, Retrying, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from ..db import connect, now_iso

log = logging.getLogger("aeisp.ai")

RETRY_CODES = {408, 429, 500, 502, 503, 504}


class LLMError(Exception):
    """The model could not be reached, refused, or answered with something unusable."""


@dataclass
class ToolCall:
    id: str
    name: str
    args: Dict[str, Any]
    sig: Optional[str] = None        # Gemini's thought signature (base64); must be sent back with the call

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "args": self.args, "sig": self.sig}


@dataclass
class Usage:
    prompt: int = 0
    output: int = 0
    thoughts: int = 0
    total: int = 0

    def add(self, other: "Usage") -> None:
        self.prompt += other.prompt
        self.output += other.output
        self.thoughts += other.thoughts
        self.total += other.total


@dataclass
class LLMReply:
    text: str = ""
    calls: List[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    finish_reason: str = ""
    attempts: int = 1

    def as_message(self) -> dict:
        return {"role": "model", "text": self.text, "calls": [c.to_dict() for c in self.calls]}


# ---------------------------------------------------------------- usage log
class UsageLog:
    """Writes one row per model call into llm_calls."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    def record(self, *, run_id: Optional[int], purpose: str, model: str, usage: Usage, latency_ms: int,
               attempts: int, ok: bool, error: str = "") -> None:
        try:
            with connect(self.db_path) as c:
                c.execute(
                    "INSERT INTO llm_calls (ts, run_id, purpose, model, prompt_tokens, output_tokens, thought_tokens, "
                    "total_tokens, latency_ms, attempts, ok, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (now_iso(), run_id, purpose, model, usage.prompt, usage.output, usage.thoughts, usage.total,
                     latency_ms, attempts, 1 if ok else 0, error[:500] or None))
        except Exception:  # noqa: BLE001  accounting must never break a run
            log.exception("could not record an llm call")

    def totals(self, run_id: Optional[int] = None) -> dict:
        sql = ("SELECT COUNT(*) AS calls, SUM(ok = 0) AS failed, COALESCE(SUM(prompt_tokens), 0) AS prompt, "
               "COALESCE(SUM(output_tokens), 0) AS output, COALESCE(SUM(thought_tokens), 0) AS thoughts, "
               "COALESCE(SUM(total_tokens), 0) AS total, COALESCE(SUM(latency_ms), 0) AS latency_ms FROM llm_calls")
        args: tuple = ()
        if run_id is not None:
            sql += " WHERE run_id = ?"
            args = (run_id,)
        with connect(self.db_path) as c:
            r = dict(c.execute(sql, args).fetchone())
        r["failed"] = int(r["failed"] or 0)
        return r

    def calls(self, run_id: int) -> List[dict]:
        with connect(self.db_path) as c:
            rows = c.execute("SELECT * FROM llm_calls WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------- the interface
class BaseLLM:
    model = "none"

    def __init__(self, usage_log: Optional[UsageLog] = None) -> None:
        self.usage_log = usage_log

    # subclasses implement these two; they return an LLMReply (for _json the text is the JSON document)
    def _generate(self, system: str, messages: List[dict], tools: Optional[List[dict]]) -> LLMReply:
        raise NotImplementedError

    def _generate_json(self, system: str, messages: List[dict], schema: dict) -> LLMReply:
        raise NotImplementedError

    def _recorded(self, fn, purpose: str, run_id: Optional[int]) -> LLMReply:
        t0 = time.perf_counter()
        try:
            reply = fn()
        except Exception as e:
            if self.usage_log:
                self.usage_log.record(run_id=run_id, purpose=purpose, model=self.model, usage=Usage(),
                                      latency_ms=int((time.perf_counter() - t0) * 1000),
                                      attempts=getattr(e, "attempts", 1), ok=False, error=str(e))
            raise
        if self.usage_log:
            self.usage_log.record(run_id=run_id, purpose=purpose, model=reply.model or self.model, usage=reply.usage,
                                  latency_ms=int((time.perf_counter() - t0) * 1000), attempts=reply.attempts, ok=True)
        return reply

    def generate(self, system: str, messages: List[dict], tools: Optional[List[dict]] = None, *,
                 purpose: str = "agent", run_id: Optional[int] = None) -> LLMReply:
        return self._recorded(lambda: self._generate(system, messages, tools), purpose, run_id)

    def generate_json(self, system: str, messages: List[dict], schema: dict, *, purpose: str = "json",
                      run_id: Optional[int] = None) -> Tuple[dict, LLMReply]:
        reply = self._recorded(lambda: self._generate_json(system, messages, schema), purpose, run_id)
        try:
            data = json.loads(reply.text)
        except (TypeError, ValueError) as e:
            raise LLMError(f"the model did not return valid JSON: {e}") from e
        if not isinstance(data, dict):
            raise LLMError("the model returned JSON that is not an object")
        return data, reply


# ---------------------------------------------------------------- Gemini
def _retryable(e: BaseException) -> bool:
    from google.genai import errors

    if isinstance(e, errors.APIError):
        return getattr(e, "code", None) in RETRY_CODES
    name = type(e).__name__
    return name in ("ReadTimeout", "ConnectTimeout", "ConnectError", "RemoteProtocolError", "TimeoutException")


class GeminiLLM(BaseLLM):
    def __init__(self, api_key: str, model: str, timeout_s: int = 90, retries: int = 4,
                 usage_log: Optional[UsageLog] = None, temperature: float = 0.2) -> None:
        super().__init__(usage_log)
        if not api_key:
            raise LLMError("GEMINI_API_KEY is not set in .env")
        from google import genai
        from google.genai import types

        self._types = types
        self.model = model
        self.retries = retries
        self.temperature = temperature
        self._client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=timeout_s * 1000))

    # ------------------------------------------------------------ conversion
    def _contents(self, messages: List[dict]) -> list:
        t = self._types
        out = []
        for m in messages:
            role = m["role"]
            if role == "user":
                out.append(t.Content(role="user", parts=[t.Part(text=m["text"])]))
            elif role == "model":
                parts = [t.Part(text=m["text"])] if m.get("text") else []
                for c in m.get("calls", []):
                    sig = base64.b64decode(c["sig"]) if c.get("sig") else None
                    parts.append(t.Part(function_call=t.FunctionCall(id=c.get("id") or None, name=c["name"],
                                                                      args=c.get("args") or {}),
                                        thought_signature=sig))
                if parts:
                    out.append(t.Content(role="model", parts=parts))
            elif role == "tool":
                parts = [t.Part(function_response=t.FunctionResponse(id=r.get("id") or None, name=r["name"],
                                                                      response=r["response"]))
                         for r in m["results"]]
                out.append(t.Content(role="user", parts=parts))
            else:
                raise ValueError(f"unknown message role {role!r}")
        return out

    def _reply(self, resp, attempts: int) -> LLMReply:
        um = resp.usage_metadata
        usage = Usage(prompt=(um.prompt_token_count or 0) if um else 0,
                      output=(um.candidates_token_count or 0) if um else 0,
                      thoughts=(um.thoughts_token_count or 0) if um else 0,
                      total=(um.total_token_count or 0) if um else 0)
        if not resp.candidates:
            reason = getattr(getattr(resp, "prompt_feedback", None), "block_reason", None)
            raise LLMError(f"the model returned no answer (blocked: {reason})")
        cand = resp.candidates[0]
        texts, calls = [], []
        for i, part in enumerate((cand.content.parts if cand.content else None) or []):
            if part.function_call is not None:
                fc = part.function_call
                sig = base64.b64encode(part.thought_signature).decode() if part.thought_signature else None
                calls.append(ToolCall(id=fc.id or f"call_{i}", name=fc.name, args=dict(fc.args or {}), sig=sig))
            elif part.text and not part.thought:
                texts.append(part.text)
        finish = str(cand.finish_reason.name if cand.finish_reason else "")
        return LLMReply(text="".join(texts).strip(), calls=calls, usage=usage, model=self.model,
                        finish_reason=finish, attempts=attempts)

    def _with_retries(self, fn):
        attempts = {"n": 0}

        def once():
            attempts["n"] += 1
            return fn()

        try:
            for attempt in Retrying(retry=retry_if_exception(_retryable), stop=stop_after_attempt(self.retries + 1),
                                    wait=wait_exponential_jitter(initial=2, max=30), reraise=True):
                with attempt:
                    resp = once()
        except RetryError as e:  # pragma: no cover (reraise=True makes this rare)
            err = LLMError(str(e))
            err.attempts = attempts["n"]
            raise err from e
        except LLMError:
            raise
        except Exception as e:
            err = LLMError(f"{type(e).__name__}: {e}")
            err.attempts = attempts["n"]
            raise err from e
        return resp, attempts["n"]

    def _generate(self, system, messages, tools):
        t = self._types
        cfg = t.GenerateContentConfig(
            system_instruction=system, temperature=self.temperature,
            tools=[t.Tool(function_declarations=[t.FunctionDeclaration(**d) for d in tools])] if tools else None,
            automatic_function_calling=t.AutomaticFunctionCallingConfig(disable=True))
        resp, n = self._with_retries(
            lambda: self._client.models.generate_content(model=self.model, contents=self._contents(messages), config=cfg))
        return self._reply(resp, n)

    def _generate_json(self, system, messages, schema):
        t = self._types
        cfg = t.GenerateContentConfig(system_instruction=system, temperature=self.temperature,
                                      response_mime_type="application/json", response_json_schema=schema)
        resp, n = self._with_retries(
            lambda: self._client.models.generate_content(model=self.model, contents=self._contents(messages), config=cfg))
        return self._reply(resp, n)


def make_llm(settings, usage_log: Optional[UsageLog] = None) -> BaseLLM:
    return GeminiLLM(settings.gemini_api_key, settings.gemini_model, settings.ai_timeout_s, settings.ai_retries,
                     usage_log)
