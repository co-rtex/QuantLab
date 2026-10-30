"use client";

import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { api, type User, type Workspace } from "./api";

interface Ctx {
  user: User | undefined;
  workspaces: Workspace[];
  workspace: Workspace | undefined;
  setWorkspaceId: (id: string) => void;
  refresh: () => Promise<void>;
}

const WorkspaceContext = createContext<Ctx | null>(null);
const KEY = "quantlab.workspace";

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User>();
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [selected, setSelected] = useState<string | null>(null);

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
      .then((r) => alive && apply(r))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);

  const setWorkspaceId = (id: string) => {
    setSelected(id);
    try {
      localStorage.setItem(KEY, id);
    } catch {}
  };

  const workspace = workspaces.find((w) => w.id === selected);
  return (
    <WorkspaceContext.Provider value={{ user, workspaces, workspace, setWorkspaceId, refresh }}>
      {children}
    </WorkspaceContext.Provider>
  );
}

export function useWorkspace(): Ctx {
  const ctx = useContext(WorkspaceContext);
  if (!ctx) throw new Error("useWorkspace must be used inside WorkspaceProvider");
  return ctx;
}
