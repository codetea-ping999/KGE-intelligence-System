"""Claude API adapter for the LLM extractor, plus a recorder for offline replay.

The adapter only produces raw text. Everything it returns still goes through
`LLMExtractor` parsing, the validation gate and human review like any other LLM output.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kge.extract import LLMClient, prompt_key

DEFAULT_MODEL = "claude-opus-5-5"

#: JSON schema the response must follow (structured outputs). Field meanings match
#: the prompt template in kge.extract.
EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "assertions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "subject_type": {"type": "string"},
                    "predicate": {"type": "string"},
                    "object": {"type": "string"},
                    "object_type": {"type": "string"},
                    "object_datatype": {"type": "string"},
                    "epistemic_type": {"type": "string", "enum": ["fact", "hypothesis"]},
                    "confidence": {"type": "number"},
                    "quote": {"type": "string"},
                    "valid_from": {"type": "string"},
                    "valid_to": {"type": "string"},
                },
                "required": [
                    "subject",
                    "predicate",
                    "object",
                    "epistemic_type",
                    "confidence",
                    "quote",
                ],  # fmt: skip
                "additionalProperties": False,
            },
        }
    },
    "required": ["assertions"],
    "additionalProperties": False,
}


class LLMError(Exception):
    """Base class for failures of the live LLM call."""


class LLMRefused(LLMError):
    """The model declined the request (stop_reason == "refusal")."""


class LLMTruncated(LLMError):
    """The response hit max_tokens, so its JSON would be incomplete."""


class LLMUnavailable(LLMError):
    """The API could not be reached or rejected the request (credentials, network, 4xx/5xx)."""


class ClaudeLLMClient:
    """Calls the Claude Messages API. Credentials come from the environment
    (ANTHROPIC_API_KEY, or an `ant auth login` profile)."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        effort: str = "medium",
        max_tokens: int = 16000,
        client: Any = None,
    ) -> None:
        if client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - depends on the environment
                raise RuntimeError(
                    "the Claude adapter needs the optional dependency: uv sync --extra claude"
                ) from exc
            client = anthropic.Anthropic()
        self._client = client
        self.requested_model = model
        #: the model that produced the last answer (the requested one until a call is made)
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens

    def complete(self, prompt: str) -> str:
        try:
            response = self._create(prompt)
        except Exception as exc:
            # SDK errors (and its TypeError for missing credentials) become one error type.
            if isinstance(exc, TypeError) or type(exc).__module__.startswith("anthropic"):
                raise LLMUnavailable(f"{type(exc).__name__}: {exc}") from exc
            raise
        if response.stop_reason == "refusal":
            raise LLMRefused(f"model declined the extraction request ({response.stop_details})")
        if response.stop_reason == "max_tokens":
            raise LLMTruncated("response was cut off at max_tokens")
        texts = [b.text for b in response.content if b.type == "text"]
        if not texts:
            raise LLMRefused("response contained no text block")
        # after a server-side fallback another model answered: provenance names that one
        self.model = str(getattr(response, "model", None) or self.requested_model)
        return str(texts[0])

    def _create(self, prompt: str) -> Any:
        # Server-side fallback reroutes a safety decline to another model inside the
        # same call; a decline of the whole chain still surfaces as stop_reason "refusal".
        return self._client.beta.messages.create(
            model=self.requested_model,
            max_tokens=self.max_tokens,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA},
            },
            messages=[{"role": "user", "content": prompt}],
        )


class RecordingLLMClient:
    """Wraps a live client and stores every response as a replay fixture."""

    def __init__(self, inner: LLMClient, fixtures_dir: str | Path) -> None:
        self.inner = inner
        self.dir = Path(fixtures_dir)

    @property
    def model(self) -> str:
        """The model that actually answered the last call (it can change on fallback)."""
        return self.inner.model

    def complete(self, prompt: str) -> str:
        response = self.inner.complete(prompt)
        key = prompt_key(prompt)
        self.dir.mkdir(parents=True, exist_ok=True)
        record = {"prompt_key": key, "model": self.inner.model, "response": response}
        (self.dir / f"{key}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return response
