# ALN 离线运行归档内容核查

日期：2026-09-05。状态：内容扫描及 55 个候选复核完成；不作为完整脱敏证明。

`tools/standalone/runtime_archive_audit.py` 在读取前验证四个固定归档的 SHA-256，
以流式方式扫描所有普通文件，保存成员路径、类型、字节数、内容 SHA 及脱敏候选位置。
不执行归档代码，不把归档路径或链接释放到主机。跨读取边界的模式识别、去重和
危险成员路径拒绝已做针对性检查。脚本没有修改运行归档或 APK。

| 归档 | 成员数 | 普通文件扫描字节 | 候选数 |
|---|---:|---:|---:|
| Debian rootfs | 8692 | 644867237 | 41 |
| Termux bootstrap | 3490 | 70758704 | 14 |
| PRoot overlay | 32 | 396324 | 0 |
| 原版 Codex 0.153.3（本报告扫描快照，现已被 0.153.4 候选取代） | 8 | 291867383 | 0 |

升级前证据目录：`artifacts/private/audits/runtime-archives-20260905-r1/`。该目录保存的是
0.153.3 归档证据。
逐归档 JSON 保留所有成员，`summary.json` 保存总数和校验值，
`candidate-review-r3.json` 保存最终逐项复核结果；前两轮的失败及待审结果保留，
未输出匹配到的密钥正文。

r10 已对实际固定的 Codex 0.153.4 重新生成不可覆盖审计：
`artifacts/private/audits/runtime-archives-20260905-r2/`。Debian、Termux bootstrap 和
PRoot overlay 的输入及扫描报告与 r1 逐字节一致，因此复用原 55 项候选复核；新的
Codex 0.153.4 归档扫描 8 个成员、291867383 bytes，候选为 0。复用条件、双方报告
SHA256 和边界记录在 `candidate-review-reuse.json`，不再沿用“0.153.4 尚未扫描”的旧状态。

## 候选复核

55 个候选中，13 个是 SSH 公开算法名称；10 个格式或诊断字符串与固定上游源码
中的 C 字符串相符；24 个完整 PEM 样例与 GnuTLS 3.8.9 的公开源码内容逐字相符。
剩余 8 个 Debian OpenSSH 二进制标记随后核对为 `sshkey.c` 的固定 `MARK_BEGIN`
内容；匹配区域之后没有 PEM 换行或 base64 正文。这里的二进制常量区域不一定是
单独的 C 字符串，先前用第一个 NUL 作为完整字符串边界导致过强的后缀假设。
第三轮保留成员 SHA 和具体偏移，仅把检测到的区域归为公开格式标记，不推断
同一二进制的其他任意字节已经通过秘密内容证明。

来源取自 [GnuTLS 官方下载入口](https://www.gnutls.org/download.html) 和
[OpenSSH 官方 portable 下载入口](https://www.openssh.org/portable.html) 指向的分发站点。
保存的原始归档如下；本轮核验 HTTPS 获取结果与现场 SHA，尚未验证发布者签名，
也没有因此补齐 Debian 或 Termux 的全部对应构建材料。

- GnuTLS 3.8.9：`artifacts/private/sources/gnutls-selftest-review-20260905-r1/`；
  source SHA-256 `69e113d802d1670c4d5ac1b99040b1f2d5c7c05daec5003813c049b5184820ed`。
- OpenSSH 10.0p1：`artifacts/private/sources/openssh-marker-review-20260905-r1/`；
  source SHA-256 `021a2e709a0edf4250b1256bd5a9e500411a90dddabea830ed59cef90eb9d85c`。

初次候选审查对标记后缀作了过强假设，断言失败且未形成最终报告。
第二次审查仅在实际公共源码匹配成功时分类，其余明确保留待审。

## 用户状态与边界

扫描的路径规则没有命中 `.ssh`、`.cheby`、`.codex`、用户认证配置、签名密钥文件
或捕获脚本排除的用户状态路径。Debian 的 shadow/gshadow 及备份共检查 106 项，
密码字段均为锁定或未设置值，没有密码哈希。`etc/hostname` 已实际读取为固定
`debuerreotype` 字符串；没有将它误报为用户认证信息。检查没有修改归档内容。

检查覆盖普通文件原始字节，未递归解码文件内部的嵌套归档或反编译二进制，
也不覆盖未来配置、运行后数据或全部可能凭证格式。最终 APK 和交付资产仍需与
已核查归档逐项对应。完整真机、模型、场景和回退验收均保持原要求。
