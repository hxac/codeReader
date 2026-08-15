# 逐元素与标量运算族：TBinOp/TBinSOp 抽象

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 NPU 侧一条逐元素二元指令（如 TADD）从公共 API 到 `vadd` intrinsic 的完整分层。
2. 掌握 `TBinOp.hpp` 的「策略模式」设计：一个 `Op` 函子（functor）只回答"算什么"，模板负责"怎么遍历、怎么摆 repeat"。
3. 掌握标量变体 `TBinSOp.hpp` 与二元版本的差异（广播标量、少一个操作数步长）。
4. 理解什么情况下一条指令**不能**套模板——以 TFMOD 为反例，看它如何用多条 intrinsic 组合实现。
5. 能独立列出"新增一条二元指令"需要触达的全部文件清单。

本讲是单元四（计算指令族精读）的第一讲，承接 u3-l4 中建立的 CPU 仿真三层结构（API → `*_IMPL` → `*_Impl`），把视角切换到 NPU 真机实现。

## 2. 前置知识

阅读本讲前，请确认理解以下概念（均在前几讲出现过，这里从 NPU 实现视角补充）：

- **repeat 与 256 字节**：昇腾 Vector 单元一次 repeat 处理 256 字节（`REPEAT_BYTE = 256`），即 fp32 一次 64 个元素、fp16 一次 128 个元素。一条 intrinsic 可以带一个 `repeats` 计数（最多 255 次），硬件自动连续发射。
- **block 与 32 字节对齐**：向量访存的最小颗粒是 32 字节 block（`BLOCK_BYTE_SIZE = 32`）。`repeatStride` 的单位是 block 而非元素——`repeatStride = 8` 表示相邻 repeat 之间跳 8×32B = 256B，恰好一个 repeat 的长度。
- **mask 与有效区**：Vector 指令用 mask 决定哪些通道生效。`set_mask_count()` + `SetVectorCount(n)` 切到"计数模式"（只处理前 n 个元素），`set_mask_norm()` + `SetContMaskByDType<T>(n)` 是"连续掩码模式"。尾块（validCol 不是 repeat 整数倍）必须靠它们保护。
- **`__ubuf__` 指针**：Unified Buffer（片上向量缓冲）地址空间的指针类型标注，与 `__gm__`（全局内存）相对。Vector intrinsic 的操作数必须是 `__ubuf__` 指针。
- **intrinsic**：CCE 编译器提供的内建函数（如 `vadd`），一条对应一条底层向量指令。它是 PTO NPU 实现的最底层。
- **策略模式（ functor）**：C++ 模板编程中，把"一个可变的动作"封装成只含静态函数的小结构体，由外层模板在编译期调用。本讲的 `AddOp<T>`、`AddSOp<T>` 就是策略类。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `include/pto/npu/a2a3/TBinOp.hpp` | 二元逐元素指令的通用遍历/编排模板（本讲主角一） |
| `include/pto/npu/a2a3/TBinSOp.hpp` | 标量变体的通用编排模板（本讲主角二） |
| `include/pto/npu/a2a3/TAdd.hpp` | TADD 的策略类 `AddOp` 与 `TADD_IMPL`，展示模板的" instantiation"方式 |
| `include/pto/npu/a2a3/TAddS.hpp` | TADDS（加标量）的策略类与 `TADDS_IMPL` |
| `include/pto/npu/a2a3/TFmod.hpp` | 反例：不走模板、手工组合 intrinsic 的二元指令 |
| `include/pto/common/pto_instr.hpp` | 公共 API 薄壳层：`TADD`/`TFMOD` 对外签名（TSYNC + MAP_INSTR_IMPL） |
| `include/pto/common/pto_instr_impl.hpp` | 按"架构 × 后端"把 `TAdd.hpp`/`TFmod.hpp` 等实现头批量引入 |
| `include/pto/common/constants.hpp` | `REPEAT_BYTE`、`BLOCK_BYTE_SIZE`、`REPEAT_MAX` 等硬件常数 |
| `docs/isa/TADD.md`、`docs/isa/TFMOD.md` | 两条指令的 ISA 文档（语义、约束、示例） |

## 4. 核心概念与源码讲解

### 4.1 二元运算模板：TBinOp 的策略模式

#### 4.1.1 概念说明

A2/A3 上有 17 条逐元素二元指令（TADD、TSUB、TMUL、TDIV、TMAX、TMIN、TPOW、TAND、TOR、TXOR、TSHL、TSHR 及若干融合变体——可自行 `grep` 验证）。它们在硬件层的唯一区别是调用哪条 `vXXX` intrinsic；而"如何用 repeat/mask/stride 遍历一个 tile 的有效区"这件事完全相同。

`TBinOp.hpp` 就是把后者抽成模板，把前者留成策略类接口。这是一种典型的**控制反转**：指令文件（如 `TAdd.hpp`）不再各自写几百行循环编排，只需提供一个小结构体：

```text
TBinOp 模板（怎么遍历）
   └── 调用 Op::BinInstr(...)（算什么）──→ 映射到 vadd / vmul / vmax ...
```

策略类必须提供两个 `BinInstr` 重载（后面解释为什么是两个）。

#### 4.1.2 核心流程

`BinaryInstr` 入口根据"tile 数据是否连续"分派到不同路径，整体是一棵编译期（`if constexpr`）+ 运行期（`if`）混合的决策树：

```text
BinaryInstr(dst, src0, src1, validRow, validCol)
├── 小形状快路：Rows ≤ 255 且 Cols < 一个 repeat 的元素数
│     └── Bin1LNormModeSmall：一次 BinInstr，用连续 mask 盖住 validCol
├── 编译期可判定连续（Cols == ValidCol 或 Rows == 1）
│     └── BinaryInstrFastPath
│           ├── 形状对不齐 repeat / 总 repeat 数超上限 → Bin1LCountMode（计数 mask）
│           └── 否则 → Bin1LNormMode（整 repeat 一发 + 尾块补一发）
└── 一般路径 BinaryInstrGeneralPath（动态有效区、跨行不连续）
      ├── 运行期检测连续 → 同上 1L 两条路
      └── 不连续 → 按"行数 vs 每行 repeat 数"三选一：
            ├── repeat 总数很小（< SMALL_RPT_BINOP=4）→ Bin2LCountMode（逐行计数 mask）
            ├── 行数少、列 repeat 多且对齐 → Bin2LNormModeColVLAlign
            └── 否则 → Bin2LNormModeRowRpt（按行组织 repeat，Head/Tail 拆分）
```

两条路线的本质权衡是：

- **Norm 模式**（`set_mask_norm`）：用 `repeatStride` 让硬件自动跳行，一条 intrinsic 吃掉多行——发射指令数最少，但要求地址布局规整。
- **Count 模式**（`set_mask_count`）：把整块有效区当一维连续元素数处理，用 `SetVectorCount(validRow * validCol)` 圈住范围——对不对齐的形状更鲁棒，代价是每行/每块都要显式设 mask、且 `repeatStride` 退化。

另一个硬件约束在 [TBinOp.hpp:105-146](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TBinOp.hpp#L105-L146) 中显式处理：`repeats` 与 `repeatStride` 都是 8 位（上限 255）。当 tile 行数超过 255 或行距换算成 block 数超过 255 时，`Bin2LNormModeTail` 通过 `strideOverFlag` 降级为"每次只发 1 个 repeat、步长 1"的循环版本，用指令数换合法性。

#### 4.1.3 源码精读

**计数模式的骨架**——[TBinOp.hpp:20-29](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TBinOp.hpp#L20-L29)：进入计数 mask，把有效元素总数写给向量长度，发一条 `Op::BinInstr`，再恢复常规 mask。注意 `repeats` 传 0（由 mask 完全控制范围）：

```cpp
template <typename Op, typename T>
PTO_INTERNAL void Bin1LCountMode(
    __ubuf__ T* dstPtr, __ubuf__ T* src0Ptr, __ubuf__ T* src1Ptr, unsigned validRow, unsigned validCol)
{
    set_mask_count();
    SetVectorCount(validRow * validCol);
    Op::BinInstr(dstPtr, src0Ptr, src1Ptr, 0);
    set_mask_norm();
    SetFullVecMaskByDType<T>();
}
```

**整 repeat + 尾块模式**——[TBinOp.hpp:55-70](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TBinOp.hpp#L55-L70)：先一发吃掉所有完整 repeat（`headRepeats` 可以为 0），若还有尾巴，则换连续 mask 只处理剩余元素。`[[unlikely]]` 提示编译器尾块是冷路径：

```cpp
unsigned numElements = validRow * validCol;
unsigned headRepeats = numElements / elementsPerRepeat;
unsigned tailElements = numElements % elementsPerRepeat;
Op::BinInstr(dstPtr, src0Ptr, src1Ptr, headRepeats); // headRepeats can be zero
if (tailElements) [[unlikely]] {
    unsigned offset = headRepeats * elementsPerRepeat;
    SetContMaskByDType<T>(tailElements);
    Op::BinInstr(dstPtr + offset, src0Ptr + offset, src1Ptr + offset, 1);
    SetFullVecMaskByDType<T>();
}
```

**入口分派**——[TBinOp.hpp:240-260](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TBinOp.hpp#L240-L260)：`BinaryInstr` 先做小形状快路，再按"编译期能否证明数据连续"分流到 FastPath / GeneralPath。`TileData::Cols == TileData::ValidCol` 意味着容量形状与有效区重合（无尾块），这是纯 `if constexpr` 的判断：

```cpp
if constexpr ((TileData::Rows <= pto::REPEAT_MAX) && (TileData::Cols < elementsPerRepeat)) {
    // 小形状优化：一次 BinInstr + 连续 mask
    ...
}
if constexpr ((TileData::Cols == TileData::ValidCol) || (TileData::Rows == 1)) {
    BinaryInstrFastPath<...>(dstPtr, src0Ptr, src1Ptr, validRow, validCol);
} else {
    BinaryInstrGeneralPath<...>(dstPtr, src0Ptr, src1Ptr, validRow, validCol);
}
```

**硬件常数**来自 [constants.hpp:20-28](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/constants.hpp#L20-L28)：`REPEAT_BYTE = 256`、`REPEAT_MAX = 255`、`BLOCK_BYTE_SIZE = 32`、`REPEAT_STRIDE_MAX = 255`。模板参数 `elementsPerRepeat`（每 repeat 元素数）与 `blockSizeElem`（每 block 元素数）都是指令文件用这两个常数除以 `sizeof(T)` 算出来再传入的。

#### 4.1.4 代码实践

**实践目标**：用静态证据确认"一条模板派生一族指令"，而不是只信本讲的文字。

**操作步骤**：

1. 在仓库根目录执行：

   ```bash
   grep -l "TBinOp.hpp" include/pto/npu/a2a3/*.hpp | wc -l
   grep -l "TBinOp.hpp" include/pto/npu/a2a3/*.hpp
   ```

2. 挑 `TMul.hpp` 与 `TMax.hpp` 两个文件，diff 它们与 `TAdd.hpp` 的差异。
3. 打开 [include/pto/common/pto_instr_impl.hpp:22-34](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr_impl.hpp#L22-L34)，观察这些指令头如何被统一批量引入。

**需要观察的现象**：步骤 1 应输出 17（含 `TBinOp.hpp` 自身不算的话是 16 个指令文件 + 若干间接包含）；步骤 2 中三个文件的差异应当只集中在策略类（`AddOp`/`MulOp`/`MaxOp`）、intrinsic 名（`vadd`/`vmul`/`vmax`）和 dtype 白名单上，编排代码零差异。

**预期结果**：差异行数在 30 行以内。这验证了抽象的有效性：新指令的边际成本 ≈ 一个策略类 + 一层 `*_IMPL` 薄壳。（具体 diff 行数待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：`Bin1LNormMode` 里为什么要区分 `headRepeats` 和 `tailElements` 两次发射，而不是把总元素数一次性交给 mask？

**答案**：`repeats` 参数的上限是 255，且 repeat 粒度是 256 字节。完整 repeat 部分用 norm 模式一发（或多发）吃掉，硬件无需 mask 干预；只有不足一个 repeat 的尾巴才切换到连续 mask（`SetContMaskByDType`）精确圈住。这样绝大部分工作走免掩码的快路径，只有冷路径付 mask 切换的成本。

**练习 2**：`BinaryInstr` 的分派里，什么条件会走 `BinaryInstrGeneralPath` 而不是 `FastPath`？

**答案**：编译期无法证明 tile 数据连续——即 `TileData::Cols != TileData::ValidCol`（容量列宽大于有效列宽，行间有"空洞"）且 `Rows != 1`。此时 GeneralPath 再在运行期用 `(TileData::Cols == validCol) || (validRow == 1)` 复查一次，因为动态有效区可能恰好填满列宽。

**练习 3**：为什么模板参数要传 `TileData`（整个 tile 类型）而不是只传 `Rows`/`Cols` 两个数？

**答案**：分派既用容量形状（`TileData::Rows/Cols`，编译期常量，供 `if constexpr` 选择路径），也用有效区信息（`TileData::ValidCol` 参与连续性判断）以及 `RowStride`（参与 repeatStride 计算）。把 tile 类型整体作为参数，可以让所有这些编译期信息随模板一起推导，避免调用方手工拆装十几个非类型参数。

### 4.2 从模板到指令：TADD 的完整装配

#### 4.2.1 概念说明

`TBinOp.hpp` 只是"发动机"，一台完整指令还需要：对外 API（带事件同步）、契约检查（dtype/布局/有效区）、以及把 tile 引用降到 `__ubuf__` 裸指针的"点火"代码。`TAdd.hpp` 展示了这套装配的标准三段式，与 u2-l4 讲过的薄壳约定完全对应：

```text
用户代码 TADD(dst, src0, src1)                    ← common/pto_instr.hpp（TSYNC + MAP_INSTR_IMPL）
        └→ TADD_IMPL(dst, src0, src1)             ← TAdd.hpp：检查 + 取有效区 + 算常数
                └→ TAdd<...>(tile, tile, tile)     ← TAdd.hpp：__cce_get_tile_ptr 取 __ubuf__ 指针
                        └→ BinaryInstr<AddOp<T>, ...>  ← TBinOp.hpp：编排
                                └→ vadd(...)      ← CCE intrinsic，一条对应硬件向量指令
```

#### 4.2.2 核心流程

`TADD_IMPL` 的执行步骤：

1. `TAddCheck`：三个 `static_assert` 做 dtype 一致性、dtype 白名单、行主序布局检查（编译期失败，报错信息以 `"Fix: TADD ..."` 开头，直接告诉你怎么改）；两个 `PTO_ASSERT` 检查 src0/src1 有效区与 dst 一致（运行期）。
2. 以 `sizeof(T)` 折算 `blockSizeElem`（32B / sizeof）与 `elementsPerRepeat`（256B / sizeof）。
3. 从 tile 类型取三个 `RowStride`（编译期常量）。
4. 若三个 stride 相等，走同址版 `BinaryInstr`（一个 stride 模板参数，`if constexpr` 内部还能进一步合并路径）；否则走 [TBinOp.hpp:262-375](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TBinOp.hpp#L262-L375) 的"不同 tile 形状"重载，dst/src0/src1 各自带步长。

#### 4.2.3 源码精读

**策略类 `AddOp`**——[TAdd.hpp:20-32](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TAdd.hpp#L20-L32)：整个"TADD 是什么运算"的全部内容就在这两行 `vadd` 调用里。两个重载分别对应"默认步长"（8,8,8，即每 repeat 跳 256B）与"自定义步长"（模板编排层传入，用于跨行连续摆放）：

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

vadd 参数的完整含义为：`(dst, src0, src1, repeats, dstBlockStride, src0BlockStride, src1BlockStride, dstRepeatStride, src0RepeatStride, src1RepeatStride)`。blockStride 固定为 1（repeat 内部数据连续），可变的只有 repeatStride——这正是两个重载只差在 repeatStride 的原因。

**内核层 `TAdd`**——[TAdd.hpp:34-54](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TAdd.hpp#L34-L54)：用 `__cce_get_tile_ptr` 把 tile 句柄降为 UB 裸指针，然后按"三 stride 是否一致"选择 `BinaryInstr` 的两个重载之一：

```cpp
if constexpr (dstRowStride == src0RowStride && dstRowStride == src1RowStride) {
    BinaryInstr<AddOp<T>, T, TileDataDst, elementsPerRepeat, blockSizeElem, dstRowStride>(
        dstPtr, src0Ptr, src1Ptr, validRows, validCols);
} else {
    BinaryInstr<AddOp<T>, T, elementsPerRepeat, blockSizeElem, dstRowStride, src0RowStride, src1RowStride>(
        dstPtr, src0Ptr, src1Ptr, validRows, validCols);
}
```

**契约检查 `TAddCheck`**——[TAdd.hpp:56-78](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TAdd.hpp#L56-L78)：dtype 白名单为 `int32_t/int/int16_t/half/float16_t/float/float32_t`；强制行主序（`isRowMajor`）；运行期断言三个 tile 的有效区一致。A2A3 与 A5 的白名单不同（A5 更宽，见 [docs/isa/TADD.md:46-53](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TADD.md#L46-L53)），这就是"实现契约与架构绑定"的体现。

**公共 API 薄壳**——[pto_instr.hpp:112-118](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L112-L118)：用户调用的 `TADD` 只有三行——折叠等待事件、宏转发到 `TADD_IMPL`、返回记录事件：

```cpp
template <typename TileDataDst, typename TileDataSrc0, typename TileDataSrc1, typename... WaitEvents>
PTO_INST RecordEvent TADD(TileDataDst& dst, TileDataSrc0& src0, TileDataSrc1& src1, WaitEvents&... events)
{
    TSYNC(events...);
    MAP_INSTR_IMPL(TADD, dst, src0, src1);
    return {};
}
```

#### 4.2.4 代码实践

**实践目标**：亲手追一遍"TADD 一个调用点最终生成了几条 `vadd`"。

**操作步骤**：

1. 打开 [demos/baseline/add/csrc/kernel/add_custom.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp)，找到 `TADD` 的调用处，记下 tile 的容量形状与有效区来源。
2. 按 `sizeof(T)` 推算 `elementsPerRepeat`（例如 fp16 = 128）。
3. 对照 4.1.2 的分派树，手工判断该调用走哪条路径、发射几条 `BinInstr`。
4. 如有昇腾环境，用 CCE 工具链导出汇编（`.o` 反汇编或编译器 dump）数 `vadd` 条数核对；没有环境则写下你的推导过程。

**需要观察的现象**：典型 128×128 fp16 tile、有效区全满时，`numElements = 16384`，`headRepeats = 128`，`tailElements = 0`——理论上一条 `BinInstr(repeats=128)` 搞定（`repeats ≤ 255`）。

**预期结果**：推导结论应为"1 条 vadd"。若有效区是 100 列（非 repeat 对齐），则应推出 Norm 模式下"整块一发 + 尾块一发"共 2 条，或 Count 模式若干条，取决于连续性判断。汇编核对部分待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `TADD_IMPL` 里 `elementsPerRepeat` 和 `dstRowStride` 都用 `constexpr` 而不是普通变量？

**答案**：它们是模板非类型参数。`BinaryInstr` 内部的分派大量使用 `if constexpr`（如 `TileData::Cols == TileData::ValidCol`、`repeatStride <= REPEAT_STRIDE_MAX`），只有编译期常量才能喂给 `if constexpr`；同时常量折叠让所有偏移计算在编译期完成，运行期没有额外开销。

**练习 2**：`TAdd` 为什么要区分"三 stride 相同"与"不同"两个 `BinaryInstr` 重载？

**答案**：stride 相同是绝大多数场景（三个 tile 同形状），此时可以把 `TileData` 整个传进模板，走 4.1 节那棵更激进的优化分派树（含小形状快路、FastPath）；stride 不同时（如把窄 tile 结果累加进宽 tile 的一角），只能用 [TBinOp.hpp:262-375](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TBinOp.hpp#L262-L375) 的通用重载，dst/src0/src1 分别计算偏移。为常见路径保留更强的特化，是这类库的常规取舍。

### 4.3 标量运算变体：TBinSOp 与 TADDS

#### 4.3.1 概念说明

`TADDS(dst, src, scalar)` 表示 `dst = src + scalar`——第二个操作数是一个广播到全 tile 的标量寄存器值，而不是另一个 tile。硬件为此提供独立 intrinsic（`vadds`），因此 PTO 侧也有一套平行模板 `TBinSOp.hpp`，复用同样的 Norm/Count 双模式思想，但结构更简单：

- 操作数从三个降到两个 tile 指针 + 一个标量值（按值传递，走标量寄存器）。
- `repeatStride` 只需 dst 和 src 两个，没有 src1 的。
- 步长上限检查相应只查两份。

仓库中有 10 个指令文件直接包含 `TBinSOp.hpp`（TAddS/TSubS/TMulS/TDivS/TMaxS/TMins/TPowS 等），与二元版一一对应。

#### 4.3.2 核心流程

`TBinSInstr` 入口的分派逻辑与二元版同构：

```text
TBinSInstr(dst, src0, scalar, validRow, validCol)
├── 编译期可证连续（两 tile 都 Cols==ValidCol，或都 Rows==1）
│     ├── 对不齐 repeat / 超 repeat 上限 → BinS1LCountMode
│     └── 否则 → BinS1LNormMode
└── 运行期复查连续 → 同上
      └── 不连续 → countMode（repeat 总数 < SMALL_RPT=4）
                  / isColRpt（行少列多）→ ColVLAlign 或 2LCount
                  / 否则 → BinS2LNormModeRowRpt（Head/Tail）
```

注意 [TBinSOp.hpp:180-236](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TBinSOp.hpp#L180-L236) 中 `tileDataContinue` 的定义要求 **dst 和 src 两个 tile 同时**满足连续条件（`&&` 连接、`||` 分组），比二元版多了一份约束——因为任何一个操作数有空洞都会破坏"一维连续"假设。

#### 4.3.3 源码精读

**标量策略类**——[TAddS.hpp:18-29](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TAddS.hpp#L18-L29)：与 `AddOp` 唯一的区别是 intrinsic 换成 `vadds`、第三个操作数是标量值 `T src1` 而非指针：

```cpp
template <typename T>
struct AddSOp {
    PTO_INTERNAL static void BinSInstr(__ubuf__ T* dst, __ubuf__ T* src0, T src1, uint8_t repeats)
    {
        vadds(dst, src0, src1, repeats, 1, 1, 8, 8);
    }
    ...
};
```

**`TADDS_IMPL` 的标量专属检查**——[TAddS.hpp:46-71](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TAddS.hpp#L46-L71)：除了与 `TADD_IMPL` 同款的 dtype/有效区检查外，多了一条 `TileDataSrc::Loc == TileType::Vec` 的 `static_assert`——标量向量运算只对 UB 上的 Vec tile 有意义，Mat/Acc 等 Cube 侧 tile 会被编译期拒绝：

```cpp
static_assert(TileDataSrc::Loc == TileType::Vec, "TileType of src and dst tiles must be TileType::Vec.");
```

另注意 [TAddS.hpp:31-44](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TAddS.hpp#L31-L44) 的内核层 `TAddS` 没有"同 stride / 异 stride"双路径——它统一按 dst/src 两个 stride 调 `TBinSInstr`，因为两操作数的分派树已经在模板内部用 `if constexpr` 处理了。

#### 4.3.4 代码实践

**实践目标**：体会"标量版是二元版的投影"。

**操作步骤**：

1. 并排打开 `TBinOp.hpp` 与 `TBinSOp.hpp`，逐个函数对照：`Bin1LCountMode ↔ BinS1LCountMode`、`Bin2LNormModeHead ↔ BinS2LNormModeHead`……
2. 记录每个函数对的差异行（参数少一个指针/步长）。
3. 再对照 `TFmodS.hpp` 与 `TFmod.hpp`（都存在于 a2a3 目录），确认"二元 + 标量成对出现"是本指令族的惯例。

**需要观察的现象**：两文件结构与行数高度接近（各约 240 与 240 行），函数签名呈系统性的"减一个操作数"关系。

**预期结果**：能总结出一张"二元 → 标量"的机械改写规则表（指针参数→值参数、三步长→两步长、三 tile 连续→两 tile 连续）。这张表在综合实践中会直接复用。

#### 4.3.5 小练习与答案

**练习 1**：既然可以用 `TADD` + 一个"每元素都等于 c 的 tile"实现加标量，为什么还要独立的 `TADDS`？

**答案**：省一次 TLOAD 和一半 UB 占用。标量走标量寄存器广播，`vadds` 一条 intrinsic 完成；等价 tile 方案要先在 UB 里摆一个常量 tile 并多读一份操作数。tile 级 ISA 的指令膨胀（二元/标量成对）正是为了把这类常见模式留给硬件最短路径。

**练习 2**：`TBinSInstr` 里 `tileDataContinue` 为什么是 `(dst连续 && src连续) || (dst一行 && src一行)` 而不是只判 dst？

**答案**：连续性假设的是"所有操作数在内存中都构成一维连续序列"。二元/标量指令读 src 写 dst，任何一方存在行间空洞（Cols > ValidCol）都会让 `validRow * validCol` 的一维偏移与实际地址不符，导致读写错位。因此每个参与 tile 都必须满足条件；`Rows == 1` 的分支则是因为单行 tile 天然连续。

### 4.4 NPU intrinsic 映射与例外：TFMOD 为何不走模板

#### 4.4.1 概念说明

模板抽象成立的前提是：**硬件为这条运算提供了一条 1:1 的向量 intrinsic**（vadd、vmul、vmax…）。当运算没有对应 intrinsic 时，模板就套不进去。仓库里的 TFMOD 是最好的活教材：`fmod` 是一条**复合**运算：

\[ \mathrm{fmod}(a, b) = a - \mathrm{trunc}(a / b) \times b \]

硬件没有单条 `vmod`，于是 [TFmod.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TFmod.hpp) 用四条 intrinsic 串行组合实现，且每步之间插 `pipe_barrier(PIPE_V)` 强制 Vector 流水线内按序完成（后一步读前一步的输出，存在 RAW 依赖）。

#### 4.4.2 核心流程

`TFmod` 的执行流程：

```text
TFmod(kernel 层)
├── set_mask_count + set_vector_mask(0, validCols)：按行设 mask
└── 逐行循环（i = 0 .. validRows）
      └── FmodF32Instr(dst行, src0行, src1行)
            ├── vdiv   dst = a / b        ── pipe_barrier
            ├── vconv_f322f32z dst = trunc(dst)（取整）── pipe_barrier
            ├── vmul   dst = dst * b       ── pipe_barrier
            └── vsub   dst = a - dst       ── pipe_barrier
└── set_mask_norm + set_vector_mask(-1, -1)：恢复默认 mask
```

对比模板路线的三个显著退步：逐行循环（不能跨行用 repeatStride）、每条 intrinsic 只发 1 个 repeat、每步都要 barrier。这就是"没有原生 intrinsic"的真实代价——也是判断"一条新指令值不值得加"的性能直觉来源。

#### 4.4.3 源码精读

**复合 intrinsic 序列**——[TFmod.hpp:19-35](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TFmod.hpp#L19-L35)。注释直接写出了公式，四条 intrinsic 与公式四步一一对应：

```cpp
// Formula: fmod(a, b) = a - trunc(a/b) * b
struct FmodOp {
    PTO_INTERNAL static void FmodF32Instr(__ubuf__ float* dst, __ubuf__ float* src0, __ubuf__ float* src1)
    {
        vdiv(dst, src0, src1, 1, 1, 1, 1, 8, 8, 8);
        pipe_barrier(PIPE_V);
        vconv_f322f32z(dst, dst, 1, 1, 1, 8, 8);
        pipe_barrier(PIPE_V);
        vmul(dst, dst, src1, 1, 1, 1, 1, 8, 8, 8);
        pipe_barrier(PIPE_V);
        vsub(dst, src0, dst, 1, 1, 1, 1, 8, 8, 8);
        pipe_barrier(PIPE_V);
    }
};
```

注意所有 blockStride/repeatStride 都写死默认值（1/8），`repeats` 固定为 1——`FmodF32Instr` 每次只处理"一行中 mask 圈住的一段"，行间推进靠外层循环的指针加 `rowStride`（[TFmod.hpp:41-65](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TFmod.hpp#L41-L65)）。

**约束比模板版更严**——[TFmod.hpp:67-83](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TFmod.hpp#L67-L83)：dtype 白名单只有 float/float32_t（`vconv_f322f32z` 是 f32 专用取整），同样强制行主序、三 tile 有效区一致。这与 [docs/isa/TFMOD.md:44-49](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TFMOD.md#L44-L49) 的约束描述一致（文档还注明除零行为目标自定义、CPU 仿真在 debug 下会断言）。

**API 层一视同仁**——[pto_instr.hpp:2288](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L2288) 声明的 `TFMOD` 薄壳与 `TADD` 完全同构（TSYNC + MAP_INSTR_IMPL + RecordEvent）；[pto_instr_impl.hpp:97-98](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr_impl.hpp#L97-L98) 把 `TFmod.hpp`/`TFmodS.hpp` 与其他指令头并列引入。**模板 vs 手写只是实现内部的自由度，对用户不可见**——这正是"接口分叉集中在 common 层，实现按目录隔离"纪律的收益。

#### 4.4.4 代码实践

**实践目标**：完成规格指定的任务——基于 TBinOp 模板思路，列出"新增一条 TFMod"需要改动的文件清单，再对照仓库现状验证你的判断。

**操作步骤**：

1. 先不看仓库里已有的 `TFmod.hpp`，自己推演：假设要新增二元指令 `TFMOD`，按 u2-l4 与本讲的知识列出改动清单。你的清单应至少包含：
   - `include/pto/common/pto_instr.hpp`：新增 `TFMOD` API 薄壳（TSYNC + MAP_INSTR_IMPL）；
   - `include/pto/common/pto_instr_impl.hpp`：include 新实现头；
   - `include/pto/cpu/TFmod.hpp`：CPU 仿真实现（可用 u3-l4 的 `BINARY_OP_DEF` 骨架宏，一行写 `fmod` 语义）；
   - `include/pto/npu/a2a3/TFmod.hpp`（以及需要的 a5/a6 版本）：NPU 实现——若硬件有单条 intrinsic，写策略类套 `TBinOp.hpp`；否则手工组合；
   - `docs/isa/TFMOD.md`：ISA 文档；
   - `tests/cpu/st/testcase/tfmod/`：ST 用例四件套（kernel / main / gen_data / CMakeLists）。
2. 然后逐项打开仓库现状核对：`docs/isa/TFMOD.md`（存在）、[pto_instr.hpp:2288](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L2288)（`TFMOD` 声明存在）、[pto_instr_impl.hpp:97-98](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr_impl.hpp#L97-L98)（已引入）、`include/pto/npu/a2a3/TFmod.hpp` 与 `TFmodS.hpp`（存在）、`tests/cpu/st/testcase/tfmod/`（四件套齐全）。
3. 重点核对一个偏差：你的清单里若写了"NPU 侧应套 TBinOp 模板"，会发现真实现走了手工组合路线（4.4.3）。分析原因：没有单条 `vmod` intrinsic。

**需要观察的现象**：清单与现状的吻合度应当很高，唯一的结构性偏差就是"NPU 实现是否走模板"。

**预期结果**：得出结论——**新增一条二元指令的文件清单是固定的（约 6 处），唯一需要设计决策的是 NPU 实现路线**：有 1:1 intrinsic → 策略类 + TBinOp（约 100 行）；没有 → 手工编排（TFmod 路线）。CPU 侧与文档侧则与该决策无关。若要实际跑通 `tests/cpu/st/testcase/tfmod` 用例（`python3 tests/run_cpu.py`，参见 u1-l3），待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`FmodF32Instr` 里每条 intrinsic 后都要 `pipe_barrier(PIPE_V)`，而 `TBinOp` 模板里一条 `Op::BinInstr` 之后就结束了，不需要 barrier。为什么？

**答案**：barrier 解决的是同一流水线内的读后写（RAW）依赖：TFMOD 的下一步 intrinsic 要读上一步的输出 dst。`TBinOp` 的策略类只发**一条** intrinsic，不存在内部依赖；跨流水线的依赖（如 Vector 结果被 MTE3 写回）由 API 层的 RecordEvent/事件机制表达（u2-l3），不归实现层管。

**练习 2**：如果未来硬件提供了单条 `vmod` intrinsic，把 TFMOD 改造成 TBinOp 模板版需要动哪些代码？

**答案**：只需重写 `include/pto/npu/a2a3/TFmod.hpp`：删掉 `FmodOp`/逐行循环，新增含两个 `BinInstr` 重载的策略类（映射 `vmod`），再把 `TFmod` 内核层改为调用 `BinaryInstr<FmodOp<T>, ...>`。API 层、CPU 仿真、文档、ST 用例都不用动——这正体现了分层的好处。

**练习 3**：`TFmod.hpp` 头部版权年份是 2026，而 `TAdd.hpp` 是 2025。结合 `git log include/pto/npu/a2a3/TFmod.hpp` 你能推断什么？

**答案**：TFMOD 是比 TADD 晚加入仓库的指令（近期提交）。这符合 u1-l1 讲过的模式：新指令通常先落 CPU 仿真与文档，NPU 实现随后跟进；也说明"模板 vs 手写"的路线选择至今仍在演进。（具体提交时间以 `git log` 输出为准，待本地验证。）

## 5. 综合实践

**任务：为"TLog（逐元素自然对数，假想指令）"写一份完整的实现方案书。**

假设硬件提供了单条 `vlog` intrinsic（签名与 `vadd` 同构）。请产出：

1. **文件清单**：仿照 4.4.4 的核对结果，列出全部 6 处改动点（common API、impl 引入、cpu 实现、npu/a2a3 实现、ISA 文档、ST 用例目录）。
2. **策略类代码**：参照 [TAdd.hpp:20-32](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TAdd.hpp#L20-L32) 写出 `LogOp<T>` 的两个 `BinInstr` 重载（注意一元指令只有 src0 一个源操作数——思考 stride 参数少几个）。
3. **dtype 白名单**：`vlog` 只对浮点有意义，写出你的 `static_assert` 白名单并说明理由。
4. **路线判断**：既然是单条 intrinsic，说明它应走 `TBinOp` 模板；再想一想——一元指令的模板或许该叫 `TUnOp`，检查仓库里是否已存在类似的一元模板（提示：CPU 侧有 `UNARY_OP_DEF`，见 u3-l4；NPU 侧用 `grep -l "UnOp\|Unary" include/pto/npu/a2a3/*.hpp` 查证）。
5. **验证方式**：说明 ST 用例的 golden 数据怎么在 `gen_data.py` 里用 numpy 生成（`np.log`），比对方式参照 `tests/cpu/st/testcase/tfmod/gen_data.py`。

这个任务把本讲三个模块（二元模板、标量/变体思想、intrinsic 映射决策）与 u3-l4 的 CPU 骨架、u10-l1 的测试体系串成一条线，做完即具备贡献一条新指令的前置能力（完整流程见 u11-l1）。

## 6. 本讲小结

- NPU 侧逐元素指令分四层装配：公共 API 薄壳（TSYNC + MAP_INSTR_IMPL）→ `*_IMPL`（契约检查 + 常数折算）→ 内核层（tile 降 `__ubuf__` 指针）→ `TBinOp` 编排模板 → `vXXX` intrinsic。
- `TBinOp.hpp` 是策略模式：指令文件只写一个约 15 行的策略类（`AddOp` 等）映射到 intrinsic，遍历/分块/mask/步长全部由模板承担——17 条 A2A3 二元指令共享这一套代码。
- 分派树在 Norm 模式（repeatStride 让硬件跨行，最快）与 Count 模式（计数 mask，最鲁棒）之间选择，依据是形状对齐性与 255 的 repeats/repeatStride 上限；小形状有专门快路。
- 标量变体 `TBinSOp` + `vadds` 是二元版的"减一个操作数"投影：值参数代替指针、双步长代替三步长、连续性要求所有参与 tile 同时满足。
- 模板成立的前提是硬件有 1:1 intrinsic；TFMOD 没有单条 `vmod`，于是用 `vdiv/vconv/vmul/vsub` 四步组合 + `pipe_barrier` 逐行实现——对用户 API 层完全透明。
- 新增一条二元指令的文件清单固定（common API、impl 引入、cpu 实现、npu 实现、ISA 文档、ST 用例），唯一的设计决策是 NPU 侧走模板还是手工组合。

## 7. 下一步学习建议

本讲拆完的是"最规整"的指令族。接下来两讲进入同样大量复用模板、但语义更复杂的方向：

- **u4-l2 规约指令**：`TRowSum/TColSum` 与 TPart 部分规约——规约方向（行/列）打破逐元素的对称性，观察模板如何参数化"规约轴"，以及多核规约协议中 TPart 的角色。
- **u4-l3 数据重排指令**：`TGather/TScatter`、`MGather/MScatter`——按索引寻址后，地址不再能用单一 stride 描述，看实现如何退化到逐行/逐元素 gather。

建议同步阅读：`include/pto/npu/a2a3/TMul.hpp` 与 `TMax.hpp`（验证 4.1.4 的结论），以及 `docs/isa/TADDS.md`（标量变体的 ISA 文档，对照本讲 4.3）。
