# page_pool.rs：固定页分配器与 RAII 许可

## 1. 本讲目标

本讲精读 `pegainfer-core/src/page_pool.rs`——整个 crate 里行数最少、却垫在最底层的一块地基（195 行，含 63 行内嵌测试）。学完后你应该能够：

- 解释 **RAII permit** 如何把"还页"变成编译器强制的义务，从而消除手动归还的遗漏风险（包括 `?` 提前返回与 panic 路径）。
- 手推 `absorb`、`try_grow`、`Drop` 三个操作对 free-list 的**精确影响**，包括元素的进出顺序。
- 说出 `PagePool` / `OwnedPagePermit` / `PageId` 三者的分工，以及它们在 `kv_pool.rs` 里的真实消费方式——尤其是 padding 页为什么在构造期就被永久租走。
- 结合源文件内嵌的三个单元测试理解 acquire/drop 的恢复语义，并亲手补两个新测试锁住尚未覆盖的性质。

承接前文：u2-l4 我们看到 core 用 `KvDesc` 向 kernels 描述"扁平页表 + 末页长度"，并立下契约"页号只在签发它的池内有效"。但当时没有回答一个更底层的问题——**宿主侧是谁在记账"第 3 页现在归请求 A 所有"？** 本讲就是这个问题的答案。

## 2. 前置知识

- **RAII 与 `Drop`**：Rust 的"资源获取即初始化"。值离开作用域（正常结束或 panic 栈展开）时 `Drop::drop` 自动执行，C++ 的 `unique_ptr` 析构是同款模式。本讲中"资源"就是页的所有权。
- **`Arc<T>`**：原子引用计数的共享指针。`PagePool` 的 `Clone` 不复制数据，只是多一个指向同一内部状态的句柄。
- **`parking_lot::Mutex`**：比标准库更轻的互斥锁，`lock()` 返回守卫，守卫存活期间临界区独占。u1-l3 已确认它是 workspace 依赖之一。
- **`Vec` 当栈用**：`push` 进尾、`pop` 出尾，均为 \( O(1) \)。free-list 就是一个 `Vec<PageId>`，配的就是这套栈操作。
- **KV cache 分页**（回顾 u2-l4 / u1-l1）：每个请求的 KV 状态按固定大小的"页"切分，一页容纳 `page_size` 个 token 在**全部层**的 K 与 V；请求持有一组页编号，页在池内从 0 开始编号。
- **"构造性确定性"（constructively deterministic）**：出自 [CLAUDE.md](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/CLAUDE.md) 的 EP free-running 纪律——"every dummy-row input must be constructively deterministic"。本讲会看到这条多卡纪律在单卡内存分配上的第一个投影。

## 3. 本讲源码地图

| 文件 | 行数 | 本讲中的角色 |
|------|------|-------------|
| [src/page_pool.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/page_pool.rs) | 195 | 唯一精读对象：分配器本体 + 内嵌单测（L132 起的 `mod tests`） |
| [src/kv_pool.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs) | 563 | 消费方：`KvPoolInner` 持有 `PagePool` 与 `padding_permit`；下一讲（u3-l2）的主对象 |
| [src/lib.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/lib.rs) | 15 | [lib.rs:11](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/lib.rs#L11) 处 `pub mod page_pool;`，13 个模块之一 |
| [CLAUDE.md](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/CLAUDE.md) | — | EP free-running 纪律（"Padding rows are protocol surface"）的事实来源 |
| [free-running-dp.md](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/models/glm52/free-running-dp.md) | — | EP 纪律的规范文档；本讲只借其单卡投影，细节留待 u5-l1 |

整个 `page_pool.rs` 的骨架一目了然：

```text
PagePool (Arc<PagePoolInner>)                   ← 共享句柄，Clone 廉价
  └─ PagePoolInner { capacity_pages, free_list: Mutex<Vec<PageId>> }
OwnedPagePermit { inner, pages: Vec<PageId> }   ← RAII 租约，Drop 归还
PageId(usize)                                   ← 不透明页编号
```

## 4. 核心概念与源码讲解

### 4.1 PageId：不透明的页编号

#### 4.1.1 概念说明

`PageId` 是一个**单字段 newtype**：内部就是一个 `usize`，但类型系统只允许你持有它、比较它、传递它；想变回整数，只有 core 内部的一条窄门 `index()`。为什么要包一层？因为这个 crate 里 `usize` 到处都是——token 下标、层号、元素偏移、字节长度。裸用 `usize` 当页号，迟早会把"第 5 个 token"误传给"第 5 页"的接口；newtype 让这类混淆在编译期暴露。

#### 4.1.2 核心流程

`PageId` 没有执行流程，只有一组类型约束：

- `Copy`：值语义，传参零成本。
- `Eq + Hash`：可作 `HashSet`/`HashMap` 的键（将来做页去重、前缀缓存时直接可用）。
- `Debug`：测试断言与诊断打印。
- `index()` 是 `pub(crate)`："页号 → 整数"的转换被锁在 crate 内部，且构造函数 `PageId(...)`（元组结构体语法）对 crate 外同样不可见——外部既造不出也拆不开。

#### 4.1.3 源码精读

- [src/page_pool.rs:L24-L32](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/page_pool.rs#L24-L32) 定义不透明页号：`PageId(usize)` 带全套 derive，唯一方法 `index(self) -> usize` 标注 `pub(crate)`。字段没有 `pub`，所以 crate 外既不能构造也不能读取它。
- crate 内唯一的构造点是 [src/page_pool.rs:L36](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/page_pool.rs#L36) 的 `(0..capacity_pages).rev().map(PageId)`——只有池自己能"造"页号。
- 全 crate 把页号转成喂给 kernel 的 `i32` 的地方在消费方：[src/kv_pool.rs:L296-L298](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L296-L298)（`extend_page_indices_i32`，走 `p.index() as i32`）。

#### 4.1.4 代码实践

1. **实践目标**：验证 `PageId` 的封装强度。
2. **操作步骤**：在你的本地克隆里，往 `page_pool.rs` 的 `mod tests` 临时加一行 `let _: usize = PageId(0).index();`（同 crate 内合法）；再试着想象把同样的代码放进 `tests/` 目录的集成测试文件里。
3. **需要观察的现象**：集成测试是独立 crate，`index()` 与 `PageId(...)` 构造都不可见。
4. **预期结果**：集成测试版本编译失败（E0624 / 私有字段错误）。`pub(crate)` 把"造页号、读页号"的能力锁死在 core 内部。待本地验证（如果你动手试了编译错误信息）。

#### 4.1.5 小练习与答案

1. **问：为什么 `index()` 不做成 `pub`？**
   答：一旦公开，下游就能对页号做算术（如 `id + 1` 猜下一页），绕过分配器直接索引共享 GPU buffer，破坏"页进出必须经过池"的所有权纪律。收窄到 `pub(crate)` 后，唯一的出口是 `kv_pool.rs` 转 kernel 需要的 `i32`。
2. **问：`Hash` derive 目前没有消费点，为什么带着？**
   答：为未来机制（页集合去重、前缀缓存键）预留；derive 零成本，且让 `PageId` 可放进任何标准容器。
3. **问：u2-l4 说"页号只在签发它的池内有效"，`PageId` 的哪个设计在配合这条契约？**
   答：不携带池信息、不可构造、不可拆箱——页号无法被伪造或误造，只能从某个池的 free-list 里"租"出来，因此它的有效性天然以池为前提（`kv_pool.rs` 的 `belongs_to` 与本讲的 `absorb` 断言都是在运行时复核这一点）。

### 4.2 PagePool：锁保护 free-list 的共享固定页分配器

#### 4.2.1 概念说明

`PagePool` 是**固定容量**的页分配器：构造时决定总页数（此后不变），运行期的全部活动只是"从 free-list 借出"与"放回"。"固定"不是偷懒——它来自 KV 池的几何：`kv_pool.rs` 在构造期用模型几何算出 `num_pages` 并一次性分配整块 GPU buffer（[src/kv_pool.rs:L221-L224](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L221-L224)），页数与显存一一对应，运行期没有"多造一页"这个选项。

结构上是"句柄 + 内部状态"两件套：`PagePool` 本体 `Clone`（复制 `Arc` 指针，可发给每个 `KvState`）；`PagePoolInner` 只有两个字段——不变的 `capacity_pages` 与唯一可变状态 `free_list: Mutex<Vec<PageId>>`。

还有一个贯穿全文件的可见性设计：**类型是 `pub` 的，方法全是 `pub(crate)`**。模型 crate 能在签名里提到 `PagePool` 这个名字，却无法 `new`、无法 acquire——页的进出只发生在 core 内部的 `kv_pool` 模块。这是"类型公开、能力不公开"的手法，也直接决定了本讲实践必须写成内嵌单测（见 4.4）。

#### 4.2.2 核心流程

`new` 与 `try_acquire_many` 的关键全在**顺序**：

```text
new(capacity):
  free_list = [capacity-1, capacity-2, ..., 1, 0]   # rev 收集
                              ↑ 栈顶（Vec 尾部）是 PageId(0)

try_acquire_many(n):
  n == 0     → 返回空 permit（不拿锁、永不失败）
  锁 free_list
  剩余 < n   → 返回 None（检查与取页在同一临界区，无插队窗口）
  弹出 n 页  → 从尾部弹，先拿到 PageId(0)，再 1、2……升序
  返回 Some(permit)
```

由此读出两条不变量：

1. **分配顺序确定性**：同一池、同一操作序列，任何一次运行发放的页号完全一致；首次 acquire 拿到的一定是 `PageId(0)`。
2. **全有或全无**：要么整批拿到 `n` 页，要么 `None`，绝不"拿到一半"。

加上归还方（4.3），任意时刻成立守恒律：

\[ \text{available\_pages}(t) \; + \sum_{p \,\in\, \text{全部存活租约}} \text{len}_p(t) \;=\; \text{capacity\_pages} \]

所有修改都发生在同一把锁内，所以这条等式在任意线程交错下都成立——这正是调度器敢于用 `available_pages()` 做准入判断的前提。单页操作 \( O(1) \)，一次 acquire 的代价是 \( O(n) \) 次弹栈加一次锁。

#### 4.2.3 源码精读

- [src/page_pool.rs:L5-L15](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/page_pool.rs#L5-L15) 定义 `PagePool`（doc 一句话点题 "Shared fixed-page allocator with RAII-owned permits"）与 `PagePoolInner`；`#[derive(Clone, Debug)]` 说明克隆只是复制 `Arc`。
- [src/page_pool.rs:L34-L43](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/page_pool.rs#L34-L43) 构造函数：`(0..capacity_pages).rev().map(PageId).collect()` 把降序区间收进 Vec，使**栈顶恰好是 0 号页**。
- [src/page_pool.rs:L45-L67](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/page_pool.rs#L45-L67) `try_acquire_many`：`n == 0` 在拿锁**之前**返回空 permit；随后在**同一锁守卫存活期间**完成"检查剩余量 → 弹出 n 页"，L60 的 `expect("free_list length checked above")` 把"此处不可能失败"的推理写成断言。
- [src/page_pool.rs:L69-L75](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/page_pool.rs#L69-L75) 两个只读探针：`capacity_pages()` 读不变量（不拿锁），`available_pages()` 拿锁读长度。调度器用后者决定还能不能收请求（`kv_pool.rs` 在 [src/kv_pool.rs:L261-L267](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L261-L267) 原样转发这两个探针）。

#### 4.2.4 代码实践

1. **实践目标**：用只读探针"看见"全有或全无语义。
2. **操作步骤**：阅读内嵌测试 `reports_insufficient_capacity_with_none`（[src/page_pool.rs:L165-L171](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/page_pool.rs#L165-L171)）：池容量 2、`_permit` 已持 1 页（可用剩 1），再 `try_acquire_many(2)`。
3. **需要观察的现象**：返回 `None`，且失败的申请没有消耗任何页。
4. **预期结果**：`is_none()` 断言通过。这正是 `kv_pool.rs` 两段式准入敢于"先从每个池 reserve、失败靠 drop 回滚"的机械前提——依据是 [src/kv_pool.rs:L274-L283](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L274-L283) 的 doc 注释："reserve from every pool first, commit only when all reservations hold, and let a failure roll the rest back by drop"。

#### 4.2.5 小练习与答案

1. **问：`new(4)` 之后 free_list 的内容是什么？为什么要 `rev()`？**
   答：`[3, 2, 1, 0]`。`pop` 从 Vec 尾部取，倒序填充让 0 号页位于栈顶，分配顺序天然升序——这是"分配顺序确定性"的机械来源。
2. **问：`try_acquire_many(0)` 为什么在拿锁前特判？这个分支被谁依赖？**
   答：零页申请没有临界区工作，提前返回省一次锁且永不失败。[src/kv_pool.rs:L242-L248](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L242-L248) 的 `KvPool::alloc()` 靠它给每个新请求造一个合法的零页初始租约——"请求一出生就持有（空的）租约"。
3. **问：两个线程同时 `try_acquire_many(3)` 与 `try_acquire_many(2)`，池剩 4 页，可能的结果集合是什么？**
   答：要么"3 成 2 败"，要么"2 成 3 败"（看谁先拿锁）；不可能两个都成功（4 < 3+2），也不可能任何一个部分成功——检查与取页在同一临界区内，操作线性可序化。

### 4.3 OwnedPagePermit：RAII 租约、absorb、try_grow 与 Drop 归还

#### 4.3.1 概念说明

`OwnedPagePermit` 是"一纸租约"：持有它就持有若干页，**丢掉它就等于还页**。字段只有两个——指回池的 `Arc<PagePoolInner>` 与名下的 `pages: Vec<PageId>`。没有 `release()`、`return_pages()` 之类的手动 API，归还只有一个入口：`Drop`。这就是 RAII 消除"手动归还遗漏"的方式：

- 函数中途 `?` 提前返回？局部 permit 离开作用域，Drop 执行，页归还。
- 线程 panic？栈展开逐帧执行 Drop，页归还。
- 忘了还？不可能——除了所有权转移（`absorb`），页跟着值走，值没了页就回来了。

`kv_pool.rs` 把这个性质直接写进了设计：`KvReservation` 的 doc 是 "Returns its pages on drop unless committed"（[src/kv_pool.rs:L286-L290](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L286-L290)）——失败路径根本不写清理代码，靠 Drop 自动回滚。

三个操作各自的语义：

- **`absorb(other)`**：把另一份租约整体吞并。`other` 按值传入、`pages` 被搬空，随后丢弃时命中 Drop 的空提前返回，**不会重复还页**；此后只有吸收方的 Drop 会归还全部页。跨池吸收被 `Arc::ptr_eq` 断言拦下——"页还错 free-list"是无法用类型系统表达的错误，只能用运行时断言守卫（u2-l4 的契约"页号只在签发池内有效"在这里被强制执行）。
- **`try_grow(n)`**：给当前租约追加 n 页。成功返回 `true`；失败（剩余不足）返回 `false` 且**租约原封不动**。注意它扩张的是"租约持有的页集合"，不是池容量——池容量在 `new` 后就冻结了。
- **`Drop`**：按 `pages` 的**逆序** `extend` 回 free-list。方向不是随手写的：`pages = [0, 1]` 逆序还回后 0 号页位于栈顶，下次分配又先拿到 0——升序发放、逆序归还、再升序发放，页的**复用顺序也是确定的**。

为什么准入路径用 `absorb`（reserve → commit）而不是直接 `try_grow`？`try_reserve` 的 doc 给出了答案（[src/kv_pool.rs:L274-L277](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L274-L277)）："Growing a request's own permit instead cannot be undoed"——`try_grow` 借出即刻生效、无法回滚；多池原子准入需要"全部预留成功才提交"，而 reservation 失败时 drop 即回滚。commit 半段就是 [src/kv_pool.rs:L380-L384](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L380-L384) 的 `commit_reservation`，一行 `self.permit.absorb(reservation.permit)`。而 decode 循环里请求自然增长用的是 `try_grow`：[src/kv_pool.rs:L360-L374](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L360-L374) 的 `ensure_capacity`，失败时 `bail!("KvState: out of pages ...")`。

#### 4.3.2 核心流程

一次"申请 → 合并 → 归还"的完整生命周期（capacity = 4）：

```text
初始:        free = [3,2,1,0]
A=acq(1):    A.pages=[0]      free = [3,2,1]
B=acq(2):    B.pages=[1,2]    free = [3]
A.absorb(B): A.pages=[0,1,2]  free = [3]      ← 页不过池，B 被搬空后无害消亡
drop(A):     [2,1,0] 逆序入栈  free = [3,2,1,0] ← 与初始逐位相同
下次 acq(4): 拿到 [0,1,2,3]
```

absorb 的合并细节：

```text
A.pages=[0], B.pages=[1,2]        （同池，assert Arc::ptr_eq 通过）
A.absorb(B):
  A.pages = [0, 1, 2]             ← Vec::append 保序拼接
  B.pages = []                    ← append 顺带搬空 B
B 随即消亡 → Drop 见空列表提前返回，不还页
此后这 3 页的归还完全由 A 的 Drop 负责（一次性逆序放回）
```

注意 absorb 全程不碰 free-list：页从一份租约直接转移到另一份，不存在"先还回池、再被别人抢走"的窗口——这是 commit 半段原子性的来源。

#### 4.3.3 源码精读

- [src/page_pool.rs:L17-L22](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/page_pool.rs#L17-L22) `OwnedPagePermit` 的定义，doc 一句话讲完全部语义（"A set of pages leased from a PagePool. Pages return automatically on drop."）；它**不 `Clone`**——租约不可复制，页的所有权独占。
- [src/page_pool.rs:L78-L87](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/page_pool.rs#L78-L87) 只读接口 `pages()` / `len()`。L83 的注释是个小窗口：`is_empty` 曾是死代码，在一次代号 hawk 的清扫（#743）中被删除，留下 `len` 并配 `#[allow(clippy::len_without_is_empty)]` 压住 clippy——透露了这个仓库"不留死代码"的卫生习惯。
- [src/page_pool.rs:L89-L97](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/page_pool.rs#L89-L97) `absorb`：先 `assert!(Arc::ptr_eq(...), "absorb requires permits from the same pool")`（doc 明言 "merging across pools would return pages to the wrong free list"），再 `self.pages.append(&mut other.pages)`。
- [src/page_pool.rs:L99-L117](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/page_pool.rs#L99-L117) `try_grow`：拿锁 → 检查剩余 → 不够就 `return false`（此刻什么都没动）→ 够则 `reserve` 后逐页弹栈追加。与 `try_acquire_many` 同款的全有或全无。
- [src/page_pool.rs:L120-L130](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/page_pool.rs#L120-L130) `Drop for OwnedPagePermit`：空列表提前返回（被吸收后的旧租约、零页租约都走这条捷径，省一次锁）；否则 `std::mem::take` 把页清单整体取走，再拿锁 `free_list.extend(pages.into_iter().rev())` 逆序放回。

#### 4.3.4 代码实践

1. **实践目标**：亲眼确认"吸收后只有吸收方还页"与"grow 失败无副作用"。
2. **操作步骤**：阅读内嵌测试 `try_grow_extends_permit_and_returns_all_on_drop`（[src/page_pool.rs:L173-L193](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/page_pool.rs#L173-L193)），注意 L186-188：超额 grow 失败后立刻断言租约长度仍是 3、可用仍是 1。
3. **需要观察的现象**：grow 成功时页**保序追加**到租约尾部；失败时租约与 free-list 都不变；drop 时含 grow 来的页一起归还。
4. **预期结果**：三个断言组全部通过。absorb 版的对应测试在综合实践（第 5 节）里亲手补上。待本地验证。

#### 4.3.5 小练习与答案

1. **问：`absorb` 为什么必须断言同池？跨池吸收会坏在哪里？**
   答：归还时页被 push 进租约自己那份 `inner.free_list`（[src/page_pool.rs:L127](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/page_pool.rs#L127)）。跨池吸收后，A 池的页会随 B 池租约在 B 池的 free-list 里"复活"，A 池从此少页——同一页被两方同时持有。`Arc::ptr_eq` 在第一时间 panic，把错误挡在测试期。（同款检查在 [src/kv_pool.rs:L349-L353](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L349-L353) 的 `belongs_to` 里还出现一次。）
2. **问：`try_grow(2)` 失败后租约处于什么状态？依据哪几行？**
   答：原封不动。[src/page_pool.rs:L106-L109](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/page_pool.rs#L106-L109) 在锁内先检查 `free_list.len() < n` 即返回 `false`，此刻 `self.pages` 尚未被碰过；`reserve`/`push` 全在检查之后。
3. **问：`Drop` 里为什么是 `extend(rev)` 而不是顺序 `extend`？**
   答：设 `pages = [0, 1]`，顺序还回会让 free-list 尾部变成 `[..., 0, 1]`，下次 pop 先拿到 1——复用顺序开始抖动。逆序还回让 0 号页最后入栈、位于栈顶，下轮继续升序发放。若删掉 `rev`，内嵌测试 L159-162 的 `[0,1,2,3]` 断言就会失败（可自行推演：会得到 `[1,0,2,3]`）。顺序确定性是"构造性确定性"纪律的一部分。
4. **问：为什么 `Drop` 要先 `std::mem::take` 再拿锁？**
   答：把页清单整体移入局部变量、`self.pages` 置空，锁内归还逻辑只消费局部值；Drop 体内对租约字段只有一次访问，归还不可能被重复触发，且临界区缩到最短。

### 4.4 内嵌单元测试：三个测试就是三份契约

#### 4.4.1 概念说明

`page_pool.rs` 自带三个单测（[src/page_pool.rs:L132-L194](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/page_pool.rs#L132-L194)）。呼应 u1-l3 的判据"构造了什么"：这些测试**不构造任何 GPU 资源**（没有 `DeviceContext`、没有 `CudaSlice`），是纯宿主数据结构测试，落在 `cargo test --release -p pegainfer-core --lib` 的快车道里，无 GPU 机器也能跑，也不需要任何 `#[ignore]`。

| 测试 | 验证的契约 |
|------|-----------|
| `acquires_pages_and_restores_on_drop` | 分配升序；drop 后完整恢复；恢复后再整批分配仍升序取满 |
| `reports_insufficient_capacity_with_none` | 剩余不足返回 `None`，不产生部分分配 |
| `try_grow_extends_permit_and_returns_all_on_drop` | grow 追加保序；超额 grow 失败且租约不变；drop 归还全部（含 grow 来的页） |

#### 4.4.2 核心流程

以第一个测试为例（[src/page_pool.rs:L137-L163](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/page_pool.rs#L137-L163)），它用一对花括号划出租约生存期，把"作用域结束 = 还页"演给读者看。注意 Rust 局部变量按**声明的逆序** drop——`permit`（L143 声明）先于 `permit_b`（L148 声明）创建，所以 `permit_b` 先 drop：

```text
pool = new(4)                     free = [3,2,1,0]
{ permit   = acq(2) → [0,1]       free = [3,2]
  permit_b = acq(1) → [2]         free = [3]
}                                 ← permit_b drop: [2] 逆序入栈 → [3,2]
                                  ← permit   drop: [1,0] 逆序入栈 → [3,2,1,0]
acq(4) → [0,1,2,3]                ← 与全新池逐位一致（L159-162 的断言）
```

最后一步的断言不止查"数量回来了"，还查**顺序**：`[PageId(0), PageId(1), PageId(2), PageId(3)]`。这一条把 4.2 与 4.3 的两个顺序论证（升序发放、逆序归还）同时锁进了回归测试。

消费方 `kv_pool.rs` 的测试还从上层复核了同一套语义：[src/kv_pool.rs:L550-L562](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L550-L562)（5 页池：1 页 padding + 4 页可用；`KvState` 持 2 页时可用 2，drop 后回到 4）、[src/kv_pool.rs:L540-L548](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L540-L548)（`try_grow` 失败路径经 `ensure_capacity` 以 `Err` 露出）。

#### 4.4.3 源码精读

- [src/page_pool.rs:L137-L163](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/page_pool.rs#L137-L163) `acquires_pages_and_restores_on_drop`：如上逐行核对；L148-152 的第二次 acquire 顺带验证"占用中再分配拿到下一批页（`[PageId(2)]`）"。
- [src/page_pool.rs:L165-L171](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/page_pool.rs#L165-L171) `reports_insufficient_capacity_with_none`：`_permit` 用下划线命名**刻意保持存活**，制造"可用不足"的前置条件。
- [src/page_pool.rs:L173-L193](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/page_pool.rs#L173-L193) `try_grow_extends_permit_and_returns_all_on_drop`：L181-183 成功路径（`[0]` → `[0,1,2]`，可用 1）；L186-188 失败路径（要 2 页只剩 1 页必败，租约长度与可用数都不变）；L192 出作用域后 4 页全回。

#### 4.4.4 代码实践

1. **实践目标**：把三个既有测试跑绿，并用手推表核对每条断言。
2. **操作步骤**：运行 `cargo test --release -p pegainfer-core --lib page_pool`（按名过滤，只命中本模块）；对照 4.4.2 的推演表逐条核对第一个测试的断言。
3. **需要观察的现象**：3 个测试通过；整个命令不需要 GPU（可对照 u1-l3：`--lib` 不编译 `tests/` 目录，且这些测试不构造 GPU 资源）。
4. **预期结果**：`test result: ok. 3 passed` 的形式。待本地验证（本讲义写作环境未执行命令，行为依据是源码与 u1-l3 的测试分层结论）。

#### 4.4.5 小练习与答案

1. **问：`acquires_pages_and_restores_on_drop` 的最后断言（拿到 `[0,1,2,3]`）若失败，可能违反了哪条性质？**
   答：要么"升序发放"（`rev` 构造或 pop 顺序）被破坏，要么"逆序归还"（Drop 的 `extend` 方向）被破坏。单靠这个断言无法区分，可插入 `available_pages()` 探针序列定位。
2. **问：这三个测试为什么不需要 GPU，也没有 `#[ignore]`？**
   答：u1-l3 的判据是"构造了什么"：`PagePool` 全程不触碰 `DeviceContext`/`CudaSlice`，free-list 是宿主 `Vec`；而且 core 里本来就没有任何 `#[ignore]`，GPU 边界靠命令分层与运行时 `expect` 显式红灯。
3. **问：现有测试没覆盖 `try_acquire_many(0)` 与跨池 `absorb` 的 panic 路径，哪个值得补？**
   答：`try_acquire_many(0)` 值得补：它被 `KvPool::alloc` 依赖（零页初始租约），一行断言即可锁住"永不失败"契约。跨池 `absorb` 的 panic 可用 `#[should_panic]` 测，但它守护的是同 crate 内不可能发生的误用（调用点池来源单一），优先级较低——写了也不亏。

## 5. 综合实践

**任务：给 `PagePool` 补两个新单测锁住未覆盖的性质，并审计 padding 页的生命周期。**

第一部分对应本讲规格中的实践任务。先说一个关键约束：`PagePool` 的方法全是 `pub(crate)`，`tests/` 目录下的集成测试是独立 crate，**看不到这些方法**——所以新测试必须放进 [src/page_pool.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/page_pool.rs) 已有的 `mod tests` 内（L194 收尾花括号之前），这也是规格中"或新文件"行不通的原因。只动 `#[cfg(test)]` 代码，不影响生产构建。

1. **实践目标**：
   - 测试一：多轮 acquire/drop 之后 free-list **完整恢复**（含顺序恢复）。
   - 测试二：`permit.absorb(other)` 之后，**仅释放吸收方时页才回到池中**。
2. **操作步骤**：在你的本地克隆建分支（仓库禁止直推 main），把下面的测试追加进 `mod tests`。以下为示例代码（非项目原有代码）：

   ```rust
   #[test]
   fn multi_round_acquire_drop_restores_free_list() {
       let pool = PagePool::new(4);
       for _ in 0..100 {
           let a = pool.try_acquire_many(3).expect("acquire 3");
           let b = pool.try_acquire_many(1).expect("acquire 1");
           assert_eq!(pool.available_pages(), 0);
           drop(b);
           assert_eq!(pool.available_pages(), 1);
           drop(a);
           assert_eq!(pool.available_pages(), 4);
       }
       // 顺序也恢复了：整批借出恰好是升序 0..4
       let all = pool.try_acquire_many(4).expect("all pages");
       assert_eq!(all.pages(), &[PageId(0), PageId(1), PageId(2), PageId(3)]);
   }

   #[test]
   fn absorb_returns_pages_only_when_absorber_drops() {
       let pool = PagePool::new(3);
       let mut main = pool.try_acquire_many(1).expect("main");
       let extra = pool.try_acquire_many(2).expect("extra");
       assert_eq!(pool.available_pages(), 0);

       main.absorb(extra); // extra 被消费：pages 已搬空，其 Drop 无事发生
       assert_eq!(main.pages(), &[PageId(0), PageId(1), PageId(2)]);
       assert_eq!(pool.available_pages(), 0); // 页仍在租约手里

       drop(main);
       assert_eq!(pool.available_pages(), 3); // 吸收方 drop 才统一归还
   }
   ```

   然后运行 `cargo test --release -p pegainfer-core --lib page_pool`。
3. **需要观察的现象**：按名过滤恰好命中本模块 5 个测试（原有 3 + 新增 2）；测试一里每一轮拿到的都是同样的页集合。
4. **预期结果**：`page_pool::tests` 下 5 个测试全部通过（`test result: ok. 5 passed` 的形式）。待本地验证——本讲义写作环境未执行编译，以上断言是从源码语义逐行推演的。完成后 `cargo fmt`（CI 强制 `cargo fmt --check`）；若进一步演练贡献流程，提交信息可用 Commitizen 格式如 `test(core): cover page_pool absorb and recovery semantics`（本地跑绿即可，不必真的提交）。

第二部分：审计 padding 页的生命周期（为 u3-l2 铺路）。真实消费场景是 [src/kv_pool.rs:L226-L229](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L226-L229)：`KvPool` 构造时 `PagePool::new(num_pages)` 之后**立刻** `try_acquire_many(1)` 拿走 0 号页存为 `padding_permit`（[src/kv_pool.rs:L117-L125](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L117-L125)，doc 写明它服务 CUDA Graph 的 padding 槽位），`padding_page_id()` 从这份永久租约读出页号（[src/kv_pool.rs:L269-L272](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L269-L272)）。请完成：

1. **推演表**：手写 free-list 状态表，覆盖 `new(4)` → 构造期取 padding 页 → 请求 A `acq(2)` → 请求 B `acq(1)` → A `grow(1)` → `drop(A)` → `drop(B)` 的每一步（形如 4.3.2）。回答：任何时刻 padding 页可能回到 free-list 吗？依据是什么？
2. **回答三个问题**（各 2-3 句，引用行号）：
   - padding 页为什么必须在**构造期**预留，而不是运行期需要 padding 时再申请？
   - 为什么用**长期持有的 permit**（RAII）而不是"0 号页永远不发放"的编码约定？
   - CLAUDE.md 的 EP 纪律说 "Padding rows are protocol surface"，本讲的哪个性质是它在单卡上的投影？

参考答案要点：`padding_permit` 是 `KvPoolInner` 的字段，随池存活、没有任何代码路径 drop 它，所以 free-list 永远不会再发放 0 号页（[src/kv_pool.rs:L542](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/kv_pool.rs#L542) 的测试注释 "1 padding, 2 available" 从上层印证了这一点）；构造期预留保证从第一张 CUDA Graph 捕获起 padding 页号就固定（当前实现下恒为 0，源于 `rev` 构造 + 构造期首个 acquire；图回放要求下标稳定，呼应 u4-l1）；用 permit 而非编码约定，是让"闲置"与"被持有"在类型系统里成为同一件事——RAII 显式持有比隐式"跳过 0 号"更可审计；"构造性确定"的单卡投影即：升序发放、逆序归还、padding 页以 RAII 永久持有，三者合起来使任何运行的页布局可复现。

## 6. 本讲小结

- `PagePool` 是**固定容量**的共享页分配器：`Arc` 句柄 + `Mutex<Vec<PageId>>` free-list，容量在 `new` 后冻结；类型公开、方法全部 `pub(crate)`，页的进出只发生在 core 内部的 `kv_pool` 模块。
- 分配语义是**全有或全无**：检查与取页在同一临界区，`try_acquire_many` 要么整批成功要么 `None`，零页申请永不失败——这是 `kv_pool` 两段式准入"失败靠 drop 回滚"的机械前提，守恒律 \(\text{available} + \sum \text{len} = \text{capacity}\) 任意时刻成立。
- `OwnedPagePermit` 用 **RAII** 把还页变成 Drop 的义务：panic、early-return 都不需要手写清理；`absorb` 靠 `Arc::ptr_eq` 断言 + `Vec::append` 搬空实现"吞并后旧租约无害消亡"；`try_grow` 失败时租约原封不动。
- **顺序确定性**由三处细节合谋：`rev()` 构造让发放升序、`Drop` 逆序 `extend` 让复用继续升序、padding 页在构造期以永久 permit 拿走——这是全 crate "构造性确定性"纪律的第一个样本。
- 三个内嵌单测就是三份契约（恢复、拒绝、grow），全是纯 CPU 测试；补测试必须进同文件内嵌模块，`pub(crate)` 可见性使 `tests/` 集成测试无法触达这些方法。

## 7. 下一步学习建议

- **下一讲 u3-l2（kv_pool.rs）** 是本讲的直接上层：`KvPool` 把 `PagePool`、整块 `CudaSlice<bf16>` 与 `KvLayout` 装配成 page-first 几何的 KV 池，`try_reserve` / `commit_reservation` 把本讲的 Drop 回滚升级为跨多池的原子准入。建议带着综合实践的三问去读。
- 侧向补充：CUDA Graph 为何要求指针/下标稳定（u4-l1 的主题）解释了 padding 页存在的最终原因；EP free-running 的多卡投影见 [free-running-dp.md](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/models/glm52/free-running-dp.md)（规范正文，u5-l1 展开）。
- 想巩固本讲：把综合实践的两个测试真的写进本地分支跑绿，再给 `try_acquire_many(0)` 补一行契约断言（4.4.5 练习三）。
