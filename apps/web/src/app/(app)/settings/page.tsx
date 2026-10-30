"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { PageHeader } from "@/components/Shell";
import { ErrorNote, Panel } from "@/components/ui";
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
  const members = useApi<Member[]>(ws ? `/workspaces/${ws}/members` : null);
  const audit = useApi<Page<AuditEvent>>(ws && isOwner ? `/workspaces/${ws}/audit?limit=30` : null);
  const [err, setErr] = useState<ApiError>();
  const [msg, setMsg] = useState<string>();

  async function invite(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget; // capture before await: React clears currentTarget
    const f = new FormData(form);
    setErr(undefined);
    try {
      await api(`/workspaces/${ws}/members`, { method: "POST", json: { email: f.get("email"), role: f.get("role") } });
      form.reset();
      members.reload();
      setMsg("Member added.");
    } catch (x) {
      setErr(x as ApiError);
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
        <Panel title="Members">
          <table className="data">
            <thead>
              <tr>
                <th>Name</th>
                <th>Email</th>
                <th>Role</th>
              </tr>
            </thead>
            <tbody>
              {members.data?.map((m) => (
                <tr key={m.user_id}>
                  <td>{m.display_name}</td>
                  <td className="text-ink-2">{m.email}</td>
                  <td>{m.role}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {isOwner && (
            <form onSubmit={invite} className="mt-4 flex flex-wrap items-end gap-2 text-sm">
              <label className="min-w-56 flex-1">
                <span className="mb-1 block">Add an existing user by email</span>
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
              <button className="btn btn-primary">Add member</button>
            </form>
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
          <div className="overflow-x-auto">
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
