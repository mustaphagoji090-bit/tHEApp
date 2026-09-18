"""Swappable text backends for the art director.

The director needs a model that returns strict JSON. Either provider below does that; pick
with DIRECTOR_PROVIDER in .env. OpenAI is the default so a single OpenAI key can run the
whole app (transcription, prompt writing and -- if you want -- images).
"""
from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod

import httpx

from . import config, keys_store

log = logging.getLogger(__name__)


class WriterError(RuntimeError):
    pass


class RefusalError(WriterError):
    """The model declined the request outright, rather than failing technically."""


class Writer(ABC):
    name = "base"

    @abstractmethod
    def complete_json(self, *, system: str, user: str, schema: dict, max_tokens: int) -> dict:
        """Return the model's response parsed as JSON, validated against `schema`."""


class OpenAIWriter(Writer):
    name = "openai"

    def complete_json(self, *, system, user, schema, max_tokens) -> dict:
        key = keys_store.get("OPENAI_API_KEY")
        if not key:
            raise WriterError("No OpenAI API key set -- it writes the image prompts. Paste one in Settings.")

        body = {
            "model": config.OPENAI_TEXT_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "result", "strict": True, "schema": schema},
            },
            "max_tokens": max_tokens,
        }
        data = self._post(key, body)

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        if message.get("refusal"):
            raise RefusalError(message["refusal"])
        if choice.get("finish_reason") == "length":
            raise WriterError(
                "The model hit its output limit before finishing. Lower BEATS_PER_CALL "
                "or raise the token budget."
            )
        content = message.get("content")
        if not content:
            raise WriterError(f"Empty response from {config.OPENAI_TEXT_MODEL}.")
        return json.loads(content)

    def _post(self, key: str, body: dict) -> dict:
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        with httpx.Client(timeout=600.0) as client:
            response = client.post(
                "https://api.openai.com/v1/chat/completions", headers=headers, json=body
            )
            # Newer OpenAI models renamed this field; retry once rather than fail the run.
            if response.status_code == 400 and "max_completion_tokens" in response.text:
                body = dict(body)
                body["max_completion_tokens"] = body.pop("max_tokens")
                response = client.post(
                    "https://api.openai.com/v1/chat/completions", headers=headers, json=body
                )

        if response.status_code != 200:
            raise WriterError(f"OpenAI error {response.status_code}: {response.text[:400]}")
        return response.json()


class AnthropicWriter(Writer):
    name = "anthropic"

    def complete_json(self, *, system, user, schema, max_tokens) -> dict:
        import anthropic

        key = keys_store.get("ANTHROPIC_API_KEY")
        auth_token = os.getenv("ANTHROPIC_AUTH_TOKEN")
        if not key and not auth_token:
            raise WriterError("No Anthropic API key set. Paste one in Settings.")
        client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()

        kwargs = dict(
            model=config.DIRECTOR_MODEL,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        try:
            response = client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
            )
        except (TypeError, AttributeError, anthropic.BadRequestError):
            # Older SDK, or no fallback beta on this endpoint -- proceed without it.
            response = client.messages.create(**kwargs)

        if getattr(response, "stop_reason", None) == "refusal":
            detail = getattr(response, "stop_details", None)
            raise RefusalError(getattr(detail, "category", None) or "declined")

        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            raise WriterError("Empty response from the director model.")
        return json.loads(text)


WRITERS: dict[str, type[Writer]] = {"openai": OpenAIWriter, "anthropic": AnthropicWriter}


def available_writers() -> list[str]:
    status = config.provider_status()
    return [name for name in WRITERS if status.get(name)]


def get_writer(name: str | None = None) -> Writer:
    name = name or config.director_provider()
    if not name:
        raise WriterError(
            "No prompt writer is configured. Paste an OpenAI or Anthropic key in Settings."
        )
    if name not in WRITERS:
        raise WriterError(f"Unknown prompt writer '{name}'. Choose one of: {', '.join(WRITERS)}")
    return WRITERS[name]()
