"""Regression coverage for upstream CLI options on the GIS loading paths."""

from unittest.mock import Mock

import pandas as pd
import pytest
from click.testing import CliRunner

from embedding_atlas import cli
from embedding_atlas.graph_metrics import knn_to_edges


def test_state_reaches_fast_path_metadata(tmp_path, monkeypatch):
    path = tmp_path / "points.parquet"
    pd.DataFrame({"lon": [1.0, 2.0], "lat": [3.0, 4.0]}).to_parquet(path)
    make_server = Mock()
    monkeypatch.setattr("embedding_atlas.server.make_server", make_server)
    monkeypatch.setattr("uvicorn.run", Mock())
    result = CliRunner().invoke(
        cli.main,
        [
            str(path),
            "--state",
            '{"charts":{"custom":{"type":"instances"}}}',
            "--no-auto-port",
        ],
    )
    assert result.exit_code == 0, result.output
    metadata = make_server.call_args.args[0].metadata
    assert metadata["props"]["initialState"] == {
        "charts": {"custom": {"type": "instances"}}
    }
    assert metadata["props"]["data"]["projection"]["isGis"] is True


@pytest.mark.parametrize(
    ("flag", "columns"),
    [
        ("--degree", {"in_degree", "out_degree", "degree"}),
        ("--kcore", {"kcore"}),
        ("--clustering", {"clustering"}),
    ],
)
def test_graph_metrics_are_not_silently_skipped_by_gis_fast_path(
    tmp_path, monkeypatch, flag, columns
):
    path = tmp_path / "neighbors.parquet"
    pd.DataFrame(
        {
            "lon": [1.0, 2.0, 3.0],
            "lat": [4.0, 5.0, 6.0],
            "neighbors": [
                {"ids": [1, 2], "distances": [0.1, 0.2]},
                {"ids": [0, 2], "distances": [0.1, 0.2]},
                {"ids": [0, 1], "distances": [0.1, 0.2]},
            ],
        }
    ).to_parquet(path)
    fast_load = Mock(side_effect=AssertionError("graph metrics need the full path"))
    make_server = Mock()
    monkeypatch.setattr(cli, "_try_fast_load", fast_load)
    monkeypatch.setattr(cli, "make_server", make_server)
    monkeypatch.setattr("uvicorn.run", Mock())
    result = CliRunner().invoke(
        cli.main,
        [str(path), "--neighbors", "neighbors", flag, "--no-auto-port"],
    )
    assert result.exit_code == 0, result.output
    fast_load.assert_not_called()
    dataset = make_server.call_args.args[0].dataset
    assert columns <= set(dataset.columns)
    assert len(dataset) == 3


def test_legacy_pagerank_import_uses_guarded_graph_metrics():
    from embedding_atlas.pagerank import knn_to_edges as legacy_knn_to_edges

    assert legacy_knn_to_edges is knn_to_edges
