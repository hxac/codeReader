# u5-l5 MX 混合精度矩阵乘：TMATMUL_MX 与 A5 性能实现

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 MX（microscaling）混合精度的核心思想：数据用 FP8/FP4 低精度存储，每 32 个连续元素共享一个 E8M0 缩放因子。
2. 掌握 `TMATMUL_MX` 指令族的 API 形态（基本 / ACC 累加 / BIAS / `TGEMV_MX`），以及它与普通 `TMATMUL` 在 dtype 白名单、K 对齐约束、缩放因子通路上的三点差异。
3. 读懂缩放因子（scale）的数据布局：GM 侧 `MX_A_ND` / `MX_B_DN` / `MX_A_ZZ` / `MX_B_NN` 布局、片上 `TileLeftScale` / `TileRightScale` 缓冲类型，以及 `GetScaleAddr` 地址绑定。
4. 能独立阅读 `kernels/manual/a5/matmul_mxfp4_performance` 性能算子，理解它的多核切分、L1 caching、scale 独立缓存与多级 double buffer 设计，并用利用率表判断瓶颈。

## 2. 前置知识

本讲站在 u5-l1（TMATMUL 与 Cube 单元）和 u5-l3（gemm_performance 优化）的肩膀上。先快速回顾，再补充本讲新概念。

### 2.1 你应该已经知道（来自前置讲义）

- **TMATMUL 指令族**（u5-l1）：`C = A·B` 定义在有效区内，数据通路为 GM → L1（`TileType::Mat`）→ L0A/L0B（`TileLeft`/`TileRight`）→ 累加器（`TileAcc`）→ 写回；NPU 底层是一条 `mad` intrinsic；ACC 变体靠 `cmatrixInitVal=false` 实现 split-K 累加。
- **四级流水与利用率表**（u5-l3）：`TLOAD → TEXTRACT → TMATMUL → TSTORE` 四级流水，靠 `(srcPipe, dstPipe, eventId)` 事件配对同步；性能调优以「TMATMUL/TEXTRACT/TLOAD/TSTORE Ratio」利用率为决策标尺。
- **Manual 模式缓冲管理**（u3-l2）：`TASSIGN` 把片上偏移绑给 Tile；scale 类 Tile 也走同一机制，但地址要经过 `GetScaleAddr` 编码——这是本讲的重点之一。

### 2.2 本讲新概念：混合精度与 MX

**为什么需要混合精度？** 大模型推理/训练的瓶颈常常不是算力而是带宽。把权重从 FP16 压到 FP8（1 字节）甚至 FP4（半字节），搬运字节数直接减半、再减半。但低精度格式动态范围极小——FP4 (e2m1) 只有 1 位指数，能表示的数值只有 ±{0, 0.5, 1, 1.5, 2, 3, 4, 6} 一档；真实张量里一行元素的最大值可能是最小值的几千倍，单一格式根本装不下。

**MX（microscaling，OCP MX 标准）的解法**：分块缩放（block-wise scaling）。把 K 维（或某维）上每 **32 个连续元素**划为一组，组内数据用低精度表示，组配一个 **E8M0 缩放因子**：

- **E8M0**：8 位纯指数格式（无尾数、无符号），字节值 \( e \) 表示缩放因子 \( 2^{e-127} \)。例如字节 127 表示 \( 2^0=1 \)，字节 130 表示 \( 2^3=8 \)；0xFF 是 NaN。这样每组先除以组内最大值对应的 2 的幂，把数据压进小格式能表示的范围，计算时再乘回来。
- **PTO 中的类型名**：数据侧 `float8_e4m3_t` / `float8_e5m2_t`（FP8，1 字节）、`float4_e2m1x2_t` / `float4_e1m2x2_t`（FP4，**两个元素打包在 1 字节里**，所以类型名带 `x2`）；缩放因子侧 `float8_e8m0_t`。

**MX 矩阵乘的数学含义**：不是「先反量化成 FP32 再乘」，等价形式是缩放因子直接进入点积：

\[
\mathrm{C}_{i,j} \;=\; \sum_{k=0}^{K-1} \Big(\mathrm{A}_{i,k}\cdot s_A[i,\lfloor k/32\rfloor]\Big)\cdot\Big(\mathrm{B}_{k,j}\cdot s_B[\lfloor k/32\rfloor,j]\Big)
\]

其中 \( s_A \) 是 `m×(K/32)` 的 E8M0 矩阵，\( s_B \) 是 `(K/32)×n`。硬件在 Cube 单元内部（`mad_mx`）完成「乘低精度数据 × 乘 2 的幂缩放」，全程不需要把数据展开回高精度——这正是 MX 的性能价值所在。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [docs/isa/TMATMUL_MX.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TMATMUL_MX.md) | TMATMUL_MX 的 ISA 文档：数学语义、C++ intrinsic、约束、Auto/Manual 示例 |
| [include/pto/common/pto_instr.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp) | 指令统一入口薄壳（TSYNC + MAP_INSTR_IMPL 转发） |
| [include/pto/cpu/TMatmul.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TMatmul.hpp) | CPU 仿真实现——`TMatmulMX` 是 MX 语义最权威的功能参考 |
| [include/pto/npu/a5/TMatmul.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a5/TMatmul.hpp) | A5 真机实现：`mad_mx` intrinsic 映射与 `CheckMadMxValid` 静态检查 |
| [include/pto/npu/a5/README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a5/README.md) | A5 指令实现目录说明（每条指令一个头文件，ST 测试在 tests/npu/a5） |
| [include/pto/common/constants.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/constants.hpp) | `MX_COL_LEN/MX_ROW_LEN/MX_BLOCK_SIZE` 等布局常量 |
| [include/pto/common/pto_tile.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp) | `MX_A_ND` 等 5 维布局、`TileLeftScale/TileRightScale` 缓冲类型 |
| [include/pto/npu/a5/utils.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a5/utils.hpp) | `GetScaleAddr`：scale tile 的地址编码 |
| [kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp) | 本讲主角：A5 上的 MXFP4 高性能 matmul kernel |
| [kernels/manual/a5/matmul_mxfp4_performance/scripts/gen_data.py](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/matmul_mxfp4_performance/scripts/gen_data.py) | 造数与 golden：MXFP4 打包、E8M0 反量化参考实现 |
| [kernels/manual/a5/matmul_mxfp4_performance/main.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/matmul_mxfp4_performance/main.cpp) | host 侧入口：文件尺寸计算（体现 fp4/32 分组）与校验 |
| [kernels/manual/a5/matmul_mxfp4_performance/README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/matmul_mxfp4_performance/README.md) | 算子说明、tiling 参数表、实测性能表 |
| [tests/cpu/st/testcase/tmatmul_mx/tmatmul_mx_kernel.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tmatmul_mx/tmatmul_mx_kernel.cpp) | CPU 仿真 ST 用例：最小单 tile 的 MX matmul 全流水 |

> 阅读提示：本算子 README 的 Specification 表沿用了 mxfp8 模板（Data Inputs 写作 `float8_e5m2`），目录布局小节也写成了 `matmul_mxfp8_performance/`。**实际类型以 kernel 源码与 gen_data.py 为准**：数据是 `float4_e2m1x2_t`（MXFP4），缩放因子是 `float8_e8m0_t`，输出 `bfloat16`。这本身就是个好教训——读 PTO 工程，源码永远优先于文档。

## 4. 核心概念与源码讲解

### 4.1 MX 指令语义：TMATMUL_MX 指令族

#### 4.1.1 概念说明

`TMATMUL_MX` 是 `TMATMUL` 的混合精度版本：在 `c, a, b` 三个操作数之外，多了 `aScaleMatrix` / `bScaleMatrix` 两个缩放因子 tile。指令族包括：

| 指令 | 语义 | 底层（A5） |
| --- | --- | --- |
| `TMATMUL_MX(c, a, aScale, b, bScale)` | `c = (A⊗s_A)·(B⊗s_B)`，c 清零起始 | `mad_mx`，`cmatrixInitVal=true` |
| `TMATMUL_MX(cOut, cIn, a, aScale, b, bScale)` | 累加形式，`cOut = cIn + ...`，split-K 用 | `mad_mx`，`cmatrixInitVal=false` |
| `TMATMUL_MX(c, a, aScale, b, bScale, bias)` | 带偏置，bias 打包进 C 指针高 32 位 | `mad_mx` 的 bias 通路 |
| `TGEMV_MX(c, a, aScale, b, bScale)` | M=1 的退化形式（向量 × 矩阵） | 同 `mad_mx`，`gemvCtrl=false` |

其中 ⊗ 表示按 32 元素分组广播乘（见 2.2 节公式）。A6 架构上还有 HiF4 变体（三级缩放 Ea/Eb/Ec），本讲末尾「下一步学习」会指路。

#### 4.1.2 核心流程

一条 `TMATMUL_MX` 从 API 到硬件的调用链（承接 u2-l4/u5-l1 的三层结构）：

```text
TMATMUL_MX(c, a, aScale, b, bScale, events...)
  │
  ├─ TSYNC(events...)                      # 折叠等待传入的事件
  ├─ MAP_INSTR_IMPL → TMATMUL_MX_IMPL      # 按后端宏路由（CPU / npu/a5 互斥）
  │     ├─ 取有效区 m = a.validRow, k = a.validCol, n = b.validCol
  │     ├─ CheckDynamicMmad(m, k, n)       # 运行期断言 ∈ [1, 4095]
  │     ├─ CheckMadMxValid<...>()          # 编译期静态检查（dtype/分形/容量）
  │     └─ [NPU] mad_mx(c, a, b, m, k, n, phase, gemvCtrl, biasCtrl, initVal)
  │         └─ 注意：scale 不作为指针传给 intrinsic！
  │            scale 通过 TASSIGN(scaleTile, GetScaleAddr(aTile.data()))
  │            提前绑定到 L0A/L0B 旁的 scale 存储区
  └─ return RecordEvent                    # 挂在 PIPE_M（Cube）流水线
```

关键认知：**scale 操作数走的是「地址绑定」而非「指令参数」通路**。指令签名里的 scale tile 主要用于编译期合法性检查与 Auto 模式推导；真机上 `mad_mx` 只收 `c/a/b` 三个指针，缩放因子由硬件从与 L0A/L0B 关联的 scale 区读取（4.2 节展开）。

#### 4.1.3 源码精读

**① 统一入口薄壳**——与其他 PTO 指令完全同构（TSYNC 等待 → 宏转发 → 返回 RecordEvent）：

[include/pto/common/pto_instr.hpp:576-583](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L576-L583) 这段是 `TMATMUL_MX` 基本形式的公共 API：先 `TSYNC(events...)` 折叠等待，再 `MAP_INSTR_IMPL(TMATMUL_MX, ...)` 转发到当前后端的 `TMATMUL_MX_IMPL`。同文件 L576-L637 还有 ACC / BIAS / 带 `AccPhase` 的全部重载，`TGEMV_MX` 在 L504-L575。

**② CPU 仿真实现——MX 语义的功能标准**：

[include/pto/cpu/TMatmul.hpp:139-163](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TMatmul.hpp#L139-L163) 这段 `TMatmulMX` 是整个指令最直白的语义定义：三重循环计算点积，其中

```cpp
// 每个 scale 因子作用于 32 个元素一组
double scaleFactor = scale0.GetElement(i, k / 32) * scale1.GetElement(k / 32, j);
mul_acc += src0.GetElement(i, k) * src1.GetElement(k, j) * scaleFactor;
```

注意两点：`k / 32` 整数除法正是「每 32 个 K 元素共享一个 scale」的体现；scale 元素先转 `double` 相乘再乘数据——CPU 仿真只求功能正确，不模拟硬件逐组缩放的时序。[include/pto/cpu/TMatmul.hpp:213-219](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/TMatmul.hpp#L213-L219) 是把它包成 `TMATMUL_MX_IMPL` 的薄包装。

**③ A5 真机实现——`mad_mx` intrinsic**：

[include/pto/npu/a5/TMatmul.hpp:60-73](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a5/TMatmul.hpp#L60-L73) 这段 `TMatmulMx` 把三个 tile 指针分别 cast 到 `__cc__`（L0C）、`__ca__`（L0A）、`__cb__`（L0B）地址空间，然后调用一条 `mad_mx` intrinsic——**scale 指针不出现**，验证了 4.1.2 的结论。[include/pto/npu/a5/TMatmul.hpp:75-89](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a5/TMatmul.hpp#L75-L89) 的 `TMatmulMxBias` 与普通 `TMatmulBias` 同款技巧：把 bias 指针打包进 C 指针高 32 位（`xd = c | (bias << 32)`），一条指令完成加偏置。

**④ 编译期契约检查 `CheckMadMxValid`**：

[include/pto/npu/a5/TMatmul.hpp:103-127](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a5/TMatmul.hpp#L103-L127) 这段静态检查约束了四件事：
- dtype 组合：`(isFp4 || isFp8) && C 是 float`（组合枚举见 [L91-101](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a5/TMatmul.hpp#L91-L101)）；
- `TileLeft::Cols % 64 == 0`：K 必须是 64 的倍数——因为 scale 列数 = K/32，而 MX 布局里 scale 以 2 列为最小单位（4.2 节的 `MX_COL_LEN=2`）；
- 分形摆放：Left 列主序 + SFractal 行主序、Right 行主序 + SFractal 列主序、Acc 列主序 + SFractal 行主序（与 `TileLeft`/`TileRight`/`TileAcc` 别名的默认布局一致）；
- 累加器容量：`Rows*Cols*sizeof(float) <= PTO_L0C_SIZE_BYTES`。

**⑤ `*_IMPL` 三兄弟**：

[include/pto/npu/a5/TMatmul.hpp:269-322](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a5/TMatmul.hpp#L269-L322) 这段依次是基本形式（`cmatrixInitVal=true`，C 清零起始）、累加形式（`cmatrixInitVal=false`，读入 cIn 续算）和 bias 形式（额外断言 bias 是 `float`、`TileType::Bias`、单行）。`TGEMV_MX_IMPL` 在 [L324-338](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a5/TMatmul.hpp#L324-L338)，与 `TMATMUL_MX_IMPL` 的唯一差别是 m 固定传 1 且 `gemvCtrl=false`（关闭 GEMV 优化的开关参数取反，见 L26 的 `GetGemvCtrl`）。

**⑥ TMATMUL_MX 与 TMATMUL 差异对照**（结合 [docs/isa/TMATMUL_MX.md:89-98](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TMATMUL_MX.md#L89-L98) 的 Constraints 与源码）：

| 维度 | TMATMUL（u5-l1） | TMATMUL_MX |
| --- | --- | --- |
| 操作数 | c, a, b | c, a, aScale, b, bScale（+bias） |
| A/B dtype | s8 / f16 / bf16 / f32 / fp8 / hifloat8 | 仅 FP8 四种组合与 FP4 四种组合 |
| C dtype | int32（s8 输入）或 float | **必须 float** |
| K 约束 | k ∈ [1,4095] 即可 | 额外要求 `Cols % 64 == 0` |
| 缩放因子 | 无 | E8M0 tile，经 `GetScaleAddr` 绑定到 L0 旁路 scale 区 |
| 底层 intrinsic | `mad` | `mad_mx` |
| 实现位置 | a2a3 / a5 / a6 | a5 首发（`include/pto/npu/a5/TMatmul.hpp`），a6 扩展 HiF4 |

#### 4.1.4 代码实践

**实践目标**：在 CPU 仿真下跑通 MX 指令的 ST 用例，观察它覆盖的 dtype 组合与流水骨架。

**操作步骤**：

1. 浏览用例目录 `tests/cpu/st/testcase/tmatmul_mx/`，确认有 `tmatmul_mx_kernel.cpp`、`main.cpp`、`gen_data.py`、`CMakeLists.txt` 四件套（u10-l1 会展开测试体系）。
2. 在仓库根目录执行（u1-l3 介绍的 CPU 路径唯一入口）：

   ```bash
   python3 tests/run_cpu.py -t tmatmul_mx --verbose
   ```

3. 打开 [tests/cpu/st/testcase/tmatmul_mx/tmatmul_mx_kernel.cpp:639-703](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tmatmul_mx/tmatmul_mx_kernel.cpp#L639-L703)，对照 `LaunchTMATMUL_MX<tilingKey>` 的 12 个分支：key 1-3 是 FP8（e5m2×e5m2、e4m3×e4m3、e4m3×e5m2），key 4-10 是 FP4（e2m1x2/e1m2x2 的各种组合与尾块形状），key 11-12 是 `TGEMV_MX`。
4. 再看 [tests/cpu/st/testcase/tmatmul_mx/tmatmul_mx_kernel.cpp:187-200](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tmatmul_mx/tmatmul_mx_kernel.cpp#L187-L200)：`TMATMUL_MX(cTile, aTile, aScaleTile, bTile, bScaleTile)` 的 bias/无 bias 两个分支，以及随后的 `TSTORE`。

**需要观察的现象**：gtest 输出中每个 tilingKey 一个 case，全部 `[  PASSED  ]`；`--verbose` 下能看到 cmake 增量构建与 `gen_data.py` 造数过程。

**预期结果**：CPU 仿真下单 tile 的 MX matmul 与 numpy golden 比对通过。具体打印文案**待本地验证**（我未在本环境执行）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `CheckMadMxValid` 要求 `TileLeft::Cols % 64 == 0`，而普通 `TMATMUL` 没有这条约束？

**答案**：MX 缩放因子按 32 个 K 元素一组共享，K 列对应 K/32 个 scale 列；而 MX 布局中 scale 的最小编址单位是 2 列（`MX_COL_LEN = 2`，见 4.2 节），所以 K/32 必须是偶数，即 K 是 64 的倍数。普通 TMATMUL 没有 scale 通路，K 只要落在 [1,4095] 即可。

**练习 2**：`TMATMUL_MX` 的累加形式和基本形式分别对应 `mad_mx` 的哪个控制位？这个设计在 split-K 循环里怎么用？

**答案**：对应 `cmatrixInitVal`：基本形式传 `true`（C 矩阵初值取 0），累加形式传 `false`（读入 C 的真实值续算）。split-K 时第一轮用基本形式、后续轮全用累加形式——见性能 kernel 的 `MatmulAcc`（[mxmatmul_performance_kernel.cpp:30-39](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp#L30-L39)）：`k == 0` 时 `TMATMUL_MX(cTile, ...)`，否则 `TMATMUL_MX(cTile, cTile, ...)`。

**练习 3**：`TGEMV_MX` 和 `TMATMUL_MX` 在 NPU 实现上的本质区别是什么？

**答案**：本质是同一条 `mad_mx`，区别只在 m 传 1 还是 `aMatrix.GetValidRow()`，以及 `gemvCtrl` 开关（[TMatmul.hpp:324-338](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a5/TMatmul.hpp#L324-L338) 中 `TMatmulMx<..., false, true, false>` 最后一个布尔参数）。`gemvCtrl=false` 告诉硬件走 M=1 的 GEMV 数据通路。

### 4.2 缩放因子布局：E8M0、MX 布局与 scale tile

#### 4.2.1 概念说明

MX 的缩放因子不是普通矩阵：它有自己的数据类型（E8M0）、自己的布局（`MX_A_*` / `MX_B_*`）、自己的片上缓冲类型（`TileLeftScale` / `TileRightScale`）和自己的地址编码（`GetScaleAddr`）。理解这条独立的「scale 通路」是看懂 MX kernel 的钥匙：

```text
GM (scaleA: E8M0, MX_A_ND/MX_A_ZZ 布局)
   │  TLOAD（与数据独立搬运）
   ▼
L1 (TileScaleA: TileType::Mat, 分形粒度 32 字节)
   │  TEXTRACT / TMOV（切片进 L0）
   ▼
L0 旁路 scale 区（TileLeftScaleCompact / TileRightScaleCompact，
   地址 = GetScaleAddr(L0A/L0B 数据地址)，右移 4 位编码）
   ▲
   │  硬件自动读取，不走指令参数
mad_mx
```

#### 4.2.2 核心流程

scale 从 GM 到 Cube 单元要跨三级布局：

1. **GM 级**：`scaleA` 物理上是 `[m, K/32]` 的 uint8（E8M0）矩阵，`scaleB` 因 B 是 DN（列主序）而物理转置为 `[n, K/32]`。逻辑上用 `Layout::MX_A_ND` / `MX_B_DN`（线性）或 `MX_A_ZZ` / `MX_B_NN`（分形）描述。
2. **5 维重排**：PTO 统一 5 维建模（u2-l1），MX 布局把 scale 重新切组——ND 布局下 `[m, c]` 变成 `[1, 1, m, c/2, 2]`（最内维是成对的 scale 字节）；ZZ 布局下变成 `[1, m/16, c/2, 16, 2]`，即 16 行 × 2 字节 = **32 字节一个分形块**。
3. **L0 级**：`TileLeftScaleCompact<X, M, K/32>` 的分形粒度是 `TileConfig::fractalMxSize = 32` 字节（对比数据 tile 的 `fractalABSize = 512` 字节），与 ZZ 的 32 字节块一一对应；绑定地址用 `GetScaleAddr(dataTile.data())` = `addr >> 4` 右移 4 位编码（代码事实；等价于以 16 字节为粒度的编码地址，具体格式属于目标定义）。

#### 4.2.3 源码精读

**① 布局常量**：

[include/pto/common/constants.hpp:39-41](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/constants.hpp#L39-L41) 定义了三个 MX 布局基石：`MX_COL_LEN = 2`（scale 最内成对）、`MX_ROW_LEN = 16`（ZZ 分形块 16 行）、`MX_BLOCK_SIZE = 32`（一组 scale 覆盖 32 个数据元素）。紧随其后的 L43-45 是 A6 HiF4 的对应常量（4/16/64）。

**② GM 侧 5 维布局定义**：

[include/pto/common/pto_tile.hpp:870-882](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L870-L882) 是 `Layout::MX_A_ND` 的 `TileShape2D`：把 `[rows, cols]` 重写为 `Shape<1, 1, rows, cols/2, 2>`，并 `static_assert` cols 必须能被 2 整除；配套的 stride 见 [include/pto/common/pto_tile.hpp:856-868](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L856-L868)（行距 = cols、对间距 = 2、对内 = 1）。B 侧的 `MX_B_DN` 在 [include/pto/common/pto_tile.hpp:1046-1074](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1046-L1074)，形状是 `[1, 1, cols, rows/2, 2]`（行列对调，因为 B 是列主序）。ZZ 分形变体 `MX_A_ZZ` 在 [L839-850](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L839-L850)：`Shape<1, rows/16, cols/2, 16, 2>`——正是 32 字节块。布局枚举整体定义在 [include/pto/common/type.hpp:163-175](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/type.hpp#L163-L175)。

**③ 片上 scale tile 类型**：

[include/pto/common/pto_tile.hpp:1739-1757](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1739-L1757) 定义了四个 scale 缓冲别名：`TileLeftScale` / `TileLeftScaleCompact`（`TileType::ScaleLeft`，行主序 + SFractal 行主序）、`TileRightScale` / `TileRightScaleCompact`（`TileType::ScaleRight`，列主序 + SFractal 列主序），分形粒度都是 `TileConfig::fractalMxSize`——[include/pto/common/pto_tile.hpp:1083-1085](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1083-L1085) 可见 `fractalMxSize = 32` 对 `fractalABSize = 512`。注意：**scale tile 的 Cols = K/32**，例如 [docs/isa/TMATMUL_MX.md:110-115](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TMATMUL_MX.md#L110-L115) 示例里 `A` 是 `TileLeft<float8_e5m2_t, 16, 64>`，`ScaleA` 是 `TileLeftScale<float8_e8m0_t, 16, 2>`（64/32 = 2）。

**④ 地址编码 `GetScaleAddr`**：

[include/pto/npu/a5/utils.hpp:81-86](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a5/utils.hpp#L81-L86) 就一行核心逻辑：`return addr >> SHIFT_MX_ADDR;`，`SHIFT_MX_ADDR = 4`（同文件 L19）。ISA 文档的 Manual 示例 [docs/isa/TMATMUL_MX.md:126-154](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TMATMUL_MX.md#L126-L154) 演示了标准用法：`TASSIGN(scaleA, GetScaleAddr(a.data()))`——把 scale tile 绑到与 `a` 关联的编码地址。CPU 仿真有逐字相同的实现（[include/pto/cpu/NPUMemoryModel.hpp:286-289](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/cpu/NPUMemoryModel.hpp#L286-L289)），保证双后端行为一致。Auto 模式下对应的指令是 `TGET_SCALE_ADDR`（见 ST 用例 [tmatmul_mx_kernel.cpp:173-176](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tmatmul_mx/tmatmul_mx_kernel.cpp#L173-L176) 在 `__PTO_AUTO__` 分支中调用）。

**⑤ 造数脚本——物理布局的 ground truth**：

[kernels/manual/a5/matmul_mxfp4_performance/scripts/gen_data.py:44-53](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/matmul_mxfp4_performance/scripts/gen_data.py#L44-L53) 生成数据与缩放因子：数据取 {-1,0,1} 的 FP4 值；scale 字节取 127~130（即缩放因子 \( 2^{0..3} \in \{1,2,4,8\} \)），并显式写出 E8M0 换算 `x1_scale = 2 ** (x1_scale_gm.astype(np.float32) - 127)`。[L55-59](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/matmul_mxfp4_performance/scripts/gen_data.py#L55-L59) 是 MX 反量化的参考实现：`x1[:, i] = x1_gm[:, i] * x1_scale[:, i // 32]`——`i // 32` 与 CPU 仿真里的 `k / 32` 完全一致。[L24-34](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/matmul_mxfp4_performance/scripts/gen_data.py#L24-L34) 的 `pack_two_fp4` 展示 FP4 打包：偶数下标元素放低 4 位、奇数放高 4 位，两个 FP4 共享 1 字节——这就是 kernel 里所有偏移都 `>> 1` 的原因。最后 [L66-73](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/matmul_mxfp4_performance/scripts/gen_data.py#L66-L73) 把 B 与 scaleB 转置（DN 布局）后落盘。

**⑥ host 侧的尺寸账**：

[kernels/manual/a5/matmul_mxfp4_performance/main.cpp:34-42](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/matmul_mxfp4_performance/main.cpp#L34-L42) 用三行算清了 MX 的压缩比：`aFileSize = m*k*sizeof(U)/2`（FP4 两元素一字节）、`aScaleFileSize = m*k/32*sizeof(X)`（32 元素一个 scale 字节）。对 m=2040, k=8192：A 数据 8.3 MB，scaleA 仅 0.52 MB——缩放因子开销只有数据的 1/16。

#### 4.2.4 代码实践

**实践目标**：用一个小脚本验证 E8M0 换算与 FP4 半字节顺序，为综合实践的布局图打好底。

**操作步骤**：

1. 在 `kernels/manual/a5/matmul_mxfp4_performance/scripts/` 目录下进入 Python 交互环境（需要 numpy、ml_dtypes、en_dtypes）：

   ```python
   import numpy as np
   import sys; sys.path.insert(0, ".")
   # 示例代码：手动复现 E8M0 与打包逻辑（不依赖 gen_data.py 内部）
   e = np.array([127, 128, 130], dtype=np.uint8)     # E8M0 字节
   print(2.0 ** (e.astype(np.float32) - 127))         # 期望 [1. 2. 8.]

   m, k = 2, 8
   x = np.array([[1, 0, 1, 0, 1, 0, 1, 0],
                 [0, 1, 0, 1, 0, 1, 0, 1]], dtype=np.uint8)  # 模拟 FP4 值
   flat = x.flatten()
   low  = (flat[1::2] & 0x0F) << 4    # 奇数下标 → 高 4 位
   high = flat[::2] & 0x0F            # 偶数下标 → 低 4 位
   packed = (low | high).reshape(m, k // 2)
   print(packed)                      # 期望 [[0x01, 0x01, ...], [0x10, 0x10, ...]]
   ```

2. 对照 [gen_data.py:24-34](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/matmul_mxfp4_performance/scripts/gen_data.py#L24-L34) 确认你的半字节顺序与脚本一致（脚本里 `matrix_high = matrix_bin[::2]` 取偶数下标留在低位）。

**需要观察的现象**：E8M0 字节 127/128/130 分别换算出 1/2/8；打包结果中偶数列元素落在每字节的低 4 位。

**预期结果**：与上面注释中的期望值一致。脚本行为可离线推理，具体打印**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`scaleA` 是 `[m, K/32]` 的 E8M0 矩阵。若 K=8192、m=2040，scaleA 占多少字节？它与 A 数据（MXFP4）的字节比是多少？

**答案**：scaleA = 2040 × (8192/32) = 2040 × 256 = 522,240 字节（约 0.5 MB）。A 数据 = 2040 × 8192 / 2 = 8,355,840 字节。比值 = (1/32) / (1/2) = **1/16**——scale 开销仅为数据的 6.25%。

**练习 2**：`MX_A_ZZ` 布局的一个分形块是多少字节？它为什么和 `TileLeftScale` 的 `fractalMxSize = 32` 对上？

**答案**：ZZ 块是 16 行 × 2 个 scale 字节 = 32 字节（`MX_ROW_LEN × MX_COL_LEN × 1B`）。`TileLeftScale` 的分形粒度就是按这个 L0 侧 scale 区的物理块大小设定的 32 字节，两者一致，`TMOV`/`TEXTRACT` 搬 scale 时整块搬运、无需重排。

**练习 3**：为什么 `TASSIGN(scaleTile, GetScaleAddr(aTile.data()))` 绑定的是「a 的地址」而不是 scale 自己的存储偏移？

**答案**：因为 L0 侧的 scale 存储区是 L0A/L0B 的**伴生区**（A6 文档称之为 L0AMX/L0BMX），硬件 `mad_mx` 从与 L0A/L0B 数据关联的 scale 区读缩放因子。`GetScaleAddr` 把数据 tile 的地址右移 4 位编码成 scale 区地址，从而把「哪个 scale 区」与「哪份数据」绑定起来；scale 的实际写入则由后续 `TEXTRACT`/`TMOV` 以该编码地址为目的地址完成。

### 4.3 A5 性能实现：matmul_mxfp4_performance

#### 4.3.1 概念说明

[include/pto/npu/a5/README.md:5-13](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a5/README.md#L5-L13) 说明了 A5 目录的组织：每条指令一个头文件，ST 测试在 `tests/npu/a5/src/st/`。MXFP4 性能算子则是把这些指令组装成完整流水线的样板工程，它要同时解决四个问题（README 的 Optimization Notes，[README.md:53-87](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/matmul_mxfp4_performance/README.md#L53-L87)）：

1. **多核切分**：32 核按 4×8 网格分输出，K 不切；
2. **base block 选择**：256×256×256，兼顾计算访存比与 512 字节对齐写回；
3. **L1 caching**：数据 `stepKa=stepKb=2` 批量搬运；**scale 与数据独立缓存**，比例参数 `mxScalePara=4`；
4. **多级 double buffer**：L1 数据、L1 scale、L0 数据、L0 scale 全部乒乓。

MXFP4 的性能逻辑：FP4 把搬运字节压到 FP16 的 1/4，算术强度（FLOP/字节）翻 4 倍，更容易把 Cube 单元喂饱——性能表里大形状下 TMATMUL Ratio 高达 90.7% 就是证据。

#### 4.3.2 核心流程

整个 kernel 的执行流程（自顶向下）：

```text
MxMatmulPerformance<<<32>>>                      # 32 核 SPMD 启动
 └─ RunMxMatmulDispatch                          # 核号 → (mIterIdx, nIterIdx)，处理 m/n 尾块
     └─ RunMxMatmulWithTail                      # 声明全部 tile 类型（动态有效区）
         ├─ InitGMOffsets                        # 按核算 GM 基址（fp4 偏移 >>1，scale 偏移 >>5）
         ├─ InitBuffers                          # L1 分级摆放 + L0 乒乓 + GetScaleAddr 绑定
         ├─ InitSyncFlags                        # 首轮补 set_flag（u5-l3 同款技巧）
         ├─ Compute                              # i/j 双层输出块循环 + kIter K 循环
         │   └─ ProcessKIteration
         │       ├─ TLOAD  数据 panel [256, 512]fp4 → L1（每 stepKa=2 轮一次）
         │       ├─ TLOAD  scale panel [256, 64] → L1（每 stepKscaleA=8 轮一次）
         │       └─ MacroMatmul
         │           ├─ TEXTRACT 数据 L1→L0A/L0B（按 kIter 切 baseK=256 片）
         │           ├─ TEXTRACT scale L1→L0 scale 区
         │           └─ TMATMUL_MX / TMATMUL_MX(acc)  # mad_mx
         ├─ StoreResult                          # TSTORE：L0C → GM（FIXPIPE，bf16）
         └─ WaitSyncFlags                        # 尾轮补 wait_flag
```

三组乒乓标志（`mte2DBFlag` / `mte2mxDBFlag` / `mte1DBFlag`）分别驱动 L1 数据、L1 scale、L0（数据+scale）的缓冲切换；数据与 scale 的搬运节奏不同步（2 轮 vs 8 轮），所以 L1 侧需要两个独立标志。

#### 4.3.3 源码精读

**① 常量与流水线心智模型**：

[mxmatmul_performance_kernel.cpp:16-28](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp#L16-L28) 定义了全 kernel 的关键常量：`SCALE_FACTOR = 32`（MX 分组）、`SHIFT_SCALE_FACTOR = 5`（>>5 即 /32，用于 scale 的 GM 偏移）、`L0_PINGPONG_BYTES = 32KiB`（L0A/L0B 乒乓半区上限）、`mxScalePara = 4`（scale 缓存批量放大系数）。注释里写明了四级流水的心智模型：TLOAD(GM→L1) → TEXTRACT(L1→L0) → TMATMUL_MX(Cube) → TSTORE(L0C→GM)。

**② 累加辅助函数**：

[mxmatmul_performance_kernel.cpp:30-39](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp#L30-L39) `MatmulAcc` 把「首轮清零、后续累加」的 split-K 惯用法封装成一行判断——`k == 0` 走基本形式，否则走 `TMATMUL_MX(cTile, cTile, ...)`。

**③ GM 偏移：fp4 与 scale 的两套换算**：

[mxmatmul_performance_kernel.cpp:64-82](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp#L64-L82) `InitGMOffsets` 用 `get_block_idx()` 算出本核负责的 `(mIterIdx, nIterIdx)` 后计算四个源地址和一个目的地址。注意三个移位：`gmOffsetA = (rowStart * k) >> 1`（FP4 两元素一字节）、`gmOffsetScaleA = (rowStart * k) >> SHIFT_SCALE_FACTOR`（32 元素一个 scale 字节）、`gmOffsetC = rowStart * n`（输出是 bf16，指针类型已含元素大小）。这一段是 4.2 节布局知识的直接应用。

**④ 缓冲初始化：L1 分级摆放 + scale 编码绑定**：

[mxmatmul_performance_kernel.cpp:89-123](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp#L89-L123) `InitBuffers` 先摆 L1：数据 panel 在前（`aMatTile` 两个乒乓各 `baseM*baseK*stepKa*sizeof(U)/2` = 64 KB），scale panel 在后（`baseM*baseScaleK*stepKscaleA` = 16 KB 每个）。再摆 L0：`aTile[0]=0x0`、`aTile[1]=0x0+L0_PINGPONG_BYTES`——每个乒乓半区 32 KiB，恰好装下一个 `[256,256]` 的 FP4 tile（256×256/2 = 32 KB）。最关键的四行在 [L119-122](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp#L119-L122)：`TASSIGN(aScaleTile[i], GetScaleAddr(aTile[i].data()))` 把 L0 scale tile 绑到对应乒乓半区的编码地址。

**⑤ K 迭代主体：三级流水的事件编排**：

[mxmatmul_performance_kernel.cpp:125-161](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp#L125-L161) `MacroMatmul` 是一轮 K 迭代的计算半段：先 `WaitFlag<PIPE_M, PIPE_MTE1>` 等 Cube 腾出当前 L0 乒乓槽，再对数据与 scale 各发一条 `TEXTRACT`（列偏移 `(kIter % stepK) * baseK` / `(kIter % stepKscale) * baseScaleK`），数据 panel 消耗完（`(kIter+1) % stepKa == 0`）就 `SetFlag<PIPE_MTE1, PIPE_MTE2>` 释放 L1 槽，最后 `SetFlag/WaitFlag<PIPE_MTE1, PIPE_M>` 交接 Cube 并调用 `MatmulAcc`。[L163-232](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp#L163-L232) 的 `ProcessKIteration` 是搬运半段：`kModstepKa == 0` 时构造 `[currentM, baseK*stepKa]` 的 GM 视图并 TLOAD 数据 panel；`kModstepKscaleA == 0` 时（每 8 轮）才 TLOAD scale panel——scale 量小，攒 8 轮一起搬，摊薄 DMA 次数。数据与 scale 的 GM 视图分别用 `Layout::ND/DN` 与 `Layout::MX_A_ND/MX_B_DN` 描述（[L178-183](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp#L178-L183)）。

**⑥ 输出与首尾补同步**：

[mxmatmul_performance_kernel.cpp:234-253](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp#L234-L253) `StoreResult` 用 `PIPE_M → PIPE_FIX` 事件对把 cTile 交给 FIXPIPE 写回（L0C 的 float 累加值在 TSTORE 通路转 bf16）。[L255-271](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp#L255-L271) 的 `InitSyncFlags`/`WaitSyncFlags` 是 u5-l3 见过的技巧：循环里的事件是「先 wait 后 set」的反向配对，首轮前补 set、尾轮后补 wait，保证事件记录一次、等待一次。

**⑦ 尾块处理与 tile 类型声明**：

[mxmatmul_performance_kernel.cpp:273-330](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp#L273-L330) `Compute` 的 i/j 循环里，`currentM = (i == loopsM-1 && remM > 0) ? remM : baseM` 处理非对齐尾块——所有 tile 都以动态有效区（`-1, -1` 模板参数）重新构造，正是 u2-l2 讲过的「容量静态、有效区动态」机制在性能算子里的用法。tile 类型集中在 [L354-380](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp#L354-L380)：L1 数据 tile（`TileType::Mat`，512 字节分形）、L1 scale tile（**32 字节分形**）、L0 的 `TileLeftCompact`/`TileRightCompact` 与 `TileLeftScaleCompact`/`TileRightScaleCompact`、累加器 `TileAccCompact<float, baseM, baseN>`（L0C 上一定是 float）。

**⑧ 入口与启动配置**：

[mxmatmul_performance_kernel.cpp:431-465](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp#L431-L465) `MxMatmulPerformance` 是 `__global__` 入口，把 uint8 指针 reinterpret 成 `bfloat16_t`（输出）、`float4_e2m1x2_t`（数据）、`float8_e8m0_t`（scale）；`LaunchMxMatmul` 给出默认配置：`blockDim=32`，`m=2040, k=8192, n=8100`，`singleCoreM=512, singleCoreN=1024`（mIter=4、nIter=8），`baseM=baseK=baseN=256`，`stepKa=stepKb=2`。这些数与 [README.md:70-87](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/matmul_mxfp4_performance/README.md#L70-L87) 的 tiling 参数表一一对应。

**⑨ 性能表判读**（承接 u5-l3 的方法论）：

[README.md:89-102](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/matmul_mxfp4_performance/README.md#L89-L102) 的实测表显示：`2048³` 小形状下 TMATMUL Ratio 只有 44.7%（流水线没喂饱，尾块与启动开销占比高）；`2048×8192×8192` 大形状下 TMATMUL 90.7%、TEXTRACT 88.1%、TLOAD 45.8%、TSTORE 4.6%——Cube 利用率接近饱和，瓶颈在 Cube 与 L1→L0 切片（TEXTRACT），TSTORE 因输出相对输入极小（bf16 输出 vs 1/4 字节的 FP4 输入）而几乎不构成压力。这与「MXFP4 提高算术强度」的预期完全吻合。

#### 4.3.4 代码实践

**实践目标**：通过源码阅读与参数推算，验证缓冲布局的容量账，并预测一处参数修改的影响。（本算子仅面向 A5 真机，CPU 仿真无对应 demo 入口，故本实践为源码阅读型。）

**操作步骤**：

1. **数乒乓**：在 [mxmatmul_performance_kernel.cpp:89-123](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp#L89-L123) 中数出 `BUFFER_NUM=2` 的乒乓组数（应为 5 组：aMat、bMat、aScaleMat、bScaleMat、以及 L0 侧共用 `mte1DBFlag` 的 aTile/bTile/aScaleTile/bScaleTile）。
2. **算容量**（由模板参数推算，非源码注释）：
   - L1 数据：`aMatTile` 每个乒乓 `256×256×2/2 = 64 KB`，两组（A、B）共 `64×2×2 = 256 KB`；
   - L1 scale：`aScaleMatTile` 每个 `256×8×8 = 16 KB`，两组共 `64 KB`；
   - L0 数据：`aTile` 每个乒乓半区 `256×256/2 = 32 KB`，正好顶满 `L0_PINGPONG_BYTES`。
3. **预测修改**：把 `LaunchMxMatmul` 中的 `m=2040` 改为 `m=2048`、`n=8100` 改为 `n=8192`（均为对齐值），预测 `RunMxMatmulDispatch` 中 `m % singleCoreM != 0` 与 `n % singleCoreN != 0` 的尾块分支（[L419-424](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/matmul_mxfp4_performance/mxmatmul_performance_kernel.cpp#L419-L424)）不再触发，`Compute` 中所有 `currentM/currentN` 恒等于 `baseM/baseN`。
4. 若有 A5 环境（CANN ≥ 8.5，`source ${ASCEND_INSTALL_PATH}/bin/setenv.bash`），按 [README.md:104-129](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a5/matmul_mxfp4_performance/README.md#L104-L129) 走 `python3 scripts/gen_data.py` → `bash run.sh -r npu -v Ascend950` 验证。

**需要观察的现象**：真机运行成功打印 `test success`；改成对齐形状后性能应不劣于 `m=2040, n=8100` 的参考行（0.3773 ms），因为尾块分支与动态有效区的开销消失。

**预期结果**：容量账与步骤 2 的数字一致；对齐修改的功能与性能结论**待本地（真机）验证**。

#### 4.3.5 小练习与答案

**练习 1**：数据 panel 每 `stepKa=2` 轮 K 迭代搬一次，scale panel 每 `stepKscaleA=8` 轮才搬一次。为什么节奏可以不同？`mxScalePara=4` 在其中的作用是什么？

**答案**：因为 scale 与数据体积比是 1/16（FP4：数据 1/2 字节/元素，scale 1/32 字节/元素），缓存同样 K 跨度时 scale 占用小得多，可以攒更多轮一起搬，摊薄 DMA 启动开销。`stepKscaleA = stepKa × mxScalePara = 2×4 = 8`，即 scale 的缓存跨度是数据的 4 倍（覆盖 8×256 = 2048 个 K 元素），`mxScalePara` 就是这个放大系数。

**练习 2**：性能表中 `m=2048, k=8192, n=8192` 一行 TSTORE Ratio 只有 4.6%，而 `2048³` 一行是 25.6%。为什么大 K 会让 TSTORE 占比下降？

**答案**：K 增大只增加计算与输入搬运量，不改变输出大小（输出由 m×n 决定）。K 从 2048 涨到 8192，Cube 与 TLOAD/TEXTRACT 的工作量约涨 4 倍，而 TSTORE 不变，占比自然被稀释。这也说明该算子在 K 维深时几乎不受写回带宽约束。

**练习 3**：`Compute` 中为什么每个 `(i,j)` 输出块循环内都要重新构造一遍所有 tile（`aMatTile[buf] = TileMatA(currentM, baseK*stepKa);` 等），而不是循环外构造一次？

**答案**：因为尾块的存在：最后一个 i/j 块的 `currentM/currentN` 可能小于 `baseM/baseN`（如 m=2040 时最后的块只有 248 行），tile 的动态有效区必须随之更新，让 TLOAD 的搬运量、TEXTRACT 的切片、TMATMUL_MX 的 m/n 都收缩到有效区内——这是「容量形状静态、有效区动态」（u2-l2）保证尾块不越界的机制。

## 5. 综合实践

**任务**：对照 [docs/isa/TMATMUL_MX.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TMATMUL_MX.md) 与 `kernels/manual/a5/matmul_mxfp4_performance`，画出一轮 K 迭代中 MX matmul 输入（数据 + scale）从 GM 到 L0 的完整内存布局图，标注每段的形状、类型与字节大小。

**参考答案**（以默认配置 m=2040, k=8192, n=8100、baseM=baseK=baseN=256 为例，取某个内部满块 i、j）：

```text
GM（物理布局，gen_data.py 落盘格式）
┌───────────────────────────────────────────────────────────────────────┐
│ A 数据: [2040, 8192] float4_e2m1x2_t（ND 行主序，2 元素/字节）          │
│   本轮 panel: 行 [i*256, i*256+256) × 列 [kIter*512, +512)             │
│   = [256, 512] 元素 → 256×512/2 = 64 KB                                │
│ scaleA: [2040, 256] float8_e8m0_t（MX_A_ND，逻辑 5 维 [1,1,2040,128,2]）│
│   本轮 panel: [256, 64] 字节（覆盖 64×32 = 2048 个 K 元素）= 16 KB      │
│ B 数据: 物理转置后 [8100 行视角, 8192]（DN 列主序）→ panel [512, 256]    │
│ scaleB: [n, 256] E8M0（MX_B_DN）→ panel [64, 256] = 16 KB              │
└───────────────────────────────────────────────────────────────────────┘
        │ TLOAD（每 2 轮搬数据 / 每 8 轮搬 scale，各自乒乓）
        ▼
L1（TileType::Mat；数据 512B 分形，scale 32B 分形）
  aMatTile[2]  各 [256, 512]fp4 = 64 KB   ← mte2DBFlag 切换
  bMatTile[2]  各 [512, 256]fp4 = 64 KB
  aScaleMatTile[2] 各 [256, 64] = 16 KB   ← mte2mxDBFlag 独立切换
  bScaleMatTile[2] 各 [64, 256] = 16 KB
        │ TEXTRACT（切出本轮 baseK=256 的片）
        ▼
L0（乒乓半区各 ≤ 32 KiB，mte1DBFlag 切换）
  L0A  aTile      [256, 256] fp4 = 32 KB ──┐
  L0A↗ aScaleTile [256, 8]   E8M0          │ GetScaleAddr(aTile.data()) = addr>>4
  L0B  bTile      [256, 256] fp4 = 32 KB ──┤ 绑定到 L0A/L0B 的伴生 scale 区
  L0B↗ bScaleTile [8, 256]   E8M0          │ （scale 列数 = 256/32 = 8）
                                           ▼
                    mad_mx: c[i,j] += Σ (A·sA)·(B·sB)   ← 每 32 个 k 共享一组 scale
                    L0C: TileAccCompact<float, 256, 256> = 256 KB
                           │ TSTORE（FIXPIPE: fp32 → bf16）
                           ▼
                    GM c[2040, 8100] bfloat16（ND）
```

检查要点：任一阶段的 scale 列数 = 该阶段覆盖的 K 元素数 ÷ 32——L1 scale panel 覆盖 8 轮 baseK（8×256 = 2048 个 K 元素）故 64 列，L0 每轮只切出 baseK=256 对应的 8 列；所有 FP4 字节数都做了 /2，所有 scale 字节数都做了 /32。若你画出的图满足这三条自检，说明布局已经吃透。

## 6. 本讲小结

- MX（microscaling）用「FP8/FP4 数据 + 每 32 元素一个 E8M0（\( 2^{e-127} \)）缩放因子」在 1/2~1/4 字节每元素的存储下保住动态范围；scale 开销仅为 MXFP4 数据的 1/16。
- `TMATMUL_MX` 是 `TMATMUL` 的混合精度版本：C 必须 float、A/B 只允许 FP8/FP4 的各四种组合、K 必须是 64 的倍数；CPU 仿真的 `TMatmulMX`（`scale0.GetElement(i, k/32) * scale1.GetElement(k/32, j)`）是语义的权威定义。
- scale 走独立通路：GM 侧 `MX_A_ND/MX_B_DN/MX_A_ZZ/MX_B_NN` 布局（5 维重排，最内成对、ZZ 为 32 字节块），片上 `TileLeftScale/TileRightScale`（32 字节分形），经 `TASSIGN(scale, GetScaleAddr(data))`（地址右移 4 位）绑到 L0 伴生 scale 区——`mad_mx` 不收 scale 指针。
- `matmul_mxfp4_performance` 用四级流水 `TLOAD → TEXTRACT → TMATMUL_MX → TSTORE` 组织 MXFP4 GEMM：32 核 4×8 切分、256³ base block、数据 stepK=2 与 scale `mxScalePara=4` 的差异化 L1 caching、五组乒乓缓冲、动态有效区处理 2040/8100 这类非对齐尾块。
- 性能判读沿用 u5-l3 的利用率表：大 K 形状下 TMATMUL Ratio 90.7%（Cube 近饱和）、TSTORE 仅 4.6%——MXFP4 通过压缩输入字节把算子推向 Cube Bound。

## 7. 下一步学习建议

- **下一讲 u6-l1（多核编程与 SyncAll）**：本讲 32 核 SPMD 切分只用了「各核输出独立」的零同步方案；当输出需要跨核规约时就要引入核间同步原语，u6 单元会展开。
- **A6 HiF4 扩展**：读 [docs/isa/TMATMUL_MX_HIF4.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TMATMUL_MX_HIF4.md) 与 [docs/isa/TQUANT_HIF4.md](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TQUANT_HIF4.md)，看三级缩放（Ea/Eb/Ec、64 字节 patch）如何把 MX 的「一组一个 scale」细化到子组，以及 `include/pto/npu/a6/` 的实现——这也是 u11-l2 架构适配一讲的原型素材。
- **量化指令对照**：回看 u4-l4 的 `TQUANT/TDEQUANT` 与 `include/pto/cpu/TQuant.hpp` 中 `ComputeMxScalingFromExponent`（E8M0 → 浮点换算，注意 TQUANT 用的是 2 的幂的倒数方向），思考「训练后量化产出 MX 数据 + scale」的完整链路如何用 PTO 指令拼出来。
- **性能手段归纳**：把本讲的 L1 caching、多级乒乓、尾块处理与 u5-l3 的 gemm_performance 对照列表，到 u6-l3（性能分析与优化方法论）时你会得到一张完整的调优清单。
