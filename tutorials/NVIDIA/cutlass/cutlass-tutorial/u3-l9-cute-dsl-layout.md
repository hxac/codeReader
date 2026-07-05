# CuTe DSL（Python）：布局与张量

## 1. 本讲目标

本讲是专家层「CuTe DSL」系列的第一讲。在前两阶（u2-l1、u2-l2）里，我们已经在 C++ 里吃透了 CuTe 的两大基石——`Layout = (Shape, Stride)` 与 `Tensor = (Engine, Layout)`。本讲要把这两块基石**完整地搬到 Python**：

- 理解 CuTe DSL（CUTLASS 4.x）是什么、它的「Python 前端 + MLIR 后端」运行模型；
- 掌握用 `cute.make_layout` / `cute.make_tensor` 等 Python API 构造布局与张量；
- 看清 Python DSL 与 C++ CuTe 在概念上的**一一对应**，以及它们在「静态 vs 动态」表达上的差异；
- 跑通官方 `hello_world.ipynb`，并亲手验证一个 `(4,4):(1,4)` 布局的索引映射与 C++ 完全一致。

学完后，你应当能用 Python 流畅地写出与 C++ CuTe 等价的布局/张量代码，并为下一讲「CuTe DSL GEMM 实践」（u3-l10）铺好地基。

## 2. 前置知识

本讲默认你已经学完：

- **u2-l1 CuTe Layout 与布局代数**：`Layout = (Shape, Stride)` 是「坐标 → 下标」的纯函数；`crd2idx`、`composition`、`zipped_divide`、`local_tile` 等代数运算的含义。
- **u2-l2 CuTe Tensor 与引擎**：`Tensor = (Engine, Layout)`，`T(c) = *(E + L(c))`；指针被打上 gmem/smem/rmem 等内存空间标签；切片用下划线 `_` / `None`。

如果你对上面任意一个符号感到陌生，建议先回看这两讲。下面只补充本讲独有的、属于 DSL 的新概念：

- **DSL（Domain-Specific Language，领域特定语言）**：为某一类问题专门设计的语言或 API。CuTe DSL 指的是 CUTLASS 用 Python 写的、专门用来描述 GPU 张量计算的前端。
- **JIT（Just-In-Time，即时编译）**：程序运行到某处时才把它编译成机器码。CuTe DSL 用 `@cute.jit` 装饰的函数在第一次被调用时编译成 GPU 内核。
- **MLIR（Multi-Level Intermediate Representation，多层中间表示）**：一种可嵌套「方言」的编译器 IR。CuTe DSL 的 Python 代码不会直接变成 PTX，而是先构造一段 CuTe MLIR，再由后端下沉到 LLVM→PTX→CUBIN。
- **静态值 vs 动态值**：编译期就确定大小的（如字面量 `4`）是静态值；运行时才知道的（如 `cutlass.Int32(n)`）是动态值。DSL 在打印时用普通数字表示静态值，用 `?` 表示动态值。

## 3. 本讲源码地图

CuTe DSL 的 Python 源码位于 [`python/CuTeDSL/cutlass/cute/`](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/python/CuTeDSL/cutlass/cute)。注意：本讲的「Layout 抽象」并不在某个 `layout.py` 里，而是按「抽象接口 → 具体实现」分两处存放（这是 DSL 的真实组织方式，不要去找一个不存在的 `cute/layout.py`）：

| 文件 | 职责 | 本讲关注点 |
| --- | --- | --- |
| [`python/CuTeDSL/cutlass/cute/typing.py`](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/python/CuTeDSL/cutlass/cute/typing.py) | 抽象接口层（ABC）：`Shape/Stride/Coord` 类型别名、抽象类 `Layout`、`ComposedLayout`、`Tensor` | 它们定义了「概念契约」 |
| [`python/CuTeDSL/cutlass/cute/core.py`](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/python/CuTeDSL/cutlass/cute/core.py) | 具体实现 + 布局工厂与代数运算：`_Layout`、`make_layout`、`make_identity_layout`、`make_ordered_layout`、`crd2idx`、`local_tile` 等 | 用得最多的入口 |
| [`python/CuTeDSL/cutlass/cute/tensor.py`](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/python/CuTeDSL/cutlass/cute/tensor.py) | 张量实现与工厂：`_Tensor`、`make_tensor`、`make_identity_tensor` | `Tensor = Engine ∘ Layout` |
| [`python/CuTeDSL/cutlass/cute/__init__.py`](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/python/CuTeDSL/cutlass/cute/__init__.py) | 把上述符号统一汇聚成 `cute.*` 公共 API，并挂上 `cute.jit`/`cute.kernel`/`cute.compile` | 「用户只写 `import cutlass.cute as cute`」的真相 |
| [`python/CuTeDSL/cutlass/cute/runtime.py`](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/python/CuTeDSL/cutlass/cute/runtime.py) | 主机端运行时：`from_dlpack`（torch/numpy 互操作）、`make_ptr` | 把外部数组喂给 DSL |
| [`examples/python/CuTeDSL/cute/notebooks/hello_world.ipynb`](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/python/CuTeDSL/cute/notebooks/hello_world.ipynb) | 最小可运行示例：`@cute.kernel` + `@cute.jit` + `.launch()` | 跑通第一个 DSL 程序 |
| [`examples/python/CuTeDSL/cute/notebooks/cute_layout_algebra.ipynb`](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/python/CuTeDSL/cute/notebooks/cute_layout_algebra.ipynb) | Python 版布局代数（coalesce/composition/divide/product）大全 | 与 C++ 文档逐条对照 |
| [`examples/python/CuTeDSL/cute/notebooks/tensor.ipynb`](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/python/CuTeDSL/cute/notebooks/tensor.ipynb) | 张量创建、元素访问、切片、坐标张量 | `make_tensor` 实操 |

> 小提示：C++ CuTe 的 `Layout`/`Tensor` 是**模板**，参数在编译期决议；DSL 的同名类则是**包装了 MLIR IR Value 的 Python 对象**，参数在「构造 IR 时」决议。两者名字、语义一致，落地形态不同。

---

## 4. 核心概念与源码讲解

### 4.1 DSL 概述与运行模型

#### 4.1.1 概念说明

CUTLASS 4.x 的 CuTe DSL 是一套 **Python 前端**：你用普通 Python 写张量计算，DSL 在背后把这些 Python 调用翻译成 **CuTe MLIR**（一种带 `cute` 方言的中间表示），再由编译后端继续下沉为 PTX/CUBIN，最终在 GPU 上跑。

为什么要再做一套 Python 前端？因为 C++ 模板元编程写 GEMM 调试痛苦、迭代慢；Python 让你能用「解释器式」的体验去拼装内核，同时仍然拿到与 C++ 同级的性能（核心计算还是落在同一套 CuTe IR 与同一条硬件指令路径上）。

DSL 的代码分两种角色，与 CUDA 的 host/device 划分一一对应：

- **`@cute.kernel` 装饰的函数** → **设备端内核**（device kernel），真正跑在 GPU 上。
- **`@cute.jit` 装饰的函数** → **主机端函数**（host JIT function），跑在 CPU 上，负责设置网格、调用 `.launch(...)` 启动上面的 kernel。

这两个装饰器在 `__init__.py` 里只是 `cutlass_dsl.CuTeDSL` 方法的别名：

[python/CuTeDSL/cutlass/cute/__init__.py:221-224](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/python/CuTeDSL/cutlass/cute/__init__.py#L221-L224) — 把 `CuTeDSL.jit` / `CuTeDSL.kernel` / `CompileCallable` 暴露成 `cute.jit` / `cute.kernel` / `cute.compile`。

#### 4.1.2 核心流程

一次 DSL 程序的执行可以概括为：

```text
用户写 @cute.jit / @cute.kernel 的 Python 函数
        │  (第一次调用时触发)
        ▼
解释器执行函数体 → 每个 cute.make_layout / make_tensor 调用
        │           都「惰性」构造一段 CuTe MLIR IR
        ▼
得到完整的 MLIR IR 图（含静态/动态信息）
        │  (cute.compile 可显式触发，并 dump PTX/CUBIN)
        ▼
后端：MLIR → LLVM IR → PTX → ptxas → CUBIN
        │
        ▼
主机端 .launch(grid=..., block=...) 把 CUBIN 推给 GPU 执行
```

关键认知有三点：

1. **惰性建 IR**：你在 `@cute.jit` 里写 `L = cute.make_layout((4,4), stride=(1,4))`，并不是「立即得到一个 Python 对象然后算了」，而是「向当前 IR 插入点发射一条构造该 Layout 的 IR 操作」。返回的 `L` 是包着 `ir.Value` 的 `_Layout` 对象。
2. **静态 vs 动态在 IR 里区分**：静态量（字面量 `4`）被编进 IR 类型，编译期完全已知；动态量（`cutlass.Int32(n)`）在 IR 里是带运行时值的「符号整数」`SymInt`，打印成 `?`。
3. **两套打印反映两个阶段**：`print(L)`（Python 端）打印的是 IR 的**静态类型信息**；`cute.printf("{}", L)`（设备端 printf）打印的是**运行时实际值**。这一点在布局代数 notebook 里反复演示，是理解 DSL 输出的钥匙。

#### 4.1.3 源码精读

`hello_world.ipynb` 用最少的代码展示了 host/device 两层结构。设备端 kernel：

[examples/python/CuTeDSL/cute/notebooks/hello_world.ipynb cell-3](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/python/CuTeDSL/cute/notebooks/hello_world.ipynb) — `@cute.kernel def kernel():` 内用 `cute.arch.thread_idx()` 取线程号，`cutlass.dynamic_expr(tidx == 0)` 把运行时比较包成 DSL 可识别的条件，只有 0 号线程执行 `cute.printf("Hello world")`。

主机端 jit 函数负责启动它：

[examples/python/CuTeDSL/cute/notebooks/hello_world.ipynb cell-5](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/python/CuTeDSL/cute/notebooks/hello_world.ipynb) — `@cute.jit def hello_world():` 先在主机打印 `hello world`，再用 `kernel().launch(grid=(1,1,1), block=(32,1,1))` 启动一个含 32 线程（一个 warp）的内核。

运行方式有两种：直接调用 `hello_world()`（边编译边跑），或先 `cute.compile(hello_world)` 再反复执行（编译一次、跑多次）。notebook 还演示了 `cute.compile[KeepPTX, KeepCUBIN](hello_world)` 在编译期 dump 出 PTX/CUBIN 文件——这正是上面流程图里「MLIR → PTX → CUBIN」那一段的可观测入口。

#### 4.1.4 代码实践

- **实践目标**：跑通 `hello_world.ipynb`，亲眼看一遍「Python → MLIR → GPU」的完整链路。
- **操作步骤**：
  1. 安装 DSL 包（在仓库根目录）：`pip install -e python/CuTeDSL`（具体依赖见 `python/CuTeDSL/pyproject.toml`，需要 GPU 与匹配的 CUDA 驱动）。
  2. 启动 jupyter：`jupyter notebook examples/python/CuTeDSL/cute/notebooks/hello_world.ipynb`，从上到下逐格执行。
  3. 也可直接当脚本跑：把 notebook 的 cell 顺序拼成一个 `.py` 用 `python` 执行。
- **需要观察的现象**：先看到 `Running hello_world()...`，随后出现 `hello world`（主机打印）与 `Hello world`（设备打印）。notebook 提示：因为 CPU 与 GPU 的打印是**异步**的，`Compiling...`（第二个方法的）可能先于第一个 kernel 的 `Hello world` 出现——这是正常的异步现象，值得留意。
- **预期结果**：与 notebook 已保存的输出一致：
  ```text
  Running hello_world()...
  Compiling...
  hello world
  Hello world
  Compiling with PTX/CUBIN dumped...
  Running compiled version...
  hello world
  Hello world
  ```
- **若无 GPU / 未装 DSL**：本步骤需真实 CUDA 环境，运行结果**待本地验证**。可退化为「源码阅读型实践」——只读上面三个 cell，画出 host→kernel→launch 的调用关系图。

#### 4.1.5 小练习与答案

- **练习 1**：`@cute.kernel` 和 `@cute.jit` 各自装饰的函数分别跑在 CPU 还是 GPU？
  - **答**：`@cute.kernel` 跑在 GPU（设备端内核）；`@cute.jit` 跑在 CPU（主机端，负责编译与 launch）。
- **练习 2**：为什么 notebook 里会出现「`Compiling...` 先于前一个 kernel 的 `Hello world`」？
  - **答**：主机 `print` 立即输出，而设备 `cute.printf` 要等内核在 GPU 上真正执行、再异步回传，两者不保证按代码顺序到达屏幕。

---

### 4.2 Python Layout

#### 4.2.1 概念说明

Python DSL 的 `Layout` 与 C++ CuTe 完全同构：它是一对 `(Shape, Stride)`，是一个把**逻辑坐标映射到线性下标**的纯函数，**本身不持有任何数据**。数据要等到下一节 `Tensor` 才登场。

`Shape` 与 `Stride` 都是可任意嵌套的「整数元组」（IntTuple）。Shape 描述每一维有多大；Stride 描述该维每跨一步下标前进多少。对扁平坐标 \(c=(c_0,c_1,\dots)\)，下标为：

\[
\text{idx} = \sum_i c_i \cdot \text{stride}_i
\]

省略 `stride` 时，DSL 自动推导一个紧凑布局（官方文档称 *compact left-most*）。

#### 4.2.2 核心流程

构造与使用一个 Layout 的典型步骤：

1. `L = cute.make_layout(shape, stride=...)` 构造。
2. 用 `cute.crd2idx(coord, L)` 或 `L(coord)` 把坐标映射成下标。
3. 用代数运算变换布局：`coalesce`（合并化简）、`composition(A, B)`（复合 \(A \circ B\)）、`complement`（补）、`logical_divide`/`zipped_divide`/`tiled_divide`/`flat_divide`（分块的各种排布）、`logical_product`/`blocked_product`/`raked_product`（复制 tile）。
4. 配合张量做切片：`local_tile(tensor, tiler, coord)` 取一块。

#### 4.2.3 源码精读

**类型契约**先看抽象层。`Shape/Stride/Coord` 是带递归的类型别名：

[python/CuTeDSL/cutlass/cute/typing.py:330-333](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/python/CuTeDSL/cutlass/cute/typing.py#L330-L333) — 定义 `IntTuple`、`Shape`、`Stride`（注意 `Stride` 可以是 `ScaledBasis`，对应 C++ 的 `1@k` 表达）、`Coord` 四个递归类型别名。

抽象类 `Layout(ir.Value)` 只声明契约（`shape`、`stride`、`get_hier_coord`），不带实现：

[python/CuTeDSL/cutlass/cute/typing.py:336-371](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/python/CuTeDSL/cutlass/cute/typing.py#L336-L371) — `Layout` 继承自 MLIR 的 `ir.Value`，用 `@abstractmethod` 声明 `shape`/`stride`/`get_hier_coord`，说明「Layout 本质上就是一段 IR 值」。

**具体实现**是 `_Layout`，它的文档串把「Layout = (Shape, Stride)」讲得很直白，并给出与本讲练习任务同款的例子：

[python/CuTeDSL/cutlass/cute/core.py:1109-1141](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/python/CuTeDSL/cutlass/cute/core.py#L1109-L1141) — `_Layout` 类文档：`make_layout((4,8))` 默认紧凑布局；`make_layout((4,8), stride=(8,1))` 显式给步长；`crd2idx((2,3), layout) = 2*8 + 3*1 = 19`。

`_Layout.__call__` 是「调用布局 = 把它当函数用」的入口，且区分「取下标」与「切片」两种语义：

[python/CuTeDSL/cutlass/cute/core.py:1289-1300](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/python/CuTeDSL/cutlass/cute/core.py#L1289-L1300) — 若坐标含下划线（`_`/`None`）走 `_cute_ir.slice`（切片，返回子布局），否则走 `crd2idx`（取下标）。

**工厂函数** `make_layout` 做「校验 congruent → 打包 shape/stride → 调底层 IR 构造」：

[python/CuTeDSL/cutlass/cute/core.py:3569-3634](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/python/CuTeDSL/cutlass/cute/core.py#L3569-L3634) — `make_layout(shape, *, stride=None)`；省略 stride 时只传 shape 给 IR，由 IR 推导紧凑步长；`stride` 是**仅关键字参数**（避免与嵌套元组歧义）。

同族还有两个常用工厂：`make_identity_layout(shape)`（步长为 `1@0,1@1,...` 的恒等布局）与 `make_ordered_layout(shape, order)`（按 `order` 指定「从快到慢」的维度顺序，`order=(1,0)` 即行主序、`(0,1)` 即列主序）：

[python/CuTeDSL/cutlass/cute/core.py:3678-3724](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/python/CuTeDSL/cutlass/cute/core.py#L3678-L3724) — `make_ordered_layout`，文档明确：`(4,4)` 配 `order=(0,1)` 得 `stride=(1,4)`（列主序）。

**核心转换** `crd2idx(coord, layout)` 就是上式 \(\sum_i c_i \cdot s_i\) 的实现：

[python/CuTeDSL/cutlass/cute/core.py:4005-4052](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/python/CuTeDSL/cutlass/cute/core.py#L4005-L4052) — 文档给出 `make_layout((5,4), stride=(4,1))`，`crd2idx((2,3), L) = 2*4 + 3 = 11`；若 `layout` 传的是元组/整数会自动 `make_layout`。

**代数运算**的 Python 入口都在 `__init__.py` 里从 `core` 重新导出，包括 `coalesce`、`composition`、`complement`、`logical_divide`/`zipped_divide`/`tiled_divide`/`flat_divide`、`logical_product`/`blocked_product`/`raked_product`、`local_tile`、`local_partition` 等：

[python/CuTeDSL/cutlass/cute/__init__.py:44-133](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/python/CuTeDSL/cutlass/cute/__init__.py#L44-L133) — `from .core import (...)` 把布局工厂与全部代数运算挂到 `cute.*`。

`local_tile` 是分块取片的高层封装，内部第一步就是 `zipped_divide`：

[python/CuTeDSL/cutlass/cute/core.py:5050-5106](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/python/CuTeDSL/cutlass/cute/core.py#L5050-L5106) — 数学定义 \(\text{local\_tile}(\text{input}, \text{tiler}, \text{coord}) = \text{zipped\_divide}(\text{input}, \text{tiler})[\text{coord}]\)，文档举例 `(16,24)` 张量用 `tiler=(2,4)`、`coord=(1,1)` 取出 `(8,6)` 的块。

> 关于「静态 vs 动态」最直观的演示见 `cute_layout_algebra.ipynb` 的 `composition_static_vs_dynamic_layout`：同样的 `(10,2):(16,4)` 与 `(5,4):(1,5)`，静态版 `print` 直接给出化简结果 `(5,(2,2)):(16,(80,4))`，动态版（用 `cutlass.Int32`）多保留了几处 size-1 模式——因为对「未知运行时值」做化简在数学上不安全。

#### 4.2.4 代码实践

- **实践目标**：亲手验证 `(4,4):(1,4)` 的索引映射。
- **操作步骤**（在装好 DSL 的环境里，存为 `t.py` 运行）：
  ```python
  # 示例代码
  import cutlass
  import cutlass.cute as cute

  @cute.jit
  def check_layout():
      L = cute.make_layout((4, 4), stride=(1, 4))   # (4,4):(1,4)
      print(">>> layout:", L)
      for i in cutlass.range_constexpr(4):
          for j in cutlass.range_constexpr(4):
              idx = cute.crd2idx((i, j), L)
              cute.printf("crd ({},{}) -> idx {}", i, j, idx)

  check_layout()
  ```
- **需要观察的现象**：`print(L)` 应打印出 `(4,4):(1,4)`；`cute.printf` 逐格打印坐标到下标的映射。
- **预期结果**：由 `idx = i*1 + j*4`：
  | (i,j) | idx | | (i,j) | idx |
  | --- | --- | --- | --- | --- |
  | (0,0) | 0 | | (0,1) | 4 |
  | (1,0) | 1 | | (1,1) | 5 |
  | (2,0) | 2 | | (3,3) | 15 |
  即第 0 列连续占据下标 0–3、第 1 列占据 4–7……这正是 4×4 **列主序**矩阵的排布。
- **若无 GPU / 未装 DSL**：运行结果**待本地验证**；可退化为「纸笔验证」——按 \(\sum_i c_i s_i\) 手算全部 16 个坐标，确认与上表一致。

#### 4.2.5 小练习与答案

- **练习 1**：`make_layout((4,4), stride=(1,4))` 与 `make_ordered_layout((4,4), order=(0,1))` 得到的布局是否相同？
  - **答**：相同。后者 `order=(0,1)` 即列主序、步长 `(1,4)`，与前者显式给出 `(1,4)` 一致。
- **练习 2**：把 `coalesce` 作用到 `(2,(1,6)):(1,(6,2))` 上，结果是什么？
  - **答**：`12:1`（见 `cute_layout_algebra.ipynb` 的 `coalesce_example`）。它把多模式拍平合并为单模式，且作为「整数上的函数」保持不变——`size` 与每个下标的映射都不变。
- **练习 3**：`crd2idx((2,3), make_layout((5,4), stride=(4,1)))` 等于几？
  - **答**：\(2\times4 + 3\times1 = 11\)（`crd2idx` 源码文档示例）。

---

### 4.3 Python Tensor

#### 4.3.1 概念说明

有了 Layout，再把「数据」接上就是 Tensor。DSL 的张量定义与 C++ CuTe 一字不差：

\[
T(c) = (E \circ L)(c) = *(E + L(c))
\]

其中 \(E\) 是 Engine（提供 `e + d` 偏移与 `*e` 解引用），\(L\) 是 Layout。访问坐标 \(c\) 时：先用 Layout 算出下标 \(L(c)\)，再把引擎（指针）前移这么多、解引用得到值。

Engine 有三种来源：
- **指针型**：`make_ptr(dtype, base, address_space)` 造的指针，带 gmem/smem/rmem 空间标签；
- **整数 / 整数元组型**：得到「坐标张量」（coordinate tensor），把坐标映射到坐标，常用作 `make_identity_tensor`；
- **共享内存描述符型**（SmemDesc）：SM90+ 描述符视图。

#### 4.3.2 核心流程

1. 准备 Engine：用 `make_ptr` 显式构造，或用 `from_dlpack` 把 torch/numpy 数组直接包成张量。
2. `T = cute.make_tensor(engine, layout)` 组合。
3. 访问：`T[i, j]` 取元素（完整坐标）；`T[None, j]` 或 `T[_, j]` 做切片（不完整坐标），返回子张量。
4. `T.element_type` / `T.layout` / `T.shape` / `T.memspace` 查询属性。

#### 4.3.3 源码精读

抽象 `Tensor(ABC)` 把数学定义写进了文档串：

[python/CuTeDSL/cutlass/cute/typing.py:685-745](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/python/CuTeDSL/cutlass/cute/typing.py#L685-L745) — 给出公式 \(T(c) = (E \circ L)(c) = *(E + L(c))\)，并注明 load/store 仅在 rmem/smem/gmem/generic 等特定空间可用；同文件还给出 host 侧用 `from_dlpack(torch.tensor(...))` 创建张量、用 `@cute.jit` 定义 `add` 内核的最小示例。

具体实现 `_Tensor` 同样在文档里给出创建与访问范例：

[python/CuTeDSL/cutlass/cute/tensor.py:122-147](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/python/CuTeDSL/cutlass/cute/tensor.py#L122-L147) — 示例：`make_tensor(ptr, make_layout(shape=(4,8), stride=(8,1)))`；`tensor[0,0]` 取元素；`tensor[None, 0]` 取第 0 列的切片。

`_Tensor.__getitem__` 实现了「完整求值 vs 切片」的分流，与 C++ CuTe 的 `operator()`/`operator[]` 语义对应：

[python/CuTeDSL/cutlass/cute/tensor.py:230-317](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/python/CuTeDSL/cutlass/cute/tensor.py#L230-L317) — 文档说明：坐标无下划线时 `T(c) = *(E + L(c))`（取值），含下划线时 `T(c) = make_tensor(E + L(c'), slice(L, c))`（切片）；对指针型最终落到 `_cute_ir.memref_load`。

工厂 `make_tensor(iterator, layout)` 是「Engine + Layout → Tensor」的总入口，文档把 \(T = E \circ L\) 再强调了一遍：

[python/CuTeDSL/cutlass/cute/tensor.py:717-813](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/python/CuTeDSL/cutlass/cute/tensor.py#L717-L813) — 按 `iterator` 类型分派：整数/元组→坐标张量（`CoordTensorType`）、`Pointer`→`MemRefType`、`SmemDesc`→`SmemDescViewType`；若 `layout` 给的是裸 shape 会自动 `make_layout`，若是「normal」的 ComposedLayout 会自动退化为普通 Layout。

主机侧把外部数组喂进来的 `from_dlpack`，是 DSL 与 torch/numpy/jax 互操作的桥梁：

[python/CuTeDSL/cutlass/cute/runtime.py:804-861](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/python/CuTeDSL/cutlass/cute/runtime.py#L804-L861) — 凡是支持 `__dlpack__` 的对象都能转成 CuTe `Tensor`；对 `torch.float4_e2m1fn_x2` 等子字节打包 dtype，会返回**逻辑元素布局**（如 `(128,128)` 的 x2 打包张量暴露为 `(128,256)` 的 FP4 张量）。

`tensor.ipynb` 把上述用法串成一个可跑的例子：用 `make_ptr` 拿 torch 张量首地址、套 `(8,5):(5,1)` 布局、`fill(1)` 填充并打印；随后用 `from_dlpack` 更省事地做同样的事：

[examples/python/CuTeDSL/cute/notebooks/tensor.ipynb cell-2](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/python/CuTeDSL/cute/notebooks/tensor.ipynb) — `make_layout((8,5), stride=(5,1))` + `make_tensor(ptr, layout)` + `tensor.fill(1)` + `cute.print_tensor(tensor)`。

#### 4.3.4 代码实践

- **实践目标**：用 `from_dlpack` 把一个 torch 张量包成 CuTe Tensor，验证其 `shape`/`layout` 与原数组一致。
- **操作步骤**（装好 DSL + torch 后运行）：
  ```python
  # 示例代码
  import torch, cutlass
  from cutlass.cute.runtime import from_dlpack

  a = torch.arange(0, 8 * 5, dtype=torch.float32).reshape(8, 5)
  mA = from_dlpack(a)
  print(mA.shape)   # 预期 (8,5)
  print(mA.stride)  # 预期 (5,1)
  print(mA.layout)  # 预期 (8,5):(5,1)
  ```
- **需要观察的现象**：`mA.layout` 打印出的形状与步长，应与 `a` 的行主序排布一致（连续维为最右维 N=5，步长 1；跨步维 M=8，步长 5）。
- **预期结果**：`(8,5)` / `(5,1)` / `(8,5):(5,1)`，与 `typing.py` 中 `from_dlpack` 文档示例一致。
- **若无 GPU / 未装 DSL**：运行结果**待本地验证**；可退化为「源码阅读型实践」——阅读 `tensor.ipynb` 的 cell-6~cell-8，说明 `from_dlpack` 为何对 torch 与 numpy 都生效（答案：都实现了 DLPack 协议）。

#### 4.3.5 小练习与答案

- **练习 1**：`make_tensor(ptr, make_layout((4,8), stride=(8,1)))` 得到的张量是行主序还是列主序？
  - **答**：步长 `(8,1)` 表示第 0 维跨步 8、第 1 维跨步 1，即最右维连续，是**行主序**。
- **练习 2**：对一个指针型张量 `T`，`T[2, 3]` 与 `T[(2, 3)]` 有区别吗？
  - **答**：没有，都按完整坐标 `(2,3)` 求值，返回该元素（见 `_Tensor.__getitem__` 文档）。
- **练习 3**：`T[None, 0]` 返回的是什么？
  - **答**：返回一个**子张量**（切片），即第 0 列对应的视图，引擎已偏移到该列起点、布局被 `slice` 降维——不是单个元素。

---

### 4.4 与 C++ CuTe 对照

#### 4.4.1 概念说明

本讲的要旨是：**Python DSL 不是 CuTe 的「简易版」，而是同一套概念的另一种落地**。你在 u2-l1/u2-l2 学过的每一条 CuTe 规则，在 DSL 里都有同名、同义的对应物。理解这种一一对应，就能把已有的 C++ CuTe 经验无损迁移过来。

#### 4.4.2 核心对照表

下表把两侧的关键概念对齐（左列来自前两阶讲义与 C++ 源码，右列来自本讲所引 DSL 源码）：

| 概念 | C++ CuTe（u2-l1 / u2-l2） | Python DSL（本讲） |
| --- | --- | --- |
| 布局本质 | `Layout<Shape, Stride>`，坐标→下标纯函数 | `cute.Layout`，`(Shape, Stride)`，同样纯函数 |
| 构造 | `make_layout(make_shape(4,4), make_stride(1,4))` | `cute.make_layout((4,4), stride=(1,4))` |
| 坐标→下标 | `crd2idx(crd, layout)` | `cute.crd2idx(crd, layout)` 或 `layout(crd)` |
| 恒等布局 | `make_identity_layout(shape)` | `cute.make_identity_layout(shape)` |
| 复合 | `composition(A, B)` | `cute.composition(A, B)` |
| 分块取片 | `local_tile(tensor, tiler, coord)` | `cute.local_tile(tensor, tiler, coord)` |
| 张量本质 | `Tensor<Engine, Layout>`，`T(c)=*(E+L(c))` | `cute.Tensor`，`T(c)=(E∘L)(c)=*(E+L(c))` |
| 构造张量 | `make_tensor(ptr, layout)` | `cute.make_tensor(iterator, layout)` |
| 切片语义 | `operator()` 含 `_` 时切片 | `__getitem__` 含 `None`/`_` 时切片 |
| 内存空间 | 指针类型隐含 gmem/smem/rmem | `make_ptr(..., address_space)` 显式标注 |

#### 4.4.3 源码精读：同一份代数，两种落地

`cute_layout_algebra.ipynb` 在开头就点明：它的全部示例都**直接对应** C++ CuTe 的 `media/docs/cpp/cute/01_layout.md` 与 `02_layout_algebra.md`：

[examples/python/CuTeDSL/cute/notebooks/cute_layout_algebra.ipynb cell-0](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/python/CuTeDSL/cute/notebooks/cute_layout_algebra.ipynb) — 引用 C++ 的 01_layout.md / 02_layout_algebra.md，声明本 notebook 只是把同一套 Layout 代数（composition / divide / product）用 Python DSL 演示一遍。

也就是说，`coalesce`、`composition`、`logical_divide` 家族（`zipped_divide`/`tiled_divide`/`flat_divide`）、`logical_product` 家族（`blocked_product`/`raked_product`）这整套代数，DSL 与 C++ **同名、同后置条件、同输出**。例如 notebook 的 `logical_divide_2d_example` 把 `(9,(4,8)):(59,(13,1))` 用 tiler `(3:3, (2,4):(1,8))` 切分，得到 `((3,3),((2,4),(2,2))):((177,59),((13,2),(26,1)))`——这与 C++ CuTe `logical_divide` 逐位一致。

**真正的差异在「静态 vs 动态」的表达**，这是 DSL 特有的、C++ 没有的观感：

- C++ 里「动态」靠 `dynamic_extent` 或运行时 `int`，模板还是静态的，你看不到 `?`。
- DSL 里动态量用 `cutlass.Int32(x)` 构造，IR 里记成 `SymInt`，`print` 时该位显示为 `?`（静态信息不可知），只有 `cute.printf` 才打出真实运行时值。notebook 的 `composition_example`、`composition_static_vs_dynamic_layout` 把这一点演示得最清楚：同一组数，静态版 `print` 出 `(6,2):(8,2)`，动态版出 `(6,2):((?{div=3},2),?)`。

这套「静态优先、动态兜底」的设计哲学，与 u1-l4 讲过的 CUTLASS 数值类型「硬件优先、软件兜底」如出一辙：能编译期定的就编进 IR 类型（零运行时开销），定不了的才留下运行时符号。

#### 4.4.4 代码实践

- **实践目标**：用同一段数学描述，分别在「纸面 C++ CuTe」与「真实 Python DSL」上算同一组坐标→下标，确认两者完全一致。
- **操作步骤**：
  1. 选定布局 \(L = (4,4):(1,4)\)。
  2. **C++ 侧**（纸笔，回忆 u2-l1）：`crd2idx((i,j), L) = i + 4j`，列出 16 个坐标的下标。
  3. **DSL 侧**（运行 4.2.4 的 `check_layout`）：把 `cute.printf` 打出的 16 个下标抄下来。
  4. 逐格比对两份表。
- **需要观察的现象**：两边表格应**逐格相同**——这正是「DSL 与 C++ CuTe 概念一致」的最强证据。
- **预期结果**：与 4.2.4 的预期表完全一致（(0,0)→0 … (3,3)→15，列主序）。
- **若无法运行 DSL**：C++ 侧可借助 `cute_layout_algebra.ipynb` 已保存的输出作为「DSL 侧真值」，同样能完成比对。

#### 4.4.5 小练习与答案

- **练习 1**：用一句话概括 DSL 与 C++ CuTe 在 Layout/Tensor 上的关系。
  - **答**：概念与代数一一对应（同名同义），差异只在落地形态——C++ 是模板（编译期决议），DSL 是包着 MLIR IR 的 Python 对象（建 IR 时决议），并多出一套「静态 vs 动态（`?`）」的表达。
- **练习 2**：为什么 notebook 同时用 `print(L)` 和 `cute.printf("{}", L)` 两种打印？
  - **答**：前者打印 IR 的**静态类型**信息（动态位显示为 `?`），后者打印**运行时实际值**；两者对照能看清哪些是编译期已知、哪些是运行时才确定。
- **练习 3**：DSL 里要表达「这个维度运行时才知道大小」，该怎么做？
  - **答**：用 `cutlass.Int32(n)`（或 `cute.sym_int64(...)`）代替字面量构造该维，DSL 会把它建模成 `SymInt`，打印为 `?`，并保留可推导的整除性（`div=...`）信息供编译器优化。

---

## 5. 综合实践

把本讲的四个最小模块串起来，完成一个小任务：**用 Python DSL 构造一个 4×4 列主序张量视图，遍历它，并证明它的坐标→下标映射与 C++ CuTe 一致。**

任务分解：

1. **运行模型**（对应 4.1）：仿照 `hello_world.ipynb`，写一个 `@cute.jit` 函数作为主机入口；理解它会在第一次调用时编译成 MLIR→PTX→CUBIN。
2. **构造 Layout**（对应 4.2）：`L = cute.make_layout((4,4), stride=(1,4))`。
3. **构造 Tensor**（对应 4.3）：用一个「坐标张量」当 Engine 来演示映射——`coord_t = cute.make_identity_tensor(L.shape)`，再用 `make_tensor` 套上 `L`；或更贴近实战地用 `from_dlpack(torch.arange(16).reshape(4,4))` 取真实数据，再 `recast`/重排成 `(4,4):(1,4)` 的视图。
4. **验证一致性**（对应 4.4）：在 `@cute.jit` 内对每个 `(i,j)` 调 `cute.crd2idx((i,j), L)`，与 `4.4` 表格逐格比对。

参考骨架（示例代码，需在装好 DSL 的 GPU 环境运行）：

```python
# 示例代码
import cutlass
import cutlass.cute as cute

@cute.jit
def verify_column_major():
    L = cute.make_layout((4, 4), stride=(1, 4))           # 4.2 Layout
    t = cute.make_identity_tensor(L.shape)                 # 4.3 坐标张量作 Engine
    T = cute.make_tensor(t.iterator, L)                    # T = E ∘ L
    print(">>> layout:", L)
    for i in cutlass.range_constexpr(4):
        for j in cutlass.range_constexpr(4):
            idx = cute.crd2idx((i, j), L)
            cute.printf("crd ({},{}) -> idx {}", i, j, idx)

verify_column_major()                                      # 4.1 触发 JIT 编译并运行
```

- **验收标准**：`print(L)` 显示 `(4,4):(1,4)`；16 条 `crd (...) -> idx` 与 4.2.4 / 4.4.4 的预期表逐格相同（列主序：第 0 列 0–3，第 1 列 4–7，……，第 3 列 12–15）。
- **进阶**（可选）：把 `stride=(1,4)` 换成 `make_ordered_layout((4,4), order=(1,0))`（行主序），重新运行，观察下标序列变成「行连续」；再对 `L` 调一次 `cute.coalesce`，确认它作为「整数上的函数」不变。
- **若无 GPU 环境**：整任务**待本地验证**，但可全程用纸笔完成 C++ 侧计算，并把 notebook 已保存输出当 DSL 侧真值来比对，从而在不运行的情况下验证一致性。

## 6. 本讲小结

- CuTe DSL 是 CUTLASS 4.x 的 **Python 前端**：`@cute.kernel` 写设备内核、`@cute.jit` 写主机入口，背后把 Python 调用**惰性**翻译成 CuTe MLIR，再下沉为 PTX/CUBIN。
- Python `Layout` 与 C++ 同构：`cute.make_layout(shape, stride=...)` 造 `(Shape, Stride)` 纯函数；`cute.crd2idx(coord, L)` 或 `L(coord)` 算下标；整套代数（`coalesce`/`composition`/`*_divide`/`*_product`/`local_tile`）与 C++ 同名同义。
- Python `Tensor` 沿用 `T = E ∘ L`、`T(c) = *(E + L(c))`：`cute.make_tensor(iterator, layout)` 组合，Engine 可是指针、坐标元组或 SmemDesc；`from_dlpack` 让 torch/numpy 直接接入。
- DSL 特有的是「静态 vs 动态」表达：字面量为静态（编进 IR 类型），`cutlass.Int32(x)` 为动态（`SymInt`，打印为 `?`）；`print` 看静态类型、`cute.printf` 看运行时值。
- DSL 与 C++ CuTe **概念一一对应**：`cute_layout_algebra.ipynb` 明确声明它只是 C++ `01_layout.md`/`02_layout_algebra.md` 的 Python 演示，同一组布局在两侧输出逐位一致。
- 本讲验证了 `(4,4):(1,4)` 的索引映射为 \(i + 4j\)（列主序），与 C++ CuTe 完全相同。

## 7. 下一步学习建议

- **下一讲 u3-l10「CuTe DSL GEMM 实践」**：把本讲的 Layout/Tensor 用起来——用 `make_mma_atom`/`make_tiled_copy` 组装 GEMM 主循环，配合 `cute.copy`/`cute.gemm` 完成一个可跑的 Hopper GEMM，并尝试 autotune。
- **继续读源码**：
  - 布局代数的更多边角：`core.py` 中 `complement`、`right_inverse`/`left_inverse`、`max_common_vector` 等，对应 C++ `02_layout_algebra.md` 的进阶部分；
  - 张量的切片与重排：`tensor.py` 中 `recast_tensor`、`domain_offset`，以及 `core.py` 的 `recast_layout`/`group_modes`；
  - Swizzle（共享内存交错）：`cute_layout_algebra.ipynb` 引用的 `composed_layout.ipynb` 与 `core.py` 的 `make_swizzle`/`Swizzle`/`E`。
- **对照阅读**：把 `examples/python/CuTeDSL/cute/notebooks/cute_layout_algebra.ipynb` 的每个 cell 与 `media/docs/cpp/cute/02_layout_algebra.md` 的对应小节并排读，是巩固「两侧一致」直觉的最佳路径。
