# no_std 与 alloc 支持

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `crossbeam-skiplist` 的「`core` → `alloc` → `std`」三档分层是如何用 Cargo feature 与 `cfg` 门控实现的。
- 解释为什么底层 `base::SkipList` 可以在 `no_std + alloc` 环境下使用，而高层 `SkipMap`/`SkipSet` 必须依赖 `std`。
- 读懂 `alloc_helper.rs` 里手写的 `Global` 分配器，并说明它为何不直接用标准库（尚未稳定的）`alloc::alloc::Global`。
- 自己动手用 `--no-default-features --features alloc` 编译本 crate，观察高层封装消失、底层原语仍在的现象。

本讲是专家层（u5）的第一篇，承接 u1-l3（目录与 feature 分层）和 u2-l5（Node 内存布局）。它不再讲并发算法，而是回答一个工程问题：**这套无锁跳表，到底能在多「裸」的环境里跑起来？**

## 2. 前置知识

阅读本讲前，建议你已经了解：

- **`#![no_std]` 是什么**：Rust crate 默认链接 `std`；写上 `#![no_std]` 后，crate 只能依赖 `core`（与 OS 无关的基础类型，如 `Option`、`slice`、原子类型），不能直接用 `std::collections`、`std::thread`、`Vec` 等。需要堆分配时再额外引入 `alloc` crate（提供 `Box`、`Vec`、`Layout`、`alloc::alloc` 等）。
- **Cargo feature 是什么**：feature 是编译期的「开关」，通过 `#[cfg(feature = "xxx")]` 让某些代码仅在开启时编译。feature 之间可以互相隐含（如 `std` 隐含 `alloc`）。
- **`crossbeam-epoch` 的 `Collector` 与 `Guard`**（见 u2-l6）：epoch-based 内存回收需要一个 `Collector`，临界区里要有 `Guard`。这一点决定了高层封装为何离不开 `std`。
- **`Layout` 与手动分配**（见 u2-l5）：跳表节点是变长的（塔高不同），通过 `Layout::extend` 拼出布局后，用 `alloc::alloc::alloc(layout)` 申请、`dealloc(ptr, layout)` 释放。

一个关键直觉：**「能在 `no_std` 下用」不等于「什么都不依赖」**。无锁数据结构通常仍需要堆分配（`alloc`）和原子操作（`target_has_atomic`）。本 crate 的设计就是把「必须依赖什么」精确地切成三档，让嵌入式 / 内核 / WASM 等场景能只挑自己能提供的那一档。

## 3. 本讲源码地图

本讲只涉及三个文件，都很短：

| 文件 | 作用 |
| --- | --- |
| `src/lib.rs` | crate 根，声明 `#![no_std]`，用 `extern crate` 与 `#[cfg]` 把模块按三档门控 |
| `src/alloc_helper.rs` | 手写的 `Global` 分配器，封装 `alloc::alloc` 的 `alloc`/`dealloc`，替代尚未稳定的 `alloc::alloc::Global` |
| `Cargo.toml` | 声明 `default = ["std"]`、`std`、`alloc` 三个 feature 及其级联关系，并设置 MSRV=1.74 |

此外会少量引用 `src/base.rs`（看 `Global` 的真实使用点）和 `src/map.rs`（看高层为何需要 `std`）。

## 4. 核心概念与源码讲解

### 4.1 三档门控：`core` → `alloc` → `std`

#### 4.1.1 概念说明

`crossbeam-skiplist` 的能力被切成三档，从弱到强：

1. **`core` 档（最弱）**：不开启任何 feature。此时只有 `comparator` 和 `equivalent` 两个「纯比较 trait」模块可用——它们只用 `core::cmp::Ordering` / `core::borrow::Borrow`，不需要堆，也不需要原子回收。
2. **`alloc` 档**：开启 `alloc` feature。引入 `extern crate alloc`，于是有了堆分配能力，底层无锁原语 `base::SkipList` 和手写分配器 `alloc_helper` 解锁。
3. **`std` 档（默认、最强）**：开启 `std` feature（隐含 `alloc`）。引入 `extern crate std`，高层易用封装 `SkipMap`/`SkipSet` 解锁。

设计动机：跳表的**算法内核**（`base`）只依赖「堆 + 原子 + 一个外部传入的 `Collector`」，完全可以在 `no_std + alloc` 的内核/嵌入式环境运行；而**人体工学封装**（`map`/`set`）依赖一个进程级全局默认 `Collector`，那个全局量靠 `thread_local` 实现，只能存在于 `std`。把两者用 feature 切开，就能让裸环境复用算法内核，又不牺牲高层 API 的易用性。

注意一个工程现实：`Cargo.toml` 里明确写了「同时关闭 `std` 和 `alloc` 暂不支持」（见下方源码）。也就是说，`core` 档虽然能编译（`comparator`/`equivalent` 总在），但拿不到任何数据结构，所以**实际可用的只有 `alloc` 档和 `std` 档两档**。

#### 4.1.2 核心流程

门控的级联关系如下：

```
开启 std   ──隐含──▶ 开启 alloc ──外加──▶ crossbeam-epoch/std, crossbeam-utils/std
开启 alloc ──外加──▶ crossbeam-epoch/alloc
（两者都关）──▶ 仅 core：只剩 comparator/equivalent，无任何数据结构（官方标注"暂不支持"）
```

而每一个「需要堆与原子」的模块，都被同一道复合门把守：

```
#[cfg(all(feature = "alloc", target_has_atomic = "ptr"))]
```

这里有两个条件：

- `feature = "alloc"`：用户开了 `alloc`（或 `std`）。
- `target_has_atomic = "ptr"`：目标平台支持「指针宽度的原子操作」。这是 Rust 内置的 `cfg`（不是 Cargo feature），由编译器根据目标三元组自动设置。`crossbeam-epoch` 的无锁回收必须建立在原子指针之上，没有它整套机制就无从谈起，所以连底层 `base` 也要它来把门。

`extern crate` 同样按档引入：`alloc` 在「`alloc` 档」引入，`std` 在「`std` 档」引入。

#### 4.1.3 源码精读

先看 crate 顶部的 `#![no_std]`：

[文件路径:src/lib.rs:231](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L231)

这一行告诉编译器：本 crate 默认不链接 `std`，只能用 `core`。这是整个 `no_std` 设计的「总开关」。

紧接着是按档引入标准库的 `extern crate`：

[文件路径:src/lib.rs:244-247](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L244-L247)

中文说明：

- `extern crate alloc;` 只在「`alloc` feature 开启 **且** 目标支持原子指针」时编译——它把堆分配 crate 引入作用域。
- `extern crate std;` 只在「`std` feature 开启」时编译——它把完整的标准库引入作用域（`std` 已隐含 `alloc`，所以这里不再重复 `target_has_atomic` 条件）。

再看模块声明与门控：

[文件路径:src/lib.rs:249-269](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L249-L269)

中文逐行说明：

- `mod alloc_helper;`——手写分配器，挂在 `alloc + target_has_atomic` 门后（私有模块）。
- `pub mod base;` 与 `pub use crate::base::SkipList;`——底层无锁跳表原语，同样挂在 `alloc + target_has_atomic` 门后。
- `pub mod map;` / `pub mod set;` 与 `pub use crate::{map::SkipMap, set::SkipSet};`——高层封装，挂在 `std` 门后。
- `pub mod comparator;` / `pub mod equivalent;`——纯比较 trait，**没有任何门控**，永远可见（这是唯一的 `core` 档内容）。

这套 `cfg` 的级联关系定义在 `Cargo.toml`：

[文件路径:Cargo.toml:27-38](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/Cargo.toml#L27-L38)

中文说明：

- `default = ["std"]`：默认就是最强档。
- `std = ["alloc", "crossbeam-epoch/std", "crossbeam-utils/std"]`：开 `std` 会自动开 `alloc`，并把 `std` 透传给两个 crossbeam 依赖。
- `alloc = ["crossbeam-epoch/alloc"]`：开 `alloc` 只把 `alloc` 透传给 `crossbeam-epoch`。
- 第 37 行的注释写得明明白白：「同时关闭 `std` 和 `alloc` 暂不支持」——印证了「实际只有两档可用」。

为了让 `no_std` 友好，依赖声明关掉了各自的默认 feature：

[文件路径:Cargo.toml:40-42](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/Cargo.toml#L40-L42)

`default-features = false` 保证 `crossbeam-epoch`/`crossbeam-utils` 默认不会偷偷把 `std` 拉进来；只有当本 crate 的 `std`/`alloc` feature 主动透传时，它们才会带上对应能力。另外 `categories` 里含 `"no-std"`（[Cargo.toml:16](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/Cargo.toml#L16)），MSRV 为 1.74（[Cargo.toml:10](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/Cargo.toml#L10)）。

#### 4.1.4 代码实践

**实践目标**：用编译器替我们「画」出当前 feature 配置下的模块可见性树。

**操作步骤**：

1. 生成默认档（`std`）的文档清单：

   ```bash
   cargo doc -p crossbeam-skiplist --no-deps --features std
   ```

2. 生成 `alloc` 档的文档清单：

   ```bash
   cargo doc -p crossbeam-skiplist --no-deps --no-default-features --features alloc
   ```

3. 在浏览器里分别打开两份 `target/doc/crossbeam_skiplist/index.html`，对比左侧的模块/类型列表。

**需要观察的现象**：

- 默认档应能看到 `base`、`map`、`set`、`comparator`、`equivalent` 全部模块，以及 `SkipList`、`SkipMap`、`SkipSet` 三个类型。
- `alloc` 档应只能看到 `base`、`comparator`、`equivalent`，以及 `SkipList`；`SkipMap`/`SkipSet` 应当**消失**（因为它们挂在 `#[cfg(feature = "std")]` 门后）。

**预期结果**：可见性差异与 4.1.3 的 `cfg` 分析完全一致。具体命令输出待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `default = ["std"]` 改成 `default = []`，下游用户 `cargo build` 时默认能用到哪些类型？

**参考答案**：默认什么数据结构都拿不到。因为没有任何 feature 被开启，`base`/`map`/`set`/`alloc_helper` 全部不编译，只剩下 `comparator` 和 `equivalent` 两个比较 trait 模块。下游必须显式 `features = ["alloc"]` 或 `["std"]` 才能用上跳表。

**练习 2**：`target_has_atomic = "ptr"` 这个条件，在哪种目标平台上会不满足？不满足时会怎样？

**参考答案**：在一些不支持「指针宽度原子操作」的冷门或嵌入式目标上不满足（绝大多数主流目标如 x86、ARM、RISC-V、WASM 都满足）。不满足时，即便开了 `alloc`，`base` 和 `alloc_helper` 也不会编译，因为 `crossbeam-epoch` 的无锁回收本就建立在原子指针之上——这是硬性物理前提。

---

### 4.2 为何 `base` 只需 `alloc`，而 `map`/`set` 需要 `std`

#### 4.2.1 概念说明

第二档分界线回答一个关键问题：**都是无锁跳表，为什么底层 `base` 能在 `no_std + alloc` 跑，高层 `map`/`set` 却非要 `std`？**

答案只有一个词：**全局默认 `Collector`**。

回顾 u2-l6：epoch 回收需要一个 `Collector` 来推进 epoch、管理垃圾。底层 `base::SkipList` 把 `Collector` 当作**构造参数**由外部传入——它自己不关心这个 `Collector` 从哪来，因此不依赖任何全局状态，可以在 `alloc` 档运行。

而高层 `SkipMap` 为了「让用户完全看不见 `Collector` 与 `Guard`」，在构造时直接调 `epoch::default_collector()` 拿一个进程级全局 `Collector`。这个全局 `Collector` 在 `crossbeam-epoch` 里是用 `thread_local` 实现的，而 `thread_local` 属于 `std`——这就是高层封装离不开 `std` 的根本原因。

#### 4.2.2 核心流程

两层的依赖差异可以这样对比：

| 维度 | `base::SkipList`（`alloc` 档可用） | `SkipMap`/`SkipSet`（仅 `std` 档） |
| --- | --- | --- |
| `Collector` 来源 | 用户在 `new`/`with_comparator` 时显式传入 | 内部调 `epoch::default_collector()` 取全局量 |
| 是否依赖 `thread_local` | 否 | 是（全局 Collector 建立 thread_local 之上） |
| 是否依赖 `std` | 否（只需 `alloc`） | 是 |
| 用户是否需要管 `Guard` | 是（每个方法要传 `&Guard`） | 否（方法内自动 `epoch::pin()`） |

一句话：**底层把 `Collector` 当参数，所以裸；高层把 `Collector` 当全局单例，所以方便但要 `std`。**

#### 4.2.3 源码精读

先看 `base.rs` 的 import，确认它**完全不碰 `std`**：

[文件路径:src/base.rs:3-21](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L3-L21)

中文说明：

- 第 3 行只从 `alloc` 取了 `handle_alloc_error`（分配失败时的终止处理），这是 `alloc` crate 的稳定 API。
- 第 4-13 行全部来自 `core`：`Layout`、`cmp`、`ptr`、原子类型等。
- 第 15-16 行来自 `crossbeam-epoch` 与 `crossbeam-utils`——而这两个依赖在 `alloc` 档（不开 `std`）下同样可用。
- 没有任何 `std::` 导入。这正是 `base` 能跑在 `no_std + alloc` 的直接证据。

再看 `map.rs`，它的高层封装在构造时取了全局 `Collector`：

[文件路径:src/map.rs:44](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L44)

`epoch::default_collector()` 返回进程级全局 `Collector` 的句柄。`with_comparator` 同理（[src/map.rs:61](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L61)）。`crossbeam-epoch` 的 `default_collector`/`pin`（无参版）只在它自己的 `std` feature 下编译（本 crate 的 `std` feature 会把 `std` 透传过去，见 4.1.3 的 `Cargo.toml:32`）。所以 `map`/`set` 必须 `#[cfg(feature = "std")]`，与 lib.rs 的门控一致。

#### 4.2.4 代码实践

**实践目标**：亲手验证「关掉 `std` 后，`base` 仍可用、`SkipMap`/`SkipSet` 不可用」。

**操作步骤**：

1. 在仓库根目录用 `--no-default-features --features alloc` 编译本 crate：

   ```bash
   cargo build -p crossbeam-skiplist --no-default-features --features alloc
   ```

2. 临时新建一个 `examples/no_std_probe.rs`（注意：这是**示例代码**，仅用于探测，不属于项目原有文件，验证完应删除），内容尝试同时用 `base::SkipList` 和 `SkipMap`：

   ```rust
   // 示例代码：仅用于探测 feature 门控
   use crossbeam_epoch as epoch;
   use crossbeam_skiplist::base::SkipList;

   fn main() {
       let guard = unsafe { epoch::unprotected() };
       let list: SkipList<u64, u64> = SkipList::new(epoch::default_collector().clone());
       list.insert("test".to_string(), 0u64, &guard);
       println!("len = {}", list.len());
   }
   ```

   > 说明：上面这个探测例为了简洁直接用了 `epoch::default_collector()` 和 `epoch::unprotected()`，它们本身在 `crossbeam-epoch` 的 `std`/`alloc` 下行为不同；本例只是为了触发「`base` 可编译」这一现象。一个更严格的 `no_std + alloc` 用法应当由调用方自行持有 `Collector`（见综合实践）。

3. 用 alloc 档运行它：

   ```bash
   cargo run -p crossbeam-skiplist --no-default-features --features alloc --example no_std_probe
   ```

4. 再把探测例改成 `use crossbeam_skiplist::SkipMap;`，重复第 3 步。

**需要观察的现象**：

- 步骤 3：能编译并运行（`base::SkipList` 在 `alloc` 档可见）。
- 步骤 4：编译失败，报类似 `cannot find type SkipMap in crate root`——因为 `SkipMap` 的 re-export 挂在 `#[cfg(feature = "std")]` 门后，`alloc` 档下根本不存在。

**预期结果**：与门控分析一致。具体编译输出待本地验证。验证完请删除探测例，**不要修改源码或长期留下示例文件**。

#### 4.2.5 小练习与答案

**练习 1**：假设 `crossbeam-epoch` 未来把 `default_collector` 改成不依赖 `thread_local` 的实现（例如用 `Once`+静态存储），`map`/`set` 是否就能降到 `alloc` 档？

**参考答案**：理论上是的。`map`/`set` 对 `std` 的唯一硬依赖就是这个全局默认 `Collector`（以及无参 `epoch::pin()`）。若 `crossbeam-epoch` 能在 `alloc` 档提供等价的全局 `Collector`，本 crate 就可以把 `map`/`set` 的门控放宽到 `alloc + target_has_atomic`。这正是 feature 分层的价值——把「能不能更裸」的决定权留给底层依赖。

**练习 2**：为什么 `base::SkipList` 的方法签名里到处都是 `&Guard`，而 `SkipMap` 的方法里看不到 `Guard`？

**参考答案**：`base` 把「何时进入临界区」交给调用方，所以每个操作需要调用方传入 `&Guard`，调用方自己决定 `Guard` 的生命周期（这在 `no_std` 下是必要的，因为没有全局 pin）。`SkipMap` 在每个公共方法首行 `let guard = &epoch::pin();` 自动 pin 一个临时 `Guard`，方法返回时丢弃——用一次 pin 的开销换取「用户无需关心 Guard」的易用性（详见 u4-l14）。

---

### 4.3 `alloc_helper::Global`：手写的分配器

#### 4.3.1 概念说明

`base.rs` 里 `Node` 是变长的（塔高不同），需要按 `Layout` 手动申请/释放内存（见 u2-l5）。Rust 标准库 `alloc` crate 提供了底层函数 `alloc::alloc::alloc(layout)` / `dealloc(ptr, layout)`，但**没有**提供一个稳定的、对象化的「全局分配器」类型。

`alloc::alloc::Global` 这个类型确实存在，但它是 **nightly 才稳定**的——它实现了 `Allocator` trait，而 `Allocator` trait 至今未稳定。本 crate 的 MSRV 是 1.74（stable），不能用 nightly API。

于是作者在 `alloc_helper.rs` 里手写了一个极简的 `Global`：一个零大小结构体，把 `alloc::alloc::alloc`/`dealloc` 包成 `allocate`/`deallocate` 方法，并妥善处理「零大小类型（ZST）」这一边界情况。

#### 4.3.2 核心流程

`Global` 的分配/释放流程：

```
allocate(layout)
  └─▶ alloc_impl(layout, zeroed=false)
        ├─ layout.size() == 0 ?  → 返回一个「悬空但非空、对齐正确」的指针（dangling）
        └─ 否则                  → alloc::alloc::alloc(layout) 或 alloc_zeroed(layout)
                                   返回 NonNull<u8>（失败为 None）

deallocate(ptr, layout)
  ├─ layout.size() == 0 ?  → 什么都不做（ZST 没有真正分配过）
  └─ 否则                  → alloc::alloc::dealloc(ptr, layout)
```

为什么要单独处理 `size == 0`？因为 `alloc()` 对零大小布局的行为是「可能返回空指针或悬空指针」，不可移植；而本 crate 里 `Node` 的 `Layout::new::<Self>()` 头部不会是零大小，但作者仍把 ZST 情况保守地处理成「返回对齐正确的悬空指针」，与标准库 `Allocator` 对 ZST 的契约保持一致（ZST 分配不占堆，deallocate 时也不真正释放）。

#### 4.3.3 源码精读

文件开头只有两行 import，全部来自 `core`：

[文件路径:src/alloc_helper.rs:1-7](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/alloc_helper.rs#L1-L7)

中文说明：第 1 行只用了 `core` 的 `Layout` 和 `NonNull`——没有任何 `std`。第 3-6 行的注释写明了它的来历：「基于尚未稳定的 `alloc::alloc::Global`」，并指出一个关键差异：**标准库的 `Global` 返回 `NonNull<[u8]>`（带长度），而本实现返回 `NonNull<u8>`（不带长度）**——因为本 crate 自己掌握 `Layout`，不需要分配器回传长度。

核心方法是 `alloc_impl`：

[文件路径:src/alloc_helper.rs:12-34](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/alloc_helper.rs#L12-L34)

中文逐段说明：

- 入参 `zeroed: bool` 决定调用 `alloc` 还是 `alloc_zeroed`——用一个布尔复用同一段逻辑。
- `size == 0` 分支调用内部函数 `dangling(layout)` 返回一个「对齐正确、无 provenance」的悬空指针，模拟 ZST 分配。
- `size != 0` 分支是 `unsafe` 块，调用 `alloc::alloc::alloc(layout)` 或 `alloc::alloc::alloc_zeroed(layout)`，再用 `NonNull::new` 把裸指针包成 `Option<NonNull<u8>>`（分配失败返回 `None`）。
- 第 25 行的 `#[allow(clippy::disallowed_methods)]` 是因为本项目 lint 禁止直接用 `alloc::alloc::alloc`，但分配器助手本身正是「唯一允许直接调用」的地方，所以显式放行。

`dangling` 内部用到一个 `without_provenance_mut` 辅助函数（构造一个「只有地址、没有 provenance」的指针）：

[文件路径:src/alloc_helper.rs:16-19](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/alloc_helper.rs#L16-L19)

之所以不直接用标准库的 `Layout::dangling` 或 `ptr::without_provenance_mut`，是因为它们都还没稳定（注释 `// Layout::dangling is unstable`）。所以作者自己实现了一个等价物：

[文件路径:src/alloc_helper.rs:68-85](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/alloc_helper.rs#L68-L85)

中文说明：

- 在 Miri 下用 `core::mem::transmute(addr)`（注释解释：int→pointer 的 transmute 当前恰好产生「无 provenance 指针」的语义，但这是 sysroot crate 的特权，不是稳定的 transmute 保证）。
- 非 Miri 下用 `addr as *mut T`（并特别说明这种写法兼容 CHERI 等带 provenance 标签的架构，而 transmute 在 CHERI 上会出错）。
- 这套 `#[cfg(miri)]` 分叉，是为了让 Miri 能更精确地追踪 provenance、暴露潜在 UB，同时不影响真实构建。

对外暴露的两个分配入口与一个释放入口：

[文件路径:src/alloc_helper.rs:38-46](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/alloc_helper.rs#L38-L46)

`allocate` 走 `zeroed=false`，`allocate_zeroed` 走 `zeroed=true`（注意它带 `#[allow(dead_code)]`——目前 `base.rs` 没有用到清零分配，预留作未来用途）。

[文件路径:src/alloc_helper.rs:50-65](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/alloc_helper.rs#L50-L65)

`deallocate` 同样先判 `size != 0`，只有非零大小才真正 `dealloc`；安全注释详细说明了调用方必须保证 `layout` 与当初分配时一致。

最后看 `Global` 在 `base.rs` 里的真实使用点：

[文件路径:src/base.rs:19](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L19)

`Node::alloc` 申请内存：

[文件路径:src/base.rs:169-184](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L169-L184)

中文说明：第 172 行 `Global.allocate(layout)` 拿到节点内存；返回 `None` 时第 174 行用 `handle_alloc_error(layout)` 终止程序（这是 `alloc` crate 的标准失败处理，不会 panic 而是直接 abort）。`Node::dealloc` 释放内存：

[文件路径:src/base.rs:189-195](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L189-L195)

第 193 行 `Global.deallocate(...)` 把节点内存还给堆。整个 `Node` 的生杀大权，就握在这两个对 `Global` 的调用上。

#### 4.3.4 代码实践

**实践目标**：通读 `alloc_helper.rs` 后，用自己的话回答「为什么本项目要手写 `Global`」，并定位每一个「不得不自己写」的具体原因。

**操作步骤**：

1. 打开 `src/alloc_helper.rs`，逐行阅读（全文仅 86 行）。
2. 在文件里搜索全部「unstable」相关注释，把每一条「为什么不直接用标准库」的理由列成清单。
3. 对照下方的「参考答案」核对。

**需要观察的现象**：你应该至少能找出三条「标准库对应物未稳定」的理由。

**参考答案（手写 `Global` 的三条理由）**：

1. **`alloc::alloc::Global` 本身未稳定**：它实现了 `Allocator` trait，而 `Allocator` 是 nightly-only。本 crate MSRV=1.74（stable），不能用。
2. **`Layout::dangling` / `ptr::without_provenance_mut` 未稳定**：处理 ZST 分配需要的「对齐正确的悬空指针」没有现成稳定 API，只能自己实现 `without_provenance_mut`。
3. **返回类型更贴合需求**：标准库 `Global::allocate` 返回 `NonNull<[u8]>`（带长度），而本 crate 自己掌握 `Layout`，只需要 `NonNull<u8>`，自己实现可以省掉不必要的长度信息。

**预期结果**：你的清单应与上述三条对应。这是源码阅读型实践，无运行结果，无需「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`alloc_impl` 为什么要把 `alloc::alloc::alloc` 放在 `unsafe` 块里，而 `dangling` 分支不需要？

**参考答案**：`alloc::alloc::alloc(layout)` 是 `unsafe` 的——它返回一个未初始化的裸指针，调用方必须保证按 `layout` 正确使用与释放，否则会触发 UB。而 `dangling` 分支只是构造一个不指向任何真实堆内存的「悬空但非空」指针，并且不会解引用它，`NonNull::new_unchecked` 在传入「对齐值非零」时是安全的（对齐保证非零，故非空），所以这一分支本身不引入新的 `unsafe` 义务（其内部那个 `unsafe` 已由 SAFETY 注释论证）。

**练习 2**：如果 `size == 0` 时直接调用 `alloc::alloc::alloc(Layout::new::<()>())`，会发生什么？

**参考答案**：标准库对零大小 `Layout` 的 `alloc` 行为是「实现定义」的——可能返回空指针，也可能返回某个悬空指针，跨平台不一致。本实现显式短路 `size == 0`、返回对齐正确的 `dangling` 指针，是为了与 `Allocator` trait 对 ZST 的契约保持一致（ZST 分配不占堆、不真正释放），避免依赖未定义/不可移植行为。

## 5. 综合实践

把本讲三块知识串起来：在 `no_std + alloc` 环境下**亲手驱动一次底层 `base::SkipList`**，体会「为什么 base 能裸跑」。

**任务**：写一个独立的 `no_std + alloc` 库 crate（不是在本项目里改，而是新建一个外部 crate 来依赖本项目），用 `base::SkipList` 完成一次「插入 → 查询 → 删除」全流程，全程显式管理 `Collector` 与 `Guard`。

**建议步骤**：

1. 新建一个外部 crate（如 `skiplist-nostd-probe`），在它的 `Cargo.toml` 里：

   ```toml
   [dependencies]
   crossbeam-skiplist = { path = "../crossbeam-skiplist", default-features = false, features = ["alloc"] }
   crossbeam-epoch = { path = "../crossbeam-epoch", default-features = false, features = ["alloc"] }
   ```

   注意必须 `default-features = false` 再开 `alloc`，否则会被默认拉进 `std`。

2. 在 `lib.rs` 顶部写 `#![no_std]`，并 `extern crate alloc;`，再准备一个简单的 `Box`/`String` 用法以确认 `alloc` 确实可用。

3. 写一个函数，**自行构造 `Collector`**（而不是用 `default_collector`），并 pin 一个 `Guard`：

   ```rust
   // 示例代码：演示 no_std + alloc 下使用 base::SkipList
   use crossbeam_epoch as epoch;
   use crossbeam_skiplist::base::SkipList;

   pub fn demo() {
       let collector = epoch::Collector::new();
       let list: SkipList<u64, u64> = SkipList::new(collector.clone());
       let guard = collector.register();   // 注册当前线程/上下文
       let pin = guard.pin();              // 进入临界区，拿到 Guard

       list.insert(1u64, 10u64, &pin);
       let entry = list.get(&1u64, &pin);
       assert!(entry.is_some());
       list.remove(&1u64, &pin);
       assert!(list.get(&1u64, &pin).is_none());
   }
   ```

   > 上述 API 名称（如 `Collector::register` / `LocalHandle::pin`）来自 `crossbeam-epoch`，请以你本地 `crossbeam-epoch` 版本的实际签名为准；若签名有出入，请到 `crossbeam-epoch` 文档里查对应方法名再调整。具体能否在 `no_std + alloc` 下编译通过，**待本地验证**。

4. 编译这个外部 crate（`cargo build`），确认它**不依赖 `std`**（可以用 `cargo tree` 或检查是否链接了 `std`）。

5. 把 `features` 改成 `["std"]` 重新编译，对比二进制大小 / 依赖变化，体会「`std` 档多带来了什么」。

**需要观察的现象与预期结果**：

- `alloc` 档下，外部 crate 能编译并调用 `base::SkipList`，全程没有 `std`（没有 `thread_local`、没有默认 `Collector`）。
- 你必须自己管 `Collector` 与 `Guard`，这正是「base 能裸跑的代价」。
- 切到 `std` 档后，你可以改用 `SkipMap`，代码里 `Collector`/`Guard` 全部消失——这就是高层封装的价值。

这个综合实践覆盖了本讲全部三个最小模块：三档门控（步骤 1 的 feature 配置）、`base` vs `map`/`set` 的 `std` 分界（步骤 3 必须用 base 且自管 Collector）、以及 `alloc_helper::Global` 在底层支撑了 `Node` 的分配（步骤 3 的每一次 insert 都会经 `Global.allocate`，可回顾 4.3.3）。

## 6. 本讲小结

- `crossbeam-skiplist` 顶部声明 `#![no_std]`，靠 `extern crate alloc`（`alloc` 档）和 `extern crate std`（`std` 档）按需引入标准库，形成 `core` → `alloc` → `std` 三档；其中「`core` 档（双关 feature）」官方标注暂不支持，实际可用的是 `alloc` 档和 `std` 档。
- 每个需要堆与原子的模块都挂在 `#[cfg(all(feature = "alloc", target_has_atomic = "ptr"))]` 门后；`map`/`set` 进一步要求 `feature = "std"`；`comparator`/`equivalent` 无门控、永远可见。
- `base::SkipList` 能在 `no_std + alloc` 跑，是因为它把 `Collector` 当构造参数；`SkipMap`/`SkipSet` 必须依赖 `std`，是因为它们用 `epoch::default_collector()` 取进程级全局 `Collector`，而后者建立在 `thread_local`（`std`）之上。
- `alloc_helper::Global` 是手写的极简分配器，封装 `alloc::alloc::alloc`/`dealloc` 并妥善处理 ZST；它存在的理由是 `alloc::alloc::Global`、`Layout::dangling`、`ptr::without_provenance_mut` 都尚未稳定，而本 crate MSRV=1.74（stable）。
- `without_provenance_mut` 在 Miri 与非 Miri 下分叉实现，兼顾 provenance 追踪与 CHERI 兼容；`Global.allocate`/`deallocate` 在 `base.rs` 的 `Node::alloc`/`dealloc` 中被直接调用，是节点内存的唯一出入口。
- 用 `--no-default-features --features alloc` 可亲手验证：`base`/`SkipList` 可见，`SkipMap`/`SkipSet` 消失。

## 7. 下一步学习建议

- 下一讲 **u5-l17 内存序分析**：本讲我们看到 `base.rs` 大量 `core::sync::atomic` 的 `Ordering`。下一讲会系统梳理 `Relaxed`/`Release`/`Acquire`/`SeqCst` 在 `base.rs` 各处的选择与理由，并解释 `NodeRef::decrement` 的 `fetch_sub(Release)` + `fence(Acquire)` 为何能保证回收安全。
- 若想深入「为什么 `no_std` 还能做无锁回收」，建议阅读 `crossbeam-epoch` 自身的 `no_std` + `alloc` 设计，重点看 `Collector`、`LocalHandle`、`Guard` 三者的关系，以及它在没有 `thread_local` 时的退化方案。
- 如果你对 ZST 与 `Layout` 的边界情况感兴趣，可以回到 u2-l5 重读 `Node::get_layout`（`Layout::extend` + `pad_to_align`），与本讲的 `alloc_helper::Global` 对 `size == 0` 的处理对照，体会 Rust 手动内存管理对「变长 + ZST」的完整处理范式。
