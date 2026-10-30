# RequestKv：一个请求的 KV 生命周期

## 1. 本讲目标

上一讲（u2-l1）我们认识了 `BlockPool`——它把物理页的发放、共享与回收管了起来，但一直没回答：**一个具体请求的 KV 到底是怎么一步步走完一生的？** 本讲就沿着 `RequestKv` 这条主线，把答案补全。学完后你应该能够：

1. 完整复述一个请求从 `new_request` 到 `release` 的 KV 状态变迁：预填（prefill）→ 解码（decode）循环 → 释放，以及投机解码（speculative）这条变体路径。
2. 解释 **schedule（调度）与 apply（应用）两阶段分离** 的设计：为什么「预约块」和「提交结果」必须是两个动作，以及它带来的失败恢复能力（`revert_schedule`、drop 守卫）。
3. 理解视图族的三条不变式：`decode_view` 的 `seq_len == kv_position + 1`、`prefill_view` 覆盖目标长度、页表行必须**精确覆盖**序列长度。
4. 熟练使用状态查询族（`kv_position` / `generated_tokens` / `is_complete` / `resident_blocks`）推断任意时刻的池容量变化。

## 2. 前置知识

本讲假设你已读过 u1-l3（页几何）与 u2-l1（BlockPool 与 padding 块）。在此之上，只需要再补三个推理引擎的常识：

- **预填（prefill）**：把 prompt 的所有 token 一次（或分块）喂进模型，计算出它们的 K/V 并写入缓存。这是「读题」阶段，计算量大但只做一次。
- **解码（decode）**：预填产出第一个生成 token 之后，每一步只把**一个**新 token 喂进模型，算出它的 K/V，再采样出下一个 token。这是「答题」阶段，一步一个 token。
- **悬空 token（dangling token）**：每次 forward 刚采样出来的那个 token，它的 K/V **还没有**被计算——它要等到下一步 forward 时才作为输入被算进缓存。`tests/lifecycle.rs` 的注释里有原话："token 42 is 'dangling' (not yet in KV)"。理解「悬空」是理解 `decode_view` 为什么是 `kv_position + 1` 的关键。
- **投机解码（speculative decode，选读）**：先让一个小模型连猜 `k` 个候选 token，再让大模型一次 forward 同时验证这 `k` 个位置，接受猜对的前缀。KV 层面表现为「一次预约多个位置的块，但只提交被接受的部分」。

另外回顾两个上一讲的结论，本讲会反复用到：

- 池构造时预留 1 块做 CUDA Graph 的 padding 槽，所以 `available_blocks() == total_blocks() - 1`。
- `BlockPool` 只发放**块 ID**，不持有显存；「拿了几块」完全可以从 `available_blocks()` 的变化观察出来——这正是本讲实践任务的观察窗口。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [src/pool.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs) | 本讲主战场。`RequestKv` 结构体与调度族、视图族、提交族、查询族全部在这里（约 L373-L718） |
| [tests/lifecycle.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/lifecycle.rs) | 端到端生命周期测试。**需要真实 CUDA 设备**（`make_manager` 会创建 `CudaContext`） |
| [src/view.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/view.rs) | 视图族返回的 `KvView`：三字段轻量视图，构造时强校验页表契约 |
| [src/manager.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/manager.rs) | `KvCacheManager`：测试里经它拿 `pool()`。本讲只当门面用，深入留给 u2-l4 |
| [kvbm/kvbm-logical/src/integrations/scheduled.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/kvbm/kvbm-logical/src/integrations/scheduled.rs) | 上游地基。`SchedulableSequence`（真正的状态机）、`ScheduleError`、`DecodeOutcome` 都定义在这里；本 crate 的 `RequestKv` 是它的薄包装 |

一个先建立起来的总印象：`RequestKv` 自己几乎没有逻辑，它是 `SchedulableSequence<()>` 的委托层——每个方法一两行，转调 `self.seq.xxx(&pool.block_manager)`。**协议（什么时候该干什么）定义在本 crate 的方法签名与文档里，机制（状态机、块分配）住在上游 kvbm-logical。** 读本讲时两边的源码对照着看。

## 4. 核心概念与源码讲解

先给一张总路线图。`RequestKv` 的结构体文档把生命周期写成了一行协议（原文见 [src/pool.rs:L373-L378](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L373-L378)）：

```
new_request
    │
    ▼
schedule_prefill(n) → prefill_view(n) → [forward] → apply_prefill / apply_prefill_chunk
    │                                                            │
    │            ←── 失败：revert_schedule 撤销预约，或直接 drop ──┘
    ▼
┌─ 解码循环（每轮一个 token）──────────────────────────────────────┐
│   schedule_decode → decode_view → [forward] → apply_decode      │
│   （或投机支线：schedule_speculative(k) → speculative_view(k)    │
│     → [forward] → apply_speculative(accepted)；失败 revert）     │
└──────────────────────────── is_complete() ──────────────────────┘
    │
    ▼
release()（或直接 drop）→ 块回到池
```

每个 forward 步骤内部都遵循同一个三拍子节奏：**预约（schedule）→ 给内核看视图（view）→ 提交（apply）**。下面四个小节依次拆解这三个拍子加一个「观察窗口」。

### 4.1 调度族：schedule_prefill / schedule_decode / schedule_speculative 与 ScheduleError

#### 4.1.1 概念说明

「调度」= **为这一步 forward 要写入的 KV 预约物理块**。它只动块、不动状态：预约成功后 `kv_position` 不变、没有任何 token 被认定已算完——计算要等 forward 真正跑完，由 apply 来承认。

为什么要单独一拍「预约」？因为块是稀缺资源，必须**先谈好再干活**：

- 准入判断需要它：池不够时这一步直接失败，GPU 白跑之前就能拒绝（测试 `exhaust_capacity_returns_error` 演示的正是这个）。
- 失败恢复需要它：forward 可能在预约之后、提交之前失败（CUDA 错误、请求被取消）。预约与提交分离后，「已预约未提交」是一个明确的状态，可以精确撤销（`revert_schedule`），甚至什么都不做、直接 drop 也能靠析构守卫把块还回去。

三个调度入口对应三种 forward 形状：

| 入口 | 一步写入的 KV | 典型预约量 |
|------|--------------|-----------|
| `schedule_prefill(num_tokens)` | 预填的一 chunk（可达上千 token） | \(\lceil \text{num\_tokens} / \text{block\_size} \rceil\) 块 |
| `schedule_decode` | 1 个 token（当前的悬空 token） | 通常 0 块（塞进现有块）；恰好填满最后一块时会**多**预约下一块 |
| `schedule_speculative(k)` | k 个连续位置（悬空 token + 候选草稿） | 覆盖这 k 个位置所需的块 |

#### 4.1.2 核心流程

```
schedule_*(参数, pool):
    1. 检查请求状态机是否允许（必须处于 Idle/可调度态）
    2. 计算本步新增 KV 需要多少块
    3. 向 block_manager 申请块
       ├─ 成功：请求进入「已预约未提交」态，返回 Ok(())
       └─ 不够：返回 Err(ScheduleError::AllocationFailed { needed })
                  ——此时一块都没拿到，请求什么也不持有
```

失败分类由上游 `ScheduleError` 承载，共七种（见 4.1.3 的表）。

#### 4.1.3 源码精读

三个调度方法本体极薄，全部转调上游（[src/pool.rs:L450-L473](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L450-L473)）：

```rust
pub fn schedule_prefill(
    &mut self,
    num_tokens: usize,
    pool: &BlockPool,
) -> Result<(), ScheduleError> {
    self.seq.schedule_prefill(num_tokens, &pool.block_manager)
}

pub fn schedule_decode(&mut self, pool: &BlockPool) -> Result<(), ScheduleError> {
    self.seq.schedule_decode(&pool.block_manager)
}

pub fn schedule_speculative(
    &mut self,
    num_draft_tokens: usize,
    pool: &BlockPool,
) -> Result<(), ScheduleError> {
    self.seq
        .schedule_speculative(num_draft_tokens, &pool.block_manager)
}
```

这段代码做的事：把「本步要写多少 token 的 KV」连同池的底层 `block_manager` 交给上游 `SchedulableSequence`，由它完成状态检查与块申请。注意三类调度的语义差别写在文档注释里——`schedule_speculative` 的注释明确「覆盖 `num_draft_tokens` 个连续位置（当前悬空 token + 候选草稿），失败用 `revert_schedule` 归还」。

失败类型全部定义在上游（[kvbm/kvbm-logical/src/integrations/scheduled.rs:L307-L331](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/kvbm/kvbm-logical/src/integrations/scheduled.rs#L307-L331)）：

| 变体 | 含义 |
|------|------|
| `NotIdle { state }` | 状态机不在可调度态（比如已经预约了一步还没 apply，又预约下一步） |
| `PrefillOverrun { .. }` | 这一步预填会越过 prompt 末尾（position + num_tokens > 输入长度） |
| `PrefillComplete` | 预填已完成，不允许再 schedule_prefill |
| `PrefillNotComplete { .. }` | 预填还没完成，不允许进入 decode |
| `AllocationFailed { needed }` | **池满**：需要 `needed` 块但空闲不足 |
| `GenerationComplete { .. }` | 生成已达上限，无事可调度 |
| `WrongDanglingCount { .. }` | 悬空 token 数与该步要求不符 |

容量耗尽的行为由集成测试钉死（[tests/lifecycle.rs:L144-L169](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/lifecycle.rs#L144-L169)）：4 块的池（可用 3），三个 16-token prompt 的请求各占 1 块后 `available_blocks() == 0`，第四个请求 `schedule_prefill(16)` 返回 `Err`；注意测试**从不 release 这个失败请求**，最后可用块数照样回到 3——说明失败的调度一块都没拿到、不持有任何资源，drop 它无需善后。

还有一个关键细节：**调度是按步渐进的，不是一次锁死整个请求的终身需求**。`multiple_requests_share_capacity`（[tests/lifecycle.rs:L52-L79](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/lifecycle.rs#L52-L79)）里请求 A 是「16-token prompt + 最多 16 个输出」，测试注释算出它终身需要 \(\lceil 31/16 \rceil = 2\) 块（16 个 prompt token + 15 个会真正算 KV 的生成 token；最后一个生成 token 完成请求即停，永远悬空、不需要 KV）。但 `schedule_prefill(16)` 此刻只拿走 1 块——第二块要等到解码阶段按需补充。这让多个请求能交错共享容量。

#### 4.1.4 代码实践

1. **实践目标**：不看测试答案，独自推演「容量耗尽」场景中每一步的可用块数。
2. **操作步骤**：
   - 只读 [tests/lifecycle.rs:L144-L169](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/lifecycle.rs#L144-L169)，在纸上写一列 `available_blocks()` 的期望值：初始、每个请求 schedule 后、每个请求 apply 后、三个全放完后、第四个请求调度失败后。
   - 再写一个**无需 GPU** 的纯逻辑版本（`BlockPool` 不碰 CUDA，pool.rs 的内联测试就是这么用的；下面是**示例代码**，可存为你自己克隆里的 `tests/schedule_rehearsal.rs`）：

   ```rust
   use pegainfer_kv_cache::BlockPool;

   #[test]
   fn schedule_failure_holds_no_blocks() {
       let pool = BlockPool::new(16, 4).unwrap(); // 4 块，1 块 padding
       assert_eq!(pool.available_blocks(), 3);

       let mut held: Vec<_> = (0..3).map(|_| {
           let mut r = pool.new_request(vec![1; 16], 1, None);
           r.schedule_prefill(16, &pool).expect("schedule");
           r.apply_prefill(42, &pool).expect("apply");
           r
       }).collect();
       assert_eq!(pool.available_blocks(), 0);

       let mut overflow = pool.new_request(vec![1; 16], 1, None);
       assert!(overflow.schedule_prefill(16, &pool).is_err());

       for r in &mut held { r.release().unwrap(); }
       assert_eq!(pool.available_blocks(), 3);
   }
   ```

3. **需要观察的现象**：`schedule_prefill` 失败后不 release `overflow`，最终可用块数仍是 3。
4. **预期结果**：按上面的推导应全部通过（本机未运行，**待本地验证**；纯逻辑测试不需要 CUDA 设备）。

#### 4.1.5 小练习与答案

**练习 1**：对一个 40-token 的 prompt 调 `schedule_prefill(40, pool)`（`block_size = 16`），至少要预约几块？为什么？

答案：\(\lceil 40/16 \rceil = 3\) 块。预填一步要为这 40 个 token 的 KV 全部落位，页是最小分配单位，所以按页数向上取整。

**练习 2**：为什么 `new_request` 时不把请求终身需要的块一次性全部锁定？

答案：因为解码阶段的 KV 需求是逐步显现的（`schedule_decode` 按步拿块，甚至在 token 填满当前块的哪一步才多拿一块——见 4.2 的「积极分配」）。提前锁死会让长请求霸占大量暂时空置的块，`multiple_requests_share_capacity` 里 A、B 两个请求就无法共存了。渐进预约 = 更高并发。

**练习 3**：连续调用两次 `schedule_decode`（中间没有 apply）会发生什么？

答案：从 `ScheduleError` 的定义看应当返回 `NotIdle { state }`——上一步已预约未提交，状态机不在可调度态（[kvbm/kvbm-logical/src/integrations/scheduled.rs:L310-L311](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/kvbm/kvbm-logical/src/integrations/scheduled.rs#L310-L311)）。具体报错文本**待本地验证**（可在纯逻辑测试里断言 `matches!(err, ScheduleError::NotIdle { .. })`）。

### 4.2 视图族：prefill_view / decode_view / speculative_view

#### 4.2.1 概念说明

预约好了块，forward 之前还要回答注意力内核的两个问题：**这个请求的 KV 分布在哪几页？序列现在多长？** 这就是视图族的职责——把事务层（块、位置）翻译成内核只读的 `KvView`（三个字段：页表行、`seq_len`、`page_size`，见 [src/view.rs:L12-L16](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/view.rs#L12-L16)）。

两个不变式是这一族的灵魂：

1. **seq_len 是「本步之后」的长度**。视图描述的不是当前状态，而是「这一步 forward 写入完成后，内核将看到的 KV 全貌」。文档原话：*"The view's seq_len = kv_position + prompt_len (post-advance state that FlashInfer attention metadata expects)"*（[src/pool.rs:L480-L483](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L480-L483)）。于是：
   - `decode_view()`：\( \text{seq\_len} = \text{kv\_position} + 1 \)（现有 KV + 悬空 token 这一步将要写入的那条）；
   - `prefill_view(prompt_len)`：\( \text{seq\_len} = \text{kv\_position} + \text{prompt\_len} \)——**覆盖目标长度**，支持从非零位置续算的分块预填；
   - `speculative_view(k)`：\( \text{seq\_len} = \text{kv\_position} + k \)。
2. **页表行必须精确覆盖 seq_len**。内核按 \( \text{seq\_len} = (\text{num\_pages}-1) \times \text{page\_size} + \text{last\_page\_len} \) 反推长度：多给一页，内核会**多读一段序列之外的垃圾 K/V**；少给一页则越界（`KvView::new` 的断言注释原文引用了 issue #291，[src/view.rs:L19-L27](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/view.rs#L19-L27)）。

精确性之所以要专门 defended，是因为**原始块列表不可信**：kvbm 的 `schedule_decode` 有「积极分配」行为——当本步 token 恰好填满当前最后一块时，会顺手把下一块也分配了。于是 `page_indices()`（原始持有列表）可以比实际 KV 需要多一块。直接把原始列表给内核 = 让内核看到更长的序列 = 读垃圾页。

#### 4.2.2 核心流程

```
prefill_view(prompt_len):
    target = kv_position + prompt_len
    pages  = step_page_indices(prompt_len)   # 截断到 ceil(target / block_size)
    KvView::new(pages, target, block_size)   # 构造断言：pages.len() == ceil(target / block_size)

step_page_indices(new_tokens):
    assert new_tokens > 0
    kv_tokens = kv_position + new_tokens
    原始 page_indices() 截断到 ceil(kv_tokens / block_size) 页   # 丢弃积极多配的那块
```

`decode_view` / `speculative_view` 同构，只是 `new_tokens` 固定为 1 或 `k`。

#### 4.2.3 源码精读

三个视图方法（[src/pool.rs:L477-L515](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L477-L515)）：

```rust
pub fn prefill_view(&self, prompt_len: usize) -> KvView {
    let target_seq_len = self.seq.kv_position() + prompt_len;
    KvView::new(
        self.step_page_indices(prompt_len),
        target_seq_len,
        self.seq.block_size(),
    )
}

pub fn decode_view(&self) -> KvView {
    KvView::new(
        self.step_page_indices(1),
        self.seq.kv_position() + 1,
        self.seq.block_size(),
    )
}
```

这段代码做的事：`seq_len` 一律取「当前 `kv_position` 加上本步新写入的 token 数」，页表行一律取 `step_page_indices`，绝不用原始块列表。

为什么绝不能用原始列表——`step_page_indices` 的文档注释讲得非常直白（[src/pool.rs:L618-L631](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L618-L631)）：

```rust
/// Page IDs covering exactly the KV tokens present after this step
/// appends `new_tokens` (`kv_position + new_tokens`). `page_indices()`
/// can hold one block more: kvbm's `schedule_decode` eagerly allocates
/// the next generation block whenever this step's token fills the last
/// slot of the current block. Page tables built from the raw list make
/// the kernel see a longer sequence than exists — use this for any
/// per-step page row handed to a forward pass.
pub fn step_page_indices(&self, new_tokens: usize) -> Vec<i32> {
    assert!(new_tokens > 0, "a forward step appends at least one token");
    let kv_tokens = self.seq.kv_position() + new_tokens;
    let mut pages = self.page_indices();
    pages.truncate(kv_tokens.div_ceil(self.seq.block_size()));
    pages
}
```

这段代码做的事：先按「现有 KV + 本步新增」算出目标 token 数，再把原始页表截断到 \(\lceil \text{kv\_tokens} / \text{block\_size} \rceil\) 页，把积极多配的块从内核视野里裁掉。

契约的最后防线在 `KvView::new` 的构造断言（[src/view.rs:L23-L34](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/view.rs#L23-L34)）：`assert_eq!(page_indices.len(), seq_len.div_ceil(page_size))`——页数对不上直接 panic，宁可崩溃也不让内核读错。配套的 `last_page_len`（[src/view.rs:L44-L51](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/view.rs#L44-L51)）有个容易踩的细节：`seq_len` 恰好整除 `page_size` 时最后一页是**满页**（返回 `page_size`），而不是 0。

两条不变式各有专门的测试钉住：

- `decode_view_seq_len_invariant`（[tests/lifecycle.rs:L194-L216](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/lifecycle.rs#L194-L216)）：解码循环里逐轮断言 `view.seq_len() == req.kv_position() + 1`。
- `prefill_view_covers_target_seq_len`（[tests/lifecycle.rs:L218-L235](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/lifecycle.rs#L218-L235)）：20-token 预填断言 `seq_len == 20`、`num_pages == 2`。
- `page_boundary_crossing`（[tests/lifecycle.rs:L81-L107](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/lifecycle.rs#L81-L107)）：16-token prompt 恰好填满第 0 页，第一次 decode 的视图 `seq_len == 17`、`num_pages == 2`、`last_page_len == 1`——**跨页瞬间**的三个数字全被点名。

#### 4.2.4 代码实践

1. **实践目标**：亲眼看到「积极分配」确实存在，且 `step_page_indices` 在每个块边界都把页表裁回精确值。
2. **操作步骤**：
   - 精读 pool.rs 的内联测试 `step_page_indices_exact_at_block_boundaries`（[src/pool.rs:L961-L1008](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L961-L1008)）：它扫了 10 种 prompt 长度 × 23 个解码步，在每一步断言视图页数恰为 \(\lceil \text{kv} / 16 \rceil\)，并数着原始列表超出的次数（`raw_overshoots`），最后断言**确实发生过超出**。
   - 运行它（构造的只有 `BlockPool`，无需 GPU）：
     ```bash
     cargo test --release -p pegainfer-kv-cache --lib step_page_indices_exact_at_block_boundaries -- --nocapture
     ```
3. **需要观察的现象**：测试通过；`raw_overshoots > 0` 的断言说明原始页表确实周期性多一块。
4. **预期结果**：通过（**待本地验证**）。顺带思考：如果哪天 kvbm 不再积极分配，这个测试的最后一个断言会失败——它同时是「可以退役裁剪逻辑」的哨兵。

#### 4.2.5 小练习与答案

**练习 1**：`seq_len = 17`、`page_size = 16`，求 `num_pages`、`last_page_len`，并验证内核反推公式。

答案：`num_pages = 2`（\(\lceil 17/16 \rceil\)），`last_page_len = 1`；反推 \((2-1) \times 16 + 1 = 17\) ✓。

**练习 2**：`seq_len = 32`、`page_size = 16` 时 `last_page_len` 是多少？

答案：**16 而不是 0**。`last_page_len` 的实现里 `rem == 0` 时返回 `page_size`（[src/view.rs:L44-L51](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/view.rs#L44-L51)）——最后一页是满页；若返回 0，内核会把序列长度算成 16，静默丢一半上下文。

**练习 3**：把 `page_indices()`（原始列表）直接当页表传给内核，最坏会发生什么？这个 bug 的真实代价记录在哪里？

答案：在块边界上列表比实际需要多一块，内核由 \((\text{num\_pages}-1) \times \text{page\_size} + \text{last\_page\_len}\) 反推出偏长的序列，注意力会读到序列之外的垃圾 K/V（少页则越界）。代价记录在 issue #291：曾让 Kimi DP8 在 H200 上死锁、让 qwen3 的 FlashInfer 元数据在每个块边界多读一页垃圾（见 [src/pool.rs:L952-L960](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L952-L960) 的测试注释）。

### 4.3 提交族：apply_prefill / apply_prefill_chunk / apply_decode / apply_speculative、revert_schedule 与 release

#### 4.3.1 概念说明

forward 跑完、新 KV 已落在预约的页里，提交族负责让**事务层承认这次计算**，一次 apply 做三件事：

1. 把本步写满的块**封块注册**（注册过的块从此可被前缀缓存复用——上一讲「释放的注册块进 LRU 非活跃池」的伏笔在这里接上）；
2. 推进 `kv_position`；
3. 登记新采样的 token（它成为新的悬空 token），`generated_tokens` 加一。

提交族的五个成员各管一种收尾：

| 方法 | 场景 | 关键语义 |
|------|------|---------|
| `apply_prefill(token, pool)` | **最后一个**预填 chunk | 收下本步产出的第一个生成 token；此后进入解码态 |
| `apply_prefill_chunk(pool)` | 非最后的预填 chunk | 只封块、推进 `kv_position`，**不**产出 token（文档明确：最后一块必须走 `apply_prefill`） |
| `apply_decode(token, pool)` | 解码一步 | 返回 `DecodeOutcome`，告知块边界/长度上限是否到达 |
| `apply_speculative(accepted, pool)` | 投机验证步 | 只提交**被接受**的前缀，拒绝的草稿块按 LIFO 归还 |
| `release()` / `drop` | 请求终结 | 全部块归还：注册块进非活跃池（可复用），未注册块回空闲池 |

`DecodeOutcome` 是 `apply_decode` / `apply_speculative` 的返回值（[kvbm/kvbm-logical/src/integrations/scheduled.rs:L183-L188](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/kvbm/kvbm-logical/src/integrations/scheduled.rs#L183-L188)），四个变体可两两组合：

```rust
pub enum DecodeOutcome {
    Continue,
    BlockCompleted,
    MaxLength,
    BlockCompletedAndMaxLength,
}
```

从命名即可读出语义：「这一步是否恰好封满一块」（`BlockCompleted`，提示调度器该块已可复用）与「生成是否已达 `max_output_tokens`」（`MaxLength`，请求完成），两者同时发生就是第四个变体。

两阶段分离的**失败恢复面**由两个出口兜底：`revert_schedule` 主动撤销「已预约未提交」的一步（文档举例：投机的 forward 或 apply 失败时）；什么都不做直接 drop 则由 `SchedulableSequence` 的析构守卫归还——测试 `schedule_without_apply_returns_blocks_on_drop` 的注释就是这个行为的规格说明（*"dropping RequestKv must return those blocks to the pool"*，[tests/lifecycle.rs:L171-L192](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/lifecycle.rs#L171-L192)）。

#### 4.3.2 核心流程

```
apply_prefill(token):
    校验状态 == 已调度的最终预填 chunk
    封块注册本 chunk 的页 → kv_position += chunk_len
    记录 token 为悬空 token，generated_tokens += 1     # 第一个生成 token

apply_decode(token):
    封块/推进 kv_position（把上一个悬空 token 的 KV 落账）
    记录新 token 为悬空 token，generated_tokens += 1
    返回 Continue | BlockCompleted | MaxLength | BlockCompletedAndMaxLength

apply_speculative(accepted_tokens):
    只保留 accepted 前缀对应的 KV；被拒草稿块 LIFO 归还
    generated_tokens += accepted.len()

失败出口 A：revert_schedule()   → 撤销本步预约，请求存活，可继续
失败出口 B：直接 drop            → 析构守卫归还本步预约的块
终结：     release()            → 归还全部块（注册块 → 非活跃池，可被前缀缓存命中）
```

#### 4.3.3 源码精读

提交族同样是一层薄委托（[src/pool.rs:L517-L564](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L517-L564)）：

```rust
pub fn apply_prefill(&mut self, token: u32, pool: &BlockPool) -> anyhow::Result<()> {
    self.seq
        .apply_prefill(Some(token), &pool.block_manager)
        .map_err(|e| anyhow::anyhow!("apply_prefill: {e}"))
}

/// Apply a non-final prefill chunk: registers the chunk's blocks and
/// advances `kv_position` without emitting a generated token. The final
/// chunk must go through [`Self::apply_prefill`] instead.
pub fn apply_prefill_chunk(&mut self, pool: &BlockPool) -> anyhow::Result<()> {
    self.seq
        .apply_prefill(None, &pool.block_manager)
        .map_err(|e| anyhow::anyhow!("apply_prefill_chunk: {e}"))
}

pub fn apply_decode(&mut self, token: u32, pool: &BlockPool) -> anyhow::Result<DecodeOutcome> {
    self.seq
        .apply_decode(token, &pool.block_manager)
        .map_err(|e| anyhow::anyhow!("apply_decode: {e}"))
}
```

这段代码做的事：`apply_prefill` 与 `apply_prefill_chunk` 其实是上游同一个方法 `apply_prefill(Some/None)` 的两个外壳——`Some(token)` 表示这是产出首 token 的最终 chunk，`None` 表示中间 chunk。投机提交与回退（[src/pool.rs:L540-L558](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L540-L558)）的文档注释把分工写得很清楚：*"kvbm keeps the `accepted_tokens` KV and LIFO-releases the rejected draft blocks"*；`revert_schedule` 则 *"Undo a scheduled-but-unapplied KV reservation … and return its blocks to the pool"*。

drop 守卫的完整规格（[tests/lifecycle.rs:L171-L192](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/lifecycle.rs#L171-L192)）：

```rust
/// Schedule allocates blocks, but if we never apply (e.g. forward
/// failed), dropping RequestKv must return those blocks to the pool.
#[test]
fn schedule_without_apply_returns_blocks_on_drop() {
    let mgr = make_manager(10); // 9 usable
    let before = mgr.pool().available_blocks();

    {
        let mut req = mgr.pool().new_request(vec![1; 32], 1, None);
        // 32 tokens → ceil(32/16) = 2 blocks scheduled
        req.schedule_prefill(32, mgr.pool())
            .expect("schedule_prefill");
        assert!(mgr.pool().available_blocks() < before);
        // No apply_prefill — simulate forward failure. Drop req here.
    }

    assert_eq!(
        mgr.pool().available_blocks(),
        before,
        "blocks must return on drop"
    );
}
```

这段测试做的事：32-token 预填预约 2 块（可用 9 → 7），在内层作用域结束时**故意不 apply 就 drop**，出作用域后可用块数必须回到 9。引擎由此获得一条免费的安全网：forward 崩了、请求被取消、上层代码 panic 展开——只要 `RequestKv` 被析构，块就不会泄漏。

端到端全流程的黄金样例是 `single_request_prefill_decode_release`（[tests/lifecycle.rs:L10-L50](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/lifecycle.rs#L10-L50)），它把 4.1-4.4 四族 API 按协议顺序各用一遍，综合实践一节会逐行排演它。

#### 4.3.4 代码实践

1. **实践目标**：预测 `single_request_prefill_decode_release` 中六次提交的返回值（尤其 `DecodeOutcome`）。
2. **操作步骤**：
   - 场景：prompt 10 个 token，`max_output_tokens = 6`；`block_size = 16`。`apply_prefill(42)` 之后 `kv_position = 10`、`generated = 1`；解码循环执行 5 次 `apply_decode(100 + i)`，`kv_position` 走 11→15，始终没有跨过 16 的块边界。
   - 先在纸上写下每次 `apply_decode` 的预期 `DecodeOutcome`，再在**你自己的克隆**里把测试复制一份，将 `let _ = req.apply_decode(...)` 改成收集返回值并 `println!("{:?}", outcome)`。
3. **需要观察的现象**：前几次与最后一次的变体不同。
4. **预期结果**：按变体命名语义推断应为 4 次 `Continue` + 最后一次 `MaxLength`（生成数到达上限 6；全程未封满块，故无 `BlockCompleted`）。此为**基于变体命名的推断，待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`apply_prefill` 有 `token` 参数而 `apply_prefill_chunk` 没有，为什么？

答案：只有**最终**预填 chunk 会触发采样、产出第一个生成 token（要把它登记为悬空 token）；中间 chunk 只封块推进位置，没有 token 可交。上游用 `Some(token)` / `None` 区分（[src/pool.rs:L519-L532](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L519-L532)）。

**练习 2**：`revert_schedule` 和 `release` 都把块还给池，差别是什么？

答案：`revert_schedule` 只撤销**当前这一步已预约未提交**的预约，请求继续存活、状态回到可再次调度（用于一步失败后的重试）；`release` 终结整个请求，归还其**全部**块——其中注册过的块进入非活跃池成为前缀缓存的一部分，而非简单回空闲池（[src/pool.rs:L552-L564](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L552-L564)，非活跃池语义见 u2-l1）。

**练习 3**：`apply_speculative` 为什么必须 LIFO 归还被拒的草稿块？

答案：投机一步预约的是覆盖 k 个连续位置的一串块，验证后只有被接受前缀的 KV 要保留。被拒部分集中在预约串的**尾部**，后进先出的归还顺序恰好对应「从尾部往回收」，使保留前缀与已注册块的边界保持一致，避免误放仍被接受的块（文档注释：*"kvbm keeps the `accepted_tokens` KV and LIFO-releases the rejected draft blocks"*，[src/pool.rs:L540-L550](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L540-L550)；完整语义在上游 kvbm，**深入实现待确认**）。

### 4.4 状态查询族：kv_position / generated_tokens / is_complete / resident_blocks

#### 4.4.1 概念说明

调度器每一步都要「看一眼」请求才能决策，查询族就是这扇观察窗。四个量各数一件事，**初学者最容易混的是前三个**：

| 查询 | 数的是什么 | 单调性 |
|------|-----------|--------|
| `kv_position()` | K/V **已算完落账**的 token 数（悬空 token 不算） | 只增 |
| `generated_tokens()` | 已产出的生成 token 数（含悬空的那颗） | 只增 |
| `is_complete()` | 生成是否已达 `max_output_tokens` | 一旦真不再变 |
| `resident_blocks()` | 此刻**物理上**持有的块数（注册 + 已 stage + 未指派） | 可升可降（apply/revert/释放） |

`kv_position` 与 `generated_tokens` 永远差 1（预填完成后）：产出的最新 token 悬空着，等下一步才算 KV。这就是「视图 = `kv_position` + 1」的代数根源。

`resident_blocks` 是四个里唯一**可以暂时超过** \(\lceil \text{kv\_position} / \text{block\_size} \rceil\) 的量——因为积极分配的块、预约未提交的块都真实占着物理页。文档原话：*"Physical blocks currently held by this request, including registered, staged, and eagerly allocated dangling blocks"*（[src/pool.rs:L592-L596](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L592-L596)）。调度器做准入时必须用它（而非 `kv_position` 推算），否则会低估占用。

#### 4.4.2 核心流程

```
任意时刻：
    kv_position       = 已封块/已落账的 KV token 数
    generated_tokens  = kv_position - prompt_len + 1        （预填完成后）
    is_complete()     ⇔ generated_tokens ≥ max_output_tokens
    resident_blocks   = assigned + staged + unassigned      ≥ ceil(kv_position / block_size)

available_blocks()    = 池总块数 - 1(padding) - Σ各请求 resident_blocks（近似，忽略非活跃池回收的细节）
```

（最后一个式子是理解综合实践里容量排演的钥匙：**观察一个请求的占用，就看 `available_blocks` 掉了多少**。）

#### 4.4.3 源码精读

查询族一览（[src/pool.rs:L577-L596](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L577-L596)）：

```rust
/// Tokens with KV already computed.
pub fn kv_position(&self) -> usize {
    self.seq.kv_position()
}

pub fn generated_tokens(&self) -> usize {
    self.seq.generated_tokens()
}

/// Physical blocks currently held by this request, including registered,
/// staged, and eagerly allocated dangling blocks.
pub fn resident_blocks(&self) -> usize {
    self.seq.assigned_blocks() + self.seq.staged_blocks() + self.seq.unassigned_blocks()
}
```

这段代码做的事：三个计数直接读上游序列；`resident_blocks` 明确把三类持有（已注册 / 已 stage / 未指派）加总——其中「未指派」就包含积极分配出的下一块。

与视图族衔接的两个页表查询也值得认识（[src/pool.rs:L598-L631](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L598-L631)）：`page_indices()` 是**原始**持有列表（可以多一块，私有方法）；`current_page_indices()` 把它截断到当前 `kv_position` 需要的页数，供 offload 等模块做「现在已落账的页」的快照；`step_page_indices(n)` 是 forward 用的精确页表行（4.2 已精读）。三者同名异义，正是「原始 vs 落账 vs 本步」三个视角的代码化。

测试侧的对应断言：`single_request_prefill_decode_release` 在预填 apply 后立刻断言 `kv_position() == 10`、`generated_tokens() == 1`（[tests/lifecycle.rs:L31-L33](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/lifecycle.rs#L31-L33)），解码五轮后断言 `generated_tokens() == 6` 且 `is_complete()`（[tests/lifecycle.rs:L44-L45](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/lifecycle.rs#L44-L45)）——注意 `is_complete` 为真时**最后那颗 token 仍是悬空的**，`kv_position` 停在 15 而非 16。

#### 4.4.4 代码实践

1. **实践目标**：用查询族给 `single_request_prefill_decode_release` 建一张状态时间线表。
2. **操作步骤**：对照 [tests/lifecycle.rs:L10-L50](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/lifecycle.rs#L10-L50)，为下表的每个空格填值（`block_size = 16`，prompt 10，max 输出 6）：

   | 时刻 | kv_position | generated_tokens | is_complete | resident_blocks |
   |------|-------------|------------------|-------------|-----------------|
   | new_request 后 | 0 | 0 | 否 | 0 |
   | schedule_prefill(10) 后 | ？ | 0 | 否 | ？ |
   | apply_prefill(42) 后 | 10 | ？ | 否 | ？ |
   | 第 2 次 apply_decode 后 | ？ | 3 | 否 | ？ |
   | 第 5 次 apply_decode 后 | 15 | ？ | ？ | ？ |

3. **需要观察的现象**：`resident_blocks` 一列前两行就能确定（预约 1 块），后几行因积极分配时机依赖上游实现，标注「待确认」。
4. **预期结果**：填完后与 4.4.5 练习 1 的答案对照；如本地有环境，可复制测试加打印核实 `resident_blocks` 列（**待本地验证**）。

#### 4.4.5 小练习与答案

**练习 1**：给出上表的完整答案。

答案：schedule_prefill(10) 后 `kv_position = 0`（调度不动位置）、`resident_blocks = 1`；apply_prefill(42) 后 `generated_tokens = 1`、`resident_blocks = 1`（10 个 token 一块装得下；42 悬空不占 KV）；第 2 次 apply_decode 后 `kv_position = 12`（10 + 42 + 100 两颗已落账）；第 5 次 apply_decode 后 `generated_tokens = 6`、`is_complete = true`、`kv_position = 15`（第 6 颗生成 token 悬空，永不算 KV）、`resident_blocks` 大概率仍为 1（未触块边界，是否发生积极分配**待确认**）。

**练习 2**：预填完成后，`kv_position` 与 `generated_tokens` 恒差 1，这个 1 是什么？

答案：就是悬空 token——刚采样出来、KV 尚未计算的那颗。它是下一步 forward 的输入；若请求就此完成（如上题），它永远悬空，这也是「16 prompt + 16 输出只需 \(\lceil 31/16 \rceil = 2\) 块」里那个「-1」的来源。

**练习 3**：调度器准入一个新请求时，为什么不该用 \(\lceil \text{kv\_position} / \text{block\_size} \rceil\) 估算在场请求的占用？

答案：那是「已落账」的页数下界；真实占用还包括积极分配的未指派块、已预约未提交的块。`resident_blocks()` 把三类全部加总（[src/pool.rs:L594-L596](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L594-L596)），用它才不会超发。

## 5. 综合实践

把整讲串起来的任务：**给两个生命周期测试写出 `available_blocks()` 的逐行剧本，再用一个无 GPU 的纯逻辑测试验证 drop 守卫**。

**第一步：排演 `single_request_prefill_decode_release`**（[tests/lifecycle.rs:L10-L50](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/lifecycle.rs#L10-L50)，池 32 块）。逐行写可用块数并注明依据：

| 步骤 | available_blocks | 依据 |
|------|------------------|------|
| 构造后 | 31 | 32 − 1 padding（测试断言） |
| new_request([1;10], 6) | 31 | 只建序列，不占块 |
| schedule_prefill(10) | 30 | \(\lceil 10/16 \rceil = 1\) 块 |
| prefill_view(10) | 30 | 视图只读 |
| apply_prefill(42) | 30 | 块早在预约时扣过 |
| 5 × (schedule_decode + apply_decode) | 30 左右 | 不跨 16 边界时基本不再拿块；某步若触发积极分配会再 −1（**待本地验证**） |
| release() | 31 | 测试断言回到 initial_avail |

**第二步：排演 `multiple_requests_share_capacity`**（[tests/lifecycle.rs:L52-L79](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/lifecycle.rs#L52-L79)，池 10 块）。测试只断言了三处硬值（初始 9、`after_a < 9`、`after_b < after_a`、双释放后回到 9），请把每一处 `<` 换成你预测的精确值，并注明哪些数字依赖积极分配时机（**待本地验证**）。

**第三步：验证 drop 守卫（无 GPU）**。模仿 4.1.4 的纯逻辑写法，把 [tests/lifecycle.rs:L173-L192](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/lifecycle.rs#L173-L192) 改写成 `BlockPool::new(16, 10)` 版本（下面是**示例代码**）：

```rust
use pegainfer_kv_cache::BlockPool;

#[test]
fn drop_after_schedule_returns_blocks_without_cuda() {
    let pool = BlockPool::new(16, 10).unwrap(); // 9 可用
    let before = pool.available_blocks();
    {
        let mut req = pool.new_request(vec![1; 32], 1, None);
        req.schedule_prefill(32, &pool).expect("schedule");
        assert_eq!(pool.available_blocks(), before - 2); // ceil(32/16)=2
        // 故意不 apply —— 模拟 forward 失败，出作用域直接 drop
    }
    assert_eq!(pool.available_blocks(), before, "blocks must return on drop");
}
```

运行：`cargo test --release -p pegainfer-kv-cache --test drop_rehearsal`（文件名自定；纯逻辑，无需 CUDA，**输出待本地验证**）。三个数字 `before - 2` / `before` 都来自测试注释里的 2 块预算，若断言失败请回头检查你对积极分配时机的理解。

## 6. 本讲小结

- 一个 forward 步骤固定三拍：**schedule（预约块）→ view（给内核精确页表）→ apply（封块、推进 `kv_position`、登记新悬空 token）**；`RequestKv` 是上游 `SchedulableSequence` 的薄包装，协议在本 crate、机制在 kvbm-logical。
- 调度按步渐进预约（预填只拿当前 chunk 的页，解码逐步补块），失败（如 `ScheduleError::AllocationFailed`）时不持有任何资源；`revert_schedule` 撤销一步，直接 drop 靠析构守卫归还——两阶段分离就是引擎的失败恢复能力。
- 视图的三条不变式：`seq_len` 取「步后」长度（decode 恒为 `kv_position + 1`）、`prefill_view` 覆盖目标长度、页表行必须精确覆盖 `seq_len`（多一页读垃圾、少一页越界，#291 的教训）；`step_page_indices` 专门裁掉积极分配多出的块。
- 提交族按场景分工：`apply_prefill`（带首 token 的最终 chunk）/ `apply_prefill_chunk`（中间 chunk）/ `apply_decode`（返回 `DecodeOutcome` 四变体）/ `apply_speculative`（只提交被接受前缀，LIFO 回收拒绝块）；`release` 让注册块进非活跃池供前缀缓存复用。
- 查询族四件套各数一件事：`kv_position`（已落账 KV）、`generated_tokens`（已产出，含悬空）、`is_complete`（达上限）、`resident_blocks`（物理持有，含积极分配，可超过落账需求——准入估算必须用它）。

## 7. 下一步学习建议

- **下一讲 u2-l3（KvView 与注意力内核的契约）**：本讲把 `KvView` 当黑盒用了，下一讲钻进 [src/view.rs](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/view.rs)，看 `KvViewDesc` 如何把布局、物理缓冲指针与页表打包成内核元数据束，并亲手构造 17-token 的越界/缺页反例。
- 想看调度协议的真实消费者，去 `pegainfer-qwen3` 的调度器/executor 里搜 `schedule_prefill` / `decode_view` 的调用点，对照本讲的三拍子。
- 想先补全上下文可跳读 u2-l5（前缀缓存）：`release` 进非活跃池的块如何被 `match_and_add_prefix` 重新挂上，本讲 4.3 的「封块注册」正是那条链的起点。
