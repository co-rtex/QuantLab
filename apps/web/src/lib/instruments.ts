"use client";

import { useCallback, useEffect, useState } from "react";
import { api, ApiError, type Instrument, type Page } from "./api";

// The API serves at most this many instruments per request.
const PAGE = 2000;

interface Loaded {
  versionId: string | null;
  items?: Instrument[];
  error?: ApiError;
}

/**
 * Every instrument of a dataset version, fetched page by page (#23). A single
 * `limit=2000` request cut large catalogs short: the benchmark index could be missing from
 * the choices, and a default universe left out every ticker past the first page.
 * Unlike useApi, data from another version is never returned while this one loads.
 */
export function useInstruments(versionId: string | null) {
  const [state, setState] = useState<Loaded>({ versionId: null });
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    if (versionId === null) return;
    let alive = true;
    (async () => {
      try {
        const items: Instrument[] = [];
        for (;;) {
          const page = await api<Page<Instrument>>(`/dataset-versions/${versionId}/instruments?limit=${PAGE}&offset=${items.length}`);
          items.push(...page.items);
          if (!page.items.length || items.length >= page.total) break;
        }
        if (alive) setState({ versionId, items });
      } catch (e) {
        const error = e instanceof ApiError ? e : new ApiError(0, "network", String(e));
        if (alive) setState((s) => ({ versionId, items: s.versionId === versionId ? s.items : undefined, error }));
      }
    })();
    return () => {
      alive = false;
    };
  }, [versionId, nonce]);
  const reload = useCallback(() => setNonce((n) => n + 1), []);
  const current = versionId !== null && state.versionId === versionId;
  return {
    data: current && state.items ? { items: state.items, total: state.items.length } : undefined,
    error: current ? state.error : undefined,
    loading: versionId !== null && !current,
    reload,
  };
}
