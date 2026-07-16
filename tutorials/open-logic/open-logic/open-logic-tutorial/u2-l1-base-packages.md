# base 包体系：math/logic/array/string/attribute

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 Open Logic `base` 区域五个公共包（`pkg_array` / `pkg_math` / `pkg_logic` / `pkg_string` / `pkg_attribute`）各自的职责与适用场景。
- 理解这五个包之间的依赖关系，并能解释为什么 `compile_order.txt` 把它们排成「attribute → array → math → string → logic」的顺序。
- 学会在自己的实体里 `use` 这些包，并用 `log2ceil`、`binaryToGray`、`choose`、`hex2StdLogicVector` 等函数简化常量计算与端口宽度推导。
- 理解 `pkg_attribute` 如何用「一套声明、各厂商忽略不认识的部分」的方式封装跨厂商综合属性，让同一份 VHDL 跑在 Vivado / Quartus / Efinity / Gowin 等不同工具上。
- 能编写并运行一个小测试，打印并校验这些函数的返回值。

## 2. 前置知识

本讲默认你已经学过 **u1-l5（编码规范与阅读一个实体）**，熟悉以下概念：

- **VHDL 包（package）**：把类型、常量、函数集中声明、可被多个实体 `use` 复用的编译单元，分「包声明（header）」与「包体（body）」两段。
- **泛型（generic）**：实例化时确定的参数。本讲会看到大量「用函数在编译期由泛型推导端口宽度」的写法。
- **`std_logic_vector` / `unsigned` / `signed`**：VHDL 里最常用的位向量类型，分别属于 `ieee.std_logic_1164` 与 `ieee.numeric_std`。
- **综合属性（synthesis attribute）**：写在信号/对象上的 `attribute ... of ... : signal is ...;`，用来指导综合工具如何推断硬件（例如「别把这几个寄存器合并进 SRL」）。
- **AXI-S 握手、两进程法、shadow 寄存器**：u1-l5 已建立，本讲在引用 `olo_base_pl_stage` 时会用到。

如果你对 VHDL 函数重载（同名函数、不同参数类型）还不熟，记住一句话即可：VHDL 允许同一个函数名有多种参数类型版本，编译器按调用时的实参类型自动选择——本讲里 `choose`、`max`、`min`、`count` 都 heavily 依赖这一点。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [olo_base_pkg_array.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_array.vhd) | 定义全库共用的数组类型（`StlvArray*_t`、`IntegerArray_t`、`RealArray_t` 等）与数组转换/展平函数，是其他几个包的类型基础。 |
| [olo_base_pkg_math.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_math.vhd) | 数学与类型转换函数：`log2`/`log2ceil`、`max`/`min`、`choose`、`count`、整数与位向量互转、`fromString` 等。 |
| [olo_base_pkg_logic.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_logic.vhd) | 位级逻辑函数：移位、格雷码、`ppcOr`、`to01`、位序/字节序翻转、置位索引、PRBS 多项式常量。 |
| [olo_base_pkg_string.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_string.vhd) | 字符串处理：大小写转换、去空白、十六进制串转位向量、错误消息拼装。 |
| [olo_base_pkg_attribute.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_attribute.vhd) | 封装跨厂商综合属性声明与配套常量，包体为空，纯声明。 |
| [compile_order.txt](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/compile_order.txt) | 全库编译顺序，体现了五个包的依赖先后。 |
| [olo_base_pl_stage.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd) | 流水线寄存器实体，是 `pkg_attribute` 的典型消费者。 |
| [olo_base_fifo_async.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_async.vhd) | 异步 FIFO，集中展示了 `log2ceil`、`binaryToGray`、`grayToBinary`、`compareNoCase` 的真实用法。 |
| [olo_base_pkg_math_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_pkg_math/olo_base_pkg_math_tb.vhd) | `pkg_math` 的 VUnit 测试台，本讲代码实践的参照对象。 |

**五个包的依赖关系**（箭头表示「依赖 / use」）：

```
pkg_attribute  (无依赖，仅 ieee)
pkg_array      (无依赖，仅 ieee)
      │
      ▼
pkg_math ───────► (使用 array 的 IntegerArray_t / RealArray_t / BoolArray_t)
      │
      ├──► pkg_string  (使用 math 的 max / choose)
      └──► pkg_logic   (使用 math 的 log2ceil)
```

这条依赖链直接决定了 `compile_order.txt` 的前几行顺序：`olo_base_pkg_attribute`（第 1 行）、`olo_base_pkg_array`（第 4 行）、`olo_base_pkg_math`（第 5 行）、`olo_base_pkg_string`（第 9 行）、`olo_base_pkg_logic`（第 16 行）。**包必须在所有使用它的实体之前编译**，否则编译报错——这正是 `compile_order.txt` 存在的意义。

> 关于循环依赖的一个设计细节：`pkg_math` 与 `pkg_logic` 里报告非法参数时，**故意不调用** `pkg_string` 的 `errorMessage()`，而是手写 `report` 字符串。源码注释明确说明这是为了避免 `math ↔ string` 之间形成循环依赖。我们会在 4.3 节展开。

## 4. 核心概念与源码讲解

### 4.1 pkg_array：数组类型基础

#### 4.1.1 概念说明

在 RTL 里经常需要表达「一组同类型信号」，例如 4 个通道各自的数据总线、FIR 滤波器的一组系数。VHDL 用 **数组类型（array type）** 来描述它。但标准 VHDL 有一个长期痛点：在 VHDL-2002 及以前，`array of std_logic_vector` 这种「元素本身又是一个不定宽向量」的类型很难直接声明。

Open Logic 用 **VHDL-2008**（u1-l1 已说明这是项目硬性前提）解决了这个问题，并在 `pkg_array` 里提供两类数组类型：

1. **定宽元素数组**：`StlvArray2_t`、`StlvArray3_t` …… 一直到 `StlvArray64_t`、`StlvArray512_t`。每个类型的元素是一个固定宽度的 `std_logic_vector`，适合直接用作实体端口。
2. **不定宽元素数组**（VHDL-2008 特性）：`StlvArray_t`（元素是任意宽 `std_logic_vector`）、`UnsignedArray_t`、`SignedArray_t`。元素宽度在实例化时再约束。
3. **标量数组**：`IntegerArray_t`、`RealArray_t`、`BoolArray_t`，元素分别是整数、实数、布尔。

这个包看似简单，却是其他包的「类型地基」：`pkg_math` 里的 `count`、`maxArray`、`fromString` 都以 `IntegerArray_t` / `RealArray_t` / `BoolArray_t` 为参数。所以 `pkg_array` 必须先于 `pkg_math` 编译。

#### 4.1.2 核心流程

`pkg_array` 除了类型声明，还提供 5 个转换函数，最关键的是「展平（flatten）」与「反展平（unflatten）」：

- **flatten**：把一个「等宽元素的数组」按顺序拼成一条一维 `std_logic_vector`。常用于把多通道并行数据写进单口 RAM。
- **unflatten**：反向操作，把一条一维位向量按给定 `elementSize` 切回数组。常用于从 RAM 读出后还原成多通道。

流程伪代码：

```
flattenStlvArray(a):
    ElementSize  = a(0) 的位宽
    ElementCount = a 的元素个数
    Flat = 长度 = ElementCount * ElementSize 的位向量
    for i in 0 .. ElementCount-1:
        Flat[(i+1)*ElementSize-1 : i*ElementSize] = a(i)
    return Flat

unflattenStlvArray(a, elementSize):
    ElementCount = a.length / elementSize
    Unflat = 数组(0..ElementCount-1)，每元素 elementSize 位
    for i in 0 .. ElementCount-1:
        Unflat(i) = a[(i+1)*elementSize-1 : i*elementSize]
    return Unflat
```

#### 4.1.3 源码精读

类型声明集中在包头部。注意定宽数组从 2 位一直到 64 位、另有 512 位，覆盖常见总线宽度；标量数组与不定宽数组在最后：

- [olo_base_pkg_array.vhd:L29-L69](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_array.vhd#L29-L69) —— 声明全部数组类型。其中 `StlvArray_t`（第 67 行）正是 VHDL-2008 才允许的「元素为不定宽 `std_logic_vector`」的写法。

`flattenStlvArray` 的实现很短，关键是用 `a(0)'length` 反推每个元素的位宽，再循环拼接：

- [olo_base_pkg_array.vhd:L124-L135](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_array.vhd#L124-L135) —— `flattenStlvArray`，循环把每个元素写入目标位向量的对应切片。

`unflattenStlvArray` 是镜像操作，注意它声明返回数组时同时约束了范围与元素宽度：

- [olo_base_pkg_array.vhd:L137-L147](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_array.vhd#L137-L147) —— `unflattenStlvArray`，按 `elementSize` 切片还原数组。

另外三个小转换函数：`arrayInteger2Real`（整数数组转实数数组）、`arrayStdl2Bool` / `arrayBool2Stdl`（位向量与布尔数组互转），实现都很直白，可在 [olo_base_pkg_array.vhd:L84-L122](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_array.vhd#L84-L122) 自行阅读。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：确认 `pkg_array` 是 `pkg_math` 的类型基础，理解依赖顺序的由来。
2. **步骤**：
   - 打开 `olo_base_pkg_math.vhd`，找到它的 `use work.olo_base_pkg_array.all;`（包体顶部）。
   - 在 `pkg_math` 头部搜索 `IntegerArray_t`、`RealArray_t`、`BoolArray_t`，看看哪些函数以它们为参数（如 `count`、`maxArray`、`fromString` 返回 `RealArray_t`）。
   - 打开 `compile_order.txt`，确认 `olo_base_pkg_array`（第 4 行）排在 `olo_base_pkg_math`（第 5 行）之前。
3. **需要观察的现象**：`pkg_math` 离开 `pkg_array` 提供的数组类型就无法编译，因为它的函数签名直接引用了这些类型。
4. **预期结果**：你能用一句话说明「array 必须先于 math 编译」的原因——math 的函数签名借用了 array 的类型。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `StlvArray_t`（元素是不定宽 `std_logic_vector`）在 VHDL-2002 下不能写，而 Open Logic 可以？

> **答案**：把「不定宽类型」作为数组元素需要 VHDL-2008 的 unconstrained array element 特性。Open Logic 全库要求 VHDL-2008（u1-l1），所以可以直接用，既省去了为每个宽度都单独声明一个类型的麻烦，又能在端口上传递任意宽度的多通道数据。

**练习 2**：`flattenStlvArray` 要求所有元素等宽。如果传入不等宽数组会发生什么？

> **答案**：它用 `a(0)'length` 作为所有元素的位宽并据此分配目标向量。若其他元素更宽会被截断、更窄会留下未赋值的位，行为不正确。因此调用方必须保证等宽——这正是「不定宽数组」使用时需要开发者自行约束的责任边界。

---

### 4.2 pkg_math 与 pkg_logic：数学与位级逻辑函数

#### 4.2.1 概念说明

这两个包提供「纯函数」——没有时钟、没有状态，输入决定输出，既能在仿真里调用，也能在综合时被求值成常量。它们最大的价值是**在声明区（declarative region）做编译期计算**：

- 用 `log2ceil(Depth_g+1)-1 downto 0` 自动推导 FIFO 水位信号的位宽，省去手算。
- 用 `choose(UseReady_g, '1', '0')` 把布尔泛型翻译成具体信号初值。
- 用 `binaryToGray` 把二进制地址转成格雷码，供异步 FIFO 跨时钟域安全传递指针。

`pkg_math` 偏「数与类型」，`pkg_logic` 偏「位操作」。两者都 `use work.olo_base_pkg_math.all`（logic 依赖 math 的 `log2ceil`），所以归在本节一起讲。

`pkg_math` 的典型函数：

| 函数 | 作用 |
| --- | --- |
| `log2` / `log2ceil` | 整数（或 real）的以 2 为底对数，`log2ceil` 向上取整 |
| `isPower2` | 判断是否 2 的幂 |
| `greatestCommonFactor` / `leastCommonMultiple` | 最大公约数 / 最小公倍数 |
| `max` / `min` | 取二者较大/较小（重载 integer / real） |
| `choose` | 三目运算：`s` 为真返回 `t`，否则 `f`（重载 8 种类型） |
| `count` | 统计某值在数组/位向量中出现次数 |
| `toUslv` / `toSslv` / `toStdl` | 整数转无符号/有符号位向量 / 转 std_logic |
| `fromUslv` / `fromSslv` / `fromStdl` | 反向转换 |
| `fromString` | 字符串转 real 或 RealArray_t |
| `maxArray` / `minArray` | 数组中的最大/最小值 |

`pkg_logic` 的典型函数：

| 函数 | 作用 |
| --- | --- |
| `zerosVector` / `onesVector` | 生成全 0 / 全 1 位向量 |
| `shiftLeft` / `shiftRight` | 带 `fill` 填充的移位（负数自动反向） |
| `binaryToGray` / `grayToBinary` | 二进制 ↔ 格雷码 |
| `ppcOr` | 并行前缀 OR（如 `0100 → 0111`） |
| `to01` / `to01X` | 把 `'Z'/'X'/'-'` 等归一化到 `'0'/'1'/'X'` |
| `invertBitOrder` / `invertByteOrder` | 翻转位序 / 字节序 |
| `getLeadingSetBitIndex` / `getTrailingSetBitIndex` | 找最高/最低置位 bit 的下标 |
| `Polynomial_Prbs*_c` | PRBS2~PRBS32 的多项式常量 |

#### 4.2.2 核心流程

**`log2` 与 `log2ceil` 的实现思路**是「反复除以 2 数次数」，避免依赖浮点 `ieee.math_real`（注意 `pkg_math` 虽然 `use ieee.math_real.all`，但 `log2` 整数版本身是纯整数实现）：

- `log2(arg)`：地板值，`arg=8` → 3，`arg=5` → 2。
- `log2ceil(arg)`：天花板值。它复用一个巧妙的恒等式——对 `arg>0`：

  \[
  \lceil \log_2 n \rceil = \lfloor \log_2 (2n - 1) \rfloor
  \]

  所以 `log2ceil(arg) = log2(arg*2 - 1)`。对 `arg=0` 做特例返回 0（数学上应为 \(-\infty\)），目的是让 `log2ceil(0)-1` 不产生负宽度，从而允许「零长度数组」合法存在而非编译报错。

**格雷码（Gray code）** 的核心是相邻两个数只有 1 位不同，跨时钟域传递时即使被采样到中间值也最多错 1 位，不会出现多位同时跳变导致的「毛刺值」。转换关系：

- 二进制 → 格雷：\( g_i = b_i \oplus b_{i+1} \)，最高位不变。一行 `xor` 即可。
- 格雷 → 二进制：\( b_i = g_i \oplus b_{i+1} \)，需要从高位向低位依次异或（有数据依赖，故用循环）。

**`choose`** 本质就是函数化的三目运算符 `s ? t : f`，之所以要重载 8 种类型，是因为 VHDL 函数不支持 C++ 那样的模板泛型，每种返回类型都得单独写一份实现。

#### 4.2.3 源码精读

`log2` 用整数除法数次数：

- [olo_base_pkg_math.vhd:L185-L197](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_math.vhd#L185-L197) —— `log2`，循环 `ArgShift_v := ArgShift_v / 2` 直到为 1，每轮计数加一。

`log2ceil` 直接套用上面的恒等式，并对 0 特判：

- [olo_base_pkg_math.vhd:L200-L206](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_math.vhd#L200-L206) —— `log2ceil(arg: natural)`，`return log2(arg * 2 - 1)`。

`choose` 的重载族——每个类型一份几乎相同的实现，这里看 `std_logic` 版作为代表：

- [olo_base_pkg_math.vhd:L305-L315](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_math.vhd#L305-L315) —— `choose` 的 `std_logic` 重载，`if s then return t; else return f;`。

整数与位向量互转的「语法糖」函数，目的是省去反复写 `std_logic_vector(to_unsigned(...))`：

- [olo_base_pkg_math.vhd:L460-L465](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_math.vhd#L460-L465) —— `toUslv`，一行封装 `to_unsigned`。
- [olo_base_pkg_math.vhd:L486-L489](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_math.vhd#L486-L489) —— `fromUslv`，一行封装 `to_integer(unsigned(...))`。

`binaryToGray` 一行 `xor` 完成：

- [olo_base_pkg_logic.vhd:L167-L172](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_logic.vhd#L167-L172) —— `binaryToGray`，`binary xor ('0' & binary(高位 downto 低位+1))`。

`grayToBinary` 因有数据依赖需循环，从最高位向低位逐位异或：

- [olo_base_pkg_logic.vhd:L175-L186](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_logic.vhd#L175-L186) —— `grayToBinary`，`Binary_v(b) := gray(b) xor Binary_v(b + 1)`。

PRBS 多项式常量集中声明，供 `olo_base_prbs` 等实体直接引用，省去用户查表：

- [olo_base_pkg_logic.vhd:L79-L109](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_logic.vhd#L79-L109) —— `Polynomial_Prbs2_c` 到 `Polynomial_Prbs32_c`，每一位 `1` 代表 LFSR 抽头位置。

**真实使用现场**——异步 FIFO 把这些函数串成了完整功能：

- [olo_base_fifo_async.vhd:L27-L29](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_async.vhd#L27-L29) —— 一次性 `use` 了 math、logic、string 三个包。
- [olo_base_fifo_async.vhd:L61](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_async.vhd#L61) —— `In_Level : out std_logic_vector(log2ceil(Depth_g+1)-1 downto 0)`，用 `log2ceil` 由 `Depth_g` 推导水位信号位宽。
- [olo_base_fifo_async.vhd:L233-L238](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_async.vhd#L233-L238) —— 读写地址经 `binaryToGray` 跨时钟域，对侧再用 `grayToBinary` 还原。

#### 4.2.4 代码实践

本节是本讲的主实践，对应规格里的任务：在 `pkg_math` 与 `pkg_logic` 中各找两个函数，写一个小测试打印并校验其返回值。

Open Logic 已经自带 `olo_base_pkg_math_tb` 与 `olo_base_pkg_logic_tb`，它们用 VUnit 的 `check_equal` 校验、用 `run("...")` 分离用例。我们分两步走。

**第一步：运行已有测试台，确认环境通。**

1. **目标**：在 `sim/` 目录用 GHDL 跑通 `olo_base_pkg_math_tb`，确认函数行为与文档一致。
2. **操作步骤**（沿用 u1-l4 的运行器）：
   ```bash
   cd sim
   python run.py --ghdl olo.olo_base_pkg_math_tb
   ```
   > 说明：VUnit 的测试过滤名形如「库名.TB 名[.用例名]」。`olo_base_pkg_math_tb` 声明为 `-- vunit: run_all_in_same_sim`，故其下多个 `run("log2")`、`run("log2ceil-int")` 等用例会在同一次仿真里顺序执行。**精确的过滤字符串以本地 VUnit 输出为准，待本地验证。**
3. **需要观察的现象**：仿真日志里每个用例打印 `check_equal ... passed`，最后 `test_suite` 全绿退出。
4. **预期结果**：例如 `log2(8)=3`、`log2ceil(5)=3`、`log2ceil(0)=0`（特例）等断言全部通过。

**第二步：动手扩展，打印返回值。**

参照已有 TB 写一个最小 demo（**示例代码**，非仓库原有文件），同时打印与校验 `log2` / `log2ceil` / `binaryToGray` / `grayToBinary`：

```vhdl
-- 示例代码：u2_l1_pkg_demo_tb.vhd（放到 test/base/ 下并注册到 test_configs 才能被 run.py 发现）
library ieee;
    use ieee.std_logic_1164.all;
    use ieee.numeric_std.all;
library vunit_lib;
    context vunit_lib.vunit_context;
library olo;                         -- 单库策略：生产代码与包都在 olo 库
    use olo.olo_base_pkg_math.all;
    use olo.olo_base_pkg_logic.all;

entity u2_l1_pkg_demo_tb is
    generic (runner_cfg : string);
end entity;

architecture sim of u2_l1_pkg_demo_tb is
begin
    p_demo : process is
        variable Gray_v : std_logic_vector(3 downto 0);
    begin
        test_runner_setup(runner, runner_cfg);

        -- pkg_math
        info("log2(8)        = " & integer'image(log2(8)));          -- 期望 3
        info("log2ceil(1000) = " & integer'image(log2ceil(1000)));   -- 期望 10
        check_equal(log2(8), 3);
        check_equal(log2ceil(1000), 10);

        -- pkg_logic：4 = 0100，格雷码应为 0110（=6）
        Gray_v := binaryToGray(std_logic_vector(to_unsigned(4, 4)));
        info("binaryToGray(0100) = " & to_hstring(Gray_v));          -- 期望 6
        check_equal(grayToBinary(Gray_v),
                    std_logic_vector(to_unsigned(4, 4)));            -- 往返一致

        test_runner_cleanup(runner);
    end process;
end architecture;
```

1. **目标**：亲眼看到函数返回值被打印，并用 `check_equal` 自动校验。
2. **操作步骤**：把文件放到 `test/base/`，在 `sim/test_configs/olo_base.py` 里参照其它 TB 注册一行（具体写法见 u10-l2）；再 `python run.py --ghdl olo.olo_base_pkg_demo_tb` 运行。若不想改配置，也可直接复用第一步的已有 TB，在某个 `run("...")` 分支里加两行 `info(...)` 即可。
3. **需要观察的现象**：仿真日志打印出 `log2(8) = 3`、`log2ceil(1000) = 10`、`binaryToGray(0100) = 6`。
4. **预期结果**：`check_equal` 全部通过，`grayToBinary(binaryToGray(x)) = x` 往返一致。
5. 若本地未装 GHDL/VUnit，**待本地验证**——可只阅读 `olo_base_pkg_math_tb.vhd` 的断言来理解行为（见下方源码引用）。

参照已有 TB 的写法：

- [olo_base_pkg_math_tb.vhd:L56-L68](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_pkg_math/olo_base_pkg_math_tb.vhd#L56-L68) —— `run("log2")` 与 `run("log2ceil-int")` 用例，`check_equal(log2(8), 3)` 等。注意它 `use olo.olo_base_pkg_math.all` 与 `use olo.olo_base_pkg_array.all` 都来自 `library olo`，印证了单库编译策略。

#### 4.2.5 小练习与答案

**练习 1**：用 `log2ceil` 推导「表示 0~999 共 1000 个值」所需的最小位宽，写出 VHDL 声明并说明结果。

> **答案**：`signal Idx : std_logic_vector(log2ceil(1000)-1 downto 0);`。`log2ceil(1000) = log2(1999)`，`1999 → 999 → 499 → 249 → 124 → 62 → 31 → 15 → 7 → 3 → 1` 共 10 次除法，结果为 10，故位宽 10 位（\(2^{10}=1024 \ge 1000\)）。

**练习 2**：`binaryToGray` 只需一行 `xor`，而 `grayToBinary` 却要用循环。为什么？

> **答案**：二进制转格雷时每个格雷位 \(g_i\) 只依赖两个二进制位 \(b_i, b_{i+1}\)，无依赖、可并行。格雷转二进制时 \(b_i = g_i \oplus b_{i+1}\)，而 \(b_{i+1}\) 本身又依赖 \(g_{i+1} \oplus b_{i+2}\)，存在从最高位向低位的链式数据依赖，只能顺序求值，故用循环。

**练习 3**：`choose(boolean, T, T)` 已经重载了 8 种 `T`。如果调用 `choose(true, 5, 3.0)`（一个 integer、一个 real）会发生什么？

> **答案**：编译报错。重载决议要求 `t` 与 `f` 同类型，没有「integer/real 混用」的重载版本。调用方需先把两者统一成同一类型（例如都写 `5.0` 与 `3.0`）再调用。

---

### 4.3 pkg_string：字符串处理

#### 4.3.1 概念说明

`pkg_string` 提供 VUnit/VHDL 标准库缺失的字符串工具：大小写转换、去空白、大小写不敏感比较、十六进制串转位向量、字符计数、错误消息拼装。

它最实际的使用场景是**解析字符串型泛型**。Open Logic 在 `fix` 区域（u8-l1 会详讲）为了能被 Verilog 实例化，把自定义类型（如定点格式）用字符串泛型传递；而 `intf` / `base` 区域也有一些「用字符串选择行为」的泛型，例如 `olo_base_fifo_async` 的 `Optimization_g`，需要大小写不敏感地与 `"LATENCY"` 比较——这正是 `compareNoCase` 的用武之地。

#### 4.3.2 核心流程

`compareNoCase(a, b)` 的流程是「先各自 `trim` 去空白、再 `toUpper` 统一大写、最后比较相等」：

```
compareNoCase(a, b) := (toUpper(trim(a)) = toUpper(trim(b)))
```

`hex2StdLogicVector(a, bits, hasPrefix)` 的流程：

1. `trim` + `toLower` 规范化输入。
2. 若 `hasPrefix`，校验前两个字符是 `"0x"` 并跳过。
3. 逐字符查表把 `0-9/a-f` 转成 4 位 nibble，左移拼进结果。
4. 按 `bits` 截断或零扩展到目标宽度。

关于**循环依赖**的设计取舍：`pkg_string` 里的 `errorMessage(name, message)` 是个很方便的「拼错误字符串」函数。你会自然期望 `pkg_math` / `pkg_logic` 在 `assert` 里也用它。但源码故意不用，而是手写 `report "olo_base_pkg_math.fromStdl(): Illegal argument"`。原因：`pkg_string` 已经 `use work.olo_base_pkg_math.all`（因为 `hex2StdLogicVector` 用到 `max` 与 `choose`），如果 `pkg_math` 反过来 `use pkg_string`，就形成 `math ↔ string` 的循环依赖，VHDL 不允许。源码注释把这一点写得很清楚：

- [olo_base_pkg_math.vhd:L500-L504](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_math.vhd#L500-L504) —— `fromStdl` 里手写 `report`，注释说明「不能用 `errorMessage()` 以避免与 `pkg_string` 的循环依赖」。
- [olo_base_pkg_logic.vhd:L294-L297](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_logic.vhd#L294-L297) —— `invertByteOrder` 同样手写 `report`，同样的循环依赖注释。

#### 4.3.3 源码精读

`compareNoCase` 一行实现，复用 `toUpper` + `trim`：

- [olo_base_pkg_string.vhd:L104-L109](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_string.vhd#L104-L109) —— `compareNoCase`。

`hex2StdLogicVector` 用 `max` 与 `choose` 计算最大可能位数（这正是 `pkg_string` 依赖 `pkg_math` 的原因）：

- [olo_base_pkg_string.vhd:L132-L200](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_string.vhd#L132-L200) —— `hex2StdLogicVector`，逐字符 `case` 转换 nibble 再拼接。

`errorMessage` 极简，把名字与消息用 `" - "` 连起来：

- [olo_base_pkg_string.vhd:L221-L226](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_string.vhd#L221-L226) —— `errorMessage`。

**真实使用现场**——异步 FIFO 用 `compareNoCase` 比较字符串泛型 `Optimization_g`：

- [olo_base_fifo_async.vhd:L273-L274](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_async.vhd#L273-L274) —— `compareNoCase(Optimization_g, "LATENCY")`，按字符串泛型选择 RAM 写地址是否寄存。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：理解字符串泛型如何被解析成硬件行为差异。
2. **步骤**：
   - 打开 `olo_base_fifo_async.vhd`，找到第 273-274 行的 `compareNoCase(Optimization_g, "LATENCY")`。
   - 阅读它所在的三目表达式：当 `Optimization_g="LATENCY"` 时用 `ri.WrAddr`（当前地址，低延迟），否则用 `ri.WrAddrReg`（寄存过的地址）。
   - 在 `olo_base_fifo_async.vhd` 头部找到 `Optimization_g` 泛型的声明与合法取值说明。
3. **需要观察的现象**：一个字符串泛型通过 `compareNoCase` 被翻译成两条不同的 RTL 连线，且大小写不敏感（用户写 `"latency"` 也能生效）。
4. **预期结果**：你能画出「`Optimization_g` 字符串 → `compareNoCase` 布尔结果 → 三目选择 → RAM 写地址来源」这条调用链。
5. **待本地验证**：可进一步在 TB 里分别用 `"LATENCY"` 与另一种取值仿真，对比 RAM 写地址的延迟差异。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `compareNoCase` 内部要先 `trim` 再比较？

> **答案**：VHDL 字符串字面量与泛型常带尾随空格（VHDL 的 `string` 是定长，赋值短串会补空格）。先 `trim` 能让 `"LATENCY"`、`"LATENCY  "`、`"  latency"` 都被判等，提升泛型对用户的容错性。

**练习 2**：假如要让 `pkg_math` 也能调用 `errorMessage`，又不破坏依赖关系，可以怎么调整？

> **答案**：把 `errorMessage`（以及它依赖的纯字符串函数）下沉到一个不依赖 `pkg_math` 的新底层包（如 `pkg_string_base`），让 `pkg_math` 与 `pkg_string` 都 `use` 它即可打破环。Open Logic 当前的取舍是「不值得为这一个函数新开包」，直接手写 `report` 更简单——这是典型的「依赖清洁」与「代码简洁」之间的工程权衡。

---

### 4.4 pkg_attribute：跨厂商综合属性封装

#### 4.4.1 概念说明

综合属性（synthesis attribute）用来「告诉综合工具如何推断硬件」。例如「不要把这一排寄存器合并进 SRL（移位寄存器 LUT）」「把这两个寄存器当异步同步器对待，别优化掉」「别改动这个信号」。痛点在于：**不同厂商的属性名与取值类型都不一样**。

| 用途 | Vivado (AMD) | Quartus (Altera) | Synplify/Efinity/Gowin |
| --- | --- | --- | --- |
| 抑制 SRL 提取 | `shreg_extract="no"` | — | `syn_srlstyle="registers"` |
| 同步器寄存器 | `async_reg=true` | — | `async_reg=true`（Efinity） |
| 禁止信号改动 | `dont_touch=true` / `keep="yes"` | `dont_merge=true` / `preserve=true` | `syn_keep=1` / `syn_preserve=1` |
| RAM/ROM 风格 | `ram_style` / `rom_style` | `ramstyle` / `romstyle` | `syn_ramstyle` / `syn_romstyle` |

如果每个实体都直接写厂商属性，代码就被绑死在某一家的工具上，违背 Open Logic「Pure VHDL、厂商无关」的哲学（u1-l1）。

`pkg_attribute` 的解法很优雅：**把所有厂商的属性一次性声明出来，再配上语义化的常量名**。实体代码只引用语义常量（如 `ShregExtract_SuppressExtraction_c`），由工具各取所需——**厂商工具会自动忽略它不认识的属性**。于是一份 VHDL 能同时被 Vivado / Quartus / Efinity / Gowin / Synplify 综合，且每个工具都拿到自己能识别的那几条属性。

注意一个细节：`syn_keep` / `syn_preserve` 被声明为 `integer` 而非 `boolean`。源码注释解释：虽然文档说支持 boolean，但 Gowin 只接受 integer，而 Synopsys/Efinity 实测也接受 integer，于是选了兼容性最广的 integer。这是「跨厂商兼容」需要做出的具体取舍。

#### 4.4.2 核心流程

`pkg_attribute` 的使用流程是「声明属性 → 定义配套常量 → 实体里把属性绑到信号上」：

1. 包里声明 `attribute shreg_extract : string;` 并定义 `constant ShregExtract_SuppressExtraction_c : string := "no";`。
2. 实体里 `use work.olo_base_pkg_attribute.all;`。
3. 在信号声明后写 `attribute shreg_extract of VldReg : signal is ShregExtract_SuppressExtraction_c;`。

**包体是空的**（只有 `end package body;`）——这个包只做声明，不含任何逻辑，所以也不会引入额外依赖（它只 `use ieee`，这就是它能在 `compile_order.txt` 排第 1 行的原因）。

#### 4.4.3 源码精读

属性与常量声明按用途分组，每组都注释了适用工具：

- [olo_base_pkg_attribute.vhd:L30-L36](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_attribute.vhd#L30-L36) —— `shreg_extract` 及其两个常量（Vivado 专用）。
- [olo_base_pkg_attribute.vhd:L55-L58](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_attribute.vhd#L55-L58) —— `async_reg`（Vivado / Efinity），用于异步同步器寄存器。
- [olo_base_pkg_attribute.vhd:L76-L79](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_attribute.vhd#L76-L79) —— `syn_keep : integer`，注释说明为何选 integer（Gowin 兼容）。
- [olo_base_pkg_attribute.vhd:L100-L119](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_attribute.vhd#L100-L119) —— RAM/ROM 风格属性，覆盖 Vivado / Quartus / Efinity / Synplify / Gowin。

空包体，证明此包无逻辑、无依赖：

- [olo_base_pkg_attribute.vhd:L126-L128](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_attribute.vhd#L126-L128) —— `package body` 为空。

**真实使用现场**——`olo_base_pl_stage`（u1-l5 读过的流水线寄存器）对同一对寄存器 `VldReg` / `DataReg` 一次性绑定了 6 个厂商属性：

- [olo_base_pl_stage.vhd:L249-L266](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L249-L266) —— 对 `VldReg` / `DataReg` 绑定 `shreg_extract`、`syn_srlstyle`、`dont_merge`、`preserve`、`syn_keep`、`syn_preserve`，全部用语义常量赋值。
- [olo_base_pl_stage.vhd:L138](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L138) —— `use work.olo_base_pkg_attribute.all;`，引入这些属性。

#### 4.4.4 代码实践（源码阅读型）

1. **目标**：理解「一份 VHDL、多厂商属性」如何落地，并解释为何 `pl_stage` 要抑制 SRL 提取。
2. **步骤**：
   - 打开 `olo_base_pl_stage.vhd` 第 249-266 行，数一下 `VldReg` 上绑了几个属性，分别属于哪几家厂商。
   - 对照 `olo_base_pkg_attribute.vhd` 找到每个常量的定义（如 `ShregExtract_SuppressExtraction_c := "no"`）。
   - 思考：`pl_stage` 是「单级流水线寄存器」，`Valid` 与 `Data` 必须严格对齐在同一拍。如果综合工具把 `DataReg` 优化进 SRL（用 LUT 当移位寄存器），会引入额外延迟、打乱握手时序，所以必须抑制。
3. **需要观察的现象**：6 个属性里，Vivado 认识 `shreg_extract`，Quartus 认识 `dont_merge`/`preserve`，Efinity/Gowin 认识 `syn_srlstyle`/`syn_keep`/`syn_preserve`——每个工具各取所需，其余被忽略。
4. **预期结果**：你能解释「为什么抑制 SRL 提取」对带反压的流水线寄存器是必须的，并说出这套封装如何让 `pl_stage` 跨厂商可综合。
5. **待本地验证**：若有 Vivado，可分别用「绑定属性」与「注释掉属性」综合 `pl_stage`，对比资源报告中是否出现 SRL。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `pkg_attribute` 的包体是空的？把它做成空包体有什么好处？

> **答案**：它只声明属性类型与常量，没有任何需要被求值的逻辑，所以包体为空。好处是它不依赖任何其它 `work` 包（只 `use ieee`），可以排在 `compile_order.txt` 第 1 行，被所有实体安全引用，也不会卷入任何循环依赖。

**练习 2**：实体里写了 `attribute shreg_extract of VldReg : signal is ShregExtract_SuppressExtraction_c;`，但当前综合用的是 Quartus（不认识 `shreg_extract`）。会发生什么？

> **答案**：Quartus 会忽略它不认识的 `shreg_extract` 属性，转而识别同一条 `attribute` 列表里它认识的 `dont_merge` / `preserve` / `syn_keep` / `syn_preserve`（这些是为 Quartus/Synplify 系准备的）。这正是「一次声明、各取所需」设计的目标——同一份代码无需 `ifdef` 即可在多家工具下都得到「别动这对寄存器」的等效效果。

---

## 5. 综合实践

把本讲五个包串起来，完成一个「迷你地址译码器」的源码阅读 + 小设计任务：

**任务背景**：你要写一个实体，输入是一个 `Depth_g` 深度的 FIFO 的水位 `Level`，输出「是否快满了」标志与「水位二进制对应的格雷码」（用于跨时钟域透传）。

**要求**：

1. 端口宽度全部用 `pkg_math` 的 `log2ceil` 由 `Depth_g` 推导，不手写数字。
2. 用 `pkg_math` 的 `choose` 根据布尔泛型 `AlmFullEn_g` 决定 `AlmFull` 是否生效（不启用时恒为 `'0'`）。
3. 用 `pkg_logic` 的 `binaryToGray` 把水位转成格雷码输出。
4. 用 `pkg_array` 的 `IntegerArray_t` 在内部保存一组阈值，用 `maxArray` 取其中最大值。
5. 给关键寄存器绑定 `pkg_attribute` 里的 `ShregExtract_SuppressExtraction_c`，确保不被优化进 SRL。

**参考骨架（示例代码）**：

```vhdl
-- 示例代码：综合实践骨架，仅说明五个包如何协同，非仓库原有文件
library ieee;
    use ieee.std_logic_1164.all;
    use ieee.numeric_std.all;
library work;
    use work.olo_base_pkg_math.all;
    use work.olo_base_pkg_logic.all;
    use work.olo_base_pkg_array.all;
    use work.olo_base_pkg_attribute.all;

entity mini_level_gray is
    generic (
        Depth_g     : positive := 16;
        AlmFullEn_g : boolean  := true
    );
    port (
        Clk        : in  std_logic;
        Rst        : in  std_logic;
        Level      : in  std_logic_vector(log2ceil(Depth_g+1)-1 downto 0); -- pkg_math 推宽度
        AlmFull    : out std_logic;
        LevelGray  : out std_logic_vector(Level'range)
    );
end entity;

architecture rtl of mini_level_gray is
    constant Thresholds_c : IntegerArray_t(0 to 2) := (Depth_g-1, Depth_g*3/4, Depth_g/2); -- pkg_array
    constant MaxThresh_c  : integer := maxArray(Thresholds_c);                            -- pkg_math
    signal LevelReg       : std_logic_vector(Level'range);
    attribute shreg_extract of LevelReg : signal is ShregExtract_SuppressExtraction_c;     -- pkg_attribute
begin
    process(Clk) begin
        if rising_edge(Clk) then
            if Rst = '1' then
                LevelReg <= (others => '0');
            else
                LevelReg <= Level;
            end if;
        end if;
    end process;

    -- pkg_math: choose 把布尔泛型翻译成信号
    AlmFull   <= choose(AlmFullEn_g, '1', '0') when fromUslv(LevelReg) >= MaxThresh_c else '0';
    -- pkg_logic: 二进制转格雷
    LevelGray <= binaryToGray(LevelReg);
end architecture;
```

**验收**：

- 编译顺序正确（`pkg_array` → `pkg_math` → `pkg_logic` 都在 `pkg_attribute` 之后、本实体之前）。
- 改 `Depth_g` 时端口宽度自动跟随，无需改代码。
- 仿真：令 `Level` 从 0 递增到 `Depth_g-1`，观察 `LevelGray` 相邻值只差 1 位（格雷码性质），`AlmFull` 在越过 `MaxThresh_c` 时拉起。
- 若 `AlmFullEn_g=false`，`AlmFull` 恒为 `'0'`（验证 `choose` 生效）。
- 综合属性：综合报告中 `LevelReg` 未被吸收进 SRL（**待本地验证**，需对应厂商工具）。

## 6. 本讲小结

- Open Logic 的五个 `base` 公共包是全库的「工具箱与类型地基」：`pkg_array` 提供数组类型、`pkg_math`/`pkg_logic` 提供编译期可求值的纯函数、`pkg_string` 处理字符串泛型、`pkg_attribute` 封装跨厂商综合属性。
- 包之间有清晰的依赖链 `array → math → {logic, string}`，`attribute` 独立无依赖；这条链直接决定了 `compile_order.txt` 的前几行顺序。`pkg_math`/`pkg_logic` 故意不调用 `pkg_string.errorMessage()`，以避免循环依赖。
- `log2ceil` 是最常见的「由泛型推导端口宽度」工具，对 0 特判返回 0 以支持零长度数组；`binaryToGray`/`grayToBinary` 服务于异步 FIFO 的跨时钟域指针传递，二者一个并行一个需循环，源于数据依赖差异。
- `choose`、`max`/`min`、`count` 等函数通过重载覆盖多种类型，本质是把三目运算、取极值、计数等小操作函数化，让声明区代码更可读。
- `pkg_attribute` 用「一次声明全部厂商属性 + 语义常量 + 工具各取所需」的方式，让同一份 VHDL 跨 Vivado/Quartus/Efinity/Gowin/Synplify 可综合，包体为空、无依赖、可安全排第一编译。
- 这些包都有配套 VUnit 测试台（如 `olo_base_pkg_math_tb`），用 `check_equal` + `run("...")` 用例化校验，是学习函数行为的最佳入口。

## 7. 下一步学习建议

- **下一讲 u2-l2（流水线阶段与 AXI-S 握手）**：本讲引用的 `olo_base_pl_stage` 将被深入拆解，你会看到 `pkg_attribute` 绑定的那对寄存器如何配合两进程法与 shadow 寄存器实现带反压的流水线。建议先复习 u1-l5 的两进程法与 AXI-S 握手。
- **继续阅读源码**：`olo_base_fifo_async.vhd` 是这五个包的「集大成消费者」，通读它能把 `log2ceil`/`binaryToGray`/`grayToBinary`/`compareNoCase` 的真实用法一次性串起来。
- **横向扩展**：学完本讲后，遇到任何 `olo_base_*` 实体里的常量计算、端口宽度推导、字符串泛型解析，都可以回到这五个包里找对应函数；之后进入 u4（跨时钟域）时会再次见到 `binaryToGray` 的核心地位。
