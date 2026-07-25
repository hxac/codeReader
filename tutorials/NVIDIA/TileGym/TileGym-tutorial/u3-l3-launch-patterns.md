# 内核启动模式：ct.launch 与 grid 计算

## 1. 本讲目标

上一篇（u3-l1）我们看清了 cuTile 内核的「骨架」：`@ct.kernel` 装饰器、`ConstInt` 常量、`ct.bid`/`ct.num_blocks` 网格索引。但内核函数本身只是一段待编译的 Tile IR——**谁来启动它？启动多少个块？参数怎么传进去？** 这些都发生在「主机侧」（CPU/Python 一侧）的 `_launch_*` 函数里。

本讲专门讲主机侧的启动逻辑。学完后你应当能够：

- 说清 `num_programs = min(NUM_SM * 4, n_rows)` 这行代码每一项的含义，以及它和内核装饰器 `@ct.kernel(occupancy=4)` 的呼应关系。
- 解释为什么每个 `_launch_*` 函数都要先对输入输出张量调 `.contiguous()`。
- 默写出 `ct.launch` 的四个位置参数（stream / grid / kernel / args 元组）及其顺序约定。
- 看懂 `_Softmax.forward` 如何根据 `use_tma`/`use_chunked`/`use_multi_wave` 在四种启动变体之间做选择。

本讲只讲「怎么启动」，不重复讲内核内部的算术（见 u3-l1）和数据搬运原语（见 u3-l2），也暂不展开四种 softmax 算法本身的差异（那是 u3-l4 的主题）。

## 2. 前置知识

阅读本讲前，请确认你已理解以下概念（在 u3-l1、u3-l2 中建立）：

- **主机侧（host）与设备侧（device）**：Python 代码运行在 CPU 上（主机侧），负责准备数据、计算网格大小、把内核「提交」给 GPU；内核函数体运行在 GPU 上（设备侧）。本讲讲的全是主机侧代码。
- **block / program / grid**：GPU 的一次启动会派生出一群「块」（block，cuTile 里也叫 program）。所有块的集合叫 grid。`ct.bid(0)` 是当前块在 x 维的编号，`ct.num_blocks(0)` 是 x 维的块总数。
- **SM（Streaming Multiprocessor）**：GPU 上真正执行块的硬件单元。一块 GPU 有 `NUM_SM` 个 SM，每个 SM 可以同时驻留若干个块。
- **occupancy（占用率）**：每个 SM 上能同时跑多少个块。它是性能调优的核心概念，本讲会反复用到。
- **ConstInt**：`ct.Constant[int]`，编译期常量，会被「烤进」特化内核（见 u3-l1）。

如果你对 `ct.gather`/`ct.load` 还不熟，也不用担心——本讲引用的内核代码里会出现它们，但我们的关注点是**启动这几个内核的那层 Python 包装**，而不是内核内部怎么搬数据。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件展开：

| 文件 | 作用 |
| --- | --- |
| [src/tilegym/ops/cutile/softmax.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py) | softmax 的全部四种内核实现 + 四个对应的 `_launch_*` 主机侧启动函数 + `_Softmax` autograd 封装。是本讲的主样本。 |

为对照说明，还会引用两个辅助文件：

| 文件 | 作用 |
| --- | --- |
| [src/tilegym/ops/cutile/silu_and_mul.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py) | 另一个算子的启动写法，用来对照「contiguous 保证」的两种风格（直接 `.contiguous()` vs `_ensure_contiguous` 装饰器）。 |
| [src/tilegym/experimental.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/experimental.py) | 对 `ct.launch` 做了一次 monkey-patch，恰好暴露了 `ct.launch` 的官方参数签名，是本讲确认参数顺序的最可靠依据。 |

记忆口诀：**「算 grid → 保连续 → 拿流 → 装参数 → `ct.launch`」**。下面四个模块就按这条主线展开。

## 4. 核心概念与源码讲解

### 4.1 grid 计算：NUM_SM × occupancy

#### 4.1.1 概念说明

启动内核时最关键的一个决定是：**这次要启动多少个块（grid 有多大）？** 块太少，GPU 上的 SM 闲置；块太多，调度开销上升。softmax 给出了两种截然不同的回答：

1. **静态持久化调度（static persistent）**：只启动「刚好把 GPU 填满」数量的块，每个块用一个 `for` 循环跨步处理多行。块「持久驻留」，循环复用。
2. **多波调度（multi-wave）**：一行一块，启动 `n_rows` 个块。当块数超过 SM 能同时驻留的数量时，多余的块排成若干「波」依次执行。

两种方式对应不同的 grid 计算公式，这正是练习题要你对比的核心。

> 术语澄清：本仓库代码注释里把 `for row_idx in range(pid, N_ROWS, num_programs)` 这种写法叫「静态持久化调度」（static persistent scheduling）。它的本质就是经典的 **grid-stride loop**——每个块从自己的编号出发，按块总数跨步，覆盖一段连续的工作项。

#### 4.1.2 核心流程

静态持久化调度的 grid 计算分三步：

1. **查硬件**：读取当前 GPU 的 SM 数量 `NUM_SM`。
2. **算饱和块数**：`NUM_SM × occupancy`，即「让每个 SM 驻留 `occupancy` 个块」所需的总块数。
3. **封顶**：用 `min(..., n_rows)` 防止块数超过实际行数（持久化模型里每个块至少要分到一行才有意义）。

公式化为：

\[
\text{num\_programs} = \min(\text{NUM\_SM} \times \text{occupancy},\ \text{n\_rows})
\]

\[
\text{blocks\_per\_SM} = \frac{\text{num\_programs}}{\text{NUM\_SM}} \approx \text{occupancy}
\]

这里有一个**必须呼应的设计约定**：内核装饰器上的 `@ct.kernel(occupancy=K)` 和启动函数里的 `NUM_SM * K` 用的是**同一个常数 K**。

- `occupancy=K` 是给编译器的**提示**：告诉它「我打算让每个 SM 驻留 K 个块」，编译器据此分配寄存器/共享内存，力求真跑起来时每 SM 能驻留 K 个。
- `NUM_SM * K` 是给运行时的**实际派发**：真的启动这么多块，让提示变成现实。

两者用同一个 K，才能让「每 SM 恰好 K 个驻留块」的假设成立。看代码你会发现：basic 内核 `occupancy=4` 配 `NUM_SM*4`，TMA 内核 `occupancy=2` 配 `NUM_SM*2`，chunked 内核 `occupancy=4` 配 `NUM_SM*4`——处处一致。

多波调度则完全不同：它直接 `grid = (n_rows, 1, 1)`，与 `NUM_SM` 无关，也**没有** grid-stride 循环——每个块只处理自己那 `bid` 对应的一行。

#### 4.1.3 源码精读

先看 basic 内核装饰器上的 occupancy 提示，以及它内部典型的 grid-stride 循环：

[softmax.py:18-31 — basic 内核 occupancy=4 与静态持久化循环](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L18-L31)

```python
@ct.kernel(occupancy=4)
def _softmax_kernel(output, input, N_ROWS: ConstInt, TILE_SIZE: ConstInt, DIM_COLS: ConstInt):
    pid = ct.bid(0)
    num_programs = ct.num_blocks(0)        # 读回启动时定的 grid.x
    offsets = ct.arange(TILE_SIZE, dtype=ct.int32)
    for row_idx in range(pid, N_ROWS, num_programs):   # grid-stride
        ...
```

注意内核里的 `num_programs = ct.num_blocks(0)` **不是写死的**，它在运行时读回主机侧定下的 `grid.x`。所以主机侧改 grid，内核循环步长自动跟着变——两边通过 `ct.num_blocks` 解耦。

再看主机侧如何算出这个 grid：

[softmax.py:189-191 — 计算 NUM_SM 与 grid（basic 内核）](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L189-L191)

```python
NUM_SM = torch.cuda.get_device_properties(input.device).multi_processor_count
num_programs = min(NUM_SM * 4, n_rows)     # 这里的 4 == 装饰器的 occupancy=4
grid = (num_programs, 1, 1)
```

- `multi_processor_count` 是 PyTorch 暴露的 SM 数量（例如 H100 为 132，A100 40GB 为 108）。
- `NUM_SM * 4` 就是「填满 GPU 所需的块数」。当 `n_rows`（例如 256）小于它时，`min` 取 `n_rows`，每个块只处理一行；当 `n_rows` 很大（例如几十万）时，取 `NUM_SM * 4`，每个块跨步处理很多行。

TMA 版本完全同构，只是常数换成 2：

[softmax.py:252-254 — TMA 内核用 occupancy=2](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L252-L254)

```python
NUM_SM = torch.cuda.get_device_properties(input.device).multi_processor_count
num_programs = min(NUM_SM * 2, n_rows)     # 2 == _softmax_kernel_tma 的 occupancy=2
grid = (num_programs, 1, 1)
```

对照 TMA 内核装饰器 `@ct.kernel(occupancy=2)`（[softmax.py:80](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L80)），同样是「装饰器系数 == 启动系数」。

最后看多波版本，grid 完全不依赖 NUM_SM：

[softmax.py:207-212 — 多波启动：一行一块](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L207-L212)

```python
def _launch_softmax_kernel_multi_wave_full_row_reg_cached_ldg(input, output, TILE_SIZE):
    n_rows, n_cols = input.shape
    input = input.contiguous()
    output = output.contiguous()
    grid = (n_rows, 1, 1)                   # 直接等于行数，不查 NUM_SM
```

对应的内核（[softmax.py:53-66](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L53-L66)）里 `row_idx = ct.bid(0)`，**没有** `for ... range(pid, N_ROWS, num_programs)` 循环——一个块只干一行。当 `n_rows` 远大于 SM 能驻留的块数时，这些块就分波次执行，故名 multi-wave。

#### 4.1.4 代码实践

**实践目标**：亲手算出你机器上 basic 与多波两种调度的 grid，理解 `min(NUM_SM*4, n_rows)` 在不同规模下的取值。

**操作步骤**：

1. 用下面这段示例脚本（**示例代码**，非仓库原有）打印你机器的 SM 数量与两种 grid：

   ```python
   # 示例代码
   import torch
   NUM_SM = torch.cuda.get_device_properties(0).multi_processor_count
   print("NUM_SM =", NUM_SM)

   for n_rows in [256, 4096, 100_000]:
       basic = min(NUM_SM * 4, n_rows)
       multi_wave = n_rows
       print(f"n_rows={n_rows:>7}  basic grid={basic:<6}  multi_wave grid={multi_wave}")
   ```

2. 在不运行的情况下，先在纸上预测输出，再运行核对。

**需要观察的现象**：

- 当 `n_rows` 较小（如 256）时，`basic` 与 `multi_wave` 的 grid 很可能**相等**（都接近 `n_rows`），此时两种调度差别不大。
- 当 `n_rows` 很大（如 100000）时，`basic` 被 `NUM_SM*4` 封顶（如 132×4=528），而 `multi_wave` 仍是 100000——差距悬殊。

**预期结果**：具体数值取决于你的 GPU 型号（`NUM_SM` 不同），所以**待本地验证**。但结论是确定的：静态持久化把 grid 控制在「刚好填满 GPU」，多波则让 grid 随数据量线性增长。

#### 4.1.5 小练习与答案

**练习 1**：假设 GPU 有 132 个 SM，basic 内核（occupancy=4）处理 `n_rows=1000` 的输入。启动多少个块？每个块处理多少行？

**答案**：`num_programs = min(132*4, 1000) = min(528, 1000) = 528` 个块。每个块用 grid-stride 循环处理 `ceil(1000/528) ≈ 2` 行（具体是 `range(pid, 1000, 528)`，大部分块处理 2 行，靠后的块处理 1 行）。

**练习 2**：如果有人把内核装饰器改成 `@ct.kernel(occupancy=4)`，却把启动写成 `min(NUM_SM * 8, n_rows)`，会出现什么「概念上的不匹配」？是否一定报错？

**答案**：提示（occupancy=4）说「每 SM 驻留 4 块」，但实际启动了 `NUM_SM*8` 个块，相当于每 SM 平均要塞 8 个块——多于提示值。这不一定报错（occupancy 只是提示，不是硬约束），但编译器按 4 块/SM 分配寄存器，实际塞 8 块可能导致寄存器不足、部分块排队，偏离「单波驻留」的初衷。仓库代码刻意保持两数相等正是为了避免这种偏差。

**练习 3**：多波版本为什么不写 `@ct.kernel(occupancy=...)`？

**答案**：多波的 grid 等于行数，块数可能远超 SM×驻留数，本身就预期「分多波执行」，不追求「单波全部驻留」，因此不给出 occupancy 提示。它的并行度由数据量决定，而非由 SM 数决定。

### 4.2 contiguous 保证

#### 4.2.1 概念说明

每个 `_launch_*` 函数在算完 grid 之后、调 `ct.launch` 之前，都会做一件看起来很无聊的事：

```python
input = input.contiguous()
output = output.contiguous()
```

为什么必须做？因为 cuTile 内核拿到的是**底层设备指针 + 形状信息**，内核里的 `ct.gather`/`ct.load` 会按「行主序、紧密排布」的假设去算内存偏移。如果张量**不连续**（non-contiguous）——比如它是某个大张量的切片 `x[:, ::2]`、转置 `x.t()`、或者带 padding 的视图——它的**步长（stride）**就和默认假设不一致，内核算出来的偏移就会指向错误的位置，读出垃圾数据甚至越界。

`.contiguous()` 的语义是：如果张量已经是连续的，**原样返回**（零开销）；如果不连续，**拷贝**出一份连续的副本。因此它是一个「保底且无损」的操作。

#### 4.2.2 核心流程

主机侧保证连续的流程：

1. **识别所有进内核的张量参数**（input、output，以及反向时的 grad 等）。
2. **逐个 `.contiguous()`**：已经是连续的免去拷贝，否则物化一份。
3. 把**保证连续之后**的张量装进 args 元组传给 `ct.launch`。

仓库里有两套写法：

- **就地两行**：softmax 的四个 `_launch_*` 函数各自手写 `input = input.contiguous(); output = output.contiguous()`。
- **装饰器统一处理**：silu_and_mul 用 `_ensure_contiguous` 装饰器，把「所有 Tensor 入参自动转连续」抽成可复用逻辑。

#### 4.2.3 源码精读

softmax basic 启动函数里的两行：

[softmax.py:186-187 — 启动前保证 input/output 连续](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L186-L187)

```python
    # Ensure tensors are contiguous
    input = input.contiguous()
    output = output.contiguous()
```

四个启动函数（basic / multi_wave / tma / chunked）都重复了这两行——这是有意为之的防御式写法，因为外部调用者可能传入任意视图。

再看 silu_and_mul 的装饰器版本，它把这件事泛化：

[silu_and_mul.py:109-119 — _ensure_contiguous 装饰器](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L109-L119)

```python
def _ensure_contiguous(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        def maybe_to_contiguous(x):
            return x.contiguous() if isinstance(x, torch.Tensor) else x
        args = [maybe_to_contiguous(arg) for arg in args]
        kwargs = {k: maybe_to_contiguous(v) for k, v in kwargs.items()}
        return fn(*args, **kwargs)
    return wrapper
```

它被用在对外注册的实现上（[silu_and_mul.py:205-207](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L205-L207)）：

```python
@register_impl("silu_and_mul", backend="cutile")
@_ensure_contiguous
def silu_and_mul(input, out=None):
    ...
```

装饰器顺序很重要：`_ensure_contiguous` 离函数更近，所以先于 `register_impl` 生效——任何进入这个实现的调用，张量入参都会先被转连续。两种写法**目的相同**，只是 softmax 选择在启动函数内部手写、silu_and_mul 选择在对外入口上统一拦截。

#### 4.2.4 代码实践

**实践目标**：亲眼看到「不连续输入会让内核读到错误数据」，从而理解 `.contiguous()` 不是多余的。

**操作步骤**：

1. 构造一个不连续视图，并观察它的 stride：

   ```python
   # 示例代码
   import torch
   x = torch.randn(4, 8, device="cuda")
   view = x[:, ::2]          # 取偶数列，不连续
   print(view.shape, view.stride())   # stride 不是默认的 (8, 1)
   print(view.is_contiguous())        # False
   c = view.contiguous()
   print(c.is_contiguous(), torch.equal(c, view))  # True True，但 c 是一份新拷贝
   ```

2. （可选）如果你有可用后端，比较 `tilegym.ops.softmax(view)` 与 `tilegym.ops.softmax(view.contiguous())` 的结果是否一致。

**需要观察的现象**：`view.is_contiguous()` 为 `False`，且 `view.stride()` 第二维不是 1。`.contiguous()` 之后得到的新张量数据相同但布局变成了紧密排布。

**预期结果**：内部 `.contiguous()` 会把 `view` 物化成连续副本再交给内核，所以两种调用结果应当一致；如果没有这层保证，内核按错误 stride 读数据会得到完全错误的结果。**待本地验证**实际调用。

#### 4.2.5 小练习与答案

**练习 1**：`.contiguous()` 在张量已经连续时会有性能损失吗？

**答案**：不会。PyTorch 的 `.contiguous()` 在输入已连续时直接返回 `self`，不分配、不拷贝。所以「无脑先调一次」是安全且零开销的防御式写法。

**练习 2**：为什么 softmax 不像 silu_and_mul 那样用 `_ensure_contiguous` 装饰器，而是在每个 `_launch_*` 里手写两行？

**答案**：两种都可以。softmax 的启动函数只处理固定的 input/output 两个张量，手写两行直白清晰；silu_and_mul 的实现入口参数更多（input、out，反向还有 grad_output 等），用装饰器统一处理更省心、更不易漏。这是工程取舍，不是对错问题。

### 4.3 ct.launch 的参数约定

#### 4.3.1 概念说明

`ct.launch` 是把内核「提交」到 GPU 执行的最终一步。它的签名是**四个位置参数**（positional-only）：

```
ct.launch(stream, grid, kernel, kernel_args)
```

| 参数 | 含义 | softmax 里的典型值 |
| --- | --- | --- |
| `stream` | CUDA 流，决定内核排在哪条队列上、与周围 op 的先后顺序 | `torch.cuda.current_stream()` |
| `grid` | 网格大小，三元组 `(x, y, z)`，即三个维度各启动多少个块 | `(num_programs, 1, 1)` |
| `kernel` | `@ct.kernel` 装饰后的内核对象（已被 JIT 编译） | `_softmax_kernel` |
| `kernel_args` | 一个**元组**，元素按内核位置参数顺序排列 | `(output, input, n_rows, TILE_SIZE, original_n_cols)` |

最可靠的「签名证据」来自 `experimental.py`——它对 `ct.launch` 做了 monkey-patch，patch 函数的参数列表就是原始签名：

[experimental.py:68-73 — patched_launch 暴露了 ct.launch 的官方签名](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/experimental.py#L68-L73)

```python
def _patched_launch(stream, grid, kernel, kernel_args, /):   # 注意末尾的 /：仅位置参数
    msg = getattr(kernel, "_tracked_message", None)
    if msg:
        warn_once(msg, "EXPERIMENTAL")
        kernel._tracked_message = None
    return _original_launch(stream, grid, kernel, kernel_args)
```

末尾的 `/` 表示这四个参数**只能按位置传，不能用关键字**。这也是为什么 softmax 里所有调用都是位置参数形式。

#### 4.3.2 核心流程

构造一次 `ct.launch` 调用的步骤：

1. **拿当前流**：`torch.cuda.current_stream()`。用「当前流」而非固定流，是为了和 PyTorch 上下文里前后的算子排在同一条队列上，保持执行顺序（避免内核抢跑、读到尚未就绪的数据）。
2. **定 grid**：按 4.1 节算出的 `(num_programs, 1, 1)`。softmax 只用 x 维，所以后两维是 1。
3. **选内核**：选择本次要启动的 `@ct.kernel` 对象（如 `_softmax_kernel`）。
4. **装参数元组**：按内核签名**逐位置**放入参数。张量直接放；`ConstInt` 参数放对应的 Python `int`（cuTile 会把它当编译期常量特化）。
5. **一次性提交**：`ct.launch(stream, grid, kernel, args)`。

#### 4.3.3 源码精读

basic 内核的完整启动调用：

[softmax.py:193-204 — basic 内核的 ct.launch 调用](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L193-L204)

```python
    ct.launch(
        torch.cuda.current_stream(),          # 1) 流
        grid,                                  # 2) grid = (num_programs, 1, 1)
        _softmax_kernel,                       # 3) 内核对象
        (                                      # 4) args 元组，顺序与内核签名一致
            output,
            input,
            n_rows,           # → N_ROWS: ConstInt
            TILE_SIZE,        # → TILE_SIZE: ConstInt
            original_n_cols,  # → DIM_COLS: ConstInt（注意：参数名 DIM_COLS，实参叫 original_n_cols）
        ),
    )
```

对照内核签名 `def _softmax_kernel(output, input, N_ROWS, TILE_SIZE, DIM_COLS)`（[softmax.py:19-25](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L19-L25)），可以看到：

- **顺序必须一致**：args 元组的第 1 个元素填第 1 个参数，依此类推。
- **名字不必一致**：实参叫 `original_n_cols`，形参叫 `DIM_COLS`——`ct.launch` 按位置匹配，不看名字。
- **张量在前、ConstInt 在后**：虽然语言上不强制，但仓库里所有内核都把张量参数放前面、`ConstInt` 放后面，是一种约定俗成。

多波版本的调用更短，但同样是四参：

[softmax.py:213-218 — 多波内核的 ct.launch 调用](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L213-L218)

```python
    ct.launch(
        torch.cuda.current_stream(),
        grid,
        _softmax_kernel_multi_wave_full_row_reg_cached_ldg,
        (output, input, n_cols, TILE_SIZE),    # 对应内核 (output, input, N, TILE_SIZE)
    )
```

> 一个容易踩的坑：args 是**元组**，不是 `**kwargs`。如果你误把参数写成关键字形式（如 `ct.launch(stream, grid, kernel, output=output, ...)`），会因为「仅位置参数」约定直接报错。

#### 4.3.4 代码实践

**实践目标**：通过「阅读 + 配对」练习，掌握 args 元组与内核签名的位置对应关系。

**操作步骤**：

1. 打开 [softmax.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py)，找到 `_softmax_kernel_tma` 的签名（[第 80-87 行](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L80-L87)）和它的启动调用（[第 257-268 行](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L257-L268)）。
2. 在纸上画一张「形参 ↔ 实参」对应表，逐位配对。
3. 检查：实参个数是否与形参个数相等？ConstInt 形参是否都收到了 Python int？

**需要观察的现象**：TMA 内核签名是 `(output, input, N_ROWS, N_COLS, TILE_SIZE)` 共 5 个参数，启动元组也恰好 5 个元素 `(output, input, n_rows, original_n_cols, TILE_SIZE)`，一一对应。

**预期结果**：你能不看答案说出「第 4 位形参 `N_COLS` 接收的是实参 `original_n_cols`」。这是阅读任何 cuTile 启动代码的基本功。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ct.launch` 的第一个参数是 `torch.cuda.current_stream()`，而不是随便一条流或 `None`？

**答案**：用「当前流」能保证内核和 PyTorch 上下文里前后的算子排在同一条队列上，维持正确的执行顺序（比如 softmax 的输入必须是前一个算子写完的）。如果排在另一条流上，就需要额外的同步事件来保序，容易出错。

**练习 2**：args 元组里的 `n_rows` 是 Python `int`，内核里却声明为 `N_ROWS: ConstInt`。这两者是怎么统一的？

**答案**：`ct.launch` 在 JIT 特化内核时，会把元组里对应位置的 `int` 当作编译期常量「烤进」内核，生成一个针对该 `n_rows` 优化的特化版本（这正是 `ConstInt` 的意义，见 u3-l1）。所以主机侧看到的是普通 int，设备侧得到的是编译期常量。

**练习 3**：`grid = (num_programs, 1, 1)` 里后两个 1 能省掉吗（写成 `grid = (num_programs,)`）？

**答案**：不能想当然。仓库里统一用三元组 `(x, y, z)`，这是 CUDA grid 的标准三维表示。`ct.bid(0)`/`ct.num_blocks(0)` 读的是 x 维。是否接受一元组取决于 `ct.launch` 的实现校验——仓库从不冒这个险，始终传完整三元组。遵循既有写法最稳妥。

### 4.4 多启动变体选择

#### 4.4.1 概念说明

softmax 有**四个** `_launch_*` 函数，对应四种内核。但用户调用的只是一个统一入口 `tilegym.ops.softmax(x, use_tma=..., use_chunked=..., use_multi_wave=...)`。谁来根据这些开关选择走哪个启动函数？答案是 `_Softmax.forward`（一个 `torch.autograd.Function`）。

这是一个典型的「**策略分发**」结构：

- 对外：一个签名稳定的函数，靠布尔开关选策略。
- 对内：每个策略是一个独立的 `_launch_*` + 内核组合，互不干扰、可单独调优。

#### 4.4.2 核心流程

`_Softmax.forward` 的选择逻辑（按优先级从高到低）：

```
1. use_multi_wave 为真        → 多波启动（grid = n_rows）
2. use_chunked 为真           → 分块启动（3 遍算法，grid-stride，occupancy=4）
3. use_tma 为真               → TMA 启动（grid-stride，occupancy=2）
4. 否则（全假）               → basic 启动（grid-stride，occupancy=4）
```

另有两个约束/保护：

- `assert not (use_tma and use_chunked)`：TMA 与 chunked 互斥，不能同时开。
- **架构回退**：若 `use_tma=True` 但当前 GPU 的 compute capability < 9（即低于 H100，没有硬件 TMA），打印一条警告并把 `use_tma` 改回 `False`，让它走 basic 路径。这就是为什么 u1-l3 里说「TMA 在低架构上会回退」。

#### 4.4.3 源码精读

TMA 的架构回退保护：

[softmax.py:322-332 — 低架构下 use_tma 被改回 False 并告警](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L322-L332)

```python
        if use_tma and torch.cuda.get_device_capability(x.device)[0] < 9:
            import warnings
            warnings.warn(
                "softmax: use_tma=True has no effect on this GPU (TMA is emulated). "
                "Falling back to use_tma=False. Pass use_tma=False to suppress this.",
                UserWarning, stacklevel=3,
            )
            use_tma = False
```

四路分发主体：

[softmax.py:340-352 — forward 里的四路启动选择](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L340-L352)

```python
        if use_multi_wave:
            _launch_softmax_kernel_multi_wave_full_row_reg_cached_ldg(x, y, TILE_SIZE=TILE_SIZE)
        elif use_chunked:
            _launch_softmax_kernel_chunked(x, y, TILE_SIZE=min(TILE_SIZE, MAX_TILE_SIZE))
        elif use_tma:
            _launch_softmax_kernel_tma(x, y)
        else:
            _launch_softmax_kernel(x, y, TILE_SIZE=TILE_SIZE)
```

注意几个细节：

- **TILE_SIZE 的来源**：`TILE_SIZE = next_power_of_2(n_cols)`（[softmax.py:334](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L334)）。因为 `ct.arange(TILE_SIZE)` 要求编译期 2 的幂（见 u3-l2），所以要把列数向上取整到最近的 2 的幂。
- **chunked 的封顶**：`min(TILE_SIZE, MAX_TILE_SIZE)`，其中 `MAX_TILE_SIZE = 8192`（[softmax.py:335](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L335)）。列数极大时，把每块处理的列数限制在 8192 以内，从而**强制启用分块**（chunked 的意义就在于此）。
- **输出张量由主机侧分配**：`y = torch.empty_like(x)`（[softmax.py:338](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L338)），再把 `y` 作为 output 传进内核。内核只负责填数据，不负责分配。

把四个变体的启动特征汇总成一张表：

| 变体 | 启动函数 | grid | 装饰器 occupancy | 内核循环 | 典型场景 |
| --- | --- | --- | --- | --- | --- |
| basic（默认） | `_launch_softmax_kernel` | `min(NUM_SM*4, n_rows)` | 4 | grid-stride | 通用 |
| multi-wave | `_launch_softmax_kernel_multi_wave_...` | `n_rows` | 无 | 一块一行 | 行数适中、要最大并行 |
| TMA | `_launch_softmax_kernel_tma` | `min(NUM_SM*2, n_rows)` | 2 | grid-stride | H100+，单 tile 装载整行 |
| chunked | `_launch_softmax_kernel_chunked` | `min(NUM_SM*4, n_rows)` | 4 | grid-stride + 三遍 | 列数极大（>8192） |

#### 4.4.4 代码实践

**实践目标**：追踪一次用户调用 `tilegym.ops.softmax(x, use_multi_wave=True)` 是如何最终落到对应启动函数的，验证你对四路分发的理解。

**操作步骤**：

1. 在 `tilegym.ops.softmax`（[softmax.py:356-383](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L356-L383)）里找到 `use_chunked`/`use_multi_wave` 是如何从 `**kwargs` 里取出的。
2. 跟到 `_Softmax.apply(...)`，再到 `forward` 里的 `if use_multi_wave: ...` 分支。
3. 在该分支处**手动模拟**（在脑中或纸上）：如果同时传 `use_tma=True` 和 `use_chunked=True`，会在哪一行 `assert` 报错？

**需要观察的现象**：`softmax` 函数把 `use_chunked`/`use_multi_wave` 从 kwargs 取出后传给 `_Softmax.apply`，而 `use_tma` 是显式形参。四路 `if/elif` 只会命中一条分支。

**预期结果**：

- `use_tma=True, use_chunked=True` → 在 [softmax.py:321](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L321) 的 `assert not (use_tma and use_chunked)` 处抛 `AssertionError`。
- `use_multi_wave=True` 优先级最高，即便同时设了 `use_tma` 也会走多波分支（因为它是第一个 `if`）。

#### 4.4.5 小练习与答案

**练习 1**：用户调用 `tilegym.ops.softmax(x, use_tma=True)`，但机器是 A100（compute capability 8.0）。实际会走哪个内核？

**答案**：走 **basic** 内核。因为 `forward` 里检测到 `get_device_capability(x.device)[0] < 9`，会打印一条 `UserWarning` 并把 `use_tma` 改回 `False`，随后落入 `else` 分支调用 `_launch_softmax_kernel`（basic）。TMA 是 H100+ 才有的硬件特性，低架构上只是模拟，所以代码选择回退。

**练习 2**：为什么 `use_multi_wave` 写在 `if/elif` 的最前面，而不是最后？

**答案**：这定义了优先级。`use_multi_wave` 是「一行一块、不要持久化」的截然不同的调度模式，一旦用户显式要求它，就应当压过 `use_chunked`/`use_tma` 的语义。如果放在最后，用户同时设了多个开关时反而会被前面的分支抢走，行为不符合预期。

**练习 3**：chunked 变体为什么要把 `TILE_SIZE` 和 `MAX_TILE_SIZE=8192` 取 `min`？

**答案**：当 `n_cols` 很大（比如几万）时，`next_power_of_2(n_cols)` 会得到一个非常大的 TILE_SIZE，一次性把整行装进一个 tile 会撑爆寄存器/共享内存。取 `min(..., 8192)` 强制把每块处理的列数限制在 8192，于是 `num_chunks = (N_COLS + TILE_SIZE - 1) // TILE_SIZE` 会大于 1，真正触发「分块三遍」算法。若 `n_cols` 本身小于 8192，`min` 不改变 TILE_SIZE，相当于不分块。

## 5. 综合实践

把本讲的四条主线（grid 计算、contiguous、ct.launch 参数、变体选择）串起来，完成下面这个**源码阅读型综合任务**：

**任务**：为 softmax 假设新增第五种启动变体 `use_persistent_high_occ`（占位设想，仅用于练习，不要真的改源码），要求它用「静态持久化 + occupancy=8」。请写出你需要改动的**所有位置**及改动内容。

**参考答案（仅设计，不落盘）**：

1. **内核**：复制 `_softmax_kernel`，装饰器改成 `@ct.kernel(occupancy=8)`，内核体保持 grid-stride 不变。
2. **启动函数**：新增 `_launch_softmax_kernel_persistent_high_occ`，其中 `num_programs = min(NUM_SM * 8, n_rows)`（注意 8 必须与装饰器 occupancy 一致），`grid = (num_programs, 1, 1)`，记得写 `input = input.contiguous(); output = output.contiguous()`，最后 `ct.launch(torch.cuda.current_stream(), grid, <新内核>, (output, input, n_rows, TILE_SIZE, original_n_cols))`。
3. **forward 分发**：在 `_Softmax.forward` 里加一个形参 `use_persistent_high_occ=False`，并在 `if/elif` 链合适的位置加一个分支调用新启动函数。
4. **对外入口**：在 `softmax(..., **kwargs)` 里加 `use_persistent_high_occ = kwargs.get("use_persistent_high_occ", False)` 并透传给 `_Softmax.apply`。

这个练习综合检验了：你是否理解 occupancy 与启动系数的呼应（模块 4.1）、是否记得 contiguous 保证（模块 4.2）、是否记得 `ct.launch` 的四参顺序与 args 元组（模块 4.3）、以及是否看懂了 forward 的分发结构（模块 4.4）。

## 6. 本讲小结

- **grid 计算**：静态持久化调度的 grid 为 `min(NUM_SM × occupancy, n_rows)`，其中 `occupancy` 必须与内核装饰器 `@ct.kernel(occupancy=K)` 的 K 一致；多波调度则为 `grid = n_rows`，与 NUM_SM 无关。
- **NUM_SM × occupancy 的含义**：`NUM_SM × K` 是「让每个 SM 驻留 K 个块」所需的块数，即填满 GPU 的块数；`min` 防止块数超过行数。
- **contiguous 保证**：每个启动函数都会 `.contiguous()`，因为内核按紧密行主序算内存偏移，非连续视图（切片/转置）会导致读错数据；已连续时零开销。
- **ct.launch 四参**：`(stream, grid, kernel, kernel_args)`，均为位置参数；`kernel_args` 是**元组**，按内核位置参数顺序排列，名字不必一致。
- **变体选择**：`_Softmax.forward` 按 `use_multi_wave → use_chunked → use_tma → basic` 的优先级四路分发；TMA 与 chunked 互斥；低架构（compute capability < 9）下 TMA 自动回退到 basic。
- **TILE_SIZE**：由 `next_power_of_2(n_cols)` 得到（满足 `ct.arange` 的 2 的幂要求），chunked 时再与 `MAX_TILE_SIZE=8192` 取 min 以强制分块。

## 7. 下一步学习建议

本讲把「主机侧怎么启动内核」讲透了，但刻意**没有**深入四种 softmax 内核的算法差异。建议下一步：

- **学习 u3-l4《softmax 内核全解》**：把 basic / TMA / chunked（三遍）/ multi-wave 四种实现的**算法本身**讲清楚——为什么 basic 要减最大值、chunked 为什么要三遍、TMA 的单 tile 装载如何贴合硬件。本讲的启动知识是读懂 u3-l4 的前提。
- **对照阅读 silu_and_mul 的启动**（[silu_and_mul.py:256-261](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L256-L261)）：它用 `grid = (batch_size,)` 的一维 grid，是「一行一块」最朴素的例子，可以当作 multi-wave 的极简版来对照。
- **预告 u5**：当你学到分块 GEMM（u5-l1）和持久化 matmul（u5-l2）时，会再次遇到 `NUM_SM × occupancy` 的 grid 公式和 grid-stride 调度——本讲建立的直觉在那里会直接复用。
