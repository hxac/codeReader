# 数值精度总账：bf16 状态、ex2/tanh 近似与量化点

## 1. 本讲目标

前面几讲我们分别拆了 Kernel 1 的计算阶段、16×16 求逆、Kernel 2 的 MMA 各相位与状态 I/O。本讲把散落在两个 kernel 里的**数值精度决策**收拢成一本"总账"。读完本讲你应该能够：

1. 拿着 kernel 源码，逐处标出**fp32 保持点**（哪些量全程用 fp32 计算与存储）与 **bf16 量化点**（fp32→bf16 的舍入发生在哪些确定位置）。
2. 说清 `ex2.approx.ftz.f32`、`tanh.approx.f32`、`rsqrtf`、FMA 这些"不精确"的指令为什么既能通过 `torch.equal` 级的 bit-exact 测试，又不损害端到端精度。
3. 独立推导：在 `lower_bound = -5`、`CHUNK = 16` 下，`exp(cumsum(g))` 的动态范围上界，并论证它恰好落在 bf16 可表示范围内——这是 FlashKDA 不需要 FLA 那套块内重缩放（intra-chunk rescaling）的根源。

本讲是"总账"性质的复盘：不引入新的调用链，而是把 u2-l7、u3-l1、u3-l4、u3-l5、u3-l6 中已经见过的代码，统一换上"精度"这副眼镜再看一遍。

## 2. 前置知识

### 2.1 bf16 与 fp32 的格式差异

| 格式 | 符号位 | 指数位 | 尾数位 | 相对精度 | 正常数范围 |
|---|---|---|---|---|---|
| fp32 | 1 | 8 | 23 | \(2^{-24}\) 量级 | \([2^{-126},\approx 3.4\times 10^{38}]\) |
| bf16 | 1 | 8 | 7 | \(2^{-8}\) 量级（约 0.4%） | 与 fp32 **完全相同** |

两个关键事实：

- **动态范围相同**：bf16 与 fp32 共享 8 位指数，所以"能不能表示"（上下溢）的判断对两者一致；
- **尾数差 16 倍**：bf16 每次量化带来的相对误差约 \(2^{-8}\)，是 fp32 的 \(2^{16}\) 倍。因此浮点的相对误差与数值量级无关——存 \(5\times 10^{34}\) 和存 \(5\times 10^{-35}\) 的相对精度一样。

bf16→fp32 的转换是**精确的**（尾数只是补零），所以本讲里所有"量化"都特指 fp32→bf16 方向的舍入；FlashKDA 统一使用 RNE（round-to-nearest-even）。

### 2.2 FMA 的"单次舍入"

\( \mathrm{fma}(c, a, b) \) 计算 \(c + a \times b\) 并**只舍入一次**。与之相对，朴素的 `c + a * b` 在 fp32 里要舍入两次（乘法一次、加法一次）。两次舍入与一次舍入的结果可能相差 1 ulp——对 bit-exact 测试来说这是致命差异，所以参考实现必须精确复刻 FMA 的舍入次数（见 4.2.3）。

### 2.3 PTX 近似指令

SM90 上有些超越函数没有"精确"实现，只有硬件近似指令，例如 `ex2.approx.ftz.f32`（\(2^x\)，结果低于正常数下界时冲零，即 FTZ）、`tanh.approx.f32`。它们的误差大于 1 ulp（tanh 近似的绝对误差在 \(10^{-3}\) 量级，ex2 在 \(10^{-7}\) 量级，精确常数以 PTX ISA 文档为准），但有两个宝贵性质：

1. **确定性**：同一输入永远得到同一输出比特，不依赖任何运行时状态；
2. **高吞吐**：单条指令完成，无需多项式展开。

### 2.4 bit-exact 测试的哲学

FlashKDA 的测试用 `torch.equal` 断言 kernel 输出与 PyTorch 参考实现**逐位相等**。这要求参考实现复刻的不是"数学上正确"的行为，而是**硬件实际执行的行为**——包括近似指令、FMA 舍入顺序、蝶形归约顺序、以及每一个 bf16 量化点的位置。理解了这一点，4.2 节"近似指令为何可被 exact-match 接受"的答案就不再反直觉：测试从不要求近似指令"准"，只要求双方用**同一个**。

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
|---|---|
| `csrc/smxx/utils.cuh` | 三条近似指令原语（ex2/tanh/sigmoid）、bf16↔fp32 转换、单 warp MMA 的两种 epilogue、前代换求逆的量化点 |
| `csrc/smxx/fwd_kernel1.cuh` | L2 归一化、门控激活与 cumsum、decay 家族的全部量化点；exp(g_total) 的就地变换 |
| `csrc/smxx/fwd_kernel2.cuh` | 状态常驻 bf16、各 MMA 相位的累加器精度、状态更新的融合 FFMA |
| `csrc/flash_kda.cpp` | `gate_scale = lower_bound × log2(e)` 的 host 侧换底 |
| `setup.py` | `--use_fast_math`（`expf`→`ex2.approx.ftz` 的编译器保证） |
| `tests/torch_ref.py` | 数值工具箱：`fp32_ex2_ftz`、`fp32_fma`、`l2_normalize_kernel_match`、`inv_fwd_subst_16` |
| `tests/test_fwd.py` | `torch.equal` 断言与 fla fp64 金标对比 |
| `docs/20260420-flashkda-v1-deep-dive.md` | 设计者自述的精度决策（第 3 节）与 CHUNK=16 的范围论证（第 1 节） |

## 4. 核心概念与源码讲解

### 4.1 bf16/fp32 分工地图

#### 4.1.1 概念说明

整个 FlashKDA 前向的精度策略可以压缩成三条原则：

- **原则一：存储与搬运用 bf16，乘加累加用 fp32。** bf16 让 workspace、片上状态、MMA 操作数的占用减半；而一切 GEMM 的累加器（HMMA 天然 fp32）、归约、递推更新都保持 fp32。
- **原则二：量化点固定在边界。** fp32→bf16 的舍入不是"随处发生"，而是只发生在少数几个明确位置（量化后立刻进入 bf16 存储或 bf16 乘法链）。每多一个量化点，端到端误差多一份 \(2^{-8}\) 级别的贡献，也多一处 bit-exact 复刻的负担。
- **原则三：递推状态特殊处理。** 状态 `s` 以 bf16 常驻片上（省一半 smem、免掉每条 GEMM 喂入前的 fp32→bf16 转换），但**每次更新** \(s \leftarrow s\cdot e^{g_{\text{total}}} + \delta s\) 用 fp32 FFMA 完成后才量化回 bf16。deep-dive 第 3 节明确说：只要更新本身是 fp32 FMA，中间结果存 bf16 "在我们的推理基准上没有可测的精度损失"（[docs/20260420-flashkda-v1-deep-dive.md:L45-L55](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/docs/20260420-flashkda-v1-deep-dive.md#L45-L55)，原文为英文，此处意译）。

一个常被忽略的推论：**L 矩阵是全链路唯一"永不量化"的中间量**——它是求逆的种子，量化会直接污染前代换的精度（u3-l1 讲过 fp16 Neumann 级数在这里翻车的教训）。

#### 4.1.2 核心流程

把两讲已经读过的计算链按精度重新走一遍（`[fp32]` 表示计算/存储都是 fp32，`→bf16` 表示量化点）：

```text
K1（每 tile）:
  q,k [bf16输入] → L2归一化 [fp32 FMA累加+蝶形+rsqrtf] →bf16 原地写回
  g [bf16 logits] + dt_bias[fp32] → 门控激活 [fp32, log2域] → cumsum [fp32] 
      → g_cumsum, g_total [fp32 smem]        ← 全程无量化
  exp2(g_cumsum) [fp32 ex2.approx] →bf16→ 与 q,k 的 bf16 乘法链
      → k_decayed / q_decayed / k_inv / k_restored [bf16 workspace]
  g_total → exp2(g_total) [fp32, 就地] → gmem fp32 段      ← workspace 唯一 fp16..fp32 段
  L = k_decayed @ k_invᵀ [HMMA fp32累加] → ×beta[fp32 sigmoid] [fp32 smem]   ← 不量化
  Mqk = q_decayed @ k_invᵀ [HMMA fp32累加] →bf16→ Mqk [bf16 workspace]
  INV：前代换 [fp32 fmaf] → P/M →bf16→ 两次 HMMA [fp32累加] → 各自→bf16→ __hadd2 合并

K2（每 tile 递推）:
  s_acc [bf16 常驻 smem]
  Phase1 双GEMM: k_decayed@sᵀ, q_decayed@sᵀ [HMMA fp32累加, 寄存器 fp32]
  Phase2: 读出项 →bf16；beta → sigmoid[fp32] →bf16
  Phase3: u = (v − u)·β [bf16 逐op舍入]; U = INV@u [fp32累加] →bf16
  Phase4: Mqk@U [fp32累加] →bf16, 与读出项做 bf16 加法 → out
  Phase6: s = bf16( f32(s)·e^{g_total}[fp32] + HMMA[fp32] )   ← 单条 FFMA 单次舍入
  状态 I/O: gmem bf16 直通 或 gmem fp32 ⇄ 片上 bf16（转换不改变计算精度）
```

#### 4.1.3 源码精读

**(1) K1 的量化点清单**

L2 归一化：每线程把 8 个 bf16 元素升到 fp32 顺序累加（编译为 FMA 链），16-lane 蝶形归约后 `rsqrtf`，最后量化回 bf16 原地写回——量化点在出口：

- [csrc/smxx/fwd_kernel1.cuh:L276-L302](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L276-L302)：累加用 `float q_sq`；[L295-L296](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L295-L296) 的 `rsqrtf(q_sq + 1e-6f)` 本身也是近似指令；[L300-L301](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L300-L301) 的 `BF16(q_vals[i] * q_inv)` 是唯一的舍入点。

门控与 cumsum：整段在 fp32（log2 域）进行，无任何量化。注意 `g` 的 smem 缓冲与 `g_total` 都是 `float` 引擎（[csrc/smxx/fwd_kernel1.cuh:L61](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L61)、[L80-L82](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L80-L82)）：

- [csrc/smxx/fwd_kernel1.cuh:L316-L330](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L316-L330)：`sum += g_val; g_smem[...] = sum;` 逐步写 inclusive cumsum，`g_total` 取最后一行。激活链 `bf16→f32 + dt → ×a_log_exp → ×gate_scale×sigmoid`（[L321-L323](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L321-L323)）全程 fp32。
- [csrc/smxx/fwd_kernel1.cuh:L353-L357](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L353-L357)：`g_total` 在进 decay 前被就地变换为 `ex2_approx_ftz_f32(g_total)`，仍以 **fp32** 存入 workspace 的 `g_total` 段（该段每 tile 512 字节，是六段中唯一非 bf16 的段，见 [csrc/smxx/utils.cuh:L73](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L73)）。K2 相位 6 直接以 fp32 读它做衰减乘法。

decay 家族：这是 K1 最密集的量化区。exp 结果先各自量化成 bf16，再进入 bf16 乘法链：

- [csrc/smxx/fwd_kernel1.cuh:L453-L455](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L453-L455)：`exp_cumsum = BF16(ex2_approx_ftz_f32(g))`，然后 `r_qd = q * exp_cumsum * BF16(scale)`、`r_kd = k * exp_cumsum`——cutlass 的 bf16 运算符是"提升 fp32 计算、每步舍入"，`scale` 也预量化成 bf16；
- [csrc/smxx/fwd_kernel1.cuh:L463-L468](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L463-L468)：`inv_cumsum = BF16(ex2(-g))`，`k_inv = k * inv_cumsum`；`k_restored = k * inv_cumsum * BF16(reg_gt)`——注意第三个乘子是 fp32 的 `exp(g_total)` 寄存器值经 `BF16()` 显式量化后的结果，量化位置与参考实现逐点对齐。

L 与 Mqk 的分流——同一套 HMMA，两种出口：

- [csrc/smxx/utils.cuh:L167-L192](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L167-L192)：`mma_m16n16_bf16bf16fp32_1warp` 把**原始 fp32 累加器逐元素存回 smem**（注释明言"only the epilogue differs"），供 L 使用；
- [csrc/smxx/utils.cuh:L146-L165](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L146-L165)：`mma_m16n16_bf16bf16bf16_1warp` 的 `sC_store_op = [] (float x) { return BF16(x); }`（[L162](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L162)）——Mqk 的量化点就在这一行；
- [csrc/smxx/fwd_kernel1.cuh:L482-L486](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L482-L486)：两个单 warp 分别调用两者；随后 [L499-L503](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L499-L503) 的 tril 掩码与 `×sigmoid(beta)` 仍在 fp32 域完成（`beta_tile` 读 bf16 后 `float(...)` 提升）。

INV（详见 u3-l1，这里只记账）：种子 L 全程 fp32 进入前代换：

- [csrc/smxx/utils.cuh:L239-L251](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L239-L251)：前代换的行消去显式用 `fmaf(row_scale, pivot, inv[p])`——单次舍入；
- 量化点一：P 与 M 进入 HMMA 输入前（[utils.cuh:L261-L275](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L261-L275) 的 `BF16(inv[j])`、`BF16(L_fp32(...))`）；
- 量化点二：`-dc` 打包（[utils.cuh:L324-L339](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L324-L339)），`pack_bf16x2` 用 `__floats2bfloat162_rn` 显式 RNE；
- 合并：`INV = P + bf16(o)` 用 `__hadd2`（[utils.cuh:L344-L355](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L344-L355)），两次 HMMA 的累加都是 fp32。

**(2) K2 的量化点清单**

状态与输入的存储精度由 `SharedStorageK2` 一眼读出：`state_acc` 是 bf16 引擎（[csrc/smxx/fwd_kernel2.cuh:L82](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L82)），输入 stage 里只有 `g_total` 是 `float`（[L90](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L90)）。`StateFP32` 模板只改变 **gmem 侧 I/O 的 dtype**，片上 `state_acc` 恒为 bf16（u3-l6 的结论），转换函数 [utils.cuh:L378-L437](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L378-L437) 是纯类型搬运：fp32→bf16 方向才舍入，反向精确。

MMA 相位（u3-l4/u3-l5 已逐相位精读，这里列账）：

- Phase 1 双 GEMM 累加器 `u_acc/out_acc` 是 fp32 片段（[fwd_kernel2.cuh:L527-L531](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L527-L531)），K=128 全程 fp32 累加、不中途量化；
- Phase 2 量化点：读出项 `out_bf16 = BF16(out_acc)`（[L571-L574](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L571-L574)）；beta 经 fp32 tanh-sigmoid 后量化 `BF16(sigmoid_tanh_approx_f32(...))`（[L586-L587](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L586-L587)）；
- Phase 3：擦除项先量化 `u_bf16 = BF16(u_acc)`（[L595](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L595)），delta 修正 `(v − u)·β` 是纯 bf16 逐元素运算（[L603-L604](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L603-L604)），随后 `U = INV@u` fp32 累加、出口量化 bf16（[L620-L622](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L620-L622)）；
- Phase 4 量化点最讲究：`Mqk@U` fp32 累加后先各自量化成 bf16，再做 **bf16 加法** 合成 out（[L644-L649](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L644-L649)）——这是为了精确复刻参考实现 `out = bf16(q@s) + bf16(Mqk@U)` 的舍入顺序，是 bit-exact 的关键点之一；
- Phase 6 状态更新是全 kernel 精度设计的浓缩（[L718-L719](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L718-L719)）：

```cpp
ring_S_acc[bi][slot](c0) = BF16(bf16_to_f32(ring_S_acc[bi][slot](c0)) * g0 + u_acc[bi](c0));
```

`f32(s) * g0 + u_acc` 被编译成**单条 FFMA**（单次舍入），衰减因子 `g0` 是 fp32 读入的 `exp(g_total)`，HMMA 结果 `u_acc` 也是 fp32——"bf16 存储 + fp32 更新"原则一与原则三在此汇合。

**(3) workspace 精度速查表**

| workspace 段 | dtype | 字节/tile | 生产者→消费者 |
|---|---|---|---|
| k_decayed / q_decayed / k_restored | bf16 | 各 4096 | K1 decay_apply → K2 Phase1/3/6 |
| g_total（内容为 \(e^{g_{\text{total}}}\)） | **fp32** | 512 | K1 L356 就地变换 → K2 Phase6 衰减乘子 |
| INV / Mqk | bf16 | 各 512 | K1 求逆/MMA → K2 Phase3/4 |

#### 4.1.4 代码实践

**实践：用数值实验验证"bf16 状态 + fp32 更新"的精度声明**（示例代码，纯 PyTorch，可先在单卡跑）。

1. **目标**：复现 deep-dive 第 3 节的定性结论——只要更新用 fp32 FMA，状态以 bf16 存储不产生可见误差积累。
2. **操作步骤**：保存下面的脚本为 `state_drift.py` 并运行（GPU 优先，CPU 也可）：

```python
# state_drift.py（示例代码）
import torch

torch.manual_seed(0)
T, D = 4096, 128
decay = torch.rand(T, dtype=torch.float64) * 0.999      # e^g_total ∈ (0,1)
delta = torch.randn(T, D, dtype=torch.float64) * 0.01   # 每 tile 的 HMMA 更新量

s_fp32  = torch.zeros(D, dtype=torch.float64)           # 金标：全程 fp64
s_bf16  = torch.zeros(D, dtype=torch.bfloat16)          # 内核策略：bf16 存储
for t in range(T):
    s_fp32 = s_fp32 * decay[t] + delta[t]                                  # fp64 参考
    s_bf16 = (s_bf16.float() * decay[t].float() + delta[t].float()).to(torch.bfloat16)  # fp32 FFMA + bf16 量化
    if t in (15, 255, 4095):
        rel = ((s_bf16.double() - s_fp32).norm() / s_fp32.norm()).item()
        print(f"t={t:5d}  相对偏差 = {rel:.3e}")
```

3. **需要观察的现象**：相对偏差应稳定在 \(10^{-3}\) 量级附近（单次 bf16 量化的 \(2^{-8}\) 水平），**不随 t 增长而系统性放大**。
4. **预期结果**：若把更新改成 `s_bf16.float() * decay[t].float()` 先舍入再加（双舍入），或把状态本身也换成 bf16 参与乘法，偏差会明显增大。可以把这两种劣化变体加进脚本对比。准确数值**待本地验证**（结论的量级可预期，具体数字与随机种子相关）。

#### 4.1.5 小练习与答案

**练习 1**：workspace 六段里为什么只有 `g_total` 用 fp32 存储？
**答案**：`g_total` 的消费者是 K2 Phase 6 的衰减乘子，它直接进入与 fp32 累加器融合的 FFMA（[fwd_kernel2.cuh:L718](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L718)）。若量化成 bf16，状态每 tile 额外引入一次 \(2^{-8}\) 相对误差，且该误差会被后续所有 tile 复利放大；其余五个消费者本来就是 bf16 MMA 的操作数，量化是必然的，存 bf16 不损失额外信息。

**练习 2**：Phase 4 中为什么不把 `Mqk@U` 的 fp32 结果直接加到未量化的读出项 fp32 累加器上、最后只量化一次？
**答案**：那会是"数学上更好"的选择，但与 kernel 行为不符就不是本 kernel 了——参考实现 `torch_ref` 的对应行是 `_out(bf16) + matmul(Mqk, U)(bf16)` 两次独立量化后的 bf16 加法（[tests/torch_ref.py:L232-L233](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L232-L233)）。kernel 选择逐分量量化再相加，参考实现必须复刻同一舍入点才能 `torch.equal`。这也说明：bit-exact 项目里"精度优化"必须两侧同步，否则是在制造不一致。

**练习 3**：bf16→fp32 转换需要考虑舍入吗？
**答案**：不需要。bf16 尾数是 fp32 的前缀，转换只是高位补零，是精确映射（kernel 用单条 `cvt.f32.bf16`，[utils.cuh:L55-L59](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L55-L59)）。因此"量化点"这个概念只存在于 fp32→bf16 方向。

### 4.2 PTX 近似指令与 bit-exact 复刻

#### 4.2.1 概念说明

kernel 里的超越函数不是"精确"的：sigmoid 用 `tanh.approx.f32` 拼出、指数用 `ex2.approx.ftz.f32`、L2 归一化用 `rsqrtf`。这些指令带来两类问题，答案分别是什么？

**问题一：这么粗糙的近似，端到端精度能接受吗？** 能，因为近似误差都发生在"语义上只需要大致正确"的量上：

- sigmoid 出现在门控与 beta 上——遗忘强度和写入强度本来就不需要 7 位十进制精度；tanh 近似约 \(10^{-3}\) 的绝对误差与下游 bf16 量化的 \(2^{-8}\approx 3.9\times 10^{-3}\) 同量级或更小，**不构成新的精度瓶颈**；
- `ex2` 近似误差在 \(10^{-7}\) 量级，远小于 bf16 舍入——exp 结果出来后立刻就要量化 bf16（4.1.3 的 decay 量化点），1~2 ulp 的 fp32 差异绝大多数被量化吸收；
- deep-dive 第 3 节末尾给出了与 fla `chunk_kda` 的 fp64 金标对比图（[docs/20260420-flashkda-v1-deep-dive.md:L69-L71](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/docs/20260420-flashkda-v1-deep-dive.md#L69-L71)），误差水平与 Triton 实现相当；`tests/test_fwd.py` 的 `test_fwd_vs_fla` 用 0.005 的相对容差断言（[tests/test_fwd.py:L360-L361](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L360-L361)）。

**问题二：近似指令怎么通过 `torch.equal`？** 关键在 2.4 节的哲学：**测试不要求近似"准"，只要求参考实现执行"同一个近似"**。PTX 近似指令是确定的纯函数，于是参考实现可以逐位复刻。

#### 4.2.2 核心流程

`torch_ref.py` 复刻硬件行为的三条通道：

```text
通道一（同指令）：sigmoid —— 用 load_inline 把同一段 tanh.approx.f32 PTX 编译成
                  PyTorch 可调的 CUDA 扩展 sigmoid_ext → 逐位相同有充分保证
通道二（等价模拟）：ex2 —— torch.special.exp2 + torch.where 显式模拟 FTZ 冲零
                  （CUDA 的 fp32 exp2f 即落在 ex2 指令上，FTZ 差异是仅有的缝隙）
通道三（数值工具）：FMA 单次舍入（fp64 中间量）、蝶形归约顺序、
                  torch.mm(..., out_dtype=fp32) 复刻 HMMA 的 bf16 输入/fp32 累加
```

另一个容易漏掉的点：`a_log_exp = expf(A_log)` 这行看似无害的 host 侧标量计算（[fwd_kernel1.cuh:L258](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L258)）也被纳入复刻——扩展整体用 `--use_fast_math` 编译（[setup.py:L78](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L78)），`expf` 因此映射为 `ex2.approx.ftz(x·log2e)`；参考实现的对应行是 `fp32_ex2_ftz(A_log * LOG2E)`（[tests/torch_ref.py:L167](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L167)）。fast-math 与 bit-exact 通常互斥，这个项目里它们却是一体的——因为参考实现连 fast-math 的效果一起复刻了。

#### 4.2.3 源码精读

kernel 侧的三条近似原语集中在 utils 头部：

- [csrc/smxx/utils.cuh:L38-L42](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L38-L42)：`ex2_approx_ftz_f32`，内联 PTX `ex2.approx.ftz.f32`；
- [csrc/smxx/utils.cuh:L44-L53](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L44-L53)：`tanh_approx_f32` 与 `sigmoid_tanh_approx_f32 = tanh(x/2)/2 + 1/2`（用恒等式 \(\sigma(x) = \tanh(x/2)/2 + 1/2\) 把 sigmoid 也押到 tanh 指令上）；
- 使用点：门控激活（[fwd_kernel1.cuh:L323](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L323)）、L 的 beta 掩码（[fwd_kernel1.cuh:L502](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L502)）、K2 的 beta 激活（[fwd_kernel2.cuh:L586-L587](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L586-L587)）；decay 与 g_total 的所有指数（[fwd_kernel1.cuh:L356](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L356)、[L453](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L453)、[L466](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L466)）。

参考实现侧的对应物：

- **通道一**：[tests/torch_ref.py:L7-L38](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L7-L38)——CUDA 源码里就是同一句 `asm("tanh.approx.f32 %0, %1;")`，经 `load_inline` 编译为扩展 `sigmoid_ext`。调用点：门控 [L169](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L169)、beta [L220](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L220)；
- **通道二**：[tests/torch_ref.py:L47-L52](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L47-L52)——`fp32_ex2_ftz` 用 `torch.special.exp2` 加 `torch.where(ret.abs() < tiny, 0, ret)` 模拟 FTZ。它能对拍成功，说明在本项目的取值域内 `torch.special.exp2`（底层即 CUDA `exp2f`）与 `ex2.approx.ftz` 在正常数上逐位一致，FTZ 差异由 `torch.where` 补齐——这正是一处"看起来在用精确函数、实际在复刻近似指令"的精妙写法（该一致性是经验事实，综合实践中会设计实验去探测它）；
- **通道三**：`fp32_fma` 用 fp64 中间量实现单次舍入（[L55-L59](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L55-L59)），`l2_normalize_kernel_match` 复刻 8 元素 FMA 链 + `[8,4,2,1]` 蝶形顺序（[L62-L78](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L62-L78)），`inv_fwd_subst_16` 逐 FMA 复刻前代换（[L87-L122](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L87-L122)）；GEMM 一律 `torch.mm(..., out_dtype=torch.float32)`（如 [L216](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L216)、[L235](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L235)）以匹配 HMMA 的 bf16 输入/fp32 累加。

断言侧：[tests/test_fwd.py:L260-L261](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L260-L261) 的两条 `torch.equal` 是整个复刻体系的验收口——任何一个近似或量化点失配，都会在这里变成断言失败。

#### 4.2.4 代码实践

**实践：实测近似指令与"精确"函数的差距**（示例代码）。

1. **目标**：量化 `tanh.approx.f32` 与 `torch.sigmoid` 的差距，并直接探测"通道二"的一致性假设——`ex2.approx.ftz` 与 `torch.special.exp2` 到底差多少 ulp。
2. **操作步骤**：

```python
# approx_probe.py（示例代码）
import torch
from torch.utils.cpp_extension import load_inline

src = r"""
#include <torch/extension.h>
__global__ void k(const float* x, float* y, float* z, int n) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < n) {
        asm("ex2.approx.ftz.f32 %0, %1;" : "=f"(y[i]) : "f"(x[i]));
        float th; asm("tanh.approx.f32 %0, %1;" : "=f"(th) : "f"(x[i] * 0.5f));
        z[i] = th * 0.5f + 0.5f;   // sigmoid_tanh_approx
    }
}
void probe(torch::Tensor x, torch::Tensor y, torch::Tensor z) {
    int n = x.numel();
    k<<<(n+255)/256, 256>>>(x.data_ptr<float>(), y.data_ptr<float>(), z.data_ptr<float>(), n);
}
"""
ext = load_inline(name="probe_ext", cpp_sources="void probe(torch::Tensor,torch::Tensor,torch::Tensor);",
                  cuda_sources=src, functions=["probe"], verbose=False)

x = torch.cat([torch.linspace(-130, 1, 1_000_000), torch.linspace(-20, 20, 1_000_000)]).cuda()
ex2_ptx, sig_ptx = torch.empty_like(x), torch.empty_like(x)
ext.probe(x, ex2_ptx, sig_ptx)

ex2_ref = torch.special.exp2(x)
sig_ref = torch.sigmoid(x)

def bit_diff(a, b):
    a32, b32 = a.view(torch.int32), b.contiguous().view(torch.int32)
    return (a32 != b32).float().mean().item(), (a32 != b32).sum().item()

print("ex2.approx vs torch.special.exp2 : 比例/个数 =", bit_diff(ex2_ptx, ex2_ref))
print("tanh-sigmoid vs torch.sigmoid    : 比例/个数 =", bit_diff(sig_ptx, sig_ref))
print("sigmoid 最大绝对误差:", (sig_ptx - sig_ref).abs().max().item())
```

3. **需要观察的现象**：tanh-sigmoid 与精确 sigmoid 的不一致比例应接近 1（误差远超 1 ulp）、最大绝对误差在 \(10^{-3}\) 量级；`ex2.approx` 与 `torch.special.exp2` 的不一致情况是本实验的悬念——若为 0，通道二的一致性假设在本域内成立。
4. **预期结果**：sigmoid 差距大（这回答"为什么不直接用 torch.sigmoid 对拍"）；ex2 差距**待本地验证**——它的结果直接决定第 5 节综合实践中变体 C 的预测。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `torch_ref` 不干脆用 `torch.sigmoid`，而要费劲 `load_inline` 一段 PTX？
**答案**：`tanh.approx.f32` 的误差（约 \(10^{-3}\)）远大于 fp32 舍入，与精确 sigmoid 的输出比特大面积不同。这些输出进入 cumsum 后误差还会累积放大，必然改变下游 bf16 量化点的舍入结果，`torch.equal` 必挂。复刻的唯一可靠办法就是执行同一条指令。

**练习 2**：`--use_fast_math` 通常被视作数值可比性的敌人，为什么这个项目敢开？
**答案**：fast-math 把 `expf` 等函数映射到近似指令、允许 FMA 合并——这些都在参考实现的复刻清单里（`fp32_ex2_ftz`、`fp32_fma`、`l2_normalize_kernel_match`）。项目把"编译器会做什么"当作规格的一部分写进了参考实现，因此 fast-math 反而是规格成立的前提。风险在于：换编译器版本或改 flag 可能悄悄改变_lowering_，bit-exact 就碎了——这也是二次开发时不能乱动 [setup.py:L78-L79](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L78-L79) 的原因。

**练习 3**：`fp32_fma` 为什么用 fp64 中间量 `(c.to(f64) + a.to(f64)*b.to(f64)).to(f32)` 就能模拟单次舍入？
**答案**：三个 fp32 数的精确乘加结果最多需要约 50 位尾数，fp64 的 53 位尾数足以**精确**表示它；最后一次性转回 fp32，等价于对精确结果做一次 RNE——与硬件 FFMA 的语义一致。

### 4.3 CHUNK=16 的范围论证

#### 4.3.1 概念说明

deep-dive 第 1 节把"CHUNK=16 让数值范围落在 bf16 内"列为三大动机之首（[docs/20260420-flashkda-v1-deep-dive.md:L16-L19](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/docs/20260420-flashkda-v1-deep-dive.md#L16-L19)）。这里"范围"指的不是尾数精度，而是**指数范围**：块内衰减系数 \(e^{\text{cumsum}}\) 最小能到多小而不掉出 bf16 的可表示区间。FLA 选 CHUNK=64，块内需要一套精心设计的重缩放来避免下溢；FlashKDA 选 CHUNK=16 后这套机制整体省掉——代价是块数变多，收益是数值路径极简。

#### 4.3.2 核心流程（推导）

门的构造（u1-l2 已建立）：

\[ \tilde g_t = a \cdot \sigma\!\left(e^{A_{\log}} (g_t^{\text{raw}} + b_{\text{dt}})\right),\qquad a = \text{lower\_bound} = -5 \]

由 \(\sigma(\cdot)\in(0,1)\) 得 \(\tilde g_t \in (a,\, 0)\)：门恒负，只遗忘不放大。kernel 在 **log2 域**工作（host 侧预乘换底，见 4.3.3），每步门为：

\[ g_t = \tilde g_t \log_2 e \in \left(a\log_2 e,\ 0\right) \approx (-7.2135,\ 0) \]

块内 inclusive cumsum 的最坏界（CHUNK=16 步全部取到下界）：

\[ c_i = \sum_{t \le i} g_t \in \left(16 \cdot a \log_2 e,\ 0\right] = (-115.42,\ 0] \]

于是 decay 系数的取值范围：

\[ 2^{c_i} \in \left[\,2^{-115.42},\ 1\,\right] \approx \left[\,2.1\times 10^{-35},\ 1\,\right] \]

对照 bf16/fp32 的正常数下界 \(2^{-126} \approx 1.2\times 10^{-38}\)：

- 最小 decay 系数 \(2^{-115.42} > 2^{-126}\)，**仍是正常数**——既不触发 `ex2.approx.ftz` 的冲零，也没有精度坍缩；
- 反向系数 \(2^{-c_i} \le 2^{115.42} \approx 5.2\times 10^{34} < 2^{127}\)，`k_inv` **不上溢**；
- `k_restored = k \cdot 2^{c_{15} - c_i}` 的指数 \(c_{15}-c_i \in (-115.42, 0]\)，天然落在 \((0,1]\) 区间——"先除后乘"的构造让最危险的组合量反而有界。

**与 CHUNK=64 对比**（FLA 的选择）：\(64 \times 7.2135 = 461.7\)，则 \(2^{-461.7}\) 远低于 bf16 最小非正规数 \(2^{-133}\)（彻底冲零、块内相对衰减信息丢失），\(2^{+461.7}\) 上溢为 inf。这就是 FLA 必须做块内重缩放、而 FlashKDA 可以把整条 decay 路径写成"一次 exp2、一次乘法"的根源。

**裕度与耦合**：不触发冲零要求 \(\text{CHUNK} \times |a| \log_2 e < 126\)，即 \(\text{CHUNK} \le \lfloor 126 / 7.2135 \rfloor = 17\)。CHUNK=16 恰好留出一格余量（同时保持 2 的幂便于 MMA 分块）。反过来，若要放宽 `lower_bound`（例如 \(-10\)），每步界变为 \(14.43\)，CHUNK 上限缩到 8——**lower_bound 与 CHUNK 是一对绑定的架构参数**，这是 u3-l12 讨论"扩展方向"时的硬约束。

#### 4.3.3 源码精读

- 换底发生在 host 侧：[csrc/flash_kda.cpp:L128](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L128) `float gate_scale = float(lower_bound * 1.4426950408889634);`——把 lower_bound 预乘 \(\log_2 e\)，kernel 内 cumsum 全程 log2 域，所有指数一步进入 `ex2`，省掉每次的换底 FMA（deep-dive 第 4 节的"Base-2 exponent"优化，[L75-L77](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/docs/20260420-flashkda-v1-deep-dive.md#L75-L77)）；
- 乘法点：[csrc/smxx/fwd_kernel1.cuh:L323](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L323) `g_val = gate_scale * sigmoid_tanh_approx_f32(g_val);`——从此 `g_smem` 里存的就是 log2 域门值，界 \( (a\log_2 e, 0) \) 由此成立；
- 界的两个消费端：cumsum 后的 `ex2` 调用（[L356](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L356)、[L453](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L453)、[L466](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L466)）——4.3.2 的推导保证这些调用的输出落在正常数区间，FTZ 分支界内不触发；
- 测试侧的默认参数印证：[tests/test_fwd.py:L225](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L225) `LOWER_BOUND = -5.0` 与 CHUNK=16 搭配正是设计点。

#### 4.3.4 代码实践

**实践：把范围论证变成可复算的表格**（示例代码）。

1. **目标**：对 CHUNK 与 lower_bound 的组合自动判断"是否冲零/上溢"，验证 CHUNK=17 的理论上限。
2. **操作步骤**：

```python
# range_check.py（示例代码）
import math

LOG2E = math.log2(math.e)
for a in (-2.0, -5.0, -10.0):
    for C in (16, 17, 18, 32, 64):
        drop = C * abs(a) * LOG2E            # 块内 cumsum 的 log2 域最坏跌幅
        lo, hi = 2.0 ** (-drop), 2.0 ** drop  # decay 与反向系数的极值
        flush = lo < 2.0 ** -126              # 低于正常数下界 → FTZ 冲零
        over  = hi >= 2.0 ** 127              # 超出 fp32/bf16 上界 → inf
        print(f"a={a:5.1f} CHUNK={C:3d}  2^-{drop:7.2f} = {lo:.3e}  "
              f"{'FLUSH!' if flush else 'ok  '}  {hi:.3e} {'OVERFLOW!' if over else 'ok'}")
```

3. **需要观察的现象**：`a=-5` 行中 CHUNK=16/17 两列 ok、18 起 FLUSH；`a=-10` 行中 16 也应 FLUSH（\(16\times 14.43=230.9>126\)）；CHUNK=64 全军覆没。
4. **预期结果**：表格输出与 4.3.2 的手算一致；特别确认 \(\lfloor 126/(5\log_2 e)\rfloor = 17\)。此脚本纯 CPU，无需 GPU。

#### 4.3.5 小练习与答案

**练习 1**：既然 bf16 与 fp32 指数域相同，"落在 bf16 范围内"的说法是不是与 bf16 无关？
**答案**：就上下溢判断而言确实只取决于共享的 8 位指数，等价于"落在 fp32 正常数范围内"。但结论是 bf16 专属的：只有当系数不下溢时，把它**量化成 bf16 存储**（workspace、操作数都是 bf16）才是无损于范围的；且 bf16 的 FTZ 语义同样以 \(2^{-126}\) 为界。表述更准确的说法是："decay 系数的动态范围足够窄，使得 bf16 这种'宽指数、窄尾数'的格式既能装下它、又不必做重缩放"。

**练习 2**：cumsum 的界用到了"每步都取到下界"的最坏情况。实际模型的门会到这么负吗？这会削弱论证吗？
**答案**：实际门值通常远高于下界，最坏界是保守的——但这正是设计想要的：正确性论证不应依赖输入分布。测试里 `g=U(-8,8)`、`bias=N(0,8)` 等扫描用例（[tests/test_fwd.py:L45-L68](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L45-L68)）刻意覆盖极端激活，但**激活后的门**被 sigmoid 钳在 \((a,0)\) 内，无论 raw logits 多疯狂界都成立——这是把无界输入折叠进有界门构造的漂亮设计。

**练习 3**：如果把 CHUNK 改成 32 并保持 lower_bound=-5，最小 decay 系数是多少？需要什么机制才能正确？
**答案**：\(2^{-32\times 7.2135} = 2^{-230.8} \approx 10^{-70}\)，低于 \(2^{-126}\) 会被冲零。需要 FLA 式的块内重缩放：把衰减拆成"块间标量因子 × 块内有界因子"，定期对状态做显式 renormalize——正是 deep-dive 所说的 "elaborate intra-chunk rescaling tricks"。

## 5. 综合实践

**任务：用消融法找出"kernel 精度复刻的最小必要集合"**——写 `approx_study.py`，把 `tests/torch_ref.py` 中的近似复刻逐个替换成"更精确"的实现，观察哪些替换会破坏与 kernel 的 bit-exact（`torch.equal` 变 `False`）。

设计思路：不改 kernel、不改 `torch_ref.py` 原文件，用 `importlib` 加载模块后**猴子补丁**它的三个复刻通道（它们都是模块级名字，函数体内通过模块全局查找，补丁即时生效）。

```python
# approx_study.py（示例代码；在仓库根目录运行，需已安装 flash_kda）
import math, importlib.util
import torch, torch.nn.functional as F
import flash_kda

spec = importlib.util.spec_from_file_location("torch_ref", "tests/torch_ref.py")
tr = importlib.util.module_from_spec(spec); spec.loader.exec_module(tr)  # 首次运行会触发 load_inline 编译

def make_inputs(T=1024, H=8, D=128, seed=0):
    torch.manual_seed(seed)
    q = F.normalize(torch.randn(1, T, H, D, device="cuda"), p=2, dim=-1).to(torch.bfloat16)
    k = F.normalize(torch.randn(1, T, H, D, device="cuda"), p=2, dim=-1).to(torch.bfloat16)
    v = torch.randn(1, T, H, D, dtype=torch.bfloat16, device="cuda")
    g = torch.randn(1, T, H, D, dtype=torch.bfloat16, device="cuda")
    beta = torch.randn(1, T, H, dtype=torch.bfloat16, device="cuda")
    A_log = torch.rand(H, device="cuda"); dt_bias = torch.rand(H, D, device="cuda")
    h0 = torch.randn(1, H, D, D, device="cuda").to(torch.bfloat16)
    return q, k, v, g, beta, A_log, dt_bias, h0

def run_pair():
    """kernel 与（可能已被打补丁的）参考实现各跑一次，返回是否 bit-exact。"""
    q, k, v, g, beta, A_log, dt_bias, h0 = make_inputs()
    out_k = torch.zeros_like(q); fs_k = torch.zeros_like(h0)
    flash_kda.fwd(q, k, v, g, beta, 1/math.sqrt(128), out_k,
                  A_log=A_log, dt_bias=dt_bias, lower_bound=-5.0,
                  initial_state=h0.clone(), final_state=fs_k)
    out_r = torch.zeros_like(q); fs_r = torch.zeros_like(h0)
    tr.torch_ref(q, k, v, g, beta, 1/math.sqrt(128), out_r,
                 A_log=A_log, dt_bias=dt_bias, lower_bound=-5.0,
                 initial_state=h0.clone(), final_state=fs_r)
    torch.cuda.synchronize()
    return torch.equal(out_k, out_r), torch.equal(fs_k, fs_r)

def study(name, patch, restore):
    print(f"[{name}] out_equal/state_equal = {run_pair()}")
    restore()

# A. 基线：原样复刻（预期 True/True）
study("baseline", lambda: None, lambda: None)

# B. 把 tanh.approx sigmoid 换成精确 torch.sigmoid（通道一）
orig_sig = tr.sigmoid_ext.sigmoid_tanh_fp32
tr.sigmoid_ext.sigmoid_tanh_fp32 = lambda x: torch.sigmoid(x)
study("exact sigmoid", lambda: None, lambda: setattr(tr.sigmoid_ext, "sigmoid_tanh_fp32", orig_sig))

# C. 把 exp2 换成精确 exp(x*ln2)（通道二）
orig_ex2 = tr.fp32_ex2_ftz
tr.fp32_ex2_ftz = lambda x: torch.exp(x.float() * math.log(2.0))
study("exact exp", lambda: None, lambda: setattr(tr, "fp32_ex2_ftz", orig_ex2))

# D. 只去掉 FTZ 模拟（保留 torch.special.exp2）
tr.fp32_ex2_ftz = lambda x: torch.special.exp2(x.float() if x.dtype==torch.float16 else x)
study("exp2 without ftz", lambda: None, lambda: setattr(tr, "fp32_ex2_ftz", orig_ex2))

# E. 把 FMA 单次舍入换成朴素双舍入（通道三）
orig_fma = tr.fp32_fma
tr.fp32_fma = lambda c, a, b: c + a * b
study("naive fma", lambda: None, lambda: setattr(tr, "fp32_fma", orig_fma))
```

**观察与预期**（全部标注：具体结果**待本地验证**，下表是基于本讲分析的预测）：

| 变体 | 预测 | 依据 |
|---|---|---|
| A 基线 | True / True | CI 即此状态 |
| B 精确 sigmoid | **False** | tanh 近似误差 ~\(10^{-3}\) ≫ fp32 ulp，必然改变 bf16 舍入点 |
| C 精确 exp | False（待验证） | 取决于 `torch.special.exp2` 与 `ex2.approx` 是否逐位一致（4.2.4 探测过） |
| D 去掉 FTZ | **True** | 4.3 节论证：界内不会产生亚正规输出，冲零分支是防御性的 |
| E 朴素 FMA | False（概率性） | 双舍入差异 1 ulp，需跨过 bf16 舍入边界才显现——若 True 请把 T 加大到 8192 重试 |

**产出**：把实验结果整理成"最小必要集合"结论——预期它包含：{tanh.approx 版 sigmoid（g 与 beta 两处）、ex2+FTZ、fp32 FMA 单次舍入、L2 蝶形归约顺序、HMMA 的 fp32 累加、以及 4.1 节那张量化点地图的每一个位置}。这份清单就是"想复刻这个 kernel 的数值行为，最少要抄哪些作业"的权威答案；任何一项替换成"更精确"的实现，都会（或有机会）让 `torch.equal` 失败。

注意：若变体 D 意外为 False，说明存在本讲未推导到的亚正规触发路径（例如极端 A_log 输入）——那本身就是有价值的发现，请回到 4.3 的界检查哪个环节被突破。

## 6. 本讲小结

- **分工总原则**：存储/搬运 bf16、乘加累加 fp32；fp32→bf16 量化只发生在固定边界（K1 的 decay 家族与 Mqk/INV 出口、K2 的各相位出口），bf16→fp32 方向精确无损。L 矩阵是唯一全程 fp32 的中间量，状态更新 `bf16(f32(s)·e^g + fp32acc)` 用单条 FFMA 收尾。
- **近似指令的两重合法性**：端到端看，tanh/ex2 的误差不超过下游 bf16 量化的 \(2^{-8}\) 粒度，不构成精度瓶颈（fp64 金标验证）；测试看，近似指令是确定性纯函数，`torch_ref` 通过同指令编译（sigmoid）、exp2+FTZ 模拟、fp64 中间量模拟 FMA 三条通道逐位复刻。
- **fast-math 与 bit-exact 在此项目互为因果**：`--use_fast_math` 决定了 kernel 实际执行的指令 _lowering_，参考实现把 lowering 当规格复刻——因此改编译 flag 等于改规格。
- **范围论证**：门 \(\tilde g\in(-5,0)\)（ln 域）⇒ log2 域每步 \(\in(-7.2135,0)\) ⇒ 16 步 cumsum \(\in(-115.42,0]\) ⇒ decay 系数 \(\in[2^{-115.42},1]\)，安全落在 \(2^{-126}\) 之上；CHUNK≤17 是硬上限，CHUNK=16 与 lower_bound=-5 构成绑定约束，这正是省掉 FLA 块内重缩放的根源。
- workspace 六段中唯有 `g_total`（内容 \(e^{g_{\text{total}}}\)）以 fp32 传递——它直接进入 K2 状态更新的融合 FFMA，量化它会带来复利放大的误差。

## 7. 下一步学习建议

- **u3-l9（测试方法学）**：本讲的量化点地图和复刻通道是 `torch_equal` 测试的"供给侧"；下一讲从"需求侧"看测试体系如何组织 bit-exact 断言、fla fp64 金标与参数扫描，并把综合实践的结论用于为新场景补测试。
- **u3-l12（二次开发）**：范围论证给出的 lower_bound×CHUNK 耦合是扩展的头号约束；届时把本讲的"最小必要集合"当作改动后必须逐项复核的清单。
- 若想继续深挖数值，建议阅读 PTX ISA 中 `ex2.approx.ftz.f32`/`tanh.approx.f32` 的误差定义，以及 CUDA C Programming Guide 的 fast-math 一节，把本讲中标注"量级/待确认"的常数补齐。
