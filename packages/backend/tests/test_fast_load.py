"""Tests for ``fast_load_parquet``.

The loader must:
  * materialise the parquet into a DuckDB TABLE (not a view) — the
    viewer issues ``ALTER TABLE ... ADD COLUMN`` on every color-by
    click, which fails on a view.
  * provide a stable per-row id under ``id_column`` projected from the
    parquet reader's ``file_row_number`` virtual column, with no
    follow-on ``ALTER TABLE ... ADD COLUMN`` + ``UPDATE rowid`` pass.
  * compute correct bounds and row count.
  * survive a parquet schema that already contains ``file_row_number``
    (collision fallback to ``ROW_NUMBER() OVER ()``).
  * handle ALTER TABLE / UPDATE (the viewer's color-by path).
"""

from __future__ import annotations

import struct

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from embedding_atlas.fast_load import fast_load_parquet


@pytest.fixture
def latlon_parquet(tmp_path):
    path = tmp_path / "small.parquet"
    n = 1000
    table = pa.table(
        {
            "id": pa.array([f"row-{i}" for i in range(n)]),
            "lat": pa.array([(i % 180) - 90 + 0.5 for i in range(n)], type=pa.float64()),
            "lon": pa.array([(i % 360) - 180 + 0.25 for i in range(n)], type=pa.float64()),
            "name": pa.array([f"name-{i}" for i in range(n)]),
        }
    )
    pq.write_table(table, str(path))
    return path, n


@pytest.fixture
def wkb_parquet(tmp_path):
    """Parquet with a WKB Point geometry column."""
    path = tmp_path / "wkb.parquet"
    n = 50

    def point_wkb(x, y):
        # little-endian Point: byte_order=1, geom_type=1 (Point), then x,y as f64.
        return struct.pack("<BIdd", 1, 1, x, y)

    table = pa.table(
        {
            "id": pa.array([f"r{i}" for i in range(n)]),
            "geometry": pa.array([point_wkb(i * 0.1, i * 0.1 + 5) for i in range(n)]),
        }
    )
    pq.write_table(table, str(path))
    return path, n


def test_loads_table_not_view(latlon_parquet):
    path, n = latlon_parquet
    res = fast_load_parquet(str(path))

    # Must be a TABLE — the viewer's color-by issues ALTER TABLE on it.
    kind = res.connection.sql(
        f"SELECT table_type FROM information_schema.tables "
        f"WHERE table_name = '{res.table}'"
    ).fetchone()
    assert kind is not None and kind[0] == "BASE TABLE"

    assert res.row_count == n
    assert res.x_column == "lon"
    assert res.y_column == "lat"
    assert res.id_column == "__row_index__"
    assert res.x_bounds is not None
    assert res.y_bounds is not None


def test_table_supports_alter_add_column(latlon_parquet):
    """The viewer's color-by clicks issue ALTER + UPDATE — they must
    succeed against the loader's output table."""
    path, n = latlon_parquet
    res = fast_load_parquet(str(path))
    con = res.connection
    con.execute(
        f'ALTER TABLE "{res.table}" ADD COLUMN __ev_test_id INTEGER DEFAULT 0'
    )
    con.execute(
        f'UPDATE "{res.table}" '
        f'SET __ev_test_id = CASE WHEN lat > 0 THEN 1 ELSE 0 END'
    )
    counts = con.sql(
        f'SELECT __ev_test_id, COUNT(*) FROM "{res.table}" GROUP BY __ev_test_id'
    ).fetchall()
    assert sum(c for _, c in counts) == n


def test_id_column_is_zero_based_and_dense(latlon_parquet):
    path, n = latlon_parquet
    res = fast_load_parquet(str(path))
    con = res.connection
    rows = con.sql(
        f'SELECT "{res.id_column}" FROM "{res.table}" ORDER BY "{res.id_column}"'
    ).fetchall()
    assert [r[0] for r in rows] == list(range(n))


def test_view_query_after_load(latlon_parquet):
    path, n = latlon_parquet
    res = fast_load_parquet(str(path))
    con = res.connection

    # Mosaic-style scatter projection — viewer's hot path.
    out = con.sql(
        f'SELECT lon, lat, "{res.id_column}" FROM "{res.table}" '
        f'WHERE "{res.id_column}" < 5 ORDER BY "{res.id_column}"'
    ).fetchall()
    assert len(out) == 5
    assert out[0][2] == 0
    assert out[4][2] == 4

    # Filter pushdown to specific id.
    row = con.sql(
        f'SELECT id, lon, lat FROM "{res.table}" WHERE "{res.id_column}" = 42'
    ).fetchone()
    assert row[0] == "row-42"


def test_bounds_match_data(latlon_parquet):
    path, n = latlon_parquet
    res = fast_load_parquet(str(path))
    x_min, x_max = res.x_bounds  # type: ignore[misc]
    y_min, y_max = res.y_bounds  # type: ignore[misc]
    # lon = (i % 360) - 180 + 0.25, lat = (i % 180) - 90 + 0.5 for i ∈ [0, 1000)
    assert -180 <= x_min <= -179.7
    assert 179.0 <= x_max <= 180.5
    assert -90 <= y_min <= -89.4
    assert 89.0 <= y_max <= 90.5


def test_collision_with_file_row_number_uses_window_fallback(tmp_path):
    """If the parquet has its own ``file_row_number`` column, the loader
    must not collide on the virtual reader column — fall back to
    ``ROW_NUMBER() OVER ()``."""
    path = tmp_path / "collision.parquet"
    n = 20
    table = pa.table(
        {
            "lat": pa.array([float(i) for i in range(n)]),
            "lon": pa.array([float(-i) for i in range(n)]),
            "file_row_number": pa.array(list(range(100, 100 + n))),
        }
    )
    pq.write_table(table, str(path))

    res = fast_load_parquet(str(path))
    con = res.connection

    # The original file_row_number column is preserved (with values
    # 100..119), and our id_column is a separate dense rank.
    cols = [r[1] for r in con.sql(f'PRAGMA table_info("{res.table}")').fetchall()]
    assert "file_row_number" in cols
    assert res.id_column in cols

    rows = con.sql(
        f'SELECT file_row_number, "{res.id_column}" FROM "{res.table}" '
        f'ORDER BY file_row_number'
    ).fetchall()
    assert [r[0] for r in rows] == list(range(100, 100 + n))
    # ROW_NUMBER() OVER () is 1-based; that's fine — it's monotone unique.
    assert sorted(r[1] for r in rows) == sorted({r[1] for r in rows})  # all distinct
    assert len({r[1] for r in rows}) == n


def test_limit_pushed_into_view(latlon_parquet):
    path, n = latlon_parquet
    res = fast_load_parquet(str(path), limit=42)
    assert res.row_count == 42
    con = res.connection
    n_actual = con.sql(f'SELECT COUNT(*) FROM "{res.table}"').fetchone()[0]
    assert n_actual == 42


def test_geometry_column_extraction(wkb_parquet):
    path, n = wkb_parquet
    res = fast_load_parquet(str(path))
    assert res.row_count == n
    # ST_X / ST_Y synthesised columns get reserved names.
    assert res.x_column == "lon"
    assert res.y_column == "lat"
    assert res.id_column == "__row_index__"
    con = res.connection
    rows = con.sql(
        f'SELECT id, lon, lat, "{res.id_column}" FROM "{res.table}" '
        f'ORDER BY "{res.id_column}" LIMIT 3'
    ).fetchall()
    # Point i has x=i*0.1, y=i*0.1+5
    assert rows[0][1] == pytest.approx(0.0)
    assert rows[0][2] == pytest.approx(5.0)
    assert rows[2][1] == pytest.approx(0.2)
    assert rows[2][2] == pytest.approx(5.2)


def test_id_column_avoids_collision_with_existing_row_index(tmp_path):
    path = tmp_path / "collide_idx.parquet"
    n = 5
    table = pa.table(
        {
            "lat": pa.array([float(i) for i in range(n)]),
            "lon": pa.array([float(-i) for i in range(n)]),
            "__row_index__": pa.array([99, 98, 97, 96, 95]),
        }
    )
    pq.write_table(table, str(path))
    res = fast_load_parquet(str(path))
    assert res.id_column != "__row_index__"
    # Original column still queryable, and the synthesised one is unique.
    con = res.connection
    rows = con.sql(
        f'SELECT __row_index__, "{res.id_column}" FROM "{res.table}" '
        f'ORDER BY __row_index__ DESC'
    ).fetchall()
    assert [r[0] for r in rows] == [99, 98, 97, 96, 95]
    assert len({r[1] for r in rows}) == n
