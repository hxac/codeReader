# CuTe Atoms：MMA 与 Copy 原子

## 1. 本讲目标

上一讲（u2-l3）我们让 CuTe 张量「动起来」：`cute::copy` 搬运数据、`cute::gemm` 做乘加，并且看到同一个算法函数会根据张量的**内存空间**和**静态 Layout** 在编译期自动分派到不同的硬件指令。但那是 CuTe 算法层的「黑盒」——我们并没有回答：一条具体的 Tensor Core 指令到底长什么样？它需要哪些线程、哪些寄存器片段、什么样的数据排布？又怎么把它复用到更大的 tile 上？

本讲就打开这个黑盒，学完之后你应当能够：

- 说出 CuTe 的 **Operation → Traits → Atom** 三段式封装思想，以及它为什么能把硬件指令和上层算法解耦。
- 读懂 `MMA_Atom` / `Copy_Atom` 如何封装「一条指令」，并能用 `.call(...)` 在张量上触发它。
- 用 `make_tiled_mma` / `make_tiled_copy` 把单个 atom **铺成更大的 tile**，并理解其中的线程划分 `ThrLayoutVMNK`。
- 理解 atom 的 `MMA_Traits` / `Copy_Traits` 与具体架构（SM70/80/90/100…）指令的对应关系。
- 用 `get_thread_slice(...).partition_A/B/C(...)` 把一个全局/共享内存张量切分到「每个线程该看哪些元素」。

## 2. 前置知识

在进入本讲前，你需要已经掌握（来自 u2-l1、u2-l2、u2-l3）：

- **Layout = (Shape, Stride)** 是「坐标 → 线性下标」的纯函数，本身不持有数据（u2-l1）。
- **Tensor = (Engine, Layout)**：Engine 提供数据指针/存储，Layout 负责坐标映射；指针带 gmem/smem/rmem 空间标签，编译期决定走哪条指令（u2-l2）。
- `cute::copy` / `cute::gemm` 是统一入口，靠重载分发到硬件指令（u2-l3）。

本讲要补上的最后一块拼图是：**指令本身**——即 Tensor Core 的 `mma`、`cp.async`、TMA 等指令在 CuTe 里如何被表示成一个可复用的对象（atom）。两个关键术语先记住：

- **MMA**（Matrix Multiply-Accumulate）：Tensor Core 上的矩阵乘加指令，计算 \(D = A\cdot B + C\)。
- **fragment（片段）**：一条 MMA 指令要求每个线程手里持有的那一小块寄存器数据，例如 SM80 的 `mma.m16n8k16` 要求每个线程持有 8 个 half（4 个 A、2 个 B、4 个 C）。atom 的工作之一就是把「逻辑矩阵坐标」翻译成「线程 + 片段」的划分。

## 3. 本讲源码地图

本讲围绕 `include/cute/atom/` 目录展开，这是 CuTe 把硬件指令封装成可复用单元的核心所在。

| 文件 | 作用 |
| --- | --- |
| [include/cute/atom/mma_traits.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/mma_traits.hpp) | 定义 `MMA_Traits` 概念（每条 MMA 指令的元信息）和默认的 `UniversalFMA` 软件实现，以及把片段「拆包」调用指令的 `mma_unpack`。 |
| [include/cute/atom/mma_atom.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/mma_atom.hpp) | 定义 `MMA_Atom`（单条指令）与 `TiledMMA`（拼成大 tile），以及线程切片 `ThrMMA`、工厂 `make_tiled_mma`。**本讲主战场。** |
| [include/cute/atom/copy_atom.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/copy_atom.hpp) | 定义 `Copy_Atom` / `TiledCopy` / `ThrCopy` 与工厂 `make_tiled_copy`，把拷贝指令也做成原子。 |
| [include/cute/atom/copy_traits.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/copy_traits.hpp) | 定义 `Copy_Traits` 概念与 `UniversalCopy`、`AutoVectorizingCopy` 等默认拷贝 trait。 |
| [include/cute/atom/partitioner.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/partitioner.hpp) | 通用的「线程-值」切片器 `TV_Tiler`，是 `ThrMMA`/`ThrCopy` 切分张量背后的统一思路。 |
| include/cute/atom/mma_traits_sm80.hpp、…_sm90.hpp 等 | 各代架构 MMA 指令的 `MMA_Traits` 特化（arch 关联）。 |
| examples/cute/tutorial/sgemm_sm80.cu | 用 `make_tiled_mma` 装配 SM80 GEMM 的真实可运行示例，是本讲实践的蓝本。 |

## 4. 核心概念与源码讲解

### 4.1 Atom 的设计思想：Operation → Traits → Atom

#### 4.1.1 概念说明

GPU 每一代架构都引入一组新的 Tensor Core 指令：Volta 的 `mma`、Ampere 的 `mma.m16n8k16`、Hopper 的 `wgmma`、Blackwell 的 `umma`……它们在形状、数据类型、线程分工上千差万别。如果让上层 GEMM 代码直接写 PTX 内联汇编，代码会被绑死在某一架构上，毫无复用性。

CuTe 的解法是把「一条硬件指令」拆成三层、层层加抽象，最终得到一个干净的、与上层算法同语言（都是 CuTe Layout/Tensor）的对象——**Atom**：

1. **Operation（操作）**：一个最朴素的 C++ `struct`，只做两件事——声明这条指令的**寄存器数组类型**（`DRegisters` 等），并提供一个 `fma(...)` / `copy(...)` 静态函数，内部就是 `asm volatile(...)` 内联 PTX。它不依赖 Layout、不依赖 Tensor，只描述「物理上这条指令要吃几个寄存器」。
2. **Traits（特征）**：一个以 Operation 为模板参数的 `MMA_Traits<MMAOperation>`（或 `Copy_Traits<CopyOperation>`）特化，给出这条指令的**逻辑元信息**——逻辑计算类型 `ValTypeA/B/C/D`、指令的逻辑形状 `Shape_MNK`、线程编号 `ThrID`，以及最关键的 **(线程, 值) → 坐标** 的 Layout（`ALayout`/`BLayout`/`CLayout`）。这一层把「物理寄存器」翻译成「逻辑矩阵坐标」。
3. **Atom（原子）**：把 Operation + Traits 合体，再加上面向 Tensor 的 `.call(...)`、`.partition(...)` 接口，得到一个可以直接作用于 `cute::Tensor` 的对象。上层算法只和 Atom 打交道，完全不碰 PTX。

这三层关系是：**Atom = Traits(Operation)**，而 `MMA_Atom<Operation>` 会自动派生自 `MMA_Traits<Operation>`，从而继承所有元信息。

#### 4.1.2 核心流程

以一条 SM80 的 `mma.m16n8k16` FP16 指令为例，三段的对应关系是：

```text
SM80_16x8x16_F16F16F16F16_TN   (Operation, mma_sm80.hpp)
        │  提供 DRegisters/ARegisters/BRegisters/CRegisters + fma() PTX
        ▼
MMA_Traits<SM80_16x8x16_F16F16F16F16_TN>   (mma_traits_sm80.hpp)
        │  提供 ValType*=half_t, Shape_MNK=<16,8,16>, ThrID, ALayout/BLayout/CLayout
        ▼
MMA_Atom<SM80_16x8x16_F16F16F16F16_TN>     (mma_atom.hpp)
        │  派生自上面的 Traits，加上 call()/make_fragment_*() 接口
        ▼
make_tiled_mma(MMA_Atom{...}, Layout<Shape<_2,_2>>{})  →  TiledMMA
        │  把单个 atom 铺成 2×2 个 atom 的更大 tile，并算出线程布局
```

注意命名规则（来自官方文档）：`SM80_16x8x16_F16F16F16F16_TN` 依次编码了「首推架构 SM80、M×N×K=16×8×16、D/A/B/C 的类型都是 F16、A 行主/B 列主（TN）」。读名字就能知道这条指令做什么。

#### 4.1.3 源码精读

Operation 层：SM80 m16n8k16 指令的 Operation struct，`fma` 内就是 PTX，4 个寄存器数组别名告诉上层「每个线程要给它几个寄存器」。

[include/cute/arch/mma_sm80.hpp:59-87](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/arch/mma_sm80.hpp#L59-L87) —— `SM80_16x8x8_F16F16F16F16_TN` Operation：声明 `DRegisters=uint32_t[2]` 等并内联 `mma.sync.aligned.m16n8k8...` PTX。

Traits 层：给同一条指令补上逻辑元信息，把「线程-值」映射到「M,N 坐标」。

[include/cute/atom/mma_traits_sm80.hpp:62-75](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/mma_traits_sm80.hpp#L62-L75) —— `MMA_Traits<SM80_16x8x8_F16F16F16F16_TN>` 特化：`Shape_MNK = <16,8,8>`、`ThrID = Layout<_32>`（一个 warp 32 线程）、`CLayout = SM80_16x8_Row`。

`MMA_Traits` 概念的「契约」（一个 trait 必须提供哪些字段）写在注释里，默认的 `UniversalFMA` 是 host 可跑的软件兜底实现：

[include/cute/atom/mma_traits.hpp:41-91](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/mma_traits.hpp#L41-L91) —— `MMA_Traits` 概念文档 + `MMA_Traits<UniversalFMA<D,A,B,C>>`：`Shape_MNK=<1,1,1>`、单线程，是理解所有硬件 trait 的「最小模板」。

**arch 关联的关键设计**：`mma_atom.hpp` 在文件末尾按架构版本 `#include` 所有 `mma_traits_smXX.hpp`，所以只要 `#include <cute/atom/mma_atom.hpp>`，从 SM61 到 SM120 的所有 MMA atom 都可用，且通过宏（如 `CUTE_ARCH_MMA_SM80_ENABLED`）在编译时按 `__CUDA_ARCH__` 启用对应 PTX：

[include/cute/atom/mma_atom.hpp:694-703](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/mma_atom.hpp#L694-L703) —— 按架构逐个包含 `mma_traits_sm61.hpp` … `mma_traits_sm120_sparse.hpp`。

#### 4.1.4 代码实践

**实践目标**：建立「Operation / Traits / Atom」三段式的直觉，确认同一名字的指令在三个文件里各司其职。

**操作步骤（源码阅读型）**：

1. 打开 [include/cute/arch/mma_sm80.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/arch/mma_sm80.hpp)，找到 `SM80_16x8x16_F16F16F16F16_TN` 的 Operation struct，记下它的 `ARegisters`/`BRegisters`/`CRegisters` 数组长度。
2. 打开 [include/cute/atom/mma_traits_sm80.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/mma_traits_sm80.hpp)，找到它的 `MMA_Traits` 特化，记下 `Shape_MNK` 和 `ThrID`。
3. 回答：该指令一个 warp（32 线程）共完成多大的矩阵乘？每个线程持有几个 A 元素？

**预期结果**：`Shape_MNK=<16,8,16>` 表示逻辑上算 16×8、K 累加 16；32 线程协作；从 `ARegisters` 长度可算出每线程持有的 A 元素数。具体数值**待本地验证**（取决于你在源码里定位到的精确特化）。

#### 4.1.5 小练习与答案

**练习 1**：为什么要把 Operation 和 Traits 拆成两层，而不是直接写一个大 struct？
**答案**：Operation 只描述「物理指令吃几个寄存器、PTX 是什么」，零依赖、可被不同上层复用；Traits 才把寄存器翻译成逻辑坐标。分开后，同一份 Operation 元信息可以被 atom 层、profiler、代码生成器等各自使用，且让 PTX 隔离在一个文件里便于维护。

**练习 2**：`UniversalFMA` 与 `SM80_16x8x16_F16F16F16F16_TN` 的 `Shape_MNK` 分别是多少？为什么前者是 (1,1,1)？
**答案**：前者 (1,1,1)，后者 (16,8,16)。`UniversalFMA` 是纯软件标量 FMA，单线程算一个乘加，没有 Tensor Core 的并行结构，所以形状退化为 (1,1,1)，可在 host 上运行，用于调试和不依赖特定架构的兜底。

---

### 4.2 MMA_Atom：单条 MMA 指令的封装

#### 4.2.1 概念说明

`MMA_Atom` 是「一条 MMA 指令」在 CuTe 中的最终形态：它派生自对应的 `MMA_Traits`，因此自带所有元信息（形状、线程布局、值类型），又额外提供两个面向 Tensor 的能力——

- **`.call(D, A, B, C)`**：在「已经按本指令要求切分好的」4 个 rank-1 张量上，真正触发一次乘加 \(D = A\cdot B + C\)。它要求这 4 个张量都在寄存器（rmem）里。
- **`.make_fragment_A/B/C(partitioned_tensor)`**：在已经 partition 过的张量上，按指令期望的片段布局，生成寄存器片段张量（从共享内存拷贝到寄存器的目标）。

一句话：`MMA_Atom` 让你用 CuTe Tensor 的语言去喂一条 PTX 指令，而不必手算「第 17 个线程的第 3 个寄存器对应矩阵的哪个元素」。

#### 4.2.2 核心流程

调用一条 MMA 指令的内部流程是：

```text
MMA_Atom::call(D, A, B, C)            // 4 个 rank-1 rmem Tensor
   └─ static_assert 各张量 rank==1
   └─ mma_unpack(Traits, D, A, B, C)  // 进 mma_traits.hpp
        ├─ 从 MMA_Op 取出 RegTypeA/B/C/D（寄存器标量类型，如 uint32_t）
        ├─ recast<RegTypeX>(X)        // 把张量重解释为寄存器元素类型
        ├─ static_assert 片段长度 == RegNumX（指令要求的个数）
        └─ detail::explode(MMA_Op::fma, rD, rA, rB, rC)
                                        // 把张量元素展开成 fma() 的标量参数并调用 PTX
```

关键点：`mma_unpack` 不直接持有指令逻辑，它只是「拆包员」——把 CuTe Tensor 的元素解包成 `MMA_Op::fma` 期望的标量寄存器实参，真正的 PTX 在 `MMA_Op::fma` 里。这样 atom 层和指令层彻底解耦。

#### 4.2.3 源码精读

`MMA_Atom` 的主模板定义：它把传入的 Operation 包成 `MMA_Traits<Op>` 并派生，对外暴露 `ValType*`、`Shape_MNK`、`ThrID`、`Layout*_TV` 等别名。

[include/cute/atom/mma_atom.hpp:44-72](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/mma_atom.hpp#L44-L72) —— `MMA_Atom<MMA_Traits<Op>>` 派生自 Traits，导出值类型与「线程-值」布局别名。

`.call(...)` 接口：校验 4 个张量都是 rank-1，再交给 `mma_unpack`。三参数重载 `call(A,B,C)` 等价于 `call(C,A,B,C)`（用 C 同时当输入和输出，复现累加器）。

[include/cute/atom/mma_atom.hpp:88-118](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/mma_atom.hpp#L88-L118) —— `MMA_Atom::call` 的四参/三参重载，校验 rank==1 后调用 `mma_unpack`。

`mma_unpack`：把 CuTe Tensor 拆成 `MMA_Op::fma` 的标量寄存器实参。注意 4 个 `is_rmem` 断言——MMA 只吃寄存器张量。

[include/cute/atom/mma_traits.hpp:106-151](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/mma_traits.hpp#L106-L151) —— `mma_unpack`：`recast` 到寄存器类型、断言片段长度、`detail::explode(MMA_Op::fma, ...)` 触发 PTX。

#### 4.2.4 代码实践

**实践目标**：体会「`.call()` 只接受 rank-1 寄存器张量」这一约束，理解 atom 与上层算法的接口边界。

**操作步骤（源码阅读型）**：

1. 阅读 [include/cute/atom/mma_atom.hpp:88-105](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/mma_atom.hpp#L88-L105) 的 `call`，注意 `static_assert(DLayout::rank == 1, ...)`。
2. 再阅读 [include/cute/atom/mma_traits.hpp:119-122](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/mma_traits.hpp#L119-L122) 的 4 个 `is_rmem` 断言。
3. 回答：如果你直接拿一个 smem 张量（rank-1）传给 `call`，会在哪一行编译失败？为什么 atom 不自己负责把数据从 smem 搬到寄存器？

**预期结果**：会因 `is_rmem<TA>::value` 为 false 而 `static_assert` 失败。Atom 只负责「算」，不负责「搬」——搬运是 `Copy_Atom`（下一节）的职责，这正是上一讲 `cute::gemm` 内部「先 copy 再 gemm」小流水线的体现。

#### 4.2.5 小练习与答案

**练习 1**：`MMA_Atom::call(A, B, C)`（三参数）和 `call(D, A, B, C)`（四参数）有什么关系？
**答案**：三参数版本等价于 `call(C, A, B, C)`——把 C 同时作为输入累加器和输出 D，即 \(C \leftarrow A\cdot B + C\)，这正是 GEMM 主循环里反复累加 K 维时想要的行为。

**练习 2**：`mma_unpack` 为什么要先 `recast<RegTypeA>(A)` 而不直接用 A 的元素类型？
**答案**：上层张量的逻辑元素类型可能是 `half_t`，但 PTX 指令实际操作的寄存器是 `uint32_t`（一条寄存器装 2 个 half）。`recast` 把张量重解释成指令真实使用的寄存器标量类型，让 `explode` 能把元素逐个喂给 `fma()` 的标量参数。

---

### 4.3 TiledMMA：用 Atom 拼出更大计算单元与线程划分

#### 4.3.1 概念说明

一条 MMA atom 的形状有限（如 16×8×16）。真实 GEMM 的 threadblock tile 往往是 128×128×32，需要把很多个 atom 拼起来。`TiledMMA` 就是「把一个或多个 `MMA_Atom` 沿 M/N/K 维铺开」得到的更大计算单元，它的核心产出是一个**线程布局** `ThrLayoutVMNK`：描述「哪个线程负责 atom 网格里的哪个位置」。

`TiledMMA` 由三样东西构造：

- **Atom**：用哪个 `MMA_Atom`（含其内部线程数 `AtomThrID`，如 warp=32）。
- **AtomLayoutMNK**：atom 沿 M/N/K 各铺几个，如 `Layout<Shape<_2,_2>>` 表示 2×2 个 atom（K 维默认 1）。
- **PermutationMNK**：铺之前对各维做的置换（多数情况用默认 `_`）。

最终线程总数 \(T = \text{AtomThrID} \times \prod\text{AtomLayoutMNK}\)。例如 SM80 atom（32 线程）铺 2×2 得到 128 线程，正好是一个 warp group 里两个 warp 做 M、两个 warp 做 N 的常见结构。

#### 4.3.2 核心流程

`TiledMMA` 的线程布局由 `tiled_product` 生成，它是「原子内线程布局」与「原子间网格布局」的笛卡尔积：

```text
ThrLayoutVMNK = tiled_product(AtomThrID, AtomLayoutMNK)
```

得到的布局是 4 维 `(V, M, N, K)`：`V` 是「atom 内部的线程号」（如 0..31），`M/N/K` 是「atom 在网格里的坐标」。任意 `threadIdx.x` 都能通过 `get_slice(thr_idx)` 映射到一个 `(v,m,n,k)` 坐标，从而知道自己负责哪些 atom。

对一个输入张量，`TiledMMA` 提供 `thrfrg_C/thrfrg_A/thrfrg_B` 把它从 `(M,N,...)` 重组为：

```text
((ThrV,(ThrM,ThrN)), (FrgV,(RestM,RestN,...)))
```

即「哪个线程（Thr）+ 该线程持有的值（FrgV）+ 还剩多少 tile（Rest）」。这正是 partition 的基础（见 4.5）。

#### 4.3.3 源码精读

`TiledMMA` 的定义与线程布局计算：

[include/cute/atom/mma_atom.hpp:208-231](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/mma_atom.hpp#L208-L231) —— `TiledMMA` 结构：`ThrLayoutVMNK = decltype(tiled_product(AtomThrID{}, AtomLayoutMNK{}))`，构造函数调用 `tiled_product` 算出 `thr_layout_vmnk_`。

`thrfrg_C` 的四步重组（注释极为清楚）：置换 → 按 atom 形状切块 → 把 atom 内 (M,N) 变成 (Thr,Val) → 再按 C-线程网格切块。

[include/cute/atom/mma_atom.hpp:249-275](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/mma_atom.hpp#L249-L275) —— `thrfrg_C`：`logical_divide` → `zipped_divide` → `compose(AtomLayoutC_TV,_)` → `zipped_divide`，最终得到 `((ThrV,(ThrM,ThrN)),(FrgV,(RestM,RestN)))`。

工厂函数 `make_tiled_mma`：两个重载——传 `MMA_Atom<Op>` 直接用；传裸 `Op` 则自动包成 `MMA_Atom<Op>`。默认线程布局是 `Layout<Shape<_1,_1,_1>>`（不铺开）。

[include/cute/atom/mma_atom.hpp:526-554](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/mma_atom.hpp#L526-L554) —— `make_tiled_mma`：把二维 `thr_layout` 补成三维（K 维补 0 步长），返回 `TiledMMA`。

线程总数查询与打印工具：

[include/cute/atom/mma_atom.hpp:634-678](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/mma_atom.hpp#L634-L678) —— `size(mma)`/`thr_size(mma)` 返回线程数；`print(TiledMMA)` 打印 `ThrLayoutVMNK`，正是实践任务要用的「打印线程布局」。

真实用法（SM80 GEMM 示例）：把 SM80 m16n8k16 atom 铺成 2×2、并指定 LDSM 所需的置换 tile。

[examples/cute/tutorial/sgemm_sm80.cu:375-377](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/cute/tutorial/sgemm_sm80.cu#L375-L377) —— `make_tiled_mma(SM80_16x8x16_F16F16F16F16_TN{}, Layout<Shape<_2,_2>>{}, Tile<_32,_32,_16>{})`，得到 128 线程的 TiledMMA。

#### 4.3.4 代码实践

**实践目标**：用 `make_tiled_mma` 包装一个 SM80 mma atom，打印其线程布局 `ThrLayoutVMNK`，并验证线程总数。

**操作步骤（修改 + 编译运行型）**：

1. 在 `examples/cute/tutorial/` 下新建 `u2l4_probe_mma.cu`（**示例代码**，非项目原有文件），内容如下：

   ```cpp
   // 示例代码：打印 TiledMMA 的线程布局
   #include <cute/tensor.hpp>
   #include <cute/atom/mma_atom.hpp>
   using namespace cute;

   int main() {
     // SM80 m16n8k16 FP16 atom，铺成 2(M) x 2(N) 个 atom
     TiledMMA mma = make_tiled_mma(SM80_16x8x16_F16F16F16F16_TN{},
                                   Layout<Shape<_2,_2>>{});
     print(mma);                       // 打印 ThrLayoutVMNK 等
     printf("num_threads = %d\n", int(size(mma)));  // 期望 32 * 2 * 2 = 128
     return 0;
   }
   ```

2. 在 [examples/cute/tutorial/CMakeLists.txt](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/cute/tutorial/CMakeLists.txt) 末尾仿照 `cute_tutorial_sgemm_sm80`（第 48–51 行）加一段 `cutlass_example_add_executable(cute_tutorial_u2l4_probe_mma u2l4_probe_mma.cu)`。
3. 用 `cmake .. -DCUTLASS_NVCC_ARCHS=80` 配置后 `make cute_tutorial_u2l4_probe_mma -j` 并运行。

**需要观察的现象**：`print(mma)` 会先输出 `TiledMMA`，再输出 `ThrLayoutVMNK:` 后跟一个 4 维 Layout `(32,2,2,1)` 之类的形状；`num_threads` 应为 128。

**预期结果**：线程布局形如 `(32,2,2,1)`，总线程数 128。这些打印路径全是 host 端静态 Layout 运算，**不触发 PTX**，所以即使在没有 SM80 GPU 的机器上也能看到布局输出（实际运行 `mma` 指令才需要 sm80 硬件）。若编译/输出与本讲描述不符，以本地结果为准（**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：把上例的 `Layout<Shape<_2,_2>>` 改成 `Layout<Shape<_1,_4>>`，线程总数和 `ThrLayoutVMNK` 会怎么变？
**答案**：线程总数仍是 32×1×4 = 128，但布局的 M、N 分量从 (2,2) 变成 (1,4)——即所有「额外」的原子都铺在 N 方向，表示 1 个原子宽 M、4 个原子宽 N 的网格。

**练习 2**：`ThrLayoutVMNK` 为什么是 4 维 `(V,M,N,K)` 而不是 3 维？
**答案**：`V` 是「atom 内部」的线程号（一个 warp 内的 0..31），与 `M/N/K`（atom 在网格中的坐标）是两个不同层次。把它单独成一维，才能用 `get_slice(thr_idx)` 一次性把一个全局 `threadIdx` 拆成「属于哪个 atom 内线程 + 落在哪个 atom 网格位置」。

---

### 4.4 Copy_Atom 与 TiledCopy：把拷贝也做成原子

#### 4.4.1 概念说明

MMA 需要 A/B 在寄存器里，但数据起初在 gmem、中间在 smem——搬运它们同样需要高效指令（`cp.async`、LDSM、TMA…）。CuTe 用**完全对称**的设计把拷贝指令也封装成原子：

- `Copy_Atom<CopyOperation, ValType>`：一条拷贝指令 + 它处理的逻辑值类型。和 `MMA_Atom` 一样，它派生自 `Copy_Traits<CopyOperation>`，提供 `.call(src, dst)`（以及带谓词的 `.call(prd, src, dst)`）。
- `TiledCopy`：把 `Copy_Atom` 铺成一个更大的 tile，给出 `(thr, val) → 坐标` 的布局 `TiledLayout_TV`。
- `ThrCopy`：某个线程的切片，提供 `partition_S`/`partition_D`（切分源/目标张量）。

`Copy_Traits` 比 `MMA_Traits` 多一个维度：它区分 **Src/Dst/Ref** 三套布局——因为拷贝有方向，源端和目标端的线程-值映射可能不同（例如 LDSM 从 smem 读、写寄存器）。`ValType` 参数则让同一个位级指令（如 `cp.async` 一个 128-bit）可以被解释成 8 个 half 或 4 个 float。

#### 4.4.2 核心流程

构造一个 `TiledCopy` 有多种工厂，最常用的是 `make_tiled_copy(atom, thr_layout, val_layout)`：

```text
make_tiled_copy(Copy_Atom<...>{}, thr_layout, val_layout)
   ├─ raked_product(thr_layout, val_layout) → layout_mn  // (m,n)->(thr,val)
   ├─ 求逆 + reshape 得到 (thr,val)->(m,n) 的 TV 布局
   └─ TiledCopy{atom, layout_tv, tiler}
```

另有三个「与某个 TiledMMA 对齐」的便捷工厂 `make_tiled_copy_A/B/C(atom, mma)`：它们直接复用 `mma.get_layoutA_TV()` 等，让「从 smem 搬 A/B 到寄存器」的拷贝线程划分与 MMA 的完全一致——这正是 GEMM 主循环里 `s2r`（shared-to-register）拷贝的标准做法。

#### 4.4.3 源码精读

`Copy_Atom` 主模板：派生自 `Copy_Traits`，把位级 `BitLayout*` 通过 `recast_layout<uint1_t, ValType>` 转成值级 `ValLayout*`，并算出 `NumValSrc`/`NumValDst`。

[include/cute/atom/copy_atom.hpp:44-75](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/copy_atom.hpp#L44-L75) —— `Copy_Atom`：导出 `ValLayoutSrc/Dst/Ref` 与 `NumValSrc/NumValDst`。

`.call(src, dst)`：当张量长度恰等于指令 `NumValSrc` 时调用 `copy_unpack` 触发指令；否则递归剥一维。带谓词版本还会判断 `prd(0)` 决定是否拷贝。

[include/cute/atom/copy_atom.hpp:90-114](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/copy_atom.hpp#L90-L114) —— `Copy_Atom::call(src, dst)`：长度匹配则 `copy_unpack`，否则递归。

`TiledCopy` 与线程-值切分 `tidfrg_S`/`tidfrg_D`：

[include/cute/atom/copy_atom.hpp:185-248](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/copy_atom.hpp#L185-L248) —— `TiledCopy`：`tidfrg_S/D` 把张量切成 `(Thr,(FrgV,FrgX),(RestM,RestN,...))`。

工厂函数 `make_tiled_copy`（通用）与 `make_tiled_copy_A/B/C`（与 MMA 对齐）：

[include/cute/atom/copy_atom.hpp:421-446](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/copy_atom.hpp#L421-L446) —— `make_tiled_copy_A/B/C`：复用 `mma.get_layoutA/B/C_TV()` 让拷贝划分与 MMA 一致。

[include/cute/atom/copy_atom.hpp:490-517](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/copy_atom.hpp#L490-L517) —— `make_tiled_copy`：`raked_product` 组合线程/值布局，求逆得到 TV 布局。

`Copy_Traits` 概念与默认 `UniversalCopy`（标量拷贝，host 可跑）：

[include/cute/atom/copy_traits.hpp:43-92](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/copy_traits.hpp#L43-L92) —— `Copy_Traits` 契约 + `Copy_Traits<UniversalCopy<S,D>>`：`ThrID=Layout<_1>`，`SrcLayout/DstLayout` 按位宽给出。

arch 关联：和 mma 一样，`copy_atom.hpp` 末尾按架构包含各 `copy_traits_smXX.hpp`（含 SM90/SM100 的 TMA trait）。

[include/cute/atom/copy_atom.hpp:658-688](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/copy_atom.hpp#L658-L688) —— 按架构包含 `copy_traits_sm50/75/80/90/100.hpp` 及 SM90/SM100 的 TMA trait。

#### 4.4.4 代码实践

**实践目标**：用一个 `UniversalCopy` atom（host 可跑，无需特殊硬件）构造 `TiledCopy`，理解线程/值布局如何决定「谁搬哪些元素」。

**操作步骤（源码阅读 + 思考型）**：

1. 阅读 [examples/cute/tutorial/sgemm_sm80.cu:176-178](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/cute/tutorial/sgemm_sm80.cu#L176-L178)，看 `make_tiled_copy_A(s2r_atom_a, mma)` 如何让 smem→寄存器拷贝与 MMA 对齐，再用 `get_slice(threadIdx.x).partition_S(sA)` 切出本线程的源片段。
2. 另可参考同目录 `tiled_copy.cu`（在 [examples/cute/tutorial/CMakeLists.txt:53-56](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/cute/tutorial/CMakeLists.txt#L53-L56) 注册为 `cute_tutorial_tiled_copy`），它用纯 `UniversalCopy` 演示 TiledCopy，不依赖 TMA/LDSM，适合入门观察。
3. 回答：为什么 `make_tiled_copy_A` 要传 `mma` 进去？如果拷贝线程划分与 MMA 不一致会怎样？

**预期结果**：拷贝必须与 MMA 对齐，否则搬进寄存器的数据排列和 MMA 期望的片段排列对不上，乘出来就是错的结果。`make_tiled_copy_A` 通过复用 `mma.get_layoutA_TV()` 保证二者一致。

#### 4.4.5 小练习与答案

**练习 1**：`Copy_Traits` 为什么有 `SrcLayout`/`DstLayout`/`RefLayout` 三个，而 `MMA_Traits` 只有 `ALayout`/`BLayout`/`CLayout`？
**答案**：拷贝有源和目标两个端，且两端的线程-值映射可能不同（如 LDSM：源是 smem 的特殊布局，目标是寄存器）。`RefLayout` 是「参考」布局，用来在 src/dst 之间做坐标对齐与变换（`right_inverse(Ref).compose(Src)`）。MMA 的三个布局分别对应 A/B/C 三个矩阵 operand，是另一种切分。

**练习 2**：`Copy_Atom<SM80_CP_ASYNC<...>, half_t>` 第二个模板参数 `half_t` 起什么作用？
**答案**：它指定这条拷贝指令处理的**逻辑值类型**。同一条 128-bit `cp.async` 在位级完全相同，但传 `half_t` 表示把它看成 8 个 half 的向量、传 `float` 则看成 4 个 float。`Copy_Atom` 据此用 `recast_layout<uint1_t, ValType>` 把位级布局换算成值级布局。

---

### 4.5 partition：把张量切分到每个线程

#### 4.5.1 概念说明

有了 `TiledMMA` / `TiledCopy`，最后一步是回答：「给我一个全局或共享内存的张量，再给我一个 `threadIdx`，告诉我这个线程该读写哪些元素？」这就是 **partition**。

- `TiledMMA::get_slice(thr_idx)` 返回一个 `ThrMMA`，它持有该线程在 `(V,M,N,K)` 里的坐标。`ThrMMA::partition_A/B/C(tensor)` 把张量切成「本线程持有的片段」。
- `TiledCopy::get_slice(thr_idx)` 返回 `ThrCopy`，`partition_S/D` 切源/目标张量。
- 更底层的 [include/cute/atom/partitioner.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/partitioner.hpp) 里的 `TV_Tiler` 给出了「线程-值」切分的通用范式：`zipped_divide(tensor, Tiler).compose(TiledLayout_TV, _)`，`ThrMMA`/`ThrCopy` 都是这个思想的特化。

partition 的输出张量第一维是「atom/指令」维（即 `MMA` 或 `CPY`），后面是 `Rest` 维；对它再调 `make_fragment_A/B/C`（或直接当寄存器片段）就得到喂给 `.call()` 的 rank-1 片段。

#### 4.5.2 核心流程

一个线程使用 MMA 的标准三步：

```text
ThrMMA thr_mma = mma.get_slice(threadIdx.x);        // 1. 取本线程切片
Tensor tCgC  = thr_mma.partition_C(gC);            // 2. 切全局 C: (MMA, MMA_M, MMA_N)
Tensor tCrA  = thr_mma.partition_fragment_A(sA);    //    切/建寄存器片段 A: (MMA, MMA_M, MMA_K)
Tensor tCrC  = thr_mma.make_fragment_C(tCgC);      //    建累加器片段
// ... 主循环里: copy(tXsA -> tCrA), 然后 gemm(thr_mma, tCrA, tCrB, tCrC)
```

注意 `partition_C` 作用于「全局/共享内存」张量（输出地址），而 `partition_fragment_A/B`（及其底层 `make_fragment_A/B`）作用于「寄存器」张量——它会按指令期望的片段布局新建一个寄存器张量，其形状匹配 partition 结果，从而让随后的 `copy(smem片段 → rmem片段)` 能向量化。

#### 4.5.3 源码精读

`ThrMMA` 与 `partition_A/B/C`：内部先调 `TiledMMA::thrfrg_X` 算出全线程的布局，再用本线程的 `(v,m,n,k)` 坐标切片，得到只含本线程数据的视图。

[include/cute/atom/mma_atom.hpp:459-495](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/mma_atom.hpp#L459-L495) —— `ThrMMA::partition_C/A/B`：`make_tensor(data, thrfrg_X(layout))` 后用 `thr_vmnk` 坐标切片。

`make_fragment_A/B/C`（定义在 `MMA_Atom` 上，`partition_fragment_*` 是它俩的组合）：根据 partition 后张量的布局，构造匹配的寄存器片段。

[include/cute/atom/mma_atom.hpp:129-195](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/mma_atom.hpp#L129-L195) —— `MMA_Atom::make_fragment_C/A/B`：校验已 partition，按 `FrgType*` 建片段张量（注释解释了为什么要基于已 partition 的布局来「匹配向量化」）。

通用切分器 `TV_Tiler`（partitioner.hpp）：一句话 `zipped_divide(tensor, Tiler).compose(TiledLayout_TV, _)`，是所有 partition 的本质。

[include/cute/atom/partitioner.hpp:50-98](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/partitioner.hpp#L50-L98) —— `TV_Tiler::apply` 与 `TV_Partitioner::partition`：`zipped_divide + compose` 后按坐标切片。

真实主循环里的用法（sgemm_sm80）：`get_slice` → `partition_C` → `partition_fragment_A/B` → `make_fragment_C`。

[examples/cute/tutorial/sgemm_sm80.cu:156-163](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/cute/tutorial/sgemm_sm80.cu#L156-L163) —— `ThrMMA thr_mma = mma.get_slice(threadIdx.x); tCgC=partition_C(gC); tCrA=partition_fragment_A(sA); tCrB=partition_fragment_B(sB); tCrC=make_fragment_C(tCgC);`。

#### 4.5.4 代码实践

**实践目标**：用 `partition_A/B/C` 把一个小张量切到某个线程，观察每个线程分到的元素，建立「partition = 给每个线程发各自的数据视图」的直觉。

**操作步骤（编译运行型，接 4.3.4 的 probe 文件）**：

1. 在 4.3.4 创建的 `u2l4_probe_mma.cu` 的 `main()` 里，`print(mma)` 之后追加（**示例代码**）：

   ```cpp
   // 示例代码：观察 partition_A/B/C
   using T = cutlass::half_t;
   auto gA = make_tensor<T>(make_shape(Int<16>{}, Int<16>{}));  // (M,K) = (16,16)
   auto gB = make_tensor<T>(make_shape(Int< 8>{}, Int<16>{}));  // (N,K) = (8,16)
   auto gC = make_tensor<T>(make_shape(Int<16>{}, Int< 8>{}));  // (M,N) = (16,8)

   for (int tid = 0; tid < int(size(mma)); tid += 32) {          // 只看每 warp 的 0 号线程
     auto thr_mma = mma.get_thread_slice(tid);
     auto tCrA = thr_mma.partition_fragment_A(gA);               // (MMA, MMA_M, MMA_K)
     auto tCrB = thr_mma.partition_fragment_B(gB);               // (MMA, MMA_N, MMA_K)
     auto tCgC = thr_mma.partition_C(gC);                       // (MMA, MMA_M, MMA_N)
     printf("tid=%2d  shape(tCrA)=", tid); print(shape(tCrA)); printf("\n");
     printf("tid=%2d  shape(tCrB)=", tid); print(shape(tCrB)); printf("\n");
     printf("tid=%2d  shape(tCgC)=", tid); print(shape(tCgC)); printf("\n");
   }
   ```

2. 重新编译运行 `cute_tutorial_u2l4_probe_mma`。

**需要观察的现象**：每个线程的 `tCrA/tCrB/tCgC` 第一维 `MMA` 等于该 atom 一次 call 的片段数（SM80 m16n8k16 下 A 片段为 4 等），后两维随 tid 不同而指向矩阵的不同区域；不同线程分到不同的元素集合，合起来覆盖整个 tile。

**预期结果**：能看到每个线程分到一个 `(MMA, MMA_M, MMA_K)` 形状的 A 片段（具体数值**待本地验证**）。partition 同样是纯 Layout 运算，host 端即可打印形状。

#### 4.5.5 小练习与答案

**练习 1**：`partition_C(gC)` 返回的张量第一维为什么叫 `MMA` 维？它代表什么？
**答案**：第一维对应「一次 MMA 指令 call 所需的值集合」——即 atom 内 (Thr,Val) 中本线程的 Val 部分。后面两维 `MMA_M/MMA_N` 是「还有多少个这样的 atom 片段」。把第一维喂给 `MMA_Atom::call` 就是一次完整的指令调用。

**练习 2**：`partition_fragment_A` 和 `partition_A` 有什么区别？
**答案**：`partition_A(atensor)` 返回的是对**输入张量 atensor 本身**的视图（共享 atensor 的数据指针，通常是 smem/gmem）；`partition_fragment_A` = `make_fragment_A(partition_A(atensor))`，它在 partition 结果上新建一个**寄存器片段张量**（独立存储），形状匹配但用于存放即将拷贝进寄存器的数据，随后作为 `copy` 的目标、`gemm` 的输入。

---

## 5. 综合实践

把本讲四块内容（atom 三段式、MMA_Atom、TiledMMA、partition）串成一个完整的小任务：**读懂一段真实 GEMM 主循环的开头，并用自己的 probe 复现其中的关键对象**。

任务步骤：

1. **阅读真实代码**：打开 [examples/cute/tutorial/sgemm_sm80.cu](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/cute/tutorial/sgemm_sm80.cu)，定位三处：
   - 第 375–377 行：`make_tiled_mma(SM80_16x8x16_F16F16F16F16_TN{}, Layout<Shape<_2,_2>>{}, Tile<_32,_32,_16>{})` 装配 TiledMMA。
   - 第 156–163 行：`get_slice` + `partition_C` + `partition_fragment_A/B` + `make_fragment_C`，建立每个线程的 C 累加器与 A/B 寄存器片段。
   - 第 176–178 行：`make_tiled_copy_A(s2r_atom_a, mma)` + `get_slice` + `partition_S`，建立与 MMA 对齐的 smem→寄存器拷贝。
2. **画出数据流图**：用箭头标出 `gA →(TiledCopy A)→ sA →(s2r TiledCopy)→ tCrA →(ThrMMA/MMA_Atom.call)→ tCrC` 这条从全局内存到累加器的完整路径，并标注每一步用的是本讲的哪个对象。
3. **写 probe 验证**：把 4.3.4 与 4.5.4 的 `u2l4_probe_mma.cu` 合并成一个程序，打印：① `mma` 的 `ThrLayoutVMNK` 与线程总数；② `make_tiled_copy_A` 得到的 TiledCopy 的 `Tiler_MN` 与线程数；③ 某个线程 `partition_A/B/C` 的形状。编译运行（`CUTLASS_NVCC_ARCHS=80`）。
4. **回答检验问题**：
   - TiledMMA 的线程数和 TiledCopy（由 `make_tiled_copy_A` 得到）的线程数是否相等？为什么必须相等？（提示：同一个 thread block 里线程数固定。）
   - 把 atom 从 `SM80_16x8x16_F16F16F16F16_TN` 换成 `UniversalFMA<T,T,T>`（见 sgemm_sm80.cu 第 478 行的用法），`ThrLayoutVMNK` 会变成什么样？这对应「不用 Tensor Core」的退化情形。

如果手头没有 SM80 GPU，第 3 步的打印部分仍可在 host 端运行（都是静态 Layout 运算）；只有真正触发 `mma` PTX 的 `.call()` 才需要 sm80 硬件。运行结果以本地为准。

## 6. 本讲小结

- CuTe 用 **Operation → Traits → Atom** 三段式封装硬件指令：Operation 是裸 PTX + 寄存器数组，Traits 补上逻辑形状与「线程-值→坐标」布局，Atom 合体后提供面向 Tensor 的 `.call()`。
- `MMA_Atom` 是「一条 MMA 指令」，`.call(D,A,B,C)` 经 `mma_unpack` 把 rank-1 寄存器张量拆包成 `MMA_Op::fma` 的标量实参触发 PTX；它只吃 rmem 张量。
- `TiledMMA` 用 `tiled_product(AtomThrID, AtomLayoutMNK)` 把 atom 铺成更大 tile，核心产出是线程布局 `ThrLayoutVMNK`；`make_tiled_mma` 是其工厂。
- `Copy_Atom`/`TiledCopy` 与 MMA 完全对称，但多出 Src/Dst/Ref 三套布局和 `ValType` 值类型；`make_tiled_copy_A/B/C` 让拷贝划分与给定 MMA 对齐。
- **partition** 回答「每个线程读写哪些元素」：`get_slice(thr_idx)` 得到 `ThrMMA`/`ThrCopy`，`partition_A/B/C` 与 `partition_S/D` 切出本线程片段，底层范式是 `zipped_divide + compose`（见 `TV_Tiler`）。
- 所有 atom 的 arch 关联通过文件末尾按架构 `#include` 对应 `*_traits_smXX.hpp` 实现，从 SM61 到 SM120（含 TMA）统一可用。

## 7. 下一步学习建议

本讲把「单条指令」到「一个 tile 的线程划分」打通了。接下来可以：

1. **进入 CUTLASS 3.x 的 GEMM 通用模型**（u2-l7、u2-l8）：看 `CollectiveBuilder` 如何自动挑选本讲这些 atom（`make_tiled_mma`、TMA copy atom）并组装成完整的主循环，你会在这里再次见到 `ThrMMA::partition_*`。
2. **深入 arch 层**（u2-l5）：系统浏览 `mma_sm70/80/90/100` 与 `copy_sm90_tma`，理解 `wgmma`/`umma`/TMA 这些更高层指令的 Operation 与 Traits，以及它们如何要求 warpgroup 级别的线程划分。
3. **读 Hopper warp-specialized 实战**（u2-l9）：看 producer/consumer 如何用本讲的 TMA `Copy_Atom` 搬数据、用 `MMA_Atom` 算，并用异步流水线（u3-l1）把它们重叠起来。
4. 想亲手验证布局代数，可继续读 [media/docs/cpp/cute/0t_mma_atom.md](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/media/docs/cpp/cute/0t_mma_atom.md) 官方文档，它按 Volta/Ampere/Hopper 逐代讲解了 atom 命名与线程布局的推导。
