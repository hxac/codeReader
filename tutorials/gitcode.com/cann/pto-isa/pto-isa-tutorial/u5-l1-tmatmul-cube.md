# TMatmul 与 Cube 单元：矩阵乘指令族

## 1. 本讲目标

学完本讲，你应该能够：

1. 掌握 `TMATMUL`、`TMATMUL_ACC`、`TMATMUL_BIAS`（以及 GEMV 变体）的 API 形式与各自的适用场景。
2. 理解 Cube 单元（M 流水线）的输入/输出 tile 位置约束：为什么 A 必须是 `TileLeft`、B 必须是 `TileRight`、C 必须是 `TileAcc`。
3. 理解一条 matmul 的数据通路：GM → L1（Mat tile）→ L0A/L0B（Left/Right tile）→ 累加器（Acc tile）→ 写回，以及 K 维分块累加（split-K）时 `TMATMUL_ACC` 的作用。
4. 能对照 CPU 仿真实现与 NPU intrinsic 实现阅读同一条指令的双后端代码。

## 2. 前置知识

本讲建立在前面几讲的概念之上，先用两段话把它们串起来。

**Cube 单元与三条流水线。** 昇腾 AICORE 上有三类计算/搬运流水线：MTE2（搬入）、MTE3（搬出）、Vector（向量计算）以及 **Cube（矩阵计算，pipe 名为 `PIPE_M`）**。前面单元里读的 TADD/TRowSum 等都是 Vector 指令，而矩阵乘走的是 Cube 单元——它是独立的硬件乘累加阵列，有自己的输入缓冲 L0A/L0B 和输出累加器。Cube 与 Vector/MTE 并行执行，所以跨流水线的依赖依然要用 u2-l3 学过的事件（set_flag/wait_flag）表达。

**Tile 的五族属性回顾。** u2-l2 讲过每个 Tile 都有「位置（TileType）」属性。本讲它会变成主角：Cube 指令对操作数位置的检查是**编译期强制的**——`TileType::Left` 对应 L0A、`TileType::Right` 对应 L0B、`TileType::Acc` 对应累加器、`TileType::Bias` 对应 bias 缓冲。此外，`TileType::Mat` 对应 L1 上的中转 tile（u3-l1 讲过 TLOAD 可以把 GM 数据搬进 Mat tile 并边搬边分形）。这几个别名在 u2-l4 的分层里由 `TileLeft/TileRight/TileAcc` 模板提供，本讲会看到它们的默认布局参数。

**有效区决定 M/K/N。** 和所有 PTO 指令一样，matmul 的数学语义定义在**有效区**内：M、K、N 三个维度的实际长度分别取自三个 tile 的 validRow/validCol，而容量形状（Rows/Cols）负责对齐与分形摆放。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [docs/isa/TMATMUL.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TMATMUL.md) | TMATMUL 的 ISA 文档：数学语义、约束、示例 |
| [include/pto/common/pto_instr.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp) | 公共 API：TMATMUL/TMATMUL_ACC/TMATMUL_BIAS 的薄壳声明与重载 |
| [include/pto/npu/a2a3/TMatmul.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TMatmul.hpp) | A2/A3 真机实现：检查 + 映射到 `mad` intrinsic |
| [include/pto/cpu/TMatmul.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TMatmul.hpp) | CPU 仿真实现：三重循环 + FMA 求功能正确 |
| [include/pto/common/pto_tile.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp) | `TileLeft/TileRight/TileAcc` 别名的默认布局定义 |
| [include/pto/common/type.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/type.hpp) | `AccPhase` 枚举定义 |
| [tests/cpu/st/testcase/tmatmul/tmatmul_kernel.cpp](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tmatmul/tmatmul_kernel.cpp) | ST 用例 kernel：完整展示 TLOAD→TMOV→TMATMUL(→ACC)→TSTORE 链路 |
| [tests/cpu/st/testcase/tmatmul/gen_data.py](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tmatmul/gen_data.py) | ST 用例造数：numpy golden 的生成方式 |

## 4. 核心概念与源码讲解

### 4.1 TMatmul 基本形式

#### 4.1.1 概念说明

`TMATMUL` 是 Cube 单元上的矩阵乘指令：输入两个 tile（左矩阵 A、右矩阵 B），输出一个累加器 tile C。它解决了「用 Vector 指令做矩阵乘效率极低」的问题——Cube 单元是专门的矩阵乘累加阵列，一条 `mad` 指令（matrix add-multiply）就能完成一整个 tile 的乘累加。

数学语义（定义在有效区内）：

\[ C_{i,j} = \sum_{k=0}^{K-1} A_{i,k} \cdot B_{k,j}, \quad 0 \le i < M,\ 0 \le j < N \]

其中 \( M = A.\text{validRow} \)、\( K = A.\text{validCol} \)、\( N = B.\text{validCol} \)。这与 ISA 文档中的定义一致，见 [docs/isa/TMATMUL.md:L14-L24](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TMATMUL.md#L14-L24)（文档中的数学诠释小节，M/K/N 的取法写得很明确）。

#### 4.1.2 核心流程

一条 `TMATMUL(c, a, b)` 的执行流程：

```
TMATMUL(cTile, aTile, bTile, events...)
  ├─ TSYNC(events...)            # 折叠等待上游事件（如 MTE1 的 TMOV 完成牌）
  ├─ MAP_INSTR_IMPL(TMATMUL, ...)  # 宏转发到 TMATMUL_IMPL
  │    ├─ CheckStaticMad()       # 编译期：dtype 三元组、TileType 位置
  │    ├─ m/k/n = 各 tile 有效区   # 运行期：取 validRow/validCol
  │    ├─ CheckDynamicMad(m,k,n)  # 运行期：m/k/n ∈ [1, 4095]
  │    └─ TMatmul<...>(...)       # 取 __ca__/__cb__/__cc__ 指针，调 mad intrinsic
  └─ return RecordEvent          # 返回挂在 PIPE_M（Cube）上的事件
```

数据通路（NPU 上）：

```
GM --TLOAD--> L1 (Mat tile, 可边搬边分形)
              --TMOV--> L0A (Left tile) / L0B (Right tile)
                         --TMATMUL(mad)--> 累加器 (Acc tile, __cc__)
                                            --TSTORE/TMOV--> GM/UB
```

#### 4.1.3 源码精读

**公共 API 薄壳。** 与 u3-l4 总结的三段式骨架完全一致——TSYNC 等待、转发 IMPL、返回 RecordEvent：

[include/pto/common/pto_instr.hpp:L654-L670](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L654-L670) 定义了两个 `TMATMUL` 重载：普通形式和带 `AccPhase` 模板参数的 UF-aware 形式（见 4.3.3 对 AccPhase 的解释）。

```cpp
template <typename TileRes, typename TileLeft, typename TileRight, typename... WaitEvents>
PTO_INST RecordEvent TMATMUL(TileRes& cMatrix, TileLeft& aMatrix, TileRight& bMatrix, WaitEvents&... events)
{
    TSYNC(events...);
    MAP_INSTR_IMPL(TMATMUL, cMatrix, aMatrix, bMatrix);
    return {};
}
```

**NPU 实现：从 tile 到 `mad` intrinsic。** [include/pto/npu/a2a3/TMatmul.hpp:L156-L167](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TMatmul.hpp#L156-L167) 是 `TMATMUL_IMPL`：先静态检查，再从有效区取出 m/k/n，动态检查后调用内层 `TMatmul` 模板。注意模板实参 `<Phase, TileRes, TileLeft, TileRight, false, true, false>`——倒数第二个布尔 `cmatrixInitVal=true` 表示「本次乘累加要**初始化**累加器（覆盖旧值）」，这正是 TMATMUL 与 TMATMUL_ACC 的本质区别（见 4.2）。

[include/pto/npu/a2a3/TMatmul.hpp:L37-L53](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TMatmul.hpp#L37-L53) 是内层 `TMatmul`，把三个 tile 数据指针分别翻译到 Cube 的三个地址空间后调用 `mad`：

```cpp
__cc__ typename TileRes::DType* c = ...;   // 累加器地址空间
__ca__ typename TileLeft::DType* a = ...;  // L0A
__cb__ typename TileRight::DType* b = ...; // L0B
...
mad(c, a, b, m, k, n, static_cast<uint8_t>(Phase), kDirectionAlign, cmatrixSource, cmatrixInitVal);
```

其中 L47-L51 有个值得注意的细节：非 GEMV 路径下若 `m == 1` 会强制改成 16，注释写明是「避免在 A3 上落入 gemv 模式」——单行矩阵乘在硬件上有独立通路，这里显式绕开以保证行为一致。

另外 [L20-L35](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TMatmul.hpp#L20-L35) 的 `GetKDirectionAlign` 仅对 f32×f32 生效，检查 K 方向是否对齐，作为 `mad` 的 `kDirectionAlign` 参数传入（性能提示，不影响语义）。

**CPU 仿真实现：三重循环求正确。** [include/pto/cpu/TMatmul.hpp:L165-L169](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TMatmul.hpp#L165-L169) 的 `TMATMUL_IMPL` 只是把 `acc` 指针传 `nullptr` 然后调 `TMatmulNzZn`。核心计算在 [L50-L77](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TMatmul.hpp#L50-L77)：

```cpp
cpu::parallel_for_1d(0, M, ..., [&](std::size_t i) {      // 行级并行
    for (uint16_t j = 0; j < N; j++) {
        typename TileAcc::DType mul_acc = 0;
        PTO_CPU_VECTORIZE_LOOP
        for (uint16_t k = 0; k < K; k++) {
            ...
            mul_acc = std::fma(a, b, mul_acc);   // float 用 FMA
        }
        dst.SetElement(i, j, acc ? acc->GetElement(i, j) + mul_acc : mul_acc);
    }
});
```

读点有三个：一是 `acc` 指针是否为空正好复用了「初始化/累加」两种语义（TMATMUL 传 `nullptr` 即不累加）；二是 float 路径用 `std::fma`（融合乘加，只舍入一次），非 float 走 `+=`；三是 `GetElement/SetElement` 内部会按 tile 的分形布局做坐标映射，所以仿真天然支持 Nz/Zn 摆放。

#### 4.1.4 代码实践

**实践目标**：跑通官方 tmatmul ST 用例，观察一条 TMATMUL 的完整链路。

**操作步骤**：

1. 在仓库根目录执行 `python3 tests/run_cpu.py -t tmatmul --verbose`（`-t` 指定单个用例，见 [tests/run_cpu.py:L455-L456](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_cpu.py#L455-L456)）。
2. 打开 [tests/cpu/st/testcase/tmatmul/tmatmul_kernel.cpp:L75-L109](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tmatmul/tmatmul_kernel.cpp#L75-L109)，对照 gtest 输出找到 `case1`（fp16×fp16→fp32，M=40/K=50/N=60）执行的代码路径。

**需要观察的现象**：用例先由 `gen_data.py` 生成 `x1_gm.bin/x2_gm.bin/golden.bin`，然后 cmake 构建、gtest 逐 case 比对通过。

**预期结果**：所有 `TMATMULTest.*` 用例 PASSED。kernel 中的链路是 `TLOAD(Mat tile) → set/wait(MTE2→MTE1) → TMOV(Left/Right tile) → set/wait(MTE1→M) → TMATMUL → set/wait(M→FIX) → TSTORE`——注意 TMATMUL 前等的是 `PIPE_MTE1`（TMOV 所在流水线）的牌，之后给 `PIPE_FIX` 挂牌，这正是 u2-l3 事件机制在 Cube 通路上的应用。运行结果待本地验证（取决于本机 GCC ≥ 13 / Clang ≥ 15 的 C++20 环境）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `TMATMUL` 的 A/B/C 不能都用 `TileType::Vec` 的普通 tile？
**答案**：静态检查会失败。Cube 单元的操作数有物理落点：A 必须在 L0A（`TileType::Left`）、B 在 L0B（`TileType::Right`）、C 在累加器（`TileType::Acc`）。`CheckStaticMad` 中的 `static_assert(TileLeft::Loc == TileType::Left, ...)` 等三条断言（[include/pto/npu/a2a3/TMatmul.hpp:L94-L96](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TMatmul.hpp#L94-L96)）在编译期拦截这种写法。

**练习 2**：M=100、K=200、N=64 的 fp16 矩阵乘，`m/k/n` 三个运行期参数分别从哪来？会不会触发动态检查失败？
**答案**：m = `aMatrix.GetValidRow()` = 100，k = `aMatrix.GetValidCol()` = 200，n = `bMatrix.GetValidCol()` = 64（[include/pto/npu/a2a3/TMatmul.hpp:L160-L162](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TMatmul.hpp#L160-L162)）。三者都在 [1, 4095] 内，不会触发 `CheckDynamicMad` 断言。

### 4.2 累加与偏置变体

#### 4.2.1 概念说明

矩阵乘在实际算子中很少孤立出现，最常见的两个伴生需求是：

- **K 维分块累加**：K 太大一次算不完（或想把 K 拆给多份输入流水线处理），需要「这一轮的结果加到上一轮的累加器上」——这就是 `TMATMUL_ACC`。
- **偏置加法**：神经网络全连接层/attention 投影都是 `C = A·B + bias`——这就是 `TMATMUL_BIAS`，bias 是长度为 N 的一行向量，硬件有专门的 bias 缓冲通路，不必再用一条 Vector 指令补加。

还有一族 GEMV 变体（`TGEMV/TGEMV_ACC/TGEMV_BIAS`），语义上是 M=1 的矩阵乘，走硬件的 gemv 通路，本讲把它们当作变体一并认识（MX 混合精度变体 `TMATMUL_MX` 留到 u5-l5）。

#### 4.2.2 核心流程

三者的语义差异可以用一个统一公式描述：

\[ \text{TMATMUL: } C = A \cdot B \qquad \text{TMATMUL\_ACC: } C_{out} = C_{in} + A \cdot B \qquad \text{TMATMUL\_BIAS: } C = A \cdot B + \text{bias}_{j} \]

split-K 主循环的模式（也是综合实践的任务）：

```
for i in 0..R-1:
    TLOAD A_i, B_i            # 第 i 段 K 的输入
    TMOV 到 Left/Right tile
    if i == 0:
        TMATMUL(c, a, b)      # 首轮：初始化累加器
    else:
        TMATMUL_ACC(c, c, a, b)  # 后续轮：累加到 c（in/out 可共用同一 tile）
TSTORE(c)
```

数学上等价于一次大 K 的乘法：

\[ \sum_{i=0}^{R-1} A_i \cdot B_i = A \cdot B, \quad K = \sum_i K_i \]

#### 4.2.3 源码精读

**TMATMUL_ACC：一个布尔翻转出累加语义。** NPU 侧 [include/pto/npu/a2a3/TMatmul.hpp:L169-L187](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TMatmul.hpp#L169-L187)：

```cpp
PTO_INTERNAL void TMATMUL_ACC_IMPL(TileRes& cOutMatrix, TileRes& cInMatrix, TileLeft& aMatrix, TileRight& bMatrix)
{
    ...
    TMatmul<Phase, TileRes, TileLeft, TileRight, false, false, false>(  // cmatrixInitVal=false
        cOutMatrix.data(), aMatrix.data(), bMatrix.data(), m, k, n, kDirectionAlign);
}

// Convenience overload when accumulator input/output share the same tile.
PTO_INTERNAL void TMATMUL_ACC_IMPL(TileRes& cMatrix, TileLeft& aMatrix, TileRight& bMatrix)
{
    TMATMUL_ACC_IMPL<Phase, TileRes, TileLeft, TileRight>(cMatrix, cMatrix, aMatrix, bMatrix);
}
```

与 `TMATMUL_IMPL` 相比，硬件层面的全部差异就是 `mad` 的 `cmatrixInitVal` 实参从 `true` 变 `false`——告诉 Cube 单元不要清零累加器，把乘累加结果叠上去。C++ 层面则多了一个「输入/输出共用同一 tile」的便利重载，L182 的注释明确说明它是转发到双 tile 版本。CPU 侧对应实现是 [include/pto/cpu/TMatmul.hpp:L171-L175](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TMatmul.hpp#L171-L175)：把 `&cInMatrix` 作为 `acc` 指针传入 `TMatmulNzZn`，于是 L74 那行 `dst.SetElement(i, j, acc->GetElement(i, j) + mul_acc)` 生效——同一个 `acc` 指针在两个后端里承担了同一个语义角色。

公共 API 有三个重载（无 Phase / 有 Phase / 共用 tile 版本），见 [include/pto/common/pto_instr.hpp:L672-L699](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L672-L699)。

**TMATMUL_BIAS：bias 指针打包进 C 的高 32 位。** NPU 侧 [include/pto/npu/a2a3/TMatmul.hpp:L55-L75](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TMatmul.hpp#L55-L75) 的 `TMatmulBias` 里最有意思的是这两行（L67-L68）：

```cpp
uint64_t xd = ((uint64_t)c) & 0xffffffffULL | ((((uint64_t)d) & 0xffffffffULL) << 32);
c = (__cc__ typename TileRes::DType*)xd;
```

bias 指针 `d`（`__biasbuf__` 空间）被移到高 32 位、与 C 指针拼成一个 64 位值一起传给 `mad`——这是底层 intrinsic 的接口约定：一条 `mad` 同时拿到 C 和 bias 的地址，硬件在做完乘累加后顺手把 bias 加上，省掉一条单独的 Vector 加法指令和一次 Acc→UB 的往返。bias 的静态约束在 `TMATMUL_BIAS_IMPL`（[L189-L204](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TMatmul.hpp#L189-L204)）：类型必须与 C 相同、`TileType::Bias`、只有一行。CPU 侧则直白得多：先做普通 matmul，再逐列把 `bias(0, c)` 加到每一行（[include/pto/cpu/TMatmul.hpp:L177-L188](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TMatmul.hpp#L177-L188)），其中 `CheckBiasValid`（[L128-L137](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TMatmul.hpp#L128-L137)）复刻同样的约束。

**现成的 split-K 范例。** [tests/cpu/st/testcase/tmatmul/tmatmul_kernel.cpp:L114-L173](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tmatmul/tmatmul_kernel.cpp#L114-L173) 的 `RunTMATMUL_SPLIT_K` 就是 4.2.2 伪代码的原文实现：循环里第 0 轮走 `TMATMUL`、其余轮走 `TMATMUL_ACC(cTile, cTile, ...)`（L155-L167），注意这个用例的循环里没有插事件（CPU 仿真单线程按序执行，可省；真机上需要在轮间补依赖，参见 u6-l2）。L185-L186 把它实例化为 `<float, half, half, 128, 128, 64>`、repeats=5。

#### 4.2.4 代码实践

**实践目标**：体会「repeats 次累加 ≡ 一次大 K」的等价性，并亲眼看到 TMATMUL/TMATMUL_ACC 的分工。

**操作步骤**（源码阅读 + 本地改写）：

1. 读 [gen_data.py:L43-L49](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tmatmul/gen_data.py#L43-L49)：golden 就是 `for i in range(repeats): golden += matmul_reference(x1[i], x2[i])`——numpy 侧同样用「逐段累加」构造参考答案，与 kernel 的 TMATMUL_ACC 循环一一对应。
2. 把 `case3` 的参数（[gen_data.py:L105](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tmatmul/gen_data.py#L105)）改为 `tmatmulParams(np.float16, np.float16, np.float32, 128, 128, 128, False, repeats=4)`。
3. 同步把 [tmatmul_kernel.cpp:L185-L186](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tmatmul/tmatmul_kernel.cpp#L185-L186) 的实例化改为 `RunTMATMUL_SPLIT_K<float, half, half, 128, 128, 128>` 且 repeats 传 `4`。
4. 重新运行 `python3 tests/run_cpu.py -t tmatmul -g 'TMATMULTest.case3' --verbose`。
5. 思考题自答：如果想验证「一次大 K」版本，应如何把 repeats=4、K=128 改写成 repeats=1、K=512？（提示：`RunTMATMUL_SPLIT_K` 的 numRepeats 传 1 时走的就是首轮 `TMATMUL` 分支；gen_data 侧改成 `repeats=1, k=512`。）

**需要观察的现象**：case3 改参后依然 PASSED——说明 4 次 K=128 的 TMATMUL_ACC 累加与 golden（numpy 逐段累加）一致。

**预期结果**：比对通过；若进一步做了 K=512 单次版本，其 golden 与累加版 golden 在 fp32 累加精度下应当几乎一致（元素级可能有极小浮点差异，gtest 容差内通过）。运行结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`TMATMUL_ACC(c, c, a, b)` 与 `TMATMUL_ACC(cOut, cIn, a, b)` 两个重载在 NPU 实现上的关系是什么？
**答案**：单 tile 版本纯粹是便利转发，直接以 `cMatrix` 同时充当输入和输出调用双 tile 版本（[include/pto/npu/a2a3/TMatmul.hpp:L182-L187](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TMatmul.hpp#L182-L187)）。双 tile 版本允许输入累加器与输出累加器在不同 tile 上（例如乒乓缓冲），是更一般的形式。

**练习 2**：为什么 bias 不设计成「矩阵乘完再用 TADD 加一个 tile」？
**答案**：功能上可以，但代价高：需要把 Acc tile 搬到 UB（一次跨存储搬移）、广播 bias 到 M 行、再跑一条 Vector 指令、可能还要搬回去。`TMATMUL_BIAS` 让 Cube 单元在 `mad` 内部直接从 `__biasbuf__` 读 bias 完成相加（见 L67-L68 的指针打包），一条指令、零额外搬移。

**练习 3**：CPU 仿真中 `TMATMUL_IMPL` 与 `TMATMUL_ACC_IMPL` 只差一个实参，是哪个？
**答案**：`acc` 指针。前者传 `static_cast<TileAcc*>(nullptr)`（不累加），后者传 `&cInMatrix`（[include/pto/cpu/TMatmul.hpp:L165-L175](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TMatmul.hpp#L165-L175)）。

### 4.3 Cube 约束

#### 4.3.1 概念说明

Cube 指令的约束比 Vector 指令多一层「物理落点」：不仅要类型对、形状对，操作数还必须躺在正确的存储层级上。这些约束分三类：

1. **类型三元组白名单**：`(CType, AType, BType)` 只有四组（A2/A3 上），因为 Cube 阵列只支持这些乘法组合。
2. **静态形状拼接**：`TileLeft::Rows == TileRes::Rows`、`TileLeft::Cols == TileRight::Rows`、`TileRight::Cols == TileRes::Cols`——即 M/K/N 在容量形状层面就要能拼上。
3. **运行期长度上限**：m/k/n 各自 ∈ [1, 4095]（`MMAD_MAX_SUPPORT_LENGTH`）。

分形布局约束（Left 非行主序+SFractal 行主序、Right 行主序+SFractal 列主序、Acc 非行主序）在 A2A3 的 static_assert 里不显式检查，但由 `TileLeft/TileRight/TileAcc` 别名的默认模板参数保证；A5 实现则显式检查（见 ISA 文档 A5 段落）。

#### 4.3.2 核心流程

约束的检查时机分布：

| 约束 | 检查方式 | 位置 |
| --- | --- | --- |
| dtype 三元组 | `static_assert`（编译期） | `CheckStaticMad` |
| TileType 位置（Left/Right/Acc） | `static_assert`（编译期） | `CheckStaticMad` |
| M/K/N 容量形状拼接 | `static_assert`（编译期，CPU 版亦有） | `CheckMadValid` / 文档 |
| bias 类型/位置/单行 | `static_assert`（编译期） | `TMATMUL_BIAS_IMPL` / `CheckBiasValid` |
| m/k/n ∈ [1,4095] | `PTO_ASSERT` / `assert`（运行期） | `CheckDynamicMad` |
| 分形布局（A5） | `static_assert`（编译期） | A5 实现（本讲不展开） |

数据通路与容量的直觉：L1 上的 Mat tile 容量最大（TLOAD 的落点）、L0A/L0B 其次、Acc 累加器数量最少——这决定了真机上 M/N 方向 tile 不能开太大，否则装不下（这也是 u5-l3 性能优化时 tile 形状扫描要扫的原因）。

#### 4.3.3 源码精读

**静态检查全集。** [include/pto/npu/a2a3/TMatmul.hpp:L77-L107](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TMatmul.hpp#L77-L107)：

```cpp
static_assert(  // 四组 dtype 三元组
    (int32_t, int8_t, int8_t) || (float, half, half) ||
    (float, float, float) || (float, bfloat16_t, bfloat16_t),
    "The data type is not supported.");
static_assert(TileLeft::Loc == TileType::Left, ...);   // 位置三连
static_assert(TileRight::Loc == TileType::Right, ...);
static_assert(TileRes::Loc == TileType::Acc, ...);
```

注意一个规律：**输出永远是「宽」类型**——s8×s8→s32、f16×f16→f32、bf16×bf16→f32。因为累加链长（K 可到 4095），窄类型中间和会溢出，硬件在累加器里用宽类型保存部分和。这与 ISA 文档 [docs/isa/TMATMUL.md:L57-L67](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TMATMUL.md#L57-L67) 列出的 A2A3 检查一致（文档同时指出累加器的精确行为与类型提升是 target/实现定义的，见 L24）。

动态上限在 [L99-L107](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TMatmul.hpp#L99-L107) 的 `CheckDynamicMad`，常量 `MMAD_MAX_SUPPORT_LENGTH = 4095` 定义在 [L17](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TMatmul.hpp#L17)——`mad` intrinsic 用 16 位传 m/k/n，硬件保留了一位做标记，所以可用上限是 4095 而非 65535。

**CPU 版的镜像检查。** [include/pto/cpu/TMatmul.hpp:L21-L48](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TMatmul.hpp#L21-L48) 的 `CheckMadValid` 复刻了类型三元组、形状拼接，并额外检查分形布局一致性（L38-L47 的注释解释了 CPU 仿真要兼容两种 Left tile 编码来源：手写别名的 tile 与 PTOAS 生成的显式声明 tile）。动态上限在 [L114-L126](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TMatmul.hpp#L114-L126)。两端 *_IMPL 签名逐字相同、按后端宏互斥编译——这是 u2-l4 「接口分叉在 common、实现按目录隔离」纪律的又一次体现。

**TileLeft/TileRight/TileAcc 别名的默认布局。** [include/pto/common/pto_tile.hpp:L1719-L1721](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1719-L1721)（TileLeft：ColMajor BLayout + SLayout::RowMajor）、[L1730-L1732](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1730-L1732)（TileRight：RowMajor + ColMajor 分形）、[L1760-L1762](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1760-L1762)（TileAcc）：别名屏蔽了架构间默认布局差异，用别名声明 Cube 操作数就自动满足布局约束——这是 u2-l2 讲过的「别名屏蔽架构差异」在 Cube 通路上的落地。

**AccPhase 是什么。** [include/pto/common/type.hpp:L234-L239](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/type.hpp#L234-L239) 定义了 `AccPhase`（Unspecified/Partial/Final），它作为可选模板参数透传给 `mad` 的第 7 个实参，用于告知硬件本次乘累加在 K 拆分中的阶段（对应底层 unit-flag 精度控制，具体行为 target 定义）。日常使用默认 `Unspecified` 即可。

**对齐取整的实践细节。** ST 用例 kernel 里 [tmatmul_kernel.cpp:L31-L34](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tmatmul/tmatmul_kernel.cpp#L31-L34) 把容量 M/N/K 向上对齐到 blockAlign（s8 是 32、其他 16），有效区保持原值（40/50/60）——「容量对齐硬件、有效区表达真实尺寸」是 Cube tile 声明的标准姿势，正好复习 u2-l2 的双轨制。

#### 4.3.4 代码实践

**实践目标**：亲手触发一次编译期约束失败，把「静态检查」从文字变成体验。

**操作步骤**：

1. 复制 `tests/cpu/st/testcase/tmatmul` 用例 kernel 中 `RunTMATMUL<float, half, half, float, 40, 50, 60, false>` 一行的调用，临时把 `TileAcc<float,...>` 的用法改坏：例如把 `AccTile cTile;` 的声明换成 `Tile<TileType::Vec, float, M, N, ...>`（或直接把 outType 改成 `half`）。
2. 重新编译：`python3 tests/run_cpu.py -t tmatmul --verbose`。

**需要观察的现象**：编译器在 static_assert 处报错，错误消息正是 `CheckStaticMad` 里的英文字符串（如 `"The data type is not supported."` 或 `"TileRes TileType must be set to TileType::Acc."`）。

**预期结果**：编译失败且报错指向 [include/pto/npu/a2a3/TMatmul.hpp:L83-L96](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TMatmul.hpp#L83-L96)（CPU 后端则指向 `CheckMadValid`）。改回后恢复正常。**注意**：这是破坏性实验，做完务必还原，不要把改动提交（本实践仅在本地临时修改验证）。

#### 4.3.5 小练习与答案

**练习 1**：m=k=n=4096 的 tile 能一次算完吗？
**答案**：不能。运行期 `CheckDynamicMad`（NPU 侧 PTO_ASSERT、CPU 侧 assert）要求 m/k/n 各自 ∈ [1, 4095]，4096 越界（`MMAD_MAX_SUPPORT_LENGTH = 4095`，[include/pto/npu/a2a3/TMatmul.hpp:L17](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TMatmul.hpp#L17)）。K 方向应拆成多段用 TMATMUL_ACC 累加；M/N 方向则拆成多个输出 tile。

**练习 2**：`TileLeft<half, M, K>` 的默认 BLayout 是什么？为什么和 `TileRight` 不同？
**答案**：TileLeft 默认 `BLayout::ColMajor` + `SLayout::RowMajor` 分形，TileRight 默认 `BLayout::RowMajor` + `SLayout::ColMajor` 分形（[include/pto/common/pto_tile.hpp:L1719-L1732](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1719-L1732)）。这是 L0A/L0B 硬件期望的数据摆放：Cube 阵列沿 K 方向取操作数，两种缓冲的分形方向相反才能让 K 维在物理上对齐拼接。

**练习 3**：int8×int8 的输出为什么是 int32 而不是 int8/int16？
**答案**：K 最多 4095 时，s8×s8 的部分和理论上可到 \( 127^2 \times 4095 \approx 6.6 \times 10^7 \)，超出 int16 范围；`static_assert` 的白名单只允许 `(int32_t, int8_t, int8_t)`，硬件累加器按 int32 累加，需要窄输出时再用 TCVT（u4-l4）转换。

## 5. 综合实践

**任务**：写一个属于你自己的 split-K mini-kernel（示例代码级别，不要求提交仓库），把本讲三个模块串起来。

要求：

1. 仿照 `RunTMATMUL_SPLIT_K` 的结构，实现 \( C_{128 \times 128} = \sum_{i=0}^{3} A_i \cdot B_i \)，其中每个 \( A_i, B_i \) 为 [128,128] 的 fp16 矩阵（即 K 拆 4 段，每段 128）。
2. 用 `TileLeft<half, 128, 128>`、`TileRight<half, 128, 128>`、`TileAcc<float, 128, 128>` 声明操作数，容量对齐到 16 的倍数（128 已满足）。
3. 主循环按「首轮 TMATMUL、后续 TMATMUL_ACC」的模式写，并在每轮 TLOAD→TMOV→TMATMUL 之间插入正确的 set/wait 事件对（参考 [tmatmul_kernel.cpp:L79-L87](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tmatmul/tmatmul_kernel.cpp#L79-L87) 的 MTE2→MTE1→M 链条；轮间依赖参考 u2-l3 的乒乓编号）。
4. 用 numpy 造 4 对随机矩阵，以 fp32 累加生成 golden（可直接模仿 [gen_data.py:L22-L49](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tmatmul/gen_data.py#L22-L49) 的 `matmul_reference` 与累加循环），在 CPU 仿真下比对。
5. 思考并记录：把 4 段合并成一次 K=512 的 TMATMUL，golden 完全相同吗？哪一种在真机上可能更快、为什么？（提示：拆段让每轮 L0A/L0B 占用更小、可与其他 tile 搭配做乒乓；合并减少指令数与累加器读写次数。这是 u5-l3 性能优化的引子。）

**验收标准**：CPU 仿真比对通过（gtest 容差内）；能口头说清 TMATMUL 与 TMATMUL_ACC 在 `cmatrixInitVal` 上的差异、三个操作数的 TileType 约束、以及 m/k/n 的来源与上限。运行结果待本地验证。

## 6. 本讲小结

- `TMATMUL` 是 Cube 单元（PIPE_M）上的矩阵乘，语义 \( C_{i,j} = \sum_k A_{i,k} B_{k,j} \) 定义在有效区内，M/K/N 分别取自 A 的 validRow/validCol 与 B 的 validCol。
- 变体族共享同一内层 `mad` intrinsic：`TMATMUL_ACC` 靠 `cmatrixInitVal=false` 实现累加（split-K 主循环的标准写法：首轮 TMATMUL、后续 ACC）；`TMATMUL_BIAS` 把 bias 指针打包进 C 指针高 32 位，一条指令完成「乘累加 + 加偏置」；`TGEMV` 系列是 M=1 的专用通路。
- 约束三层：编译期 dtype 三元组白名单与 TileType 位置（`CheckStaticMad`/`CheckMadValid`）、编译期 M/K/N 容量形状拼接、运行期 m/k/n ∈ [1, 4095]；输出类型必然「宽于」输入（s8→s32、f16→f32），因为长 K 累加需要宽部分和。
- 数据通路是 GM → L1（Mat tile，TLOAD）→ L0A/L0B（Left/Right tile，TMOV）→ 累加器（Acc，TMATMUL）→ GM/UB；跨流水线依赖用事件表达，TMATMUL 前等 MTE1 的牌、后给 PIPE_FIX 挂牌。
- CPU 仿真（`TMatmulNzZn` 三重循环 + FMA，`acc` 指针区分是否累加）与 NPU 实现（`mad` intrinsic + 指针地址空间翻译）的 *_IMPL 签名逐字相同、按后端宏互斥编译——「一处签名、两端实现」的分层纪律再次得到验证。

## 7. 下一步学习建议

- **下一讲 u5-l2（GEMM 基线）**：把本讲的单 tile 矩阵乘放进 M/N/K 三重分块循环，构成完整 GEMM 算子，并分析基线瓶颈。
- **u5-l3（高性能 GEMM）**：学习 double buffer 与事件编排如何让本讲的 TMATMUL 与 TLOAD/TMOV 重叠，理解「CUBE Bound / MTE Bound」判定。
- **延伸阅读**：`include/pto/npu/a5/TMatmul.hpp` 对比 A2A3 版的约束差异（fp8 组合、显式分形检查）；`docs/isa/TMATMUL_MX.md` 与 u5-l5 的 MX 混合精度矩阵乘（scale factor 每 32 个元素一组，CPU 实现见 `TMatmulMX` 的 `k / 32` 缩放逻辑）。
