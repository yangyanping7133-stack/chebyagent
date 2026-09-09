# 日常场景续验：2026-09-08

## 08:12–08:17 UTC：CIAN恢复；短指令遇模型连接403

上一目标轮是progress：提交3c1adb2、同签名装机、24项保留和现金真实拒绝证据。
本轮未重装、清数据、改模型/认证、改Mac或手机VPN、开浏览器或绕过资金保护。
完整场景目标仍未完成，不用本次访问恢复代替租房验收。

- USB设备YOUR_DEVICE_SERIAL在线，初始前台系统桌面，实际/proc未发现Codex
  app-server或PhoneBridge进程。正常打开Cheby后，本地链路自然恢复，
  phone_status在线且accessibility_enabled/bound=true，未手工写无障碍设置。
- 08:13打开原生ru.cian.main，加载后出现可操作首页、租房入口、真实房屋照片
  和售房价格，而非此前403。主审实际查看截图，私存
  artifacts/private/scenario-acceptance-20260908/cian-recovered-home.jpg。
  首页推荐落在Smolensk一带的售房；不视为公司周边租房合格结果。
- 当前租房Skill手机与源码SHA256一致：
  aeb3891c3db9a6a2318f212a15d9b0fce0fa64e2215be08ad2ce9f4684e12f56。
  手机auth、private/user-preferences、mem0-export文件存在；本轮只核对存在，
  不把它提升为认证有效或全部持久文件内容重新验收。
- 返回Cheby创建空手机会话，真实输入并发送仅“找房子”；输入截图私存
  cian-short-intent-composed.jpg。手机会话01a08015-0b04-7951-b26a-501becae95a8，
  固定证据/root/.codex/sessions/2026/09/08/rollout-2026-09-08T08-14-17-01a08015-0b04-7951-b26a-501becae95a8.jsonl。
  task_started为08:14:36.372Z，user原文08:14:41.477Z；未注入结果。
- 08:15–08:17观察未出现模型回复/工具/图像或terminal事件，客户端显示
  “状态待同步·暂停重试”。实际app-server PID30321存在；不能因无输出
  断言回合已停止，也没有重复发送或重启回合。
- app-server日志08:14:18以及08:14:42–08:14:52连续记录ChatGPT Responses
  WebSocket HTTP403；没有足够证据判定是认证过期、账户限制或具体网络原因。
  Android当时只有已验证CELLULAR默认网络174，无VPN NetworkAgent，
  com.v2ray.ang无活动Service。此为同时观察到的网络状态，不是403因果证明。
  官方认证页已查阅：https://learn.chatgpt.com/docs/auth；未据此清除认证重登。

下一步先核对该固定回合的终态和当前模型连接/手机已配置网络的状态，
不要盲重发“找房子”或重新安装。恢复连接后原回合/明确终态后的受控续跑
仍须核实租赁类型、公司实际步行2km、月租105000、每张照片、费用、覆盖和
约十套图文原链接。其他理发/按摩/洗狗/打车及客户端质量缺口全部保留。

## 用户开启手机 VPN 后：原回合已出现响应

- 用户明确要求以后手机任务前先开 VPN，并说明本次已自行开启；不改 Mac VPN。
- 后续现场检查 Android NetworkAgent 175 为 VPN CONNECTED、VALIDATED，
  底层为 CELLULAR 174。此状态取代上面的早先无 VPN 观察。
- 重新读取同一固定回合，发现 08:16:44.747Z 已记录模型回复，说明将通过
  Cian 原生应用按通勤、预算、中介费偏好筛选且不联系房东或提交申请；
  08:16:45.436Z 发出读取 cian-rental-finder/SKILL.md 的工具调用。
  因此上文“08:15–08:17未出现回复”的观察已被后续回读纠正，不能作为终态结论。
- 未重发找房子、重启回合或改认证。当前只证明原回合开始响应，尚无合格房源、
  照片审核、费用和实际步行路线的验收证据；不把 VPN 同时恢复当作 403 完整因果证明。

## 08:21–08:29 UTC：回合仍活着，但模型调用间隔约五分钟

上一轮属于 progress：保存用户明确的手机 VPN 前置偏好，确认 VPN 已连上、
原回合出现模型响应。本轮不把相同状态重述算作新增验收。

- 实际进程 PID30321 仍存在，08:21:55.934Z 新增发现 ACE/PhoneBridge 工具调用，
  08:27:09.232Z 新增并行读取 ACE、私人偏好、手机状态、能力的调用。
  三次工具结果分别在 08:16:45.605Z、08:21:56.120Z、08:27:09.363Z 返回，
  工具耗时 0.0–0.1 秒；相邻模型调用约隔 310/313 秒。
  仍未进入 CIAN 筛房，模型输入图像数为 0，无终态，不重发或重启。
- 实际查看手机截图 cian-waiting-model.jpg：真实找房子输入、折叠过程、
  本机连接标记和禁用发送按钮仍在。连接标记只说明本机连接，不证明模型健康。
- 手机 VPN NetworkAgent175 仍 CONNECTED/VALIDATED，轻量查看 v2rayNG
  界面显示土耳其出口、连接成功和 162ms；未切换服务/配置，随即返回 Cheby。
- 手机 Debian 和 Android 原生 curl 访问 chatgpt.com 均观察到 TCP 连接超时；
  IPv4 超时、IPv6立即失败；同轮 example.com 为200而 yandex.ru 超时。
  后续对 ChatGPT models 接口的不带认证只读探测，直连及现有 localhost:10808
  SOCKS 分别约 0.77/0.64秒返回401（只证明网络可达，不是认证成功）；
  随后实际认证只读查询又在10.21秒发生URLError，两条已解析 IPv4 再次超时。
  未打印凭据、请求/响应正文，未修改代理、DNS、模型或登录。
- 证据支持网络连通存在波动，不能确定是哪条分流规则、VPN 节点或模型服务问题，
  也不能断言约五分钟间隔完全由网络造成。App Server 同时仍记录模型目录刷新子进程超时。

后续先解决/核实真实模型链路的连续调用能力，再验收房源。保持固定原回合，
用新工具动作或明确终态判断执行，不把进程存在、VPN成功或偶发HTTP返回当成场景成功。

## 08:49 UTC：仅 ChebyCodex 使用手机 VPN；CIAN 恢复首页

- 用户随后手动重启 App、创建新会话并发送“找房子”。新会话
  01a08031-0f45-78b3-9d09-3c62dfda183c 于08:45:05收到真实输入，
  08:45:08响应，08:45:41明确报告 CIAN 的403并结束，没有合格房源。
  此事件取代继续等待旧回合的建议；本轮未重复发送任务。
- 用户明确要求仅 ChatGPT 使用 VPN，其他 App 不使用。手机未安装单独的
  com.openai.chatgpt；实际模型客户端为 ChebyCodex/com.termux，UID10233。
- v2rayNG 开启“分应用”，关闭“绕行模式”，仅勾选 com.termux。重新启动
  现有节点后，Android VPN NetworkAgent183 CONNECTED/VALIDATED，作用范围
  明确仅 Uid10233，OwnerUid10241；CIAN10237、Yandex Maps10235不在其中。
  此为按 App 分流，不是 ChebyCodex 内按模型或域名分流。
- 生效后读取手机现有登录状态进行只读模型目录检查，三次 HTTP200，耗时
  1.74/1.61/1.61秒；凭据与响应正文不输出。服务尚未启动时的三次403不计成功。
- 正常终止并重新打开 CIAN（不清数据、不退出登录），真实首页、照片、价格和
  租房入口恢复；截图 cian-after-cheby-only-vpn.jpg 已实际查看。只证明访问恢复，
  未筛选、未联系、未预约，不代表找房场景通过。
- 无障碍启用且服务绑定正常；保留 com.termux 的原授权。Mac VPN、服务端配置、
  模型、登录凭据均未改变。本轮分流设置截图 vpn-cheby-only-configured.jpg。
- 前一诊断阶段曾试 MTU1280 后恢复1500；手机 Hev TUN 已关闭改用内置
  xray-core。单次切换曾出现成功后再超时，不能单独归因于 TUN 修复。
  土耳其服务器只读核验端口及出站正常，未修改或重启；现以分流后的证据为准。
