# Standalone MCP Image Gate 3.5

Status: PASS on the target Android phone for a live PhoneBridge screenshot, inline chat rendering, full-screen preview, and recovery after restarting the ChebyCodex UI.

This checkpoint closes the screenshot-return defect found during the Yandex Maps cafe use case. It does not expand browser access, shared-storage access, or remote-media loading.

## Defect and root cause

PhoneBridge already returned `android_capture_screenshot` as an MCP result containing both a text block and an `image` block with Base64 bytes. The local Codex adapter projected every completed `mcpToolCall` only as a `ToolBlock`, so it discarded the image. The model then referred to the Connector's private Linux artifact path in Markdown; the final display boundary correctly redacted that path, leaving a literal broken image expression instead of a visible screenshot.

The generated schema from the installed Codex app-server confirmed that `mcpToolCall.result.content` is the authoritative result array used by both live item notifications and `thread/read` recovery.

## Implementation boundary

The standalone adapter now projects valid MCP image content into the existing `MediaBlock` model:

- Base64 is rejected before decoding when it can exceed 8 MiB.
- Only `image/png`, `image/jpeg`, and `image/webp` are accepted.
- The declared media type must match the file signature.
- At most four images are projected from one tool item.
- Image bytes live only in a process-private, 32 MiB bounded LRU cache behind a random opaque asset identifier.
- No filesystem path, `data:` URI, raw Base64, localhost endpoint, or MCP JSON enters the display model.
- The UI bounds source dimensions and decoded pixels, samples oversized images, supports an inline fit view, and supports a full-screen fit view.
- `thread/read` reprojects the persisted MCP content and repopulates the process-private cache after UI process death.
- Completed multi-block items patch every projected block before replacing their root order.
- Private-path image Markdown degrades to its readable caption instead of rendering broken syntax.

Remote `MediaBlock` identifiers still fail closed to the existing unavailable placeholder. This change does not fetch any external URI and does not hard-code Yandex or another product vertical into the renderer.

## Automated verification

- `:app:testStandaloneUnitTest`: PASS, 325 tests.
- `:app:lintStandalone`: PASS.
- `:app:assembleStandalone`: PASS.
- Distributed `:app:compileDebugKotlin`: PASS.
- Gateway recovery fixture: PASS for MCP image projection without path or payload exposure.
- Media-store negative gates: PASS for MIME mismatch, unsupported type, and payload larger than 8 MiB.
- Private image Markdown regression: PASS.

## Real-device Yandex Maps gate

The new ChebyCodex thread required the Agent to open only the installed package `ru.yandex.yandexmaps`, prohibited URLs and browsers, and required a real nearby cafe detail card followed by `android_capture_screenshot`.

Observed result:

- Android foreground package: `ru.yandex.yandexmaps`.
- Real place: `Секретер` (`Sekreter`).
- Yandex category: Cafe, Coffee shop, Restaurant.
- Rating shown: 5.0 from 730 ratings.
- Distance shown on the selected detail card: 580 m.
- ChebyCodex rendered the returned screenshot as an inline image with caption `手机截图`.
- Tapping the image opened a working full-screen preview.
- No browser or URL was opened.
- No `<private path>` or Base64 appeared in the conversation.

After explicitly force-stopping only `com.cheby.codex.mobile.standalone`, relaunching it restored the same thread through `thread/read`, repopulated the private media cache, and displayed the screenshot again. Termux/Codex and the standalone Node remained alive.

## Artifact and device verification

- Standalone APK SHA-256: `4b40c3e7e98ed58b3ac4b7882c8b851ebe12a7f19bf3e99b7831e88e0c6b71d3`.
- Installed base APK SHA-256 matched the local artifact: PASS.
- Signing certificate SHA-256: `190d4bd967692d845a233995b924b150449f98a66babec481babdebe60266ccb`.
- Installed package/version: `com.cheby.codex.mobile.standalone`, `0.4.5-standalone` (`12`).
- `com.termux.permission.RUN_COMMAND`: granted.
- Standalone Node `android.permission.WRITE_SECURE_SETTINGS`: granted.
- Standalone Node Accessibility service: enabled and bound.

Evidence retained outside the repository:

- Yandex detail screen: `/tmp/cheby-standalone-yandex-gate/yandex-cafe-detail-media-fix.png`, SHA-256 `cb1a8dd10dc4df0fd7ecd9279c981d1f19aaa66282d90122139d17d9dacc9dfe`.
- Live inline Cheby image: `/tmp/cheby-standalone-yandex-gate/cheby-visible-mcp-image.png`, SHA-256 `0d4e5c11643992606a4ea2f601cb8be066d2a66f2278d08a65ab7ffc40b60651`.
- Full-screen preview: `/tmp/cheby-standalone-yandex-gate/cheby-mcp-image-fullscreen.png`, SHA-256 `3ad8cbf6665cfa974dc13da18499ca0c7e5ae3f549a390c56595b14a0ec5a653`.
- Restored inline image after UI restart: `/tmp/cheby-standalone-yandex-gate/cheby-media-after-restart-visible.png`, SHA-256 `04c8e6657b50808179aed10950322f94a9c2cd56d31dc8bca5cad9d164c47e80`.

## Remaining boundary

The cache is deliberately process-local rather than a second durable media database. Recovery depends on Codex retaining the MCP result in the thread record; the real restart gate confirms that behavior for this screenshot. Long-duration eviction, many-image conversations, corrupted persisted image content, and device reboot recovery remain separate stress and lifecycle work.
