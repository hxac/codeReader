# 编写并运行第一个 Triton kernel

## 1. 本讲目标

学完本讲后，你应当能够：

- 看懂并亲手写出一个最小的 Triton kernel（用 `@triton.jit` 装饰一个函数）。
- 用 `tl.load` / `tl.store` 完成显存（DRAM）的读取与写回，并理解「指针 + 偏移」与越界掩码（mask）。
- 理解 Triton 的 SPMD（单程序多数据）模型：`grid` 决定并行启动多少个 program，`program_id` 告诉当前 program 自己是第几个。
- 知道一个 kernel 编译后会产出哪些中间产物（ttir / ttgir / llir / ptx / cubin），并能通过 `.asm` 查看它们。
- 在没有 GPU 的机器上，用 `TRITON_INTERPRET=1` 解释器模式跑通并验证自己的 kernel。

本讲是 Unit 1 的收官篇。在前三讲（项目定位、构建安装、目录结构）建立的全局地图之上，本讲让你第一次「动手写、动手跑」。

---

## 2. 前置知识

本讲假设你已经具备：

- **基本 Python 能力**：能看懂函数、装饰器、lambda。
- **一点 PyTorch 经验**：会用 `torch.rand`、`torch.empty_like`、张量的 `.numel()`。本讲用 PyTorch 只是为了「准备输入数据」和「当参考答案对拍」，kernel 本身与 PyTorch 无关。
- **对 GPU 的直觉**（不要求会写 CUDA）：知道 GPU 上有成千上万个线程在并行做同一件事。Triton 的「块（block）」就是让你一次处理一小段数据，编译器帮你把这块数据分给一组线程。

两个需要先建立的直觉：

1. **指针（pointer）**：在 Triton kernel 里，张量是以「指向首元素的指针」形式传入的。你想访问第 `i` 个元素，就用 `ptr + i`。这一点和 NumPy「直接用下标」不同，更接近 C 的指针算术。
2. **SPMD 模型**：你只写「一个 program 要做的事」，再告诉 Triton「启动多少个 program」。每个 program 用 `program_id` 区分自己，处理数据的不同片段。这和 CUDA 的「一个线程块写一次，启动很多个」是同一个思想。

> 名词速查：
> - **kernel**：运行在 GPU 上的函数。
> - **program**：Triton 的并行单位，大致对应 CUDA 的一个线程块（CTA）。
> - **grid**：一次启动的 program 总数（可是一维/二维/三维）。
> - **block**：一个 program 一次性处理的那一小段数据。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `python/tutorials/01-vector-add.py` | 官方入门教程：向量加法 kernel，本讲全程以此为范本。 |
| `python/triton/__init__.py` | 顶层包，导出 `jit`、`cdiv` 等公共 API。 |
| `python/triton/language/__init__.py` | `triton.language`（简称 `tl`）的导出清单，告诉我们 `tl.load`/`tl.store`/`tl.program_id`/`tl.arange` 从哪来。 |
| `python/triton/language/core.py` | `tl` 语言的核心实现：`program_id`、`arange`、`load`、`store`、`constexpr` 都定义在这里。 |
| `python/triton/runtime/jit.py` | `@triton.jit` 装饰器、`JITFunction`、`KernelInterface` 与解释器分支。 |
| `python/triton/compiler/compiler.py` | `CompiledKernel`，编译产物容器，`.asm` 字典就在这里。 |
| `python/triton/knobs.py` | 配置旋钮，`TRITON_INTERPRET` 在这里被读取。 |

阅读顺序建议：先看教程 `01-vector-add.py` 建立感性认识，再逐个对照 `core.py` / `jit.py` 看背后实现。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：jit 装饰器、tl.load/store、grid 与 program_id、编译产物查看。

---

### 4.1 jit 装饰器

#### 4.1.1 概念说明

`@triton.jit` 是写 Triton kernel 的入口。它把一个普通的 Python 函数「标记」成一个会被即时编译（JIT，Just-In-Time）的 kernel。

关键点：

- **函数体不会按普通 Python 那样逐行执行**。当你调用这个函数时，Triton 会把函数体翻译成中间表示（IR），最终编译成 GPU 二进制。函数体里只能出现「编译器认识的东西」：Python 基本类型、`tl` 提供的算子、传入的参数、以及其他 `@triton.jit` 函数。
- **即时编译**：kernel 在第一次用某组参数类型/形状调用时才真正编译，之后相同特征（cache key）的调用会命中缓存、直接启动。
- **它只是个「包装」**：被装饰后，函数变成了一个 `JITFunction` 对象，拥有 `[grid]` 这种特殊的调用语法。

#### 4.1.2 核心流程

从装饰到运行的链路：

1. `@triton.jit` 装饰函数 `fn`。
2. 装饰器内部判断是否处于解释器模式：
   - 若 `TRITON_INTERPRET=1`：返回 `InterpretedFunction`（纯 Python 逐 program 模拟，无需 GPU）。
   - 否则：返回 `JITFunction`。
3. 用 `kernel[grid](...)` 调用时：
   - `__getitem__(grid)` 记住 grid，返回一个「待调用」的代理。
   - 真正调用时进入 `run`：解析参数、做特化、（首次时）编译、最终在 GPU 上启动。

伪代码：

```text
@triton.jit
def fn(...):            # 1. 被装饰
    ...

fn[grid](args)          # 2. __getitem__(grid) 记下 grid
  -> fn.run(grid=grid, ...)   # 3. 首次调用触发编译 -> CompiledKernel
  -> 在 GPU 上启动             # 4. 后续相同特征命中缓存直接启动
```

#### 4.1.3 源码精读

先看教程里如何使用装饰器。`add_kernel` 就是一个被 `@triton.jit` 装饰的函数，参数里的 `x_ptr` 等都是「指针」，`BLOCK_SIZE: tl.constexpr` 标注为编译期常量：

[python/tutorials/01-vector-add.py:29-36](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/python/tutorials/01-vector-add.py#L29-L36) —— `@triton.jit` 装饰 `add_kernel`，函数体内是 kernel 逻辑。

再看 `jit` 装饰器本身的实现。`jit` 既是无参装饰器（`@triton.jit`）也支持带参形式（`@triton.jit(debug=...)`）。核心在 `decorator` 里：根据 `knobs.runtime.interpret` 在 `InterpretedFunction` 与 `JITFunction` 之间二选一：

[python/triton/runtime/jit.py:954-1006](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/python/triton/runtime/jit.py#L954-L1006) —— `jit` 装饰器定义，`decorator` 内根据 `knobs.runtime.interpret` 选择 `InterpretedFunction` 或 `JITFunction`。

注意第 985 行的判断 `if knobs.runtime.interpret:`，它就是「无 GPU 也能跑」的开关源头。这个开关读取的环境变量定义在 knobs 里：

[python/triton/knobs.py:472](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/python/triton/knobs.py#L472) —— `interpret: env_bool = env_bool("TRITON_INTERPRET")`，把环境变量 `TRITON_INTERPRET` 映射成布尔旋钮。

`jit` 通过顶层包导出，所以我们才能直接写 `import triton; @triton.jit`：

[python/triton/__init__.py:20](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/python/triton/__init__.py#L20) —— 从 `runtime.jit` 导入 `jit` 等符号。

#### 4.1.4 代码实践

**目标**：直观感受「被装饰后函数不再是普通函数」。

**步骤**：

1. 写一个最小脚本（示例代码）：

```python
# 示例代码
import triton

@triton.jit
def hello_kernel(x_ptr):
    pass

print(type(hello_kernel))
```

2. 在不设 `TRITON_INTERPRET` 的情况下运行，观察打印出的类型（应是 `JITFunction`，需 GPU 环境）。
3. 设置环境变量后再运行：`TRITON_INTERPRET=1 python 你的脚本.py`，观察类型是否变成 `InterpretedFunction`。

**需要观察的现象**：被装饰的对象类型不是 `function`，而是 `JITFunction` / `InterpretedFunction`。

**预期结果**：普通模式下为 `JITFunction`，解释器模式下为 `InterpretedFunction`。

> 若环境无 GPU，仅 `TRITON_INTERPRET=1` 分支可稳定复现，`JITFunction` 分支为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果在一个 kernel 函数体里 `import numpy as np` 并调用 `np.add(...)`，会发生什么？

**答案**：会报错。`@triton.jit` 函数体只能访问 Python 基本类型、`tl` 算子、传入参数和其他 jit 函数。NumPy 不在编译器认识的白名单里，因此无法被翻译成 IR。（这是第 4.1.1 节强调的「函数体不是普通 Python」的体现。）

**练习 2**：`@triton.jit` 装饰后，函数第一次调用和第二次调用（相同参数类型）的开销主要差在哪里？

**答案**：第一次调用要触发「特化 + 编译」（AST → TTIR → … → cubin），开销大；第二次相同 cache key 命中缓存，只需启动，开销小。这就是「JIT」中「即时」与缓存的意义。

---

### 4.2 tl.load / tl.store

#### 4.2.1 概念说明

GPU 上的数据放在显存（DRAM）里。kernel 要处理数据，得先把数据「搬进来」算，再把结果「搬出去」。在 Triton 里：

- `tl.load(ptr, mask=..., other=...)`：从显存读一个 block 的数据到寄存器。
- `tl.store(ptr, value, mask=...)`：把一个 block 的结果写回显存。

两个关键设计：

1. **指针算术**：`tl.load` 接收的不是单个地址，而是「一组地址」（一个 block 的指针）。`x_ptr + offsets` 会得到一个长度为 `BLOCK_SIZE` 的指针数组，`tl.load` 一次性把它们指向的数据全读进来。
2. **掩码 mask**：当数据长度不是 `BLOCK_SIZE` 的整数倍时，最后一个 block 会「多出来」几个越界位置。用 `mask=offsets < n_elements` 把越界位置挡掉，避免读/写非法内存。

#### 4.2.2 核心流程

以向量加法为例，一次 load/store 的流程：

```text
offsets = block_start + tl.arange(0, BLOCK_SIZE)   # 生成一串下标
mask    = offsets < n_elements                       # 哪些下标合法
x = tl.load(x_ptr + offsets, mask=mask)             # 读入 x block
y = tl.load(y_ptr + offsets, mask=mask)             # 读入 y block
output  = x + y                                     # 寄存器内计算
tl.store(output_ptr + offsets, output, mask=mask)   # 写回结果
```

注意 `mask` 同时用于 load 和 store：读的时候越界位置不读、写的时候越界位置不写，保证安全。

`other` 参数（本例未用）用来指定「被 mask 掉的位置填什么默认值」，未给则为未定义值。

#### 4.2.3 源码精读

先看教程里 load/store 的真实用法，注释解释得很清楚——`offsets` 是一组指针、`mask` 防越界：

[python/tutorials/01-vector-add.py:44-54](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/python/tutorials/01-vector-add.py#L44-L54) —— 构造 `offsets`、`mask`，`tl.load` 读入 x/y，相加后 `tl.store` 写回。

再看 `tl.load` 的定义。它本质是个 `@builtin`，把 Python 调用转发给 `_semantic.load(...)`（语义层，负责真正生成 IR）。`mask` 与 `other` 支持 `constexpr`，会被解包后传下去：

[python/triton/language/core.py:2333-2375](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/python/triton/language/core.py#L2333-L2375) —— `load` 的签名与文档：`mask[idx]` 为假则不读，`other[idx]` 为假位置的返回值。

`tl.store` 与之对称，`mask[idx]` 为假则不写：

[python/triton/language/core.py:2394-2422](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/python/triton/language/core.py#L2394-L2422) —— `store` 把 `value` 写入 `pointer` 指向的位置，同样支持 `mask`。

这两个符号通过 `triton.language` 的 `__init__` 导出，所以 `tl.load` / `tl.store` 可用：

[python/triton/language/__init__.py:89](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/python/triton/language/__init__.py#L89) 与 [python/triton/language/__init__.py:111](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/python/triton/language/__init__.py#L111) —— 从 `core` 导出 `load` 与 `store`。

#### 4.2.4 代码实践

**目标**：体会 mask 在边界处理中的作用。

**步骤**：

1. 设想 `n_elements = 10`，`BLOCK_SIZE = 4`。则 `cdiv(10, 4) = 3`，共 3 个 program。
2. 手算第 3 个 program（`pid = 2`）：`block_start = 8`，`offsets = [8,9,10,11]`，`mask = [True,True,False,False]`。
3. 阅读上面的教程代码，确认 `tl.load`/`tl.store` 都带了这个 mask。

**需要观察的现象**：越界的 `offsets`（10、11）被 mask 屏蔽，不会触发越界访问。

**预期结果**：边界 program 只处理合法的 2 个元素，多余 2 个位置被跳过。这是「非整除也能安全运行」的根源。

> 本实践为「源码阅读 + 手算」型，无需 GPU，结论可直接推导。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `tl.load` 的 `mask=mask` 去掉，当 `n_elements` 不是 `BLOCK_SIZE` 整数倍时会怎样？

**答案**：最后一个 program 会读到越界地址，行为未定义（可能读到垃圾数据甚至崩溃）。mask 是处理非整除边界的标准做法。

**练习 2**：`tl.load(x_ptr + offsets, mask=mask, other=0.0)` 中 `other=0.0` 的作用是什么？

**答案**：被 mask 屏蔽的位置不真正读内存，而是填入 `0.0` 作为该位置的值。这在需要后续对整块做规约、又不想让越界垃圾值污染结果时很有用。

---

### 4.3 grid 与 program_id

#### 4.3.1 概念说明

Triton 用 SPMD 模型组织并行：

- **program**：你写的函数体，是「一个 program 要做的事」。
- **grid**：一次启动多少个 program 并行跑。可看作「数据的切分份数」。
- **program_id(axis)**：当前 program 在某个轴上的编号（从 0 开始），用来决定自己处理数据的哪一段。

最常见的是一维 grid：把长度为 N 的数据切成若干块，每块由一个 program 处理。第 `pid` 个 program 处理 `[pid*BLOCK_SIZE, (pid+1)*BLOCK_SIZE)` 这一段。

grid 的取值如何确定？用 `triton.cdiv(n_elements, BLOCK_SIZE)`——即「向上取整的除法」，保证覆盖所有元素。

#### 4.3.2 核心流程

从「数据长度 N」到「grid 大小」再到「每个 program 的数据段」：

```text
N = 输入长度
grid = ( cdiv(N, BLOCK_SIZE), )          # 一维：需要多少个块
每个 program pid:
    block_start = pid * BLOCK_SIZE        # 本 program 数据起点
    offsets     = block_start + [0..BLOCK_SIZE-1]
    mask        = offsets < N             # 越界保护
```

向上取整除法的数学定义：

\[
\text{cdiv}(n, b) = \left\lceil \frac{n}{b} \right\rceil = \left\lfloor \frac{n + b - 1}{b} \right\rfloor
\]

grid 还支持「可调用对象」形式：`grid = lambda meta: (cdiv(n, meta['BLOCK_SIZE']), )`。这样 grid 能读取传给 kernel 的元参数（如 `BLOCK_SIZE`），在 autotune 改变 `BLOCK_SIZE` 时 grid 会自动跟着变。

#### 4.3.3 源码精读

教程里 `program_id` 决定本 program 处理哪一段，并构造 `offsets` 与 `mask`：

[python/tutorials/01-vector-add.py:39-47](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/python/tutorials/01-vector-add.py#L39-L47) —— `pid = tl.program_id(axis=0)`，`block_start = pid * BLOCK_SIZE`，`offsets` 与 `mask` 的构造。

教程里 grid 用 lambda 形式，依赖 `meta['BLOCK_SIZE']`，并用 `triton.cdiv` 计算块数：

[python/tutorials/01-vector-add.py:67-75](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/python/tutorials/01-vector-add.py#L67-L75) —— `grid = lambda meta: (triton.cdiv(n_elements, meta['BLOCK_SIZE']), )`，随后 `add_kernel[grid](...)` 启动。

`tl.program_id` 的定义——一个 `@builtin`，返回当前 program 在指定轴上的编号（轴取 0/1/2）：

[python/triton/language/core.py:1794-1809](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/python/triton/language/core.py#L1794-L1809) —— `program_id(axis)` 返回当前 program 实例在给定轴上的 id。

配套的 `num_programs(axis)` 返回该轴上启动的 program 总数（写更复杂的 grid 时常用）：

[python/triton/language/core.py:1812-1821](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/python/triton/language/core.py#L1812-L1821) —— `num_programs(axis)` 返回某轴上的 program 总数。

`tl.arange(start, end)` 生成连续整数 block，是构造 `offsets` 的标准手段（注意 `end - start` 需为 2 的幂）：

[python/triton/language/core.py:1829-1846](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/python/triton/language/core.py#L1829-L1846) —— `arange(start, end)` 返回 `[start, end)` 内的连续值，长度需为 2 的幂。

最后是 `triton.cdiv` 的实现，即上面那个向上取整公式：

[python/triton/__init__.py:68-70](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/python/triton/__init__.py#L68-L70) —— `cdiv(x, y) = (x + y - 1) // y`，向上取整除法。

#### 4.3.4 代码实践

**目标**：理解 grid 大小与 program_id 的对应关系。

**步骤**：

1. 取 `N = 98432`（教程里的值），`BLOCK_SIZE = 1024`。
2. 计算 `cdiv(98432, 1024)`。手算：\(98432 / 1024 = 96.125\)，向上取整为 97。
3. 第 `pid = 96`（最后一个 program）：`block_start = 96 * 1024 = 98304`，`offsets` 范围 `[98304, 99327]`，其中只有 `[98304, 98431]`（128 个元素）合法，其余被 mask 屏蔽。

**需要观察的现象**：grid 大小 97 正好覆盖 98432 个元素；最后一个 program 仅处理 128 个有效元素。

**预期结果**：grid = (97,)；每个 program 处理 1024 个槽位，最后一个 program 只有 128 个有效元素，其余靠 mask 跳过。

> 本实践为手算型，无需运行；若想验证可在 `TRITON_INTERPRET=1` 下打印 `tl.num_programs(0)`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 grid 用 `lambda meta: (cdiv(n, meta['BLOCK_SIZE']),)` 而不直接写死一个整数？

**答案**：因为 `BLOCK_SIZE` 是可变的元参数（尤其在 autotune 时会尝试多个值）。用 lambda 让 grid 能读取当前实际传入的 `BLOCK_SIZE`，自动算出正确的块数，避免 grid 与 block 大小不一致。

**练习 2**：如果数据是二维的（例如矩阵），grid 会怎么写？

**答案**：grid 通常写成二维 `(cdiv(M, BLOCK_M), cdiv(N, BLOCK_N))`，kernel 内用 `pid_m = tl.program_id(0)`、`pid_n = tl.program_id(1)` 分别取两个轴的编号，定位到矩阵中的一个块。这正是第 u2-l2 讲（矩阵乘法）要做的事。

---

### 4.4 编译产物查看

#### 4.4.1 概念说明

一个 kernel 从 Python 函数到 GPU 二进制，会经过一条多级 IR 流水线（在 u1-l1 已建立的全局地图里）：

\[
\text{AST} \rightarrow \text{TTIR} \rightarrow \text{TTGIR} \rightarrow \text{LLIR} \rightarrow \text{PTX} \rightarrow \text{cubin}
\]

- **TTIR**：逻辑层 IR，还看不出 GPU 线程细节。
- **TTGIR**：带 GPU 布局的物理层 IR。
- **LLIR**：LLVM IR。
- **PTX**：NVIDIA 的并行线程执行汇编。
- **cubin**：最终的 GPU 二进制（NVIDIA 后端）。

编译完成后，这些产物都装在一个叫 `CompiledKernel` 的对象里，通过它的 `.asm` 字典按级别访问（如 `kernel.asm['ttir']`、`kernel.asm['ttgir']`）。这是排查问题、理解「我的 Python 到底变成了什么」的关键窗口。

#### 4.4.2 核心流程

`CompiledKernel` 在编译结束时被构造：

```text
compile(...)
  -> 产出 metadata_group（一组文件路径：各级 IR + 一个 .json 元数据）
  -> CompiledKernel(src, metadata_group, hash)
       读取 .json -> metadata
       读取各级 IR 文件 -> self.asm = { "ttir": ..., "ttgir": ..., "ptx": ..., "cubin": ... }
       self.kernel = asm[二进制扩展名]   # 例如 cubin
```

`asm` 是一个以「文件后缀」为键的字典：`.ttir` → `'ttir'`、`.ttgir` → `'ttgir'`、`.ptx` → `'ptx'`、`.cubin` → `'cubin'`。其中二进制（cubin）以 bytes 存储，其余以文本存储。

#### 4.4.3 源码精读

`CompiledKernel.__init__` 把编译产物装进 `self.asm`。注意它把每个文件按后缀名当键，二进制后缀（`binary_ext`，NVIDIA 是 `cubin`）用 `read_bytes()`，其余用 `read_text()`：

[python/triton/compiler/compiler.py:423-431](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/python/triton/compiler/compiler.py#L423-L431) —— 构造 `self.asm = AsmDict({...})`，并令 `self.kernel = self.asm[binary_ext]`。

`asm_files` 是从 `metadata_group` 里筛出「非 json」的文件得到的，json 那份则是 kernel 的元数据（名字、shared 内存大小、num_warps 等）：

[python/triton/compiler/compiler.py:407-422](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/python/triton/compiler/compiler.py#L407-L422) —— `CompiledKernel.__init__` 开头：解析 metadata、还原 `GPUTarget`、记录 `src/hash/name`。

`kernel[grid](...)` 的启动入口在这里：`CompiledKernel.run` 是个 property，首次访问时调用 `_init_handles()` 延迟加载二进制到 GPU（这涉及设备查询、launcher 创建，所以做成延迟）：

[python/triton/compiler/compiler.py:487-491](https://github.com/triton-lang/triton/blob/23ca0e48a2bd5eac19745e4dd90d8b3933dba6db/python/triton/compiler/compiler.py#L487-L491) —— `run` 属性：`self._run` 为空时调用 `_init_handles()` 完成延迟初始化。

而 `.asm` 在 `__init__` 阶段就已就绪，因此你**不需要真正启动 kernel**，就能查看它的各级 IR。

#### 4.4.4 代码实践

**目标**：查看一个 kernel 编译产出的各级 IR。

**步骤**（示例代码）：

```python
# 示例代码（需要 GPU 环境才能真正编译；无 GPU 时此步为「待本地验证」）
import torch, triton, triton.language as tl

@triton.jit
def add_kernel(x_ptr, y_ptr, out_ptr, n, BLOCK_SIZE: tl.constexpr):
    pid = tl.program_id(0)
    offs = pid * BLOCK_SIZE + tl.arange(0, BLOCK_SIZE)
    mask = offs < n
    x = tl.load(x_ptr + offs, mask=mask)
    y = tl.load(y_ptr + offs, mask=mask)
    tl.store(out_ptr + offs, x + y, mask=mask)

x = torch.rand(128, device='cuda'); y = torch.rand(128, device='cuda')
out = torch.empty_like(x)
add_kernel[(1,)](x, y, out, 128, BLOCK_SIZE=128)

print(add_kernel.asm.keys())        # 看有哪些级别
print(add_kernel.asm['ttir'])       # 打印逻辑 IR
```

**需要观察的现象**：`asm.keys()` 至少包含 `ttir`、`ttgir`、`ptx`、`cubin`（NVIDIA 后端）；`asm['ttir']` 能看到形如 `tt.load` / `tt.add` / `tt.store` 的操作。

**预期结果**：能看到从 TTIR 到 cubin 的各级文本/二进制产物，TTIR 中的操作与你在 Python 里写的 load/add/store 一一对应。

> 无 GPU 环境：本步为「待本地验证」。可用 `TRITON_INTERPRET=1` 跑通逻辑，但解释器模式不产出 `.asm`（它不真正编译），故查看 IR 需真实 GPU。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `self.kernel = self.asm[binary_ext]` 而不是 `self.asm['cubin']` 写死？

**答案**：因为不同后端的二进制扩展名不同：NVIDIA 是 `cubin`，AMD 是 `hsaco`。用 `backend.binary_ext` 取键，让同一份 `CompiledKernel` 代码兼容多后端。

**练习 2**：查看 `.asm` 需要先调用 `kernel[grid](...)` 启动 kernel 吗？

**答案**：不需要。`.asm` 在 `CompiledKernel.__init__` 时就由磁盘文件填充完毕；而真正的二进制加载（`_init_handles`，查询设备、创建 launcher）是延迟到访问 `.run` 时才发生。所以「看 IR」和「跑 kernel」是两件事。

---

## 5. 综合实践

把本讲四个模块串起来：参照 `01-vector-add.py`，写一个对长度为 N 的向量做**逐元素乘 2** 的 Triton kernel，并在无 GPU 的解释器模式下验证正确。

### 实践目标

综合运用：`@triton.jit` 装饰器、`tl.load`/`tl.store` 与 mask、`grid`/`program_id`/`BLOCK_SIZE`，并尝试查看编译产物。

### 操作步骤

1. 新建 `mul2.py`（示例代码），写入下面内容：

```python
# 示例代码
import torch
import triton
import triton.language as tl

@triton.jit
def mul2_kernel(x_ptr, out_ptr, n_elements, BLOCK_SIZE: tl.constexpr):
    pid = tl.program_id(axis=0)                 # 4.3 我是第几个 program
    block_start = pid * BLOCK_SIZE
    offsets = block_start + tl.arange(0, BLOCK_SIZE)
    mask = offsets < n_elements                  # 4.2 越界掩码
    x = tl.load(x_ptr + offsets, mask=mask)      # 4.2 读入
    out = x * 2.0                                # 逐元素乘 2（标量广播）
    tl.store(out_ptr + offsets, out, mask=mask)  # 4.2 写回

def mul2(x: torch.Tensor) -> torch.Tensor:
    out = torch.empty_like(x)
    n = x.numel()
    # 4.3 grid 用 lambda，依赖 BLOCK_SIZE
    grid = lambda meta: (triton.cdiv(n, meta['BLOCK_SIZE']), )
    mul2_kernel[grid](x, out, n, BLOCK_SIZE=1024)
    return out

if __name__ == "__main__":
    torch.manual_seed(0)
    x = torch.rand(98432)
    ref = x * 2.0
    got = mul2(x)
    diff = (ref - got).abs().max().item()
    print("max abs diff =", diff)
    assert diff < 1e-5, "结果不一致"
    print("OK")
```

2. **在无 GPU 环境运行**（关键）：设置解释器模式后再跑——

```bash
TRITON_INTERPRET=1 python mul2.py
```

3. （可选，需 GPU）去掉 `TRITON_INTERPRET`，改在 `cuda` 张量上运行，并在 `mul2` 返回后查看编译产物：

```python
# 示例代码（需 GPU）
print(mul2_kernel.asm.keys())
print(mul2_kernel.asm['ttir'])
```

### 需要观察的现象

- 解释器模式下输出 `max abs diff` 应为一个极小数（如 0 或 1e-7 量级），随后打印 `OK`。
- 改变 `n` 为非 `BLOCK_SIZE` 整数倍（如 1000）依然正确——验证 mask 边界处理生效。
- （GPU 下）`asm['ttir']` 里能看到 `tt.load`、一个 elementwise 乘法、`tt.store`。

### 预期结果

- 解释器模式：结果与 PyTorch 参考一致，`assert` 通过。
- 非整除长度同样通过，证明 mask 起作用。
- GPU 模式下能 dump 出各级 IR；无 GPU 时 IR 查看为「待本地验证」。

### 排查提示

- 报 `interpret is not available` 之类：确认环境变量是 `TRITON_INTERPRET`（不是 `TRITON_INTERPREPT`，容易拼错）。
- 结果差很多：检查 `mask` 是否同时用于 load 和 store、`BLOCK_SIZE` 是否标注为 `tl.constexpr`。

---

## 6. 本讲小结

- `@triton.jit` 把普通 Python 函数包装成 kernel；函数体被翻译成 IR 而非逐行执行，`TRITON_INTERPRET=1` 时改走纯 Python 解释器（无需 GPU）。
- `tl.load(ptr + offsets, mask=...)` / `tl.store(ptr + offsets, value, mask=...)` 负责显存读写；指针算术给出一组地址，mask 处理非整除边界。
- Triton 是 SPMD 模型：`grid` 决定启动多少个 program，`program_id(axis)` 告诉当前 program 自己的编号，`triton.cdiv` 用来算 grid 大小。
- 一个 kernel 编译产出 TTIR → TTGIR → LLIR → PTX → cubin 多级产物，全部装在 `CompiledKernel.asm` 字典里，按后缀（`ttir`/`ttgir`/`ptx`/`cubin`）取用；查看 IR 不需要真正启动 kernel。
- `BLOCK_SIZE` 必须是 `tl.constexpr`（编译期常量），grid 用 `lambda meta` 形式以自适应 `BLOCK_SIZE`。

---

## 7. 下一步学习建议

本讲让你「会写、会跑」一个最简单的 kernel。接下来建议：

- **深入 `tl` 语言**：进入 Unit 2（`u2-l1` tl 核心类型与 constexpr），系统学习 tensor/block_type、dtype 体系与更多算子。
- **写矩阵乘法**：`u2-l2` 把指针算术推广到二维，实现分块 GEMM，是理解 Triton 真正威力的下一步。
- **理解 JIT 内部**：若你对 `kernel[grid](...)` 背后的特化与编译好奇，可跳到 Unit 3（`u3-l1` JITFunction）看调用链细节。
- **复用官方教程**：`python/tutorials/` 下还有 softmax、layer norm、矩阵乘法等范例，建议逐个在 `TRITON_INTERPRET=1` 下跑通，作为练手素材。
