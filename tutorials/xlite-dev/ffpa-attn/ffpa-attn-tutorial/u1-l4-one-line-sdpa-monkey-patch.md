# 一行代码替换 SDPA：ffpa_attn_func 与 monkey-patch

## 1. 本讲目标

学完本讲后，你应当能够：

- 说出 `ffpa_attn_func` 与 `torch.nn.functional.scaled_dot_product_attention`（下称 **SDPA**）的签名为什么能「对齐」，以及这种对齐使得一行 monkey-patch 成为可能。
- 解释当 FFPA 不支持某种输入（例如 `D ≤ 256`）时，它如何**自动回退（fallback）到原生 SDPA**，而不会发生无限递归。
- 自己写一段脚本，验证「哪个调用走 FFPA、哪个调用回退到 SDPA」。

本讲是整个手册的「跑起来」收尾篇。读完它，你就具备了把 FFPA 接入任意一个使用 `F.scaled_dot_product_attention` 的现成模型的能力。

## 2. 前置知识

本讲默认你已经读过 [u1-l1](u1-l1-what-is-ffpa-split-d.md) 与 [u1-l3](u1-l3-repo-layout-code-map.md)，知道：

- **FFPA** 只在**大 head_dim（D > 256）+ 长序列（N ≥ 512）**的 prefill 场景下比 SDPA 快；其余场景应回退到 SDPA。
- **SDPA** 是 PyTorch 内置的标准注意力实现 `torch.nn.functional.scaled_dot_product_attention`。
- `ffpa_attn_func` 是 FFPA 暴露给用户的公共入口，定义在 `src/ffpa_attn/ffpa_attn_interface.py`。

补充两个 Python / PyTorch 小概念，初学者可能不熟：

- **monkey-patch（猴子补丁）**：在运行时把某个模块里的函数替换成自己的函数。本讲里就是把 `F.scaled_dot_product_attention` 这个名字重新指向 `ffpa_attn_func`，于是所有调用 `F.scaled_dot_product_attention(...)` 的代码都会不知不觉地改走 FFPA。
- **`torch._C._nn.scaled_dot_product_attention`**：这是 SDPA 的**底层 C++ ATen 绑定**，也就是 Python 层 `F.scaled_dot_product_attention` 最终调用的「真身」。理解「Python 符号」和「底层绑定」是两层不同的东西，是本讲避免递归的关键。

注意力的输出公式回顾：

\[
O = \mathrm{softmax}(\mathrm{scale}\cdot QK^\top)\, V
\]

其中 \(\mathrm{scale}\) 默认是 \(1/\sqrt{D}\)。FFPA 与 SDPA 计算的是同一个数学公式，只是实现不同。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [src/ffpa_attn/__init__.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/__init__.py) | 包入口，把 `ffpa_attn_func` 暴露为 `from ffpa_attn import ffpa_attn_func`。 |
| [src/ffpa_attn/ffpa_attn_interface.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ffpa_attn_interface.py) | 定义公共函数 `ffpa_attn_func`，是 monkey-patch 的「替换者」，也是回退判定的发生地。 |
| [src/ffpa_attn/functional.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py) | 定义 `FFPAAttnMeta.fallback()`，集中管理「要不要回退到 SDPA」的全部判定逻辑。 |
| [tests/test_monkey_patch.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_monkey_patch.py) | 锁定 monkey-patch 这一公开用法的测试，演示「回退不递归」的验证技巧。 |
| [README.md](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/README.md) | 文档化的「一行接入」示例。 |

记住一条主线：**用户调 `ffpa_attn_func` → 它先问 `fallback()` 要不要回退 → 回退则直接调原生 SDPA 的底层绑定，不回退则进入 FFPA 自己的 kernel。**

## 4. 核心概念与源码讲解

### 4.1 ffpa_attn_func：与 SDPA 签名对齐的入口

#### 4.1.1 概念说明

monkey-patch 之所以能「一行」完成，前提是替换者与被替换者**长得够像**。FFPA 故意把 `ffpa_attn_func` 的参数列表设计得和 SDPA 对齐：同样的 `(query, key, value)` 前三个位置参数，同样的关键字名 `attn_mask / dropout_p / is_causal / scale / enable_gqa`。绝大多数模型调用 SDPA 时都用「位置传 q/k/v + 关键字传各种开关」的写法，于是把名字一换，代码完全不用改就能跑。

需要诚实说明的一点：FFPA 的关键字**顺序与原生 SDPA 略有不同**（例如 FFPA 没有 `dropout_mask`，且 `dropout_p` 与 `is_causal` 的位置和原生不完全一致）。所以严格意义上并非「逐字相同签名」。但因为模型代码几乎都以**关键字**方式传递这些开关，位置差异不会暴露，monkey-patch 在实际工程里是透明的。这一点 docstring 里用「Signature aligned with SDPA」来概括。

#### 4.1.2 核心流程

`ffpa_attn_func` 的执行只有三步：

```text
1. 构造 meta：从 **kwargs 解析 backend 配置（默认 Triton）
2. 判定 fallback：meta.fallback(query, key, attn_mask, dropout_p)
     ├─ True  → 直接调用原生 SDPA（torch._C._nn...）并返回
     └─ False → 做输入归一化，交给 autograd Function（FFPAAttnFunc）
```

注意第 2 步是一个**短路**：只要判定需要回退，函数立刻返回 SDPA 的结果，根本不会进入 FFPA 的 kernel 路径。回退与否只看前向，不看反向。

#### 4.1.3 源码精读

公共入口与回退判定（[src/ffpa_attn/ffpa_attn_interface.py:156-181](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ffpa_attn_interface.py#L156-L181)）：

```python
meta = FFPAAttnMeta.from_kwargs(**kwargs)
if meta.fallback(query, key, attn_mask, dropout_p):
  # HACK: Avoid recursive for monkey-patch usage.
  return torch._C._nn.scaled_dot_product_attention(
      query, key, value,
      attn_mask=attn_mask, dropout_p=dropout_p,
      is_causal=is_causal, scale=scale, enable_gqa=enable_gqa,
  )

meta, query, key, value, attn_bias = meta.normalize(
    query, key, value, attn_mask, dropout_p, is_causal, scale, enable_gqa,
)
return FFPAAttnFunc.apply(query, key, value, attn_bias, meta)
```

要点：

- `meta.fallback(...)` 返回 `True` 就走 SDPA；注意这里调用的是 `torch._C._nn.scaled_dot_product_attention`（底层绑定），**不是** `F.scaled_dot_product_attention`（Python 符号）——这正是 4.3 节要讲的不递归关键。
- 不回退时，`meta.normalize(...)` 做形状/类型校验并把 `attn_mask` 归一化成可加偏置 `attn_bias`，最后交给 `FFPAAttnFunc.apply`（autograd 边界）。

函数签名（[src/ffpa_attn/ffpa_attn_interface.py:71-81](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ffpa_attn_interface.py#L71-L81)），可以和 SDPA 的签名逐项对照：

```python
def ffpa_attn_func(
  query, key, value,
  attn_mask=None, dropout_p=0.0, is_causal=False,
  scale=None, enable_gqa=False, **kwargs,
) -> torch.Tensor:
```

包入口（[src/ffpa_attn/__init__.py:1-2](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/__init__.py#L1-L2)）保证你能 `from ffpa_attn import ffpa_attn_func`：

```python
from .ffpa_attn_interface import ffpa_attn_func, ffpa_attn_varlen_func
```

#### 4.1.4 代码实践

**实践目标**：确认 `ffpa_attn_func` 与 SDPA 在「不被 patch」的情况下算的是同一个数学结果。

**操作步骤**：

1. 构造一组 D=512、fp16 的 q/k/v。
2. 分别用 `ffpa_attn_func` 和原生 SDPA 计算，比较 `max_abs_err`。

```python
import math, torch
from ffpa_attn import ffpa_attn_func
native = torch._C._nn.scaled_dot_product_attention  # 原生 SDPA 真身

torch.manual_seed(0)
q = torch.randn(1, 8, 512, 512, dtype=torch.float16, device="cuda")
k = torch.randn(1, 8, 512, 512, dtype=torch.float16, device="cuda")
v = torch.randn(1, 8, 512, 512, dtype=torch.float16, device="cuda")
scale = 1.0 / math.sqrt(512)

ref = native(q, k, v, scale=scale)
out = ffpa_attn_func(q, k, v, scale=scale)
print("max_abs_err =", (out.float()-ref.float()).abs().max().item())
```

**需要观察的现象**：输出是一个很小的正数（fp16 下通常在 1e-3 ~ 1e-1 量级），而不是 0，也不是 NaN。

**预期结果**：两者数值相近但**不相等**——这说明 D=512 时确实跑了两套不同的 kernel（FFPA ≠ 原生 Flash）。结果待本地验证（需要 CUDA GPU）。

#### 4.1.5 小练习与答案

**练习 1**：为什么作者要把 `ffpa_attn_func` 的签名设计成和 SDPA 对齐，而不是另起一个完全不同的 API？

**参考答案**：为了让用户用一行 monkey-patch（`F.scaled_dot_product_attention = ffpa_attn_func`）就能把 FFPA 接入现成模型，而不必修改模型里每一处注意力调用。签名对齐 = 接入零成本。

**练习 2**：`ffpa_attn_func` 接受一个 `**kwargs`，它会被什么消费？

**参考答案**：由 `FFPAAttnMeta.from_kwargs(**kwargs)` 消费，目前只认识 `backend / forward_backend / backward_backend` 三个 FFPA 专有关键字，其余会抛 `TypeError`。这也是「签名与 SDPA 对齐」的代价：扩展关键字只能塞进 `**kwargs`。

### 4.2 FFPAAttnMeta.fallback：什么情况下回退到 SDPA

#### 4.2.1 概念说明

`fallback()` 是一个纯判定函数：给它输入张量和 dropout，它回答一个布尔值——`True` 表示「这个用例别用 FFPA，直接交给原生 SDPA」。把判定逻辑集中在一个方法上，是为了让公共 API 层不必到处重复硬编码「head_dim 多少算小」之类的规则。回退是**静默且自动**的：用户不需要写任何 if-else，FFPA 自己决定何时退回 SDPA。

回退条件的核心直觉来自 [u1-l1](u1-l1-what-is-ffpa-split-d.md)：FFPA 的 Split-D 只在**大 D + 长序列**下才划算；当 `D ≤ 256` 或序列太短时，标准 FlashAttention（SDPA）已经够快甚至更快，强行用 FFPA 反而吃亏，于是回退。

#### 4.2.2 核心流程

以默认的 Triton 后端为例，`fallback()` 返回 `True`（即回退）当且仅当下列**任意一条**成立（其余情况返回 `False`，即走 FFPA）：

| 条件 | 含义 |
| --- | --- |
| `forward_meta.name == "sdpa"` | 用户显式选了 SDPA 后端，前向必然走 SDPA |
| `_should_use_aten_small_d_forward(D)`，即 `D ≤ 256`（且未开 env 小 D 开关） | 小 head_dim，FFPA 无优势 |
| `D > 1024` | head_dim 超出 FFPA 当前支持上限 |
| `8 <= Nq < 512` | query 序列太短（但又不是 decode 那种极短），走 FFPA 不划算 |
| `Nkv < 512` | KV 序列太短，同理 |

补充两条针对 CuTeDSL 后端的额外回退：硬件/head_dim 不被该后端支持，或 `attn_mask`/`dropout_p>0` 与 cutedsl 不兼容时，也会回退（这些会在 [u6](u6-l1-cutedsl-overview-sm80-sm90.md) 详讲）。

一个对本讲实践至关重要的推论：

- **D=128**：命中 `D ≤ 256` → 回退到 SDPA。
- **D=512（且 Nq=Nkv=512）**：五条都不命中 → 走 FFPA。

#### 4.2.3 源码精读

判定主逻辑（[src/ffpa_attn/functional.py:515-522](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L515-L522)）：

```python
return any([
  _should_use_aten_small_d_forward(self.forward_meta, D),  # D <= 256
  D > 1024,
  attn_mask is not None and self.forward_meta.name == "cutedsl",
  dropout_p > 0.0 and self.forward_meta.name == "cutedsl",
  (8 <= Nq < 512),
  Nkv < 512,
])
```

「小 D」的阈值常量（[src/ffpa_attn/functional.py:49-50](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L49-L50)）：

```python
_ATEN_SMALL_HEAD_DIM_MAX = 256   # D <= 256 视为小 D，回退
_FFPA_SMALL_HEAD_DIM_MIN = 64    # 允许小 D 走 FFPA 的下界（需 env 开关）
```

小 D 判定函数（[src/ffpa_attn/functional.py:75-81](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L75-L81)）：

```python
def _should_use_aten_small_d_forward(forward_backend, head_dim):
  return head_dim <= _ATEN_SMALL_HEAD_DIM_MAX and not _backend_allows_small_d(
      forward_backend, head_dim
  )
```

也就是说 `D ≤ 256` 默认就回退；只有显式设置环境变量 `FFPA_TRITON_ALLOW_SMALL_D`（或 cutedsl 对应开关）时，才允许小 D 强行走 FFPA，这是给开发者调优用的逃生口。

注意 `fallback()` 一开始还有一段形状断言（[src/ffpa_attn/functional.py:486-490](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L486-L490)），要求输入必须是 4 维 `[B, Nh, N, D]`，这是后续 `B, Nh_q, Nq, D = query.shape` 解包的前提。

#### 4.2.4 代码实践

**实践目标**：在不实际跑 kernel 的前提下，手动预测若干组输入会不会回退。

**操作步骤**：阅读上面的判定表，对下表每一行写出「走 FFPA / 回退 SDPA」并给出命中的条件。

| D | Nq | Nkv | 默认后端 | 你的预测 |
| --- | --- | --- | --- | --- |
| 128 | 64 | 64 | triton | ？ |
| 512 | 512 | 512 | triton | ？ |
| 512 | 256 | 512 | triton | ？ |
| 1024 | 1024 | 1024 | triton | ？ |
| 2048 | 1024 | 1024 | triton | ？ |

**需要观察的现象 / 预期结果**：

- 128/64/64 → 回退（命中 `D ≤ 256`）。
- 512/512/512 → FFPA（五条都不命中）。
- 512/256/512 → 回退（命中 `8 ≤ Nq < 512`）。
- 1024/1024/1024 → FFPA（`D > 1024` 不成立，1024 不大于 1024）。
- 2048/1024/1024 → 回退（命中 `D > 1024`）。

这是「源码阅读型实践」，无需 GPU。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `8 <= Nq < 512` 要回退，而 `Nq == 1`（decode）不在这个范围里？

**参考答案**：`Nq < 8` 的极短 query（典型如 decode 的 `Nq == 1`）走的是 FFPA 专门的 **split-KV decode 路径**（见 [u4-l3](u4-l3-decode-fwd-split-kv.md)），所以不被这条规则回退；而 `8 ≤ Nq < 512` 是「不上不下」的中等长度，既不够长发挥 Split-D 的优势、又不够短触发 decode 路径，直接回退 SDPA 更划算。

**练习 2**：如果想让 `D=128` 也强制走 FFPA，该怎么办？

**参考答案**：设置环境变量 `FFPA_TRITON_ALLOW_SMALL_D=1`，使 `_backend_allows_small_d` 返回 `True`，从而让 `_should_use_aten_small_d_forward` 返回 `False`，不再因小 D 而回退。注意这只是开发调优手段，性能未必更好。

### 4.3 monkey-patch 接入与避免递归

#### 4.3.1 概念说明

现在把前两节合起来看真正的 monkey-patch。设想你写了：

```python
F.scaled_dot_product_attention = ffpa_attn_func
```

之后，模型里任何 `F.scaled_dot_product_attention(q,k,v)` 都会调到 `ffpa_attn_func`。一旦这个用例（比如 `D=128`）需要回退，`ffpa_attn_func` 内部就要去「调一次 SDPA」。

**隐患**：如果回退时调的也是 `F.scaled_dot_product_attention`，那它又指向 `ffpa_attn_func`，于是再次进入 `ffpa_attn_func` → 再次判定回退 → 再次调 `F.scaled_dot_product_attention` ……这就是**无限递归**。

**解决办法**：回退时不要调那个被 patch 过的 Python 符号，而是直接调它的**底层 C++ 绑定** `torch._C._nn.scaled_dot_product_attention`。底层绑定是另一个对象，不受 Python 层 monkey-patch 影响，于是递归被打破。源码里那句 `# HACK: Avoid recursive for monkey-patch usage.` 注释的就是这件事。

#### 4.3.2 核心流程

```text
用户代码:  F.scaled_dot_product_attention(q,k,v)      # 已被 patch
            └─> ffpa_attn_func(q,k,v)
                  ├─ fallback()==True
                  │     └─> torch._C._nn.scaled_dot_product_attention(...)   # 底层绑定，不会再回到 ffpa_attn_func
                  └─ fallback()==False
                        └─> FFPAAttnFunc.apply(...)                          # FFPA kernel
```

关键点：回退分支调用的是**底层绑定**（`torch._C._nn...`），而不是 **Python 符号**（`F....`）。这两者在 monkey-patch 之后是两个不同的可调用对象。

> 小贴士：如果你自己在别的项目里做类似的 monkey-patch + 内部回退，请沿用同样的模式——回退时绕过被 patch 的符号，直接调底层。

#### 4.3.3 源码精读

回退分支调底层绑定（[src/ffpa_attn/ffpa_attn_interface.py:157-168](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ffpa_attn_interface.py#L157-L168)，注意第 158 行的 HACK 注释）：

```python
if meta.fallback(query, key, attn_mask, dropout_p):
  # HACK: Avoid recursive for monkey-patch usage.
  return torch._C._nn.scaled_dot_product_attention(
      query, key, value, attn_mask=attn_mask, dropout_p=dropout_p,
      is_causal=is_causal, scale=scale, enable_gqa=enable_gqa,
  )
```

测试如何「锁死」这一行为（[tests/test_monkey_patch.py:29-30](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_monkey_patch.py#L29-L30)）先定义原生真身：

```python
def _native_sdpa(*args, **kwargs):
  return torch._C._nn.scaled_dot_product_attention(*args, **kwargs)
```

大 D 用例的关键技巧（[tests/test_monkey_patch.py:127-133](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_monkey_patch.py#L127-L133)）：**既 patch Python 符号指向 FFPA，又把底层绑定替换成一个会抛异常的桩**。

```python
monkeypatch.setattr(F, "scaled_dot_product_attention", ffpa_attn_func)
monkeypatch.setattr(
  torch._C._nn, "scaled_dot_product_attention", _block_native_sdpa
)
out = F.scaled_dot_product_attention(q, k, v, **kwargs)
```

其中 `_block_native_sdpa`（[tests/test_monkey_patch.py:65-69](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_monkey_patch.py#L65-L69)）一旦被调用就抛 `AssertionError`：

```python
def _block_native_sdpa(*args, **kwargs):
  raise AssertionError(
    "large-D monkey-patched case unexpectedly fell back to native SDPA"
  )
```

**这个技巧极其巧妙**：大 D 用例应当走 FFPA、不应当触发回退；如果代码有 bug 让它意外回退，就会调用底层绑定，而底层绑定已被换成「抛异常的桩」，测试立刻失败。换句话说，测试通过 = 大 D 确实走了 FFPA 而非偷偷回退；同时也证明了 patch 后**不会递归**（否则也会反复进入桩而抛异常）。

README 里文档化的「一行接入」（[README.md:49-53](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/README.md#L49-L53)）：

```python
>>> import torch.nn.functional as F
>>> from ffpa_attn import ffpa_attn_func
>>> # Monkey-patch SDPA to point to FFPA. Every thing that FFPA
>>> # does not support will auto fallback to SDPA: D <= 256, etc.
>>> F.scaled_dot_product_attention = ffpa_attn_func # one-line code
```

#### 4.3.4 代码实践

**实践目标**：复刻测试里的「堵回退通道」技巧，亲眼确认 D=128 会回退、D=512 不会。

**操作步骤**（需要 CUDA GPU，结果待本地验证）：

```python
import math, torch
from ffpa_attn import ffpa_attn_func

# 先保存原生底层绑定，方便最后还原
_native = torch._C._nn.scaled_dot_product_attention

def _block(*a, **k):
    raise AssertionError("意外回退到了原生 SDPA")

for D in (128, 512):
    n = 512 if D > 256 else 64
    torch.manual_seed(0)
    q = torch.randn(1, 8, n, D, dtype=torch.float16, device="cuda")
    k = torch.randn(1, 8, n, D, dtype=torch.float16, device="cuda")
    v = torch.randn(1, 8, n, D, dtype=torch.float16, device="cuda")
    scale = 1.0 / math.sqrt(D)

    # 把底层绑定换成桩：一旦 FFPA 回退，就抛异常
    torch._C._nn.scaled_dot_product_attention = _block
    try:
        out = ffpa_attn_func(q, k, v, scale=scale)
        print(f"D={D:4d}: 走 FFPA（未触发回退）")
    except AssertionError:
        print(f"D={D:4d}: 回退到 SDPA（触发了底层绑定）")
    finally:
        torch._C._nn.scaled_dot_product_attention = _native
```

**需要观察的现象 / 预期结果**：

- `D=128`：打印「回退到 SDPA」。
- `D=512`：打印「走 FFPA」。

如果两者顺序打印正确，你就同时验证了三件事：签名对齐让替换可行、`fallback()` 判定正确、回退走的是底层绑定而非 Python 符号。

#### 4.3.5 小练习与答案

**练习 1**：把回退分支里的 `torch._C._nn.scaled_dot_product_attention` 改成 `F.scaled_dot_product_attention`（假设已 patch），会发生什么？

**参考答案**：对小 D 用例会无限递归——`ffpa_attn_func` 回退时调 `F.scaled_dot_product_attention`，而后者已被 patch 成 `ffpa_attn_func`，于是再次进入 `ffpa_attn_func`，判定又回退，永无止境，最终 `RecursionError`。这正是源码用底层绑定 + HACK 注释要避免的。

**练习 2**：测试里把 `torch._C._nn.scaled_dot_product_attention` 替换成 `_block_native_sdpa` 后，为什么大 D 用例仍能正常返回结果？

**参考答案**：因为大 D 用例根本不回退，不会触碰底层绑定，所以那个「抛异常的桩」从未被调用，`ffpa_attn_func` 走的是 FFPA kernel 路径并正常返回。桩只在小 D / 短序列等回退情形才会被触发。

## 5. 综合实践

把本讲三个模块串起来，写一个完整的小脚本：**先 monkey-patch，再用 D=128 与 D=512 两组输入分别调用，自动判断各自走哪条路，并打印与原生 SDPA 的 max_abs_err**。

```python
# ffpa_monkey_patch_demo.py
# 运行前提：CUDA GPU + 已安装 ffpa_attn（triton-only 即可）
import math
import torch
import torch.nn.functional as F
from ffpa_attn import ffpa_attn_func

native = torch._C._nn.scaled_dot_product_attention   # 原生 SDPA 真身

def make_qkv(dtype, headdim, n):
    torch.manual_seed(0)
    q = torch.randn(1, 8, n, headdim, dtype=dtype, device="cuda")
    k = torch.randn(1, 8, n, headdim, dtype=dtype, device="cuda")
    v = torch.randn(1, 8, n, headdim, dtype=dtype, device="cuda")
    return q, k, v

# 1) 一行 monkey-patch：从此 F.scaled_dot_product_attention 即 ffpa_attn_func
F.scaled_dot_product_attention = ffpa_attn_func

for headdim in (128, 512):
    n = 512 if headdim > 256 else 64          # 让序列长度也落在不回退区，便于孤立观察 D 的影响
    q, k, v = make_qkv(torch.float16, headdim, n)
    scale = 1.0 / math.sqrt(headdim)

    ref = native(q, k, v, scale=scale)        # 参考真值：原生 SDPA
    out = F.scaled_dot_product_attention(q, k, v, scale=scale)  # 走被 patch 后的入口

    err = (out.float() - ref.float()).abs().max().item()
    print(f"D={headdim:4d}  max_abs_err={err:.6f}")
```

**需要观察的现象 / 预期结果**（待本地验证）：

- `D=128`：由于 `ffpa_attn_func` 内部 `fallback()==True`，`out` 实际是原生 SDPA 算出来的，与 `ref` 同源同输入，`max_abs_err` 应为 `0.0`（无 dropout 时完全一致）。
- `D=512`：`fallback()==False`，跑的是 FFPA kernel，与原生 Flash 是两套实现，`max_abs_err` 是一个很小的正数（fp16 下约 1e-3 ~ 1e-1）。

这正好把三件事一次性证明：**签名对齐 → patch 可行**；**err≈0 说明 D=128 回退到了同一个原生 SDPA**；**err>0 说明 D=512 确实走了不同的 FFPA kernel**。

> 进阶（可选）：在循环里再套上 4.3.4 的「堵底层绑定」技巧，把「走 FFPA / 回退 SDPA」的判断从「推测」变成「程序自动打印」。

## 6. 本讲小结

- `ffpa_attn_func` 的签名与 SDPA 对齐（前三个位置参数 + 同名关键字），这是 `F.scaled_dot_product_attention = ffpa_attn_func` 一行 monkey-patch 得以成立的前提。
- `FFPAAttnMeta.fallback()` 集中管理「回退到 SDPA」的全部判定：默认 Triton 后端下，`D ≤ 256`、`D > 1024`、`8 ≤ Nq < 512`、`Nkv < 512` 任一成立即回退。
- 回退分支调用的是**底层 C++ 绑定** `torch._C._nn.scaled_dot_product_attention`，而非被 patch 过的 Python 符号，从而避免无限递归——源码以 `# HACK: Avoid recursive for monkey-patch usage.` 注释标记。
- 测试 `test_monkey_patch.py` 用「把底层绑定替换成抛异常的桩」这一巧妙技巧，同时锁定了「大 D 走 FFPA」与「回退不递归」两件事。
- 对小 D 用例，FFPA 静默回退 SDPA，输出与原生 SDPA 同源（数值一致）；对大 D 用例，FFPA 跑自己的 kernel（数值有小差异但数学等价）。

## 7. 下一步学习建议

下一讲 [u2-l1 ffpa_attn_func 签名、张量布局与返回](u2-l1-ffpa-attn-func-signature-layout.md) 会逐参数深入 `ffpa_attn_func` 的张量布局 `[B, Nh, N, D]`、dtype 约束与返回值，把本讲「签名对齐」这句话展开成可操作的细节。

如果你对「分发到底是怎么走的」更感兴趣，可以直接跳到第 3 单元 [u3-l1 四后端总览](u3-l1-four-backends-overview.md) 与 [u3-l3 FFPAAttnMeta：输入校验与回退判定](u3-l3-meta-normalize-and-fallback.md)，那里会系统讲 `fallback()` 之后的 `normalize()` 校验链路与四后端选择。建议按顺序读到第 3 单元再回头做本讲的进阶实践。
