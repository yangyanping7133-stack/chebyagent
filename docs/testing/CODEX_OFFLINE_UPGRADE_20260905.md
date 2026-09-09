# Codex 离线升级工程检查点（2026-09-05）

状态：**离线组件、ALN 安装、本机启动与真实 GLM 图片闭环 PASS；Agent 场景验收未完成。**
本记录不表示 ChebyAgent 1.0 已完成，也不沿用旧 JUY 手机成功证据。

## 版本与来源

当天直接查询 [官方 npm latest](https://registry.npmjs.org/@openai/codex/latest)，
稳定版已更新为 `0.153.4`。其 optional dependency 对 ARM64 Linux 指向
`@openai/codex@0.153.4-linux-arm64`。

- [官方平台包元数据](https://registry.npmjs.org/@openai/codex/0.153.4-linux-arm64)
- [原始下载归档](https://registry.npmjs.org/@openai/codex/-/codex-0.153.4-linux-arm64.tgz)
- [官方 CLI 安装说明](https://learn.chatgpt.com/docs/codex/cli)
- 缓存路径：`artifacts/private/runtime/codex-0.153.4/codex-linux-arm64.tgz`
- 大小：`121707000` bytes
- SHA-256：`439c0dd0d6923f607b4e5cd1e3079c12f0b86f6e5007f07e377d6ad25e2d7bb9`
- npm SHA-512 integrity：`sha512-QKdjYLYV4hXIuUQDP3P6F4NXuWFoKo9WUoV4nAREIx55kiUyi8UsYdsVobkeXir5n/maEQgYMCKLHVma4rNPiw==`

本地缓存同时通过 SHA-256 与官方 metadata 的 SHA-512 校验。
归档的平台 metadata 为 `aarch64-unknown-linux-musl`、layout version `1`。
原始六个 vendor 文件包括 Codex、code-mode host、rg、bwrap、zsh 和 package metadata；
真实解包后逐项 SHA-256 与原归档内容一致，没有修改原版二进制。

## 安装边界

`runtime.lock` 版本为 `4.1.0-dev19`。该版本号升级会在覆盖安装后重新运行
guest finalize，使已部署设备真正刷新 Codex MCP 与技能，同时保留凭证、会话和 App 数据。
APK 开发版本为
`0.7.0-aln-private-dev`、versionCode `9`，不提前命名为验收完成的 `1.0`。

继续使用两个旧基础归档，不重新压制、不更换它们的校验值：

| 归档 | SHA-256 |
|---|---|
| `debian-rootfs-aarch64.tar.zst` | `ef6acc3d5842700dfc879e3ca83532a4b2c495ebb8a4a7cac645cbfd7aa56c7e` |
| `termux-proot-overlay-aarch64.tar.zst` | `334a0e6aa93cf3264f416879d4a95ee5ad9df52a9c202f12436b07eaf7bc067c` |

Gradle 将原始 Codex `.tgz` 作为第三个离线归档装入 APK，打包前验证 SHA-256，
APK 内不再压缩 `.tgz`。初始化仅使用 APK 内材料，不需要网络下载依赖。

`install-codex-overlay.py` 先校验完整归档，再安全提取到同级 staging 目录，
验证 metadata 和所有预期可执行文件，最后重命名为
`/opt/cheby/runtime/codex-0.153.4`。不接收符号链接、非普通文件、重复条目、路径越界或
未知平台内容。旧 `/opt/cheby/runtime/codex-0.147.0` 继续保留。

guest provisioning 在切换 `/opt/codex` 前运行新版本的 version、app-server help、
code-mode host help 检查；安装 provider launcher、GLM 本机协议适配器，并保留默认禁用的
旧 vision MCP 兼容脚本做语法检查。1.0 的所有图片只送入主 GLM 原生图片输入，不路由到
独立视觉模型。手机界面直接连接 0.153.4 官方 `app-server` 的本机 WebSocket，
用官方 `--ws-token-sha256` 校验界面的 bearer；不再打包或启动自维护 stdio/WebSocket bridge。
上游仍将 WebSocket transport 标为实验接口，因此本轮只按单机私测验收，不宣称生产稳定。
模型凭证与实际服务鉴权不在这项静态检查的通过范围内。

保留旧目录只支持后续回退材料准备，不代表旧 APK 能自动无损降级；
Codex 会话、应用数据库、Android versionCode 和签名仍需单独验证兼容性。

## 本次验证

| 检查 | 结果 |
|---|---|
| 官方 stable 与 ARM64 metadata fresh 查询 | PASS，均为 0.153.4 |
| 官方 npm SHA-512 / 缓存 SHA-256 | PASS |
| 原始包真实提取、六项 vendor 文件原字节校验 | PASS |
| 相同 archive 重复安装 | PASS |
| 两个基础归档原 SHA-256 | PASS |
| 九项安装器回归：保留旧版、错误 hash、错误版本、越界、链接、已有目录、staging 链接、重复、重试 | PASS |
| 四项打包 gate 回归：完整材料、伪造新 manifest、缺 provider、重复压缩 | PASS |
| runtime.lock 与 gate 固定字典一致 | PASS |
| provisioning shell syntax、Python syntax、git diff whitespace | PASS |
| Gradle / 新 APK static gate / ALN 安装 | r8 release 构建、14/14 静态检查及覆盖安装 PASS；设备 APK hash 与候选一致 |
| Linux ARM64 实际执行 / 模型鉴权 / 原生图片 | Codex 连接、GLM 鉴权与 RuStore `VK ID` 盲测 PASS |
| 21 个场景及 7 个改写 | 未执行 |

回归入口：`python3 tools/standalone/test_codex_offline_overlay.py`，共 13 项。

此升级改动涉及 `Android/appliance/build.gradle.kts`、`runtime.lock`、两个 provisioning
脚本、新安装器，以及 `appliance_static_gate.py`。Termux required assets 和 readiness
常量由主任务同步为 dev19，并包含五个新增资源：`codex-linux-arm64.tgz`、
`install-codex-overlay.py`、`provider-launcher.py`、`glm-chat-adapter.py`。

## dev18 原生图片闭环

r7 现场探针证明 Codex 完成三次模型请求、手机截图文件是真实 JPEG，但官方 app-server
把 MCP 图片结果降成了只含 `artifact_path` 的字符串，GLM 没有收到下一轮原生图片。
dev18 adapter 仅接受 `/root/.cheby/phonebridge/artifacts` 下的普通 PNG/JPEG，拒绝符号
链接、路径逃逸、空文件、非图片和超过 2 MiB 的文件；读取后作为紧邻工具结果的原生
多模态消息送给 GLM，同时保留原工具文本。

provider/vision 回归 **28/28 PASS**，离线 overlay 回归 **14/14 PASS**。r8 候选为：

- APK：`artifacts/private/releases/aln-private-candidate-20260905-r8-native-vision/ChebyAgent-0.7.0-aln-private-candidate-r8-native-vision.apk`
- SHA-256：`7c1676ecc67bbd8d6bef915cc043ae72af294a24f5d0b626f4a86efe31f8da9b`
- 签名证书 SHA-256：`dc9a4b8cbefe4da91e1a56233b3d2f164d651df1dcc27996ff8ce381c8bb2730`

ALN 覆盖安装后，设备路径中的 `base.apk` 现场 SHA-256 与上述候选完全一致；无障碍服务
恢复，本机 Codex 重新连接。在全新会话中，Codex 打开 `ru.vk.store` 并截图，GLM 最终
仅返回截图可见标题 `VK ID`。这项证据只通过图片传输和语义识别门，不代表 21+7 场景通过。
