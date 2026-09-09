# ChebyAgent 俄罗斯生活助手 1.0：私测交付目标

目标评审日期：2026-09-05。本次参谋回合只完善目标文档，不继续改产品代码、
操作手机或推送仓库。以下是后续实施的交付标准，不是已完成声明。

## 一句话目标

让一位从中国来俄罗斯旅游或短期工作、不懂技术的用户，只用一部兼容的
Android 手机，安装一个 APK、完成必要授权并配置自己的模型凭证后，就能用
简短中文指令，让 Agent 在真实 App 中完成七类生活任务，并看懂结果、补充条件、
确认或取消操作。最终交付可回退的安装包、私有代码仓库和纯手机安装指南。

成功标准是“能独立使用、能真实办事、结果有证据”，不是“组件安装好了、能聊天”。

## 本轮模型范围（用户已确认）

- 1.0 只使用 `GLM 5.3 Flash`，配置用户自己的 API Token；21 个主用例及
  7 个改写用例全部使用同一冻结配置，不自动切换或回退。
- GLM 原生图片输入作为唯一视觉路径；不提供独立 MiniMax 视觉 MCP 或凭据入口。
- 已确认的是模型范围，不代表 Token 已提供、真实接入已完成或能力已验证。

## 产品与架构边界

- 目标用户与场景聚焦俄罗斯旅游及短期工作；不是通用手机自动化平台或多租户服务。
- 单 APK 内包含原版上游 Codex 和现有 Linux 运行层；不要求用户另装 Termux，
  不重写这两块。模型 API 仍需要用户自己的网络、账号和余额；单机不等于模型离线。
- UI 只承载通用输入、展示、选择、确认、取消；Gateway 只转发和管理通用协议状态；
  Node 只提供手机原子操作；推理、工具编排、并发及场景判断由 Codex 和 Skills 承担。
- Skills 面向用户意图组织：咖啡、理发、按摩、犬类洗护等各有专业判断，
  可共享 Yandex Maps 的 App 操作参考。不把所有行业塞进一个泛泛的“搜索 Skill”，
  也不把业务规则搬进 UI。只有真实跑通的路径才标记为已验证。
- 优先原生 App。单机版与非单机版继续隔离；保留旧稳定版归档，不在本次合并。
- 实验机保留小红书；清理仅限确认范围内的第三方应用，不碰系统组件。

## 分三批交付，保持完整范围

### A. 手机执行与模型底座

验收出口：用户能在手机上独立完成配置，并从聊天界面发出短指令，经模型调用
手机工具，在真实 App 中执行动作、返回可显示的截图，而不是助手在 Mac 上代操作。

1. 在 ALN 新实验机完成单 APK 安装、离线运行依赖初始化、启动、重启及重新连接检查。
   开发时可以用 Mac 安装和检查；最终用户安装流程不能依赖 Mac。
2. 使用封版时核实的最新稳定上游 Codex，锁定版本、来源及校验值；不偷偷自动升级。
3. 本轮手机主模型配置只使用 GLM 5.3 Flash 的用户凭证与服务地址；
   保留用户 MCP 服务配置能力，但不把未配置的模型或视觉服务计入验收。
4. GLM 实测：鉴权与文字回答、启用受支持的推理能力、多步工具调用，
   以及无效凭证/不可用服务的可理解错误。不得用接口格式兼容代替测试。
5. GLM 5.3 Flash 实测原生图片理解与手机截图读取，并验证模型利用图片结果
   继续执行的完整路径。图片未成功送达或服务不可用时明确解释，不伪称已看图。
6. 凭证不进入 APK、代码仓库、截图演示、日志或文档；不要求用户把密钥贴进聊天。
7. 通用 UI 验证图片上传与展示、文本/表格/候选项、进度、补充条件、确认和取消。
   中断后应恢复或明确标记失败，不能永久转圈；保持已有菜单等富消息的回归覆盖，
   本次不新增一套独立点餐产品。

### B. 七类真实任务与 Skills

验收出口：下文七场景 × 三个用例在新手机通过真实 Agent 路径完成。
三类用例分别覆盖短指令、个性约束、追问/修正/现实障碍，而不是简单换地点。

- 先选一个实测可用的主模型冻结配置，跑全部 21 个用例；每项均有判定，
  发布验收要求 21/21 达到各自终点。账号等阻塞不能从分母删除或算通过。
- 每个场景再选一个基准用例，用不同自然表述复跑一次，检查是否依赖长提示词或偶然路径。
- 不再设置第二模型代表性用例；多步操作、视觉输入和确认边界均由同一 GLM
  冻结配置覆盖，避免跨模型结果混入主分母。
- 保存原始短指令、模型/Skill/App 版本、实际工具操作、关键截图与终态、
  首轮成功与否、重试、人工介入及耗时。发生开发者代操作或临场修代码的那次不算通过，
  修复后重新跑完整用例；用户正常授权、提供必要条件和最终确认单独记录。
- 搜索场景终点是可追溯、符合约束的候选及推荐依据；导航默认步行，直接进入导航。
  不把所有任务都做成长报告。
- 租房终点是符合已确认预算/区域/租期的真实房源及总费用、限制说明，不自动联系房东。
- 打车终点是可核对起终点、Business 车型及实时价格的提交前确认状态；
  不可用则解释，不擅自降级。外卖终点是可核对商品、地址、配送及总费用的提交前状态。
  本轮测试不实际叫车、支付、预约或发消息；因此不宣称交易履约完成。

### C. 可交付、可回退、为开源准备

验收出口：一个不参与开发的人按指南，仅用兼容手机完成安装配置并跑通基准任务；
开发者能找到对应源码、构建材料和旧版交付件。

- 使用项目自有且可持续升级的签名，保存签名密钥但不提交仓库。
- 私有仓库保存经敏感信息检查的源码、Skills、构建说明、测试结论及版本标签；
  APK、校验值及必要离线材料在对应私有发布资产中归档，不混进普通源码历史。
- 保存旧稳定版及其材料，说明回退安装方式和数据兼容边界；不承诺任意版本无损降级。
- 中文手机安装指南覆盖兼容条件、APK 获取、初始化、无障碍等必要授权、
  模型与视觉/MCP 配置、必备 App、示例指令、常见问题及凭证保护。
- 准备依赖来源、版本、许可证、修改清单、构建和对应源码材料，列明待核查项。
  “不商用”不作为自动合规结论。当前只做私测与开源准备，不公开仓库或正式对外发布。
- 固定验收分母为 43 项：21 个主用例、7 个改写、5 个设备门槛、4 个 GLM
  供应商门槛和 6 个交付门槛。任一 FAIL/BLOCKED 都必须使 `release_ready=false`；
  功能用例不能代替指南、源码/许可、持续签名、私有标签和最终资产的交付证据。

## 暂不扩张

不做长期保活压力测试、深度性能调优、跨所有手机的兼容承诺、分布式版合并、
自研替换 Linux 层、重写 Codex、商店上架或公开发布。性能先完整记录，
但崩溃、永久等待、无法取消等影响功能的问题必须修复。后续登录体验重构不抢主线。

## 外部依赖及完成口径

- 尚待明确：第三方应用清理名单；尚待用户在手机安全界面配置并验证
  GLM 5.3 Flash 凭证。模型选择已由用户确认，不再重复询问。
- 俄区 App 的登录、验证码、地域服务、定位、网络及付款资料可能需要用户配合。
  Yandex Go 外卖是否可在当前地区完成需实机验证；若依赖另一个 App，先说明原因和路径。
- 每项只标记 PASS / FAIL / BLOCKED，并附证据或具体阻塞原因；
  可以归档未完成的工程检查点，但不能把它标成已通过的 1.0 私测交付版。
- 网络/VPN由用户解决；本轮不会把别人的凭证或历史账号打包给新用户。

## 2026-09-05 参谋回合现场检查点

已核对分支 `codex/aln-russia-agent-1.0`。工作区已有未提交的提供商启动脚本、
视觉 MCP 脚本及桥接入口改动；这些属于前一轮实施，保留不动，不视为已测试或已部署。
本回合仅扩充本文目标，纠正“禁止一切推送”为用户已授权的“私有交付、禁止公开”。
手机安装、配置 UI、场景测试和私有仓库交付仍需逐项以现场结果证明。

---

## 初始技术基线与用例表

Requested 2026-09-05. Experimental phone: Huawei ALN-AL00 (Android compatibility
level 12). Preserve Xiaohongshu. Keep the distributed repository independent.

## Acceptance

1. Inventory third-party apps, confirm the exact cleanup scope, install the
   offline appliance and Yandex Maps, Cian, Yandex Go. No system-app removal.
2. Bundle upstream Codex CLI 0.153.4 (npm latest rechecked 2026-09-05), with pinned
   provenance and checksums. Preserve upstream binaries and the existing PRoot
   and Debian runtime. Fresh install, launch, reconnect and a real tool action
   must work on ALN; old-device evidence does not establish this.
3. This round uses only GLM 5.3 Flash with a user-owned API key and endpoint.
   Native GLM image input supplies the only vision path. The independent
   MiniMax vision MCP is not packaged or registered.
   Preserve configurable MCP services. Never put secrets in Git, APK or chat logs.
   Saving settings, authenticating, model reasoning, tool calls, image
   understanding and a completed scenario are separate checks.
4. Seven scenarios, three cases each, with short user prompts. Skills own
   discovery, ranking, clarifications and workflows; UI renders generic rich
   messages, Gateway forwards, Node performs atomic device operations.
5. Prepare source licensing, dependency notices, build instructions and a
   sanitized export check. Private repository delivery is authorized; do not
   publish publicly. Non-commercial intent alone
   does not satisfy upstream license obligations.

## Scenarios and cases

| Scenario | Basic request | Constraint | Follow-up / boundary |
|---|---|---|---|
| Coffee / Maps | 找附近最好的咖啡厅 | 安静，能坐着办公 | 带我去推荐的那家 |
| Haircut / Maps | 找附近最好的理发店 | 男士剪发，今天营业 | 核对价格和预约方式 |
| Massage / Maps | 找附近最好的按摩店 | 正规放松按摩，90分钟 | 核对项目、时长和价格 |
| Samoyed grooming / Maps | 找附近适合萨摩耶洗澡的店 | 大型犬，洗澡吹干梳毛 | 明示体重未知，核实接待范围 |
| Rental / Cian | 帮我找合适的房子 | 用户提供预算、租期、地区 | 对比真实房源的总费用和限制 |
| Taxi / Go | 帮我打车去冬宫 | 默认 Business，明确车型优先 | 价格确认后才叫车；测试不下单 |
| Food / Go | 帮我点个外卖 | 核对地址、偏好、预算及配送 | 展示购物车和总价，测试不支付 |

“Best” means best fit among inspected candidates, not an unprovable city-wide
claim. Prefer recent review evidence, number of reviews, distance, service fit,
current opening hours and transparent costs. Unknowns remain explicit. Rentals
need budget and duration; never invent them. Go service availability and login
depend on the live app and account.

## Delivery record

Initial state: repository clean at cb534fd; old appliance 0.6.6, Codex 0.147.0.
ALN has 22 third-party applications, none of the three target Russian apps and
no appliance. 450 GB free reported. Cleanup choice remains pending. The user
subsequently narrowed the release gate to GLM 5.3 Flash only; credentials and
Russian-app login remain unverified.

Record each implementation checkpoint and test outcome in a separate dated
acceptance report. Draft skills remain unverified until executed through the
phone agent against the actual app.
