# 架构适配：A5/A6 后端与跨代迁移

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `include/pto/npu/` 下 a2a3 / a5 / a6 / kirin 各目录的组织方式差异，以及一条指令的实现如何被架构宏路由到具体目录。
2. 掌握 A5 引入的新编程形态：SIMT（warp 级线程模型，以 engram_simt 为标本）、Int64 软件模拟扩展、MX 缩放因子指令。
3. 以 TMatmul 为标本，对比同一指令在 a2a3 / a5 / a6 三代后端的实现差异，并写一份可复用的迁移要点清单。
4. 理解 PTO 在「性能」与「抽象统一」之间的架构取舍：哪些东西跨代不变（API 薄壳、bias 指针打包），哪些东西允许每代自由重写（intrinsic 契约、检查逻辑、片上容量）。

本讲是学习手册的倒数第二讲，属于二次开发的「架构演进」主题。它建立在 u11-l1（新增一条指令的完整闭环）与 u5-l5（MX 混合精度矩阵乘）之上：u11-l1 告诉你「一条指令横切哪几层文件」，本讲告诉你「这些文件在每一代硬件上为什么长得不一样、迁移时要改什么」。

## 2. 前置知识

本讲默认你已掌握前几讲的内容，这里只做要点回顾与少量新术语补充。

### 2.1 已有认知回顾（来自前置讲义）

- **三维坐标定位一条指令**（u1-l2）：同一条指令是「指令 × 后端 × 架构」坐标下的多个同名文件，如 `TADD` 同时存在于 `include/pto/cpu/` 与 `include/pto/npu/a2a3/`。
- **编译期后端路由**（u2-l4）：`__CPU_SIM` / `__CCE_AICORE__` / `__COSTMODEL` 三宏决定后端；`arch_macro.hpp` 把编译器传入的 `__NPU_ARCH__` 数字翻译成 `PTO_NPU_ARCH_A2A3 / A5 / A6` 等语义宏；`*_IMPL` 宏转发约定让公共 API 薄壳与实现解耦。
- **TMatmul 基础**（u5-l1）：数据通路 GM→L1（`TileType::Mat`）→L0A/L0B（`TileLeft`/`TileRight`）→累加器（`TileAcc`），M/K/N 取自各 tile 的 validRow/validCol，底层 intrinsic 是 `mad`。
- **MX 混合精度**（u5-l5）：K 维每 32 个元素共享一个 E8M0 缩放因子，缩放直接进入 Cube 点积；scale 走「地址绑定」通路（`GetScaleAddr` 地址编码）。
- **新增指令的五层横切**（u11-l1）：公共 API 薄壳 → Op 枚举与流水线登记 → 各后端 `*_IMPL` → 汇总头挂载 → ISA 文档与 ST 用例。

### 2.2 本讲新术语

| 术语 | 含义 |
|---|---|
| SoC 代际 | 昇腾芯片代，A2（910B）/A3（910C）共用一套实现，A5（950）、A6 为新一代 |
| intrinsic | CCE 编译器内置的硬件指令函数（如 `mad`、`vadd`），是指令实现的最终落点 |
| SIMT | Single Instruction Multiple Threads，A5 新增的 warp 级线程编程形态，与 SIMD（tile 级向量）相对 |
| warp | SIMT 的调度单位，A5 上一个 warp 固定 32 个线程（lane） |
| RegTensor | A5 引入的寄存器张量包装类型，把裸指针操作升级为带类型的寄存器对象 |
| 能力位（capability） | `ArchTraits` 中按架构特化的 `SupportsXxx` 编译期常量，用于拦截不支持的用法 |
| L0C | Cube 累加器所在的片上缓冲，容量随架构不同（本讲关键差异点） |

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [include/pto/common/arch_macro.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/arch_macro.hpp) | 把 `__NPU_ARCH__` 数字翻译为 `PTO_NPU_ARCH_*` 语义宏与特性宏 |
| [include/pto/common/pto_instr_impl.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr_impl.hpp) | 按架构宏互斥挂载对应目录的实现头文件 |
| [include/pto/npu/a6/header.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a6/header.hpp) | A6 组合入口：6 条专用指令 + 其余复用 A5 |
| [include/pto/npu/a2a3/TMatmul.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TMatmul.hpp) | A2/A3 矩阵乘实现（迁移对比基准） |
| [include/pto/npu/a5/TMatmul.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a5/TMatmul.hpp) | A5 矩阵乘实现（新增 MX、FP8 支持） |
| [include/pto/npu/a6/TMatmul.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a6/TMatmul.hpp) | A6 矩阵乘实现（混合精度分派宏） |
| [include/pto/npu/a5/Int64Common.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a5/Int64Common.hpp) | A5/A6 的 Int64 双 32 位 lane 软件模拟 |
| [include/pto/npu/a5/TGetScaleAddr.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a5/TGetScaleAddr.hpp) | MX 缩放因子地址编码指令 |
| [include/pto/npu/a5/MGather.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a5/MGather.hpp) | 含 SIMT kernel 的矩阵级 Gather 实现 |
| [kernels/manual/a5/engram_simt/README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/engram_simt/README.md) | A5 SIMT 编程形态的完整探索算子文档 |
| [kernels/manual/a5/engram_simt/engram-simt_kernel.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/engram_simt/engram-simt_kernel.cpp) | engram SIMT kernel 源码 |
| [include/pto/common/arch_capability.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/arch_capability.hpp) | `ArchTraits` 能力位，按架构特化 |
| [include/pto/common/buffer_limits.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/buffer_limits.hpp) | 各架构片上缓冲容量常量（L0A/L0B/L0C） |
| [include/README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/README.md) | 逐指令后端支持状态表 |

## 4. 核心概念与源码讲解

### 4.1 A5/A6 目录组织：架构宏路由与分层复用

#### 4.1.1 概念说明

`include/pto/npu/` 是「真机后端」的家，按 SoC 代际分目录。当前快照下的目录规模差异很大：

| 目录 | 条目数（含 README） | 定位 |
|---|---|---|
| `a2a3/` | 121 | A2（910B）/A3（910C）共用一套实现 |
| `a5/` | 138 | A5（950）完整实现 + 新形态指令 |
| `a6/` | 11 | A6 只为「与 A5 行为不同」的指令写专用实现，其余复用 A5 |
| `kirin9030/`、`kirinX90/`、`kirinDev0000/` | 19 等 | Kirin 系列后端 |

这个规模差异本身就是设计宣言：**a2a3 与 a5 是两套「全量」实现，a6 是一套「增量」实现**。A6 不是把 130 个头文件再抄一遍，而是只重写行为确实不同的 6 条指令（TLoad/TExtract/TMatmul/TReshape/TQuant/SyncAll），其余经组合头直接引用 A5 的头文件。这背后是跨代迁移的核心经济学：新一代硬件上大部分指令的语义与数据通路不变，变的只是个别 intrinsic 契约。

对比 a2a3 与 a5 两个全量目录的文件名差集，还能看出指令族的演化：

- **A5 新增**：`Int64Binary/Int64Common/Int64Div/Int64Rearrange/Int64Reduce`（Int64 扩展五件套）、`TGetScaleAddr`（MX 缩放地址）、`THistogram`、`TRandom`、`TRsqrt`、`TInterleave/TDeInterleave`、若干标量位运算变体（`TAndS/TOrS/TXorS`）。
- **A5 重组**：a2a3 的行规约族 `TRowSum/TRowMax/TRowMin` 在 a5 合并进 `TRowReduce/TRowReduceIdx` 公共骨架；`TPartOp/TPartArgOp` 演化为 `TPartBinOps/TPartArgBinOps`。
- **命名清理**：少量文件大小写归一（`TFmod→TFMod`、`TDequant→TDeQuant`、`TCI→Tci`、`TMaxS→TMaxs`）——迁移脚本若按文件名硬匹配会踩坑。
- 两个目录都有 `custom/` 子目录（放置自定义高性能变体，如 a5 的 `TSqrtHp`、`TLog_Custom`）。

#### 4.1.2 核心流程

一个 kernel 源文件从 `#include <pto/pto-inst.hpp>` 开始，到落到具体架构目录的路径是：

```text
pto-inst.hpp
  └──（按 __CPU_SIM / __COSTMODEL / __CCE_AICORE__ 三选一，见 u2-l4）
      pto_instr_impl.hpp
        ├── #ifdef PTO_NPU_ARCH_A2A3  → #include "pto/npu/a2a3/TAdd.hpp" ...（逐条列全）
        ├── #ifdef PTO_NPU_ARCH_A5    → #include "pto/npu/a5/XXX.hpp" ...（逐条列全）
        ├── #ifdef PTO_NPU_ARCH_A6    → #include "pto/npu/a6/header.hpp"（一个组合头）
        └── #ifdef PTO_NPU_ARCH_KIRIN* → 各 kirin header.hpp
```

架构宏的源头在 `arch_macro.hpp`：CCE 编译器用 `--cce-aicore-arch` 传入的架构号（`__NPU_ARCH__`）被翻译成语义宏。注意 A6 与 Kirin 系列一样带 `PTO_COMM_NOT_SUPPORTED`——**A6 当前不支持通信指令集**，这一点后面还会在能力位里再次出现。

#### 4.1.3 源码精读

**架构号 → 语义宏的翻译表**。[include/pto/common/arch_macro.hpp:L19-L38](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/arch_macro.hpp#L19-L38) 这段把 `2201` 映射为 A2A3、`3101/3510` 映射为 A5（其中 3510 额外定义 `PTO_URMA_SUPPORTED`，对应 u7-l4 讲过的 URMA DMA 引擎）、`9201` 映射为 A6 并标记通信不可用：

```cpp
#if __NPU_ARCH__ == 2201
#define PTO_NPU_ARCH_A2A3
#elif (__NPU_ARCH__ == 3101) || (__NPU_ARCH__ == 3510)
#define PTO_NPU_ARCH_A5
#if __NPU_ARCH__ == 3510
#define PTO_URMA_SUPPORTED
#endif
...
#elif __NPU_ARCH__ == 9201
#define PTO_COMM_NOT_SUPPORTED
#define PTO_NPU_ARCH_A6
#endif
```

这就是「同代号内还能再分特性」的做法：A5 的两个芯片型号共用 `PTO_NPU_ARCH_A5` 指令实现，仅以 `PTO_URMA_SUPPORTED` 区分通信引擎能力。

**汇总头的按架构互斥挂载**。[include/pto/common/pto_instr_impl.hpp:L197-L203](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr_impl.hpp#L197-L203) 是 A5 分支的开头——非 CostModel 时逐条 include a5 目录的实现头（一直列到第 327 行附近）；而 [include/pto/common/pto_instr_impl.hpp:L331-L333](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr_impl.hpp#L331-L333) 的 A6 分支只有一行 include：

```cpp
#ifdef PTO_NPU_ARCH_A6
#include "pto/npu/a6/header.hpp"
#endif
```

两种组织风格并存：a2a3/a5 用「平铺清单」（新指令要来这里登记，u11-l1 的清单纪律），a6 用「组合头转发」（登记点收敛到一个文件）。

**A6 组合头：专用与复用的边界**。[include/pto/npu/a6/header.hpp:L20-L32](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a6/header.hpp#L20-L32) 用注释明说了边界，随后混排两类 include：

```cpp
// A6 uses dedicated TLoad/TExtract/TMatmul implementations,
// while some other instructions still reuse A5.
#include "pto/npu/a5/TAssign.hpp"
#include "pto/npu/a6/SyncAll.hpp"
#include "pto/npu/a5/TAdd.hpp"
#include "pto/npu/a6/TLoad.hpp"
#include "pto/npu/a5/TStore.hpp"
#include "pto/npu/a6/TExtract.hpp"
#include "pto/npu/a6/TMatmul.hpp"
...
```

读这段要建立一个认知：**「A6 支持」这个说法的实现粒度是 per-instruction 的**。`include/README.md` 的逐指令状态表（[include/README.md:L24-L34](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/README.md#L24-L34)）列出了 CPU/Costmodel/A2/A3/A5/Kirin 六列——注意该表目前还没有 A6 列，A6 的实际支持范围要以 `a6/header.hpp` 的 include 清单为准（经 A5 间接复用的部分同样可用）。这也提醒我们：迁移到新架构时，状态表可能滞后，源码挂载点才是事实。

#### 4.1.4 代码实践

**实践目标**：亲手验证「a6 = 少量专用 + 大量复用 A5」这一组织方式，并产出一张自己的架构路由图。

**操作步骤**：

1. 在仓库根目录执行 `ls include/pto/npu/a6/`，列出全部 11 个条目。
2. 执行 `grep -c "a6/" include/pto/npu/a6/header.hpp` 与 `grep -c "a5/" include/pto/npu/a6/header.hpp`，统计专用与复用的 include 条数。
3. 打开 [include/pto/npu/a6/header.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a6/header.hpp)，把每个 include 按「a6 专用 / a5 复用」两栏抄进表格。
4. 用 `ls include/pto/npu/a2a3/ | wc -l` 与 `ls include/pto/npu/a5/ | wc -l` 对比两个全量目录的规模。

**需要观察的现象**：a6 目录里只有 6 条指令头（TLoad/TExtract/TMatmul/TReshape/TQuant/SyncAll）加公共头（common/datatype/utils/TSync），而 header.hpp 中引用 `a5/` 路径的行数与引用 `a6/` 的行数为同一量级。

**预期结果**：你应当得到「6 条专用指令 + 5 个公共头 + 若干 A5 复用」的结构结论，并能画出 `pto-inst.hpp → pto_instr_impl.hpp → a6/header.hpp → {a6/*, a5/*}` 的路由图。本实践为纯源码阅读，无需硬件，可直接完成。

#### 4.1.5 小练习与答案

**练习 1**：为什么 A2 与 A3 能共用 `a2a3/` 一个目录，而 A5、A6 各有目录？

**答案**：A2（910B）与 A3（910C）的 Cube/Vector/MTE 指令契约与片上组织对 PTO 层面完全一致，实现可共享（arch_macro.hpp 中单一 `2201` 即映射 `PTO_NPU_ARCH_A2A3`）；而 A5 相对 A2/A3 出现了实质差异——新的 intrinsic 签名（如 `mad` 去掉 `kDirectionAlign` 参数）、MX/FP8/Int64/SIMT 等新能力、L0C 容量翻倍——必须另立目录；A6 又在 A5 基础上改变了部分 intrinsic（混合精度 `mad_*` 变体），因此为这些「行为不同」的指令单开目录，其余复用 A5。

**练习 2**：如果给 A6 新增一条 a2a3/a5 都有的指令实现，应该改哪些文件？

**答案**：在 `include/pto/npu/a6/` 新建该指令头文件（实现 `*_IMPL`，签名与公共 API 薄壳约定一致），然后在 `include/pto/npu/a6/header.hpp` 增加 include；由于 `pto_instr_impl.hpp` 的 A6 分支只 include 这个组合头，不需要动 `pto_instr_impl.hpp`。这与 a2a3/a5 分支「在 pto_instr_impl.hpp 平铺清单登记」的做法不同——登记位置随目录组织方式而变。

**练习 3**：`PTO_URMA_SUPPORTED` 为什么定义在 A5 分支内部而不是 A5 外面？

**答案**：因为它表达的是「A5 代内某个具体型号（`__NPU_ARCH__ == 3510`）才有的特性」，作用域应小于 `PTO_NPU_ARCH_A5`。嵌套在分支内可以保证：只有 A5 命中时才进一步检查型号，其他架构永远不会误定义该宏。

### 4.2 A5 新编程形态：SIMT、Int64 扩展与 MX 指令

#### 4.2.1 概念说明

A2/A3 的 PTO 是纯 SIMD（tile 级向量）模型：数据必须先经 MTE2 搬进 UB，Vector 流水线以 repeat 为单位整块处理。A5 在此之上引入了三种新形态，它们都体现在 a5 目录的文件构成里：

1. **SIMT（warp 级线程模型）**：kernel 可以写成「每个线程管若干列」的形态，数据从 GM 经 D-cache 直达寄存器，绕过 MTE2 与 UB。PTO 把它作为与 tile 指令并列的编程形态暴露（部分指令如 MGather 内部就用 SIMT kernel 实现），`kernels/manual/a5/engram_simt/` 是官方探索算子。
2. **Int64 扩展**：向量硬件没有原生 64 位整数 lane，A5 用「高低两个 32 位 lane + 进位 intrinsic」在指令层模拟出 `int64_t/uint64_t` 的 tile 运算，对用户表现为 TADD 等指令直接支持 64 位 dtype。
3. **MX 指令**：u5-l5 讲过的 `mad_mx` 点积与缩放因子地址编码（`TGetScaleAddr`），A5 首发并在 A6 继续扩展组合。

三种形态的共同点：**都是「硬件有新能力 → PTO 用既有 tile 抽象包一层」**，用户侧 API 风格不变，这是「抽象统一」在指令供给端的体现。

#### 4.2.2 核心流程

以 engram_simt 为例，SIMT kernel 的执行模型与 SIMD 完全不同：

```text
SIMD（A2/A3 基线）:
  GM --MTE2 DMA--> UB tile --Vector repeat--> 结果 --MTE3--> GM
  每个位置串行，8 个 head 的 gather 是 8 次串行 DMA

SIMT（A5 融合 kernel）:
  GM --D-cache--> 线程寄存器（LDG 直读）
  warp 内：__builtin_cce_redux_add_f32 做 32 lane 硬件归约
  warp 间：UB scratch + __sync_workitems() 交换部分和
  MTE2 busy cycles 从 106,261（基线）降到 5（融合后）
```

线程映射采用「列所有权」：`tx = lane_id ∈ [0,32)`、`ty = warp_id`，全局列号 `col = ty × 32 + tx`，一个线程终身拥有若干列。加速的边界由 D-cache 工作集决定：每个位置（position）触碰的 cacheline 数为

\[ \text{CL/position} = (H + 2) \times \frac{D}{32} \]

其中 H=8 个 head、D 为嵌入维度，每行 D 个 float、每 cacheline 128 字节（32 个 float）。A5 每核 D-cache 只有 1024 行：D=128 时每位置 40 行（占比 3.9%），跨位置还有复用，实测加速 4.50×；D=512 时每位置 160 行（15.6%），64 个位置的工作集远超容量、命中率趋近于零，加速塌缩到 1.13×——**SIMT 不是免费午餐，它的性能是 D-cache 局部性的函数，而 SIMD 基线对访问模式不敏感**。

#### 4.2.3 源码精读

**SIMT kernel 的样子**。[kernels/manual/a5/engram_simt/engram-simt_kernel.cpp:L155-L172](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/engram_simt/engram-simt_kernel.cpp#L155-L172) 是主 kernel 的签名与前几行，浓缩了 SIMT 的全部语法要素：

```cpp
__simt_vf__ AICORE LAUNCH_BOUND(1024) PTO_INLINE void simt_engram_v2(
    __gm__ float* __restrict__ gmOutput, __gm__ const float* __restrict__ gmTable, ...)
{
    ...
    const uint32_t tx = __cce_simt_get_TID_X();
    const uint32_t ty = __cce_simt_get_TID_Y();
```

`__simt_vf__` 标记 SIMT 向量函数；`LAUNCH_BOUND(1024)` 声明每线程寄存器预算（1024 线程档=32 GPR/线程，512 档=64 GPR/线程）；`__cce_simt_get_TID_X/Y` 取线程坐标。kernel 内直接 `gmTable[...]` 读 GM——这就是「Register-Forwarding Direct-GM」数据流。

**warp 内归约与 warp 间同步**。[kernels/manual/a5/engram_simt/engram-simt_kernel.cpp:L185-L203](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/engram_simt/engram-simt_kernel.cpp#L185-L203) 展示两件套：`__builtin_cce_redux_add_f32` 单条指令完成 warp 内 32 lane 求和；跨 warp 的部分和写进 UB scratch 数组后用 `__sync_workitems()` 做一次栅栏：

```cpp
float warp_dot = __builtin_cce_redux_add_f32(h_val * g_val);
scrBuf[ty] = warp_dot;
__sync_workitems();
```

注意 SIMT kernel 里 UB 的角色变了：从「数据必经之路」降级为「warp 间交换少量标量的 scratch」。这解释了 README 里 MTE2 busy cycles 接近零的现象。

**流式累加 vs 独立加载的取舍**。[kernels/manual/a5/engram_simt/engram-simt_kernel.cpp:L208-L211](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/engram_simt/engram-simt_kernel.cpp#L208-L211)（32 GPR 档）用一个累加器流式吸收 8 行 embedding，省寄存器；而 [kernels/manual/a5/engram_simt/engram-simt_kernel.cpp:L347-L354](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/engram_simt/engram-simt_kernel.cpp#L347-L354)（LB(512)、64 GPR 档）改成 t0..t7 八个独立临时量再树形求和——寄存器预算换内存级并行，同一文件里两种写法对照，是学习「资源-并行度」权衡的绝佳标本。

**指令实现内部的 SIMT kernel**。SIMT 不只是算子作者的可选项，PTO 自己也用它实现指令：[include/pto/npu/a5/MGather.hpp:L77](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a5/MGather.hpp#L77) 定义 `simt_mgather_row_kernel`（`AICORE __simt_vf__ LAUNCH_BOUND(1024)`），再由 [include/pto/npu/a5/MGather.hpp:L170](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a5/MGather.hpp#L170) 的 `cce::async_invoke<simt_mgather_row_kernel<...>>` 发射。也就是说 `MGATHER(Row 模式)` 在 A5 上的底层是一段 warp 级查表代码——用户看到的还是一条 tile 指令。

**Int64 软件模拟**。[include/pto/npu/a5/Int64Common.hpp:L17-L26](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a5/Int64Common.hpp#L17-L26) 定义了操作枚举与 64 位加法的实现——先 `vaddc` 算低 32 位并产出进位 mask，再 `vaddcs` 把进位喂给高 32 位：

```cpp
enum class Int64Op { Add, Sub, Mul, Shl, Shr, Max, Min };

#if defined(PTO_NPU_ARCH_A5) || defined(PTO_NPU_ARCH_A6)
PTO_INTERNAL void Int64AddRegs(...)
{
    MaskReg carry, carryOut;
    vaddc(carry, dstLow, lhsLow, rhsLow, mask);
    vaddcs(carryOut, dstHigh, lhsHigh, rhsHigh, carry, mask);
}
```

整段被 `PTO_NPU_ARCH_A5 || A6` 门控——Int64 能力是 A5 起才有的。上层效果在 [include/pto/npu/a5/TAdd.hpp:L41-L43](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a5/TAdd.hpp#L41-L43)：TADD 遇到 `int64_t/uint64_t` tile 时 `if constexpr` 分派到 `Int64Binary<Int64Op::Add, ...>`，其余类型走常规 `BinaryInstr`。用户视角下「TADD 支持 int64」是透明的。

**MX 缩放地址编码**。[include/pto/npu/a5/TGetScaleAddr.hpp:L22-L29](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a5/TGetScaleAddr.hpp#L22-L29) 全文只有一个转发：把数据 tile 的地址编码成 scale tile 地址（`__cce_pto_get_mx_scale_tile`）。这就是 u5-l5 讲过的「scale 走地址绑定而非指令参数通路」在源码上的落点，A5 目录才有此文件。

#### 4.2.4 代码实践

**实践目标**：不写代码，通过阅读 engram_simt 的文档与源码，回答「什么 workload 该从 SIMD 迁到 SIMT」。

**操作步骤**：

1. 阅读 [kernels/manual/a5/engram_simt/README.md:L18-L25](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/engram_simt/README.md#L18-L25) 的「When to Use SIMT Fusion vs Baseline SIMD」表。
2. 阅读 [kernels/manual/a5/engram_simt/README.md:L112-L129](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/engram_simt/README.md#L112-L129) 的 A5 SIMT 资源表（UB 256KB、D-cache 128KB=1024 行×128B、4 个 warp scheduler、warp 宽 32、冷缺失约 550 cycle）。
3. 对照 [kernels/manual/a5/engram_simt/engram-simt_kernel.cpp:L155-L215](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/engram_simt/engram-simt_kernel.cpp#L155-L215)，找出三处 SIMT 专有构件（TID 获取、warp 归约、workitem 同步）所在的行号。
4. （可选，需 CANN 仿真环境）按 [kernels/manual/a5/engram_simt/README.md:L899-L915](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/engram_simt/README.md#L899-L915) 执行 `bash run.sh -r sim -v Ascend910_9599` 跑默认 8 个测试。

**需要观察的现象**：步骤 1-3 为纯阅读，应观察到「B=1 时 SIMT 反而慢（0.8×，启动开销约 1100 cycle 无法摊销）、B≥4 且 D≤256 时显著加速（1.4-4.5×）、D=512 时收益趋近 1（工作集 12,288 CL 远超 1024 行 D-cache）」的结论链；步骤 4 的运行结果**待本地验证**（本讲写作环境无 CANN 仿真器，未实际执行）。

**预期结果**：你能用自己的话写出判据——SIMT 适合「每字节 FLOP 极低（访存受限）、批量足够大（摊销启动开销）、工作集能装进或部分复用 D-cache」的 gather/查表类 workload；否则 SIMD tile 流水仍是默认选择。

#### 4.2.5 小练习与答案

**练习 1**：engram 的 SIMD 基线里，8 个 head 的 gather 为什么成为瓶颈？SIMT 如何消除它？

**答案**：基线用 TLOAD 逐个 head 搬运（README 第 5 节伪代码中 8 次串行 DMA），每次都要 GM→UB 走 MTE2 并加 pipe_barrier，MTE2 busy 成为 kernel 主成本。SIMT 让每线程用 LDG 直读 GM（经 D-cache 进寄存器），8 行累加变成寄存器内的 FADD 链（或 8 个独立 LDG），MTE2 busy 从 106,261 cycles 降到 5 cycles（硬件 setup）。

**练习 2**：`__sync_workitems()` 与 u2-l3 学的 `set_flag/wait_flag` 事件有什么区别？

**答案**：事件同步的是**流水线间**的依赖（MTE2→V→MTE3），由硬件队列按 flag 三元组配对，tile 编程必用；`__sync_workitems()` 是 **SIMT warp 间**的栅栏，语义 closer 于 u6-l1 的块内同步，只在 SIMT kernel 里出现，且设计目标是少用（engram 通过 ColChunks 重映射把大部分配置优化到零 barrier）。两者分属两套执行模型，不混用。

**练习 3**：Int64 的 `vaddc/vaddcs` 为什么必须成对出现？只调 `vaddc` 会怎样？

**答案**：`vaddc` 只算低 32 位并把进位输出为 mask；高 32 位必须用 `vaddcs`（带进位输入的加法）消费这个 mask，否则 64 位结果的高位丢失进位信息，跨 2^32 边界的加法会静默出错。这是用 32 位 lane 模拟 64 位运算的标准进位链手法。

### 4.3 跨代迁移标本：TMatmul 的三代实现对比

#### 4.3.1 概念说明

「跨代迁移」就是把一份在 a2a3 上跑通的 kernel 源码改到 a5/a6 上编译运行。PTO 的承诺是：**公共 API（`TMATMUL` 薄壳、tile 类型、事件系统）不变，kernel 主体逻辑不用改**；但实现头内部的 intrinsic 契约、dtype 白名单、检查逻辑、容量约束每代都可能在变。迁移工作 = 确认这些变化点不触碰你的用法。

TMatmul 是最好的标本，因为它三代都有专用实现，且差异覆盖了迁移会遇到的所有变化类型：

| 变化类型 | a2a3 | a5 | a6 |
|---|---|---|---|
| `mad` intrinsic 签名 | 带 `kDirectionAlign` 运行时参数 | 去掉该参数，`gemvCtrl` 提升为编译期模板参数 | 同 a5 签名，另增 4 个混合精度变体 |
| m==1 规避 | 运行时 `m=16` hack | 无（编译期 `gemvCtrl` 显式选择） | 无 |
| dtype 白名单 | 4 组（s8s8/f16f16/f32f32/bf16bf16） | 增加 FP8 四组合与 hifloat8 | 再增 F16×S8、F16×S4、BF16×E4M3、S8×S4 等混合精度 |
| MX 指令 | 无 | `mad_mx` + FP4/FP8 组合 | 继承并扩展 FP8×FP4、HiF4 组合 |
| 布局检查 | 只查 TileType 位置 | 增加分形（SFractal/RowMajor）static_assert | 同 a5 并加 L0C 容量检查 |

#### 4.3.2 核心流程

迁移一条指令用法的判定流程（以 TMatmul 为例）：

```text
1. 找到三代同名实现头（npu/a2a3|a5|a6/TMatmul.hpp）
2. 逐项对比：
   a. 底层 intrinsic 与模板参数列表（调用约定是否变）
   b. Check*Valid 的 static_assert / PTO_ASSERT（约束是否收紧或放宽）
   c. dtype 组合表（你的数据类型是否仍被支持）
   d. 涉及的片上容量常量（buffer_limits.hpp 按架构分档）
3. 公共 API 层（XXX → XXX_IMPL 宏转发）三代一致 → kernel 调用点无需改动
4. 在目标架构重新编译，static_assert / PTO_ASSERT 会把遗漏项拦在编译期或启动期
5. 用目标架构的 ST 用例验证数值正确性（u10-l1 的测试体系）
```

关键认识：PTO 把「代际差异」尽量压成**编译期错误**（static_assert）而不是运行期性能退化，所以迁移的第一反馈通道就是编译器报错信息——报错文本里往往直接写着架构名（如 `"For A6 int32 accumulation, supported input pairs are..."`）。

#### 4.3.3 源码精读

**a2a3：运行时参数与 m==1 规避**。[include/pto/npu/a2a3/TMatmul.hpp:L37-L53](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TMatmul.hpp#L37-L53) 的设备端模板带一个运行时 `bool kDirectionAlign` 参数（仅 f32×f32 由 `GetKDirectionAlign` 计算传入），并在 m==1 时强制改成 16：

```cpp
__tf__ AICORE void TMatmul(... uint16_t m, uint16_t k, uint16_t n, bool kDirectionAlign)
{
    ...
    if constexpr (!isGemv) {
        if (m == 1) {
            m = 16; // avoid gemv mode, if m is 1, the gemv mode will be used in a3
        }
    }
    mad(c, a, b, m, k, n, static_cast<uint8_t>(Phase), kDirectionAlign, cmatrixSource, cmatrixInitVal);
}
```

这个 hack 是典型的「架构怪癖封装」：A3 的 `mad` 见到 m==1 会自动切换 GEMV 模式导致性能陷阱，PTO 在 A2/A3 实现里替用户垫了一层。dtype 白名单在 [include/pto/npu/a2a3/TMatmul.hpp:L83-L96](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TMatmul.hpp#L83-L96)（CheckStaticMad）：仅 s8×s8→s32、f16×f16→f32、f32×f32→f32、bf16×bf16→f32 四组，且只检查 TileType 位置。

**a5：gemvCtrl 编译期化 + MX 全家桶**。[include/pto/npu/a5/TMatmul.hpp:L23-L42](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a5/TMatmul.hpp#L23-L42) 中 `GetGemvCtrl`（`Rows != 1`）的结果作为编译期模板参数 `gemvCtrl` 传入，`mad` 不再收 `kDirectionAlign`：

```cpp
template <typename TileLeft>
PTO_INTERNAL constexpr bool GetGemvCtrl() { return TileLeft::Rows != 1; }
...
    mad(c, a, b, m, k, n, static_cast<uint8_t>(Phase), gemvCtrl, cmatrixSource, cmatrixInitVal);
```

m==1 的运行时 hack 随之消失——「是否 GEMV」从「运行时看 m 值规避」变成「编译期由指令种类显式声明」（`TMATMUL_IMPL` 传 `true` 禁用 GEMV 模式，`TGEMV_IMPL` 传 `false` 启用，见 [include/pto/npu/a5/TMatmul.hpp:L170-L183](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a5/TMatmul.hpp#L170-L183)）。同时 a5 新增 `TMatmulMx/TMatmulMxBias`（底层 `mad_mx`，[include/pto/npu/a5/TMatmul.hpp:L60-L73](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a5/TMatmul.hpp#L60-L73)），float 累加的白名单扩展出 FP8 e4m3/e5m2 四组合与 hifloat8（[include/pto/npu/a5/TMatmul.hpp:L150-L160](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a5/TMatmul.hpp#L150-L160)）；检查也从「只查位置」收紧为「位置 + 分形布局 + L0C 容量」（[include/pto/npu/a5/TMatmul.hpp:L103-L127](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a5/TMatmul.hpp#L103-L127) 的 `CheckMadMxValid`，其中 `accBytes <= PTO_L0C_SIZE_BYTES` 正是引用按架构分档的容量常量）。

**a6：混合精度分派宏**。[include/pto/npu/a6/TMatmul.hpp:L69-L102](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a6/TMatmul.hpp#L69-L102) 用宏 `PTO_A6_DISPATCH_MAD` 在编译期按 dtype trait 分派到四个 intrinsic 之一：

```cpp
#define PTO_A6_DISPATCH_MAD(C_PTR, A_PTR, B_PTR, M_VAL, K_VAL, N_VAL)              \
    do {                                                                            \
        if constexpr (kIsMmadS8S4<TileRes, TileLeft, TileRight>) {                  \
            mad_s8s4(...);                                                          \
        } else if constexpr (kIsMmadBf16S4<...>) { mad_bf16s4(...); }               \
        else if constexpr (kIsMmadF16S4<...>)  { mad_f16s4(...); }                  \
        else { mad(...); }                                                          \
    } while (false)
```

trait 定义在 [include/pto/npu/a6/TMatmul.hpp:L19-L61](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a6/TMatmul.hpp#L19-L61)：`kIsMmadF16S8/F16S4/F16E4M3/Bf16E4M3/Bf16S8/Bf16S4/S8S4` 等，对应「宽操作数 × 窄操作数」的混合精度矩阵乘。A6 还把 MX 的组合表扩到 FP8×FP4、FP16×FP4、BF16×FP4（[include/pto/npu/a6/TMatmul.hpp:L153-L173](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a6/TMatmul.hpp#L153-L173)），HiF4 组合则用 `#if defined(PTO_NPU_ARCH_A6)` 门控（[include/pto/npu/a6/TMatmul.hpp:L175-L201](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a6/TMatmul.hpp#L175-L201)）——同一份 a6 头文件被非 A6 架构包含时这些组合自动退化为 false，防止误用。int32 累加路径 A6 也比 A5 宽：[include/pto/npu/a6/TMatmul.hpp:L257-L267](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a6/TMatmul.hpp#L257-L267) 在 `PTO_NPU_ARCH_A6` 下允许 int8×int4b_t（`MMAD.s8s4`），非 A6 仍只许 int8×int8。

**三代不变的部分**：`TMATMUL_IMPL` 的外壳结构（Check → 取 validRow/validCol → CheckDynamicMmad → 调设备端模板）逐代一致；bias 指针打包进 C 指针高 32 位的技巧三代共用（a2a3 [L67-L68](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TMatmul.hpp#L67-L68)、a5 [L54-L55](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a5/TMatmul.hpp#L54-L55)、a6 [L114-L115](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a6/TMatmul.hpp#L114-L115)）；m/k/n ∈ [1,4095] 的动态范围三代相同。迁移时应先找「不变量」建立信心，再逐项核对「变化量」。

#### 4.3.4 代码实践

**实践目标**：亲手完成 TMatmul 三代实现的差异表（综合实践清单的预演）。

**操作步骤**：

1. 并排打开三代 `TMatmul.hpp`，各自定位：设备端模板签名、`mad*` 调用行、`Check*Valid`、`TMATMUL_IMPL`。
2. 对每个维度填写「a2a3 / a5 / a6」三列：intrinsic 参数、GEMV 控制方式、int32 白名单、float 白名单、MX 支持、分形检查、L0C 容量检查。
3. 用 `grep -n "kDirectionAlign" include/pto/npu/a5/TMatmul.hpp include/pto/npu/a6/TMatmul.hpp` 验证该参数在新代已消失。
4. 用 `grep -n "PTO_L0C_SIZE_BYTES" include/pto/npu/a2a3/TMatmul.hpp include/pto/npu/a5/TMatmul.hpp include/pto/npu/a6/TMatmul.hpp` 观察容量检查只在 a5/a6 出现。

**需要观察的现象**：步骤 3 的两条 grep 应无输出（或仅 a2a3 有输出）；步骤 4 应只在 a5/a6 命中。差异表应能覆盖 4.3.1 表格中的全部行。

**预期结果**：产出一张 7 行 × 3 列的差异对照表。本实践为纯源码阅读，无需硬件。若某项 grep 结果与预期不符，以源码为准并更新你的表格——说明代际差异又在演进。

#### 4.3.5 小练习与答案

**练习 1**：a2a3 的 `kDirectionAlign` 参数为什么在 a5 被删掉而不是保留成空操作？

**答案**：该参数是为 A2/A3 上 f32×f32 的 K 对齐场景服务的 `mad` intrinsic 入参；A5 的 `mad` intrinsic 契约本身变了（不再接收该参数，硬件内部处理对齐），实现层保留一个无人消费的参数只会误导迁移者以为它仍有效。PTO 的原则是「实现头贴紧当代 intrinsic」，跨代一致性由公共 API 层保证，而不是靠实现层伪装。

**练习 2**：把 a2a3 上的 `TMATMUL(s8, s8 → s32)` kernel 原样搬到 A6 编译，可能遇到什么新情况？

**答案**：编译仍会通过（int8×int8→int32 在 A6 依旧合法），但 A6 的 `CheckMadValid` 在 `PTO_NPU_ARCH_A6` 下额外允许 int8×int4b_t→int32；反过来，若在 A2/A3 上用 int4 操作数则会被 static_assert 拒绝。即「向下迁移」（新代→旧代）比「向上迁移」更容易踩白名单，迁移清单里要双向核对 dtype 组合。

**练习 3**：三代 `TMATMUL_IMPL` 都以 `aMatrix.GetValidRow()/GetValidCol()` 取 M/K，这说明什么设计原则？

**答案**：M/K/N 永远来自 tile 的有效区（u5-l1 的结论），这一契约跨代不变。实现头内部可以自由更换 intrinsic、增删检查，但「公共 API 的语义（有效区语义、事件返回、tile 类型约束）」是跨代稳定的接口面——这正是「kernel 源码一行不改即可跨代编译」的根基。

### 4.4 架构取舍：性能 vs 抽象统一

#### 4.4.1 概念说明

跨代支持的根本矛盾：**抽象统一要求所有架构长一个样，性能要求每个架构贴紧自家硬件**。PTO 的解法是分层取舍，把「统一」压在薄的一层，把「自由」留给厚的一层：

| 层 | 统一程度 | 内容 |
|---|---|---|
| 公共 API 薄壳（common/） | 完全统一 | `XXX` → `XXX_IMPL` 宏转发、tile 类型、事件系统、Op 枚举 |
| 能力声明（arch_capability.hpp） | 统一接口、按架构取值 | `SupportsFp8`、`SupportsComm` 等能力位 |
| 容量常量（buffer_limits.hpp） | 统一接口、按架构取值 | L0A/L0B/L0C 尺寸 |
| 实现头（npu/a2a3、a5、a6） | 完全自由 | intrinsic 选择、repeat 组织、检查逻辑、软件模拟 |

用户侧感受到的取舍结果是：**功能正确性尽量跨代一致（宁可软件模拟也不砍 API），极致性能必须按架构重新调优（tile 形状、指令排序、容量预算每代不同）**。A6 的「增量复用 A5」与 Int64 的「软件模拟」是「抽象统一优先」的两个极端案例；a2a3 保留 m==1 hack、a5 删除它，则是「性能优先、每代贴紧硬件」的案例。

#### 4.4.2 核心流程

能力位与容量常量如何参与编译期决策：

```text
kernel 用到 FP8 matmul
  → 编译期 ArchTraits<ChipArch::A2A3>::SupportsFp8 == false → static_assert 拦截
  → ArchTraits<ChipArch::A5/A6>::SupportsFp8 == true  → 放行到 a5/a6 实现

kernel 选择 tile 形状（如 Acc 256×256 fp32 = 256KB）
  → PTO_L0C_SIZE_BYTES：A2A3 = 128KiB → CheckMadMxValid 装不下 → 编译失败
  →                    A5/A6 = 256KiB → 通过
  → 同一份 kernel 在两代硬件上可选的 tile 形状空间不同
```

也就是说，架构差异被编码为两类编译期数据（能力位、容量常量），配合实现头里的 static_assert，把「这台硬件做不了」从运行期崩溃提前为编译期报错。

#### 4.4.3 源码精读

**能力位的继承结构**。[include/pto/common/arch_capability.hpp:L61-L70](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/arch_capability.hpp#L61-L70) 定义 `ArchTraitsFp4Capable` 基类（Bf16/Fp8/Fp4/SyncAll/TQuant/THistogram/MxLayout 全开），[L83-L106](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/arch_capability.hpp#L83-L106) 三个特化各取所需：

```cpp
template <> struct ArchTraits<ChipArch::A2A3> : ArchTraitsBase<ChipArch::A2A3> {
    static constexpr bool SupportsBf16 = true;
    static constexpr bool SupportsSyncAll = true;
    static constexpr bool SupportsComm = true;          // 无 Fp8/Fp4/MxLayout
};
template <> struct ArchTraits<ChipArch::A5> : ArchTraitsFp4Capable<ChipArch::A5> {
    static constexpr bool SupportsComm = true;          // 全能力 + 通信
};
template <> struct ArchTraits<ChipArch::A6> : ArchTraitsFp4Capable<ChipArch::A6> {
    // SupportsComm currently false — A6 comm support is a separate workstream.
};
```

A6 注释明说通信支持是独立工作流——与 arch_macro.hpp 的 `PTO_COMM_NOT_SUPPORTED`、u7-l1 的「Kirin 与 A6 架构通信整体不可用」三处互相印证。**迁移到 A6 前先查能力位**，这是清单的第一项。

**容量常量的按架构分档**。[include/pto/common/buffer_limits.hpp:L111-L121](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/buffer_limits.hpp#L111-L121) 定义 L0C 容量：A5/A6 为 256 KiB，A2A3（与 KirinX90）为 128 KiB，Kirin9030 仅 64 KiB：

```cpp
#if defined(PTO_NPU_ARCH_A5)
#define PTO_L0C_SIZE_BYTES (256u * 1024u)
#elif defined(PTO_NPU_ARCH_A6)
#define PTO_L0C_SIZE_BYTES (256u * 1024u)
#elif defined(PTO_NPU_ARCH_KIRINX90) || defined(PTO_NPU_ARCH_A2A3)
#define PTO_L0C_SIZE_BYTES (128u * 1024u)
```

对 GEMM 类 kernel 的直接含义：Acc tile 的形状上限在 A5/A6 上翻倍（u5-l2 讲过 L0 乒乓半区约束 tile 形状），**从 A2A3 迁往 A5 时可以（也应该）重新扫描更大的 base block**；反向迁移则可能编译失败。注意宏用 `#ifndef` 包裹（允许外部覆盖），且未识别架构直接 `#error`——未适配的新架构会在第一时间暴露，而不是静默用错容量。

**抽象升级的代价与收益：TAdd 两代对比**。[include/pto/npu/a2a3/TAdd.hpp:L19-L30](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TAdd.hpp#L19-L30) 的策略类直接操作裸指针与 repeat 步长：

```cpp
struct AddOp {
    static void BinInstr(__ubuf__ T* dst, __ubuf__ T* src0, __ubuf__ T* src1, uint8_t repeats) {
        vadd(dst, src0, src1, repeats, 1, 1, 1, 8, 8, 8);
    }
    static void BinInstr(..., uint8_t dstRepeatStride, uint8_t src0RepeatStride, uint8_t src1RepeatStride) { ... }
};
```

而 [include/pto/npu/a5/TAdd.hpp:L23-L30](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a5/TAdd.hpp#L23-L30) 换成寄存器对象与谓词寄存器：

```cpp
struct AddOp {
    static void BinInstr(RegTensor<T>& reg_dst, RegTensor<T>& reg_src0, RegTensor<T>& reg_src1, MaskReg& preg) {
        vadd(reg_dst, reg_src0, reg_src1, preg, MODE_ZEROING);
    }
};
```

`RegTensor` 是 [include/pto/npu/a5/common.hpp:L63-L70](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a5/common.hpp#L63-L70) 定义的薄包装（内含一个 `RegType reg` 成员并提供隐式转换）；`MaskReg` 即 `vector_bool`（[L38](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a5/common.hpp#L38)）。A5 的 TAdd 还多了一个运行时实现选择参数 `VFImplKind version`（[include/pto/common/type.hpp:L241-L247](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/type.hpp#L241-L247) 定义了 `VFIMPL_1D/2D × POST_UPDATE` 五档）。这是「抽象统一」的代价样本：a5 的指令实现层引入了更厚的寄存器级抽象（换来 Int64 透明支持、谓词掩码、多实现切换），a2a3 的实现层则更贴硬件（换来对 repeat 组织的完全控制）。**两种风格并存于同一个库，用户 API 无感知——这正是取舍发生在实现层的证明。**

#### 4.4.4 代码实践

**实践目标**：体验「同一份 kernel 源码，不同架构不同的编译期命运」。

**操作步骤**：

1. 读 [include/pto/common/arch_capability.hpp:L83-L106](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/arch_capability.hpp#L83-L106)，抄下 A2A3 与 A5/A6 各自 `SupportsFp8`、`SupportsComm` 的值。
2. 读 [include/pto/common/buffer_limits.hpp:L111-L121](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/buffer_limits.hpp#L111-L121)，计算 A2A3 与 A5 上能容纳的最大 fp32 Acc tile（行×列）。
3. 思维实验（纸面推导，不编译）：假设你有一个 A2A3 kernel，Acc tile 为 128×128 fp32、用 `TMATMUL_MX`（MXFP8 输入）、并用 `TGET` 做跨卡通信——逐项判断它在 A5、A6 上的命运（通过 / 编译失败 / 功能缺失）。
4. （可选）在仓库中 `grep -rn "SupportsFp8" include/ docs/ | head`，观察能力位在何处被消费。

**需要观察的现象**：步骤 2 中 128 KiB 与 256 KiB 对应的最大 fp32 Acc 分别约为 128×256 与 128×512（或 256×256）量级；步骤 3 的结论应区分「容量翻倍反而放开 tile 形状」「MX 在 A2A3 根本不存在该指令」「通信在 A6 被能力位与宏双重拦截」三种不同命运。

**预期结果**：写出三行判定：A2A3 kernel → A5：三项全部通过且 tile 形状空间变大（还可重调优）；→ A6：MX 与容量通过，通信被拦（`SupportsComm=false`、`PTO_COMM_NOT_SUPPORTED`）。步骤 4 的消费点数量与位置**待本地验证**（以实际 grep 结果为准）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 PTO 不把 a2a3/a5/a6 三套实现合并成一套「万能实现」，用 if constexpr 分支处理差异？

**答案**：差异不只是参数级，而是 intrinsic 契约级（`mad` 签名不同、`RegTensor` vs 裸指针、repeat 组织不同）。强行合并会让每个实现头塞满架构分支，静态检查（如 A6 独有的 int4 白名单）也要跟着分支化，可读性与可维护性急剧下降；而且 kirin 等架构的 `__tf__`/`__cce_get_tile_ptr` 都是空宏（arch_macro.hpp L40-L45），合并头无法统一这些关键字级差异。分目录 + 互斥 include 把每代实现保持成「贴紧本代硬件的干净代码」，代价只是目录数量。

**练习 2**：A6 复用 A5 的 TAdd，这会不会导致 A6 上 TAdd 行为错误？

**答案**：不会，因为复用的前提是该指令在两代硬件上的行为与 intrinsic 契约一致（`vadd` 在 A5/A6 上语义相同）。a6/header.hpp 的注释划定的正是这条边界：只有「行为不同」的指令（TLoad/TExtract/TMatmul/TReshape/TQuant/SyncAll）才写 A6 专用版。判断标准不是「硬件代际不同」，而是「PTO 层的实现是否需要不同」。

**练习 3**：`buffer_limits.hpp` 的宏为什么都包在 `#ifndef` 里？

**答案**：提供覆盖逃生口——特殊版本、实验性构建或容量可配置的型号可以在编译命令行 `-DPTO_L0C_SIZE_BYTES=...` 覆盖默认值，而不用改库源码。同时未识别架构直接 `#error`，保证「要么用对、要么显式声明」，避免静默落到错误容量。

## 5. 综合实践

**任务：产出一份《TMatmul 跨代迁移要点清单》（a2a3 → a5 → a6）**。

这是本讲规格指定的实践任务：对比同一指令在 `include/pto/npu/a2a3` 与 `include/pto/npu/a5`（及 a6）下的实现差异，写一份迁移要点清单。要求清单不只是罗列差异，而是能指导一次真实迁移。

**操作步骤**：

1. **收集事实**：按 4.3.4 的差异表方法，并排精读三个文件，覆盖以下维度：设备端模板签名、`mad*` intrinsic 及参数、GEMV 控制方式、int32/float 累加白名单、MX 组合、分形与 TileType 检查、容量检查（`PTO_L0C_SIZE_BYTES`）、bias 打包方式。
2. **补充上下文**：读 [include/pto/common/arch_macro.hpp:L19-L38](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/arch_macro.hpp#L19-L38)（架构宏）、[include/pto/common/arch_capability.hpp:L83-L106](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/arch_capability.hpp#L83-L106)（能力位）、[include/pto/common/buffer_limits.hpp:L111-L121](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/buffer_limits.hpp#L111-L121)（L0C 容量）、[include/pto/npu/a6/header.hpp:L20-L32](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a6/header.hpp#L20-L32)（A6 挂载方式）。
3. **写清单**：按「迁移前检查 / 无需改动 / 需要重调优 / 会被拦截」四类组织要点。参考骨架（请用你自己的观察填实）：
   - **无需改动**：kernel 里 `TMATMUL/TMATMUL_ACC/TMATMUL_BIAS/TGEMV` 的调用写法、有效区取 M/K/N 的语义、事件编排、m/k/n∈[1,4095] 动态范围。
   - **迁移前检查**：dtype 组合是否在目标代白名单（尤其向下迁移）；MX 指令是否目标代存在；通信指令 A6 不可用。
   - **需要重调优**：Acc/L0 相关 tile 形状（A5/A6 L0C 256KiB vs A2A3 128KiB）；m==1 场景在 a2a3 由实现兜底、新代由 TGEMV 显式表达——若你的 kernel 曾依赖该兜底行为，确认新代写法。
   - **会被拦截**：分形布局不合规（a5/a6 收紧的 static_assert）、Acc 超 L0C、不支持的 dtype 组合——都会在编译期或启动期报错，报错文本含架构名。
4. **验证清单**：把清单套用到一个真实算子上（如 u5-l3 的 gemm_performance 或 u5-l5 的 matmul_mxfp4_performance），逐条核对它是否已在源码中处理了对应要点。
5. （可选，需硬件/仿真环境）用 `tests/script/run_st.py -v a5` 跑 tmatmul 相关 ST 用例验证；**待本地验证**。

**预期结果**：一份 15-20 条的迁移要点清单，每条注明依据的源码位置（文件:行号）。这份清单的方法论（找同名三代实现 → 对比 intrinsic/检查/容量 → 四类归档）可平移到任何指令的跨代迁移。

## 6. 本讲小结

- **目录组织**：`include/pto/npu/` 按代际分目录——a2a3（121 项）与 a5（138 项）是全量实现，a6（11 项）是增量实现（6 条专用指令 + 经 header.hpp 复用 A5），kirin 系列另立；`arch_macro.hpp` 把架构号翻译成语义宏与特性宏，`pto_instr_impl.hpp` 据此互斥挂载。
- **A5 新形态**：SIMT（`__simt_vf__`/`LAUNCH_BOUND`/warp 归约/`__sync_workitems`，数据 GM→D-cache→寄存器绕过 UB，性能是 D-cache 局部性的函数）、Int64 双 lane 软件模拟（`vaddc/vaddcs` 进位链，对用户透明）、MX 缩放指令（`TGetScaleAddr` 地址编码）。
- **TMatmul 三代对比**：a2a3 带 `kDirectionAlign` 运行时参数与 m==1→16 hack；a5 把 GEMV 控制编译期化（`gemvCtrl` 模板参数）并新增 `mad_mx` 与 FP8 白名单；a6 用 `PTO_A6_DISPATCH_MAD` 宏分派混合精度 intrinsic（F16×S8/S4、S8×S4 等），HiF4 组合按架构宏门控。
- **架构取舍**：统一性压在公共 API 薄壳、能力位（`ArchTraits`）与容量常量（`buffer_limits.hpp`）三层；实现头完全自由（a2a3 裸指针 + repeat 步长 vs a5 RegTensor/MaskReg/VFImplKind 抽象层）。代际差异尽量编码为编译期拦截。
- **迁移方法论**：先确认不变量（API、有效区语义、事件系统），再逐项核对变化量（intrinsic 契约、dtype 白名单、容量、新指令可用性），最后在目标架构重编译 + ST 验证；向下迁移比向上迁移更易踩白名单。
- **事实核对习惯**：`include/README.md` 状态表可能滞后（尚无 A6 列），以源码挂载点（`a6/header.hpp`）与能力位为准。

## 7. 下一步学习建议

本讲是单元十一的第二讲，学习手册即将收官。建议的后续方向：

1. **动手验证迁移清单**：若你有 A5/A6 真机或 CANN 仿真环境，挑一个 `kernels/manual/a2a3/` 下的算子（如 gemm_performance）尝试迁移编译，用你写的清单预判每一步结果，再与实际编译输出对照——这是对本讲最好的检验。
2. **阅读 kirin 系列后端**：`include/pto/npu/kirin9030/`（19 项）展示了「裁剪版后端」的组织方式（arch_macro.hpp 中 kirin 系列带 `PTO_COMM_NOT_SUPPORTED` 且把 `__tf__` 等关键字定义为空宏），对照 a6 的增量复用，理解「全量/增量/裁剪」三种目录策略。
3. **回读 u10-l3 CostModel 的架构参数**：`costmodel` 中 A5 VF 曲线与各架构流水线参数与本讲的容量常量、能力位同源，理解「功能仿真（CPU）/性能仿真（CostModel）/真机（NPU）」三个后端如何共享架构事实。
4. **关注仓库演进**：A6 通信支持（`ArchTraits` 注释标明为独立工作流）、`include/README.md` 状态表的 A6 列补齐、SIMT 形态是否会反哺到更多指令实现（如 a5/MGather 的先例），都是观察「架构适配如何生长」的活教材。
