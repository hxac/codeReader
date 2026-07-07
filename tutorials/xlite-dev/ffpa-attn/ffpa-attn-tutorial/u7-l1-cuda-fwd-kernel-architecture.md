# CUDA 前向 kernel 架构（prefill / mma / tma / swizzle）

## 1. 本讲目标

本讲是「手写 CUDA 后端」单元的第一篇。前三单元已经讲清了 FFPA 的 Python 分发层（`ffpa_attn_func` → `FFPAAttnFunc` → 四后端）和默认的 Triton 后端。本讲跨过 Python 边界，进入 `csrc/cuffpa` 下的**手写 CUDA 前向 kernel**——也就是在 `ENABLE_FFPA_CUDA_IMPL=1` 编译出 `ffpa_attn._C` 后才会被 `CUDABackend` 选中的那条路径。

读完本讲，你应该能够：

- 说出 CUDA 前向有哪几套 kernel 模板、分别服务于什么形状（大 head_dim prefill / 小 head_dim / decode）。
- 在源码里定位「KV 块主循环」「Split-D 的 D 维内层循环」「online softmax」「P@V 累加」四段代码，并画出 `g2s → s2r → MMA` 的数据流水线。
- 解释 `mma.cuh` 用内联 PTX 封装了哪些 `mma.sync` 与 `ldmatrix.sync` 指令、为什么 bf16 只能走 fp32 累加。
- 区分 `cp_async.cuh`（Ampere 的 `cp.async`）与 `tma.cuh`（Hopper 的 TMA bulk-copy）两套异步加载机制，以及 `swizzle.cuh` 如何消除 SMEM bank 冲突。

本讲只讲**前向**。反向 kernel、`env.py` 的构建配置、每个 head_dim 的代码生成与 C++ pybind 分发分别留给后续讲义（u7-l2/u7-l3/u7-l4）。

## 2. 前置知识

在进入 CUDA 源码前，先用最朴素的语言对齐几个 GPU 概念。已熟悉的读者可以跳过。

- **GPU 内存层级**：一张 GPU 卡上，每个线程能直接、最快访问的是**寄存器（register）**；一个线程块（block）内所有线程共享一块**共享内存（shared memory，SRAM/SMEM）**；所有线程都能访问但最慢的是**全局内存（global memory，gmem/显存）**。kernel 写优化的核心，就是把数据从慢的 gmem 经 SMAM 搬到寄存器，再算。习惯上把 `gmem → SRAM` 的搬运记作 **g2s**，`SRAM → register` 记作 **s2r**，`register → gmem` 记作 **r2g**。
- **warp 与 MMA**：GPU 以 32 个线程组成的 **warp** 为基本调度单位。Ampere（SM80）起，硬件提供 `mma.sync`（Matrix Multiply-Accumulate）指令，一条指令让一个 warp 完成一小块矩阵乘（如 16×8×16），结果碎片化地分布在 warp 内 32 个线程的寄存器里。配套的 `ldmatrix.sync` 指令专门把 SRAM 里的数据按 MMA 期望的碎片布局加载进寄存器。
- **SRAM bank 冲突与 swizzle**：SRAM 分成 32 个 bank，同一个 warp 的线程若同时访问同一 bank 会被串行化（bank conflict）。**swizzle** 是一种用地址位 XOR 打乱列顺序的手法，让同一行不同列错落到不同 bank，从而无冲突访问。
- **异步拷贝（cp.async / TMA）**：`cp.async` 让数据从 gmem 直达 SRAM 而不经过寄存器，且可与计算重叠；Hopper（SM90）进一步提供 **TMA（Tensor Memory Accelerator）**，一条指令搬运一整块（tile）二维张量。两者都靠 `commit_group/wait_group` 或 mbarrier 来确认完成。
- **online softmax**：经典 softmax 需要先扫一遍求 max、再扫一遍求 exp 与求和。FlashAttention 用「在线」算法把分块后的 max 与求和滚动更新，做到一次遍历——本讲的 CUDA kernel 完全沿用这套机制（详见 u4-l1）。
- **MMA fragment（碎片）**：一条 `m16n8k16` 的 MMA，其 16×8 的输出 C 并非整齐存在某个线程里，而是按 PTX 规定的「碎片布局」分散在 32 个线程的寄存器中（每个线程持有若干 `uint32_t`，里面打包了两个 fp16/bf16）。本讲很多代码就是在按这套布局寻址。

> ⚠️ FFPA 的 CUDA 后端**只实现前向**，且**默认不编译**——必须在构建时设 `ENABLE_FFPA_CUDA_IMPL=1` 才会生成 `ffpa_attn._C`（回顾 u1-l2、u3-l1）。没有编译时，`CUDABackend` 在运行时会被 `CUDA_FWD_AVAILABLE` 短路为不可用。

## 3. 本讲源码地图

本讲涉及的全部在 `csrc/cuffpa/` 下，五个文件构成一条清晰的分层：

| 文件 | 角色 | 本讲关注点 |
|---|---|---|
| `ffpa_attn_fwd.cuh` | 三套前向 kernel 模板 | 大 D Split-D 主循环、decode 两阶段、小 D persistent |
| `prefill.cuh` | kernel 用到的所有工具函数 | g2s / s2r / mask / softmax / rescale / store |
| `mma.cuh` | PTX 指令封装 | `mma.sync`、`ldmatrix.sync` 包装 |
| `cp_async.cuh` | 异步拷贝（Ampere 级） | `cp.async`、`commit/wait_group`、128b load/store |
| `tma.cuh` | 异步拷贝（Hopper 级，实验性） | TMA descriptor、mbarrier、`load_2d` |

另有 `swizzle.cuh`（被 g2s/s2r 调用）、`launch_templates.cuh`（决定用哪套 kernel 与如何 launch）作为旁证。本讲不逐行讲解 decode/小 D kernel，而是以**大 D Split-D kernel 为主线**，把流水线讲透，再用对照的方式点出另两套的差异。

## 4. 核心概念与源码讲解

### 4.1 CUDA 前向架构总览：三套 kernel 与启动分发

#### 4.1.1 概念说明

FFPA 的手写 CUDA 前向不是「一个 kernel 走天下」，而是针对三种典型形状各写了一套模板：

1. **大 head_dim prefill kernel**（`ffpa_attn_split_d_fwd_template`）：FFPA 的招牌。每个 block 拥有 `Br` 行 Q，沿 KV 序列循环；为了不让 SRAM 随 D 爆炸，它把 head_dim 方向再切成宽 16 的小片段，在 MMA 层做精细分块——这就是 **Split-D**。适用于大 D（D>128）的 prefill。
2. **小 head_dim persistent kernel**（`ffpa_attn_persistent_d_fwd_template`）：经典 FlashAttention-2 风格，把整个 Q/K/V tile 一次性驻留 SRAM/寄存器，不再对 D 做内层切分。D 越小、SMEM 越够用，这种「不切 D」的调度反而更省同步、吞吐更高。仅用于 D≤256。
3. **decode 两阶段 kernel**（`ffpa_attn_splitkv_decode_stage1/stage2_template`）：当 `Nq=1`（解码）时，Q 行太少撑不满 SM，于是沿 KV 切成多段并行，stage1 各段算「部分输出 + 局部 LSE」，stage2 用 log-sum-exp 合并。

「该用哪一套」不是 kernel 自己决定的，而是**启动器** `launch_templates.cuh` 在 host 端根据 `kHeadDim`、`Nq`、占用率启发式选定的。这是手写 CUDA 与 Triton 后端的一大区别：Triton 后端在 Python 侧用 `num_splits==1` 判定走 generic 还是 decode（回顾 u4-l3）；CUDA 后端把这条决策放进了 C++ 启动器。

#### 4.1.2 核心流程

启动器（host 端）的决策可以用下面这段伪代码概括（对应 `launch_templates.cuh` 第 461–547 行的真实分支）：

```
if kHeadDim > kMaxDForSmallDKernel:           # 大 D
    num_splits = select_decode_num_splits(占用率启发式)
    if Nq == 1 and num_splits > 1 and 无 attn_bias and 无 dropout:
        launch decode_stage1  +  decode_stage2   # split-KV 两阶段
        return
# 否则落到 prefill：
if 编译期开了 ENABLE_FFPA_PERSIST_KV_G2S 且 kHeadDim <= kMaxDForSmallDKernel:
    launch ffpa_attn_persistent_d_fwd_template  # 小 D，不切 D
else:
    launch ffpa_attn_split_d_fwd_template       # 大 D，Split-D
```

三套 kernel 的**网格与线程块布局完全一致**：block 大小为 `WARP_SIZE * kMmaTileSeqLenQ * kMmaTileSeqLenK`，grid 在 Q 行块与 `(batch, head)` 两个轴上扇出。

#### 4.1.3 源码精读

启动分发的主干在 `launch_templates.cuh`，三段关键判定如下。

[launch_templates.cuh:461-496](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L461-L496) 是 decode 路径的判定：先用 `select_decode_num_splits(...)` 算并行切分数，再在 `Nq==1 && num_splits>1` 时分配两块 fp32 scratch（`partial_out`、`chunk_lse`），分别 launch stage1 与 stage2。

[launch_templates.cuh:500-511](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L500-L511) 是统一的 launch 宏 `LAUNCH_TEMPLATE_FUNC_BASE`：先 `cudaFuncSetAttribute` 放开动态共享内存上限，再用 `<<<grid, block, smem_size_base, stream>>>` 启动。注意它把所有运行期参数（Q/K/V/O、`softmax_lse`、`Nq/Nkv/Nh/Nh_kv`、`scale`、`Tc`、`causal`、`attn_bias`、`dropout_p`、`philox_seed/offset`）原样透传给 kernel。

[launch_templates.cuh:517-547](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L517-L547) 是 prefill 的二选一：`kHeadDim <= kMaxDForSmallDKernel` 选 `ffpa_attn_persistent_d_fwd_template`，否则选 `ffpa_attn_split_d_fwd_template`，并把一长串编译期配置（acc 精度、prefetch、persist、stage、pad 等）实例化进模板。

三个 kernel 模板的签名都在 `ffpa_attn_fwd.cuh`：

- 大 D：[ffpa_attn_fwd.cuh:85-107](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh#L85-L107)
- decode stage1：[ffpa_attn_fwd.cuh:791-800](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh#L791-L800)
- decode stage2：[ffpa_attn_fwd.cuh:1095-1102](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh#L1095-L1102)
- 小 D persistent：[ffpa_attn_fwd.cuh:1288-1310](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh#L1288-L1310)（注意其 `static_assert(kHeadDim <= 256)` 与 `kStageQK==1 && kStagePV==1`，见 [ffpa_attn_fwd.cuh:1314-1315](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh#L1314-L1315)）。

C++ 侧的统一入口（被 pybind 暴露为 `ffpa_attn._C.ffpa_attn_forward`）在 [ffpa_attn_api.cc:28-30](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_api.cc#L28-L30)，它再按 `acc` 编码分发到 `ffpa_attn_fwd_fp16f16/fp16f32/bf16f32`（这部分由 u7-l3 详讲）。

#### 4.1.4 代码实践

**实践目标**：在不实际编译运行的前提下，靠源码阅读理清「给定一个形状，CUDA 前向会走哪条路径」。

**操作步骤**：

1. 打开 [launch_templates.cuh:461-547](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L461-L547)。
2. 针对下面三种用例，沿决策树判断各走哪套 kernel：
   - (a) `D=512, Nq=8192`（长 prefill，大 D）
   - (b) `D=128, Nq=8192`（小 D prefill）
   - (c) `D=512, Nq=1`（decode，假设无 mask/dropout）
3. 找到 `select_decode_num_splits` 的调用（[launch_templates.cuh:464-465](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L464-L465)），阅读它的实参，理解它是按 `Nb*Nh*div_ceil(Nq,16)` 与 `num_sms*2` 估算占用率的。

**需要观察的现象**：

- (a) 命中 `ffpa_attn_split_d_fwd_template`（大 D Split-D）。
- (b) 若编译期开了 `ENABLE_FFPA_PERSIST_KV_G2S`，命中 `ffpa_attn_persistent_d_fwd_template`（小 D，不切 D）。
- (c) 命中 decode stage1+stage2。

**预期结果**：把三套 kernel 的选择条件用自己的话写成一张「输入形状 → kernel」对照表。

> 待本地验证：若你没有 GPU 或未编译 `_C`，无法真跑；本实践为源码阅读型，重点是理解决策树，无需运行命令。

#### 4.1.5 小练习与答案

**练习 1**：为什么 decode 路径的判定里多了一条 `!has_attn_bias && !has_dropout`？

**答案**：decode 两阶段 kernel（stage1/stage2）目前没有实现 `attn_bias` 与 `dropout`，因此一旦带掩码或 dropout，启动器就**不能**走 split-KV 两阶段，只能回退到 prefill kernel（由它处理 mask/dropout）。这是手写 CUDA「按形状特化、功能裁剪」的典型取舍。

**练习 2**：三套 kernel 的网格布局是否相同？为什么 launch 宏可以共用 `LAUNCH_TEMPLATE_FUNC_BASE`？

**答案**：三套 prefill/decode-stage1 的网格在 `(Q 行块, batch*head)` 两轴上扇出、block 维度也由 `kMmaTileSeqLenQ*kMmaTileSeqLenK` 决定，参数列表一致，所以可以共用一个 launch 宏。decode stage2 的网格不同（`dim3(Nq, Nb*Nh, 1)`），它是单独 launch 的，不走该宏。

---

### 4.2 前向主循环：Split-D 大 D kernel 的 g2s → s2r → MMA 流水线

#### 4.2.1 概念说明

`ffpa_attn_split_d_fwd_template` 是本讲的主角。它解决的核心矛盾是：**head_dim 很大（如 512）时，若像经典 FA-2 那样一次把整行 D 的 Q/K/V 都塞进 SRAM，SRAM 会爆**。Split-D 的做法是把 D 方向也切成宽 16 的片段，SRAM 任意时刻只驻留 16 宽的切片，于是 **SRAM 占用与 D 无关（O(1) in D）**——这正是 FFPA 能支持 D=512/1024 的根本（回顾 u1-l1、u4-l2）。

代价是把压力转嫁到**寄存器**：D 在两次矩阵乘里身份不同。

- 在 \(S = QK^\top\) 中，D 是**归约维**，可以分段相加，最后得到一份完整的 score。
- 在 \(O = PV\) 中，D 是**输出维**，每个 D 片段对应一个独立的输出累加器，必须各存各的——于是寄存器随 D 线性增长（O(d/4)）。

整个 kernel 围绕「一个 KV 块」组织，每个 KV 块内部又嵌套「一个 D 片段」的内层循环，外层再串起 online softmax 的滚动更新。

#### 4.2.2 核心流程

单个 thread block 处理 `Br` 行 Q、遍历 `Tc` 个 KV 块。把流程浓缩成伪代码：

```
# 状态：m_old (行最大), l_old (行求和), R_D (各 D 片段的 O 累加器) —— 均初始化 -inf/0
for tile_K_seqlen in range(Tc_eff):              # 外层：遍历 KV 块
    # ===== Phase A: 算完整 score S = Q·K^T（D 被切成 D/16 片）=====
    R_S = 0
    for tile_K_d in range(D / 16):               # 内层：Split-D 归约
        Q_slice, K_slice = g2s_one_d_slice(); s2r_ldmatrix()   # g2s→s2r
        R_S += mma_m16n8k16(Q_slice, K_slice)    # 分段累加进同一块 score
    # ===== Phase B: mask + online softmax =====
    apply_kv_tail_mask(R_S); apply_causal_mask(R_S); apply_attn_bias(R_S)
    m_new = max(m_old, rowmax(R_S * scale))
    P = exp(R_S * scale - m_new)
    l_new = exp(m_old - m_new) * l_old + rowsum(P)
    # ===== Phase C: P·V（每个 D 片段一个累加器，P 被复用）=====
    for j in range(D / 16):                      # 内层：每个 V 片段
        V_slice = g2s_one_d_slice(); s2r_ldmatrix_trans()
        R_O[j] = mma_m16n8k16(P, V_slice)        # 独立累加器
    # ===== Phase D: 滚动重缩放 O、更新 m/l =====
    R_D = exp(m_old - m_new) * R_D + R_O         # 对每个 D 片段
    m_old, l_old = m_new, l_new

O_final = R_D / l_final                          # KV 循环结束后再除一次
store O, store LSE = log(l_final) + m_final
```

四个阶段对应四类计算：**算 score（Split-D 归约）→ softmax → 算 O（Split-D 输出）→ 滚动更新**。注意 Phase A 和 Phase C 都有 D 的内层循环，但语义相反：A 是「累加进同一块」，C 是「各片段各自累加」。

`g2s → s2r → MMA` 的流水线靠 **cp.async 多级缓冲（`kStageQK/kStagePV ≤ 4`）** 与 **寄存器乒乓（`kRegPipeKV`）** 实现：当前块在算 MMA 时，下一块的数据已经在异步搬入 SRAM。

#### 4.2.3 源码精读

**模板签名与 launch_bounds**：[ffpa_attn_fwd.cuh:85-96](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh#L85-L96)。注意末尾两个编译期精度旋钮 `kMmaAccFloat32QK/PV`（0=fp16 累加、1=fp32 累加）与 `kOStorageAccFloat32`，以及四类调度开关 `kPrefetchQK/PV`、`kShareSmemQKV`、`kPersistQs2r/kPersistQg2s`、`kRegPipeKV`、`kStageQK/kStagePV`、`kPadQ/K/V`。

**block 几何与 GQA 头映射**：[ffpa_attn_fwd.cuh:115-142](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh#L115-L142)。这里算出 `Br`、`Bc`、`kNumThreads`，从 `blockIdx` 解出 `(Nb_id, Nh_id)`，并用 `group_size = Nh / Nh_kv; kv_head_idx = Nh_id / group_size` 实现 GQA（与 Triton 后端 `off_hkv = off_hq // group_size` 同构，回顾 u4-l4）。

**KV 块主循环**：[ffpa_attn_fwd.cuh:233-234](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh#L233-L234) 的 `for (int tile_K_seqlen = 0; tile_K_seqlen < Tc_eff; ...)`。循环上界 `Tc_eff` 在 [ffpa_attn_fwd.cuh:222-228](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh#L222-L228) 按 causal 提前裁剪，使因果路径只多付一次比较分支。

**Phase A — Split-D 算 score**：[ffpa_attn_fwd.cuh:311-312](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh#L311-L312) 是 D 内层循环 `for (int tile_K_d = 0; tile_K_d < (kHeadDim / kMmaAtomK); ...)`；循环体先 `cp_async_qkv_g2s`（g2s）+ `commit_group/wait_group`，再 `sync_fetch_qkv_frags_s2r`（s2r），最后做 MMA。MMA 计算见 [ffpa_attn_fwd.cuh:410-442](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh#L410-L442)，按 `kMmaAccFloat32QK` 选择 `m16n8k16_abf32` 或 `m16n8k16_f16f16f16`，把各 D 片段累加进同一块 `R_S`。

**Phase B — mask + online softmax**：[ffpa_attn_fwd.cuh:506-538](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh#L506-L538)。依次 `sync_apply_kv_mask`（尾部 padding 列 → −inf）、`sync_apply_causal_mask`、`sync_apply_attn_bias`、`sync_online_safe_softmax`、`sync_apply_dropout_to_p`。

**Phase C — P@V 累加**：[ffpa_attn_fwd.cuh:578-736](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh#L578-L736)。外层 `for (j=0; j<kValTileHeadDimV; ...)` 遍历每个 V 片段，内层 `for (tile_V_Bc ...)` 遍历 Bc 方向的 MMA，每次用 `p_offset = tile_V_Bc*2` 选出 P 的对应切片复用，结果累加进独立累加器 `R_O`。

**Phase D + 收尾**：滚动重缩放在 [ffpa_attn_fwd.cuh:724-742](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh#L724-L742)（`sync_rescaling_tiling_o` + `sync_update_max_expsum`）；KV 循环结束后的最终除法 [ffpa_attn_fwd.cuh:749-754](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh#L749-L754)；O 与 LSE 的回写 [ffpa_attn_fwd.cuh:759-770](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh#L759-L770)。

**decode 两阶段**作为对照：stage1 在 [ffpa_attn_fwd.cuh:910-1046](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh#L910-L1046) 用纯 GEMV（`kUseGemv=true`，`acc[row][col]` 一维累加）算每个 KV 段的部分输出，写入 `partial_out` 与 `chunk_lse`（[ffpa_attn_fwd.cuh:1080-1085](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh#L1080-L1085)）；stage2 在 [ffpa_attn_fwd.cuh:1127-1148](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh#L1127-L1148) 用 log-sum-exp 跨段合并。这与 Triton 后端的 decode 路径（u4-l3）思想一致，但用纯向量归约实现。

#### 4.2.4 代码实践

**实践目标**：定位大 D kernel 的 KV 块循环与 online softmax 段，画出 `g2s → s2r → MMA` 流水线。

**操作步骤**：

1. 打开 [ffpa_attn_fwd.cuh:233-455](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh#L233-L455)（KV 主循环 + Phase A）。
2. 在 `for (tile_K_seqlen ...)` 内，找到三个动作的确切行：
   - g2s：`cp_async_qkv_g2s<...>(...)` + `ffpa::cp_async::commit_group()`（约 [ffpa_attn_fwd.cuh:335-340](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh#L335-L340)）。
   - s2r：`sync_fetch_qkv_frags_s2r<...>(...)`（Q 在 [ffpa_attn_fwd.cuh:353-370](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh#L353-L370)，K 在 [ffpa_attn_fwd.cuh:374-390](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh#L374-L390)）。
   - MMA：`m16n8k16_abf32` 或 `m16n8k16_f16f16f16`（[ffpa_attn_fwd.cuh:428-440](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh#L428-L440)）。
3. 画出一张数据流图：`gmem Q/K → [cp.async] → SRAM Q/K tile → [ldmatrix] → 寄存器 R_Q/R_K → [mma.sync] → 寄存器 R_S(score)`。标注 cp.async 的多级缓冲（`kStageQK`）在哪几个 `wait_group` 处同步。

**需要观察的现象**：

- D 内层循环 `for (tile_K_d ...)` 里，每次迭代只搬 16 宽的 Q/K 切片，SRAM 工作集恒定。
- `R_S`（score）在所有 D 片段上累加，最终是一份完整的 `[Br,Bc]` score。
- online softmax 段（Phase B）发生在「整块 score 算完之后、P@V 之前」。

**预期结果**：得到一张标注了行号的「单 KV 块流水线」图，能说清每一级数据在 SRAM 还是寄存器。

> 待本地验证：本实践为源码阅读型，无需 GPU。

#### 4.2.5 小练习与答案

**练习 1**：Phase A 的 `R_S` 维度是 `[1][kValTileSeqLenK][4 or 2]`，为什么第三维是 4 或 2？

**答案**：第三维对应一条 `m16n8k16` MMA 输出 C 的 fragment 寄存器数。fp32 累加（`kMmaAccFloat32QK=1`）时 C 是 fp16 输入但 fp32 累加，一个 fragment 占 4 个 `uint32_t`（每个装 1 个 fp32）；fp16 累加时占 2 个 `uint32_t`（每个装 2 个 fp16）。bf16 强制走 fp32 累加，故恒为 4。

**练习 2**：为什么 P@V 阶段（Phase C）需要为每个 D 片段配独立累加器，而 QK^T 阶段（Phase A）只需一个 score？

**答案**：在 \(S=QK^\top\) 中 D 是归约维，分块结果可相加，所以各 D 片段累加进同一块 score；在 \(O=PV\) 中 D 是输出维，每个 D 片段对应输出 O 的不同列，无法相加，只能各存各的累加器（`R_O[j]`）。这正是 Split-D 把 SRAM 压力转移到寄存器的体现。

**练习 3**：`Tc_eff` 与 `Tc` 有何区别？

**答案**：`Tc = div_ceil(Nkv, Bc)` 是 KV 块总数；`Tc_eff` 是因果模式下裁剪后的实际上界——对当前 Q 行块，超出最后一个「还含可见 key」的 KV 块直接不遍历，从而让非因果路径只多付一次比较分支、因果路径少跑无用块（见 [ffpa_attn_fwd.cuh:222-228](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh#L222-L228)）。

---

### 4.3 prefill.cuh 的组织：g2s / s2r / softmax / store 工具函数

#### 4.3.1 概念说明

`ffpa_attn_fwd.cuh` 的 kernel 主体其实「很瘦」——它只负责编排循环与状态，真正的搬数、算 softmax、回写都委托给 `prefill.cuh` 里的一组 `sync_*` / `cp_async_*` 函数。这种分层让 kernel 主体可读，也让碎片级的 PTX 寻址细节集中在一处。

`prefill.cuh` 把工作切成五类：**(1) 编译期校验**、**(2) g2s 加载**、**(3) s2r 加载**、**(4) mask 与 softmax**、**(5) rescale 与 store**。

#### 4.3.2 核心流程

| 类别 | 代表函数 | 作用 |
|---|---|---|
| 校验 | `check_large_d_compiling_states` / `check_small_d_compiling_states` | `static_assert` 所有模板不变量（如 `Br>=Bc`、stage≤4、pad 为 8 的倍数） |
| g2s | `cp_async_qkv_g2s` | 用 `cp.async` 搬一个 `BrOrBc×16` 的 Q/K/V 切片入 SRAM，按 swizzle/pad 寻址，OOB 零填充 |
| s2r | `sync_fetch_qkv_frags_s2r` | 用 `ldmatrix.sync` 把 SRAM 切片按 MMA fragment 布局载入寄存器（Q=x4、K=x2、V=x2.trans） |
| mask | `sync_apply_kv_mask` / `sync_apply_causal_mask` / `sync_apply_attn_bias` / `sync_apply_dropout_to_p` | 在 score fragment 上就地施加掩码（−inf）与 dropout |
| softmax | `sync_online_safe_softmax` | 行 max → exp(s·scale − m) → 行求和，并把 score 原地改写成 P |
| rescale/store | `sync_precompute_rescale_factors` / `sync_rescaling_tiling_o` / `sync_update_max_expsum` / `sync_rescaling_final_o` / `sync_store_o_r2g` / `sync_store_lse_r2g` | O 的滚动重缩放、m/l 更新、最终除法、O/LSE 回写 |

g2s 与 s2r 的寻址都要按 `kPad==0`（swizzle）或 `kPad>0`（padding）两种 SRAM 布局选择列地址，这是手写 CUDA「无 bank 冲突」的关键。

#### 4.3.3 源码精读

**编译期校验**：[prefill.cuh:18-68](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/prefill.cuh#L18-L68)。例如 `static_assert(kMmaAtomM==16 && kMmaAtomN==8 && kMmaAtomK==16)` 锁定 MMA atom 形状，`static_assert(Br >= Bc)` 保证 SRAM 复用，`static_assert((kPersistQg2s & kPersistQs2r)==0)` 禁止两种 Q 持久化策略同时开。

**g2s 加载**：[prefill.cuh:141-193](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/prefill.cuh#L141-L193) 的 `cp_async_qkv_g2s`。它把线程映射到 `(row, col_chunk)`，构造 SRAM 地址时用 `swizzle::permuted<kMmaAtomK>(row, col)` 或裸 `col`（由 `kSwizzle = (kPad==0)` 决定），再调 `cp_async::cp_async_zfill<16>`；`seqlen_bound` 之外的行用 `row_valid=false` 触发零填充，避免尾 tile 的越界与分支。

**s2r 加载**：[prefill.cuh:209-284](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/prefill.cuh#L209-L284) 的 `sync_fetch_qkv_frags_s2r`。按 `kTrans`/`kNumRegs` 分三条分支：Q 用 `ldmatrix_m8n8x4`（4 fragment）、K 用 `ldmatrix_m8n8x2`、V 用 `ldmatrix_m8n8x2_trans`（转置，供 P@V 按列主序消费）。SRAM 寻址同样区分 swizzle/pad。

**online softmax**：[prefill.cuh:717-788](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/prefill.cuh#L717-L788)（fp32 累加分支）。先用 `__fmaf_rn(s, scale, -m_new)` 在寄存器里算 `s·scale − m`、再 `expf` 得 P，同时累加行求和 `l_new`，并把 `R_S` 原地从 fp32 改写成 fp16/bf16 的 P 供 P@V 复用。行 max/sum 的 warp 级归约用 `warp::reduce_max/sum<float, 4>`（4 线程一组，对应 m16n8k16 的 fragment 行布局）。

**O 滚动重缩放与 m/l 更新**：`sync_update_max_expsum` 在 [prefill.cuh:979-998](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/prefill.cuh#L979-L998) 实现 FA-2 递推 \(l_{new}=\exp(m_{old}-m_{new})\cdot l_{old}+\sum P\) 与 \(m_{old}\leftarrow\max(m_{old},m_{new})\)；最终除法 `sync_rescaling_final_o` 在 [prefill.cuh:1003-1043](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/prefill.cuh#L1003-L1043) 用 `__frcp_rn(1/l)` 把 fp32 累加器缩回激活 dtype。

**回写**：`sync_store_o_r2g` 在 [prefill.cuh:1089-1159](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/prefill.cuh#L1089-L1159) 用 warp shuffle 把 fragment 重新打包成连续 128-bit 向量，再用 `st.global.v4` 一次写 128 bit，并复用 `R_Q/R_K` 寄存器作 shuffle 暂存以免额外 SRAM；`sync_store_lse_r2g` 在 [prefill.cuh:1051-1080](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/prefill.cuh#L1051-L1080) 写 `LSE = log(row_sum) + row_max`。

#### 4.3.4 代码实践

**实践目标**：跟踪一次「g2s → s2r」的数据搬运，确认 swizzle/pad 两套寻址如何切换。

**操作步骤**：

1. 打开 [prefill.cuh:141-193](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/prefill.cuh#L141-L193)。
2. 定位 `constexpr bool kSwizzle = (kPad == 0)`，理解「`kPad==0` 用 swizzle、`kPad>0` 用 padding」这条二选一。
3. 跟到 SRAM 地址表达式：`kSwizzle ? swizzle::permuted<kMmaAtomK>(load_smem_BrOrBc, load_smem_d + i) : load_smem_d + i`。
4. 再看 s2r 端 [prefill.cuh:209-284](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/prefill.cuh#L209-L284)，确认它用了**同样的** swizzle/pad 寻址——这是「写进去的列序」与「读出来的列序」必须一致的关键。

**需要观察的现象**：

- g2s 与 s2r 的 swizzle 表达式完全镜像，保证 ldmatrix 读到的 fragment 布局正确。
- OOB 行靠 `cp_async_zfill` 的 `row_valid=false` 零填充，没有 `if` 分支干扰主路径。

**预期结果**：能说清「`kPadQ=0` 时 Q 走 swizzle，`kPadQ>0` 时 Q 走 padding」对 kernel 行为的影响。

> 待本地验证：本实践为源码阅读型。

#### 4.3.5 小练习与答案

**练习 1**：`sync_online_safe_softmax` 为什么在算完 P 后要把 `R_S`「原地」从 fp32 改写成激活 dtype？

**答案**：紧接着的 P@V 用 `mma.sync`，其操作数必须是 fp16/bf16。把 score 原地改写成 P（fp16/bf16）可以复用同一批寄存器（`R_S` 既存过 score、又存 P），省去额外的 P 缓冲，减少寄存器压力。

**练习 2**：`sync_store_o_r2g` 为什么用 warp shuffle 打包，而不是直接每线程写自己的 fragment？

**答案**：MMA 输出 fragment 在 32 个线程间是碎片化分布的，单个线程持有的若干 `uint32_t` 并非 gmem 中连续的 16 字节。用 `__shfl_sync` 在 4 线程组内交换，把连续 128-bit（8 个 fp16）聚到 `lane%4==0` 的线程，再发 `st.global.v4`，才能写出对齐的 128-bit 事务，带宽最高。

---

### 4.4 mma.cuh：对 PTX mma 与 ldmatrix 指令的封装

#### 4.4.1 概念说明

`mma.cuh` 是整条 kernel 栈里最贴近硬件的一层。它把裸 PTX 汇编（`mma.sync.aligned.m16n8k16...`、`ldmatrix.sync.aligned...`）包成 C++ 函数，参数全是 `uint32_t*`（指向寄存器 fragment）。这样做有三个好处：

1. **可读**：kernel 主体调 `m16n8k16_abf32(...)` 而非塞一长串 `asm volatile`。
2. **编译期分流**：用 `if constexpr` 按 dtype/累加精度/更新模式选 PTX 变体，每个 kernel 特化只编出一条 PTX。
3. **精度约束显式化**：bf16 没有 fp16-累加的 mma 指令，必须 fp32 累加——这条硬约束在封装里用静态断言/注释钉死。

`MMAMode` 区分两种累加器初始化：`kInplaceUpdate`（D 同时作输入和输出，即 `D += A·B`）与 `kAutoZeroFill`（D 初值置 0）。

#### 4.4.2 核心流程

封装的指令族如下：

| 封装函数 | PTX 指令 | 用途 | D fragment 寄存器数 |
|---|---|---|---|
| `m16n8k16_f16f16f16` | `mma...f16.f16.f16.f16` | fp16 输入 + fp16 累加 | 2 |
| `m16n8k16_f16f16f32` | `mma...f32.f16.f16.f32` | fp16 输入 + fp32 累加 | 4 |
| `m16n8k16_bf16bf16f32` | `mma...f32.bf16.bf16.f32` | bf16 输入 + fp32 累加 | 4 |
| `m16n8k16_abf32` | 上两者的 dtype 分发 | 按 `kDataType` 选 f16 或 bf16 | 4 |
| `ldmatrix_m8n8x4` | `ldmatrix.x4.m8n8` | 加载 Q 的 4 fragment | — |
| `ldmatrix_m8n8x2` | `ldmatrix.x2.m8n8` | 加载 K 的 2 fragment | — |
| `ldmatrix_m8n8x2_trans` | `ldmatrix.x2.trans.m8n8` | 加载 V 的 2 fragment（转置） | — |

A 矩阵（Q 或 P）固定 4 个输入寄存器（`RA0..RA3`），B 矩阵（K 或 V）固定 2 个（`RB0,RB1`），对应 `m16n8k16` 的 16×16 的 A 与 16×8 的 B。

#### 4.4.3 源码精读

**fp16 累加变体**：[mma.cuh:24-52](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/mma.cuh#L24-L52) 的 `m16n8k16_f16f16f16`。`kInplaceUpdate` 把 `RD0[0]/RD1[0]` 同时放进输出和输入约束（`"=r"`/`"r"`），等价 `D += A·B`；`kAutoZeroFill` 把累加器输入写成常量 `0`。注释提示「stage=1 时 kAutoZeroFill 性能不佳」。

**bf16 变体与硬约束**：[mma.cuh:84-112](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/mma.cuh#L84-L112) 的 `m16n8k16_bf16bf16f32`。文件头注释明确：BF16 硬件只有 fp32 累加变体，不存在 bf16 累加的 mma，故 FFPA 的 BF16 路径必走此封装（`kMmaAccFloat32QK=1/PV=1`）。

**dtype 分发**：[mma.cuh:118-133](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/mma.cuh#L118-L133) 的 `m16n8k16_abf32`。`if constexpr (is_same_v<kDataType, __half>)` 选 `f16f16f32`，否则 `static_assert` 限定 bf16 并选 `bf16bf16f32`，编译期消解。

**ldmatrix 族**：[mma.cuh:135-171](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/mma.cuh#L135-L171)。`x4`（4 fragment，给 Q）、`x2`（2 fragment，给 K）、`x2.trans`（转置，给 V）。

#### 4.4.4 代码实践

**实践目标**：列出 `mma.cuh` 封装了哪些 PTX 指令，并理解 `kInplaceUpdate` 的含义。

**操作步骤**：

1. 打开 [mma.cuh:24-52](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/mma.cuh#L24-L52)。
2. 找到内联汇编字符串 `mma.sync.aligned.m16n8k16.row.col.f16.f16.f16.f16`。
3. 对照 `kInplaceUpdate` 分支里 `RD0[0]` 同时出现在 `"=r"`（输出）和 `"r"`（最后的输入）约束里——这是 PTX 内联汇编表达「读改写」的标准写法。
4. 列表统计：文件里有几条不同后缀（`f16.f16.f16.f16` / `f32.f16.f16.f32` / `f32.bf16.bf16.f32`）的 `mma`，以及 4 条 `ldmatrix`。

**需要观察的现象**：

- 同一个 `m16n8k16` 形状，因输入 dtype（f16/bf16）与累加 dtype（f16/f32）组合，编出不同 PTX。
- bf16 没有 `...f16` 累加变体。

**预期结果**：得到一张「封装函数 → PTX 后缀 → 用途」对照表。

> 待本地验证：本实践为源码阅读型。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `m16n8k16_abf32` 不直接用运行时 `if`，而用 `if constexpr`？

**答案**：每个 kernel 模板特化只对应一种 `kDataType`，运行时分支会让一个 kernel 里同时编出 f16 与 bf16 两条 PTX，浪费指令缓存且其中一条永不执行。`if constexpr` 在编译期消解，保证每个特化只编出一条 PTX。

**练习 2**：`MMAMode::kInplaceUpdate` 与 `kAutoZeroFill` 在累加器初值上有何区别？为什么 FFPA 默认用前者？

**答案**：`kInplaceUpdate` 把累加器 `D` 作为输入读入再做 `D += A·B`（读改写）；`kAutoZeroFill` 把输入当 0，等价 `D = A·B`。FFPA 的 QK/PV 都需要把上一个 D 片段的累加值累加进来（Split-D 归约、多块 KV 累加），所以默认 `kInplaceUpdate`。注释也指出 stage=1 时 `kAutoZeroFill` 性能不佳。

---

### 4.5 异步加载：cp_async.cuh（cp.async）与 tma.cuh（TMA）+ swizzle.cuh

#### 4.5.1 概念说明

把 gmem 数据搬进 SRAM 有两代硬件机制：

- **cp.async（Ampere SM80 起）**：一条指令搬 16 字节（128-bit）从 gmem 直达 SRAM，不经过寄存器；用 `commit_group/wait_group` 管理一批未完成拷贝，从而与计算重叠。FFPA 的 Q 数据通路、所有非 Hopper 路径都用它。
- **TMA（Hopper SM90 起）**：一条指令搬运一整块二维 tile，硬件自动处理越界填充、swizzle、多级 mbarrier 同步。吞吐与编程模型都更强，但只在 SM90+ 可用。FFPA 把它作为**实验性**通路（`ENABLE_FFPA_EXPERIMENTAL_TMA`），目前 Q 仍走 cp.async、K/V 可走 TMA。

两者都依赖 **swizzle** 来让 SRAM 访问无 bank 冲突。`swizzle.cuh` 的 `permuted(i,j)` 用地址位 XOR 打乱列顺序，且其位模式与 CuTe 的 `Swizzle<B,4,3>` 及 TMA 的 `SWIZZLE_32B/64B/128B` 完全一致——这意味着 cp.async 写入的 swizzled SRAM，TMA 也能以同样的字节布局直接写入，二者可互换。

#### 4.5.2 核心流程

- **cp.async 通路**：`cp_async<16>(smem_ptr, gmem_ptr)` 发射一条 128-bit 异步拷贝 → `commit_group()` 打包 → `wait_group<n>()` 等到最多剩 n 组未完成。OOB 用 `cp_async_zfill`（`src_size=0` 时硬件零填充）。128-bit 的普通 load/store 由 `ldg_sync_128b` / `stg_sync_128b`（`uint4`）承担。
- **TMA 通路**：host 端用 `make_2d_copy_desc` 构造 `CUtensorMap` 描述符（含 dtype、box 形状、swizzle、L2 promotion、OOB fill）→ device 端 `load_2d(smem, &tensor_map, x, y, barrier, bytes)` 发射 bulk-copy 并在 mbarrier 上 `arrive_tx` → 消费者用 `wait_barrier_parity(phase)` 等待（按相位翻转，避免过度 arrive）。
- **swizzle**：`permuted<kColStride>(row, col)` 返回该 chunk 应加的列偏移；`kColStride=16/32/64` 分别对应 SWIZZLE_32B/64B/128B。

#### 4.5.3 源码精读

**cp.async 基元**：[cp_async.cuh:19-44](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/cp_async.cuh#L19-L44)。`commit_group`、`wait_group<n>`、`cp_async<16>` 都是一行 `asm volatile`。注意 `cp.async.cg.shared.global.L2::128B` 的 `cg`（cache global，仅 L2 缓存）与 `L2::128B`（128 字节扇区提示）。

**零填充 OOB**：[cp_async.cuh:52-69](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/cp_async.cuh#L52-L69) 的 `cp_async_zfill`。当 `row_valid=false` 时 `copy_bytes=0`，硬件零填充目标且不发 gmem 读，从而尾 tile 无分支。

**128-bit load/store**：[cp_async.cuh:73-90](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/cp_async.cuh#L73-L90)。`ldg_sync_128b`/`stg_sync_128b` 把指针强转 `uint4*` 一次读写 16 字节，是 decode kernel 与 O 回写的主力。

**TMA 能力探测与开关**：[tma.cuh:30-43](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/tma.cuh#L30-L43)。`device_supports_tma` 判 `major>=9`；`is_experimental_tma_enabled` 读 `ENABLE_FFPA_EXPERIMENTAL_TMA` 环境变量。

**TMA descriptor**：[tma.cuh:63-96](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/tma.cuh#L63-L96)。`Copy2DDescriptorParams` 描述 minor/major 维、box 形状、swizzle、L2 promotion、OOB fill；`make_2d_copy_desc` 调 driver API `cuTensorMapEncodeTiled` 生成 `CUtensorMap`。

**TMA bulk-copy + mbarrier**：[tma.cuh:170-216](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/tma.cuh#L170-L216)。`load_2d` 由单线程（`issuer_lane`）发 `cp_async_bulk_tensor_2d` 并 `barrier_arrive_tx`；`issue_load_2d_to_dst_swizzled` 直接写入 kernel 既有的 swizzled SRAM 槽（要求 descriptor 的 swizzle 与 `kCols` 匹配）。注释说明：Q 仍用 cp.async 同步，K/V 用 `wait_barrier_parity` 同步，两套 group 计数器互不干扰（[tma.cuh:129-160](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/tma.cuh#L129-L160)）。

**swizzle**：[swizzle.cuh:74-101](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/swizzle.cuh#L74-L101) 的 `permuted`。文件头的大段 ASCII 表（[swizzle.cuh:1-50](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/swizzle.cuh#L1-L50)）直观展示了行 0–3 与行 4–7 的 bank 错落，正是 bank-conflict-free 的来源。

#### 4.5.4 代码实践

**实践目标**：对照 `swizzle.cuh` 的 ASCII 表，理解 cp.async 与 TMA 为何能共用同一套 swizzled SRAM 布局。

**操作步骤**：

1. 打开 [swizzle.cuh:1-50](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/swizzle.cuh#L1-L50)，观察行 0–3 的 bank 标记（`b 0~3` 写 0、`b 4~7` 写 8）与行 4–7（互换为 8/0）——这就是 XOR swizzle。
2. 看 [swizzle.cuh:89-99](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/swizzle.cuh#L89-L99)，确认 `kColStride=16`（SWIZZLE_32B）、`32`（64B）、`64`（128B）三种模式的 XOR 掩码。
3. 再看 [tma.cuh:183-199](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/tma.cuh#L183-L199) 的注释：TMA descriptor 的 swizzle 模式必须与 `kCols` 匹配，硬件写入的字节模式与既有 `kPad==0` 的 cp.async 路径完全一致。

**需要观察的现象**：

- 同一份 swizzled SRAM，既能被 cp.async 写入（g2s 端用 `permuted` 寻址）、被 ldmatrix 读出（s2r 端用同样的 `permuted`），也能被 TMA 直接写入。
- 这是 FFPA 能把 TMA 作为「drop-in 加速器」插入既有 cp.async 数据通路的前提。

**预期结果**：能用一句话说清「为什么 TMA 与 cp.async 可以共用 swizzled SRAM」。

> 待本地验证：本实践为源码阅读型。

#### 4.5.5 小练习与答案

**练习 1**：`cp_async_zfill` 相比「先 `if (row_valid) cp_async else 写 0`」有什么优势？

**答案**：它利用硬件支持的 `cp-size/src-size` 形式：`src_size=0` 时硬件自动零填充且不发 gmem 读，整条 warp 的指令流不产生分支（无 warp divergence），主路径性能不受影响，同时避免了 OOB gmem 地址的未定义行为。

**练习 2**：TMA 通路为什么用 `wait_barrier_parity(phase)` 而不是 `wait(arrive())`？

**答案**：TMA 的 mbarrier 初始化为 `arrive_count==1`，只由 producer 的 `barrier_arrive_tx` 信号翻转相位。若消费者用 `wait(arrive())` 会额外 arrive 一次，破坏下一相位的计数。按相位位等待（`wait_parity`）才能正确轮转；对 Blackwell 等跨代理可见性要求高的设备，还要补 `fence_proxy_async_shared_cta`（见 [tma.cuh:111-127](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/tma.cuh#L111-L127) 注释）。

---

## 5. 综合实践

把本讲四条主线串成一个源码追踪任务：**跟踪一个 KV 块在 Split-D 大 D kernel 里的完整生命周期**。

1. 从 [ffpa_attn_fwd.cuh:233](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh#L233) 的 KV 主循环进入。
2. 沿 Phase A（[ffpa_attn_fwd.cuh:311-455](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh#L311-L455)）画数据流：`cp_async_qkv_g2s`（[prefill.cuh:141-193](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/prefill.cuh#L141-L193)）→ `sync_fetch_qkv_frags_s2r`（[prefill.cuh:209-284](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/prefill.cuh#L209-L284)）→ `m16n8k16_abf32`（[mma.cuh:118-133](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/mma.cuh#L118-L133)）。
3. 标注每一步数据在哪一级存储（gmem / SRAM / 寄存器）、用的是 cp.async 还是 ldmatrix、SRAM 列地址是否经过 `swizzle::permuted`。
4. 接 Phase B 的 `sync_online_safe_softmax`（[prefill.cuh:717-788](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/prefill.cuh#L717-L788)）与 Phase C 的 P@V，最终到 Phase D 的 `sync_rescaling_tiling_o` + `sync_update_max_expsum`。
5. 产出一张「单 KV 块流水线时序图」，要求把 cp.async 多级缓冲（`kStageQK`）与寄存器乒乓（`kRegPipeKV`）的同步点（`wait_group` / `__syncthreads`）也标出来。

完成后，你应当能解释：**为什么 SRAM 占用与 D 无关、寄存器却随 D 线性增长**——这正是 Split-D 的本质，也是 FFPA 区别于经典 FA-2 的关键。

> 待本地验证：本实践以源码阅读与画图为主；若你已用 `ENABLE_FFPA_CUDA_IMPL=1` 编译并在 SM80+ GPU 上运行，可额外用 `cuda-memcheck`/Nsight Compute 观察寄存器与 SMEM 占用，与图中的标注对照。

## 6. 本讲小结

- FFPA 的 CUDA 前向有**三套 kernel 模板**：大 D 的 `ffpa_attn_split_d_fwd_template`（Split-D，主路径）、小 D 的 `ffpa_attn_persistent_d_fwd_template`（不切 D）、decode 的两阶段 `..._stage1/stage2_template`（split-KV + log-sum-exp 合并）；由 `launch_templates.cuh` 按 `kHeadDim`、`Nq`、占用率选定。
- 大 D kernel 的主循环四阶段：**算 score（Split-D 归约）→ mask + online softmax → 算 O（Split-D 输出，每片段独立累加器）→ 滚动重缩放 O 并更新 m/l**；SRAM 占用 O(1) in D、寄存器 O(d/4)。
- `prefill.cuh` 把搬数（g2s/s2r）、mask、softmax、rescale、store 拆成一组 `sync_*`/`cp_async_*` 工具函数，kernel 主体只编排循环；g2s 与 s2r 用镜像的 swizzle/pad 寻址保证 fragment 布局正确。
- `mma.cuh` 用内联 PTX 封装 `mma.sync.m16n8k16`（f16/f32/bf16-f32 三种累加）与 `ldmatrix.sync`（x4/x2/x2.trans），用 `if constexpr` 编译期消解 dtype，bf16 强制 fp32 累加。
- `cp_async.cuh` 提供 Ampere 级 `cp.async` 异步拷贝（含 OOB 零填充、128-bit load/store），`tma.cuh` 提供 Hopper 级 TMA bulk-copy（descriptor + mbarrier 相位等待），二者通过 `swizzle.cuh` 的 XOR swizzle 共用同一份 bank-conflict-free 的 SRAM 布局。

## 7. 下一步学习建议

- **u7-l2** 将深入 `swizzle.cuh`/`launch_templates.cuh` 与 SRAM/寄存器复杂度分析，量化「O(1) SRAM、O(d/4) 寄存器」的来源，并讲解 persist Q g2s/s2r 策略的权衡。
- **u7-l3** 讲解 `env.py` 如何按 `(dtype,acc)` 为每个 head_dim 生成翻译单元，以及 `ffpa_attn_api.cc` 的 pybind 统一入口如何把 `acc` 编码（0=f16、1=f32，bf16 必须 1）分发到具体 kernel——它会解释本讲看到的 `kMmaAccFloat32QK/PV` 是如何被选定的。
- **u7-l4** 讲解 `env.py` 的构建期与运行期 `FFPA_*` 变量，包括本讲出现的 `kPadQ/K/V`、`kStageQK/PV`、`ENABLE_FFPA_EXPERIMENTAL_TMA` 等开关如何被注入。
- 若想横向对比，可回看 **u4-l1/u4-l2**（Triton 后端的 online softmax 与 Split-D），体会「手写 CUDA 与 Triton 在同一算法骨架下的不同实现取舍」。
