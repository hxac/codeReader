# score_mod：可编程打分修改

## 1. 本讲目标

本讲承接 [u4-l1 在线 Softmax 数值核心](u4-l1-online-softmax.md)。在上一讲里，分数 `S = QKᵀ` 直接进入 online softmax 的 `exp2` 与重缩放流程。本讲要回答一个问题：**如果我想在 softmax 之前对分数做一点手脚**（比如给远处的 key 降权、把分数夹在有界区间、加上一个学习到的偏置），FA4 该怎么做？

学完后你应当能够：

- 说清 `score_mod` 是什么、它的回调签名长什么样、每个参数从哪来。
- 理解 `call_score_mod` / `apply_score_mod_inner` 如何在**编译期**把用户回调内联进 kernel。
- 知道 `softcap` / `ALiBi` / 相对位置偏置这三种典型修改的数学与源码实现。
- 掌握 `aux_tensors` / `aux_scalars` 两种把运行期数据喂进回调的扩展机制。
- 自己写一个 `score_mod` 并与 PyTorch 参考实现对拍。

## 2. 前置知识

- **注意力分数**：`S = QKᵀ/√d`，形状概念上是 `(q_len, k_len)` 的矩阵，softmax 按行归一化后再乘 `V`。
- **`@cute.jit`**：FA4（CuTeDSL）里用 `@cute.jit` 装饰的函数是**编译期**函数。它不是在 Python 解释器里跑，而是被翻译成 MLIR → PTX → CUBIN。所以你写的 `score_mod` 本质是一段会被“缝合”进 kernel 的代码，而不是运行期回调。
- **`cutlass.Constexpr`**：编译期常量。`score_mod` 函数对象本身以 `Constexpr` 形式传入，意味着换一个 `score_mod` 就会**重新编译**一个新 kernel（这一点在 [u4-l1](u4-l1-online-softmax.md) 和 [u2-l1](u2-l1-public-api.md) 里讨论 `compile_key` 时已埋下伏笔）。
- **SSA / `TensorSSA`**：CuTeDSL 里寄存器张量的“值形态”。`.load()` 把可写寄存器张量变成只读 SSA 值，`cute.TensorSSA(...)` 则把一个裸运算结果包回张量。你会在示例里反复看到这对操作。
- **`exp2` 与 `scale_log2`**：上一讲建立的换底技巧。本讲会解释为什么有 `score_mod` 时，缩放因子的“身份”会发生变化。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [flash_attn/cute/softmax.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/softmax.py) | 定义 `call_score_mod`、`call_score_mod_bwd`，以及真正驱动逐元素修改的 `apply_score_mod_inner` / `apply_score_mod_bwd_inner`。 |
| [flash_attn/cute/utils.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/utils.py) | `AuxData` 数据结构、内置 `softcap` 工厂 `create_softcap_scoremod`、缩放分发 `compute_softmax_scale_log2`、缓存键哈希 `hash_callable`。 |
| [flash_attn/cute/interface.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py) | 公共 API `flash_attn_func`，把 `softcap` 折叠成 `score_mod`，并对用户 `score_mod` 做架构校验与哈希。 |
| [flash_attn/cute/flash_fwd.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py) | 前向主循环里调用 `apply_score_mod` 的位置，演示“何时、用哪些坐标”调用回调。 |
| [tests/cute/score_mod_definitions.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/tests/cute/score_mod_definitions.py) | 一整套示例 `score_mod`（`@cute.jit`）与对应的 PyT eager 参考实现，是学习回调写法的最佳模板。 |

---

## 4. 核心概念与源码讲解

### 4.1 score_mod 概念：在 softmax 之前改写分数

#### 4.1.1 概念说明

标准注意力里，每个 `(q, k)` 对的分数是 `S = QKᵀ/√d`，随后 `softmax(S)V`。但很多模型想在这个 `S` 上动刀：

- **ALiBi**：`S' = S − slope_h · |q_idx − kv_idx|`，让远处 key 的注意力被距离惩罚，且每个头有几何递减的斜率。
- **softcap**（Grok/Gemma 用）：`S' = cap · tanh(S/cap)`，把分数夹在 `[-cap, cap]`，防止单个分数爆炸主导整行。
- **相对/绝对位置偏置**：`S' = S + bias(q_idx, kv_idx)`，给每个位置对加一个查表偏置。
- **滑窗、块对角**：把不该看的位置直接置 `-inf`（这其实更像 `mask_mod`，但用 `score_mod` 也能表达）。

这些都共享同一个抽象：**给定一个分数和它的坐标，输出一个新分数**。FA4 把这件事抽象成 `score_mod`——一个用户写的、固定签名的 `@cute.jit` 函数。

> 术语澄清：FA4 里有两类用户回调——`score_mod` 改**数值**（输入输出都是浮点分数），`mask_mod` 改**可见性**（输出布尔，决定要不要置 `-inf`）。本讲只讲 `score_mod`。两者互不冲突，可以同时用；但 `score_mod` 与 `causal=True`/`window_size` 是互斥的（这些会走内置掩码路径），这点在 [u3-l1](u3-l1-attention-mask.md) 已有交代。

#### 4.1.2 核心流程

从“用户写函数”到“kernel 里多出几条指令”，链路如下：

```text
用户 @cute.jit score_mod
        │  作为 Constexpr 传入 kernel 类（编译期常量）
        ▼
apply_score_mod_inner   ← 取出当前 tile 的分数向量与坐标向量
        │  先把分数乘 softmax_scale，再分块（vec_size）送进回调
        ▼
call_score_mod          ← 统一适配新旧两套签名（带/不带 aux_scalars）
        │  内联调用用户的 score_mod(...)
        ▼
写回 acc_S              ← 修改后的分数覆盖累加器，之后才进 online softmax
```

关键点：**`score_mod` 作用在 softmax 之前**。它修改的是 `acc_S`（分数累加器），改完之后才轮到 [u4-l1](u4-l1-online-softmax.md) 里的 `online_softmax` 做 `row_max`/`exp2`/`rescale`。所以 `score_mod` 可以安全地返回 `-inf`（等价于掩码）、返回夹紧值（softcap）、返回加偏置值——online softmax 都能正确处理。

#### 4.1.3 源码精读

先看公共 API 暴露的入口。`flash_attn_func` 把 `softcap`、`score_mod`、`aux_tensors`、`aux_scalars` 都作为可选参数透传：

参数签名见 [flash_attn/cute/interface.py:2709-2731](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L2709-L2731) —— 注意 `softcap: float = 0.0`、`score_mod`、`score_mod_bwd`、`aux_tensors`、`aux_scalars` 同时存在。

在 `_flash_attn_fwd` 内部，`softcap` 与 `score_mod` 二选一，且对老架构（SM8x）禁用用户自定义 `score_mod`，并计算用于缓存键的哈希：

[flash_attn/cute/interface.py:605-614](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L605-L614) 说明：`softcap` 非空时调用 `utils.create_softcap_scoremod(softcap)` 生成一个等价 `score_mod`；`score_mod_hash = utils.hash_callable(score_mod)` 进入 `compile_key`——这正是“换 score_mod 就重编译”的根源。

#### 4.1.4 代码实践

**实践目标**：建立“`softcap` 只是 `score_mod` 的语法糖”的直觉。

**操作步骤**：

1. 打开 [flash_attn/cute/utils.py:159-167](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/utils.py#L159-L167)，读 `create_softcap_scoremod` 的 5 行实现。
2. 打开 [flash_attn/cute/interface.py:605-607](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L605-L607)，确认 `softcap` 被转成 `score_mod`。

**需要观察的现象**：你会发现 `softcap` 没有独立代码路径，它只是工厂函数生成的回调。

**预期结果**：能口头复述“`flash_attn_func(q,k,v,softcap=0.5)` 在内部等价于 `flash_attn_func(q,k,v,score_mod=create_softcap_scoremod(0.5))`”。（运行验证见第 5 节综合实践。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `score_mod` 必须在 softmax **之前**生效，而不能在之后？

> **答案**：softmax 之后已经是概率（行和为 1），再修改会破坏归一化，且无法表达 `-inf` 掩码。`score_mod` 改的是分数 `S`，online softmax 拿到改过的 `S` 再统一求 `row_max`/归一化，数学上仍是合法的精确注意力。

**练习 2**：`softcap` 与用户传入的 `score_mod` 能否同时使用？

> **答案**：不能。interface.py 第 606 行有断言 `assert score_mod is None`，二者互斥。要用 softcap 又想加别的修改，请自己写一个组合了 tanh 夹紧与其他逻辑的 `score_mod`。

---

### 4.2 回调签名与 call_score_mod 注入机制

#### 4.2.1 概念说明

`score_mod` 的核心是它的**固定签名**。无论你做什么修改，函数长这样：

```python
@cute.jit
def my_score_mod(score, batch_idx, head_idx, q_idx, kv_idx, seqlen_info, aux_tensors):
    return ...  # 返回修改后的 score
```

七个参数的来源与含义：

| 参数 | 含义 | 形态 |
| --- | --- | --- |
| `score` | 当前 `(q,k)` 对的分数（**已乘 softmax_scale**） | `TensorSSA`，长度为 `vec_size` |
| `batch_idx` | 当前 batch 索引 | `Int32` 标量或向量 |
| `head_idx` | 当前 head 索引 | `Int32` |
| `q_idx` | query 的逻辑位置索引 | `Int32` 向量 |
| `kv_idx` | key/value 的逻辑位置索引 | `Int32` 向量 |
| `seqlen_info` | 序列长度与偏移（varlen 用，见 [u3-l3](u3-l3-seqlen-info-varlen.md)） | `SeqlenInfoQK` 对象 |
| `aux_tensors` | 用户传入的额外张量元组（偏置表等），可能为 `()` | tuple，元素为 cute tensor |

> 关于“逻辑位置”：默认 `q_idx`/`kv_idx` 是**每条序列内**从 0 开始的逻辑索引（因果掩码 `kv_idx ≤ q_idx` 就基于此）。如果需要跨序列的全局位置，要从 `seqlen_info.offset_q`/`offset_k` 自己加（见 4.4 节的 global 变体）。

#### 4.2.2 核心流程

注入分三层：

1. **`apply_score_mod_inner`**：从累加器 `acc_S` 与坐标张量 `index_tensor` 里，按 `vec_size` 抽出一组分数与坐标；先把分数乘 `softmax_scale`；处理 Pack-GQA 的 head 偏移与越界回绕（`fastdiv_mods`）；最后调用 `call_score_mod`。
2. **`call_score_mod`**：一个薄适配层，兼容新旧两套签名（带不带 `aux_scalars`），然后调用用户 `score_mod`。
3. **用户 `score_mod`**：拿到 `(score, b, h, q_idx, kv_idx, seqlen_info, aux_tensors)`，返回新分数。

伪代码（省略 Pack-GQA/回绕细节）：

```text
for each vec_size block of the tile:
    score_vec   = acc_S[block] * softmax_scale          # 先缩放
    kv_idx_vec  = index_tensor[block].kv_idx            # 坐标
    q_idx_vec   = index_tensor[block].q_idx
    new_score   = call_score_mod(score_mod, score_vec, b, h,
                                 q_idx_vec, kv_idx_vec, seqlen_info, aux_data)
    acc_S[block] = new_score                            # 写回
# 之后 acc_S 才进入 online_softmax
```

#### 4.2.3 源码精读

**用户签名的统一入口** `call_score_mod`，见 [flash_attn/cute/softmax.py:19-51](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/softmax.py#L19-L51)。注意三点：

- `score_mod` 形参标注为 `cutlass.Constexpr`，说明它是**编译期**注入的，整个调用会被内联。
- 第 30 行 `aux_tensors = aux_data.tensors if aux_data.tensors is not None else ()`：保证回调总能拿到一个可索引的元组（没有辅助张量时是空元组）。
- 第 32–42 行的“兼容垫片”：如果 `aux_data.scalars` 非空，就用带 `aux_scalars=...` 的 8 参数签名调用，否则用 7 参数签名。这样老版（无 `aux_scalars`）的 `score_mod` 仍能工作。反向版本 `call_score_mod_bwd` 结构完全对称，见 [flash_attn/cute/softmax.py:54-89](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/softmax.py#L54-L89)。

**真正的驱动函数** `apply_score_mod_inner`，见 [flash_attn/cute/softmax.py:453-578](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/softmax.py#L453-L578)。其中两段最关键：

- 第 518 行 `score_vec[j] = score_tensor[i + j] * softmax_scale`：**缩放发生在回调之前**。用户拿到的 `score` 已经是 `QKᵀ · softmax_scale`。这对理解 softcap 的 `cap` 量纲很重要。
- 第 564–573 行调用 `call_score_mod(...)`，把组装好的 SSA 向量交给用户回调；第 576–578 行把结果写回 `score_tensor`（即 `acc_S`）。

**前向主循环里的调用点**，见 [flash_attn/cute/flash_fwd.py:1147-1156](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L1147-L1156)：

```python
if const_expr(score_mod is not None):
    self.apply_score_mod(mma_params.thr_mma_qk, batch_idx, head_idx,
                         m_block, acc_S, n_block,
                         softmax_scale=softmax.softmax_scale, seqlen=seqlen, ...)
```

`const_expr(score_mod is not None)` 是编译期判断：没有 `score_mod` 时整段代码会被裁掉，零开销。`apply_score_mod` 方法本身负责用 MMA 的 `partition_C` 把当前 tile 的坐标 `cS` 切成线程私有片段 `tScS`，再交给 `apply_score_mod_inner`，见 [flash_attn/cute/flash_fwd.py:1195-1228](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L1195-L1228)。

#### 4.2.4 代码实践

**实践目标**：跟踪一次 `score_mod` 调用的完整数据流，确认“坐标从哪来、缩放在哪做、结果写到哪”。

**操作步骤**：

1. 在 `flash_fwd.py` 的 `FlashAttentionForwardSm80.compute_one_n_block` 里定位 QK 的 MMA 完成（产生 `acc_S`）之后、PV 的 MMA 之前的位置——那就是 `apply_score_mod` 的插入点（约 [flash_fwd.py:1147](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L1147)）。
2. 顺着调用进入 `apply_score_mod_inner`，确认第 518 行的缩放与第 564 行的回调调用。
3. 画出数据流：`acc_S(gmem 概念) → score_vec(rmem) → ×softmax_scale → call_score_mod → 写回 acc_S → online_softmax`。

**需要观察的现象**：确认 `score_mod` 修改的是 `acc_S`，且发生在 `online_softmax` **之前**。

**预期结果**：得到一张明确标注“缩放 → 回调 → softmax”顺序的草图。无需运行，纯源码阅读。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `score_mod` 形参声明为 `cutlass.Constexpr`，而不是普通 Python 参数？

> **答案**：`Constexpr` 使其在编译期固定，编译器能把用户函数**内联**进 kernel 并据此特化（比如裁掉 `aux_tensors` 为空的分支）。代价是换一个 `score_mod` 就触发重编译——这正是它进入 `compile_key` 的原因。

**练习 2**：用户在 `score_mod` 里收到的 `score` 是 `QKᵀ` 还是 `QKᵀ/√d`？

> **答案**：是 `QKᵀ · softmax_scale`，即已缩放（见 softmax.py:518）。所以 softcap 的 `cap`、ALiBi 的 `slope` 都是在“缩放后的分数”这一量纲下设定的。

---

### 4.3 softmax_scale 的双重身份（有/无 score_mod）

#### 4.3.1 概念说明

这个细节极易踩坑，且与 [u4-l1](u4-l1-online-softmax.md) 的 `scale_log2` 直接相关。回顾上一讲：没有 `score_mod` 时，缩放因子被折叠进 `scale_log2 = softmax_scale · log₂e`，在 `exp2` 那一步一次性完成“缩放 + 换底”。

但有 `score_mod` 时这条路走不通：用户回调需要看到**已缩放**的分数（否则 softcap 的 `cap`、ALiBi 的 `slope` 都失去确定的量纲）。于是 FA4 把缩放“提前”到回调之前，`scale_log2` 退化为纯粹的换底常数 `log₂e`。

#### 4.3.2 核心流程

\[
\begin{aligned}
\text{无 score\_mod:}&\quad \text{softmax\_scale\_log2} = \text{softmax\_scale}\cdot\log_2 e,\quad \text{softmax\_scale}:=\text{None} \\
\text{有 score\_mod:}&\quad \text{softmax\_scale\_log2} = \log_2 e,\quad \text{softmax\_scale}\text{ 保留，回调前乘到 score 上}
\end{aligned}
\]

两种情况下最终数学结果完全一致（误差仅来自浮点舍入），区别只在“缩放这一刀切在哪里”。

#### 4.3.3 源码精读

分发逻辑见 [flash_attn/cute/utils.py:185-197](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/utils.py#L185-L197)：

```python
def compute_softmax_scale_log2(softmax_scale, score_mod):
    if const_expr(score_mod is None):
        return softmax_scale * LOG2_E, None        # 折叠，scale 置 None
    else:
        return LOG2_E, softmax_scale               # 只换底，scale 留给回调前用
```

前向 kernel 在构造 `Softmax` 时调用它（[flash_fwd.py:705](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L705)），得到的 `softmax_scale`（可能为 `None`）随后透传给 `apply_score_mod`，并在 `apply_score_mod_inner` 第 518 行乘到分数上。

#### 4.3.4 代码实践

**实践目标**：验证“缩放时机不同、结果相同”。

**操作步骤**：

1. 阅读上述 `compute_softmax_scale_log2`，确认两条分支。
2. 在 `apply_score_mod_inner` 第 518 行确认 `* softmax_scale` 只在有 `score_mod` 时实际生效（无 `score_mod` 时这段代码整体被 `const_expr` 裁掉，且 `softmax_scale` 为 `None` 也意味着不会走到这里）。

**需要观察的现象**：无 `score_mod` 路径里，`online_softmax` 用 `acc_S * scale_log2`；有 `score_mod` 路径里，分数在回调前已乘 `softmax_scale`，`online_softmax` 只用 `log₂e` 换底。

**预期结果**：理解为什么两条路径数学等价。待本地验证：可对比 `flash_attn_func(...,score_mod=identity)` 与不传 `score_mod` 的输出是否一致（identity 回调见 [score_mod_definitions.py:13-15](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/tests/cute/score_mod_definitions.py#L13-L15)）。

#### 4.3.5 小练习与答案

**练习**：如果用户写了一个 `score_mod` 只返回 `score` 不变（identity），最终结果会和不开 `score_mod` 完全 bit-级一致吗？

> **答案**：不一定 bit 一致。因为缩放时机变了（回调前乘 `softmax_scale` vs 在 `exp2` 里乘 `scale_log2`），浮点结合顺序不同会引入微小差异，但都在 fp16/bf16 舍入误差范围内，数学上等价。

---

### 4.4 三大实例：softcap、ALiBi、相对位置偏置

#### 4.4.1 概念说明

这一节用三个经典例子把签名用熟。

**softcap**：把分数夹紧到 `[-cap, cap]`，导数有界，防止某个分数过大主导整行（注意力“饱和”）。

\[ S' = \text{cap}\cdot\tanh(S/\text{cap}) \]

**ALiBi**（Attention with Linear Biases）：不引入位置编码，而是在分数上减一个随距离线性增长、随 head 几何递减的惩罚。

\[ S' = S - \text{slope}_h\cdot|q\_idx - kv\_idx|,\qquad \text{slope}_h = 2^{-(h+1)} \]

**相对位置偏置**：给分数加上一个只依赖相对距离的项（T5/相对位置编码的简化版）。

\[ S' = S + |q\_idx - kv\_idx| \]

#### 4.4.2 核心流程

三者写法高度一致：拿到 `score` 与坐标，做点算术，返回。难点在 CuTeDSL 的运算细节：

- 取绝对值用 `mlir_math.absi(...)`，它返回裸值，需要用 `cute.TensorSSA(value, shape, dtype)` 包回张量。
- 多头斜率（ALiBi）用 `exp2` 计算，并巧妙利用 `ln2 · log₂e = 1` 把常数化简。
- 标量广播用 `cute.full_like(x, c)` 造一个同形状常量。

#### 4.4.3 源码精读

**softcap（内置工厂）** 见 [flash_attn/cute/utils.py:159-167](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/utils.py#L159-L167)：

```python
@cute.jit
def scoremod_premask_fn(acc_S_SSA, batch_idx, head_idx, q_idx, kv_idx, seqlen_info, aux_tensors):
    scores = acc_S_SSA / softcap_val
    return softcap_val * cute.math.tanh(scores, fastmath=True)
```

反向版本见 [flash_attn/cute/utils.py:170-179](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/utils.py#L170-L179)，用的是 tanh 的导数 `1 − tanh²`：

\[ \frac{dS'}{dS} = 1 - \tanh^2(S/\text{cap}) \]

**ALiBi** 见 [tests/cute/score_mod_definitions.py:87-97](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/tests/cute/score_mod_definitions.py#L87-L97)。注意斜率的推导：

```python
slope_exp = (h_idx + 1) * -8
slope = cute.math.exp2(slope_exp * 0.125 * 0.6931... * 1.4426...)
```

其中 `0.6931... = ln2`、`1.4426... = log₂e`，二者乘积为 1，所以

\[
\text{slope} = \text{exp2}((h+1)\cdot(-8)\cdot 0.125) = \text{exp2}(-(h+1)) = 2^{-(h+1)}
\]

与 eager 参考 [score_mod_definitions.py:510-512](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/tests/cute/score_mod_definitions.py#L510-L512) 的 `slope = 2 ** (-8 * (h + 1) / 8)` 完全一致。这里用 `exp2` 而非 `exp`，呼应 [u4-l1](u4-l1-online-softmax.md) 的换底加速。

**相对位置偏置** 见 [tests/cute/score_mod_definitions.py:40-44](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/tests/cute/score_mod_definitions.py#L40-L44)：

```python
@cute.jit
def score_mod_rel_bias(tSrS_ssa, b_idx, h_idx, q_idx, kv_idx, seqlen_info, aux_tensors):
    diff = q_idx - kv_idx
    abs_diff = cute.TensorSSA(mlir_math.absi(diff), diff.shape, diff.dtype)
    return tSrS_ssa + abs_diff.to(cutlass.Float32)
```

`.to(cutlass.Float32)` 是必须的：`q_idx`/`kv_idx` 是 `Int32`，加到浮点分数上要先转类型。

> **向量化变体**：同一文件里每个例子大多有 `_vectorized` 版本（如 [score_mod_definitions.py:100-112](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/tests/cute/score_mod_definitions.py#L100-L112) 的 ALiBi 向量版）。区别在于：标量版里 `q_idx`/`kv_idx` 已是向量 SSA，直接做向量运算；向量版则用 `q_idx[0]` 取行首坐标，再用 `range_constexpr` 循环逐列算 `diff0 - i`。`__vec_size__` 属性控制每次喂多少列（SM80/90/120 限 1 或 2，SM100 可到 4）。

#### 4.4.4 代码实践

**实践目标**：读懂三种 `score_mod` 的 eager 参考实现，建立“cute 版 ↔ torch 版”的对照能力。

**操作步骤**：

1. 对照 ALiBi 的 cute 版（87-97 行）与 eager 版（510-512 行），逐项确认 `slope` 计算等价。
2. 打开 [tests/cute/test_score_mod.py:59-70](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/tests/cute/test_score_mod.py#L59-L70)，看 `TEST_PAIRS` 如何把 cute 版与 eager 版配对，并用 `flex_attention` 做参考对拍。

**需要观察的现象**：每个 cute `score_mod` 都有一个签名相同的 eager 闭包 `(score, b, h, q_idx, kv_idx) -> score'`。

**预期结果**：能独立把任意一个 cute `score_mod` 翻译成等价 PyTorch 实现。这是第 5 节综合实践的前置能力。

#### 4.4.5 小练习与答案

**练习 1**：ALiBi 为什么每个 head 的斜率不同？`slope_h = 2^(−(h+1))` 意味着什么？

> **答案**：让不同 head 关注不同尺度的距离。`h=0` 时 `slope=0.5`（强惩罚远距离），`h` 越大斜率越小（越能看远）。这种几何递减让模型在不同 head 上覆盖不同的“感受野”。

**练习 2**：相对位置偏置里，为什么必须 `.to(cutlass.Float32)`？

> **答案**：`q_idx - kv_idx` 是 `Int32`，而 `tSrS_ssa` 是浮点分数。CuTeDSL 不做隐式类型提升，整数加到浮点上必须显式转换，否则类型不匹配。

**练习 3**：softcap 的 `cap` 应该在“缩放前”还是“缩放后”的分数上设定？为什么？

> **答案**：缩放后。因为用户回调收到的是 `score · softmax_scale`，softcap 作用在这个量纲上。所以同一个 `cap` 值在 `softmax_scale=1/√d` 下，实际夹紧的是 `QKᵀ/√d` 的范围。

---

### 4.5 aux_tensors / aux_scalars 扩展机制

#### 4.5.1 概念说明

到目前为止的例子都只用了坐标做修改。但很多真实模型需要**运行期数据**——比如一张 `(batch,)` 的偏置表、一组 `(head,)` 的缩放、一个学习的位置偏置矩阵。这些数据不能写死在编译期函数里。FA4 用两个通道把它们送进回调：

- **`aux_tensors`**：一个张量元组，随 `q/k/v` 一起从 gmem 传进 kernel，回调里用坐标索引读取。
- **`aux_scalars`**：一个标量元组（运行期捕获），适合传“当前调用独有的几个数”，避免靠闭包捕获导致缓存键变化。

二者都打包进 `AuxData` 这个具名元组。

#### 4.5.2 核心流程

```text
用户: flash_attn_func(..., aux_tensors=[bias_b, bias_h], aux_scalars=(scale,))
        │  interface.py 把它们转成 cute tensor / 类型元组
        ▼
AuxData(tensors=(cute_bias_b, cute_bias_h), scalars=(scale,))
        │  作为 kernel 实参一路传到 apply_score_mod_inner
        ▼
call_score_mod 取出 aux_data.tensors / aux_data.scalars，按名传给回调
        ▼
回调内: bias = aux_tensors[0][batch_idx]   # 用坐标查表
```

#### 4.5.3 源码精读

**`AuxData`** 是一个极简具名元组，见 [flash_attn/cute/utils.py:25-27](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/utils.py#L25-L27)：

```python
class AuxData(NamedTuple):
    tensors: tuple | list | None = None
    scalars: tuple | None = None
```

interface.py 把它装配好后随调用链传递，例如 [flash_attn/cute/interface.py:1087](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L1087) 处的 `AuxData(aux_tensors, aux_scalars)`。注意 `aux_scalar_metadata = tuple(type(s) for s in aux_scalars)`（[interface.py:662](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L662)）进入 `compile_key`：**标量的类型进缓存键，但具体数值不进**——这样改值不重编译，改类型才重编译。

**用 aux_tensors 的回调示例**：按 batch 取偏置的 `score_mod_batch_bias`，见 [tests/cute/score_mod_definitions.py:138-147](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/tests/cute/score_mod_definitions.py#L138-L147)：

```python
@cute.jit
def score_mod_batch_bias(tSrS_ssa, b_idx, h_idx, q_idx, kv_idx, seqlen_info, aux_tensors):
    batch_bias = aux_tensors[0]
    b_frag = cute.make_rmem_tensor(1, cutlass.Int32)
    b_frag.store(b_idx)
    bias_frag = cute.make_rmem_tensor(1, batch_bias.element_type)
    bias_frag[0] = batch_bias[b_frag[0]]
    return tSrS_ssa + (bias_frag.load()).to(cutlass.Float32)
```

模式固定：造一个 1 元素寄存器张量存索引 → 用索引查 `aux_tensors[k]` → `.load()` 取 SSA 值 → 转浮点 → 加到分数上。多张量时用 `aux_tensors[1]`、`aux_tensors[2]` 继续取（如 `score_mod_dual_buffer` 用了两个，见 [score_mod_definitions.py:160-178](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/tests/cute/score_mod_definitions.py#L160-L178)）。

**用 aux_scalars 的回调示例**：见 [tests/cute/test_score_mod.py:119-121](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/tests/cute/test_score_mod.py#L119-L121)，签名多一个 `aux_scalars` 参数，直接 `aux_scalars[0]` 取值。

> 关于 `seqlen_info`：本讲例子多数没用到它，但当需要**跨序列全局位置**时（如全局 token 偏置），要在回调内 `kv_idx + seqlen_info.offset_k` 得到全局索引，再查 `aux_tensors`。参考 `score_mod_global_kv_bias`，[score_mod_definitions.py:204-218](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/tests/cute/score_mod_definitions.py#L204-L218)。

#### 4.5.4 代码实践

**实践目标**：学会用 `aux_tensors` 把一张偏置表喂进回调。

**操作步骤**：

1. 阅读 `score_mod_batch_bias` 及其 eager 工厂 `batch_bias_factory`，[score_mod_definitions.py:527-531](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/tests/cute/score_mod_definitions.py#L527-L531)。
2. 看测试如何调用：[test_score_mod.py:166-183](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/tests/cute/test_score_mod.py#L166-L183) 的 `run_cute_flash` 接受 `aux_tensors` 并透传给 `_flash_attn_fwd`。

**需要观察的现象**：`aux_tensors` 是普通 `torch.Tensor` 列表，FA4 在 interface 里自动转成 cute tensor。

**预期结果**：理解“偏置张量在 host 是 torch tensor，进 kernel 变 cute tensor，回调里用坐标索引”的全过程。待本地验证：仿照 `TEST_PAIRS_WITH_AUX_TENSORS` 跑一次带偏置的前向。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `aux_scalars` 的**类型**进 `compile_key`，而**数值**不进？

> **答案**：类型决定寄存器分配与指令选择（Int32 vs Float32 不同），必须编译期固定；而数值是运行期数据，改它不改变生成的代码，只改变运行时行为。这样调参（改 cap、改 scale）不会触发重编译。

**练习 2**：想在回调里查一张 `(num_heads, max_kv)` 的二维偏置表，该用 `aux_tensors` 还是 `aux_scalars`？

> **答案**：`aux_tensors`。它是张量数据，必须随输入一起从 gmem 加载；`aux_scalars` 只适合少数标量。回调里用 `head_idx` 和 `kv_idx`（或全局 kv 索引）两维索引即可。

---

## 5. 综合实践

**任务**：亲手实现一个 softcap `score_mod`，传入 `flash_attn_func`，并与两条参考路径对拍。

**目标**：把本讲的“签名 → 注入 → 缩放时机 → 数值对拍”串成一条完整链路。

### 步骤 1：写 cute 版 softcap

仿照 [utils.py:159-167](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/utils.py#L159-L167) 写自己的版本（这是**示例代码**，不是项目原有调用脚本）：

```python
# 示例代码：自定义 softcap score_mod
import cutlass
import cutlass.cute as cute

CAP = 0.5  # 夹紧上界

@cute.jit
def my_softcap(score, b_idx, h_idx, q_idx, kv_idx, seqlen_info, aux_tensors):
    scaled = score / cute.full_like(score, CAP)
    return cute.full_like(score, CAP) * cute.math.tanh(scaled, fastmath=True)
```

### 步骤 2：写 PyTorch 参考实现

softcap 作用在“缩放后”的分数上，所以参考实现要先把 `QKᵀ` 乘 `softmax_scale`：

```python
# 示例代码：PyTorch 参考（数学等价）
import torch, math

def attention_softcap_ref(q, k, v, cap, softmax_scale=None):
    # q,k,v: (b, s, h, d) -> 转成 (b, h, s, d)
    q, k, v = map(lambda x: x.transpose(1, 2), (q, k, v))
    d = q.shape[-1]
    scale = softmax_scale if softmax_scale is not None else 1.0 / math.sqrt(d)
    s = (q.float() @ k.float().transpose(-2, -1)) * scale     # 缩放后的分数
    s = cap * torch.tanh(s / cap)                              # softcap
    p = torch.softmax(s, dim=-1)
    return (p @ v.float()).to(q.dtype).transpose(1, 2)
```

### 步骤 3：对拍三条路径

构造小输入并在 Hopper/Blackwell GPU 上对比（SM8x 不支持用户 `score_mod`，见 [interface.py:609-610](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L609-L610)）：

```python
# 示例代码：三路对拍
from flash_attn.cute import flash_attn_func

b, s, h, d = 2, 512, 8, 64
q = torch.randn(b, s, h, d, device="cuda", dtype=torch.float16)
k = torch.randn_like(q); v = torch.randn_like(q)

out_builtin, _ = flash_attn_func(q, k, v, softcap=0.5)         # 路径 A：内置 softcap
out_custom,  _ = flash_attn_func(q, k, v, score_mod=my_softcap) # 路径 B：自定义 score_mod
out_ref        = attention_softcap_ref(q, k, v, cap=0.5)        # 路径 C：PyTorch 参考

print((out_builtin - out_custom).abs().max())   # 预期 ~0（两者等价）
print((out_custom  - out_ref   ).abs().max())   # 预期在 fp16 舍入误差内
```

### 需要观察的现象

- 路径 A 与路径 B 应几乎完全一致（都走同一套 tanh 夹紧），证明 `softcap=` 只是 `score_mod` 的语法糖。
- 路径 B/C 的最大误差应在 fp16 量级（约 1e-2 ~ 1e-3），因为 FA4 是分块 online softmax，与朴素参考只在浮点舍入上不同。

### 预期结果

- 理解 `score_mod` 收到的是**已缩放**分数（4.3 节），所以参考实现里必须先 `* scale` 再 tanh。
- 理解换 `cap` 值不会触发重编译（`softcap` 值不进缓存键，`aux_scalars` 类型才进）——可尝试改 `CAP` 重跑，第二次应秒回。
- 若无 GPU，以上为“待本地验证”：可先读 [test_score_mod.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/tests/cute/test_score_mod.py) 里 `run_cute_flash` 与 `run_flex_reference` 的对拍逻辑，确认理解无误后再上机。

## 6. 本讲小结

- `score_mod` 是一个固定签名 `(score, b, h, q_idx, kv_idx, seqlen_info, aux_tensors)` 的 `@cute.jit` 回调，作用在 softmax **之前**，对分数做任意可编程修改。
- 它以 `cutlass.Constexpr` 注入，编译期内联进 kernel；其源码哈希 `hash_callable` 进入 `compile_key`，所以换一个 `score_mod` 就重编译。
- `call_score_mod` / `apply_score_mod_inner` 是注入链：前者兼容带/不带 `aux_scalars` 的两套签名，后者负责从累加器与坐标张量里按 `vec_size` 抽取并回写。
- 有 `score_mod` 时缩放“提前”到回调之前（`compute_softmax_scale_log2` 退化为只换底 `log₂e`），用户拿到的是 `QKᵀ·softmax_scale`；无 `score_mod` 时缩放折叠进 `scale_log2` 在 `exp2` 完成。两条路数学等价。
- softcap / ALiBi / rel_bias 三个实例展示了“坐标算术 + 类型转换 + exp2 换底”的标准写法；`aux_tensors` / `aux_scalars` 提供运行期张量与标量的扩展通道。
- 内置 `softcap=` 参数只是 `create_softcap_scoremod` 生成的 `score_mod` 的语法糖，与用户 `score_mod` 互斥。

## 7. 下一步学习建议

- 想了解 `score_mod` 的“姐妹”机制——按布尔掩码置 `-inf` 的 `mask_mod`，继续读 [u3-l1 AttentionMask](u3-l1-attention-mask.md)，对照二者的签名差异与 R2P 位图实现。
- 想深入 `compile_key` 与 JIT 缓存为何能把“换 score_mod 触发重编译”做对，读 [u11-l1 JIT 编译与缓存机制](u11-l1-jit-and-cache.md)。
- 想看反向如何把 `score_mod` 的导数（如 softcap 的 `1−tanh²`）接进 `dQ/dK/dV`，预习 `call_score_mod_bwd` 与 `apply_score_mod_bwd_inner`（[softmax.py:581-698](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/softmax.py#L581-L698)），并承接 [u9-l1 反向算法](u9-l1-backward-algorithm-sm80.md)。
