# 并发正确性：两段式准入、RAII 纪律与 EP 自由跑动

> 讲义 id：u5-l1 · 依赖：u3-l2（kv_pool.rs）、u4-l1（cuda_graph.rs）· 层级：advanced
>
> 永久链接 base（本讲全部链接基于 HEAD `09a1a38f`）：core 内文件以 `pegainfer-core/` 为前缀；仓库根文件（CLAUDE.md、docs/、pegainfer-gemma4/）直接挂在同一 commit 下。

## 1. 本讲目标

u3 和 u4 分别精读了「是什么」：页池怎么分配、KV 池怎么准入、CUDA Graph 怎么回放。本讲换一个视角，回答「**为什么必须是这个形状**」：

1. 能独立复述**两段式 admission**（`try_reserve` → `commit_reservation`）与 **RAII 归还**各自防御的失败模式——单段准入在多池下如何制造「占了 A 池却等不到 B 池」的部分占用死锁。
2. 能论证 **padding 页为什么必须在池构造期预留**：页号恒定、内容无害，是 CUDA Graph 回放确定性的地基；运行期再申请或外借都会破坏不变量。
3. 能把 core 的**单卡内存纪律**逐条映射到 CLAUDE.md 的 **EP free-running 三不变量**（不停止、固定链、padding 即协议面），看懂「单卡的 try-then-decide」如何放大成「多卡的先约定、绝不等待」。

学完本讲，你读到任何一段资源管理代码时，应该会条件反射地追问三个问题：**失败路径还资源吗？占了一半会卡住谁？这段字节会被别人看到吗？**

## 2. 前置知识

- **admission（准入）**：调度器决定「这个请求这一步能不能进 batch」。准入失败不是错误，是排队信号——这个语义区分贯穿本讲。
- **部分占用死锁**：多个资源池、逐个获取的经典问题。请求 R1 拿住了 A 池的页却在等 B 池，请求 R2 拿住了 B 池的页却在等 A 池——两个都在等对方释放，谁也走不完。教科书解法是**两段获取**（先全部预订、再统一拿走），本讲的 `try_reserve`/`commit` 就是它的工程化。
- **RAII（Resource Acquisition Is Initialization）**：资源所有权绑在对象生命周期上，`Drop` 时自动归还。Rust 的移动语义让「忘了还」在类型层面不可能表达——permit 一旦离开作用域，页必然回池。
- **CUDA Graph 回放**（u4-l1 已讲机制）：decode 热路径录成图后原样重发录制时刻烘焙的参数，要求「**内容动、地址不动**」——一切变化只能走预分配 buffer 与原地重填的 i32 页表。
- **EP（Expert Parallelism）与 free-running**：MoE 模型把专家切到多卡，每层 forward 都要做 dispatch/combine 集合通信；free-running 指每个 rank 的 engine loop **无条件全速跑**，空 rank 以 padding 行进场，唯一耦合是每步固定的集体通信链。**mispair（错位配对）**：某 rank 的第 N 层和别人第 N+k 层配上，产出的字节是「确定性的垃圾」且不 crash——最恶性的失败模式（详见 EP 文档 §2）。

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
|---|---|
| [pegainfer-core/src/page_pool.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/page_pool.rs) | RAII 纪律的原点：`OwnedPagePermit` 的 Drop / absorb / try_grow，194 行，全部纯 CPU |
| [pegainfer-core/src/kv_pool.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs) | 两段式准入（`try_reserve`/`KvReservation`/`commit_reservation`）与 padding 页的构造期预留 |
| [pegainfer-core/src/cuda_graph.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph.rs) | 「捕获期间禁分配」纪律与捕获失败的排水路径（RAII 不可用处的手工等价物） |
| [pegainfer-gemma4/src/kv.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-gemma4/src/kv.rs) | 双池准入的**真实消费者**：`admit_tokens`，两段式协议的现场证据 |
| [pegainfer-gemma4/src/serve.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-gemma4/src/serve.rs) | padding 页的现场消费：空槽填 `padding_page_id()` |
| [CLAUDE.md](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/CLAUDE.md) | EP Free-Running Discipline 四条不变量（仓库级纪律声明） |
| [docs/models/glm52/free-running-dp.md](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/models/glm52/free-running-dp.md) | EP free-running 的规范文档：三条纪律、padding corner cases、kernel gates 实测 |

## 4. 核心概念与源码讲解

本讲的四个最小模块：**4.1 两段式准入**、**4.2 RAII 归还不变量**、**4.3 padding 确定性**、**4.4 EP free-running 不变量**。前三个在单卡 core 内，第四个把它们放大到多卡。

### 4.1 两段式 admission：「先全部预订，再统一过户」

#### 4.1.1 概念说明

一个请求的 KV 页可能来自**多个池**（gemma4 同时有滑动窗 local 池和全局 global 池；Qwen3.5 混合架构还要加线性注意力状态）。如果准入是「单段」的——直接往请求自己的 permit 上生长——就会出现：

- 拿到了 local 池的页，global 池却没了；
- 请求已经「半只脚」进了 batch，手里握着 local 页等 global；
- 别的只差 local 页的请求也被它挡住。

极端情形就是两池循环等待：持有 A 页者等 B，持有 B 页者等 A，整个 fleet 卡死。防御办法是经典两段获取协议：

1. **reserve 半段（调度面）**：对每个池各发一次 `try_reserve`，拿到的是「预约」（`KvReservation`），**还没绑定到任何请求**；
2. **全有或全无**：任一池返回 `None`，已拿到的预约直接 drop——Drop 就是回滚，两个池回到请求到来之前的占用率；
3. **commit 半段（应用面）**：全部预约在手，才把它们 `absorb` 进请求的 `KvState`，页正式过户。

注意 core 里还有一个「单段」入口 `ensure_capacity`——它直接生长请求自己的 permit。它不是漏洞，而是**分相位分工**：`ensure_capacity` 只在调度器已经放行、容量已有保证的执行路径上用；准入路径必须走两段式。两个入口对应两个相位，混用才会出事。

#### 4.1.2 核心流程

```text
调度步开始
  │
  ├─ 对请求 R：
  │    r_local  = local_pool.try_reserve(n_local)    ─┐ 任一为 None：
  │    r_global = global_pool.try_reserve(n_global)  ─┘ drop 全部已得预约 → 排队，下一轮再试
  │
  ├─ 全部 Some：
  │    kv.local.吸收(r_local)        # gemma4: extend_resident / core: commit_reservation
  │    kv.global.commit_reservation(r_global)
  │    R 正式进 batch
  │
  └─ loop 无条件继续（没有任何等待/睡眠）
```

关键性质：**失败路径的回滚不写一行代码**——预约对象离开作用域即还页。占用率在任一时刻满足守恒式：

\[ \forall t:\ \bigl|\mathrm{free}(t)\bigr| + \sum_{p\,\in\,\mathrm{permits}(t)} |p| = \mathrm{capacity} \]

没有「已分配但既不在 free list 也不在任何 permit 里」的幽灵页，是下面 4.2 的 RAII 保证的。

#### 4.1.3 源码精读

**reserve 半段**——[pegainfer-core/src/kv_pool.rs:L274-L283](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L274-L283)：`try_reserve` 的文档注释把协议说得很完整——「reserve from every pool first, commit only when all reservations hold, and let a failure roll the rest back by drop. **Growing a request's own permit instead cannot be undone.**」（直接生长请求自己的 permit 是不可撤销的）。注意返回 `Option`：「页暂时不够」是调度常态，不是错误。

**预约对象**——[pegainfer-core/src/kv_pool.rs:L286-L299](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L286-L299)：`KvReservation` 包着一个 `OwnedPagePermit`，文档只有一句「Returns its pages on drop unless committed」——回滚语义就是 Drop 语义。`extend_page_indices_i32` 让调度器在**不 commit** 的状态下就能把预约页号抄进计划数组（gemma4 的 prefix-cache 捕获正是这样用）。

**commit 半段**——[pegainfer-core/src/kv_pool.rs:L380-L384](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L380-L384)：`commit_reservation` 一行 `permit.absorb(reservation.permit)`，文档写明「The apply half of an atomic multi-pool admission」——同池约束由 `absorb` 的断言强制（见 4.2.3）。

**真实消费者的完整现场**——[pegainfer-gemma4/src/kv.rs:L334-L416](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-gemma4/src/kv.rs#L334-L416)：`admit_tokens` 的函数文档就是本模块的命题陈述——「**Atomic across the two pools: a refused side drops the other side's reservation, leaving both pools at their pre-request occupancy.**」核心序列（节选）：

```rust
// pegainfer-gemma4/src/kv.rs:L381-L406（原文节选）
let mut local_rs = Vec::with_capacity(local_need);
while local_rs.len() < local_need {
    match local_pool.try_reserve(1) {
        Some(r) => local_rs.push(r),
        None => break,
    }
}
let local_granted = local_rs.len() == local_need;
let global_r = if local_granted { global_pool.try_reserve(global_need) } else { None };
match (local_granted, global_r) {
    (true, Some(global_r)) => {
        kv.local.extend_resident(local_rs);
        kv.global.commit_reservation(global_r);
        Ok(())
    }
    (local_granted, global_r) => {
        // Report availability after rollback.
        drop((local_rs, global_r));
        anyhow::bail!("admission refused ...");
    }
}
```

读三个细节：① local 侧**每页一个预约**（[L382-L383](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-gemma4/src/kv.rs#L382-L383) 注释：滑动窗口的队首要逐页释放，所以这些页不能共享一个 permit）；② global 侧**只在 local 全额到手后才尝试**——把「占着 A 等 B」的窗口压到最小；③ 失败分支显式 `drop((local_rs, global_r))` 后才读 `available_pages()` 打日志，保证报出的可用数是**回滚之后**的真值。入口处还有一个跨池护栏——[L342-L346](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-gemma4/src/kv.rs#L342-L346) 用 `belongs_to` 断言状态与池同源，否则「会发出 executor 无法寻址的页号」。

另一处消费点在 [pegainfer-gemma4/src/serve.rs:L1434-L1438](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-gemma4/src/serve.rs#L1434-L1438)：prefix-cache 捕获同样先 reserve 双池、抄页号、拷页，任何一步 `?` 早退，已持预约全部随作用域 drop 回滚。

#### 4.1.4 代码实践

1. **实践目标**：确认「两段式协议在真实消费者里逐字落地」，并手推单段准入的反例。
2. **操作步骤**：
   - 读 [pegainfer-gemma4/src/kv.rs:L334-L416](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-gemma4/src/kv.rs#L334-L416) 全函数，标出 reserve 循环、global 尝试、commit 分支、drop 回滚分支各自行号；
   - 在仓库根执行 `grep -rn "try_reserve" pegainfer-*/src --include="*.rs" | grep -v tutorial`，列出全部消费点（预期至少 gemma4 的 kv.rs 与 serve.rs）；
   - 纸面推演反例（不需要跑代码）：local 池剩 2 页、global 池剩 0 页，请求 R1 需要 2+2。假设准入是「先 `ensure_capacity` local 再 `ensure_capacity` global」，写出 R1 在第一条 bail 之后的资源状态；再让另一请求 R2 已持有 global 页、只差 1 页 local 才能跨页解码——推演两池循环等待的形成。
3. **需要观察的现象**：反例中 R1 的 local 页「既还不了也用不上」（`ensure_capacity` 成功的那一半没有回滚入口），R2 永远等不到 local 页 → 两个请求互相卡死；而真实代码里 R1 的预约在 drop 时全数归还，R2下一轮就能拿到页。
4. **预期结果**：你能在笔记里画出两条时间线（单段 vs 两段）的页占用对照图，单段版本在 t₂ 时刻出现「持有却不前进」的占用。
5. grep 结果因仓库演进而变，以实际输出为准；纸面推演无运行依赖。

#### 4.1.5 小练习与答案

**练习 1**：`try_reserve` 返回 `Option<KvReservation>`，而 `ensure_capacity` 返回 `Result<()>` 并在失败时 `bail!`——为什么同一件「页不够」有两种返回语义？

**答案**：`try_reserve` 是**调度面**原语，「页不够」是预期常态，`None` 就是「这轮别放行」，调度器排队下轮再试，不构成错误；`ensure_capacity` 是**执行面**原语，只在调度器已承诺容量之后调用，页不够意味着调度承诺被打破，必须带着上下文报警（`bail!("KvState: out of pages ...")`）。两段式准入防的是前者阶段的错误用法；后者阶段的失败是 bug 信号。（u3-l2 练习已建立这一区分，此处从「相位分工」角度复述。）

**练习 2**：如果允许两个不同 `KvPool` 的预约互相 `absorb`（去掉同池断言），最先坏掉的是什么？

**答案**：页会还错 free list——`OwnedPagePermit::Drop` 只认自己持有的 `PagePoolInner`，A 池的页被并进 B 池 permit 后，drop 时会推进 B 池的空闲表，A 池凭空少页、B 池出现重复页号，随后 KV 写入错位。core 的防线是 `absorb` 里的 `Arc::ptr_eq` 断言（见 4.2.3），`KvState::belongs_to` 则把同一约束暴露给消费者做入口自检。

**练习 3**：gemma4 的 `admit_tokens` 里，为什么 local 池逐页 `try_reserve(1)` 而 global 池一次 `try_reserve(n)`？

**答案**：滑动窗口的队首页会**逐页释放**（窗口前移时最老的页单独归还），所以 local 侧每个页必须是独立 permit 才能单独 drop；global 池整段持有、整段释放，一个 permit 装全部页即可。预约的粒度必须匹配释放的粒度——这也是「RAII 粒度设计」的一般原则。

### 4.2 RAII 归还不变量：Drop、absorb 与失败路径的零泄漏

#### 4.2.1 概念说明

两段式准入解决「跨池原子性」，RAII 解决「**任意一条提前退出路径**（`?`、`bail!`、`ensure!`、`panic!`、`match` 早退）都不丢资源」。没有 RAII 时，acquire 返回裸句柄，每条错误路径都要手写 release——漏一条就永久泄漏，free list 守恒式被破坏，池慢慢「蒸发」。有了 RAII：

- 页的所有权只存在于 `OwnedPagePermit` 这一个类型里；
- Rust 的作用域规则保证 permit 离开作用域必走 `Drop`；
- 于是「归还」不需要出现在任何调用点的代码里——**回滚是默认行为，忘记回滚在类型层面不可表达**。

`KvState`（持 permit + `seq_len`）与 `KvReservation`（仅持 permit）都是这个纪律的载体；`cuda_graph.rs` 的捕获失败排水路径则展示了 FFI 边界上 RAII 不可用时如何**手工等价**地恢复不变量。

#### 4.2.2 核心流程

```text
OwnedPagePermit 生命周期：
  try_acquire_many(n) / try_grow(n)   ← 原子获取（长度检查与弹出在同一锁临界区内）
        │
        ▼
  pages: Vec<PageId>  ← 唯一所有权；可 absorb 合并（限同池）、不可部分释放
        │
        ▼
  Drop: take(pages) → lock(free_list) → 逆序 push 回  ← 全部归还，无路径可绕过

不变量（对任意时刻 t）：
  I1 守恒：|free(t)| + Σ|permit| = capacity（无幽灵页）
  I2 原子性：try_acquire_many/try_grow 要么全额成功、要么 permit 原样（不存在半获取）
  I3 同池性：absorb 前后页仍属于同一 free list（断言强制）
```

#### 4.2.3 源码精读

**原子获取**——[pegainfer-core/src/page_pool.rs:L45-L67](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/page_pool.rs#L45-L67)：`try_acquire_many` 在同一把锁的临界区内**先检查长度再逐个弹出**——`free_list.len() < n` 时直接 `None`，一个页都没动。这保证 I2：调用者永远不会拿到「半袋页」。`n == 0` 走无锁快路径（L46-L51），这也是 `KvPool::alloc` 能 `expect("zero acquire")` 的依据——零页预约不可能失败。

**Drop 归还**——[pegainfer-core/src/page_pool.rs:L120-L130](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/page_pool.rs#L120-L130)：

```rust
// 原文节选
impl Drop for OwnedPagePermit {
    fn drop(&mut self) {
        if self.pages.is_empty() { return; }
        let pages = std::mem::take(&mut self.pages);
        let mut free_list = self.inner.free_list.lock();
        free_list.extend(pages.into_iter().rev());
    }
}
```

三个细节：先把 `pages` 的所有权 `take` 出来（锁临界区内只做 `extend`，持锁时间最短）；**逆序**还回（配合构造期 `(0..cap).rev()` 建表、pop 取页，让「最近释放的页最先被再取」，地址局部性最好）；空 permit 早退（`alloc` 出来的零页状态反复 drop 不碰锁）。

**absorb 与 try_grow**——[pegainfer-core/src/page_pool.rs:L89-L97](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/page_pool.rs#L89-L97) 与 [L99-L117](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/page_pool.rs#L99-L117)：`absorb` 用 `assert!(Arc::ptr_eq(...))` 强制同池（I3），合并后**只剩吸收方一个 permit 持有这些页**——被吸收方变为空壳，drop 时不重复归还；`try_grow` 与 `try_acquire_many` 同构——检查与弹出同锁，失败时 permit 原样（文档原话「On failure the permit is unchanged」），这正是 `KvState::ensure_capacity` bail 时无泄漏的原因。

**KvState 的 RAII 载体**——[pegainfer-core/src/kv_pool.rs:L301-L308](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L301-L308)：`KvState` 持 `permit + seq_len + pool`，文档写明「Holds an RAII page permit that auto-returns pages on drop」。请求结束（状态 drop）即自动还页——[L550-L562](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L550-L562) 的 `kv_state_drop_returns_pages` 测试锁的就是这个行为（需 GPU，见测试内 `DeviceContext::new`）。

**RAII 不可用处的手工等价：捕获排水**——[pegainfer-core/src/cuda_graph.rs:L180-L189](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph.rs#L180-L189)：

```rust
// 原文节选：kernel 闭包出错时的排水路径
if let Err(e) = kernels() {
    let mut aborted: CUgraph = std::ptr::null_mut();
    unsafe { sys::cuStreamEndCapture(stream, &raw mut aborted) };
    if !aborted.is_null() { unsafe { sys::cuGraphDestroy(aborted) }; }
    return Err(e);
}
```

CUDA 的捕获状态是 FFI 裸句柄，没有 Rust 析构函数可挂。若 kernels 失败后直接 `return Err`，**流会滞留在 capturing 状态**——后续所有 launch 都会失败，等于把「泄漏」从内存换成了驱动状态。所以这里必须手工排水：先 `EndCapture` 收回半成品图、`Destroy` 之、再传播原始错误。这是同一纪律的另一种写法：**不变量的恢复必须发生在每一条提前退出路径上，无论靠类型还是靠手**。

#### 4.2.4 代码实践

1. **实践目标**：亲手运行 RAII 行为的测试，并完成一次 kv_pool.rs 失败路径的「无泄漏审计」。
2. **操作步骤**：
   - 跑纯 CPU 测试：`cargo test --release -p pegainfer-core --lib page_pool -- --nocapture`（三个测试：acquire/drop 恢复、容量不足返回 None、try_grow 原子性）；
   - 审计 kv_pool.rs 的全部提前退出路径，逐条回答「此时有没有已取出的页？谁来还？」。清单（行号见 4.2.5 练习 3 的答案表）：
     - `KvLayout::with_storage_and_format` 的 `derive_strides` 失败（[L83-L88](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L83-L88)）；
     - 池构造的两次 overflow `bail`（[L204-L209](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L204-L209)、[L212-L218](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L212-L218)）与 buffer 分配失败（[L221-L224](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L221-L224)）；
     - padding 预约的 `expect`（[L227-L229](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L227-L229)）；
     - `ensure_capacity` 的 `bail!`（[L367-L372](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L367-L372)）；
     - `desc_for_len` 的 `ensure!`（[L400-L403](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L400-L403)）。
3. **需要观察的现象**：page_pool 测试全绿；审计表里每条路径都能指认「页要么尚未取出、要么在 permit 里随 Drop 归还」。
4. **预期结果**：审计结论「kv_pool.rs 内不存在绕过 permit 的页所有权转移，全部失败路径无泄漏」（逐条理由见练习 3 答案）。
5. `cargo test` 需本地执行；无环境时以测试源码断言为准，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`Drop` 里为什么先 `std::mem::take(&mut self.pages)` 再加锁，而不是直接锁住后迭代 `self.pages`？

**答案**：把 Vec 的所有权移进局部变量，锁临界区里只剩一次 `extend`——持锁路径最短、且不持有 `&mut self` 的其他字段；同时即使未来有人在临界区内做更复杂的事，pages 也已经在局部，语义上「这一刻资源已离开对象」更清晰。逆序 `extend` 则让最近释放的页下轮最先被再分配（pop 端），地址重用稳定。

**练习 2**：`try_grow` 失败返回 `false` 时，permit 为什么「天然」保持原样，不需要任何补偿代码？

**答案**：长度检查（`free_list.len() < n` → return false）和逐页弹出在**同一把锁的同一临界区**内完成——要么检查就挡下（一个页没弹出），要么全部弹出成功。不存在「弹出一半发现不够」的中间态。这是「检查与生效同临界区」模式的直接收益：失败原子性免费获得。

**练习 3**：给出 kv_pool.rs 失败路径审计表（本讲综合实践第 (3) 问的参考答案）。

**答案**：

| 路径 | 位置 | 此时的页 | 归还者 |
|---|---|---|---|
| stride 推导失败 | kv_pool.rs:L83-L88 | 尚未创建 PagePool | 无页可泄 |
| 容量 overflow（元素域/字节域） | L204-L218 | 同上（检查先于分配） | 无页可泄 |
| buffer 分配失败 | L221-L224 | PagePool 未建 | 无页可泄 |
| padding 预约 `expect` panic | L227-L229 | padding permit 已取 | panic 展开 → permit Drop 还页；构造失败，池不存在，无泄漏 |
| `ensure_capacity` bail | L367-L372 | 页在 state 的 permit 里，`try_grow` 原子失败未动它 | permit 继续持有（请求仍活着），语义正确 |
| `desc_for_len` ensure! | L400-L403 | 纯元数据操作 | 无分配 |

### 4.3 padding 确定性：构造期预留的「协议页」

#### 4.3.1 概念说明

u4-l1 讲过：decode 按批大小分桶录 CUDA Graph，空槽填 padding 行。padding 行需要两个确定性：

- **页号确定**：图录制那一刻页表数组里出现的页号，在图整个生命周期内必须永远指向合法内存。真实请求的页会随请求生灭而流转，唯独 padding 页必须「**永不外借、页号恒定**」。
- **内容无害**：padding 槽每步都会被 KV append 写入——写的是无害垃圾（输出本来就丢弃），但写入目标必须是那块永远合法的内存。

所以答案是：**在池构造期就永久扣下一页**，此后它不进 free list、不参与任何准入、页号终身不变。这不是运行期的优化选择，而是图回放正确性的先决条件——运行期「需要时再申请一页当 padding」在图语义下根本不成立：图已经把那个 i32 页号烘焙进了 kernel 参数，改不了了。

注意区分两个层面：**core 的 padding 页管「地址确定」**；内容层面「KV append 写无害垃圾」在单卡下够了，但在 EP 下 padding 行的字节会流向对端——那时「垃圾也必须是构造性确定的垃圾」，这正是 4.4 的主题。

#### 4.3.2 核心流程

```text
KvPool::with_storage_and_format
  ├─ 几何推导 + overflow 检查（失败即返回，无副作用）
  ├─ alloc_zeros 单块大 buffer（所有请求的 KV 都是它的视图）
  ├─ PagePool::new(num_pages)
  ├─ pool.try_acquire_many(1).expect("pool must have at least 1 page for padding")   ← 构造期一次性扣页
  └─ padding_permit 存进 KvPoolInner（Arc 共享，永不 drop 直到池销毁）

decode 图填充空槽（消费者侧）：
  for r in batch..padded:
      page_indices[r] ← padding_page_id()   # 每步、每张图、每个空槽，同一个页号
      窗口元数据     ← 1 token（position 0 / last_len 1）
```

#### 4.3.3 源码精读

**构造期预留**——[pegainfer-core/src/kv_pool.rs:L117-L125](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L117-L125)：`KvPoolInner` 的 `padding_permit: OwnedPagePermit` 字段文档写得直白——「Reserved page for bucket CUDA Graph padding. FlashInfer metadata for padding slots points here with seq_len=1. KV append writes harmless garbage; attention output is discarded.」。预留动作在 [L226-L229](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L226-L229)：

```rust
// 原文节选
let pool = PagePool::new(num_pages);
let padding_permit = pool
    .try_acquire_many(1)
    .expect("pool must have at least 1 page for padding");
```

用 `expect` 而非优雅降级，是刻意的**启动期 fail-fast**：一个连 padding 页都留不出的池不可能服务任何图，问题应在 boot 时炸出来，而不是让 decode 走到一半再热路径崩溃。页号出口在 [L269-L272](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L269-L272)：`padding_page_id()` 就是这个 permit 第一页的下标——permit 永不释放，所以返回值在池生命周期内是常量。

**消费者现场（双池）**——[pegainfer-gemma4/src/serve.rs:L2203-L2212](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-gemma4/src/serve.rs#L2203-L2212)：

```rust
// 原文节选：Pad rows write each pool's reserved padding page — never a real
// request's KV — at position 0 with a one-token window.
for r in batch..padded {
    local_origins.push(0);
    local_rows[r].push(self.local_pool.padding_page_id());
    local_last.push(1);
    ...
    global_rows[r].push(self.global_pool.padding_page_id());
    ...
}
```

注意注释的措辞就是纪律本身：「**never a real request's KV**」。gemma4 有两个池（滑动窗 + 全局），两套页表行各需要一个合法空槽页号，且两池页号空间独立（u2-l4 讲过「页号只在签发它的池内有效」），所以**每池各预留一页**、各自填各自的行。u4-l1 还引用过 qwen3 单池版本的同类填充（`batch_decode_buffers.rs`），机制同源。

**与两段式准入的合流**——[pegainfer-core/src/kv_pool.rs:L274-L283](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L274-L283)（4.1 已读）：准入管「真实页怎么安全地**易主**」，padding 页管「有一页**永不易主**、永远可指」。两者合起来才凑齐图回放的前提：**录制那一刻页表里出现过的任何页号——真实请求的（由准入保证其在图生命周期内不被错误复用）与 padding 的（由构造期预留保证恒定）——都指向合法内存**。

**图捕获侧的纪律**——[pegainfer-core/src/cuda_graph.rs:L96-L104](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/cuda_graph.rs#L96-L104)：`run_or_capture` 的文档要求闭包是「a pure GPU kernel sequence — **no CPU-GPU sync, no allocation**」。捕获期间一旦分配，新地址就会进图，重放时指向已释放/漂移的内存——所以捕获闭包只能碰预分配 buffer 与原地重填的 i32 数组，padding 页号的恒定正是这条纪律在页表维度的落点。

#### 4.3.4 代码实践

1. **实践目标**：验证「padding 页号恒定、不随分配变化」，并理解 `expect` 的 fail-fast 语义。
2. **操作步骤**：
   - 读 kv_pool.rs 的 [L495-L538](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L495-L538)（`kv_state_lifecycle`）与 [L540-L548](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L540-L548)（`kv_state_out_of_pages`）——注意测试注释里反复出现「N pages total: 1 reserved for padding」的账目（例如 5 页池只有 4 页可分配），这就是 padding 页在测试视角的存在感；
   - 推理题（纸面）：`test_pool(16, 5)` 的池里，`alloc()` 两个状态、各自 `ensure_capacity(16)`，随后 `padding_page_id()` 返回值会变吗？.available_pages() 此时应显示多少？
   - 有 GPU 时可把上述推理写成断言加进 `mod tests`（示例代码，非项目原有）：
     ```rust
     let pool = test_pool(16, 5);
     let pid = pool.padding_page_id();
     let mut a = pool.alloc(); a.ensure_capacity(16).unwrap();
     let mut b = pool.alloc(); b.ensure_capacity(16).unwrap();
     assert_eq!(pool.padding_page_id(), pid);   // 页号恒定
     assert_eq!(pool.available_pages(), 2);     // 5 - 1(padding) - 1(a) - 1(b)
     ```
3. **需要观察的现象**：padding 页号在任意分配序列前后不变；可用页账目始终满足 `capacity - 1(padding) - Σ持有`。
4. **预期结果**：断言全过——但 kv_pool 测试需 GPU（`DeviceContext::new`），无 GPU 环境时以上为「待本地验证」。
5. 生命周期测试同时展示了 `reset`（测试专用，[L410-L420](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L410-L420)）如何整 permit 换新来「归还全部」。

#### 4.3.5 小练习与答案

**练习 1**：padding 页为什么必须构造期 `expect` 扣下，而不能「首次 decode 需要空槽时再申请」？

**答案**：decode 图在首次执行时就把页表数组的内容（含空槽页号）录制进 kernel 参数；此后每次重放原样重发。若空槽页号来自运行期申请，两次 decode 之间页可能流转/复用，图里的旧页号指向别人的 KV——静默读错数据。构造期扣下则页号终身恒定；而 `expect` 的 panic 属启动期 fail-fast：留不出 padding 页的池无法服务任何图，应当在 boot 时炸而不是热路径上坏。

**练习 2**：假设允许运行期把 padding 页短暂借给某个真实请求、用完再还（页号仍恒定），会破坏什么？

**答案**：至少三重破坏：① 借出期间图回放的空槽正在读写该页——与真实请求的 KV append 形成数据竞态；② 还回来时页内容已被真实 KV 污染，padding 行的输入不再是「无害且确定的垃圾」，byte 稳定性丧失（EP 下这直接违反 padding 即协议，见 4.4）；③ 借还窗口内若发生准入失败/panic，页可能悬在中间态。所以设计上 padding 页根本不进分配面——`padding_permit` 永不参与 `try_reserve`。

**练习 3**：`padding_page_id()` 返回 `i32` 而不是 `PageId`/`usize`，透露了它的消费方在哪里？

**答案**：消费方是 GPU 侧的页表数组——调度器把它压进 `Vec<i32>`（宿主侧规划）再上传给 kernel（u2-l4 讲过「数据不动、索引动」，i32 索引数组就是那条通道）。`PageId(usize)` 是池内的不透明句柄，一旦要上图就得转成 kernel ABI 的 i32；直接以 i32 出口是在类型上声明「这个值生来就是给页表数组用的」。

### 4.4 EP free-running 不变量：单卡纪律的多卡放大

#### 4.4.1 概念说明

前三个模块都在单卡（或单进程）内。EP（专家并行）把纪律压力放大几个量级：每层 forward 都有全 fleet 参与的 dispatch/combine 集合通信，**任何两个 rank 的步调错开一位，产出的就是「字节确定性的垃圾」且不 crash**（mispair，最难排查的失败模式）。仓库的应对写在两处：

- [CLAUDE.md:L113-L120](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/CLAUDE.md#L113-L120) 的 **EP Free-Running Discipline**（仓库级声明，四条不变量，违反任何一条即死锁设计）；
- 规范文档 [docs/models/glm52/free-running-dp.md](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/models/glm52/free-running-dp.md)（「Canonical doc」，含五道 kernel gate 的实测验收）。

四条不变量原文（CLAUDE.md L117-L120）：① **No rank ever stops or waits**——engine loop 无条件全速跑，EP 协调里的任何静默等待（condvar、sleep-poll）都是 bug；② **每步集体链固定**——不允许条件性 collective，省活要靠 kernel 内零负载进场，不靠 host 协商；③ **launch 次数即全局时钟**（配对把所有 rank 钉在 ±1 launch 内）——协调意味着**事先约定第 N 步装什么**，绝不是「等大家都就绪」；④ **padding 行是协议面**——它的字节会到达对端，每个 dummy 输入必须构造性确定。

本模块的任务是建立映射：这三四条不是凭空发明的新规矩，而是 core 单卡纪律在多卡下的**同构放大**。

#### 4.4.2 核心流程

映射表（本讲核心产出）：

| 单卡 core 纪律 | 源码锚点 | EP free-running 不变量 | 规范锚点 |
|---|---|---|---|
| 准入全是 `try_*` 非阻塞：`None` 就排队，loop 不等待 | kv_pool.rs:L274-L283 | ① 不停止不等待：idle rank 以 padding 行全速进场 | CLAUDE.md:L117 |
| 决策与生效分离：先全部 reserve（纯 host 决策）、后统一 commit（生效） | kv_pool.rs:L278-L283 / L380-L384 | ③ 先约定第 N 步内容、绝不等待就绪：协调发生在 launch 之前 | CLAUDE.md:L119；free-running-dp.md:L61-L63（「不变量从运行时协议保证变成代码结构保证」） |
| padding 页构造期预留、页号恒定、内容无害 | kv_pool.rs:L117-L125 / L226-L229 | ④ padding 即协议面：字节上 wire，输入必须构造性确定，有 byte-stability gate 守护 | CLAUDE.md:L120；free-running-dp.md:L145-L149 |
| 图回放「内容动地址不动」：捕获期禁分配、页表原地重填 | cuda_graph.rs:L96-L104 | ② 固定链：`num_tokens` 等 kernel 实参每步变化会破坏 whole-step graph，空 rank 选整 bucket 进场 | free-running-dp.md:L154-L157（路 A/路 B） |

一句话读法：**单卡把「不可撤销的 GPU 动作」（图回放、KV 写入）与「可撤销的 host 决策」（reserve/plan）切开；EP 把同一刀切在 collective 上——launch 之前的约定怎么改都行（本地的、可回滚的），launch 之后没有任何协商余地（全局的、不可撤销的）。**

#### 4.4.3 源码精读

**「先约定、绝不等待」的原文依据**——[docs/models/glm52/free-running-dp.md:L61-L63](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/models/glm52/free-running-dp.md#L61-L63)：因为每步的 collective 链由模型代码写死、顺序无自由度，「所有 rank 对 step 计数一致」这一不变量退化为「loop 无条件跑」——「**不变量从运行时协议保证变成代码结构保证**」。这与 `KvReservation` 的设计完全同构：两段式的本质也是把正确性从「运行期手工回滚协议」变成「Drop 的代码结构保证」。

**三条纪律（承重墙）**——[docs/models/glm52/free-running-dp.md:L138-L149](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/models/glm52/free-running-dp.md#L138-L149)：固定链纪律（省 collective 的唯一正确姿势是零负载进场）、形状本地纪律（buffer 按协议最大值保守 bound，不依赖「别人此刻的真实行数」）、**padding 即协议**——最后一条的原文值得逐字读：「禁止『输出会被丢弃所以输入无所谓』——**输出被丢弃，但路由和字节已经上了 wire，影响的是别人的 step**」。对照 core：`KvPoolInner` 文档说 padding 页「KV append writes harmless garbage; attention output is discarded」——单卡下「无害垃圾」成立的前提恰恰是**这些字节不上 wire**；一旦上 wire（EP），同样的行必须升级为构造性确定，且由 [gate 4（L228-L236）](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/models/glm52/free-running-dp.md#L228-L236)用 460 个路由快照逐字节验证（实测空 rank 每 bucket 分组内字节逐步恒定）。

**padding 的图安全姿势**——[docs/models/glm52/free-running-dp.md:L150-L160](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/models/glm52/free-running-dp.md#L150-L160)：§5 把 padding corner cases 展开，其中「路 A vs 路 B」（L154-L157）是模块 4.3 的多卡版论证：`num_tokens=0` 语义干净但它是 kernel 实参、每步变化会破坏 whole-step graph（对应 cuda_graph.rs 的「捕获闭包禁分配/参数须稳定」）；所以空 rank 选**整 bucket dispatch 进场**——与 gemma4 让空槽指向恒定 padding 页是同一个「让变化的批量长成固定形状」的思想。glm52 的 `GLM52_PADDING_STEP` 契约（固定 token、position 0、seq_len 1、写 padding page 位置 0）与 core `padding_page_id()` + 一 token 窗口（serve.rs:L2205-L2212）几乎逐字段同构。

**失败模型与 fate-sharing**——[docs/models/glm52/free-running-dp.md:L169-L181](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/models/glm52/free-running-dp.md#L169-L181)：单卡哲学是 fail-fast（构造期 `expect`、执行期 `bail!`）；EP 把 fate-sharing 变成物理事实——一步失败 collective group 无法重新对齐，全 fleet 经超时 fail-stop。core 侧没有「优雅降级后继续」的路径，正是为这个模型准备的：**局部错误要么在 launch 前消化（reserve 失败排队），要么整进程退出**，不存在「带病继续跑」。

**纪律的工程表述**——[CLAUDE.md:L113-L120](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/CLAUDE.md#L113-L120)：注意 L115 点名「K3's gang lane follows it」——这套纪律已从 glm52 推广到 K3 的 gang lane，是跨模型线的仓库级约定，不是单一模型的实现细节。

#### 4.4.4 代码实践

1. **实践目标**：亲手完成「单卡机制 → EP 不变量」的映射论证，检验自己真的能迁移而不是背诵。
2. **操作步骤**：
   - 通读 [docs/models/glm52/free-running-dp.md](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/models/glm52/free-running-dp.md) 的 §2（不可约同步分析）、§4（三条纪律）、§5（padding corner cases）、§8 的 gate 4/5 段落；
   - 把 4.4.2 的映射表抄成空表（只留左列和右列标题），凭记忆+回看源码填满中间两列，每格必须带一个文件+行号锚点；
   - 回答联动题（写进笔记）：若某空 rank 的 admission 实现成了「等 global 池有空页再继续」（一个静默 wait），在 free-running fleet 里会发生什么？
3. **需要观察的现象**：填表时你会发现右列每条都能在左列找到结构相同的单卡对应物——这就是「同构放大」的实证。
4. **预期结果**：联动题参考答案——该 rank 的 loop 停摆 → 它不再按节拍进入每层的 dispatch/combine → 对端 rank 的 collective 凑不齐参与者 → NVLink/NCCL barrier 超时 → 按 §6 失败模型全 fleet fail-stop（文档实测杀 peer 后约 17s 超时、进程退出）。所以 CLAUDE.md L117 说「任何静默等待都是 bug」不是风格偏好，而是死锁定理；core 侧对应的防线就是「准入永远 try-then-decide、loop 永不等待」。
5. 文档阅读无运行依赖；gate 复跑需 GB300 硬件（§8 命令），本实践不要求。

#### 4.4.5 小练习与答案

**练习 1**：为什么「等对端就绪」（条件变量/sleep 轮询）在 EP 协调里被定性为 bug，而在单卡调度里 `try_reserve` 返回 `None` 后排队下一轮却完全正确？

**答案**：区别在「等」的对象。单卡排队等的是**本地资源**，loop 本身继续转，别的请求照常被服务，资源释放是本地事件、必然发生（RAII 保证）；EP 静默等待等的是**对端行为**，而「等」这个动作本身就让你退出了 collective 的固定节拍——你在等的人也在等你（或等第三方），集体链断开，失败以超时形式传染全 fleet。free-running-dp.md §2 的结论：唯一删不掉的同步是 collective 本身，其余一切（admission、KV pool……）必须 rank-local 且非阻塞。

**练习 2**：core 的 padding 页内容是「无害垃圾」，EP 文档却要求 padding 行输入「构造性确定」并有 byte-stability gate——矛盾吗？

**答案**：不矛盾，是两个不同的字节域。core padding 页存的是 **KV 值**，单卡下这些值只被本地的 attention 读、输出被丢弃，所以「垃圾」无害；但决定 padding 行**行为**的输入（token id、position、seq_len、页号——即路由和寻址元数据）在任何部署下都必须确定，否则图回放/调度本身就错。EP 下 dispatch 把整行（含由这些输入决定的路由选择）发给对端，垃圾值也会变成 wire 字节影响别人的 step，于是连 KV 值域都被 gate 4 纳入恒定验证。core 在类型上已把「寻址确定性」做对（页号恒定、seq_len=1 的固定窗口），值域的确定性是 EP 层加码。

**练习 3**：用一句话说清 `try_reserve → commit_reservation` 与「先约定第 N 步内容、绝不等待」的对应关系。

**答案**：两者都是把**可撤销的决策（reserve / step 规划）全部放在不可撤销的动作（commit / collective launch）之前**——reserve 阶段怎么失败都只是本地回滚（drop 预约），launch 之后不存在任何协商，所以正确性不依赖任何运行期「等待-重试」协议，而由代码结构直接保证。

## 5. 综合实践

写一篇短文（建议 600–1000 字，含代码引用与行号链接），三个部分覆盖本讲全部模块：

1. **反例调度序列**：设定 local 池剩 2 页、global 池剩 2 页；请求 R1 需 2+2，请求 R2 已持 1 页 global、只差 1 页 local 才能跨页。假设准入只有单段（直接 `ensure_capacity` 逐池生长），逐步写出资源状态，指出循环等待在哪一步形成、为什么 permit 的 RAII 也救不了它（提示：RAII 还页发生在 permit **drop** 时，而「半准入」的请求 permit 还活着——这正是 [kv_pool.rs:L274-L277](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L274-L277) 文档说的「Growing a request's own permit instead cannot be undone」）；再对照 [pegainfer-gemma4/src/kv.rs:L397-L407](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-gemma4/src/kv.rs#L397-L407) 的 drop 回滚说明两段式如何切断它。
2. **协议对应**：指出 `KvReservation` 的 reserve/commit 两半与 EP 文档「先约定第 N 步内容、绝不等待」（[CLAUDE.md:L119](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/CLAUDE.md#L119)）的结构同构，并引用 [free-running-dp.md:L61-L63](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/models/glm52/free-running-dp.md#L61-L63) 的「不变量从运行时协议保证变成代码结构保证」作结。
3. **无泄漏审计**：完成 4.2.5 练习 3 的审计表并核对你自己的版本——kv_pool.rs 每条 early-return/bail/panic 路径（L83-L88、L204-L218、L221-L224、L227-L229、L367-L372、L400-L403）都要指认「此刻谁持有页、失败后页回哪」，结论应是无泄漏。

**验收标准**：短文里的每个论断都有一个可点击的源码锚点；反例部分能画出单段/两段两条时间线的页占用对照；审计表六行俱全。全程纸面可完成，无 GPU 依赖（涉及运行的一律标注「待本地验证」）。

## 6. 本讲小结

- **两段式准入**：`try_reserve`（schedule 半段，`Option` 语义，未绑定请求）→ `commit_reservation`（apply 半段，`absorb` 强制同池）；任一池失败即 drop 全部预约回滚，两个池回到请求前占用率（gemma4 `admit_tokens` 文档原文）。防御的失败模式：多池下「占了 A 等 B」的部分占用死锁。
- **RAII 归还**：页的所有权只存在于 `OwnedPagePermit`，「检查与弹出同临界区」给 try_* 原子性，Drop 给零泄漏；每条提前退出路径（含 `ensure_capacity` 的 bail、池构造的 panic）都经得起审计。cuda_graph.rs 的捕获排水路径是 FFI 处的手工等价物。
- **padding 确定性**：padding 页在池构造期 `expect` 一次性扣下、永不外借、页号终身恒定（`padding_page_id()` 为常量），空槽配 seq_len=1 的一 token 窗口；与两段式准入合流成「图录制时见过的页号在其生命周期内永远合法」。
- **EP free-running**：四条不变量（不停止、固定链、launch 计数即时钟、padding 即协议面）是上述单卡纪律的同构放大——可撤销决策全在 launch 前，launch 后零协商；padding 字节上了 wire 就必须构造性确定并由 gate 守护。
- 一以贯之的问题意识：**失败路径还资源吗？占了一半会卡住谁？这段字节会被别人看到吗？**

## 7. 下一步学习建议

- **下一讲 u5-l2（测试策略与二次开发）**：本讲的审计表和反例推演正是「为一个 core 改动选择正确验证层级」的素材——两段式准入、Drop 回滚这类行为该用什么测试锁住，将在那里展开。
- **延伸阅读**：① [docs/models/glm52/free-running-dp.md](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/models/glm52/free-running-dp.md) 全文（尤其 §8 五道 gate 的验收方法——纪律不是宣言而是被测出来的）；② [pegainfer-gemma4/src/kv.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-gemma4/src/kv.rs) 的完整准入与滑窗逐页释放逻辑（4.1 的消费现场全貌）；③ 回看 [page_pool.rs:L132-L194](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/page_pool.rs#L132-L194) 的三个内嵌测试，体会「纯 CPU 测试锁并发语义」的分层（u1-l3 的 GPU 隔离判据在这里落地）。
- **动手方向**：如果想让本讲的纪律长出肌肉记忆，试着为 4.1.4 的反例写一个可编译的最小复现（示例代码，放独立环境，勿改仓库源码），用两个假想池 + 单段准入复现循环等待，再换两段式验证消解——这正好是 u5-l2 演练贡献的预演。
