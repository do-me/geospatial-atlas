# Geospatial Atlas
This is a fork of [Embedding Atlas](https://apple.github.io/embedding-atlas) adapted for geospatial data. As embeddings or rather their 2D projections share the exact same visualization challenges like 2D geospatial data, Embedding Atlas and all its functionality serve a great deal in geospatial data exploration!

It can **visualize up to ~200M points** in your WebGPU-enabled browser! Make sure to use Chrome, Safari or activate the flag in Firefox.

Find various example apps [here](https://github.com/do-me/geospatial-atlas-apps). Try for example the [6M GlobalGeoTree explorer](https://do-me.github.io/geospatial-atlas-apps/GlobalGeoTree/)!

[LinkedIn Post for more context](https://www.linkedin.com/posts/dominik-weckm%C3%BCller_geospatial-atlas-is-born-explore-100m-points-activity-7411826555179429890-CiHX?utm_source=share&utm_medium=member_desktop&rcm=ACoAAC8q3V4BiZXfSx0JnRGDOF3d6Fzu4HdRtDE)

## Example screenshots

![alt text](screenshots/image-4.png)
![alt text](screenshots/image-5.png)
![alt text](screenshots/image-3.png)
![alt text](screenshots/image.png)
![alt text](screenshots/image-1.png)
![alt text](screenshots/image-2.png)

## Installation

```bash
git clone https://github.com/do-me/geospatial-atlas.git
cd geospatial-atlas
npm install
npm run build
```

Running on an Intel Mac? Then add this line to `packages/backend/pyproject.toml`:

`required-environments = ["sys_platform == 'darwin' and platform_machine == 'x86_64'"]`

For Windows, Silicon Macs and Linux everything should work out of the box.

## Usage (after installation above)

Currently the parquet files require both a `lat` (or latitude) and `lon` (or longitude) column. A Geometry column is not being parsed at the moment (but can be implemented fairly easily with DuckDB spatial). Preprocessing with DuckDB is recommended.

Execute this command directly from the root directory of the repository.
```bash
uv --directory packages/backend run embedding-atlas your_dataset_with_lat_lon_coords.parquet --text your_name_column
```

Alternatively you can cd into the backend folder and run it from there:
```
cd packages/backend
uv run embedding-atlas your_dataset_with_lat_lon_coords.parquet --text your_name_column
```

The screenshots above were created with these two datasets:
- [Foursquare 100M Places](https://huggingface.co/datasets/do-me/foursquare_places_100M), [direct download]()
- [50k poorly geocoded news](https://huggingface.co/datasets/do-me/50k_poorly_geocoded_news), [direct download](https://huggingface.co/datasets/do-me/50k_poorly_geocoded_news/resolve/main/geocoded_news.parquet)

## Build & Deploy GitHub Pages

The static web app is deployed manually (no CI). To rebuild and deploy:

```bash
# 1. Install dependencies (first time only)
npm install

# 2. Build all packages (utils, component, table, viewer, docs)
npm run build

# 3. Deploy the built site to the gh-pages branch
./scripts/deploy-gh-pages.sh
```

Then in GitHub → Settings → Pages, set the source to the `gh-pages` branch (root `/`).

The live site is available at: https://do-me.github.io/geospatial-atlas/

## To Do

- Disallow zooming out further than zoom level 0 to avoid weird shifting effects
- Adapt density and point radius ranges
- Add basemap attribution
- Release own "geospatial-atlas" pip package?
- Test everything properly
- And much more! Feel free to open PRs!

---

## Original Embedding Atlas Readme

[![NPM Version](https://img.shields.io/npm/v/embedding-atlas)](https://www.npmjs.com/package/embedding-atlas)
[![PyPI - Version](https://img.shields.io/pypi/v/embedding-atlas)](https://pypi.org/project/embedding-atlas/)
[![Paper](https://img.shields.io/badge/paper-arXiv:2505.06386-b31b1b.svg)](https://arxiv.org/abs/2505.06386)
[![GitHub License](https://img.shields.io/github/license/apple/embedding-atlas)](./LICENSE)

Embedding Atlas is a tool that provides interactive visualizations for large embeddings. It allows you to visualize, cross-filter, and search embeddings and metadata.

**Features**

- 🏷️ **Automatic data clustering & labeling:**
  Interactively visualize and navigate overall data structure.

- 🫧 **Kernel density estimation & density contours:**
  Easily explore and distinguish between dense regions of data and outliers.

- 🧊 **Order-independent transparency:**
  Ensure clear, accurate rendering of overlapping points.

- 🔍 **Real-time search & nearest neighbors:**
  Find similar data to a given query or existing data point.

- 🚀 **WebGPU implementation (with WebGL 2 fallback):**
  Fast, smooth performance (up to few million points) with modern rendering stack.

- 📊 **Multi-coordinated views for metadata exploration:**
  Interactively link and filter data across metadata columns.

Please visit <https://apple.github.io/embedding-atlas> for a demo and documentation.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="./packages/docs/assets/embedding-atlas-dark.png">
  <img alt="screenshot of Embedding Atlas" src="./packages/docs/assets/embedding-atlas-light.png">
</picture>

## Get started

To use Embedding Atlas with Python:

```bash
pip install embedding-atlas

embedding-atlas <your-dataset.parquet>
```

In addition to the command line tool, Embedding Atlas is also available as a Python Notebook (e.g., Jupyter) widget:

```python
from embedding_atlas.widget import EmbeddingAtlasWidget

# Show the Embedding Atlas widget for your data frame:
EmbeddingAtlasWidget(df)
```

Finally, components from Embedding Atlas are also available in an npm package:

```bash
npm install embedding-atlas
```

```js
import { EmbeddingAtlas, EmbeddingView, Table } from "embedding-atlas";

// or with React:
import { EmbeddingAtlas, EmbeddingView, Table } from "embedding-atlas/react";

// or Svelte:
import { EmbeddingAtlas, EmbeddingView, Table } from "embedding-atlas/svelte";
```

For more information, please visit <https://apple.github.io/embedding-atlas/overview.html>.

## BibTeX

For the Embedding Atlas tool:

```bibtex
@misc{ren2025embedding,
  title={Embedding Atlas: Low-Friction, Interactive Embedding Visualization},
  author={Donghao Ren and Fred Hohman and Halden Lin and Dominik Moritz},
  year={2025},
  eprint={2505.06386},
  archivePrefix={arXiv},
  primaryClass={cs.HC},
  url={https://arxiv.org/abs/2505.06386},
}
```

For the algorithm that automatically produces clusters and labels in the embedding view:

```bibtex
@misc{ren2025scalable,
  title={A Scalable Approach to Clustering Embedding Projections},
  author={Donghao Ren and Fred Hohman and Dominik Moritz},
  year={2025},
  eprint={2504.07285},
  archivePrefix={arXiv},
  primaryClass={cs.HC},
  url={https://arxiv.org/abs/2504.07285},
}
```

## Development

This repo contains multiple sub-packages:

Frontend:

- `packages/component`: The `EmbeddingView` and `EmbeddingViewMosaic` components.

- `packages/table`: The `Table` component.

- `packages/viewer`: The frontend application for visualizing embedding and other columns. It also provides the `EmbeddingAtlas` component that can be embedded in other applications.

- `packages/density-clustering`: The density clustering algorithm, written in Rust.

- `packages/umap-wasm`: An implementation of UMAP algorithm in WebAssembly (with the [umappp](https://github.com/libscran/umappp) C++ library).

- `packages/embedding-atlas`: The `embedding-atlas` package that get published. It imports all of the above and exposes their API in a single package.

Python:

- `packages/backend`: A Python package named `embedding-atlas` that provides the `embedding-atlas` command line tool.

Documentation:

- `packages/docs`: The documentation website.

For more information, please visit <https://apple.github.io/embedding-atlas/develop.html>.

## License

This code is released under the [`MIT license`](LICENSE).
