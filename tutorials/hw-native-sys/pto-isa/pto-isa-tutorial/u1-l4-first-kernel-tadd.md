# 第一个 Tile 内核：TADD 测试用例逐行精读

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐行读懂 `tests/cpu/st/testcase/tadd` 下的内核代码：看懂 `Tile`、`GlobalTensor`、`Shape`、`Stride` 的模板参数各代表什么。
2. 理解一条完整的数据链路：`TASSIGN`（把 Tile 绑定到片上地址）→ `TLOAD`（GM 搬入 UB）→ `TADD`（向量计算）→ `TSTORE`（写回 GM）。
3. 说清 `set_flag` / `wait_flag` 为什么必须插在「搬运」和「计算」之间，以及它们在 CPU 模拟器上实际发生了什么。
4. 掌握 ST 测试 `main.cpp` 的骨架：aclrt 初始化 → 分配 → 读 golden → launch → 比对，以及 `gen_data.py` 如何按用例名生成数据目录。
5. 独立完成实践：运行 `case_float_64x64_64x64` 用例，并新增一个 32x128 的新用例跑通。

## 2. 前置知识

本讲假设你已完成 u1-l3，知道 ST 用例「四件套」（`*_kernel.cpp`、`main.cpp`、`gen_data.py`、`CMakeLists.txt`）的分工。在此基础上，补充几个硬件侧概念：

- **GM（Global Memory）与 UB（Unified Buffer）**：GM 是设备上的大容量 DDR 内存，容量大但访问慢；UB 是 AI Core 内部的片上缓冲，容量小（几十到几百 KB）但访问快。一条 Tile 指令（如 TADD）只能操作位于 UB 里的数据，所以必须先「搬进来、算完、再搬出去」。
- **流水线（Pipe）**：Ascend AI Core 内部按功能分为若干条独立硬件流水线，本讲涉及三条：
  - `PIPE_MTE2`：Memory Transfer Engine 2，负责 GM → 片上缓冲的搬入（TLOAD 走这条）。
  - `PIPE_V`：Vector 向量计算单元（TADD 走这条）。
  - `PIPE_MTE3`：负责片上缓冲 → GM 的搬出（TSTORE 走这条）。
  三条流水线可以并行工作，因此「第 2 块数据的搬运」可以和「第 1 块数据的计算」重叠——这正是流水线优化的来源，也带来了同步需求。
- **事件（Event）**：不同流水线之间传递「我干完了」信号的机制。生产流水线 `set_flag` 置位，消费流水线 `wait_flag` 等待，从而保证消费者读到的数据一定已经写好。
- **golden 比对**：测试预先用 Python（numpy）算出一份「标准答案」存成 `golden.bin`，设备/模拟器算完后再逐元素比对，误差在容差内即通过。

一个提醒：CPU 模拟器（`__CPU_SIM`）把这些硬件概念全部「扮演」了一遍——`__gm__`、`__ubuf__` 等修饰符被展开为空宏，aclrt 接口退化为 `malloc`/`memcpy`，事件退化为空函数。所以同一份内核代码既能编译到 NPU，也能在你本机直接跑，这正是 PTO「一份代码、多套后端」的第一手体验。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `tests/cpu/st/testcase/tadd/tadd_kernel.cpp` | 被测内核：`runTAdd`（设备侧）+ `LaunchTAdd`（启动封装）+ 显式模板实例化 |
| `tests/cpu/st/testcase/tadd/main.cpp` | gtest 测试骨架：aclrt 初始化、数据灌入、launch、golden 比对 |
| `tests/cpu/st/testcase/tadd/gen_data.py` | 用 numpy 生成 `input1.bin` / `input2.bin` / `golden.bin` |
| `tests/cpu/st/testcase/tadd/CMakeLists.txt` | 一行调用 `pto_cpu_sim_st(tadd)` 完成构建注册 |
| `include/pto/common/pto_tile.hpp` | `Shape` / `Stride` / `GlobalTensor` / `Tile` 四个核心类型的定义 |
| `include/pto/common/pto_instr.hpp` | `TASSIGN` / `TLOAD` / `TADD` / `TSTORE` 的公共 API 声明 |
| `include/pto/common/event.hpp` | `Op` 枚举与「指令 → 流水线」映射表 |
| `include/pto/common/cpu_stub.hpp` | CPU 模拟器对硬件语义的替身（空宏、空事件、malloc 版 aclrt） |
| `include/pto/cpu/TAssign.hpp` | `TASSIGN` 的 CPU 实现（把片上偏移地址解析成模拟内存指针） |
| `tests/common/test_common.h` | `ReadFile` / `WriteFile` / `ResultCmp` 测试工具函数 |

## 4. 核心概念与源码讲解

### 4.1 内核模板参数：一份代码，多种形状与数据类型

#### 4.1.1 概念说明

`tadd_kernel.cpp` 中真正干活的是模板函数 `runTAdd`，它有 5 个模板参数：

```cpp
template <typename T, int kGRows_, int kGCols_, int kTRows_, int kTCols_>
```

- `T`：元素类型（`float`、`int32_t`、`int16_t`、`aclFloat16` 等）。
- `kGRows_` / `kGCols_`：Global 侧（GM 里整个矩阵）的行数、列数。
- `kTRows_` / `kTCols_`：Tile 侧（一次搬进 UB 的块）的行数、列数。

TADD 用例里 Global 形状和 Tile 形状恰好相同（一次搬完），但参数分开定义是为了让同一个模板天然支持「大矩阵分多次 tile 处理」的写法。把形状做成**编译期常量**是 PTO 的核心设计取舍：编译器（以及 `static_assert` 检查）在编译时就知道 tile 多大、放在哪，越界、不对齐等错误在编译期直接报出来，而不是等到运行时出错。

#### 4.1.2 核心流程

```text
TEST_F 用例（main.cpp）
    │  test_tadd<float, 64, 64, 64, 64>()
    ▼
LaunchTAdd<T, ...>（显式实例化于 tadd_kernel.cpp，main.cpp 只见声明）
    │  aclFloat16 → half 的类型归一化
    ▼
runTAdd<T, ...>（真正的设备侧代码：TASSIGN → TLOAD → TADD → TSTORE）
```

由于 `runTAdd` 定义在 `tadd_kernel.cpp` 内部、`main.cpp` 只能看到 `LaunchTAdd` 的声明，所以每个具体「类型 × 形状」组合都必须在 `tadd_kernel.cpp` 末尾**显式实例化**一次，链接期才能找到符号。这解释了为什么「新增一个用例」需要同时改三个文件（内核、main、gen_data）——后面综合实践会用到。

#### 4.1.3 源码精读

先看内核的函数签名与修饰符：

[tests/cpu/st/testcase/tadd/tadd_kernel.cpp:16-17](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L16-L17) 定义了模板函数 `runTAdd`，返回类型 `AICORE` 是 NPU 编译器的函数属性；在 CPU 模拟器上 `AICORE` 被 [include/pto/common/cpu_stub.hpp:37](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/cpu_stub.hpp#L37) 定义为空宏，所以它就是普通 C++ 函数。参数里的 `__gm__ T __out__* out` 表示「指向全局内存的输出指针」，`__gm__`/`__out__` 同样在 [include/pto/common/cpu_stub.hpp:39-40](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/cpu_stub.hpp#L39-L40) 被展开为空。

启动封装和类型归一化：

[tests/cpu/st/testcase/tadd/tadd_kernel.cpp:48-51](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L48-L51) 中，`LaunchTAdd` 对 `aclFloat16` 做了特判：把它转成 `half` 再进入内核。`aclFloat16` 是面向 aclrt 接口的半精度类型（CPU 路径下在 [include/pto/common/type.hpp:444](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/type.hpp#L444) 被定义为 `_Float16` 的别名），内核模板统一按 `half` 处理。

显式实例化清单：

[tests/cpu/st/testcase/tadd/tadd_kernel.cpp:54-62](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L54-L62) 列出了 5 个实例化：`float`/`int32_t`/`int16_t` 的 64×64、`aclFloat16` 的 16×256，以及被 `CPU_SIM_BFLOAT_ENABLED` 守卫的 `bfloat16_t` 用例。这份清单与 `main.cpp` 里的 `TEST_F`、`gen_data.py` 里的 `case_params_list` 三方一一对应。

`Tile` 的完整模板参数表在 [include/pto/common/pto_tile.hpp:1390-1395](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L1390-L1395)：

```cpp
template <
    TileType Loc_, typename Element_, const int Rows_, const int Cols_,
    const BLayout BFractal_ = BLayout::RowMajor,
    const int RowValid_ = Rows_, const int ColValid_ = Cols_, ...>
struct Tile { ... };
```

| 参数位 | 含义 | tadd 中的取值 |
| --- | --- | --- |
| `Loc_` | Tile 所在的片上位置类型（决定挂到哪块缓冲） | `TileType::Vec`（UB 上的向量 tile） |
| `Element_` | 元素类型 | `T` |
| `Rows_` / `Cols_` | tile 的容量形状（编译期常量） | `kTRows_` / `kTCols_` |
| `BFractal_` | 行主/列主布局 | `BLayout::RowMajor` |
| `RowValid_` / `ColValid_` | 有效区域（静态写死或 `-1` 表示运行时传入） | `-1, -1`（运行时） |

#### 4.1.4 代码实践

1. **实践目标**：建立「模板参数 → 显式实例化 → TEST_F → 数据目录」的四方对应关系。
2. **操作步骤**：打开 [tests/cpu/st/testcase/tadd/tadd_kernel.cpp:54-62](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L54-L62)、[tests/cpu/st/testcase/tadd/main.cpp:94-101](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/main.cpp#L94-L101)、[tests/cpu/st/testcase/tadd/gen_data.py:76-83](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/gen_data.py#L76-L83) 三处，逐行对齐。
3. **需要观察的现象**：`int16_t` 用例在三个文件中分别出现在哪一行？`bfloat16_t` 用例为什么三处都带了条件编译/条件追加？
4. **预期结果**：整理出一张 5 行对应表（dtype、Global 形状、Tile 形状、TEST_F 名、数据目录名）。bf16 需要编译器支持 C++23 `std::bfloat16_t`，所以三处都由 `CPU_SIM_BFLOAT_ENABLED` / `PTO_CPU_SIM_ENABLE_BF16` 开关控制，保持同开同关。

#### 4.1.5 小练习与答案

**练习 1**：为什么不把 `kTRows_`/`kTCols_` 设计成运行时参数（比如构造函数传进去）？

**答案**：tile 形状是编译期常量时，`static_assert` 可以在编译期检查「列数 × 元素大小是否 32 字节对齐」「tile 是否超出 UB 容量」等约束（见 4.2.3），错误在编译阶段暴露；同时 NPU 编译器能据此静态分配合片上地址、生成最优的指令 repeat 参数。运行时形状则这些检查和优化都无法进行。PTO 的折中是：形状静态、**有效区域**（ValidRow/ValidCol）可以运行时化。

**练习 2**：`LaunchTAdd` 的 `stream` 参数在函数体里根本没有用到，为什么还保留？

**答案**：这是为了与 NPU 板端的启动接口签名保持一致——真机上 launch 需要把内核提交到某个 `aclrtStream`。CPU 模拟器上流是退化的（[include/pto/common/cpu_stub.hpp:104](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/cpu_stub.hpp#L104) 中 `aclrtSynchronizeStream` 直接返回 0），保留参数可以让同一份 `main.cpp` 无改动地在两种环境编译。

### 4.2 Tile 定义与 TASSIGN 地址绑定

#### 4.2.1 概念说明

内核开头 4 行 `using` 完成了「数据描述」，接下来 3 个 `TASSIGN` 完成了「资源绑定」。这是 PTO 手工模式（Manual Mode）最重要的两个动作：

- **GlobalTensor** 只是一个「视图」：GM 指针 + 五维形状（`Shape`）+ 五维步长（`Stride`），本身不拥有内存。它回答「数据在 GM 里如何排布」。
- **Tile** 描述「UB 里的一块容量」，但**不自动分配内存**——手工模式下程序员必须用 `TASSIGN` 显式告诉它落在 UB 的哪个偏移地址上。这相当于手工做内存分配：地址写错（两块 tile 重叠、越界）不会有运行时保护，只能靠编译期检查和程序员自己规划。

#### 4.2.2 核心流程

```text
类型定义（编译期）
    DynShapeDim5 = Shape<1,1,1,kGRows_,kGCols_>   # 五维，前三维固定为 1，后两维是矩阵
    DynStridDim5 = Stride<1,1,1,kGCols_,1>        # 行间步长 kGCols_，列间步长 1 → 行主排布
    GlobalData   = GlobalTensor<T, Shape, Stride>
    TileData     = Tile<Vec, T, kTRows_, kTCols_, RowMajor, -1, -1>

运行时
    构造 3 个 Tile 对象（传入运行时有效区域 64×64）
    TASSIGN(src0Tile, 0x0)      ┐
    TASSIGN(src1Tile, 0x4000)   ├ 三个 tile 各占 UB 的一段偏移
    TASSIGN(dstTile,  0x8000)   ┘
    构造 3 个 GlobalTensor 视图（包住 GM 指针）
```

地址规划的算术依据（以 float 64×64 为例）：

\[ \text{tile 字节数} = kTRows_ \times kTCols_ \times \text{sizeof}(T) = 64 \times 64 \times 4 = 16384 = 0\text{x}4000 \]

所以 `0x0 / 0x4000 / 0x8000` 三个起点恰好让三块 tile 首尾相接、互不重叠，总共占用 \(3 \times 0\text{x}4000 = 0\text{xC}000\) 即 48 KB 的 UB。

#### 4.2.3 源码精读

类型定义四连：

[tests/cpu/st/testcase/tadd/tadd_kernel.cpp:19-22](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L19-L22) 依次定义了 Shape、Stride、GlobalTensor、Tile 四个别名。逐个看底层定义：

- [include/pto/common/pto_tile.hpp:28-32](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L28-L32)：`DYNAMIC = -1`；`Shape` 是 5 个 `int64_t` 模板参数的结构体，每个维度既可以写死（编译期常量），也可以填 `DYNAMIC` 留到运行时再给。
- [include/pto/common/pto_tile.hpp:144-147](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L144-L147)：`Stride` 与 `Shape` 同构。tadd 中 `Stride<1,1,1,kGCols_,1>` 表示第 4 维（行）每前进一格地址加 `kGCols_` 个元素、第 5 维（列）每前进一格加 1——这正是行主序矩阵的步长表达。
- [include/pto/common/pto_tile.hpp:272-291](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L272-L291)：`GlobalTensor` 模板为 `<元素类型, Shape, Stride, Layout>`，构造函数接收 GM 指针和（可选的）运行时形状/步长，只把动态维度拷进成员，静态维度直接用编译期常量。

Tile 对象的构造与运行时有效区域：

[tests/cpu/st/testcase/tadd/tadd_kernel.cpp:23-25](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L23-L25) 以 `(kTRows_, kTCols_)` 构造三个 tile。因为模板里 `RowValid_`/`ColValid_` 传的是 `-1`（`DYNAMIC`），所以匹配到 [include/pto/common/pto_tile.hpp:1469-1479](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L1469-L1479) 的「两个维度都是运行时变量」构造函数，把 64、64 存入 `RowMaskInternal`/`ColMaskInternal`。这样 tile 的**容量**是 64×64，**有效区域**在运行时决定——TADD 只对有效区域内的元素负责（见 4.3.3 的约束说明）。

TASSIGN 绑定：

[tests/cpu/st/testcase/tadd/tadd_kernel.cpp:26-28](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L26-L28) 三行 `TASSIGN` 把三个 tile 绑到 `0x0`、`0x4000`、`0x8000`。公共 API 在 [include/pto/common/pto_instr.hpp:101-105](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_instr.hpp#L101-L105)，另有编译期地址重载 [include/pto/common/pto_instr.hpp:110-118](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_instr.hpp#L110-L118)（`TASSIGN<0x4000>(tile)` 形式，可触发静态越界/对齐检查）。CPU 后端的实现是 [include/pto/cpu/TAssign.hpp:22-37](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/cpu/TAssign.hpp#L22-L37)：对 Tile 调用 `NPUMemoryModel::Instance().ResolveAssignedAddress<T>(addr)`，由 [include/pto/cpu/NPUMemoryModel.hpp:209-218](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/cpu/NPUMemoryModel.hpp#L209-L218) 把「UB 偏移地址」翻译成模拟内存里的真实指针——也就是说 CPU 模拟器内部真的维护了一块按 GM/UB/L1 分区的模拟地址空间，`0x4000` 这类偏移会被映射到模拟 UB 区域的对应位置。

GlobalTensor 视图：

[tests/cpu/st/testcase/tadd/tadd_kernel.cpp:30-32](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L30-L32) 用三个 GM 指针构造三个 `GlobalData` 视图。注意这一步没有任何数据搬运，只是「把指针和形状步长打包」。

对齐约束（编译期红线）：

[include/pto/common/pto_tile.hpp:1522-1529](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L1522-L1529) 的 `static_assert` 要求：行主且非分形布局下，`Cols * sizeof(DType)` 必须是 [include/pto/common/pto_tile.hpp:1079](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L1079) 定义的 `alignedSize = 32` 的整数倍。验证一下 tadd 的取值：float 64 列 → \(64 \times 4 = 256\)，\(256 \bmod 32 = 0\) ✓；half 256 列 → \(256 \times 2 = 512\) ✓。

#### 4.2.4 代码实践

1. **实践目标**：验证 UB 地址规划意识——算字节数、判断是否重叠。
2. **操作步骤**：
   - 对 5 个显式实例化逐个计算 tile 字节数：`bytes = Rows × Cols × sizeof(T)`。
   - 检查在 `0x0 / 0x4000 / 0x8000` 的布局下，哪个 dtype 的 tile 之间有富余、哪个正好首尾相接。
   - 思考（不要真改）：若把 `TASSIGN(dstTile, 0x8000)` 改成 `TASSIGN(dstTile, 0x2000)`，float 64×64 用例会发生什么？
3. **需要观察的现象**：`int16_t` 64×64 的 tile 只有 8192 字节（`0x2000`），三个 tile 实际只需 24 KB，`0x4000` 间隔留了一倍余量；float/int32 的 64×64 正好 16 KB，间隔零余量。
4. **预期结果**：`0x2000` 落在 src0Tile 的地址区间 `0x0`~`0x4000` 内部，dstTile 与 src0Tile 重叠——TADD 写 dst 的同时会覆盖 src0 的数据。本用例中 src0 在 TADD 执行时才被读取，读写同一块区域属于未定义行为，输出可能「碰巧」正确也可能错乱；若增加第二轮使用 src0 的计算则必然出错。结论：**重叠布局是否出错取决于指令时序，必须避免**。（此推演为源码阅读型分析，具体现象待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：`Shape<1, 1, 1, kGRows_, kGCols_>` 前三个 `1` 是干什么的？

**答案**：PTO 的 `Shape`/`Stride` 统一为五维设计，以覆盖 NCHW 之类的多维张量。对二维矩阵而言，把前三维固定为 1、矩阵放在第 4、5 维即可。第 4 维步长为 `kGCols_`、第 5 维步长为 1，正是行主序。

**练习 2**：GlobalTensor 需要 TASSIGN 吗？

**答案**：不需要。`TASSIGN` 的 CPU 实现（[include/pto/cpu/TAssign.hpp:25-36](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/cpu/TAssign.hpp#L25-L36)）对 Tile 走 `ResolveAssignedAddress`（分配片上偏移），对 GlobalTensor 走 `SetAddr`（直接改指针）。GM 的地址由 host 侧 `aclrtMalloc` 分配，内核里 GlobalTensor 构造时传入指针即完成绑定；`TASSIGN` 主要是给**片上** Tile 用的。真机上 TASSIGN 也可用于让 GlobalTensor 视图整体平移（如多 tile 循环里滑动窗口）。

### 4.3 数据搬运与计算：TLOAD → TADD → TSTORE

#### 4.3.1 概念说明

三条指令构成最小完整的「load-compute-store」链：

- `TLOAD(tile, global)`：把 GM 中 global 视图描述的那块数据搬进 tile 所在的 UB（走 `PIPE_MTE2`）。
- `TADD(dst, src0, src1)`：逐元素相加，三个操作数都是 UB 里的 Vec tile（走 `PIPE_V`）。
- `TSTORE(global, tile)`：把 tile 数据写回 GM（走 `PIPE_MTE3`）。

数学语义（对有效区域内每个 `(i, j)`）：

\[ \mathrm{dst}_{i,j} = \mathrm{src0}_{i,j} + \mathrm{src1}_{i,j} \]

#### 4.3.2 核心流程

```text
GM(src0) ──TLOAD──▶ UB(src0Tile) ─┐
GM(src1) ──TLOAD──▶ UB(src1Tile) ─┴─TADD─▶ UB(dstTile) ──TSTORE──▶ GM(out)
        MTE2 流水线                     V 流水线              MTE3 流水线
```

单 tile 版本里三条指令看似串行；在多 tile 循环版本（见 u3-l3 乒乓缓冲）中，三流水线重叠执行才是性能来源。

#### 4.3.3 源码精读

公共 API 声明（都在 `pto_instr.hpp`，实现在各后端目录——这正是 u1-l3 讲过的「声明在 common、实现按后端归位」）：

- [include/pto/common/pto_instr.hpp:263-266](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_instr.hpp#L263-L266)：`PTO_INST RecordEvent TLOAD(TileData& dst, GlobalData& src, WaitEvents&... events)` —— 从 GM 搬入 tile；变参 `WaitEvents` 允许把「等待事件」作为参数传入而不是单独调 `wait_flag`（本用例用的是后者风格）。
- [include/pto/common/pto_instr.hpp:175](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_instr.hpp#L175)：`TADD(dst, src0, src1, events...)` —— 返回 `RecordEvent`，因为计算完成后需要记录事件告知下游。
- [include/pto/common/pto_instr.hpp:364-373](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_instr.hpp#L364-L373)：`TSTORE(GlobalData& dst, TileData& src, ...)` 的重载族（不同 tile 类型/量化参数有不同版本）。

内核中的调用：

[tests/cpu/st/testcase/tadd/tadd_kernel.cpp:34-41](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L34-L41) 一次性完成了两趟 TLOAD、一趟 TADD、一趟 TSTORE，中间穿插两对事件同步（下一模块展开）。最后 [tests/cpu/st/testcase/tadd/tadd_kernel.cpp:42](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L42) 的 `out = dstGlobal.data();` 把（本用例中未变化的）GM 指针写回——由于 `dstGlobal` 就是围绕 `out` 构造且从未移动，这一行在此没有实际效果，属于多 tile 循环模板的通用写法（循环里会滑动指针）。

类型与布局约束：

[docs/isa/TADD.md:46-55](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/docs/isa/TADD.md#L46-L55) 记录了实现检查：A2A3 上 `DType` 只允许 `int32_t/int16_t/half/float` 且必须行主布局；A5 支持面更宽。这些检查以后端的 `static_assert` 落实——传错类型会直接编译失败。文档同时说明：迭代域取 `dst` 的有效区域，`src0/src1` 假定兼容、不做显式运行时校验。

#### 4.3.4 代码实践

1. **实践目标**：体验编译期约束。
2. **操作步骤**：把 `tadd_kernel.cpp` 复制一份到临时目录（不要改原文件），在末尾加一行显式实例化 `template void LaunchTAdd<int8_t, 64, 64, 64, 64>(int8_t*, int8_t*, int8_t*, void*);`，用 `python3 tests/run_cpu.py -t tadd` 编译。
3. **需要观察的现象**：编译在 `include/pto/npu/a2a3/` 下某个检查点报 `static_assert` 错误。
4. **预期结果**：编译失败，错误信息指出 `int8_t` 不在 TADD 支持类型列表中（与 [docs/isa/TADD.md:48-50](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/docs/isa/TADD.md#L48-L50) 的约束一致）。具体报错文本待本地验证。验证后记得删除临时改动。

#### 4.3.5 小练习与答案

**练习 1**：`TLOAD(src0Tile, src0Global)` 中谁是目的、谁是源？

**答案**：tile 是目的、GlobalTensor 是源——参数顺序是 `(dst, src)`。而 `TSTORE(global, tile)` 相反，是 `(dst, src)` 语义下的「GM 为目的、tile 为源」，两者参数顺序保持「第一个参数写、第二个参数读」的统一约定。

**练习 2**：为什么 TADD 不直接支持「GM 里的两个矩阵相加」，非要先 TLOAD？

**答案**：向量/矩阵计算单元只能访问片上缓冲（UB/L0），GM 数据必须先经 MTE 流水线搬入。把「搬运」和「计算」拆成显式指令，正是 PTO 不隐藏底层能力的体现：程序员可以控制搬运与计算的重叠度来榨性能。

### 4.4 事件同步：set_flag / wait_flag

#### 4.4.1 概念说明

三条流水线并行工作时，指令的**发起顺序**不等于**完成顺序**：`TADD` 可能在 `TLOAD` 数据还没落到 UB 时就读了旧值。事件机制就是跨流水线的握手：

- `set_flag(pipeA, pipeB, id)`：`pipeA` 侧的指令全部完成后，为 `pipeB` 置位事件 `id`——「pipeA 的产出已就绪，pipeB 可以用了」。
- `wait_flag(pipeA, pipeB, id)`：`pipeB` 停下来等待 `pipeA` 置位的事件 `id`。

一对 `set/wait` 必须参数完全一致才能配对；事件号 `EVENT_ID0`~`EVENT_ID7` 共 8 个（[include/pto/common/event.hpp:14](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/event.hpp#L14) 与 [include/pto/common/cpu_stub.hpp:184-191](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/cpu_stub.hpp#L184-L191)）。

#### 4.4.2 核心流程

tadd 内核的完整事件时序：

```text
TLOAD(src0)          ┐
TLOAD(src1)          ├─ PIPE_MTE2（搬入）
set_flag(MTE2, V, ID0)      # MTE2 完成后置位 ID0，通知 V
wait_flag(MTE2, V, ID0)     # V 侧等待 ID0 → 保证 UB 里数据就绪
TADD                 ── PIPE_V（计算）
set_flag(V, MTE3, ID0)      # V 完成后置位 ID0，通知 MTE3
wait_flag(V, MTE3, ID0)     # MTE3 侧等待 ID0 → 保证 dstTile 已算完
TSTORE               ── PIPE_MTE3（搬出）
```

两处都复用 `EVENT_ID0` 是安全的：第一对事件在 `TADD` 前已完成握手，信号消费完毕，之后重新置位不会产生歧义。若流水线上同时存在多份在途数据（乒乓双缓冲），就必须交替使用 `EVENT_ID0`/`EVENT_ID1`（u3-l3 展开）。

#### 4.4.3 源码精读

内核中的两对同步：

[tests/cpu/st/testcase/tadd/tadd_kernel.cpp:36-40](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L36-L40) 是整个内核的「节奏控制器」：第一对夹在 TLOAD 与 TADD 之间，第二对夹在 TADD 与 TSTORE 之间。

指令到流水线的映射从哪来：

[include/pto/common/event.hpp:21-27](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/event.hpp#L21-L27) 的 `Op` 枚举为每条指令分配编号（`TLOAD`、`TSTORE_VEC`、`TADD` 都在开头几项）；[include/pto/common/event.hpp:154-163](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/event.hpp#L154-L163) 定义 `OpPipeEntry` 模板与 `PTO_DEFINE_OP_PIPE` 宏，随后 [include/pto/common/event.hpp:165-170](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/event.hpp#L165-L170) 逐条登记：`TLOAD → PIPE_MTE2`、`TSTORE_VEC → PIPE_MTE3`、`SCALAR → PIPE_S`、`TADD → PIPE_V`。写内核时查这张表就知道该在哪些流水线之间握手。

CPU 模拟器上事件是「空操作」：

[include/pto/common/cpu_stub.hpp:123-124](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/cpu_stub.hpp#L123-L124) 把 `set_flag`/`wait_flag` 实现为空函数体。因为 CPU 模拟器按源码顺序同步执行指令——TLOAD 函数返回时数据已经真实写进模拟 UB，TADD 天然看到正确数据。流水线常量本身在 [include/pto/common/cpu_stub.hpp:51-61](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/cpu_stub.hpp#L51-L61) 定义（`PIPE_S=0`、`PIPE_V=1`、`PIPE_MTE2=3`、`PIPE_MTE3=4` 等），只用于通过编译和 CostModel 建模，不产生真实等待。

#### 4.4.4 代码实践

1. **实践目标**：理解「CPU 模拟器查不出事件缺失」这一测试盲区。
2. **操作步骤**：临时注释掉 [tests/cpu/st/testcase/tadd/tadd_kernel.cpp:37](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L37) 的 `wait_flag`，重新 `python3 tests/run_cpu.py -t tadd`；跑完后恢复代码。
3. **需要观察的现象**：CPU 模拟器上测试**仍然通过**，输出与 golden 一致。
4. **预期结果**：通过。原因即 4.4.3 所述——CPU 顺序执行，事件是空操作；而同一份代码上真机（NPU）时，V 流水线可能在 MTE2 写完 UB 前读到未初始化数据，结果是**非确定性错误**。因此事件正确性必须在 NPU/sim 环境验证，或依赖 u7-l4 的 CostModel/一致性检查。（现象待本地验证。）

#### 4.4.5 小练习与答案

**练习 1**：`set_flag(PIPE_V, PIPE_MTE3, EVENT_ID0)` 的三个参数分别是什么意思？

**答案**：`PIPE_V`（第一参数）是信号的产生方/更新方——V 流水线；`PIPE_MTE3`（第二参数）是信号的等待方——MTE3 流水线；`EVENT_ID0` 是事件编号。配对的 `wait_flag` 必须以相同的三元组出现。

**练习 2**：把第一对同步改成 `set_flag(PIPE_V, PIPE_MTE2, EVENT_ID0)` / `wait_flag(PIPE_V, PIPE_MTE2, EVENT_ID0)`（方向反了），CPU 模拟器上会挂吗？真机上呢？

**答案**：CPU 模拟器上不会挂（两个函数都是空实现，参数随便传）。真机上这是错误的方向：MTE2 需要「V 等 MTE2」的信号，反向声明后 V 与 MTE2 之间没有有效握手，且语义矛盾，可能死等或读到脏数据。教训：CPU 测试通过 ≠ 事件链正确。

### 4.5 ST 测试骨架：main.cpp 与 gen_data.py

#### 4.5.1 概念说明

内核只是「被测对象」，`main.cpp` 才是把测试跑起来的骨架。它模拟了一个最小 host 侧运行时：初始化运行时 → 准备 GM 内存 → 灌入输入 → 调用内核 → 取回输出 → 与 golden 比对。`gen_data.py` 则负责在构建期生成输入与标准答案，两者靠**用例命名约定**衔接：

```text
TEST_F 名 = TADDTest.case_float_64x64_64x64_64x64
     │
     ├─ gen_data.py（在构建目录 build/ 下运行）生成目录：build/TADDTest.case_float_64x64_64x64_64x64/{input1.bin,input2.bin,golden.bin}
     └─ main.cpp 运行时按 "../" + Suite名.用例名 找到同一目录（gtest 从 build/bin/ 启动，".." 即 build/）
```

命名规则在 [tests/cpu/st/testcase/tadd/gen_data.py:59-62](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/gen_data.py#L59-L62)：`case_{dtype缩写}_{G行}x{G列}_{T行}x{T列}_{V行}x{V列}`。

#### 4.5.2 核心流程

```text
main.cpp 的 test_tadd<T,...>()：
  1. aclInit / aclrtSetDevice / aclrtCreateStream     初始化
  2. aclrtMallocHost ×3 + aclrtMalloc ×3              host 与 device 内存
  3. ReadFile(input1.bin / input2.bin)                 从 golden 目录读输入
  4. aclrtMemcpy(H2D) ×2                               灌入 device
  5. LaunchTAdd(...)                                    执行内核
  6. aclrtSynchronizeStream + aclrtMemcpy(D2H)         取回输出，WriteFile(output.bin)
  7. aclrtFree / aclrtFreeHost / aclrtDestroyStream    释放
  8. ReadFile(golden.bin) + ReadFile(output.bin)       ResultCmp 比对
```

CPU 模拟器上，步骤 1~7 全部由 `cpu_stub.hpp` 里的内联函数扮演（如 [include/pto/common/cpu_stub.hpp:74-87](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/cpu_stub.hpp#L74-L87) 的 `aclrtMallocHost` 就是 `calloc`），因此这份 `main.cpp` 在真机与模拟器之间无需任何改动。

#### 4.5.3 源码精读

fixture 与 golden 目录定位：

[tests/cpu/st/testcase/tadd/main.cpp:21-25](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/main.cpp#L21-L25) 定义空的 gtest fixture；[tests/cpu/st/testcase/tadd/main.cpp:27-34](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/main.cpp#L27-L34) 的 `GetGoldenDir()` 用 gtest 的 `current_test_info()` 拿到当前用例名，拼出 `../TADDTest.case_xxx` 相对路径。这条路径能对上是有讲究的：CMake 把可执行文件统一输出到 `build/bin/`（见 [tests/cpu/st/CMakeLists.txt:29](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/CMakeLists.txt#L29)），驱动脚本从 `build/bin/` 启动 gtest（见 [tests/run_cpu.py:333-338](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/run_cpu.py#L333-L338) 的注释），于是 `../` 正好落到构建目录根部——也就是 `gen_data.py` 被复制并运行、按用例名建目录生成数据的地方。

初始化与内存：

[tests/cpu/st/testcase/tadd/main.cpp:44-59](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/main.cpp#L44-L59) 完成 aclrt 初始化和 6 块内存（3 host + 3 device）的分配，`fileSize = kGRows_ * kGCols_ * sizeof(T)`（[tests/cpu/st/testcase/tadd/main.cpp:42](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/main.cpp#L42)）。注意 `aclrtMemset(dstDevice, ..., 0, ...)` 把输出区清零——这样有效区域外若未被内核写过，输出保持 0，与 golden 的约定一致（见 gen_data 部分）。

灌入、执行、取回：

[tests/cpu/st/testcase/tadd/main.cpp:61-71](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/main.cpp#L61-L71) 读两个输入文件、H2D 拷贝、调用 `LaunchTAdd`、同步流、D2H 拷回并写出 `output.bin`。`ReadFile`/`WriteFile` 是 [tests/common/test_common.h:64-128](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/common/test_common.h#L64-L128) 提供的二进制文件读写工具。

golden 比对：

[tests/cpu/st/testcase/tadd/main.cpp:84-91](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/main.cpp#L84-L91) 读回 golden 与 output 后调用 `ResultCmp<T>(golden, devFinal, 0.001f)`。比对逻辑在 [tests/common/test_common.h:231-269](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/common/test_common.h#L231-L269)：逐元素算绝对差与相对比，`diff > eps && relRatio > eps` 同时成立才算错（[tests/common/test_common.h:259-261](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/common/test_common.h#L259-L261)），并统计输出 [tests/common/test_common.h:272-278](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/common/test_common.h#L272-L278) 打印 max diff / err count 等指标——测试失败时先看这一行。

TEST_F 用例表：

[tests/cpu/st/testcase/tadd/main.cpp:94-101](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/main.cpp#L94-L101) 的 5 行 `TEST_F` 每行实例化一个「dtype × 形状」组合，名字即数据目录名。

gen_data.py 的 golden 语义：

[tests/cpu/st/testcase/tadd/gen_data.py:21-38](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/gen_data.py#L21-L38)：输入是 `[1,10)` 的随机整数转成目标 dtype（避免浮点误差放大）；golden 先全 0，再只把 `[:row_valid,:col_valid]` 区域填上 `input1 + input2`——**有效区域外期望为 0**，与 `main.cpp` 里输出区先清零配合，验证 TADD 只写有效区域。参数类 `TAddParams` 与主循环见 [tests/cpu/st/testcase/tadd/gen_data.py:42-50](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/gen_data.py#L42-L50)、[tests/cpu/st/testcase/tadd/gen_data.py:76-92](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/gen_data.py#L76-L92)：脚本被复制到构建目录根部运行，对每个用例直接以用例名 `makedirs` 并 `chdir` 进去生成三个 bin（开头创建的 `testcases/` 子目录实际未被使用），所以数据目录就是 `build/TADDTest.case_xxx/`，与 `GetGoldenDir()` 的 `../` 路径严格对应。

一行 CMake 的背后：

[tests/cpu/st/testcase/tadd/CMakeLists.txt:11](https://github.com/hw-native-sys-pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/CMakeLists.txt#L11) 只有一句 `pto_cpu_sim_st(tadd)`，函数体在 [tests/cpu/st/testcase/CMakeLists.txt:11-35](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/CMakeLists.txt#L11-L35)：自动把 `main.cpp` + `tadd_kernel.cpp`（存在才加）编成名为 `tadd` 的可执行文件，并接好 include 路径与 gtest。新增指令用例时 CMake 侧只需照抄这一行并把目录名登记进 [tests/cpu/st/testcase/CMakeLists.txt:39-164](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/CMakeLists.txt#L39-L164) 的 `ALL_TESTCASES` 列表。

#### 4.5.4 代码实践

1. **实践目标**：跑通一个用例并学会用 gtest 过滤器精确定位单条 case。
2. **操作步骤**：
   - 无 CANN 环境：`python3 tests/run_cpu.py -t tadd`（构建 + 生成数据 + 运行全部 4 个用例，u1-l2 已跑过流程的话这一步很快）。
   - 只跑 float 用例：`python3 tests/run_cpu.py -t tadd -g "TADDTest.case_float_64x64_64x64_64x64"`（`-g` 透传 gtest filter，见 [tests/run_cpu.py:454-455](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/run_cpu.py#L454-L455)）。
   - 有 CANN 环境时可用 NPU 模拟器：`python3 tests/script/run_st.py -r sim -v a3 -t tadd`（参数定义见 [tests/script/run_st.py:273-277](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/script/run_st.py#L273-L277)，`-r sim` 需要 `ASCEND_HOME_PATH`）。
3. **需要观察的现象**：gtest 输出每个用例的 OK/FAILED；比对通过时打印一行 `max diff: ..., err count: 0` 之类的统计。
4. **预期结果**：`TADDTest.case_float_64x64_64x64_64x64` 等用例全部 PASSED，`err count` 为 0。具体输出文本待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：golden 里有效区域外为什么填 0，而不是也填 `input1+input2`？

**答案**：PTO 指令只承诺写**有效区域**内的元素，区域外是 padding，值不保证。golden 把区域外设为 0、`main.cpp` 把输出区先清零，这样「内核没碰区域外」这一正确行为会被比对通过；若内核意外污染了区域外，输出非 0，比对立刻失败。

**练习 2**：`ResultCmp` 的判定为什么要求 `diff > eps && relRatio > eps` 同时成立才算错？

**答案**：见 [tests/common/test_common.h:259-261](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/common/test_common.h#L259-L261)。绝对差和相对差取「或」会误伤两种情况：大数值的正常浮点舍入（绝对差大但相对差小）和小数值的正常舍入（相对差大但绝对差极小）。两者同时超限才判错，是浮点比较的常见稳妥写法。

## 5. 综合实践

**任务：给 TADD 新增一个 `case_float_32x128_32x128_32x128` 用例并跑通。**

这个任务会把你在本章学的所有环节串起来。开始前先完成前置步骤：跑通现有用例（见 4.5.4）。

**第一步：设计检查（先算后改）。**
- tile 字节数：\(32 \times 128 \times 4 = 16384 = 0\text{x}4000\) 字节——与 float 64×64 相同，因此 `0x0/0x4000/0x8000` 的 TASSIGN 布局**无需修改**，三块 tile 仍然互不重叠。
- 对齐检查：行主下列数满足 \(128 \times 4 = 512\)，\(512 \bmod 32 = 0\) ✓，能通过 [include/pto/common/pto_tile.hpp:1522-1529](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L1522-L1529) 的 `static_assert`。
- 数据类型 `float` 在 TADD 的 A2A3 支持列表内 ✓。

**第二步：改三个文件（各加一行/一项）。**

1. [tests/cpu/st/testcase/tadd/tadd_kernel.cpp:54](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L54) 附近新增显式实例化：

   ```cpp
   template void LaunchTAdd<float, 32, 128, 32, 128>(float* out, float* src0, float* src1, void* stream);
   ```

2. [tests/cpu/st/testcase/tadd/main.cpp:94](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/main.cpp#L94) 附近新增：

   ```cpp
   TEST_F(TADDTest, case_float_32x128_32x128_32x128) { test_tadd<float, 32, 128, 32, 128>(); }
   ```

3. [tests/cpu/st/testcase/tadd/gen_data.py:77](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/gen_data.py#L77) 的 `case_params_list` 中追加：

   ```python
   TAddParams(np.float32, 32, 128, 32, 128, 32, 128),
   ```

   命名必须严格按 [tests/cpu/st/testcase/tadd/gen_data.py:59-62](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/gen_data.py#L59-L62) 的规则生成 `TADDTest.case_float_32x128_32x128_32x128`，且与 `TEST_F` 名完全一致，否则 `GetGoldenDir()` 找不到数据目录。

**第三步：构建运行。**

```bash
python3 tests/run_cpu.py -t tadd -g "TADDTest.case_float_32x128_32x128_32x128"
```

（有 CANN 环境可再用 `python3 tests/script/run_st.py -r sim -v a3 -t tadd` 在 NPU 模拟器上复跑一遍。）

**第四步：观察并验证。**

1. 构建目录下出现新数据目录 `TADDTest.case_float_32x128_32x128_32x128/`（与 gtest 二进制的 `bin/` 目录同级），内含三个 bin 文件，总大小各 \(32 \times 128 \times 4 = 16384\) 字节。
2. gtest 输出新用例 PASSED，`ResultCmp` 统计 `err count` 为 0。
3. 故意把 `TEST_F` 名写错一位（如 `32x1280`）再跑一次，应看到 `ReadFile` 报「Failed to get file」——体会命名约定的强约束；验证后改回。
4. 完成后 `git checkout` 恢复三个文件（本讲义约定不修改源码仓库）。

预期结果均待本地验证；若第 2 步失败，优先核对三个文件中的 dtype/形状三元组是否完全一致。

## 6. 本讲小结

- PTO 内核的「数据描述」由 `GlobalTensor`（GM 视图：指针 + `Shape` + `Stride`）和 `Tile`（片上容量：`TileType` + 元素类型 + 编译期形状 + 布局 + 有效区域）两个模板类型承担，形状静态化使约束检查全部前移到编译期。
- 手工模式下 `TASSIGN` 负责把 Tile 绑到片上偏移地址，地址规划要自己算字节数（\( \text{Rows} \times \text{Cols} \times \text{sizeof}(T) \)）保证不重叠、满足 32 字节对齐。
- 最小执行链是 `TLOAD`（MTE2）→ `TADD`（V）→ `TSTORE`（MTE3），每对相邻流水线之间用参数一致的 `set_flag`/`wait_flag` 握手。
- CPU 模拟器把硬件语义「扮演」为空操作：事件是空函数、aclrt 是 malloc/memcpy——所以 CPU 测试验证**数值语义**，但查不出事件链错误，后者必须上 NPU/sim 验证。
- ST 四件套靠命名约定衔接：`TEST_F` 名 = `gen_data.py` 生成的数据目录名，新增用例必须同时改内核显式实例化、`TEST_F`、`TAddParams` 三处，CMake 只需一行 `pto_cpu_sim_st(tadd)`。

## 7. 下一步学习建议

本讲你已读完一条最短内核链路，接下来两条路：

1. **横向铺开（推荐先走）**：u2 单元深入四个数据抽象——`Shape`/`Stride` 的动态维度玩法（u2-l2）、Tile 的有效区域掩码与分形布局（u2-l3）、TASSIGN 与 UB 容量规划（u2-l4）。本讲只用了最简单的静态形状 + 全量有效区域。
2. **纵向加深**：u3-l1/l2 系统讲事件模型与「指令 → 流水线」映射表（本讲只用了表里三行）；u3-l3 在 `demos/baseline/add` 里看多 tile 循环 + 乒乓双缓冲，那才是事件 ID 复用、`TASSIGN` 地址规划真正发挥价值的场景。

阅读建议：把 [include/pto/common/event.hpp:165-295](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/event.hpp#L165-L295) 的完整映射表扫一遍，数一数 `PIPE_V` 上挂了多少条指令——你会对本讲 TADD 的位置有更立体的认识。
