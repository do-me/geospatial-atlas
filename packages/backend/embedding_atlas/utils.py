# Copyright (c) 2025 Apple Inc. Licensed under MIT License.

import logging
from io import BytesIO
from pathlib import Path
from typing import Any

import inquirer
import pandas as pd
import pyarrow as pa

logger = logging.getLogger("embedding-atlas")


def load_pandas_data(url: str) -> pd.DataFrame:
    suffix = Path(url).suffix.lower()

    if suffix == ".parquet":
        df = pd.read_parquet(url)
    elif suffix == ".json" or suffix == ".ndjson":
        df = pd.read_json(url)
    elif suffix == ".jsonl":
        df = pd.read_json(url, lines=True)
    else:
        df = pd.read_csv(url)
    return df


def load_huggingface_data(filename: str, splits: list[str] | None) -> pd.DataFrame:
    try:
        from datasets import load_dataset
    except ImportError:
        print(
            "⚠️ Loading Hugging Face datasets requires the `datasets` package to be installed. Please run `pip install datasets`, then try again."
        )
        exit(-1)

    ds: Any = load_dataset(filename)

    if splits is None or len(splits) == 0:
        ds_split_options = []
        for key in ds.keys():
            option = (f"{key} ({ds[key].num_rows} rows)", key)
            ds_split_options.append(option)
        split_question = [
            inquirer.Checkbox(
                "split",
                message=f"Select which data splits you want to load for dataset [{filename}]",
                choices=sorted(ds_split_options),
            ),
        ]
        splits = inquirer.prompt(split_question)["split"]  # type: ignore

    if splits is None or len(splits) == 0:
        raise ValueError("must select at least one split")

    dfs = []
    for split in splits:
        df = ds[split].to_pandas()
        df["split"] = split
        dfs.append(df)
    df = pd.concat(dfs, ignore_index=True)
    return df


# IPC compression is OFF. We tried zstd and the gain wasn't worth the
# damage:
#   1. fzstd 0.1.1 (the JS decoder we ship via flechette) rejects the
#      payload pyarrow emits — every column-discovery query fails with
#      ``Error: invalid zstd data`` (frame magic mismatch).
#   2. Even when fzstd accepted the bytes, pyarrow's zstd IPC writer
#      drops the 8-byte buffer padding flechette requires for
#      ``BigInt64Array`` views, so ``__row_index__`` blows up with
#      ``start offset of BigInt64Array should be a multiple of 8``.
#   3. The compression ratio on randomized u32 lon/lat is ~8 % anyway
#      (measured on the eubucco 322 M-row file: 3.26 GB compressed vs
#      ~3.86 GB raw), so the wire savings don't justify either bug.
# The 2 GB ``Response.arrayBuffer()`` ceiling that originally motivated
# zstd is now bypassed by ``streamingRestConnector`` (viewer side), which
# splits the body into per-message ``Uint8Array``s as it streams in,
# never allocating a single buffer larger than ``_MAX_BATCH_ROWS`` × the
# row width (see comment on _MAX_BATCH_ROWS below).
_IPC_WRITE_OPTIONS = pa.ipc.IpcWriteOptions()


# Per-batch row cap when serialising large tables. ``combine_chunks``
# (one batch with a ~2.6 GB body for the 322 M-row scatter) trips two
# limits in the browser:
#   1. Chrome's per-tab single-``ArrayBuffer`` cap is ~2.0 GB on
#      macOS — the body is one Arrow buffer per column, and the JS
#      streaming connector's pre-allocated ``Uint8Array`` for the
#      Content-Length-sized response goes ``Array buffer allocation
#      failed`` past 2 GB regardless of free RAM.
#   2. Even when split across two columns, a single 1.29 GB-per-column
#      buffer holds the whole IPC body in one message — which means the
#      streaming JS decoder cannot find a message boundary to chunk on.
# At the eubucco file the largest single Arrow IPC batch we want is
# ~256 K rows = ~2 MB body for u32+u32 — small enough that JS sees ~1230
# small messages and never holds more than a few MB in any one
# allocation. Per-batch IPC framing overhead is ~144 bytes; over 1230
# batches that is ~177 KB on the wire, lost in the noise.
_MAX_BATCH_ROWS = 262_144


def arrow_to_bytes(arrow: pa.Table | pa.RecordBatchReader):
    # When DuckDB hands back a RecordBatchReader (1.4+), draining it
    # batch-by-batch in Python is dramatically slower than letting Arrow
    # coalesce in C++ — measured 3.2 s vs. 0.28 s on a 302 MB / 75 M-row
    # scatter result. We drain to a Table first, then re-batch with a
    # bounded ``max_chunksize`` so the JS streaming decoder always sees
    # message-aligned chunks.
    if isinstance(arrow, pa.RecordBatchReader):
        arrow = arrow.read_all()
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, arrow.schema, options=_IPC_WRITE_OPTIONS) as writer:
        if isinstance(arrow, pa.Table) and arrow.num_rows > 0:
            # Single combine + re-batch: the combine walks chunk-by-chunk
            # in C++ (~10 GB/s) so a 2.6 GB scatter rebatched into 1230
            # 256 K-row batches still totals ~250 ms — invisible next to
            # the network transfer of the 2.58 GB body.
            for batch in arrow.combine_chunks().to_batches(
                max_chunksize=_MAX_BATCH_ROWS
            ):
                writer.write_batch(batch)
        else:
            writer.write(arrow)
    return sink.getvalue().to_pybytes()


def stream_arrow_ipc(reader: pa.RecordBatchReader, *, batch_chunk_bytes: int = 4 * 1024 * 1024):
    """Yield Arrow IPC stream bytes incrementally as the cursor produces
    record batches. Lets uvicorn start sending the response while DuckDB
    is still computing later batches, so the wire and the engine overlap.

    The IPC writer accumulates into ``BytesIO`` until ``batch_chunk_bytes``
    is queued, then flushes a chunk to the network. Unlike
    ``arrow_to_bytes`` this never holds the full materialised body in
    memory — peak server-side RSS for a 1.2 GB scatter drops to a single
    record-batch's worth of bytes (~50 MB for 1 M-row batches).
    """
    sink = BytesIO()
    writer = pa.ipc.new_stream(sink, reader.schema, options=_IPC_WRITE_OPTIONS)
    try:
        for batch in reader:
            writer.write_batch(batch)
            if sink.tell() >= batch_chunk_bytes:
                yield sink.getvalue()
                sink.seek(0)
                sink.truncate(0)
    finally:
        writer.close()
    tail = sink.getvalue()
    if tail:
        yield tail


def to_parquet_bytes(df: pd.DataFrame) -> bytes:
    class NoCloseBytesIO(BytesIO):
        def close(self):
            pass

        def actually_close(self):
            super().close()

    bytes_io = NoCloseBytesIO()
    df.to_parquet(bytes_io)
    result = bytes_io.getvalue()
    bytes_io.actually_close()
    return result


def apply_logging_config():
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: (%(name)s) %(message)s",
    )

    logging.getLogger("httpx").setLevel(logging.WARNING)
