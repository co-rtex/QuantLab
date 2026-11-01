"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "./api";

type Poll<T> = number | ((d: T | undefined) => number | false);

// The retry delay for a polling query whose first request failed transiently (#16).
const FIRST_RETRY_MS = 1000;

interface Loaded<T> {
  path: string | null;
  data?: T;
  error?: ApiError;
  /** Seconds until api() sends the request again: the server was busy (#223). */
  busy?: number;
}

/**
 * Fetch-on-mount hook with optional polling. `path=null` skips the request.
 * While a new path loads, the previous data stays visible only if it is the same resource
 * with another query string (a page, a filter, a refresh): no flash of empty content. Data
 * from another resource is never shown as this one's (#23, B7): switching workspace used
 * to show the old workspace's lists until the new ones arrived.
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
    let last: T | undefined; // last successful response for this path
    let failures = 0;
    const tick = async () => {
      let permanent = false;
      try {
        last = await api<T>(path, {
          onBusy: (busy) => {
            if (!cancelled) setState((s) => ({ path, data: s.path === path ? s.data : undefined, busy }));
          },
        });
        failures = 0;
        if (!cancelled) setState({ path, data: last });
      } catch (e) {
        const error = e instanceof ApiError ? e : new ApiError(0, "network", String(e));
        // 4xx (gone, forbidden, invalid) will not fix itself; network errors and 5xx may.
        permanent = error.status >= 400 && error.status < 500 && error.status !== 429;
        failures += 1;
        if (!cancelled) setState((s) => ({ path, data: s.path === path ? s.data : undefined, error }));
      }
      if (cancelled || permanent) return;
      const p = pollRef.current;
      // Decide from the last good data: a transient error used to pass `undefined` here,
      // which stopped function-based polls for good (jobs then looked stuck forever).
      let delay = typeof p === "function" ? p(last) : p;
      // With no good data yet a function poll cannot tell whether to go on, and stopped:
      // one failed first poll after "Run validation suite" left the panel blank while
      // the job finished (#16). A polling query keeps trying, with backoff.
      if (!delay && p !== undefined && failures && last === undefined) delay = FIRST_RETRY_MS;
      if (delay) timer = setTimeout(tick, failures ? Math.min(delay * 2 ** failures, 30_000) : delay);
    };
    tick();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [path, nonce]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  const current = state.path === path;
  const sameResource = path !== null && state.path !== null && resource(state.path) === resource(path);
  return {
    data: current || sameResource ? state.data : undefined,
    error: current ? state.error : undefined,
    busy: current ? state.busy : undefined,
    loading: path !== null && !current,
    reload,
  };
}

function resource(path: string): string {
  return path.split("?", 1)[0];
}
