"use client";

import type { SyntheticStatus } from "@/components/ui";
import type { DatasetVersion } from "./api";
import { useApi } from "./useApi";
import { useWorkspace } from "./workspace";

/**
 * Whether a dataset version of the current workspace is synthetic, so that every list
 * that shows results (experiments, compare, ML, portfolios) can label them (#23): results
 * on synthetic data must never pass for real ones. "real" only for a version the
 * workspace's list positively marks as not synthetic; "unknown" while the list loads,
 * after it failed, or when the version is not in it.
 */
export function useSynthetic(): (versionId: string | null | undefined) => SyntheticStatus {
  const { workspace } = useWorkspace();
  const versions = useApi<DatasetVersion[]>(workspace ? `/workspaces/${workspace.id}/dataset-versions` : null);
  const known = new Map(versions.data?.map((v) => [v.id, v.is_synthetic]));
  return (id) => {
    const s = id ? known.get(id) : undefined;
    return s === undefined ? "unknown" : s ? "synthetic" : "real";
  };
}

/** The label for several results together: synthetic if any is, else unknown if any is. */
export function combined(statuses: SyntheticStatus[]): SyntheticStatus {
  return statuses.includes("synthetic") ? "synthetic" : statuses.includes("unknown") ? "unknown" : "real";
}
