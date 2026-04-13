// Copyright (c) 2025 Apple Inc. Licensed under MIT License.

import type { AsyncDuckDB, AsyncDuckDBConnection } from "@duckdb/duckdb-wasm";

import type { Logger } from "./logging.js";

/** Type for window.EMBEDDING_ATLAS_CONFIG. Set this in index.html to configure the application. */
export interface AppConfig {
  home: "file-viewer" | "backend-viewer";

  /**
   * Load data from a given URL. Return types:
   * - { data, filename }: Loaded binary data and file name with extension (e.g., data.parquet)
   * - { url }: A new URL to load with regular fetch()
   * - `undefined`: Load the existing URL with regular fetch()
   * - `true`: The data has been imported into the given table.
   */
  loadDataFromUrl?: (
    url: string,
    context: {
      table: string;
      db: AsyncDuckDB;
      connection: AsyncDuckDBConnection;
      fetch: (url: string) => Promise<Uint8Array<ArrayBuffer>>;
      logger?: Logger;
    },
  ) => Promise<
    { data: Uint8Array<ArrayBuffer>; filename?: string } | { url: string; filename?: string } | undefined | true
  >;
}

export function resolveAppConfig(): AppConfig {
  let config: Partial<AppConfig> = {};
  if (typeof window != undefined && (window as any).EMBEDDING_ATLAS_CONFIG != undefined) {
    config = (window as any).EMBEDDING_ATLAS_CONFIG;
  }
  return {
    home: "backend-viewer",
    ...config,
  };
}

/**
 * Auto-detect runtime mode by probing for the backend metadata endpoint.
 * Returns "backend-viewer" if the backend is serving, "file-viewer" otherwise.
 */
export async function detectHome(): Promise<AppConfig["home"]> {
  try {
    const res = await fetch("./data/metadata.json");
    if (res.ok) return "backend-viewer";
  } catch {
    // no backend
  }
  return "file-viewer";
}
