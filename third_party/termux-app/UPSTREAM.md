# Termux upstream snapshot

- Repository: https://github.com/termux/termux-app
- Tag: `v0.118.3`
- Commit: `5b657c6adf4304e5198951ce815fe0205dcac29c`
- Imported: 2026-08-08
- License: GPL-3.0; see `LICENSE.md` and module-specific license files

This directory is a source snapshot, not an APK dependency. It intentionally contains only the source and build references needed by the embedded appliance:

- `app/src/`, excluding downloaded `bootstrap-*.zip` artifacts;
- `terminal-emulator/src/`;
- `terminal-view/src/`;
- `termux-shared/src/`;
- the upstream Gradle files and license/readme files used as provenance references.

The upstream development keystore, CI metadata, Gradle wrapper, and art-generation inputs are not imported. Cheby build adapters live under `Android/`. Any unavoidable source compatibility patch is recorded in `PATCHES.md` so an upstream refresh remains reviewable.

The first internal appliance keeps `applicationId=com.termux` and target SDK 28. Any package-name change requires rebuilding the Termux bootstrap and packages for the new private prefix; changing Java package declarations alone is not sufficient.
