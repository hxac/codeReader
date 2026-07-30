# 跑通第一个 kernel：vector-add 教程

## 1. 本讲目标

本讲是「认识 Triton-Ascend 与第一次运行」单元的收尾，目标是用一个最小的、能跑通的例子，把前几讲建立的「架构地图」变成手上的肌肉记忆。学完本讲你应该能够：

- 在 Ascend NPU 上独立运行 `tutorials/01-vector-add.py`，并看懂它的输出。
- 理解 `@triton.jit` 装饰器如何把一段普通 Python 函数变成 Triton kernel。
- 理解 `grid` 与 `tl.program_id` 如何把一整条向量切分给多个并发的 program（程序实例）去处理。
- 理解 `tl.load` / `tl.store` 的「块指针 + 掩码」访存写法，以及为什么需要 `mask`。
- 掌握用 PyTorch 原生结果校验 Triton kernel 正确性的标准做法。

本讲承接 u1-l1（架构总图）、u1-l2（分层）、u1-l3（安装与构建）。u1-l3 已经把环境装好，本讲就在这个环境上点亮第一个 kernel。

## 2. 前置知识

在阅读源码前，先用通俗语言建立几个直觉。

### 什么是「kernel」

在 GPU/NPU 编程里，「kernel」指的是一段会在硬件上被成千上万个并发执行单元同时运行的函数。你只写一份代码，但同一个函数会被「很多个 program」同时跑，每个 program 处理数据的不同切片。Triton 的编程模型就是：你写一个处理「一个数据块（block）」的函数，Triton 负责把它扩展到很多块上去。

### 什么是「block」「grid」「program」

- **block（块）**：一次 kernel 调用里，一个 program 处理的那一小段数据。vector-add 里每个 program 处理 `BLOCK_SIZE` 个元素。
- **grid（网格）**：一次启动里总共有多少个 program。它通常用一个元组表示，比如 `(num_blocks,)` 表示一维网格。
- **program（程序实例）**：grid 中的一个执行实例，用一个编号 `program_id` 区分自己。

### 什么是「指针 + 偏移 + 掩码」

NPU 上的内存（DRAM）是按地址连续存放的。要读取一段数据，你需要：
1. 一个基地址（指针 `x_ptr`）；
2. 一组相对基地址的偏移（`offsets`，比如 `0,1,2,...,1023`）；
3. 一个掩码（`mask`），用来屏蔽掉「超出真实数据长度」的那些位置——否则访问到非法地址会出错。

这三样东西凑在一起，就是 Triton 里 `tl.load(x_ptr + offsets, mask=mask)` 的含义。

### 为什么 vector-add 适合做第一个例子

逐元素相加 `output[i] = x[i] + y[i]` 没有复杂的依赖，每个元素的输出只依赖同下标的两个输入，天然适合「把数据切成块、多个 program 并行处理」的模型，所以它被几乎所有加速计算框架（包括社区 Triton）当作入门第一例。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [third_party/ascend/tutorials/01-vector-add.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/tutorials/01-vector-add.py) | 本讲的主角：一个完整的、可在 Ascend NPU 上运行的 vector-add 示例，包含 kernel、启动器、校验三部分。 |
| [docs/en/quick_start.md](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/quick_start.md) | 官方快速上手文档，说明如何运行本例以及期望输出。 |
| [python/triton/runtime/jit.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/runtime/jit.py) | Triton core：`@triton.jit` 装饰器、`JITFunction`、`KernelInterface.__getitem__`，决定 kernel 如何被编译与启动。 |
| [python/triton/language/core.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/language/core.py) | Triton core：`program_id`、`arange`、`load`、`store` 等语言原语的定义。 |
| [python/triton/__init__.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/__init__.py) | Triton core：`cdiv`（向上取整除法）工具函数，用于计算 grid 大小。 |

注意一个 u1-l2 强调过的点：本例本身在 `third_party/ascend/` 下（属于 Ascend 亲和代码），但它调用的 `@triton.jit`、`tl.load` 等全部来自 Triton core（`python/`）。这正是「Ascend 例子复用 core 通用语法」这一分层原则的体现。

## 4. 核心概念与源码讲解

本讲按三个最小模块拆分：`@triton.jit`、`grid 与 program_id`、`tl.load/tl.store`。三者串起来就是 vector-add 的完整执行链路。

### 4.1 `@triton.jit`：把 Python 函数变成 kernel

#### 4.1.1 概念说明

`@triton.jit` 是一个装饰器（decorator）。把它加在一个普通 Python 函数上面，这个函数就不再是「直接用 Python 解释器执行」的函数，而是变成一个「待编译的 kernel 描述」。Triton 会在你第一次用它去处理真实数据时，把它编译成目标硬件（这里是 Ascend NPU）能执行的二进制。

关键点：`@triton.jit` 本身是**目标无关**的——它不关心你要跑在 GPU 还是 NPU。真正决定「编译成什么」的是运行时被选中的后端（Ascend 后端，见 u3）。所以同一段 `@triton.jit` kernel，换一个 device 就能跑在别的硬件上，这也是 Triton-Ascend「兼容社区 Triton 语法」的根源。

#### 4.1.2 核心流程

当你写下 `add_kernel[grid](...)` 时，Triton 内部大致经历：

1. `@triton.jit` 把 `add_kernel` 包成一个 `JITFunction` 对象（而不是普通函数）。
2. `add_kernel[grid]` 触发 `KernelInterface.__getitem__`，它返回一个「记住了 grid」的可调用代理。
3. 调用这个代理 `(...)` 时，最终进入 `JITFunction.run(grid=..., ...)`。
4. `run` 根据「函数源码 + 参数类型 + 编译选项」算出一个缓存键：命中缓存就直接复用，没命中就把源码编译成 TTIR，再交给后端（Ascend）继续 lowering，最终启动。

伪代码：

```
@triton.jit
def add_kernel(...):        # 此时 add_kernel 已是 JITFunction 对象
    ...

add_kernel[grid](args)      # 等价于 KernelInterface.__getitem__(grid) → 代理
                            # 代理(args) → JITFunction.run(grid=grid, args)
                            #   → 查缓存 / 编译 TTIR → Ascend 后端 lowering → 启动
```

#### 4.1.3 源码精读

先看示例里的 kernel 定义。`@triton.jit` 装饰函数，参数里有指针、长度，还有一个特殊的 `BLOCK_SIZE: tl.constexpr`：

[third_party/ascend/tutorials/01-vector-add.py:49-56](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/tutorials/01-vector-add.py#L49-L56) —— 用 `@triton.jit` 声明 kernel；`x_ptr/y_ptr/output_ptr` 是三个输入输出向量的指针，`n_elements` 是向量长度，`BLOCK_SIZE: tl.constexpr` 是「编译期常量」（每个 program 处理的元素数），标注 `constexpr` 才能把它当作形状值参与编译。

`@triton.jit` 装饰器本身在 core 里只是一个返回 `JITFunction` 的工厂。当未启用解释器模式时，它把原函数包成 `JITFunction`：

[python/triton/runtime/jit.py:893-903](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/runtime/jit.py#L893-L903) —— `jit` 的最终实现：如果开启了运行时解释模式（`knobs.runtime.interpret`）就返回 `InterpretedFunction`（见 u10 解释器调试），否则返回 `JITFunction`。这一处分支也是后面 u10 调试讲义会用到「解释器模式」的入口。

而「`add_kernel[grid]`」这种方括号语法，来自 `KernelInterface.__getitem__`：

[python/triton/runtime/jit.py:364-370](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/runtime/jit.py#L364-L370) —— `__getitem__` 记住 `grid`，返回一个 lambda，调用它时带着 `warmup=False` 进入 `self.run(grid=grid, ...)`。这正是「`fn[grid](*args)`」语法的实现。

> 说明：真正的编译触发点（生成 TTIR）藏在 `JITFunction.run` 内部，属于 u3-l1「`@triton.jit` 与编译入口」的精读内容。本讲只需记住：`run` 会按需编译并缓存，第一次调用较慢（编译），后续调用命中缓存就很快。

#### 4.1.4 代码实践

**实践目标**：在 NPU 上跑通本例，确认 `@triton.jit` kernel 能被正确编译并执行。

**操作步骤**（来自官方 quick_start）：

1. 确保 CANN 环境变量已加载（u1-l3）：
   ```bash
   source /usr/local/Ascend/ascend-toolkit/set_env.sh
   ```
2. 运行示例：
   ```bash
   python3 ./third_party/ascend/tutorials/01-vector-add.py
   ```
   对应官方文档的运行说明见 [docs/en/quick_start.md:45-52](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/quick_start.md#L45-L52)。

**需要观察的现象**：终端会打印两段几乎相同的张量，以及一行「最大误差」。

**预期结果**（按官方文档给出的输出）：

```
tensor([0.8329, 1.0024, 1.3639,  ..., 1.0796, 1.0406, 1.5811], device='npu:0')
tensor([0.8329, 1.0024, 1.3639,  ..., 1.0796, 1.0406, 1.5811], device='npu:0')
The maximum difference between torch and triton is 0.0
```

对应 [docs/en/quick_start.md:56-60](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/quick_start.md#L56-L60)。出现这个输出即说明环境正确。

> 说明：本编写环境没有 Ascend NPU，无法实跑；以上为官方文档记载的期望输出，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `@triton.jit` 去掉，直接调用 `add_kernel[grid](...)` 会发生什么？

> **答案**：`add_kernel` 会变回普通 Python 函数，没有 `KernelInterface.__getitem__`，方括号语法 `add_kernel[grid]` 会触发 `TypeError`（函数不支持取下标），更不会触发任何编译。

**练习 2**：为什么 `BLOCK_SIZE` 要标注成 `tl.constexpr`，而 `n_elements` 不用？

> **答案**：`BLOCK_SIZE` 会在 kernel 内部被当作「张量形状」使用（`tl.arange(0, BLOCK_SIZE)` 要在编译期知道长度），必须是编译期常量，所以用 `constexpr`。`n_elements` 是运行时才已知的数值（参与 `offsets < n_elements` 比较），不需要也无法在编译期固定，所以是普通参数。

---

### 4.2 `grid` 与 `program_id`：把数据切分给多个 program

#### 4.2.1 概念说明

一个 `@triton.jit` kernel 描述的是「一个 program 怎么处理一个块」。但真实数据有上万个元素，一个块装不下，也不够并行。`grid` 就是用来告诉 Triton「这次启动一共开多少个 program」，`program_id` 则让每个 program 知道「我是第几号，该处理哪一段数据」。

vector-add 的切分思路是：把长度为 `n_elements` 的向量，按 `BLOCK_SIZE` 一块一块地切，切成若干块，每块交给一个 program。块数就是 grid 的大小。

#### 4.2.2 核心流程

1. 计算 grid 大小（块数）= 向上取整除法 `cdiv(n_elements, BLOCK_SIZE)`。
2. 每个 program 用 `pid = tl.program_id(axis=0)` 取到自己的编号。
3. 计算本块的起点 `block_start = pid * BLOCK_SIZE`。
4. 用 `tl.arange(0, BLOCK_SIZE)` 生成块内相对偏移 `0..BLOCK_SIZE-1`。
5. 绝对偏移 `offsets = block_start + 相对偏移`，这就是本 program 要读写的那些元素下标。

举个具体例子（来自示例注释）：向量长度 256，`BLOCK_SIZE=64`，那么共有 4 个 program，分别处理 `[0:64)`、`[64:128)`、`[128:192)`、`[192:256)`。

向上取整除法的定义：

\[
\text{cdiv}(n, B) = \left\lceil \frac{n}{B} \right\rceil = \left\lfloor \frac{n + B - 1}{B} \right\rfloor
\]

代码里写成整数表达式 `(n + B - 1) // B`。

#### 4.2.3 源码精读

kernel 里取 program 编号：

[third_party/ascend/tutorials/01-vector-add.py:59](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/tutorials/01-vector-add.py#L59) —— `pid = tl.program_id(axis=0)`：用一维网格，所以 `axis=0`。每个 program 拿到一个不同的 `pid`，从而处理不同的数据块。

计算本块的起点和偏移：

[third_party/ascend/tutorials/01-vector-add.py:64-65](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/tutorials/01-vector-add.py#L64-L65) —— `block_start = pid * BLOCK_SIZE` 算出本块在整条向量里的起点；`offsets = block_start + tl.arange(0, BLOCK_SIZE)` 把起点加上块内偏移，得到本 program 要访问的绝对下标数组。

grid 怎么来的？看启动器 `add`：

[third_party/ascend/tutorials/01-vector-add.py:85-86](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/tutorials/01-vector-add.py#L85-L86) —— `grid = lambda meta: (triton.cdiv(n_elements, meta['BLOCK_SIZE']), )`：grid 是一个「接收 meta、返回元组」的函数；Triton 在启动前会调用它，把 `BLOCK_SIZE=1024` 作为 `meta['BLOCK_SIZE']` 传进去，算出 program 总数。最后用 `add_kernel[grid](...)` 启动。

`cdiv` 与 `program_id` 在 core 里的定义。`cdiv` 就是向上取整除法：

[python/triton/__init__.py:67-68](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/__init__.py#L67-L68) —— `cdiv(x, y)` 返回 `(x + y - 1) // y`，正是上面的公式。

`program_id` 接收一个轴（0/1/2，对应三维网格），返回当前 program 在该轴上的编号：

[python/triton/language/core.py:1605-1620](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/language/core.py#L1605-L1620) —— `program_id(axis)` 最终调用语义层的 `_semantic.program_id(axis)`。注意文档注释说 axis 必须是 0、1 或 2（三维网格）。

语义层对 axis 做合法性校验并生成取编号的 IR：

[python/triton/language/semantic.py:38-41](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/language/semantic.py#L38-L41) —— 若 axis 不在 `(0,1,2)` 则报错；否则用 builder 创建一条 `create_get_program_id(axis)` 指令，返回 `int32` 类型的张量。

> 补充：u2 会专门讲 NPU 上「grid 大小/并发任务数」还要结合 AI 核数、Vector 核数来规划；本讲先按通用 Triton 模型理解，把 grid 当作「块数」即可。

#### 4.2.4 代码实践

**实践目标**：观察「改变向量长度会改变 program 总数」，从而直观理解 grid。

**操作步骤**：

1. 打开 `third_party/ascend/tutorials/01-vector-add.py`，定位到 `test_vector_addition` 里的 `size = 98432`（[第 96 行](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/tutorials/01-vector-add.py#L96)）。
2. 在 `add` 函数里，临时加一行打印，求出实际 program 数（这是「示例代码」，原文件没有这行）：
   ```python
   def add(x: torch.Tensor, y: torch.Tensor):
       output = torch.empty_like(x)
       n_elements = output.numel()
       grid = lambda meta: (triton.cdiv(n_elements, meta['BLOCK_SIZE']), )
       print("n_elements =", n_elements, " BLOCK_SIZE=1024 => grid =", triton.cdiv(n_elements, 1024))  # 示例代码
       add_kernel[grid](x, y, output, n_elements, BLOCK_SIZE=1024)
       return output
   ```
3. 分别把 `size` 改成 `98432`、`100000`、`1024` 运行，观察打印出的 grid 值。

**需要观察的现象**：grid 值会随 `size` 变化，且是向上取整的结果。

**预期结果**（按 cdiv 公式手算，**待本地运行验证**）：
- `size=98432, BLOCK_SIZE=1024` → grid = \(\lceil 98432/1024 \rceil = \lceil 96.125 \rceil = 97\)
- `size=100000, BLOCK_SIZE=1024` → grid = \(\lceil 100000/1024 \rceil = 98\)
- `size=1024, BLOCK_SIZE=1024` → grid = 1

也就是说：前两个例子里，最后一个 program 只会处理「不满一块」的剩余元素——这正是下一节 `mask` 要解决的问题。

#### 4.2.5 小练习与答案

**练习 1**：本例用的是一维网格 `axis=0`。如果有人写成 `tl.program_id(axis=1)`，会怎样？

> **答案**：因为 grid 只有一维（元组只有一个元素），第 1、2 轴上的 program 数都是 0，`program_id(1)` 恒为 0，于是所有 program 都认为自己处理第 0 块，结果只会计算正确向量的第一块、其余位置不变（或越界），整体结果是错的。所以一维网格必须用 `axis=0`。

**练习 2**：`grid` 为什么写成一个「返回元组的 lambda」，而不是直接写一个整数？

> **答案**：因为 grid 可能依赖 kernel 的 `constexpr` 参数（如 `BLOCK_SIZE`）。把 grid 写成函数 `lambda meta: (...)`，Triton 就能在拿到 `meta['BLOCK_SIZE']` 之后再计算 grid，从而让「块大小」与「块数」自动配套。如果直接写死整数，改 `BLOCK_SIZE` 时就必须手动同步改两处。

---

### 4.3 `tl.load` / `tl.store`：带掩码的分块访存

#### 4.3.1 概念说明

有了 `offsets`，下一步就是真正读写内存。Triton 的做法是「指针 + 偏移 + 掩码」：

- `x_ptr + offsets`：一个「张量化的指针」，表示一连串要读的地址。
- `mask=mask`：一个布尔张量，与 `offsets` 等长；`True` 的位置才真正访问内存，`False` 的位置跳过（读时给默认值，写时不写）。

为什么必须要有 mask？因为向量长度往往不是 `BLOCK_SIZE` 的整数倍，最后一个 program 会分到一些「超出真实长度」的下标。比如 `size=98432, BLOCK_SIZE=1024` 时，最后一个 program 的下标会到 \(97 \times 1024 = 99328\)，而真实数据只到 98431，\(98432..99327\) 这一段都是越界的，必须用 mask 屏蔽。

#### 4.3.2 核心流程

vector-add 的访存与计算流程：

1. 构造掩码：`mask = offsets < n_elements`，越界位置为 `False`。
2. 读入两个输入块：`x = tl.load(x_ptr + offsets, mask=mask)`，`y` 同理。
3. 块内逐元素相加：`output = x + y`（这条语句没有循环，整块一起算，这正是 Triton「块级编程」的好处）。
4. 写回结果：`tl.store(output_ptr + offsets, output, mask=mask)`，同样用 mask 保护写操作。

伪代码：

```
mask    = offsets < n_elements          # 哪些下标是合法的
x       = tl.load(x_ptr + offsets, mask=mask)
y       = tl.load(y_ptr + offsets, mask=mask)
output  = x + y                          # 整块并行计算
tl.store(output_ptr + offsets, output, mask=mask)
```

#### 4.3.3 源码精读

构造掩码，然后两次 load、一次加法、一次 store，这是整个 kernel 的全部计算：

[third_party/ascend/tutorials/01-vector-add.py:67-74](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/tutorials/01-vector-add.py#L67-L74) —— `mask = offsets < n_elements` 标出合法下标；`tl.load(x_ptr + offsets, mask=mask)` 与 `tl.load(y_ptr + offsets, mask=mask)` 分别从 DRAM 读入两个块；`output = x + y` 整块相加；`tl.store(output_ptr + offsets, output, mask=mask)` 把结果写回 DRAM。注释里也写明了「Load x and y from DRAM / Write back to DRAM」。

`load` 与 `store` 在 core 里的签名（本例只用到 `pointer` 和 `mask` 两个参数）：

[python/triton/language/core.py:2111-2112](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/language/core.py#L2111-L2112) —— `load(pointer, mask=None, other=None, ...)`：当 `pointer` 是「N 维张量指针」时，`mask` 会被广播到 `pointer.shape`，逐元素决定哪些地址真正被读取；本例正属于这种情况（`x_ptr + offsets` 是一维张量指针）。

[python/triton/language/core.py:2187](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/language/core.py#L2187) —— `store(pointer, value, mask=None, ...)`：与 `load` 对称，把 `value` 写入 `pointer` 指定的地址，`mask` 为 `False` 的位置不写。

块内偏移来自 `arange`：

[python/triton/language/core.py:1641-1644](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/language/core.py#L1641-L1644) —— `arange(start, end)` 返回 `[start, end)` 的连续值。文档要求 `end - start` 是 2 的幂且不超过 `TRITON_MAX_TENSOR_NUMEL`，所以 `BLOCK_SIZE` 一般要取 256/512/1024/2048 这样的 2 的幂。

> 补充：这些 `load/store` 最终会被 Ascend 后端 lower 成 Linalg 的访存算子（见 u4）。本讲只关心「怎么写」，不关心「怎么编译」。

#### 4.3.4 代码实践

**实践目标**：改变 `BLOCK_SIZE` 与向量长度，验证「在 mask 保护下，无论块大小怎么取，最大误差始终为 0」。

**操作步骤**：

1. 打开 `third_party/ascend/tutorials/01-vector-add.py`。
2. 修改 `add` 函数里的启动参数（[第 86 行](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/tutorials/01-vector-add.py#L86)），把 `BLOCK_SIZE=1024` 分别改成 `256`、`512`、`2048`、`4096`（这些都是 2 的幂，符合 `arange` 要求）。
3. 同时在 `test_vector_addition` 里把 `size = 98432`（[第 96 行](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/tutorials/01-vector-add.py#L96)）改成一些非 `BLOCK_SIZE` 整数倍的值，如 `100000`、`65537`、`1`。
4. 每次改完运行一次脚本，关注最后一行 `The maximum difference ... is X`。

**需要观察的现象**：无论 `BLOCK_SIZE` 与 `size` 怎么搭配（只要 `BLOCK_SIZE` 是 2 的幂且向量非空），最后一行的最大误差都应该是 `0.0`。

**预期结果**（**待本地验证**）：最大误差恒为 `0.0`。原因是 vector-add 是逐元素相加，Triton kernel 与 PyTorch 原生 `x + y` 对同一批 fp32 数据执行完全相同的加法，结果逐位一致，因此差值为 0。

**额外思考实验**（不必改代码）：如果把 `mask=mask` 从 `tl.store(...)` 里删掉会怎样？此时最后一个 program 会把 `output` 里越界位置的值写回 `output_ptr` 之后越界地址，造成非法写访问，通常会触发运行时错误或内存破坏——这正是 mask 的价值。

#### 4.3.5 小练习与答案

**练习 1**：`mask` 为什么在 `load` 和 `store` 两处都要写？

> **答案**：`load` 处的 mask 防止读越界地址（越界位置不会被读，避免非法访问）；`store` 处的 mask 防止把计算结果写到越界地址去覆盖别的数据。两端都要保护，缺一不可。

**练习 2**：如果把 `BLOCK_SIZE` 改成 `1000`（不是 2 的幂）会怎样？

> **答案**：`tl.arange(0, 1000)` 里的 `end - start = 1000` 不是 2 的幂，违反了 `arange` 的约束（见 [core.py:1647-1657](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/language/core.py#L1647-L1657) 的文档），会在编译期报错。所以 `BLOCK_SIZE` 必须取 2 的幂。

**练习 3**：校验正确性时，示例用的是 `torch.testing.assert_close`。如果只比较「最大误差为 0」够不够严谨？为什么 vector-add 的误差能恰好为 0？

> **答案**：对本例而言够用，因为逐元素 fp32 加法是确定性的、与硬件无关的位级运算，Triton 和 PyTorch 对同一输入做同样的加法，结果逐位相同，误差必然为 0。但对更复杂的算子（如涉及规约、乘累加、不同精度），就可能出现非零误差，那时需要用 `assert_close(rtol=..., atol=...)` 给出合理容差（u2、u10 会讨论精度问题）。

## 5. 综合实践

把三个最小模块串起来，完成下面这个贯穿任务：

**任务**：基于 `01-vector-add.py`，把它改成一个「逐元素加权相加」kernel `output = a * x + y`（`a` 是一个标量常数），并验证正确性。

**要求与步骤**：

1. **改 kernel 签名**：在 `add_kernel` 里新增一个参数 `a`（普通 Python 标量，不需要 `constexpr`）。
2. **改计算**：把 `output = x + y` 改成 `output = a * x + y`。这一步仍只需一行块级运算，体会 Triton「不写循环」的好处。
3. **改启动器 `add`**：让 `add(x, y, a)` 把 `a` 透传给 `add_kernel[grid](...)`。
4. **改校验**：在 `test_vector_addition` 里，把 `output_torch = x + y` 改成 `output_torch = 2.0 * x + y`，并在调用 `add(x, y, 2.0)` 时传入 `a=2.0`。
5. **运行验证**：运行脚本，观察最大误差是否仍为 0。

**检查清单**：

- [ ] kernel 里 `a` 不需要标注 `constexpr`（它是数值参数，不是形状参数）。
- [ ] `grid` 仍是 `lambda meta: (triton.cdiv(n_elements, meta['BLOCK_SIZE']),)`，与 `a` 无关。
- [ ] `mask` 在 `load` 和 `store` 两处都保留。
- [ ] 最终最大误差为 0（**待本地验证**）。

完成这个任务，说明你已经掌握了「定义 kernel → 切分 grid → 块级访存计算 → 校验」这条最短的 Triton 开发闭环。这也是后续所有更复杂 kernel（矩阵乘、softmax、attention）的骨架。

## 6. 本讲小结

- `@triton.jit` 把普通 Python 函数变成「待编译的 kernel」，它本身目标无关；真正决定跑在哪的是运行时选中的后端（Ascend）。
- `kernel[grid](...)` 的方括号语法来自 `KernelInterface.__getitem__`，它记住 grid 后进入 `JITFunction.run`，由 `run` 负责按需编译（生成 TTIR）并缓存。
- `grid` 决定开多少个 program；`tl.program_id(axis=0)` 让每个 program 拿到自己的编号，从而处理数据的不同切片。块数用 `cdiv(n_elements, BLOCK_SIZE)` 向上取整得到。
- `tl.arange(0, BLOCK_SIZE)` 生成块内偏移，`offsets = block_start + 相对偏移` 得到绝对下标；`BLOCK_SIZE` 必须是 2 的幂。
- `tl.load(ptr + offsets, mask=mask)` / `tl.store(ptr + offsets, value, mask=mask)` 用「指针 + 偏移 + 掩码」实现分块访存；mask 用于屏蔽最后一个 program 的越界位置。
- 校验正确性的标准做法：用 PyTorch 原生实现算一份参考结果，再用 `torch.testing.assert_close`（或最大误差）比对 Triton 输出。vector-add 因逐元素加法确定一致，误差可恰为 0。
- 本例文件在 `third_party/ascend/` 下，却只用 core 的通用语法——这正是 u1-l2「Ascend 例子复用 core 语法」分层原则的活样本。

## 7. 下一步学习建议

本讲你已经能在 NPU 上跑通一个 kernel。接下来的学习方向：

1. **u2-l1（Python 侧迁移）**：本例里出现了 `device='npu'`、`import torch_npu`。如果你手头有现成的 GPU 版 Triton 脚本，u2 会系统讲解 `torch.cuda.* → torch.npu.*` 的替换映射，帮你把任意 GPU 脚本搬到 NPU。
2. **u2-l2 / u2-l3（NPU 硬件模型与访存约束）**：本讲把 grid 当成「块数」泛泛理解，但在真实的 Ascend NPU 上，grid/并发任务数还要受 AI 核数、Vector 核数、内存对齐、UB 容量等约束。学完这两讲你才知道如何为 NPU 选「正确」的 grid 与 `BLOCK_SIZE`。
3. **u3-l1（`@triton.jit` 与编译入口）**：本讲只说「`run` 会编译成 TTIR」，却没有展开。u3-l1 会带你进入 `JITFunction.run` 内部，看清 TTIR 到底在哪一步生成、怎么进 Ascend 编译流水线。
4. **继续阅读源码**：想加深块级编程直觉，可以继续看 [tutorials/03-matrix-multiplication.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/tutorials/03-matrix-multiplication.py)，它把本讲的「一维切分」推广到二维分块，并引入了 `tl.dot`。
