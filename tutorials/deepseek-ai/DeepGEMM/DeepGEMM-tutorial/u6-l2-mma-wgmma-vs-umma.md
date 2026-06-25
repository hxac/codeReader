# MMA 抽象：WGMMA(SM90) vs UMMA(SM100)

## 1. 本讲目标

上一讲（u6-l1）我们走进了 SM90 FP8 GEMM 设备内核，看到 math warp-group 用一种神秘的「矩阵乘加指令」驱动 tensor core。本讲就回答：**这条指令到底是什么、它的输入如何被编码、SM90 与 SM100 两代架构在「张量核编程模型」上有哪些本质差异。**

读完本讲你应当能够：

- 说清 SM90 **WGMMA** 指令「M=64、K=32 固定、只有 N 可选」的形状约束，以及 `FP8MMASelector` 如何按 N 选出对应指令。
- 看懂 SM90 / SM100 两套共享内存描述符（`GmmaDescriptor` / `SmemDescriptor`）如何把「地址 + swizzle + 步长」打包进一个 64 位整数。
- 掌握 SM100 **UMMA/tcgen05** 模型相对 WGMMA 的三大跃迁：累加器搬到 tensor memory（TMEM）、N 维度运行时编码进指令描述符、缩放因子（SF）由硬件吸收而非软件相乘。
- 对比 SM90 与 SM100 在「MMA 描述符」和「SF 描述符」上的设计差异。

## 2. 前置知识

- **tensor core（张量核）**：GPU 里专门做小矩阵乘加（MMA）的硬件单元，一条指令算完一个 \( M \times K \) 乘 \( K \times N \) 的小块。
- **warp-group**：4 个 warp（共 128 个线程）组成的执行单元。SM90 的 WGMMA 指令以 warp-group 为单位发射。
- **共享内存（shared memory, smem）**：片上高速存储，地址用 16 字节为粒度（所以源码里随处可见 `addr >> 4`）。
- **主维（major）**：承接 u2-l1/u4-l2。K-major 指沿 K 轴连续存放、MN-major 指沿 MN 轴连续存放。SM90 的 WGMMA 强制 K-major，SM100 放宽。
- **描述符（descriptor）**：一个把「数据在哪里、长什么样」编码进固定位域的小整数，硬件直接消费，省去逐元素寻址。
- **缩放因子（SF）**：承接 u2-l2。FP8/FP4 范围窄，每若干元素需要一个 scale。SM90 用 FP32 存、软件读取相乘；SM100 用打包 UE8M0 存、硬件吸收。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [mma/sm90.cuh](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/mma/sm90.cuh) | SM90 WGMMA 抽象：`FP8MMA`/`FP8MMASelector` 选指令，`make_smem_desc`/`make_gmma_desc` 构造 64 位描述符 |
| [mma/sm100.cuh](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/mma/sm100.cuh) | SM100 UMMA 抽象：`make_smem_desc`/`make_umma_desc` 构造 `SmemDescriptor`，`make_sf_desc` 构造 SF 描述符，`make_runtime_instr_desc_with_sf_id` 组装指令描述符 |
| [impls/sm90_fp8_gemm_1d1d.cuh](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh) | SM90 内核，展示 `FP8MMASelector` 与 `make_smem_desc`/`wgmma` 的真实调用 |
| [impls/sm100_fp8_fp4_gemm_1d1d.cuh](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_gemm_1d1d.cuh) | SM100 内核，展示 `make_umma_desc`/`make_sf_desc`/UTCCP 与 `tcgen05` MMA 的真实调用 |

> 说明：`mma/sm90.cuh` 与 `mma/sm100.cuh` 是 DeepGEMM 对 CUTLASS/CuTe 底层 MMA 指令的**薄封装**——它们不自己发明指令，只把「选哪条指令、把共享内存布局编码成硬件要的描述符」这两件事用模板封装好，供各设备内核复用。

## 4. 核心概念与源码讲解

先用一张表建立两代架构的直觉差异，后面三个小节再逐个拆源码。

| 维度 | SM90 WGMMA | SM100 UMMA / tcgen05 |
|---|---|---|
| 指令家族 | `wgmma.mma_async` | `tcgen05.mma` |
| 指令形状 | M=64、K=32（FP8）固定，N∈{8…256} 步长 8 | M∈{64,128,256}、N∈{8…256}，K 按指令粒度 |
| N 维度 | **编译期**由 selector 选定指令 | **运行时**编码进 `InstrDescriptor` |
| 操作数来源 | 共享内存（描述符 `desc_a`/`desc_b`） | 共享内存（`SmemDescriptor`） |
| 累加器位置 | **寄存器**（`float accum[]`） | **tensor memory (TMEM)** |
| 缩放因子 SF | **软件** `ld_shared` 读取后手动乘 | **硬件**吸收，UTCCP 预加载进 TMEM |
| 描述符 | `GmmaDescriptor`（64 位） | `SmemDescriptor` + `InstrDescriptorBlockScaled` |

一句话总结：**SM90 是「固定形状指令 + 寄存器累加 + 软件 SF」，SM100 是「运行时可变形状 + TMEM 累加 + 硬件 SF」，两者都靠一个 64 位共享内存描述符告诉硬件数据在哪、怎么 swizzle。**

### 4.1 WGMMA（SM90）：固定形状指令与按 N 选择

#### 4.1.1 概念说明

SM90（Hopper）引入了 **WGMMA（Warp-Group Matrix Multiply Accumulate）**：一条指令由一个 warp-group（128 线程）发射，从共享内存直接读 A、B，算完一个 \( 64 \times N \times 32 \) 的小块，结果累加进**寄存器**。

关键约束：

- \( M \) 恒为 64（一条指令固定 64 行）。
- \( K \) 恒为 32（FP8，每个 E4M3 占 1 字节，32 个即 32 字节，正好对齐 swizzle 原子）。
- \( N \) 是唯一自由度，取值 \(\{8, 16, 24, \dots, 256\}\)，步长 8。

因为 \( M \)、\( K \) 固定，**不同的 N 对应不同的硬件指令**——CUTLASS 为每个 N 提供一个 `MMA_64xNx32_F32E4M3E4M3_SS_TN` 类型。DeepGEMM 用 `FP8MMASelector<N>` 在编译期把「想要的 N」映射成「具体的指令类型」。

后缀含义（不展开 CUTLASS 命名细节）：`SS` 表示两个操作数都来自**共享内存**（shared-shared，区别于 A 放寄存器的 `RS`）；`SS_TN` 是 FP8 WGMMA 的固定布局要求，与 u2-l1 讲的「SM90 强制 K-major」一致。

#### 4.1.2 核心流程

一个 `BLOCK_N` 的输出块，math warp-group 这样算：

1. 编译期用 `FP8MMASelector<BLOCK_N>::type` 得到一个 `FP8MMA<N, ...>` 类型别名 `WGMMA`。
2. K 维循环：`BLOCK_K / WGMMA::K` 次（128/32 = 4 次），每次算一个 \( 64 \times N \times 32 \) 切片。
3. 每次循环：
   - 用 `make_smem_desc` 把 A、B 的 smem 地址 + 布局打包成 64 位描述符；
   - 调 `WGMMA::wgmma(desc_a, desc_b, accum, scale_d)` 发射指令。
4. `scale_d=false` 时累加器清零（首块），`scale_d=true` 时累加到旧值（后续块）。

#### 4.1.3 源码精读

**按 N 选指令**——`FP8MMASelector::select_mma` 用一串 `if constexpr` 把 N 映射到具体 MMA 类型：

[sm90.cuh:32-68](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/mma/sm90.cuh#L32-L68)（`FP8MMASelector` 与 `select_mma`）——`N` 从 8 枚举到 256，每个分支返回一个 `MMA_64xNx32_F32E4M3E4M3_SS_TN` 实例，编译期消解为零运行时开销。

**指令封装**——`FP8MMA` 把选中的指令包成统一接口：

[sm90.cuh:14-30](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/mma/sm90.cuh#L14-L30) —— 注意三个 `static constexpr int`：

- `M = 64`、`K = 32`（FP8 固定，BF16 则为 16、TF32 为 8，见同文件 L89、L164）；
- `kNumAccum = M * N / 128`，因为 M 恒为 64，所以 `kNumAccum == N / 2`。

`wgmma` 静态方法（L22-24）展开 `make_index_sequence<N/2>`，把 N/2 组累加器与描述符交给底层 `MMA::fma`，后者映射成一条 `wgmma.mma_async` PTX 指令；`scale_d` 三元决定 `ScaleOut::One/Zero`，实现「首块清零、后续累加」。

> WGMMA 的 K=32 与 u6-l1 讲的 `BLOCK_K==128` 约束直接相关：一个 128-K 块正好拆成 4 条 K=32 的 WGMMA，所以 SF 的「每 128 个 K 一个」粒度与指令边界天然对齐。

**真实调用**——SM90 内核里 math 线程发起计算的片段：

[sm90_fp8_gemm_1d1d.cuh:289-293](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L289-L293) —— 循环 `BLOCK_K / WGMMA::K`（=4）次，每次 `make_smem_desc` 取 A/B 描述符、`WGMMA::wgmma` 发射指令；类型别名定义在同文件 [L59](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L59)。

#### 4.1.4 代码实践

**实践目标**：验证 WGMMA 的「M=64、K=32 固定、N 步长 8」约束在源码中的体现。

**操作步骤**：

1. 打开 `mma/sm90.cuh`，数一数 `FP8MMASelector::select_mma` 中 `if constexpr` 分支的个数与 N 取值。
2. 对比同文件 `BF16MMA`（L78-93）的 `K` 值与 `FP8MMA` 的 `K` 值，思考为何不同。
3. 打开 `impls/sm90_fp8_gemm_1d1d.cuh` L289，确认循环上界是 `BLOCK_K / WGMMA::K`。

**需要观察的现象**：

- `FP8MMA` 的 `K=32`，`BF16MMA` 的 `K=16`——因为 BF16 占 2 字节，同样 32 字节只能装 16 个元素；这与「K=32 字节对齐 swizzle 原子」一致。

**预期结果**：你会确认每种 dtype 的 WGMMA K 粒度 = \( \frac{\text{swizzle 原子字节数}}{\text{元素字节数}} \)，FP8 为 32、BF16 为 16。

**待本地验证**：若手头有 SM90 卡，可在 `DG_JIT_DUMP_SASS=1` 下编译一个小 GEMM，用 `cuobjdump` 观察生成的 `wgmma.mma_async` 指令的 N 维与 BLOCK_N 是否一致。

#### 4.1.5 小练习与答案

**练习 1**：为何 `FP8MMA` 的 `kNumAccum` 公式是 `M * N / 128`？代入 M=64 化简。

**答案**：M=64 时 `kNumAccum = 64*N/128 = N/2`。一条 64×N×32 的 WGMMA 把结果存进 N/2 个 64 位寄存器对（每个 64 位装 2 个 float），所以累加器组数恰为 N/2，与 `make_index_sequence<N/2>` 的展开次数一致。

**练习 2**：若 BLOCK_N=128，一次 `BLOCK_K` 循环发射几条 WGMMA 指令？

**答案**：`BLOCK_K / WGMMA::K = 128 / 32 = 4` 条，每条算 64×128×32，4 条覆盖 64×128×128 的一个 block。

### 4.2 UMMA 描述符（SM100）：TMEM 累加与运行时形状

#### 4.2.1 概念说明

SM100（Blackwell）把张量核升级为 **UMMA / tcgen05** 模型，相对 WGMMA 有三大变化：

1. **累加器搬到 TMEM**：结果不再放寄存器，而是放在专用的 **tensor memory（TMEM）**——一块容量更大、带宽更高的片上存储。内核用「TMEM 列地址」指定累加位置。
2. **N 维度运行时可变**：不再为每个 N 提供独立指令类型，而是用一条通用 `tcgen05.mma` 指令，把 M/N/dtype 编码进一个 **指令描述符（`InstrDescriptorBlockScaled`）**，运行时写入。
3. **SF 硬件吸收**：缩放因子不再是软件读取相乘，而是预加载进 TMEM，由 MMA 指令在硬件内部完成缩放（见 4.3）。

相应地，操作数仍然来自共享内存，但描述符从 `GmmaDescriptor` 换成了 `cute::UMMA::SmemDescriptor`，并多出一个 `version_`、`lbo_mode_` 字段。

#### 4.2.2 核心流程

SM100 内核的 MMA 发射流程（简化）：

1. **构造静态描述符**：`make_umma_desc` 把 A、B 的 smem 布局打包成 `SmemDescriptor`（地址 + layout + SBO/LBO）。
2. **构造指令描述符**：`cute::UMMA::make_instr_desc_block_scaled` 编码 dtype、M、N、major；`make_sf_desc` 单独为 SF 造描述符。
3. **K 维循环**：每个 K 切片：
   - 用 UTCCP 把 SF 从共享内存搬到 TMEM（4.3 详述）；
   - 用 `advance_umma_desc_lo` 更新 A/B 描述符的地址低位（推进 K）；
   - 用 `make_runtime_instr_desc_with_sf_id` 把当前 SF 的 TMEM id 写进指令描述符；
   - 调 `ptx::SM100_MMA_MXF8F6F4_SS::fma(a_desc, b_desc, tmem_col, ..., runtime_instr_desc, ...)` 发射 tcgen05 指令。

> 与 WGMMA 对比：WGMMA 的「换 N = 换指令」，UMMA 的「换 N = 改指令描述符的一个字段」——这就是 `update_instr_desc_with_umma_n` 存在的意义。

#### 4.2.3 源码精读

**SmemDescriptor 构造**——`make_smem_desc`（SM100 版）：

[sm100.cuh:13-39](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/mma/sm100.cuh#L13-L39) —— 与 SM90 版对比有两点关键不同：

- **多了 `version_=1` 和 `lbo_mode_=0`**（L19、L22），这是 SM100 描述符格式的新字段；
- **参数顺序不同**：SM100 是 `(layout, smem_ptr, SBO, LBO)`，SM90 是 `(smem_ptr, layout_type, LBO, SBO)`——两代封装故意不同，调用时务必注意。

位域仍是「16 字节粒度」：`start_address_ = uint_ptr >> 4`、`stride_byte_offset_ = SBO >> 4`、`leading_byte_offset_ = LBO >> 4`（L29、L35-36）。

**完整 A/B 描述符**——`make_umma_desc` 按 K-major / MN-major 分支计算 SBO、LBO：

[sm100.cuh:94-133](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/mma/sm100.cuh#L94-L133) —— K-major 分支（L100-112）断言 `kSwizzleMode == BLOCK_K * sizeof(dtype_t)`（每个 K 块恰好一个 swizzle 原子，所以 LBO=0）；MN-major 分支（L113-132）则按 atom 尺寸计算跨步。其中 `num_non_contiguous = 128 / get_atom_base(layout_type)`（L99）——atom base 取 16 或 32（见 [L56-59](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/mma/sm100.cuh#L56-L59)），描述了「一个 swizzle 原子里有多少个非连续段」。

**指令描述符的运行时组装**：

[sm100.cuh:135-149](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/mma/sm100.cuh#L135-L149) —— `make_runtime_instr_desc_with_sf_id` 把 `InstrDescriptorBlockScaled` 移到 64 位整数的高 32 位、并在低 32 位写入 `a_sf_id_`/`b_sf_id_`（指向 TMEM 里的 SF 位置）；`update_instr_desc_with_umma_n` 则把实际 N 写入 `n_dim_ = umma_n >> 3`（除 8 是因为 N 步长为 8）。**这正是 SM100「N 运行时可变」的落点**：swap-AB 模式下，有效 M 在运行时才知道，于是动态改写 N 字段。

**真实调用**——SM100 内核的 MMA issue 段：

[sm100_fp8_fp4_gemm_1d1d.cuh:284-293](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_gemm_1d1d.cuh#L284-L293) —— 先造 `instr_desc`（指令描述符）、`sf_desc`（SF 描述符）、`a_desc`/`b_desc`（操作数描述符）。注意 `accum_stage_idx * UMMA_N`（见 L385、L389）是 **TMEM 列地址**——累加器不再叫 `accum[]` 寄存器数组，而是 TMEM 里的一段列。

[sm100_fp8_fp4_gemm_1d1d.cuh:374-392](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_gemm_1d1d.cuh#L374-L392) —— `issue_umma` lambda：`advance_umma_desc_lo` 推进 K 维地址、`make_runtime_instr_desc_with_sf_id` 注入 SF id，最后 `mma_t::fma(...)` 发射 tcgen05 MMA。`mma_t` 在 `kNumMulticast==1` 时是 `SM100_MMA_MXF8F6F4_SS`，否则是 `_2x1SM_SS`（双 CTA multicast，承接 u6-l1 的 2-CTA cluster）。

#### 4.2.4 代码实践

**实践目标**：对比 SM90 与 SM100 两套 `make_smem_desc`，看清描述符位域与参数顺序的差异。

**操作步骤**：

1. 并排打开 `mma/sm90.cuh` 的 `make_smem_desc`（L195-209）与 `mma/sm100.cuh` 的 `make_smem_desc`（L13-39）。
2. 列出两者各自的：①参数顺序；②位域字段；③默认值。
3. 在 `sm100_fp8_fp4_gemm_1d1d.cuh` 中找到 `UMMA_M`/`UMMA_N` 的合法取值断言（L299-302），记录 M 可取哪些值。

**需要观察的现象**：

- SM100 描述符比 SM90 多 `version_`、`lbo_mode_` 两个字段；
- SM100 的 M 可取 64/128/256（断言 L299-301），而 SM90 WGMMA 的 M 固定 64。

**预期结果**：你会直观看到「SM100 用一个字段（`n_dim_`）表达 SM90 要用一整条独立指令表达的 N 维度自由度」。

**待本地验证**：在 SM100 上 `DG_JIT_DUMP_SASS=1` 编译，观察 `tcgen05.mma` 指令是否随 BLOCK_N 变化而保持同一条（仅操作数/描述符不同），与 SM90「N 变则指令变」对比。

#### 4.2.5 小练习与答案

**练习 1**：SM100 内核里，累加器「在哪里」？用什么标识？

**答案**：在 TMEM（tensor memory），用列地址标识，例如 `accum_stage_idx * UMMA_N` 给出累加区起始列。这与 SM90 把累加器放在寄存器数组 `accum[]` 形成本质区别。

**练习 2**：`update_instr_desc_with_umma_n` 里 `n_dim_ = umma_n >> 3`，为什么除以 8？

**答案**：因为 UMMA 的 N 步长固定为 8（断言 L299-302 要求 `UMMA_N % 8 == 0`），把 N 除以 8 编码进描述符可节省位宽。这也呼应 WGMMA 的 N 步长同为 8。

### 4.3 SF 描述符：从软件相乘到硬件吸收

#### 4.3.1 概念说明

缩放因子（SF）的处理是两代架构差异最集中的体现：

- **SM90（WGMMA）**：SF 是普通 FP32 张量，放在共享内存里。math 线程用普通加载指令 `ptx::ld_shared` 把它读进寄存器，**在软件里手动乘**到累加结果上。SF 与 MMA 指令彼此独立。
- **SM100（UMMA）**：SF 是打包 UE8M0（承接 u2-l2）。它不再由软件读取，而是用专用指令 **UTCCP（cp.async 到 TMEM）** 预先搬进 TMEM，MMA 指令通过指令描述符里的 `a_sf_id`/`b_sf_id` **引用 TMEM 里的 SF 位置，由硬件在乘加内部吸收**。

为此 SM100 专门提供 `make_sf_desc` 构造 SF 的共享内存描述符，供 UTCCP 使用。这是 SM90 完全没有的概念。

#### 4.3.2 核心流程

SM100 SF 流水（简化）：

1. SF 已被宿主 `transform_sf_into_required_layout` 变换成 MN-major + 16B 对齐布局（承接 u2-l2），TMA 搬进共享内存。
2. MMA issue 阶段，每隔若干 K 块，leader 线程用 UTCCP 把 SF 从共享内存搬到 TMEM：
   - `make_sf_desc(smem_sfa[...])` 构造 SF 描述符；
   - `replace_smem_desc_addr` 动态替换地址（循环复用同一个描述符对象）；
   - `cute_utccp_t::copy(sf_desc, tmem_col)` 发射 UTCCP。
3. MMA 指令通过 `make_runtime_instr_desc_with_sf_id` 拿到 `sfa_id`/`sfb_id`，硬件自动用 TMEM 里的 SF 缩放 A、B。

#### 4.3.3 源码精读

**SF 描述符构造**——`make_sf_desc`：

[sm100.cuh:41-48](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/mma/sm100.cuh#L41-L48) —— 注释点明三个事实：

- UTCCP 默认 **K-major** 布局；
- atom 尺寸 `8 × 128 bits`；
- 因为 UTCCP 是 128 位宽、K 方向只有 1 个 atom，所以 `LBO = 0`、`SBO = 8 * 16`（128 字节）。

它直接复用 `make_smem_desc`，layout 固定 `SWIZZLE_NONE`。地址复用靠 [replace_smem_desc_addr](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/mma/sm100.cuh#L50-L54)（L50-54）只改 `start_address_`。

**真实调用**——SM100 内核把 SFA/SFB 搬进 TMEM：

[sm100_fp8_fp4_gemm_1d1d.cuh:353-369](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_gemm_1d1d.cuh#L353-L369) —— 对 SFA（L353-360）和 SFB（L361-369）分别：循环每个 UTCCP 对齐组，`replace_smem_desc_addr` 改地址、`cute_utccp_t::copy` 搬到 TMEM 列 `kTmemStartColOfSFA`/`kTmemStartColOfSFB`。注意 `cute_utccp_t` 按 `kNumMulticast` 选 1-CTA 或 2-CTA 版本（L350-351）。

**对比 SM90 的软件 SF**：

[sm90_fp8_gemm_1d1d.cuh:275-281](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L275-L281) —— SM90 用 `ptx::ld_shared` 把 FP32 的 SFA/SFB 读进寄存器 `scale_a_*`、`scales_b[]`，稍后在软件里乘到累加结果上。这里没有任何「SF 描述符」——SF 在 SM90 只是一段被普通加载的 FP32 数据。

> 一句话对比：SM90 里 SF 是「数据，软件读、软件乘」；SM100 里 SF 是「带专用描述符、专用加载指令（UTCCP）、专用存储（TMEM）、硬件乘」的资源。

#### 4.3.4 代码实践

**实践目标**：跟踪 SF 在两代架构内核中的不同生命周期。

**操作步骤**：

1. 在 `sm90_fp8_gemm_1d1d.cuh` 中搜索 `smem_sfa`、`smem_sfb`、`scale_a`、`scales_b`，确认 SF 被普通 `ld_shared` 读取并在软件里相乘（提示：L275-281 读取，后续 `Promote with scales` 段相乘）。
2. 在 `sm100_fp8_fp4_gemm_1d1d.cuh` 中搜索 `make_sf_desc`、`UTCCP`、`kTmemStartColOfSF`，确认 SF 被搬进 TMEM，且 MMA 指令只引用 `sfa_id`/`sfb_id` 而不显式乘。

**需要观察的现象**：

- SM90 内核里能看到对 SF 值的算术运算（乘法）；
- SM100 内核里看不到对 SF 的软件乘法，只有搬运（UTCCP）和引用（sf_id）。

**预期结果**：你会确认 SM100 把 SF 的「缩放」完全下沉到硬件，软件只负责「把 SF 放对位置」。

**待本地验证**：在 SM100 上对比禁用/启用 UE8M0（u2-l2 的 `disable_ue8m0_cast` 开关）时，SF 的 dtype 与 `make_sf_desc` 的输入是否随之变化。

#### 4.3.5 小练习与答案

**练习 1**：`make_sf_desc` 为何把 `LBO` 设为 0？

**答案**：因为 UTCCP 是 128 位宽，SF 的 atom 是 `8 × 128 bits`，在 K 方向只有 1 个 atom，跨 atom 的 K 步长（LBO）不存在，故为 0；只有 MN 方向的 atom 步长（SBO=128 字节）有意义。

**练习 2**：为什么 SM90 没有 `make_sf_desc` 这个函数？

**答案**：SM90 的 SF 是 FP32 普通数据，用普通共享内存加载 `ld_shared` 读进寄存器即可，不需要专用的描述符/加载指令/TMEM 通路；硬件也不参与缩放，所以没有 SF 描述符这一抽象。

## 5. 综合实践

**任务**：用一张对比表 + 一段调用链追踪，把本讲三个模块串起来，回答「同一次 FP8 GEMM，SM90 与 SM100 在 MMA 这一层到底差在哪」。

**步骤**：

1. 自制一张「MMA 编程模型对比表」，至少包含以下行：指令名、指令形状可变维度、操作数描述符类型、累加器位置、SF 角色、SF 描述符、N 维度决定方式。先自己填，再对照本讲第 4 节开头那张表核对。

2. **调用链追踪（SM90）**：从 `sm90_fp8_gemm_1d1d.cuh` 的 `using WGMMA = ...`（L59）出发，画出「编译期选指令 → 运行期 `make_smem_desc` 造描述符 → `WGMMA::wgmma` 发射 → 寄存器 `accum[]` 收结果」的链路，标注每一步在哪个文件第几行。

3. **调用链追踪（SM100）**：从 `sm100_fp8_fp4_gemm_1d1d.cuh` 的 `instr_desc`/`sf_desc`/`a_desc`/`b_desc` 构造（L284-293）出发，画出「造描述符 → UTCCP 把 SF 搬进 TMEM → `advance_umma_desc_lo` 推 K → `make_runtime_instr_desc_with_sf_id` 注入 sf_id → `mma_t::fma` 发射 tcgen05 → TMEM 列收结果」的链路。

4. **写一段总结**（3-5 句）：指出 SM100 相对 SM90 新增的硬件资源（TMEM、UTCCP、指令描述符里的 sf 字段）分别解决了 WGMMA 的哪个痛点（寄存器压力、软件 SF 开销、N 不可变）。

**预期产出**：两张调用链草图 + 一段对比说明，能清晰回答「为什么 SM100 能把 FP8/FP4 算得更快」——因为累加器进 TMEM 缓解寄存器压力、SF 硬件吸收省软件开销、N 运行时可变让 swap-AB 等场景更灵活。

## 6. 本讲小结

- SM90 的 **WGMMA** 是「固定形状指令」：M=64、K=32（FP8）写死，只有 N∈{8…256} 步长 8 可选；`FP8MMASelector<N>` 在编译期把 N 映射到具体 `MMA_64xNx32_..._SS_TN` 类型。
- WGMMA 操作数用 64 位 **`GmmaDescriptor`** 描述，位域含 smem 地址（>>4）、layout、LBO、SBO；`make_smem_desc`/`make_gmma_desc` 负责打包，结果累加进**寄存器**。
- SM100 的 **UMMA/tcgen05** 是「运行时可变形状」：用一条通用 `tcgen05.mma`，M∈{64,128,256}、N 由 `InstrDescriptorBlockScaled.n_dim_` 运行时写入（`umma_n >> 3`）。
- SM100 操作数用 **`SmemDescriptor`**（多了 `version_`/`lbo_mode_`）描述，累加器进 **TMEM**（用列地址标识），与 SM90 寄存器累加形成本质差异。
- SF 是两代分水岭：SM90 软件 `ld_shared` 读 FP32 SF 再手动乘；SM100 用 `make_sf_desc` + UTCCP 把打包 UE8M0 SF 搬进 TMEM，MMA 通过 `sf_id` 引用、由硬件吸收。
- 两套封装参数顺序故意不同（SM90: `ptr, layout, LBO, SBO`；SM100: `layout, ptr, SBO, LBO`），跨架构阅读代码时务必留意。

## 7. 下一步学习建议

- **下一步学 u6-l3「PTX 内联函数：TMA 加载与栅栏」**：本讲的 WGMMA/UMMA 都依赖 PTX 封装（`ptx::warpgroup_arrive`、`ptx::tcgen05_*`、`ptx::exchange` 等），下一讲系统拆解 `ptx/` 与 `comm/barrier.cuh`，补齐「指令怎么发射、多线程怎么同步」的最后一环。
- **回顾 u4-l2「TMA 描述符与 swizzle」**：本讲的 smem 描述符里的 `layout_type`/swizzle 与 TMA 描述符的 swizzle 是同一套机制，回头对比能加深对「swizzle 原子」的理解。
- **进阶阅读**：打开 `mma/sm90.cuh` 的 `BF16MMA`/`TF32MMARS` 与 `mma/sm100.cuh` 的 `to_umma_layout_type`，体会不同 dtype 如何复用同一套描述符构造逻辑；这些会在 u9（其它内核家族）的 BF16/TF32/HyperConnection 内核中被复用。
