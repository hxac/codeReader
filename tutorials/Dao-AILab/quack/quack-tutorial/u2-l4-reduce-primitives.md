# 归约原语：warp/row/online

## 1. 本讲目标

本讲深入 `quack/reduce.py`，拆解三个被所有归约内核（softmax、rmsnorm、cross_entropy）反复调用的核心原语：

1. **`warp_reduce`** —— 单个 warp 内的对齐子组归约，是所有归约的「第一级」。
2. **`row_reduce`** —— 把一行数据归约成单个标量，编排「warp 内 + 跨 warp/cluster」两阶段。
3. **`online_softmax_reduce`** —— 把 max 与 sum 耦合进一次归约的 online softmax 合并。

学完后你应当能够：

- 说清 `warp_reduce` 在什么条件下走硬件 `redux.sync`、什么时候退化为 shuffle 蝶形。
- 读懂 `row_reduce` 的两阶段结构，并指出 `cluster_n > 1` 时跨 CTA 归约用的是哪几个原语。
- 解释 online softmax 为何要把 `(max, sum)` 打包成一个 `Int64`、只用一次 mbarrier 往返完成合并。

## 2. 前置知识

### 2.1 为什么要分层归约

GPU 上一「行」要归约的数据通常有几千个元素，而一个 warp 只有 32 个 lane，一个 CTA 最多几十个 warp，一个 cluster 最多 8 个 CTA。所以一次行归约必然分多级完成，每一级把上一层若干线程的部分和（partial）合并：

\[ \text{lane 内} \to \text{warp 内} \to \text{block(CTA) 内} \to \text{cluster 内} \]

每一级的「合并」都遵循同一套思路：先让每个参与者算出自己的 partial，再用同步原语把所有 partial 收敛到一个全 replicate 的结果。

### 2.2 三种硬件归约手段

- **`redux.sync`（PTX `redux.sync`）**：一条指令完成整个 warp 的归约，硬件广播结果到所有 lane。最快，但只覆盖部分 dtype/op/架构组合。
- **shuffle 蝶形（`shfl.sync.bfly`）**：软件实现的归约。每轮把相邻 lane 的值交换并合并，经 \(\log_2(\text{lanes})\) 轮后所有 lane 持有完整结果。通用，但每轮有一半 lane 闲置。
- **共享内存 + 屏障**：跨 warp 或跨 CTA 时用。每个参与者把 partial 写进 smem，屏障同步后某个代表线程读回所有 partial 再归约。

### 2.3 cluster 与 mbarrier 事务屏障

`cluster_n > 1` 时，一个逻辑行被拆给 cluster 里的多个 CTA。各 CTA 的 partial 要彼此交换。QuACK 用 **事务屏障（transaction barrier, mbarrier）** 协调：

- `mbarrier_arrive_and_expect_tx(mbar, tx_bytes)`：把本 CTA 的屏障「上膛」，告知「请等待 `tx_bytes` 字节到达」。
- `st.async.shared::cluster.mbarrier::complete_tx::bytes`（封装在 `store_shared_remote` 里）：异步地把数据写到 peer CTA 的 smem，**同时**给目标 CTA 的屏障记一笔到达字节数。
- `mbarrier_wait(mbar, phase)`：等到所有 peer 都把承诺的字节写完，才放行。

这套机制让「跨 CTA 通信」和「信用计数」合并进同一条异步存储，是 cluster 归约能高效完成的关键。

### 2.4 承接前讲

[u2-l2](u2-l2-softmax-fwd.md) 已经讲过 Softmax 内核的整体数据流 `gmem→smem→rmem→归约→gmem`，以及它在线 / 非在线两种归约路径的选择。本讲不再重复内核框架，而是**钻进归约调用本身**，看 `row_reduce(...)` 和 `online_softmax_reduce(...)` 这两个名字内部到底做了什么。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [quack/reduce.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py) | 本讲主角：`warp_reduce`、`block_reduce`、`cluster_reduce`、`row_reduce`、`online_softmax_reduce` 全部在此 |
| [quack/utils.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/utils.py) | `store_shared_remote`（跨 CTA 异步写）、`f32x2_to_i64`/`i64_to_f32x2`（max+sum 打包） |
| [quack/reduction_base.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduction_base.py) | reduction buffer 布局、mbarrier 分配、`_initialize_cluster` 的 `cluster_arrive` |
| [quack/softmax.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py) | 三个原语的真实调用点，展示 `hook_fn=cute.arch.cluster_wait` 的用法 |

---

## 4. 核心概念与源码讲解

### 4.1 warp_reduce：单步 redux.sync 与蝶形兜底

#### 4.1.1 概念说明

`warp_reduce` 是整个归约体系的最底层：在一个 warp 内，把「对齐的、连续的 `threads_in_group` 个 lane」的值合并，结果广播回组内每个 lane。

它的设计哲学是**能用硬件就用硬件**：

- `Int32` 的 ADD/MAX/MIN 在所有架构上都有 `redux.sync`；
- `Float32` 的 FMAX/FMIN **只在 SM100 系列**（数据中心 Blackwell）有 `redux.sync`，SM120（消费级 Blackwell）**没有** fp32 redux，SM90（Hopper）也没有；
- 其余情况（如 `MUL`、或上面不覆盖的 dtype/架构）退化为 `cute.arch.warp_reduction` 的 shuffle 蝶形。

这一切选择都在**编译期**完成（用 `const_expr`），所以不会产生运行期分支开销——未命中的那条路径根本不会被编进 cubin。

#### 4.1.2 核心流程

```
输入: val（每 lane 一个值）、op、threads_in_group（≤32）
1. 取当前架构 arch、推导 val_dtype
2. 编译期判定 op 与 dtype，尝试匹配到一个 redux "kind"
     Int32           → add / max / min
     Float32 + SM100 → fmax / fmin
3. 若 kind 命中：
     a. 按 threads_in_group 算出本 lane 所属子组的成员掩码 mask
     b. 把 kind 映射到 prims.ReductionKind
     c. 调 prims.redux_sync(val, kind, mask)  ← 一条硬件指令
4. 若 kind 未命中（不支持）：
     a. （可选）对 val 取绝对值
     b. 调 cute.arch.warp_reduction（shuffle 蝶形）兜底
```

当 `threads_in_group == 32`（整 warp）时 mask 直接是 `0xFFFFFFFF`；当 `threads_in_group < 32` 时，需要一个**子组掩码**——只让与本 lane 同组的 lane 参与归约，其它 lane 被屏蔽。

子组划分按「对齐连续」规则：lane 0..g-1 是一组，lane g..2g-1 是一组……因此本 lane 的组基地址是：

\[ \text{base\_lane} = \text{lane\_idx}\ \&\ (32 - g) \]

成员掩码就是 `(2^g - 1) << base_lane`。

#### 4.1.3 源码精读

整函数定义在 [quack/reduce.py:21-89](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L21-L89)。

第一步——编译期判定 dtype 与 op，归类到 `kind`：

[quack/reduce.py:47-58](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L47-L58) —— `Int32` 走 add/max/min；`Float32` 且架构属于 SM100 家族才走 fmax/fmin。注意 `arch.is_family_of(Arch.sm_100f)` 这个判断把 SM120 排除在外，正对应 docstring 所说的「redux fp32 does not exist on SM120」。

第二步——子组掩码与硬件指令：

[quack/reduce.py:59-86](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L59-L86) —— `kind` 命中分支。当 `threads_in_group < WARP_SIZE` 时按上面公式算 `mask`（L60-L68），最后 `return prims.redux_sync(val, redux_kind, mask, **kwargs)` 发出单条 `redux.sync`。`kwargs` 只在 `abs`/`nan` 置位时才传，因为 DSL 的 `dsl_user_op` 包装不接受显式 `None` 关键字（L71-L75 的注释解释了这一点）。

第三步——蝶形兜底：

[quack/reduce.py:87-89](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L87-L89) —— `kind` 未命中分支：先对 `abs` 做显式 `absf`（蝶形没有 REDUX.MAXABS），再 `cute.arch.warp_reduction(val, op, ...)` 走 shuffle。

> **关于 `abs` / `nan` 限定符**：`abs` 在 redux 路径折叠进 `REDUX.MAXABS`，在兜底路径变成显式 `absf`；`nan` 让 NaN 污染结果（CUTLASS 的 amax 语义），但**只在 redux 路径可靠**——蝶形用的 `max.f32` 会丢弃 NaN。docstring（L36-L41）明确警告：只在确保是 SM100 家族时才依赖 `nan` 语义。

#### 4.1.4 代码实践

**目标**：在不运行代码的前提下，准确预测 `warp_reduce` 在不同场景下走哪条路径。

**步骤**：

1. 打开 [quack/reduce.py:42-89](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L42-L89)。
2. 对下表每个场景，手动跟踪 `val_dtype`、`is_max/is_min` 判定，写出 `kind` 的值，并判断最终是 `redux.sync` 还是 `warp_reduction`。

| 场景 | dtype | op | 架构 | kind | 走 redux? |
|------|-------|----|------|------|-----------|
| A | Int32 | operator.add | SM90 | ? | ? |
| B | Float32 | cute.arch.fmax | SM100 | ? | ? |
| C | Float32 | cute.arch.fmax | SM120 | ? | ? |
| D | Float32 | operator.mul | SM100 | ? | ? |
| E | Int32 | max | SM120 | ? | ? |

**预期结果**（待本地核对）：

- A → `kind="add"`，走 redux；
- B → `kind="fmax"`，走 redux（SM100 家族有 fp32 redux）；
- C → `kind=None`（SM120 不属于 sm_100f 家族），走蝶形兜底；
- D → `kind=None`（MUL 没有对应 kind），走蝶形兜底；
- E → `kind="max"`，走 redux（Int32 全架构支持）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `warp_reduce` 的所有分支判断都用 `const_expr(...)` 包裹，而不是普通 `if`？

**答案**：`const_expr` 把判断标记为编译期分支，DSL 只编入命中分支，不命中分支的代码（例如 `prims.redux_sync` 调用或 `warp_reduction` 调用）不会出现在最终 cubin 里。若用普通 `if`，两条路径都会被编译，运行期才选择，既增大 cubin 又引入分支开销。

**练习 2**：当 `threads_in_group = 8` 时，lane 19 所在子组的 mask 是多少？

**答案**：`base_lane = 19 & (32 - 8) = 19 & 24 = 16`；`group_mask = (1<<8)-1 = 0xFF`；`mask = 0xFF << 16 = 0x00FF0000`，即覆盖 lane 16..23。

---

### 4.2 row_reduce：warp 内归约 + 跨 warp/cluster 两阶段

#### 4.2.1 概念说明

`warp_reduce` 只能在一个 warp 内归约。但一行的数据往往跨多个 warp、甚至跨多个 CTA。`row_reduce` 把「一行 → 一个标量」的完整归约编排成两阶段：

1. **第一阶段（warp 内）**：每个 lane 先用 `TensorSSA.reduce` 把自己持有的若干元素压成 lane-local partial，再用 `warp_reduce` 在 `min(threads_per_row, 32)` 个 lane 之间合并，得到「每 warp 一个 partial」。
2. **第二阶段（跨 warp / 跨 cluster）**：仅当 `warps_per_row > 1` 或 `cluster_n > 1` 时执行。`warps_per_row > 1` 走 `block_reduce`（同一 CTA 内跨 warp），`cluster_n > 1` 走 `cluster_reduce`（跨 CTA）。这两者由 `block_or_cluster_reduce` 统一分发。

`row_reduce` 还支持把**多个值打包成一个 tuple**一起归约（例如 online 场景的 max+sum），它们共用同一次 mbarrier 往返；也支持一个可选的 `hook_fn`，让调用者在两个阶段之间插入一个同步动作。

#### 4.2.2 核心流程

```
输入: x（TensorSSA 或标量或 tuple）、op、threads_per_row、
      reduction_buffer（可选）、mbar_ptr（可选）、hook_fn（可选）

第一阶段 —— warp 内：
  对 x 中每个值 xi：
    a. 若 xi 是张量: val = xi.reduce(op, init_val)   # lane 内压成标量
       否则:          val = xi                        # 已是标量
    b. val = warp_reduce(val, warp_op, threads_in_group=min(threads_per_row, 32))
  # 现在 每个 warp 持有本 warp 的一个 partial

  if hook_fn is not None: hook_fn()    # ← 调用者插入 cluster_wait 等同步

第二阶段 —— 跨 warp / 跨 cluster（仅当需要时）：
  if reduction_buffer is not None and (warps_per_row>1 or cluster_n>1):
    val = block_or_cluster_reduce(val, op, buffer, mbar_ptr, ...)
      ├─ mbar_ptr is None  → block_reduce   (CTA 内跨 warp)
      └─ mbar_ptr 非空      → cluster_reduce  (跨 CTA)
返回: 每个值的全局归约结果
```

**关键点**：第二阶段的「是否需要」是编译期决定的结构性判断（`warps_per_row > 1 or cluster_n > 1`）。如果一行只占一个 warp 且没有 cluster，整个第二阶段都不会被编译进 cubin，`row_reduce` 退化成一次 `warp_reduce`。

#### 4.2.3 源码精读

整函数见 [quack/reduce.py:201-267](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L201-L267)。

**第一阶段——lane 内压标量 + warp 内合并**：

[quack/reduce.py:222-240](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L222-L240) —— `xi.reduce(op, init_val, reduction_profile=0)` 是 CuTe 张量的内建 lane-local 归约；随后把 DSL 的 `cute.ReductionOp` 映射到 Python 算子（ADD→`operator.add`、MAX→`cute.arch.fmax`/`max`…），交给 `warp_reduce`。注意 Float32 的 MAX/MIN 映射到 `cute.arch.fmax/fmin`，正是为了让 `warp_reduce` 能命中 SM100 的 redux 路径。

**插入 hook 的位置**：

[quack/reduce.py:243-244](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L243-L244) —— `hook_fn` 在 warp 归约之后、跨 CTA 归约之前被调用。Softmax 传入的是 `cute.arch.cluster_wait`（见 [quack/softmax.py:151](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L151)）：它与 `_initialize_cluster` 里的 `cluster_arrive_relaxed()`（[quack/reduction_base.py:94](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduction_base.py#L94)）配对，把「等所有 CTA 初始化完 mbarrier」这件事**重叠到 warp 归约的计算时间上**，而不是在内核开头空等。这就是 hook 设计的价值。

**第二阶段——跨 warp / 跨 cluster 分发**：

[quack/reduce.py:245-266](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L245-L266) —— 读出 `(warps_per_row, cluster_n)`，断言「cluster_n>1 必须给 mbar_ptr」，然后调 `block_or_cluster_reduce`。

分发逻辑在 [quack/reduce.py:164-198](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L164-L198)（`block_or_cluster_reduce`）：`mbar_ptr is None` 走 `block_reduce`，否则走 `cluster_reduce`。

**`block_reduce`（CTA 内跨 warp）**：[quack/reduce.py:92-110](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L92-L110)。每个 warp 由 lane 0 把自己的 partial 写进 reduction_buffer，`barrier()` 后让一个 warp 读回所有 partial，再 `warp_reduce` 合并。

**`cluster_reduce`（跨 CTA）**：[quack/reduce.py:113-161](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L113-L161) —— 这是本讲跨 CTA 归约的核心，下一节「代码实践」会逐行跟踪。

#### 4.2.4 代码实践（本讲主实践）

**目标**：跟踪 `cluster_n > 1` 时 `row_reduce → cluster_reduce` 如何用 mbarrier + `cluster_wait` 完成跨 CTA 归约，并画出两阶段数据流。

**操作步骤**：

1. 打开 [quack/reduce.py:113-161](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L113-L161)（`cluster_reduce`）。
2. 逐段阅读并标注每一步属于「准备」「通信」「等待」「合并」中的哪一类：
   - L134-L137：取 `cta_rank_in_cluster`、`lane_idx`、`warp_idx`，由 warp_idx 算出 `(row_idx, col_idx)`，并从 buffer 形状读出 `(rows_per_block, (warps_per_row, cluster_n))`。
   - L138-L142：**准备**。warp 0 选出一个线程，算出 `tx_bytes` 并对本 CTA 的 mbarrier 调 `mbarrier_arrive_and_expect_tx`「上膛」。
   - L143-L150：**通信**。每个 `lane_idx < cluster_n` 的 lane 调 `store_shared_remote`，把本 CTA 的 partial（带 `cta_rank_in_cluster` 标签）异步写到 peer CTA `lane_idx` 的 buffer，**同时给 peer 的 mbarrier 记一笔到达字节**。
   - L151：**等待**。`mbarrier_wait` 阻塞到所有 peer 都写够承诺字节。
   - L152-L160：**合并**。每个 lane 读 `num_iter` 个 buffer 槽位累加，再 `warp_reduce` 收敛。
3. 推导 `tx_bytes` 的含义：[quack/reduce.py:140-142](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L140-L142) 里 `tx_bytes = num_warps * cluster_n * width/8`。注意每个 peer CTA 都会把 `num_warps` 个 partial（每个 warp 一个）写进**本**CTA 的 buffer，共 `cluster_n` 个 peer，所以本 CTA 屏障要等的总字节正是 `num_warps * cluster_n * width/8`。
4. 看 `store_shared_remote` 如何把「写数据」与「记信用」合并进一条 PTX：[quack/utils.py:36-58](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/utils.py#L36-L58)，关键指令是 `st.async.shared::cluster.mbarrier::complete_tx::bytes`。

**需要观察 / 画出的数据流**（两阶段）：

```
阶段 1 —— warp 内（每个 CTA 各自做）:
  lane-local:  xi.reduce(op)            # 每 lane 一个 partial
  warp 内:     warp_reduce(...,32 lanes) # 每 warp 一个 partial
  hook:        cluster_wait()            # 等 cluster 里所有 CTA 初始化完成
                                          (与上面的 warp 归约重叠)

阶段 2 —— 跨 CTA（cluster_reduce）:
  本 CTA 的 partial ──STAS──> 写进每个 peer 的 buffer（带本 CTA rank 标签）
                              同时给每个 peer 的 mbarrier 记信用
  mbarrier_wait:           每个 CTA 等到所有 peer 都写够 num_warps*cluster_n 个元素
  合并:                    每个 CTA 读回「所有 peer 的所有 warp」的 partial，warp_reduce
  结果:                    每个 CTA 都持有完整归约结果（全 replicate）
```

**预期结果**：跨 CTA 归约本质是一个「通过共享内存做的全连接广播」——每个 CTA 把自己的 partial 发给所有 peer，屏障之后每个 CTA 的 buffer 里都收齐了全部 partial，于是各自做一次读+合并就得到相同结果。`cluster_wait` 之所以放在 hook 里，是为了把 cluster 同步等待藏在 warp 归约的计算背后。

> 若本地有 SM90+ 的 GPU，可进一步实证：把 `cluster_n` 设为 2 与设为 1 分别编译（参考 [u2-l1](u2-l1-reduction-base.md) 的 `_set_cluster_n`），用 `cute.printf` 在 `cluster_reduce` 前后打印 `cta_rank_in_cluster` 与合并结果，确认每个 CTA 得到一致值。否则标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `row_reduce` 在 [quack/reduce.py:231-232](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L231-L232) 把 Float32 的 MAX 映射成 `cute.arch.fmax` 而非 Python 内置 `max`？

**答案**：`cute.arch.fmax` 在 `warp_reduce` 里会被识别成 `is_max`（L55），从而在 SM100 上命中 `redux.sync` 的 fmax 路径；而 Python 内置 `max` 虽也被 `is_max` 捕获，但语义上 `fmax` 明确是「非 NaN 传播」的硬件 fmax，与 redux 指令语义一致，避免歧义。

**练习 2**：若一行的数据完全落在一个 warp 内、且 `cluster_n == 1`，第二阶段会执行吗？

**答案**：不会。[quack/reduce.py:251](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L251) 的条件 `warps_per_row > 1 or cluster_n > 1` 不成立，整个第二阶段在编译期被剔除，`row_reduce` 退化为单次 `warp_reduce`。

---

### 4.3 online_softmax_reduce：max/sum 耦合归约

#### 4.3.1 概念说明

Softmax 的数值稳定写法是先求行最大值 `m`，再算 \(\exp(x-m)\) 的和 \(s\)，最后 \(y_i = \exp(x_i - m)/s\)。朴素做法要两次独立归约：一次 MAX 拿 `m`，一次 ADD 拿 `s`（这正是 [u2-l2](u2-l2-softmax-fwd.md) 非在线路径的写法）。

**Online softmax** 把这两次归约合并成一次，秘诀在于一个**可合并的 (max, sum) 对**。给定两个局部对 \((m_a, s_a)\) 和 \((m_b, s_b)\)，它们的合并结果是：

\[
m = \max(m_a,\ m_b), \qquad s = s_a \cdot e^{m_a - m} + s_b \cdot e^{m_b - m}
\]

这个公式满足结合律，所以可以把「求 max」和「求 sum」耦合在一次归约里逐步合并。好处是：跨 warp / 跨 CTA 时，**只需要一次 mbarrier 往返就能同时拿到全局 max 和全局 sum**，而不是非在线路径的两次往返、两个缓冲槽。

为了让 max 和 sum「同时」在一条事务里传输，QuACK 把它们打包成一个 `Int64`：高 32 位放 max，低 32 位放 sum。

#### 4.3.2 核心流程

```
输入: x（Float32 张量）、threads_per_row、reduction_buffer、mbar_ptr、hook_fn

第一阶段 —— warp 内的耦合归约:
  max_x   = warp_reduce(x.reduce(MAX),            fmax, 32 lanes)
  exp_x   = exp2((x - max_x)*log2e)              # 用本 warp 的 max 重缩放
  sum_exp = warp_reduction(exp_x.reduce(ADD),     add,   32 lanes)
  # 此刻每 warp 持有 (max_x, sum_exp) 一对，以及自己的 exp_x

  if hook_fn: hook_fn()

第二阶段 —— 跨 warp / 跨 cluster 合并 (max, sum) 对:
  if reduction_buffer 存在 and (warps_per_row>1 or cluster_n>1):
    assert buffer 是 Int64
    打包: packed = f32x2_to_i64(max_x, sum_exp)

    if mbar_ptr is None (CTA 内跨 warp):
       lane 0 写 packed 进 buffer → barrier → 一个 warp 读回所有对
       m_final = max 所有 max_x
       sum    = Σ sum_i * exp(max_i - m_final)      # 用合并公式逐对重缩放
       (可选) 用 exp(max_x - m_final) 重缩放本 warp 的 exp_x

    else (跨 CTA):
       同上，但用 store_shared_remote + mbarrier_wait 收集所有 CTA 的对

返回: (max_x, sum_exp, exp_x?)
```

注意两个细节：

1. `sum_exp` 的合并**不是简单相加**，而是每次都要按「新 max」重缩放（合并公式）；
2. 若 `return_exp_x=True`，返回的 `exp_x` 已被重缩放到全局 `max_x_final`，调用方拿去直接除以 `sum_exp` 就是最终 softmax 输出，无需重算。

#### 4.3.3 源码精读

整函数见 [quack/reduce.py:270-367](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L270-L367)。

**第一阶段——warp 内耦合归约**：

[quack/reduce.py:282-294](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L282-L294) —— 先 `warp_reduce` 出 `max_x`（用 `fmax` 以命中 redux），再用 `max_x` 计算 `exp_x`，最后对 `exp_x` 做加法归约得 `sum_exp_x`。`log2_e` 的出现是因为代码用 `exp2(... * log2e)` 而非 `exp`（硬件 `exp2` 更快）。

**CTA 内跨 warp 合并**（`mbar_ptr is None`）：

[quack/reduce.py:308-323](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L308-L323) —— lane 0 用 `f32x2_to_i64(max_x, sum_exp_x)` 把一对值打包写进 Int64 buffer（L309-L310），屏障后一个 warp 用 `i64_to_f32x2` 拆回（L314-L317），算出 `max_x_final`，再用合并公式重缩放每个 `sum_exp_x`（L318-L320）。打包/拆包函数见 [quack/utils.py:137-147](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/utils.py#L137-L147)。

**跨 CTA 合并**（`mbar_ptr` 非空）：

[quack/reduce.py:324-366](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L324-L366) —— 与 4.2 的 `cluster_reduce` 同构：warp 0 上膛 mbarrier（L326-L332），每个 `lane_idx < cluster_n` 用 `store_shared_remote` 把打包后的 Int64 发给 peer 并记信用（L333-L341），`mbarrier_wait` 等齐（L342），然后每个 lane 读 `num_iter` 个槽位、拆包、按合并公式累加（L343-L363）。因为打包成单个 Int64，**整个跨 CTA 合并只用一个 buffer、一次 mbarrier 往返**——这正是 docstring（[quack/reduce.py:281](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L281)）要求 buffer 是 `Int64`、且形状最后一维是单个槽的原因。

**调用方如何使用**：Softmax 在线路径把 `return_exp_x=True`（[quack/softmax.py:170](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L170)），拿到已重缩放的 `exp_x` 和全局 `denom`，直接 `y = exp_x * rcp(denom)`（[quack/softmax.py:173](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L173)）。

#### 4.3.4 代码实践

**目标**：用具体数字验证 online softmax 的合并公式，理解「为什么 sum 不能直接相加」。

**步骤**：

1. 设一个 warp A 的局部对 \((m_a, s_a) = (4,\ e^{2-4}+e^{4-4}) = (4,\ e^{-2}+1)\)，warp B 的局部对 \((m_b, s_b) = (3,\ e^{1-3}+e^{3-3}) = (3,\ e^{-2}+1)\)。
2. 按合并公式手算全局 \(m=\max(4,3)=4\)，\(s = s_a e^{m_a-m} + s_b e^{m_b-m} = s_a\cdot 1 + s_b\cdot e^{-1}\)。
3. 对照 [quack/reduce.py:359-362](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L359-L362) 的 `sum_exp_x += sum_exp_x_single_warp[i] * exp(max_x_single_warp[i] - max_x_final)`，确认代码用的正是这个公式。
4. 思考：若直接把 \(s_a + s_b\) 当作全局 sum 会错在哪？

**预期结果**：\(s_a, s_b\) 各自是用「自己的局部 max」归一化的指数和，量纲不一致，直接相加会系统性偏小。只有先用全局 max 重缩放（乘以 \(e^{m_{\text{local}}-m_{\text{global}}}\)）才能相加。这就是 max 和 sum 必须**耦合**传输的根本原因，也是 QuACK 用 `Int64` 把它们打包、只做一次 mbarrier 往返的动机。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `online_softmax_reduce` 的 reduction buffer 必须是 `Int64` 类型（[quack/reduce.py:303-305](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L303-L305) 断言）？

**答案**：因为要把 `(max_x, sum_exp)` 两个 Float32 打包进同一个存储单元，用一次事务屏障同时传输。两个 Float32 = 64 bit = 一个 Int64，所以 buffer 元素类型必须是 `Int64`；`f32x2_to_i64`/`i64_to_f32x2` 负责打包/拆包。

**练习 2**：相比非在线路径的两次 `row_reduce`（MAX + ADD），在线路径在跨 CTA 时节省了什么？

**答案**：节省了一次完整的 mbarrier 往返和一个缓冲槽。非在线路径对 max 和 sum 分别做一次 cluster 归约，各需一次「STAS → mbarrier_wait」往返（见 [quack/softmax.py:148-162](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L148-L162) 用了 `mbar_ptr+0` 和 `mbar_ptr+1` 两个屏障、两个槽）；在线路径把它们打包成一对，单次往返同时拿到全局 max 和 sum。

---

## 5. 综合实践

把三个模块串起来，完整跟踪一次 **cluster 模式下的在线 softmax 行归约**，画出从 lane 到 cluster 的完整数据流。

**任务**：

1. 从 [quack/softmax.py:164-171](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L164-L171) 的 `online_softmax_reduce(...)` 调用出发，列出传入的参数（注意 `reduction_buffer[None, None, 0]` 取的是哪个槽、`mbar_ptr` 是否非空、`hook_fn` 是什么）。
2. 进入 [quack/reduce.py:270-367](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L270-L367)，标注：
   - 哪几行是「lane 内 + warp 内」耦合归约（阶段 1）；
   - `hook_fn()`（即 `cluster_wait`）在哪一行、为什么放在这里；
   - 哪几行是「跨 CTA」合并（阶段 2），分别对应 `mbarrier_arrive_and_expect_tx`、`store_shared_remote`、`mbarrier_wait`、合并循环。
3. 画出一张数据流图，节点包括：每 lane 的 partial、每 warp 的 (max, sum) 对、打包成 Int64、peer CTA buffer、mbarrier 信用、拆包合并、最终 (max, sum, exp_x)。
4. 回答：为什么这个流程里 max 和 sum 必须一起传输、一起合并？如果拆成两次独立 cluster 归约会损失什么？

**交付物**：一张标注完整的数据流图 + 一段说明 `cluster_wait` 与 `mbarrier_wait` 各自等待的是什么（前者等 cluster 里所有 CTA 完成初始化级别的 arrive；后者等 peer CTA 把承诺字节的 partial 写进本 CTA buffer）。

> 实证可选：若有合适 GPU，参考 [u2-l2](u2-l2-softmax-fwd.md) 的运行方式，用 `cluster_n=2` 跑一次在线 softmax，与 `cluster_n=1` 的输出做数值比对，确认耦合归约的正确性。否则标注「待本地验证」。

## 6. 本讲小结

- **`warp_reduce`** 是归约体系的最底层，编译期在 `redux.sync`（Int32 全架构、Float32 仅 SM100）与 shuffle 蝶形兜底之间二选一；子组掩码让 `threads_in_group < 32` 的对齐子组也能正确归约。
- **`row_reduce`** 把「一行 → 标量」编排成两阶段：先 `TensorSSA.reduce` + `warp_reduce` 得到每 warp 一个 partial，再视情况走 `block_reduce`（CTA 内）或 `cluster_reduce`（跨 CTA）。第二阶段在 `warps_per_row==1 and cluster_n==1` 时被编译期剔除。
- **`hook_fn`** 让调用者在两阶段之间插入 `cluster_wait()`，把 cluster 同步等待重叠到 warp 归约的计算时间上。
- **跨 CTA 归约**靠事务屏障：`mbarrier_arrive_and_expect_tx` 上膛 → `store_shared_remote`（`st.async…complete_tx::bytes`）写数据并记信用 → `mbarrier_wait` 等齐；本质是「通过 smem 的全连接广播」，每个 CTA 最终都持完整结果。
- **`online_softmax_reduce`** 用可合并的 (max, sum) 对把两次归约耦合为一次；两个 Float32 打包成 Int64，跨 CTA 只需一次 mbarrier 往返；合并时按全局 max 重缩放每个 sum。
- 数学上 \(s = s_a e^{m_a-m} + s_b e^{m_b-m}\) 是耦合归约成立的基础，也是 max/sum 必须同传的原因。

## 7. 下一步学习建议

- **[u2-l5 RMSNorm](u2-l5-rmsnorm.md)**：看另一个调用同一套 `row_reduce` 的归约内核，比较它为何需要额外的 `rstd` 缓冲、reduction_dtype 如何影响 redux 选择。
- **[u3-l5 异步流水线与同步原语](u3-l5-pipeline-sync.md)**：本讲的 mbarrier 是「单次」事务屏障；持久化内核里它被升级成 `PipelineStasAsync`，在多级流水线中反复复用 phase，建议接着学。
- **直接阅读**：`quack/reduce.py` 里的 `swap_shuffle_reduce`（[L370-458](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L370-L458)）是一种「结果分布而非复制」的高级蝶形变体，被 EVT 式 epilogue 使用，可作为进阶阅读。
