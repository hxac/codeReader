# 布局可视化与 Analyzer

## 1. 本讲目标

学完本讲后，你应该能够：

1. 用 `tilelang.tools.plot_layout` 把一个 `T.Layout` 或 `T.Fragment` 画成二维网格图，区分「按输入空间画」和「按输出空间画」两种视角。
2. 打开编译期的 **LayoutVisual** Pass，让编译器在 `LayoutInference` 之后自动打印并保存它为每个 fragment 推理出的线程-寄存器布局。
3. 厘清项目里**两个同名却不同的「Analyzer」**：`tilelang.tools.Analyzer`（静态 roofline 性能估算器）与 TVM 的 `arith::Analyzer`（由 Z3 SMT 求解器支撑的符号分析器，被布局 C++ 代码用来做边界与整除证明）。
4. 理解 `Layout` / `Fragment` 提供的 `map_forward_index` / `map_forward_thread` 等「求值接口」是如何同时支撑手动绘图、自动可视化与编译器内部符号推理的。

## 2. 前置知识

本讲建立在 **u3-l4（布局标注、swizzle 与 L2 优化）** 之上，那里已经讲清了三个核心概念：`Layout` 是「逻辑索引到物理位置的纯函数」、`Fragment` 在 `Layout` 之上叠加了「线程-值（Thread-Value）映射」、以及 shared-memory swizzle 与 threadblock swizzle 是两件不同的事。本讲只新增「如何把这些抽象**看见**」与「如何**估算**一个 kernel 的性能上下限」两件事。

如果你还不熟悉下列概念，建议先建立直觉：

- **二维栅格可视化**：把一个多维缓冲拍扁成 (行, 列) 网格，每个格子涂一种颜色、写一个数字，是观察布局最直观的方式。
- **屋顶线模型（Roofline）**：一个 kernel 的运行时间下界由两条「屋顶」决定——算力屋顶 `计算量 / 峰值算力` 与带宽屋顶 `访存量 / 峰值带宽`，实际时间约为两者的最大值：

  \[
  T_{\text{est}} = \max\!\left(\frac{\text{FLOPs}}{\text{PeakFLOPS}},\ \frac{\text{Bytes}}{\text{PeakBW}}\right)
  \]

- **SMT 求解器（Z3）**：给定一组关于整数的等式与不等式约束，Z3 能判定是否存在解、或求出某个表达式在约束下的整数取值区间。编译器用它来证明「这个索引一定落在 `[0, 64)` 内」「这个除法一定能整除」之类的事实，从而省掉运行时边界检查。
- **matplotlib**：本讲的绘图工具底层就是它，输出 pdf / png / svg 三种格式。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [tilelang/layout/layout.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/layout/layout.py) | `Layout` 类：逻辑索引 → 物理位置的纯函数，提供 `map_forward_index` / `repeat` / `expand` / `inverse` 等求值与变换接口。 |
| [tilelang/layout/fragment.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/layout/fragment.py) | `Fragment` 类：继承 `Layout`，叠加 `forward_thread`（线程映射）与 `forward_index`（寄存器内偏移），是生成 MMA/WGMMA/MFMA 指令的依据。 |
| [tilelang/tools/plot_layout.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/plot_layout.py) | 手动绘图入口 `plot_layout`：按对象类型分派到 Fragment 视图（T/L）或 Layout 视图（input/output）。 |
| [tilelang/analysis/layout_visual.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/analysis/layout_visual.py) | 编译期 Pass `LayoutVisual`：遍历 SBlock 上的 `layout_map` 注解，打印并调用 `plot_layout` 保存推理出的 fragment 布局。 |
| [tilelang/backend/pass_pipeline/pipeline_utils.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/pass_pipeline/pipeline_utils.py) | 把 PassContext 配置翻译成 `LayoutVisual` 开关与格式的胶水层。 |
| [tilelang/cuda/pipeline.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/pipeline.py) | CUDA 后端编译流水线：`LayoutInference` → `LayoutVisual` → `LowerTileOp` 的接入点。 |
| [tilelang/tools/Analyzer.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/Analyzer.py) | **性能 Analyzer**：遍历 TIR 统计 `tl.gemm` 的 FLOPs 与 `tl.copy` 的 global 字节数，套屋顶线公式给出时间估计。 |
| [tilelang/carver/arch/cuda.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/arch/cuda.py) | `CUDA` 设备模型，给性能 Analyzer 提供 `compute_capability` 与 `bandwidth`。 |
| [src/layout/layout.cc](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/layout/layout.cc) | **符号 Analyzer** 的真实用武之地：用 TVM `arith::Analyzer` 的 `int_set` / `const_int_bound` / `Simplify` / `DetectIterMap` 做布局推理（由 `USE_Z3` 支撑）。 |
| [CMakeLists.txt](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/CMakeLists.txt) | `USE_Z3 ON`：把 Z3 SMT 求解器编进原生库，供 `arith::Analyzer` 在重约束下兜底。 |

## 4. 核心概念与源码讲解

### 4.1 被绘制的对象：Layout 与 Fragment

#### 4.1.1 概念说明

可视化工具本身不创造布局，它只是把已经存在的 `Layout` / `Fragment` 对象「翻译」成图。所以先要搞清这两个对象对外暴露了哪些**可求值的接口**——绘图和符号推理都依赖它们。

- `Layout` 回答的问题是：**逻辑下标 `(i, j, …)` 落在哪个物理位置？** 它由两部分定义：输入形状 `shape`，以及一个 `forward_fn`，把每一维的迭代变量映射成一组的「正向索引」。
- `Fragment` 回答的是更细的问题：**这个逻辑元素归哪个线程（thread）、落在该线程的哪个寄存器槽（local index）？** 因此它在 `Layout` 之上多了 `forward_thread`（线程维）与 `forward_index`（寄存器内偏移）。

一句话区分：`Layout` 只描述「位置」，`Fragment` 描述「位置 + 归属的线程/寄存器」。

#### 4.1.2 核心流程

构造一个 `Layout` 的流程是：

1. 为 `shape` 的每一维造一个 `IterVar`（命名 `i0, i1, …`，范围 `[0, size)`）。
2. 把这些变量喂给用户传入的 `forward_fn`，得到一列 `PrimExpr`（正向索引）。
3. 经 FFI 在 C++ 端建出 `LayoutNode`。

求值时，`map_forward_index(indices)` 内部用 TVM 的 `IndexMap` 把「输入变量 → 正向索引」的映射代入具体的 `indices`，得到物理坐标。

#### 4.1.3 源码精读

`Layout.__init__` 逐维造 `IterVar` 并调用 `forward_fn`，最后走 FFI 构造（[tilelang/layout/layout.py:L13-L43](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/layout/layout.py#L13-L43)）——这一段说明「布局 = 形状 + 一个 lambda」。

求值接口 `map_forward_index` 是绘图与推理共用的钥匙：它把 `forward_vars` 与 `forward_index` 拼成一个 `IndexMap`，再 `map_indices` 把具体下标投到物理坐标（[tilelang/layout/layout.py:L97-L125](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/layout/layout.py#L97-L125)）。`Layout.__call__` 就是它的语法糖（[tilelang/layout/layout.py:L254-L255](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/layout/layout.py#L254-L255)），所以 `layout(i, j)` 等价于查询映射。

`Fragment.map_forward_thread` 与之同构，只是它把 `[forward_thread]` 当作输出、查询的是「线程维」（[tilelang/layout/fragment.py:L166-L186](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/layout/fragment.py#L166-L186)）。`Fragment` 还支持 `repeat`（在输入维或线程维上重复）与 `replicate`（新增一个 replicate 维，让多组线程各跑一份），这两个组合算子是「由一个小 atom 布局拼出 warp/block 级布局」的标准手法（[tilelang/layout/fragment.py:L117-L151](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/layout/fragment.py#L117-L151)）。

`Layout` 与 `Fragment` 通过 `tilelang/language/common.py:19` 被挂到 `tilelang.language`，所以 `T.Layout` / `T.Fragment` 直接可用。

> 说明：`print_fragment_format` 里用到的 `layout.forward_thread`、`layout.forward_index`、`layout.replicate_size` 并不是 Python 端定义的属性，而是 C++ 端用 `def_ro` 反射出来的只读字段（见 [src/layout/layout.cc:L1223-L1226](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/layout/layout.cc#L1223-L1226)），Python 侧经 `@tvm_ffi.register_object` 自动可见。

#### 4.1.4 代码实践

**目标**：手工构造一个 4×4 转置布局，并查询它的正向映射。

**步骤**：

```python
import tilelang.language as T

# 逻辑 (i, j) -> 物理位置 (j, i)：典型转置
transpose = T.Layout([4, 4], lambda i, j: (j, i))

# 查询：逻辑下标 (1, 2) 映射到哪个物理坐标？
print("forward_vars :", transpose.get_forward_vars())
print("input shape  :", transpose.get_input_shape())
print("output shape :", transpose.get_output_shape())
print("(1,2) ->", transpose.map_forward_index([1, 2]))  # 期望 [2, 1]
```

**观察现象**：`get_output_shape()` 返回 `[4, 4]`；`map_forward_index([1, 2])` 返回 `[2, 1]`，验证了 `(i,j)->(j,i)` 这一转置关系。

**预期结果**：输出形如 `forward_vars : [i0:[0,4), i1:[0,4)]`、`(1,2) -> [2, 1]`。若 `get_output_shape()` 报错或返回异常，说明 `forward_fn` 的输出维与输入维数量不一致（输出维由返回元组的长度决定）。

#### 4.1.5 小练习与答案

**练习 1**：构造一个 `[2, 4, 8]` 的三维布局，把 `(i, j, k)` 映射到 `(k, i*4+j)`，它的输出形状是什么？

**参考答案**：`forward_fn` 返回 2 个分量，故输出是二维；`k∈[0,8)`、`i*4+j∈[0, 2*4)=[0,8)`，所以 `get_output_shape()` 应为 `[8, 8]`。这与 `examples/plot_layout/layout_transform.py` 里的 `reshape_layout` 完全一致。

**练习 2**：`Layout.inverse()` 在什么场景下有用？

**参考答案**：当你手里只有「物理位置」却要反查「原始逻辑下标」时——例如给定一个 fragment 的寄存器槽位，反推它对应 shared memory 中的哪个元素（即 MMA `ldmatrix` 的逆映射）。

---

### 4.2 手动绘制任意布局：plot_layout

#### 4.2.1 概念说明

`plot_layout` 是「你自己手里有一个 Layout/Fragment，想看它长什么样」时用的工具。它是一个**按类型分派**的薄入口：

- 传入 `T.Fragment` → 画「线程-寄存器」视图，每个格子涂线程色，标注 `T<thread>` 与 `L<local>`。
- 传入 `T.Layout` → 画「位置映射」视图，每个格子标注它映射到的（展平后的）物理坐标，支持 `view="input"` 与 `view="output"` 两种视角。

它不依赖编译器，也不需要 GPU——纯 Python + matplotlib，适合在设计映射时反复迭代。

#### 4.2.2 核心流程

`plot_layout` 的统一流程是「**枚举输入空间 → 查询映射 → 画格子**」：

1. 从对象取出 `input_shape`（可能带 `replicate` 维）。
2. 用 `itertools.product` 枚举所有逻辑下标。
3. 对每个下标，调 `map_forward_index`（Layout/Fragment 都有）得到物理坐标；若是 Fragment，再调 `map_forward_thread` 得到线程号。
4. 用 matplotlib 画矩形并填色、写字，最后按 `formats` 保存为 pdf/png/svg。

对 `T.Layout`，它额外用 `view` 参数决定「网格按谁排列」：`view="input"` 时网格是输入空间、格子写输出坐标；`view="output"` 时网格是输出空间、格子写输入坐标。

#### 4.2.3 源码精读

入口分派就在 `plot_layout` 函数体里：用 `isinstance(layout, Fragment)` 与 `isinstance(layout, T.Layout)` 二选一，否则抛 `TypeError`（[tilelang/tools/plot_layout.py:L47-L61](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/plot_layout.py#L47-L61)）。注意它**先判 Fragment**——因为 `Fragment` 继承自 `Layout`，顺序反了会误走 Layout 分支。

**Fragment 视图** `_plot_fragment_layout`：先取出 `input_shape`、`replicate_size`、线程数 `num_threads`（[tilelang/tools/plot_layout.py:L125-L130](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/plot_layout.py#L125-L130)），再两遍枚举——第一遍填 `thread_map`，第二遍填 `value_map`（[tilelang/tools/plot_layout.py:L143-L163](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/plot_layout.py#L143-L163)）；随后用前 `warp_size=32` 个线程上 hsv 色板，让一个 warp 内的线程颜色易区分，并在线程数不足 32 时发出告警（[tilelang/tools/plot_layout.py:L173-L183](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/plot_layout.py#L173-L183)）；最后每格写 `T<线程>` 与 `L<寄存器>`（[tilelang/tools/plot_layout.py:L198-L225](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/plot_layout.py#L198-L225)）。同一线程的多个格子用 `T0/1/2` 这种斜杠列表表示「这几格属于同一线程的不同寄存器」。

**Layout 视图** `_plot_layout_map`：先收集所有「输入下标 → 输出坐标」映射，并由实际映射到的坐标**反推 output_shape**（[tilelang/tools/plot_layout.py:L348-L361](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/plot_layout.py#L348-L361)）。`view="output"` 分支按输出空间画网格、格子标原始输入坐标（[tilelang/tools/plot_layout.py:L365-L480](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/plot_layout.py#L365-L480)）；`view="input"` 分支按输入空间画网格、格子标展平后的输出坐标（[tilelang/tools/plot_layout.py:L482-L606](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/plot_layout.py#L482-L606)）。高维布局会被「保留最后一维为列、前面所有维合并为行」的 `_flatten_to_2d` 拍扁成二维。

格式解析与保存由 `_parse_formats` / `_save_plot` 负责，支持 `"pdf" / "png" / "svg" / "all" / "png,svg"` 等写法（[tilelang/tools/plot_layout.py:L64-L104](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/plot_layout.py#L64-L104)）。

> 提示：PNG/PDF/SVG 输出依赖 matplotlib，需安装可视化可选依赖 `pip install "tilelang[vis]"`（见 [docs/tools/layout_visualization.md](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/tools/layout_visualization.md)）。

#### 4.2.4 代码实践

**目标**：画出 GEMM 中 shared memory 上常见的 swizzle 布局，直观看到 XOR 重排如何打散列。

**步骤**（直接来自 `examples/plot_layout/layout_swizzle.py`）：

```python
from tilelang.layout import make_quarter_bank_swizzled_layout, make_full_bank_swizzled_layout
from tilelang.tools import plot_layout

element_size = 16  # float16 = 16 bits

# Quarter-bank (32B) swizzle：8x16，仅做 1-bit XOR
layout = make_quarter_bank_swizzled_layout(8, 16, element_size)
plot_layout(layout, name="swizzle_quarter_8x16", formats="png")

# Full-bank (128B) swizzle：8x64，3-bit XOR
layout = make_full_bank_swizzled_layout(8, 64, element_size)
plot_layout(layout, name="swizzle_full_8x64", formats="png")
```

**观察现象**：`./tmp/swizzle_quarter_8x16.png` 与 `./tmp/swizzle_full_8x64.png` 会被生成。在 input 视图下，相邻行的「输出坐标」会按 XOR 模式跳变——quarter-bank 只在行 4-7 之间做半个 8 元素 swap，full-bank 则有更密集的 3-bit 重排。

**预期结果**：两张图成功落盘，标题标注 `[8x16] -> [8x16]`（swizzle 不改变形状、只重排位置）。若报 `ModuleNotFoundError: No module named 'matplotlib'`，先装可视化依赖。

> 待本地验证：不同 element_size（如 fp32 取 32）下 XOR 位数与列分组的变化，建议自行对比。

#### 4.2.5 小练习与答案

**练习 1**：把同一个 `T.Layout([4,4], lambda i,j: (j,i))` 分别用 `view="input"` 和 `view="output"` 画出来，描述两张图的区别。

**参考答案**：`view="input"` 时，网格按 `(i,j)` 排列（行 0 是 `i=0`），每格写物理坐标展平值，转置会让「列方向上的数字递增」；`view="output"` 时，网格按物理 `(j,i)` 排列，每格写原始 `(i,j)`——两张图互为「行列对调」的视图。

**练习 2**：为什么 `plot_layout` 在 Fragment 上只接受二维输入形状？

**参考答案**：`_plot_fragment_layout` 把 `input_shape` 直接当成 `(nrows, ncols)` 来 `nrows, ncols = input_shape`（[tilelang/tools/plot_layout.py:L190](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/plot_layout.py#L190)），因此非二维会解包失败；更高维 fragment 需先 `reshape`/手动拍扁。官方文档也把「二维 + 单值映射」列为限制。

---

### 4.3 编译期自动可视化：LayoutVisual Pass

#### 4.3.1 概念说明

4.2 讲的是「你自己有布局对象」。但很多时候你关心的是：**编译器在 `LayoutInference` 之后，到底给我的 fragment 缓冲选了什么布局？** 这才是排查「为什么我的 MMA 指令布局对不上」时最需要的视图。

`LayoutVisual` 就是这个编译期 Pass：它不修改 IR，只读 `LayoutInference` 写在每个 SBlock 上的 `layout_map` 注解，把里面每个 `T.Fragment` 的形状、线程表达式、寄存器表达式打印出来，并可选地调用 `plot_layout` 存图。它解决的是「让编译器的内部决策可见」。

#### 4.3.2 核心流程

完整链路是「**配置 → 接入 → 遍历注解 → 打印/画图**」：

1. 用户在 `@tilelang.jit(pass_configs={...})` 里设 `TL_LAYOUT_VISUALIZATION_ENABLE=True` 与可选的 `TL_LAYOUT_VISUALIZATION_FORMATS`。
2. 配置经 `normalize_pass_configs` 进入 `PassContext.config`。
3. 各后端流水线在 `LayoutInference()` 之后、`LowerTileOp()` 之前**固定位置**调用 `LayoutVisual(mod)`。
4. `LayoutVisual` 读 `should_enable_layout_visual()` 决定是否真的执行，读 `get_layout_visual_formats()` 决定输出哪些格式。
5. 访问器扫到带 `layout_map` 注解的 SBlock，对其中每个 Fragment 先打印文本，再按格式调 `plot_layout` 存图到 `./tmp`。

#### 4.3.3 源码精读

**接入点**：CUDA 后端在 `LayoutInference` 与 `LowerTileOp` 之间插入 `LayoutVisual(mod)`（[tilelang/cuda/pipeline.py:L113-L117](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/pipeline.py#L113-L117)）。HIP/Metal/CPU/WebGPU 后端的 pipeline 同样在这个位置调用（如 [tilelang/rocm/pipeline.py:L38-L40](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/rocm/pipeline.py#L38-L40)、[tilelang/cpu/pipeline.py:L34-L36](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cpu/pipeline.py#L34-L36)）。位置很关键：必须在 `LayoutInference` 写完 `layout_map` 之后、`LowerTileOp` 把 fragment 展开成底层 intrinsic 之前——否则要么没注解可读，要么 fragment 已被 lower 掉。

**胶水层**：`should_enable_layout_visual` 读 `TL_LAYOUT_VISUALIZATION_ENABLE`（[tilelang/backend/pass_pipeline/pipeline_utils.py:L37-L40](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/pass_pipeline/pipeline_utils.py#L37-L40)）；`get_layout_visual_formats` 读 `TL_LAYOUT_VISUALIZATION_FORMATS`，空串兜底为 `["txt"]`，并对非法格式报 `ValueError`（[tilelang/backend/pass_pipeline/pipeline_utils.py:L55-L80](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/pass_pipeline/pipeline_utils.py#L55-L80)）；`LayoutVisual(mod)` 是把这两者与真正的 Pass 缝合的薄包装（[tilelang/backend/pass_pipeline/pipeline_utils.py:L83-L87](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/pass_pipeline/pipeline_utils.py#L83-L87)）。

**访问器**：`_LayoutVisualVisitor.visit_sblock_` 检查 SBlock 的 `annotations` 是否含 `"layout_map"`，若有则逐项打印 `Shape / Thread / Index / Replicate`，并对每个声明的图像格式调 `plot_layout`（[tilelang/analysis/layout_visual.py:L84-L100](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/analysis/layout_visual.py#L84-L100)）；非二维输入形状会跳过画图只给告警。文本格式化逻辑在 `print_fragment_format`（[tilelang/analysis/layout_visual.py:L14-L39](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/analysis/layout_visual.py#L14-L39)）。`LayoutVisual` 本身被包成一个 `prim_func_pass`（[tilelang/analysis/layout_visual.py:L103-L108](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/analysis/layout_visual.py#L103-L108)）。

**配置键**：两个开关都是 `PassConfigKey` 枚举成员，默认 `False`（[tilelang/transform/pass_config.py:L194-L201](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/transform/pass_config.py#L194-L201)）。注意 `LayoutVisual` 是「只看不改」的 Pass，所以 `pass_visualizer` 在复现流水线时特意把它跳过（[tilelang/tools/pass_visualizer/core.py:L132](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/pass_visualizer/core.py#L132) 注释）。

#### 4.3.4 代码实践

**目标**：编译一个 GEMM，让编译器打印 `C_local` 这个 fragment 的推理布局并保存 svg。

**步骤**（改自 `examples/visual_layout_inference/visual_layout_inference.py`）：

```python
import tilelang
import tilelang.language as T

@tilelang.jit(
    pass_configs={
        tilelang.PassConfigKey.TL_LAYOUT_VISUALIZATION_ENABLE: True,
        tilelang.PassConfigKey.TL_LAYOUT_VISUALIZATION_FORMATS: "svg",
    }
)
def matmul(A, B, block_M, block_N, block_K, dtype=T.float16, accum_dtype=T.float32):
    M, N, K = T.const("M, N, K")
    A: T.Tensor((M, K), dtype)
    B: T.Tensor((K, N), dtype)
    C = T.empty((M, N), dtype)

    with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=128) as (bx, by):
        A_shared = T.alloc_shared((block_M, block_K), dtype)
        B_shared = T.alloc_shared((block_K, block_N), dtype)
        C_local = T.alloc_fragment((block_M, block_N), accum_dtype)
        T.clear(C_local)
        for k in T.Pipelined(T.ceildiv(K, block_K), num_stages=3):
            T.copy(A[by * block_M, k * block_K], A_shared)
            T.copy(B[k * block_K, bx * block_N], B_shared)
            T.gemm(A_shared, B_shared, C_local)
        T.copy(C_local, C[by * block_M, bx * block_N])
    return C

# 触发编译（任意合法 shape）
import torch
a = torch.randn(128, 128).cuda().half()
b = torch.randn(128, 128).cuda().half()
c = matmul(a, b, 32, 32, 32)
```

**观察现象**：编译期控制台会打印类似下面的片段（来自示例注释）：

```text
C_local inferred layout:
  Shape: [32, 32] -> [8]
  Thread: _j // 16 * 64 + _i // 16 * 32 + _i % 8 * 4 + _j % 8 // 2
  Index:  [_j % 16 // 8 * 4 + _i % 16 // 8 * 2 + _j % 2]
  Replicate:  1
```

同时 `./tmp/C_local_layout.svg` 会被生成——这就是编译器为 `C_local` 选定的 MMA 累加 fragment 布局。

**预期结果**：控制台打印上述 `Thread` / `Index` 表达式，且 `./tmp` 下出现 `C_local_layout.svg`。`Shape: [32,32] -> [8]` 说明 32×32 的逻辑块被压成每个线程持 8 个寄存器值。

> 待本地验证：把 `block_M, block_N` 换成 64×64 或改 `dtype`，观察 `Thread` / `Index` 表达式如何随 MMA 指令的微块尺寸变化。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `LayoutVisual` 必须放在 `LayoutInference` 之后、`LowerTileOp` 之前？

**参考答案**：`layout_map` 注解是 `LayoutInference` 写上去的（之前没有）；而 `LowerTileOp` 会把 fragment 展开成底层 intrinsic、fragment 对象本身不再以原貌留在 IR 里（之后画不了）。只有这个窗口期能同时看到「已推理、未展开」的 fragment。

**练习 2**：`TL_LAYOUT_VISUALIZATION_FORMATS` 设为空串或不设时会发生什么？

**参考答案**：`get_layout_visual_formats` 兜底返回 `["txt"]`，访问器仍会打印文本映射（因为「只要启用就一定打印文本」），但不会生成任何图像文件（`txt` 不被当作图像格式，会被 `_LayoutVisualVisitor` 过滤掉）。

---

### 4.4 Analyzer：roofline 性能估算与 Z3 符号推理

> **先澄清一个易混点**：项目里有两个名字都叫「Analyzer」，但它们是不同的东西。
> - `tilelang.tools.Analyzer`（[tilelang/tools/Analyzer.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/Analyzer.py)）是一个 **Python 静态 roofline 性能估算器**，统计 FLOPs 与访存字节数，给出时间估计——它**不调用 Z3**。
> - TVM 的 `arith::Analyzer`（C++）才是**符号分析器**，被 [src/layout/layout.cc](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/layout/layout.cc) 大量用于布局推理，由 `USE_Z3 ON` 编译选项接入 Z3 SMT 求解器做边界/整除证明。
>
> 本节把两者都讲清楚：前者教你怎么估算 kernel 的性能上下限，后者让你理解编译器内部「证明索引在界内」的机制。

#### 4.4.1 概念说明

**性能 Analyzer（roofline）**：在编译或基准之前，你常常想问「这个 kernel 最多能多快？」。屋顶线模型给出的答案是「理想情况下，时间不低于算力屋顶与带宽屋顶的较大者」。`tilelang.tools.Analyzer` 把这件事自动化：它读一份 TIR `PrimFunc`，数出其中 `tl.gemm` 贡献的 FLOPs 与 `tl.copy` 贡献的 global 字节数，乘上循环与 grid 维度得到总量，再套屋顶线公式。它产出的是**静态估算**，不是 GPU 实测——用于「在调参/调优前快速判断算子的算力或访存瓶颈」。

**符号 Analyzer（arith + Z3）**：布局推理时，编译器要回答诸如「`forward_index_[i]` 的取值范围是多少（决定 output_shape）」「这个映射是否双射（决定能否求逆）」「这个表达式能否被整除化简」等问题。TVM 的 `arith::Analyzer` 提供了三类原子能力：`Simplify`（化简）、`const_int_bound`（在约束下求表达式的整数上下界）、`int_set`（求区间集）、以及 `DetectIterMap`（判定迭代映射是否单射/双射）。当基于重写的化简器搞不定硬约束时，Z3 SMT 求解器作为兜底给出证明。tilelang 在 `CMakeLists.txt` 里 `set(USE_Z3 ON)`（[CMakeLists.txt:L455-L460](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/CMakeLists.txt#L455-L460)），把这套能力编进原生库。

#### 4.4.2 核心流程

**性能 Analyzer 的流程**是「**遍历 TIR → 识别 op → 累加工作量 → 套屋顶线**」：

1. 把传入的 `PrimFunc` 包成 `IRModule`，并从 `buffer_map` 记下哪些是 global 缓冲。
2. 用 `ir_transform` 做 pre/post 双向遍历：进入 `For` 时把循环长度压栈、退出时弹栈；遇到 `thread_extent`/`thread_binding` 记录 `blockIdx.x/y`。
3. 遇到 `tl.copy` / `tl.tileop.copy` 调 `_analyze_copy`：若某一侧是 global，按元素数 × dtype 字节 × 循环 × grid 累加 `total_global_bytes`。
4. 遇到 `tl.gemm` / `tl.tileop.gemm` 调 `_analyze_gemm`：按 `2*M*N*K` × 循环 × grid 累加 `total_flops`。
5. `calculate` 用设备模型的 `bandwidth` 与峰值 TFLOPS 表算出 `mem_time` 与 `compute_time`，取最大者。

**符号 Analyzer 在布局里的用法**以 `OutputShape` 为典型：先 `Bind` 每个输入变量的域，再对每个 `forward_index_[i]` 求 `int_set`——若得到有限区间，直接取 `max` 当该维大小；若 `int_set` 退化成 `(-∞, +∞)`（常见于含位运算的表达式），就回退到 `const_int_bound` 求保守上下界；再不行就用输入形状兜底。

#### 4.4.3 源码精读

**性能 Analyzer**

- 峰值 TFLOPS 表 `ARCH_CONFIGS` 只覆盖 sm80/86/89，值为 `(cores_per_sm, clock_GHz, flops_per_cycle, max_sm_count)`（[tilelang/tools/Analyzer.py:L10](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/Analyzer.py#L10)）。结果 `AnalysisResult` 是 frozen dataclass（[tilelang/tools/Analyzer.py:L15-L31](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/Analyzer.py#L15-L31)）。
- `_analyze_gemm` 取 `call.args[5..7]` 为 M/N/K，按 `2*M*N*K` 累加（[tilelang/tools/Analyzer.py:L90-L106](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/Analyzer.py#L90-L106)）；`_analyze_copy` 在源/目之一为 global 时按 region 元数 × dtype 字节累加（[tilelang/tools/Analyzer.py:L58-L88](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/Analyzer.py#L58-L88)）。
- 遍历器 `ir_pass` 的 pre/post 回调负责循环栈与 grid 维的维护（[tilelang/tools/Analyzer.py:L108-L170](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/Analyzer.py#L108-L170)）。注意它只认 `blockIdx.x/y`，不认 `blockIdx.z`（见文档限制）。
- `calculate` 套屋顶线：`estimated_time = max(mem_time, compute_time)`，无峰值表时退化为只看 `mem_time`（[tilelang/tools/Analyzer.py:L172-L213](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/Analyzer.py#L172-L213)）。设备模型 `CUDA` 提供 `compute_capability` 与 `bandwidth=[750, 12080]`（MB/s，`bandwidth[1]` 用于估算）（[tilelang/carver/arch/cuda.py:L142-L153](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/carver/arch/cuda.py#L142-L153)）。
- 一行入口 `Analyzer.analysis(fn, device)`（[tilelang/tools/Analyzer.py:L215-L225](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/Analyzer.py#L215-L225)）。注意构造 `CUDA("cuda")` 会真去查 0 号设备，所以即便不跑 kernel 也需要可见 GPU（见 [docs/tools/analyzer.md](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/tools/analyzer.md)）。

**符号 Analyzer（arith + Z3）**

- `LayoutNode::OutputShape` 是最佳示例：`UpdateAnalyzer` 先把每个输入变量的域 `Bind` 进 `arith::Analyzer`，再对每个 `forward_index_[i] + 1` 求 `int_set`；若区间退化（含位运算时常见），回退到 `const_int_bound` 求 `(max-min+1)`，最后兜底用 `input_size_`（[src/layout/layout.cc:L535-L578](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/layout/layout.cc#L535-L578)）。`+ 1` 是为了把闭区间 `[min, max]` 转成 `int_set` 的半开语义以拿到 `max`。
- `Simplify` 贯穿所有变换：例如 `SubstituteForwardIndex` 在代入具体下标后调 `analyzer->Simplify` 化简（[src/layout/layout.cc:L76-L89](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/layout/layout.cc#L76-L89)）。
- 双射性判定用 `arith::DetectIterMap(..., IterMapLevel::Bijective, &analyzer)`，决定布局能否求逆（[src/layout/layout.cc:L468-L485](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/layout/layout.cc#L468-L485)）。
- Z3 的接入在构建侧：`set(USE_Z3 ON CACHE STRING "Use Z3 SMT solver for TileLang optimizations")`（[CMakeLists.txt:L455-L460](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/CMakeLists.txt#L455-L460)）。

#### 4.4.4 代码实践

**实践 A（必做，roofline 估算）**：用 `tilelang.tools.Analyzer` 估算一个 GEMM 的 FLOPs、访存与屋顶线时间。

```python
import tilelang
import tilelang.language as T
from tilelang.carver.arch import CUDA
from tilelang.tools import Analyzer

M = N = K = 1024

@tilelang.jit
def matmul(A, B, block_M, block_N, block_K):
    A: T.Tensor((M, K), T.float16)
    B: T.Tensor((N, K), T.float16)
    C = T.empty((M, N), T.float16)
    with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=128) as (bx, by):
        A_shared = T.alloc_shared((block_M, block_K), T.float16)
        B_shared = T.alloc_shared((block_N, block_K), T.float16)
        C_local  = T.alloc_fragment((block_M, block_N), T.float32)
        T.clear(C_local)
        for k in T.serial(T.ceildiv(K, block_K)):
            T.copy(A[by * block_M, k * block_K], A_shared)
            T.copy(B[bx * block_N, k * block_K], B_shared)
            T.gemm(A_shared, B_shared, C_local, transpose_B=True)
        T.copy(C_local, C[by * block_M, bx * block_N])
    return C

tir = matmul.get_tir(block_M=128, block_N=128, block_K=32)
device = CUDA("cuda")
r = Analyzer.analysis(tir, device)

print("FLOPs        :", r.total_flops)             # 期望 2*M*N*K
print("Global bytes :", r.total_global_bytes)
print("Est. seconds :", r.estimated_time)
print("Peak TFLOPS  :", r.expected_tflops)
print("Bandwidth    :", r.expected_bandwidth_GBps, "GB/s")
```

**观察现象**：`total_flops` 应等于 \(2 \cdot M \cdot N \cdot K = 2 \times 1024^3\)；`estimated_time` 是 `max(mem_time, compute_time)`。

**预期结果**：FLOPs 输出 `2147483648`（即 \(2 \cdot 1024^3\)），与文档断言一致（见 [docs/tools/analyzer.md](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/tools/analyzer.md)）。`expected_tflops` 在 sm90 等未列入 `ARCH_CONFIGS` 的卡上会是 `None`，此时 `estimated_time` 退化为纯带宽时间。

> 待本地验证：把估算时间与 `kernel.get_profiler().do_bench()` 的实测延迟对照，算出「实际 / 理想」的达成比例——这是判断 kernel 是否已逼近屋顶线的标准动作（详见 u8-l3）。

**实践 B（选做，符号化简）**：用 TVM 的 `arith::Analyzer`（即上节 C++ 用的同一个分析器，Python 侧为 `tvm.arith.Analyzer`）对一个含约束的索引表达式做边界推理，直观感受「编译器怎么证明索引在界内」。

```python
from tilelang import tvm
from tvm import te

ana = tvm.arith.Analyzer()
n = te.var("n")
i = te.var("i")

# 化简一个常见的「对齐到 4 的倍数」表达式
print("(i // 4) * 4           =", ana.Simplify((i // 4) * 4))
print("(((i // 4) * 4) + 3)   =", ana.Simplify(((i // 4) * 4) + 3))

# 在「i 属于 [0, 64)」的约束下，求 i//4*4+3 的整数上下界（边界证明）
ana.Bind(i, tvm.ir.Range(0, 64))
bnd = ana.const_int_bound(i // 4 * 4 + 3)
print("bound of i//4*4+3 in [0,64): min=", bnd.min_value, " max=", bnd.max_value)
```

**观察现象**：`Simplify((i//4)*4)` 不会变成 `i`（因为两者只在 `i` 是 4 的倍数时相等），但 `const_int_bound` 在 `i∈[0,64)` 约束下能给出 `i//4*4+3` 的精确整数区间——这正是 `LayoutNode::OutputShape` 求 output 维大小时做的事。

**预期结果**：约束下的 bound 形如 `min=3, max=63`（即 `i∈[0,64)` 时 `i//4*4+3 ∈ [3, 63]`）。

> 待本地验证：上述精确数值请本地运行确认；不同 TVM 版本的 `Simplify` 输出形式可能略有差异。若 `tvm.arith.Analyzer` 在你的环境不可用，可改为阅读 [src/layout/layout.cc:L549-L578](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/layout/layout.cc#L549-L578) 做源码阅读型实践：画出「`int_set` 退化 → 回退 `const_int_bound` → 再回退 `input_size_`」的三级回退流程图。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `tilelang.tools.Analyzer` 把 GEMM 的 FLOPs 算成 `2*M*N*K`？

**参考答案**：一次矩阵乘 `C[M,N] = A[M,K]·B[K,N]` 每个输出元素需要 K 次乘加，即 2K 次浮点运算；共 M·N 个输出元素，故 \(2 \cdot M \cdot N \cdot K\)。这是业界统计 GEMM 算力的统一约定（见 `_analyze_gemm` 的 `flops_per_call = 2 * M * N * K`）。

**练习 2**：在 `OutputShape` 里，为什么对 `forward_index_[i] + 1` 而不是 `forward_index_[i]` 求 `int_set`？

**参考答案**：`int_set` 返回的是半开区间 `[min, max)`，而坐标是闭区间 `[min, max]`。加 1 后 `int_set(expr+1).max()` 正好给出闭区间的上界，于是该维大小就是 `max`（见 [src/layout/layout.cc:L554](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/layout/layout.cc#L554)）。

**练习 3**：`expected_tflops` 在什么情况下是 `None`？这对 `estimated_time` 有什么影响？

**参考答案**：当设备的 `compute_capability[:2]` 不在 `ARCH_CONFIGS`（仅 sm80/86/89）里时，`get_peak_tflops` 返回 `None`；此时 `calculate` 算不出 `compute_time`，`estimated_time` 退化为纯访存时间 `mem_time`（见 [tilelang/tools/Analyzer.py:L188-L204](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/Analyzer.py#L188-L204)）。

## 5. 综合实践

围绕一个 GEMM，把本讲三件事串起来，给 kernel 做一次「**看见布局 → 估算上限 → 实测对照**」的完整体检：

1. **开 LayoutVisual**：按 4.3.4 给 GEMM 加 `TL_LAYOUT_VISUALIZATION_ENABLE=True, TL_LAYOUT_VISUALIZATION_FORMATS="svg"`，编译后在 `./tmp/C_local_layout.svg` 里观察编译器为累加 fragment 选的线程/寄存器布局；再用 4.2.4 的 `make_full_bank_swizzled_layout` 手动画出 `A_shared` 的 swizzle 布局做对照。
2. **静态估算**：用 4.4.4 实践 A 的 `Analyzer.analysis(tir, CUDA("cuda"))` 得到 `total_flops`、`total_global_bytes`、`estimated_time`，并算出理论算力强度 `FLOPs / Bytes`（即该算子在屋顶线图上的「斜率」位置）。
3. **实测对照**：用 u8-l3 的 `kernel.get_profiler().do_bench()` 测出真实延迟，计算 `实测TFLOPS = total_flops / latency_s / 1e12`，再除以 `expected_tflops` 得到「屋顶线达成比例」。
4. **下结论**：若达成比例高（>60%），说明 kernel 已逼近算力屋顶，继续优化的收益有限；若比例低，结合 LayoutVisual 的图判断是否是布局/bank-conflict/sync 问题。

> 若本地无 GPU，第 1、2 步仍可做（LayoutVisual 只需编译、Analyzer 需构造 `CUDA` 设备模型但会真查设备），第 3 步标为「待本地验证」。

## 6. 本讲小结

- `Layout` 只描述「逻辑下标 → 物理位置」，`Fragment` 额外带「线程 / 寄存器」映射；两者都靠 `map_forward_index` / `map_forward_thread` 这类**可求值接口**同时支撑绘图、自动可视化与编译器内部推理。
- `plot_layout` 是按类型分派的纯 Python 绘图工具：Fragment 画 `T<线程>/L<寄存器>` 视图，Layout 画 `input`/`output` 两种位置映射视图；只依赖 matplotlib，无需 GPU。
- **LayoutVisual** 是编译期「只看不改」的 Pass，固定挂在各后端 `LayoutInference → LowerTileOp` 之间，读 `layout_map` 注解打印并保存推理出的 fragment 布局，由 `TL_LAYOUT_VISUALIZATION_ENABLE/FORMATS` 两个 `PassConfigKey` 控制。
- 项目有**两个同名 Analyzer**：`tilelang.tools.Analyzer` 是 roofline 性能估算器（数 FLOPs/字节、套屋顶线、不碰 Z3）；TVM `arith::Analyzer`（C++）才是符号分析器，被 `src/layout/layout.cc` 用来求输出形状（`int_set`→`const_int_bound` 三级回退）、化简与判定双射性，由 `USE_Z3 ON` 接入 Z3 SMT 求解器兜底。
- 屋顶线公式 \(\max(\text{FLOPs}/\text{PeakFLOPS},\ \text{Bytes}/\text{PeakBW})\) 给出 kernel 时间的理想下界，配合 profiler 实测可算出「达成比例」，是判断优化空间的标准标尺。

## 7. 下一步学习建议

- **横向**：本讲的 LayoutVisual 属于「编译期可视化」家族，与 **u9-l1** 的 lower trace / pass_visualizer / `T.print` 互补——前者看布局，后者看 IR 文本与结构树。建议把它们组合起来调试同一个 GEMM。
- **纵深布局**：若想理解 `LayoutInference` 是**如何**推出这些 fragment 布局的，回到 **u6-l2** 读 LayoutInference Pass 的三级约束传播（Strict/Common/Free）与 `InferLayout` 钩子。
- **纵深符号分析**：对 `arith::Analyzer` 感兴趣的读者，可继续读 TVM 的 `arith` 模块（`const_int_bound`、`int_set`、`modular_set`、`DetectIterMap`），以及 [src/layout/utils.h](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/layout/utils.h) 里 `check_forward_index` 这类用符号分析做布局等价校验的工具。
- **实战**：把综合实践搬到 FlashAttention / MLA（见 u8-l3）上，观察 attention 算子里 `Q/K/V` 多个 fragment 的布局差异，并用 Analyzer 估算其算力强度。
