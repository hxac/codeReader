# NPU 后端实现剖析：以 TADD 走读 a2a3 链路

## 1. 本讲目标

学完本讲，你应该能够：

1. 对照 CPU 后端，说出 NPU a2a3 后端为一条二元向量指令（TADD）增加的三层结构：`*_IMPL` 检查层、`__tf__` TF 层、算子结构体（`AddOp`）层。
2. 看懂 CCE 内置指令 `vadd` 的 `repeatTimes / blkStride / repeatStride` 参数含义，并理解 PTO 如何从 Tile 的 `RowStride` 推导这些参数。
3. 理解 `TAddCheck` 中编译期 `static_assert`（类型一致性、dtype 白名单、行主布局）与运行期 `PTO_ASSERT`（有效区域匹配）各自拦截什么错误。
4. 掌握向量单元的两种掩码模式 `set_mask_count` / `set_mask_norm`，并能解释本版本性能优化后的 TRem 中，`vcmpvs_lt` 为什么必须切到 norm 掩码模式并显式传入 `repeatTimes`。

## 2. 前置知识

- **四层抽象（承 u4-l1）**：一条 PTO 指令从上到下分为 User API wrapper（`include/pto/common/pto_instr.hpp`）→ 后端 `*_IMPL`（含 Check）→ TF 层函数 → CCE 内置指令。wrapper 只负责等待事件、转发操作数、返回 `RecordEvent`，不含算法。
- **CCE 内置指令**：在昇腾 ccec 编译器里以函数形式内联展开的硬件指令（如 `vadd`、`vdiv`、`vsel`），它们操作的是 `__ubuf__`（Unified Buffer）指针，而不是 Tile 对象。CPU 上没有这些内置指令，这正是 CPU 后端另写一份实现的原因。
- **向量指令的度量衡（承 u2-l1）**：向量单元一次 repeat 处理 256 字节（`REPEAT_BYTE = 256`），一个 block 是 32 字节（`BLOCK_BYTE_SIZE = 32`），`repeatTimes` 上限 255（`REPEAT_MAX`），repeat 步长上限 255（`REPEAT_STRIDE_MAX`）。
- **Tile 的容量形状与有效区域（承 u2-l3）**：`TileData::Cols`、`RowStride` 是编译期常量；`validRows/validCols` 是运行期有效区域。后端实现要在「硬件按 repeat 粒度搬数据」和「用户只想算有效区域」之间做适配。
- **掩码模式**：向量单元有一个向量掩码寄存器。`set_mask_count()` 把掩码寄存器当作「参与计算的元素个数」用；`set_mask_norm()` 把它当作「逐 lane 的位掩码」用。本讲 4.5 节会深入。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/pto/common/pto_instr.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr.hpp) | 公共 API 声明层：TADD/TREM 的 wrapper，经 `MAP_INSTR_IMPL` 转发到各后端 |
| [include/pto/cpu/TAdd.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/cpu/TAdd.hpp) | CPU 后端的 TADD：多线程 for 循环直接模拟，作为对照基准 |
| [include/pto/npu/a2a3/TAdd.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TAdd.hpp) | NPU a2a3 后端的 TADD：`AddOp` + `TAdd`（TF 层）+ `TAddCheck` + `TADD_IMPL` |
| [include/pto/npu/a2a3/TBinOp.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TBinOp.hpp) | 通用二元指令框架：按形状/连续性选择 count/norm 掩码与 repeat 编排 |
| [include/pto/npu/a2a3/TRem.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TRem.hpp) | TREM 的 a2a3 实现（本版本刚做过性能优化）：掩码模式切换的活教材 |
| [include/pto/common/constants.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/constants.hpp) | `REPEAT_BYTE`/`BLOCK_BYTE_SIZE`/`REPEAT_MAX`/`BIT_TO_BYTE` 等度量衡 |
| [include/pto/common/utils.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/utils.hpp) | `SetVectorCount`/`SetContMaskByDType`/`CeilDivision` 等掩码与除法助手 |
| [tests/cpu/st/testcase/trem/](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/cpu/st/testcase/trem/trem_kernel.cpp) | TREM 的 ST 用例：展示 tmp tile 的布局与容量要求 |

## 4. 核心概念与源码讲解

### 4.1 算子结构体：AddOp——对 CCE 内置指令的最薄封装

#### 4.1.1 概念说明

NPU 后端真正执行计算的是 ccec 内置指令（如 `vadd`）。但 PTO 不让每条 Tile 指令直接散落着调用内置指令，而是先用一个**算子结构体（policy 类）**把内置指令包起来，提供统一签名的静态方法 `BinInstr`。这样 `TBinOp.hpp` 的通用框架就可以对「任意二元算子」编程：框架负责形状切分、掩码、repeat 编排，算子结构体只负责「给我指针和 repeat 参数，我发一条硬件指令」。

这是一种策略模式：`AddOp`、`SubOp`、`MaxOp`、`MulOp`……每个结构体封装一条 CCE 内置指令，共用同一套 `BinInstr` 接口约定。全仓有 14 个 a2a3 头文件包含 `TBinOp.hpp` 复用这个框架。

#### 4.1.2 核心流程

```text
AddOp<T>::BinInstr(dst, src0, src1, repeats)            ← 简单重载：块步长 1、repeat 步长 8（连续排布）
    └─ vadd(dst, src0, src1, repeats, 1, 1, 1, 8, 8, 8)

AddOp<T>::BinInstr(dst, src0, src1, repeats, dRpt, s0Rpt, s1Rpt)  ← 全参重载：跨行排布时用
    └─ vadd(dst, src0, src1, repeats, 1, 1, 1, dRpt, s0Rpt, s1Rpt)
```

两个重载只暴露「repeat 次数」和「三个操作数各自的 repeat 步长」，块步长固定为 1（块内连续）。这正好是 Tile 行主排布下需要的全部自由度。

#### 4.1.3 源码精读

[include/pto/npu/a2a3/TAdd.hpp:L20-L32](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TAdd.hpp#L20-L32) 定义 `AddOp`，两个 `BinInstr` 重载都只是转发到 `vadd`：

```cpp
template <typename T>
struct AddOp {
    PTO_INTERNAL static void BinInstr(__ubuf__ T* dst, __ubuf__ T* src0, __ubuf__ T* src1, uint8_t repeats)
    {
        vadd(dst, src0, src1, repeats, 1, 1, 1, 8, 8, 8);
    }
    PTO_INTERNAL static void BinInstr(
        __ubuf__ T* dst, __ubuf__ T* src0, __ubuf__ T* src1, uint8_t repeats, uint8_t dstRepeatStride,
        uint8_t src0RepeatStride, uint8_t src1RepeatStride)
    {
        vadd(dst, src0, src1, repeats, 1, 1, 1, dstRepeatStride, src0RepeatStride, src1RepeatStride);
    }
};
```

注意 `PTO_INTERNAL` 修饰：它标记「内部零件」，与公共 ISA 接口的 `PTO_INST`（承 u4-l1）区分可见性——用户永远不应直接调用 `AddOp`。

#### 4.1.4 代码实践

打开 [include/pto/npu/a2a3/TMax.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TMax.hpp) 与 [include/pto/npu/a2a3/TSub.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TSub.hpp)，确认两件事：① 它们的 `MaxOp`/`SubOp` 结构体与 `AddOp` 逐行同构，只是内置指令换成 `vmax`/`vsub`；② 它们的 TF 层函数同样调用 `BinaryInstr<...>` 框架。这就是「换一条指令 = 换一个 Op 结构体」的复用方式。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `BinInstr` 的全参重载只暴露 repeat 步长、不暴露块步长（blkStride 固定为 1）？

**答案**：PTO 的 Tile 在 UB 内按行主连续存放，同一 repeat 内的 256 字节数据总是物理连续的，块间不需要跳；唯一需要「跳」的场景是让相邻 repeat 落到不同行（行间距 = `RowStride`），这由 repeat 步长表达。固定 blkStride=1 既简化接口，也保证 repeat 内带宽最优。

**练习 2**：如果新增一条 `TXor` 指令要走 TBinOp 框架，最少要写什么？

**答案**：写一个 `XorOp` 结构体，在 `BinInstr` 里调用 CCE 的 `vxor`（参数表照抄 `vadd` 的形式），再写 TF 层函数与 `*_IMPL`。切分/掩码/repeat 编排全部由 `BinaryInstr` 框架复用。

### 4.2 TF 层实现与 TBinOp 通用二元框架

#### 4.2.1 概念说明

TF 层（标记为 `__tf__` 的模板函数）是「Tile 世界」与「UB 指针世界」的分界线：它接收带 `__out__`/`__in__` 限定的 Tile 数据句柄，用 `__cce_get_tile_ptr` 取出 `__ubuf__` 指针，从 Tile 类型里抽出编译期 `RowStride`，然后调用通用框架 `BinaryInstr`。`TBinOp.hpp` 是所有同形二元指令共享的「形状适配器」：它的全部工作是回答一个问题——**给定有效区域 (validRows×validCols) 和容量形状，怎样用最少的 `vadd` 指令恰好覆盖有效区域、不越界、不漏算？**

#### 4.2.2 核心流程

`TAdd` TF 层先按「三个操作数行步长是否相同」分流，再进入框架：

```text
TAdd(__tf__)
 ├─ 三个 RowStride 相同 → BinaryInstr<同 strides 版>       （更激进的小形状/快速路径）
 └─ 任一不同          → BinaryInstr<异 strides 版>        （直接走按行 repeat 的通用路径）

同 strides 版 BinaryInstr 的决策树：
 ├─ 小形状（Rows ≤ 255 且 Cols < elementsPerRepeat）
 │    → Bin1LNormModeSmall：一行不足一个 repeat，用连续掩码 + validRow 个 repeat 堆行
 ├─ 编译期连续（Cols == ValidCol 或 Rows == 1）
 │    → FastPath：
 │       ├─ Cols 非 repeat 对齐 或 总 repeat 数 > 255 → Bin1LCountMode（count 掩码一把梭）
 │       └─ 否则                                    → Bin1LNormMode（整段 repeat + 尾部掩码）
 └─ 否则 → GeneralPath：
      ├─ 运行期连续（validCol == Cols 或 validRow == 1）→ 同 FastPath 的运行期判断
      └─ 非连续（有效区域是容量内的小窗口）：
           ├─ 小 repeat 启发（< SMALL_RPT_BINOP=4）→ Bin2LCountMode（逐行 count 掩码）
           ├─ 行数少 → Bin2LNormModeColVLAlign（列按 repeat 对齐时逐行整 repeat）
           └─ 一般情形 → Bin2LNormModeRowRpt（列切 repeat、行做 repeat 步长，尾部再掩码）
```

异 strides 版没有小形状/快速路径，直接 `Bin2LNormModeRowRpt` 按行排 repeat，其中每行三个操作数各自用自己的行步长寻址，且任一步长超过 `REPEAT_STRIDE_MAX` 时退化为「一行一条指令」。

#### 4.2.3 源码精读

[include/pto/npu/a2a3/TAdd.hpp:L34-L54](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TAdd.hpp#L34-L54) 是 TF 层主体——取指针、编译期分流：

```cpp
__tf__ PTO_INTERNAL void TAdd(
    typename TileDataDst::TileDType __out__ dst, ..., unsigned validRows, unsigned validCols)
{
    using T = typename TileDataDst::DType;
    __ubuf__ T* dstPtr = (__ubuf__ T*)__cce_get_tile_ptr(dst);   // Tile 句柄 → UB 指针
    ...
    if constexpr (dstRowStride == src0RowStride && dstRowStride == src1RowStride) {
        BinaryInstr<AddOp<T>, T, TileDataDst, elementsPerRepeat, blockSizeElem, dstRowStride>(...);
    } else {
        BinaryInstr<AddOp<T>, T, elementsPerRepeat, blockSizeElem, dstRowStride, src0RowStride, src1RowStride>(...);
    }
}
```

决策树入口在同 strides 版 [TBinOp.hpp:L240-L260](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TBinOp.hpp#L240-L260)：先做小形状优化，再按「编译期是否连续」分流到 FastPath/GeneralPath。

count 掩码路径 [TBinOp.hpp:L21-L29](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TBinOp.hpp#L21-L29) 最能体现框架思想——把有效元素总数交给掩码，`repeats` 传 0 由硬件按计数展开：

```cpp
PTO_INTERNAL void Bin1LCountMode(... unsigned validRow, unsigned validCol)
{
    set_mask_count();
    SetVectorCount(validRow * validCol);   // 只算前 validRow*validCol 个元素
    Op::BinInstr(dstPtr, src0Ptr, src1Ptr, 0);
    set_mask_norm();
    SetFullVecMaskByDType<T>();            // 恢复满掩码，避免污染后续指令
}
```

norm 掩码按行 repeat 的核心 [TBinOp.hpp:L148-L172](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TBinOp.hpp#L148-L172)：把「列方向切 repeat、行方向做 repeat 步长」合成一条多 repeat 指令（`repeatStride = rowStride / blockSizeElem`，让相邻 repeat 恰好落在相邻行），列尾不足一个 repeat 时用连续掩码补一发：

```cpp
constexpr unsigned repeatStride = rowStride / blockSizeElem;   // 行距换算成 32B 块数
...
Op::BinInstr(dstPtr + offset, src0Ptr + offset, src1Ptr + offset,
             validRow, repeatStride, repeatStride, repeatStride);  // validRow 个 repeat，每个落一行
```

`REPEAT_MAX=255` 的兜底在 [TBinOp.hpp:L83-L103](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TBinOp.hpp#L83-L103)（每 255 个 repeat 一批）与 [TBinOp.hpp:L105-L146](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TBinOp.hpp#L105-L146)（行数超限时分批，步长超 `REPEAT_STRIDE_MAX` 时逐行单发）。异 strides 版的同名函数在 [TBinOp.hpp:L262-L375](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TBinOp.hpp#L262-L375)，逻辑相同，只是三个操作数各自维护偏移。

#### 4.2.4 代码实践

纯源码阅读实践：给 `BinaryInstr` 同 strides 版的决策树填一张表。对一个 `Tile<Vec, half, 16, 256, RowMajor, -1, -1>`（容量 16×256，有效区域运行期给定），分别取 `(validRow, validCol) = (16, 256)` 与 `(4, 100)`，沿着 [TBinOp.hpp:L240-L260](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TBinOp.hpp#L240-L260) 的 `if constexpr` 逐层判断，写出各自最终落到哪个函数、发出了几条 `vadd`。提示：half 的 `elementsPerRepeat = 256/2 = 128`，两个 case 的 `validCol` 分别是 2 个 repeat 整与「非 repeat 对齐」。

**预期结果**（待本地验证）：`(16,256)` 走 FastPath 的 `Bin1LNormMode`（编译期 Cols==ValidCol 成立，总 repeat 数 32 ≤ 255），整段 2×16 个 repeat 一条指令发出；`(4,100)` 走 GeneralPath 非连续分支，`Cols=256` 时 `normColRepeat=2`、`Rows*normColRepeat=32` 不小于 `SMALL_RPT_BINOP=4`，且 `Rows(16) < normColRepeat+1` 不成立，落入 `Bin2LNormModeRowRpt`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Bin1LCountMode` 结束后要 `set_mask_norm(); SetFullVecMaskByDType<T>();`？

**答案**：向量掩码寄存器是核内全局状态，count 模式 + 有限计数如果残留，后续所有向量指令都会被错误地截断。恢复 norm 模式并置满掩码（`set_vector_mask(-1, -1)`）是一种「用完归位」的卫生约定，防止本指令的掩码泄漏到用户或下一条指令。

**练习 2**：TF 层为什么要区分「同 strides / 异 strides」两个 `BinaryInstr` 重载，而不是只用最通用的那个？

**答案**：同 strides 是绝大多数场景（三个 tile 同形），可以走更激进的小形状优化与编译期连续性判断，生成更少指令；异 strides 需要为每个操作数单独寻址，编排更保守。为常见情形留快速路径、为一般情形留正确路径，是性能库的典型取舍。

### 4.3 CCE 向量指令：vadd 的 repeat/stride 参数

#### 4.3.1 概念说明

`vadd(dst, src0, src1, repeatTimes, dstBlkStride, src0BlkStride, src1BlkStride, dstRepeatStride, src0RepeatStride, src1RepeatStride)` 是昇腾向量单元的双目指令。理解它的三个刻度是读懂一切 a2a3 向量实现的前提：

- **repeat**：一次重复处理 256 字节，即 \(\text{elementsPerRepeat} = \frac{256}{\text{sizeof}(T)}\) 个元素——fp32 是 64 个，fp16 是 128 个。
- **blkStride（块步长）**：repeat 内相邻 32 字节块之间的距离，单位是 32 字节块（`BLOCK_BYTE_SIZE`）。连续排布取 1。
- **repeatStride（repeat 步长）**：相邻两个 repeat 起点之间的距离，同样以 32 字节块为刻度。从 PTO 的推导方式 `repeatStride = rowStride / blockSizeElem`（[TBinOp.hpp:L153](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TBinOp.hpp#L153)）可以看出：一行占 `rowStride` 个元素即 `rowStride/blockSizeElem` 个 32B 块，把它作为 repeat 步长，下一个 repeat 就恰好落在下一行。

#### 4.3.2 核心流程

地址推进的统一公式：

\[
\text{offset}(r, k) = r \times \text{repeatStride} \times 32\text{B} + k \times \text{blkStride} \times 32\text{B}
\]

其中 \(r\) 是 repeat 序号、\(k\) 是块序号。两个常用特例：

| 场景 | 参数 | 效果 |
| --- | --- | --- |
| 连续大段数据 | `repeats=N, blk=1, rpt=8` | 8 个 32B 块 = 256B = 恰好一个 repeat 的数据量，repeat 首尾相接，等效于顺序处理 \(N \times \text{elementsPerRepeat}\) 个元素 |
| 按行堆叠 | `repeats=validRow, blk=1, rpt=rowStride/blockSizeElem` | 每个 repeat 落在一行，一条指令处理整列块 |

#### 4.3.3 源码精读

[include/pto/npu/a2a3/TAdd.hpp:L22-L25](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TAdd.hpp#L22-L25) 的 `vadd(dst, src0, src1, repeats, 1, 1, 1, 8, 8, 8)` 即「连续大段」特例。度量衡常量定义在 [include/pto/common/constants.hpp:L20-L23](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/constants.hpp#L20-L23) 与 [L28](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/constants.hpp#L28)：

```cpp
constexpr int REPEAT_BYTE = 256;              // 一次 repeat 处理 256 字节
constexpr const uint64_t BLOCK_MAX_PER_REPEAT = 8; // 256 / 32 = 8，一个 repeat 含 8 个 32B 块
constexpr int REPEAT_MAX = 255;               // repeatTimes 上限
constexpr const int BLOCK_BYTE_SIZE = 32;     // 一个 block 32 字节
constexpr const int REPEAT_STRIDE_MAX = 255;  // repeat 步长上限（uint8_t）
```

`TADD_IMPL` 在 [include/pto/npu/a2a3/TAdd.hpp:L85-L90](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TAdd.hpp#L85-L90) 把 Tile 类型换算成这些刻度：

```cpp
constexpr unsigned blockSizeElem = BLOCK_BYTE_SIZE / sizeof(T);     // 一个 32B 块的元素数
constexpr unsigned elementsPerRepeat = REPEAT_BYTE / sizeof(T);    // 一个 repeat 的元素数
constexpr unsigned dstRowStride = TileDataDst::RowStride;          // 行距（元素数，编译期）
```

#### 4.3.4 代码实践

手算练习（纸面即可，无需环境）：对 `Tile<Vec, float, 8, 128, RowMajor, -1, -1>` 调用 TADD，计算 `blockSizeElem`、`elementsPerRepeat`、按行堆叠时的 `repeatStride`，并回答：若 `validRow=8, validCol=128` 且走 `Bin2LNormModeRowRpt`，一条 `vadd` 的 `repeatTimes` 与三个 repeat 步长各是多少？

**预期结果**：`blockSizeElem = 32/4 = 8`，`elementsPerRepeat = 256/4 = 64`，`repeatStride = 128/8 = 16`；每行 128 列 = 2 个整 repeat，无尾部，故对每个列块各发一条 `repeatTimes=8、repeat 步长 16` 的 `vadd`（两次列偏移调用）。（推导基于源码公式，具体走哪个分支待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：fp16 与 fp32 各发一条 `repeats=1, blk=1, rpt=8` 的 `vadd`，各处理多少元素？

**答案**：都是 256 字节：fp16 处理 128 个元素，fp32 处理 64 个元素。repeat 的刻度是字节而不是元素，这正是 `elementsPerRepeat` 必须按 `sizeof(T)` 换算的原因。

**练习 2**：`repeatTimes` 与 `repeatStride` 都是 `uint8_t`，为什么 `RowStride` 很大的 tile 还能工作？

**答案**：`repeatStride` 上限 255（`REPEAT_STRIDE_MAX`），当 `rowStride / blockSizeElem > 255` 时框架不会再把多行压进一条指令，而是退化为逐行单发 `BinInstr(..., 1, 1, 1, 1)`，用指针加法绕开步长上限——见 [TBinOp.hpp:L111-L146](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TBinOp.hpp#L111-L146) 的 `strideOverFlag` 分支。

### 4.4 编译期检查：TAddCheck 的 static_assert 防线

#### 4.4.1 概念说明

NPU 后端的 `*_IMPL` 在进入 TF 层之前先跑一个 `Check` 函数，把「类型合法、布局合法」前移到编译期（`static_assert`），把「有效区域匹配」留给运行期（`PTO_ASSERT`）。对照 CPU 后端：CPU 的 `TADD_IMPL` 只有运行期形状断言、完全没有类型/布局 `static_assert`（CPU 上 `dst[idx] = src0[idx] + src1[idx]` 对任何可加类型都合法）。所以**「CPU 模拟器跑通」不能证明这条指令在全后端合法**——这是承自 u2-l1 的重要结论在 TADD 上的具体体现。

#### 4.4.2 核心流程

```text
TADD_IMPL(dst, src0, src1)
 ├─ TAddCheck<T, ...>          ← 编译期三道门 + 运行期两道门
 │    ├─ static_assert#1 dst/src0/src1 元素类型一致
 │    ├─ static_assert#2 T ∈ {int32, int16, half, float16, float, float32} 白名单
 │    ├─ static_assert#3 三个 tile 都是行主布局
 │    ├─ PTO_ASSERT src0 有效区域 == dst 有效区域   ← 运行期
 │    └─ PTO_ASSERT src1 有效区域 == dst 有效区域   ← 运行期
 └─ TAdd<...>(dst.data(), ..., dst.GetValidRow(), dst.GetValidCol())   ← 进 TF 层
```

#### 4.4.3 源码精读

[include/pto/npu/a2a3/TAdd.hpp:L56-L78](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TAdd.hpp#L56-L78) 是完整的 `TAddCheck`：

```cpp
static_assert(
    std::is_same<T, typename TileDataSrc0::DType>::value && std::is_same<T, typename TileDataSrc1::DType>::value,
    "Fix: TADD the data type of dst must be consistent with of src0 and src1.");        // 门 1
static_assert(
    std::is_same<T, int32_t>::value || ... || std::is_same<T, float>::value || std::is_same<T, float32_t>::value,
    "Fix: TADD has invalid data type.");                                                 // 门 2
static_assert(
    TileDataDst::isRowMajor && TileDataSrc0::isRowMajor && TileDataSrc1::isRowMajor,
    "Fix: TADD only support row major layout.");                                         // 门 3
```

CPU 侧对照 [include/pto/cpu/TAdd.hpp:L63-L75](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/cpu/TAdd.hpp#L63-L75)：只有两条与 NPU 版措辞相同的运行期 `PTO_ASSERT`（L68-L73），随后直接进 `TAdd_Impl`。CPU 的计算体（[L24-L43](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/cpu/TAdd.hpp#L24-L43)）按行主/列主两个分支用 `parallel_for_rows` 多线程模拟，列主时把行列角色互换——它模拟的是「数学语义」，不模拟 repeat/掩码这些硬件细节。

运行期白名单拦截的例子：A2A3 的 TADD 白名单没有 `bfloat16_t` 与 `int8_t`，若用这些类型编译 NPU 目标，`static_assert#2` 直接报错；同样的代码在 CPU 模拟器上却能正常跑出结果。

#### 4.4.4 代码实践

写一份「错误触发对照表」（纸面分析 + 本地验证）：

1. **门 1 触发**：把 `tests/cpu/st/testcase/tadd/tadd_kernel.cpp` 复制一份，将 dst tile 声明为 `Tile<Vec, float, ...>` 而 src0/src1 保持 `Tile<Vec, half, ...>`。
2. **门 2 触发**：三个 tile 都用 `int8_t`。
3. **门 3 触发**：三个 tile 都改成 `BLayout::ColMajor`。

对每个 case 预测报错信息（即对应 `static_assert` 的 `"Fix: ..."` 文案），然后在 NPU 编译链路（`python3 tests/script/run_st.py -r sim -v a3 -t tadd`，需 CANN 环境）或本地对 `TAddCheck` 模板做显式实例化来验证。注意：直接用 `python3 tests/run_cpu.py -t tadd` 跑 CPU 模拟器**不会**触发这三个错误，因为 CPU 后端的 `TADD_IMPL` 根本不含这些检查——这本身就是本节最重要的观察点。（编译期行为的完整验证待本地 CANN 环境。）

#### 4.4.5 小练习与答案

**练习 1**：为什么类型检查用 `static_assert` 而有效区域检查用 `PTO_ASSERT`？

**答案**：元素类型与布局是模板参数，编译期完全已知，违规必然是代码写错，早失败可以给出带 `"Fix:"` 提示的定点诊断；有效区域 `validRows/validCols` 是运行期值（尾块大小由 tiling 决定），只能运行期断言。

**练习 2**：门 2 的白名单与 ISA 文档、`include/README.md` 状态表是什么关系？

**答案**：白名单是该指令在 a2a3 后端实际支持的 dtype 集合，是「实现的事实」；docs/isa 文档与状态表是它的对外陈述。三者由贡献清单（见 u8-l2）约束保持一致——给指令扩 dtype 时需要同时改 Check 白名单、文档类型表和 ST 用例。

### 4.5 掩码模式与 repeat 修正：以优化后的 TRem 为例

#### 4.5.1 概念说明

向量单元的掩码寄存器有两种解释，由 `set_mask_count()` / `set_mask_norm()` 切换：

- **count 模式**：掩码值表示「参与计算的元素个数」。算术指令（vadd/vmul…）在该模式下按计数覆盖所有元素——`Bin1LCountMode` 甚至传 `repeats=0` 而全靠计数，就是因为算术路径会按 count 自动展开。
- **norm 模式**：掩码是逐 lane 的位掩码（连续掩码 = 前 n 个 lane 使能），配合显式 `repeatTimes` 控制覆盖范围。

**比较指令是例外**。`vcmpvs_lt` 的输出不是「同形状的向量」，而是**打包位掩码**（每 8 个元素压成 1 字节），其写出长度由 `repeatTimes` 决定，不会按 count 自动扩展到整个计数。本版本（提交 6285cda9「TRem Performance optimization」）正是修复了这一点：旧代码传 `repeatTimes=1`，只会写出打包掩码的前 64 位，validCols 超过 64 的部分残留 UB 旧值，导致 vsel 按脏位做符号修正。

#### 4.5.2 核心流程

TRem 的数学定义（对 fp32 与 int32 取「与除数同号」的余数）：

\[
\text{rem}(a, b) = a - \lfloor a/b \rfloor \cdot b, \qquad \text{若 } \text{rem} \cdot b < 0 \text{ 则 } \text{rem} \mathrel{+}= b
\]

每行的指令序列（`RemF32Instr`，逐行调用）：

```text
count 模式，计数 = validCols          ← 算术部分全部按 count 覆盖
  vdiv → floor(vconv) → vmul → vsub   ← 主式
  vmul(dst, src1)                      ← 符号探测积
norm 模式，满 lane 掩码
  vcmpvs_lt(cmpMask, tmp, 0.0f, repeatTimes, ...)   ← repeatTimes = ⌈validCols / elementsPerRepeat⌉，显式！
count 模式，计数 = validCols
  vadd(tmp, dst, src1)                 ← 候选修正值
  vector_dup(addrBuf, maskAddr)        ← 把 cmpMask 地址复制成 64bit 地址缓冲
  set_cmpmask(addrBuf)                 ← 二级寻址：vsel 经 addrBuf 找到位掩码
  vsel(dst, tmp, dst, ..., mode=2)     ← VSEL_TENSOR_TENSOR_MODE：位为 1 取 tmp，否则保 dst
```

tmp tile 的单行布局：`[0, dstRowStride)` 放候选值，其后是 32 字节对齐的打包位掩码（`maskFloats` 个元素），再后是 8 个元素的地址缓冲。

#### 4.5.3 源码精读

关键注释与修复后的代码在 [include/pto/npu/a2a3/TRem.hpp:L44-L55](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TRem.hpp#L44-L55)：

```cpp
// vcmpvs_lt must run in norm mask mode with explicit repeatTimes: unlike
// arithmetic ops, the compare op does not auto-extend a single repeat to
// the whole count ..., so repeat=1 would only emit the first 64 bits of
// the packed mask and leave stale UB in the rest.
__ubuf__ uint8_t* cmpMask = reinterpret_cast<__ubuf__ uint8_t*>(tmp + dstRowStride);
set_mask_norm();
set_vector_mask(-1, -1);
vcmpvs_lt(cmpMask, tmp, 0.0f, repeatTimes, 1, 1, 8, 8);
pipe_barrier(PIPE_V);
set_mask_count();
set_vector_mask(0, validCols);
```

`repeatTimes` 在 TF 层 [TRem.hpp:L175](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TRem.hpp#L175) 一次性算好，逐行复用：`uint16_t repeatTimes = CeilDivision(validCols, elementsPerRepeat);`——例如 fp32、validCols=512 时为 8 个 repeat，打包掩码 512 位全部写满。

vsel 的二级寻址在 [TRem.hpp:L60-L76](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TRem.hpp#L60-L76)：`set_cmpmask` 收到的不是掩码本身，而是一个装着「掩码地址」的缓冲（`vector_dup` 把地址复制满 `cmpmaskLen=2` 个 uint32 lane，即 64 位地址），`vsel(..., 2)` 按 `VSEL_TENSOR_TENSOR_MODE` 经该缓冲读取位掩码。tmp 容量需求由 [TRemCheck 的 L209-L216](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TRem.hpp#L209-L216) 在运行期把关：`tmpRequiredCols = RowStride + maskFloats + BIT_TO_BYTE`。

掩码助手函数的定义在 [include/pto/common/utils.hpp:L57-L69](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/utils.hpp#L57-L69)：`SetVectorCount(n)` 即 `set_vector_mask(0, n)`（count 语义），`SetFullVecMaskByDType<T>()` 即 `set_vector_mask(-1, -1)`（满 lane），`SetContMaskByDType(n)` 走 `SetContinuousMask`（前 n 个 lane）。

用例侧的 tmp tile 在 [tests/cpu/st/testcase/trem/trem_kernel.cpp:L23-L28](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/cpu/st/testcase/trem/trem_kernel.cpp#L23-L28) 声明为 `TileDataDst tmpTile(1, kTCols_)`——单行、列数与数据 tile 相同，正好容纳 TRemCheck 要求的布局。

#### 4.5.4 代码实践

1. **实践目标**：理解「算术指令按 count 自动展开、比较指令必须显式 repeat」的差异，并验证 TRem 用例在 CPU 模拟器上的行为。
2. **操作步骤**：
   - 运行 `python3 tests/run_cpu.py -t trem`（CPU 模拟器路径），观察 `case_float_64x512_64x64` 等 5 个用例是否通过。
   - 用 `git show 6285cda9 -- include/pto/npu/a2a3/TRem.hpp` 查看本次优化的完整 diff，对比新旧两版 `vcmpvs_lt` 调用行。
   - 纸面推演：validCols=512、T=float 时，旧版 `repeatTimes=1` 会写出多少位掩码？多少列的符号修正因此读到脏数据？
3. **需要观察的现象**：CPU 模拟器上 TREM 数值正确（CPU 后端是纯 C++ `std::fmod` 风格语义，不含掩码概念）；diff 中新增的两段注释（L44-L48、L60-L64）与 `set_mask_norm/set_vector_mask(-1,-1)` 成对出现。
4. **预期结果**：512 个 fp32 元素打包位掩码共 512 位，一个 repeat 只写 64 位（64 个元素），旧版有 448 位是 UB 残留——即 64 列以后的符号修正不可靠；新版 `repeatTimes=8` 全部写满。（CPU 侧运行结果待本地验证；NPU 侧行为差异须上 sim/真机才能观测。）

#### 4.5.5 小练习与答案

**练习 1**：为什么 `RemF32Instr` 里算术指令（vdiv/vmul/vsub/vadd）不需要像 `vcmpvs_lt` 这样显式传 repeatTimes？

**答案**：它们全程运行在 count 模式（`set_vector_mask(0, validCols)`），CCE 的算术指令路径会按掩码计数自动确定覆盖范围；而比较指令的输出是打包位掩码、长度由 repeatTimes 决定，count 模式下传 1 就真的只写一个 repeat 的 64 位。源码注释指出 CANN 自身的高层比较接口也是按 \(\lceil \text{count} \times \text{sizeof}(T) / 256 \rceil\) 计算并显式传入 repeat 的。

**练习 2**：`vsel` 从模式 0 换成模式 2（`VSEL_TENSOR_TENSOR_MODE`）后，`set_cmpmask` 的参数为什么从掩码指针变成了 `addrBuf`？

**答案**：模式 2 通过二级寻址读掩码——`set_cmpmask` 期望收到一个缓冲，其中每个 lane 存的是掩码的**地址**（B32 dtype 用 64 位地址，占 `cmpmaskLen=2` 个 uint32）。所以先用 `vector_dup` 把 `cmpMask` 的地址灌满 `addrBuf`，再 `set_cmpmask(addrBuf)`；掩码本体和地址缓冲都排在 tmp tile 的单行布局里。

**练习 3**：TRem 为什么以行为单位循环调用 `RemInstr`，而不是像 TADD 那样把多行压进一条指令？

**答案**：TRem 是一条**多步复合指令**（除、取整、乘、减、比较、选择共约 10 条内置指令，且步骤间有 `pipe_barrier(PIPE_V)` 依赖），中间还要在 tmp 里放行级候选值与位掩码。多行复用一条 repeat 序列需要为每行准备独立的掩码/地址缓冲，得不偿失；逐行执行、行间指针按 `RowStride` 平移（[TRem.hpp:L177-L185](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TRem.hpp#L177-L185)）是最简单且正确的编排。

## 5. 综合实践

完成规格中规定的三段式对照实践，产出一篇笔记：

1. **逐段对照笔记**：按下表骨架，把 [include/pto/cpu/TAdd.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/cpu/TAdd.hpp) 与 [include/pto/npu/a2a3/TAdd.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TAdd.hpp) 逐段对齐，每个环节各引 3-5 行源码：

| 环节 | CPU 后端 | NPU a2a3 后端 |
| --- | --- | --- |
| 公共入口 | [pto_instr.hpp:L174-L180](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr.hpp#L174-L180)（两后端共用同一 wrapper） | 同左 |
| 检查 | 仅运行期 `PTO_ASSERT`（[cpu/TAdd.hpp:L68-L73](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/cpu/TAdd.hpp#L68-L73)） | `TAddCheck` 三道编译期 + 两道运行期（[a2a3/TAdd.hpp:L56-L78](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TAdd.hpp#L56-L78)） |
| 实现分发 | `TAdd_Impl` 直接循环（[cpu/TAdd.hpp:L20-L61](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/cpu/TAdd.hpp#L20-L61)） | `__tf__ TAdd` 取 UB 指针后进 `BinaryInstr` 框架（[a2a3/TAdd.hpp:L34-L54](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TAdd.hpp#L34-L54)） |
| 最底层 | `dst[idx] = src0[idx] + src1[idx]`（标量加） | `vadd(dst, src0, src1, repeats, 1,1,1, 8,8,8)`（向量内置指令） |

2. **static_assert 触发示例**：按 4.4.4 节构造三个触发用例（类型不一致 / int8_t 越白名单 / 列主布局），每条记录：触发的断言行号、预期的 `"Fix: ..."` 文案、为什么 CPU 模拟器拦不住它。可先在 CPU 模拟器上确认「同样的错误代码照样跑通」，再上 sim 验证编译期报错（待本地 CANN 环境）。
3. **TRem 掩码分析**：用自己的话（不超过 200 字）回答——`vcmpvs_lt` 为什么必须 `set_mask_norm()` + 显式 `repeatTimes`，而同函数里的 `vadd/vmul` 可以一直留在 count 模式？引用 [TRem.hpp:L44-L55](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TRem.hpp#L44-L55) 的注释与 4.5 节的推导作为依据。

## 6. 本讲小结

- a2a3 后端为一条二元指令提供三层结构：`AddOp` 算子结构体最薄封装 CCE 内置指令、`__tf__` TF 层把 Tile 句柄换算成 UB 指针与编译期步长、`TBinOp.hpp` 通用框架按形状/连续性编排 repeat 与掩码。
- `vadd` 的刻度体系：一次 repeat = 256 字节 = 8 个 32B 块；`repeatStride = rowStride / blockSizeElem` 让相邻 repeat 恰好落在相邻行；`REPEAT_MAX=255` 与 `REPEAT_STRIDE_MAX=255` 超限时框架退化为逐行单发。
- `TAddCheck` 用三道编译期 `static_assert`（类型一致、dtype 白名单、行主布局）加两道运行期 `PTO_ASSERT`（有效区域匹配）设防；CPU 后端没有前三道，「CPU 跑通 ≠ 全后端合法」。
- `BinaryInstr` 的决策树按「小形状 → 编译期连续 → 运行期连续 → 非连续」逐层选择 count 掩码一把梭或 norm 掩码按行 repeat，用完掩码必须恢复满 lane。
- 本版本优化后的 TRem 揭示了比较指令的特殊性：`vcmpvs_lt` 输出打包位掩码、长度由 `repeatTimes` 决定，必须切 norm 模式并显式传 \(\lceil \text{validCols}/\text{elementsPerRepeat} \rceil\)；`vsel` 模式 2 经 `addrBuf` 二级寻址读掩码，tmp tile 需容纳候选值 + 位掩码 + 地址缓冲。

## 7. 下一步学习建议

- 下一讲（u4-l4）转向数据搬运指令族 TLOAD/TSTORE，观察 MTE2/MTE3 流水线上的指令如何与本章向量指令（PIPE_V）通过事件握手。
- 想继续深挖 a2a3 的布局适配，可对照阅读 [include/pto/npu/a2a3/TRowProd.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TRowProd.hpp)——它在同一提交窗口内做了类似的掩码/repeat 优化，是 4.5 节方法的第二个样本。
- 想理解 A5 代际在同一指令上的差异（如 int64 用寄存器对仿真），预习 u4-l7；想亲手补齐一条指令，先读 u8-l2 的贡献清单。
