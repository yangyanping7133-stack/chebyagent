# ChebyAgent

ChebyAgent 是一个 Android 端本地 Agent：一个 APK 内置 Codex CLI、PRoot/Debian Linux
运行层、手机控制工具、可恢复会话以及面向真实 App 的 Skills。安装后由用户在手机上
填写自己的模型凭证；仓库和 APK 不包含模型 Token、签名私钥、个人账号或手机数据。

## 工作方式

```text
手机聊天界面 → Codex 官方 app-server → 原版 Codex CLI
                                      ├─ 手机工具 → 原生 App
                                      └─ 模型原生图片输入 → 页面理解
```

UI 承载通用文本、图片、候选项、进度、确认和取消；Codex 与 Skills 负责判断及工具编排。
0.7.0 默认接入 GLM 5.3 Flash，也保留 MiniMax 与 OpenAI 兼容入口。用户仍可配置自己的
本地 MCP；独立视觉 MCP 已移除，截图直接作为模型图片输入。

仓库同时包含：

- Android 对话 UI、官方 app-server 客户端和崩溃恢复；
- ChebyNode 无障碍/截图/受控点击能力；
- 内嵌 Termux + PRoot/Debian + ARM64 Codex CLI；
- 本机 PhoneBridge、Local MCP 与 ACE 记忆适配；
- 地图、餐厅、咖啡、超市、理发、按摩、宠物美容、租房、外卖和打车 Skills；
- 网关、连接器、断线恢复、静态审计和验收工具。

## 0.7.0 验收边界

- Huawei ALN-AL00 上已完成覆盖安装、冷启动、会话恢复和无障碍恢复验证。
- 本地 Codex app-server、文字与原生图片输入、手机控制和结果卡片链路已通过真机验证。
- Yandex Go 打车、CIAN 租房和 Yandex Maps 咖啡场景已运行到提交前停止；不会代替用户付款、下单或预约。
- 咖啡场景最终一次运行约六分钟，尚未达到三分钟响应目标。

安装说明见 [手机安装指南](docs/architecture/ALN_PHONE_INSTALL_GUIDE.md)。Release 提供完整
APK、SHA-256、签名证书摘要、锁定的运行输入和对应上游源码。

## 构建

工程要求 JDK 17、Android SDK 35/NDK 27.2.12479018，以及
`Android/appliance/runtime/runtime.lock` 中锁定的 ARM64 运行资产。把 Release 中的
binary-input 资产放入文档指定路径后运行：

```bash
cd Android
./gradlew :app:testStandaloneUnitTest :appliance:testDebugUnitTest :appliance:assembleRelease
```

签名私钥、用户凭证、手机数据和大型离线资产不进入 Git 历史。完整构建/源码交付契约见
[Release 与对应源码说明](docs/RELEASE_AND_CORRESPONDING_SOURCE.md)。

## License

项目整体按 GPL-3.0-only 发布，因为完整 Android appliance 派生并链接了 GPLv3-only 的
Termux app/shared 代码。Codex CLI 及 ACE 保留各自 Apache-2.0 声明；其他组件的归属和
源码坐标见 [第三方声明](THIRD_PARTY_NOTICES.md) 与
[组件库存](docs/licensing/COMPONENT_INVENTORY_20260905.md)。
