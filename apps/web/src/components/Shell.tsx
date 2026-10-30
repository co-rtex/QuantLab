"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import type { ReactNode } from "react";
import { api } from "@/lib/api";
import { useWorkspace } from "@/lib/workspace";

const NAV = [
  { href: "/", label: "Overview" },
  { href: "/data", label: "Market data" },
  { href: "/portfolios", label: "Portfolios" },
  { href: "/experiments", label: "Experiments" },
  { href: "/compare", label: "Compare" },
  { href: "/ml", label: "ML research" },
  { href: "/settings", label: "Workspace" },
];

export function Shell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const { user, workspaces, workspace, setWorkspaceId } = useWorkspace();

  async function signOut() {
    await api("/auth/logout", { method: "POST" }).catch(() => {});
    router.push("/login");
  }

  return (
    <div className="min-h-screen md:grid md:grid-cols-[13.5rem_1fr]">
      <aside className="border-b border-rule bg-surface md:sticky md:top-0 md:h-screen md:border-b-0 md:border-r">
        <div className="flex items-center justify-between px-4 py-4 md:block">
          <Link href="/" className="font-serif text-lg font-semibold tracking-tight">
            QuantLab
          </Link>
          <p className="hidden text-xs text-ink-2 md:mt-0.5 md:block">Research workstation</p>
        </div>
        <div className="px-4 pb-3">
          <label htmlFor="ws" className="mb-1 block text-xs text-ink-2">
            Workspace
          </label>
          <select
            id="ws"
            className="field"
            value={workspace?.id ?? ""}
            onChange={(e) => setWorkspaceId(e.target.value)}
          >
            {workspaces.map((w) => (
              <option key={w.id} value={w.id}>
                {w.name}
              </option>
            ))}
          </select>
        </div>
        <nav aria-label="Primary" className="flex gap-1 overflow-x-auto px-2 pb-2 md:block md:space-y-0.5">
          {NAV.map((n) => {
            const active = n.href === "/" ? pathname === "/" : pathname.startsWith(n.href);
            return (
              <Link
                key={n.href}
                href={n.href}
                aria-current={active ? "page" : undefined}
                className={`block whitespace-nowrap rounded px-2.5 py-1.5 text-sm ${
                  active ? "bg-cobalt-soft font-medium text-cobalt" : "text-ink-2 hover:bg-sunken hover:text-ink"
                }`}
              >
                {n.label}
              </Link>
            );
          })}
        </nav>
        <div className="hidden border-t border-rule px-4 py-3 text-xs text-ink-2 md:absolute md:bottom-0 md:block md:w-full">
          <div className="truncate" title={user?.email}>
            {user?.display_name ?? "…"}
          </div>
          <button onClick={signOut} className="mt-1 text-cobalt hover:underline">
            Sign out
          </button>
        </div>
      </aside>
      <main className="min-w-0 px-4 py-6 md:px-8 md:py-8">{children}</main>
    </div>
  );
}

export function PageHeader({ title, lede, actions }: { title: string; lede?: ReactNode; actions?: ReactNode }) {
  return (
    <header className="mb-6 flex flex-wrap items-end justify-between gap-4">
      <div className="max-w-3xl">
        <h1 className="font-serif text-2xl font-semibold tracking-tight md:text-[1.75rem]">{title}</h1>
        {lede && <p className="mt-1.5 text-sm leading-relaxed text-ink-2">{lede}</p>}
      </div>
      {actions && <div className="flex gap-2">{actions}</div>}
    </header>
  );
}
