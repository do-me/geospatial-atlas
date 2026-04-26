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


# IPC compression. zstd squashes a sorted u32 lon/lat scatter to ~10 % of
# raw bytes — at the eubucco 322 M-row file the wire goes 2.58 GB → ~250
# MB, which is what unblocks the post-u32 browser fetch (Chrome's
# ``Response.arrayBuffer()`` aborts past ~2 GB; the auto-memory note
# documents the silent-fail behaviour we hit before this fix). The
# compression cost is single-threaded zstd on the server's main writer
# thread — measured 350-450 MB/s on the M-series box, so ~3 s for 1.3 GB
# of u32. The decompress side runs in JS via ``fzstd``.
#
# We hard-wire zstd here because the client-side codec is unconditionally
# registered in ``EmbeddingViewMosaic.svelte``; once both ends understand
# the format there's no value in the f32-passthrough fallback.
_IPC_WRITE_OPTIONS = pa.ipc.IpcWriteOptions(compression="zstd")


def arrow_to_bytes(arrow: pa.Table | pa.RecordBatchReader):
    # When DuckDB hands back a RecordBatchReader (1.4+), draining it
    # batch-by-batch in Python is dramatically slower than letting Arrow
    # coalesce in C++ — measured 3.2 s vs. 0.28 s on a 302 MB / 75 M-row
    # scatter result. We drain to a Table first, then serialise in one
    # write. The streaming path is still available via ``stream_arrow_ipc``
    # for callers that need the per-batch memory cap.
    if isinstance(arrow, pa.RecordBatchReader):
        arrow = arrow.read_all()
    # Coalesce all column chunks into a single contiguous batch *before*
    # writing the IPC stream. DuckDB exports a 75 M-row Float32 column as
    # ~610 RecordBatches (default 122 880 rows each); the JS Arrow library
    # then has to allocate a 300 MB Float32Array and `memcpy` every chunk
    # into it the moment the renderer calls `.toArray()` for GPU upload.
    # ``combine_chunks`` does the merge once on the C++ side (~10 GB/s
    # vectorised memcpy), so the wire stream is one batch and the
    # browser-side `.toArray()` returns the underlying buffer view with
    # zero copy. Net: ~500 ms shaved off the browser GPU-upload path on a
    # 600 MB scatter pull.
    if isinstance(arrow, pa.Table) and arrow.num_rows > 0:
        arrow = arrow.combine_chunks()
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, arrow.schema, options=_IPC_WRITE_OPTIONS) as writer:
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
