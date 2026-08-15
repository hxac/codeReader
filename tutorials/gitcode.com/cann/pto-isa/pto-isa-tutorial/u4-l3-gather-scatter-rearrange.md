# 数据重排指令：TGather/TScatter、TExtract/TInsert 与 MGather/MScatter

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清「按索引重排数据」在 PTO 中分为哪三个层级：tile 内按索引重排（TGATHER/TSCATTER）、GM↔tile 按索引搬运（MGATHER/MSCATTER）、tile 内按窗口搬移（TEXTRACT/TINSERT）。
2. 掌握 TGATHER/TSCATTER 的三种形态：索引驱动、掩码模式（MaskPattern）与比较收集（TGather_cmp），并知道它们的 CPU 仿真与 NPU 实现差异。
3. 掌握 MGATHER/MSCATTER 的 Coalesce（Row/Elem）、OOB（越界策略）与 Atomic（原子写）三个编译期旋钮。
4. 能用 TGATHER + TSCATTER 完成一个「行重排→逆重排还原」的小实践，并验证数据一致。

## 2. 前置知识

- **Gather/Scatter（收集/散布）**：常规搬运（TLOAD/TSTORE）按地址连续搬；Gather 按「索引数组」从源中挑元素（`dst[i] = src[idx[i]]`），Scatter 反过来按索引写到目的位置（`dst[idx[i]] = src[i]`）。embedding 查表、MoE token 重排、TopK 取值都靠它们。
- **掩码模式（MaskPattern）**：当索引不是数组而是编译期已知的规律（如「每 2 个取第 1 个」）时，PTO 用 `MaskPattern::P0101/P1010/P0001/...` 描述，NPU 上可映射到单条向量化指令（如 `vreducev2`），比逐元素索引快得多。枚举定义见 [include/pto/common/type.hpp:L152-L160](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/type.hpp#L152-L160)。
- **平坦（flat）索引**：TGATHER/TSCATTER 的索引语义是对 tile 的**线性一维展开**寻址：`flat = row * Cols + col`。想按「行」重排，就把索引写成 `目标行 * Cols`。
- **流水线与事件**：本讲指令全部挂在 Vector（V）、Scalar（S）、MTE2/MTE3 等流水线上；MGATHER 内部会跨 V/S/MTE 三条流水线协作，需要 set/wait flag 表达依赖（复习 u2-l3）。
- **有效区（valid region）**：重排指令只在 validRow/validCol 内定义语义（复习 u2-l2）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/pto/common/pto_instr.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L1126-L1171) | 公共 API 层：TGATHER/TSCATTER/MGATHER/MSCATTER/TEXTRACT/TINSERT 的统一入口薄壳 |
| [include/pto/common/type.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/type.hpp#L403-L423) | MaskPattern、GatherAxis/GatherOOB、ScatterAxis/ScatterOOB/ScatterAtomicOp/ScatterConflict、Coalesce 等枚举 |
| include/pto/cpu/TGather.hpp、include/pto/cpu/TScatter.hpp | TGATHER/TSCATTER 的 CPU 仿真实现（逐元素循环，功能正确优先） |
| include/pto/npu/a2a3/TGather.hpp、include/pto/npu/a2a3/TScatter.hpp | A2/A3 真机实现（vgather/vreducev2/vcopy intrinsic + DMA） |
| include/pto/npu/a2a3/MGather.hpp、include/pto/npu/a2a3/MScatter.hpp | GM↔tile 级 Gather/Scatter 的真机实现（Row DMA / Elem 逐点 / GM→L1 三条通路） |
| include/pto/cpu/TExtract.hpp、include/pto/cpu/TInsert.hpp | 窗口搬移指令的 CPU 仿真实现 |
| docs/isa/TGATHER.md、docs/isa/TSCATTER.md、docs/isa/MSCATTER.md、docs/isa/TEXTRACT.md、docs/isa/TINSERT.md | ISA 文档（语义、约束、汇编形式） |
| tests/cpu/st/testcase/tgather、tests/cpu/st/testcase/tscatter | ST 用例：索引式与掩码式两种用法都有完整可运行样例 |

## 4. 核心概念与源码讲解

### 4.1 tile 级按索引重排：TGATHER/TSCATTER

#### 4.1.1 概念说明

TGATHER 解决「从片上 tile 中按索引挑数据」的问题，TSCATTER 解决「把 tile 数据按索引写到目的 tile 的指定位置」。两者都有两套驱动方式：

- **索引式**：索引放在一个 int tile 里，运行时才知道具体取哪些元素——通用但慢（逐元素寻址）。
- **掩码式**：选取规律编译期已知（P0101 = 每两个取第一个、P0001 = 每四个取第一个……），走专用向量化指令——快但只支持 2 选 1 / 4 选 1。

此外 TGATHER 还有第三个形态 **比较收集（TGather_cmp）**：逐行与阈值比较，把满足条件的元素下标收集到 dst、命中个数写到 cdst，是 TopK/过滤类算子的底层积木。

#### 4.1.2 核心流程

**索引式 TGATHER**（数学语义，CPU 与文档一致）：

设 dst 有效区为 \( R \times C \)，src0 展平后共 \( N \) 个元素：

\[ \mathrm{dst}_{i,j} = \mathrm{src0}.\mathrm{flat}[\ \mathrm{indices}_{i,j}\ ] \]

- CPU 仿真：越界索引（负数或 \( \ge N \)）写 0；A2/A3 真机不检查越界（target-defined）。
- NPU 实现：先 `vmuls` 把索引乘以元素字节数得到**字节偏移**，再 `vgather` 按 base+偏移取数；因此 A2/A3 上要求索引必须是 b32、数据必须是 b16/b32。

**掩码式 TGATHER**：设掩码周期为 2（P0101/P1010）或 4（P0001/...），

\[ \mathrm{dst}_{i,k} = \mathrm{src}_{i,\ \sigma(k)} ,\quad \sigma(k) = 2k{+}b \ \text{或}\ 4k{+}b \]

- `GATHER_ROW`（默认）：在**每行内部**压缩列，dst 列数 = src 列数 / 2 或 / 4。
- `GATHER_COL`：按掩码**选行**，dst 行数 = src 行数 / 2 或 / 4，列不变。

**索引式 TSCATTER**：

\[ \mathrm{dst}.\mathrm{flat}[\ \mathrm{indices}_{i,j}\ ] = \mathrm{src}_{i,j} \]

注意它是**平坦写**：索引直接给出 dst 的线性偏移。A2/A3 实现会先把 dst 整块清零再散布（未命中的位置为 0）；CPU 仿真**不**清零，需要自己先 TEXPANDS 初始化。

**掩码式 TSCATTER**：TGATHER 掩码版的逆操作——把压缩后的数据放回按掩码展开的位置，未选中位置填 0；`P1111` 退化为一次 TMOV。

#### 4.1.3 源码精读

**(a) 公共 API 层**。与所有 PTO 指令一样是「TSYNC 等待 → 转发 IMPL」薄壳：掩码式 TGATHER 见 [include/pto/common/pto_instr.hpp:L1163-L1171](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L1163-L1171)，比较收集 TGATHER 见 [include/pto/common/pto_instr.hpp:L1126-L1137](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L1126-L1137)，索引式 TSCATTER 见 [include/pto/common/pto_instr.hpp:L2035-L2038](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L2035-L2038)。模板参数（maskPattern、gatherType/scatterType）就是在这里编译期注入的。

**(b) CPU 仿真：索引式 TGATHER 的核心循环**。[include/pto/cpu/TGather.hpp:L93-L114](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TGather.hpp#L93-L114) 中，对每个 (r, c)：读索引 → 越界判断（`IndexInBounds`，负数或超界写 0）→ 把平坦索引换算回 (srcR, srcC) → 经 `GetTileElementOffset` 完成布局感知的取数。这就是「平坦索引」语义的直接证据：

```cpp
const std::size_t flat = static_cast<std::size_t>(raw);
const std::size_t srcR = flat / TileDataS0::Cols;
const std::size_t srcC = flat % TileDataS0::Cols;
```

**(c) CPU 仿真：掩码判定**。[include/pto/cpu/TGather.hpp:L37-L59](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TGather.hpp#L37-L59) 的 `MaskSelect` 用取模实现七种掩码（如 P0101 ⟺ `idx % 2 == 0`）；掩码式 GATHER_ROW 的循环在 [include/pto/cpu/TGather.hpp:L128-L141](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TGather.hpp#L128-L141)，`didx` 只在选中时递增——即「压缩写」。GATHER_COL 的选行版本在 [include/pto/cpu/TGather.hpp:L144-L159](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TGather.hpp#L144-L159)。

**(d) NPU 实现：索引式 TGATHER**。[include/pto/npu/a2a3/TGather.hpp:L54-L89](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TGather.hpp#L54-L89) 按数据位宽分两条路径，套路相同：逐行 `set_vector_mask(0, validCol)` 进入 count 模式 → `vmuls` 把索引乘以 `sizeof(DType)` 算出**字节偏移** → `pipe_barrier` → `vgather` 完成收集。区别在于 b32 数据把 vmuls 结果直接写进 dst，b16 数据必须先落到 `tmp`（文档 docs/isa/TGATHER.md 的 Temporary tile 一节解释了 tmp 的由来）。契约检查（索引必须 b32、数据 b16/b32、dst 连续存储）在 [include/pto/npu/a2a3/TGather.hpp:L18-L30](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TGather.hpp#L18-L30) 与 [include/pto/npu/a2a3/TGather.hpp:L93-L104](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TGather.hpp#L93-L104)。

**(e) NPU 实现：掩码式 TGATHER**。[include/pto/npu/a2a3/TGather.hpp:L121-L135](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TGather.hpp#L121-L135)：GATHER_COL 走逐行 `vcopy`（按掩码算出行跨度）；GATHER_ROW 走单条 `vreducev2`——这正是掩码枚举注释「与 VREDUCEv2 的 pattern mode 保持一致」的落点，一条 intrinsic 完成整 tile 压缩，是它比索引式快的根因。dst 行数约束（= src 行数 / 2 或 / 4）在 [include/pto/npu/a2a3/TGather.hpp:L150-L156](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TGather.hpp#L150-L156)。

**(f) NPU 实现：索引式 TSCATTER**。[include/pto/npu/a2a3/TScatter.hpp:L36-L58](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TScatter.hpp#L36-L58)：先 `InitUBBuffer` 把 dst 清零（[L17-L33](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TScatter.hpp#L17-L33)，用 `vector_dup`），再标量双循环 `dstPtr[ix] = srcPtr[...]` 平坦散布；因为读索引走 Scalar 流水线，前后有 V↔S 的 flag 配对。掩码式的实现（含 `P1111 → TMOV` 的快速路径）在 [include/pto/npu/a2a3/TScatter.hpp:L158-L203](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TScatter.hpp#L158-L203)，掩码→目的偏移的换算表在 [L97-L116](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TScatter.hpp#L97-L116)。对照 CPU 版（[include/pto/cpu/TScatter.hpp:L23-L42](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TScatter.hpp#L23-L42)，不清零；[L91-L95](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TScatter.hpp#L91-L95)，P1111 同样退化 TMOV）可以体会到「两端 *_IMPL 签名逐字相同、行为细节有差」的 PTO 惯例。

**(g) ST 用例是最可靠的用法样例**。索引式 TGATHER 的完整 kernel（含 tmp tile 的 TASSIGN 摆放）见 [tests/cpu/st/testcase/tgather/tgather_kernel.cpp:L504-L552](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tgather/tgather_kernel.cpp#L504-L552)；索引式 TSCATTER（注意它先用 `TEXPANDS(dstTile, 0.0f)` 初始化再散布）见 [tests/cpu/st/testcase/tscatter/tscatter_kernel.cpp:L18-L50](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tscatter/tscatter_kernel.cpp#L18-L50)。

#### 4.1.4 代码实践

**实践目标**：用源码阅读验证「平坦索引」语义，为综合实践做铺垫。

1. 打开 [include/pto/cpu/TGather.hpp:L93-L114](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TGather.hpp#L93-L114)，回答：索引 tile 某元素值为 `130`、src0 是 [8, 16] 的 tile，数据取自哪个 (row, col)？（手算：130 / 16 = 8 余 2 → (8, 2) 越界，写 0——因为 src0 只有 8 行。）
2. 打开 [include/pto/cpu/TScatter.hpp:L34-L41](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TScatter.hpp#L34-L41)，对比 dst 写入方式与 TGATHER 的读取方式，确认两者都是**平坦偏移**。
3. 运行现成 ST 用例：`python3 tests/run_cpu.py --case tgather`（若 `--case` 参数名不符，请先阅读 `tests/run_cpu.py -h` 再选择正确过滤方式；具体命令行为待本地验证）。

**需要观察的现象**：tgather 用例中掩码式（P0101 等）与索引式（test_tgather1D_*）两组用例全部 PASS。

**预期结果**：确认索引语义为 `dst.flat ← src.flat[idx]` 的双向操作，且 CPU 端 TSCATTER 不清零 dst。

#### 4.1.5 小练习与答案

**练习 1**：为什么 A2/A3 的索引式 TGATHER 要求索引必须是 int32/uint32，而掩码式没有这个要求？
**答案**：索引式在 NPU 上用 `vgather` intrinsic，它吃的是**字节偏移**（先 `vmuls` 乘 `sizeof(DType)`），偏移量按 b32 处理；掩码式的选取规律编码在 MaskPattern 模板参数里，走 `vreducev2/vcopy`，根本不读索引 tile。

**练习 2**：`TSCATTER<MaskPattern::P1111>` 会执行什么？为什么这样设计？
**答案**：直接退化为 `TMOV_IMPL(dst, src)`（CPU 版 [include/pto/cpu/TScatter.hpp:L93-L95](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TScatter.hpp#L93-L95)、NPU 版 [include/pto/npu/a2a3/TScatter.hpp:L162-L165](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TScatter.hpp#L162-L165)）。全选掩码的散布等价于整块拷贝，走连续拷贝通路避免逐元素散布的代价。

### 4.2 矩阵级按索引搬运：MGATHER/MSCATTER

#### 4.2.1 概念说明

TGATHER 的索引和源都在片上（UB），如果**源是一张放在 GM 里的大表**（典型如 embedding 表），先把整表 TLOAD 进 UB 再 TGATHER 既不现实也不必要。MGATHER 直接「GM 表 + UB 索引 tile → UB 目的 tile」，跳过整表搬运；MSCATTER 是其镜像（UB tile + 索引 → 散布回 GM），并额外提供**原子加**语义，用于多核向同一 GM 区域累加的场景。

两个编译期旋钮（枚举在 [include/pto/common/type.hpp:L403-L423](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/type.hpp#L403-L423)）：

- **Coalesce（聚合粒度）**：`Row` = 一个索引取整行（embedding 查表），索引 tile 有效形为 [1, R]；`Elem` = 每个元素一个索引（逐点查表），索引 tile 与 dst 同形。
- **OOB（越界策略）**：MGATHER 用 `GatherOOB::{Undefined, Clamp, Wrap, Zero}`；MSCATTER 用 `ScatterOOB::{Undefined, Skip, Clamp, Wrap}`。

#### 4.2.2 核心流程

以最常用的 `MGATHER<Coalesce::Row>`（ND 表）为例：

```
for r in [0, validRow):
    idx = indices[r]                    # Scalar 流水线读索引
    safeIdx = mgather_remap<Oob>(idx, tableRows, doRead)   # 越界重映射
    if doRead:
        MTE2 DMA: dst 第 r 行 ← GM 表第 safeIdx 行（lenBytes = validCol * sizeof(T))
```

数学上：

\[ \mathrm{dst}_{r,\ :} = \mathrm{table}[\min(\mathrm{remap}(\mathrm{idx}_r,\ N)),\ :] \]

其中 remap 依 OOB 取恒等 / 夹取到 \( N-1 \) / 取模 / 越界跳过并填 0。

MSCATTER 的流程对称，加上一个可选原子步：`set_atomic_XXX()` + `set_atomic_add()` 后，多个核对同一 GM 地址的散布会硬件原子累加，最后 `set_atomic_none()` 关闭。

#### 4.2.3 源码精读

**(a) 越界重映射**。[include/pto/npu/a2a3/MGather.hpp:L36-L51](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/MGather.hpp#L36-L51) 的 `mgather_remap` 用 `if constexpr` 按 OOB 模板参数在编译期选定策略，并输出 `doRead` 决定是否发起 DMA。MSCATTER 的对应函数在 [include/pto/npu/a2a3/MScatter.hpp:L49-L64](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/MScatter.hpp#L49-L64)。

**(b) Row 模式主循环：三条流水线协作**。[include/pto/npu/a2a3/MGather.hpp:L103-L149](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/MGather.hpp#L103-L149)（`MGatherRowImpl`）：入口先 `PtoSetWaitFlag<PIPE_V, PIPE_S>` / `<PIPE_MTE3, PIPE_S>` 保证索引 tile（V 产出、MTE3 可能写）就绪；循环体里 Scalar 管线读 `idxPtr[r]`，逐行发 `copy_gm_to_ubuf_*` DMA（MTE2）；出口再为 V/MTE2/MTE3 挂上「数据已就绪」的 flag。这是 u2-l3 事件机制在一条指令内部的实战演出。

**(c) Elem 模式**。[include/pto/npu/a2a3/MGather.hpp:L208-L242](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/MGather.hpp#L208-L242)（`MGatherElemImpl`）：逐元素 `dstPtr[off] = tablePtr[safeIdx]`，无 DMA 聚合，适合完全随机的访问模式。

**(d) 契约检查**。[include/pto/npu/a2a3/MGather.hpp:L405-L479](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/MGather.hpp#L405-L479)（`MGatherCheck`）几乎是一份「使用说明书」：索引必须 int32/uint32、dst/索引必须是 Vec tile、表与 dst 的布局配对只允许「ND 表 + 行主序 ND tile」或「NZ 表 + NZ 分形 tile」两种（NZ 分支见 L441-L453 的 16×C0 约束）、`Coalesce::Row` 要求索引 tile 有效形 [1, R]（L461-L466）、`Coalesce::Elem` 要求索引与 dst 同形（L467-L478）。

**(e) 分发与 GM→L1 通路**。[include/pto/npu/a2a3/MGather.hpp:L482-L554](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/MGather.hpp#L482-L554)（`MGATHER_IMPL`）按「dst 是 Mat（L1）还是 Vec（UB）」「表是 ND 还是 NZ」「Row 还是 Elem」三维分发。特别地，dst 为 Mat 时走 [L295-L325](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/MGather.hpp#L295-L325) 的 GM→L1 通路：gather 结果直接以 NZ 分形摆进 L1，供 Cube 单元做矩阵乘——「查表结果直接喂 Cube，不经过 UB 中转」。

**(f) MSCATTER 的原子写**。[include/pto/npu/a2a3/MScatter.hpp:L92-L108](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e8003a8a/include/pto/npu/a2a3/MScatter.hpp#L92-L108)（`MScatterAtomicAddSet`）按数据类型选择 `set_atomic_f32/f16/bf16/s32/...` 再 `set_atomic_add()`；哪些类型允许 Add 由 [L28-L35](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/MScatter.hpp#L28-L35) 的 `IsValidMScatterAtomic` 编译期拦截。公共 API 的多旋钮重载见 [include/pto/common/pto_instr.hpp:L2112-L2155](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L2112-L2155)。

#### 4.2.4 代码实践

**实践目标**：把 MGATHER 的约束变成肌肉记忆。

1. 阅读 `MGatherCheck`（上面 (d) 的链接），逐条抄下 ND 配路对 tile 布局的要求。
2. 浏览 CPU ST 用例 `tests/cpu/st/testcase/mgather/`（目录含 gen_data.py、main.cpp 等），找到它使用的 Coalesce 模式与 OOB 模式，并读 gen_data.py 中 golden 的计算方式，回答：golden 是怎么模拟越界索引的？
3. 修改设想的调用：把 `MGATHER<Coalesce::Row>` 的索引 tile 故意声明成与 dst 同形（[R, C]），不运行，仅根据 L461-L466 的 static_assert 预测编译错误信息。

**需要观察的现象**：gen_data.py 的 golden 与 kernel 输出逐元素一致（用例 PASS）。

**预期结果**：能不查文档写出一条合法的 MGATHER 调用（dtype、tile 类型、索引形状、布局配对全部合规）。运行结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：embedding 查表应该选 `Coalesce::Row` 还是 `Coalesce::Elem`？索引 tile 什么形状？
**答案**：`Coalesce::Row`——一个 token 的 embedding id 取一整行向量；索引 tile 有效形必须是 [1, R]（R = dst 有效行数）。

**练习 2**：多核各持有一部分计数，要累加到 GM 同一张直方图上，用 MSCATTER 的哪个组合？
**答案**：`MSCATTER<Coalesce::Elem, ScatterAtomicOp::Add, ...>`：散布目的地是逐元素索引，且必须开原子加，否则多核并发写同一 GM 地址会互相覆盖。

**练习 3**：MGATHER 的 GM→L1 通路（dst 为 Mat tile）省掉了什么？
**答案**：省掉「GM→UB（gather）→ UB→L1（再摆分形）」的中转，查表结果直接以 NZ 分形写入 L1，减少一次片上搬运，并让查表与 Cube 计算衔接更紧。

### 4.3 窗口重排：TEXTRACT/TINSERT（附 TConcat/TFillPad）

#### 4.3.1 概念说明

Gather/Scatter 的索引是「数据驱动、逐元素」的；很多场景只需要**固定窗口**的搬移：从一个大 tile 里按编译期形状抠出一个子块（TEXTRACT），或把小 tile 贴进大 tile 的指定角落（TINSERT）。它们是分块流水（把循环里第 k 块抠出来写回）和残差拼接类算子的基本动作，还顺带支持搬移时量化/ReLU（preQuantScalar / fp 向量变体）。同族的 TCONCAT（两 tile 拼接）与 TFILLPAD（把有效区按 PadVal 扩到对齐形状）解决「拼」与「补齐」两个相邻需求，本讲只做定位介绍，不展开。

#### 4.3.2 核心流程

TEXTRACT（无量化变体）：

\[ \mathrm{dst}_{r,\ c} = \mathrm{src}[r + \mathrm{idxRow},\ c + \mathrm{idxCol}] \]

TINSERT 相反：

\[ \mathrm{dst}[r + \mathrm{idxRow},\ c + \mathrm{idxCol}] = \mathrm{src}_{r,\ c} \]

约束：`dst 有效区 + (idxRow, idxCol)` 必须落在 src 有效区内（TINSERT 反之），由 assert 检查。量化变体把每个元素先过 `quantize_element`（scale 来自标量或 fp tile 的第 0 行），常用于 Acc（fp32）→ Vec（int8/fp16）的精度收缩通路。

#### 4.3.3 源码精读

**(a) CPU 仿真的 TEXTRACT 核心**。[include/pto/cpu/TExtract.hpp:L21-L47](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TExtract.hpp#L21-L47)：双循环 + `GetElement/SetElement` 完成布局感知的窗口拷贝；`if constexpr (quantMode != NoQuant)` 分支在同一循环里完成量化（scale 按行主序取列下标、列主序取行下标，见 L33）。分发入口（ConvTile 走专用卷积路径，普通 tile 走窗口拷贝）在 [L134-L142](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TExtract.hpp#L134-L142)。

**(b) TINSERT 核心**。[include/pto/cpu/TInsert.hpp:L19-L44](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TInsert.hpp#L19-L44)：与 TEXTRACT 镜像——窗口内每元素读 src、写 `dst(r+idxRow, c+idxCol)`；三个重载（普通 / preQuantScalar / fp 向量）分别落在 [L47-L50](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TInsert.hpp#L47-L50)、[L59-L70](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TInsert.hpp#L59-L70)、[L72-L85](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TInsert.hpp#L72-L85)。

**(c) ConvTile 特例**。当 src 是卷积格式的 ConvTile（FRACTAL_Z）时，TEXTRACT 切换到 [include/pto/cpu/TExtract.hpp:L115-L131](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TExtract.hpp#L115-L131) 的 `TEXTRACT_CONVTILE_IMPL`：从 L1 的 Mat（FRACTAL_Z）按分形块拷出 Right（L0B）tile，约束检查（src 必须 Mat + FRACTAL_Z、dst 必须 Right）在 [L89-L113](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TExtract.hpp#L89-L113)。这是 u5-l4 卷积通路的伏笔。

**(d) 公共 API 层**。TEXTRACT 家族见 [include/pto/common/pto_instr.hpp:L852-L901](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L852-L901)（含一拆二的变体），TINSERT 家族见 [L1008-L1033](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L1008-L1033)。

#### 4.3.4 代码实践

**实践目标**：用「改一个参数观察行为」的方式理解窗口偏移。

1. 打开 CPU ST 用例 `tests/cpu/st/testcase/textract/`，找到 kernel 中 `TEXTRACT(dst, src, idxRow, idxCol)` 的调用点。
2. 在草稿上画一个 [16, 32] 的 src，分别手算 `(idxRow, idxCol) = (0, 0)` 与 `(4, 8)`、dst 为 [8, 16] 时，dst 每个元素来自 src 的哪个窗口。
3. 对照该用例 gen_data.py 的 golden 切片代码（通常是 numpy 切片 `src[r0:r0+dr, c0:c0+dc]`），确认你的手算与 golden 一致。

**需要观察的现象**：手算窗口与 gen_data.py 的切片区间完全一致。

**预期结果**：能独立写出任意 (idxRow, idxCol) 下的 golden 表达式。

#### 4.3.5 小练习与答案

**练习 1**：TEXTRACT 与 TMOV + TRESHAPE 都能「取子块」吗？区别在哪？
**答案**：不能混用。TMOV 是整块拷贝（形状须匹配）、TRESHAPE 是零拷贝换解释（总字节数须相等）；TEXTRACT 是唯一支持「小窗口从大 tile 抠取」的指令，且允许带量化/ReLU。

**练习 2**：为什么 TEXTRACT 的量化 scale 在行主序时按列下标（c）索引，列主序时按行下标（r）索引？
**答案**：量化参数按「逻辑列」逐列给定时，行主序 tile 的同一列元素在存储中跨行分布，遍历外层是 c（见 [include/pto/cpu/TExtract.hpp:L30-L33](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TExtract.hpp#L30-L33) 外层循环为 c），所以 scalarIndex 取 c；列主序对称取 r。本质是让「同一逻辑列共享一个 scale」。

## 5. 综合实践

**任务：行重排 + 逆还原——用 TGATHER 打乱一个 [64, 64] half tile 的行，再用 TSCATTER 还原，验证数据一致。**

设计思路（基于 4.1 的平坦索引语义）：

- 设行置换为 `perm`（`perm[i]` = 第 i 个输出行取自源的第几行），逆置换 `inv` 满足 `inv[perm[i]] = i`。
- TGATHER 阶段：索引 tile 第 i 行填 `perm[i] * 64 + j`（j 为列号），则 `dst[i, :] = src[perm[i], :]`。
- TSCATTER 阶段：对聚集结果的第 i 行，写回平坦偏移 `perm[i] * 64 + j` 即可还原——**散布索引与收集索引相同**。

示例代码（仿照 [tests/cpu/st/testcase/tgather/tgather_kernel.cpp:L504-L552](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tgather/tgather_kernel.cpp#L504-L552) 与 [tests/cpu/st/testcase/tscatter/tscatter_kernel.cpp:L18-L50](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tscatter/tscatter_kernel.cpp#L18-L50) 编写，**标注为示例代码，未在本讲义中运行**）：

```cpp
using namespace pto;
using TileH  = Tile<TileType::Vec, half,    64, 64, BLayout::RowMajor, -1, -1>;
using TileI  = Tile<TileType::Vec, int32_t, 64, 64, BLayout::RowMajor, -1, -1>;  // 索引必须 b32

TileH srcTile(64, 64), gatTile(64, 64), outTile(64, 64);
TileI idxTile(64, 64);
TASSIGN(srcTile, 0x0);
TASSIGN(idxTile, 0x0 + 64 * 64 * sizeof(half));
TASSIGN(gatTile, 0x0 + 64 * 64 * (sizeof(half) + sizeof(int32_t)));
TASSIGN(outTile, 0x0 + 2 * 64 * 64 * (sizeof(half) + sizeof(int32_t)));

TLOAD(srcTile, srcGlobal);
TLOAD(idxTile, idxGlobal);            // idxTile[i][j] = perm[i] * 64 + j
set_flag(PIPE_MTE2, PIPE_V, EVENT_ID0);
wait_flag(PIPE_MTE2, PIPE_V, EVENT_ID0);
TGATHER(gatTile, srcTile, idxTile, tmpTile);      // 需额外 b32 tmp tile（A2/A3 要求）
TEXPANDS(outTile, (half)0);           // CPU 端 TSCATTER 不清零，必须先铺 0
TSCATTER(outTile, gatTile, idxTile);  // 同一套索引即可还原
set_flag(PIPE_V, PIPE_MTE3, EVENT_ID1);
wait_flag(PIPE_V, PIPE_MTE3, EVENT_ID1);
TSTORE(outGlobal, outTile);
```

操作步骤：

1. 在本地复制 `tests/cpu/st/testcase/tgather/` 四件套为新用例目录（不要改源目录），按上面骨架改写 kernel；host/gen_data 侧用 numpy 生成 `perm = np.random.permutation(64)`、`idx = (perm[:, None] * 64 + np.arange(64))`，golden 直接取 `src`（还原后应与输入全等）。
2. 参考 `tests/README.md` 与 `tests/script/run_st.py` 注册并运行该用例（注册方式待本地确认）。
3. 观察输出：gtest 比对应全部通过，最大误差为 0。
4. 进阶：把 perm 换成 `np.argsort(行号 % 2)`（奇偶行交错），观察输出变为行交错重排后还原仍成功。

**预期结果**：TGATHER 后 gatTile 的第 i 行等于 src 第 perm[i] 行；TSCATTER 后 outTile 与 srcTile 逐元素相等。运行输出待本地验证。

## 6. 本讲小结

- 按索引重排分三层：TGATHER/TSCATTER（UB tile 内）、MGATHER/MSCATTER（GM 表 ↔ UB tile）、TEXTRACT/TINSERT（固定窗口搬移）。
- TGATHER/TSCATTER 的索引语义是**平坦一维偏移**（`flat = row * Cols + col`）；按行重排 = 索引写成 `目标行 * Cols`。
- 索引式走逐元素/`vgather`（慢而通用），掩码式（MaskPattern）走 `vreducev2/vcopy`（快但只支持 2 选 1、4 选 1）；`P1111` 散布退化为 TMOV。
- A2/A3 端契约：索引式 TGATHER 索引必须 b32、数据 b16/b32、需要 tmp tile；NPU 的 TSCATTER 先清零 dst，CPU 端不清零（需手动 TEXPANDS）——CPU 通过不代表真机行为完全一致。
- MGATHER 的三个旋钮：Coalesce（Row=整行查表、Elem=逐点）、GatherOOB（Undefined/Clamp/Wrap/Zero）、目的 tile（Vec=进 UB，Mat=直接 NZ 进 L1 喂 Cube）；MSCATTER 额外支持 ScatterAtomicOp::Add 原子累加。
- TEXTRACT/TINSERT 是窗口级搬移并支持搬移中量化/ReLU；ConvTile 输入时 TEXTRACT 自动切换为卷积分形通路。

## 7. 下一步学习建议

- 下一讲（u4-l4）进入类型转换与量化指令 TCvt/SetQuant/TQuant，本讲 TEXTRACT/TINSERT 中出现的 `quantize_element`、preQuantScalar 将在那里展开。
- 想看索引重排的真实算子用法，可预读 u4-l5 的 TopK（`kernels/manual/a2a3/topk`）——TMrgSort + Gather 的组合。
- 建议同步通读 docs/isa/TGATHER.md 与 docs/isa/MSCATTER.md 的 Constraints 小节，把本讲整理的契约表与官方文档互相印证。
