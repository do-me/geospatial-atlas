#!/bin/bash
# Deploy the built docs site to the gh-pages branch

set -euo pipefail

DIST_DIR="packages/docs/.vitepress/dist"
REPO_DIR=$(git rev-parse --show-toplevel)
TEMP_DIR=$(mktemp -d)

echo "📦 Copying dist to temp directory..."
cp -r "$REPO_DIR/$DIST_DIR/"* "$TEMP_DIR/"

# Add .nojekyll to prevent GitHub Pages from processing with Jekyll
touch "$TEMP_DIR/.nojekyll"

echo "🔀 Setting up gh-pages branch..."
cd "$TEMP_DIR"
git init
git checkout -b gh-pages
git add -A
git commit -m "Deploy geospatial-atlas to GitHub Pages"

echo "🚀 Pushing to gh-pages branch..."
git remote add origin "$(cd "$REPO_DIR" && git remote get-url origin)"
git push origin gh-pages --force

echo "🧹 Cleaning up..."
rm -rf "$TEMP_DIR"

echo "✅ Deployed to gh-pages branch!"
echo "   Set this branch as the source in GitHub → Settings → Pages"
