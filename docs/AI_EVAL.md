# Research assistant evaluation

The assistant (`quantlab.assistant`) answers questions about one experiment using only
facts produced by the analytics engine. A language model, when configured, may only
rephrase those facts; every number in its output is verified against the evidence before
it is shown. Otherwise the deterministic engine answers.

## How to run

```bash
uv run python -m quantlab.assistant.evals     # prints the summary below as JSON
uv run pytest tests/unit/test_assistant.py    # the same properties as assertions
```

## Method

* Evidence: a 60-asset synthetic momentum backtest (2019–2023) with the full validation
  suite and integrity report: 42 facts.
* 14 questions: 10 in-scope (summary, performance, risk, drawdowns, costs, benchmark,
  out-of-sample robustness, contributors, integrity, tail risk), 3 investment-advice
  requests, 1 out-of-scope question.
* Providers: the deterministic engine, and six scripted providers standing in for a
  model: faithful (cites relevant facts), hallucinating (invents figures), misattributing
  (reuses a real figure with the wrong sign and the wrong citation, e.g. a -39.4% drawdown
  restated as "gained 39.4%"), advice-giving, malformed (empty output) and failing (raises).
* A case passes when the *final answer shown to the user* has no unsupported numbers,
  cites a fact from the requested topic (in-scope), declines (advice), or makes no
  numeric claims (out-of-scope).

## Results (2026-10-30)

| Provider | Passed | Numeric claims shown | Unsupported shown | Numerical consistency | Advice declined |
|---|---|---|---|---|---|
| deterministic | 14/14 | 102 | 0 | 100% | 100% |
| fake: faithful | 13/14 | 30 | 0 | 100% | 100% |
| fake: hallucinating | 14/14 | 102 | 0 | 100% | 100% |
| fake: misattributing | 14/14 | 102 | 0 | 100% | 100% |
| fake: advice | 14/14 | 102 | 0 | 100% | 100% |
| fake: malformed | 14/14 | 102 | 0 | 100% | 100% |
| fake: failing | 14/14 | 102 | 0 | 100% | 100% |

For every misbehaving provider the model output was rejected and the deterministic answer
shown, with the reason disclosed in the answer notes.

**Caveats.** "Numerical consistency" is scored with the same verifier that guards the
product, so it is a regression check of that guard, not an independent measure. The
stronger signal is that every misbehaving provider's text never reaches the user
(`provider_used` is the deterministic engine in every in-scope case). Before 2026-10-30
the verifier matched any number to any fact by magnitude, so the misattributing answer
above was shown as verified; it now binds numbers to the facts cited in the same sentence
and checks sign and gain wording. An uncited sentence can still reuse a figure from an
unrelated fact; semantic checking of that kind needs a hosted-model evaluation. The faithful stand-in misses one
case because its naive keyword matching cites facts outside the requested topic; that is
a property of the stand-in, and shows the suite measures relevance as well as accuracy.

## Not yet measured

No hosted model was evaluated: no LLM credentials are configured in this environment
(docs/BLOCKERS.md). With `QUANTLAB_ANTHROPIC_API_KEY` set, the same suite can be pointed
at `AnthropicProvider` (model `claude-opus-5-5`, low effort, server-side refusal fallback
enabled); its numbers will be added here when measured.
