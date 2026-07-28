# CUDA 模板与 GEMM 内核族

## 1. 本讲目标

本讲承接 [u7-l1 C++ 算子实现机制](u7-l1-cpp-ops.md)（`T.gemm` 如何被 `GemmNode::Lower` 降级）与 [u2-l3 计算原语：gemm 与 reduce](u2-l3-compute-primitives.md)（`T.gemm` 的 Python 语义），打开 C++ 降级后真正落地的「设备端 CUDA 模板」。

读完本讲，你应当能够：

- 看懂 `src/tl_templates/cuda/` 下按 SM 架构分发的 GEMM 模板地图，知道 `T.gemm` 最终会调用哪一族 C++ 模板。
- 理解三套张量核心指令族的封装差异：同步的 **mma**（sm75~sm120）、异步的 **wgmma**（sm90 Hopper）、Blackwell 的 **tcgen05**（sm100，累加器位于 TMEM）。
- 掌握 wgmma 相对 mma 在「同步模型」与「描述符（descriptor）」上的关键变化。
- 了解为 GEMM 服务的辅助模板：数据搬运 `copy.h`（cp.async/TMA）、同步 `barrier.h`、规约 `reduce.h`。

> 本讲几乎全部是「源码阅读型」内容：这些模板是编进 device kernel 里的头文件，本身不是可独立运行的程序。本讲的代码实践侧重**读懂差异并从生成的 CUDA 源码里验证**。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**张量核心（Tensor Core）与矩阵乘指令。** 现代 GPU 有一类专用矩阵乘加速单元，NVIDIA 称为 Tensor Core。一条「矩阵乘加」指令可以一次性完成一个小矩阵块的 \( D = A \times B + C \)。不同代际架构（SM 版本）暴露的指令不同：

- sm70（Volta）：`mma`（第一代，16×16×4）。
- sm75/sm80/sm89（Turing/Ampere/Ada）：`mma.sync`，形状如 16×8×16（fp16）/16×8×32（fp8）。
- sm90（Hopper）：`wgmma.mma_async`，一个 **warp group（128 线程）** 协作的**异步**矩阵乘。
- sm100（Blackwell）：`tcgen05.mma`，操作数是共享内存描述符、累加器在专用 **TMEM（Tensor Memory）** 里。

**同步 vs 异步。** `mma.sync` 顾名思义是同步的：线程发出指令、结果就绪后才继续。`wgmma` 是异步的：线程「投递」一批乘加、用 `warpgroup_commit_batch` + `warpgroup_wait` 显式等待，借此让搬运与计算重叠（参见 [u4-l2 软件流水线](u4-l2-software-pipeline.md)、[u4-l3 warp 特化](u4-l3-warp-specialization.md)）。

**共享内存描述符（SMEM descriptor）。** 从 Hopper 起，张量核心可以直接「从共享内存吃数据」。硬件用一个 64 位描述符把共享内存的一段区域 + 布局/swizzle 编码成一个「地址」。wgmma 的 `ss` 变体接收的就是这种描述符，而不是裸指针；Blackwell 的 tcgen05 同样用描述符作为操作数。理解这一点，是看懂 wgmma/tcgen05 模板参数「不像指针」的关键。

如果你对 CuTe（CUTLASS 的 layout/tensor 库）还陌生，只需记住：本讲的模板大量使用 `TiledMMA`、`make_tensor`、`partition_fragment_A/B` 等 CuTe 原语来把一个线程块级 tile 切分成「每条线程负责哪几个寄存器」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/tl_templates/cuda/gemm.h` | 顶层按 `__CUDA_ARCH_LIST__` 分发到具体 sm 版本的「路由头」。 |
| `src/tl_templates/cuda/gemm_mma.h` | **mma 族**：sm75~sm120 共用的 CuTe `TiledMMA` 同步实现，导出 `gemm_ss/rs/sr`。 |
| `src/tl_templates/cuda/gemm_sm70.h` | Volta（sm70）专用，基于 CUTLASS 的 `mma_tensor_op_sm70`（wmma）。 |
| `src/tl_templates/cuda/gemm_sm90.h` | **wgmma 族**：Hopper 异步 warp-group MMA，`use_wgmma` 开关可回退到 mma。 |
| `src/tl_templates/cuda/gemm_sm100.h` | **tcgen05 族**：Blackwell 的 `tcgen05.mma.ws`，累加器在 TMEM。 |
| `src/tl_templates/cuda/instruction/mma.h` | mma 指令的「数据类型/形状 → CUTLASS 实现」分发表，导出 `mma_sync`。 |
| `src/tl_templates/cuda/instruction/wgmma.h` | wgmma 指令的分发表，宏展开枚举所有支持的 (M,N,K,类型) 组合。 |
| `src/tl_templates/cuda/copy.h` | 数据搬运模板：`cp.async`、标量 `ld/st`、`cp_warp/cp_block`、NVSHMEM 远程拷贝。 |
| `src/tl_templates/cuda/copy_sm90.h` | Hopper 的 TMA（`cp.async.bulk.tensor`）搬运模板。 |
| `src/tl_templates/cuda/barrier.h`、`reduce.h` | 屏障（mbarrier/named barrier）与规约（`SumOp/MaxOp`）辅助模板。 |
| `src/op/gemm.cc` | **桥接**：`GemmNode` 在这里挑选指令族（TCGEN5MMA/WGMMA/MFMA/MMA）并拼出模板字符串。 |

## 4. 核心概念与源码讲解

### 4.1 按架构分发的 GEMM 模板地图

#### 4.1.1 概念说明

设备端 GEMM 模板不是「一份代码跑所有卡」，而是「按 SM 架构选不同的头」。原因有二：每代张量核心指令不同；swizzle 布局、对齐、线程组织也不同。TileScale 用两层分发把这件事做完：

1. **编译期头文件分发**：顶层 `gemm.h` 用预处理器宏 `__CUDA_ARCH_LIST__`（由 nvcc 在编译每个架构时给出）选 `gemm_smXX.h`。
2. **C++ 降级期指令族选择**：`src/op/gemm.cc` 的 `GemmNode::getGemmInst` 决定本次 `T.gemm` 用哪条指令（mma / wgmma / tcgen05 / mfma），并把它编码进模板字符串。

#### 4.1.2 核心流程

```text
T.gemm(A,B,C)                      # 前端 intrin（u2-l3）
   │  GemmNode::Lower (src/op/gemm.cc, u7-l1)
   ▼
getGemmInst() 选择指令族 ──► 拼模板字符串 "tl::gemm_ss<128,128,32,4,4,0,1,1,0,0,true>"
   │  发出 tl::tl_gemm(op_str, Aptr, Bptr, Cptr)
   ▼
codegen_cuda 把它打印成 C++ 调用 tl::gemm_ss<...>(pA,pB,pC)   # u7-l3
   ▼
nvcc 编译时，gemm.h 按 __CUDA_ARCH_LIST__ 选 gemm_sm90.h / gemm_mma.h / ...
```

#### 4.1.3 源码精读

顶层路由头按架构阶梯式分发：

[src/tl_templates/cuda/gemm.h:3-18](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/tl_templates/cuda/gemm.h#L3-L18) —— 按架构选头：sm≥1200 走 `gemm_sm120.h`，sm≥1000 走 `gemm_sm100.h`，sm≥900 走 `wgmma.h`+`gemm_sm90.h`，sm≥890/750/700 分别走对应头。

注意一个反直觉点：`gemm_sm80.h`、`gemm_sm89.h`、`gemm_sm120.h` 都只是几行「再导出」，它们统统 `#include "gemm_mma.h"` 后 `using tl_mma::gemm_ss/rs/sr`。也就是说 **sm75~sm120 的非异步 GEMM 共用同一份 mma 实现**，区别只在 `instruction/mma.h` 里选了哪条 `mma.sync` 指令（见 4.2）。

[src/tl_templates/cuda/gemm_sm80.h:1-9](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/tl_templates/cuda/gemm_sm80.h#L1-L9) —— sm80 头文件只是把 `gemm_mma.h` 里的三个函数引入 `tl` 命名空间。

C++ 降级侧，指令族的「裁判」是 `getGemmInst`：

[src/op/gemm.cc:129-142](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/gemm.cc#L129-L142) —— 优先级：`kTCGEN5MMA`（sm100 且满足条件）→ `kWGMMA`（Hopper 且 M≥64 且 warp 数为 4 的倍数）→ `kMFMA`（AMD CDNA）→ `kMMA`（其余 CUDA）。

两个门禁函数决定能否走高级指令：

[src/op/gemm.cc:110-127](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/gemm.cc#L110-L127) —— `allowTcgen5Mma` 要求 sm100、A/B 在 shared、C 在 `shared.tmem`，且形状有对应模板；`allowWgmma` 要求 Hopper、`m_>=64`、`num_warps%4==0` 且未在 pass_config 里禁用 WGMMA。

随后 `Lower` 据此拼模板字符串（4.2~4.4 会用到）：

[src/op/gemm.cc:523-532](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/gemm.cc#L523-L532) —— 按 A/B 是否为 fragment 选 `gemm_rs`（A 在寄存器）/`gemm_sr`（B 在寄存器）/`gemm_ss`（都在 shared）。

[src/op/gemm.cc:549-551](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/gemm.cc#L549-L551) —— 关键：对 Hopper 目标，追加一个布尔模板实参 `(gemm_inst == kWGMMA ? "true" : "false")`，这就是 `gemm_sm90.h` 里 `use_wgmma` 开关的来源——同一条 `tl::gemm_ss` 在 Hopper 上既可能走 wgmma、也可能回退到 mma。

最终模板调用经 `tl::tl_gemm` builtin 发出，由 codegen 打印为 C++（详见 u7-l3）：

[src/op/gemm.cc:570-571](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/gemm.cc#L570-L571) —— 把「模板字符串 + A/B/C 指针」四元组交给 `tl_gemm` builtin。

#### 4.1.4 代码实践

1. **目标**：确认「同一段 `T.gemm` 源码在不同架构下落到不同头文件」。
2. **步骤**：
   - 打开 `src/tl_templates/cuda/gemm.h`，对照本节表格，确认 sm80 与 sm90 的分支落在不同文件。
   - 打开 `gemm_sm80.h`，确认它只是 `using tl_mma::gemm_ss`，真正实现在 `gemm_mma.h`。
   - 在 `src/op/gemm.cc` 的 `getGemmInst` 里追踪：一块 `M=128`、threads=128 的 Hopper kernel 会返回哪个枚举？（答：`kWGMMA`。）
3. **观察现象**：你会发现「架构选择」被拆成了两步——头文件分发（nvcc 编译期）与指令族选择（TileLang 降级期），二者必须一致。
4. **预期结果**：能口述出 sm80 走 `gemm_mma.h`、sm90（满足条件）走 `gemm_sm90.h` 的 wgmma、sm100 走 `gemm_sm100.h` 的 tcgen05。

#### 4.1.5 小练习与答案

- **练习**：为什么 `gemm.h` 要用 `__CUDA_ARCH_LIST__ >= 900` 而不是 `__CUDA_ARCH__ >= 900`？
- **答案**：TileScale 一次可能为多个架构编译同一份 fatbin（`-arch=compute_XX` 列表）。`__CUDA_ARCH_LIST__` 反映「本编译单元覆盖的最高架构」，便于在头文件层做一次性分发；`__CUDA_ARCH__` 是 per-pass 的、device 函数内部才用。

---

### 4.2 mma 指令模板：同步的 mma.sync 路径

#### 4.2.1 概念说明

mma 族是 sm75~sm120 的「基线」张量核心实现，**同步**：每条 `mma.sync` 指令由一个 warp（32 线程）协作完成一个小矩阵乘，结果直接写回每个线程持有的寄存器（fragment）。TileScale 用 CuTe 的 `TiledMMA` 把「一个 threadblock 的 tile」组织成「多个 warp × 多条 mma 指令」。

它的接口三元组 `gemm_ss / gemm_rs / gemm_sr` 描述操作数来源：

- `ss`：A、B 都在 shared memory，先各自 `copy` 到寄存器再 `gemm`。
- `rs`：A 已经在寄存器（fragment），B 在 shared。
- `sr`：A 在 shared，B 在寄存器。

#### 4.2.2 核心流程

以 `gemm_ss` 的 `body` 为例：

```text
为 A、B 构造 swizzled shared layout + LDSM 拷贝原子
   │
partition：把 shared tile 切成每个线程的寄存器视图 tCrA/tCrB
   │
if clear_accum: clear(acc)            # 清零累加器
for k in K_tiles (unroll):
    copy(shared_A[k] -> tCrA)         # shared -> register（同步）
    copy(shared_B[k] -> tCrB)
    gemm(tiled_mma, tCrA, tCrB, acc)  # 一条 mma.sync，acc += A*B
```

整个过程没有 arrive/commit/wait——`gemm()` 返回时累加已完成。

#### 4.2.3 源码精读

指令分发表把「数据类型 + 形状」映射到 CUTLASS 的 mma 原子：

[src/tl_templates/cuda/instruction/mma.h:93-104](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/tl_templates/cuda/instruction/mma.h#L93-L104) —— fp16/bf16/int8 各自的 `mma.sync` 形状（如 `SM80_16x8x16_F32F16F16F32_TN`、`SM80_16x8x32_S32S8S8S32_TN`）。末尾的 `_TN` 表示 A 行主序、B 列主序（GEMM 的常见布局）。

[src/tl_templates/cuda/instruction/mma.h:149-163](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/tl_templates/cuda/instruction/mma.h#L149-L163) —— 对外的 `mma_sync` 模板函数：以 `<AType,BType,CType,M,N,K,...>` 查表，调用对应 `Impl::fma`（即内联 PTX 的 `mma.sync`）。

`gemm_mma.h` 里，`TL_DISPATCH_MMA` 宏把类型→指令的映射装进 `DispatchInstruction`：

[src/tl_templates/cuda/gemm_mma.h:23-31](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/tl_templates/cuda/gemm_mma.h#L23-L31) —— 宏定义：每个特化给出 `MMA = MMA_Atom<指令>` 与 `MMA_Group`（控制 N 维每 warp 覆盖多少列）。

[src/tl_templates/cuda/gemm_mma.h:42-108](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/tl_templates/cuda/gemm_mma.h#L42-L108) —— 按架构的条件分发块：sm1200 多了 `SM120_16x8x32_TN` 的 fp8 原子；sm800 起支持 fp16/bf16/tf32/int8/fp64；sm750 仅有 `SM75_16x8x8_F32F16F16F32_TN`。

`GemmTensorOp` 把这些拼成一个完整的 tile GEMM。它的类型别名揭示了对布局的精心安排：

[src/tl_templates/cuda/gemm_mma.h:280-295](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/tl_templates/cuda/gemm_mma.h#L280-L295) —— 由 `OperandTraits`（带 swizzle 的 shared layout + LDSM/Default 拷贝）与 `DispatchInstruction`（mma 原子）组装出 `TiledMMA`。`OperandTraits`（[L131-262](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/tl_templates/cuda/gemm_mma.h#L131-L262)）按位宽（16/32/8/64）和 leading_dim 对 64/32 的余数选不同 swizzle，目的是消除 bank conflict。

同步计算主循环：

[src/tl_templates/cuda/gemm_mma.h:367-376](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/tl_templates/cuda/gemm_mma.h#L367-L376) —— `clear(acc)` 后，unroll 循环里先 `copy` A、B 到寄存器，再 `gemm(mma, tCrA, tCrB, acc)`。注意这里没有任何 arrive/wait——这就是「同步」的含义。

最后是导出的自由函数：

[src/tl_templates/cuda/gemm_mma.h:458-464](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/tl_templates/cuda/gemm_mma.h#L458-L464) —— `tl::tl_mma::gemm_ss` 就是把所有模板参数填好、调 `MMA::body`。这正是 `gemm_sm80.h` 里 `using` 进来的那个符号。

#### 4.2.4 代码实践

1. **目标**：确认 mma 路径的「同步、shared→register→gemm」三段式。
2. **步骤**：在 `gemm_mma.h::body`（L336 起）里数一数：有几处 `copy(...)`、几处 `gemm(...)`、有没有出现 `arrive/wait` 字样。
3. **观察现象**：你会看到 shared→register 的 `copy` 与 `gemm` 一一配对，循环体里完全没有异步原语。
4. **预期结果**：能复述「mma 族靠显式 copy 把数据搬进寄存器，再用同步 `mma.sync` 累加」。

#### 4.2.5 小练习与答案

- **练习**：`gemm_mma.h` 里 `float` 类型的输入为什么被改写成 `tfloat32_t`？（提示：看 L272-277）
- **答案**：sm80 的张量核心做 fp32 矩阵乘时，实际用的是 TF32（tensor-float-32）指令 `SM80_16x8x8_F32TF32TF32F32_TN`——把 fp32 的尾数截断到 10 位再送入 tensor core。所以代码在类型别名里把 `float` 映射成 `tfloat32_t`，语义上对应 `T.gemm` 的 TF32 精度。

---

### 4.3 wgmma 指令模板：异步的 warp-group MMA

#### 4.3.1 概念说明

wgmma 是 Hopper（sm90）引入的**异步、warp-group 粒度**张量核心指令。关键变化有三：

1. **协作单位从 warp（32 线程）升到 warp group（128 线程）**，单条指令吞吐更高。
2. **异步**：投递 → `commit_batch` → `wait`，可与 TMA/cp.async 搬运重叠（参见 u4-l2/u4-l3）。
3. **操作数可以是共享内存描述符**：`ss` 变体让 A、B 都直接从 shared「喂」给 wgmma，无需线程先把数据 load 到寄存器。

由于操作数寻址方式的差异，wgmma 的模板签名多了一个 `use_wgmma` 布尔；为假时回退到 4.2 的 mma 实现。

#### 4.3.2 核心流程

```text
用 GMMA::ss_op_selector 选出本次 (M,N,K,类型) 对应的 GMMA 指令
   │
partition：得到 tCsA/tCsB（shared 描述符视图）、tCrA/tCrB（寄存器）
warpgroup_fence_operand(acc)        # 进入 async 前的栅栏
warpgroup_arrive()                  # 投递
if clear_accum: accumulate_ = Zero  # 本批覆盖累加器
for k_block in K:
    gemm(tiled_mma, tCrA[k], tCrB[k], acc)   # 投递一条 wgmma（异步）
    accumulate_ = One                          # 后续批次改为累加
warpgroup_commit_batch()            # 提交整批
warpgroup_wait<wg_wait>()           # 等待完成
warpgroup_fence_operand(acc)        # 退出 async
```

对比 4.2：少了 `copy`，多了 `arrive/commit/wait` 三连。

#### 4.3.3 源码精读

`gemm_sm90.h` 的 `GemmTensorOp` 用 CUTLASS 的 selector 选指令，并用 `GMMA::Major` 表达布局：

[src/tl_templates/cuda/gemm_sm90.h:32-50](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/tl_templates/cuda/gemm_sm90.h#L32-L50) —— `GmmaMajorA/B` 由 `trans_A/trans_B` 决定（MN-major 或 K-major）；shared layout 由 `ss_smem_selector` 选（满足 wgmma 的对齐要求）；`static_assert(num_warp_m % 4 == 0)` 强制 M 维以 warp-group（4 warp）为粒度。

异步主循环（`body`，即 `gemm_ss` 的落地）：

[src/tl_templates/cuda/gemm_sm90.h:59-96](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/tl_templates/cuda/gemm_sm90.h#L59-L96) —— `make_tiled_mma(GMMA::ss_op_selector<...>)` 选指令；`warpgroup_arrive()` → 循环 `gemm(...)` → `warpgroup_commit_batch()` → `warpgroup_wait<wg_wait>()`。`clear_accum` 通过 `tiled_mma.accumulate_ = GMMA::ScaleOut::Zero` 实现（首次覆盖、之后 `One` 累加）——注意这与 mma 族的 `clear(acc)` 不同，wgmma 的清零是指令的输入控制位。

`use_wgmma` 开关与约束：

[src/tl_templates/cuda/gemm_sm90.h:239-258](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/tl_templates/cuda/gemm_sm90.h#L239-L258) —— `gemm_ss` 模板带 `use_wgmma=true`（默认）。为真时一组 `static_assert` 约束：A/B 的 leading dim 必须等于默认（不支持自定义 stride）、offset 必须为 0——因为 wgmma 的描述符寻址比 mma 灵活度低。为假时退回 `tl_mma::GemmTensorOp::body`（即 4.2）。

[src/tl_templates/cuda/gemm_sm90.h:308-336](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/tl_templates/cuda/gemm_sm90.h#L308-L336) —— `gemm_sr`（A 在 shared、B 在寄存器）里 `static_assert(!use_wgmma, "wgmma doesn't support gemm_sr")`：wgmma 没有这种变体，必须回退 mma。这是 wgmma 相对 mma 在「操作数组合」上的一个硬限制。

支持的指令形状由 `instruction/wgmma.h` 宏展开枚举。`M` 固定为 64（一个 warp-group 覆盖 64 行），`N` 在 8~256 间以 8 为步长：

[src/tl_templates/cuda/instruction/wgmma.h:220-252](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/tl_templates/cuda/instruction/wgmma.h#L220-L252) —— `TL_WGMMA_FOREACH_N_FLOAT_MUL8(OP)` 把 `OP` 在 `N=8,16,…,256` 上展开。

[src/tl_templates/cuda/instruction/wgmma.h:301-343](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/tl_templates/cuda/instruction/wgmma.h#L301-L343) —— fp8（e4m3/e5m2）的 `ss` 形状定义与批量实例化。`CallWgmmaSS`/`CallWgmmaRS`（[L36-85](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/tl_templates/cuda/instruction/wgmma.h#L36-L85)）把「描述符 + 累加器寄存器数组」展开成对应 CUTLASS `Impl::fma` 的形参，说明 wgmma 的操作数确实是 64 位描述符（`uint64_t desc_a/desc_b`）。

> **wgmma vs mma 一句话总结**：mma 同步、warp 粒度、操作数在寄存器（需显式 copy）；wgmma 异步、warp-group 粒度、`ss` 操作数直接是共享内存描述符，靠 `arrive/commit/wait` 同步，且不支持自定义 stride、offset 与 `gemm_sr`。

#### 4.3.4 代码实践

1. **目标**：在生成的 CUDA 源码里找到 wgmma 的异步三连与描述符。
2. **步骤**：
   - 用 `examples/gemm/example_gemm.py`，把 `block_M=128, block_N=128, block_K=32`（满足 wgmma 条件），在一块 Hopper（sm90）卡上 `kernel.get_kernel_source()`。
   - 在输出的 `.cu` 里搜 `warpgroup_arrive`、`warpgroup_commit_batch`、`warpgroup_wait`、`wgmma`。
3. **观察现象**：你会看到 `gemm_ss` 模板被实例化，循环体里没有 `cp.async` 把 A/B 搬到寄存器，而是直接对 shared 描述符调用 wgmma。
4. **预期结果**：能指出「wgmma 的操作数是描述符、同步靠 commit/wait」这一点在 PTX/CUDA 源码上的体现。若手边没有 Hopper 卡，标注「待本地验证」并仅做源码侧对照。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 `gemm_sm90.h` 在 `use_wgmma=false` 时仍然要 `#include "gemm_mma.h"`？
- **答案**：Hopper 上并非所有 `T.gemm` 都满足 wgmma 条件（如 M<64、warp 数非 4 倍数、需要自定义 stride 或 `gemm_sr`）。这些情形需回退到 mma 实现，所以头文件里同时引入了 mma 路径。
- **练习 2**：wgmma 用 `accumulate_ = ScaleOut::Zero/One` 表达「清零/累加」，mma 用 `clear(acc)`。这两种做法的本质区别是什么？
- **答案**：`clear(acc)` 是在指令**之外**用单独语句把累加器清零；`ScaleOut::Zero/One` 是把「是否读取旧累加器」编码进 wgmma **指令本身**的控制位（D = scaleC·C + A·B）。后者是异步指令的必要设计——你不能在指令投递前后随便改寄存器。

---

### 4.4 tcgen05 指令模板：Blackwell 的 TMEM MMA

#### 4.4.1 概念说明

sm100（Blackwell）引入了 `tcgen05.mma` 指令，变化比 wgmma 更大：

- **累加器在 TMEM（Tensor Memory）**——一块专用片上存储，而不是线程寄存器。所以 `C` 的「地址」是一个 32 位 TMEM 偏移 `uint32_t tmem_c`，不是指针。
- **操作数是共享内存描述符**（`uint64_t`），与 wgmma `ss` 类似。
- **只有一条线程发射**：指令由 `elect_one_sync()` 选出的那个线程发出（`tcgen05.mma.ws.cta_group::1`），但所有线程都要参与随后的等待。
- 完成**靠 mbarrier** 通知，不是 `warpgroup_wait`。

TileScale 把它封装成 `tl::tcgen5mma_gemm_ss`，仅在 `allowTcgen5Mma` 为真（C 在 `shared.tmem` 等）时启用（见 4.1.3）。

#### 4.4.2 核心流程

```text
由 DispatchInstruction 选 SM100_MMA_* 原子（按 M=32/64/128 与类型）
   │
partition：A、B 的 shared 描述符；acc 绑定到 TMEM 地址 pC
accumulate_ = clear_accum ? Zero : One
for k_block:
    cute::gemm(tiled_mma, sA_frag[k], sB_frag[k], acc)   # 展开成 tcgen05.mma.ws PTX
    accumulate_ = One
cutlass::arch::umma_arrive(umma_bar_ptr)                 # 到 mbarrier 报到
```

#### 4.4.3 源码精读

指令原子用内联 PTX 直接写 `tcgen05.mma.ws`：

[src/tl_templates/cuda/gemm_sm100.h:18-50](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/tl_templates/cuda/gemm_sm100.h#L18-L50) —— `SM100_MMA_F16BF16_WS_SS::fma`：`elect_one_sync()` 选一个线程，发出 `tcgen05.mma.ws.cta_group::1.kind::f16 [tmem_c], desc_a, desc_b, idescE, p(scaleC), 0`。注意操作数是 `uint64_t desc_a/desc_b`（描述符）、`uint32_t tmem_c`（TMEM 地址）、`uint32_t scaleC`（对应 accumulate_）、`idescE`（指令描述符）。

[src/tl_templates/cuda/gemm_sm100.h:66-68](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/tl_templates/cuda/gemm_sm100.h#L66-L68) —— `FrgTypeC = UMMA::tmem_frg_ws_1sm<c_type>`：累加器片段类型显式是 TMEM 片段，这印证了「C 在 TMEM」。

`DispatchInstruction`（sm100）按 M 与类型选原子，M=128 用普通 `_SS`、M=32/64 用带 `.ws`（warp-specialized）变体以打满 128 lane：

[src/tl_templates/cuda/gemm_sm100.h:216-229](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/tl_templates/cuda/gemm_sm100.h#L216-L229) —— bf16/fp16 在 M∈{64,32} 时选 `SM100_MMA_F16BF16_WS_SS`。代码注释（[L380-L384](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/tl_templates/cuda/gemm_sm100.h#L380-L384)）说明：普通 tcgen05mma 在 M=64 时不能占满 128 条 lane（PTX 文档的 layout F），故采用 `.ws` 变体。

计算主循环与 mbarrier 报到：

[src/tl_templates/cuda/gemm_sm100.h:373-406](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/tl_templates/cuda/gemm_sm100.h#L373-L406) —— `body_ss`：`acc.data() = pC`（把累加器绑到传入的 TMEM 地址），`accumulate_ = clear_accum ? Zero : One`，循环 `cute::gemm(...)`，最后 `cutlass::arch::umma_arrive(umma_bar_ptr)`。注意 `body_ss` 的形参里 `accum` 是 `uint32_t`（TMEM 偏移）、还有 `umma_bar_ptr` 与运行期 `clear_accum`——这些都是 mma/wgmma 没有的。

对外的唯一入口（仅 ss）：

[src/tl_templates/cuda/gemm_sm100.h:426-436](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/tl_templates/cuda/gemm_sm100.h#L426-L436) —— `tl::tcgen5mma_gemm_ss` 把 `(pA, pB, accum, umma_bar_ptr, clear_accum)` 交给 `GemmTensorOp::body_ss`。降级侧 `gemm.cc`（[L483-L499](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/gemm.cc#L483-L499)）据此拼出 `tl::tcgen5mma_gemm_ss<M,N,K,atom_m,atom_n,atom_k,transA,transB,accum_dtype>` 并把 mbar 指针、clear_accum 作为运行期参数传入。

> **三代指令族对照**（关键差异）：

| 维度 | mma（sm75~120） | wgmma（sm90） | tcgen05（sm100） |
|------|-----------------|---------------|------------------|
| 同步模型 | 同步 | 异步 `arrive/commit/wait` | 异步，靠 `umma_arrive` + mbarrier |
| 协作单位 | warp（32） | warp-group（128） | 单线程发射 + 全体等待 |
| 操作数 | 寄存器（需先 copy） | shared 描述符（`ss`）或寄存器（`rs`） | shared 描述符 |
| 累加器 | 寄存器（rmem） | 寄存器（rmem） | **TMEM**（`uint32_t` 偏移） |
| 清零方式 | `clear(acc)` | `ScaleOut::Zero` | `ScaleOut::Zero`（UMMA） |
| 支持变体 | ss/rs/sr | ss/rs（无 sr） | 仅 ss |
| 自定义 stride/offset | 支持 | **不支持**（有 static_assert） | 由指令描述符决定 |

#### 4.4.4 代码实践

1. **目标**：识别 tcgen05 与 wgmma 在「累加器位置」上的根本差异。
2. **步骤**：对比 `gemm_sm90.h::body`（L74-76，`acc` 由 `make_rmemptr`）与 `gemm_sm100.h::body_ss`（L393-394，`acc.data() = pC` 且 `pC` 是 `uint32_t`）。
3. **观察现象**：wgmma 的累加器用 `make_rmem_ptr`（寄存器指针），tcgen05 的累加器直接绑定一个 32 位 TMEM 偏移。
4. **预期结果**：能口述「Blackwell 把累加器搬到了专用 TMEM，因此 C 必须声明在 `shared.tmem`（这也是 `allowTcgen5Mma` 要求 C 在 tmem 的原因）」。

#### 4.4.5 小练习与答案

- **练习**：`gemm_sm100.h` 里为什么要在 `elect_one_sync()` 保护下才发 PTX？
- **答案**：`tcgen05.mma.ws` 是 CTA 级指令，只需也只应由一个线程代表整个 warp-group/CTA 发射；若多线程同时发射会重复执行。`elect_one_sync()` 选出 lane 0 来发射，但后续的等待/屏障仍需全体线程参与。

---

### 4.5 辅助模板：copy（TMA/LSU）、barrier 与 reduce

#### 4.5.1 概念说明

GEMM 模板不是孤立运作的，它依赖三类辅助设备模板：

- **copy 模板**：把数据从 global 搬到 shared，或做远程/向量化拷贝。分两路：`cp.async`（Ampere LSU）与 TMA（Hopper 的 `cp.async.bulk.tensor`）。
- **barrier 模板**：为异步搬运与异步 MMA 提供同步——`__syncthreads`、named barrier、mbarrier。
- **reduce 模板**：GEMM 之外，reduce/softmax 等 kernel 需要跨线程规约，封装在 `reduce.h`。

这些模板与 [u2-l5 T.copy](u2-l5-copy-view.md) 的前端 `T.copy`、[u4-l2 软件流水](u4-l2-software-pipeline.md) 的 `cp.async`/TMA 注入 pass 直接对应。

#### 4.5.2 核心流程

```text
T.copy(global -> shared)  ──lower──►  copy.h 的 cp_async_gs（Ampere）
                                   或 copy_sm90.h 的 tma_load（Hopper + TMA 描述符）
T.gemm 之后做 reduce       ──lower──►  reduce.h 的 SumOp/MaxOp + warp 规约
异步段之间                  ──lower──►  barrier.h 的 mbarrier/named barrier
```

#### 4.5.3 源码精读

`copy.h` 提供基于 LSU（load/store unit）的 `cp.async` 与标量/向量化存取：

[src/tl_templates/cuda/copy.h:16-52](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/tl_templates/cuda/copy.h#L16-L52) —— `cp_async_commit`/`cp_async_wait` 与 `cp_async_gs<N>`：内联 `cp.async.cg/ca.shared.global`（16B 用 `cg` 缓存全局、4/8B 用 `ca` 缓存全部），可选 `L2::128B` 预取提示。这正是 u4-l2 `InjectPTXAsyncCopy` 替换的目标指令。

[src/tl_templates/cuda/copy.h:201-242](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/tl_templates/cuda/copy.h#L201-L242) —— `cp_warp`：warp 粒度的向量化拷贝，由 `UNROLL_FACTOR` 与 `enable_aggressive_vectorize`（用 `int4`=16B 宽）控制吞吐。这是 [u6-l3 CP-engine](u6-l3-cpengine-remote-copy.md) 远程拷贝 `cp_warp/cp_block` 的本地版本。

[src/tl_templates/cuda/copy.h:277-387](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/tl_templates/cuda/copy.h#L277-L387) —— `nvshmem_cp_threadgroup/warp/block` 与 `cp_block`：按 16B→8B→4B→2B→1B 逐级降宽的对齐拷贝，被分布式远程搬运复用。

Hopper 的 TMA 搬运在 `copy_sm90.h`：

[src/tl_templates/cuda/copy_sm90.h:17-27](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/tl_templates/cuda/copy_sm90.h#L17-L27) —— `tma_load`（按字节数）：`cp.async.bulk.shared::cluster.global.mbarrier::complete_tx::bytes`，搭配 mbarrier 的 `expect_tx` 完成通知（u4-l2/u4-l3）。

[src/tl_templates/cuda/copy_sm90.h:41-60](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/tl_templates/cuda/copy_sm90.h#L41-L60) —— `tma_load`（描述符版）：接收 `CUtensorMap` 描述符 + 坐标，是 `T.copy` 走 TMA 路径（[u2-l5](u2-l5-copy-view.md)）的最终落地指令。文件还提供 2D~5D 与 multicast 多播变体。

规约模板提供算子与累加类型选择：

[src/tl_templates/cuda/reduce.h:14-40](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/tl_templates/cuda/reduce.h#L14-L40) —— `AccType` 把 fp16/bf16 提升到 float 累加以提精度；`SumOp/MaxOp/MinOp/BitAndOp` 是设备端规约的合并函数对象，配合 warp shuffle 规约（`reduce.h` 后半部分）支撑 [u2-l3 reduce_*](u2-l3-compute-primitives.md) 的实现。

屏障模板（`barrier.h`）封装 named barrier 与 mbarrier，供 wgmma/tcgen05 的生产-消费同步使用；`gemm_sm90.h` 还内联了 warp 特化专用的屏障辅助：

[src/tl_templates/cuda/gemm_sm90.h:352-386](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/tl_templates/cuda/gemm_sm90.h#L352-L386) —— `warp_scheduler_barrier_sync/arrive` 与 `mma_init`：在 warp 特化（u4-l3）下，生产 warp-group 与消费 warp-group 用 named barrier（256 或 384 线程） rendezvous。

#### 4.5.4 代码实践

1. **目标**：在一份生成的 CUDA 源码里同时看到「TMA 搬运 + wgmma + mbarrier 同步」三者协作。
2. **步骤**：在 Hopper 上编译一个 `num_stages=3` 的 matmul（`examples/gemm/example_gemm.py`），`get_kernel_source()` 后分别搜 `cp.async.bulk.tensor`（TMA load）、`wgmma`（计算）、`mbarrier`（同步）。
3. **观察现象**：prologue 里若干次 TMA load + `mbarrier.arrive_expect_tx`，循环体里 `wgmma` 与下一轮 TMA 交错，`mbarrier.wait` 保护读写。
4. **预期结果**：能把 4.5 的三个辅助模板与 [u4-l2 软件流水](u4-l2-software-pipeline.md) 的 prologue/body/epilogue 三段对上。若无 Hopper，标注「待本地验证」并仅做源码对照。

#### 4.5.5 小练习与答案

- **练习**：`copy.h` 的 `cp_async_gs` 与 `copy_sm90.h` 的 `tma_load` 都做 global→shared 搬运，本质区别是什么？
- **答案**：`cp.async` 是 **LSU、线程发起、按地址** 的搬运，每个线程搬一小段；TMA 是 **TMA 引擎、单线程（通常 elect_one）发起、按张量描述符 + 坐标** 的搬运，硬件自动处理多维寻址与 swizzle，吞吐与维度无关，且天然配合 mbarrier 异步完成。前者普适（sm80+），后者仅 Hopper（sm90+）。

## 5. 综合实践

把本讲三族指令串起来，做一次「端到端追踪」：

1. 选 `examples/gemm/example_gemm.py`，把参数设为 `M=N=K=1024, block_M=128, block_N=128, block_K=32, dtype=fp16, accum_dtype=fp32`，`num_stages=3`。
2. **降级侧追踪**：在 `src/op/gemm.cc::GemmNode::Lower` 设断点或加日志，记录 `getGemmInst` 的返回值与拼出的模板字符串（应形如 `tl::gemm_ss<128,128,32,4,4,0,1,1,0,0,true,...>`）。
3. **模板侧对照**：根据你的卡（sm80 / sm90 / sm100），分别预期会命中：
   - sm80（如 A100）：`gemm_mma.h::body`，源码里有 `mma.sync` 与 shared→register 的 `copy`。
   - sm90（如 H100）：`gemm_sm90.h::body`，源码里有 `wgmma.mma_async` + `warpgroup_commit_batch/wait`，无寄存器 copy。
   - sm100（如 B200）：`gemm_sm100.h::body_ss`，源码里有 `tcgen05.mma.ws`，累加器在 TMEM。
4. **辅助模板验证**：在同一份源码里标注出 TMA/cp.async 搬运、mbarrier 同步出现的位置，画出「搬 A/B → 等搬运 → wgmma/tcgen05 → 等计算」的时序。
5. **产出**：一张表，三列分别是「架构 / 命中的 `gemm_*.h` / 关键 PTX 助记符」，并用一句话解释为什么 wgmma 不需要 shared→register 的 copy 而 mma 需要。

> 若手边只有一种架构的卡，对另两种架构做「源码阅读型」分析即可，并在结论里标注「待本地验证」。

## 6. 本讲小结

- GEMM 设备模板按 `__CUDA_ARCH_LIST__` 在 `gemm.h` 分发；sm75~sm120 共用 `gemm_mma.h`，sm90 用 `gemm_sm90.h`，sm100 用 `gemm_sm100.h`。
- 降级侧 `GemmNode::getGemmInst` 按 `TCGEN5MMA → WGMMA → MFMA → MMA` 选指令族，并把选择编码进 `tl::gemm_ss/rs/sr` 或 `tl::tcgen5mma_gemm_ss` 的模板字符串。
- **mma 族**同步、warp 粒度、操作数在寄存器（需先 `copy`），支持 ss/rs/sr 全部变体。
- **wgmma 族**异步、warp-group 粒度、`ss` 操作数是 shared 描述符，靠 `arrive/commit/wait` 同步；不支持自定义 stride/offset，也没有 `sr` 变体。
- **tcgen05 族**更进一步：操作数是描述符、**累加器在 TMEM**、单线程发射、靠 mbarrier 通知，仅 `ss` 变体。
- `copy.h`/`copy_sm90.h`（cp.async/TMA）、`barrier.h`（mbarrier/named barrier）、`reduce.h`（SumOp/MaxOp）是支撑 GEMM 的三类辅助模板，分别对应搬运、同步、规约。

## 7. 下一步学习建议

- 阅读 [u7-l3 目标后端 codegen 深入](u7-l3-codegen-internals.md)，看 `tl_gemm` builtin 如何被 `codegen_cuda.cc` 打印成 `tl::gemm_ss<...>(pA,pB,pC)` 的 C++ 调用——补上「模板字符串 → 设备源码」的最后一环。
- 结合 [u4-l3 Warp 特化与 Hopper wgmma](u4-l3-warp-specialization.md)，理解 `gemm_sm90.h` 里 `warp_scheduler_barrier_*`/`mma_init` 在生产-消费模型里的角色。
- 若对 sm100 感兴趣，可读 `src/tl_templates/cuda/tcgen_05_ld.h` 与 `copy_sm100.h`，了解 TMEM 分配与 Blackwell 专用搬运模板（多为「待确认」的进阶材料）。
- 想验证本讲结论，最直接的方式是用 `kernel.get_kernel_source()` 对照不同架构下生成的 PTX/CUDA，这也是后续贡献新算子模板时的标准调试手段。
