# 因果掩码 is_causal 与可加 attn_mask

## 1. 本讲目标

本讲聚焦 `ffpa_attn_func` 的两种「限制可见范围」机制：**因果掩码（causal）** 和 **可加注意力掩码（attn_mask）**。学完后你应当能够：

- 说清 `is_causal=True` 时 FFPA 采用的「query 对齐到 KV 尾部」约定，并能写出 query 行 \(r\) 只看 key 列 \(k \le r+(N_{kv}-N_q)\) 的来源。
- 区分 `attn_mask` 的两种语义：**布尔掩码**（`True` 参与注意力、`False` 映射为 \(-\infty\)）与**可加偏置**（additive bias，直接加到 score 上）。
- 掌握 `attn_mask` 的 2/3/4 维广播规则，知道每种维度对应 `[B, Nh_q, Nq, Nkv]` 的哪一维。
- 知道 `is_causal=True` 与显式 `attn_mask` **不能同时设置**，以及 `is_causal` 要求 `Nkv \ge Nq`。
- 读懂 `normalize_inputs`、`normalize_attn_mask`、`_validate_attn_mask_shape` 三个函数的校验逻辑。

本讲承接 [u2-l1](./u2-l1-ffpa-attn-func-signature-layout.md)（签名与 `[B,Nh,N,D]` 布局）与 [u2-l2](./u2-l2-self-cross-decode-attention.md)（self/cross/decode 三种模式），是公共 API 用法的第三块拼图。

## 2. 前置知识

### 2.1 注意力与 softmax 回顾

标准注意力计算（[u2-l1](./u2-l1-ffpa-attn-func-signature-layout.md) 已建立）：

\[
S = \text{scale}\cdot Q K^\top \in \mathbb{R}^{N_q \times N_{kv}}, \qquad
O = \text{softmax}(S)\, V
\]

其中 softmax 沿 **key 维（最后一维 \(N_{kv}\)）** 归一化：

\[
\text{softmax}(s)_j = \frac{e^{s_j}}{\sum_{l} e^{s_l}}
\]

> **术语**：\(N_q\) 是 query 序列长度（行数），\(N_{kv}\) 是 key/value 序列长度（列数），\(D\) 是 head\_dim。score 矩阵 \(S\) 形状恒为 \([N_q, N_{kv}]\)，输出 \(O\) 的序列维跟 \(N_q\)。

### 2.2 什么是「掩码」

很多时候我们不想让某个 query 行看到所有 key 列，例如：

- **因果（causal）**：自回归语言模型里，第 \(i\) 个 token 只能看它自己及之前的 token，不能「偷看未来」。
- **padding**：批次里短序列用 pad 填充，注意力应当忽略 pad 位置。
- **位置偏置**：ALiBi、相对位置偏置等，给不同 key 位置加一个常数偏置。

掩码的本质是：在 softmax **之前**修改 score 矩阵 \(S\)。FFPA 沿用 PyTorch SDPA 的两种修改方式：

| 语义 | 输入类型 | 对 score 的作用 | 典型用途 |
|:---|:---|:---|:---|
| 布尔掩码 | `torch.bool` | `True → 0`（参与），`False → -inf`（屏蔽） | padding、自定义可见性 |
| 可加偏置 | `float` 张量 | 直接 \(S \leftarrow S + \text{bias}\) | 位置偏置、ALiBi |

把某个位置的 score 置为 \(-\infty\) 后，\(e^{-\infty}=0\)，该位置在 softmax 中权重为 0，等价于「不可见」。

### 2.3 张量广播（broadcasting）速记

`attn_mask` 不必显式写成完整 \([B, Nh_q, Nq, Nkv]\)，可以靠广播省略某些维度。规则是：**某一维大小为 1 时，沿该维复制；大小必须为 1 或与目标相等**。本讲的 `_validate_attn_mask_shape` 就是在检查这套广播规则。

### 2.4 与回退（fallback）的关系

[u1-l4](./u1-l4-one-line-sdpa-monkey-patch.md) 已讲过：当形状落在 FFPA 不擅长的区间时，`ffpa_attn_func` 会**自动回退到原生 SDPA**。回退条件里有一条 `8 \le Nq < 512` 与 `Nkv < 512`。本讲的掩码处理在「非回退」路径上发生（默认 Triton 后端、大 \(D\)、长序列），所以**实践时要用足够长的序列**才能让 FFPA kernel 自己处理掩码；否则只是落到 SDPA（结果仍然正确，但看不到 FFPA 的处理过程）。

## 3. 本讲源码地图

本讲只涉及两个文件，全部校验逻辑集中在 `functional.py`：

| 文件 | 角色 | 本讲关注点 |
|:---|:---|:---|
| `src/ffpa_attn/ffpa_attn_interface.py` | 公共入口，定义 `ffpa_attn_func` | `is_causal` / `attn_mask` 参数签名与 docstring |
| `src/ffpa_attn/functional.py` | 分发层，含 `FFPAAttnMeta` | `normalize_inputs`（因果校验）、`normalize_attn_mask`（掩码归一化）、`_validate_attn_mask_shape`（形状校验）|

调用链（[u2-l1](./u2-l1-ffpa-attn-func-signature-layout.md) 已见过）：

```
ffpa_attn_func(q,k,v, attn_mask=..., is_causal=...)
  ├── meta.fallback(...)            # 先短路判定是否回退 SDPA
  └── meta.normalize(...)
        ├── normalize_inputs(...)    # ← 本讲 4.1：因果校验
        └── normalize_attn_mask(...) # ← 本讲 4.2 / 4.3：掩码归一化 + 形状校验
              └── _validate_attn_mask_shape(...)
```

## 4. 核心概念与源码讲解

### 4.1 因果掩码 is_causal：尾部对齐语义与校验

#### 4.1.1 概念说明

`is_causal=True` 是一种**内置的、零开销的**因果掩码：调用者不需要自己构造三角矩阵，FFPA 在 kernel 内部直接用「行号 vs 列号」的比较来屏蔽未来位置，省下了构造和搬运大掩码张量的开销。

关键在于 FFPA（与 SDPA）采用的是 **query 对齐到 KV 尾部（queries aligned to KV tail）** 的约定。直观理解：把 \(N_q \times N_{kv}\) 的 score 矩阵想象成一个矩形，因果掩码的下三角矩阵**贴在矩形的右下角**，而不是左上角。这意味着：

- **自注意力（\(N_q = N_{kv} = N\)）**：退化为标准三角掩码，query 行 \(r\) 只看 key 列 \(k \le r\)。
- **分块/解码预填充（\(N_{kv} > N_q\)）**：query 行 \(r\) 只看 key 列 \(k \le r + (N_{kv} - N_q)\)。最后一行 query（\(r=N_q-1\)）能看到全部 \(N_{kv}\) 个 key。

这条约定有一个硬性前提：**\(N_{kv} \ge N_q\)**。因为只有 query 数不超过 key 数时，「贴在右下角」才有意义；否则因果掩码会让某些 query 行一个 key 都看不到（全 \(-\infty\)，softmax 分母为 0，出现 NaN）。

#### 4.1.2 核心流程

把尾部对齐写成掩码公式（\(r\) 为 query 行下标，\(k\) 为 key 列下标，均从 0 开始）：

\[
m_{r,k} =
\begin{cases}
0 & \text{若 } k \le r + (N_{kv} - N_q) \\
-\infty & \text{否则}
\end{cases}
\]

最终 score \(S' = S + m\)，再过 softmax。

校验流程（在 `normalize_inputs` 中）：

1. 若 `is_causal=True` 且同时传了显式 `attn_mask` → 报错（两者互斥）。
2. 若 `is_causal=True` 且 `Nkv < Nq` → 报 `ValueError`。
3. 通过校验后，`is_causal` 被存入 `attn_meta.is_causal`，随 meta 一路传到 kernel，由 kernel 内部用行/列号比较实现，**不物化掩码**。

#### 4.1.3 源码精读

`is_causal` 的参数声明与 docstring 解释（query 行 \(r\) 只看 \(k \le r+(N_{kv}-N_q)\)，要求 \(N_{kv}\ge N_q\)）：

- [src/ffpa_attn/ffpa_attn_interface.py:124-128](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ffpa_attn_interface.py#L124-L128) — `is_causal` 参数文档，写明尾部对齐约定与 `Nkv >= Nq` 要求。

互斥校验：`attn_mask` 与 `is_causal` 不能同时设置（注意这里抛的是 `RuntimeError`，不是 `ValueError`）：

- [src/ffpa_attn/functional.py:565-568](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L565-L568) — `attn_mask is not None and is_causal` → `RuntimeError`。

因果形状校验（本模块的核心一行）：

- [src/ffpa_attn/functional.py:620-624](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L620-L624) — `is_causal and key.size(2) < query.size(2)` 即 `Nkv < Nq` 时抛 `ValueError`，错误信息明确写出「queries are aligned to the KV tail」。

关键片段（只保留因果相关部分）：

```python
# functional.py:565-568  互斥
if attn_mask is not None and is_causal:
    raise RuntimeError(
        "ffpa_attn_func: explicit attn_mask should not be set when is_causal=True"
    )

# functional.py:620-624  尾部对齐要求 Nkv >= Nq
if is_causal and key.size(2) < query.size(2):
    raise ValueError(
        f"is_causal=True requires Nkv >= Nq (queries are aligned to the KV tail), "
        f"got Nq={query.size(2)}, Nkv={key.size(2)}"
    )
```

通过校验后，`is_causal` 落到 `attn_meta`，最终在 kernel 内部用比较指令实现（如 `offs_kv <= offs_m + (Nk - Nq)`），这一点会在 [u4-l4](./u4-l4-fwd-features-gqa-mask-dropout.md) 的 causal 掩码段详细展开，本讲只需知道「校验在此、实现在 kernel」。

补充：回退路径与 `is_causal` 的关系。`fallback()` 本身**不看** `is_causal`，它只看 `Nq`/`Nkv`/`D` 等前向形状：

- [src/ffpa_attn/functional.py:515-522](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L515-L522) — 回退条件里含 `8 <= Nq < 512` 与 `Nkv < 512`。这意味着 docs 示例里 `Nq=128, Nkv=8192` 的「分块因果」用例**会回退到 SDPA**（结果正确，但由 SDPA 处理掩码）；只有 `Nq, Nkv >= 512` 的大序列才真正进 FFPA kernel。

#### 4.1.4 代码实践

**实践目标**：复现 `docs/index.md` 的因果自注意力示例，验证 FFPA 的 `is_causal` 与 SDPA 数值一致；并亲手触发「`Nkv < Nq`」错误。

**操作步骤**：

1. 复现 docs 的自注意力因果示例（`N=4096, D=512`，落在 FFPA 大 \(D\) 路径上）：

```python
# 示例代码：复现 docs/index.md 的 causal 示例（example-causal 段）
import torch
import torch.nn.functional as F
from ffpa_attn import ffpa_attn_func

B, H, N, D = 1, 8, 4096, 512
q = torch.randn(B, H, N, D, dtype=torch.bfloat16, device="cuda")
k = torch.randn(B, H, N, D, dtype=torch.bfloat16, device="cuda")
v = torch.randn(B, H, N, D, dtype=torch.bfloat16, device="cuda")

out = ffpa_attn_func(q, k, v, is_causal=True)
ref = F.scaled_dot_product_attention(q, k, v, is_causal=True)
print(out.shape, out.dtype)
print(f"vs SDPA max_abs_err={(out - ref).abs().max().item():.4e}")
```

2. 触发 `Nkv < Nq` 错误（构造一个 query 比 key 还长的因果用例）：

```python
# 示例代码：故意违反 Nkv >= Nq
q_bad = torch.randn(1, 8, 8192, 512, dtype=torch.bfloat16, device="cuda")
k_bad = torch.randn(1, 8, 4096, 512, dtype=torch.bfloat16, device="cuda")
v_bad = torch.randn(1, 8, 4096, 512, dtype=torch.bfloat16, device="cuda")
ffpa_attn_func(q_bad, k_bad, v_bad, is_causal=True)  # 期望抛 ValueError
```

**需要观察的现象**：
- 步骤 1 打出的 `max_abs_err` 应为小量（bf16 下通常 `1e-1` 量级或更小，与 [u2-l1](./u2-l1-ffpa-attn-func-signature-layout.md) 的 self-attn 误差同阶）。
- 步骤 2 抛出 `ValueError: is_causal=True requires Nkv >= Nq ...`。

**预期结果**：步骤 1 数值与 SDPA 吻合；步骤 2 抛 `ValueError`，错误信息含 `got Nq=8192, Nkv=4096`。

> 待本地验证：具体 `max_abs_err` 数值依赖 GPU 与 bf16 实现，请在本地 CUDA 环境运行确认。

#### 4.1.5 小练习与答案

**练习 1**：若 `Nq=128, Nkv=8192, D=512, is_causal=True`，query 行 \(r=0\) 能看到哪些 key 列？这次调用会走 FFPA kernel 还是回退 SDPA？

> **答案**：\(r=0\) 看到 key 列 \(k \le 0 + (8192-128) = 8067\)，即前 8068 个 key。但 `Nq=128` 命中回退条件 `8 <= Nq < 512`，故**回退到 SDPA**，由 SDPA 实现该因果掩码。

**练习 2**：为什么 `is_causal` 不需要调用者传入任何掩码张量？kernel 是如何知道哪些位置要屏蔽的？

> **答案**：因果关系完全由「query 行号 \(r\) 与 key 列号 \(k\) 的大小关系」决定，kernel 内部直接比较 `k <= r + (Nkv - Nq)` 即可，无需任何外部张量，因此既省显存又省搬运。这正是 `is_causal` 相比手造三角 `attn_mask` 的优势。

---

### 4.2 attn_mask 的两种语义：布尔掩码与可加偏置

#### 4.2.1 概念说明

当因果掩码不够用时（例如 padding、自定义可见性、位置偏置），调用者可以传 `attn_mask`。FFPA 完全对齐 SDPA 的两种语义：

1. **布尔掩码（`dtype=torch.bool`）**：`True` 表示该位置**参与**注意力，`False` 表示**屏蔽**。内部把 `False` 转成 \(-\infty\)、`True` 转成 \(0\) 的可加偏置。注意布尔掩码**不能 `requires_grad`**（不可导）。
2. **可加偏置（`dtype` 为 `float32` 或 query 的 fp16/bf16）**：直接作为一个偏置张量 \(b\) 加到 score 上：\(S' = S + b\)。常用于 ALiBi、相对位置编码等。

两种语义最终都被 `normalize_attn_mask` 归一成**同一个东西：一个 4 维可加偏置张量** `attn_bias`，再交给 kernel。也就是说，kernel 内部只认「可加偏置」一种形式，归一化层负责把布尔语义翻译过来。

#### 4.2.2 核心流程

`normalize_attn_mask(query, key, attn_mask)` 的执行流程：

1. `attn_mask is None` → 直接返回 `None`（无掩码）。
2. **设备校验**：`attn_mask` 必须与 query 同设备，否则 `TypeError`。
3. **dtype 校验**：只接受 `bool` / `float32` / query 自身 dtype，否则 `TypeError`。
4. **形状校验**：调用 `_validate_attn_mask_shape`（见 4.3）。
5. **布尔→偏置转换**：若是 bool，用 `torch.where(mask, 0, -inf)` 生成偏置；否则原样作为偏置。
6. **维度补全**：2 维补成 `[1,1,Nq,Nkv]`、3 维补成 `[B,1,Nq,Nkv]`；4 维不动。
7. **连续性**：若最后一维 stride 不为 1（非行连续），调用 `.contiguous()`。
8. 返回紧凑的 4 维 `attn_bias`（**不展开**广播维度，广播由 kernel 用零 stride 处理）。

布尔转换的数学等价：对布尔掩码 \(M\)，

\[
b_{r,k} =
\begin{cases}
0 & \text{若 } M_{r,k} = \text{True} \\
-\infty & \text{若 } M_{r,k} = \text{False}
\end{cases}
,\qquad S' = S + b
\]

#### 4.2.3 源码精读

`normalize_attn_mask` 完整定义与 docstring：

- [src/ffpa_attn/functional.py:633-693](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L633-L693) — 把用户 SDPA 风格 `attn_mask` 转成 4 维可加偏置；docstring 说明布尔语义为「`True` 参与、`False` 映射 \(-\infty\)」，并强调返回的是**紧凑**偏置（广播维度不物化）。

设备与 dtype 双校验：

- [src/ffpa_attn/functional.py:658-667](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L658-L667) — 设备不一致 → `TypeError`；dtype 不在 `{bool, float32, query.dtype}` → `TypeError`。

布尔→\(-\infty\) 转换（本模块的灵魂几行）：

- [src/ffpa_attn/functional.py:673-680](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L673-L680) — `torch.where(attn_mask, 0, neg_inf)`，生成与 query 同 dtype 的可加偏置。

维度补全与连续性：

- [src/ffpa_attn/functional.py:684-693](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L684-L693) — 2D/3D 用 `view` 补成 4D；若最后维 stride≠1 则 `.contiguous()`。

关键片段：

```python
# functional.py:673-682  布尔掩码翻译成 -inf 偏置；浮点掩码原样用作偏置
if attn_mask.dtype == torch.bool:
    neg_inf = torch.tensor(float("-inf"), dtype=query.dtype, device=query.device)
    attn_bias = torch.where(
        attn_mask, torch.zeros((), dtype=query.dtype, device=query.device), neg_inf
    )
else:
    attn_bias = attn_mask

# functional.py:684-693  补维 + 保行连续
if attn_bias.dim() == 2:
    attn_bias = attn_bias.view(1, 1, attn_bias.size(0), attn_bias.size(1))
elif attn_bias.dim() == 3:
    attn_bias = attn_bias.view(attn_bias.size(0), 1, attn_bias.size(1), attn_bias.size(2))
if attn_bias.stride(-1) != 1:
    attn_bias = attn_bias.contiguous()
return attn_bias
```

> **关于「不展开」**：`normalize_attn_mask` 返回的 4 维偏置**保持紧凑**——例如用户传 `[1,1,1,Nkv]`，返回仍是 `[1,1,1,Nkv]`，而**不会**物化成 `[B,Nh_q,Nq,Nkv]`。真正的「零 stride 广播」发生在 Triton wrapper/kernel 内部（详见 [u4-l4](./u4-l4-fwd-features-gqa-mask-dropout.md) 的 `_attn_bias_broadcast_strides`）。这样做避免了为一个 key 位置偏置分配巨大张量。

补充：CuTeDSL 后端不支持 `attn_mask`。在 `fallback()` 中 `cutedsl + attn_mask` 命中回退条件：

- [src/ffpa_attn/functional.py:518](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L518) — `attn_mask is not None and self.forward_meta.name == "cutedsl"` 返回回退。
- [src/ffpa_attn/functional.py:560-564](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L560-L564) — `normalize_inputs` 另有 `NotImplementedError` 守卫，提示需要掩码时改用 `forward_backend='triton'`。

#### 4.2.4 代码实践

**实践目标**：构造一个 `[1,1,1,Nkv]` 的可加 key 位置偏置，传入 `attn_mask`，验证 FFPA 与「手动加偏置的 SDPA」数值一致。

**操作步骤**：

```python
# 示例代码：可加 key 位置偏置
import torch
import torch.nn.functional as F
from ffpa_attn import ffpa_attn_func

B, H, Nq, Nkv, D = 1, 8, 512, 8192, 512   # Nq,Nkv>=512 保证走 FFPA kernel
q = torch.randn(B, H, Nq,  D, dtype=torch.bfloat16, device="cuda")
k = torch.randn(B, H, Nkv, D, dtype=torch.bfloat16, device="cuda")
v = torch.randn(B, H, Nkv, D, dtype=torch.bfloat16, device="cuda")

# [1,1,1,Nkv] 的 key 位置偏置：每个 query 行、每个 head 都加同一组 per-key 偏置
bias = torch.randn(1, 1, 1, Nkv, dtype=torch.bfloat16, device="cuda")

out_ffpa = ffpa_attn_func(q, k, v, attn_mask=bias)
ref = F.scaled_dot_product_attention(q, k, v, attn_mask=bias)  # SDPA 同样把 bias 当可加
print(f"FFPA vs SDPA(additive) max_abs_err={(out_ffpa - ref).abs().max().item():.4e}")

# 对照：布尔掩码语义（屏蔽掉后一半 key）
bool_mask = torch.ones(1, 1, 1, Nkv, dtype=torch.bool, device="cuda")
bool_mask[..., Nkv // 2:] = False          # 后一半 key 不可见
out_bool = ffpa_attn_func(q, k, v, attn_mask=bool_mask)
ref_bool = F.scaled_dot_product_attention(q, k, v, attn_mask=bool_mask)
print(f"FFPA vs SDPA(bool) max_abs_err={(out_bool - ref_bool).abs().max().item():.4e}")
```

**需要观察的现象**：两组 `max_abs_err` 都应为小量，说明 FFPA 把可加偏置与布尔掩码都正确地合并进了 softmax。

**预期结果**：误差与 [u2-l1](./u2-l1-ffpa-attn-func-signature-layout.md) 的 self-attn 对照同阶；布尔掩码版本里被屏蔽位置在 softmax 后权重为 0。

> 待本地验证：具体误差数值请在本地 CUDA 环境运行确认。注意 `Nq=512` 不满足 `8<=Nq<512`（512 不小于 512），故不回退，确实进 FFPA kernel。

**附加验证（错误路径）**：给布尔掩码开 `requires_grad`，期望被拒：

```python
bad = torch.ones(1, 1, Nq, Nkv, dtype=torch.bool, device="cuda", requires_grad=True)
ffpa_attn_func(q, k, v, attn_mask=bad)  # 期望 TypeError
```

对应守卫在 [src/ffpa_attn/functional.py:569-572](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L569-L572)。

#### 4.2.5 小练习与答案

**练习 1**：若你想让 query 行只看到 key 的前半部分，用布尔掩码和用可加偏置分别该怎么写？二者最终在 kernel 里是否等价？

> **答案**：布尔：`mask[..., :Nkv//2]=True, mask[..., Nkv//2:]=False`；可加：`bias[..., Nkv//2:] = -inf`。归一化后布尔版本就被 `torch.where` 转成了同样的 \(-\infty\) 可加偏置，**二者在 kernel 内完全等价**。

**练习 2**：为什么 `normalize_attn_mask` 对 4 维用户掩码不做 `view` 补维，却仍要检查 `stride(-1)`？

> **答案**：4 维掩码维度数已对齐，无需补维；但用户传入的张量可能不是行连续（如某个转置的结果），此时最后一维 stride 不为 1，kernel 按行加载会出错，故需 `.contiguous()` 保证最后一维（key 维）在内存里连续。

---

### 4.3 attn_mask 形状广播校验

#### 4.3.1 概念说明

`_validate_attn_mask_shape` 解决的问题是：用户传来的 `attn_mask` 维度不一（可能是 2/3/4 维），且各维大小可能是 1（表示广播）。该校验函数按 **SDPA fused-kernel 的广播约定** 检查掩码能否广播到完整 score 形状 `[B, Nh_q, Nq, Nkv]`。

理解这套约定的关键是「**最后两维永远是 `[Nq, Nkv]`**」：

- 2 维 `[Nq, Nkv]`：最常见，每个 batch、每个 head 共用同一个掩码。
- 3 维 `[B, Nq, Nkv]`：**第一维是 batch，不是 head**（这是 SDPA 约定，容易踩坑）；head 维通过广播为 1。
- 4 维 `[B, Nh_q, Nq, Nkv]`：最完整，可对每个 batch、每个 head 给不同掩码。

对于每个维度，大小要么等于目标，要么为 1（广播）。

#### 4.3.2 核心流程

校验规则汇总（目标形状 `[B, Nh_q, Nq, Nkv]`）：

| 用户掩码维度 | 形状 | 维度映射与校验 |
|:---:|:---|:---|
| 2D | `[A, B]` | `A ∈ {1, Nq}`（query 维），`B ∈ {1, Nkv}`（key 维）|
| 3D | `[C, A, B]` | `C ∈ {1, B_batch}`（**batch 维**），`A ∈ {1, Nq}`，`B ∈ {1, Nkv}` |
| 4D | `[C, H, A, B]` | `C ∈ {1, B_batch}`，`H ∈ {1, Nh_q}`，`A ∈ {1, Nq}`，`B ∈ {1, Nkv}` |

> 注意：3D 掩码没有「head」这一层，它的第一维直接是 batch。若想对每个 head 用不同掩码，必须用 4D。

任何不在 {2,3,4} 的维度数、或某维大小既不是 1 也不是目标值，都会抛 `ValueError`。

#### 4.3.3 源码精读

`_validate_attn_mask_shape` 完整定义：

- [src/ffpa_attn/functional.py:341-388](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L341-L388) — 校验 SDPA 风格掩码广播维度，docstring 指明目标为 `[B, Nh_q, Nq, Nkv]`。

分段对应：

- 维度数必须为 2/3/4：[src/ffpa_attn/functional.py:358-362](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L358-L362)
- query 维（倒数第二）∈ {1, Nq}：[src/ffpa_attn/functional.py:363-367](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L363-L367)
- key 维（最后）∈ {1, Nkv}：[src/ffpa_attn/functional.py:368-372](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L368-L372)
- 3D 的 batch 维 ∈ {1, B}：[src/ffpa_attn/functional.py:373-377](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L373-L377)
- 4D 的 batch 维与 head 维 ∈ {1, 目标}：[src/ffpa_attn/functional.py:378-388](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L378-L388)

关键片段：

```python
# functional.py:358-362  只接受 2/3/4 维
if attn_mask.dim() not in (2, 3, 4):
    raise ValueError("ffpa_attn_func: attn_mask must be 2-D, 3-D, or 4-D ...")
# functional.py:363-372  最后两维：query 维与 key 维
if attn_mask.size(-2) not in (1, seqlen_q):      # query 维
    raise ValueError(...)
if attn_mask.size(-1) not in (1, seqlen_k):      # key 维
    raise ValueError(...)
# functional.py:373-388  3D 校验 batch 维；4D 校验 batch 维 + head 维
```

注意校验只判「能否广播」，**不区分布尔与可加**——dtype 的区分由 `normalize_attn_mask` 在调用本函数之前完成（[src/ffpa_attn/functional.py:663-671](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L663-L671)）。

#### 4.3.4 代码实践

**实践目标**：用不同形状的 `attn_mask` 调用，观察哪些通过、哪些被 `_validate_attn_mask_shape` 拒绝，从而记住广播规则。

**操作步骤**：

```python
# 示例代码：逐一试探掩码形状（设 B=2, H=8, Nq=512, Nkv=8192, D=512）
import torch
from ffpa_attn import ffpa_attn_func
B, H, Nq, Nkv, D = 2, 8, 512, 8192, 512
q = torch.randn(B, H, Nq,  D, dtype=torch.bfloat16, device="cuda")
k = torch.randn(B, H, Nkv, D, dtype=torch.bfloat16, device="cuda")
v = torch.randn(B, H, Nkv, D, dtype=torch.bfloat16, device="cuda")

def trial(name, shape):
    m = torch.zeros(shape, dtype=torch.bfloat16, device="cuda")
    try:
        ffpa_attn_func(q, k, v, attn_mask=m)
        print(f"{name}: OK  shape={shape}")
    except Exception as e:
        print(f"{name}: FAIL ({type(e).__name__}) shape={shape}")

trial("2D [Nq,Nkv]",     (Nq, Nkv))        # OK
trial("2D [1,Nkv]",      (1, Nkv))         # OK（query 维广播）
trial("3D [B,Nq,Nkv]",   (B, Nq, Nkv))     # OK
trial("3D [1,Nq,Nkv]",   (1, Nq, Nkv))     # OK（batch 广播）
trial("4D [1,1,1,Nkv]",  (1, 1, 1, Nkv))   # OK（全广播，key 位置偏置）
trial("4D [B,H,Nq,Nkv]", (B, H, Nq, Nkv))  # OK（最完整）
trial("BAD key 维",      (Nq, Nkv + 1))    # FAIL：key 维既不是 1 也不是 Nkv
trial("BAD 5D",          (1,1,1,Nq,Nkv))   # FAIL：维度数不在 {2,3,4}
```

**需要观察的现象**：前 6 个用例打印 `OK`；后 2 个打印 `FAIL (ValueError)`。

**预期结果**：与上表广播规则完全一致。

> 待本地验证：在本地 CUDA 环境运行确认每个用例的 OK/FAIL 归属。

#### 4.3.5 小练习与答案

**练习 1**：用户想给 batch 内**第 0 个样本**用一个掩码、**第 1 个样本**用另一个掩码（同一个 head），应该用几维掩码？形状是什么？

> **答案**：用 3 维 `[B, Nq, Nkv]`（即 `[2, Nq, Nkv]`）。3D 第一维是 batch，可以对每个样本给不同掩码；head 维通过广播共用。注意**不能**用 2D（2D 对所有 batch/head 共用）。

**练习 2**：一个形状为 `[Nh_q, Nq, Nkv]` 的 3D 掩码会被接受吗？为什么？

> **答案**：会**被拒**。3D 掩码的第一维按 SDPA 约定是 **batch 维**，必须 ∈ {1, B}。若想按 head 区分掩码，第一维 `Nh_q` 不会等于 batch（除非巧合），且即便等于也会被当成 batch 维校验，语义错误。要对每个 head 用不同掩码，必须用 **4D** `[1, Nh_q, Nq, Nkv]`。

---

## 5. 综合实践

把本讲三个模块串起来：**写一个小脚本，同时演示「因果自注意力」「可加 key 位置偏置」「布尔 padding 掩码」三种场景，并对照 SDPA 验证数值；最后故意触发两个错误路径。**

```python
# 示例代码：综合实践
import torch
import torch.nn.functional as F
from ffpa_attn import ffpa_attn_func

B, H, N, D = 1, 8, 4096, 512
q = torch.randn(B, H, N, D, dtype=torch.bfloat16, device="cuda")
k = torch.randn(B, H, N, D, dtype=torch.bfloat16, device="cuda")
v = torch.randn(B, H, N, D, dtype=torch.bfloat16, device="cuda")

# 场景 A：因果自注意力（normalize_inputs 因果校验 → kernel 内部行/列比较）
o_causal = ffpa_attn_func(q, k, v, is_causal=True)
r_causal = F.scaled_dot_product_attention(q, k, v, is_causal=True)
print(f"[causal]      max_abs_err={(o_causal - r_causal).abs().max().item():.4e}")

# 场景 B：可加 key 位置偏置（normalize_attn_mask：浮点偏置 → 紧凑 4D → kernel 零 stride 广播）
bias = torch.randn(1, 1, 1, N, dtype=torch.bfloat16, device="cuda")
o_bias = ffpa_attn_func(q, k, v, attn_mask=bias)
r_bias = F.scaled_dot_product_attention(q, k, v, attn_mask=bias)
print(f"[additive]    max_abs_err={(o_bias - r_bias).abs().max().item():.4e}")

# 场景 C：布尔 padding 掩码（normalize_attn_mask：bool → where(.,0,-inf) → 可加偏置）
pad = min(256, N)
bool_mask = torch.ones(1, 1, N, N, dtype=torch.bool, device="cuda")
bool_mask[..., pad:, :] = False        # query 维后段是 padding，整行屏蔽
o_pad = ffpa_attn_func(q, k, v, attn_mask=bool_mask)
r_pad = F.scaled_dot_product_attention(q, k, v, attn_mask=bool_mask)
print(f"[bool/pad]    max_abs_err={(o_pad - r_pad).abs().max().item():.4e}")

# 错误路径 1：is_causal 与 attn_mask 同时设置 → RuntimeError
try:
    ffpa_attn_func(q, k, v, is_causal=True, attn_mask=bias)
except RuntimeError as e:
    print(f"[err1] RuntimeError（预期）: {e}")

# 错误路径 2：is_causal 但 Nkv<Nq → ValueError
try:
    ffpa_attn_func(q[..., :N // 2, :], k, v, is_causal=True)  # Nq=N/2 < N=Nkv
except ValueError as e:
    print(f"[err2] ValueError（预期）: {e}")
```

**验收标准**：三个场景的 `max_abs_err` 均为小量；两个错误路径分别抛出 `RuntimeError` 与 `ValueError`。若把 `N` 改成小于 512，注意前向会回退 SDPA（结果仍正确，但不是 FFPA kernel 在处理掩码）。

> 待本地验证：完整脚本需在具备大 \(D\)（≥320）支持的 CUDA GPU 上运行；具体误差数值以本地实测为准。

## 6. 本讲小结

- `is_causal=True` 是**零开销内置因果掩码**：query 行 \(r\) 只看 key 列 \(k \le r+(N_{kv}-N_q)\)，要求 \(N_{kv} \ge N_q\)，否则 `normalize_inputs` 抛 `ValueError`。
- `is_causal=True` 与显式 `attn_mask` **互斥**，同时设置会抛 `RuntimeError`（[functional.py:565-568](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L565-L568)）。
- `attn_mask` 有两种语义：**布尔**（`True` 参与、`False`→\(-\infty\)，不可导）与**可加偏置**（直接加到 score）；`normalize_attn_mask` 把二者统一归一成 4 维可加偏置。
- 归一化后的偏置**保持紧凑**（不展开广播维度），真正的零 stride 广播在 kernel 内完成，省显存。
- `attn_mask` 的 dtype 仅接受 `bool`/`float32`/query dtype，且须与 query 同设备。
- `_validate_attn_mask_shape` 按 SDPA 约定校验广播：最后两维恒为 `[Nq, Nkv]`，3D 第一维是 **batch（不是 head）**，4D 才能按 head 区分。

## 7. 下一步学习建议

- 掩码归一化产出的 `attn_bias` 如何在 kernel 内被消费？请接着读 [u4-l4 前向特性：GQA / attn_bias / causal / dropout 实现](./u4-l4-fwd-features-gqa-mask-dropout.md)，那里讲 `_attn_bias_broadcast_strides` 与 causal 的 `offs_kv <= offs_m + (Nk - Nq)` 实现。
- 想理解 `is_causal` 与 `attn_mask` 的梯度如何反传？见 [u5-l1 FlashAttention-2 反向算法与 Delta 预处理](./u5-l1-bwd-algo-delta-preprocess.md)（大 \(D\) Triton 支持可加掩码梯度）。
- 想从更高层看 `normalize` 在整条调用链中的位置？回顾 [u3-l3 FFPAAttnMeta：输入校验与 SDPA 回退判定](./u3-l3-meta-normalize-and-fallback.md)。
- 若你关心 varlen（packed THD）路径的因果掩码（`causal=True`，尾右对齐），可跳读 [u2-l5 变长注意力 ffpa_attn_varlen_func](./u2-l5-varlen-packed-thd.md)。
