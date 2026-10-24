# page_pool.rs：固定页分配器与 RAII 许可

## 1. 本讲目标

本讲精读 `pegainfer-core/src/page_pool.rs`——整个 crate 里最小、却是最底层的"地基"文件（195 行）。学完后你应该能够：

- 解释 **RAII permit** 如何让"页归还"变成编译器强制的语义，从而消除手动归还的遗漏风险（包括 panic、early-return 路径）。
- 手推 `absorb`、`try_grow`、`Drop` 三个操作对 free-list 的**精确影响**（包括元素的进出顺序）。
- 说出 `PagePool` / `OwnedPagePermit` / `PageId` 三者的分工，以及它们在 `kv_pool.rs` 中的真实消费方式——尤其是 padding 页为什么必须在构造期预留。
- 结合源文件内嵌的三个单元测试理解并发 acquire/drop 的恢复语义，并亲手补两个新测试。

承接前文：u2-l4 我们看到"core 用 `KvDesc` 描述状态、kernels 只消费扁平页表"，但当时没有回答一个更底层的问题——**这些页是谁的？谁在宿主侧记录"第 3 页现在归请求 A 所有"？** 本讲就是这个问题的答案。

## 2. 前置知识

- **RAII 与 Drop**：Rust 的资源获取即初始化。当一个值离开作用域（正常结束或 panic 展开时），其 `Drop::drop` 自动执行。C++ 程序员熟悉的 `unique_ptr` 析构就是这个模式。本讲中"资源"就是页的所有权。
- **`Arc<T>`**：原子引用计数的共享指针。`PagePool` 克隆不复制数据，只是多一个指向同一 `PagePoolInner` 的句柄。
- **`parking_lot::Mutex`**：比标准库更快的互斥锁，不可重入、无中毒（poisoning）。本讲只需知道：`lock()` 返回守卫，守卫存活期间临界区被独占。
- **`Vec` 当栈用**：`push` 加到尾部、`pop` 从尾部取，均为 \( O(1) \)。free-list 就是一个 `Vec<PageId>`。
- **KV cache 分页**（回顾 u2-l4）：每个请求的 KV 状态按固定大小的"页"（page，含 `page_size` 个 token 的全部层 K/V）切分，页在池中编号，请求持有一组页编号。
- **"构造性确定性"（constructively deterministic）**：来自 [CLAUDE.md](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md) 的 EP free-running 纪律——padding 行的每个字节都要确定可复现，"any dummy-row input must be constructively deterministic"。本讲会看到它在单卡上的第一个具体样本。

## 3. 本讲源码地图

| 文件 | 行数 | 本讲中的角色 |
|------|------|-------------|
| [page_pool.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/page_pool.rs) | 195 | 唯一精读对象：分配器本体 + 内嵌单测（L132 起的 `mod tests`） |
| [kv_pool.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs) | 563 | 消费方：`KvPoolInner` 持有 `PagePool` + `padding_permit`，下一讲（u3-l2）的主对象 |
| [lib.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/lib.rs) | 15 | `pub mod page_pool;` 在 [lib.rs:11](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/lib.rs#L11)，13 个模块之一 |
| [CLAUDE.md](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md) | — | EP free-running 纪律（"Padding rows are protocol surface"）与 CUDA Graph 指针稳定性的事实来源 |
| [free-running-dp.md](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docs/models/glm52/free-running-dp.md) | — | EP 纪律的规范文档，本讲只借用其单卡投影，细节留待 u5-l1 |

整个 `page_pool.rs` 的骨架一目了然：

```text
PagePool (Arc<PagePoolInner>)          ← 共享句柄，Clone 廉价
  └─ PagePoolInner { capacity_pages, free_list: Mutex<Vec<PageId>> }
OwnedPagePermit { inner, pages: Vec<PageId> }   ← RAII 租约，Drop 归还
PageId(usize)                          ← 不透明页编号
```

## 4. 核心概念与源码讲解

### 4.1 PageId：不透明的页编号

#### 4.1.1 概念说明

`PageId` 是一个**单字段 newtype**：内部就是一个 `usize`，但对外的类型系统只允许你持有它、比较它、传递它——想把它变回整数，只有 core 内部的一条窄门 `index()`。为什么包一层？因为在这个 crate 里，`usize` 到处都是：token 下标、层号、元素偏移、字节长度。裸用 `usize` 当页编号，迟早会把"第 5 个 token"误传给"第 5 页"的接口。newtype 让这类错误在编译期就暴露。

#### 4.1.2 核心流程

`PageId` 没有流程，只有约束：

- `Copy`：值语义，传参零成本，不需要引用。
- `Eq + Hash`：可以放进 `HashSet` / 当 `HashMap` 的键（将来做页去重、前缀缓存时直接可用）。
- `PartialEq + Debug`：测试断言与诊断打印。
- `index()` 是 `pub(crate)`：把"页编号 → 整数"的转换收在 crate 内部。全 crate 唯一的转换点在 `kv_pool.rs` 里把页编号变成喂给 kernel 的 `i32`（[kv_pool.rs:297](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L297)，`extend_page_indices_i32`）。

#### 4.1.3 源码精读

[page_pool.rs:24-32](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/page_pool.rs#L24-L32) 定义不透明页编号：`PageId(usize)` 带全套 derive，唯一的转换方法 `index(self) -> usize` 标注 `pub(crate)`，外部 crate 无法把 `PageId` 还原成数字——这保证了页编号不会被绕过池直接当显存下标用。

注意构造函数 `PageId` 本身（tuple struct 的构造语法）对 crate 外同样是不可见的（没有 `pub` 构造器导出），只有 `(0..capacity_pages).rev().map(PageId)` 这类 crate 内部代码能造出它（见 [page_pool.rs:36](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/page_pool.rs#L36)）。

#### 4.1.4 代码实践

1. **实践目标**：验证 `PageId` 的封装强度。
2. **操作步骤**：在 `page_pool.rs` 的 `mod tests` 里临时加一行 `let _: usize = PageId(0).index();`（同 crate 内合法），再试着想象把同样的代码放进 `tests/` 目录的集成测试里。
3. **需要观察的现象**：集成测试是独立 crate，`index()` 与 `PageId(...)` 构造都不可见，会直接编译失败。
4. **预期结果**：`pub(crate)` 的可见性把"造页号、读页号"的能力锁死在 core 内部；集成测试只能通过 `PagePool` 的公开行为间接观察页。待本地验证（如果你动手试了编译错误）。

#### 4.1.5 小练习与答案

1. **问：为什么 `index()` 不做成 `pub`？**
   答：一旦公开，任何下游都能把页编号当整数做算术（比如 `id + 1` 猜下一页），绕过分配器直接索引共享 buffer，破坏"页进出必须经过池"的所有权纪律。收窄到 `pub(crate)` 后，唯一的消费点是 `kv_pool.rs` 转成 kernel 需要的 `i32`。
2. **问：`Hash` derive 在当下代码里似乎没人用，为什么要带？**
   答：这是给未来机制（如页集合去重、前缀缓存键）预留的能力；derive 是零成本声明，不带反而会在需要时多一次改动。同时也让 `PageId` 可放进任何标准容器。

### 4.2 PagePool：锁保护 free-list 的共享固定页分配器

#### 4.2.1 概念说明

`PagePool` 是一个**固定容量**的页分配器：构造时决定一共有多少页（容量此后不再变），之后所有分配都只是从 free-list 里"借出"、归还时"放回"。"固定"不是偷懒——它来自 KV 池的几何：`kv_pool.rs` 在构造期用模型几何算出 `num_pages`、一次性分配整块 GPU buffer，页数与显存一一对应，运行期根本没有"多造一页"这个选项。

结构上它是"句柄 + 内部状态"的经典两件套：

- `PagePool` 本体 `Clone + Arc`，克隆极廉价，可以发给每个 `KvState`；
- `PagePoolInner` 里只有两个字段：`capacity_pages`（不变量）和 `free_list: Mutex<Vec<PageId>>`（唯一可变状态）。

还有一个值得注意的可见性设计：**类型是 `pub` 的，方法全是 `pub(crate)`**。crate 外（模型 crate）能在签名里提到 `PagePool` 这个名字，却无法 `new`、无法 acquire——页的进出完全发生在 core 内部的 `kv_pool` 模块。这是"类型公开、能力不公开"的手法。

#### 4.2.2 核心流程

`new` 与 `try_acquire_many` 的关键在**顺序**：

```text
new(capacity):
  free_list = [capacity-1, capacity-2, ..., 1, 0]   # rev 收集
                              ↑ 栈顶（Vec 尾部）是 PageId(0)

try_acquire_many(n):
  n == 0            → 返回空 permit（永不清锁、永不失败）
  锁 free_list
  剩余 < n          → 返回 None（检查与取页在同一临界区）
  弹出 n 个页        → 从尾部弹，先拿到 PageId(0)，再 1、2……升序
  返回 Some(permit)
```

两个不变量直接从中读出来：

1. **分配顺序确定性**：同一池、同一操作序列，任何机器、任何一次运行，页编号的发放顺序完全一致。首次 acquire 拿到的一定是 `PageId(0)`。
2. **全有或全无**：要么一次拿到全部 `n` 页，要么 `None`，绝不会出现"拿到一半"。检查 `free_list.len() < n` 与弹出循环共享同一个锁守卫，其他线程没有插队窗口。

单页操作均为 \( O(1) \)（`Vec` 尾部 push/pop），一次 acquire 的代价是 \( O(n) \) 次弹栈加一次锁。

#### 4.2.3 源码精读

- [page_pool.rs:5-15](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/page_pool.rs#L5-L15) 定义 `PagePool`（doc 注释一句话点题："Shared fixed-page allocator with RAII-owned permits"）与 `PagePoolInner`。注意 `PagePool` 是 `#[derive(Clone, Debug)]`，克隆只是复制 `Arc` 指针。
- [page_pool.rs:34-43](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/page_pool.rs#L34-L43) 构造函数：`(0..capacity_pages).rev().map(PageId).collect()` 把降序区间收进 `Vec`，使**栈顶恰好是 0 号页**。
- [page_pool.rs:45-67](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/page_pool.rs#L45-L67) `try_acquire_many`：`n == 0` 的特判在拿锁**之前**返回空 permit；随后在**同一个锁守卫存活期间**完成"检查剩余量 → 弹出 n 页"，循环里的 `expect("free_list length checked above")` 把"不可能失败"的推理写成断言。
- [page_pool.rs:69-75](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/page_pool.rs#L69-L75) 两个只读探针：`capacity_pages()` 读不变量（不拿锁），`available_pages()` 拿锁读 free-list 长度。调度器用后者决定还能不能再收请求。

#### 4.2.4 代码实践

1. **实践目标**：用只读探针"看见"全有或全无语义。
2. **操作步骤**：阅读内嵌测试 `reports_insufficient_capacity_with_none`（[page_pool.rs:165-171](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/page_pool.rs#L165-L171)）；池容量 2、手里已持 1 页（可用剩 1），再 `try_acquire_many(2)`。
3. **需要观察的现象**：返回 `None`，且 `available_pages()` 仍是 1——失败的申请不消耗任何页。
4. **预期结果**：测试断言 `is_none()` 通过。这正是 `kv_pool.rs` 两段式准入敢于"先从每个池 reserve、失败靠 drop 回滚"的前提（见 [kv_pool.rs:274-283](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L274-L283) 的 doc 注释）。

#### 4.2.5 小练习与答案

1. **问：`new(4)` 之后 `free_list` 的内容是什么？为什么要 `rev()`？**
   答：`[3, 2, 1, 0]`。`pop` 从 Vec 尾部取，倒序填充让 0 号页位于栈顶，于是分配顺序天然是升序的——这是"分配顺序确定性"的机械来源。
2. **问：`try_acquire_many(0)` 为什么要在拿锁前特判？这个分支被谁依赖？**
   答：零页申请没有任何临界区工作，提前返回既省一次锁也永不失败。`kv_pool.rs:244` 的 `KvPool::alloc()` 用它给每个新请求造一个合法的零页初始 permit——"请求一出生就持有（空的）租约"。
3. **问：两个线程同时 `try_acquire_many(3)` 和 `try_acquire_many(2)`，池剩 4 页，可能的结果集合是什么？**
   答：要么"3 成功、2 失败"，要么"2 成功、3 失败"（取决于谁先拿锁）；不可能两个都成功（4 < 3+2），也不可能任何一个"部分成功"——因为检查与取页在同一临界区内，线性可序化。

### 4.3 OwnedPagePermit：RAII 租约、absorb、try_grow 与 Drop 归还

#### 4.3.1 概念说明

`OwnedPagePermit` 是"一纸租约"：持有它就持有若干页，**丢掉它就等于还页**。它的字段只有两个——指回池的 `Arc<PagePoolInner>` 和自己名下的 `pages: Vec<PageId>`。没有显式的 `release()`、`return_pages()` 之类的手动 API，归还只有一个入口：`Drop`。

这就是 RAII 消除"手动归还遗漏"的方式：

- 函数中途 `?` 提前返回？局部 permit 离开作用域，Drop 执行，页归还。
- 线程 panic？栈展开逐帧执行 Drop，页归还。
- 忘了还？不可能——除了"所有权转移"（`absorb` 把页搬进别的 permit），页跟着值走，值没了页就回来了。

`kv_pool.rs` 把这个性质直接写进了两段式准入的设计里：`KvReservation` 的 doc 是 "Returns its pages on drop unless committed"（[kv_pool.rs:286-290](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L286-L290)），而 `try_reserve` 的 doc 更直白："let a failure roll the rest back by drop"——失败路径根本不写清理代码，靠 Drop 自动回滚。

三个操作各自的语义：

- **`absorb(other)`**：把另一份租约整体吞并。消耗 `other`（按值传入），`other.pages` 被搬空后丢弃时命中 Drop 的空提前返回，不会重复还页；此后**只有吸收方的 Drop 会归还全部页**。跨池吸收会被 `Arc::ptr_eq` 断言拦下——页还错 free-list 是无法用类型系统表达、只能用运行时断言守卫的错误。
- **`try_grow(n)`**：给当前租约追加 n 页。成功返回 `true`；失败（剩余不足）返回 `false` 且**租约原封不动**。注意它扩张的是"租约持有的页集合"，不是池的总容量——池容量在 `new` 之后就冻结了。
- **`Drop`**：归还时按 `pages` 的**逆序** `extend` 回 free-list。这个方向不是随手的：`pages = [0, 1]` 逆序还回后，`1` 先入栈、`0` 后入栈（0 在栈顶），下次分配又先拿到 0——**升序发放、升序回收、再升序发放**，页的复用顺序也是确定的。

#### 4.3.2 核心流程

一次典型的"申请 → 扩张 → 合并 → 归还"生命周期（capacity = 4）：

```text
初始:      free = [3,2,1,0]
A=acq(1):  A.pages=[0]        free = [3,2,1]
B=acq(2):  B.pages=[1,2]      free = [3]
A.grow(1): A.pages=[0,3]      free = []          ← 追加到尾部
B.absorb 进 A 不存在，改为示例 A 吸收空租约 acq(0)：
           A.pages 不变，空租约 Drop 无事发生
drop(B):   B.pages=[1,2] 逆序还回 → free = [2,1]
drop(A):   A.pages=[0,3] 逆序还回 → free = [3,2,1,0]
终态:      与初始完全一致，且下次 acq(4) 得到 [0,1,2,3]
```

absorb 的合并语义：

```text
A.pages=[0], B.pages=[1,2]   （同一池，assert Arc::ptr_eq 通过）
A.absorb(B):
  A.pages = [0, 1, 2]        ← append 保序拼接
  B.pages = []               ← append 搬空
  B 随即被丢弃 → Drop 见空列表，提前返回，不还页
此后 free-list 的恢复完全由 A 的 Drop 负责
```

#### 4.3.3 源码精读

- [page_pool.rs:17-22](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/page_pool.rs#L17-L22) `OwnedPagePermit` 的定义：doc 一句话讲完全部语义（"A set of pages leased from a PagePool. Pages return automatically on drop."）；它不 `Clone`——租约不可复制，页的所有权独占。
- [page_pool.rs:78-87](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/page_pool.rs#L78-L87) 只读接口 `pages()` / `len()`。L83 的注释值得一看：`is_empty` 曾是死代码，在一次代号 hawk 的清扫（PR #743）里被删掉，只留 `len` 并配 `#[allow(clippy::len_without_is_empty)]` 压住 clippy 的告警——小注释，但透露了这个仓库"不留死代码"的卫生习惯。
- [page_pool.rs:89-97](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/page_pool.rs#L89-L97) `absorb`：先 `assert!(Arc::ptr_eq(...), "absorb requires permits from the same pool")`，再 `self.pages.append(&mut other.pages)`。`Vec::append` 把 other 的元素整体搬走并清空它，other 析构时的 Drop 自然无事发生——"吞并后旧租约无害消亡"完全靠这条链保证。
- [page_pool.rs:99-117](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/page_pool.rs#L99-L117) `try_grow`：拿锁 → 检查剩余 → 不够就 `return false`（此刻什么都没动）→ 够则 `reserve` 容量后逐页弹栈追加。与 `try_acquire_many` 同款的全有或全无。
- [page_pool.rs:120-130](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/page_pool.rs#L120-L130) `Drop for OwnedPagePermit`：空列表提前返回（吸收后的旧租约、零页租约都走这条捷径）；否则 `std::mem::take` 把页清单整体取走，再拿锁 `free_list.extend(pages.into_iter().rev())` 逆序放回。`mem::take` 保证 Drop 体内对 `self.pages` 只有这一次访问，随后的归还不可能重复触发。

#### 4.3.4 代码实践

1. **实践目标**：亲眼确认"吸收后只有吸收方还页"。
2. **操作步骤**：阅读内嵌测试 `try_grow_extends_permit_and_returns_all_on_drop`（[page_pool.rs:173-193](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/page_pool.rs#L173-L193)），然后照它的风格写一个 absorb 版（完整代码见第 5 节综合实践）。
3. **需要观察的现象**：`absorb` 之后 `available_pages()` 降为两份租约的合并持有量，被吸收方消失；只有吸收方 drop 后可用页数才回升。
4. **预期结果**：断言全部通过。待本地验证（本讲写作环境未运行测试，推演依据是 L91-97 与 L120-130 的源码语义）。

#### 4.3.5 小练习与答案

1. **问：`absorb` 为什么必须断言同池？跨池吸收会坏在哪里？**
   答：租约归还时把页 push 进 `self.inner.free_list`（L127），`inner` 是它自己那份池。跨池吸收后，A 池的页会随 B 池的租约在 B 池的 free-list 里"复活"，而 A 池从此少页——双重计数，随后同一页可能被两方同时持有。`Arc::ptr_eq` 在第一时间 panic，把这类错误挡在测试期。
2. **问：`try_grow(2)` 失败后租约处于什么状态？依据是哪几行？**
   答：原封不动。[page_pool.rs:106-109](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/page_pool.rs#L106-L109) 在锁内先检查 `free_list.len() < n` 即返回 `false`，此时 `self.pages` 还没被碰过；`reserve`/`push` 都在检查之后。
3. **问：`Drop` 里为什么是 `extend(rev)` 而不是顺序 `extend`？**
   答：假设 `pages = [0, 1]`，顺序还回会让 free-list 变成 `[..., 0, 1]`，下次 pop 先拿到 1——复用顺序开始抖动。逆序还回让最早分配的 0 号页最后入栈、位于栈顶，下轮分配继续升序发放。顺序确定性是"构造性确定性"纪律的一部分。
4. **问：为什么 `Drop` 要先 `std::mem::take` 再拿锁？**
   答：把页清单先整体移出，`self.pages` 变成空 Vec，之后锁内的归还逻辑消费的是局部变量 `pages`；Drop 体内对租约字段只有一次访问，归还动作不可能被重复触发。

### 4.4 内嵌单元测试：三个测试就是三份契约

#### 4.4.1 概念说明

`page_pool.rs` 自带三个单测（[page_pool.rs:132-194](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/page_pool.rs#L132-L194)）。呼应 u1-l3 的判据：这些测试**不构造任何 GPU 资源**（没有 `DeviceContext`、没有 `CudaSlice`），是纯宿主数据结构测试，落在 `cargo test --release -p pegainfer-core --lib` 的快车道里，不需要 GPU 机器就能跑。

| 测试 | 验证的契约 |
|------|-----------|
| `acquires_pages_and_restores_on_drop` | 分配升序；drop 后完整恢复；恢复后再分配仍升序取满 |
| `reports_insufficient_capacity_with_none` | 剩余不足返回 `None`，不产生部分分配 |
| `try_grow_extends_permit_and_returns_all_on_drop` | grow 追加保序；超额 grow 失败且租约不变；drop 归还全部（含 grow 来的页） |

#### 4.4.2 核心流程

以第一个测试为例（[page_pool.rs:137-163](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/page_pool.rs#L137-L163)），它用一对花括号划出租约的生存期，把"作用域结束 = 还页"直接演给读者看：

```text
pool = new(4)
  { permit = acq(2) → [0,1]，剩 2
    permit_b = acq(1) → [2]，剩 1
  }                        ← 两个租约在此同时 drop
acq(4) → [0,1,2,3]         ← 完整、有序地回来了
```

注意最后一步的断言不止检查"数量回来了"，还检查**顺序**：`[PageId(0), PageId(1), PageId(2), PageId(3)]`。这一条把 4.2.2 与 4.3 的两个顺序论证（升序发放、逆序归还）同时锁进了回归测试。

#### 4.4.3 源码精读

- [page_pool.rs:137-163](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/page_pool.rs#L137-L163) `acquires_pages_and_restores_on_drop`：如上逐行核对，其中 L148-152 的第二次 acquire 顺带验证了"占用中再分配会拿到下一批页（`[PageId(2)]`）"。
- [page_pool.rs:165-171](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/page_pool.rs#L165-L171) `reports_insufficient_capacity_with_none`：`_permit` 用下划线命名刻意保持存活，制造"可用不足"的前置条件。
- [page_pool.rs:173-193](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/page_pool.rs#L173-L193) `try_grow_extends_permit_and_returns_all_on_drop`：L186 的 `assert!(!permit.try_grow(2))` 是失败路径测试——剩余只有 1 页，要 2 页必败；L187-188 立刻断言租约长度仍是 3、可用仍是 1，把"失败不产生副作用"钉死。

#### 4.4.4 代码实践（本讲核心实践）

给 `PagePool` 补两个新单测，锁住两个尚未被现有测试覆盖的性质。

1. **实践目标**：
   - 测试一：多轮 acquire/drop 之后 free-list **完整恢复**（含顺序恢复）。
   - 测试二：`permit.absorb(other)` 之后，**仅释放吸收方时页才回到池中**。
2. **操作步骤**：
   - 新建分支（本仓库禁止直推 main）：`git checkout -b test/core-page-pool`。
   - 把下面的测试追加到 [page_pool.rs](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/page_pool.rs) 已有的 `mod tests` 内（L194 收尾花括号之前）。**必须放这里**：`PagePool` 的方法全是 `pub(crate)`，放进 `tests/` 集成测试目录（独立 crate）会编译失败；同文件内嵌测试则是同 crate，畅通无阻。

   以下为示例代码（非项目原有代码）：

   ```rust
   #[test]
   fn free_list_fully_recovers_after_repeated_rounds() {
       let pool = PagePool::new(3);
       for _ in 0..10 {
           let permit = pool.try_acquire_many(2).expect("acquire");
           assert_eq!(permit.pages(), &[PageId(0), PageId(1)]);
           drop(permit);
       }
       assert_eq!(pool.available_pages(), 3);
       let all = pool.try_acquire_many(3).expect("all pages");
       assert_eq!(all.pages(), &[PageId(0), PageId(1), PageId(2)]);
   }

   #[test]
   fn absorb_returns_pages_only_when_absorber_drops() {
       let pool = PagePool::new(2);
       let mut a = pool.try_acquire_many(1).expect("a");
       let b = pool.try_acquire_many(1).expect("b");
       assert_eq!(pool.available_pages(), 0);

       a.absorb(b); // b 被消费：pages 已被搬空，其 Drop 无事发生
       assert_eq!(a.pages(), &[PageId(0), PageId(1)]);
       assert_eq!(pool.available_pages(), 0);

       drop(a);
       assert_eq!(pool.available_pages(), 2);
   }
   ```

   - 运行 `cargo test --release -p pegainfer-core --lib page_pool`。
3. **需要观察的现象**：该命令按名过滤，恰好命中本模块 5 个测试（原有 3 个 + 新增 2 个）；循环测试里每一轮拿到的都是同样的 `[0, 1]` 两页。
4. **预期结果**：`page_pool::tests` 下 5 个测试全部通过（`test result: ok. 5 passed` 的形式）。待本地验证——本讲义写作环境未执行编译，以上断言是从源码语义逐行推演的。完成后记得 `cargo fmt`（CI 强制 `cargo fmt --check`）。
5. 若你想进一步演练贡献流程：提交信息可用 Commitizen 格式 `test(core): cover page_pool absorb and recovery semantics`，但不要真的提交，本地跑绿即可。

#### 4.4.5 小练习与答案

1. **问：`acquires_pages_and_restores_on_drop` 的最后一个断言（拿到 `[0,1,2,3]`）如果失败，可能违反了哪两条顺序性质之一？**
   答：要么"升序发放"被破坏（rev 构造或 pop 顺序变了），要么"逆序归还"被破坏（Drop 里 extend 方向反了，导致复用顺序抖动、某些页缺位）。单靠这个断言无法区分二者，可加中间探针（`available_pages` 序列）定位。
2. **问：这三个测试为什么不需要 GPU，也不用 `#[ignore]`？**
   答：u1-l3 的判据是"构造了什么"：`PagePool` 全程不触碰 `DeviceContext`/`CudaSlice`，free-list 是宿主 `Vec`。测试天然是纯 CPU 的，落在 `--lib` 快车道，没有任何 `#[ignore]` 标注。
3. **问：现有测试没有覆盖 `try_acquire_many(0)` 与跨池 `absorb` 的 panic 路径，哪个值得补、哪个不值得？**
   答：`try_acquire_many(0)` 值得补：它被 `KvPool::alloc` 依赖（零页初始租约），一行断言即可锁住"永不失败"契约。跨池 `absorb` 的 panic 用 `#[should_panic]` 也能测，但它守护的是"不可能的误用"（调用方都在同 crate 内、池来源单一），优先级低——不过写了也不亏。

## 5. 综合实践

**任务：审计 padding 页的完整生命周期，并用 free-list 状态推演表 + 新增测试把它锁住。**

回顾一个真实的消费场景（下一讲 u3-l2 的入口，这里只看与 page_pool 有关的部分）：

- [kv_pool.rs:226-229](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L226-L229)：`KvPool` 构造时 `PagePool::new(num_pages)` 之后**立刻** `try_acquire_many(1)` 拿走 0 号页，存为 `padding_permit`（[kv_pool.rs:117-125](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L117-L125)，doc 写明它服务 CUDA Graph 的 padding 槽位，KV append 写入"无害垃圾"，注意力输出被丢弃）。
- [kv_pool.rs:269-272](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/kv_pool.rs#L269-L272)：`padding_page_id()` 从这份永久持有的 permit 里读出页号。

请完成四步：

1. **推演表**：手写一张 free-list 状态表，覆盖 `new(4)` → 构造期取 padding 页 → 请求 A `acq(2)` → 请求 B `acq(1)` → A `grow(1)` → drop(A) → drop(B) 的每一步（形如第 4.3.2 节的推演）。回答：任何时刻 padding 页可能回到 free-list 吗？依据是什么？
2. **测试锁定**：把 4.4.4 的两个测试跑绿；如果想进一步验证推演，可在测试一里模拟"构造期先取走一页"：`new(4)` 后先 `acq(1)` 并用 `_` 绑定持有，后续断言全部从 `PageId(1)` 起算。
3. **回答三个问题**（各 2-3 句，引用行号）：
   - padding 页为什么必须在**构造期**预留，而不是运行期要 padding 时再申请？
   - 为什么用**长期持有的 permit**（RAII）而不是简单地"从编号 1 开始分配、0 号页永远闲置"？提示：permit 让"闲置"与"被持有"在类型系统里是同一件事，`padding_page_id()` 读的就是租约上的页。
   - [CLAUDE.md](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/CLAUDE.md) 的 EP 纪律说"Padding rows are protocol surface"，本讲的哪个性质是它在单卡上的投影？
4. **自查**：你的两个新测试是否每个都只服务一个不变量（完整恢复 / 吸收后唯一归还方）？对照 CLAUDE.md 的 AI 辅助贡献规范——"每个改动文件服务于同一不变量"——这是它的最小版本。

参考答案要点：padding 页由 `KvPoolInner` 永久持有（`padding_permit` 字段随池存活），没有任何代码路径会 drop 它，因此 free-list 永远不会再发放 0 号页——确定性由此而来；构造期预留保证从第一张 CUDA Graph 捕获起 padding 页号就固定（图回放要求指针/下标稳定，呼应 u4-l1）；"构造性确定"的单卡投影即：分配顺序升序、归还逆序、闲置页以 RAII 显式持有，三者合起来使任何运行、任何步骤的页布局可复现。

## 6. 本讲小结

- `PagePool` 是**固定容量**的共享页分配器：`Arc<PagePoolInner>` 句柄 + `Mutex<Vec<PageId>>` free-list，容量在 `new` 后冻结；类型公开、方法全部 `pub(crate)`，页的进出只发生在 core 内部。
- 分配语义是**全有或全无**：检查与取页在同一临界区，`try_acquire_many` 要么整批成功要么 `None`，零页申请永败不了——这是 `kv_pool` 两段式准入"失败靠 drop 回滚"的机械前提。
- `OwnedPagePermit` 用 **RAII** 把还页变成 Drop 的义务：panic、early-return 都不再需要手写清理；`absorb` 靠 `Arc::ptr_eq` 断言 + `Vec::append` 搬空实现"吞并后旧租约无害消亡"；`try_grow` 失败时租约原封不动。
- **顺序确定性**由三处细节合谋：`rev()` 构造让发放升序、`Drop` 逆序 `extend` 让复用继续升序、padding 页构造期以永久 permit 拿走——这是全 crate"构造性确定性"纪律的第一个样本。
- 三个内嵌单测就是三份契约（恢复、拒绝、grow），全是纯 CPU 测试，落在 `--lib` 快车道；补测试必须进同文件内嵌模块，`pub(crate)` 可见性使 `tests/` 集成测试无法触达这些方法。

## 7. 下一步学习建议

- **下一讲 u3-l2（kv_pool.rs）** 是本讲的直接上层：`KvPool` 把 `PagePool`、整块 `CudaSlice<bf16>` 与 `KvLayout` 装配成"page-first 几何的 KV 池"，`try_reserve`/`commit_reservation` 把本讲的 Drop 回滚升级为跨多池的原子准入。建议带着本讲综合实践的三问去读。
- 侧向补充：CUDA Graph 为何要求指针稳定（u4-l1 的主题）能解释 padding 页存在的最终原因；EP free-running 的多卡投影见 [free-running-dp.md](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/docs/models/glm52/free-running-dp.md)（规范正文，u5-l1 展开）。
- 想巩固本讲：把 4.4.4 的两个测试真的写进本地分支跑绿，再尝试给 `try_acquire_many(0)` 补一行契约断言（4.4.5 练习三）。
