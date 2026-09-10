# ChebyAgent 0.7.3

ChebyAgent 0.7.3 is the current phone-only Android release. Ordinary users
install `ChebyAgent-0.7.3.apk`; USB, Termux setup and a Mac-side network proxy
are not required after installation.

## Changes

- recovers an interrupted or partially prepared embedded Linux/Codex runtime;
- refreshes guest DNS from the active Android network and removes the retired
  Mac-assisted loopback model proxy;
- supports GPT-5.6 Sol through ChatGPT/Codex login, including local selection
  of the user's own Codex `auth.json`;
- supports GLM 5.3 Flash through a Z.AI API token;
- removes MiniMax from settings and runtime routing;
- displays each provider's native English reasoning values;
- keeps phone screenshots and agent process details collapsed by default.

## Verification

- release build and local static/unit gates passed;
- signed APK installed over the existing app on Huawei ALN-AL00 (Android 12,
  ARM64), then reconnected without USB;
- GPT-5.6 Sol and GLM 5.3 Flash each completed a real Yandex Maps task on the
  phone, searched nearby coffee shops and returned three observed names;
- no reservation, order or payment was made;
- the release APK contains no user login file, provider token or signing key.

## Integrity

- APK SHA-256: `e0945173cf3d72947297dc040d26276e33fd7e03a6c24e4bb218a116f4330e44`
- signer certificate SHA-256: `dc9a4b8cbefe4da91e1a56233b3d2f164d651df1dcc27996ff8ce381c8bb2730`

Corresponding-source and third-party license binding is documented in
[`RELEASE_SOURCE_BINDING_0.7.3.md`](RELEASE_SOURCE_BINDING_0.7.3.md).
