# Copyright (c) 2025 Apple Inc. Licensed under MIT License.

"""Compatibility imports for PageRank functions moved to graph_metrics."""

from .graph_metrics import compute_pagerank_column, knn_to_edges, pagerank

__all__ = ["compute_pagerank_column", "knn_to_edges", "pagerank"]
