# 二次开发扩展指南：新增后端 / head_dim

## 1. 本讲目标

本讲是 FFPA 学习手册的「收官篇」。前面 8 个单元已经把「公共 API → 分发层 → 各后端 kernel → 构建系统 → 自动调优 → 测试」整条链路拆解完毕。本讲不再讲新机制，而是站在**维护者/二次开发者**的视角回答两个最实际的问题：

1. **我想新增一个后端（比如一个新的硬件路径或实验性 kernel）**，需要改动哪些文件、在哪些位置「接线」？
2. **我想新增一个 head_dim（比如 448）**，需要在代码生成、自动调优网格、测试形状、持久化配置里同步做什么？

学完本讲你应该能够：

- 画出「一个后端从字符串名到 kernel 执行」的完整接入清单，并能在 `functional.py` 与后端 `__init__.py` 里逐一定位接入点。
- 区分 FFPA 中**三套相互独立的 head_dim 列表**（CUDA 代码生成集、Triton 持久化调优集、测试覆盖集），并知道新增一个 head_dim 时每一层该改哪里。
- 理解扩展时保持 `torch.compile` 与 autograd 兼容的两条铁律（meta 实现要对齐、不要乱用 `register_autograd`）。

## 2. 前置知识

本讲假设你已经读过下面三篇（它们是本讲的依赖）：

- **u3-2 Backend 配置类体系**：`Backend` 基类用 `forward`/`backward` 两个 `bool|None` 字段表达「管不管某一段」、`__post_init__` 自动补全、四个子类的专有字段与硬约束。
- **u7-3 每个 head_dim 代码生成与 C++ pybind 分发**：`env.py` 的 per-headdim 代码生成器 `generate_split_headdim_sources`、C++ 三级分发链、pybind 绑定 `ffpa_attn._C`。
- **u8-2 持久化调优配置生成器 CLI**：`DEFAULT_HEADDIMS`/`DEFAULT_SEQLENS` 任务网格、entry schema、离线生成器落盘成设备 JSON。

如果某些术语你已经熟悉，可以跳过下面这段复述。需要回顾的关键术语：

- **后端（backend）**：一段具体的 kernel 实现路径。FFPA 有四个：`sdpa`（基线/回退）、`cuda`（手写前向）、`triton`（默认前向+反向）、`cutedsl`（Hopper 最快）。
- **分发（dispatch）**：运行时根据 `head_dim` 与用户选的 `Backend` 把一次调用路由到具体 kernel 的过程，集中在 `_FFPAAttnFunc.forward/backward`。
- **`torch.library` 三件套**：`define`（声明 schema）+ `impl("CUDA")`（真实现）+ `register_fake`（meta/形状推导实现），三者合起来让一个 kernel 成为 `torch.compile` 友好的合法算子。
- **`register_autograd`**：把一个前向算子和**唯一**一个反向公式绑定。FFPA 因「一前向多反向」基本不用它（varlen 是唯一例外）。
- **head_dim（D）**：注意力头维度。FFPA 的主战场是 D>256。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关心什么 |
| --- | --- | --- |
| [src/ffpa_attn/functional.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py) | 分发层：Backend 配置类、`FFPAAttnMeta`、`_FFPAAttnFunc` autograd Function | 新增后端的核心接线点 |
| [src/ffpa_attn/triton/__init__.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/__init__.py) | Triton 后端的 `torch.library` op 注册 | 「三件套」的标准范例 |
| [src/ffpa_attn/cuda/__init__.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cuda/__init__.py) | CUDA 后端的 op 注册 | 仅前向 op 的范例 |
| [src/ffpa_attn/cute/__init__.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py) | CuTeDSL 后端的 op 注册 | `custom_op`+`register_autograd` 的 varlen 范例 |
| [env.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py) | 构建期总指挥 | 新增 head_dim 的代码生成层 |
| [src/ffpa_attn/autotune.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py) | 持久化调优 CLI | autotune 任务网格 |
| [src/ffpa_attn/triton/_persistent_autotune.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py) | 持久化配置常量与运行时查找 | `DEFAULT_HEADDIMS`/`DEFAULT_SEQLENS` 的真正定义处 |
| [tests/test_ffpa_fwd.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_ffpa_fwd.py) / [tests/test_ffpa_bwd.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_ffpa_bwd.py) | 正确性测试 | 测试侧的 head_dim 覆盖列表 |

---

## 4. 核心概念与源码讲解

### 4.1 后端接入的「分发契约」：Backend 子类、_coerce_backend 与 _BACKEND_MAP

#### 4.1.1 概念说明

在 FFPA 里，「后端」不是一个抽象接口类（没有 `class Backend(ABC)`），而是由**四件相互配套的东西**共同组成的隐式契约：

1. 一个 `Backend` 子类（携带该后端的专有旋钮与硬约束）。
2. 一个注册到 `_BACKEND_MAP` 的字符串名（让 `backend="xxx"` 这种写法能解析）。
3. 一组 `isinstance(..., XxxBackend)` 分支（让 `_FFPAAttnFunc.forward/backward` 知道该调谁）。
4. 一个 `torch.library` 注册的 `ffpa_attn::_fwd_*` / `ffpa_attn::_bwd_*` 算子（让真 kernel 能被 `torch.compile` 看见）。

**这四者缺一不可**。少第 2 项，用户传字符串名会报「unknown backend」；少第 3 项，forward 会走到 `raise ValueError("Unsupported forward_backend=...")`；少第 4 项，`torch.compile` 会图中断或报「op not found」。本模块先把前三件讲清楚（第 4 件放到 4.2）。

为什么用「字符串名 + isinstance 分发」而不用多态方法（比如 `backend.forward(...)`）？因为反向是「运行时才决定的」——同一个前向后端可配多个反向后端（见 u3-l5），把 forward/backward 写成数据流分支比写成虚函数更利于表达这种「一对多」关系。

#### 4.1.2 核心流程

新增后端时，分发层这条链路要按下面顺序打通：

```
用户传 backend="mybe" 或 forward_backend=MyBackend(...)
        │
        ▼
FFPAAttnMeta.from_kwargs  ──► _coerce_backend(source=...)  ──► _BACKEND_MAP["mybe"] = MyBackend
        │                          （字符串→实例；实例原样返回）
        ▼
_resolve_backend_pair  （校验 forward/backward 对称性、None 补全）
        │
        ▼
_FFPAAttnFunc.forward:  if isinstance(meta.forward_meta, MyBackend): → 调你的 op
_FFPAAttnFunc.backward: if isinstance(meta.backward_meta, MyBackend): → 调你的 op
```

关键设计：`_coerce_backend` 会根据调用来源 `source` 生成不同的 `forward/backward` 标志——

- `source="backend"`：用户用了 `backend=` 简写，两段都归它，故 `cls_name()`（两段都默认 True）。
- `source` 以 `"forward"` 开头：`cls_name(forward=True, backward=False)`。
- `source` 以 `"backward"` 开头：`cls_name(forward=False, backward=True)`。

这就是「同一个字符串名，在不同位置解析出不同标志」的实现，也是前向/反向可独立解耦的根。

#### 4.1.3 源码精读

**Backend 基类与 `__post_init__` 自动补全**——`None` 表示「未声明」，由构造期补成 bool：

[`src/ffpa_attn/functional.py:108-129`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L108-L129) 定义 `Backend(name, forward=None, backward=None)`，并在 `__post_init__` 里：两者都 `None` 则都置 `True`；只有一个是 `None` 则取另一个的非。新增子类**必须**在 `__post_init__` 里先调 `super().__post_init__()`，否则补全逻辑会跳过。

**`_coerce_backend` 与它内部的 `_BACKEND_MAP`**——字符串名解析的唯一入口：

[`src/ffpa_attn/functional.py:284-305`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L284-L305) 注意 `_BACKEND_MAP` 是**函数内的局部变量**（不是模块常量），所以「注册一个新后端名」就是在这一个字典里加一行：

```python
_BACKEND_MAP = {
  "cuda": CUDABackend,
  "triton": TritonBackend,
  "cutedsl": CuTeDSLBackend,
  "sdpa": SDPABackend,
}
```

新增后端 `"mybe"`：在这里加 `"mybe": MyBackend`，并实现 `MyBackend` 子类。

**一个真实子类的样子——`CUDABackend`**：[`src/ffpa_attn/functional.py:150-171`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L150-L171)。它演示了新增子类的标准套路：

- `name: str = "cuda"`（默认名）；
- 专有旋钮 `acc`、`stages`；
- `__post_init__` 里用 `assert` 强制硬约束（`assert not self.backward, "cuda backend does not support backward"`）；
- 一个把旋钮编码成 int 的 `@property`（`acc_code`，供 op 边界用）。

**对称性校验——`_resolve_backend_pair`**：[`src/ffpa_attn/functional.py:253-281`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L253-L281)。如果你的新后端**前后向必须成对**（像 cutedsl 那样），就在这里加一段 `if forward_backend.name == "mybe" and backward_backend.name != "mybe": raise ValueError(...)`。

**`from_kwargs` 的单向自动补全**：[`src/ffpa_attn/functional.py:439-442`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L439-L442)。cutedsl 当前若只显式给了前向，会自动补一个后向（反之亦然）。如果你的后端也是前后对称，可照此添加。

#### 4.1.4 代码实践

**实践目标**：在不写任何 kernel 的前提下，验证「漏掉 `_BACKEND_MAP` 这一项」会被分发层拦下来。

**操作步骤**（纯源码阅读型实践，不修改源码）：

1. 打开 `functional.py`，确认 `_BACKEND_MAP`（L286-L291）目前只有四个键。
2. 在本地 Python 里模拟「传一个未注册的名字」：

```python
# 示例代码：仅用于复现错误路径，不是项目原有代码
from ffpa_attn.functional import _coerce_backend
try:
    _coerce_backend("mybe", source="backend")
except ValueError as e:
    print("caught:", e)
```

**需要观察的现象**：应抛出 `ValueError: ffpa_attn_func: backend must be 'cuda', 'triton', 'cutedsl', or 'sdpa', got 'mybe'`。

**预期结果**：这证明 `_BACKEND_MAP` 是后端名的唯一注册点；新增后端必须在此加一行，否则连字符串解析都过不了。**待本地验证**（取决于你的环境能否 import ffpa_attn）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_BACKEND_MAP` 写成 `_coerce_backend` 的局部变量，而不是模块级常量？

> **答案**：它只在字符串→类解析这一个地方被用到，且其键与 `source` 语义紧耦合。写成局部变量缩小了作用域，避免被其他模块误用；同时把「名字注册」和「名字解析」物理上放在同一个函数里，新增后端时只改一处即可。

**练习 2**：假如新后端 `MyBackend` 不支持反向，应该在子类的哪里强制？

> **答案**：在 `__post_init__` 里 `assert not self.backward, "mybe backend does not support backward"`，与 `CUDABackend`（L164）完全一致。这样用户一旦误传 `backward_backend="mybe"`，会在构造期立即 fail-fast，而不是等到 backward 时才崩。

**练习 3**：`_coerce_backend("triton", source="forward_backend")` 与 `_coerce_backend("triton", source="backend")` 返回的对象，`forward`/`backward` 字段分别是什么？

> **答案**：前者 `TritonBackend(forward=True, backward=False)`；后者 `TritonBackend()` 即 `forward=True, backward=True`（两段都归它）。依据见 L297-L300。

---

### 4.2 从 op 注册到 autograd：torch.library 三件套与 _FFPAAttnFunc 分发

#### 4.2.1 概念说明

`_BACKEND_MAP` 让用户**能选**你的后端，但要让 kernel**真的被调用且能被 torch.compile 追踪**，还差两步：

1. 把真 kernel 注册成一个 `torch.ops.ffpa_attn._fwd_*` 算子（三件套）。
2. 在 `_FFPAAttnFunc.forward/backward` 里加 `isinstance` 分支去调它。

**三件套各自的角色**（承接 u3-l5）：

- `torch.library.define`：声明算子的 schema（参数/返回的 Tensor 类型）。Dynamo 据此判断它是合法节点。
- `@torch.library.impl("CUDA")`：真实现，通常懒导入昂贵的 kernel 模块。
- `@torch.library.register_fake`：**meta 实现**，只根据输入形状/dtype 推导输出形状，不跑真 kernel。`torch.compile` 在 trace 期调用它。

> ⚠️ meta 实现必须与真实现**逐行对齐**（输出形状/dtype/LSE 对齐粒度都要一致），否则 `torch.compile` 推导出的图与实际执行不符。

**为什么不用 `register_autograd` 绑定反向？** 因为 FFPA 是「一前向多反向」（见下一小节的源码注释）：cuda 前向可配 triton 或 sdpa 反向，cutedsl 前向可配 cutedsl/triton/sdpa 反向。`register_autograd` 只能绑**唯一**反向，强绑一个会在 `fullgraph=True` 下静默忽略用户的 `backward_backend`。所以反向分发留在 `_FFPAAttnFunc.backward` 里按运行时 `meta.backward_meta` 决定。（唯一例外是 varlen，它只有单一反向公式，用 `custom_op`+`register_autograd` 自管 autograd——见 4.2.3 的 cutedsl varlen。）

#### 4.2.2 核心流程

一个前向算子从注册到执行的链路：

```
后端 __init__.py:
   define("ffpa_attn::_fwd_mybe", schema)
   @impl("CUDA")        →  分配输出、调真 kernel、返回 (o, lse)
   @register_fake       →  用 FakeTensor 推导同样的 (o, lse) 形状
                                  │
functional.py 顶部:  try: from .mybe import _ffpa_attn_forward_mybe  (except → None)
                                  │
_FFPAAttnFunc.forward:
   elif isinstance(meta.forward_meta, MyBackend):
       O, lse = _ffpa_attn_forward_mybe(q, k, v, O, ...)   ← 你的 op
                                  │
_FFPAAttnFunc.backward:
   elif isinstance(meta.backward_meta, MyBackend):
       dq, dk, dv = _ffpa_attn_backward_mybe(...)          ← 你的反向（若有）
```

注意：`functional.py` 顶部的导入是**可选后端的标准模式**——核心后端（triton/aten）直接 import，可选后端（cuda/cute）用 `try/except` 包裹、失败置 `None`。你的新后端若依赖重型编译产物，也应走这个模式。

#### 4.2.3 源码精读

**三件套的标准范例——Triton 前向 op**：

[`src/ffpa_attn/triton/__init__.py:266-272`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/__init__.py#L266-L272) 是 `define`，schema 里把所有「控制位」（causal/autotune/enable_tma…）都编码成 `int`/`float` 标量——这是跨 op 边界的惯例，布尔/枚举一律编码成 int。

[`src/ffpa_attn/triton/__init__.py:275-327`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/__init__.py#L275-L327) 是 `@impl("CUDA")` 真实现：分配 `o` 与 `softmax_lse`（注意 LSE 的对齐粒度是 **128**：`seqlen_q_aligned = ((seqlen_q + 127) // 128) * 128`），再懒导入并调用 `_ffpa_attn_forward_impl`。

[`src/ffpa_attn/triton/__init__.py:330-351`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/__init__.py#L330-L351) 是 `@register_fake`：用**同样的** 128 对齐公式推导 `o`/`lse` 形状，不跑 kernel。

> 对照 CUDA 后端：[`src/ffpa_attn/cuda/__init__.py:23-101`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cuda/__init__.py#L23-L101) 是仅前向 op 的范例，但它的 LSE 对齐粒度是 **8**（`((seqlen_q + 7) // 8) * 8`）。**这就是为什么 meta 实现不能跨后端复用**——不同后端的 LSE 对齐粒度不同。

**前向分发分支——`_FFPAAttnFunc.forward`**：

[`src/ffpa_attn/functional.py:764-829`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L764-L829) 是前向的总分发。结构是：先用 `_should_use_aten_small_d_forward` 判小 D（D≤256 走 aten），再 `elif isinstance(meta.forward_meta, CUDABackend/TritonBackend/CuTeDSLBackend)` 依次匹配，最后兜底 `raise ValueError("Unsupported forward_backend=...")`。**新增后端就是在这条 `elif` 链里插一个分支**。

**反向分发分支——`_FFPAAttnFunc.backward`**：

[`src/ffpa_attn/functional.py:862-923`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L862-L923) 关键不对称：**大小 D 看前向 meta、大 D 后端看反向 meta**。所以新增后端的反向分支挂在 `if not use_aten_small_d_forward:` 块内的 `isinstance(meta.backward_meta, ...)` 链里。

**「为何不用 register_autograd」的权威注释**：

[`src/ffpa_attn/functional.py:946-968`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L946-L968) 用一张表写明了前向↔反向的多对多关系，并解释了用 `_ffpa_apply`（带 `@torch._dynamo.disable`）在 autograd 边界主动图断、让真 `_FFPAAttnFunc.backward` eager 执行的设计。新增后端时这段注释里的表也要更新。

**varlen 的另一种范式——`custom_op`+`register_autograd`**：

[`src/ffpa_attn/cute/__init__.py:708-723`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L708-L723) 用 `torch.library.custom_op` 注册 `_varlen_fwd_cute`，再在 [`src/ffpa_attn/cute/__init__.py:940-944`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L940-L944) 用 `register_autograd` 绑定唯一反向。**仅当你的新后端只有单一反向公式时**才能用这套，否则必须像 dense 路径那样把反向留在 `_FFPAAttnFunc.backward`。

#### 4.2.4 代码实践

**实践目标**：读懂「一前向多反向」为什么排除了 `register_autograd`，并验证一次 `torch.compile` 调用不报错。

**操作步骤**：

1. 阅读 [`functional.py:946-965`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L946-L965) 那段注释里的「forward_backend × backward_backend」表，数一下 cutedsl 前向可以配几种反向。
2. 在本地（需有支持的 GPU 与 ffpa_attn 安装）跑一段最小 compile 用例：

```python
# 示例代码：验证 torch.compile 能穿过 ffpa_attn_func
import torch
from ffpa_attn import ffpa_attn_func

q = torch.randn(1, 32, 8192, 512, dtype=torch.bfloat16, device="cuda", requires_grad=True)
k = torch.randn(1, 32, 8192, 512, dtype=torch.bfloat16, device="cuda", requires_grad=True)
v = torch.randn(1, 32, 8192, 512, dtype=torch.bfloat16, device="cuda", requires_grad=True)

@torch.compile(fullgraph=True)
def f(q, k, v):
    return ffpa_attn_func(q, k, v).float().sum()

out = f(q, k, v); out.backward()
print("compiled fwd+bwd OK")
```

**需要观察的现象**：`fullgraph=True` 不抛图断错误，且 `.backward()` 得到非零梯度。这正是因为 `_ffpa_apply` 用 `@torch._dynamo.disable` 主动断图、把真反向保留在 eager。

**预期结果**：打印 `compiled fwd+bwd OK`，且 `q.grad.abs().sum() > 0`。**待本地验证**（本机若无 H200/A100 等大显存卡，可把 N=8192 调小到 2048、D 调到 320 重试）。

#### 4.2.5 小练习与答案

**练习 1**：Triton 前向 op 的 LSE 对齐粒度是 128，CUDA 前向是 8。如果你复制 Triton 的 meta 实现给一个 LSE 对齐粒度为 8 的新后端，会发生什么？

> **答案**：`torch.compile` 在 trace 期按 128 对齐推导出比实际更大的 `lse` 形状，运行时真实现却按 8 对齐分配——形状不匹配，轻则下游算子报维度错，重则静默读越界。结论：**meta 实现必须与该后端真实现的 LSE 对齐公式逐字一致**，不能跨后端复用。

**练习 2**：新增后端若想支持反向，分支应该加在 `_FFPAAttnFunc.backward` 的哪个 `if` 块里？为什么不能加在最外层？

> **答案**：加在 `if not use_aten_small_d_forward:` 块内的 `isinstance(meta.backward_meta, MyBackend)` 分支（参考 L862-L923）。最外层会被「小 D 走 aten flash 反向」的 `else` 分支截走（L924-L940），大 D 后端的反向只有进 `if not use_aten_small_d_forward:` 才会被路由到。

**练习 3**：dense 路径用 `_FFPAAttnFunc` 管反向、varlen 路径用 `register_autograd`。判断「我的新后端该用哪种」的依据是什么？

> **答案**：看反向公式是否唯一。若一个前向可能配多个反向（如 triton 前向可配 triton/sdpa 反向），就必须留在 `_FFPAAttnFunc.backward` 按运行时 meta 分发，**不能**用 `register_autograd`（它只能绑唯一反向，会在 `fullgraph=True` 下静默忽略 `backward_backend`）。只有像 varlen 那样「前向↔反向一一对应」时才用 `custom_op`+`register_autograd` 自管 autograd。

---

### 4.3 新增 head_dim 之一：手写 CUDA 的代码生成层

#### 4.3.1 概念说明

FFPA 里「head_dim」不是一个全局常量，而是**三套相互独立的列表**——这是本讲最容易踩坑的地方：

| 列表 | 定义处 | 默认值 | 控制什么 |
| --- | --- | --- | --- |
| CUDA 构建集 | `env.py::get_enabled_headdims` | `range(256, 1025, 64)` | 编译哪些 head_dim 的 `.cu` 翻译单元 |
| Triton 调优集 | `_persistent_autotune.py::DEFAULT_HEADDIMS` | `[320, 512, 640, 768, 1024]` | 离线给哪些 head_dim 调优并落盘 config |
| 测试覆盖集 | `tests/test_ffpa_{fwd,bwd}.py::HEADDIMS` | fwd `[64,128,320,512,640]` / bwd `[64,320,512]` | 正确性回归覆盖哪些 head_dim |

**核心洞察**：这三套列表互相**不同步**。以 `448` 为例：

- \(448 = 256 + 3\times 64\)，所以 \(448 \in \{256,320,384,448,\dots,1024\}\) —— **448 已在 CUDA 默认构建集里**（仓库里已能找到 `csrc/cuffpa/generated/ffpa_attn_fwd_fp16_hdim448.cu`）。
- 但 \(448 \notin \{320,512,640,768,1024\}\) —— **448 不在 Triton 调优集里**，运行时拿不到为它专门调过的 config。
- 测试侧也没有 448。

所以「新增 head_dim=448」对不同子系统意味着完全不同的工作量：对 CUDA 代码生成是「什么都不用做」（已自动生成），对 Triton 调优是「加进 `DEFAULT_HEADDIMS` 并重跑 CLI」，对测试是「加进测试 `HEADDIMS`」。**先搞清楚你要扩的是哪一层，再动手。**

> 本讲的练习任务之所以选 448，正是为了暴露这种「同名不同命」的差异。若你真的想新增一个「三层都没有」的全新维度，挑 480（\(480 = 256+3\times64+32\)，仅在 `ENABLE_FFPA_ALL_HEADDIM` 的 `range(32,1025,32)` 里出现）会更典型。

#### 4.3.2 核心流程

手写 CUDA 后端新增/启用一个 head_dim 的流程（关键是**几乎全自动**）：

```
env.py::get_enabled_headdims()
   优先级：FFPA_DEV_HEADDIMS > ENABLE_FFPA_ALL_HEADDIM > 默认 range(256,1025,64)
            │
            ▼
generate_split_headdims(headdims)
   ├─ _render_decls_header(headdims)   → ffpa_attn_fwd_decls.h（每个 D 三个声明）
   ├─ for d in headdims:
   │     _render_per_headdim_fp16_tu(d) → ffpa_attn_fwd_fp16_hdim{d}.cu（fp16f16+fp16f32 两个符号）
   │     _render_per_headdim_bf16_tu(d) → ffpa_attn_fwd_bf16_hdim{d}.cu（bf16f32 一个符号）
   └─ _render_dispatch_tu(headdims)    → ffpa_attn_fwd_dispatch.cu（按 d 的 switch）
            │   （_write_if_changed：内容没变就不动 mtime，保持增量编译）
            ▼
get_build_sources() 把生成的 .cu 当作真正的编译源
            │
            ▼
nvcc 并行编译（每个 head_dim 两个 TU，可被 MAX_JOBS 并行驱动）
            │
            ▼
运行时 ffpa_attn_fwd_dispatch.cu 里的 switch(d) 选中 ffpa_attn_fwd_*_d{d}
```

每个 head_dim 会生成 **2 个 `.cu` 翻译单元**（fp16 一个、bf16 一个）和 **3 个符号声明**（fp16f16、fp16f32、bf16f32）。bf16 只有 1 个符号（bf16f32），因为 GPU 没有 bf16 累加的 MMA PTX，bf16 必须用 fp32 累加——这条约束在代码生成层就锁死了（见 `_render_per_headdim_bf16_tu`）。

#### 4.3.3 源码精读

**head_dim 集合的三级优先级**——`get_enabled_headdims`：

[`env.py:389-416`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L389-L416) 优先级是 `FFPA_DEV_HEADDIMS`（开发期子集）> `ENABLE_FFPA_ALL_HEADDIM`（`range(32,1025,32)`）> 默认 `range(256,1025,64)`。所以「只编译 448 一个维度」的做法是 `FFPA_DEV_HEADDIMS="448"`，而**不需要改源码**。

**代码生成主入口**——`generate_split_headdim_sources`：

[`env.py:440-511`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L440-L511) 遍历 `headdims`，为每个 `d` 写两个 `.cu`。注意 L489-L502 的 stale-file 清理：旧布局的生成文件会被自动删除，避免残留误导。`fwd_generated_count = len(headdims) * 2 + 1`（每个 D 两 TU + 一个 dispatch）。

**单个 fp16 TU 的渲染**——`_render_per_headdim_fp16_tu`：

[`env.py:582-626`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L582-L626) 对每个 `d` 渲染两个入口符号 `ffpa_attn_fwd_fp16f16_d{d}` 与 `ffpa_attn_fwd_fp16f32_d{d}`，区别仅在累加器精度常量 `kMmaAccFloat32QK/PV`。

**bf16 TU 的渲染**——`_render_per_headdim_bf16_tu`：

[`env.py:628-652`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L628-L652) 只渲染 `ffpa_attn_fwd_bf16f32_d{d}` 一个符号，且把 `kMmaAccFloat32QK=1; kMmaAccFloat32PV=1` 写死——这就是「bf16 必须 fp32 累加」在代码生成层的体现。

**dispatch TU**——`_render_dispatch_tu`：

[`env.py:684-732`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L684-L732) 生成一个 `switch(d)`，把 `ffpa_attn_fwd_fp16f16`/`fp16f32`/`bf16f32` 三个统一入口按 `Q.size(3)` 分发到对应 `_d{d}` 符号；未覆盖的维度抛 `"headdim not support!"`。

**把生成文件接入编译**——`get_build_sources`：

[`env.py:734-758`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L734-L758) 把 `ffpa_attn_api.cc`（pybind 入口）与生成的 `.cu` 拼成最终编译源列表。这意味着**只要某个 `d` 进了 `get_enabled_headdims()`，它的 TU 就会自动被编译、自动被 dispatch 选中**——无需手写任何 C++。

#### 4.3.4 代码实践

**实践目标**：确认 448 在 CUDA 代码生成层「已自动支持」，并体验「只编译一个维度」的快速构建。

**操作步骤**：

1. 在仓库里确认 448 的生成文件已存在（无需运行任何命令，用目录浏览即可）：

```
csrc/cuffpa/generated/ffpa_attn_fwd_fp16_hdim448.cu
csrc/cuffpa/generated/ffpa_attn_fwd_bf16_hdim448.cu
```

2. 阅读上面两个文件，确认它们分别声明了 `ffpa_attn_fwd_fp16f16_d448`、`ffpa_attn_fwd_fp16f32_d448`、`ffpa_attn_fwd_bf16f32_d448` 三个符号。
3. （可选，需本地 CUDA 工具链）用开发期子集做一次最小构建，验证代码生成对任意维度通用：

```bash
# 示例命令：仅编译 head_dim=480（默认集里没有的维度），stages 只编 1-2
FFPA_DEV_HEADDIMS="480" ENABLE_FFPA_ALL_STAGES=0 \
ENABLE_FFPA_CUDA_IMPL=1 pip install -e . --no-build-isolation
```

**需要观察的现象**：步骤 1 能找到 `hdim448` 文件（因为 448 ∈ 默认 `range(256,1025,64)`）；步骤 3（若运行）会在 `generated/` 下出现 `ffpa_attn_fwd_*_hdim480.cu`，证明只要把维度喂给 `get_enabled_headdims`，代码生成会自动产出 TU。

**预期结果**：448 的代码生成「零改动」即可用；任意新维度（如 480）只要进入 `get_enabled_headdims`（通过 `FFPA_DEV_HEADDIMS` 或开启 `ENABLE_FFPA_ALL_HEADDIM`）就会被自动生成与编译。**步骤 3 待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`get_enabled_headdims()` 默认返回什么？`ENABLE_FFPA_ALL_HEADDIM=1` 后返回什么？

> **答案**：默认 `list(range(256, 1025, 64))` = `[256,320,384,448,512,576,640,704,768,832,896,960,1024]`；开启 ALL_HEADDIM 后 `list(range(32, 1025, 32))`（多出 32 的整数倍维度，如 288、480、544…）。依据见 `env.py:414-416`。

**练习 2**：为什么每个 head_dim 生成 2 个 `.cu` 文件、却对应 3 个符号？bf16 为什么少一个？

> **答案**：fp16 文件含 2 个符号（fp16f16、fp16f32，对应 fp16 累加与 fp32 累加两种精度），bf16 文件含 1 个符号（bf16f32），合计 3 个。bf16 没有 bf16f16 变体，因为 GPU 不存在 bf16 累加的 MMA PTX 指令，bf16 必须用 fp32 累加——这条约束在 `_render_per_headdim_bf16_tu`（`env.py:628-652`）里通过写死 `kMmaAccFloat32QK=1; kMmaAccFloat32PV=1` 锁死。

**练习 3**：`_write_if_changed`（`env.py:422-438`）的作用是什么？为什么对增量编译很重要？

> **答案**：仅当文件内容真的变化时才写盘，否则保持原 mtime。这样当生成内容未变时，`setuptools`/`ninja` 能跳过对应 TU 的重编译，让「每次构建都跑一遍生成器」在稳态下近乎零开销。这对 CI 反复构建很关键。

---

### 4.4 新增 head_dim 之二：Triton 持久化调优网格与测试形状

#### 4.4.1 概念说明

CUDA 后端的 head_dim 是「构建期」的事（编出 TU 即可），而 Triton 后端的 head_dim 是「调优期」的事——Triton kernel 本身是 JIT 的、对任意 head_dim 都能跑，但**性能依赖于为该 head_dim 调过的 launch config**。如果某个 head_dim 没进调优网格，运行时 `autotune=False` 就只能用写死的默认 config（可能很慢）或就近匹配一个相邻维度的 config（次优）。

所以新增 head_dim 的「第二层」是：

1. 把它加进 `DEFAULT_HEADDIMS`（控制离线调优覆盖哪些维度）。
2. 重跑 `python -m ffpa_attn.autotune` 生成含新维度的设备 JSON。
3. 把它加进测试的 `HEADDIMS`，保证回归覆盖。

任务网格的笛卡尔积规模需要心里有数。前向任务数大致是：

\[
|\text{tasks}_{\text{fwd}}| \approx |\text{dtypes}|\times|\text{HEADDIMS}|\times 2_{\text{causal}}\times(|\text{prefill}|^2 + |\text{prefill}|\times|\text{decode}|)
\]

其中 `prefill` 是 `DEFAULT_SEQLENS` 里 ≥512 的子集。每多一个 head_dim，就会让任务数按上式增长一档，所以 `DEFAULT_HEADDIMS` 故意只保留 5 个「高频大 D」维度，避免调优爆炸。

#### 4.4.2 核心流程

```
_persistent_autotune.py:  DEFAULT_HEADDIMS = [320,512,640,768,1024]   ← 在这里加 448
                                       │
autotune.py: _iter_forward_tasks / _iter_backward_tasks
   for headdim in DEFAULT_HEADDIMS:     ← 任务网格自动覆盖新维度
       for causal in (False, True):
           for seqlen_q in prefill_seqlens:
               for seqlen_k in prefill_seqlens: ...
                                       │
python -m ffpa_attn.autotune --mode fast → 设备 JSON 里多了 headdim=448 的 entry
                                       │
运行时 _persistent_autotune.lookup_persistent_config
   按 direction/kernel/causal/dtype/has_attn_bias/has_dropout 过滤
   再用 nearest_value 就近匹配 head_dim（448 会命中自己，或回退到 512/320）
```

测试侧则是把维度加进 `tests/test_ffpa_fwd.py` 与 `tests/test_ffpa_bwd.py` 的 `HEADDIMS` 列表，让 `DISPATCH_SHAPES`（`HEADNUMS × HEADDIMS` 笛卡尔积）自动覆盖。

#### 4.4.3 源码精读

**调优网格常量的真正定义处**：

[`src/ffpa_attn/triton/_persistent_autotune.py:29-30`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L29-L30) 定义 `DEFAULT_HEADDIMS = [320, 512, 640, 768, 1024]` 与 `DEFAULT_SEQLENS = [1, 512, 1024, 2048, 4096, 8192, 16384]`。**新增 head_dim 到调优网格就是改这一行**——加 448 即 `DEFAULT_HEADDIMS = [320, 448, 512, 640, 768, 1024]`。

**前向任务网格**——`_iter_forward_tasks`：

[`src/ffpa_attn/autotune.py:237-289`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L237-L289) 四层循环：`dtype × headdim(in DEFAULT_HEADDIMS) × causal × (seqlen_q × seqlen_k)`，再加 decode（`seqlen_q=1`）任务。注意 L251-L252 会跳过 `causal and seqlen_k < seqlen_q`（因果要求 Nkv≥Nq，见 u2-l3）。改了 `DEFAULT_HEADDIMS`，这里的笛卡尔积会自动扩展。

**payload 里的 tune_grid**：

[`src/ffpa_attn/autotune.py:869-872`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/autotune.py#L869-L872) 把 `DEFAULT_HEADDIMS` 写进 JSON 的 `tune_grid.headdims` 字段，供运行时与下游工具核对覆盖范围。

**运行时就近匹配的兜底**：即便你没把 448 加进 `DEFAULT_HEADDIMS`，运行时也能通过 `nearest_value`（最近、并列取大）匹配到 320 或 512 的 config——这就是为什么 448「不开调优也能跑，只是未必最优」。详见 u8-3。

**测试侧的 head_dim 列表**：

[`tests/test_ffpa_fwd.py:25-26`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_ffpa_fwd.py#L25-L26) `HEADDIMS = [64, 128, 320, 512, 640]`，被 `DISPATCH_SHAPES`（L44-L45）展开成 `HEADNUMS × HEADDIMS` 笛卡尔积做「能跑通」冒烟。

[`tests/test_ffpa_bwd.py:25`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_ffpa_bwd.py#L25) 反向 `HEADDIMS = [64, 320, 512]`（更精简，因反向更慢）。**新增 head_dim 到测试覆盖就是改这两个列表**——加 448 即各加一项。

#### 4.4.4 代码实践

**实践目标**：动手把 448 接入「调优网格 + 测试覆盖」，并理解为何这两步对 CUDA 代码生成（4.3）是多余的。

**操作步骤**：

1. **调优网格**：把 [`_persistent_autotune.py:29`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_persistent_autotune.py#L29) 改为 `DEFAULT_HEADDIMS = [320, 448, 512, 640, 768, 1024]`（仅作为练习理解；本任务不要求你真的改源码）。
2. **重跑 CLI**（需支持的 GPU）：

```bash
# 示例命令：为新维度生成持久化 config
FFPA_AUTOTUNE_MAX_CONFIGS=4 python -m ffpa_attn.autotune \
  --mode fast --directions forward --dtypes bf16 --B 1 --H 32 --overwrite
```

3. **测试覆盖**：把 448 加进 `tests/test_ffpa_fwd.py` 的 `HEADDIMS` 与 `tests/test_ffpa_bwd.py` 的 `HEADDIMS`，然后跑一个聚焦子集：

```bash
# 示例命令：只跑 448 相关的 dispatch 冒烟
pytest tests/test_ffpa_fwd.py -k '512 or 448' -q
```

**需要观察的现象**：步骤 2 生成的设备 JSON 里应出现 `"headdim": 448` 的 entry（kernel 可能是 `fwd_generic` 或 `decode_fwd_stage1`）；步骤 3 应能看到 448 的 dispatch 用例被执行并通过 finite/shape 校验。

**预期结果**：448 在 Triton 侧从「靠就近匹配 320/512」升级为「有专属调优 config」；测试侧从「未覆盖」升级为「冒烟覆盖」。注意 CUDA 代码生成层（4.3）对 448 **无需任何改动**，因为它早已在默认构建集里。**步骤 2、3 待本地验证**（需 GPU 与较长调优时间）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `DEFAULT_HEADDIMS` 只有 5 个维度，而 CUDA 默认构建集有 13 个？

> **答案**：构建集只决定「编不编 TU」，成本是编译时间（可并行、一次性）；调优集决定「为每个维度×seqlen×causal 笛卡尔积跑多少次 benchmark」，成本是**运行时**，且随维度数线性（甚至平方，因 seqlen 两两组合）增长。调优太贵，故只挑高频大 D（320/512/640/768/1024）放进默认网格，其余维度靠运行时 `nearest_value` 就近匹配。依据见 `_iter_forward_tasks`（`autotune.py:237-289`）的四层循环。

**练习 2**：如果把 448 加进 `DEFAULT_HEADDIMS` 但忘了重跑 `python -m ffpa_attn.autotune`，运行时会怎样？

> **答案**：设备 JSON 里没有 448 的 entry，运行时 `lookup_persistent_config` 查不到精确匹配，会经 `nearest_value` 就近匹配到 320 或 512（并列取大，故倾向 512）的 config，或回退到写死的默认 config。结果：能跑、正确，但**未必最优**。这是「无静默回退」原则的例外——调优缺失不会报错，只会次优。

**练习 3**：测试侧 fwd 的 `HEADDIMS` 包含 64、128（小 D），但 `DEFAULT_HEADDIMS` 不含它们。为什么测试要覆盖小 D？

> **答案**：小 D（D≤256）会走 `fallback()` 回退 SDPA（见 u3-l3），测试必须同时覆盖「FFPA kernel 路径」与「SDPA 回退路径」才算完整（见 u9-1）。而调优网格只关心 FFPA 自己的 kernel 性能，小 D 走 SDPA、无需 FFPA 调优，故不在 `DEFAULT_HEADDIMS` 里。两套列表服务不同目的。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这份**扩展清单**（本讲的核心实践任务）。请针对两种扩展场景，逐项列出要改的文件与具体位置：

### 场景 A：新增一个 head_dim（以 448 为例）

填完下表（答案见后，但建议先自己写）：

| 子系统 | 是否需改动 | 改哪个文件 / 哪一行 | 原因 |
| --- | --- | --- | --- |
| CUDA 代码生成 | ？ | ？ | ？ |
| Triton 持久化调优 | ？ | ？ | ？ |
| 测试覆盖（fwd） | ？ | ？ | ？ |
| 测试覆盖（bwd） | ？ | ？ | ？ |
| 运行时回退判定 | ？ | ？ | ？ |

**参考答案**：

| 子系统 | 是否需改动 | 改哪里 | 原因 |
| --- | --- | --- | --- |
| CUDA 代码生成 | **否**（448 已在默认集） | — | \(448\in\text{range}(256,1025,64)\)，`generate_split_headdim_sources` 已自动产出 `hdim448.cu`。若想让它成为**唯一**编译维度，用 `FFPA_DEV_HEADDIMS="448"`，仍不改源码 |
| Triton 持久化调优 | **是** | `_persistent_autotune.py:29` 的 `DEFAULT_HEADDIMS` 加 `448`，再重跑 `python -m ffpa_attn.autotune` | \(448\notin[320,512,640,768,1024]\)，否则运行时只能就近匹配 320/512 |
| 测试覆盖（fwd） | **是** | `tests/test_ffpa_fwd.py:25` 的 `HEADDIMS` 加 `448` | 让 `DISPATCH_SHAPES`（`HEADNUMS×HEADDIMS`）冒烟覆盖该维度 |
| 测试覆盖（bwd） | **是** | `tests/test_ffpa_bwd.py:25` 的 `HEADDIMS` 加 `448` | 反向正确性回归 |
| 运行时回退判定 | **否** | — | `fallback()`（`functional.py:474-522`）按 `D≤256`/`D>1024`/`Nq`/`Nkv` 判定，与具体维度值无关；448 既不≤256 也不>1024，不会被回退 |

> 进阶思考：若新增的是 **480**（三层都没有），则 CUDA 代码生成层也要动——要么开 `ENABLE_FFPA_ALL_HEADDIM=1`，要么 `FFPA_DEV_HEADDIMS="480"`。这就是「同名不同命」的完整体现。

### 场景 B：新增一个后端（假设叫 `mybe`）

按调用顺序列出 `functional.py` 与后端 `__init__.py` 的接入点：

**在 `functional.py` 里（共 5 处）：**

1. **顶部导入**（参考 L27-L41 的 try/except 模式）：`try: from .mybe import _ffpa_attn_forward_mybe, _ffpa_attn_backward_mybe except Exception: ... = None`。
2. **Backend 子类**（参考 `CUDABackend` L150-L171）：定义 `MyBackend(name="mybe", ...)`，在 `__post_init__` 里 `super().__post_init__()` 并用 `assert` 强制硬约束（如不支持反向就 `assert not self.backward`）。
3. **`_BACKEND_MAP`**（L286-L291）：加 `"mybe": MyBackend`。
4. **`_FFPAAttnFunc.forward` 分支**（在 L774-L829 的 `elif isinstance(...)` 链里）：插一个 `elif isinstance(meta.forward_meta, MyBackend): O, lse = _ffpa_attn_forward_mybe(...)`。
5. **`_FFPAAttnFunc.backward` 分支**（在 L862-L923 的 `isinstance(meta.backward_meta, ...)` 链里）：插反向分支（若支持）。若前后必须对称，还要在 `_resolve_backend_pair`（L272-L279）加校验；若要像 cutedsl 那样单向自动补全，在 `from_kwargs`（L439-L442）加一段。

**在后端 `__init__.py` 里（共 3 件套）：**

参考 [`triton/__init__.py:266-351`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/__init__.py#L266-L351)：

1. `torch.library.define("ffpa_attn::_fwd_mybe", schema)`——控制位编码成 int。
2. `@torch.library.impl("ffpa_attn::_fwd_mybe", "CUDA")`——分配输出、懒导入并调真 kernel，返回 `(o, lse)`。
3. `@torch.library.register_fake("ffpa_attn::_fwd_mybe")`——用**与真实现完全一致**的 LSE 对齐公式推导形状。

**两条铁律**（保持 torch.compile/autograd 兼容）：

- **铁律一**：meta 实现（`register_fake`）的输出形状/dtype/LSE 对齐粒度必须与真实现逐字一致，**不可跨后端复用**（Triton 是 128、CUDA 是 8）。
- **铁律二**：若一个前向可配多个反向，**不要**用 `register_autograd` 绑定反向（会在 `fullgraph=True` 下静默忽略 `backward_backend`）；把反向留在 `_FFPAAttnFunc.backward` 按运行时 `meta.backward_meta` 分发。仅当反向公式唯一时（如 varlen）才用 `custom_op`+`register_autograd`。

最后别忘了更新 `functional.py:946-965` 那张「前向↔反向」注释表，把你的新后端加进去——它是后来者理解分发关系的第一手文档。

## 6. 本讲小结

- **后端是一份四件套契约**：`Backend` 子类 + `_BACKEND_MAP` 名字 + `isinstance` 分发分支 + `torch.library` 算子。四者缺一，分发链路都会断。
- **`_BACKEND_MAP` 是后端名的唯一注册点**，它是 `_coerce_backend`（`functional.py:284-305`）内的局部字典；`source` 参数让同一字符串名在前向/反向位置解析出不同 `forward/backward` 标志，这是前向反向可独立解耦的根。
- **`torch.library` 三件套**（define/impl/register_fake）让 kernel 成为 `torch.compile` 友好的合法算子；meta 实现必须与真实现逐行对齐（尤其 LSE 对齐粒度），不可跨后端复用。
- **FFPA 基本不用 `register_autograd`**，因为「一前向多反向」；反向按运行时 `meta.backward_meta` 在 `_FFPAAttnFunc.backward` 分发。varlen 是唯一例外（单一反向公式，用 `custom_op`+`register_autograd`）。
- **head_dim 有三套相互独立的列表**：CUDA 构建集（`env.py::get_enabled_headdims`，默认 `range(256,1025,64)`）、Triton 调优集（`DEFAULT_HEADDIMS=[320,512,640,768,1024]`）、测试覆盖集（`tests/...::HEADDIMS`）。新增一个维度时，先确认要扩哪一层——448 在第一层已自动支持、在第二三层才是真正的新增。
- **CUDA 代码生成全自动**：只要 head_dim 进入 `get_enabled_headdims`，`generate_split_headdim_sources` 自动产出每维度 2 文件 3 符号并被 dispatch 选中；bf16 因无 bf16-acc MMA PTX 被代码生成层强制 fp32 累加。

## 7. 下一步学习建议

本讲是学习手册的收官篇，建议从以下方向继续深入：

1. **动手做一个最小后端**：仿照 `cuda/__init__.py`（仅前向、最简）写一个 `MyBackend`，把本讲场景 B 的清单逐项落实，用 `forward_backend=MyBackend(...)` 跑通一次大 D 前向。这是验证你是否真正理解分发链路的最好方式。
2. **重读 u3-5 与 u8-3**：现在你已经从「扩展者」视角看过 op 注册与持久化查找，再回看这两篇会对「meta 实现对齐」「就近匹配回退」有更深的体会。
3. **跟踪一个真实的 head_dim 接入**：以 480（三层都没有）为目标，按本讲综合实践的场景 A 完整走一遍「代码生成 → 调优 → 测试」，体会三套列表的不同节奏。
4. **阅读 `csrc/cuffpa/ffpa_attn_api.cc` 与 `launch_templates.cuh`**：如果你新增的 head_dim 需要非默认的 tile/stage 配置，就要在 `getConfigXXX()`（u7-2/u7-4）里加特化——这是从「Python 接线」跨入「CUDA kernel 工程」的入口。
5. **贡献回上游**：FFPA 是开源项目，新增后端/head_dim 时记得同步更新 `docs/`（尤其是 `docs/env.md` 的开关表与 `docs/user_guide/autotune.md` 的网格说明），并按 u9-1/u9-2 的测试范式补齐正确性与调优回归。
