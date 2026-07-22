# 大规模部署：EP 与 PD 分离资料

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 **大规模专家并行（Expert Parallelism，EP）** 解决什么问题：为什么 DeepSeek 这种 MoE 模型在超大集群上必须把「专家（expert）」分布到很多 GPU 上，而不是简单地把模型切成几份。
- 说清楚 **Prefill-Decode 分离（PD Disaggregation）** 解决什么问题：为什么把「理解输入」和「逐字生成」两个阶段拆成两套独立服务，能同时提升吞吐、降低延迟。
- 认识 **2025 年 5 月的里程碑**：SGLang 成为第一个完整支持大规模 EP + PD 分离的全开源推理引擎，在 **96 张 H100** 上把成本压到 **$0.20 / 1M output tokens**。
- 在本仓库中 **定位这个主题的全部资料**（PyTorch Day China 幻灯片、AMD meetup 幻灯片、LMSYS 博客），把它们组成一个「资料簇」整体阅读。

> 本讲承接 [u2-l4 DeepSeek MLA 与模型优化资料](u2-l4-deepseek-mla.md)：上一讲讲的是「**单个模型怎么优化**」（MLA 如何压缩 KV Cache、v0.3 如何把 MLA 提速 7×）；本讲把镜头拉远到「**整个集群怎么把 DeepSeek 跑到生产级**」，你会看到 MLA 提速在大规模部署里被进一步用到了极致——它支撑了 **DP Attention**，让上千个请求能共享同一份压缩后的 KV Cache。

## 2. 前置知识

本仓库不含运行时代码，本讲的「源码」是导航索引 `README.md`、两份仓库内 PDF 幻灯片，以及 README 指向的一篇 LMSYS 博客。下面这几个概念是读懂它们的「背景补丁」。

### 2.1 MoE：稀疏的「专家」结构

**MoE（Mixture of Experts，混合专家）** 是 DeepSeek-V3 / R1 采用的模型结构。它的核心是：把模型里最吃算力的 **前馈网络（FFN）** 拆成很多个「**专家（expert）**」（DeepSeek-V3 有 256 个），每个 token 只激活其中少数几个（通常 8 个）。

好处是：模型总参数量可以做得很大（能力更强），但每次推理只动一小部分（算得更快）。代价是：**256 个专家的权重全部要常驻显存**，显存成了瓶颈。这正是 EP 要解决的问题。

### 2.2 三种「并行」先分清楚

把一个大模型放到很多 GPU 上跑，有三种切法，本讲会反复出现：

| 并行方式 | 切什么 | 适合谁 |
|---------|--------|--------|
| **张量并行（TP）** | 把每一层的矩阵**纵向切开**，每张卡算一部分，再 all-reduce 拼起来 | 密层、大矩阵 |
| **数据并行（DP）** | 每张卡**各拿一份完整模型**，处理不同请求 | 减少通信、可堆 batch |
| **专家并行（EP）** | 把**不同的专家分给不同的卡**，token 按需被路由（dispatch）到对应专家所在的卡 | MoE 的稀疏 FFN |

关键直觉：EP 会带来一种特殊的通信——**all-to-all**（每个 token 都可能要去任何一张卡找它的专家），既不规则又容易负载不均。处理这种通信是 EP 工程化的难点。

### 2.3 Prefill 与 Decode：两个性格迥异的阶段

LLM 推理分两个阶段：

- **Prefill（预填充）**：把整段输入一次性算掉，生成第一份 KV Cache。**计算密集（computation-intensive）**。
- **Decode（解码）**：之后一个 token 一个 token 往外吐，每步都要读写 KV Cache。**访存密集（memory-intensive）**。

传统做法是把两个阶段塞进**同一个引擎、同一批 GPU** 里统一调度。本讲第 4.2 节会讲这种「统一调度」为什么在大规模 MoE 下会出三个大问题，从而引出 PD 分离。

### 2.4 RDMA：跨节点的「直接搬显存」

**RDMA（Remote Direct Memory Access，远程直接内存访问）** 允许一台机器**不经 CPU、直接读写另一台机器的显存**。本讲里 Prefill 节点算完的 KV Cache，就是靠 RDMA 跨节点搬到 Decode 节点的，这是 PD 分离能在工程上跑通的关键。

---

## 3. 本讲源码地图

本仓库不含运行时代码，所谓「源码」就是导航索引 `README.md` 与仓库内的资料文件。本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md) | 资料导航索引；本讲要反复回到它的 Announcement、Slides、Blog 三个区段定位「大规模 EP」条目 |
| [slides/sglang_pytorch_china_2025.pdf](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/slides/sglang_pytorch_china_2025.pdf) | **PyTorch Day China** 上宣讲的幻灯片（24 页，仓库内资产），README 的 2025 年 5 月里程碑公告**明确点名**它就是这场大规模 EP 工作的配套幻灯片 |
| [slides/amd_meetup_sglang_ep.pdf](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/slides/amd_meetup_sglang_ep.pdf) | **AMD SGLang Meetup** 的「Large-scale Deployment of Emerging LLMs」幻灯片（8 页，仓库内资产），是里程碑之后又一轮对外讲解 |
| LMSYS 博客（外链）`https://lmsys.org/blog/2025-05-05-large-scale-ep/` | README 的 Blog 区段收录的「Deploying DeepSeek with PD Disaggregation and Large-Scale Expert Parallelism on 96 H100 GPUs」，是本主题**最权威、最详细**的技术来源，也是幻灯片的底稿 |

> 提醒：两份 PDF 是二进制幻灯片，本讲无法逐页展示其内部文字（命令行环境未安装 PDF 文本提取工具，已确认）。凡涉及「第几页具体写了什么」的细节，都标注「**待本地验证**」。本讲能 100% 核实的是 README 里的文字条目、两份 PDF 的页数与文件元信息，以及 README 指向的那篇 LMSYS 博客的正文——后者正是本讲实践任务指定的阅读对象。

---

## 4. 核心概念与源码讲解

### 4.1 大规模专家并行（Expert Parallelism）

#### 4.1.1 概念说明

**专家并行（EP）** 要解决的问题是：DeepSeek-V3 有 256 个专家，权重极大，单张卡（甚至单台 8 卡节点）根本放不下、也跑不动全部专家。

EP 的思路一句话：**把专家分散到很多张 GPU 上，每个 token 按需被送到它的专家所在的卡上去算，算完再送回来。**

```
256 个专家 ──分散──► GPU0: 专家{0..31}   GPU1: 专家{32..63}  ...  GPU7: 专家{224..255}
                              ▲                  ▲
              token 按路由表 ──┴──────────────────┘  (all-to-all dispatch)
```

但 EP 有三个工程难点，SGLang 用 DeepSeek 开源的三个组件分别攻克：

| 难点 | 用什么解决 | 它是什么 |
|------|-----------|---------|
| token 怎么高效送到对的专家卡（不规则 all-to-all） | **DeepEP** | DeepSeek 开源的 EP 通信库 |
| 专家算的分组矩阵乘（Grouped GEMM）不够快 | **DeepGEMM** | DeepSeek 开源的高效 MoE 矩阵乘库 |
| 各卡专家负载不均，慢卡拖累全队 | **EPLB** | Expert Parallelism Load Balancer，专家负载均衡器 |

README 的 5 月里程碑公告把这三个组件一起列为 SGLang 新支持的能力（**待本地验证**：公告原文未逐一展开，具体组件名以博客正文为准）：

[README.md:7-L9](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L7-L9) —— 公告宣布 SGLang 成为「第一个完整支持大规模 EP + PD 分离的全开源推理引擎」，成本降到 $0.20 / 1M output tokens，并链接到博客与 PyTorch Day China 幻灯片。

#### 4.1.2 核心流程

LMSYS 博客把 DeepSeek 在集群上的**并行设计**拆成四个部件，每个部件用不同的并行策略。理解这张表就理解了「为什么 EP 是其中一环」：

| 部件 | 并行策略 | 关键理由 |
|------|---------|---------|
| **Attention 层** | MLA + **DP Attention** | 消除 KV Cache 跨卡重复（承接 [u2-l4](u2-l4-deepseek-mla.md) 的 MLA） |
| **Dense FFN（3 层）** | DP 优于 TP | 高 TP 会把 18432 维切得不能被 128 整除，效率差 |
| **Sparse FFN（MoE）** | **EP**（本节主角） | 专家权重太大，必须分散到多卡 |
| **LM Head** | DP | 词表大，DP 比 vocab 并行更省通信 |

**为什么 Dense FFN 要用 DP 而不是 TP？** 博客给了一段漂亮的显存数学（这里转述其结论）。纯 TP 下，单层 Transformer 每卡显存近似为：

\[
\text{Memory} = \frac{N_{\text{param}}}{\text{TP}} + (1+k)\, N_{\text{hidden\_state}}\cdot \text{DP}
\]

其中 \(N_{\text{hidden\_state}} = n_{\text{token}}\times n_{\text{hidden\_size}}\) 是每卡隐状态规模，\(N_{\text{param}} = n_{\text{intermediate\_size}}\times n_{\text{hidden\_size}}\) 是该层参数量，\(k\) 是 CUDA Graph 复制带来的额外开销系数。假设 \(\text{DP}=\text{TP}\)，对 TP 求最优可得：

\[
\text{TP}^{*} = \sqrt{\frac{N_{\text{param}}}{(1+k)\, N_{\text{hidden\_state}}}}
\]

博客用 DeepSeek-V3 的 `intermediate_size = 18432` 代入：**Prefill 阶段** CUDA Graph 通常关闭（\(k=0\)），每卡 token 数轻易超过 2048，算出最优 TP ≤ 3；**Decode 阶段** 每卡约 128 token、\(k=3\)，最优 TP = 6。两阶段最优 TP 都很小，所以与其硬上大 TP，不如用 DP 更省显存。用户可用启动参数 `--moe-dense-tp-size=1` 开启「DP 化的 dense FFN」。

> 通信上也划算：纯 TP 每个 FFN 要两次 all-reduce；改成 DP 后只剩「前一个 attention 的 reduce-scatter + 下一个的 all-gather」，通信量减半；当 attention 也是纯 DP 时，FFN 之间甚至**零跨卡通信**。

**Sparse FFN 的 EP 流程**则更复杂，核心是把「attention 算完的隐状态 → 路由到专家 → 专家算完 → 合并回来」做成计算与通信重叠：

```
隐状态
  │
  ├─ DeepEP dispatch ──► 把 token 发到各自专家所在卡 (all-to-all)
  ├─ DeepGEMM Grouped GEMM ──► 专家计算
  └─ DeepEP combine ──► 把结果收回来
        ↑
   Two-batch Overlap (TBO): 把 batch 切两半，让上面三步的「通信」与「计算」重叠，顺带把峰值显存减半
```

其中 **EPLB** 负责「专家往哪些卡放」的规划：DeepSeek-V3 原始 256 个专家只能按 2 的幂做并行；EPLB 允许冗余专家（256 + 32 = 288 个），于是能支持 EP=12、EP=72 这类非 2 的幂的规模。

#### 4.1.3 源码精读

**证据一：5 月里程碑公告把 EP 列为头号能力。**

[README.md:9](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L9) —— 原文「SGLang has become the first fully open-source LLM serving engine to support **large-scale Expert-Parallelism (EP)** and Prefill-Decode disaggregation, achieving throughput that matches the performance reported in the DeepSeek official blog. The cost has been reduced to **$0.20 per 1M output tokens**.」这是仓库里对 EP 里程碑最权威的一句定性。

**证据二：博客给出了 EP 的量化收益。**

LMSYS 博客正文（README [L116](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L116) 指向）写明：相比同样的资源用朴素张量并行（vanilla TP），这套 EP 优化策略把**输出吞吐提升多达 5×**。博客还给出具体数字——Decode 阶段在 9 节点（EP72，只有 DeepSeek 一半的规模）上达到 **22,282 tokens/sec** 每节点，比 TP16 基线快 **5.2×**。

**证据三：PyTorch Day China 幻灯片是这套 EP 设计的图文版。**

[slides/sglang_pytorch_china_2025.pdf](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/slides/sglang_pytorch_china_2025.pdf) —— 共 24 页。README 公告（[L9](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L9)）明确说这份幻灯片「presented at PyTorch Day China」就是这场大规模 EP 工作的配套讲解。它最可能用图示呈现并行设计四部件与 EP 的 dispatch/combine 流程（**待本地验证**：具体每页内容需打开 PDF 核实）。

#### 4.1.4 代码实践

这是一个**源码阅读 + 表格理解型实践**——通过 DeepEP 的「调度模式兼容性表」理解 EP 为什么和 PD 分离绑在一起。

1. **实践目标**：理解 DeepEP 两种调度模式（Normal / Low-Latency）分别适合哪个阶段，以及为什么单靠 EP 的「auto 模式」不够、还需要 PD 分离来配合。
2. **操作步骤**：
   - 打开 README [L116](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L116) 指向的 LMSYS 博客，定位「Expert Parallelism with DeepEP」小节里的那张兼容性表。
   - 把表格抄下来（共三行：Normal / Low-Latency / Auto，四列：Long Input / Long Output / DP Attention / CUDA Graph）：
   
     | 模式 | 长输入(Prefill) | 长输出(Decode) | DP Attention | CUDA Graph |
     |------|:---:|:---:|:---:|:---:|
     | Normal | ✅ | ❌ | ✅ | ❌ |
     | Low-Latency | ❌ | ✅ | ✅ | ✅ |
     | Auto | ✅ | ✅ | ❌ | ✅ |
   
3. **需要观察的现象**：没有任何一个模式能同时满足「长输入 ✅、长输出 ✅、DP Attention ✅、CUDA Graph ✅」四个需求。
4. **预期结果**：你会得出结论——Normal 适合 Prefill、Low-Latency 适合 Decode，但 auto 模式没法在同一通信组里同时跑两种；所以必须**把 Prefill 和 Decode 拆开（即 PD 分离）**，让每个阶段用各自的调度模式。这就把 4.1（EP）和 4.2（PD 分离）逻辑上连起来了。（表格内容来自博客正文，可在本地打开博客核实。）
5. **延伸**：在博客里搜索 `--moe-dense-tp-size=1` 与 `SGL_ENABLE_JIT_DEEPGEMM`，看用户实际要设哪些开关。

#### 4.1.5 小练习与答案

**练习 1**：既然有 TP（张量并行），为什么 MoE 的稀疏 FFN 偏偏要用 EP？

> **参考答案**：TP 是把每个矩阵均匀切开，但 MoE 的稀疏 FFN 有 256 个专家、权重总量极大，单卡放不下；而且每个 token 只激活少数专家，TP 会浪费大量「没被激活的专家分片」的算力与显存。EP 把不同专家放不同卡，既解决显存放不下，又让 token 只去真正需要的卡，更贴合 MoE 的稀疏性。

**练习 2**：EPLB 为什么要把专家数从 256 增加到 288？

> **参考答案**：原始 256 个专家只能做 2 的幂的并行规模（2/4/8/16…）；EPLB 允许「冗余专家」（256 + 32 = 288），既能把热门专家复制多份缓解负载不均，又能解锁 EP=12、EP=72 这类非 2 的幂的配置，让集群划分更灵活。

**练习 3**：DeepEP 的 Normal 和 Low-Latency 两种调度模式，各自为什么不能通吃两个阶段？

> **参考答案**：Normal 模式为长输入（Prefill）优化、追求最大吞吐，但它产生「符号化 shape」，与 CUDA Graph 不兼容，decode 阶段用它会因 kernel 启动开销变大而变慢；Low-Latency 模式为 decode 优化、支持 CUDA Graph、延迟最低，但它要预分配固定显存，且不适合长输入。两者各有最佳舞台，所以需要按阶段分开用。

---

### 4.2 Prefill-Decode 分离部署（PD Disaggregation）

#### 4.2.1 概念说明

**Prefill-Decode 分离（PD Disaggregation）** 的思路一句话：**别让 Prefill 和 Decode 在同一批 GPU 上互相打扰，把它们拆成两套独立的服务，各自调到最优。**

为什么要拆？回到第 2.3 节：Prefill 是计算密集、Decode 是访存密集，两者对硬件的需求完全相反。传统「统一调度」把两个阶段混在一起跑，博客指出会产生三个问题：

1. **Prefill 打断 Decode（Prefill Interruption）**：新来的长输入 Prefill 任务会频繁打断正在逐字生成的 Decode 任务，造成生成延迟抖动。
2. **DP Attention 不均衡（DP Attention Imbalance）**：在 DP Attention 下，可能一个 DP worker 在跑 Prefill、另一个在跑 Decode，导致 Decode 延迟升高。
3. **与 DeepEP 不兼容（Incompatible with DeepEP）**：第 4.1.4 节那张表已经说明，DeepEP 的两种调度模式分别对应 Prefill 和 Decode，统一调度没法同时满足两者。

PD 分离把 Prefill 和 Decode 拆开，三个问题一起解决：每个阶段都能用自己的最优调度模式、最优并行配置，互不干扰。

#### 4.2.2 核心流程

博客描述的 PD 分离架构是在「**Prefill Server（预填充服务）**」和「**Decode Server（解码服务）**」之间做配对与 KV Cache 搬运，流程如下：

```
请求进来
  │
  ▼
① Prefill Server 与 Decode Server「握手」配对，各自建立本地 sender / receiver
  │
  ▼
② Decode Server 预分配好 KV Cache 空间，通知 Prefill Server 开始前向计算
  │
  ▼
③ Prefill Server 算完 KV Cache ──(RDMA 跨节点传输)──► Decode Server
  │
  ▼
④ Decode Server 接管，开始逐 token 生成
```

为了让传输不拖慢调度，工程上有三点关键设计（来自博客）：

- **非阻塞传输（Non-blocking Transfer）**：发送/接收在后台线程里跑，不让调度器的事件循环停下来。
- **基于 RDMA 的传输**：用队列对（queue pair）建连、用分散-聚集元素（SGE）搬运不连续的显存块，跨节点直接搬显存。
- **可插拔的高性能传输库**：SGLang 提供灵活 API，可对接 **Mooncake**、**NIXL** 等高性能 RDMA 库。

> 小结一句：PD 分离 = **职责拆分**（两阶段各自最优）+ **数据搬运**（用 RDMA 把 Prefill 算出的 KV Cache 送到 Decode 节点）。

#### 4.2.3 源码精读

**证据一：README 公告把 PD 分离和 EP 并列为里程碑。**

[README.md:9](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L9) —— 同一句公告里，PD 分离（Prefill-Decode disaggregation）与 EP 并列，构成「第一个全开源支持」的双能力。这说明在团队叙事里，**EP 与 PD 分离是绑定的一对**——也呼应了 4.1.4 的结论：没有 PD 分离，EP 的两种调度模式就无法同时发挥作用。

**证据二：博客标题与正文都把 PD 分离放在显眼位置。**

[README.md:116](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L116) —— 博客标题「Deploying DeepSeek with **PD Disaggregation** and Large-Scale Expert Parallelism on 96 H100 GPUs」，直接把 PD 分离写进标题。博客正文有专门的「Prefill and Decode Disaggregation」章节，逐一展开三个问题与 RDMA 实现细节（**可在本地打开博客核实**）。

**证据三：PyTorch Day China 幻灯片应有专门的 PD 分离架构图。**

[slides/sglang_pytorch_china_2025.pdf](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/slides/sglang_pytorch_china_2025.pdf) —— 这份 24 页幻灯片基于上述博客，最可能包含 Prefill Server / Decode Server 配对与 RDMA 传输的架构示意图（**待本地验证**：具体页码需打开 PDF 核实）。

#### 4.2.4 代码实践

这是一个**调用链/流程跟踪型实践**——把 PD 分离的一次完整请求画成时序，理解「握手 → 预分配 → 计算 → 传输 → 解码」的先后。

1. **实践目标**：能复述一次请求从进入到开始吐字，在 Prefill/Decode 两套服务之间经历了哪些步骤，以及 RDMA 传输发生在哪一步。
2. **操作步骤**：
   - 打开 README [L116](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L116) 的博客，定位「Implementation Details」小节。
   - 对照 4.2.2 的四步流程，给每一步标注「发生在 Prefill Server 还是 Decode Server」「是否涉及跨节点传输」：
   
     | 步骤 | 发生在 | 是否跨节点 |
     |------|--------|:---:|
     | 握手配对 | 两者 | 是（建立连接） |
     | 预分配 KV Cache | Decode Server | 否 |
     | 前向计算 KV Cache | Prefill Server | 否 |
     | KV Cache 传输 | 两者之间 | **是（RDMA）** |
     | 逐 token 生成 | Decode Server | 否 |
   
3. **需要观察的现象**：跨节点的重活只有「KV Cache 传输」一步，且它被设计成非阻塞、后台进行。
4. **预期结果**：你能解释为什么这套设计能把 Prefill 的「计算密集」和 Decode 的「访存密集」彻底隔离开——因为两者跑在不同的 GPU 组上，只通过一次 RDMA 传 KV Cache 衔接。
5. **延伸思考**：如果 KV Cache 很大（长上下文），这次 RDMA 传输会不会成为瓶颈？博客在「Limitations」里提到 TTFT（首字延迟）还在 2–5 秒，可作为佐证。

#### 4.2.5 小练习与答案

**练习 1**：统一调度（把 Prefill 和 Decode 混在一起）有哪三个问题？

> **参考答案**：① Prefill 打断 Decode，造成生成延迟抖动；② DP Attention 下不同 worker 一个跑 Prefill、一个跑 Decode，导致 Decode 延迟升高；③ 与 DeepEP 不兼容——DeepEP 的两种调度模式分别对应 Prefill 和 Decode，没法在同一通信组里同时用。

**练习 2**：PD 分离为什么必须用 RDMA 来搬 KV Cache？

> **参考答案**：Prefill 和 Decode 现在跑在**不同的节点（GPU 组）**上，Prefill 算出的 KV Cache 必须跨节点送到 Decode 节点才能继续生成。RDMA 能不经 CPU、直接跨节点读写显存，延迟低、不打断调度器（配合后台线程做非阻塞传输），是让这种跨节点搬运在工程上跑得快的关键。

**练习 3**：PD 分离和 EP 是「绑死」的吗？能不能只要 EP、不要 PD 分离？

> **参考答案**：从 DeepEP 的兼容性表看，两者强相关但不是完全绑死。EP 的 auto 模式可以单独工作，但它没法在同一通信组里同时支持 Prefill 的 Normal 调度和 Decode 的 Low-Latency 调度，也就没法和 DP Attention 完美配合。引入 PD 分离后，Prefill/Decode 各用各的调度模式，EP 的潜力才被完全释放——所以工程实践中它们是一对组合拳。

---

### 4.3 PyTorch Day China / AMD Meetup 部署案例

#### 4.3.1 概念说明

前两节讲的是「EP 和 PD 分离是什么、为什么」，本节讲「**它在真实集群上跑出了什么成绩**」——也就是 2025 年 5 月那个被反复宣传的里程碑。

这个里程碑的几个关键数字（全部来自 README 公告 [L9](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L9) 与博客正文）：

| 维度 | 数字 | 含义 |
|------|------|------|
| 硬件规模 | **96 张 H100**（12 节点 × 8 卡，Atlas Cloud 提供） | 生产级大集群 |
| 输入吞吐 | **52.3k tokens/sec** 每节点（2000 token 输入） | Prefill 能力 |
| 输出吞吐 | **22.3k tokens/sec** 每节点 | Decode 能力 |
| 成本 | **$0.20 / 1M output tokens** | 约官方 DeepSeek Chat API 的 1/5 |
| 地位 | 第一个**追平 DeepSeek 官方吞吐**的全开源实现 | 里程碑意义 |

这一节同时也是本讲的「资料簇」示范：同一个大规模部署主题，在 README 里**横跨 Announcement、Slides、Blog 三个区段**，外加两份不同活动的幻灯片。

#### 4.3.2 核心流程

把「大规模 EP + PD 分离」这个主题在本仓库的资料排成时间线（承接 [u1-l3](u1-l3-readme-navigation.md) 与 [u2-l4](u2-l4-deepseek-mla.md) 的资料簇方法论）：

```
2025-05-05  LMSYS 博客发布（里程碑的权威技术全文）           ← README Blog 区段
     │
2025-05     Announcement 公告：首个全开源支持 EP+PD，$0.20/1M   ← README Announcement
            并点名 PyTorch Day China 幻灯片
     │
            slides/sglang_pytorch_china_2025.pdf（24 页）       ← README Slides 顶部（被公告引用）
     │
2025-08-22  AMD SGLang Meetup：Large-scale Deployment 讲解       ← README Slides / AMD SGLang Meetup
            slides/amd_meetup_sglang_ep.pdf（8 页）
```

这条线说明：**一个里程碑发布后，团队会用博客写全文、用幻灯片做图文宣讲、并在后续 meetup 里反复讲**。读的时候要把它们当成同一件事的不同载体。

#### 4.3.3 源码精读

本主题的资料簇在 README 里有四个入口，逐一精读它们的登记行：

**入口 ①：5 月里程碑公告（同时点了博客 + 幻灯片）。**

[README.md:7-L9](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L7-L9) —— 这是整个主题的「总纲」。注意它一句话里同时给了两个链接：一个外链博客（`https://lmsys.org/blog/2025-05-05-large-scale-ep/`），一个仓库内幻灯片（`./slides/sglang_pytorch_china_2025.pdf`）。这正是 [u1-l3](u1-l3-readme-navigation.md) 讲的「**内链 vs 外链**」判断：`./slides/...` 是仓库内资产，`https://...` 是外部链接。

**入口 ②：PyTorch Day China 幻灯片（仓库内 PDF，本讲主角之一）。**

被入口 ① 直接引用——公告说它「presented at PyTorch Day China」。文件是 [slides/sglang_pytorch_china_2025.pdf](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/slides/sglang_pytorch_china_2025.pdf)，共 24 页。注意：它**没有**作为独立条目出现在 Slides 区段的列表里，而是「嵌」在 Announcement 公告里被引用——这是一种需要留心的登记方式。

**入口 ③：AMD SGLang Meetup 的部署幻灯片（仓库内 PDF，本讲另一主角）。**

[README.md:42](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L42) —— `[2025-08-22] [AMD SGLang Meetup - Large-scale Deployment of Emerging LLMs](slides/amd_meetup_sglang_ep.pdf)`，登记在 `### AMD SGLang Meetup` 子区段下（[L38](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L38)），链接是 `slides/...` 相对路径，是仓库内资产。文件名里的 `ep` 暗示它讲的正是大规模专家并行部署。

**入口 ④：LMSYS 博客（外链，最详细的技术来源）。**

[README.md:116](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L116) —— `[2025-05-05] [Deploying DeepSeek with PD Disaggregation and Large-Scale Expert Parallelism on 96 H100 GPUs](https://lmsys.org/blog/2025-05-05-large-scale-ep/)`，登记在 `## Blog` → `## LMSYS Org` 区段下（[L112-L114](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L112-L114)）。**标题里的「96 H100 GPUs」就是本节那张表里 96 张卡的来源**，与公告、幻灯片互为印证。

#### 4.3.4 代码实践（资料簇检索）

这是一个**源码阅读型实践**——用 `grep` 把这个主题在 README 里的全部痕迹一次性捞出来，亲手验证它是一个跨多区段的资料簇。

1. **实践目标**：用命令确认「大规模 EP / PD 分离」主题确实横跨 Announcement、Slides、Blog 多个区段。
2. **操作步骤**：在仓库根目录执行（任选其一）：

   ```bash
   grep -niE 'expert.parallel|large-scale-ep|prefill.decode|disaggregat|96 ?h100|0\.20' README.md
   ```

3. **需要观察的现象**：输出会命中 Announcement 区段的 5 月公告、Slides 区段的 AMD meetup 条目、Blog 区段的 large-scale-ep 博客等多行，且它们分属不同的 `##` / `###` 区段。
4. **预期结果**：你至少能看到 3 条以上的匹配，分布在 Announcement、Slides、Blog 三个顶层区段——这就是「资料簇」的直接证据。（命令本身可在本地验证；具体匹配行数以你本地仓库为准。）
5. **延伸**：再执行 `git ls-files slides/ | grep -iE 'ep|pytorch'`，确认两份幻灯片确实是仓库内资产。

#### 4.3.5 小练习与答案

**练习 1**：README 里关于「大规模 EP」最权威的一句话在哪里？它同时给了哪两个链接？

> **参考答案**：在 Announcement 区段的 5 月公告（[L9](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L9)）。它同时给了一个**外链**（LMSYS 博客 `2025-05-05-large-scale-ep`）和一个**仓库内幻灯片**（`./slides/sglang_pytorch_china_2025.pdf`）。判断依据是前缀：`https://` 是外链，`./` 是内链。

**练习 2**：`slides/amd_meetup_sglang_ep.pdf` 与 `slides/sglang_pytorch_china_2025.pdf` 两份幻灯片有什么不同？

> **参考答案**：① 活动不同：前者是 2025-08-22 的 AMD SGLang Meetup，标题「Large-scale Deployment of Emerging LLMs」；后者是 PyTorch Day China，被 5 月公告直接引用。② 页数不同：前者 8 页，后者 24 页。③ 登记方式不同：前者是 Slides 区段 AMD meetup 子段下的独立条目（[L42](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L42)），后者嵌在 Announcement 公告里被引用（[L9](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L9)）。两者讲的是同一个里程碑的不同轮次宣讲。

**练习 3**：96 张 H100 是怎么算出来的？为什么成本能压到 $0.20 / 1M tokens？

> **参考答案**：96 = 12 节点 × 每节点 8 张 H100（博客标题「on 96 H100 GPUs」、正文「12 nodes, each equipped with 8 H100 GPUs」一致）。成本能压低，是因为 EP + PD 分离把吞吐提升了**多达 5×**（相对朴素 TP），固定硬件成本被分摊到 5 倍的 token 上，所以单 token 成本降到约官方 API 的 1/5。

---

## 5. 综合实践

这是本讲规格指定的主实践：**依据幻灯片与 README 指向的「large-scale-ep」博客，写一份要点说明——为什么 EP + PD 分离能把成本降到 $0.20 / 1M output tokens。**

任务：打开 README [L116](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L116) 的 LMSYS 博客，结合本讲三个模块，把「降本」这条因果链补全成下面这张表（已给出骨架，请填「机制」与「带来的收益」两列）：

| 环节 | 机制（它做了什么） | 带来的收益 |
|------|------------------|-----------|
| ① MoE 显存瓶颈 | EP 把 256 个专家分散到多卡 | ？ |
| ② Prefill/Decode 互相打扰 | PD 分离 + RDMA 搬 KV Cache | ？ |
| ③ KV Cache 跨卡重复 | DP Attention（建立在 MLA 上） | ？ |
| ④ 专家负载不均 | EPLB（256→288 冗余专家） | ？ |
| ⑤ 多节点通信拖慢 | Two-batch Overlap（TBO） | ？ |
| ⑥ MoE 矩阵乘慢 | DeepGEMM（Grouped GEMM） | ？ |
| **合计** | — | 吞吐最多 5× → 成本降到 1/5 → **$0.20 / 1M** |

要求：

1. 每行用一句话写清「机制」与「收益」，收益尽量落到「吞吐↑ / 延迟↓ / 显存↓ / 负载均衡」之一。
2. 在表下方用 3 到 5 句话把这条链子串成一段连贯说明，结论要能回答：**为什么是「降到约 1/5」而不是别的倍数？**（提示：因为端到端输出吞吐相对朴素 TP 提升多达 5×，固定成本被摊到 5 倍 token 上。）
3. 标注哪些数字你**在博客里亲眼读到**（如 5×、22.3k、$0.20），哪些是本讲推断。保持诚实，便于复查。
4. 完成后，把这份「降本因果链」和 [u2-l4](u2-l4-deepseek-mla.md) 的 MLA 提速对照——你会看到 MLA（单模型优化）和 EP+PD（系统级优化）是如何在端到端成本里**分工合作**的。

> 说明：本实践是「源码阅读 + 归纳写作」型，不需要运行任何程序（本仓库无可运行脚本）。若无法联网打开博客，可退而依据本讲转述的博客要点作答，并标注「未直接打开博客，要点待复核」。

---

## 6. 本讲小结

- **EP 解决「专家放不下」**：DeepSeek-V3 有 256 个专家、权重极大，EP 把专家分散到多卡、用 DeepEP 做 token 路由、DeepGEMM 做分组矩阵乘、EPLB 做负载均衡。
- **PD 分离解决「两阶段互相打扰」**：把计算密集的 Prefill 和访存密集的 Decode 拆成两套服务，用 RDMA 跨节点搬 KV Cache 衔接；它还让 DeepEP 的两种调度模式（Normal 给 Prefill、Low-Latency 给 Decode）能各得其所。
- **EP 与 PD 分离是一对组合拳**：DeepEP 的兼容性表说明单靠 EP 的 auto 模式无法同时满足两阶段，必须靠 PD 分离来配合，二者在工程上绑定。
- **2025 年 5 月里程碑**：SGLang 成为第一个追平 DeepSeek 官方吞吐的全开源引擎，在 96 张 H100（12 节点 × 8 卡）上跑出 52.3k 输入 / 22.3k 输出 tokens/sec 每节点，成本降到 **$0.20 / 1M output tokens**（约官方 API 的 1/5）。
- **这是一个典型的资料簇**：同一主题横跨 README 的 Announcement（公告）、Slides（PyTorch Day China + AMD meetup 两份幻灯片）、Blog（large-scale-ep 博客）三个区段；其中 PyTorch Day China 幻灯片嵌在公告里被引用，AMD meetup 幻灯片是独立条目。
- **降本的本质**：EP + PD 分离把端到端输出吞吐相对朴素 TP 提升多达 5×，固定硬件成本被摊到 5 倍 token 上，单 token 成本自然降到约 1/5。

## 7. 下一步学习建议

- 下一讲 [u2-l6 路由与权重热更新资料](u2-l6-router-weights.md) 会从「单集群部署」走到「多副本调度与不停机更新」：讲 SGLang Router（cache-aware 负载均衡）和分布式权重热更新，你会看到大规模部署之上还需要一层「流量分发」与「在线更新」能力。
- 若想补 EP / PD 分离的实现代码，它们**不在本仓库**——本仓库只是路标。请到主仓库 `sgl-project/sglang`，并以博客附录列出的相关 PR（如 #4521、#4767、#4836 等）为入口深入。
- 想看这套部署在 **AMD 硬件**上的版本，可提前跳读 [u3-l2 硬件适配与量化资料](u3-l2-quantization-hardware.md)，了解 fp8/mxfp 量化与 AITER/MoRI 如何在 MI300X 上承接类似的大规模服务。
- 想理解大规模共享 KV Cache 可能带来的**安全副作用**，可读 [u3-l3 安全与边界：KV Cache 侧信道资料](u3-l3-kv-cache-side-channel.md)——PD 分离与共享缓存是把双刃剑。
- 复习时可回看 [u2-l4 DeepSeek MLA](u2-l4-deepseek-mla.md)：本讲的 DP Attention 正是建立在 MLA 的低秩 KV Cache 之上，两讲合起来才构成「单模型优化 → 集群级部署」的完整画面。
