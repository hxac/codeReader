# 前缀缓存：块哈希、匹配与 LoRA salt

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出前缀缓存的缓存键是什么：由「位置 + 块内容 + 父块哈希 + salt」构成的 lineage 哈希链，以及为什么共享前缀的请求会得到完全相同的键。
2. 解释两条匹配规则的动机：**只匹配整块**（partial tail block 永远不参与匹配），以及**永远留下最后一个 prompt token 不缓存**（cacheable = ⌊(n−1)/b⌋）。
3. 走通 `probe_prefix` → `PrefixProbe` 的探测链路，理解 `gpu_hit_blocks` / `cpu_query_window` / `held_blocks` / `reusable_blocks` 四个口径的差别。
4. 掌握 `match_and_add_prefix` 如何把已注册块「挂」到新请求上、`kv_position` 如何前移，以及 LoRA 名做 salt 如何实现缓存隔离。
5. 说明回收侧：`evict_inactive`、`active_prefix_hashes` 与上游 `BlockManager` 的 LRU 非活跃池如何配合。

## 2. 前置知识

本讲假设你已读过 u2-l1（BlockPool 与 padding 块）和 u2-l2（RequestKv 生命周期）。在此之上补充四个通俗概念：

- **前缀缓存（prefix cache）**：很多请求共享同一个 prompt 前缀（系统提示词、多轮对话的历史、few-shot 示例）。这些 token 的 K/V 一旦算过一次，后到的请求就没必要再算一遍——把已算好的整块 KV 直接挂给新请求，跳过这部分 prefill。收益非常直观：Qwen3-4B 上一个 ~1900 token 的重复 prompt，首 token 延迟（TTFT）从 141.8ms 降到 16.3ms（约 8.7 倍），见 [docs/models/qwen3/prefix-cache.md:L1-L16](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/docs/models/qwen3/prefix-cache.md#L1-L16)。
- **LoRA**：一种轻量微调方案——基座模型权重不动，额外挂一组很小的「适配器」权重参与前向。同一个 prompt 在不同适配器下会算出**不同的 K/V 字节**。这是本讲 salt 机制的直接动因。
- **盐（salt）**：密码学借来的词——计算哈希时混入的一段额外输入。同一个内容配上不同的盐，得到完全不同的哈希。把 LoRA 名当盐混进块哈希链，就能让「同 token 不同适配器」的块永远互相不可见。
- **活跃池 / 非活跃池**：承接 u2-l1 的结论——请求 `release()` 之后，它注册过的完整块不回空闲池，而是进入 LRU 管理的**非活跃池**（可被前缀匹配复活，也可能被回收）；正在被请求持有或被强引用钉住的块在**活跃池**。前缀缓存命中就是从这两个池里「认领」块。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `src/pool.rs` | 本讲主战场：`BlockPool` 的 salt 加链路（`new_request` / `new_request_with_cache_salt`）、探测（`probe_prefix` / `PrefixProbe`）、匹配（`RequestKv::match_and_add_prefix*`）、回收（`evict_inactive` / `active_prefix_hashes`），以及一组纯逻辑内联测试 |
| `tests/lifecycle.rs` | 需 CUDA 的集成测试，含本讲核心例证 `lora_salt_isolates_prefix_cache` |
| `Cargo.toml` | `dynamo-kv-hashing` 依赖（`compute_salt_hash` 的来源），版本经 workspace 统一钉在一个 git rev 上 |
| `kvbm/kvbm-logical/src/integrations/scheduled.rs` | 上游 `SchedulableSequence`：真正的匹配算法、「留最后一个 token」上限的强制点、`kv_position` 前移 |
| `kvbm/kvbm-logical/src/sequence/mod.rs` | 上游哈希链：`BlockSequence::new` 接受 salt，`all_sequence_hashes` 产出整条 lineage 链 |
| `kvbm/kvbm-logical/src/manager/mod.rs` | 上游 `BlockManager::match_blocks`（线性前缀匹配）与 `evict_inactive` |
| `kvbm/kvbm-logical/src/pools/inactive/backends/lru_backend.rs` | LRU 非活跃索引：插入即进 LRU、分配时弹最旧、命中即复活 |
| `pegainfer-qwen3/src/executor.rs` | 消费侧真实调用点：`match_and_add_prefix` 与 `probe_prefix` 怎么被引擎用到 |

## 4. 核心概念与源码讲解

### 4.1 块哈希链：前缀缓存的键

#### 4.1.1 概念说明

前缀缓存的第一个问题是：**怎么判断「这个块以前算过」？** 答案是给每个完整块算一个内容身份。但单块内容哈希不够——同一个 16-token 片段出现在序列不同位置时语义不同（RoPE 位置编码不同），也不能直接当缓存键。kvbm 的做法是 **lineage 哈希链**：每个块的哈希覆盖「它自己的内容 + 它在序列中的位置 + 父块的哈希」，概念上：

\[
h_0 = H(\text{salt},\ \text{pos}_0,\ c_0), \qquad
h_i = H(\text{salt},\ \text{pos}_i,\ c_i,\ h_{i-1})
\]

这样第 \(i\) 块的哈希实际上代表了「从序列开头到第 \(i\) 块」的完整历史——两个 prompt 只要前缀相同，前缀上每一块的哈希都逐位相同；一旦某块内容分叉，它和之后所有块的哈希全部不同。源码注释的原话是 "position + content + parent fragment, so block `i` of prompt `P` hashes the same no matter which request computed it"（见 [src/pool.rs:L635-L642](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L635-L642)）。

第二条设计：**salt 从链头就混入**。salt 变了，整条链从 \(h_0\) 起全部改变——这就是 LoRA 隔离的全部原理。

#### 4.1.2 核心流程

- 请求创建时：`tokens + block_size + salt` → `TokenBlockSequence`，内部为每个**封满**的块推导 lineage 哈希。
- 查询键：`all_sequence_hashes()` 返回所有完整块的哈希，按序列顺序排列；尾部不满的块不出现在链上（**只匹配整块**规则的哈希侧体现）。
- 跨层寻址：`sequence_hash_bytes` 把 128 位哈希打包成大端 16 字节，供 KV-offload 的 CPU 层当内容键使用。

#### 4.1.3 源码精读

上游 `BlockSequence` 的构造接受 salt，并暴露整条哈希链（[kvbm/kvbm-logical/src/sequence/mod.rs:L220-L251](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/kvbm/kvbm-logical/src/sequence/mod.rs#L220-L251)）：

```rust
pub fn new(tokens: Vec<Token>, block_size: u32, salt_hash: Option<SaltHash>) -> Self {
    let tokens = Tokens::from(tokens);
    Self { sequence: TokenBlockSequence::new(tokens, block_size, salt_hash) }
}
...
/// Returns all sequence hashes from completed blocks.
pub fn all_sequence_hashes(&self) -> Vec<SequenceHash> {
    self.sequence.blocks().iter().map(|b| b.kvbm_sequence_hash()).collect()
}
```

这段代码做了什么：salt 在序列构造时就进入哈希推导；`all_sequence_hashes` 只对 `blocks()`（已封满的块）取哈希——尾部的半块天然被排除。

16 字节内容键的打包在 [src/pool.rs:L735-L739](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L735-L739)：

```rust
fn sequence_hash_bytes(hash: &SequenceHash) -> [u8; 16] {
    hash.as_u128().to_be_bytes()
}
```

这段代码做了什么：把 lineage 哈希转成大端 16 字节，作为 offload 层（CPU/SSD）寻址块的稳定线上格式（该机制的完整恢复链路是 u3-l1 的主题）。

哈希链的两条关键性质由内联测试 `prompt_block_hashes_stable_across_shared_prefix` 守护（[src/pool.rs:L749-L776](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L749-L776)）：两个 64-token 请求共享前 48 个 token，断言前 3 块哈希逐位相等、第 4 块（内容分叉处）必不相等；末尾还断言换一个 LoRA 名后**第 0 块**哈希就不同：

```rust
// A different LoRA salt must poison the match — same tokens, new keys.
let c = pool.new_request(shared, 8, Some("adapter-x"));
assert_ne!(c.prompt_block_hashes()[0], ha[0],
    "salt (lora) must scope the prefix cache");
```

#### 4.1.4 代码实践

1. **实践目标**：亲眼确认「共享前缀哈希一致、分叉即全变、salt 毒化整条链」三条性质。
2. **操作步骤**：先笔头预测——prompt A = 48 个共享 token + 16 个 `7000..` token，prompt B = 同样 48 个共享 token + 16 个 `9000..` token，block_size = 16，各产出几块哈希？哪些相等？然后运行（纯逻辑、无需 GPU）：

   ```bash
   cargo test --release -p pegainfer-kv-cache --lib prompt_block_hashes -- --nocapture
   ```

3. **需要观察的现象**：测试通过；`--nocapture` 下无额外输出（该测试只靠断言说话）。
4. **预期结果**：`ha.len() == 4`、`ha[..3] == hb[..3]`、`ha[3] != hb[3]`、salt 版本第 0 块即不同。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么不用「整条 prompt 的哈希」做缓存键，而要逐块建链？
**答案**：整条 prompt 哈希只能命中完全相同的 prompt；逐块链允许两个请求只在**前缀**上相同就能共享前面所有块，后面各自分叉——这正是多轮对话（历史相同、新问题不同）与系统提示词复用的真实形态。

**练习 2**：第 5 块内容变了，第 6 块内容没变，第 6 块的哈希会变吗？
**答案**：会。\(h_6\) 的输入里包含 \(h_5\)（parent fragment），父变了链上之后全部改变。这保证了「分叉之后绝不误共享」。

**练习 3**：`all_sequence_hashes` 为什么不含尾部半块？
**答案**：半块意味着这部分 token 还在增长（下一个请求可能续写不同内容），且半块的 KV 未封口、身份不稳定；只有封满的块才有确定的内容边界，这就是「只匹配整块」规则。

### 4.2 PrefixProbe：probe_prefix 与加盐链路

#### 4.2.1 概念说明

探测（probe）回答的问题是：「这个 prompt 的前缀，现在 GPU 上已经有多少块？」它**不创建请求、不预约调度**，只产出一个只读的 `PrefixProbe`。它的原始动机是 KV offload 的 CPU 层预取（u3-l1 详讲），但它的字段清晰地划分了四个容易混淆的「块数」口径：

- `gpu_hit_blocks`：GPU 上已经命中的连续前缀块数。
- `held_blocks`：探针实际用强引用**钉住**的块数（GPU 命中 + 之后从 CPU 层提交进来的块）。
- `reusable_blocks`：真正允许被请求复用的块数 = `min(held, cacheable)`。
- `cpu_query_window`：还需要去 CPU 层查询的块数 = `cacheable − gpu_hit`。

其中「可复用上限」服从本讲最重要的公式。设 prompt 长度为 \(n\)、块大小为 \(b\)：

\[
\text{cacheable} = \left\lfloor \frac{n-1}{b} \right\rfloor
\]

即**永远比「整条 prompt 能凑出的满块数」少留出至少一个 token**。若 \(n\) 恰为 \(b\) 的整数倍，则最后**一整块**都不可复用。

#### 4.2.2 核心流程

```text
probe_prefix(prompt, lora)
  └─ probe_prefix_with_cache_salt(prompt, cache_salt=None, lora)
       ├─ new_request_with_cache_salt(prompt, 0, cache_salt, lora)
       │     ├─ salt_hash = compute_salt_hash(cache_salt, lora)   ← 加盐点
       │     └─ SchedulableSequence::new(..., Some(salt_hash))    ← 链头混入
       ├─ seq_hashes = 该临时请求的 all_sequence_hashes()
       ├─ cacheable = (n - 1) / block_size                        ← 留最后一个 token
       └─ gpu_guard = block_manager.match_blocks(&seq_hashes)     ← 线性前缀匹配
  → PrefixProbe { seq_hashes, gpu_hit, cacheable, block_size, held: gpu_guard }
```

注意：公共入口 `probe_prefix` 固定 `cache_salt = None`，`cache_salt` 变体目前是 crate 私有（`fn` 而非 `pub fn`），供 MTP 等「同样 token 会因块外状态产生不同 KV」的场景在 crate 内部使用——对外公开的身份维度只有 `lora_name`。

#### 4.2.3 源码精读

加盐链路的公共入口与内部实现（[src/pool.rs:L148-L187](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L148-L187)）：

```rust
/// `lora_name` scopes the prefix cache: blocks registered under one
/// adapter (or the base model, `None`) never match a request running
/// under a different adapter — the name is folded into the block-hash
/// chain as a salt, so K/V computed with different weights can't be
/// silently reused.
pub fn new_request(&self, prompt_tokens: Vec<u32>, max_output_tokens: usize,
                   lora_name: Option<&str>) -> RequestKv {
    self.new_request_with_cache_salt(prompt_tokens, max_output_tokens, None, lora_name)
}

fn new_request_with_cache_salt(...) -> RequestKv {
    let salt_hash = compute_salt_hash(cache_salt, lora_name)
        .expect("compute_salt_hash is infallible for string cache/lora identities");
    let seq = SchedulableSequence::new(
        prompt_tokens, max_output_tokens, self.block_size as u32, None, Some(salt_hash));
    ...
}
```

这段代码做了什么：`new_request` 把 LoRA 名一路传进 `compute_salt_hash`，产出 `SaltHash` 后**无条件**以 `Some(salt_hash)` 喂给序列——即使两个身份都是 `None`，也会得到一个规范默认盐，所以基座模型的请求们共享同一个身份。`compute_salt_hash` 来自 `dynamo-kv-hashing`（[Cargo.toml:L10](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/Cargo.toml#L10)，上游按 git rev 钉版，见根 [Cargo.toml:L45](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/Cargo.toml#L45)）；其内部构造在上游仓库，本讲只需确认「两个可选字符串身份 → 一个盐」这一契约。

探测本体（[src/pool.rs:L207-L229](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L207-L229)）：

```rust
let num_input = prompt_tokens.len();
let rkv = self.new_request_with_cache_salt(prompt_tokens, 0, cache_salt, lora_name);
let seq_hashes = rkv.seq.inner().sequence().all_sequence_hashes();
// match_and_add_prefix leaves >=1 prompt token uncached, so a request
// can reuse at most this many leading blocks — the CPU load must not
// exceed it, or the trailing loaded block would never be re-matched.
let cacheable = num_input.saturating_sub(1) / self.block_size;
let gpu_guard = self.block_manager.match_blocks(&seq_hashes);
let gpu_hit = gpu_guard.len();
```

这段代码做了什么：临时请求只为拿哈希链；`cacheable` 直接按公式截断；`match_blocks` 拿到的强引用整批存进 `held`，钉住命中块在探测与真正 prefill 之间不被回收。

上游匹配算法（[kvbm/kvbm-logical/src/manager/mod.rs:L115-L157](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/kvbm/kvbm-logical/src/manager/mod.rs#L115-L157)）：

```rust
/// Linear prefix match: walks `seq_hash` left-to-right, stopping on
/// the first hash that hits neither the active nor the inactive pool.
pub fn match_blocks(&self, seq_hash: &[SequenceHash]) -> Vec<ImmutableBlock<T>> {
```

这段代码做了什么：从左到右线性走哈希链，**遇到第一个未命中的哈希立即停**——前缀匹配天然要求连续；活跃池和非活跃池都算命中，命中即持有强引用。

四个口径的访问器集中在 `PrefixProbe`（[src/pool.rs:L276-L339](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L276-L339)），其中 `reusable_blocks` 与 `cpu_query_hashes` 显式受 cacheable 封顶：

```rust
pub fn reusable_blocks(&self) -> usize { self.held.len().min(self.cacheable) }

pub fn cpu_query_hashes(&self) -> Vec<Vec<u8>> {
    if self.gpu_hit >= self.cacheable { return Vec::new(); }
    self.seq_hashes[self.gpu_hit..self.cacheable] ...
}
```

这段代码做了什么：即使 `held` 比 `cacheable` 多（比如 GPU 命中盖过了整个可复用窗口），复用上限仍被 final-token 规则钳住；GPU 命中已覆盖全部可复用块时，CPU 查询集为空（无事可做，正常 prefill 即可）。

#### 4.2.4 代码实践

1. **实践目标**：用内联测试 `probe_boundary_hash_obeys_reusable_cap_and_final_token_rule` 把四个口径手算一遍。
2. **操作步骤**：阅读 [src/pool.rs:L866-L886](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L866-L886)。场景：prompt 恰好 64 token（4 个满块），种子请求把 4 块全部注册并释放。先自己填表再运行验证：

   | 量 | 公式 | 你的预测 |
   |---|---|---|
   | `gpu_hit_blocks` | 4 块全部命中 | ? |
   | `cacheable` | (64−1)/16 = 3 | ? |
   | `reusable_blocks` | min(4, 3) | ? |
   | `boundary_hash(48)` / `boundary_hash(64)` | 48 是 3 块边界 / 64 > 3 块 | ? |

   ```bash
   cargo test --release -p pegainfer-kv-cache --lib probe_boundary -- --nocapture
   ```

3. **需要观察的现象**：测试通过；特别注意 `gpu_hit_blocks() == 4` 而 `reusable_blocks() == 3`——**GPU 命中可以超过复用上限，上限永远赢**。
4. **预期结果**：`boundary_hash(0)`、`boundary_hash(47)`（非块对齐）、`boundary_hash(64)`（超上限）均为 `None`，`boundary_hash(48)` 为 `Some`。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `cpu_query_window` 用 `cacheable − gpu_hit` 而不是「总块数 − gpu_hit」？
**答案**：CPU 层只该加载「将来会被复用」的块。超过 cacheable 的块即使加载进来，`reusable_blocks` 也把它们钳掉，最后一块永远不会被重新匹配——白搬显存（源码注释原话："the CPU load must not exceed it, or the trailing loaded block would never be re-matched"）。

**练习 2**：探针为什么要把命中块用强引用钉住（`held` 字段）？
**答案**：探测与请求真正 prefill 之间存在时间窗（CPU 层加载、排队），期间别的请求分配块可能触发 LRU 回收；强引用（`ImmutableBlock`）把这些块保持在活跃池，物理页不会被改写。

**练习 3**：`new_request_with_cache_salt` 为什么总是传 `Some(salt_hash)`，即便调用方两个身份都给了 `None`？
**答案**：让「无盐」也成为一个确定身份：所有基座模型请求共享同一个规范默认盐，彼此之间正常共享前缀；同时代码路径统一，不存在「有盐/无盐」两套哈希推导。

### 4.3 match_and_add_prefix：挂前缀与「永远留最后一个 token」

#### 4.3.1 概念说明

匹配是前缀缓存的**消费**动作：在请求第一次 `schedule_prefill` 之前调用一次，把命中的整块直接挂进该请求的块指派表，返回可跳过的 token 数，`kv_position` 同步前移——之后 prefill 只需转发剩余后缀。「永远留最后一个 token」在 kvbm 上游被强制执行，理由有二（[kvbm/kvbm-logical/src/integrations/scheduled.rs:L428-L441](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/kvbm/kvbm-logical/src/integrations/scheduled.rs#L428-L441) 的注释原文）：

1. **采样需要**：最后一段 prefill 必须跑，才能用最后一个位置的 logits 采样出**第一个生成 token**（全命中会导致无 forward 可做）。
2. **状态机合法性**：全命中会让调度状态机「prefill 完成、悬空 token 数为 0」——没有合法的下一步，直接死锁。

qwen3 的实践文档还注明 vLLM 采用同样的规则（[docs/models/qwen3/prefix-cache.md:L9-L16](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/docs/models/qwen3/prefix-cache.md#L9-L16)）。

#### 4.3.2 核心流程

```text
fresh RequestKv（尚未 schedule_prefill）
  └─ match_and_add_prefix(pool)            ← 公共入口
       └─ match_and_add_prefix_up_to(pool, usize::MAX)
            └─ kvbm SchedulableSequence::match_and_add_prefix_up_to(manager, max)
                 ├─ max_blocks = min(⌊(n−1)/b⌋, requested_max)   ← 双重上限
                 ├─ 沿哈希链匹配、把命中块挂进 assignments
                 └─ prefill_position += count·b;  kv_position = prefill_position
  返回 count × block_size（token 数）
之后：schedule_prefill 只需覆盖剩余后缀；chunk_start 直接取 kv_position
```

#### 4.3.3 源码精读

本 crate 的薄包装（[src/pool.rs:L393-L423](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L393-L423)）：

```rust
/// Match the prompt's full blocks against registered blocks and skip
/// their prefill. Returns the number of cached tokens; `kv_position()`
/// advances by the same amount. Must be called on a fresh request,
/// before the first `schedule_prefill`.
///
/// Matching always leaves at least one prompt token uncached so the
/// final prefill chunk can emit the first generated token.
pub fn match_and_add_prefix(&mut self, pool: &BlockPool) -> anyhow::Result<usize> {
    self.match_and_add_prefix_up_to(pool, usize::MAX)
}
```

`match_and_add_prefix_up_to` 的包装体还做一件事：把事件光标 `emitted_blocks` 直接设到当前已指派块数——前缀命中的块**不再**向 KV 路由发 store 事件，因为最先封存它们的请求已经发过（[src/pool.rs:L409-L423](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L409-L423)，事件流本身是 u3-l2 的主题）。返回值以 token 计：`blocks * self.seq.block_size()`。

上游的强制上限与位置前移（[kvbm/kvbm-logical/src/integrations/scheduled.rs:L443-L472](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/kvbm/kvbm-logical/src/integrations/scheduled.rs#L443-L472)）：

```rust
let bs = self.inner.block_size();
let max_blocks =
    (self.inner.num_input_tokens().saturating_sub(1) / bs).min(requested_max_blocks);
let count = self.inner.match_and_add_prefix(manager, max_blocks)
    .unwrap_or_else(|_| panic!("prefix match should not produce duplicates"));
if count > 0 {
    self.prefill_position += count * self.inner.block_size();
    self.kv_position = self.prefill_position;
}
```

这段代码做了什么：即使调用方传 `usize::MAX`，kvbm 仍按 ⌊(n−1)/b⌋ 截断——**final-token 规则在机制层强制，不依赖调用方自觉**；匹配后 `kv_position` 与 `prefill_position` 一起跳到命中块覆盖的末尾。

`up_to` 变体的存在理由（同文件注释）：混合注意力模型的辅助状态可能只能在**短于最长 KV 命中**的边界上恢复，所以允许调用方主动选一个更短的精确边界。配套查询 `prefix_matched_blocks`（[src/pool.rs:L683-L686](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L683-L686)）与 `registered_boundary_hash`（[src/pool.rs:L425-L446](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L425-L446)）让调用方核对「实际挂上了几块、边界哈希是否与探测器一致」。

消费侧真实调用点（[pegainfer-qwen3/src/executor.rs:L1885-L1929](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L1885-L1929)）：

```rust
let matched = rkv.match_and_add_prefix(self.kv_mgr.pool())?;
req.cached_tokens = Some(matched);
...
req.chunk_start = rkv.kv_position();
```

这段代码做了什么：qwen3 executor 在请求的第一个 prefill 分块前匹配，把跳过量记到 `cached_tokens`，后续分块起点直接取 `kv_position`。注释还说明 prompt logprobs 请求会**跳过匹配**——打分需要每个位置的 logits，缓存位置不转发。

#### 4.3.4 代码实践

1. **实践目标**：跑通本讲的核心例证 `lora_salt_isolates_prefix_cache`，看清同名命中、异名全 miss。
2. **操作步骤**：

   ```bash
   # 需要 CUDA（经 KvCacheManager 建真实显存缓冲）
   cargo test --release -p pegainfer-kv-cache --test lifecycle lora_salt -- --nocapture
   ```

   无 GPU 环境时的等价纯逻辑路径：运行 crate 内联测试（只用 `BlockPool`，无需 CUDA），它验证同一机制，只是把身份从 LoRA 名换成了 cache_salt：

   ```bash
   cargo test --release -p pegainfer-kv-cache --lib cache_salt_isolates -- --nocapture
   ```

3. **需要观察的现象**：两组测试均通过；对照 [tests/lifecycle.rs:L237-L270](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/lifecycle.rs#L237-L270) 与 [src/pool.rs:L791-L835](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L791-L835) 的断言逐条核对。
4. **预期结果**：48-token prompt（3 个满块）注册释放后——基座（`None`）匹配 0；适配器 "b" 匹配 0；适配器 "a" 再次请求匹配 32（2 块，被 final-token 规则钳掉 1 块）。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：prompt 长度 32、block_size 16，两块都被之前请求注册过。`match_and_add_prefix` 返回多少？为什么不是 32？
**答案**：返回 16。cacheable = ⌊(32−1)/16⌋ = 1，最后一块整块留下：最后一段 prefill 必须至少转发 1 个 token 才能采出第一个生成 token，且状态机不允许「prefill 完成但零悬空 token」。

**练习 2**：`match_and_add_prefix_up_to(pool, 2)` 在一个 5 块命中、cacheable 为 3 的请求上返回什么？
**答案**：2 块对应的 token 数。上限是 min(⌊(n−1)/b⌋, requested_max)，主动上限与机制上限取更小者；匹配后 `kv_position` 只前进 2 块，剩余部分照常 prefill（内联测试 `probe_and_attach_can_select_a_shorter_exact_boundary` 完整演示了这个场景，[src/pool.rs:L837-L864](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L837-L864)）。

**练习 3**：为什么 `match_and_add_prefix` 必须在第一次 `schedule_prefill` **之前**调用？
**答案**：kvbm 侧 `require_idle()` 强制匹配只能在 Idle 状态发生；调度一旦开始，块指派表与位置游标已被预填流程占用，再挂前缀会破坏「悬空 token 计数」与页表的一致性。

### 4.4 回收：evict_inactive、active_prefix_hashes 与 LRU 后端

#### 4.4.1 概念说明

缓存必须有回收，否则非活跃池无限膨胀。本 crate 的回收面有三个部件：

- `BlockPool::evict_inactive`：**一次性清空**非活跃池（冷缓存冲刷）。使用中的块（活跃池）不受影响。它也是 offload 路径上「把前缀赶出 HBM、逼下一个请求从 CPU 层恢复」的手段。
- `BlockPool::active_prefix_hashes`：只查**活跃池**前缀，不晋升非活跃条目、不钉住块——一个「看一眼还活着多长前缀」的只读探照灯。
- 上游 LRU 非活跃索引：块释放 → 进 LRU；新分配缺块 → 从 LRU 最旧端弹出复用；前缀命中 → 从索引中弹出（复活为活跃）。

#### 4.4.2 核心流程

```text
请求 release()
  └─ 注册过的完整块 → 非活跃池（LRU 索引: SequenceHash → BlockId）
       ├─ 后续请求 match_blocks 命中 → 从 LRU 弹出，挂为新请求的活跃块（复活）
       ├─ allocate_blocks 需要块 → pop_lru，最旧条目回空闲池（隐性回收）
       └─ evict_inactive() → 全部倒回空闲池（显式冲刷）
```

LRU 的排序口径值得注意：模块自述为 "least-recently-**inserted** block evicted first"（[kvbm/kvbm-logical/src/pools/inactive/backends/lru_backend.rs:L1-L27](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/kvbm/kvbm-logical/src/pools/inactive/backends/lru_backend.rs#L1-L27)）——因为命中即弹出（复活），留在索引里的条目从未被再次触及，插入序就是有效的年龄序。

#### 4.4.3 源码精读

本 crate 的两个回收面入口（[src/pool.rs:L125-L146](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L125-L146)）：

```rust
/// Evict every cached-but-unused block from the GPU prefix cache (drain the
/// inactive pool). In-use blocks are untouched. A cold-cache flush — and,
/// for the offload path, the way to force a prefix out of HBM so a
/// subsequent request must restore it from the CPU tier.
pub fn evict_inactive(&self) {
    self.block_manager.evict_inactive();
}

/// Return active prompt-prefix hashes without promoting inactive entries or
/// keeping the matched blocks alive.
pub fn active_prefix_hashes(&self, prompt_tokens: Vec<u32>, lora_name: Option<&str>)
    -> Vec<[u8; 16]>
{
    let num_input = prompt_tokens.len();
    let request = self.new_request(prompt_tokens, 0, lora_name);
    let hashes = request.seq.inner().sequence().all_sequence_hashes();
    let cacheable = num_input.saturating_sub(1) / self.block_size;
    let active = self.block_manager.active_prefix_len(&hashes).min(cacheable);
    ...
}
```

这段代码做了什么：`evict_inactive` 是显式冲刷；`active_prefix_hashes` 复用同一条加盐哈希链，但走 `active_prefix_len`——只统计仍被强引用持有的连续前缀长度，且同样被 cacheable 封顶（salt 维度一致，跨身份查询自然 miss）。

上游的两副面孔（[kvbm/kvbm-logical/src/manager/mod.rs:L87-L95](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/kvbm/kvbm-logical/src/manager/mod.rs#L87-L95) 与 [kvbm/kvbm-logical/src/pools/inactive/backends/lru_backend.rs:L65-L86](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/kvbm/kvbm-logical/src/pools/inactive/backends/lru_backend.rs#L65-L86)）：

```rust
// manager: drain 非活跃池，活跃块（含 padding 预留）不受影响
pub fn evict_inactive(&self) {
    drop(self.store.drain_inactive_to_mutable());
}

// LRU 后端: 分配缺块时从最旧端弹出（隐性回收）
fn allocate(&mut self, count: usize) -> Vec<(SequenceHash, BlockId)> {
    for _ in 0..count {
        if let Some(entry) = self.cache.pop_lru() { allocated.push(entry); } ...
}

fn insert(&mut self, seq_hash: SequenceHash, block_id: BlockId) {
    assert!(self.cache.len() < self.cache.cap().get(),
        "LRU backend insert would cause eviction! ...");
```

这段代码做了什么：回收有两条路——**显式** `evict_inactive` 全量冲刷、**隐性** `allocate` 从 LRU 最旧端取；`insert` 的断言表明 LRU 容量按「能装下全部块」配置，正常路径绝不静默丢条目。

消费侧的一个真实用法（[pegainfer-qwen3/src/executor.rs:L2505-L2521](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L2505-L2521)）：纯 L2 模式下，qwen3 在探测前先 `evict_inactive()` 清掉跨请求的 HBM 留存，让 `probe_prefix` 看到 `gpu_hit == 0`、整段可复用前缀都去 CPU 层查询——evict 在这里是「测量开关」而不是内存压力应对。

#### 4.4.4 代码实践

1. **实践目标**：验证「release 之后前缀进非活跃池：`active_prefix_hashes` 变空，但匹配仍可命中」的活跃/非活跃分野。
2. **操作步骤**：阅读并运行内联测试 `active_prefix_hashes_exclude_reclaimable_entries`（[src/pool.rs:L778-L789](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L778-L789)，纯逻辑无 GPU）：

   ```bash
   cargo test --release -p pegainfer-kv-cache --lib active_prefix_hashes -- --nocapture
   ```

   场景：40-token prompt，prefill 32 token（2 块）后——`active_prefix_hashes` 长度为 2（块仍被请求持有）；`release()` 之后——立刻变空。
3. **需要观察的现象**：两段断言先后通过：持有期 2、释放后 0。
4. **预期结果**：如上。随后思考：此时若 `match_and_add_prefix` 同 prompt 新请求，仍能命中 2 块（块在**非活跃池**里，`match_blocks` 同时查两个池）——这正是 4.3 实践里 `lora_salt_isolates_prefix_cache` 的前半段铺垫。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`evict_inactive` 会不会误伤正在服务的请求？padding 块会不会被清掉？
**答案**：都不会。它只 drain 非活跃池；在用块由请求的强引用保持在活跃池。padding 块被 `std::mem::forget` 的 RAII 把手钉死在活跃池（u2-l1），天然免疫。

**练习 2**：非活跃池里的块什么时候被**隐性**回收？
**答案**：新请求分配块而空闲池不足时，`allocate` 从 LRU 最旧端弹出条目归还复用——最老的缓存前缀最先牺牲，无需任何显式调用。

**练习 3**：`active_prefix_hashes` 与 `probe_prefix` 都走同一条哈希链，差别在哪？
**答案**：`probe_prefix` 经 `match_blocks` 拿强引用、会复活非活跃命中、还给 offload 提供查询窗口；`active_prefix_hashes` 走 `active_prefix_len`，只统计活跃前缀、不晋升不钉住，适合「只看不动」的观测场景。

## 5. 综合实践

把本讲三个机制（加盐哈希链、匹配挂前缀、非活跃池复活）串成一个完整剧本——这就是规格指定的核心实践：**复现 `lora_salt_isolates_prefix_cache` 的场景并补全状态追踪**。

**任务设定**：`KvCacheManager` 以 32 块、block_size=16 建池（1 块 padding → 31 可用）。prompt 为 48 个 `7`（3 个满块）。

**步骤**（参照 [tests/lifecycle.rs:L237-L270](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/lifecycle.rs#L237-L270) 逐行跟踪，先填表再跑测试）：

| 步骤 | 操作 | 预期 `available_blocks()` | 预期匹配结果 |
|---|---|---|---|
| 1 | 请求 a（`Some("a")`）`schedule_prefill(48)` + `apply_prefill(42)` | 31 → 28（3 块被占） | — |
| 2 | `a.release()` | 28 → 31（3 块进**非活跃池**，不回空闲池也计为可用） | — |
| 3 | 基座请求（`None`）`match_and_add_prefix` | 31 | **0**：salt 不同，哈希链从第 0 块就不同 |
| 4 | 请求 b（`Some("b")`）`match_and_add_prefix` | 31 | **0**：同上 |
| 5 | 请求 a2（`Some("a")`）`match_and_add_prefix` | 31（2 块从非活跃池复活为活跃） | **32**：3 块全部哈希命中，但 cacheable = ⌊47/16⌋ = 2 钳掉 1 块 |

验证命令（需 CUDA）：

```bash
cargo test --release -p pegainfer-kv-cache --test lifecycle lora_salt -- --nocapture
```

无 GPU 时用等价的纯逻辑内联测试覆盖同一机制（cache_salt 维度）：`cargo test --release -p pegainfer-kv-cache --lib cache_salt_isolates -- --nocapture`（[src/pool.rs:L791-L835](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L791-L835)，其中还能看到探测路径同样被隔离：错误 salt 的 `probe_prefix` 得 `gpu_hit_blocks() == 0`）。断言数值待本地验证。

**最后回答规格的问题：如果去掉 salt，两个不同 LoRA 适配器会出什么正确性事故？**

块哈希将只由 token 内容、位置与父链决定——适配器 a 注册的块与适配器 b 的请求算出**完全相同的键**。于是 b 的 `match_and_add_prefix` 会命中 a 算出的块，跳过这段 prefill，注意力内核随后拿着**用 a 的权重算出的 K/V** 去和 **b 的权重算出的 Q** 做点积。这不是崩溃，而是**静默的输出污染**：分布悄悄漂移，没有任何断言或错误会触发（源码注释的措辞正是 "K/V computed with different weights can't be **silently** reused"——salt 的作用就是让这种复用在键空间里直接不可能，而不是在挂载时检查）。反向同样成立：基座请求会误吃 LoRA 块、LoRA 请求会误吃基座块。由于同一条哈希链还被 offload 的 CPU 层查询与路由事件复用，隔离在键推导处做一次，所有层自动生效。

## 6. 本讲小结

- 前缀缓存的键是 **lineage 哈希链**：位置 + 块内容 + 父块哈希 + salt，共享前缀逐位相同、分叉即全变；尾部半块永不参与（只匹配整块）。
- **永远留最后一个 token**：cacheable = ⌊(n−1)/b⌋ 在 kvbm 机制层强制，既保证最后一段 prefill 能采出第一个生成 token，也避免状态机死锁；`reusable_blocks`、CPU 查询窗口、`active_prefix_hashes` 全部服从它。
- **salt 从链头混入**：`new_request` 的 LoRA 名经 `compute_salt_hash` 折进每个块的哈希，同 token 异身份的请求互相不可见——隔离发生在键空间，GPU 池、offload 查询、事件流一层生效。
- `probe_prefix` 产出的 `PrefixProbe` 区分四个口径：`gpu_hit_blocks`（GPU 命中）、`held_blocks`（强引用钉住的）、`reusable_blocks`（min(held, cacheable)）、`cpu_query_window`（还需查 CPU 层的）。
- `match_and_add_prefix` 把命中块挂进请求指派表、`kv_position` 前移命中 token 数，且必须在首次 `schedule_prefill` 之前调用；`up_to` 变体允许主动选更短边界。
- 回收双路：显式 `evict_inactive` 冷冲刷（qwen3 还用它制造纯 L2 模式），隐性 `allocate` 从 LRU 最旧端弹出；release 的注册块进非活跃池，命中即复活。

## 7. 下一步学习建议

本讲只讲了 GPU 池内的命中与回收；`PrefixProbe` 的另一半——`cpu_query_hashes` → `reserve_loaded_blocks` → `commit_loaded_blocks` 的 **CPU 层恢复链路**——是下一讲 u3-l1（恢复-提交：reserve_loaded_blocks 与 KV offload 桥）的主题，那里还会解释 `boundary_hash` 的 lineage 语义与 #704 性能回归锚。建议顺带阅读：

- [kvbm/kvbm-logical/src/integrations/scheduled.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/kvbm/kvbm-logical/src/integrations/scheduled.rs#L428-L472) 的前缀匹配一节，对照本讲 4.3。
- [docs/models/qwen3/prefix-cache.md](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/docs/models/qwen3/prefix-cache.md#L1-L17)，看引擎层如何把这些原语接到执行器与准确度门禁上。
- 若想看「按引用计数而非物理块计数」的共享前缀陷阱（#403），读同文档的 Pitfalls 一节——它是前缀缓存落地时最容易踩的容量核算坑。
