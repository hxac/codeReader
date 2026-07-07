# torch.compile 兼容与 torch.library 自定义算子

## 1. 本讲目标

本讲解决一个工程问题：FFPA 的手写 Triton / CUDA kernel 是「外部黑盒」，而用户又希望用 `torch.compile` 加速整网训练。Dynamo 在 trace 代码时并不认识这些 kernel，怎么让它「看见」、又怎么保证反向传播仍按用户指定的后端走？

学完本讲你应当能：

- 说清 `torch.library` 的「三件套」`define` / `impl` / `register_fake` 各自的作用，尤其是 `register_fake`（meta 实现）对 `torch.compile` 的意义。
- 解释为什么 FFPA **不能** 用 `torch.library.register_autograd` 把前向算子绑定到单一反向公式——核心是「一个前向后端对应多个可选反向后端」。
- 理解 `_ffpa_apply` 用 `@torch._dynamo.disable` 在 autograd 边界主动制造「图断（graph break）」的设计动机。

本讲承接 [u3-l4](u3-l4-autograd-function-dispatch.md)（`_FFPAAttnFunc` 的前向/反向分发），把视线从「运行时如何分发」转移到「如何在 `torch.compile` 下安全地暴露这套分发」。

## 2. 前置知识

本讲需要几个 PyTorch 生态概念，下面用最朴素的方式解释：

- **`torch.compile` / Dynamo / Inductor**：`torch.compile` 是 PyTorch 2.x 的加速入口。它先用 **Dynamo** 把 Python 函数 *trace*（追踪）成一张「算子图」，再用 **Inductor** 把这张图编译成高性能代码（Triton/C++）。trace 阶段，Dynamo 不会真正执行算子，而是用「假张量」推演每个算子的输出形状/dtype。
- **图断（graph break）**：当 Dynamo 遇到它无法 trace 的代码（例如不认识的 Python 逻辑），它会在这里「断开」，把断点之前/之后分别编译成两张子图，中间这段以「急切（eager）」模式原样运行。图断不是错误，但会削弱编译收益。
- **FakeTensor / meta 实现**：一种「只有形状和 dtype、没有真实数据」的张量。Dynamo trace 时调用算子的 *meta 实现*，只为推导输出形状，不跑真正的 kernel。
- **`torch.library` 自定义算子**：PyTorch 官方注册自定义 C/Python 算子的 API。注册后算子出现在 `torch.ops.<名字空间>.<算子名>` 下，获得统一的类型签名、调度与 `torch.compile` 支持。
- **`torch.autograd.Function`**：定义「前向 + 反向」一对函数以获得自定义梯度的经典机制（FFPA 的 `_FFPAAttnFunc` 就是它）。
- **`register_autograd`**：把一个已注册的「前向算子」绑定到一个「反向公式」，让 `torch.compile` 编译出的图自带可微性。它要求**一前向对应唯一一反向**。

复习一个 u3 系列已经建立的关键事实（来自 [u3-l2](u3-l2-backend-config-dataclasses.md) 与 [u3-l4](u3-l4-autograd-function-dispatch.md)）：FFPA **故意把前向后端与反向后端解耦**，前向走 `forward_meta`、反向走 `backward_meta`，二者可以不同。这一条是本讲几乎所有设计的根因。

## 3. 本讲源码地图

| 文件 | 本讲关注的内容 |
| --- | --- |
| [src/ffpa_attn/functional.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py) | 顶部关于 `register_autograd` 的注释、`_ffpa_apply`（`@torch._dynamo.disable`）、`FFPAAttnFunc` 包装类、`_ffpa_varlen_apply` |
| [src/ffpa_attn/triton/\_\_init\_\_.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/__init__.py) | `_fwd_triton` / `_bwd_triton` 的 `define` + `impl("CUDA")` + `register_fake` 三件套 |
| [src/ffpa_attn/triton/_ffpa_fwd.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py) | Python 包装 `_ffpa_attn_forward_triton` 如何调用 `torch.ops.ffpa_attn._fwd_triton` |
| [src/ffpa_attn/cuda/\_\_init\_\_.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cuda/__init__.py) | `_fwd_cuda` 三件套（只有前向算子，没有 `_bwd_cuda`） |
| [src/ffpa_attn/cute/\_\_init\_\_.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py) | dense 路径（无 `register_autograd`）与 varlen 路径（`custom_op` + `register_autograd`）的对照 |

## 4. 核心概念与源码讲解

### 4.1 torch.compile 与自定义算子的张力

#### 4.1.1 概念说明

FFPA 的真正计算在 Triton kernel（`_ffpa_attn_forward_impl`）和手写 CUDA kernel（`_C.ffpa_attn_forward`）里。从 Dynamo 的角度看，这些都是「看不见内部」的黑盒：Dynamo 既不知道它们的输出形状，也无法把它们分解（decompose）成已知算子。

如果直接在一个被 `torch.compile` 包裹的函数里调用 `_ffpa_attn_forward_impl(...)`，会出现两种坏结果：

1. Dynamo 在此处**图断**，整段 FFPA 调用退化成急切执行，编译收益归零。
2. 更糟的是，Dynamo 可能误把含随机性的 kernel 当成可 trace 的普通 Python，导致 trace 出来的图与真实运行行为不一致。

FFPA 的对策是：**把这些黑盒 kernel 包成正式的 `torch.library` 自定义算子**，让 Dynamo 把它当成图里的一个「不透明的合法节点」，既保留编译（前后的算子仍被编译/融合），又不在节点内部乱 trace。这就是本讲的出发点。

#### 4.1.2 核心流程

一个自定义算子要让 `torch.compile` 满意，需要三件套：

```text
1. define       ──►  声明算子的「签名 schema」（参数/返回的 Tensor 类型与基本类型）
                        之后 torch.ops.ffpa_attn._fwd_triton 才存在
2. impl("CUDA") ──►  真正的实现：跑 Triton/CUDA kernel（仅在真 CUDA 张量上触发）
3. register_fake ──► meta 实现：用 fake tensor 推导输出形状/dtype（Dynamo trace 时调用）
```

Dynamo trace 时只调用 `register_fake`；真正运行（或 Inductor 生成代码后）才调用 `impl`。两者职责分离，是 `torch.compile` 兼容的关键。

#### 4.1.3 源码精读

FFPA 的 Triton 后端把这三个动作集中在 [src/ffpa_attn/triton/\_\_init\_\_.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/__init__.py) 顶部。先看名字空间常量：

- [_OP_NAMESPACE = "ffpa_attn"](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/__init__.py#L175) —— 所有 FFPA 算子都注册在 `ffpa_attn` 名字空间下，调用入口是 `torch.ops.ffpa_attn.<算子名>`。

CUDA 后端采用同样的名字空间（[src/ffpa_attn/cuda/\_\_init\_\_.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cuda/__init__.py#L20)），所以四个后端的算子共享一个名字空间、互不冲突。

#### 4.1.4 代码实践

**实践目标**：确认「注册算子」之后，`torch.ops.ffpa_attn._fwd_triton` 这个符号确实存在且带签名。

**操作步骤**：

1. 在已安装 FFPA 的环境里执行：
   ```python
   import torch
   import ffpa_attn  # 触发 triton/__init__.py 的 define
   op = torch.ops.ffpa_attn._fwd_triton
   print(op)
   print(op._schemas if hasattr(op, "_schemas") else "no schema attr")
   ```
2. 对比一个**未注册**的假算子：`torch.ops.ffpa_attn._does_not_exist` 会抛 `AttributeError`（或返回一个 `OpNotFound` 占位），而 `_fwd_triton` 不会。

**需要观察的现象**：`_fwd_triton` 打印出来是一个 `OpOverload` 对象，且能查到它的 schema（形如 `(Tensor q, Tensor k, ...) -> (Tensor o, Tensor softmax_lse)`）。

**预期结果**：注册成功的算子是「一等公民」，可在 `torch.compile` 图里作为一个节点出现；未注册的符号则不可见。

> 待本地验证：本步骤需要在装好 FFPA（且能 `import torch` ≥ 2.10）的环境里运行；本讲作者无法在此替你执行。

#### 4.1.5 小练习与答案

**练习 1**：如果不调用 `torch.library.define`，直接在代码里写 `torch.ops.ffpa_attn._fwd_triton(...)`，会发生什么？

**参考答案**：`torch.ops.ffpa_attn._fwd_triton` 这个符号根本不存在（`define` 负责创建它），访问时会抛 `AttributeError` / `OpNotFound`。`define` 是算子「出生证明」。

**练习 2**：为什么把名字空间统一成 `ffpa_attn`，而不是每个后端一个名字空间（如 `ffpa_triton` / `ffpa_cuda`）？

**参考答案**：因为分发层（`functional.py`）按 `forward_meta` 在运行时决定调哪个后端的算子；统一名字空间让分发逻辑只挑「算子名」，命名上不与后端耦合，也避免算子名爆炸。

### 4.2 torch.library 三件套精读：以 `_fwd_triton` 为例

#### 4.2.1 概念说明

`_fwd_triton` 是 Triton 前向在 `torch.library` 里的正式面孔。Python 包装函数 `_ffpa_attn_forward_triton`（在 `_ffpa_fwd.py` 里）**不再包含任何 kernel 代码**，它的全部职责是：把 Python 风格的参数（`bool`、`str`）**整型化**后，转交给 `torch.ops.ffpa_attn._fwd_triton`。

这里有个容易忽略的细节：算子 schema 里所有「布尔开关」都被编码成 `int`（0/1），字符串枚举（如 `"fast"/"max"`）也被编码成 `int`。这是因为算子 schema 对基本类型的约束比普通 Python 函数严格，用 `int` 传递最稳妥，由 `impl` 内部用 `bool(...)` 还原。

#### 4.2.2 核心流程

`_fwd_triton` 三件套的协作流程：

```text
_ffpa_attn_forward_triton(Q,K,V,...)        ← Python 包装（_ffpa_fwd.py）
        │  把 causal/autotune/enable_tma 等 bool → int
        ▼
torch.ops.ffpa_attn._fwd_triton(...)        ← 注册算子（define 创建）
        │
        ├── Dynamo trace 时 ──► _fwd_triton_fake(...)        （register_fake）
        │                       只算 o / softmax_lse 的形状
        │
        └── 真 CUDA 运行时 ──► _fwd_triton_torch_op(...)      （impl "CUDA"）
                                懒导入 _ffpa_attn_forward_impl
                                连续化 → 分配输出 → 跑 Triton kernel
```

注意 `impl` 内部对真正 kernel 是**懒导入**（`from ._ffpa_fwd import _ffpa_attn_forward_impl`），这样在 trace 期或 CPU 环境下不会强行加载 kernel 模块。

#### 4.2.3 源码精读

**(a) define —— 声明 schema**

[define 声明 `_fwd_triton` 的签名](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/__init__.py#L266-L272)：

```python
torch.library.define(
  f"{_OP_NAMESPACE}::_fwd_triton",
  "(Tensor q, Tensor k, Tensor v, Tensor? attn_bias, float softmax_scale, "
  "int causal, int autotune, int autotune_mode_is_max, float dropout_p, int philox_seed, int philox_offset, "
  "int enable_tma, int enable_ws) "
  "-> (Tensor o, Tensor softmax_lse)",
)
```

读 schema：三个 `Tensor`（q/k/v）+ 一个可空 `Tensor? attn_bias` + 若干 `float`/`int` 标量，返回一对 `(o, softmax_lse)`。注意 `causal`、`autotune`、`enable_tma`、`enable_ws` 都是 `int`——这就是上面说的「布尔→整型」编码。

**(b) impl —— 真正的 CUDA 实现**

[impl 注册到 `"CUDA"` dispatch key](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/__init__.py#L275-L327)：

```python
@torch.library.impl(f"{_OP_NAMESPACE}::_fwd_triton", "CUDA")
def _fwd_triton_torch_op(q, k, v, attn_bias, softmax_scale, causal, ...):
  from ._ffpa_fwd import _ffpa_attn_forward_impl as _triton_fwd_kernel  # 懒导入
  if q.stride(-1) != 1: q = q.contiguous()   # 末维连续化
  ...
  o = torch.empty_like(q)
  seqlen_q_aligned = ((seqlen_q + 127) // 128) * 128
  softmax_lse = torch.empty(q.size(0), q.size(1), seqlen_q_aligned, ...)
  _triton_fwd_kernel(q, k, v, o, softmax_lse, ..., enable_ws=bool(enable_ws))
  return o, softmax_lse
```

要点：`"CUDA"` 表示这个实现只在 CUDA 后端调度时触发（在 CPU 上调用会因无实现而报错）；`int` 参数在这里用 `bool(...)` 还原成 Python 布尔再传给 kernel。

**(c) register_fake —— meta 实现（Dynamo 用的形状推导）**

[register_fake 给出 fake 实现](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/__init__.py#L330-L351)：

```python
@torch.library.register_fake(f"{_OP_NAMESPACE}::_fwd_triton")
def _fwd_triton_fake(q, k, v, attn_bias, softmax_scale, causal, ...):
  seqlen_q_aligned = ((q.size(2) + 127) // 128) * 128
  o = torch.empty_like(q)
  softmax_lse = q.new_empty(q.size(0), q.size(1), seqlen_q_aligned, dtype=torch.float32)
  return o, softmax_lse
```

它和 `impl` 里的形状计算**逐行对应**（同样的 `seqlen_q_aligned`、同样的 `o`/`softmax_lse` 形状），但完全不跑 kernel、不分配真实显存——`q` 此时是一个 fake tensor，`torch.empty_like(q)` 也只描述形状。这就是 Dynamo trace 时调用的版本。

**(d) Python 包装如何调用注册算子**

[包装函数 `_ffpa_attn_forward_triton` 调用 `torch.ops.ffpa_attn._fwd_triton`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L1500-L1514)：

```python
O_storage, softmax_lse_storage = torch.ops.ffpa_attn._fwd_triton(
  Q, K, V, attn_bias, softmax_scale,
  int(causal), int(autotune), int(autotune_mode == "max"),
  dropout_p, philox_seed, philox_offset,
  int(enable_tma), int(enable_ws),
)
```

这里清楚展示了「`bool → int`」与「`str 'max'/'fast' → int`」的整型化：`int(causal)`、`int(autotune_mode == "max")`。`_FFPAAttnFunc.forward` 调用的就是这个包装（见 [u3-l4](u3-l4-autograd-function-dispatch.md)）。

CUDA 后端的 `_fwd_cuda` 三件套结构完全一致（[cuda/\_\_init\_\_.py 的 define/impl/register_fake](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cuda/__init__.py#L23-L101)），唯一区别是它**只注册了前向算子、没有 `_bwd_cuda`**——因为 CUDA 后端天生没有反向（见 [u3-l1](u3-l1-four-backends-overview.md) 能力矩阵）。这一点会在 4.3 节成为关键。

#### 4.2.4 代码实践

**实践目标**：体会「schema 用 `int` 编码布尔」这一约定，并验证 meta 实现与真实现的形状一致。

**操作步骤**：

1. 阅读 [triton/\_\_init\_\_.py#L266-L351](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/__init__.py#L266-L351)，数一数 schema 里有几个 `int` 类型的「布尔/枚举」参数（答案：`causal, autotune, autotune_mode_is_max, enable_tma, enable_ws` 共 5 个）。
2. 对照 [cuda/\_\_init\_\_.py#L81-L101](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cuda/__init__.py#L81-L101) 的 `_fwd_cuda_fake`，注意它的 `seqlen_q_aligned = ((Q.size(2) + 7) // 8) * 8` 与 Triton 的 `((.. + 127)//128)*128` **对齐粒度不同**（CUDA 按 8、Triton 按 128 对齐）。这说明 meta 实现必须与各自的真实现严格逐字对应，不能复用。

**需要观察的现象**：两个后端的 LSE 对齐粒度不同，但 `register_fake` 与同后端的 `impl` 内部计算**完全相同**。

**预期结果**：你能用自己的话总结「为什么 meta 实现不能跨后端复用」——因为每个后端的真实现里形状计算细节（对齐粒度）不同，meta 必须如实复刻。

> 待本地验证：第 2 步的形状对齐差异属源码阅读结论，可直接核对；如要运行 `register_fake`，需构造 fake tensor 调用 `torch.library.opcheck(..., test="fake")`，需本地 GPU/包环境。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_fwd_triton_torch_op`（impl）里要用 `from ._ffpa_fwd import _ffpa_attn_forward_impl` 做**懒导入**，而不是写在文件顶部？

**参考答案**：注册算子的 `impl` 在模块 import 时就被装饰注册，但 kernel 应当**只在真要跑时**才加载。懒导入避免在 trace 期、CPU 环境、或仅做形状推导时强行 import 重型 Triton/CUDA 模块，也缩短 import 时间。

**练习 2**：`softmax_lse` 的第三维被对齐到 128 的倍数，但 Python 包装最后又切片 `[..., :seqlen_q]`（见 [_ffpa_fwd.py#L1515](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L1515)）。这说明了什么？

**参考答案**：kernel 出于对齐/向量化需要把 LSE 缓冲区 padding 到 128 倍数，但对外可见的有效长度仍是真实 `seqlen_q`。meta 实现与真实现都先按对齐长度分配，再由包装层裁剪——两端必须一致，否则 Dynamo 推导的形状会与运行时不符。

### 4.3 一前向多反向：为什么不能用 `register_autograd`

#### 4.3.1 概念说明

`torch.library.register_autograd` 的语义是：给一个**前向算子**绑定**唯一一个反向公式**，于是 `torch.compile` 编译出的整张图就自带可微性。这本是「最优雅」的方案——算子既被编译、又自动可微。

但 FFPA 偏偏不能用。原因在于 [u3-l2](u3-l2-backend-config-dataclasses.md)/[u3-l4](u3-l4-autograd-function-dispatch.md) 反复强调的那条：**前向后端与反向后端是解耦的、可独立选择的**。一个前向算子在运行时可能配好几种不同的反向 kernel，具体走哪个取决于用户传入的 `backward_backend`，这是一个**运行时的 Python 决策**。

#### 4.3.2 核心流程

FFPA 的前向↔反向组合矩阵（来自源码注释，下一节给出）：

| 前向后端 (`forward_backend`) | 可配的反向后端 (`backward_backend`) |
| --- | --- |
| `sdpa` | 不适用（前向永远经 `meta.fallback()` 短路回退，不走 `_FFPAAttnFunc`） |
| `cuda` | `triton`、`sdpa` |
| `triton` | `triton`、`sdpa` |
| `cutedsl` | `cutedsl`、`triton`、`sdpa` |

注意 `cuda` 前向有**两个**合法反向；`cutedsl` 前向有**三个**。这意味着不存在「一个前向算子 → 唯一反向公式」的映射。

如果硬用 `register_autograd` 把 `_fwd_cuda` 绑到（比如说）Triton 反向，后果是：用户在 `torch.compile(fullgraph=True)` 下设置 `backward_backend='sdpa'` 时，**这个选择会被静默忽略**——编译图里写死了 Triton 反向公式。这就破坏了 SDPA 反向路径，而且**不报错**，是最危险的正确性 bug。

#### 4.3.3 源码精读

[functional.py 顶部关于 `register_autograd` 的注释](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L946-L965) 把这件事说得很明白：

```python
# We cannot use ``torch.library.register_autograd`` on the forward ops
# (``_fwd_cuda`` / ``_fwd_triton``) because each forward backend supports
# *multiple* backward backends selected at runtime via ``backward_backend``:
#
#   forward_backend   │  backward_backend
#   ──────────────────┼───────────────────
#   sdpa              │  (n/a — always short-circuits via meta.fallback())
#   cuda              │  triton, sdpa
#   triton            │  triton, sdpa
#   cutedsl           │  cutedsl, triton, sdpa
#
# ``register_autograd`` binds a forward op to exactly one backward formula.
# Hard-coding one backward (e.g. always Triton) would silently ignore the
# user-requested ``backward_backend`` under ``torch.compile``, breaking the
# sdpa backward path when ``fullgraph=True``.
```

注释紧接着给出 FFPA 的替代方案（这其实是 4.4 节的内容，但它在同一段里）：保留 `torch.autograd.Function`（`_FFPAAttnFunc`），让它的 `backward` 在运行时读 `meta.backward_meta` 做完整分发；再用 `@torch._dynamo.disable` 把这个 autograd 边界从 Dynamo 里隔离开。

反向算子本身也被注册成 `torch.library` 算子：[triton 的 `_bwd_triton` 三件套](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/__init__.py#L354-L498) 与前向同构（`define` + `impl("CUDA")` + `register_fake`）。但**反向算子是「被 `_FFPAAttnFunc.backward` 主动调用的工具」，而不是「绑定到前向算子上的 autograd 公式」**——这一区别正是「不使用 `register_autograd`」的体现。

> 对照：CuTeDSL 的 **varlen** 路径是另一番景象。varlen 只有一个反向公式，所以它**可以**也**确实**用了 `@custom_op` + `register_autograd`（见 [cute/\_\_init\_\_.py#L708](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L708) 与 [#L940](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L940)）。而 dense 的 `_fwd_cute` / `_bwd_cute` 注释明确写着「No `register_autograd`: backward is managed by `_FFPAAttnFunc`」（[cute/\_\_init\_\_.py#L510-L516](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L510-L516)）。dense 与 varlen 的对比能帮你彻底理解这条边界。

#### 4.3.4 代码实践

**实践目标**：亲手验证「`forward_backend='cuda'` 配 `backward_backend='sdpa'`」这一组合在源码层面是合法的，从而论证为何它无法被 `register_autograd` 表达。

**操作步骤**：

1. 在 [functional.py#L946-L960](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L946-L960) 找到注释里的组合矩阵，确认 `cuda` 行确有两个反向候选。
2. 翻到 [functional.py 的 `_FFPAAttnFunc.backward`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L852-L943)（[u3-l4](u3-l4-autograd-function-dispatch.md) 已精读），确认反向分支只看 `meta.backward_meta` 的类型（`TritonBackend` / `CuTeDSLBackend` / `SDPABackend`），而**不看**前向用了什么。这正是「一前向多反向」的实现基础。
3. 用一段假想论证：假如把 `_fwd_cuda` 用 `register_autograd` 绑定到 Triton 反向公式，那么当用户传 `backward_backend=SDPABackend()` 时，`_FFPAAttnFunc.backward` 那段 `isinstance(meta.backward_meta, SDPABackend)` 的分支就**永远不会在编译图里被执行**——因为编译图里的反向是写死的 register_autograd 公式。

**需要观察的现象**：`backward` 的分发依据是 `backward_meta`，与前向 `forward_meta` 完全独立；这种独立性无法被「一个前向算子绑一个反向」的 `register_autograd` 表达。

**预期结果**：你能写出一段话，逻辑链为「前向多反向 → `register_autograd` 只能绑一个 → 强绑会静默忽略 `backward_backend` → 因此 FFPA 选择不绑、改用 `autograd.Function` + 图断」。

> 本实践为源码阅读型，结论可直接从注释与 `backward` 源码得出，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：为什么注释特别强调「under `torch.compile`, breaking the sdpa backward path when `fullgraph=True`」？如果是 `fullgraph=False`（允许图断），还会有这个问题吗？

**参考答案**：`fullgraph=True` 禁止图断，强制整张图被编译并使用注册的 autograd 公式——此时写死的反向一定会覆盖用户的 `backward_backend`。`fullgraph=False` 允许图断，FFPA 本可以用 `_ffpa_apply` 的图断绕开（这正是 4.4 节方案），但 FFPA 不希望依赖「恰好图断」来保证正确性，所以从根上拒绝 `register_autograd`。

**练习 2**：varlen 路径为什么**敢**用 `register_autograd`？

**参考答案**：varlen 只有 CuTeDSL 一个后端、只有一个反向公式（`_varlen_fwd_backward`），满足「一前向一反向」前提，所以可以安全绑定。dense 路径因前向多反向而不行——这是场景差异，不是技术偏好。

### 4.4 `_ffpa_apply` 的 `dynamo.disable` 图断设计

#### 4.4.1 概念说明

既然不能用 `register_autograd`，反向就只能留在 `torch.autograd.Function`（`_FFPAAttnFunc`）里。但 `torch.autograd.Function.apply` 在 `torch.compile` 下有另一个坑：Dynamo 会尝试**内联（inline）** 这个 Function，并为其生成一个模板化的反向。对于 Dynamo 看不穿的自定义 Function，这个模板反向会**产生零梯度**——因为它无法理解你在 `backward` 里基于 `backward_meta` 的运行时分发。

源码注释的原话是：Dynamo 会「replace the real backward with an auto-generated template that produces zero gradients」（见 [functional.py#L974-L979](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L974-L979) 的 `FFPAAttnFunc` docstring）。

FFPA 的解法很直接：**主动制造一次图断**，让 Dynamo 不要碰这个 autograd 边界，把 `_FFPAAttnFunc` 的前向/反向原封不动地交给急切执行。

#### 4.4.2 核心流程

图断机制：

```text
ffpa_attn_func(...)
   └─► FFPAAttnFunc.apply(...)              # 公共包装类（见下）
          └─► _ffpa_apply(...)              # @torch._dynamo.disable  ◄── 图断在此
                 └─► _FFPAAttnFunc.apply(...)   # 真 autograd.Function
                        ├─ forward:  按 forward_meta 分发 → torch.ops.ffpa_attn._fwd_*
                        └─ backward: 按 backward_meta 分发 → torch.ops.ffpa_attn._bwd_* / SDPA
```

`@torch._dynamo.disable` 的语义是「Dynamo 看到 `_ffpa_apply` 就停手，把它当成一个不透明的急切调用」。结果是：

- `torch.compile` 不会把 `_FFPAAttnFunc` 内联，也就不会生成错误的模板反向。
- `_FFPAAttnFunc.forward`/`backward` 在急切模式下完整运行，`backward_meta` 的运行时分发得以保留。
- 代价：FFPA 调用处会有一次图断，编译收益仅体现在 FFPA **之外**的算子上。

#### 4.4.3 源码精读

**图断守卫 `_ffpa_apply`**：

[_ffpa_apply 用 @torch._dynamo.disable 包裹 _FFPAAttnFunc.apply](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L966-L968)：

```python
@torch._dynamo.disable
def _ffpa_apply(*args, **kwargs):
  return _FFPAAttnFunc.apply(*args, **kwargs)
```

**公共包装类 `FFPAAttnFunc`**：

[FFPAAttnFunc 把 apply 委托给 _ffpa_apply](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L971-L987)：

```python
class FFPAAttnFunc:
  """Public-facing autograd Function wrapper.

  ``_FFPAAttnFunc`` holds the real ``forward`` / ``backward``, but its
  auto-generated ``apply`` cannot be directly called under
  ``torch.compile`` — Dynamo would inline it and replace the real backward
  with an auto-generated template that produces zero gradients.  This
  wrapper delegates to :func:`_ffpa_apply`, which is guarded by
  ``torch._dynamo.disable`` so Dynamo leaves the autograd boundary intact.
  """
  @classmethod
  def apply(cls, *args, **kwargs):
    return _ffpa_apply(*args, **kwargs)
```

注意：`_FFPAAttnFunc`（带下划线、真正的 `torch.autograd.Function`）与 `FFPAAttnFunc`（无下划线、公共包装类）是**两个不同的对象**。后者只是个有 `apply` 类方法的薄壳，目的是让外部调用点（如 [ffpa_attn_interface.py#L181](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ffpa_attn_interface.py#L181) 的 `FFPAAttnFunc.apply(...)`）天然经过图断。需要检视真前向/反向实现的代码可以另用 `_FFPAAttnFunc`。

**varlen 走同一套**：varlen 路径也用同样的图断守卫 [_ffpa_varlen_apply](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L990-L1020)，并由 [FFPAAttnVarlenFunc](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L1023-L1033) 包装——尽管 varlen 自己用 `custom_op` 管 autograd，但同样需要在 `torch.compile` 下断图，原因相同。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：（1）用自己的话解释「`forward_backend='cuda'` 配 `backward_backend='sdpa'`」为何不能用 `register_autograd`；（2）用 `torch.compile` 包裹一次 `ffpa_attn_func` 调用，验证它**不报错**，并观察 FFPA 调用处发生的图断。

**操作步骤**：

1. 阅读并复述 [functional.py#L946-L965](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L946-L965) 的注释，写一段话回答本实践第 (1) 问（参考 4.3.4 的结论）。
2. 编写下面这段验证脚本（**示例代码**，需在装好 FFPA 的 CUDA 环境运行）：
   ```python
   # 示例代码：验证 torch.compile 下 ffpa_attn_func 不报错 + 观察图断
   import torch
   from ffpa_attn import ffpa_attn_func

   B, H, N, D = 1, 32, 8192, 512  # 大 D，确保走 FFPA 而非回退 SDPA
   q = torch.randn(B, H, N, D, dtype=torch.bfloat16, device="cuda", requires_grad=True)
   k = torch.randn(B, H, N, D, dtype=torch.bfloat16, device="cuda", requires_grad=True)
   v = torch.randn(B, H, N, D, dtype=torch.bfloat16, device="cuda", requires_grad=True)

   @torch.compile
   def fn(q, k, v):
       out = ffpa_attn_func(q, k, v)
       return out.sum()           # 标量，便于反向

   # 前向（应不报错）
   loss = fn(q, k, v)
   # 反向（验证梯度不为零，证明图断保留了真反向）
   loss.backward()
   print("q.grad is None?", q.grad is None, "| q.grad abs-sum:", q.grad.abs().sum().item())

   # 用 dynamo.explain 查看 graph break
   fn_raw = fn  # torch.compile 返回的可调用对象
   # 取原始函数做 explain：
   explain = torch._dynamo.explain(lambda qq, kk, vv: ffpa_attn_func(qq, kk, vv).sum())(q, k, v)
   print("graph_breaks:", len(explain.graph_breaks))
   ```
3. （可选）若想看到图断发生在 `_ffpa_apply`，可设置环境变量 `TORCH_LOGS="+dynamo"` 后再运行，在日志里搜索 `_ffpa_apply` 或 `graph break`。

**需要观察的现象**：

- `fn(q,k,v)` 与 `loss.backward()` 均**不报错**。
- `q.grad is None` 应为 `False`，且 `q.grad.abs().sum()` 是一个**正的有限数**——这证明 `@torch._dynamo.disable` 的图断成功保留了 `_FFPAAttnFunc` 的真反向（而不是被模板反向替换成零梯度）。
- `torch._dynamo.explain(...)` 报告的 `graph_breaks` 数量 ≥ 1。

**预期结果**：图断发生在 `_ffpa_apply`（即 `@torch._dynamo.disable` 守卫处），FFPA 内部的前向/反向以急切模式运行，`backward_backend` 的运行时分发不受编译影响，梯度正常。

> 待本地验证：上述运行结果需要 CUDA GPU（建议 Hopper/Ada，大 D=512 走 FFPA 而非回退）与已构建的 FFPA 包。本讲作者无法在此执行，请以本地实测为准；若 `torch._dynamo.explain` 的 API 在你的 torch 版本字段名略有差异，以本地版本为准。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `@torch._dynamo.disable` 从 `_ffpa_apply` 上去掉，`torch.compile(fn)` 还能正常工作吗？梯度会怎样？

**参考答案**：编译本身可能不立即报错，但 Dynamo 会内联 `_FFPAAttnFunc`，用模板反向替换真反向，导致 `q.grad` 变成 0（或 `None`）。这正是源码 docstring 警告的「zero gradients」。图断守卫是防止该问题的根本手段。

**练习 2**：`_ffpa_apply` 用 `*args, **kwargs` 透传，而不是写出完整参数列表。这样写的好处与风险各是什么？

**参考答案**：好处是 `_FFPAAttnFunc.apply` 签名若变化，`_ffpa_apply` 无需同步修改，维护成本低。风险是失去静态参数检查，传错参数要等到运行时才报错；不过 `_FFPAAttnFunc.apply` 的调用方都在仓库内部（`ffpa_attn_interface.py` 等），可控性可接受。

## 5. 综合实践

把本讲的三条主线串成一条「`torch.compile` 下的完整调用链追踪」：

1. **起点**：从 [ffpa_attn_interface.py 的 `ffpa_attn_func`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ffpa_attn_interface.py#L71-L181) 出发，确认它最终调 `FFPAAttnFunc.apply`（公共包装类）。
2. **图断**：进入 [functional.py 的 `FFPAAttnFunc.apply` → `_ffpa_apply`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L966-L987)，标出 `@torch._dynamo.disable` 制造图断的位置。用一句话说明：为什么这里必须断图（提示：`autograd.Function` 内联 → 模板反向 → 零梯度）。
3. **前向算子**：沿 `_FFPAAttnFunc.forward` 的 Triton 分支，走到 [triton/\_\_init\_\_.py 的 `_fwd_triton` 三件套](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/__init__.py#L266-L351)，分别写出 `define` / `impl` / `register_fake` 三者各自的「调用时机」（trace 期 or 运行期）。
4. **反向分发**：在 [`_FFPAAttnFunc.backward`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L852-L943) 里指出反向分支依据是 `backward_meta`（与前向无关），并联系 [4.3 的注释矩阵](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L946-L965)，解释这正是不用 `register_autograd` 的根因。
5. **验证**：运行 4.4.4 的脚本，确认编译不报错、梯度非零、`explain` 报告了图断。

产出物：一张标注了「图断点 / trace 期调用 / 运行期调用 / 运行时反向分发依据」的调用链图，加一段解释「为什么 FFPA 同时需要 `torch.library` 三件套（让前向被编译）和 `dynamo.disable` 图断（让反向不被错误内联）」。

## 6. 本讲小结

- FFPA 用 `torch.library` 的三件套 `define` / `impl("CUDA")` / `register_fake` 把 Triton/CUDA kernel 暴露成 `torch.ops.ffpa_attn._fwd_*`，使 `torch.compile` 能把它当作合法节点，其中 `register_fake`（meta 实现）在 Dynamo trace 期负责推导输出形状/dtype。
- meta 实现必须与各自后端的真实现「逐行对应」（如 LSE 对齐粒度 Triton=128、CUDA=8），不能跨后端复用；schema 里的布尔/枚举开关统一编码成 `int`。
- FFPA **不用** `register_autograd`，因为一个前向后端可配多个反向后端（cuda→{triton,sdpa}；cutedsl→{cutedsl,triton,sdpa}），而 `register_autograd` 只能绑唯一反向，强绑会在 `fullgraph=True` 下**静默忽略**用户的 `backward_backend`。
- 反向分发保留在 `_FFPAAttnFunc.backward` 里，依据是运行时的 `meta.backward_meta`；反向算子（如 `_bwd_triton`）是被主动调用的工具，而非绑定到前向的 autograd 公式。
- 为避免 `torch.compile` 内联 `autograd.Function` 生成「零梯度模板反向」，FFPA 用 `@torch._dynamo.disable` 守卫的 `_ffpa_apply` 在 autograd 边界**主动图断**，前后向以急切模式运行；varlen 路径（`_ffpa_varlen_apply`）沿用同一手法。
- 对照：dense 路径「无 `register_autograd`、反向交给 `_FFPAAttnFunc`」；varlen 路径「`custom_op` + `register_autograd` 自管 autograd」——差异源于 varlen 只有一个反向公式。

## 7. 下一步学习建议

- 想看 `_fwd_triton` 的 `impl` 懒导入指向的真正前向 kernel，进入 [u4-l1 Triton 前向 kernel 与 online softmax 主循环](u4-l1-triton-fwd-online-softmax.md)，那里精读 `_ffpa_attn_forward_impl` 的 Split-D 主循环。
- 想理解反向算子 `_bwd_triton` 内部的 shared-pid 设计，进入 [u5-l2 dK/dV 与 dQ kernel：shared program-id 设计](u5-l2-dkdv-dq-shared-pid.md)。
- 想看「`custom_op` + `register_autograd` 自管 autograd」的完整范例，进入 [u6-l2 CuTeDSL 布局转换、校验与 varlen 接入](u6-l2-cutedsl-layout-varlen.md)，对照本讲 dense 路径加深理解。
- 若你对「在 E2E 训练里只替换大 D 全注意力层、其余回退 SDPA」的集成策略感兴趣，可跳到 [u9-l3 E2E 训练集成与日志可观测性](u9-l3-e2e-training-and-logging.md)。
