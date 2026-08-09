# Split-K 归约

## 1. 本讲目标

本讲围绕 QuACK GEMM 的 **Split-K（沿收缩轴切分）** 机制展开。读完本讲，你应当能够：

- 说清 **为什么** 要沿 K 维切分一个矩阵乘，以及它带来「并行度提升」与「需要一次归约」的取舍。
- 区分 `SplitKMode` 的三种合并模式（`SERIAL` / `PARALLEL` / `SEPARATE`）在**确定性、延迟、实现路径**上的差异。
- 读懂 `split_k_reduce.py` 这个独立的归约内核，理解它如何把若干 f32 部分和在**固定顺序**下求和并施加线性 epilogue。
- 解释为什么 SEPARATE 模式要把 `alpha`/`beta`/`C`/`bias` **全部路由到归约内核**，而不是在 GEMM 里就算掉。
- 抓住贯穿三种模式的**唯一不变量**：完整线性 epilogue 只在「拥有已完成 f32 和」的那个实体上运行**恰好一次**。

## 2. 前置知识

在进入本讲前，你需要具备以下认知（它们在依赖讲义 `u5-l1` 及更早讲义中已建立）：

- **GEMM 的主循环与 epilogue**：QuACK 的 GEMM 设备内核分两段——主循环（mainloop）做 A/B 加载与 MMA 累加，epilogue 在累加器凑齐后做 `D = α·D + β·C + rowvec + colvec` 的收尾存储。见 `u5-l1`。
- **累加器是 f32**：无论输入是 bf16/fp8/tf32，MMA 的累加器都是 `Float32`。Split-K 的部分和（partials）也一律以 f32 流转。
- **持久化内核与 tile 调度**：GEMM 是持久化内核，一个 CTA 会连续处理多个输出 tile，由 `TileScheduler` 分配 work index。见 `u3-l4`。
- **CuTe-DSL 的 `const_expr`**：被 `const_expr` 包裹的判断在**编译期折叠**，只编入命中分支。`split_k == 1` 时的整条 split-K 分支会被编译期剔除。见 `u1-l4`。
- **gmem 计数信号量 `Semaphore`**：一个 elected thread 自旋 `acquire load` 等待，再用 `release_store`（写绝对值，确定性）或 `release_add`（原子自增，可交换）发布进度。见 `u3-l5`。

> 关键直觉：浮点加法**不满足结合律**。`(a+b)+c` 与 `a+(b+c)` 在计算机里可能给出不同结果。因此「把 S 个部分和按什么顺序加起来」直接决定了结果是否**逐位可复现（bitwise deterministic）**——这正是 SERIAL 与 PARALLEL 的核心分野。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `quack/gemm_config.py` | 定义 `SplitKMode` 枚举（三模式的唯一真相源）。 |
| `quack/gemm_base.py` | 设备侧：`split_k_partial_commit`（非终结 split 提交部分和）、`epilogue_split_k`（终结协议）、`_frag_stripe_op`（工作区读写）。 |
| `quack/gemm.py` | 主机侧：`_staged_split_k_workspace`、`_split_k_buffers`（分配信号量与工作区）、`_reduce_staged_split_k`、`run_gemm_plan`（SEPARATE 路由）、`_build_gemm_plan`（编译签名重定向）。 |
| `quack/split_k_reduce.py` | 独立归约内核 `SplitKReduce`：把 staged partials 求和并施加线性 epilogue。 |
| `quack/sync/barrier.py` | `Semaphore` 原语：`wait_eq` / `release_store` / `release_add`。 |
| `tests/test_gemm_split_k.py` | 数值正确性、run-to-run 逐位确定性、split_k=1 与基线逐位一致等回归测试。 |

## 4. 核心概念与源码讲解

### 4.1 为什么需要 Split-K 与三种合并模式（SplitKMode）

#### 4.1.1 概念说明

考虑一次矩阵乘 `D[M,N] = A[M,K] @ B[K,N]`。GPU 上的高性能 GEMM 把输出切成 tile（如 `128×128`），每个 tile 由一个 CTA 负责计算。CTA 数量大约是 `⌈M/tile_M⌉ × ⌈N/tile_N⌉`。

当 **M 和 N 都很小、K 却很大**时，输出 tile 数量太少，填不满 GPU 上成百上千个 SM——这叫**占用率不足（occupancy-starved）**，大量 SM 闲置、K 维的计算压在少数 CTA 上串行进行。

Split-K 的对策：**沿 K 维把乘积累加拆成 `split_k` 段**。每个输出 tile 不再由一个 CTA 一口气算完整个 K，而是由 `split_k` 个 CTA 各算一段 K 的部分积（partial），最后把 `split_k` 个部分和加起来。

代价是显然的：本来一次乘法就出结果，现在多了一次「把部分和归约（reduce）成最终结果」的步骤。所以 Split-K 是用「一次额外归约」换「tile 数 × split_k、占用率提升」——只在占用率不足时才划算。这也是为什么公共 API 里 `split_k=None` 表示**交给 autotuner 判断**是否启用（见 `u8-l1`）。

#### 4.1.2 核心流程

不论哪种模式，全流程都是：

```
1. 把 K 分成 split_k 段。
2. 每个 split 对应一组 CTA，各自算自己那段 K 的 f32 部分积（partials）。
3. 把 split_k 个 partials 「合并（combine）」成最终输出 tile。
4. 在「合并完成」的实体上，运行完整线性 epilogue 恰好一次，写出 D。
```

步骤 3「怎么合并」就是 `SplitKMode` 三选一的分歧点。三者的**统一不变量**是：

> 完整 epilogue（`α·D + β·C + rowvec + colvec`）只在拥有「已完成 f32 和」的实体上运行**恰好一次**。非终结的 split 只产出裸 f32 部分和，不做任何 epilogue 数学。

这一点写在 `gemm_base.py` 的核心注释里，请仔细读：

[quack/gemm_base.py:84-98](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L84-L98) —— 这段注释一句话点明三种模式与「epilogue 只跑一次」的不变量。

#### 4.1.3 源码精读

`SplitKMode` 是一个 `IntEnum`，定义在叶子模块 `gemm_config.py`（之所以放这里而不是 `gemm_interface.py`，是因为内核层 `gemm_base` 在 import 期就要消费它，而 `gemm_interface` 在导入图里位于 `quack.gemm` 之上，放上面会成环）：

[quack/gemm_config.py:9-33](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_config.py#L9-L33) —— 三模式枚举定义。`IntEnum` 的选择不是偶然：值以普通 int 形式穿过 `torch.library` 自定义算子的 schema 边界，并能稳定 pickle 进 jit-cache 的 key。

三段注释（`gemm_config.py:22-33`）精确概括了每种模式的语义，这里提炼为对照表：

| 模式 | 合并方式 | 确定性 | 延迟 | 实现位置 |
|------|----------|--------|------|----------|
| `SERIAL` (0) | 旋转门（turnstile）按 split 顺序提交；最后一个 split 终结 | ✅ 逐位确定 | 中 | GEMM 内核内 |
| `PARALLEL` (1) | 到达顺序提交（不等）；到达计数器填满后最后一个 split 终结 | ❌ 不确定（最快） | 最低 | GEMM 内核内 |
| `SEPARATE` (2) | 每个 split 写自己的工作区切片；独立归约内核按固定顺序求和 | ✅ 逐位确定 | 高（多一次内核启动） | 独立内核 `split_k_reduce.py` |

注意「所有模式都以 f32 提交裸部分和；完整 epilogue 在拥有已完成 f32 和的实体上恰好运行一次」这句话（`gemm_config.py:22-24`）——它是后两小节的纲领。

#### 4.1.4 代码实践

**实践目标**：用源码阅读确认 Split-K 只在占用率不足时才被启用。

**操作步骤**：

1. 打开 `quack/gemm_interface.py`，找到 `_expand_split_k_configs`（约第 371 行）及其 docstring。
2. 阅读它的注释，回答：它在什么条件下才会为候选配置追加 `split_k > 1` 的变体？调用前提（`split_k is None`）说明什么？

**需要观察的现象**：函数只在用户传 `split_k=None`（让 autotuner 自己选）时被调用，且只对「占用率不足」的形状扩展。这印证了 Split-K 的设计动机。

**预期结果**：你会看到它对「形状偏小、SM 装不满」的情况才生成 split-K 候选——这是「Split-K 是占用率补救手段」的代码佐证。（运行时行为待本地 GPU 验证。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `SplitKMode` 用 `IntEnum` 而不是普通 `Enum`？

> **答案**：`IntEnum` 的值是普通 int，能直接穿过 `torch.library` 自定义算子 schema 的类型边界，并且能稳定 pickle 进 jit-cache 的缓存键（`gemm_config.py:18-20`）。普通 `Enum` 不会。

**练习 2**：三种模式都遵守的「唯一不变量」是什么？

> **答案**：完整线性 epilogue 只在拥有「已完成 f32 和」的实体上运行**恰好一次**；非终结的 split 只产出裸 f32 部分和，不做任何 epilogue（否则 `α`/`β`/`C`/`bias` 会被施加 `split_k` 次）。

---

### 4.2 SERIAL / PARALLEL：内核内就地终结（per-tile 信号量与 partials 工作区）

#### 4.2.1 概念说明

`SERIAL` 和 `PARALLEL` 共用同一套设备侧机制：**合并发生在 GEMM 内核内部，不另起内核**。调度器把 work-id 空间沿 split-K 维放大 `split_k` 倍，于是同一个输出 tile 会被 `split_k` 个不同的 CTA「分工」处理。

对每个输出 tile，这 `split_k` 个 CTA 中：

- **非终结 split**（`split_idx < split_k - 1`）：把自己那段 K 算出的累加器片段（f32）写进该 tile 的**共享工作区**（per-tile partials workspace），**完全不跑 epilogue**。
- **终结 split**（`split_idx == split_k - 1`）：先算自己那段 K，再等所有「兄弟」split 把部分和写完，把工作区里别人的部分和折叠进自己的累加器，**然后**跑完整 epilogue 并写出 D。

两种模式的区别只在「非终结 split 如何写工作区、如何同步」：

- **SERIAL（旋转门）**：用「绝对值发布」`release_store(split_idx+1)`。只有当 `flag == split_idx`（即前 `split_idx` 个 split 都已提交）时，split `split_idx` 才被允许写。split 0 用普通 store 初始化区域，其余 split 用 `red.add`。→ **提交顺序固定为 0,1,2,…,S−1，逐位确定**。
- **PARALLEL（到达计数器）**：用「自增发布」`release_add()`，host 预先把工作区清零，每个 split 一到就 `red.add`（不等待）。→ **提交顺序即到达顺序，每次运行可能不同，不确定但最快**。

#### 4.2.2 核心流程

SERIAL 模式下，对某个输出 tile，s 个 split 的时序：

```
split 0:  store(初始化区域)          → release_store(1)
split 1:  wait_eq(1) → red.add       → release_store(2)
split 2:  wait_eq(2) → red.add       → release_store(3)
...
split S-1 (终结): wait_eq(S-1) → fold 工作区 + 自己的部分 → 跑 epilogue → 写 D → release_store(0) 自清理
```

PARALLEL 模式下，时序变成（无 wait）：

```
每个 split: red.add（到哪算哪）      → release_add()
split S-1 (终结): wait_eq(S-1) → fold → 跑 epilogue → 写 D → release_store(0)
```

关键点：**两种模式都用同一个 `wait_eq(S-1)` 让终结 split 等所有兄弟完成**。区别只在前 S−1 个 split 是「排队写（确定）」还是「抢着写（不确定）」。

#### 4.2.3 源码精读

**主机侧：分配信号量与工作区。** `_split_k_buffers` 为每个输出 tile 分配一个 Int32 完成标志和一个 f32 工作区：

[quack/gemm.py:292-327](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L292-L327) —— 注意 `SERIAL` 分支用 `torch.empty`（split 0 会就地初始化），而 `PARALLEL` 分支用 `torch.zeros`（host 预清零，每个 split 直接 red.add）。tile 数按 cluster 向上取整，因为边界 cluster 可能含「整块越界但仍跑协议」的 CTA。

这里有个极易踩坑的几何量：**per-CTA 的 M tile 在 SM100 的 2-CTA MMA 下要折半**（`gemm.py:306-307`）。工作区区域步长是 `cta_tile_m * tile_N`，必须和内核逐字对齐，否则读写两侧不一致。这正是 `u4-l2` 讲过的 `cta_tile_shape_m` 镜像规则。

**设备侧：非终结 split 提交部分和。** `split_k_partial_commit` 是核心：

[quack/gemm_base.py:517-571](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L517-L571) —— 读 `if split_k_mode == SERIAL` 的两个分支：`wait_eq(split_idx, skip_zero=True)` 是旋转门等待；`split_idx == 0` 时用 `"store"` 初始化，否则 `"red_add"`；末尾 `release_store(split_idx+1)`。PARALLEL 分支则直接 `"red_add"` + `release_add()`。

实际读写工作区条带的是 `_frag_stripe_op`，三种操作（`store`/`red_add`/`load_add`）共享完全相同的寻址，这是「写侧与读侧可证明一致」的关键：

[quack/gemm_base.py:573-619](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L573-L619) —— 注意 `red_add` 用 `cute.arch.red(..., op="add", sem="relaxed", scope="gpu")`，这是一条单向 L2 归约加，不回读。片段可被 4 整除时走向量化 `v4`，否则退化为标量线程步进。

**设备侧：终结协议。** `epilogue_split_k` 是把上面这一切「包」在 epilogue 闭包外面的协调器：

[quack/gemm_base.py:643-757](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L643-L757) —— 读三处：
- 第 682 行的 `const_expr(split_k > 1 and split_k_mode != SEPARATE)` 把整个信号量/工作区逻辑用编译期判断包起来——`split_k==1` 或 SEPARATE 时这段代码不进 cubin。
- 第 715-721 行的 `load_acc_and_fold`：终结 split 加载自己的累加器片段后，**额外把工作区条带加进来**（`_frag_stripe_op("load_add")`），这才是它的「真实累加器」。
- 第 728-734 行：`split_idx < split_k-1` 走非终结提交；否则（终结 split）第 742 行 `wait_eq(self.split_k - 1)` 等齐所有兄弟，再跑 epilogue，最后 `release_store(0)` 自清理并 `producer_tail()` 排空自己的 TMA 存储。

**同步原语**：`release_store` 与 `release_add` 的语义差异定义在 `sync/barrier.py`：

[quack/sync/barrier.py:11-17](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/sync/barrier.py#L11-L17) —— 一句话点透：`release_store` 是「绝对值发布，调用方须是该点位的唯一发布者」（旋转门内，确定性）；`release_add` 是「自增发布，可交换，到达者任意顺序发布」（到达计数器，非确定）。

> 顺带印证一个常被忽视的事实：浮点加法不满足结合律，所以 `red.add` 的到达顺序变了，最终和就变了——这就是 PARALLEL 不确定的**数学根源**，而非工程瑕疵。

#### 4.2.4 代码实践

**实践目标**：用源码阅读对比 SERIAL 的「旋转门」与 PARALLEL 的「到达计数器」，并理解确定性差异的来源。

**操作步骤**：

1. 打开 `tests/test_gemm_split_k.py`，读 `test_gemm_split_k`（第 36-58 行）与 `test_gemm_split_k_serial_staged_f32_bitwise_equal`（第 265-284 行）。
2. 注意第 50-51 行：`if split_k_mode == SplitKMode.PARALLEL: return`——测试对 PARALLEL **跳过**逐位确定性检查。
3. 注意第 57-58 行：对 SERIAL / SEPARATE，用 `torch.equal`（而非近似 `allclose`）连续比较 3 次独立运行的结果。

**需要观察的现象**：测试代码用 `torch.equal` 做的是**逐位**比较。它对 PARALLEL 直接 `return`，说明作者明确承认 PARALLEL 每次运行的位级结果可能不同。

**预期结果**：
- SERIAL / SEPARATE：3 次运行 `torch.equal` 必须全为 True（逐位可复现）。
- PARALLEL：不检查逐位等价，只检查数值正确性（`_assert_close_double_baseline`）。

（这两条用例都需要 SM90/100/120 的 GPU，运行结果待本地验证。）

**源码阅读型实践**：在 `split_k_partial_commit`（`gemm_base.py:517`）里数一下 SERIAL 路径用到的 `Semaphore` 方法调用序列，对照 PARALLEL 路径，填下表：

| | 等待 | 写工作区 | 发布 |
|---|---|---|---|
| SERIAL | `wait_eq(split_idx)` | split0 `store` / 其余 `red_add` | `release_store(split_idx+1)` |
| PARALLEL | （无） | `red_add`（host 预清零） | `release_add()` |

#### 4.2.5 小练习与答案

**练习 1**：为什么 SERIAL 的工作区用 `torch.empty`，而 PARALLEL 用 `torch.zeros`？

> **答案**：SERIAL 由 split 0 用普通 `store` 就地初始化区域（`gemm_base.py:559-561`），所以 host 无需清零；后续 split 在旋转门保护下 `red.add`，区域在被加之前必然已被初始化。PARALLEL 不等待、split 到达即 `red.add`，若不预清零就会把脏数据加进去，故 host 必须 `torch.zeros`（`gemm.py:323-326`）。

**练习 2**：`split_k == 1` 时，SERIAL 的旋转门代码会出现在最终的 cubin 里吗？

> **答案**：不会。`split_k` 是挂在内核类上的 `const_expr` 静态值（`gemm_base.py:84,98`），`epilogue_split_k` 用 `const_expr(self.split_k > 1 and ...)` 包住整段协议（`gemm_base.py:682`）。`split_k==1` 时该前缀在 trace 期折叠为 False，整条 split-K 分支被编译期剔除，cubin 与非 split 基线逐位相同（这正是 `test_gemm_split_k_one_matches_baseline` 验证的）。

---

### 4.3 SEPARATE：staged 归约内核（split_k_reduce 与 epilogue 路由）

#### 4.3.1 概念说明

`SEPARATE`（staged）与前两种模式**根本不同**：合并不在 GEMM 内核里，而是由**第二个独立内核**完成。

思路很朴素：
1. GEMM 内核的每个 split 把自己的 f32 部分和写进**自己的专属工作区切片**（互不重叠，无需原子、无需信号量）。
2. 一个独立的归约内核 `SplitKReduce` 把这些切片**按固定升序**读回、求和，施加完整 epilogue，写出 D。

因为每个 split 写的是**不相交**的切片，所以 GEMM 阶段完全不需要 split-K 的同步协议——它退化成一个「把 D 重定向到 f32 工作区、剥光 epilogue」的普通 GEMM。所有复杂度都搬到了第二个内核。

**为什么 SEPARATE 要把 alpha/beta/bias 全部路由到归约内核？** 这是本节的核心问题，答案有三层：

1. **数学正确性**：epilogue 必须施加在「完整和」上恰好一次。若 GEMM 在自己的部分和上就乘了 `α`，那么 S 个 split 求和会得到 `Σ α·partial = α·Σpartial`——看似对，但 `β·C` 会变成 `S·β·C`（C 被加了 S 次），bias 同理被加 S 次。所以 GEMM 必须**只产出裸 f32 部分和**，epilogue 数学交给归约内核。
2. **职责单一**：归约内核读的是裸 f32 部分和，它必须**拥有全部 epilogue 数学**（`α·sum + β·C + bias`），才能保证施加恰好一次。
3. **add_to_output**（`D += 先前的 D`）：必须在 f32 求和完成**之后**加一次，也只能交给归约内核。

于是 SEPARATE 的 GEMM 编译签名被刻意「简化」成：f32 的 D、没有 C、`alpha=beta=1`、没有 bias、没有 add_to_output。这些项全部在归约内核里重新出现。

#### 4.3.2 核心流程

```
主机侧 run_gemm_plan (SEPARATE 分支):
  1. ws = _staged_split_k_workspace(D, split_k)   # (L*split_k, M, N_pad) f32
  2. D_gemm, C_gemm = ws, None                     # GEMM 输出重定向到 ws，剥光 epilogue
  3. launch_gemm(...)                              # 每个 split 写自己的 ws 切片
  4. _reduce_staged_split_k(ws, D, C, ...)         # 归约内核：按序求和 + 完整 epilogue

设备侧 SplitKReduce.kernel (对每个输出 tile):
  for s in 0..split_k-1: 加载 frags[s]            # 读所有 split 的部分和
  acc = frags[0].load()
  for s in 1..split_k-1: acc += frags[s].load()   # 固定升序求和 → 确定性
  apply_linear_epilogue(acc, C, alpha, beta, rowvec, colvec)   # 完整 epilogue 恰好一次
  (可选) acc += 先前的 D                            # add_to_output
  store(acc.to(out_dtype), D)
```

#### 4.3.3 源码精读

**主机侧工作区分配。** `_staged_split_k_workspace`：

[quack/gemm.py:270-289](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L270-L289) —— 工作区的 batch 维是 `num_l * split_k`，编码组合索引 `(l * split_k + s)`；连续维填充到 16 字节（4 个 f32）以满足 TMA 对齐，最后再切回真实 N。majorness 与 D 一致（n-major / m-major 两条路）。

**主机侧编译签名重定向。** 这是「把所有 epilogue 操作数路由到归约内核」的代码所在，`_build_gemm_plan` 里：

[quack/gemm.py:898-910](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L898-L910) —— `staged_split_k` 为真时：`d_dtype = Float32`、`c_dtype/c_major = None`、`alpha_mode=beta_mode=sr_seed_mode=0`（即「缺省」）、`rowvec_gemm/colvec_gemm = None`、`gemm_add_to_output = False`。于是**编译出的 GEMM 看到的是一个无任何 epilogue 附加项的纯 f32-D GEMM**。这些被清零的操作数随后在 `_reduce_staged_split_k` 里原样传给归约内核。

**主机侧运行期路由。** `run_gemm_plan`：

[quack/gemm.py:604-668](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L604-L668) —— 第 614-617 行：SEPARATE 时 `D_gemm, C_gemm = split_k_workspace, None`；第 657-668 行：GEMM 跑完后立即调 `_reduce_staged_split_k`，把真正的 D、C、alpha、beta、bias、add_to_output 全部喂给归约内核。

`_reduce_staged_split_k` 还处理一个细节：m-major 输出时把 m/n 视图转置（并**交换** rowvec/colvec 的角色），保证归约内核总是看到「连续维在最后」的布局（`gemm.py:330-357`）。

**独立归约内核。** `SplitKReduce` 是一个标准的 CuTe-DSL 归约内核（结构上类似 `rms_final_reduce`，见 `u2-l5`）：

[quack/split_k_reduce.py:35-75](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/split_k_reduce.py#L35-L75) —— 类配置：128 线程、每行 16 线程，`tiler_mn = (8, 16*vec_elems)`，`vec_elems ∈ {1,2,4}`。

设备内核的核心两段——**按固定顺序求和**与**施加共享 epilogue**：

[quack/split_k_reduce.py:206-231](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/split_k_reduce.py#L206-L231) —— 第 206-208 行用 `range_constexpr` **编译期展开**的固定升序循环求和（确定性来源）；第 225 行调 `apply_linear_epilogue`——这是从 `gemm_default_epi` 导入的**同一份**线性 epilogue 数学，所以归约内核和 SERIAL/PARALLEL 的终结 split 施加的 epilogue **逐位一致**。第 227-228 行的 `add_to_output` 在 f32 求和后才加，恰好一次。

> 这份「共享 epilogue」的设计直接支撑了测试 `test_gemm_split_k_serial_staged_f32_bitwise_equal`：SERIAL 与 SEPARATE 在 f32 输出下必须**逐位相等**（`tests/test_gemm_split_k.py:265-284`），因为两者都按固定升序求和 f32 部分和、跑同一份 epilogue 数学。

**编译与公共入口。** 归约内核同样走 `@jit_cache` 缓存（`u2-l6`、`u8-l2`）：

[quack/split_k_reduce.py:236-294](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/split_k_reduce.py#L236-L294) —— 用 `sym_int` 把 M/N/L 设为符号维，产物对任意形状复用；`alpha_mode`/`beta_mode`（0=缺省/1=立即数/2=设备指针）编码进编译键。公共入口 `split_k_reduce` 经 `@cute_op("quack::split_k_reduce_out")` 注册成自定义算子（`split_k_reduce.py:297-388`）。

#### 4.3.4 代码实践

**实践目标**：亲手验证 SEPARATE 模式把 alpha/beta/bias 路由到归约内核这一设计，并用测试确认其确定性。

**操作步骤**：

1. 打开 `tests/test_gemm_split_k.py`，读 `test_gemm_split_k_bias_alpha`（第 110-123 行）与 `test_gemm_split_k_full_linear_epilogue`（第 379-419 行）。
2. 注意第 118 行的参考实现：`out_ref = alpha * (A.float() @ B.float()) + bias`——`alpha` 只乘了一次、`bias` 只加了一次，不论 `split_k_mode` 是哪种。
3. 注意第 415 行的 `quack_gemm(..., alpha=alpha, beta=beta, rowvec_bias=rowvec, colvec_bias=colvec, split_k=4, split_k_mode=split_k_mode)`——三模式都吃同一组 epilogue 参数。
4. 回到 `gemm.py:898-910`，对照确认：SEPARATE 时这些参数在 GEMM 编译签名里被清零，转而在 `_reduce_staged_split_k` 里传给归约内核。

**需要观察的现象**：无论选哪个 `split_k_mode`，`alpha`/`bias` 的参考实现完全相同（只施加一次）。这说明三模式对外暴露的语义一致——差异只在内部由谁施加 epilogue。

**预期结果**：
- 三种模式的 `out` 都通过 `_assert_close_double_baseline`（数值正确）。
- SERIAL / SEPARATE 还通过 `torch.equal` 的逐位确定性检查（第 417-419 行）；PARALLEL 跳过。
- 这等价于证明：SEPARATE 把 epilogue 路由到归约内核后，结果与 SERIAL（在 GEMM 内终结）**数值等价、f32 下逐位等价**。

（运行需 SM90/100/120 GPU；结果待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：如果 SEPARATE 不把 `beta`/`C` 路由到归约内核，而是在 GEMM 里就让每个 split 算 `partial + beta·C`，会发生什么？

> **答案**：每个 split 都会加一次 `beta·C`，S 个 split 求和后 `beta·C` 被加了 S 次，结果变成 `Σpartial + S·beta·C`，与正确语义 `Σpartial + beta·C` 不符。同理 `rowvec`/`colvec` bias 也会被加 S 次。所以 epilogue 的每一项都必须只在完整和上施加一次——这正是把它们全部路由到归约内核的原因。

**练习 2**：SEPARATE 的归约内核为什么能保证逐位确定性？

> **答案**：因为求和用 `range_constexpr` 编译期展开的**固定升序**循环（`split_k_reduce.py:206-208`），每个 tile 的 split 顺序永远是 0,1,…,S−1。没有原子、没有到达顺序的不确定性。这与 SERIAL 的旋转门「固定顺序」是同一类保证，所以两者 f32 输出逐位相等。

**练习 3**：SEPARATE 比 SERIAL 多了什么开销？什么时候仍值得用？

> **答案**：多了一次独立的内核启动（归约内核），以及更大的工作区（`L*split_k` 个 tile 而非 per-tile 一份）。但它的优势是 GEMM 阶段完全不携带 split-K 同步协议，对 autotune 与各 SM 内核的侵入更小，且归约内核的确定性与并行度可控。当 GEMM 内核内终结的同步开销（信号量、red.add 串行化）反而拖累时，或架构路径不便支持内核内终结时，SEPARATE 是干净的退路。

---

## 5. 综合实践

**任务**：把本讲三条主线串起来——通读一次完整的 SEPARATE split-K 调用，画出从用户 API 到设备写出 D 的全链路。

**步骤**：

1. 从用户侧入口出发：阅读 `quack/gemm_interface.py` 中公共 `gemm` 的 `split_k` / `split_k_mode` 参数（约第 1013-1014 行的 docstring 注释：`SERIAL/SEPARATE deterministic, PARALLEL fastest but arrival-order`）。
2. 跟到 `quack/gemm.py` 的 `_build_gemm_plan`（第 898-910 行）：确认 SEPARATE 把 GEMM 编译签名简化为「f32-D、无 epilogue 附加项」。
3. 跟到 `run_gemm_plan`（第 613-668 行）：确认 SEPARATE 先分配工作区、把 GEMM 输出重定向、跑 GEMM，再调 `_reduce_staged_split_k`。
4. 跟到 `quack/split_k_reduce.py` 的 `kernel`（第 132-233 行）：确认归约内核按固定升序求和、施加共享 `apply_linear_epilogue`。
5. 画一张数据流图，标注三个关键「恰好一次」：① GEMM 写工作区（每个 split 写自己的切片）；② 求和（固定升序）；③ epilogue（在完整和上跑一次）。

**验收**：你的图里应当能清楚回答——「alpha 在哪里乘？」「C 在哪里加？」「为什么不会加 S 次？」如果都能指向归约内核的 `apply_linear_epilogue` 这一处，你就掌握了 SEPARATE 的精髓。

**延伸（可选）**：把同一张图改画成 SERIAL 版本，对比「合并」发生在哪里（GEMM 内核的终结 split vs 归约内核），并标出 `release_store` 旋转门的位置。

## 6. 本讲小结

- **Split-K 的动机**：当 M/N 小、K 大导致占用率不足时，沿 K 维切分以提升 tile 数与并行度，代价是多一次部分和归约。
- **统一不变量**：三种模式都以 f32 提交裸部分和；完整线性 epilogue 只在拥有「已完成 f32 和」的实体上运行**恰好一次**（`gemm_base.py:84-98`）。
- **SERIAL**：旋转门 `release_store(split_idx+1)` 把提交顺序固定为 0…S−1，逐位确定；split 0 用 store 初始化、其余 red.add；终结 split 折叠工作区后跑 epilogue。
- **PARALLEL**：到达计数器 `release_add()`，host 预清零、split 到达即 red.add、不等待；最快但每次运行的位级结果可能不同（浮点加法不满足结合律的数学后果）。
- **SEPARATE**：每个 split 写自己的工作区切片（无原子），独立归约内核 `split_k_reduce` 按固定升序求和并施加共享 `apply_linear_epilogue`；为保 epilogue 恰好一次，`alpha`/`beta`/`C`/`bias`/`add_to_output` **全部路由到归约内核**，GEMM 编译签名被简化为无附加项的 f32-D。
- **工程要点**：`split_k` 是 `const_expr`，`split_k==1` 编译期折叠成与非 split 基线逐位相同的 cubin；工作区 tile 计数须按 cluster 取整、per-CTA M tile 在 SM100 2-CTA 下折半（`cta_tile_shape_m`）；归约内核与终结 split 共享同一份 epilogue 数学，故 SERIAL 与 SEPARATE 的 f32 输出逐位相等。

## 7. 下一步学习建议

- **`u8-l1`（自动调优）**：`split_k=None` 让 autotuner 通过 `_expand_split_k_configs` 自动决定切分因子，建议结合该讲理解「占用率不足」的判定与剪枝。
- **`u8-l2`（.o JIT 缓存）**：归约内核同样走 `@jit_cache`，`SplitKMode` 作为 `IntEnum` 进缓存键；理解两级缓存与源码指纹如何覆盖 split-K 变体。
- **`u3-l5`（异步流水线与同步原语）**：本讲的 `Semaphore`（`wait_eq`/`release_store`/`release_add`）在那里有更底层的展开，包括 split-K 的 SERIAL/PARALLEL 两种合并模式如何对应 turnstile 与可交换发布。
- **`u6-l3`（默认线性 epilogue）**：本讲反复出现的 `apply_linear_epilogue` 是那一讲的主角，理解它能帮你彻底看清「epilogue 恰好一次」的数学与实现。
- **继续阅读源码**：`tests/test_gemm_split_k.py` 是极佳的自学材料，里面每条用例都编码了一种 split-K 的边界条件（ragged N、cluster 取整、split 大于 K-tile 数、m-major 输出、mixed C majorness 等），逐条读懂胜过任何文档。
