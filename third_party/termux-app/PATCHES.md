# Local compatibility patches

The imported base is Termux `v0.118.3` at commit `5b657c6adf4304e5198951ce815fe0205dcac29c`.

## Android SDK 35 compilation

- `app/src/main/java/com/termux/app/activities/HelpActivity.java`
  - Removed the obsolete `WebSettings.setAppCacheEnabled(false)` call. The method is absent from the Android SDK 35 compile stubs and the activity already enforces `WebSettings.LOAD_NO_CACHE` on the preceding line.
- `app/src/main/java/com/termux/app/TermuxService.java`
  - Added braces around the background-only stdin assignment and made the following log-level assignment's existing unconditional behavior explicit. This resolves the SDK 35 suspicious-indentation gate without changing command behavior.

## Appliance branding

- `app/src/main/res/values/strings.xml`
  - Replaced the application name and bootstrap progress/error strings shown during the ChebyCodex first-run flow. This prevents an internal Termux implementation detail from being presented as a separately installed product; no runtime or command behavior changes.
- `app/src/main/java/com/termux/app/TermuxService.java`
  - The foreground notification opens the sole ChebyCodex launcher, uses appliance wording, and omits terminal-specific session, exit, and wake-lock controls. Runtime lifecycle remains owned by the appliance instead of exposing the embedded terminal implementation as a second product surface.

No runtime path, package prefix, command dispatch, bootstrap, terminal, or process-management behavior is changed by this patch.

## Apple Silicon host build

- The Cheby Gradle adapters build the two upstream ndk-build projects with NDK `27.2.12479018`.
- Upstream `v0.118.3` names NDK 22.1, whose macOS host tools reject Apple Silicon with `Unknown host CPU architecture: arm64`.
- The Android output ABI remains `arm64-v8a`; both native libraries require a real-device load gate before this host-tool substitution is accepted.
