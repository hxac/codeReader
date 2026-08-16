# u8-l1 Flash Attention：复杂算子的 tile 级实现

## 1. 本讲目标

本讲是单元八「复杂算子实战」的第一讲。前面七个单元里，我们已经把 PTO 的积木一块块拆过：GlobalTensor 视图（u2-l1）、Tile 编程模型（u2-l2）、事件同步（u2-l3）、TLOAD/TSTORE 搬运（u3-l1）、TMATMUL 矩阵乘（u5-l1）、规约指令（u4-l2）、双缓冲流水线（u6-l2）与性能方法论（u6-l3）。本讲把这些积木组装成一台完整的机器——Flash Attention（FA）算子。

学完本讲，你应该能够：

1. **掌握 online softmax 的 tile 化实现**：理解为什么 softmax 不能「先算完再归一化」，掌握 (global_max, global_sum) 流式递推的数学与代码。
2. **理解 Flash Attention 的分块 K/V 流水**：读懂 `compute_qk → compute_p → compute_pv → compute_gu` 四阶段流水线，理解 Cube 核与 Vector 核如何通过 GM FIFO 协作，理解 `qkPreloadNum` 预执行机制。
3. **对照 A2/A3 与 A5 两版实现差异**：理解 A5 上 `FIFO_MODE` 三种数据通路（GM 中转 vs UB 直达）与 softmax 宏的差异。

本讲所有代码引用均来自仓库真实文件，永久链接基于当前 HEAD `8aacb8e0`。

## 2. 前置知识

阅读本讲前，请确认你理解以下概念（不熟悉可回看对应讲义）：

- **注意力计算（Attention）**：给定查询 \( Q \in \mathbb{R}^{S_0 \times H} \)、键 \( K \in \mathbb{R}^{S_1 \times H} \)、值 \( V \in \mathbb{R}^{S_1 \times H} \)（H 为 HEAD_SIZE），单头注意力的输出是：

  \[ \text{QK} = QK^\top \in \mathbb{R}^{S_0 \times S_1}, \quad P = \operatorname{softmax}\!\left(\frac{\text{QK}}{\sqrt{H}}\right), \quad O = PV \in \mathbb{R}^{S_0 \times H} \]

  朴素实现需要把整个 \( S_0 \times S_1 \) 的注意力矩阵写进全局内存。当 \( S_0 = S_1 = 32768 \) 时，仅这个矩阵就是 2 GB（fp16），这就是 Flash Attention 要消灭的开销。

- **Tile 与流水线（u2-l2 / u6-l2）**：Tile 是片上固定形状 2-D 缓冲；MTE2（搬入）、MTE1（片上搬移）、M（Cube 计算）、V（向量计算）、MTE3/FIX（写回）是并行流水线，跨流水线依赖用 `(srcPipe, dstPipe, eventId)` 三元组事件表达。

- **Cube 核与 Vector 核**：昇腾 AI Core 内部同时有 Cube 单元（矩阵乘）和 Vector 单元（逐元素/规约）。A2/A3 上一个 AI Core 含 2 个向量子核（`VEC_CORES = 2`，见 [fa_performance_kernel.h:L26](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/fa_performance_kernel.h#L26)）。FA 的关键设计就是把 matmul 交给 Cube、把 softmax 交给 Vector，两边并行。

- **TPipe 跨核 FIFO（u3-l2）**：`TALLOC/TPUSH/TPOP/TFREE` 构成生产者-消费者环形 FIFO，是 Cube 与 Vector 之间传递 tile 数据的协议。

- **规约与广播互逆（u4-l2）**：`TROWMAX/TROWSUM` 把 2-D tile 折叠成列向量；`TROWEXPANDSUB/TROWEXPANDMUL/TROWEXPANDDIV` 把列向量广播回 2-D 做逐行运算。本讲的 online softmax 完全建立在这两组指令上。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `kernels/manual/common/flash_atten/README.md` | A2/A3 版 FA 的权威文档：公式、分阶段实现说明、流水线编排、性能表 |
| `kernels/manual/common/flash_atten/fa_performance_kernel.cpp` | A2/A3 版 kernel 主体：`compute_qk/compute_p/compute_pv/compute_gu` 四个阶段函数与 `runTFA` 主入口 |
| `kernels/manual/common/flash_atten/pto_macro_fa_softmax.hpp` | online softmax 宏：init 与流式两个实现 |
| `kernels/manual/common/flash_atten/pto_macro_fa_gu.hpp` | GU（running update）宏：\( O \leftarrow O \cdot \text{exp\_max} + PV \) 及最终归一化 |
| `kernels/manual/common/flash_atten/pto_macro_matmul.hpp` | Cube 侧 matmul 宏：L0A/L0B 乒乓 + `TMATMUL`，按 32 KiB 半区自动选 Cube_K |
| `kernels/manual/common/flash_atten/fa_performance_kernel.h` | 调优默认值：`kFaCubeS1=128`、`kFaTileS1=256`、`kFaQkPreload=4`、`kFaCvFifoSize=8` |
| `kernels/manual/a5/flash_atten/README.md` | A5 版说明（引用 common 文档）与 A5 特有优化概述 |
| `kernels/manual/a5/flash_atten/fa_performance_kernel.cpp` | A5 版 kernel：`FIFO_MODE` 三种数据通路、`TMPipe`、`UF_ENABLE=0` |
| `kernels/manual/a5/flash_atten/pto_macro_fa_softmax.hpp` | A5 版 softmax 宏：去掉 `pipe_barrier`、`TRESHAPE` 前置 |
| `demos/cpu/flash_attention_demo/flash_attention_demo.cpp` | CPU 仿真可运行的单 tile FA：完整指令链 + naive 参考实现，是本讲代码实践的主阵地 |
| `demos/torch_jit/flash_atten/README.md` | JIT FA 与 fused baseline 的性能对比表（加速比数据来源） |

## 4. 核心概念与源码讲解

FA 的整体结构可以先用一句话概括：**沿 S1（K/V 序列）方向把注意力切成 tile 流，每流入一个 tile 就增量更新一份逐行的 (max, sum, O) 状态，全程不落盘完整注意力矩阵**。下面按三个最小模块展开：4.1 讲「增量更新」的数学与实现（online softmax）；4.2 讲「tile 流」如何变成 Cube/Vector 四阶段流水（Q/K/V 分块流水）；4.3 讲同一算法在 A5 上的重新布线。

### 4.1 online softmax：从朴素 softmax 到流式递推

#### 4.1.1 概念说明

softmax 的定义是 \( p_{ij} = e_{ij} / \sum_j e_{ij} \)。它有两个致命特点：

1. **分母依赖整行**：算第 \( i \) 行的任何一个 \( p_{ij} \) 都需要先看完第 \( i \) 行全部 \( S_1 \) 个元素。朴素做法必须先物化整个 \( S_0 \times S_1 \) 矩阵再归一化——这正是 FA 要避免的。
2. **数值稳定**：\( e^{x} \) 在 \( x \) 稍大时就溢出（fp16 上超过 11 即 inf），实践必须先减去行最大值：\( e_{ij} = \exp(X_{ij} - M_i) \)。

online softmax 的思路是：把行最大值 \( M_i \) 和行和 \( S_i \) 当作**可修正的运行状态**，每来一个新 tile 就把它们更新一次，同时用缩放因子把旧的累积结果折算到新的基准下。这样任何时刻片上只需要保存当前 tile 和几条 [rows, 1] 的状态向量，内存占用从 \( O(S_0 S_1) \) 降到 \( O(S_0) \)。

#### 4.1.2 核心流程

设 scale \( s = 1/\sqrt{H} \)。对第 \( t \) 个 S1 tile（记其打分块为 \( X^{(t)} \)），递推分六步（与 [kernels/manual/common/flash_atten/README.md:L264-L321](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/README.md#L264-L321) 中的 Step 1–6 一一对应）：

1. **本 tile 行最大**：\( m_i = \max_j X^{(t)}_{ij} \)
2. **更新全局最大**：\( M_i = \max(M^{\text{prev}}_i,\, m_i) \)
3. **旧和缩放因子**：\( c_i = \exp\big(s \cdot (M^{\text{prev}}_i - M_i)\big) \) —— 注意 \( M^{\text{prev}}_i \le M_i \)，所以 \( c_i \le 1 \)，永不溢出
4. **本 tile 指数**：\( e_{ij} = \exp\big(s \cdot (X^{(t)}_{ij} - M_i)\big) \)
5. **本 tile 行和**：\( \ell_i = \sum_j e_{ij} \)
6. **更新全局和**：\( S_i = c_i \cdot S^{\text{prev}}_i + \ell_i \)

对应到输出侧，PV 累积也要同步缩放（这就是 4.2 里的 GU 阶段）：

\[ O_i \leftarrow c_i \cdot O^{\text{prev}}_i + E^{(t)}_i V^{(t)} \]

所有 tile 处理完后做最终归一化：\( O_i \leftarrow O_i / S_i \)。

伪代码：

```text
M, S ← -inf, 0 ; O ← 0
for t in 0..num_tiles-1:            # 沿 S1 流式
    X  ← QK^T 的第 t 个 tile        # Cube 阶段算出
    m  ← rowmax(X)
    M' ← max(M, m)                  # 新全局最大
    c  ← exp(s·(M - M'))            # 旧量折算到新基准
    E  ← exp(s·(X - M'))            # 本 tile 指数（存 fp16 喂给 PV matmul）
    ℓ  ← rowsum(E)
    S  ← c·S + ℓ
    O  ← c·O + E·V_tile             # GU 阶段
    M  ← M'
O ← O / S                           # 最后一个 tile 后归一化
```

#### 4.1.3 源码精读

**(1) CPU 仿真版：一条指令链看懂全貌**

先看最简版本。[demos/cpu/flash_attention_demo/flash_attention_demo.cpp:L235-L256](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/cpu/flash_attention_demo/flash_attention_demo.cpp#L235-L256) 用单个 tile（S=64, D=32）把整个 FA 链写成了一段直线代码：

```cpp
TLOAD(qTile, qGlobal);  TLOAD(kTile, kGlobal);  TLOAD(vTile, vGlobal);
TMOV(qLeft, qTile);  TTRANS(ktTile, kTile, kTile);  TMOV(kRight, ktTile);
TMATMUL(scoresAcc, qLeft, kRight);   // Q·K^T → Acc
TMOV(scores, scoresAcc);
TMULS(scores, scores, scale);        // × 1/√H

TROWMAX(rowMax, scores, scores);            // Step 1: 行最大
TROWEXPANDSUB(scoresCentered, scores, rowMax);
TEXP(expScores, scoresCentered);            // Step 4: 指数
TROWSUM(rowSum, expScores, expScores);      // Step 5: 行和
TROWEXPANDDIV(probs, expScores, rowSum);    // 最终归一化

TMOV(pLeft, probs);  TMOV(vRight, vTile);
TMATMUL(outAcc, pLeft, vRight);      // P·V
TSTORE(oGlobal, outAcc);
```

注意 [L247-L251](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/cpu/flash_attention_demo/flash_attention_demo.cpp#L247-L251) 这五行正是 4.1.2 递推的「一次性」版本：单个 tile 覆盖全部 S1，所以 `rowMax` 直接就是全局最大、`rowSum` 直接就是全局和，不需要修正项。**把这段代码里的「一步到位」换成「逐步修正」，就是性能版 kernel 的 online softmax。**

**(2) 性能版：init 模式**

性能版 kernel 的 softmax 在 [pto_macro_fa_softmax.hpp:L44-L96](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/pto_macro_fa_softmax.hpp#L44-L96)（第一个 tile，无历史状态可修正）：

```cpp
constexpr float scale = constexpr_inv_sqrt(HEAD_SIZE);   // 编译期算 1/√H
...
TROWMAX(new_global_max, input_x, tmp_float);             // Step 1+2：首 tile 局部最大即全局最大
TROWEXPANDSUB(p_tile_f32, input_x, new_global_max);      // X - M（广播相减）
TMULS(p_tile_f32, p_tile_f32, scale);                    // × s —— 注意 scale 在减法之后乘
TEXP(p_tile_f32, p_tile_f32);                            // Step 4：exp
TROWSUM(new_global_sum, p_tile_f32, tmp_float);          // Step 5：行和
TRESHAPE(p_tile_f32_1d, p_tile_f32);
TRESHAPE(x_exp_1d, x_exp);
TCVT(x_exp_1d, p_tile_f32_1d, RoundMode::CAST_ROUND);    // fp32 → fp16，喂给 PV matmul
```

两个细节值得注意：

- **scale 的位置**：代码先减最大值再乘 scale（`TROWEXPANDSUB` 之后 `TMULS(scale)`），与 README 公式 \( e_{ij} = \exp(s(X_{ij} - M_i)) \) 等价（\( s>0 \) 时先乘后减与先减后乘再取指数结果相同，但后者中间量更小、更稳）。
- **`tmp_float` 操作数**：`TROWMAX/TROWSUM` 都带一个与输入同形的 tmp tile，这是 NPU 上规约指令的通用契约（u4-l2 讲过：向量硬件没有 1:1 折叠 intrinsic，多步实现需要暂存）。

**(3) 性能版：流式（not_init）模式**

真正体现「online」的是 [pto_macro_fa_softmax.hpp:L98-L182](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/pto_macro_fa_softmax.hpp#L98-L182)，逐段对照 4.1.2 的六步：

```cpp
TROWMAX(local_max, input_x, tmp_float);                       // Step 1
TRESHAPE(tmp_shw_local_max, local_max);
TRESHAPE(tmp_shw_new_global_max, new_global_max);
TMAX(tmp_shw_local_max, tmp_shw_local_max, tmp_shw_new_global_max);  // Step 2: M = max(M_prev, m)
TRESHAPE(tmp_shw_exp_max, exp_max);
TSUB(tmp_shw_exp_max, tmp_shw_new_global_max, tmp_shw_local_max);    // M_prev - M（注意顺序）
TMULS(tmp_shw_new_global_max, tmp_shw_local_max, 1.0f);      // 拷贝新最大回 new_global_max
TMULS(tmp_shw_exp_max, tmp_shw_exp_max, scale);
TEXP(tmp_shw_exp_max, tmp_shw_exp_max);                      // Step 3: c = exp(s·(M_prev - M))
TROWEXPANDSUB(p_tile_f32, input_x, local_max);               // Step 4 前半：X - m
TMULS(p_tile_f32, p_tile_f32, scale);  TEXP(p_tile_f32, p_tile_f32);  // Step 4：e = exp(s·(X-m))
TROWSUM(local_sum, p_tile_f32, tmp_float);                   // Step 5: ℓ
TMUL(tmp_shw_new_global_sum, tmp_shw_new_global_sum, tmp_shw_exp_max); // Step 6 前半：c·S_prev
TADD(tmp_shw_new_global_sum, tmp_shw_new_global_sum, tmp_shw_local_sum); // Step 6: + ℓ
```

细心的读者会问：Step 4 里减的是 `local_max`（已更新为全局最大），不是旧最大——对，因为 `TMAX` 是原地更新 `tmp_shw_local_max`，此后 `local_max` 语义已经变成新全局最大 \( M_i \)。另外这里大量 `TRESHAPE` 是把 [rows, 1] 列向量重解释为 [1, rows] 行向量——`TMAX/TSUB/TMUL` 等逐元素指令在 1-D 上更不受布局约束（文件头注释 L28 也点明 2-D→1-D reshape 是为了绕开布局约束、让 cast 更快）。

**(4) 派发与 causal 掩码**

[pto_macro_fa_softmax.hpp:L184-L212](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/pto_macro_fa_softmax.hpp#L184-L212) 是统一入口 `pto_macro_fa_softmax`：按 `init` 模板参数二选一；若开启 `CAUSAL_MASK` 且当前 tile 完全落在因果上三角（`s1_index > s0_index`），则直接把 `x_exp` 清零、`exp_max` 置 1（`TMULS(x_exp, x_exp, 0.0)` 分支），跳过整段计算。对角 tile 内部则用 `TTRI` 生成上三角掩码乘 `-inf` 后加到打分上（[L63-L79](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/pto_macro_fa_softmax.hpp#L63-L79)）。

**(5) GU：输出侧的另一半递推**

online softmax 只维护了分母状态，输出 \( O \) 的累积缩放在 [pto_macro_fa_gu.hpp:L31-L48](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/pto_macro_fa_gu.hpp#L31-L48)：

```cpp
pto::TROWEXPANDMUL(prev_sv_tile, prev_sv_tile, exp_max);   // O ← c · O_prev（exp_max 逐行广播）
pto::TADD(prev_sv_tile, prev_sv_tile, est_sv_tile);        // O ← O + PV_tile
// last tile 版本多一步：
pto::TROWEXPANDDIV(prev_sv_tile, prev_sv_tile, new_global_sum);  // O ← O / S（最终归一化）
```

所有操作 `dst == src` 原地完成，UB 里只需常驻一份 `runningOTile`（文件头注释 L27-L28 点明设计意图：O 常驻 UB、避免额外 TLOAD/TSTORE）。

#### 4.1.4 代码实践

**实践目标**：在 CPU 仿真上跑通最小 FA，并验证 online softmax 递推与朴素 softmax 数值等价。

**操作步骤**：

1. 进入仓库根目录，运行 CPU demo（u1-l3 讲过的入口）：

   ```bash
   python3 tests/run_cpu.py --demo flash_attn --verbose
   ```

2. 观察输出中的 `max_abs_diff(pto, ref)`、`verify_allclose` 与 `perf:` 行。参考实现在 [flash_attention_demo.cpp:L127-L169](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/cpu/flash_attention_demo/flash_attention_demo.cpp#L127-L169)，它就是朴素三段式 softmax（先减 max、再 exp 求和、最后除分母）。

3. 打开 [flash_attention_demo.cpp:L247-L251](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/cpu/flash_attention_demo/flash_attention_demo.cpp#L247-L251)，把五行 softmax 链改写成「两段 tile」的 online 形式（示例代码，仅示意前半行，S=64 拆成两个 32 列 tile）：

   ```cpp
   // 示例代码：把 scores 按列拆两半，模拟两个 S1 tile 的递推
   // tile0: M0 = rowmax(s0), E0 = exp(s0-M0), S = rowsum(E0)
   // tile1: m1 = rowmax(s1), M1 = max(M0,m1), c = exp(M0-M1)
   //        E1 = exp(s1-M1), S = c*S + rowsum(E1)
   // 每步只用 TROWMAX/TMAX/TSUB/TEXP/TROWEXPANDSUB/TMUL/TADD/TROWSUM
   ```

   由于 demo 的 Tile 是编译期静态形状，最简单的做法是声明两个 `ScoresPlain` 的列切片 tile（`Tile<Vec, float, kS, kS/2, ...>`），对 GM 视图分两列段 TLOAD。

**需要观察的现象**：改写后 `max_abs_diff` 仍应在 `2e-4` 量级（fp32 下应更小），`bad=0`。

**预期结果**：递推版与一步到位版输出一致（差异仅来自浮点求和顺序）。若无法本地运行，请标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Step 3 的缩放因子 \( c_i = \exp(s(M_{\text{prev}} - M_i)) \) 永远不会上溢？

**答案**：因为 \( M_i = \max(M_{\text{prev}}, m) \ge M_{\text{prev}} \)，指数参数 \( s(M_{\text{prev}} - M_i) \le 0 \)，所以 \( 0 < c_i \le 1 \)。同理 Step 4 的 \( X_{ij} - M_i \le 0 \)，\( e_{ij} \le 1 \)。这正是「先减最大值」带来的数值稳定性。

**练习 2**：online softmax 状态里，哪一个量**不能**放进 fp16 累积？

**答案**：全局和 \( S_i \) 与运行输出 \( O \) 都应保持 fp32。\( S_i \) 随 tile 数增长（长序列下可达数百），fp16 超过 65504 即溢出；且逐 tile 缩放累加对精度敏感。kernel 中 `l2_global_sum`、`qk_tile_fifo`、`pv_tile_fifo`、`runningOTile` 全部为 float（见 [fa_performance_kernel.cpp:L694-L712](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/fa_performance_kernel.cpp#L694-L712)），只有喂给 Cube matmul 的 `x_exp` 才降为 half（matmul 输入必须 fp16，且 \( e_{ij} \le 1 \) 无溢出风险）。

**练习 3**：CAUSAL_MASK 开启时，`num_tiles_s1` 如何变化？

**答案**：见 [fa_performance_kernel.cpp:L758-L760](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/fa_performance_kernel.cpp#L758-L760)：`num_tiles_s1 = 1 + (block_idx * CUBE_S0) / Tile_S1`，即每个 Q 块只处理不超过自身行位置的对角块及以下部分，越界 tile 在 softmax 层被清零跳过（`TMULS(x_exp, x_exp, 0.0)`），计算量约减半。

### 4.2 Q/K/V 分块流水：四阶段 Cube/Vector 协作

#### 4.2.1 概念说明

online softmax 解决了「正确性」，分块流水解决「吞吐」。FA 的两条 matmul（QK 与 PV）是 Cube 擅长的，softmax 及其规约是 Vector 擅长的。若按朴素顺序执行，四个阶段完全串行，任何时刻只有一个单元在干活。

性能版 kernel 的做法是**把四个阶段拆成两条核上的流水，用 GM 环形 FIFO 衔接**（[fa_performance_kernel.cpp:L54-L58](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/fa_performance_kernel.cpp#L54-L58) 的注释就是这张图的文字版）：

```text
QK (Cube):  compute_qk  ──qk_tile_fifo (fp32, GM)──▶
P  (Vec):   compute_p   ──p_tile_fifo (fp16, GM)───▶
PV (Cube):  compute_pv  ──pv_tile_fifo (fp32, GM)──▶
GU (Vec):   compute_gu  ──▶ o_out（含最终归一化）
```

分块有三个层次（默认值见 [fa_performance_kernel.h:L19-L26](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/fa_performance_kernel.h#L19-L26)）：

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `CUBE_S0` | 每核 M 块（如 128） | 一个 Cube 块覆盖的 Q 行数，也是多核切分粒度 |
| `CUBE_S1` | 128 | 单次 Cube matmul 的 N/K 维块 |
| `TILE_S1` | 256 | 逻辑 tile 沿 S1 的长度，`kTileFactor = TILE_S1 / CUBE_S1` 个 Cube 子块 |
| `qkPreloadNum` | 4 | 预执行的 (QK,P) 深度 |
| `CV_FIFO_SIZE` | 8 | 三条 GM FIFO 的槽深度 |

多核切分沿 (B, N, S0/CUBE_S0)，S1 是规约轴不切分——每个核独立算完自己 Q 块的完整注意力，**零跨核通信**（[README:L469-L471](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/README.md#L469-L471)）。这符合 u6-l1 的「按输出归属切分」首选准则。

#### 4.2.2 核心流程

主循环（steady state）一个迭代的调度如下（对应 [fa_performance_kernel.cpp:L853-L911](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/fa_performance_kernel.cpp#L853-L911)）：

```text
对每个逻辑 tile_id（沿 S1）:
    next_qk_tile = tile_id + qkPreloadNum          # 预执行目标
    Cube:  compute_qk(next_qk_tile, sub_tile...)   # 领先生产 QK
    Vec:   compute_p(next_qk_tile, row_slice...)   # 消费老 QK、生产 P
    Cube:  compute_pv(tile_id, sub_tile...)        # 消费老 P、生产 PV
    Vec:   compute_gu(tile_id)                     # 消费老 PV、更新 runningO
进入循环前：warm-up 段先跑 qkPreloadNum 轮 (QK, P)
循环结束后：尾部 wait 全部事件 + pipe_barrier
```

Cube 侧内部还有一层流水：`pto_macro_matmul` 把 L1 里的 Mat tile 按 `Cube_K`（能塞进 32 KiB L0 半区的最大值，32/64/128/256 四档，见 [pto_macro_matmul.hpp:L81-L100](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/pto_macro_matmul.hpp#L81-L100)）切片 `TEXTRACT` 进 L0A/L0B 双缓冲，与 `TMATMUL` 交替重叠（[L157-L209](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/pto_macro_matmul.hpp#L157-L209)）——这正是 u6-l2 讲的事件驱动 double buffer 在宏内部的复用。

README 用三张 SVG 展示了 `qkPreloadNum` 的效果：preload=0 时四阶段完全串行；preload=2/4 时 QK/P 提前跑，Vector 单元得以填满（[README:L434-L451](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/README.md#L434-L451)）。仿真还指出当前瓶颈在 Cube 侧 TSTORE（[README:L459-L460](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/README.md#L459-L460)）——QK/PV 部分和都要经 GM FIFO 中转，这个观察直接引出 4.3 的 A5 改造。

#### 4.2.3 源码精读

**(1) compute_qk：Q 常驻 + K 流式**

[fa_performance_kernel.cpp:L235-L319](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/fa_performance_kernel.cpp#L235-L319) 是 QK 阶段。最核心的优化是 **Q tile 常驻 L1**：

```cpp
if (tile_id == 0 && sub_tile_id == 0) {
    TLOAD(qMatTile, qGlobal);      // Q 只在整个块的第一个 tile 加载一次
}
TLOAD(kMatTile, kGlobal);          // K 每个 sub_tile 都要换
```

（[L275-L279](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/fa_performance_kernel.cpp#L275-L279)）之后的事件对 `set_flag/wait_flag(PIPE_MTE2, PIPE_MTE1)` 保证 MTE1 搬移完成才发起 matmul，`qkMatTileEventId` 则保护 K tile 双缓冲的写覆盖。计算调用：

```cpp
pto_macro_matmul<Cube_S0, Cube_HEAD, Cube_S1>(qMatTile, kMatTile, qkAccTile, AccMode::InitFinalSum);
```

（[L285](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/fa_performance_kernel.cpp#L285)）`AccMode::InitFinalSum` 表示本子块初始化累加器且是最终求和（区别于 PV 阶段在 `kTileFactor` 个子块间累积的 `Init/Acc`）。结果写入 GM FIFO 的环形槽位（`buf_idx = tile_id % QKP_CV_FIFO`，[L297-L309](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/fa_performance_kernel.cpp#L297-L309)），最后一个子块 `TPUSH` 挂牌通知消费者。入口处的 `TALLOC` 与出口的 `TPUSH` 构成 FIFO 槽位生命周期（[L253-L255](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/fa_performance_kernel.cpp#L253-L255)、[L311-L313](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/fa_performance_kernel.cpp#L311-L313)）。

**(2) compute_p：向量子核切分 + softmax**

[fa_performance_kernel.cpp:L427-L551](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/fa_performance_kernel.cpp#L427-L551) 在 Vector 侧执行。两个关键机制：

- **行切分**：`Vec_S0 = Cube_S0 / VEC_CORES / kTileFactor`（[L438](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/fa_performance_kernel.cpp#L438)）。一个 `CUBE_S0 × TILE_S1` 的逻辑 tile 被 2 个向量子核 × `kTileFactor` 个行切片瓜分，每个切片独立跑一遍 softmax 递推、各自维护本核的 reduce 状态（`ReduceTileF_T` 覆盖 `Cube_S0 / VEC_CORES` 行，切片视图用运行期 TASSIGN 平移，[L481-L496](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/fa_performance_kernel.cpp#L481-L496)）。`get_subblockid()` 提供子核身份（[L445-L446](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/fa_performance_kernel.cpp#L445-L446)），这是天然的 SPMD。
- **数据进出**：`TPOP` 从 qk FIFO 取槽（[L453-L455](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/fa_performance_kernel.cpp#L453-L455)），按子块循环 TLOAD 到 UB 切片视图（[L464-L472](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/fa_performance_kernel.cpp#L464-L472)）；softmax 宏（4.1.3 已精读）在 [L500-L508](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/fa_performance_kernel.cpp#L500-L508) 被调用，`initFlag` 由 `tile_id == 0` 决定；产出的 fp16 `x_exp` 写回 p FIFO（[L514-L530](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/fa_performance_kernel.cpp#L514-L530)）。

**(3) compute_pv：第二 matmul 与跨子块累加**

[fa_performance_kernel.cpp:L325-L421](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/fa_performance_kernel.cpp#L325-L421)。P 从 FIFO 取（`TPOP`/`TFREE`），V 从 GM 加载，matmul 的累加模式按子块位置选择（[L386-L395](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/fa_performance_kernel.cpp#L386-L395)）：

```cpp
const AccMode accMode =
    (sub_tile_id == 0) ?
        (is_last_subtile || next_will_be_skipped ? AccMode::InitFinalSum : AccMode::InitPartialSum) :
        (is_last_subtile || next_will_be_skipped ? AccMode::AccFinalSum  : AccMode::AccPartialSum);
```

直觉：`kTileFactor` 个 `CUBE_S1` 子块共享一个 `TILE_S1` 逻辑 tile 的 PV 累加器，首子块 Init、其余 Acc，最后一个子块（或 causal 即将跳过时）标记 Final 直接触发写出——`TMATMUL_ACC<AccPhase::Final>` 把「累加完成」与「FIXPIPE 写出」折叠进一条指令（u5-l3 讲过的 UF 通路，由 [fa_performance_kernel.cpp:L25](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/fa_performance_kernel.cpp#L25) 的 `UF_ENABLE 1` 打开）。PV 结果 `TALLOC`+`TSTORE`+`TPUSH` 进 pv FIFO（[L405-L414](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/fa_performance_kernel.cpp#L405-L414)）。

**(4) compute_gu：合并与最终归一化**

[fa_performance_kernel.cpp:L553-L605](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/fa_performance_kernel.cpp#L553-L605)。第一个 tile 的 PV 直接作为 `runningOTile` 初值（`TLOAD(runningOTile, pvGlobal)`，[L573-L576](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/fa_performance_kernel.cpp#L573-L576)）；后续 tile 走 `pto_macro_fa_gu` / `pto_macro_fa_gu_last`（4.1.3 (5)）；最后一个 tile 完成后 `TSTORE(outGlobal, runningOTile)` 写出最终 O（[L596-L603](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/fa_performance_kernel.cpp#L596-L603)）。

**(5) runTFA：装配车间**

`runTFA`（[L607-L947](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/fa_performance_kernel.cpp#L607-L947)）负责装配：

- **片上缓冲规划**：L1 侧 Q/K/P/V Mat tile（[L685](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/fa_performance_kernel.cpp#L685)），UB 侧 softmax 全套 tile（[L713-L716](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/fa_performance_kernel.cpp#L713-L716)），均有 `static_assert` 容量检查（L1 ≤ 512 KiB、UB ≤ 192 KiB）。两个 Cube 累加器用 `assign_running_acc_tile` 在 `0x0 / 0x10000` 两个 L0C 半区乒乓（[L214-L229](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/fa_performance_kernel.cpp#L214-L229)、[L687-L689](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/fa_performance_kernel.cpp#L687-L689)），避免 QK 写与 PV 读在同一累加器上冲突。
- **三条 FIFO**：[L795-L806](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/fa_performance_kernel.cpp#L795-L806) 用 `TPipe` 模板定义，槽大小分别为 `Cube_S0×TILE_S1×4B`（QK, fp32）、`×2B`（P, fp16）、`Cube_S0×HEAD×4B`（PV, fp32），深度 8。方向宏 `DIR_C2V/DIR_V2C` 标明生产者→消费者。
- **首尾补同步**：[L761-L774](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/fa_performance_kernel.cpp#L761-L774) 预先 set 一轮事件、[L913-L929](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/fa_performance_kernel.cpp#L913-L929) 尾部补 wait——u6-l2 讲过的 InitSyncFlags/WaitSyncFlags 模式，防止真机首轮死等。
- **核号→通信槽**：块数超过 `kCvMaxCores`（25）时用 `TSYNC_CVID` 为多余块分配复用的 comm 槽位（[L724-L729](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/fa_performance_kernel.cpp#L724-L729)）。

**(6) host 侧**：`LaunchTFA` 在启动前先 `warmup_kernel<<<24>>>` 唤醒全部核，再用 `PTO_PREFETCH` 把 Q/K/V 预取进 L2（[L965-L983](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/fa_performance_kernel.cpp#L965-L983)）。

#### 4.2.4 代码实践

**实践目标**：用仓库自带的 FA cost model 脚本理解分块参数如何影响流水线周期，体会「Cube/Vector 双资源时间线」。

**操作步骤**：

1. 单点估算一个形状（脚本用法见 [README:L139-L144](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/README.md#L139-L144)）：

   ```bash
   cd kernels/manual/common/flash_atten
   python3 scripts/fa_cost_model.py --mode npu --soc Ascend910B3 \
     --head 128 --s0 4096 --s1 4096 \
     --cube-s0 128 --cube-s1 128 --tile-s1 1024 --qk-preload 4
   ```

2. 做 tiling 搜索（[README:L147-L156](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/README.md#L147-L156)）：

   ```bash
   python3 scripts/fa_cost_model.py --mode npu --all-socs --search --head 128 \
     --seq-list 1024,2048,4096,8192,16384,32768 \
     --cube-s0-list 64,128,256 --cube-s1-list 64,128 \
     --tile-s1-list 128,256,512,1024,2048 --qk-preload-list 2,4,6
   ```

3. 换一个非法组合验证合法性检查，例如 `--head 64 --s0 256 --s1 4096 --cube-s0 256 --cube-s1 64 --tile-s1 2048`：此时 `Vec_S0 = 256/(2×32) = 4`，FP32 向量切片 16 字节不满足 32 字节对齐，应被拒绝（[README:L122-L133](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/README.md#L122-L133)）。

**需要观察的现象**：搜索输出中每档序列长度给出的最优 `(cube_s0, cube_s1, tile_s1, qk_preload)` 组合及预测周期；短序列偏好小 `TILE_S1`、长序列趋向 `CUBE_S1=128`（README L106-L112 的 `extra_cube_s1_subtile` 修正项解释了原因）。

**预期结果**：S1=32768 附近的最优 `tile_s1` 明显大于 S1=1024 附近——短序列 tile 数少，逻辑 tile 同步开销占比高（`logical_tile_sync` 项）。绝对数值仅供参考，README 明确「用排名而非绝对时间做探索，落盘前仍需上板校准」。脚本无需 NPU 硬件即可运行；若本地缺少 numpy 等依赖请先安装，运行失败则标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Q tile 可以只在 `tile_id == 0 && sub_tile_id == 0` 时加载一次，而 K/V 每个 tile 都要加载？

**答案**：多核按 S0 切分后，每个核的 Q 块（`CUBE_S0 × H`）在整条 S1 流水期间不变，是「常驻数据」；K/V 沿 S1 流动，每个 tile 内容不同。Q 常驻让 GM 读流量从 \( O(S_0 \cdot S_1 \cdot H) \) 降为 \( O((S_0 + S_1) \cdot H) \)，这正是「tile 复用率」的来源之一。

**练习 2**：`qkp_tile_fifo_size` 与 `qkPreloadNum` 是什么关系？加大它们各自付出什么代价？

**答案**：README 说明 `qkp_tile_fifo_size = 1 + qkPreloadNum`（深度须容纳预执行深度），kernel 里两者分别由 `QK_PRELOAD` 与 `CV_FIFO_SIZE` 模板参数控制、默认 4 和 8。加大 `qkPreloadNum` 提升 Cube/Vector 重叠度，但线性增加 GM FIFO 缓冲占用与 warm-up 时延；加大 FIFO 深度提升抗背压能力，但同样线性增加 GM 中转缓冲（每槽 `CUBE_S0×TILE_S1×4B`）。

**练习 3**：GU 阶段为什么放在 Vector 而不是 Cube？

**答案**：GU 的核心指令是 `TROWEXPANDMUL/TADD/TROWEXPANDDIV`——逐元素乘加与逐行广播除法，属于 Vector 指令族；且 GU 消费的 PV 此刻刚由 Cube 写出、而最终 O 归一化又是逐行除法，放 Vector 可以让 Cube 专心做下一个 tile 的 QK/PV，两条流水都不空转（见 [README:L412-L418](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/README.md#L412-L418) 的四步描述）。

### 4.3 A5 差异：从 GM 中转到 UB 直达

#### 4.3.1 概念说明

4.2 的流水有一个结构性开销：QK 和 PV 的部分和都要从 Cube 的累加器（L0C）**写回 GM FIFO，再由 Vector 从 GM 读进 UB**——一进一出两次 GM 往返。仿真已经显示瓶颈在 Cube 侧 TSTORE（[README:L459-L460](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/README.md#L459-L460)）。

A5（Ascend 950 代，仓库内 `__DAV_C310__` 宏）的改造围绕两点：

1. **数据通路重构**：A5 的 TMOV 支持 L0C→UB 直达与 UB→L1 直达（可顺带做 ND2NZ 分形重打包），于是三条 FIFO 各自可以在「GM 中转」与「片上直达」之间选择。
2. **同步语义差异**：A5 向量核的指令间依赖由硬件顺序保证，softmax 宏里的 `pipe_barrier(PIPE_V)` 全部删除。

#### 4.3.2 核心流程

A5 kernel 用一个 `FIFO_MODE` 编译期开关描述三档通路（[kernels/manual/a5/flash_atten/fa_performance_kernel.cpp:L30-L54](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/flash_atten/fa_performance_kernel.cpp#L30-L54)）：

| 模式 | QK 通路 | P 通路 | PV 通路 |
| --- | --- | --- | --- |
| 0 `ALL_GM_PATH` | L0C→GM→UB（TSTORE/TLOAD） | UB→GM→L1 | L0C→GM→UB |
| 1 `ALL_UB_PATH` | L0C→UB（TMOV） | UB→L1（TMOV ND2NZ + TINSERT） | L0C→UB（TMOV） |
| 2 `QK_PV_UB_ONLY`（默认） | L0C→UB（TMOV） | UB→GM→L1（回退 GM） | L0C→UB（TMOV） |

模式 2 是默认值，注释标注为 "maximum utilization"：QK/PV 走片上直达消灭两次 GM 往返，P 通路因为 UB→L1 的 ND2NZ 重打包开销可能不划算而保留 GM 中转。注意 [L52-L54](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/flash_atten/fa_performance_kernel.cpp#L52-L54)：`#define FIFO_MODE 2`，且模式 1/2 强制 `UF_ENABLE == 0`（[L61-L64](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/flash_atten/fa_performance_kernel.cpp#L61-L64)）——UF（累加直写）通路与 TMOV 直达通路互斥，直达时由显式 TMOV 负责 L0C→UB。

A5 目录下还有一份 `fa_performance_dn_kernel.cpp`，是同一算法的「DN 布局」变体（P 以 NZ 分形直接供 Cube），三档 FIFO_MODE 语义相同。

#### 4.3.3 源码精读

**(1) TMPipe 与三种 FIFO 类型**

A2/A3 用 `TPipe`（单一 GM 环形 FIFO 机制）；A5 改用 `TMPipe`，按 `FIFOType` 分流（枚举定义在 [include/pto/common/fifo.hpp:L55-L60](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/fifo.hpp#L55-L60)：`GM_FIFO` / `VEC_FIFO` / `MAT_FIFO` / `CTRL_FIFO`）。QK 管道的两种形态（[kernels/manual/a5/flash_atten/fa_performance_kernel.cpp:L982-L995](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/flash_atten/fa_performance_kernel.cpp#L982-L995)）：

```cpp
#if USE_L0C_TO_DUAL_UB_PATH_QK
    using QKPipe = TMPipe<BUF0_QK_READY, FIFOType::VEC_FIFO, QKFiFoDepth, QKFiFoSyncT,
                          TileQKData, TileDataF_T, false, 0>;         // 直达：无需 GM 缓冲
    QKPipe qkPipe((uint32_t)(uint64_t)qkVecTile[0].data());           // 构造只喂 UB 地址
#else
    using QKPipe = TMPipe<BUF0_QK_READY, FIFOType::GM_FIFO, ...>;     // 中转：喂 GM 槽缓冲
    QKPipe qkPipe(qk_tile_fifo_block, (uint32_t)(uint64_t)qkVecTile[0].data());
#endif
```

直达模式下深度固定为 2（双缓冲），因为不再受 GM 槽数约束；P 管道的 `MAT_FIFO` 与 PV 管道的 `VEC_FIFO` 同理（[L998-L1023](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/flash_atten/fa_performance_kernel.cpp#L998-L1023)）。

**(2) softmax 宏差异**

对比两份 `pto_macro_fa_softmax.hpp`：

- A2/A3 版在每个依赖前一条结果的地方插 `pipe_barrier(PIPE_V)`（如 [common 版 L141-L143](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/pto_macro_fa_softmax.hpp#L141-L143)）；A5 版全部删除（[a5 版 L124-L136](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/flash_atten/pto_macro_fa_softmax.hpp#L124-L136) 一气呵成）。
- A5 版把所有 `TRESHAPE` 集中前置到计算前（[a5 版 L116-L122](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/flash_atten/pto_macro_fa_softmax.hpp#L116-L122)），指令序列更规整、利于硬件流水。

**(3) UF 关闭**

A5 kernel 第一处差异就是 [L19](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/flash_atten/fa_performance_kernel.cpp#L19) `#define UF_ENABLE 0`（common 版为 1）：累加器写出不再走「Final 相位直写 FIXPIPE」的捷径，改由 TMOV 显式搬运——这是为直达通路让路。

**(4) 文档对照**

A5 的 README 主体直接引用 common 版文档（[kernels/manual/a5/flash_atten/README.md:L92-L101](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/flash_atten/README.md#L92-L101)），性能表标注 TBD 待上板测量（[L78-L88](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/flash_atten/README.md#L78-L88)），A5 特有优化概括为「缓冲分配策略、CV 同步机制、流水深度参数」三点（[L103-L111](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/flash_atten/README.md#L103-L111)）——即本节所讲内容。这也印证 u1-l1 的判断：同一算子跨代实现共享算法与文档，差异集中在通路与同步。

#### 4.3.4 代码实践

**实践目标**：亲手确认 A2/A3 与 A5 两版 kernel 的差异面。

**操作步骤**：

1. 对比两份 softmax 宏（源码阅读型实践，无需硬件）：

   ```bash
   diff kernels/manual/common/flash_atten/pto_macro_fa_softmax.hpp \
         kernels/manual/a5/flash_atten/pto_macro_fa_softmax.hpp | less
   ```

2. 统计 `pipe_barrier` 出现次数：

   ```bash
   grep -c "pipe_barrier" kernels/manual/common/flash_atten/pto_macro_fa_softmax.hpp \
                          kernels/manual/a5/flash_atten/pto_macro_fa_softmax.hpp
   ```

3. 在 A5 kernel 中把 [L53](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/flash_atten/fa_performance_kernel.cpp#L53) 的 `FIFO_MODE 2` 临时改成 0（或用 `-DFIFO_MODE=0` 编译），重读 `TMPipe` 定义处展开的分支。

**需要观察的现象**：步骤 2 中 common 版计数大于 0、a5 版为 0；diff 显示 A5 版删除全部 `#if defined(__DAV_C220_VEC__)` 保护块并把 `TRESHAPE` 上移。

**预期结果**：确认「同步语义」与「数据通路」是两版仅有的两类实质差异，四阶段函数体的算法结构不变。修改 `FIFO_MODE` 只影响三条管道的构造，不影响 `compute_qk/p/pv/gu` 的调用序列。

#### 4.3.5 小练习与答案

**练习 1**：为什么模式 2（QK_PV_UB_ONLY）让 P 通路回退 GM，而不是全走模式 1？

**答案**：P 通路直达需要 `TMOV ND2NZ + TINSERT` 把 UB 上的行主序 fp16 重打包成 Cube 需要的 NZ 分形布局，重打包本身是开销；当 P tile 较小或 L1 布局已适配时，GM 中转（TSTORE/TLOAD 自带布局变换）反而更稳。默认注释 "maximum utilization" 表明实测模式下 2 综合最优——通路选择是实测问题，不是先验问题。

**练习 2**：A5 上 `UF_ENABLE` 为什么必须为 0（模式 1/2 时）？

**答案**：UF 的 `AccPhase::Final` 把「累加器写出」折叠进 `TMATMUL_ACC`，落点固定走 FIXPIPE→GM；而直达通路要求落点是 UB（由 TMOV L0C→UB 完成）。两者争夺同一份数据的搬运权，编译期互斥，[L61-L64](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/flash_atten/fa_performance_kernel.cpp#L61-L64) 用 `#error` 强制约束。

**练习 3**：如果要在 CPU 仿真（`__CPU_SIM`）下运行 FA kernel，会发生什么？

**答案**：跑不起来这套性能版——它依赖 `__DAV_C220__/__DAV_C310__` 核型宏、FFTS、`TPipe/TMPipe` 等真机机制，CPU 后端只覆盖单 tile 指令仿真。CPU 上验证 FA 逻辑应使用 `demos/cpu/flash_attention_demo`（4.1.4 的实践）或 `tests/cpu/st/testcase/tflashattn` ST 用例；跨核流水与 FIFO 时序必须上真机或仿真器验证——这是 u2-l3/u6-l2 反复强调的「CPU 验功能、真机验同步」纪律在本算子上的体现。

## 5. 综合实践

**任务：解释「序列越长、FA 相对 fused baseline 的加速比反而越低」的现象。**

数据来源是 [demos/torch_jit/flash_atten/README.md:L51-L72](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/torch_jit/flash_atten/README.md#L51-L72) 的对比表（910B2、aarch64 fused baseline）。取 S0=1024 一行：

| S1 | Fused µs | JIT µs | 加速比 | JIT 归一化 TFLOPS |
| --- | ---: | ---: | ---: | ---: |
| 1024 | 74.4 | 19.4 | 3.83× | 83.8 |
| 2048 | 73.1 | 26.2 | **2.79×** | 124.3 |
| 4096 | 75.0 | 43.1 | 1.74× | 151.1 |
| 8192 | 90.3 | 76.9 | 1.17× | 169.6 |

要求完成三件事：

1. **数据抽取**：从表中读出两个实现各自的时间随 S1 的增长规律（fused 几乎平坦：74→90 µs；JIT 近似线性：19→77 µs）。
2. **从 tile 复用率角度解释**，要点应包括：
   - JIT kernel 时间必须随 S1 线性增长——工作量本身是 \( O(S_0 S_1 H) \)，GOps 恒按 \( 4 S_0 S_1 H \) 计（两个 matmul × 每 MAC 2 FLOP），可用 [kernels/manual/common/flash_atten/README.md:L218-L225](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/README.md#L218-L225) 的 GOps 表验证：S0=1024、S1=1024、H=128 时 \( 4 \times 1024 \times 1024 \times 128 / 10^9 \approx 536.87 \) G，与表中该形状一行完全一致；
   - 与此同时 JIT 的**每 FLOP 效率在提升**（归一化 TFLOPS 84→170）：Q tile 常驻 L1 被 \( S_1 / \text{TILE\_S1} \) 个 tile 复用（4.2.3 (1)），warm-up/drain 与 GU 收尾被更多 tile 摊薄——这就是 tile 复用率随 S1 上升的直接证据；
   - fused baseline 时间平坦说明它在此形状区间被自身固定开销（多次 kernel 启动、注意力矩阵物化流水）主导，直到 S1=8192 才开始抬头（75→90）；
   - 于是加速比 = 平坦 / 线性 → 随 S1 压缩到 1.1× 附近。外推到 S1=32768（表未收录，可用 4.2.4 的 cost model 脚本 `--seq-list` 补测），JIT 时间继续线性增长而 fused 终将进入带宽区，比值进一步逼近 1——**FA 的本质收益不在小算力形状下的加速比，而在不物化 \( S_0 \times S_1 \) 矩阵带来的内存占用与带宽节省，以及长序列下持续爬升的归一化吞吐**。
3. **交叉验证**：对照 [kernels/manual/common/flash_atten/README.md:L200-L207](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/README.md#L200-L207) 的 kernel 版归一化 TFLOPS 表（1 核 38.27@S1=1024 → 172.86@S1=8192），确认「S1 越大效率越高」的结论在两份独立数据中一致；再引用 [README:L193-L196](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/README.md#L193-L196) 的三条官方总结作为答案骨架。

产出物：一页分析笔记，包含两张表的数据引用、tile 复用论证链、以及对「为什么 FA 值得做」的一句话结论。若你手头有 910B 硬件，可运行 `python fa_benchmark.py`（[demos/torch_jit/flash_atten/README.md:L8-L12](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/torch_jit/flash_atten/README.md#L8-L12)）复测并把 S1 扫到 32768；没有硬件则以本讲的「待本地验证」规范标注数据缺口。

## 6. 本讲小结

- **online softmax 是 FA 的算法内核**：把逐行 (max, sum) 与输出 O 变成可修正的运行状态，用缩放因子 \( c = \exp(s(M_{\text{prev}} - M)) \le 1 \) 折算历史，片上只需 \( O(S_0) \) 状态、全程不物化 \( S_0 \times S_1 \) 注意力矩阵；PTO 实现就是 `TROWMAX → TROWEXPANDSUB → TEXP → TROWSUM` 加修正项的组合。
- **四阶段跨核流水是 FA 的工程形态**：`compute_qk`(Cube) → `compute_p`(Vec) → `compute_pv`(Cube) → `compute_gu`(Vec) 靠三条 GM 环形 FIFO 衔接，`qkPreloadNum` 预执行打破级联依赖；多核沿 S0 切分、S1 是规约轴，零跨核通信。
- **Q 常驻 + K/V 流式是 tile 复用的关键**：Q tile 每个 \( \text{TILE\_S1} \) 块只加载一次，GM 读流量从三次方级降为平方级，直接解释了「S1 越大归一化吞吐越高」的性能曲线。
- **A5 与 A2/A3 同构不同路**：算法与四阶段结构完全一致，差异集中在 `FIFO_MODE` 三档数据通路（GM 中转 vs `TMOV` 片上直达）与同步语义（A5 删掉 softmax 中的全部 `pipe_barrier`、关闭 UF）。
- **本算子是前七单元的全量复习**：TMATMUL（u5-l1）、规约与广播（u4-l2）、事件双缓冲（u6-l2）、TPipe FIFO（u3-l2）、多核切分（u6-l1）、Bound 判定（u6-l3）全部在同一份 kernel 里出场。
- **验证纪律不变**：CPU 仿真（`flash_attention_demo`、ST 用例 tflashattn）只验功能；流水线时序、FIFO 深度、事件配对必须真机或仿真器验证；性能结论以 onboard 数据为准（README L196 明确 simulation 数字显著偏高）。

## 7. 下一步学习建议

- **下一讲 u8-l2**：MoE 通信算子（dispatch/combine 与 mega 融合）。FA 是「单卡内 Cube/Vector 协作」的极致，MoE 则引入跨卡通信与 token 重排，你将看到 TGet/TPut、原子累加与 tile 级流水（u7 系列知识）在真实大模型算子里的组合方式。
- **源码延伸阅读**（按收益排序）：
  1. `kernels/manual/common/flash_atten/README.md` 的 Cost Model 一节（L57-L177）——把本讲的流水线直觉公式化；
  2. `demos/cpu/mla_attention_demo`——另一种注意力变体（MLA）在同一套指令上的表达；
  3. `include/pto/npu/a5/TPush.hpp` 中 `TMPipe` 的实现（L805 起）——理解 VEC_FIFO/MAT_FIFO 直达通路的事件协议；
  4. `tests/cpu/st/testcase/tflashattn`——用 ST 用例视角再走一遍 FA 的功能验证。
- **动手方向**：若有 910B 环境，用 `bash run.sh --cases` 扫 `TILE_S1 ∈ {128,256,512}` 观察逻辑 tile 同步开销；若只有 CPU，把 4.1.4 的单 tile demo 改造成双 tile online 版本是最有价值的练习。
