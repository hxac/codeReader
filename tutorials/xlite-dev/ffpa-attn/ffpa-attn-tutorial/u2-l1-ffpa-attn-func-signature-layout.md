# ffpa_attn_func 签名、张量布局与返回

## 1. 本讲目标

本讲是「公共 API 与典型使用场景」单元的第一讲。学完后你应当能够：

- 看懂 `ffpa_attn_func` 的完整函数签名，并说出每个参数的作用。
- 正确构造 `[B, Nh, N, D]` 布局的 `query / key / value` 张量，知道每一维代表什么。
- 理解 `Nh_q` 必须是 `Nh_kv` 整数倍的约束从何而来。
- 掌握 `scale / is_causal / dropout_p / enable_gqa / attn_mask` 这几个关键参数的语义。
- 知道 `ffpa_attn_func` 返回的张量 `O` 的形状与 dtype，以及它与 SDPA 在数值上为何接近。

本讲只聚焦「**怎么正确地调用**」这一件事，不展开后端分发与 kernel 内部细节（那是后续单元的主题）。

## 2. 前置知识

在阅读本讲前，你需要对以下概念有最基本的印象。如果完全陌生，先看本节的通俗解释。

### 2.1 缩放点积注意力（Scaled Dot-Product Attention, SDPA）

给定查询 `Q`、键 `K`、值 `V`，注意力的核心公式是：

\[
O = \mathrm{softmax}\!\left(\text{scale} \cdot QK^{\top}\right) V
\]

其中 `QK^T` 计算每个 query 与所有 key 的相似度（点积），`softmax` 把相似度归一成权重，再乘 `V` 得到加权输出。`scale` 通常取 \(1/\sqrt{D}\)，目的是防止点积值过大导致 softmax 梯度消失。PyTorch 提供了官方实现 `torch.nn.functional.scaled_dot_product_attention`（简称 **SDPA**），FFPA 的签名就是与它对齐的。

### 2.2 张量布局 `[B, Nh, N, D]`

注意力输入通常是一个四维张量，四个维度分别是：

| 维度 | 含义 | 典型记法 |
|:---:|:---|:---|
| `B` | batch size，批大小 | `size(0)` |
| `Nh` | num_heads，注意力头数 | `size(1)` |
| `N` | seq_len，序列长度 | `size(2)` |
| `D` | head_dim，每个头的特征维度 | `size(3)` |

注意：FFPA 用的就是 SDPA 的 `(B, H, N, D)` 布局，而 FlashAttention 习惯用 `(B, N, H, D)`。FFPA 在内部需要时会自己做转换，对外只暴露 SDPA 布局。

### 2.3 半精度 fp16 / bf16

深度学习推理/训练常用 16 位浮点以节省显存、加速计算：

- `torch.float16`（fp16）：1 位符号 + 5 位指数 + 10 位尾数，数值范围小、精度高。
- `torch.bfloat16`（bf16）：1 位符号 + 8 位指数 + 7 位尾数，数值范围与 fp32 相同、精度略低。

FFPA 的 kernel 只针对这两种 dtype 做了优化，`fp32` 输入会被拒绝。

### 2.4 GQA / MQA（分组查询注意力）

- **MHA（多头注意力）**：`Q/K/V` 头数相同。
- **GQA（Grouped-Query Attention）**：`Q` 的头数 `Nh_q` 多于 `K/V` 的头数 `Nh_kv`，且 `Nh_q` 是 `Nh_kv` 的整数倍，`group_size = Nh_q / Nh_kv`。同一组里的若干个 Q 头共享同一对 K/V 头，能省显存又不掉太多精度（Llama-3 常用 32/8）。
- **MQA（Multi-Query Attention）**：GQA 的极端特例，`Nh_kv = 1`，所有 Q 头共享一对 K/V。

### 2.5 与上一讲的衔接

上一讲（u1-l4）你已经知道：`ffpa_attn_func` 可以一行 monkey-patch 接管 SDPA，内部先做 `fallback()` 判定——`D ≤ 256`、`D > 1024`、`Nq < 512` 等情形会**静默回退到原生 SDPA**，否则进入 FFPA kernel。本讲不再重复回退逻辑，而是把镜头对准**回退通过之后**的那条主路径：签名怎么读、张量怎么摆、参数怎么填、返回什么。

## 3. 本讲源码地图

本讲涉及的文件很少，都是「公共入口」层面：

| 文件 | 作用 |
|:---|:---|
| [src/ffpa_attn/ffpa_attn_interface.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ffpa_attn_interface.py) | 定义 `ffpa_attn_func` 与 `ffpa_attn_varlen_func` 两个公共入口，是本讲主角。签名、docstring、回退/校验/分发都在这里。 |
| [src/ffpa_attn/functional.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py) | `FFPAAttnMeta.normalize_inputs()` 在这里做形状/dtype/GQA/causal/scale 的具体校验。本讲会引用其中几段。 |
| [src/ffpa_attn/__init__.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/__init__.py) | 把 `ffpa_attn_func` 从顶层包导出，所以你能 `from ffpa_attn import ffpa_attn_func`。 |
| [docs/index.md](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/index.md) | 文档首页，含 self/cross/gqa/causal/backward 五个最小示例，本讲复现其中的 self-attention 示例。 |

> 本讲**不会**深入 `functional.py` 里的 `FFPAAttnFunc`（autograd Function）和后端分发——那属于 u3 单元。本讲只用到 `normalize_inputs` 里能直接说明签名约束的那几行。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **4.1** `ffpa_attn_func` 签名全貌与整体调用流程
2. **4.2** 张量布局 `[B, Nh, N, D]` 与 GQA / 形状约束
3. **4.3** dtype 约束（fp16/bf16）与 `scale` 默认值
4. **4.4** 关键参数语义与返回值

### 4.1 ffpa_attn_func 签名全貌与整体调用流程

#### 4.1.1 概念说明

`ffpa_attn_func` 是 FFPA 对外暴露的**密集注意力**（dense attention）主入口。它有两个设计原则：

1. **签名与 `torch.nn.functional.scaled_dot_product_attention` 对齐**——前三个位置参数是 `query / key / value`，后续是同名关键字。这样它才能被一行 monkey-patch 无缝替换 SDPA（见上一讲）。
2. **Python 层尽量薄**——只做「回退判定 → 校验归一化 → 交给 autograd Function」，真正的 kernel 计算都在后端。

#### 4.1.2 核心流程

`ffpa_attn_func` 函数体只有三步（伪代码）：

```text
def ffpa_attn_func(query, key, value, attn_mask=None, dropout_p=0.0,
                   is_causal=False, scale=None, enable_gqa=False, **kwargs):
    1. meta = FFPAAttnMeta.from_kwargs(**kwargs)        # 解析 backend 等扩展参数
    2. if meta.fallback(...):                            # 小 D 等情形
         return 原生 SDPA(query, key, value, ...)         #   回退，且不递归
    3. meta, q, k, v, attn_bias = meta.normalize(...)    # 校验 + 归一化掩码
       return FFPAAttnFunc.apply(q, k, v, attn_bias, meta)  # 进入 autograd 边界
```

关键点：

- 第 2 步的回退**必须调用底层 C++ 绑定** `torch._C._nn.scaled_dot_product_attention`，而不是被 patch 过的 Python 符号，否则会无限递归（上一讲已讲）。
- 第 3 步 `normalize` 会做形状/dtype/scale 校验，并把用户给的 `attn_mask` 转成内部的 `attn_bias`。
- `FFPAAttnFunc.apply` 是 `torch.autograd.Function`，前向算 `O`，反向算 `dQ/dK/dV`，本讲只关心它返回的前向 `O`。

#### 4.1.3 源码精读

先看签名本身。注意位置参数 `query/key/value` 后面全部是关键字参数，并且末尾有一个 `**kwargs` 用来接收 FFPA 特有的扩展（如 `backend`、`forward_backend`、`backward_backend`）：

[ffpa_attn_interface.py:71-81](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ffpa_attn_interface.py#L71-L81) — 函数定义。`query/key/value` 是位置参数；`attn_mask/dropout_p/is_causal/scale/enable_gqa` 都是带默认值的关键字参数；`**kwargs` 收纳 FFPA 专有扩展（后端配置）。签名与 SDPA 对齐，末尾 `**kwargs` 承载后端配置。

docstring 开头就点明了「Signature aligned with SDPA」以及「Dispatches by query.dtype (fp16 / bf16)」：

[ffpa_attn_interface.py:82-87](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ffpa_attn_interface.py#L82-L87) — docstring 说明签名对齐 SDPA、按 dtype 分发、保持 Python 层精简以兼容 `torch.compile`。

函数体的三步流程（回退 → 归一化 → apply）：

[ffpa_attn_interface.py:156-181](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ffpa_attn_interface.py#L156-L181) — `fallback` 短路回退 SDPA；否则 `normalize` 校验后交给 `FFPAAttnFunc.apply`。注意 `# HACK: Avoid recursive for monkey-patch usage.` 这行注释解释了为何回退要调底层绑定。

#### 4.1.4 代码实践

**实践目标**：确认 `ffpa_attn_func` 能从顶层包导入，且签名与 SDPA 一致。

**操作步骤**：

1. 确认已按 u1-l2 安装好（`pip install -e . --no-build-isolation` 或 PyPI wheel）。
2. 在 Python 里检查导入与签名：

```python
import inspect
from ffpa_attn import ffpa_attn_func
print(inspect.signature(ffpa_attn_func))
```

**需要观察的现象**：打印出的签名应当是
`(query, key, value, attn_mask=None, dropout_p=0.0, is_causal=False, scale=None, enable_gqa=False, **kwargs)`。

**预期结果**：能正常导入，签名与上文一致。若无 GPU 或环境未装好，导入本身仍可成功（kernel 在调用时才触发），但 `inspect` 需要 `torch` 已安装。

> 若运行报错，请先回到 u1-l2 检查安装步骤。本步骤的具体输出**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ffpa_attn_func` 要把 `attn_mask/dropout_p/...` 都设成关键字参数，而不是位置参数？

**参考答案**：为了与 `torch.nn.functional.scaled_dot_product_attention` 的调用习惯保持一致，这样 monkey-patch 后所有原本 `F.scaled_dot_product_attention(q,k,v, is_causal=True)` 这样的调用都能原样工作。

**练习 2**：函数末尾的 `**kwargs` 用来做什么？如果传一个它不认识的 key 会怎样？

**参考答案**：`**kwargs` 接收 FFPA 专有扩展（`backend / forward_backend / backward_backend`）。docstring 明确说「Any other kwarg raises `TypeError`」，所以传一个未知 key 会在 `FFPAAttnMeta.from_kwargs` 处抛 `TypeError`。

---

### 4.2 张量布局 `[B, Nh, N, D]` 与 GQA / 形状约束

#### 4.2.1 概念说明

`query / key / value` 三个张量都按 `[B, Nh, N, D]` 四维摆放。但「Q 的形状」与「K/V 的形状」不必完全相同——这正是 cross-attention 与 GQA 的来源：

- **序列长度可不同**：`query` 的 `Nq`（`size(2)`）可以≠ `key/value` 的 `Nkv`。例如解码时 `Nq=1`、`Nkv=8192`。
- **头数可不同（GQA/MQA）**：`query` 的 `Nh_q` 可以多于 `key/value` 的 `Nh_kv`，但必须**整除**：`Nh_q % Nh_kv == 0`。

有几个**硬约束**始终成立：

1. 三个张量都必须是 **4 维**。
2. 三个张量的 `batch size`（`size(0)`）必须相同。
3. `key` 和 `value` 的 `Nh_kv`（`size(1)`）必须相同。
4. `key` 和 `value` 的 `Nkv`（`size(2)`）必须相同。
5. 三个张量的 `D`（`size(3)`, head_dim）必须相同。
6. `Nh_q` 必须是 `Nh_kv` 的整数倍。

#### 4.2.2 核心流程

校验全部集中在 `FFPAAttnMeta.normalize_inputs()` 里，按一个固定顺序逐条检查，任何一条不过就抛带提示的 `ValueError`。流程如下（伪代码）：

```text
normalize_inputs(query, key, value, ...):
    if 任何一个不是 4 维:        raise ValueError("must be 4-D [B, H, N, D]")
    if batch size 不一致:        raise ValueError("must share the same batch size")
    if key.Nh != value.Nh:       raise ValueError("key/value must share num_heads")
    if query.Nh % key.Nh != 0:   raise ValueError("Nh_q must be integer multiple of Nh_kv")
    if key.N != value.N:         raise ValueError("key/value must share seqlen")
    if head_dim 三者不一致:       raise ValueError("must share the same head dim")
    if (not enable_gqa) and Nh_q != Nh_kv:  raise ValueError("set enable_gqa=True")
    if is_causal and Nkv < Nq:   raise ValueError("is_causal requires Nkv >= Nq")
    ...
```

注意第 6 条约束（`Nh_q % Nh_kv == 0`）和「`enable_gqa=False` 但头数不等」是**两条不同的检查**：前者是结构性的（头数连整除关系都不满足就直接报错），后者是语义开关（即便整除，只要你没显式说 `enable_gqa=True`，也会报错让你明确意图）。这体现了 FFPA「默认行为与 SDPA 完全一致」的设计取向。

#### 4.2.3 源码精读

docstring 里对三个张量的布局与 GQA 约束有完整说明：

[ffpa_attn_interface.py:106-113](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ffpa_attn_interface.py#L106-L113) — `query` 是 `[B, Nh_q, Nq, D]`；`key/value` 是 `[B, Nh_kv, Nkv, D]`；`Nh_q` 必须是 `Nh_kv` 的整数倍，`group_size = Nh_q / Nh_kv`；`key` 与 `value` 必须共享相同的 `Nh_kv` 与 `Nkv`。

逐条形状校验的源码：

[functional.py:592-611](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L592-L611) — 4 维检查、batch 一致、`key.Nh==value.Nh`、`query.Nh % key.Nh == 0`、`key.N==value.N`、三者 head_dim 一致。

GQA 开关与因果约束两条额外检查：

[functional.py:613-624](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L613-L618) — `enable_gqa=False` 但 `Nh_q != Nh_kv` 时报错，提示「Set `enable_gqa=True` or use matching head counts」。

[functional.py:620-624](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L620-L624) — `is_causal=True` 要求 `Nkv >= Nq`（query 对齐到 KV 尾部的约定，详见 u2-l3）。

#### 4.2.4 代码实践

**实践目标**：用错误形状触发几条校验，熟悉报错信息。

**操作步骤**：

```python
import torch
from ffpa_attn import ffpa_attn_func

# 构造 D=512（走 FFPA 大 D 路径，避开 fallback）
B, D = 1, 512
q = torch.randn(B, 32, 8192, D, dtype=torch.bfloat16, device="cuda")
k = torch.randn(B, 8,  8192, D, dtype=torch.bfloat16, device="cuda")  # Nh_kv=8
v = torch.randn(B, 8,  8192, D, dtype=torch.bfloat16, device="cuda")

# 1) 头数成倍数关系，但没开 enable_gqa -> 应报 ValueError 提示 enable_gqa=True
try:
    ffpa_attn_func(q, k, v)
except ValueError as e:
    print("case1:", e)

# 2) 开了 enable_gqa，应当通过形状校验（会继续走 kernel）
out = ffpa_attn_func(q, k, v, enable_gqa=True)
print("case2 out.shape:", tuple(out.shape))  # 期望 (1, 32, 8192, 512)
```

**需要观察的现象**：

- `case1` 抛 `ValueError`，信息含「`enable_gqa=False but query num_heads ... != key/value num_heads`」。
- `case2` 正常返回，输出形状为 `(1, 32, 8192, 512)`，即与 `query` 形状相同。

**预期结果**：如上。`case1` 的具体报错文字以源码为准；`case2` 需要可用的 CUDA GPU，否则会因 device 不匹配报错——**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`Nh_q=32, Nh_kv=8` 时 `group_size` 是多少？这种配置属于 GQA 还是 MQA？

**参考答案**：`group_size = 32 / 8 = 4`，属于 GQA（Llama-3 风格）。MQA 是 `Nh_kv=1` 的特例，此时 `group_size = Nh_q`。

**练习 2**：如果 `key` 和 `value` 的 `Nkv` 不同（例如 `key` 是 8192、`value` 是 4096），会在哪一步报错？

**参考答案**：在 `normalize_inputs` 检查 `key.size(2) != value.size(2)` 时报 `ValueError`，提示「key and value must share the same seqlen」。因为 `QK^T` 用的 key 长度和 `PV` 用的 value 长度必须一致。

---

### 4.3 dtype 约束（fp16/bf16）与 scale 默认值

#### 4.3.1 概念说明

**dtype 约束**：`query / key / value` 的 dtype 必须是 `torch.float16` 或 `torch.bfloat16` 之一，且三者必须一致。这是因为 FFPA 的 kernel（无论 Triton/CUDA/CuTeDSL）都是针对这两种 16 位浮点写的 MMA（矩阵乘加）指令，`fp32` 输入没有专用快速路径。

**scale 默认值**：`scale` 是作用在 `QK^T` 上的预 softmax 缩放因子。如果调用时不传（`scale=None`），FFPA 会自动取标准注意力缩放：

\[
\text{scale} = \frac{1}{\sqrt{D}}, \quad D = \text{query.size(-1)}
\]

这与 SDPA 的默认行为完全一致。你也可以显式传一个 `float`（例如推理框架里常见的 `query.size(-1) ** -0.5` 或自定义值）。

#### 4.3.2 核心流程

- **dtype 校验**：在 `normalize_inputs` 里，`if query.dtype not in (fp16, bf16): raise TypeError`。注意顺序——这个检查在形状校验之前，所以即使形状也错，dtype 不对会先报 `TypeError`。
- **scale 填充**：在所有形状校验通过后，`normalize_inputs` 根据是否传 `scale` 决定：

```text
if scale is None:
    attn_meta.scale = 1.0 / sqrt(query.size(-1))   # 标准默认
else:
    attn_meta.scale = float(scale)                  # 用户显式值
```

  这个 `attn_meta.scale` 之后会一路传给前向/反向 kernel。

#### 4.3.3 源码精读

dtype 检查（注意它早于形状检查）：

[functional.py:586-589](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L586-L589) — `query.dtype not in (torch.float16, torch.bfloat16)` 时抛 `TypeError`，提示「ffpa_attn_func only supports fp16/bf16」。

scale 默认值填充：

[functional.py:626-629](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L626-L629) — `scale=None` 时取 `1.0 / math.sqrt(query.size(-1))`，否则用 `float(scale)`。

docstring 对 `scale` 的说明：

[ffpa_attn_interface.py:129-130](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ffpa_attn_interface.py#L129-L130) — `scale` 是作用在 `QK^T` 上的预 softmax 缩放因子，`None` 时默认 `1/sqrt(D)`。

> 补充：docstring 的 `:raises TypeError:` 段（[ffpa_attn_interface.py:147](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ffpa_attn_interface.py#L147)）也明确列出「if query.dtype is neither fp16 nor bf16」会抛 `TypeError`。

#### 4.3.4 代码实践

**实践目标**：分别验证 dtype 拒绝与 scale 默认行为。

**操作步骤**：

```python
import torch, math
from ffpa_attn import ffpa_attn_func

B, H, N, D = 1, 8, 4096, 512

# 1) fp32 输入应被拒绝
qf = torch.randn(B, H, N, D, dtype=torch.float32, device="cuda")
try:
    ffpa_attn_func(qf, qf, qf)
except TypeError as e:
    print("fp32 rejected:", e)

# 2) 不传 scale：默认 1/sqrt(D)
q = torch.randn(B, H, N, D, dtype=torch.bfloat16, device="cuda")
out_default = ffpa_attn_func(q, q, q)

# 3) 显式传标准 scale：结果应与默认一致
out_scaled = ffpa_attn_func(q, q, q, scale=1.0 / math.sqrt(D))
print("scale diff:", (out_default - out_scaled).abs().max().item())
```

**需要观察的现象**：

- 第 1 步抛 `TypeError`，含「only supports fp16/bf16」。
- 第 3 步的 `scale diff` 应该是 `0`（或极小的数值噪声，取决于实现是否完全位等价）。

**预期结果**：dtype 拒绝确定发生；`scale diff` 是否严格为 0 **待本地验证**（理论上同一 scale 应得到相同结果，但 kernel 内部归约顺序可能引入微小差异）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 scale 默认取 `1/sqrt(D)` 而不是 `1/D`？

**参考答案**：当 `Q`、`K` 各分量近似独立、均值为 0、方差为 1 时，点积 `Q·K` 的方差正比于 `D`，标准差正比于 `\sqrt{D}`。除以 `\sqrt{D}` 能把点积的标准差归一到 O(1) 量级，使 softmax 处于梯度健康的数值区间。这是「缩放点积注意力」得名的由来。

**练习 2**：如果传入 `scale=0.0` 会发生什么？

**参考答案**：`normalize_inputs` 只在 `scale is None` 时才覆盖默认值，否则用 `float(scale)`，所以 `scale=0.0` 会被原样接受。此时 `scale * QK^T` 全为 0，softmax 输出均匀分布，`O` 等于 `V` 沿序列维度的均值。这不是错误，只是一个退化的注意力。

---

### 4.4 关键参数语义与返回值

#### 4.4.1 概念说明

除了 `query/key/value/scale`，签名里还有几个关键参数，本讲给出**语义层面的速览**（深入实现见后续 u2-l2~l4）：

| 参数 | 类型 | 默认 | 语义 |
|:---|:---|:---:|:---|
| `attn_mask` | `Tensor \| None` | `None` | 注意力掩码。布尔掩码：`True` 表示参与注意力、`False` 映射为 `-inf`；浮点掩码：作为可加偏置（additive bias）。需能广播到 `[B, Nh_q, Nq, Nkv]`。 |
| `dropout_p` | `float` | `0.0` | attention 权重的 dropout 概率，范围 `[0, 1)`，`1.0` 不支持。 |
| `is_causal` | `bool` | `False` | 因果掩码。`True` 时 query 行 `r` 只看 KV 中 `k <= r + (Nkv - Nq)`（query 对齐到 KV 尾部），需 `Nkv >= Nq`。 |
| `enable_gqa` | `bool` | `False` | 是否开启 GQA/MQA。**默认 `False` 以与 SDPA 完全对齐**；头数不等时必须显式传 `True`。 |

**返回值**：单个张量 `O`，形状 `[B, Nh_q, Nq, D]`（与 `query` 完全同形），dtype 与输入一致，内容是 `softmax(scale * QK^T) V`。

> 注意 `ffpa_attn_func` 的返回**只有一个张量**（不像 `ffpa_attn_varlen_func` 可选返回 `lse`）。它是一个普通可微张量，调用 `.backward()` 即可触发 FFPA 反向（见 docs 的 backward 示例）。

#### 4.4.2 核心流程

返回值的形状遵循一个简单原则：**输出形状 = query 形状**。因为注意力的输出是对每个 query 位置、每个头、每个特征维算一个加权和，维度结构与 query 完全一致：

```text
query : [B, Nh_q, Nq, D]
key   : [B, Nh_kv, Nkv, D]
value : [B, Nh_kv, Nkv, D]
---------------------------------
O     : [B, Nh_q, Nq, D]   # 与 query 同形，dtype 也相同
```

即便 `Nq != Nkv`（cross/decode）或 `Nh_q != Nh_kv`（GQA），输出形状也只跟随 query。GQA 时每个 Q 头的结果由它所属 group 共享的那对 K/V 头算出，但**输出头数仍是 `Nh_q`**。

#### 4.4.3 源码精读

`is_causal` 的语义在 docstring 里有精确表述：

[ffpa_attn_interface.py:124-128](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ffpa_attn_interface.py#L124-L128) — `is_causal=True` 时 query 行 `r` 只看 `k <= r + (Nkv - Nq)`（尾对齐约定），需 `Nkv >= Nq`。

`enable_gqa` 默认 `False` 的设计意图：

[ffpa_attn_interface.py:131-134](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ffpa_attn_interface.py#L131-L134) — 默认 `False` 以精确匹配 SDPA；头数不等时需显式传 `True` 才进入 GQA/MQA 语义。

返回值的 docstring：

[ffpa_attn_interface.py:144-145](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ffpa_attn_interface.py#L144-L145) — 返回 `O`，形状 `[B, Nh_q, Nq, D]`，内容是 `softmax(scale * QK^T) V`。

docs 首页的 self-attention 示例（输出形状与 dtype 可直接看出）：

[docs/index.md:61-79](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/index.md#L61-L79) — `B,H,N,D = 1,32,8192,512`，bf16 输入，`out = ffpa_attn_func(q,k,v)` 得到 `(B,H,N,D)` 输出，并与 SDPA 对比 `max_abs_err`。

#### 4.4.4 代码实践

**实践目标**：复现 docs 的 self-attention 示例，确认返回形状/dtype 与数值正确性。这正是本讲规格指定的主实践任务。

**操作步骤**：直接照抄 [docs/index.md:61-79](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/index.md#L61-L79)：

```python
import torch
import torch.nn.functional as F
from ffpa_attn import ffpa_attn_func

# D: 32, 64, ..., 320, ..., 1024 (FA-2 <= 256, FFPA supports up to 1024).
B, H, N, D = 1, 32, 8192, 512  # batch_size, num_heads, seq_len, head_dim
q = torch.randn(B, H, N, D, dtype=torch.bfloat16, device="cuda")
k = torch.randn(B, H, N, D, dtype=torch.bfloat16, device="cuda")
v = torch.randn(B, H, N, D, dtype=torch.bfloat16, device="cuda")

# FFPA self attention; layout follows SDPA: (B, H, N, D).
out = ffpa_attn_func(q, k, v)  # -> torch.Tensor of shape (B, H, N, D)
print(out.shape, out.dtype)    # 期望 torch.Size([1, 32, 8192, 512]) torch.bfloat16

ref = F.scaled_dot_product_attention(q, k, v)
print(f"vs SDPA max_abs_err={(out - ref).abs().max().item():.4e}")
```

**需要观察的现象**：

- `out.shape` 打印 `torch.Size([1, 32, 8192, 512])`，`out.dtype` 为 `torch.bfloat16`——印证「输出形状 = query 形状，dtype 一致」。
- `max_abs_err` 是一个很小的正数（bf16 下通常在 `1e-2 ~ 1e-1` 量级，因为 bf16 本身精度有限）。

**预期结果**：形状与 dtype 如上；`max_abs_err` 的具体数值**待本地验证**（取决于 GPU 型号、Triton 版本与 autotune 选中的 config，但应当与 docs 宣称的「与 SDPA 数值一致」相符，即在同一数量级）。

#### 4.4.5 小练习与答案

**练习 1**：在 self-attention（`Nq == Nkv`、`Nh_q == Nh_kv`）下，`ffpa_attn_func(q,k,v)` 与 `ffpa_attn_func(q,k,v, enable_gqa=True)` 的结果是否相同？为什么？

**参考答案**：相同。当 `Nh_q == Nh_kv` 时 `group_size = 1`，GQA 退化为普通 MHA，每个 Q 头对应自己独立的 K/V 头，与不开 `enable_gqa` 的路径数学上等价。`enable_gqa` 只是一个语义开关，决定「头数不等」是否合法，不影响 `group_size=1` 时的计算。

**练习 2**：返回的张量 `O` 的形状在 cross-attention（`Nq=128, Nkv=8192`）下是多少？

**参考答案**：仍然是 `[B, Nh_q, Nq, D] = [B, Nh_q, 128, D]`。输出形状只跟随 query 的 `Nq` 与 `Nh_q`，与 KV 的长度/头数无关。可参考 docs 的 cross-attention 示例（[docs/index.md:83-102](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/index.md#L83-L102)）。

---

## 5. 综合实践

把本讲的签名、布局、dtype、scale、返回值串起来，做一个「调用正确性体检」小任务。

**任务**：写一个脚本 `signature_check.py`，对同一组随机权重分别用 **fp16** 和 **bf16** 调用 `ffpa_attn_func`，并验证以下断言：

1. 两种 dtype 的输出形状都是 `[1, 32, 8192, 512]`。
2. 两种 dtype 的输出 dtype 分别与输入一致。
3. 各自与同 dtype 的 SDPA 对比，`max_abs_err` 都在合理范围（记录具体数值）。
4. 显式传 `scale=1/math.sqrt(512)` 与不传 `scale` 的输出差异极小。

参考骨架（**示例代码**，需在有 GPU 的环境运行）：

```python
import math
import torch
import torch.nn.functional as F
from ffpa_attn import ffpa_attn_func

B, H, N, D = 1, 32, 8192, 512

for dt in (torch.float16, torch.bfloat16):
    q = torch.randn(B, H, N, D, dtype=dt, device="cuda")
    k = torch.randn(B, H, N, D, dtype=dt, device="cuda")
    v = torch.randn(B, H, N, D, dtype=dt, device="cuda")

    o_default = ffpa_attn_func(q, k, v)
    o_scaled  = ffpa_attn_func(q, k, v, scale=1.0 / math.sqrt(D))
    ref       = F.scaled_dot_product_attention(q, k, v)

    assert tuple(o_default.shape) == (B, H, N, D), "shape mismatch"
    assert o_default.dtype == dt, "dtype mismatch"
    err_sdpa = (o_default - ref).abs().max().item()
    err_scale = (o_default - o_scaled).abs().max().item()
    print(f"{dt}: shape={tuple(o_default.shape)} dtype={o_default.dtype} "
          f"err_vs_sdpa={err_sdpa:.4e} err_vs_explicit_scale={err_scale:.4e}")
```

**观察重点**：

- 两个 dtype 都能跑通，说明 dtype 约束是「fp16/bf16 二选一」而非只支持其中之一。
- `err_vs_sdpa` 体现 FFPA 与 SDPA 的数值一致性；fp16 通常比 bf16 误差更小（尾数位更多）。
- `err_vs_explicit_scale` 应接近 0，印证 scale 默认值确实是 `1/sqrt(D)`。

> 具体误差数值**待本地验证**；若机器无 CUDA 或未安装 FFPA，脚本会在 `device="cuda"` 处报错，请先完成 u1-l2 的安装。

## 6. 本讲小结

- `ffpa_attn_func(query, key, value, attn_mask=None, dropout_p=0.0, is_causal=False, scale=None, enable_gqa=False, **kwargs)` 的签名与 `torch.nn.functional.scaled_dot_product_attention` 对齐，末尾 `**kwargs` 承载 FFPA 专有的后端配置。
- 三个张量都是 `[B, Nh, N, D]` 四维布局；`Nq` 与 `Nkv` 可不同（cross/decode），`Nh_q` 必须是 `Nh_kv` 的整数倍（GQA/MQA），`key/value` 必须共享相同的 `Nh_kv` 与 `Nkv`。
- dtype 必须是 `fp16` 或 `bf16`（且三者一致），否则在 `normalize_inputs` 里抛 `TypeError`。
- `scale=None` 时默认取 `1/sqrt(D)`，与 SDPA 完全一致；也可显式传入任意 `float`。
- `enable_gqa` 默认 `False` 以精确匹配 SDPA，头数不等时必须显式传 `True`；`is_causal=True` 要求 `Nkv >= Nq`。
- 返回**单个张量** `O`，形状 `[B, Nh_q, Nq, D]`（与 query 同形）、dtype 与输入一致，内容是 `softmax(scale * QK^T) V`。

## 7. 下一步学习建议

本讲只讲了「**怎么把参数填对**」。接下来：

- **u2-l2 自注意力 / 交叉注意力 / 解码注意力**：深入 `Nq != Nkv` 的场景，理解为什么解码（`Nq=1`）会走专门的 split-KV 路径。
- **u2-l3 因果掩码 is_causal 与可加 attn_mask**：把本讲只点到为止的 `is_causal` 尾对齐约定与 `attn_mask` 的布尔/可加两种语义讲透。
- **u2-l4 GQA / MQA 分组查询注意力**：配合 `repeat_interleave` 参考实现，验证 GQA 输出与 SDPA 一致。
- **u2-l5 变长注意力 ffpa_attn_varlen_func**：学习 packed THD `[T,H,D]` 布局与 `cu_seqlens` 约定——它是本讲 `ffpa_attn_func` 的「变长版兄弟」。

如果暂时不想继续 API 层，也可以跳到 **u3 单元（分发层与后端架构）**，看 `ffpa_attn_func` 的函数体里那三步（`fallback → normalize → FFPAAttnFunc.apply`）背后到底是怎么按 head_dim 和 backend 分发到不同 kernel 的。
