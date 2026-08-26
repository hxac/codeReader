# GlobalTensor：全局内存上的形状与步长抽象

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `pto::Shape` / `pto::Stride` 模板中「静态维度 + 动态维度」的混合设计：哪些信息进类型、哪些信息留到运行期。
2. 解释 `pto::GlobalTensor` 如何把一个 `__gm__` 指针、五维形状、五维步长和布局提示打包成一个轻量「视图」类型，以及它为什么**不拥有内存**。
3. 用步长公式 \(\text{offset}(i_0,\dots,i_4)=\sum_{d=0}^{4} i_d \times S_d\) 手算任意元素的偏移，并牢记步长的单位是**元素**而不是字节。
4. 使用 `TASSIGN(globalTensor, ptr + offset)` 在循环中移动 GM 窗口，实现「一个小 tile 反复搬运大矩阵不同分块」的编程模式。
5. 独立完成综合实践：为 128×256 的 float 矩阵定义 GlobalTensor（静态 Shape+Stride），按 64×64 的 tile 分两批 TLOAD 并写回，在 CPU 模拟器上验证。

本讲是单元二「核心数据抽象」的第二篇。上一讲（u2-l1）我们看过类型系统与公共常量；本讲深入 GM 侧的数据抽象，下一讲（u2-l3）将进入片上侧的 `Tile` 编程模型，两者互为镜像。

## 2. 前置知识

在读源码之前，先用通俗语言建立四个直觉。

**直觉一：GM 与片上内存是两个世界。** 承接 u1-l4 的结论：NPU 的内存是分层的——Global Memory（GM，容量大、速度慢）与片上缓冲（UB/L1/L0，容量小、速度快）。计算指令只能吃片上数据，所以任何内核都逃不开「GM → 片上 → GM」的搬运循环。`GlobalTensor` 描述 GM 侧的世界，`Tile` 描述片上侧的世界，TLOAD/TSTORE 负责在两者之间架桥。

**直觉二：指针 + 形状 + 步长 = 视图（view）。** 一个裸指针只回答「数据从哪开始」，不回答「怎么排布」。加上形状（每维多长）和步长（每维走一步跳多远），同一块内存就可以被解读成不同样子——这就是「视图」。如果你用过 numpy，可以把 `GlobalTensor` 理解成 `numpy.lib.stride_tricks.as_strided` 产物的静态类型版：**同样的内存，不同的 (shape, stride) 就是不同的视图**。

**直觉三：步长的单位是元素。** 第 \(d\) 维的步长 \(S_d\) 表示「该维下标加 1 时，地址要前进多少个**元素**」。对一个行主序的 \(R \times C\) 矩阵，行步长是 \(C\)（走一行跳 \(C\) 个元素）、列步长是 1。换算成字节要自己乘 `sizeof(T)`。

**直觉四：能进类型的信息尽量进类型。** 承接 u1-l4/u1-l5 的结论：PTO 把形状信息做成模板参数，是为了让约束检查前移到编译期（`static_assert`），并让后端为已知形状生成更优的搬运代码。但真实算子的矩阵尺寸往往运行期才知道，所以 Shape/Stride 被设计成「静态维度 + 动态维度」的混合体——这是本讲反复出现的主题。

另外，`__gm__` 是 CCE 编译器的地址空间修饰符（GM 指针）；在 CPU 模拟器下它被 stub 处理成普通指针（u1-l5 已建立这一认知）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/pto/common/pto_tile.hpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp) | 本讲主战场：`DYNAMIC`、`Shape`、`Stride`、`GlobalTensor`、`TileShape2D`、`BaseShape2D` 全部在此定义 |
| [include/pto/common/type.hpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/type.hpp) | `Layout` 枚举（布局提示）与 `GlobalTensorDim` 维度编号常量 |
| [include/pto/common/memory.hpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/memory.hpp) | 对照材料：`MemoryQualifier` 把 TileType 映射到片上地址空间（`__ubuf__`/`__cbuf__` 等） |
| [include/pto/common/pto_instr.hpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_instr.hpp) | 公共 `TASSIGN` 入口（经 `MAP_INSTR_IMPL` 分发到后端） |
| [include/pto/cpu/TAssign.hpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/cpu/TAssign.hpp) | CPU 模拟器的 `TASSIGN_IMPL`：Tile 分支与 GlobalTensor 分支 |
| [include/pto/npu/a2a3/TAssign.hpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/npu/a2a3/TAssign.hpp) | NPU（A2/A3）的 `TASSIGN_IMPL`，用于对照 |
| [include/pto/cpu/TLoad.hpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/cpu/TLoad.hpp) | 「元数据被谁消费」的证据：CPU 版 TLOAD 逐元素按 shape/stride 采集 |
| [tests/cpu/st/testcase/tadd/tadd_kernel.cpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/tadd_kernel.cpp) | 静态 Shape+Stride 的最小实例（u1-l4 精读过，本讲换一个视角看它） |
| [demos/baseline/add/csrc/kernel/add_custom.cpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/baseline/add/csrc/kernel/add_custom.cpp) | **窗口移动**的标准范式：循环内 `TASSIGN(xGlobal, x + iterOffset)` |
| [demos/cpu/gemm_demo/gemm_demo.cpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/cpu/gemm_demo/gemm_demo.cpp) | 完整五维 stride（非 1 的前三维步长）的真实用例 |
| [docs/coding/GlobalTensor.md](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/docs/coding/GlobalTensor.md) | 官方编程模型文档，本讲的多处结论可与之互证 |

## 4. 核心概念与源码讲解

本讲按五个模块展开：先建立五维坐标系（4.1），再依次精读 Shape（4.2）、Stride（4.3）、GlobalTensor（4.4），最后落在 GM 地址重绑定（4.5）。

### 4.1 五维坐标系：GlobalTensorDim 与 Layout

#### 4.1.1 概念说明

PTO 把 GM 中的张量统一建模为**五维**对象。为什么是五维而不是二维？因为 Ascend 的数据排布（如卷积友好的分形格式、图像格式）天然多于两维，统一成五维后，同一套搬运指令可以描述所有场景。官方文档明确说明：大多数二维用法把前三个维度设为 1，只用最后两维表示 (rows, cols)。

为此需要两个基础「词汇表」：

- **维度编号** `GlobalTensorDim`：DIM_0 ~ DIM_4 共 5 个维度，外加 TOTAL_DIM=5。
- **布局提示** `Layout`：ND（行主序）、DN（列主序）、NZ（Cube 分形）以及 MX_* 低精度格式、NC1HWC0 等图像格式。注意它是**提示（hint）**，用于指导后端走特定快速路径，不是逐元素的排布描述（那是 Stride 的职责）。

#### 4.1.2 核心流程

```
一个 GM 张量在 PTO 中的描述 = 指针 data_
                             + 每维长度 shape[0..4]     （Shape 提供）
                             + 每维步长 stride[0..4]    （Stride 提供）
                             + 布局提示 layout          （枚举，编译期常量）

二维矩阵 M(R×C) 的惯用法：
    维度：  DIM_0  DIM_1  DIM_2  DIM_3  DIM_4
    形状：    1      1      1      R      C
    行主序步长： 1      1      1      C      1
```

#### 4.1.3 源码精读

维度编号定义为一个常量命名空间，5 个维度加总数：

- [include/pto/common/type.hpp:427-434](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/type.hpp#L427-L434)：定义 `DIM_0`~`DIM_4` 与 `TOTAL_DIM = 5`，这是所有 Shape/Stride 代码共用的维度下标。

`Layout` 枚举覆盖了行/列主序、Cube 分形和一系列硬件排布格式：

- [include/pto/common/type.hpp:165-189](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/type.hpp#L165-L189)：`ND`（行主序）、`DN`（列主序）、`NZ`（Cube 场景的分形排布）、`SCALE`、`MX_A_ZZ` 等低精度格式、`NC1HWC0`/`NCHW` 等图像格式。本讲只用到 ND/DN/NZ 三种，其余在 u5-l5（卷积）和 u5-l6（MX 低精度）中再遇到。

对照记忆一片代码：片上侧的 Tile 用 `TileType` 决定**住在哪个片上地址空间**，这个映射由 `MemoryQualifier` 完成——

- [include/pto/common/memory.hpp:23-33](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/memory.hpp#L23-L33)：`TileType::Vec` 映射到 `__ubuf__`（Unified Buffer）。而 GlobalTensor 描述的永远是 GM，指针类型固定为 `__gm__`（见 4.4.3）。一个住片上、一个住 GM，这就是 Tile 与 GlobalTensor 的分工。

#### 4.1.4 代码实践

1. **实践目标**：建立「维度编号 + 布局枚举」的肌肉记忆。
2. **操作步骤**：打开上面两个链接，数一数 `Layout` 枚举共有多少个值；在纸上画一张 5 列的表格，标上 DIM_0~DIM_4，把一个 3×4 的行主序矩阵按惯用法填进去（前三列为 1）。
3. **需要观察的现象**：`Layout` 里有多个带 `FRACTAL_Z`、`NC1HWC0` 字样的值——它们说明五维建模不只是为普通矩阵服务。
4. **预期结果**：你能不看资料写出「二维矩阵前三维恒为 1，rows 在 DIM_3，cols 在 DIM_4」这句话。

#### 4.1.5 小练习与答案

**练习 1**：一个二维矩阵的行数和列数分别放在哪个维度？
**答案**：行数在 DIM_3，列数在 DIM_4；DIM_0~DIM_2 惯例上设为 1。

**练习 2**：`Layout::ND` 和 `Layout::DN` 的区别是什么？它们和 Stride 的职责如何分工？
**答案**：ND 表示行主序（行内连续），DN 表示列主序（列内连续）。它们是给后端的「排布提示」，用于选择快速路径；而每个元素的确切地址由 Stride 逐维计算，两者是提示与精确描述的关系。

### 4.2 Shape 模板：静态维度与动态维度

#### 4.2.1 概念说明

`pto::Shape<N1..N5>` 是一个极小的模板结构体，存放 5 个维度长度。它的精髓在于**每个模板参数既可以是一个编译期常数，也可以是 `pto::DYNAMIC`（即 -1）**：

- 填常数 → 该维度长度进入**类型**，成为 `staticShape` 数组的一部分，编译期可见；
- 填 `DYNAMIC` → 该维度长度是**运行期值**，保存在成员数组 `shape[]` 中，由构造函数参数填充。

这正好对应真实算子的两类信息：tile 尺寸通常编译期已知（静态），而整个 GM 矩阵有多大往往运行期才知道（动态）。一个 Shape 类型可以同时容纳两者。

#### 4.2.2 核心流程

```
Shape<1, 1, 1, -1, -1>  t(rows, cols);
   │                      │
   │ 静态维 1,1,1          │ 运行期传入，只填到 DYNAMIC 的位置
   ▼                      ▼
staticShape = {1,1,1,-1,-1}   shape[] = {1,1,1,rows,cols}

构造规则（编译期强制）：
  构造函数实参个数 == 模板参数中 DYNAMIC 的个数
  例：Shape<1,1,1,-1,-1> 有 2 个 DYNAMIC → 只能用 2 参构造，static_assert 保证
```

#### 4.2.3 源码精读

`DYNAMIC` 就是一个 constexpr 常量 -1：

- [include/pto/common/pto_tile.hpp:28](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L28)：`constexpr int DYNAMIC = -1;`——「-1 表示这个维度留给运行期」的全局约定。

Shape 的骨架与「静态进类型、动态进数组」的两份存储：

- [include/pto/common/pto_tile.hpp:30-45](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L30-L45)：`struct Shape` 把 5 个模板参数存进 `staticShape`；五参构造函数用 `if constexpr (Nk == DYNAMIC)` 逐维判断，**只有动态维才接收运行期实参**。
- [include/pto/common/pto_tile.hpp:140-142](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L140-L142)：运行期数组 `int64_t shape[5]`，默认全 1——静态维的运行期副本从不被使用。

参数个数与动态维个数必须相等，由编译期断言强制：

- [include/pto/common/pto_tile.hpp:61-77](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L61-L77)：单参构造函数里的 `static_assert` 把「DYNAMIC 的个数」加起来与 1 比较，不匹配直接编译失败；实参按 `if constexpr` 链找到唯一的动态维填入。二参、三参、四参构造同理（见 [include/pto/common/pto_tile.hpp:79-98](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L79-L98) 的二参版本，`vals[idx++]` 按维度顺序依次填充）。

无参构造把动态维默认置 1（[include/pto/common/pto_tile.hpp:47-59](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L47-L59)）。

真实用例——tadd 内核用的是**全静态** Shape：

- [tests/cpu/st/testcase/tadd/tadd_kernel.cpp:19-21](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L19-L21)：`Shape<1,1,1,kGRows_,kGCols_>` 四个维度全是编译期常量（模板参数传入），没有任何 DYNAMIC，因此构造 `GlobalData src0Global(src0)` 时连形状实参都不需要。

#### 4.2.4 代码实践

1. **实践目标**：亲手触发一次 Shape 的编译期检查，体会「静态维与动态维的配额」。
2. **操作步骤**（源码阅读 + 本地小实验，不动仓库源码，在自己的临时工程里做）：
   - 写一个小文件包含 `<pto/pto-inst.hpp>`，在 `__CPU_SIM` 宏下编译（可参考 u1-l5 用 `g++ -E/-S` 验证包含关系的做法）；
   - 定义 `using S1 = pto::Shape<1,1,1,-1,-1>;` 然后故意写 `S1 s(3);`（1 个实参对 2 个动态维），记录编译错误；
   - 改成 `S1 s(128, 256);` 再编译。
3. **需要观察的现象**：第一次编译失败，`static_assert` 报错文本是 "1-parameter constructors is only applicable to Stride with 1 dynamic dimension."（报错文案里写 Stride，但同一套构造模式 Shape/Stride 共用，语义一致）；第二次通过。
4. **预期结果**：理解「构造实参个数 = DYNAMIC 个数」是类型系统层面的硬约束，不是运行期检查。完整编译验证**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`Shape<1,1,1,-1,-1>` 与 `Shape<1,1,1,128,256>` 分别适合什么场景？
**答案**：前者适合矩阵尺寸运行期才知道的算子（如框架下发的动态 shape）；后者适合尺寸固定的算子，尺寸进类型后编译期即可完成大量合法性检查与代码生成。

**练习 2**：`staticShape` 和成员 `shape[]` 里分别存什么？静态维的值会写进 `shape[]` 吗？
**答案**：`staticShape` 存 5 个模板参数原始值（含 -1 标记）；`shape[]` 只在对应维是 DYNAMIC 时被构造函数填充。静态维不会写入 `shape[]`，它的运行期副本从不使用，查询走 `staticShape`。

**练习 3**：为什么 `Shape` 的五参构造函数不需要 `static_assert`？
**答案**：五参构造对 5 个维度各给一个实参，任何 DYNAMIC 组合下参数都足够且一一对应，不存在「个数不匹配」的问题；只有 1~4 参构造才可能与动态维个数冲突，因此只有它们需要断言。

### 4.3 Stride 模板：以元素为单位的步长

#### 4.3.1 概念说明

`pto::Stride<S1..S5>` 与 Shape 结构完全同构，只是语义换成「该维下标 +1 时地址前进多少个**元素**」。多维寻址只有一条公式：

\[
\text{offset}(i_0, i_1, i_2, i_3, i_4) = \sum_{d=0}^{4} i_d \cdot S_d
\]

对二维惯用法（前三维为 1，行主序），公式退化为 \(\text{offset}(r, c) = r \cdot S_3 + c \cdot S_4 = r \cdot C + c\)。

两个易错点：

1. **单位是元素不是字节**。float 时行步长 256 意味着前进 256×4=1024 字节。
2. **窗口形状和整矩阵步长可以不一致**。这是 PTO 分块搬运的核心技巧：Shape 描述「这次搬多大的一块」，Stride 描述「这块在整矩阵里怎么走」——一个 64×64 的窗口完全可以带着 256 的行步长，套在 128×256 大矩阵的左上角上。

#### 4.3.2 核心流程

```
GM 中一个 R×C 行主序矩阵，取左上角 r×c 的窗口：
    WinShape  = Shape<1, 1, 1, r, c>      ← 窗口大小
    WinStride = Stride<1, 1, 1, C, 1>     ← 行步长 = 整矩阵列数 C（不是 c！）

窗口内第 (i, j) 个元素在 GM 中的偏移 = i*C + j
（若误写成 Stride<1,1,1,c,1>，窗口第二行会从错误地址取数）
```

#### 4.3.3 源码精读

Stride 与 Shape 逐行同构，同样有 staticShape 式的双份存储与构造断言：

- [include/pto/common/pto_tile.hpp:144-160](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L144-L160)：`struct Stride` 的五参构造，`staticStride` 数组 + `if constexpr` 逐维填充。
- [include/pto/common/pto_tile.hpp:176-193](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L176-L193)：单参构造 + `static_assert`（「1 个实参只能配恰有 1 个动态维的 Stride」），常见用法是只让行步长 DYNAMIC、列步长固定 1，然后 `Stride<1,1,1,-1,1> ld(cols)`——这与官方文档 `GT t(ptr, {rows, cols}, {ld})` 的示例一致（[docs/coding/GlobalTensor.md:50-57](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/docs/coding/GlobalTensor.md#L50-L57)）。
- [include/pto/common/pto_tile.hpp:255-257](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L255-L257)：运行期数组 `stride[5]`。

仓库为最常见的二维场景准备了两个**别名工厂**，省去手写五维模板参数：

- [include/pto/common/pto_tile.hpp:719-728](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L719-L728)：`TileShape2D<T, rows, cols, Layout::ND>` 展开为 `Shape<1,1,1,rows,cols>`——名字叫 TileShape 但它就是「二维形状」助手。
- [include/pto/common/pto_tile.hpp:790-805](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L790-L805)：`BaseShape2D<T, rows, cols, Layout::ND>` **实际是 Stride 助手**（继承自 `Stride`，官方文档也专门提醒这一点），ND 时展开为 `Stride<rows*cols, rows*cols, rows*cols, cols, 1>`——注意 DIM_3 步长是 `cols`，即整块矩阵的列数。
- [include/pto/common/pto_tile.hpp:806-821](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L806-L821)：DN（列主序）版本，DIM_3/DIM_4 的步长角色互换（`<1,1,1,1,rows>`）。
- NZ（分形）版本见 [include/pto/common/pto_tile.hpp:771-789](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L771-L789)，它把 rows/cols 折算成 `C0_SIZE_BYTE/sizeof(T)` 的整数倍块，细节留到 u2-l3/u4-l5。

两个真实用例：

- [tests/cpu/st/testcase/tadd/tadd_kernel.cpp:20-21](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L20-L21)：`Stride<1,1,1,kGCols_,1>`——行步长等于矩阵列数，标准行主序整矩阵视图。
- [demos/cpu/gemm_demo/gemm_demo.cpp:83-85](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/cpu/gemm_demo/gemm_demo.cpp#L83-L85)：GEMM 三个矩阵的 Stride 前三维是 `kM*kK`、`kK*kN` 等真实批量步长（A/B/C 各是一批矩阵中的第 0 个），证明五维不是摆设。

#### 4.3.4 代码实践

1. **实践目标**：能手算步长偏移，并识别「窗口步长 ≠ 窗口列数」的写法。
2. **操作步骤**：纸面完成三问——
   - 128×256 行主序 float 矩阵，元素 (47, 100) 的偏移是多少个元素、多少字节？
   - `demos/baseline/add` 中窗口 Stride 为 `Stride<1,1,1,tileCols,1>`（tileCols 是**整块**列数），窗口是 tileSRows×tileSCols，写出窗口内 (i, j) 的偏移公式；
   - 把 gemm_demo 的 GlobalA 五维 stride 逐维抄下来，说明 DIM_0 的步长 `kM*kK` 什么时候会用到。
3. **需要观察的现象**：第三问里 DIM_2 步长是 `kM*kK`（整矩阵大小），只有当 DIM_2 方向有多个矩阵时才会走到。
4. **预期结果**：第一问答案 \(47 \times 256 + 100 = 12172\) 个元素 = 48688 字节；第二问 \(\text{offset} = i \cdot \text{tileCols} + j\)，即窗口行宽是大矩阵行宽，不是窗口自己的宽度。

#### 4.3.5 小练习与答案

**练习 1**：`BaseShape2D` 名字里带 Shape，为什么说它是 Stride 助手？
**答案**：它的所有特化都继承自 `pto::Stride<...>`（如 ND 版继承 `Stride<rows*cols, rows*cols, rows*cols, cols, 1>`），产出的是步长类型；官方文档 GlobalTensor.md 也专门强调这一点。命名历史原因，按「Base=整矩阵基准步长」理解即可。

**练习 2**：一个 64×64 的 tile 窗口套在 128×256 行主序矩阵的第 2 个行块（rows 64~127）的第 0 列块上，Stride 应该怎么写？
**答案**：`Stride<1,1,1,256,1>`。窗口行步长等于**整矩阵**列数 256；窗口位置由指针偏移（`base + 64*256`）决定，不改 Stride。这正是 4.5 节 TASSIGN 移动窗口的用法。

**练习 3**：把上题的 Stride 误写成 `Stride<1,1,1,64,1>` 会发生什么？
**答案**：窗口第 i 行会从 `base + i*64` 取数，即把大矩阵按 64 列宽错误重排，搬进 tile 的数据全错。CPU 模拟器的 TLOAD 按步长逐元素采集（见 4.4.3 的 TLoad 证据），所以这种错误**不需要 NPU 就能被发现**——输出与 golden 对不上。

### 4.4 GlobalTensor：把指针、形状、步长打包

#### 4.4.1 概念说明

有了 Shape 和 Stride，`GlobalTensor<Element, Shape, Stride, Layout>` 只剩下「打包」工作：

- `Element`：元素类型（如 `float`）；
- `Shape`/`Stride`：上面两节的五维描述；
- `Layout`：布局提示，默认 `Layout::ND`。

它内部只有三个数据成员：一个 `__gm__` 指针 `data_`、一份运行期 `shape_`、一份运行期 `stride_`。**它不分配、不拥有任何内存**——只是给已经存在的 GM 内存贴一张「如何解读」的标签。TLOAD/TSTORE/MGATHER/MSCATTER 等搬运指令吃的就是这张标签（官方文档开篇即说明这一点，见 [docs/coding/GlobalTensor.md:1-10](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/docs/coding/GlobalTensor.md#L1-L10)）。

查询接口分两套：静态维用**模板版** `GetShape<dim>()`（constexpr，可进 `static_assert`）；动态维用**运行期版** `GetShape(dim)`（读成员数组）。写接口 `SetShape<dim>()`/`SetStride<dim>()` 只允许作用于 DYNAMIC 维，静态维在编译期就被拒绝。

#### 4.4.2 核心流程

```
GlobalTensor<float, Shape<1,1,1,64,64>, Stride<1,1,1,256,1>, Layout::ND> win(ptr);

构造时（只拷贝动态维）：
    data_   = ptr
    shape_  ← 构造实参中属于 DYNAMIC 维的值（本例全静态，无需填）
    stride_ ← 同上

查询时：
    静态维：GetShape<DIM_3>()  → 编译期返回 64（static_assert 保证非 DYNAMIC）
    动态维：GetShape(DIM_3)    → 运行期读 shape_.shape[3]
    指针：  data()             → __gm__ float*
```

#### 4.4.3 源码精读

类型头部：四个别名把 Element、Shape、Stride、Layout 收拢，并推导出 `__gm__` 指针类型：

- [include/pto/common/pto_tile.hpp:272-290](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L272-L290)：`DType = __gm__ RawDType`（先用 `remove_gm_t` 剥掉可能重复的 `__gm__` 修饰，定义在 [include/pto/common/pto_tile.hpp:259-270](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L259-L270)）；`staticShape`/`staticStride` 数组把 Shape/Stride 的静态值直接摊平到 GlobalTensor 自己身上，查询时无需再穿透一层。

构造函数：`if constexpr` 逐维过滤，静态维完全忽略运行期实参：

- [include/pto/common/pto_tile.hpp:291-326](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L291-L326)：形参 `shape`/`stride` 带默认值 `defaultShape`/`defaultStride`（全 1，见 [include/pto/common/pto_tile.hpp:652-658](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L652-L658)），所以全静态类型只需传指针——这正是 tadd 里 `GlobalData src0Global(src0);` 一个参数就够的原因。

两套查询接口的对照：

- [include/pto/common/pto_tile.hpp:329-363](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L329-L363)：运行期 `GetShape(dim)`/`GetStride(dim)`，内部经私有助手 `GetShapeSize`/`GetStrideSize`（[include/pto/common/pto_tile.hpp:624-643](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L624-L643)）做「静态返回常量、动态读成员」的分派。
- [include/pto/common/pto_tile.hpp:365-373](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L365-L373)：模板版 `GetShape<dim>()` 是 constexpr，且对 DYNAMIC 维直接 `static_assert` 报错（"dim x is dynamic, cannot be obtained using the template interface."）——想拿编译期值，该维就必须是静态的。

写接口只放行动态维：

- [include/pto/common/pto_tile.hpp:439-445](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L439-L445)、[include/pto/common/pto_tile.hpp:509-515](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L509-L515)：`SetShape<dim>(s)` / `SetStride<dim>(s)`，`static_assert(staticShape[dim] == DYNAMIC, "dim must be DYNAMIC")`。

CPU 模拟器专属的按坐标读写，把步长公式原样写进源码：

- [include/pto/common/pto_tile.hpp:584-592](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L584-L592)：`GetElement(i0..i4)` 中 `offset = i0*S0 + i1*S1 + i2*S2 + i3*S3 + i4*S4`，即 4.3.1 节的公式 \(\sum i_d S_d\) 的代码化。
- 指针访问器与友元声明：[include/pto/common/pto_tile.hpp:579-582](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L579-L582)（`data()` 返回 `DType*`；`TASSIGN_IMPL` 是友元，所以 4.5 节能改私有指针）；私有成员与 `SetAddr` 见 [include/pto/common/pto_tile.hpp:645-649](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L645-L649)。

**元数据被谁消费？** 以 CPU 版 TLOAD 为证：

- [include/pto/cpu/TLoad.hpp:145-159](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/cpu/TLoad.hpp#L145-L159)：TLOAD 实现先把 `src.GetShape(...)`×5、`src.GetStride(...)`×5 收进两个数组，再对窗口内每个 (row, col) 调 `MapTileIndicesToGlobalOffset`（按 shape/stride 换算 GM 偏移）逐元素 `src.GetElement(offset)` 采集。**这就是「错误的 Stride 会直接算错地址」的代码级证据。**
- [include/pto/cpu/TLoad.hpp:71-81](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/cpu/TLoad.hpp#L71-L81)：ND/DN 路径还用 `assert` 校验「GlobalTensor 的形状乘积 == tile 有效区域」，说明 GlobalTensor 的 Shape 必须描述**本次搬运的窗口**，而不是整个大矩阵。

类型萃取 Trait（后端用来区分 Tile 与 GlobalTensor）：

- [include/pto/common/pto_tile.hpp:1823-1827](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L1823-L1827)：`is_global_data_v` / `is_tile_data_v`，TASSIGN 的分支判断就靠它们（见 4.5.3）。

#### 4.4.4 代码实践

1. **实践目标**：验证「全静态 GlobalTensor 只需一个指针实参」以及两套查询接口的差异。
2. **操作步骤**（源码阅读型 + 本地小实验）：
   - 对照 [tests/cpu/st/testcase/tadd/tadd_kernel.cpp:19-32](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L19-L32)，写出 tadd 的 `GlobalData` 寻址公式（`offset(r,c) = r*kGCols_ + c`）；
   - 在自己的临时测试里定义 `using GT = pto::GlobalTensor<float, pto::Shape<1,1,1,64,64>, pto::Stride<1,1,1,256,1>>;`，尝试 `GT::GetShape<3>()`（模板版）和 `gt.GetShape(3)`（运行期版）两种调用；
   - 再定义一个 `Shape<1,1,1,-1,-1>` 的动态版本，对它调用模板版 `GetShape<3>()`，记录编译错误。
3. **需要观察的现象**：模板版对静态维在编译期给出常量；对动态维直接 `static_assert` 失败，报错信息提示应使用运行期接口。
4. **预期结果**：静态信息在编译期即可用（可用于 `static_assert` 或模板推导），动态信息只能运行期查询。完整编译验证**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 GlobalTensor 构造函数对静态维「忽略」运行期实参，而不是复制一份？
**答案**：静态维的权威值在类型里（`staticShape`），复制到运行期数组会造成两份可能不一致的真相；查询接口用 `if constexpr` 直接返回常量，运行期副本对静态维毫无用途。

**练习 2**：`GlobalTensor::DType` 是什么？为什么需要 `remove_gm_t`？
**答案**：`DType = __gm__ RawDType`，即带 GM 地址空间修饰的元素类型。`remove_gm_t` 用来剥掉用户可能已经写过的 `__gm__` 修饰，避免出现 `__gm__ __gm__ T` 这样的重复修饰导致编译错误。

**练习 3**：CPU 模拟器的 TLOAD 为什么逐元素搬运而不是一次 memcpy？
**答案**：因为 Stride 允许窗口非连续（如 64×64 窗口配 256 的行步长，每行之间要跳过 192 个元素），只有逐元素按公式 \(\sum i_d S_d\) 计算偏移才能覆盖任意步长；只有当步长表明内存连续时，后端才可能优化成整块拷贝。

### 4.5 GM 地址重绑定：用 TASSIGN 移动窗口

#### 4.5.1 概念说明

TASSIGN 一词两用（u1-l4 见过它的 Tile 用法）：

- 对 **Tile**：实参是**整数**（UB 偏移地址），把片上缓冲的某个偏移绑给 tile；
- 对 **GlobalTensor**：实参是**同类型 `__gm__` 指针**，直接替换 `data_`，即「把视图整体平移到新起点」。

后者就是**移动窗口**模式：Shape/Stride 类型不变，每轮循环把指针推进一个 tile 的距离，同一对「tile + GlobalTensor」就能扫过整个大矩阵。相比每轮构造新对象，它清晰表达了「视图滑动」的意图，也与 NPU 后端的实现路径一致。

两条硬约束（编译期强制）：

1. 指针必须是**指针类型**（不能给 GlobalTensor 传整数）；
2. 指针的元素类型必须与 `GlobalTensor::DType` 一致。

#### 4.5.2 核心流程

```
分块搬运大矩阵（每轮搬一个 tile）：

初始化：  tile(64,64) ← TASSIGN(tile, 0x0)            # Tile 用整数 UB 偏移
          win(base)                                 # GlobalTensor 带静态 Shape/Stride

循环 b = 0..B-1：
    TASSIGN(win, base + b*64*C)                   # 窗口平移：跳过 b 个 64 行块
    TLOAD(tile, win)      # MTE2：GM → UB
    （事件同步，见 u1-l4 的握手套路）
    TSTORE(outWin, tile)  # MTE3：UB → GM

要点：Shape/Stride 全程不变，动的只有 data_ 指针。
```

#### 4.5.3 源码精读

公共入口只是一行转发（分层设计的又一例，承接 u1-l5 的 MAP_INSTR_IMPL 认知）：

- [include/pto/common/pto_instr.hpp:101-105](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_instr.hpp#L101-L105)：`TASSIGN(obj, addr)` → `MAP_INSTR_IMPL(TASSIGN, obj, addr)` 分发到当前后端的 `TASSIGN_IMPL`。

CPU 后端实现——两条分支一目了然：

- [include/pto/cpu/TAssign.hpp:22-37](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/cpu/TAssign.hpp#L22-L37)：`is_tile_data_v` 分支要求**整数**地址并经 `NPUMemoryModel::ResolveAssignedAddress` 把 UB 偏移解析成宿主指针；GlobalTensor 分支要求**指针**类型且 `static_assert` 元素类型一致，最后调私有的 `SetAddr(addr)`（能访问私有成员是因为 GlobalTensor 在 [include/pto/common/pto_tile.hpp:579-580](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L579-L580) 声明了友元）。
- NPU 侧对照：[include/pto/npu/a2a3/TAssign.hpp:17-35](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/npu/a2a3/TAssign.hpp#L17-L35) 的分支与断言完全同构，Tile 分支把整数偏移 `reinterpret_cast` 成片上指针——**公共接口一份、实现按后端各归其位**（u1-l3 的结论再次兑现）。

标准用例——`demos/baseline/add` 是「移动窗口」的教科书：

- [demos/baseline/add/csrc/kernel/add_custom.cpp:46-52](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/baseline/add/csrc/kernel/add_custom.cpp#L46-L52)：窗口定义 `Shape<1,1,1,tileSRows,tileSCols>`（**tile 大小**）配 `Stride<1,1,1,tileCols,1>`（**整块矩阵行宽**）——Shape 与 Stride 刻意不等宽，这就是「窗口套大矩阵」。
- [demos/baseline/add/csrc/kernel/add_custom.cpp:83-88](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/baseline/add/csrc/kernel/add_custom.cpp#L83-L88)：循环内 `iterOffset = offset + i * tileSRows * tileSCols`，然后 `TASSIGN(xGlobal, x + iterOffset)` 等三连——每轮把三个窗口同步平移到当前 tile。
- [demos/baseline/add/csrc/kernel/add_custom.cpp:114-115](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/baseline/add/csrc/kernel/add_custom.cpp#L114-L115)：循环结束后 `TASSIGN(zGlobal, z); z = zGlobal.data();` 把指针**复原到矩阵起点**再取回——`data()` 是把 `__gm__` 指针交还给宿主编排的出口（tadd 里也有同款 `out = dstGlobal.data();`，见 [tests/cpu/st/testcase/tadd/tadd_kernel.cpp:41-42](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L41-L42)）。

官方文档对 TASSIGN 的说明与之互证：[docs/coding/GlobalTensor.md:86-88](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/docs/coding/GlobalTensor.md#L86-L88)（"TASSIGN(globalTensor, ptr) sets the underlying GM pointer"，指针类型由 `TASSIGN_IMPL` 的 `static_assert` 强制匹配）。

#### 4.5.4 代码实践

1. **实践目标**：吃透 add_custom 的窗口平移公式，能对任意轮次手算偏移。
2. **操作步骤**：
   - 读 [demos/baseline/add/csrc/kernel/add_custom.cpp:72-88](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/baseline/add/csrc/kernel/add_custom.cpp#L72-L88)，抄下 `offset`（核间偏移，来自 `block_idx`）与 `iterOffset`（核内轮次偏移）两个公式；
   - 先从常量定义算出窗口尺寸：源码里 `BLOCK_ROWS=20`、`BLOCK_COLS=1`、`BUFFER_NUM=2`（[demos/baseline/add/csrc/kernel/add_custom.cpp:19-21](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/baseline/add/csrc/kernel/add_custom.cpp#L19-L21)）、`tileNum=2`（[demos/baseline/add/csrc/kernel/add_custom.cpp:30](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/demos/baseline/add/csrc/kernel/add_custom.cpp#L30)）、`tileRows=20, tileCols=2048`（内核入口处的实参），于是 `bTileRows = 20/20 = 1`、`bTileCols = 2048/1 = 2048`、`tileSRows = 1`、`tileSCols = 2048/2/2 = 512`；
   - 手算第 3 轮（i=2）、block_idx=1 时的 `iterOffset`。
3. **需要观察的现象**：总偏移 = 核间起点偏移 + 核内轮次偏移，两部分都按「元素个数」计数（不是字节）。
4. **预期结果**：`offset = 1 × bTileRows × bTileCols = 1×1×2048 = 2048`；`iterOffset = 2048 + 2×tileSRows×tileSCols = 2048 + 2×512 = 3072` 个元素。

#### 4.5.5 小练习与答案

**练习 1**：`TASSIGN(tile, 0x4000)` 和 `TASSIGN(gt, ptr+64)` 的实参类型约束有何不同？
**答案**：Tile 分支要求整数（UB 偏移，`std::is_integral_v`），CPU 下经 NPUMemoryModel 解析成宿主指针；GlobalTensor 分支要求指针（`std::is_pointer_v`）且指向元素类型必须与 `GlobalTensor::DType` 完全一致，两条都是 `static_assert` 编译期强制。

**练习 2**：为什么 add_custom 在循环结束后要 `TASSIGN(zGlobal, z)` 再 `z = zGlobal.data()`，直接 `z.data()` 不行吗？
**答案**：循环里 zGlobal 的指针已被平移到某个 tile 起点，直接取 `data()` 会拿到偏移后的指针；先 TASSIGN 复原到矩阵基址，`data()` 才是宿主期望的输出起点。这体现「视图指针是可变状态，用完要归位」的习惯。

**练习 3**：移动窗口模式下，Shape/Stride/指针三者谁在循环中变化？
**答案**：只有指针（`data_`）变。Shape 描述每轮窗口大小（固定），Stride 描述窗口在大矩阵中的走法（固定），指针决定窗口落在哪——三者分离正是该模式可读、可维护的原因。

## 5. 综合实践

**任务**：新建一个 ST 用例 `tcopywin`——把 128×256 float 矩阵的前 64 列按两个 64×64 tile 分两批「搬」到 128×64 的输出矩阵，用 CPU 模拟器 + golden 比对验证。它综合了本讲全部四个最小模块：静态 Shape+Stride（4.2/4.3）、GlobalTensor 打包（4.4）、TASSIGN 移动窗口（4.5）。

**设计要点**（先想清楚再动手）：

- 输入矩阵 128×256，行主序；输出矩阵 128×64。
- 输入窗口：`Shape<1,1,1,64,64>` + `Stride<1,1,1,256,1>`（行步长 = **256**，本实践的灵魂）。
- 输出窗口：`Shape<1,1,1,64,64>` + `Stride<1,1,1,64,1>`（输出矩阵本身行宽就是 64）。
- 两批：批 0 取 rows 0~63，批 1 取 rows 64~127（均取 cols 0~63）。

**步骤**：

1. 复制四件套：`cp -r tests/cpu/st/testcase/tadd tests/cpu/st/testcase/tcopywin`（本地练习，不提交仓库）。
2. 在 [tests/cpu/st/testcase/CMakeLists.txt](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/CMakeLists.txt#L39-L47) 的 `ALL_TESTCASES` 列表中追加一行 `tcopywin`（该函数按目录名注册用例，见 [tests/cpu/st/testcase/CMakeLists.txt:11-16](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/CMakeLists.txt#L11-L16)），目录内 CMakeLists 改成 `pto_cpu_sim_st(tcopywin)`。
3. 编写内核 `tcopywin_kernel.cpp`（**示例代码**，基于 [tadd_kernel.cpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L16-L43) 改写）：

   ```cpp
   #include <pto/pto-inst.hpp>
   using namespace pto;

   template <typename T, int kGRoles_, int kGCols_, int kTRows_, int kTCols_>
   AICORE void runTCopyWin(__gm__ T __out__* out, __gm__ T __in__* src)
   {
       // 窗口形状 = tile 形状；输入步长 = 整矩阵行宽（窗口套大矩阵）
       using WinShape = Shape<1, 1, 1, kTRows_, kTCols_>;
       using InStride = Stride<1, 1, 1, kGCols_, 1>;   // 行步长 256
       using OutStride = Stride<1, 1, 1, kTCols_, 1>;  // 行步长 64
       using InGlobal = GlobalTensor<T, WinShape, InStride>;
       using OutGlobal = GlobalTensor<T, WinShape, OutStride>;
       using TileData = Tile<TileType::Vec, T, kTRows_, kTCols_, BLayout::RowMajor, -1, -1>;

       TileData tile(kTRows_, kTCols_);
       TASSIGN(tile, 0x0);              // Tile：整数 UB 偏移
       InGlobal inWin(src);             // GlobalTensor：指针构造
       OutGlobal outWin(out);
       constexpr int kBatches = kGRoles_ / kTRows_;    // 128/64 = 2 批

       for (int b = 0; b < kBatches; ++b) {
           TASSIGN(inWin, src + b * kTRows_ * kGCols_);   // 窗口下移 64 行
           TASSIGN(outWin, out + b * kTRows_ * kTCols_);
           TLOAD(tile, inWin);
           set_flag(PIPE_MTE2, PIPE_MTE3, EVENT_ID0);
           wait_flag(PIPE_MTE2, PIPE_MTE3, EVENT_ID0);
           TSTORE(outWin, tile);
           set_flag(PIPE_MTE3, PIPE_MTE2, EVENT_ID0);
           wait_flag(PIPE_MTE3, PIPE_MTE2, EVENT_ID0);
       }
       out = outWin.data();
   }

   template void LaunchCopy<float, 128, 256, 64, 64>(float* out, float* src, void* stream);
   ```
   （`LaunchCopy` 的外壳函数仿照 tadd 的 `LaunchTAdd` 编写即可。事件链在 CPU 模拟器上是空操作，只验证数值；NPU 上的正确性待真机/sim 验证——这正是 u1-l4/u1-l5 已建立的边界认知。）
4. 修改 `main.cpp`：注意输入与输出的文件大小**不同**——输入 `128*256*sizeof(float)`，输出/golden `128*64*sizeof(float)`（tadd 的 main.cpp 对三个 buffer 用同一 fileSize，需要拆开）；套件名与用例名按 u1-l4 讲过的「TEST_F 名 = gen_data 目录名」约定，例如 `TCopyWinTest.case_float_128x256_128x64`。
5. 编写 `gen_data.py`（**示例代码**）：

   ```python
   x = NumExt.astype(np.random.randint(1, 10, size=[128, 256]), np.float32)
   golden = NumExt.zeros([128, 64], np.float32)
   for b in range(2):
       golden[b*64:(b+1)*64, :] = x[b*64:(b+1)*64, 0:64]   # 两批各取前 64 列
   NumExt.write_array("input1.bin", x, np.float32)
   NumExt.write_array("golden.bin", golden, np.float32)
   ```
6. 运行：`python3 tests/script/run_st.py -r sim -v a3 -t tcopywin`。

**需要观察的现象**：

- 正确版本：输出 = 输入前 64 列的两批拼接，gtest 通过。
- 反例实验：把 `InStride` 改成 `Stride<1,1,1,64,1>`（行步长误用窗口宽度），重新生成并运行——预期 golden 比对**失败**，因为窗口每行只前进 64 个元素，第 2 行起取到的是错误数据（对照 4.4.3 的 TLoad 逐元素采集证据，这是纯步长错误，CPU 模拟器即可暴露）。
- 再试：把两批的批间偏移从 `b*kTRows_*kGCols_` 改成 `b*kTRows_*kTCols_`，同样预期失败（第二批取错位置）。

**预期结果**：三组运行中，只有原始版本通过；两个反例分别证明「Stride 必须是整矩阵行宽」与「窗口平移必须按整矩阵行宽跳」。完整流程**待本地验证**（本讲义未在环境中实际执行，若目录名/注册方式与当前脚本行为不符，以 `tests/script/run_st.py` 的实际探测逻辑为准）。

## 6. 本讲小结

- PTO 把 GM 张量统一建模为**五维**对象：`GlobalTensor = __gm__ 指针 + Shape + Stride + Layout 提示`，它是**不拥有内存的视图**，由 TLOAD/TSTORE 等搬运指令消费。
- `Shape`/`Stride` 采用「静态维度进类型 + 动态维度（`DYNAMIC = -1`）留运行期」的混合设计，构造实参个数必须等于 DYNAMIC 个数（`static_assert` 编译期强制）。
- 步长单位是**元素**，寻址公式 \(\text{offset} = \sum_{d=0}^{4} i_d S_d\)；二维惯用法是前三维为 1、行步长=整矩阵列数。`TileShape2D` 是 Shape 助手，`BaseShape2D` 名字带 Shape 但**是 Stride 助手**。
- 查询分两套：静态维用模板版 `GetShape<dim>()`（constexpr，DYNAMIC 维编译期拒绝），动态维用运行版 `GetShape(dim)`；`SetShape/SetStride` 只对 DYNAMIC 维开放。
- `TASSIGN` 一词两用：Tile 吃**整数** UB 偏移，GlobalTensor 吃**同类型 `__gm__` 指针**并整体平移窗口（内部 `SetAddr`）；循环内移动窗口、结束时指针归位是标准范式（add_custom）。
- CPU 版 TLOAD 按 shape/stride 逐元素采集，因此**步长错误在 CPU 模拟器上就能暴露**，不必上 NPU。

## 7. 下一步学习建议

本讲讲完了 GM 侧的抽象，下一讲 **u2-l3《Tile 编程模型深度剖析》** 讲它的镜像——片上侧的 `Tile`：容量形状与有效区域（valid mask）的区别、`TileType` 与 `BLayout` 的完整含义、512 字节对齐与分形（fractal）布局约束。建议重点对比三组概念：`GlobalTensor::Shape`（窗口多大） vs `Tile` 容量形状（片上装多少）；`Layout::ND/DN`（GM 布局提示） vs `BLayout::RowMajor/ColMajor`（tile 内部布局）；`TASSIGN` 的指针语义 vs UB 偏移语义。源码上可继续精读 [include/pto/common/pto_tile.hpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp) 的 `Tile` 定义（与本讲的 GlobalTensor 同文件），并预读 [docs/coding/Tile.md](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/docs/coding/Tile.md)。
