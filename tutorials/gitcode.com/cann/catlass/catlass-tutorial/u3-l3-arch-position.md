# 硬件架构抽象层 Arch 与 Position

## 1. 本讲目标

CATLASS 要在两代昇腾硬件（AtlasA2 / Ascend950）上跑同一套上层算子逻辑，又要针对每代硬件的容量与指令差异做底层特化。完成本讲后，你应该能够：

- 读懂 `arch.hpp` 里 `AtlasA2`、`Ascend950` 两个结构体承载的存储容量常量，并能口算某块 Tile 是否放得下 L0C/L1；
- 说出 `PositionGM / PositionL1 / PositionL0A / PositionL0B / PositionL0C / PositionUB` 这些位置标签分别对应哪一层存储、为什么用空类型（`std::integral_constant`）实现；
- 分清「`CATLASS_ARCH` 预处理宏」与「`Arch::AtlasA2/Ascend950` C++ 类型（ArchTag）」两条**并行**的架构特化通道，以及它们各自驱动什么。

## 2. 前置知识

本讲假设你已经建立下面两块认知（分别来自 u1-l2 与 u3-l1）：

- **存储层级与数据通路**：一颗 AI Core 内数据沿 `GM → L1 → L0A/L0B → L0C → UB` 逐层内移，越内层容量越小、速度越快。`L0C` 是矩阵乘的累加器，按 `fp32`（4 字节）累加；`UB`（Unified Buffer）是向量核做激活、量化等后处理的地方。
- **Layout 是寻址地基**：`Layout` 只存 `shape/stride`，把逻辑坐标映射成线性偏移，本身不持有数据。

本讲要做的事情，是给这些存储层**起统一的类型名**（Position 标签），并把**每一层的容量上限**固化为编译期常量，从而让 TileShape 选择、搬运组件特化都能在编译期查表完成。

> 一个关键直觉：CATLASS 几乎所有「架构差异」都被压在编译期。运行时不会去判断「我现在在 A2 还是 950」，而是在编译时就由宏和类型把对的那一份代码选出来。本讲的两个主角——**容量常量**和 **Position 标签**——都是这个「编译期决定」机制的零件。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`include/catlass/arch/arch.hpp`](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/arch/arch.hpp) | 定义 `AtlasA2`、`Ascend950` 两个架构结构体（容量常量）和 `PositionGM...PositionUB` 等位置标签。本讲核心。 |
| [`include/catlass/catlass.hpp`](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/catlass.hpp) | 仓库最底层公共头。用 `CATLASS_ARCH` 宏 `#if` 分支给出架构相关常量（如 `BYTE_PER_BLK_FP`、MX 量化常量）。 |
| [`include/catlass/gemm/gemm_type.hpp`](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/gemm_type.hpp) | `GemmType` 把 `Element + Layout + Position` 打包，说明 Position 标签如何进入类型链路。 |
| [`include/catlass/gemm/dispatch_policy.hpp`](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/dispatch_policy.hpp) | 用 `Arch::AtlasA2` 作为 ArchTag 组装各种 DispatchPolicy，并有编译期 `static_assert` 限制某些策略只能用于特定架构。 |
| [`CMakeLists.txt`](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/CMakeLists.txt) / [`examples/CMakeLists.txt`](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/CMakeLists.txt) | 把 CMake 变量 `CATLASS_ARCH` 同时转成编译器 `--npu-arch` 标志和 C++ 预处理宏。 |

---

## 4. 核心概念与源码讲解

### 4.1 架构容量常量：AtlasA2 与 Ascend950

#### 4.1.1 概念说明

矩阵乘算子的性能，几乎取决于「一块 Tile 能否塞进片上缓存」：塞得进就能反复复用、减少对 GM 的读取；塞不进就得拆得更碎、搬运更频繁。因此每一代硬件的**各级存储容量上限**，是 TileShape 选择的硬约束。

CATLASS 的做法是：为每一代硬件定义一个**纯静态结构体**，把 `L1_SIZE / L0A_SIZE / L0B_SIZE / L0C_SIZE / UB_SIZE / FIXBUF_SIZE / BIAS_SIZE` 全部写成 `static constexpr` 常量。这些常量：

- **零运行期开销**：`constexpr` 在编译期就求值，不会生成任何指令或变量。
- **可被模板直接读取**：`Arch::AtlasA2::L0C_SIZE` 能直接出现在 `static_assert` 或容量检查模板里。

> 这个结构体本身不存任何数据，它只是一个「架构档案（profile）」，把硬件规格搬进类型系统，供后续模板查表。

#### 4.1.2 核心流程

架构档案的用法是一条单向链路：

1. **编译期选定架构**：构建时通过 `CATLASS_ARCH`（详见 4.3）与上层组装时的 `ArchTag` 决定用 `AtlasA2` 还是 `Ascend950`。
2. **容量常量参与校验**：TileShape 选择器、`TileShapeAlignChecker` 等模板读取 `ArchTag::L0C_SIZE` 等常量，判断一块 Tile 是否合法。
3. **两代硬件的关键差异**主要落在三处：`L0C`（128KB→256KB，翻倍）、`UB`（192KB→248KB）、`FIXBUF`（7KB→16KB）、`BIAS`（1KB→4KB）。其中 **L0C 翻倍**对 L0TileShape 选择影响最大（见 4.1.4）。

两代架构的容量对照表（单位均为字节，源码即按字节定义）：

| 常量 | AtlasA2 | Ascend950 | 说明 |
| --- | --- | --- | --- |
| `L1_SIZE` | 512 KB | 512 KB | L1 缓存，两代相同 |
| `L0A_SIZE` | 64 KB | 64 KB | L0A，两代相同 |
| `L0B_SIZE` | 64 KB | 64 KB | L0B，两代相同 |
| `L0C_SIZE` | **128 KB** | **256 KB** | 累加器，**翻倍** |
| `UB_SIZE` | 192 KB | 248 KB | 向量核缓冲，增大 |
| `FIXBUF_SIZE` | 7 KB | 16 KB | FixPipe 缓冲，增大 |
| `BIAS_SIZE` | 1 KB | 4 KB | Bias 缓冲，增大 |

#### 4.1.3 源码精读

`AtlasA2` 的容量常量定义在 [include/catlass/arch/arch.hpp:18-26](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/arch/arch.hpp#L18-L26)，每个常量都用 `* 1024` 表达「KB→字节」，意图一目了然：

```cpp
struct AtlasA2 {
    static constexpr uint32_t BIAS_SIZE   = 1024;        // 1 KB
    static constexpr uint32_t FIXBUF_SIZE = 7 * 1024;    // 7 KB
    static constexpr uint32_t UB_SIZE     = 192 * 1024;  // 192 KB
    static constexpr uint32_t L1_SIZE     = 512 * 1024;  // 512 KB
    static constexpr uint32_t L0A_SIZE    = 64 * 1024;   // 64 KB
    static constexpr uint32_t L0B_SIZE    = 64 * 1024;   // 64 KB
    static constexpr uint32_t L0C_SIZE    = 128 * 1024;  // 128 KB
};
```

`Ascend950` 结构体与之同构，仅数值不同，见 [include/catlass/arch/arch.hpp:29-37](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/arch/arch.hpp#L29-L37)：

```cpp
struct Ascend950 {
    static constexpr uint32_t BIAS_SIZE   = 4 * 1024;    // 4 KB
    static constexpr uint32_t FIXBUF_SIZE = 16 * 1024;   // 16 KB
    static constexpr uint32_t UB_SIZE     = 248 * 1024;  // 248 KB
    static constexpr uint32_t L1_SIZE     = 512 * 1024;  // 512 KB
    static constexpr uint32_t L0A_SIZE    = 64 * 1024;   // 64 KB
    static constexpr uint32_t L0B_SIZE    = 64 * 1024;   // 64 KB
    static constexpr uint32_t L0C_SIZE    = 256 * 1024;  // 256 KB
};
```

这两个结构体都位于 `namespace Catlass::Arch`（见 [arch.hpp:16](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/arch/arch.hpp#L16)），因此下游代码以 `Arch::AtlasA2`、`Arch::Ascend950` 引用它们，并把它们当作 **ArchTag** 在模板链路里传递——例如 [dispatch_policy.hpp:27](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/dispatch_policy.hpp#L27) 就把 `Arch::AtlasA2` 作为 `MmadBase` 的模板参数：

```cpp
using MmadAtlasA2 = MmadBase<Arch::AtlasA2, false>;
```

#### 4.1.4 代码实践

**实践目标**：用 `L0C_SIZE` 口算 L0TileShape 的容量上限，体会 L0C 翻倍（128KB→256KB）对 Tile 选择的实际影响。

**操作步骤**：

1. L0C 按 fp32（4 字节）累加，所以一块 L0Tile 的 C 占用为 \(\,M \times N \times 4\,\) 字节。
2. AtlasA2 的 L0C 容量为 \(\,128 \times 1024 = 131072\,\) 字节，最多容纳 \(\,131072 / 4 = 32768\,\) 个 fp32 元素。
3. Ascend950 的 L0C 容量为 \(\,256 \times 1024 = 262144\,\) 字节，最多容纳 \(\,262144 / 4 = 65536\,\) 个 fp32 元素。
4. 以常见的 **pingpong 双缓冲**（两份 L0C 缓冲交替）为例，实际可用容量要除以 2：
   - AtlasA2：\(\,M \times N \le 16384\,\)，例如 `L0TileShape(128, 128)`（\(=16384\)）刚好占满；
   - Ascend950：\(\,M \times N \le 32768\,\)，例如 `L0TileShape(128, 256)`（\(=32768\)）刚好占满。

**需要观察的现象**：同样想在 pingpong 下塞一块 `L0TileShape(128, 256)` 的 C——

- 在 AtlasA2 上：\(128 \times 256 \times 4 \times 2 = 262144\) 字节 \(> 131072\) 字节，**放不下**，必须把 N 减半或放弃双缓冲；
- 在 Ascend950 上：恰好 \(= 262144\) 字节，**放得下**。

**预期结果**：正是因为 L0C 翻倍，Ascend950 上可以选用更大的 L0Tile N 维（更多并发、更高吞吐）或在同等 Tile 下免费启用双缓冲隐藏搬运延迟。这就是「L0C 翻倍 → L0TileShape 选择变宽」的因果链。

> 待本地验证：以上为按容量公式推演。若要确认某样例实际选用的 L0TileShape，可打开对应样例（如 `examples/00_basic_matmul`）查看其 `L0TileShape` 别名并与本节公式对照。

#### 4.1.5 小练习与答案

**练习 1**：AtlasA2 的 L1 容量是 512KB。若 A、B 都用 fp16（2 字节），单核想同时常驻一块 `L1Tile(128, 256, 256)` 的 A 和一块 `L1Tile(128, 256, 256)` 的 B，L1 放得下吗？（提示：L1Tile 的 k 维是搬运批次，A 占 \(M \times K\)、B 占 \(K \times N\)。）

**答案**：A 占 \(128 \times 256 \times 2 = 65536\) 字节，B 占 \(256 \times 256 \times 2 = 131072\) 字节，合计 \(196608\) 字节 \(= 192\text{KB} < 512\text{KB}\)，**放得下**（还有余量做 pingpong 多缓冲）。

**练习 2**：为什么这些容量用 `static constexpr uint32_t` 而不是普通 `const` 或宏？

**答案**：`static constexpr` 既是真正的编译期常量（可作模板参数、可进 `static_assert`、不占运行期存储），又受类型系统与作用域约束（比宏安全、可调试）。宏没有类型与作用域保护，普通 `const` 不保证编译期求值。

---

### 4.2 Position 位置标签：给存储层起类型名

#### 4.2.1 概念说明

数据在算子里是「流动」的：同一块矩阵，刚从 GM 搬进来时在 GM、搬进 L1 后在 L1、再搬进 L0A 后在 L0A……它在**不同存储层需要不同的布局**（GM 朴素排布、L1/L0C 按分形排布，见 u3-l1）。为了让模板代码能「按所处存储层」选择正确的搬运指令和布局，CATLASS 给每一层存储起了一个**编译期类型标签**，这就是 Position。

Position 标签的关键性质：**它是一个空类型（empty type）**，不携带任何运行期数据，只用作模板参数/函数参数里的「位置占位符」，让编译器在编译期把「这块数据现在在哪一层」这个信息编进类型。

#### 4.2.2 核心流程

Position 的实现依赖 C++ 的 `std::integral_constant`：

1. `PositionType<POS>` 把一个 `AscendC::TPosition` 枚举值包成 `std::integral_constant<AscendC::TPosition, POS>`——一个只有类型、大小为 0 的空类。
2. 再为每一层起一个易读的别名：`PositionGM`、`PositionL1`、`PositionL0A` ……
3. 这些别名有两种用法：
   - **作为类型/标签实参**：如 `tla::MakeTensor(gm, layout, Arch::PositionGM{})`，告诉模板「这个 Tensor 的数据落在 GM」；
   - **作为枚举值存进 GemmType**：`GemmType` 的 `POSITION` 字段存的就是 `AscendC::TPosition` 枚举值。

CATLASS 的 6 个主位置标签（外加 `PositionBias`）与存储层、AscendC 原始枚举值的对照：

| Position 别名 | `AscendC::TPosition` 值 | 对应存储层 | 典型用途 |
| --- | --- | --- | --- |
| `PositionGM` | `GM` | Global Memory（显存） | 输入/输出矩阵常驻 |
| `PositionL1` | `A1` | L1 缓存 | GM→L1 搬运后的中转 |
| `PositionL0A` | `A2` | L0A 缓存 | 矩阵 A 进入 Mmad 前 |
| `PositionL0B` | `B2` | L0B 缓存 | 矩阵 B 进入 Mmad 前 |
| `PositionL0C` | `CO1` | L0C 累加器 | Mmad 的累加结果 |
| `PositionUB` | `VECCALC` | Unified Buffer | 向量核激活/量化后处理 |

> 第 7 个 `PositionBias`（对应 `C2`）专门给 bias 用，本讲主题里的「6 个」指上表这 6 个主存储层。

#### 4.2.3 源码精读

Position 机制的核心是这两段，见 [include/catlass/arch/arch.hpp:39-48](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/arch/arch.hpp#L39-L48)：

```cpp
template <AscendC::TPosition POS>
using PositionType = std::integral_constant<AscendC::TPosition, POS>;

using PositionGM   = PositionType<AscendC::TPosition::GM>;
using PositionL1   = PositionType<AscendC::TPosition::A1>;
using PositionL0A  = PositionType<AscendC::TPosition::A2>;
using PositionL0B  = PositionType<AscendC::TPosition::B2>;
using PositionL0C  = PositionType<AscendC::TPosition::CO1>;
using PositionBias = PositionType<AscendC::TPosition::C2>;
using PositionUB   = PositionType<AscendC::TPosition::VECCALC>;
```

- `PositionType` 是「把枚举值提升为类型」的标准技巧（`std::integral_constant`），其 `.value` 即原始枚举；
- 每个别名绑死一层存储，名字比裸枚举值（`A1/A2/B2/CO1/VECCALC`）直观得多。

Position 进入类型链路的方式见 [include/catlass/gemm/gemm_type.hpp:20-25](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/gemm_type.hpp#L20-L25)：

```cpp
template <class Element_, class Layout_, AscendC::TPosition POSITION_ = AscendC::TPosition::GM>
struct GemmType {
    using Element = Element_;
    using Layout = Layout_;
    static constexpr AscendC::TPosition POSITION = POSITION_;
};
```

注意 `GemmType` 默认 `POSITION = GM`——这正是「输入矩阵一开始都在 GM」的体现；当数据被搬运到 L1/L0 时，类型选择器会产出带新 `POSITION` 的 `GemmType`（同一份数据在不同层有不同 Position）。

在 kernel 代码里，Position 标签被当作实参传给 `MakeTensor`，明确这块 Tensor 落在哪一层。例如 [include/catlass/gemm/kernel/basic_matmul_tla_ub_visitor.hpp:150-151](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul_tla_ub_visitor.hpp#L150-L151)：

```cpp
auto tensorA = tla::MakeTensor(gmA, params.layoutA, Arch::PositionGM{});
auto tensorB = tla::MakeTensor(gmB, params.layoutB, Arch::PositionGM{});
```

这里 `Arch::PositionGM{}` 构造了一个空对象，仅用于把「在 GM」这个信息编进 `tensorA/tensorB` 的类型。

#### 4.2.4 代码实践

**实践目标**：通过追踪一处真实的 Position 标签使用，理解「空类型标签如何把位置信息编进类型」。

**操作步骤**：

1. 打开 [basic_matmul_tla_ub_visitor.hpp:150-153](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul_tla_ub_visitor.hpp#L150-L153)，找到 `tensorA`、`tensorB`、`tensorBias` 三处 `MakeTensor` 调用。
2. 观察它们都传了 `Arch::PositionGM{}`——说明输入 A/B/Bias 都来自 GM。
3. 在同文件后续代码中搜索把数据搬进 L1/UB 后构造的 Tensor，看它的 Position 实参是否变成了 `PositionL1{}` 或 `PositionUB{}`。

**需要观察的现象**：同一份逻辑数据（如矩阵 A）在 GM 和 L1 上分别用不同 Position 标签的 Tensor 表示，二者**类型不同**、不能直接混用——这正是 Position 标签的安全价值：编译器能在编译期阻止「把 GM Tensor 当成 L1 Tensor 用」。

**预期结果**：你能指出至少两处 `MakeTensor` 调用，其第三参数分别是 `PositionGM{}` 与某个内层 Position，且对应「搬运前/搬运后」两个阶段。

> 待本地验证：具体每段 kernel 中内层 Tensor 用的 Position 取决于该算子的数据通路；以你打开的文件实际代码为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `PositionGM` 用 `std::integral_constant` 实现，而不是直接 `using PositionGM = AscendC::TPosition::GM;`？

**答案**：`AscendC::TPosition::GM` 是一个**枚举值**（右值），不能直接当类型用；`std::integral_constant<...>` 把它包成一个**类型**，既可以实例化为空对象当函数实参（`PositionGM{}`），又能用 `.value` 取回枚举值，是「值→类型」提升的标准做法。

**练习 2**：说出矩阵 A 从 GM 一路搬进 Mmad 指令所经过的 Position 序列。

**答案**：`PositionGM`（GM 原始数据）→ `PositionL1`（GM→L1 后）→ `PositionL0A`（L1→L0A 后），随后进入 `AscendC::Mmad`，结果落到 `PositionL0C`。

---

### 4.3 CATLASS_ARCH 宏：驱动架构特化的「另一条」通道

#### 4.3.1 概念说明

初学者最容易踩的坑：以为「选 A2 还是 950」只有一种机制。实际上 CATLASS 有**两条并行的架构特化通道**，分工不同：

1. **`CATLASS_ARCH` 预处理宏**：来自 CMake 变量，是 `#if` 预处理分支的依据，负责**与编译器行为绑定、与底层标量常量绑定**的特化（如某个搬运块大小 `BYTE_PER_BLK_FP`、是否启用 MX 量化常量）。
2. **`Arch::AtlasA2 / Ascend950` C++ 类型（ArchTag）**：是 4.1 讲的容量档案，走模板/类型系统，负责**容量校验、DispatchPolicy 选择、搬运组件特化路由**。

二者是**平行存在**的：构建时它们一起被选定（`2201`↔`AtlasA2`、`3510`↔`Ascend950`），但分别用预处理和类型系统两条路落地。理解这点，才能看懂为什么有的差异在 `#if` 里、有的差异在模板参数里。

#### 4.3.2 核心流程

`CATLASS_ARCH` 从构建命令到源码常量的链路：

1. **CMake 变量**：用户用 `-DCATLASS_ARCH=2201`（或 `3510`）指定；未指定时默认 `2201`。
2. **同时转成两样东西**（关键一步，见 4.3.3 源码）：
   - 编译器架构标志 `--npu-arch=dav-${CATLASS_ARCH}` → 决定 ASC 编译器按哪代硬件的指令集/约束编译；
   - C++ 预处理宏 `CATLASS_ARCH=<值>` → 注入到源码，供 `#if` 分支使用。
3. **源码 `#if` 分支**：`catlass.hpp` 据 `CATLASS_ARCH` 选出对应的架构相关常量。

#### 4.3.3 源码精读

先看链路起点——CMake 默认值，见 [CMakeLists.txt:46-49](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/CMakeLists.txt#L46-L49)：

```cmake
if(NOT DEFINED CATLASS_ARCH OR NOT CATLASS_ARCH)
    message(WARNING "CATLASS_ARCH is not defined, use default value \"2201\"")
    set(CATLASS_ARCH 2201)
endif()
```

再看「一变两用」的关键两行，见 [examples/CMakeLists.txt:15-16](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/CMakeLists.txt#L15-L16)：

```cmake
add_compile_options($<$<COMPILE_LANGUAGE:ASC>:--npu-arch=dav-${CATLASS_ARCH}>)  # ① 编译器架构标志
add_compile_definitions(CATLASS_ARCH=${CATLASS_ARCH})                           # ② C++ 预处理宏
```

- 第 15 行只对 `ASC` 语言（即交给昇腾编译器的 `.cpp` 设备代码）加 `--npu-arch=dav-2201` 或 `dav-3510`；
- 第 16 行把 `CATLASS_ARCH` 定义成预处理宏，于是源码里的 `#if CATLASS_ARCH == 2201` 这类判断就有了依据。

预处理宏驱动的典型分支在 [include/catlass/catlass.hpp:38-42](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/catlass.hpp#L38-L42)：`BYTE_PER_BLK_FP`（A1→C2 Pipe 到 GM 的数据块大小）在两代硬件上取值不同：

```cpp
#if !defined(CATLASS_ARCH) || CATLASS_ARCH == 2201
constexpr uint32_t BYTE_PER_BLK_FP = 128; // datablock size of A1->C2PiPE2GM
#elif defined(CATLASS_ARCH) && CATLASS_ARCH == 3510
constexpr uint32_t BYTE_PER_BLK_FP = 64;
#endif
```

注意第一个分支带 `!defined(CATLASS_ARCH)`——这保证「宏未定义时」也走 A2 路径，与 CMake 默认值 `2201` 对齐。

另一处是 **Ascend950 专属**的 MX 微缩放量化常量，整个块被 `#if` 包住，见 [include/catlass/catlass.hpp:46-50](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/catlass.hpp#L46-L50)：

```cpp
#if (defined(CATLASS_ARCH) && CATLASS_ARCH == 3510)
constexpr uint32_t MX_SCALE_COPY_GROUP_NUM = 2; // Mx-scale matrix 2-byte aligned
constexpr uint32_t MX_SCALE_GROUP_NUM = 32;     // Data count for one MX-scale factor per group
constexpr uint32_t MX_BASEK_FACTOR = 64;        // Data matrix alignment at K-dimension
#endif
```

> 顺带一提，[catlass.hpp:26-36](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/catlass.hpp#L26-L36) 还定义了一批架构无关的公共常量（`BYTE_PER_C0=32`、`BYTE_PER_FRACTAL=512`、`STRIDE_LIMIT=65536` 等），它们与 CATLASS_ARCH 无关、两代硬件共享，是「分形/对齐」的通用基线。

**对照 ArchTag 通道**：与上面 `#if` 并行的，是 4.1 讲的类型通道。一个能清楚展示「ArchTag 在编译期被检查」的例子见 [include/catlass/gemm/dispatch_policy.hpp:317-318](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/dispatch_policy.hpp#L317-L318)：

```cpp
struct MmadPingpongMutex : public MmadBase<ArchTag_, false> {
    static_assert(std::is_same_v<ArchTag_, Arch::Ascend950>,
                  "MmadPingpongMutex only supports Arch::Ascend950");
```

这里 `static_assert` 用 ArchTag（C++ 类型）把「Mutex 同步只支持 950」这个约束**编译期钉死**——若你误把 `Arch::AtlasA2` 传进来，编译直接报错。这正是 ArchTag 通道与 CATLASS_ARCH 宏通道的区别：一个走类型系统做能力校验，一个走预处理选常量。

#### 4.3.4 代码实践

**实践目标**：亲手切换 `CATLASS_ARCH`，观察它如何同时改变编译器标志与源码常量分支。

**操作步骤**：

1. 用默认架构编译 `00_basic_matmul`（即 `CATLASS_ARCH=2201`），确认能编过：
   ```bash
   bash scripts/build.sh -DCATLASS_ARCH=2201
   ```
2. 切到 Ascend950 再编译一次（无 950 硬件也能编译，仅看能否通过）：
   ```bash
   bash scripts/build.sh -DCATLASS_ARCH=3510
   ```
3. 在源码里临时给 [catlass.hpp:38](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/catlass.hpp#L38) 的 `#if` 分支各加一句 `#error arch=xxx`，分别在两种 `CATLASS_ARCH` 下编译，观察报出的是哪一支（**验证后务必还原，不要改动源码**）。

**需要观察的现象**：
- `build.sh` 帮助信息里 `CATLASS_ARCH` 的取值说明（`2201(AtlasA2/A3)/3510(Ascend950PR/DT)`）；
- 两次编译产生的 `--npu-arch=dav-2201` 与 `dav-3510` 差异；
- `BYTE_PER_BLK_FP` 在两次编译下分别被解析为 `128` 与 `64`。

**预期结果**：你能说清「同一个 CMake 变量，既驱动编译器选 dav 架构、又驱动源码 `#if` 选常量」这条一变两用的链路。

> 待本地验证：步骤 3 属于「为理解而临时改源码」的验证手段，验证完应还原；本仓库不允许实际修改源码。

#### 4.3.5 小练习与答案

**练习 1**：`CATLASS_ARCH` 宏通道与 `Arch::AtlasA2/Ascend950` 类型（ArchTag）通道，各自更适合承载哪种差异？

**答案**：宏通道适合「与编译器指令/底层标量绑定、且只在 `#if` 里选一次」的差异（如块大小 `BYTE_PER_BLK_FP`、是否定义 MX 常量）；ArchTag 通道适合「需要在模板链路里被传递、被 `static_assert` 校验、被搬运组件按类型特化路由」的差异（如容量常量、DispatchPolicy 选择）。两者常配合使用。

**练习 2**：若用户构建时既不传 `-DCATLASS_ARCH`，源码也没被注入该宏，`BYTE_PER_BLK_FP` 会取哪个值？为什么？

**答案**：取 `128`。因为 [catlass.hpp:38](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/catlass.hpp#L38) 的第一支条件是 `!defined(CATLASS_ARCH) || CATLASS_ARCH == 2201`，「未定义」也命中 A2 路径，与 CMake 默认值一致，保证「不指定 = 默认 A2」。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个综合任务（即本讲规格里的代码实践任务）：

**任务**：对比 AtlasA2 与 Ascend950 的 `L0C_SIZE`（128KB vs 256KB），说明这对 L0TileShape 选择的影响；并列出 6 个 Position 标签对应的存储层。

**建议步骤**：

1. **容量侧（承接 4.1）**：打开 [arch.hpp:18-37](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/arch/arch.hpp#L18-L37)，抄下两代架构的 `L0C_SIZE`。按 4.1.4 的公式，分别计算 pingpong 双缓冲下两代硬件能容纳的最大 `M×N`（A2：≤16384；950：≤32768）。写一句结论：**L0C 翻倍让 Ascend950 可以选用更宽的 L0Tile N 维，或在同等 Tile 下免费双缓冲**。
2. **Position 侧（承接 4.2）**：打开 [arch.hpp:42-48](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/arch/arch.hpp#L42-L48)，填一张「Position 别名 → 存储层」表（GM/L1/L0A/L0B/L0C/UB）。
3. **贯通侧（承接 4.3）**：解释为什么换到 950 时，除了改 `L0TileShape`，还要把构建参数切成 `-DCATLASS_ARCH=3510`（驱动 `--npu-arch=dav-3510` 与 `BYTE_PER_BLK_FP=64` 等宏分支），并在组装层把 ArchTag 换成 `Arch::Ascend950`（驱动容量常量与 DispatchPolicy 特化）——**两条通道缺一不可**。

**交付物**：一段话 + 两张小表，说清「硬件容量 → TileShape 约束」「Position → 存储层」「宏/类型双通道 → 架构特化」三组关系。

## 6. 本讲小结

- `arch.hpp` 用 `AtlasA2`、`Ascend950` 两个**纯静态结构体**把各级存储容量固化为 `static constexpr` 常量，是 TileShape 容量校验的编译期依据；两代关键差异是 **L0C 翻倍（128KB→256KB）**、UB/FIXBUF/BIAS 增大。
- **Position 标签**用 `std::integral_constant`（空类型）给 GM/L1/L0A/L0B/L0C/UB 六层存储起类型名，把「数据在哪一层」编进类型，既作 `MakeTensor` 的标签实参，也作为枚举值存进 `GemmType::POSITION`。
- 架构特化有**两条并行通道**：`CATLASS_ARCH` 预处理宏（CMake 一变两用：`--npu-arch` 标志 + 源码 `#if` 常量）与 `Arch::AtlasA2/Ascend950` C++ 类型 ArchTag（容量档案 + DispatchPolicy 模板特化 + `static_assert` 能力校验）。
- `examples/CMakeLists.txt:15-16` 是「一变两用」的关键：同一 CMake 变量同时变成编译器标志与预处理宏。
- 「换架构」= 同时切 `CATLASS_ARCH` 宏与 ArchTag 类型，二者分别管底层常量/指令与容量/组件路由，缺一不可。

## 7. 下一步学习建议

本讲把「硬件规格」装进了类型系统，下一步可以看这些规格**如何被实际使用**：

- **U4 Block 层与主循环**：看 `BlockMmad` 如何读取 `ArchTag` 的容量常量、用 Position 标签驱动 `TileCopy`/`TileMmad` 完成 GM→L1→L0 的搬运与计算（u4-l1）。
- **U5 Tile 层与硬件指令**：看 `tile/atlasa2/` 与 `tile/ascend950/` 两个子目录如何按 ArchTag 把同一语义的搬运/MMad 路由到两代硬件的不同实现（u5-l3）。
- **U10 跨架构迁移**：把本讲的「双通道」认知用于实战——从 A2 迁移到 Ascend950 时，到底要改哪些宏分支、容量约束与 Tile 组件（u10-l1）。
