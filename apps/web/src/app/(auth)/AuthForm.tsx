"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useState, type FormEvent } from "react";
import { api, ApiError } from "@/lib/api";
import { safeNext } from "@/lib/safeRedirect";

export function AuthForm({ mode }: { mode: "login" | "register" }) {
  const router = useRouter();
  const params = useSearchParams();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    setBusy(true);
    setError(null);
    try {
      await api(`/auth/${mode}`, {
        method: "POST",
        json:
          mode === "login"
            ? { email: f.get("email"), password: f.get("password") }
            : { email: f.get("email"), password: f.get("password"), display_name: f.get("display_name") },
      });
      router.push(safeNext(params.get("next"), window.location.origin));
    } catch (err) {
      if (err instanceof ApiError && Array.isArray(err.details)) {
        setError((err.details as { msg: string }[]).map((d) => d.msg.replace("Value error, ", "")).join(". "));
      } else {
        setError(err instanceof Error ? err.message : "Something went wrong");
      }
      setBusy(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="space-y-4" noValidate={false}>
      {mode === "register" && (
        <div>
          <label htmlFor="display_name" className="mb-1 block text-sm">
            Name
          </label>
          <input id="display_name" name="display_name" required maxLength={120} className="field" autoComplete="name" />
        </div>
      )}
      <div>
        <label htmlFor="email" className="mb-1 block text-sm">
          Email
        </label>
        <input id="email" name="email" type="email" required className="field" autoComplete="email" />
      </div>
      <div>
        <label htmlFor="password" className="mb-1 block text-sm">
          Password
        </label>
        <input
          id="password"
          name="password"
          type="password"
          required
          minLength={mode === "register" ? 10 : 1}
          className="field"
          autoComplete={mode === "register" ? "new-password" : "current-password"}
          aria-describedby={mode === "register" ? "pw-help" : undefined}
        />
        {mode === "register" && (
          <p id="pw-help" className="mt-1 text-xs text-ink-2">
            At least 10 characters with upper and lower case letters and a digit.
          </p>
        )}
      </div>
      {error && (
        <p role="alert" className="rounded bg-loss-soft px-3 py-2 text-sm text-loss">
          {error}
        </p>
      )}
      <button type="submit" className="btn btn-primary w-full justify-center" disabled={busy}>
        {mode === "login" ? (busy ? "Signing in…" : "Sign in") : busy ? "Creating account…" : "Create account"}
      </button>
      <p className="text-center text-sm text-ink-2">
        {mode === "login" ? (
          <>
            New to QuantLab?{" "}
            <Link href="/register" className="text-cobalt underline underline-offset-2">
              Create an account
            </Link>
          </>
        ) : (
          <>
            Already have an account?{" "}
            <Link href="/login" className="text-cobalt underline underline-offset-2">
              Sign in
            </Link>
          </>
        )}
      </p>
    </form>
  );
}
