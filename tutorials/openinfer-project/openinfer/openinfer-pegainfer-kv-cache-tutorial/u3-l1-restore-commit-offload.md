# 恢复-提交：reserve_loaded_blocks 与 KV offload 桥

## 1. 本讲目标

GPU 显存（HBM）装不下所有会话的 KV。上一讲（u2-l5）的前缀缓存只能复用**还在 GPU 上**的块；一旦块被 LRU 逐出，下次同前缀请求就得整段重算。本讲解开最后一块拼图——**CPU/SSD 层恢复路径**：当一个前缀只存在于主机内存（或更慢的层）时，如何把它搬回 GPU 并注册成「看起来就像一直住在 GPU 上」的可复用前缀。

学完后你应当能够：

1. 独立走通 `probe_prefix → reserve_loaded_blocks → commit_loaded_blocks → match_and_add_prefix` 的完整恢复链路，并解释每一步为什么长这样。
2. 推导 `PrefixProbe` 的三个窗口口径（`gpu_hit` / `cacheable` / `cpu_query_window`），理解 16 字节内容键与 lineage 哈希链的关系。
3. 说清本 crate（逻辑池）与兄弟 crate `pegainfer-kv-offload`（物理搬运）的分工边界。
4. 认识 `tests/restore_commit.rs` 顶部注释作为 **#704 性能回归锚**的工程记录方式。

## 2. 前置知识

本讲建立在前两讲的概念上，先快速回顾：

- **lineage 哈希链**（u2-l5）：每个整块的缓存键 = f(位置, 块内 token, 父块哈希, salt)。共享前缀的块哈希逐位相同，分叉后全部不同。这个性质由内联测试 `prompt_block_hashes_stable_across_shared_prefix` 守护。
- **末 token 规则**（u2-l5）：一个 n token 的 prompt 最多复用 \( \lfloor (n-1)/b \rfloor \) 个整块（b 为 block_size），永远留最后一个 prompt token 不缓存，保证首个生成 token 由最后一段 prefill 产生。
- **非活跃池**（u2-l1/u2-l5）：请求 `release` 后，已注册块进入 LRU 非活跃池——它们还在 GPU 上、还能被匹配，直到被挤出。`evict_inactive()` 可以显式把非活跃池清空（[src/pool.rs:L129-L131](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L129-L131) 强制把前缀挤出 HBM，正是测试恢复路径的手段）。
- **指针稳定的物理缓冲**（u2-l4）：`KvBuffer` 一次性分配、地址永不移动。本讲会看到 kv-offload 只注册一次它的基址。

一句话直觉：**恢复 = 在逻辑池里"补登记"一段本不存在的 GPU 前缀，同时让物理层异步把字节填进对应的页**。登记（逻辑）与搬运（物理）是两条腿，本 crate 管登记，kv-offload 管搬运。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| `src/pool.rs` | 恢复链路的全部逻辑池 API：`probe_prefix` / `reserve_loaded_blocks` / `commit_loaded_blocks`、`PrefixProbe`、`LoadReservation`、`sequence_hash_bytes` |
| `tests/restore_commit.rs` | 恢复链路的纯逻辑测试（无需 GPU），兼作 #704 性能回归锚 |
| `../pegainfer-kv-offload/src/engine.rs` | 兄弟 crate：`OffloadEngine` 连接器，真正在 GPU 与 pegaflow host/SSD 层之间搬运字节 |
| `../pegainfer-qwen3/src/executor.rs` | 消费侧接线实例：执行器如何把本 crate 的 API 与 `OffloadEngine` 缝在一起 |
| `../pegainfer-qwen3/tests/kv_offload_cpu_hit.rs` | 端到端集成测试：真实模型上验证 save → 逐出 → 恢复 → 重匹配 |
| `../CLAUDE.md` | 架构分工：共享分页 KV 原语在 `pegainfer-kv-cache`，host/SSD/RDMA 卸载桥在 `pegainfer-kv-offload` |

CLAUDE.md 中的一句话定位（[CLAUDE.md:L113](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/CLAUDE.md#L113)）：**模型调度器自持请求状态；共享分页 KV 原语在 `pegainfer-kv-cache`；host/SSD/RDMA 卸载桥在 `pegainfer-kv-offload`**。本讲就是这两个 crate 的交界处。

## 4. 核心概念与源码讲解

### 4.1 PrefixProbe：CPU 层查询窗口与 LoadReservation 目的地清单

#### 4.1.1 概念说明

`PrefixProbe` 回答一个问题：**"这个 prompt 的前缀，GPU 上已经有几块、还差几块？"** 它是一个不创建请求的只读探测器，同时用强引用（`held: Vec<ImmutableBlock<()>>`）把 GPU 命中的块钉住——因为接下来要发起一次异步 CPU→GPU 加载，期间这些块绝不能被 LRU 逐出，否则"接续"就断了。

三个关键口径（设 prompt 长 n、block_size 为 b）：

| 口径 | 公式 | 含义 |
|------|------|------|
| 整块数 | \( \lfloor n/b \rfloor \) | `seq_hashes.len()`，全部可能的缓存键 |
| `cacheable` | \( \lfloor (n-1)/b \rfloor \) | 复用上限（末 token 规则钳制） |
| `gpu_hit` | `match_blocks` 命中的连续前缀块数 | 已在 HBM 的部分 |
| `cpu_query_window` | \( \text{cacheable} - \text{gpu\_hit} \) | 需要向 CPU 层查询的块数 |

注意 `cpu_query_window` 是 `cacheable` 而非整块数减 `gpu_hit`：**CPU 加载量绝不能超过复用上限**。源码注释直说原因——匹配永远留下至少一个 prompt token 不缓存，若多加载了含末 token 的那块，它永远不会再被匹配到，等于白搬（[src/pool.rs:L216-L219](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L216-L219)）。

`LoadReservation` 则是恢复的另一头：reserve 出来的 GPU 目的地块清单。它对外**不透明**——字段私有，只能由 `reserve_loaded_blocks` 产生、被 `commit_loaded_blocks` 消费，你只能读两个访问器：`page_ids()`（物理页号，按租约顺序）和 `len()`。

#### 4.1.2 核心流程

```
probe_prefix(prompt, lora)
  ├─ 构造一次性请求 → 算出完整 lineage 哈希链 seq_hashes
  ├─ cacheable = ⌊(n-1)/b⌋
  ├─ match_blocks(seq_hashes) → gpu_hit 个强引用（钉住，防逐出）
  └─ 返回 PrefixProbe { seq_hashes, gpu_hit, cacheable, held }

之后探测器可回答：
  cpu_query_hashes()  → [gpu_hit..cacheable) 块的 16 字节键（查 CPU 层用）
  cpu_query_window()  → 上面的块数（不物化字节）
  boundary_hash(t)    → t token 处（块对齐）的前缀血统键（P/D 联合寻址用）
  held_blocks()       → 目前钉住的总块数（GPU 命中 + 已提交的加载块）
```

#### 4.1.3 源码精读

探测入口与三个口径的推导（[src/pool.rs:L207-L229](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L207-L229)）——`probe_prefix` 是公共入口（[src/pool.rs:L200-L202](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L200-L202)），内部委托给带 cache_salt 的私有版本：

```rust
let cacheable = num_input.saturating_sub(1) / self.block_size;
let gpu_guard = self.block_manager.match_blocks(&seq_hashes);
let gpu_hit = gpu_guard.len();
PrefixProbe { seq_hashes, gpu_hit, cacheable, block_size: self.block_size, held: gpu_guard }
```

CPU 查询键的切窗（[src/pool.rs:L324-L332](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L324-L332)）：GPU 命中已覆盖全部可复用块时返回空——没什么可加载，正常 prefill 即可：

```rust
if self.gpu_hit >= self.cacheable {
    return Vec::new();
}
self.seq_hashes[self.gpu_hit..self.cacheable]
    .iter()
    .map(|h| sequence_hash_bytes(h).to_vec())
    .collect()
```

`boundary_hash`（[src/pool.rs:L308-L319](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L308-L319)）：以 token 数为参数、必须块对齐且不超过复用上限，返回**覆盖整条前缀血统**的键（而非仅末块内容）。qwen3 执行器在 vLLM 兼容模式下用 `cpu_query_window()` 切自己的哈希链（[executor.rs:L2528-L2536](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L2528-L2536)），注释明确该窗口口径可被外部键方案替换。

`LoadReservation` 的两个访问器（[src/pool.rs:L358-L370](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L358-L370)）——文档注释一句话讲透它与搬运层的对应关系："第 i 个被租借的块落在 `page_ids()[i]`"：

```rust
pub fn page_ids(&self) -> Vec<i32> {
    self.blocks.iter().map(|b| b.block_id() as i32).collect()
}
```

#### 4.1.4 代码实践

**目标**：手算并验证三个窗口口径。

1. 阅读 `tests/restore_commit.rs` 的 `commit_absorbs_full_prefix`（[tests/restore_commit.rs:L11-L29](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/restore_commit.rs#L11-L29)）。
2. 手算：prompt 为 33×16+1 = 529 个 token、b=16，求整块数、`cacheable`、`cpu_query_window`（GPU 空池）。
3. 运行（该测试文件自述纯逻辑、无需 GPU）：`cargo test --release -p pegainfer-kv-cache --test restore_commit`。
4. 对照测试断言 `cpu_query_window() == 33` 验证你的推导。

**需要观察的现象**：两个测试通过；无 CUDA 环境也能跑。
**预期结果**：整块数 = 33，\( \text{cacheable} = \lfloor 528/16 \rfloor = 33 \)，窗口 = 33 − 0 = 33。（529 = 33 整块 + 1 个转发 token，末 token 规则恰好不裁掉任何整块。）

#### 4.1.5 小练习与答案

**练习 1**：prompt 长 130、b=16、GPU 空池，`cpu_query_window` 是多少？若长 129 呢？
**答案**：130 → 整块 8、\( \text{cacheable}=\lfloor 129/16 \rfloor=8 \)、窗口 8；129 → 整块 8、cacheable 8、窗口 8。两种都只能加载 8 块——第 9 块含末 token，永不复用。

**练习 2**：为什么 `PrefixProbe` 必须持有 GPU 命中块的强引用，而不是探测完就放？
**答案**：探测与随后的加载/重匹配之间隔着一次异步 CPU→GPU 拷贝。若期间块被 LRU 逐出、页被新请求复用写入，恢复块接续的"前半段"就没了，重匹配只能命中后半段甚至错配。`held` 把窗口关死。

**练习 3**：`held_blocks()` 与 `reusable_blocks()` 何时不相等？
**答案**：当有人把探测器的加载块数扩到超过 `cacheable` 时。`reusable_blocks() = held.len().min(cacheable)`（[src/pool.rs:L293-L296](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L293-L296)）按机制层钳制。

### 4.2 commit_loaded_blocks：在 continuation 哈希下注册恢复块

#### 4.2.1 概念说明

`reserve_loaded_blocks(count)` 从空闲池拿 count 个**可变块**（`MutableBlock`）作为加载目的地；块压力下返回 `None`，调用方放弃预取、从头 prefill（[src/pool.rs:L236-L239](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L236-L239)）。

字节落进这些页之后，`commit_loaded_blocks` 做逻辑侧的"补登记"：把第 i 个加载块**以 `seq_hashes[gpu_hit + i]`（continuation 哈希）注册**进注册表。为什么要用"接续"哈希？因为 lineage 链上前 0..gpu_hit 号块的哈希已被 GPU 命中块占用——恢复块必须是链条的**下一段**，注册后 0..gpu_hit+reserved 才构成一条可被整体重匹配的连续前缀。注册错一个偏移，链条就断：qwen3 集成测试的注释直言，"GPU+CPU 混合命中"这个用例专抓 query/commit 偏移里差一个 `gpu_hit` 的 bug，纯 CPU 场景（gpu_hit==0）抓不到（[kv_offload_cpu_hit.rs:L293-L296](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/tests/kv_offload_cpu_hit.rs#L293-L296)）。

#### 4.2.2 核心流程

```
reserve_loaded_blocks(k) → LoadReservation（k 个可变块；压力下 None）
物理层异步把 k 块字节写进 reservation.page_ids() 对应的页
commit_loaded_blocks(&mut probe, reservation):
    start = probe.gpu_hit
    for i, block in reservation:
        hash = probe.seq_hashes[start + i]        # continuation 哈希
        block.stage(hash, block_size) → 完整块     # 声明"这是整块有效 KV"
        probe.held.push(register_block(完整块))     # 注册 + 纳入钉住集
之后：new_request(...).match_and_add_prefix() 命中整段 GPU+CPU 前缀
```

`stage` 是 kvbm 的仪式：把可变块标记为"已填满、内容以该哈希为准"，`register_block` 再把它挂进按哈希寻址的注册表并返回 RAII 强引用。这套与 padding 块预留（u2-l1）、正常封块（u2-l2）是同一机制——恢复块在逻辑层与正常计算出的块**完全同权**。

#### 4.2.3 源码精读

提交循环本体（[src/pool.rs:L248-L257](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L248-L257)）：

```rust
let start = probe.gpu_hit;
for (i, block) in reservation.blocks.into_iter().enumerate() {
    let hash = probe.seq_hashes[start + i];
    let complete = block
        .stage(hash, self.block_size)
        .expect("loaded block stage: block_size invariant violated");
    probe.held.push(self.block_manager.register_block(complete));
}
```

文档注释点出第二个关键动作：注册后的块被折进探测器的钉住集，"探测器持续持有每个已注册块直到请求 prefill，关闭注册与重匹配之间的逐出窗口"（[src/pool.rs:L241-L247](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L241-L247)）。

纯逻辑测试 `commit_extends_partial_gpu_hit`（[tests/restore_commit.rs:L31-L56](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/restore_commit.rs#L31-L56)）是本讲的基准场景：先用真实请求在池里种下 3 块，再探测（gpu_hit=3、窗口=5）、reserve(5)、commit，断言 `held_blocks()==8`，最后同 prompt 新请求 `match_and_add_prefix` 命中 8×16=128 token：

```rust
let mut probe = pool.probe_prefix(prompt.clone(), None);
assert_eq!(probe.gpu_hit_blocks(), 3);
assert_eq!(probe.cpu_query_window(), 5);
let reservation = pool.reserve_loaded_blocks(5).expect("reserve");
pool.commit_loaded_blocks(&mut probe, reservation);
assert_eq!(probe.held_blocks(), 8);
```

qwen3 执行器里的真实接线（消费侧，验证这套 API 的用法正是设计意图）：

- 探测与查询（[executor.rs:L2518-L2523](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L2518-L2523)）：`probe_prefix(...)` 后取 `probe.cpu_query_hashes()` 交给连接器；
- 预约与装载（[executor.rs:L2151-L2159](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L2151-L2159)）：`reserve_loaded_blocks(num_blocks)` 拿预约，`reservation.page_ids()` 交给 `offload.load(lease, page_ids)`；
- 落地提交（[executor.rs:L1974-L1993](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L1974-L1993)）：装载成功才 `commit_loaded_blocks(&mut st.probe, reservation)`；失败则丢弃预取状态，请求从头 prefill——**恢复永远是优化，不是正确性依赖**。

#### 4.2.4 代码实践

**目标**：纸上走一遍提交循环的索引。

1. 场景：`gpu_hit=3`、reserve 5 块、`seq_hashes.len()=8`。列出 5 个加载块各自以 `seq_hashes` 的第几号哈希注册。
2. 若实现误写成 `let start = 0;`，描述重匹配会发生什么。
3. 运行 `cargo test --release -p pegainfer-kv-cache --test restore_commit` 对照断言。

**预期结果**：
1. 依次是 `seq_hashes[3..8]`——第 i 块用 `seq_hashes[3+i]`。
2. 加载块会以 `seq_hashes[0..5]` 注册，但 0..3 号哈希已由 GPU 命中块持有（注册表冲突/重复键，取决于注册表去重策略），而 5..8 号哈希无人认领——新请求匹配到第 3 块处链条断裂，只能命中 3 块，后 5 块的搬运全部浪费；这正是集成测试注释里说的"差一个 gpu_hit"事故。

#### 4.2.5 小练习与答案

**练习 1**：`reserve_loaded_blocks` 返回 `None` 时调用方该做什么？为什么不能等一等？
**答案**：放弃预取、从头 prefill（qwen3 的做法：先 `release_query_lease` 归还 CPU 层租约再返回）。等待会把请求挂在空闲块上，而空闲块紧张恰恰意味着别的请求在用——推理引擎的调度宁可重算也不停摆。

**练习 2**：`commit_loaded_blocks` 里 `stage` 的 `expect` 为什么敢断言不会失败？
**答案**：`stage(hash, block_size)` 失败的唯一原因是块容量与 block_size 不变式被破坏；预约块来自同一个 `BlockManager`、同一个 block_size，属于"按构造成立"的不变式，违反即程序错误，应当 panic 而非运行时降级。

**练习 3**：恢复块被 `match_and_add_prefix` 命中、请求完成后 `release`，这些块去哪？
**答案**：与正常块完全一样——进入 LRU 非活跃池，可被后续请求再次匹配，直到被挤出。恢复是一次性成本，之后就是普通 GPU 前缀缓存。

### 4.3 sequence_hash_bytes：128 位 lineage 哈希 → 16 字节内容键

#### 4.3.1 概念说明

两个世界说两种"话"：逻辑池内部用 kvbm 的 `SequenceHash`（128 位血统哈希，类型化）；offload 层（以及更远的 pegaflow、跨进程对端）需要一个**字节串内容键**来寻址。翻译函数只有一行：

```rust
fn sequence_hash_bytes(hash: &SequenceHash) -> [u8; 16] {
    hash.as_u128().to_be_bytes()
}
```

（[src/pool.rs:L737-L739](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L737-L739)）

三个要点：

1. **它是纯编码，不是新哈希**：`[u8;16]` 与 `SequenceHash` 一一对应，16 字节内容键承载的就是整条 lineage——保存方（save）与查询方（query）只要共享前缀就推出相同键，这是整套跨层缓存成立的根基。
2. **大端序（`to_be_bytes`）**：注释写明"为线上序稳定"（big-endian for a stable on-wire ordering）——键要进网络协议、磁盘索引、字典序比较，字节序必须与主机架构无关。
3. **单一翻译点**：所有面向 offload 的出口（`cpu_query_hashes`、`boundary_hash`、`active_prefix_hashes`、save 侧的 `assigned_block_hashes`）都经这同一个函数，杜绝两套编码并存。

#### 4.3.2 核心流程

```
kvbm SequenceHash（128 位血统哈希）
        │  sequence_hash_bytes: as_u128().to_be_bytes()
        ▼
[u8; 16] 大端内容键
        ├─ save 侧：assigned_block_hashes() → (page_id, [u8;16]) 交给连接器存
        └─ query 侧：cpu_query_hashes() → Vec<Vec<u8>> 交给连接器查
```

#### 4.3.3 源码精读

save 侧的出口（[src/pool.rs:L657-L664](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L657-L664)）——"请求当前持有的每块的 (页号, 内容哈希)，按 prompt 顺序"，封块后驱动 offload 保存：

```rust
pub fn assigned_block_hashes(&self) -> Vec<(i32, [u8; 16])> {
    self.seq.inner().assignments().assigned_iter()
        .map(|(&id, block)| (id as i32, sequence_hash_bytes(&block.sequence_hash())))
        .collect()
}
```

与之配套的 `assigned_block_guards()`（[src/pool.rs:L674-L681](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L674-L681)）返回与上面对齐的 `KvBlockGuard` 钉子——异步 GPU→CPU 拷贝进行期间，源块不能被回收复写。

键一致性的守护测试（[src/pool.rs:L749-L776](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L749-L776)）：共享 3 块前缀的两个请求，`prompt_block_hashes`（`#[cfg(test)]` 的内部视图，同样经 `sequence_hash_bytes`）前 3 个键逐字节相等、第 4 块分叉必不同、异 LoRA salt 必不同。注释直陈动机："否则一个请求存的热块永远匹配不到下一个请求"。

消费侧的对应物：kv-offload 的 `OffloadEngine` 模块文档明确"块内容哈希在这里是不透明的 `Vec<u8>`——调用方（调度器）从 kvbm 序列哈希推导，本层不依赖逻辑缓存哈希方案"（[engine.rs:L1-L10](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L1-L10)）。两 crate 之间唯一的契约就是这 16 个字节。

#### 4.3.4 代码实践

**目标**：确认"键 = 纯编码"并理解大端序的意义。

1. 读 [src/pool.rs:L735-L739](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L735-L739) 的文档注释与实现。
2. 在本机任意 Rust 环境（或 `rustc main.rs` 直接编译）运行下面的**示例代码**（非项目代码），观察同一 u128 在大端/小端下的字节差异：

```rust
fn main() {
    let h: u128 = 0x00_11_22_33_44_55_66_77_88_99_AA_BB_CC_DD_EE_FF;
    println!("be: {:02x?}", h.to_be_bytes()); // 首字节 00 —— 高位在前，任意平台一致
    println!("le: {:02x?}", h.to_le_bytes()); // 首字节 ff —— 依赖平台
}
```

3. 思考：若 save 侧用小端、query 侧用大端，会发生什么？

**需要观察的现象**：大端输出以 `00, 11, 22...` 开头，小端以 `ff, ee, dd...` 开头。
**预期结果**：两端编码不一致时，同一血统哈希产生两个不同键——CPU 层永远查不中自己存的块，缓存命中率静默归零（不报错、只变慢）。这正是"单一翻译点"约束存在的原因。

#### 4.3.5 小练习与答案

**练习 1**：`cpu_query_hashes()` 返回 `Vec<Vec<u8>>` 而 `boundary_hash()` 返回 `[u8; 16]`，为什么类型不同？
**答案**：`[u8;16]` 是栈上定长值，池内部传递零分配；`Vec<Vec<u8>>` 是要跨 crate 交给 offload 连接器的所有权集合——连接器把哈希当不透明字节串，定长信息对它没有意义。

**练习 2**：16 字节内容键会不会碰撞？
**答案**：键空间即 128 位 lineage 哈希的空间，碰撞概率与哈希本身相同；`sequence_hash_bytes` 不引入任何额外碰撞面（一一映射）。

### 4.4 兄弟 crate pegainfer-kv-offload：真正的搬运工与 #704 回归锚

#### 4.4.1 概念说明

大纲把 kv-offload 的内部结构标为"待确认"；本讲写作时已实测确认：crate 共 7 个源文件（`config.rs`、`engine.rs`、`handle.rs`、`host.rs`、`layout.rs`、`lib.rs`、`vllm_hash.rs`）加集成测试 `tests/cpu_roundtrip.rs`。核心类型是 `OffloadEngine`——"把 KV 块在 pegainfer 的 GPU 分页缓存与 pegaflow 的 host/SSD 层之间搬运的进程内连接器"。

分工边界一句话：**本 crate 管逻辑（谁的块、什么哈希、挂在链条哪里），kv-offload 管字节（哪个源、哪个目的地、怎么拷）**。两侧通过三个纯数据契约咬合：

| 契约 | 本 crate 侧 | kv-offload 侧 |
|------|-------------|----------------|
| 内容键 | `cpu_query_hashes()` / `sequence_hash_bytes` | `query(req_id, block_hashes, ...)` 的不透明 `Vec<u8>` |
| 目的地页号 | `LoadReservation::page_ids()` | `load(lease, dst_block_ids)`："第 i 个租借块落进 dst_block_ids[i]" |
| 拷贝期间钉住 | `KvBlockGuard`（save 侧 `assigned_block_guards`） | `submit_save(..., keep_alive)` 的泛型负载 |

#### 4.4.2 核心流程

完整闭环（qwen3 执行器实测接线的顺序）：

```
保存（前台请求结束后）：
  assigned_block_hashes() + assigned_block_guards()
    → OffloadEngine::save(page_ids, hashes, keep_alive=guards)   # 异步 D2H，尽力而为
    → flush_offload_saves()                                       # 落盘屏障
逐出（制造恢复场景）：
  evict_inactive()                                                # HBM 只剩 CPU 层有这前缀
恢复（新请求到达）：
  probe_prefix() → cpu_query_hashes()
    → OffloadEngine::query() → QueryHit{ lease, num_blocks }      # CPU 层连续命中长度
    → reserve_loaded_blocks(num_blocks) → page_ids()
    → OffloadEngine::load(lease, page_ids)                        # 异步 H2D
    → 落地后 commit_loaded_blocks(&mut probe, reservation)
    → 请求 prefill 时 match_and_add_prefix() 命中整段前缀
```

两个设计不变式值得记住：**保存是尽力而为**（丢一次保存只损失未来命中，绝不伤正确性）；**恢复是优化**（装载失败就从头 prefill）。KV offload 永远不站在正确性的关键路径上。

#### 4.4.3 源码精读

`OffloadEngine::new` 以 `KvBuffer` 注册 GPU 侧（[engine.rs:L101](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L101)）——注册时"烘焙原始设备地址"（见 `with_arenas_on` 的文档：每个 arena 的设备分配必须终身存活且指针稳定），这正是 u2-l4 讲过的 `KvBuffer` 指针稳定性承诺的第二个消费者。

查询：`query` 问"block_hashes 的前缀有多长住在 CPU 层"（[engine.rs:L381](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L381)，内部 `submit_query` 在 [L328](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L328)），命中返回租约 + 块数。

装载（[engine.rs:L392-L406](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L392-L406)）——文档与 `LoadReservation::page_ids` 的文档逐字呼应：

> Copy the leased CPU blocks into the GPU blocks named by `dst_block_ids` ... pegaflow maps the i-th leased block onto `dst_block_ids[i]` for each layer.

**#704 性能回归锚**。`tests/restore_commit.rs` 顶部注释（[tests/restore_commit.rs:L1-L5](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/restore_commit.rs#L1-L5)）记录：

> Also serves as the registration-cost regression anchor for #704 (a 1024-block commit is ~1.6ms after the registry PRT fix; it was 36ms before.)

背后的故事在提交 `a157baf6`（`fix: eliminate the scheduler-thread stall on bulk KV restore (#704) (#705)`）：kvbm 注册表的位置映射 `PositionalRadixTree` 每逢新位置首次注册就分配一个完整分片的 DashMap——分片数随核数伸缩（48 核机器 256 片、192 核 H200 节点 1024 片），实测该分配占用每块注册 25µs 中的 21µs，比真正状态转移（约 0.5µs）贵 35 倍，把 1024 块的批量恢复提交推到 36ms，直接卡住调度线程。修复把内层换成免分配的普通 `HashMap`，同一提交信息给出实测数字：**1024 块恢复提交在 48 核机器上 36.4ms → 1.67ms（22 倍），1.63µs/块**。该提交同时改动了 `kvbm-logical` 注册表、本测试文件（新增 56 行）与 `pegainfer-kv-offload/src/engine.rs`——一次跨三个 crate 的性能修复，最终把数字锚在测试文件的第一行注释里。

#### 4.4.4 代码实践

**目标**：用 git 考古还原 #704，并理解回归锚的记录方式。

1. 在 `pegainfer-kv-cache` 目录运行：`git log --oneline --follow -- tests/restore_commit.rs`（预期两个提交：改名提交与 `a157baf6`）。
2. 运行 `git show a157baf6 --stat`，找出改了哪几个 crate、各自几行。
3. 阅读该提交的完整信息（`git show a157baf6` 不带 `--stat`），摘出 36.4ms → 1.67ms 的测量条件（机器核数、块数）。
4. （可选，待本地验证）在本地仓库临时给 `commit_absorbs_full_prefix` 加计时（**示例代码**，读者本地实验用，不属于项目代码）：

```rust
let t0 = std::time::Instant::now();
pool.commit_loaded_blocks(&mut probe, reservation);
eprintln!("commit 33 blocks: {:?}", t0.elapsed());
```

把 prompt 与 reserve 数放大到 1024 块（需要约 1025×16 token 的 prompt 与千块以上的池），对照 1.63µs/块 的锚值。

**需要观察的现象**：`--stat` 显示 `kvbm-logical/src/registry/*`、`tests/restore_commit.rs`、`pegainfer-kv-offload/src/engine.rs`、`pegainfer-sample/src/lib.rs` 等多个文件；计时输出为微秒量级。
**预期结果**：小规模提交耗时在数十微秒内；1024 块约 1.6–2ms。第 4 步的具体数字依赖本地机器核数，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么保存路径设计成"尽力而为"，而恢复路径的失败要显式处理？
**答案**：保存丢了大不了下次不命中、重算一次——只是性能；恢复失败如果不清理（归还没用上的预约块与租约），会泄漏 GPU 空闲块和 CPU 层租约 TTL，伤害后续请求。所以 `settle_prefetch` 的 Err 分支要移除预取状态、`start_prefetch_load` 预约失败要 `release_query_lease`。

**练习 2**：`query` 与 `load` 之间为什么隔着"租约（lease）"？
**答案**：查询命中到装载落地之间，CPU 层的块可能被逐出。租约把命中块钉在 host 层，且 `load` 按 `dst_block_ids[i]` 的顺序映射，保证"查到的第 i 块"就是"装进第 i 个目的地页"的那块字节。

**练习 3**：为什么 #704 的修复落在 kvbm-logical 而不是本 crate？
**答案**：瓶颈在注册表（`PositionalRadixTree`）的数据结构选择，而注册表属于上游 kvbm-logical；本 crate 的 `commit_loaded_blocks` 只是循环调用方。性能修复要落在真正的成本所在层——这也是回归锚记在本测试文件、修复却在另一个 crate 的原因。

## 5. 综合实践

**任务**：以 `commit_extends_partial_gpu_hit` 为蓝本，新写一个「2 块 GPU 命中 + 6 块恢复」的测试，完整走一遍 probe → reserve → commit → 再匹配。

**推导先行**（先手算再写码）：

- 目标 `held_blocks()==8`、命中 8×16=128 token → 需要 `cacheable ≥ 8`，即 prompt 长 \( n \geq 8 \times 16 + 1 = 129 \)。取 129：整块数 8、`cacheable = ⌊128/16⌋ = 8`。
- GPU 命中 2 块 → 用一个 32 token 的种子请求（prompt 的前 32 个 token，恰好 2 整块）prefill 后 release。
- 恢复 6 块 → `cpu_query_window = 8 − 2 = 6`。

在本地仓库 `tests/restore_commit.rs` 末尾追加（**示例代码**，读者本地实验用；该文件纯逻辑、无需 GPU）：

```rust
#[test]
fn commit_extends_two_block_gpu_hit_with_six_loaded() {
    let pool = BlockPool::new(BLOCK_SIZE, 64).expect("pool");
    // 8 full blocks + 1 forwarded token → cacheable = 8.
    let prompt: Vec<u32> = (0..=(8 * BLOCK_SIZE as u32)).map(|i| i % 89).collect();

    // Seed the GPU cache with the first 2 blocks via a real request.
    let mut seed = pool.new_request(prompt[..2 * BLOCK_SIZE].to_vec(), 1, None);
    seed.schedule_prefill(2 * BLOCK_SIZE, &pool).expect("seed schedule");
    seed.apply_prefill(1, &pool).expect("seed apply");
    seed.release().expect("seed release");

    // Probe: 2-block GPU hit, the restore covers the remaining 6.
    let mut probe = pool.probe_prefix(prompt.clone(), None);
    assert_eq!(probe.gpu_hit_blocks(), 2);
    assert_eq!(probe.cpu_query_window(), 6);

    let reservation = pool.reserve_loaded_blocks(6).expect("reserve");
    pool.commit_loaded_blocks(&mut probe, reservation);
    assert_eq!(probe.held_blocks(), 8);

    // A request over the same prompt reuses the whole GPU+CPU prefix.
    let mut req = pool.new_request(prompt, 4, None);
    let matched = req.match_and_add_prefix(&pool).expect("match");
    assert_eq!(matched, 8 * BLOCK_SIZE);
}
```

**操作步骤**：

1. 追加测试后运行：`cargo test --release -p pegainfer-kv-cache --test restore_commit`。
2. 观察新测试与两个既有测试全部通过。
3. 故意把 `reserve_loaded_blocks(6)` 改成 `(7)`（超出 cacheable 的加载）：观察 `held_blocks()` 变 9 而 `match_and_add_prefix` 仍只命中 128——多出的块永不复用；再改回。
4. 把种子请求换成 48 token（3 块），断言 `gpu_hit_blocks()==3`、窗口变 5——验证你对口径的控制。

**预期结果**：第 2 步三个测试全绿；第 3 步印证 4.1 讲的"CPU 加载不得超 cacheable"；第 4 步 gpu_hit=3、window=5、matched 仍为 128。全程无需 GPU（**待本地验证**——本讲写作环境未执行，结论基于同构既有测试与源码推导）。

## 6. 本讲小结

- 恢复链路四拍：`probe_prefix`（算窗口、钉住 GPU 命中）→ `reserve_loaded_blocks`（拿目的地页）→ 物理层异步 `load` → `commit_loaded_blocks`（在 continuation 哈希 `seq_hashes[gpu_hit+i]` 下补登记），随后 `match_and_add_prefix` 把 GPU+CPU 拼成一条连续可复用前缀。
- 三个窗口口径：`cacheable = ⌊(n−1)/b⌋`（末 token 规则）、`gpu_hit`（连续命中）、`cpu_query_window = cacheable − gpu_hit`；CPU 加载量绝不能超过 `cacheable`。
- 16 字节内容键 = `SequenceHash.as_u128().to_be_bytes()`：纯编码、大端线上序、单一翻译点，是本 crate 与 kv-offload 之间唯一的契约。
- 分工：`pegainfer-kv-cache` 管逻辑登记（哈希、链条、钉住），`pegainfer-kv-offload` 的 `OffloadEngine` 管字节搬运（save 尽力而为、query 发租约、load 按页号映射）；恢复永远是优化——失败就从头 prefill。
- 注册偏移错一个 `gpu_hit`，链条就断在后半段——"GPU+CPU 混合命中"是专门抓这类 bug 的测试场景。
- #704：批量恢复提交 36.4ms → 1.67ms 的修复落在 kvbm 注册表（内层换普通 HashMap），数字作为回归锚记在 `tests/restore_commit.rs` 顶部注释——性能修复跨了三个 crate，证据锚在最贴近复现场的测试里。

## 7. 下一步学习建议

- **下一讲 u3-l2（KV 事件流与缓存感知路由）**：看 `PrefixProbe` 的兄弟机制——块 Create/Remove 事件如何广播给带外路由，`RegisteredBlock` 的两套哈希体系（引擎 lineage vs 路由 u64）如何对应。
- 精读 `../pegainfer-kv-offload/src/engine.rs` 全文：`flush_saves_then` 的屏障链、`PEGAINFER_KV_TRANSFER_MODE` 的 Direct vs Kernel 取舍（#704 同一批 A/B 的结论），是分布式 KV 一致性的好教材。
- 有 GPU + Qwen3-4B 权重时，运行 `../pegainfer-qwen3/tests/kv_offload_cpu_hit.rs`（`PEGAINFER_TEST_MODEL_PATH` 指向权重），观察 `cached_tokens` 与 `external_hit_tokens` 如何把「本地命中」与「跨连接器恢复」分开记账。
