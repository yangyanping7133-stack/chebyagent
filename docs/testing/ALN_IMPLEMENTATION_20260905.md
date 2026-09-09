# ALN 1.0 实施记录：2026-09-05

本文件记录工程检查点，不能替代 `ALN_RUSSIA_AGENT_1_0.md` 的完整验收。

## 当前模型冻结

用户在 2026-09-05 将 1.0 验收范围收敛为唯一模型 `GLM 5.3 Flash`。
21 个场景用例与 7 个改写用例均使用同一冻结配置。GLM 原生图片输入是唯一视觉路径；
独立 MiniMax 视觉 MCP 已从设置、运行时注册和 APK 资源中移除。

2026-09-06 用户将产品可选模型扩展为 GLM 5.3 Flash、MiniMax M3 和
GPT-5.6 Terra；三者都以多模态入口接入，并在界面中单独选择推理强度。
主流程仍默认 GLM 5.3 Flash 低强度。这一产品扩展不改写上述历史验收口径。
通用 MCP 配置从用户界面隐藏，底层 Agent MCP 和内置 PhoneBridge 保留。

## 交接

HANDOFF VERIFIED。在单机子仓库，分支 `codex/aln-russia-agent-1.0`，初始 HEAD
`cb534fdb396ada6c9a42f763e70681d306f09382`。四个交接文件 SHA256 全部匹配；
保留原有未提交文件，未 reset/stash/切换分支/新建 worktree。已通知原任务，
在新任务创建实际交付目标，无预算限制。邻接分布式仓库未修改。

## 当前工程变化

- Android 系统密钥库 AES-GCM 加密保存模型 profile 和用户 MCP。1.0 界面与保存入口
  强制选择 GLM；升级时删除旧独立视觉字段及其已存凭据。
  密文存于不参与备份的私有目录；界面读取仅返回“已配置”标志。
- 运行时仅展开选中 profile，0600 临时配置由本机 bridge 消费后删除；重连复用
  bridge 内存，配置更新覆盖缓存。普通工具不继承凭证环境变量。
- 服务地址改变时，空白密码不会把原凭证带到新地址。MCP 默认本机服务，远端逐项授权。
- Gateway 移除隐藏多图片流水线，附件传真实文件引用；推理强度遵循模型配置。
- 原版 Codex 已由 0.153.3 升到 0.153.4 离线 overlay，保留原 Linux/PRoot 归档及旧 Codex；
  手机 Gateway 改为直连官方 app-server，自维护 stdio/WebSocket bridge 不再打包启动。
  详见 `CODEX_OFFLINE_UPGRADE_20260905.md` 与 `PROVIDER_INTEGRATION_20260905.md`。

PRoot 同 UID 进程不提供对恶意 Agent 的强凭证隔离；未作这种安全承诺。

## 已观察证据

| 项目 | 结果 | 边界 |
|---|---|---|
| ALN 连接与型号 | PASS | 明确指定 YOUR_DEVICE_SERIAL，get-state=device，ALN-AL00 / Android 12 |
| 初始第三方应用盘点 | PASS | 22 个第三方包，小红书保留；三个俄区目标 App 与单机 APK 未安装 |
| 离线 Python / Node 测试 | PASS | 当前 50 项：25 项 provider/adapter/vision/bridge、13 项 overlay、6 项账本、6 项依赖库存；无真实模型请求 |
| 第一轮 Android 回归 | FAIL | 341 项中 1 项图片会话恢复失败，需修正并复验 |
| 修复后 Android 回归 | PASS | app 341/341、配置存储 12/12；appliance debug/release 构建及 release lint 通过 |
| dev14 持续签名候选 | PASS | debug 与 release 均以项目证书签名；这不等于真机或模型验收 |
| 设置密码在设备上加密保存/恢复 | BLOCKED | dev14 APK 尚未覆盖安装验证 |
| GLM 文字、工具与原生图片真实接入 | BLOCKED | 用户凭证尚未在手机安全配置 |
| 七场景 21 项及七项换表述 | BLOCKED | 未开始 ALN 真实 Agent 用例，分母保留 |
| 私有 GitHub 交付与最终签名 | BLOCKED | 工程检查点，未作为 1.0 交付 |

首轮 Android 构建日志：`/private/tmp/chebyagent-aln-build-20260905.log`。
后续结果追加，不能覆盖首次失败事实。

## 回退材料

本轮重建前 APK 与既有归档
`artifacts/private/releases/standalone-yandex-go-business-dev-20260811/ChebyCodex-0.6.6-yandex-go-business-dev.apk`
逐文件 SHA256 均为 `536f2e4eb608c717e84fe93e5bab90d8f6bc313f29033687cc10443080b86f18`。
归档还保留 SHA256SUMS 与旧报告；旧报告不是 ALN 通过证据。

未卸载手机应用，未清除任何用户数据，未提交交易，未推送远端或公开发布。

## 目标恢复后检查点

用户删除目标后停止，再按“恢复”重新建立完整 active 目标，无 token 预算。
指定 ALN 的 get-state 再次为 device。已修复关闭视觉的 MCP transport 和
模型 catalog 空基础指令两项启动缺陷；当前保留固定 0.153.4 上游完整指令原文及 hash。
新增回归后 Python/Node 组合 **34/34 PASS**，尚未代替 Android 与设备验证。

补入理发、按摩、大型犬洗护、Cian 租房、Go 外卖五个 draft Skill，结构校验通过，
已接入离线打包与初始化；不标为新手机已验证。旧打车 Skill 在本私测版禁用
确认后的最终下单，菜单 Skill 移除隐藏 Gateway 转录假设，使用独立视觉 MCP。
中文纯手机指南已形成待实测草稿：docs/architecture/ALN_PHONE_INSTALL_GUIDE.md。

## 完整回归与签名检查点

第二轮 Android 编译成功，图片恢复用例通过；完整回归中既有 Relay 连接测试
在 15 秒限时内超时（日志 `/private/tmp/chebyagent-aln-build-r2-20260905.log`）。
未修改该用例或增加超时时间。单独复验 Relay 测试通过，随后取消测试过滤，
完整应用回归 **342/342 PASS**，配置存储测试 **12/12 PASS**（后者复用本轮已通过结果）。
完整结果日志：`/private/tmp/chebyagent-aln-full-recheck-20260905.log`。

项目独立 PKCS12 签名材料已生成并限制为当前用户访问，未加入源码或发布资产。
签名工具首次尝试因同一单行密码文件被读取两次而失败；调整为 PKCS12 共用密码后，
签名及 apksigner 校验通过，未更换证书。开发 APK 的静态检查 12 项全部通过。

- APK：`artifacts/private/releases/aln-private-dev-20260905-r1/ChebyAgent-0.7.0-aln-private-dev.apk`
- SHA256：`f1a155aa5def0c6552fd33bbcfb2f98f61308a6f7353dc1bbbf04919a467f1f0`
- 签名后静态报告：`/private/tmp/chebyagent-aln-static-signed-20260905.log`
- 本件是可调试开发检查点，不能作为最终 1.0 发布包。

ALN 安装前再次确认 `com.termux` 不存在；已传输 APK，系统显示“继续安装”。
远程点击后再次截图仍显示该提示，未生效，已请用户在手机上手动确认。
安装命令仍在等待，不能报告安装完成。首次初始化、模型接入与全部真实场景仍须各自验证。

依赖库存已由独立子任务完成并停止写入，见
`docs/licensing/COMPONENT_INVENTORY_20260905.md`；223 个包均保留在清单。
子任务报告针对性测试 6/6 通过，完整源码及归属材料的缺项仍待完成。

## 非调试候选与源码候选扫描

`:appliance:assembleRelease` 及 `lintVitalRelease` 通过；没有修改应用源代码，
也未因生成候选而变更设备上的待安装流程。

- 非调试候选：`artifacts/private/releases/aln-private-candidate-20260905-r1/ChebyAgent-0.7.0-aln-private-candidate.apk`
- SHA256：`cb05a90f86bccc02c31c60f7e34bc9c4eb5410bb4e7943eddf143db8155e3519`
- 项目签名验证通过，现场比较确认与开发包使用同一证书。
- 静态检查新增 `--require-release`，本候选 **14 项通过**；开发包的反向检查按预期
  因 `not_debuggable` 失败。构建、签名和检查报告均归档在候选目录 `evidence/`。
- 版本标识仍为当前开发里程碑，此件不是已通过全验收的 1.0 包，不发布。

源码及 HEAD 可达历史扫描覆盖 815 个当前文件、1540 个历史 blob，约 30.2 MB。
只扫描当前 HEAD，不遍历其他分支或相邻工作区。10 项匹配逐项复核为显示脱敏测试中的
固定字母数字样例、模拟 provider 用例默认值或扫描器自身的私钥标记文本。
报告保存在 `artifacts/private/audits/source-secrets-20260905-r1*.json`，不输出匹配值。
该扫描只覆盖选定模式和源码范围；忽略目录内离线归档、最终导出及用户配置仍需独立检查，
不能称全部发布材料已脱敏。

## ALN 安装确认推进

用户明确授权继续处理手机安装后，通过当前系统 UI 获取准确控件位置，
依次完成风险提示、勾选已了解风险和继续安装。未修改纯净模式或安全策略。
先前普通点击未推进的状态已被新的现场结果取代。

系统随后显示“请输入锁屏密码继续安装”（Huawei coauthservice），需要用户直接在
手机上完成身份验证。已通知用户，不收集或记录密码，不尝试猜测或绕过。
当前安装会话仍保持等待，没有重复发起安装，尚无 SUCCESS 返回。
证据是本机设备 UI dump `/data/local/tmp/cheby-install-next.xml`；受保护页面截图
返回空文件，不能把空文件作为图像证据。

根 README 已改为当前单机私测说明，保留真实验证边界。没有提交、推送或公开发布。

## 私有源码检查点归档

后续已创建私有仓库
`https://github.com/yangyanping7133-stack/ChebyAgent-Russia-standalone`，
现场 API 验证 `isPrivate=true`。从当前工作区导出独立源码副本并初始化独立 Git，
原工作区及其共享 Git 配置保持无远端，未修改相邻分布式工作区。

- 副本：`artifacts/private/source-snapshots/aln-source-20260905-r2`。
- 816 个源码文件及一个来源清单；逐文件 SHA 校验通过。
- 导出扫描覆盖 817 文件，三个候选匹配已复核的测试样例及检测标记，
  报告 `artifacts/private/audits/source-snapshot-r2-review-20260905.json`。
- 副本作为全新根提交检查时发现既有 Markdown 空格、末尾空行及原版 Codex 指令
  的尾空格。为保持已构建源码和固定上游指令原文，没有修改这些字节；
  原工作区改动的 `git diff --check` 仍通过。
- 分支 `codex/aln-private-checkpoint`，提交
  `6473fe06ec331e04556e5dbfce098bade2392cfb`；GitHub API 返回相同提交。
- 初次 HTTPS push 因未配置命令行凭证助手失败，随后仅对该命令使用已登录的 gh
  凭证助手并成功，未导出令牌或修改全局 Git 配置。

这是源码工程检查点。尚未创建最终版本标签或发布 APK / 离线资产，
也不代表真机初始化、模型接入、场景测试或 1.0 交付通过。
当前安装会话现场复查仍在运行，系统密码验证待用户在手机上完成。

## ALN 安装完成

用户告知已取消锁屏密码后，原安装会话终态退出码 0，输出先记录
`INSTALL_FAILED_ABORTED: User rejected permissions`，随后同一 adb 调用自动执行
`Performing Streamed Install` 并返回 `Success`。没有另起重复安装。
`pm path com.termux` 现场返回已安装的 base.apk 路径，安装等待状态据此解除。
此时尚未启动应用，不把安装成功算作离线初始化或模型验收通过。

## r7 低推理与历史备用凭据检查点

手机模型设置已收敛为 GLM 5.3 Flash 单模型执行，默认推理强度为 `low`。
当时 MiniMax M3 只增加为备用 Token 槽，不参与当前任务路由；两项 Token 均由
AndroidKeyStore AES-GCM 密文保存，界面只显示“已配置”。r6 覆盖安装后保存两项
凭据，强制停止并重启应用、再覆盖安装 r7 后，两项仍显示“已配置”，且 `low`
保持选中。该入口现已移除，升级读取配置时会删除旧 MiniMax 视觉凭据。

截图传输调整为长边不超过 1280 像素、JPEG 质量 75、编码不超过 512 KiB，并保留
原屏幕坐标缩放信息。GLM adapter 改为每轮清空未回放的隐藏 thinking，runtime marker
更新为 `4.1.0-dev17`。当时的 provider/vision 26 项和离线 overlay 14 项回归通过，appliance
单元测试及 release 构建通过。

- r7 APK：`artifacts/private/releases/aln-private-candidate-20260905-r7-thinking-fix/ChebyAgent-0.7.0-aln-private-candidate-r7-thinking-fix.apk`
- SHA256：`e24ef445f7902256a3827e90360bbedadaf3e83a9072657fa3b0dcff3b53c464`
- 签名证书 SHA256：`dc9a4b8cbefe4da91e1a56233b3d2f164d651df1dcc27996ff8ce381c8bb2730`
- release 静态检查：14/14 PASS；ALN 覆盖安装：PASS。

真实手机回归中，重新开启安装后被系统关闭的无障碍服务后，Codex 调用
`android_capture_screenshot` 已返回真实 JPEG 并在会话中显示“完成”；但官方
app-server 尚未发起携带该 MCP 图片结果的下一次模型请求，最终页面标题没有返回。
同机纯文字及本地命令回合可以完成，因此图片工具闭环仍为 **BLOCKED**，不得记为
GLM 原生图片验收通过。21 个场景和 7 个改写仍未开始。

当前 r7 每次通过 adb 覆盖安装仍传输完整 APK。Android 代码、权限、Manifest 或内置
大归档变化必须重新签完整 APK；仅运行脚本、provider adapter 和提示词的后续变更可在
实现“签名小补丁 + 手机端摘要/版本校验 + 原子替换 + 回退”后脱离 379 MiB 全量安装。

## r8 原生截图闭环与安装后复验

r7 的诊断探针记录到 Codex 正常退出、三次模型请求、真实手机 JPEG 和后续工具回合，
但 `live_screenshot_native_image_present=false`：官方 app-server 将 MCP 截图结果保存为
本机文件后，在下一轮交给 provider 的只是含 `artifact_path` 的字符串。此前页面里显示
“手机截图/完成”只能证明工具产物存在，不能证明 GLM 看到了图片。

dev18 在 GLM adapter 增加严格限定的回收层：只读取
`/root/.cheby/phonebridge/artifacts` 内的常规 PNG/JPEG，拒绝符号链接、路径逃逸、空文件、
非图片及超过 2 MiB 的文件，再作为紧邻工具结果的原生多模态消息交给 GLM。针对字符串化
截图、无效图片与符号链接逃逸均增加回归；provider/vision **28/28 PASS**，离线 overlay
**14/14 PASS**，release 构建与 lint 通过，release 静态检查 **14/14 PASS**。

- r8 APK：`artifacts/private/releases/aln-private-candidate-20260905-r8-native-vision/ChebyAgent-0.7.0-aln-private-candidate-r8-native-vision.apk`
- SHA256：`7c1676ecc67bbd8d6bef915cc043ae72af294a24f5d0b626f4a86efe31f8da9b`
- 签名证书 SHA256：`dc9a4b8cbefe4da91e1a56233b3d2f164d651df1dcc27996ff8ce381c8bb2730`
- 设备安装包 SHA256：与 r8 候选一致；覆盖安装 PASS。

覆盖安装后系统再次关闭无障碍服务，现场恢复并验证为
`com.termux/com.chebysight.chebyagent.android.AgentAccessibilityService`。本机 Codex 随后
重新显示“已连接”，既有 GLM 配置和会话仍可用。第一次 ADB 自动输入被截断，GLM 正确
追问包名，该回合不计入验收；第二次在全新会话完整输入后，Codex 打开 `ru.vk.store`、
截取手机画面，GLM 最终仅回答 `VK ID`。安装后证据为：

- `artifacts/private/evidence/aln-r8-installed-native-vision/rustore-native-vision-installed-r8.png`
  SHA256 `f52f989cc61a3809e362bcb7f513e5100d5bd5262acd95fc7926e8deb6b30927`
- `artifacts/private/evidence/aln-r8-installed-native-vision/rustore-native-vision-installed-r8.xml`
  SHA256 `fe997626deadf86045b9b7842ad28c36082edb17f9743c035de05c6da3a3c4ba`

因此 r7 的图片闭环阻塞已被 r8 的真实语义结果取代。仍未完成：七类 21 个场景、7 个
改写、俄区 App 安装与账号登录、来源许可收口、最终私有 GitHub 标签及最终 APK 交付。

## r9 结构化展示与 ALN 候选验收

r9 为旧版扁平 comparison JSON 增加本地结构化展示兼容，完整 Gradle 构建、测试、
release lint 与 14 项静态检查通过。持续使用同一私有签名证书。

- r9 APK：`artifacts/private/releases/aln-private-candidate-20260905-r9-structured-ui/ChebyAgent-0.7.0-aln-private-candidate-r9-structured-ui.apk`
- SHA256：`046cd33b3242fc1a10b79d4d1b22ea1e1ac9bbae05d02f7e2cbfab6c6f4f00e8`
- 签名证书 SHA256：`dc9a4b8cbefe4da91e1a56233b3d2f164d651df1dcc27996ff8ce381c8bb2730`

ALN 上安装的 `base.apk` hash 与 r9 完全一致；冷进程重启后 16 秒内恢复两项服务、
本机 Codex 连接和历史。真实截图用例返回内嵌手机截图，GLM 正确读出
`本机 Codex 已连接`。设备安装、冷重启、无障碍截图三项记为 PASS。

主基准当前已判定：租房基础 PASS、外卖基础 PASS、外卖约束 FAIL、外卖确认 PASS。
外卖约束失败原因是模型在要求“热的俄餐”时选择了培根卡邦尼；虽然预算和付款前停止
均正确，但不能用后续购物车展示通过覆盖该语义失败。候选验收账本位于
`artifacts/private/evidence/aln-r9-acceptance-20260905/`。

手机整机重启已发起，但重启后 Mac 暂时看不到 USB 设备；首次解锁/USB 恢复前，
`device-reboot-reconnect` 保持 BLOCKED。未把等待人工解锁当作通过。
当前覆盖升级仍是约 379 MiB 全量 APK；签名小补丁机制尚未实现。

验收账本已升级为 schema 2，在原 37 个功能/设备/供应商用例外，增加 6 个
交付门槛：手机指南、对应源码、许可/notice、持续签名、私有标签和最终 APK。
升级保留原结果不变，当前总计 43 项：9 PASS、1 FAIL、33 BLOCKED；
`release_ready` 仍为 false，`verify-release` 以非零状态拒绝发布。

持续签名恢复演练使用当前保留的私有 PKCS12，从 release unsigned APK
重新签出一份临时 APK；`apksigner` 验证通过，密钥库、临时签名件和 r9
候选件的证书 SHA256 均为
`dc9a4b8cbefe4da91e1a56233b3d2f164d651df1dcc27996ff8ce381c8bb2730`。
密钥/口令文件权限为 `0600` 且被 Git 排除；临时 APK 已删除。证据见
`evidence/delivery-persistent-signing.json`。这只关闭“现有密钥可恢复签名”的子门槛；
最终 APK 未封版、最终回退/更新指南未走查，因此 `delivery-persistent-signing`
仍为 BLOCKED。

源码凭证扫描增加 GLM 常见的“32 位标识.密钥段”形状，报告不输出命中原文。
当前工作树 836 个源文件与 HEAD 可达的 1600 个历史 blob 扫描得到 13 个
候选；逐项复核均是脱敏器测试值、本机探针测试值或私钥检测字面量，
新增的 GLM 形状命中为 0，未发现真实凭证进入当前扫描范围。扫描及复核证据见
`evidence/source-secret-scan-current-r2.json` 和
`evidence/source-secret-scan-review-current-r2.json`。忽略的私有资产、嵌套归档和最终
发布包仍需单独扫描，私有 GitHub 标签也尚未创建。

候选项目源码快照 `aln-source-snapshot-20260905-r2` 已从 Git 管理的文件与当前
未忽略改动生成，自动排除 `artifacts/private/` 和密钥类忽略项。清单覆盖
837 个文件，离线验证逐项核对文件集、类型、权限、大小和 SHA256，并会拒绝
篡改、额外文件和符号链接。对快照本身的 838 个文件（含清单）进行凭证扫描，
4 个候选均复核为测试值或检测字面量，GLM 形状命中为 0。证据见
`evidence/source-snapshot-secret-scan-r2.json` 及其 review 文件。该快照仍包含未提交
修改，也未与最终 APK/标签绑定，不将 `delivery-source-archive` 标为 PASS。

## Termux bootstrap 对应源码收集

以已验证的 75 个实际 bootstrap 二进制包和 r3 固定配方证据为输入，新增只读配方解析器，
收集 71 个母包的 123 项上游/生成源码输入。收集器不 source 或执行任何配方 shell；
只展开白名单内的标量表达式，并逐项验证配方声明 SHA256 或解析后的 git commit。

- 输出：`artifacts/private/sources/termux-bootstrap-sources-20260905-r1/`；
- 464 个文件，`840875088` bytes；
- manifest SHA256：`b2ea3d98621cd4e98ac41d9a264f6591b459db97b9266e65deb0935c108ca2d3`；
- 针对性测试 6/6 PASS；仅使用输出目录的离线 `--verify` 返回 PASS。

旧 URL 漂移按精确 hash 使用官方或发行镜像回退，不接受版本相同但 hash 不同的文件。
唯一特殊项 `foot 1.21.0` 明确保留历史字节缺口：配方声明的 Codeberg 压缩包当前被限流，
Gentoo distfile 的压缩 SHA 不同，但其解压源码树与独立镜像标签解析到的固定提交
`68f5eab0b0fa08becebbed412947ba19246c2518` 逐文件一致；两份归档均保留以便离线复核。
`command-not-found` 当年的未固定 master `repo.json` 也不能重建为同一历史字节。

这完成了 bootstrap 源码输入的保存，不等于可重现二进制、完整 notices 或再分发合规完成。
手机仍未恢复 USB 枚举，真实重启恢复和剩余用例状态没有变化。

## r10 GLM 原生图片技能收口

对 9 个随 APK 打包的生活技能进行发布前复核时，发现其文字仍要求在截图像素不可读时
调用独立视觉 MCP，与本轮“只用 GLM 5.3 Flash 原生图片”的冻结范围冲突。r10 已将所有
技能统一为：UI 节点可读时使用节点；截图或附件只有实际图片字节进入下一轮 GLM 输入后
才算视觉证据；路径、文件名和 unsupported-image 占位符都不能当作看过图片；独立视觉
MCP 在本版禁用。菜单多页流程也要求每页实际图片进入 GLM，不再按路径转交视觉服务。

外卖约束失败同步形成最小修正：用户指定菜系时将其视为硬约束，必须由可见分类、餐厅或
菜品文字证明；“热菜”不能推导为“俄餐”，找不到合格俄餐时明确报告不可用或未知，不以
意大利菜或其他菜系替代。该修改尚未在真机重跑，因此原 `food-constraint` 仍保持 FAIL。

应用旧历史兼容逻辑不再保留“交给视觉 MCP”的提示文本，只识别旧路径记录并隐藏私有路径。
release static gate 新增技能路由检查，要求 9 项均声明 GLM native image，并拒绝旧的独立
视觉调用语句。构建时还发现固定的 0.153.4 上游基础指令少了原文中的一个尾随空格，导致
启动器 SHA 校验失败；现已从固定源码归档恢复逐字节原文，SHA256 为
`ac8ae107a0d72fe3476b430afb161ea4e67da2e446d778aefc44828160559807`。

- r10 APK：`artifacts/private/releases/aln-private-candidate-20260905-r10-glm-native-skills/ChebyAgent-0.7.0-aln-private-candidate-r10-glm-native-skills.apk`
- 大小：`397178785` bytes（约 379 MiB，仍为完整 APK 更新）。
- SHA256：`993ec68f77e439ea2db9ad1353a65e3992ead6db8365c29cd97947da301a9873`。
- 签名证书 SHA256：`dc9a4b8cbefe4da91e1a56233b3d2f164d651df1dcc27996ff8ce381c8bb2730`，与 r9 一致。
- 离线回归：overlay 16/16、provider/adapter/image 28/28、ledger 10/10 PASS。
- Gradle：459 个任务成功，包含两组单测、debug/release 构建和 release lint。
- release 静态检查：15/15 PASS，新增 `runtime_skills_native_glm_images`。

证据位于 `artifacts/private/evidence/aln-r10-build-20260905/`。r10 尚未安装到 ALN；
2026-09-05T19:59:08Z 复查时 ADB 列表为空，macOS USB 清单也没有该手机，所以设备重启
恢复、精确文字请求和 food-constraint 重跑都不能提前记为通过。r9 的 9 PASS、1 FAIL、
33 BLOCKED 仍是当前真实验收账本，r10 只是一份已构建、已签名但未实机验收的新候选。
早期源码快照已被后续修改取代；最新候选快照的具体目录、文件数和清单 hash 记录在
`artifacts/private/evidence/aln-r10-build-20260905/source-snapshot-current.json`。
该候选快照仍不等于最终标签或合规完成。

r10 又补了两层交付物脱敏核查。加入 APK 与许可账本工具后重新冻结最新候选快照：
源码清单 841 个文件，离线逐项验证通过。当前工作树 841 个文件和 HEAD 可达 1600 个
历史 blob 重新扫描，共
14 个候选；其中唯一的 GLM 点号 token 形状命中来自 APK 扫描器自身的合成检测测试，
其余仍只来自测试假密钥和防御性检测字面量。签名 APK 的 816 个 ZIP 成员全部在 ZIP
解压后扫描，共 398267628 bytes，
候选为 0；APK 内固定的三项大运行归档则由独立 r2 归档审计覆盖。0.153.4 Codex 归档
8 个成员、291867383 bytes，候选为 0；其他三项固定归档与已完成 55 项候选复核的 r1
扫描报告逐字节一致。以上降低了“token 被打入交付物”的风险，但不构成对任意二进制的
数学式无秘密证明，也不改变 r10 尚未真机安装和整体验收未完成的状态。

许可库存也已按 r10 实际固定归档重建为 r3，纠正旧清单中的 Codex 0.153.3 和旧 prompt
hash。新增 fail-closed 交付许可账本，覆盖 367 个运行/Maven 组件行并逐项显式标为 BLOCKED，
而不是保留模糊的 `NOT_ASSESSED`：5 个运行包缺直接许可证路径、5 个 Maven POM 缺许可证
声明，项目自有许可决定、逐组件义务判断、JNI/最终 APK 字节映射及最终源码交付绑定仍未
关闭。验收账本的 `delivery-license-notices` 已附该证据但继续保持 BLOCKED。
