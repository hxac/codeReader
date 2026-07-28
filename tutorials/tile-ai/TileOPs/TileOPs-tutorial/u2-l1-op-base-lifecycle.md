# Op 基类与生命周期

## 1. 本讲目标

上一篇（u1-l4）我们已经把「先实例化、再调用」的算子使用套路正式化成了一条**可调用契约**：`op = XxxOp(构造参数)`，然后 `out = op(*inputs)`。我们还知道构造期会「安装 kernel_map」，调用期才在 `forward` 里真正选并 JIT 编译 kernel。

但是，`op(*inputs)` 这一行背后到底发生了什么？「安装 kernel_map」又是怎么装的？本讲就钻进 `Op` 基类内部，把这条链路拆开。

学完本讲，你应当能够：

1. 说清 `Op` 基类的几个关键**类属性**（`kernel`、`kernel_map`、`dtype`、`_static_axes`）各自代表什么、在哪个阶段被填充。
2. 复述 **`dispatch_kernel → default_kernel_map → _install_kernel_map`** 这条「kernel 安装」标准流程，并能指出 `dispatch_kernel` 在子类构造函数里被调用的确切位置。
3. 理解 `forward` / `__call__` 的可调用契约，以及「快路径」复用的思想。
4. 认识三个 **codegen 契约方法**（`_infer_output_shapes` / `_validate_dtypes` / `eval_roofline`）及其当前的 `staged-rollout`（分阶段上线）状态。

> 本讲只讲「Op 基类这一层」的机制。代码生成（codegen）的内部实现留到 U8，kernel 选择与架构兼容的细节留到 u2-l2，形状推断与缓存的细节留到 u2-l3。

## 2. 前置知识

在继续之前，请确认你已经理解以下概念（它们来自 u1-l1 ~ u1-l4）：

- **双层分离**：每个算子被劈成 `Op`（L2，主机侧、无状态入口，负责校验/布局/torch.compile 兼容）和 `Kernel`（L1，TileLang 的硬件相关实现）。本讲的主角是 L2 的 `Op`。
- **input-inferred（输入推断）**：形状与 dtype 尽量延迟到调用时从输入张量推断，而不是在构造时写死。
- **可调用契约**：`op(*inputs)` 会转发给子类必须实现的 `forward`。
- **地面真值（ground truth）**：用 PyTorch 同名函数的输出来验证我们的算子是否正确。

另外需要一点点 Python 知识：

- **抽象基类（ABC）与 `@abstractmethod`**：被 `@abstractmethod` 标记的方法，子类必须实现，否则子类无法被实例化。
- **`@property`**：把一个方法伪装成属性访问，`obj.foo` 而不是 `obj.foo()`。
- **`__init_subclass__`**：一个钩子方法，每当某个类**继承** `Op` 时（即子类的 `class 定义被解释器执行的那一刻`），Python 会自动调用 `Op.__init_subclass__(cls)`。本讲会看到 TileOPs 用它来自动「装配」一些方法。

## 3. 本讲源码地图

本讲几乎全部围绕下面这一个文件展开，辅以几个真实子类作为佐证：

| 文件 | 作用 |
| --- | --- |
| `tileops/ops/op_base.py` | **本讲主角**。定义 `Op` 抽象基类：类属性、kernel 安装流程、可调用契约、三个 codegen 契约。 |
| `tileops/ops/gemm.py` | `GemmOp` —— 最经典的 input-inferred 子类，用来印证「构造期 dispatch、调用期 forward」的时序。 |
| `tileops/ops/reduction/softmax.py` | `SoftmaxFwdOp` 家族 —— 用来印证 `_static_axes` 在 forward 中动态绑定。 |
| `tileops/ops/compile_boundary.py` | `register_instance` —— `dispatch_kernel` 顺带做的「编译边界」实例注册。 |
| `tileops/kernels/kernel_base.py` | `Kernel` 基类 —— 用来理解 `kernel_map` 里登记的到底是什么。 |

## 4. 核心概念与源码讲解

### 4.1 Op 类属性：一个 Op 实例「持有什么」

#### 4.1.1 概念说明

`Op` 是一个抽象基类，它本身不能被实例化（因为有未实现的抽象方法）。但它在**类级别**声明了一组属性，规定了「每个 Op 子类的实例，在构造完成后、被调用前，应该持有哪些状态」。

可以把这些属性分成两类：

- **登记表类**：`kernel_map` —— 一个 `dict[str, Kernel类]`，记录「这个名字 → 用哪个 Kernel 类」。它在**构造期**就被填好。
- **运行时状态类**：`kernel`、`dtype`、`input_shapes` —— 它们在**调用期**（`forward`）才被填上真实值。

理解这条「**构造期填登记表、调用期填运行时状态**」的时序分界，是理解整个 Op 生命周期的钥匙。

#### 4.1.2 核心流程

```text
class Op(ABC):                    # 类定义被解释器执行
    kernel_map = None             # ← 登记表（构造期填充）
    kernel:   Kernel              # ← 当前激活 Kernel 实例（调用期填充）
    dtype:    None                # ← 运行时 dtype（调用期填充）
    _static_axes = frozenset()    # ← 构造期已「提交」的轴（见 4.3）
        │
        ▼  某个子类 class FooOp(Op): 被定义
   __init_subclass__(cls) 自动触发 → 装配 codegen 方法（见 4.4）
        │
        ▼  op = FooOp(...)        # 构造期
   __init__ → dispatch_kernel() → 填充 self.kernel_map（登记表）
        │
        ▼  out = op(a, b)         # 调用期
   forward() → 校验 / 推断 → 填充 self.kernel / self.dtype（运行时状态）
```

#### 4.1.3 源码精读

`Op` 的类属性定义在 [tileops/ops/op_base.py:45-55](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/op_base.py#L45-L55)：

```python
kernel: Kernel
kernel_map: Optional[dict[str, Kernel]] = None
dtype: Optional[torch.dtype] = None
device: Optional[Union[torch.device, str]] = 'cuda'
input_shapes: Optional[list[tuple]] = None

# Set of (input_index, axis) pairs identifying static (ctor-committed) axes.
_static_axes: frozenset[tuple[int, int]] = frozenset()
```

逐行说明：

- `kernel: Kernel` 只写了**类型注解**、没有给默认值。它表示「当前被选中、并已实例化好的那个 `Kernel` 对象」，在 `forward` 里被赋值（例如 `self.kernel = kernel`）。
- `kernel_map` 默认 `None`，等 `dispatch_kernel` 来填。注意它的 value 是 **`Kernel` 类**（工厂），还不是实例。
- `dtype` 默认 `None`；对完全 input-inferred 的算子（如 `GemmOp`），它在第一次 `forward` 时才从输入推断出来。
- `_static_axes` 是一个**不可变集合**（`frozenset`），元素是 `(input_index, axis)` 二元组，表示「在构造期就已经提交（committed）的轴」。它的作用在 4.3 详述，这里先记住它默认为空。

> 为什么用 `frozenset` 而不是 `set`？因为不可变、可哈希，且语义上「已提交的轴」在生命周期内不应被随意改动。

值得单独提一句的是基类的 docstring 示例 [tileops/ops/op_base.py:24-30](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/op_base.py#L24-L30)：它把一个 Op 的「一生」浓缩成了四步——`实例化 → op(Q,K,V) → op.check() → op.profile()`。本讲聚焦前两步（构造与调用），后两步（正确性与性能）分别在 U5、U6/U7 讲。

#### 4.1.4 代码实践

**实践目标**：亲手观察「构造期只填登记表、调用期才填运行时状态」这条时序。

**操作步骤**（需要 CUDA 机器；若无可改为「源码阅读型」，见下）：

1. 实例化一个 `GemmOp`，但**先不调用**它，打印 `op.__dict__`，看哪些属性已经存在、`kernel` 是否还是未设置。
2. 调用一次 `op(a, b)`，再打印 `op.__dict__`，对比多了哪些运行时属性（如 `m/n/k/dtype/kernel`）。

```python
# 示例代码（非项目原有代码）
import torch
from tileops.ops import GemmOp

op = GemmOp()                       # 构造期结束
print("构造后 __dict__:", {k: v for k, v in op.__dict__.items() if k != "_kernel_cache"})
print("kernel_map 已就位?:", op.kernel_map)   # 应是 {"gemm_kernel": <class>, ...}
print("dtype 已知?:", op.dtype)               # 应是 None

a = torch.randn(64, 128, dtype=torch.float16, device="cuda")
b = torch.randn(32, 128, dtype=torch.float16, device="cuda")
out = op(a, b)                      # 第一次调用
print("调用后 dtype:", op.dtype)              # 现在是 torch.float16
print("调用后 m,n,k:", op.m, op.n, op.k)       # 现在是 64, 32, 128
```

**需要观察的现象**：构造后 `kernel_map` 已填充，但 `dtype/m/n/k` 都还是 `None`；调用后这些「运行时状态」才被填上。

**预期结果**：与时序图一致——登记表在构造期就位，运行时状态在调用期就位。

> 待本地验证：如果你没有 CUDA Hopper 机器，无法真正跑 `forward`，可改为阅读 [tileops/ops/gemm.py:45-67](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/gemm.py#L45-L67) 的 `__init__`，自行标注哪些属性在构造期赋值、哪些（`m/n/k/dtype`）被显式初始化为 `None`。

#### 4.1.5 小练习与答案

**练习 1**：`Op.kernel` 只有类型注解、没有默认值，这和 `kernel_map = None` 有什么区别？为什么 `kernel` 不给默认值？

**参考答案**：`kernel_map = None` 是一个真实的类属性，实例可以直接读到 `None`；`kernel: Kernel` 只是类型提示，**不创建属性**，实例在它被赋值前访问会抛 `AttributeError`。`kernel` 不给默认值，恰恰是为了表达「它在调用前根本不存在」这一语义——如果给个 `None`，反而会模糊「未设置」和「设置为 None」两种状态。

**练习 2**：`_static_axes` 为什么用 `frozenset` 而不是 `list` 或普通 `set`？

**参考答案**：「已提交的轴」是一组无序、去重的 `(input_index, axis)` 标识，语义上不应被运行中改动；`frozenset` 不可变、可哈希，能防止误改，也方便作为缓存逻辑的稳定依据。

### 4.2 dispatch_kernel 与 default_kernel_map：Kernel 是怎么「装上去」的

#### 4.2.1 概念说明

构造一个 Op 时，有一件必须做的事：**决定这个 Op 能用哪些 Kernel**。这件事由两个方法协作完成：

- `default_kernel_map`（抽象属性）：子类**声明**「我默认能用哪些 Kernel」，形如 `{"gemm_kernel": GemmKernel, "gemv_kernel": GemvKernel}`。
- `dispatch_kernel`（普通方法）：基类提供的**统一入口**，负责把「默认登记表」与「用户覆盖」合并、做架构兼容校验、最终写到 `self.kernel_map`，并顺手做编译边界注册。

为什么把「声明」和「安装」分开？因为「声明」是子类的个性（GEMM 用 GEMM kernel，softmax 用 softmax kernel），而「安装」的逻辑（合并覆盖、查架构、注册）对所有 Op 都一样，应该由基类统一提供，避免每个子类都抄一遍。

#### 4.2.2 核心流程

```text
op = FooOp(kernel_map=user_override)
        │
        ▼  子类 __init__ 主动调用
self.dispatch_kernel(user_override)
        │
        ├──► self._install_kernel_map(user_override)
        │        │
        │        ├── default = self.default_kernel_map     # 子类声明
        │        ├── 对每个 name：
        │        │     选 user_override[name]（若有），否则用 default[name]
        │        │     校验 kernel.supported_archs 是否含当前 SM 版本
        │        └── 写入 self.kernel_map = resolved
        │
        └──► self._instance_key = register_instance(self)  # 编译边界注册
```

「架构兼容」是这里的重点：每个 `Kernel` 类可以声明 `supported_archs`（如 `[90]` 表示只能在 SM_90 / Hopper 上跑）。安装时，基类会读当前 GPU 的 SM 版本（`get_sm_version()`），如果某个 kernel 不支持当前架构，就**立即抛 `ValueError`**——让问题在构造期暴露，而不是拖到调用期才崩。

#### 4.2.3 源码精读

先看声明侧。`default_kernel_map` 被设计成 **`@property` + `@abstractmethod`**，见 [tileops/ops/op_base.py:74-77](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/op_base.py#L74-L77)：

```python
@property
@abstractmethod
def default_kernel_map(self) -> dict[str, Kernel]:
    raise NotImplementedError("Op must implement default_kernel_map")
```

**为什么是 abstract property？** 因为每个具体 Op **必须**告诉框架自己默认能用哪些 Kernel——这是算子能跑起来的前提，没有合理默认值可以提供。把它设成 `@abstractmethod`，意味着「忘记声明的子类根本无法实例化」，把错误挡在了构造之前；设成 `@property`（而非普通方法），则让使用方用 `op.default_kernel_map`（像属性一样）读取，调用更自然。

再看真实子类的声明。`GemmOp.default_kernel_map` 在 [tileops/ops/gemm.py:68-74](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/gemm.py#L68-L74)：

```python
@property
def default_kernel_map(self) -> Dict[str, Kernel]:
    kernels: Dict[str, Kernel] = {"gemm_kernel": GemmKernel}
    # GemvKernel is SM90-only; only advertise it where it can install.
    if get_sm_version() in (GemvKernel.supported_archs or []):
        kernels["gemv_kernel"] = GemvKernel
    return kernels
```

注意它**不是静态的**——`GemvKernel` 只在 SM_90 上才被登记进去。这正是「声明」也是**运行时」的：它读取了当前 GPU 的能力。

接下来是统一入口 `dispatch_kernel`，见 [tileops/ops/op_base.py:192-197](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/op_base.py#L192-L197)：

```python
def dispatch_kernel(self, kernel_map: Optional[dict[str, Kernel]] = None) -> None:
    """Resolve and install the kernel map (auto-discovery entry point)."""
    self._install_kernel_map(kernel_map)
    # Conforming __init__s all pass through here — the zero-boilerplate
    # registration point for the compile dispatch boundary.
    self._instance_key = register_instance(self)
```

它只做两件事：调 `_install_kernel_map` 装填登记表；调 `register_instance` 注册到编译边界（为 torch.compile 服务，U10 详述）。子类的 `__init__` 只要调一次 `self.dispatch_kernel(kernel_map)` 就完成全部安装——这就是注释里说的「zero-boilerplate registration point」。

`GemmOp.__init__` 正是这样做的，见 [tileops/ops/gemm.py:45-55](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/gemm.py#L45-L55)（关键行是第 55 行 `self.dispatch_kernel(kernel_map)`）。

真正的合并与架构校验在 `_install_kernel_map`，见 [tileops/ops/op_base.py:157-190](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/op_base.py#L157-L190)。核心是这段逐项解析 + 架构检查 [tileops/ops/op_base.py:175-190](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/op_base.py#L175-L190)：

```python
resolved: dict[str, Kernel] = {}
current_arch = get_sm_version()
for name, default_kernel in default_map.items():
    if candidate_map is not None and name in candidate_map:
        kernel_type = candidate_map[name]      # 用户覆盖优先
    else:
        kernel_type = default_kernel
    if (kernel_type is not None
            and kernel_type.supported_archs is not None
            and current_arch not in kernel_type.supported_archs):
        raise ValueError(f'{kernel_type.__name__} is not supported on architecture {current_arch}')
    resolved[name] = kernel_type
self.kernel_map = resolved
```

注释还指出一个设计意图（[op_base.py:163-169](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/op_base.py#L163-L169)）：无论是「自动发现」还是「用户手动覆盖」，都走同一条「校验 + 安装」路径，保证架构兼容检查在两种来源下**行为完全一致**。

最后看一眼「编译边界注册」做了什么——非常薄，见 [tileops/ops/compile_boundary.py:17-29](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/compile_boundary.py#L17-L29)：用一个 `WeakValueDictionary`（弱引用字典）以 `str(id(op))` 为键存下 `op` 自身。本讲只需知道它存在；为什么用弱引用、为什么用字符串键，留到 u10-l1 详解。

#### 4.2.4 代码实践

**实践目标**：在真实源码里标注 `dispatch_kernel` 的调用时机，并理解 `default_kernel_map` 为何必须是 abstract property。

**操作步骤（源码阅读型）**：

1. 在 `tileops/ops/` 下任选三个 `Op` 子类（如 `gemm.py`、`norm/rms_norm.py`、`reduction/softmax.py`），用编辑器/Grep 找到每个 `__init__` 里 `self.dispatch_kernel(...)` 的那一行，确认**每个符合规范的子类都在自己的构造函数里调用了它**。
   - 提示：`RMSNormFwdOp` 在 [tileops/ops/norm/rms_norm.py:61](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/norm/rms_norm.py#L61) 调用。
2. 思考：如果某个子类忘记调 `dispatch_kernel`，会怎样？（提示：`self.kernel_map` 会停在基类的 `None`，`forward` 里取 `self.kernel_map["..."]` 时抛 `TypeError`。）
3. 做一个思想实验：假设把 `default_kernel_map` 从 abstract property 改成普通方法 `def default_kernel_map(self)`，对调用方和使用方各有什么影响？

**需要观察的现象 / 预期结果**：

- 所有合规子类的 `__init__` 中都恰好有一次 `self.dispatch_kernel(kernel_map)` 调用——这就是「统一安装点」。
- `default_kernel_map` 若改成普通方法，使用方就得写 `op.default_kernel_map()`（多一对括号），更重要的是 abstract property 的「强制实现」语义不变；选 property 主要是为了**读起来像数据**而非动作。

**待本地验证**：如果你想确认架构校验真的会报错，可尝试用一个显式声明 `supported_archs` 且不含当前 SM 版本的 Kernel 去构造 Op（仅在能改测试代码的前提下）。

#### 4.2.5 小练习与答案

**练习 1**：`dispatch_kernel` 为什么放在基类、而不是让每个子类各自实现「合并覆盖 + 架构校验」？

**参考答案**：因为这套逻辑对所有 Op 完全相同（拿默认表、套用户覆盖、查架构、写回 `kernel_map`），集中到基类可以避免重复、保证行为一致（尤其是架构校验不会因子类实现差异而漏检），并让子类 `__init__` 只需一行 `self.dispatch_kernel(kernel_map)`。

**练习 2**：`GemmOp.default_kernel_map` 为什么要在「读当前 SM 版本」后再决定是否登记 `GemvKernel`，而不是无条件登记？

**参考答案**：`GemvKernel` 是 SM_90 专用的。如果无条件登记它，那么在非 SM_90 机器上，`_install_kernel_map` 的架构校验会立即抛 `ValueError`，导致 `GemmOp` 在这些机器上根本无法构造。先按当前架构过滤，就能让「声明」与「能装」保持一致。

### 4.3 forward、__call__ 与缓存键：一次调用是怎么走的

#### 4.3.1 概念说明

安装好登记表后，Op 就等着被调用了。可调用契约的核心是：

- `__call__`：基类提供，**透明转发**给 `forward`，让 `op(a, b)` 等价于 `op.forward(a, b)`。
- `forward`：抽象方法，每个子类**必须**实现，里面写「校验 → 推断 → 选 kernel → 调 kernel → 返回输出」的真实逻辑。

基类本身只规定「`forward` 必须存在」，并不规定它内部怎么写。但 TileOPs 的子类普遍遵循一个模式——**快路径（fast path）**：把「上一次调用的输入特征」记下来，下次如果输入特征没变，就跳过校验/推断/选 kernel，直接复用上次的 kernel。这在「同形状反复调用」的基准测试和服务场景下能省掉可观的 Python 开销。

与「选 kernel」紧密相关的是 `_cache_key`：它回答「**什么样的两次调用可以共用同一个已 JIT 编译的 kernel**」。`Op` 给了一个默认实现，并配合 `_static_axes` 工作。

#### 4.3.2 核心流程

```text
out = op(a, b)
   │
   ▼
Op.__call__(a, b)            # 基类：op_base.py:210-212
   └──► return self.forward(a, b)
                │
                ▼ （子类实现，例如 GemmOp.forward）
        sig = (a.shape, b.shape, a.dtype)
        if sig != self._active_sig:        # 特征变了？
            self._validate_dtypes(a, b)    #   校验 dtype
            m,n,k = self._infer_mnk(a,b)   #   推断
            mode,kernel = self._get_kernel(m,n,k,dtype)   # 选/建/缓存 kernel
            self._active = (mode, kernel, n, m)
            self._active_sig = sig
        # 快路径：直接复用 self._active 里的 kernel
        mode, kernel, n, m = self._active
        return kernel(a, b)
```

`_get_kernel` 内部会用一个「形状 → Kernel 实例」的字典做缓存。决定「形状」如何参与缓存的关键，就是 `_cache_key`。默认实现在 [tileops/ops/op_base.py:214-250](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/op_base.py#L214-L250)，它把所有「**未在构造期提交的轴**」的尺寸拼成 key：

```text
cache_key = 所有 (input_index, axis) 中、不属于 _static_axes 的那些维度的尺寸
```

换句话说，构造期已提交的轴（`_static_axes`）被**排除**在 key 之外——因为它们的尺寸在构造期就已固定，没必要再参与「每次调用」的区分。

#### 4.3.3 源码精读

先看可调用契约本身，[tileops/ops/op_base.py:206-212](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/op_base.py#L206-L212)：

```python
@abstractmethod
def forward(self, *args: object, **kwargs: object) -> Union[torch.Tensor, tuple]:
    raise NotImplementedError("forward method is not implemented")

def __call__(self, *args: object, **kwargs: object) -> Union[torch.Tensor, tuple]:
    """Make the op callable - delegates to forward()"""
    return self.forward(*args, **kwargs)
```

`forward` 是 `@abstractmethod`——没有它的子类无法实例化；`__call__` 是普通方法，原样转发。这就是「可调用契约」的全部基类实现。

然后看真实子类的快路径。`GemmOp.forward` 在 [tileops/ops/gemm.py:121-146](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/gemm.py#L121-L146)，关键开头 [tileops/ops/gemm.py:126-139](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/gemm.py#L126-L139)：

```python
sig = (a.shape, b.shape, a.dtype)
if sig != self._active_sig:
    self._validate_dtypes(a, b)
    m, n, k = self._infer_mnk(a, b)
    ...
    mode, kernel = self._get_kernel(m, n, k, a.dtype)
    self.kernel = kernel
    self._active = (mode, kernel, n, m)
    self._active_sig = sig

mode, kernel, n, m = self._active        # 快路径
...
```

注释（[gemm.py:122-125](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/gemm.py#L122-L125)）说得很直白：在「同形状反复调用」的基准/服务场景里，这一跳过能省掉 dtype 校验、形状推断和缓存查找。

> 注意：`GemmOp` 并没有直接复用基类的 `_cache_key`，而是**自己覆盖**了它（[tileops/ops/gemm.py:88-91](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/gemm.py#L88-L91)），把形状投影到 kernel 真正依赖的 `(m, n, k, trans_a, trans_b, dtype)`：
>
> ```python
> def _cache_key(self, *input_shapes: Tuple[int, ...]) -> Hashable:
>     return (self.m, self.n, self.k, self.trans_a, self.trans_b,
>             None if self.dtype is None else str(self.dtype))
> ```
>
> 这正是基类 docstring 建议的做法——「投影到 kernel 数学上真正依赖的维度」。u2-l3 会专门讲它如何减少重复 JIT 编译。

那么基类默认的 `_cache_key` 长什么样？见 [tileops/ops/op_base.py:231-250](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/op_base.py#L231-L250)：

```python
if not self._static_axes and type(self)._cache_key is Op._cache_key:
    # ... emit UserWarning once per subclass ...
return tuple(
    s
    for i, shape in enumerate(input_shapes)
    for axis, s in enumerate(shape)
    if (i, axis) not in self._static_axes
)
```

两件事：

1. 它**警告**：如果某子类既没设 `_static_axes`、也没覆盖 `_cache_key`，那默认 key 会等于「完整输入形状」，导致「每个新形状重新编译一次」（cache 过度碎片化）。这个警告每个子类只触发一次。
2. 它**计算**：遍历每个输入的每一维，丢掉 `_static_axes` 里登记的轴，剩下的尺寸组成 key。

谁在用这套机制？`SoftmaxFwdOp` 家族是一个范本。它在 `forward` 里根据 `dim` **动态绑定** `_static_axes`：单维路径见 [tileops/ops/reduction/softmax.py:166-168](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/reduction/softmax.py#L166-L168)，多维路径见 [tileops/ops/reduction/softmax.py:128-131](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/reduction/softmax.py#L128-L131)。注释点明：因为 reduction 轴是「参数相关」（依赖运行时 `dim`），所以 `_static_axes` 必须在 forward 里、`dim` 归一化之后才绑定，而不是写在类级别。

#### 4.3.4 代码实践

**实践目标**：解释 `_cache_key` 默认实现的含义，并对比「默认实现」与「GemmOp 覆盖版」的差异。

**操作步骤（源码阅读型）**：

1. 读 [tileops/ops/op_base.py:214-250](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/op_base.py#L214-L250) 的 docstring，用自己的话写下「默认 `_cache_key` 返回什么」。
2. 读 [tileops/ops/gemm.py:88-91](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/gemm.py#L88-L91) 的覆盖版，回答：对于 `a=[M,K]`、`b=[N,K]`，默认实现会返回几个数？覆盖版返回几个？为什么覆盖版能让 `(M=64,K=128)` 和 `(M=64,K=256)` 命中不同 key（正确），却又不会因为某个无关维度而过度碎片化？

**需要观察的现象 / 预期结果**：

- 默认实现返回「所有非提交轴的尺寸」，对 2D 的 `(M,K)`、`(N,K)` 且 `_static_axes` 为空时，返回 `(M, K, N, K)` 共 4 个数（且 K 出现两次）。
- 覆盖版返回 `(M, N, K, trans_a, trans_b, dtype)`，把 trans 布局与 dtype 也纳入，因为它们会影响 kernel 选哪个、编译成什么。
- 默认实现「永远正确但可能过度碎片化」；覆盖版「投影到 kernel 真正依赖的量」，更省编译。

> 待本地验证：若想看到那条 `UserWarning`，可构造一个既没设 `_static_axes`、又没覆盖 `_cache_key`、并且真的调用了 `_cache_key` 的最小 Op（仅作教学），观察「每个子类只警告一次」的行为。

#### 4.3.5 小练习与答案

**练习 1**：基类的 `__call__` 只是 `return self.forward(...)`。既然如此，为什么不直接让用户写 `op.forward(a, b)`，而要绕一层 `__call__`？

**参考答案**：`__call__` 让 Op 实例「像函数一样」被调用，即 `op(a, b)`。这在 API 美感和与 PyTorch 习惯（`nn.Module` 也是 `model(x)`）上一致；同时 `forward` 作为独立名字，方便子类内部、测试代码显式调用，或在 `__call__` 里将来加入钩子（如统一日志/计时）而不破坏调用方。

**练习 2**：默认 `_cache_key` 为什么在 `_static_axes` 为空、且子类未覆盖时发警告，而不是直接报错？

**参考答案**：默认实现「功能上永远正确」（不会算错结果），只是会让 kernel 缓存过度碎片化（每个新形状编译一次），属于性能隐患而非正确性错误。发一次性 `UserWarning` 足以提醒开发者去覆盖，同时不至于阻断那些「形状变化本就不大、碎片化无所谓」的合法用法。

### 4.4 三个 codegen 契约方法与 staged-rollout 状态

#### 4.4.1 概念说明

除了 `default_kernel_map` 和 `forward`，`Op` 基类还**声明**了三个「契约方法」：

| 方法 | 契约（应返回/做什么） | 用途 |
| --- | --- | --- |
| `_validate_dtypes(*tensors)` | 校验输入张量的 dtype 是否合法，不合法抛错 | 每次 `forward` 调用前 |
| `_infer_output_shapes(**shape_kwargs)` | 根据输入形状推出输出形状 dict | 形状规约 / fake 张量 |
| `eval_roofline()` | 返回 `(flops, bytes)` 二元组 | 性能模型（SOL 效率） |

这三个方法有一个特殊之处：在「完全实现（implemented）」的算子上，它们的函数体**不是手写的**，而是由 TileOPs 的**代码生成（codegen）**从 manifest 规约自动合成的（U8 详述）。也就是说，manifest 是「唯一真相来源」，代码自动服从它。

但项目目前处于一个**过渡期**：这三个契约在基类里还**没有**用 `@abstractmethod` 强制，而是抛 `NotImplementedError` 的 stub。代码里用 `FIXME(staged-rollout)` 标记块解释了原因——这正是本讲学习目标里要你「认识」的 staged-rollout 状态。

#### 4.4.2 核心流程

```text
# 1) 子类被定义的那一刻
class FooOp(Op): ...
        │
        ▼
Op.__init_subclass__(FooOp)           # op_base.py:57-72
   ├── 懒导入 codegen 模块
   ├── maybe_install_validator(FooOp)         # 尝试合成 _validate_dtypes
   └── maybe_install_eval_roofline(FooOp)     # 尝试合成 eval_roofline
        │
        │  规则（来自 docstring）：
        │   - 子类未声明 manifest 元数据 → 跳过（no-op）
        │   - 子类自己提供了覆盖 → 跳过
        │   - status: spec-only → 跳过（保留 stub）
        │   - 否则 → 合成纯 Python 方法体
        ▼
# 2) 运行时
op._validate_dtypes(a, b)     # forward 内调用，走合成版或子类手写版
op.eval_roofline()            # 性能评测时调用
```

关键点：codegen 发生在**类定义时**（`__init_subclass__`），不是每次调用时——所以合成的就是普通 Python 方法，没有任何运行时 `eval`/解析（这是 manifest 设计的安全边界，U8 详述）。

#### 4.4.3 源码精读

自动装配的入口是 `__init_subclass__`，[tileops/ops/op_base.py:57-72](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/op_base.py#L57-L72)：

```python
def __init_subclass__(cls, **kwargs: object) -> None:
    super().__init_subclass__(**kwargs)
    from tileops.ops._dtype_codegen import maybe_install_validator
    from tileops.ops._roofline_codegen import maybe_install_eval_roofline
    maybe_install_validator(cls)
    maybe_install_eval_roofline(cls)
```

两个细节：

- **懒导入**：`from tileops.ops._dtype_codegen import ...` 写在函数体里而不是文件顶部。docstring（[op_base.py:64-67](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/op_base.py#L64-L67)）说明这是为了**避免在 `Op` 定义时就触发循环导入**。
- 两个 `maybe_install_*` 函数都是「有就装、没有就静默返回」，符合上面流程图里的四条跳过规则。

再看三个契约的 stub。它们结构高度一致，以 `eval_roofline` 为例，[tileops/ops/op_base.py:127-155](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/op_base.py#L127-L155)。docstring（[op_base.py:127-135](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/op_base.py#L127-L135)）强调了一个重要设计：合成出来的 `eval_roofline` 是「直接操作 `self.*` 属性的纯 Python」，**L1 基类故意不提供任何通用的 roofline 表达式求值器**（即 roofline.md §4.4.6 的 "Evaluator Surface Boundary"）。

每个 stub 都带一个 `FIXME(staged-rollout)` 块。以 `eval_roofline` 的为例，[tileops/ops/op_base.py:136-155](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/op_base.py#L136-L155)：

```python
# FIXME(staged-rollout): L1 Op does not yet strictly enforce eval_roofline
# via @abstractmethod; base raises NotImplementedError instead.
#
# Broken invariant: L1 base does not strictly enforce implementation
#     of eval_roofline on every concrete Op subclass.
# Why: Introducing @abstractmethod now would break every existing
#     concrete op under tileops/ops/ (none of them ship an
#     eval_roofline yet). ...
# Cleanup: once all concrete ops ... implement eval_roofline (via codegen
#     emission per docs/design/roofline.md §4.4), convert this stub ...
#     to `@abstractmethod`.
raise NotImplementedError(...)
```

这套 `FIXME(staged-rollout)` 注释块是项目规范（见 `.claude/rules/code-style.md`）。它有固定四段：

- **一句话摘要**：现在还没用 `@abstractmethod` 强制，而是抛 `NotImplementedError`。
- **Broken invariant**：被破坏的不变量——「L1 基类目前没有强制每个具体 Op 都实现该方法」。
- **Why**：为什么现在不能强制——「改成 `@abstractmethod` 会立刻让所有还没迁移的具体 Op 都无法实例化」，而信任模型要求每个 Op 单独开 PR 迁移。
- **Cleanup**：什么时候清理——「等所有具体 Op 都实现了这三个方法后，就把这些 stub 转成 `@abstractmethod`」。

`_validate_dtypes` 与 `_infer_output_shapes` 的 stub 形态完全一致，分别见 [tileops/ops/op_base.py:104-125](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/op_base.py#L104-L125) 与 [tileops/ops/op_base.py:88-102](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/op_base.py#L88-L102)。

作为对比，看一个**已经手写**了契约的子类。`GemmFp8Op._validate_dtypes` 是手写的（[tileops/ops/gemm.py:197-215](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/gemm.py#L197-L215)），而 `RMSNormFwdOp.eval_roofline` 也是手写的（[tileops/ops/norm/rms_norm.py:76-84](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/norm/rms_norm.py#L76-L84)）。这说明现状是「**部分手写、部分 codegen、部分还是 stub**」的混合态——这正是 staged-rollout 的字面含义。

#### 4.4.4 代码实践

**实践目标**：理解三个 codegen 契约的 staged-rollout 状态，并能在源码里分辨「stub」「手写版」「codegen 合成版」。

**操作步骤（源码阅读型）**：

1. 在 `tileops/ops/` 全目录用 Grep 搜索 `def eval_roofline`，列出哪些 Op **自己实现了** `eval_roofline`（这些是手写版，例如 `rms_norm.py`）。
2. 对比：基类 stub 在 [op_base.py:151-155](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/op_base.py#L151-L155) 抛 `NotImplementedError`；如果你 grep 不到某 Op 自己的 `eval_roofline`，又没被 codegen 合成，那它调用时就会触发这个 stub。
3. 读任意一个 `FIXME(staged-rollout)` 块（上面已给出 `eval_roofline` 的），确认它包含「一句话摘要 / Broken invariant / Why / Cleanup」四段。
4. 思考：为什么项目不直接「一口气把所有 Op 都迁移好、再把 stub 改成 `@abstractmethod`」？

**需要观察的现象 / 预期结果**：

- 实现了 `eval_roofline` 的 Op 是少数（手写先行），多数仍依赖 stub 或未来的 codegen。
- `FIXME(staged-rollout)` 块结构统一、措辞规范，便于日后 `grep -rn 'FIXME(staged-rollout)'` 批量清理。

**待本地验证**：若你有 CUDA 机器，可对一个尚未实现 `eval_roofline` 的 Op 在 `forward` 之后调用 `op.eval_roofline()`，观察它是否抛出 `NotImplementedError`（指向 roofline.md §4.4 / §4.4.6）。

#### 4.4.5 小练习与答案

**练习 1**：三个契约方法为什么现在不是 `@abstractmethod`？这带来了什么好处和代价？

**参考答案**：因为把它们改成 `@abstractmethod` 会立刻让所有「还没迁移」的具体 Op 都无法实例化，打断正常开发。代价是 L1 基类无法在实例化时强制每个 Op 都实现它们（即注释里的 Broken invariant），只能在运行时抛 `NotImplementedError`；好处是迁移可以「逐 Op、逐 PR」推进，符合项目的信任模型。Cleanup 条件是「所有具体 Op 都实现后」再升级为 `@abstractmethod`。

**练习 2**：`__init_subclass__` 里为什么要用「懒导入」（把 `import` 写在函数体里）？

**参考答案**：`op_base.py` 是被很多模块依赖的底层文件，而 `_dtype_codegen` / `_roofline_codegen` 反过来可能依赖 `Op` 或 ops 包的其他内容。如果在文件顶部直接 import，会在 `Op` 定义尚未完成时就触发导入链，造成循环导入。把 import 推迟到 `__init_subclass__` 被调用时（即第一个子类定义时），此时 `Op` 已定义完毕，循环被打破。

**练习 3**：为什么 `eval_roofline` 的合成体被要求是「直接操作 `self.*` 的纯 Python」，而不允许一个「通用 roofline 求值器」？

**参考答案**：合成发生在类定义时，结果是确定的纯 Python 方法，没有运行时 `eval`/解析——既安全（不受 manifest 内容当代码执行的风险），又好静态分析、好调试。一个「通用求值器」会把 manifest 当数据在运行时解释执行，违反 roofline.md §4.4.6 的 "Evaluator Surface Boundary"。这一设计的内部细节是 U8 的主题。

## 5. 综合实践

把本讲学的「构造期 dispatch / 调用期 forward / codegen 契约」串起来，完成下面这个**源码跟踪任务**：

**任务**：选择 `tileops/ops/gemm.py` 中的 `GemmOp`，画一张「`op = GemmOp()` 到 `out = op(a, b)` 再到 `flops, bytes = op.eval_roofline()`」的**完整时序图**，并在图上标注以下信息：

1. **构造期**：`__init__` 在哪一行调用 `dispatch_kernel`？`dispatch_kernel` 内部依次调用了哪两个方法/函数？`self.kernel_map` 被填成了什么（用 `default_kernel_map` 的真实返回值说明 `GemvKernel` 为何只在 SM_90 出现）？
2. **第一次调用**：`forward` 里的 `_active_sig` 判断为「不相等」时，依次执行了哪几步（校验、推断、选 kernel、缓存）？此时 `self.kernel` / `self.dtype` / `self.m/n/k` 从何而来？
3. **第二次同形状调用**：快路径跳过了哪些步骤？`self._active` 复用了什么？
4. **性能查询**：`GemmOp` 的 `eval_roofline` 是手写、codegen 合成、还是仍是 stub？给出你的判断依据（提示：在 `gemm.py` 里 grep `eval_roofline`）。

**验收标准**：

- 时序图能清晰区分「构造期 / 第一次调用 / 快路径」三个阶段。
- 每个标注都引用了真实文件与行号（permalink）。
- 对 `eval_roofline` 的来源给出了有依据的判断（而不是猜测）。

> 提示：本任务不需要 CUDA 机器，全部可以通过阅读 `op_base.py` 与 `gemm.py` 完成；`eval_roofline` 的判断则可能需要结合 U8 的 codegen 知识——若无法确定，请标注「待确认」并说明你查阅了哪些线索。

## 6. 本讲小结

- `Op` 的类属性分为**登记表类**（`kernel_map`，构造期填充）和**运行时状态类**（`kernel`/`dtype`/`input_shapes`，调用期填充）；`_static_axes` 记录构造期已提交的轴。
- Kernel 安装走 **`dispatch_kernel → _install_kernel_map`**：子类用 `default_kernel_map`（abstract property）声明默认 kernel，基类统一合并用户覆盖、做架构兼容校验、写回 `self.kernel_map`，并顺手注册到编译边界。
- 可调用契约是 **`__call__` 透明转发给抽象的 `forward`**；真实子类普遍用 `_active_sig` 实现「同形状复用」的快路径。
- `_cache_key` 决定「哪些调用共用一个已编译 kernel」；基类默认实现用 `_static_axes` 排除已提交轴，并在「未设 `_static_axes` 且未覆盖」时一次性警告，提示过度碎片化风险。
- 三个 **codegen 契约**（`_validate_dtypes` / `_infer_output_shapes` / `eval_roofline`）由 `__init_subclass__` 在类定义时尝试自动装配；目前处于 **staged-rollout** 过渡期——基类用抛 `NotImplementedError` 的 stub 加 `FIXME(staged-rollout)` 块，而非 `@abstractmethod`，以便逐 Op 迁移。
- staged-rollout 注释块有固定四段（摘要 / Broken invariant / Why / Cleanup），便于日后 `grep` 批量清理。

## 7. 下一步学习建议

本讲把 `Op` 基类的「骨架」讲完了。建议按以下顺序继续：

1. **u2-l2 Kernel 选择与架构兼容性**：深入 `_install_kernel_map` 的架构校验、`supported_archs` 的运行时过滤，以及用户用 `kernel_map` 参数 override 默认 kernel 的完整路径。
2. **u2-l3 形状推断与 Kernel 缓存**：聚焦 `_cache_key` 与 `_static_axes`，理解 lazy build（首次 forward 构造并缓存）与 eager 构造的区别，以及 fixed-rank / arbitrary-rank 的时序差异。
3. **u2-l4 跟读 GemmOp 完整链路**：把本讲的骨架套到 `GemmOp` 上，走一遍 `_infer_mnk`、GEMV 快路径、`_active_sig` 缓存的真实代码。
4. 若你对 codegen 已经好奇，可以先跳读 `tileops/ops/_dtype_codegen.py` 与 `_roofline_codegen.py`，但系统讲解在 **U8**（manifest 驱动的代码生成）。
