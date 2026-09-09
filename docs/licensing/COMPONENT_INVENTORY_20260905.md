# 实际离线组件库存与来源材料（2026-09-05）

> **历史库存。** 本文件记录形成 0.7.0 证据集的详细取证过程。最新的通过项、阻断项与
> 公开发布决定以 [`RELEASE_COMPLIANCE_AUDIT_0.7.0.md`](RELEASE_COMPLIANCE_AUDIT_0.7.0.md)
> 和本目录 [`README.md`](README.md) 为准。

本检查点已经从实际待打包归档读取包管理元数据和版权文件，保存可复核证据。
**包库存和许可证材料已提取；完整对应源码、通知义务与再分发合规仍未完成。**
没有操作手机、运行 Gradle、修改签名、提交代码或写入远端服务。

## 库存结果

| 来源 | 实际包数 | 直接或归档链接解析得到版权材料 | 固定源码配方版本匹配 |
|---|---:|---:|---:|
| Debian rootfs | 145 | 145 | 101 组不同 Debian source name/version 的精确 source archives 已保存并离线复核 |
| Termux bootstrap | 75 | 70 | 75/75 配方匹配；71 个母包的 123 项上游/生成源码输入已保存并离线复核 |
| Termux overlay | 3 | 3 | 0/3；三个包比 bootstrap 对应源码提交更新 |

合计 **223 个包记录、243 份归档证据文件**，提取内容约 3.85 MB。
不只依赖 `runtime.lock`：Debian 与 bootstrap 包数来自各自归档中的
`var/lib/dpkg/status`；overlay 没有 dpkg control 数据，其三包身份来自固定 lock 与原
capture 脚本，文件成员与 hash 已另行盘点，没有伪称从 overlay 读取到包版本控制数据。

Debian 文档目录的 symlink 按归档命名空间解析。Termux bootstrap 的
`SYMLINKS.txt` 也已解析。overlay 中 proot/libtalloc 的 copyright 指向合并后
bootstrap 的 `share/LICENSES/`，因此这两项有实际可读材料，不属于“链接文件存在但内容未知”。
没有在主机创建或跟随这些链接。

## 五项直接版权路径缺失的处理

原始库存中缺少包自身版权路径的五项仍原样保留，后续配方证据追加说明：

| 二进制包 | 实际版本 | 固定配方中的母包 | 新增证据与边界 |
|---|---|---|---|
| bzip2 | 1.0.8-6 | libbz2 | 精确版本的 `bzip2.subpackage.sh`；bootstrap 已有 libbz2 LICENSE |
| curl | 8.12.1 | libcurl | 精确版本的 `curl.subpackage.sh`；bootstrap 已有 libcurl copyright |
| gpgv | 2.4.5-3 | gnupg | 精确版本的 `gpgv.subpackage.sh` 和母包 GPL-3.0 声明；独立组件 attribution/notice 材料仍需从对应源码补足 |
| libsmartcols | 2.40.2-3 | util-linux | 精确版本的 `libsmartcols.subpackage.sh`；bootstrap 已有母包多份许可证文件；不能将母包所有许可证直接赋给每个子文件 |
| xz-utils | 5.8.0 | liblzma | 精确版本的 `xz-utils.subpackage.sh`；bootstrap 已有母包 COPYING 与各许可证文本 |

这些对应关系来自官方固定提交中的子包配方，不由相似包名或依赖关系猜测。
找到通用许可证文本不等于保留了该组件全部版权声明，也不等于已经履行源码提供义务。

## 实际保存的来源材料

官方 bootstrap 标签 `bootstrap-2025.03.28-r1+apt-android-7` 解析到提交
`5a6d1c1eb868795dce83a6c269387b9f82d21805`。
已保存 [该固定提交的 Termux 构建源码归档](https://github.com/termux/termux-packages/tree/5a6d1c1eb868795dce83a6c269387b9f82d21805)，
并从中提取 **341 份相关配方、补丁及许可材料**。

- 归档大小：`7618167` bytes。
- SHA-256：`d36592f747fea017af60647e17e764eba254fefdf503b195e05abbbdcad9742d`。
- 配方包含上游 source URL/hash 表达式、构建选项和补丁；未执行任何源码脚本。
- 版本解析只处理已知字面量、版本变量、数组首项、epoch 和 revision；不执行 shell。
- 所有 75 个 bootstrap 包版本已匹配。overlay 的 proot `5.1.107.89`、
  libandroid-shmem `0.7`、libtalloc `2.4.3` 与该旧提交中的版本不同；下方已另按各自
  精确版本定位配方提交并保存上游源码，不再列为来源缺项。

额外纳入两个 Codex binary payload：rootfs 内的 `0.147.0` 与新 overlay 的 `0.153.4`，
分别保留实际 package metadata。旧版本仍存在于交付资产中，不能从组件分母删除。

另已现场核对原版提示文件 `Android/appliance/runtime/codex-base-instructions.md`：

- 来源：[Codex rust-v0.153.4 models-manager/prompt.md](https://raw.githubusercontent.com/openai/codex/rust-v0.153.4/codex-rs/models-manager/prompt.md)。
- 大小：`20903` bytes。
- SHA-256：`ac8ae107a0d72fe3476b430afb161ea4e67da2e446d778aefc44828160559807`。
- 作为 Codex 源码组件纳入 Apache-2.0 与适用 notices 材料，不遗漏这个独立打包文本。

## 可复核文件

所有大量材料保存在 Git 忽略的 `artifacts/private/sources/`，本文件不把它们写入源码历史。

| 文件 | 内容 / SHA-256 |
|---|---|
| `component-inventory-20260905-r3/inventory.json` | r10 当前固定归档的 223 包、证据索引和 3 个 extra components；`e634d718131609fb30fe0cce64e33164c6b2994968223568be8be0bb0aef2433` |
| `component-inventory-20260905-r3/packages.tsv` | 便于人工审阅的包表；`71959493c7e89fcd4cc35b37810ddef9ee8e4e5bacf8224c5dd45ba6ccb87cc5` |
| `component-inventory-20260905-r3/evidence/` | 实际版权文本、status 与 metadata，逐文件 hash 在 inventory 内 |
| `termux-recipes-20260905-r3/recipe-inventory.json` | 最终配方对应关系和 341 项证据；`566d5cadd23da23adb473ca15afba12de42ab686563157fd0aca46a4e6b79a0f` |
| `termux-recipes-20260905-r3/recipe-evidence/` | 固定提交中的配方、补丁与许可文件 |
| `termux-bootstrap-sources-20260905-r1/manifest.json` | 75 个二进制包、71 个母包、123 项源码输入和 464 个保留文件；`b2ea3d98621cd4e98ac41d9a264f6591b459db97b9266e65deb0935c108ca2d3` |
| `component-inventory-20260905-supplement/source-manifest.json` | 各结果关联、固定源码归档和新增 Codex prompt 来源 |

原 r1/r2 组件库存保留但已被 r10 的 r3 库存取代：新库存把 Codex payload 正确记录为
0.153.4，并把基础指令记录为 20903 bytes 对应的 `ac8ae...` hash。配方解析仍最终引用 r3，
修正了版本数组、字面变量、epoch 和零 revision。

复现工具：`tools/standalone/component_inventory.py`、
`tools/standalone/component_source_enrichment.py`；使用新的 output 目录，拒绝覆盖已有快照。
解压使用 Mac 上已有 Node 24 的内置 zstd，未安装软件、未启动归档中的可执行程序。
六项针对性验证已通过，覆盖链接合并、目录别名、链接循环、路径越界和安全版本解析。

## 仍需完成

1. Debian 的 101 组对应 source package/version 已从官方 snapshot 保存：350 个文件、
   590938522 bytes。每个文件均复核 snapshot SHA-1，并按 `.dsc` 的 SHA-256/size 交叉验证；
   `.dsc` 签名已保留但尚未做密码学验证，因此仍不能声称签名真实性已验。
2. Termux bootstrap 的 123 项上游/生成源码输入已经保存；仍需完成可重现构建验证、
   逐组件 notice/安装信息评估。`command-not-found` 的历史构建输入原为未固定 master，
   `foot 1.21.0` 的历史压缩字节未取回，边界详见下方补充。overlay 三包的精确配方、
   补丁和源码也已完成保存。
3. Codex 两版本 binary 的完整依赖 notices 与适用源码材料；下方已补入固定上游源码及
   自带 LICENSE/NOTICE，但尚未证明所有二进制依赖的义务均已覆盖。
4. Android/JNI 最终 APK 覆盖、项目自有代码许可、完整 notices 与最终对应源码
   交付方式仍待完成。下方新增 Gradle 实际解析库存，不声称已做完合规评估。

## 后续补充：Codex 固定源码及声明

两个版本均从官方 GitHub 标签解析到具体 commit，再按 commit 下载完整源码归档。
保留标签响应、完整归档、逐文件哈希与 LICENSE/NOTICE/依赖锁文件，不执行归档代码。

| 版本 | 固定 commit | 源码归档 SHA256 | 提取证据文件 |
|---|---|---|---:|
| 0.147.0 | `be6e8eac029b183056b7e4402879f15d2c85f61b` | `f0513d22bd932c53dd4eb267cc30d24b75c6dd6a72865178f092fb8913b9d6f1` | 157 |
| 0.153.4 | `3d2ee51ca2d5db578f328aa75e20aa22c0197c9a` | `bbbf66ffa30846f1e9bc3ae8a87a5aa0bb768efee8dbb94759dc9eb64bb4aa3a` | 172 |

输出位于 `artifacts/private/sources/codex-<version>-source-20260905-r1/`，
每份 `manifest.json` 记录实际来源 URL 和校验值。全部 329 份提取证据哈希复核通过。
0.153.4 源码归档中的 `models-manager/prompt.md` 与当前打包基础指令逐字节一致。
两个版本自带的 NOTICE 均包含 Ratatui 代码归属，原文已保存；不以单个 Apache 声明
替代它。源码快照不证明可重现二进制或已齐备全部 Cargo/npm 依赖源码。

复现入口：`tools/standalone/codex_source_inventory.py`，使用新的输出目录。当前打包的
0.153.4 材料已用 `--verify` 离线复核标签到 commit 的引用链、完整源码归档、172 份
提取证据与目录闭包，结果 PASS；针对性测试 4/4 PASS。

## 后续补充：Android 实际解析依赖

通过同一 Gradle 工程的 `:appliance:debugRuntimeClasspath` 取得：

- 148 个解析图节点（含本地项目与平台节点）；110 个外部 AAR/JAR 等构建输入。
- 141 份已成功解析的 Maven POM；136 份包含直接许可证声明。
- 对 110 个输入逐项复核 SHA256 后，从 AAR/JAR 及嵌套 JAR 保存 13 条声明记录，
  内容去重后为 5 份文件。此数量只代表实际归档中找到的声明，不能推断其余无需声明。

材料：`artifacts/private/sources/android-dependencies-20260905-r2/manifest.json`、
同目录 `poms/`，以及 `android-notices-20260905-r1/manifest.json` 与 `evidence/`。
POM 只记录直接声明，未解析父 POM 继承；构建输入清单也不等于最终 APK 字节级清单。
JNI、裁剪/合并后的文件覆盖、缺失声明、对应源码仍须继续核对。

首轮旧 resolved-artifacts 入口遇到本地 Android 模块多种产物的选择歧义，未生成完成清单。
改用外部组件 artifact view 后成功，本地项目仍保留在解析图中；r1 失败产物保留，
最终引用 r2。日志 `/private/tmp/chebyagent-aln-gradle-inventory-r2-20260905.log`。
没有修改应用构建配置或重新生成 APK。

复现入口：`tools/standalone/gradle_dependency_inventory.init.gradle` 与
`tools/standalone/android_notice_inventory.py`。所有新输出拒绝覆盖已有快照。

## 后续补充：Debian 精确源码归档

以实际待打包 rootfs 的 r3 库存为输入，145 个 Debian binary packages 去重得到
101 个精确 source package/version。官方 snapshot 归档已保存在
`artifacts/private/sources/debian-sources-20260905-r4/`：

- 101 个 source packages，350 个文件，共 590938522 bytes；
- `manifest.json` SHA256：`1a231f1eed609ef48359455292a71e11d9a003ae3298758194c497311d97eda1`；
- 离线 `--verify` 现场返回 PASS，并复核全部本地文件和 `.dsc` 关联；
- 101 份 `.dsc` 均为 `PRESENT_NOT_VERIFIED`，表示保存了签名而非已验证签名。

复现入口：`tools/standalone/debian_source_inventory.py`。测试夹具曾遗漏 Debian `.dsc`
续行字段要求的前导空格；修正夹具后，Debian 来源针对性测试 12/12 PASS。新版清单
按排序后的 Debian source name/version 投影绑定，不再因无关的 Codex extra component
变化误报；任何 Debian 来源身份变化仍会 fail closed。r4 是 schema 1 旧快照，离线复核
以其有序的 101 个来源身份作为兼容绑定，并已完成全部实料复核。

## 后续补充：Termux overlay 三包精确来源

通过 Termux 官方提交历史逐包定位实际版本，并保存每个提交的完整配方仓库归档、
对应 package 目录及配方声明的上游 source archive：

| 包 | 版本 | 精确配方提交 | 上游源码 SHA256 |
|---|---|---|---|
| proot | 5.1.107.89 | `48ba8fb652272580ba3f9663973fc2792e2a3566` | `e1240f63de03e6da536d74041c7937ddd8737ab27743857d79285724b948eca8` |
| libandroid-shmem | 0.7 | `b25e257208da6d2e8b558b8a2b51762158a2e806` | `1e5ff8459bc0a8c229dd8a94b27d119987e09ef3414331c2b5ebfff20b98e867` |
| libtalloc | 2.4.3 | `fbc049451e7fc59cdf510732aad49bd45590b0bb` | `dc46c40b9f46bb34dd97fe41f548b0e8b247b77a918576733c528e83abd854dd` |

材料位于 `artifacts/private/sources/termux-overlay-sources-20260905-r1/`，共保存
28001206 bytes；`manifest.json` SHA256 为
`995c11ddd1ea38cfb1c7bf2c2cdd34d939ca9d1b0b547940c7d7fd3ec1667a95`。
离线复核 3 个上游归档、3 个配方仓库归档及 4 个配方/补丁文件全部通过。
复现入口 `tools/standalone/termux_overlay_source_inventory.py`，针对性测试 3/3 PASS。
这证明来源字节和配方坐标一致，不等于可重现构建或全部再分发义务已经完成。

## 后续补充：Termux bootstrap 上游源码输入

以 r3 的实际 75 个 bootstrap 二进制包为输入，去重到 71 个母包并解析固定配方，
保存 123 项上游归档、GNU 补丁、git commit 归档或配方内生成源码。完整输出位于
`artifacts/private/sources/termux-bootstrap-sources-20260905-r1/`：

- 464 个保留文件，共 `840875088` bytes；
- `manifest.json` SHA256：`b2ea3d98621cd4e98ac41d9a264f6591b459db97b9266e65deb0935c108ca2d3`；
- 离线 `--verify` 现场返回 PASS，覆盖文件集合、逐文件 hash、配方声明 hash、git commit
  固定以及特殊来源等价证明；
- 4 个没有外部归档的包按 `RECIPE_CONTAINED_SOURCE` 保留完整配方目录；
- 收集器只解析白名单内的标量表达式，从不 source 或执行配方 shell，针对性测试 6/6 PASS。

两个不能抹平的历史边界已写入 manifest：`command-not-found` 配方使用未固定 master 的
`repo.json`，现保存同一配方提交中的快照但不声称等于历史构建时字节；`foot 1.21.0`
原 Codeberg 压缩包当前受限且历史声明 hash 对应字节未取回，现保留 hash 固定的 Gentoo
distfile，并逐文件证明其源码树与独立镜像的 `1.21.0` 标签固定提交
`68f5eab0b0fa08becebbed412947ba19246c2518` 一致。后者证明源码树等价，不证明两个
压缩包字节相同。

复现入口：`tools/standalone/termux_bootstrap_source_inventory.py`；单元测试入口：
`tools/standalone/test_termux_bootstrap_source_inventory.py`。材料齐备不等于二进制可重现，
也不等于逐项许可证、NOTICE 与 GPL 安装信息义务已经完成。

## 后续补充：交付许可阻塞账本

新增 `tools/standalone/delivery_license_ledger.py`，把 r3 运行库存、实际 Gradle 依赖和归档内
notice 证据合并成 fail-closed 交付账本。它不替代法律判断，也不会因为找到一份许可证文本
就自动宣布合规；每个尚未完成人工义务判断和最终交付绑定的组件都明确保留为 BLOCKED。

当前账本覆盖 367 行：223 个运行包、3 个额外运行组件、141 个 Maven 声明。223 个运行包
中 5 个缺包自身直接许可证路径；141 个 Maven 声明中 5 个 POM 未声明许可证，11 个坐标
在解析归档内找到 notice 证据。全部 367 行当前仍为 BLOCKED；项目自有代码最终许可决定、
逐组件 notice/source offer/安装信息/重链接判断、JNI 与最终 APK 字节映射、源码与最终标签
绑定均列为跨组件阻塞项。账本位于
`artifacts/private/evidence/aln-r10-build-20260905/delivery-license-ledger-r1.json`，并复制进验收
账本 evidence 后，将 `delivery-license-notices` 从泛化的 “Not run” 更新为有证据的 BLOCKED。
这提高了 GAP 的可审计性，不把许可交付门槛标为 PASS。
