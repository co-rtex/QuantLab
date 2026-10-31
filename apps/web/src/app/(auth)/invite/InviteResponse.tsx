"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
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
  // `again`: the failure may pass (network, 429, 5xx), so the check can be run again with the
  // stored token. `switchAccount`: the invitation is for another address.
  | { kind: "error"; message: string; user?: User; again?: boolean; switchAccount?: boolean; requestId?: string }
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

// Used, withdrawn or expired invitations are final. "Not for this account" is not: the token
// is kept so the user can sign in with the invited address and open /invite again.
function isFinal(e: unknown): boolean {
  return e instanceof ApiError && e.status >= 400 && e.status < 500 && e.status !== 429 && e.code !== "invitation_not_found";
}

// A failed fetch rejects with a TypeError, not an ApiError.
function isTransient(e: unknown): boolean {
  return !(e instanceof ApiError) || e.status === 0 || e.status === 429 || e.status >= 500;
}

function failure(e: unknown, user?: User): State {
  return {
    kind: "error",
    message: explain(e),
    user,
    again: isTransient(e),
    switchAccount: e instanceof ApiError && e.code === "invitation_not_found",
    requestId: e instanceof ApiError ? e.requestId : undefined,
  };
}

async function signOutAndRetry() {
  await api("/auth/logout", { method: "POST" }).catch(() => undefined);
  // Full navigation (not router.push): the session is gone and app state must reset.
  // eslint-disable-next-line @next/next/no-location-assign-relative-destination
  window.location.assign("/login?next=/invite");
}

function explain(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.code === "invitation_expired") return "This invitation has expired. Ask the workspace owner to send a new one.";
    if (e.code === "invitation_used") return "This invitation has already been used, declined or withdrawn.";
    if (e.code === "invitation_withdrawn") return "This invitation was withdrawn. Ask a workspace owner for a new one.";
    if (e.code === "invitation_not_found")
      return "This invitation is not valid for the account you are signed in with. Sign in with the invited email address.";
    if (e.status >= 500) return "QuantLab could not check the invitation just now. It is kept: try again in a moment.";
    return e.message;
  }
  return "QuantLab could not be reached. Your invitation is kept: try again when you are back online.";
}

export function InviteResponse() {
  const router = useRouter();
  const [state, setState] = useState<State>({ kind: "loading" });
  const [busy, setBusy] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const [retried, setRetried] = useState(false);

  const check = useCallback(async () => {
    const token = readToken();
    if (!token) {
      setState({ kind: "missing" });
      return;
    }
    let user: User;
    try {
      user = await api<User>("/auth/me");
    } catch (e) {
      // Only a 401 means there is no session (#85). An outage used to send a signed-in
      // invitee round the login flow, and never said the API was down.
      setState(e instanceof ApiError && e.status === 401 ? { kind: "signed-out" } : failure(e));
      return;
    }
    try {
      const preview = await api<Preview>("/auth/invitations/preview", { method: "POST", json: { token } });
      setState({ kind: "ready", user, preview, token });
    } catch (e) {
      if (isFinal(e)) forget();
      setState(failure(e, user));
    }
  }, []);

  useEffect(() => {
    (async () => {
      await check();
    })();
  }, [check]);

  // "Try again" disappears with the error; keep focus in the answer instead of the body.
  useEffect(() => {
    if (retried && state.kind !== "loading") box.current?.focus();
  }, [retried, state.kind]);

  function again() {
    setRetried(true);
    setState({ kind: "loading" });
    void check();
  }

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
      if (isFinal(e)) forget();
      setState(failure(e, state.user));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div ref={box} tabIndex={-1} className="outline-none">
      {body()}
    </div>
  );

  function body() {
    if (state.kind === "loading") return <p role="status" className="text-sm text-ink-2">Checking your invitation…</p>;
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
          <div className="rounded bg-loss-soft px-3 py-2 text-loss">
            <p role="alert">{state.message}</p>
            {state.requestId && <p className="mt-0.5 text-xs">Request {state.requestId}</p>}
          </div>
          {state.user && <p className="text-ink-2">Signed in as {state.user.email}.</p>}
          <div className="flex flex-wrap gap-2">
            {state.again && (
              <button className="btn btn-primary" onClick={again}>
                Try again
              </button>
            )}
            {state.user && state.switchAccount && (
              <button className="btn btn-primary" onClick={signOutAndRetry}>
                Sign out and use another account
              </button>
            )}
            <Link href="/" className="btn btn-quiet">
              Go to QuantLab
            </Link>
          </div>
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
}
