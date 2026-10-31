"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { type ReactNode, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { useWorkspace } from "@/lib/workspace";
import { QueryError } from "./ui";

const NAV = [
  { href: "/", label: "Overview" },
  { href: "/data", label: "Market data" },
  { href: "/portfolios", label: "Portfolios" },
  { href: "/experiments", label: "Experiments" },
  { href: "/compare", label: "Compare" },
  { href: "/optimize", label: "Construction" },
  { href: "/ml", label: "ML research" },
  { href: "/settings", label: "Workspace" },
];

export function Shell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const { user, workspaces, workspace, setWorkspaceId, error, retry } = useWorkspace();

  const [signOutFailed, setSignOutFailed] = useState(false);

  async function signOut() {
    try {
      await api("/auth/logout", { method: "POST" });
    } catch (e) {
      // 401: the session had already ended. Anything else means the httpOnly cookie may
      // still be valid, so do not pretend the user is signed out.
      if (!(e instanceof ApiError && e.status === 401)) {
        setSignOutFailed(true);
        return;
      }
    }
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
          {signOutFailed && (
            <p role="alert" className="mt-1 text-loss">
              Sign-out failed; you are still signed in. Try again.
            </p>
          )}
        </div>
      </aside>
      <main className="min-w-0 px-4 py-6 md:px-8 md:py-8">
        {error && (
          <div className="mb-6">
            <QueryError q={{ data: workspaces.length ? workspaces : undefined, error, reload: retry }} what="your account and workspaces" />
          </div>
        )}
        {children}
      </main>
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
