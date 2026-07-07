# FFPAAttnMeta：输入校验与 SDPA 回退判定

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `ffpa_attn_func` 从「拿到用户输入」到「进入 FFPA kernel」之间，公共 API 层做了哪三件事：构造 meta、回退判定、校验归一化。
- 逐条列出 `FFPAAttnMeta.fallback()` 在默认后端下会触发「回退原生 SDPA」的全部条件，并解释为什么解码（`Nq=1`）反而**不**回退。
- 读懂 `normalize_inputs()` 对形状、GQA、causal、dropout、dtype 的全套校验顺序与异常类型。
- 读懂 `normalize_attn_mask()` 如何把布尔/可加掩码统一归一化成一个紧凑 4 维可加 bias。
- 理解 `_should_use_aten_small_d_forward()` 这条「小 D 走 aten」判定是如何被环境变量微调的。

本讲是 u3 单元「分发层」的第三篇，承接 [u3-l2 Backend 配置类体系](u3-l2-backend-config-dataclasses.md)：上一讲讲清了 `Backend` 配置对象是什么，本讲讲清这些配置对象被装进 `FFPAAttnMeta` 后，**在真正调用 kernel 之前**还要经过哪两道关卡（回退判定 + 输入校验）。

## 2. 前置知识

- **SDPA**：`torch.nn.functional.scaled_dot_product_attention`，PyTorch 自带的融合注意力，FFPA 既对齐它的签名，也把它当作回退目标。
- **回退（fallback）**：FFPA 只在「大 `head_dim` + 长序列」时更快；不擅长的场景不报错，而是悄悄改走原生 SDPA，对外保持结果正确。详见 [u1-l1](u1-l1-what-is-ffpa-split-d.md)。
- **`[B, Nh, N, D]` 布局**：batch、头数、序列长度、头维度。`Nq` 是 query 的序列长，`Nkv` 是 key/value 的序列长。详见 [u2-l1](u2-l1-ffpa-attn-func-signature-layout.md)。
- **GQA/MQA**：多个 query 头共用一组 K/V，`group_size = Nh_q / Nh_kv`。详见 [u2-l4](u2-l4-gqa-mqa-grouped-attention.md)。
- **dataclass**：Python 用 `@dataclass` 自动生成构造函数的类，`FFPAAttnMeta` 就是一个 dataclass。
- **monkey-patch 与递归**：把 `F.scaled_dot_product_attention` 替换成 `ffpa_attn_func` 后，回退分支必须调用底层 C++ 绑定 `torch._C._nn.scaled_dot_product_attention`，否则会无限递归。详见 [u1-l4](u1-l4-one-line-sdpa-monkey-patch.md)。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件：

| 文件 | 作用 |
|---|---|
| [src/ffpa_attn/functional.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py) | 定义 `FFPAAttnMeta`（含 `from_kwargs` / `fallback` / `normalize_inputs` / `normalize_attn_mask`）、`_should_use_aten_small_d_forward`、`_validate_attn_mask_shape`、autograd Function `_FFPAAttnFunc` |
| [src/ffpa_attn/ffpa_attn_interface.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ffpa_attn_interface.py) | 公共入口 `ffpa_attn_func`，把上面三步串起来的调用方 |
| [src/ffpa_attn/cute/__init__.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py) | 提供 `cute_forward_available` / `cute_max_supported_head_dim`，被 `fallback()` 的 cutedsl 分支调用 |
| [tests/test_monkey_patch.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_monkey_patch.py) | 用「把底层绑定换成抛异常的桩」同时锁定「大 D 走 FFPA」与「回退不递归」 |

## 4. 核心概念与源码讲解

先看公共 API 把这三步串起来的全景。`ffpa_attn_func` 的函数体只有三步：

[src/ffpa_attn/ffpa_attn_interface.py:156-181](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ffpa_attn_interface.py#L156-L181) —— 构造 meta → 回退判定 → 校验归一化 → 进入 autograd：

```python
meta = FFPAAttnMeta.from_kwargs(**kwargs)          # ① 解析后端配置
if meta.fallback(query, key, attn_mask, dropout_p): # ② 回退短路判定
    # HACK: Avoid recursive for monkey-patch usage.
    return torch._C._nn.scaled_dot_product_attention(...)  # 走底层绑定，避免递归

meta, query, key, value, attn_bias = meta.normalize( # ③ 校验 + 掩码归一化
    query, key, value, attn_mask, dropout_p, is_causal, scale, enable_gqa,
)
return FFPAAttnFunc.apply(query, key, value, attn_bias, meta)  # ④ 进入 kernel
```

注意一个关键的顺序细节：**回退判定（②）发生在校验（③）之前**。也就是说，如果一次调用被判为「回退」，那么 FFPA 专属的那些校验（GQA 整除、causal 尾对齐等）根本不会执行——它直接交给原生 SDPA，由 SDPA 用自己的规则去校验。这意味着 `fallback()` 必须是一个**廉价、只看前向、不依赖完整校验**的快速预判。

下面按四个最小模块拆开讲。

---

### 4.1 FFPAAttnMeta 数据类与 from_kwargs 构造

#### 4.1.1 概念说明

`FFPAAttnMeta` 是一个**非张量的「调度信封」**：它把「这次调用要用哪两个后端（前向/反向）」和「这次注意力的选项（是否 causal、scale、dropout、是否需要梯度）」打包成一个对象，穿过 autograd Function 边界传到 kernel。

为什么需要它？因为 `_FFPAAttnFunc.forward` / `backward` 是 `torch.autograd.Function` 的静态方法，签名被 autograd 协议约束（只能接收张量和少量非张量），而 FFPA 的调度状态又很多（两个 `Backend` 配置对象 + 一组注意力选项）。把这些零散状态收进一个 dataclass，既能让 autograd 边界保持清爽，也能让校验逻辑集中在一个对象上。

`FFPAAttnMeta` 持有三个字段：

- `attn_meta: AttentionMeta` —— 注意力选项（`is_causal` / `scale` / `dropout_p` / `is_grad_enabled`）。
- `forward_meta: Backend` —— 前向后端配置对象（如 `TritonBackend(forward=True)`）。
- `backward_meta: Backend` —— 反向后端配置对象。

#### 4.1.2 核心流程

`FFPAAttnMeta.from_kwargs(**kwargs)` 是公共 API 唯一的构造入口，流程是：

1. 从 `kwargs` 中 `pop` 出 `backend`、`forward_backend`、`backward_backend`，并用 `_coerce_backend` 把字符串或实例统一成 `Backend` 实例。
2. 如果还有剩余 `kwargs`，**立即抛 `TypeError`**（fail-fast，拒绝未知参数）。
3. 按优先级解析：「显式 `forward_backend` / `backward_backend` > `backend` 简写 > 默认 Triton」。`backend` 仅在两侧都未显式时充当两段，否则被静默忽略。
4. 单边指定 cutedsl 时自动补全另一边为 cutedsl。
5. 交给 `_resolve_backend_pair` 做「cutedsl 必须前后对称」等硬校验，并补默认 Triton。

#### 4.1.3 源码精读

dataclass 字段与默认工厂（前向/反向默认都是 `TritonBackend`）：

[src/ffpa_attn/functional.py:391-406](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L391-L406) —— `FFPAAttnMeta` 定义与 `__post_init__` 调用 `_resolve_backend_pair`：

```python
@dataclass
class FFPAAttnMeta:
    attn_meta: AttentionMeta = field(default_factory=AttentionMeta)
    forward_meta: Backend = field(default_factory=lambda: TritonBackend(forward=True))
    backward_meta: Backend = field(default_factory=lambda: TritonBackend(backward=True))

    def __post_init__(self) -> None:
        self.forward_meta, self.backward_meta = _resolve_backend_pair(
            self.forward_meta, self.backward_meta
        )
```

`from_kwargs` 的优先级解析与未知参数拒绝：

[src/ffpa_attn/functional.py:408-450](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L408-L450) —— 关键片段：未知 kwarg 立即报错、`backend` 简写只在双侧都未显式时生效、cutedsl 单边自动补全：

```python
if kwargs:                                        # 剩余未知 kwarg → TypeError
    unexpected = ", ".join(sorted(kwargs))
    raise TypeError(f"ffpa_attn_func() got unexpected keyword argument(s): {unexpected}")

if forward_backend is None and backward_backend is None and backend is not None:
    backend = _coerce_backend(backend, source="backend")  # backend 简写充当前后两段
    forward_backend = backend
    backward_backend = backend

if forward_backend is not None and backward_backend is None and forward_backend.name == "cutedsl":
    backward_backend = CuTeDSLBackend()           # 单边 cutedsl 自动补全
...
forward_backend, backward_backend = _resolve_backend_pair(forward_backend, backward_backend)
```

`_resolve_backend_pair` 的硬约束（cutedsl 前后必须对称、None 补默认 Triton）：

[src/ffpa_attn/functional.py:253-281](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L253-L281) —— cutedsl 不对称直接抛 `ValueError`：

```python
if forward_backend.name == "cutedsl" and backward_backend.name != "cutedsl":
    raise ValueError("forward_backend='cutedsl' requires backward_backend='cutedsl'")
if backward_backend.name == "cutedsl" and forward_backend.name != "cutedsl":
    raise ValueError("backward_backend='cutedsl' requires forward_backend='cutedsl'")
```

#### 4.1.4 代码实践

**实践目标**：验证 `from_kwargs` 的优先级与未知参数拒绝。

**操作步骤**（阅读型实践，可在本地用 `python` 交互）：

1. `from ffpa_attn.functional import FFPAAttnMeta, CuTeDSLBackend, TritonBackend`。
2. 分别构造三种 meta，打印 `meta.forward_meta.name` 与 `meta.backward_meta.name`：
   - `FFPAAttnMeta.from_kwargs(backend="cutedsl")`
   - `FFPAAttnMeta.from_kwargs(forward_backend="cutedsl")`（单边，应自动补全）
   - `FFPAAttnMeta.from_kwargs(forward_backend="triton", backward_backend="sdpa")`
3. 再试 `FFPAAttnMeta.from_kwargs(unknown_flag=True)`。

**需要观察的现象**：

- 第一种：前后都是 `cutedsl`。
- 第二种：前后都是 `cutedsl`（单边补全生效）。
- 第三种：前向 `triton`、反向 `sdpa`。
- 第四种：抛 `TypeError: ffpa_attn_func() got unexpected keyword argument(s): unknown_flag`。

**预期结果**：`backend` 简写等价于同时设前后；显式单边优先；未知 kwarg 被 fail-fast 拒绝。若你的环境跑不出上述结果，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果用户同时传 `backend="triton"` 和 `forward_backend="cutedsl"`，最终前向走哪个后端？

**答案**：走 `cutedsl`。因为 `from_kwargs` 的优先级是「显式 `forward_backend` > `backend`」：当 `forward_backend` 已显式给出时，`backend` 简写被静默忽略，不再充当前向。`backward` 未显式则由 `backend="triton"` 充当，故反向是 triton。

**练习 2**：为什么 `from_kwargs` 要在解析后端之前就拒绝未知 kwarg？

**答案**：为了让拼错参数名（如 `casual=True`）立刻报错而不是被默默吞掉、退回默认值，避免用户以为某个选项生效了其实没有。这是 fail-fast 设计。

---

### 4.2 fallback() 与 _should_use_aten_small_d_forward：SDPA 回退判定

#### 4.2.1 概念说明

`fallback()` 是 `FFPAAttnMeta` 的一个方法，返回一个 `bool`：**True 表示这次调用应当回退到原生 SDPA，False 表示进入 FFPA kernel**。它是公共 API 里那道「短路开关」，决定了你这次注意力到底跑在 FFPA 还是 SDPA 上。

它有两个设计前提：

1. **只看前向，不看反向**。回退判定只决定前向走哪条路；如果前向回退到 SDPA，反向自然也由 SDPA 一并完成（SDPA 自己有反向）。
2. **廉价预判，发生在完整校验之前**。它只读 `query` / `key` 的形状、`attn_mask` 是否为 None、`dropout_p` 是否大于 0，以及 `forward_meta.name`，不做昂贵检查。

`_should_use_aten_small_d_forward()` 是 `fallback()` 内部最核心的一个子判定：**「小 `head_dim`（D ≤ 256）该不该走 aten」**。这是 FFPA 与 SDPA 的分界线（详见 [u1-l1](u1-l1-what-is-ffpa-split-d.md)）：D ≤ 256 时 FlashAttention-2 已经够用，FFPA 的 Split-D 优势发挥不出来。

#### 4.2.2 核心流程

`fallback()` 是三分支结构：

```
if forward_meta.name == "sdpa":        # 用户显式要 sdpa 前向
    return True                          # 永远回退（sdpa 前向 = 原生 SDPA）
elif forward_meta.name == "cutedsl":   # cutedsl 后端
    return cutedsl 硬件/head_dim 不满足     # 仅硬件/形状不满足才回退；mask/dropout 不在这里回退
else:                                    # triton 或 cuda（默认）
    return any([ 一组条件 ])              # 任一成立即回退
```

默认分支（triton / cuda）的回退条件，**任一成立即回退到 SDPA**：

| 条件 | 含义 |
|---|---|
| `_should_use_aten_small_d_forward(meta, D)` 为 True | `D ≤ 256` 且后端未开启 small-d 开关 → 小 D 走 aten |
| `D > 1024` | head_dim 超过 FFPA 上限 |
| `8 <= Nq < 512` | query 序列中等长度，prefill 主 kernel 并行度不够划算 |
| `Nkv < 512` | KV 太短，填不满 SM |
| `attn_mask is not None and name == "cutedsl"` | （默认分支恒为 False，冗余保险） |
| `dropout_p > 0.0 and name == "cutedsl"` | （默认分支恒为 False，冗余保险） |

> **关键洞察**：`8 <= Nq < 512` 这个区间特意排除了 `Nq < 8`。也就是说**解码（`Nq=1`）不会在这里回退**——它会进入 FFPA 的 split-KV 解码专用路径（详见 [u4-l3](u4-l3-decode-fwd-split-kv.md)）。`fallback()` 把「中等长度 query」和「真正的解码」区分开：前者回退 SDPA 更划算，后者走 FFPA 的解码路径更划算。

`_should_use_aten_small_d_forward()` 的逻辑非常简单：

```
should_use_aten_small_d_forward(backend, D) =
    (D ≤ 256) AND (NOT backend_allows_small_d(backend, D))
```

而 `_backend_allows_small_d()` 只有在 `D ∈ [64, 256]`、后端是 Triton 或 CuTeDSL、**且对应环境变量开启**时才返回 True：

- Triton 后端看 `FFPA_TRITON_ALLOW_SMALL_D`
- CuTeDSL 后端看 `FFPA_CUTE_ALLOW_SMALL_D`
- CUDA / SDPA 后端永远不允许小 D（小 D 一律走 aten）

换句话说，默认情况下 `D ≤ 256` 一定走 aten；只有开发者显式打开 env 开关，才允许 Triton/CuTeDSL 接管小 D（用于测试/调试）。

#### 4.2.3 源码精读

常量定义——小 D 上界 256、FFPA 小 D 下界 64：

[src/ffpa_attn/functional.py:49-50](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L49-L50) —— 这两个常量是整条小 D 判定的基石：

```python
_ATEN_SMALL_HEAD_DIM_MAX = 256
_FFPA_SMALL_HEAD_DIM_MIN = 64
```

`_backend_allows_small_d` 与 `_should_use_aten_small_d_forward`：

[src/ffpa_attn/functional.py:65-81](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L65-L81) —— 注意 CUDA/SDPA 后端在这两个函数里都走不到「允许」分支：

```python
def _backend_allows_small_d(backend, head_dim) -> bool:
    if not (_FFPA_SMALL_HEAD_DIM_MIN <= head_dim <= _ATEN_SMALL_HEAD_DIM_MAX):
        return False
    if isinstance(backend, TritonBackend):
        return _allow_triton_small_d()
    if isinstance(backend, CuTeDSLBackend):
        return _allow_cute_small_d()
    return False                                  # cuda/sdpa 永远不允许小 D

def _should_use_aten_small_d_forward(forward_backend, head_dim) -> bool:
    return head_dim <= _ATEN_SMALL_HEAD_DIM_MAX and not _backend_allows_small_d(
        forward_backend, head_dim
    )
```

`fallback()` 的三分支主体——sdpa 永远回退、cutedsl 只查硬件/head_dim、默认分支用 `any([...])`：

[src/ffpa_attn/functional.py:474-522](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L474-L522) —— 默认分支的六个条件：

```python
if self.forward_meta.name == "sdpa":
    return True                                   # sdpa 前向恒回退

if self.forward_meta.name == "cutedsl":
    from .cute import (cute_forward_available, cute_max_supported_head_dim)
    cutedsl_hw_unsupported = ((
        D < _FFPA_SMALL_HEAD_DIM_MIN or (          # D<64 太小
          D <= _ATEN_SMALL_HEAD_DIM_MAX
          and not _backend_allows_small_d(self.forward_meta, D)
        )
      ) or D > cute_max_supported_head_dim(query.device)
        or not cute_forward_available(query.device))   # 硬件不支持(sm<major 8)
    return cutedsl_hw_unsupported

return any([                                       # 默认分支(triton/cuda)
    _should_use_aten_small_d_forward(self.forward_meta, D),  # D≤256 且未开 small-d
    D > 1024,
    attn_mask is not None and self.forward_meta.name == "cutedsl",  # 默认分支恒 False
    dropout_p > 0.0 and self.forward_meta.name == "cutedsl",        # 默认分支恒 False
    (8 <= Nq < 512),                               # 中等长度 query 回退；Nq<8(解码)不回退
    Nkv < 512,
])
```

cutedsl 分支调用的两个能力探测函数（设备 major≥8 才可用、上限由 SM80 路径决定）：

[src/ffpa_attn/cute/__init__.py:128-160](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L128-L160) —— `cute_forward_available` 查 `major >= 8`，`cute_max_supported_head_dim` 返回 SM80 上限：

```python
def cute_forward_available(device=None) -> bool:
    ...
    major, _ = torch.cuda.get_device_capability(device)
    return major >= 8

def cute_max_supported_head_dim(device=None) -> int:
    del device
    return SM80_SUPPORTED_HEAD_DIM
```

公共 API 里回退为 True 时走底层绑定（避免 monkey-patch 递归）：

[src/ffpa_attn/ffpa_attn_interface.py:157-168](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ffpa_attn_interface.py#L157-L168) —— `# HACK: Avoid recursive for monkey-patch usage.` 注释标注了为何用 `torch._C._nn` 而非 `F.scaled_dot_product_attention`：

```python
if meta.fallback(query, key, attn_mask, dropout_p):
    # HACK: Avoid recursive for monkey-patch usage.
    return torch._C._nn.scaled_dot_product_attention(
        query, key, value, attn_mask=attn_mask, dropout_p=dropout_p,
        is_causal=is_causal, scale=scale, enable_gqa=enable_gqa,
    )
```

#### 4.2.4 代码实践

**实践目标**：追踪 `fallback()` 逻辑，列出默认后端下所有触发回退的条件；并验证「解码（`Nq=1`）不回退、中等长度 query 回退」。

**操作步骤**（阅读 + 可选运行型实践）：

1. 读 [src/ffpa_attn/functional.py:515-522](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L515-L522)，把默认分支 `any([...])` 的每个条件抄成一张「条件 → 含义」表。
2. （可选，需 CUDA）构造下面三种用例，用 `meta.fallback(q, k, None, 0.0)` 直接判定，打印返回值：
   - 解码：`q` 形状 `[1, 32, 1, 512]`，`k`/`v` 形状 `[1, 32, 8192, 512]`。
   - 中等 query：`q` 形状 `[1, 32, 256, 512]`，`k`/`v` 形状 `[1, 32, 8192, 512]`。
   - 小 D：`q`/`k`/`v` 形状 `[1, 32, 8192, 128]`。

**需要观察的现象**：

- 解码（`Nq=1`）：`fallback` 返回 **False**（不回退，进 FFPA 解码路径）。因为 `8 <= 1 < 512` 为 False、`Nkv=8192 < 512` 为 False。
- 中等 query（`Nq=256`）：`fallback` 返回 **True**（回退 SDPA）。因为 `8 <= 256 < 512` 为 True。
- 小 D（`D=128`）：`fallback` 返回 **True**（回退 SDPA）。因为 `_should_use_aten_small_d_forward` 为 True。

**预期结果**：三种用例分别得到 False / True / True，印证「解码走 FFPA、中等 query 与小 D 回退 SDPA」。若本地无 CUDA，可纯看形状手算这三个布尔表达式，标注「待本地验证」运行部分。

#### 4.2.5 小练习与答案

**练习 1**：为什么默认分支里那两条 `... and self.forward_meta.name == "cutedsl"` 的条件看起来是「死代码」？

**答案**：因为控制流上，`name == "cutedsl"` 的请求已经在 `elif` 分支提前 `return` 了，能走到最后这个 `any([...])` 分支的只有 `triton` 或 `cuda`，它们的 `name` 不可能等于 `"cutedsl"`，所以这两条恒为 False。它们是防御性冗余（belt-and-suspenders），保证将来若有人调整分支顺序也不会把 cutedsl 的 mask/dropout 误判进 FFPA。

**练习 2**：设 `FFPA_TRITON_ALLOW_SMALL_D=1` 后，`D=128` 的默认 Triton 前向还会回退吗？

**答案**：不会。`_should_use_aten_small_d_forward` 会因 `_backend_allows_small_d` 返回 True 而变 False，于是 `fallback` 的第一条不成立；若 `Nq`/`Nkv` 都足够大，整体返回 False，FFPA Triton 会接管小 D。这正是该 env 开关「让 Triton 也跑小 D（便于测试）」的用途。

---

### 4.3 normalize_inputs()：全套输入校验

#### 4.3.1 概念说明

`normalize_inputs()` 是**只在非回退路径上运行**的完整校验函数。它做两件事：

1. **校验**所有用户输入：dtype、形状、GQA 头数关系、causal 约束、dropout 取值范围、backend 专属约束（如 cutedsl 不支持 mask/dropout、cuda 的 acc 与 dtype 配合）。
2. **填充** `attn_meta` 字段：把 `is_causal` / `dropout_p` / `is_grad_enabled` / `scale`（默认 `1/√D`）写进 meta，供 kernel 使用。

它的设计哲学是「**该报错就报错，不静默妥协**」：除了「硬件/head_dim 不匹配」会静默回退 SDPA 外，其它所有非法组合（dtype 错、形状不匹配、causal 与 mask 冲突、cutedsl 配 dropout 等）都直接抛 `TypeError` / `ValueError` / `NotImplementedError` / `RuntimeError`。

#### 4.3.2 核心流程

`normalize_inputs()` 的校验大致按以下顺序（前面的先判）：

1. `dropout_p` 范围：必须 `0 ≤ dropout_p < 1`（`=1.0` 也不行，SDPA 融合 kernel 不支持）。
2. backend 专属能力拒绝（抛 `NotImplementedError`）：
   - cutedsl + 大 D dropout
   - cutedsl + attn_mask
3. 语义冲突拒绝：
   - `attn_mask` 与 `is_causal=True` 同时设置 → `RuntimeError`
   - 布尔 `attn_mask` 还 `requires_grad` → `TypeError`
4. 填充 `attn_meta`（`is_causal` / `dropout_p` / `is_grad_enabled`）。
5. dtype 与 acc 配合：
   - cuda + bf16 + `acc='f16'` → `ValueError`（没有 bf16-acc 的 mma PTX 指令）
   - dtype 不在 `{fp16, bf16}` → `TypeError`
6. 形状校验（一批 `ValueError`）：
   - q/k/v 必须 4 维
   - 三者 batch 相同
   - key/value 头数相同
   - `Nh_q % Nh_kv == 0`（GQA 整除）
   - key/value 序列长相同
   - 三者 head_dim 相同
7. `enable_gqa=False` 但 `Nh_q != Nh_kv` → `ValueError`
8. `is_causal=True` 但 `Nkv < Nq` → `ValueError`（causal 尾对齐约定要求 `Nkv ≥ Nq`）
9. `scale=None` 时填默认 \( 1/\sqrt{D} \)。

默认 scale 的数学表达：

\[
\text{scale}_{\text{default}} = \frac{1}{\sqrt{D}}
\]

#### 4.3.3 源码精读

dropout 范围与 cutedsl 能力拒绝（`NotImplementedError`）：

[src/ffpa_attn/functional.py:546-572](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L546-L572) —— 注意 `dropout_p=1.0` 单独被拒：

```python
if not 0.0 <= dropout_p <= 1.0:
    raise ValueError(f"... dropout_p must be in [0, 1], got {dropout_p}")
if dropout_p >= 1.0:
    raise ValueError("... dropout_p=1.0 is not supported by SDPA fused kernels")
if dropout_p > 0.0 and query.size(-1) > 256 and isinstance(self.forward_meta, CuTeDSLBackend):
    raise NotImplementedError("... large-D dropout is not supported by forward_backend='cutedsl'")
if attn_mask is not None and isinstance(self.forward_meta, CuTeDSLBackend):
    raise NotImplementedError("... attn_mask is not supported by forward_backend='cutedsl'...")
if attn_mask is not None and is_causal:
    raise RuntimeError("... explicit attn_mask should not be set when is_causal=True")
if attn_mask is not None and attn_mask.dtype == torch.bool and attn_mask.requires_grad:
    raise TypeError("... boolean attn_mask cannot require gradients")
```

dtype 与 acc 配合校验：

[src/ffpa_attn/functional.py:580-589](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L580-L589) —— bf16 必须 f32 累加、dtype 只能 fp16/bf16：

```python
if isinstance(self.forward_meta, CUDABackend) and query.dtype == torch.bfloat16 \
        and self.forward_meta.acc_code == _ACC_F16:
    raise ValueError("bf16 activations require acc='f32'; no bf16-acc mma PTX exists.")
if query.dtype not in (torch.float16, torch.bfloat16):
    raise TypeError(f"ffpa_attn_func only supports fp16/bf16, got {query.dtype}")
```

形状与 GQA/causal 校验（一批 `ValueError`）：

[src/ffpa_attn/functional.py:591-624](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L591-L624) —— 注意 causal 尾对齐约束 `Nkv >= Nq`：

```python
if query.size(1) % key.size(1) != 0:
    raise ValueError("query num_heads must be an integer multiple of key/value num_heads (GQA/MQA)...")
...
if not enable_gqa and query.size(1) != key.size(1):
    raise ValueError("enable_gqa=False but query num_heads ... != key/value num_heads ...")
if is_causal and key.size(2) < query.size(2):
    raise ValueError("is_causal=True requires Nkv >= Nq (queries are aligned to the KV tail)...")
```

scale 默认值填充：

[src/ffpa_attn/functional.py:626-629](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L626-L629) —— `scale=None` 时取 `1/sqrt(D)`：

```python
if scale is None:
    self.attn_meta.scale = 1.0 / math.sqrt(query.size(-1))
else:
    self.attn_meta.scale = float(scale)
```

#### 4.3.4 代码实践

**实践目标**：触发「`is_causal=True` 但 `Nkv < Nq`」并确认抛 `ValueError`。

**操作步骤**（阅读 + 可选运行型实践）：

1. 先想清楚为什么不能直接用小 Nkv：若 `Nkv < 512`，`fallback()` 会先回退 SDPA，根本走不到 `normalize_inputs`。所以必须构造一个 **`fallback` 为 False** 但 **`Nkv < Nq`** 的用例。
2. 推导：要让 `fallback` 为 False，需 `D ∈ (256, 1024]`、`Nkv ≥ 512`、`Nq ≥ 512` 或 `Nq < 8`。再叠加 `Nkv < Nq`，得到合法选择：`D=512`、`Nkv=512`、`Nq=1024`。
3. （可选，需 CUDA）运行下面示例代码（**示例代码，非项目原有**）：

   ```python
   import torch
   from ffpa_attn import ffpa_attn_func

   q = torch.randn(1, 32, 1024, 512, dtype=torch.bfloat16, device="cuda")
   k = torch.randn(1, 32, 512,  512, dtype=torch.bfloat16, device="cuda")
   v = torch.randn(1, 32, 512,  512, dtype=torch.bfloat16, device="cuda")
   ffpa_attn_func(q, k, v, is_causal=True)   # 期望抛 ValueError
   ```

**需要观察的现象**：抛出 `ValueError: is_causal=True requires Nkv >= Nq (queries are aligned to the KV tail), got Nq=1024, Nkv=512`。

**预期结果**：因为 `D=512 / Nkv=512 / Nq=1024` 让 `fallback()` 返回 False（不回退），控制流进入 `normalize_inputs()`，被 causal 尾对齐校验拦下。若本地无 CUDA，可手动跟踪：`fallback` 的 `any([...])` 各项均为 False（`D=512` 不 ≤256 不 >1024；`8<=1024<512` 为 False；`Nkv=512<512` 为 False），故进入 normalize 抛错。运行部分标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么「`is_causal=True` 且 `Nkv < Nq`」要被拒绝？

**答案**：FFPA 的 causal 采用「query 对齐 KV 尾部」的约定：query 行 `r` 只看 key 列 `k ≤ r + (Nkv − Nq)`。当 `Nkv < Nq` 时 `Nkv − Nq < 0`，会出现某些 query 行没有任何可见 key（下界为负），掩码语义无法定义，所以直接拒绝。详见 [u2-l3](u2-l3-causal-and-attn-mask.md)。

**练习 2**：用户传了一个 fp32 的 query，会得到什么异常？为什么不是回退 SDPA？

**答案**：得到 `TypeError: ffpa_attn_func only supports fp16/bf16, got torch.float32`。因为 dtype 错误属于「非法输入」而非「FFPA 不擅长的场景」，设计上选择直接报错而不是静默回退——回退应当只用于「硬件/形状不匹配」，dtype 必须由调用方自己先转成 fp16/bf16。

---

### 4.4 normalize_attn_mask()：掩码归一化为可加 bias

#### 4.4.1 概念说明

`normalize_attn_mask()` 把用户传入的 SDPA 风格 `attn_mask` 转换成 FFPA kernel 内部统一使用的**4 维可加偏置（additive bias）**。SDPA 的 `attn_mask` 有两种语义：

- **布尔掩码**：`True` 表示该位置参与注意力，`False` 表示被屏蔽（映射为 `−∞`，softmax 后权重为 0，且不可导）。
- **可加偏置**（floating）：直接加到 `QK^T` 的 score 上（如相对位置偏置 ALiBi）。

FFPA 的 Triton kernel 只认「可加 bias」一种形式，所以布尔掩码要先用 `torch.where(mask, 0, -inf)` 转成可加。

它还有一个重要特性：**保持紧凑**。用户给的掩码常常在某些维度上广播（如 `[1, 1, 1, Nkv]` 的纯 key 偏置），`normalize_attn_mask()` **不会**把它物化展开成完整的 `[B, Nh_q, Nq, Nkv]`，而是只 `view` 成 4 维、保留零 stride 的广播维度。kernel 内部靠 stride 自动向广播维度取常量，从而省显存、省带宽。

#### 4.4.2 核心流程

1. `attn_mask is None` → 直接返回 `None`。
2. 设备一致性：掩码必须和 query 同设备，否则 `TypeError`。
3. dtype 合法性：必须是 `bool`、`float32`、或与 query 同 dtype，否则 `TypeError`。
4. 形状广播校验：交给 `_validate_attn_mask_shape`，按 SDPA 约定校验 2/3/4 维广播。
5. 布尔 → 可加：`torch.where(mask, 0, -inf)`。
6. 维度对齐：2 维 → `view(1,1,M,N)`；3 维 → `view(B,1,M,N)`；4 维保持。
7. 最末维连续：若 `stride(-1) != 1`，调用 `.contiguous()`。
8. 返回 4 维可加 `attn_bias`。

`_validate_attn_mask_shape` 的广播规则（与 SDPA 融合 kernel 约定一致）：

| 维度 | 允许的形状 | 约束 |
|---|---|---|
| 2 维 | `[Nq, Nkv]` | 广播到所有 batch/head |
| 3 维 | `[B, Nq, Nkv]` | 首维是 **batch**（不是 head） |
| 4 维 | `[B, Nh_q, Nq, Nkv]` | 才能按 head 区分 |

末两维恒为 `[Nq, Nkv]`（或 `1` 广播）；任何不满足广播的形状抛 `ValueError`。

#### 4.4.3 源码精读

设备/dtype 合法性校验：

[src/ffpa_attn/functional.py:655-667](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L655-L667) —— 三种合法 dtype：

```python
if attn_mask.device != query.device:
    raise TypeError("... attn_mask must be on the same device as query ...")
if attn_mask.dtype not in (torch.bool, torch.float32, query.dtype):
    raise TypeError("... attn_mask dtype must be bool, torch.float32, or match query dtype ...")
```

形状广播校验委托给 `_validate_attn_mask_shape`：

[src/ffpa_attn/functional.py:341-388](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L341-L388) —— 末两维必须是 `[Nq, Nkv]`、3 维首维是 batch、4 维第二维才是 head：

```python
if attn_mask.dim() not in (2, 3, 4):
    raise ValueError("... attn_mask must be 2-D, 3-D, or 4-D ...")
if attn_mask.size(-2) not in (1, seqlen_q):
    raise ValueError("... attn_mask query dimension must be 1 or {seqlen_q} ...")
if attn_mask.size(-1) not in (1, seqlen_k):
    raise ValueError("... attn_mask key dimension must be 1 or {seqlen_k} ...")
if attn_mask.dim() == 3 and attn_mask.size(0) not in (1, batch):
    raise ValueError("... 3-D attn_mask batch dimension must be 1 or {batch} ...")
if attn_mask.dim() == 4:
    if attn_mask.size(0) not in (1, batch): ...
    if attn_mask.size(1) not in (1, nheads_q): ...   # 4 维第二维才是 head
```

布尔 → 可加转换与维度对齐：

[src/ffpa_attn/functional.py:673-693](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L673-L693) —— `view` 保留广播、最末维不连续才 `.contiguous()`：

```python
if attn_mask.dtype == torch.bool:
    neg_inf = torch.tensor(float("-inf"), dtype=query.dtype, device=query.device)
    attn_bias = torch.where(attn_mask, torch.zeros((), dtype=query.dtype, ...), neg_inf)
else:
    attn_bias = attn_mask

if attn_bias.dim() == 2:
    attn_bias = attn_bias.view(1, 1, attn_bias.size(0), attn_bias.size(1))
elif attn_bias.dim() == 3:
    attn_bias = attn_bias.view(attn_bias.size(0), 1, attn_bias.size(1), attn_bias.size(2))

if attn_bias.stride(-1) != 1:
    attn_bias = attn_bias.contiguous()
return attn_bias
```

> 注意 `view`（而非 `expand`）：`view` 只改形状、不改数据，广播维度仍以零 stride 表达，kernel 内部用 stride 取常量，避免物化 `[B, Nh_q, Nq, Nkv]`。

#### 4.4.4 代码实践

**实践目标**：构造一个 `[1,1,1,Nkv]` 的纯 key 可加偏置，传入 `ffpa_attn_func`，验证它与「手动给 SDPA 加同样偏置」结果一致，并理解它没有被物化成 4 维。

**操作步骤**（可选运行，需 CUDA；否则做阅读推导）：

1. 构造 query/key/value：`[1, 32, 512, 512]` bf16。
2. 构造 `bias = torch.zeros(1, 1, 1, 512)`，给最后 64 个 key 位置加 `−10`（模拟「屏蔽尾部」）。
3. 分别计算：
   - `o_ffpa = ffpa_attn_func(q, k, v, attn_mask=bias)`
   - `o_sdpa = torch._C._nn.scaled_dot_product_attention(q, k, v, attn_mask=bias, scale=1/math.sqrt(512))`
4. 比较 `torch.testing.assert_close(o_ffpa, o_sdpa, atol=1e-2, rtol=1e-2)`。

**需要观察的现象**：

- 两者逐元素接近（bf16 容差内）。
- 进 `normalize_attn_mask` 后，`bias` 仍是 4 维且最末维长 512，但因为 batch/head/Nq 维度都是 1，它代表的是「广播的常量偏置」，并未物化成 `[1, 32, 512, 512]`。

**预期结果**：FFPA 与「加同偏置的 SDPA」数值一致；偏置保持紧凑。若本地无 CUDA，可阅读 [src/ffpa_attn/functional.py:684-692](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L684-L692) 推导 `view` 后的形状与 stride，运行部分标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：用户传了一个 3 维掩码 `[B, Nq, Nkv]`，第二维（`Nq`）实际想表达 head，会被接受吗？

**答案**：会被当作「batch 维」接受，但语义会错。`_validate_attn_mask_shape` 规定 3 维掩码的首维是 batch、不是 head；想按 head 区分必须用 4 维 `[B, Nh_q, Nq, Nkv]`。这是与 SDPA 融合 kernel 一致的约定，详见 [u2-l3](u2-l3-causal-and-attn-mask.md)。

**练习 2**：为什么最后要检查 `stride(-1) != 1` 才 `.contiguous()`？

**答案**：kernel 内部沿最末维（Nkv）做向量化的 score 累加，要求最末维在内存中连续（stride 为 1）。其余维度可以靠 stride 广播而不必连续。所以只在最末维不连续时才付出 `.contiguous()` 的拷贝代价，其它情况保留紧凑视图，省显存与带宽。

---

## 5. 综合实践

把本讲四个模块串起来，做一个「**FFPA 调度侦探**」小任务：给定一组 `(D, Nq, Nkv, is_causal, attn_mask, dropout_p, backend)`，**不实际跑 kernel**，只靠阅读源码推断这次调用会走到哪一步、得到什么结果。

请按下表逐行推断（假设 CUDA 可用、默认后端、未开 small-d env）：

| 用例 | D | Nq | Nkv | is_causal | attn_mask | 推断 `fallback()` | 推断最终结果 |
|---|---|---|---|---|---|---|---|
| A | 128 | 8192 | 8192 | False | None | ? | ? |
| B | 512 | 1 | 8192 | False | None | ? | ? |
| C | 512 | 256 | 8192 | False | None | ? | ? |
| D | 512 | 1024 | 512 | True | None | ? | ? |
| E | 512 | 8192 | 8192 | False | bool 掩码 + cutedsl 后端 | ? | ? |

**参考答案**：

- A：`fallback` = True（`D=128 ≤ 256` → aten 小 D）。回退 SDPA。
- B：`fallback` = False（解码 `Nq=1`，`8<=1` 为 False，`Nkv=8192` 不 <512）。进 FFPA 解码路径。
- C：`fallback` = True（`8 <= 256 < 512`）。回退 SDPA。
- D：`fallback` = False（`D=512`、`Nq=1024` 不 <512、`Nkv=512` 不 <512）。进 `normalize_inputs`，被 `is_causal=True requires Nkv >= Nq`（`512 < 1024`）拦下，抛 `ValueError`。
- E：`fallback`（cutedsl 分支）硬件/head_dim 满足时返回 False → 进 `normalize_inputs`，被「cutedsl + attn_mask」拦下，抛 `NotImplementedError`。

完成推断后，再阅读 [src/ffpa_attn/ffpa_attn_interface.py:156-181](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ffpa_attn_interface.py#L156-L181) 与 [src/ffpa_attn/functional.py:474-631](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L474-L631) 自查每一步是否一致。

## 6. 本讲小结

- 公共 API `ffpa_attn_func` 在进入 kernel 前有三步：`from_kwargs` 构造 meta → `fallback()` 回退判定 → `normalize()` 校验归一化；**回退判定发生在校验之前**。
- `FFPAAttnMeta` 是装着「两个后端配置 + 注意力选项」的非张量调度信封，`from_kwargs` 按优先级「显式前后端 > `backend` 简写 > 默认 Triton」解析，未知 kwarg 立即报错。
- `fallback()` 默认后端下，`D ≤ 256`、`D > 1024`、`8 ≤ Nq < 512`、`Nkv < 512` 任一成立即回退 SDPA；**解码 `Nq < 8` 故意不回退**，走 FFPA 解码路径。
- `_should_use_aten_small_d_forward` 把 `D ≤ 256` 的小 D 判定交给 aten，只有 `FFPA_TRITON_ALLOW_SMALL_D` / `FFPA_CUTE_ALLOW_SMALL_D` 才能让 Triton/CuTeDSL 接管小 D。
- `normalize_inputs()` 是只在非回退路径运行的完整校验，对 dtype/形状/GQA/causal/dropout/backend 专属约束一律 fail-fast 报错，并把 `scale` 默认填为 \( 1/\sqrt{D} \)。
- `normalize_attn_mask()` 把布尔/可加掩码统一归一化为紧凑 4 维可加 bias，`view` 保留广播、不物化，最末维不连续才 `.contiguous()`。

## 7. 下一步学习建议

本讲讲清了「进入 kernel 之前」的回退判定与校验。下一讲 [u3-l4 FFPAAttnFunc autograd Function 前向/反向分发](u3-l4-autograd-function-dispatch.md) 将进入「校验之后」的世界：`FFPAAttnFunc.apply` 如何按 `head_dim` 与 backend 把前向分发到 aten/cuda/triton/cute、`backward` 如何按 `backward_meta` 路由反向 kernel，以及 `save_for_backward` 保存了哪些张量。建议同时阅读：

- [src/ffpa_attn/functional.py:746-850](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L746-L850)（`_FFPAAttnFunc.forward`）
- [src/ffpa_attn/functional.py:852-943](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L852-L943)（`_FFPAAttnFunc.backward`）

若你想更理解回退后的解码路径，可跳读 [u4-l3 Decode 前向：split-KV 两阶段](u4-l3-decode-fwd-split-kv.md)。
