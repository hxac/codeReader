# GQA / MQA 与 pack_gqa

## 1. 本讲目标

学完本讲后，读者应该能够：

- 说清楚 **GQA / MQA** 与标准 MHA 在头数配比上的区别，以及为什么 GQA/MQA 能省显存、省带宽。
- 解释 FA4 用 `pack_gqa_layout` 把「多个 Q 头」折叠进「序列维」的纯视图技巧，理解它为什么能让一个 KV tile 被多个 Q 头复用。
- 看懂 `make_packgqa_tiled_tma_atom` 如何在折叠头维的同时**保持 TMA 描述符维度不变**，以及当 TMA 不可用时 kernel 如何用 `cp.async` + 手算指针的回退路径。
- **搞清楚从本版本起 Ampere(SM80) / SM120 前向基线 kernel 也完整支持 `pack_gqa`**（`__init__` 折叠 + `PackGQA` 加载/存储 Q/O/LSE），而不再像旧版本那样仅在 epilogue 处理输出。
- 自己动手构造一个 GQA 输入，分别开启和关闭 `pack_gqa` 调用 `flash_attn_func`，验证两者数值一致并观察性能差异。

本讲是**前向高级特性**的第一篇，承接 u6-l1（Ampere 前向主循环），只聚焦「GQA/MQA 在 FA4 里是怎么被加速的」这一件事。

## 2. 前置知识

在进入源码前，先用最通俗的语言把几个概念讲清楚。

### 2.1 MHA / GQA / MQA 的头数配比

注意力计算的核心是 \(S = QK^\top,\; P = \mathrm{softmax}(S/\sqrt{d}),\; O = PV\)。其中 Q 的形状里有「头数」（`num_heads`，记作 \(H\)），K/V 也有自己的头数（`num_heads_kv`，记作 \(H_{kv}\)）。

- **MHA（Multi-Head Attention）**：\(H = H_{kv}\)，每个 Q 头独占一组 K/V 头。
- **GQA（Grouped-Query Attention）**：\(H_{kv} < H\)，多个 Q 头**共享**同一组 K/V 头。每个 Q 头 \(h\) 用的是第 \(h /\!(H/H_{kv})\) 个 KV 头。
- **MQA（Multi-Query Attention）**：\(H_{kv} = 1\)，所有 Q 头共享唯一一组 K/V，是 GQA 的极端情形。

我们把这个共享比例记作 `qhead_per_kvhead = num_heads / num_heads_kv`。它必须是整数（interface.py 里会断言），含义是「一个 KV 头被几个 Q 头共享」。

> **为什么需要 GQA/MQA？** 在自回归解码里，K/V 要被缓存（KV cache）供每一步生成复用。把 \(H_{kv}\) 调小，K/V cache 的体积和读写带宽都按比例下降，是当前大模型推理省显存/提吞吐的标准手段（如 Llama、Qwen 等都用 GQA）。代价是表达能力略降，但实践中精度损失很小。

### 2.2 一个朴素问题：KV 被重复加载

如果 kernel 按传统方式为**每个 Q 头**单独发射一个工作块（work tile），那么共享同一个 KV 头的 `qhead_per_kvhead` 个 Q 头，会把**同一块 KV** 从 HBM 读 `qhead_per_kvhead` 次。GQA 的 KV 本来就是为了省带宽才做小的，结果读取端又把它放大回来，这是 FA4 想用 `pack_gqa` 解决的核心矛盾。

### 2.3 需要回顾的旧术语

本讲会用到前面讲义里建立的概念：**tile / 分块**（u6-l1）、**gmem / smem / rmem 三级存储**（u6-l1）、**TMA 与 cp.async 拷贝**（u5-l2）、**CuTe 张量与 layout（形状+步长）的纯视图操作**（u5-2）。记不住没关系，遇到时会再点一句。

## 3. 本讲源码地图

本讲主要涉及三个文件：

| 文件 | 作用 |
| --- | --- |
| [flash_attn/cute/pack_gqa.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/pack_gqa.py) | 全部 pack/unpack 逻辑：`pack_gqa_layout`（视图折叠）、`make_packgqa_tiled_tma_atom`（保持 TMA 维度）、`PackGQA`（TMA 不可用时的指针计算与 load/store）。 |
| [flash_attn/cute/interface.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py) | 公共 API。决定何时自动开启 `pack_gqa`、何时强制关闭它，并把 `seqlen_q` 在调度层面乘以 `qhead_per_kvhead`。 |
| [flash_attn/cute/flash_fwd.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py) | `FlashAttentionForwardSm80`：Ampere 前向基线，也是 **SM120 的父类**。本版本起在 `__init__` 折叠 Q/O/LSE、prologue 用 `PackGQA.load_Q`、epilogue 用 `PackGQA.store_O/store_LSE`，**完整支持 pack_gqa**。 |

辅助理解（只引用关键几行，不展开）：Hopper [`flash_fwd_sm90.py`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm90.py) 与 Blackwell [`flash_fwd_sm100.py`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py)（在 `to_underlying_arguments` 里调用 `pack_gqa_layout`），SM120 子类 [`flash_fwd_sm120.py`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm120.py)（在 `__init__` 里把 `arch` 强制为 `sm_80` 以复用 cp.async 代码路径），以及 [`block_info.py`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/block_info.py)（因果掩码在折叠后的坐标修正）。

---

## 4. 核心概念与源码讲解

### 4.1 GQA/MQA 头数配比与 KV 复用问题

#### 4.1.1 概念说明

GQA/MQA 的数学定义很简单：Q 头 \(h\) 用的是 KV 头 \(h // \text{qhead\_per\_kvhead}\)。比如 `num_heads=8, num_heads_kv=2`，则 `qhead_per_kvhead=4`，Q 头 0/1/2/3 都用 KV 头 0，Q 头 4/5/6/7 都用 KV 头 1。

难点不在数学，而在**实现效率**。FA4 的前向 kernel 把工作按 `(batch, head, m_block)` 三维切分（见 u6-l1 的 tile scheduler）。如果「head」维用 Q 的头数 \(H\)，那么共享同一 KV 头的 `qhead_per_kvhead` 个工作块会各自把同一块 KV 从 HBM 搬到 smem，造成 KV 的重复加载。

#### 4.1.2 核心流程

FA4 在公共 API 层先做两件事：

1. **校验整除关系**，并算出共享比例 `qhead_per_kvhead`。
2. **决定是否开启 `pack_gqa`**：当 `qhead_per_kvhead > 1`（即非 MHA）时，默认自动开启。

```
num_head, num_head_kv  ←  从 q/k 形状取出
assert num_head % num_head_kv == 0
qhead_per_kvhead = num_head // num_head_kv
if pack_gqa is None:
    pack_gqa = (qhead_per_kvhead > 1)   # 非 MHA 时自动开
```

注意一个关键结论：**`pack_gqa` 只改变实现，不改变数学结果**。开或关，输出 O 和 LSE 都应当（在浮点舍入误差内）一致——这也是本讲综合实践要验证的事。它进入 `compile_key`（编译缓存键），所以切换会触发重新编译，但不应改变数值。

#### 4.1.3 源码精读

整除校验与共享比例计算在 interface.py：

[interface.py:448-461](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L448-L461) —— 断言 `num_head % num_head_kv == 0`，算出 `qhead_per_kvhead`，并在用户未显式指定时按「非 MHA 自动开启」设置 `pack_gqa`。

当 `pack_gqa` 关闭时，kernel 走「按 Q 头索引 KV 头」的传统路径。所有前向 kernel 里都能看到同一个三分支判断，以 Ampere 基线为例：

[flash_fwd.py:822](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L822) —— `num_head_kv = num_head if const_expr(self.pack_gqa) else num_head // self.qhead_per_kvhead`。开启时 kernel 看到的 head 维已经是 KV 头维（因为 `__init__` 已把头折叠进序列，见 4.4），故 `num_head_kv` 直接等于 `num_head`；关闭时把 Q 头号除以共享比得到 KV 头号。Hopper kernel 里对应 `head_idx_kv = head_idx // self.qhead_per_kvhead if not pack_gqa else head_idx`：

[flash_fwd_sm90.py:681-683](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm90.py#L681-L683) —— Hopper 版本的同一逻辑，关闭时按共享比映射 Q 头号到 KV 头号。

#### 4.1.4 代码实践

**实践目标**：确认 FA4 接受 GQA 形状，且 `qhead_per_kvhead` 必须整除。

**操作步骤**（阅读型实践，无需 GPU）：

1. 读 [interface.py:448](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L448) 的断言。
2. 构造一个 `num_heads=8, nheads_kv=3` 的 q/k（故意不整除），调用 `flash_attn_func`。

**预期结果**：抛出 `AssertionError: num_head must be divisible by num_head_kv`。这验证了 GQA 的整除约束是在 Python 公共 API 层、kernel 编译之前就拦下的「早失败」护栏。

#### 4.1.5 小练习与答案

**练习 1**：`num_heads=8, nheads_kv=1` 是 MHA、GQA 还是 MQA？`qhead_per_kvhead` 是多少？

**答案**：MQA（KV 头数为 1），`qhead_per_kvhead = 8`。

**练习 2**：为什么说「按 Q 头切分工作块」会让 GQA 的 KV 被重复读取？

**答案**：共享同一 KV 头的 `qhead_per_kvhead` 个 Q 头各自成为一个工作块，每个工作块都把同一块 KV 从 HBM 搬进 smem，于是同一 KV 块被搬了 `qhead_per_kvhead` 次。

---

### 4.2 pack_gqa_layout：把头维折叠进序列维

#### 4.2.1 概念说明

`pack_gqa` 的核心思想一句话概括：**别让共享同一 KV 的多个 Q 头各自为政，把它们「拼」进序列维，让一个工作块同时算这些 Q 头，KV 只搬一次。**

具体做法是把 Q（以及 O、LSE）的张量做一次**纯视图（view）变换**：把头维 `nheads` 拆成 `(qhead_per_kvhead, nheads_kv)`，再把内层 `qhead_per_kvhead` 与序列维 `seqlen_q` 合成一个**层级模式** `(qhead_per_kvhead, seqlen_q)`。

变换后的「逻辑序列长度」变成 `seqlen_q * qhead_per_kvhead`，而头维变成 `nheads_kv`。这样 tile scheduler 的 head 维直接是 KV 头数，每个工作块天然覆盖多个 Q 头，它们共享同一次 K/V 加载——KV 复用率立即提升 `qhead_per_kvhead` 倍。

> **关键点**：这是**纯 layout 变换，不搬运任何数据**。它只重新解释同一块显存的形状和步长，开销几乎为零。CuTe 张量 = 数据指针 + layout（形状与步长），换 layout 不换指针。

#### 4.2.2 核心流程

以 Q/O 张量（CuTe 里模式顺序为 `(seqlen_q, headdim, nheads, batch)`，头维在 index 2）为例：

```
输入：  (seqlen_q, headdim, nheads,            batch)
输出：  ((qhead_per_kvhead, seqlen_q), headdim, nheads_kv, batch)
```

步长相应改写：内层 `qhead_per_kvhead` 的步长 = 原来的 `head_stride`；新的 `nheads_kv` 步长 = `head_stride * qhead_per_kvhead`。这样逻辑行号 `idx` 到 `(头内偏移 h_idx, 序列位置 m_idx)` 的映射就是简单的 divmod：

\[
h\_idx = idx \bmod \text{qhead\_per\_kvhead}, \qquad m\_idx = idx \,//\, \text{qhead\_per\_kvhead}
\]

在 kernel 内部，凡是要从折叠后的「打包行号」还原出真实的 Q 头和序列位置，都用这个 divmod。

LSE 张量的模式顺序是 `(seqlen_q, nheads, batch)`，头维在 index 1，折叠规则完全对称（`head_idx=1`）。

#### 4.2.3 源码精读

折叠函数本身的实现非常短：

[pack_gqa.py:15-40](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/pack_gqa.py#L15-L40) —— `pack_gqa_layout`。注意它只是用 `cute.make_tensor(T.iterator, cute.make_layout(shape_packed, stride=stride_packed))` 重新包了一个 layout，`iterator`（数据指针）原封不动。`head_idx` 参数决定头维在第几模式：Q/O 用 `head_idx=2`，LSE 用 `head_idx=1`，从而同一个函数能复用于两类张量。

`unpack_gqa_layout` 是它的逆操作，形状相乘、步长还原回去：

[pack_gqa.py:86-112](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/pack_gqa.py#L86-L112) —— 逆变换，把 `qhead_per_kvhead` 从序列维展开回头维。

折叠动作发生在 kernel 把宿主张量转成 kernel 参数的 `to_underlying_arguments` / `__init__` 阶段（即「编译/缓存命中后、真正 launch 前」）。三代前向 kernel 在各自的同一位置做这件事：

- Hopper kernel：[flash_fwd_sm90.py:253-258](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm90.py#L253-L258) —— 当 `pack_gqa` 开启时，对 `mQ / mO / mLSE` 分别调用 `pack_gqa_layout` 重新解释。
- Blackwell kernel：[flash_fwd_sm100.py:553-558](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L553-L558) —— 同样的折叠。
- **Ampere 基线（SM80/SM120）**：[flash_fwd.py:676-681](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L676-L681) —— 本版本**新增**，从 `mK.shape[2]` 取 `nheads_kv`，对 `mQ / mO`（`head_idx=2`）与 `mLSE`（`head_idx=1`）折叠。

折叠后，kernel 看到的 `seqlen_q` 变成了打包后的值。宿主侧调度也同步用了这个「打包序列长」：

[interface.py:555-563](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L555-L563) —— `seqlen_q_packgqa = max_seqlen_q * qhead_per_kvhead`，并用它计算 `num_m_blocks`。也就是说「头折叠进序列」这件事在 host 调度层和 kernel 内部是**一致的**。

一个容易忽略的细节：**因果掩码在折叠后必须修正坐标**。折叠让逻辑序列变长，但因果边界要按「真实序列长度」算，不能跨 Q 头串扰。`BlockInfo` 用 `qhead_per_kvhead_packgqa` 把 `m_idx` 除回去再算因果 `n_idx`：

[block_info.py:31-46](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/block_info.py#L31-L46) —— `m_idx_max = ceil_div(m_idx_max, qhead_per_kvhead_packgqa)`、`m_idx_min = m_idx_min // qhead_per_kvhead_packgqa`。`AttentionMask` 构造时也传入同样的 `qhead_per_kvhead`（见 [flash_fwd.py:1006-1013](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L1006-L1013)），保证共享 KV 的多个 Q 头各自的因果掩码仍然正确。

#### 4.2.4 代码实践

**实践目标**：在纯 NumPy/Torch 层复现 `pack_gqa_layout` 的 divmod 映射，确认它只是重排索引。

**操作步骤**：

1. 取 `seqlen_q=4, qhead_per_kvhead=2`，逻辑打包序列长度 = 8。
2. 对打包行号 `idx = 0..7`，按 \(h\_idx = idx \bmod 2,\ m\_idx = idx // 2\) 还原。
3. 对照本节 divmod 公式，确认 `(h_idx, m_idx)` 序列。

**预期结果**：`idx: 0→(0,0), 1→(1,0), 2→(0,1), 3→(1,1), 4→(0,2), 5→(1,2), 6→(0,3), 7→(1,3)`。即「同一真实位置的两个 Q 头相邻排列」，这正是让一个 tile 同时覆盖多 Q 头的几何原因。

#### 4.2.5 小练习与答案

**练习 1**：`pack_gqa_layout` 会不会分配新显存、拷贝数据？

**答案**：不会。它只用原张量的 `iterator`（指针）配上一组新的 shape/stride 重新构造 CuTe 张量，是零拷贝的纯视图操作。

**练习 2**：为什么 Q/O 用 `head_idx=2` 而 LSE 用 `head_idx=1`？

**答案**：两类张量的模式顺序不同。Q/O 是 `(seqlen, headdim, nheads, batch)`，头维在 index 2；LSE 没有 headdim 维，是 `(seqlen, nheads, batch)`，头维在 index 1。`head_idx` 就是用来让同一个折叠函数适配这两种模式顺序的。

---

### 4.3 保持 TMA 维度不变与 kernel 内的 pack / cp.async 回退

#### 4.3.1 概念说明

4.2 的折叠带来一个棘手问题。在 Hopper/Blackwell 上，Q 的全局→共享搬运用的是 **TMA**（见 u5-l2、u6-l2），而 TMA 靠一个**固定维度的硬件描述符**工作。如果直接按折叠后的形状 `((qhead_per_kvhead, seqlen), headdim, nheads_kv, batch)` 建 TMA，那就是 **5 维 TMA**，和普通 MHA 的 4 维 TMA 不一样——意味着要为 pack_gqa 单独维护一套 TMA 描述符和编译产物，复杂且容易出 bug。

FA4 的解法很巧妙：**TMA 描述符仍按「nheads 折进 seqlen」的 4 维形状建，只在 kernel 里把得到的 tma_tensor 再 unpack 回折叠形状供寻址使用。** 这样硬件层面的 TMA 维度和普通 MHA 完全一致，只是软件层的坐标解释不同。源码注释把这话说得很直白。

但 TMA 这条路有个前提：**CTA tile 的序列维必须能被 `qhead_per_kvhead` 整除**（否则一个 tile 装不下整数个 Q 头，TMA 没法对齐）。当这个前提不满足，或与 SplitKV 等特性冲突时，kernel 会**回退到 `cp.async`**，用 `PackGQA` 类手算每个线程要读的 gmem 指针。

#### 4.3.2 核心流程

TMA 路径（`make_packgqa_tiled_tma_atom`）：

```
gmem_tensor:  (seqlen, d, nheads, b)              # 4 维
   │ layout_utils.select + group_modes
   ▼
              ((nheads, seqlen), d, b)             # 仍是 4 维，nheads 并入 seqlen
   │ 用 cta_tiler ((q, tile_m//q), tile_n) 建 TMA atom
   ▼
tma_atom + tma_tensor（4 维 TMA 描述符，与 MHA 同维度）
   │ 把 tma_tensor 再 unpack
   ▼
              ((qhead_per_kvhead, seqlen), d, nheads_kv, b)   # kernel 寻址用的折叠形状
```

注意建 TMA 时用的 `cta_tiler` 是 `((qhead_per_kvhead, cta_tiler[0] // qhead_per_kvhead), cta_tiler[1])`——把一个 CTA tile 在序列维上切成「`qhead_per_kvhead` 个 Q 头 × 若干真实行」，所以 `cta_tiler[0] % qhead_per_kvhead == 0` 是硬性要求。

cp.async 回退路径（`PackGQA.load_Q / store_O / store_LSE`）：

```
对打包行号 idx = block * tile_m + row：
    m_idx = idx // qhead_per_kvhead        # 真实序列位置
    h_idx = idx - m_idx * qhead_per_kvhead # 头内偏移
    ptr   = elem_pointer(tensor, ((h_idx, m_idx),))
用 shuffle_sync 把指针广播到同 warp 的相关线程，再 cute.copy
```

#### 4.3.3 源码精读

保持 TMA 维度的核心函数：

[pack_gqa.py:43-83](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/pack_gqa.py#L43-L83) —— `make_packgqa_tiled_tma_atom`。开头的注释直接点明意图：「keep the same TMA dimension as usual … If we instead pack directly to ((qhead_per_kvhead, seqlen), d, nheads_kv, b) we'd have 5D TMA」。第 60 行的断言 `cta_tiler[0] % qhead_per_kvhead == 0` 就是上面说的整除前提。

Hopper kernel 在建 Q 的 TMA copy 时，按 `pack_gqa` 开关选择用 `make_packgqa_tiled_tma_atom` 还是普通 `make_tiled_tma_atom`：

[flash_fwd_sm90.py:273-281](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm90.py#L273-L281) —— `partial(make_packgqa_tiled_tma_atom, ...) if pack_gqa else cpasync.make_tiled_tma_atom`，且传入 TMA 的是**未折叠**的 `mQ_og`（因为折叠由函数内部完成）。

TMA 能否用于 Q，取决于那个整除条件：

[flash_fwd_sm90.py:225-228](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm90.py#L225-L228) —— `use_tma_Q = arch >= sm_90 and not (pack_gqa and tile_m % qhead_per_kvhead != 0)`。Blackwell 同理：[flash_fwd_sm100.py:275](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L275)。不满足就只能走 cp.async 回退。

回退路径的指针计算在 `PackGQA.compute_ptr`：

[pack_gqa.py:115-140](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/pack_gqa.py#L115-L140) —— `m_idx = idx // qhead_per_kvhead`、`h_idx = idx - m_idx * qhead_per_kvhead`，再用 `utils.elem_pointer(tensor, ((h_idx, m_idx),))` 算出该线程负责行的 gmem 指针。这正是 4.2.2 的 divmod 公式落到代码里。

回退时的实际 load/store 三个方法：`load_Q`（[pack_gqa.py:142-185](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/pack_gqa.py#L142-L185)）、`store_LSE`（[pack_gqa.py:187-220](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/pack_gqa.py#L187-L220)）、`store_O`（[pack_gqa.py:222-263](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/pack_gqa.py#L222-L263)）。它们都是「先用 `compute_ptr` 算指针 → 用 `shuffle_sync` 在 warp 内广播 → 带越界谓词 `cute.copy`」的对称结构。**谁调用它们，是下一节 4.4 的重点**——本版本起 Ampere/SM120 在 prologue、epilogue 都会调用。

最后，`interface.py` 里有几处**强制关闭 `pack_gqa`** 的规则，理解它们就理解了 pack_gqa 的适用边界：

[interface.py:632-636](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L632-L636) —— 块稀疏且稀疏掩码的头维 ≠ 1（非广播）时关闭。

[interface.py:599-600](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L599-L600) —— 2CTA 指令要求 `tile_m % qhead_per_kvhead == 0 或不开 pack_gqa`。

[interface.py:908-909](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L908-L909) —— hd256 专用 2CTA kernel 暂不支持，关闭。

[interface.py:1502-1503](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L1502-L1503) —— **反向尚未支持**，反向路径里 `pack_gqa = False`（注释明确写 `pack_gqa backward not yet supported in bwd`）。

#### 4.3.4 代码实践

**实践目标**：搞清楚一次调用到底走了 TMA 路径还是 cp.async 回退路径。

**操作步骤**（阅读 + 推理型实践）：

1. 取本讲综合实践的 GQA 配置：`num_heads=8, nheads_kv=2 → qhead_per_kvhead=4`，`head_dim=64`。
2. 在 interface.py 里查 SM90 的 tile 选择：`head_dim=64` 时 `_tile_size_fwd_sm90` 给出的 `tile_m`（典型值 128，详见 u2-l2）。
3. 判断 `tile_m % qhead_per_kvhead == 0` 是否成立（128 % 4 == 0，成立）。
4. 据此推断：在 Hopper 上 Q 走 TMA 路径（`use_tma_Q=True`），不会进 `PackGQA.load_Q`。

**预期结果**：得到「该配置下 Q/O 走 TMA、`PackGQA` 的 cp.async 路径不会被触发」的结论。若把 `qhead_per_kvhead` 改成不能整除 `tile_m` 的值（例如 3），则会触发 cp.async 回退——但注意公共 API 的整除校验只管 `num_heads % num_heads_kv`，不管 `tile_m`，所以这种情形只有在特定 head_dim/tile 组合下才会发生。

#### 4.3.5 小练习与答案

**练习 1**：为什么 FA4 不直接为 pack_gqa 建 5 维 TMA 描述符？

**答案**：为了和普通 MHA 复用同一套 4 维 TMA 描述符结构，避免维护两套编译产物、降低复杂度。做法是 TMA 按「nheads 折进 seqlen」的 4 维形状建，再在 kernel 里把 tma_tensor unpack 回折叠形状寻址。

**练习 2**：什么条件下 `PackGQA.load_Q`（cp.async 回退）会被实际调用？

**答案**：当 `tile_m % qhead_per_kvhead != 0`（TMA 无法对齐到整数个 Q 头），或在 SM100 上 `pack_gqa` 与 SplitKV 同时启用等 TMA 不可用情形；以及——见 4.4——**在 Ampere(SM80)/SM120 上，Q 加载根本不用 TMA，prologue 直接走 `PackGQA.load_Q`**。此时 kernel 放弃 TMA，改用 `compute_ptr` 手算每线程 gmem 指针 + `shuffle_sync` 广播 + `cute.copy` 的 cp.async 路径。

---

### 4.4 SM80/SM120 前向 kernel 的 pack_gqa 完整路径（本版本新增）

#### 4.4.1 概念说明

前两节讲的「折叠 + TMA/cp.async 回退」是 Hopper/Blackwell 的玩法。**本版本之前，Ampere(SM80) 基线 kernel 对 pack_gqa 只做了「半套」**：epilogue 里调用 `PackGQA.store_O` / `store_LSE` 把折叠后的输出写回，但 **Q 的加载仍然走传统 `head_idx // qhead_per_kvhead` 路径**，`__init__` 也没有对 Q/O/LSE 做折叠视图。换句话说，旧版 Ampere 的 pack_gqa 只在「写回端」生效，读 Q 端和主循环并没有真正享受到「一个 tile 覆盖多 Q 头」的好处。

**本版本（1f7ce2f..5835c73）补齐了这半套**：Ampere 基线 `FlashAttentionForwardSm80` 现在和 Hopper/Blackwell 一样，在 `__init__` 里折叠 Q/O/LSE、在 prologue 用 `PackGQA.load_Q` 加载 Q、主循环不变、epilogue 用 `PackGQA.store_O/store_LSE` 写回，**形成完整的 pack_gqa 代码路径**。

更重要的是 **SM120 直接继承 SM80**（`FlashAttentionForwardSm120` 是 `FlashAttentionForwardSm80` 的子类），因此 SM120 也一并获得了完整的 pack_gqa 支持。这正是本次新增的两个 PR 之一（[NVIDIA][CuTe,Fwd,sm120] Implement Pack-GQA on SM120）的核心内容。

> **为什么 SM120 要复用 SM80 的 cp.async 路径？** SM120（Ada/Blackwell 消费级）虽然有 TMA 硬件，但当前 DSL 在 SM120 上的 TMA 支持还不完整，且输出回写的 TMA bulk store 在 SM120 上不可靠。所以 SM120 子类在 `__init__` 里把 `arch` 强制设回 `sm_80`，让整条前向（包括 pack_gqa）走 Ampere 的 cp.async 代码路径。

#### 4.4.2 核心流程

SM80/SM120 前向 kernel 在 pack_gqa 开启时的完整数据流：

```
__init__ 阶段（to_underlying_arguments）：
   mQ, mO  ← pack_gqa_layout(..., head_idx=2)     # 头折进序列
   mLSE    ← pack_gqa_layout(..., head_idx=1)
   seqlen_q_static ← mQ.shape[0][1]               # 折叠后形状是 ((), ...)，取 [0][1] 拿真实 seqlen
   use_tma_O ← (sm_90 ≤ arch < sm_120)            # Ampere(80) 与 SM120(120) 都为 False → cp.async 输出

prologue（加载 Q）：
   if pack_gqa: pack_gqa.load_Q(mQ_cur, sQ, ...)   # PackGQA 用 divmod 手算指针，cp.async 搬 Q
   else:        self.load_Q(gQ, sQ, ...)           # 传统 local_tile 路径

mainloop：
   与非 pack_gqa 完全相同（Q 已折叠进序列，K/V 一次加载被多 Q 头复用）

epilogue（写回 O/LSE）：
   use_tma_O=False 分支：
      if pack_gqa: pack_gqa.store_O(...) / store_LSE(...)   # PackGQA 手算指针写回
      else:        普通 gmem_tiled_copy 写回
```

注意三处与 Hopper/Blackwell 的差异：(1) SM80/SM120 **从不**对 Q 用 TMA（Ampere 无 TMA，SM120 被强制成 Ampere），所以 prologue 一定是 `PackGQA.load_Q`；(2) 输出回写 `use_tma_O` 为 False，走 cp.async；(3) 折叠后 `seqlen_q_static` 要从层级形状 `mQ.shape[0]` 再下钻一层取 `mQ.shape[0][1]`。

#### 4.4.3 源码精读

**① `__init__` 里的折叠（本版本新增）**：

[flash_fwd.py:676-681](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L676-L681) —— 当 `self.pack_gqa` 为真时，从 `mK.shape[2]` 取 `nheads_kv`，对 `mQ / mO` 用 `head_idx=2`、对 `mLSE` 用 `head_idx=1` 调用 `pack_gqa_layout`。这与 Hopper [flash_fwd_sm90.py:253-258](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm90.py#L253-L258) 完全对齐。折叠后 `mQ.shape[0]` 变成 `(qhead_per_kvhead, seqlen_q)` 这个层级模式，后续凡是用到真实 `seqlen_q` 的地方都要取 `mQ.shape[0][1]`。

**② `use_tma_O` 的架构边界**：

[flash_fwd.py:658](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L658) —— `self.use_tma_O = Arch.sm_90 <= self.arch < Arch.sm_120`。Ampere(80) 与 SM120(被强制成 80) 都不满足，输出回写走 cp.async 分支，这正是 epilogue 里 `PackGQA.store_O` 能被调用的大前提。

**③ prologue 加载 Q 的分支（本版本新增）**：

[flash_fwd.py:963-967](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L963-L967) —— `if not pack_gqa` 走传统 `self.load_Q(gQ, ...)`；否则构造 `PackGQA(...)` 并调用 `pack_gqa.load_Q(mQ_cur, sQ, gmem_tiled_copy_Q, tidx, m_block, seqlen.seqlen_q)`。注意传给 `load_Q` 的是折叠后的整张 `mQ_cur`（而非某个 `local_tile` 切片），因为 `PackGQA.compute_ptr` 内部自己用 `block * m_block_size` 算偏移。

**④ 头维索引与 seqlen 的折叠适配**：

[flash_fwd.py:822](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L822) —— `num_head_kv = num_head if pack_gqa else num_head // qhead_per_kvhead`。折叠后 kernel 的 head 维已是 KV 头数，直接用 `num_head`。

[flash_fwd.py:802](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L802) —— `seqlen_q_static = mQ.shape[0][1] if pack_gqa else mQ.shape[0]`。折叠后真实 `seqlen_q` 藏在层级模式的第二层。

**⑤ epilogue 写回 O/LSE（已存在，本版本起成为完整路径的一环）**：

[flash_fwd.py:363-365](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L363-L365) —— 构造 `PackGQA` 实例（用于 epilogue）。

[flash_fwd.py:390](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L390) —— `pack_gqa.store_LSE(mLSE_cur, lse, tiled_mma, tidx, m_block, seqlen.seqlen_q)`（`pack_gqa` 分支）。

[flash_fwd.py:449](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L449) —— `pack_gqa.store_O(mO_cur, tOrO, gmem_tiled_copy_O, tidx, m_block, seqlen.seqlen_q)`（`pack_gqa` 分支，且只在 `use_tma_O=False` 的 cp.async epilogue 里）。

**⑥ SM120 继承并复用 cp.async 路径**：

[flash_fwd_sm120.py:13-17](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm120.py#L13-L17) —— `FlashAttentionForwardSm120.__init__` 调用 `super().__init__(...)` 后把 `self.arch = Arch.sm_80`。于是上面 ①~⑤ 的整条 cp.async pack_gqa 路径在 SM120 上原样生效，无需为 SM120 单独写一份。

#### 4.4.4 代码实践

**实践目标**：在 SM80 或 SM120 上确认 `pack_gqa=True` 走的是新的完整路径（`PackGQA.load_Q` + 折叠），且与 `pack_gqa=False` 数值一致。

**操作步骤**（推理 + 可选运行型实践）：

1. 读 [flash_fwd.py:963-967](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py#L963-L967)，确认 `pack_gqa=True` 时 prologue 调用的是 `PackGQA.load_Q` 而非传统 `self.load_Q`。
2. 读 [flash_fwd_sm120.py:13-17](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm120.py#L13-L17)，确认 SM120 通过 `self.arch = Arch.sm_80` 复用 SM80 的 cp.async 路径（含 pack_gqa）。
3. 若手头有 SM80/SM120 GPU，运行本讲第 5 节的综合实践脚本，用 `FLASH_ATTENTION_ARCH=sm_80`（或在真 SM120 卡上）分别跑 `pack_gqa=True/False`。

**需要观察的现象与预期结果**：

1. 在 SM80/SM120 上，`pack_gqa=True` 不再「仅 epilogue 生效」，Q 加载也走 `PackGQA.load_Q`（可结合 `CUTE_DSL_KEEP_PTX=1` 导出的 PTX 观察到 `ldmatrix`/`cp.async` 的 Q 加载模式变化——**待本地验证**）。
2. 两种开关下输出 O 与 LSE 的 `max diff` 应在 fp16/bf16 舍入量级，证明新路径数学等价。

> 若无法确定运行结果，明确标注「待本地验证」。本环境无 GPU，不能给出实测 PTX 或耗时。

#### 4.4.5 小练习与答案

**练习 1**：为什么 SM120 子类要把 `self.arch` 设回 `sm_80`，而不是保持 `sm_120`？

**答案**：为了让 SM120 复用 Ampere 的 cp.async 代码路径（包括 Q 加载和 cp.async 输出回写）。SM120 的 TMA 输出回写在当前 DSL 下不可靠，强制成 `sm_80` 后 `use_tma_O`、`use_tma_Q` 等开关自动落到 cp.async 分支，pack_gqa 也跟着走 `PackGQA` 的完整路径，无需为 SM120 维护单独实现。

**练习 2**：旧版 Ampere 的 pack_gqa「只做半套」具体指什么？本版本补上了哪半套？

**答案**：旧版只在 epilogue 用 `PackGQA.store_O/store_LSE` 写回折叠后的输出，但 `__init__` 没折叠 Q/O/LSE、prologue 仍用传统 `head_idx // qhead_per_kvhead` 加载 Q。本版本补上了 `__init__` 的 `pack_gqa_layout` 折叠（Q/O/LSE）和 prologue 的 `PackGQA.load_Q`，使 Q 加载端也享受到「一个 tile 覆盖多 Q 头」、KV 只搬一次的好处。

---

## 5. 综合实践

把四个模块串起来，完成规格里要求的核心实践：**用同一份 GQA 输入，对比 `pack_gqa=True` 与 `pack_gqa=False`，验证数值一致并测耗时差异。**

```python
# 示例代码：需安装 flash-attn-4 且有 Hopper/Blackwell/SM120 GPU
import torch
from flash_attn.cute import flash_attn_func

torch.manual_seed(0)
device = "cuda"
dtype = torch.float16

# GQA 配置：8 个 Q 头共享 2 个 KV 头 → qhead_per_kvhead = 4
batch, seqlen, num_heads, nheads_kv, head_dim = 2, 1024, 8, 2, 64
assert num_heads % nheads_kv == 0

q = torch.randn(batch, seqlen, num_heads, head_dim, device=device, dtype=dtype)
k = torch.randn(batch, seqlen, nheads_kv, head_dim, device=device, dtype=dtype)
v = torch.randn(batch, seqlen, nheads_kv, head_dim, device=device, dtype=dtype)

# 1) 数值一致性：两次用同一份 q/k/v，只切换 pack_gqa
out_pack, lse_pack = flash_attn_func(q, k, v, causal=True, pack_gqa=True)
out_nopack, lse_nopack = flash_attn_func(q, k, v, causal=True, pack_gqa=False)

max_diff = (out_pack - out_nopack).abs().max().item()
print(f"O  max diff (pack vs no-pack): {max_diff:.3e}")     # 期望在 fp16 舍入量级（如 < 1e-2）
print(f"LSE max diff: {(lse_pack - lse_nopack).abs().max().item():.3e}")

# 2) 性能对比：注意首次调用会 JIT 编译，要先 warmup
for pg in (True, False):
    flash_attn_func(q, k, v, causal=True, pack_gqa=pg)     # warmup（触发并缓存编译）
torch.cuda.synchronize()

import time
def bench(pg, repeats=50):
    flash_attn_func(q, k, v, causal=True, pack_gqa=pg)     # 再 warmup 一次
    torch.cuda.synchronize()
    t0 = time.perf_counter()
    for _ in range(repeats):
        flash_attn_func(q, k, v, causal=True, pack_gqa=pg)
    torch.cuda.synchronize()
    return (time.perf_counter() - t0) / repeats * 1000      # ms / 次

print(f"pack_gqa=True  : {bench(True):.3f} ms / call")
print(f"pack_gqa=False : {bench(False):.3f} ms / call")
```

**需要观察的现象与预期结果**：

1. `max_diff` 应在 fp16 舍入量级（远小于 1），证明 pack_gqa 只改实现不改数学。
2. `lse_pack` 与 `lse_nopack` 形状都是 `(batch, num_heads, seqlen_q)` 且 float32——pack_gqa 在 kernel 内部对 LSE 做了折叠视图，但写回的是同一块用户内存，所以**对用户暴露的 LSE 形状不变**。
3. 性能：`pack_gqa=True` 通常**更快**，因为同一 KV 块被多个 Q 头复用、HBM 读 KV 的次数下降。具体加速比随 `qhead_per_kvhead`、`seqlen`、硬件而变。**确切加速数字待本地验证**（本环境无 GPU，无法给出实测值）。
4. **若在 SM80/SM120 上运行**：结合 4.4 的分析，`pack_gqa=True` 现在走的是「`__init__` 折叠 + `PackGQA.load_Q` + `PackGQA.store_O/store_LSE`」的完整 cp.async 路径，而不再回退到「只在 epilogue 生效」的旧实现。可用 `FLASH_ATTENTION_ARCH=sm_80` 在任意支持卡上强制验证这条路径。

> 若运行时遇到 OOM，用 `CUDA_VISIBLE_DEVICES` 选一块空闲 GPU（见 CLAUDE.md 的提示）。若想观察「是否走 TMA vs cp.async」，可结合 4.3.4 / 4.4.4 的推理：Hopper 上 `tile_m=128, qhead_per_kvhead=4` 整除 → Q 走 TMA；SM80/SM120 上 Q 一定走 `PackGQA.load_Q`（cp.async）。

---

## 6. 本讲小结

- **GQA/MQA** 让多个 Q 头共享一组 KV 头，比例 `qhead_per_kvhead = num_heads / num_heads_kv` 必须整除；它省的是 KV cache 体积与带宽。
- 朴素实现按 Q 头切分工作块会让同一 KV 被重复加载 `qhead_per_kvhead` 次，这正是 `pack_gqa` 要解决的低效。
- `pack_gqa_layout` 是一次**零拷贝的纯 layout 视图变换**：把 `qhead_per_kvhead` 个 Q 头折叠进序列维，逻辑序列长变成 `seqlen_q * qhead_per_kvhead`，让一个工作块覆盖多 Q 头、KV 只搬一次。
- 折叠后因果/滑窗掩码要按真实序列长度算，`BlockInfo` 与 `AttentionMask` 都用 `qhead_per_kvhead` 把打包行号除回去做坐标修正。
- TMA 路径靠 `make_packgqa_tiled_tma_atom` **保持 4 维 TMA 描述符不变**（先把 nheads 并进 seqlen 建 TMA，再 unpack 回折叠形状），前提是 `tile_m % qhead_per_kvhead == 0`。
- 不满足整除或与 SplitKV/hd256/块稀疏/反向等冲突时，回退到 `PackGQA` 的 `cp.async` 路径（`compute_ptr` 用 divmod 手算每线程 gmem 指针）。
- **本版本起 Ampere(SM80)/SM120 前向基线也完整支持 pack_gqa**：`__init__` 折叠 Q/O/LSE、prologue 用 `PackGQA.load_Q`、epilogue 用 `PackGQA.store_O/store_LSE`；SM120 通过把 `arch` 强制为 `sm_80` 复用整条 cp.async 路径。反向目前不支持 pack_gqa。

## 7. 下一步学习建议

- **u7-l2 SplitKV 与 Combine Kernel**：长上下文下 KV 维的另一种「省/拆」策略，与 pack_gqa（折叠 Q 头维）正交，可对比学习。注意本版本起 SM120 前向显式断言 `num_splits==1`（interface.py:567-568），与 SM90 一样不支持 SplitKV。
- **u7-l3 Paged KV Cache**：分页 KV 同样服务于解码场景，理解它与 pack_gqa 如何在 SM100 kernel 内共存。
- **延伸阅读**：直接对照 [flash_fwd.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd.py) 的 `__init__`（约 658-711 行，含 pack_gqa 折叠与 `use_tma_O` 边界）与 prologue/epilogue（约 363-449、963-967 行），看 SM80/SM120 如何在一个 kernel 里把「折叠 + `PackGQA` 加载/存储」串成完整路径；再对比 [flash_fwd_sm90.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm90.py) 的 `to_underlying_arguments`（约 250-360 行），理解 TMA 与 cp.async 两条 pack_gqa 实现的取舍。
