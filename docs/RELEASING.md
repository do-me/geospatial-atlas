# Releasing geospatial-atlas

The desktop app is released on GitHub using bare `v*` tags. The static
web viewer is deployed manually. This fork does not publish packages
to PyPI or npm.

| Stream            | Tag prefix | Distribution                               | Workflow                              |
| ----------------- | ---------- | ------------------------------------------ | ------------------------------------- |
| Desktop app       | `v*`       | `.dmg` / `.deb` / `.rpm` / `.msi` / `.exe` | `.github/workflows/app-release.yml`   |
| Static web viewer | None       | GitHub Pages                               | `scripts/deploy-gh-pages.sh` (manual) |

Desktop releases up through `app-v0.0.6` used the `app-v*` prefix; the
scheme switched at v0.0.7 when the Zenodo auto-DOI integration went
live.

Mobile is documented in [MOBILE.md](./MOBILE.md) and not shipped yet.

---

## Cutting a desktop release

Minimal path:

```bash
# 1. Bump the desktop version and its lockfile without creating a tag.
npm --prefix apps/desktop version 0.0.9 --no-git-tag-version
#
# 2. Update CHANGELOG.md with the highlights.
#
# 3. Commit the version bump.
git commit -am "chore(desktop): bump version to 0.0.9"

# 4. After merging the release changes into main, tag that commit and push.
git tag v0.0.9 origin/main
git push origin v0.0.9
```

Pushing the `v0.0.9` tag triggers
`.github/workflows/app-release.yml`, which:

1. Checks that the tag matches `apps/desktop/package.json`.
2. Spawns three matrix builds: **macOS arm64**, **Linux x64**, **Windows x64**.
3. Builds the viewer → backend static → PyInstaller sidecar → Electron
   app (via `electron-builder`) for each.
4. Uploads `.dmg`, `.deb`, `.rpm`, `.msi`, and `.exe` artifacts.
5. Creates a **draft** GitHub Release with the tag as name and all
   artifacts attached.
6. You review the draft on github.com, write release notes (or let the
   auto-generated ones stand), and click **Publish**.

Manual runs of the desktop workflow build downloadable artifacts only;
they do not create a release from an unversioned branch.

### Unsigned builds

For the very first release you can ship **unsigned** builds. Expect
these user-facing caveats:

- **macOS**: First-launch warning: right-click → Open. Or after Sequoia
  (15.1+): System Settings → Privacy & Security → "Open Anyway".
- **Windows**: SmartScreen will show "Windows protected your PC — Don't
  run". Users click _More info → Run anyway_. This goes away once you
  have an EV code-signing cert.
- **Linux**: Install the `.deb` or `.rpm` using the system package manager.

This is fine for a v0.0.x / pre-1.0 release where early users tolerate
friction. Add signing before v0.1.

---

## Semver

We follow **semver** for each stream independently:

- `0.0.x`: unstable, breakage expected every version.
- `0.y.0`: usable but API may shift; document breakage in changelog.
- `1.0.0`: commit to backwards compatibility for the public API
  (CLI flags, backend HTTP schema, exported viewer's `metadata.json`).

The desktop release version is independent of the source-package version
inherited from upstream (for example, desktop `v0.0.9` and source `0.26.0`).

---

## Signing & notarization — unlocking v0.1

When you outgrow the unsigned warnings:

### macOS

1. Join the [Apple Developer Program](https://developer.apple.com/programs/)
   ($99/year).
2. Generate a **Developer ID Application** certificate in Xcode or the
   Apple Developer portal.
3. Export the `.p12`, base64-encode it:
   ```bash
   base64 -i cert.p12 -o cert.b64
   ```
4. Add these to repo **Settings → Secrets and variables → Actions**
   (electron-builder's standard env vars):
   - `CSC_LINK` = contents of `cert.b64` (or an https URL to it)
   - `CSC_KEY_PASSWORD` = the `.p12` password
   - `APPLE_ID`
   - `APPLE_APP_SPECIFIC_PASSWORD` (from appleid.apple.com)
   - `APPLE_TEAM_ID`

The workflow currently sets `CSC_IDENTITY_AUTO_DISCOVERY=false` to
force unsigned builds; remove that line when you wire secrets up.
electron-builder auto-enables notarization when the `APPLE_*` env
vars are present.

### Windows

EV code-signing certs cost $200–400/year. Then:

- `CSC_LINK` (Windows runner) = base64 of `.pfx`
- `CSC_KEY_PASSWORD` = the `.pfx` password

### Linux

Linux doesn't have a Gatekeeper-equivalent. Just sign `.deb` / `.rpm` with
`dpkg-sig` / `rpm --addsign` if you want repository integration.

---

## Changelog

Keep a top-level `CHANGELOG.md`. Prepend on each release:

```md
## v0.0.9 — 2026-10-16

**Desktop release.**

### Added

- Native macOS / Linux / Windows app (Electron + PyInstaller sidecar).
- Fast path for GeoParquet files via DuckDB spatial (`ST_X`/`ST_Y`).
- Live progress bar during load (DuckDB `query_progress()`).
- Per-dataset URL-hash persistence.
- Drag-and-drop dataset loading.
- Row-limit input for sampling large files.

### Known issues

- Unsigned: first-launch warnings on macOS + Windows.
- iOS / Android not yet shipped — see docs/MOBILE.md.
```

---

## CI and source packages

`.github/workflows/ci.yml` runs on pull requests, pushes to `main`, and
manual dispatch. It retains builds, type checks, formatting, tests, and
Clippy. Python checks use 3.12, including a clean installed-wheel smoke
test. The wheel is built for validation and desktop bundling, not PyPI
publication.

CI does not generate demo embeddings, upload unused package/site
artifacts, publish registries, or deploy Pages. Demo cache regeneration,
when needed, is a separate manual step:

```bash
npm run generate_cache --workspace packages/docs
```

---

## Web (static viewer) release

The static viewer currently deploys via the existing
`scripts/deploy-gh-pages.sh`, following the build steps in the root
README. GitHub Pages must use the `gh-pages` branch, root `/`, rather
than GitHub Actions as its source. Desktop tags and release publication
do not deploy the site.

---

## Post-release verification

After a release publishes:

1. Download the `.dmg` / `.deb` / `.msi` from the GitHub Release.
2. Install on a clean VM or use a separate test profile, preserving saved
   viewer state in the normal profile.
3. Open a Parquet file with `lon`/`lat`.
4. Confirm: WebGPU probe passes, load progresses, view restores on relaunch.

If a published release is broken, document the issue and ship a new patch
version rather than replacing artifacts behind an existing version tag.
