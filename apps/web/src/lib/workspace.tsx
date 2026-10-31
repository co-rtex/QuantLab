"use client";

import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { api, ApiError, type User, type Workspace } from "./api";

interface Ctx {
  user: User | undefined;
  workspaces: Workspace[];
  workspace: Workspace | undefined;
  setWorkspaceId: (id: string) => void;
  refresh: () => Promise<void>;
  /** Why the account or its workspaces could not be loaded, if they could not (#18). */
  error: ApiError | undefined;
  retry: () => void;
}

const WorkspaceContext = createContext<Ctx | null>(null);
const KEY = "quantlab.workspace";

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User>();
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<ApiError>();
  const [attempt, setAttempt] = useState(0);

  const load = () => Promise.all([api<User>("/auth/me"), api<Workspace[]>("/workspaces")]);
  const apply = ([me, ws]: [User, Workspace[]]) => {
    setUser(me);
    setWorkspaces(ws);
    let stored: string | null = null;
    try {
      stored = localStorage.getItem(KEY);
    } catch {}
    setSelected((cur) => cur ?? ws.find((w) => w.id === stored)?.id ?? ws[0]?.id ?? null);
  };
  const refresh = async () => apply(await load());

  useEffect(() => {
    let alive = true;
    load()
      .then((r) => {
        if (!alive) return;
        setError(undefined);
        apply(r);
      })
      .catch((e) => {
        // Used to be swallowed, leaving an app with no user and no workspace and no reason.
        if (alive) setError(e instanceof ApiError ? e : new ApiError(0, "network", String(e)));
      });
    return () => {
      alive = false;
    };
  }, [attempt]);
  const retry = () => setAttempt((n) => n + 1);

  const setWorkspaceId = (id: string) => {
    setSelected(id);
    try {
      localStorage.setItem(KEY, id);
    } catch {}
  };

  const workspace = workspaces.find((w) => w.id === selected);
  return (
    <WorkspaceContext.Provider value={{ user, workspaces, workspace, setWorkspaceId, refresh, error, retry }}>
      {children}
    </WorkspaceContext.Provider>
  );
}

export function useWorkspace(): Ctx {
  const ctx = useContext(WorkspaceContext);
  if (!ctx) throw new Error("useWorkspace must be used inside WorkspaceProvider");
  return ctx;
}

/** Whether the current user may change things in the workspace (editor or owner), as the
 * API requires for running and cancelling jobs: viewers are not offered what they would
 * be refused (#107). */
export function useCanEdit(): boolean {
  const role = useWorkspace().workspace?.role;
  return role === "editor" || role === "owner";
}
