# 主 crate 重导出门面与目录结构

## 1. 本讲目标

本讲聚焦 crossbeam 仓库根目录下那个名叫 `crossbeam` 的主 crate——它本身**几乎不写任何实现代码**，唯一的职责是把若干子 crate 重新打包成一个统一的 `crossbeam::*` 命名空间。

读完本讲，你应当能够：

1. 说清楚什么是「门面（facade）重导出」模式，以及 crossbeam 为什么这样组织代码。
2. 看懂 `src/lib.rs` 中 `#![no_std]` 与 `extern crate std`、`#[cfg(feature = "...")]` 三者如何配合，实现「同一份源码、no_std/alloc/std 三级能力」。
3. 读懂 `pub use` 与 `#[doc(inline)]` 是怎么把 6 个子 crate 的公开项拼成一张干净的对外 API 表，并能对照源码画出 `crossbeam` 的模块树。
4. 理解 `tests/subcrates.rs` 如何作为「契约测试」守住这个门面，以及 `no_atomic.rs` 这个由 CI 生成、被符号链接进子 crate 的数据文件起什么作用。

> 本讲承接 u1-l1（项目全景）与 u1-l2（工作区与特性系统）。那两讲建立了「crossbeam 是 8 个 crate 组成的 workspace、主 crate 以门面重导出子 crate、能力按 no_std/alloc/std 分级」的认知；本讲不再重复这些结论，而是钻进主 crate 唯一的源码文件 `src/lib.rs`，看门面到底是**怎么**搭起来的。

## 2. 前置知识

- **Cargo workspace**：多个 crate 共享同一份 `Cargo.lock` 和构建配置。crossbeam 的 workspace 成员在根 `Cargo.toml` 的 `[workspace] members` 里列出（详见 u1-l2）。
- **`pub use` 重导出**：Rust 里可以把别处的项「再次公开」。例如 `pub use foo::Bar;` 让 `Bar` 也能通过当前 crate 访问。这是搭门面的核心工具。
- **`#[cfg(...)]` 条件编译**：编译期根据条件（如某个 feature 是否开启、某个目标平台）决定某段代码是否参与编译。
- **feature（特性）**：Cargo 的编译期开关。crossbeam 的关键特性是 `std`（默认开启）和 `alloc`，二者构成一条「连带链」：开 `std` 会自动带上 `alloc`（详见 u1-l2）。
- **no_std**：不链接标准库 `std`、只依赖 `core`（和可选的 `alloc`）的 Rust 程序，常用于嵌入式、内核等无操作系统环境。
- **门面模式（Facade）**：用一个「外壳」把内部多个子系统重新组织成一个更简洁的对外接口。在这里，主 crate `crossbeam` 就是壳，6 个子 crate 是子系统。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用它讲什么 |
|---|---|---|
| [src/lib.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/src/lib.rs) | 主 crate 的**全部源码**，一个文件搭起整个门面 | `#![no_std]`、`extern crate std`、`#[cfg]`、`pub use`、`#[doc(inline)]` |
| [tests/subcrates.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/tests/subcrates.rs) | 门面的「契约测试」，逐个子 crate 验证重导出可用 | 守住门面不被破坏 |
| [no_atomic.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/no_atomic.rs) | 由 CI 生成的「不支持原子操作」的目标三元组清单 | 编译期探测目标平台能力 |

> 关键事实：主 crate 的 `src/` 目录下**只有 `lib.rs` 这一个文件**，没有任何实现。这本身就是「门面」最有力的证据——这个 crate 不造轮子，只做搬运与包装。

## 4. 核心概念与源码讲解

### 4.1 门面模式：为什么主 crate 只有一个 `lib.rs`

#### 4.1.1 概念说明

crossbeam 把功能拆成了 6 个独立的子 crate（`crossbeam-utils`、`crossbeam-channel`、`crossbeam-epoch`、`crossbeam-queue`、`crossbeam-deque`、`crossbeam-skiplist`）。这样做的代价是：用户想用多个功能时，得记住 6 个不同的 crate 名字、在 `Cargo.toml` 里加 6 行依赖、在代码里写 6 种路径。

「门面」就是解决这个痛点的——做一个名叫 `crossbeam` 的主 crate，它把常用子 crate 的内容重新挂到统一的 `crossbeam::*` 路径下。用户只需要依赖一个 `crossbeam`，就能用 `crossbeam::channel`、`crossbeam::epoch`、`crossbeam::utils::Backoff` 等一致的写法。

正因为主 crate 只做「搬运」，它的 `src/` 目录里**只有 `lib.rs` 一个文件**，里面全是 `pub use`，没有任何业务逻辑。

#### 4.1.2 核心流程

门面的搭建可以归纳为三步：

1. **声明自己 no_std**：主 crate 以 `#![no_std]` 起步，默认不拉入标准库。
2. **按特性逐层开门**：用 `#[cfg(feature = "...")]` 决定在 `std`/`alloc`/无特性 三种组合下分别重导出哪些子 crate。
3. **重导出 + 文档内联**：用 `pub use` 把子 crate 挂到主 crate 路径上，必要时用 `#[doc(inline)]` 让文档把它们当成「原生模块」展示。

#### 4.1.3 源码精读

`src/lib.rs` 开头是一大段模块级文档注释，相当于给整个 crate 写的「目录」，把工具分成 Atomics / Data structures / Memory management / Thread synchronization / Utilities 五大类：

[src/lib.rs:1-39](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/src/lib.rs#L1-L39) ——这段文档注释就是门面给用户的「导览图」，里面所有的 `[`xxx`]` 链接都指向**重导出后**的路径（如 `[`AtomicCell`]: atomic::AtomicCell`），而不是子 crate 原路径。

而真正干活的「搬运代码」只有文件末尾的 20 多行（下两节精读）。文件其余部分全是属性与文档。一个 crate 的全部可执行逻辑只有约 20 行 `pub use`——这就是门面模式最直观的样子。

#### 4.1.4 代码实践

1. **实践目标**：确认主 crate 是纯门面、不含实现。
2. **操作步骤**：在仓库根目录列出 `src/` 下所有 `.rs` 文件。
   ```bash
   ls -1 src
   ```
3. **需要观察的现象**：只应看到 `lib.rs` 一个文件。
4. **预期结果**：`lib.rs` 是 `src/` 下唯一的源码文件。可以再 `wc -l src/lib.rs`，会发现整个门面只有约 80 行（其中大半是注释）。

#### 4.1.5 小练习与答案

- **练习 1**：如果用户同时依赖了 `crossbeam` 和 `crossbeam-channel` 两个 crate，会不会编译出两份 `crossbeam-channel` 的代码？
  - **答案**：不会。Cargo 对同一个 crate（按包名 + 版本去重）只编译一次。主 crate 的 `pub use crossbeam_channel as channel` 与用户直接依赖的 `crossbeam_channel` 指向同一个编译产物，主 crate 只是对它做了一层路径别名。
- **练习 2**：为什么 crossbeam 不直接把所有功能写进一个 crate，而要先拆成 6 个子 crate、再用门面打包？
  - **答案**：拆 crate 让每个模块能独立设定特性、版本号、MSRV 与依赖，便于单独发布与复用（例如嵌入式项目可能只想要 `crossbeam-utils`，不想拉入需要 `std` 的 channel）。门面则是为了给「全都要」的用户一个统一入口。两者并不矛盾。

### 4.2 `#![no_std]` 与 `extern crate std`：三级能力的总开关

#### 4.2.1 概念说明

主 crate 想同时服务三类用户：嵌入式（no_std，连堆分配都没有）、需要堆但没操作系统的（alloc）、以及普通带标准库的环境（std）。crossbeam 的策略是「同一份源码、按特性分级」。

这套分级的起点，是文件顶部的 `#![no_std]`。它告诉编译器：**默认情况下不要把 `std` 拉进来**。这样在没有标准库的目标上也能编译。但是当用户开启了 `std` 特性时，又必须能把 `std` 用回来——这件事由一行条件编译的 `extern crate std;` 完成。

#### 4.2.2 核心流程

三级能力的「点亮」关系（承接 u1-l2 的特性连带链）：

```
default = ["std"]
   │
   std   ──连带给──►  alloc                 （开 std 自动开 alloc）
   │                       │
   ├──► crossbeam-utils/std   ├──► crossbeam-epoch/alloc
   ├──► crossbeam-channel/std       crossbeam-queue/alloc
   ├──► crossbeam-deque/std
   ├──► crossbeam-epoch/std
   ├──► crossbeam-queue/std
   └──► crossbeam-utils/std
```

落到 `src/lib.rs` 上只有两行关键字：

- `#![no_std]`：默认不链接 `std`。
- `#[cfg(feature = "std")] extern crate std;`：仅在开启 `std` 特性时，显式把 `std` 拉回作用域。

> 为什么需要 `extern crate std;`？在 `#![no_std]` crate 里，`std` 不会像普通 crate 那样被自动引入 preluded 作用域。要用 `std::thread`、`std::sync::Mutex` 等任何标准库项，就必须先 `extern crate std;` 把它显式声明进来。这行被 `#[cfg(feature = "std")]` 包住，所以关掉 `std` 特性时它不参与编译，crate 保持 no_std。

#### 4.2.3 源码精读

[src/lib.rs:41-55](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/src/lib.rs#L41-L55) ——这几行是分级的总开关。关键三段：

```rust
#![no_std]                                  // L41：默认 no_std
#![doc(test( ... ))]                        // L42-45：doctest 不自动注入 crate 名
#![warn(missing_docs, ... )]                // L46-52：lint 配置

#[cfg(feature = "std")]
extern crate std;                           // L54-55：仅 std 特性下显式引入 std
```

- L41 的 `#![no_std]` 让这个 crate 默认只能用 `core`。
- L54-55 的 `#[cfg(feature = "std")] extern crate std;` 是「把 std 重新打开」的钥匙。子 crate 那边（如 `crossbeam-utils/src/lib.rs`）也有同样的写法，参见 [crossbeam-utils/src/lib.rs:41-45](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/lib.rs#L41-L45)，这是一种 crossbeam 全家桶统一约定。
- L46-52 的 `#![warn(...)]` 里有三条值得注意：`clippy::alloc_instead_of_core`、`clippy::std_instead_of_alloc`、`clippy::std_instead_of_core`——它们强制开发者在写代码时必须**显式选择**用 `core`/`alloc`/`std` 中的哪一个，从 lint 层面守住「能 no_std 的就别偷用 std」的纪律。这也是门面能干净分级的隐性保障。

#### 4.2.4 代码实践

1. **实践目标**：直观感受「同一份 `src/lib.rs`，关掉 std 后能编译、开了 std 才有 channel」。
2. **操作步骤**：
   ```bash
   # 只编译主 crate 本身，关掉默认特性（仅保留 utils 必选）
   cargo build -p crossbeam --no-default-features

   # 关掉默认特性后，再单独开 alloc
   cargo build -p crossbeam --no-default-features --features alloc
   ```
3. **需要观察的现象**：
   - 第一条命令应当成功——因为 `crossbeam::utils`、`crossbeam::atomic` 这些路径不依赖 `std`。
   - 第二条命令成功后，`crossbeam::epoch`、`crossbeam::queue` 这两个仅要求 `alloc` 的模块变得可用。
4. **预期结果**：两条命令都应编译通过；若在 `--no-default-features` 下尝试 `use crossbeam::channel;`，会因为没有 `std` 特性而报「找不到 `channel`」的错误——这正是 `#[cfg(feature = "std")]` 在起作用。
5. **若环境不支持某条命令**：待本地验证。

#### 4.2.5 小练习与答案

- **练习 1**：把 `#[cfg(feature = "std")] extern crate std;` 这行删掉，但在 `std` 特性下编译，会发生什么？
  - **答案**：因为顶部有 `#![no_std]`，`std` 不会自动进入作用域；一旦后续代码（或被重导出的、需要 `std` 的子 crate 的某些路径）引用到 `std::` 下的项，就会因找不到 `std` 而编译失败。这行就是把 `std` 重新「解锁」的开关。
- **练习 2**：为什么 `alloc` 特性下不需要 `extern crate alloc;`（而 `std` 下需要 `extern crate std;`）？
  - **答案**：实际上是否需要显式 `extern crate` 与 crate 的 edition/prelude 行为有关。在本 crate 中，`alloc` 相关的能力是通过**子 crate**（`crossbeam-epoch`/`crossbeam-queue`）的重导出间接获得的，主 crate 自己并不直接 `use alloc::`，因此不需要在 `src/lib.rs` 里显式 `extern crate alloc;`。而 `std` 之所以显式声明，是 crossbeam 的统一风格（子 crate 里也能看到同样的 `extern crate std;`）。待本地进一步验证具体引用点。

### 4.3 `pub use` 重导出与 `#[doc(inline)]`：统一命名空间是怎么拼出来的

#### 4.3.1 概念说明

门面的核心动作是「重新摆放命名空间」。`src/lib.rs` 末尾约 20 行 `pub use` 把 6 个子 crate 的公开项重新挂到 `crossbeam::*` 下。但注意——它**不是**机械地把 `crossbeam_utils` 原封不动地暴露出来，而是做了一些有意的「重塑」：

- 有些子 crate 直接作为模块别名挂出（如 `crossbeam_channel as channel`）。
- 有些只挑出个别类型，装进一个手写的 `utils` 模块（如 `Backoff`、`CachePadded`）。
- 实验性的 `crossbeam-skiplist` **故意不被**门面收编（它只是 workspace 成员，不是主 crate 的依赖；详见 u1-l1）。

`#[doc(inline)]` 则是文档层面的润色：默认情况下，`pub use` 重导出的项在 rustdoc 里会显示成「Re-exports」条目；加上 `#[doc(inline)]` 后，被重导出的内容会**直接内联**到当前路径下显示，让 `crossbeam::channel` 在文档里看起来就像一个原生模块，达到「无感门面」的效果。

#### 4.3.2 核心流程

主 crate 对外路径与子 crate 的对应关系（这是本讲最重要的一张表）：

| 主 crate 对外路径 | 来源 | 触发特性 | 说明 |
|---|---|---|---|
| `crossbeam::atomic` | `crossbeam_utils::atomic` | 始终 | utils 必选且始终带 `atomic` |
| `crossbeam::utils::{Backoff, CachePadded}` | `crossbeam_utils::{Backoff, CachePadded}` | 始终 | **手写** `utils` 模块，只挑这两个类型 |
| `crossbeam::thread`, `crossbeam::scope` | `crossbeam_utils::thread` | `std` | 作用域线程 |
| `crossbeam::sync` | `crossbeam_utils::sync` | `std` | Parker/ShardedLock/WaitGroup |
| `crossbeam::channel`, `crossbeam::select` | `crossbeam_channel` | `std` | crate 别名 + 宏 |
| `crossbeam::deque` | `crossbeam_deque` | `std` | crate 别名 |
| `crossbeam::epoch` | `crossbeam_epoch` | `alloc` | 仅需堆分配 |
| `crossbeam::queue` | `crossbeam_queue` | `alloc` | 仅需堆分配 |

可以看到一个微妙之处：**`crossbeam::utils` 并不是 `crossbeam_utils` 的整体别名**，而是一个只装了 `Backoff` 和 `CachePadded` 的手写模块。如果你想要 `crossbeam_utils` 里的其它东西（如 `atomic`），得走 `crossbeam::atomic` 这个独立路径。

#### 4.3.3 源码精读

[src/lib.rs:57-79](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/src/lib.rs#L57-L79) ——这 20 多行就是整个门面的「搬运区」。逐段看：

```rust
pub use crossbeam_utils::atomic;                       // L57：始终可用
pub mod utils {                                        // L59：手写模块
    pub use crossbeam_utils::{Backoff, CachePadded};   // L65：只挑这两个
}
#[cfg(feature = "std")]
#[cfg(not(crossbeam_loom))]
pub use crossbeam_utils::thread::{self, scope};        // L68-70：std 下挂 thread/scope
#[cfg(feature = "std")]
#[doc(inline)]
pub use {
    crossbeam_channel as channel,                      // crate 别名
    crossbeam_channel::select,                         // 宏单独导出
    crossbeam_deque as deque,
    crossbeam_utils::sync,
};                                                     // L71-76：std 下整组挂出
#[cfg(feature = "alloc")]
#[doc(inline)]
pub use { crossbeam_epoch as epoch, crossbeam_queue as queue }; // L77-79：alloc 下挂 epoch/queue
```

几个关键细节：

- **L57 `pub use crossbeam_utils::atomic;`**：把 utils 的 `atomic` 模块直接平移到根。因为 utils 是**必选依赖**（根 `Cargo.toml` 里始终带 `features = ["atomic"]`，参见 [Cargo.toml:56](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/Cargo.toml#L56)），所以这行不带任何 `cfg`，永远参与编译。
- **L59-66 手写的 `utils` 模块**：注意它是 `pub mod utils { ... }` 而不是 `pub use`，因此可以在模块体内写自己的文档注释（L62-64）。这是一种「窄门面」——只把最常用的 `Backoff`/`CachePadded` 暴露出来，避免把整个 `crossbeam_utils` 倾倒给用户。
- **L68-70 的双重 cfg**：`#[cfg(feature = "std")]` 叠加 `#[cfg(not(crossbeam_loom))]`。`crossbeam_loom` 是测试用的内部 cfg（用 loom 做并发模型检查时启用，详见 u7-l3），在 loom 模式下不挂这组 `thread`，改走另一套。这是门面对「测试态」的让路。
- **L70 `thread::{self, scope}`**：`self` 把 `crossbeam_utils::thread` 模块挂成 `crossbeam::thread`，`scope` 再额外把函数挂成 `crossbeam::scope`，这样用户既能写 `crossbeam::scope(...)`，也能写 `crossbeam::thread::scope(...)`（两种写法在 u1-l4 里都会用到）。
- **L71-76 与 L77-79 的 `#[doc(inline)]`**：让 `channel`/`deque`/`sync`/`epoch`/`queue` 在文档里**内联展开**，用户点开 `crossbeam::channel` 时看到的是 channel 的完整内容，而不是一行冷冰冰的「Re-export of crossbeam_channel」。注意 `thread`（L68-70）和 `atomic`（L57）**没有**加 `#[doc(inline)]`，文档里它们会显示为重导出——这是有意为之的取舍。
- **为什么 epoch/queue 只要 `alloc` 而 channel/deque 要 `std`？** 因为跳表/队列的无锁结构靠 `crossbeam-epoch` 做内存回收，只需要堆（`alloc`）；而 channel、作用域线程、Parker 这些需要操作系统级的线程与同步原语，必须 `std`。所以门面把这两组分到不同的 `cfg` 门后。

> 顺带一提：`select`（L74）是被单独 `pub use` 出来的**宏**，所以它在根路径 `crossbeam::select`，而不是 `crossbeam::channel::select`。`tests/subcrates.rs` 第 3 行 `use crossbeam::select;` 正好印证了这一点。

#### 4.3.4 代码实践

1. **实践目标**：验证「两条路径，同一个东西」——门面路径与子 crate 原路径指向同一个类型。
2. **操作步骤**：写一个最小二进制，分别从两个路径拿到 `unbounded` 通道：
   ```rust
   // 示例代码：放在一个依赖了 crossbeam 和 crossbeam-channel 的 crate 里
   fn main() {
       // 路径一：通过门面
       let (s1, _r1) = crossbeam::channel::unbounded::<i32>();

       // 路径二：通过子 crate 原路径
       let (s2, _r2) = crossbeam_channel::unbounded::<i32>();

       // 两条路径产出的 Sender 是同一个类型，可以放进同一个集合
       let senders: Vec<crossbeam_channel::Sender<i32>> = vec![s1, s2];
       for s in senders { let _ = s.send(0); }
   }
   ```
   对应的 `Cargo.toml` 片段：
   ```toml
   [dependencies]
   crossbeam = { path = ".." }                 # 或 version = "0.8"
   crossbeam-channel = { path = "../crossbeam-channel" }
   ```
3. **需要观察的现象**：`crossbeam::channel::Sender` 与 `crossbeam_channel::Sender` 可以无障碍地放进同一个 `Vec`，编译器不报类型不匹配。
4. **预期结果**：编译通过、运行正常。这证明门面只是路径别名，背后是同一个具体类型。

#### 4.3.5 小练习与答案

- **练习 1**：`crossbeam::utils` 与 `crossbeam_utils` 是同一个模块吗？为什么 `crossbeam::utils::atomic` 不存在？
  - **答案**：不是。`crossbeam::utils` 是主 crate 手写的「窄模块」，只装了 `Backoff` 和 `CachePadded`。`atomic` 是通过另一条独立的 `pub use crossbeam_utils::atomic;` 挂在根上的，所以路径是 `crossbeam::atomic`，而不是 `crossbeam::utils::atomic`。
- **练习 2**：如果想让 `crossbeam::channel` 在文档里**不**内联（保留「Re-export」字样），应改哪里？
  - **答案**：删掉 [src/lib.rs:72](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/src/lib.rs#L72) 那行 `#[doc(inline)]`。默认行为是显示为重导出。注意这只是文档展示差异，不影响代码语义。
- **练习 3**：为什么 `crossbeam::epoch` 只要 `alloc` 特性，而 `crossbeam::channel` 要 `std`？
  - **答案**：epoch（无锁内存回收）和 queue（无锁队列）只需要堆分配就能工作；channel 需要真正的线程阻塞/唤醒（依赖操作系统的同步原语，住在 `std` 里），所以必须 `std`。门面据此把它们分到 `#[cfg(feature = "alloc")]` 与 `#[cfg(feature = "std")]` 两扇门后。

### 4.4 跨 crate 公开类型检查：`tests/subcrates.rs`

#### 4.4.1 概念说明

门面是「纯搬运」，最容易出的问题就是：某次重构把某个 `pub use` 删了或改了 `cfg`，导致 `crossbeam::xxx` 路径突然消失，依赖门面的用户代码集体编译失败。为了防止这种回归，crossbeam 在 `tests/subcrates.rs` 里写了一组「契约测试」——**不测行为，只测「这些路径确实存在、确实可用」**。

这是一种很实用的工程习惯：给公共 API 写「存在性测试」，把 API 表面本身当成一种契约钉住。

#### 4.4.2 核心流程

测试文件为每个被门面收编的子 crate 写一个独立的 `#[test]` 函数，每个函数只做两件事：

1. 通过 `crossbeam::xxx` 门面路径构造/调用一个代表性 API。
2. 让编译器「走一遍」这条路径，从而证明重导出仍然有效。

只要某个 `pub use` 失效，对应的测试函数会编译失败，CI 立刻变红。

#### 4.4.3 源码精读

[tests/subcrates.rs:1-47](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/tests/subcrates.rs#L1-L47) ——整份文件就是 5 个微型测试。开头先确认宏路径：

```rust
use crossbeam::select;   // L3：证明 select 宏在 crossbeam 根下可用
```

随后每个子 crate 一个测试：

- [tests/subcrates.rs:5-13](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/tests/subcrates.rs#L5-L13) `channel`：构造 `bounded(1)` 并用 `select!` 宏收发，同时验证 `crossbeam::channel` 与 `crossbeam::select` 两条路径。
- [tests/subcrates.rs:15-20](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/tests/subcrates.rs#L15-L20) `deque`：`crossbeam::deque::Worker::new_fifo()` + `push/pop`。
- [tests/subcrates.rs:22-25](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/tests/subcrates.rs#L22-L25) `epoch`：`crossbeam::epoch::pin()`，验证 epoch 门面可达。
- [tests/subcrates.rs:27-32](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/tests/subcrates.rs#L27-L32) `queue`：`crossbeam::queue::ArrayQueue::new(10)`。
- [tests/subcrates.rs:34-47](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/tests/subcrates.rs#L34-L47) `utils`：这一条最有意思，它同时验证了**三种**路径——
  ```rust
  crossbeam::utils::CachePadded::new(7);   // 手写的 utils 模块
  crossbeam::scope(|scope| { ... });        // 根上的 scope 函数
  crossbeam::thread::scope(|scope| { ... });// thread 模块下的 scope
  ```
  它正好对应 [src/lib.rs:59-70](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/src/lib.rs#L59-L70) 里那几条「形状不同」的重导出。

> 这份测试**故意写在主 crate 的 `tests/` 目录**（集成测试）而不是某个子 crate 里，因为它要站在「最终用户」的视角——只能看到 `crossbeam::*` 门面路径，看不到子 crate 内部。这恰好是门面要保证的契约。

#### 4.4.4 代码实践

1. **实践目标**：亲手跑一遍契约测试，感受它的「存在性守护」作用。
2. **操作步骤**：
   ```bash
   cargo test -p crossbeam --test subcrates
   ```
3. **需要观察的现象**：5 个测试全部通过，每个测试只花了极少时间（它们不做性能或并发压力，只验证路径存在）。
4. **预期结果**：输出 `5 passed`。若故意把 `src/lib.rs` 里某条 `pub use` 注释掉再跑，对应的测试函数会**编译失败**——这正是契约测试的报警方式。

#### 4.4.5 小练习与答案

- **练习 1**：为什么这些测试不写任何 `assert_eq!` 之类的断言，依然有价值？
  - **答案**：它们的价值在「编译期」。只要测试能编译通过，就说明门面路径全部存在、类型可构造、方法可调用。回归发生时，失败发生在编译阶段（`error: cannot find ...`），比运行期断言更早暴露问题。
- **练习 2**：如果把 `utils` 测试里的 `crossbeam::thread::scope(...)` 改成 `crossbeam_utils::thread::scope(...)`，测试还能守住门面吗？
  - **答案**：不能。那样验证的就是子 crate 原路径，而非门面路径。门面被破坏时（例如某条 `pub use` 丢失），这个测试照样编译通过，失去守护意义。所以契约测试必须**只走门面路径**。

### 4.5 `no_atomic.rs`：编译期探测「没有原子操作」的目标

#### 4.5.1 概念说明

门面的「始终可用」里其实藏着一个前提：目标平台得支持原子操作。`crossbeam::atomic`（也就是 `crossbeam_utils::atomic`）里的 `AtomicCell`、`AtomicConsume` 都依赖原子指令。但 Rust 支持一些**完全没有原子操作**的目标（某些老的 ARM、MIPS、BPF、MSP430 等）。在这些目标上，crossbeam 必须在编译期「知道自己不能用」，从而降级或关闭 atomic 模块。

`no_atomic.rs` 就是一份这样的「黑名单」——由 CI 脚本自动生成的、不支持原子操作的目标三元组列表。它本身只是一个常量数组，但它是 crossbeam 整套「按平台能力分级」的输入数据。

#### 4.5.2 核心流程

`no_atomic.rs` 的生命周期：

1. **生成**：CI 脚本 `ci/no_atomic.sh` 用 `rustc --print all-target-specs-json` 列出所有已知目标，筛出 `max-atomic-width == 0` 或不满足 8 位最小原子宽度的目标，生成 `no_atomic.rs`（见 [ci/no_atomic.sh:22](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/ci/no_atomic.sh#L22)）。
2. **消费**：`crossbeam-utils/build.rs` 用 `include!("no_atomic.rs")` 把这份常量数组纳入构建脚本（参见 [crossbeam-utils/build.rs:15](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/build.rs#L15)）。
3. **判定**：build.rs 拿到当前编译目标的三元组，若它出现在黑名单里，就 `println!("cargo:rustc-cfg=crossbeam_no_atomic")`，向编译器注入一个 cfg 标志（参见 [crossbeam-utils/build.rs:39-41](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/build.rs#L39-L41)）。
4. **使用**：子 crate 源码用 `#[cfg(not(crossbeam_no_atomic))]` 来决定是否编译依赖原子操作的代码（例如 [crossbeam-utils/src/atomic/consume.rs:1](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/atomic/consume.rs#L1)）。

> 有一个容易忽略的细节：根目录的 `no_atomic.rs` 是**正本**，`crossbeam-utils/no_atomic.rs` 是一个指向 `../no_atomic.rs` 的**符号链接**。所以 build.rs 里的 `include!("no_atomic.rs")` 实际纳入的是根目录这份正本。这是 crossbeam 为了「一份名单、多个 crate 共享」而采用的轻量手段。

#### 4.5.3 源码精读

[no_atomic.rs:1-13](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/no_atomic.rs#L1-L13) ——整份文件就是一个常量数组：

```rust
// This file is @generated by no_atomic.sh.
// It is not intended for manual editing.

const NO_ATOMIC: &[&str] = &[
    "armv4t-none-eabi",
    "armv5te-none-eabi",
    "bpfeb-unknown-none",
    "bpfel-unknown-none",
    "mipsel-sony-psx",
    "msp430-none-elf",
    "thumbv4t-none-eabi",
    "thumbv5te-none-eabi",
];
```

要点：

- **L1-2 的注释**强调「由脚本生成、不要手改」。任何新增「无原子目标」的修订都应通过重跑 `ci/no_atomic.sh` 完成，而不是人手编辑。CI 里专门有一个 job 在名单过期时自动提 PR（参见 [.github/workflows/ci.yml:160-190](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/.github/workflows/ci.yml#L160-L190)）。
- **L4-13 的目标清单**：可以看到 `msp430-none-elf`（16 位 MCU，无原子）、`*-none-eabi` 的早期 ARM、BPF 目标等——它们都没有可用的原子指令。
- 注意：主 crate 的 `src/lib.rs` **本身并不直接 `include!` 这份文件**，它对原子能力的「感知」完全是通过依赖 `crossbeam-utils`（在 build.rs 阶段注入 `crossbeam_no_atomic` cfg）间接获得的。所以 `no_atomic.rs` 虽然放在仓库根、被列为本讲关键源码，但它的直接消费者是 `crossbeam-utils/build.rs`，主 crate 是「间接受益」。

#### 4.5.4 代码实践

1. **实践目标**：看清「名单 → cfg → 源码门控」的完整链路。
2. **操作步骤**（源码阅读型实践）：
   - 打开 [crossbeam-utils/build.rs:15](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/build.rs#L15)，确认它 `include!("no_atomic.rs")`（走符号链接到根正本）。
   - 跟到 [crossbeam-utils/build.rs:39-41](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/build.rs#L39-L41)，看 `NO_ATOMIC.contains(&&*target)` 如何触发 `crossbeam_no_atomic` cfg。
   - 用 Grep 在 `crossbeam-utils/src/` 下搜索 `crossbeam_no_atomic`，看哪些源码用 `#[cfg(not(crossbeam_no_atomic))]` 把原子代码「包」起来。
3. **需要观察的现象**：会看到 `consume.rs` 等文件用该 cfg 做条件编译；atomic 模块在无原子目标上整体被裁剪。
4. **预期结果**：你能画出「目标三元组 → `NO_ATOMIC` 命中 → `crossbeam_no_atomic` cfg → 源码门控」的完整数据流。可选的运行验证（在本地有 nightly 时）：`cargo build -p crossbeam-utils --target msp430-none-elf`，观察 atomic 相关代码被裁剪（待本地验证）。

#### 4.5.5 小练习与答案

- **练习 1**：为什么不直接在源码里写死一个 `#[cfg(target_arch = "msp430")]` 之类的判断，而要维护一份名单？
  - **答案**：因为「无原子」的目标会随 Rust 工具链更新而增减，且判定依据是 `max-atomic-width`/`min-atomic-width` 这类目标规格字段，不是简单的 `target_arch`。用脚本从 `rustc` 的目标规格动态生成名单，比手写一堆 `cfg` 更准确、更易维护。
- **练习 2**：`no_atomic.rs` 与本讲的「门面」主题有什么关系？
  - **答案**：它守护的是门面里那句「`crossbeam::atomic` 始终可用」的前提。在普通目标上，`atomic` 模块确实始终可用；但在无原子目标上，crossbeam 通过这份名单在编译期识别并降级，避免给用户提供一份「编译通过但运行错误」的 atomic 实现。门面的承诺因此是「有条件」的——条件由 `no_atomic.rs` 表达。

## 5. 综合实践

把本讲的知识串起来，完成下面这个**门面速写 + 双路径验证**的任务。

**任务一：画出 `crossbeam` 的模块树**

对照 [src/lib.rs:57-79](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/src/lib.rs#L57-L79)，手绘（或用文本）画出 `crossbeam` 的对外模块树，要求：

1. 标注每个公开项来自哪个子 crate。
2. 标注每个公开项需要哪个特性（始终 / `std` / `alloc`）。
3. 特别标出 `crossbeam::utils` 是「手写窄模块」、`crossbeam-skiplist` 「未被门面收编」这两个例外。

参考骨架（请补全来源与特性）：

```
crossbeam
├── atomic            ← crossbeam_utils::atomic        (始终)
├── utils
│   ├── Backoff       ← ?
│   └── CachePadded   ← ?
├── channel           ← ?                              (std)
├── select            ← ?                              (std, 宏)
├── deque             ← ?                              (std)
├── epoch             ← ?                              (alloc)
├── queue             ← ?                              (alloc)
├── sync              ← ?                              (std)
├── thread            ← ?                              (std)
└── scope             ← ?                              (std)
```

**任务二：双路径同源验证**

新建一个最小 crate，同时在 `[dependencies]` 里加上 `crossbeam` 与 `crossbeam-channel`，写一段代码：

1. 分别用 `crossbeam::channel::unbounded()` 与 `crossbeam_channel::unbounded()` 创建两个发送端。
2. 把两个发送端放进同一个 `Vec<crossbeam_channel::Sender<i32>>`，证明它们是同一个类型。
3. 再用 `crossbeam::select!` 宏同时 `recv` 两个接收端（参考 [tests/subcrates.rs:9-12](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/tests/subcrates.rs#L9-L12) 的写法）。

**任务三：跑契约测试**

```bash
cargo test -p crossbeam --test subcrates
```

确认 5 个测试全部通过，从而确认你画出的这张门面表与代码实际行为一致。

> 预期：任务一你能填出与本讲 4.3 节那张表一致的内容；任务二编译通过、运行正常；任务三输出 `5 passed`。三者合起来，说明你已经完整掌握了 crossbeam 的门面结构。

## 6. 本讲小结

- 主 crate `crossbeam` 是一个**纯门面**：`src/` 下只有 `lib.rs` 一个文件，没有任何实现，全部由 `pub use` 搬运子 crate 组成。
- `#![no_std]` + `#[cfg(feature = "std")] extern crate std;` 是「同一份源码、no_std/alloc/std 三级能力」的总开关，叠加 `clippy::*_instead_of_*` lint 守住纪律。
- 门面**有意识地重塑**命名空间：`atomic` 平移到根、`Backoff`/`CachePadded` 装进手写的 `utils` 窄模块、`channel`/`deque`/`epoch`/`queue` 按特性（`std`/`alloc`）分门别类挂出，实验性的 `skiplist` 不收编。
- `#[doc(inline)]` 让被重导出的子 crate 在文档里「内联展开」，呈现为一个统一的原生命名空间。
- `tests/subcrates.rs` 是一组**契约测试**，只走门面路径、靠「能编译通过」来钉住 API 表面不被破坏。
- `no_atomic.rs` 是 CI 生成的「无原子目标」名单，经符号链接被 `crossbeam-utils/build.rs` 消费，注入 `crossbeam_no_atomic` cfg，守护 `crossbeam::atomic` 始终可用」这一承诺的边界。

## 7. 下一步学习建议

本讲把「门面怎么搭」讲透了，但还没有真正动手写过并发代码。建议下一步：

1. **进入 u1-l4《作用域线程 scope 快速上手》**：以 `crossbeam::scope` 为入口，第一次动手写并发程序，体验门面路径下的真实用法——这也是 u2 单元「crossbeam-utils 并发原语基石」的预热。
2. **回头看 u1-l2 的特性连带链**：结合本讲看到的 `#[cfg(feature = "std")]`/`#[cfg(feature = "alloc")]`，再读一遍根 `Cargo.toml` 的 `[features]`，会有一拍即合的感觉。
3. **后续 u2-l3（AtomicCell）**：本讲反复提到的 `crossbeam::atomic::AtomicCell`，其实现细节（直接原子 vs 序列锁兜底）将在那里展开；届时你会更理解 `no_atomic.rs` 为什么要存在。
