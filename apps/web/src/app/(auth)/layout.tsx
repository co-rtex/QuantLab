import type { ReactNode } from "react";

export default function AuthLayout({ children }: { children: ReactNode }) {
  return (
    <div className="grid min-h-screen md:grid-cols-[1fr_minmax(22rem,28rem)]">
      <section className="hidden flex-col justify-between border-r border-rule bg-surface p-10 md:flex">
        <p className="font-serif text-xl font-semibold">QuantLab</p>
        <div className="max-w-md">
          <p className="font-serif text-3xl leading-snug">
            Every result carries the dataset version, the assumptions and the code that produced it.
          </p>
          <p className="mt-4 text-sm leading-relaxed text-ink-2">
            Backtest strategies on versioned data, measure risk, validate out of sample and reproduce any experiment
            from its manifest.
          </p>
        </div>
        <p className="text-xs text-ink-3">Research tooling. Not investment advice.</p>
      </section>
      <section className="flex items-center justify-center px-6 py-12">
        <div className="w-full max-w-sm">{children}</div>
      </section>
    </div>
  );
}
