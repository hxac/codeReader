# 路由与权重热更新资料

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 **SGLang Router（缓存感知负载均衡器，Cache-Aware Load Balancer）** 解决什么问题：当一台推理服务扛不住、要起「多副本（multiple workers）」时，谁来把进来的请求分发到合适的副本上？为什么「按缓存命中率分发」比「平均轮询」更省算力。
- 说清楚 **分布式权重热更新（Update Weights From Distributed）** 解决什么问题：模型权重变了（比如训练每一步都在更新），怎样让一个**已经在对外服务**的集群**不用重启、不停机**地换上新权重。
- 把这两件事放进 **大规模 RLHF 训练** 这个真实场景里看：为什么「路由」+「权重热更新」是让上万卡 RLHF 训练能跑起来的两块关键拼图。
- 在本仓库里 **定位本主题的全部资料**，并特别学会区分两个**文件名几乎一样**、却分属不同活动、不同时期的 router 幻灯片：`sglang_router.pdf`（下划线）与 `sglang-router.pdf`（连字符）。

> 本讲承接 [u2-l5 大规模部署：EP 与 PD 分离资料](u2-l5-large-scale-ep.md)：上一讲讲的是「**一个集群内部怎么把 DeepSeek 跑到生产级**」（EP 分专家、PD 分阶段、RDMA 搬 KV Cache）；本讲把镜头再往上抬一层——当一个集群还不够、要起**多个推理副本**时，谁来分发流量（**Router**），以及这些副本的权重需要频繁换新时怎么不重启地换（**权重热更新**）。你会看到上一讲的 RadixAttention / KV Cache（参见 [u2-l4](u2-l4-deepseek-mla.md)）正是 Router「缓存感知」的依据。

---

## 2. 前置知识

本仓库不含运行时代码，本讲的「源码」是导航索引 `README.md` 与三份仓库内 PDF 幻灯片。下面几个概念是读懂它们的「背景补丁」。

### 2.1 多副本服务：一台不够，就起很多台

LLM 推理服务跑起来后，单台机器（一个 SGLang server）的吞吐是有上限的。生产环境的常规做法是**横向扩展**：起很多个一模一样的推理进程（每个叫一个 **worker / replica**），再用一个**负载均衡器（load balancer）**挡在前面，把进来的请求分给它们。

```
                 ┌─────────────┐
   请求流 ──────► │  负载均衡器  │ ──┬──► worker 0 (SGLang server)
                 └─────────────┘   ├──► worker 1 (SGLang server)
                                   └──► worker 2 (SGLang server)
```

最朴素的分发策略是**轮询（round-robin）**：第 1 个请求给 worker 0、第 2 个给 worker 1……雨露均沾。本讲的 Router 提出的问题是：**「雨露均沾」真的是最优的吗？** 答案是未必——因为 SGLang 有 KV Cache 可以复用（见 2.3）。

### 2.2 KV Cache 与 RadixAttention：算过的就别再算

LLM 推理时，每个 token 都会算出一组「键值」缓存下来，叫 **KV Cache**（参见 [u2-l4 DeepSeek MLA](u2-l4-deepseek-mla.md)）。它的关键性质是：**如果两个请求的前缀（system prompt、few-shot 示例、对话上文）相同，那么这段前缀对应的 KV Cache 可以直接复用**，不必重算。

SGLang 用 **RadixAttention**（基数树）把历史上算过的前缀组织成一棵树，新请求进来先在树里找「最长可复用前缀」，命中就省掉一大段 prefill 计算。这正是 Router 要利用的「**缓存红利**」。

### 2.3 为什么「按缓存分发」能省算力

把 2.1 和 2.2 拼起来就得到本讲的直觉：

- 假设 worker 0 刚算过一段前缀 `A`，它的 RadixAttention 树里缓存了 `A` 的 KV Cache。
- 现在新请求 `A+B` 进来。如果负载均衡器**聪明地**把它分给 worker 0，`A` 这段直接命中、不用重算，只算 `B`。
- 如果**笨笨地**按轮询分给 worker 1，worker 1 没有 `A` 的缓存，就得从头把 `A` 重算一遍——白白浪费算力。

所以「**把请求分给最可能已经有它前缀缓存的那个副本**」就是 **cache-aware（缓存感知）** 路由的核心思想。SGLang Router 就是把这个思想工程化的负载均衡器。

### 2.4 RLHF：权重一直在变的特殊场景

**RLHF（Reinforcement Learning from Human Feedback，基于人类反馈的强化学习）** 是大模型对齐的主流方法。大规模 RLHF 训练里有一类「推理」工作叫 **rollout（采样生成）**：用「当前这一步的策略模型」去生成大量回答，再拿这些回答算奖励、更新模型。

关键痛点是：**每训练一步，策略模型的权重就变一次**。如果每次换权重都要把服务停掉、把几十上百 GB 的权重从磁盘重新加载进显存（冷启动），那 RLHF 训练会被「反复重启」彻底拖垮。这就是「**权重热更新**」要解决的问题——它和 Router 一起，构成了大规模 RLHF 推理的基础设施（详见第 4.3 节）。

---

## 3. 本讲源码地图

本仓库不含运行时代码，所谓「源码」就是导航索引 `README.md` 与仓库内的资料文件。本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md) | 资料导航索引；本讲要反复回到它的 Slides、Blog、Videos 三个区段定位 router / 权重热更新 / RLHF 条目 |
| [slides/sglang_router.pdf](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/slides/sglang_router.pdf)（**下划线**） | [2024-11-16] 标题「SGLang Router」，登记在 Biweekly Meeting；Router 概念的**早期引入**版本（仓库内资产） |
| [slides/sglang-router.pdf](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/slides/sglang-router.pdf)（**连字符**） | [2025-01-15] 标题「Cache-Aware Load Balancer in SGLang」，登记在 Hyperbolic 线下聚会；Router 的**更晚、更聚焦缓存感知**版本（仓库内资产） |
| [slides/update-weights-from-distributed.pdf](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/slides/update-weights-from-distributed.pdf) | [2024-11-30] 标题「Update Weights From Distributed」，登记在 Biweekly Meeting；分布式权重热更新的讲解（仓库内资产） |

> 提醒：三份 PDF 是二进制幻灯片，本讲无法逐页展示其内部文字（命令行环境既未安装 `poppler-utils`，也无法运行 Python PDF 库，已确认）。凡涉及「第几页具体写了什么」的细节，都标注「**待本地验证**」。本讲能 100% 核实的是 README 里的文字条目（标题、日期、所在区段、行号）、三份 PDF 的文件存在性与大小，以及 README 指向的配套 YouTube 录像与 v0.4 发布博客的标题——后者把「Cache-Aware Load Balancer」明列为 v0.4 的三大特性之一，是本主题最权威的文字佐证。

---

## 4. 核心概念与源码讲解

### 4.1 缓存感知负载均衡器（SGLang Router）

#### 4.1.1 概念说明

**SGLang Router** 是一个独立运行的**负载均衡进程**，挡在多个 SGLang server（worker）副本前面。它要回答的问题是：**一个请求来了，分给哪个副本最划算？**

- 传统负载均衡（如简单的 round-robin 或随机）只看「负载均不均」，不管每个副本里**缓存了什么**。
- **Cache-Aware Load Balancer（缓存感知负载均衡器）** 额外看一眼「哪个副本最可能已经缓存了这个请求的前缀」，把请求送给缓存命中率最高的那个，从而最大化 RadixAttention 的复用红利、减少重复 prefill 计算。

这正好和 [u2-l5](u2-l5-large-scale-ep.md) 讲的大规模部署衔接：当集群规模上去、副本变多，**副本之间的缓存协调**就成了吞吐天花板——Router 就是管这件事的。README 里它的两个名字都出现了：早期叫「SGLang Router」，后期更精准地叫「Cache-Aware Load Balancer」。

#### 4.1.2 核心流程

把一次请求在 Router 下的旅程画出来（直觉流程，**待本地验证**：具体实现细节以幻灯片/主仓库代码为准）：

```
请求到达 Router
  │
  ▼
① Router 提取请求的前缀指纹（例如对 prompt 前缀做哈希）
  │
  ▼
② 查「各副本当前的缓存状态」：哪个 worker 已经缓存了这段前缀？
  │
  ├── 有副本缓存命中 ──► 优先发给该副本（命中越多越优先）
  │
  └── 没有副本命中   ──► 退化为按负载选（让最闲的副本兜底）
  │
  ▼
③ 请求被转发到选定副本，命中前缀的 KV Cache 被复用，只算增量部分
```

这里的「各副本缓存状态」可以用一棵**跨副本的 RadixAttention 索引**来近似：Router 持有每个 worker 缓存了哪些前缀的摘要，新请求的前缀哈希在里面找最近邻。直觉上的收益可以这么表达——设某请求前缀长度为 \(L_{\text{prefix}}\)、命中复用比例为 \(r\)（\(0 \le r \le 1\)），则该请求的 prefill 计算量从正比于 \(L_{\text{prefix}}\) 降到正比于 \((1-r)\,L_{\text{prefix}}\)：

\[
\text{Prefill 计算量} \;\propto\; (1-r)\,L_{\text{prefix}}
\]

\(r\) 越大（缓存命中率越高），节省越多。Router 的全部意义就是**在多副本间把 \(r\) 顶到最大**——而 round-robin 因为不看缓存，相当于把请求随机撒，\(r\) 会很低。

#### 4.1.3 源码精读（两份 router 幻灯片 + v0.4 博客）

本主题在 README 里构成一个迷你的「**资料簇**」（参见 [u1-l3](u1-l3-readme-navigation.md) 的资料簇方法论）：同一件事（Router）在不同活动、不同时期各登记了一次，外加一篇把它列为正式特性的发布博客。

**入口 ①：早期版本——`sglang_router.pdf`（下划线，Biweekly）。**

[README.md:100](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L100) —— `[2024-11-16] [SGLang Router](slides/sglang_router.pdf) and [Side-Channel KV Cache Attack](slides/Possible_Timing_Side_Channel_Of_KV_Cache.pdf)`。它登记在 `### SGLang Biweekly Meeting` 子区段下（[L92](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L92)），是双周开发者例会上的分享。注意两个细节：① 文件名用的是**下划线** `sglang_router.pdf`；② 它和「KV Cache 侧信道攻击」**挤在同一行**（`and` 连接）——这两件看似不相关的事被并排登记，恰好都和「多副本共享缓存」有关（侧信道将在 [u3-l3](u3-l3-kv-cache-side-channel.md) 详讲）。

**入口 ②：晚期版本——`sglang-router.pdf`（连字符，Hyperbolic 线下聚会）。**

[README.md:62](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L62) —— `[2025-01-15] [Cache-Aware Load Balancer in SGLang](slides/sglang-router.pdf)`。它登记在 `### Hyperbolic in-person meetup` 子区段下（[L58](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L58)），是 Hyperbolic 公司线下聚会的分享。注意三个细节：① 文件名换成**连字符** `sglang-router.pdf`；② 标题从泛化的「SGLang Router」**精确化为「Cache-Aware Load Balancer」**，说明半年后这个特性的「缓存感知」内核被凸显出来；③ 它是**独立条目**，不像入口 ① 那样和别的主题挤一行。

**入口 ③：v0.4 发布博客——把「Cache-Aware Load Balancer」列为正式特性。**

[README.md:118](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L118) —— `[2024-12-04] [SGLang v0.4: Zero-Overhead Batch Scheduler, **Cache-Aware Load Balancer**, Faster Structured Outputs](https://lmsys.org/blog/2024-12-04-sglang-v0-4/)`。**博客标题本身就把「Cache-Aware Load Balancer」写成了 v0.4 的三大卖点之一**（另外两个是零开销批调度器、更快的结构化输出——分别对应 [u2-l2](u2-l2-scheduler-performance.md) 与 [u2-l3](u2-l3-constrained-decoding.md)）。时间线上，v0.4 发布于 2024-12-04，正好**夹在**入口 ①（11-16）与入口 ②（次年 01-15）之间，说明 Router 是从「11 月双周会预告 → 12 月随 v0.4 正式发布 → 次年 1 月线下聚会再细化」逐步成熟的。这条 v0.4 博客是本主题**最权威的文字佐证**（可在本地打开博客核实）。

> **下划线 vs 连字符——本讲的辨析重点**：两个文件名只差一个字符（`_` vs `-`），却是**不同活动、不同时期**的两份幻灯片，绝不能混为一谈。下表把两者逐项对照（全部依据 README 文字，可核实）：

| 对照维度 | `sglang_router.pdf`（下划线） | `sglang-router.pdf`（连字符） |
|---------|------------------------------|------------------------------|
| 所在 README 分区 | `### SGLang Biweekly Meeting`（[L92](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L92)） | `### Hyperbolic in-person meetup`（[L58](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L58)） |
| 登记日期 | `[2024-11-16]` | `[2025-01-15]` |
| README 标题 | 「SGLang Router」（泛称） | 「Cache-Aware Load Balancer in SGLang」（聚焦缓存感知） |
| 同行条目 | 与「Side-Channel KV Cache Attack」同行 | 独立条目 |
| 阶段定位 | 较早，Router 概念引入 | 较晚，特性已随 v0.4 正式发布后的再宣讲 |
| 配套录像 | YouTube「SGLang Developer Sync 20241116」（[L174](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L174)） | 无同日期 Dev Sync（Hyperbolic 是线下聚会，无常规双周录像） |

#### 4.1.4 代码实践（资料簇检索 + 辨析）

这是一个**源码阅读型实践**——用 `grep` 把两份 router 幻灯片的登记行一次性捞出来，亲手验证「下划线 vs 连字符」确实指向不同条目。

1. **实践目标**：用命令确认仓库里有两份文件名高度相似的 router 幻灯片，并理清它们的归属差异。
2. **操作步骤**：在仓库根目录执行：

   ```bash
   # 1) 看仓库里到底有几份 router 幻灯片（文件层面）
   git ls-files slides/ | grep -i router

   # 2) 看 README 把它们登记在哪里（文字层面）
   grep -niE 'router|cache-aware load balancer' README.md
   ```

3. **需要观察的现象**：
   - 第 1 条命令应输出两个文件：`slides/sglang-router.pdf`（连字符）与 `slides/sglang_router.pdf`（下划线）。
   - 第 2 条命令应在 README 里命中**两行**：[L62](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L62)（Hyperbolic，连字符）与 [L100](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L100)（Biweekly，下划线），外加 [L118](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L118) 的 v0.4 博客（Cache-Aware Load Balancer）。
4. **预期结果**：你会清楚地看到——「文件名只差一个字符」在仓库里对应的是**两份不同活动、不同日期**的资料，绝不能当成同一份。（命令本身可在本地验证；具体匹配行以你本地仓库为准。）
5. **延伸**：再到 Videos 区段执行 `grep -niE 'developer sync 20241116|developer sync 20241130' README.md`，验证 Biweekly 的两份幻灯片（router 与权重热更新）都有同日期的 YouTube 录像配套。

#### 4.1.5 小练习与答案

**练习 1**：既然有现成的通用负载均衡器（Nginx、HAProxy 之类），SGLang 为什么还要自己做一个 Router？

> **参考答案**：通用负载均衡只能按连接数、轮询等「外部负载」分发，**看不到每个副本里 SGLang 的 RadixAttention 缓存了什么前缀**。SGLang Router 是「**缓存感知**」的——它知道哪个副本已经缓存了请求的前缀，把请求送给命中率最高的副本，从而复用 KV Cache、省掉重复 prefill。这是通用 LB 做不到的、与推理引擎内部状态耦合的能力。

**练习 2**：`sglang_router.pdf` 和 `sglang-router.pdf` 是同一份文件吗？怎么最快区分？

> **参考答案**：不是。前者用**下划线**、登记在 Biweekly、日期 `[2024-11-16]`、标题「SGLang Router」；后者用**连字符**、登记在 Hyperbolic 线下聚会、日期 `[2025-01-15]`、标题「Cache-Aware Load Balancer in SGLang」。最快的区分办法是执行 `git ls-files slides/ | grep -i router`，会看到两个文件名只差一个字符，再用 `grep -ni router README.md` 对照它们各自的登记行。

**练习 3**：为什么 v0.4 发布博客（[L118](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L118)）能作为本主题的「权威佐证」？

> **参考答案**：因为它的标题把「Cache-Aware Load Balancer」明列为 v0.4 的三大特性之一，等价于官方对「这个特性已正式发布」的书面背书。两份 router 幻灯片（11-16 与 01-15）分别落在 v0.4 发布（12-04）的前后，正好串起「预告 → 发布 → 再宣讲」的成熟曲线，博客是中间那块最硬的锚点。

---

### 4.2 分布式权重热更新（Update Weights From Distributed）

#### 4.2.1 概念说明

**分布式权重热更新（Update Weights From Distributed）** 要解决的问题是：**一个已经在对外服务的推理集群，当模型权重需要更新时，怎样不重启、不停机地换上新权重？**

为什么这是个问题？因为现代大模型动辄几十到上百 GB，朴素做法是「停服务 → 从磁盘把新权重复制进每张卡的显存 → 重启」，这个**冷启动（cold start）** 过程动辄几十秒到几分钟，期间整个集群无法服务。在「权重很少变」的普通在线服务里这无所谓；但在「**权重频繁变**」的场景（典型就是 RLHF，每训练一步就变一次，见 4.3），反复冷启动会让训练根本跑不动。

「分布式」三个字强调的是：权重要**一次性分发到集群里所有副本、所有 GPU rank**，让它们原子地切换到同一份新权重，对外保持一致。本讲对应的幻灯片 `update-weights-from-distributed.pdf` 就是讲这件事（[L98](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L98)）。

#### 4.2.2 核心流程

把一次「分布式权重热更新」的流程画出来（直觉流程，**待本地验证**：具体 API 名与参数以幻灯片/主仓库代码为准）：

```
新权重就绪（例如由训练端产出，常驻在分布式存储 / trainer 显存里）
  │
  ▼
① 调用 update_weights 接口，把新权重的来源告诉每个 worker
  │
  ▼
② 各 worker / 各 GPU rank 从分布式来源读取新权重，写入新显存区
  │
  ▼
③ 原子地把推理指针切换到新权重（旧权重随后回收）
  │
  ▼
④ 对外服务不中断（或仅极短切换），继续用新权重处理请求
```

关键在于「**从分布式来源读**」而非「**从本地磁盘读 + 重启进程**」：跳过了进程重启和磁盘冷读，把「换权重」的时间从分钟级压到秒级甚至更低。配合第 4.1 的 Router，**权重切换期间 Router 还能继续把流量分到健康副本**，对外做到近乎无感更新。

> 小结一句：权重热更新 = **不停机** + **分布式广播** + **原子切换**，把「换模型」从「重启大工程」变成「一次 API 调用」。

#### 4.2.3 源码精读

**证据一：README 把它登记为 Biweekly 正式分享。**

[README.md:98](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L98) —— `[2024-11-30] [Update Weights From Distributed](slides/update-weights-from-distributed.pdf)`，登记在 `### SGLang Biweekly Meeting` 子区段下（[L92](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L92)）。标题「**From Distributed**」直接点明它的核心：权重不是从本地来，而是从**分布式来源**来。

**证据二：它有同日期的 YouTube 配套录像。**

[README.md:172](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L172) —— `[2024-11-30] [SGLang Developer Sync 20241130](https://www.youtube.com/watch?v=CcdGb310KWU)`。这是 [u1-l3](u1-l3-readme-navigation.md) / [u2-l2](u2-l2-scheduler-performance.md) 反复强调的「**按日期配对找录像**」技巧：Biweekly 的每份幻灯片，都去 Videos 区段（[L164](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L164) 的 `### SGLang Biweekly Meeting`）按**同日期**找配套的 Developer Sync——这份权重热更新幻灯片对应的，正是 20241130 那一期。

**证据三：它在时间线上紧挨着 Router，并与 v0.4 同期。**

把 4.1 和 4.2 的资料按日期排成一条线：

```
2024-11-16  SGLang Router (Biweekly)            ← L100, 配套 Dev Sync 20241116 (L174)
     │
2024-11-30  Update Weights From Distributed      ← L98,  配套 Dev Sync 20241130 (L172)
     │
2024-12-04  v0.4 发布（含 Cache-Aware Load Balancer）  ← L118 博客
     │
2025-01-15  Cache-Aware Load Balancer (Hyperbolic) ← L62
     │
2025-04-22  Optimizing Large Scale RLHF with SGLang ← L94（见 4.3）
```

这条线说明：**Router（11-16）与权重热更新（11-30）几乎同时成熟、一起随 v0.4 发布（12-04），半年后被组合用于大规模 RLHF（04-22）**——这正是下一节 4.3 要讲的故事。

#### 4.2.4 代码实践（调用链跟踪）

这是一个**调用链/流程跟踪型实践**——对照 4.2.2 的四步流程，理解一次权重热更新「谁在干什么」。

1. **实践目标**：能复述一次分布式权重热更新从「新权重就绪」到「对外恢复服务」经历的步骤，并说清为什么它比冷启动快。
2. **操作步骤**：打开 [slides/update-weights-from-distributed.pdf](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/slides/update-weights-from-distributed.pdf)，配合 [README.md:172](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L172) 的 YouTube 录像（先听讲解再看 PDF），给 4.2.2 的四步各填一句「具体在做什么」：

   | 步骤 | 在做什么（你来填） | 比冷启动省在哪 |
   |------|------------------|---------------|
   | ① 触发更新 | ？ | 不用人工停服务 |
   | ② 分布式读取 | ？ | 不用从本地磁盘慢读 |
   | ③ 原子切换 | ？ | 不用重启进程 / 重建 KV 结构 |
   | ④ 恢复服务 | ？ | 请求几乎不中断 |

3. **需要观察的现象**：整套流程里**没有任何一步**要求「停掉推理进程、重新加载全部权重」。
4. **预期结果**：你能解释「热」就「热」在**进程不重启、显存不冷读**——新权重走分布式通道进显存，切换是指针级的。（幻灯片具体页码与 API 名**待本地验证**；若无法打开 PDF，可退而看 YouTube 20241130 录像。）
5. **延伸思考**：如果集群有几十个副本，切换瞬间若有请求正好在跑，Router（4.1）能怎么帮忙？提示：把流量暂时导到「尚未切换」或「已切换完成」的健康副本，避免半新半旧的副本接客。

#### 4.2.5 小练习与答案

**练习 1**：为什么标题特意叫「Update Weights **From Distributed**」，强调「From Distributed」？

> **参考答案**：因为它区别于「从本地磁盘加载权重」的朴素做法——新权重来自**分布式来源**（例如 trainer 端、分布式存储），各副本/各 GPU rank 直接从该来源读取并原子切换，从而跳过进程重启与磁盘冷读，把换权重的时间从分钟级压到秒级。这正是「热」更新的关键。

**练习 2**：冷启动（停服务 + 重载权重 + 重启）和热更新，在 RLHF 训练里差别有多大？

> **参考答案**：RLHF 每训练一步权重就变一次，可能成百上千步。若每步都冷启动（每次几十秒到几分钟），训练会被反复重启彻底拖垮、根本跑不动；热更新把每步换权重压到秒级甚至更低，且不中断对外采样，才让「频繁换权重」的大规模 RLHF 在工程上可行。差别是「能不能跑起来」vs「完全跑不动」。

**练习 3**：去 Videos 区段按日期找，`update-weights-from-distributed.pdf` 配套的 YouTube 录像是哪一期？

> **参考答案**：幻灯片日期是 `[2024-11-30]`，到 Videos 区段的 Biweekly 子段按同日期找，对应 [L172](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L172) 的「SGLang Developer Sync 20241130」。这是「Biweekly 幻灯片按日期配对录像」检索法的又一次应用。

---

### 4.3 RLHF 大规模训练场景：把 Router 与权重热更新拼起来

#### 4.3.1 概念说明

前两节分别讲了「**Router（多副本分发）**」和「**权重热更新（不停机换权重）**」这两块拼图。本节把它们放进一个真实的高价值场景——**大规模 RLHF 训练**——看它们为什么要一起出现。

大规模 RLHF 训练里，有一类「推理」工作叫 **rollout（采样生成）**：用**当前这一步的策略模型（policy）**去生成大量回答样本，供训练端算奖励、更新权重。它的两个特征正好踩中前两节的痛点：

1. **权重每一步都在变** → 必须用「**权重热更新**」（4.2）把新策略权重不停机地推进推理集群。
2. **需要海量并行采样** → 要起**很多推理副本**，于是必须有「**Router**」（4.1）来分发采样请求、并尽量复用前缀缓存。

此外，RLHF 通常还要同时服务多个不同角色的模型——**策略模型（policy/actor）**、**参考模型（reference）**、有时还有**奖励/评论家模型（reward/critic）**——它们权重各异、更新节奏不同，对「多副本 + 热更新」的需求只会更强烈。README 把这场分享登记为 Biweekly 的 `[2025-4-22] [Optimizing Large Scale RLHF with SGLang]`（[L94](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L94)），它是 Router + 权重热更新这套基础设施的「**成果汇报**」。

#### 4.3.2 核心流程

把 RLHF 一步训练里「推理侧」的工作画成循环（直觉流程，**待本地验证**：具体规模与参数以幻灯片为准）：

```
            ┌──────────────── RLHF 训练循环（每一步）────────────────┐
            │                                                          │
            ▼                                                          │
  ① 训练端产出新策略权重                                               │
            │                                                          │
            ▼                                                          │
  ② update_weights（4.2）：把新权重不停机地推进 N 个推理副本            │
            │                                                          │
            ▼                                                          │
  ③ Router（4.1）：把海量 rollout 请求分发到各副本，复用前缀缓存         │
            │                                                          │
            ▼                                                          │
  ④ 各副本用新权重生成样本（rollout）                                   │
            │                                                          │
            ▼                                                          │
  ⑤ 样本回送训练端 → 算奖励 → 更新权重 → 回到 ①                        │
            └──────────────────────────────────────────────────────────┘
```

两个本讲特性在这条循环里各管一段：**②靠权重热更新**保证「换权重不停机」，**③靠 Router**保证「海量采样高效分发」。少了任何一个，循环都会断——没有热更新，②变成分钟级冷启动，循环卡死；没有 Router，③变成无缓存协调的野蛮分发，rollout 吞吐塌方。

#### 4.3.3 源码精读

**证据一：README 把「大规模 RLHF」作为 Biweekly 的专门分享登记。**

[README.md:94](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L94) —— `[2025-4-22] [Optimizing Large Scale RLHF with SGLang](https://gamma.app/docs/Optimizing-Large-Scale-RLHF-with-SGLang-dc69w8usckezkcu)`，登记在 `### SGLang Biweekly Meeting` 子段下（[L92](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L92)）。注意它是**外链**（`https://gamma.app/...`，托管在 Gamma 上的在线幻灯片），不是仓库内 PDF——这与 4.1/4.2 两份仓库内 PDF 不同，符合 [u1-l3](u1-l3-readme-navigation.md) 的「**内链 vs 外链**」判断：`slides/` 开头是内链，`https://` 开头是外链。它是本讲两块拼图（Router + 权重热更新）落地到 RLHF 的总结性资料。

**证据二：三个条目构成一条完整的「基础设施 → 场景落地」资料簇。**

把本讲三处证据排成资料簇，能看到清晰的因果递进：

| 日期 | 资料 | 角色 | README 行 |
|------|------|------|-----------|
| 2024-11-16 | `sglang_router.pdf`（Router 概念） | 基础设施① | [L100](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L100) |
| 2024-11-30 | `update-weights-from-distributed.pdf`（热更新） | 基础设施② | [L98](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L98) |
| 2024-12-04 | v0.4 博客（Cache-Aware Load Balancer 正式发布） | 正式发布锚点 | [L118](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L118) |
| 2025-01-15 | `sglang-router.pdf`（Cache-Aware 再宣讲） | 基础设施①细化 | [L62](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L62) |
| 2025-04-22 | 「Optimizing Large Scale RLHF with SGLang」 | **场景落地（两块拼图合一）** | [L94](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L94) |

读法：先读 11 月两份 Biweekly 幻灯片（基础设施），再用 12 月 v0.4 博客确认它们已正式发布，最后读 04 月的 RLHF 分享看它们如何组合落地。

#### 4.3.4 代码实践（场景价值链填表）

这是一个**源码阅读 + 归纳型实践**——把 Router 与权重热更新对 RLHF 的价值填成一张因果表。

1. **实践目标**：能说清「Router」和「权重热更新」分别解决了大规模 RLHF 的哪个痛点，以及少了某一块会怎样。
2. **操作步骤**：打开 [README.md:94](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L94) 指向的「Optimizing Large Scale RLHF with SGLang」（Gamma 在线幻灯片），结合本讲 4.1、4.2，把下表填完（已给骨架）：

   | RLHF 痛点 | 靠哪个特性解决 | 它具体做了什么 | 少了它会怎样 |
   |----------|---------------|---------------|-------------|
   | 每步权重都变，频繁重启受不了 | 权重热更新（4.2） | ？ | ？ |
   | 海量 rollout 要并行采样 | Router（4.1） | ？ | ？ |
   | 多角色模型（policy/reference/critic）并存 | 多副本 + 热更新 | ？ | ？ |

3. **需要观察的现象**：每个痛点都能被本讲的两块拼图之一（或组合）接住。
4. **预期结果**：你能用一句话总结——**「权重热更新」让 RLHF 每步换权重不停机，「Router」让海量采样高效分发并复用缓存，二者合起来才让上万卡的 RLHF 训练在工程上跑得动**。（Gamma 幻灯片具体内容**待本地验证**；若无法联网打开，可依据本讲转述要点作答并标注「未直接打开，要点待复核」。）
5. **延伸**：在 Gamma 幻灯片里留意是否提到 policy / reference / critic 等多模型的部署形态，以及单步 rollout 的吞吐数字。

#### 4.3.5 小练习与答案

**练习 1**：为什么说 Router 和权重热更新是大规模 RLHF 的「两块拼图」，缺一不可？

> **参考答案**：RLHF 的 rollout 阶段同时有两个特征——「权重每步都变」和「需要海量并行采样」。权重热更新解决前者（不停机换权重），Router 解决后者（多副本高效分发 + 缓存复用）。少了热更新，每步都要冷启动，循环卡死；少了 Router，海量采样无缓存协调、吞吐塌方。所以二者必须一起出现。

**练习 2**：`[2025-4-22] Optimizing Large Scale RLHF with SGLang`（[L94](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L94)）是仓库内 PDF 还是外链？怎么判断？

> **参考答案**：是**外链**（`https://gamma.app/docs/...`，托管在 Gamma）。判断依据是 [u1-l3](u1-l3-readme-navigation.md) 的规则：链接以 `https://` 开头是外链，以 `slides/`、`./` 等相对路径开头才是仓库内资产。这与本讲另外两份仓库内 PDF（`sglang_router.pdf`、`update-weights-from-distributed.pdf`）形成对照。

**练习 3**：RLHF 训练里为什么常常要同时服务 policy、reference 等多个模型？这给「多副本 + 热更新」带来什么要求？

> **参考答案**：RLHF 算奖励时通常要用「参考模型」做 KL 约束（防止策略模型跑偏太远），所以推理侧要同时跑 policy 和 reference（有时还有 reward/critic）。这些模型权重不同、更新节奏不同（reference 常冻结、policy 每步变），于是需要**更多副本**（Router 来分发）和**独立的热更新通道**（分别换各自的权重）。规模越大，对这两块基础设施的要求越高。

---

## 5. 综合实践

这是本讲规格指定的主实践，分两部分：**①整理两份 router 幻灯片的差异；②说明权重热更新对大规模 RLHF 训练的价值。**

### 任务一：两份 router 幻灯片辨析表

依据 [README.md](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md) 的文字登记（必要时打开两份 PDF 核对），把下表填全。骨架已给出，请补齐「**活动性质**」「**标题侧重点**」「**配套录像**」三列：

| 对照项 | `sglang_router.pdf`（下划线） | `sglang-router.pdf`（连字符） |
|--------|------------------------------|------------------------------|
| 文件名差异 | 下划线 `_` | 连字符 `-` |
| 登记日期 | `[2024-11-16]` | `[2025-01-15]` |
| README 行号 | [L100](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L100) | [L62](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L62) |
| 所在分区 | `### SGLang Biweekly Meeting` | `### Hyperbolic in-person meetup` |
| 活动性质 | ？（线上双周例会？） | ？（线下聚会？） |
| 标题侧重点 | ？ | ？ |
| 配套录像 | ？（[L174](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L174)？） | ？ |

填完后用一句话结论：**哪一份更接近 v0.4 正式发布后的「成品化」讲解？为什么？**（提示：看日期与 v0.4 博客 [L118](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L118) 的先后，以及标题是否精确到「Cache-Aware」。）

### 任务二：权重热更新对大规模 RLHF 的价值

写一段 150–300 字的说明，要求：

1. 先点出 RLHF rollout 阶段的两个特征（**权重每步都变**、**海量并行采样**）。
2. 说明「**权重热更新**」如何把「每步换权重」从分钟级冷启动压到秒级、且不中断对外采样。
3. 顺带点一句「**Router**」在其中的角色（分发海量采样、复用缓存）。
4. 结论落到：**为什么没有权重热更新，大规模 RLHF 就「跑不动」**。
5. 标注哪些是你在 [README.md:94](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L94) 的 RLHF 资料（或配套录像）里**亲眼读到**的，哪些是本讲**推断**的。保持诚实，便于复查。

> 说明：本实践是「源码阅读 + 归纳写作」型，不需要运行任何程序（本仓库无可运行脚本）。若无法打开 PDF / Gamma 幻灯片，可退而依据本讲转述的要点作答，并标注「未直接打开资料，要点待复核」。

---

## 6. 本讲小结

- **Router = 缓存感知负载均衡器**：当推理服务要起多副本时，它挡在前面，把请求分给「最可能已缓存该前缀」的副本，最大化 RadixAttention 复用、减少重复 prefill——比朴素 round-robin 更省算力。
- **两份文件名几乎一样的 router 幻灯片要分清**：`sglang_router.pdf`（下划线，[2024-11-16] Biweekly，「SGLang Router」）是早期概念引入；`sglang-router.pdf`（连字符，[2025-01-15] Hyperbolic，「Cache-Aware Load Balancer in SGLang」）是更晚、聚焦缓存感知的细化版。
- **v0.4 是本主题的权威锚点**：[L118](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L118) 的 v0.4 发布博客把「Cache-Aware Load Balancer」明列为三大特性之一，夹在两份 router 幻灯片之间，串起「预告 → 发布 → 再宣讲」的成熟曲线。
- **分布式权重热更新 = 不停机换权重**：通过「从分布式来源读取 + 原子切换」，跳过进程重启与磁盘冷读，把换权重从分钟级压到秒级；标题「From Distributed」（[L98](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L98)）点明了它的核心。
- **Biweekly 幻灯片按日期配对录像**：router（11-16）与权重热更新（11-30）在 Videos 区段都有同日期的 Developer Sync（[L174](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L174)、[L172](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L172)），这是反复用到的检索技巧。
- **二者合起来托起大规模 RLHF**：RLHF 的 rollout 阶段「权重每步都变 + 海量并行采样」，正好被权重热更新（不停机换权重）与 Router（高效分发 + 缓存复用）分别接住；[L94](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L94) 的「Optimizing Large Scale RLHF with SGLang」就是这套基础设施的成果汇报。

## 7. 下一步学习建议

- 本单元（u2）到此结束，进入 [u3 进阶：深度阅读与硬件适配](u3-l1-meetup-blog-reading.md)。建议从 [u3-l1 精读一篇 meetup 回顾博客](u3-l1-meetup-blog-reading.md) 开始，它教你怎么从一篇长博客里一次提炼多个项目（SGLang / XGrammar / FlashInfer / MLC-LLM）的要点。
- 想看「共享 KV Cache」的**安全副作用**，强烈推荐 [u3-l3 安全与边界：KV Cache 侧信道资料](u3-l3-kv-cache-side-channel.md)——本讲 [L100](https://github.com/sgl-project/sgl-learning-materials/blob/160433e8779cb38c4a894674474f67a85bc8915d/README.md#L100) 里和 Router 同行的「Side-Channel KV Cache Attack」就在那里详讲，正好补上「缓存复用是双刃剑」的另一面。
- 想深入 Router 与权重热更新的**实现代码**，它们**不在本仓库**——本仓库只是路标。请到主仓库 `sgl-project/sglang`，以 `sglang_router`（router 子项目）与 `update_weights` / `update_weights_from_distributed` 等接口名为入口检索源码。
- 想把「多副本 + 路由」放在更完整的部署画面里看，可回看 [u2-l5 大规模部署：EP 与 PD 分离](u2-l5-large-scale-ep.md)：那里讲的是「单集群内部怎么跑 DeepSeek」，本讲则是「多副本之间怎么分发与更新」，两者合起来才是生产级服务全貌。
- 复习时可回看 [u2-l4 DeepSeek MLA](u2-l4-deepseek-mla.md)：本讲 Router 的「缓存感知」依赖 RadixAttention 对 KV Cache 前缀的复用，而 MLA 又是压缩 KV Cache 的关键——三讲串起来是「压缩缓存 → 复用缓存 → 跨副本协调缓存」的完整链条。
