# 片上搬移类指令：TMov、TTrans、TReshape

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 TMov、TTrans、TReshape 三条指令各自解决什么问题、语义边界在哪里；
- 理解「片上数据重排」的真实代价：有的重排是零拷贝别名（TReshape），有的必须逐元素搬运（TTrans），有的还要做分形重打包（TMOV 的 ND→NZ）；
- 对照 CPU 仿真实现与 NPU 实现阅读这三条指令的源码；
- 在算子流水线中，为一次数据搬运需求选出「改 GlobalTensor 视图 / TMOV / TTRANS / TRESHAPE」中最合适的一种。

## 2. 前置知识

本讲建立在 u2 与 u3 前两讲的概念之上，先快速回顾：

- **片上 tile 与 TileType**：Tile 是片上固定容量的 2-D 缓冲，`TileType::Vec` 对应 UB、`Mat` 对应 L1、`Left/Right` 对应 L0A/L0B、`Acc` 对应 Cube 累加器（见 u2-l2）。不同 TileType 住在不同的片上存储里，**跨存储层级搬数据正是 TMOV 的主业**。
- **有效区（valid region）**：tile 容量形状编译期静态，有效区运行期可动态设定，指令只在有效区内定义语义（见 u2-l2）。
- **布局（BLayout/SLayout/SFractal）**：BLayout 描述块间行/列主序，SLayout 描述分形块内摆放；`NoneBox` 表示非分形的普通行/列主序数据（见 u2-l2）。
- **TASSIGN**：Manual 模式下把片上地址绑给 Tile（见 u3-l2）。本讲的例子中会频繁出现它。
- **事件同步**：跨流水线依赖用 set/wait flag 表达（见 u2-l3）。本讲的指令都挂在 Vector 流水线上（TRESHAPE 除外，它是编译期操作）。
- **一句话区分本讲三条指令**：
  - `TMOV`：把数据从一个 tile **复制/重打包**到另一个 tile（可跨存储层级、可变布局）；
  - `TTRANS`：把 tile **转置**（\( \mathrm{dst}_{i,j} = \mathrm{src}_{j,i} \)），需要显式 tmp；
  - `TRESHAPE`：**不搬任何字节**，只把同一块存储重新解释成另一种形状（bitwise 视角切换）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [docs/isa/TMOV.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TMOV.md) | TMOV 指令的 ISA 文档：语义、ND→NZ/ZN/ZZ 重打包公式、约束与示例 |
| [docs/isa/TTRANS.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TTRANS.md) | TTRANS 的 ISA 文档：转置语义、tmp 尺寸公式、ConvTile 格式变换 |
| [docs/isa/TRESHAPE.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TRESHAPE.md) | TRESHAPE 的 ISA 文档：bitwise reshape 语义与约束 |
| [include/pto/cpu/TMov.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TMov.hpp) | TMOV 的 CPU 仿真实现（逐元素拷贝） |
| [include/pto/cpu/TTrans.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TTrans.hpp) | TTRANS 的 CPU 仿真实现（含 ConvTile 格式变换） |
| [include/pto/cpu/TReshape.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TReshape.hpp) | TRESHAPE 的 CPU 侧实现 |
| [include/pto/npu/a2a3/TMov.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TMov.hpp) | TMOV 的 A2/A3 真机实现（burst 拷贝、fixpipe） |
| [include/pto/npu/a2a3/TTrans.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TTrans.hpp) | TTRANS 的 A2/A3 真机实现（分块转置算法） |
| [include/pto/npu/a2a3/TReshape.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TReshape.hpp) | TRESHAPE 的 A2/A3 真机实现（地址别名） |
| [tests/cpu/st/testcase/ttrans/ttrans_kernel.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/ttrans/ttrans_kernel.cpp) | TTRANS 的 ST 用例 kernel（本讲实践的模板） |
| [tests/cpu/st/testcase/treshape/main.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/treshape/main.cpp) | TRESHAPE 的 ST 用例（断言别名语义） |

## 4. 核心概念与源码讲解

### 4.1 TMov：tile 之间的复制与重打包

#### 4.1.1 概念说明

TMOV 解决的问题是：**同一份数据需要以不同的「存储位置 + 布局 + 甚至 dtype」出现在另一条指令的输入端**。典型场景有三类：

1. **跨存储层级**：Cube 单元吃 L1/L0 里的分形数据，向量计算产出的结果在 UB 里，中间必须有人把数据从 UB 搬到 L1/L0 并顺手摆成 Cube 要的格式——这是 `Vec → Vec`、`Mat → Left/Right/Bias/Scaling`、`Acc → Mat/Vec` 的各条 TMOV 通路（[docs/isa/TMOV.md:L10-L16](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TMOV.md#L10-L16) 介绍了这些通路）。
2. **布局重打包 ND→NZ**：Cube 单元要求操作数是 NZ 分形格式（`C0×C0` 小块、块内列主序），而普通向量计算产出的是行主序 ND 数据，`TMOV(dstNZ, src)` 负责重打包（[docs/isa/TMOV.md:L26-L32](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TMOV.md#L26-L32)）。
3. **量化/ReLU 顺手做**：`Acc → Mat` 通路支持在搬出的同时做 cast、ReLU、标量/向量量化（fixpipe 的硬件能力），见约束清单 [docs/isa/TMOV.md:L187-L199](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TMOV.md#L187-L199)。

注意与 u3-l1 的对比：**TLOAD/TSTORE 负责 GM ↔ 片上，TMOV 只负责片上 ↔ 片上**。

#### 4.1.2 核心流程

最朴素的拷贝语义：

\[ \mathrm{dst}_{i,j} = \mathrm{src}_{i,j}, \quad (i,j) \in \text{有效区} \]

ND→NZ 重打包则是把行主序源数据按 `C0×C0` 分形重新摆放，块间列主序、块内行主序；ND→ZN 更进一步，每个输出分形是源 \(K_0 \times 16\) 切片的**转置**（[docs/isa/TMOV.md:L53-L71](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TMOV.md#L53-L71)）：

\[ D_{ZN}[k_1, n_1, j, i] = S_{ND}[k_1 K_0 + i][n_1 \cdot 16 + j], \quad i \in [0,K_0),\ j \in [0,16) \]

其中 \(K_0 = 32\text{B}/\mathrm{sizeof}(T)\)。真机实现把下标分解全部用 2 的幂移位/掩码完成，避免除法。

调用骨架（与其他指令一致）：

```
TSYNC(events...)   // 折叠等待一组事件
TMOV(dst, src)     // MAP_INSTR_IMPL 转发到 TMOV_IMPL
返回 RecordEvent    // 供下游 wait
```

#### 4.1.3 源码精读

**CPU 仿真端**——最朴素的逐元素拷贝：

- [include/pto/cpu/TMov.hpp:L20-L34](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TMov.hpp#L20-L34)：`TMOV_IMPL` 的主实现。先断言 src/dst 有效区一致，再双重循环 `dst.SetElement(r, c, src.GetElement(r, c))` 逐元素复制。注意 `SetElement/GetElement` 内部走 u2-l2 讲过的 `GetTileOffset` 布局映射，所以**源和目的布局不同时，这一层循环天然完成重打包**——CPU 仿真用「布局感知的逐元素读写」统一覆盖了 ND→NZ/ZN 等所有通路。
- [include/pto/cpu/TMov.hpp:L36-L53](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TMov.hpp#L36-L53)：ReLU 变体——先调朴素版拷贝，再在目的 tile 上把负数清零；其余量化变体（L55-L105）在 CPU 仿真下同样只做拷贝（量化参数被 `(void)` 掉），因为 CPU 仿真只保证功能正确。

**NPU（A2/A3）端**——按通路分发到硬件原语：

- [include/pto/npu/a2a3/TMov.hpp:L188-L196](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TMov.hpp#L188-L196)：`TMOV_IMPL` 入口按「ConvTile 还是普通 tile」二选一分发。
- [include/pto/npu/a2a3/TMov.hpp:L18-L35](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TMov.hpp#L18-L35)：`TMovCcToCb` 把 L1（`__cc__`）数据经 `pto_copy_matrix_cc_to_cbuf` 搬进 UB（`__cbuf__`），同时携带 `QuantPre` 量化模式与 `reluMode`——这就是「搬出累加器时顺手量化+ReLU」的硬件实现。
- [include/pto/npu/a2a3/TMov.hpp:L37-L69](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TMov.hpp#L37-L69)：`TMovToBt` 把 L1 数据搬进 bias 表（`__biasbuf__`），可见行数为 1、64 字节对齐等编译期断言与 [docs/isa/TMOV.md:L207-L214](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TMOV.md#L207-L214) 的约束一一对应。

ISA 文档中的经典用法示例（Vec ND → Mat NZ，为 Cube 准备 Left 操作数）：

- [docs/isa/TMOV.md:L258-L268](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TMOV.md#L258-L268)：源是 `TileType::Vec` + `RowMajor` + `NoneBox`（ND），目的是 `TileType::Mat` + `ColMajor` + `RowMajor`（NZ），一条 `TMOV(dst, src)` 完成重打包，无需 tmp。

#### 4.1.4 代码实践

1. **实践目标**：验证「TMOV 的目的 tile 布局决定数据摆放」。
2. **操作步骤**：
   - 阅读 [docs/isa/TMOV.md:L344-L358](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TMOV.md#L344-L358) 的 Manual 示例（`Mat → TileLeft`）。
   - 打开 [tests/cpu/st/testcase/tmov/](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tmov/) 下的 ST 用例，观察它的源/目的 tile 类型组合。
   - 运行：`python3 tests/script/run_st.py -r sim -t tmov`（run_st.py 的参数见 [tests/script/run_st.py:L270-L282](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/script/run_st.py#L270-L282)，`-r sim` 为仿真模式，`-t` 指定用例名）。
3. **需要观察的现象**：gtest 全绿；若故意把目的 tile 的 `SLayout` 改成与文档约束冲突的组合，编译期 static_assert 会直接报错（而不是运行期出错）。
4. **预期结果**：CPU 仿真下 TMov 用例通过；布局约束在编译期被拦截。
5. 具体输出依赖本地环境，待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `TileType::Mat → TileType::Bias` 的通路要求源 tile 行数必须为 1？

**答案**：bias 是挂在 Cube matmul 上的一维查表数据，硬件 bias 表（`__biasbuf__`）按单行向量组织；[include/pto/npu/a2a3/TMov.hpp:L54-L57](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TMov.hpp#L54-L57) 用 `static_assert(SrcTileData::Rows == 1, ...)` 与 64 字节对齐断言把这个硬件约束固化在编译期。

**练习 2**：ND→NZ 与 ND→ZN 都是「为 Cube 摆数据」，本质区别是什么？

**答案**：ND→NZ 只在 32 字节块网格上重排（`vsstb` 散写），块内数据不动；ND→ZN 还必须转置每个 \(K_0 \times 16\) 分形**内部**的元素，所以用 `vgather2` 做元素级gather（[docs/isa/TMOV.md:L53-L71](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TMOV.md#L53-L71)）。代价上后者显著更贵。

**练习 3**：CPU 仿真的 `TMOV_IMPL` 只有一个双重循环，为什么能覆盖 ND→NZ、ND→ZN 等多种重打包？

**答案**：因为 `GetElement/SetElement` 内部走 `GetTileOffset` 的布局映射（u2-l2），源按源布局读、目的按目的布局写，布局差异在读写两端自然消解；仿真只求功能正确，不建模硬件通路的效率差异。

### 4.2 TTrans：tile 转置

#### 4.2.1 概念说明

TTRANS 解决的问题是：**片上数据需要行列互换**——例如 GEMM 里 B 矩阵按 `[K,N]` 到达却要按 `[N,K]` 消费，或者卷积权重需要转置后进入 im2col 通路。语义就是一个纯转置：

\[ \mathrm{dst}_{i,j} = \mathrm{src}_{j,i} \]

（[docs/isa/TTRANS.md:L14-L18](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TTRANS.md#L14-L18)）

两个容易踩坑的设计点：

- **API 强制要求 tmp 操作数**：`TTRANS(dst, src, tmp)` 三参数（[docs/isa/TTRANS.md:L44-L47](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TTRANS.md#L44-L47)）。真机上转置按分块算法执行，tmp 是算法的暂存区；转置尺寸取自 `src.GetValidRow()/GetValidCol()`（[docs/isa/TTRANS.md:L59](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TTRANS.md#L59)），不一定是容量形状。
- **ConvTile 扩展**：当操作数是 ConvTile（5-D 卷积数据）时，TTRANS 语义升级为格式变换（NCHW↔NC1HWC0、→FRACTAL_Z 等），这是 u5-l4 卷积通路的前置知识（[docs/isa/TTRANS.md:L99-L103](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TTRANS.md#L99-L103)）。

#### 4.2.2 核心流程

2-D tile 转置 `[H, W] → [W, H]`：

```
读取 (r, c) ← src 有效区 (validRow × validCol)
写入 (c, r) → dst
tmp 作为真机分块转置算法的暂存缓冲
```

真机上 tmp 的尺寸公式（[docs/isa/TTRANS.md:L69-L79](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TTRANS.md#L69-L79)）：

\[ \text{tmpSize} = W \times \left\lceil\frac{H}{\text{RowStride}}\right\rceil \times \text{RowStride} \times \text{sizeof(DType)} \]

其中 RowStride 对 b8 类型是 32、对 b16/b32 是 16（对应硬件一次能搬的对齐行数）。当 stride 不满足对齐条件（`dstStride % RowStride == 0`、`srcStride % ElemPerBlock == 0` 等）时，真机退化为标量拷贝路径，此时不需要 tmp。

#### 4.2.3 源码精读

**CPU 仿真端**：

- [include/pto/cpu/TTrans.hpp:L335-L345](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TTrans.hpp#L335-L345)：`TTrans_Impl`——读 `src.GetValidRow()/GetValidCol()` 确定转置域，双重循环里 `dst.SetElement(c, r, src.GetElement(r, c))`，下标一换就是转置。**tmp 在 CPU 仿真里根本不被触碰**（算法不需要暂存），它只是 API 形参。
- [include/pto/cpu/TTrans.hpp:L369-L385](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TTrans.hpp#L369-L385)：`TTRANS_IMPL` 入口——先 static_assert 源/目的元素宽度一致，再按「ConvTile 还是普通 tile」分流；普通 tile 还断言 `Src::ValidRow == Dst::ValidCol && Src::ValidCol == Dst::ValidRow`（转置维度必须镜像匹配）。
- [include/pto/cpu/TTrans.hpp:L53-L107](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TTrans.hpp#L53-L107)：`CheckValidConvShape`——ConvTile 各格式变换的形状断言（如 NCHW→NC1HWC0 要求 \(C_1 = \lceil C/C_0 \rceil\)），可以当「格式变换对照表」读。

**NPU（A2/A3）端**：

- [include/pto/npu/a2a3/TTrans.hpp:L230-L231](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TTrans.hpp#L230-L231)：`TTransOperation`——真机转置的核心算法函数，配套的 `TransFullSubTiles/TransYTailTiles/TransTailTiles`（L67-L228）把转置域切成「整子块 / Y 尾块 / 一般尾块」分别处理，这正是 tmp 存在的原因：分块搬运需要暂存。
- [include/pto/npu/a2a3/TTrans.hpp:L275-L276](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TTrans.hpp#L275-L276)：`TTrans` 主入口（注意真机端函数直接叫 `TTrans` 而非 `TTRANS_IMPL`，由 `MAP_INSTR_IMPL` 桥接）。

**ST 用例**——完整的一条「TLOAD → 同步 → TTRANS → 同步 → TSTORE」流水：

- [tests/cpu/st/testcase/ttrans/ttrans_kernel.cpp:L18-L38](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/ttrans/ttrans_kernel.cpp#L18-L38)：定义源/目的/tmp 三个 tile。注意两处细节：① 列数经过 32 字节对齐公式 `aligned_Cols = ((kTCols_*sizeof(T)+31)/32)*(32/sizeof(T))` 向上取整（容量形状必须满足硬件对齐，有效区才表达真实形状）；② Manual 模式下三块缓冲用 TASSIGN 摆在互不重叠的偏移上。
- [tests/cpu/st/testcase/ttrans/ttrans_kernel.cpp:L46-L56](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/ttrans/ttrans_kernel.cpp#L46-L56)：标准指令序列——TLOAD 后 `set_flag(PIPE_MTE2, PIPE_V)/wait_flag` 挂牌等牌，TTRANS 后 `set_flag(PIPE_V, PIPE_MTE3)/wait_flag`，再 TSTORE。这正是 u1-l4 讲过的 MTE2→V→MTE3 事件链。

#### 4.2.4 代码实践

1. **实践目标**：亲手完成规格中的任务——对一个 [64,64] tile 执行 TTRANS，再把结果 TRESHAPE 成 [128,32]，在 CPU 仿真下验证（见本讲 4.3.4 与第 5 节的综合版本，本步先只做 TTRANS）。
2. **操作步骤**：
   - 复制 `tests/cpu/st/testcase/ttrans/` 目录为 `tests/cpu/st/testcase/ttrans_my/`（注意：这会新增测试文件，请在学习分支上做，不要提交到主干）。
   - 在 `ttrans_kernel.cpp` 中把模板实参改为 `kGRows_=64, kGCols_=64, kTRows_=64, kTCols_=64`，即实例化 `runTTRANS<float, 64, 64, 64, 64>`。
   - 同步修改 `gen_data.py` 中 golden 数据的形状为 64×64 的转置。
   - 运行 `python3 tests/script/run_st.py -r sim -t ttrans_my`。
3. **需要观察的现象**：输出矩阵等于输入矩阵的转置；若把 golden 算错（比如忘了转置），gtest 会逐元素报差。
4. **预期结果**：`[ OK ]` 全部通过；转置语义与 `dst[i][j] == src[j][i]` 一致。
5. 待本地验证（依赖本地 cmake/numpy 环境，可先由 `python3 tests/run_cpu.py` 自动补齐）。

#### 4.2.5 小练习与答案

**练习 1**：CPU 仿真下 tmp 完全没被写过，为什么 API 还强制传它？

**答案**：PTO 是跨后端统一的虚拟 ISA：真机（A2/A3）的分块转置算法需要暂存区（`TransFullSubTiles` 等函数），API 必须按最严后端设计；CPU 仿真只是该指令的一个投影（见 u2-l4 的「三段式薄壳」）。这也意味着 **tmp 的 TASSIGN 不能省**——真机上漏绑会踩到未定义地址。

**练习 2**：一个 fp16 的 [64, 100] tile（有效区即 64×100）做 TTRANS，按公式 tmp 需要多少元素？

**答案**：fp16 是 b16，RowStride=16，H=64, W=100：\(100 \times \lceil 64/16 \rceil \times 16 = 100 \times 4 \times 16 = 6400\) 个元素（×2 字节 = 12800 B）。注意公式里的 W、H 取 validCol/validRow。

**练习 3**：什么时候应该用 TTRANS，什么时候应该直接换 GlobalTensor 的视角（u2-l1）？

**答案**：如果「转置」只发生在 GM 数据的读取方式上，改 GlobalTensor 的 shape/stride 视图是零成本的（TLOAD 时按转置后的 stride 取数即可）；只有当数据已经在片上、且消费端布局无法通过 tile 布局参数表达时，才需要真正执行 TTRANS。**能用视图解决的绝不搬运**——这是片上重排的第一条成本原则。

### 4.3 TReshape：零拷贝的形状重解释

#### 4.3.1 概念说明

TRESHAPE 解决的问题是：**同一块片上存储需要换一种形状/元素类型来看**。它是一个 *bitwise* 操作：不改变任何字节，只改变解释方式（[docs/isa/TRESHAPE.md:L10-L13](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TRESHAPE.md#L10-L13)）。典型用途：把 `[16,16]` 的结果看成 `[1,256]` 喂给逐元素指令；或者把 `[1,8]` 的 fp32 看成 `[2,16]` 的 fp16（位模式重解释）。

三条硬约束（[docs/isa/TRESHAPE.md:L42-L48](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TRESHAPE.md#L42-L48)）：

1. TileType 必须相同（不能 Vec reshape 成 Mat——那是 TMOV 的事）；
2. 总字节数必须相等（`sizeof(InElem)*InNumel == sizeof(OutElem)*OutNumel`）；
3. 不能跨「分形/非分形」边界（`NoneBox` 与 boxed 布局之间禁止 reshape）。

#### 4.3.2 核心流程

```
TRESHAPE(dst, src):
    编译期检查：TileType 相同、总字节相等、类型兼容、不跨 box 边界
    执行期动作：
        A2/A3 真机  → TASSIGN_IMPL(dst, src.data())   // dst 直接绑到 src 的地址：别名
        __PTO_AUTO__→ __cce_alias(dst.data(), src.data(), 0)
        CPU 仿真    → dst.data() = src.data()          // 同样是指针重绑
```

关键理解：**TRESHAPE 之后 dst 和 src 指向同一块存储**，写其中一个另一个立刻可见。它不是拷贝！

#### 4.3.3 源码精读

- [include/pto/cpu/TReshape.hpp:L20-L50](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TReshape.hpp#L20-L50)：四组 static_assert 依次落地三条约束 + 元素类型兼容性（浮点↔浮点、整数↔整数）。
- [include/pto/cpu/TReshape.hpp:L52-L62](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TReshape.hpp#L52-L62)：`#ifdef __CPU_SIM` 分支把 `src.data()` 指针直接赋给 `dst.data()`——别名实现；`#else` 分支（非 CPU_SIM 复用此头文件的路径）做逐字节拷贝。⚠️ 注意：[docs/isa/TRESHAPE.md:L50-L53](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TRESHAPE.md#L50-L53) 的 Notes 写「CPU 仿真为逐字节拷贝、A2/A3 为别名」，与当前代码**相反**——以代码和 ST 测试为准（CPU 仿真同样是别名），文档此处疑似过时，待确认。
- [include/pto/npu/a2a3/TReshape.hpp:L40-L54](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TReshape.hpp#L40-L54)：真机 Manual 模式用 `TASSIGN_IMPL(dst, reinterpret_cast<uintptr_t>(src.data()))` 完成地址别名；Auto 模式用编译器内建 `__cce_alias`。**真机上是零周期操作**，不产生任何搬运指令。
- [tests/cpu/st/testcase/treshape/main.cpp:L22-L45](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/treshape/main.cpp#L22-L45)：ST 用例直接断言别名语义——`ASSERT_EQ(dst.data(), src.data())`，随后写 `src.data()[17]` 断言 `dst.data()[17]` 可见、反向亦然。这是「文档说法 vs 代码行为」争议的最权威裁决。

#### 4.3.4 代码实践

1. **实践目标**：验证 TRESHAPE 的别名语义与字节相等约束。
2. **操作步骤**：
   - 运行现成用例：`python3 tests/script/run_st.py -r sim -t treshape`。
   - （示例代码）在自己的试验目录里写一个最小片段：

     ```cpp
     // 示例代码：非项目原有文件，仅演示 TRESHAPE 用法
     using namespace pto;
     using Src = Tile<TileType::Vec, float, 64, 64>;   // 4096 个 float
     using Dst = Tile<TileType::Vec, float, 128, 32>;  // 仍是 4096 个 float
     Src src; Dst dst;
     TASSIGN(src, 0);
     TASSIGN(dst, Src::GetSizeInBytes());  // Manual 下仍要给 dst 绑地址，但 TRESHAPE 后二者别名
     TRESHAPE(dst, src);
     ```

   - 再试着把 `Dst` 改成 `Tile<TileType::Vec, float, 100, 40>`（4000 个 float）。
3. **需要观察的现象**：第一段编译通过且 `dst.data() == src.data()`；第二段在**编译期**报 `TRESHAPE: Total byte size must match.`。
4. **预期结果**：字节相等约束由 static_assert 拦截，错误信息直接给出原因；别名使得通过 dst 写入的数据立即可从 src 读到。
5. 待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`Tile<TileType::Vec, int32_t, 8, 8>` 能 reshape 成 `Tile<TileType::Vec, int16_t, 16, 8>` 吗？

**答案**：可以。总字节都是 256（64×4B = 128×2B），TileType 相同、整数↔整数兼容、双方都是 NoneBox，四条约束全部满足。这正是「位模式重解释」的用法——一对 int16 拼成一个 int32 的视角。

**练习 2**：TRESHAPE 之后，原来 src 的 TASSIGN 地址还有效吗？通过 src 还能读写数据吗？

**答案**：有效，且完全可读写。TRESHAPE 是别名而非移动，src 与 dst 共享同一存储（[tests/cpu/st/testcase/treshape/main.cpp:L40-L44](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/treshape/main.cpp#L40-L44) 双向验证了这一点）。要警惕的反而是**别名带来的意外共享**：改了 src 会"顺便"改掉 dst。

**练习 3**：把 u3-l1 学过的 TSTORE 与本讲指令串起来：`[64,64]` tile 转置后 reshape 成 `[128,32]`，写回 GM 时 TSTORE 的 GlobalTensor 视图应该是什么形状？

**答案**：转置 + reshape 后 tile 的有效形状是 128 行 × 32 列，因此 GlobalTensor 视图应为 `Shape(1,1,1,128,32)`、最内维 stride 为 1（模仿 [ttrans_kernel.cpp:L42-L44](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/ttrans/ttrans_kernel.cpp#L42-L44) 的构造方式）。GM 只认最终写出的字节布局，中间经历过几次视角切换它并不关心。

## 5. 综合实践

**任务**：完成本讲规格指定的综合实践——对一个 [64,64] tile 执行 TTRANS，再执行 TRESHAPE 成 [128,32]，用 CPU 仿真打印中间结果验证。

**步骤**（基于 `tests/cpu/st/testcase/ttrans/` 改造，建议复制为新目录 `ttrans_reshape`，在学习分支上操作）：

1. **定义 tile**：仿照 [ttrans_kernel.cpp:L25-L34](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/ttrans/ttrans_kernel.cpp#L25-L34)，取 `kTRows_=64, kTCols_=64`，fp32 下 32 字节对齐公式不改变 64 这个值，因此源/目的 tile 分别是 `Tile<Vec, float, 64, 64>` 与 `Tile<Vec, float, 64, 64>`（转置后形状不变），tmp 同形；另加 `using FlatT = Tile<TileType::Vec, float, 128, 32>;`。
2. **TASSIGN 摆缓冲**：src 在 0、dst 在 `64*64*4`、tmp 在 `2*64*64*4`（模仿 L36-L38，务必互不重叠——u3-l2 讲过 Manual 模式的第一责任）。
3. **指令序列**：

   ```
   TLOAD(srcTile, srcGlobal);
   set_flag(MTE2, V, EVENT_ID0); wait_flag(MTE2, V, EVENT_ID0);
   TTRANS(dstTile, srcTile, tmpTile);
   TRESHAPE(flatTile, dstTile);            // 别名：flatTile 与 dstTile 同存储
   set_flag(V, MTE3, EVENT_ID0); wait_flag(V, MTE3, EVENT_ID0);
   TSTORE(flatGlobal, flatTile);           // flatGlobal 是 [128,32] 视图
   ```

4. **golden 验证**：在 `gen_data.py` 里对输入 `x`（64×64）先 `x.T` 再 `.reshape(128, 32)`（numpy 按行主序 reshape，与 tile 行主序一致）作为期望输出；也可以在 main.cpp 里把中间 tile 打印出来人工核对 `dst[r][c] == src[c][r]`。
5. **运行**：`python3 tests/script/run_st.py -r sim -t ttrans_reshape`。
6. **观察点**：
   - TRESHAPE 后写 flatTile 与写 dstTile 是同一块存储，TSTORE 实际写出的是转置结果的字节流；
   - 总字节数 64×64×4 = 128×32×4 满足 reshape 约束；
   - 若把 reshape 目的写成 [128,31]（3968 个元素），编译期即报 `Total byte size must match`。
7. **预期结果**：gtest 通过，输出矩阵每行 32 个元素、共 128 行，第 `i` 行第 `j` 列的值等于 `src[j/32... ]` 按 `x.T.reshape(128,32)` 展开后的值。待本地验证。

## 6. 本讲小结

- **TMOV 是片上↔片上的搬运工**：跨 TileType 存储层级（Vec↔Mat↔Acc 等）、跨布局（ND→NZ/ZN/ZZ 重打包）复制数据，`Acc→Mat` 通路还能顺手做量化与 ReLU；GM↔片上则归 u3-l1 的 TLOAD/TSTORE 管。
- **TTRANS 是真搬运**：\( \mathrm{dst}_{i,j} = \mathrm{src}_{j,i} \)，转置域由有效区决定，API 强制 tmp（真机分块算法需要）；ConvTile 场景下升级为 NCHW↔NC1HWC0↔FRACTAL_Z 格式变换。
- **TRESHAPE 是零拷贝别名**：只改解释方式不改字节，约束是 TileType 相同、总字节相等、不跨 box 边界；CPU 仿真与 A2/A3 真机都实现为地址别名（ST 用例断言了这一点，ISA 文档 Notes 与代码不一致，以代码为准）。
- **成本排序**：改 GlobalTensor 视图（免费）< TRESHAPE（零周期别名）< TMOV（一次片上搬运/重打包）< TTRANS（分块转置，最贵）。为算子选指令时按这个顺序优先。
- **CPU 仿真的角色**：TMOV 用布局感知的逐元素循环统一覆盖所有通路、TTRANS 忽略 tmp、TRESHAPE 重绑指针——仿真只保证功能正确，通路效率与 tmp 真实用量必须以 NPU 实现与 ISA 文档为准。
- ST 用例 `ttrans` 展示了标准的 `TLOAD → 事件 → 计算 → 事件 → TSTORE` 流水骨架，可直接作为自己写用例的模板。

## 7. 下一步学习建议

- 下一讲 **u3-l4「CPU 仿真实现剖析：以 TAdd 为例读透一条指令」**将把本讲的「CPU 仿真套路」推广到 `ElementTileOp` 通用骨架，解释为什么几十条逐元素指令能共享一份仿真代码。
- 学习 u4-l3 的 Gather/Scatter 前，回头体会本讲的「视图 vs 搬运」二分法——MGather/MScatter 本质是「带索引的 GM 视图读取」。
- u5-l1（TMatmul）与 u5-l4（卷积）会大量使用本讲的 TMOV ND→NZ 重打包与 TTRANS ConvTile 格式变换，届时把 [docs/isa/TMOV.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TMOV.md) 的重打包公式当作速查表。
- 建议继续阅读的源码：[include/pto/npu/a2a3/TTrans.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TTrans.hpp) 的分块转置算法（`TransFullSubTiles` 系列）与 [include/pto/npu/a2a3/TMov.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TMov.hpp) 的各通路分发。
