# autotuner 配置空间与硬件建模

## 1. 本讲目标

本讲是「后端、性能、扩展与工程化」单元的一讲，承接 [u3-l2](u3-l2-full-lower-tl-pipeline.md) 的 `lower_tl` 链路，专门回答一个问题：

> **生成出来的 kernel 用多大的 tile、多少线程、几级流水，性能才好？这些选择能不能自动确定，而不是靠人手拍脑袋？**

注意力 kernel 是访存密集型计算，性能对分块大小（`block_M`/`block_N`）、线程数（`num_threads`）、流水级数（`stages`）极度敏感——同一份代码，配置差一档，吞吐可能差几倍。AttentionEngine 提供了一套「硬件建模 + 配置枚举过滤 + 运行期搜索缓存」的机制来半自动地解决这件事。

学完本讲，你应当能够：

- 说出 `block_M`/`block_N`/`block_K`、`num_threads`、`stages`、`qk_mem_level`/`acco_mem_level` 这些配置项各自的含义。
- 理解 `decider.py` 如何用 `memory_usage` 公式估算一份配置的 shared memory 与寄存器占用，并用硬件上限把不合法配置过滤掉。
- 读懂 `arch_base.py` / `H100.py` 如何把一张 GPU 的关键参数（smem 上限、寄存器上限、mma 原语尺寸）抽象成数据。
- 说清楚 `mha.py` 里 `tune=True`、`tune_file="attn_tl.json"` 是怎么一路传到模板里的 `{{TUNE}}` / `{{TUNE_FILE}}` 占位符，又怎么驱动 TileLang 的 `@autotune` 做实际搜索与 JSON 缓存的。
- 区分三条既相关又独立的调优线索：静态可行性过滤（`decider`）、运行期 profile 搜索（模板内 `@autotune`）、离线 profile 搜索器（`AttnFwdTunner`）。

## 2. 前置知识

### 2.1 什么是 tile（分块）与 mma 原语

注意力计算 \(O = \mathrm{softmax}(QK^\top)V\) 在 GPU 上不会一次性把整个 \(QK^\top\) 矩阵算出来（那样 \(O(T^2)\) 显存爆炸），而是把 Q 序列、KV 序列切成小块（tile），在片上 shared memory 里一块一块地算（即 FlashAttention 的 online 算法，见 [u2-l6](u2-l6-lower-online-func.md)）。

- `block_M`：Q 方向的块高（一次处理多少个 query token）。
- `block_N`：KV 方向的块宽（一次处理多少个 key/value token）。
- `block_K`：缩并维（打分 gemm 的 K 维，即 `dim_qk`）。
- `block_KV`：输出 gemm 的缩并维（即 `dim_v`）。

块越大，数据复用越好、访存越少，但片上缓冲与寄存器占用也越大；超过硬件上限就会编译失败或寄存器溢出（spill）变慢。

而 **mma 原语（MMA primitive）** 是 GPU 上做矩阵乘加的基本指令的形状。例如 Hopper（H100）的 `wgmma` 指令一次算 \(64\times16\times16\) 的矩阵乘，Ampere（A100）的 `mma.m16n8k16` 是 \(16\times8\times16\)。因此 tile 尺寸必须是 mma 原语尺寸的整数倍，否则无法对齐发射。

### 2.2 shared memory 与寄存器（register）上限

GPU 每个 SM（流多处理器）有定量的片上资源：

- **shared memory（共享内存）**：片上高速缓存，块内线程可共享。H100 每 SM 最多约 228 KB，A100 约 164 KB。
- **寄存器**：每线程私有的最快存储，H100/A100 每线程最多 255 个 32-bit 寄存器，整个 thread block 共享一个寄存器文件（A100/H100 为 65536 个）。

autotuner 的核心约束就是：**一份 tile 配置估算出来的 shared memory 与寄存器占用，不能超过这些硬件上限。**

### 2.3 流水（pipeline / stages）

`stages` 表示软件流水的级数：在算当前一块的同时，预取下一块（甚至下几块）的数据，掩盖访存延迟。级数越多越能掩盖延迟，但 shared memory 也要乘以 `stages`（要同时缓存多块），所以 `stages` 和 smem 占用线性相关。

## 3. 本讲源码地图

本讲涉及的关键文件（路径相对于仓库根，但 import 时相对于 `attention_engine/` 这个 PYTHONPATH 根）：

| 文件 | 作用 |
| --- | --- |
| `attention_engine/autotuner/arch/arch_base.py` | 硬件参数抽象基类 `Arch`，列出所有需要建模的字段（默认全 0）。 |
| `attention_engine/autotuner/arch/H100.py`（及 `A100.py`、`RTX4090.py`） | 具体显卡的硬件参数实现。 |
| `attention_engine/autotuner/arch/__init__.py` | 按 `compute_capability` 元组把显卡分派到具体 Arch 类的 `AttnDevice` 字典。 |
| `attention_engine/autotuner/decider.py` | 静态可行性过滤：`memory_usage` 估算 + `decider` 枚举配置并过滤。 |
| `attention_engine/core/lower/lower.py` | `TunnerOutput` / `TunnerOutputBwd` 数据类，把 `tune`/`tune_file` 转成模板占位符。 |
| `attention_engine/core/template/tl_template/attn/attn_tl.py` | 生成代码骨架：`get_configs()`/`get_bwd_configs()` 定义搜索空间，`{{TUNE}}`/`{{TUNE_FILE}}` 开关与 `tune()` 助手做运行期搜索与 JSON 缓存。 |
| `attention_engine/autotuner/attnfwd_tunner.py` | 离线 profile 搜索器 `AttnFwdTunner`，用 `tl.profiler.cached` + bench 函数独立搜索并写 JSON。 |
| `attn_script/mha.py` | 用户侧示例，构造引擎时传 `tune=True, tune_file=...`。 |

> 提示：`attention_engine/` 是 PYTHONPATH 根而非 Python 包，所以代码里写的是 `from autotuner.decider import decider`、`from core.utils import meta_tensor`、`from benchmark.bench_utils import ...`。

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：硬件参数建模（4.1）、`memory_usage` 估算（4.2）、config 枚举与过滤（4.3）、运行期 `tune` 接入（4.4）、离线 profile 搜索器（4.5）。

### 4.1 硬件参数建模：Arch 体系

#### 4.1.1 概念说明

要判断一份 tile 配置「合不合法」，必须先有一份「这张卡到底有多少资源」的描述。AttentionEngine 用一个简单的 `Arch` 类来承载这件事：它把 GPU 的关键参数（寄存器上限、shared memory 上限、mma 原语尺寸、每 mma 的线程数、线程数上限等）抽成数据字段，不依赖任何运行期探测——是一份**静态的硬件规格表**。

#### 4.1.2 核心流程

1. `Arch` 基类声明所有字段，默认全为 0（「未配置」哨兵）。
2. 每张具体显卡（`H100`/`A100`/`RTX4090`）继承 `Arch`，在 `__init__` 里填上自己的真实数值。
3. `arch/__init__.py` 用一个 `AttnDevice` 字典，以 `(major, minor)` 形式的 compute capability 为键，把显卡分派到对应类。
4. 运行期（在生成的模板里）读 `torch.cuda.get_device_capability()` 得到元组，查字典得到当前 Arch 实例；查不到就回退到 `H100()`。

#### 4.1.3 源码精读

基类 `Arch` 把所有需要的硬件字段先列出来，默认 0：

[arch_base.py:L1-L13](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/autotuner/arch/arch_base.py#L1-L13) —— 声明 `reg_cap`/`smem_cap`/`compute_max_core`/`warp_size`/`sm_partition`/`transaction_size`/`bandwidth`/`register_per_thread` 等字段，全部默认 0。

H100 的具体数值（本讲贯穿示例）：

[H100.py:L4-L21](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/autotuner/arch/H100.py#L4-L21) —— 关键参数：`reg_cap=65536`（寄存器文件总量）、`register_per_thread=255`（每线程上限）、`smem_cap=232448`（约 227 KB）、`mma_primitive=[64,16,16]`（wgmma 的 M/N/K）、`threads_per_mma=128`（一个 warp group 128 线程驱动 wgmma）、`threads_cap=1024`（每 block 线程上限）。

对比 A100，差异主要在 mma 原语与 smem 上限：

[A100.py:L4-L20](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/autotuner/arch/A100.py#L4-L20) —— `mma_primitive=[16,8,16]`（mma.m16n8k16）、`threads_per_mma=32`（一个 warp）、`smem_cap=163*1024`。

按 compute capability 分派的字典：

[\_\_init\_\_.py:L6-L10](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/autotuner/arch/__init__.py#L6-L10) —— `(8,0)→A100`、`(8,9)→RTX4090`、`(9,0)→H100`。

#### 4.1.4 代码实践

实践目标：确认 Arch 是纯静态规格表，且能按 compute capability 分派。

操作步骤：

1. 读 `arch_base.py`，确认 `Arch` 没有任何方法、只有字段——它就是一张数据表。
2. 读 `H100.py` / `A100.py`，对比 `mma_primitive` 与 `threads_per_mma`：H100 是 `[64,16,16]`/128，A100 是 `[16,8,16]`/32。

需要观察的现象：H100 的 `threads_per_mma=128`（warp group）远大于 A100 的 `32`（单 warp），这直接决定了 4.3 里「warp row 对齐」约束的分母。

预期结果：能口述「Hopper 用 warp group 发射 wgmma，所以 tile 行高与线程数的对齐粒度更大」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Arch` 把字段默认设成 0 而不是用 `None`？

参考答案：因为这些字段后面都要参与算术比较（`shared_mem <= hardware_meta.smem_cap`）。设成 0 当作「未配置」哨兵，能让一份未填全的 Arch 在比较时自然把所有配置判为「不合法」从而被过滤，是一种保守的安全默认；如果用 `None`，比较时会抛 `TypeError`。

**练习 2**：在 `AttnDevice` 字典里，compute capability 为 `(8, 6)`（如部分 A40/A10）会分派到哪个类？

参考答案：查不到 `(8,6)` 这个键，会触发 `KeyError`，在模板里被 `except KeyError` 捕获后回退到 `H100()`（见 [attn_tl.py:L18-L21](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L18-L21)）。也就是说，未登记的卡默认按 H100 的规格建模——这是一个值得注意的近似。

### 4.2 memory_usage 估算

#### 4.2.1 概念说明

给定一份 tile 配置，在真正编译运行之前，能不能**先估算它会占多少 shared memory、多少寄存器**？`memory_usage` 函数就是干这件事的。它把 Q/K/scores/V/输出这些 tile 的字节数加起来得到 smem 占用，把 scores 与输出累加器在寄存器里的占用折算成「每线程寄存器数」，供 4.3 的过滤使用。

关键设计：它**不依赖运行**，纯按 tile 尺寸和 dtype 字节数算——是个解析式估算器。

#### 4.2.2 核心流程

shared memory（字节）由五项 tile 累加：

\[ S_{\text{mem}} = \underbrace{M K b_d}_{Q} + \underbrace{N K \cdot \text{stages} \cdot b_d}_{K(\text{流水})} + \underbrace{M N \cdot l_{\text{qk}}^{(s)} \cdot 4}_{\text{scores}} + \underbrace{N V \cdot \text{stages} \cdot b_d}_{V(\text{流水})} + \underbrace{M V \cdot l_{\text{acco}}^{(s)} \cdot 4}_{\text{acc\_o}} \]

其中 \(b_d\) 是 `dtype_size`（如 bf16 = 2 字节），常数 4 是累加 dtype（fp32 = 4 字节，`dtype_accum_size`），\(l_{\text{qk}}^{(s)}\)、\(l_{\text{acco}}^{(s)}\) 是这两组 tile 在 shared 层的「是否驻留」开关（取 `qk_mem_level[1]`、`acco_mem_level[1]`）。

寄存器占用（折算到每线程，单位是 4 字节寄存器）：

\[ r = \frac{M N \cdot l_{\text{qk}}^{(r)} \cdot 4 + M V \cdot l_{\text{acco}}^{(r)} \cdot 4}{\text{num\_threads} \cdot 4} \]

其中 \(l_{\text{qk}}^{(r)}\)、\(l_{\text{acco}}^{(r)}\) 是这两组 tile 在 register 层的开关（取 `qk_mem_level[0]`、`acco_mem_level[0]`）。

`qk_mem_level` / `acco_mem_level` 取自一个小集合 `[[1,0,0],[1,1,0]]`，分别代表「只在寄存器」（第 0 位为 1）与「寄存器 + shared 都有」（第 0、1 位都为 1）。

#### 4.2.3 源码精读

`memory_usage` 的实现：

[decider.py:L11-L37](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/autotuner/decider.py#L11-L37) —— 注意三个细节：① K 与 V tile 都乘了 `stages`（流水同时缓存多块）；② scores 与 acc_o 用 `dtype_accum_size=4`（fp32 累加），其它用输入 dtype 的 `dtype_size`；③ `reg_num` 最后除以 `num_threads * bytes_per_register(=4)`，得到「每线程寄存器数」。

`qk_mem_level` / `acco_mem_level` 的取值集合：

[decider.py:L85-L86](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/autotuner/decider.py#L85-L86) —— 两个候选 `[[1,0,0],[1,1,0]]`，模拟「scores/acc_o 放在寄存器」与「同时放 shared + 寄存器」两种布局。

#### 4.2.4 代码实践

实践目标：手算一份配置的 `memory_usage`，建立对占用规模的直觉。

操作步骤：

1. 取 H100、bf16（`dtype_size=2`）、`block_M=128`、`block_N=128`、`block_K=dim_qk`、`dim_v=128`、`num_threads=256`、`stages=2`、`qk_mem_level=[1,1,0]`、`acco_mem_level=[1,1,0]`。
2. 代入公式算 \(S_{\text{mem}}\)：\(Q=128\cdot128\cdot2\)、\(K=128\cdot128\cdot2\cdot2\)、\(\text{scores}=128\cdot128\cdot1\cdot4\)、\(V=128\cdot128\cdot2\cdot2\)、\(\text{acc\_o}=128\cdot128\cdot1\cdot4\)。
3. 把五项相加，与 H100 的 `smem_cap=232448` 比较。

需要观察的现象：仅这一组配置的 smem 估算就接近十几万字节，说明大 tile + 多级流水很容易撞上限。

预期结果：手算得到的 \(S_{\text{mem}}\) 远小于 232448 时合法；若把 `stages` 调到 4 或 `block_M`/`block_N` 翻倍，会迅速突破上限被过滤。具体数值「待本地验证」——建议直接调用 `memory_usage(...)` 打印核对。

#### 4.2.5 小练习与答案

**练习 1**：为什么 K tile 和 V tile 都乘了 `stages`，而 Q tile 没有？

参考答案：Q tile 在一个 KV 内循环里是常驻复用的（同一块 Q 对所有 KV 块），只需存一份；K/V tile 是循环里逐块加载的，多级流水要同时缓存当前块与后续若干块的 K/V，所以 shared 占用要乘以 `stages`。

**练习 2**：`dtype_accum_size=4` 这个常数对应什么？

参考答案：scores 与输出累加器用 fp32（4 字节）累加以保数值精度，即使输入是 bf16/fp16（2 字节）。所以这两块的占用按 4 算，比输入 tile 更「贵」。

### 4.3 config 空间枚举与可行性过滤：decider

#### 4.3.1 概念说明

有了 `memory_usage` 与 Arch，`decider` 把所有可能的 tile 配置**枚举**出来，再逐份过滤掉不合法的，留下一个「硬件可行」的候选集合。它是一个**静态可行性过滤器（static feasibility pruner）**——只看硬件容量，不实测性能。

> ⚠️ 重要现状说明：在当前 HEAD，`decider` **还没有被引擎调用**。`attn_engine.py` 里那一行 `need_engine_fuse, fuse_config = decider(qkv_meta, device)` 是注释掉的。所以 `decider` 目前是一个「已建模、待接线」的预览能力。真正在跑的自动调优走的是 4.4 介绍的运行期 `@autotune` 路径。本讲仍把它作为理解「配置空间 + 硬件约束」的核心教具来精读。

#### 4.3.2 核心流程

`decider(qkv_meta, hardware_meta)` 的步骤：

1. 从 `qkv_meta` 抽出 batch/seqlen/head/dim_qk/dim_v/dtype。
2. 用 mma 原语尺寸作为步长，枚举每个维度的候选块尺寸（`block_M`/`block_N`/`block_K`/`num_threads`），上限是「序列长度向上取整到步长的倍数」。
3. 组合枚举 `itertools.product(block_M, block_N, block_K, qk_mem_level, acco_mem_level, num_threads, stages)`。
4. 对每份组合调 `memory_usage` 估算，套三道硬件上限过滤 + 两道对齐/语义过滤。
5. 返回 `(need_fuse, configs)`，`need_fuse` 表示「是否存在至少一份合法配置」。

过滤条件（全过才保留）：

- \(S_{\text{mem}} \le \text{smem\_cap}\)
- \(r \cdot \text{num\_threads} \le \text{reg\_cap}\)（整个 block 的寄存器总量）
- \(r \le \text{register\_per\_thread}\)（每线程寄存器上限）
- `block_M` 必须被「每 mma 的行数 × 一个 mma 占的线程数折算」整除（warp row 对齐）
- `block_K == dim_qk`（当前只支持缩并维不分块）

#### 4.3.3 源码精读

维度枚举，步长就是 mma 原语尺寸，上限用 `next_multiple_of`（向上取整到步长倍数）：

[decider.py:L49-L83](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/autotuner/decider.py#L49-L83) —— `block_M` 步长 `mma_primitive[0]`、`block_N` 步长 `mma_primitive[1]`、`block_K` 步长 `mma_primitive[2]` 且要求 `dim_qk % bk == 0`、`num_threads` 步长 `threads_per_mma`。

辅助函数 `next_multiple_of` 的语义：

[decider.py:L7-L8](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/autotuner/decider.py#L7-L8) —— 返回不小于 `x` 的下一个 `y` 的倍数（`x` 已是倍数时返回 `x` 本身）。它把块尺寸上限钳制到序列长度，避免枚举出比序列还大的无意义块。

组合枚举与过滤主体：

[decider.py:L91-L124](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/autotuner/decider.py#L91-L124) —— 注意控制流：外层 `if` 是三道硬件上限（不过直接跳过整份），内层两个 `if ...: continue` 分别是 warp row 对齐与 `block_K==dim_qk`，命中就跳过。所以保留条件是「上限全过 AND 对齐成立 AND K 等于全维」。

`__main__` 给了一组形状与 H100 的样例：

[decider.py:L131-L143](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/autotuner/decider.py#L131-L143) —— 形状 `[1,2048,12,96]`/`[1,2048,12,96]`/`[1,2048,12,192]`、bf16、`H100()`，打印所有合法配置。

#### 4.3.4 代码实践

实践目标：跑 `decider.py` 的 `__main__`，数出合法配置数量，并定位是哪些约束把大多数配置过滤掉了。

操作步骤：

1. 在 `attention_engine/` 目录下（保证 `from autotuner.arch import H100` 与 `from core.utils import meta_tensor` 能解析），运行：
   ```bash
   cd attention_engine && python -m autotuner.decider
   ```
   （或 `PYTHONPATH=attention_engine python attention_engine/autotuner/decider.py`）
2. 数打印出的配置条数（即 `len(fuse_configs)`）。
3. 分析被过滤的主因：在 `__main__` 里临时加几行计数，分别统计被三道硬件上限、warp 对齐、`block_K!=dim_qk` 各自刷掉了多少份。

需要观察的现象：枚举空间（`itertools.product` 笛卡尔积）通常达百万量级，但合法配置只剩很少几份；其中 `block_K==dim_qk` 这一条（`dim_qk=96`，只保留 `bk=96`）会刷掉绝大多数 `block_K` 候选，是过滤最狠的一道。

预期结果：能指出「smem_cap / reg_cap / register_per_thread / warp 对齐 / block_K==dim_qk」各自的贡献占比。**具体合法配置数量与各项占比待本地验证**（依赖 torch 与具体硬件 Arch）。

> 调试提示：若运行报 `ModuleNotFoundError: autotuner`，说明没有把 `attention_engine/` 设进 `PYTHONPATH`（见 [u1-l2](u1-l2-setup-and-first-run.md)）。

#### 4.3.5 小练习与答案

**练习 1**：给定 H100（`mma_primitive=[64,16,16]`、`threads_per_mma=128`），warp row 对齐约束 `bm % ((nt/threads_per_mma) * mma_primitive[0])` 在 `num_threads=128` 时分母是多少？

参考答案：\((128/128)\times 64 = 64\)。所以 `block_M` 必须是 64 的倍数才能保留——这与「wgmma 的 M=64」一致，保证块行高能对齐到一次 wgmma 的行。

**练习 2**：为什么 `decider` 最后用 `need_fuse = len(configs) > 0` 作为返回？

参考答案：`need_fuse` 告诉上层「是否存在硬件可行的配置」。若没有任何配置合法，说明该形状在当前 tile 化策略下无法直接生成 kernel（可能需要更小的 mma 原语或拆分策略），上层可据此回退或报错。它是一个「能不能做」的布尔信号，而 `configs` 是「具体怎么做」的候选清单。

### 4.4 运行期 tune 接入：TUNE/TUNE_FILE 开关与模板渲染

#### 4.4.1 概念说明

真正在跑的自动调优不在 `decider`，而在**生成的 TileLang 代码内部**。机制是：用户在构造 `AttentionEngine` 时传 `tune=True` 与 `tune_file="xxx.json"`，这两个值经 `lower_tl` 落进 `TunnerOutput` 数据类的 `TUNE`/`TUNE_FILE` 字段，渲染成模板顶部的 `TUNE = {{TUNE}}` / `TUNE_FILE = "{{TUNE_FILE}}"` 两个开关。开关打开时，模板里的 `tune()` 助手会调 TileLang 自带的 `@autotune` 装饰器去逐份编译+计时、挑出最快的配置，并按「问题形状」为键把结果缓存进 JSON 文件；下次同形状直接读缓存，不再重搜。

#### 4.4.2 核心流程

数据流：

1. 用户侧：`mha.py` 里 `AttentionEngine(..., tune=True, tune_file="attn_tl.json", tune_bwd=True, tune_file_bwd="attn_tl_bwd.json")`。
2. 引擎把参数透传给 `lower_tl` 的 `tune` / `tune_file`（前向）与 `tune_bwd` / `tune_file_bwd`（反向）。
3. `lower_tl` 构造 `TunnerOutput(TUNE=str(tune), TUNE_FILE=str(tune_file))`，`__dict__` 一并并入渲染 kwargs。
4. 模板 `attn_tl.py` 顶部出现 `TUNE = {{TUNE}}` / `TUNE_FILE = "{{TUNE_FILE}}"`（以及 bwd 的两个）。
5. 运行期：`if TUNE:` 走搜索分支（调 `@autotune` 装饰过的 kernel + `tune()` 助手 + JSON 缓存），否则走默认配置分支（用 `{{block_M}}`/`{{block_N}}`/`{{stages}}` 等固定值）。

搜索空间的定义在模板的 `get_configs()` / `get_bwd_configs()` 里，**当前是写死的列表**，并不消费 `decider` 的产物。其中 `get_bwd_configs()` 会根据 `attn_device`（4.1 的硬件模型）微调反向的 `block_N`——这是 Arch 模型目前唯一真正接入运行期搜索空间的地方。

#### 4.4.3 源码精读

用户侧开关（本讲贯穿示例）：

[mha.py:L109-L117](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L109-L117) —— `tune=True, tune_file="attn_tl.json"`、`tune_bwd=True, tune_file_bwd="attn_tl_bwd.json"`。

引擎透传给 `lower_tl`：

[attn_engine.py:L111-L135](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L111-L135) —— 注意 L117 那行 `# need_engine_fuse, fuse_config = decider(qkv_meta, device)` 是注释掉的，印证 `decider` 尚未接线；而 `tune`/`tune_file` 被如实传给 `_compile_tl`。

`TunnerOutput` 数据类（模板占位符的数据源）：

[lower.py:L155-L163](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L155-L163) —— `TUNE`/`TUNE_FILE` 默认 `"False"`/`""`，另带 `block_M`/`block_N`/`stages`/`thread_num`/`shared_fuse` 五个默认配置（`TUNE=False` 时用）。

`lower_tl` 把开关与默认配置填进 `TunnerOutput`：

[lower.py:L641-L653](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L641-L653) —— `tuned_config is None` 时用构造参数建 `TunnerOutput`；`dimv>256` 时有个手工特例把 `block_M/block_N` 降到 64、`stages=1`、`shared_fuse=True`（注释里写着 `TODO: remove this special check into autotuner`，说明它本该由 autotuner 自动决定）。

模板顶部的四个开关：

[attn_tl.py:L460-L463](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L460-L463) —— `TUNE`/`TUNE_FILE`/`TUNE_BWD`/`TUNE_FILE_BWD` 四个全局量直接由 Jinja 占位符渲染。

前向搜索空间定义（写死列表）：

[attn_tl.py:L33-L48](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L33-L48) —— `get_configs()` 用 `itertools.product` 枚举 `block_M×block_N×num_stages×thread_num×shared_fuse`，其中 `shared_fuse` 取自模板渲染的 `{{shared_fuse}}`（单元素列表）。

反向搜索空间定义（这里 Arch 模型真正介入）：

[attn_tl.py:L225-L236](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L225-L236) —— `get_bwd_configs()`：`block_N = [64, 128] if isinstance(attn_device, H100) else [32, 64, 128]`。这是当前 Arch 硬件模型唯一真正喂给运行期搜索空间的接点。

`@autotune` 装饰器包住 kernel：

[attn_tl.py:L176-L186](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L176-L186) —— `tune=True` 时 kernel 被 `@autotune(configs=get_configs(), warmup=10, rep=10)` 包裹，TileLang 会逐份配置编译+预热+计时。

`tune()` 助手做 JSON 缓存与搜索：

[attn_tl.py:L474-L528](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L474-L528) —— 先按 `problem_keys`（B/H/N_CTX/D_HEAD/D_HEADV/causal）查 `tune_file`，命中则直接复用 `tuned_config`；未命中才调 `kernel_profiler(...)` 实测、把最优配置写回 JSON。`TUNE=False` 时则直接用 `{{block_M}}`/`{{block_N}}`/`{{stages}}` 默认值。

#### 4.4.4 代码实践

实践目标：观察 `tune=True/False` 对生成代码与运行行为的差异。

操作步骤：

1. 跑 `attn_script/mha.py`（先确保环境按 [u1-l2](u1-l2-setup-and-first-run.md) 配好）。第一次跑 `tune=True` 时会在脚本工作目录生成 `attn_tl.json` 与 `attn_tl_bwd.json`。
2. 打开 `attn_tl.json`，确认里面每条记录形如 `{B, H, N_CTX, D_HEAD, D_HEADV, causal, tuned_config: {...}}`。
3. 第二次跑同样的形状：观察不再触发搜索（直接读缓存，启动更快）。
4. 把构造参数改成 `tune=False`（或注释掉），重新跑：此时生成代码里 `TUNE = False`，走 `{{block_M}}=128` 等默认配置，不再生成/读取 JSON。

需要观察的现象：`tune=True` 首跑慢（要遍历搜索空间编译计时多份），但选出的配置通常比默认更优；`tune=False` 首跑快但用固定配置。

预期结果：`attn_tl.json` 内 `tuned_config` 字段的 `block_M`/`block_N`/`num_stages`/`thread_num` 是实测最优值。**具体数值待本地验证**（依赖硬件与实测延迟）。

#### 4.4.5 小练习与答案

**练习 1**：`TUNE` 与 `TUNE_FILE` 这两个开关，分别控制什么？

参考答案：`TUNE`（布尔）决定「要不要搜索」——为真就走 `@autotune` 实测搜索，为假就用 `{{block_M}}` 等默认值；`TUNE_FILE`（路径字符串）决定「搜索结果存哪/从哪读」——以问题形状为键做 JSON 缓存，避免同形状重复搜索。

**练习 2**：为什么 `get_bwd_configs()` 里要 `isinstance(attn_device, H100)` 分支，而 `get_configs()`（前向）没有？

参考答案：反向的 `block_N` 候选受 Hopper wgmma 的 N 粒度影响更敏感，所以按是否 H100 收窄到 `[64,128]`；前向搜索空间目前是写死的统一列表，没有按硬件细分。这反映 Arch 模型对运行期搜索空间的接入还很不完整——前向空间与 `decider` 都还没接上，是明确的改进方向。

### 4.5 离线 profile 搜索：AttnFwdTunner

#### 4.5.1 概念说明

除了「模板内嵌的 `@autotune`」这条运行期路径，仓库还提供一个**独立的离线搜索器** `AttnFwdTunner`（以及同构的 `SigmoidTunner`）。它不走模板开关，而是自己枚举配置、用 `tl.profiler.cached` 编译缓存每份配置、用 bench 函数逐份计时，挑出最快的那份写进 JSON。它更像一个「手动跑一遍、把最优配置固化下来」的工具脚本，**不被引擎自动调用**。

#### 4.5.2 核心流程

1. `generate_config()`：用 `itertools.product` 枚举 `block_M×block_N×num_threads×stages`，并过滤掉 `block_M % (num_warps*16) != 0` 的（warp 对齐，与 `decider` 的对齐思想一致，但更朴素）。
2. `tune()`：
   - 先查 `file_path` 的 JSON 缓存，命中（同 problem_keys）就直接返回。
   - 否则用 `ThreadPoolExecutor` **并行**调 `cache_module` 编译缓存每份配置。
   - 再**串行**逐份跑 bench 函数（GPU 计时必须串行，避免互相干扰）。
   - 取 `latency` 最小的配置，连同 problem_keys 写回 JSON。

#### 4.5.3 源码精读

配置枚举与 warp 对齐过滤：

[attnfwd_tunner.py:L41-L59](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/autotuner/attnfwd_tunner.py#L41-L59) —— `num_warps = thread_num // 32`，要求 `block_M % (num_warps * 16) == 0`。

并行编译 + 串行计时：

[attnfwd_tunner.py:L91-L140](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/autotuner/attnfwd_tunner.py#L91-L140) —— 编译（`cache_module` → `tl.profiler.cached`）用线程池并行，计时（`bench_func`）必须串行。`bench_func` 默认是 `bench_sigmoidattn_fwd`，返回带 `latency`/`tflops`/`tflops_ref` 的字典（见 [u5-l4](u5-l4-benchmark-and-correctness.md)）。

结果写回 JSON：

[attnfwd_tunner.py:L160-L172](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/autotuner/attnfwd_tunner.py#L160-L172) —— 把 `problem_keys` + `tuned_config` + bench 结果追加进 JSON 文件。

#### 4.5.4 代码实践

实践目标：读懂 `AttnFwdTunner` 与模板内 `@autotune` 的分工差异。

操作步骤（源码阅读型，无需 GPU）：

1. 对照 4.4 的模板 `tune()` 助手与本节的 `AttnFwdTunner.tune()`，列出两者的相同点（都按 problem_keys 做 JSON 缓存、都挑最低延迟）与不同点（前者靠 TileLang `@autotune` 在编译期搜索，后者自己用 `tl.profiler.cached`+bench 手动搜索）。
2. 注意 `AttnFwdTunner` 的 bench 是串行的（`for mod, tuned_config in cached_results:`），而 `cache_module` 是并行的——解释为何编译可并行而计时不可。

需要观察的现象：两条路径产出的 JSON 结构几乎一致（都是 problem_keys + tuned_config），但来源不同——这正是它们能互换复用的原因。

预期结果：能口述「模板内 `@autotune` 是声明式、与代码生成耦合；`AttnFwdTunner` 是命令式、可独立对任意已编译 kernel 跑搜索」。

#### 4.5.5 小练习与答案

**练习 1**：`AttnFwdTunner` 为什么把「编译」放线程池并行、「计时」放串行？

参考答案：编译（`tl.profiler.cached`）是 CPU 侧的重活，多份之间无依赖，并行能加速；而 GPU 计时（`bench_func` 启动 kernel 并量延迟）必须独占 GPU、串行执行，否则多份 kernel 抢占 SM 会互相污染计时结果，测出来的延迟不可信。

**练习 2**：`AttnFwdTunner` 与模板里的 `tune()` 助手，缓存键（problem_keys）分别包含哪些字段？

参考答案：两者都用 `{B, H, N_CTX, D_HEAD, D_HEADV, causal}` 这一组问题形状作为键（见 [attnfwd_tunner.py:L64-L66](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/autotuner/attnfwd_tunner.py#L64-L66) 与 [attn_tl.py:L465-L472](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_tl.py#L465-L472)）。键相同就能跨工具复用缓存——只要 problem_keys 一致，无论谁搜出来的最优配置都能被另一方直接读取。

## 5. 综合实践

把五个模块串起来，做一次「配置空间 → 硬件过滤 → 运行期搜索」的完整追踪：

1. **建模**：选定 H100（或你手头的卡），从 `arch/__init__.py` 确认 `AttnDevice` 会分派到哪个 Arch 类；若未登记，确认会回退 `H100()`。
2. **静态过滤**：用 `mha.py` 的形状（`B=1, H=128, S=2048, D=DV=128`）构造一组 `qkv_meta`，调用 `decider(qkv_meta, H100())`，打印合法配置数量，并按「smem_cap / reg_cap / register_per_thread / warp 对齐 / block_K==dim_qk」分类统计被刷掉的份数（在过滤循环里加计数器）。
3. **对照默认值**：把 `decider` 留下的候选 `block_M`/`block_N`，与 `lower.py` 里 `TunnerOutput` 的默认 `block_M=128/block_N=128/stages=2`（以及 `dimv>256` 时的 64/64/1 特例）对比，看默认值是否落在合法集合里。
4. **运行期验证**：跑 `mha.py`（`tune=True`），打开生成的 `attn_tl.json`，看 `@autotune` 实测选出的 `block_M`/`block_N` 是否也在 `decider` 的合法集合内；若 `decider` 把它过滤掉了，分析是哪个约束过于保守（这正是「`decider` 待接线、待校准」的实证）。
5. **写一句话结论**：说明在你这台机器上，静态建模（`decider`）、运行期搜索（`@autotune`）、默认配置三者给出的 tile 是否一致，若不一致差异在哪。

> 若无 GPU 环境，步骤 4 可降级为「源码阅读型」：只做步骤 1–3 的静态分析 + 步骤 4 的代码阅读，标注实测数值为「待本地验证」。

## 6. 本讲小结

- 注意力 kernel 性能对 `block_M`/`block_N`/`num_threads`/`stages` 极度敏感，autotuner 的任务就是自动选出好的配置。
- `Arch`（`arch_base.py` + `H100`/`A100`/`RTX4090`）是一份静态硬件规格表：`smem_cap`/`reg_cap`/`register_per_thread`/`mma_primitive`/`threads_per_mma`，按 compute capability 分派。
- `memory_usage` 用解析式估算一份配置的 shared memory 与每线程寄存器占用，无需运行；K/V tile 乘 `stages`，scores/acc_o 用 fp32（4 字节）累加。
- `decider` 笛卡尔枚举配置空间，用三道硬件上限 + warp 对齐 + `block_K==dim_qk` 过滤，但目前**尚未接线**（`attn_engine.py` 里那行调用是注释掉的）。
- 真正在跑的自动调优在生成代码内部：`tune`/`tune_file` → `TunnerOutput` → 模板的 `{{TUNE}}`/`{{TUNE_FILE}}` → TileLang `@autotune` + `tune()` 助手 + JSON 缓存；Arch 模型目前只在反向 `get_bwd_configs()` 接入。
- `AttnFwdTunner` 是独立的离线搜索器（编译并行、计时串行），与模板内 `@autotune` 共用 `{B,H,N_CTX,D_HEAD,D_HEADV,causal}` 缓存键，可互换复用 JSON。

## 7. 下一步学习建议

- 想理解「搜出来的配置跑得多准、怎么校验误差」，继续读 [u5-l4 基准测试与正确性验证](u5-l4-benchmark-and-correctness.md)，那里讲 `do_bench` 的 GPU 计时、L2 flush 与 `print_debug` 误差校验，是本讲 bench 函数的底层。
- 想完整理解 `tune_file` JSON 之外的「整段生成代码长什么样、怎么导出调试」，读 [u5-l6 测试与调试技巧](u5-l6-testing-and-debugging.md)，练习把 `@autotune` 包裹的 kernel 完整导出人工阅读。
- 想从架构层面评估「autotuner 该如何与 `decider` 真正接线、新增算子/新场景的调优工作量」，读 [u5-l7 架构取舍、限制与二次开发方向](u5-l7-architecture-tradeoffs.md)，本讲指出的「`decider` 待接线、前向空间与 Arch 未接」正是其中的待办项。
