# 布局标注与 L2 Swizzle

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 Ascend 上「存储布局（layout）」到底是什么、为什么 L1 里的矩阵不是简单行优先，而是 **fractal（分形）/ zN** 排布。
- 用 `T.annotate_layout({buf: layout})` 显式给片上 buffer 标注布局，并理解 `make_zn_layout` / `make_nz_layout` 两个工厂函数各自生成什么样的仿射变换。
- 区分两个名字相近但**用途完全不同**的 `T.use_swizzle`：一个是核间任务重排（提升 L2 cache 局部性），一个是 GPU 共享内存的 bank-conflict swizzle；并看懂 Ascend 版 `use_swizzle` 如何落到 `tl::ascend::thread_block_swizzle` 模板。
- 理解 `LayoutInference` pass 如何把「标注在某一个 buffer 上的布局」沿着算子调用链自动传播，最终被 codegen 读出来打印成 `layout::zN`。
- 建立 `src/layout` 目录下 `Layout` / `Fragment` / `SwizzledLayout` 三种 IR 与 GPU / Ascend 两条路线的分工地图。

## 2. 前置知识

本讲承接 u4-l1（Expert 内存分配与 Cube/Vector Scope），只做最简回顾：

- **片上存储层级**（u1-l1、u3-l1）：GM → L1（属 Cube）→ L0A/L0B/L0C（Cube 寄存器级）→ UB（属 Vector）。Expert 模式用 `T.alloc_L1` / `T.alloc_L0A` / `T.alloc_L0B` / `T.alloc_L0C` 把 scope 钉死。
- **Cube 矩阵乘指令**（u3-l3）：`Mmad` 不读「行优先」矩阵，它要求数据按 **fractal** 排布；`T.gemm_v0` 内部已包好搬运，`T.mma` 只发一条 `Mmad`，搬运要自己写。
- **TIR block 与属性**（u2-l1、u4-l1）：tile-lang 的 `with T.Scope("C"):` 会给 block 贴属性；`T.annotate_layout` 同样是给 block 贴一个 `layout_map` 属性，本讲就来拆这个属性。

一个关键区分：**scope** 回答「数据放在哪块硬件」，**layout** 回答「这块硬件里数据按什么形状摆放」。两者正交——同一个 L1 buffer，scope 是 `shared.l1`，layout 可以是 `zN` 也可以是 `nZ`。本讲专攻后者。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tilelang/intrinsics/ascend_layout.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/intrinsics/ascend_layout.py) | Ascend 布局的核心实现：`AscendLayout` 枚举、`make_zn_layout` / `make_nz_layout` / `make_col_major_layout` 三个工厂函数。 |
| [tilelang/language/\_\_init\_\_.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/__init__.py) | 前端入口：`T.annotate_layout`（L101）、GPU 版 `use_swizzle`（L93）、Ascend 版 `use_swizzle`（L221，靠 `del` 覆盖前者）、`npu_use_swizzle`（L202）。 |
| [tilelang/layout/layout.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/layout/layout.py) | `Layout` Python 类，`__init__` 接收 `shape / forward_fn / layout_tag`，经 FFI 构造 C++ `tl.Layout` 对象。 |
| [src/layout/layout.h](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/layout/layout.h) | C++ 侧 `AscendLayout` 枚举与 `ascendLayoutMap`（zN/nZ/zZ/Row/Col 字符串）、`LayoutNode`。 |
| [src/layout/gemm_layouts.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/layout/gemm_layouts.cc) | **GPU 路线**的 swizzle 布局（bank-conflict）：`makeHalfBankSwizzleLayout` / `makeFullBankSwizzleLayout` / `makeGemmABLayout`。 |
| [src/layout/swizzle.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/layout/swizzle.cc) | **GPU 路线**的 `SwizzlePattern` / `SwizzledLayoutNode`（XOR 重排）。 |
| [src/transform/ascend_infer_buffer_scope.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_infer_buffer_scope.cc) | `AscendInferBufferScope` pass：推断 scope **并**给 L1 buffer 注入默认 zN 布局（`CreateDefaultZnLayout` 回调 Python `make_zn_layout`）。 |
| [src/transform/layout_inference.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/layout_inference.cc) | `LayoutInference` pass：读 `kLayoutMap` 标注，用 BFS 三级推理把布局沿算子链传播，最后把 `layout_map` 写回每个 block。 |
| [src/transform/frontend_legalize.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/frontend_legalize.cc) | `SwizzleFinder`：扫描 `thread_block_swizzle` 调用、设置函数级 `use_swizzle` 属性的辅助逻辑。 |
| [src/op/ascend.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/op/ascend.cc) | codegen 拼接搬运模板时读 `layout_map[buf]->AscendLayoutStr()` 打印 `layout::zN`（L326）；注册 `ascend_use_swizzle` builtin（L1368）。 |
| [src/target/codegen_ascend.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc) | `ascend_use_swizzle` intrinsic 分发（L686）与 `UseSwizzleCodegen`（L2652），把 intrinsic 翻成 `tl::ascend::thread_block_swizzle<...>(pid)`。 |
| [src/tl_templates/ascend/common.h](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h) | `thread_block_swizzle` 设备模板（L196），用 `GemmIdentityBlockSwizzle` 重排 block 坐标。 |
| [examples/gemm/example_gemm_intrinsic.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic.py) | 高性能 GEMM，同时用 `T.use_swizzle`（L62）与 L1 buffer，是本讲实践的改写起点。 |
| [tilelang/engine/phase.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py) | 两阶段 pass 顺序：`AscendInferBufferScope`（L52，先注入默认 zN）→ `LayoutInference`（L67，再传播）。 |

---

## 4. 核心概念与源码讲解

### 4.1 存储布局入门：为什么 Ascend 的数据不是行优先

#### 4.1.1 概念说明

在 CPU/GPU 上，一个 `[M, N]` 矩阵默认按**行优先（row-major）**连续存放：第 0 行整行、再第 1 行整行……地址 `addr = i * N + j`。但 Ascend 的 Cube 核执行 `Mmad`（矩阵乘累加）时，**硬件并不直接吃行优先数据**。它的矩阵计算单元以一个固定的「分形（fractal）」为单位吞吐数据：每次搬一小块 `16 行 × 若干列` 进 L0A/L0B，再算一个 `16×16×16` 的矩阵乘。

为了让 GM→L1→L0 的搬运（`DataCopy`）能直接喂给这个硬件单元，Ascend 规定了一种专门的片上布局，tile-lang 里叫 **zN**。在 zN 下，矩阵不是逐行铺，而是先切成「16 行一摞」的 fractal，每个 fractal 内部再按 C0 单元（32 字节为一组）排布。源码里把这两个粒度写成常量：

- `BYTE_PER_C0 = 32`：一个 C0 单元 = 32 字节（Cube 的最小寻址粒度）。
- `C0_NUM_PER_FRACTAL = 16`：一个 fractal 由 16 个 C0 组成。
- `BYTE_PER_FRACTAL = 512`：一个 fractal 共 512 字节。

对 `float16`（16 bit），`ELE_NUM_PER_C0 = 32*8/16 = 16`，即一个 C0 装 16 个元素；一个 fractal 就是 `16 行 × 16 列`。`make_zn_layout` 的注释里也直接贴了对应的 Catlass `Layout` 表达式，可作为对照。

#### 4.1.2 核心流程

zN 布局把逻辑坐标 `(i, j)` 映射成线性偏移 `index`，其核心是把 `i` 拆成「在 fractal 内的第几行（`i % 16`）」与「第几个 fractal（`i // 16`）」、把 `j` 拆成「在 C0 内的第几个元素（`j % ELE_NUM_PER_C0`）」与「第几个 C0（`j // ELE_NUM_PER_C0`）」：

\[
\text{index} = \underbrace{(i//16)\cdot S_r}_{\text{跨 fractal 的行步长}} + \underbrace{(j//c)\cdot S_c}_{\text{跨 C0 的列步长}} + \underbrace{(i\%16)\cdot c}_{\text{fractal 内行}} + \underbrace{(j\%c)}_{\text{C0 内列}}
\]

其中 \(c = \text{ELE\_NUM\_PER\_C0}\)（fp16 时为 16），\(S_r = \text{ELE\_NUM\_PER\_FRACTAL}\)（fp16 时 256），\(S_c = \text{round\_up}(M,16)\cdot c\)。直观地说：**先按 16 行切块，块内一行挨着一行；块与块之间按「行优先」排**。这正是 `zN` 这个名字的来源——「Z」表示 fractal 分块，主轴沿 M（行）方向。

与之相对的是 **nZ**：fractal 的 16 单位放在 N（列）方向，适合转置类数据。还有非分形的 `RowMajor` / `ColMajor`，用于 GM 这种「平铺」存储。

> 为什么默认是 zN 而不是行优先？因为 GM→L1 的 `DataCopy` 与 L1→L0A/L0B 的搬运都按 fractal 吞吐，L1 里存成 zN 后，`Mmad` 几乎零开销就能取到对齐的数据块；存成行优先反而要在搬运时做昂贵的重排。

#### 4.1.3 源码精读

布局常量与 `AscendLayout` 枚举定义在 Python 侧：

[ascend_layout.py:18-20](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/intrinsics/ascend_layout.py#L18-L20) 定义 C0 / fractal 的字节粒度；[ascend_layout.py:10-15](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/intrinsics/ascend_layout.py#L10-L15) 把五种布局编成枚举（`kRowMajor=0, kColMajor=1, kzN=2, kzZ=3, knZ=4`）。C++ 侧用同一组枚举值做反向字符串映射：

[layout.h:22-35](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/layout/layout.h#L22-L35) —— `ascendLayoutMap` 把枚举翻成 codegen 要打印的字符串 `layout::zN` / `layout::nZ` 等。这条映射是「Python 算布局 → C++ 打字符串」的桥梁。

那么 L1 buffer 的 zN 是谁给的？答案是 `AscendInferBufferScope` pass 在推断 scope 的同时**顺手注入默认 zN**：

[ascend_infer_buffer_scope.cc:786-793](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_infer_buffer_scope.cc#L786-L793) —— 对每个 L1 buffer，只要用户没显式标注布局（`layout_map.count(buf->data) == 0`），就给它塞一个默认 zN。注意注释明确「不要覆盖用户自定义的 nZ」。而默认 zN 的构造是通过 FFI **回调 Python 的 `make_zn_layout`**：

[ascend_infer_buffer_scope.cc:805-818](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_infer_buffer_scope.cc#L805-L818) —— C++ 不硬编码复杂的仿射 AST，而是调 `tl.ascend.make_zn_layout` 这个 Python 全局函数。这是一种「复杂表达式留在 Python 端便于调试、C++ 只做调度」的设计。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：确认 L1 buffer 默认拿到 zN 布局，并理解 C0/fractal 的尺寸。
2. **步骤**：打开 `ascend_layout.py`，把 `BYTE_PER_C0 / C0_NUM_PER_FRACTAL / BYTE_PER_FRACTAL` 与 4.1.1 的解释对照；再在 `ascend_infer_buffer_scope.cc` 搜 `CreateDefaultZnLayout`，确认它只对 L1、且只在「用户未标注」时触发。
3. **观察现象**：思考——如果 dtype 是 `int8`（8 bit），`ELE_NUM_PER_C0` 会变成多少？（答：\(32\times8/8=32\)，即一个 C0 装 32 个 int8，fractal 变成 `16 行 × 32 列`。）
4. **预期结果**：能口算出 fp16/int8 下「一个 fractal 多少元素」。
5. **说明**：纯阅读，无需运行；如需验证数值，可待本地在 Python 里 `import` 后打印常量。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `bool` 类型的 buffer（见 u3-l1）不适用 zN？
**答案**：zN 是面向 Cube `Mmad` 的 fractal 布局；`bool` 一般是 Vector 侧的掩码/比较结果，被 `merge smem` pass 强制钉到 UB，而 UB 走的是 Vector 向量布局，不需要 fractal。

**练习 2**：`zN` 与 `nZ` 的差别用一句话概括？
**答案**：两者都是 fractal（Z）分块；`zN` 把 fractal 的 16 单位放在 **M（行）** 方向（行优先分形），`nZ` 放在 **N（列）** 方向（列优先分形）。

---

### 4.2 T.annotate_layout 与 make_zn_layout：显式标注片上布局

#### 4.2.1 概念说明

4.1 讲了 L1 buffer 会自动得到 zN。但有时你想**显式控制**：比如某块 buffer 你想用 `nZ`，或者某个 `fragment`/GM buffer 想指定布局。这时就用 `T.annotate_layout({buf: layout})`。它做两件事：

1. 用 `make_zn_layout(buf)`（或 `make_nz_layout` / `make_col_major_layout`）构造一个 `Layout` 对象——里面既有一段仿射 `transform_func`（坐标→偏移），又带一个 `layout_tag`（标明这是哪种 Ascend 布局）。
2. 把 `{buf: layout}` 作为**块属性** `layout_map` 贴到当前 block 上，供后续 pass 与 codegen 读取。

关键点：`Layout` 对象同时携带「数学变换」与「标签」两份信息。前者给 `LayoutInference` 做坐标推理（4.4），后者给 codegen 直接打印 `layout::zN` 字符串（避免重新解析仿射式）。

#### 4.2.2 核心流程

标注一条数据通路通常分三步：

1. **分配 buffer**：`A_L1 = T.alloc_L1((S1, block_M, K_L1), dtype)`。
2. **构造布局**：`layout = make_zn_layout(A_L1)`——工厂函数读 `buf.dtype / buf.shape`，算出 `ELE_NUM_PER_C0` 等常量，返回带 `layout_tag=kzN` 的 `Layout`。
3. **贴属性**：`T.annotate_layout({A_L1: layout})`，等价于 `block_attr({"layout_map": {A_L1.data: layout}})`。

`Layout` 的构造由 Python 类完成：[layout.py:17-48](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/layout/layout.py#L17-L48) —— 为 `shape` 每一维建一个 `IterVar`，调用 `forward_fn(*vars)` 得到正向索引表达式，连同 `layout_tag` 一起经 FFI 构造 C++ `tl.Layout`。

`make_zn_layout` 本体在 [ascend_layout.py:33-77](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/intrinsics/ascend_layout.py#L33-L77)：`transform_func(*args)` 取最后两维 `(i, j)`，按 4.1.2 的公式算出新的线性偏移，最后 `return T.Layout(shape, transform_func, layout_tag=AscendLayout.kzN.value)`。`make_nz_layout`（[ascend_layout.py:89-135](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h) 风格相同，只是把 16 单位换到列侧，`layout_tag=knZ`。

#### 4.2.3 源码精读

前端入口 `T.annotate_layout`：

[\_\_init\_\_.py:101-131](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/__init__.py#L101-L131) —— 把字典里的 buffer 换成 `buffer.data`（TIR 句柄），返回 `block_attr({"layout_map": ...})`。注意它返回的是一个**块属性**，必须写在 `with T.Scope(...)` 这类 block 内部才会生效。

codegen 如何消费这个标注？在拼接 `DataCopy` 搬运模板时，`src/op/ascend.cc` 直接读 `layout_map`：

[ascend.cc:326-340](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/op/ascend.cc#L326-L340) —— 当 `config.print_gm_layout` / `print_src_layout` / `print_dst_layout` 为真时，用 `T.layout_map[buf]->AscendLayoutStr()` 把布局翻译成 `layout::zN` 字符串塞进模板参数；找不到就退化成 `layout::RowMajor`。所以你在生成代码里看到的 `DataCopy<..., layout::zN, ...>` 就来自这里。

> `layout_tag` 在 C++ 侧被还原成枚举的位置在 [layout.cc:58](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/layout/layout.cc#L58)：`static_cast<AscendLayout>(Downcast<IntImm>(ascend_layout)->value)`，再经 `ascendLayoutMap` 翻成字符串。

#### 4.2.4 代码实践（本讲主实践）

1. **实践目标**：给高性能 GEMM 的 `A_L1` / `B_L1` 显式标注 zN 布局，并在生成代码里观察到 `layout::zN`。
2. **操作步骤**：
   - 复制 `examples/gemm/example_gemm_intrinsic.py` 为 `my_gemm_layout.py`。
   - 在 `with T.Scope("C"):` 内、`init_flag()` 之后，加入：
     ```python
     from tilelang.intrinsics import make_zn_layout, make_nz_layout
     T.annotate_layout({A_L1: make_zn_layout(A_L1), B_L1: make_zn_layout(B_L1)})
     ```
   - 保留原有的 `cid = T.use_swizzle(...)`（见 4.3）。
   - 运行 `python my_gemm_layout.py`（需真实 NPU 或仿真环境）。
3. **观察现象**：脚本第 110 行 `print(func.get_kernel_source())` 会打印生成的 Ascend C 代码；在其中搜索 `DataCopy`，观察模板参数里出现 `layout::zN`。
4. **预期结果**：搬运模板形如 `copy_gm_to_l1<..., layout::zN, ...>(...)`；正确性仍输出 `Kernel Output Match!`（因为 L1 本就默认 zN，显式标注只是让它可见）。
5. **进阶**：把 `A_L1` 改成 `make_nz_layout(A_L1)`，重新生成代码，对比模板里 `layout::nZ` 与搬运参数的变化——**注意**：随意改 A 的布局很可能让 `Mmad` 取错数据导致结果错误，这一步重点看「生成代码差异」，正确性「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：既然 L1 默认就是 zN，`T.annotate_layout({A_L1: make_zn_layout(A_L1)})` 还有意义吗？
**答案**：有。它的价值在于（a）让布局在源码里显式可见、可读；（b）覆盖默认值——比如你想强制 `nZ`，或给 `fragment`/GM 这类**没有默认 zN** 的 buffer 标注；（c）给 `LayoutInference` 提供一个确定的传播起点（见 4.4）。

**练习 2**：`make_zn_layout(buf)` 返回的 `Layout` 为什么同时带 `transform_func` 和 `layout_tag` 两份信息？
**答案**：`transform_func`（坐标→偏移的仿射）给 `LayoutInference` 做数学推理与坐标改写；`layout_tag` 给 codegen 直接打印 `layout::zN`，避免在 C++ 端反向解析仿射式。一份算、一份印，各司其职。

---

### 4.3 T.use_swizzle：核间任务重排优化 L2 Cache 局部性

#### 4.3.1 概念说明

`use_swizzle` 这个名字很容易和 4.2 的「存储布局」混在一起，但它**根本不是数据布局**。它是**核间任务调度（thread-block swizzle / rasterization）**：决定「第 `cid` 号核，在这一轮去算哪一个输出 tile」。

朴素调度是 `bx = cid // n_num; by = cid % n_num`——核 0、1、2… 顺次算相邻 tile。问题在于：相邻核同时算相邻 tile 时，它们从 GM 取的 A 行/B 列会在 L2 cache 里互相挤占，产生「热点块」，cache 命中率下降。swizzle 的做法是给 `cid` 加一个**可调偏移**（`off`），让相邻核错开取数据，使同一块 A 行尽量被连续几轮复用，提升 L2 命中率。

> 一个关键陷阱：前端**有两个同名 `use_swizzle`**。第一个是 GPU 用的 `use_swizzle(panel_size, order, enable)`；文件末尾用 `del use_swizzle` 删掉它，再定义一个 Ascend 用的 `use_swizzle(cid, m, n, k, block_m, block_n, ...)`。所以在 Ascend 后端写 `T.use_swizzle(...)`，命中的是**第二个**定义。详见源码精读。

#### 4.3.2 核心流程

Ascend 版 `T.use_swizzle(cid, M, N, K, block_M, block_N, off, dir)` 的执行链：

1. **前端**：发射 `call_intrin("int32", Op.get("tl.ascend_use_swizzle"), "thread_block_swizzle<M,N,K,block_M,block_N,off,dir>", cid, total_tiles)`——把调度参数编码进一个**字符串模板名**。
2. **builtin 注册**：`src/op/ascend.cc` 用 `TIR_DEFINE_TL_BUILTIN(ascend_use_swizzle)` 把它注册成一条 TIR intrinsic。
3. **codegen**：`codegen_ascend.cc` 识别到这条 intrinsic，调 `UseSwizzleCodegen`，输出 `tl::ascend::thread_block_swizzle<M,N,K,block_M,block_N,off,dir>(pid)`。
4. **设备模板**：`common.h` 的 `thread_block_swizzle` 用 `GemmIdentityBlockSwizzle<off, dir>` 把线性 `pid` 重映射成 `(m, n)` 块坐标，再拍扁回线性 `cid`。

典型用法（来自高性能 GEMM）：在「每个核循环处理多个 tile」的结构里，每轮把 `cid` 过一遍 swizzle：

```python
for i in T.serial(T.ceildiv(m_num * n_num, core_num)):
    cid = T.use_swizzle(i * core_num + cid, M, N, K, block_M, block_N, off=3)
    if cid < m_num * n_num:
        bx = cid // n_num
        by = cid % n_num
        ...
```

`off=3` 就是「相邻核错开 3 个 tile」的偏移量，是可调的性能旋钮。

#### 4.3.3 源码精读

两个同名函数的覆盖关系是本节最容易踩坑处：

[\_\_init\_\_.py:93-98](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/__init__.py#L93-L98) 定义 **GPU 版** `use_swizzle(panel_size, order="row", enable=True)`，返回块属性 `threadblock_swizzle_pattern = tl::rasterization2DRow<panel_size>`（对应 `src/tl_templates/cuda/threadblock_swizzle.h` 与 `hip/` 下的 GPU 设备模板）。随后：

[\_\_init\_\_.py:217-223](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/__init__.py#L217-L223) —— `del use_swizzle` 删掉 GPU 版，再定义 **Ascend 版** `use_swizzle(cid, m, n, k, block_m, block_n, off=1, dir=0, in_loop=False)`，它只是 `npu_use_swizzle`（[\_\_init\_\_.py:202-214](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/__init__.py#L202-L214)）的别名。`npu_use_swizzle` 先断言 `m%block_m==0 and n%block_n==0`，再发射带模板名串的 intrinsic。

builtin 注册与 codegen：

[ascend.cc:1368](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/op/ascend.cc#L1368) 注册 `ascend_use_swizzle`；[codegen_ascend.cc:686-687](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L686-L687) 在 intrinsic 大分发里识别它并交给 `UseSwizzleCodegen`；[codegen_ascend.cc:2652-2658](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L2652-L2658) 的实现非常薄——把 `args[0]`（模板名串）拼成 `tl::ascend::thread_block_swizzle<...>`，把 `args[1]`（`pid`）作为实参打印进去。

设备端真正「重排」的逻辑在模板里：

[common.h:196-213](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L196-L213) —— `thread_block_swizzle<M,N,K,block_M,block_N,SwizzleOffset,SwizzleDirection>(pid)` 用 `GemmIdentityBlockSwizzle<offset, direction>` 把 `pid` 还原成块坐标 `(m, n)`，再 `return coord.m() * cols + coord.n()` 拍扁。`SwizzleOffset` 就是前端的 `off`。

> 辅助：[frontend_legalize.cc:36-62](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/frontend_legalize.cc#L36-L62) 的 `SwizzleFinder` 会扫描 `thread_block_swizzle` 调用并给函数贴一个 `use_swizzle=True` 属性。注意 `phase.py:61` 里 `FrontendLegalize` 当前是**注释掉**的，所以这条属性设置并未在主流程生效——但这不影响 swizzle 本身，因为 intrinsic 走的是上面那条「builtin → codegen」的常规通路。

#### 4.3.4 代码实践（源码阅读 + 参数实验型）

1. **实践目标**：理解 `off` 参数如何改变核→tile 映射，并确认生成代码里的模板调用。
2. **操作步骤**：阅读 `examples/gemm/example_gemm_intrinsic.py` 第 61-65 行的循环结构；然后对 `func.get_kernel_source()` 的输出搜索 `thread_block_swizzle`，确认它带着 `<M,N,K,block_M,block_N,3,0>` 这样的模板参数。
3. **观察现象**：把 `off=3` 改成 `off=1` 重新生成代码，模板实参相应变化；在能跑的环境下用 `do_bench` 对比两种配置的耗时（参考 u7-l4 的 msprof 方法）。
4. **预期结果**：`off` 越大，相邻核错开越远，L2 局部性改变；但过大也可能损害局部性，存在最优值。性能数字「待本地验证」。
5. **说明**：本实践不改正确性逻辑（swizzle 是一一映射，结果不变），只改调度顺序。

#### 4.3.5 小练习与答案

**练习 1**：`T.use_swizzle` 改变了算子的数学结果吗？
**答案**：不改变。它是一一映射（把每个 `pid` 重排到唯一的块坐标），所有 tile 仍被恰好算一次，只是「谁先算、相邻核算哪块」变了。它优化的是 L2 cache 局部性，不是数值。

**练习 2**：为什么前端要用 `del use_swizzle` 再重新定义，而不是给两个函数起不同名字？
**答案**：让用户在任意后端都写 `T.use_swizzle(...)` 这一个名字，由「当前 import 链里谁最后定义了它」决定走 GPU 还是 Ascend 路线。`del + 重定义` 是模块级覆盖的惯用写法；代价是签名差异容易被忽略（GPU 版第一参是 `panel_size`，Ascend 版第一参是 `cid`）。

---

### 4.4 LayoutInference pass：布局在算子间的自动传播

#### 4.4.1 概念说明

4.2 的 `T.annotate_layout` 只标了**一个** buffer 的布局。但一个 buffer 会被 `copy`、`gemm`、`reduce` 等多个算子读写，相邻算子往往需要**一致**的布局才能正确对接。靠人手给每个 buffer 都标一遍既繁琐又易错。`LayoutInference` pass 就是来「自动补全」的：给它一两个标注或默认值（如 L1 的 zN），它沿着算子调用链把布局传播到所有相关 buffer，最后把完整的 `layout_map` 写回每个 block。

这个 pass 在 `LowerAndLegalize` 阶段、`AscendLowerParallelToVector` 之后执行（[phase.py:67](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/phase.py#L67)），排在 `LowerTileOp` 之前。也就是说它工作在「还保留 tile 语义」的 IR 上，这样每个算子（如 `copy`、`gemm`）都还知道自己对布局的要求。

#### 4.4.2 核心流程

`LayoutInference` 的核心是一个**带约束的 BFS 传播**，分三级松弛：

1. **收集**：遍历函数体，把每个算子调用（`copy`/`gemm`/`reduce`/`parallel` 等）解析成一个 `Operator` 推理器，记录它读写了哪些 buffer，建立 `use_list`（buffer → 用到它的算子下标列表）。
2. **读标注**：在 `Block` 节点上读 `attr::kLayoutMap`（即 `T.annotate_layout` 贴的属性），把 `{buffer: layout}` 放进初始 `layout_map`。
3. **三级 BFS**：
   - 第 1 步 `kStrict`：每个算子按最严格约束做一次推理；
   - 第 2 步 `kCommon`：用队列 BFS，把新推出的布局喂给依赖它的算子，直到不动点；
   - 第 3 步 `kFree`：进一步放松约束，再跑一遍 BFS，尽量给每个 buffer 都配上布局。
4. **校验**：所有 `local.fragment` buffer 必须被推出布局，否则报错。
5. **回写**：把最终的 `layout_map` 写到每个 block 的 `kLayoutMap` 属性上，供 codegen 读取。

传播过程中若同一个 buffer 被推出两种不同布局，会直接 `ICHECK` 失败——这就是「布局冲突」，通常意味着你的标注与算子约束矛盾。

#### 4.4.3 源码精读

pass 注册与入口：

[layout_inference.cc:657-671](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/layout_inference.cc#L657-L671) —— `LayoutInference()` 先跑 `ParallelLoopTransformer`，再用 `BufferUseDefCollector` 收集，最后 `LayoutInferencer` 回写。注册名 `tl.LayoutInference`。

读 `T.annotate_layout` 标注的位置：

[layout_inference.cc:481-509](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/layout_inference.cc#L481-L509) —— 在 `VisitStmt_(BlockNode*)` 里，若 block 带 `kLayoutMap` 注解，就把 `{Var: Layout}` 还原成 `{Buffer: Layout}`（先按 data 句柄查，查不到再按名字+shape+dtype 模糊匹配），并入 `annotated_layout_map_`。

三级 BFS 推理：

[layout_inference.cc:341-353](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/layout_inference.cc#L341-L353) —— 依次 `kStrict`（不回填队列）、`kCommon`（BFS 到不动点）、`kFree`（放松后再 BFS）。每个算子的实际推理在 `run_infer_step` 里调 `next->InferLayout(LayoutInferArgs{target, thread_bounds, layout_map}, level)`（[layout_inference.cc:282-283](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/layout_inference.cc#L282-L283)）——不同算子（copy/gemm/reduce）各自实现 `InferLayout`，决定「给定输入布局，输出该是什么布局」。

> 与 4.1 的衔接：`AscendInferBufferScope`（phase.py:52）先给 L1 注入默认 zN，`LayoutInference`（phase.py:67）随后把它传播开。所以即便你不写 `T.annotate_layout`，zN 也会经这两步到达 codegen。`T.annotate_layout` 的作用是**覆盖默认、或给非 L1 buffer 提供传播起点**。

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：验证「不标注也能得到 zN」是因为两步 pass 的接力。
2. **操作步骤**：在 `phase.py` 里定位 `LowerAndLegalize` 的 pass 序列，确认 `AscendInferBufferScope`（L52）在 `LayoutInference`（L67）之前。再在 `layout_inference.cc` 的 `Run()` 里数三级循环。
3. **观察现象**：若把默认 zN 注入注释掉（仅思考，不真改源码），`LayoutInference` 就失去传播起点，最终 codegen 会退化成 `layout::RowMajor`（见 4.2.3 的 fallback 分支）。
4. **预期结果**：能讲清「默认 zN 来自 AscendInferBufferScope，传播靠 LayoutInference，打印靠 codegen」这条三段链。
5. **说明**：纯阅读理解，不运行。

#### 4.4.5 小练习与答案

**练习 1**：`LayoutInference` 为什么要在 `LowerTileOp` 之前跑？
**答案**：因为它依赖每个算子（copy/gemm/reduce）的 `InferLayout` 语义来传播布局；一旦 `LowerTileOp` 把高层 tile op 降成底层循环，算子边界就消失了，无法再做「按算子推理布局」。所以必须趁 tile 语义还在时传播。

**练习 2**：如果 `T.annotate_layout` 给的布局与算子约束冲突，会发生什么？
**答案**：BFS 推理中同一个 buffer 会被推出两种不同布局，触发 `ICHECK(StructuralEqual()(...))` 失败并打印「Get different layout for ...」，提示哪两个布局冲突。这是编译期错误，不会跑到运行期。

---

### 4.5 src/layout 代码地图：Layout / Fragment / SwizzledLayout 与 GPU/Ascend 分工

#### 4.5.1 概念说明

到目前为止我们说的「布局」其实跨了**两条后端路线**，混在同一个 `src/layout` 目录里，初学者很容易张冠李戴。本节把它们分清楚：

- **Ascend 路线**（本讲主线）：布局 = fractal 形状（zN/nZ）+ 一个 `layout_tag`。Python 端 `ascend_layout.py` 算仿射，C++ 端 `layout.h` 的 `LayoutNode` 存标签，codegen 打 `layout::zN`。
- **GPU 路线**（CUDA/HIP/CDNA/Volta）：布局 = `Fragment`（线程→元素的映射，描述 wmma/mfma 寄存器分布）与 `SwizzledLayout`（XOR 重排，消除 shared memory bank conflict）。这套在 `gemm_layouts.cc` / `swizzle.cc` 里。

两条路线**共享** `LayoutInference` pass 的框架（4.4 的 BFS 三级推理是通用的），但「布局对象」与「优化目标」不同：Ascend 优化 Cube 吞吐的对齐，GPU 优化 bank conflict 与寄存器映射。

#### 4.5.2 核心流程

理解 `src/layout` 的阅读顺序：

1. **`layout.h` / `layout.cc`**：定义基类 `LayoutNode`，含 `input_size_`、`forward_index_`、`ascend_layout_` 三个核心字段。所有布局（含 GPU 的子类）都继承自它。`AscendLayoutStr()` 返回 `layout::zN` 这类字符串。
2. **`gemm_layouts.cc`**：GPU 专用。`makeGemmFragment*` 系列构造 `Fragment`（如 `makeGemmFragment8x8` 描述一个 8×8 的线程分布）；`makeHalfBankSwizzleLayout` / `makeFullBankSwizzleLayout` / `makeGemmABLayout` 构造消除 bank conflict 的 swizzle 布局。
3. **`swizzle.cc`**：GPU 专用。`SwizzlePattern(bits, base, shift)` 描述「对哪几位做 XOR」，`SwizzledLayoutNode` 在普通 `Layout` 的 `Forward` 末尾套一层 `pattern_.swizzle(expr)`。

> 关键结论：**Ascend 后端基本不碰 `gemm_layouts.cc` / `swizzle.cc`**。你在本讲前几节看到的 zN/nZ 全部来自 `ascend_layout.py` + `layout.h`。把 `src/layout` 当成「布局 IR 的公共定义 + GPU 的具体实现」，而 Ascend 的具体实现主要在 Python 侧。

#### 4.5.3 源码精读

GPU bank-conflict swizzle 的代表（仅供对照，Ascend 不走这里）：

[gemm_layouts.cc:321-337](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/layout/gemm_layouts.cc#L321-L337) —— `makeHalfBankSwizzleLayout` 把列坐标与「行/2」做 `xor4x4`，让相邻行落到不同 bank；[gemm_layouts.cc:537-556](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/layout/gemm_layouts.cc#L537-L556) `makeGemmABLayout` 按 `element_size / kfactor` 选不同 swizzle 策略。这些函数的目标是 **shared memory bank conflict**，与 Ascend 的 fractal/Cube 吞吐无关。

`SwizzlePattern` 的 XOR 实现：

[swizzle.cc:20-35](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/layout/swizzle.cc#L20-L35) —— 构造时要求 `shift >= bits`；`swizzle(expr)` 把 `expr` 拆成 `high/low`，对 `high` 的某几位（`mask`）做 XOR 再重组。[swizzle.cc:53-59](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/layout/swizzle.cc#L53-L59) 的 `SwizzledLayoutNode::Forward` 在基类 `Forward` 结果上再套 `pattern_.swizzle(expr)`。

对照 Ascend 的 `LayoutNode`（标签式）：

[layout.h:37-78](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/layout/layout.h#L37-L78) —— `LayoutNode` 同时承载 `forward_index_`（坐标映射）与 `ascend_layout_`（标签）。GPU 的 `SwizzledLayoutNode` 继承它并加上 `pattern_`；Ascend 的布局则只用基类的标签能力。两者共享基类与 `LayoutInference` 框架。

#### 4.5.4 代码实践（源码阅读型）

1. **实践目标**：建立「哪个文件服务哪条后端」的清晰地图。
2. **操作步骤**：在 `src/layout` 下用编辑器折叠浏览：`layout.h`（公共基类）→ `layout.cc`（基类方法）→ `gemm_layouts.cc` + `swizzle.cc`（GPU 实现）。再回看 `tilelang/intrinsics/ascend_layout.py`（Ascend 实现）。
3. **观察现象**：注意 `gemm_layouts.cc` 里所有函数名都带 `Gemm`/`CDNA`/`Volta`/`Hopper` 等 GPU 架构名，而 Ascend 的工厂函数叫 `make_zn_layout`（小写 + Ascend 术语）——命名风格本身就透露了归属。
4. **预期结果**：看到「bank conflict / Fragment / xor」能立刻判断是 GPU 路线；看到「zN / fractal / C0」能判断是 Ascend 路线。
5. **说明**：纯阅读，目的是避免后续读源码时把两套混为一谈。

#### 4.5.5 小练习与答案

**练习 1**：`LayoutInference` pass 是 Ascend 专用吗？
**答案**：不是。它是一个**通用** pass（注册名 `tl.LayoutInference`），GPU 和 Ascend 都跑。差别在于「布局对象」：GPU 侧是 `Fragment`/`SwizzledLayout`，Ascend 侧是带 `layout_tag` 的基础 `Layout`。pass 的 BFS 三级推理框架对两者一致。

**练习 2**：为什么 Ascend 的 zN 实现主要在 Python（`ascend_layout.py`），而 GPU 的 swizzle 在 C++（`gemm_layouts.cc`）？
**答案**：设计选择。Ascend 的 fractal 仿射式较长、易变，留在 Python 便于调试与迭代（`ascend_infer_buffer_scope.cc` 的注释也明说「Python 端便于调试，C++ 只回调」）；GPU 的 `Fragment`/swizzle 与 TVM 的 C++ IR 结合更紧，且涉及寄存器分配，留在 C++ 更自然。两者通过 FFI 的 `tl.ascend.make_zn_layout` 桥接。

---

## 5. 综合实践

把本讲四条线索串起来，完成一次「布局 + swizzle」的可观测改造：

**任务**：基于 `examples/gemm/example_gemm_intrinsic.py`，做三件事并用 `func.get_kernel_source()` 验证。

1. **显式标注布局**：在 `with T.Scope("C"):` 内对 `A_L1`、`B_L1` 调 `T.annotate_layout({A_L1: make_zn_layout(A_L1), B_L1: make_zn_layout(B_L1)})`。在生成代码里确认搬运模板出现 `layout::zN`（对应 4.2）。
2. **追踪传播**：阅读 `phase.py`，讲清楚这两个 buffer 的 zN 实际来自哪一步——是你写的 `T.annotate_layout`，还是 `AscendInferBufferScope` 的默认注入？分别注释掉其中一个（仅思考），推断生成代码的差异（对应 4.1 + 4.4）。
3. **调 swizzle**：保留并调整 `T.use_swizzle(..., off=3)` 的 `off`（试 `1` 与 `5`），在生成代码里确认 `thread_block_swizzle<..., off, 0>(pid)` 的模板实参随之变化；在能跑的环境下用 `do_bench`（或 u7-l4 的 msprof）记录三组耗时（对应 4.3）。

**验收**：能画出这样一张数据通路图——

```
T.annotate_layout / AscendInferBufferScope(默认)
        │  写入 block 的 kLayoutMap
        ▼
   LayoutInference (BFS 三级传播)  ──→  每个 buffer 都有 layout_map
        ▼
   codegen 读 layout_map → 打印 layout::zN    （数据摆放）
   T.use_swizzle → thread_block_swizzle 模板   （核间调度）
```

并用自己的话讲清：**布局（layout）决定数据怎么摆，swizzle 决定核怎么轮流算；两者正交，可叠加。** 性能数字与正确性「待本地验证」（需真实 NPU 或 camodel 仿真，见 u7-l5）。

## 6. 本讲小结

- Ascend 的 Cube 核以 **fractal** 为单位吞吐数据，L1 里的矩阵默认按 **zN** 排布（16 行一摞的 fractal，由 `BYTE_PER_C0=32 / C0_NUM_PER_FRACTAL=16` 决定粒度），不是简单行优先。
- `T.annotate_layout({buf: layout})` 把 `{buffer: Layout}` 作为块属性 `layout_map` 贴到 block 上；`make_zn_layout` / `make_nz_layout` 工厂同时产出仿射变换与 `layout_tag`（一份给推理、一份给 codegen 打印）。
- 默认 zN 由 `AscendInferBufferScope` pass 注入（它回调 Python `make_zn_layout`），`T.annotate_layout` 用于覆盖默认或给非 L1 buffer 提供传播起点。
- `T.use_swizzle` 是**核间任务重排**（不是数据布局）：经 `tl.ascend_use_swizzle` intrinsic → `UseSwizzleCodegen` → `thread_block_swizzle` 模板，用 `off` 偏移让相邻核错开取数据，提升 L2 cache 命中率。前端有两个同名 `use_swizzle`，靠 `del + 重定义` 切到 Ascend 版。
- `LayoutInference` pass 用 BFS 三级（Strict/Common/Free）推理把布局沿算子链自动传播，排在 `LowerTileOp` 之前；它是 GPU/Ascend 共用的通用框架。
- `src/layout` 里 `gemm_layouts.cc` / `swizzle.cc` 是 **GPU 路线**（bank conflict、Fragment），Ascend 的布局实现主要在 Python `ascend_layout.py`；两者共享 `LayoutNode` 基类与 `LayoutInference` 框架。

## 7. 下一步学习建议

- **顺接 u5（CV 分离与跨核机制）**：zN 布局是 Cube 侧 L1/L0 的约定；当 Cube 结果要传给 Vector（L0C→UB），数据要在 fractal 与 Vector 布局间转换，这正是 u5-l4「Workspace 消除」要解决的搬运开销问题。
- **深入 codegen**：想看 `layout::zN` 如何驱动真实 `DataCopy` 指令，可读 u6-l2「Ascend C / PTO 双 Codegen」与 u6-l3「tl_templates 模板库」，重点对照 `src/tl_templates/ascend/common.h` 里带 `layout::zN` 的搬运模板。
- **高性能 GEMM 实战**：本讲的 swizzle + 布局只是两块拼图，完整的极致性能 GEMM 还需多缓冲、flag 流水、`kL0Size` 调参，见 u7-l2「高性能 GEMM 优化」。
- **调参自动化**：`off`、`block_M/N`、`num_stages` 这类旋钮可以交给 autotuner 自动搜索，见 u7-l6「自动调参 Autotuner」。
