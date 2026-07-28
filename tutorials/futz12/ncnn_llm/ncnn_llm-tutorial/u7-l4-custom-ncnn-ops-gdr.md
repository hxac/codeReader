# 自定义 ncnn 算子 GDR / ShortConv

## 1. 本讲目标

本讲解决一个问题：**当模型用到了 ncnn 原生不支持的算子时，ncnn_llm 是如何把它「插」进推理流水线的？**

学完本讲，读者应该能够：

- 说出继承 `ncnn::Layer` 实现自定义算子的基本套路，并解释 `one_blob_only` / `support_inplace` 等标志位的含义。
- 读懂 `ShortConv`（带滑动状态的一维短卷积）与 `GatedDeltaRule`（门控 DeltaRule 线性注意力）两个自定义算子的 `forward` 实现。
- 解释 `register_gdr_layers` 为什么必须在 `load_param` / `load_model` **之前**调用。
- 说出 `sconv_cnt` / `gdr_cnt` 两个配置字段如何决定 `create_ctx` 选用 `qwen3_5_ctx`，以及在 prefill / generate 中这两类 cache 是如何随上下文传递的。

## 2. 前置知识

本讲建立在已经学过的几讲之上，这里只做最小回顾：

- **u2-l1 基类 ncnn_llm_base**：所有模态运行时的公共底座，`KVCache` 是 `vector<pair<ncnn::Mat, ncnn::Mat>>`，按 Transformer 层数组织。本讲要讲的 `sconv_cache` / `gdr_cache` 是 **KV cache 之外、额外的两类状态**，只有混合架构才需要。
- **u2-l2 共享文本运行时**：讲过「跨模型族共享 decoder + KV cache 运行时」是本项目的设计主线，并留下一个伏笔——「唯一例外是带 `sconv_cnt` / `gdr_cnt` 的 Qwen3.5 混合架构在 generate 里**内联展开** decoder」。本讲就是要兑现这句话，讲清楚为什么要内联、内联了什么。
- **ncnn 的基本调用三步**：`create_extractor()` → `input("inN", …)` → `extract("outN", …)`，输入输出靠字符串插槽名绑定。本讲的 cache 传递就是靠这套插槽约定完成的。
- **混合架构（hybrid model）的直觉**：传统 Transformer 用自注意力（attention）建模长程依赖，复杂度随序列长度平方增长；现代边缘端模型（如 Qwen3.5 系列）常把一部分层换成**线性注意力 / 线性 RNN**（如 DeltaNet、GatedDeltaRule），用一个固定大小的「状态矩阵」替代不断增长的 KV cache，从而在长序列下更省内存、更快。本讲的两个算子正是这种混合架构在 ncnn_llm 里的落地产物。

> 一个关键区分：本讲讨论的 `GatedDeltaRule` / `ShortConv` 是 **decoder 子网内部的 ncnn 算子**，而 u4 讲的 RoPE 是 **decoder 外部、由 C++ 直接预算的 cos/sin cache**。二者不在同一层，不要混淆。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/utils/gdr.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/gdr.h) | 声明 `GatedDeltaRule`、`ShortConv` 两个自定义 ncnn Layer 类，以及注册函数 `register_gdr_layers`。 |
| [src/utils/gdr.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/gdr.cpp) | 两个算子的 `forward` 实现，以及 creator/destroyer 工厂与注册函数。 |
| [src/ncnn_llm_gpt.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h) | 定义上下文类层次 `ncnn_llm_gpt_ctx` → `qwen3_5_ctx`，后者多出 `sconv_cache` / `gdr_cache`；并声明成员 `sconv_cnt` / `gdr_cnt`。 |
| [src/ncnn_llm_gpt.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp) | 构造函数里读取配置并注册算子；prefill / generate 里传递与回收两类 cache。 |

阅读顺序建议：先看 `gdr.h`（看接口）→ 看 `gdr.cpp` 末尾的 `register_gdr_layers`（看怎么进 ncnn）→ 看 `ncnn_llm_gpt.cpp` 构造函数的注册时机 → 回到 `gdr.cpp` 读两个 `forward` → 最后看 ctx 与 cache 的传递。

---

## 4. 核心概念与源码讲解

### 4.1 ncnn 自定义算子机制与 register_gdr_layers

#### 4.1.1 概念说明

ncnn 的模型文件（`.param`）里，每一行算子都用一个**类型名字符串**标识（如 `Convolution`、`ReLU`）。ncnn 内置了一张「类型名 → 构造函数」的表。当遇到 ncnn 不认识的类型名时，就需要项目自己把「类型名 → 怎么 new 出这个 Layer」登记进去，这就是 `Net::register_custom_layer`。

实现一个自定义算子的套路是：

1. 继承 `ncnn::Layer`，重写 `forward`（多输入多输出版本，或单 blob 版本）。
2. 在构造函数里设置标志位（告诉 ncnn 这个算子的特性）。
3. 写一个返回 `ncnn::Layer*` 的 creator 函数和一个 delete 它的 destroyer 函数。
4. 调用 `register_custom_layer("类型名", creator, destroyer)` 登记。

`gdr.h` 里两个类正是这样声明的：

- [src/utils/gdr.h:L7-L17](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/gdr.h#L7-L17) — `GatedDeltaRule` 继承 `ncnn::Layer`，重写多 blob 版 `forward`，并声明了两个成员 `num_k_heads` / `num_v_heads`（默认 128）。
- [src/utils/gdr.h:L19-L25](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/gdr.h#L19-L25) — `ShortConv` 同样继承 `ncnn::Layer` 并重写 `forward`。

> 小提醒：`GatedDeltaRule::num_k_heads` / `num_v_heads` 这两个字段在 `forward` 里**实际上并未被使用**——`forward` 直接从输入 `ncnn::Mat` 的形状（`query.h`、`query.c`、`query.w`）推导头数与维度。因此这两个字段是「声明了但当前不生效」的配置位，读源码时不必纠结它们的赋值。

#### 4.1.2 核心流程

`register_gdr_layers` 把两个算子的类型名登记到某个 `ncnn::Net`：

```text
register_gdr_layers(net)
  ├─ net.register_custom_layer("GatedDeltaRule", GatedDeltaRule_creator, GatedDeltaRule_destroyer)
  └─ net.register_custom_layer("ShortConv",       ShortConv_creator,       ShortConv_destroyer)
```

creator / destroyer 是极简的工厂与析构函数。登记之后，ncnn 在解析 `.param` 文件遇到 `GatedDeltaRule` / `ShortConv` 类型名时，就知道该调哪个 creator 来 new 出对应的 Layer 对象。

#### 4.1.3 源码精读

注册函数本体只有两行：

[src/utils/gdr.cpp:L358-L362](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/gdr.cpp#L358-L362) — 把 `"GatedDeltaRule"` 与 `"ShortConv"` 两个类型名连同各自的 creator/destroyer 登记进传入的 `net`。

creator / destroyer 工厂成对出现，逻辑简单（new / delete）：

[src/utils/gdr.cpp:L338-L356](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/gdr.cpp#L338-L356) — 四个小函数分别负责构造与销毁两个 Layer 对象。

**注册时机至关重要**。在 `ncnn_llm_gpt` 构造函数里，`register_gdr_layers` 出现在 `load_param` / `load_model` **之前**：

[src/ncnn_llm_gpt.cpp:L73-L80](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L73-L80) — 先在第 73 行登记自定义算子，第 75–76 行才加载 decoder 的 `.param` / `.bin`。顺序不能反：ncnn 解析 `.param` 时需要已经知道每个类型名怎么构造，否则会因「未知算子」而加载失败。

注意：登记只在 `decoder_net` 上做。因为这两个算子只出现在 decoder 子网里（embed、proj_out 子网不需要）。

#### 4.1.4 代码实践

**实践目标**：确认注册时机与「未知算子」的失败行为。

**操作步骤**：

1. 打开 [src/ncnn_llm_gpt.cpp:L73](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L73)，确认 `register_gdr_layers(*decoder_net);` 在 `decoder_net->load_param(...)` / `load_model(...)` 之前。
2. 做一个思想实验（**不必真改源码**）：如果把第 73 行移到第 80 行（加载之后），用一个含 `GatedDeltaRule` 算子的 decoder `.param` 加载，会发生什么？
3. 想确认的话，可以在 `register_gdr_layers` 内临时加一行 `printf`（这是允许的源码阅读型改动，改完记得还原），分别打印「登记时」和「load_param 返回值」，观察顺序。

**需要观察的现象**：登记必须在解析 `.param` 之前完成。

**预期结果**：调换顺序后，`load_param` 会因找不到 `GatedDeltaRule` / `ShortConv` 的构造函数而失败，模型构造抛出异常被 catch 成「load model failed」。实际运行结果**待本地验证**（需要一份带 GDR/ShortConv 算子的模型权重）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `register_gdr_layers` 只作用在 `decoder_net`，而不在 `embed_net` / `proj_out_net` 上？

> **答**：这两个算子是 decoder 内部混合架构层的组成模块，embed（token 查表）与 proj_out（投影到 logits）子网里没有它们；对没有自定义算子的 Net 调用注册是无意义的。

**练习 2**：creator 函数返回的是 `ncnn::Layer*`，destroyer 负责释放。如果只登记了 creator、忘记登记 destroyer，会有什么后果？

> **答**：ncnn 内部仍会持有 creator 产生的裸指针，销毁时找不到对应的 destroyer 就无法正确释放，导致内存泄漏。所以 creator 与 destroyer 必须**成对**登记（参见源码 L360–L361 的写法）。

---

### 4.2 ShortConv：带滑动状态的一维短卷积

#### 4.2.1 概念说明

`ShortConv` 是混合架构里常见的「**短卷积前端**」：在每个 token 进入线性注意力之前，先用一个小核（kernel size 通常为 3 或 4）的、**按通道独立**（depthwise）的一维因果卷积做一次局部平滑，再用 SiLU 激活。它的作用是给线性注意力补充**局部、平移等变**的归纳偏置——这是纯线性注意力缺乏的。

它的难点不在于卷积本身，而在于**流式推理**：自回归解码时每次只来 1 个 token，但卷积需要看前 `kernel_size-1` 个 token。所以算子必须把「最近的若干输入」作为**状态**保存下来，下一轮拼到新输入前面继续卷。这正是 `conv_state` 这个输入输出 blob 的用途。

#### 4.2.2 核心流程

设核长为 \(K\)、分组数（通道数）为 \(G\)、本批序列长度为 \(L\)。算子的三个输入是：

| 输入 | 含义 |
| --- | --- |
| `weight_mat` (bottom[0]) | 每组 \(K\) 个卷积权重 |
| `mixed_qkv` (bottom[1]) | 本批输入，形状 \((G, L)\) |
| `conv_state` (bottom[2]) | 上一批留下的状态（首轮为空） |

对输出位置 \(i\)、分组 \(g\)，做因果卷积再加 SiLU：

\[
s_{i,g}=\sum_{k=0}^{K-1} x_{i-(K-1)+k}^{(g)}\cdot w_k^{(g)},\qquad
\mathrm{out}_{i,g}=\mathrm{SiLU}(s_{i,g})=s_{i,g}\cdot\sigma(s_{i,g})
\]

其中 \(x\) 是「状态拼接本批输入」后的完整序列，超出范围的位置按 0 填充（首轮）或由上一批状态提供（后续轮）。

执行步骤：

1. **拼缓冲**：首轮时前面补 \(K-1\) 个 0 再接本批输入；非首轮时把 `conv_state` 的行拼在本批输入前面，得到 `stated_mixed_qkv`。
2. **取新状态**：把 `stated_mixed_qkv` 的**最后 \(K\) 行**复制出来作为下一批的 `conv_state`。
3. **做卷积 + SiLU**：对每个输出位置、每组，按上式累加并激活。
4. 输出两个 blob：卷积结果（top[0]）与新状态（top[1]）。

#### 4.2.3 源码精读

构造函数设置两个关键标志位：

[src/utils/gdr.cpp:L272-L276](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/gdr.cpp#L272-L276) — `one_blob_only=false`（多输入 blob）、`support_inplace=false`（不支持原地）。这两个标志告诉 ncnn 该算子需要多个独立输入且会分配新输出。

拼缓冲的逻辑分首轮 / 非首轮：

[src/utils/gdr.cpp:L288-L300](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/gdr.cpp#L288-L300) — `conv_state` 为空时前面补零；非空时先拷状态行再接本批输入。

取出新状态（拖尾 \(K\) 行）：

[src/utils/gdr.cpp:L302-L306](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/gdr.cpp#L302-L306) — `last_conv_state` 取缓冲区最后 `kernel_size` 行，作为下一批的输入状态。

卷积主循环（用 OpenMP 按组并行）：

[src/utils/gdr.cpp:L311-L331](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/gdr.cpp#L311-L331) — 对每组 `g`、每个输出位置 `i`，从 `stated_mixed_qkv` 里取连续 \(K\) 行做加权求和，再乘 `sigmoid(sum)` 实现 SiLU。注意 `base = prefix_len + i`、`src_i = base-(K-1)+k`，这正是因果卷积（当前 token 与之前 \(K-1\) 个 token）的索引。

最后把两个结果写回：

[src/utils/gdr.cpp:L308-L309](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/gdr.cpp#L308-L309) 与 [src/utils/gdr.cpp:L333](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/gdr.cpp#L333) — `top_blobs[0]` 是卷积输出，`top_blobs[1]` 是新状态。

#### 4.2.4 代码实践

**实践目标**：在纸面上跑一次 `ShortConv::forward`，验证「状态在两批之间正确衔接」。

**操作步骤**：

1. 取 \(K=3\)、\(G=1\)，权重 \(w=[1,1,1]\)（即做 3 步滑动求和）。
2. 第一批输入 \(x=[a,b,c]\)（`conv_state` 为空）。
   - 拼缓冲：前面补 \(K-1=2\) 个 0，得 \([0,0,a,b,c]\)。
   - 输出 \(i=0,1,2\)：分别取 \([0,0,a]\to a\)、\([0,a,b]\to a+b\)、\([a,b,c]\to a+b+c\)。
   - 新状态 = 拖尾 3 行 = \([a,b,c]\)（即最后 3 个输入）。
3. 第二批输入 \(x=[d]\)，状态 = \([a,b,c]\)。
   - 拼缓冲：\([a,b,c,d]\)，`prefix_len = K = 3`。
   - 输出 \(i=0\)：`base = 3+0 = 3`，取 `src_i = 3-(3-1)+k = 1+k`，即行 \(1,2,3 = [b,c,d]\to b+c+d\)。
   - 新状态 = 拖尾 3 行 = \([b,c,d]\)。
4. 把上述结果与 [src/utils/gdr.cpp:L311-L331](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/gdr.cpp#L311-L331) 的索引公式逐项对齐。

**需要观察的现象**：第二批的输出 \(b+c+d\) 正好接续第一批最后一个输出 \(a+b+c\)——也就是说，状态让卷积「跨批」地保持了正确的因果上下文。

**预期结果**：手算值与公式一致。这是纯算术推导，无需运行；如要在机器上验证，需自备调用该算子的测试程序，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`ShortConv` 为什么用 `one_blob_only=false`？

> **答**：因为它需要 `weight_mat`、`mixed_qkv`、`conv_state` **三个独立输入** blob，而 `one_blob_only=true` 是 ncnn 里「只有一个输入一个输出」算子的优化标记，不适用于多输入算子。

**练习 2**：如果忽略 `conv_state`（每批都当首轮处理、状态不衔接），输出会有什么问题？

> **答**：跨批边界处会丢失前 \(K-1\) 个 token 的上下文，等于在每批开头都强行补零，导致边界 token 的卷积结果错误，自回归解码时每个 token 都会受影响。

---

### 4.3 GatedDeltaRule：门控 DeltaRule 线性注意力

#### 4.3.1 概念说明

`GatedDeltaRule` 是 DeltaNet 家族的**门控线性注意力**算子。传统自注意力用不断增长的 KV cache 存历史；线性注意力则用一个**固定大小的状态矩阵** \(S\)（每个头一个 \(d_k\times d_v\) 矩阵）作为「联想记忆」——存的是 key/value 的累积，查询时用 query 去读。这样推理时每步只需更新一个固定矩阵，内存不随序列增长。

「DeltaRule」的含义是它的更新规则：不是简单累加 \(k\otimes v\)，而是写入一个**修正量** \(\delta\)，让状态在被当前 key 查询时能正好返回当前 value。这比原始线性注意力更准确。「Gated」指每步有一个门控 \(e^{g_t}\in(0,1)\) 衰减旧记忆，让模型能「遗忘」。

#### 4.3.2 核心流程

算子有 **8 个输入** blob：

| 输入 | 含义 |
| --- | --- |
| `A_log` (bottom[0]) | 每头的对数衰减基 |
| `dt_bias` (bottom[1]) | 每头的时间偏置 |
| `b` (bottom[2]) | 生成更新强度 \(\beta=\sigma(b)\) |
| `a` (bottom[3]) | 生成门控 \(g=-e^{A}\cdot\mathrm{softplus}(a+d_t)\) |
| `query/key/value` (bottom[4-6]) | Q、K、V |
| `initial_state` (bottom[7]) | 上一批的状态矩阵（首轮为空） |

输出有 **2 个** blob：注意力输出（top[0]）与新状态矩阵（top[1]）。

设单头状态 \(S\) 为 \(d_k\times d_v\) 矩阵（代码中按 `state[dk*dv+dv]` 一维展开，行对应 key 维、列对应 value 维）。每个时间步 \(t\) 依次执行（先对 q、k 做 L2 归一化）：

\[
\tilde{S}_t = e^{g_t}\,S_{t-1} \qquad\text{(衰减)}
\]

\[
m_t = k_t^{\top}\tilde{S}_t \qquad\text{(用旧状态查询当前 key,得到预测值)}
\]

\[
\delta_t = \beta_t\,(v_t-m_t) \qquad\text{(修正量 = 真值 − 预测)}
\]

\[
S_t = \tilde{S}_t + k_t\,\delta_t^{\top} \qquad\text{(外积写回修正)}
\]

\[
o_t = \frac{1}{\sqrt{d_k}}\,S_t^{\top}q_t \qquad\text{(用 query 读状态)}
\]

其中标量缩放 \(\mathrm{scale}=1/\sqrt{d_k}\)。门控与更新强度由输入派生：

\[
\beta_t=\sigma(b_t),\qquad g_t=-e^{A_{\log}}\cdot\mathrm{softplus}(a_t+d_t^{\text{bias}})
\]

注意 \(e^{g_t}\in(0,1)\)（因为 \(g_t<0\)），所以它是一个合法的衰减系数；首轮没有 `initial_state` 时 \(S_0=0\)。

#### 4.3.3 源码精读

几个标量工具函数先备好：

[src/utils/gdr.cpp:L5-L38](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/gdr.cpp#L5-L38) — `l2norm`（按行 L2 归一化）、`sigmoidf`、`softplusf`（带 ±20 的数值截断避免溢出）。

核心递推在 `torch_recurrent_gated_delta_rule`，五步与上面的公式一一对应：

[src/utils/gdr.cpp:L90-L93](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/gdr.cpp#L90-L93) — 衰减：`state[i] *= g_t_exp`（\(e^{g_t}\)）。

[src/utils/gdr.cpp:L95-L102](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/gdr.cpp#L95-L102) — 预测值 \(m_t=k_t^\top\tilde{S}_t\)（代码里 `kv_mem`）。

[src/utils/gdr.cpp:L104-L108](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/gdr.cpp#L104-L108) — 修正量 \(\delta_t=\beta_t(v_t-m_t)\)。

[src/utils/gdr.cpp:L110-L116](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/gdr.cpp#L110-L116) — 外积写回 \(S_t=\tilde{S}_t+k_t\delta_t^\top\)。

[src/utils/gdr.cpp:L118-L126](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/gdr.cpp#L118-L126) — 读出 \(o_t=S_t^\top q_t\cdot\mathrm{scale}\)。

`forward` 的职责是「准备参数 + 调整内存布局 + 调递推 + 回写」。先从输入形状推出维度：

[src/utils/gdr.cpp:L152-L157](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/gdr.cpp#L152-L157) — `num_heads=query.h`、`seq_len=query.c`、`k_head_dim=query.w`、`v_head_dim=value.w`；并固定 `use_qk_l2norm_in_kernel=true`。

然后做一次**布局转置**：输入是按 `(t,h,d)`（通道=t）排的 ncnn::Mat，递推函数要按 `(h,t,d)` 连续，所以先把 q/k/v 重排：

[src/utils/gdr.cpp:L176-L194](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/gdr.cpp#L176-L194) — 把 `query_data`/`key_data`/`value_data` 从 `(t,h,d)` 拷成 `(h,t,d)` 的 `query_t`/`key_t`/`value_t`。

计算 \(\beta\) 与 \(g\)：

[src/utils/gdr.cpp:L204-L211](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/gdr.cpp#L204-L211) — `beta=sigmoid(b)`。

[src/utils/gdr.cpp:L213-L225](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/gdr.cpp#L213-L225) — `g=-exp(A_log)*softplus(a+dt_bias)`，每头一个 \(A\)、每个时间步一个 \(a\)。

初始化状态矩阵（首轮置零，否则从 `initial_state` 拷入）：

[src/utils/gdr.cpp:L232-L241](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/gdr.cpp#L232-L241) — `initial_state` 非空则 `memcpy`，否则 `memset` 为 0。

调用递推，再把输出布局从 `(h,t,d)` 转回 `(t,h,d)`，并把新状态写回：

[src/utils/gdr.cpp:L243-L267](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/gdr.cpp#L243-L267) — 调 `torch_recurrent_gated_delta_rule`，回写 `top_blob`（注意力输出）与 `state_out`（新状态）。

> 准确性说明：输出 `top_blob` 在 [L160](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/gdr.cpp#L160) 用 `k_head_dim` 作为宽度创建，而回写循环（L252–264）按 `v_head_dim` 步长写入。这两者一致的前提是目标模型里 \(d_k=d_v\)（Qwen3.5 混合架构满足此条件）；这是算子对调用方的隐含约定，不是 bug。

#### 4.3.4 代码实践

**实践目标**：用最小数值例子复现 5 步递推，确认「状态写回 value」的 DeltaRule 直觉。

**操作步骤**：

1. 令 \(d_k=d_v=1\)、单头、单步（\(L=1\)），首轮 `initial_state` 为空（\(S_0=0\)）。
2. 取 \(q=1,\ k=1,\ v=5\)，并设 \(g=-\ln 2\)（即 \(e^g=0.5\)）、\(\beta=\sigma(0)=0.5\)、\(\mathrm{scale}=1\)。
3. 按 [src/utils/gdr.cpp:L90-L126](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/gdr.cpp#L90-L126) 的五步算：
   - 衰减：\(\tilde{S}=0.5\times 0=0\)
   - 预测：\(m=k\tilde{S}=0\)
   - 修正：\(\delta=\beta(v-m)=0.5\times 5=2.5\)
   - 写回：\(S=\tilde{S}+k\delta=0+1\times 2.5=2.5\)
   - 读出：\(o=S\cdot q\cdot\mathrm{scale}=2.5\)
4. 想象「同一 key 再来一次、value 仍为 5」：第二步的预测 \(m=k\tilde{S}\) 会更接近 5，修正 \(\delta\) 更小——这就是 DeltaRule「让状态准确记忆 value」的收敛行为。

**需要观察的现象**：第一步输出 \(o=2.5\)（被 \(\beta\) 和 scale 缩放过的 value），状态 \(S=2.5\) 被写回供下一批使用。

**预期结果**：手算值与公式一致。带门控与 softplus 的完整数值验证需构造可调用的算子 wrapper，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么要在递推前对 q、k 做 L2 归一化（`use_qk_l2norm_in_kernel=true`）？

> **答**：归一化后 \(q\)、\(k\) 的范数为 1，使内积 \(k^\top S\)、\(S^\top q\) 的数值稳定在合理范围，避免状态矩阵在长序列下数值爆炸或坍缩；这也是 DeltaNet 论文的标准做法。

**练习 2**：门控 \(e^{g_t}\) 与更新强度 \(\beta_t\) 各自的语义是什么？

> **答**：\(e^{g_t}\in(0,1)\) 是**遗忘门**，每步把旧状态整体按比例衰减，让远期记忆逐步淡出；\(\beta_t\in(0,1)\) 是**写入门**，控制新修正量 \(\delta\) 写入状态的强度，相当于 DeltaRule 的「学习率」。

**练习 3**：为什么说这个算子的推理内存不随序列长度增长？

> **答**：因为它把历史压缩进固定大小的状态矩阵 \(S\)（每头 \(d_k\times d_v\)），无论已处理多少 token，\(S\) 的大小不变；这与自注意力需要随序列增长 KV cache 形成对比。

---

### 4.4 qwen3_5_ctx：sconv / gdr cache 的载体与传递

#### 4.4.1 概念说明

前两讲看到，`ShortConv` 与 `GatedDeltaRule` 各自有一个「跨批状态」需要保存：短卷积存最近的 \(K\) 行输入、DeltaRule 存状态矩阵 \(S\)。这些状态和 KV cache 一样，必须**随推理上下文 ctx 流动**，否则多轮对话或自回归解码就会丢失记忆。

为此，本项目在 ctx 类层次里专门加了一个派生类 `qwen3_5_ctx`，比基类 ctx 多两个字段：`sconv_cache` 与 `gdr_cache`。是否使用它，由两个配置字段 `sconv_cnt` / `gdr_cnt` 决定——它们分别表示 decoder 里 ShortConv 层和 GDR 层的**个数**。

#### 4.4.2 核心流程

ctx 的选用规则：

```text
create_ctx(sconv_cnt, gdr_cnt)
  ├─ 若 sconv_cnt>0 或 gdr_cnt>0  →  new qwen3_5_ctx   (带 sconv_cache / gdr_cache)
  └─ 否则                          →  new ncnn_llm_gpt_base_ctx  (仅 KV cache)
```

两类 cache 的传递遵循和 KV cache 完全相同的「**输入插槽 → 输出插槽**」循环，只是名字不同：

| 状态 | 输入插槽（喂入旧值） | 输出插槽（取出新值） |
| --- | --- | --- |
| KV cache（逐层） | `cache_k%d` / `cache_v%d` | `out_cache_k%d` / `out_cache_v%d` |
| ShortConv 状态 | `cache_conv%d` | `out_cache_conv%d` |
| GDR 状态 | `cache_gdr%d` | `out_cache_gdr%d` |

`%d` 是层号，循环上限分别是 `attn_cnt`（KV）、`sconv_cnt`（conv）、`gdr_cnt`（gdr）。

由此可以解释 u2-l2 留下的伏笔：共享文本运行时的 `llm_run_decoder_with_kv` 只处理 KV cache，**不认识**这两类额外插槽；所以遇到 qwen3.5 混合架构时，generate 里必须**内联展开** decoder 调用，手动把 conv/gdr 插槽也喂入、取出。

#### 4.4.3 源码精读

配置读取（构造函数）：

[src/ncnn_llm_gpt.cpp:L109-L114](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L109-L114) — 从 `model.json` 的 `setting` 读 `sconv_cnt` / `gdr_cnt`，二者都是**选填**（用 `contains` 守卫，缺省为 0）。成员声明在 [src/ncnn_llm_gpt.h:L110-L111](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L110-L111)，默认 0。

ctx 类层次：

[src/ncnn_llm_gpt.h:L71-L89](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h#L71-L89) — `qwen3_5_ctx` 继承 `ncnn_llm_gpt_ctx`，多出 `sconv_cache` / `gdr_cache`；`clone()` 把它们连同 KV cache、`cur_token`、`position_id` 一起复制（`ncnn::Mat` 是引用计数浅拷贝，配合 decoder「读旧写新」可安全共享，详见 u2-l5）。

选用规则：

[src/ncnn_llm_gpt.cpp:L9-L14](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L9-L14) — `create_ctx` 的核心分支。

**prefill 冷启动**（纯文本首发）：在主体 decoder 运行里，除了提取 KV cache，还提取两类新状态：

[src/ncnn_llm_gpt.cpp:L306-L320](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L306-L320) — 提取 `out_cache_conv%d` 与 `out_cache_gdr%d`（注意：冷启动时**不喂入**旧 cache，层内部从空状态开始）。

末位 token 的单步 decode 里，把这些刚提取的状态作为输入喂回，并取出更新后的状态：

[src/ncnn_llm_gpt.cpp:L362-L398](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L362-L398) — 先 `ex.input("cache_conv%d", …)` / `ex.input("cache_gdr%d", …)`，再 `ex.extract("out_cache_conv%d", …)` / `ex.extract("out_cache_gdr%d", …)`。

构造返回 ctx 时，把两类 cache 装进 `qwen3_5_ctx`：

[src/ncnn_llm_gpt.cpp:L422-L433](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L422-L433) — `create_ctx` 选出 `qwen3_5_ctx` 后，用 `dynamic_pointer_cast` 向下转型并搬入 `sconv_cache` / `gdr_cache`。

**generate 自回归**：这正是 u2-l2 所说的「内联展开」之处。先 `dynamic_pointer_cast<qwen3_5_ctx>` 判断；非 qwen 走共享 `llm_run_decoder_with_kv`，qwen 则手动展开：

[src/ncnn_llm_gpt.cpp:L912-L968](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L912-L968) — `if (!qwen_ctx)` 走共享函数；`else` 分支手动 `create_extractor`，依次 input KV、conv、gdr 旧 cache，再 extract 三类新 cache 回写到 ctx。conv/gdr 的 input 见 [L931-L940](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L931-L940)，extract 回写见 [L952-L965](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L952-L965)。

#### 4.4.4 代码实践

**实践目标**：把「配置 → ctx 选型 → cache 传递」整条链在源码里走一遍，并回答实践任务里的三个问题。

**操作步骤**：

1. **何时注册**：在 [src/ncnn_llm_gpt.cpp:L73](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L73) 确认 `register_gdr_layers` 在构造函数里、`load_param`/`load_model`（L75–76）之前调用，且只作用于 `decoder_net`。
2. **如何选 ctx**：读 [L109-L114](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L109-L114)（读配置）→ [L9-L14](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L9-L14)（`create_ctx` 分支）。只要 `sconv_cnt>0` 或 `gdr_cnt>0`，就造 `qwen3_5_ctx`。
3. **如何在 prefill/generate 传递**：在 prefill 冷启动主体 [L306-L320](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L306-L320)（提取）与末位 decode [L362-L398](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L362-L398)（喂入+回收）；在 generate [L912-L968](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L912-L968) 看 `qwen_ctx` 分支如何把 cache 从 ctx 取出、再写回。
4. **可选动手**：在 [L931](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L931) 附近临时加一行日志（如 `fprintf(stderr, "step: feed sconv[0] rows=%d\n", qwen_ctx->sconv_cache[0].h);`），观察生成每步时 cache 的形状是否稳定（不随步数增长）。改完务必还原。

**需要观察的现象**：生成过程中 `sconv_cache[i].h` 与 `gdr_cache[i]` 的大小应**保持不变**（短卷积状态恒为 \(K\) 行、GDR 状态恒为 \(d_k\times d_v\)），而 KV cache 的行数则随步数 +1。这正是混合架构「部分层内存固定」的体现。

**预期结果**：日志显示 conv/gdr cache 形状稳定、KV cache 单调增长。实际数值**待本地验证**（需 Qwen3.5 混合架构模型）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 generate 里对 qwen3.5 模型要「内联展开」decoder，而不能直接调 `llm_run_decoder_with_kv`？

> **答**：`llm_run_decoder_with_kv` 只处理 KV cache 的 `cache_k%d`/`cache_v%d` 插槽，不认识 `cache_conv%d`/`cache_gdr%d`；若直接调用，ShortConv/GDR 层拿不到上一批的状态，等于每步都从零状态开始，混合架构的记忆就断了。所以必须手动展开 extractor，把两类额外插槽也喂入、取出。

**练习 2**：`create_ctx` 用 `sconv_cnt>0 || gdr_cnt>0` 判断，为什么要用「或」而不是「与」？

> **答**：只要模型里**存在任何一种**混合架构层（短卷积或 GDR），就需要相应的 cache 字段，因此必须用 `qwen3_5_ctx`；用「与」会漏掉「只有 ShortConv 没有 GDR」（或反之）的模型，导致 cache 无处存放。

**练习 3**：`qwen3_5_ctx::clone()` 直接复制 `sconv_cache` / `gdr_cache`（`ncnn::Mat` 浅拷贝），为何安全？

> **答**：`ncnn::Mat` 引用计数，浅拷贝只共享底层缓冲；而 decoder 每步「读旧 cache、写新分配」，新 extract 出来的 Mat 会替换掉副本里的指针，不会改写被共享的旧缓冲，因此多份 ctx 互不污染（与 u2-l5 讲的 KV cache clone 同理）。

---

## 5. 综合实践

**任务**：画出 Qwen3.5 混合架构模型在 ncnn_llm 中「一次 prefill + 一步 generate」的**完整数据流图**，重点标注 GDR/ShortConv 状态的流动。

要求：

1. 从 `model.json` 的 `setting.sconv_cnt` / `setting.gdr_cnt` 出发，标出它们分别影响：构造函数里的 `register_gdr_layers` 调用、`create_ctx` 的分支、prefill/generate 里 cache 循环的上限。
2. 在图上用三种颜色/记号区分三类状态：KV cache（`cache_k/v%d` ↔ `out_cache_k/v%d`）、ShortConv 状态（`cache_conv%d` ↔ `out_cache_conv%d`）、GDR 状态（`cache_gdr%d` ↔ `out_cache_gdr%d`）。
3. 在「decoder 子网内部」框里，标出 ShortConv 与 GatedDeltaRule 两个自定义算子所在位置：输入 token 先经 ShortConv（用 `cache_conv`）做局部平滑，再进入 GatedDeltaRule（用 `initial_state`/`cache_gdr`）做线性注意力，输出再与普通注意力层的 KV cache 一起汇出。
4. 写出冷启动 prefill 与 generate 两个阶段、三类状态「是否喂入旧值、从哪里取新值」的对照表。

**参考要点**（可用来核对自画图）：

| 阶段 | KV cache | ShortConv 状态 | GDR 状态 |
| --- | --- | --- | --- |
| prefill 主体（冷） | 只取新值（`out_cache_k/v`） | 只取新值（`out_cache_conv`） | 只取新值（`out_cache_gdr`） |
| prefill 末位 decode | 喂入主体结果、取更新值 | 喂入主体结果、取更新值 | 喂入主体结果、取更新值 |
| generate 每步 | 喂入 ctx、取更新值回写 | 喂入 ctx、取更新值回写 | 喂入 ctx、取更新值回写 |

完成后，应能用自己的话回答：「为什么混合架构既需要 KV cache 又需要 sconv/gdr cache？它们各管什么？」（KV cache 管自注意力层的历史、随序列增长；sconv/gdr 状态管线性注意力/短卷积层的历史、大小固定。）

---

## 6. 本讲小结

- 自定义 ncnn 算子的套路是：继承 `ncnn::Layer` 重写 `forward` → 设置标志位 → 写 creator/destroyer → 用 `register_custom_layer` 按类型名登记。
- `register_gdr_layers` 把 `GatedDeltaRule` 与 `ShortConv` 登记进 `decoder_net`，且**必须在 `load_param`/`load_model` 之前**调用，否则 ncnn 解析 `.param` 时找不到构造函数。
- `ShortConv` 是带**滑动状态**的 depthwise 因果短卷积 + SiLU：每批把状态（拖尾 \(K\) 行）拼到新输入前做卷积，再输出新状态，状态在批与批之间衔接。
- `GatedDeltaRule` 是门控 DeltaRule 线性注意力：用一个固定 \(d_k\times d_v\) 状态矩阵 \(S\)，每步「衰减 → 用 key 查旧状态 → 用 (value−预测) 算修正 → 外积写回 → 用 query 读出」，内存不随序列增长。
- `sconv_cnt` / `gdr_cnt` 两个配置字段（选填，缺省 0）决定 `create_ctx` 是否选用带额外 cache 字段的 `qwen3_5_ctx`，并决定 decoder 调用里 conv/gdr 插槽循环的上限。
- 由于共享运行时 `llm_run_decoder_with_kv` 不认识 conv/gdr 插槽，qwen3.5 混合架构在 generate 里**内联展开** decoder，手动完成三类状态的喂入与回收——这是「共享运行时」设计主线的唯一例外。

## 7. 下一步学习建议

- **回到 u2-l4 / u2-l5 对照**：现在再读 generate 的自回归循环与 ctx 的多轮传递，会看到三类状态是如何与 `clone_ctx`、`position_id` 推进协同的；可以验证多轮对话里 conv/gdr 状态也像 KV cache 一样被累积保留。
- **向后接 u8-l6（接入新模型家族）**：若要接入一个新的混合架构模型，本讲的「注册时机 + ctx 选型 + cache 传递」就是改动清单的核心；需要判断新模型是否用到自定义算子、是否需要新的派生 ctx。
- **延伸阅读 ncnn Layer API**：如果想自己写一个自定义算子，可阅读 ncnn 官方的 `Layer` 基类文档与 `register_custom_layer` 用法，关注 `one_blob_only`、`support_inplace`、`support_vulkan` 等标志位对调度与 GPU 支持的影响。
- **对比 GDR 与 Mamba/GLA**：`GatedDeltaRule` 与 Mamba、Gated Linear Attention 同属线性 RNN 家族；理解了本讲的五步递推后，可对比这些变体在「门控方式、状态更新规则」上的差异。
