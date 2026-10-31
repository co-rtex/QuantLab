"use client";

import type { DatasetVersion } from "./api";
import { useApi } from "./useApi";
import { useWorkspace } from "./workspace";

/**
 * Whether a dataset version of the current workspace is synthetic, so that every list
 * that shows results (experiments, compare, ML, portfolios) can label them (#23): results
 * on synthetic data must never pass for real ones.
 */
export function useSynthetic(): (versionId: string | null | undefined) => boolean {
  const { workspace } = useWorkspace();
  const versions = useApi<DatasetVersion[]>(workspace ? `/workspaces/${workspace.id}/dataset-versions` : null);
  const synthetic = new Set(versions.data?.filter((v) => v.is_synthetic).map((v) => v.id));
  return (id) => !!id && synthetic.has(id);
}
