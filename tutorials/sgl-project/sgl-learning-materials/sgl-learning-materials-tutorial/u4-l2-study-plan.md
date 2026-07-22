# 设计个人 SGLang 学习路线

## 1. 本讲目标

本讲是专家层（u4）的第二篇，承接前面所有讲义，把散落在 `README.md` 与 `slides/`、`blogs/` 里的资料，重新组织成一条**为你自己量身定制**的学习路线。

学完本讲，你应当能够：

1. 不再按时间或按事件读资料，而是**按目标（部署 / 优化 / 硬件 / 安全）给资料分组**，知道「我现在想解决什么问题，该去读哪几份」。
2. 为不同身份（部署工程师、性能优化工程师、硬件适配工程师、贡献者）**排出合理的阅读顺序**，而不是随便挑一份 PDF 就开始啃。
3. 厘清本仓库作为「路标」的边界——知道何时该留在仓库内读幻灯片，何时该跳到官方文档、论文、主仓库代码。
4. 产出一份**可执行的 4 周学习计划**，每周锁定 2–3 份仓库内资料并写明目标。

> 本讲几乎不含新知识，它的价值在于「**编排**」——把前 14 讲（u1–u3、u4-l1）已经讲过的资料，按读者目标重新装进四个抽屉，并给出阅读节奏。

## 2. 前置知识

本讲默认你已经建立以下认知（若某条陌生，建议先补对应讲义）：

- **本仓库是「资料聚合库」而非运行时代码库**，核心资产是 `README.md` 导航索引与 `slides/`、`blogs/` 少量文件（详见 u1-l1、u1-l2）。
- **README 是一张导航地图**，分 Announcement / Slides / Blog / Videos / Paper / Documentation 六大区段，Slides 内再用 `###` 按事件归类（详见 u1-l3）。
- **判断资料归属看链接前缀**：`slides/`、`blogs/`、`./` 开头是仓库内资产，`https://` 开头是外部链接（详见 u1-l3）。
- **同主题资料常横跨多事件、多区段，构成「资料簇」**，应整体阅读（详见 u2 各讲、u3-l1）。
- 四个目标维度的技术背景：
  - **调度与性能优化**（u2-l2）：CPU 开销隐藏、FLPM 公平调度。
  - **受限解码 / 结构化输出**（u2-l3）：压缩有限状态机、XGrammar。
  - **DeepSeek MLA**（u2-l4）：多头潜在注意力压缩 KV Cache。
  - **大规模部署**（u2-l5）：Expert Parallelism（EP）与 Prefill-Decode 分离。
  - **路由与权重热更新**（u2-l6）：Cache-Aware Router、分布式权重热更新。
  - **硬件与量化**（u3-l2）：AMD 适配、fp8/mxfp、AITER。
  - **安全**（u3-l3）：KV Cache 时序侧信道。

如果上面这些名词你都眼熟，本讲会非常顺；若有几个陌生，把它们当作「**待解锁的资料簇**」即可，本讲会告诉你去哪里解锁。

## 3. 本讲源码地图

本讲只深度依赖一个文件，但要频繁调用 `git ls-files` 与 `grep` 来「反查」它。

| 文件 / 命令 | 作用 |
| --- | --- |
| `README.md` | 唯一的导航索引；本讲所有资料分组的「真相来源」 |
| `git ls-files slides/` | 列出 `slides/` 下**全部**已提交资产（含尚未登记进 README 的） |
| `grep -ni '关键词' README.md` | 按主题反查 README，定位资料簇 |

> 小提示：`git ls-files slides/` 会显示 27 个文件，而 README 的 `## Slides` 区段只显式索引了其中一部分。例如 `slides/meetup_shenzhen.pdf` 已提交但**尚未登记进 README**（来自最近的 commit `160433e` "add shenzhen meetup slides"）。所以「想看全部资产」用 `git ls-files`，「想看官方索引」用 README——两条线互补。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 按目标分组** → **4.2 排阅读顺序** → **4.3 衔接外部资源**。三者正好对应「选料 → 排菜 → 配佐餐酒」。

---

### 4.1 按目标的资料分组（部署 / 优化 / 硬件 / 安全）

#### 4.1.1 概念说明

读者带着不同问题来到这个仓库：

- 有人问「**怎么把 SGLang 部署到几十张卡上**」——这是**部署**目标。
- 有人问「**单引擎为什么比别人快**」——这是**优化**目标。
- 有人问「**怎么跑在 AMD 卡上、要不要量化**」——这是**硬件**目标。
- 有人问「**共享 KV Cache 会不会泄露数据**」——这是**安全**目标。

如果按 README 的**时间顺序**或**事件顺序**读，你会在这四个目标之间反复横跳，效率很低。正确做法是把全部资料**按目标重新分进四个抽屉**，再按需取用。这正是 u1-l3「按主题反查」技能在整本手册尺度上的放大版。

#### 4.1.2 核心流程

把一份资料归入某个目标抽屉，只需三步：

```
1. 用 grep -ni '关键词' README.md 找到该资料在 README 的行号；
2. 读标题判断它回答哪类问题（部署/优化/硬件/安全）；
3. 把「文件名 + README 行号」记进对应目标组的清单。
```

注意：一份资料可能**同时属于多个组**。例如 MLA 既是「优化」（压缩 KV Cache 提速），又是「硬件/部署」的前置（决定显存能否放下）；EP 既是「部署」（多卡铺专家），又是「优化」（追平官方吞吐）。**多重归属是正常的**，正是「资料簇」的特征——同主题在不同视角下复用。

#### 4.1.3 源码精读：四个目标抽屉

下面四张表是本讲的核心交付物。每行给出：仓库内文件名 → README 锚点行号 → 一句话作用。所有行号均对齐当前 HEAD。

**(A) 优化抽屉——「单引擎如何更快」**

| 文件名 | README 锚点 | 作用 |
| --- | --- | --- |
| `slides/lmsys_1st_meetup_sglang.pdf` | [README.md:76](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L76) | 概览 + CPU Overhead Hiding，建立全局认知 |
| `slides/sglang-FLPM.pdf` | [README.md:96](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L96) | FLPM 公平调度算法（请求间公平与效率） |
| `slides/SGLang-Performance-Optimization-YinengZhang.pdf` | [README.md:72](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L72) | GPU MODE 实操：profile→定位瓶颈→优化→复测 |
| `slides/lmsys_1st_meetup_constrained_decoding.pdf` | [README.md:78](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L78) | 受限解码 + 压缩有限状态机 |
| `slides/lmsys_1st_meetup_xgrammar.pdf` | [README.md:84](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L84) | XGrammar 结构化生成引擎 |
| `slides/lmsys_1st_meetup_deepseek_mla.pdf` | [README.md:80](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L80) | DeepSeek MLA 原理 |
| `slides/sglang_deepseek_model_optimizations.pdf` | [README.md:64](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L64) | DeepSeek 模型专项优化（v0.3 的 7× 提速） |
| `slides/sglang_v0_2.pdf` | [README.md:110](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L110) | 最早的本地版本幻灯片，了解起点 |
| `blogs/Efficient LLM Deployment and Serving.md` | [README.md:86](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L86) | 唯一本地长博客，串联 SGLang/XGrammar/FlashInfer/MLC-LLM |

**(B) 部署抽屉——「如何把服务铺到集群 / 多副本」**

| 文件名 | README 锚点 | 作用 |
| --- | --- | --- |
| `slides/sglang_pytorch_china_2025.pdf` | [README.md:9](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L9) | 大规模 EP + PD 分离，96×H100，成本 $0.20/1M tokens |
| `slides/amd_meetup_sglang_ep.pdf` | [README.md:42](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L42) | AMD meetup：新兴 LLM 大规模部署 |
| `slides/sglang-router.pdf` | [README.md:62](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L62) | Cache-Aware Load Balancer（Hyperbolic meetup 版） |
| `slides/sglang_router.pdf` | [README.md:100](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L100) | SGLang Router（biweekly 版，注意下划线/连字符之差） |
| `slides/update-weights-from-distributed.pdf` | [README.md:98](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L98) | 分布式权重热更新（RLHF rollout 关键） |
| `slides/amd_meetup_sglang_roadmap.pdf` | [README.md:40](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L40) | roadmap，了解部署能力演进方向 |

> 部署抽屉里 `[2025-4-22] Optimizing Large Scale RLHF with SGLang`（[README.md:94](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L94)）是 gamma 外链，配合权重热更新理解大规模 RLHF。

**(C) 硬件抽屉——「如何跑在 AMD/NVIDIA 上、如何量化」**

| 文件名 | README 锚点 | 作用 |
| --- | --- | --- |
| `slides/sglang-fp8-mxfp-quantizations.pdf` | [README.md:102](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L102) | fp8 / mxfp 量化方案 |
| `slides/amd_meetup_aiter_mori.pdf` | [README.md:48](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L48) | AITER / MoRI：AMD 推理内核库（对标 NVIDIA 侧 FlashInfer） |
| `slides/amd_meetup_sglang_roadmap.pdf` | [README.md:40](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L40) | AMD 平台适配 roadmap（与部署抽屉共用） |
| `slides/amd_dev_day_v2.pdf` | [README.md:90](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L90) | AMD Advancing AI 2024：SGLang 高效推理 |
| `slides/cuda_tech_briefing_at_nvidia_gtc_2025.pdf` | [README.md:56](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L56) | NVIDIA GTC 2025 CUDA 技术简报 |

**(D) 安全抽屉——「共享 KV Cache 有什么风险」**

| 文件名 | README 锚点 | 作用 |
| --- | --- | --- |
| `slides/Possible_Timing_Side_Channel_Of_KV_Cache.pdf` | [README.md:100](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L100) | KV Cache 时序侧信道可行性探讨 |

安全抽屉最轻，但它与「优化」「部署」抽屉有**张力**：RadixAttention 前缀复用和 Cache-Aware Router 带来的吞吐红利，正是侧信道的根源。粗略地，命中与未命中的时延差约为

\[
\Delta t \;\approx\; c \cdot n
\]

其中 \(n\) 是被复用前缀的 token 数、\(c\) 是单 token 节省的时间。\(n\) 越大，时延差越明显，oracle 越好用——这正是 u3-l3 讲过的 prompt 窃取风险。所以**安全学习必须放在优化/部署之后**，先懂红利再懂风险（这一点直接决定了 4.2 的排序）。

#### 4.1.4 代码实践：用 grep 自建一个目标抽屉

1. **实践目标**：亲手把「优化抽屉」从 README 里挖出来，验证上表不是凭空写的。
2. **操作步骤**：
   ```bash
   # 在仓库根目录执行
   grep -ni 'overhead\|schedul\|FLPM\|perform' README.md
   grep -ni 'constrained\|xgrammar\|fsm' README.md
   grep -ni 'MLA\|deepseek' README.md
   ```
3. **需要观察的现象**：每条命令都会命中若干行，行号应与上表「README 锚点」列一致。
4. **预期结果**：你会看到调度类命中第 72/76/96 行附近，受限解码类命中第 78/84 行附近，MLA 类命中第 64/80 行附近。把命中行整理成清单，就得到「优化抽屉」。
5. **若行号对不上**：说明 HEAD 已变化，改用 `grep -ni` 重新定位，不要迷信固定行号。

#### 4.1.5 小练习与答案

- **练习 1**：`slides/sglang_deepseek_model_optimizations.pdf` 应归入哪个抽屉？还能归入哪个？
  - **答案**：主归「优化」（DeepSeek 模型专项优化、7× 提速）；也可归「硬件」（涉及在具体卡上的优化），它是典型的多重归属资料。
- **练习 2**：`slides/amd_meetup_sglang_ep.pdf` 同时属于哪两个抽屉？
  - **答案**：部署（大规模 EP）与硬件（AMD meetup 语境）。
- **练习 3**：为什么安全抽屉只有一份资料？
  - **答案**：本仓库不收运行时代码，安全议题目前只有一个「可行性探讨」幻灯片；真正的可配置开关（如关闭跨租户复用）在主仓库 `sgl-project/sglang`，见 4.3。

---

### 4.2 学习顺序建议

#### 4.2.1 概念说明

分组解决了「读哪些」，顺序解决「**先读哪个**」。同一抽屉里资料有先后依赖：不懂 MLA 压缩 KV Cache，就看不懂 PD 分离为什么要跨节点搬 KV Cache；不懂单引擎调度，就看不懂多副本 Router。乱序阅读会导致「每个字都认识、连起来不知所云」。

#### 4.2.2 核心流程：六条排序原则

把任意一组资料排成序列，依次套用以下原则（前面原则优先级更高）：

1. **先广后深**：先用综述/概览建立全局地图，再钻单个机制。
   - 例：先 `slides/lmsys_1st_meetup_sglang.pdf`（概览），再 `sglang-FLPM.pdf`（细节）。
2. **概念先于机制**：先懂「为什么」，再看「怎么做到 7×」。
   - 例：先读 MLA 原理（`lmsys_1st_meetup_deepseek_mla.pdf`），再读工程优化（`sglang_deepseek_model_optimizations.pdf`）。
3. **单机先于集群**：单引擎的调度/解码/MLA 在前，多副本 Router / 大规模 EP / PD 分离在后。
   - 例：先调度（u2-l2）→ 再 Router（u2-l6）→ 再 EP/PD（u2-l5）。
4. **红利先于风险**：先读吞吐红利（RadixAttention、Cache-Aware Router），再读安全边界（侧信道）。
5. **仓库内先于外链**：本地 PDF 是入口锚点，外链博客是延伸确认；先抓本地，再跳外。
6. **录像优先于幻灯片**：能在 Videos 区段找到同日期 YouTube 录像的，**先听讲解再看 PDF**，效率更高。

#### 4.2.3 源码精读：用 README 自身的排序佐证原则

README 的 `## Slides` 区段本身就在示范「排序」，只是它按**时间倒序**排（最新在最上），这对「追新」友好，但对「从零学」不友好——所以我们要**重排**。

- README 把最近的能力放在最前：[README.md:34-36](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L34-L36)（PyTorch Conference 2025、AMD AI Dev Day 2025）。
- 而「从零学」的真正起点在底部：第一次 meetup 的概览 [README.md:76](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L76)、v0.2 起点 [README.md:110](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L110)。

**原则 6 的锚点**：Slides 区段每份 PDF 几乎都能在 Videos 区段按同日期找到录像。例如：

- GPU MODE 性能优化：幻灯片 [README.md:72](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L72) ↔ 录像 [README.md:154](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L154)（同为 2024-11-10）。
- 第一次 meetup：幻灯片簇 [README.md:74-86](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L74-L86) ↔ 录像 [README.md:158](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L158)（同为 2024-10-16）。

注意：并非每份 PDF 都有录像。biweekly 的 `update-weights-from-distributed.pdf`（[README.md:98](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L98)）对应日期 2024-11-30 在 Videos 有 sync 录像（[README.md:172](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L172)），但内容不一定逐对应；遇到无录像的，就只能硬啃 PDF。

#### 4.2.4 代码实践：为「优化抽屉」排出一条阅读链

1. **实践目标**：把 4.1 中优化抽屉的 9 份资料排成一条**由浅入深**的阅读链。
2. **操作步骤**：套用原则 1–2、6，给出顺序并各写一句「为什么放这里」。
3. **参考排序（示例答案）**：
   1. `slides/lmsys_1st_meetup_sglang.pdf` — 全局概览，建立地图（原则 1）。
   2. `blogs/Efficient LLM Deployment and Serving.md` — 配套回顾博客，把概览翻译成人话（原则 1 + 配套阅读）。
   3. `slides/sglang-FLPM.pdf` — 调度细节（原则 2：先概念后机制里的「机制」）。
   4. `slides/SGLang-Performance-Optimization-YinengZhang.pdf` — 性能工程实操（原则 2）。
   5. `slides/lmsys_1st_meetup_constrained_decoding.pdf` — 受限解码概念（原则 2）。
   6. `slides/lmsys_1st_meetup_xgrammar.pdf` — XGrammar 工程化（概念之后的机制）。
   7. `slides/lmsys_1st_meetup_deepseek_mla.pdf` — MLA 原理（原则 2）。
   8. `slides/sglang_deepseek_model_optimizations.pdf` — MLA 工程优化（原理之后的机制）。
   9. `slides/sglang_v0_2.pdf` — 起点回望，收尾。
4. **需要观察的现象**：这条链里「原理」永远在「工程优化」之前，「概览」永远在最前。
5. **预期结果**：你得到一条没有知识断点的链，每份新资料都建立在前一份之上。

#### 4.2.5 小练习与答案

- **练习 1**：原则 3「单机先于集群」要求把哪两份资料排在 Router 之前？
  - **答案**：先读单引擎调度（`lmsys_1st_meetup_sglang.pdf`、`sglang-FLPM.pdf`），再读多副本 Router（`sglang-router.pdf` / `sglang_router.pdf`）。Router 的 cache-aware 调度依赖对单引擎 RadixAttention 的理解。
- **练习 2**：原则 4「红利先于风险」具体要求什么顺序？
  - **答案**：先读 RadixAttention 与 Cache-Aware Router 的吞吐红利（优化/部署抽屉），再读 `Possible_Timing_Side_Channel_Of_KV_Cache.pdf`（安全抽屉）。不懂红利就看不懂风险来源。
- **练习 3**：为什么原则 5 建议「仓库内先于外链」？
  - **答案**：本地 PDF 是稳定的入口锚点，外链（lmsys.org 等）可能改版；先用本地资料建立认知框架，再用外链博客做延伸确认，认知更稳。

---

### 4.3 资料与外部文档 / 论文 / 主仓库的衔接

#### 4.3.1 概念说明

本仓库是**路标**，不是终点。u1-l1 已建立一条递进链路：

> **资料（本仓库）→ 用法（文档站）→ 原理（论文/博客）→ 代码（主仓库）**

本模块解决「**何时该离开本仓库、跳到哪**」。判据很简单——看你**下一步想做什么**：

| 你想做的事 | 该跳到的入口 |
| --- | --- |
| 把 SGLang 跑起来、查参数 | 文档站（用法） |
| 搞懂设计原理、看实验 | 论文 + LMSYS/AMD 博客（原理） |
| 看作者亲口讲解 | Videos（YouTube） |
| 读实现、改可配置开关 | 主仓库 `sgl-project/sglang`（代码） |

#### 4.3.2 核心流程：四个外衔接通道 + 主仓库

README 底部四个区段就是四个外衔接通道，越往下越「稳定长青」（u1-l3 已指出）：

1. **Documentation（用法）**：把引擎用起来的第一站。
2. **Paper（原理/学术）**：最权威的设计依据。
3. **Videos（讲解）**：幻灯片的「配音版」。
4. **Blog（原理/深度）**：版本发布与硬件适配的深度文章。

第五个通道不在 README 显式列出，但贯穿全仓库——**主仓库 `sgl-project/sglang`**：所有「可配置开关」「具体实现」都在那里。

#### 4.3.3 源码精读：四个通道的 README 锚点

- **Documentation**（注意 README 原文有拼写错误 `Documentaion`）：[README.md:189-191](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L189-L191)，指向 `https://sgl-project.github.io/`。
  ```markdown
  ## Documentaion
  [SGLang Documentation](https://sgl-project.github.io/)
  ```
- **Paper**：[README.md:184-186](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L184-L186)，NeurIPS 24 论文（RadixAttention 的学术出处）。
  ```markdown
  ## Paper
  [NeurIPS 24] [SGLang: Efficient Execution of Structured Language Model Programs](https://arxiv.org/abs/2312.07104)
  ```
- **Videos**：[README.md:148-182](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L148-L182)，按事件分组的 YouTube 录像，频道入口在第 150 行。
- **Blog**：[README.md:112-146](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L112-L146)，分 LMSYS Org、AMD、Meta PyTorch、Microsoft Azure 四个子区段。
  - 关键深度博客：大规模 EP [README.md:116](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L116)、v0.4 [README.md:118](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L118)、v0.3 [README.md:120](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L120)、RadixAttention 原始博客 [README.md:126](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L126)。

> **衔接边界提醒**：本仓库 README 通篇**没有**出现主仓库 `sgl-project/sglang` 的代码链接——这印证了「本仓库只聚合资料、不含运行时代码」的定位（u1-l1）。当你需要「关掉跨租户 KV 复用」这类**具体开关**时，README 帮不了你，必须去主仓库。这个边界将在 u4-l3 专题展开。

#### 4.3.4 代码实践：为问题选对入口

1. **实践目标**：给三个真实问题，分别选出正确的资料衔接入口（仓库内 / 文档 / 论文 / 主仓库）。
2. **操作步骤**：阅读下列问题，写出「先去哪」，再给出该入口在 README 的行号或 URL。
3. **问题与参考答案**：
   - **Q1**：「我想知道 RadixAttention 这个名字最早是在哪篇论文提出的？」→ 去论文通道，[README.md:186](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L186) 的 NeurIPS 论文。
   - **Q2**：「我想看 v0.4 的 Zero-Overhead Batch Scheduler 到底怎么实现的？」→ 先看深度博客建立认知（[README.md:118](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L118)），再看主仓库 `sgl-project/sglang` 的调度器代码。
   - **Q3**：「我想在本地把 SGLang 跑起来。」→ 去文档站 `https://sgl-project.github.io/`（[README.md:191](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L191)），本仓库没有安装命令。
4. **需要观察的现象**：三个问题分别对应「原理」「原理+代码」「用法」三种需求，入口各不相同。
5. **预期结果**：你不会再把「想跑起来」的需求错引到论文，也不会把「想看实现」的需求困在 README。

#### 4.3.5 小练习与答案

- **练习 1**：README 的 `## Documentaion` 区段为什么只有一行？
  - **答案**：因为文档站是「用法」的统一入口，所有使用细节都在 `sgl-project.github.io`，本仓库只负责指路。
- **练习 2**：如果你想确认「压缩 FSM 能加速多少倍」，本仓库够吗？
  - **答案**：本仓库只有概念幻灯片（`lmsys_1st_meetup_constrained_decoding.pdf`），具体数字要去 LMSYS 博客 `2024-02-05-compressed-fsm`（[README.md:124](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L124)）。
- **练习 3**：「关闭跨租户 KV 复用」这个开关能在本仓库找到吗？
  - **答案**：不能。本仓库不含运行时代码，可配置开关在主仓库 `sgl-project/sglang`。

---

## 5. 综合实践

把本讲三个模块串起来：**用 4.1 选料、用 4.2 排序、用 4.3 标注每份资料的外衔接**，产出一份为期 4 周的个人学习计划。

### 实践目标

输出一张 4 周学习表，每周指定 **2–3 份仓库内资料**（写出文件名），写明**本周学习目标**，并标注每份资料的**外衔接入口**（可选）。下面给出一份**示例计划**（面向「想从零懂到能做大规模部署」的读者），你可以照它改造为自己的版本。

### 示例：4 周学习计划

**第 1 周｜建立全局认知（优化抽屉·入门段）**

- 目标：用综述资料建立「SGLang 是什么、为什么快」的地图。
- 资料：
  1. `slides/lmsys_1st_meetup_sglang.pdf`（[README.md:76](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L76)）— 概览 + CPU Overhead Hiding。
  2. `blogs/Efficient LLM Deployment and Serving.md`（[README.md:86](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L86)）— 配套回顾，把四个项目串起来。
  3. `slides/sglang_v0_2.pdf`（[README.md:110](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L110)）— 起点。
- 外衔接：先看第一次 meetup 录像（[README.md:158](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L158)），再看 PDF。

**第 2 周｜钻进核心机制（优化抽屉·深水区）**

- 目标：吃透调度、受限解码、MLA 三大单引擎机制。
- 资料：
  1. `slides/sglang-FLPM.pdf`（[README.md:96](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L96)）+ `slides/lmsys_1st_meetup_constrained_decoding.pdf`（[README.md:78](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L78)）— 调度与受限解码。
  2. `slides/lmsys_1st_meetup_deepseek_mla.pdf`（[README.md:80](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L80)）+ `slides/sglang_deepseek_model_optimizations.pdf`（[README.md:64](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L64)）— MLA 原理 + 工程优化。
  3. `slides/lmsys_1st_meetup_xgrammar.pdf`（[README.md:84](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L84)）— 结构化生成。
- 外衔接：受限解码的提速数字去 compressed-fsm 博客（[README.md:124](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L124)）确认。

**第 3 周｜从单机走向集群（部署抽屉）**

- 目标：理解多副本 Router、大规模 EP、PD 分离如何把单引擎能力放大到集群。
- 资料：
  1. `slides/sglang-router.pdf`（[README.md:62](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L62)）— Cache-Aware Load Balancer。
  2. `slides/update-weights-from-distributed.pdf`（[README.md:98](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L98)）— 权重热更新（RLHF）。
  3. `slides/sglang_pytorch_china_2025.pdf`（[README.md:9](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L9)）+ `slides/amd_meetup_sglang_ep.pdf`（[README.md:42](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L42)）— 大规模 EP + PD 分离。
- 外衔接：大规模 EP 的成本与吞吐数字去 large-scale-ep 博客（[README.md:116](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L116)）。

**第 4 周｜硬件适配与安全边界（硬件 + 安全抽屉）**

- 目标：补齐跨硬件（AMD/量化）视角，并以安全收尾，形成完整闭环。
- 资料：
  1. `slides/sglang-fp8-mxfp-quantizations.pdf`（[README.md:102](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L102)）+ `slides/amd_meetup_aiter_mori.pdf`（[README.md:48](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L48)）— 量化与 AMD 内核。
  2. `slides/amd_meetup_sglang_roadmap.pdf`（[README.md:40](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L40)）— roadmap，回顾全链路演进。
  3. `slides/Possible_Timing_Side_Channel_Of_KV_Cache.pdf`（[README.md:100](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L100)）— 安全收尾（**必须放在第 4 周**，遵循「红利先于风险」）。

### 操作步骤（你也可以照此定制自己的计划）

1. 在 4.1 的四张表里圈出与你目标最相关的抽屉（不必四个全选）。
2. 对选中的抽屉，套用 4.2 的六条原则排出阅读链。
3. 把阅读链切成 4 段，每段对应一周，每周控制在 2–3 份。
4. 给每份资料标注一个外衔接入口（4.3），形成「仓库内入口 + 外部延伸」的双层结构。
5. 把成品存进 `sgl-learning-materials-tutorial/` 之外的个人笔记（**不要**写进本讲义目录，那是讲义区）。

### 需要观察的现象 / 预期结果

- 你的 4 周计划应当满足：第 1 周是综述、第 2 周是单机机制、第 3 周是集群、第 4 周含安全收尾——**没有知识断点**。
- 每份资料都能在 README 找到锚点行号；若某份资料在 README 找不到（如 `slides/meetup_shenzhen.pdf`），说明它尚未登记，应在计划里标注「未索引，需 `git ls-files slides/` 确认」。

## 6. 本讲小结

- **按目标分组**比按时间/事件读更高效：四个抽屉——优化（单引擎提速）、部署（集群/多副本）、硬件（AMD/量化）、安全（侧信道）。
- **一份资料可多重归属**，这是「资料簇」的特征；多重归属是正常的，不是分类错误。
- **阅读顺序六原则**：先广后深、概念先于机制、单机先于集群、红利先于风险、仓库内先于外链、录像优先于幻灯片。
- **安全必须放在优化/部署之后**：不懂 RadixAttention/Router 的吞吐红利，就看不懂侧信道风险的根源。
- **本仓库是路标**：跑起来→文档站、懂原理→论文/博客、看实现/开关→主仓库 `sgl-project/sglang`；README 通篇不出现主仓库代码链接，印证其「聚合资料」定位。
- **最终交付物是一份 4 周计划**：用 4.1 选料、4.2 排序、4.3 标外衔接，每周 2–3 份仓库内资料 + 周目标。

## 7. 下一步学习建议

- **紧接本讲**：读 **u4-l3《资料的边界与延伸资源》**，把本讲 4.3 的衔接通道展开成一份完整的「延伸资源清单」，彻底厘清本仓库与文档站、论文、主仓库的分工。
- **往回巩固**：若你的 4 周计划在某一周卡住，回到对应主题讲义——调度（u2-l2）、受限解码（u2-l3）、MLA（u2-l4）、EP/PD（u2-l5）、Router（u2-l6）、量化/硬件（u3-l2）、安全（u3-l3）。
- **动手贡献**：学完想反哺社区，按 **u4-l1** 的格式规范，把你新读到的资料登记进 README 并提 PR。
- **跳出仓库**：当本仓库已不能满足你（需要参数、实现、开关），果断跳到文档站 `https://sgl-project.github.io/` 与主仓库 `sgl-project/sglang`——那是「资料→用法→原理→代码」链路的最后两环。
