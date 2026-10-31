"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { PageHeader } from "@/components/Shell";
import { ErrorNote, Panel, QueryError } from "@/components/ui";
import { api, ApiError, type Page } from "@/lib/api";
import { dateTime } from "@/lib/format";
import { useApi } from "@/lib/useApi";
import { useWorkspace } from "@/lib/workspace";

interface Member {
  user_id: string;
  email: string;
  display_name: string;
  role: string;
}
interface Invitation {
  id: string;
  email: string;
  role: string;
  status: string;
  expires_at: string;
}
interface Created extends Invitation {
  accept_path: string;
}
interface AuditEvent {
  id: string;
  action: string;
  target_type: string | null;
  target_id: string | null;
  created_at: string;
  request_id: string | null;
}

export default function SettingsPage() {
  const router = useRouter();
  const { workspace, user, refresh, setWorkspaceId } = useWorkspace();
  const ws = workspace?.id;
  const isOwner = workspace?.role === "owner";
  const canEdit = isOwner || workspace?.role === "editor";
  const members = useApi<Member[]>(ws ? `/workspaces/${ws}/members` : null);
  const audit = useApi<Page<AuditEvent>>(ws && isOwner ? `/workspaces/${ws}/audit?limit=30` : null);
  const invitations = useApi<Invitation[]>(ws && isOwner ? `/workspaces/${ws}/invitations` : null);
  // The accept link is returned once, when the invitation is created.
  const [link, setLink] = useState<{ email: string; url: string }>();
  const [err, setErr] = useState<ApiError>();
  const [msg, setMsg] = useState<string>();
  const [removing, setRemoving] = useState<Member>(); // member awaiting confirmation
  const [removeErr, setRemoveErr] = useState<ApiError>();
  const [confirmName, setConfirmName] = useState("");
  const [pending, setPending] = useState<"rename" | "delete" | "remove">();
  const confirmBox = useRef<HTMLDivElement>(null);
  const membersHeading = useRef<HTMLHeadingElement>(null);
  // Focus follows the confirmation step: into it when it opens (its prompt, so a repeated
  // Enter cannot confirm), and back to the row's Remove button when it closes (#105).
  const returnTo = useRef<string | null>(null);
  useEffect(() => {
    if (removing) {
      confirmBox.current?.focus();
      return;
    }
    const id = returnTo.current;
    if (id === null) return;
    returnTo.current = null;
    // "" is the heading: after a removal the row, and its button, are about to go.
    const back = id ? document.querySelector<HTMLElement>(`[data-remove="${CSS.escape(id)}"]`) : null;
    (back ?? membersHeading.current)?.focus();
  }, [removing]);

  function openRemove(m: Member) {
    setRemoveErr(undefined);
    setRemoving(m);
  }
  function closeRemove(removed = false) {
    if (removing) returnTo.current = removed ? "" : removing.user_id;
    setRemoving(undefined);
  }

  async function invite(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget; // capture before await: React clears currentTarget
    const f = new FormData(form);
    setErr(undefined);
    try {
      const created = await api<Created>(`/workspaces/${ws}/invitations`, {
        method: "POST",
        json: { email: f.get("email"), role: f.get("role") },
      });
      form.reset();
      invitations.reload();
      setLink({ email: created.email, url: new URL(created.accept_path, window.location.origin).href });
      setMsg(`Invitation created for ${created.email}. They join once they accept it.`);
    } catch (x) {
      setErr(x as ApiError);
    }
  }

  async function revoke(id: string) {
    setErr(undefined);
    try {
      await api(`/workspaces/${ws}/invitations/${id}`, { method: "DELETE" });
      invitations.reload();
      setMsg("Invitation withdrawn.");
    } catch (x) {
      setErr(x as ApiError);
    }
  }

  async function setRole(m: Member, role: string) {
    setErr(undefined);
    try {
      await api(`/workspaces/${ws}/members/${m.user_id}`, { method: "PATCH", json: { role } });
      setMsg(`${m.display_name} is now ${role === "owner" ? "an" : "a"} ${role}.`);
      await refresh();
    } catch (x) {
      setErr(x as ApiError);
    }
    members.reload();
  }

  /** After leaving or deleting the current workspace, select one that is still there. */
  async function selectRemaining() {
    const left = await api<{ id: string }[]>("/workspaces");
    await refresh();
    setWorkspaceId(left[0]?.id ?? null);
  }

  async function removeMember(m: Member) {
    if (pending) return; // one request at a time: a double click must not send two
    setErr(undefined);
    setRemoveErr(undefined);
    setPending("remove");
    try {
      await api(`/workspaces/${ws}/members/${m.user_id}`, { method: "DELETE" });
      closeRemove(true);
      if (m.user_id === user?.id) {
        await selectRemaining();
        setMsg(`You left ${workspace?.name}.`);
        return;
      }
      setMsg(`${m.display_name} was removed from ${workspace?.name}.`);
      members.reload();
    } catch (x) {
      setRemoveErr(x as ApiError); // beside the confirmation, e.g. the last owner (409)
      setErr(x as ApiError);
    } finally {
      setPending(undefined);
    }
  }

  async function rename(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (pending) return;
    setErr(undefined);
    const name = String(new FormData(e.currentTarget).get("name") ?? "").trim();
    setPending("rename");
    try {
      await api(`/workspaces/${ws}`, { method: "PATCH", json: { name, description: workspace?.description ?? "" } });
      await refresh();
      setMsg(`Workspace renamed to ${name}.`);
    } catch (x) {
      setErr(x as ApiError);
    } finally {
      setPending(undefined);
    }
  }

  async function deleteWorkspace(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (pending || !workspace || confirmName !== workspace.name) return;
    setErr(undefined);
    setPending("delete");
    try {
      await api(`/workspaces/${ws}`, { method: "DELETE" });
      setConfirmName("");
      await selectRemaining();
      setMsg(`Workspace ${workspace.name} was deleted, with its data, experiments and portfolios.`);
    } catch (x) {
      setErr(x as ApiError);
    } finally {
      setPending(undefined);
    }
  }

  async function createWorkspace(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    try {
      const w = await api<{ id: string }>("/workspaces", { method: "POST", json: { name: f.get("name"), description: "" } });
      await refresh();
      setWorkspaceId(w.id);
      setMsg("Workspace created.");
    } catch (x) {
      setErr(x as ApiError);
    }
  }

  async function signOutEverywhere() {
    await api("/auth/logout?everywhere=true", { method: "POST" });
    router.push("/login");
  }

  return (
    <>
      <PageHeader title="Workspace and account" lede={workspace ? `You are ${workspace.role === "owner" ? "an" : "a"} ${workspace.role} of ${workspace.name}.` : undefined} />
      {msg && (
        <p role="status" className="mb-4 rounded bg-gain-soft px-3 py-2 text-sm text-gain">
          {msg}
        </p>
      )}
      <div className="mb-4">
        <ErrorNote error={err} />
      </div>
      <div className="grid gap-6 xl:grid-cols-2">
        <Panel title={<span ref={membersHeading} tabIndex={-1} className="outline-none">Members</span>}>
          <QueryError q={members} what="members" />
          <table className="data">
            <thead>
              <tr>
                <th>Name</th>
                <th>Email</th>
                <th>Role</th>
                {isOwner && <th className="sr-only">Actions</th>}
              </tr>
            </thead>
            <tbody>
              {members.data?.map((m) => (
                <tr key={m.user_id}>
                  <td>{m.display_name}</td>
                  <td className="text-ink-2">{m.email}</td>
                  <td>
                    {isOwner ? (
                      <select
                        aria-label={`Role of ${m.display_name}`}
                        value={m.role}
                        onChange={(e) => setRole(m, e.target.value)}
                        className="field py-0.5"
                      >
                        <option value="viewer">viewer</option>
                        <option value="editor">editor</option>
                        <option value="owner">owner</option>
                      </select>
                    ) : (
                      m.role
                    )}
                  </td>
                  {isOwner && (
                    <td className="r whitespace-nowrap">
                      <button
                        type="button"
                        className="btn btn-quiet py-0.5"
                        data-remove={m.user_id}
                        aria-label={m.user_id === user?.id ? "Leave this workspace" : `Remove ${m.display_name}`}
                        aria-expanded={removing?.user_id === m.user_id}
                        aria-controls="remove-member"
                        onClick={() => openRemove(m)}
                      >
                        {m.user_id === user?.id ? "Leave" : "Remove"}
                      </button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
          {isOwner && removing && (
            <div
              id="remove-member"
              ref={confirmBox}
              role="group"
              aria-labelledby="remove-member-prompt"
              tabIndex={-1}
              onKeyDown={(e) => {
                if (e.key === "Escape") closeRemove();
              }}
              className="mt-3 rounded border border-loss/30 bg-loss-soft p-3 text-sm outline-none"
            >
              <p id="remove-member-prompt" className="text-loss">
                {removing.user_id === user?.id
                  ? `Leave ${workspace?.name}? You lose access to it immediately.`
                  : `Remove ${removing.display_name} from ${workspace?.name}? They lose access immediately.`}
              </p>
              {removeErr && <p className="mt-1 text-loss">{removeErr.message}</p>}
              <div className="mt-2 flex flex-wrap gap-2">
                <button
                  type="button"
                  className="btn btn-quiet text-loss"
                  aria-busy={pending === "remove"}
                  aria-disabled={pending === "remove"}
                  onClick={() => removeMember(removing)}
                >
                  {removing.user_id === user?.id ? "Confirm leaving" : `Confirm removing ${removing.display_name}`}
                </button>
                <button type="button" className="btn btn-quiet" onClick={() => closeRemove()}>
                  {removing.user_id === user?.id ? "Stay" : `Keep ${removing.display_name}`}
                </button>
              </div>
            </div>
          )}
          {isOwner && (
            <form onSubmit={invite} className="mt-4 flex flex-wrap items-end gap-2 text-sm">
              <label className="min-w-56 flex-1">
                <span className="mb-1 block">Invite by email</span>
                <input name="email" type="email" required className="field" />
              </label>
              <label>
                <span className="mb-1 block">Role</span>
                <select name="role" className="field">
                  <option value="viewer">Viewer</option>
                  <option value="editor">Editor</option>
                  <option value="owner">Owner</option>
                </select>
              </label>
              <button className="btn btn-primary">Send invitation</button>
            </form>
          )}
          {isOwner && link && (
            <div className="mt-3 rounded border border-rule p-3 text-sm">
              <label htmlFor="invite-link" className="mb-1 block">
                Send this link to {link.email}. It is shown only once and works only for that address.
              </label>
              <div className="flex gap-2">
                <input id="invite-link" readOnly value={link.url} className="field flex-1 font-mono text-xs" />
                <button type="button" className="btn btn-quiet" onClick={() => navigator.clipboard?.writeText(link.url)}>
                  Copy
                </button>
              </div>
            </div>
          )}
          {isOwner && (
            <div className="mt-4">
              <QueryError q={invitations} what="pending invitations" />
            </div>
          )}
          {isOwner && invitations.data && invitations.data.length > 0 && (
            <div className="mt-4">
              <h3 className="mb-1 text-sm font-semibold">Pending invitations</h3>
              <table className="data">
                <thead>
                  <tr>
                    <th>Email</th>
                    <th>Role</th>
                    <th>Expires</th>
                    <th>
                      <span className="sr-only">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {invitations.data.map((i) => (
                    <tr key={i.id}>
                      <td className="text-ink-2">{i.email}</td>
                      <td>{i.role}</td>
                      <td>{i.status === "expired" ? "expired" : dateTime(i.expires_at)}</td>
                      <td>
                        <button className="btn btn-quiet py-0.5" onClick={() => revoke(i.id)}>
                          Withdraw
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <p className="mt-3 text-xs text-ink-3">Viewers can read results. Editors can run experiments. Owners manage members and see the audit log.</p>
        </Panel>
        <div className="space-y-6">
          <Panel title="Account">
            <dl className="space-y-1 text-sm">
              <div className="flex gap-2">
                <dt className="w-20 text-ink-2">Name</dt>
                <dd>{user?.display_name}</dd>
              </div>
              <div className="flex gap-2">
                <dt className="w-20 text-ink-2">Email</dt>
                <dd>{user?.email}</dd>
              </div>
            </dl>
            <button onClick={signOutEverywhere} className="btn btn-quiet mt-4">
              Sign out on all devices
            </button>
          </Panel>
          {workspace && canEdit && (
            <Panel title="This workspace">
              <form onSubmit={rename} className="flex gap-2 text-sm" key={workspace.id}>
                <label className="flex-1">
                  <span className="mb-1 block">Name</span>
                  <input name="name" required maxLength={120} defaultValue={workspace.name} className="field" />
                </label>
                <button className="btn btn-quiet self-end" aria-busy={pending === "rename"} aria-disabled={pending === "rename"}>
                  {pending === "rename" ? "Renaming…" : "Rename"}
                </button>
              </form>
              {isOwner && (
                <form onSubmit={deleteWorkspace} className="mt-5 space-y-2 border-t border-rule pt-4 text-sm">
                  <p className="text-ink-2">
                    Deleting the workspace removes its datasets, experiments, ML runs and portfolios for every member. It cannot be undone.
                  </p>
                  <label className="block">
                    <span className="mb-1 block">Type {workspace.name} to confirm</span>
                    <input value={confirmName} onChange={(e) => setConfirmName(e.target.value)} className="field" autoComplete="off" />
                  </label>
                  <button
                    className="btn btn-quiet text-loss"
                    disabled={confirmName !== workspace.name}
                    aria-busy={pending === "delete"}
                    aria-disabled={pending === "delete" || undefined}
                  >
                    {pending === "delete" ? "Deleting…" : "Delete workspace"}
                  </button>
                </form>
              )}
            </Panel>
          )}
          <Panel title="New workspace">
            <form onSubmit={createWorkspace} className="flex gap-2 text-sm">
              <label className="flex-1">
                <span className="sr-only">Workspace name</span>
                <input name="name" required maxLength={120} placeholder="Workspace name" className="field" />
              </label>
              <button className="btn btn-quiet">Create workspace</button>
            </form>
          </Panel>
        </div>
      </div>
      {isOwner && (
        <Panel title="Audit log" className="mt-6" aside="Sensitive actions in this workspace">
          <QueryError q={audit} what="the audit log" />
          <div className="overflow-x-auto" tabIndex={0}>
            <table className="data num">
              <thead>
                <tr>
                  <th>When</th>
                  <th>Action</th>
                  <th>Target</th>
                  <th>Request</th>
                </tr>
              </thead>
              <tbody>
                {audit.data?.items.map((a) => (
                  <tr key={a.id}>
                    <td>{dateTime(a.created_at)}</td>
                    <td>{a.action}</td>
                    <td className="text-ink-2">{a.target_type ? `${a.target_type} ${a.target_id?.slice(0, 8) ?? ""}` : "—"}</td>
                    <td className="text-xs text-ink-3">{a.request_id?.slice(0, 12)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      )}
    </>
  );
}
