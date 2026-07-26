# 第一次调用 TileGym 算子

## 1. 本讲目标

上一篇我们装好了 TileGym 环境。本篇的目标是**亲手调通第一个 TileGym 算子**。读完本讲，你应该能够：

1. 知道 `tilegym.ops` 这个统一入口是怎么来的、怎么用。
2. 用 `set_backend` 切换当前后端，并能用 `get_available_backends` 查看哪些后端真正可用。
3. 写出一个约 10 行的最小脚本，调用 `tilegym.ops.softmax`。
4. 把 TileGym 的结果与 PyTorch 参考实现做「最大绝对误差」对比，验证正确性。

本篇刻意只挑最简单的 `softmax` 算子，**不展开内核内部细节**——内核写法是后续 U3 以后的内容。本篇只关心「怎么调用、怎么验证」。

## 2. 前置知识

在动手前，先建立两个直觉。

**第一，什么是 softmax。** 给定一个向量 \(x\)，softmax 把它归一化成一组「概率」（和为 1）。为了避免大数溢出，实际计算时先减去最大值 \(m\)：

\[
\text{softmax}(x)_i = \frac{e^{x_i - m}}{\sum_j e^{x_j - m}}, \quad m = \max_j x_j
\]

对二维张量 \((M, N)\)，就是**逐行**在最后一维 \(N\) 上做 softmax。这与 `torch.nn.functional.softmax(x, dim=-1)` 的语义完全一致，所以我们能直接拿 PyTorch 当「标准答案」来比对。

**第二，TileGym 的「算子」是 GPU 内核的封装。** 你调用的 `tilegym.ops.softmax(x)` 不会真的在 CPU 上算，而是把张量丢给一个 GPU 内核去算。因此：

- 输入张量必须在 **CUDA 设备**上（`device="cuda"`）。
- 当前后端（默认 `cutile`）必须真的可用，否则调用会抛 `NotImplementedError`。

如果你还没确认本机有哪些后端可用，请先回到上一篇（u1-l2）跑一遍 `tilegym.get_available_backends()`。

## 3. 本讲源码地图

本讲涉及的文件很少，但它们构成了「从 `import` 到算子调用」的完整链路：

| 文件 | 作用 |
| --- | --- |
| [src/tilegym/__init__.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/__init__.py) | 包入口：检查 torch 依赖、导出 `ops` 子模块与 `set_backend` 等后端函数 |
| [src/tilegym/ops/__init__.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/__init__.py) | ops 子包入口：按可用性导入各后端实现，并通过 `from .ops import *` 暴露统一算子名 |
| [src/tilegym/ops/ops.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py) | 所有算子的「统一签名 + stub」所在地，`softmax` 就定义在这里 |
| [src/tilegym/backend/selector.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py) | 后端选择器：探测可用后端、`set_backend`、环境变量读取 |
| [src/tilegym/backend/dispatcher.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py) | `@dispatch` 装饰器：按当前后端把调用路由到具体实现 |
| [tests/ops/test_softmax.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_softmax.py) | softmax 的官方测试，是最权威的「正确调用范例」 |
| [tests/ops/README.md](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/README.md) | 测试框架说明，示范如何写一个算子测试 |

> 说明：本讲只引用上面这些**真实存在**的文件；`dispatcher.py` / `selector.py` 的细节在 U2 会专门讲，这里只取理解「调用链」所需的最少部分。

## 4. 核心概念与源码讲解

### 4.1 tilegym.ops 统一入口

#### 4.1.1 概念说明

TileGym 有四套后端（cuTile、tilecpp、triton、cutile-rs），同一套算子名（如 `softmax`）可能在不同后端下有不同实现。如果让你记住「cuTile 的 softmax 在哪个文件、triton 的 softmax 又在哪个文件」，会很痛苦。

所以 TileGym 提供了**统一入口**：无论底层是哪个后端，你都只写

```python
tilegym.ops.softmax(x)
```

由库自己去决定该调哪个实现。这个 `tilegym.ops` 就是「算子的统一门面（facade）」。

#### 4.1.2 核心流程

从 `import tilegym` 到 `tilegym.ops.softmax(x)` 可用，经历了三步：

1. **包初始化**：`tilegym/__init__.py` 先校验 torch，再把 `ops` 子模块挂上去。
2. **ops 子包初始化**：`ops/__init__.py` 按各后端可用性导入实现，并通过 `from .ops import *` 把 `softmax` 这类名字暴露出来。
3. **统一签名 + stub**：`ops/ops.py` 里每个算子都被 `@dispatch("算子名")` 装饰，函数体本身只是个「占位实现（stub）」，默认抛 `NotImplementedError`。

伪代码表示：

```
import tilegym
   └─ tilegym/__init__.py:  检查 torch → 导入 backend 函数 → from . import ops
        └─ ops/__init__.py: from .ops import *   # 暴露 softmax 等名字
             └─ ops/ops.py: @dispatch("softmax") def softmax(...): raise NotImplementedError(...)
```

注意第 3 步：`ops.py` 里的 `softmax` **自己不会真正算 softmax**，它只是一个「带统一签名的占位」。真正干活的是后端实现（由 `@dispatch` 在调用时去注册表里查找）。这一点是理解 TileGym 架构的关键，U2 会深入讲注册表机制。

#### 4.1.3 源码精读

**包入口导出 `ops`：**

[src/tilegym/__init__.py:50](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/__init__.py#L50) 把 `ops` 子模块挂到顶层包上，所以 `tilegym.ops` 才能用。

它在导入 `ops` 之前，还做了两件与「能否调用」直接相关的事：

[src/tilegym/__init__.py:20-23](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/__init__.py#L20-L23) 校验 torch 是否安装；缺失会直接抛出带安装指引的 `ImportError`，而不是在后续调用时才报错。

[src/tilegym/__init__.py:34-40](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/__init__.py#L34-L40) 导出 `set_backend` / `get_current_backend` / `get_available_backends` 等后端函数，让它们也能通过 `tilegym.xxx` 直接访问。

**ops 子包暴露算子名：**

[src/tilegym/ops/__init__.py:52](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/__init__.py#L52) 用 `from .ops import *` 把 `ops.py` 里的所有公开算子名（含 `softmax`）拉到 `tilegym.ops` 命名空间。

另外，它会**按可用性**导入 cuTile 实现：[src/tilegym/ops/__init__.py:15-24](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/__init__.py#L15-L24) 只在 `is_backend_available("cutile")` 为真时才导入 `.cutile`，否则把 `cutile` 置为 `None` 并发警告。这就是「本机没装 cuda-tile 时 softmax 调不通」的根源。

**softmax 的统一签名 stub：**

[src/tilegym/ops/ops.py:225-244](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py#L225-L244) 定义了 `softmax`。关键信息有三点：

- 它被 `@dispatch("softmax")` 装饰（**没有**指定 `fallback_backend`，因此用默认值 `"pytorch"`）。
- 签名是 `softmax(x, use_tma=False, **kwargs)`，作用在二维张量 \((M, N)\) 的最后一维 \(N\) 上。
- 函数体只 `raise NotImplementedError(...)`——也就是说，如果没有任何后端注册了 `softmax` 实现，调用它就会抛这个错。

#### 4.1.4 代码实践

**实践目标**：确认你写出的 `tilegym.ops.softmax` 真的指向 `ops.py` 里的那个 stub 包装函数。

**操作步骤**：

1. 在装好 TileGym 的环境里启动 Python。
2. 依次执行下面几行：

```python
import tilegym
print(tilegym.__version__)          # 期望: 1.4.0
print(tilegym.ops.softmax)          # 期望: 一个被 @dispatch 包装过的函数
print(tilegym.ops.softmax.__module__)  # 期望指向 tilegym.ops.ops
```

**需要观察的现象**：`softmax` 不是 `None`，且其 `__module__` 指向 `tilegym.ops.ops`，说明统一入口确实来自 `ops.py`。

**预期结果**：版本号打印为 `1.4.0`（见 [src/tilegym/__init__.py:75](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/__init__.py#L75)），`softmax.__module__` 为 `tilegym.ops.ops`。**若 `tilegym.ops.softmax` 为 `None` 或导入时报 cutile 警告**，说明当前后端不可用，需回到 u1-l2 排查 `cuda-tile` / tileiras 安装。具体打印文本随环境而异，待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `tilegym.ops.softmax` 自己的函数体里只有一句 `raise NotImplementedError`，但实际调用时却能算出正确结果？

**参考答案**：因为 `@dispatch("softmax")` 把它包成了一个分发函数。真正调用时，分发器会去全局注册表 `_REGISTRY` 中按当前后端查找**已注册的后端实现**（由各后端模块用 `register_impl` 注册），找到就直接调用真正干活的内核；`ops.py` 里这段 `raise` 只是「谁都没实现时的兜底」。

**练习 2**：`from .ops import *` 为什么能把 `softmax` 带进 `tilegym.ops` 命名空间？

**参考答案**：`ops.py` 没有定义 `__all__`，于是 `import *` 会导入所有不以下划线开头的公开名字，`softmax` 正是其中之一。

---

### 4.2 set_backend 切换后端

#### 4.2.1 概念说明

统一入口的好处是「写一次，到处跑」，但**到底走哪个后端**，由一个「当前后端」全局变量决定。`set_backend` 就是用来改这个变量的。

为什么需要它？因为不同机器装的后端不同，同一个算子你也可能想对比不同后端的表现。TileGym 用一个模块级字符串 `_CURRENT_BACKENDS` 记录当前后端，默认值是 `"cutile"`。

#### 4.2.2 核心流程

后端选择的生命周期：

```
import tilegym
  └─ selector 探测各后端可用性 → 填充 _AVAILABLE_BACKENDS 集合
  └─ 读取环境变量 CUTILE_TUTORIALS_BACKEND → 设置 _CURRENT_BACKENDS（默认 cutile）
        └─ 运行时: set_backend("triton") → 改写 _CURRENT_BACKENDS
        └─ 运行时: get_current_backend() → 读 _CURRENT_BACKENDS
```

探测规则（来自 [src/tilegym/backend/selector.py:188-195](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L188-L195)）：

| 后端 | 可用性判据 |
| --- | --- |
| `cutile` | 能 `import cuda.tile` |
| `triton` | 恒为可用（nvtriton 还需 `ENABLE_TILE=1`） |
| `tilecpp` | 模块可导入（运行时再查 nvcc≥13.3，且缓存） |
| `cutile-rs` | `cargo` 在 PATH 或存在预编译 `.so` |

只有出现在 `_AVAILABLE_BACKENDS` 里的后端，`set_backend` 才会接受。

#### 4.2.3 源码精读

**查看可用后端**：[src/tilegym/backend/selector.py:218-219](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L218-L219) 直接返回 `_AVAILABLE_BACKENDS` 集合。

**初始化可用集合**：[src/tilegym/backend/selector.py:198-205](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L198-L205) 在模块加载时遍历探测结果，把可用的后端加入集合。

**环境变量选择**：[src/tilegym/backend/selector.py:208-215](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L208-L215) 读取 `CUTILE_TUTORIALS_BACKEND`；若该值不在可用集合中，直接抛 `ValueError`。也就是说你可以在启动 Python 前「预定」后端，而不必在脚本里写 `set_backend`。

**切换后端**：[src/tilegym/backend/selector.py:232-248](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L232-L248) 是 `set_backend` 全文。它做了两道校验：

- 后端必须在 `_AVAILABLE_BACKENDS` 中，否则 `ValueError`。
- 若选 `tilecpp`，还会**立即**复查 `is_tilecpp_available()`（即 nvcc≥13.3），让调用方「快速失败」而不是等到 dispatch 时才默默回退。

成功后写入 `_CURRENT_BACKENDS` 并打一条 info 日志。

**读取当前后端**：[src/tilegym/backend/selector.py:228-229](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L228-L229) 返回当前值，供 `@dispatch` 在每次调用算子时查询。

#### 4.2.4 代码实践

**实践目标**：列出本机可用后端，并切换到一个可用的非默认后端（若有的话），再切回 `cutile`。

**操作步骤**：

```python
import tilegym

print("available:", tilegym.get_available_backends())  # 例如 {'cutile', 'triton'}
print("current  :", tilegym.get_current_backend())     # 例如 cutile

# 切换到 triton（它恒为可用，是最稳妥的切换目标）
tilegym.set_backend("triton")
print("now      :", tilegym.get_current_backend())     # 期望: triton

# 切回默认
tilegym.set_backend("cutile")
```

**需要观察的现象**：`get_available_backends()` 返回的集合**因机器而异**——在有 cuda-tile 的机器上应包含 `cutile`，没有则不含。切换后 `get_current_backend()` 立即反映新值。

**预期结果**：在标准安装了 `cuda-tile` 的机器上，可用集合至少包含 `cutile` 和 `triton`，切换前后 `get_current_backend()` 准确变化。**若 `get_available_backends()` 里没有 `cutile`**，说明 cuTile 后端在本机不可用，本讲后续的 softmax 正确性验证将无法用 cutile 完成（可改用 `triton` 后端，前提是该后端注册了 softmax 实现；具体能否跑通待本地验证）。尝试 `set_backend("cutile-rs")` 而该后端不在集合中时，会抛 `ValueError`。

#### 4.2.5 小练习与答案

**练习 1**：在不修改 Python 脚本的前提下，如何让 TileGym 启动时就把后端设成 `triton`？

**参考答案**：在运行脚本前设置环境变量，例如 `CUTILE_TUTORIALS_BACKEND=triton python your_script.py`。`_load_from_environment()`（[selector.py:208-215](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L208-L215)）会在 import 时读取它。注意该值必须是可用后端，否则 import 阶段就报错。

**练习 2**：为什么 `set_backend("tilecpp")` 要在切换时**额外**查一次 nvcc 版本，而 `set_backend("triton")` 不用？

**参考答案**：因为 `_AVAILABLE_BACKENDS` 中 tilecpp 的条目只反映「模块能否导入」这个廉价检查（见 [selector.py:251-261](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L251-L261)），真正的运行时要求（nvcc≥13.3）被设计成延迟且缓存。`set_backend` 在这里主动补查一次，是为了让用户在「显式选择 tilecpp」时立刻拿到明确的错误，而不是等到第一次 dispatch 时才悄悄回退。triton 恒为可用，无需这种补查。

---

### 4.3 最小调用脚本：跑通 softmax

#### 4.3.1 概念说明

前面两节是「理论」，这一节把所有东西串成一个能跑的脚本。调用一个 TileGym 算子只需要三件事：

1. 准备一个**在 CUDA 上**的张量。
2. 确认当前后端可用（默认 `cutile`）。
3. 调用 `tilegym.ops.softmax(x)`。

#### 4.3.2 核心流程

```
torch.rand(M, N, device="cuda")   # 1. 造数据，放 GPU
        │
        ▼
tilegym.ops.softmax(x)            # 2. dispatch 按当前后端选实现 → 启动 GPU 内核
        │
        ▼
返回 (M, N) 的概率张量             # 3. 形状与输入一致
```

这里 dispatch 的关键逻辑（本讲只看懂行为，不展开注册表）来自 [src/tilegym/backend/dispatcher.py:74-81](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L74-L81)：每次调用时先看有没有显式传 `backend=` 参数，没有就用 `get_current_backend()` 的值去注册表里查实现。

#### 4.3.3 源码精读

**softmax 的入参约定**：[src/tilegym/ops/ops.py:228-243](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py#L228-L243) 明确写出：输入是形状 \((M, N)\) 的二维张量，沿最后一维 \(N\) 计算；`use_tma` 等是可选的内核开关，本讲用默认值即可。

**官方调用范例**：测试文件是「最权威的用法说明书」。[tests/ops/test_softmax.py:55-61](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_softmax.py#L55-L61) 示范了如何造数据：

```python
device = torch.device("cuda")
x = torch.rand(m, n, device=device, dtype=dtype)
```

它用的就是 `(256, 2048)` 这组参数（见 [test_softmax.py:24-35](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_softmax.py#L24-L35) 的 parametrize），与本讲实践任务完全一致。

**先设后端再调用**：[tests/ops/test_softmax.py:48-49](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_softmax.py#L48-L49) 在调用算子前先 `tilegym.set_backend(backend)`——这是好习惯，尤其在脚本里要对比多个后端时。

#### 4.3.4 代码实践

**实践目标**：跑出 TileGym softmax 的第一行输出，确认链路通畅。

**操作步骤**：把下面约 8 行存为 `first_softmax.py` 并运行（`python first_softmax.py`）。

```python
# 示例代码
import torch
import tilegym

tilegym.set_backend("cutile")          # 选定后端（也可用环境变量预定）

x = torch.rand(256, 2048, device="cuda", dtype=torch.float32)  # 数据放 GPU
y = tilegym.ops.softmax(x)             # 调用 TileGym 算子

print(y.shape)                         # 期望: torch.Size([256, 2048])
print(y.sum(dim=-1)[:3])               # 每行应接近 1.0
```

**需要观察的现象**：输出形状与输入一致；每行之和接近 1（softmax 的定义性质）。

**预期结果**：形状为 `torch.Size([256, 2048])`，`y.sum(dim=-1)` 每个元素都极接近 `1.0`（fp32 下误差通常在 1e-6 量级）。若抛 `NotImplementedError: softmax is not implemented for cutile`，说明 cuTile 实现未注册/未导入——回到 u1-l2 检查 `cuda-tile` 与 tileiras。具体求和数值待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `device="cuda"` 去掉（张量留在 CPU），会发生什么？

**参考答案**：TileGym 的 softmax 是 GPU 内核，期望输入在 CUDA 上。CPU 张量会让内核在启动或内部访问时出错（具体报错信息取决于后端，待本地验证）。所以调用前必须确保张量在 `cuda`。

**练习 2**：调用时能否临时指定一个与当前后端不同的后端，而不调用 `set_backend`？

**参考答案**：可以。`@dispatch` 的 wrapper 支持显式 `backend=` 关键字参数（见 [dispatcher.py:76](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L76)），例如 `tilegym.ops.softmax(x, backend="triton")` 会临时走 triton 实现，且不影响全局的 `_CURRENT_BACKENDS`。

---

### 4.4 与 torch 参考对比：正确性验证

#### 4.4.1 概念说明

「能跑」不等于「算得对」。验证 GPU 内核正确性的标准做法是：**用 PyTorch 官方实现当参考答案（reference），再比较两者的逐元素差异**。

最直观的指标是**最大绝对误差**：

\[
\text{max\_abs\_err} = \max_{i,j} \left| y^{\text{tilegym}}_{ij} - y^{\text{torch}}_{ij} \right|
\]

对 fp32 的 softmax，由于两种实现都是数值稳定的，这个误差应当非常小（参考测试用 `atol=1e-7`，见下文）。

#### 4.4.2 核心流程

```
x (cuda, fp32)
   ├──► tilegym.ops.softmax(x)        ──► y_tg
   └──► torch.nn.functional.softmax(x, dim=-1)  ──► y_ref
                                  │
                                  ▼
        max_abs_err = (y_tg - y_ref).abs().max()
```

TileGym 自己的测试框架就是按这个套路设计的：每个测试类实现一个 `reference` 静态方法返回 PyTorch 结果，再用 `assertCorrectness` 比对（见 [tests/ops/README.md:33-56](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/README.md#L33-L56) 与 [tests/ops/README.md:60-62](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/README.md#L60-L62)）。本讲我们用「手写最大绝对误差」复刻它的精神。

#### 4.4.3 源码精读

**参考实现写法**：[tests/ops/test_softmax.py:15-17](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_softmax.py#L15-L17) 直接用 `torch.nn.functional.softmax(x, dim=-1)` 作为 reference——与本讲实践任务指定的参考函数完全一致。

**容差选取**：[tests/ops/test_softmax.py:64-67](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_softmax.py#L64-L67) 给出了官方认定的容差：fp32 用 `rtol=1e-5, atol=1e-7`，fp16 用 `rtol=1e-3, atol=1e-5`。这意味着对 fp32，最大绝对误差期望在 `1e-6` 量级或更小。

**框架级断言**：[tests/ops/test_softmax.py:69-77](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_softmax.py#L69-L77) 调用 `self.assertCorrectness(tilegym.ops.softmax, self.reference, {"x": x}, ...)`。它的含义正是：用相同输入 `{"x": x}` 分别跑 TileGym 算子和 reference，再按 `rtol/atol` 判断是否一致。我们手写脚本就是在「拆解」这个调用。

#### 4.4.4 代码实践

**实践目标**：计算 TileGym softmax 与 PyTorch 参考的最大绝对误差，判断是否在可接受范围内。

**操作步骤**：把下面约 10 行存为 `check_softmax.py` 并运行。

```python
# 示例代码
import torch
import tilegym

tilegym.set_backend("cutile")
x = torch.rand(256, 2048, device="cuda", dtype=torch.float32)

y_tg   = tilegym.ops.softmax(x)                       # TileGym 实现
y_ref  = torch.nn.functional.softmax(x, dim=-1)       # PyTorch 参考答案

max_abs_err = (y_tg - y_ref).abs().max().item()
print("max abs error:", max_abs_err)
print("within tol   :", max_abs_err < 1e-5)           # 参考 test_softmax 的 atol/rtol
```

**需要观察的现象**：终端打印一个很小的浮点数；`within tol` 为 `True`。

**预期结果**：对 fp32、形状 `(256, 2048)`，最大绝对误差通常在 `1e-6` 量级或更小（与官方 `atol=1e-7, rtol=1e-5` 一致）。**精确数值随 GPU 型号、CUDA/cuda-tile 版本而异，待本地验证**；只要 `within tol` 为 `True`，即可认为 cuTile softmax 正确。

**拓展（可选）**：把 dtype 改成 `torch.float16`，观察误差变大（仍应在 `1e-3` 量级内），体会低精度对数值误差的影响。若想直接跑官方测试，可执行：

```bash
pytest tests/ops/test_softmax.py -k test_op -v --log-cli-level=INFO
```

该命令来自 [tests/ops/README.md:13-15](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/README.md#L13-L15)。

#### 4.4.5 小练习与答案

**练习 1**：为什么不直接比较 `torch.equal(y_tg, y_ref)`，而要用最大绝对误差？

**参考答案**：GPU 内核与 PyTorch 实现在浮点累加顺序、中间精度上几乎不可能做到逐 bit 一致，`torch.equal` 要求完全相等，几乎必然失败。浮点正确性应基于容差（`rtol`/`atol`）判断，最大绝对误差正是容差检验的直观形式。

**练习 2**：如果把 `x` 的列数从 2048 改成 1009（一个非 2 的幂、也不能被常见 tile 整除的数），最大绝对误差会明显变大吗？

**参考答案**：不会明显变大。TileGym 的 softmax 内核会处理边界（如 padding / check_bounds，U3 会讲），列数不是 2 的幂只影响性能与边界处理，不影响数值正确性——`test_softmax.py` 的 parametrize 里就专门包含了 `(256, 1009, torch.float16)` 和 `(256, 9, torch.float32)` 这类「奇怪」列宽（见 [test_softmax.py:24-35](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_softmax.py#L24-L35)）。具体数值待本地验证。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个**贯穿任务**（就是本讲规格里指定的实践）：

> 写一个约 10 行脚本：随机生成 `(256, 2048)` 的 fp32 张量，调用 `tilegym.ops.softmax`，并与 `torch.nn.functional.softmax` 比较最大绝对误差。

要求脚本里体现以下四点（对应四个最小模块）：

1. 通过 `tilegym.get_available_backends()` 打印可用后端，并用 `tilegym.get_current_backend()` 确认当前后端。
2. 用 `tilegym.set_backend("cutile")` 显式选定后端（若本机无 cutile，可改为 `triton` 并说明原因）。
3. 在 `device="cuda"` 上造数据并调用 `tilegym.ops.softmax(x)`。
4. 与 `torch.nn.functional.softmax(x, dim=-1)` 求最大绝对误差，并依据 `1e-5` 判定是否通过。

参考实现可直接综合 4.3.4 与 4.4.4 的两段示例代码拼接而成。完成后，请再尝试：

- 用 `tilegym.ops.softmax(x, backend="triton")` 临时切到 triton 后端跑一次，比较两次的 `max_abs_err` 是否都达标（若 triton 注册了 softmax 实现；待本地验证）。
- 用环境变量 `CUTILE_TUTORIALS_BACKEND=triton` 启动同一个脚本（删掉脚本里的 `set_backend`），验证 import 阶段就完成了后端选择。

## 6. 本讲小结

- `tilegym.ops` 是**统一算子入口**，由 `ops/__init__.py` 的 `from .ops import *` 暴露；`ops.py` 里每个算子只是带 `@dispatch` 的「统一签名 stub」，自己不真正计算。
- **当前后端**由 `set_backend` 控制，默认 `cutile`；可用后端在 import 时探测并写入 `_AVAILABLE_BACKENDS`，也可用环境变量 `CUTILE_TUTORIALS_BACKEND` 预定。
- 调用算子的最小要素：张量在 **CUDA** 上、当前后端**可用**、按 `ops.py` 的签名传参（softmax 为 `softmax(x, use_tma=False, **kwargs)`）。
- 验证正确性的标准套路：以 `torch.nn.functional.softmax` 为 reference，比较**最大绝对误差**；fp32 下应满足官方容差 `rtol=1e-5, atol=1e-7`。
- `tests/ops/test_softmax.py` 是最权威的「正确调用 + 容差」范例，遇到用法疑问先读它。

## 7. 下一步学习建议

你已经能调用并验证一个算子了。接下来：

- **横向扩展**：用同样的套路试一个别的算子，比如 `tilegym.ops.silu_and_mul` 或 `tilegym.ops.rms_norm`（注意它们的签名与输入约束不同，先读 `ops.py` 里对应的 docstring）。
- **纵向深入（U2）**：本讲反复提到的 `@dispatch`、`_REGISTRY`、`register_impl`、fallback 机制，将在 **u2-l1 统一算子接口 ops.py** 与 **u2-l2 后端注册表与分发机制 dispatcher.py** 中系统讲解。如果你想理解「为什么 `softmax(x, backend="triton")` 能临时换后端」，那是必经之路。
- **跑官方测试**：执行 `pytest tests/ops/test_softmax.py -k test_op -v`，观察多个变体（`use_tma`/`use_chunked`/`use_multi_wave`）如何被参数化——这会为 U3 学习真实内核打好基础。
