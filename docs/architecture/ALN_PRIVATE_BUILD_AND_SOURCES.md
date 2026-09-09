# ALN 私测版构建材料与来源核查

> **历史检查点。** 本文件保留 2026-09-05 当时的来源核查过程，文中的“当前”与待办
> 不代表 0.7.0 最新状态。当前结论以
> [`../licensing/RELEASE_COMPLIANCE_AUDIT_0.7.0.md`](../licensing/RELEASE_COMPLIANCE_AUDIT_0.7.0.md)
> 和 [`../licensing/README.md`](../licensing/README.md) 为准。

核查日期：2026-09-05。状态：**工程来源检查点；交付许可证及完整对应源码材料尚未齐备。**
本文件区分已经定位的来源与待补材料，不把私有仓库或非商业目的当作许可证豁免。
现阶段不公开仓库、不发布公开 APK。

## 已定位材料

| 部件 | 固定来源或证据 | 当前边界 |
|---|---|---|
| Termux app | `third_party/termux-app/SOURCE.lock`：`v0.118.3`、commit `5b657c6adf4304e5198951ce815fe0205dcac29c`，[上游](https://github.com/termux/termux-app/tree/5b657c6adf4304e5198951ce815fe0205dcac29c) | 已有源码、来源 lock 和 GPLv3-only 声明；终端库及共享库包含明确逐文件例外 |
| Termux bootstrap | `2025.03.28-r1+apt-android-7`，ARM64 zip SHA-256 `c8d702b6f742935001c37cda81b8ac69504a95d5cf28f2899532dd8cd4b057eb` | 75 个二进制包已对应到 71 个母包并保存 123 项源码输入；可重现构建、逐项 notice/安装信息评估仍待完成 |
| PRoot overlay | PRoot `5.1.107.89`、libandroid-shmem `0.7`、libtalloc `2.4.3`，版本和三个原 deb 的 SHA-256 在 `runtime.lock` | overlay SHA 已现场复核；不能用三个版本号代替对应许可证、源码和构建材料 |
| Debian rootfs | `debian:13.6-slim`、image config 与原 layer digest 在 `runtime.lock` | rootfs archive SHA 已复核；它还包含旧 Codex 0.147.0 及额外 Debian 包，需要逐包清单 |
| 新 Codex CLI | [npm 0.153.4 ARM64 metadata](https://registry.npmjs.org/@openai/codex/0.153.4-linux-arm64)，声明 `Apache-2.0`、上游 `openai/codex` | 原版运行文件已逐项保真；npm 平台归档不含 LICENSE / NOTICE，交付材料需另外补入对应版本内容与依赖 notices |
| 旧 Codex CLI | base archive 内 `0.147.0`，本轮保留作为旧运行目录 | 它仍存在于交付材料，不能因新入口使用 0.153.4 就从来源、notice 清单删除 |
| Android / JVM 依赖 | `Android/appliance/build.gradle.kts`、各 `Android/embedded/*/build.gradle.kts` | 需从实际解析依赖生成完整版本与许可证清单；声明坐标不是实际打包证明 |
| 项目代码和 Skills | 当前单机仓库与此次改动 | 根目录尚无完成的项目级 LICENSE；需明确自有部分许可并与嵌入组件要求一起核对 |

Termux 许可证依据是已读的 `third_party/termux-app/LICENSE.md` 与
`third_party/termux-app/termux-shared/LICENSE.md`，应随对应源码完整保留。
后者列出了 MIT、Apache-2.0、GPLv2 + Classpath exception 等特定文件例外，
不能给全部组件统一填一个未经核实的许可证。

[Apache-2.0 原文第 4 节](https://www.apache.org/licenses/LICENSE-2.0)说明再分发时应提供许可证、
保留相关归属声明，以及适用的 NOTICE；修改部分还应有变更标记。
Termux 已声明 [GPLv3-only](https://www.gnu.org/licenses/gpl-3.0.html)，对应源码、构建资料、
接收者可获得的材料与适用安装信息应在交付前逐项核对。此处不把“开源准备”写成合规完成。

## 构建工作现场

仅在 `ChebyCodex-Mobile-standalone` 子仓库继续当前未提交工作；
不借用、修改或合并旁边分布式仓库。构建资产留在 Git 忽略的 `artifacts/private/`，
最终按私有发布版本归档，源码历史不塞入巨型 rootfs 或凭证。

开发机当前约定：

```sh
cd /path/to/chebyagent/Android
export JAVA_HOME=/path/to/jdk-17
export ANDROID_HOME=/path/to/android-sdk
export ANDROID_SDK_ROOT=/path/to/android-sdk
./gradlew :app:testStandaloneUnitTest :appliance:testDebugUnitTest :appliance:assembleDebug
```

这只是可执行的构建入口；本文件不声称该命令在本次检查点已通过。
Gradle 构建机可以解析构建依赖；手机首次初始化不应下载运行依赖，两者分开验证。
apk 输出为 `Android/appliance/build/outputs/apk/debug/appliance-debug.apk`。

静态验证入口：

```sh
cd /path/to/chebyagent
python3 tools/standalone/test_codex_offline_overlay.py
python3 tools/standalone/appliance_static_gate.py \
  --apk Android/appliance/build/outputs/apk/debug/appliance-debug.apk \
  --apkanalyzer /path/to/android-sdk/cmdline-tools/latest/bin/apkanalyzer
```

`runtime-assets.sha256` 随打包生成，覆盖 provisioning、模型设置脚本、工具桥接、Skills
及三个二进制归档。static gate 同时检查固定 lock、archive 实际 SHA、
必需资源和离线初始化规则。debug APK 不能替代最终可持续签名的私测交付 APK。

## 交付前尚需形成的材料

- 与交付版本一致的组件清单：实际 Gradle 解析结果、bootstrap 包清单、Debian dpkg 包清单、
  每包许可证与对应源码获取位置、Codex 两个版本的依赖归属声明。
- 对应版本完整许可证/NOTICE 文件、项目自有代码许可决定、相对固定上游的修改清单。
- 干净源码归档和能使用这些材料构建的说明；必须检查历史和构建资产内是否含用户凭证、
  controller token、签名私钥、旧设备数据。仅对文件名做扫描不代表内容已脱敏。
- 单独保管的持续签名及恢复流程；提供旧稳定 APK、校验值、对应源码与明确的数据回退边界。
- 私有版本标签与发布资产清单，实际核验 GitHub 可见性仍为 private；不把上传成功等同于
  新手机可安装、模型接通或 21 项真实场景通过。

旧资料 `STANDALONE_SELF_CONTAINED_RUNTIME_4_1.md` 记录旧机结果，保留作历史参考；
本轮主模型、ALN、签名、私有交付标准以 `ALN_RUSSIA_AGENT_1_0.md` 为准。

## 后续现场进展

当前完整应用测试 341/341、配置存储测试 12/12 已通过；早期 342 项回归及随后删除的
旧 ledger codec 用例作为历史记录保留。项目独立签名的开发 APK及静态报告已归档；
具体失败重跑历史、APK SHA 和手机安装等待状态见
`docs/testing/ALN_IMPLEMENTATION_20260905.md`，不能据此宣称 1.0 已交付。

来源库存现已覆盖实际 223 个 Linux/Termux 包、Termux bootstrap 71 个母包的 123 项
源码输入、两个 Codex 固定版本完整上游源码
及自带 LICENSE/NOTICE，以及 110 个 Android 外部构建输入与 141 份 POM。
详见 `docs/licensing/COMPONENT_INVENTORY_20260905.md`；上表初始缺项按该后续证据细化。
历史 `command-not-found` 输入与 `foot` 压缩包字节有明确边界；可重现构建、最终 APK
归属覆盖和许可决定仍未完成。
