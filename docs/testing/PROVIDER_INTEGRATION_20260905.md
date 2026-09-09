# 2026-09-05 GLM 5.3 Flash 单模型工程检查点

## 恢复实施后的修正

默认关闭视觉时仍完整声明 stdio command/args，再设置 enabled=false。
固定上游配置解析会先校验 transport，因此只写 enabled=false 会导致启动失败。
模型 catalog 必须带基础指令；省略和空串都不能保留预期原版行为。
现随 APK 提供原封不动的
[0.153.4 models-manager/prompt.md](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/models-manager/prompt.md)，
本地文件为 runtime/codex-base-instructions.md，SHA-256
`ac8ae107a0d72fe3476b430afb161ea4e67da2e446d778aefc44828160559807`。
Launcher 读取前核对该哈希；GLM 5.3 Flash 使用同一原文，不插入自写基础提示词。
初始化复制路径与原版源码同样作为离线材料固定。来源树为
`3d2ee51ca2d5db578f328aa75e20aa22c0197c9a`，prompt blob 为
`907ff8b877026871b088f01f4366cea36e1f02cd`。

新增 GLM Chat 适配、原生图片、工具命名空间往返、关闭视觉及原文损坏/缺失回归后，
本机组合测试 **38/38 PASS**（25 provider/adapter/vision/bridge + 13 overlay）。
这不替代 ALN Codex 配置加载及真实请求。

本报告只证明本地配置与边界测试。尚未使用用户凭证调用真实 GLM 推理服务；
不能把下列 PASS 当成手机模型接入或七场景通过。

## 官方协议核查

2026-09-05 读取以下官方页面，未运行页面提供的安装脚本。

| 分工 | 固定模型 | 默认地址 | 推理配置 |
|---|---|---|---|
| 唯一验收模型 | `glm-5.3-flash` | `https://api.z.ai/api/paas/v4` | 默认 `max`；`low/high/max` |

- [Z.AI GLM-5.3-Flash](https://docs.z.ai/guides/vlm/glm-5.3-flash)：模型代码、
  原生文字/图片输入、函数调用、思考配置与 Chat Completions 请求形态。
- [Z.AI Quick Start](https://docs.z.ai/guides/overview/quick-start)：普通 API 根地址为
  `https://api.z.ai/api/paas/v4`；Coding Plan 使用独立根地址
  `https://api.z.ai/api/coding/paas/v4`。

Codex 0.153.4 使用 Responses 协议，而当前 GLM-5.3-Flash 官方接口使用 Chat
Completions。APK 因此内置只监听 `127.0.0.1` 的小型适配器，把 Codex 的文字、图片、
函数/自定义工具请求翻译为 GLM Chat 请求，并把返回结果翻译回 Responses SSE；不修改
Codex 二进制。真实 GLM Token 只进入适配器环境，Codex 进程得到随机本机令牌。
手机选择的 effort 生效，不继承桌面任务的模型或思考强度。模型目录字段只是客户端能力
声明，不是服务鉴权或效果实测证明。

手机 Gateway 现直接连接 0.153.4 官方 `codex app-server` WebSocket。官方参数
`--ws-auth capability-token --ws-token-sha256` 让原始界面令牌不必写进命令行；自维护的
stdio/WebSocket bridge 不再随 APK 打包或启动。上游仍将 WebSocket transport 标为
实验接口，本轮按私测路径验证并保留旧代码供回退，不据此宣称生产稳定。

Android 入口只选择 GLM。独立 MiniMax 视觉 MCP 已从设置、启动配置和 APK 资源中移除。

## 配置与凭证合同

Android 向 `/root/.cheby/provider-settings.json` 暂存选中配置，必须为普通 0600 文件；
provider launcher 在启动 Codex 前读取并删除已消费文件。已运行实例没有新配置时继续使用
当前内存状态；出现新配置时，由受控启动脚本先结束自己记录的旧实例再启动新实例。
清除凭证通过空 `apiKey` 的配置覆盖，随后启动失败，不回退旧 key。缺文件、坏 JSON、
不安全文件权限都失败关闭。

每个 Codex 子进程通过 `CHEBY_PROVIDER_SETTINGS` 接收配置，launcher 读取并从下一层
环境移除该完整 JSON。仅非敏感 model catalog 落盘为 0600；真实 GLM key 只传给本机
适配器，Codex 使用随机回环令牌。key 不进入 argv、catalog 或生成的配置参数。一般 shell
工具的继承环境排除这些凭证变量。

这属于减少明文驻留和意外日志暴露：PRoot 的同 UID 进程及 `/proc` 仍不构成对恶意
Agent 的强秘密隔离，不能宣称内存凭证不可读。Android 侧持久化加密、暂存时机、清除和
重连 UI 属于另一实现单元，需一起进行设备验收。

配置字段：`provider/apiKey/baseUrl/reasoningEffort`，以及
`mcpServers[{name,url,token,enabled,remoteAuthorized}]`。MCP 名称限制为 ASCII
字母数字、下划线、连字符；忽略大小写检查重复，保留 phonebridge 与 cheby 内置命名。
默认仅允许本地回环 MCP；远程服务必须逐项明确布尔授权且使用 HTTPS。不接受 URL 用户名、
密码、query、fragment、控制字符。模型远端服务是本轮明确选择的服务，支持用户填写
自己的 HTTPS 地址；本地 HTTP 仅用于回环服务。

## 视觉与错误边界

允许的图片根目录只有：

- `/root/.cheby/turn-inputs`：Android 通用附件在 guest rootfs 的真实路径。
- `/root/.cheby/phonebridge/artifacts`：现有 phonebridge 配置的截图产物目录。

不允许整个 `/root/.cheby`，不使用未存在的 `/mnt/cheby-turn-images`；路径须为绝对
普通文件并通过解析后根目录检查，拒绝指向目录外的符号链接。只发送 PNG/JPEG，单图最多
8 MiB，响应最多 2 MiB。不跟随 HTTP 重定向，防止 Authorization 被带到其他地址。
非 `stop` 终态、空回答、畸形响应、HTTP 错误都不是识图成功。

MCP stdio 使用有界逐行 JSON-RPC，坏 JSON、过长行、非法请求不会让下一条合法 ping
失去响应；不反射原始异常、服务响应体、URL 或密钥到工具错误文本。

## 本地验证结果

| 检查 | 结果 | 证据与边界 |
|---|---|---|
| GLM 固定模型、默认地址、reasoning override | PASS | 固定配置生成断言 |
| Responses → Chat 文字、原生图片和工具命名空间往返 | PASS | 本机模拟上游，未调用真实 GLM |
| 配置 shape、key、endpoint、重复/保留 MCP 名称 | PASS | 恶意及错误输入测试 |
| 远程 MCP 独立授权 | PASS | 缺授权拒绝，显式 true 接受 |
| 凭证不进入参数/catalog；整份 JSON 不继续继承 | PASS | 假 key 字符串与 exec 环境断言 |
| Bridge 重连、替换、清除、坏文件 fail closed | PASS | Node VM 执行实际消费函数，真实临时文件 |
| 图像路径/大小、响应/截断、错误不回显 | PASS | 临时图片与模拟 HTTP opener |
| MCP 坏 JSON/过长行后恢复 ping | PASS | 真实输入输出流测试 |
| Bridge JavaScript 语法 | PASS | `node --check` |
| 真实提供商鉴权、推理、工具、多步手机任务 | BLOCKED | 未安全配置用户凭证 |
| GLM 原生看图并继续操作 | BLOCKED | 未安全配置用户凭证及设备验证 |

测试入口：`tools/standalone/test_provider_vision.py`，2026-09-05 本机 **25/25** 通过。
使用 Python 3.9、Node 24.20.0；`NODE_BINARY` 可指定已安装的 Node 路径，默认 `node`。
本地运行 `python3 tools/standalone/test_provider_vision.py`；`git diff --check` 通过。
模拟 HTTP opener 只检验请求及失败处理，不包含实际网络调用，更不代表外部服务已接通。

下一验收在 ALN 真机逐个验证：保存与重连、无效 key、不可达 endpoint、GLM 文字回答、
工具执行、原生图片理解和后续手机动作。保存状态不得显示为「已接通」。七场景的 21 个
主用例和 7 个换表述用例仍保留原分母。
