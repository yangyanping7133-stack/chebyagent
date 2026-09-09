# V0.1 intentionally keeps the release build unobfuscated while the gateway protocol stabilizes.
# OkHttp/Okio do not require reflection rules. Keep protocol model names out of
# release builds unless Android framework entry points reference them directly.
