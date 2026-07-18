# 发布管理与 submodule 版本固定

## 1. 本讲目标

本讲是「版本管理与仓库维护」单元的第一讲。学完后你应当能够：

- 看懂 `Changelog.md` 的发布分组结构，能从任意一次 release（如 `2020.2`）中读出它固定了哪些子模块、各自被钉在哪个版本。
- 理解集合仓库「整体快照 + 个别更新」的版本管理思路：为什么 `psi_fpga_all` 不保证各子模块始终处于最新状态。
- 掌握对比两次 release 之间子模块版本差异的方法，并能区分 `Changelog.md`（人类可读的变更记录）与 `.gitmodules` + gitlink（机器认定的真实指针）两套事实来源的关系。

本讲只读两个文件：`Changelog.md` 与 `.gitmodules`，不涉及任何脚本运行。

## 2. 前置知识

本讲承接 u1-l2（git submodule 机制）与 u1-l3（目录结构与四大类库）。在继续之前，请确认你理解下面三个概念：

- **collection-repo（集合仓库）**：`psi_fpga_all` 本身几乎不含代码，它把 23 个独立的 FPGA 库用 git submodule 挂到固定目录下。这一点在 u1-l1 已建立。
- **gitlink（子模块指针）**：父仓库并不保存子模块的文件内容，只在 git 树对象里保存一条「特殊目录条目」（模式 `160000`），指向子模块的某个具体 commit。`.gitmodules` 只负责登记 `path → url` 的映射，真正「钉在哪个 commit」的是 gitlink。详见 u1-l2。
- **tag 与语义化版本（SemVer）**：子模块仓库用 `git tag` 给重要 commit 起名字，比如 `2.13.0`。SemVer 的格式是 `主版本.次版本.修订号`（`MAJOR.MINOR.PATCH`）：不兼容改动升 MAJOR、向后兼容的新功能升 MINOR、只修 bug 升 PATCH。SemVer 的大小比较按字典顺序逐段比：`2.13.0 > 2.7.1`，因为次版本 `13 > 7`。

> 通俗类比：把 `psi_fpga_all` 想成一张「全家福照片」。每次 release 就是给全家人拍一张合影，照片里每个人的姿势（= 子模块版本）是固定的。`Changelog.md` 是相册背面的说明文字，记录「这次拍照谁换了新姿势」。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用它做什么 |
| --- | --- | --- |
| `Changelog.md` | 人类可读的发布记录，按 release 版本（`2018.1` ~ `2021.1`）分组列出每次固定/升级了哪些子模块及其版本 | 读出每个 release 的分组结构、子模块版本号、发布节奏 |
| `.gitmodules` | git 与机器共同认定的子模块权威清单，每条含 `path` 与相对 `url` | 对比 Changelog，理解「变更记录」与「真实指针」两套来源的关系 |
| `README.md`（仅一段） | 给出发布策略的文字说明 | 引用「约每 3 个月更新、中间可能不是最新、可单独更新」的策略原文 |

> 提醒：各子模块内部的源码（如 `PsiSim.tcl`、`config.tcl`）住在 submodule 里，本仓库当前未直接检出，**本讲不涉及**。本讲只看集合仓库自身的两个清单文件。

## 4. 核心概念与源码讲解

### 4.1 Changelog 的发布分组结构

#### 4.1.1 概念说明

`Changelog.md` 是集合仓库给人类读的「发布账本」。它的核心设计有两点：

1. **按 release 分段**：每段以一个二级标题开头，标题里同时给出**版本号**和**发布日期**，例如 `2020.2 (20.10.2020)` 表示 2020 年第 2 次发布、拍板于 2020 年 10 月 20 日。
2. **按四大类分组**：每个 release 内部按 `TCL` / `VHDL` / `Python` / `VivadoIP`（有时写成 `VivadoIp`，大小写不统一）四类组织，与 u1-l3 讲的四大目录完全对应。每一类下面再列出该类涉及的子模块。

这种结构与 u1-l3 的结论互相印证：四大类不是随意划分，而是仓库的「官方分类法」，在 README、`.gitmodules`、`Changelog.md` 三处保持一致。

#### 4.1.2 核心流程

阅读 `Changelog.md` 时，按下述流程定位信息：

```text
1. 从上往下扫二级标题（##），越靠上越新（2021.1 在最顶部）。
2. 选定一个 release 后，在其段落内找四类标题（TCL/VHDL/Python/VivadoIP）。
3. 每一类下，每一行就是一个子模块：[名字](链接) 版本号。
4. 注意：不是每个 release 都列出全部 23 个子模块 ——
   有的 release 是「完整快照」（列出该类所有库），
   有的 release 是「增量记录」（只列出本次发生变化的库）。
```

第 4 点是本讲最容易踩坑的地方，下一节会用真实代码展示两种模式的区别。

#### 4.1.3 源码精读

先看「完整快照」模式的代表 —— `2020.2`，它列出了几乎所有子模块：

> [Changelog.md#L11-L39](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md#L11-L39) —— `2020.2 (20.10.2020)` 这一段，下含 TCL（2 个）、VHDL（5 个）、Python（5 个）、VivadoIP（11 个），是典型的完整快照。

其中开头几行如下：

```text
## 2020.2 (20.10.2020)
* TCL
  * [PsiSim](...) 2.5.0
  * [PsiIpPackage] (...) 2.3.0
* VHDL
  * [en\_cl\_fix](...) 1.1.5
  * [psi\_common](...) 2.13.0
  ...
```

再看「增量记录」模式的代表 —— 最顶部的 `2021.1`，它只列出了少数几个库：

> [Changelog.md#L1-L9](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md#L1-L9) —— `2021.1 (23.08.2021)` 只列出 `PsiIpPackage`、`en_cl_fix`、`psi_common`、`psi_fix`、`vivadoIP_mem_test` 共 5 个库，其余 18 个子模块没有出现。

这说明 `2021.1` 只记录了**本次发布升级过的库**，而不是仓库当时的完整状态。同理 `2020.1`（[Changelog.md#L40-L52](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md#L40-L52)）也只列了 6 个库。

由此得到一条**关键判断规则**：

> 看到某个 release 没列出某个子模块，**不能**断定「该子模块在这个版本里不存在」。它很可能是「本次没变化，所以没记」。要看该子模块在那个时间点的真实版本，应向下回溯到最近一次列出了它的 release。

#### 4.1.4 代码实践

**实践目标**：亲手数一数每个 release 列出了多少个子模块，从而分辨「完整快照」与「增量记录」。

**操作步骤**：

1. 打开 `Changelog.md`。
2. 对 8 个 release（`2021.1`、`2020.2`、`2020.1`、`2019.4`、`2019.3`、`2019.2`、`2019.1`、`2018.1`）逐个统计：每一类下有几行（即几个库）。
3. 把每个 release 的「四类库数量之和」填进下表。

**需要观察的现象**：`2020.2` 与 `2019.4` 的总数接近 23（仓库子模块总数），而 `2021.1`、`2020.1` 明显更少。

**预期结果**（参考答案，按本讲 HEAD 的 `Changelog.md` 实数）：

| release | TCL | VHDL | Python | VivadoIP | 合计 | 模式判断 |
| --- | --- | --- | --- | --- | --- | --- |
| 2021.1 | 1 | 3 | 0 | 1 | 5 | 增量 |
| 2020.2 | 2 | 5 | 5 | 11 | 23 | 完整快照 |
| 2020.1 | 1 | 3 | 1 | 3 | 8 | 增量 |
| 2019.4 | 2 | 5 | 5 | 8 | 20 | 完整快照 |

> 待本地验证：上表中「合计」是你手动数出来的；若你数到的数字与本表不同，以你实际看到的为准，并思考差异原因（例如某个库在某 release 是否真的被列出）。

#### 4.1.5 小练习与答案

**练习 1**：`2021.1` 这一段里完全没有 `Python` 这一类的标题。能否据此说「2021.1 发布时仓库里没有 Python 库」？

**答案**：不能。`2021.1` 是增量记录，只列出本次升级的库；Python 类本次没有库升级，所以标题被省略。仓库里 Python 库依然存在（见 `.gitmodules` 的 5 条 Python 子模块），只是继承了上一次（`2020.2`）的版本。

**练习 2**：想要知道 `psi_tb` 在 `2021.1` 发布时的版本，应该怎么做？

**答案**：`2021.1` 没列 `psi_tb`，所以从 `2021.1` 往下回溯，找最近一次列出 `psi_tb` 的 release。回溯到 `2020.2`（[Changelog.md#L19](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md#L19)）看到 `psi_tb 2.6.0`，即 `2021.1` 时 `psi_tb` 仍为 `2.6.0`（除非有人在两次发布之间单独更新了它）。

---

### 4.2 子模块版本号 / tag 的记录方式

#### 4.2.1 概念说明

在 `Changelog.md` 里，每个子模块名后面跟的那串字符，就是该子模块仓库的一个 **git tag**（标签）。绝大多数库采用语义化版本（SemVer），少数老库沿用旧式 tag。理解版本号有两个用处：

- **比较新旧**：判断两次 release 之间某个库是否升级、升了多少。
- **回溯真相**：当 `.gitmodules` 记录的子模块目录名与 Changelog 不一致时（命名演变），用版本号和时间线理清来龙去脉。

#### 4.2.2 核心流程

SemVer 三段式 `MAJOR.MINOR.PATCH` 的比较规则：

\[ \text{version}(M.m.p) < \text{version}(M',m',p') \iff (M,m,p) \text{ 在字典序下小于 } (M',m',p') \]

也就是**先比 MAJOR，相同则比 MINOR，再相同则比 PATCH**。例如 `2.7.1 < 2.13.0`，因为 MAJOR 相同（2==2），而 MINOR `7 < 13`。

> 注意陷阱：作为字符串比较时 `"2.7.1" > "2.13.0"`（因为字符 `'7' > '1'`），这是错的。必须按点分段转成整数再比。

对于非 SemVer 的旧式 tag（如 `V1.00_20180125`），没有统一比较规则，只能按发布日期或上下文判断新旧。

#### 4.2.3 源码精读

**代表 1：`psi_common` 的版本演进**。在 8 个 release 中追它的版本号，能看到一条清晰的升级曲线：

| release | `psi_common` 版本 | 出处 |
| --- | --- | --- |
| 2018.1 | 2.0.0 | [Changelog.md#L142](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md#L142) |
| 2019.1 | 2.1.0 | [Changelog.md#L127](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md#L127) |
| 2019.2 | 2.2.0 | [Changelog.md#L112](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md#L112) |
| 2019.3 | 2.5.1 | [Changelog.md#L88](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md#L88) |
| 2019.4 | 2.7.1 | [Changelog.md#L61](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md#L61) |
| 2020.1 | 2.12.0 | [Changelog.md#L44](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md#L44) |
| 2020.2 | 2.13.0 | [Changelog.md#L17](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md#L17) |
| 2021.1 | 2.17.0 | [Changelog.md#L6](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md#L6) |

MAJOR 一直是 2（没有破坏性改动），MINOR 从 0 一路涨到 17，说明 `psi_common` 在这期间持续增加新功能，是仓库里演进最活跃的库之一。

**代表 2：旧式 tag `vivadoIP_sync_det_edge`**。这个库没有用 SemVer：

> [Changelog.md#L38](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md#L38) —— `vivadoIP_sync_det_edge V1.00_20180125`，tag 形如「V 版本号 _ 日期」，是早期命名风格。

> [Changelog.md#L52](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md#L52) —— `2020.1` 里它仍是 `V1.00_20180125`，说明这个库长期未升级，tag 一直没变。

**代表 3：`en_cl_fix` 是 fork**。注意它行尾带说明，并且下一行有「Original Location」指向 Enclustra 原仓库：

> [Changelog.md#L59-L60](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md#L59-L60) —— `en_cl_fix 1.1.3 - fork of a library provided by Enclustra GmbH`，并给出原始仓库链接。这意味着它的版本号是 PSI 自己 fork 后打的，与上游 Enclustra 版本不一定对应。

#### 4.2.4 代码实践

**实践目标**：用 SemVer 比较规则，从 `Changelog.md` 里重建一个库的版本演进时间线。

**操作步骤**：

1. 选定 `psi_fix`（VHDL 类）。
2. 在 8 个 release 中逐个找出它的版本号（提示：`2018.1` 为 `2.0.0`，见 [Changelog.md#L144](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md#L144)）。
3. 把版本号按时间从早到晚排成一行，用箭头连接。

**需要观察的现象**：每次相邻 release 之间，MINOR 还是 PATCH 在涨？有没有跳过若干次（说明该 release 没列它，需回溯）？

**预期结果**（参考答案）：

```text
2.0.0 (2018.1) → 2.1.0 (2019.1) → 2.2.0 (2019.2) → 2.3.2 (2019.3) → 2.3.3 (2019.4) → 2.4.1 (2020.1) → 2.4.1 (2020.2) → 3.1.0 (2021.1)
```

注意 `2020.1` 与 `2020.2` 都是 `2.4.1`（`2020.2` 没升级它），而到 `2021.1` MAJOR 跳到 3 —— 出现了一次破坏性改动。这是一条典型的「长期小步升级 + 偶发大版本」的演进曲线。

#### 4.2.5 小练习与答案

**练习 1**：作为字符串，`"2.7.1"` 和 `"2.13.0"` 哪个更大？作为 SemVer 呢？

**答案**：作为字符串 `"2.7.1" > "2.13.0"`（因为 `'7' > '1'`）；作为 SemVer `2.7.1 < 2.13.0`（MINOR 段 7 < 13）。比较版本号必须按点分段转整数。

**练习 2**：`vivadoIP_sync_det_edge` 的 tag `V1.00_20180125` 里 `20180125` 是什么含义？为什么这种 tag 难以用 SemVer 规则比较？

**答案**：`20180125` 是日期（2018-01-25），是该版本创建/发布的日期。它没有 `MAJOR.MINOR.PATCH` 三段结构，所以无法用 SemVer 的分段比较规则判断新旧，只能靠日期或它在多次 release 中是否变化来推断。

---

### 4.3 发布节奏与单独更新子模块的策略

#### 4.3.1 概念说明

理解版本号的最终目的，是回答两个工程问题：

1. **仓库多久更新一次？** —— 决定你何时该来拉取新版本。
2. **我能自己更新某个子模块吗？** —— 决定你被旧版本卡住时有没有出路。

`psi_fpga_all` 的策略在 README 里只用一句话说明，却奠定了整个集合仓库的版本哲学：**整体快照 + 个别更新**。即：每隔一段时间发布一个「经过测试、互相兼容」的整体快照；两次发布之间，各子模块可能已经有了更新版本，但本仓库不一定跟进；如果用户急需某个库的新功能，可以**单独**更新那一个子模块。

#### 4.3.2 核心流程

```text
集合仓库发布策略
├─ 整体快照（约每 3 个月一次）
│   ├─ 维护者把 23 个子模块指针统一移到「一组已知兼容的 commit」
│   ├─ 在 Changelog.md 顶部追加一段 release 记录
│   └─ 此时各子模块处于「协调一致」的状态
├─ 两次发布之间
│   ├─ 各子模块上游可能已有更新版本
│   └─ 但本仓库不跟进 —— 仍是上一次快照的旧版本
└─ 个别更新（用户自助）
    └─ 用户可单独 git -C <submodule> checkout <新 tag>
       再提交父仓库的 gitlink 变更，只升级自己需要的那一个
```

**为什么集合仓库不保证各子模块始终最新？** 一句话：**版本固定是为了保证兼容性**。23 个库之间用相对路径互相引用（u1-l1 的「目录结构即接口」），盲目把某个库升到最新版，可能引入与其它库不兼容的改动，破坏整体快照「经过测试、互相兼容」的保证。所以维护者宁愿让中间状态「旧但稳」，把「新」留给下一次完整测试后的整体快照。

#### 4.3.3 源码精读

**发布节奏的原文**在 README 里只有一句：

> [README.md#L16](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/README.md#L16) —— `The repository will be updated regularly (roughly every 3 months) but it may not always contain the vey-newest state of all submodules in between the updates. You can update submodules individually if required.`（原文「vey-newest」为拼写笔误）。

把这段话拆成三条工程结论：

1. **节奏**：roughly every 3 months（约每 3 个月）。
2. **不保证最新**：两次更新之间，可能不是各子模块的最新状态。
3. **可单独更新**：如有需要，你可单独更新某个子模块。

**用 release 日期核对「约每 3 个月」**。`Changelog.md` 各 release 标题里的日期可用来验证节奏：

| release | 日期 | 距上次间隔 |
| --- | --- | --- |
| 2018.1 | 16.10.2018 | — |
| 2019.1 | 07.01.2019 | 约 3 个月 |
| 2019.2 | 13.05.2019 | 约 4 个月 |
| 2019.3 | 02.08.2019 | 约 3 个月 |
| 2019.4 | 02.12.2019 | 约 4 个月 |
| 2020.1 | 12.05.2020 | 约 5 个月 |
| 2020.2 | 20.10.2020 | 约 5 个月 |
| 2021.1 | 23.08.2021 | 约 10 个月 |

结论：早期（2019 年）确实接近「每 3 个月」；后期节奏变慢，`2021.1` 与 `2020.2` 之间隔了约 10 个月。所以 README 用「roughly（大约）」是准确的 —— 3 个月是目标，不是承诺。

**两套事实来源的关系**。最后要分清 `Changelog.md` 与 `.gitmodules` 各自记录什么：

- `Changelog.md`：**人类可读的变更记录**，按 release 分段，记录「这次升级了谁、升到哪个版本」。它是历史账本，且（如 4.1 所述）可能是增量的。
- `.gitmodules`：只登记 `path` 与相对 `url` 的映射，**不含版本号**。例如：

  > [.gitmodules#L4-L6](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/.gitmodules#L4-L6) —— `VHDL/psi_common` 的 path 与 url，没有任何版本信息。

  真正「当前钉在哪个 commit」是父仓库 git 树里的 gitlink（u1-l2 已讲），不在 `.gitmodules` 里。

所以，「当前各子模块的真实版本」要以父仓库的 gitlink 为准（可用 `git submodule status` 查看），`Changelog.md` 只能告诉你「最后一次整体快照时计划钉在哪个版本」。两者偶尔会不一致 —— 比如有人单独更新了某子模块却没同步改 Changelog。

#### 4.3.4 代码实践

**实践目标**：验证「单独更新子模块」在版本记录上的体现，并理解 Changelog 与 gitlink 的关系。

**操作步骤**：

1. 假设你想把 `psi_common` 单独升级到比 `2021.1`（`2.17.0`）更新的版本。写出「单独更新」的操作思路（**示例代码**，未实际运行）：
   ```bash
   # 示例代码：单独更新 psi_common 这一个子模块
   cd VHDL/psi_common
   git fetch --tags
   git checkout 2.20.0        # 假设上游已有该 tag
   cd ../..
   git add VHDL/psi_common    # 把更新后的 gitlink 暂存
   git commit -m "bump psi_common to 2.20.0 individually"
   ```
2. 思考：执行上述步骤后，`Changelog.md` 会不会自动出现一段新 release？

**需要观察的现象**：gitlink 变了，但 `Changelog.md` 文本**没有**变化。

**预期结果**：`Changelog.md` 不会自动更新 —— 它是人工维护的文档。「单独更新」只改 gitlink（机器真相），不改 Changelog（人类账本）。这也正是 4.3.3 强调「两套事实来源可能不一致」的原因：单独更新后，若想保持账本与真相一致，需手动在 `Changelog.md` 里补一行说明。

> 待本地验证：上述 `git checkout 2.20.0` 是否成功，取决于 `psi_common` 上游是否真有该 tag；本仓库当前未检出子模块，无法在此验证。

#### 4.3.5 小练习与答案

**练习 1**：README 说「约每 3 个月更新」，但 `2020.2` 到 `2021.1` 实际隔了约 10 个月。这是否说明 README 在骗人？

**答案**：不算骗人。README 用的是「roughly（大约）」，3 个月是维护者追求的目标节奏，实际受测试工作量、节假日等影响会浮动。阅读工程文档时要注意「roughly / approximately / 约」这类限定词，它们表示软目标而非硬承诺。

**练习 2**：同事告诉你「我用 `git submodule status` 看到 `psi_common` 现在是 `2.19.0`，但 `Changelog.md` 顶部 `2021.1` 写的是 `2.17.0`」。请给出最可能的解释。

**答案**：最可能是有人对 `psi_common` 做了**单独更新**，把它的 gitlink 移到了 `2.19.0`，但没有（或还没）在 `Changelog.md` 里补记。这正是「机器真相（gitlink）」与「人类账本（Changelog）」不一致的典型场景。要确认，可查父仓库 `git log -- VHDL/psi_common` 看是否有单独 bump 的提交。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个**版本差异对比**任务（本讲的核心实践）。

**任务**：从 `Changelog.md` 中选取 `2020.2` 与 `2019.4` 两次 release，分别提取 `psi_common`、`psi_fix`、`PsiSim` 三个子模块的版本号，做成对比表，指出哪些子模块在两次发布之间发生了版本升级；再用一句话说明集合仓库为什么不保证各子模块始终处于最新状态。

**操作步骤**：

1. 在 `2020.2` 段（[Changelog.md#L11-L39](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md#L11-L39)）找出三个库的版本：
   - `psi_common` → `2.13.0`（[L17](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md#L17)）
   - `psi_fix` → `2.4.1`（[L20](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md#L20)）
   - `PsiSim` → `2.5.0`（[L13](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md#L13)）
2. 在 `2019.4` 段（[Changelog.md#L54-L79](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md#L54-L79)）找出三个库的版本：
   - `psi_common` → `2.7.1`（[L61](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md#L61)）
   - `psi_fix` → `2.3.3`（[L63](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md#L63)）
   - `PsiSim` → `2.4.0`（[L56](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md#L56)）
3. 填入对比表，用 SemVer 规则判断是否升级。

**预期结果（参考答案）**：

| 子模块 | 2019.4 | 2020.2 | 是否升级 | 判断依据 |
| --- | --- | --- | --- | --- |
| `psi_common` | 2.7.1 | 2.13.0 | 是 | MINOR 7→13 |
| `psi_fix` | 2.3.3 | 2.4.1 | 是 | MINOR 3→4 |
| `PsiSim` | 2.4.0 | 2.5.0 | 是 | MINOR 4→5 |

三个子模块在 `2019.4` → `2020.2` 之间**全部发生了升级**。

**最后一句话**（集合仓库为何不保证各子模块始终最新）：

> 因为 `psi_fpga_all` 用版本固定来保证 23 个互相用相对路径引用的库「整体兼容、经过测试」，盲目跟进某个库的最新版可能破坏这种兼容性；所以它选择每隔约 3 个月发布一个协调一致的整体快照，两次发布之间维持「旧但稳」的状态，需要新功能的用户可单独更新某个子模块、自行承担兼容风险。

## 6. 本讲小结

- `Changelog.md` 按 release（`## YYYY.X (DD.MM.YYYY)`）分段，段内按 `TCL` / `VHDL` / `Python` / `VivadoIP` 四类分组，每行一个子模块名 + 版本号（tag）。
- release 有两种记录模式：**完整快照**（如 `2020.2`、`2019.4`，列出几乎全部库）与**增量记录**（如 `2021.1`、`2020.1`，只列出本次升级的库）；看到某 release 没列某库，不能断定它不存在，要向下回溯。
- 版本号多为 SemVer（`MAJOR.MINOR.PATCH`），按点分段转整数比较；少数老库用旧式 tag（如 `V1.00_20180125`），`en_cl_fix` 是带「Original Location」的 fork。
- 发布策略是「整体快照 + 个别更新」：约每 3 个月一次整体快照（实际 3~10 个月浮动），两次发布间不保证各子模块最新，用户可单独更新某个子模块。
- 集合仓库不保证最新的根本原因是**用版本固定换取兼容性**：23 个库互相相对路径引用，整体快照保证「已知兼容」，单独更新则由用户自担风险。
- 存在两套事实来源：`Changelog.md` 是人工维护的人类账本（可能增量、可能滞后），父仓库 gitlink 才是机器认定的真实指针；单独更新子模块只改 gitlink，不自动改 Changelog。

## 7. 下一步学习建议

- **下一篇 u3-l2（维护与扩展集合仓库）**：当你想往仓库里**新增**一个 submodule 时，需要遵循 `.gitmodules` 的相对 URL 约定，并理解提交 `774a090` 带来的 SSH/HTTPS 双兼容。本讲的「版本固定」概念会延续过去 —— 新增子模块本质就是新增一条 gitlink + 一条 `.gitmodules` 记录。
- **u3-l3（端到端工作流）**：把克隆、仿真、IP 打包串成一条流水线，届时你会真正用到「当前 gitlink 钉在哪个版本」来判断环境是否一致。
- **延伸阅读（项目外）**：若对 git submodule 的指针机制仍不熟，建议阅读 Git 官方手册中关于「gitlink (mode 160000)」与 `git submodule` 的章节，重点理解「`.gitmodules` 只存映射、真正的 commit 存在父仓库树对象里」这一关键事实。
