# imatrix 收集与质量测试

## 1. 本讲目标

本讲回答两个紧挨着 u11-l1（GGUF 生成与量化工具）之后的工程问题：

1. **「我这个 GGUF 到底准不准？」** —— 量化把 fp16 权重压成 2bit/4bit 必然引入误差，但误差有多大、有没有让模型变笨？我们需要一个**不依赖单次采样回答**的客观打分方法。
2. **「2bit 量化（IQ2_XXS）没有 imatrix 就不能用，那 imatrix 从哪来？」** —— u3-l4 已经讲过 IQ2_XXS 每值仅约 2bit、靠码本查表，离不开 imatrix（列重要性）指导码本搜索。本讲讲清这条 imatrix 是怎么**用 ds4 运行时自己跑出来的**，以及没有它时的合成兜底公式。

学完后你应当能够：

- 说清 ds4 用 `--imatrix-out` 收集 routed-MoE imatrix 的完整流程，以及它产出的 llama.cpp 兼容 `.dat` 文件长什么样。
- 说清 imatrix 缺失时量化器 `deepseek4-quantize` 的**合成兜底公式**是什么、为什么它只是权宜之计。
- 说清 `gguf-tools/quality-testing/` 三件套（`collect_official.py` / `score_official` / `compare_scores.py`）如何用「目标 token 负对数似然（target-token NLL）」把任意本地 GGUF 对照官方 DeepSeek V4 续写打分并对比。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：imatrix 是「列重要性」，不是权重本身。**
量化一个矩阵 \(W\) 时，不同输入列被「用到」的程度不同：某些列的激活值长期很大，量化它们出错代价高；某些列几乎不被激活，量化糙一点无所谓。imatrix 就是把这种「列被用到的程度」量化成一个浮点向量，告诉量化器在选 scale 和码本时**把误差更多地摊到不重要的列上**。所以 imatrix 来自**激活统计**，而不是权重本身。

**直觉二：评判量化质量不能用「问模型一个问题看答得对不对」。**
单次采样回答受随机性、提示措辞影响极大，今天答对明天答错，无法回归。正确做法是**固定一条权威答案（官方续写），让本地模型逐 token 给这条答案打分**——模型给官方答案分配的概率越高（负对数似然越低），说明这个量化版本越接近官方模型。

**直觉三：teacher forcing（教师强制）。**
打分时我们**不让本地模型自己往下生成**，而是把官方答案的每个 token 强行喂进去（`eval(target_token)`），只读取它对这个 token 的概率估计。这样无论本地模型采样会不会跑偏，我们都只在「官方答案」这一条确定路径上比较概率，结果可复现。

> 名词速查：**NLL** = negative log likelihood = \(-\log P\)，概率越高 NLL 越低，越低越好。**LCP** = longest common prefix，本地贪心解码与官方续写的最长公共前缀长度。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 | 归属 |
|------|------|------|
| [ds4.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c) | 引擎核心；内含 imatrix 收集器 `ds4_imatrix_collector` 与驱动 `ds4_engine_collect_imatrix` | 收集端 |
| [ds4_cli.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c) | CLI 前端；解析 `--imatrix-*` 参数并派发到收集器 | 收集端 |
| [ds4_help.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_help.c) | 帮助文本；登记四个 imatrix 选项 | 收集端 |
| [gguf-tools/imatrix/README.md](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/imatrix/README.md) | imatrix 流程说明 + 数据集描述 | 收集端 |
| [gguf-tools/deepseek4-quantize.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c) | 量化器；含 `.dat` 读取 `imatrix_load`、查找 `imatrix_find`、**合成兜底** | 消费端 |
| [gguf-tools/quants.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/quants.c) | 量化内核；含 `ds4q_type_traits` 表与 `ds4q_requires_imatrix` | 消费端 |
| [gguf-tools/quality-testing/collect_official.py](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/quality-testing/collect_official.py) | 抓取官方 DeepSeek V4 续写，生成 `manifest.tsv` | 打分端 |
| [gguf-tools/quality-testing/score_official.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/quality-testing/score_official.c) | 链接 ds4 运行时，对本地 GGUF 逐 case 打分 | 打分端 |
| [gguf-tools/quality-testing/compare_scores.py](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/quality-testing/compare_scores.py) | 对比两份打分 TSV，输出 NLL/胜场/LCP | 打分端 |
| [gguf-tools/Makefile](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/Makefile) | `quality-score` 目标，把 `score_official.c` 链到 ds4 运行时 | 打分端 |

一句话定位：**收集端**（ds4 运行时）产出 imatrix `.dat`；**消费端**（量化器）读它来指导量化，没有时用合成兜底；**打分端**（quality-testing 三件套）独立于 imatrix，用来客观衡量任意两个 GGUF 谁更接近官方模型。

## 4. 核心概念与源码讲解

### 4.1 imatrix 收集与 .dat 格式

#### 4.1.1 概念说明

u3-l4 已经确立：ds4 的 2bit 量化（IQ2_XXS）每值仅约 2bit、靠码本查表，**必须有 imatrix 指导码本搜索**。那么这个 imatrix 从哪来？

ds4 的做法很巧妙：**不另写一套前向推理代码，而是复用 Metal prefill 主路径，把已经算出来的中间激活「偷看」一眼累加起来。** 具体来说，DeepSeek V4 的 routed MoE 专家有三类权重张量（见 [imatrix 收集器的文档注释 ds4.c:20065-20076](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L20065-L20076)）：

- `blk.N.ffn_gate_exps.weight`（gate 专家）
- `blk.N.ffn_up_exps.weight`（up 专家）
- `blk.N.ffn_down_exps.weight`（down 专家）

这三类矩阵在做矩阵乘时的**输入**分别是：

- gate/up 的输入 = FFN 归一化后的激活行（`batch_ffn_norm`）；
- down 的输入 = 路由加权后的 routed SwiGLU 行（`batch_routed_mid`）。

而这两个张量在 Metal prefill 图里**已经物化好了**（u5-l2、u6-l1）。收集器只需在每层 prefill 之后把它们从 GPU 读回宿主，对每个被路由命中的专家累加「输入的平方和」，就得到了真实的激活重要性——**完全不改推理数学**。

为什么只收集 routed MoE、不收集 shared/attention 投影？因为 u1-l2/u3-l4 已确立的「非对称量化」策略：只有 routed 专家被压到低比特，所以只有它们需要 imatrix；其余保持高精度（Q8），不需要。

#### 4.1.2 核心流程

收集流程的伪代码：

```
读入校准数据集（已渲染的聊天 prompt，用 DS4_IMATRIX_PROMPT 分隔）
分配 Metal prefill 图（与正式推理同一张 layer-major 图）
为每一层、每个专家开两个累加器：gate_up_sum2[il][expert][col]、down_sum2[il][expert][col]
for 每个 prompt:
    重置图的 prefill 状态（清 KV）
    把 prompt 走一遍 prefill（可能分块）
    每算完一层 il:
        读回 batch_ffn_norm、batch_routed_mid、batch_router_selected
        for 每个 token t:
            sq[col] = ffn_norm[t][col]^2
            for 每个被选中的专家 expert:
                gate_up_sum2[il][expert][col] += sq[col]      # gate/up 共用同一输入
                down_sum2[il][expert][k]   += routed_mid[t][expert][k]^2
                down_count[il][expert]++
    （可选）达到 max_prompts / max_tokens 提前停
把累加器归一化（除以命中次数）后写成 llama.cpp 兼容 .dat
```

关键数学：对每个专家 \(e\)、每个输入列 \(c\)，imatrix 记录的是**均方激活**：

\[
\text{imatrix}[e,c] \;=\; \frac{1}{N_e}\sum_{t\in\text{路由到 }e} x_t[c]^2
\]

其中 \(x_t\) 是该 token 在该层的 FFN 归一化输入（gate/up）或路由加权 SwiGLU 行（down），\(N_e\) 是专家 \(e\) 被命中的次数。命中次数为 0 的专家（从未被路由到）写全 1，表示「无信息，等同对待」。

#### 4.1.3 源码精读

**收集器结构体** —— 两个大数组 `gate_up_sum2` 与 `down_sum2` 是核心累加器，外加每专家命中计数（[ds4.c:20077-20092](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L20077-L20092)）：

```c
typedef struct {
    float *gate_up_sum2;   /* [active layer][active expert][hidden] */
    float *down_sum2;      /* [active layer][active expert][expert FFN] */
    uint32_t gate_up_count[DS4_MAX_LAYER][DS4_MAX_EXPERT];
    uint32_t down_count[DS4_MAX_LAYER][DS4_MAX_EXPERT];
    ...
    uint64_t observed_tokens;
    uint64_t observed_routes;   /* 累计命中次数，写入日志 */
    ...
} ds4_imatrix_collector;
```

注释里的 `[active layer][active expert][hidden]` 说明这是**按专家打包**的三维布局——这一点决定了 `.dat` 的打包方式（见下文）。

**核心累加函数** `imatrix_collect_layer_batch`（[ds4.c:20131-20184](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L20131-L20184)）——它先从 GPU 读回三个已物化的张量，然后逐 token、逐选中专家累加平方和。关键片段：

```c
/* 读回已物化的 MoE 输入（不改推理数学） */
ds4_gpu_tensor_read(g->batch_ffn_norm, 0, c->ffn_norm_buf, norm_bytes);
ds4_gpu_tensor_read(g->batch_routed_mid, 0, mid_dst, mid_bytes);
ds4_gpu_tensor_read(g->batch_router_selected, 0, c->selected_buf, sel_bytes);

for (uint32_t t = 0; t < n_tokens; t++) {
    const float *x = c->ffn_norm_buf + (size_t)t * DS4_N_EMBD;
    for (uint32_t i = 0; i < DS4_N_EMBD; i++) c->sq_tmp[i] = x[i] * x[i];   /* gate/up 输入平方 */
    for (uint32_t slot = 0; slot < DS4_N_EXPERT_USED; slot++) {
        const int expert = c->selected_buf[...];                              /* 哪个专家被选中 */
        float *gate_up = imatrix_gate_up_ptr(c, il, (uint32_t)expert);
        for (uint32_t i = 0; i < DS4_N_EMBD; i++) gate_up[i] += c->sq_tmp[i]; /* 累加 */
        ...
        float *down = imatrix_down_ptr(c, il, (uint32_t)expert);
        for (uint32_t i = 0; i < DS4_N_FF_EXP; i++) down[i] += mid[i]*mid[i]; /* down 输入平方 */
        c->down_count[il][expert]++;
    }
}
```

注意 gate 和 up 专家**共用同一个 `gate_up_sum2` 累加器**——因为它们的 matmul 输入都是同一个 FFN 归一化激活行。down 专家单独累加，因为它的输入是路由加权后的 SwiGLU 行。

**`.dat` 写出格式** —— `imatrix_write_entry`（[ds4.c:20190-20218](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L20190-L20218)）逐专家把命中次数归一化后写出。命中为 0 的专家写全 1：

```c
if (count == 0) {
    for (uint32_t i = 0; i < n_col; i++) tmp[i] = 1.0f;        /* 无信息 → 等同对待 */
} else {
    const float inv = 1.0f / (float)count;
    for (uint32_t i = 0; i < n_col; i++) tmp[i] = src[i] * inv; /* 均方激活 */
}
```

整个 `.dat` 文件由 `imatrix_collector_save`（[ds4.c:20220-20268](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L20220-L20268)）按以下字节布局写出（全部小端 i32 + float）：

```
n_entries (= 层数 × 3)
for 每个张量条目:
    name_len (i32) | name 字节 | ncall=1 (i32) | nval (i32) | nval 个 float
chunks (i32)                       ← 处理了多少个 prefill 块
dataset_len (i32) | dataset 字节   ← 校准数据集路径，便于溯源
```

其中**每个条目的 `nval = n_expert × n_columns`**：所有专家的向量被打包进同一条目（[imatrix README: imatrix/README.md#L96-L102](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/imatrix/README.md#L96-L102)）。这是 ds4 的约定：Flash 每条 routed 张量条目含 256 个专家向量，PRO 含 384 个。量化器在读侧按 `expert_id × n_columns` 切片取出对应专家。

**驱动函数** `ds4_engine_collect_imatrix`（[ds4.c:24991-25140](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24991-L25140)）——它有一个硬约束：**仅 Metal 后端可用**，因为收集器 hook 的是 layer-major Metal prefill 图：

```c
if (e->backend != DS4_BACKEND_METAL || !e->metal_ready) {
    fprintf(stderr, "ds4: imatrix collection currently requires --metal\n");
    return 1;
}
```

而在 CPU 构建（`-DDS4_NO_GPU`）下整个函数被编译期切除，直接报错（[ds4.c:24997-25006](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24997-L25006)）。它还用 `metal_graph_reset_prefill_state` 在每个 prompt 之前清掉 KV（[ds4.c:25077](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L25077)），保证每个 prompt 独立统计、不串味。

**CLI 入口** —— 四个选项（[ds4_help.c:253-256](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_help.c#L253-L256)）：

```
--imatrix-dataset FILE     渲染好的聊天 prompt 数据集
--imatrix-out FILE         输出 llama 兼容的 routed-MoE imatrix .dat
--imatrix-max-prompts N    收集 N 条 prompt 后停
--imatrix-max-tokens N     收集到 N 个 prompt token 后停
```

`main` 在 `--imatrix-out` 非空时**短路**到收集器，跳过正常推理（[ds4_cli.c:1689-1695](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1689-L1695)）。`--imatrix-out` 与 `--imatrix-dataset` 必须成对出现（[ds4_cli.c:1616-1621](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1616-L1621)）。

#### 4.1.4 代码实践（源码阅读型）

> 本实践需要 Metal Mac + 真实 GGUF 才能真正运行收集，故设计为源码阅读 + 可选实跑。

**实践目标**：搞清一条 imatrix 从「激活」到 `.dat` 字节的完整路径，并验证「不改推理数学」这一断言。

**操作步骤**：

1. 打开 [ds4.c:20131-20184](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L20131-L20184)，确认 `imatrix_collect_layer_batch` 只调 `ds4_gpu_tensor_read` 读回 `batch_ffn_norm` / `batch_routed_mid` / `batch_router_selected`，没有任何 `ds4_gpu_tensor_write` 回写设备——这证明收集是**只读旁路**，不碰推理数据流。
2. 打开 [ds4.c:20220-20268](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L20220-L20268)，数一遍 `.dat` 写出的字段顺序，对照 4.1.3 的字节布局自己画一张图。
3.（可选实跑）在有 Metal 的 Mac 上跑 README 的冒烟测试（[imatrix/README.md:81-87](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/imatrix/README.md#L81-L87)）：
   ```sh
   ./ds4 -m MODEL.gguf \
     --imatrix-dataset gguf-tools/imatrix/dataset/rendered_prompts.txt \
     --imatrix-out /tmp/ds4-test.imatrix.dat \
     --imatrix-max-prompts 1 --imatrix-max-tokens 4096
   ```

**需要观察的现象**（若实跑）：stderr 会打印 `collecting routed-MoE imatrix ... (layers=.., experts=..)`、每 10 条 prompt 刷一次 `prompts=.. tokens=.. routes=..`，最后 `wrote imatrix ... from N prompts, M tokens, K routed expert observations`。

**预期结果**：`/tmp/ds4-test.imatrix.dat` 生成，大小约为 `(层数×3 个条目) × (每条目 n_expert×n_col×4 字节) + 头部`。无法本地验证时记为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 gate 和 up 专家共用同一个累加器 `gate_up_sum2`，而 down 专家要单独一个？
**答案**：gate 与 up 矩阵在做 matmul 时**输入是同一个** FFN 归一化激活行（`batch_ffn_norm`），所以它们的「列重要性」完全相同，共用一个累加器即可；down 矩阵的输入是路由加权后的 SwiGLU 行（`batch_routed_mid`），是不同的张量，必须单独统计。

**练习 2**：一个从未被任何 prompt 路由命中的「冷专家」，它的 imatrix 向量会是什么值？为什么？
**答案**：全 1。因为命中计数为 0，`imatrix_write_entry` 走 `count == 0` 分支写 `1.0f`（[ds4.c:20209-20210](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L20209-L20210)）。全 1 表示「没有激活信息，所有列等同对待」，避免除零，也让量化器对冷专家退化为不偏倚的均匀量化。

---

### 4.2 imatrix 缺失时的合成兜底（官方续写抓取的前置）

> 说明：本小节对应规格里的「官方续写抓取」之前的必备知识——合成兜底公式。它解释了「没有 imatrix 时量化器怎么办」，是理解「为什么要辛苦收集 imatrix」的反面动机。下一小节（4.3）再讲 quality-testing 的官方续写抓取。

#### 4.2.1 概念说明

收集 imatrix 要跑几百万 token 的 Metal prefill，成本不低。如果用户手头没有 `.dat`，但又非要量化一个 `requires_imatrix=true` 的类型（也就是 IQ2_XXS），怎么办？

`deepseek4-quantize` 的做法是：**用权重矩阵自身的列能量当 imatrix 的合成替代品**。注意这里有一个关键区分：

- **真 imatrix**（4.1 收集的）= **激活**的均方（输入端统计）；
- **合成兜底** = **权重**的列平方和（参数端统计）。

激活统计告诉你「这列被用得多猛」，权重统计只告诉你「这列的权重数值有多大」——两者相关但不一样。激活统计才是量化误差该参考的真正目标，所以合成兜底只是**权宜之计**，u3-l4 已点明「IQ2_XXS 离不开 imatrix」正是此意。

#### 4.2.2 核心流程

谁需要 imatrix？由量化类型表 `ds4q_type_traits` 的 `requires_imatrix` 字段决定（[quants.c:39-61](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/quants.c#L39-L61)）。其中第 4 个布尔是 `can_quantize`（u11-l1 讲过，仅 `q8_0/q2_K/q4_K/iq2_xxs` 为真），第 5 个布尔就是 `requires_imatrix`：

| 类型 | can_quantize | requires_imatrix |
|------|:---:|:---:|
| q8_0 | ✓ | ✗ |
| q2_K | ✓ | ✗ |
| q4_K | ✓ | ✗ |
| **iq2_xxs** | ✓ | **✓** |

所以在**实际可写出的四种类型里，只有 IQ2_XXS 强制要求 imatrix**（[quants.c:1039-1042](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/quants.c#L1039-L1042) 的 `ds4q_requires_imatrix` 就是查这张表）。

合成兜底公式：对一个 \(R \times C\) 的权重矩阵 \(W\)，按列累加平方：

\[
\text{synthetic}[c] \;=\; \sum_{r=0}^{R-1} W[r,c]^2
\]

即「每列的权重能量」。它只在该类型 `requires_imatrix` 且外部没提供 imatrix 时才计算。

#### 4.2.3 源码精读

合成兜底就藏在量化一行权重的核心函数里（[deepseek4-quantize.c:1117-1128](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L1117-L1128)）：

```c
float *synthetic = NULL;
const float *im_ptr = imat;                          /* 外部提供的真 imatrix（可能为 NULL） */
if (!im_ptr && ds4q_requires_imatrix(type)) {        /* 没给 + 该类型必须要 */
    synthetic = xcalloc((size_t)ncols, sizeof(float));
    for (int64_t r = 0; r < nrows; r++) {
        const float *row = src + (size_t)r * (size_t)ncols;
        for (int64_t c = 0; c < ncols; c++) synthetic[c] += row[c] * row[c];   /* 列平方和 */
    }
    im_ptr = synthetic;                              /* 用合成值顶上 */
}
size_t written = ds4q_quantize_chunk(type, src, out.data, 0, nrows, ncols, im_ptr);
```

注意三个细节：

1. `src` 在这里是**反量化回的 float32 权重**（u11-l1 已讲过 HF safetensors 字节先反量化回 float32 再重新量化），所以 `row[c]` 是权重值，`synthetic[c]` 确实是权重的列能量。
2. 合成兜底**只对 `requires_imatrix` 的类型触发**；对 q4_K / q2_K，没给 imatrix 时 `im_ptr` 就是 NULL，直接传 NULL 给 `ds4q_quantize_chunk`（这些类型不靠 imatrix 也能量化，imatrix 对它们只是「误差加权增益」）。
3. 用 `--imatrix-strict` 时，`imatrix_find` 找不到条目会直接 `exit(1)`（[deepseek4-quantize.c:850-853](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L850-L853)），**禁止合成兜底**——这正是 u11-l1 提到的严格校验闸门，用于复现官方向量时不许偷懒。

**`.dat` 读侧如何把打包条目切回单专家** —— `imatrix_find`（[deepseek4-quantize.c:819-855](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L819-L855)）按张量名查到条目后，若发现 `n_values == ncols × n_experts`（即 ds4 的打包格式），就按 `expert_id × ncols` 偏移切出该专家的向量：

```c
if (expert_id >= 0 && n_experts > 0 && (int64_t)e->n_values == ncols * (int64_t)n_experts) {
    return e->values + (size_t)expert_id * (size_t)ncols;   /* 切第 expert_id 个专家 */
}
```

这正好对应 4.1.3 里收集端「一个条目打包 n_expert 个专家」的写法——读写两侧的打包约定必须一致，否则切错专家。

**`.dat` 加载** —— `imatrix_load`（[deepseek4-quantize.c:761-814](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L761-L814)）顺序读 `n_entries`，每条读 `name_len/name/ncall/nval/values`，再做有限性检查（`isfinite`），尾部可选地读 `chunks/dataset` 溯源段。它把条目装进哈希表 `im->map`，供 `imatrix_find` 按名查。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：验证「合成兜底是权重能量、真 imatrix 是激活能量」这一区分，并搞清 strict 模式如何禁止兜底。

**操作步骤**：

1. 打开 [deepseek4-quantize.c:1117-1128](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L1117-L1128)，确认 `synthetic[c] += row[c]*row[c]` 累加的是 `src`（权重），不是激活。
2. 对照 [quants.c:39-61](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/quants.c#L39-L61)，确认只有 `iq2_xxs` 同时满足 `can_quantize=true` 且 `requires_imatrix=true`。
3. 打开 [deepseek4-quantize.c:850-853](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L850-L853)，确认 `im->strict` 为真且找不到条目时直接 `exit(1)`——也就是说 `--imatrix-strict` 下根本不会走到 4.2.3 的合成兜底分支。

**需要观察的现象**：合成兜底分支的进入条件是 `!im_ptr && ds4q_requires_imatrix(type)`，二者缺一不可。

**预期结果**：能口头复述「真 imatrix = 激活均方（4.1 收集），合成兜底 = 权重列平方和（本节），strict 模式禁用兜底」。

#### 4.2.5 小练习与答案

**练习 1**：如果用户量化 q4_K 时既没给 imatrix 也没开 strict，会发生什么？
**答案**：q4_K 的 `requires_imatrix=false`，所以合成兜底分支不触发，`im_ptr=NULL` 直接传给 `ds4q_quantize_chunk`。q4_K 不靠 imatrix 也能完成量化（它有 4bit 加分组自适应仿射，u3-l4），imatrix 对它只是「误差加权增益」而非必需。

**练习 2**：为什么合成兜底用「权重的列平方和」而不是「权重的列绝对值之和」或「列均值」？
**答案**：因为 imatrix 的消费端（`ds4q_quantize_chunk` 选 scale/码本时）期望的是一个与「能量/方差」成比例的重要性度量，平方和与方差同量纲（能量∝方差×样本数），能正确反映该列出错对整体 MSE 的贡献；绝对值之和或均值不能正确加权高频大幅列。

---

### 4.3 官方续写抓取、打分与对比

#### 4.3.1 概念说明

imatrix 解决「量化时该偏袒哪些列」，但它不能直接告诉你「量化后模型变笨了多少」。衡量后者用 `gguf-tools/quality-testing/` 三件套，核心指标是**目标 token 负对数似然（target-token NLL）**：

1. 先用官方 DeepSeek V4 API（`temperature=0`，确定性）给 100 个固定 prompt 各生成一段**官方续写**。
2. 对每个本地 GGUF，让它 prefill 同一个 prompt，然后**逐 token 读出它对官方续写每个 token 的概率**，求 \(-\log P\) 之和。
3. 两个 GGUF 谁的总 NLL 低，谁就「更像官方模型」——这是一个**逐 token、可复现、不依赖单次采样**的客观比较。

为什么不用困惑度（perplexity）跑一大堆通用语料？因为 ds4 关心的是「在 DeepSeek V4 自己的输出分布上」的一致性，而不是通用文本上的语言建模能力。用官方续写当锚点，测的就是「量化有没有让模型偏离它本来的输出风格」。

#### 4.3.2 核心流程

三步流水线（对应 [quality-testing/README.md](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/quality-testing/README.md) 的四节）：

```
步骤 1  collect_official.py
   读 prompts.jsonl（100 条）→ 调官方 API（temperature=0, max_tokens=24）→
   写 prompts/case_*.txt、continuations/case_*.txt、responses/case_*.json、manifest.tsv

步骤 2  score_official（C，链接 ds4 运行时）
   对每个本地 GGUF，遍历 manifest.tsv 每个 case：
     prefill prompt → 对官方续写逐 token：argmax(贪心首 token/LCP) + token_logprob(NLL) + eval(教师强制)

步骤 3  compare_scores.py OLD.tsv NEW.tsv
   按 case_id 对齐两份 TSV → token 加权 avg_nll、胜场、首 token 命中、平均 LCP
```

NLL 的数学定义（\(T\) 为续写长度）：

\[
\text{NLL} = \sum_{i=0}^{T-1} -\log P(t_i \mid \text{prompt}, t_{<i}), \qquad
\text{avg\_nll} = \frac{\text{NLL}}{T}
\]

对比时的相对变化（`compare_scores.py` 第 72 行）：

\[
\text{relative\_change} = \left(\frac{\text{avg\_nll}_{\text{new}}}{\text{avg\_nll}_{\text{old}}} - 1\right)\times 100\%
\]

负值表示新 GGUF 更接近官方。README 给出的实测（[imatrix/README.md:169-176](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/imatrix/README.md#L169-L176)）：用 imatrix 的 Q4 相对旧 Q4，avg NLL 从 0.1774 降到 0.1739，相对变化 \(-1.95\%\)，100 个 case 里 54 胜 46 负——imatrix 确实让本地模型更贴近官方。

#### 4.3.3 源码精读

**抓取官方续写** `collect_official.py` —— 它构造请求时强制确定性（[collect_official.py:124-154](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/quality-testing/collect_official.py#L124-L154)）：

```python
payload = {
    "model": model,
    "messages": [{"role": "user", "content": prompt}],
    "temperature": 0,          # 确定性，保证官方续写可复现
    "max_tokens": max_tokens,
    "logprobs": True,
    "top_logprobs": top_logprobs,
    "stream": False,
}
```

带指数退避重试（[collect_official.py:157-181](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/quality-testing/collect_official.py#L157-L181)）：对 429/5xx 重试、`delay *= 1.7`，最多 6 次；4xx（非 429）直接抛错。`main` 把每条结果落盘并写 `manifest.tsv`（[collect_official.py:184-243](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/quality-testing/collect_official.py#L184-L243)）。需要 `DEEPSEEK_API_KEY`，且 **prompt 列表 tracked（`prompts.jsonl` 100 条），但官方响应不 tracked**（因为是外部 API 派生物）。

**本地打分** `score_official.c` —— 它链接完整 ds4 运行时，对每个 case 做 prefill + 教师强制逐 token 评分。核心循环（[score_official.c:114-135](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/quality-testing/score_official.c#L114-L135)）：

```c
for (int i = 0; i < target.len; i++) {
    const int greedy = ds4_session_argmax(session);                 /* 本地贪心首 token */
    if (i == 0) first_match = (greedy == target.v[i]);
    if (still_matching && greedy == target.v[i]) lcp++;             /* 贪心 LCP */
    else still_matching = false;

    ds4_token_score score;
    ds4_session_token_logprob(session, target.v[i], &score);        /* 官方 token 的 logprob（只读旁路） */
    nll += -(double)score.logprob;                                  /* 累加 NLL */

    ds4_session_eval(session, target.v[i], err, sizeof(err));       /* 教师强制：喂官方 token 前进 */
}
```

三个关键点：

1. `ds4_session_argmax` / `ds4_session_token_logprob` 都是 u4-l3 讲过的**只读旁路**——不推进 checkpoint、不动 KV，只读当前 logits。
2. NLL 用的是 `ds4_session_token_logprob` 返回的归一化 logprob（log-sum-exp 归一，u4-l3），所以是真实概率的负对数，不是裸 logit。
3. `ds4_session_eval(session, target.v[i])` 是**教师强制**：无论本地模型想采样什么，都强行把官方 token 喂进 KV 前进一格，保证后续 token 的概率都是在「官方续写这条确定路径」上算的。

每个 case 的 prompt 用 `ds4_encode_chat_prompt`（带聊天模板）编码，续写用 `ds4_tokenize_text`（纯文本，不带模板）编码（[score_official.c:100-103](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/quality-testing/score_official.c#L100-L103)）。同一 session 在 case 之间复用，靠 `ds4_session_sync` 切换 prompt 前缀（[score_official.c:109-112](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/quality-testing/score_official.c#L109-L112)），享受 u2-l3 的前缀复用。

输出 TSV 七列（[score_official.c:78](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/quality-testing/score_official.c#L78)）：`id, prompt_tokens, target_tokens, nll, avg_nll, first_match, greedy_lcp`。

**对比** `compare_scores.py` —— 按 `case_id` 取两份 TSV 的交集，做 **token 加权**平均（不是 case 等权，[compare_scores.py:43-75](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/quality-testing/compare_scores.py#L43-L75)）：

```python
old_nll += o["nll"]; new_nll += n["nll"]      # 先按 case 累加总 NLL
tokens  += t                                   # 累加 token 数
...
avg_old = old_nll / tokens                      # token 加权平均
avg_new = new_nll / tokens
print(f"relative_nll_change\t{(avg_new/avg_old - 1.0)*100.0:.3f}%")
```

用 token 加权而非 case 等权，是因为不同 case 续写长度不同（虽 `max_tokens=24` 但有的被截断），token 加权更公平。它还分别统计**单 case 胜场**（`delta<0` 算新胜）、**首 token 命中数**、**平均贪心 LCP**，并打印改进最多/退步最多的 8 个 case 供定位。

**构建** —— `make -C gguf-tools quality-score`（[gguf-tools/Makefile:35-37](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/Makefile#L35-L37)）会先到上级目录编 `ds4.o + 后端.o`，再把 `score_official.c` 链接成 `quality-testing/score_official`——注意它和收集器一样复用完整引擎，所以也按平台选 Metal（macOS）或 CUDA（Linux）后端（[gguf-tools/Makefile:4-23](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/Makefile#L4-L23)）。

#### 4.3.4 代码实践（源码阅读 + 可选实跑）

**实践目标**：把「官方续写 → 本地 NLL → 新旧对比」整条链走通，理解为什么 NLL 能客观衡量量化质量。

**操作步骤**：

1. **阅读追踪**：从 [score_official.c:114-135](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/quality-testing/score_official.c#L114-L135) 出发，画出一次 case 的 token 流：`argmax(只读) → token_logprob(只读,算 NLL) → eval(教师强制,前进 KV)`，确认前两个不推进 checkpoint。
2. **阅读对比**：打开 [compare_scores.py:65-75](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/quality-testing/compare_scores.py#L65-L75)，确认 avg_nll 是 token 加权（除以 `tokens` 而非 case 数）。
3.（可选实跑，需 DeepSeek API key + 本地 GGUF + Metal/CUDA）：
   ```sh
   export DEEPSEEK_API_KEY=...
   python3 gguf-tools/quality-testing/collect_official.py \
     --prompts gguf-tools/quality-testing/prompts.jsonl \
     --out gguf-tools/quality-testing/data/flash --count 100 --max-tokens 24
   make -C gguf-tools quality-score
   gguf-tools/quality-testing/score_official OLD.gguf gguf-tools/quality-testing/data/flash/manifest.tsv /tmp/old.tsv 4096
   gguf-tools/quality-testing/score_official NEW.gguf gguf-tools/quality-testing/data/flash/manifest.tsv /tmp/new.tsv 4096
   python3 gguf-tools/quality-testing/compare_scores.py /tmp/old.tsv /tmp/new.tsv
   ```

**需要观察的现象**（若实跑）：`compare_scores.py` 输出 `old_avg_nll / new_avg_nll / delta_new_minus_old / relative_nll_change / case_wins_new_old_ties / first_token_matches_old_new / avg_greedy_lcp_old_new`，以及改进/退步最多的 8 个 case。

**预期结果**：`delta_new_minus_old` 为负、`relative_nll_change` 为负、`case_wins` 新 > 旧，说明新 GGUF（如带 imatrix 的版本）更接近官方。无法本地验证时记为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么打分循环里 `ds4_session_eval` 喂的是 `target.v[i]`（官方 token），而不是本地模型自己 argmax 出来的 token？
**答案**：这是教师强制。如果让本地模型自己生成，它会沿自己的分布跑偏，测的就不是「官方续写上的概率」而是「本地采样轨迹」，结果不可复现也无法对照官方。强行喂官方 token，保证所有本地 GGUF 都在同一条确定路径上被比较，差异只来自它们对官方 token 的概率估计。

**练习 2**：`compare_scores.py` 为什么用 token 加权平均而不是 case 等权平均？
**答案**：因为各 case 的续写长度可能不同（受 `max_tokens` 截断、EOS 提前结束影响）。case 等权会让短续写的 case 与长续写 case 同等权重，扭曲总体 NLL；token 加权让每个 token 贡献相等，更公平地反映「每个官方 token 上的平均负对数似然」。

**练习 3**：NLL 比较和 ds4-eval（u10-l4）的能力回归套件有什么分工？
**答案**：ds4-eval 测「模型能不能用同一条推理路径解硬题」（GPQA/AIME 等，关注通过率与轨迹稳定性）；quality-testing 测「量化版本相对官方模型的输出分布偏移」（NLL，关注概率一致性）。前者是能力下限，后者是保真度——一个量化版本可能 ds4-eval 分数没变但 NLL 变差，说明它在「看起来还行」的同时已经悄悄偏离了官方分布。

## 5. 综合实践

**任务**：为「用 imatrix 重新量化一个 Q2 GGUF」这件事，端到端解释三件事，并指出每一步对应的源码位置。

1. **imatrix 从哪来**：描述 `./ds4 --imatrix-out ... --imatrix-dataset ...` 跑起来后，`ds4_engine_collect_imatrix` 如何复用 Metal prefill 图、读回哪三个已物化张量、对 gate/up 与 down 分别累加什么、最终 `.dat` 一个条目打包多少个专家向量。引用 [ds4.c:20131-20184](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L20131-L20184) 与 [ds4.c:20220-20268](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L20220-L20268)。

2. **没有 imatrix 会怎样**：写出合成兜底公式 \(\text{synthetic}[c]=\sum_r W[r,c]^2\)，说明它统计的是**权重**而非**激活**，且只在 `requires_imatrix=true`（即 IQ2_XXS）时触发；并说明 `--imatrix-strict` 如何禁止兜底。引用 [deepseek4-quantize.c:1117-1128](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L1117-L1128) 与 [quants.c:39-61](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/quants.c#L39-L61)。

3. **怎么验证新 GGUF 更接近官方**：描述 `collect_official.py`（确定性抓官方续写）→ `score_official`（教师强制逐 token 算 NLL）→ `compare_scores.py`（token 加权对比）三步，并解释为什么 NLL 负向变化说明保真度提升。引用 [score_official.c:114-135](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/quality-testing/score_official.c#L114-L135) 与 [compare_scores.py:43-75](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/quality-testing/compare_scores.py#L43-L75)。

**交付**：一张包含三栏（步骤 / 关键源码位置 / 一句话原理）的表格，外加一段说明「为什么 imatrix 的真值（激活均方）优于合成兜底（权重能量）」。

## 6. 本讲小结

- **imatrix 收集是只读旁路**：`ds4_engine_collect_imatrix` 复用 Metal layer-major prefill 图，读回已物化的 `batch_ffn_norm`/`batch_routed_mid`/`batch_router_selected`，对每个路由命中的专家累加输入平方和——**完全不改推理数学**，且仅 Metal 后端可用。
- **`.dat` 按专家打包**：一个 routed 张量条目含 `n_expert × n_columns` 个 float（Flash 256、PRO 384），量化器读侧按 `expert_id × ncols` 切片；格式是 llama.cpp 兼容的 legacy 二进制，尾部带 `chunks` 与数据集路径溯源。
- **gate/up 共用累加器，down 独立**：因为前两者的 matmul 输入是同一个 FFN 归一化激活，down 的输入是路由加权后的 SwiGLU 行。
- **合成兜底是权重能量、非激活**：缺失 imatrix 且类型 `requires_imatrix`（仅 IQ2_XXS）时，量化器用 \(\sum_r W[r,c]^2\) 顶上，是权宜之计；`--imatrix-strict` 禁止兜底。
- **质量打分用目标 token NLL**：`collect_official.py` 确定性抓官方续写 → `score_official` 教师强制逐 token 读 logprob 算 NLL → `compare_scores.py` token 加权对比，负向变化说明更接近官方。
- **两种质量度量分工**：ds4-eval（u10-l4）测能力下限，quality-testing 测分布保真度，互补不替代。

## 7. 下一步学习建议

- **u11-l3（测试向量与回归）**：本讲的 NLL 是「相对官方」的统计指标，下一讲讲「绝对 logits」级别的回归——用 `tests/test-vectors` 的 golden 向量做逐 logit 严格比对，精度比 NLL 更高、更能发现后端漂移。
- **重读 u3-l4**：本讲的合成兜底公式与 `requires_imatrix` 都根植于 u3-l4 讲过的量化块结构与「为什么 IQ2_XXS 必须要 imatrix」，建议对照复习。
- **动手方向**：若有 Metal Mac，按 [imatrix/README.md](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/imatrix/README.md) 第 2、3 节真正收集一份小 imatrix、用 `deepseek4-quantize --imatrix` 重生成一个 GGUF，再用本讲的 quality-testing 流程对比有无 imatrix 的 NLL 差异，亲手复现 \(-1.95\%\) 这个结果。
