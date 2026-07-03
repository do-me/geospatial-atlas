// Copyright (c) 2025 Apple Inc. Licensed under MIT License.

export interface EmbeddingViewConfig {
  /** Color scheme. */
  colorScheme?: "light" | "dark" | null;

  /** View mode. */
  mode?: "points" | "density" | null;

  /** Minimum average density for density contours to show up.
   * The density is measured as number of points per square points (aka., px in CSS units). */
  minimumDensity?: number | null;

  /** Override the automatically calculated point size.
   * If not specified, point size is calculated based on density. */
  pointSize?: number | null;

  /** Width of the survivor ring as a fraction of the point radius (0.1–1).
   * The ring extends outward beyond the point circle. Default 0.1. */
  survivorRingWidth?: number | null;

  /** Show each point's name (the `text` column) as a text label placed above
   * the dot. The names are collision-culled and revealed progressively as you
   * zoom in, so denser areas surface more names at higher zoom. Only the most
   * important `pointLabelsMaxCount` points are candidates. Default: false. */
  showPointLabels?: boolean | null;

  /** Maximum number of points that are candidates for name labels when
   * `showPointLabels` is on. Points are taken in `importance` order (if an
   * importance column is present) so the most significant names win.
   * Default: 2000. */
  pointLabelsMaxCount?: number | null;

  /** Generate labels automatically.
   * By default labels are generated automatically if the `labels` prop is not specified,
   * and a `text` column is specified in the Mosaic view,
   * or a `queryClusterLabels` callback is specified in the non-Mosaic view.
   * Set this to `false` to disable automatic labels. */
  autoLabelEnabled?: boolean | null;

  /** The density threshold to filter the clusters before generating automatic labels.
   * The value is relative to the max density. */
  autoLabelDensityThreshold?: number | null;

  /** The stop words for automatic label generation. By default use NLTK stop words. */
  autoLabelStopWords?: string[] | null;

  /** Approximate maximum number of points to render when downsampling is active.
   * Points are sampled with bias toward sparse regions (fewer points kept in dense areas).
   * The sampling probability is given by this formula:
   * P(i) = (downsampleMaxPoints / numPointsInViewport) * (2 / (1 + density(p_i) / maxDensity * downsampleDensityWeight))
   * Default: 4,000,000. Set to null or Infinity to disable downsampling. */
  downsampleMaxPoints?: number | null;

  /** Density weight for downsampling (0-10).
   * Higher values mean more aggressive culling in dense areas.
   * Default: 5 */
  downsampleDensityWeight?: number | null;

  /** Cap on points to render while the user is actively dragging or wheeling.
   * The configured `downsampleMaxPoints` still applies once the gesture
   * settles. Setting this drops frame time during pan on very large datasets
   * (75M-row Overture parquet goes from ~9 fps at 4M cap to ~25 fps at 200K
   * during the gesture, then snaps to full quality on release).
   * Default: 200,000. Set to null to disable adaptive capping. */
  downsampleMaxPointsInteractive?: number | null;
  /** Enable GIS mode (Mercator projection for the Y axis). */
  isGis?: boolean | null;

  /** MapLibre style URL. */
  mapStyle?: string | null;
}
