# 注意力 kernel：融合多头注意力与 decoder masked MHA

## 1. 本讲目标

在 u3-l1 中我们读了 layernorm、activation、add_residual 这类「elementwise + reduction」的小 kernel。本讲进入 FT 里**最复杂、也是性能收益最大**的一类 kernel：**融合的多头注意力（fused Multi-Head Attention, MHA）**。

读完本讲，你应当能够：

1. 说清楚「融合」二字的含义——为什么要把 `QK^T → softmax → PV` 三步压进**单个 CUDA kernel**，相比朴素实现省掉了哪些中间显存读写。
2. 看懂 `decoder_masked_multihead_attention` 这套文件的**分层组织**：参数结构（`.h`）→ 按精度/按 `size_per_head` 的调度（`.cu` + 子目录）→ 真正的 kernel 模板（`.hpp`）。
3. 理解自回归解码时 **causal mask（因果掩码）** 为何在这种「单 query」融合 kernel 里几乎是「免费」的，以及 `masked_tokens` 这种运行期掩码处理的是什么。
4. 说出 FP16 / BF16 / INT8（int8_mode==2）/ FP8 四种精度版本在**同一份模板**里是通过哪些条件分支和工具函数区分的。

> 本讲对应的最小模块：① `decoder_masked_multihead_attention` 的 fused masked MHA；② `decoder_masked_multihead_attention/` 子目录的多版本实现。

## 2. 前置知识

### 2.1 复习：标准注意力的数学定义

给定一组 query、key、value（都按头切分），单头注意力为：

\[
\mathrm{Attention}(Q,K,V) = \mathrm{softmax}\!\left(\frac{QK^{\top}}{\sqrt{D_h}}\right)V
\]

其中 \(D_h\) 是每个头的隐藏维度（`size_per_head`），\(\sqrt{D_h}\) 是缩放因子。多头（Multi-Head）就是把 \(D\) 维切成 \(H\) 个 \(D_h\) 维，每个头独立算一次注意力，再把结果拼回 \(D\) 维。

> 本讲统一使用仓库里的术语（见 `decoder_masked_multihead_attention.h:44-49`）：**B**=batch size、**L**=序列长度、**D**=隐藏维度、**H**=头数、**Dh**=每头隐藏维度 \(D_h = D/H\)。

### 2.2 朴素实现的「三段式」开销

如果照公式分步实现（这正是 FT 里 `UnfusedAttentionLayer` 的做法，见 u3-l3），需要：

1. 用 cuBLAS 做一次 GEMM 算 `QK^T`，把 `[B,H,L,L]` 的分数矩阵**写回显存**。
2. 启动 softmax kernel，把分数**读出来**、归一化、再**写回去**。
3. 再用 cuBLAS 做一次 GEMM 算 `PV`，**再读一次**分数、**再读一次** V，写出结果。

中间那个 `[B,H,L,L]` 的分数矩阵要经历「写→读→写→读」至少两个来回，对显存带宽是巨大浪费。融合 kernel 的核心动机就是：**把这个分数矩阵永远留在片上（共享内存 + 寄存器），不让它落盘到显存**。

### 2.3 需要的 CUDA 概念

- **共享内存（shared memory / smem）**：每个线程块（block）内部的高速 SRAM，约几十 KB，延迟远低于显存。本讲里 `qk_smem`、`q_smem` 都在这里。
- **寄存器（register）**：每个线程私有的最快存储。`out` 累加器放在这里。
- **warp shuffle（`__shfl_xor_sync`）**：让同一 warp（32 个线程）内的寄存器直接交换数据，不经过共享内存，是做归约（reduction）的高效手段（u3-l1 的 `warpReduceSum` 已用过）。
- **grid/block 配置**：本讲 kernel 的 `grid = (num_heads, batch_size)`，即「每个 block 负责一个 (序列, 头)」。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [decoder_masked_multihead_attention.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/decoder_masked_multihead_attention.h) | 参数结构 `Multihead_attention_params` 与对外函数声明，是这套 kernel 的「接口契约」。 |
| [decoder_masked_multihead_attention.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/decoder_masked_multihead_attention.cu) | 顶层调度器：按 `hidden_size_per_head` 分发到 12 个特化版本，并按数据类型（FP32/FP16/BF16/FP8）选模板。 |
| [decoder_masked_multihead_attention/decoder_masked_multihead_attention_template.hpp](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/decoder_masked_multihead_attention/decoder_masked_multihead_attention_template.hpp) | **核心 kernel 模板**：真正实现 `QK^T→softmax→PV` 融合逻辑的 `masked_multihead_attention_kernel`。 |
| [decoder_masked_multihead_attention/decoder_masked_multihead_attention_64.cu](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/decoder_masked_multihead_attention/decoder_masked_multihead_attention_64.cu) | 子目录里**每个 `size_per_head` 对应一个 `.cu` 文件**（32/48/…/256）。本文件代表 `Dh=64` 这一档：定义启动宏 + 按序列长度选 block 配置 + 显式模板实例化。 |
| [decoder_masked_multihead_attention_utils.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/decoder_masked_multihead_attention_utils.h) | 工具函数库：向量类型、`add`/`mul`/`fma` 的 PTX 内联汇编重载、rotary embedding、INT8/FP8 的量化反量化、`qk_dot` 点积。 |
| [DecoderSelfAttentionLayer.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/DecoderSelfAttentionLayer.cc) | **调用方**：把 layer 的输入组装成 `params` 后调用 `masked_multihead_attention(params, stream)`，是理解 kernel 如何被上层使用的入口。 |

> 说明：这套代码最初源自 NVIDIA TensorRT 开源仓库的「QKVToContext / fused attention」实现，FT 在此基础上扩展了 KV cache、beam search、INT8/FP8、rotary、ALiBi、T5 relative bias 等推理场景需要的能力。因此你会看到它风格与 FT 自己写的 kernel（如 layernorm）略有不同——更底层、模板参数更多、大量 PTX 汇编。

## 4. 核心概念与源码讲解

### 4.1 参数结构与设计动机：为什么这样组织输入

#### 4.1.1 概念说明

注意力计算需要「一堆零散的指针和标量」：Q/K/V 及其 bias、KV cache、缩放因子、当前时间步、各种 mask……如果把它们当作几十个函数参数逐个传，既容易写错也难维护。FT 的做法是把这些**全部塞进一个 POD 结构体 `Multihead_attention_params`**，按值传给 kernel（`__global__` 函数按值接收结构体，编译器会把字段放进常量内存/寄存器，访问很快）。

这套结构体还用模板参数 `bool CROSS_ATTENTION` 区分两种用法：
- **masked self-attention（本讲重点）**：decoder 自回归时，query 是「当前这一个新 token」，K/V 来自**自己累积的 cache**。
- **cross-attention**：decoder 去关注 encoder 的输出（用于 T5/BART 这类 encoder-decoder 模型），K/V 来自 encoder memory。`CROSS_ATTENTION=true` 时多出 `memory_length_per_sample` 等字段。

#### 4.1.2 核心流程

一个 block（对应一个 `<序列, 头>`）的工作可以概括为：

```
1. 解析 blockIdx → 得到 (batch_idx bi, head_idx hi)；若该序列 finished 则提前 return
2. 确定 tlength = 当前序列的有效长度（决定循环上界，即 causal 边界）
3. 把当前 token 的 Q、K、V（含 bias）加载到共享内存/寄存器，并把新 K、V 写进 cache
4. for ti in [first_step, tlength]:            // QK^T
       qk[ti] = dot(Q, K_cache[ti]) / sqrt(Dh)   // 结果存 smem，不落盘
       （可选）加 relative_bias / ALiBi linear_bias
5. 求 qk_max（block 归约）→ softmax（exp + 归一化，原地更新 smem）
6. for ti in [first_step, tlength]:            // PV
       out += softmax[ti] * V_cache[ti]          // 累加在寄存器 out
7. 把 V_PER_ITER 组线程的部分输出做树形归约 → vo==0 的线程写回 params.out
```

第 4、5、6 步的全部中间结果（分数 `qk`、归一化后的 `logits`、部分和 `out`）都**不离开片上存储**——这就是「融合」的全部含义。

#### 4.1.3 源码精读

参数基类 `Multihead_attention_params_base` 定义了所有字段，注释里写明了 B/L/D/H/Dh 的含义：

[decoder_masked_multihead_attention.h:51-120](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/decoder_masked_multihead_attention.h#L51-L120) — 参数结构体。几个关键字段：

- `q/k/v` 与 `q_bias/k_bias/v_bias`：当前 token 的输入和偏置（维度 B×D 和 D）。
- `k_cache`/`v_cache`：累积的 K、V 缓存（至少 B×L×D），是自回归加速的关键。
- `cache_indir`：beam search 时用来「按 beam id 间接寻址」cache 的索引数组。
- `inv_sqrt_dh`：在 host 上预先算好的 \(1/\sqrt{D_h}\)，避免 kernel 内做开方。
- `timestep`：当前时间步；`masked_tokens`：运行期掩码（见 4.3）；`relative_attention_bias`（T5）/`linear_bias_slopes`（ALiBi）：位置偏置。
- `int8_mode`、`qkv_scale_out`、`attention_out_scale`、`query_weight_output_scale` 等：INT8/FP8 量化相关（见 4.4）。

模板偏特化区分 self / cross，并用别名简化使用：

[decoder_masked_multihead_attention.h:157-161](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/decoder_masked_multihead_attention.h#L157-L161) — `Masked_multihead_attention_params<T>` 等价于 `Multihead_attention_params<T, false>`，`Cross_multihead_attention_params<T>` 等价于 `<T, true>`。

对外只暴露**按数据类型重载**的两个自由函数，BF16/FP8 受宏守卫：

[decoder_masked_multihead_attention.h:173-188](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/decoder_masked_multihead_attention.h#L173-L188) — 声明 `masked_multihead_attention`（FP32 / `uint16_t`=FP16 / `__nv_bfloat16` / `__nv_fp8_e4m3`）与 `cross_multihead_attention`。注意 FP16 走的是 `uint16_t` 重载（因为 `half` 在底层就是 16 位无符号整数存储）。

#### 4.1.4 代码实践：阅读调用方如何填参数

**实践目标**：理解 kernel 不是孤立存在的——上层 layer 把输入组装成 `params` 再一次性调用。

**操作步骤**：
1. 打开 [DecoderSelfAttentionLayer.cc](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/DecoderSelfAttentionLayer.cc) 第 100–145 行。
2. 观察它如何逐字段给 `params` 赋值（`params.k_cache`、`params.timestep`、`params.inv_sqrt_dh`…）。
3. 注意第 118 行：`inv_sqrt_dh = 1.F / (sqrtf(hidden_size_per_head) * q_scaling)`——开方在 host 算好，kernel 里只做乘法。
4. 第 144 行就是唯一一次 kernel 启动：`masked_multihead_attention(params, stream);`。

**需要观察的现象**：调用方完全没有 `<<<grid, block>>>` 这样的启动语法——它只调一个普通函数，**所有启动细节被藏在 `.cu` 调度层里**（见 4.2）。这印证了 u3-l1 讲过的 `invokeXxx` 约定：layer 永远不直接写 `<<<>>>`。

**预期结果**：你能说清楚「layer 负责填参数 + 调函数，`.cu` 负责选模板 + 配置 grid/block」的分工。结果是否能在你本机复现属于「待本地验证」（需要 GPU 与编译环境）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `inv_sqrt_dh` 要在 host 上预先算好，而不是每个线程在 kernel 里各自算 `sqrtf`？

> **答案**：`sqrtf` 是相对昂贵的标量运算，而 kernel 里每个 block 都会用同一个值。在 host 算一次、塞进结构体（进常量内存），所有线程共享，避免成千上万个 block 重复开方。

**练习 2**：参数结构体里既有 `timestep` 又有 `masked_tokens`，它们分别控制什么？

> **答案**：`timestep`（及 `length_per_sample`）决定 `tlength`，即「causal 边界」——kernel 循环 `ti <= tlength`，结构性地保证不看未来。`masked_tokens` 是一个逐位置的 bool 数组，用于在已计算出的分数里把 padding 等位置显式置零，是「运行期掩码」。

---

### 4.2 多版本文件与调度：一份模板，12 个特化 + 4 种精度

#### 4.2.1 概念说明

这个 kernel 高度依赖编译期常量来优化：
- `Dh`（每头维度）：必须是编译期常量，才能确定「一个向量装几个元素」「一个 block 要几个线程」。
- `THREADS_PER_KEY`/`THREADS_PER_VALUE`/`THREADS_PER_BLOCK`：决定每个线程负责几个 key/value，影响访存向量化程度。

但 `Dh` 在运行时才知道（不同模型 `size_per_head` 不同：BERT 是 64，GPT-J 是 256，有些是 80、112…）。于是 FT 的套路是：**为每个常见 `Dh` 值各编译一份特化代码**，运行时用 `switch` 选。这正是子目录里那 12 个 `decoder_masked_multihead_attention_<N>.cu` 文件（N=32,48,64,80,96,112,128,144,160,192,224,256）的由来。

这套「运行期枚举 → 编译期模板」的分发套路，你在 u1-l4（data_type 分发 bertExample）和 u2（DataType→模板）已经见过，这里是它最典型的工业级应用。

#### 4.2.2 核心流程

调度分三层：

```
masked_multihead_attention(params, stream)          // .h 声明，.cu 实现的按类型重载
   └─ multihead_attention_<T, KERNEL_PARAMS_TYPE>   // 顶层 .cu：switch(Dh) 选 <Dh, Dh_MAX>
        └─ mmha_launch_kernel<T,Dh,Dh_MAX,...>      // 子目录 .cu（如 _64.cu）：选 block 配置
             └─ masked_multihead_attention_kernel   // template.hpp：真正的 kernel
                <T,Dh,Dh_MAX,THDS_PER_KEY,THDS_PER_VALUE,THDS_PER_BLOCK,...>
```

`Dh_MAX` 是把 `Dh` 向上取整到 32/64/128/256（分配共享内存用上限，`Dh<Dh_MAX` 时多余的部分用 `Dh==Dh_MAX || vi<Dh` 判断跳过）。

#### 4.2.3 源码精读

**第一层：顶层 `.cu` 的 `switch`**——按 `hidden_size_per_head` 选 `<Dh, Dh_MAX>`，对应 12 档：

[decoder_masked_multihead_attention.cu:25-68](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/decoder_masked_multihead_attention.cu#L25-L68) — `multihead_attention_` 函数。例如 `case 64: mmha_launch_kernel<T, 64, 64, ...>`、`case 96: <T, 96, 128, ...>`（96 取整到 128）。`default: assert(false)` 表示其它维度不支持。

**按数据类型分发**——同一份 switch 对 FP32/FP16/BF16/FP8 各实例化一次：

[decoder_masked_multihead_attention.cu:72-102](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/decoder_masked_multihead_attention.cu#L72-L102) — 注意第 96–102 行的 `#ifdef ENABLE_FP8` 分支，把 `__nv_fp8_e4m3` 分发到同一套 `multihead_attention_` 模板。BF16 同理在第 86–92 行。这呼应 u1-l2 讲过的：ENABLE_BF16/ENABLE_FP8 是编译期条件编译开关。

**第二层：子目录 `.cu`（以 `_64.cu` 为例）——启动宏**：

[decoder_masked_multihead_attention_64.cu:27-38](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/decoder_masked_multihead_attention/decoder_masked_multihead_attention_64.cu#L27-L38) — `MMHA_LAUNCH_KERNEL` 宏做三件事：① 用 `smem_size_in_bytes` 算共享内存大小；② 设 `grid = (num_heads, batch_size)`——即每个 block 负责一个 (序列, 头)；③ 用 `<<<grid, block, smem_sz, stream>>>` 启动真正的 kernel。

**按序列长度动态选 block 配置**（关键优化）：

[decoder_masked_multihead_attention_64.cu:43-71](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/decoder_masked_multihead_attention/decoder_masked_multihead_attention_64.cu#L43-L71) — `mmha_launch_kernel` 用 `tlength`（序列当前长度）分桶：

| tlength | THREADS_PER_BLOCK | THREADS_PER_KEY |
|---|---|---|
| < 32 | 64 | 4 |
| < 2048 | 128 | 2 |
| ≥ 2048 | 256 | 1 |

直觉：序列越短，每个 key 分到的线程越多（`THREADS_PER_KEY` 大），并行点积更快；序列越长，要把更多线程用来覆盖更多 key，于是 `THREADS_PER_KEY` 降到 1、block 放大到 256。另外用 `params.cache_indir == nullptr` 切换 `HAS_BEAMS` 模板参数（beam search 时需要间接寻址）。

`THREADS_PER_VALUE` 来自一个编译期 trait：

[decoder_masked_multihead_attention_template.hpp:1947-1956](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/decoder_masked_multihead_attention/decoder_masked_multihead_attention_template.hpp#L1947-L1956) — `threads_per_value_t<T, Dh_MAX>::value = Dh_MAX * sizeof(T) / 16`，即「每 16 字节（一条 cache 线宽）配几个线程」。FP8 特化为 `Dh_MAX*4/16`，因为 FP8 的 V 在 kernel 内部按 float（4 字节）加载。

每个子目录 `.cu` 末尾是**显式模板实例化**（如 `_64.cu:75-99`），为 `float`/`uint16_t`/`__nv_bfloat16`/`__nv_fp8_e4m3` × `masked`/`cross` 共 8 种组合各 `template void mmha_launch_kernel<...>(...)`，让编译器真正生成这些版本的机器码。

#### 4.2.4 代码实践：画出调度决策树

**实践目标**：把「输入 → 最终启动的 kernel 实例」这条决策链画清楚。

**操作步骤**：
1. 假设模型 `size_per_head = 64`、数据类型 FP16、当前 `timestep = 50`、无 beam search。
2. 沿着 4.2.2 的流程图，逐层推断：
   - `masked_multihead_attention(params<half>, ...)` → 命中哪个 `.cu` 重载？（提示：`half` 对应 `uint16_t` 重载）
   - `multihead_attention_` 的 switch 命中哪一 case？（`case 64`）
   - `_64.cu` 的 `mmha_launch_kernel` 里，`tlength=50` 落在哪个桶？（`<2048` → 128 线程、`THREADS_PER_KEY=2`）
3. 最终被启动的 kernel 全名应该是 `masked_multihead_attention_kernel<uint16_t, 64, 64, 2, ?, 128, false, false>`。请把 `?`（THREADS_PER_VALUE）按 4.2.3 的公式手算出来（提示：`Dh_MAX=64`，`sizeof(uint16_t)=2`）。

**需要观察的现象**：通过这个练习，你会直观看到「为什么这套文件要拆成 1 个顶层 `.cu` + 12 个子目录 `.cu`」——因为每个 `(Dh, 数据类型)` 组合都要被编译器单独实例化成一份机器码，且 block 配置还要随序列长度动态变化。

**预期结果**：`?` = `64 * 2 / 16 = 8`。

#### 4.2.5 小练习与答案

**练习 1**：为什么不直接写一个「`Dh` 作为运行期变量」的通用 kernel，而要为 12 个 `Dh` 各编译一份？

> **答案**：`Dh` 被大量用于确定向量宽度、循环展开次数、共享内存大小、`static_assert` 约束。若它是运行期变量，编译器无法展开循环、无法确定向量化宽度，性能会大幅下降。用编译期常量换来激进的循环展开和向量化，代价只是二进制体积变大（因此才需要按需实例化）。

**练习 2**：`Dh=96` 时 `Dh_MAX` 为什么是 128 而不是 96？

> **答案**：`Dh_MAX` 用于**预留共享内存和向量宽度的上限**，必须能被向量化宽度整除（`static_assert(Dh_MAX % THREADS_PER_KEY == 0)`）。把 96 取整到 128 能让线程/向量划分整齐；真正计算时用 `Dh == Dh_MAX || vi < Dh` 守卫，跳过 `vi >= 96` 的越界部分，既保证性能又保证正确性。

---

### 4.3 融合 kernel 核心算法：QK^T → softmax → PV 全在片上

> 这是本讲最核心的一节。建议你边读边对照 `decoder_masked_multihead_attention_template.hpp` 第 1119–1939 行。

#### 4.3.1 概念说明：causal mask 为什么在这里「免费」

在 prefill/context 阶段（一次处理整段 prompt，有 L 个 query），因果性要求位置 i 只能看位置 ≤i，需要一个下三角 mask 矩阵显式挡住未来。

但**解码阶段每步只来 1 个新 token**——当前步只有「位置 t」这一个 query，它本来就应该看 `0..t` 的所有 key。所以这套 decoder masked MHA 的 causal 性是**结构性保证**的：循环上界是 `ti <= tlength`，**根本不会去读 tlength 之后的位置**，无需任何下三角矩阵。

那 `masked_tokens`（运行期 bool 数组）处理的是什么？是 padding、已经 finished 的序列这类「虽然时间上在过去，但语义上不想 attend」的位置——把它们在 softmax 里强制置零。

#### 4.3.2 核心流程

每个 block（一个 `<序列 bi, 头 hi>`）的完整执行流程：

```
─── 准备 ───
bi=blockIdx.y, hi=blockIdx.x;  若 finished[bi] 提前 return
tlength = (cross) ? memory_len-1 : timestep 或 length_per_sample + max_prefix_prompt
first_step = max(0, tlength+1 - memory_max_len)   # 循环下界（环形 cache）
加载当前 token 的 Q（→q_smem）、K、V（→寄存器），int8_mode==2 时反量化

─── ① QK^T ───
for ti in [first_step, tlength):       # 注意：causal 上界
    k_vec = 从 k_cache 读出第 ti 步的 K（HAS_BEAMS 时按 cache_indir 间接寻址）
    qk = dot(Q, k_vec) * inv_sqrt_dh   # Qk_dot 用 warp shuffle / HMMA
    （可选）qk += relative_attention_bias   # T5
    （可选）qk += linear_bias_slopes * dist # ALiBi
    if is_mask(ti): 不更新 qk_max       # padding → 后续 exp 得 0
    qk_smem[ti-first_step] = qk          # ★ 写共享内存，不写显存

─── ② softmax（原地）───
qk_max = block_wide_reduce_max(qk_smem)   # warp shuffle + red_smem 两级归约
for ti: logit = exp(qk_smem[ti] - qk_max) # 减最大值防溢出
        if is_mask(ti): logit = 0
sum = block_wide_reduce_sum(logit)
inv_sum = 1 / (sum + 1e-6)
for ti: qk_smem[ti] = logit * inv_sum     # ★ 归一化后原地覆盖

─── ③ PV（累加在寄存器）───
out = 0
for ti in [first_step, tlength):
    v_vec = 从 v_cache 读出第 ti 步的 V
    out = fma(logit, v_vec, out)          # ★ out 是寄存器，从不落盘
把当前步的 V（含 bias）写回 v_cache[tlength_circ]

─── ④ 归约与写回 ───
把 V_PER_ITER 组线程的部分 out 做树形归约（经 out_smem）
vo==0 的线程把最终 out 写到 params.out（FP8/INT8 时量化后写）
```

对照朴素三段式，省掉的显存读写包括：`[B,H,L,L]` 分数矩阵的写+读、softmax 的读+写、PV 中对分数和部分和的反复读写。这些现在全部发生在共享内存和寄存器里。

#### 4.3.3 源码精读

**kernel 签名与 grid**：

[decoder_masked_multihead_attention_template.hpp:1119-1198](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/decoder_masked_multihead_attention/decoder_masked_multihead_attention_template.hpp#L1119-L1198) — 模板参数里 `DO_CROSS_ATTENTION` 和 `HAS_BEAMS` 都是 bool；第 1196 行 `if (params.finished[bi]) return;` 让已完成序列的 block 立刻退出，避免无效计算（呼应 u5/u6 会讲的 finished mask）。

**共享内存布局**：`qk_smem`（float，存分数 + 复用为 logits）、`out_smem`（最终归约）、`q_smem`（当前 Q）、`red_smem`（block 归约）。大小由 `smem_size_in_bytes` 动态计算：

[decoder_masked_multihead_attention_template.hpp:1059-1094](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/decoder_masked_multihead_attention/decoder_masked_multihead_attention_template.hpp#L1059-L1094) — 取 `max(softmax_sz, red_sz, transpose_rotary_size)`，因为这几个阶段时间上不重叠，可以复用同一块 smem。

**causal 边界 tlength 与环形 cache**：

[decoder_masked_multihead_attention_template.hpp:1224-1229](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/decoder_masked_multihead_attention/decoder_masked_multihead_attention_template.hpp#L1224-L1229) — `tlength` 是循环上界（cross 用 encoder 长度，masked 用 timestep 或 `length_per_sample + max_prefix_prompt`）；`first_step = max(0, tlength+1-memory_max_len)` 实现**环形 cache**——当序列长度超过 `memory_max_len` 时，从头覆盖，下界 `first_step` 随之上移。`tlength_circ = tlength % memory_max_len` 是写入新 V 时的环形位置。

**① QK^T 主循环**：

[decoder_masked_multihead_attention_template.hpp:1516-1593](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/decoder_masked_multihead_attention/decoder_masked_multihead_attention_template.hpp#L1516-L1593) — 每步从 `k_cache` 向量化读出 K，做点积。第 1567 行 `Qk_dot<T, THREADS_PER_KEY>::dot(q_vec, k) * params.inv_sqrt_dh` 是核心：`THREADS_PER_KEY` 个线程合作算一个点积（用 warp shuffle 汇总）。`is_mask` 为真时不更新 `qk_max`（第 1590 行），使后续 `exp(-inf)=0`，padding 自然被忽略。

ALiBi 线性位置偏置在这一步加进去，注释画出了 input/pad/output 的位置示意：

[decoder_masked_multihead_attention_template.hpp:1578-1589](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/decoder_masked_multihead_attention/decoder_masked_multihead_attention_template.hpp#L1578-L1589) — `(ki - qi) * slope[hi]`，且要扣除 padding 数量。

**点积的高效实现**（在 utils 里）：

[decoder_masked_multihead_attention_utils.h:595-674](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/decoder_masked_multihead_attention_utils.h#L595-L674) — `qk_dot_` 用 shuffle 归约；FP16 还提供 `qk_hmma_dot_`（第 649 行起），用 half 矩阵乘累加指令（`hmma`）把 2×2 的 half 点积压成一条指令。

**② softmax（两级 block 归约）**：

[decoder_masked_multihead_attention_template.hpp:1595-1664](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/decoder_masked_multihead_attention/decoder_masked_multihead_attention_template.hpp#L1595-L1664) — `qk_max` 先在 warp 内 `__shfl_xor_sync` 归约（第 1600 行），再跨 warp 经 `red_smem` 归约（第 1609–1624 行），最后广播。然后第 1629–1646 行算 `logit = exp(qk - qk_max)`、mask 置零、`block_sum` 求和、`inv_sum = 1/(sum+1e-6f)`（用 `__fdividef` 快速除法），第 1659 行原地归一化。`+1.e-6f` 防止除零。

**③ PV 累加（寄存器 out）**：

[decoder_masked_multihead_attention_template.hpp:1729-1768](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/decoder_masked_multihead_attention/decoder_masked_multihead_attention_template.hpp#L1729-L1768) — 第 1734 行从 `v_cache_batch` 读 V，第 1765 行 `out = fma(logit, v, out)` 累加到寄存器 `out`。注意 `out` 是线程私有寄存器，整段循环里**一次显存写都没有**。为支持环形 cache，分成 `ti < memory_max_len`（第 1729 行）和 `ti >= memory_max_len`（第 1769 行）两段循环，注释说明是为了让编译器能优化掉取模运算。

**当前步 V 的写回**：

[decoder_masked_multihead_attention_template.hpp:1817-1881](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/decoder_masked_multihead_attention/decoder_masked_multihead_attention_template.hpp#L1817-L1881) — 第 1857 行把加完 bias 的 V 写回 `v_cache[tlength_circ * Dh]`（环形位置），供下一步使用；同时用当前步的 logit 初始化 `out`。

**④ 最终树形归约与写回**：

[decoder_masked_multihead_attention_template.hpp:1886-1938](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/decoder_masked_multihead_attention/decoder_masked_multihead_attention_template.hpp#L1886-L1938) — 第 1889 行的循环把 `V_PER_ITER` 组线程的部分 `out` 经 `out_smem` 做二叉树归约（上半组写 smem、下半组读并 add）。第 1913 行 `vo==0` 的线程把最终结果写到 `params.out`；FP8/INT8 在这里做最后的量化（见 4.4）。

#### 4.3.4 代码实践：对比融合 vs 朴素实现的显存读写

**实践目标**：用「记账法」量化融合 kernel 到底省了多少显存读写。

**操作步骤**：
1. 假设 `batch=1, head=1, L=512, Dh=64`，朴素三段式会产生一个 `[1,1,512,512]` 的 float 分数矩阵（约 1MB）。
2. 列出朴素实现里这个分数矩阵被读写几次：① GEMM 写出 → ② softmax 读入 → ③ softmax 写出 → ④ PV 读入。
3. 对照本讲 4.3.2 的流程，确认融合 kernel 里这个分数矩阵**只存在于 `qk_smem`（共享内存）**，对显存的读写次数为 0。
4. 再看 `out`（部分和）：朴素实现里 PV 的中间结果要写显存，融合 kernel 里它是寄存器，写显存 0 次。

**需要观察的现象**：你会得出「融合把 ~1MB 的中间矩阵从『4 次显存往返』变成『0 次』」的结论。这就是 FT 在 decoder 推理上比 PyTorch 朴素实现快数倍的核心原因之一——**注意力是访存密集型，省带宽就是省时间**。

**预期结果**：能写出一张「中间张量 × 朴素读写次数 × 融合读写次数」的对比表。具体耗时数字属于「待本地验证」（需要 GPU 实测）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 softmax 要先求 `qk_max` 再做 `exp(qk - qk_max)`，而不是直接 `exp(qk)`？

> **答案**：数值稳定性。如果直接 `exp`，某些分数可能很大导致 `exp` 上溢出为 inf。减去最大值后最大的一项变成 `exp(0)=1`，其余都 ≤1，既防上溢也方便归一化。求 max 本身需要一个 block 级归约（`__shfl_xor_sync` + `red_smem`）。

**练习 2**：`__fdividef(1.f, sum + 1.e-6f)` 里的 `1.e-6f` 起什么作用？为什么用 `__fdividef` 而不是普通 `/`？

> **答案**：`1.e-6f` 防止 `sum=0`（极端全 mask 情况）时除零得到 nan/inf。`__fdividef` 是 CUDA 的快速除法内置函数，比标准 `/` 快，注意力这种热路径上值得用。

**练习 3**：环形 cache（`first_step`、`tlength_circ`）解决什么问题？

> **答案**：当生成长度超过预分配的 `memory_max_len` 时，K/V cache 不能无限增长。环形策略让新 token 覆盖最老的位置（`tlength_circ = tlength % memory_max_len`），并通过上移 `first_step` 跳过被覆盖的老 key，实现固定显存的「滑动窗口」注意力。

---

### 4.4 多精度支持：FP16 / BF16 / INT8 / FP8 怎么共用一份模板

#### 4.4.1 概念说明

同一份 kernel 模板要服务四种精度，靠三个手段：

1. **模板参数 `T`**：决定存储精度（FP16=`uint16_t`、BF16=`__nv_bfloat16`、FP8=`__nv_fp8_e4m3`、FP32=`float`）。计算精度 `Tk` 由 `kernel_type_t<T>` 推导——**FP8 的 `Tk` 是 float**（即 FP8 只在「存储」时是 8 位，计算用 FP32）。
2. **向量类型与 PTX 重载**：`add`/`mul`/`fma` 对每种类型都有专门的 PTX 内联汇编重载（如 `add.f16x2` 一次算两个 FP16）。
3. **运行期分支 + 宏**：INT8（`int8_mode==2`）和 FP8（`FP8_MHA_KERNEL`）在加载 Q/V 和写回 output 处插入**量化/反量化**分支。

#### 4.4.2 核心流程

```
加载 Q（int8_mode==2? 反量化 int8→Tk : vec_conversion 原样/升精度）
   ── QK^T、softmax 用 Tk（FP16/BF16/FP8 都用 FP32 累加关键量）──
加载 V（同理 int8_mode==2 反量化）
   ── PV 累加用 Tk ──
写回 out：
   FP8:  out *= query_weight_output_scale * attn_output_weight_input_scale_inv → fp8
   INT8: out *= attention_out_scale → cast_to_int8
   其它: vec_conversion 原样写
```

FP8 还有个特别之处：softmax 的 `exp` 多乘了一个 `query_weight_output_scale^2` 因子（fake quantization 风格的缩放）。

#### 4.4.3 源码精读

**计算精度 `Tk` 的推导（FP8→float）**：

[decoder_masked_multihead_attention_template.hpp:1045-1055](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/decoder_masked_multihead_attention/decoder_masked_multihead_attention_template.hpp#L1045-L1055) — `kernel_type_t<__nv_fp8_e4m3>::Type = float`。所以 `using Tk = ...` 让 FP8 kernel 内部所有累加用 float，只有落盘到 cache / output 时才转回 fp8。

**INT8（int8_mode==2）加载 Q 时的反量化**：

[decoder_masked_multihead_attention_template.hpp:1246-1254](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/decoder_masked_multihead_attention/decoder_masked_multihead_attention_template.hpp#L1246-L1254) — 读 `int8_t`，用 `float_from_int8` 转成 float，再乘 `qkv_scale_out[0]` 缩放，最后 `convert_from_float` 转成 `Tk`。V 的加载在第 1828–1836 行同理（乘 `qkv_scale_out[2]`）。

**FP8 的 softmax 缩放**：

[decoder_masked_multihead_attention_template.hpp:1631-1643](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/decoder_masked_multihead_attention/decoder_masked_multihead_attention_template.hpp#L1631-L1643) — `FP8_MHA_KERNEL` 为真时，`exp` 的指数多乘 `query_weight_output_scale[0]^2`，把 FP8 量化的尺度因子吸收进 softmax。

**写回时的量化（FP8 / INT8）**：

[decoder_masked_multihead_attention_template.hpp:1913-1938](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/decoder_masked_multihead_attention/decoder_masked_multihead_attention_template.hpp#L1913-L1938) — FP8 用 `result_scale = query_weight_output_scale * attention_output_weight_input_scale_inv` 缩放后写 fp8；INT8 用 `attention_out_scale` 缩放再 `cast_to_int8`。这两条量化路径承接 u9（量化讲义）会讲的 ScaleList 机制。

**量化反量化工具函数（在 utils 里）**：

[decoder_masked_multihead_attention_utils.h:943-1035](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/decoder_masked_multihead_attention_utils.h#L943-L1035) — `float_from_int8`（int8→float）、`cast_to_int8`（float→int8），都基于把多个 int8 打包进 int16/int32/int64 再用 PTX 转换的技巧，保证一条指令处理多个元素。FP8/BF16/FP16 的 `convert_from_float` 在第 730–860 行，同样大量用 PTX（`cvt.rn.f16.f32`）。

**FP8 的向量类型定义**：

[decoder_masked_multihead_attention_utils.h:62-71](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/kernels/decoder_masked_multihead_attention_utils.h#L62-L71) — `fp8_2_t`/`fp8_4_t`/`fp8_8_t` 让 FP8 也能 2/4/8 元素一组向量化访存，BF16 有对应的 `bf16_4_t`/`bf16_8_t`（第 47–60 行）。向量化是这套 kernel 高带宽的关键：每次访存尽量读满 16 字节。

#### 4.4.4 代码实践：追踪一条 INT8 数据的旅程

**实践目标**：理解同一份模板如何通过运行期 `int8_mode` 分支处理 INT8。

**操作步骤**：
1. 假设 `int8_mode == 2`（w8a8，权重和激活都是 INT8）。
2. 跟踪一个 INT8 的 Q 元素从「显存里的 int8」到「kernel 内部参与点积」的全过程：
   - 读：`*reinterpret_cast<const int8_t*>(params.q)[offset]`（4.4.3 第一条）。
   - 反量化：`float_from_int8(...) * qkv_scale_out[0]` → float。
   - 转内部精度：`convert_from_float(q, ...)` → `Tk`。
   - 参与点积：`Qk_dot::dot(...)`。
3. 再跟踪输出：累加完的 float `out` → `* attention_out_scale` → `cast_to_int8` → 写成 int8 到 `params.out`。

**需要观察的现象**：你会看到「**计算用高精度（float/Tk），存储用低精度（int8）**」的模式——这正是量化推理能在几乎不损精度的情况下省带宽、省显存的核心套路（u9 会展开讲）。

**预期结果**：能画出「int8 存储 → 反量化 → FP32 计算 → 量化 → int8 存储」的往返图。

#### 4.4.5 小练习与答案

**练习 1**：为什么 FP8 把 `Tk` 设成 float，而不是用 FP8 做累加？

> **答案**：FP8 只有 8 位（E4M3），精度很低，直接用它做点积累加（很多次加法）会累积巨大误差。所以 FP8 只在「存储/传输」时是 8 位以省带宽，所有累加（点积、PV 的 fma）都在 FP32 里做。这是「低精度存储 + 高精度计算」的标准做法。

**练习 2**：`MMHA_USE_FP32_ACUM_FOR_LOGITS` 和 `MMHA_USE_FP32_ACUM_FOR_OUT` 这两个宏如果定义，会改变什么？

> **答案**：它们强制 softmax 的 logits 累加和输出的部分和用 FP32 而非 `Tk`（如 FP16）。这是在「速度」和「精度」间切换的开关：用 FP32 累加更准但更慢、占更多寄存器。默认不开，追求速度；对精度敏感的场景（如大词表、长序列）可以打开。

---

## 5. 综合实践

**任务**：选定一组具体配置，完整复述「一次 `masked_multihead_attention` 调用」的全过程，把本讲四个模块串起来。

**配置假设**：模型 `size_per_head=64`、`num_heads=12`、`batch=2`、`beam_width=1`、当前 `timestep=50`、`memory_max_len=1024`、数据类型 FP16、`int8_mode=0`、无 relative bias、无 ALiBi、`masked_tokens` 中第 5 个位置是 padding。

**请完成**：

1. **调度链**（用 4.2）：从 `masked_multihead_attention(params<half>, stream)` 出发，写出最终启动的 kernel 模板实参（`<T, Dh, Dh_MAX, THDS_PER_KEY, THDS_PER_VALUE, THDS_PER_BLOCK, DO_CROSS_ATTENTION, HAS_BEAMS>`），以及 `grid`、`block` 的具体数值。
2. **片上数据流**（用 4.3）：写出 `qk_smem`、`q_smem`、`out`、`red_smem` 各自存了什么、在哪一阶段被写入和消费，并指出 `qk_smem` 被复用了几次。
3. **mask 行为**（用 4.3.1）：解释位置 5（padding）为什么不会影响最终输出——它在 QK^T、softmax 两步分别发生了什么。
4. **省了多少显存**（用 4.3.4）：估算朴素实现下 `[B,H,L,L]` 分数矩阵的字节数，并说明融合 kernel 省掉了它的几次显存往返。
5. **精度路径**（用 4.4）：说明 FP16 下 `Tk` 是什么、`convert_from_float` 在哪里被用到。

**参考要点**（供你对照，不是要你背）：
- 第 1 题：`<uint16_t, 64, 64, 2, 8, 128, false, false>`，`grid=(12,2)`，`block=128`。
- 第 3 题：QK^T 算出的 `qk[5]` 仍在 smem 里，但 `is_mask` 使 `qk_max` 不考虑它；softmax 时 `logit[5] = exp(...) → 0`，于是 PV 中 `out += 0 * V[5]`，等效于完全不 attend 位置 5。
- 第 4 题：`2*12*50*50*4 ≈ 240KB`（L=50 因为 timestep=50），朴素约 4 次往返，融合 0 次。

> 数字结果是否能在你本机验证属于「待本地验证」；重点是理解数据流与调度逻辑，而非记住数字。

## 6. 本讲小结

- 这套 `decoder_masked_multihead_attention` 文件分为三层：`.h`（参数结构 `Multihead_attention_params`）→ 顶层 `.cu`（按 `Dh` 与数据类型 switch 分发）→ 子目录 12 个 `_<N>.cu`（按序列长度选 block 配置 + 显式实例化）→ `template.hpp`（真正的融合 kernel）。
- **「融合」= 把 `QK^T`、`softmax`、`PV` 的所有中间结果（分数矩阵、logits、部分和）留在共享内存和寄存器**，朴素实现里那个 `[B,H,L,L]` 分数矩阵要写显存好几次，融合后零次——这是访存密集型的注意力能大幅加速的根本原因。
- **causal mask 在 decoder 单 query 场景是「结构性免费」的**：循环上界 `ti <= tlength` 天然不看未来；运行期 `masked_tokens` 才是显式处理 padding/finished 的掩码。
- 调度是「运行期枚举 → 编译期模板」的工业级范本：12 档 `Dh` × 4 种数据类型 × 2（masked/cross）被编译成各自机器码，block 配置再随 `tlength` 动态分桶。
- **多精度共用一份模板**：`T` 控制存储精度、`kernel_type_t<T>` 推导计算精度（FP8→float）；INT8（`int8_mode==2`）和 FP8 通过加载时反量化、写回时量化的运行期分支接入，体现「低精度存储 + 高精度计算」的量化推理思想。
- kernel 的并行模型是「一个 block 负责一个 (序列, 头)」，内部用 warp shuffle + 两级共享内存归约求 max/sum，V 的部分和用树形归约合并。

## 7. 下一步学习建议

- **进入 layer 层**：下一讲 **u3-l3（注意力层：Unfused / Fused / TensorParallel）** 会讲 `FusedAttentionLayer`、`UnfusedAttentionLayer` 如何把本讲的 kernel 和 cuBLAS GEMM（u2-l3）组合成完整的注意力模块，以及 `TensorParallel` 版本在哪里插入 all-reduce。本讲的 `DecoderSelfAttentionLayer.cc` 调用点就是衔接点。
- **看 KV cache 如何被组织**：本讲只说 K/V cache 是「按 `[B,H,L,D]` 布局、环形写入」，**u6-l2（KV Cache 机制与拼装）** 会讲 cache 在 GPT/beam search 下如何被 `gpt_kernels` 的 transpose/tile/重排 kernel 管理。
- **跑一个最小验证**：`tests/unittests/test_attention_kernels.cu` 测的是 unfused attention；FP8 路径有专门的 `decoder_masked_multihead_attention_fp8_test.cc`。建议阅读它们，看测试如何构造 `params`、调用 kernel、与参考实现比对（u11-l1 会系统讲测试体系）。
- **精读模板的 INT8/FP8 分支前**，建议先读 u9-l1（INT8 量化）和 u9-l3（FP8）建立 ScaleList、量化粒度的背景，再回头看本讲 4.4 会更顺。
