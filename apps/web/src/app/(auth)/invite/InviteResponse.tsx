"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api, ApiError, type User } from "@/lib/api";
import { dateTime } from "@/lib/format";

// The token arrives in the URL fragment (/invite#token=...), so it never reaches a server log
// or a Referer header. Signing in navigates away, so it waits in sessionStorage until then.
const KEY = "quantlab.invitation";

interface Preview {
  workspace_id: string;
  workspace_name: string;
  role: string;
  invited_by: string | null;
  expires_at: string;
}

type State =
  | { kind: "loading" }
  | { kind: "signed-out" }
  | { kind: "missing" }
  | { kind: "ready"; user: User; preview: Preview; token: string }
  | { kind: "error"; message: string; user?: User }
  | { kind: "done"; message: string; workspace?: string };

function readToken(): string | null {
  const fromHash = new URLSearchParams(window.location.hash.slice(1)).get("token");
  try {
    if (fromHash) {
      sessionStorage.setItem(KEY, fromHash);
      // Drop the token from the address bar and history.
      window.history.replaceState(null, "", window.location.pathname);
      return fromHash;
    }
    return sessionStorage.getItem(KEY);
  } catch {
    return fromHash;
  }
}

function forget() {
  try {
    sessionStorage.removeItem(KEY);
  } catch {
    /* storage unavailable: nothing was stored */
  }
}

function explain(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.code === "invitation_expired") return "This invitation has expired. Ask the workspace owner to send a new one.";
    if (e.code === "invitation_used") return "This invitation has already been used, declined or withdrawn.";
    if (e.code === "invitation_not_found")
      return "This invitation is not valid for the account you are signed in with. Sign in with the invited email address.";
    return e.message;
  }
  return "Something went wrong. Try again.";
}

export function InviteResponse() {
  const router = useRouter();
  const [state, setState] = useState<State>({ kind: "loading" });
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    (async () => {
      const token = readToken();
      if (!token) {
        setState({ kind: "missing" });
        return;
      }
      let user: User;
      try {
        user = await api<User>("/auth/me");
      } catch {
        setState({ kind: "signed-out" });
        return;
      }
      try {
        const preview = await api<Preview>("/auth/invitations/preview", { method: "POST", json: { token } });
        setState({ kind: "ready", user, preview, token });
      } catch (e) {
        setState({ kind: "error", message: explain(e), user });
      }
    })();
  }, []);

  async function answer(accept: boolean) {
    if (state.kind !== "ready") return;
    setBusy(true);
    try {
      await api(`/auth/invitations/${accept ? "accept" : "decline"}`, { method: "POST", json: { token: state.token } });
      forget();
      setState(
        accept
          ? { kind: "done", message: `You joined ${state.preview.workspace_name}.`, workspace: state.preview.workspace_id }
          : { kind: "done", message: "Invitation declined." },
      );
    } catch (e) {
      setState({ kind: "error", message: explain(e), user: state.user });
    } finally {
      setBusy(false);
    }
  }

  if (state.kind === "loading") return <p className="text-sm text-ink-2">Checking your invitation…</p>;
  if (state.kind === "missing")
    return <p className="text-sm text-ink-2">Open the link from your invitation to join a workspace.</p>;
  if (state.kind === "signed-out")
    return (
      <div className="space-y-4 text-sm">
        <p>Sign in with the email address the invitation was sent to, or create an account with it.</p>
        <div className="flex gap-2">
          <Link href="/login?next=/invite" className="btn btn-primary">
            Sign in
          </Link>
          <Link href="/register?next=/invite" className="btn btn-quiet">
            Create account
          </Link>
        </div>
      </div>
    );
  if (state.kind === "error")
    return (
      <div className="space-y-3 text-sm">
        <p role="alert" className="rounded bg-loss-soft px-3 py-2 text-loss">
          {state.message}
        </p>
        {state.user && <p className="text-ink-2">Signed in as {state.user.email}.</p>}
        <Link href="/" className="btn btn-quiet">
          Go to QuantLab
        </Link>
      </div>
    );
  if (state.kind === "done")
    return (
      <div className="space-y-3 text-sm">
        <p role="status">{state.message}</p>
        <button
          className="btn btn-primary"
          onClick={() => {
            if (state.workspace) {
              try {
                localStorage.setItem("quantlab.workspace", state.workspace);
              } catch {
                /* the app falls back to the first workspace */
              }
            }
            router.push("/");
          }}
        >
          Continue
        </button>
      </div>
    );
  const { preview, user } = state;
  return (
    <div className="space-y-4 text-sm">
      <p>
        {preview.invited_by ?? "A workspace owner"} invited you to <strong>{preview.workspace_name}</strong> as{" "}
        {preview.role === "owner" ? "an" : "a"} <strong>{preview.role}</strong>.
      </p>
      <p className="text-ink-2">
        Signed in as {user.email}. The invitation expires {dateTime(preview.expires_at)}.
      </p>
      <div className="flex gap-2">
        <button className="btn btn-primary" disabled={busy} onClick={() => answer(true)}>
          Accept invitation
        </button>
        <button className="btn btn-quiet" disabled={busy} onClick={() => answer(false)}>
          Decline
        </button>
      </div>
    </div>
  );
}
