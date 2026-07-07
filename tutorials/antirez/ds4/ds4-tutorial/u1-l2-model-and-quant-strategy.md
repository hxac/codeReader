# DeepSeek V4 模型背景与量化策略

## 1. 本讲目标

本讲是「认识 DwarfStar」单元的第二篇。上一篇（u1-l1）讲清楚了 ds4「是什么、为什么做」，本讲要回答一个更具体的问题：**ds4 到底在跑一个什么样的模型，它为什么能把一个几百上千亿参数的大模型塞进一台个人电脑？**

读完本讲，你应该能够：

1. 说清 DeepSeek V4 **Flash** 与 **PRO** 两个模型的差别，以及它们各自对应什么样的内存档位。
2. 说清 ds4 的「**非对称量化**」策略——为什么只把 routed MoE 专家压到 2bit/4bit，而 shared 专家、投影层、路由层完全不动。
3. 理解 **MTP（多 token 预测）投机解码**与**长上下文（最长 1M token）**对本地推理意味着什么。
4. 看懂 README 的下载脚本与速度表，能根据自己的机器选出正确的 GGUF。

本讲只读两个核心文档：`README.md` 与 `MODEL_CARD.md`，并补充几处量化工具源码作为佐证。我们刻意不深入量化算法的实现细节——那是后续讲义 u3-l4 的主题。本讲只讲「**为什么这么选**」。

## 2. 前置知识

本讲面向初学者，但有几个术语必须先建立直觉。如果你已经熟悉，可以跳过本节。

- **参数（parameter）**：神经网络里可学习的数值。模型「大小」通常用参数个数衡量，例如 284B 就是约 2840 亿个参数。每个参数在内存里占多少字节，取决于**精度**。
- **精度 / 比特宽度（bit-width）**：一个参数用几位来存储。FP16 用 16 位（2 字节），FP8 用 8 位（1 字节），2bit 量化约用 2 位。位数越少，模型越小、推理越快，但精度损失越大。
- **量化（quantization）**：把高精度参数压缩成低精度的过程。本讲的「2bit / 4bit」就是量化目标。
- **MoE（Mixture of Experts，混合专家）**：一种把一个大模型拆成很多「专家」子网络的结构。每个 token 只激活其中少数几个专家。这样模型**总参数**可以很大，但每次推理**实际激活（active）的参数**很小。DeepSeek V4 Flash 总参数 284B，但每步只激活约 13B。
- **routed 专家 vs shared 专家**：DeepSeek V4 的 MoE 里，由路由器（router）**按需挑选**的叫 routed 专家（占绝大多数体积）；**每层总是参与**的叫 shared 专家。后面会看到，这个区分是量化策略的关键。
- **imatrix（重要性矩阵）**：量化时给每一列权重打的一个「重要性分数」，告诉量化器哪些列不能压得太狠。后面会解释为什么 2bit 量化需要它。
- **KV 缓存（KV cache）**：推理时为了避免重复计算而保存的「历史注意力中间状态」。它随上下文长度增长，是长上下文推理的内存瓶颈。
- **MTP（Multi-Token Prediction，多 token 预测）**：一种「投机解码」加速技巧——用一个小模型一次猜好几个 token，再由主模型批量验证。本讲只讲它在 ds4 里的定位，机制留给后续讲义 u6-l2。

## 3. 本讲源码地图

本讲主要依据两个 Markdown 文档，外加两处量化工具源码做佐证：

| 文件 | 作用 | 本讲用到哪里 |
| --- | --- | --- |
| [MODEL_CARD.md](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/MODEL_CARD.md) | 官方 DeepSeek-V4-Flash 模型卡的摘要：模型家族、架构、精度、推理模式、benchmark | 模型规模、KV 缓存压缩结构、官方 FP4+FP8 精度策略 |
| [README.md](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md) | 项目主文档：动机、下载脚本、量化策略、速度表、SSD 流式、分布式、MTP | 量化策略、内存档位、MTP、速度数据 |
| [gguf-tools/quants.h](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/quants.h) | GGUF 量化类型的窄 API 头文件（类型枚举、是否需要 imatrix） | 佐证 IQ2_XXS / Q2_K / Q4_K 的类型编号与 imatrix 需求 |
| [gguf-tools/deepseek4-quantize.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c) | 离线 GGUF 生成/量化工具 | 佐证 imatrix 缺失时的合成 fallback |

> 提示：本讲引用源码时给出的链接是**永久链接**（基于固定 commit），点击会跳到 GitHub 上对应行。后续讲义会深入 `ds4.c` 等真正的引擎源码。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **Flash 与 PRO 的区别与内存档位** —— 先认识模型本身。
2. **2bit / 4bit 非对称量化策略** —— 再看 ds4 怎么把它塞进内存。
3. **MTP 与长上下文** —— 最后看它如何兼顾加速与超长对话。

---

### 4.1 Flash 与 PRO 的区别与内存档位

#### 4.1.1 概念说明

DeepSeek V4 是一个**预览版模型家族**，里面有两个 MoE 语言模型：

- **DeepSeek-V4-Flash**：较小、较高效的模型，是 ds4 的**首要目标**。
- **DeepSeek-V4-Pro**：更大的模型，只在**极高内存机器**上支持。

两者都支持 **1M（百万）token 的上下文长度**。理解它们的关键是「**总参数 vs 激活参数**」的区分：MoE 模型可以把总参数做得很大（知识容量大），同时让每次推理只激活一小部分（速度快、显存占用低）。

ds4 的设计前提是「**本地高端机器优先，从 96/128GB 内存起步**」。所以选哪个模型、用哪个 GGUF，完全由你机器的内存档位决定。这点非常重要：**ds4 不是一个能跑任意 GGUF 的通用 runner**，它只跑为本项目发布的 DeepSeek V4 Flash/PRO GGUF。

#### 4.1.2 核心流程

把「模型选型」理解成一条单向决策链：

```text
机器内存档位
   │
   ├── 96 / 128 GB  ──► Flash，2bit 量化（q2-imatrix / q2-q4-imatrix）
   ├── ≥ 256 GB     ──► Flash，4bit 量化（q4-imatrix）
   ├── 512 GB 单机  ──► PRO，2bit 量化（pro-q2-imatrix）
   └── 两台 512 GB  ──► PRO，4bit 量化，分布式层切分（pro-q4-layers00-30 / ...31-output）
```

记忆要点：

- **Flash 是默认选项**。绝大多数个人机器（MacBook Pro/Max 96/128GB、DGX Spark 128GB）都跑 Flash。
- **PRO 要么单台 512GB（2bit 流式），要么两台 512GB 拼起来（4bit 分布式）**。它不是给普通笔记本用的。
- 上下文越长，KV 缓存吃内存越多，所以「模型能塞下」不等于「还能开 1M 上下文」。

#### 4.1.3 源码精读

模型规模来自 `MODEL_CARD.md` 的「Model Family」表：

[MODEL_CARD.md:14-22](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/MODEL_CARD.md#L14-L22) —— 官方模型卡列出的两个模型规模：Flash 284B 总参 / 13B 激活，PRO 1.6T 总参 / 49B 激活，两者都是 1M 上下文。

```text
| Model             | Total parameters | Active parameters | Context length |
| DeepSeek-V4-Flash | 284B             | 13B               | 1M tokens      |
| DeepSeek-V4-Pro   | 1.6T             | 49B               | 1M tokens      |
```

注意 PRO 的总参数（1.6T）是 Flash（284B）的近 6 倍，但激活参数只从 13B 涨到 49B（约 3.7 倍）。这正是 MoE 的价值：**知识量随总参数线性增长，而单步算力只随激活参数增长**。

ds4 优先做 Flash 的理由，README 的「Motivations」写得很直白：

[README.md:33-36](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L33-L36) —— 说明 Flash「接近前沿」、PRO 更好、两者都「非常耐 2bit 量化」，并且这类几百亿模型严格优于更小的（哪怕是稠密的）模型。

把模型规模换算成内存档位，看 README 的下载脚本注释：

[README.md:105-119](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L105-L119) —— 四个主要下载目标，每行注释直接写明对应的内存档位。

```sh
./download_model.sh q2-imatrix      # 96/128 GB RAM machines, imatrix-tuned q2
./download_model.sh q2-q4-imatrix   # 96/128 GB RAM machines, q2 with last 6 layers q4
./download_model.sh q4-imatrix      # >= 256 GB RAM machines, imatrix-tuned q4
./download_model.sh pro-q2-imatrix  # 512 GB RAM machines, PRO q2 imatrix quant
```

而真实内存占用，README 给了一组很关键的经验数字：

[README.md:825-832](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L825-L832) —— 2bit Flash GGUF 本体约 81GB；1M 上下文还要再叠加约 26GB（其中压缩 indexer 单独就约 22GB）。所以在 128GB 机器上，「模型 + 满上下文」会超内存，建议上下文开 100~300k。

这段话隐含了一个本讲贯穿的逻辑：**模型权重和 KV 缓存是两笔独立的内存开销**。选 GGUF 决定第一笔，选 `--ctx` 决定第二笔。

#### 4.1.4 代码实践

**实践目标**：根据机器内存，挑出正确的 GGUF。

**操作步骤**：

1. 打开 [README.md:105-119](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L105-L119) 的下载目标列表。
2. 对三种机器配置，写下你会选哪个 `download_model.sh` 目标：
   - 一台 128GB 的 MacBook Pro M5 Max
   - 一台 256GB 的工作站
   - 一台 512GB 的 Mac Studio M3 Ultra（单机）

**需要观察的现象 / 预期结果**：

| 机器 | 推荐目标 | 理由（一句话） |
| --- | --- | --- |
| 128GB M5 Max | `q2-imatrix`（或 `q2-q4-imatrix`） | 2bit Flash 约 81GB，128GB 装得下并留出 KV 与系统内存 |
| 256GB 工作站 | `q4-imatrix` | 内存充裕，可上 4bit Flash，质量更好 |
| 512GB 单机 | `pro-q2-imatrix` | 只有 512GB 才放得下 PRO 的 2bit（约 6 倍 Flash 体积） |

> 如果你的机器内存不在这几档（例如 64GB），单机跑不下完整 Flash，那就要进入 SSD 流式模式——那是后续讲义 u9-l1/u9-l2 的内容，本讲不展开。

#### 4.1.5 小练习与答案

**练习 1**：Flash 的总参数是 284B、激活参数 13B。这句话的「激活参数」为什么比「总参数」小这么多？

**参考答案**：因为 Flash 是 MoE 模型，每个 token 只由路由器挑出的少数 routed 专家 + shared 专家处理，绝大多数专家在当步休眠。所以「总参数」代表知识容量，而「激活参数」代表每步实际算力与显存读写量。

**练习 2**：为什么 README 说在 128GB 机器上「2bit quants 已经 81GB」，所以满 1M 上下文不现实？

**参考答案**：模型权重（81GB）和 KV 缓存（1M 约 26GB）是两笔**叠加**的内存开销，加起来超过 100GB，再算上系统、图计算 scratch 就会爆内存。所以正确做法是选小一点的 `--ctx`（如 100~300k）。

---

### 4.2 2bit / 4bit 非对称量化策略

#### 4.2.1 概念说明

把 284B 参数的 Flash 塞进 128GB 机器，光靠 FP8（1 字节/参数，约 284GB）远远不够。ds4 的核心招数是 **2bit / 4bit 量化**。但它的做法很特别——叫做「**非对称量化（asymmetrical quantization）**」：

> **只把 routed MoE 专家压到 2bit，其它一切保持高精度。**

具体来说，根据 README：

- routed 专家的 **up/gate** 投影用 `IQ2_XXS`（约 2bit）。
- routed 专家的 **down** 投影用 `Q2_K`（2bit K-量化）。
- **shared 专家、所有投影层（projection）、路由层（routing）一律不量化**，保持高精度。

为什么这么分？因为 routed 专家**占了模型体积的绝大多数**（MoE 的本质就是专家多），把它压下去收益最大；而 shared 专家、投影、路由每层都要经过、对每个 token 都关键，压狠了质量立刻崩。这是一个「**把省下来的比特预算花在最不影响质量的地方**」的工程取舍。

这个取舍其实和**官方精度**高度一致。官方 DeepSeek-V4 的 instruct 模型本身就是「FP4 + FP8 Mixed」：**FP4 用于 MoE 专家参数，FP8 用于其它参数**。ds4 只是把这个思路推得更极端（专家用 2bit，其它甚至更高精度）。

为什么 2bit 还能用？README 直接给出承诺：2bit 量化「**不是开玩笑的**——它们表现良好、能在编码 agent 下工作、可靠地调用工具」。

#### 4.2.2 核心流程

量化决策可以画成一个「**预算分配**」问题。设模型总参数 \(P\)，其中 routed 专家参数 \(P_{\text{exp}}\)，其它参数 \(P_{\text{other}}\)。若用统一 \(b\) 比特量化，体积约为：

\[
\text{size}(b) \approx \frac{b}{8}\,(P_{\text{exp}} + P_{\text{other}})
\]

但 ds4 用非对称比特 \(b_{\text{exp}} < b_{\text{other}}\)：

\[
\text{size} \approx \frac{b_{\text{exp}}}{8}P_{\text{exp}} \;+\; \frac{b_{\text{other}}}{8}P_{\text{other}}
\]

由于 MoE 模型里 \(P_{\text{exp}} \gg P_{\text{other}}\)（专家占绝大多数体积），把 \(b_{\text{exp}}\) 从 8 降到 2（省 75%）几乎决定了整体体积，而把 \(b_{\text{other}}\) 保持在高位对总体积影响很小、对质量保护却很大。这就是非对称量化的全部直觉。

配套流程：

```text
GGUF 生成时
   │
   ├── routed MoE 专家：IQ2_XXS (gate/up) + Q2_K (down)   ← 需要 imatrix
   ├── shared 专家 / 投影 / 路由：保持原精度
   └── （4bit 版本）把 routed 专家整体换成 Q4_K，需要更大内存
```

一个关键细节：`IQ2_XXS` 这种 2bit 格式**需要 imatrix（重要性矩阵）**，因为它要在极低比特下区分「哪一列更重要，不能压扁」。`Q4_K` 这类 K-量化格式自带「块缩放 + 最小值」，对 imatrix 依赖弱得多。后续讲义 u3-l4 会讲具体的 block 结构，这里只记住结论即可。

#### 4.2.3 源码精读

量化策略的权威描述在 README 的「Model Weights」段：

[README.md:99-103](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L99-L103) —— 明确说明 2bit 量化是「非常不对称的」：**只量化 routed MoE 专家**，up/gate 用 `IQ2_XXS`，down 用 `Q2_K`；它们占模型体积的绝大多数；shared 专家、投影、路由**保持不动以保证质量**。

```text
The 2 bit quants use a very asymmetrical quantization: only the routed MoE
experts are quantized, up/gate at `IQ2_XXS`, down at `Q2_K`. They are
the majority of all the model space: the other components (shared experts,
projections, routing) are left untouched to guarantee quality.
```

这个策略和官方精度哲学同源：

[MODEL_CARD.md:80-87](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/MODEL_CARD.md#L80-L87) —— 官方 instruct 模型用「FP4 + FP8 Mixed」：**FP4 用于 MoE 专家参数，FP8 用于其它大部分参数**。ds4 的「只压 routed 专家」正是把官方「MoE 专家用低精度」这一条推到了 2bit。

这些量化格式在工具源码里有对应类型编号，佐证它们是真实存在的格式：

[gguf-tools/quants.h:27-33](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/quants.h#L27-L33) —— 量化类型枚举里，`Q2_K = 10`、`Q4_K = 12`、`IQ2_XXS = 16`，注释说明这些枚举值「**故意与 GGUF/GGML 类型 ID 对齐**」，所以模板元数据可以直接拷贝、无需翻译。

```c
    DS4Q_TYPE_Q2_K    = 10,
    ...
    DS4Q_TYPE_Q4_K    = 12,
    ...
    DS4Q_TYPE_IQ2_XXS = 16,
```

而「IQ2_XXS 需要 imatrix、Q4_K 不一定需要」可以在 API 层确认：

[gguf-tools/quants.h:64](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/quants.h#L64) —— 头文件暴露了 `ds4q_requires_imatrix(type)`，按类型查询「该格式是否需要 imatrix」。

[gguf-tools/deepseek4-quantize.c:17-19](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L17-L19) —— 工具注释明确：**当目标类型需要 imatrix 而用户没提供时**，会退回到「合成权重能量启发式」作为 fallback（这正是缺 imatrix 时的兜底）。

[gguf-tools/deepseek4-quantize.c:1117-1126](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L1117-L1126) —— fallback 的具体实现：没有 imatrix 时，用「每列权重的平方和」作为该列重要性，合成一份 imatrix 再交给量化器。

```c
    if (!im_ptr && ds4q_requires_imatrix(type)) {
        synthetic = xcalloc((size_t)ncols, sizeof(float));
        for (int64_t r = 0; r < nrows; r++) {
            const float *row = src + (size_t)r * (size_t)ncols;
            for (int64_t c = 0; c < ncols; c++) synthetic[c] += row[c] * row[c];
        }
        im_ptr = synthetic;
    }
```

这也解释了为什么 README 反复强调「**优先用 imatrix 版本**」（`q2-imatrix` 而非裸 `q2`）：真实采集的 imatrix 比这个合成 fallback 质量更好。imatrix 是怎么采集的，留给讲义 u11-l2。

#### 4.2.4 代码实践

**实践目标**：对照 MODEL_CARD 与 README，把「为什么只压 routed 专家」讲清楚，并理解 imatrix 的角色。

**操作步骤（源码阅读型实践）**：

1. 读 [MODEL_CARD.md:80-87](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/MODEL_CARD.md#L80-L87)：记下官方 instruct 模型「FP4 用于 MoE 专家、FP8 用于其它」。
2. 读 [README.md:99-103](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L99-L103)：记下 ds4「只压 routed 专家，up/gate = IQ2_XXS，down = Q2_K，其余不动」。
3. 读 [gguf-tools/deepseek4-quantize.c:1117-1126](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L1117-L1126)：看 imatrix 缺失时的合成 fallback。
4. 用一段话回答：为什么 routed 专家被量化，而 shared 专家 / 投影 / 路由不被量化？

**需要观察的现象 / 预期结果**：你应该能写出类似下面的回答：

> routed 专家是 MoE 里**按需激活**的，每个 token 只用其中几个，且它们**占了模型体积的绝大多数**——把它们压到 2bit 几乎决定了整体体积，而单步只激活少数专家意味着量化误差不会全部叠加。shared 专家、投影、路由则**每层每 token 都必经**，对质量影响直接，且体积占比小，省它们得不偿失。这和官方「FP4 给 MoE 专家、FP8 给其它」是同一思路，ds4 只是更激进。

5. 进阶思考：`IQ2_XXS`（2bit）为什么非需要 imatrix 不可，而 `Q4_K` 不一定需要？

> **预期结果**：2bit 比特太少，必须靠 imatrix 标出「重要列」避免把它们压扁；`Q4_K` 这类 K-量化自带每块的 scale/min，自适应能力强，对 imatrix 依赖弱。源码佐证：`deepseek4-quantize.c` 只在「类型需要 imatrix 且未提供」时才触发合成 fallback。

> 注：本实践是**源码阅读型**，不需要运行模型。如果你想真正生成一个 GGUF，那是讲义 u11-l1 的离线工具链任务，耗时很长。

#### 4.2.5 小练习与答案

**练习 1**：假设 Flash 里 routed 专家参数 \(P_{\text{exp}}\) 占总参数的 90%，其它占 10%。把 routed 专家从 8bit 压到 2bit、其它保持 8bit，总体积大约变成原来的多少？

**参考答案**：用本节公式，
\[
\text{新体积} = \tfrac{2}{8}\cdot 0.9P + \tfrac{8}{8}\cdot 0.1P = 0.225P + 0.1P = 0.325P
\]
即约为原来的 32.5%（约省 2/3）。这正是 2bit 能把 284B 模型塞进 81GB 的原因。

**练习 2**：README 反复说「优先用 imatrix 版本」（如 `q2-imatrix`）。请用本节源码解释，如果不用 imatrix 会发生什么。

**参考答案**：`IQ2_XXS` 需要 imatrix（见 `quants.h` 的 `ds4q_requires_imatrix`）。没提供时，`deepseek4-quantize.c:1117-1126` 会用「每列权重平方和」合成一份 fallback imatrix。合成版用的是**权重自身能量**而非**真实激活统计**，质量不如真实 imatrix，所以官方推荐优先用 imatrix 版本。

**练习 3**：`q2-q4-imatrix` 这个目标的注释是「q2 with last 6 layers q4」。为什么要把**最后 6 层**单独换成 4bit？

**参考答案**（推理方向，待本地验证）：模型的最后几层（靠近输出 logits 的层）对最终预测影响最大，把它们的 routed 专家从 2bit 提到 4bit，可以低成本地提升输出质量，而只增加很少体积。这是一种「**尾部提精度**」的微调。具体增益需要本地 benchmark 验证。

---

### 4.3 MTP 与长上下文

#### 4.3.1 概念说明

ds4 的两个「加分项」直接影响使用体验：**长上下文**和 **MTP 投机解码**。它们解决的其实是两个不同的问题。

**长上下文（最长 1M token）**：DeepSeek V4 能开 100 万 token 的上下文，这对本地推理是巨大优势——可以喂进整本书、整个代码库或超长对话。它能做到这点，靠的是**压缩 KV 缓存**：不是给每个 token、每层都存一份完整 KV，而是用「滑动窗口 + 压缩 + 索引」的组合，把历史 KV 大幅压缩。这条特性还和 ds4 的核心理念「**KV 缓存是一等磁盘公民**」直接挂钩——压缩后的 KV 小到值得存盘复用，这是后续 u4-l2、u8-x 系列要深入的内容。

**MTP（Multi-Token Prediction）投机解码**：一种**实验性**加速手段。ds4 提供一个可选的小「draft」GGUF（仅 Flash），让模型一次猜好几个 token，再批量验证，猜对的就白赚。但 README 明确：当前 MTP 路径**仍处于实验阶段**，是「correctness-gated（正确性优先）」的，目前**最多只带来轻微提速**，还不是一个有意义的生成速度收益。

本讲的关键结论：**长上下文是 ds4 的硬实力（可用、推荐），MTP 是实验性尝鲜（可选、谨慎）**。初学者先把长上下文用好，MTP 留到进阶讲义再玩。

#### 4.3.2 核心流程

**压缩 KV 的层级结构**（来自 MODEL_CARD 架构描述）可以这样理解：

```text
每一层都有：
  1. raw 滑动窗口：最近 128 个 token 的完整高分辨率 KV（每层都有）
  2. 压缩 KV：把更早的历史按比例压缩成少量行
       ├── 偶数层（从第 2 层起）：ratio-4，每 4 token 压成 1 行，并带 indexer
       └── 奇数层（从第 3 层起）：ratio-128，每 128 token 压成 1 行，无 indexer
```

直观地说：最近的 128 token 记得清清楚楚（raw 窗口）；更早的内容按 4:1 或 128:1 的比例浓缩。ratio-4 层还多了一个「indexer」，在压缩历史太大时按 top-k（最多 512 行）挑出最相关的压缩行来 attend。这样 1M token 才不会爆显存。

压缩比的「时间轴」效果可以用一个粗略公式体会：若一段长为 \(L\) 的历史，在 ratio-\(r\) 层只占约 \(L/r\) 行 KV。所以 \(L=10^6\) 在 ratio-128 层约 \(7812\) 行，而不是 \(10^6\) 行。

**MTP 投机解码**的流程（高层）：

```text
1. draft 模型一次预测 k 个候选 token
2. 主模型对这 k 个 token 做一次（并行）验证
3. 置信度门控（--mtp-margin）：只接受高置信的连续前缀
4. 接受的 token 一次提交，拒绝处回退到正常自回归
```

注意第 3 步：ds4 用 `--mtp-margin` 做**置信度门控**，避免「部分接受但总体更慢」的情况。这也是它目前「只有轻微提速」的原因之一——门控偏保守。

#### 4.3.3 源码精读

压缩 KV 的层级规则来自 MODEL_CARD 架构段：

[MODEL_CARD.md:30-43](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/MODEL_CARD.md#L30-L43) —— 用表格说明每层如何压缩：第 0、1 层只有 raw 128-token 窗口；从第 2 层起的偶数层是 ratio-4（带 indexer）；从第 3 层起的奇数层是 ratio-128。一个 token 在压缩层同时 attend 最近 128 token 的 raw 窗口和更老的压缩历史。

[MODEL_CARD.md:45-50](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/MODEL_CARD.md#L45-L50) —— 区分两类压缩层：**ratio-4 是「选择性压缩注意力」**（带第二个 indexer 流，当压缩历史超过 top-k 时评分选出最多 512 行）；**ratio-128 是「重度压缩」**（无 indexer，直接用所有 ratio-128 压缩行）。

ds4 把这些固定为常量并从 GGUF 元数据校验：

[MODEL_CARD.md:53-65](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/MODEL_CARD.md#L53-L65) —— 固定实现常量：43 层、raw 滑动窗口 128 token、indexer heads 64、indexer head 维 128、indexer top-k 512；并指出这正是模型能暴露 1M 上下文而无需为每个 token 每层存全 KV 的**实际原因**。

```text
- Layers: 43
- Raw sliding-window attention: 128 tokens
- Indexer heads: 64
- Indexer head dimension: 128
- Indexer top-k: 512
```

这也解释了 4.1 里「1M 上下文约 26GB、indexer 单独约 22GB」——indexer 流就是 ratio-4 层多出来的那份压缩状态，它既是长上下文的功臣，也是内存大头。

MTP 在 README 里的定位：

[README.md:136-140](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L136-L140) —— MTP 是**可选**的投机解码 GGUF（仅 Flash），需用 `--mtp` 显式开启；当前路径**仍是实验性的**、correctness-gated，目前**最多带来轻微提速**，不是有意义的生成加速。

CLI 段落也重申了同样的警告：

[README.md:692-695](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L692-L695) —— `--mtp` / `--mtp-draft` 只对贪心解码有用，用 `--mtp-margin` 置信度门控避免慢的部分接受，应被视为**实验性轻微提速路径**。

#### 4.3.4 代码实践

**实践目标**：用 MODEL_CARD 的常量估算长上下文的 KV 节省，并理解 MTP 的实验性边界。

**操作步骤（源码阅读 + 推演型实践）**：

1. 读 [MODEL_CARD.md:53-65](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/MODEL_CARD.md#L53-L65)，记下 `ratio-4` / `ratio-128` 的含义与 1M 上下文的实现原因。
2. 读 [README.md:136-140](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L136-L140) 与 [README.md:692-695](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L692-L695)，记下 MTP 的实验性定位与 `--mtp-margin` 门控。
3. 做一个推演：在 ratio-128 层，1M（\(10^6\)）token 的历史大约会压成多少行 KV？

**需要观察的现象 / 预期结果**：

> 在 ratio-128 层，\(10^6 / 128 \approx 7812\) 行压缩 KV（再加最近 128 token 的 raw 窗口）。相比「每 token 一行」的 \(10^6\) 行，压缩了约 128 倍。这就是 1M 上下文可行的核心原因。ratio-4 层压缩更轻（约 \(10^6/4 = 250000\) 行），但靠 indexer top-k=512 进一步筛掉大部分。

4. 思考题：如果有人告诉你「我开了 MTP，生成快了一倍」，根据 README 你应该怎么回应？

> **预期结果**：根据 README 的明确表态，当前 MTP 路径**最多带来轻微提速**、不是有意义的生成加速。所以「快一倍」与官方描述不符——要么是测量误差（比如把 prefill 和生成混在一起），要么是更早/不同 commit 的行为。应建议用 `ds4-bench`（讲义 u10-l5）单独测生成 token/s 来复核。**待本地验证。**

> 注：本实践不需要真正跑 MTP（它需要单独下载 mtp GGUF 且只对贪心解码有效）。重点是建立「长上下文可用、MTP 实验性」的正确预期。

#### 4.3.5 小练习与答案

**练习 1**：DeepSeek V4 每一层的 raw 滑动窗口是 128 token。这一层同时还有压缩历史。一个 token 在压缩层 attend 的对象是什么？

**参考答案**：它同时 attend（1）最近 128 token 的 raw 高分辨率 KV，以及（2）更早历史的压缩 KV 行（ratio-4 或 ratio-128，取决于层奇偶）。raw 行和压缩行用相同的注意力/值维度，所以能被同一次混合注意力计算消费。

**练习 2**：为什么 ratio-4 层需要一个 indexer，而 ratio-128 层不需要？

**参考答案**：ratio-4 压缩较轻，长上下文下压缩行仍然很多，需要 indexer 在压缩历史超过 top-k（512）时**评分选出最相关的若干行**，否则 attend 成本太高。ratio-128 压缩极重，行数本身已经很少，直接全部 attend 即可，不需要 indexer。

**练习 3**：MTP 的 draft 状态会随磁盘 KV 一起持久化吗？根据你对 README「Disk KV Cache」段的阅读回答。

**参考答案**：不会。README 在「Disk KV Cache」段明确：**MTP draft logits/state 不持久化**，加载磁盘 checkpoint 后 draft 状态会被作废，由正常生成重建。这是因为 MTP 是临时加速状态，不是模型会话状态的一部分。细节见讲义 u6-l2 / u8-x。

---

## 5. 综合实践

把本讲三个模块串起来，做一个**「为我的机器选型并解释清楚」**的小任务。

**任务背景**：假设你的朋友有一台 **128GB 的 MacBook Pro M5 Max**，想用 ds4 跑 DeepSeek V4 做日常编码助手。他问你三个问题，请基于本讲内容回答（每题都要引用一处源码链接）：

1. 「我该下载哪个 GGUF？为什么不是 PRO？」
2. 「2bit 量化会不会蠢到没法用？它到底压了什么？」
3. 「我能开 1M 上下文吗？MTP 要不要开？」

**参考回答框架**（你可以基于此改写）：

1. 选 `q2-imatrix`（[README.md:105-119](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L105-L119)）。PRO 的总参数是 Flash 的近 6 倍（[MODEL_CARD.md:14-22](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/MODEL_CARD.md#L14-L22)），128GB 远远装不下 PRO，单机 PRO 至少要 512GB。
2. 不会。2bit 只量化 routed MoE 专家（up/gate = IQ2_XXS，down = Q2_K），shared 专家/投影/路由全保持原精度（[README.md:99-103](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L99-L103)）。这和官方「FP4 给 MoE 专家、FP8 给其它」同源（[MODEL_CARD.md:80-87](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/MODEL_CARD.md#L80-L87)），且 README 承诺它在编码 agent 下工作良好。
3. 不建议开满 1M。模型本体已约 81GB，1M 上下文还要再加约 26GB（indexer 单独约 22GB）（[README.md:825-832](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L825-L832)），128GB 机器开 100~300k 更稳。MTP 目前是实验性、最多轻微提速（[README.md:136-140](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L136-L140)），初学者可以先不开。

**进阶（可选）**：用本节的「预算分配公式」估算——如果把这台 128GB 机器换成 256GB，改用 `q4-imatrix` 后，模型本体大约会从 81GB 涨到多少？（提示：routed 专家比特从 2 翻到 4，而专家占绝大部分体积。）

## 6. 本讲小结

- DeepSeek V4 有 **Flash（284B/13B 激活）** 和 **PRO（1.6T/49B 激活）** 两个 MoE 模型，都支持 1M 上下文；Flash 是 ds4 首要目标，PRO 留给 512GB 级机器。
- 内存档位直接决定 GGUF 选型：**96/128GB → q2-imatrix**，**≥256GB → q4-imatrix**，**512GB 单机 → pro-q2-imatrix**。
- ds4 用**非对称量化**：只把 routed MoE 专家压到 2bit（up/gate = `IQ2_XXS`，down = `Q2_K`），shared 专家/投影/路由保持高精度；这与官方「FP4 给 MoE 专家、FP8 给其它」同源。
- `IQ2_XXS` 需要 imatrix，缺失时工具会用「列权重平方和」合成 fallback（`deepseek4-quantize.c:1117-1126`），所以 README 推荐「优先 imatrix 版本」。
- **长上下文**靠压缩 KV（raw 128 窗口 + ratio-4 带 indexer + ratio-128）实现，是 ds4 的硬实力；**MTP** 是实验性、correctness-gated 的轻微提速路径，初学者可先不开。
- 模型权重与 KV 缓存是**两笔叠加**的内存开销，选 GGUF 决定第一笔，选 `--ctx` 决定第二笔。

## 7. 下一步学习建议

本讲把「模型是什么、为什么这么量化」讲清楚了，但还没有进入一行 C 代码。建议接着：

- **u1-l3 目录结构与源码地图**：建立「哪个文件负责什么」的心智地图，为读源码做准备。
- **u1-l4 构建系统与多后端编译**：理解 Metal/CUDA/ROCm/CPU 四后端的构建方式。
- **u1-l5 下载模型与首次运行**：真正把模型跑起来，观察 prefill 与生成速度。
- 之后再进入 **u3-l4 量化格式与张量族**：那里会深入 `block_q8_K / q4_K / q2_K / iq2_xxs` 的具体结构与 dot product 参考实现，把本讲的「策略」落到「数学与字节布局」。

如果你对 imatrix 采集流程特别感兴趣，可以提前跳读 [gguf-tools/imatrix/README.md](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/imatrix/README.md)，那是讲义 u11-l2 的主题。
