# 目录结构与模块全景

## 1. 本讲目标

前两篇（u1-l1、u1-l2）我们已经建立了「Op(L2)/Kernel(L1) 双层分离 + Spec-driven」的心智模型，并让项目在本机跑了起来。本篇退后一步看**全景**——把仓库里的目录、背后的 M1–M8 模块、以及模块之间传递的「数据契约」三件事对齐起来。读完本讲，你应当能够：

1. 说出 `tileops/`、`workloads/`、`tests/`、`benchmarks/`、`docs/`、`scripts/` 等顶层目录各自的职责，并知道哪些是「库代码」、哪些是「周边设施」。
2. 把架构文档里的 M1–M8 八个模块**一一映射**到具体目录与关键产物，并能复述四条数据流（Op Delivery / Perf Tuning / HW Calibration / Publish）各自的完成度。
3. 理解模块之间靠**数据契约（data contracts）**通信，记住本讲三条主线契约——`signature` / `workloads` / `roofline`——以及 `raw time` 这一计时契约，并会用 `load_manifest()` / `load_workloads()` 程序化核对。

本讲覆盖的最小模块：**目录布局**、**M1–M8 模块参考表**、**Data Contracts**。

## 2. 前置知识

- **库（library）vs 应用（application）**：应用有一个「入口」（`main()`、Web 服务、CLI 命令）；库没有自己的入口，它的产物是**被别人 import 的模块**。TileOPs 是库——`architecture.md` 第一段就强调「the runtime interface remains plain Python imports」。所以本篇讲的「目录」是按**职责**（规约 / 实现 / 测试 / 基准 / 性能）切分的，而不是按「请求处理流程」切分的。
- **数据契约（data contract）**：两个模块之间约定好的「数据格式」。例如 M4（基准）必须按某种格式把每个 workload 的实测耗时交给 M5（roofline）去算效率——这个格式约定就是契约。契约的好处是：**一方换实现、另一方不受影响**，只要契约不变。
- **family（家族）**：一组语义相关的算子归为一个 family（如 `elementwise`、`attention`、`moe`）。manifest 按 family 组织文件，但 family 与文件**不一定 1:1**——大 family 会被拆成多个文件（shard）。

> 本讲承接 u1-l1 的「Spec-driven：manifest 是唯一真相来源」与 u1-l2 的「Op 可被 import 调用」这两条认知，不再重复它们本身，而是把它们放进更大的目录与模块图景里去定位。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲怎么用 |
| --- | --- | --- |
| `docs/design/architecture.md` | 架构总纲。定义 M1–M8 八模块、四条数据流、模块参考表、数据契约表、目录布局说明。 | 本讲最主要的事实来源：模块表、契约表、目录说明都在这里。 |
| `README.md` | 项目门面。Overview、Architecture（双层）、Key Properties、Documentation。 | 用来定位「docs/ 在哪、API 参考发到哪」等面向用户的目录指引。 |
| `tileops/manifest/__init__.py` | manifest 的程序化访问入口：`load_manifest()`、`load_workloads()`、`manifest_files()`。 | 用它核对目录布局里「manifest 有多少文件、多少算子」，并演示数据契约的程序化读取。 |
| `workloads/workload_base.py` | 共享输入生成层的基类。 | 理解 `workloads/` 为什么「不算一个模块」、它的边界在哪。 |

## 4. 核心概念与源码讲解

### 4.1 顶层目录布局

#### 4.1.1 概念说明

TileOPs 是一个 GPU 算子**库**，目录按「职责」切分。顶层大致分三圈：

- **核心库（`tileops/`）**：会被 `import` 的代码。又分五个子目录：
  - `tileops/manifest/` —— 规约（spec），算子接口的**唯一真相来源**。
  - `tileops/ops/` —— Op 层（L2，主机侧调度）。
  - `tileops/kernels/` —— Kernel 层（L1，TileLang GPU 实现）。
  - `tileops/perf/` —— 性能模型公式 + GPU profile（roofline 与硬件标定）。
  - `tileops/testing/`、`tileops/trace/`、`tileops/utils/` —— 辅助工具（测试工具函数、trace、杂项 utils）。
- **质量与性能设施（`workloads/`、`tests/`、`benchmarks/`）**：不算被 import 的库代码，而是围绕算子的「输入生成 / 正确性 / 性能」三件套。
- **文档与工具（`docs/`、`scripts/`、`.claude/`、`.github/`）**：设计文档、CI/统计脚本、agent 技能与规则、GitHub 工作流。

`architecture.md` 的「Directory Structure」小节明确说：**本文档不跟踪文件清单，请直接查目录树本身**——也就是说，目录会随算子增长而变化，权威是 `git ls-files` 或目录本身，而不是某张静态表。这张表只给出**稳定的职责映射**。

#### 4.1.2 核心流程

面对一个陌生算子，推荐这样按目录「找东西」：

1. **先查规约**：到 `tileops/manifest/` 找它所属 family 的 YAML，看 `signature` / `workloads` / `roofline`。
2. **再看实现**：到 `tileops/ops/` 找 Op 类（L2），到 `tileops/kernels/` 找 Kernel（L1）。
3. **核对正确性**：到 `tests/ops/` 找同名测试，看 `ref_program` 与容差。
4. **看性能**：到 `benchmarks/ops/` 找同名基准，看它对照哪些 baseline。
5. **读设计**：到 `docs/design/` 读相关设计文档（如想加新算子就读 `ops-design.md`）。

这条「找东西」的路径，本质就是数据在各模块间流动的顺序（详见 4.2 的模块表与 4.3 的契约表）。

#### 4.1.3 源码精读

`architecture.md` 开篇点明「库」属性：

[docs/design/architecture.md:1-3](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/docs/design/architecture.md#L1-L3) —— 「spec-driven GPU operator platform … but the runtime interface remains plain Python imports」。这句话解释了为什么没有 `main.py` 或服务入口：运行时接口就是普通 import。

「Directory Structure」小节的权威说明：

[docs/design/architecture.md:133-135](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/docs/design/architecture.md#L133-L135) —— 顶层布局概述：`tileops/`（manifest、kernels、ops、perf）、`workloads/`、`tests/`、`benchmarks/`、`docs/`、`scripts/`；并声明「This doc does not track the file inventory — consult the tree itself」。

README 指明设计与 API 文档的归属：

[README.md:89-91](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/README.md#L89-L91) —— 设计文档在 `docs/`，完整 API 参考与性能表发布到 `TileOPs.github.io`。这说明 `docs/` 存的是**设计文档**（人写的），API 参考是**自动生成**的（见下文 4.2 的 Agent Production Loop / Documentation System）。

基于上面的事实，本讲整理出这份**稳定的职责映射表**（产物列对应 4.2 的 M 模块）：

| 顶层目录 | 职责 | 关键子目录/产物 | 对应模块 |
| --- | --- | --- | --- |
| `tileops/manifest/` | 算子规约（唯一真相来源） | 各 family 的 `.yaml`、`shape_rules.py` | M1 |
| `tileops/ops/` | Op 层（L2 主机侧） | `op_base.py`、各 family 子包 | M2 |
| `tileops/kernels/` | Kernel 层（L1 TileLang） | `kernel_base.py`、各 family 子包 | M2 |
| `tileops/perf/` | roofline 公式 + GPU profile | `formulas.py`、`profile.py`、`profiles/*.yaml` | M5 / M6 |
| `tileops/testing/` `tileops/trace/` `tileops/utils/` | 辅助工具 | 测试工具、trace、杂项 utils | 支撑性，非核心 M 模块 |
| `workloads/` | 共享输入生成 + parametrize 装饰器 | `workload_base.py`、各 family 输入 | 共享层（**不算模块**） |
| `tests/` | 对照 PyTorch 的数值正确性 | `test_base.py`、`ops/test_*.py` | M3 |
| `benchmarks/` | 基准执行时间、驱动优化循环 | `benchmark_base.py`、`ops/bench_*.py` | M4 |
| `docs/` | 设计文档 + perf 表 | `design/*.md`、`perf/` | M8 |
| `scripts/` | 工具脚本 | `validate_manifest.py`、`manifest_stats.py`、`nightly_report.py`、`ci/` | 跨 M（校验/统计/CI） |
| `.claude/` | agent 技能、规则、领域规则、评审清单 | `skills/`、`rules/`、`domain-rules/` | agent 生产循环工具 |
| `.github/` | CI 工作流 | `workflows/*.yml` | M7 |

> 注意 `tileops/perf/` 同时承载两个模块：`profiles/` 子目录是 M6（硬件标定数据），而 `formulas.py` / `profile.py` 属于 M5（roofline 计算）。这是「目录」与「模块」并非严格 1:1 的典型例子——**目录是物理组织，模块是逻辑职责**。

#### 4.1.4 代码实践

**实践目标**：用 `git ls-files` 摸清顶层目录的真实文件分布，验证本节的职责映射表。

**操作步骤**：

```bash
# 1) 统计每个顶层目录下的已跟踪文件数
git ls-files | cut -d/ -f1 | sort | uniq -c | sort -rn

# 2) 看 tileops/ 的二级子目录
git ls-files 'tileops/*' | cut -d/ -f1-2 | sort | uniq -c

# 3) 列出 manifest 目录下的所有 yaml
git ls-files 'tileops/manifest/*' | grep -E '\.yaml$'
```

**需要观察的现象**：

- 第 1 步会看到 `tileops`、`tests`、`workloads`、`benchmarks`、`docs`、`scripts`、`.github`、`.claude` 等顶层条目，各自带一个文件计数。
- 第 2 步会看到 `tileops/manifest`、`tileops/ops`、`tileops/kernels`、`tileops/perf` 等二级目录，以及 `tileops/testing`、`tileops/trace`、`tileops/utils`。
- 第 3 步会列出约 22 个 `*.yaml`（确切数见 4.2.4 的实践）。

**预期结果**：顶层条目与上表一一对应；`tileops/` 是文件最多的核心库。具体文件数随 HEAD 变化，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `workloads/` 被称作「共享层（shared layer）」而不是一个模块（M）？

**参考答案**：因为它**同时被 M3（测试）和 M4（基准）消费**，提供的是两边都要用的「输入生成 + parametrize 装饰器」，自己不产出规约、实现或性能结论。`architecture.md` 的模块参考表里它被单列并标注「_(shared layer, not a module)_」（见 [docs/design/architecture.md:79](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/docs/design/architecture.md#L79)）。`workload_base.py` 的模块文档也强调「正确性专属逻辑（ref_program/check/tolerance）留在 tests/，不放进 workloads/」（[workloads/workload_base.py:1-7](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/workloads/workload_base.py#L1-L7)），正是为了保持它「中立可共享」。

**练习 2**：`tileops/perf/` 既装 `formulas.py` 又装 `profiles/`，这两样东西分别服务哪个模块？为什么放进同一个目录？

**参考答案**：`profiles/*.yaml` 服务 M6（HW Profile，离线标定的硬件参数），`formulas.py` / `profile.py` 服务 M5（Roofline，算硬件效率）。放进同一目录是因为它们都属于「性能/硬件」这一**物理主题**；但逻辑上分属两个模块——这正是「目录 ≠ 模块」的体现。

---

### 4.2 M1–M8 模块参考表

#### 4.2.1 概念说明

`architecture.md` 把整个平台抽象成 **8 个模块（M1–M8）**，用**四条数据流**把它们串成端到端流水线。模块是逻辑职责，目录是物理实现——4.1 的职责映射表已经把两者连起来了，本节给出模块的权威定义。

这 8 个模块里，**只有 Op Delivery 流是 done 状态**，其余三条都还是 partial（见 4.2.3 的状态表）。理解这个现状很重要：它解释了为什么仓库里有些东西（如 `tileops/perf/`）已经存在但还没完全闭环。

#### 4.2.2 核心流程

一个算子从规约到发布的「理想」流程（即 Agent Production Loop）：

1. **M1**：在 `tileops/manifest/` 写规约（signature / workloads / roofline）。
2. **M2**：写 Kernel（`tileops/kernels/`）与 Op（`tileops/ops/`），写测试，写 docstring。
3. **M3**：跑 `tests/`，对照 PyTorch 参考核对数值；失败就回 M2 改。
4. **M4**：跑 `benchmarks/`，产出每个 workload 的 raw time，喂给 M5。
5. **M5**：用 raw time + manifest roofline 公式 + GPU profile，算出 SOL 效率。
6. 效率不够 → 优化 kernel，回到第 2 步。
7. 提 PR → **M7**（CI）查正确性与性能回归 → 合并 → **M8**（docs）自动更新。

四条数据流分别是：

- 🟢 **Op Delivery**：M1 → M2 → M3 → M7（把一个算子从规约送到 CI 守门）。
- 🔵 **Perf Tuning**：M1 → M4 → M5 → M2（基准 → roofline → 反馈优化 kernel）。
- 🟠 **HW Calibration**：HW 微基准 → M6 → M5（硬件标定数据喂给 roofline）。
- 🟣 **Publish**：M2 / M7 → M8（设计文档 + 夜间基准数据 → 发布）。

#### 4.2.3 源码精读

模块总览与四流引言：

[docs/design/architecture.md:5-7](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/docs/design/architecture.md#L5-L7) —— 「The platform consists of 8 modules (M1–M8). Four data flows connect them into end-to-end pipelines.」

四流状态表（这是理解「项目当前做到哪一步」的关键）：

[docs/design/architecture.md:58-65](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/docs/design/architecture.md#L58-L65) —— Op Delivery = done；Perf Tuning = partial（M4 能产 raw time、roofline 公式与 GPU profile loader 已有，但**效率计算与优化闭环尚未打通**）；HW Calibration = partial（缺 tensor core 标定）；Publish = partial（缺 API 参考生成）。

**模块参考表**（本讲把它转写成中文，逐行配目录）：

[docs/design/architecture.md:67-79](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/docs/design/architecture.md#L67-L79) —— 模块参考表原文。

| 模块 | 职责 | 关键产物 | 本仓库目录 |
| --- | --- | --- | --- |
| **M1: Spec** | 声明算子接口、workloads、roofline 公式 | `tileops/manifest/` | `tileops/manifest/` |
| **M2: Kernel + Op** | GPU kernel 实现与用户侧 Python API | `tileops/kernels/`、`tileops/ops/` | 同左 |
| **M3: Correctness** | 对照 PyTorch 参考的数值正确性 | `tests/` | `tests/` |
| **M4: Perf Tuning** | 基准执行时间，驱动 kernel 优化循环 | `benchmarks/` | `benchmarks/` |
| **M5: Roofline** | 从 raw time + 公式 + HW profile 算硬件效率 | `tileops/perf/` | `tileops/perf/`（公式部分）|
| **M6: HW Profile** | 离线标定的 GPU 硬件参数（带宽、FLOPS）| `tileops/perf/profiles/` | `tileops/perf/profiles/` |
| **M7: CI Gate** | 每 PR 的正确性与性能回归守门 | CI pipeline | `.github/workflows/` |
| **M8: Docs** | 设计文档、API 参考、性能表 | TileOPs.github.io | `docs/`（设计文档部分）|
| Workloads _(共享层，非模块)_ | M3 与 M4 共用的输入生成 + parametrize 装饰器 | `workloads/` | `workloads/` |

Agent Production Loop 的七步原文：

[docs/design/architecture.md:109-117](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/docs/design/architecture.md#L109-L117) —— 从「读 M1 规约」到「合并 → M8 自动更新」的完整循环。

文档生成系统的「人工 vs 自动」分工（解释 M8）：

[docs/design/architecture.md:119-131](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/docs/design/architecture.md#L119-L131) —— API 参考来自代码 docstring（sphinx/mkdocs 自动生成）、性能表来自基准原始数据、支持矩阵来自 manifest workloads；设计文档则在开发期间**人工撰写**。这正解释了 README 为什么说 `docs/` 放设计文档、而 API 参考在 `TileOPs.github.io`。

#### 4.2.4 代码实践

**实践目标**：统计 `tileops/manifest/` 下每个 family YAML 文件的算子数，建立「family（文件）→ 算子数」映射，并核对总数。

**操作步骤**：

```bash
# 1) 列出所有 manifest yaml（注意 family 与文件不严格 1:1）
git ls-files 'tileops/manifest/*' | grep -E '\.yaml$'

# 2) 统计每个文件的「顶层算子键」数量。
#    manifest 顶层结构是 op_name -> entry，顶层键顶格书写、形如 Name:，
#    所以「顶格的 XxxYyy:」即为一个算子。
grep -cE '^[A-Za-z][A-Za-z0-9_]*:' tileops/manifest/*.yaml
```

**需要观察的现象 / 预期结果**：第 2 步会得到每个文件的算子计数。本讲（基于当前 HEAD）实测如下，合计 **184 个算子**：

| family 文件 | 算子数 |
| --- | ---: |
| `elementwise_binary.yaml` | 24 |
| `elementwise_unary_math.yaml` | 24 |
| `reduction.yaml` | 19 |
| `elementwise_unary_activation.yaml` | 16 |
| `attention.yaml` | 14 |
| `mamba.yaml` | 13 |
| `normalization.yaml` | 12 |
| `linear_attention.yaml` | 10 |
| `pool.yaml` | 9 |
| `moe.yaml` | 7 |
| `convolution.yaml` | 6 |
| `position_encoding.yaml` | 6 |
| `sequence_modeling.yaml` | 6 |
| `gemm.yaml` | 3 |
| `elementwise_fused_gated.yaml` | 3 |
| `attention_indexing.yaml` | 2 |
| `bmm.yaml` | 2 |
| `elementwise_generative.yaml` | 2 |
| `elementwise_multi_input.yaml` | 2 |
| `scan.yaml` | 2 |
| `quantization.yaml` | 1 |
| `regularization.yaml` | 1 |
| **合计** | **184** |

注意 **family ≠ 文件**：`elementwise` 这一个逻辑 family 被**拆成 6 个文件**（`elementwise_binary` / `elementwise_fused_gated` / `elementwise_generative` / `elementwise_multi_input` / `elementwise_unary_activation` / `elementwise_unary_math`，合计 24+3+2+2+16+24 = **71 个算子**）；`attention` 相关也分 `attention.yaml` 与 `attention_indexing.yaml` 两个文件。manifest 加载器（见 4.3.3）的文档明确说「most families use a single file, but large families … are sharded across multiple files」。

> 待本地验证：上述计数针对当前 HEAD `9bda1ac`；算子会随开发增长，请以你本机的 `grep -cE` 结果为准，但方法与「合计=各 family 之和」不变。

#### 4.2.5 小练习与答案

**练习 1**：仓库里没有名为 `M5` 或 `roofline` 的顶层目录，那 M5 的代码到底在哪？这说明了「模块」与「目录」的什么关系？

**参考答案**：M5 的公式在 `tileops/perf/formulas.py`、profile 加载在 `tileops/perf/profile.py`，与 M6 的 `tileops/perf/profiles/` 共处 `tileops/perf/`。说明「模块」是**逻辑职责**，「目录」是**物理组织**，二者不要求一一对应——多个模块可以共用一个目录（perf 同时是 M5+M6），一个模块也可以跨多个目录（M2 跨 `ops/` 与 `kernels/`）。权威映射在 `architecture.md` 的「Key Artifact」列（[docs/design/architecture.md:67-79](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/docs/design/architecture.md#L67-L79)）。

**练习 2**：四条数据流里只有 Op Delivery 是 `done`，其余三条都是 `partial`。请用一句话解释「Perf Tuning 流 partial」具体缺什么。

**参考答案**：M4 已经能产出每个 workload 的 raw time，manifest 里的 roofline 公式和 GPU profile loader（`tileops/perf/`）也已就位，但**把 raw time 换算成 SOL 效率、并把效率反馈回 kernel 优化的闭环尚未打通**（见 [docs/design/architecture.md:63](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/docs/design/architecture.md#L63)）。这也是为什么 u1-l2 末尾点名的 `eval_roofline()` 目前主要返回 `(flops, bytes)`，完整效率评估还在路上。

---

### 4.3 Data Contracts（模块间的数据契约）

#### 4.3.1 概念说明

模块之间不靠「直接调用对方内部」通信，而是靠**数据契约**：约定好格式的一份数据，从产出一方流向消费一方。`architecture.md` 的 Data Contracts 小节给出**完整契约清单**（并强调拓扑图为了清晰做了简化，这张表才是权威）。

本讲聚焦学习目标点名的四条契约：

- **signature**（M1 → M2）：算子接口声明——输入/输出/参数的名字、dtype、形状。Op 层据此做校验与 codegen。
- **workloads**（M1 → M4，并经共享层喂给 M3）：基准负载——一组 `(shape, dtypes, label, op 参数)`，用来参数化基准（与测试）。
- **roofline**（M1 → M5）：性能公式——给定 workload 算出理论的 `(flops, bytes)`，供 M5 换算 SOL 效率。
- **raw time**（M4 → M5）：实测耗时——每个 workload 的 kernel 实际跑出来的时间。

前三条都**起源于 manifest**（M1），再次印证 u1-l1 的「manifest 是唯一真相来源」：接口、负载、性能模型全部声明在规约里，下游模块只消费、不自行发明。

#### 4.3.2 核心流程

把四条契约画到模块流上：

```
M1 (manifest)
 │  signature, workloads        ──► M2 (ops/kernels)   [契约: signature]
 │  workloads (shapes, dtypes)  ──► M4 (benchmarks)    [契约: workloads]
 │  roofline formulas           ──► M5 (perf)          [契约: roofline]
 │
M4 (benchmarks)
 └─ raw time per workload       ──► M5 (perf)          [契约: raw time]
        │
M5 = f(raw_time, roofline(flops,bytes), gpu_profile)  →  SOL efficiency
```

要点：

1. **M1 是三条契约的共同源头**：signature、workloads、roofline 都写在 `tileops/manifest/*.yaml` 里。
2. **raw time 是唯一的「实测」契约**：它来自 M4 在 GPU 上真的跑 kernel，其余三条都是**声明式**的。
3. **M5 是三条输入的汇合点**：raw time（实测）× roofline 公式（声明）× GPU profile（标定，来自 M6）→ 效率。这正是 4.2 说的 Perf Tuning 闭环需要打通的那一步。

#### 4.3.3 源码精读

Data Contracts 完整契约表：

[docs/design/architecture.md:81-96](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/docs/design/architecture.md#L81-L96) —— 完整契约清单。本讲关注的几行：

| From | To | Artifact | Format |
| --- | --- | --- | --- |
| M1 | M2 | signature, workloads | `tileops/manifest/` |
| M1 | M4 | workloads (shapes, dtypes) | `tileops/manifest/` |
| M1 | M5 | roofline formulas (flops, bytes) | `tileops/manifest/` |
| M4 | M5 | raw time per workload | JUnit XML |

注意 **M2 → M3 / M2 → M4 的契约是「Op callable / Python import」**——也就是说，正确性测试和基准都不直接碰 kernel 内部，而是 import Op、像函数一样调用它（呼应 u1-l2 讲过的 `__call__ → forward`）。这与 u1-l1 的双层分离一致：M3/M4 只看 L2 Op 的对外行为，不依赖 L1 kernel 的实现细节。

manifest 的程序化访问入口（契约的「读取端」）：

[tileops/manifest/__init__.py:1-14](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/manifest/__init__.py#L1-L14) —— 模块文档：family 分文件、加载时合并为单个 `ops` dict、**重名算子跨文件会抛 `ValueError`**。CLAUDE.md 也强调「Op names must remain unique across files — duplicates raise at load time」。

[tileops/manifest/__init__.py:27-38](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/manifest/__init__.py#L27-L38) —— `manifest_files()`：返回所有参与合并的 YAML 文件，按名排序。这就是 4.2.4 统计 family 的程序化等价物。

[tileops/manifest/__init__.py:41-61](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/manifest/__init__.py#L41-L61) —— `load_manifest()`：带 `lru_cache(maxsize=1)`（每进程只合并一次），逐文件 `yaml.safe_load` 后合并；遇到重名 `name` 抛 `ValueError`（带「already defined in <原文件>」提示）。这是「契约被消费」的主入口。

[tileops/manifest/__init__.py:64-77](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/manifest/__init__.py#L64-L77) —— `load_workloads(op_name)`：返回某算子的 workloads 列表（docstring 给了 `RMSNormFwdOp` 的真实样例：`{'x_shape': [2048, 4096], 'dtypes': ['float16','bfloat16'], 'label': 'llama-3.1-8b-prefill'}`）。这就是「workloads 契约」的读取端。

> 三个入口的分工记忆法：`manifest_files()` 看**有哪些规约文件**、`load_manifest()` 拿**全量算子字典**、`load_workloads(name)` 取**某算子的基准负载**。CLAUDE.md 明确要求「prefer `load_manifest` / `load_workloads`，永远不要自己重新实现合并」。

#### 4.3.4 代码实践

**实践目标**：用程序化入口 `load_manifest()` 核对 4.2.4 的 family 统计，并用 `load_workloads()` 亲眼看到「workloads 契约」长什么样。

**操作步骤**（需要先 `make install`，见 u1-l2）：

```python
# 示例代码（非项目原有代码，为本讲实践撰写）
from tileops.manifest import load_manifest, load_workloads, manifest_files

# 1) 有多少个 manifest 文件？多少个算子？
files = list(manifest_files())
ops = load_manifest()
print(f"manifest 文件数: {len(files)}")
print(f"算子总数: {len(ops)}")

# 2) 看 workloads 契约的真实模样（以 RMSNorm 为例）
wl = load_workloads("RMSNormFwdOp")
print(f"RMSNormFwdOp workloads 条目数: {len(wl)}")
print("第一条:", wl[0])
```

**需要观察的现象 / 预期结果**：

- `manifest 文件数` 应与 4.2.4 的 yaml 个数一致（约 22）。
- `算子总数` 应为 **184**（与 4.2.4 的 `grep -cE` 合计一致）。
- `RMSNormFwdOp` 的第一条 workloads 形如 `{'x_shape': [2048, 4096], 'dtypes': ['float16', 'bfloat16'], 'label': 'llama-3.1-8b-prefill'}`（与 [tileops/manifest/__init__.py:71-72](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/manifest/__init__.py#L71-L72) 的 docstring 一致）。

> 待本地验证：文件数与算子总数随 HEAD 变化；以你本机结果为准，但「程序化合计 == grep 合计」这一一致性必须成立。若没装好环境，可改为纯阅读型实践：直接读 `tileops/manifest/normalization.yaml` 里 `RMSNormFwdOp` 条目的 `workloads` 字段，对照本节契约表逐键解释。

#### 4.3.5 小练习与答案

**练习 1**：为什么 M2 → M3、M2 → M4 的契约格式写的是「Python import」而不是某个 YAML 或 XML？

**参考答案**：因为正确性测试和基准都是**在进程内直接调用 Op**——`import` 一个 Op 类、实例化、传 tensor 调用，拿输出与 PyTorch 参考比对或计时。这种契约是最自然的「运行时接口」，呼应 `architecture.md` 第一段的「runtime interface remains plain Python imports」。它也强化了双层分离：M3/M4 只依赖 L2 Op 的对外行为，与 L1 kernel 实现解耦。

**练习 2**：四条契约（signature / workloads / roofline / raw time）里，哪一条是「实测」的、哪三条是「声明」的？为什么这个区分重要？

**参考答案**：只有 **raw time 是实测**（M4 在 GPU 上真的跑 kernel 得到）；signature / workloads / roofline 都是**声明式**的，写在 manifest 里。这个区分重要是因为 M5 的 SOL 效率 = `理论最短时间 / 实测时间`——分子由声明式契约（roofline 给 flops/bytes、M6 给 profile）决定、分母由实测契约（raw time）决定。声明部分可人审、可被 validator 校验；实测部分则依赖 M4 的计时协议（这会是 U6 的主题）。

---

## 5. 综合实践

把本讲三个模块串成一张**全景表**：从「目录 → 模块 → 数据契约 → manifest family」四个视角统一起来。

**任务**：用下面的脚本（示例代码，非项目原有代码）生成一份「family → 文件 → 算子数 → 所属 M 模块 → 对外契约」的汇总表，并回答一个综合问题。

```python
# 示例代码（非项目原有代码，为本讲实践撰写）
from collections import Counter
from tileops.manifest import load_manifest, manifest_files

# family（这里用「文件名去扩展名」近似 family；注意 elementwise 跨多文件）
ops = load_manifest()

# 1) 按来源文件聚合算子数
per_file = Counter()
for name, entry in ops.items():
    # entry 自身不直接记来源文件；改用 manifest_files + 逐文件解析来归因
    pass

# 更直接的做法：逐文件解析顶层键
import re, pathlib
rows = []
for p in manifest_files():
    text = p.read_text(encoding="utf-8")
    names = [ln.split(":", 1)[0] for ln in text.splitlines()
             if re.match(r"^[A-Za-z][A-Za-z0-9_]*:", ln)]
    rows.append((p.name, len(names)))

rows.sort(key=lambda r: (-r[1], r[0]))
total = sum(n for _, n in rows)
for name, n in rows:
    print(f"{n:3d}  {name}")
print(f"{total:3d}  TOTAL across {len(rows)} files")
```

**操作步骤**：

1. 跑上面的脚本，得到「文件 → 算子数」表（应与 4.2.4 一致，合计 184）。
2. 把 `elementwise_*.yaml` 的 6 个文件归并成一个逻辑 family `elementwise`，验证它一个 family 就贡献了 71 个算子。
3. 对照 4.1 的职责映射表，给每个 family 文件标注它**最终被哪些 M 模块消费**（提示：所有 family 的 `signature` 都流向 M2；`workloads` 流向 M3/M4；`roofline` 流向 M5）。

**需要观察的现象 / 预期结果**：

- 脚本输出与 4.2.4 的 `grep -cE` 结果**逐文件一致**，合计 184。
- 归并后，逻辑 family 数 < 文件数（22），因为 elementwise 与 attention 各自跨多文件。
- 每个文件的契约流向都符合：`signature → M2`、`workloads → M3/M4`、`roofline → M5`。

**综合问题**：如果有人想新增一个算子，按本讲的全景图，他需要触碰哪几个目录、分别对应哪些 M 模块与数据契约？

**参考答案**（自检用）：

1. `tileops/manifest/`（**M1**）—— 先写规约，这是 signature / workloads / roofline 三条契约的源头。
2. `tileops/kernels/`（**M2 / L1**）—— 写 TileLang kernel。
3. `tileops/ops/`（**M2 / L2**）—— 写 Op 类，它消费 `signature` 契约做校验/codegen，并向 M3/M4 暴露「Python import」契约。
4. `workloads/`（**共享层**）—— 若需要新的基准输入生成，在这里加（保持它「不含正确性逻辑」的边界）。
5. `tests/ops/`（**M3**）—— 写正确性测试，消费「Op callable」契约，对照 PyTorch。
6. `benchmarks/ops/`（**M4**）—— 写基准，消费「workloads」与「Op callable」契约，产出「raw time」契约交给 M5。
7. `docs/design/`（**M8**）—— 必要时更新设计文档。
8. 提 PR 后由 `.github/`（**M7**）跑 CI 守门。

> 这正是 `architecture.md`「Agent Production Loop」七步（[docs/design/architecture.md:109-117](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/docs/design/architecture.md#L109-L117)）落到目录层面的样子——也是后续 U2–U9 各篇要逐层展开的内容。

## 6. 本讲小结

- TileOPs 是**库**不是应用，运行时接口就是普通 import（[architecture.md:1-3](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/docs/design/architecture.md#L1-L3)）；顶层目录按**职责**切分，权威文件清单是 `git ls-files` 而非静态表。
- 顶层目录与模块的稳定映射：`tileops/manifest/`→M1、`tileops/ops/`+`tileops/kernels/`→M2、`tests/`→M3、`benchmarks/`→M4、`tileops/perf/`→M5/M6、`.github/`→M7、`docs/`→M8；`workloads/` 是**共享层，不算模块**。
- **目录 ≠ 模块**：`tileops/perf/` 同时是 M5+M6，`tileops/ops/`+`kernels/` 共同构成 M2——目录是物理组织，模块是逻辑职责。
- 平台 = 8 模块 + 4 数据流；**只有 Op Delivery 流是 done**，Perf Tuning / HW Calibration / Publish 三流均为 partial（[architecture.md:58-65](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/docs/design/architecture.md#L58-L65)）。
- 模块靠**数据契约**通信；本讲四条主线契约：`signature` / `workloads` / `roofline` 都起源于 M1（声明式），`raw time` 来自 M4（实测），四者在 M5 汇合算 SOL 效率。
- manifest 共 **22 个 family YAML、184 个算子**（当前 HEAD），family 与文件不 1:1（elementwise 跨 6 文件、71 算子）；程序化读取统一走 `load_manifest()` / `load_workloads()`，重名算子在加载时抛错。

## 7. 下一步学习建议

本讲给出了「全景地图」。接下来有两个方向，按你的兴趣选择：

- **想看「目录里到底装了什么」的细节**：进入 **U4（Manifest：规约即真理）**，尤其是：
  - **u4-l1（Manifest 文件组织与加载）**——把本讲 4.3 提到的 `load_manifest()` / `load_workloads()` 讲透，并演示 family 合并与重名报错。
  - **u4-l2（Signature 与形状规约）**——展开本讲的 `signature` 契约，讲 inputs/outputs/params 与 shape_rules。
- **想看「一个算子怎么被调用」的实现**：进入 **U2（Op 层：用户侧调度器）**：
  - **u2-l1（Op 基类与生命周期）**——系统讲 `tileops/ops/op_base.py` 的 `dispatch_kernel` / `forward` / `__call__`，即本讲「M2 → M3/M4 的 Python import 契约」背后的实现。

横向可平行阅读 **u1-l4（算子的公开 API 与调用方式）**，它把 `tileops/ops/__init__.py` 的导出聚合与可调用契约讲清楚，是本讲「Op callable」契约的具体化。后续 **U5（正确性测试体系）** 与 **U6（性能基准评测）** 则分别展开本讲的 M3 与 M4 两个模块。
