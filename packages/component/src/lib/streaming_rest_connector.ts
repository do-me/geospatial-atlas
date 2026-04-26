// Copyright (c) 2025 Apple Inc. Licensed under MIT License.
//
// Mosaic-core REST connector that streams the arrow response body
// instead of calling ``Response.arrayBuffer()``.
//
// Why: ``Response.arrayBuffer()`` aborts past ~2 GB in Chrome — the
// fetch pipeline's intermediate Mojo IPC buffer caps lower than the
// underlying V8 ``ArrayBuffer`` allocator. The 322 M-row eubucco file
// produces a 2.6 GB raw u32 scatter response (or 2.1 GB after server-
// side zstd). Reading via ``response.body.getReader()`` and copying
// chunks into a single pre-allocated ``Uint8Array`` of the
// Content-Length size goes straight through V8 (which supports up to
// ~4 GB ArrayBuffers on modern Chrome), so the 2 GB ceiling lifts.
//
// Falls back to the upstream ``restConnector`` for non-arrow queries
// (json/exec) where the response is small and there's no win to be had.

import { restConnector, type Connector } from "@uwdata/mosaic-core";
import { tableFromIPC } from "@uwdata/flechette";

/** Same shape as Mosaic's ``restConnector`` options. */
export interface StreamingRestConnectorOptions {
  /** Base URL of the DuckDB REST endpoint (e.g. ``http://127.0.0.1:5088/data/query``). */
  uri: string;
  /** Flechette IPC extraction options. Forwarded to ``tableFromIPC``. */
  ipc?: Parameters<typeof tableFromIPC>[1];
}

/** Read a fetch ``Response`` body as a single ``Uint8Array``, allocating
 *  once with the ``Content-Length`` and copying chunks in place. Avoids
 *  ``Response.arrayBuffer()``'s stricter Chrome cap. */
async function readBodyToUint8Array(res: Response): Promise<Uint8Array> {
  const contentLengthHeader = res.headers.get("Content-Length");
  const contentLength = contentLengthHeader != null ? Number(contentLengthHeader) : NaN;
  const reader = res.body!.getReader();
  if (Number.isFinite(contentLength) && contentLength > 0) {
    // Known size: one allocation, no concatenation pass. The ~4 GB V8
    // ArrayBuffer cap is the only limit; ``Response.arrayBuffer()`` was
    // capped much lower by the fetch impl's intermediate Mojo buffer.
    const buf = new Uint8Array(contentLength);
    let offset = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (value) {
        buf.set(value, offset);
        offset += value.length;
      }
    }
    if (offset !== contentLength) {
      // Server lied about Content-Length; fall back to a sized copy of
      // what we actually got (truncate the alloc by re-slicing).
      console.warn(
        `[atlas-stream] Content-Length=${contentLength} but received ${offset} bytes — truncating`,
      );
      return buf.subarray(0, offset);
    }
    return buf;
  }
  // No Content-Length (chunked transfer with no advertised total).
  // Accumulate chunks then concatenate once at the end. Pays a 2× peak
  // residency but on responses small enough to lack Content-Length the
  // peak is irrelevant.
  const chunks: Uint8Array[] = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    if (value) {
      chunks.push(value);
      total += value.length;
    }
  }
  const buf = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    buf.set(chunk, offset);
    offset += chunk.length;
  }
  return buf;
}

/** Mosaic-core connector with the streaming-arrow override. Initialise
 *  it the same way as ``restConnector`` and pass the result to
 *  ``coordinator.databaseConnector(...)``. */
export function streamingRestConnector(options: StreamingRestConnectorOptions): Connector {
  const baseConnector = restConnector({ uri: options.uri, ipc: options.ipc });
  const ipcOptions = options.ipc;
  return {
    query: async (query: any) => {
      // Non-arrow queries (small JSON / exec) go through the upstream
      // connector unchanged. Only arrow responses are at risk of the
      // 2 GB ceiling.
      if (query?.type !== "arrow") {
        return baseConnector.query(query);
      }
      const res = await fetch(options.uri, {
        method: "POST",
        mode: "cors",
        credentials: "omit",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(query),
      });
      if (!res.ok) {
        throw new Error(
          `[atlas-stream] arrow query failed with HTTP ${res.status}: ${await res.text()}`,
        );
      }
      const bytes = await readBodyToUint8Array(res);
      return tableFromIPC(bytes, ipcOptions);
    },
  };
}
