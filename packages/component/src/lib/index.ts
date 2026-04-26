// Copyright (c) 2025 Apple Inc. Licensed under MIT License.

// Side-effect import: registers a zstd codec with flechette so the
// Mosaic IPC decoder can inflate the compressed Arrow buffers that
// the backend now emits. Must precede any Mosaic query — see
// ``ipc_codec.ts`` for the wire-cap rationale.
import "./ipc_codec.js";

export {
  EmbeddingView,
  EmbeddingViewMosaic,
  maxDensityModeCategories,
  type EmbeddingViewMosaicProps,
  type EmbeddingViewProps,
} from "./embedding_view/api.js";

export { defaultCategoryColors } from "./colors.js";
export { streamingRestConnector, type StreamingRestConnectorOptions } from "./streaming_rest_connector.js";
export { registerZstdCodec } from "./ipc_codec.js";

export type { EmbeddingViewConfig } from "./embedding_view/embedding_view_config.js";
export type { EmbeddingViewTheme } from "./embedding_view/theme.js";
export type {
  CustomComponent,
  DataField,
  DataPoint,
  DataPointID,
  Label,
  LabelContent,
  OverlayProxy,
} from "./embedding_view/types.js";
export type { Point, Rectangle, ViewportState } from "./utils.js";
