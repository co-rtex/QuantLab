import { Suspense, type ReactNode } from "react";
import { Shell } from "@/components/Shell";
import { WorkspaceProvider } from "@/lib/workspace";

function ShellFallback() {
  return (
    <div className="min-h-screen md:grid md:grid-cols-[13.5rem_1fr]" aria-busy="true">
      <aside className="border-r border-rule bg-surface" />
      <main className="px-8 py-8">
        <div className="h-8 w-64 animate-pulse rounded bg-sunken" />
      </main>
    </div>
  );
}

export default function AppLayout({ children }: { children: ReactNode }) {
  return (
    <WorkspaceProvider>
      {/* Everything inside depends on the URL and the signed-in user, so it streams at runtime. */}
      <Suspense fallback={<ShellFallback />}>
        <Shell>{children}</Shell>
      </Suspense>
    </WorkspaceProvider>
  );
}
