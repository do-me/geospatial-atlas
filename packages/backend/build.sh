#!/bin/bash

set -e

rm -rf dist/

# @embedding-atlas/component transitively imports the pre-built WASM
# from @embedding-atlas/density-clustering, which is .gitignored.
# Build it first so a fresh checkout works end-to-end.
echo "Building @embedding-atlas/density-clustering (WASM)..."
pushd ../density-clustering
npm run build
popd

# The viewer imports pre-built artifacts from @embedding-atlas/component
# (packages/component/dist + /svelte). Rebuild them next so source
# changes to the component package actually land in the viewer bundle.
echo "Building @embedding-atlas/component..."
pushd ../component
npm run build
popd

echo "Building viewer frontend..."

pushd ../viewer
npm run build
popd

echo "Copying viewer assets..."

rm -rf ./embedding_atlas/static
rm -rf ./embedding_atlas/widget_static
cp -r ../viewer/dist ./embedding_atlas/static

npm run build

uv build --wheel
