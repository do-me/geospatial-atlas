// Copyright (c) 2025 Apple Inc. Licensed under MIT License.
//
// Register a zstd decoder with flechette so the Mosaic REST connector
// can decode IPC streams whose buffers are zstd-compressed.
//
// Why: the backend ``arrow_to_bytes`` writes IPC with
// ``IpcWriteOptions(compression="zstd")``. Without a registered codec,
// flechette's ``decompressBuffer`` calls ``codec.decode`` on a null
// codec and throws. The compression cuts the wire from raw u32 to
// ~10 % — at the 322 M eubucco file the scatter response goes
// 2.58 GB → ~250 MB, which is what gets us under Chrome's
// ``Response.arrayBuffer()`` ceiling so the points actually render.
//
// The registration is a global side effect against flechette's
// module-level codec table, so this file must be imported BEFORE any
// Mosaic query runs. The component package's ``lib/index.ts`` does
// the side-effect import at the top so any consumer that depends on
// ``@embedding-atlas/component`` (viewer, anywidget, streamlit,
// desktop, …) inherits the codec automatically.

import { CompressionType, setCompressionCodec } from "@uwdata/flechette";
import { decompress } from "fzstd";

let registered = false;

export function registerZstdCodec(): void {
  if (registered) return;
  registered = true;
  setCompressionCodec(CompressionType.ZSTD, {
    // flechette hands us the raw compressed bytes for one Arrow buffer
    // (already past its 8-byte uncompressed-length prefix). ``fzstd``'s
    // ``decompress`` returns a ``Uint8Array`` of the inflated payload —
    // exactly the shape flechette expects back.
    decode: (bytes: Uint8Array) => decompress(bytes),
    // The encode path is unused on the client; throw to make a
    // mis-call obvious instead of silently producing wrong bytes.
    encode: () => {
      throw new Error("[atlas] zstd encode not supported on client");
    },
  });
}

// Module-level side effect: register on import. Any consumer that
// imports anything from ``@embedding-atlas/component`` triggers the
// registration before its own Mosaic queries fire.
registerZstdCodec();
