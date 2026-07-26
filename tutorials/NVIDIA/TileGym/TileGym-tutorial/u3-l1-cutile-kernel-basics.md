# cuTile 内核基础：@ct.kernel 与网格索引

## 1. 本讲目标

本讲是 cuTile 内核编程模型的入门。学完后你应该能够：

- 看懂一个 cuTile 内核的整体骨架：装饰器、签名、常量、索引、循环。
- 理解 `@ct.kernel` 装饰器的作用，以及它如何把一段 Python 函数变成 GPU 内核。
- 理解 `ConstInt`（`ct.Constant[int]`）这种「编译期常量」存在的意义。
- 看懂 `ct.bid(0)` / `ct.num_blocks(0)` 这套网格索引，并能解释 grid-stride 循环。
- 理解 `occupancy` 这个编译期提示如何影响 launch 时 grid 的大小。

本讲只讲「内核长什么样、怎么启动」，**不**深入数据搬运原语（load/gather）与启动细节（这些是后续 u3-l2、u3-l3 的内容）。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**什么是 GPU 内核。** 你在 Python 里写一个普通的 `softmax`，CPU 串行地一个数一个数算。GPU 内核（kernel）是另一种写法：你只写「一个线程块（block）怎么处理一小块数据」，GPU 会在成千上万个块上同时执行这份代码。cuTile 就是让你用 Python 写这种「单块代码」的 DSL。

**什么是 tile（瓦片）。** GPU 的全局显存（HBM）很大但很慢，片上存储（寄存器、共享内存）很小但很快。tile 编程模型的核心思路是：把大数据切成一小块一小块的「瓦片」，把一块瓦片搬到片上快速存储里，反复计算完再写回。cuTile 里你看到的 `TILE_SIZE` 就是一块瓦片的大小。

**什么是 block / program。** GPU 启动内核时会创建一个「网格（grid）」，网格由许多「块（block）」组成。每个块内部又有很多线程。cuTile 沿用了和 Triton 类似的术语，把一个块称为一个 **program**：代码里常见的变量 `pid` 就是 "program id"（块号），`num_programs` 就是块的总数。所以 cuTile 的 API 名 `ct.bid`（block id）和惯用变量名 `pid`（program id）指的是同一个东西——块的编号。

> 本讲承接 u1-l3「第一次调用 TileGym 算子」：你已经知道一次 `tilegym.ops.softmax(x)` 会经 `dispatch → _REGISTRY → _Softmax.apply` 最终落到本讲要讲的 `_softmax_kernel`。现在我们打开这个内核，看它内部是怎么写的。

## 3. 本讲源码地图

本讲只涉及两个文件：

| 文件 | 作用 |
| --- | --- |
| `src/tilegym/ops/cutile/softmax.py` | cuTile 版 softmax，含 4 个内核变体与各自 launch 函数。本讲主要看它的 **basic 内核**（grid-stride 调度）和 **multi-wave 内核**（一行一块，作为对照）。 |
| `src/tilegym/ops/cutile/silu_and_mul.py` | cuTile 版 `silu_and_mul`，一个「一行一块」的逐元素内核，用来对照说明不同调度策略。 |

此外 `src/tilegym/ops/cutile/utils.py` 提供辅助函数 `next_power_of_2`，launch 时用于把列数对齐到 2 的幂，本讲会顺带提到。

## 4. 核心概念与源码讲解

### 4.1 `@ct.kernel` 装饰器与内核签名

#### 4.1.1 概念说明

一个 cuTile 内核本质上是一个 **Python 函数**，但它不是被 Python 解释器执行的。`@ct.kernel` 这个装饰器的作用是：把这个函数体交给运行时编译器 `tileiras`，JIT（即时编译）成 GPU 能跑的机器码（PTX/SASS）。

所以执行流程是两段的：

1. **编译期**：`tileiras` 读到这个函数，把它「翻译」成一棵中间表示（Tile IR），再生成 GPU 代码。
2. **运行期**：主机侧调用 `ct.launch(...)`，把编译好的内核在 GPU 上以网格的形式启动。

正因如此，内核函数体里写的 `ct.exp`、`ct.gather` 这些不是普通函数调用，而是「构造 Tile IR 的指令」。函数里也不能写任意 Python（比如不能 `print` 到终端、不能动态 `import`），只能用 cuTile DSL 支持的构造。

内核的**签名**（参数列表）也和普通函数不同，分两类参数：

- **张量参数**（`output`、`input`）：运行期才确定的数据指针，按值传入（传的是指针）。
- **常量参数**（`N_ROWS: ConstInt`）：编译期就确定的标量，会被「烤进」编译出来的内核里（见 4.2）。

#### 4.1.2 核心流程

```text
@ct.kernel(occupancy=4)        # 1. 标记为内核 + 给编译器 occupancy 提示
def _softmax_kernel(            # 2. 签名：先列张量，再列 ConstInt
    output, input,              #    张量参数（运行期值）
    N_ROWS: ConstInt,           #    编译期常量
    ...
):
    pid = ct.bid(0)             # 3. 我是第几号块（program id）
    ...                         # 4. 函数体：用 cuTile DSL 写「一个块怎么干活」
```

主机侧（普通 Python）再调用 `ct.launch(stream, grid, _softmax_kernel, (args...))` 启动它。

#### 4.1.3 源码精读

cuTile 版 softmax 的 basic 内核定义在这里，注意装饰器、签名、以及注释里的 "Static persistent scheduling"：

[src/tilegym/ops/cutile/softmax.py:18-25](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L18-L25) —— 用 `@ct.kernel(occupancy=4)` 定义 basic softmax 内核，签名中 `output/input` 是张量参数，三个 `ConstInt` 是编译期常量。

对比一下没有带 `occupancy` 提示的写法，这是 multi-wave 变体，装饰器只有一个光秃秃的 `@ct.kernel`：

[src/tilegym/ops/cutile/softmax.py:53-60](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L53-L60) —— `@ct.kernel`（无 occupancy 参数），multi-wave 内核：一个块处理一整行。

`silu_and_mul` 的内核也是同样套路，`@ct.kernel` 后跟一段「每个块处理一行」的函数体：

[src/tilegym/ops/cutile/silu_and_mul.py:21-28](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L21-L28) —— `@ct.kernel` 标记 `_silu_and_mul_kernel_row_wise`，签名注释明确写了「每个块算整行输出」。

> 小结：`@ct.kernel` 是 cuTile 内核的「入口标记」，括号里的 `occupancy=` 是可选的编译期提示（4.4 详解）；签名里张量在前、`ConstInt` 在后，是一种约定俗成的写法。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：建立「装饰器 + 签名」的整体印象，不涉及运行。
2. **步骤**：打开 `src/tilegym/ops/cutile/softmax.py`，找到第 18 行的 `_softmax_kernel` 和第 53 行的 `_softmax_kernel_multi_wave_full_row_reg_cached_ldg`。
3. **观察**：两个内核都带 `@ct.kernel`，但只有前者带 `occupancy=4`；两者的参数都是「张量在前、ConstInt 在后」。
4. **预期结果**：你能用一句话说出「`@ct.kernel` 把 Python 函数标记成待 JIT 编译的 GPU 内核，括号里可带编译提示」。

#### 4.1.5 小练习与答案

- **Q1**：`@ct.kernel` 装饰的函数，是 Python 解释器直接执行的吗？
  - **答**：不是。它被运行时编译器 `tileiras` JIT 编译成 GPU 代码，再由 `ct.launch` 在 GPU 上启动；函数体里的 `ct.exp` 等是在构造 Tile IR 指令。
- **Q2**：内核签名里的参数分哪两类？
  - **答**：张量参数（运行期数据指针，如 `output`、`input`）和编译期常量（如 `N_ROWS: ConstInt`）。

---

### 4.2 `ConstInt` 编译期常量

#### 4.2.1 概念说明

`ConstInt` 是项目里的一个类型别名：

```python
ConstInt = ct.Constant[int]
```

当一个内核参数标注成 `param: ConstInt`，它的含义是：**这个值在编译期就已知，会被「烤进」编译出来的内核里**。

为什么要这样做？GPU 内核的性能极度依赖「形状是否已知」。例如 `ct.arange(TILE_SIZE)` 要分配一个长度为 `TILE_SIZE` 的瓦片，如果 `TILE_SIZE` 在编译期已知，编译器就能精确分配寄存器、展开循环、做静态边界检查；如果未知，就只能走通用的、慢得多的动态路径。

这和 Triton 的 `tl.constexpr`、CUDA C++ 的模板常量是同一个思想：**把「形状/大小」这类值变成编译期常量，换取更优的代码**。

代价是：每一个不同的 `TILE_SIZE` 取值，编译器都会生成一份**单独**的内核（即「特化」），这也是为什么后面（u3-l3、u5-l3）会看到 launch 函数里要把列数对齐到 2 的幂（`next_power_of_2`）——为了减少特化版本的数量。

#### 4.2.2 核心流程

```text
主机侧:  N_ROWS, TILE_SIZE, DIM_COLS 都是普通 int
          │
          ▼  ct.launch(..., (output, input, n_rows, TILE_SIZE, original_n_cols))
          │  这些 int 值被「代入」签名里的 ConstInt 参数
          ▼
编译期:  tileiras 用这些具体数值特化内核
          │  TILE_SIZE=1024 → 生成一份内核
          │  TILE_SIZE=2048 → 生成另一份内核
          ▼
运行期:  在 GPU 上启动特化好的内核
```

#### 4.2.3 源码精读

类型别名定义在两个文件顶部，完全一致：

[src/tilegym/ops/cutile/softmax.py:15](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L15) —— `ConstInt = ct.Constant[int]`，把 `ct.Constant[int]` 取一个短名。

签名里把它用作编译期常量的类型标注：

[src/tilegym/ops/cutile/softmax.py:19-25](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L19-L25) —— `N_ROWS`、`TILE_SIZE`、`DIM_COLS` 三个 `ConstInt`，其中 `TILE_SIZE` 直接决定 `ct.arange(TILE_SIZE)` 的瓦片长度，必须在编译期已知。

然后在函数体里，`TILE_SIZE` 被直接喂给 `ct.arange` 来生成瓦片长度的偏移索引：

[src/tilegym/ops/cutile/softmax.py:29](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L29) —— `offsets = ct.arange(TILE_SIZE, dtype=ct.int32)`，因为 `TILE_SIZE` 是编译期常量，编译器能精确分配这个瓦片。

主机侧的 launch 函数则负责把这些 `ConstInt` 的具体数值传进去（注意它们出现在 args 元组的后半段，和张量一起传）：

[src/tilegym/ops/cutile/softmax.py:193-204](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L193-L204) —— `ct.launch(stream, grid, _softmax_kernel, (output, input, n_rows, TILE_SIZE, original_n_cols))`，三个 int 按 `ConstInt` 参数顺序传入。

> 小结：`ConstInt` = `ct.Constant[int]` = 编译期已知的整型常量。它让编译器能针对具体数值特化内核、精确分配瓦片。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：理解「同一个内核、不同 `TILE_SIZE` 会生成不同特化版本」。
2. **步骤**：读 `_launch_softmax_kernel`（softmax.py:173-204），注意 `TILE_SIZE` 默认 `1024`，但由 `next_power_of_2(n_cols)` 计算后传入。
3. **观察**：`n_cols=2048` 时 `TILE_SIZE=2048`；`n_cols=1009` 时 `TILE_SIZE=1024`（对齐到 2 的幂）。
4. **预期结果**：能说出「对齐到 2 的幂减少了特化版本数，从而减少 JIT 编译次数」。

#### 4.2.5 小练习与答案

- **Q1**：为什么 `TILE_SIZE` 必须是 `ConstInt`，而 `input` 张量不能是？
  - **答**：`TILE_SIZE` 决定瓦片大小（`ct.arange(TILE_SIZE)`），编译期已知才能精确分配寄存器、展开循环；张量是运行期数据指针，每次启动都可能指向不同数据，无法也不应烤进内核。
- **Q2**：如果不对齐到 2 的幂，让 `TILE_SIZE` 取任意值，会有什么后果？
  - **答**：几乎每个不同的列宽都会触发一次新的 JIT 特化编译，启动变慢、缓存膨胀；对齐到 2 的幂把无数取值收敛到少数几个，大幅减少特化版本。

---

### 4.3 网格索引与 grid-stride 循环（`bid` / `num_blocks`）

> 本节包含本讲的主实践任务。

#### 4.3.1 概念说明

GPU 内核启动时，会创建 `num_programs` 个块，每个块拿到一个编号。cuTile 用两个原语告诉你「我是谁、一共多少个」：

- `ct.bid(0)`：当前块在网格第 0 维上的编号（block id / program id），取值范围 `[0, num_blocks(0))`。
- `ct.num_blocks(0)`：网格第 0 维上的块总数。

这两者对应 CUDA 里的 `blockIdx.x` 和 `gridDim.x`，也对应 Triton 里的 `tl.program_id(0)` 和网格大小。网格是三维的 `(x, y, z)`，本讲的 softmax/silu 都只用第 0 维（`grid = (num_programs, 1, 1)`）。

有了「我是第 p 号块、一共 P 个块」，最自然的两种调度策略是：

1. **一行一块**（multi-wave / 一波一波）：`grid = (N_ROWS,)`，第 p 号块就处理第 p 行。块数等于行数。silu_and_mul 和 softmax multi-wave 用这种。
2. **grid-stride 循环**（静态持久化调度）：`grid = (P,)` 其中 `P < N_ROWS`，每个块用 `for row_idx in range(pid, N_ROWS, P)` 跨步地处理多行。softmax basic 用这种。

策略 2 就是本讲主实践要解释的 **grid-stride 循环**，也叫**静态持久化调度（static persistent scheduling）**。

#### 4.3.2 核心流程

grid-stride 循环的工作方式：

```text
pid = ct.bid(0)            # 我是第 pid 号块
num_programs = ct.num_blocks(0)   # 一共 num_programs 个块

for row_idx in range(pid, N_ROWS, num_programs):
    处理第 row_idx 行
```

第 `pid` 号块处理哪些行？

\[ \text{rows}_p = \{\, p,\; p + P,\; p + 2P,\; \dots \,\} \quad\text{其中 } P = num\_programs \]

即从 `pid` 出发，每次步进 `num_programs`，直到越过 `N_ROWS`。每个块处理的行数约为：

\[ \text{rows\_per\_block} \approx \left\lceil \frac{N\_ROWS}{P} \right\rceil \]

这种调度被称为「**静态持久化**」的两层含义：

- **持久化（persistent）**：块数 `P` 远小于行数 `N_ROWS` 时，一个块不会只干一行就退出，而是「赖在」SM 上持续处理多行，复用已分配的寄存器/共享内存等资源。
- **静态（static）**：哪个块处理哪些行，在**启动时**（launch）就由循环的起点 `pid` 和步长 `num_programs` 完全确定，运行期不再做动态分配。

对照的「一行一块」策略：`grid = (N_ROWS,)`，块数等于行数，每个块只处理一行——简单直接，但当行数非常多时块数会很大。

#### 4.3.3 源码精读

softmax basic 内核就是教科书式的 grid-stride 写法：

[src/tilegym/ops/cutile/softmax.py:26-31](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L26-L31) —— 注释写明 "Static persistent scheduling: each block processes multiple rows"；`pid = ct.bid(0)` 取块号，`num_programs = ct.num_blocks(0)` 取块总数，`for row_idx in range(pid, N_ROWS, num_programs)` 跨步遍历多行。

同一个文件里的 multi-wave 内核则是「一行一块」的对照样本：

[src/tilegym/ops/cutile/softmax.py:64-66](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L64-L66) —— multi-wave 内核里 `row_idx = ct.bid(0)`，块号直接等于行号，**没有** `for ... range(pid, N, num_programs)` 循环。

silu_and_mul 同样是「一行一块」：

[src/tilegym/ops/cutile/silu_and_mul.py:28-33](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L28-L33) —— `bid = ct.bid(0)` 注释明说 "this gives us our row"，`row_idx = bid`，块号即行号。

主机侧 launch 时，「一行一块」策略体现为 `grid = (batch_size,)`（块数 = 行数）：

[src/tilegym/ops/cutile/silu_and_mul.py:255-256](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L255-L256) —— `grid = (batch_size,)` 然后启动 `_silu_and_mul_kernel_row_wise`，每个块对应一行。

而 grid-stride 策略则体现为 `grid = (num_programs, 1, 1)`，其中 `num_programs = min(NUM_SM * 4, n_rows)`（见 4.4）：

[src/tilegym/ops/cutile/softmax.py:190-191](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L190-L191) —— `num_programs = min(NUM_SM * 4, n_rows)` 后 `grid = (num_programs, 1, 1)`，块数通常远小于行数。

> 小结：`ct.bid(0)` / `ct.num_blocks(0)` 给出块号与块总数；softmax basic 用 `range(pid, N_ROWS, num_programs)` 做 grid-stride（静态持久化），silu 和 softmax multi-wave 用 `row_idx = bid` 做一行一块。

#### 4.3.4 代码实践（本讲主任务）

1. **实践目标**：亲手在 softmax basic 内核里定位 `pid`、`num_programs` 和 grid-stride 循环，并用自己的话解释「静态持久化调度」。
2. **操作步骤**：
   1. 打开 `src/tilegym/ops/cutile/softmax.py`，定位第 27–31 行。
   2. 找到三处：`pid = ct.bid(0)`（27 行）、`num_programs = ct.num_blocks(0)`（28 行）、`for row_idx in range(pid, N_ROWS, num_programs)`（31 行）。
   3. 假设 `NUM_SM = 132`、`n_rows = 256`，按 4.4 的公式算出 `num_programs = min(132*4, 256) = 256`。此时块数等于行数，每个块其实只跑一行——观察 grid-stride 循环在「块数 ≥ 行数」时如何优雅退化为「一行一块」（`range(pid, 256, 256)` 对每个 `pid` 恰好只取 `pid` 本身）。
   4. 再假设 `n_rows = 4096`：`num_programs = min(528, 4096) = 528`，则第 0 号块处理第 0、528、1056、… 行。
3. **需要观察的现象**：
   - `pid` 是每个块自己的编号，`num_programs` 是所有块共享的「步长」。
   - 循环用 `range(start, stop, step)` 的标准 Python 语义：起点 `pid`、步长 `num_programs`、终点 `N_ROWS`。
4. **预期结果**：你能解释「持久化」=块复用资源处理多行、「静态」=分工在 launch 时定死。
5. **运行验证（可选，需可用 GPU 与 cutile 后端）**：
   ```python
   import torch, tilegym
   tilegym.set_backend("cutile")
   x = torch.rand(4096, 2048, device="cuda", dtype=torch.float32)
   y = tilegym.ops.softmax(x)  # 走 basic 内核（use_tma/use_chunked/use_multi_wave 全 False）
   ref = torch.nn.functional.softmax(x, dim=-1)
   print((y - ref).abs().max().item())  # 应在 ~1e-6 量级
   ```
   若本机无可用的 cutile 后端，本实践以「源码阅读 + 手算分工」为准，运行部分标注「待本地验证」。

#### 4.3.5 小练习与答案

- **Q1**：`ct.bid(0)` 和 `ct.num_blocks(0)` 里的 `0` 指什么？
  - **答**：网格的第 0 维。网格是三维的 `(x, y, z)`，本讲内核只用第 0 维（`grid` 形如 `(N, 1, 1)` 或 `(N,)`），所以都传 `0`，对应 CUDA 的 `blockIdx.x` / `gridDim.x`。
- **Q2**：当 `num_programs >= N_ROWS` 时，grid-stride 循环退化成什么？
  - **答**：`range(pid, N_ROWS, num_programs)` 在 `num_programs >= N_ROWS` 时对每个 `pid < N_ROWS` 只取到 `pid` 自身，等价于「一行一块」——这正是 `min(NUM_SM*4, n_rows)` 中 `n_rows` 较小时的情形。

---

### 4.4 `occupancy`：从编译期提示到 grid 大小

#### 4.4.1 概念说明

`occupancy`（占用率）是 GPU 编程里衡量「一个 SM（流式多处理器）上能同时驻留多少个块」的指标。SM 越能容纳多的块，越能通过块间切换来掩盖访存延迟（一个块在等显存时，SM 可以执行另一个块的计算）。

在 cuTile 里，`@ct.kernel(occupancy=N)` 是一个**给编译器的提示**：请把内核编译成「每个 SM 上能同时跑 N 个块」的形态。这会影响编译器对寄存器用量、瓦片大小的取舍——占用率越高，通常要求每块用更少的寄存器/共享内存。

这个提示最直接的下游影响，就是 **launch 时的 grid 大小**。host 侧会按

\[ num\_programs = \min(\, NUM\_SM \times occupancy,\; N\_ROWS \,) \]

来计算要启动多少个块，让每个 SM 恰好分到约 `occupancy` 个块。这里：

- `NUM_SM = multi_processor_count`，GPU 的 SM 总数，由硬件决定。
- `occupancy` 就是内核装饰器里的那个值（basic softmax 是 `4`，TMA 版是 `2`）。
- `n_rows` 是「行」这一维的工作量上限，避免启动超过工作量的块。

把 4.3 和 4.4 串起来：`occupancy`（编译期）→ `num_programs`（launch 时）→ grid 大小 → 内核里的 `num_blocks(0)` → grid-stride 循环的步长。这就是一条完整的「从编译提示到调度循环」的链路。

#### 4.4.2 核心流程

```text
@ct.kernel(occupancy=4)            # 编译期：告诉编译器目标占用率
        │
        ▼  tileiras 编译内核，约束寄存器/瓦片以让「每 SM 4 块」可行
host:  NUM_SM = 多处理器数
       num_programs = min(NUM_SM * 4, n_rows)
       grid = (num_programs, 1, 1)
        │
        ▼  ct.launch(stream, grid, kernel, args)
kernel: num_programs = ct.num_blocks(0)   # 等于 grid 的第 0 维
        for row_idx in range(pid, N_ROWS, num_programs):  # 步长 = num_programs
```

#### 4.4.3 源码精读

装饰器里的 `occupancy=4`：

[src/tilegym/ops/cutile/softmax.py:18](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L18) —— `@ct.kernel(occupancy=4)`，给编译器的占用率提示。

host 侧按 `occupancy` 算 grid（注意 `4` 与上面装饰器里的 `4` 一一对应）：

[src/tilegym/ops/cutile/softmax.py:189-191](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L189-L191) —— `NUM_SM = ...multi_processor_count`，`num_programs = min(NUM_SM * 4, n_rows)`，`grid = (num_programs, 1, 1)`。

对比 TMA 版，装饰器里 occupancy 不同（`2`），launch 时乘的系数也随之不同：

[src/tilegym/ops/cutile/softmax.py:80-81 与 252-254](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L80-L81) —— TMA 内核用 `@ct.kernel(occupancy=2)`，对应 launch 里 `num_programs = min(NUM_SM * 2, n_rows)`（见 softmax.py:252-254）。

> 小结：`occupancy` 是编译期提示，决定「每 SM 多少块」可行；launch 函数用 `min(NUM_SM * occupancy, n_rows)` 把它变成实际 grid 大小，再喂给内核里的 `ct.num_blocks(0)`。装饰器里的 `N` 和 launch 里的 `* N` 必须保持一致。

#### 4.4.4 代码实践（源码阅读型）

1. **目标**：验证「装饰器 occupancy 与 launch 系数一一对应」。
2. **步骤**：
   1. 在 softmax.py 里找到三处：basic 内核 `@ct.kernel(occupancy=4)`（18 行）与 launch `min(NUM_SM * 4, n_rows)`（190 行）；TMA 内核 `@ct.kernel(occupancy=2)`（80 行）与 launch `min(NUM_SM * 2, n_rows)`（253 行）。
   2. 自检 GPU 的 SM 数：`torch.cuda.get_device_properties(0).multi_processor_count`（待本地验证）。
3. **预期结果**：你能解释「basic 软最大启动 4 倍 SM 数量的块、TMA 软最大 2 倍」，并理解这直接来自装饰器里的占用率提示。

#### 4.4.5 小练习与答案

- **Q1**：如果把装饰器写成 `@ct.kernel(occupancy=4)`，但 launch 里写 `min(NUM_SM * 2, n_rows)`，会有什么问题？
  - **答**：内核按「每 SM 4 块」编译（占用较少寄存器），但实际只启动「每 SM 2 块」，浪费了硬件可容纳的并发，掩盖延迟的能力下降，性能通常变差。两者应保持一致。
- **Q2**：为什么公式里要 `min(..., n_rows)`？
  - **答**：`n_rows` 是总工作量。当行数少于 `NUM_SM*occupancy` 时，启动超过行数的块没有意义（多余块通过 `range` 循环什么也做不了），用 `n_rows` 封顶避免空转。

---

## 5. 综合实践

把本讲四个模块串成一个任务：**读懂 basic softmax 内核的「装饰器 → 常量 → 索引 → grid」全链路，并画出分工表。**

1. 阅读 `src/tilegym/ops/cutile/softmax.py` 的 `_softmax_kernel`（18–50 行）与 `_launch_softmax_kernel`（173–204 行）。
2. 用一张表填出（手算，假设 `NUM_SM = 132`）：

   | 场景 | `n_rows` | `occupancy` | `num_programs` | 每个 `pid` 处理的行数 |
   | --- | --- | --- | --- | --- |
   | 小张量 | 100 | 4 | ? | ? |
   | 中张量 | 1024 | 4 | ? | ? |
   | 大张量 | 100000 | 4 | ? | ? |

3. 验证你的分工：对任一 `pid`，`range(pid, n_rows, num_programs)` 覆盖的行数应约等于你算的「每个 pid 处理的行数」，且所有 pid 合起来恰好覆盖 `[0, n_rows)`。
4. （可选运行验证）在可用 cutile 后端的机器上，用 4.3.4 的脚本跑一遍 softmax，确认数值正确；并试着把 `n_rows` 调到 100000 观察仍能正常计算（grid-stride 循环对任意行数都成立）。运行结果「待本地验证」。

**参考答案**（`NUM_SM=132`，`num_programs = min(132*4, n_rows) = min(528, n_rows)`）：

| 场景 | `n_rows` | `occupancy` | `num_programs` | 每个 pid 处理的行数（约） |
| --- | --- | --- | --- | --- |
| 小张量 | 100 | 4 | 100（被 n_rows 封顶） | 1 |
| 中张量 | 1024 | 4 | 528 | ⌈1024/528⌉ = 2 |
| 大张量 | 100000 | 4 | 528 | ⌈100000/528⌉ ≈ 190 |

## 6. 本讲小结

- `@ct.kernel`（可带 `occupancy=N` 提示）把一个 Python 函数标记成待 `tileiras` JIT 编译的 GPU 内核；函数体用 cuTile DSL 构造 Tile IR。
- 内核签名分两类参数：张量（运行期数据指针）和 `ConstInt`（`ct.Constant[int]`，编译期常量）；`TILE_SIZE` 之所以是 `ConstInt`，是为了让 `ct.arange` 等能精确分配瓦片。
- `ct.bid(0)` / `ct.num_blocks(0)` 给出块号与块总数（对应 CUDA 的 `blockIdx.x` / `gridDim.x`、Triton 的 `tl.program_id`）。
- softmax basic 用 `for row_idx in range(pid, N_ROWS, num_programs)` 做 **grid-stride 循环**，即**静态持久化调度**：块数 `P = num_programs` 通常小于行数，每个块跨步处理多行、复用资源；silu 和 softmax multi-wave 则用「一行一块」。
- `occupancy` 是编译期提示，launch 函数用 `num_programs = min(NUM_SM * occupancy, n_rows)` 把它变成 grid 大小；装饰器里的 `N` 与 launch 里的 `* N` 一一对应。

## 7. 下一步学习建议

- **u3-l2 数据搬运原语**：本讲刻意没讲 `ct.gather`/`ct.scatter`/`ct.load`/`ct.store`/`ct.arange` 的细节，这些是下一讲的主题，讲完你就能完全读懂 softmax 内核函数体的每一行。
- **u3-l3 内核启动模式**：深入 `_launch_softmax_kernel` 的 grid 计算、`contiguous` 保证、`ct.launch` 的三参数约定，以及四种 softmax 变体的选择逻辑。
- **u3-l4 softmax 全解**：把本讲的 basic 内核与 TMA、chunked、multi-wave 三种实现整体对比，并用 `torch.autograd.Function` 串起前向。
- 继续阅读源码：`src/tilegym/ops/cutile/silu_and_mul.py` 的 `_silu_and_mul_kernel_row_wise` 是理解「一行一块」调度最干净的样本，建议通读一遍。
