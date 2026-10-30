"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "./api";

type Poll<T> = number | ((d: T | undefined) => number | false);

interface Loaded<T> {
  path: string | null;
  data?: T;
  error?: ApiError;
}

/**
 * Fetch-on-mount hook with optional polling. `path=null` skips the request.
 * Previous data stays visible while a new path loads (no flash of empty content).
 */
export function useApi<T>(path: string | null, opts: { poll?: Poll<T> } = {}) {
  const [state, setState] = useState<Loaded<T>>({ path: null });
  const [nonce, setNonce] = useState(0);
  const pollRef = useRef<Poll<T> | undefined>(opts.poll);
  useEffect(() => {
    pollRef.current = opts.poll;
  });

  useEffect(() => {
    if (path === null) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      let data: T | undefined;
      try {
        data = await api<T>(path);
        if (!cancelled) setState({ path, data });
      } catch (e) {
        const error = e instanceof ApiError ? e : new ApiError(0, "network", String(e));
        if (!cancelled) setState((s) => ({ path, data: s.path === path ? s.data : undefined, error }));
      }
      if (cancelled) return;
      const p = pollRef.current;
      const delay = typeof p === "function" ? p(data) : p;
      if (delay) timer = setTimeout(tick, delay);
    };
    tick();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [path, nonce]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  const current = state.path === path;
  return {
    data: path === null ? undefined : state.data,
    error: current ? state.error : undefined,
    loading: path !== null && !current,
    reload,
  };
}
