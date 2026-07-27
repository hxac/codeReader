# 算子的公开 API 与调用方式

## 1. 本讲目标

上一讲（u1-l2）你已经用 `GemmOp` 跑通了第一个算子，对「实例化 + 调用」有了手感。本讲把这种手感**正式化为契约**，让你在面对仓库里 180 多个算子时，知道三件事：

1. **去哪找、怎么导**：`tileops.ops` 这个包是算子的唯一公开入口，所有算子类都从这里聚合导出。
2. **怎么调用**：每个算子都是一个「可调用对象（callable）」，`op(*inputs)` 等价于 `op.forward(*inputs)`，这是所有算子统一的调用契约。
3. **形状与 dtype 何时确定**：TileOPs 遵循 **input-inferred**（输入推断）原则——多数算子的形状与 dtype 在**调用时**才从输入张量推断，而不是在**构造时**绑定。

学完本讲，你应当能：从 `tileops.ops` 导入任意算子类，正确实例化它，调用它，并用 PyTorch 作为「地面真值（ground truth）」验证结果，同时说清楚哪些维度是构造期提交的、哪些是调用时推断的。

## 2. 前置知识

本讲建立在前三讲之上，默认你已经理解：

- **Op / Kernel 双层分离**（u1-l1）：`Op`（L2，主机侧 Python 入口，负责校验、布局、`torch.compile` 兼容）与 `Kernel`（L1，TileLang GPU 实现）边界严格。本讲只和 `Op` 层打交道，`Kernel` 是黑盒。
- **首次运行**（u1-l2）：会 `make install`、能用 `GemmOp()` 跑 GEMM、知道 NT 布局（`trans_b=True`，`gemm(a, b)` 数学上等于 `a @ b.T`）、知道用 PyTorch 当地面真值。
- **目录与模块全景**（u1-l3）：`tileops/ops/` 对应 M2（实现），`tileops/manifest/` 是规约的唯一真相来源。

一个本讲要用到的 Python 基础概念：**可调用对象（callable）**。在 Python 里，只要一个类定义了 `__call__` 魔术方法，它的实例就可以像函数一样被「调用」——`obj(x)` 会被翻译成 `obj.__call__(x)`。TileOPs 的每个 `Op` 都是可调用对象，这是统一调用契约的基础。

> 术语：本文用「构造期（construction）」指 `Op(...)` 实例化那一刻，用「调用时（call time / forward time）」指 `op(*inputs)` 执行那一刻。区分这两个时间点是理解 input-inferred 的关键。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [`tileops/ops/__init__.py`](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/__init__.py) | 算子的**公开导出聚合**：从各子模块收集所有 `Op` 类，统一对外暴露 | §4.1 |
| [`tileops/ops/op_base.py`](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/op_base.py) | `Op` 抽象基类：定义 `__call__ → forward` 契约、`dispatch_kernel`、`default_kernel_map` | §4.2 |
| [`tileops/ops/gemm.py`](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/gemm.py) | `GemmOp` 与 `GemmFp8Op` 的实现 | §4.2、§4.3 |
| [`tileops/ops/norm/rms_norm.py`](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/norm/rms_norm.py) | `RMSNormFwdOp`：构造期需提交 `dtype` 的反例 | §4.3、§4.4 |
| [`tileops/ops/reduction/softmax.py`](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/reduction/softmax.py) | `SoftmaxFwdOp`：形状/dtype 完全调用时推断的典型 | §4.3、§4.4 |
| [`README.md`](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/README.md) | Quick Start 示例 | §4.2 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**ops 导出聚合**（§4.1）、**可调用契约**（§4.2）、**input-inferred 调用模式**（§4.3）。§4.4 给出贯穿前两者的代码实践。

### 4.1 ops 导出聚合：找到你要的算子

#### 4.1.1 概念说明

TileOPs 有 180 多个算子，分散在 `tileops/ops/` 下的十几个子模块（`gemm.py`、`norm/`、`reduction/`、`attention/`、`moe/`……）。如果你要直接从子模块导入，得先知道某个算子住在哪个文件——这很累。

于是 `tileops/ops/__init__.py` 充当了**单一聚合入口（aggregation surface）**：它把所有算子类从各自的子模块收集上来，重新导出。用户只需要写一行：

```python
from tileops.ops import GemmOp, RMSNormFwdOp, SoftmaxFwdOp
```

而不需要关心它们到底定义在 `gemm.py`、`norm/rms_norm.py` 还是 `reduction/softmax.py`。这是 Python 库设计的常见模式（`__init__.py` 作为包的「门面」）。

#### 4.1.2 核心流程

聚合的建立遵循一个固定的三步模式：

1. **按家族分组的导入语句**：每条 `from .子模块 import OpClass1, OpClass2` 把一组相关算子拉进来。
2. **`__all__` 白名单**：一个字符串列表，显式声明「这个包对外暴露哪些名字」。`__all__` 的作用有二：一是 `from tileops.ops import *` 时只导入列表里的名字；二是给 IDE / 文档工具一个明确的公开 API 清单。
3. **加载即注册**：因为 `__init__.py` 在 `import tileops.ops` 时整体执行，导入一个算子类的同时也会触发该类的 `__init_subclass__`（见 u8 进阶内容，本讲只需知道它会按 manifest 自动装配一些方法）。

#### 4.1.3 源码精读

先看导出聚合文件的开头，每个家族都是一组导入：

从 gemm 家族导入 `GemmFp8Op` 和 `GemmOp`（[tileops/ops/__init__.py:47](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/__init__.py#L47)）：

```python
from .gemm import GemmFp8Op, GemmOp
```

从 norm 家族导入一整批归一化算子，`RMSNormFwdOp` 在其中（[tileops/ops/__init__.py:53-64](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/__init__.py#L53-L64)）：

```python
from .norm import (
    AdaLayerNormFwdOp,
    ...
    RMSNormFwdOp,
)
```

从 reduction 家族导入约 20 个归约算子，本讲实践要用的 `SoftmaxFwdOp` 也在内（[tileops/ops/__init__.py:79-103](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/__init__.py#L79-L103)）：

```python
from .reduction import (
    AllFwdOp,
    ...
    SoftmaxFwdOp,
    ...
)
```

注意 line 65 还导出了基类本身 `Op`，方便需要继承扩展的开发者（[tileops/ops/__init__.py:65](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/__init__.py#L65)）：

```python
from .op_base import Op
```

随后 `__all__` 把这些名字汇成白名单，`"GemmOp"`、`"RMSNormFwdOp"`、`"SoftmaxFwdOp"`、`"Op"` 都在其中（[tileops/ops/__init__.py:118-233](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/__init__.py#L118-L233)）。这就构成了 `tileops.ops` 的全部公开 API。

> 观察：`__init__.py` 里 reduction 那段有几行被注释掉的 `# "CummaxOp",`、`# "ReduceMaxOp",`（[tileops/ops/__init__.py:212-227](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/__init__.py#L212-L227)）。这说明项目处于活跃开发中，部分算子尚未「上岸」，公开 API 是逐 PR 增长的。看到这种注释，说明对应算子还没到 implemented 状态。

#### 4.1.4 代码实践

**实践目标**：学会用工具发现公开 API，而不是死记。

**操作步骤**（待本地验证，需 GPU + 已安装环境）：

```python
import tileops.ops as t

# 1. 直接看公开 API 有多大
names = [n for n in dir(t) if not n.startswith("_")]
print("公开算子/符号数:", len(names))

# 2. 按关键词筛选你想找的算子家族
print("含 'Norm' 的:", [n for n in names if "Norm" in n])
print("含 'Attention' 的:", [n for n in names if "Attention" in n])
print("含 'Pool' 的:", [n for n in names if "Pool" in n])

# 3. 对照 __all__ 确认它们真的对外暴露
print("SoftmaxFwdOp 在 __all__ 中:", "SoftmaxFwdOp" in t.__all__)
```

**需要观察的现象**：`dir(t)` 列出的名字应当与 `__all__` 基本一致（额外含少量模块级常量）；按关键词能快速定位到家族。

**预期结果**：含 `Norm` 的名字会返回 `RMSNormFwdOp`、`LayerNormFwdOp`、`BatchNormFwdOp` 等十来个；含 `Pool` 的会返回 `MaxPool1dFwdOp`…`AvgPool3dFwdOp` 等。

#### 4.1.5 小练习与答案

**练习 1**：为什么推荐 `from tileops.ops import GemmOp`，而不是 `from tileops.ops.gemm import GemmOp`？

> **答案**：前者走公开聚合入口，是稳定的公开 API，未来即便 `GemmOp` 被搬到别的文件，`__init__.py` 的重新导出仍保持不变；后者依赖内部文件布局，重构时会断。

**练习 2**：怎么判断一个算子（如 `CummaxOp`）当前是否对用户开放？

> **答案**：看它是否出现在 `tileops.ops.__all__` 中。若只在源码里以注释形式存在（如 `# "CummaxOp",`），说明尚未实现，未对外暴露。

---

### 4.2 可调用契约：实例化 + `__call__` → `forward`

#### 4.2.1 概念说明

「可调用契约」是 TileOPs 对所有算子统一的**调用约定**。它由三句话概括：

1. **先实例化，再调用**：`op = SomeOp(构造参数)`，然后 `out = op(输入张量...)`。构造参数描述「这个算子长什么样」（如 GEMM 的转置布局、归一化的 `normalized_shape`），输入张量是「这次要算的数据」。
2. **`op(*inputs)` 等价于 `op.forward(*inputs)`**：`Op` 基类实现了 `__call__`，它只是把调用原样转发给 `forward`。用户写哪种都行，但**惯例是直接 `op(...)`**。
3. **`forward` 是每个算子必须实现的抽象方法**：基类只规定签名占位（`forward(self, *args, **kwargs)`），真正的执行逻辑由每个具体算子（`GemmOp`、`RMSNormFwdOp`…）提供。

这套契约的好处是**一致性**：不管你用哪个算子，套路都是「构造 + 调用」，不必每个算子学一套新接口。

#### 4.2.2 核心流程

一次 `op(*inputs)` 的执行过程（以 `GemmOp` 为例）：

```text
op = GemmOp(trans_b=True)        # 1. 构造：dispatch_kernel 安装 kernel_map
d = op(a, b)                     # 2. 调用：触发 __call__
        │
        ▼
Op.__call__(a, b)                # 3. 基类把调用转发给 forward
        │
        ▼
GemmOp.forward(a, b)             # 4. 子类执行：校验 dtype → 推断 m,n,k
        │                           → 选 kernel（首次会 JIT 编译并缓存）
        ▼
kernel(a, b)                     # 5. 真正的 GPU 计算（L1 层，本讲黑盒）
        │
        ▼
d : torch.Tensor([M, N])         # 6. 返回输出张量
```

构造期与调用期的分工很清晰：构造期**安装 kernel_map**（声明有哪些可选 kernel），调用期**真正选并编译 kernel**（根据具体形状/dtype）。

#### 4.2.3 源码精读

契约的源头在 `Op` 基类。先看最关键的两行——可调用契约的全部秘密（[tileops/ops/op_base.py:210-212](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/op_base.py#L210-L212)）：

```python
def __call__(self, *args: object, **kwargs: object) -> Union[torch.Tensor, tuple]:
    """Make the op callable - delegates to forward()"""
    return self.forward(*args, **kwargs)
```

这就是「`op(...)` 等价于 `op.forward(...)`」的实现：`__call__` 只是一个透明转发。

而 `forward` 本身是抽象方法，基类不提供实现，留给子类（[tileops/ops/op_base.py:206-208](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/op_base.py#L206-L208)）：

```python
@abstractmethod
def forward(self, *args: object, **kwargs: object) -> Union[torch.Tensor, tuple]:
    raise NotImplementedError("forward method is not implemented")
```

再看构造期的统一入口 `dispatch_kernel`，它是所有 `Op` 子类构造函数必须经过的「关卡」（[tileops/ops/op_base.py:192-197](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/op_base.py#L192-L197)）：

```python
def dispatch_kernel(self, kernel_map: Optional[dict[str, Kernel]] = None) -> None:
    """Resolve and install the kernel map (auto-discovery entry point)."""
    self._install_kernel_map(kernel_map)
    # Conforming __init__s all pass through here — the zero-boilerplate
    # registration point for the compile dispatch boundary.
    self._instance_key = register_instance(self)
```

它做两件事：调用 `_install_kernel_map` 把「默认 kernel 表」与「用户 override」合并校验并安装到 `self.kernel_map`；再把当前实例注册到编译边界（u10 内容，本讲略）。`default_kernel_map` 则是一个抽象 property，要求每个子类声明自己默认用哪些 kernel（[tileops/ops/op_base.py:74-77](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/op_base.py#L74-L77)）。

现在看 `GemmOp` 如何落实这套契约。构造函数（[tileops/ops/gemm.py:45-66](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/gemm.py#L45-L66)）保存布局参数，并调用 `dispatch_kernel` 完成安装：

```python
def __init__(self, trans_a: bool = False, trans_b: bool = True, ...) -> None:
    self.trans_a = trans_a
    self.trans_b = trans_b
    self._tune = tune
    self.dispatch_kernel(kernel_map)          # ← 必经关卡
    self._kernel_cache: Dict[Hashable, Kernel] = {}
    ...
    self.dtype: Optional[torch.dtype] = None  # ← 注意：构造期 dtype 为空
```

`forward` 的核心是**快路径缓存**：用输入签名 `(a.shape, b.shape, a.dtype)` 与上次比对，相同就跳过校验与推断，直接复用上次的 kernel（[tileops/ops/gemm.py:121-146](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/gemm.py#L121-L146)）：

```python
def forward(self, a: torch.Tensor, b: torch.Tensor) -> torch.Tensor:
    sig = (a.shape, b.shape, a.dtype)
    if sig != self._active_sig:               # 首次或形状变了：走完整路径
        self._validate_dtypes(a, b)
        m, n, k = self._infer_mnk(a, b)
        ...
        mode, kernel = self._get_kernel(m, n, k, a.dtype)
        self._active = (mode, kernel, n, m)
        self._active_sig = sig
    mode, kernel, n, m = self._active         # 稳态：直接复用
    ...
    return kernel(a, b)
```

> 为什么需要快路径？在推理 / 基准测试里，同一个算子会被几万次用相同形状调用。若每次都重新推断形状、查缓存表，Python 层开销会累积。快路径把「形状没变」这个最常见情形压缩成一次元组比较 + 一次解包。这是 u2 会深入的主题，本讲只需感知到它的存在。

README 的 Quick Start 就是这套契约的最小示范（[README.md:74-87](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/README.md#L74-L87)）：

```python
gemm = GemmOp()         # 构造
d = gemm(a, b)          # 调用 == gemm.forward(a, b) == a @ b.T
```

#### 4.2.4 代码实践

**实践目标**：亲手验证 `op(...)` 与 `op.forward(...)` 完全等价，并体会构造期 vs 调用期的分工。

**操作步骤**（待本地验证）：

```python
import torch
from tileops.ops import GemmOp

a = torch.randn(512, 256, device="cuda", dtype=torch.float16)
b = torch.randn(512, 256, device="cuda", dtype=torch.float16)   # N=512, K=256

op = GemmOp()                  # 构造期：安装 kernel_map，此时还没编译任何 kernel
d1 = op(a, b)                  # 调用期：首次调用，触发 JIT 编译
d2 = op.forward(a, b)          # 再次调用，应与 d1 完全一致

print("d1 shape:", tuple(d1.shape))              # 预期 (512, 512)
print("callable 等价:", torch.equal(d1, d2))      # 预期 True
print("op.dtype:", op.dtype)                      # 预期 torch.float16（调用期才绑定）
```

**需要观察的现象**：首次 `op(a, b)` 会明显变慢（JIT 编译），第二次很快（命中快路径 + kernel 已编译）；`op.dtype` 在构造期是 `None`，调用后才变成 `torch.float16`——这正是下一节要讲的 input-inferred。

**预期结果**：`d1`、`d2` 形状均为 `(512, 512)` 且 `torch.equal` 返回 `True`。

#### 4.2.5 小练习与答案

**练习 1**：既然 `__call__` 只是转发给 `forward`，为什么不直接让用户调 `op.forward(...)`，还要提供 `__call__`？

> **答案**：`op(a, b)` 比 `op.forward(a, b)` 更简洁，更接近「函数调用」直觉，也和 PyTorch 的 `nn.Module(x)` 习惯一致。此外 `__call__` 这一层未来可以在转发前后插入统一逻辑（如 `torch.compile` 边界处理），而 `forward` 保持纯净。

**练习 2**：如果一个 `Op` 子类忘了在 `__init__` 里调用 `dispatch_kernel`，会出什么问题？

> **答案**：`self.kernel_map` 不会被安装，`forward` 里访问 `self.kernel_map[...]` 会抛 `AttributeError` 或 `KeyError`。`dispatch_kernel` 是契约规定的「必经关卡」，所有合规子类的构造函数都要经过它。

---

### 4.3 input-inferred：形状与 dtype 在调用时推断

#### 4.3.1 概念说明

**input-inferred（输入推断）**是 TileOPs 区别于很多算子库的核心设计取向。它的含义是：**算子的形状与 dtype，尽量在「调用时」从输入张量推断，而不是在「构造时」写死**。

对比两种风格：

| 风格 | 构造时做什么 | 调用时做什么 | 代表 |
| --- | --- | --- | --- |
| 构造期固定 | 提交完整形状 + dtype | 只喂同形状数据 | 传统手写 kernel 封装 |
| **input-inferred** | 只提交「算子骨架」参数 | 从输入推断形状/dtype，按需 JIT 编译 | `GemmOp`、`SoftmaxFwdOp` |

input-inferred 的好处：**同一个 Op 实例可以处理多种形状**，避免「每种形状 new 一个 op」。代价是：首次对某个 `(形状, dtype)` 的调用要 JIT 编译 kernel（慢），编译结果按形状缓存（之后快）。这与 u1-l2 讲过的 DeepGEMM「compile-on-first-call + per-config cache」一脉相承。

不过 input-inferred 不是「一刀切」。TileOPs 的真实策略分三档，理解这个谱系是本模块的核心：

- **完全 input-inferred**：构造期不绑定任何形状/dtype。代表：`GemmOp`、`SoftmaxFwdOp`——形状和 dtype 全在 `forward` 里从输入推断。
- **部分绑定**：构造期提交影响 kernel 结构的「骨架」参数（如归一化的 `normalized_shape`，因为它决定 padding 量），但其余维度调用时推断。代表：`RMSNormFwdOp`。
- **dtype 是否构造期提交**：因 padding / kernel 选择需要，有的算子（`RMSNormFwdOp`）要求构造期给 `dtype`；有的（`SoftmaxFwdOp`、`GemmOp`）调用时从输入读。

#### 4.3.2 核心流程

判断一个算子的 input-inferred 程度，看它的 `__init__` 签名 + `forward` 里何时读 `x.dtype`/`x.shape`：

```text
                        构造期 __init__              调用期 forward
GemmOp            trans_a/trans_b 布局        从 a,b 推断 m,n,k,dtype
SoftmaxFwdOp      dim（归约轴）                从 x 推断 M,N,dtype
RMSNormFwdOp      normalized_shape, dtype     从 x 推断 leading dims（M）
```

关键直觉：**只要某个维度会影响「kernel 的物理结构」（如 padding 到 256 对齐、是否走 GEMV 快路径），它就必须在构造期或调用期早点确定**；纯粹的数据形状（如 batch、M 的大小）则可以延迟到调用时。

数学上，input-inferred 让一个 Op 实例覆盖的形状空间是一个集合 \(\{(m,n,k,\text{dtype}) \mid \text{合法}\}\)，而 kernel cache 为这个集合里每个「值得特化」的配置维护一份编译产物。

#### 4.3.3 源码精读

**完全 input-inferred 的典范：`GemmOp`。** 它的 `__init__` 只接收布局参数，完全不接形状/dtype（[tileops/ops/gemm.py:45-51](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/gemm.py#L45-L51)）：

```python
def __init__(self, trans_a: bool = False, trans_b: bool = True,
             kernel_map=None, tune: bool = False) -> None:
```

而 `m, n, k, dtype` 全部在 `forward` 里由输入推导。布局到逻辑维度的推导规则见 `_infer_mnk`（[tileops/ops/gemm.py:76-86](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/gemm.py#L76-L86)）：

```python
def _infer_mnk(self, a, b) -> Tuple[int, int, int]:
    k_a, m   = (a.shape[0], a.shape[1]) if self.trans_a else (a.shape[1], a.shape[0])
    n,   k_b = (b.shape[0], b.shape[1]) if self.trans_b else (b.shape[1], b.shape[0])
    if k_a != k_b:
        raise ValueError(...)        # 收缩维 K 必须一致
    return m, n, k_a
```

docstring 里把四种 `(trans_a, trans_b)` 布局到数学语义的映射写得很清楚（[tileops/ops/gemm.py:27-31](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/gemm.py#L27-L31)）：默认 `(False, True)` 即 NT，`gemm(a, b)` == `a @ bᵀ`。

**dtype 也调用时推断的典范：`SoftmaxFwdOp`。** 它构造期连 `dtype` 都不要求，构造函数只接收 `dim`（[tileops/ops/reduction/softmax.py:289-296](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/reduction/softmax.py#L289-L296)）：

```python
class SoftmaxFwdOp(_SoftmaxBaseOp):
    _op_kind = "softmax"
    _kernel_key = "softmax_fwd"
    _kernel_class = SoftmaxKernel

    def __init__(self, dim: Optional[int] = None, *, kernel_map=None, tune: bool = False):
        super().__init__(dim=dim, kernel_map=kernel_map, tune=tune)
```

dtype 是在 `forward` 的校验里从 `x` 读出来的（`self.dtype = x.dtype`，见基类 `_validate`，[tileops/ops/reduction/softmax.py:100](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/reduction/softmax.py#L100)）。`N`（归约维大小）和 `M`（其余维乘积）同样在 `forward` 里算（[tileops/ops/reduction/softmax.py:160-171](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/reduction/softmax.py#L160-L171)）。注意 line 104 起的 `forward` 接受任意秩（arbitrary-rank）输入，把任意维度的 softmax 都归约为 2D `(M, N)` 再交给 kernel——这是它能「一个实例吃多种形状」的关键。

**部分绑定的反例：`RMSNormFwdOp`。** 它的 `__init__` 把 `normalized_shape` 与 `dtype` 标为**构造期必填**，其中 `dtype` 还是关键字专用（keyword-only，`*` 之后）（[tileops/ops/norm/rms_norm.py:44-63](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/norm/rms_norm.py#L44-L63)）：

```python
def __init__(self, normalized_shape, eps=None, *, dtype, kernel_map=None, tune=False):
    self.normalized_shape = tuple(int(d) for d in normalized_shape)
    ...
    self.dtype = dtype                       # ← 构造期就提交
    ...
    self.N_padded = align_up(self.N, ALIGNMENT)   # ← padding 依赖 dtype 字节
    self.dispatch_kernel(kernel_map)
```

为什么 `RMSNormFwdOp` 不能完全 input-inferred？因为它要把特征维 `N` 向上 pad 到 256 对齐（`N_padded`），这个 padding 与 kernel 结构强相关，必须在构造期定死；同时 roofline 的字节记账要用 `dtype.itemsize`（见 §4.4），所以 `dtype` 也得构造期给。但**前导维 `M`（batch 等）依然在 `forward` 里推断**（[tileops/ops/norm/rms_norm.py:122-129](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/norm/rms_norm.py#L122-L129)）：`x` 先被 reshape 成 `(M, N)`，`M = x_flat.shape[0]`，按 `M` 取缓存的 kernel。

> 小结：input-inferred 是一个**谱系**，不是非黑即白。判断方法是看 `__init__` 签名里有哪些「形状/dtype」参数——没有就是完全推断，有就是部分绑定，且这些参数通常对应「影响 kernel 物理结构」的维度。

#### 4.3.4 代码实践

见 §4.4 的综合实践（它同时覆盖了 `SoftmaxFwdOp` 与 `RMSNormFwdOp` 的对比）。

#### 4.3.5 小练习与答案

**练习 1**：`SoftmaxFwdOp(dim=-1)` 实例化后，先后喂入形状 `(1024, 4096)` 和 `(8, 1024, 4096)` 的输入，会报错吗？

> **答案**：不会。因为 `N = x.shape[dim]` 和 `M = 其余维乘积` 都在 `forward` 里推断，两种形状会被分别编译成各自 `(M, N)` 的 kernel 并缓存。这正是 input-inferred 的威力：一个实例覆盖多种 batch。

**练习 2**：`RMSNormFwdOp` 为什么把 `dtype` 设计成构造期必填，而 `SoftmaxFwdOp` 不需要？

> **答案**：RMSNorm 的 `N_padded = align_up(N, ALIGNMENT)` 与 kernel 结构强绑定，且构造期就要算好；其 roofline 字节记账也要 `dtype.itemsize`。Softmax 的对齐 padding 在 kernel 内部处理（见其注释 "Alignment padding is handled by the kernel's forward()"），Op 层不依赖 dtype 决定结构，故可延迟到调用时读 `x.dtype`。

---

### 4.4 综合代码实践：对比 SoftmaxFwdOp / RMSNormFwdOp 与 PyTorch

本节是本讲的主实践，把 §4.1（导入）、§4.2（调用契约）、§4.3（input-inferred）三者串起来。

#### 4.4.1 实践目标

从 `tileops.ops` 导入 `SoftmaxFwdOp` 或 `RMSNormFwdOp`，跑一个例子，与对应的 PyTorch 函数对比结果，观察**输出 dtype 与 shape**，并体会两种算子在 input-inferred 程度上的差异。

#### 4.4.2 操作步骤

下面的脚本同时演示两个算子（待本地验证，需 Hopper GPU + 已 `make install`）：

```python
import torch
import torch.nn.functional as F
from tileops.ops import SoftmaxFwdOp, RMSNormFwdOp

torch.manual_seed(0)

# ===== A. SoftmaxFwdOp：完全 input-inferred（dtype 也调用时读）=====
x = torch.randn(1024, 4096, device="cuda", dtype=torch.float16)
softmax = SoftmaxFwdOp(dim=-1)          # 构造期：只给 dim，没给 dtype/shape
y = softmax(x)                           # 调用期：推断 M=1024, N=4096, dtype=fp16
y_ref = F.softmax(x, dim=-1)             # PyTorch 地面真值

print("=== SoftmaxFwdOp ===")
print("output shape:", tuple(y.shape), "(预期 (1024, 4096))")
print("output dtype:", y.dtype,          "(预期 torch.float16)")
print("max abs err :", (y - y_ref).abs().max().item())
print("op.dtype     :", softmax.dtype,    "(调用后才绑定)")

# ===== B. RMSNormFwdOp：部分绑定（构造期需 normalized_shape + dtype）=====
x2  = torch.randn(2048, 4096, device="cuda", dtype=torch.float16)
w   = torch.randn(4096, device="cuda", dtype=torch.float16)
rms = RMSNormFwdOp(normalized_shape=(4096,), dtype=torch.float16)  # 构造期提交
z   = rms(x2, w)                                            # 调用期：推断 M=2048
z_ref = F.rms_norm(x2, normalized_shape=(4096,), weight=w, eps=1e-6)

print("\n=== RMSNormFwdOp ===")
print("output shape:", tuple(z.shape), "(预期 (2048, 4096))")
print("output dtype:", z.dtype,         "(预期 torch.float16)")
print("max abs err :", (z - z_ref).abs().max().item())
```

#### 4.4.3 需要观察的现象

1. **输出 dtype 与输入一致**：两个算子都保持 `torch.float16`（fp16 进 fp16 出），与 PyTorch 行为对齐。
2. **输出 shape 与输入一致**：softmax 与 rms_norm 都不改变形状。
3. **误差很小但不为零**：因为 TileLang kernel 内部用 fp32 累加、再 cast 回 fp16（见 u3-l3 数值稳定性），与 PyTorch 的浮点实现路径不同，会有 ULP 级（末位）误差。
4. **`softmax.dtype` 调用后才被赋值**，而 `rms.dtype` 在构造期就已是 `torch.float16`——直观体现两者的 input-inferred 差异。
5. **首次调用明显慢**：JIT 编译；重复调用很快（命中 `_kernel_cache`）。

#### 4.4.4 预期结果

- `SoftmaxFwdOp`：shape `(1024, 4096)`，dtype `torch.float16`，与 `F.softmax` 的最大误差约 \(10^{-3}\) 量级（fp16 精度）。
- `RMSNormFwdOp`：shape `(2048, 4096)`，dtype `torch.float16`，与 `F.rms_norm`（eps=1e-6）的最大误差约 \(10^{-2}\) 量级。

> 若你本地没有 GPU，无法运行：上述结论标注为「待本地验证」。可改为**源码阅读型实践**——对照 §4.3.3 的源码链接，口头复述两个算子分别在 `__init__` 与 `forward` 里读/写了哪些形状与 dtype 字段，完成「绑定 vs 推断」的对照表。

#### 4.4.5 小练习与答案

**练习 1**：把 `RMSNormFwdOp` 的构造写成 `RMSNormFwdOp((4096,), torch.float16)`（位置参数）能成功吗？为什么？

> **答案**：不能。`dtype` 在签名里位于 `*` 之后（keyword-only，[rms_norm.py:49](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/norm/rms_norm.py#L49)），必须用 `dtype=...` 传。若写成位置参数，`torch.float16` 会被当成第二个位置参数 `eps`（一个 `torch.dtype` 当 `float` 用），随后报错。这是 TileOPs 用 keyword-only 强制 API 清晰的常见手法。

**练习 2**：`RMSNormFwdOp.eval_roofline()` 在 `forward` 之前调用会怎样？为什么？

> **答案**：会抛 `RuntimeError`（见 [rms_norm.py:76-81](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/norm/rms_norm.py#L76-L81)）。因为 roofline 的 `(flops, bytes)` 依赖调用时才绑定的前导维 `M`（`self._last_roofline_mn` 由 `forward` 写入）。这也是 input-inferred 的副作用：性能模型只能在「真正算过一次」之后才有意义。这个细节会在 u6/u7（性能基准与 roofline）展开。

## 5. 综合实践

设计一个小任务，把本讲的导入、调用契约、input-inferred 三个模块全部用上。

**任务：用 TileOPs 复现一个「归一化 + 激活」的小算子链，并用 PyTorch 验证。**

1. 从 `tileops.ops` 导入 `RMSNormFwdOp` 和 `SoftmaxFwdOp`（用 §4.1 的发现方法确认它们在 `__all__` 里）。
2. 构造一个 `[batch=4, seq=128, dim=256]` 的 fp16 输入 `x`。
3. 先对最后一个维度做 `RMSNormFwdOp`（注意 `normalized_shape=(256,)`、`dtype` 为 keyword-only），再对倒数第二维（seq 维，即 `dim=1`）做 `SoftmaxFwdOp`。
4. 用 `F.rms_norm` 和 `F.softmax` 复现同样链路，对比两者的最大误差、输出 shape、dtype。
5. **思考题**：这两步里，哪些维度是构造期提交的？哪些是调用时推断的？如果把 `batch` 从 4 改成 16，是否需要重新 `new` 一个 op 实例？

**验收标准**：
- 能正确解释「`batch` 维是调用时推断的，所以改 batch 不必重建实例」——这正是 input-inferred 的价值。
- 输出 shape 应为 `(4, 128, 256)`，dtype 为 `torch.float16`，与 PyTorch 误差在 fp16 量级。

## 6. 本讲小结

- `tileops.ops` 是算子的**单一公开导出入口**：`__init__.py` 把散落各子模块的算子类聚合重导，配 `__all__` 白名单；用户统一写 `from tileops.ops import XxxOp`。
- **可调用契约**：每个 `Op` 实例先 `op = XxxOp(构造参数)`，再 `out = op(输入张量...)`；`__call__` 透明转发给 `forward`；构造期经 `dispatch_kernel` 安装 `kernel_map`，调用期在 `forward` 里真正选并编译 kernel。
- **input-inferred 是一个谱系**：`GemmOp`/`SoftmaxFwdOp` 把形状与 dtype 全部延迟到调用时推断；`RMSNormFwdOp` 因 padding 结构把 `normalized_shape` 与 `dtype` 留在构造期提交，但前导维仍调用时推断。
- **是否构造期绑定，看是否影响 kernel 物理结构**：影响结构的维度（如对齐 padding、收缩维）需早绑定；纯数据形状（batch、M）可延迟。
- 首次调用会 JIT 编译并按 `(形状, dtype)` 缓存，之后命中缓存很快；`eval_roofline()` 这类性能查询必须在 `forward` 之后才有意义。
- 验证算子正确性的标准做法：以对应 PyTorch 函数（`F.softmax`、`F.rms_norm`、`torch.matmul`）为地面真值，比较输出与最大误差。

## 7. 下一步学习建议

本讲你掌握了 `Op` 的**外部用法**。下一讲 **u2-l1「Op 基类与生命周期」**将带你进入 `Op` 的**内部机制**：`dispatch_kernel` 如何与 `default_kernel_map` 协作、`_cache_key` 与 `_static_axes` 如何决定 kernel 复用、`_validate_dtypes` / `_infer_output_shapes` / `eval_roofline` 三个 codegen 契约的 staged-rollout 状态。

建议继续阅读的源码：

- [`tileops/ops/op_base.py`](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/op_base.py)：`Op` 基类全貌，重点看 `_install_kernel_map` 与 `_cache_key`。
- [`tileops/ops/gemm.py`](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/gemm.py)：`GemmOp` 的 `_get_kernel` 与 GEMV 快路径，是 u2-l4 跟读整条链路的预演。

如果你对「为什么 dtype 要 fp32 累加」感兴趣，可以提前扫一眼 [.claude/rules/code-style.md](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/.claude/rules/code-style.md) 的数值提升条目，那是 u3-l3 的内容。
