"""Assistant orchestration: optional LLM phrasing over verified facts, deterministic fallback."""

from __future__ import annotations

import re
from typing import Any, Protocol

from quantlab.assistant.grounding import (
    Answer,
    Fact,
    classify,
    deterministic_answer,
    verify_answer,
)

SYSTEM_RULES = """You explain the results of one quantitative backtest to a researcher.

Rules:
- Use only the facts listed below. Every number you write must come from a fact, and each
  sentence that states a number must cite it inline, e.g. "Sharpe was 0.32 [F4]".
- Do not calculate new statistics, extrapolate, or describe future performance.
- If the facts do not answer the question, say what is missing.
- Never recommend buying, selling, holding or allocating money; this is research, not advice.
- Mention that results come from synthetic data when the facts say so.
- Be concise: at most 150 words, plain prose or short bullets.

Facts (id | description | value | source):
"""


ADVICE_LANGUAGE = re.compile(
    r"\b(you should|i (would )?recommend|i suggest you|consider (buying|selling|investing)|"
    r"(strong )?(buy|sell) (signal|rating)|go long|go short)\b",
    re.IGNORECASE,
)


class ProviderError(RuntimeError):
    """The LLM provider failed, refused, or returned unusable output."""


class LLMProvider(Protocol):
    name: str

    def complete(self, system: str, question: str) -> str: ...


def system_prompt(facts: list[Fact]) -> str:
    return SYSTEM_RULES + "\n".join(
        f"{f.id} | {f.label} | {f.display()} | {f.source}" for f in facts
    )


class AnthropicProvider:
    """Claude via the official Anthropic SDK (optional dependency ``anthropic``)."""

    def __init__(self, api_key: str, model: str = "claude-opus-5-5", timeout: float = 30.0) -> None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depends on installed extras
            raise ProviderError("the anthropic package is not installed") from exc
        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=1)
        self.model = model
        self.name = f"anthropic:{model}"

    def complete(self, system: str, question: str) -> str:  # pragma: no cover - network
        a = self._anthropic
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=2000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={"effort": "low"},
                # The fact list is stable per experiment, so it is cached as a prefix.
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": question}],
            )
        except a.APIConnectionError as exc:
            raise ProviderError(f"connection error: {exc}") from exc
        except a.RateLimitError as exc:
            raise ProviderError("rate limited") from exc
        except a.APIStatusError as exc:
            raise ProviderError(f"API error {exc.status_code}") from exc
        if response.stop_reason == "refusal":
            raise ProviderError("model declined the request")
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if not text:
            raise ProviderError("empty response")
        return text


def answer_question(
    question: str, facts: list[Fact], provider: LLMProvider | None = None
) -> Answer:
    """Answer from evidence. The LLM only rephrases facts; its output is verified."""
    question = question.strip()[:1000]
    base = deterministic_answer(question, facts)
    base.verification = verify_answer(base.text, facts)
    intents = classify(question)
    if provider is None:
        base.notes.append("No language model configured; answer assembled from computed facts.")
        return base
    if base.declined or intents == ["unknown"] or not base.citations:
        return base  # advice and out-of-scope questions never reach the model
    try:
        text = provider.complete(system_prompt(facts), question)
    except ProviderError as exc:
        base.notes.append(f"Language model unavailable ({exc}); showing the deterministic answer.")
        return base
    except Exception as exc:  # any unexpected provider failure must not break the page
        base.notes.append(
            f"Language model failed ({type(exc).__name__}); showing the deterministic answer."
        )
        return base
    if not isinstance(text, str) or not text.strip():
        base.notes.append(
            "Language model returned malformed output; showing the deterministic answer."
        )
        return base
    check = verify_answer(text, facts)
    if ADVICE_LANGUAGE.search(text):
        base.notes.append("Language model answer rejected: it contained investment advice.")
        base.verification["rejected_llm"] = {**check, "advice": True}
        return base
    if not check["grounded"] or (check["numeric_claims"] and not check["cited"]):
        base.notes.append(
            "Language model answer rejected: it contained figures not supported by the "
            f"evidence ({', '.join(check['unsupported'][:5]) or 'missing citations'})."
        )
        base.verification["rejected_llm"] = check
        return base
    return Answer(text, check["cited"], provider.name, intents, verification=check)


def facts_payload(facts: list[Fact]) -> list[dict[str, Any]]:
    return [
        {
            "id": f.id,
            "label": f.label,
            "value": f.value,
            "display": f.display(),
            "unit": f.unit,
            "source": f.source,
        }
        for f in facts
    ]
