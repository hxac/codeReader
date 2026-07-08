# SM90 sparse prefill phase1 kernel

## 1. 本讲目标

本讲深入 FlashMLA 的 **SM90 sparse prefill** 主力计算 kernel——`phase1`。

读完本讲你应该能够：

- 说清 `phase1` kernel 的对外接口（模板函数 `run_fwd_phase1_kernel`）与 host 侧启动函数 `run` 的职责。
- 读懂 kernel 主循环里的 **online softmax**：行最大值、`exp2f` 重缩放、`rL` 累加，以及双 warpgroup 之间的 seesaw 状态合并。
- 理解两个模板参数 `<D_QK, HAVE_TOPK_LENGTH>` 为何要编译期化、`fwd.cu` 如何用 `if/else` 把运行时值派发到 4 个特化。
- 解释 `instantiations/` 下 4 个 `.cu` 各自实例化哪个 `(HEAD_DIM, HAVE_TOPK_LENGTH)` 组合，以及为什么按组合拆分实例化文件。
- 把 `config.h` 里的静态常量（`D_V`、`B_H`、`B_TOPK`、`NUM_THREADS`）、smem 布局与三个 GMMA `TiledMMA` 与 kernel 行为对上号。

本讲只覆盖 SM90 sparse **prefill** 的 phase1。sparse **decode**、FP8 反量化、combine 归并分别在 u5、u4 讨论，不在这里重复。

## 2. 前置知识

在进入源码前，先回顾几条来自前置讲义的关键认知：

- **sparse attention 的语义契约（u6-l1）**：prefill 用 `indices` 张量给出每行 query 要 attend 的 K token 下标，无效索引为 `-1` 或 `>= s_kv`；`topk_length` 是每行 query 的「最左若干个有效」截断；输出三件套为 `out`、`max_logits`、`lse`，且统一用 **base-e**。`attn_sink` 只缩放 `out`、不改 `lse`；当一行 query 没有任何有效 token（lonely query）时，输出强制 `out=0`、`lse=+inf`。本讲的 kernel 就是这套契约的 GPU 实现。
- **运行时值编译期化（u2-l3）**：DISPATCH 系列宏用「立即调用的 lambda」把运行时的 `head_dim`、`bool` 标志变成 `static constexpr`，从而为每个取值生成一份独立模板特化。本讲的 `<D_QK, HAVE_TOPK_LENGTH>` 是同一个思想，只不过这里用**显式 `if/else`** 而非宏来落地的——动机完全一致。
- **base-2 内部 / base-e 对外（u4-l2、u3-l3）**：kernel 内部的 softmax 用 `exp2f` 与 `sm_scale_div_log2 = sm_scale / ln2`，避免昂贵的 `expf`；写出 `lse` 时再 `×ln2` 转回 base-e。这与 decode 的 combine kernel 完全一致。
- **GMMA 与 seesaw（u3-l2、u3-l3）**：Hopper 上两操作数都在 smem 记 `SS`、A 在寄存器记 `RS`；dense decode 把输出 O 竖切成 `O_L/O_R`，靠 `stmatrix→smem→gemm_ss` 让两个 warpgroup 交换 P。本讲的 sparse prefill kernel 沿用了同一套 `RS`(local P) + `SS`(remote P) 的 seesaw 手法，只是切法升级为二维（见 4.3）。
- **MLA 的 K/V 同源**：`SparseAttnFwdParams` 里 `kv` 同时充当 K 与 V，V 取其前 `d_v=512` 维；`d_qk` 可以是 `512`(MODEL1) 或 `576`(V3.2，多 64 维 RoPE)。

> 提示：如果你对 `exp2f(score - max)` 这类 online softmax 数学已经熟悉，可以快速跳到 4.3 看双 warpgroup 的二维 seesaw；4.1/4.2 偏接口与配置。

## 3. 本讲源码地图

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| [csrc/sm90/prefill/sparse/phase1.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/phase1.h#L1-L10) | 1–10 | 对外声明模板函数 `run_fwd_phase1_kernel<D_QK, HAVE_TOPK_LENGTH>`，是 phase1 的唯一公开入口。 |
| [csrc/sm90/prefill/sparse/config.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/config.h#L1-L147) | 1–147 | `KernelTemplate` 类：编译期常量、smem 布局、三个 GMMA `TiledMMA`、`SharedMemoryPlan`、`NamedBarriers`、以及 `devfunc`/`run` 的声明。是 kernel 的「静态蓝图」。 |
| [csrc/sm90/prefill/sparse/phase1.cuh](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/phase1.cuh#L1-L646) | 1–646 | kernel 的全部实现：device 函数 `devfunc`（主循环）、host 启动函数 `run`（TMA 描述符、grid、cluster launch）、以及薄封装 `run_fwd_phase1_kernel`。 |
| [csrc/sm90/prefill/sparse/fwd.cu](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/fwd.cu#L1-L30) | 1–30 | 唯一被 `setup.py` 编译的非实例化 `.cu`：`run_fwd_kernel` 按 `d_qk × have_topk_length` 显式派发到 4 个特化。 |
| [csrc/sm90/prefill/sparse/fwd.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/fwd.h#L1-L9) | 1–9 | 声明 `run_fwd_kernel`，供上层 `csrc/api/sparse_fwd.h` 调用。 |
| `csrc/sm90/prefill/sparse/instantiations/phase1_k{512,576}{,_topklen}.cu` | 各 ~10 行 | 4 个显式实例化文件，每个文件钉死一个 `(D_QK, HAVE_TOPK_LENGTH)` 组合，交给 NVCC 并行编译。 |

一句话串起调用链：上层接口 `sparse_fwd.h` → [`fwd.h`](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/fwd.h#L5-L9) 的 `run_fwd_kernel` → [`fwd.cu`](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/fwd.cu#L9-L28) 里 `if/else` 选特化 → [`phase1.h`](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/phase1.h#L7-L8) 的 `run_fwd_phase1_kernel<D_QK, HAVE_TOPK_LENGTH>` → [`phase1.cuh`](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/phase1.cuh#L574-L639) 的 `KernelTemplate::run` 启动 kernel。

## 4. 核心概念与源码讲解

### 4.1 phase1 接口与启动函数

#### 4.1.1 概念说明

`phase1` kernel 的「接口」分两层：

1. **device 层对外接口**：模板函数 `run_fwd_phase1_kernel<D_QK, HAVE_TOPK_LENGTH>(params)`。这是 phase1 唯一被外部（`fwd.cu`）调用的函数。两个模板参数把「运行时才知道的值」提前到编译期：
   - `D_QK`：head 维度，取 `512` 或 `576`。它决定了 smem 里 K 块的 tile 数（`D_QK/64`）、QK^T 的 GEMM 次数、以及是否多算一个 RoPE tile。
   - `HAVE_TOPK_LENGTH`：调用方是否传入了 `topk_length` 张量。它决定了「每行 query 有效 token 数」是逐行动态读取（`true`）还是整批统一（`false`）。

2. **host 层启动函数**：`KernelTemplate::run(params)`。它负责把 `SparseAttnFwdParams` 翻译成 CUDA 启动参数——为 Q 构建 TMA 描述符、为 O 构建 `CUtensorMap`、申请动态 shared memory、计算 grid、调用 `cutlass::launch_kernel_on_cluster`。

把 `D_QK`/`HAVE_TOPK_LENGTH` 编译期化的直接收益是 **kernel 内部分支消失**：例如 `if constexpr (D_QK == 576)` 让 RoPE 的第 9 个 tile 在 512 维版本里根本不存在，`HAVE_TOPK_LENGTH` 的两路取值也各自生成无分支代码。这对访存密集、热路径极敏感的 attention kernel 很关键（呼应 u2-l3「把运行时值编译期化」的动机）。

#### 4.1.2 核心流程

`run(params)` 的流程：

1. **断言前置条件**：`h_kv==1`（MLA/MQA）、`topk % (2*B_TOPK)==0`（每轮处理 2 个 block、省掉边界判断）、`topk>0`、`h_q % B_H==0`。
2. **构建 Q 的 TMA**：用 `cute::make_tma_copy(SM90_TMA_LOAD{}, gQ, SmemLayoutQ{})`，把全局 Q 张量按 smem 布局烘焙成 TMA 描述符。
3. **构建 O 的 TensorMap**：调用 `cuTensorMapEncodeTiled` 生成 3D（`D_V × h_q × s_q`）的 `CUtensorMap`，供 kernel 用 TMA 异步写回 O。
4. **设动态 smem 上限**：`cudaFuncSetAttribute(..., cudaFuncAttributeMaxDynamicSharedMemorySize, smem_size)`。
5. **计算 grid 并启动**：grid 为 `((h_q/B_H)*s_q, 1, 1)`，每个 CTA 处理「一行 query × 一个 64 头组」。

启动后控制权交给 device 函数 `devfunc`，那是 4.3 的主角。

#### 4.1.3 源码精读

对外接口声明极其简洁——一个模板函数原型，藏在 `sm90::fwd` 命名空间里：

[phase1.h:L5-L8](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/phase1.h#L5-L8) 声明了 `run_fwd_phase1_kernel<D_QK, HAVE_TOPK_LENGTH>`，两个模板参数正是本讲的派发维度。

`fwd.cu` 是把这个模板「具象化」的派发器，唯一被 setup.py 编译的非实例化 `.cu`：

[fwd.cu:L9-L28](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/fwd.cu#L9-L28) 先用 `params.topk_length != nullptr` 判出 `have_topk_length`，再用两层 `if/else`（外层 `d_qk`、内层 `have_topk_length`）落到 4 个特化之一；不支持的 `d_qk` 抛 `std::runtime_error`。

注意这里**没有用 u2-l3 的 DISPATCH 宏**，而是手写 `if/else`。原因有二：组合数只有 4 个，手写更直白；且 `d_qk` 只支持 2 个离散值，显式 `throw` 比宏的默认 `TORCH_CHECK` 更贴合「枚举封闭」的语义。

host 启动函数 `run` 把 params 翻译成 TMA + grid：

[phase1.cuh:L581-L592](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/phase1.cuh#L581-L592) 为 Q 构建 TMA 描述符：`shape=(h_q, d_qk, s_q)`，步长 `(stride_q_h_q, 1, stride_q_s_q)`，目标 smem 布局为 `SmemLayoutQ`。

[phase1.cuh:L594-L615](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/phase1.cuh#L594-L615) 为输出 O 构建 3D `CUtensorMap`（box `[64, B_H, 1]`、128B swizzle），供 kernel 端用 `SM90_TMA_STORE_3D` 异步写回。

[phase1.cuh:L628-L637](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/phase1.cuh#L628-L637) 计算 grid 并启动。grid 第一维为 `(h_q/B_H)*s_q`，注释明确说明「把 `s_q` 编码进 grid.x」——因为 prefill 的 `s_q` 可能超过 65536，而 grid.y/grid.z 的上限正是 65536，所以必须折进不受此限的 grid.x。

#### 4.1.4 代码实践

**实践目标**：把「运行时值」到「编译期模板实参」的映射关系亲手跑通一遍，验证你对派发链的理解。

**操作步骤**：

1. 打开 [fwd.cu:L9-L28](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/fwd.cu#L9-L28)。
2. 构造 4 个假想的 `SparseAttnFwdParams`（只需关心 `d_qk` 与 `topk_length` 两个字段）：
   - A：`d_qk=576`、`topk_length=nullptr`（V3.2、整批统一 topk）
   - B：`d_qk=576`、`topk_length` 非空（V3.2、逐行 topk）
   - C：`d_qk=512`、`topk_length=nullptr`（MODEL1、整批统一）
   - D：`d_qk=512`、`topk_length` 非空（MODEL1、逐行）
3. 对每个用例，写出 `run_fwd_kernel` 最终调用的是 `run_fwd_phase1_kernel<?, ?>` 的哪一组模板实参。

**需要观察的现象 / 预期结果**：

| 用例 | `d_qk` | `topk_length` | 模板实参 | 对应实例化文件 |
| --- | --- | --- | --- | --- |
| A | 576 | nullptr | `<576, false>` | `phase1_k576.cu` |
| B | 576 | 非空 | `<576, true>` | `phase1_k576_topklen.cu` |
| C | 512 | nullptr | `<512, false>` | `phase1_k512.cu` |
| D | 512 | 非空 | `<512, true>` | `phase1_k512_topklen.cu` |

4. 再追问：若有人传 `d_qk=384`，会走哪条路？预期在 [fwd.cu:L25-L27](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/fwd.cu#L25-L27) 抛 `Unsupported d_qk value in sparse attention fwd kernel`。这就是「枚举封闭、不支持的值显式报错」的设计。

> 本实践为源码阅读型，无需 GPU；若想真正触发，可仿照 `tests/test_flash_mla_sparse_prefill.py` 构造输入并断点在 `run_fwd_kernel`。

#### 4.1.5 小练习与答案

**练习 1**：为什么不把 `D_QK` 也做成运行时参数、在 kernel 里用 `if (d_qk == 576)` 分支处理 RoPE tile？

**答案**：`D_QK` 决定了 QK^T 的 GEMM 次数（512→8 次、576→9 次）、smem K 块的 tile 数、以及 `if constexpr (D_QK == 576)` 处的第 9 个 tile。若改成运行时分支，热路径上每次循环都要判断且无法展开，NVCC 也无法据此做寄存器/smem 的静态规划。编译期化后，512 版本根本不含第 9 个 tile 的代码，576 版本则是无分支的固定 9 次 GEMM。

**练习 2**：`run_fwd_kernel` 里 `have_topk_length` 的判定依据是什么？为什么用「指针是否为空」而不是额外传一个 bool？

**答案**：依据是 `params.topk_length != nullptr`（[fwd.cu:L10](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/fwd.cu#L10)）。这是 FlashMLA 全家通用的「可空张量用 `nullptr` 表示禁用」约定（见 u2-l2）：不需要额外 bool，指针本身既是数据来源又是开关。对应的 `HAVE_TOPK_LENGTH` 模板参数，让 kernel 在 `false` 时完全不加载 `topk_length`、不生成逐行掩码代码。

### 4.2 config.h：静态配置与 smem/GMMA 蓝图

#### 4.2.1 概念说明

`config.h` 把整个 kernel 的「静态蓝图」集中在一个类模板 `KernelTemplate<D_QK, HAVE_TOPK_LENGTH>` 里，包含四类信息：

- **编译期常量**：tile 尺寸、线程数、初始值。
- **smem 布局**：Q/O/K/S 各自的 `SmemLayout`，用 CUTLASS 的 `GMMA::Layout_*_SW128_Atom` 消除 bank conflict。
- **三个 GMMA `TiledMMA`**：QK 用 `SS`，PV 用 `RS`(local P) 与 `SS`(remote P)——和 dense decode seesaw 完全同构。
- **`SharedMemoryPlan` 与 `NamedBarriers`**：smem 缓冲布局与 warpgroup 间点名的同步原语。

把它和 u3-l2 的 dense decode `config.h`/`traits.h` 对照看，会发现同样的「GMMA + SW128 smem + NamedBarrier」配方——这是 Hopper attention kernel 的通用骨架。

#### 4.2.2 核心流程

几个关键常量之间的关系（这是理解后续主循环的钥匙）：

- `B_H = 64`：一个 CTA 负责的 head 数（query 头组的粒度）。
- `B_TOPK = 64`：一个 K token 块含 64 个 token，也是 topk 的对齐粒度。
- `D_V = 512`：V 的维度（取 K 的前 512 维）。
- `NUM_THREADS = 128*3 = 384`：3 个 warpgroup，其中 WG0、WG1 是计算消费者，WG2 是加载生产者。
- `D_QK/64`：K 沿 head 维的 tile 数（512→8、576→9），决定 QK^T 的 GEMM 次数。

smem 关键复用：`sS[0]`（存 remote P）在 `D_QK==576` 时与 K 的 RoPE 段重叠以省 smem，512 时才单独开两块（见下文源码）。这呼应 u5-l1「RoPE 段特殊处理」的设计。

三个 `TiledMMA` 与 seesaw 的对应（同 u3-l2）：

- `TiledMMA_QK`：`SS`，Q 与 K 都在 smem，产出 attention score `rP`。
- `TiledMMA_PV_LocalP`：`RS`，用自己的 P（在寄存器 `rS` 里）× smem 里的 V。
- `TiledMMA_PV_RemoteP`：`SS`，用对方 warpgroup 经 `stmatrix` 写进 smem 的 P（`sS0`/`sS1`）× smem 里的 V。

#### 4.2.3 源码精读

[config.h:L18-L29](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/config.h#L18-L29) 定义 `KernelTemplate` 与核心常量。注意 `D_Q=D_K=D_QK`、`D_V=512` 固定，`B_H=B_TOPK=64`，`NUM_THREADS=384`，初始 `mi` 用 `-1e30`（不是 `-inf`，避免后续 `exp2f` 产生 NaN 的边界处理）。

[config.h:L84-L97](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/config.h#L84-L97) 定义三个 `TiledMMA`：QK 用 `MMA_64x64x16_*_SS`，PV 的 LocalP 用 `MMA_64x256x16_*_RS`、RemoteP 用 `MMA_64x256x16_*_SS`。注意 PV 的 MMA 是 `64x256`——一次算 256 维 V，恰好是 `D_V/2`，即每个 warpgroup 负责的 V 半边。

[config.h:L69-L82](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/config.h#L69-L82) 是 `SharedMemoryPlan`：`q_o` 是 Q/O 的 union（两者生命周期不重叠，省 smem）；`k[2]` 是双缓冲的 K 块；`s[D_QK==576 ? 1 : 2]` 是 remote P 的 smem 缓冲——注释明确说 V3.2（576）让 `sS[0]` 与 K 的 RoPE 段重叠，MODEL1（512）才开两块；`is_kv_valid[2][B_TOPK]` 存每个 token 的有效性掩码；`sM`/`sL` 是 warpgroup 间交换 max/sum 的标量缓冲；最后是一组 transaction barrier（`bar_k*_ready/free`）驱动 K 的生产-消费流水。

[config.h:L107-L116](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/config.h#L107-L116) 定义 8 个 `NamedBarriers`，名字直接揭示了 seesaw 的握手语义：`wg0_bunch_0_ready`/`wg1_bunch_0_ready`（交换 batch 的 max）、`wg0_s0_ready`/`wg1_s1_ready`（交换 P 到 smem）、`sL_ready`（epilogue 的 sum 归约）、`warpgroup{0,1}_sync`/`epilogue_sync`。

[config.h:L118-L136](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/config.h#L118-L136) 的 `save_rS_to_sS` 用 `SM90_U32x4_STSM_N`（即 `stmatrix`）把寄存器里的 P 写到 smem，供对方 warpgroup 用 `gemm_ss` 读取——这正是 seesaw「交换 P」的落点。

#### 4.2.4 代码实践

**实践目标**：把 `config.h` 的静态常量与主循环行为对上号。

**操作步骤**：

1. 在 [config.h:L22-L29](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/config.h#L22-L29) 找到 `D_V`、`B_H`、`B_TOPK`、`NUM_THREADS`。
2. 回答：一个 CTA 一次 QK^T GEMM 产出的 `rP` 形状是多少？（提示：`B_H × B_TOPK`）
3. 回答：为什么 PV 的 MMA 选 `64x256` 而不是 `64x512`？（提示：`D_V/2 = 256`，每个 warpgroup 只算 V 的半边）
4. 在 [config.h:L75](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/config.h#L75) 找到 `s` 数组，解释为何 V3.2(576) 只开 1 块、MODEL1(512) 开 2 块。

**预期结果**：

- `rP` 形状为 `64×64`（`B_H` 个 head × `B_TOPK` 个 token）。
- PV 选 `64x256` 是因为 O 被竖切成左右两半（各 256 维），分给 WG0/WG1。
- V3.2 时 K 的 RoPE 段（第 9 个 tile， dims 512–575）在 QK^T 用完后不再被 PV 使用，可被 `sS[0]` 复用，故只需 1 块；MODEL1 没有 RoPE 段可复用，需独立开 2 块做双缓冲。

#### 4.2.5 小练习与答案

**练习 1**：`MAX_INIT_VAL` 为何用 `-1e30` 而非 `-INFINITY`？

**答案**：`mi`（running max）初值若为 `-INFINITY`，首轮 `exp2f(score - mi)` 在 `score` 也极小时可能产生 `0×inf = NaN` 的边界问题；用有限的大负数 `-1e30` 既保证被任何真实 score 覆盖，又避免 `inf` 参与运算。真正的「无有效 token」由 `rL==0` 在 epilogue 单独判定（见 4.3）。

**练习 2**：`NamedBarriers` 里的 `wg0_s0_ready` 和 `wg1_s1_ready` 分别在交换什么？

**答案**：`wg0_s0_ready` 表示 WG0 已把本地 P 经 `stmatrix` 写入 `sS0`，可供 WG1 的 `gemm_ss`（remote P）读取；`wg1_s1_ready` 则是 WG1 写好 `sS1` 通知 WG0。两者实现了 P 在两个 warpgroup 间的双向交换。

### 4.3 online softmax 主循环与双 warpgroup seesaw

#### 4.3.1 概念说明

这是 phase1 的心脏。要解决的核心问题：给定一行 query（64 个 head），对它在 `indices` 里指定的 topk 个 K token 做 sparse attention，输出 `out`、`max_logits`、`lse`。

这里有两层设计叠加：

1. **online softmax（FlashAttention 核心）**：不在显存里物化完整的 `N×N` attention 矩阵，而是维护三个 running 状态——行最大值 `rM`、归一化指数和 `rL`、加权输出累加 `rO`——逐块（每块 `B_TOPK=64` 个 token）更新。数学上与一次性 softmax 完全等价，但显存占用仅 `O(1)`。

2. **双 warpgroup 的二维 seesaw**：一个 CTA 里有 WG0、WG1 两个计算 warpgroup。它们沿**两个独立轴**切分工作量：
   - **token 块轴**：WG0 处理偶数块 `block_idx`，WG1 处理奇数块 `block_idx+1`（主循环 `block_idx += 2`）。每块 64 个 token。
   - **V 维轴**：WG0 负责输出 O 的左半（V 的 0–255 维，即 NoPE 段前半），WG1 负责右半（256–511 维）。

   于是形成一个 2×2 的小矩阵：每个 warpgroup 都要做 2 次 PV GEMM——一次用自己的 P（local，`gemm_rs`）算自己 token 块对自己 V 半的贡献，一次用对方的 P（remote，`gemm_ss`）算对方 token 块对自己 V 半的贡献。两个 warpgroup 全程满载，互不闲置。

这与 u3-l3 的 dense decode seesaw 同源（都是 `RS` local + `SS` remote + `stmatrix` 交换 P），但 dense decode 是沿输出 V 维一维切，这里升级为「token 块 × V 维」的二维切分——因为 prefill 的 `s_q>1` 且 topk 较大，token 维有足够的并行度可拆。

#### 4.3.2 核心流程

先给出单 warpgroup、单 token 块的 online softmax 数学（base-2 内部表示，`scale = sm_scale_div_log2`）：

设当前块算出的原始 attention score 为 \(s_{ij}\)（已乘 `scale`）。记 \(m\) 为到目前所有块的行最大值，\(\ell\) 为归一化和，\(O\) 为加权输出。处理一个新块时：

\[
m_{\text{new}} = \max\bigl(m,\ \max_{j\in\text{block}} s_{ij}\bigr)
\]

\[
O \leftarrow O\cdot 2^{m - m_{\text{new}}} + \sum_{j\in\text{block}} 2^{s_{ij}-m_{\text{new}}}\, V_j
\]

\[
\ell \leftarrow \ell\cdot 2^{m - m_{\text{new}}} + \sum_{j\in\text{block}} 2^{s_{ij}-m_{\text{new}}}
\]

更新后 \(m \leftarrow m_{\text{new}}\)。最终输出 \(O/\ell\)，且 \(\text{lse}=\log_2 \ell + m\)（再 `×ln2` 转 base-e）。

双 warpgroup 的合并，就是把上面公式里「一个块」拆成 WG0 的块 A 与 WG1 的块 B：

\[
m = \max(m_A, m_B)
\]

\[
O = 2^{m_A - m}O_A + 2^{m_B - m}O_B,\qquad \ell = 2^{m_A - m}\ell_A + 2^{m_B - m}\ell_B
\]

kernel 里这个合并靠两类共享实现：

- **`sM`（max 交换）**：WG0 先算出自己的 `new_maxs` 写进 `sM`，发 `wg0_bunch_0_ready`；WG1 读 `sM`、与自己的 max 取 `max` 得到全局 `m`，再据此 rescale。
- **`sS0/sS1`（P 交换）**：各自把 rescale 后的 P 用 `stmatrix` 写进 smem，供对方的 `gemm_ss` 算「对方 token 块对我 V 半」的贡献。
- **`reduce_L`**：循环结束后，两个 warpgroup 的 \(\ell\) 还要跨 WG 求和（因为它们各持一半 token 块的贡献），用 `__shfl_xor_sync` + `sL` 缓冲完成。

整条流程（以一轮 `block_idx/block_idx+1` 为例，CTA 内三组 warpgroup 协同）：

```text
WG2(producer): 用 cp.async 把 block_idx 的 K 装进 k[0]、block_idx+1 的 K 装进 k[1]，
               并把每个 token 的有效性写进 is_kv_valid[]，发 ready barrier
WG0(consumer): 1) 等 k[0] ready，做 QK^T(block_idx) → rP0
               2) mask_rP：把无效 token 的 score 置 -inf
               3) online_softmax(WG0)：更新 rM/rL，rescale rO，算出 P0，写 sM → 通知 WG1
               4) gemm_rs(P0,  V[block_idx]  左半)   → 累加 rO 左半（自己的块）
               5) 等 WG1 写好 sS1 → gemm_ss(sS1, V[block_idx+1] 左半) → 累加 rO 左半（对方的块）
WG1(consumer): 1) 等 k[1] ready，做 QK^T(block_idx+1) → rP1
               2) mask_rP
               3) 等 WG0 的 sM → online_softmax(WG1)：用全局 max rescale，算 P1，写 sS1
               4) gemm_rs(P1, V[block_idx+1] 右半) → 累加 rO 右半
               5) 等 WG0 写好 sS0 → gemm_ss(sS0, V[block_idx]   右半) → 累加 rO 右半
循环结束: reduce_L 跨 WG 合并 ℓ → store_O 写回 out/max_logits/lse
```

`HAVE_TOPK_LENGTH` 在主循环里的作用是计算 `num_topk_blocks`（要处理几个 64-token 块），并在线程级加载 token 时做 `offs < topk_length` 的逐行掩码——这是 u6-l1「最左若干个有效」语义的落点。

#### 4.3.3 源码精读

先看 CTA 的索引分解与 `topk_length`/`num_topk_blocks` 的计算：

[phase1.cuh:L46-L50](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/phase1.cuh#L46-L50) 把 `blockIdx.x` 拆成 `q_h_idx`（head 组号，低位）与 `s_q_idx`（query 行号，高位），并取 warpgroup/warp/线程号。

[phase1.cuh:L80-L81](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/phase1.cuh#L80-L81) 是 `HAVE_TOPK_LENGTH` 的核心分支：`true` 时逐行 `__ldg` 读 `topk_length[s_q_idx]` 并 `ceil_div` 得块数；`false` 时直接用整批统一的 `params.topk / B_TOPK`。编译期化后每个版本只含一路代码。

online softmax 的 running 状态初始化：

[phase1.cuh:L96-L101](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/phase1.cuh#L96-L101) 定义 `rM[2]`（两行的 running max，初值 `MAX_INIT_VAL`）、`rL[2]`（归一化和，初值 0）、`rO`（PV 的累加器）、`rP`/`rS`（QK^T 的结果与转 bf16 的拷贝）。每个线程持 2 行是因为 `B_H=64` 被 128 线程按「每线程 2 行」覆盖。

online softmax 主体（核心中的核心）：

[phase1.cuh:L133-L184](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/phase1.cuh#L133-L184) 是 `online_softmax_and_rescale_o` lambda，逐行做：用 `__shfl_xor_sync(_,1)` 与 `__shfl_xor_sync(_,2)` 在 warp 内跨 4 线程归约出行 max（[L150-L151](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/phase1.cuh#L150-L151)），算 `new_max = max(old_max, cur_max)`，用 `exp2f(rM - new_max)` rescale `rO`（[L159-L164](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/phase1.cuh#L159-L164)），把 `rP` 转 `exp2f` 形式同时累加 `cur_sum` 进 `rL`（[L169-L176](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/phase1.cuh#L169-L176)）。注意 [L155-L156](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/phase1.cuh#L155-L156)：WG1 的 `old_max` 来自 `sM`（WG0 写入），WG0 的 `old_max` 来自自己的 `rM`——这就是双 WG 合并 max 的接续点。

WG0 主循环里 seesaw 的两次 PV GEMM：

[phase1.cuh:L319-L326](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/phase1.cuh#L319-L326) WG0 先做 `mask_rP` + `online_softmax_and_rescale_o`，再 `gemm_rs(rS, sV0l, rO)`——用自己的 local P 算「block_idx 对 O 左半」的贡献。

[phase1.cuh:L348-L353](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/phase1.cuh#L348-L353) WG0 等 WG1 写好 `sS1` 后，`rescale_rO` + `gemm_ss(sS1, sV1l, rO)`——用 remote P 算「block_idx+1 对 O 左半」的贡献。两段合起来覆盖了 O 左半的全部 token。

WG1 的对应两段在 [phase1.cuh:L416-L423](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/phase1.cuh#L416-L423)：`gemm_rs(rS, sV1r, rO)`（local，block_idx+1 对 O 右半）+ `gemm_ss(sS0, sV0r, rO)`（remote，block_idx 对 O 右半）。

跨 WG 的 ℓ 归约与 epilogue：

[phase1.cuh:L186-L199](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/phase1.cuh#L186-L199) 的 `reduce_L`：先用 `__shfl_xor_sync` 在 warp 内归约 `rL`，写进 `sL`，再经 `NamedBarrier::arrive_and_wait(sL_ready)` 读对端 `sL[(tid/4)^32]` 求和——把两个 warpgroup 各自的 ℓ 合并成全局 ℓ。

[phase1.cuh:L201-L209](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/phase1.cuh#L201-L209) 是 `store_O` 里的 attn_sink 与 lonely query 处理：归一化因子取 `1.0f / (rL + exp2f(attn_sink - rM))`（分母多出 sink 项，等价于 u6-l1 说的「`out *= e^L/(e^L+e^sink)`」），且 `rL==0` 时强制 `scale_factors=0`（无有效 token → 输出 0）。

[phase1.cuh:L444-L460](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/phase1.cuh#L444-L460) 由 WG1 写出 `max_logits` 与 `lse`：`rL==0`（lonely query）时分别置 `-INFINITY`/`+INFINITY`；否则 `max_logits = rM*ln2`、`lse = logf(rL) + rM*ln2`——注意这里的 `×CUDART_LN2_F` 正是「内部 base-2 → 对外 base-e」的转换（呼应 u4-l2、u6-l1）。

最后是 producer warpgroup（WG2）：

[phase1.cuh:L462-L557](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/phase1.cuh#L462-L557) 是加载生产者：先用 `cutlass::arch::warpgroup_reg_dealloc<72>` 退还寄存器给两个计算 warpgroup（它们 `reg_alloc<216>`，见 [L84](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/phase1.cuh#L84)），再用 `cp.async` 按 tile 把 K 从 global 搬进 `k[0]`/`k[1]`，并用 [L485-L489](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/phase1.cuh#L485-L489) 的 `t >= 0 && t < s_kv`（以及 `HAVE_TOPK_LENGTH` 时额外 `offs < topk_length`）判定每个 token 有效性，写入 `is_kv_valid[]`。这就是 u6-l1「无效索引 `-1`/`>=s_kv` 与 topk_length 截断」的物理实现。

#### 4.3.4 代码实践

**实践目标**：用一个简化的 PyTorch 参照，验证「online softmax 逐块更新」与「一次性 softmax」数值等价，从而建立对 kernel 主循环正确性的信心。

**操作步骤**（源码阅读 + 小实验，无需 GPU 也可在 CPU 跑）：

1. 打开 `tests/ref.py`，找到 `ref_sparse_attn_fwd`（u6-l1 已介绍）。确认它用的是 gather 出全部 topk token 后一次性 softmax。
2. 写一个最小脚本，把同一组 `q/kv/indices` 同时喂给「一次性 softmax」和「分两块的 online softmax合并」，对比二者 `out`。

示例代码（**示例代码**，非项目原有文件）：

```python
# 示例代码：验证 online softmax 两块合并 == 一次性 softmax
import torch, math
torch.manual_seed(0)
d_qk, dv, topk = 576, 512, 128
q  = torch.randn(1, 64, d_qk, dtype=torch.float64)          # [s_q, h_q, d_qk]
kv = torch.randn(topk, 1, d_qk, dtype=torch.float64)        # [topk, h_kv=1, d_qk]
scale = 1.0 / math.sqrt(d_qk)

# 1) 一次性 softmax（参照）
scores = (q[0] @ kv[:, 0].transpose(-2, -1)) * scale        # [64, topk]
p = torch.softmax(scores, dim=-1)
out_ref = p @ kv[:, 0, :dv]                                  # [64, dv]
lse_ref = torch.logsumexp(scores, dim=-1)                    # base-e

# 2) 分两块 online softmax 合并（模仿 kernel 的 block_idx / block_idx+1）
half = topk // 2
def block_contrib(k_block):
    s = (q[0] @ k_block.transpose(-2, -1)) * scale           # [64, half]
    m = s.max(dim=-1).values
    p = torch.exp2((s - m[:, None]) / math.log(2))           # 内部用 base-2
    o = p @ k_block[:, :dv]
    return m, p.sum(dim=-1) * 1.0, o, p                      # m, l(base2), o, p

m0, l0, o0, _ = block_contrib(kv[:half, 0])
m1, l1, o1, _ = block_contrib(kv[half:, 0])
m = torch.maximum(m0, m1)
o = o0 * torch.exp2((m0 - m) / math.log(2)) + o1 * torch.exp2((m1 - m) / math.log(2))
l = l0 * torch.exp2((m0 - m) / math.log(2)) + l1 * torch.exp2((m1 - m) / math.log(2))
out_kernel = o / l                                           # 归一化
lse_kernel = (torch.log2(l) + m) * math.log(2)              # base-2 -> base-e

print("out max abs diff:", (out_kernel - out_ref).abs().max().item())
print("lse max abs diff:", (lse_kernel - lse_ref).abs().max().item())
```

**需要观察的现象 / 预期结果**：`out` 与 `lse` 的最大绝对误差应在 `1e-9` 量级（float64 下），从而验证「分块 online softmax + 跨块合并」与一次性 softmax 等价——这正是 kernel 主循环的数学保证。若把 `half` 改成 `topk//4`、循环 4 次，结论同样成立。

> 说明：本实践用 float64 在 CPU 验证数学等价性；kernel 用 bf16 + base-2 `exp2f`，数值会有容差级差异，正确性由 `tests/test_flash_mla_sparse_prefill.py` 的三重容差（abs/rel/cos_diff）判定（见 u8-l2）。

#### 4.3.5 小练习与答案

**练习 1**：WG0 的 `gemm_ss(sS1, sV1l, rO)` 里，`sS1` 是谁的 P、`sV1l` 是哪个 token 块的 V？

**答案**：`sS1` 是 **WG1** 经 `stmatrix` 写入 smem 的 P（remote P），对应 token 块 `block_idx+1`；`sV1l` 是 `k[1]`（即 `block_idx+1`）里 V 的左半（dims 0–255）。这次 GEMM 算的是「对方 token 块对 O 左半的贡献」，正好补上 WG0 自己只覆盖 `block_idx` 的缺口。

**练习 2**：为什么 `reduce_L` 必须在 `store_O` 之前、且必须跨 WG0/WG1 求和？

**答案**：两个 warpgroup 各自只处理一半 token 块（WG0 偶数块、WG1 奇数块），它们的 `rL` 只是「自己那半」的归一化和。而 `store_O` 里归一化用的是全局 \(\ell\)（`1/(rL + sink)`），必须先把两半的 \(\ell\) 合并（`reduce_L` 的 `__shfl_xor_sync` + `sL` 蝶形归约正是为此），否则分母偏小、输出偏大。

**练习 3**：`store_O` 里 `rL[i] == 0.0f` 的分支对应 u6-l1 描述的哪种边界情况？

**答案**：对应 **lonely query**——一行 query 没有任何有效 token（所有索引都无效或被 topk_length 截断）。此时 `rL==0`，kernel 强制 `scale_factors=0` 使 `out=0`，并在 [L447-L449](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/phase1.cuh#L447-L449) 把 `max_logits`/`lse` 分别置为 `-INFINITY`/`+INFINITY`，与 u6-l1 的契约一致。

### 4.4 模板特化与 instantiation 文件组织

#### 4.4.1 概念说明

`KernelTemplate<D_QK, HAVE_TOPK_LENGTH>` 与 `run_fwd_phase1_kernel<D_QK, HAVE_TOPK_LENGTH>` 都是模板，模板本身不产生代码——必须有**显式实例化**（explicit instantiation）才会被编译成机器码。FlashMLA 把这 4 个实例化拆到 `instantiations/` 下 4 个独立 `.cu` 文件，每个文件只实例化一个 `(D_QK, HAVE_TOPK_LENGTH)` 组合。

这套做法在 u1-l3 已提过（`instantiations/` 子目录的通用职责），本讲具体落到 sparse prefill。

#### 4.4.2 核心流程

4 个组合与文件的对应关系：

| 文件 | 模板实参 | 含义 |
| --- | --- | --- |
| [phase1_k512.cu](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/instantiations/phase1_k512.cu#L1-L11) | `<512, false>` | MODEL1、整批统一 topk |
| [phase1_k512_topklen.cu](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/instantiations/phase1_k512_topklen.cu#L1-L11) | `<512, true>` | MODEL1、逐行 topk_length |
| [phase1_k576.cu](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/instantiations/phase1_k576.cu#L1-L9) | `<576, false>` | V3.2、整批统一 topk |
| [phase1_k576_topklen.cu](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/instantiations/phase1_k576_topklen.cu#L1-L9) | `<576, true>` | V3.2、逐行 topk_length |

为什么按组合拆文件？三个理由：

1. **并行编译**：文件顶部的 NOTE 写得直白——拆开是为了「compile them in parallel」。NVCC 单个翻译单元实例化多个超长模板 kernel 会非常慢，拆成 4 个独立 TU 后，`setup.py`（[`L85-L88`](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/setup.py#L85-L88)）可让 NVCC 多线程并行编译，构建时间显著下降。
2. **降低单 TU 的编译压力**：每个特化都是「一份完整 kernel」，寄存器/smem 规模大，单 TU 放多个会让 NVCC/ptxas 的内存与时间开销爆炸。拆开后每个 TU 只扛一个特化。
3. **按需注册**：`setup.py` 的 `sources` 列表显式列出这 4 个文件（连同 `fwd.cu`），增删一个特化只动一个文件 + 一行 sources，干净利落。

#### 4.4.3 源码精读

每个实例化文件只有两行有效代码——`#include` 头文件 + 一条显式实例化语句：

[phase1_k512.cu:L1-L10](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/instantiations/phase1_k512.cu#L1-L10) 同时 `#include "../phase1.h"`（拿声明）与 `#include "../phase1.cuh"`（拿定义），然后用 `template void run_fwd_phase1_kernel<512, false>(const SparseAttnFwdParams&);` 触发实例化。文件顶部 NOTE 明确说明拆分的动机是并行编译。

其余三个文件结构完全一致，只是模板实参换成 `<512, true>`、`<576, false>`、`<576, true>`。

`setup.py` 把这 4 个文件连同 `fwd.cu` 一起注册进编译列表：

setup.py:[L84-L88](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/setup.py#L84-L88) 列出 `fwd.cu` 与 4 个 `instantiations/*.cu`——只有 `.cu` 进编译，`.h`/`.cuh`/`config.h` 通过 `#include` 被动卷入。

#### 4.4.4 代码实践

**实践目标**：亲手核对 4 个实例化文件的 `(HEAD_DIM, HAVE_TOPK_LENGTH)` 映射，并理解拆分动机。

**操作步骤**：

1. 依次打开 `csrc/sm90/prefill/sparse/instantiations/` 下 4 个 `.cu`：
   - `phase1_k512.cu`
   - `phase1_k512_topklen.cu`
   - `phase1_k576.cu`
   - `phase1_k576_topklen.cu`
2. 读每个文件最后一行的 `template void run_fwd_phase1_kernel<...>(...)`，填表：

| 文件 | `D_QK`(HEAD_DIM) | `HAVE_TOPK_LENGTH` |
| --- | --- | --- |
| `phase1_k512.cu` | 512 | false |
| `phase1_k512_topklen.cu` | 512 | true |
| `phase1_k576.cu` | 576 | false |
| `phase1_k576_topklen.cu` | 576 | true |

3. 阅读文件顶部 NOTE（`phase1_k512.cu` 与 `phase1_k512_topklen.cu` 有），原文说明拆分是为了「compile them in parallel」。
4. 在 [setup.py:L85-L88](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/setup.py#L85-L88) 确认这 4 个 `.cu` 都在 `sources` 列表里。

**需要观察的现象 / 预期结果**：

- 4 个文件分别钉死 4 个互不重叠的 `(D_QK, HAVE_TOPK_LENGTH)` 组合，正好覆盖 `fwd.cu` 派发的全部出口。
- 文件名规律：`phase1_k{HEAD_DIM}{_topklen}`，后缀 `_topklen` 表示 `HAVE_TOPK_LENGTH=true`。
- 命名约定可推广：若将来新增 `D_QK=448` 支持，只需新增 `phase1_k448.cu` 与 `phase1_k448_topklen.cu` 两个文件、在 `fwd.cu` 加一个 `else if` 分支、在 `setup.py` 加两行 sources。

**预期结果（拆分目的总结）**：按组合拆实例化文件 = 并行编译提速 + 降低单 TU 编译压力 + 增删特化只动局部文件。这是 CUTLASS 系大型模板项目的通行做法。

#### 4.4.5 小练习与答案

**练习 1**：如果把这 4 条显式实例化合并进 `fwd.cu` 一个文件，会发生什么？

**答案**：功能上等价（链接器照样能找到符号），但编译会显著变慢——`fwd.cu` 会变成一个含 4 份完整 kernel 实例化的巨型翻译单元，NVCC/ptxas 需要串行处理，内存峰值高、耗时线性叠加。拆成 4 个 TU 后可被 `setup.py` 的 `NVCC_THREADS` 并行编译，构建时间大幅缩短。

**练习 2**：为什么实例化文件要同时 `#include "../phase1.h"` 和 `#include "../phase1.cuh"`？只 include `.cuh` 不行吗？

**答案**：`.h` 是对外声明（被 `fwd.cu` 调用，也是实例化符号的「契约」），`.cuh` 是实现（含模板定义）。显式实例化 `template void run_fwd_phase1_kernel<...>` 必须能看到模板的**定义**才能生成代码，所以必须有 `.cuh`；同时 include `.h` 保证声明与实例化的签名严格一致、避免 ODR（单一定义规则）相关的坑。只 include `.cuh` 在本例也能编过，但同时 include `.h` 是更稳妥、更自文档化的写法。

## 5. 综合实践

把本讲四个模块串起来，做一次「**从接口到主循环的纵向追踪**」。

**任务**：选定一个具体配置——`d_qk=576`、`h_q=128`、`s_q=4`、`topk=256`、**带** `topk_length`——完成下列追踪并产出一张说明图（文字版即可）。

1. **派发层（4.1）**：在 [fwd.cu:L9-L28](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/fwd.cu#L9-L28) 确定本配置走 `run_fwd_phase1_kernel<576, true>`，对应实例化文件 `phase1_k576_topklen.cu`。
2. **配置层（4.2）**：在 [config.h:L22-L29](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/config.h#L22-L29) 确认 `B_H=64`、`B_TOPK=64`、`NUM_THREADS=384`、`D_V=512`；在 [config.h:L75](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/config.h#L75) 确认 V3.2(576) 的 `s` 数组只开 1 块（与 K 的 RoPE 段重叠）。
3. **启动层（4.1）**：在 [phase1.cuh:L629](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/phase1.cuh#L629) 算出 grid = `(h_q/B_H)*s_q = 2*4 = 8` 个 CTA；在 [phase1.cuh:L46-L47](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/phase1.cuh#L46-L47) 解释 `blockIdx.x` 如何拆成 `(q_h_idx, s_q_idx)`。
4. **主循环层（4.3）**：在 [phase1.cuh:L80-L81](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/phase1.cuh#L80-L81) 算出 `num_topk_blocks = ceil_div(topk_length, 64)`（逐行）；在 [phase1.cuh:L306-L307](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/prefill/sparse/phase1.cuh#L306-L307) 确认主循环 `block_idx += 2`，故 `topk=256` → `num_topk_blocks=4` → 循环 2 轮，每轮 WG0/WG1 各吃一个 64-token 块。
5. **合并层（4.3）**：用 4.3.4 的示例脚本，把 `topk=256`、`half=128` 跑一遍，确认分块 online softmax 与一次性 softmax 数值等价。

**预期产出**：一段说明文字 + 一张表格，把「配置值 → 派发实参 → grid → 循环轮数 → 数值等价验证」逐行对上。如果你能独立完成这张表，说明你已经把 phase1 从接口到主循环彻底打通。

> 若有 SM90 GPU 且已按 u1-l2 编译安装，可进一步用 `tests/test_flash_mla_sparse_prefill.py` 跑真实 kernel，对照你手算的 grid/轮数。无 GPU 时本实践为纯源码阅读 + CPU 数值验证。

## 6. 本讲小结

- `phase1` 的对外接口是模板函数 `run_fwd_phase1_kernel<D_QK, HAVE_TOPK_LENGTH>`；`fwd.cu` 用显式 `if/else`（而非 DISPATCH 宏）把运行时的 `d_qk` 与 `topk_length!=nullptr` 派发到 4 个特化，动机同 u2-l3 的「运行时值编译期化」。
- `config.h` 集中了 kernel 的静态蓝图：`D_V=512`、`B_H=B_TOPK=64`、`NUM_THREADS=384`，三个 GMMA `TiledMMA`（QK-SS / PV-LocalP-RS / PV-RemoteP-SS），以及与 dense decode 同源的 `stmatrix` 交换 P 机制。
- 主循环是 online softmax + **二维 seesaw**：WG0/WG1 沿「token 块（偶/奇）× V 半边（左/右）」切分，每个 warpgroup 各做一次 `gemm_rs`(local P) 与一次 `gemm_ss`(remote P)，靠 `sM`(max) 与 `sS0/sS1`(P) 双向交换合并状态，数学上与单流处理等价。
- 无效索引与 `topk_length` 截断在 producer warpgroup 用 `t>=0 && t<s_kv` 与 `offs<topk_length` 物化为 `is_kv_valid[]`，再由 `mask_rP` 把无效 score 置 `-inf`；attn_sink 缩放 `out`、lonely query(`rL==0`) 置 `out=0`/`lse=+inf`，全部承接 u6-l1 的契约。
- 内部统一 base-2（`exp2f`、`sm_scale_div_log2`），写出时 `×ln2` 转 base-e；`reduce_L` 用 `__shfl_xor_sync`+`sL` 蝶形归约跨 WG 合并 ℓ。
- 4 个实例化拆到 `instantiations/` 下独立 `.cu`，每个钉死一个 `(D_QK, HAVE_TOPK_LENGTH)` 组合，目的是并行编译提速、降低单 TU 压力、按需注册。

## 7. 下一步学习建议

- **横向对比 SM100 sparse prefill（u6-l3）**：Blackwell 上 head64/head128 phase1 与 SM100 的 `fwd_for_small_topk` 变体如何复用/改写这套 seesaw 思路，重点看 tile 配置差异与小 topk 专用路径。
- **纵向回到 dense seesaw（u3-l3）**：把本讲的二维 seesaw 与 dense decode 的一维 `O_L/O_R` 切分对照，理解「同一套 RS/SS + stmatrix 手法如何适配 prefill(s_q>1) vs decode(s_q=1)」。
- **接口与实现选择（u6-l4）**：继续看 `csrc/api/sparse_fwd.h` 如何在 SM100 head128 上对 small_topk 与普通实现做「优先/兜底」选择，把 u6-l2/u6-l3 的 kernel 串进上层派发框架。
- **数值正确性体系（u8-l2）**：若你想把本讲 4.3.4 的等价性验证推广到 bf16 + 三重容差，阅读 `tests/ref.py` 与 `tests/test_flash_mla_sparse_prefill.py` 的用例生成与判定逻辑。
