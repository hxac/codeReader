# FP8 块量化原语：weight-only GEMV 与 groupwise GEMM

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 GLM5.2 每个投影的数值表示：fp8 e4m3 权重字节 + 每 128×128 块一个 f32 `weight_scale_inv`，并能写出反量化公式。
2. 解释**为什么**小批量（decode）走 weight-only GEMV、大批量（prefill / 宽 bucket）走「激活量化 + SM100 groupwise GEMM」：小 m 时投影是访存受限的，CUTLASS 的 M-tile 会把 1 行填充到 64 行，还得多付一次激活量化 kernel 和一次 TMA scale 重排。
3. 复述 `FP8_GEMV_MAX_ROWS = 8` 这条实测分界的来历（#812 的 verify-span bucket 实验：48 行跑 GEMV 让步时×3）。
4. 掌握 `pack_proj_pair` 的 gate|up / q_a|kv_a 打包惯例，以及 `Glm52Fp8GemmScratch`、`Glm52MlpScratch` 的构造约束——尤其是「scratch 构造必须发生在 CUDA Graph 捕获之前」这扇唯一的窗口。

## 2. 前置知识

**矩阵乘的三种形态。** 神经网络里的线性层 \( Y = XW^\top \) 按行数 \( m \)（本项目中叫 `rows`，一步里同时前向传播的 token 数）分三档：

| 形态 | 行数 | 瓶颈 | GLM5.2 中的场景 |
|---|---|---|---|
| GEMV | m = 1 | 访存（权重每字节只做 1 次乘加） | 单请求 decode |
| batched GEMV | 2 ≤ m ≤ 8 | 访存（权重仍然只读一遍） | 小 decode bucket |
| GEMM | m > 8 | 算力（张量核心吃满） | prefill、宽 bucket |

关键直觉：当 \( m \) 很小时，计算量 \( O(mnk) \) 里 \( m \) 是小系数，而**权重字节 \( n \times k \) 是一定要读的**——算术强度约等于每字节 1 次乘加，访存带宽才是天花板。此时用张量核心的大 tile 反而是浪费：tile 会把 1 行真实数据填充成 64 行的 tile 宽度，白白多算 63 行的量，还要求激活先量化成 fp8（多一个 kernel）并把 scale 重排成 TMA 需要的布局（再一个 kernel）。

**fp8 e4m3fn 编码。** 1 字节 = 1 符号位 + 4 指数位 + 3 尾数位，指数偏置是 7，没有 inf（e=15 且尾数非零即 NaN），支持 subnormal。它的值是：

\[
\text{val} = (-1)^s \times
\begin{cases}
2^{e-7} \times (1 + m/8), & e > 0 \\
2^{-6} \times (m/8), & e = 0
\end{cases}
\]

e4m3 能表示的最大正常数是 \( 2^8 \times 1.75 = 448 \)——这就是后面激活量化用「amax / 448」做 scale 的原因。

**块量化（blockwise scaling）。** 朴素 fp8 量化对一整行权重共用一个 scale，精度不够；GLM5.2 的 checkpoint 契约（u2-l1 讲过，probe_config_json 钉死了 fp8 e4m3 + 128x128 weight block）把权重 \( W \in \mathbb{R}^{n \times k} \) 切成 \( 128 \times 128 \) 的块，每块一个 f32 scale（checkpoint 字段名 `weight_scale_inv`，形状 \( [\lceil n/128 \rceil, \lceil k/128 \rceil] \)）。反量化即：

\[
W_{ij} \approx s_{\lfloor i/128 \rfloor,\; \lfloor j/128 \rfloor} \cdot q_{ij}, \qquad q_{ij} \in \text{e4m3}
\]

**GEMM 的激活侧。** 大 \( m \) 路径要求激活也变成 fp8：每行每 128 列一组（per-token per-128-group）求 amax，按 amax/448 定 scale，激活字节 + 每 128 列一个 f32 scale 一起喂给 GEMM kernel。这套「激活 scale + 权重 scale 都按 128 对齐分组」的方案就叫 **groupwise（块比例）GEMM**。

本讲不需要你懂 CUDA 编程，但要知道两个事实：CUDA Graph 捕获期间不允许做某些惰性初始化（比如加载 DSL AOT 模块库）；两个 stream 并发读写同一块 scratch 缓冲是数据竞争。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/fp8.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs) | 本讲主角：共享 fp8 投影原语——权重表示 `ProjWeight`、两条路径的 `fp8_linear_into` / `fp8_linear_large_m_into`、打包 `pack_proj_pair`、scratch 两件套 |
| [src/dense.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/dense.rs) | 消费者示例：前 3 层 dense MLP 就是「权重包 + 薄 forward」 |
| [src/mla_front.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs) | 另一个消费者：q_a|kv_a 水平打包（`pack_qa_kva`），u4-l1 再细讲 |
| [src/scratch.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/scratch.rs) | 整步 decode 的持久 scratch 聚合处（指针稳定性设计） |
| [src/model/mod.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs) | decode bucket 表 `GLM52_DECODE_BUCKETS` 与 GEMV 白名单启动期预检 |
| [pegainfer-kernels/src/ops/glm52/moe_gemv.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/glm52/moe_gemv.rs) | GEMV 启动器：校验、指针提取、64/96 行的 token 轴拆分 |
| [pegainfer-kernels/src/ops/glm52/moe_quant.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/glm52/moe_quant.rs) | 激活 per-token-group 量化启动器 |
| [pegainfer-kernels/src/ops/glm52/fp8_gemm.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/glm52/fp8_gemm.rs) | SM100 groupwise GEMM 启动器 + CuTe DSL AOT 预加载 |
| [pegainfer-kernels/csrc/glm52/glm52_moe_gemv.cu](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/glm52/glm52_moe_gemv.cu) | CUDA 侧实现：批量化常量、形状白名单、mma 配置表 |
| [docs/models/glm52/fp8-blockwise-gemm-lab.md](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/models/glm52/fp8-blockwise-gemm-lab.md) | kernel_lab 实验/基准记录（CUTLASS vs CuTe DSL tcgen05） |

## 4. 核心概念与源码讲解

### 4.1 fp8 e4m3 与 128 块 scale：权重在显存里的样子

#### 4.1.1 概念说明

GLM5.2 的每一个稠密投影（MLA 的 q_a/q_b/kv_a/o_proj、dense MLP、共享专家、indexer 权重）都是同一个三元组：fp8 权重字节 `[n, k]`（行主序）+ f32 块 scale 字节 `[⌈n/128⌉, ⌈k/128⌉]` + 形状 `(n, k)`。u2-l2 讲过 loader 怎么把它搬上 GPU；本讲关心它被搬上 GPU **之后**怎么用。128 这个数（`FP8_BLOCK`）是贯穿全 crate 的对齐单位：scale 块边界、GEMM 的 k%128 约束、打包/切片的 128 对齐要求，全都源于它。

#### 4.1.2 核心流程

设备驻留权重的生命周期：

```
checkpoint bytes (host)
   │  ProjWeight::upload（仅测试）/ from_device（生产 loader 路径）
   ▼
ProjWeight { weight: CudaSlice<u8>, scale: CudaSlice<u8>, n, k }
   │  （可选）pack_proj_pair 水平拼接 / slice_rows / slice_cols 切片
   ▼
每个 decode step 被两条路径之一消费（见 4.2）
```

`from_device` 与 `upload` 做同一套字节校验：`weight.len() == n*k` 且 `scale.len() == ⌈n/128⌉*⌈k/128⌉*4`——打包漂移在 Rust 边界就崩，而不是死在 kernel 深处。

#### 4.1.3 源码精读

块大小常量与主机侧 e4m3 解码：

[fp8.rs:28-35](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L28-L35) 定义 `FP8_BLOCK = 128`、GEMM workspace 32 MiB，以及本讲的标题常量 `FP8_GEMV_MAX_ROWS = 8`——注释明确说这是「批量化 GEMV 的行数上限：超过它寄存器 tile 会溢出，fp8 GEMM 路径（激活量化 + FlashInfer SM100 groupwise GEMM）胜出——在 #812 的 verify-span bucket 上实测，48 行跑 GEMV 让步时×3」。

[fp8.rs:160-172](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L160-L172) 是 OCP `float8_e4m3fn` 的纯主机解码 `e4m3_to_f32`：按符号/指数/尾数位拆字节，e=0 走 subnormal 分支 \( 2^{-6} \cdot m/8 \)。注意它的用途注释：**主机侧反量化路径（kv_b 吸收因子）用，GPU kernel 不用它**——kernel 在片上即时反量化。

权重两种形态——host 侧字节包与设备驻留：

[fp8.rs:182-197](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L182-L197) 定义 `Glm52ProjBytes`（裸字节 + 形状）和 `ProjWeight`（已驻留 GPU 的 fp8 投影），注释把契约写死：行主序 `[n,k]` 权重 + `[n/128, k/128]` f32 `weight_scale_inv`。

[fp8.rs:231-254](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L231-L254) 是生产路径 `from_device`：**零拷贝**包裹已驻留缓冲并做与 `upload` 相同的字节数校验——「打包漂移在这里崩，不在 kernel 里崩」。

128 对齐的三种切片/拼接操作（本讲只要求读懂思想，MLA TP 分片是 u4/u5 的内容）：

[fp8.rs:257-297](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L257-L297) `slice_rows`：按输出行切片，**两个边界都必须落在 128 行 scale 块网格上**，因为每块的 `weight_scale_inv` 行要跟着权重行一起走。

[fp8.rs:344-383](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L344-L383) `pack_proj_pair`：把共享同一输入的两个投影拼成 `[a.n + b.n, k]`，权重字节沿 n 直拼、scale 行沿 n 直拼；要求 `a.n % 128 == 0`，这样 b 的 scale 行仍对齐拼接后的 row/128 网格。注释点名两个客户：gate|up 与 MLA 的 q_a|kv_a（4.3 详解）。

#### 4.1.4 代码实践

**实践：用公式手工解码 5 个真实测试字节，验证你对 e4m3 的理解。**

1. **实践目标**：不靠 GPU，确认你能按位拆解 e4m3fn，并理解为什么 kernel 测试要刻意避开某些编码。
2. **操作步骤**：
   - 打开 [fp8_gemm.rs 测试里的 VALUES 表（fp8_gemm.rs:307-313）](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/glm52/fp8_gemm.rs#L307-L313)，它给出了 5 个「字节 → 期望 f32」对：`(0xc0, -2.0), (0xb8, -1.0), (0x00, 0.0), (0x38, 1.0), (0x40, 2.0)`。
   - 对每个字节按 4.1 的公式手算：s = b>>7，e = (b>>3)&0xF，m = b&0x7。例如 `0x38` = 0b0011_1000 → s=0, e=7, m=0 → \( 2^{0} \times 1.0 = 1.0 \)；`0xc0` = 0b1100_0000 → s=1, e=8, m=0 → \( -2^{1} \times 1.0 = -2.0 \)。
   - 再看 [fp8.rs:586-593](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L586-L593) 的 `synth_fp8`：合成测试字节时把 `b & 0x7F == 0x7F` 的编码改成 `0x7E`——想想为什么（提示：e=15 且 m≠0 在 e4m3fn 里是 NaN，不是数字）。
3. **需要观察的现象**：纸面推演；每个字节的公式结果应与 VALUES 表完全一致。
4. **预期结果**：5 个字节全部对上；`0x7F`/`0xFF` 是仅有的两个 NaN 编码，这就是合成器避开它们的原因。
5. 本实践为纯 CPU 纸面推演，无需运行环境。

#### 4.1.5 小练习与答案

**练习 1**：一个 `[2048, 6144]` 的 fp8 投影，权重和 scale 各占多少字节？

**答案**：权重 \( 2048 \times 6144 = 12{,}582{,}912 \) 字节（约 12 MiB）；scale \( (2048/128) \times (6144/128) \times 4 = 16 \times 48 \times 4 = 3072 \) 字节。对照 [fp8.rs:208-214](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L208-L214) 的校验公式 `n.div_ceil(128) * k.div_ceil(128) * 4`，一致。

**练习 2**：`n = 576`（kv_a 的输出维）时 scale 有几行？最后一块是整块吗？

**答案**：\( \lceil 576/128 \rceil = 5 \) 行，第 5 块只覆盖第 512~575 共 64 行，是半块。所以 `pack_proj_pair` 只要求**第一个**投影的 n 是 128 的倍数（[fp8.rs:361-365](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L361-L365)）——拼接的**起点**必须落在网格上，尾巴允许半块。

### 4.2 GEMV vs GEMM：FP8_GEMV_MAX_ROWS = 8 的路径选择

#### 4.2.1 概念说明

同一份 `ProjWeight` 有两条消费路径，分界是行数：

- **小 m：weight-only GEMV。** 激活保持 bf16 直接读进 kernel，fp8 权重在片上按块 scale 即时反量化。**省掉了激活量化 kernel、省掉了 TMA scale 重排、一个 kernel 代替三个**，且瓶颈就是权重访存本身——不再受 CUTLASS「M-tile 把 1 填到 64」的计算浪费拖累。模块头注释（[fp8.rs:1-10](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L1-L10)）把这套论证写成了 fp8.rs 的「开场白」：MLA、dense MLP、MoE 共享专家全走这些 helper，只有 EP 的多行路由专家链留在 grouped CUTLASS 路径（u5-l2）。
- **大 m：激活量化 + SM100 groupwise GEMM。** 先用 per-token-group kernel 把 bf16 激活量化成 fp8 + f32 scale，再跑 SM100（Blackwell 张量核心）块比例 GEMM。多付两个前置 kernel，但 \( m \) 大行数让张量核心的算力优势兑现。

「8」不是理论推导，是**实测**：#812 给 MTP verify-span 加了宽 bucket，发现 48 行还跑 GEMV 会让寄存器 tile 溢出、步时×3，于是划下这条线（[fp8.rs:31-35](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L31-L35)）。

#### 4.2.2 核心流程

一个投影调用进 fp8.rs 后，路径由「调用方 + 行数」共同决定：

```
投影调用 rows 行
├─ 走 Glm52MlpScratch 的（dense MLP / 共享专家，见 4.3）
│   ├─ rows ≤ 8（scratch.wide == None）→ weight-only GEMV 链
│   │     rows 1/2 ：CUDA core 位稳定路径
│   │     rows 4/8 ：白名单形状上派发张量核心 mma 路径
│   └─ rows > 8（scratch.wide == Some）→ 激活量化 + groupwise GEMM（wide 路线）
└─ 直接调 fp8_linear_into 的（MLA q_b/o_proj 等）
      全部走 GEMV；rows = 64/96 由启动器沿 token 轴拆成 32/48 两个子批
```

GEMV 侧的形状白名单是**编译期实例化**的：CUDA 文件里只有 `kBatchedGemvBatch2/4/Full(8)/16/32/48` 这些批次常量（[glm52_moe_gemv.cu:606-616](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/glm52/glm52_moe_gemv.cu#L606-L616)）和 `whitelisted_linear_shape(n, k)` 形状表（[glm52_moe_gemv.cu:624](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/glm52/glm52_moe_gemv.cu#L624)）。decode bucket 表与它形成一份双写契约——启动时有预检兜底（见下）。

GEMM 侧（wide 路线）的每步三段：

```
bf16 激活 [rows, k]
   ① per-token-group 量化：amax/448 → fp8 字节 [rows, k] + f32 scale [rows, k/128]
   ② SM100 groupwise GEMM：(fp8 激活 × fp8 权重) 各带块 scale → bf16 输出 [rows, n]
   （③ 输出直接写入调用者给定的 out 缓冲，无中间拷贝）
```

#### 4.2.3 源码精读

小 m 入口 `fp8_linear_into`：

[fp8.rs:392-409](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L392-L409) 校验 `input.len() >= rows * w.k` 后直接转发给 GEMV 启动器。文档注释交代了数值契约：rows>1 跑 weight-stationary 批量 kernel（权重仍只读一遍）；rows 1/2 是位稳定的 CUDA core 路径；rows 4/8 在胜出形状上派发张量核心 mma 路径——「每个 bucket 内确定，但与 rows=1 不位相同」，跨 bucket 的浮点分歧是整步合同接受的事实。

kernels 侧启动器与 64/96 拆分：

[moe_gemv.rs:235-269](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/glm52/moe_gemv.rs#L235-L269) 公共体先做「rows/n/k 为正 + 四块缓冲长度 + scale 长度 = ⌈n/128⌉⌈k/128⌉×4」的完整校验；partials 变体额外要求 rows ≤ 48。

[moe_gemv.rs:291-296](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/glm52/moe_gemv.rs#L291-L296) 普通（非 partials）启动对 rows=64/96 沿 token 轴拆成 `(32,0),(32,32)` / `(48,0),(48,48)` 两个子批：注释引 #813 的教训——这个宽度的寄存器 tile 会溢出；激活/输出是 token 主序的，拆分只是指针偏移。

大 m 入口 `fp8_linear_large_m_into`：

[fp8.rs:66-108](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L66-L108) 先 `ensure!`（rows%4=0、rows ≤ scratch 容量、k ≤ scratch 宽度），然后 ① 调 `glm52_fp8_per_token_group_quant_bf16_launch` 把 bf16 激活写进 scratch 的 `activation`/`activation_scale`，② 调 `glm52_fp8_groupwise_gemm_sm100_launch` 完成投影。两步都写进**同一个 scratch**——所以它必须以 `&mut` 借走 scratch。

[fp8_gemm.rs:94-111](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/glm52/fp8_gemm.rs#L94-L111) GEMM 启动器的形状约束：`m%4=0 && k%128=0`，激活 scale 长度 ≥ `m * ⌈k/128⌉`（每行每 128 列一个 f32），权重 scale 长度 ≥ `⌈n/128⌉ * ⌈k/128⌉ * 4`——两侧 scale 布局正好是 4.1 讲的两种粒度（行×列组 / 块×块）。

[moe_quant.rs:45-70](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/glm52/moe_quant.rs#L45-L70) 激活量化启动器：`Glm52MoeQuantShape { rows, width, group_size }` 强制 `group_size == 128` 且 `width % 128 == 0`。同文件 [moe_quant.rs:79-104](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/glm52/moe_quant.rs#L79-L104) 还有一个 UE8M0（scale 取 2 的幂）变体，但注释说它**只**用于 FlashMLA fp8 KV cache 写入，MoE/dense GEMM 激活保持 amax/448 scale——分清这两条量化支线，u4-l3 会再遇到 UE8M0。

bucket 表与启动期白名单预检：

[model/mod.rs:290-298](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L290-L298) `GLM52_DECODE_BUCKETS = [1, 2, 4, 8, 16, 32, 48, 64, 96]`，注释说明批量化 GEMV kernel **恰好**按这些行数实例化（`kBatchedGemvBatch*`），漂移会在启动边界崩。

[model/mod.rs:857-894](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L857-L894) 「crash-early 预检」：构建完对每个 bucket 用零填充哑元（最小白名单形状 n=128, k=6144）真的启动一次 GEMV——因为 graph 是懒捕获的，没有这个预检的话，缺模板实例化的 bucket 要等到线上第一次流量达到该 bucket 才炸。这是「启动期 fail-fast」哲学（u1-l2、u2-l1 已建立）在本模块的具体体现。

GEMM 路径内部的二级分派（读一读即可）：

[fp8_gemm.rs:123-148](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/glm52/fp8_gemm.rs#L123-L148) 在 offset=0 且 DSL AOT 表命中精确 `(m,n,k)` 时派发给 CuTe DSL tcgen05 双子——kernel_lab 实验线的产物（[fp8-blockwise-gemm-lab.md TL;DR](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/models/glm52/fp8-blockwise-gemm-lab.md#L3)）：4 个 wide 路线形状在 bucket 16–96 上换来整步 p50 **−8.7~−9.9%**（[同文档 e2e 小节](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/models/glm52/fp8-blockwise-gemm-lab.md#L111)）。分派按 bucket 确定性发生，graph 捕获与回放一致。

#### 4.2.4 代码实践

**实践：手推「bucket → 路径」路由表，并读通启动预检。**

1. **实践目标**：把 4.2.2 的分派规则落到 9 个具体 bucket 上，并理解预检在防什么故障。
2. **操作步骤**：
   - 读 [model/mod.rs:290-298](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L290-L298) 抄下 bucket 表。
   - 对每个 bucket 判定 dense MLP（走 `Glm52MlpScratch`，分界在 `FP8_GEMV_MAX_ROWS`）和 MLA 的直接 `fp8_linear_into` 调用各自走哪条路。判据：[fp8.rs:495-505](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L495-L505) 的 `wide: (rows > FP8_GEMV_MAX_ROWS).then(...)`，以及 [moe_gemv.rs:291-296](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/glm52/moe_gemv.rs#L291-L296) 的 64/96 拆分。
   - 最后读 [model/mod.rs:857-894](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L857-L894)，回答：预检为什么放在 build 尾部、为什么用零哑元就够、为什么只在 `prefill_chunk_size.is_none()`（即纯 decode 形态）时执行。
3. **需要观察的现象**：纯纸面推演（无 GPU 时不需要运行任何命令）。
4. **预期结果**：路由表如下——

| bucket | dense MLP / 共享专家（scratch.wide） | MLA 直接线性（GEMV 内部） |
|---|---|---|
| 1 | GEMV（m=1 内核） | GEMV m=1 |
| 2 | GEMV（寄存器 tile，位稳定） | GEMV rows=2 |
| 4 / 8 | GEMV（mma 路径，白名单形状） | GEMV rows=4/8 |
| 16 / 32 / 48 | **激活量化 + groupwise GEMM** | GEMV rows=16/32/48（模板已扩展，#812） |
| 64 | **GEMM** | GEMV 拆 (32+32) |
| 96 | **GEMM** | GEMV 拆 (48+48) |

   预检答案：graph 懒捕获意味着没有东西会提前触达每个 bucket，预检把「线上第一次达到该 bucket 才崩」提前到启动；零哑元足以触发模板实例化检查（不需要真实数据）；prefill-only（TP4）形态不走 decode bucket，无需预检。此结论为静态阅读所得，**待本地验证**（可在 GPU 机器上故意给 `GLM52_DECODE_BUCKETS` 加一个未实例化的行数，观察启动报错文案）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 weight-only GEMV 说「省激活量化与 TMA 重排、一个 kernel 代替三个」？被省掉的三个 kernel 分别是什么？

**答案**：大 m 路线需要 ① 激活 bf16→fp8 per-token-group 量化 kernel、② scale 布局重排（TMA 描述符要求的 relayout）、③ GEMM 本体。GEMV 路线把激活留在 bf16、scale 反量化融进 GEMV 主循环，只剩一个 kernel，且访存模式退化为「顺读权重一遍」，正好匹配小 m 时「权重带宽是天花板」的形态（[fp8.rs:1-10](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L1-L10)）。

**练习 2**：`FP8_GEMV_MAX_ROWS` 定在 8 而不是 16 或 4，依据是什么？如果换一张寄存器文件更小的卡，这个数应该怎么处理？

**答案**：依据是 #812 verify-span bucket 的实测——48 行跑 GEMV 寄存器 tile 溢出，步时×3（[fp8.rs:31-35](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L31-L35)）。它是实测分界不是理论推导，所以换硬件应重新 benchmark 再定；这也是为什么注释把它写成「measured on the #812 verify-span buckets」而不是一条数学规则。

**练习 3**：GEMM 路线的 `rows` 为什么必须 %4=0？bucket 表里有不满足的值吗？

**答案**：SM100 GEMM kernel 的 m 维按 4 对齐（[fp8_gemm.rs:94-97](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/glm52/fp8_gemm.rs#L94-L97) 与 [fp8.rs:47-49](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L47-L49) 双重强制）。bucket 表 `[1,2,4,8,16,32,48,64,96]` 中走 GEMM 的 16/32/48/64/96 全部是 4 的倍数；1/2/4/8 本来就留在 GEMV。scratch 构造处 `rows.next_multiple_of(4)`（[fp8.rs:501](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L501)）是给「行数不是 bucket 值」的调用方用的对齐保险。

### 4.3 投影打包与 scratch 约定：gate|up 惯例与捕获窗口

#### 4.3.1 概念说明

**打包惯例。** SwiGLU MLP 要算 `down(silu(gate(h)) * up(h))`，gate 和 up 共享同一输入 `h`。与其两次 GEMV 再拼接，不如在**加载期**就把两个 `[I, k]` 权重沿 n 拼成 `[2I, k]`：一次投影直接写出 `[gate | up]` 交错布局，SwiGLU kernel 天然消费这个布局，中间零拷贝。同理，MLA 的 q_a（n=2048）与 kv_a（n=576）共享 hidden 输入，可拼成 `[2624, 6144]` 一次算完。这个惯例的前提只有一个：第一个投影的 n 是 128 的倍数（scale 网格对齐，4.1 已证）。

**scratch 约定。** 整步前向被 CUDA Graph 捕获后，回放的就是录制时的**设备指针**——所以每一步的每个中间缓冲都必须在捕获前一次性分配、之后地址永不变（u1-l3 的「指针稳定性」）。fp8.rs 提供两层 scratch：

- `Glm52Fp8GemmScratch`：wide 路线的激活 fp8 字节、激活 scale、32 MiB GEMM workspace。构造函数里藏着一条关键纪律——**CuTe DSL AOT 模块加载只能发生在这里**（graph 捕获期间做库加载是非法的），所以 scratch 构造必须先于该 bucket 的捕获。
- `Glm52MlpScratch`：一个 SwiGLU 形状的完整中间缓冲（gate_up / silu_out / gemv_partial），并在 `rows > 8` 时自动内嵌一个 `Glm52Fp8GemmScratch`。**每个实例独占自己的 gemv_partial**——层前向里 ctx 流与 aux 流重叠执行，共享指针就是竞争。

#### 4.3.2 核心流程

一次 `fp8_mlp_into` 的完整决策树（dense 层 decode 每步都走它）：

```
fp8_mlp_into(gate_up, down, input, scratch, out)
│  ensure: scratch.intermediate == gate_up.n / 2   ← 唯一的跨对象 join 校验
├─ scratch.wide = Some（rows > 8）：
│    ① 激活量化 → wide.activation
│    ② GEMM(gate_up) → scratch.gate_up
│    ③ 独立 bf16 SwiGLU kernel → scratch.silu_out
│    ④ 激活量化 + GEMM(down) → out
└─ scratch.wide = None（rows ≤ 8）：
     ① GEMV partials(gate_up)：mma 路径停在 f32 k 片段和（返回 ksplit>0）
     │    ksplit==0 则走寄存器 tile，直接写 bf16 gate_up
     ② ksplit>0：融合 reduce+SiLU·mul kernel 一步吃掉片段和 → silu_out
     │    ksplit==0：独立 bf16 SwiGLU kernel → silu_out
     ③ GEMV(down, silu_out) → out
```

scratch 的构造链（生产路径）：

```
Glm52DecodeScratch::new_for_backend(bucket 行数)
  ├─ dense_mlp:  Glm52MlpScratch::new(12288, rows)
  ├─ shared_mlp: Glm52MlpScratch::new(2048, rows)
  └─ …（MLA/indexer/router 等，后续讲）
每 bucket 一套 → 构造在 CUDA Graph 捕获之前 → DSL 模块加载窗口合法
```

#### 4.3.3 源码精读

`Glm52Fp8GemmScratch` 与捕获窗口：

[fp8.rs:46-64](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L46-L64) 构造函数先 `ensure!(rows%4=0 && width%128=0)`，然后调用 `glm52_fp8_dsl_preload()`——注释原文：「Scratch 构造发生在该 bucket 的 CUDA-graph 捕获**之前**——这是 DSL AOT 模块加载唯一合法的窗口」。

[fp8_gemm.rs:25-49](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/glm52/fp8_gemm.rs#L25-L49) `glm52_fp8_dsl_preload` 用 `Once` 保证全进程只加载一次，`GLM52_FP8_DSL=0` 是 kill switch；库加载本身捕获期非法、launch 入口捕获安全，这就是窗口纪律的出处。

`Glm52MlpScratch` 的独占所有权：

[fp8.rs:464-508](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L464-L508) 字段注释写明设计动机：`intermediate` 钉死精确形状（dense 12288 与共享专家 2048 各有实例，**接错在这里崩**）；`gemv_partial`「每个 scratch 一份，ctx/aux 流重叠永远不会看到共享指针」；`wide` 在 `rows > FP8_GEMV_MAX_ROWS` 时构造，宽度取 `max(HIDDEN, intermediate)`——注释解释「gate|up 吃 hidden（k=6144），down 吃 intermediate，一份 scratch 两腿共用」。

`fp8_mlp_into` 的双路径：

[fp8.rs:527-538](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L527-L538) 入口的 `ensure!` 只校验 `s.intermediate == gate_up.n/2` 这一个 join——注释说权重内部一致性已在权重包构建处钉死，scratch 配对是本调用唯一新引入的跨对象关系。wide 分支三连发（量化 GEMM → 独立 SwiGLU → 量化 GEMM）。

[fp8.rs:544-576](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L544-L576) GEMV 分支的融合技巧：`glm52_fp8_weight_only_gemv_partials_launch` 返回 ksplit>0 时，gate|up 的 f32 片段和留在 `gemv_partial`，由 `glm52_gemv_reduce_silu_mul_launch` **一次 launch** 完成「固定顺序归约 + SiLU·mul」——注释强调与独立 reduce→silu 对「位相同」（两个和都在 SiLU 前舍入到 bf16），少一次 launch 且数值不动。

融合 reduce 的 kernels 侧：

[moe_gemv.rs:50-57](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/glm52/moe_gemv.rs#L50-L57) `GLM52_GEMV_MMA_SCRATCH_FLOATS_PER_ROW = 16 × 24576`——「max ksplit(16) × max 白名单 n（dense gate|up 24576）」，调用方用这一个常数分配，避免在 Rust 侧镜像 CUDA 的 per-shape 配置表；真正的需求量仍由启动器精确把关。

[moe_gemv.rs:59-97](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/glm52/moe_gemv.rs#L59-L97) 融合 reduce 启动器，注释同样写「与独立的 reduce → silu 对位相同」。

消费者一：dense MLP（最薄的用法）：

[dense.rs:27-33](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/dense.rs#L27-L33) `Glm52DenseMlpWeights` 就是 `gate_up: ProjWeight`（已打包 `[2I, H]`）+ `down: ProjWeight` 两个字段；[dense.rs:83-89](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/dense.rs#L83-L89) 生产构造路径 `from_device` 先按 `[I,H]/[I,H]/[H,I]` 逐个钉形状再 `pack_proj_pair(gate, up)`；[dense.rs:97-112](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/dense.rs#L97-L112) decode 前向整个函数体就是一句 `fp8_mlp_into(...)`；[dense.rs:114-127](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/dense.rs#L114-L127) prefill 前向则是三条语句的手工 wide 链（量化 GEMM → SwiGLU → 量化 GEMM）——prefill 行数天然大 m，直接走 GEMM 而不经过 `Glm52MlpScratch` 的 rows 分支。

消费者二：MLA 的 q_a|kv_a 打包：

[mla_front.rs:196-211](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L196-L211) `pack_qa_kva` 先用 `glm52_gemv_mma_routes` 查 mma 表是否路由打包形状（batch=8、n=2048+576、k=6144），无路由就返回 `None` 走分离启动——「打包启动若落进寄存器 tile 会写出没有任何消费者懂的交错布局」，所以路由查询是硬前提。[fp8.rs:606-691](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L606-L691) 的 GPU 测试 `qa_kva_pack_batch8_parity` 验证了这套等价性（kv_a 位相同、q_a 因 split 因子变化只给 1e-2 容差）。

scratch 聚合与指针稳定性总纲：

[scratch.rs:1-22](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/scratch.rs#L1-L22) 模块注释是全 crate scratch 设计的「宪法」：无每步分配器流量 + 指针稳定性是整步 CUDA Graph 的两个前提；安全规则是每个缓冲绑定唯一调用点用途、只跨层共享（78 层形状相同）。

[scratch.rs:125-126](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/scratch.rs#L125-L126) 生产构造点：`dense_mlp: Glm52MlpScratch::new(ctx, 12288, tokens)` 与 `shared_mlp: Glm52MlpScratch::new(ctx, 2048, tokens)`——同一个 struct、两个 intermediate、互不通用。

#### 4.3.4 代码实践

**实践：算一笔 scratch 显存账，体会 rows>8 的代价与收益。**

1. **实践目标**：量化理解 `wide` 字段带来的显存增量，以及 `gemv_partial` 为什么是 scratch 里的大头。
2. **操作步骤**：
   - 读 [fp8.rs:481-508](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L481-L508) 的 `Glm52MlpScratch::new`，列出全部字段及分配公式（bf16 按 2 字节、f32 按 4 字节）。
   - 手算 `Glm52MlpScratch::new(ctx, 12288, 8)`（dense MLP、bucket=8）与 `Glm52MlpScratch::new(ctx, 12288, 16)`（bucket=16）的各字段字节数与总账。
   - 对照 [scratch.rs:125-126](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/scratch.rs#L125-L126)：78 层共享这两个实例，所以这笔账每个 rank 只付一次。
3. **需要观察的现象**：纸面计算；如本机可编译，可运行 `cargo test --release -p pegainfer-glm52 --features glm52 --lib fp8` 跑 fp8.rs 内非 ignore 的单测确认编译（GPU 门是 `#[ignore]` 的，纯 CPU 环境只会跳过）。**待本地验证**。
4. **预期结果**（手算参考，bf16=2B、f32=4B）：
   - bucket=8：gate_up = 8×2×12288×2 = 393,216 B；silu_out = 8×12288×2 = 196,608 B；gemv_partial = 8×(16×24576)×4 = 12,582,912 B（约 12 MiB）；wide = None。合计约 13.2 MiB。
   - bucket=16：gate_up = 786,432 B；silu_out = 393,216 B；gemv_partial 约 25.2 MB；wide = Glm52Fp8GemmScratch(rows=16, width=max(6144,12288)=12288)：activation = 16×12288 = 196,608 B，activation_scale = 16×96×4 = 6,144 B，workspace = 32 MiB。合计约 58 MB，其中 workspace 一项就占 32 MiB。
   - 结论：跨过 8 行分界的显存代价约 +45 MB/实例（主要是 32 MiB workspace 和翻倍的 partials），换来的是宽 bucket 上 −8.7~−9.9% 的整步时延（lab 文档 e2e 实测）——这是一笔明确的工程取舍。

#### 4.3.5 小练习与答案

**练习 1**：`pack_proj_pair` 为什么必须校验 `a.k == b.k` 和 `a.n % 128 == 0`，却不要求 `b.n % 128 == 0`？

**答案**：`a.k == b.k` 因为拼接是「同一输入喂两个投影」的语义前提（gate|up 都吃 hidden，q_a|kv_a 都吃 hidden）；`a.n % 128 == 0` 保证第二个投影的 scale 行块从 128 网格上的位置开始，拼接后的 scale 张量每块仍对应正确的权重行段；`b.n` 可以是半块尾巴（如 kv_a 的 576 = 4.5×128），`div_ceil` 已处理（见 4.1 练习 2 与 [fp8.rs:361-365](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L361-L365)）。

**练习 2**：如果两个 `Glm52MlpScratch` 实例共享同一个 `gemv_partial` 缓冲，会发生什么？

**答案**：不一定是「立即算错」，而是**流重叠下的数据竞争**：ctx 流与 aux 流并发执行时（例如共享专家在 aux 流上跑、主链在 ctx 流上跑），两次 GEMV partials 写同一指针互相覆盖，结果取决于调度。设计者因此让每个 scratch 独占自己的 partial 缓冲，把「永远不会看到共享指针」变成结构事实（[fp8.rs:472-477](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L472-L477) 字段注释）。

**练习 3**：`Glm52Fp8GemmScratch::new` 里的 `glm52_fp8_dsl_preload()` 如果挪到 GEMM launch 内部（第一次调用时加载），会破坏什么？

**答案**：第一次 wide 投影发生在 graph 捕获期间，而 CUDA Graph 捕获窗口内做 DSL AOT 模块（cuLibrary 加载）是非法操作，捕获会直接失败；即使侥幸不炸，分派与捕获的一致性也失去保证。正确做法就是现在这样：scratch 构造先于捕获，库加载收进构造函数这扇「唯一合法窗口」（[fp8.rs:51-53](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L51-L53) 与 [fp8_gemm.rs:25-29](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/glm52/fp8_gemm.rs#L25-L29)）。

## 5. 综合实践

**任务：为一个 dense 层（层 0，纯 MLP 无 MoE）画出 bucket=4 与 bucket=32 两种步形状下，MLP 半场的完整 kernel 链路图并标注数据形态。**

要求：

1. 从 `glm52_dense_mlp_forward_into`（[dense.rs:97-112](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/dense.rs#L97-L112)）出发，对照 `fp8_mlp_into` 的两个分支（[fp8.rs:535-576](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L535-L576)），逐 kernel 写出：kernel 名、输入张量（形状+dtype）、输出张量（形状+dtype）。
2. bucket=4 应得到一条 3-kernel 链（GEMV partials → 融合 reduce+silu·mul → GEMV down；partials 落寄存器 tile 时为 GEMV → SwiGLU → GEMV 的变体）；bucket=32 应得到一条 5-kernel 链（量化 → GEMM → SwiGLU → 量化 → GEMM）。
3. 用 bucket=32 的链乘以 3 个 dense 层得到 dense 层每步 kernel 数，再留意其余 75 层是更重的 MoE 层（u5 展开），体会 u6-l2 要讲的「为什么必须整步 graph 化」的量级直觉。
4. 有 GPU 的话，可用 `--dump-graph-png`（u6-l3 讲）导出 bucket=32 的整步图，在图里找你列出的 kernel 节点核对顺序；无 GPU 则完成纸面链路图即可，标注「待本地验证」。

参考答案要点（bucket=32，dense MLP 半场）：

```
h [32, 6144] bf16（post-attention-norm 输出）
 ① per-token-group 量化      → act [32, 6144] u8 + act_scale [32, 48] f32
 ② SM100 groupwise GEMM      → gate_up [32, 24576] bf16（gate|up 交错）
 ③ bf16 SwiGLU               → silu [32, 12288] bf16
 ④ per-token-group 量化      → act [32, 12288] u8 + act_scale [32, 96] f32
 ⑤ SM100 groupwise GEMM      → out [32, 6144] bf16（加回残差）
```

bucket=4（假设 gate|up 形状在 mma 白名单表内，ksplit>0）：

```
h [4, 6144] bf16
 ① GEMV partials(gate_up)    → partial [ksplit, 4, 24576] f32（ksplit 由 CUDA 侧表给出）
 ② 融合 reduce+SiLU·mul      → silu [4, 12288] bf16
 ③ GEMV(down)                → out [4, 6144] bf16
```

## 6. 本讲小结

- GLM5.2 每个投影的统一数值契约：fp8 e4m3 权重 `[n,k]` + f32 `weight_scale_inv` `[⌈n/128⌉,⌈k/128⌉]`；128（`FP8_BLOCK`）是对齐宇宙的中心——scale 块、GEMM 的 k%128、打包/切片的网格全由它决定。
- 小 m（≤8 行）走 weight-only GEMV：bf16 激活直读、片上反量化，一个 kernel 替代「激活量化 + TMA 重排 + GEMM」三个，避开 CUTLASS M-tile 1→64 的填充浪费；分界 `FP8_GEMV_MAX_ROWS = 8` 来自 #812 verify-span bucket 的实测（48 行 GEMV 步时×3）。
- 大 m 走 per-token-group 激活量化（amax/448、128 列一组）+ SM100 groupwise GEMM；GEMM 内部还有 DSL tcgen05 二级分派（bucket 16–96，整步 p50 −8.7~−9.9%）。
- 路由不止一处：MLP 经 `Glm52MlpScratch.wide` 整体切路径；直接 `fp8_linear_into` 调用在 64/96 bucket 由启动器沿 token 轴拆成 32/48 子批；CUDA 侧 `kBatchedGemvBatch*` 白名单与 `GLM52_DECODE_BUCKETS` 是双写契约，启动期预检兜底。
- 打包惯例：gate|up 与 q_a|kv_a 在加载期经 `pack_proj_pair` 沿 n 拼接（首个 n 必须 128 对齐），一次投影写出 `[gate | up]` 布局，SwiGLU/融合 reduce 直接消费，零中间拷贝；融合 reduce 与独立两步位相同。
- scratch 是 graph 捕获的前置条件：`Glm52Fp8GemmScratch::new` 收拢 DSL AOT 模块加载（唯一合法窗口），`Glm52MlpScratch` 按 intermediate 精确定型且每实例独占 partials（流重叠防竞争），全部缓冲启动期一次分配、地址永不变。

## 7. 下一步学习建议

本讲给你的是「单投影原语」；接下来把镜头拉远：

1. **u3-l2（bookend）与 u3-l3（dense MLP 与 Rows）**：dense.rs 全貌、`Rows<C>` 如何用 const 泛型把缓冲宽度变成类型事实。
2. **u4-l1（MLA front）**：本讲的 `pack_proj_pair` 客户 `pack_qa_kva` 与 `fp8_linear_pair_into` 在 MLA 投影链里的完整用法，以及 2048/512+64 维度数学。
3. **u5-l2（EP MoE）**：唯一留在 grouped CUTLASS 路径的多行路由专家链，以及 UE8M0 量化支线的另一半故事。
4. **u6-l3（CUDA Graph）**：本讲反复强调的「捕获窗口」「指针稳定性」如何被 `precapture_step_graphs` 制度化。
5. 延伸阅读：[docs/models/glm52/fp8-blockwise-gemm-lab.md](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/models/glm52/fp8-blockwise-gemm-lab.md) 的 CUTLASS/DSL 基准与调参记录，以及 [pegainfer-kernels/csrc/glm52/glm52_moe_gemv.cu](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/glm52/glm52_moe_gemv.cu) 的 mma 配置表注释（想深挖 kernel 时再读）。
