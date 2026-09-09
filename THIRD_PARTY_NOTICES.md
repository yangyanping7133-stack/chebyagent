# Third-party software and corresponding source

ChebyAgent 0.7.0 is a combined Android appliance. The top-level license is
GPL-3.0-only because the application derives from and links GPLv3-only Termux
app/shared code. Files carrying a narrower or different upstream license keep
that license and attribution.

## Termux and Linux layer

- `third_party/termux-app`: pinned Termux application source, GPLv3-only with
  the file-level exceptions recorded in its retained `LICENSE.md` files.
- Termux bootstrap: `bootstrap-2025.03.28-r1+apt-android-7`, SHA-256
  `c8d702b6f742935001c37cda81b8ac69504a95d5cf28f2899532dd8cd4b057eb`.
- Debian rootfs: Debian 13.6 slim ARM64 layer, SHA-256
  `ef6acc3d5842700dfc879e3ca83532a4b2c495ebb8a4a7cac645cbfd7aa56c7e`.
- Termux PRoot overlay, SHA-256
  `334a0e6aa93cf3264f416879d4a95ee5ad9df52a9c202f12436b07eaf7bc067c`.

The Release carries the exact Debian source packages, Termux build recipes,
patches and upstream source archives corresponding to the binaries embedded in
the APK. Package-level copyright evidence and the reproducible inventory tools
are retained under `docs/licensing/` and `tools/standalone/`.

## OpenAI Codex CLI

The appliance bundles the official ARM64 Linux Codex CLI `0.153.4` payload and
retains an older `0.147.0` payload inside the base rootfs. Both upstream source
trees, LICENSE and NOTICE files are supplied as Release assets. The payload is
used through its app-server interface and is not relicensed by this project.
Codex upstream is Apache-2.0.

## ACE

The dependency-free ACE core snapshot under
`connector/cheby_connector/ace_core/` comes from
`kayba-ai/agentic-context-engine` commit
`321d430e520f369315bad512cd2d90f1fa14a596` and is Apache-2.0. Its unmodified
LICENSE and provenance are retained beside the source.

## Android/JVM dependencies

The Gradle dependency graph uses AndroidX, Kotlin, Material Components, Guava,
Markwon, JUnit and their transitive dependencies. Exact coordinates, POM
license declarations and notices are captured by:

- `tools/standalone/gradle_dependency_inventory.init.gradle`
- `tools/standalone/android_notice_inventory.py`
- `docs/licensing/COMPONENT_INVENTORY_20260905.md`

The Release source-evidence asset contains the resolved POM declarations and
archive notices used for this build. It does not transfer ownership of any
third-party mark, service or application.

Five resolved Maven artifacts did not repeat a license in their direct POM.
Their version-bound package or upstream evidence resolves the license identity
without treating a missing direct POM field as permission to omit attribution:

- `com.atlassian.commonmark:commonmark:0.13.0` and
  `commonmark-ext-gfm-strikethrough:0.13.0`: BSD-2-Clause; the complete notice
  is embedded in both resolved JARs and retained in the license-evidence asset.
- `com.google.guava:guava:24.1-jre`: Apache-2.0; the module inherits its
  license declaration from the Guava parent project.
- `commons-io:commons-io:2.5`: Apache-2.0; the complete license and Apache
  Commons IO NOTICE are embedded in the resolved JAR and retained in the
  license-evidence asset.
- `org.codehaus.mojo:animal-sniffer-annotations:1.14`: MIT; the complete MIT
  grant and copyright notice are retained in the resolved POM comment.

The five Termux bootstrap subpackages whose own package paths omit a standalone
license file are mapped to their exact parent recipe/source evidence:

- `bzip2` to `libbz2`, `curl` to `libcurl`, `gpgv` to `gnupg`,
  `libsmartcols` to `util-linux`, and `xz-utils` to `liblzma`.

These mappings close the missing-license-identity gap only. The version-bound
delivery ledger and compliance audit remain authoritative about unresolved
package-specific redistribution obligations.
