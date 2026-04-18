#!/bin/bash

set -e

rm -rf dist/

# Build every workspace the viewer (and therefore the backend static
# bundle) transitively depends on. Each of these ships gitignored
# artifacts (WASM pkg/, tsc dist/, vite dist/) that a fresh clone
# doesn't have. Order matters — each step depends on the previous.
echo "Building @embedding-atlas/density-clustering (WASM)..."
pushd ../density-clustering
npm run build
popd

echo "Building @embedding-atlas/umap-wasm (WASM)..."
pushd ../umap/umap-wasm
npm run build
popd

echo "Building @embedding-atlas/utils (tsc)..."
pushd ../utils
npm run build
popd

echo "Building @embedding-atlas/component (vite)..."
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
