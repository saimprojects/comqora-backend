"""Bounded Fazita transport. No arbitrary URLs, redirects or user-supplied keys."""

import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from django.conf import settings

from .credentials import decrypt_key

UNAVAILABLE = "Your Manager is temporarily unavailable. Please try again later or contact support."
MAX_RESPONSE_BYTES = 2_000_000
MODEL_TIMEOUT = 300


def response_failure(data):
    """Keep diagnostics useful without persisting arbitrary provider messages or data."""
    details = data.get("incomplete_details")
    reason = details.get("reason") if isinstance(details, dict) else None
    if reason == "max_output_tokens":
        return ProviderError(
            "The model reached its output-token limit. No partial answer or tool call was accepted."
        )
    if reason == "content_filter":
        return ProviderError("The provider stopped this response for content filtering.")
    error = data.get("error")
    error = error if isinstance(error, dict) else data
    code = error.get("code")
    known_codes = {
        "server_error",
        "rate_limit_exceeded",
        "insufficient_quota",
        "invalid_api_key",
        "model_not_found",
        "invalid_request_error",
        "unsupported_parameter",
        "context_length_exceeded",
    }
    suffix = f" ({code})" if isinstance(code, str) and code in known_codes else ""
    return ProviderError(
        f"The provider returned a failed or incomplete response{suffix}. No partial answer or tool call was accepted."
    )


def response_chunks(response, deadline):
    """Single buffered reads prevent a slow stream from hiding the elapsed-time check."""
    total = 0
    while True:
        if time.monotonic() >= deadline:
            raise ProviderError("The AI provider exceeded this request's time budget.")
        chunk = response.read1(8192)
        if time.monotonic() >= deadline:
            raise ProviderError("The AI provider exceeded this request's time budget.")
        if not chunk:
            return
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise ProviderError(
                "The AI response exceeded the safe size limit. Try a narrower question."
            )
        yield chunk


def read_response_stream(response, deadline):
    """Only a terminal response is trusted; deltas never execute tools or become answers."""
    pending, data_lines = b"", []
    for chunk in response_chunks(response, deadline):
        pending += chunk
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            line = line.rstrip(b"\r")
            if line.startswith(b"data:"):
                data_lines.append(line[5:].lstrip(b" "))
            elif not line and data_lines:
                raw = b"\n".join(data_lines)
                data_lines = []
                if raw == b"[DONE]":
                    raise ProviderError("The AI stream ended without a completed response.")
                event = json.loads(raw)
                if not isinstance(event, dict):
                    raise ValueError()
                kind = event.get("type")
                if kind in {"error", "response.failed", "response.incomplete"}:
                    detail = event.get("response")
                    raise response_failure(detail if isinstance(detail, dict) else event)
                if kind == "response.completed":
                    result = event.get("response")
                    if (
                        not isinstance(result, dict)
                        or result.get("status") != "completed"
                        or result.get("error")
                        or not isinstance(result.get("output"), list)
                    ):
                        raise ProviderError("The AI stream returned an invalid completion.")
                    return result
                # Ignore deltas, heartbeat comments and Fazita/Codex metadata events.
    raise ProviderError("The AI stream was interrupted before completion. Please try again.")


class ProviderError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


PATHS = {
    "models": "/v1/models",
    "responses": "/v1/responses",
    "chat": "/v1/chat/completions",
    "messages": "/v1/messages",
}


def api_key(connection):
    import os

    if connection.encrypted_api_key:
        return decrypt_key(connection.encrypted_api_key)
    name = connection.key_environment_variable
    if not re.fullmatch(r"FAZITA_[A-Z0-9_]+", name):
        return ""
    return os.environ.get(name, "") or (
        getattr(settings, "FAZITA_API_KEY", "") if name == "FAZITA_API_KEY" else ""
    )


def request_json(connection, endpoint, payload=None, *, deadline=None):
    key = api_key(connection)
    if not key:
        raise ProviderError(
            "The AI provider key is not configured. Ask the platform administrator to configure Fazita."
        )
    if endpoint not in PATHS:
        raise ProviderError("Unsupported AI endpoint.")
    streaming = endpoint == "responses" and payload is not None and payload.get("stream") is True
    limit = 15 if endpoint == "models" else MODEL_TIMEOUT
    deadline = min(deadline if deadline is not None else float("inf"), time.monotonic() + limit)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProviderError("This investigation has reached its time budget.")
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if streaming else "application/json",
        "Authorization": f"Bearer {key}",
    }
    if endpoint == "messages":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    request = Request(
        "https://fazita.com" + PATHS[endpoint],
        headers=headers,
        data=json.dumps(payload).encode() if payload is not None else None,
    )
    try:
        with build_opener(NoRedirect()).open(request, timeout=remaining) as response:
            if (
                streaming
                and "text/event-stream" in response.headers.get("Content-Type", "").lower()
            ):
                return read_response_stream(response, deadline)
            raw = b"".join(response_chunks(response, deadline))
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except HTTPError as exc:
        messages = {
            401: "Fazita rejected the API key. Ask the administrator to check the connection.",
            403: "This Fazita key does not have access to the selected model.",
            429: "Fazita's usage limit was reached. Please try again later.",
            400: "Fazita rejected the model or tool format. Check this model's protocol in Jazzmin.",
            404: "The configured model or endpoint was not found. Check the model ID and protocol in Jazzmin.",
        }
        raise ProviderError(
            messages.get(
                exc.code,
                f"The AI provider is temporarily unavailable (HTTP {exc.code}). Please try again.",
            )
        ) from None
    except (URLError, TimeoutError, OSError):
        raise ProviderError(
            "The AI provider timed out or could not be reached. Your business records are unchanged."
        ) from None
    except (ValueError, TypeError):
        raise ProviderError(
            "The AI provider returned an unreadable response. Try again or select another model."
        ) from None


class Session:
    def __init__(self, model, instructions, history, tools):
        self.model = model
        self.instructions = instructions
        self.messages = history
        self.tools = tools

    def ask(self, *, deadline=None):
        model = self.model
        common = {"model": model.model_id}
        if model.protocol == "responses":
            payload = {
                **common,
                "instructions": self.instructions,
                "input": self.messages,
                "store": False,
                "stream": True,
                "include": ["reasoning.encrypted_content"],
                "tools": [{"type": "function", **t, "strict": False} for t in self.tools],
            }
        elif model.protocol == "chat":
            payload = {
                **common,
                "messages": [{"role": "system", "content": self.instructions}, *self.messages],
                "store": False,
                "tools": [{"type": "function", "function": t} for t in self.tools],
            }
        else:
            if model.max_output_tokens is None:
                raise ProviderError("Messages requires an explicit output-token budget.")
            payload = {
                **common,
                "system": self.instructions,
                "messages": self.messages,
                "max_tokens": model.max_output_tokens,
                "tools": [
                    {
                        "name": t["name"],
                        "description": t["description"],
                        "input_schema": t["parameters"],
                    }
                    for t in self.tools
                ],
            }
        if model.max_output_tokens is not None and model.protocol in {"responses", "chat"}:
            parameter = (
                "max_output_tokens" if model.protocol == "responses" else "max_completion_tokens"
            )
            payload[parameter] = model.max_output_tokens
        data = request_json(model.connection, model.protocol, payload, deadline=deadline)
        calls, text = [], []
        try:
            if model.protocol == "responses":
                if data.get("status") not in {None, "completed"} or data.get("error"):
                    raise response_failure(data)
                output = data["output"]
                self.messages.extend(output)
                for item in output:
                    if item["type"] == "function_call":
                        calls.append(
                            {
                                "id": item["call_id"],
                                "name": item["name"],
                                "arguments": item["arguments"],
                            }
                        )
                    elif item["type"] == "message":
                        text.extend(
                            p.get("text", "")
                            for p in item.get("content", [])
                            if p["type"] == "output_text"
                        )
            elif model.protocol == "chat":
                message = data["choices"][0]["message"]
                self.messages.append(message)
                text.append(message.get("content") or "")
                for call in message.get("tool_calls") or []:
                    calls.append({"id": call["id"], **call["function"]})
                if data["choices"][0].get("finish_reason") == "length":
                    raise ProviderError(
                        "The model reached its output limit. Please narrow the question."
                    )
            else:
                self.messages.append({"role": "assistant", "content": data["content"]})
                for item in data["content"]:
                    if item["type"] == "tool_use":
                        calls.append(
                            {"id": item["id"], "name": item["name"], "arguments": item["input"]}
                        )
                    elif item["type"] == "text":
                        text.append(item["text"])
                if data.get("stop_reason") == "max_tokens":
                    raise ProviderError(
                        "The model reached its output limit. Please narrow the question."
                    )
            answer = "\n".join(text)
            if len(calls) > 8 or len(answer) > 30000:
                raise ValueError()
            return answer, calls, data.get("usage") or {}
        except (KeyError, IndexError, TypeError, ValueError):
            raise ProviderError(
                "This model returned an unsupported response format. Check its protocol in Jazzmin."
            ) from None

    def add_results(self, results):
        if self.model.protocol == "messages":
            self.messages.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": call_id, "content": result}
                        for call_id, result in results
                    ],
                }
            )
        else:
            for call_id, result in results:
                if self.model.protocol == "responses":
                    self.messages.append(
                        {"type": "function_call_output", "call_id": call_id, "output": result}
                    )
                else:
                    self.messages.append(
                        {"role": "tool", "tool_call_id": call_id, "content": result}
                    )
