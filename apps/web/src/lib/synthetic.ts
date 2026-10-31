"use client";

import type { SyntheticStatus } from "@/components/ui";
import type { DatasetVersion } from "./api";
import { useApi } from "./useApi";
import { useWorkspace } from "./workspace";

/** A result row that can say whether its dataset is synthetic. */
export interface SyntheticRow {
  // From the API since #106: null when the dataset could not be checked.
  is_synthetic?: boolean | null;
  dataset_version_id?: string | null;
}

/**
 * Whether results on the given rows come from synthetic data, so that every list that
 * shows results (experiments, compare, ML, portfolios) labels them (#23): results on
 * synthetic data must never pass for real ones. A row's own `is_synthetic` decides; null
 * is "unknown". Only rows from an API without the field are looked up in the workspace's
 * dataset versions, and then "real" only for a version positively marked as not
 * synthetic: "unknown" while that list loads, after it failed, or when the version is
 * not in it.
 */
export function useSynthetic(rows: readonly SyntheticRow[] | undefined): (row: SyntheticRow | undefined) => SyntheticStatus {
  const { workspace } = useWorkspace();
  const lookup = !!rows?.some((r) => r.is_synthetic === undefined);
  const versions = useApi<DatasetVersion[]>(workspace && lookup ? `/workspaces/${workspace.id}/dataset-versions` : null);
  const known = new Map(versions.data?.map((v) => [v.id, v.is_synthetic]));
  return (row) => {
    if (!row) return "unknown";
    if (row.is_synthetic !== undefined) return row.is_synthetic === true ? "synthetic" : row.is_synthetic === false ? "real" : "unknown";
    const s = row.dataset_version_id ? known.get(row.dataset_version_id) : undefined;
    return s === undefined ? "unknown" : s ? "synthetic" : "real";
  };
}

/** The label for several results together: synthetic if any is, else unknown if any is. */
export function combined(statuses: SyntheticStatus[]): SyntheticStatus {
  return statuses.includes("synthetic") ? "synthetic" : statuses.includes("unknown") ? "unknown" : "real";
}
