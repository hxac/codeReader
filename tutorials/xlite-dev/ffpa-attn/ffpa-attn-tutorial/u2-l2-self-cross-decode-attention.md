# 自注意力 / 交叉注意力 / 解码注意力

## 1. 本讲目标

上一讲（u2-l1）我们拆解了 `ffpa_attn_func` 的签名、`[B, Nh, N, D]` 布局和返回值，默认场景是 query、key、value 三者序列长度相等（`Nq == Nkv`）的自注意力。但真实模型里，query 的长度 `Nq` 经常和 key/value 的长度 `Nkv` 不一样：

- **自注意力（self-attention）**：自己看自己，`Nq == Nkv`。
- **交叉注意力（cross-attention）**：一段 query 去看另一段更长的 key/value，`Nq != Nkv`（例如编码器输出当 KV、解码器当 Q）。
- **解码注意力（decoding attention）**：query 极短（常常 `Nq == 1`）、KV 很长（`Nkv` 很大），典型场景是自回归生成时拿新 token 去扫一整条 KV cache。

本讲学完后你应该能做到：

1. 能清楚地区分 self / cross / decode 三种注意力在 `Nq` 与 `Nkv` 上的关系，并能正确构造「短 query / 长 KV」的输入张量。
2. 理解 FFPA 对 query/key/value 在头数和序列长度上的硬约束（`key` 和 `value` 必须共享相同的 `Nh_kv` 与 `Nkv`）。
3. 理解为什么 FFPA 在解码这种「query 行太少、并行度不够」的场景下，要切到一条专门的 **split-KV（拆分 KV）** kernel 路径，而不是复用 prefill 主 kernel。

## 2. 前置知识

- **注意力公式回顾**：输出 \(\,O=\mathrm{softmax}(s\cdot QK^{\top})V\,\)，其中 \(Q\in\mathbb{R}^{Nq\times D}\)、\(K,V\in\mathbb{R}^{Nkv\times D}\)。注意 \(QK^{\top}\) 的形状是 \(Nq\times Nkv\)——这正是 `Nq` 和 `Nkv` 可以不相等的数学基础：Q 决定行数，KV 决定列数。
- **`Nq` 与 `Nkv` 记号**：本讲统一把 query 序列长度记作 `Nq`，把 key/value 序列长度记作 `Nkv`（上一讲里它们都叫 `N`，本讲因为二者可能不等所以分开命名）。
- **GPU 占用率（occupancy）直觉**：一个 kernel 想跑得快，得让 GPU 上的「流多处理器」（SM）尽可能都忙起来。FFPA 的 prefill 主 kernel 沿 **query 行块**（query row blocks）做并行，每个程序块（program）负责一组 query 行。当 `Nq` 很大时，query 行块很多，能把 SM 填满；当 `Nq==1` 时，query 行块极少，大量 SM 闲置——这就是第 4 节要讲的「为什么要拆 KV」的动机。
- **本讲接续上一讲的关键术语**：`[B, Nh, N, D]` 布局、GQA/MQA、`enable_gqa`、`normalize_inputs`、`FFPAAttnFunc.apply`、fallback 回退。

## 3. 本讲源码地图

本讲涉及的文件很少，但串起了一条「公共入口 → 输入校验 → 前向 kernel 路径选择」的小链路：

| 文件 | 作用 |
| --- | --- |
| `docs/index.md` | 项目首页，含 self / cross / causal 的最小可运行示例与特性对照表，是本讲实践的直接依据。 |
| `src/ffpa_attn/ffpa_attn_interface.py` | 公共入口 `ffpa_attn_func` 的定义与 docstring，明确写了 cross-attention、GQA、causal 的语义和 `key/value` 共享 `Nkv` 的约束。 |
| `src/ffpa_attn/functional.py` | 分发层。`FFPAAttnMeta.fallback()` 决定何时回退 SDPA；`normalize_inputs()` 做形状校验；这两个函数都和 `Nq`、`Nkv` 直接相关。 |
| `src/ffpa_attn/triton/_ffpa_fwd.py` | 默认 Triton 后端的前向 kernel。`_ffpa_attn_forward_impl` 在 **generic（prefill）** 与 **decode（split-KV）** 两条路径之间做选择，是本讲「为什么 Nq=1 走专门路径」的核心证据。 |

## 4. 核心概念与源码讲解

### 4.1 三种注意力模式：self / cross / decode 的 Nq 与 Nkv 关系

#### 4.1.1 概念说明

回到注意力公式 \(O=\mathrm{softmax}(s\cdot QK^{\top})V\)：

- \(QK^{\top}\in\mathbb{R}^{Nq\times Nkv}\) 的 **行数由 Q 决定**、**列数由 KV 决定**。
- 这意味着只要 \(Q\) 和 \(K,V\) 的最后一维（`head_dim` \(D\)）一致，\(Nq\) 和 \(Nkv\) 完全可以不相等，矩阵乘在数学上始终成立。

据此可以把三种模式用 `Nq` 和 `Nkv` 的关系区分清楚：

| 模式 | 典型 `Nq` vs `Nkv` | 直觉类比 | 例子 |
| --- | --- | --- | --- |
| 自注意力 self | `Nq == Nkv` | 「自己看自己全篇」 | 预填充阶段一次性算整段序列 |
| 交叉注意力 cross | `Nq != Nkv`（通常 `Nq < Nkv`） | 「拿着短问题去长文档里找答案」 | 解码器 Q 看编码器输出的 KV |
| 解码注意力 decode | `Nq` 极小（常 `Nq == 1`），`Nkv` 很大 | 「每生成一个新 token，就去扫一遍历史 KV cache」 | 自回归生成的单步前向 |

注意：cross 和 decode 在 API 层面 **没有分开的函数**，它们都是用同一个 `ffpa_attn_func`、靠 `Nq != Nkv` 这一形状特征来体现的。FFPA 内部才会根据 `Nq` 的大小决定走哪条 kernel（见 4.4）。

#### 4.1.2 核心流程

对一个 `[B, Nh, N, D]` 的调用，三种模式在数学上只差 `Nq`、`Nkv` 取值：

```text
self   :  Q:[B, Nh, N,   D]   K,V:[B, Nh, N,   D]   → O:[B, Nh, N,   D]
cross  :  Q:[B, Nh, Nq,  D]   K,V:[B, Nh, Nkv, D]   → O:[B, Nh, Nq,  D]   (Nq != Nkv)
decode :  Q:[B, Nh, 1,   D]   K,V:[B, Nh, Nkv, D]   → O:[B, Nh, 1,   D]   (Nq 极小)
```

输出 `O` 的序列维度 **永远跟 Q 走**（形状为 `[B, Nh_q, Nq, D]`），这是上一讲已经确立的结论。

#### 4.1.3 源码精读

公共入口的 docstring 开宗明义地写明了 cross-attention 与 GQA 的支持，并点明 `key`、`value` 必须共享相同的 `Nh_kv` 与 `Nkv`：

> Supports cross-attention where `query` seqlen (`Nq`) differs from `key`/`value` seqlen (`Nkv`) ... `key` and `value` must share the same `Nh_kv` and the same `Nkv`.

这段说明位于函数 docstring 中：[src/ffpa_attn/ffpa_attn_interface.py:89-95](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ffpa_attn_interface.py#L89-L95)。

函数签名本身仍然只有一个统一的 `ffpa_attn_func`，并没有为 cross/decode 单独开接口；cross 与 decode 仅靠传入张量的形状（`Nq != Nkv`）来体现：[src/ffpa_attn/ffpa_attn_interface.py:71-81](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ffpa_attn_interface.py#L71-L81)。

`docs/index.md` 顶部的特性表也用 `Nq=Nkv`、`Nq!=Nkv` 把 self 与 cross 两种模式列成了并排特性：[docs/index.md:17-21](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/index.md#L17-L21)。

#### 4.1.4 代码实践

**实践目标**：不写代码，先用形状把三种模式区分清楚。

**操作步骤**：对照上面的三种形状表，填写下表的「输出形状」一列（假设 `B=2, Nh=8, D=512`）。

| 模式 | `Q.shape` | `K.shape`=`V.shape` | `O.shape`？ |
| --- | --- | --- | --- |
| self | `[2,8,4096,512]` | `[2,8,4096,512]` | （自填） |
| cross | `[2,8,128,512]` | `[2,8,8192,512]` | （自填） |
| decode | `[2,8,1,512]` | `[2,8,8192,512]` | （自填） |

**预期结果**：三行的 `O.shape` 分别为 `[2,8,4096,512]`、`[2,8,128,512]`、`[2,8,1,512]`——即输出序列维 **始终跟 Q 的 `Nq` 走**。

#### 4.1.5 小练习与答案

**练习 1**：如果 `Nq=0`（空 query），`O` 的形状是什么？FFPA 会接受吗？

> **答案**：`O` 形状会是 `[B, Nh_q, 0, D]`（零长序列维度）。但空序列在实际 kernel 里属于边界情况，本讲不展开，建议当作「待本地验证」的非典型用例。

**练习 2**：cross-attention 里 `Nq > Nkv`（query 比 KV 还长）合法吗？

> **答案**：数学上合法（\(QK^{\top}\) 仍是 \(Nq\times Nkv\)），FFPA 的形状校验也不禁止 `Nq > Nkv`。只要满足 4.2 的约束即可；不过工程上更常见的是 `Nq < Nkv`。

### 4.2 共享 Nkv 约束与输入校验

#### 4.2.1 概念说明

「`Nq` 可以不等于 `Nkv`」不等于「随便填」。FFPA 对 key/value 有两条硬约束：

1. **`key` 和 `value` 必须共享同一个 `Nh_kv`**（头数相同）。
2. **`key` 和 `value` 必须共享同一个 `Nkv`**（序列长度相同）。

第二条尤其重要：在 cross/decode 里，Q 的 `Nq` 可以随便变，但 K 和 V 必须是「成对」的——它们要描述同一段 KV 序列，长度当然得一致，否则 \(QK^{\top}\) 算出的注意力权重（长度跟 `Nkv` 走）没法再去加权 `V`（如果 `V` 的长度跟 `K` 不同，加权就错位了）。

#### 4.2.2 核心流程

`ffpa_attn_func` 的执行三步走（承接上一讲）：先 `fallback` 短路回退、再 `normalize` 校验、最后 `FFPAAttnFunc.apply` 进入 autograd 边界。本节关心 `normalize` 里的形状校验，它会把所有不合法的 `Nq`/`Nkv` 组合一一拦下：

```text
ffpa_attn_func(q,k,v,...)
  └─ meta.normalize_inputs(...)   # 校验：4-D、同 batch、K/V 同 Nh_kv、K/V 同 Nkv、同 D、GQA、causal...
        └─ 不合法 → 抛 ValueError
```

#### 4.2.3 源码精读

`normalize_inputs` 里明确校验「key 和 value 必须共享同一个序列长度」，否则抛 `ValueError`：

```python
if key.size(2) != value.size(2):
  raise ValueError(
    f"key and value must share the same seqlen, got Nk={key.size(2)}, Nv={value.size(2)}"
  )
```

这段代码在：[src/ffpa_attn/functional.py:606-609](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L606-L609)。

注意：这里只要求 **K 与 V** 的 `Nkv` 相等，并没有要求 `Nq == Nkv`——所以 cross/decode（`Nq != Nkv`）天然是合法的。同一函数里还校验了「K 与 V 头数相等」（[src/ffpa_attn/functional.py:596-600](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L596-L600)）与「Q 头数整除 K 头数」的 GQA 约束（[src/ffpa_attn/functional.py:601-605](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L601-L605)）。

此外，`is_causal=True` 时要求 `Nkv >= Nq`（query 对齐到 KV 尾部），否则报错——这在 cross/decode 场景尤其要注意（解码时 `Nq` 小、`Nkv` 大，天然满足）：

```python
if is_causal and key.size(2) < query.size(2):
  raise ValueError(
    f"is_causal=True requires Nkv >= Nq (queries are aligned to the KV tail), "
    f"got Nq={query.size(2)}, Nkv={key.size(2)}"
  )
```

代码位置：[src/ffpa_attn/functional.py:620-624](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L620-L624)。

#### 4.2.4 代码实践

**实践目标**：亲手触发一次 `Nkv` 不匹配的报错，理解校验在哪一层。

**操作步骤**（示例代码，需在带 CUDA 的 GPU 环境运行，结果待本地验证）：

```python
import torch
from ffpa_attn import ffpa_attn_func

B, H, D = 1, 8, 512
q = torch.randn(B, H, 128, D, dtype=torch.bfloat16, device="cuda")
# 故意让 K 和 V 的序列长度不一致：Nk=8192, Nv=4096
k = torch.randn(B, H, 8192, D, dtype=torch.bfloat16, device="cuda")
v = torch.randn(B, H, 4096, D, dtype=torch.bfloat16, device="cuda")

ffpa_attn_func(q, k, v)  # 期望抛 ValueError: key and value must share the same seqlen ...
```

**需要观察的现象**：调用应直接抛出 `ValueError`，且报错信息形如 `key and value must share the same seqlen, got Nk=8192, Nv=4096`。

**预期结果**（待本地验证）：报错信息与 [src/ffpa_attn/functional.py:606-609](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L606-L609) 完全一致——说明校验发生在 `normalize_inputs`，而不是 kernel 内部。

#### 4.2.5 小练习与答案

**练习**：为什么 FFPA 要求 `K` 和 `V` 共享 `Nkv`，却不要求 `Q` 的 `Nq` 等于 `Nkv`？

> **答案**：因为 \(O=\mathrm{softmax}(s\cdot QK^{\top})V\)。\(QK^{\top}\) 把注意力权重的列数钉死为 `Nkv`，加权 `V` 时 `V` 的行数必须等于这个 `Nkv`，所以 `K`、`V` 必须 `Nkv` 相等；而 \(Q\) 只决定权重的「行数」`Nq`，与 `Nkv` 无关，故 `Nq` 可自由变化，这正是 cross/decode 的依据。

### 4.3 构造短 query / 长 KV 输入：cross-attn 示例

#### 4.3.1 概念说明

把 4.1 的概念落到代码：要构造一个 cross / decode 输入，只要在 `[B, Nh, N, D]` 布局里把 `Q` 的第三维填成 `Nq`、把 `K`/`V` 的第三维填成 `Nkv` 即可，其余（batch、头数、head_dim、dtype）按上一讲的约束保持一致。FFPA 的输出会与 PyTorch 原生 `F.scaled_dot_product_attention`（SDPA）数值接近，可以用 `max_abs_err` 来核对。

#### 4.3.2 核心流程

```text
1. 用相同 dtype(fp16/bf16)、相同 device 构造 Q[Nq]、K[Nkv]、V[Nkv]
2. out = ffpa_attn_func(q, k, v)            # 走 FFPA 大 D 路径
3. ref = F.scaled_dot_product_attention(q,k,v)  # SDPA 参考值
4. 比较 (out - ref).abs().max()              # max_abs_err 应很小
```

#### 4.3.3 源码精读

`docs/index.md` 给出了可直接复现的 **Cross-Attention / Decoding-Attention** 示例，注释里就写明了 `Nq can differ from Nkv but Nk==Nv required`（这正是 4.2 的约束）：

```python
# Short-query / long-KV, e.g. incremental decoding or cross-attention:
# Q: [B, H, Nq, D], K/V: [B, H, Nkv, D]; Nq can differ from Nkv but Nk==Nv required.
B, H, D = 1, 8, 512
Nq, Nkv = 128, 8192
q = torch.randn(B, H, Nq,  D, dtype=torch.bfloat16, device="cuda")
k = torch.randn(B, H, Nkv, D, dtype=torch.bfloat16, device="cuda")
v = torch.randn(B, H, Nkv, D, dtype=torch.bfloat16, device="cuda")

out = ffpa_attn_func(q, k, v)  # -> (B, H, Nq, D) = (1, 8, 128, 512)
```

完整示例与 SDPA 误差对比见：[docs/index.md:81-102](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/index.md#L81-L102)。注意它把 cross 与 decoding 放在同一个示例块里讲——因为二者 API 用法完全一样，区别只在 `Nq` 的大小。

`docs/index.md` 还有一条 NOTE 概括了 cross/GQA/causal 三类语义，明确「K/V 必须共享相同的 `Nh_kv` 与 `Nkv`」，可作为速查：[docs/index.md:56-57](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/index.md#L56-L57)。

#### 4.3.4 代码实践

**实践目标**：复现 docs 的 cross-attn 示例（`Nq=128, Nkv=8192, D=512`），再额外测一个 `Nq=1` 的解码用例，对比 FFPA 与 SDPA。

**操作步骤**（需 CUDA GPU，结果待本地验证）：

```python
import torch
import torch.nn.functional as F
from ffpa_attn import ffpa_attn_func

def compare(Nq, Nkv, D=512, B=1, H=8):
    q = torch.randn(B, H, Nq,  D, dtype=torch.bfloat16, device="cuda")
    k = torch.randn(B, H, Nkv, D, dtype=torch.bfloat16, device="cuda")
    v = torch.randn(B, H, Nkv, D, dtype=torch.bfloat16, device="cuda")
    out = ffpa_attn_func(q, k, v)
    ref = F.scaled_dot_product_attention(q, k, v)
    err = (out - ref).abs().max().item()
    print(f"Nq={Nq:>4}, Nkv={Nkv:>5} -> out.shape={tuple(out.shape)}, "
          f"max_abs_err={err:.4e}")

# (1) cross-attention：复现 docs 示例
compare(Nq=128, Nkv=8192)
# (2) decoding：Nq=1 的极端短 query
compare(Nq=1,   Nkv=8192)
```

**需要观察的现象**：

- 两次调用的 `out.shape` 分别是 `(1,8,128,512)` 与 `(1,8,1,512)`——序列维跟 `Nq` 走。
- 两个 `max_abs_err` 都应该在 bf16 量级（通常 `1e-2 ~ 1e-1` 量级，随硬件略有差异），说明 FFPA 与 SDPA 数值一致。

**预期结果**（待本地验证）：`max_abs_err` 为一个小正数；形状与上述一致。

> ⚠️ 注意 fallback：当 `Nq < 512` 或 `Nkv < 512` 时，FFPA 默认会 **回退到 SDPA**（见 [src/ffpa_attn/functional.py:515-522](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L515-L522)）。上面的 `Nq=128` 用例满足 `8 <= Nq < 512`，因此 **会触发回退**——此时 `out` 实际就是 SDPA 算的，`max_abs_err` 接近 0 是正常的。要把 cross 真正留在 FFPA kernel 上，需要 `Nq >= 512`（见综合实践）。`Nq=1` 的解码用例同样会因 `Nq<512` 回退；如果你想让解码也走 FFPA decode kernel，需用 4.4 介绍的机制并在满足回退判定的前提下观察。

#### 4.3.5 小练习与答案

**练习**：把上面 `compare` 的 `Nq` 改成 `8192`、`Nkv` 改成 `128`（query 比 KV 还长），会发生什么？

> **答案**：形状校验通过（不禁止 `Nq > Nkv`），`out.shape=(1,8,8192,512)`，`max_abs_err` 仍是小量。注意此时 `Nkv=128 < 512` 会触发回退 SDPA（见 [src/ffpa_attn/functional.py:521](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L521) 的 `Nkv < 512` 条件）。

### 4.4 解码专用路径：为什么 Nq=1 走 split-KV

#### 4.4.1 概念说明

这是本讲最关键的问题。直觉上，FFPA 既然有一个能处理任意 `Nq`/`Nkv` 的 prefill 主 kernel（generic 路径），为什么解码（`Nq` 很小）还要单独走一条 **split-KV** 路径？原因在于 **GPU 并行度（occupancy）**：

- prefill 主 kernel 沿 **query 行块** 做并行：每个 program 负责一段 query 行（block of `Nq`），网格的并行度 ≈ `batch × heads × cdiv(Nq, BLOCK_M)`。
- 当 `Nq` 很大（如 8192）时，这个乘积远大于 SM 数，能把 GPU 填满，效率高。
- 当 `Nq==1` 时，query 行块极少（`cdiv(1,64)==1`），并行度 ≈ `batch × heads`，常常远小于 SM 数，**大量 SM 闲置**，prefill kernel 会严重浪费算力。

解决办法是 **沿 KV 维度再切一刀**（split-KV）：把长 KV 切成若干 chunk，让多个 program 各算一段 KV 的「部分输出」与「局部 log-sum-exp」，最后再用一个小 kernel 把这些部分结果在 **对数域** 合并。这样就把并行度从「query 行块数」放大到「query 行块数 × KV chunk 数」，重新把 SM 填满。

#### 4.4.2 核心流程

split-KV 两阶段：

```text
阶段 1（stage1）：把 KV 切成 num_splits 个 chunk
  每个 program 处理 (chunk_idx, batch×heads, Q 块)
  → 输出 partial_out:[B, H, num_splits, Nq, D]（fp32） 与 chunk_lse:[B, H, num_splits, Nq]

阶段 2（stage2）：跨 chunk 合并
  用 log-sum-exp 把各 chunk 的 partial_out 按 chunk_lse 加权求和 → 最终 O:[B, H, Nq, D]
```

对数域合并的原理：每个 chunk 算出的是 \(\mathrm{softmax}\) 在一段 KV 子集上的归一化结果与对应的 \(\log\sum\exp\)（即 LSE）。要把多段合并，需要用 LSE 做加权（数学上等价于把分段 softmax 拼回全局 softmax），公式形如：

\[
O = \sum_{c} \mathrm{softmax}_c\cdot \exp(\mathrm{lse}_c)\, \bigg/ \sum_{c}\exp(\mathrm{lse}_c)
\]

这正是 FlashAttention 系列「split-KV / FlashDecoding」的标准做法。

**何时触发 split-KV？** 由一个占用率启发式决定：当 query 行块提供的并行度 `batch × heads × num_m_blocks` 已经 ≥ GPU 可用 SM 数的 80% 时，说明 prefill 主 kernel 自己就能填满 GPU，于是 `num_splits=1`（不切，走 generic）；否则就切 KV 来补并行度。注意 `num_splits==1` 并不等于「decode 路径」，而是走 **generic（prefill）** 路径——只有 `num_splits>1` 才进 decode 路径。

#### 4.4.3 源码精读

`_ffpa_attn_forward_impl` 是默认 Triton 后端的低层前向入口，它在 generic 与 decode 之间二选一。先调用 `_get_decode_num_splits` 算出 `num_splits`，再据此分流：

```python
num_splits = _get_decode_num_splits(
  seqlen_q, seqlen_k, headdim, batch, nheads_q, q.device
)
...
if num_splits == 1:
  _ffpa_attn_forward_generic_impl(...)   # prefill 主 kernel
  return

_ffpa_attn_forward_decode_impl(..., num_splits=num_splits, ...)  # split-KV
```

这段分流逻辑在：[src/ffpa_attn/triton/_ffpa_fwd.py:1384-1444](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L1384-L1444)（关键判定在 [L1411-L1427](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L1411-L1427) 的 `if num_splits == 1` 分支与随后的 decode 调用）。

`_get_decode_num_splits` 把「可用 SM 数 ×2」当作并行预算，并用 `cdiv(Nq,64)` 估算 query 行块数，再交给启发式 `_decode_num_splits_heuristic`：

```python
num_m_blocks = triton.cdiv(seqlen_q, 64)
num_splits = _decode_num_splits_heuristic(
  batch * nheads_q * num_m_blocks,   # query 行块提供的并行度
  num_sms,
  num_n_blocks,
  max_splits=128,
)
```

代码位置：[src/ffpa_attn/triton/_ffpa_fwd.py:262-284](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L262-L284)。从这里能直接看出：**`Nq` 越小，`num_m_blocks` 越小，并行度越低，就越可能切 KV**。`Nq==1` 时 `num_m_blocks==1`，是最容易触发 split 的情形。

启发式里的「80% 阈值」是决定 `num_splits==1` 的关键一行：

```python
if batch_nheads_mblocks >= 0.8 * num_sms:
  return 1     # 并行度已足够，不切，走 generic（prefill）
```

见 [src/ffpa_attn/triton/_ffpa_fwd.py:228-229](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L228-L229)。换言之：只有当 query 行块填不满 80% 的 SM 时，才会进入 split 搜索、最终走 decode 路径。

在 decode 路径里，`partial_out` 与 `chunk_lse` 的形状就直接体现了「按 chunk 切」的设计：

```python
n_chunks = num_splits
chunk_size = triton.cdiv(seqlen_k, n_chunks)
...
partial_out = torch.empty((batch, nheads_q, n_chunks, seqlen_q, headdim), ...)  # 每 chunk 一份部分 O
chunk_lse  = torch.empty((batch, nheads_q, n_chunks, seqlen_q), ...)            # 每 chunk 一份局部 LSE
```

见 [src/ffpa_attn/triton/_ffpa_fwd.py:1092-1106](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L1092-L1106)。stage1 的网格也把 `chunk_idx` 作为第一维，正是为了让多个 program 并行算不同 KV 段：

```python
def stage1_grid(meta):
  return (
    triton.cdiv(seqlen_k, meta["CHUNK_SIZE"]),  # chunk 维（=num_splits 等价的 KV 块数）
    batch * nheads_q,
    triton.cdiv(seqlen_q, meta["BLOCK_M"]),
  )
```

见 [src/ffpa_attn/triton/_ffpa_fwd.py:1110-1115](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L1110-L1115)。

此外，`Nq==1` 时还有更细的特化：`USE_GEMV` 分支用「向量归约」替代「矩阵 tile」，避免单行 query 还去付 MMA tile 的开销：

```python
if USE_GEMV:  # gemv
  # Single-query decode path. Use vector reductions instead of MMA tiles so a
  # one-row query does not pay matrix-tile overhead.
```

见 [src/ffpa_attn/triton/_ffpa_fwd.py:582-588](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L582-L588)。这就是「`Nq==1` 用 GEMV 更快」的源码出处。

> 术语小词典：
> - **split-KV / FlashDecoding**：沿 KV 维度切分以提升并行度的技巧。
> - **LSE（log-sum-exp）**：\(\log\sum_i\exp(s_i)\)，softmax 分母的对数，用于分段结果在对数域安全合并。
> - **GEMV**：matrix-vector 乘法（这里 Q 只有 1 行，\(QK^{\top}\) 退化为向量点积）。

#### 4.4.4 代码实践

**实践目标**：不运行 kernel，而是 **读懂分流逻辑**，亲手用启发式判断两种形状分别走哪条路径。

**操作步骤**：

1. 阅读 [src/ffpa_attn/triton/_ffpa_fwd.py:262-284](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L262-L284) 与 [L228-L229](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L228-L229) 的阈值规则。
2. 假设 GPU 有 148 个 SM（`num_sms = 148*2 = 296`），`D=512`（故 `block_n=64`），对下表两种形状手算 `batch × heads × num_m_blocks`，判断是否 `>= 0.8 * num_sms`，并给出 `num_splits` 与最终走的路径。

| 形状 | `num_m_blocks=cdiv(Nq,64)` | `batch×heads×num_m_blocks` | `>=0.8*296`? | `num_splits` | 路径 |
| --- | --- | --- | --- | --- | --- |
| `B=1,H=32,Nq=8192,Nkv=8192` | （自算） | （自算） | （自判） | （自判） | （generic/decode?） |
| `B=1,H=32,Nq=1,Nkv=8192` | （自算） | （自算） | （自判） | （自判） | （generic/decode?） |

**需要观察的现象 / 预期结果**（待本地验证，但手算结论是确定的）：

- 形状 1：`num_m_blocks=128`，`batch×heads×num_m_blocks=1×32×128=4096 ≫ 0.8×296≈237`，故 `num_splits=1` → **generic（prefill）路径**。
- 形状 2：`num_m_blocks=1`，`1×32×1=32 < 237`，触发 split 搜索 → `num_splits>1` → **decode（split-KV）路径**。

这正是「`Nq==1` 走专门路径」的本质：query 太少、并行度不够，于是切 KV 来补。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `batch` 或 `heads` 加大（比如 `B=8, H=32`）保持 `Nq=1`，是否还一定走 decode 路径？

> **答案**：不一定。`batch×heads×num_m_blocks = 8×32×1 = 256`，已接近 `0.8×296≈237` 的阈值。若 `B×H` 大到让该乘积 ≥ 阈值，则 `num_splits=1`，走 generic。这说明 **是否走 decode 取决于「并行度是否够」，而不是「Nq 是否为 1」本身**——`Nq==1` 只是通常会让并行度不够。

**练习 2**：为什么 split-KV 的合并必须在 **对数域**（用 LSE）做，而不是直接把各 chunk 的 `partial_out` 加起来？

> **答案**：每个 chunk 的 `partial_out` 已经被它那段 KV 子集的 softmax 分母归一化过了，分母不同，直接相加会丢失全局归一化。用 LSE 把各段分母的贡献 `exp(lse_c)` 当作权重重新归一，才能等价于在全局 KV 上做 softmax。

## 5. 综合实践

把本讲三个要点（三种 `Nq`/`Nkv` 关系、共享 `Nkv` 约束、decode split-KV 触发条件）串起来，完成下面这个小任务（需 CUDA GPU，结果待本地验证）：

1. **构造一组真正会留在 FFPA kernel 上的 cross-attention 输入**：要求 `Nq >= 512` 且 `Nkv >= 512`（避免被 [src/ffpa_attn/functional.py:515-522](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L515-L522) 的 fallback 拦截），例如 `B=1, H=8, Nq=1024, Nkv=8192, D=512`，调用 `ffpa_attn_func` 并与 SDPA 对比 `max_abs_err`。

2. **画一张分流决策图**：以 `num_m_blocks = cdiv(Nq,64)` 与阈值 `0.8 × num_sms` 为依据，画出 `ffpa_attn_func → fallback? → normalize → FFPAAttnFunc.forward(triton) → _ffpa_attn_forward_impl → (num_splits==1 ? generic : decode)` 这条完整链路，并在每个分叉点标注判定条件与对应源码行号。

3. **解释一个反直觉现象**：用你的决策图说明「为什么 `Nq=128, Nkv=8192, D=512` 这个看似标准的 cross-attn 用例，默认却不会留在 FFPA kernel 上」（提示：`8 <= Nq < 512` 触发 fallback）。并回答：若希望它留在 FFPA，除了放大 `Nq` 还有什么办法？（提示方向：可阅读 [src/ffpa_attn/functional.py:515-522](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L515-L522) 里的 fallback 是否依赖 `forward_meta`，但这属于后续 u3 单元「分发层与回退判定」的内容，本讲点到为止。）

## 6. 本讲小结

- 注意力的 `Nq`（query 行数）与 `Nkv`（KV 列数）可以不相等：**self** 是 `Nq==Nkv`，**cross** 是 `Nq!=Nkv`（常 `Nq<Nkv`），**decode** 是 `Nq` 极小（常 `Nq==1`）；输出 `O` 的序列维永远跟 `Nq` 走。
- FFPA 用 **同一个 `ffpa_attn_func`** 服务这三种模式，cross/decode 仅靠传入张量的形状来体现，没有单独的 API。
- 硬约束：`key` 和 `value` 必须共享相同的 `Nh_kv` 与 `Nkv`（否则在 `normalize_inputs` 抛 `ValueError`）；`is_causal=True` 还要求 `Nkv>=Nq`。
- `8 <= Nq < 512` 或 `Nkv < 512` 会触发 **回退 SDPA**，所以「小 `Nq`」的 cross/decode 默认可能并不真的跑在 FFPA kernel 上。
- decode 之所以走专门的 **split-KV** 路径，是因为 query 行太少时 prefill 主 kernel 并行度不足以填满 GPU；于是沿 KV 再切一刀、用 LSE 在对数域合并，把并行度补回来。是否切由 `_get_decode_num_splits` 的占用率启发式（`0.8 × num_sms` 阈值）决定。
- `Nq==1` 时还会进一步走 `USE_GEMV` 特化，用向量归约替代 MMA tile，避免单行 query 的 tile 开销。

## 7. 下一步学习建议

- 本讲多次碰到 **fallback 回退 SDPA** 与 `forward_meta`，但只点到为止。建议下一站进入第 3 单元，精读 `FFPAAttnMeta.fallback()`（[src/ffpa_attn/functional.py:474-522](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L474-L522)）与 `normalize_inputs`，搞清「到底什么形状会留在 FFPA、什么形状会回退」。
- 本讲的 decode split-KV 是「公共 API 视角」的概览；想看 stage1/stage2 kernel 内部的真实循环、LSE 合并与 `USE_GEMV` 实现，可继续阅读 [src/ffpa_attn/triton/_ffpa_fwd.py:494-828](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L494-L828)，对应第 4 单元「Triton 后端前向」中 decode 那一篇讲义。
- 若你对变长（packed THD）注意力感兴趣，可预习 `ffpa_attn_varlen_func`（[src/ffpa_attn/ffpa_attn_interface.py:184-271](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ffpa_attn_interface.py#L184-L271)），它是另一种「不等长」的处理方式（用 `cu_seqlens` 描述每个序列边界），对应第 2 单元的 varlen 讲义。
