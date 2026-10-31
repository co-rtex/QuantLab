"use client";

import { useState, type FormEvent } from "react";
import { ErrorNote, Panel, SyntheticLabel, type SyntheticStatus } from "@/components/ui";
import { api, ApiError } from "@/lib/api";

interface FactRef {
  id: string;
  label: string;
  display: string;
  source: string;
}
interface Reply {
  text: string;
  citations: string[];
  provider: string;
  declined: boolean;
  notes: string[];
  facts: FactRef[];
  verification: { grounded?: boolean; numeric_claims?: number };
}

const SUGGESTIONS = [
  "Summarize the results",
  "What was the worst drawdown?",
  "Did it beat the benchmark?",
  "How much did trading costs matter?",
  "Is the result robust out-of-sample?",
];

function Cited({ text }: { text: string }) {
  const parts = text.split(/(\[F\d+\])/g);
  return (
    <p className="whitespace-pre-line text-sm leading-relaxed">
      {parts.map((p, i) =>
        /^\[F\d+\]$/.test(p) ? (
          <sup key={i} className="mx-0.5 rounded bg-cobalt-soft px-1 text-[0.7rem] font-medium text-cobalt">
            {p.slice(1, -1)}
          </sup>
        ) : (
          <span key={i}>{p}</span>
        ),
      )}
    </p>
  );
}

export function AssistantPanel({ experimentId, synthetic }: { experimentId: string; synthetic: SyntheticStatus }) {
  const [question, setQuestion] = useState("");
  const [history, setHistory] = useState<{ q: string; a: Reply }[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError>();

  async function ask(q: string) {
    if (!q.trim()) return;
    setBusy(true);
    setErr(undefined);
    try {
      const a = await api<Reply>(`/experiments/${experimentId}/assistant`, { method: "POST", json: { question: q } });
      setHistory((h) => [{ q, a }, ...h].slice(0, 6));
      setQuestion("");
    } catch (e) {
      setErr(e as ApiError);
    }
    setBusy(false);
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    ask(question);
  }

  return (
    <Panel
      title="Ask about this experiment"
      aside={
        <span className="flex flex-wrap items-center justify-end gap-2">
          <SyntheticLabel status={synthetic} />
          Answers cite computed results; no investment advice
        </span>
      }
    >
      <form onSubmit={onSubmit} className="flex flex-wrap gap-2">
        <label className="min-w-64 flex-1">
          <span className="sr-only">Question</span>
          <input className="field" value={question} onChange={(e) => setQuestion(e.target.value)} maxLength={1000} placeholder="What drove the largest drawdown?" />
        </label>
        <button className="btn btn-primary" disabled={busy || question.trim().length < 2}>
          {busy ? "Answering…" : "Ask"}
        </button>
      </form>
      <div className="mt-2 flex flex-wrap gap-1.5">
        {SUGGESTIONS.map((s) => (
          <button key={s} type="button" className="rounded border border-rule px-2 py-0.5 text-xs text-ink-2 hover:bg-sunken" onClick={() => ask(s)} disabled={busy}>
            {s}
          </button>
        ))}
      </div>
      <div className="mt-3">
        <ErrorNote error={err} />
      </div>
      <ol className="mt-4 space-y-5" aria-live="polite">
        {history.map(({ q, a }, i) => (
          <li key={i} className="border-t border-sunken pt-4 first:border-0 first:pt-0">
            <p className="mb-2 text-sm font-medium">{q}</p>
            <Cited text={a.text} />
            {a.facts.length > 0 && (
              <table className="data num mt-3">
                <thead>
                  <tr>
                    <th>Ref</th>
                    <th>Evidence</th>
                    <th className="r">Value</th>
                    <th>Source</th>
                  </tr>
                </thead>
                <tbody>
                  {a.facts.map((f) => (
                    <tr key={f.id}>
                      <td>{f.id.slice(1)}</td>
                      <td className="whitespace-normal">{f.label}</td>
                      <td className="r whitespace-normal">{f.display}</td>
                      <td className="text-xs text-ink-3">{f.source}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <p className="mt-2 text-xs text-ink-3">
              {a.provider === "deterministic" ? "Assembled from computed facts" : `Phrased by ${a.provider}, every figure verified`}
              {a.verification.grounded === false ? " · verification failed" : ""}
              {a.notes.length > 0 ? ` · ${a.notes.join(" ")}` : ""}
            </p>
          </li>
        ))}
      </ol>
    </Panel>
  );
}
