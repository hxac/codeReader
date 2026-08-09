# CuTe-DSL 编程模型入门

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 `@cute.jit` 与 `@cute.kernel` 两个装饰器各自的作用，以及它们在 QuACK 里是如何搭配使用的。
- 理解 CuTe-DSL 中**静态值（编译期）**与**动态值（运行期）**的根本区别，并掌握 `const_expr` 的语义。
- 区分三种循环 `range` / `cutlass.range` / `cutlass.range_constexpr`，知道什么时候用哪一个。
- 记住 DSL 控制流的主要限制，以及「源码必须落盘」这条容易踩坑的约束。

本讲是后续所有内核讲义（归约、GEMM）的地基：QuACK 的每一个内核都是用这套编程模型写出来的，理解了它，再读 `reduce.py`、`softmax.py` 就不会对着满屏 `const_expr` 发懵。

## 2. 前置知识

本讲默认你已经读过前置讲义「QuACK 是什么」与「目录结构与模块地图」，知道：

- QuACK 用 **CuTe-DSL**（一个嵌在 Python 里的领域专用语言）来写 CUDA 内核，而不是写 C++。
- 这些 Python 函数最终会被**编译成 GPU 机器码**，性能接近手写 CUDA。

此外需要一点通用概念：

- **AST（抽象语法树）**：Python 解释器会把源码先解析成一棵语法树，再执行。CuTe-DSL 也是先「读你的 Python 源码 → 构建 AST → 翻译成中间表示（IR）→ 生成机器码」。
- **JIT（即时编译）**：不是提前把所有代码编译好，而是在「第一次运行 / 第一次拿到具体参数」时才编译。
- **IR（中间表示）**：介于高级语言和机器码之间的一种表示，CuTe-DSL 用的是 MLIR。

一句话总结 CuTe-DSL 的核心思想：**你写的是 Python，但 DSL 只接受 Python 的一个子集；编译器逐语句判断，哪些在编译期算掉、哪些翻译成 GPU 上真正执行的 IR。**

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [docs/dsl_control_flow.rst](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/docs/dsl_control_flow.rst) | CuTe-DSL 官方控制流说明，定义三种循环与 `const_expr` 的语义 |
| [docs/limitations.rst](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/docs/limitations.rst) | DSL 限制清单，定义静态值与动态值、依赖类型等约束 |
| [quack/reduce.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py) | 归约原语集合，集中展示了 `@cute.jit`、`const_expr`、`cutlass.range_constexpr` 的真实用法 |
| [quack/softmax.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py) | `@cute.jit`（主机入口）+ `@cute.kernel`（设备内核）的典型搭配 |
| [quack/broadcast_utils.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/broadcast_utils.py) | `cutlass.range(..., unroll_full=True)` 的真实示例 |
| [tests/dsl/test_mixed_constexpr_if.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/dsl/test_mixed_constexpr_if.py) | 最小可运行的 `@cute.kernel` + `cute.compile` 示例，本讲实践的模板 |

## 4. 核心概念与源码讲解

### 4.1 @cute.jit 与 @cute.kernel：把 Python 变成 GPU 代码

#### 4.1.1 概念说明

普通 Python 函数由解释器一行行执行。而加上 `@cute.jit` 或 `@cute.kernel` 装饰器的函数，会被 DSL **截获**：DSL 读取它的源码、构建 AST、翻译成 MLIR IR，最终编译成 GPU 机器码。换句话说，这两个装饰器是「请把我编译成 GPU 代码，而不是当普通 Python 跑」的标记。

两者的区别在于**产物形态**：

- **`@cute.kernel`**：定义一个**可启动的 CUDA 内核**。它有一个明确的并行启动配置（grid / block / cluster），你实例化它后调用 `.launch(...)` 把它丢到 GPU 上跑。
- **`@cute.jit`**：定义一个 **JIT 编译的函数**，本身不是「并行内核」，而是承担两种角色：
  1. **主机侧入口/编排者**：在 GPU 之外做准备工作（如切分 tile、构造拷贝描述），然后在内部调用 `.launch(...)` 启动一个 `@cute.kernel`；
  2. **设备侧辅助函数**：被其它 `@cute.jit` / `@cute.kernel` 调用，在编译时**内联**进调用方的 IR，相当于「设备代码里的子函数」。

> 一句话记忆：**`@cute.kernel` 是「会被并行启动的内核」；`@cute.jit` 是「会被编译的 Python 函数」，既能当主机编排，也能当设备内联帮手。**

#### 4.1.2 核心流程

一个典型 QuACK 内核从 Python 到 GPU 的流程：

1. 你调用公共 API（如 `quack.softmax(x)`）。
2. 内部进入一个 `@cute.jit` 的 `__call__`（主机编排者），它计算 tile 形状、构造 `TiledCopy` 等编译期/运行期参数。
3. `__call__` 调用 `self.kernel(...).launch(grid=, block=, cluster=, stream=...)`，把 `@cute.kernel` 启动到 GPU。
4. 编译过程：DSL 读源码 → AST → MLIR IR → PTX → cubin。`@cute.kernel` 与它调用的所有 `@cute.jit` 辅助函数（如 `warp_reduce`）都被内联进同一份内核 IR。
5. `cute.compile(...)` 是「触发编译」的入口，返回一个可直接调用的函数对象。

#### 4.1.3 源码精读

先看 `softmax.py` 的经典搭配：`__call__` 是 `@cute.jit`，`kernel` 是 `@cute.kernel`。

主机侧编排者 `__call__`（注意末尾的 `.launch`）：

[quack/softmax.py:66-85](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L66-L85) —— 这是 `@cute.jit`，它在 GPU 之外准备好 `tiler_mn`、`threads_per_row`，然后调用 `self.kernel(...).launch(...)` 启动设备内核。`grid` 由行数除以 tile 高、`cluster_n` 决定；`block` 是线程数；`cluster` 仅当 `cluster_n > 1` 时给出。

紧随其后的设备内核：

[quack/softmax.py:87-95](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L87-L95) —— 这是 `@cute.kernel`，签名里 `tiler_mn`、`tiled_copy`、`threads_per_row` 都是从主机侧 `__call__` 传进来的；它才是真正跑在每一个线程块上的并行代码。

再看「设备侧辅助函数」的例子——`warp_reduce`，它本身是 `@cute.jit`，被其它设备函数调用：

[quack/reduce.py:21-29](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L21-L29) —— `@cute.jit def warp_reduce(...)`，它在 `row_reduce`、`online_softmax_reduce` 中被调用，编译时内联进上层内核，并不是一个独立启动的并行内核。

最后看「触发编译」的入口 `cute.compile`，来自测试文件（这是最小可运行范例）：

[tests/dsl/test_mixed_constexpr_if.py:295-300](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/dsl/test_mixed_constexpr_if.py#L295-L300) —— `fn = cute.compile(_launch_scalar, kernel, ...)` 返回一个编译好的函数对象，再用 `fn(...)` 真正执行。

#### 4.1.4 代码实践

1. **实践目标**：分清 `@cute.jit` 与 `@cute.kernel` 的职责边界。
2. **操作步骤**：
   - 打开 `quack/softmax.py`，定位 `Softmax` 类里的 `__call__`（`@cute.jit`）和 `kernel`（`@cute.kernel`）。
   - 找到 `.launch(grid=..., block=..., cluster=..., stream=...)` 这一行（约 L80-L85）。
   - 确认 `grid` 的第一个维度 `cute.ceil_div(mX.shape[0], tiler_mn[0])` 来自运行期张量形状，而 `cluster` 是否为 `None` 由 `const_expr(self.cluster_n > 1)` 在编译期决定。
3. **需要观察的现象**：`__call__` 里既有运行期计算（`cute.ceil_div`），又有编译期分支（`const_expr`），两者共存于同一个 `@cute.jit` 函数中。
4. **预期结果**：你能用一句话说出「`__call__` 是主机入口、负责编排与启动；`kernel` 是设备并行体、由 `.launch` 投放到 GPU」。
5. 本实践的运行结果属于源码阅读型，**待本地验证**（若要在 GPU 上实际编译运行，参见 4.4 节的 `cute.compile` 示例）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `warp_reduce` 用 `@cute.jit` 而不是 `@cute.kernel`？

**答案**：因为它不是一个会被并行启动的内核，而是一个被其它设备代码调用的「帮手」。用 `@cute.jit` 标记后，编译时它会被内联进调用方（如 `row_reduce` → 最终的 softmax 内核）的 IR 里。`@cute.kernel` 才需要 `.launch(grid, block, ...)`。

**练习 2**：在 `softmax.py` 里，`__call__` 调用 `self.kernel(...).launch(...)`。如果把 `.launch(...)` 删掉会发生什么？

**答案**：`self.kernel(...)` 只构造出一个内核实例对象，必须调用 `.launch(...)` 才会真正在 GPU 上启动。删掉 `.launch` 意味着内核被定义却从未投放执行，运行时不会产生计算。

---

### 4.2 const_expr 与编译期常量：静态值 vs 动态值

#### 4.2.1 概念说明

这是 CuTe-DSL **最核心**的概念，理解了它，后面的代码就读通了一半。

DSL 把函数里的值分成两类（见 [docs/limitations.rst:23-44](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/docs/limitations.rst#L23-L44)）：

- **静态值（Static / 编译期）**：在 JIT 编译时就已经知道、编译完不再改变。包括 Python 的 `list`/`tuple`/`dict`，以及带 `cutlass.Constexpr` 类型标注的参数。它们用于「元编程」——决定生成什么样的代码。
- **动态值（Dynamic / 运行期）**：在 GPU 上运行时才知道或改变的值。只有特定类型能做动态值，比如 `Int32`、`Float32`、`Bool`、`Tensor` 等。`int` 传入会自动转 `Int32`，`float` 传入会自动转 `Float32`。

> DSL 把 Python 原生类型当作 C++ 模板参数来处理——编译期决定，运行期不可改结构。

`const_expr(...)` 就是「**把这个判断标记为编译期判断**」的开关：

- `if cutlass.const_expr(cond):` → 编译期分支：编译器在生成代码前就算出 `cond` 是 `True` 还是 `False`，**只把命中那条分支编进产物**。
- 普通 `if cond:` → 运行期分支：两条分支都会被翻译成 IR，由 GPU 在运行时根据 `cond` 选择。

类似地，`cutlass.Constexpr[int]` / `cutlass.Constexpr[bool]` 是参数类型标注，表示「这个参数是编译期常量」。不同的常量取值会编译出**不同的 cubin**。

#### 4.2.2 核心流程

`const_expr` 的价值在于「**特化（specialization）**」：用编译期标志裁剪代码。

以文档里的 ReLU epilogue 为例（[docs/dsl_control_flow.rst:144-156](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/docs/dsl_control_flow.rst#L144-L156)）：

```text
gemm(..., False)   # do_relu 为 False → 生成的 IR 里完全没有 ReLU 代码
gemm(..., True)    # do_relu 为 True  → ReLU 代码被编进 IR
```

也就是说，同一个 Python 函数，因为一个 `Constexpr` 参数取值不同，编译出**两份不同的机器码**。这就是「编译期分支」与「运行期分支」的本质差异：前者在编译时就消失了一条分支，后者两条分支都保留。

#### 4.2.3 源码精读

`warp_reduce` 是 `const_expr` 元编程的极佳范例。它的目标是：根据**数据类型、归约算子、GPU 架构**，在编译期决定走硬件 `redux.sync` 指令，还是走 shuffle 蝶形归约。

首先看导入与参数标注：

[quack/reduce.py:9](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L9) —— `from cutlass import Int32, Int64, Float32, Boolean, const_expr`，`const_expr` 是从这里导入的「编译期判断」开关。

参数里大量使用 `Constexpr` 标注（L25-L28）：`threads_in_group: cutlass.Constexpr[int]`、`dtype: cutlass.Constexpr = None`、`abs: cutlass.Constexpr[bool] = False`。这些参数的不同取值会产生不同的编译产物。

核心的编译期分派逻辑：

[quack/reduce.py:42-59](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L42-L59) —— 这里一连串 `const_expr(...)` 判断：先从 `val` 取出 `val_dtype`，再用 `const_expr(op is max ...)` 判断算子是不是 `max`/`min`，再用 `const_expr(val_dtype == Int32)` 判断类型，最后用 `const_expr(kind is not None)` 决定走 redux 路径还是 fallback。**所有这些判断在编译时全部算掉**，最终编出来的内核只含命中那条路径的代码。

> 为什么必须编译期？因为 `kind` 用来从一个 Python 字典里选 `prims.ReductionKind.ADD/MAX/...`，进而决定发射哪条硬件指令（`redux.sync` 还是 shuffle）。指令选择必须在代码生成之前完成，不能留到运行期。

再看 `softmax.py` 里一个更小的例子：

[quack/softmax.py:100](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L100) —— `cluster_y = const_expr(0) if const_expr(self.cluster_n == 1) else cute.arch.block_idx()[1]`。当 `cluster_n == 1`（编译期可知）时，`cluster_y` 直接是常量 `0`，连读取 block 索引的指令都不编进内核；否则才读取运行期索引。

#### 4.2.4 代码实践（本讲指定实践任务）

1. **实践目标**：在 `reduce.py` 的 `warp_reduce` 中找出 `const_expr` 的用法，并解释为何这些值必须是编译期可知的。
2. **操作步骤**：
   - 对照 [docs/dsl_control_flow.rst:73-102](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/docs/dsl_control_flow.rst#L73-L102)（if-else 与 `const_expr` 的语义）。
   - 在 [quack/reduce.py:42-59](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L42-L59) 中逐一标出 `const_expr(...)`：`dtype is not None`、`op is max`、`val_dtype == Int32`、`val_dtype == Float32 and arch.is_family_of(Arch.sm_100f)`、`kind is not None` 等。
   - 思考：如果把 `const_expr(op is max)` 改成普通 `if (op is max)`，会发生什么？
3. **需要观察的现象**：所有这些判断的输入（`op`、`dtype`、`arch`）在编译时都已确定，没有任何运行期 `Tensor`/`Int32` 值参与。
4. **预期结果**：你应能解释——`op` 是 Python 可调用对象（`Callable`），`dtype` 是类型对象，`arch` 是架构枚举，它们只能当编译期值用；而 `kind` 决定要发射的硬件指令，必须在生成 IR 前定下来。若改成运行期 `if`，DSL 会因为「无法在运行期根据值选择发射哪条指令」而报错或生成错误的代码。
5. 结论属于源码阅读型，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`warp_reduce` 里 `threads_in_group: cutlass.Constexpr[int]` 与参数 `val: cute.Numeric` 有什么本质区别？

**答案**：`threads_in_group` 是**静态值**（编译期常量），不同的取值会编译出不同的 cubin；`val` 是**动态值**（运行期才知道的张量/数值），在 GPU 上每条线程都不同。

**练习 2**：文档说「把动态值传给原生 Python 控制流会报错」（[docs/dsl_control_flow.rst:20](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/docs/dsl_control_flow.rst#L20)）。请结合 `const_expr` 解释这句话。

**答案**：原生 Python 控制流（如 `cutlass.range_constexpr` 的循环边界、`if const_expr(...)` 的谓词）要求操作数是编译期已知的静态值。如果把一个动态值（比如某个 `Int32` 运行期变量）塞进 `const_expr(...)`，DSL 无法在编译时算出它的真假，就会报错（文档里标 ❌ 的例子正是如此）。

---

### 4.3 三种循环：range / cutlass.range / cutlass.range_constexpr

#### 4.3.1 概念说明

DSL 识别三种 `for` 循环（见 [docs/dsl_control_flow.rst:24-32](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/docs/dsl_control_flow.rst#L24-L32)）：

| 写法 | 何时求值 | 产物 | 典型用途 |
| --- | --- | --- | --- |
| `range(...)`（Python 内建） | 运行期 | **一定**翻译成 IR 循环 | 真正的运行期循环 |
| `cutlass.range(...)` | 运行期 | 翻译成 IR 循环，但支持展开/流水线提示（如 `unroll=2`、`unroll_full=True`） | 想要循环又想控制展开 |
| `cutlass.range_constexpr(...)` | **编译期** | **完全展开**，循环在代码生成前消失 | 编译期展开、按索引选择寄存器槽 |

关键差别：

- `range_constexpr` 在 Python 解释器里跑、**完全展开**，所有循环索引必须是 `Constexpr`（[docs/dsl_control_flow.rst:37-41](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/docs/dsl_control_flow.rst#L37-L41)）。
- `range` / `cutlass.range` 即使边界是 Python 值，也会生成 IR 里的循环（[docs/dsl_control_flow.rst:32-36](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/docs/dsl_control_flow.rst#L32-L36)）。

#### 4.3.2 核心流程

什么时候用哪种？决策树：

```text
循环索引需要在编译期「按名字」访问寄存器 / 选择代码路径？
  ├─ 是 → cutlass.range_constexpr（必须完全展开，索引为 Constexpr）
  └─ 否 → 循环要在 GPU 上真正跑？
            ├─ 是，且想控制展开度 → cutlass.range(unroll=...)
            └─ 是，普通循环      → range(...)
```

最常见、也最容易出错的是第一种：当代码写 `rmem_tensor[i]` 这种「按编译期索引 `i` 访问某个寄存器槽」时，`i` 必须是编译期常量，循环必须用 `range_constexpr` 完全展开，否则 `rmem_tensor[i]` 里的 `i` 在运行期无法解析成具体的寄存器。

#### 4.3.3 源码精读

先看 `range_constexpr` 在 `cluster_reduce` 里的用法：

[quack/reduce.py:152-160](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L152-L160) —— `for i in cutlass.range_constexpr(num_iter):` 这里 `num_iter = cute.ceil_div(warps_per_row * cluster_n, cute.arch.WARP_SIZE)` 是编译期可算的值，循环体里用 `i` 作为索引 `buf[row_idx, idx]` 累加。因为每个 `i` 对应一段需要展开的代码，所以必须完全展开。

`online_softmax_reduce` 里更典型——按 `i` 访问**寄存器张量**槽位：

[quack/reduce.py:348-353](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L348-L353) 与 [quack/reduce.py:359-362](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L359-L362) —— `max_x_single_warp[i]` / `sum_exp_x_single_warp[i]` 是寄存器（rmem）张量，下标 `i` 必须在编译期确定具体槽位，所以用 `range_constexpr` 展开。

再看 `cutlass.range`（保留为 IR 循环 + 展开提示）的对比示例，来自 `broadcast_utils.py`：

[quack/broadcast_utils.py:21-26](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/broadcast_utils.py#L21-L26) —— `for r in cutlass.range(cute.size(...), unroll_full=True):`。这里也想完全展开，但用的是 `cutlass.range(unroll_full=True)` 而非 `range_constexpr`——区别在于 `cutlass.range` 的边界可以是**动态值**（运行期才知道 `cute.size(...)`），而展开度由 `unroll_full` 提示控制；`range_constexpr` 则要求边界本身就是 `Constexpr`。

#### 4.3.4 代码实践

1. **实践目标**：对比 `range_constexpr` 与 `cutlass.range`，理解「为什么前者必须完全展开」。
2. **操作步骤**：
   - 在 [quack/reduce.py:156](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L156)（`cluster_reduce`）找到 `for i in cutlass.range_constexpr(num_iter):`。
   - 在 [quack/broadcast_utils.py:21](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/broadcast_utils.py#L21) 找到 `for r in cutlass.range(..., unroll_full=True):`。
   - 思考：`cluster_reduce` 里如果把 `range_constexpr` 换成 `cutlass.range`，循环体里的 `idx = lane_idx + i * cute.arch.WARP_SIZE` 还能正确编译吗？
3. **需要观察的现象**：`cluster_reduce` 的循环体只是用 `i` 算一个索引去读 `buf`，理论上换成 `range` 也能编译；但 `online_softmax_reduce`（L348/L359）里 `i` 被用来索引**寄存器张量** `max_x_single_warp[i]`，这种用法**必须**用 `range_constexpr`。
4. **预期结果**：你能指出「按编译期索引访问寄存器槽」是强制使用 `range_constexpr` 的根本原因；而当循环体不涉及编译期索引、且希望边界可动态时，用 `cutlass.range`。
5. 结论属于源码阅读型，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：下面两段代码，哪段能通过 DSL 编译？为什么？

```python
# A
for i in cutlass.range_constexpr(bound):   # bound 是一个 Int32 运行期参数
    ...

# B
for i in range(bound):                      # bound 是一个 Int32 运行期参数
    ...
```

**答案**：**B 能通过，A 不能**。`range_constexpr` 要求循环索引/边界是编译期 `Constexpr`（见 [docs/dsl_control_flow.rst:59-62](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/docs/dsl_control_flow.rst#L59-L62) 标 ❌ 的例子）；运行期的 `bound` 应该用 `range` 或 `cutlass.range`，它们会把循环翻译成 IR。

**练习 2**：`cutlass.range(x, unroll=2)` 与 `range(x)` 生成的 IR 有何不同？

**答案**：两者都会生成一条 IR 循环，但 `unroll=2` 提示编译器每次展开 2 个循环体（控制展开度与流水线），`range` 则交给编译器默认策略。`unroll_full=True` 则提示完全展开（[quack/broadcast_utils.py:21](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/broadcast_utils.py#L21)）。

---

### 4.4 控制流限制与「源码必须落盘」约束

#### 4.4.1 概念说明

DSL 只支持 Python 的一个**子集**（见 [docs/limitations.rst:8-12](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/docs/limitations.rst#L8-L12)）。控制流方面的主要限制（[docs/dsl_control_flow.rst:158-167](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/docs/dsl_control_flow.rst#L158-L167)）：

- **不支持提前退出**：循环体内不能 `break` / `continue` / `pass` / `raise`，`if` 体内不能 `return`。
- **控制流体内定义的值，体外不可见**：在 `if` / `for` 体里赋值的变量，出了该控制流就用不了。
- **变量类型不可在控制流中改变**：同一个变量在循环体外是 `Int32`，体内不能改成 `Float32`（这是「无依赖类型」约束，[docs/limitations.rst:77-90](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/docs/limitations.rst#L77-L90)）。
- **变量必须先于控制流定义**。

另外，列表 / 字典的结构在内核执行期间不可改（[docs/limitations.rst:16-29](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/docs/limitations.rst#L16-L29)）：它们只能当编译期的「容器」，不能用动态下标去改结构。

还有一个**容易踩坑的环境约束**——**源码必须落盘**。DSL 用 `inspect.getsourcelines()` 去读取 `@cute.jit` / `@cute.kernel` 函数的源码来做 AST 解析。如果你在交互式 Python REPL（或 `python -c`）里**直接定义**这些函数，源码拿不到，会抛 `OSError: could not get source code`。

#### 4.4.2 核心流程

为什么有这些限制？因为 DSL 要把 Python 控制流翻译成 **结构化的 MLIR 控制流**（[docs/limitations.rst:108-117](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/docs/limitations.rst#L108-L117)）。结构化控制流要求：每个块有单一的入口/出口、变量的类型在整个控制流中一致。Python 那种「随时 `return`、随时改类型」的动态特性无法映射成结构化 IR，所以被禁止。

「源码落盘」的流程则是：

```text
@cute.kernel def kernel(...):   ← 装饰器触发 DSL
        ↓
inspect.getsourcelines(kernel)  ← DSL 读取该函数源码文本
        ↓
解析成 AST → 翻译成 MLIR IR → 编译
```

`inspect.getsourcelines()` 依赖函数所在 `.py` 文件。REPL 里的函数没有对应文件，所以失败。解决办法：**把内核函数写进一个 `.py` 文件**（哪怕用临时文件）再 import。

#### 4.4.3 源码精读

项目里专门记录了这条约束。见仓库的 `AGENTS.md`：

[AGENTS.md（CuTe DSL Conventions 小节的 Note）](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/AGENTS.md) —— 明确写道：DSL 依赖 `inspect.getsourcelines()` 解析内核定义，在普通 Python REPL 里直接定义 `@cute.kernel` / `@cute.jit` 会因源码检查失败而报 `OSError: could not get source code`，应写入临时文件。

这也是为什么 QuACK 的所有测试都把 `@cute.kernel` 写在 `.py` 文件里。看测试文件里的最小内核：

[tests/dsl/test_mixed_constexpr_if.py:229-237](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/dsl/test_mixed_constexpr_if.py#L229-L237) —— 一个 `@cute.kernel`，函数体里是「合法」的控制流：变量 `val` 先于 `if` 定义、类型全程是 `Int32`、没有 `break`/`return`。注意它同时展示了 4.2 节的 `const_expr` 与 4.3 节的混合判断 `if const_expr(flag) and d == 1:`。

> 这个测试文件本身就是一份「DSL 控制流什么能写、什么不能写」的活教材，建议通读。

#### 4.4.4 代码实践

1. **实践目标**：亲手体验「源码必须落盘」约束，并验证一个最小内核能编译。
2. **操作步骤**：
   - 参考 [tests/dsl/test_mixed_constexpr_if.py:295-300](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/dsl/test_mixed_constexpr_if.py#L295-L300) 的写法，新建一个 `my_kernel.py` 文件（**必须是文件，不是 REPL**），里面写一个最小的 `@cute.kernel`，再用 `cute.compile(...)` 编译。
   - 然后尝试在 `python` 交互式终端里**直接粘贴**同样的 `@cute.kernel` 定义并调用 `cute.compile`。
3. **需要观察的现象**：文件版本能正常编译；REPL 版本会在 `cute.compile` 时抛出类似 `OSError: could not get source code` 的错误。
4. **预期结果**：你验证了「源码必须落盘」这条约束——DSL 需要从 `.py` 文件读取函数源码才能做 AST 解析。
5. 该实践需要 GPU 与 `cutlass-dsl` 环境，若无环境则**待本地验证**；可退化为「源码阅读型实践」：通读 [docs/limitations.rst:108-117](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/docs/limitations.rst#L108-L117) 与 [tests/dsl/test_mixed_constexpr_if.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/dsl/test_mixed_constexpr_if.py) 来确认合法控制流的写法。

#### 4.4.5 小练习与答案

**练习 1**：下面这段内核代码违反了哪条 DSL 限制？

```python
@cute.jit
def foo(predicate: cutlass.Boolean):
    if predicate:
        val = 10
        return              # (A)
    cute.printf("%d\n", val)  # (B)
```

**答案**：违反两条：(A) 控制流体内不允许 `return`；(B) `val` 是在 `if` 体内定义的，出了 `if` 不可见（见 [docs/dsl_control_flow.rst:184-192](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/docs/dsl_control_flow.rst#L184-L192) 标 ❌ 的例子）。正确做法是把 `val` 在 `if` 之前定义好。

**练习 2**：为什么在 Jupyter / REPL 里直接写 `@cute.kernel` 会失败？

**答案**：DSL 装饰器会用 `inspect.getsourcelines()` 读取函数源码来做 AST 解析，而 REPL 里定义的函数没有对应的 `.py` 文件可读，于是抛 `OSError: could not get source code`（见 [AGENTS.md](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/AGENTS.md) 的 Note）。解决办法是写进文件再 import。

---

## 5. 综合实践

把本讲的四个模块串起来，完成一个「最小可编译内核」的端到端理解任务：

**任务**：以 [tests/dsl/test_mixed_constexpr_if.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/dsl/test_mixed_constexpr_if.py) 为模板，写一个 `@cute.jit` 函数 `_launch`，它调用一个 `@cute.kernel` `_pick`，要求：

1. `_pick` 接收一个 `flag: cutlass.Constexpr[bool]` 和一个 `d: Int32`。
2. 用 `if const_expr(flag):` 在编译期分两支：`flag=True` 时把输出写成 `Int32(1)`，`flag=False` 时再用运行期 `if d == 1:` 决定输出 `1` 还是 `2`。
3. 用 `cute.compile(_launch, ...)` 编译，并分别用 `flag=True` 和 `flag=False` 调用，观察是否得到两份不同的编译产物。

**完成后请回答**（把四个模块的知识用上）：

- 你用的是 `@cute.jit` 还是 `@cute.kernel` 作为并行内核入口？为什么？（对应 4.1）
- `flag` 为什么必须是 `Constexpr[bool]` 而不是 `Boolean`？（对应 4.2）
- 如果循环里要按编译期索引访问寄存器，该用哪种 `range`？（对应 4.3）
- 你的代码写在哪里才能被 `cute.compile` 成功解析？（对应 4.4）

> 参考答案要点：用 `@cute.kernel` 作为被 `.launch` 的并行内核，`@cute.jit` 作为编译入口编排者；`flag` 必须编译期可知才能用 `const_expr` 裁剪分支；寄存器槽访问用 `cutlass.range_constexpr`；代码必须写进 `.py` 文件，不能在 REPL。本任务需要 GPU 与 cutlass-dsl 环境，**待本地验证**。

## 6. 本讲小结

- `@cute.kernel` 定义**可启动的并行 CUDA 内核**（用 `.launch(grid, block, cluster)`），`@cute.jit` 定义**被编译的 Python 函数**，既能当主机编排者，也能当设备内联帮手。
- CuTe-DSL 的核心是**静态值（编译期）与动态值（运行期）**之分；`const_expr(...)` 把判断标记为编译期，从而**特化**出不同的 cubin。
- 三种循环：`range` / `cutlass.range` 生成 IR 循环（后者可带 `unroll` 提示），`cutlass.range_constexpr` 在编译期**完全展开**、索引必须为 `Constexpr`。
- DSL 只支持 Python 子集：控制流不能提前退出、体内变量体外不可见、类型不可在控制流中改变。
- **源码必须落盘**：DSL 靠 `inspect.getsourcelines()` 读源码，REPL 里直接定义内核会报 `OSError`。
- `cute.compile(...)` 是触发编译的入口，返回可调用的函数对象。

## 7. 下一步学习建议

本讲建立了 DSL 编程模型的地基。接下来建议：

1. **第 2 单元「归约内核」**：直接精读 [quack/reduce.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py) 与 [quack/softmax.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py)，把本讲的 `const_expr` / `range_constexpr` / `@cute.kernel` 放进一个完整内核里看它们如何协作。
2. **先看 `ReductionBase`**（第 2 单元第 1 讲）：它是 rmsnorm / softmax / cross_entropy 共享的基类，能帮你建立「主机侧配置 → 设备侧内核」的整体观。
3. **想深入了解 DSL 本体**：阅读 [docs/dsl_control_flow.rst](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/docs/dsl_control_flow.rst) 与 [docs/limitations.rst](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/docs/limitations.rst) 全文，以及上游 CuTe-DSL 文档（链接见 `AGENTS.md`）。
