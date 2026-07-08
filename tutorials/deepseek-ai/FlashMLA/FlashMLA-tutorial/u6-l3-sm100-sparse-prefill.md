# SM100 sparse prefill 与 small_topk 变体

## 1. 本讲目标

上一讲（u6-l2）我们读完了 **SM90** 上的 sparse prefill 主力 kernel `phase1`，它用「online softmax + 双 warpgroup 二维 seesaw + `cp.async` 装载」实现了 token-level 稀疏注意力。本讲把目光移到 **SM100（Blackwell，sm_100f）**，回答三个问题：

1. 在 Blackwell 这一代新硬件上，sparse prefill 的 `phase1` kernel 长什么样？为什么分成 `head64` 和 `head128` 两条实现？
2. 当 `topk`（每个 query 实际参与计算的 KV token 数）很小时，为什么还需要一个专门的 `fwd_for_small_topk` 变体？
3. 这一个 `small_topk` kernel 为什么能**同时服务 prefill 和 decode 两种阶段**？模板开关是怎么做到的？

学完后你应当能够：

- 说清 SM100 sparse prefill 三条实现路径（`head64` / 普通 `head128` / `small_topk head128`）的职责分工与选择条件；
- 看懂 Blackwell 上 TMEM（Tensor Memory）、UMMA、UTCCP、2-CTA cluster、CLC 这些新硬件概念在 kernel 里的落地方式；
- 把 `KernelTemplate<SparseAttnFwdMode FWD_MODE, int D_QK>` 这种「用一个模板参数同时编译出 prefill 和 decode 两份 kernel」的设计复述出来；
- 读懂 `common_subroutine.h` 里被多个 kernel 复用的 device 子例程。

## 2. 前置知识

本讲默认你已经掌握 u6-l1（sparse attention 的语义、`indices` 编码、`out/max_logits/lse` 输出、`attn_sink`、lonely query）和 u6-l2（online softmax 主循环、`P=QK^T`、`O+=SV`、base-2 内部 / base-e 输出的约定）。下面补充几个 **Blackwell（SM100）专属**的概念，它们是本讲理解 kernel 的钥匙。

> 说明：SM90（Hopper）的核心算力单元是 WGMMA（Warpgroup MMA），数据放在 shared memory（smem）与寄存器里；SM100（Blackwell）在 smem 和寄存器之间多加了一层 **Tensor Memory（TMEM）**，并引入了一整套围绕 TMEM 的新指令。

| 术语 | 含义 | 在本讲中的作用 |
|---|---|---|
| **TMEM（Tensor Memory）** | Blackwell 每个 SM 内部一块专用的片上存储（类似一块更大的“寄存器堆”），归 `UMMA` 使用 | 累加器 `O`、部分 `Q`、`P` 都放在 TMEM，不再挤寄存器 |
| **UMMA** | Unified MMA，Blackwell 的 tensor-core 矩阵乘指令，操作数可来自 smem 或 TMEM | `P = QK^T`、`O += SV` 的核心指令 |
| **UTCCP** | Unified Tensor Copy，把数据从 smem 拷进 TMEM（S→T） | 把 `Q` 从 smem 搬进 TMEM 喂给 UMMA |
| **TMEM Allocator** | 运行时在 TMEM 里划出一块（`Allocator1Sm` / `Allocator2Sm`） | kernel 开头申请 512 列 TMEM，结尾释放 |
| **2-CTA cluster / `2x1SM` MMA** | 把一个逻辑 MMA 拆给 cluster 内两个 CTA 协同完成 | `head128` 用 2 个 CTA 合作算一个更大的 tile |
| **2-SM multicast TMA** | 一次 TMA load 同时把数据分发给 cluster 里的 2 个 CTA | `SM100_TMA_2SM_LOAD_NOSPLIT` 装载 `Q` |
| **CLC（Cooperative Launch Control）** | Blackwell 的硬件任务分发机制：常驻 kernel 主动向硬件“领活”，而非 grid 预分配 | `small_topk` 的 prefill 模式用它分发 `s_q` |

另外，本讲的 `P=QK^T` 仍然沿用 u6-l2 提到的 **dual gemm（双矩阵乘）**技巧：一次 UMMA 同时算两块拼接在一起的小矩阵，从而把吞吐吃满。代码里常以 `N = 2 * B_TOPK` 或 `B_TOPK*2` 的形式体现。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `csrc/sm100/prefill/sparse/` 下，按用途分三类：

| 文件 | 作用 |
|---|---|
| [fwd/head64/config.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd/head64/config.h) | SM100 **head64** 普通 phase1 的静态蓝图（tile 尺寸、smem 布局、TiledMMA、barrier） |
| [fwd/head64/phase1.cuh](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd/head64/phase1.cuh) | head64 的 kernel 主体（device 函数）与 host 端 `run_fwd_phase1_kernel` 启动 |
| [fwd/head128/config.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd/head128/config.h) | SM100 **head128** 普通 phase1 的静态蓝图（2-CTA cluster 版） |
| [fwd/head128/phase1.cuh](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd/head128/phase1.cuh) | head128 的 kernel 主体，含 `tQ/sQ` 拆分与 cluster 启动 |
| [fwd_for_small_topk/head128/config.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd_for_small_topk/head128/config.h) | **small_topk** 变体的静态蓝图，带 `FWD_MODE` 模板参数 |
| [fwd_for_small_topk/head128/phase1.cuh](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd_for_small_topk/head128/phase1.cuh) | small_topk kernel 主体，一份代码同时编译出 prefill 与 decode |
| [common_subroutine.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/common_subroutine.h) | 多个 SM100 sparse kernel 共享的 device 子例程（索引装载、掩码、P 归约、O 缩放） |
| [csrc/api/sparse_fwd.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h) | prefill 接口：`FwdFeatures` 派发与 small_topk 选择阈值 |
| [csrc/api/sparse_decode.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h) | decode 接口：`Decode_Sm100_Head128_Impl` 复用 small_topk kernel |

> 目录命名规律（承接 u1-l3）：`fwd/` = 普通 prefill kernel，按 `head64/head128` 分头数；`fwd_for_small_topk/` = 小 topk 专用变体；`common_subroutine.h` 放在 `sparse/` 顶层，被同层多个 kernel 复用。

## 4. 核心概念与源码讲解

### 4.1 SM100 head64 与 head128 phase1 kernel

#### 4.1.1 概念说明

`phase1` 这个名字来自 FlashAttention 的分阶段命名：在 sparse 场景里，`phase1` 就是「给定 `q` 和一组 `indices`，算出 `out / max_logits / lse`」的正向 kernel（本库的 sparse prefill 没有 split-KV，所以 phase1 即全部正向）。

在 SM100 上，`phase1` 按 query 头数 `h_q` 分成两条普通实现：

- **`head64`**：`h_q = 64`。一个 CTA（线程块）独立处理一份 query，3 个 warpgroup（共 384 线程）分工。架构上是「单 CTA + TMEM」。
- **`head128`**：`h_q = 128`。两个 CTA 组成 **cluster**，各管 64 个头，合作算一个更大的 tile。架构上是「2-CTA cluster + 跨 SM 的 `2x1SM` MMA」。

为什么要分头数？因为 MLA 解码 / sparse prefill 的 `h_q` 只会是 64 或 128（对应不同的模型配置）。`h_q` 越大，单次 GEMM 的 M 维越大，需要的累加器（TMEM）和寄存器越多；用一个固定 tile 的 kernel 兼顾两种头数会浪费资源，所以干脆按 `h_q` 出两条特化实现。

这两条都属于「**普通**」路径——它们面向 `topk` 较大的场景。小 `topk` 的专用变体 `small_topk` 放到 4.2 讲。

#### 4.1.2 核心流程

无论 head64 还是 head128，算法骨架和 u6-l2 的 SM90 版本一致，都是 **online softmax 的三段流水**。kernel 顶部有一段非常清晰的流水注释（以 head64 为例）：

```
| Copy(KV) |    MMA(P=QK^T, O+=SV)    |   Scale & Exp   |
```

每个 KV 块 `k` 经历三步：

1. **Copy**：producer 把第 `k` 个 `B_TOPK` 大小的 KV 块（按 `indices` gather）搬进 smem；
2. **MMA**：`P_k = Q K_k^T`（累加进 TMEM 的 `P`），再 `O += S_{k-1} V_{k-1}`；
3. **Scale & Exp**：从 TMEM 取出 `P_k`，做掩码（无效 index 置 `-inf`）、求行最大值 `mi`、计算 `S_k = exp2(P_k * scale - mi)`，并把历史 `O` 按比例 `rescale`。

online softmax 的核心是 rescale 公式。设当前已聚合的最大值为 `mi`、加权和为 `O`、归一化常数 `li`；新块算出局部最大 `cur_max`，则：

\[
\text{scale\_for\_old} = 2^{\,mi - new\_max},\quad new\_max = \max(cur\_max, mi)
\]

\[
O \leftarrow O \cdot \text{scale\_for\_old} + S_k V_k,\qquad li \leftarrow li \cdot \text{scale\_for\_old} + \sum S_k
\]

最终输出归一化为 `O / (li + 2^{sink - mi})`（`attn_sink` 缩放，见 u6-l1），并把 `lse = mi*ln2 + log(li)`（base-2 内部转 base-e 输出）。

为了把 Copy / MMA / Scale&Exp 三段重叠起来，kernel 用 **多缓冲（`NUM_BUFS`）**：head64 用 3 个，head128 用 2 个，让第 `k` 块的拷贝与第 `k-1`/`k-2` 块的 MMA 和 softmax 并行推进。

#### 4.1.3 源码精读

**(a) head64 的静态配置**——单 CTA、3 缓冲、TS+SS 两种 MMA：

- [config.h:28-36](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd/head64/config.h#L28-L36)：钉死 `D_Q=D_K=576, D_V=512`（MLA 的 512 NoPE + 64 RoPE），`B_H=64, B_TOPK=64, NUM_BUFS=3, NUM_THREADS=384`。
- [config.h:40-48](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd/head64/config.h#L40-L48)：`tmem_cols` 规划 TMEM 列——`O` 占 0~256，`Q` 占 256~400，`P` 占 400~。注意累加器 `O` 直接常驻 TMEM，不再回寄存器。
- [config.h:141-147](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd/head64/config.h#L141-L147)：定义两个 TiledMMA。`TiledMMA_P` 是 **TS**（A=Q 在 TMEM，B=K 在 smem），`N=128=2*B_TOPK` 正是 dual gemm 的痕迹；`TiledMMA_O` 是 **SS**（S、V 都在 smem，O 累加进 TMEM）。

**(b) head64 的线程分工**——kernel 把 384 个线程切成 3 个 warpgroup（每个 128 线程）：

- [phase1.cuh:152](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd/head64/phase1.cuh#L152)：`warpgroup_idx == 0` —— **Scale & Exp warps**，负责取 P、掩码、`mi/li` 维护、`rescale_O`、写回 `S`。
- [phase1.cuh:358](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd/head64/phase1.cuh#L358)：`warpgroup_idx == 1` —— **Producer warp**，用 `tma_gather4` 按 `indices` 把 KV 块搬进 smem（含一种「整块 index 全无效就跳过 TMA」的优化）。
- [phase1.cuh:413](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd/head64/phase1.cuh#L413)：剩下的 `else` 分支 —— **MMA warp**，先 `UTCCP` 把 Q 从 smem 拷进 TMEM，再循环发起 `utcmma_ts`（算 P）与 `utcmma_ss`（算 O）。

head64 的启动是**最朴素的单 CTA** 形式，grid 就是 `s_q`：

- [phase1.cuh:669](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd/head64/phase1.cuh#L669)：`kernel<<<params.s_q, NUM_THREADS, smem_size, params.stream>>>(...)`；TMEM 分配用 [phase1.cuh:145](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd/head64/phase1.cuh#L145) 的 `Allocator1Sm().allocate(512, ...)`。

**(c) head128 的 2-CTA cluster**——两个 CTA 合作，每个管 64 个头：

- [config.h:33-39](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd/head128/config.h#L33-L39)：`B_H=128, B_TOPK=128`（注释明确写 `// For 2 CTAs`，即 cluster 级共 128，每个 CTA 一半），`NUM_BUFS=2, NUM_THREADS=512`。
- [config.h:42-55](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd/head128/config.h#L42-L55)：head128 最关键的设计——**把 Q 沿特征维拆成两半**：`D_tQ=384` 维进 TMEM（`tQ`），剩下 `D_sQ=D_QK-384` 维留在 smem（`sQ`）。这是因为 TMEM 容量有限，放不下整份 128 头 × 576 维的 Q。
- [config.h:120-132](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd/head128/config.h#L120-L132)：于是 `P=QK^T` 需要**两种 MMA**——`TiledMMA_P_tQ`（2x1SM **TS**，Q 的 tQ 部分在 TMEM）和 `TiledMMA_P_sQ`（2x1SM **SS**，Q 的 sQ 部分在 smem）。`TiledMMA_O` 用 `2x1SM SS`，并用一个 permutation 布局让 `CTA0` 取 `V[:,0:256]`、`CTA1` 取 `V[:,256:512]`。

在 kernel 主体里，MMA 分支按这个拆分顺序发起两次乘法并累加进同一个 `tP`：

- [phase1.cuh:539](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd/head128/phase1.cuh#L539)：`utcmma_ss(tiled_mma_P_sQ, sQl, sKl, tP, true)` —— 先用 sQ（smem）那半算，`true` 表示清空累加器。
- [phase1.cuh:547](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd/head128/phase1.cuh#L547)：`utcmma_ts(tiled_mma_P_tQ, tQr, sKr, tP, false)` —— 再用 tQ（TMEM）那半累加。

cluster 协同的硬件基础是 `cluster_sync` 与 2-SM TMEM 分配：

- [phase1.cuh:70-71](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd/head128/phase1.cuh#L70-L71)：`cta_idx = blockIdx.x % 2`，`s_q_idx = blockIdx.x / 2`——grid 维 `2*s_q`，每两个相邻 CTA 组成一个 cluster 处理一个 query。
- [phase1.cuh:130](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd/head128/phase1.cuh#L130)：`cluster_sync()` 必须在 barrier 初始化后调用，否则 CTA1 的 TMA 可能在 CTA0 完成 barrier 初始化前就发出。
- [phase1.cuh:694-700](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd/head128/phase1.cuh#L694-L700)：启动用 `cutlass::ClusterLaunchParams`，cluster 形状 `[2,1,1]`，TMEM 用 `Allocator2Sm`（跨 2 个 SM 分配）。

> 小结：head64 = 单 CTA + 全 Q 在 TMEM；head128 = 2-CTA cluster + Q 拆 tQ(TMEM)/sQ(smem) 两半用两种 MMA。两者都是为「大 topk」设计。

#### 4.1.4 代码实践

**实践目标**：用一个对比表把 head64 与 head128 两条普通实现的「架构差异」固化下来。

**操作步骤**：

1. 打开 [fwd/head64/config.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd/head64/config.h) 与 [fwd/head128/config.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd/head128/config.h)。
2. 在下表填空（答案见 4.1.5）：

| 配置项 | head64 | head128 |
|---|---|---|
| `B_H`（cluster 级） | 64 | _____ |
| `B_TOPK`（cluster 级） | 64 | _____ |
| `NUM_BUFS` | 3 | _____ |
| `NUM_THREADS` | 384 | _____ |
| CTA 数（每 query） | 1 | _____ |
| Q 的存放 | 全在 TMEM | _____ |
| `P=QK^T` 用到的 MMA | TS 一种 | _____ |
| TMEM 分配器 | `Allocator1Sm` | _____ |
| 支持的 `D_QK` | 512 / 576 | _____ |

3. 打开 [phase1.cuh:669](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd/head64/phase1.cuh#L669)（head64 启动）与 [phase1.cuh:694-700](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd/head128/phase1.cuh#L694-L700)（head128 启动），对比 grid 形状与是否用 `ClusterLaunchParams`。

**需要观察的现象**：head64 用裸 `<<<grid, block, smem, stream>>>`，grid=`s_q`；head128 用 `cutlass::launch_kernel_on_cluster`，grid=`2*s_q`、cluster=`[2,1,1]`。

**预期结果**：你会清楚地看到「头数翻倍 → 从单 CTA 升级到 2-CTA cluster → Q 放不下而拆 tQ/sQ → MMA 从一种变两种」这条因果链。本实践为纯源码阅读型，无需 GPU，结论可直接从源码得出。

#### 4.1.5 小练习与答案

**Q1**：head128 为什么不把整份 Q 都放进 TMEM（像 head64 那样），而要拆出 `sQ` 留在 smem？
**答**：head128 的 `B_H=128`（head64 的两倍），整份 `128×576` 的 Q 远超单 SM 可用的 TMEM 列数（`tmem_cols` 里 `O` 已占去 0~256）。因此把 Q 沿特征维拆成 `tQ=384`（进 TMEM）和 `sQ`（留 smem），分别用 `TS` 和 `SS` 两种 MMA 累加进同一个 `P`，见 [config.h:42-55](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd/head128/config.h#L42-L55)。

**Q2**：填表答案。
**答**：head128 各项依次为 `128 / 128 / 2 / 512 / 2 个 CTA / 拆 tQ(TMEM)+sQ(smem) / TS+SS 两种 / Allocator2Sm / 512 与 576`。

**Q3**：head64 的 `NUM_THREADS = 128 + 128 + 128 = 384`（[config.h:36](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd/head64/config.h#L36)），对应 kernel 里哪三组线程？
**答**：对应 [phase1.cuh](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd/head64/phase1.cuh) 里 `warpgroup_idx==0`（Scale & Exp，128 线程）、`warpgroup_idx==1`（KV Producer，128 线程）、`else`（MMA + KV 有效性装载 + K-RoPE 装载，128 线程）三个 warpgroup。

---

### 4.2 small_topk 变体：为小 topk 优化的专用 kernel

#### 4.2.1 概念说明

普通 `head128` kernel 是为「`topk` 较大」调优的：`B_TOPK=128`、`NUM_BUFS=2`，每个 query 要跑很多个 KV 块，于是「装 Q、初始化 barrier、cluster_sync、分配 TMEM」这些**固定开销**能被长长的主循环摊薄。

但当 `topk` 很小（例如 DSA 稀疏注意力里每个 query 只挑几十~一千来个 token）时，主循环的迭代数 `num_k_blocks = ceil_div(topk_length, B_TOPK)` 很小，会出现两个问题：

1. **固定开销占比飙升**：装 Q、cluster 同步、TMEM 分配/释放这些一次性的工作，摊到寥寥几轮 MMA 上，吞吐被严重稀释。
2. **粗 tile 造成浪费**：`B_TOPK=128` 的 tile 在 `topk_length < 128` 时要补齐（padding），补出来的无效 token 虽然会被掩码置 `-inf`，但 MMA 仍然按整 tile 算，浪费算力与 smem。

于是作者另写了一个 **`fwd_for_small_topk`** 变体：用更细的 `B_TOPK=64` tile、更深的流水缓冲，专门服务小 `topk` 场景。它目前**只支持 `D_QK=512`**（即 MODEL1 的 KV 格式），并且在接口层有一个明确的选择阈值 `topk <= 1280`（见 4.3.3）。

> 一句话动机：**普通 kernel 为「长循环 + 大 tile」优化；小 topk 是「短循环 + 真实 token 少」，需要更细的 tile 和更深的流水来填满硬件。**

#### 4.2.2 核心流程

small_topk 的算法骨架仍是 4.1.2 那套 online softmax 三段流水，区别全在「静态配置」和「外层循环分发」上：

1. **外层循环（`run_outer_loop`）**：根据 `FWD_MODE` 决定如何分发工作。prefill 模式用 **CLC** 向硬件领 `s_q`；decode 模式读 `DecodingSchedMeta`，按 split-KV 的 `[begin_block_idx, end_block_idx)` 区间循环（详见 4.3）。
2. **内层主循环**：对每个 KV 块执行 Copy(KV) → MMA(P, O) → Scale&Exp，缓冲数 `NUM_K_BUFS`（prefill=4，decode=3）。
3. **Epilogue**：把 TMEM 里的 `O` 取出、乘 `output_scale`、转 bf16 写回；decode(splitKV) 模式则写 float32 的 `o_accum`。

#### 4.2.3 源码精读

**(a) 静态配置与普通 head128 的差异**——这是本讲实践任务的核心。打开 [fwd_for_small_topk/head128/config.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd_for_small_topk/head128/config.h)：

- [config.h:47-56](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd_for_small_topk/head128/config.h#L47-L56)：`static_assert(D_QK == 512)`（只支持 512！）；`H_Q=128`、`B_TOPK=64`（普通 head128 是 128）、`NUM_THREADS=128*4=512`。
- [config.h:61-63](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd_for_small_topk/head128/config.h#L61-L63)：`NUM_K_BUFS = IS_DECODE ? 3 : 4`（普通 head128 是 `NUM_BUFS=2`，prefill 这里更深）、`NUM_RAW_K_BUFS = IS_DECODE ? 2 : 0`（decode 才需要 raw FP8 缓冲）。
- [config.h:65-68](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd_for_small_topk/head128/config.h#L65-L68)：`D_NOPE=448, D_ROPE=64`，`TMA_K_STRIDE_FOR_DECODING = D_NOPE + 2*D_ROPE = 576`，`NUM_SCALES_EACH_TOKEN = 8`（7 个 e8m0 scale + 1 padding）——这些字段是 decode 的 FP8 KV 量化专用（承接 u5-l1 的 MODEL1 布局）。
- [config.h:70-73](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd_for_small_topk/head128/config.h#L70-L73)：epilogue 块大小分两套：`B_EPI=64`（prefill / 非 splitKV）、`B_EPI_SPLITKV=32`（splitKV 解码，块更小以便多缓冲 `NUM_EPI_SPLITKV_BUFS=4`）。

把它们与 4.1.3 的普通 head128 放一起，差异表如下：

| 配置项 | 普通 head128 | small_topk head128 |
|---|---|---|
| `B_TOPK`（cluster 级） | 128 | **64** |
| 流水缓冲数 | `NUM_BUFS=2` | **`NUM_K_BUFS=4`(prefill)/3(decode)** |
| 支持的 `D_QK` | 512 / 576 | **仅 512** |
| 用途 | 仅 prefill | **prefill + decode(splitKV)** |
| FP8 / scale / `o_accum` 字段 | 无 | **有**（decode 专用） |
| epilogue 块 | `B_EPI=64` | **64 / 32(splitKV)** |

**(b) decode 模式的反量化**——small_topk 在 decode 时直接吃 FP8 KV，由 warpgroup 1 做反量化。注意注释写得很直白：

- [phase1.cuh:397-398](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd_for_small_topk/head128/phase1.cuh#L397-L398)：`// KV fetching threads for prefill, dequant threads for decoding`——同一个 warpgroup 在两种模式下干不同的活（`if constexpr (!IS_DECODE)`）。
- [phase1.cuh:453-531](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd_for_small_topk/head128/phase1.cuh#L453-L531)：decode 分支。它从 `smem.K_raw`（FP8）读出 `fp8_e4m3`，用 `__nv_cvt_e8m0x2_to_bf162raw` 把 e8m0 scale 转成 bf16，再 `fp8x2_to_bf16x2_with_scale` 反量化，结果写进 `smem.K`（bf16）供后续 MMA 使用。这正是 u5-l2 那条「fp8→bf16 反量化」流水在 SM100 上的落地。

**(c) 为什么更深缓冲能帮小 topk**：小 topk 时主循环短，2 个缓冲不足以让 Copy 和 MMA 充分重叠；prefill 用 4 个缓冲、decode 用 3 个 + 2 个 raw FP8 缓冲，相当于把流水加深，让短循环也能维持「拷贝下一块的同时算当前块」。

#### 4.2.4 代码实践

**实践目标**：亲手对比普通 head128 与 small_topk head128 的 `config.h`，把差异落到一张表上，并解释小 topk 为何需要专门变体（即本讲指定的实践任务）。

**操作步骤**：

1. 并排打开 [fwd/head128/config.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd/head128/config.h) 与 [fwd_for_small_topk/head128/config.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd_for_small_topk/head128/config.h)。
2. 逐项核对 4.2.3 的差异表，确认每一条都能在源码里找到对应行。
3. 回答：把 `B_TOPK` 从 128 减到 64，对 `topk_length=300` 的请求分别意味着几个 KV 块？
4. 回答：为什么 small_topk 把 prefill 的缓冲从 2 加到 4？

**需要观察的现象 / 预期结果**：
- 普通 head128：`ceil_div(300, 128) = 3` 块（最后一块只有 44 个有效 token，要补 84 个 padding）。
- small_topk：`ceil_div(300, 64) = 5` 块（最后一块 44 个有效，补 20 个 padding）。
- 块更多 → 每块更小 → padding 浪费更少；同时块数变多让短循环也能重叠，于是把缓冲加深到 4 来支撑更细粒度的流水。

> 待本地验证：上述 padding 浪费分析是静态推理；若想看实际加速，需要在 B200 上跑 `benchmark/bench_flash_mla.py` 对比 `topk` 扫描下两条路径的 TFlops（本讲环境无 GPU，跳过）。

#### 4.2.5 小练习与答案

**Q1**：small_topk 为什么 `static_assert(D_QK == 512)`，不支持 576？
**答**：small_topk 的 decode 分支按 MODEL1 的 FP8 字节布局硬编码了 `D_NOPE=448 / D_ROPE=64` 与 8 个 scale（[config.h:65-68](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd_for_small_topk/head128/config.h#L65-L68)），这套布局对应 `D_QK=512`。支持 575/576 的 V3.2 布局会引入另一套反量化与 TMA 描述符，作者选择不在这一变体里覆盖，而是在接口层让 576 走普通 head128（见 4.3.3）。

**Q2**：`B_TOPK=64` 会不会让大 `topk` 场景反而变慢？
**答**：会。`B_TOPK` 越小，同样 `topk` 要跑的块越多，调度与 barrier 开销更大。所以接口层用 `topk <= 1280` 作为分界（[sparse_fwd.h:225](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L225)）：小 topk 走 small_topk，大 topk 走普通 head128，各取所长。

---

### 4.3 prefill / decode 复用：同一份 kernel 的模板开关

#### 4.3.1 概念说明

这是 small_topk 设计上最巧妙的一点：**一个 kernel 模板，编译出 prefill 和 decode 两份机器码**。

为什么能复用？因为 sparse prefill 和 sparse decode 在「每个 query 只对一小撮 KV token 做注意力」这件事上**计算结构完全相同**——都是 `P=QK^T` + `O+=SV` 的 online softmax，都按 `indices` gather KV。区别只在「数据来源与输出方式」：

| 维度 | prefill | decode (splitKV) |
|---|---|---|
| 工作分发 | CLC 领 `s_q` | 读 `DecodingSchedMeta` |
| KV dtype | bf16 | FP8（带 scale） |
| 输出 | 最终 `out`(bf16) | `o_accum`(float32，待 combine 归并) |
| 是否有 batch | 无（`s_q` 维） | 有 `b` 维 + split |

既然算法一样、只有这些「配置项」不同，最干净的做法就是用一个**模板参数 `FWD_MODE`** 在编译期把差异 `if constexpr` 掉，让两份特化共享同一套主循环代码。

#### 4.3.2 核心流程

复用通过四层 `constexpr` 开关实现：

1. **模板参数**：`template<SparseAttnFwdMode FWD_MODE, int D_QK> struct KernelTemplate`。
2. **参数类型别名**：`using ArgT = SparseFwdArgT<FWD_MODE>;`——prefill 解析为 `SparseAttnFwdParams`，decode 解析为 `SparseAttnDecodeParams`。
3. **编译期布尔**：`IS_DECODE = is_decode_v<FWD_MODE>`、`IS_PREFILL = !IS_DECODE`，贯穿整个 kernel 的 `if constexpr`。
4. **两套 TmaParams**：`std::conditional_t<IS_DECODE, TmaParamsForDecode, TmaParamsForPrefill>` 选其一。
5. **两个具名启动器**：同一份 device 函数 `sparse_attn_fwd_kernel_devfunc`，套上两个不同的 `__global__` 名字（`sparse_attn_fwd_for_small_topk_kernel` 与 `flash_fwd_splitkv_mla_fp8_sparse_kernel`），分别对应 prefill / decode。

外层循环 `run_outer_loop` 用一个 lambda 把两种分发方式抽象掉：prefill 分支用 CLC 循环领 `s_q`，decode 分支按 sched_meta 遍历 `[begin_req_idx, end_req_idx]`。

#### 4.3.3 源码精读

**(a) 模板开关与类型派发**：

- [config.h:16-21](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd_for_small_topk/head128/config.h#L16-L21)：`KernelTemplate<FWD_MODE, D_QK>`，`ArgT = SparseFwdArgT<FWD_MODE>`，`IS_DECODE / IS_PREFILL` 两个 constexpr bool。
- [config.h:25-45](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd_for_small_topk/head128/config.h#L25-L45)：`TmaParamsForPrefill`（`tensor_map_q/kv/o`）与 `TmaParamsForDecode`（多了 `o_accum`、`kv_nope/kv_rope`、`extra_kv_nope/rope`），由 `std::conditional_t` 二选一。

**(b) 外层循环的两种分发**：

- [phase1.cuh:103-146](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd_for_small_topk/head128/phase1.cuh#L103-L146)：decode 分支。`KU_LDG_256` 从 `params.tile_scheduler_metadata_ptr` 读一条 `DecodingSchedMeta`，按 `begin_req_idx..end_req_idx` 遍历 batch，每个 batch 再按 `begin_block_idx/end_block_idx` 切出 split 区间，并算出 `is_no_split` / `n_split_idx`（与 u4 的 split-KV 缓冲对接）。
- [phase1.cuh:147-187](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd_for_small_topk/head128/phase1.cuh#L147-L187)：prefill 分支。用 `ku::get_clc_query_response`（CLC）循环领 `next_job`，每领到一个 `s_q_idx` 就跑一遍主循环；用 `bar_clc_full/empty` 这对 barrier 与 CLC 握手。`bar_clc_*` 是 prefill 专属（[config.h:106](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd_for_small_topk/head128/config.h#L106)），`bar_raw_KV_*` 是 decode 专属（[config.h:109](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd_for_small_topk/head128/config.h#L109)）。

**(c) 两个具名启动器 + 统一 device 函数**：

- [phase1.cuh:933-945](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd_for_small_topk/head128/phase1.cuh#L933-L945)：两个 `__global__` 包装器，函数体都只是 `Kernel::sparse_attn_fwd_kernel_devfunc(params, tma_params)`。注释点明用意：`// We have two launchers with different kernel names to distinguish prefill and decode`——不同名字让 NVCC 生成两份独立 kernel，也方便 profiler 区分。
- [phase1.cuh:1078](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd_for_small_topk/head128/phase1.cuh#L1078)：`auto kernel = IS_PREFILL ? &sparse_attn_fwd_for_small_topk_kernel<...> : &flash_fwd_splitkv_mla_fp8_sparse_kernel<...>;`——在 host 端二选一。
- [phase1.cuh:1082-1087](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd_for_small_topk/head128/phase1.cuh#L1082-L1087)：grid 形状也按模式不同——decode 是 `2*s_q × num_sm_parts`（splitKV 沿 SM 切），prefill 是 `2*s_q × 1`。

**(d) 接口层如何选用**：prefill 侧与 decode 侧分别在不同的接口函数里调用同一个 `run_fwd_for_small_topk_phase1_kernel`，只是模板实参不同：

- prefill：[sparse_fwd.h:97](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L97) `run_fwd_for_small_topk_phase1_kernel<SparseAttnFwdMode::Prefill, 512>(params)`，当 `topk <= 1280` 且 feature 满足时被选中（[sparse_fwd.h:220-234](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L220-L234)）。
- decode：[sparse_decode.h:179](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L179) `run_fwd_for_small_topk_phase1_kernel<SparseAttnFwdMode::DecodeWithSplitKV, 512>(params)`，由 `Decode_Sm100_Head128_Impl` 在 MODEL1（d_qk=512）head128 解码时调用。

> 注意 [csrc/sm100/decode/head128/README.md](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/decode/head128/README.md) 一句话点破了这个复用：head128 的解码 kernel 「位于 `fwd_for_small_topk/head128/instantiations/phase1_decode_k512.cu`，或用 2× head64 kernel 模拟」。也就是说，SM100 上 head128 解码根本没有独立 kernel 目录，而是直接住在 sparse prefill 的 small_topk 实现里。

#### 4.3.4 代码实践

**实践目标**：追踪 small_topk kernel 的 prefill/decode 双面性，画出「接口 → 模板实参 → 启动器 → device 函数」的映射。

**操作步骤**：

1. 打开 [sparse_fwd.h:86-99](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L86-L99)（`Fwd_Sm100_Head128_Small_TopK_Impl`）和 [sparse_decode.h:156-181](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_decode.h#L156-L181)（`Decode_Sm100_Head128_Impl`），确认两者调用的是同一个 `run_fwd_for_small_topk_phase1_kernel`，只是第一模板参数分别是 `Prefill` 与 `DecodeWithSplitKV`。
2. 打开两个实例化文件 [phase1_prefill_k512.cu](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd_for_small_topk/head128/instantiations/phase1_prefill_k512.cu) 与 [phase1_decode_k512.cu](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd_for_small_topk/head128/instantiations/phase1_decode_k512.cu)，确认它们显式实例化的模板实参不同、`params` 类型也不同（`SparseAttnFwdParams` vs `SparseAttnDecodeParams`）。
3. 画一张映射图（文字版即可）：
   - prefill：`sparse_attn_prefill_interface` → `Fwd_Sm100_Head128_Small_TopK_Impl::run_` → `run_fwd_for_small_topk_phase1_kernel<Prefill,512>` → `sparse_attn_fwd_for_small_topk_kernel` → `devfunc`。
   - decode：`sparse_attn_decode_interface` → `Decode_Sm100_Head128_Impl::run_` → `run_fwd_for_small_topk_phase1_kernel<DecodeWithSplitKV,512>` → `flash_fwd_splitkv_mla_fp8_sparse_kernel` → 同一个 `devfunc`。

**需要观察的现象 / 预期结果**：两条链路最终汇聚到 [phase1.cuh:24](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd_for_small_topk/head128/phase1.cuh#L24) 的同一个 `sparse_attn_fwd_kernel_devfunc`，差异全被 `FWD_MODE` 编译期开关吃掉。这是「一份算法代码 + 模板特化」的典型范例。本实践为源码阅读型，无需 GPU。

#### 4.3.5 小练习与答案

**Q1**：为什么 prefill 用 CLC，decode 却读 `DecodingSchedMeta`？
**答**：prefill 没有 batch 维，工作单元就是 `s_q` 个 query，数量可能很大且均匀，用 CLC 让常驻 kernel 动态领活、减少 launch 开销并自均衡；decode 有 batch 维且要按 split-KV 切分（每个 SM partition 负责一段 `[begin_block_idx, end_block_idx)`），必须由 tile scheduler 预先算好 `DecodingSchedMeta`（见 u4-l3）来保证 split 与 combine 的对齐，所以走显式元数据。

**Q2**：两个 `__global__` 启动器函数体一模一样，为什么要写两个？
**答**：见 [phase1.cuh:933](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd_for_small_topk/head128/phase1.cuh#L933) 注释——不同名字让 NVCC 为 prefill 和 decode 生成两份独立的 kernel 二进制（各自有不同的 `FWD_MODE` 特化、不同的 `__launch_bounds__` 行为），也方便 nsight profiler 区分两种阶段。

**Q3**：`!regular_impl.check_if_all_features_are_supported(required_features)` 这个兜底分支（[sparse_fwd.h:226](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L226)）什么时候会触发？
**答**：当普通 head128 实现不支持请求的 feature 集合时触发。当前普通 head128 支持所有 small_topk 支持的 feature 且更多，所以这个分支主要是「安全网」——一旦未来 small_topk 支持了普通 head128 不支持的某项 feature，这条逻辑能保证仍可降级到 small_topk，而不是直接抛错。

---

### 4.4 公共子例程 common_subroutine.h

#### 4.4.1 概念说明

写多个 sparse kernel 时，有几段逻辑是**每种实现都要重复一遍**的：

- 把 `indices` 从显存装进来，并生成「这个 token 是否有效」的掩码；
- 把 UMMA 算出的 `P`（在 dual gemm 下是两半）从 TMEM 取出、掩码、跨 warp 归约成一份；
- online softmax 里对 `O` 的 rescale、求 `P` 的行最大、算 `S=exp2(...)` 与求和。

`common_subroutine.h` 就是把这些重复逻辑抽成 `CUTE_DEVICE` 函数，供 SM100 的 head64 与 small_topk head128 复用（普通 head128 因为 cluster 结构不同，自行内联了等价逻辑）。它是「算法无关、硬件相关」的工具层。

#### 4.4.2 核心流程

五个子例程各自很薄，但串起来正好是 online softmax 的一个完整内层迭代：

1. **`load_indices_and_generate_mask`**：每个线程装 8 个 index，按「`0 <= index < s_kv` 且绝对位置 `< topk_length`」生成一个 8-bit 掩码。
2. **`retrieve_mask_and_reduce_p`**：从 TMEM 取出 dual gemm 产生的两半 `P`，按掩码把无效 token 置 `-inf`，再用 smem 交换 + NamedBarrier 把两半归约成一份。
3. **`get_max`**：线程内对 `P` 求最大。
4. **`get_s_from_p`**：算 `s = exp2(p*scale - new_max)` 并求和，同时把 `s` 转成 bf16 供 `O+=SV` 用。
5. **`rescale_O`**：在 TMEM 里把 `O` 逐块读出、乘 `scale_factor`、写回（即 4.1.2 公式里的 `O *= scale_for_old`）。

#### 4.4.3 源码精读

**(a) 索引装载与掩码生成**——[common_subroutine.h:13-44](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/common_subroutine.h#L13-L44)：

- 用 `KU_LDG_256` 一次性 256 字节（8 个 int）装载；`is_valid` 同时检查 `index` 范围和 `abs_pos < topk_length`（后者实现 u6-l1 的 `topk_length` 截断语义）；最后把 8 个布尔压成一个 `char` 掩码返回。
- 注释强调「掩码在归约前做」：因为 `(-inf) + anything = -inf`，先掩码再归约保证正确性，且能和 smem 装载重叠。

**(b) P 的取出、掩码与归约**——[common_subroutine.h:67-134](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/common_subroutine.h#L67-L134)：

- 头部注释画出了 dual gemm 后 `P` 在 TMEM 里的两半布局（warp0/warp2 一组、warp1/warp3 一组）；函数先用 `ku::tmem_ld_32dp32bNx` 把两半读进寄存器 `p` 和 `p_peer`。
- 用掩码把无效位置 `-inf`，再通过 `p_exchange_buf` 让相邻 warp 交换数据、`NamedBarrier::arrive_and_wait` 同步，最后相加归约成一份 `p`。
- `STORE_BACK_P` 模板参数控制是否把归约后的 `p` 写回 smem（供后续写 `S` 用）。

**(c) O 的 rescale**——[common_subroutine.h:141-168](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/common_subroutine.h#L141-L168)：

- 把 `O` 在 TMEM 里按 `CHUNK_SIZE` 分块，循环「`tmem_ld` 读出 → 乘 `scale_factor` → `tmem_st` 写回」，配合 `fence_view_async_tmem_load/store` 保证 TMEM 访问顺序。这就是 online softmax 把历史 `O` 缩放到新 `mi` 的原地操作。

**(d) get_max / get_s_from_p**——[common_subroutine.h:170-206](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/common_subroutine.h#L170-L206)：

- `get_max` 是朴素的线程内 `max` 归约；`get_s_from_p` 用 `float2_fma` 算 `exp2(p*scale - new_max)`，累加得到 `cur_sum`（即 `li` 增量），并把结果转成 `nv_bfloat162` 返回。

**谁在调用它们**：用 Grep 可以确认，`retrieve_mask_and_reduce_p` / `load_indices_and_generate_mask` 只被 [head64/phase1.cuh](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd/head64/phase1.cuh) 与 [fwd_for_small_topk/head128/phase1.cuh](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd_for_small_topk/head128/phase1.cuh) 复用——也就是说，head64 和 small_topk 共享这套子例程，普通 head128 因 cluster 内联而不在调用者之列。

#### 4.4.4 代码实践

**实践目标**：跟踪 head64 kernel 如何调用 `common_subroutine` 完成一个完整的 softmax 内迭代。

**操作步骤**：

1. 打开 [head64/phase1.cuh:169-244](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd/head64/phase1.cuh#L169-L244)（Scale & Exp warpgroup 的主循环）。
2. 按顺序定位四个调用点：
   - `retrieve_mask_and_reduce_p<...>(...)`（约 L179）——取 P + 掩码 + 归约；
   - `get_max<NUM_ELEMS_PER_THREAD>(p)`（约 L195）——求 `cur_pi_max`；
   - `get_s_from_p<NUM_ELEMS_PER_THREAD>(s, p, ...)`（约 L222）——算 `S` 与 `cur_sum`；
   - `rescale_O<D_V, 32, tmem_cols::O>(scale_for_old)`（约 L238）——缩放历史 O。
3. 把这四步与 4.1.2 的 online softmax 公式一一对应。

**需要观察的现象 / 预期结果**：你会看到「`P` → `mi` 更新 → `S` 计算 → `O` rescale」这一串恰好对应数学公式里的 \(O \leftarrow O \cdot 2^{mi-new\_max} + SV\) 与 \(li \leftarrow li \cdot 2^{mi-new\_max} + \sum S\)，公共子例程把每一块都封装成了可复用的 device 函数。本实践为源码阅读型，无需 GPU。

#### 4.4.5 小练习与答案

**Q1**：`retrieve_mask_and_reduce_p` 为什么要在归约前做掩码（置 `-inf`）？
**答**：见 [common_subroutine.h:99-100](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/common_subroutine.h#L99-L100) 注释：\((-\infty) + \text{任何有限值} = -\infty\)（且不会产生 NaN，只要其它值非 NaN/+\infty），所以先掩码再归约结果正确；先归约再掩码则会在 `max` 操作里丢失 `-inf` 信息或引入 NaN。提前掩码还能和 smem 装载重叠，提升性能。

**Q2**：`rescale_O` 为什么用 `CHUNK_SIZE` 分块，而不是一次性处理整个 `D_V`？
**答**：TMEM 的 load/store 是异步的，且寄存器有限。分块（`CHUNK_SIZE=32`）可以在「读一块、乘一块、写一块」之间用 `fence_view_async_tmem_*` 插入恰当的等待，让 TMEM 访问与浮点乘法重叠；一次性处理整行 `D_V/2=256` 会占用过多寄存器并破坏流水。

---

## 5. 综合实践

**任务**：把本讲四个模块串起来，画出 SM100 sparse prefill 的「接口派发决策树」，并用一张总表总结三条实现路径。

**步骤**：

1. 从 [sparse_fwd.h:213-240](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L213-L240) 提取 SM100f 的派发逻辑，画出决策树：
   - `h_q == 64` → `Fwd_Sm100_Head64_Impl`（4.1 的 head64）；
   - `h_q == 128` → 同时构造 `Fwd_Sm100_Head128_Small_TopK_Impl` 与 `Fwd_Sm100_Head128_Impl`，按下式二选一：
     - `topk <= 1280` 且 small_topk 支持 feature → **small_topk**（4.2/4.3）；
     - 否则（含 `d_qk == 576`，small_topk 不支持）→ **普通 head128**（4.1）。
2. 把三条路径的关键属性填进总表：

| 路径 | CTA 数 | `B_TOPK` | 支持 `D_QK` | 用途 | 是否用 common_subroutine |
|---|---|---|---|---|---|
| head64 | 1 | 64 | 512/576 | prefill（h_q=64） | 是 |
| 普通 head128 | 2(cluster) | 128 | 512/576 | prefill（h_q=128，大 topk） | 否（内联） |
| small_topk head128 | 2(cluster) | 64 | 仅 512 | prefill（小 topk）+ decode(splitKV) | 是 |

3. 用 2~3 句话解释：为什么 `d_qk=576` 的 head128 请求在 `topk=500` 时**不会**走 small_topk？
   - 提示：看 [sparse_fwd.h:86-93](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/sparse_fwd.h#L86-L93)，small_topk 的 `DECLARE_SUPPORTED_FEATURES` 里没有 `HEAD_DIM_576`，于是 `check_if_all_features_are_supported` 返回 false，`topk<=1280 && supported` 短路失败；而普通 head128 支持 576，`!regular_impl.check` 也是 false，最终走普通 head128。

**预期结果**：你会得到一张完整的「SM100 sparse prefill 路由图」，并能解释每个分支背后的 tile 配置与 feature 集合原因。这一张图也是通向 u6-l4（sparse fwd 接口与实现选择）的直通车。

## 6. 本讲小结

- SM100 sparse prefill 的普通 `phase1` 按 `h_q` 分两条：**head64**（单 CTA，全 Q 进 TMEM，TS+SS 各一种 MMA）与 **head128**（2-CTA cluster，Q 拆 tQ(TMEM)/sQ(smem)，P 用 TS+SS 两种 MMA 累加）。
- 算法骨架仍是 online softmax 三段流水（Copy|MMA|Scale&Exp），Blackwell 把累加器 `O`、部分 `Q`、`P` 都搬进了 **TMEM**，用 **UMMA/UTCCP** 与 **2x1SM cluster MMA** 取代 Hopper 的 WGMMA。
- **small_topk** 变体用更细的 `B_TOPK=64`、更深的缓冲（prefill 4 / decode 3）专门服务小 `topk`（接口阈值 `topk<=1280`），只支持 `D_QK=512`。
- small_topk 用**模板参数 `SparseAttnFwdMode`** 把 prefill 和 decode 编进同一份代码：prefill 用 CLC 分发、吃 bf16 KV、写最终 `out`；decode 读 `DecodingSchedMeta`、吃 FP8 KV、写 `o_accum`。
- 接口层用 `FwdFeatures` 子集校验 + `topk<=1280` 阈值，在普通 head128 与 small_topk head128 之间选择；decode 侧的 `Decode_Sm100_Head128_Impl` 直接复用 small_topk kernel。
- `common_subroutine.h` 抽出索引装载/掩码、P 归约、O rescale、求 max/S 等公共逻辑，供 head64 与 small_topk 复用。

## 7. 下一步学习建议

- **u6-l4（sparse fwd 接口与实现选择）**：本讲的决策树在那篇讲义里被放大成完整的 `FwdFeatures` 派发流程，建议紧接着读，把接口校验、feature 构造、small_topk 选择阈值与 fallback 分支串成一条完整链路。
- **u5-l3 / u5-l4（FP8 sparse decode）**：本讲 4.2.3 的 decode 反量化分支是 u5 系列的反量化/crossover 技术在 SM100 上的对应实现；如果你对 `fp8x2_to_bf16x2_with_scale`、e8m0 scale 的来源感兴趣，回头读 u5-l1/u5-l2。
- **直接读源码**：想看 CLC 与 split-KV 外层循环的全貌，重点啃 [fwd_for_small_topk/head128/phase1.cuh:101-190](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd_for_small_topk/head128/phase1.cuh#L101-L190)；想看 Blackwell 2-CTA cluster 的协同，重点啃 [fwd/head128/phase1.cuh:447-570](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/sparse/fwd/head128/phase1.cuh#L447-L570) 的三个 warpgroup。
