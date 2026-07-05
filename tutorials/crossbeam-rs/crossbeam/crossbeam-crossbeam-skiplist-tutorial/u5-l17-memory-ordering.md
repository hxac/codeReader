# 内存序分析：Relaxed/Release/Acquire/SeqCst

## 1. 本讲目标

本讲是专家层「内存序」专题，只读一个文件 [`src/base.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs)。学完后你应该能够：

1. 把 `base.rs` 里每一处原子操作的 `Ordering` 选择说清楚「为什么是这个强度」，而不是死记。
2. 理解引用计数的 `fetch_sub(Release)` + `fence(Acquire)` 组合如何保证「最后一个减计数者安全地析构节点」。
3. 理解 `mark_tower` 与关键 CAS 为什么保守地用 `SeqCst`，以及源码中那一串 `TODO(Amanieu): can we use ... here?` 到底在权衡什么。
4. 能判断 `epoch::unprotected()` 在哪些场景下安全、为什么安全。

本讲不引入新算法，全部是对 u3-l8/u3-l10/u3-l11 三讲已讲过的 search/insert/remove 主链路做「内存序视角」的二次精读。

## 2. 前置知识

### 2.1 为什么需要内存序

CPU 和编译器都会**重排**内存读写以提升性能。单线程下重排是透明的（不影响可观测结果），但多线程下，如果没有同步，线程 A 的「写 x 再写 flag」可能被另一线程观测成「flag 先置位、x 还是旧值」。

Rust 的原子操作用 `Ordering` 参数告诉编译器/CPU：这一次操作要施加多强的**禁止重排**约束，从而在线程间建立 **happens-before（先行发生）** 关系。强度越高越安全，但也越慢。

### 2.2 四种核心排序强度

| Ordering | 直觉 | 在 `base.rs` 中扮演的角色 |
|---|---|---|
| `Relaxed` | 只保证本操作原子，**不**建立任何 happens-before，不阻止重排 | 「近似值」「提示值」「自洽的计数」，正确性不依赖精确值 |
| `Release`（写端）| 本次写之前的所有读写，对读到这次写的线程都可见 | 「发布」一个新状态：发布者写完数据后再翻标志位 |
| `Acquire`（读端）| 读到 Release 写入的值后，之后的所有读写都不会被重排到这次读之前 | 「获取」一个状态：读到标志位后才去访问对应数据 |
| `SeqCst` | 在 Release/Acquire 基础上，额外保证所有 `SeqCst` 操作存在一个**全局统一顺序**（total order） | 保守选择：当跨多个原子位置的「相对顺序」也要确定时使用 |

### 2.3 一个心智模型

把 `Ordering` 想成「同步的音量旋钮」：

- `Relaxed` = 静音。只管我自己这一下是原子的。
- `Release`/`Acquire` = 一对对讲机。发布者（Release）和获取者（Acquire）之间能听见彼此之前的操作。
- `SeqCst` = 全场广播。所有人不仅两两能听见，而且对「谁先谁后」达成唯一一致。

`base.rs` 的设计原则可以一句话概括：**能用 Relaxed 就用 Relaxed；只有在「发布新节点」「安全回收」「全局可见的删除标记」这三类点上，才把音量调到 Release/Acquire 或 SeqCst。** 这正是本讲要逐点验证的。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [`src/base.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs) | 唯一精读对象。无锁跳表全部算法与所有原子操作都在这里 |
| [`src/lib.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs) | 「Garbage collection」「Concurrent access」章节提供内存回收与并发语义的高层说明 |

## 4. 核心概念与源码讲解

### 4.1 全局视角：base.rs 的内存序「强度梯度」

#### 4.1.1 概念说明

读 `base.rs` 的内存序，最忌讳逐行硬背。正确的方法是先建立一个**强度梯度**的地图：作者在不同位置选了不同强度，背后是同一个问题——「这一下原子操作，到底需要保证什么？」

`base.rs` 里的原子操作可以归成五大类，强度从弱到强：

1. **纯近似值/提示**（`Relaxed`）：`len`、`seed`、`max_height`。正确性不依赖精确值。
2. **读路径解引用指针**（`load_consume` ≈ acquire）：搜索时加载后继指针。需要保证指针指向的内容已对当前线程可见。
3. **引用计数**（`Release` 写 + `Acquire` fence）：`decrement`。保证「最后一个减计数者」看到此前所有写。
4. **协作式清理**（`Release` CAS）：`help_unlink`。发布「已物理摘除」的新拓扑。
5. **关键线性化点**（`SeqCst`）：`mark_tower`、insert 的 level0 安装 CAS、建塔 CAS、remove 的摘除 CAS。需要跨多个原子位置达成全局一致顺序。

#### 4.1.2 核心流程

判断任意一处 `Ordering` 选择的「决策树」：

```text
这一下操作，读取/写入的值，是否影响「正确性」？
├─ 否（只是近似/提示/纯本地计数）        → Relaxed
└─ 是
    ├─ 是否需要「读到指针后安全解引用其内容」？ → load_consume (acquire)
    ├─ 是否是「发布新拓扑/新状态」且读者已知如何同步？
    │   ├─ 协作清理，单点发布即可            → Release（CAS 成功端）
    │   └─ 需要跨多个原子位置的全局一致顺序   → SeqCst
    └─ 是否是「计数减到 0 → 析构」？          → fetch_sub(Release) + fence(Acquire)
```

#### 4.1.3 源码精读

`base.rs` 顶部的 `use` 直接把所有用到的排序符号一次性导入，这是本讲的「词汇表」：

[base.rs:4-13](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L4-L13) 引入 `AtomicUsize`、`Ordering`、`fence`，以及 crossbeam-epoch 的 `Atomic`/`Collector`/`Guard`/`Shared`。

注意 `Ordering` 同时用于两类原子：
- 标准库 `AtomicUsize`（`refs_and_height`、`HotData` 的 `len/seed/max_height`）。
- crossbeam-epoch 的 `Atomic<Node>`（塔里的指针），它的 `load/store/compare_exchange/fetch_or` 同样接受 `Ordering`，但额外需要一个 `&Guard`（或 `unprotected()`）参数——这第二个参数就是 epoch 机制对「这次加载的指针能否安全解引用」的额外保护，详见 4.6。

#### 4.1.4 代码实践

**实践目标**：建立全局清单。

**操作步骤**：

1. 用编辑器打开 `src/base.rs`，全局搜索 `Ordering::`。
2. 把每一处按 `Relaxed / Release / Acquire / SeqCst` 分成四列。
3. 在每一处旁边用一句话标注「它保护的是什么不变式」。

**预期结果**：你会得到一张约 30+ 行的表，其中 `Relaxed` 占绝大多数，`SeqCst` 集中在 `mark_tower` 和 insert/remove 的少数几个 CAS 上，`Release`/`Acquire` 几乎只出现在引用计数。这与 4.1.1 的强度梯度吻合。

**待本地验证**：上述计数与你的搜索结果一致（不同版本的行数可能略有差异）。

#### 4.1.5 小练习与答案

**练习 1**：`base.rs` 里有没有任何一处用 `Ordering::Acquire` 做普通 `load`？
**答案**：没有。读路径用的是 crossbeam-epoch 的 `load_consume`（见 4.3），而标准的 `Acquire` 只以 `fence(Ordering::Acquire)` 的形式出现在引用计数减到 0 的分支里（见 4.4）。这是本库的一个风格特征：acquire 语义几乎全部由 `load_consume` 和那一处 fence 承担。

---

### 4.2 hot_data 的 Relaxed 载入：近似值与提示

#### 4.2.1 概念说明

`HotData` 是跳表里「频繁被多线程读写、但值本身只起提示作用」的三个计数器：

```rust
struct HotData {
    seed: AtomicUsize,       // 随机高度的种子
    len: AtomicUsize,        // 元素个数
    max_height: AtomicUsize, // 当前最高塔，作为搜索起点提示，只增不减
}
```

它们全部用 `CachePadded` 隔离到独立缓存行（避免 false sharing）。关键点是：**这三个值的「精确性」对正确性没有任何影响**——`len` 是近似值，`seed` 只是个伪随机种子，`max_height` 只是搜索从哪一层开始下降的提示。因此全部用 `Relaxed`。

#### 4.2.2 核心流程

- `len()`：`Relaxed` 载入后，因为并发增减，可能瞬时下溢成一个巨大值，代码显式把它当 0 处理。
- `random_height()`：`Relaxed` 读种子 → xorshift 推进 → `Relaxed` 写回；多个线程同时推进种子不会出错，只是少了点随机性。
- `max_height`：`Relaxed` 读后，如果新塔更高，用 `Relaxed` CAS 尝试抬高；它只用于搜索时「跳过空层」的加速，错一个也只会让搜索多走/少走几层，不影响正确性。

#### 4.2.3 源码精读

`len()` 的实现最能体现「近似值」哲学——载入是 `Relaxed`，并且显式容忍下溢：

[base.rs:516-522](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L516-L522) 注释明确说「由于 relaxed 内存序，长度计数器偶尔会下溢出非常大的值，我们把这种值当作 0」。这是因为 `insert` 先 `fetch_add`、命中旧 key 时再 `fetch_sub`，两者不在同一原子事务里，并发下 `len` 可能短暂越过真实值。既然本就只是近似，用 `Relaxed` 足矣。

`random_height` 里对 `seed` 与 `max_height` 的全部读写也都是 `Relaxed`：

[base.rs:708-751](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L708-L751) 种子的 `load`/`store`（L713、L717）、`max_height` 的 `load`（L738）与抬高它的 CAS（L740-L745）全部是 `Relaxed`。

搜索函数读取 `max_height` 作为起点提示，同样是 `Relaxed`：

[base.rs:846](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L846) `search_bound` 与 [base.rs:938](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L938) `search_position` 都用 `self.hot_data.max_height.load(Ordering::Relaxed)`。读到一个偏小的 `max_height` 只是让搜索从更低的层起步，多走几步水平指针；读到偏大的也无妨——紧接着的「跳过空层」循环会把它修正回来（见 L850-L857 与 L941-L949 的 `load(Ordering::Relaxed, guard).is_null()` 快速跳空层）。

> 注意：这几处 `is_null()` 用的是带 `guard` 的 `load(Relaxed, guard)`，不是 `unprotected()`。区别在 4.6 详述。

#### 4.2.4 代码实践

**实践目标**：理解「近似值为何不需要更强序」。

**操作步骤**：

1. 阅读 [base.rs:1068](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1068) 的 `len.fetch_add(1, Relaxed)`（乐观加）和 [base.rs:1090](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1090) / [base.rs:1118](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1118) 的 `len.fetch_sub(1, Relaxed)`（命中旧 key 时回退）。
2. 构造一个心智实验：线程 A 正在 `insert(k, v1)`（已 `fetch_add`，尚未决定是否替换），线程 B 同时 `len()`。

**需要观察的现象**：B 读到的 `len` 可能比「最终稳定值」大 1，也可能因为 A 的 `fetch_sub` 尚未发生而偏大；极端并发下甚至瞬时下溢。

**预期结果**：这正是 `len()` 文档说「approximation without any guarantees」的原因。`Relaxed` 足够，因为它本就不是真相来源（truth source），跳表的真值由指针拓扑维护。

#### 4.2.5 小练习与答案

**练习 1**：`max_height` 的 CAS 只升不降，为什么用 `Relaxed` 而不是 `SeqCst`？
**答案**：`max_height` 只是搜索的「起点提示」。CAS 失败说明别的线程已经抬得更高，直接 `break` 即可；读到旧值只会让本次搜索多走几步。它不参与任何 happens-before 推理，`Relaxed` 完全够用，`SeqCst` 反而白白增加 `mfence` 开销。

**练习 2**：`seed` 的 `load`/`store` 用 `Relaxed`，会不会让两个线程生成相同的随机高度？
**答案**：理论上可能短暂竞争同一旧种子，但 xorshift + `trailing_zeros` 映射到高度本身是概率性的，跳表正确性**不依赖**高度互不相同（即使两个节点同高也完全合法，只是退化为在 level 0 上多走一步）。所以 `Relaxed` 安全。

---

### 4.3 搜索路径的 load_consume：用数据依赖安全解引用指针

#### 4.3.1 概念说明

读路径（`search_bound`/`search_position`/`next_node`）需要加载塔里的后继指针，然后**解引用**它去读 `key`、读下一层后继。这就引出无锁数据结构的核心问题：

> 线程 B 加载到一个指针 `p` 时，`p` 指向的节点可能正被线程 A 摘除并即将回收。B 绝不能解引用一个已释放的指针。

`base.rs` 的读路径用 crossbeam-epoch 的 `Atomic::load_consume(&Guard)` 来同时解决两件事：
1. **同步**：建立「发布者写指针 → B 读到指针」的 happens-before，让 B 看到 `p` 指向节点里已写好的 `key/value`。
2. **回收安全**：`Guard` 把当前线程 pin 在某个 epoch，保证「B 持有 `Guard` 期间，通过它加载到的指针不会被回收」。

`load_consume` 名字里的 consume 指的是「数据依赖」排序：只要后续对 `p` 的访问依赖 `p` 的值，CPU 就不会把它们重排到加载之前。在实际实现里（Rust 的 consume 语义被降级为 acquire），它等价于一次 acquire 载入。本讲后续一律按 **「acquire 等价」** 对待它。

#### 4.3.2 核心流程

读路径上每一次「取后继指针」都是：

```text
succ = curr.get_level(level).load_consume(guard)   # acquire 等价 + epoch pin
# 此后解引用 succ 是安全的：内容已可见，且节点在本 Guard 期内不会被回收
```

`Shared` 返回值还携带一个 tag 位（最低位），用来表示「这条出边指向的后继所在的节点已被逻辑删除」。读路径据此决定重启搜索或协助清理（详见 u3-l8/u3-l9）。

#### 4.3.3 源码精读

`search_bound` 在每一层循环里加载后继：

[base.rs:869](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L869) 与 [base.rs:880](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L880) 都用 `pred.get_level(level).load_consume(guard)` 与 `c.get_level(level).load_consume(guard)`。`search_position` 同理：[base.rs:958](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L958)、[base.rs:969](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L969)。

`next_node` 在 level 0 上前进：

[base.rs:795](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L795) 与 [base.rs:804](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L804) 用 `load_consume(guard)` 加载后继。

> 关键对比：注意「跳过空层」的循环里用的是普通 `load(Ordering::Relaxed, guard)`（[base.rs:853](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L853)、[base.rs:945](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L945)），因为那里**只看 `is_null()`，不解引用**——既不需要 acquire 同步，也不需要保护 pointee，`Relaxed` + `guard`（仅借 epoch 防 pointee 在判空期间被释放）即可。

#### 4.3.4 代码实践

**实践目标**：体会「解引用指针」与「只判空」对内存序的不同要求。

**操作步骤**：

1. 在 [base.rs:843-920](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L843-L920) 的 `search_bound` 里，分别标出「只判空」的位置（`load(Relaxed, guard).is_null()`）和「要解引用」的位置（`load_consume(guard)`）。
2. 思考：如果把 `load_consume` 全部换成 `load(Relaxed, guard)`，会发生什么？

**需要观察的现象（推理）**：`Relaxed` 不建立 happens-before。后插入节点的线程写完 `key/value`、用 `SeqCst`/`Release` CAS 把指针发布后，搜索线程若用 `Relaxed` 读取该指针，**可能读到指针却看不到对应的 key 写入**（CPU/编译器重排），从而读到未初始化或旧数据。`load_consume` 的 acquire 语义正是堵住这个漏洞。

**预期结果**：理解 `load_consume` 是读路径「安全解引用」的必要条件，不可降级为 `Relaxed`。

**待本地验证**：上述推理可在 Miri/TSan 下通过人为改弱 `load_consume` 复现数据竞争（属破坏性实验，仅作理解）。

#### 4.3.5 小练习与答案

**练习 1**：`load_consume` 与 `epoch::unprotected()` 加载有什么本质区别？
**答案**：`load_consume`（带 `&Guard`）既提供 acquire 同步，又把当前线程 pin 在某个 epoch，保证加载到的指针在 Guard 存活期内不被回收——**可以安全解引用**。`unprotected()` 不 pin、不进 epoch，只适合「不解引用、只看 tag/判空」或「独占访问」两种场景（详见 4.6）。

---

### 4.4 引用计数的 Release + Acquire fence：安全回收的核心

#### 4.4.1 概念说明

`refs_and_height` 的高位是节点的引用计数（见 u2-l5/u2-l6）。每当一个 `Entry`/`RefEntry` 释放、或某层塔链被摘除，就要 `decrement`。这是经典的「原子引用计数 + 延迟析构」模式，其内存序有一个**铁律**：

> 减计数用 `Release`；当读到「旧值 == 1」（即本次减完正好归零）时，再插一道 `Acquire` fence，然后才能析构。

为什么？因为「归零者」必须看见**此前所有持有者**对该节点数据的全部写入，才能安全地 `drop_in_place(key)` / `drop_in_place(value)`。`Release` 的 fetch_sub 让此前所有写「发布」出去；`Acquire` fence 让归零者「获取」所有这些写。两者合起来，保证「最后一个减计数者看到完整状态」。

#### 4.4.2 核心流程

```text
decrement():
  old = refs.fetch_sub(1, Release)        # 发布：我对此节点的全部使用已对他人可见
  if (old >> HEIGHT_BITS) == 1:           # 我就是最后一个持有者
      fence(Acquire)                      # 获取：看到此前所有人的写
      guard.defer_unchecked(|| finalize)  # 排到 epoch 队列，未来某 epoch 安全析构
```

为什么归零判定看的是 `old >> HEIGHT_BITS == 1`？因为低位 5 bit 是高度，高位才是计数；`fetch_sub` 减的是 `1 << HEIGHT_BITS`（只动高位），返回的旧值右移 5 位后等于 1，意味着「减之前计数恰好是 1」，也就是「我这一下让它归零」。

#### 4.4.3 源码精读

`NodeRef::decrement` 是这个模式的范本：

[base.rs:293-304](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L293-L304) `fetch_sub(1 << HEIGHT_BITS, Ordering::Release)`（L296）+ 归零时 `fence(Ordering::Acquire)`（L300）+ `defer_unchecked(finalize)`（L301）。

`decrement_with_pin` 是同样的模式，只是「按需 pin」——仅当真的归零、需要往 epoch 队列里排时才临时 pin 一个 Guard：

[base.rs:308-324](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L308-L324) 同样是 `fetch_sub(Release)`（L315）+ `fence(Acquire)`（L319）。

对比：**增计数** `try_increment` 全程用 `Relaxed`：

[base.rs:222-249](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L222-L249) `load(Relaxed)`（L223）+ CAS `compare_exchange_weak(_, _, Relaxed, Relaxed)`（L239-L244）。

为什么增计数可以 `Relaxed` 而减计数必须 `Release`+`Acquire`？因为**析构只发生在减计数归零那一路**。增计数只是「我也持有了」的声明，并不需要发布新数据（节点内容早已写好）；真正需要「看到全部历史写」的只有最后那位析构者，而析构只会被某个 `decrement` 触发。所以同步责任完全落在 `decrement` 一侧。这是一个非常典型的**非对称引用计数**设计。

#### 4.4.4 代码实践

**实践目标**：验证 Release+Acquire fence 的「归零者看见全部写」语义。

**操作步骤**：

1. 阅读 [src/lib.rs:102-125](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L102-L125) 的「Garbage collection」章节，理解 `Entry` 句柄持有引用会推迟回收。
2. 设想：线程 A 通过 `get` 拿到 `Entry`（`try_increment` 成功，`Relaxed`），读取 `value`；线程 B 调用 `remove` 摘除节点并 `decrement`；A 释放 `Entry` 时再次 `decrement` 并归零。

**需要观察的现象（推理）**：A 的 `try_increment` 虽是 `Relaxed`，但 A 读 `value` 是在持有引用期间；B 的 `decrement(Release)` 发布了 B 对该节点的访问结束，A 的最终 `decrement` 归零时的 `fence(Acquire)` 配合 epoch，确保 `finalize` 之前所有线程的读写都已对归零线程可见。

**预期结果**：用一句话总结——「增计数 `Relaxed` 安全，是因为同步需求被推迟到唯一的析构点，由减计数的 `Release`+`Acquire` fence 集中承担」。

#### 4.4.5 小练习与答案

**练习 1**：为什么是 `fence(Acquire)` 而不是把 `fetch_sub` 直接换成 `AcqRel`？
**答案**：`fetch_sub` 只对「写」一侧有意义（它本质是 read-modify-write）。`AcqRel` 的 acquire 部分只对「读到某个值之后」才有用，而 `fetch_sub` 返回的是旧值；我们需要的 acquire 同步仅在「归零」分支才有意义。因此更省的做法是：写端统一用 `Release`，只在归零分支额外插一根独立的 `fence(Acquire)`。这样非归零路径（绝大多数 decrement）完全不必付 acquire 代价。

**练习 2**：如果删掉归零分支的 `fence(Acquire)`，会出什么问题？
**答案**：归零线程可能看不到此前其他持有者对该节点 `key/value` 的写（尤其在弱内存模型如 ARM/POWER 上），从而在 `finalize` 的 `drop_in_place` 里析构一个「看似已初始化、实则字段值不可见」的对象，造成未定义行为。这道 fence 是安全回收的最后一道闸。

---

### 4.5 关键 CAS 与 mark_tower 的 SeqCst：保守的线性化点

#### 4.5.1 概念说明

`SeqCst` 比 `Release`/`Acquire` 多一层保证：**所有 `SeqCst` 操作之间存在一个全局统一的总顺序**，每个线程对这个顺序达成一致。当算法的正确性依赖「跨多个不同原子位置的相对先后」时，就需要 `SeqCst`。

`base.rs` 把 `SeqCst` 集中用在了几个**线性化点（linearization point）**上：

- `mark_tower`：`fetch_or(1, SeqCst)` 给出边指针打删除标记，level0 的 0→1 翻转是「逻辑删除」的线性化点。
- `insert_internal` 的 level0 安装 CAS：插入的线性化点。
- 建塔阶段的若干 CAS、`remove` 的物理摘除 CAS：同样保守 `SeqCst`。

有意思的是，源码里几乎每一处 `SeqCst` 旁边都挂着一条 `TODO(Amanieu): can we use ... ordering here?`，说明作者**知道**这里很可能用更弱的序就够了，但暂时保守。

#### 4.5.2 核心流程

以删除为例（u3-l9 已讲算法，这里只看序）：

```text
mark_tower():                          # 自顶向下逐层标记
  for level in (0..height).rev():
      tag = get_level(level).fetch_or(1, SeqCst, unprotected()).tag()
      if level == 0 && tag == 1:       # level0 已被别人标记 → 我输了
          return false
  return true                          # 我成功标记 level0 → 我赢得删除权
```

`fetch_or` 本身是原子的，**「恰一个赢家」这个性质只依赖 fetch_or 的原子性，不依赖内存序**——输家必然读到 `tag==1`。那么 `SeqCst` 在这里保护的是什么？是「删除标记」与「插入/摘除 CAS」之间跨原子位置的**全局一致顺序**，让所有线程对「这个节点此刻到底在不在链表里、是否已死」达成统一视图。

#### 4.5.3 源码精读

`mark_tower` 的 `SeqCst` `fetch_or` 及其 TODO：

[base.rs:326-348](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L326-L348)。注意 [base.rs:333](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L333) 的 `TODO(Amanieu): can we use release ordering here?`——作者自问能否降到 Release。

`insert_internal` 的 level0 安装 CAS（插入的线性化点）：

[base.rs:1076-1085](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1076-L1085) `compare_exchange(_, _, SeqCst, SeqCst, guard)`，旁边 [base.rs:1075](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1075) 同样挂着 `TODO: can we use release ordering here?`。

建塔阶段的三处 `SeqCst`（载入后继、CAS 改自身出边、CAS 把新节点装进前驱）：

- [base.rs:1144](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1144) `n.get_level(level).load(SeqCst, guard)`（`TODO: can we use relaxed?` 在 L1143）。
- [base.rs:1183-1185](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1183-L1185) 自身出边 CAS（`TODO: release?` 在 L1182）。
- [base.rs:1197-1200](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1197-L1200) 装进前驱的 CAS（`TODO: release?` 在 L1196）。
- 建塔结束后 [base.rs:1227](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1227) 的最高层 tag 复查也是 `SeqCst`（`TODO: relaxed?` 在 L1226）。

`remove` 的物理摘除：先 `SeqCst` 载入后继 [base.rs:1306](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1306)（`TODO: relaxed?` 在 L1305），再 `SeqCst` CAS 摘除 [base.rs:1310-1318](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1310-L1318)（`TODO: release?` 在 L1309）。

**关键对比——`help_unlink` 用的是 `Release` 而非 `SeqCst`**：

[base.rs:768-774](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L768-L774) `pred.compare_exchange(_, _, Ordering::Release, Ordering::Relaxed, guard)`。这是全文件里少有的「作者主动放松到 Release」的 CAS。原因：`help_unlink` 只是**协作式物理清理**——逻辑删除的线性化点已经在 `mark_tower` 的 `SeqCst` 那里完成了，这里只需把「新拓扑」用 `Release` 发布给用 `load_consume`（acquire）读取的读者即可，不需要再参与全局总顺序。这一处对比非常能说明问题：**作者在确信「单点 Release 发布即可」的地方就降到 Release，只有在「跨位置全局一致」或「尚未论证清楚」的地方才保留 SeqCst。**

#### 4.5.4 代码实践

**实践目标**：归类所有 `SeqCst` 使用点，并分析 `mark_tower` 降到 Release/Acquire 的影响。

**操作步骤**：

1. 在 `src/base.rs` 全局搜 `SeqCst`，按下表归类（行号以本 HEAD 为准）：

   | 类别 | 位置 | 作用 |
   |---|---|---|
   | 逻辑删除标记 | `mark_tower` L335 | 删除的线性化点 |
   | 插入线性化点 | `insert_internal` level0 CAS L1081-1082 | 新节点挂入 |
   | 建塔载入 | L1144 | 读自身出边 tag |
   | 建塔 CAS（自身） | L1184 | 改自身出边 |
   | 建塔 CAS（前驱） | L1199 | 把新节点装进高层 |
   | 建塔后复查 | L1227 | 检查最高层是否被标 |
   | remove 摘除载入 | L1306 | 读后继 |
   | remove 摘除 CAS | L1315-1316 | 物理断链 |

2. 针对 `mark_tower` 的 `fetch_or(1, SeqCst, unprotected)`（[L335-L337](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L335-L337)）写一段分析：**若改成 `fetch_or(1, AcqRel/Release, unprotected)` 会产生哪些可观测差异？**

**分析要点（参考答案）**：

- 「恰一个赢家」**不会**被破坏。`fetch_or` 的原子性保证 level0 的 0→1 翻转只发生一次，输家必然读到 `tag==1`——这条性质与内存序无关，`Relaxed` 都成立。
- 真正受影响的是**跨位置一致性**。删除的正确性同时依赖「`mark_tower` 的标记」和「`insert_internal` 的安装 CAS / `remove` 的摘除 CAS / `help_unlink` 的清理」之间的相对顺序。`SeqCst` 给这些位置一个全局总顺序，使得「读者通过 A 位置看到节点已死」与「读者通过 B 位置看到节点仍在链中」不会矛盾地并存。降到 Release/Acquire 后，在弱内存模型（ARM、POWER）上，不同线程可能对「标记 vs 安装 vs 摘除」的先后观察不一致，理论上可能出现更微妙的交错。
- 但请注意：**真正的回收安全由 epoch + 引用计数兜底**（4.4），即使观测顺序混乱，也不会 use-after-free。因此降到 Release 在「内存安全」层面极可能仍然正确——这正是作者写 `TODO: can we use release ordering here?` 的潜台词。
- 作者目前保守用 `SeqCst` 的原因：把 `mark_tower` 与各 CAS 之间的全局顺序作为「正确性论证的脚手架」。一旦降级，必须重新做一遍完整的无锁正确性证明（线性化、ABA、跨位置不变式），工作量大且易错。这是典型的「**正确但不一定最优，优先保证可证明**」的工程取舍。

**预期结果**：你能清晰区分「`SeqCst` 保护的恰一个赢家性质（其实不需要）」与「`SeqCst` 保护的全局一致顺序（这才是它真正的用途）」。

#### 4.5.5 小练习与答案

**练习 1**：既然 `help_unlink` 的 CAS 能降到 `Release`，为什么 `remove` 的摘除 CAS（L1310-L1318）仍是 `SeqCst`？
**答案**：两者物理动作相同（把前驱指向后继），但上下文不同。`remove` 是「赢得 mark_tower 后的主动摘除」，它和 `mark_tower` 的 `SeqCst` 标记、`insert` 的 `SeqCst` 安装共同构成删除/插入的线性化论证链，作者把它们整体保持在 `SeqCst` 总顺序里以简化证明。`help_unlink` 是读路径上「顺手清理」的附带动作，逻辑删除早已在别处完成，无需参与该总顺序，故可放心降到 `Release`。

**练习 2**：`is_removed`（[base.rs:351-361](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L351-L361)）用 `Relaxed` 读 level0 tag，是否安全？
**答案**：安全。`is_removed` 是「尽力而为」的观测接口，文档说明它的返回只反映「观测时刻」的状态，删除安全性**不依赖**它。`Relaxed` 足够，因为它不需要和任何写建立 happens-before。

---

### 4.6 epoch::unprotected() 的两类安全用法

#### 4.6.1 概念说明

crossbeam-epoch 的 `Atomic::load(ordering, &Guard)` 第二个参数要求一个 `Guard`。`Guard` 的作用是 pin 住当前线程所在 epoch，保证「通过本 Guard 加载到的指针，在本 Guard 存活期内不会被回收」。

`epoch::unprotected()` 返回一个**不 pin** 的「假 Guard」。用它加载指针是 unsafe 的，因为加载到的指针可能正被并发回收。`base.rs` 里所有 `unprotected()` 都严格落在两类安全场景之一：

1. **只看 tag/判空，不解引用**：加载只是为了读指针的低位 tag 或判断是否为 null，从不解引用 pointee。pointee 是否已释放无所谓。
2. **独占访问**：整个跳表已被当前线程独占（`Drop`、`IntoIter`），不存在并发回收。

#### 4.6.2 核心流程

```text
判定一次 unprotected() 加载是否安全：
├─ 加载后是否解引用返回的指针？
│   ├─ 否（只看 .tag() / .is_null()）→ 场景 1，安全（前提：存放该原子字的位置本身存活）
│   └─ 是 → 必须满足场景 2（独占访问），否则不安全
```

#### 4.6.3 源码精读

**场景 1：只看 tag，不解引用**

`mark_tower`：注释直说「我们只是为了 tag 而加载指针，所以用 unprotected 没问题」：

[base.rs:330-337](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L330-L337) `fetch_or(1, SeqCst, epoch::unprotected())`。这里被标记的是节点**自身出边**指针（存在节点自己的 tower 里），调用方（`remove`/`Entry::remove`）已通过 `try_acquire` 持有引用计数，节点本身不会在此期间被释放；而我们只取 `.tag()`，从不解引用后继节点。

`is_removed`：同样的理由：

[base.rs:353-359](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L353-L359) `load(Relaxed, epoch::unprotected())` 后只取 `.tag()`。

`random_height` 判空：

[base.rs:726-734](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L726-L734) 对 `self.head.get_level(height-2)` 用 `unprotected()` 加载，只判 `is_null()`。`head` 是 `SkipList` 结构体本身的字段，只要 `SkipList` 存活它就存活，且只读 tag/null。

**场景 2：独占访问**

`Drop for SkipList`：注释明说「unprotected 加载是安全的，因为此时只有本线程在使用这个跳表」：

[base.rs:1413-1437](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1413-L1437)（L1419、L1427 两处 `unprotected`）。这里**会**解引用并 `finalize`，但因为 `&mut self` 保证独占，不可能有并发回收。

`IntoIter` 的 `into_iter`、`Drop`、`next` 同理：

- [base.rs:1459-1463](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1459-L1463) `into_iter` 取首节点；
- [base.rs:2266-2279](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L2266-L2279) `Drop for IntoIter` 遍历销毁；
- [base.rs:2304-2308](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L2304-L2308) `next` 推进游标。

三处注释都是同一句「Unprotected loads are okay because this function is the only one currently using the skip list」。

> **回到 4.2 的对比**：同样是「跳过空层判 null」，搜索函数里用 `load(Relaxed, guard)`（带真 Guard），而 `random_height` 里用 `load(Relaxed, unprotected())`。差别在于搜索发生在多线程并发期，必须借 Guard 防 pointee 被回收（即便只判 null，原子字本身在 head 里没问题，但跨节点访问时 Guard 是更稳妥的统一约定）；`random_height` 只访问永驻的 `head`，故可用 unprotected。

#### 4.6.4 代码实践

**实践目标**：能独立判定任意一处 `unprotected()` 是否安全。

**操作步骤**：

1. 全局搜 `unprotected()`，对每一处回答两个问题：(a) 加载后是否解引用？(b) 是否独占访问？
2. 用一张三列表格记录：位置 | 是否解引用 | 安全依据（场景 1 或 2）。

**预期结果**：所有 `unprotected()` 调用都能归入「不解引用（场景 1）」或「独占访问（场景 2）」，没有第三种。这是本库 unsafe 代码纪律性的体现，也是它能通过 Miri 验证的前提（见 u5-l19）。

**待本地验证**：可用 `cargo +nightly miri test` 跑 `tests/base.rs` 的单线程用例，确认这些 `unprotected` 不触发未定义行为。

#### 4.6.5 小练习与答案

**练习 1**：为什么 `mark_tower` 用 `unprotected()` 而不是传 `remove` 调用者已有的 `guard`？
**答案**：`mark_tower` 改的是节点**自身**出边指针，且只取 `.tag()`。它不需要 epoch 保护 pointee（不解引用后继），用 `unprotected()` 可以省去一次 pin，在删除热路径上更轻。安全性由「调用方持有引用计数 → 节点自身存活」+「只看 tag」共同保证。

**练习 2**：如果某天有人在 `front()` 这种并发读路径里误用 `unprotected()` 加载并解引用，会怎样？
**答案**：可能读到正被另一线程 `finalize` 的节点，造成 use-after-free（未定义行为）。这正是读路径必须用 `load_consume(guard)` 的原因。Miri/TSan 通常能捕获这类错误。

---

## 5. 综合实践

把本讲的四类内存序串起来，做一次「`SkipList::remove` 全链路内存序审计」。

**任务**：针对一次完整的 `remove(key)` 调用（[base.rs:1270-1337](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1270-L1337)），按执行顺序列出它触及的每一处原子操作、所用 `Ordering`、归属类别（4.2/4.4/4.5）、以及「为什么是这个强度」，并指出哪些点挂着「可放宽」的 TODO。

**参考步骤**：

1. `search_position` 定位 key：沿途 `load_consume(guard)`（4.3，acquire 等价）；`max_height` 用 `Relaxed`（4.2）；遇标记节点调 `help_unlink`，其 CAS 用 `Release`（4.5 对比）。
2. `RefEntry::try_acquire` → `try_increment`：`Relaxed` CAS（4.4 增计数）。
3. `mark_tower`：`fetch_or(1, SeqCst, unprotected())`（4.5 删除线性化点，挂 TODO）。
4. `len.fetch_sub(1, Relaxed)`（4.2 近似值）。
5. 逐层 `load(SeqCst, guard)` 取后继 + `compare_exchange(SeqCst, SeqCst)` 摘除（4.5，均挂 TODO）。
6. 摘除成功后 `n.decrement(guard)`：`fetch_sub(Release)` + 归零时 `fence(Acquire)` + `defer_unchecked(finalize)`（4.4 安全回收）。

**产出**：一张表 + 一段总结，说明「整条 remove 链路上，真正承担正确性同步的只有 `mark_tower` 的 SeqCst、`help_unlink` 的 Release CAS、以及 `decrement` 的 Release+Acquire fence 三类；其余 `Relaxed` 都是近似值或非关键的本地计数」。

**待本地验证**：把你画的表与源码逐行核对；如能运行，用 `cargo +nightly miri test tests/base.rs` 验证 unsafe 路径无 UB。

## 6. 本讲小结

- `base.rs` 的内存序呈「强度梯度」：绝大多数 `Relaxed`，少数 `Release`/`Acquire`，极少数 `SeqCst`——强度与「这一下操作保护的不变式重要性」严格对应。
- `hot_data`（`len`/`seed`/`max_height`）全是 `Relaxed`，因为它们是近似值/提示，正确性由指针拓扑而非这些计数器决定。
- 读路径用 `load_consume(&Guard)`（acquire 等价）安全解引用指针；这是 acquire 语义在 `base.rs` 里的主要载体。
- 引用计数走**非对称**模式：增计数 `Relaxed`，减计数 `fetch_sub(Release)` + 归零时 `fence(Acquire)`——同步责任集中在唯一的析构点。
- `mark_tower` 与关键 CAS 用 `SeqCst` 是保守选择，保护的是「跨原子位置的全局一致顺序」，而非「恰一个赢家」（后者由原子性保证）；`help_unlink` 降到 `Release` 是作者「确信可放宽」的对照样本。
- `epoch::unprotected()` 仅在「只看 tag/判空」或「独占访问（Drop/IntoIter）」两类场景下安全。

## 7. 下一步学习建议

- 继续阅读 **u5-l18（并发语义与 Drop/IntoIter 的安全性）**：把本讲的 `unprotected()` 独占访问与「单操作原子、多操作非原子」的并发语义结合起来理解。
- 结合 **u5-l19（测试体系与基准实践）**：用 Miri 实际验证本讲分析的 unsafe 路径，并关注 `tests/base.rs` 里对引用计数与释放时机的断言。
- 进阶：对照阅读 crossbeam-epoch 的 `Atomic::load_consume` 与 `Guard::defer_unchecked` 实现，理解「epoch pin + 引用计数」两道闸门如何在底层协作，从而把本讲的 `Release`+`Acquire` fence 落到具体的回收协议上。
