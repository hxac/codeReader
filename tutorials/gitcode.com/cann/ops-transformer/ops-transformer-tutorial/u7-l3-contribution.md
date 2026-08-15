# u7-l3 贡献流程与代码规范

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出一次合格算子贡献的**完整六步流程**：创建 Issue → 需求评审 → PR 提交 → CI 门禁 → Committer 检视 → Maintainer 合入。
2. 对照 `CONTRIBUTING.md` 区分**生态最简算子**与**项目标准算子**两套交付件清单，知道自己该交哪些文件、放在哪个目录。
3. 读懂 `.pre-commit-config.yaml`，理解 `git commit` 时本地门禁到底跑了哪五类检查、各自卡什么问题。
4. 读懂 `OAT.xml` 与 `scripts/oat_check.sh`，理解开源合规检查（许可证头、版权声明、禁二进制/归档）的判定规则与执行机制。
5. 以 u6-l1 开发的 `my_sum` 算子为样本，**模拟发起一次完整贡献**：整理目录、补齐 README 与许可证头、写 PR 描述、跑通本地检查。

本讲是「会用 → 会读 → 会写」路线的最后一环：**会贡献**。

## 2. 前置知识

本讲不再讲算子怎么写（u6-l1 已完成 `my_sum`），只讲「写完之后怎么交给社区」。需要先理解几个协作术语：

- **CLA（Contributor License Agreement，贡献者许可协议）**：签署后社区才有权发布你的代码。这是参与贡献的前置条件，在 [cann-community](https://gitcode.com/cann/community) 完成。
- **Issue**：GitCode 上的讨论工单。「Issue 先行」是本仓库的硬规矩——凡涉及新增特性/接口/配置的改动，**必须先发 Issue 讨论方案**，避免代码写完却被拒绝合入。
- **SIG（Special Interest Group，特别兴趣小组）**：社区按领域组织的评审团体。本项目对应 Ops-transformer SIG，新算子需求要在 SIG 例会上评审，通过后由 SIG 成员为你分配算子分类路径。
- **Committer / Maintainer**：两级检视角色。Committer 负责技术检视、反馈意见，通过后打 `/lgtm` 标签；Maintainer 做最终审核，打 `/approve` 标签合入。
- **fork-branch-PR 工作流**：fork 官方仓到个人空间 → 本地拉特性分支开发 → push 后向官方仓目标分支发 Pull Request。
- **git hook**：git 在特定动作（如 commit）前后自动执行的脚本。pre-commit 是一个 hook 框架，把 `.pre-commit-config.yaml` 里声明的检查安装成 `.git/hooks/pre-commit`。
- **开源合规（合规审计）**：确保仓库里每个源文件都有正确的许可证头与版权声明，且不混入二进制/归档文件。这类问题一旦流入发布包会引发法律风险，所以放在提交前的最后一道门禁。

承接前几讲的认知：u7-l1 讲过 UT 是 CI 门禁的一部分，u7-l2 讲过 `--PR_UT`/`--PR_PKG` 按 PR 变更文件裁剪执行——本讲把视角拉高，看**整个贡献流程**以及你在本地能自查的两道门禁（pre-commit 与 OAT）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [CONTRIBUTING.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CONTRIBUTING.md) | 贡献总纲：五种贡献场景、六步流程、两套交付件清单、门禁与合入规则 |
| [.gitcode/PULL_REQUEST_TEMPLATE.zh-CN.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/.gitcode/PULL_REQUEST_TEMPLATE.zh-CN.md) | PR 描述模板：四个必填段 + 类型标签 |
| [docs/zh/develop/aicore_develop_guide.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/aicore_develop_guide.md) | 标准算子开发指南：交付件明细、目录创建、`--experimental` 编译 |
| [docs/zh/develop/pre-commit_guide.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/pre-commit_guide.md) | pre-commit 配置指导书：五类 hook 的功能与用法 |
| [.pre-commit-config.yaml](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/.pre-commit-config.yaml) | pre-commit 实际配置：hook 版本、文件过滤、参数 |
| [OAT.xml](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/OAT.xml) | OAT 合规审计策略：许可证/版权策略、豁免规则、许可证文本匹配器 |
| [scripts/oat_check.sh](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/scripts/oat_check.sh) | OAT 检查脚本：安装 oat-py、确定扫描范围、解析报告并阻断提交 |
| [docs/CONTRIBUTING_DOCS.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/CONTRIBUTING_DOCS.md) | 文档贡献指南：文档类改动的范围与流程 |
| [cmake/custom_build.cmake](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/custom_build.cmake) | 构建侧对 `experimental/` 目录的接入点 |
| [examples/add_example/op_host/add_example_tiling.cpp](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_tiling.cpp) | 许可证头标准样例（复制它的头格式） |

## 4. 核心概念与源码讲解

### 4.1 贡献流程全景：从 Issue 到合入的六步流水线

#### 4.1.1 概念说明

`CONTRIBUTING.md` 定义了五种贡献场景：贡献新算子、算子 Bug 修复、算子优化、文档纠错、帮助解决他人 Issue。其中「贡献新算子」流程最长，也是本讲主线。

这个流程的核心设计思想是：**先对齐，再动手**。Issue 阶段对齐「要不要做」，SIG 评审对齐「放在哪、怎么做」，PR 阶段才对齐「代码本身」。跳过前两步直接发 PR，最常见的结局是方向不被接受或目录放错位置。

#### 4.1.2 核心流程

新算子贡献的六步流水线：

```text
1. 创建 Issue（Requirement|需求建议）
   └─ 内容：背景信息 / 价值作用 / 设计方案
2. 需求评审（Ops-transformer SIG 例会）
   ├─ 紧急：申请临时 SIG 议题 + 邮件 maintainer
   └─ 接纳：SIG 成员分配算子分类路径（如 experimental/attention）
3. PR 提交（fork → 特性分支 → 目标分支）
   └─ 按 PR 模板填写：描述 / 关联 Issue / 测试 / 文档更新 / 类型标签
4. CI 门禁（PR 下评论 compile 触发）
   └─ 代码编译 + 静态检查 + UT 测试 + 冒烟测试
5. Committer 检视 → 按意见修改 → @ Committer
6. Maintainer 审核 → /lgtm → /approve → 合入
```

注意第 4 步：门禁不是自动触发的，需要**在 PR 下评论 `compile` 指令**手动拉起；门禁通过后要在关联 Issue 里 @ Committer 推进检视。

#### 4.1.3 源码精读

五种贡献场景的定义在 [CONTRIBUTING.md:12-20](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CONTRIBUTING.md#L12-L20)，这段列出了后续小节的导航锚点——Bug 修复、算子优化、文档纠错三类都收敛为「新建对应类型 Issue」。

Issue 需要写什么，见 [CONTRIBUTING.md:32-40](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CONTRIBUTING.md#L32-L40)：背景信息、价值/作用、设计方案三要素。

需求评审与紧急通道在 [CONTRIBUTING.md:42-58](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CONTRIBUTING.md#L42-L58)。关键一句是第 58 行：

> 若需求被接纳，SIG 成员将为您分配合适的算子分类路径（如：`experimental/attention`），请将贡献算子提交至 `experimental` 对应算子分类目录。

这决定了你的代码最终放哪——**不是你自选，是评审分配**。

CI 门禁与两级检视在 [CONTRIBUTING.md:103-121](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CONTRIBUTING.md#L103-L121)：评论 `compile` 触发门禁，检查项为代码编译、静态检查、UT 测试、冒烟测试四项；Committer 检视通过后标注 `/lgtm`，Maintainer 最终标注 `/approve` 合入。静态检查若出现 codecheck 误报，交给 SIG 成员屏蔽，不要自己绕过。

PR 描述的模板在 [.gitcode/PULL_REQUEST_TEMPLATE.zh-CN.md:1-27](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/.gitcode/PULL_REQUEST_TEMPLATE.zh-CN.md#L1-L27)，包含四个段落（描述、关联的 Issue、测试、文档更新）和一组类型标签（Bug 修复/新特性/性能优化/重构/测试等）。发 PR 时这四段都要填——「测试」一段要说明做了哪些验证（UT、精度比对、泛化 shape 等），这正是 u3-l4/u7-l1 所讲测试能力的出口。

#### 4.1.4 代码实践

**实践目标**：不看答案，凭流程图复述六步流水线，并为 `my_sum` 写出 PR 描述草稿。

**操作步骤**：

1. 通读 [CONTRIBUTING.md:24-121](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CONTRIBUTING.md#L24-L121) 的「贡献新算子」全节。
2. 画出六步泳道图（自己 vs SIG vs Committer/Maintainer 各一条泳道）。
3. 对照 [.gitcode/PULL_REQUEST_TEMPLATE.zh-CN.md:1-27](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/.gitcode/PULL_REQUEST_TEMPLATE.zh-CN.md#L1-L27) 的模板，为 `my_sum` 写一份 PR 描述：描述段写「沿最后一维求和的归约算子 + 实现要点」；测试段写你在 u6-l1/u7-l1 跑过的 UT 与编译验证；类型标签勾选「✨ 新特性」。

**需要观察的现象**：模板四个段落中，「关联的 Issue」必须能填出 Issue 链接——这反过来印证「Issue 先行」不是可选项。

**预期结果**：一张泳道图 + 一份四段齐全的 PR 描述草稿。本实践为纯文档产出，无运行结果需验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么「新增 Tiling 子场景」这类改动也建议先发 Issue？

**答案**：`CONTRIBUTING.md:10` 规定，凡涉及新增特性、新增接口、新增配置参数或修改代码流程（非简单 bug 修复）的改动，务必先通过 Issue 讨论方案；不确定是否属于「简单 bug 修复」时也应提 Issue。Tiling 子场景会改变算子的切分行为，属于修改代码流程。

**练习 2**：`/lgtm` 和 `/approve` 分别由谁打？顺序能否颠倒？

**答案**：`/lgtm` 由 Committer 在检视通过后标注，`/approve` 由 Maintainer 最终审核后标注并合入（[CONTRIBUTING.md:114-121](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CONTRIBUTING.md#L114-L121)）。顺序不能颠倒——Maintainer 的合入以 Committer 技术检视通过为前提。

**练习 3**：文档错别字修复需要走 SIG 评审吗？

**答案**：不需要。它属于「文档纠错」场景，按 [docs/CONTRIBUTING_DOCS.md:29-40](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/CONTRIBUTING_DOCS.md#L29-L40) 的流程：新建 `Documentation|文档反馈` 类 Issue（或在已有 Issue 下 `/assign` 认领），修改后发 PR 即可。

### 4.2 交付件规范：生态最简算子与项目标准算子

#### 4.2.1 概念说明

`CONTRIBUTING.md` 为新算子定义了**两档交付标准**：

| 维度 | 生态最简算子 | 项目标准算子 |
|------|--------------|--------------|
| 定位 | 快速接入生态、门槛最低 | 进入正式产品目录、能力完整 |
| Kernel | 单文件 `${op_name}.cpp`（fast_kernel_launch 方式） | op_host 三件套 + op_kernel 多文件 |
| Tiling | 无独立 Tiling 实现 | 必须有 `_tiling.cpp` 等 |
| 调用方式 | PyTorch Extension 直调 `<<<>>>` | aclnn 两段式 + 图模式 |
| 测试 | 一个 `test_${op_name}.py` | tests/ut 四类 UT |
| 提交目录 | `experimental/${op_class}` | `experimental/${op_class}`（成熟后转正式目录） |
| 参考 | `examples/fast_kernel_launch_example` | `examples/add_example` |

`experimental/` 目录因此有双重含义：既是**贡献算子的落脚点**（按 SIG 分配的分类放置），也是**孵化区**（现有 `experimental/attention` 下已有 blitz_sparse_attention、kv_quant_sparse_attn_sharedkv 等算子）。

#### 4.2.2 核心流程

准备交付件时的自查顺序：

```text
1. 确认档位：走生态最简（4 类文件）还是标准算子（五层目录）
2. 对照目录树逐项盘点文件是否齐全
3. 检查命名硬约束：tiling 实现文件名必须含 "_tiling"，否则不被编译系统识别
4. 确认目录位置：experimental/${op_class}，分类由 SIG 成员分配
5. 若新增 experimental 分类 → 检查 cmake/custom_build.cmake 是否需要补 add_subdirectory
6. 编译验证：bash build.sh --pkg --soc=... --ops=... --experimental
```

#### 4.2.3 源码精读

生态最简算子的交付件目录树在 [CONTRIBUTING.md:62-72](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CONTRIBUTING.md#L62-L72)：只要 Kernel 实现文件、`tests/test_${op_name}.py`、`CMakeLists.txt`、`README.md` 四样。代码参考样例是 [examples/fast_kernel_launch_example/README.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/fast_kernel_launch_example/README.md)，其核心优势写在简介里：「单交付件——一个文件完成算子开发和 PyTorch 框架适配」，用 `<<<>>>` 语法直接启动核函数，绕过了 tiling 与 aclnn 适配。

生态档的交付要求表在 [CONTRIBUTING.md:83-87](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CONTRIBUTING.md#L83-L87)：代码交付件、文档交付件（README 必选）、精度要求（新算子需满足生态算子开源精度标准）三行。

标准算子的交付件目录树在附录 [CONTRIBUTING.md:158-178](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CONTRIBUTING.md#L158-L178)，也就是 u2 系列讲过的五层范式：`op_host`（def / tiling / 可选子场景 tiling）、`op_kernel`（入口 cpp / 头文件 / tiling_data.h / tiling_key.h）、`CMakeLists.txt`、`README.md`、`tests/ut`。

其中有一条容易被忽略的硬约束，见 [CONTRIBUTING.md:182](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CONTRIBUTING.md#L182)：

> op_host 目录下参与编译的 Tiling 实现文件，文件名须包含 `_tiling` 标识……否则不会被编译系统识别。

这承接 u1-l2 讲过的「Tiling 文件命名约定」——它不只是风格问题，是编译系统能否找到你的文件的问题。

标准档的交付要求与合规检查清单在 [CONTRIBUTING.md:186-203](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CONTRIBUTING.md#L186-L203)：多了一行「是否符合标准算子基础编程规范」，参考资料指向算子开发指南。

开发指南侧的对应内容：`--genop` 生成的标准目录结构见 [docs/zh/develop/aicore_develop_guide.md:57-72](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/aicore_develop_guide.md#L57-L72)（u6-l1 已用它生成过 `my_sum`）；贡献算子的编译命令要加 `--experimental` 旗标，见 [docs/zh/develop/aicore_develop_guide.md:427-434](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/aicore_develop_guide.md#L427-L434)：

```bash
bash build.sh --pkg --soc=${soc_version} --vendor_name=${vendor_name} --ops=${op_list} [--experimental]
```

`--experimental` 为什么必要？看构建侧的接入点 [cmake/custom_build.cmake:285-294](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/custom_build.cmake#L285-L294)：`ENABLE_EXPERIMENTAL` 开关决定编译 `experimental/attention` 还是正式的 `attention`/`mhc` 等目录。目前 `experimental/` 下只挂了 `attention` 一个分类——**如果你新增了 `experimental/gmm` 之类的分类，必须在这里补 `add_subdirectory`**，开发指南 [docs/zh/develop/aicore_develop_guide.md:98-108](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/aicore_develop_guide.md#L98-L108) 明确画了这段改法。

#### 4.2.4 代码实践

**实践目标**：为 `my_sum` 生成一份交付件自查清单，并规划它从 `examples/my_sum` 迁移到贡献目录的路径。

**操作步骤**：

1. 假设 SIG 评审把 `my_sum` 分配到 `experimental/attention`（复用现有分类，零 cmake 改动）。
2. 对照 [CONTRIBUTING.md:158-178](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CONTRIBUTING.md#L158-L178) 的标准算子目录树，逐项盘点 `my_sum` 已有哪些文件（u6-l1 的 genop 骨架 + u7-l1 的 UT），列出缺失项。
3. 执行迁移演练（在自己的 fork 里做，不要动主仓工作副本）：

```bash
cp -r examples/my_sum experimental/attention/my_sum
bash build.sh --pkg --soc=ascend910b --ops=my_sum --experimental
```

4. 观察编译日志确认 `experimental/attention/my_sum` 被纳入编译。

**需要观察的现象**：不加 `--experimental` 时，`experimental/attention` 下的算子不会被编译（对应 `custom_build.cmake` 走 `else()` 分支编正式目录）；加上后日志中能看到 experimental 路径参与编译。

**预期结果**：一份「已有 / 缺失」两列的交付件清单表；`--experimental` 编译产出 `build_out/` 下的 `.run` 包。编译行为与 u6-l1 一致，差异仅在目录与旗标；具体日志输出待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：生态最简算子和标准算子最本质的区别是什么？

**答案**：调用形态。生态最简算子走 fast_kernel_launch，单文件 Kernel + PyTorch Extension，用 `<<<>>>` 直接启动核函数，无独立 Tiling、无 aclnn 适配；标准算子是完整五层目录范式，含 op_host 三件套与 tiling，支持 aclnn 两段式与图模式调用。

**练习 2**：你把子场景 tiling 命名为 `my_sum_tiling_arch35_v2.cpp`，能被编译吗？

**答案**：能。编译系统只要求文件名包含 `_tiling` 标识（[CONTRIBUTING.md:182](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CONTRIBUTING.md#L182)），该名字包含 `_tiling`，符合识别规则。

**练习 3**：为什么贡献目录统一放 `experimental/` 而不是直接放正式的 `attention/`？

**答案**：`experimental/` 是孵化区，贡献算子先在此沉淀，由 SIG 评审分配分类路径（[CONTRIBUTING.md:58](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CONTRIBUTING.md#L58)）。正式目录算子承担产品化交付责任，交付件与精度标准更严格（对比 [CONTRIBUTING.md:83-87](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CONTRIBUTING.md#L83-L87) 与 [L186-191](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CONTRIBUTING.md#L186-L191)），且构建上由 `ENABLE_EXPERIMENTAL` 开关隔离（[cmake/custom_build.cmake:285-294](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/custom_build.cmake#L285-L294)）。

### 4.3 本地代码规范门禁：pre-commit 五类检查

#### 4.3.1 概念说明

CI 门禁（编译、静态检查、UT、冒烟）跑在服务端，反馈周期长；pre-commit 把**格式与规范类检查前移到本地 `git commit` 时刻**，让你在推送前就发现问题。两者是互补关系：pre-commit 管「格式与低级错误」，CI 管「编译与行为正确性」。

pre-commit 是一个通用的 git hooks 框架：仓库根的 `.pre-commit-config.yaml` 声明要跑哪些 hook，`pre-commit install` 把它们装进 `.git/hooks/pre-commit`，此后每次 `git commit` 自动逐个执行，任一 hook 失败则提交被阻断。

本项目配置了五类 hook：基础规范检查、clang-format（C/C++）、ruff（Python）、codespell（拼写）、OAT 合规检查。

#### 4.3.2 核心流程

```text
git commit
  └─ .git/hooks/pre-commit 触发
       ├─ 1) pre-commit-hooks：行尾空格 / 文件末尾换行 / YAML·JSON 语法 / 大文件 / 合并冲突标记 / 私钥
       ├─ 2) clang-format：按 .clang-format 格式化 .c/.h/.cpp/.hpp/.cc/.hh/.cxx/.hxx/.asc（排除 build/ 与 tests/third_party/）
       ├─ 3) ruff-check + ruff-format：Python 静态检查（带 --fix）与格式化
       ├─ 4) codespell：拼写检查（术语白名单 + 大范围 skip）
       └─ 5) OAT Compliance Check：调 scripts/oat_check.sh 做合规扫描
            ├─ 全部 Passed → 提交放行
            └─ 任一 Failed → 提交阻断，修复后重新 git add + git commit
```

首次运行时，前四类 hook 会按 `rev` 锁定的版本自动下载到隔离环境（与系统装的工具互不影响），OAT 则由脚本自动 `pip install oat-py`。紧急情况可用 `git commit --no-verify` 跳过，但正常流程不允许。

#### 4.3.3 源码精读

配置入口 [`.pre-commit-config.yaml:10-16`](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/.pre-commit-config.yaml#L10-L16) 声明了 `minimum_pre_commit_version: 4.0.0`（装老版本 pre-commit 会直接拒绝运行）和全局排除规则 `exclude: ^LICENSES/|\.(html|csv|svg)$`。

第一类基础检查在 [`.pre-commit-config.yaml:20-31`](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/.pre-commit-config.yaml#L20-L31)，来自 `pre-commit/pre-commit-hooks` v4.6.0：`trailing-whitespace`、`end-of-file-fixer`、`check-yaml`、`check-added-large-files`、`check-merge-conflict`、`detect-private-key` 六个小工具。注意 `check-json` 单独排除了 `.*_runtime_kb\.json$` 这类大 JSON。

第二类 clang-format 在 [`.pre-commit-config.yaml:33-43`](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/.pre-commit-config.yaml#L33-L43)：版本 v18.1.8，只作用于 `.(c|h|cpp|hpp|cc|hh|cxx|hxx|asc)$` 后缀（`.asc` 是 Ascend C 源文件），排除 `^build/|tests/third_party/`。参数 `-i` 表示**直接改写文件**——所以它既是检查也是自动修复，改完需要重新 `git add`。其风格要点（4 空格缩进、不限列宽 `ColumnLimit: 0`、短枚举不合并、构造函数初始化列表逐行）记录在 [docs/zh/develop/pre-commit_guide.md:133-143](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/pre-commit_guide.md#L133-L143)。「不限列宽但不自动拆行」是个刻意的取舍：换行由开发者自己控制，避免机器把长表达式重排得面目全非。

第三类 ruff 在 [`.pre-commit-config.yaml:45-52`](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/.pre-commit-config.yaml#L45-L52)：`ruff-check` 带 `--fix` 自动修复，并忽略了一大串规则（F841 未用变量、F401 未用导入、E402 导入位置等）——算子仓的 Python 多为数据生成/比对脚本，这些规则噪音大于收益。

第四类 codespell 在 [`.pre-commit-config.yaml:54-65`](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/.pre-commit-config.yaml#L54-L65)。细看它的 `--skip` 参数会发现：`*.cpp,*.h,*.md,*.py,*.sh` 等绝大多数常见类型都被跳过了——所以这条 hook 的实际检查面很窄，主要兜底少数未列入 skip 的文本文件。`-L` 后的白名单（CANN、ascend、EnQue 等）则保证项目术语不误报。

第五类 OAT 是唯一的 local hook，在 [`.pre-commit-config.yaml:67-77`](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/.pre-commit-config.yaml#L67-L77)：`entry: bash scripts/oat_check.sh`、`pass_filenames: true` 把暂存文件列表传给脚本。它的细节留到 4.4 精读。

使用方法速查在 [docs/zh/develop/pre-commit_guide.md:94-109](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/pre-commit_guide.md#L94-L109)：`pre-commit run --all-files` 查全仓、`pre-commit run clang-format` 跑单项；pre-commit 不支持目录参数，要查目录需 `find examples -type f | xargs pre-commit run --files`。批量整理历史代码另有 [scripts/format_cpp.sh](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/scripts/format_cpp.sh)，它自动排除 `build/`、`build_out/`、`third_party/`、`.git/`（见 [docs/zh/develop/pre-commit_guide.md:179-191](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/pre-commit_guide.md#L179-L191)）。

#### 4.3.4 代码实践

**实践目标**：在本地为 `my_sum` 的改动跑一遍 pre-commit，体验自动修复与阻断两个行为。

**操作步骤**：

1. 安装（Python 3.9+，见 [docs/zh/develop/pre-commit_guide.md:31-63](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/pre-commit_guide.md#L31-L63)）：

```bash
pip3 install pre-commit
cd /path/to/ops-transformer
pre-commit install        # 预期输出: pre-commit installed at .git/hooks/pre-commit
```

2. 在 `my_sum` 的某个 `.cpp` 里故意加一处行尾空格和一处不符合 4 空格缩进的代码，然后：

```bash
git add experimental/attention/my_sum
git commit -m "feat: add my_sum operator"
```

3. 改完 clang-format 自动修复的文件后重新 `git add` 再 commit。

**需要观察的现象**：第一次 commit 时 `trailing-whitespace` 与 `clang-format` 报 Failed 且文件被改写（行尾空格被删、缩进被修正）；重新 add 后第二次 commit 全部 Passed。首次运行会先下载 clang-format/ruff/codespell 环境，耗时明显偏长且需要联网。

**预期结果**：`git commit` 被阻断一次、放行一次，最终 hooks 目录下存在 pre-commit 脚本。hook 下载与运行行为依赖网络环境，具体耗时与输出待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 clang-format 排除了 `^build/` 和 `tests/third_party/`？

**答案**：`build/` 是构建产物目录（不属于源码交付件），`tests/third_party/` 是第三方测试框架源码——两者都不应被本仓库的格式规则改写（见 [`.pre-commit-config.yaml:43`](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/.pre-commit-config.yaml#L43)）。这也呼应 u1-l4 讲过的「build/ 是构建树、不进版本库」。

**练习 2**：`git commit --no-verify` 什么时候可以用？

**答案**：仅紧急情况（[docs/zh/develop/pre-commit_guide.md:112-118](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/pre-commit_guide.md#L112-L118)）。它跳过全部本地检查，正常开发流程应保证检查通过——而且跳过了本地门禁，CI 门禁仍会在 PR 侧拦截同类问题，只是发现得更晚。

**练习 3**：ruff 的 `--ignore` 列表里为什么要忽略 F401（未使用导入）？

**答案**：算子仓的 Python 主要是测试数据生成/比对脚本（gen_data.py、compare_data.py 等），常有意保留的批量导入或兼容性导入；对这类脚本严格执行未用导入检查，误报成本高于收益，故在 [`.pre-commit-config.yaml:50`](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/.pre-commit-config.yaml#L50) 显式放宽。

### 4.4 OAT 开源合规检查：OAT.xml 与 oat_check.sh

#### 4.4.1 概念说明

OAT（Open Source Audit Tool）是开源合规审计工具，本项目用的是 Python 版 `oat-py`。它回答三个问题：

1. **许可证头**：每个源文件是否带正确的 CANN License 头？
2. **文件类型**：是否混入了二进制文件（如 `.so`、图片）或归档文件（如 `.zip`、`.tar`）？
3. **版权声明**：版权归属是否合规？

为什么卡这么严？因为算子库会打包成 `.run`/`.rpm`/`.deb` 发布（u7-l2），一个缺失许可证头的文件流入发布包就是法律瑕疵；一个误提交的二进制文件会永久膨胀仓库体积。

**OAT.xml 是策略**（声明「什么算合规」），**oat_check.sh 是执行器**（声明「怎么扫、怎么阻断」）。两者配合构成 pre-commit 的第五类 hook。

#### 4.4.2 核心流程

`oat_check.sh` 的执行流程：

```text
1. 找 Python 3.7+ 解释器（python3 → python → py 依次探测）
   └─ 找不到 → 打 WARNING 跳过检查，放行提交（不硬卡环境）
2. 确保 oat-py 已安装（首次 pip install oat-py>=1.0.1，flock 文件锁防并发 pip 冲突）
3. 确定扫描范围
   ├─ PR 场景：找 HEAD 与远端 master/main/develop/dev 的 merge-base，扫描整个 PR 差异
   │            并以 HEAD SHA 前 12 位做 done-marker，同一 PR 只扫一次
   └─ 直接在主干提交：只扫当前暂存区文件
4. 组装命令：python -m oat -mode s -s <repo> -r <report_dir> -f <文件列表> -oatconfig OAT.xml
5. 解析报告 PlainReport.txt，提取两项计数
6. 判定：
   \[ \text{TOTAL\_ISSUES} = N_{\text{InvalidFileType}} + N_{\text{LicenseHeaderInvalid}} \]
   TOTAL_ISSUES > 0 → 打印明细 + 写 oat_reports/result.txt → exit 1 阻断提交
   TOTAL_ISSUES = 0 → [OK] 放行
```

#### 4.4.3 源码精读

**策略侧（OAT.xml）**：合规策略定义在 [OAT.xml:14-19](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/OAT.xml#L14-L19)——两条 policyitem：全仓 `path=".*"` 要求许可证为 `CANN-2.0`，版权声明为 `Huawei Technologies Co., Ltd.`（规则 `may`，即允许出现）。

豁免规则在 [OAT.xml:27-34](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/OAT.xml#L27-L34)：`*.yaml`、`*.yml`、`*.csv` 三类文件免查许可证头——YAML 是配置、CSV 是测试用例数据，本身不适合塞注释头。这就是 pre-commit 指导书里「YAML 配置文件和 CSV 测试用例文件已豁免」的出处。

许可证文本匹配器在 [OAT.xml:62-72](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/OAT.xml#L62-L72)：当扫描结果报 InvalidLicense 时，按这里定义的 `cann License` 标准文本做匹配。你的文件头必须与这段文本实质一致。

**标准许可证头长什么样**？看 [examples/add_example/op_host/add_example_tiling.cpp:1-9](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_tiling.cpp#L1-L9)——`Copyright (c) 2025 Huawei Technologies Co., Ltd.` 起头的 8 行注释块。给新文件补头时，直接从仓库任一现有源文件复制这个块即可。

**执行器侧（oat_check.sh）**：Python 探测逻辑在 [scripts/oat_check.sh:34-48](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/scripts/oat_check.sh#L34-L48)，找不到就打 WARNING 放行——环境缺失不硬卡提交，这是「合规检查尽量不挡开发」的宽松设计。

oat-py 的安装与文件锁在 [scripts/oat_check.sh:53-77](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/scripts/oat_check.sh#L53-L77)：`flock -w 120 9` 保证多个并发 pre-commit 进程只有一个跑 pip，锁内还会复查一遍避免重复安装——pre-commit 对每个 hook 可能起多个并行实例，这个锁是必要的。

最精巧的是 PR 范围去重策略，注释写在 [scripts/oat_check.sh:88-99](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/scripts/oat_check.sh#L88-L99)：

```text
1. 找 HEAD 与上游分支（origin/master 等）的 merge-base → 特性分支场景，
   扫描分支分叉以来变更的全部文件（整个 PR 差异）
2. 以 HEAD SHA 为键做 done-marker：第一次调用跑扫描，
   同一 HEAD 的后续调用直接 exit 0 —— 消除 CI 每个 commit 重复调用 hook 的冗余
3. 找不到 merge-base（如直接在 master 上提交）→ 退化为只扫暂存区文件，不做标记
```

这解释了一个现象：**在特性分支上改了 A 文件提交后，再改 B 文件提交，第二次 commit 时 A 也会被重新扫描**——因为 HEAD SHA 变了，done-marker 失效，属于预期行为。

命令组装在 [scripts/oat_check.sh:257-264](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/scripts/oat_check.sh#L257-L264)：`python -m oat -mode s -s <repo_root> -r <report_dir> -n <repo_name> -w 1 -f <逗号分隔文件列表>`，仓库根存在 OAT.xml 时追加 `-oatconfig`——策略与执行器在这里挂接。

判定与阻断在 [scripts/oat_check.sh:376-397](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/scripts/oat_check.sh#L376-L397)：`TOTAL_ISSUES=$(( _INVALID_TYPE + _LICENSE_INVALID ))` 大于 0 时打印「Commit blocked」、输出明细（同时落盘 `oat_reports/result.txt`）并 `exit 1`。注意它**只统计两项**：Invalid File Type 与 License Header Invalid；报告中其他 section（版权头、README 等）只落盘不阻断——「记录但不拦截」，避免过度卡人。

#### 4.4.4 代码实践

**实践目标**：亲手触发一次 OAT 阻断，再修复到通过，理解判定口径。

**操作步骤**：

1. 准备一个不带许可证头的新文件并暂存（在 fork 里做）：

```bash
printf 'int foo(void) { return 0; }\n' > experimental/attention/my_sum/op_host/no_header.cpp
git add experimental/attention/my_sum/op_host/no_header.cpp
bash scripts/oat_check.sh experimental/attention/my_sum/op_host/no_header.cpp
```

2. 观察 exit code 与输出；再查看 `cat oat_reports/result.txt`。
3. 从 [examples/add_example/op_host/add_example_tiling.cpp:1-9](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_tiling.cpp#L1-L9) 复制许可证头补到 `no_header.cpp` 顶部，重新运行脚本。
4. 对照 [OAT.xml:27-34](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/OAT.xml#L27-L34) 验证：把文件改名成 `no_header.yaml` 再跑一次，观察是否被豁免。

**需要观察的现象**：第 1 步预期输出 `License Header Invalid Total Count: 1`、提示 Commit blocked、脚本退出码为 1；第 3 步补头后预期 `[OAT] [OK] All checks passed`；第 4 步 YAML 改名后预期跳过许可证头检查。首次运行会自动 pip 安装 oat-py，较慢。

**预期结果**：阻断 → 修复 → 通过的完整闭环。OAT 扫描依赖 oat-py 安装与网络，具体输出待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 OAT 阻断项只有「文件类型」和「许可证头」两项，而不包括版权头？

**答案**：脚本 [L376-L397](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/scripts/oat_check.sh#L376-L397) 只把 `_INVALID_TYPE + _LICENSE_INVALID` 计入 TOTAL_ISSUES，版权等其他 section 仅写入 result.txt 备查。这是分级处理：许可证头缺失与二进制混入是硬风险必须拦截，版权年份等细节记录后由人工复核。

**练习 2**：你在特性分支上连续做了 3 次 commit，OAT 会不会扫 3 次全量 PR 文件？

**答案**：会扫 3 次，但每次范围都是「整个 PR 差异」。done-marker 以 HEAD SHA 为键（[scripts/oat_check.sh:95-96](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/scripts/oat_check.sh#L95-L96)），每次 commit 后 HEAD 都变，标记失效；它消除的是「同一 HEAD 被重复调用」（如 CI 对一个 commit 多次跑 hook），不是「多次 commit」。

**练习 3**：`tests/test_config.yaml` 没有 CANN License 头，为什么不算违规？

**答案**：[OAT.xml:31-32](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/OAT.xml#L31-L32) 的 `defaultPolicyFilter` 把 `*.yaml`/`*.yml` 列为豁免项——YAML 配置文件不适合嵌注释型许可证头，策略上显式放行。

## 5. 综合实践

**任务**：把 u6-l1 开发的 `my_sum` 算子整理成一次「可发 PR」的完整贡献，在本地走完全部自查门禁。

**步骤**：

1. **整理目录**。在自己的 fork 上建分支 `feat/my_sum`，把 `examples/my_sum` 迁移到 SIG 分配的分类目录（本演练用 `experimental/attention/my_sum`，复用现有分类则无需改 `cmake/custom_build.cmake`；若你演练时假设新增 `experimental/gmm` 分类，则需按 [docs/zh/develop/aicore_develop_guide.md:98-108](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/develop/aicore_develop_guide.md#L98-L108) 补 `add_subdirectory`）。

2. **补齐交付件**。对照 [CONTRIBUTING.md:158-178](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CONTRIBUTING.md#L158-L178) 逐项核对：
   - `op_host`：def / infershape / tiling（文件名含 `_tiling`）/ CMakeLists.txt
   - `op_kernel`：入口 cpp / 头文件 / tiling_data.h / tiling_key.h
   - `tests/ut`：u7-l1 已补的 tiling 与 infershape UT
   - `README.md`：仿照 [examples/add_example/README.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/README.md) 的结构写「产品支持情况 + 功能说明 + 算子原型」
   - 所有新文件补 CANN License 头（从 [examples/add_example/op_host/add_example_tiling.cpp:1-9](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/op_host/add_example_tiling.cpp#L1-L9) 复制）

3. **本地门禁自查**：

```bash
pip3 install pre-commit && pre-commit install
git add experimental/attention/my_sum
git commit -m "feat: add my_sum operator along last dim"
bash build.sh --pkg --soc=ascend910b --ops=my_sum --experimental   # 编译自查
bash build.sh --ophost_test --ops=my_sum                           # UT 自查（无需 NPU）
```

4. **写 PR 描述**。按 [.gitcode/PULL_REQUEST_TEMPLATE.zh-CN.md:1-27](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/.gitcode/PULL_REQUEST_TEMPLATE.zh-CN.md#L1-L27) 填四段：描述（算子功能 + 实现要点 + tiling 策略）、关联的 Issue（演练时填模拟 Issue 链接）、测试（UT 结果 + 编译验证）、文档更新（README 路径），类型标签勾选「✨ 新特性」。

**验收标准**：pre-commit 五类 hook 全 Passed、OAT 输出 `[OK]`、`--experimental` 编译产出 `.run` 包、UT 通过、PR 描述四段齐全。编译与 UT 结果依赖本地环境，待本地验证。

## 6. 本讲小结

- 贡献流程是**六步流水线**：Issue → SIG 评审（分配 `experimental/${op_class}` 目录）→ PR → 评论 `compile` 触发 CI 门禁（编译/静态检查/UT/冒烟）→ Committer `/lgtm` → Maintainer `/approve` 合入；「Issue 先行」是非简单 bug 修复类改动的硬要求。
- 交付件分**两档**：生态最简算子只要 Kernel 单文件 + py 测试 + CMakeLists + README 四样（fast_kernel_launch 形态）；项目标准算子要完整五层目录，且 tiling 文件名必须含 `_tiling` 才被编译系统识别；贡献算子编译需加 `--experimental`，对应 `custom_build.cmake` 的 `ENABLE_EXPERIMENTAL` 分支。
- **pre-commit 是本地门禁**，五类 hook（基础规范 / clang-format v18.1.8 / ruff / codespell / OAT）在 `git commit` 时自动执行；clang-format 带 `-i` 会直接改写文件，改完要重新 `git add`；codespell 因大范围 `--skip` 实际检查面很窄。
- **OAT 是合规门禁**：`OAT.xml` 声明策略（全仓 CANN-2.0 许可证 + Huawei 版权，yaml/yml/csv 豁免），`oat_check.sh` 是执行器（自动装 oat-py、flock 防并发、merge-base 定 PR 范围、HEAD SHA done-marker 去重）；阻断项只有「Invalid File Type + License Header Invalid」两项，其余记录不拦截。
- 环境缺失时 OAT 选择**放行而非硬卡**（找不到 Python 或装不上 oat-py 仅打 WARNING），但合规问题本身不消失——CI 侧与人工检视仍会兜底。
- PR 模板四段（描述/关联 Issue/测试/文档更新）中，「测试」一段是你在 u3-l4、u7-l1 积累的测试能力向社区交付的出口。

## 7. 下一步学习建议

- **收尾本手册**：进入 u7-l4「架构复盘与学习路线总结」，从五层范式、common 复用、多 SoC 适配、版本演进四个维度回顾整个算子库的设计取舍。
- **阅读社区协作文档**：[cann-community](https://gitcode.com/cann/community) 的 Issue 操作指南、PR 操作指南与 C++ 编程规范是 CONTRIBUTING.md 多处引用的上游文档，发真实 PR 前值得通读。
- **看一个真实贡献样本**：浏览 `experimental/attention/` 下已合入的算子（如 `blitz_sparse_attention`、`kv_quant_sparse_attn_sharedkv`）的目录构成与 README 写法，对照你为 `my_sum` 整理的清单找差距。
- **延伸阅读**：[docs/CONTRIBUTING_DOCS.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/CONTRIBUTING_DOCS.md)（文档类贡献的规范，含原子化提交原则）；若计划贡献涉及精度验收，再读 [CONTRIBUTING.md:87](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CONTRIBUTING.md#L87) 链接的生态算子开源精度标准。
