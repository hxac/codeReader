# 版本兼容、贡献流程与常见问题

## 1. 本讲目标

本讲是「工程化、调试与贡献」单元的收尾篇，也是整本手册的总结篇。前面六单元你已经学会「认识 ATB → 调用算子 → 理解内核链路 → 精读关键算子 → 通信与图算子 → 自定义算子开发 → 测试与编译」。本讲要回答三个把知识变成「实际产出」的问题：

1. **版本兼容**：ATB 的版本号意味着什么？升级 ATB 会不会让我的代码失效？它要求配套哪些 CANN 软件包？
2. **贡献流程**：我想给 ATB 贡献一个新算子或修一个 Bug，从 fork 到 PR 被合入，完整要走哪些步骤？有哪些门禁（CI、pre-commit、CLA）必须过？
3. **常见问题（FAQ）**：编译/安装时最常见的报错（如 `libtbe_adapter.so does not exist`）怎么解决？

学完后，你应该能：

- 读懂 `version.info` 里的版本声明与依赖约束，并据此判断「这个 ATB 能否和我的 CANN/PyTorch 一起用」。
- 独立完成一次完整的代码贡献：fork → 本地开发 → pre-commit 自检 → 提 PR → 触发 `/compile` 门禁 → 响应评审 → 合入。
- 写出一份「新算子 PR 检查清单」，把代码、测试、文档、配置四类交付件一次性凑齐。

## 2. 前置知识

本讲建立在 [u1-l3 构建系统与编译运行](u1-l3-build-system.md) 之上，并大量呼应 [u7-l4 编译选项、ABI 与 Sanitizers](u7-l4-build-options-abi.md)。如果你还没读过它们，至少需要先建立这几个概念：

- **CXX11 ABI**：GCC 宏 `_GLIBCXX_USE_CXX11_ABI` 决定 `std::string` 等容器的内存布局。ATB 与 PyTorch 的 ABI 必须一致，否则链接报 `undefined symbol` 或运行时传参错乱。ATB 默认用 `torch.compiled_with_cxx11_abi()` 自动探测，并把产物物理隔离到 `output/atb/cxx_abi_0` 或 `cxx_abi_1`。**切换 ABI 必须加 `--clean-first`**。
- **交付件（deliverables）**：一个新算子要进入 ATB，不只是写一段计算代码，还需要 Param 定义、ini 规格约束、JSON 序列化、测试用例、Kernel 四件套、构建登记等「配置类交付件」。详见 [u6-l4 算子交付件与配置体系](u6-l4-deliverables-config.md)。
- **rsv 版本闸门**：每个高层 Param 末尾的 `rsv[N]` 预留字段必须全 0，`CreateOperation` 入口会逐字节校验，是 ATB 跨版本兼容的关键闸门。详见 [u2-l3 算子参数体系与公共枚举](u2-l3-op-params.md)。
- **CANN 软件栈**：昇腾的完整软件栈包含 toolkit（编译器与运行时）、kernels/ops 算子包、nnal（加速库运行时依赖）三类 `.run` 包，三者版本须配套。详见 README。

> 名词速查
>
> - **CLA**（Contributor License Agreement，贡献者许可协议）：贡献代码前必须签署的法律协议，分个人/法人两类。
> - **门禁（gate / CI pipeline）**：PR 提交后自动跑的检查流水线，通过才允许合入。
> - **NNAL**：CANN 加速库运行时依赖软件包，提供 `libtbe_adapter.so` 等构建期依赖。
> - **保护分支**：只允许特定角色 push/merge 的分支（如 `master`）；非保护分支限制宽松。

## 3. 本讲源码地图

本讲主要围绕「文档与配置」展开，涉及的文件多为 Markdown 与声明性配置，而非 `.cpp` 计算代码：

| 文件 | 作用 |
| --- | --- |
| [version.info](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/version.info#L1-L6) | ATB 的版本号声明与最小依赖 CANN 包版本约束 |
| [README.md](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/README.md#L65-L67) | 版本兼容性说明（一年 ABI 兼容、8.5 配套 toolkit）、参与贡献入口 |
| [docs/contributing.md](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/contributing.md#L1-L3) | 贡献指南：CLA、贡献类别、Issue、PR 全流程 |
| [docs/contributors/gitcode-workflow.md](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/contributors/gitcode-workflow.md#L1-L10) | GitCode 工作流：fork、clone、分支、rebase、push、建 PR 的可复现命令 |
| [.pre-commit-config.yaml](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/.pre-commit-config.yaml#L1-L2) | 本地 + CI 门禁的代码风格与质量检查配置 |
| [docs/contributors/infra-faqs.md](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/contributors/infra-faqs.md#L1-L5) | PR 基础设施 FAQ：CLA 标签、CI 未触发、fork 同名 |
| [docs/faq.md](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/faq.md#L1-L4) / [docs/常见问题与回答.md](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/常见问题与回答.md#L1-L4) | 编译安装 FAQ：`libtbe_adapter.so` 缺失、NNAL 安装、ABI 确定 |
| [LICENSE](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/LICENSE#L1-L4) | CANN Open Software License Agreement Version 2.0，新文件须带版权头 |
| [scripts/build.sh](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/scripts/build.sh#L423-L433) | 构建时生成 `output/version.info` 构建溯源信息、ABI 自动探测 |

---

## 4. 核心概念与源码讲解

### 4.1 版本兼容：version.info 与一年 ABI 兼容承诺

#### 4.1.1 概念说明

任何加速库的「升级安全性」都由两条线决定：

- **源码/二进制兼容**：升级到新版本后，旧调用代码是否还能编译通过、运行正确。这关系到调用方敢不敢升级。
- **配套依赖兼容**：这个库要求宿主环境里装了哪些「配套软件」、哪些版本。装错版本会直接编译失败或运行崩溃。

ATB 用一个极简的 `version.info` 文件把这两条线的「约束侧」显式声明出来。它本身很短，却是理解「ATB 能否在我的机器上跑」的第一手依据。注意它解决的是**约束声明**；真正的**二进制兼容**则由 ABI 承诺 + rsv 闸门 + 同名两层 Operation 的接口稳定共同保证（见 [u7-l4](u7-l4-build-options-abi.md)）。

#### 4.1.2 核心流程

ATB 的版本兼容模型可以概括为「**一个版本号、三类最小依赖、一条一年 ABI 承诺、一道 rsv 闸门**」。

1. **版本号**：`version.info` 首行 `Version=8.5.0` 标明本仓库对应的 CANN 大版本。ATB 的版本号与 CANN 主版本对齐（8.5.0 对应 CANN 8.5）。
2. **三类最小依赖**：声明 ATB 运行所需的 HCCL、runtime、toolkit 三类 CANN 包的最低版本（均为 `>=8.2`）。这是「配套依赖兼容」的硬约束。
3. **一年 ABI 承诺**：ATB 的对外 API 保证前后一年的 ABI（Application Binary Interface，二进制接口）兼容——在不涉及新功能的前提下，一年内升级 ATB 不会破坏调用方的已编译二进制。
4. **rsv 版本闸门**：跨版本时，新增的 Param 字段通过末尾 `rsv[N]` 预留区扩展；`CreateOperation` 入口校验 rsv 必须全 0，旧代码传不存在的字段会被拒（详见 [u2-l3](u2-l3-op-params.md)）。

一个**容易混淆**的点：仓库根目录的 `version.info`（约束声明）与构建时在 `output/` 下生成的同名 `version.info`（构建溯源）是**两个不同文件**，不要搞混：

```
仓库根 version.info      →  人写的「版本与依赖约束」声明
output/version.info      →  构建机自动生成的「Platform/branch/commit id」溯源信息
```

> 关于 `required_package` 字段的准确性说明
>
> 经在仓库内检索（`grep -rn "required_package" --include="*.sh" --include="*.py" --include="*.cpp" ...`），仓库自身的构建脚本并**不直接读取** `version.info` 里的 `required_package_*` 字段。这些字段是面向 **CANN/NNAL 打包与出包依赖校验**的声明性契约，由上层打包工具链消费。因此不要误以为 `build.sh` 会逐行解析它——它的角色是「声明」而非「脚本输入」。

#### 4.1.3 源码精读

**① 版本与依赖声明** —— [version.info:L1-L6](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/version.info#L1-L6)

```ini
Version=8.5.0

required_package_hccl_version>="8.2"
required_package_runtime_version>="8.2"
required_package_toolkit_version>="8.2"
```

- `Version=8.5.0`：本仓库对应 CANN 8.5。
- 三行 `required_package_*_version>="8.2"`：HCCL（集合通信库）、runtime（运行时）、toolkit（编译器与工具）三类 CANN 包的最低版本均为 8.2。注意这里是**最小下限**（`>=8.2`），而不是精确等号——这意味着 8.2 及以上的 CANN 在依赖侧都满足条件，但具体能否编译还受下一处「8.5 配套」硬约束限制。

**② 一年 ABI 兼容 + 8.5 配套 toolkit** —— [README.md:L65-L67](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/README.md#L65-L67)

> ATB 的 API 保证前后一年的 ABI 兼容能力，在不涉及新功能的情况下，调用者升级一年内的 ATB 版本，不会出现兼容问题。由于 CANN 出包目录调整，ATB 8.5 版本以及主线分支必须匹配 8.5 或以上版本的 toolkit 包。

这两句话合起来给出了「**能升级到什么程度**」的边界：

- **软边界（一年 ABI 承诺）**：调用方一年内升 ATB 安全。
- **硬边界（8.5 toolkit 配套）**：因为 CANN 出包目录结构有调整，ATB 8.5/主线**强制**要求 toolkit ≥ 8.5。这条覆盖了上面 `>=8.2` 的下限——对于 8.5 这一代 ATB，实际生效的是更严的「≥8.5」。

**③ ABI 自动探测** —— [scripts/build.sh:L447-L458](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/scripts/build.sh#L447-L458)

```bash
res=$(python3 -c "import torch" &> /dev/null || echo "torch_not_exist")
if [ "$res" == "torch_not_exist" ]; then
    echo "Warning: Torch is not installed!"
    [[ "$USE_CXX11_ABI" == "" ]] && USE_CXX11_ABI=ON
fi
if [ "$USE_CXX11_ABI" == "" ]; then
    if [ $(python3 -c 'import torch; print(torch.compiled_with_cxx11_abi())') == "True" ]; then
        USE_CXX11_ABI=ON
    else
        USE_CXX11_ABI=OFF
    fi
fi
```

这段是「版本兼容」落到构建期的关键：ATB 的 ABI 默认**跟随已安装的 PyTorch**。若未装 torch，默认 `cxx_abi_1`；若装了 torch，则以 `torch.compiled_with_cxx11_abi()` 为准。这就是「配套依赖兼容」在 ABI 维度的具体实现（详见 [u1-l3](u1-l3-build-system.md)）。

**④ 构建溯源信息生成** —— [scripts/build.sh:L423-L433](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/scripts/build.sh#L423-L433)

```bash
function generate_atb_version_info()
{
    branch=$(git symbolic-ref -q --short HEAD || git describe --tags --exact-match 2> /dev/null || echo $branch)
    commit_id=$(git rev-parse HEAD)
    touch $OUTPUT_DIR/version.info
    cat>$OUTPUT_DIR/version.info<<EOF
    Platform : ${ARCH}
    branch : ${branch}
    commit id : ${commit_id}
EOF
}
```

这就是前文强调的「另一个 `version.info`」：它写到 `output/` 下，记录**这次构建**的架构、分支、commit，用于出包后溯源「这个 `.so` 是从哪个 commit 编出来的」。与根目录的约束声明文件毫无字段重叠。

#### 4.1.4 代码实践

**实践目标**：用 `version.info` + README 判断「ATB 8.5 能否在我这台装了 CANN 8.2 toolkit 的机器上编译」。

**操作步骤**：

1. 打开 `version.info`，读出 `Version` 与三个 `required_package_*` 的下限。
2. 在你的机器上查 toolkit 版本（`cat /usr/local/Ascend/ascend-toolkit/latest/version.cfg` 或 `npu-smi info`，待本地验证具体路径）。
3. 对照 README 的「8.5 必须匹配 8.5+ toolkit」硬约束，给出结论。

**需要观察的现象 / 预期结果**：

- 若 toolkit = 8.2：虽然满足 `version.info` 的 `>=8.2` 下限，但**不满足** README 的 8.5 硬约束 → **不能编译**，需升级 toolkit 到 ≥8.5。
- 若 toolkit = 8.5：满足两条 → 可以编译（前提是 NNAL、ops 包也配套，见 4.3）。

> 待本地验证：toolkit 版本查看命令在不同安装方式下路径不同，请以你机器实际为准。

#### 4.1.5 小练习与答案

**练习 1**：`version.info` 里 `required_package_toolkit_version>="8.2"` 写的是 `>=8.2`，但 README 又说 8.5 必须用 8.5+ toolkit，两者矛盾吗？以哪个为准？

> **答案**：不矛盾，是「下限」与「实际生效约束」的关系。`version.info` 声明的是 ATB 这一代通用的最低下限（8.2），README 的「8.5 配套」是因 CANN 出包目录调整而叠加在 8.5 这一代 ATB 上的**更严硬约束**。对于 ATB 8.5，实际生效的是更严的「≥8.5」，二者取交集。

**练习 2**：仓库根的 `version.info` 与 `output/version.info` 有何区别？哪个是人写的、哪个是机器生成的？

> **答案**：根 `version.info` 是人写的版本与依赖约束声明（`Version=...`、`required_package_*`）；`output/version.info` 由 `build.sh` 的 `generate_atb_version_info` 在构建时生成，记录 Platform/branch/commit id 溯源信息。前者面向打包/依赖校验，后者面向构建溯源。

---

### 4.2 贡献流程：从 fork 到 PR 合入

#### 4.2.1 概念说明

ATB 是 CANN 开源项目的一部分，采用典型的 **fork-based 贡献模型**：你没有权限直接 push 到官方仓库，必须先把仓库 fork 到自己账号，在 fork 里改代码，再发起 Pull Request（PR）请求官方合入。整个流程由三类「守门人」把关：

- **CLA（法律守门人）**：贡献前必须签 Contributor License Agreement，否则 PR 会被打上 `cann-cla/no` 红标，无法合入。
- **pre-commit + CI 门禁（质量守门人）**：代码风格、拼写、安全、编译全部要过。
- **Committer（人工守门人）**：CI 通过后，由官方 committer 做代码评审（code review），至少需要一位「非提交者本人」的 committer 同意（`/lgtm`、`/approve`）才能合入。

贡献类别在 `contributing.md` 里分了五类，每一类都对应一种 Issue 类型与处理路径。

#### 4.2.2 核心流程

一次完整贡献的生命周期如下（命令均来自 `gitcode-workflow.md`，可复现）：

```
签署 CLA ──> fork 仓库 ──> clone 到本地 ──> 建 feature 分支
   │                                              │
   │                                              v
   │                              本地开发 + 本地构建测试
   │                                              │
   │                                              v
   │                              pre-commit 自检（风格/拼写/安全）
   │                                              │
   │                                              v
   │                              commit（带版权头）──> push 到 fork
   │                                              │
   │                                              v
   │                              在 GitCode 建 PR（源=fork分支, 目标=master）
   │                                              │
   │                                              v
   │              评论 /compile 触发 CI 门禁 ──> 通过 ci-pipeline-passed
   │                                              │
   │                                              v
   │              Committer 评审 ──> /lgtm /approve ──> 合入 master
```

**关键规则与陷阱**：

1. **同步上游用 `fetch` + `rebase`，不要用 `git pull`**：`pull` 会产生混乱的 merge 历史，社区明确要求用 `git fetch upstream && git rebase upstream/master`。
2. **推 fork 用 `git push -f`**：因为本地经常 `rebase`/`amend`，远端历史会变，需要强推到自己的 fork（**只对 fork 分支强推，绝不对 upstream 强推**）。
3. **新文件必须带版权头**：`.cpp/.h` 用块注释，`.py/.sh` 用行注释，版权所有者与年份要填对。
4. **CLA 以 commit 邮箱为凭证**：检查的是 `git log --pretty=fuller` 里的 committer 邮箱，不是 GitCode 账号邮箱，二者不一致是 CLA 红标的头号原因。
5. **CI 没触发就评论 `/compile`**：webhook 偶发丢失或仓库刚建工程未就绪时，手动评论 `/compile` 重触发。

#### 4.2.3 源码精读

**① 必签 CLA** —— [docs/contributing.md:L5-L14](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/contributing.md#L5-L14)

贡献前必须在四种 CLA（Corporate / Corporate Contributor / Individual / Enterprise Admin）里选一种签署。这是法律前置条件，不签则后续一切流程走不通。

**② 五类贡献与对应 Issue 类型** —— [docs/contributing.md:L27-L63](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/contributing.md#L27-L63)

| 贡献类别 | Issue 类型 | 说明 |
| --- | --- | --- |
| Operator Bug Fixes（算子 Bug 修复） | `Bug-Report` | 先建 issue 反馈，再 `/assign` 认领 |
| Operator Optimizations（算子优化） | `Requirement` | 描述优化想法 |
| Contributing New Operators（新算子） | `Requirement` | 与 Ascend 团队讨论，分配 `contrib` 目录 |
| Documentation Corrections（文档修正） | `Documentation` | 指出文档错误 |
| Resolving Others' Issues（帮别人解题） | 评论协助 | 需要改代码则 `/assign` 自己 |

注意**新算子**比较特殊：需要先和 Ascend 团队沟通，由团队分配一个合适的 `contrib` 目录（即放到哪个算子分类下），不是随便找个文件夹塞进去。

**③ 认领 Issue** —— [docs/contributing.md:L65-L83](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/contributing.md#L65-L83)

在 issue 评论区输入 `/assign` 或 `/assign @yourself` 即可把 issue 分配给自己，bot 会把你的名字写进 assignee 列表。

**④ 新文件版权头模板** —— [docs/contributing.md:L97-L122](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/contributing.md#L97-L122)

` .cpp/.cc/.h` 文件头：

```cpp
/**
 * Copyright (c) [Name of the copyright owner]. 2025. All rights reserved.
 * This program is free software, you can redistribute it and/or modify it under the terms and conditions of
 * CANN Open Software License Agreement Version 2.0 (the "License").
 * ...
 */
```

`.py/.sh` 文件头：

```python
# Copyright (c) [Name of the copyright owner]. 2025. All rights reserved.
# This program is free software ...
# ================================================================================================================
```

要点：个人贡献填自己名字，代表雇主贡献填雇主名字；`2025` 改成你实际创建/修改文件的年份。许可协议是 **CANN Open Software License Agreement Version 2.0**（见 [LICENSE:L1-L4](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/LICENSE#L1-L4)）。

**⑤ fork → clone → 分支 → 同步** —— [docs/contributors/gitcode-workflow.md:L90-L120](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/contributors/gitcode-workflow.md#L90-L120)

```shell
# 建本地 feature 分支
git checkout -b myfeature

# 保持与 master 同步（推荐方式，勿用 git pull）
git fetch upstream
git rebase upstream/master
```

**⑥ commit + push + 建 PR** —— [docs/contributors/gitcode-workflow.md:L122-L149](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/contributors/gitcode-workflow.md#L122-L149)

```shell
git add .
git commit -m "提交内容描述"
git push -f origin myfeature     # 强推到自己的 fork
```

然后到 `https://gitcode.com/$user/ascend-transformer-boost` 页面点 `+Pull Request`，确认源分支（你的 fork 分支）与目标分支（`master`）。

**⑦ 触发门禁与评审** —— [docs/contributors/gitcode-workflow.md:L161-L179](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/contributors/gitcode-workflow.md#L161-L179)

PR 提交后在评论区输入 `/compile` 触发门禁；页面显示「CI 任务执行成功」且右上角标签为 `ci-pipeline-passed` 即通过，之后 PR 会被分配给 committer 评审。

**⑧ pre-commit 门禁配置** —— [.pre-commit-config.yaml:L1-L22](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/.pre-commit-config.yaml#L1-L22)

```yaml
minimum_pre_commit_version: 4.0.0
exclude: ^LICENSES/|\.(html|csv|svg)$
repos:
  - repo: https://gitcode.com/pre-commit/pre-commit-hooks
    rev: v4.6.0
    hooks:
      - id: trailing-whitespace      # 去行尾空格
      - id: end-of-file-fixer        # 文件末尾补换行
      - id: check-yaml               # YAML 语法
      - id: check-added-large-files  # 防误传大文件
      - id: check-merge-conflict     # 冲突标记
      - id: detect-private-key       # 防泄密私钥
      - id: check-json               # JSON 语法
```

这套配置同时服务**本地提交前**与**CI 门禁**两类场景。除基础 hooks 外，还挂了：`ruff`（Python lint/format）、`codespell`+`typos`（拼写，已为 CANN/NNAL/ascend 等专有词加白名单）、`pylint`（Python 质量）、`bandit`（Python 安全）、`clang-format`（C++ 格式化，v18.1.8，配套根目录 `.clang-format`）。所有 hook 仓库都换成了 **GitCode 镜像**以规避国内访问 GitHub 受限（详见 [docs/pre-commit配置指导书.md](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/pre-commit配置指导书.md#L1-L25)）。

> 配套阅读：[docs/contributors/infra-command.md](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/contributors/infra-command.md) 列出了评论区支持的全部命令（`/compile`、`/lgtm`、`/approve`、`/check-cla` 等）。

#### 4.2.4 代码实践

**实践目标**：在本地启用 pre-commit，对一个新算子的 `.cpp`/`.py` 文件做提交前自检，提前把门禁问题消灭在本地。

**操作步骤**（待本地验证具体版本号）：

1. 安装 pre-commit：`pip install pre-commit`（需 ≥4.0.0）。
2. 在仓库根目录执行 `pre-commit install`，把钩子挂到 `.git/hooks/pre-commit`。
3. 手动跑一次全量检查：`pre-commit run --all-files`。
4. 故意制造一个问题（如在某 `.py` 末尾不加换行、或留行尾空格），再次 `git commit`，观察钩子如何拦截并自动修复。

**需要观察的现象 / 预期结果**：

- `end-of-file-fixer` 会自动给无尾换行的文件补换行；`trailing-whitespace` 会清除行尾空格；修复后 pre-commit 会**让本次提交失败**，需要你 `git add` 改动后重新提交。
- C++ 文件会被 `clang-format -i` 就地格式化；Python 文件会被 `ruff-format` 格式化。

> 提示：本地 pre-commit 通过，基本等于过了 CI 门禁里的代码风格部分，能大幅减少 PR 反复修改的来回。

#### 4.2.5 小练习与答案

**练习 1**：为什么社区要求同步上游时用 `git fetch upstream && git rebase upstream/master`，而不是 `git pull`？

> **答案**：`git pull` 默认产生 merge commit，会让提交历史出现大量「Merge branch master」节点，难读难审；`fetch + rebase` 把你的提交「重放」到最新 master 之上，保持线性历史，便于评审。社区在 `gitcode-workflow.md` 明确禁止用 `pull` 替代。

**练习 2**：我提交 PR 后被打上 `cann-cla/no` 红标，但我明明签了 CLA，可能是什么原因？

> **答案**：CLA 检查以 **commit 里的 committer 邮箱**为凭证（`git log --pretty=fuller` 可查），不是 GitCode 账号邮箱。若二者不一致就会误判为未签。解决：要么把 GitCode 提交邮箱改成 commit 邮箱并重新签 CLA，要么用 `git config --global user.email` 把 commit 邮箱改成已签 CLA 的邮箱，然后评论 `/check-cla` 重触发。详见 4.3 与 [infra-faqs.md](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/contributors/infra-faqs.md#L3-L36)。

---

### 4.3 常见问题 FAQ：编译安装排错

#### 4.3.1 概念说明

ATB 的 FAQ 文档分两层：

- **编译安装 FAQ**（`docs/faq.md` / `docs/常见问题与回答.md`）：解决「`build.sh` 跑不起来」「`.so` 找不到」这类环境问题。
- **PR 基础设施 FAQ**（`docs/contributors/infra-faqs.md`）：解决「PR 标签异常」「CI 没触发」这类贡献流程问题。

本模块聚焦最高频的编译安装类问题。它们大多根因相同：**CANN 软件栈三类包（toolkit / kernels-ops / nnal）没有配套安装，或 ABI 没对齐**。

#### 4.3.2 核心流程

最高频报错 `libtbe_adapter.so does not exist` 的排查决策树：

```
报错 libtbe_adapter.so does not exist
        │
        v
是否已安装 nnal 软件包？
   ├─ 否 ──> 安装 Ascend-cann-nnal_{version}.run（版本须与 toolkit/kernels 一致）
   │              │
   │              v
   │     设置 ATB_BUILD_DEPENDENCY_PATH 指向 nnal 的 cxx_abi_X 目录
   │
   └─ 是 ──> ATB_BUILD_DEPENDENCY_PATH 是否正确？
              ├─ 否 ──> 重新设置，且 cxx_abi_X 须与编译 ABI 一致
              └─ 是 ──> 是否切换过 ABI？── 是 ──> bash scripts/build.sh --clean-first 全量重编
```

**三个 ABI 确定规则**（来自 `常见问题与回答.md`）：

1. 先判断是否装了 PyTorch：`python3 -c "import torch; print(torch.__version__)"`，报错即未装。
2. **未装 PyTorch**：用 `cxx_abi_1`。
3. **已装 PyTorch**：跑 `python3 -c "import torch; print(1 if torch.compiled_with_cxx11_abi() else 0)"`，输出 `1` 用 `cxx_abi_1`，输出 `0` 用 `cxx_abi_0`。

PR 基础设施类高频问题（来自 `infra-faqs.md`）：

| 现象 | 根因 | 处理 |
| --- | --- | --- |
| `cann-cla/no` 红标 | commit 邮箱与 CLA 签署邮箱不一致 | 改邮箱或重签，再评论 `/check-cla` |
| fork 失败「同名仓库」 | 个人账号下已有同名仓库 | 改已有仓库名/路径后再 fork |
| PR 后 CI 没触发 | webhook 丢失或工程未就绪 | 评论 `/compile` 重触发；刚建的仓库稍等 |
| 想直接 push 到 master | —— | 不允许，开发者只能 fork + PR |

#### 4.3.3 源码精读

**① libtbe_adapter.so 缺失与 NNAL 安装** —— [docs/faq.md:L4-L30](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/faq.md#L4-L30)

> When `libtbe_adapter.so does not exist` pops up, you can install the NNAL software package and set `ATB_BUILD_DEPENDENCY_PATH` correctly to solve this problem. … The NNAL software package version must be the same as that of the toolkit and kernels software packages.

核心三步：装 NNAL run 包 → 设置 `ATB_BUILD_DEPENDENCY_PATH` → 指向正确的 `cxx_abi_X` 子目录。run 包安装是标准三件套：`chmod +x` → `--check` → `--install`。

**② 中文版补充的 ABI 确定细节** —— [docs/常见问题与回答.md:L4-L33](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/常见问题与回答.md#L4-L33)

中文 FAQ 比 `faq.md` 多了两条关键信息：

- **不设置 `ATB_BUILD_DEPENDENCY_PATH` 的前提**：仅当 nnal 装在默认位置（`/usr/local/Ascend/nnal/...`）时，脚本会按检测到的 ABI 自动用默认路径，无需手动指定。
- **手动设置时的 ABI 判定**：给出了上面「三个 ABI 确定规则」的完整命令。

```sh
# 已装 PyTorch 时判定 ABI
python3 -c "import torch; print(1 if torch.compiled_with_cxx11_abi() else 0)"
```

**③ CLA 凭证是 commit 邮箱** —— [docs/contributors/infra-faqs.md:L3-L36](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/contributors/infra-faqs.md#L3-L36)

> CLA 检查是使用 commit 信息中的 committer 邮箱作为检查凭证的。该邮箱可以通过 `git log --pretty=fuller` 查询到。

文档还用一张表覆盖了「commit 邮箱与 GitCode 邮箱一致/不一致」两种场景下的处理方案，是排查 `cann-cla/no` 的权威依据。

**④ CI 未触发的两种原因** —— [docs/contributors/infra-faqs.md:L63-L69](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/contributors/infra-faqs.md#L63-L69)

- 网络或调度原因导致 webhook 丢失 → 评论 `/compile` 重触发。
- 仓库刚建、jenkins 工程未就绪 → 稍等系统自动建工程。

#### 4.3.4 代码实践

**实践目标**：在干净环境复现并解决 `libtbe_adapter.so does not exist`，掌握 NNAL + ABI 配置的标准排错路径。

**操作步骤**：

1. 故意不设置 `ATB_BUILD_DEPENDENCY_PATH`（或设错），运行 `bash scripts/build.sh`，观察报错信息。
2. 按 `常见问题与回答.md` 的规则，先判断是否装 PyTorch，再用 `torch.compiled_with_cxx11_abi()` 确定 `cxx_abi_X`。
3. 设置环境变量：

   ```sh
   export ATB_BUILD_DEPENDENCY_PATH={nnal install path}/nnal/atb/latest/atb/cxx_abi_{cxx_abi_version}
   ```

4. 重新编译；若此前用别的 ABI 编过，必须 `bash scripts/build.sh --clean-first`。

**需要观察的现象 / 预期结果**：

- 步骤 1 报 `libtbe_adapter.so does not exist`。
- 步骤 3-4 后编译继续推进（若仍有问题，多半是 toolkit 版本不满足 8.5 配套约束，回到 4.1 检查）。

> 待本地验证：是否复现报错取决于你机器当前状态；本实践的目的是走通「判定 ABI → 设路径 → clean-first 重编」这条标准排错链。

#### 4.3.5 小练习与答案

**练习 1**：一台机器没装 PyTorch，要手动设置 `ATB_BUILD_DEPENDENCY_PATH`，应该用 `cxx_abi_0` 还是 `cxx_abi_1`？

> **答案**：用 `cxx_abi_1`。`常见问题与回答.md` 明确：未装 PyTorch 时使用 `cxx_abi_1`。这也是 `build.sh` 在检测不到 torch 时的默认值（`USE_CXX11_ABI=ON`）。

**练习 2**：我已经正确设置了 `ATB_BUILD_DEPENDENCY_PATH`，但之前用 cxx_abi_0 编过，现在切到 cxx_abi_1 重编，为什么还是链接报错？

> **答案**：CMake 缓存、已拷贝的 `libtbe_adapter.so`、MKI 第三方库三处都残留了旧 ABI 产物，直接重编会把新旧 ABI 混在一起。必须 `bash scripts/build.sh --clean-first` 清理缓存后全量重编（见 [README.md:L137-L143](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/README.md#L137-L143)）。

---

## 5. 综合实践：编写一份「新算子 PR 检查清单」

本讲的核心实践任务是把三个模块串起来：**阅读贡献指南与 `version.info`，写出一个完整的新算子 PR 检查清单（代码、测试、文档、配置）**。这份清单应该让你（和评审者）一眼看出「这次提交还差什么」。

### 实践目标

产出一个可勾选的 Markdown 检查清单，覆盖新算子从「环境就绪」到「PR 合入」全链路，融合版本兼容（4.1）、贡献流程（4.2）、FAQ 排坑（4.3）三方面知识。

### 参考答案清单

> 示例清单（结合本讲与前序 [u6-l4 交付件](u6-l4-deliverables-config.md)、[u7-l3 测试](u7-l3-test-framework.md)）

```markdown
# 新算子 MyOp PR 检查清单

## A. 环境与版本兼容（对应 4.1）
- [ ] toolkit 版本 ≥ 8.5（满足 README 8.5 硬约束，不只是 version.info 的 >=8.2）
- [ ] NNAL / kernels-ops 包版本与 toolkit 一致
- [ ] ABI 已对齐：ATB_BUILD_DEPENDENCY_PATH 的 cxx_abi_X 与 torch.compiled_with_cxx11_abi() 一致
- [ ] 切换过 ABI 则已执行 build.sh --clean-first

## B. 代码（对应 4.2 + u6-x 算子开发链路）
- [ ] Kernel 四件套：kernel.cpp(计算) + tiling(切分) + operation.cpp(MKI注册) + CMake
- [ ] 高层 Operation：实现 GetInputNum/GetOutputNum/InferShapeImpl/CreateRunner 四个纯虚钩子
- [ ] OpsRunner：拓扑固定则构造函数组 KernelGraph，随形变则重写 SetupKernelGraph
- [ ] 注册名三/四处一致：Runner 节点 opDesc == REG_OPERATION 名 == ini 段名 == g_funcMap 键名
- [ ] Param 带 rsv[N] 预留字段且默认全 0（版本闸门）

## C. 配置交付件（对应 u6-l4）
- [ ] Param 定义加入 infer_op_params.h（命名 XxxParam）
- [ ] atb_ops_info.ini 增加段落，逗号并列声明合法 dtype/format 组合
- [ ] param_to_json.cpp 增加 OpParamToJson 模板特化（投影业务字段，排除 rsv）
- [ ] op_list.yaml 登记该 Kernel 在哪些芯片编译

## D. 测试（对应 u7-l3）
- [ ] operation_funcs.cpp 增 XxxOperationCreate（字段级 contains 回填 + 枚举转型 + rsv 拷贝）并登记 g_funcMap
- [ ] 新增 CSV 功能用例（OpName 与注册名一致）
- [ ] 精度用例带 golden 参考实现；可选性能用例
- [ ] 至少一条「非 0 rsv」反例验证版本闸门，标注 ExpectedError

## E. 文档与门禁（对应 4.2）
- [ ] 新 .cpp/.h/.py/.sh 文件带 CANN License 2.0 版权头（年份/版权所有者正确）
- [ ] 本地 pre-commit run --all-files 通过（clang-format/ruff/codespell/bandit）
- [ ] 已签 CLA，且 commit 邮箱与签署邮箱一致
- [ ] PR 关联对应 Requirement Issue
- [ ] PR 评论区 /compile 触发门禁，等 ci-pipeline-passed
```

### 需要观察的现象 / 预期结果

- 把这份清单对照你正在开发的新算子逐项打勾，能立刻发现遗漏（最常漏的是 D 的「反例用例」和 C 的「op_list.yaml 登记」——后者漏登会导致运行时取不到 Kernel）。
- 评审者拿到这份清单，评审效率显著提升，因为它把 [u6](u6-l1-plugin-infra.md) 的「同名两层 + 注册名一致」、[u6-l4](u6-l4-deliverables-config.md) 的「交付件」、[u7-l3](u7-l3-test-framework.md) 的「数据驱动测试」全部显式化成了可勾选项。

## 6. 本讲小结

- **版本兼容**由「`version.info` 的版本与依赖声明 + README 的一年 ABI 承诺与 8.5 toolkit 硬约束 + rsv 版本闸门」共同保证；注意仓库根 `version.info`（约束声明）与 `output/version.info`（构建溯源）是两个不同文件。
- **配套依赖**的关键是 toolkit / kernels-ops / nnal 三类 CANN 包版本一致，且 `ATB_BUILD_DEPENDENCY_PATH` 的 `cxx_abi_X` 与编译 ABI 对齐。
- **贡献流程**是 fork-based 模型：签 CLA → fork → clone → feature 分支 → 本地 pre-commit 自检 → push fork → 建 PR → `/compile` 门禁 → committer 评审 → 合入；同步上游用 `fetch + rebase` 而非 `pull`。
- **门禁守门人**有三类：CLA（法律）、pre-commit + CI（质量，含 clang-format/ruff/codespell/bandit 等 GitCode 镜像 hook）、committer（人工，需 `/lgtm`/`/approve`）。
- **最高频 FAQ** 是 `libtbe_adapter.so does not exist`，根因是 NNAL 未装或 `ATB_BUILD_DEPENDENCY_PATH` 设错；切 ABI 必须 `--clean-first`。
- **CLA 红标**的头号原因是 commit 邮箱与签署邮箱不一致，凭证是 `git log --pretty=fuller` 里的 committer 邮箱。

## 7. 下一步学习建议

本讲是手册的总结篇，建议你用 **综合实践** 的检查清单驱动一次真实的新算子贡献，把全手册的知识闭环：

1. **动手贡献**：从 [u6-l2 自定义 Kernel 开发](u6-l2-custom-kernel.md) 起步，按本讲的清单在 `ops_customize/` 里开发一个最小自定义算子，走通 fork → PR 全流程。
2. **回看主线**：若提交中遇到执行链路问题，回看 [u3-l2 Runner 体系](u3-l2-runner-system.md) 与 [u3-l4 Kernel/MKI 框架](u3-l4-kernel-mki.md)；遇到测试问题回看 [u7-l3 测试框架](u7-l3-test-framework.md)。
3. **持续阅读源码**：把 `docs/contributors/` 下的 `infra-command.md`（评论命令一览）、`issue-submit.md`（Issue 提交指南）读完，它们是贡献流程的操作手册。
4. **关注版本演进**：升级 ATB 时，先比对根 `version.info` 的 `Version` 与依赖下限，再确认 README 是否有新的配套硬约束，最后用 rsv 闸门理解 Param 的跨版本扩展方式。

至此，你已经从「认识 ATB」走到「能给 ATB 贡献一个完整算子」，完成了手册的全部学习路径。
