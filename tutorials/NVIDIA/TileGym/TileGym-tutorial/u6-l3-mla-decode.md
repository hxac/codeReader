# 多潜注意力 MLA 与解码

## 1. 本讲目标

本讲是「注意力内核族」的第三讲，承接 u6-l2 的解码注意力与 Split-KV，转向 DeepSeek 系列模型特有的 **多潜注意力（Multi-head Latent Attention, MLA）**。学完本讲，读者应该能够：

- 理解 MLA 与普通多头注意力（MHA）的本质区别：**联合位置编码**（内容相似度 `q·k` 与位置相似度 `qpe·kpe` 相加）。
- 读懂 cuTile 版 MLA prefill 内核 `tile_mla`：如何把两次 `mma` 累加进同一个分数瓦片，以及它复用 u6-l1 的在线 softmax + causal 分块框架。
- 读懂 MLA prefill 的 **自动调优**：按 GPU 架构（pre-SM90 / SM90+）产出不同候选瓦片，并理解本轮为 pre-SM90 新增的 `64×64/occupancy=3` 候选为何能改善小规模 MLA 的并行度。
- 读懂 cuTile 版 MLA 单步解码 `mla_decoding`：为何在解码阶段 key 与 value 共用同一个 `kv` 张量（「吸收态」），以及它如何按「头」并行。
- 读懂 `mla_decoding_split_kv`：它如何把 u6-l2 学过的 Split-KV + LSE 合并机制直接套用到 MLA 解码上，并用 `kv_len_per_split` 控制并行度。
- 能判断在长上下文解码时该选哪个 MLA 解码入口。

> **本轮更新（update）**：相比上一版，代码唯一的变化在 [mla.py:30](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla.py#L30)——`_mla_sm80_autotune_configs()` 新增了一条 `TILE_M=64, TILE_N=64, occupancy=3` 候选。本讲因此新增/强化「pre-SM90 autotune 候选」的讲解（见 4.2），并刷新全部永久链接到当前 HEAD。两个解码内核未改动。

## 2. 前置知识

### 2.1 为什么需要 MLA

普通 MHA 里，每个 token、每个头都要存独立的 K 和 V 向量，KV 缓存随序列长度和头数线性膨胀，是长上下文推理的主要显存瓶颈。DeepSeek-V2 提出的 MLA 用一个**低秩潜向量**压缩 K/V：先把 K、V 投影到一个共享的潜空间，缓存时只存这个潜向量，推理时再展开。这样在「多头」之间共享潜向量，把 KV 缓存压到很低。

但有一个麻烦：常用的旋转位置编码（RoPE）会改变向量的方向，**无法与低秩吸收线性复合**。MLA 的折中方案是「解耦位置编码」（decoupled RoPE）：

- 把每个 query/key 拆成**内容部分**（不带位置，称 nope/content，维度 `D`）和**位置部分**（带 RoPE，维度 `D_PE`，本讲记作 `qpe/kpe`）。
- 注意力分数由两部分**相加**而成：内容相似度 + 位置相似度。

这就是本讲三个内核最核心的算式，所有差异都围绕它展开。

### 2.2 联合位置编码的数学形式

对 query 位置 `i` 和 key 位置 `j`，MLA 的原始（未归一化）注意力分数是：

\[
s_{ij} = \mathrm{scale}\cdot\bigl(\underbrace{q_i\cdot k_j}_{\text{内容，维度 }D}+\underbrace{\mathrm{qpe}_i\cdot\mathrm{kpe}_j}_{\text{位置，维度 }D\_PE}\bigr)
\]

其中缩放系数取两段维度之和：

\[
\mathrm{scale}=\frac{1}{\sqrt{D+D\_PE}}
\]

关键点：**两段点积维度不同（`D` 与 `D_PE`），但相加后共享同一个 softmax**。实现上就是两次矩阵乘（`mma`）累加进同一个分数瓦片 `qk`，再做和普通注意力完全一样的在线 softmax。

> 与 u6-l1 的 FMHA 对照：FMHA 的分数只有一段 `q·k`；MLA 多了 `qpe·kpe` 这一段，其余的 causal 掩码、在线 softmax、exp2、`mma(p, v)` 完全相同。所以可以说「MLA = 多一次 mma 的 FMHA」。

### 2.3 承接 u6-l2：解码与 Split-KV、LSE

u6-l2 已建立两块知识，本讲直接复用、不再从头讲：

- **解码阶段的并行度瓶颈**：单步解码时 query 只有 1 行，天然并行任务数远少于 SM 数，硬件空转。
- **Split-KV + LSE 合并**：沿 KV 归约维把长序列切成多段并行，每段产出「部分输出 + 段内 LSE」，再用 log-sum-exp 无损合并。合并公式（两层 Flash attention）：

\[
o=\frac{\sum_s 2^{\ell_s-M}\,\tilde{o}_s}{\sum_s 2^{\ell_s-M}},\qquad M=\max_s \ell_s
\]

本讲的 `mla_decoding_split_kv` 就是把这套机制原样套到 MLA 解码上。读者应带着这两块知识进入本讲。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/tilegym/ops/cutile/mla.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla.py) | MLA **prefill** 内核 `_prefill_mla_kernel`、按架构（sm80/sm90+）的 autotune 候选、自动调优启动器 `_cutile_autotune_mla` 与 `tile_mla` 注册实现。本讲 4.1、4.2 的主样本（4.2 含本轮新增的 pre-SM90 候选）。 |
| [src/tilegym/ops/cutile/mla_decoding.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla_decoding.py) | MLA **单步解码**内核（单 split，一个 CTA 处理整条 KV），返回 `(o, l)`。4.3 的主样本。 |
| [src/tilegym/ops/cutile/mla_decoding_split_kv.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla_decoding_split_kv.py) | MLA **Split-KV 解码**内核：3D 网格沿 KV 切分，产出中间张量后调 `splitk_reduce` 合并。4.4 的主样本。 |
| [src/tilegym/ops/cutile/splitk_reduce.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/splitk_reduce.py) | Split-KV 的合并内核，u6-l2 已讲过，本讲作为 4.4 的下游被调用。 |
| [src/tilegym/ops/ops.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/ops.py) | 三个 MLA 算子的统一接口 stub（`mla`/`mla_decoding`/`mla_decoding_split_kv`），仅声明签名。 |

三个内核的关系一句话总结：`mla` 是 prefill（多 query 行），`mla_decoding` 是解码基准版（整条 KV 由单个 CTA 串行扫），`mla_decoding_split_kv` 是解码长上下文版（把 KV 切片并行再合并）。

## 4. 核心概念与源码讲解

### 4.1 MLA 联合位置编码（qpe/kpe）

#### 4.1.1 概念说明

普通注意力的分数只有一段点积 `q·k`。MLA 把 query/key 各拆成「内容」与「位置」两段，分数变成**两段点积之和**（见 2.2 节公式）。

- `q`、`k`：内容/不带位置的潜向量，维度 `D`（代码里叫 `TILE_D`，如 512）。
- `qpe`、`kpe`：带旋转位置编码的部分，维度 `D_PE`（代码里叫 `TILE_KPE`，如 64）。

这样设计的好处是：位置信息被「隔离」到独立的小维度段，不干扰低秩潜向量的吸收，又能在注意力分数里通过相加发挥作用。本模块只讲这一「相加」如何在内核里落地——它是三个内核共享的最关键机制，看懂它，后面 prefill 与两种解码只是套不同的调度壳。

#### 4.1.2 核心流程

在内循环里，每加载一个 key 块，分数瓦片 `qk` 要**累加两次 `mma`**：

```
qk = 0                                   # [TILE_M, TILE_N] 或 [TILE_N, TILE_H]
qk = mma(q,   k,   qk)   # 第一段：内容相似度 q·k   （维度 D）
qk = mma(qpe, kpe, qk)   # 第二段：位置相似度 qpe·kpe（维度 D_PE），累加进同一个 qk
# 之后 qk 走标准的在线 softmax
```

要点：

1. 两次 `mma` 的**缩减维度不同**（`D` 与 `D_PE`），但**输出形状相同**（都是 `[TILE_M, TILE_N]`），所以能直接累加。
2. `q`、`qpe` 在循环外**只加载一次**并常驻片上，循环内每轮加载 `k`、`kpe` 块。
3. 缩放 `scale = 1/sqrt(D+D_PE)` 把两段维度一起算进去（见 4.1.3 的默认值代码）。

#### 4.1.3 源码精读

以 prefill 内核为例（解码内核结构一致，只是瓦片轴不同）。先看默认缩放如何把两段维度相加，位于 `tile_mla` 注册实现里：

[mla.py:285-289](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla.py#L285-L289) — `tile_mla` 注册为 `"mla"` 算子的 cutile 实现；`scaling` 默认 `1/sqrt(q.size(-1) + qpe.size(-1))`，即 `D + D_PE`。

进入内核体，缩放系数预先乘上 `1/ln2`，以便后面用 `exp2` 快速路径（与 u6-l1 的 FMHA 同款技巧）：

[mla.py:67](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla.py#L67) — `qk_scale = qk_scale * INV_LOG_2`。

循环外一次性加载 `q`、`qpe`（内容段与位置段各一块）：

[mla.py:80-94](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla.py#L80-L94) — 加载 `q` 为 `[TILE_M, TILE_D]`、`qpe` 为 `[TILE_M, TILE_KPE]`，常驻片上供整个 KV 循环复用。

接着是本模块最核心的「联合编码」——同一个 `qk` 瓦片被两次 `mma` 累加：

[mla.py:104-123](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla.py#L104-L123) — 先 `qk = ct.mma(q, k, qk)` 算内容相似度（缩减维 `TILE_D`），再 `qk = ct.mma(qpe, kpe, qk)` 把位置相似度（缩减维 `TILE_KPE`）**累加进同一个 `qk`**。注意 `k` 用 `order=(0,1,3,2)` 加载（边搬边转置得到 `K^T`），`kpe` 同理。

两次 `mma` 之后，`qk` 已经是「内容 + 位置」的完整分数，随后走的就是 u6-l1 里见过的在线 softmax（`m_i`/`l_i` 在线更新、`exp2`、`alpha` 平移）：

[mla.py:131-141](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla.py#L131-L141) — 在线 softmax 更新 `m_i`/`l_i`，并用 `alpha` 把旧累加器平移到新基准。

解码内核里的同一模式（瓦片形状不同，但「两次 mma 累加」完全一致）：

[mla_decoding.py:79-89](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla_decoding.py#L79-L89) — `qk = ct.mma(k, q, qk)` 后 `qk = ct.mma(kpe, qpe, qk)`，两段相加。

> 结论：**联合位置编码 = 在分数瓦片上多累加一次 `mma(qpe, kpe)`**。这是 MLA 区别于 FMHA 的唯一算术差异，prefill 与两种解码都遵循它。

#### 4.1.4 代码实践

**实践目标**：用 PyTorch 复现 MLA 的「两段相加」分数，验证它与「把 q/qpe 拼接、k/kpe 拼接后做一次大点积」等价。

**操作步骤**（纯 PyTorch，可在 CPU 跑，不依赖 GPU）：

```python
# 示例代码：验证 (q·k) + (qpe·kpe) == [q|qpe]·[k|kpe]
import torch
D, D_PE = 8, 4
q   = torch.randn(D)
qpe = torch.randn(D_PE)
k   = torch.randn(D)
kpe = torch.randn(D_PE)

# 方式 A：两段相加（MLA 内核的做法）
score_A = (q @ k) + (qpe @ kpe)

# 方式 B：拼接成大向量做一次点积
q_cat  = torch.cat([q, qpe])
k_cat  = torch.cat([k, kpe])
score_B = q_cat @ k_cat

print(torch.allclose(score_A, score_B))   # 预期 True
```

**需要观察的现象**：两种算式的数值完全相等（容差内）。

**预期结果**：打印 `True`。这从数学上解释了为什么 cuTile 内核里能写成两次独立 `mma` 相加——它们在逻辑上等价于一次拼接点积，但分开写让两段能用各自最合适的瓦片尺寸（`TILE_D` 与 `TILE_KPE` 通常不同），并让位置段可以独立做 RoPE。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `qpe`/`kpe` 的维度 `D_PE` 设为 0（即不传位置编码），MLA 内核的分数会退化成什么？

**答案**：退化成普通的 `q·k`（只剩内容段），与 u6-l1 的 FMHA 分数完全一致。代码里 `kpe` 的形状带 `TILE_KPE` 维，当 `D_PE=0` 时第二次 `mma` 退化为零贡献。（注：仓库测试默认 `D_PE=64`，0 维仅为概念推演。）

**练习 2**：为什么缩放系数是 `1/sqrt(D+D_PE)` 而不是 `1/sqrt(D)`？

**答案**：因为分数是两段点积之和，等价于一个长度为 `D+D_PE` 的向量的点积（见 4.1.4）。点积的方差与向量长度成正比，故用总长度 `D+D_PE` 来归一化才能保持 softmax 的数值尺度稳定。

---

### 4.2 prefill tile_mla 与 pre-SM90 autotune

#### 4.2.1 概念说明

`tile_mla` 是 MLA 的 **prefill**（预填充）内核：query 有很多行（整段 prompt），需要计算完整的因果注意力。它建立在 u6-l1 的 FMHA prefill 框架之上——分块 QK、在线 softmax、causal 掩码、`exp2`——只在分数计算处换成 4.1 的「两段相加」。此外它支持 **GQA**（query 头多于 KV 头时共享 KV 头），并接入了 u5-l3 讲过的 **tune-once/cache/launch** 自动调优。

自动调优的关键设计是「**按 GPU 架构产出不同候选瓦片**」：在 Hopper 之前的 pre-SM90（sm80，如 Ampere）上，硬件不支持线程块簇（CGA），所以候选一律 `num_ctas=1`，可调旋钮只剩 `TILE_M`/`TILE_N`/`occupancy`；到了 SM90+（Hopper/Blackwell），候选才放开更大的瓦片（最高 `256×128`）。本轮更新正是发生在 pre-SM90 这张表上——新增一条 `64×64/occupancy=3` 候选，用来救「小规模 MLA 并行度不足」的场景（详见 4.2.3 末尾与 4.2.4）。

#### 4.2.2 核心流程

prefill 内核的调度（伪代码）：

```
grid = (ceil(S_qo / TILE_M), B * H, 1)      # 每个块算一组 query 行 ×（batch, head）
bid_x → query 行块；bid_y // H → batch；bid_y % H → query head

qk_scale = scale * (1/ln2)
加载 q [TILE_M, TILE_D]、qpe [TILE_M, TILE_KPE]   # 循环外常驻

causal 上界 hi = ceil((bid_x+1)*TILE_M / TILE_N)  # 只扫到自身行块对应的 key 块
for j in 0..hi:                                    # key 块循环
    加载 k [TILE_D, TILE_N]（转置）、kpe [TILE_KPE, TILE_N]
    qk = mma(q, k) + mma(qpe, kpe)                 # 联合位置编码（4.1）
    在近对角块 j>=mask_start 处加 causal 掩码
    在线 softmax 更新 m_i/l_i/acc
    加载 v [TILE_N, TILE_D]；acc = mma(p, v)
acc /= l_i                                         # 归一化
写回 Out
```

几个要点：

- **causal 优化**：query 行块 `bid_x` 只需要扫到 `hi = ceil((bid_x+1)*TILE_M/TILE_N)` 之前的 key 块（因果性保证更后面的 key 全被掩码，跳过省算）；只在「近对角线」的块（`j >= mask_start`）才真正构造逐元素掩码。这与 u6-l1 的 FMHA causal 优化同源。
- **GQA**：当 query 头数 `H` 大于 KV 头数时，`query_group_size = H / num_head_kv`，多个 query 头共享同一个 KV 头（`off_kv_h = head_idx // query_group_size`）。
- **exp2 快速路径**：`qk_scale` 预乘 `1/ln2`，循环内用 `ct.exp2` 代替 `exp`。
- **自动调优**：按 GPU 架构分 sm80（pre-SM90，仅 `num_ctas=1`）与 sm90+ 两套候选瓦片，首次按 cache_key 调一次 `exhaustive_search`，把最优配置与特化内核整体缓存；当设置 `TILEGYM_DISABLE_AUTOTUNE` 时，直接取第一条候选配置启动，跳过搜索。

#### 4.2.3 源码精读

内核签名与网格索引：

[mla.py:43-67](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla.py#L43-L67) — `_prefill_mla_kernel` 用 `@ct.kernel` 定义；`TILE_D`/`TILE_KPE`/`TILE_M`/`TILE_N` 是 `ConstInt`（编译期常量，会烤进特化内核）；`bid_x`/`bid_y` 解出 batch、head，GQA 下算出共享的 `off_kv_h`；`qk_scale` 预乘 `INV_LOG_2`。

causal 上界与掩码（与 u6-l1 同款的近对角线裁剪）：

[mla.py:96-128](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla.py#L96-L128) — `hi = ct.cdiv((start_m+1)*TILE_M, TILE_N)` 限定 key 块循环上界；`mask_start` 之后才造 `offs_m >= (curr_n+offs_n)` 的因果掩码，越界处置 `-1.0e6`。

联合编码的两次 `mma`（已在 4.1.3 引用）+ 在线 softmax 后，加载 `v` 并累加：

[mla.py:143-157](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla.py#L143-L157) — 加载 `v` 为 `[TILE_N, TILE_D]`，`p` 降到 `Q.dtype` 后 `acc = ct.mma(p, v, acc)`；循环结束 `acc /= l_i`，降回 `Q.dtype` 写回 `Out`。

**自动调优的两套候选配置（本轮更新点）**：

[mla.py:25-40](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla.py#L25-L40) — `_mla_sm80_autotune_configs`（pre-SM90）与 `_mla_sm90_autotune_configs`（SM90+）。pre-SM90 的候选展开后是：

```python
# _mla_sm80_autotune_configs() 现在产出 5 条候选（第 5 条是本轮新增）
#   TILE_M=64,  TILE_N=64,  num_ctas=1, occupancy=2   ← 来自双重循环
#   TILE_M=64,  TILE_N=128, num_ctas=1, occupancy=2
#   TILE_M=128, TILE_N=64,  num_ctas=1, occupancy=2
#   TILE_M=128, TILE_N=128, num_ctas=1, occupancy=2
#   TILE_M=64,  TILE_N=64,  num_ctas=1, occupancy=3   ← 本轮新增（mla.py:30）
```

[mla.py:30](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla.py#L30) — 新增的 `SimpleNamespace(TILE_M=64, TILE_N=64, num_ctas=1, occupancy=3)`。

对照 [mla.py:33-40](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla.py#L33-L40) 的 SM90+ 表，可以看出两套架构候选的**三点差异**：

| 差异点 | pre-SM90（sm80） | SM90+（sm90/sm100/sm120） |
| --- | --- | --- |
| `num_ctas`（CGA 线程块簇） | 恒为 `1`（pre-Hopper 不支持簇） | 可 `>1`（代码里仍取 `1`，但架构允许更大） |
| 最大瓦片 | `128×128` | `256×128`（多了 256 这一档 `TILE_M`） |
| `occupancy` 取值 | `2`，外加新增的 `3` | 仅 `1` 与 `2` |

**为何新增 `64×64/occupancy=3` 能帮助小规模 MLA**：prefill 的 grid 第一维是 `ceil(S_qo / TILE_M)`。当 prompt 较短（`S_qo` 小）或 batch/头数少时，grid 很小、CTA 数填不满 SM，大量 SM 闲置。把 `TILE_M` 从 128 降到 64 能让 CTA 数翻倍；而 `64×64` 是所有候选里**瓦片最小、寄存器/共享内存占用最低**的一档，足以在一个 SM 上**同时驻留 3 个 CTA**（`occupancy=3`，其余候选只能驻留 2 个）。于是这块小瓦片在最受限的小规模场景下，靠「更多 CTA × 每 SM 更多并发」换来更高的硬件占用率——这与 u5-l2/u5-l3 里为 sm100+ 静态持久化 matmul 新增 `128×128` 小瓦片候选是同一类「救 stranded SM」的思路，只是这里用 `occupancy` 而非 `num_ctas` 作旋钮（pre-SM90 没有 CGA 可用）。`exhaustive_search` 会在 5 条候选里实测择优，小规模场景更可能选中这条新的高占用候选，大规模场景仍会回到大瓦片。

**tune-once/cache/launch 的真正入口**：

[mla.py:285-303](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla.py#L285-L303) — `tile_mla` 在做完形状校验、算出 `query_group_size`、分配输出 `o` 后，把启动**完全委托**给 `_cutile_autotune_mla`（而非某个 `autograd.Function`）。

[mla.py:228-282](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla.py#L228-L282) — `_cutile_autotune_mla` 是实际的「调优 + 缓存 + 启动」三合一函数。先按 GPU capability 在 sm80/sm90+ 两套候选间二选一（[mla.py:232-233](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla.py#L232-L233)），算 `cache_key`；之后分两条路径：

- **`is_autotune_disabled()` 为真**（[mla.py:236-245](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla.py#L236-L245)）：取 `next(_configs_fn())` 即「第一条候选」（sm80 下是 `64×64/occupancy=2`），`replace_hints` 后直接启动，**不搜索、不写缓存**。这是 u5-l3 讲过的全局开关 `TILEGYM_DISABLE_AUTOTUNE` 在 MLA prefill 上的作用点——意味着 MLA prefill **会**响应这个开关（受 `next()` 取首条候选的影响，选哪条进入搜索队列的顺序因此重要）。
- **否则**（[mla.py:247-282](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla.py#L247-L282)）：首次按 `cache_key` 跑 `exhaustive_search`（三回调 `grid_fn`/`args_fn`/`hints_fn`，与 u5-l3 一致），把 `(best_cfg, replace_hints 后的 tuned_kernel)` 整体存进模块级 `_mla_tune_cache`（[mla.py:17](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla.py#L17)），之后直接 `ct.launch` 零开销；`compiler_timeout(5)` 是单候选编译护栏。

注意一个重要限制：MLA prefill **只支持推理/前向**，没有反向内核——`_cutile_autotune_mla` 只做前向启动，文件里另有一个未被 `tile_mla` 调用的遗留 autograd 桩 `_AttentionFunction`，其 `backward` 直接抛 `NotImplementedError`：

[mla.py:223-225](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla.py#L223-L225) — `backward` 抛 `NotImplementedError("Backward pass is not implemented for CuTile MLA")`，印证 MLA prefill 当前不可训练。

#### 4.2.4 代码实践

**实践目标 1**：跟踪 prefill 内核的 causal 上界计算，理解「query 行块只扫到自身对应的 key 块」如何省算。

**操作步骤 1**（源码阅读型实践）：

1. 打开 [mla.py:96-101](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla.py#L96-L101)。
2. 假设 `TILE_M=128`、`TILE_N=64`、`S_qo=512`（即 `bid_x` 取 0..3）。
3. 对 `bid_x=0`：`start_m=0`，`hi = cdiv(128, 64) = 2` → 只扫 key 块 `j=0,1`。
4. 对 `bid_x=3`（最后一个 query 块，覆盖 query 行 384..511）：`hi = cdiv(512, 64) = 8` → 扫 `j=0..7`（全部 key 块）。

**需要观察的现象**：靠后的 query 行块要扫更多 key 块（因果性允许它看到更多历史），靠前的 query 行块几乎不扫。

**预期结果**：手工算出 `bid_x=0 → hi=2`、`bid_x=1 → hi=4`、`bid_x=2 → hi=6`、`bid_x=3 → hi=8`。这解释了 causal 注意力的总计算量约为完整注意力的「一半」（三角形），也是 Flash 因果优化的收益来源。

**实践目标 2**：解释本轮新增的 sm80 `64×64/occupancy=3` 候选为何有助于小规模 MLA。

**操作步骤 2**（源码阅读 + 推理型实践）：

1. 阅读 [mla.py:25-30](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla.py#L25-L30)，确认 sm80 表的 5 条候选。
2. 取一个「小规模」MLA：`S_qo=128, B=1, H=64`，比较两条候选的理论 grid 与每 SM 并发：
   - 候选 `128×128/occupancy=2`：grid 第一维 `ceil(128/128)=1`，总 CTA `=1×B×H=64`；每 SM 最多并发 2 个 CTA。
   - 新候选 `64×64/occupancy=3`：grid 第一维 `ceil(128/64)=2`，总 CTA `=2×B×H=128`；每 SM 最多并发 3 个 CTA。
3. 注意总 CTA 从 64 翻倍到 128，且每 SM 并发从 2 升到 3——两者都把「能同时跑的 CTA 数」往上推。

**需要观察的现象**：在 CTA 数本就稀缺的小规模场景，新候选把 CTA 数和每 SM 并发都抬高，硬件占用率随之上升。

**预期结果**：小规模 MLA 下，`exhaustive_search` 更可能选中 `64×64/occupancy=3`（实测最快）；大规模（大 `S_qo`、大 batch）下大瓦片仍占优，搜索会自动回到 `128×128` 等候选。若想就地验证，可在 `TILEGYM_DISABLE_AUTOTUNE=1` 下分别手工把首条候选改成这两条之一跑 prefill 基准对比（需 GPU，**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：prefill 内核里 `q`/`qpe` 为什么在循环外只加载一次，而 `k`/`kpe`/`v` 在循环内每轮加载？

**答案**：一个 CTA 固定负责一组 query 行（`bid_x`），其 `q`/`qpe` 在整个 KV 循环中不变，故加载一次常驻片上反复复用；`k`/`kpe`/`v` 随 key 块 `j` 变化，必须每轮重新加载。

**练习 2**：`tile_mla` 的自动调优 cache_key 包含哪些字段？为什么需要这些字段？

**答案**：见 [mla.py:234](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla.py#L234)：`(S_qo, TILE_D, TILE_KPE, H, query_group_size, dtype, device)`。这些字段任一改变都会改变最优瓦片配置（序列长度影响 grid、维度影响寄存器占用、头数与分组影响 GQA 路径、dtype 影响张量核心路径），所以都要进 key 才能正确命中缓存。

**练习 3**（本轮新增）：pre-SM90（sm80）表里为什么只有 `num_ctas=1` 的候选，却出现了 `occupancy=3`？这与 SM90+ 表有何对照？

**答案**：pre-SM90（Ampere 等）硬件不支持线程块簇（CGA），所以 `num_ctas` 只能是 1，没法像 u5-l2 那样靠 `num_ctas>1` 聚合 CTA 来提并行度；于是 sm80 只能用 `occupancy`（每 SM 并发 CTA 数）这个旋钮，新增的 `occupancy=3` 正是在最小瓦片 `64×64` 上把单 SM 并发拉满。SM90+ 表（[mla.py:33-40](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla.py#L33-L40)）则反过来：靠更大的 `TILE_M`（最高 256）和更细的 occupancy=1/2 组合来覆盖大规模场景，不需要 occupancy=3。两张表是「同一调优目标、不同架构旋钮」的互补设计。

---

### 4.3 解码 mla_decoding

#### 4.3.1 概念说明

`mla_decoding` 是 MLA 的**单步解码**内核：每步只生成 1 个 token，query 只有 1 行。与 u6-l2 的 `fmha_decode` 面临同样的问题——天然并行度只有 `B·H`，可能远少于 SM 数。但它**不**做 Split-KV（那是 4.4 的事），而是采用另一种并行策略：**按头并行**（一个 CTA 处理 `TILE_H=16` 个头），让一个 CTA 串行扫完整条 KV。

它还有一个 MLA 特有的形态：**吸收态（absorbed form）**。内核名 `_naive_absorb_mla_transpose_kernel` 里的「absorb」指：解码阶段 key 与 value 共用同一个潜张量 `kv`——MLA 的低秩吸收让 K、V 同源，所以只需缓存一份。体现在启动参数里，就是 `K` 和 `V` 两个内核形参被绑定到**同一个** `kv` 张量。

#### 4.3.2 核心流程

解码内核的调度（伪代码）：

```
grid = (ceil(num_head / TILE_H), B, 1)   # bid_x → 头组（TILE_H 个头），bid_y → batch
TILE_H = 16, TILE_N = 128

加载 q   [TILE_D,   TILE_H]              # TILE_H 个头的内容段，常驻
加载 qpe [TILE_KPE, TILE_H]              # TILE_H 个头的位置段，常驻
m_prev [TILE_H] = -inf；l_prev [TILE_N, TILE_H] = 1；acc [TILE_D, TILE_H] = 0

for curr_n in 0..S_kv step TILE_N:        # 整条 KV 串行扫描（无切分）
    k   = kv[块]                          # [TILE_N, TILE_D]
    kpe = kpe[块]                         # [TILE_N, TILE_KPE]
    qk = mma(k, q) + mma(kpe, qpe)        # 联合位置编码（4.1），[TILE_N, TILE_H]
    在线 softmax 更新 m_prev/l_prev/acc   # 沿 TILE_N(即 KV) 维归约
    v = kv[块]（转置）                     # 同一个 kv 兼作 value（吸收态）
    acc = mma(v, p, acc)                  # [TILE_D, TILE_H]
acc /= l_prev
写回 o [B, num_head, TILE_D] 与 LSE l [B, num_head]
```

要点：

- **按头并行**：query 只有 1 行，所以并行维度从 query 行换成「头」——每个 CTA 算 `TILE_H=16` 个头，`bid_x` 取 `0..ceil(H/16)`。
- **吸收态**：`kv` 同时充当 K 和 V，启动时形参 `K`、`V` 都绑定到 `kv`。
- **整条 KV 串行**：不像 4.4 切分，这里一个 CTA 从头扫到尾，所以并行度就是 `ceil(H/16)·B`。
- **返回 `(o, l)`**：除了输出 `o`，还返回 LSE `l`（`m + log2(l)`），供上游（如把多个 batch 的结果再合并时）使用。

#### 4.3.3 源码精读

内核签名与累加器初始化（注意 `m_prev` 沿头维 `TILE_H`、`l_prev` 是 `[TILE_N, TILE_H]`）：

[mla_decoding.py:19-44](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla_decoding.py#L19-L44) — `q`/`qpe` 用 `allow_tma=True` + `order=(0,2,1)` 的转置 TMA 加载，reshape 成 `[TILE_D, TILE_H]` / `[TILE_KPE, TILE_H]`；`bid_y = batch_idx`。

KV 循环与联合编码（与 prefill 同构，瓦片形状不同）：

[mla_decoding.py:69-119](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla_decoding.py#L69-L119) — `qk = ct.mma(k, q, qk)` 后 `qk = ct.mma(kpe, qpe, qk)`；在线 softmax 沿 KV 维（`TILE_N`）归约；`v` 从同一个 `kv` 加载并转置后做 `mma(v, p, acc)`。

吸收态的关键证据——启动时把 `kv` 同时传给内核的 `K` 和 `V` 形参：

[mla_decoding.py:155-176](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla_decoding.py#L155-L176) — `grid = (ceil(num_head/TILE_H), B, 1)`；`ct.launch` 的参数元组里 `kv, kv, kpe`——`K=kv`、`V=kv`，故称「absorb」；输出 `o` 与 LSE `l`。

注册实现与默认参数：

[mla_decoding.py:184-200](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla_decoding.py#L184-L200) — `mla_decoding` 注册为 `"mla_decoding"` 算子；`TILE_H=16`、`TILE_N=128` 写死；`assert transpose == True`（cuTile 只支持转置布局）；`sm_scale` 默认 `1/sqrt(D+D_PE)`。

> 与 u6-l2 的 `fmha_decode` 对照：`fmha_decode` 是「K、V 分开 + Split-KV 两阶段」；`mla_decoding` 是「K=V 吸收态 + 不切分、整条串扫 + 按头并行」。两者都返回中间量供下游合并，但切分粒度不同。

#### 4.3.4 代码实践

**实践目标**：用最小脚本调用 `mla_decoding` 并与 PyTorch 参考对比（形状取自仓库测试）。

**操作步骤**：

```python
# 示例代码：最小调用 mla_decoding（需 GPU + cuTile 后端）
import math, torch, tilegym
from tilegym.backend import set_backend
set_backend("cutile")

device = "cuda"; dtype = torch.float16
B, H, D, D_PE, S_kv = 2, 32, 512, 64, 1024
q   = torch.randn(B, H, D,    device=device).to(dtype)
qpe = torch.randn(B, H, D_PE, device=device).to(dtype)
kv  = torch.randn(B, S_kv, D,    device=device).to(dtype)   # 注意：kv 兼作 K 和 V
kpe = torch.randn(B, S_kv, D_PE, device=device).to(dtype)
sm_scale = 1.0 / math.sqrt(D + D_PE)

o, l = tilegym.ops.mla_decoding(q, qpe, kv, kpe, sm_scale)   # 返回 (o, l)
print(o.shape, l.shape)   # 预期 torch.Size([2, 32, 512]) torch.Size([2, 32])
```

**需要观察的现象**：`kv` 只有一个张量（吸收态），却同时充当 key 和 value；输出 `o` 形状 `[B, H, D]`，LSE `l` 形状 `[B, H]`。

**预期结果**：`o.shape == [2, 32, 512]`、`l.shape == [2, 32]`。与 PyTorch 参考（见 `tests/ops/test_mla_decoding.py` 的 `reference`，容差 `rtol=atol=1e-2`）比对应在容差内。**待本地验证**（本环境无 GPU）。

> 正确性参考实现可对照 [test_mla_decoding.py:17-44](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/tests/ops/test_mla_decoding.py#L17-L44)：参考用 `qk = matmul(q, kv.transpose)` + `qk += matmul(qpe, kpe.transpose)`，与本讲的联合编码公式一致。

#### 4.3.5 小练习与答案

**练习 1**：解码内核里 query 只有 1 行，并行度从哪里来？

**答案**：从「头」维来。grid 第一维是 `ceil(num_head/TILE_H)`，每个 CTA 算 `TILE_H=16` 个头；加上 batch 维，总并行度为 `ceil(H/16)·B`。这是「按头并行」策略。

**练习 2**：为什么说 `mla_decoding` 是「吸收态」？从启动参数给出证据。

**答案**：见 [mla_decoding.py:163-165](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla_decoding.py#L163-L165)：`ct.launch` 的参数元组把 `kv` 同时传给内核的 `K` 和 `V` 形参（`kv, kv, kpe`），即 key、value 同源，只缓存一份潜向量——这正是 MLA 低秩吸收在解码侧的体现。

---

### 4.4 split_kv 并行

#### 4.4.1 概念说明

4.3 的 `mla_decoding` 让一个 CTA 串行扫完整条 KV。当上下文很长（`S_kv` 很大）时，**并行度仍然不够**：CTA 数只有 `ceil(H/16)·B`，可能远少于 SM 数，而且每个 CTA 的工作量随 `S_kv` 线性增长，解码延迟变高。

`mla_decoding_split_kv` 的对策正是 u6-l2 学过的 **Split-KV**：沿 KV 归约维把长序列切成 `NUM_KV_SPLITS` 段，每段派一个 CTA 并行做在线 softmax，可并行任务数提升到 `ceil(H/16)·B·NUM_KV_SPLITS`，再用 `splitk_reduce` 按 LSE 合并。切分粒度由参数 `kv_len_per_split` 控制——这是它与 4.3 版本最直接的使用差异。

#### 4.4.2 核心流程

Split-KV 解码的两阶段（伪代码）：

```
阶段 1：split 内核（3D 网格）
grid = (ceil(num_head/TILE_H), B, NUM_KV_SPLITS)   # 第 3 维是新加的 split 维
每个 CTA 只扫 [split_kv_start, split_kv_end) 这一段 KV：
    KV_LEN_PER_SPLIT = kv_len_per_split
    split_kv_start = KV_LEN_PER_SPLIT * tile_idx
    联合编码 + 在线 softmax（同 4.3，但只覆盖本段）
    产出本段的 部分输出 õ_s  与 段内 LSE ℓ_s
    写入 Att_Out[B, NUM_HEADS, NUM_KV_SPLITS, D] 与 LSE_Out[B, NUM_HEADS, NUM_KV_SPLITS]

阶段 2：splitk_reduce（复用 u6-l2 的合并内核）
读 Att_Out、LSE_Out，按两层 Flash 公式合并：
    o = Σ 2^(ℓ_s − M)·õ_s  /  Σ 2^(ℓ_s − M)，M = max_s ℓ_s
写出最终 O[B, NUM_HEADS, D]
```

要点：

- **kv 布局与 4.3 完全相同**：仍是 `[B, S_kv, D]` 的吸收态 `kv`（兼作 K/V）。Split-KV 改的是「并行调度」，不是「数据布局」。
- **`kv_len_per_split` 控制并行度**：它越小，`NUM_KV_SPLITS` 越多，并行度越高（但合并开销也越大）。为 `None` 时自动估算，目标是「让每个 SM 至少分到一个 split」。
- **只返回 `o`**：与 4.3 返回 `(o, l)` 不同，split 版把 LSE 合并做进了内部 `splitk_reduce`，对外只给最终输出。
- **自动切分估算**：`NUM_KV_SPLITS ≈ NUM_SMS // B`，`kv_len_per_split = next_power_of_2(S_kv // NUM_KV_SPLITS)`，且不小于 `TILE_N=128`。

#### 4.4.3 源码精读

split 内核多了 `NUM_KV_SPLITS`、`KV_LEN_PER_SPLIT` 两个 `ConstInt` 形参，并用 `@ct.kernel(occupancy=2)` 给出占用提示：

[mla_decoding_split_kv.py:22-46](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla_decoding_split_kv.py#L22-L46) — `pid_x`→头组、`batch_idx = ct.bid(1)`、`tile_idx = ct.bid(2)`（split 维）；与 4.3 同样的 `q`/`qpe`/`m_prev`/`l_prev`/`acc` 初始化。

每个 CTA 只扫自己的 split 段（这是与 4.3 唯一的循环差异）：

[mla_decoding_split_kv.py:76-86](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla_decoding_split_kv.py#L76-L86) — `split_kv_start = KV_LEN_PER_SPLIT * tile_idx`，`split_kv_end` 截断到 `S_kv`，循环 `for curr_n in range(split_kv_start, split_kv_end, TILE_N)` 只覆盖本段。

写出本段的中间结果（供阶段 2 合并）：

[mla_decoding_split_kv.py:144-169](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla_decoding_split_kv.py#L144-L169) — `acc /= l_prev` 后写 `Att_Out[b, head, tile_idx, :]`；`l_prev = m_prev + log2(l_prev)` 即本段 LSE，写 `LSE_Out`（用 `ct.scatter` + `check_bounds`，因为头数可能不是 `TILE_H` 整数倍）。

阶段 1 的启动与阶段 2 的合并调用（3D grid + `splitk_reduce`）：

[mla_decoding_split_kv.py:200-262](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla_decoding_split_kv.py#L200-L262) — 自动估算 `kv_len_per_split`（`NUM_SMS//B` 个 split，取 `next_power_of_2` 且 `>=TILE_N`），算 `NUM_KV_SPLITS`；`grid_split = (ceil(H/TILE_H), B, NUM_KV_SPLITS)`；启动 split 内核后立即调 `splitk_reduce(Att_Out, LSE_Out, O, S_kv)`。

`splitk_reduce` 内部的 LSE 合并（u6-l2 已详讲，这里只定位关键行）：

[splitk_reduce.py:59-84](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/splitk_reduce.py#L59-L84) — `lse_max = max(lse)`，`sumexp = Σ 2^(lse − lse_max)`，分子用 `mma`（split 多时）或逐元素加权求和（split 少时），最后 `acc = numerator / sumexp`。

注册实现：

[mla_decoding_split_kv.py:269-290](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla_decoding_split_kv.py#L269-L290) — `mla_decoding_split_kv` 注册为同名算子；`sm_scale` 默认 `1/sqrt(D+D_PE)`；对外只返回 `o`。

> 与 4.3 的核心区别：**相同的 kv 布局、相同的联合编码、相同的在线 softmax，只是把「一个 CTA 扫全程」换成「多个 CTA 各扫一段 + LSE 合并」**。这就是 u6-l2 的「两层 Flash attention」骨架被原样搬到 MLA 上的结果。

#### 4.4.4 代码实践

**实践目标**：对比 `mla_decoding` 与 `mla_decoding_split_kv` 的 kv 布局与 `kv_len_per_split` 作用，判断何时该用 split 版本。

**操作步骤**：

1. 对照 [mla_decoding.py:185-193](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla_decoding.py#L185-L193) 与 [mla_decoding_split_kv.py:269-285](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla_decoding_split_kv.py#L269-L285) 两个函数签名。
2. 记录两者的入参：两者都接收 `(q, qpe, kv, kpe, sm_scale)`，`kv` 形状都是 `[B, S_kv, D]`（吸收态，兼作 K/V）——**kv 布局相同**。
3. 唯一的接口差异是 split 版多了 `kv_len_per_split: Optional[int]`，普通版多了 `transpose: bool`。
4. 用同一个 `kv` 分别调用两者并对比输出（需 GPU，**待本地验证**）：

```python
# 示例代码：同输入对比两种解码（需 GPU + cuTile 后端）
import math, torch, tilegym
from tilegym.backend import set_backend
set_backend("cutile")
device = "cuda"; dtype = torch.float16
B, H, D, D_PE, S_kv = 1, 16, 512, 64, 8192   # 长上下文
q   = torch.randn(B, H, D,    device=device).to(dtype)
qpe = torch.randn(B, H, D_PE, device=device).to(dtype)
kv  = torch.randn(B, S_kv, D,    device=device).to(dtype)
kpe = torch.randn(B, S_kv, D_PE, device=device).to(dtype)
sm_scale = 1.0 / math.sqrt(D + D_PE)

o0, _ = tilegym.ops.mla_decoding(q, qpe, kv, kpe, sm_scale)
o1    = tilegym.ops.mla_decoding_split_kv(q, qpe, kv, kpe, sm_scale)              # 自动切分
o2    = tilegym.ops.mla_decoding_split_kv(q, qpe, kv, kpe, sm_scale, kv_len_per_split=128)  # 更细切分
print(torch.allclose(o0, o1, atol=1e-2, rtol=1e-2))   # 预期 True：切分数值等价
```

**需要观察的现象**：三个输出在容差内数值一致（切分不改变结果，只改变并行度）；`S_kv=8192` 这种长上下文下，split 版的延迟通常显著低于普通版。

**何时选 split_kv 版本**：当 `S_kv` 较大、`ceil(H/16)·B` 远小于 GPU 的 SM 数时（即普通版的 CTA 数填不满机器），应选 `mla_decoding_split_kv`，用 `kv_len_per_split` 把 KV 切分以提升并行度；`S_kv` 较小或头数/batch 已足够填满 SM 时，普通版 `mla_decoding` 更省一次合并内核的开销。

> 仓库测试 [test_mla_decoding_split_kv.py:59-62](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/tests/ops/test_mla_decoding_split_kv.py#L59-L62) 用 `kv_len_per_split ∈ {128, 512}` 覆盖「细切分」与「粗切分」两种粒度，并特意测了 8、24 这种「非 TILE_H 整数倍」的头数（DeepSeek 在张量并行下的回归形状）。

#### 4.4.5 小练习与答案

**练习 1**：`mla_decoding` 与 `mla_decoding_split_kv` 的 `kv` 张量布局有何不同？

**答案**：**没有不同**，都是吸收态 `[B, S_kv, D]`，兼作 K 和 V。两者的差别只在调度（整条串扫 vs 切分并行），不在数据布局。

**练习 2**：`kv_len_per_split` 调小会发生什么？有无代价？

**答案**：`kv_len_per_split` 越小 → `NUM_KV_SPLITS` 越大 → 阶段 1 并行度越高、单 CTA 工作量越小，长上下文延迟下降。代价是阶段 2 `splitk_reduce` 要合并更多段、中间张量 `Att_Out`/`LSE_Out` 更大、合并内核耗时上升。所以它是个「并行度 vs 合并开销」的折中旋钮，仓库用「每个 SM 至少一个 split」启发式自动取值。

**练习 3**：为什么 split 版对外只返回 `o`，而普通版返回 `(o, l)`？

**答案**：split 版把各段的 LSE 合并做进了内部的 `splitk_reduce`（见 [mla_decoding_split_kv.py:260](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla_decoding_split_kv.py#L260)），合并后段内 LSE 已无意义，故只返回最终 `o`；普通版没有合并步骤，把段内（整条 KV 的）LSE `l` 一并返回，供上游需要时复用。

---

## 5. 综合实践

**任务**：把本讲四个模块串起来，画一张「MLA 三个内核的同与异」对照表，并写一个最小脚本验证「联合位置编码」与「Split-KV 数值等价」。

1. **填表**（源码阅读型）：阅读三个内核，填写下表（答案见下方）。

| 维度 | `tile_mla`（prefill） | `mla_decoding` | `mla_decoding_split_kv` |
| --- | --- | --- | --- |
| query 行数 | 多行（prompt） | 1 行（单步） | 1 行（单步） |
| 并行维度 | query 行块 × (B·H) | 头组 × B | 头组 × B × **NUM_KV_SPLITS** |
| kv 形态 | K、V 分开 | 吸收态（kv 兼作 K/V） | 吸收态（kv 兼作 K/V） |
| 联合编码 qpe/kpe | 有（两次 mma 累加） | 有 | 有 |
| 是否 Split-KV | 否 | 否 | 是 + `splitk_reduce` |
| 返回 | `o` | `(o, l)` | `o` |
| 自动调优 | 有（tune-once/cache，sm80/sm90+ 两套候选） | 无（固定 TILE_H/N） | 无（固定 TILE_H/N） |

2. **验证等价性**（需 GPU，**待本地验证**）：
   - 用 4.1.4 的脚本验证「两段相加 == 拼接点积」。
   - 用 4.4.4 的脚本验证「普通解码 == split 解码」（容差内），亲手确认 Split-KV 不改变数值结果。
3. **（本轮新增）解释 autotune 架构差异**：对照 [mla.py:25-40](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla.py#L25-L40) 写出 pre-SM90 与 SM90+ 候选的三点差异（num_ctas、最大瓦片、occupancy 取值），并说明新增的 `64×64/occupancy=3` 在哪种规模下会被 `exhaustive_search` 选中。

**参考答案**：表中「自动调优」一行需对照 [mla.py:25-40](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla.py#L25-L40)（仅 prefill 有，且 sm80/sm90+ 两套；解码固定瓦片见 [mla_decoding.py:194-195](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla_decoding.py#L194-L195) 与 [mla_decoding_split_kv.py:197-198](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/mla_decoding_split_kv.py#L197-L198)），其余均可在本讲引用的源码行中直接核对。

## 6. 本讲小结

- **MLA 的本质差异是联合位置编码**：注意力分数 = 内容相似度 `q·k`（维 `D`）+ 位置相似度 `qpe·kpe`（维 `D_PE`），在内核里落地为「同一个分数瓦片上累加两次 `mma`」，缩放取 `1/sqrt(D+D_PE)`。除此之外，causal 掩码、在线 softmax、`exp2`、`mma(p,v)` 与 u6-l1 的 FMHA 完全一致。
- **prefill `tile_mla`** 复用 FMHA prefill 框架（分块 + 在线 softmax + causal 裁剪 + GQA），并把启动委托给 `_cutile_autotune_mla`，走 tune-once/cache/launch；MLA prefill **会**响应 `TILEGYM_DISABLE_AUTOTUNE`（禁用时取首条候选直接启动），且当前**只有前向**、反向未实现。
- **pre-SM90 autotune 候选（本轮更新）**：sm80 表新增一条 `64×64/occupancy=3`，靠「最小瓦片 + 每 SM 3 并发」在小规模 MLA（grid 填不满 SM）时把硬件占用率拉高；与 SM90+ 表（更大瓦片、occupancy=1/2）形成「不同架构、不同旋钮」的互补。
- **`mla_decoding`** 是单步解码基准版：query 1 行，改用「按头并行」（`TILE_H=16` 个头一组），整条 KV 由单个 CTA 串行扫描，返回 `(o, l)`。
- **吸收态**是 MLA 解码的形态：`kv` 兼作 K 和 V（启动时 `kv, kv`），源于 MLA 的低秩吸收，KV 缓存只存一份潜向量。
- **`mla_decoding_split_kv`** 把 u6-l2 的 Split-KV + LSE 合并原样套到 MLA 解码：3D 网格沿 KV 切分，`kv_len_per_split` 控制并行度，内部调 `splitk_reduce` 合并，对外只返回 `o`。
- **何时选 split 版**：kv 布局两种解码完全相同；长上下文（`S_kv` 大、CTA 数填不满 SM）选 split 版提并行度，短上下文选普通版省合并开销。

## 7. 下一步学习建议

- **u6-l4 专用注意力变体**：本讲的 MLA 框架会被进一步特化——`sparse_mla`（在 MLA 上叠加 top-k 稀疏索引，`ops.py` 里已有 `"sparse_mla"` stub）正是「MLA prefill + 只 attend top-k」的组合，建议对照阅读。
- **u8-l1 Transformer monkey-patch**：MLA 内核如何被接到 DeepSeek-V2 等真实模型上，看 `monkey_patch.py` 里对 MLA 类模型的替换点。
- **深入 LSE 合并**：若对 Split-KV 的数值等价性仍想吃透，重读 u6-l2 与本讲引用的 [splitk_reduce.py:59-84](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/splitk_reduce.py#L59-L84)，自己用 PyTorch 复现「分两段算 + LSE 合并」对照「整体算」的结果。
- **自动调优**：本讲 prefill 用到的 tune-once/cache/launch 机制与「按架构产出候选」的设计在 u5-l3 有系统讲解，可回顾以理解 `replace_hints`、`is_autotune_disabled` 与 `_mla_tune_cache` 的配合；u5-l2 的「stranded SM」概念则直接解释了为何要为不同 GEMM 形状/MLA 规模准备大/小两类瓦片候选。
