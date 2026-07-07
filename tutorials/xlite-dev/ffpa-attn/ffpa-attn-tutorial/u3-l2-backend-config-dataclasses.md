# Backend 配置类体系

## 1. 本讲目标

上一讲（u3-l1）我们建立了「四后端总览与选型矩阵」的全局地图，知道了 SDPA / CUDA / Triton / CuTeDSL 四个后端各自的定位与能力。但那只是一张静态的能力表——读者真正写代码时，是用关键字参数（`backend=`、`forward_backend=`、`backward_backend=`）告诉 FFPA「这次调用用哪个后端」。这些参数在内部是如何被解析、校验、并最终变成驱动 kernel 分发的对象的？本讲就回答这个问题。

学完后你应当掌握：

- `Backend` 基类中 `forward` / `backward` 两个 `bool | None` 字段的「None 语义」与自动补全规则。
- 四个后端子类（`SDPABackend` / `CUDABackend` / `TritonBackend` / `CuTeDSLBackend`）各自的专有字段、`__post_init__` 校验，以及它们在前向 / 反向中的角色。
- `TritonBackend` 上一排高级开关（`autotune` / `enable_tma` / `enable_ws` / `persist_dkdv` / `split_launch` 等）的含义与依赖关系。
- `backend` / `forward_backend` / `backward_backend` 三个参数的优先级，以及字符串如何经 `_coerce_backend` 转成 `Backend` 实例。

## 2. 前置知识

- **dataclass（数据类）**：Python 的 `@dataclass` 装饰器会自动生成 `__init__`、`__repr__` 等方法。本讲里所有 Backend 配置类都是 dataclass，字段就是构造参数。`__post_init__` 是 dataclass 在 `__init__` 末尾自动调用的钩子，常用来做字段校验与派生。
- **`None` 作为「未显式设置」的语义**：本讲大量出现 `forward: bool | None = None`。这里的 `None` 不是「关闭」，而是「用户没说，请你帮我推断」。区分这两者是理解自动补全规则的关键。
- **前向（forward）与反向（backward）解耦**：FFPA 的前向 kernel 与反向 kernel 是**独立选择**的——你可以「CUDA 前向 + Triton 反向」「Triton 前向 + SDPA 反向」等组合。所以配置不是一个后端，而是**一对**后端（前向后端 + 反向后端）。
- **承接 u3-l1**：四个后端的名字字符串 `"sdpa"` / `"cuda"` / `"triton"` / `"cutedsl"`，以及「Triton 是默认后端、CuTeDSL 在 H200 最快、CUDA 仅前向」这些结论，本讲直接使用，不再重复。

## 3. 本讲源码地图

本讲几乎全部内容集中在**一个文件**里：

| 文件 | 作用 |
| --- | --- |
| [src/ffpa_attn/functional.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py) | 分发层核心。包含 `Backend` 基类与四个子类、`_coerce_backend` 字符串转换、`_resolve_backend_pair` 配对校验、`FFPAAttnMeta.from_kwargs` 参数解析。 |

具体落点：

- `Backend` 基类与 `None` 语义：第 108–129 行。
- `SDPABackend`：第 132–148 行。
- `CUDABackend`：第 150–171 行。
- `TritonBackend`：第 174–218 行。
- `CuTeDSLBackend`：第 221–242 行。
- `_resolve_backend_pair`（配对与 cutedsl 耦合校验）：第 253–281 行。
- `_coerce_backend` / `_coerce_optional_backend`（字符串 ↔ 实例）：第 284–313 行。
- `FFPAAttnMeta.from_kwargs`（三参数优先级解析）：第 408–450 行。

## 4. 核心概念与源码讲解

### 4.1 Backend 基类：forward/backward 的 None 语义与自动补全

#### 4.1.1 概念说明

FFPA 把「一次注意力调用」拆成前向和反向两段，每段各自挑后端。所以一个后端配置对象必须回答两个问题：

1. 这个对象**要不要管前向**？
2. 这个对象**要不要管反向**？

最朴素的设计是两个 `bool` 字段。但 FFPA 用的是 `bool | None`，把 `None` 定义为「用户没有显式声明，请按约定自动补全」。这样做的好处是：用户只写 `TritonBackend()` 一个对象就能同时表示「前向 + 反向都用 Triton」，而不必啰嗦地写 `TritonBackend(forward=True, backward=True)`。

#### 4.1.2 核心流程

`__post_init__` 的自动补全规则（三种情形，互斥且完备）：

```text
若 forward 与 backward 都是 None  →  两者都置 True      （「同时管前向和反向」）
若只有 forward 是 None            →  forward = not backward （「我只管反向，前向你别管」）
若只有 backward 是 None           →  backward = not forward （「我只管前向，反向你别管」）
若两者都已显式给出                  →  原样保留（不做任何事）
```

注意第三、四种情形：用户**可以**显式写 `forward=True, backward=True`（一个对象同时承担两段），也可以写 `forward=True, backward=False`（只承担前向）。这正是「前向 / 反向解耦」在配置层的体现。

#### 4.1.3 源码精读

基类定义与字段：

[Backend 基类与 None 语义字段 src/ffpa_attn/functional.py:108-129](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L108-L129)

```python
@dataclass
class Backend:
  name: str
  forward: bool | None = None
  backward: bool | None = None

  def __post_init__(self) -> None:
    if self.forward is None and self.backward is None:
      self.forward = True
      self.backward = True
    elif self.forward is None:
      self.forward = not self.backward
    elif self.backward is None:
      self.backward = not self.forward
```

要点：

- `name` 是后端标识字符串（`"triton"` / `"cutedsl"` / `"cuda"` / `"sdpa"`），后续 dispatch 与 `fallback()` 都靠它做字符串比较，而不是 `isinstance`。
- `forward` / `backward` 默认 `None`，由 `__post_init__` 补全。
- 补全后的不变量：`forward` 和 `backward` 不会再是 `None`，且至少有一个为 `True`（不可能两个都被显式设成 `False`——那是无意义的配置，但基类不做这个检查，由下游 `_resolve_backend_pair` 兜底，见 4.4）。

#### 4.1.4 代码实践

**实践目标**：亲手验证三种构造方式下 `forward` / `backward` 的补全结果。

**操作步骤**（纯 CPU 即可，不依赖 GPU）：

```python
# 示例代码：验证 Backend 的 None 语义
from ffpa_attn.functional import Backend

# 不建议直接用基类，这里只为观察 __post_init__ 行为
b1 = Backend(name="demo")                         # 两者都 None
b2 = Backend(name="demo", backward=True)          # 只有 backward 显式
b3 = Backend(name="demo", forward=True)           # 只有 forward 显式
b4 = Backend(name="demo", forward=True, backward=True)  # 两者都显式

for b in (b1, b2, b3, b4):
    print(b.forward, b.backward)
```

**需要观察的现象 / 预期结果**：

```text
True True        # b1：都 None → 都补 True
False True       # b2：forward=None → forward = not True = False
True False       # b3：backward=None → backward = not True = False
True True        # b4：都显式 → 原样保留
```

这个「`forward=not backward`」的反向推导正是「一个对象只承担一段」的关键：`b2` 表示「我只负责反向」，`b3` 表示「我只负责前向」。

#### 4.1.5 小练习与答案

**练习 1**：如果用户写 `Backend(name="x", forward=False, backward=False)`，`__post_init__` 会怎么处理？合法吗？

**参考答案**：两个都显式为 `False`，不命中任何 `if/elif` 分支，`__post_init__` 原样保留 `(False, False)`。基类层面不报错，但这是一个「既不管前向也不管反向」的废配置；它会在后续 `_resolve_backend_pair` 里被 `assert forward_backend.forward` 或 `assert backward_backend.backward` 拦下（见 4.4）。

**练习 2**：为什么 `forward` / `backward` 设计成 `bool | None` 而不是直接 `bool`？

**参考答案**：为了区分「用户显式说关闭」与「用户没说」。如果是纯 `bool` 且默认 `True`，就无法表达「我构造这个对象时只想声明反向、前向留给另一个对象去补」；`None` 充当了「未声明」的第三态，使 `TritonBackend()`（一个对象管两段）与 `TritonBackend(forward=True, backward=False)`（只管前向）能用同一套字段表达。

---

### 4.2 四个后端子类：字段、校验与专长

#### 4.2.1 概念说明

`Backend` 只描述了「管不管前向 / 反向」这件通用的事。每个真实后端还有自己的**专有旋钮**和**硬约束**：

- **SDPABackend**：几乎是个空壳，因为前向永远在公共 API 层短路回退到原生 SDPA（见 u1-l4 / u3-l3），它主要充当「反向后端」——把反向交给 PyTorch efficient attention。
- **CUDABackend**：手写 CUDA，**只有前向**。多了 MMA 累加器精度 `acc` 和流水线 `stages` 两个旋钮。
- **TritonBackend**：默认后端，前向 + 反向齐全，旋钮最多（autotune、TMA、warp-specialize、persist 等）。
- **CuTeDSLBackend**：基于 CUTLASS 的 SM90 专用后端，旋钮较少，目前主要暴露一个反向存储精度选项。

#### 4.2.2 核心流程

每个子类都遵循同一个套路：

```text
@dataclass
class XxxBackend(Backend):
    name = "<固定字符串>"            # 各自的默认 name
    <专有字段> = <默认值>
    def __post_init__(self):
        super().__post_init__()     # 先跑基类的 forward/backward 补全
        <子类专属校验 / 派生>        # 再做自己的 assert / 归一化
```

注意 `super().__post_init__()` 这一行不可省——它保证基类的 `None` 补全先跑，子类校验里才能放心地用 `self.backward` / `self.forward`（此时它们已必为 `bool`）。

四个子类的字段对照表：

| 子类 | `name` | 专有字段 | 前向 | 反向 | 关键校验 |
| --- | --- | --- | --- | --- | --- |
| `SDPABackend` | `"sdpa"` | `high_precision_grad: bool` | ✅（但恒短路回退） | ✅（走 efficient attn） | 无额外校验 |
| `CUDABackend` | `"cuda"` | `acc: str`、`stages: int` | ✅ | ❌ | `assert not self.backward`；`acc ∈ {"f16","f32"}` |
| `TritonBackend` | `"triton"` | `autotune`、`autotune_mode`、`enable_tma`、`enable_ws`、`persist_dkdv`、`split_launch`、`preprocess_d_chunk`、`grad_kv_storage_dtype`、`grad_q_storage_dtype` | ✅ | ✅ | `autotune_mode ∈ {"fast","max"}`；`persist_dkdv` 需 `enable_tma` 且 `backward=True`；多个反向专用开关需 `backward=True` |
| `CuTeDSLBackend` | `"cutedsl"` | `grad_kv_storage_dtype` | ✅ | ✅ | `grad_kv_storage_dtype` 需 `backward=True` |

#### 4.2.3 源码精读

**SDPABackend**——最简单的子类，仅多一个 `high_precision_grad`：

[SDPABackend 字段与 docstring src/ffpa_attn/functional.py:132-148](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L132-L148)

docstring 里两句关键说明：**前向恒短路**（`Forward always short-circuits via FFPAAttnMeta.fallback`），作为反向后端时**委托给 `_efficient_attn_backward_aten`**。

**CUDABackend**——仅前向，多了精度与流水线：

[CUDABackend 字段、校验与 acc_code src/ffpa_attn/functional.py:150-171](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L150-L171)

```python
name: str = "cuda"
acc: str = "f32"
stages: int = 4 if _is_hopper_or_later() else 3

def __post_init__(self) -> None:
  super().__post_init__()
  assert not self.backward, "cuda backend does not support backward"
  assert self.acc in ("f16", "f32"), ...

@property
def acc_code(self) -> int:
  return _ACC_F32 if self.acc == "f32" else _ACC_F16
```

要点：

- `stages` 默认值在**类定义时**就求值：`_is_hopper_or_later()` 查 `torch.cuda.get_device_capability()`，Hopper(9.x) 及以上给 4 级流水线，其余给 3 级。
- `assert not self.backward` 硬性声明 CUDA 无反向——如果你想写 `CUDABackend(backward=True)` 会直接 `AssertionError`。
- `acc_code` 把字符串 `"f32"`/`"f16"` 翻译成与 C++ 端约定的整数码 `_ACC_F32=1` / `_ACC_F16=0`（见文件顶部 [src/ffpa_attn/functional.py:47-48](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L47-L48)），这个码会一路传到手写 CUDA kernel（详见 u7-l3）。

**TritonBackend**——旋钮最多，校验也最多：

[TritonBackend 全部字段 src/ffpa_attn/functional.py:174-202](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L174-L202)

[TritonBackend 的 __post_init__ 校验 src/ffpa_attn/functional.py:204-218](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L204-L218)

各开关含义（结合 docstring 第 178–191 行）：

| 开关 | 作用 | 依赖 |
| --- | --- | --- |
| `autotune` | 是否对 kernel 参数做 Triton autotune | 无 |
| `autotune_mode` | 调优搜索粒度，`"fast"` 小空间 / `"max"` 大空间 | 必须是这两者之一 |
| `enable_tma` | 启用 SM90+ 的 TMA 硬件加速（实验性） | 无 |
| `enable_ws` | 强制 warp-specialized 配置 | 需 `enable_tma=True` |
| `persist_dkdv` | 让 dK/dV 累加器以 fp32 跨块常驻寄存器 | 需 `enable_tma=True` **且** `backward=True` |
| `split_launch` | 反向把 dKdV 与 dQ 拆成两次独立 launch | 需 `backward=True` |
| `preprocess_d_chunk` | 反向 delta 预处理按 tile 分块 | 需 `backward=True` |
| `grad_kv_storage_dtype` | dK/dV 的跨 tile 存储精度（`"fp16"`/`"fp32"`） | 需 `backward=True` |
| `grad_q_storage_dtype` | dQ 的跨 tile 存储精度 | 需 `backward=True` |

校验逻辑里两条最重要的约束：

```python
if self.persist_dkdv:
  assert self.backward, "persist_dkdv is only valid for Triton backward"
  assert self.enable_tma, "persist_dkdv requires enable_tma=True"
if self.split_launch or self.preprocess_d_chunk or ... :
  assert self.backward, "backward-only Triton options require backward=True"
```

也就是说：`persist_dkdv`、`split_launch`、`preprocess_d_chunk`、`grad_*_storage_dtype` 都是**反向专用**开关，若把这个 `TritonBackend` 配置成只管前向（`backward=False`），它们就非法。这保证了一个前向对象不会被误塞反向旋钮。`grad_kv_storage_dtype` / `grad_q_storage_dtype` 会被 `_normalize_grad_kv_storage_dtype` 归一化成 `torch.float16` / `torch.float32`（或 `None`）。

**CuTeDSLBackend**——旋钮最少：

[CuTeDSLBackend 字段与校验 src/ffpa_attn/functional.py:221-242](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L221-L242)

注意 docstring 点明：`grad_kv_storage_dtype` 仅作用于 **SM80 路径**的内部 dK/dV HBM 缓冲，最终梯度总会 cast 回 `k.dtype` / `v.dtype`；**SM90 路径目前忽略它，若设置会抛错**。另外 `name = "cutedsl"`——记住这个对外字符串与子包目录名 `cute/` 不一致（见 u1-l3）。

#### 4.2.4 代码实践

**实践目标**：用 `assert` 触发各子类的校验，直观感受「硬约束」。

**操作步骤**：

```python
# 示例代码：触发各后端的 __post_init__ 校验
from ffpa_attn.functional import CUDABackend, TritonBackend, CuTeDSLBackend

# 1) CUDA 不允许 backward
try:
    CUDABackend(backward=True)
except AssertionError as e:
    print("CUDA:", e)

# 2) persist_dkdv 必须 enable_tma + backward
try:
    TritonBackend(persist_dkdv=True)             # 缺 enable_tma
except AssertionError as e:
    print("Triton persist_dkdv:", e)

# 3) autotune_mode 非法值
try:
    TritonBackend(autotune_mode="ultra")
except AssertionError as e:
    print("Triton autotune_mode:", e)

# 4) cutedsl 的 grad_kv_storage_dtype 是反向专用
try:
    CuTeDSLBackend(grad_kv_storage_dtype="fp32", forward=True, backward=False)
except AssertionError as e:
    print("CuTeDSL:", e)
```

**需要观察的现象 / 预期结果**：四条 `AssertionError` 都被捕获并打印，分别对应「cuda backend does not support backward」「persist_dkdv requires enable_tma=True」「Unsupported autotune_mode」「grad_kv_storage_dtype is a backward-only option」。

> 若你的环境里 `functional.py` 顶层 `from .cute import ...` 失败（CuTeDSL 未安装），`CuTeDSLBackend` 这个 dataclass 本身仍可构造——它只是个普通类，不依赖 cute 子包是否可用。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `CUDABackend` 要在 `__post_init__` 里 `assert not self.backward`，而不是干脆不继承 `backward` 字段？

**参考答案**：`backward` 字段来自基类 `Backend`，所有后端共享同一套字段签名，便于 `_resolve_backend_pair`、dispatch 等处用统一方式处理任意后端对象。去掉字段会破坏这一统一接口；用 `assert` 既保留了接口，又在构造时立刻把「CUDA 无反向」这个硬约束钉死，避免错误配置流到更深处。

**练习 2**：`TritonBackend(persist_dkdv=True, enable_tma=True)` 构造出来后，它的 `forward` / `backward` 分别是什么？

**参考答案**：两个都没显式给 → 命中基类「都 None → 都补 True」，所以 `forward=True, backward=True`。于是 `persist_dkdv` 的 `assert self.backward` 通过。这是一个「同时管前向 + 反向」、且反向启用 persist 的 Triton 配置。

---

### 4.3 _coerce_backend：字符串到 Backend 实例的统一转换

#### 4.3.1 概念说明

用户调用 `ffpa_attn_func(..., backend="cutedsl")` 时传的是**字符串**，但 dispatch 层需要的是**`Backend` 实例**（要去读它的 `name`、`acc_code`、`enable_tma` 等字段）。`_coerce_backend` 就是这两者之间的桥：它接受 `str` 或 `Backend` 实例，统一吐出一个 `Backend` 实例。这样上层只需写一套逻辑，不必区分「用户传的是字符串还是对象」。

#### 4.3.2 核心流程

```text
输入 backend, source
  ├─ 若 backend 是字符串：
  │    查 _BACKEND_MAP 得到类 cls（cuda/triton/cutedsl/sdpa 之一）
  │    若查不到 → ValueError
  │    若 source == "backend"        → 返回 cls()           （同时管前向+反向）
  │    否则（source 以 "forward"/"backward" 开头）：
  │        is_forward = source.startswith("forward")
  │        返回 cls(forward=is_forward, backward=not is_forward)
  └─ 若 backend 是 Backend 实例 → 原样返回
     否则 → TypeError
```

关键点：**同一个字符串，因 `source` 不同会生成 forward/backward 标志不同的对象**。

- `backend="cutedsl"`（source=`"backend"`）→ `CuTeDSLBackend()` → `(forward=True, backward=True)`。
- `forward_backend="cutedsl"`（source=`"forward_backend"`）→ `CuTeDSLBackend(forward=True, backward=False)`。
- `backward_backend="cutedsl"`（source=`"backward_backend"`）→ `CuTeDSLBackend(forward=False, backward=True)`。

`source` 的取值就三个：`"backend"` / `"forward_backend"` / `"backward_backend"`，分别对应用户的三个参数。

#### 4.3.3 源码精读

[_coerce_backend 字符串到实例的转换 src/ffpa_attn/functional.py:284-305](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L284-L305)

```python
def _coerce_backend(backend: Backend | str, *, source: str) -> Backend:
  if isinstance(backend, str):
    _BACKEND_MAP = {
      "cuda": CUDABackend,
      "triton": TritonBackend,
      "cutedsl": CuTeDSLBackend,
      "sdpa": SDPABackend,
    }
    cls_name = _BACKEND_MAP.get(backend)
    if cls_name is None:
      raise ValueError(...)
    if source == "backend":
      return cls_name()
    is_forward = source.startswith("forward")
    return cls_name(forward=is_forward, backward=not is_forward)
  if not isinstance(backend, Backend):
    raise TypeError(...)
  return backend
```

旁边还有一个可选版本的封装，处理 `None`：

[_coerce_optional_backend src/ffpa_attn/functional.py:308-313](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L308-L313)

它的作用是：当某个参数没传（`None`）时，原样返回 `None`，交给上游 `from_kwargs` 用默认值兜底。

要点：

- `_BACKEND_MAP` 把对外字符串映射到类。**`"cutedsl"` → `CuTeDSLBackend`**，再次印证对外名 `cutedsl` 与目录 `cute/` 的不一致。
- 传 `Backend` 实例（如 `CuTeDSLBackend(grad_kv_storage_dtype="fp32")`）时**原样返回**，所以用户可以用字符串做简写，也可以传一个带高级旋钮的实例做精调——两种入口等价地汇入同一条管道。

#### 4.3.4 代码实践

**实践目标**：观察同一个字符串在不同 `source` 下生成的对象差异。

**操作步骤**：

```python
# 示例代码：观察 _coerce_backend 对 source 的敏感
from ffpa_attn.functional import _coerce_backend

a = _coerce_backend("cutedsl", source="backend")
b = _coerce_backend("cutedsl", source="forward_backend")
c = _coerce_backend("cutedsl", source="backward_backend")

for obj in (a, b, c):
    print(obj.name, "forward=", obj.forward, "backward=", obj.backward)

# 传实例则原样返回
from ffpa_attn.functional import CuTeDSLBackend
inst = CuTeDSLBackend(grad_kv_storage_dtype="fp32")
print(_coerce_backend(inst, source="forward_backend") is inst)   # True
```

**需要观察的现象 / 预期结果**：

```text
cutedsl forward= True  backward= True
cutedsl forward= True  backward= False
cutedsl forward= False backward= True
True
```

#### 4.3.5 小练习与答案

**练习 1**：用户传 `forward_backend="cuda"`，`_coerce_backend` 会返回什么？接着会发生什么？

**参考答案**：返回 `CUDABackend(forward=True, backward=False)`。构造时 `CUDABackend.__post_init__` 跑 `super().__post_init__()`（`backward=False` 已显式，补 `forward = not False = True`，与传入一致），然后 `assert not self.backward` 通过（`backward=False`）。所以这个对象合法，表示「前向用 CUDA」。

**练习 2**：用户传 `backward_backend="cuda"` 会怎样？

**参考答案**：`_coerce_backend` 返回 `CUDABackend(forward=False, backward=True)`，但 `CUDABackend.__post_init__` 的 `assert not self.backward` 立刻失败，抛 `AssertionError: cuda backend does not support backward`。即字符串层不拦、由子类的硬约束拦——这是一个「失败尽量早」的设计。

---

### 4.4 from_kwargs：backend / forward_backend / backward_backend 的优先级与解析

#### 4.4.1 概念说明

`ffpa_attn_func` 的签名末尾是 `**kwargs`，里面可能含三个后端参数：`backend`、`forward_backend`、`backward_backend`。`FFPAAttnMeta.from_kwargs` 是它们的总解析器，负责把这三个参数（可能是字符串、实例、或缺省）归约成一对合法的 `Backend`：`forward_meta` + `backward_meta`。

三个参数的**优先级**（docstring 明示）：

```text
显式 forward_backend / backward_backend   >   backend   >   默认 Triton
```

也就是说：

- 显式给了 `forward_backend` 或 `backward_backend`，就以它们为准。
- 都没给、但给了 `backend`，则 `backend` 同时充当前向和反向。
- 三者都没给，前向和反向都默认 `TritonBackend`。

此外还有两条**配对规则**（在 `_resolve_backend_pair` 里强制）：

1. cutedsl 必须**对称使用**：`forward_backend='cutedsl'` 要求 `backward_backend='cutedsl'`，反之亦然，否则 `ValueError`。
2. 为了方便，`from_kwargs` 里如果只指定了 cutedsl 的一侧，会**自动补全**另一侧为 cutedsl，免去用户写两遍。

#### 4.4.2 核心流程

`from_kwargs` 的解析流程（按代码顺序）：

```text
1. 依次 pop 出 backend / forward_backend / backward_backend
   （forward/backward 立即经 _coerce_optional_backend 转成 Backend | None）
2. 若 kwargs 还有剩余 → TypeError（不允许未知参数）
3. 若 forward 和 backward 都为 None 且 backend 非 None：
      把 backend 经 _coerce_backend(source="backend") 转成对象，
      forward_backend = backward_backend = 该对象        # backend 同时充当两段
4. cutedsl 单边自动补全：
      若只给了 forward=cutedsl → backward_backend = CuTeDSLBackend()
      若只给了 backward=cutedsl → forward_backend = CuTeDSLBackend()
5. _resolve_backend_pair(forward_backend, backward_backend)：
      - None 的一侧补默认 TritonBackend(forward=True 或 backward=True)
      - 类型检查 + assert forward.forward / backward.backward
      - 校验 cutedsl 对称（不一致则 ValueError）
6. 用这对 Backend 构造 FFPAAttnMeta(forward_meta=..., backward_meta=...)
```

`_resolve_backend_pair` 的默认与耦合逻辑：

[_resolve_backend_pair 默认值与 cutedsl 耦合校验 src/ffpa_attn/functional.py:253-281](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L253-L281)

```python
forward_backend = TritonBackend(forward=True) if forward_backend is None else forward_backend
backward_backend = TritonBackend(backward=True) if backward_backend is None else backward_backend
...
assert forward_backend.forward, "forward_backend must be configured with forward=True"
assert backward_backend.backward, "backward_backend must be configured with backward=True"

if forward_backend.name == "cutedsl" and backward_backend.name != "cutedsl":
  raise ValueError("forward_backend='cutedsl' requires backward_backend='cutedsl'")
if backward_backend.name == "cutedsl" and forward_backend.name != "cutedsl":
  raise ValueError("backward_backend='cutedsl' requires forward_backend='cutedsl'")
```

> **关于 cutedsl 对称约束的说明**：配置层（`_resolve_backend_pair`）目前**强制** cutedsl 前后配对，所以从 `ffpa_attn_func` 公共 API 层面，cutedsl 必须前后都用或都不用。这条约束比 `_FFPAAttnFunc` 注释里枚举的「理论可组合表」（见 u3-l4）更严——那是反向 dispatch 本身的能力边界，本讲的配置层在它之上额外收窄了允许组合。本讲以代码实际执行的对称为准。

#### 4.4.3 源码精读

[from_kwargs 三参数解析与优先级 src/ffpa_attn/functional.py:408-450](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L408-L450)

```python
backend = kwargs.pop("backend", None)
forward_backend = _coerce_optional_backend(
  kwargs.pop("forward_backend", None), source="forward_backend"
)
backward_backend = _coerce_optional_backend(
  kwargs.pop("backward_backend", None), source="backward_backend"
)

if kwargs:
  raise TypeError(...)                          # 未知参数一律拒绝

if forward_backend is None and backward_backend is None and backend is not None:
  backend = _coerce_backend(backend, source="backend")
  forward_backend = backend
  backward_backend = backend                    # backend 充当两段

# cutedsl 单边补全（见 4.4.2 第 4 步）
if forward_backend is not None and backward_backend is None and forward_backend.name == "cutedsl":
  backward_backend = CuTeDSLBackend()
if backward_backend is not None and forward_backend is None and backward_backend.name == "cutedsl":
  forward_backend = CuTeDSLBackend()

forward_backend, backward_backend = _resolve_backend_pair(forward_backend, backward_backend)
return cls(forward_meta=forward_backend, backward_meta=backward_backend)
```

三个**容易踩的细节**：

1. **`backend` 在有显式 forward/backward 时被静默忽略**：第 3 步的 `if` 要求 `forward_backend is None and backward_backend is None`。若用户同时传了 `backend="cutedsl"` 和 `forward_backend="triton"`，则 `backend` 既不被 coerce 也不被校验，直接丢弃——最终前向是 triton、反向是默认 triton。也就是说，`backend` 不是一个「默认值」，而是一个「仅当两侧都未指定时才生效的简写」。
2. **未知 kwarg 会被拒绝**：第 2 步 `if kwargs: raise TypeError`，所以传错参数名（如 `forward_backend_typo=...`）会立刻报错，而不是被 `**kwargs` 默默吞掉。
3. **cutedsl 单边补全用的是 `CuTeDSLBackend()`**：即 `(forward=True, backward=True)` 的全管对象，交给 `_resolve_backend_pair` 后只关心它对应的那一侧标志，对称校验也通过。

另外两个构造入口 `from_backends` / `from_options`（[src/ffpa_attn/functional.py:452-472](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L452-L472)）更简单：它们只接收 `forward_backend` / `backward_backend` 两个 `Backend` 对象（不做字符串转换、不做 `backend` 简写），直接交 `_resolve_backend_pair`。`from_options` 只是 `from_backends` 的别名。`from_kwargs` 是公共 API `ffpa_attn_func` 实际走的入口。

#### 4.4.4 代码实践

**实践目标**：用三种方式指定 CuTeDSL，验证它们解析出等价的 `forward_meta` / `backward_meta`，并理解优先级。

**操作步骤**（纯 CPU 可跑，只构造 meta，不真正运行 kernel）：

```python
# 示例代码：三种方式指定 CuTeDSL 的等价性验证
from ffpa_attn.functional import FFPAAttnMeta, CuTeDSLBackend

# 方式 A：backend 简写（两侧都未显式 → backend 充当两段）
mA = FFPAAttnMeta.from_kwargs(backend="cutedsl")

# 方式 B：只指定 forward_backend（cutedsl 单边 → 自动补全 backward）
mB = FFPAAttnMeta.from_kwargs(forward_backend="cutedsl")

# 方式 C：只指定 backward_backend（cutedsl 单边 → 自动补全 forward）
mC = FFPAAttnMeta.from_kwargs(backward_backend="cutedsl")

# 方式 D（对照）：两侧都显式
mD = FFPAAttnMeta.from_kwargs(forward_backend="cutedsl", backward_backend="cutedsl")

def show(tag, m):
    f, b = m.forward_meta, m.backward_meta
    print(f"{tag}: fwd={f.name}(forward={f.forward},backward={f.backward}) "
          f"bwd={b.name}(forward={b.forward},backward={b.backward})")

show("A backend=           ", mA)
show("B forward_backend=   ", mB)
show("C backward_backend=  ", mC)
show("D both explicit      ", mD)

# 优先级演示：backend 遇到显式 forward_backend 时被忽略
mE = FFPAAttnMeta.from_kwargs(backend="cutedsl", forward_backend="triton")
show("E backend+forward    ", mE)   # 前向 triton，反向默认 triton；cutedsl 被忽略
```

**需要观察的现象 / 预期结果**：

```text
A backend=           : fwd=cutedsl(forward=True,backward=True)  bwd=cutedsl(forward=True,backward=True)
B forward_backend=   : fwd=cutedsl(forward=True,backward=False) bwd=cutedsl(forward=True,backward=True)
C backward_backend=  : fwd=cutedsl(forward=True,backward=True)  bwd=cutedsl(forward=False,backward=True)
D both explicit      : fwd=cutedsl(forward=True,backward=False) bwd=cutedsl(forward=False,backward=True)
E backend+forward    : fwd=triton(forward=True,backward=False)  bwd=triton(forward=True,backward=True)
```

**等价性解读**：A / B / C / D 四种的 `forward_meta.name` 与 `backward_meta.name` 都是 `cutedsl`——从 dispatch 角度（只看 `.name` 与对应的 `forward`/`backward` 标志）它们**等价**，都会把前向和反向都送到 CuTeDSL kernel。它们的差异仅在「那个对象身上的另一个标志位是什么」（例如 B 的 `forward_backend` 对象 `backward=False`，但这不影响 dispatch，因为 `_resolve_backend_pair` 已保证 `forward_meta.forward=True`、`backward_meta.backward=True`）。

**优先级解读**：E 证明了「显式 `forward_backend` 覆盖 `backend`」——尽管 `backend="cutedsl"`，最终前向是 triton；又因为反向未显式且 cutedsl 没有任何一侧被采纳，反向取默认 triton。`backend` 被完全忽略。

> 若想在真实 kernel 上验证（而非仅 meta），需要 SM8x / SM90 的 NVIDIA GPU 与已安装的 CuTeDSL 后端；在普通 CPU 上本实践的 meta 解析部分即可完整运行。**待本地验证**：在 H200 / SM90 机器上把 `mA` 对应的调用替换进 `ffpa_attn_func`，确认确实走 CuTeDSL kernel。

#### 4.4.5 小练习与答案

**练习 1**：`FFPAAttnMeta.from_kwargs(forward_backend="cutedsl", backward_backend="triton")` 会成功吗？为什么？

**参考答案**：不会。两侧都显式指定，跳过 cutedsl 单边补全；进入 `_resolve_backend_pair` 后，`forward_backend.name == "cutedsl"` 而 `backward_backend.name != "cutedsl"`，命中第一条耦合校验，抛 `ValueError: forward_backend='cutedsl' requires backward_backend='cutedsl'`。配置层强制 cutedsl 对称。

**练习 2**：请按优先级排出以下三种调用在前向后端上的最终取值：（a）不传任何后端参数；（b）`backend="sdpa"`；（c）`backend="triton", forward_backend="cutedsl"`。

**参考答案**：
- （a）前向 = 默认 `TritonBackend(forward=True)`（triton）。
- （b）两侧都没显式 → `backend` 生效 → 前向 = `SDPABackend()`（sdpa，forward=True）。注意 sdpa 前向会在 `fallback()` 里恒短路回退（见 u3-l3）。
- （c）`forward_backend` 显式 → `backend` 被忽略 → 前向 = `CuTeDSLBackend(forward=True, backward=False)`（cutedsl）；反向因 cutedsl 单边补全 → cutedsl。

**练习 3**：为什么 `from_kwargs` 要在第 2 步（`if kwargs: raise TypeError`）拒绝未知参数，而不是默默忽略？

**参考答案**：`ffpa_attn_func` 把 FFPA 专有配置放在 `**kwargs` 里。若默默吞掉未知参数，用户写错参数名（如把 `forward_backend` 拼错）时会得到「看似成功但实际用了默认后端」的静默错误，极难排查。显式拒绝把这类拼写错误前置到调用瞬间，符合「fail fast」原则。

## 5. 综合实践

把本讲三个知识点（None 语义、字符串转换、优先级）串起来，完成下面这个「后端配置探查器」小任务。

**任务**：写一个函数 `describe(**kwargs)`，它内部调用 `FFPAAttnMeta.from_kwargs(**kwargs)`，然后打印出「前向后端名 + 反向后端名 + 各自的 forward/backward 标志」。用它在以下场景里跑一遍，并解释每个结果：

1. `describe()`——全默认。
2. `describe(backend="cuda")`——CUDA 当两段，会发生什么？
3. `describe(forward_backend="cuda")`——CUDA 仅前向，反向默认什么？是否合法？
4. `describe(forward_backend=CUDABackend(acc="f16"))`——传带旋钮的实例。
5. `describe(forward_backend="cutedsl")`——cutedsl 单边补全。

**参考实现**：

```python
# 示例代码：综合实践——后端配置探查器
from ffpa_attn.functional import FFPAAttnMeta, CUDABackend

def describe(**kwargs):
    m = FFPAAttnMeta.from_kwargs(**kwargs)
    f, b = m.forward_meta, m.backward_meta
    extras = []
    if isinstance(f, CUDABackend):
        extras.append(f"fwd.acc={f.acc},stages={f.stages}")
    print(f"fwd={f.name}(f={f.forward},b={f.backward}) "
          f"bwd={b.name}(f={b.forward},b={b.backward}) {' '.join(extras)}")

describe()                                 # 1. 全默认 triton/triton
# describe(backend="cuda")                 # 2. 预期：构造时 AssertionError（CUDA 不能 backward）
describe(forward_backend="cuda")           # 3. fwd=cuda, bwd=triton
describe(forward_backend=CUDABackend(acc="f16"))  # 4. fwd=cuda 带 acc=f16
describe(forward_backend="cutedsl")        # 5. fwd/bwd 都是 cutedsl
```

**预期现象与解释**：

1. 全默认：`fwd=triton(f=True,b=True) bwd=triton(f=True,b=True)`——`_resolve_backend_pair` 给两侧各补一个 `TritonBackend`。
2. `backend="cuda"`：`_coerce_backend` 返回 `CUDABackend()`（forward=True, backward=True），但 `CUDABackend.__post_init__` 的 `assert not self.backward` 失败 → `AssertionError`。这正说明「CUDA 不能用 `backend=` 简写同时管两段」，必须只用 `forward_backend=`。
3. `forward_backend="cuda"`：前向 `CUDABackend(forward=True, backward=False)`（合法），反向未指定 → 默认 `TritonBackend(backward=True)`。结果 `fwd=cuda bwd=triton`——这正是「CUDA 前向 + Triton 反向」的组合。
4. 传实例：等价于方式 3，但 `acc` 被设成 `"f16"`（`acc_code` 将为 `_ACC_F16=0`），`extras` 会打印 `fwd.acc=f16,stages=...`。
5. cutedsl 单边：`fwd=cutedsl bwd=cutedsl`（反向被自动补全为 cutedsl）。

**待本地验证**：场景 2 的 `AssertionError` 文本、场景 4 的 `stages` 具体值（3 还是 4，取决于运行机器是否 Hopper+）。

## 6. 本讲小结

- `Backend` 基类用 `forward` / `backward` 两个 `bool | None` 字段表达「管不管某一段」；`None` 表示「未声明」，由 `__post_init__` 按三种情形自动补全为 `bool`。
- 四个子类各自加专有字段与硬约束：`SDPABackend` 几乎空壳（前向恒短路）；`CUDABackend` 仅前向，有 `acc` / `stages`；`TritonBackend` 旋钮最多（autotune、TMA、ws、persist、split_launch 等），且反向专用开关需 `backward=True`；`CuTeDSLBackend` 暴露一个 SM80 反向存储精度旋钮。
- `_coerce_backend` 把字符串 / 实例统一成 `Backend` 实例，且**同一个字符串随 `source` 不同**生成 forward/backward 标志不同的对象；传实例则原样返回。
- `from_kwargs` 按优先级 `显式 forward_backend/backward_backend > backend > 默认 Triton` 解析三参数；`backend` 仅在两侧都未显式时才充当两段，否则被静默忽略。
- 配置层强制 **cutedsl 对称**：`_resolve_backend_pair` 要求 cutedsl 前后配对，`from_kwargs` 还会为单边 cutedsl 自动补全另一侧，所以 `backend=` / `forward_backend=` / `backward_backend=` 三种写法对 cutedsl 等价。
- 未知 kwarg 被 `from_kwargs` 立即拒绝（`TypeError`），体现 fail-fast，避免后端参数拼写错误被静默吞掉。

## 7. 下一步学习建议

本讲只讲了「配置对象如何被构造和校验」，还没讲它们**如何驱动真正的 kernel 分发**。建议下一讲学习 **u3-l3（FFPAAttnMeta：输入校验与 SDPA 回退判定）** 与 **u3-l4（FFPAAttnFunc autograd Function 前向/反向分发）**：

- u3-l3 会讲 `FFPAAttnMeta.fallback()` 如何根据 `forward_meta.name` 与 head_dim / 序列长度决定是否绕过 FFPA 直接走 SDPA，以及 `normalize_inputs` / `normalize_attn_mask` 的全套校验。
- u3-l4 会讲 `_FFPAAttnFunc.forward` 如何按 `isinstance(meta.forward_meta, CUDABackend/TritonBackend/CuTeDSLBackend)` 分发到对应 kernel、`backward` 如何按 `meta.backward_meta` 选反向 kernel——那里你会看到本讲这些 `Backend` 对象的 `.name` / `.acc_code` / `.enable_tma` 等字段真正被消费。

如果你想立刻看到这些配置在真实 kernel 里的作用，也可以先跳到 **u4（Triton 后端前向）** 看 `autotune` / `enable_tma` 等开关如何影响 Triton kernel 的编译与 launch。
