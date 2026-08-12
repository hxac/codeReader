# Ascend 内存层级与地址空间限定符

## 1. 本讲目标

u2-l3 里我们把 `GlobalTensor` / `LocalTensor` / `DataCopy` 当成黑盒跑通了第一个矢量加法算子。从本讲开始（U3「内存层级与数据搬运」），我们要打开这个黑盒，搞清楚一件事：**数据到底存放在 AI Core 的哪一级存储里，又是怎样一级一级搬运到计算单元跟前的**。

学完本讲，你应当能够：

1. 画出 AI Core 的多级内存层次结构（GM → L1 → L0A/L0B/L0C、GM → UB），说出每一级的容量与访问速度特征。
2. 把代码里出现的 `__gm__` / `__ubuf__` / `__cbuf__` / `__ca__` / `__cb__` / `__cc__` 等地址空间限定符，准确对应到物理存储单元。
3. 区分 **Vector 通路（走 UB）** 与 **Cube 通路（走 L1/L0）** 两条数据流，并能对照真实 Kernel 源码标出每一步搬运落在哪一级。

本讲是 U3 的「地图课」：先建立硬件存储的地理认知，u3-l2 才进入 `GlobalTensor`/`LocalTensor` 的数据结构细节，u3-l3 再讲 `DataCopy` 搬运接口。三者环环相扣，但本讲只回答「数据住在哪里、走哪条路」。

## 2. 前置知识

本讲承接 u2-l1（单源编译模型）与 u2-l3（跑通 add 样例），默认你已经理解：

- **`.asc` 单源文件**：Host 与 Device Kernel 混写在同一文件，Kernel 用 `__global__` 标记（u2-l1）。
- **Kernel 限定符**：`__vector__` 表示跑在 Vector 核上、`__cube__` 表示跑在 Cube 核上；Kernel 的指针入参必须用 `__gm__` 标注（u2-l1）。
- **ACL 运行时**：Host 用 `aclrtMalloc` 申请的 Device 内存就是 GM 地址，Kernel 通过指针入参拿到它（u2-l2）。

本讲会**新引入**一个核心直觉——**存储层级（memory hierarchy）**。它和 CPU 的「内存 → L3/L2/L1 Cache → 寄存器」是同一类思想：

> 离计算单元越近的存储，容量越小、速度越快、越贵；离计算单元越远的存储，容量越大、速度越慢、越便宜。数据必须从「远而大」的外存被**搬运**到「近而快」的片上存储，计算单元才能用到它。

AI Core 的特殊之处在于：它的「近」存储不是统一的 Cache，而是**为 Cube（矩阵）和 Vector（矢量）两类计算单元分别定制的专用 Buffer**。这就引出了本讲的两个主角——地址空间限定符（声明数据住在哪）与数据通路（数据沿哪条路走）。

> 名词速查：**AI Core** 是昇腾的处理核心；它内部有 **Cube**（矩阵计算单元）、**Vector**（矢量计算单元）、**Scalar**（标量计算单元）三类计算单元，以及配套的多级存储与搬运单元。本讲只关心存储与搬运。

## 3. 本讲源码地图

本讲用两个对照样例 + 一个核心头文件串起全部概念：矢量加法 `add_tpipe_tque` 代表 **Vector 通路（走 UB）**，矩阵乘 `matmul` 代表 **Cube 通路（走 L1/L0）**。

| 文件 | 作用 | 在本讲中的角色 |
| --- | --- | --- |
| [add_tpipe_tque.asc](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add_tpipe_tque/add_tpipe_tque.asc) | 用 TPipe/TQue 实现的矢量加法 Kernel | **Vector 通路主线**：展示 `__gm__`、`GlobalTensor`(GM)、`LocalTensor`(UB)、`DataCopy`(GM↔UB) |
| [matmul.asc](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/02_matrix/matmul_tensor_api/matmul.asc) | 基于 Tensor API 的静态矩阵乘 Kernel | **Cube 通路主线**：裸写 `__cbuf__`/`__ca__`/`__cb__`/`__cc__`，覆盖 L1/L0A/L0B/L0C 全套限定符 |
| [include/basic_api/kernel_tensor.h](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_tensor.h) | `GlobalTensor` / `LocalTensor` / `LocalMemAllocator` 的声明 | 讲清「Tensor 类型如何把地址空间限定符封装起来」 |

> 本讲还会少量引用几处权威定义作为旁证：`TPosition` 枚举、`Hardware` 枚举、`GetPhyType` 映射函数，以及官方文档《基本架构》《内存层级》。这些只用于把「源码里的限定符 ↔ 物理存储」的对应关系钉死，不要求你背下来。

## 4. 核心概念与源码讲解

### 4.1 AI Core 内存层级

#### 4.1.1 概念说明

数据要在 AI Core 上被算出来，必须先从**片外**搬到**片上**。Ascend 的 AI Core 把存储分成由远到近的三层：

1. **Global Memory（GM，全局内存）**：片外 DDR，所有核共享，容量最大（GB 级）、延迟最高。Host 通过 `aclrtMalloc` 申请的就是这里。Kernel 看到的 `__gm__` 指针就指向 GM。
2. **片上中转存储（L1 Buffer / Unified Buffer）**：每个核**独享**，容量中等。L1 主要服务 Cube，UB 主要服务 Vector。
3. **计算单元私有存储（L0A / L0B / L0C）**：最靠近 Cube 计算单元，容量最小、速度最快。Cube 只能从 L0A/L0B 取数、把结果写进 L0C。

一条可以刻在脑子里的权衡是：**容量与速度此消彼长**。若把片上某级存储的容量记为 \(C\)、访问延迟记为 \(L\)，大致有 \(C_{\text{GM}} \gg C_{\text{L1}} > C_{\text{UB}} \gg C_{\text{L0}}\)，而延迟正好反过来 \(L_{\text{GM}} \gg L_{\text{L1}} \approx L_{\text{UB}} \gg L_{\text{L0}}\)。所以算子性能的命门常常是：**能不能让高速的小存储持续喂饱计算单元**——这正是后续 U13「性能优化」要解决的核心问题，本讲先建立地理认知。

官方文档《基本架构》把存储单元和搬运单元整理成两张表，是理解层级的最权威依据：

| 存储单元 | 作用（摘自官方表） |
| --- | --- |
| **L1 Buffer** | 通用内部存储，Cube 计算单元的数据中转区，可暂存反复使用的数据 |
| **L0A / L0B Buffer** | Cube 指令的输入：L0A 存左矩阵、L0B 存右矩阵 |
| **L0C Buffer** | Cube 指令的输出；累加时也是输入的一部分 |
| **Unified Buffer（UB）** | 向量和标量计算的输入与输出 |
| BT Buffer（BiasTable） | 存放矩阵计算中的 Bias |
| FP Buffer（Fixpipe） | 存放量化参数、Relu 参数等 |

来源：[基本架构.md:129-166](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/docs/zh/guide/编程指南/高级编程/硬件实现/基本架构.md#L129-L166)（存储单元介绍表）。注意一个关键事实：**Vector 计算单元的所有源/目标数据都必须落在 UB**，**Cube 计算单元的直接输入/输出必须落在 L0A/L0B/L0C**——这条硬约束决定了后面要讲的两条数据通路。

#### 4.1.2 核心流程

把存储单元按「远 → 近」摆开，再加上负责搬运的 DMA 引擎（搬运单元），就得到 AI Core 的存储层级骨架：

```
                    Host aclrtMalloc 申请
                            │
                  ┌─────────▼─────────┐
   所有核共享      │   Global Memory   │  容量最大 / 最慢
                  │        (GM)       │
                  └─────────┬─────────┘
                            │  MTE2（搬运单元：GM→L1 / GM→UB / GM→L0A·L0B）
            ┌───────────────┴───────────────┐
   每核独享  │                                 │
            ▼                                 ▼
     ┌─────────────┐                   ┌─────────────────┐
     │  L1 Buffer  │  Cube 中转         │ Unified Buffer  │  Vector/Scalar
     │  (__cbuf__) │                   │    (__ubuf__)   │
     └──────┬──────┘                   └────────┬────────┘
            │  MTE1（L1→L0A/L0B）                │  Vector 直接读写
   ┌────────┴─────────┐                          │
   ▼                  ▼                          ▼
┌─────────┐     ┌─────────┐               ┌──────────────┐
│  L0A    │     │  L0B    │   Cube 输入    │ Vector 计算  │
│(__ca__) │     │(__cb__) │               │              │
└────┬────┘     └────┬────┘               └──────────────┘
     │                │  Cube 矩阵计算单元
     └────────┬───────┘
              ▼
        ┌──────────┐
        │   L0C    │  Cube 输出 / 累加器
        │ (__cc__) │
        └──────────┘
```

四类**搬运单元（MTE / FixPipe）**负责在不同存储间搬运数据，搬运过程中还能做随路的数据格式/类型转换（来源 [基本架构.md:168-202](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/docs/zh/guide/编程指南/高级编程/硬件实现/基本架构.md#L168-L202)）：

| 搬运单元 | 负责通路 |
| --- | --- |
| **MTE2** | GM → {L1, L0A/L0B}、GM → UB |
| **MTE1** | L1 → L0A/L0B、L1 → BT Buffer |
| **MTE3** | UB → GM |
| **FixPipe** | L0C → {GM / L1}（可随路类型转换） |

> 读图要点：**Vector 通路只用到 GM 和 UB**（MTE2 进、MTE3 出），结构简单；**Cube 通路要穿过 GM → L1 → L0A/L0B → L0C → GM**，层级更多。这就是为什么矩阵乘 Kernel 的「搬运代码」明显比矢量加法长——下一节的两个样例会一目了然地印证这一点。

#### 4.1.3 源码精读

软件层面，Ascend C 用两个枚举把这些物理存储「命名」下来。`Hardware` 枚举对应**物理存储硬件**（来源 [impl/utils/common_types.h:23](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/utils/common_types.h#L23)）：

```cpp
enum class Hardware : uint8_t { GM, UB, L1, L0A, L0B, L0C, BIAS, FIXBUF, MAX };
```

而 `TPosition` 枚举对应**逻辑位置**，带有矩阵计算里「左/右/结果矩阵」的语义（来源 [impl/basic_api/common_types.h:27-47](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/basic_api/common_types.h#L27-L47)）：

```cpp
enum class TPosition : uint8_t {
    GM,    // Global Memory
    A1, A2, B1, B2, C1, C2,   // A/B/C 矩阵在 L1(1)/L0(2) 的逻辑位置
    CO1, CO2,                 // Cube 输出在 L0C(CO1) / 后续(CO2) 的位置
    VECIN, VECOUT, VECCALC,   // Vector 通路的 UB 位置（输入/输出/临时）
    ...
};
```

两者通过 [kernel_event.h:369-438](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/basic_api/kernel_event.h#L369-L438) 的 `GetPhyType` 函数一一对应，关键几行：

```cpp
constexpr Hardware GetPhyType(TPosition pos) {
    Hardware hard = Hardware::UB;          // 默认落到 UB
    if (pos == TPosition::GM)   hard = Hardware::GM;
    else if (pos == TPosition::A1) hard = Hardware::L1;   // A1 → L1
    else if (pos == TPosition::A2) hard = Hardware::L0A;  // A2 → L0A
    else if (pos == TPosition::B1) hard = Hardware::L1;   // B1 → L1
    else if (pos == TPosition::B2) hard = Hardware::L0B;  // B2 → L0B
    ...
    else if (pos == TPosition::CO1) hard = Hardware::L0C; // CO1 → L0C
    return hard;
}
```

读法：`A1`/`B1`（带 `1`）= 矩阵 A/B 在 **L1** 的副本；`A2`/`B2`（带 `2`）= 推进到 **L0A/L0B** 的副本；`CO1` = Cube 结果落在 **L0C**；`VECIN`/`VECOUT`/`VECCALC` 全部默认落到 **UB**。**编号 `1` 表示 L1 层、`2` 表示更靠近计算的 L0 层**，这是记忆窍门。

> 不必现在记住全部枚举值。只要记住这张「同义三连」：**物理（Hardware）↔ 逻辑（TPosition）↔ 限定符（下一节）**，三者说的是同一块存储。

回到样例。矢量加法 Kernel 里，数据只涉及 GM 和 UB 两级——`GlobalTensor` 描述 GM，`LocalTensor` 描述 UB：

- Kernel 入参用 `__gm__` 指针，指向 GM：[add_tpipe_tque.asc:20](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add_tpipe_tque/add_tpipe_tque.asc#L20) 声明 `__global__ __vector__ void add_custom(__gm__ uint8_t* x, __gm__ uint8_t* y, __gm__ uint8_t* z, ...)`。
- `GlobalTensor<float>` 把 GM 地址包成一个对象，并用 `SetGlobalBuffer` 绑定到某段 GM：[add_tpipe_tque.asc:26-36](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add_tpipe_tque/add_tpipe_tque.asc#L26-L36)。
- `LocalTensor<float>` 则是 UB 上的存储对象，由 `TPipe::InitBuffer` 在 UB 上分配：[add_tpipe_tque.asc:38-44](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add_tpipe_tque/add_tpipe_tque.asc#L38-L44)。

这三个类型在 [include/basic_api/kernel_tensor.h](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_tensor.h) 里声明：`GlobalTensor` 见 [kernel_tensor.h:253-254](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_tensor.h#L253-L254)，`LocalTensor` 见 [kernel_tensor.h:146-147](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_tensor.h#L146-L147)，而 `LocalMemAllocator` 的默认硬件就是 UB：[kernel_tensor.h:312-313](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_tensor.h#L312-L313) 写着 `template <Hardware hard = Hardware::UB> class LocalMemAllocator`。

注意：在这份 C++ Tensor 风格的样例里，**UB 的限定符 `__ubuf__` 被 `LocalTensor` 封装藏起来了**——你看不见它，但它确实存在。下一节的 matmul 样例会把这些限定符一次性全部裸写出来。

#### 4.1.4 代码实践

**实践目标**：用官方源码把「物理存储 ↔ 逻辑位置 ↔ 软件类型」三角关系亲手对一遍。

**操作步骤**：

1. 打开 [impl/basic_api/common_types.h:27-47](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/basic_api/common_types.h#L27-L47)，抄下 `TPosition` 的全部枚举值。
2. 打开 [kernel_event.h:369-438](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/basic_api/kernel_event.h#L369-L438)，按 `GetPhyType` 把每个 `TPosition` 映射到 `Hardware`。
3. 打开 [add_tpipe_tque.asc:20-44](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add_tpipe_tque/add_tpipe_tque.asc#L20-L44)，标出每行用到的是 GM 还是 UB。

**需要观察的现象**：`GlobalTensor` 对应的 `TPosition` 是 `GM`；`LocalTensor` 来自 `TQue<VECIN/VECOUT>`，对应的 `TPosition` 是 `VECIN`/`VECOUT`，经 `GetPhyType` 都映射到 `Hardware::UB`。

**预期结果**：你会得到一张三列对照表，形如 `VECIN → UB → LocalTensor`、`GM → GM → GlobalTensor`。本讲只读不改，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Vector 计算单元不能直接从 GM 取数，必须先搬到 UB？

> **参考答案**：Vector 计算单元的硬件数据接口只连接 UB，没有直连 GM 的通路；GM 在片外、延迟高且为所有核共享。必须先由搬运单元（MTE2）把数据从 GM 搬到片上 UB，Vector 才能以低延迟读到。

**练习 2**：`TPosition::A1` 和 `TPosition::A2` 分别对应哪个 `Hardware`？为什么一个矩阵在 L1 和 L0 各有一份副本？

> **参考答案**：`A1 → Hardware::L1`、`A2 → Hardware::L0A`。L1 是容量较大的中转区，先把 GM 上的一大块矩阵搬进来暂存；L0A 是 Cube 单元的私有输入缓冲，容量很小。每次 Cube 计算前，再从 L1 取一小块（按 baseK 切片）推进到 L0A。两级副本是为了在「容量」和「就近计算」之间取得平衡。

---

### 4.2 地址空间限定符

#### 4.2.1 概念说明

既然数据分散在 GM、UB、L1、L0A/L0B/L0C 等不同物理存储里，那么**代码里每声明一个指针或数组，都必须告诉编译器它住在哪一级存储**——否则编译器无法生成正确的 load/store 指令。承担这个职责的就是**地址空间限定符（address space qualifier）**。

它的语法形式是 `__xxx__`，写在被修饰类型的前面，例如 `__gm__ half* a` 表示「指针 `a` 指向 GM 上的 half」。你可以把它类比成 C 语言的 `const`/`volatile`：都是写在类型前、改变编译器对该变量处理方式的修饰符；只不过地址空间限定符改变的是「数据落在哪块物理存储、用哪条搬运通路访问」。

官方文档把 Cube 相关存储与限定符的对应关系列得很清楚（来源 [C语言编程概述.md:130-137](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/docs/zh/guide/编程指南/编程模型/AI-Core-SIMD编程/基于指针的C语言编程/C语言编程概述.md#L130-L137)）：

| 存储单元 | 作用 | 地址限定符 |
| --- | --- | --- |
| **L1 Buffer** | 缓存矩阵计算输入数据 | `__cbuf__` |
| **L0A Buffer** | 存储左矩阵 | `__ca__` |
| **L0B Buffer** | 存储右矩阵 | `__cb__` |
| **L0C Buffer** | 存储计算结果 | `__cc__` |
| Fixpipe Buffer | 存储量化参数 | `__fbuf__` |
| BiasTable Buffer | 存放 Bias 数据 | `__biasbuf__` |

加上矢量与全局内存常用的两个，本讲你需要熟记的**六个核心限定符**汇总如下：

| 限定符 | 物理存储 | 服务对象 | 典型出现位置 |
| --- | --- | --- | --- |
| `__gm__` | Global Memory（片外） | 全核共享、Host 可见 | Kernel 指针入参 |
| `__ubuf__` | Unified Buffer（片上） | Vector / Scalar | 矢量算子的本地缓冲 |
| `__cbuf__` | L1 Buffer（片上） | Cube 中转 | 矩阵算子的 L1 暂存 |
| `__ca__` | L0A Buffer（片上） | Cube 左矩阵输入 | 矩阵算子的 L0A |
| `__cb__` | L0B Buffer（片上） | Cube 右矩阵输入 | 矩阵算子的 L0B |
| `__cc__` | L0C Buffer（片上） | Cube 结果/累加器 | 矩阵算子的 L0C |

#### 4.2.2 核心流程

使用地址空间限定符有几条铁律：

1. **Kernel 的指针入参必须用 `__gm__`**。因为 Host 用 `aclrtMalloc` 申请到的是 GM 地址，传进 Kernel 的指针天然指向 GM（这条规则在 u2-l1 已建立）。
2. **本地缓冲按所属计算单元选限定符**：矢量计算用 `__ubuf__`；矩阵计算的 L1/L0A/L0B/L0C 分别用 `__cbuf__`/`__ca__`/`__cb__`/`__cc__`。
3. **UB 地址必须 32 字节对齐**（来源 [C语言编程概述.md:115](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/docs/zh/guide/编程指南/编程模型/AI-Core-SIMD编程/基于指针的C语言编程/C语言编程概述.md#L115)），目的是匹配硬件总线传输粒度与 SIMD 并行能力。
4. **C++ Tensor 风格会把限定符藏起来**：在 `add_tpipe_tque.asc` 里你看不到 `__ubuf__`，因为它被 `LocalTensor` 封装了；而 C 指针风格（C API）和 Tensor API 的裸数组声明会把限定符直接写出来。

把这四条规则和上一节的存储层级图叠在一起，就能解释一个常见困惑：**为什么同一个概念在不同样例里长得不一样**——因为有的样例用高层封装（`LocalTensor`/`GlobalTensor`），有的样例用裸限定符数组，但底层物理存储是同一套。

#### 4.2.3 源码精读

矩阵乘样例是观察限定符的**最佳样本**，因为它一次性把 Cube 通路四级存储的限定符全部裸写了出来。看 [matmul.asc:48-52](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/02_matrix/matmul_tensor_api/matmul.asc#L48-L52)：

```cpp
__cbuf__ half l1ABuf[baseM * K];      // L1 Buffer，A 矩阵的中转暂存
__cbuf__ half l1BBuf[K * baseN];      // L1 Buffer，B 矩阵的中转暂存
__ca__   half l0ABuf[baseM * baseK];  // L0A Buffer，Cube 左矩阵输入
__cb__   half l0BBuf[baseK * baseN];  // L0B Buffer，Cube 右矩阵输入
__cc__   float l0CBuf[baseM * baseN]; // L0C Buffer，Cube 结果累加器（注意是 float）
```

逐行对照上一节的表：`__cbuf__` → L1、`__ca__` → L0A、`__cb__` → L0B、`__cc__` → L0C。变量命名也诚实——`l1ABuf`/`l0ABuf` 直接把层级写进了名字里。注意 `l0CBuf` 用的是 `float` 而非 `half`：因为 L0C 承担累加，需要更高精度。

再看 Kernel 入参 [matmul.asc:23](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/02_matrix/matmul_tensor_api/matmul.asc#L23)：

```cpp
__global__ __cube__ void matmul_custom(__gm__ half* a, __gm__ half* b, __gm__ half* c)
```

三个指针入参都是 `__gm__`，再次印证「Kernel 入参必为 GM」。而函数限定符是 `__cube__`（跑在 Cube 核上），与矢量加法的 `__vector__` 形成对照——**执行空间限定符（`__vector__`/`__cube__`）决定用哪类核，地址空间限定符（`__gm__`/`__cbuf__`...）决定数据住哪级存储**，两者职责不同，不要混淆（u2-l1 已区分过这两类限定符）。

对照矢量加法样例：[add_tpipe_tque.asc:20](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add_tpipe_tque/add_tpipe_tque.asc#L20) 的入参同样是 `__gm__`，但 Kernel 内部不再裸写 `__ubuf__`，而是改用 `GlobalTensor`/`LocalTensor`（[add_tpipe_tque.asc:26-44](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add_tpipe_tque/add_tpipe_tque.asc#L26-L44)）。`GlobalTensor` 内部持有的就是 `__gm__` 指针——证据在 [kernel_tensor.h:265](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_tensor.h#L265)：`void SetGlobalBuffer(__gm__ PrimType* buffer, uint64_t bufferSize)`，`SetGlobalBuffer` 的入参正是一个 `__gm__` 指针。

> 小结一句：**限定符是底层真相，Tensor 类型是上层封装**。看 C 指针样例（或 matmul 的裸数组）能直接看到限定符；看 C++ Tensor 样例时，要心里有数——`GlobalTensor`= `__gm__`、`LocalTensor`= `__ubuf__`（矢量场景）。

#### 4.2.4 代码实践

**实践目标**：在真实源码里把六个核心限定符逐一找出来并对号入座。

**操作步骤**：

1. 打开 [matmul.asc:48-52](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/02_matrix/matmul_tensor_api/matmul.asc#L48-L52)，为 `__cbuf__`/`__ca__`/`__cb__`/`__cc__` 四个声明各写一行「→ 某级存储」。
2. 在同一文件里搜索 `__gm__`（[matmul.asc:23](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/02_matrix/matmul_tensor_api/matmul.asc#L23)），确认它只出现在 Kernel 指针入参处。
3. 打开 [add_tpipe_tque.asc:20-44](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add_tpipe_tque/add_tpipe_tque.asc#L20-L44)，确认矢量样例里 `__gm__` 同样只在入参出现，UB 存储被 `LocalTensor` 封装隐藏。

**需要观察的现象**：matmul 样例「限定符种类多但都是裸写」；add 样例「限定符种类少（只有 `__gm__`）但 UB 被封装」。两种风格描述的是同一套物理存储。

**预期结果**：得到一张「限定符 → 存储单元 → 出现在哪个样例」的对照表。本实践为阅读型，无需编译运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Kernel 的指针入参必须是 `__gm__`，而不能写成 `__ubuf__`？

> **参考答案**：Host 用 `aclrtMalloc` 申请的 Device 内存地址是 GM（片外全局内存）地址，Kernel 通过指针入参接收的就是这个 GM 地址。把它标成 `__ubuf__` 会让编译器误以为指针指向片上 UB，生成错误的访问指令。所以入参必须如实标注 `__gm__`，进入 Kernel 后再把数据搬运到 UB/L1 等片上存储。

**练习 2**：在 `matmul.asc` 中，`__cc__ float l0CBuf[...]` 为什么用 `float` 而其它缓冲用 `half`？

> **参考答案**：L0C（`__cc__`）是 Cube 单元的累加器，需要在 K 轴上反复累加多个部分积。累加对精度敏感，因此用 `float`（FP32）存放中间结果以避免误差累积；而 L1/L0A/L0B 存放的是原始输入矩阵，沿用输入的 `half`（FP16）即可节省存储与带宽。结果最终经 FixPipe 搬回 GM 时再做类型转换。

**练习 3**：下列说法是否正确：「`__vector__` 和 `__ubuf__` 是一回事，都表示矢量相关」。

> **参考答案**：错误。`__vector__` 是**执行空间限定符**（函数限定符），说明 Kernel 跑在 Vector 核上；`__ubuf__` 是**地址空间限定符**，说明数据住在 Unified Buffer 这级存储。两者维度不同：前者管「在哪类核上执行」，后者管「数据住在哪级存储」。只不过矢量算子通常既跑在 Vector 核上、又把数据放在 UB，所以经常成对出现，容易让人误以为是同义词。

---

### 4.3 Vector 与 Cube 数据通路

#### 4.3.1 概念说明

把前两节合起来：**存储层级决定了「数据住在哪」，地址空间限定符是「在代码里声明住址」，而数据通路（data path）则是「数据沿着哪条搬运路线从外存流到计算单元、再流回去」**。

AI Core 有两类计算单元，于是有两条风格迥异的通路：

- **Vector 通路**：短而简单，只穿越 GM 和 UB 两级。
- **Cube 通路**：长而分层，要穿越 GM → L1 → L0A/L0B → L0C → GM 多级。

官方文档《基本架构》给出的典型数据流是权威表述（来源 [基本架构.md:208-221](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/docs/zh/guide/编程指南/高级编程/硬件实现/基本架构.md#L208-L221)）：

```
Vector 典型数据流：GM → UB → Vector → UB → GM
Cube  典型数据流：GM → L1 → L0A/L0B → Cube → L0C → FixPipe → GM
```

通路的「长度」直接决定 Kernel 代码里搬运指令的数量：矢量加法只需「搬入—计算—搬出」三步；矩阵乘则要在多级存储间反复搬运。这不是 API 设计者的偏好，而是硬件物理通路决定的——**你写的搬运代码，本质是在指挥 MTE2/MTE1/FixPipe 这些 DMA 引擎沿物理通路送料**。

#### 4.3.2 核心流程

两条通路的搬运序列对照如下（方括号内为负责该步的搬运单元）：

```
【Vector 通路】（以矢量加法 z = x + y 为例）
  GM(x), GM(y)
    │  DataCopy  [MTE2: GM→UB]
    ▼
  UB(xLocal), UB(yLocal)
    │  Add       [Vector 计算]
    ▼
  UB(zLocal)
    │  DataCopy  [MTE3: UB→GM]
    ▼
  GM(z)

【Cube 通路】（以矩阵乘 C = A × B 为例）
  GM(A), GM(B)
    │  Copy(GM2L1)   [MTE2: GM→L1]
    ▼
  L1(l1ABuf), L1(l1BBuf)
    │  Copy(L1→L0A/L0B)  [MTE1: L1→L0]   ← 按 K 轴分块循环
    ▼
  L0A(l0ABuf), L0B(l0BBuf)
    │  Mmad            [Cube 累加]
    ▼
  L0C(l0CBuf)
    │  Copy(L0C→GM)    [FixPipe: L0C→GM]
    ▼
  GM(C)
```

两个要点：

1. **Cube 通路在 K 轴上有循环**：矩阵乘的 K 维很大，L0A/L0B 装不下整条 K，所以要把 K 切成多块（`baseK`），每块「L1→L0A/L0B→Cube 累加」一次，多次累加结果都汇入同一块 L0C。这就是源码里 `kLoop` 循环的来历。
2. **两条通路的搬运单元不同**：Vector 用 MTE2/MTE3，Cube 用 MTE2/MTE1/FixPipe。代码里它们表现为不同的搬运接口（`DataCopy` vs `CopyGM2L1`/`CopyL12L0A`/`CopyL0C2GM`）。

#### 4.3.3 源码精读

先看 Vector 通路在 `add_tpipe_tque.asc` 里的完整落地，正好三步：

1. **搬入 GM→UB**：[add_tpipe_tque.asc:45-46](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add_tpipe_tque/add_tpipe_tque.asc#L45-L46) 调用 `AscendC::DataCopy(xLocal, xGm, blockLength)`，把 GM 的 `xGm`/`yGm` 搬到 UB 的 `xLocal`/`yLocal`。
2. **UB 内计算**：[add_tpipe_tque.asc:54](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add_tpipe_tque/add_tpipe_tque.asc#L54) 调用 `AscendC::Add(zLocal, xLocal, yLocal, blockLength)`，Vector 单元在 UB 上完成加法。
3. **搬出 UB→GM**：[add_tpipe_tque.asc:61](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add_tpipe_tque/add_tpipe_tque.asc#L61) 调用 `AscendC::DataCopy(zGm, zLocal, blockLength)`，把结果从 UB 写回 GM。

三步严格对应「GM → UB → Vector → UB → GM」，是 Vector 通路最干净的样板。

再看 Cube 通路在 `matmul.asc` 里的落地。它先用 `MakeCopy` 把每种搬运单元封装成一个「搬运原子（atom）」，见 [matmul.asc:60-64](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/02_matrix/matmul_tensor_api/matmul.asc#L60-L64)：

```cpp
auto copyGM2L1Atom   = MakeCopy(CopyGM2L1{},   ...);  // GM → L1   (MTE2)
auto copyL12L0AAtom  = MakeCopy(CopyL12L0A{},  ...);  // L1 → L0A  (MTE1)
auto copyL12L0BAtom  = MakeCopy(CopyL12L0B{},  ...);  // L1 → L0B  (MTE1)
auto copyL0C2GMAtom  = MakeCopy(CopyL0C2GM{},  ...);  // L0C → GM  (FixPipe)
auto mmadAtom        = MakeMmad(MmadOperation{}, ...); // L0A·L0B → L0C (Cube)
```

这些 atom 的名字已经把「源级 → 目标级」写明。随后就是按通路逐段搬运：

- **GM → L1**（一次性搬入整块 A/B）：[matmul.asc:74-75](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/02_matrix/matmul_tensor_api/matmul.asc#L74-L75) 调用 `Copy(copyGM2L1Atom, l1ATensor, globalA)` 与 `...l1BTensor, globalB)`。
- **K 轴循环：L1 → L0A/L0B → Cube 累加**：[matmul.asc:79-91](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/02_matrix/matmul_tensor_api/matmul.asc#L79-L91)，循环 `kLoop = K / baseK` 次，每次先 `Copy(copyL12L0AAtom, l0ATensor, l1ATensor.Slice(...))` 把一块 K 推进到 L0A/L0B，再 `Mmad(mmadAtom.with(para), l0CTensor, l0ATensor, l0BTensor)` 累加进 L0C。
- **L0C → GM**（搬出结果）：[matmul.asc:95-96](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/02_matrix/matmul_tensor_api/matmul.asc#L95-L96) 调用 `Copy(copyL0C2GMAtom, globalC, l0CTensor)`。

> 关于 `Mutex::Lock/Unlock`：matmul 里这些成对的锁（如 [matmul.asc:71-97](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/02_matrix/matmul_tensor_api/matmul.asc#L71-L97)）是为了让多条并行搬运/计算流水线不抢占同一块 L1/L0C。它们的原理属于 u7-l1「同步机制」，本讲只需把它当成「搬运代码周边的保护壳」，不影响你理解数据沿哪条通路走。

两个样例放在一起，最能体现「通路长度决定代码长度」：add 的搬运只有两行 `DataCopy`；matmul 的搬运横跨四级存储、外加 K 轴循环。**差异的根源不是写法，而是 Vector 与 Cube 两条物理通路本身的层级深度不同**。

#### 4.3.4 代码实践

**实践目标**：亲手把两个 Kernel 的搬运路径「翻译」成 4.3.2 那样的通路草图，把抽象通路钉到具体代码行。

**操作步骤**：

1. 打开 [add_tpipe_tque.asc](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add_tpipe_tque/add_tpipe_tque.asc)，找到第 45、54、61 行，在每一行旁标注「GM→UB / Vector 计算 / UB→GM」。
2. 打开 [matmul.asc](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/02_matrix/matmul_tensor_api/matmul.asc)，找到第 74、82-83、88、96 行，分别标注「GM→L1 / L1→L0A·L0B / Cube 累加 / L0C→GM」。
3. 数一数两个 Kernel 各自的搬运段数，验证「Cube 通路比 Vector 通路多穿两级存储」。

**需要观察的现象**：add 的搬运集中在连续几行、无循环；matmul 的 L1→L0→Cube 被包在 `for (kIter ...)` 循环里，因为 K 维必须分块多次推进。

**预期结果**：得到两张带行号的通路草图。若无法在本地运行算子，明确标注「待本地验证」即可——本实践是源码阅读型，重在把通路与代码行对上号。

#### 4.3.5 小练习与答案

**练习 1**：为什么矩阵乘 Kernel 里 L1→L0A/L0B→Cube 这一段要放在 `for` 循环里，而矢量加法的 GM→UB→Add 不需要循环？

> **参考答案**：矩阵乘的 K 维通常很大，L0A/L0B 容量很小，装不下整条 K，所以必须按 `baseK` 分块，每次把一小块从 L1 推进到 L0A/L0B、做一次 Cube 累加，循环 `K/baseK` 次才能算完整条 K。矢量加法是逐元素运算，没有需要累加的长维度，一次 `DataCopy` 把整个分片搬进 UB、一次 `Add` 即可完成，因此不需要循环。

**练习 2**：`CopyGM2L1`、`CopyL12L0A`、`CopyL0C2GM` 这三个搬运 atom 分别对应哪个搬运单元（MTE2/MTE1/FixPipe）？

> **参考答案**：`CopyGM2L1` 对应 **MTE2**（GM→L1 通路）；`CopyL12L0A` 对应 **MTE1**（L1→L0A/L0B 通路）；`CopyL0C2GM` 对应 **FixPipe**（L0C→GM 通路，可随路类型转换）。命名「源级+目标级」与搬运单元职责一一对应。

**练习 3**：如果把矢量加法的结果写回改成「先搬到一个 L1，再从 L1 搬回 GM」，会更快吗？

> **参考答案**：不会，反而更慢且无意义。Vector 计算的结果天然落在 UB，回 GM 的物理通路就是 UB→GM（MTE3），直连最短。强行绕道 L1 既没有对应的性能收益（L1 服务于 Cube，不是 Vector 的回写通路），又徒增一次搬运开销。**顺着物理通路编程、不要无故绕路**是 Ascend C 的基本原则。

---

## 5. 综合实践

把三个最小模块串起来，完成本讲规格里的综合任务：**对照 `add_tpipe_tque.asc` 与 `matmul.asc`，列出每个 Kernel 用到的地址空间及其所在内存层级，画出各自的数据搬运路径**。

具体做法：

1. **建一张「地址空间清单」**。为两个 Kernel 各列一张三列表：`限定符/Tensor 类型 | 物理存储（GM/UB/L1/L0A/L0B/L0C）| 对应代码行`。
   - `add_tpipe_tque.asc` 至少应包含：`__gm__` 入参（[L20](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add_tpipe_tque/add_tpipe_tque.asc#L20)）、`GlobalTensor`→GM（[L26-36](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add_tpipe_tque/add_tpipe_tque.asc#L26-L36)）、`LocalTensor`→UB（[L38-44](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add_tpipe_tque/add_tpipe_tque.asc#L38-L44)）。
   - `matmul.asc` 至少应包含：`__gm__` 入参（[L23](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/02_matrix/matmul_tensor_api/matmul.asc#L23)）、`__cbuf__`→L1、`__ca__`→L0A、`__cb__`→L0B、`__cc__`→L0C（[L48-52](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/02_matrix/matmul_tensor_api/matmul.asc#L48-L52)）。
2. **画两张搬运路径图**。参照 4.3.2 的样式，把每个搬运接口（`DataCopy` / `Copy(...)` / `Mmad`）画成箭头，节点是存储层级。注意 matmul 要画出 K 轴的循环回环。
3. **写一句对比结论**。例如：「add 只用 GM↔UB 两级、3 步搬运；matmul 贯穿 GM→L1→L0A/L0B→L0C→GM 五级、外加 K 轴循环，差异源于 Vector 与 Cube 两条物理通路的层级深度不同。」

> 自检：如果你的两张图里，`add` 的节点只有 GM 和 UB 两个、`matmul` 的节点出现了 L1/L0A/L0B/L0C，说明你已经把本讲的「通路」核心吃透了。本实践无需编译运行，是纯源码阅读型任务；如要进一步上板验证搬运行为，可结合 u12-l1（调试能力）的 `DumpTensor` 观察各级存储的实际数据。

## 6. 本讲小结

- AI Core 的存储是**多级层次结构**：片外 GM（大而慢、全核共享）→ 片上 L1/UB（每核独享、中转）→ 计算单元私有 L0A/L0B/L0C（小而快）。容量与速度此消彼长。
- **Vector 单元只接 UB、Cube 单元只接 L0A/L0B/L0C**，这条硬约束决定了两条不同的数据通路。
- **地址空间限定符**是「在代码里声明数据住哪级存储」：`__gm__`→GM、`__ubuf__`→UB、`__cbuf__`→L1、`__ca__`→L0A、`__cb__`→L0B、`__cc__`→L0C（另有 `__fbuf__`/`__biasbuf__`）。
- 软件用 **`Hardware`（物理）/ `TPosition`（逻辑）/ 限定符** 三套等价命名指代同一块存储，`GetPhyType` 是它们的桥；编号 `1` 表 L1 层、`2` 表 L0 层。
- **Vector 通路** = `GM→UB→Vector→UB→GM`（短，MTE2/MTE3）；**Cube 通路** = `GM→L1→L0A/L0B→Cube→L0C→GM`（长，MTE2/MTE1/FixPipe，K 轴分块循环）。
- 在 C++ Tensor 风格里限定符被 `GlobalTensor`/`LocalTensor` 封装隐藏，在 C 指针风格与 Tensor API 裸数组里则直接写出来——**底层物理存储是同一套**。
- 别混淆两类限定符：`__vector__`/`__cube__` 是执行空间（在哪类核上跑），`__gm__`/`__ubuf__`/... 是地址空间（数据住哪级存储）。

## 7. 下一步学习建议

本讲建立了「数据住在哪里、走哪条路」的地理认知，但故意把搬运接口和数据结构的细节当黑盒。建议按这个顺序继续：

1. **u3-l2 GlobalTensor 与 LocalTensor 数据结构**：打开本讲反复出现的 `GlobalTensor`/`LocalTensor`，看 `SetGlobalBuffer`/`GetSize`/`GetValue` 等方法如何把 GM/UB 地址包成可操作的对象。
2. **u3-l3 DataCopy 搬运与 LocalMemAllocator 自主内存管理**：深入本讲里 `DataCopy(xLocal, xGm, ...)` 这个调用，理解 GM↔UB 搬运接口的参数与对齐约束，并学习用 `LocalMemAllocator` 自主分配 UB。
3. **u5-l1 TPipe/TQue 框架**：本讲 add 样例用的 `TPipe`/`TQue`/`AllocTensor`/`EnQue`/`DeQue` 是框架式内存管理，可对照 u3-l3 的自主式管理，理解两种范式。
4. **进阶阅读**：想深入 UB 的内部划分（静态/动态内存、Data Cache）与缓存一致性，可读官方文档 [内存层级.md](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/docs/zh/guide/编程指南/高级编程/高级AI-Core编程模型/SIMD与SIMT混合编程/内存层级.md)；想看 Cube 分形布局（NZ/ZN）如何影响 L1/L0 排布，可留到 u11-l1「Cube 计算单元、分形与 L1/L0 内存层级」。
