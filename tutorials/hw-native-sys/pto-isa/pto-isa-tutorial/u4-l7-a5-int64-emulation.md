# A5 的 64 位向量运算仿真：int64/uint64 位运算指令

## 1. 本讲目标

本版本（提交 `771b6cc1`，feat(a5): support int64 and uint64 ISA bitwise operations）为 A5 后端补齐了 int64/uint64 的**位运算**能力：TAND/TOR/TXOR/TANDS/TORS/TXORS/TNOT 新增 int64/uint64 支持，TABS 新增 int64 支持。学完本讲，你应该能够：

1. 说出 A5 向量部件为什么"做不了"原生 64 位运算，以及 PTO 用「一对 32 位寄存器（low/high）」仿真 64 位运算的完整思路。
2. 独立走读 `Int64Binary` → `Int64BinaryRepeat` → `Int64BinaryCalcRegs` → `vsts` 的完整数据流，说清 `vlds(DINTLV_B32)` 交织装载、`vintlv` 再交织、`vsts(NORM_B32)` 分半存储各自在哪一步、为什么必须这么做。
3. 解释谓词掩码如何在「按 32 位 lane 计数」的硬件约束下同时服务 64 位元素的有效区域裁剪，以及 `pintlv_b32` 为什么要把一份掩码切成 `lowMask/highMask` 两份。
4. 掌握一个 dtype 能力落地时的"四件套同步"：Check 白名单的 `static_assert`、`gen_data.py` 数据、`main.cpp` 比对方式、`docs/isa` 类型表的联动更新；并理解为什么 CPU 模拟器用通用 C++ 模板就能"天然"给出 64 位 golden，而 A5 需要一条专门的寄存器对仿真路径。

## 2. 前置知识

### 2.1 小端存储与 64 位数的"高低半字"

昇腾（和绝大多数主流 CPU）采用小端字节序：一个 64 位整数在内存中按"低位字节在前"存放。把它看成两个连续的 32 位字（word）时：

```text
内存（地址递增 →）：  [ low 32 位字 ][ high 32 位字 ]
lane 编号（32 位）：   [ 偶数位字     ][ 奇数位字     ]
```

- `low`：低 32 位，即模 \( 2^{32} \) 的那半；
- `high`：高 32 位，包含符号位（int64）或最高有效位（uint64）。

把 64 位运算拆到 32 位通道上，本质就是分别处理 `low/high` 两个半字，并在需要时在两者之间传递**进位/借位/符号**信息。

### 2.2 向量寄存器、lane 与谓词掩码（承接 u4-l2/u4-l3）

- A5 的向量寄存器一次持有 `CCE_VL = 256` 字节（仓库内 costmodel 桩件中的定义见后文）。对 32 位元素而言，一个向量寄存器就是 **64 个 32 位 lane**。
- CCE 内建指令（`vadd`/`vxor`/`vlds`/`vsts` 等）按 lane 并行工作；`MaskReg`（谓词寄存器，A5 上是 `vector_bool`）按 lane 置位，决定哪些 lane 参与运算/存储。
- `plt_b32(scalar, POST_UPDATE)`：生成"前 `scalar` 个 32 位 lane 有效"的谓词，并递减标量计数器（`POST_UPDATE` 的语义在 [tcvt_common.hpp:L131-L137](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/arch/register/tcvt_common.hpp#L131-L137) 的注释中有明确说明）。

### 2.3 承接前序讲义的结论

- u4-l1：指令分层是 User API → `*_IMPL`（含 Check）→ `__tf__` TF 层 → CCE 内建；签名在 `common/pto_instr.hpp`，实现按后端归位。
- u4-l3：a2a3 后端用"算子结构体 + TF 层 + 编译期 `static_assert` 检查"的三层结构实现指令；dtype 白名单在各后端的 Check 函数里，A2A3 与 A5 并不相同。
- u2-l1：`PTO_INST`/`PTO_INTERNAL`/`__tf__` 等宏让同一份代码在两种编译器下都合法；"CPU 跑通不等于全后端合法"。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `include/pto/npu/a5/TBinOp.hpp` | 64 位仿真的核心：`Int64Op` 枚举、`Int64BinaryCalcRegs`（寄存器对运算）、`Int64BinaryRepeat`（交织装载/分半存储）、`Int64Binary`（循环骨架） |
| `include/pto/npu/a5/TXor.hpp` | TXOR 的 TF 层与 `TXOR_IMPL`，64 位分流入口之一（二元位运算代表） |
| `include/pto/npu/a5/TAnd.hpp` / `TOr.hpp` | 与 TXor 同构的 TAND/TOR 分流入口 |
| `include/pto/npu/a5/TUnaryOp.hpp` | `Int64UnaryRepeat`/`Int64Unary`（一元 64 位路径），TNOT/TABS 的分流与白名单 |
| `include/pto/npu/a5/TBinSOp.hpp` | 标量变体路径：`Int64ScalarCalcRegs`/`Int64ScalarRepeat`/`Int64Scalar`（TANDS/TORS/TXORS 等走这里） |
| `include/pto/npu/a5/TXorS.hpp` | TXORS 的 TF 层分流入口（标量位运算代表） |
| `include/pto/npu/a5/common.hpp` | `MaskReg`、`CreatePredicate`（按 `sizeof(T)` 选 `plt_b8/b16/b32`） |
| `tests/npu/a5/src/st/testcase/txor/` | TXOR 的 A5 ST 四件套（gen_data.py / txor_kernel.cpp / main.cpp / CMakeLists.txt），含本版本新增的 int64/uint64 用例 |
| `docs/isa/TXOR.md` | TXOR 的 ISA 文档，A5 约束表已同步列出 int64/uint64 |
| `include/pto/cpu/ElementOp.h` / `ElementTileOp.h` | CPU 模拟器的对照实现：通用模板 `dst = src0 ^ src1`，64 位天然成立 |

## 4. 核心概念与源码讲解

### 4.1 Int64Op 枚举：64 位仿真的统一参数化入口

#### 4.1.1 概念说明

A5 的 64 位运算不是"一条指令一个实现"，而是**一族结构完全相同的实现**：装载（拆半）→ 运算（在 low/high 寄存器对上）→ 存储（拼回）。装载和存储对所有运算通用，真正随运算变化的只有中间那步。于是 PTO 把"做哪种运算"抽象成一个编译期枚举 `Int64Op`，用模板参数分发——这是典型的"用 `if constexpr` 链代替虚函数/switch"的零开销分发。

本版本的关键改动就是给这个枚举**扩容**：

```text
旧：enum class Int64Op { Add, Sub, Mul, Shl, Shr, Max, Min };
新：enum class Int64Op { Add, Sub, Mul, Shl, Shr, Max, Min, And, Or, Xor, Not, Abs };
```

此前该枚举只服务算术/比较/移位族（TADD/TSUB/TMUL/TSHL/TSHR/TMAX/TMIN 及其 S 后缀标量变体）；本版本新增 `And/Or/Xor/Not/Abs` 五个值，把位运算也纳入同一条仿真链路。

#### 4.1.2 核心流程

指令分流全景（`Int64Binary<`/`Int64Unary<`/`Int64Scalar<` 的全部调用点，均可 grep 复现）：

| 公共指令 | 入口文件 | 64 位分支 | 承载函数 |
| --- | --- | --- | --- |
| TADD / TSUB / TMUL / TSHL / TSHR / TMAX / TMIN | `a5/TAdd.hpp` 等 | `Int64Binary<Int64Op::Add/Sub/Mul/Shl/Shr/Max/Min,...>` | 既有能力 |
| **TAND / TOR / TXOR**（本版本新增 64 位） | `a5/TAnd.hpp` `TOr.hpp` `TXor.hpp` | `Int64Binary<Int64Op::And/Or/Xor,...>` | 二元位运算 |
| TADDS/TSUBS/TMULS/TSHLS/TSHRS/TMAXS/TMINS | `a5/TAddS.hpp` 等 | `Int64Scalar<Int64Op::...,...>` | 既有标量族 |
| **TANDS / TORS / TXORS**（本版本新增 64 位） | `a5/TAndS.hpp` `TOrS.hpp` `TXorS.hpp` | `Int64Scalar<Int64Op::And/Or/Xor,...>` | 标量位运算 |
| **TNOT**（本版本新增 64 位） | `a5/TUnaryOp.hpp` | `Int64Unary<Int64Op::Not,...>` | 一元位运算 |
| **TABS**（本版本新增 int64） | `a5/TUnaryOp.hpp` | `Int64Unary<Int64Op::Abs,...>` | 一元算术 |

分流的判断条件在所有入口都一样：

```cpp
if constexpr (std::is_same_v<T, int64_t> || std::is_same_v<T, uint64_t>) {
    // 走 64 位寄存器对仿真路径
} else {
    // 走原有 ≤32 位 lane 的通用路径
}
```

#### 4.1.3 源码精读

枚举定义只有一行，位于 [TBinOp.hpp:L21](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TBinOp.hpp#L21)：

```cpp
enum class Int64Op { Add, Sub, Mul, Shl, Shr, Max, Min, And, Or, Xor, Not, Abs };
```

以 TXOR 为例看分流。TF 层入口 [TXor.hpp:L36-L53](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TXor.hpp#L36-L53) 先把 tile 句柄换成 UB 指针，再按 dtype 二选一：

```cpp
if constexpr (std::is_same_v<T, int64_t> || std::is_same_v<T, uint64_t>) {
    Int64Binary<Int64Op::Xor, T, TileDataDst::Cols, TileDataSrc0::Cols, TileDataSrc1::Cols>(
        dstPtr, src0Ptr, src1Ptr, validRows, validCols);
} else {
    BinaryInstr<XorOp<T>, ...>(dstPtr, src0Ptr, src1Ptr, validRows, validCols, version);
}
```

注意 64 位分支**不接收 `version`（VFImplKind）参数**——`version` 是为 ≤32 位路径的多种向量化实现（1D/2D、POST_UPDATE 与否）选择的，而 64 位路径只有一种实现形态，无需再选。TAND 的分流完全同构，见 [TAnd.hpp:L46-L52](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TAnd.hpp#L46-L52)。

一元侧以 TNOT 为例，[TUnaryOp.hpp:L247-L259](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TUnaryOp.hpp#L247-L259) 中 64 位走 `Int64Unary<Int64Op::Not,...>`，其余类型走通用 `TUnaryOp<DstTile, SrcTile, NotOp<T>>`；TABS 同理（[TUnaryOp.hpp:L356-L368](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TUnaryOp.hpp#L356-L368)），但只对 `int64_t` 分流——uint64 取绝对值无意义，白名单里也没有它。

还有一个值得注意的工程细节：这套 64 位代码被 `#if defined(PTO_NPU_ARCH_A5) || defined(PTO_NPU_ARCH_A6)` 包住（[TBinOp.hpp:L363](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TBinOp.hpp#L363)、[TBinOp.hpp:L501-L505](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TBinOp.hpp#L501-L505)）。A6 经头文件级复用 A5（承接 u1-l5），所以同样享有 64 位能力；而 kirin9030/kirinX90 没有 64 位内建指令，`#else` 分支只留**声明**不留定义——因为指令头里的调用发生在被 `if constexpr` 丢弃的分支里，模板两阶段查找只需要声明能通过名字查找，永远不会真正实例化。这个设计在 [TUnaryOp.hpp:L195-L202](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TUnaryOp.hpp#L195-L202) 的注释中写得很清楚。

#### 4.1.4 代码实践

**实践目标**：亲手建立"指令 → 64 位路径"的分流全景表，验证本讲 4.1.2 的表格。

**操作步骤**：

1. 在仓库根目录执行（只读操作）：

   ```bash
   grep -rn "Int64Binary<\|Int64Unary<\|Int64Scalar<" include/pto/npu/a5/
   ```

2. 把命中结果按 `Int64Op::` 后的运算名归类，标注哪些是本版本新增的 `And/Or/Xor/Not/Abs`。
3. 任选一个未命中的位运算类指令（例如 TFMod），打开其头文件确认它确实没有 64 位分支。

**需要观察的现象**：命中行应覆盖 TAdd/TSub/TMul/TShl/TShr/TMax/TMin/TAnd/TOr/TXor 十个二元指令、对应十个标量变体，以及 TUnaryOp.hpp 内的 Not/Abs 两处；TAndS/TOrS/TXorS 走的是 `Int64Scalar`。

**预期结果****（待本地验证）**：共 22 处命中，与本讲表格一致；未接入的指令对 int64 tile 实例化时会被 Check 层的 `static_assert` 拦下（见 4.5）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Int64Op` 用模板参数（`template <Int64Op Op>`）而不是运行期函数参数传递？

**答案**：分发发生在 `if constexpr` 链上（见 4.2.3 的 `Int64BinaryCalcRegs`），编译器只为实际用到的 `Op` 实例化对应分支，其余分支被丢弃——零运行期开销，也不要求"未支持运算的 CCE 指令"可编译。若改成运行期 switch，所有分支都要通过编译，Kirin 等无 64 位内建的架构会直接编译失败，4.1.3 末尾的"声明即可"技巧也会失效。

**练习 2**：TABS 为什么不像 TNOT 一样同时支持 `int64_t` 和 `uint64_t`？

**答案**：`uint64_t` 恒非负，取绝对值是恒等变换，没有实现价值；而 `int64_t` 取绝对值需要判断符号位（在 high 半字的最高位）、做 64 位取反加一，正是 4.2.3 中 `Int64AbsRegs` 借 `vsubc/vsubcs` 传播借位的原因。白名单（`TABS_IMPL` 的 `static_assert`，[TUnaryOp.hpp:L373-L378](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TUnaryOp.hpp#L373-L378)）据此只放行了 `int64_t`。

### 4.2 高低寄存器对仿真：Int64BinaryCalcRegs

#### 4.2.1 概念说明

**为什么需要仿真？** A5 向量部件的 ALU 通路按最宽 32 位的 lane 处理数据。一个直接的源码证据是 ≤32 位通用路径里的类型映射：`XorOp<T>` 把操作数统一重解释为 8/16/32 位无符号寄存器，**32 位封顶、没有任何 64 位分支**（[TXor.hpp:L23-L31](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TXor.hpp#L23-L31)）：

```cpp
using U = std::conditional_t<
    sizeof(T) == sizeof(uint8_t), uint8_t,
    std::conditional_t<sizeof(T) == sizeof(uint16_t), uint16_t, uint32_t>>;
// vxor((RegTensor<U>&)dstReg, ...) —— 64 位类型根本没有对应的 U
```

CCE 内建指令族里也没有 64 位 lane 的按位运算。所以 64 位运算只能拆成"两个 32 位半字各自进 lane"。**为什么可行？** 因为对纯位运算（AND/OR/XOR/NOT），两个半字之间没有进位、也没有符号语义差异，`low`、`high` 可以完全独立地各算各的：

\[ \mathrm{dst}_{63..0} = \mathrm{src0}_{63..0} \oplus \mathrm{src1}_{63..0} \iff \begin{cases} \mathrm{dstLow} = \mathrm{src0Low} \oplus \mathrm{src1Low} \\ \mathrm{dstHigh} = \mathrm{src0High} \oplus \mathrm{src1High} \end{cases} \]

这正是本版本新增位运算落到 `Int64BinaryCalcRegs` 后代码格外短的原因——两行 `vxor` 完事。相比之下，既有的算术分支必须处理跨半字信息：加法用 `vaddc`（带进位加）+ `vaddcs`（进位链加），乘法用 `vmull`（32×32→64）+ 两次 `vmula` 累加交叉项。

#### 4.2.2 核心流程

一次 64 位二元位运算（以 TXOR 为例）在寄存器层的伪代码：

```text
输入：src0Low/src0High、src1Low/src1High（各为一个 256B 向量寄存器，
      low 的 lane i = 第 i 个元素的低 32 位，high 的 lane i = 同一元素的高 32 位）

vxor(dstLow,  src0Low,  src1Low)    # 低半字独立异或
vxor(dstHigh, src0High, src1High)   # 高半字独立异或

输出：dstLow/dstHigh —— 待 4.3 的交织存储拼回 UB
```

标量变体（TXORS/TANDS/TORS）多一步"标量拆半广播"：把 64 位标量按位重解释为 `uint64_t`，低/高 32 位各 `vbr`（broadcast）成一个寄存器，然后走同样的运算。

#### 4.2.3 源码精读

核心分发函数 [Int64BinaryCalcRegs，TBinOp.hpp:L433-L462](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TBinOp.hpp#L433-L462)，本版本新增的三个位运算分支位于 [TBinOp.hpp:L450-L458](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TBinOp.hpp#L450-L458)：

```cpp
} else if constexpr (Op == Int64Op::And) {
    vand((vector_u32&)dstLow,  (vector_u32&)src0Low,  (vector_u32&)src1Low,  mask, MODE_ZEROING);
    vand((vector_u32&)dstHigh, (vector_u32&)src0High, (vector_u32&)src1High, mask, MODE_ZEROING);
} else if constexpr (Op == Int64Op::Or) {
    vor(...);   // 同上，两半独立
} else if constexpr (Op == Int64Op::Xor) {
    vxor((vector_u32&)dstLow,  (vector_u32&)src0Low,  (vector_u32&)src1Low,  mask, MODE_ZEROING);
    vxor((vector_u32&)dstHigh, (vector_u32&)src0High, (vector_u32&)src1High, mask, MODE_ZEROING);
}
```

三个细节值得圈出来：

1. **强转 `vector_u32&`**：位运算不关心符号，统一用无符号 32 位寄存器视角，避免有符号比较/饱和语义介入。
2. **`MODE_ZEROING`**：谓词未选中的 lane 写零（而非保持原值），保证结果寄存器的无效 lane 是确定的。
3. **`mask` 对 low/high 各用一次**：同一份谓词能同时裁剪两个半字——为什么一份 32 位 lane 粒度的谓词恰好等于"64 位元素粒度"，见 4.4。

对照既有算术分支可感受差异：`Add` 分支（[TBinOp.hpp:L438-L440](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TBinOp.hpp#L438-L440)）先 `vaddc` 算低半字并产出进位谓词 `carry`，再 `vaddcs` 把 `carry` 吃进高半字加法——高低半字之间有一条真实的进位链。`Int64AbsRegs`（[TBinOp.hpp:L97-L108](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TBinOp.hpp#L97-L108)）则是 TNOT 的"算术版亲戚"：`vcmp_lt(srcHigh, 0)` 检测符号位、`vsubc/vsubcs` 做 0−x 的 64 位减法（借位链）、`vsel` 按符号挑选原值或相反数。而 `Not` 在一元路径里就是两条独立的 `vnot`（[TUnaryOp.hpp:L166-L169](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TUnaryOp.hpp#L166-L169)）。

标量变体的拆半广播在 [TBinSOp.hpp:L77-L98](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TBinSOp.hpp#L77-L98)：

```cpp
uint64_t scalarBits = static_cast<uint64_t>(scalar);
vbr(scalarLow,  static_cast<int32_t>(scalarBits));        // 低 32 位广播
vbr(scalarHigh, static_cast<int32_t>(scalarBits >> 32));  // 高 32 位广播
```

之后的运算分发在 [Int64ScalarCalcRegs，TBinSOp.hpp:L23-L58](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TBinSOp.hpp#L23-L58)，其中 Xor 分支（[TBinSOp.hpp:L52-L54](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TBinSOp.hpp#L52-L54)）与二元版逐字相同，只是 `src1Low/src1High` 换成了广播出来的标量半字。

#### 4.2.4 代码实践

**实践目标**：用宿主机 Python 验证"高低半字独立位运算 ≡ 64 位位运算"，把 4.2.1 的数学式落到数值上。

**操作步骤**：

1. 任意有 Python3 的机器上执行（示例代码，非项目文件）：

   ```python
   a = 0x123456789ABCDEF0
   b = 0x0F0F0F0F0F0F0F0F
   # 拆半
   al, ah = a & 0xFFFFFFFF, a >> 32
   bl, bh = b & 0xFFFFFFFF, b >> 32
   # 半字独立异或（模拟 vxor 两次）
   print(hex(al ^ bl), hex(ah ^ bh))
   # 与 64 位直接异或对照
   print(hex(a ^ b))
   ```

2. 把 `^` 换成 `&`、`|` 再各跑一遍；换成 `+` 观察"半字独立相加"何时出错（缺进位链），对照 `vaddc/vaddcs` 存在的意义。

**需要观察的现象**：AND/OR/XOR 三种位运算下，`(ah^bh)<<32 | (al^bl)` 与 `a^b` 完全一致；而加法在 `al+bl` 超过 \( 2^{32} \) 时丢失进位，与 64 位加法结果不一致。

**预期结果**：位运算恒等、算术需要进位链——这正是 `Int64Op::Xor` 分支只要两条 `vxor`、`Int64Op::Add` 分支却要 `vaddc`+`vaddcs` 的原因。

#### 4.2.5 小练习与答案

**练习 1**：`Int64MulRegs`（[TBinOp.hpp:L61-L68](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TBinOp.hpp#L61-L68) 里有一条同构实现）用 `vmull` 一次、`vmula` 两次，为什么乘法需要"1+2"三条指令？

**答案**：\( (a_l + 2^{32} a_h)(b_l + 2^{32} b_h) = a_l b_l + 2^{32}(a_l b_h + a_h b_l) + 2^{64} a_h b_h \)。`vmull` 算 \( a_l b_l \) 得到完整的 64 位低积；两个 `vmula` 分别把交叉项 \( a_l b_h \)、\( a_h b_l \) 累加到高半字；\( 2^{64} \) 项自然溢出舍弃。位运算没有这种展开项，所以两条指令即可。

**练习 2**：如果把 `vector_s32&` 换成有符号比较参与 `Int64MinMax`（[TBinOp.hpp:L407-L430](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TBinOp.hpp#L407-L430)），`int64_t` 与 `uint64_t` 的处理差在哪？

**答案**：低半字必须始终按无符号比（`vector_u32&` 强转），因为它是纯位模式；高半字则分类型——`int64_t` 用有符号 `vcmp_gt/vcmp_lt`（符号位参与大小关系），`uint64_t` 用无符号比较。代码里的 `if constexpr (std::is_same_v<T, int64_t>)` 分支正是干这个的。

### 4.3 交织装载与分半存储：Int64BinaryRepeat 的数据流

#### 4.3.1 概念说明

4.2 解决了"寄存器里怎么算"，本模块解决**数据怎么进出寄存器**。矛盾在于两侧的数据组织形式不同：

- **UB（片上缓冲）侧**：64 位元素是完整连续存放的，即 `[元素0.low][元素0.high][元素1.low][元素1.high]...`，低半字与高半字交错出现；
- **运算侧**：希望 `src0Low` 寄存器的 64 个 lane 装满 64 个元素的低半字、`src0High` 装满高半字（这样 4.4 里一份谓词才能同时裁剪两半）。

如果用普通 `vlds` 装载，得到的是"交织在同一个寄存器里"的布局，无法直接送 `vxor`。CCE 提供了配套的三件内建指令来完成"解交织装载 → 运算 → 再交织存储"的闭环：

| 内建指令 | 角色 |
| --- | --- |
| `vlds(dst0, dst1, ptr, offset, DINTLV_B32)` | **解交织装载**：一次读两个寄存器宽度（2×256B）的 32 位字，偶数位字流入 `dst0`、奇数位字流入 `dst1`——恰好把每个 64 位元素的 low/high 分进两个寄存器 |
| `vintlv(half0, half1, dstLow, dstHigh)` | **再交织**：`vlds(DINTLV_B32)` 的逆操作，把 low/high 两个寄存器按内存顺序拼回两个"可直接连续存储"的寄存器 |
| `vsts(reg, ptr, offset, NORM_B32, mask)` | **分半存储**：以 32 位普通分布把一个 256B 寄存器写回 UB，配合谓词掩码只写有效元素 |

（`DINTLV_B32`/`NORM_B32` 的精确位级约定属于 CCE 内建指令接口，在仓库内以使用点为准，本讲按其在数据流中的行为理解即可。）

#### 4.3.2 核心流程

一次 `Int64BinaryRepeat` 处理某一行中连续

\[ \text{elementsPerRepeat} = \frac{\text{CCE\_VL} \times 2}{\text{sizeof}(T)} = \frac{256 \times 2}{8} = 64 \text{ 个 int64 元素} \]

即 512 字节——恰好两个 256B 向量寄存器。完整六步（以 TXOR、`row` 行、`colOffset` 列偏移为参数）：

```text
① 算元素偏移（单位：int32 字！）
     src0Offset = (row * Src0Cols + colOffset) * 2
     dstOffset  = (row * DstCols  + colOffset) * 2
   —— colOffset 以 int64 元素计，×2 换算成 int32 字单位
② vlds(src0Low, src0High, src0, src0Offset, DINTLV_B32)   # 解交织装载源0
③ vlds(src1Low, src1High, src1, src1Offset, DINTLV_B32)   # 解交织装载源1
④ Int64BinaryCalcRegs<Xor>(dstLow, dstHigh, ...)          # 4.2 的寄存器对运算
⑤ pintlv_b32(lowMask, highMask, mask, mask)               # 4.4 的掩码切分
   vintlv(half0, half1, dstLow, dstHigh)                  # 再交织回内存顺序
⑥ vsts(half0, dst, dstOffset,              NORM_B32, lowMask)
   vsts(half1, dst, dstOffset + CCE_VL/4,  NORM_B32, highMask)   # 第二半落在 +256B
```

外层 `Int64Binary`（[TBinOp.hpp:L482-L500](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TBinOp.hpp#L482-L500)）做两层循环：`row` 从 0 到 `validRows`，每个 `row` 内按 `elementsPerRepeat` 切列、逐个调用 `Int64BinaryRepeat`，并为每轮重新构造谓词。

#### 4.3.3 源码精读

装载与存储的全部细节浓缩在 [Int64BinaryRepeat，TBinOp.hpp:L464-L480](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TBinOp.hpp#L464-L480)：

```cpp
uint32_t src0Offset = (row * Src0Cols + colOffset) * 2;
uint32_t src1Offset = (row * Src1Cols + colOffset) * 2;
uint32_t dstOffset  = (row * DstCols  + colOffset) * 2;
vlds(src0Low, src0High, (__ubuf__ int32_t*)src0, src0Offset, DINTLV_B32);
vlds(src1Low, src1High, (__ubuf__ int32_t*)src1, src1Offset, DINTLV_B32);
Int64BinaryCalcRegs<Op, T>(dstLow, dstHigh, src0Low, src0High, src1Low, src1High, mask);
pintlv_b32(lowMask, highMask, mask, mask);
vintlv(half0, half1, dstLow, dstHigh);
vsts(half0, (__ubuf__ int32_t*)dst, dstOffset, NORM_B32, lowMask);
vsts(half1, (__ubuf__ int32_t*)dst, dstOffset + CCE_VL / sizeof(int32_t), NORM_B32, highMask);
```

四个要点：

1. **指针视角换成 `int32_t*`**：源 tile 虽然是 `T=int64_t`，但装载/存储都以 32 位字为粒度操作，所以强转成 `__ubuf__ int32_t*`，偏移单位随之是"字"——这就是 ① 中 `* 2` 的来历（一个 int64 元素 = 2 个字）。
2. **`CCE_VL / sizeof(int32_t)` = 64**：第二个 `vsts` 的落点比第一个偏后 64 个字（= 256B）。两个 256B 存储拼出 512B 的完整结果区。`CCE_VL` 在仓库 costmodel 桩件中定义为 256（[a5_vf_stub.hpp:L149](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/costmodel/a5/cce_costmodel/a5_vf_stub.hpp#L149)），kirinDev0000 代码注释也印证了"a5 的 CCE_VL = 256"。
3. **装载不带掩码**：两条 `vlds` 一次读满 512B（含有效区外的数据），裁剪推迟到运算（`mask`）与存储（`lowMask/highMask`）两侧完成，装载永远整宽。
4. **循环骨架**：[Int64Binary，TBinOp.hpp:L482-L500](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TBinOp.hpp#L482-L500) 中 `colRepeats = CeilDivision(validCols, elementsPerRepeat)`，`__VEC_SCOPE__` 圈定向量寄存器作用域，每轮用 `sreg = validCols` 重建谓词（见 4.4）。

一元路径完全同构，只是少一个源：[Int64UnaryRepeat，TUnaryOp.hpp:L157-L176](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TUnaryOp.hpp#L157-L176) 一条 `vlds` 解交织装载，`Not` 分支两条 `vnot`、否则 `Int64AbsRegs`，随后同样的 `pintlv_b32 → vintlv → 两条 vsts`。外层 [Int64Unary，TUnaryOp.hpp:L178-L194](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TUnaryOp.hpp#L178-L194) 与二元版共享同一套循环/谓词写法。

#### 4.3.4 代码实践

**实践目标**：用 4 个 int64 元素手工推演"UB 内存 ↔ 寄存器"的映射，画出 mini 版示意图（综合实践的前置热身）。

**操作步骤**：

1. 写下 4 个 int64 十六进制值，例如 `A=0x1111111122222222`、`B=0x3333333344444444`、`C=0x5555555566666666`、`D=0x7777777788888888`。
2. 按小端拆半，画出 UB 中的 int32 字序列：`22222222,11111111,44444444,33333333,...`。
3. 按解交织规则把偶数位字分入 `low`、奇数位字分入 `high`，写出两个"寄存器"的 lane 0..3。
4. 各 lane 独立做 `^`（与另一个操作数同样拆好的寄存器），再交织回去，对照 Python 的 `^` 结果。

**需要观察的现象**：`low` 寄存器的 lane i 恰是元素 i 的低 32 位、`high` 的 lane i 是同一元素的高 32 位；交织回去后恢复"low,high,low,high..."的内存顺序。

**预期结果**：手工结果与 4.2.4 的 Python 直接异或一致；并直观理解"为什么一份按 32 位 lane 计数的谓词在解交织视图里就等于按 64 位元素计数"。

#### 4.3.5 小练习与答案

**练习 1**：为什么第二个 `vsts` 的偏移是 `dstOffset + CCE_VL/sizeof(int32_t)` 而不是 `dstOffset + elementsPerRepeat`？

**答案**：两者数值上恰好都等于 64，但语义不同：`vsts` 的偏移单位是"int32 字"（指针是 `int32_t*`），`CCE_VL/sizeof(int32_t)` = 256/4 = 64 **个字**，明确表达"跳过一个寄存器宽度（256B）"；`elementsPerRepeat` 是 int64 元素个数，只有在本路径 T=8 字节的场景下才数值相同。写前者不会在语义上误导后续维护者。

**练习 2**：`vlds` 不带谓词、整宽装载，会不会读到 tile 有效区域之外甚至 UB 越界？

**答案**：会读到有效元素之外的 tile 内容（尾块场景），但这无害——无效 lane 会在运算与存储两侧被掩码裁掉。至于"越出 tile 容量"：`Int64Binary` 的循环以 `CeilDivision(validCols, elementsPerRepeat)` 切列，装载范围落在 `(容量列数 + elementsPerRepeat)` 张成的区域内，而 tile 容量约束（`ValidCol ≤ Cols` 等）已由 Check 层 `static_assert` 前置保证（见 4.5.3 与 u2-l3）。真正要警惕的是用户手写 `TASSIGN` 时按 64 位元素数而非字节数估算 UB 用量——tile 占用是 `Rows×Cols×8` 字节（承接 u2-l4 的容量规划）。

### 4.4 谓词掩码切分：一份掩码的两次变身

#### 4.4.1 概念说明

A5 的谓词寄存器按 32 位 lane 置位（`plt_b8/b16/b32` 按 `sizeof(T)` 选择，**没有 b64**，见 [common.hpp（a5）:L42-L60](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/common.hpp#L42-L60)）。64 位路径的巧妙之处在于：**解交织之后，这个"缺陷"自动消失**——`srcLow` 的 lane i 就是元素 i 的低半字、`srcHigh` 的 lane i 是同一元素的高半字，于是一个"前 k 个 lane 有效"的谓词天然就是"前 k 个 64 位元素有效"，`vxor(dstLow,...,mask)` 与 `vxor(dstHigh,...,mask)` 用**同一份** `mask` 即可。

但到了存储侧，布局又变了：`vintlv` 产出的 `half0/half1` 里，每个元素的两个半字回到了相邻位置（元素横跨两个 lane）。同一份 `mask` 不再对齐存储粒度，所以需要 `pintlv_b32(lowMask, highMask, mask, mask)` 把元素粒度的谓词按与 `vintlv` 相同的交织规则重新编排，得到与 `half0/half2` 两个存储寄存器对齐的 `lowMask/highMask`——这就是"谓词掩码切分"。

#### 4.4.2 核心流程

```text
每个 repeat：
    sreg = validCols                      # 以 64 位元素计的剩余有效数
    preg  = CreatePredicate<uint32_t>(sreg)
            └─ plt_b32(sreg, POST_UPDATE) # 前 sreg 个 32 位 lane 置位（并递减 sreg）
    [运算侧]  mask 直接用于 low/high 两个寄存器的 vxor/vand/vor/vnot
    [存储侧]  pintlv_b32(lowMask, highMask, mask, mask)
              └─ 按交织规则展开成两份，分别驱动 half0 / half1 的 vsts
```

尾块（`validCols` 不是 64 整数倍时的最后一个 repeat，或整行不足 64 列）不需要特殊代码路径：`plt_b32(validCols)` 自然只点亮前 `validCols` 个 lane，装载多读的部分被存储掩码裁掉。

#### 4.4.3 源码精读

谓词构造的分流在 [common.hpp（a5）:L42-L60](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/common.hpp#L42-L60)：

```cpp
if constexpr (sizeof(T) == 1)      { reg = plt_b8(scalar, POST_UPDATE); }
else if constexpr (sizeof(T) == 2) { reg = plt_b16(scalar, POST_UPDATE); }
else if constexpr (sizeof(T) == 4) { reg = plt_b32(scalar, POST_UPDATE); }
```

64 位路径固定以 `CreatePredicate<uint32_t>` 调用（[TBinOp.hpp:L494](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TBinOp.hpp#L494)、[TUnaryOp.hpp:L189](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TUnaryOp.hpp#L189)）——刻意选择 32 位粒度，正是为了与"解交织后 lane=元素"的布局对齐。

循环里每轮把 `sreg` 重置为 `validCols` 再重建谓词（[TBinOp.hpp:L491-L497](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TBinOp.hpp#L491-L497)）：虽然 `POST_UPDATE` 会递减标量计数器（语义见 [tcvt_common.hpp:L131-L137](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/arch/register/tcvt_common.hpp#L131-L137) 的注释），但这里的计数语义是"每轮从行首重新数"，所以每轮重建、不依赖上轮递减结果。

掩码切分只占一行（[TBinOp.hpp:L476](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TBinOp.hpp#L476)、[TUnaryOp.hpp:L172](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TUnaryOp.hpp#L172)）：

```cpp
pintlv_b32(lowMask, highMask, mask, mask);
```

此外 `TBinOp.hpp` 还提供了两个掩码辅助件，属于这条路径的"配套工具箱"：

- `Int64TailMask`（[TBinOp.hpp:L36-L41](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TBinOp.hpp#L36-L41)）：`cols == 0` 时直接复用整宽掩码，否则 `plt_b32(cols, POST_UPDATE)` 生成显式尾掩码——供需要单独尾块处理的调用点使用。
- `Int64MaskPatternOffset`（[TBinOp.hpp:L23-L33](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TBinOp.hpp#L23-L33)）：把 `MaskPattern`（`P1010/P0100` 等 sub-block 谓词模式）映射成 lane 偏移（0/1/2/3），服务于更早的"宽 int64 标量掩码"修复（提交 `f3c52912`），本讲了解其存在即可。

#### 4.4.4 代码实践

**实践目标**：把"尾块靠谓词自然裁剪"看懂，并确认新增用例确实覆盖了尾块。

**操作步骤**：

1. 打开 [tests/npu/a5/src/st/testcase/txor/txor_kernel.cpp:L60-L63](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/src/st/testcase/txor/txor_kernel.cpp#L60-L63)，找到 4 个 64 位实例化：`<int64_t,4,16,4,15>`、`<uint64_t,4,16,4,15>`、`<int64_t,32,32,32,32>`、`<int64_t,1,1024,1,1024>`。
2. 对每个用例计算 `colRepeats = ceil(validCols/64)` 与最后一轮 `sreg`，填出下表并推演 `plt_b32(sreg)` 点亮哪些 lane：

   | 用例 | validCols | colRepeats | 最后一轮 sreg | 尾块？ |
   | --- | --- | --- | --- | --- |

3. 回答：`case_int64_1x1024_1x1024` 中 `plt_b32(1024)` 会怎样？

**需要观察的现象**：`4x16` 用例 `colRepeats=1`、`sreg=15`，只在 64 个 lane 中点亮前 15 个——典型的"整行不足一个 repeat"；`1x1024` 用例 `sreg=1024` 超过 lane 总数 64。

**预期结果**（表内数值待你本地推演核对）：`4x15` 是尾块用例；`32x32` 每轮 sreg=32（半个寄存器）；`1x1024` 的谓词计数超过 lane 数时按整寄存器全亮处理（饱和语义，与"16 个 repeat × 64 元素 = 1024"的切分一致）。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `pintlv_b32` 那一行删掉、两个 `vsts` 都直接用 `mask`，最可能坏掉的是哪个用例？

**答案**：带尾块的 `case_int64_4x16_4x15`。整宽 repeat（谓词全亮）下 `mask` 与 `lowMask/highMask` 往往同为全 1，错误被掩盖；而尾块场景下 `mask` 只在前 15 个 lane 置位，与交织后存储寄存器的 lane 布局不对齐，会导致部分有效半字没写出或无效半字写进 GM，golden 比对失败。这也是测试用例设计里"必须包含非整 repeat 形状"的原因。

**练习 2**：为什么 `Int64Binary` 每轮重建 `preg`，而不是像某些 ≤32 位路径那样利用 `POST_UPDATE` 的自动递减推进地址与掩码？

**答案**：`Int64BinaryRepeat` 的偏移由 `(row, colRepeat*elementsPerRepeat)` 显式计算（无 POST_UPDATE 式指针游标），谓词计数的语义是"从行首数起的有效列数"，每个 repeat 内所有 lane 的有效上限相同（都是 `validCols`），因此每轮用 `sreg = validCols` 重建即可；递减式推进反而要额外处理"跨行重置"，徒增状态。

### 4.5 A5 ST 用例与文档同步：一次 dtype 落地的四件套

#### 4.5.1 概念说明

给一条指令增加一个 dtype，看似只加一个 `if constexpr` 分支，实际要同步四处，任何一处缺失都会留下"实现有了、质量门没跟上"的缺口。本版本的 TXOR int64/uint64 落地恰好是完整范例：

1. **Check 白名单**：`TXorCheck` 的 `static_assert` 加上 `int64_t/uint64_t`（编译期拦截非法 dtype）；
2. **golden 数据**：`gen_data.py` 新增 64 位分支（含高位激励与满尺寸输入的讲究）；
3. **比对方式**：`main.cpp` 对 64 位整数改用**精确比对** `ResultCmpExact`，而不是带容差的 `ResultCmp<T>`；
4. **ISA 文档**：`docs/isa/TXOR.md` 的 A5 约束表补上 int64/uint64（中英文同步）。

同时还要回答一个关键问题：**CPU 模拟器为什么不需要任何改动就能给出 64 位 golden？**

#### 4.5.2 核心流程

```text
用户侧 TXOR(dst, src0, src1, tmp, events)          ← common/pto_instr.hpp 公共包装
    └─ MAP_INSTR_IMPL → TXOR_IMPL(dst, src0, src1, tmp)   ← a5/TXor.hpp
          ├─ TXorCheck：static_assert 白名单 + PTO_ASSERT 有效形状
          └─ TXor<...>：64 位分流 → Int64Binary → ...
ST 验证闭环：
    gen_data.py 生成 input1/input2/golden（numpy 64 位 ^）
    → main.cpp 拷入 GM、Launch、拷回、ResultCmpExact 精确比对
```

#### 4.5.3 源码精读

**（1）Check 白名单。** [TXorCheck，TXor.hpp:L59-L63](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TXor.hpp#L59-L63) 的类型白名单现已包含 `uint64_t/int64_t`；同一函数还强制行主布局（L64-66）与三 tile 同 dtype（L67-69），运行期 `PTO_ASSERT` 校验 src0/src1 有效形状与 dst 一致（L72-77）。TNOT/TABS 的对应更新见 [TUnaryOp.hpp:L263-L269](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TUnaryOp.hpp#L263-L269)（TNOT 加入 64 位）与 [TUnaryOp.hpp:L373-L378](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TUnaryOp.hpp#L373-L378)（TABS 加入 int64）。对照 ISA 文档 [TXOR.md:L47-L61](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/TXOR.md#L47-L61)：A5 行列出八种类型（含 int64/uint64），A2A3 行只有六种（≤32 位）——**同一公共指令，各后端白名单不同**，这正是 u2-l1"CPU 跑通不等于全后端合法"论断的又一例证（反向：A5 合法的 int64 在 A2A3 上非法）。

**（2）公共包装与 tmp 的去向。** 公共 API [pto_instr.hpp:L488-L496](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr.hpp#L488-L496) 的 `TXOR` 签名带 `TileDataTmp& tmp`（A2A3 需要 tmp 暂存分解式异或的中间量），A5 的 `TXOR_IMPL`（[TXor.hpp:L80-L89](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TXor.hpp#L80-L89)）接收但不使用它——文档在 [TXOR.md:L73-L75](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/TXOR.md#L73-L75) 明确说明该参数仅为 API 兼容保留。ST 内核里的写法 `TXOR(dstTile, src0Tile, src1Tile, dstTile /*not used*/, event0)`（[txor_kernel.cpp:L40](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/src/st/testcase/txor/txor_kernel.cpp#L40)）即以 dst 占位。

**（3）gen_data.py 的 64 位分支。** [gen_data.py:L24-L39](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/src/st/testcase/txor/gen_data.py#L24-L39) 有两个讲究（注释写在 L25-L27）：

```python
# Full-tile arrays so the written binary sizes match main.cpp's
# kTRows_*kTCols_ read. Use large values so the high 32-bit half of
# each 64-bit element is exercised.
```

其一，**生成满 tile 尺寸的输入**：main.cpp 按 `kTRows_*kTCols_` 整块读文件，若只生成 `valid` 区域大小的数据会读越界；golden 数组满尺寸置零、只有前 `h_valid*w_valid` 个元素填入 `input1 ^ input2`（L37-39），与内核"只写有效区"的行为精确对齐。其二，**激励取满 32 位幅值**（int64 用 `[-2^31, 2^31-1]`、uint64 用 `[0, 2^32-1]`），确保 high 半字不是全 0/全 1——若高位恒零，`vlds` 解交织、`vxor` 高半字、`vsts` 分半存储中的任何错位都可能侥幸通过比对。新用例清单在 [gen_data.py:L81-L84](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/src/st/testcase/txor/gen_data.py#L81-L84)。

**（4）main.cpp 的精确比对。** [main.cpp:L87-L91](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/src/st/testcase/txor/main.cpp#L87-L91)：

```cpp
if constexpr (std::is_same_v<T, int64_t> || std::is_same_v<T, uint64_t>) {
    EXPECT_TRUE(ResultCmpExact(golden, devFinal.data()));
} else {
    bool ret = ResultCmp<T>(golden, devFinal, 0.001f);
    EXPECT_TRUE(ret);
}
```

整数位运算没有浮点误差可言，一位错即全错，因此必须精确比对；带 0.001 容差的路径是留给浮点/舍入敏感类型的。四个新 TEST_F 在 [main.cpp:L113-L119](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/src/st/testcase/txor/main.cpp#L113-L119)，与内核实例化（[txor_kernel.cpp:L60-L63](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/src/st/testcase/txor/txor_kernel.cpp#L60-L63)）一一对应——延续 u1-l4 的"TEST_F 名 = 数据目录名"约定。内核本体（[txor_kernel.cpp:L17-L43](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/src/st/testcase/txor/txor_kernel.cpp#L17-L43)）就是标准三件套：`TASSIGN` 绑 UB（0x0/0x10000/0x20000，注意 64 位 tile 每个占 `Rows×Cols×8` 字节）→ `TLOAD` 两源 → `TXOR` → `TSTORE`，事件用 `Event<Op::TLOAD,Op::TXOR>` / `Event<Op::TXOR,Op::TSTORE_VEC>` 握手。

**（5）CPU 为什么"天然"成立。** CPU 模拟器的 TXOR 走的是通用元素级模板：[ElementOp.h:L202-L205](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/cpu/ElementOp.h#L202-L205) 的特化就是一行 `dst = src0 ^ src1;`——C++ 的 `^` 对 `int64_t/uint64_t` 是宿主机原生运算；外层 [BinaryElementTileOp_Impl，ElementTileOp.h:L19-L58](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/cpu/ElementTileOp.h#L19-L58) 只是 `parallel_for_rows` + 逐元素 `apply`，对 dtype 完全透明。所以 CPU 侧**不存在**"lane 只有 32 位"的约束，也就不需要寄存器对、交织装载、掩码切分这一整套机制；`BINARY_OP_DEF(XOR)` 宏展开（[ElementTileOp.h:L97-L105](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/cpu/ElementTileOp.h#L97-L105)、[L115](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/cpu/ElementTileOp.h#L115)）自动覆盖所有 dtype。CPU 的 `TXOR_IMPL` 同样接收并丢弃 `tmp`（[ElementTileOp.h:L159-L162](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/cpu/ElementTileOp.h#L159-L162)）。不过要注意现状：**CPU 的 txor ST 用例目前只到 int32**（`tests/cpu/st/testcase/txor/gen_data.py` 无 int64 case），64 位验证完全落在 A5 ST 一侧——因为这是 A5 后端本版本新增的能力，用例与文档都跟着后端走。

#### 4.5.4 代码实践

**实践目标**：跑通（或走读）A5 的 int64 ST 用例，并体验"CPU 天然支持"的对照。

**操作步骤**：

1. **有 CANN 环境（sim 或板端）时**，运行新增用例（命令行用法承接 u1-l4/u5-l1 的 run_st.py 约定）：

   ```bash
   python3 tests/script/run_st.py -r sim -v a5 -t txor -g TXORTest.case_int64_32x32_32x32
   python3 tests/script/run_st.py -r sim -v a5 -t txor -g TXORTest.case_int64_4x16_4x15
   ```

2. **无 NPU 环境时做源码阅读型实践**：通读 [txor_kernel.cpp:L17-L43](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/src/st/testcase/txor/txor_kernel.cpp#L17-L43)，回答：`TASSIGN(dstTile, 0x20000)` 时 dst tile 实际占用多少字节？4 个 64 位用例中哪个会触发 4.4 的尾块谓词？
3. **进阶（在本地工程副本中尝试）**：仿照 A5 用例，给 CPU 模拟器的 `tests/cpu/st/testcase/txor/gen_data.py` 与 `main.cpp` 补一个 int64 case（CPU 模板理论上天然支持，见 4.5.3（5）），用 `python3 tests/run_cpu.py` 或 run_st.py 的 sim 流程验证。

**需要观察的现象**：步骤 1 的两个用例均 PASS（`ResultCmpExact`）；步骤 3 中 CPU 侧无需改动任何头文件即可编译运行 int64 用例（若失败，记录报错位置并回看 4.5.3（5）的分析）。

**预期结果**：待本地验证（本讲义撰写环境无 CANN/昇腾硬件，未实际执行）。理论上步骤 1 通过即证明寄存器对仿真链路（装载→运算→掩码→存储）在真机语义下正确；步骤 3 通过即证明 CPU golden 与 A5 仿真对同一语义给出一致结果。

#### 4.5.5 小练习与答案

**练习 1**：`gen_data.py` 为什么把输入生成满 tile 尺寸、golden 却只填有效区？

**答案**：main.cpp 按 `kTRows_*kTCols_*sizeof(T)` 的满尺寸读入 `input1.bin/input2.bin`（文件小于该尺寸会读越界/断言），所以输入必须满尺寸；而内核受谓词掩码约束只写回有效区域，GM 其余位置被 `aclrtMemset` 清零后保持为零，golden 满尺寸置零、仅前 `h_valid*w_valid` 个元素为期望值，恰与"只写有效区"的观测对齐（承接 u2-l3"有效区域是左上角前缀"的结论）。

**练习 2**：如果把激励从 `[-2^31, 2^31)` 改成 `[0, 255)`，测试还"能过"吗？这算不算有效测试？

**答案**：很可能照样通过——所有元素高 32 位全为 0，`vxor` 高半字错位/掩码缺失等 bug 都碰不到差异。这揭示了位级仿真测试的一条通用原则：**激励必须覆盖每个半字的全幅值域**，否则寄存器对路径的"另一半"形同虚设。这正是 gen_data.py 注释强调"large values so the high 32-bit half is exercised"的原因。

**练习 3**：为什么 64 位用例用 `ResultCmpExact` 而 16 位用例用 `ResultCmp<T>(..., 0.001f)` 也能过？

**答案**：整数的 AND/OR/XOR 是逐位确定的，宿主机参考实现与硬件实现应当逐位一致，精确比对既是可能也是应当；`ResultCmp` 的容差参数是为浮点/近似算法预留的接口，对整数比较"容差为 0 的精确相等"，因此历史用例沿用带容差 API 也不会错——新代码显式选 `ResultCmpExact` 是更清晰的表达。

## 5. 综合实践

**任务：为一条 int64 TXOR 指令画出从 UB 到 UB 的寄存器级全链路示意图，并用它"人肉执行"一个尾块用例。**

1. **走读准备**。按顺序精读三段代码，边读边在纸上画数据流框图：
   - [Int64Binary，TBinOp.hpp:L482-L500](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TBinOp.hpp#L482-L500)（循环切分与谓词重建）
   - [Int64BinaryRepeat，TBinOp.hpp:L464-L480](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TBinOp.hpp#L464-L480)（偏移 ×2、`vlds`/`vsts` 与 `pintlv_b32`/`vintlv`）
   - [Int64BinaryCalcRegs 的 Xor 分支，TBinOp.hpp:L456-L458](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TBinOp.hpp#L456-L458)（两条 `vxor`）

2. **画图要求**。图中必须出现并标注：
   - UB 侧：`src0/src1/dst` 三个 tile 的行偏移公式 `(row*Cols + colOffset)*2`（字单位）；
   - 寄存器侧：`src0Low/src0High`、`src1Low/src1High`、`dstLow/dstHigh`、`half0/half1` 共 8 个向量寄存器，标出解交织（偶/奇字分流）与再交织的箭头方向；
   - 掩码侧：`mask`（元素粒度，喂给两条 `vxor`）与 `lowMask/highMask`（`pintlv_b32` 切分后分别驱动两条 `vsts`），以及第二次 `vsts` 落点 `+CCE_VL/4` 字。

3. **人肉执行尾块用例**。取 `case_int64_4x16_4x15`：自己编 4 行 × 前 15 列的 int64 数据（高位非零），按图推演一遍，最后用 numpy `^` 核对（参考 4.2.4 的脚本写法）。

4. **写对比结论**（各一段话）：
   - 对照 [gen_data.py:L24-L39](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a5/src/st/testcase/txor/gen_data.py#L24-L39) 与 [docs/isa/TXOR.md:L47-L61](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/isa/TXOR.md#L47-L61)：说明"一个 dtype 落地"要同步哪四处；
   - 结合 [ElementOp.h:L202-L205](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/cpu/ElementOp.h#L202-L205) 解释：CPU 模拟器为什么用一行通用模板 `dst = src0 ^ src1` 就天然给出 64 位 golden，而 A5 必须维护 `Int64Binary` 这一整套寄存器对仿真路径（提示：宿主机 ALU 原生 64 位 vs 向量部件 lane 上限 32 位）。

**验收标准**：图能让另一个没读过源码的同学按图复述出六步数据流；尾块推演与 numpy 结果一致；两段结论能脱离讲义独立成文。

## 6. 本讲小结

- A5 向量部件的 ALU 通路按最宽 32 位 lane 工作（`XorOp` 的类型映射 32 位封顶是直接证据），64 位运算因此被仿真为**一对 32 位寄存器（low/high）上的运算**；`Int64Op` 枚举本版本从 7 个值扩到 12 个（新增 And/Or/Xor/Not/Abs），把位运算纳入与算术同一条 `Int64Binary/Int64Unary/Int64Scalar` 链路。
- 位运算高低半字**完全独立**（两条 `vxor/vand/vor/vnot` 即可），算术则需要跨半字进位/符号链（`vaddc/vaddcs`、`vmull/vmula`、`psel`）——这是读 `Int64BinaryCalcRegs` 各分支的主线索。
- 数据进出寄存器靠"解交织装载 `vlds(DINTLV_B32)` → 运算 → `pintlv_b32` 切掩码 + `vintlv` 再交织 → 两条 `vsts(NORM_B32)` 分半存储（第二半落在 +256B）"的闭环；偏移一律以 int32 字为单位，元素偏移要 ×2。
- 谓词按 32 位 lane 置位（无 b64），但解交织后"lane i = 元素 i 的半字"，因此一份 `plt_b32(validCols)` 掩码即可同时裁剪 low/high；存储侧布局回到交织态，需 `pintlv_b32` 切成 `lowMask/highMask` 两份。尾块不需要专门路径，谓词计数自然裁剪。
- 一个 dtype 落地要同步四件套：Check 白名单 `static_assert`、`gen_data.py`（满尺寸输入 + 高位幅值激励 + golden 只填有效区）、`main.cpp` 的 `ResultCmpExact` 精确比对、`docs/isa` 类型表；A5 与 A2A3 白名单不同（A5 有 int64/uint64、A2A3 没有），再次印证"后端合法性与实现强相关"。
- CPU 模拟器的元素级模板 `dst = src0 ^ src1` 对 64 位天然成立（宿主机原生运算），不需要任何仿真机制；但 CPU 的 txor ST 用例目前只到 int32，64 位质量门完全由 A5 ST 承担。

## 7. 下一步学习建议

1. **横向对比另一条 64 位指令的落地**：按本讲 4.1.2 的分流表，任选 TAndS 或 TORS，从 TF 层入口一路读到 `Int64Scalar` 的标量拆半广播，对照 TXOR 写出异同（标量路径多了 `vbr` 广播、少了第二个源 tile）。
2. **看算术型 64 位分支**：精读 `Int64ShiftRegs`（[TBinOp.hpp:L364-L405](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TBinOp.hpp#L364-L405)）——移位量跨 32 边界时 low/high 要互相搬运位，比位运算复杂一档，是理解"寄存器对仿真"进阶形态的好材料。
3. **进入单元五**：下一讲 u5-l1（ST 测试体系）会把本讲的"四件套"泛化为整个测试基础设施的讲解——gen_data/golden/ResultCmp/run_st.py 的完整使用方法；之后 u5-l6 会回到 A5 平台，讲 MX 低精度 matmul 与 TPUSH/TPOP 的派发语义。
4. **若你对"为 PTO 贡献一条新指令"感兴趣**：可提前浏览 u8-l2 的 checklist，并把本讲的"四件套同步"作为其中"新增 dtype 支路"的子清单带入。
