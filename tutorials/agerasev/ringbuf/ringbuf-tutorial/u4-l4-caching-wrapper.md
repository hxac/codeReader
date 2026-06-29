# Caching 包装器：按需自动同步的默认策略

## 1. 本讲目标

学完本讲后，你应该能够：

1. 说出 `Caching` 包装器在 `Frozen` 之上做了哪一层「自动化」，以及它为什么能既正确又省跨核同步开销。
2. 准确描述 `CachingProd::try_push` 与 `CachingCons::try_pop` 的同步时机——什么时候 `fetch`、什么时候 `commit`、什么时候完全不碰原子量。
3. 看懂 `Caching` 对 `Observer::read_index`/`write_index` 的「按方向选择性 fetch」这一巧妙重写。
4. 解释为什么 `SharedRb::split` 默认产出 `CachingProd`/`CachingCons`，而不是上一讲学过的 `Direct`。
5. 通过源码推理，在密集 `try_push`/`try_pop` 场景下量化 `Direct` 与 `Caching` 触发原子 load/store 的频率差异。

本讲承接 u4-l3（Frozen）。Frozen 让你「手动」控制同步时机；Caching 则是把这套手动同步「按需自动化」，成为多线程场景下的默认包装策略。

## 2. 前置知识

本讲假设你已掌握以下内容（前序讲义已建立）：

- **环形缓冲区双索引**：`read`/`write` 两个索引落在 `0..2*capacity` 区间，`occupied_len`、`vacant_len`、`is_empty`、`is_full` 都由它们推导（u2-l1、u3-l1）。
- **`Frozen` 的本地缓存与三件套**：Frozen 用 `Cell<usize>` 在包装器里缓存一份 `read`/`write` 索引，提供 `commit`（本端进度回写底层）、`fetch`（拉取对端进度）、`sync`（双向）（u4-l3）。
- **`SharedRb` 的原子索引**：`read_index`/`write_index` 用 `CachePadded<AtomicUsize>` 存，读用 `Acquire`、写用 `Release`（u5-l1，本讲只需知道「原子读写就是跨核缓存同步，有开销」即可）。
- **`Direct` 的即时同步**：`Direct` 包装器零缓存，每个 `Observer` 方法都直连底层原子量（u4-l2）。

下面几个术语会反复出现，先约定好：

| 术语 | 含义 |
|------|------|
| **fetch（拉取）** | 从底层原子量读对端进度，更新本地 `Cell` 缓存。属于 **Acquire load**。 |
| **commit（提交）** | 把本地 `Cell` 缓存的进度写回底层原子量。属于 **Release store**。 |
| **本地索引** | Frozen/Caching 在包装器里用 `Cell` 缓存的那份索引，读写不触发任何原子操作。 |
| **对端索引** | 由另一方维护、自己只能 `fetch` 读取的索引（生产者的对端索引是 `read`，消费者的是 `write`）。 |

一句话直觉：**Frozen 要求你记得「何时同步」，Caching 帮你把同步时机压到「不得不同步时」才做。**

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/wrap/caching.rs` | Caching 包装器的全部实现：结构定义、别名、`Observer`/`Producer`/`Consumer` 三段 impl。本讲的核心。 |
| `src/wrap/frozen.rs` | Caching 内部包裹的 Frozen。`fetch`/`commit` 的真正定义在这里，Caching 只是调用它。 |
| `src/rb/shared.rs` | `SharedRb` 的 `Split`/`SplitRef` 实现——证明 Caching 是 SharedRb 拆分的默认产物。 |
| `src/wrap/direct.rs` | 用来对照的 Direct 包装器（即时同步）。 |
| `src/traits/observer.rs` | `is_empty`/`is_full`/`vacant_len` 的默认实现，理解 fetch 触发点需要它。 |

## 4. 核心概念与源码讲解

### 4.1 Caching 包装器：在 Frozen 之上加一层自动同步

#### 4.1.1 概念说明

先回顾 `Frozen` 的痛点（u4-l3）：它把 `read`/`write` 索引缓存在本地 `Cell` 里，索引变化先停留本地，必须你**手动**调用 `commit`/`fetch`/`sync` 才会与底层同步。这样虽然能「N 次跨核同步摊薄成 1 次」，但代价是：

- 漏 `fetch`：会误判缓冲区已满/已空（用陈旧的对端索引）。
- 忘 `commit`：对端看不到你写/读的数据。

也就是说，**Frozen 把正确性交给了调用者**。这在「我知道接下来要批量写 1000 个元素、中间不需要对端看到」的精细场景里很有用，但在「我就想正常 push/pop」的日常场景里太累，还容易出错。

`Caching` 解决的就是这个问题：它**不发明新机制**，而是「包住一个 `Frozen`」，并自动选择同步时机，让你既能享受 Frozen 的缓存红利，又不用自己管 `commit`/`fetch`。它的模块注释说得很直白：

> Fetches changes from the ring buffer only when there is no more slots to perform requested operation.
> （只在「没有空位完成本次操作」时才从环形缓冲区拉取变更。）

这就是「按需同步」（on-demand synchronization）——绝大多数操作走纯本地缓存的快路径，只有当本地缓存说「我做不了」时，才花一次原子 load 去核对真实状态。

> 提示：可以把 Caching 想成「会自动踩刹车的 Frozen」。Frozen 是手动挡，Caching 是自动挡，二者的传动系统（`commit`/`fetch`）完全一样，区别只在「谁来踩」。

#### 4.1.2 核心流程

Caching 的自动同步可以用三条规则概括，分别对应写端、读端、观测端：

1. **写端（`CachingProd`）——「满时才 fetch，成功就 commit」**
   - 每次 `try_push` 先看本地缓存：本地觉得没满，就直接写、不动原子量。
   - 本地觉得满了，才 `fetch` 一次（拉取消费者的 `read` 进度，看看是不是消费者其实已经腾出空间了）。
   - 一旦本次写入成功，**立刻** `commit`（把新的 `write` 进度写回底层，让消费者能看见）。

2. **读端（`CachingCons`）——「空时才 fetch，取到就 commit」**
   - 每次 `try_pop` 先看本地缓存：本地觉得非空，就直接取、不动原子量。
   - 本地觉得空了，才 `fetch` 一次（拉取生产者的 `write` 进度，看看是不是生产者其实已经塞了东西）。
   - 一旦本次读取成功，**立刻** `commit`（把新的 `read` 进度写回底层，让生产者知道空间被释放）。

3. **观测端（`Observer` 的索引查询）——「查对端索引才 fetch」**
   - 你显式调用 `read_index()` 时：如果你是写端（`P`），`read` 是对端索引，会先 `fetch` 再返回；如果你是读端（`C`），`read` 是自己的索引，直接返回本地缓存。
   - `write_index()` 同理对称。
   - 这样保证你「主动观测」到的索引不会是陈旧误导值，同时不污染数据面的快路径。

快路径的伪代码（写端）：

```
CachingProd::try_push(elem):
    if 本地缓存.满了():          # 纯 Cell 读，0 原子操作
        frozen.fetch()            # 1 次 Acquire load（仅此时）
    r = frozen.try_push(elem)     # 用本地缓存判定+写入，0 原子操作
    if r 成功:
        frozen.commit()           # 1 次 Release store（每次成功都要让对端看见）
    return r
```

注意「成功就 commit」是**不可省略**的：消费者必须能尽快看到刚写入的元素，否则就退化成一个「延迟可见」的 Frozen 了。Caching 省的是 **load（fetch）**，不是 **store（commit）**——这一点是本讲的核心洞见，4.2 节会用原子计数精确说明。

#### 4.1.3 源码精读

Caching 的结构极简，它就是「一个 `Frozen` 加一层自动化逻辑」：

[src/wrap/caching.rs:16-19](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/caching.rs#L16-L19) —— `Caching` 结构体，唯一字段就是内部那个 `Frozen`。Caching 没有自己的状态，所有缓存能力都复用 Frozen。

[src/wrap/caching.rs:22-24](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/caching.rs#L22-L24) —— 两个类型别名：`CachingProd<R> = Caching<R, true, false>`（只写）、`CachingCons<R> = Caching<R, false, true>`（只读）。和 `FrozenProd`/`FrozenCons`、`Prod`/`Cons` 一样，用 const generic 布尔 `P`（写权）/`C`（读权）编码权限。

[src/wrap/caching.rs:26-43](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/caching.rs#L26-L43) —— 构造与转换。`new(rb)` 直接委托 `Frozen::new(rb)`，因此同样会用 hold 标志断言「至多一个写端、一个读端」（重复拆分会 panic，见 u4-l2/u5-l2）。`freeze(self)` 把 Caching 降级回 Frozen，把「自动挡」交还给用户手动操控——这是 Caching 与 Frozen 互通的开关。

[src/wrap/caching.rs:45-54](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/caching.rs#L45-L54) —— `Wrap` trait 实现，全部委托给内部 `frozen`（`rb_ref`/`into_rb_ref`）。这意味着销毁 Caching 时，`into_rb_ref` 会经 Frozen 的析构路径「先 `commit` 再 `close` 复位 hold」，保证未提交的写入不丢失、缓冲区可被重新拆分（详见 u4-l1）。

> 本节的结论：Caching 是一层**薄包装**，它的全部魔力来自对内部 Frozen 的 `fetch`/`commit`/`try_push`/`try_pop` 的「按需」调度。理解了 Frozen，Caching 就只剩「调度时机」一个问题。

#### 4.1.4 代码实践

**实践目标**：亲手验证「Caching 与 Direct 行为完全等价，都是正确的 SPSC FIFO」，并建立「Caching 内部就是一个 Frozen」的直观印象。

**操作步骤**：新建一个 binary（例如 `examples/caching_vs_direct.rs`），写下面这段「示例代码」。注意它需要 `alloc` feature（默认开启）。

```rust
// 示例代码
use ringbuf::{traits::*, HeapRb, Prod, Cons, Arc};

fn main() {
    // === ① Caching：SharedRb::split 的默认产物 ===
    let caching_rb = HeapRb::<i32>::new(4);
    let (mut c_prod, mut c_cons) = caching_rb.split(); // 得到 CachingProd / CachingCons

    // === ② Direct：手动用 Prod/Cons（即 Direct<_,true,false> / Direct<_,false,true>）包装 ===
    let direct_rb = HeapRb::<i32>::new(4);
    let arc = Arc::new(direct_rb);
    let mut d_prod = Prod::new(arc.clone()); // Direct 即时同步
    let mut d_cons = Cons::new(arc);

    // 行为等价性：两种包装都正确实现 FIFO
    for v in 0..4 {
        assert_eq!(c_prod.try_push(v), Ok(()));
        assert_eq!(d_prod.try_push(v), Ok(()));
    }
    assert_eq!(c_prod.try_push(4), Err(4)); // 满
    assert_eq!(d_prod.try_push(4), Err(4)); // 满

    for v in 0..4 {
        assert_eq!(c_cons.try_pop(), Some(v));
        assert_eq!(d_cons.try_pop(), Some(v));
    }
    assert_eq!(c_cons.try_pop(), None); // 空
    assert_eq!(d_cons.try_pop(), None); // 空

    println!("Caching 与 Direct 行为一致，FIFO 正确");
}
```

**需要观察的现象**：两组断言全部通过，说明在单线程可见行为上 Caching 与 Direct **不可区分**——它们的差别只在「内部做了多少次原子操作」，不在「结果」。

**预期结果**：程序正常打印 `Caching 与 Direct 行为一致，FIFO 正确`。

> 待本地验证：`Prod`/`Cons` 即 `Direct` 的别名、`ringbuf::Arc` 即 `alloc::sync::Arc`（默认非 portable-atomic 时）。若你的工具链 feature 配置不同，请以本地 `cargo expand`/编译结果为准。

#### 4.1.5 小练习与答案

**练习 1**：Caching 结构体里有没有它「自己独有」的字段？如果没有，它的能力从哪里来？

**参考答案**：没有。`Caching` 只有一个字段 `frozen: Frozen<R, P, C>`（[caching.rs:17-19](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/caching.rs#L17-L19)）。它的缓存能力、`commit`/`fetch` 机制、hold 断言全部复用自内部 Frozen，Caching 只新增了「何时调用这些机制」的调度逻辑。

**练习 2**：调用 `caching.freeze()` 之后，得到的对象是 Caching 还是 Frozen？此后你写入的数据还会自动 commit 吗？

**参考答案**：得到的是 `Frozen<R, P, C>`（[caching.rs:40-42](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/caching.rs#L40-L42)）。不再自动 commit——你又回到了手动挡，必须自己 `commit`/`sync`，或依赖 Drop 时的隐式 commit（见 u4-l3）。

---

### 4.2 CachingProd：写端「满时拉取、写后提交」

#### 4.2.1 概念说明

`CachingProd` 是 Caching 的写端别名（`Caching<R, true, false>`）。它只重写了 `Producer` trait 的两个方法——底层的 `set_write_index` 和高层的 `try_push`，其余（`push_iter`、`push_slice`、`vacant_slices_mut` 等）全部继承 `Producer` 的默认实现。也就是说，CachingProd 的「自动化」只插手两处关键节点，就把整条写路径变成了按需同步。

为什么写端要「满时才 fetch」？因为生产者唯一需要关心的对端信息是**消费者把 `read` 推进到哪了**（这决定了还有没有空位）。只要本地缓存的 `read` 表明「还有空位」，生产者就完全不需要去读真实的原子 `read`——它知道自己上一次 fetch 后消费者只会让空位**变多**（消费只会前进、不会后退）。只有当本地缓存说「满了」时，才有可能「其实消费者已经腾出空间了，只是我还不知道」，这时才值得花一次原子 load 去 `fetch` 核对。

反过来，为什么「成功就 commit」不能省？因为消费者要靠真实的原子 `write` 才能看到新元素。若不 commit，就退化成 Frozen（写入对端不可见）。所以每一次成功写入都必须 `commit`——这是正确性要求，与优化无关。

#### 4.2.2 核心流程

把 `CachingProd::try_push` 的同步时机画成状态流：

```
                       ┌─ 本地缓存说"满了"? ─┐
                       │      否              是
        try_push ──────┤                      │
                       │                      ▼
                       │               frozen.fetch()   ← 1× Acquire load（拉消费者 read）
                       │                      │
                       └──────────┬───────────┘
                                  ▼
                        frozen.try_push(elem)  ← 用本地缓存判定+写 MaybeUninit 槽
                                  │             （advance 推进的是本地 write，0 原子）
                                  ▼
                          ┌── 成功? ──┐
                          │ 否        是
                          │           │
                          │           ▼
                          │    frozen.commit()  ← 1× Release store（发布新 write）
                          ▼           │
                     返回 Err(elem)   ▼
                              返回 Ok(())
```

关键计数（缓冲区**未满**的快路径，即最常见的场景）：

- `fetch`：0 次（本地没满，跳过）。
- `commit`：1 次（成功必提交）。
- 也就是说，快路径里只有 **1 次 Release store**，**0 次 Acquire load**。

对照 `Direct`（u4-l2）的 `try_push`：每次都要 `is_full()` → 读 `write_index` 和 `read_index` 两个原子量（2 次 Acquire load），再 `advance` → 1 次 Release store。即 **2 次 load + 1 次 store**。

于是 Caching 相对 Direct，在单次 `try_push` 快路径上省下了 **2 次 Acquire load**——具体说就是省去了「每次都去读消费者的 `read` 进度」这件事。

> 提示：别误以为 Caching 连 store 也省了。发布写入这件事两种包装都得做，是 SPSC 可见性的硬性要求。Caching 省的是「核对对端进度」的 load。

#### 4.2.3 源码精读

写端只重写两个方法，先看高层 `try_push`：

[src/wrap/caching.rs:114-123](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/caching.rs#L114-L123) —— `CachingProd::try_push`。三步正是 4.2.2 的流程：①`self.frozen.is_full()` 判定（`is_full` 走 Frozen 的 `Observer`，即 [frozen.rs:159-166](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L159-L166) 的 `read.get()`/`write.get()` 纯 Cell 读），本地满了才 `self.frozen.fetch()`；②委托 `self.frozen.try_push(elem)`（这是 `Producer` 的默认实现，在 Frozen 上用本地缓存工作）；③成功才 `self.frozen.commit()`。

[src/wrap/caching.rs:107-112](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/caching.rs#L107-L112) —— 底层 `set_write_index` 重写。注意它先 `self.frozen.set_write_index(value)`（推进**本地** write，见 [frozen.rs:185-190](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/frozen.rs#L185-L190)），再**立刻** `self.frozen.commit()`。这保证 `push_slice`/`push_iter` 这类走 `advance_write_index`（→ `set_write_index`）的批量方法也「一次批量 = 一次 commit」，保住了批量方法的单次同步优势。

接着看一个容易被忽略、却很巧妙的设计——`Observer` 对索引查询的重写：

[src/wrap/caching.rs:75-88](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/caching.rs#L75-L88) —— `read_index`/`write_index`。注意条件分支用的不是 `C`/`P` 一一对应，而是「按方向」：

- `read_index()` 里写的是 `if P { fetch }`：当你是**写端（P）**时，`read` 属于对端（消费者），所以要 `fetch` 拿到较新值；你是读端时 `read` 是自己的，直接读本地缓存。
- `write_index()` 里写的是 `if C { fetch }`：当你是**读端（C）**时，`write` 属于对端（生产者），所以要 `fetch`。

这保证「主动观测」拿到的索引不会误导你（比如调试时打印 `prod.occupied_len()` 不会看到陈旧到离谱的值），而又**不污染数据面快路径**——因为 `try_push` 内部调用的是 `self.frozen.is_full()`（直接打 Frozen 的 Cell），根本没走这里被 fetch 过的 `read_index`。

> 小结：CachingProd 的自动化只插在 `try_push`（满时 fetch）和 `set_write_index`（写后 commit）两处，其余继承默认实现。快路径 = 0 load + 1 store。

#### 4.2.4 代码实践

**实践目标**：通过源码推理，填出下表，量化 `CachingProd` 与 `Direct`（`Prod`）在「连续 N 次 `try_push`、缓冲区一直未满」场景下的原子操作次数。

**操作步骤**：

1. 阅读上述三个代码点，逐行标注每次 `try_push` 走过哪些原子 load/store。
2. 对照 `Direct` 的路径：`src/wrap/direct.rs:101-132`（Observer 直连原子量）+ `src/traits/observer.rs:49-76`（`is_full`/`vacant_len` 默认实现读两个索引）+ `src/wrap/direct.rs:134-139`（`set_write_index` 即 Release store）。
3. 完成「操作次数」表格。

**需要观察的现象（源码推理）**：

| 路径 | 每次 try_push 的 Acquire load | 每次 try_push 的 Release store | N 次总量（未满） |
|------|:---:|:---:|---|
| `Direct`（`Prod`） | 2（`write_index` + `read_index`） | 1 | 2N load + N store |
| `CachingProd` | 0（本地缓存判定，未满不 fetch） | 1（成功必 commit） | **0** load + N store |

**预期结果**：你会发现两者 store 次数相同（都要发布写入），但 Caching 把 load 从 2N 降到 0——这正是「按需同步」的收益，也是 `SharedRb` 默认选 Caching 的根本原因。

> 待本地验证：精确的运行时原子操作计数需借助 perf/LLVM IR 或硬件计数器，上表是基于源码逻辑的静态推理结论。

#### 4.2.5 小练习与答案

**练习 1**：假设消费者一直不消费，生产者连续 `try_push` 到缓冲区满。从第 1 次到「最终返回 `Err`」的那次，`CachingProd` 总共触发了几次 `fetch`？

**参考答案**：在「一直未满」阶段 0 次 fetch；当本地缓存首次判定「满了」时触发 1 次 `fetch`（[caching.rs:115-117](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/caching.rs#L115-L117)）；`fetch` 后若真实也满，`frozen.try_push` 返回 `Err`、不 commit、也不再 fetch。所以全程大致 **1 次 fetch**（每次「本地判满」时各一次）。对照 Direct 每次 `try_push` 都 fetch（load），差别随 N 线性放大。

**练习 2**：为什么 `CachingProd::set_write_index`（[caching.rs:108-112](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/caching.rs#L108-L112)）要在推进本地索引后**立即** commit，而不是等批量结束？

**参考答案**：因为它要服务 `push_slice`/`push_iter` 这类「调一次 `advance_write_index` 完成整批」的默认实现。在 `set_write_index` 里立即 commit，意味着「一次批量写入 = 一次 commit」，既保证整批数据在批量结束后立刻对消费者可见，又把同步开销压成 1 次 Release store。若不在这里 commit，批量方法就会「写了但对端看不见」，违反 Caching 的「成功即对外可见」语义。

---

### 4.3 CachingCons 与 SharedRb 的默认选择

#### 4.3.1 概念说明

`CachingCons`（`Caching<R, false, true>`）是 Caching 的读端别名。它和 `CachingProd` 完全对称——消费者唯一关心的对端信息是**生产者把 `write` 推进到哪了**（决定有没有新元素可取）。只要本地缓存的 `write` 表明「还有元素」，消费者就不必去读真实原子 `write`——因为生产者只会让元素变多。只有本地缓存说「空了」时，才 `fetch` 核对。取到元素后立即 `commit`（发布新的 `read`，让生产者知道空间被释放）。

本模块还要回答一个贯穿全讲的问题：**为什么 `SharedRb::split` 默认产出 Caching，而不是 Direct？**

答案是「索引存储代价驱动包装器选择」（这条规律在 u2-l3、u4-l2 已多次出现）：

- `LocalRb`（单线程，`Cell` 索引）读写索引**零跨核代价**，所以默认用 `Direct`（即时同步，简单且无额外开销）。
- `SharedRb`（多线程，原子索引）每次原子读写都触发**跨 CPU 核的缓存同步**，代价高昂。若用 `Direct`，每次 `try_push`/`try_pop` 都要做 2 load + 1 store 的跨核同步；用 `Caching` 则把 load 摊薄到「不得不做时」，在保持正确性与即时可见性的前提下显著降低跨核通信频率。

#### 4.3.2 核心流程

`CachingCons::try_pop` 与写端镜像对称：

```
                       ┌─ 本地缓存说"空了"? ─┐
                       │      否              是
        try_pop ───────┤                      │
                       │                      ▼
                       │               frozen.fetch()   ← 1× Acquire load（拉生产者 write）
                       │                      │
                       └──────────┬───────────┘
                                  ▼
                        frozen.try_pop()      ← 用本地缓存判定+assume_init_read 移出元素
                                  │             （advance 推进本地 read，0 原子）
                                  ▼
                          ┌── 取到? ──┐
                          │ None      Some
                          │           │
                          │           ▼
                          │    frozen.commit()  ← 1× Release store（发布新 read）
                          ▼           │
                     返回 None        ▼
                              返回 Some(elem)
```

快路径（缓冲区**非空**）计数：**0 load + 1 store**。对照 `Direct` 的 `try_pop`（2 load + 1 store），同样省下 2 次 load。

而 `SharedRb::split` 默认产出 Caching 的「接线」，定义在 `shared.rs` 的 `Split` 实现里：

```
SharedRb::split(self)        →  Arc::new(self).split()
Arc<SharedRb>::split(self)   →  (CachingProd::new(self.clone()), CachingCons::new(self))
SharedRb::split_ref(&mut)    →  (CachingProd::new(self),         CachingCons::new(self))
```

无论按值拆分（`split`，基于 `Arc`）还是借用拆分（`split_ref`，基于 `&'a`），两端都是 `CachingProd`/`CachingCons`。这就是 `HeapRb::split()` 返回 `HeapProd`/`HeapCons`（= `CachingProd/Cons<Arc<HeapRb<T>>>`）的由来（u2-l3 的别名在这里落地）。

#### 4.3.3 源码精读

读端两个方法与写端一一镜像：

[src/wrap/caching.rs:126-143](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/caching.rs#L126-L143) —— `CachingCons` 的 `Consumer` impl。`set_read_index`（推进本地 `read` 后立即 commit）与 `try_pop`（本地空才 fetch、取到才 commit）和写端完全对称，不再赘述。

然后是决定性的「默认产物」证据：

[src/rb/shared.rs:154-162](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L154-L162) —— `SharedRb<S>` 的 `Split` 实现：关联类型 `Prod = CachingProd<Arc<Self>>`、`Cons = CachingCons<Arc<Self>>`。这就是「SharedRb 默认用 Caching」的源头。

[src/rb/shared.rs:163-171](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L163-L171) —— `Arc<SharedRb<S>>::split`：直接 `CachingProd::new(self.clone())` 与 `CachingCons::new(self)`。`SharedRb::split`（上一段）就是先把 self 包进 `Arc` 再走这里。

[src/rb/shared.rs:181-194](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L181-L194) —— `SplitRef`（借用拆分，无需 `alloc`）同样产出 `CachingProd<&'a Self>`/`CachingCons<&'a Self>`。`StaticRb::split_ref()` 的 `StaticProd`/`StaticCons` 别名（[alias.rs:20-23](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/alias.rs#L20-L23)）正是由此而来。

最后回头看为什么要默认 Caching——底层原子的代价：

[src/rb/shared.rs:87-102](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L87-L102) —— SharedRb 的 `read_index`/`write_index` 都是 `load(Acquire)`，每次调用即一次跨核读。

[src/rb/shared.rs:123-135](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L123-L135) —— `set_write_index`/`set_read_index` 都是 `store(Release)`，每次调用即一次跨核写。

正因为这些方法「很贵」，`SharedRb` 才默认用 Caching 把对它们的调用频率降到最低；而 `LocalRb` 的同名方法只是 `Cell::get`/`set`（无跨核代价），所以默认用 Direct。这就是全讲的落脚点：**包装器策略由索引存储代价决定**。

> 边界提示：`push_slice`/`push_iter`、`pop_slice`/`pop_iter` 这些批量方法在 `CachingProd`/`CachingCons` 上**没有**被重写，继承默认实现，它们**不会**在开头 `fetch`。所以若本地缓存的「对端索引」很陈旧，批量方法可能少写/少读一些（最坏只是少搬运、不丢数据，调用方可重试）。这是 Caching「只优化最热的 `try_push`/`try_pop`」的取舍——若你确定要大批量搬运且关心满载利用率，可像 README 建议的那样先 `freeze` 再手动 `sync`。

#### 4.3.4 代码实践

**实践目标**：验证「`SharedRb` 的 `split`/`split_ref` 确实产出 CachingProd/CachingCons」，并亲手从「同一个 `Arc<SharedRb>`」构造出 Caching 两端。

**操作步骤**：运行下面这段「示例代码」，它绕过 `SharedRb::split`、直接模仿 [shared.rs:168-170](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L168-L170) 的内部做法手动构造两端。

```rust
// 示例代码
use ringbuf::{traits::*, HeapRb, Arc, CachingProd, CachingCons};

fn main() {
    let rb = HeapRb::<i32>::new(4);
    let arc = Arc::new(rb);

    // 这正是 Arc<SharedRb>::split 内部做的事
    let mut prod: CachingProd<Arc<HeapRb<i32>>> = CachingProd::new(arc.clone());
    let mut cons: CachingCons<Arc<HeapRb<i32>>> = CachingCons::new(arc);

    prod.try_push(7).unwrap();
    prod.try_push(8).unwrap();
    assert_eq!(cons.try_pop(), Some(7)); // FIFO
    assert_eq!(cons.try_pop(), Some(8));
    println!("手动构造的 Caching 两端工作正常");
}
```

**需要观察的现象**：程序编译通过、运行正确，说明 `split()` 返回的类型本质上就是 `CachingProd`/`CachingCons`，你完全可以「跳过 `split`、自己 new」得到等价结果。

**预期结果**：打印 `手动构造的 Caching 两端工作正常`。

**进阶观察（源码推理）**：把 4.1.4 的程序与 4.3.4 的程序放在一起看——前者用 `Prod`/`Cons`（Direct，2 load + 1 store/次），后者用 `CachingProd`/`CachingCons`（Caching，0 load + 1 store/次）。在多线程密集 `push`/`pop` 下，后者的跨核 load 次数远低于前者，这正是 Caching 作为默认策略的价值。

> 待本地验证：若想用真实线程压测对比延迟，可用 `std::thread` 起生产/消费两线程，配合 `cargo bench`（需 `bench` feature）测量吞吐，但精确归因到 load/store 次数仍需结合本讲的源码推理。

#### 4.3.5 小练习与答案

**练习 1**：`CachingCons::try_pop`（[caching.rs:133-142](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/caching.rs#L133-L142)）在快路径（非空）下触发几次原子 load、几次原子 store？

**参考答案**：0 次 load（`is_empty` 走本地 Cell，非空不 fetch），1 次 store（取到元素后 `commit` 发布新 `read`）。与 `CachingProd::try_push` 完全对称。

**练习 2**：如果你把 `SharedRb::split` 的产物当成「Frozen」来用（即写完不指望对端立刻看到、自己控制同步），会发生什么？为什么不会真的「冻结」？

**参考答案**：你拿到的其实是 `CachingProd`/`CachingCons`，不是 `Frozen`。即使你不主动调 `sync`，Caching 也会在每次成功 `try_push`/`try_pop` 后**自动 commit**（[caching.rs:119-121](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/caching.rs#L119-L121)、[caching.rs:138-140](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/caching.rs#L138-L140)），所以数据会立刻对端可见——它根本不会「冻结」。想要真正的手动冻结，得先调 `.freeze()` 降级成 `Frozen`（见 4.1）。

**练习 3**：用一句话解释「为什么 `LocalRb::split` 默认用 Direct，而 `SharedRb::split` 默认用 Caching」。

**参考答案**：因为 Direct 每次 `try_push`/`try_pop` 都要读写底层索引；`LocalRb` 的索引是 `Cell`（无跨核代价），直接同步最简单且零额外开销，故用 Direct；`SharedRb` 的索引是原子量（每次读写都触发跨核缓存同步、代价高），需要用 Caching 把 load 摊薄到「不得不做时」以降低跨核通信频率，故用 Caching。

## 5. 综合实践

**任务**：编写一个「Direct vs Caching 原子操作分析器」，把本讲所有要点串起来。

**要求**：

1. 对同一个 `HeapRb::<i32>::new(K)`（K 取 8），分别用两种方式拆出写端/读端：
   - **Caching 组**：直接 `rb.split()`（得到 `CachingProd`/`CachingCons`）。
   - **Direct 组**：`Arc::new(rb2)` 后 `Prod::new`/`Cons::new`（得到 `Direct` 两端）。
2. 在两组上各执行「先连续 `try_push` 把缓冲区写满、再 `try_pop` 清空」一轮，验证两者的最终结果完全一致（FIFO 正确、满则 `Err`、空则 `None`）。
3. **分析（源码推理为主）**：填写下表，回答「一轮『写满 K 个 + 清空 K 个』中，两组各触发多少次 Acquire load / Release store」。以 K=8 为例给出数字，并说明 Caching 的 load 主要发生在哪些时刻。

| 操作序列（K=8） | Direct：load / store | Caching：load / store |
|---|---|---|
| 8 次 `try_push`（前 8 次都能写入，写满） | / | / |
| 第 9 次 `try_push`（返回 `Err`） | / | / |
| 8 次 `try_pop`（清空） | / | / |
| 第 9 次 `try_pop`（返回 `None`） | / | / |

**提示（先自己算，再核对）**：

- Direct：每次 `try_push`/`try_pop` 都 2 load + 1 store（无论成败，`is_full`/`is_empty` 都要读两个索引）。
- Caching：写入阶段前 8 次快路径 0 load + 1 store；第 9 次「本地判满」触发 1 次 fetch（1 load），写失败不 commit；读取阶段前 8 次快路径 0 load + 1 store；第 9 次「本地判空」触发 1 次 fetch（1 load），取空不 commit。

**预期产出**：一段可运行的 Rust 程序（行为验证）+ 一份填好的表格（源码推理）+ 一段结论：**Caching 在保持与 Direct 完全相同的可见行为的前提下，把跨核 Acquire load 的次数从 O(操作数) 降到 O(满/空边界数)，这正是它成为 `SharedRb` 默认包装策略的原因。**

> 待本地验证：行为部分可由程序断言确认；原子操作次数为源码静态推理结论，运行时精确计数需借助性能分析工具。

## 6. 本讲小结

- `Caching` 是一层薄包装，内部只有一个 `Frozen` 字段（[caching.rs:16-19](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/wrap/caching.rs#L16-L19)），全部缓存能力复用 Frozen，自己只新增「何时 fetch/commit」的调度。
- 写端 `CachingProd::try_push` 的法则是「本地判满才 fetch、成功立即 commit」；读端 `CachingCons::try_pop` 对称为「本地判空才 fetch、取到立即 commit」。快路径只有 **0 load + 1 store**。
- `set_write_index`/`set_read_index` 被重写为「推进本地 + 立即 commit」，使 `push_slice`/`push_iter` 等「一次批量 = 一次 commit」的默认实现保持单次同步优势。
- `Observer::read_index`/`write_index` 采用「查对端索引才 fetch」的按方向重写，保证主动观测不误导，又不污染数据面快路径。
- `SharedRb::split`/`split_ref` 默认产出 `CachingProd`/`CachingCons`（[shared.rs:154-194](https://github.com/agerasev/ringbuf/blob/7e15db42ba533ecbe205e9aa4c8f04024009f411/src/rb/shared.rs#L154-L194)）；根因是 SharedRb 的索引是昂贵的原子量，Caching 把跨核 load 摊薄到「不得不做时」，而 `LocalRb`（廉价 Cell 索引）则默认用 Direct。
- 三种同步策略形成谱系：**Direct（即时同步）→ Frozen（完全手动同步）→ Caching（按需自动同步）**，Caching 是「正确性像 Direct、跨核开销逼近 Frozen」的折中默认项。

## 7. 下一步学习建议

- 进入 u5（专家层）理解 Caching 自动同步背后的**内存顺序**保障：阅读 `SharedRb` 的 `Acquire` load / `Release` store 如何在生产/消费两端建立 happens-before，保证 Caching 在 `commit` 后的数据能被对端 `fetch` 正确读到（u5-l1）。
- 阅读关于 **hold flags** 的 u5-l2，弄清 Caching `new`/`close`（经 Frozen）如何置位/复位 `read_held`/`write_held`，从运行时强制 SPSC 不变量。
- 若对「批量搬运」场景感兴趣，可结合 u8-l4 的 `transfer`，体会 Caching 默认包装在缓冲区间搬运元素时的同步开销，并对比「freeze + 手动 sync」能否进一步优化。
- 动手尝试：把本讲综合实践里的 K 调大、用两个真实线程做密集 push/pop，用 `cargo bench`（`bench` feature）粗测 Direct 与 Caching 的吞吐差异，把源码推理与实测对应起来。
