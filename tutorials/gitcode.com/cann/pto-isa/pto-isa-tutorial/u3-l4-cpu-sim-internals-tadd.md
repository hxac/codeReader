# CPU 仿真实现剖析：以 TAdd 为例读透一条指令

## 1. 本讲目标

学完本讲，你应该能够：

1. 独立读懂 `include/pto/cpu/` 下任意一条指令的仿真实现。
2. 说清一条 TADD 调用从公共 API 到 CPU 循环体的完整链路。
3. 掌握 `ElementTileOp.h` / `ElementOp.h` 这套「一条模板派生一族指令」的通用仿真骨架。
4. 对比同一指令的 CPU 仿真实现与 NPU 真机实现，理解「功能正确」与「性能最优」两种实现目标差异。

## 2. 前置知识

- **IMPL 约定**：PTO 的公共指令 API（如 `TADD`）定义在 common 层，只做「TSYNC 等事件 → 转发到 `TADD_IMPL` → 返回 RecordEvent」三件事；真正的计算体在各后端的 `TADD_IMPL` 里，按 `__CPU_SIM` / `__CCE_AICORE__` 宏互斥编译（见 u2-l4）。
- **Tile 有效区**：Tile 容量形状编译期静态，有效区（validRow/validCol）运行期确定，指令只在有效区内计算（见 u2-l2）。
- **tile 布局**：非分形（`SLayout::NoneBox`）布局下行主序元素按 `r * Cols + c` 连续存放；分形布局（Nz/Zn 等）则需要 `GetTileElementOffset` 做逻辑坐标→物理偏移的映射（见 u2-l2、u3-l1）。
- **`PTO_INTERNAL`**：标记内部链接的宏，表示该函数是后端内部实现细节，不属于面向 kernel 开发者的公开 API。

本讲全部内容都在 CPU 仿真后端（`__CPU_SIM`）语境下，「仿真」一词指用宿主机普通 C++ 代码模拟 tile 指令的功能行为。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `include/pto/common/pto_instr.hpp` | 公共 API 层：`TADD` 用户接口，转发到 `TADD_IMPL` |
| `include/pto/cpu/TAdd.hpp` | TADD 的 CPU 仿真实现（本讲主角） |
| `include/pto/cpu/TSub.hpp` | TSUB 的 CPU 仿真实现（另一种实现风格，用于对照） |
| `include/pto/cpu/ElementTileOp.h` | 通用逐元素仿真骨架：`BinaryElementTileOp_Impl` / `UnaryElementTileOp_Impl` / `BINARY_OP_DEF` 宏 |
| `include/pto/cpu/ElementOp.h` | `ElementOp` 枚举与 `ElementOpCal<DType, op>` 运算分发模板 |
| `include/pto/cpu/tile_offsets.hpp` | `GetTileElementOffset`：逻辑坐标到 tile 存储偏移的映射 |
| `include/pto/cpu/parallel.hpp` | `parallel_for_rows` 行级多线程 + `PTO_CPU_VECTORIZE_LOOP` 向量化提示 |
| `include/pto/npu/a2a3/TAdd.hpp` | TADD 的 NPU 真机实现（对照用） |

## 4. 核心概念与源码讲解

### 4.1 TAdd 仿真实现：一条指令的最小 CPU 实现

#### 4.1.1 概念说明

CPU 仿真后端的目标只有一个：**功能正确**。它不需要模拟硬件流水线、repeat 粒度或 burst DMA，只需要对有效区内每个元素做与真机语义一致的计算。因此 TADD 的 CPU 实现就是一段朴素的「遍历有效区 → 逐元素相加」的 C++ 代码，外加两处性能友好的小设计：

1. 非分形布局下直接用 `base = r * Cols` 计算行首偏移，让内层循环地址连续，可被编译器自动向量化；
2. 通过 `cpu::parallel_for_rows` 把行分配到多个宿主机线程，模拟 Vector 核的吞吐（可选）。

#### 4.1.2 核心流程

调用链从 kernel 里的 `TADD(dst, src0, src1, events...)` 开始：

```
TADD(dst, src0, src1, events...)          # common/pto_instr.hpp，公共 API
  ├─ TSYNC(events...)                     # CPU 后端：事件是空桩，无事发生（u2-l3）
  ├─ MAP_INSTR_IMPL(TADD, ...)            # 宏拼接出 TADD_IMPL(dst, src0, src1)
  │    └─ TADD_IMPL                       # cpu/TAdd.hpp
  │         ├─ row/col = dst.GetValidRow/Col()   # 取目标 tile 的有效区
  │         └─ TAdd_Impl(dst.data(), src0.data(), src1.data(), row, col)
  │              └─ 双重循环：对每个 (r,c)，dst[idx] = src0[idx] + src1[idx]
  └─ return RecordEvent{}                 # 返回一个空的事件记录
```

`TAdd_Impl` 内部按「是否分形 × 行主序/列主序」四路分支：

- **非分形 + 行主序**：外层按行并行，内层 `idx = r * Cols + c` 连续递增，配 `PTO_CPU_VECTORIZE_LOOP` 提示编译器生成 SIMD。
- **非分形 + 列主序**：内外层交换（外层按列并行），`idx = c * Rows + r`，同样保证最内层连续。
- **分形（`SFractal != NoneBox`）**：每个元素都经 `GetTileElementOffset<tile_shape>(r, c)` 换算 Nz/Zn 分形摆放下的真实偏移，无法向量化，但功能正确。

#### 4.1.3 源码精读

公共 API 薄壳——TSYNC 等事件后转发，这三行在 CPU/NPU 两个后端完全一致：

[include/pto/common/pto_instr.hpp:112-118](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L112-L118)：`TADD` 公共接口，`MAP_INSTR_IMPL` 把调用转发到当前后端的 `TADD_IMPL`。

CPU 侧入口——取有效区，把 Tile 引用解包成裸指针：

[include/pto/cpu/TAdd.hpp:63-69](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TAdd.hpp#L63-L69)：`TADD_IMPL` 从 `dst` 读出 validRow/validCol，再调 `TAdd_Impl` 执行计算体。注意有效区以 **dst 为准**，src 的有效区一致性检查在 NPU 版里有（`TAddCheck`），CPU 版这里省略了。

非分形 + 行主序的快速路径——本讲最核心的 8 行：

[include/pto/cpu/TAdd.hpp:24-33](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TAdd.hpp#L24-L33)：`if constexpr` 在编译期按 tile 形状类型选择分支。行主序时 `base = r * tile_shape::Cols` 一次算出行首，内层 `PTO_CPU_VECTORIZE_LOOP` 循环里 `idx` 连续递增，三个 tile 用同一 `idx` 寻址——这要求三个 tile 形状类型完全相同（`tile_shape` 是单一模板参数）。

分形路径——逐元素坐标换算：

[include/pto/cpu/TAdd.hpp:45-51](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TAdd.hpp#L45-L51)：分形布局下不能直接 `r * Cols + c`，每个元素都要经 `GetTileElementOffset` 映射到分形摆放后的物理偏移。

坐标映射的实现：

[include/pto/cpu/tile_offsets.hpp:64-75](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/tile_offsets.hpp#L64-L75)：`GetTileElementOffset` 在非分形时退化为 `GetTileElementOffsetPlain`（行主序 `r*Cols+c` / 列主序 `c*Rows+r`），分形时拆成「子块坐标 + 块内坐标」两段换算。

行级并行：

[include/pto/cpu/parallel.hpp:97-101](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/parallel.hpp#L97-L101)：`parallel_for_rows` 把行数交给 `parallel_for_1d`，元素总量（rows*cols）小于阈值 16384 时直接串行，否则按宿主机核数分块开 `std::thread`（见 [include/pto/cpu/parallel.hpp:54-95](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/parallel.hpp#L54-L95)）。这就是 CPU 仿真「大 tile 跑得也不慢」的原因。

#### 4.1.4 代码实践

**实践目标**：亲手跟踪一次 TADD 的完整调用链，确认「公共 API → IMPL → 循环体」三层结构。

**操作步骤**：

1. 打开 `include/pto/common/pto_instr.hpp` 第 112–118 行，找到 `TADD` 的定义，记下 `MAP_INSTR_IMPL(TADD, ...)` 这一行。
2. 打开 `include/pto/cpu/TAdd.hpp`，对照 `TADD_IMPL`（第 64 行）与 `TAdd_Impl`（第 20 行），确认签名逐参数对应。
3. 写一个最小调用示例（示例代码，非项目原有文件）：

```cpp
// 示例代码：CPU 仿真下调用 TADD 的最小骨架
#include <pto/pto-inst.hpp>
using TileF16 = TileVec<float16_t, 64, 64>;   // 64x64 fp16 UB tile（类型名以 pto_tile.hpp 为准）

void demo(TileF16& dst, TileF16& a, TileF16& b)
{
    dst.SetValidShape(64, 64);                 // 有效区 = 容量
    TADD(dst, a, b);                           // 无事件参数也合法（WaitEvents 为空包）
}
```

4. 仿照 `tests/cpu/st/testcase/tadd` 的 ST 用例结构把 demo 挂进构建（参考 u1-l4 综合实践），或直接在已有 tadd 用例里下断点。

**需要观察的现象**：调试器单步进入 `TADD` 后，`TSYNC` 是空操作，`MAP_INSTR_IMPL` 一步就落进 `TADD_IMPL`，再一步进入 `TAdd_Impl` 的循环体。

**预期结果**：确认调用栈恰好三层，循环体执行 64×64 = 4096 次加法；由于 4096 < 16384 阈值，`parallel_for_rows` 走串行路径。断点观察属于源码阅读型实践，具体栈帧**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`TADD_IMPL` 为什么以 `dst` 的有效区为准，而不检查 src 的有效区？

**参考答案**：CPU 版 `TADD_IMPL`（[TAdd.hpp:63-69](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TAdd.hpp#L63-L69)）确实省略了检查，这是仿真实现的简化——它假定调用方传参合法；而 NPU 版有 `TAddCheck`（[npu/a2a3/TAdd.hpp:70-78](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TAdd.hpp#L70-L78)）用 `PTO_ASSERT` 校验 src0/src1 有效区与 dst 一致。这也提醒我们：CPU 仿真通过 ≠ 一定没有参数错误，部分检查只在真机路径生效。

**练习 2**：为什么非分形路径要用 `if constexpr (tile_shape::isRowMajor)` 在编译期分两支，而不是运行期 if？

**参考答案**：`isRowMajor`、`Cols`、`Rows` 都是 tile 形状类型里的编译期常量。用 `if constexpr` 可以让未选中的分支根本不实例化，编译器能确信内层循环的地址是连续的，配合 `PTO_CPU_VECTORIZE_LOOP` 生成 SIMD 指令；运行期 if 则两个分支都要编译且向量化机会丢失。

### 4.2 通用 Element 仿真骨架：一套模板派生一族指令

#### 4.2.1 概念说明

TAdd 是「手写循环体」风格；但逐元素指令有几十条（SUB、MUL、NEG、RELU、SHL、AND……），如果每条都手写一遍双循环，代码会大量重复。PTO 的 CPU 后端为此提供了一套三层骨架：

| 层 | 文件 | 职责 |
| --- | --- | --- |
| 运算层 | `ElementOp.h` | `ElementOp` 枚举列出所有逐元素运算；`ElementOpCal<DType, op>::apply` 是每个运算的一行实现 |
| 遍历层 | `ElementTileOp.h` | `BinaryElementTileOp_Impl` / `UnaryElementTileOp_Impl` 负责有效区遍历、布局寻址、行并行 |
| 指令层 | 各指令头文件或 `ElementTileOp.h` 内的宏 | `T##OPNAME##_IMPL` 把两者拼起来 |

这个设计的关键是：**遍历逻辑写一次，运算逻辑一行一条，新增指令只需一行宏展开**。

注意 `ElementTileOp.h` 的遍历层比 `TAdd.hpp` 的手写版本更通用：它允许 dst/src0/src1 是**不同的 tile 类型**（三个模板参数独立），因此用 `GetTileElementOffset<TileDataDst>`、`GetTileElementOffset<TileDataSrc0>` 分别算三个 tile 各自的偏移——代价是丢失了「同类型连续寻址」的向量化机会。

#### 4.2.2 核心流程

以 `TSUB_IMPL`（若经由骨架实现）为例：

```
TXXX(dst, src0, src1, events...)
  └─ TXXX_IMPL(dst, src0, src1)                    # 指令层：一行宏 BINARY_OP_DEF(Xxx) 生成
       └─ BinaryElementTileOp_Impl<ElementOp::OP_XXX>(dst, src0, src1)
            ├─ static_assert：三个 tile 的 TileDType 相同
            ├─ assert：三个 tile 的有效区一致
            ├─ 取出三个 tile 的 data() 指针与有效区
            └─ 按 isRowMajor 分支 → parallel_for_rows 逐行
                 └─ 对每个 (r,c)：
                      ElementOpCal<DType, OP_XXX>::apply(dst[idx], src0[idx], src1[idx])
```

#### 4.2.3 源码精读

运算层——每个运算就是 `ElementOpCal` 的一个特化：

[include/pto/cpu/ElementOp.h:21-86](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/ElementOp.h#L21-L86)：`ElementOp` 枚举把二元、一元、三元、tile-标量四类逐元素运算统一编号；默认版本（[L88-91](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/ElementOp.h#L88-L91)）的 `apply` 直接 `assert(false)`，即未实现即失败。

[include/pto/cpu/ElementOp.h:93-98](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/ElementOp.h#L93-L98)：`OP_ADD` 的特化——`dst = src0 + src1`，一行就是一条指令的全部数学语义。

遍历层——二元骨架：

[include/pto/cpu/ElementTileOp.h:18-36](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/ElementTileOp.h#L18-L36)：`BinaryElementTileOp_Impl` 开头做两类检查：`static_assert` 保证三个 tile 底层数据类型一致（编译期），`assert` 保证有效区一致（运行期），然后取指针与有效区。

[include/pto/cpu/ElementTileOp.h:39-57](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/ElementTileOp.h#L39-L57)：行主序分支下，对每个 `(r,c)` 分别用三个 tile 类型各自的 `GetTileElementOffset` 求偏移，再调 `ElementOpCal<DType, op>::apply`。列主序分支（L48 起）对称地交换内外层。

指令层——一行宏生成一条指令的 IMPL：

[include/pto/cpu/ElementTileOp.h:97-115](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/ElementTileOp.h#L97-L115)：`BINARY_OP_DEF(OPNAME)` 宏展开成 `T##OPNAME##_IMPL`，内部转发到 `BinaryElementTileOp_Impl<ElementOp::OP_##OPNAME>`；紧随其后的 `BINARY_OP_DEF(SHL)` 等 10 行就定义了 TSHL/TOR/TMIN 等十条二元指令。`UNARY_OP_DEF` 同理生成 TNEG/TRELU 等。

带精度算法参数的指令也可以直接复用骨架：

[include/pto/cpu/ElementTileOp.h:132-136](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/ElementTileOp.h#L132-L136)：`TEXP_IMPL` 用模板默认参数 `PrecisionType = ExpAlgorithm::DEFAULT` 接住算法选择，实际计算仍走 `UnaryElementTileOp_Impl<ElementOp::OP_EXP>`——CPU 仿真通常忽略精度算法差异，只保证默认路径正确。

#### 4.2.4 代码实践

**实践目标**：体验「新增一条逐元素指令 = 在骨架上登记一行」。

**操作步骤**：

1. 阅读 [include/pto/cpu/ElementTileOp.h:111-124](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/ElementTileOp.h#L111-L124) 中 `BINARY_OP_DEF(MIN)` 与 `UNARY_OP_DEF(RELU)` 两个例子。
2. 在纸上（不要改源码）写出：如果要新增 `TABSMAX`（按元素取 `max(|a|,|b|)`），你需要：
   - 在 `ElementOp` 枚举（ElementOp.h）加一项 `OP_ABSMAX`；
   - 为 `ElementOpCal<DType, ElementOp::OP_ABSMAX>` 写一个特化，`apply` 为 `dst = std::max(std::abs(src0), std::abs(src1));`；
   - 加一行 `BINARY_OP_DEF(ABSMAX)`。
3. 对照 `docs/isa/` 目录确认 PTO 是否已有语义相近的指令（如 TMAX）。

**需要观察的现象 / 预期结果**：三个改动点中，遍历、并行、布局寻址代码**零改动**——这正是骨架的价值。本练习为源码阅读型设计，若在本地 fork 中真实实现，还需同步补 `docs/isa` 文档与 `tests/cpu/st/testcase` 用例（完整流程见 u11-l1）。运行验证**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`TAdd.hpp` 手写循环和 `BinaryElementTileOp_Impl` 骨架都能实现 TADD，二者取舍是什么？

**参考答案**：手写版（`TAdd.hpp`）绑定单一 `tile_shape`，三个 tile 同类型时可用 `base + c` 连续寻址并加 `PTO_CPU_VECTORIZE_LOOP`，性能最好，还能对非分形布局走快速路径；骨架版（`ElementTileOp.h`）支持三个 tile 类型互不相同、偏移分别计算，通用性最好，新增指令零成本。TADD 是最常用指令之一所以值得手写，而 SHL/AND/PRELU 等长尾指令全部走骨架。

**练习 2**：`ElementOpCal` 的默认模板对未实现的 op 会怎样？这个设计有什么好处？

**参考答案**：默认版 `apply` 直接 `assert(false && "Unsupported element op.")`（[ElementOp.h:88-91](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/ElementOp.h#L88-L91)），即编译能通过、一旦真的调用立刻失败。好处是新增枚举项后忘记写特化不会静默算错，而是运行期立刻暴露。

### 4.3 仿真与 NPU 实现对照：同一签名的两种人生

#### 4.3.1 概念说明

同一条 TADD，在 `include/pto/cpu/TAdd.hpp` 与 `include/pto/npu/a2a3/TAdd.hpp` 里是两份完全不同的实现，但**入口签名逐字相同**（`TADD_IMPL(dst, src0, src1)`），这正是 `MAP_INSTR_IMPL` 宏能在编译期无缝切换后端的前提（u2-l4）。理解两份实现的差异，就理解了「ISA 定义行为、后端实现路径」这句话的含义：

- **CPU 版**：目标是功能正确——遍历有效区，逐元素 `+`。
- **NPU 版**：目标是贴紧硬件——把 tile 数据映射为昇腾 Vector 核的 `vadd` intrinsic，按 repeat（256B 硬件重复单元）和 block stride 组织数据通路，并做严格的编译期/运行期合法性检查。

#### 4.3.2 核心流程

NPU 版 `TADD_IMPL` 的流程：

```
TADD_IMPL(dst, src0, src1)
  ├─ TAddCheck：static_assert dtype 白名单 / 行主序约束 + PTO_ASSERT 有效区一致
  ├─ 编译期推导：blockSizeElem = 32B / sizeof(T)，elementsPerRepeat = 256B / sizeof(T)
  ├─ 从 tile 类型取 RowStride（dst/src0/src1 各自的）
  └─ TAdd<...>(...)                        # __tf__ 设备函数
       ├─ __cce_get_tile_ptr：把 tile 句柄换成 __ubuf__ 指针
       └─ BinaryInstr<AddOp<T>, ...>       # TBinOp.hpp 通用模板（u4-l1 展开）
            └─ AddOp<T>::BinInstr → vadd(dst, src0, src1, repeats, 1,1,1, ...)
```

#### 4.3.3 源码精读

运算内核——一行 intrinsic：

[include/pto/npu/a2a3/TAdd.hpp:20-32](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TAdd.hpp#L20-L32)：`AddOp<T>::BinInstr` 把 PTO 语义映射到昇腾 Vector 核的 `vadd` intrinsic，参数里的 `repeats` 和三个 repeat stride 由 `TBinOp.hpp` 的 `BinaryInstr` 模板按 tile 形状折算（这个模板是 u4-l1 的主角，此处只看调用点）。

严格的检查——NPU 版比 CPU 版多出的整段逻辑：

[include/pto/npu/a2a3/TAdd.hpp:56-78](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TAdd.hpp#L56-L78)：`TAddCheck` 用 `static_assert` 限制 dtype 白名单（int32/int16/half/float 等）和「仅支持行主序」，用 `PTO_ASSERT` 校验 src0/src1 有效区与 dst 一致——对照 CPU 版（4.1.3）省略检查，可见真机后端承担了更多契约验证。

入口对齐——签名相同的 IMPL：

[include/pto/npu/a2a3/TAdd.hpp:80-94](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TAdd.hpp#L80-L94)：NPU 版 `TADD_IMPL` 先检查，再从 tile 类型编译期推导 `blockSizeElem`/`elementsPerRepeat` 与三个 `RowStride`，最后调设备函数 `TAdd`。与 CPU 版 [TAdd.hpp:63-69](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TAdd.hpp#L63-L69) 对比：函数名、参数列表完全一致，函数体一个是双重循环、一个是 intrinsic 编排——这就是「XXX_IMPL 签名逐字相同、按架构×后端互斥编译」的实例。

两版对照表：

| 维度 | CPU 仿真版 | NPU 真机版（a2a3） |
| --- | --- | --- |
| 计算体 | `dst[idx] = src0[idx] + src1[idx]` 双重循环 | `vadd` intrinsic + repeat/block stride 编排 |
| 寻址 | `GetTileElementOffset` 或连续 `base+c` | `__cce_get_tile_ptr` 取 `__ubuf__` 指针 |
| 布局支持 | 行/列主序、分形均可（分形走慢路径） | 仅行主序（`static_assert` 拦截） |
| 并行 | `parallel_for_rows` 宿主机线程 | 硬件 Vector 核天然 SIMD |
| 参数检查 | 基本省略 | dtype 白名单 + 有效区断言 |
| 事件/流水线 | 空桩、按序执行 | MTE2/V/MTE3 真流水线（u2-l3） |

#### 4.3.4 代码实践（本讲指定实践任务）

**实践目标**：对照 `include/pto/cpu/TSub.hpp` 与 `include/pto/cpu/TAdd.hpp`，画出 CPU 仿真实现的调用关系图，归纳两种实现风格。

**操作步骤**：

1. 通读 [include/pto/cpu/TSub.hpp:19-46](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TSub.hpp#L19-L46)（`TSUB` 在本仓库存在，无需退回 TMul）。
2. 与 [include/pto/cpu/TAdd.hpp:19-69](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TAdd.hpp#L19-L69) 逐段对比，重点关注：
   - TSub 用 `dst.SetElement(r, c, src0.GetElement(r, c) - src1.GetElement(r, c))`（经 Tile 成员函数读写，自动处理布局），而 TAdd 用 `data()` 裸指针 + 手工偏移；
   - TSub 的 `TSUB_IMPL` 里反而**有** `static_assert` dtype 检查和两个 `PTO_ASSERT` 有效区检查——检查是否齐全因指令而异，不能想当然；
   - TSub 没有分形分支（`SetElement` 内部消化了布局差异）。
3. 画出如下形式的调用关系图（文字版即可）：

```
TADD(dst,s0,s1,ev) ── common/pto_instr.hpp:113
  └ TADD_IMPL ── cpu/TAdd.hpp:64          TSUB(dst,s0,s1,ev) ── common/pto_instr.hpp
       └ TAdd_Impl(data 指针)                  └ TSUB_IMPL ── cpu/TSub.hpp:31  [dtype 检查 + 有效区断言]
            ├ 非分形：base+c 连续寻址               └ TSub_Impl ── cpu/TSub.hpp:20
            │   + VECTORIZE_LOOP                     └ SetElement/GetElement 成员函数读写
            └ 分形：GetTileElementOffset
```

4. 再到 [include/pto/cpu/ElementTileOp.h:111-124](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/ElementTileOp.h#L111-L124) 数一数骨架一行定义了哪些指令，把它们也标进图里。

**需要观察的现象**：三条路径（手写指针版 TAdd、成员函数版 TSub、骨架宏版 TMIN/TRELU）风格不同但结构同构：`公共 API → *_IMPL（取有效区/检查）→ *_Impl（循环体）`。

**预期结果**：得到一张「同一抽象、三种实现风格」的对照图；能说出哪种风格性能最好（TAdd 手写）、哪种最省代码（骨架宏）、哪种最简洁安全（SetElement）。图的具体形态因人而异，**待本地验证**的只是你对代码细节的抄录是否准确。

#### 4.3.5 小练习与答案

**练习 1**：CPU 仿真通过、真机却可能出错的情形，本讲能看到哪些线索？

**参考答案**：至少三类：(1) 列主序 tile——CPU 版 TAdd 支持列主序分支，NPU 版 `TAddCheck` 直接 `static_assert` 拒绝；(2) 分形布局——CPU 走 `GetTileElementOffset` 慢路径照样算对，NPU 上是否有对应通路要看指令支持；(3) src 有效区不一致——CPU 版 TAdd 不检查，NPU 版运行期断言才拦截。所以 CPU 仿真只验证功能逻辑，布局与契约约束需对照 NPU 实现或 ISA 文档。

**练习 2**：NPU 版从 tile 类型推导 `elementsPerRepeat = REPEAT_BYTE / sizeof(T)`，这个量在 CPU 版里有对应物吗？

**参考答案**：没有。`elementsPerRepeat` 是昇腾 Vector 硬件 repeat 单元（256B）的概念，CPU 仿真不模拟硬件 repeat 粒度，内层循环直接走满 `validCol`（或经向量化提示被编译器切成宿主机 SIMD）。这也再次说明：CPU 实现对齐的是**语义**，不是**微架构**。

**练习 3**：为什么 `TAdd.hpp`（CPU）能接受分形布局，`TAdd`（NPU a2a3）却只支持行主序？

**参考答案**：CPU 仿真只需「按逻辑坐标读对/写对元素」，`GetTileElementOffset` 可以统一处理任意摆放；NPU 的 `vadd` intrinsic 直接在 `__ubuf__` 连续地址上工作，重复步长等参数按行主序推导，分形数据通路需要专门的编排（TBinOp 模板按行主序假设折算 stride），因此用编译期断言把不支持的情况显式拦下。

## 5. 综合实践

**任务：给「读任意一条 CPU 指令」写一张可复用的路线卡。**

1. 从 `include/pto/cpu/` 任选一条你感兴趣的指令头文件（如 `TMul.hpp`、`TReciprocal` 相关、或 `TExp` 所在文件，用 `Glob: include/pto/cpu/T*.hpp` 列出全部候选）。
2. 用本讲的三步法读它：
   - 第一步：在 `include/pto/common/pto_instr.hpp` 用 Grep 找到公共 API，确认它转发到哪个 `*_IMPL`；
   - 第二步：读 CPU 版 `*_IMPL`，标出「有效区从哪取、检查有哪些、循环体什么风格（手写指针 / SetElement / 骨架模板）」；
   - 第三步：若 `include/pto/npu/a2a3/` 有同名文件，对照记录两版在 dtype 白名单、布局约束、检查力度上的差异。
3. 在 `tests/cpu/st/testcase/` 找它的 ST 用例（目录名通常是指令小写），看 `gen_data` 里 golden 是怎么算的。
4. 产出一张五行卡片：指令名 / 公共 API 行号 / IMPL 风格 / NPU 差异点 / 对应 ST 用例。

这张卡片就是你在 u4、u5 单元精读几十条指令时的「阅读模板」。

## 6. 本讲小结

- CPU 仿真的目标是**功能正确**：一条指令的 `*_IMPL` 取出有效区，循环体对有效区逐元素执行与真机一致的数学语义。
- 调用链固定三层：公共 API（`pto_instr.hpp`，TSYNC+MAP_INSTR_IMPL）→ `*_IMPL`（取有效区/检查）→ `*_Impl`（循环体）。
- 三种实现风格并存：手写裸指针（TAdd，可向量化、最快）、成员函数 SetElement/GetElement（TSub，简洁）、骨架模板 `BinaryElementTileOp_Impl`/`UNARY_OP_DEF` 宏（一行新增一条长尾指令）。
- `ElementOp.h` 的 `ElementOpCal<DType, op>` 特化 = 每条指令的一行数学语义；默认特化 assert 失败防静默错算。
- CPU 与 NPU 版 `*_IMPL` 签名逐字相同、按宏互斥编译；NPU 版多出 dtype 白名单、布局约束与有效区断言，CPU 通过不代表真机契约全部满足。
- `parallel_for_rows`（元素 ≥16384 才开线程）与 `PTO_CPU_VECTORIZE_LOOP` 是 CPU 仿真仅有的两处性能设计。

## 7. 下一步学习建议

本讲之后，你已具备「单条指令双后端对照」的阅读能力。下一步：

- **u4-l1（逐元素与标量运算族）**：深入 `include/pto/npu/a2a3/TBinOp.hpp` / `TBinSOp.hpp`，看 NPU 侧如何用一套模板派生 TAdd/TMul 等几十条指令——与本讲 CPU 侧的 `ElementTileOp.h` 骨架互为镜像。
- 顺手阅读 `include/pto/cpu/ElementOp.h` 的后半部分（tile-标量运算 `OP_ADDS` 等特化），为 u4 的标量变体指令做铺垫。
- 若你对「CPU 仿真的内存模型与多核模拟」更感兴趣，可跳到 u10-l2（CPU 仿真器内幕），但建议先完成单元四。
