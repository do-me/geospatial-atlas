# Copyright (c) 2025 Apple Inc. Licensed under MIT License.

import json
import os
import zipfile
from io import BytesIO

import pandas as pd

from .utils import cache_path, to_parquet_bytes


class DataSource:
    def __init__(
        self,
        identifier: str,
        dataset: pd.DataFrame,
        metadata: dict,
    ):
        self.identifier = identifier
        self.dataset = dataset
        self.metadata = metadata
        self.cache_path = cache_path("cache", self.identifier)

    def cache_set(self, name: str, data):
        path = self.cache_path / name
        with open(path, "w") as f:
            json.dump(data, f)

    def cache_get(self, name: str):
        path = self.cache_path / name
        if path.exists():
            with open(path, "r") as f:
                return json.load(f)
        else:
            return None

    def make_archive(self, static_path: str):
        io = BytesIO()

        full_parquet = to_parquet_bytes(self.dataset)
        size = len(full_parquet)

        # Split parquet into parts if it exceeds 50MB (GitHub Pages limit is 100MB, but 50MB is safer)
        MAX_SIZE = 50 * 1024 * 1024
        parts = []
        if size > MAX_SIZE:
            num_parts = (size // MAX_SIZE) + 1
            rows_per_part = len(self.dataset) // num_parts + 1
            for i in range(num_parts):
                start = i * rows_per_part
                end = (i + 1) * rows_per_part
                part_df = self.dataset.iloc[start:end]
                if len(part_df) > 0:
                    parts.append((f"data/dataset_{i}.parquet", to_parquet_bytes(part_df)))
        else:
            parts.append(("data/dataset.parquet", full_parquet))

        file_names = [os.path.basename(p[0]) for p in parts]

        with zipfile.ZipFile(io, "w", zipfile.ZIP_DEFLATED) as zip:
            zip.writestr(
                "data/metadata.json",
                json.dumps(
                    self.metadata
                    | {
                        "isStatic": True,
                        "database": {"type": "wasm", "load": True, "files": file_names},
                    }
                ),
            )
            for path, data in parts:
                zip.writestr(path, data)
            for root, _, files in os.walk(static_path):
                for fn in files:
                    p = os.path.relpath(os.path.join(root, fn), static_path)
                    zip.write(os.path.join(root, fn), p)
            for root, _, files in os.walk(self.cache_path):
                for fn in files:
                    p = os.path.join(
                        "data/cache",
                        os.path.relpath(os.path.join(root, fn), str(self.cache_path)),
                    )
                    zip.write(os.path.join(root, fn), p)
        return io.getvalue()
