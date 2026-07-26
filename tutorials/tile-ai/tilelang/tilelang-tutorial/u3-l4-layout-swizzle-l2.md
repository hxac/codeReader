# 布局标注、swizzle 与 L2 优化

## 1. 本讲目标

学完本讲后，你应该能够：

1. 说清楚 **Layout** 是什么：一个「从逻辑索引到物理位置」的纯函数，以及它如何用 `forward_fn` 描述。
2. 说清楚 **Fragment** 在 Layout 之上多了什么：线程映射，也就是决定「矩阵的某个元素到底落在哪个线程的哪个寄存器里」。
3. 区分 tilelang 里**两个都叫 swizzle 但完全不同的概念**：
   - **共享内存 swizzle（shared-memory swizzle）**：消除 bank conflict、配合张量核（WGMMA/MFMA）读 descriptor 的物理布局重排，由布局推理（layout inference）自动完成。
   - **线程块 swizzle（threadblock swizzle / rasterization）**：`T.use_swizzle` 改变线程块遍历输出网格的顺序，提升 L2 cache 命中率，**完全不碰内存布局**。
4. 会用 `T.use_swizzle(panel_size=)` 给一个 GEMM 提升局部性，并用 `plot_layout` 工具可视化 shared 布局。

> 本讲承接 [u3-l1](u3-l1-gemm-and-tileop.md)。u3-l1 讲过「一行 `T.gemm` 被编译器映射成张量核指令」，本讲要回答：这些指令读写 shared/fragment 时，数据在物理上到底是怎么排布的、又是谁决定的；以及「遍历顺序」这种和布局无关的优化怎么写。

## 2. 前置知识

- **GPU 的三级内存**：global（显存，大但慢）→ shared（片上共享内存，小但快，分 32 个 bank）→ fragment/register（寄存器，最快）。回顾 [u2-l2](u2-l2-memory-hierarchy-and-copy.md)。
- **bank conflict**：shared memory 被切成 32 个 bank，每个 bank 每周期只能服务一次访问。若一个 warp 的多个线程同时访问同一 bank，就会串行化，产生 bank conflict，拖慢访存。
- **张量核（Tensor Core / MMA / WGMMA / MFMA）**：硬件级矩阵乘指令，一次算一小块矩阵（如 16×8、64×256），但对输入数据在 shared/register 里的排布有苛刻要求。
- **L2 cache 与线程块调度**：GPU 会把若干线程块（CTA）同时驻留在 SM 上。相邻被调度的 CTA 如果读同一块 global 数据，就能复用 L2 cache 里的副本，少跑一次显存往返。
- **索引映射 = 函数**：把多维下标看成自变量，物理地址看成函数值，所谓「布局」就是这一个函数。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `tilelang/layout/layout.py` | `Layout` 类：逻辑索引 → 物理索引的纯函数抽象。 |
| `tilelang/layout/fragment.py` | `Fragment` 类：继承 Layout，额外带「线程映射」，描述寄存器级 TV 布局。 |
| `tilelang/layout/swizzle.py` | 一组 shared swizzle 构造助手（full/half/quarter bank、wgmma、tcgen05 等）。 |
| `tilelang/layout/swizzle_mode.py` | `SwizzleMode` 枚举：NONE/32B/64B/128B，及给 WGMMA/TCGEN05 descriptor 用的字段换算。 |
| `tilelang/language/annotations.py` | `T.use_swizzle` / `T.annotate_layout` 等 DSL 标注助手。 |
| `tilelang/language/tile_schedule.py` | `PersistentTileScheduler`：持久化 kernel 里用 `swizzle_size` 做 L2 友好遍历的另一种途径。 |
| `src/layout/gemm_layouts.cc` | C++ 侧 `MakeSwizzledLayout`、`DetectSwizzleMode` 等 swizzle 实现。 |
| `src/tl_templates/cuda/threadblock_swizzle.h` | `rasterization2DRow/Column` 模板：线程块栅格化的实际计算。 |
| `src/cuda/codegen/codegen_cuda.cc` | 代码生成时消费 `threadblock_swizzle_pattern` 标注。 |
| `tilelang/tools/plot_layout.py` | 把 Layout / Fragment 画成 2D 网格图的可视化工具。 |
| `examples/plot_layout/layout_swizzle.py` | shared swizzle 可视化示例（注意：见 4.3.4 的签名提示）。 |

## 4. 核心概念与源码讲解

### 4.1 Layout 抽象：从逻辑索引到物理位置的映射

#### 4.1.1 概念说明

在 tilelang 里，一个 `Layout` **不是**「数组的形状」，而是一个**索引映射函数**：它告诉你「如果我想访问逻辑坐标 \((i, j, ...)\) 的元素，它在物理上应该去读第几个位置」。

形式化地说，给定输入形状 \(\text{shape}=(d_0, d_1, \dots, d_{n-1})\)，一个 Layout 是一个函数

\[
L : (i_0, i_1, \dots, i_{n-1}) \;\mapsto\; (o_0, o_1, \dots, o_{m-1})
\]

其中 \((o_0, \dots)\) 是物理（输出）坐标。最朴素的「行主序线性布局」就是

\[
L(i, j) = (i \cdot N + j)
\]

把二维坐标压平成一维。而 swizzle / 转置 / 分块等「花式布局」，本质都只是把右边换成别的表达式（常常含 `floordiv`、`floodmod`、按位 `xor`）。把这些布局当成**函数**来看，就能用 `forward_fn` 一句话描述。

#### 4.1.2 核心流程

构造一个 Layout 的步骤是：

1. 按输入 `shape` 为每一维生成一个迭代变量 `i0, i1, ...`（每个是一个 `IterVar`，范围 \([0, d_k)\)）。
2. 调用用户传入的 `forward_fn(*vars)`，得到「物理坐标表达式」。
3. 用这些迭代变量和物理表达式，在 C++ 后端构造 `tl.Layout` 对象。

之后可以用 `map_forward_index(indices)` 把一组逻辑下标代进去算物理坐标，等价于构造一个 TVM `IndexMap` 再 `map_indices`。Layout 还能 `repeat`（在某维上重复，常用来把一个「原子布局」拼成更大的布局）、`expand`（前面补若干维）、`inverse`（求逆映射）。

#### 4.1.3 源码精读

构造函数把 `shape` 和 `forward_fn` 落成 `tl.Layout`：

[.tilelang/layout/layout.py:13-43](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/layout/layout.py#L13-L43) — 为每维建 `IterVar`，调用 `forward_fn` 算出 `forward_index`，经 FFI 构造 C++ 对象。这里的关键直觉是：**用户只写一个 lambda，编译器自动得到一个可求值、可求逆的索引映射**。

把逻辑下标代进布局求物理坐标（实现就是构造 `IndexMap` 再映射）：

[.tilelang/layout/layout.py:97-125](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/layout/layout.py#L97-L125) — `map_forward_index` 是 Layout 最常用的「求值」入口，也是 `plot_layout` 逐格画图时反复调用的函数。

把一个小布局拼大的 `repeat`：

[.tilelang/layout/layout.py:127-170](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/layout/layout.py#L127-L170) — 例如 8×8 的 MMA 原子布局，`repeat` 后就铺成一个 warp/block 级的大布局。

#### 4.1.4 代码实践

**目标**：亲手构造一个 Layout 并可视化，建立「布局 = 函数」的直觉。

**步骤**：

1. 确认装了可视化依赖：`pip install "tilelang[vis]"`（见 `examples/plot_layout/README.md`）。
2. 运行下面的**示例代码**（手工构造一个 4×4 转置布局）：

   ```python
   from tilelang.layout import Layout
   from tilelang.tools import plot_layout

   # 示例代码：逻辑 (i, j) 映射到物理 (j, i)，即转置
   T_layout = Layout((4, 4), lambda i, j: [j, i])
   print("input  shape:", T_layout.get_input_shape())   # [4, 4]
   print("output shape:", T_layout.get_output_shape())  # [4, 4]
   print("(2,1) ->", T_layout.map_forward_index([2, 1]))  # 期望 [1, 2]

   plot_layout(T_layout, name="transpose_4x4", formats="png")
   ```

3. 观察 `./tmp/transpose_4x4.png`：输入网格里每个格子标注的是它映射到的物理坐标。

**需要观察的现象**：`map_forward_index([2,1])` 返回 `[1, 2]`，说明布局函数确实把行列互换了；图里能看到一条对角对称的编号排布。

**预期结果**：能正确打印形状、映射值，并生成图片。

**待本地验证**：若无显示环境，改 `formats="png"` 并查看生成的 png 文件即可。

#### 4.1.5 小练习与答案

1. **问**：写一个把 4×4 逻辑坐标「压平成一维行主序」的 Layout，其 `forward_fn` 应返回什么？
   **答**：`lambda i, j: [i * 4 + j]`（输出是一维，所以返回单元素列表）。
2. **问**：`Layout((8, 8), lambda i, j: [i, j])` 和 `Layout((8, 8), lambda i, j: [i * 8 + j])` 的 `get_output_shape()` 有何不同？
   **答**：前者输出形状是 `[8, 8]`（二维），后者是 `[64]`（一维）；前者保留二维物理坐标，后者已线性化。

### 4.2 Fragment 布局：寄存器到线程的映射

#### 4.2.1 概念说明

`Fragment` 是 `Layout` 的子类，多了一个核心维度：**线程映射**。寄存器是「每个线程私有」的，所以描述 fragment（即一组寄存器）的数据排布，不仅要回答「物理第几个位置」，还要回答「这个位置在哪个线程的第几号寄存器」。

这就是经典的 **Thread-Value（TV）布局**：给定逻辑元素 \((i, j)\)，确定两件事

\[
\text{thread}(i, j) \quad\text{和}\quad \text{local\_idx}(i, j)
\]

即「哪个线程持有它」和「在该线程寄存器文件里的第几槽」。这一映射直接决定了 `ldmatrix`/`stmatrix`、MMA、WGMMA、MFMA 这些张量核指令能不能正确取到矩阵的每一格——硬件规定了矩阵的每一格必须落在特定的 (线程, 寄存器) 上，Fragment 就是用来精确描述这套规定的。

#### 4.2.2 核心流程

构造 Fragment 时，用户可以一次性给出 `forward_fn`（同时返回线程和索引），也可以分开给 `forward_thread_fn` 与 `forward_index_fn`：

1. 按输入 `shape` 生成迭代变量。
2. 若 `replicate > 1`，额外建一个 `rep` 迭代变量，表示「让多组线程各存一份」（广播，常用于索引/掩码缓冲，所有线程都要完整一份）。
3. 调用函数得到 `forward_thread`（一个 `IterVar`）与 `forward_index`（坐标表达式），交给 C++ 构造 `tl.Fragment`。

之后既能 `map_forward_index`（沿用 Layout 能力），也能 `map_forward_thread`（算出某逻辑元素归哪个线程），还能 `repeat`（沿线程维或数据维复制）。

#### 4.2.3 源码精读

Fragment 构造：支持「合写」与「分写」两种风格，以及 `replicate` 广播：

[.tilelang/layout/fragment.py:23-99](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/layout/fragment.py#L23-L99) — 注意 `forward_fn(*vars, thread_replicate)` 这一支：当 `replicate>1` 时多传一个 `rep` 变量，让同一份数据被多组线程复制持有。

把逻辑下标映射到线程号（供可视化与调试）：

[.tilelang/layout/fragment.py:166-186](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/layout/fragment.py#L166-L186) — `map_forward_thread` 同样构造一个 `IndexMap`，只是 `final_indices` 换成了线程维。

工具函数 `make_gemm_fragment_8x8` 给出标准 8×8 MMA fragment（warp 级 `ldmatrix` 用）：

[.tilelang/layout/swizzle.py:143-152](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/layout/swizzle.py#L143-L152) — 它返回的就是一个 `Fragment`，精确描述了 8×8 矩阵每格落在哪个线程的哪个寄存器。

#### 4.2.4 代码实践

**目标**：直观看到一个 8×8 MMA fragment 是怎么把矩阵元素分配到 32 个线程的。

**步骤**：

1. 运行**示例代码**，画出标准 8×8 fragment 的线程—局部编号映射：

   ```python
   # 示例代码
   from tilelang.layout import make_gemm_fragment_8x8
   from tilelang.tools import plot_layout

   frag = make_gemm_fragment_8x8()      # 8x8 MMA A-matrix 的 TV 布局
   print("threads:", frag.get_thread_size())
   plot_layout(frag, name="mma_8x8_a", formats="png")
   ```

2. 打开 `./tmp/mma_8x8_a.png`：每个格子里会标注 `T<线程号>` 和 `L<局部寄存器号>`。

**需要观察的现象**：同一行的若干元素被分配到不同线程（同一 warp 内），这正是 `ldmatrix` 期望的排布；颜色按线程号区分，能看出 thread→元素的分布规律。

**预期结果**：生成一张 8×8 的网格图，标注线程号与局部寄存器号。

**待本地验证**：`make_gemm_fragment_8x8()` 的确切线程分布取决于当前版本的 fragment 实现，请以本地画出的图为准。

#### 4.2.5 小练习与答案

1. **问**：为什么 `Fragment` 必须在 `Layout` 之上额外引入「线程」这一维，而 shared memory 上的缓冲用普通 `Layout` 就够了？
   **答**：shared memory 对一个 block 内所有线程可见，地址即可定位元素，不需要「哪个线程」；而 fragment 落在**每线程私有**的寄存器里，必须额外指明元素归哪个线程，才能正确生成张量核指令。
2. **问**：`replicate` 参数解决什么问题？
   **答**：当某些数据（如索引、掩码）需要「所有线程各持有一份完整拷贝」时，用 `replicate>1` 让多组线程都存一份；`make_fully_replicated_layout_fragment` 就是这个用途（见 `tilelang/layout/swizzle.py` 末尾）。

### 4.3 共享内存 swizzle 与布局推理

> ⚠️ 本节讲的是**第一种 swizzle**：shared memory 的物理布局重排，目的是消除 bank conflict、配合张量核读 descriptor。它和 4.4 的 `T.use_swizzle`（L2 栅格化）是**两回事**，别混淆。

#### 4.3.1 概念说明

当 WGMMA（Hopper）/ TCGEN05（Blackwell）/ MFMA（CDNA）这类张量核通过 **descriptor** 成块地读 shared memory 时，朴素的行主序排布往往会让一次读取里的多个元素落在同一 bank，触发 bank conflict。解决办法是**把数据在物理上做一次「XOR 重排」**：相邻几行的元素交错排布，使得一次成块访问刚好散布到不同 bank。

这种重排以「粒度」分级，对应 CUDA 的 128B / 64B / 32B 三档 swizzle（B = Byte），在 tilelang 里就是 `SwizzleMode` 的三个枚举。粒度越大（128B），XOR 位数越多，消除冲突越彻底，但对 shape 的对齐要求也越高。

关键点：**这套 swizzle 通常是「自动」的**。当你在 shared 上做 `T.gemm(A_s, B_s, C_f)` 时，GEMM 算子的 `infer_layout` 钩子会为 A_s/B_s 推导出一个 swizzled Layout，交给 **LayoutInference** Pass 落到缓冲上；代码生成时，`DetectSwizzleMode` 再从这个 Layout 反推出它是 32B/64B/128B 哪一档，填进 WGMMA descriptor 的 `layout_type_` 字段。你通常不需要手写，但**可以**用 `T.annotate_layout({buf: make_swizzled_layout(buf)})` 显式指定。

#### 4.3.2 核心流程

自动路径（最常见）：

```
T.gemm(A_s, B_s, C_f)
   └─ gemm op 的 infer_layout 钩子
        └─ 为 A_s/B_s 选 swizzled Layout（按 target/架构选 CuTe/Hopper/CDNA 实现）
             └─ LayoutInference Pass：把 Layout 赋给缓冲，改写所有访问的下标
                  └─ 代码生成时 DetectSwizzleMode(layout, buffer)
                       └─ 得到 SwizzleMode（128B/64B/32B/None）
                            └─ 填入 WGMMA/TCGEN05 descriptor 的 swizzle 字段
```

显式路径（手动指定，常用于自定义算子或测试）：

```
T.annotate_layout({smem: make_swizzled_layout(smem)})
   └─ 标注写入 block 的 layout_map 属性
        └─ LayoutInference 读 annotated_layout_map_ 作为初始已知布局，再向外传播
```

#### 4.3.3 源码精读

Python 侧的 swizzle 构造助手，本质都是「取 buffer，调 C++」：

[.tilelang/layout/swizzle.py:66-68](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/layout/swizzle.py#L66-L66) — 通用 `make_swizzled_layout(buffer, k_major, allow_pad)`。
[.tilelang/layout/swizzle.py:93-126](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/layout/swizzle.py#L93-L126) — 三档 bank swizzle：`make_full_bank_swizzled_layout`（128B）、`make_half_bank_swizzled_layout`（64B）、`make_quarter_bank_swizzled_layout`（32B）。

`SwizzleMode` 枚举与字段换算（给 descriptor 用）：

[.tilelang/layout/swizzle_mode.py:12-49](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/layout/swizzle_mode.py#L12-L49) — 注意 `wgmma_layout_type()`（none→0、32B→3、64B→2、128B→1）和 `tcgen05_layout_type()` 各有自己的编号，因为两代硬件 descriptor 字段定义不同；`smem_alignment()` 给出该模式要求的 shared 基址对齐字节数。

C++ 侧 swizzle 实现 + 模式识别：

[.src/layout/gemm_layouts.cc:921-934](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/layout/gemm_layouts.cc#L921-L934) — `MakeSwizzledLayout`：按 buffer 形状取 `(stride, continuous, element_size)`，再选 Hopper 或可 pad 的通用布局，最后 `ExpandLayout2D` 铺成二维。
[.src/layout/gemm_layouts.cc:966-997](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/layout/gemm_layouts.cc#L966-L997) — `DetectSwizzleMode`：从最小粒度（32B）往大试，用 `StructuralEqual` 判断给定 Layout 是否等于某档 swizzle，从而反推模式。这正是「自动填 descriptor」的依据。

LayoutInference 如何接收用户标注作为起点：

[.src/transform/layout_inference.cc:321-358](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/layout_inference.cc#L321-L358) — `layout_map` 初值就来自 `annotated_layout_map_`（即 `T.annotate_layout` 写入的部分），随后向外传播到所有相关缓冲。

#### 4.3.4 代码实践

**目标**：用 `T.annotate_layout` 显式给一个 shared 缓冲指定 swizzle 布局，并理解它会成为布局推理的起点。

**步骤**：

1. 阅读现成测试 `testing/python/transform/test_tilelang_transform_smem_swizzle_alignment.py`，它演示了「TMA 写入 swizzled shared 必须对齐到 swizzle 周期边界」这一约束：

   [.testing/python/transform/test_tilelang_transform_smem_swizzle_alignment.py:35](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/testing/python/transform/test_tilelang_transform_smem_swizzle_alignment.py#L35) — `T.annotate_layout({smem: make_swizzled_layout(smem)})` 的标准用法。

2. 在一个最小 GEMM 里，给 A_shared 加上显式标注（**示例代码**，仅作阅读/修改对照，运行需 CUDA 环境）：

   ```python
   # 示例代码
   from tilelang.layout import make_swizzled_layout
   # ... 在 kernel 内，T.Kernel 之后 ...
   A_shared = T.alloc_shared((block_M, block_K), dtype)
   T.annotate_layout({A_shared: make_swizzled_layout(A_shared)})
   ```

3. 用 `kernel.get_kernel_source()` 查看生成的 CUDA 源码，定位 shared 缓冲的访问下标是否被改写成 swizzle 形式（含 XOR / 位移）。

**需要观察的现象**：开 `make_swizzled_layout` 后，A_shared 的读写地址计算会从线性下标变成带 XOR 的形式；WGMMA 路径下 descriptor 的 `layout_type_` 不再是 0。

**预期结果**：能编译通过，正确性不变（swizzle 只改物理排布，不改语义）。

**待本地验证**：是否真的出现 XOR 下标取决于目标架构与 GEMM 实现；可在 `TL_DUMP_IR` 打开的 IR 里确认 LayoutInference 前后访问下标的变化。

> **关于 `examples/plot_layout/layout_swizzle.py`**：该示例用 `make_full_bank_swizzled_layout(8, 64, element_size)` 这种**三个整数参数**的写法来画图，但当前源码里这些函数的签名是单参数 `make_full_bank_swizzled_layout(buffer)`（见 [swizzle.py:93-126](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/layout/swizzle.py#L93-L126)），对应的 C++ FFI 绑定 `tl.make_full_bank_swizzled_layout` 也只收单个 `Buffer`（见 `src/layout/layout.cc` 的 `.def(...)` 注册）。**两者不一致**，直接运行该示例可能报参数数量错误——请以源码签名为准，或参考 4.1.4 用 `Layout(shape, forward_fn)` 自行构造后画图。**待本地验证**。

#### 4.3.5 小练习与答案

1. **问**：`SwizzleMode` 的 128B / 64B / 32B 指的是什么「128B」？
   **答**：swizzle 的粒度，即一次重排覆盖的字节数（128/64/32 字节），等价于 8/4/2 个 16 字节向量（`swizzle_atom_size`）。粒度越大、XOR 位数越多。
2. **问**：为什么 `DetectSwizzleMode` 要从 32B 往 128B 试，而不是反过来？
   **答**：因为 32B 是最细粒度，128B 是最粗；一个 128B swizzle 布局在某些形状下可能也满足 32B 的结构相等性判断。从细到粗、配合 `continuous % (vector_size * k)` 的整除性校验，才能挑出**最贴切**的一档（见 [gemm_layouts.cc:966-997](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/layout/gemm_layouts.cc#L966-L997)）。
3. **问**：如果我手动 `T.annotate_layout` 指定了一个 Layout，LayoutInference 还会改它吗？
   **答**：用户标注进的是「初始已知布局」（`annotated_layout_map_`），LayoutInference 以它为起点向外传播，并会校验与之冲突的推断；一般不会覆盖用户显式给定且自洽的布局（冲突会报错而非静默改写）。

### 4.4 T.use_swizzle 与 L2 栅格化（rasterization）

> ⚠️ 这是**第二种 swizzle**：改变线程块遍历输出网格的**顺序**，完全不改动 shared/fragment 的内存布局。它服务的是 **L2 cache 局部性**，不是 bank conflict。

#### 4.4.1 概念说明

考虑一个 GEMM 的 2D 输出 tile 网格（`num_m_tiles × num_n_tiles`）。如果线程块按朴素的行主序遍历（`bx` 直接当列、`by` 当行），那么相邻被调度的两个 CTA 往往沿 N 维排开，各自读不同的 B 列、不同的 A 行——L2 里很难留下可复用的数据。

`T.use_swizzle(panel_size=P)`（也叫 threadblock swizzle / **rasterization**）的做法是：把网格沿「慢轴」切成宽为 P 的**面板（panel）**，让连续的若干个 CTA 先把一个面板里「快轴」的一整列扫完，再推进到面板的下一列。这样相邻 CTA 共享同一段 A（或 B）列，命中 L2；相邻面板之间还做一次方向反转（zig-zag），让面板交界处也能复用一列。

它的「产物」是 kernel 开头的一行 `const dim3 blockIdx = tl::rasterization2DRow<P>();`——**重定义 blockIdx**，把硬件的线性 block id 重新解码成 (列, 行)。注意：它只改「谁先算哪个 tile」，不改任何布局，所以**不影响正确性**，纯粹是性能 hint。

> 持久化 kernel（persistent）里，同样的思想用 `PersistentTileScheduler(swizzle_size=P)` 的 `coord()` 解码来实现，见 4.4.3。

#### 4.4.2 核心流程

非持久化路径（`T.use_swizzle`）：

```
T.use_swizzle(panel_size=P, order="row")
   └─ 生成属性 threadblock_swizzle_pattern = (rasterization2DRow, P)
        └─ 代码生成（codegen_cuda/hip）消费该属性
             └─ 在 kernel 顶部输出：const dim3 blockIdx = tl::rasterization2DRow<P>();
                  └─ 模板内按面板解码 block_idx → (col_idx, row_idx)
```

面板解码的直觉（行主序、面板宽 P 列）：

\[
\text{block\_idx} = \text{blockIdx.x} + \text{blockIdx.y}\cdot\text{gridDim.x}
\]

\[
\text{panel\_idx} = \lfloor \text{block\_idx} \,/\, (\text{gridDim.x}\cdot P)\rfloor,\quad
\text{panel\_offset} = \text{block\_idx}\bmod(\text{gridDim.x}\cdot P)
\]

\[
\text{col} = (\text{panel\_idx}\ \text{为奇数})\ ?\ \text{gridDim.x}-1 - \lfloor\text{panel\_offset}/P\rfloor\ :\ \lfloor\text{panel\_offset}/P\rfloor
\]

\[
\text{row} = (\text{panel\_offset}\bmod P) + \text{panel\_idx}\cdot P
\]

即「先在面板内沿列方向（row 变化最快）扫满，奇数面板反向，跨面板时 row 跳 P 步」。

#### 4.4.3 源码精读

DSL 侧：`T.use_swizzle` 只是把 `(函数名, panel_size)` 包成一个属性：

[.tilelang/language/annotations.py:21-26](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/annotations.py#L21-L26) — `order="row"`→`rasterization2DRow`，`order="column"`→`rasterization2DColumn`；`enable=False` 时返回 `None`（即关闭，方便做 A/B 对比）。

CUDA 代码生成消费该属性，输出重定义的 `blockIdx`：

[.src/cuda/codegen/codegen_cuda.cc:4874-4906](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/codegen/codegen_cuda.cc#L4874-L4906) — 注意 cluster 模式下会改用 `...WithCluster<panel/cluster_x, cluster_x>`，并要求 `panel_size` 能被 `clusterDim.x` 整除。

实际的面板解码模板（就是上面公式的 C++ 实现）：

[.src/tl_templates/cuda/threadblock_swizzle.h:11-45](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/tl_templates/cuda/threadblock_swizzle.h#L11-L45) — `rasterization2DRow` / `rasterization2DColumn`；`panel_idx & 1` 那一支就是面板间 zig-zag 反转。

持久化 kernel 的等价物：`PersistentTileScheduler.coord` 的面板解码：

[.tilelang/language/tile_schedule.py:253-275](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/tile_schedule.py#L253-L275) — `group = tile_id // (primary*swizzle)`、`fast = in_group // width`、`slow = base + in_group % width`，与上面模板是同一套「面板扫描」思想；类文档对 `swizzle_size` 的含义有完整说明：

[.tilelang/language/tile_schedule.py:102-110](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/tile_schedule.py#L102-L110) — `swizzle_size==1` 关闭 swizzle；尾块（不能整除的面板）会被特殊处理（窄一格）。

#### 4.4.4 代码实践

**目标**：给一个 GEMM 打开 / 关闭 `T.use_swizzle`，对比延迟，并确认它只改遍历顺序、不改正确性。

**步骤**：

1. 参考 `examples/gemm/example_gemm_persistent.py` 的非持久化版本，它已经在用 `T.use_swizzle(10)`：

   [.examples/gemm/example_gemm_persistent.py:21](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/gemm/example_gemm_persistent.py#L21) — `T.use_swizzle(10)` 写在 `T.Kernel` 内、计算之前。

2. 复制该 kernel，做两份对照（**示例代码**）：

   ```python
   # 示例代码：A 版打开，B 版关闭
   T.use_swizzle(10)              # A 版
   # T.use_swizzle(10, enable=False)   # B 版（或直接删掉这行）
   ```

3. 分别 `.compile(...)`、用 `get_profiler().do_bench()` 测延迟，并用 `assert_allclose` 验证两版结果一致。

**需要观察的现象**：
- 两版 `assert_allclose` 都通过（证明 swizzle 不改正确性）。
- `get_kernel_source()` 里，A 版开头会出现 `const dim3 blockIdx = tl::rasterization2DRow<10>();`，B 版没有。
- 在足够大的 shape（如 4096×4096×4096）和真实 GPU 上，A 版延迟通常更低（L2 命中更好）。

**预期结果**：正确性一致；有 swizzle 的版本在合适的 shape 下更快。

**待本地验证**：L2 收益依赖 GPU 型号、grid 大小、`panel_size` 取值。无 GPU / CPU 模拟下看不到延迟差异，但可在生成源码里确认 `rasterization2DRow<N>` 是否出现。`panel_size` 的经验值常见为 8–10（如示例里的 10），需按 shape 微调。

#### 4.4.5 小练习与答案

1. **问**：`T.use_swizzle` 改了 shared memory 的数据排布吗？它和 `make_swizzled_layout` 是同一回事吗？
   **答**：没有。`T.use_swizzle` 只重定义 `blockIdx`（改线程块遍历输出网格的顺序），不碰任何缓冲的物理布局；`make_swizzled_layout` 才是真正重排 shared 物理布局以消除 bank conflict。两者一个为 L2、一个为 bank conflict，互不影响。
2. **问**：为什么面板之间要做 zig-zag 反向（`panel_idx & 1`）？
   **答**：让相邻两个面板在交界处沿同一列相向而行，使交界处的 CTA 仍能复用彼此刚加载进 L2 的那一列数据；纯单向扫描在面板边界会有一次「跳远」，损失局部性。
3. **问**：持久化 kernel 里想做同样的 L2 优化，该用 `T.use_swizzle` 还是 `PersistentTileScheduler(swizzle_size=...)`？
   **答**：用后者。持久化 kernel 只启动固定数量的常驻 worker，由调度器自己用 `coord()` 把线性 `tile_id` 解码成 (m, n)，`swizzle_size` 就在解码里起作用（见 [tile_schedule.py:253-275](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/tile_schedule.py#L253-L275)）；此时 `T.use_swizzle` 那套「重定义 blockIdx」已不适用。

## 5. 综合实践

把本讲三件事——**shared swizzle 布局、`T.use_swizzle` 的 L2 优化、布局可视化**——串成一个任务。

**任务**：基于 `examples/gemm/example_gemm_persistent.py` 的非持久化 GEMM，做下面三步。

1. **加 L2 栅格化**：保留 `T.use_swizzle(panel_size=10)`，编译并 `assert_allclose` 验证正确性；再用 `get_kernel_source()` 找到 `tl::rasterization2DRow<10>` 这一行，确认它来自 [codegen_cuda.cc:4874-4906](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/cuda/codegen/codegen_cuda.cc#L4874-L4906) 的处理。
2. **观察自动 swizzle 布局**：开启 `TL_DUMP_IR`（回顾 [u6-l1](u6-l1-pass-system-and-config.md)），在 LayoutInference 前后的 IR 里，找到 `A_shared`/`B_shared` 的访问下标从线性变成 swizzle（含 XOR/位移）的位置；再到生成源码里找 WGMMA descriptor 的 `layout_type_` 字段值，反推它是 128B/64B/32B 哪一档（对照 [swizzle_mode.py:31-45](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/layout/swizzle_mode.py#L31-L45)）。
3. **画一张 shared 布局图**：用 4.1.4 的方法，构造一个与 A_shared 形状相同的 swizzle Layout 并 `plot_layout`（参考 [tilelang/tools/plot_layout.py:6-61](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/plot_layout.py#L6-L61)），对比「线性布局」和「swizzle 布局」两张图，直观看到 XOR 重排如何把相邻行的元素错开。

**验收点**：
- 能指出 `rasterization2DRow` 这一行、并说清它属于 4.4 的 L2 优化而非 4.3 的 bank swizzle。
- 能从 IR 或源码里读出 shared 缓冲用的是哪档 swizzle。
- 画出并能解释线性 vs swizzle 两张布局图的差别。

**待本地验证**：第 2 步的具体 IR 形态、第 3 步具体生成哪档 swizzle，取决于目标架构（Ampere/Hopper/Blackwell）与 GEMM 实现，请以本地实际输出为准。

## 6. 本讲小结

- **Layout = 索引映射函数**：用 `forward_fn` 描述「逻辑坐标 → 物理坐标」，可求值、可求逆、可 `repeat`/`expand`；它只关心位置，不关心线程。
- **Fragment = Layout + 线程映射**：在寄存器级额外指明「元素归哪个线程、哪个寄存器」，是张量核 TV 布局的精确描述。
- **两种 swizzle 必须分清**：
  - shared-memory swizzle（`make_swizzled_layout` / `SwizzleMode` 32B/64B/128B）：重排物理布局，消除 bank conflict，配合张量核 descriptor，由 **LayoutInference 自动**完成，也可 `T.annotate_layout` 显式指定。
  - threadblock swizzle（`T.use_swizzle` / `rasterization2DRow`）：只改线程块遍历输出网格的顺序，提升 **L2 命中**，完全不碰布局。
- **`DetectSwizzleMode`** 把一个 Layout 反推成 32B/64B/128B，用来填 WGMMA/TCGEN05 descriptor 的 swizzle 字段。
- **持久化 kernel** 里 L2 优化改用 `PersistentTileScheduler(swizzle_size=...)` 的 `coord()` 解码，而不是 `T.use_swizzle`。
- **`plot_layout`** 能把 Layout/Fragment 画成 2D 网格，是理解布局最直接的工具。

## 7. 下一步学习建议

- 想看 LayoutInference 的完整传播算法与各 GEMM 实现：进 [u6-l2 关键 lowering Pass 解读](u6-l2-key-lowering-passes.md)，重点读 `src/transform/layout_inference.cc`。
- 想看 swizzle 布局最终如何变成 CUDA 源码与 descriptor：进 [u6-l3 设备代码生成、模板与 tile op lowering](u6-l3-device-codegen-and-templates.md)，读 `src/cuda/op/gemm.cc` 与 `src/tl_templates/cuda/`。
- 想系统用调试工具观察 IR 与布局变化：进 [u9-l1 调试工具：lower trace、pass 可视化与 T.print](u9-l1-debug-tools-lower-trace.md) 与 [u9-l2 布局可视化与 Analyzer](u9-l2-layout-viz-and-analyzer.md)。
- 想了解 H100/Blackwell 的 TMA、cluster、warpgroup 如何与这些布局配合：进 [u9-l3 高级 CUDA intrinsics、TMA/cluster 与 iket](u9-l3-cuda-intrinsics-tma-iket.md)。
