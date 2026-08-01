# World trait —— 嵌入 Typst 的环境契约

## 1. 本讲目标

上一讲（u1-l1）我们建立了全局地图：`typst` crate 是顶层门面，`compile()` 接收一个 `&dyn World` 就能完成编译。本讲就专门回答那个被刻意悬置的问题——**`World` 到底是什么，宿主要为它提供什么？**

读完本讲，你应当能够：

- 说清 `World` trait 是**嵌入 Typst 编译器时唯一必须实现的环境接口**：编译器本身「只算不取」，所有外部资源都经 `World` 注入。
- 逐一说出 `World` 的 **7 个方法**各自提供什么：`library`（标准库）、`book`（字体簿）、`main`（主文件 id）、`source`（源码）、`file`（二进制）、`font`（字体数据）、`today`（日期）。
- 理解一个看似反常的设计：**为什么文件和字体的缓存职责放在 `World` 一侧，而不是编译器内部**——这和 `typst watch`、增量编译、语言服务器（LSP）三类场景直接相关。
- 认识 `WorldExt` 扩展 trait，以及 `world_impl!` 宏如何让 `Box<W>` / `Arc<W>` / `&W` 自动满足 `World`。

本讲仍是入门层，**不**展开 `compile` 主流程的内部循环（那是 u2 的事），只把「编译器与外部世界的边界」讲透。

## 2. 前置知识

承接 u1-l1 已建立的术语：门面（facade）、再导出、`compile`、`PagedDocument`。下面补充几个本讲要用到的概念：

- **trait（特征）**：Rust 中定义一组方法签名的接口。一个类型只要实现了这些方法，就说它「满足」这个 trait。`World` 就是一个 trait，宿主自己写一个结构体并实现这 7 个方法即可。
- **`&dyn World`（trait 对象）**：把「任意一个实现了 `World` 的类型」当作同一个引用类型来用。`compile(world: &dyn World)` 的含义是：我不关心你的 `World` 具体是什么类型，只要它满足契约即可。
- **`FileId`**：Typst 用来唯一标识一个文件的句柄（由「包 + 路径」组成）。编译器内部不直接碰文件路径字符串，而是拿着 `FileId` 找 `World` 要内容。
- **`LazyHash<T>`**：一种「构造时计算好哈希、之后只读」的智能包装。 Typst 用它包裹 `Library` 和 `FontBook`，因为这两个对象体积大、内容稳定、且要参与增量缓存校验。
- **comemo / 增量编译**：Typst 用 `comemo` 这个库做记忆化（memoization）——对相同输入的纯函数调用会被缓存。`World` 上有一个 `#[comemo::track]` 注解，正是为了让 `World` 的方法调用也能被纳入这套缓存体系。本讲只点到为止，细节留到进阶层。
- **`watch` / LSP 场景**：`typst watch` 是「文件一改动就重新编译」的长驻模式；语言服务器（LSP）是 IDE 背后常驻的编译服务。它们的共同点是**一次进程内要编译很多次**，因此缓存策略和「一次性编译」很不一样。

## 3. 本讲源码地图

本讲只看两个文件，且真正的定义集中在一个文件里：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [`crates/typst-library/src/lib.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs) | Typst 标准库 crate 的入口，也是**编译器核心类型定义**的归属地。 | `World` trait 定义、`world_impl!` 宏、`WorldExt`——这三者都定义在此。 |
| [`crates/typst/src/lib.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs) | 门面 crate 的全部源码。 | 只看 `compile` 如何**消费** `World`：签名里的 `&dyn World`、内部对 `world.library()` / `world.main()` / `world.source()` 的调用。 |

> 注意：`World` 虽然是编译器接口，但定义在 `typst-library`（而不是 `typst` 门面）里。这是因为 `typst` 通过 `pub use typst_library::*` 把它平铺再导出，所以外部使用者写 `typst::World` 即可，不必关心它物理上住在哪个 crate。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **`World` trait 定义**（7 个方法 + 缓存职责的设计取舍）
2. **`world_impl!` 宏**（让智能指针与引用自动满足 `World`）
3. **`WorldExt::range`**（在 `World` 之上的扩展方法）

### 4.1 `World` trait 定义

#### 4.1.1 概念说明

可以这样理解 `World`：**编译器是一台「只算不取」的机器**。它本身不去读硬盘、不去问系统时间、不去加载字体文件——这些「与外部世界打交道」的事全部外包给 `World`。`World` 就像是这台机器的**仓库管理员**：编译器要什么，就递交一个申请（比如一个 `FileId`），管理员把对应的货物（`Source` / `Bytes` / `Font`）递回来。

为什么要把边界划得这么干净？因为这样编译器就成了一个**关于 `World` 的纯函数**：

\[ \text{compile} : \text{World} \rightarrow \text{Document} \]

给定同一个 `World`，就得到同一个结果。这带来三个直接好处：

- **可测试**：测试时塞一个「假 `World`」，不必碰真实文件系统。
- **可复现**：把日期、字体、源码全部固定下来，编译结果就完全可复现。
- **可增量**：因为输入是 `World`，输出是文档，中间用 `comemo` 缓存；当 `World` 的某些方法返回值没变时，缓存就能复用。

#### 4.1.2 核心流程

`World` 一共要求实现 **7 个方法**。下表把它们按「提供什么 / 返回类型 / 谁来定内容」整理出来：

| 方法 | 提供什么 | 返回类型 | 内容由谁决定 |
| --- | --- | --- | --- |
| `library` | 标准库（所有内置函数、模块、样式） | `&LazyHash<Library>` | 宿主构建一次，通常长期不变 |
| `book` | 字体簿（所有可用字体的**元数据**索引） | `&LazyHash<FontBook>` | 宿主扫描字体集合得到 |
| `main` | 主源码文件的 id | `FileId` | 宿主指定「从哪个文件开始编译」 |
| `source` | 按 id 取一份源码 | `FileResult<Source>` | 宿主从文件系统/内存读取并解析 |
| `file` | 按 id 取一份二进制（图片、数据等） | `FileResult<Bytes>` | 宿主读取原始字节 |
| `font` | 按索引取一个字体**数据** | `Option<Font>` | 宿主按 `book` 里的索引加载 |
| `today` | 当前日期（可带时区偏移） | `Option<Datetime>` | 宿主按系统时间或固定值提供 |

> `book` 和 `font` 的分工是关键：`book` 只是一份「有哪些字体、各自什么属性」的**目录**（体积小、可哈希、用于筛选）；真正要用某个字体时，才用 `font(index)` 把它的**完整字形数据**加载进来。这种「先查目录、按需取货」的两段式设计，避免了把所有字体全量驻留内存。

`FileResult<T>` 就是 `Result<T, FileError>` 的别名，表示「可能因为文件不存在、无权限等原因失败」。注意 `book()` / `library()` / `main()` **不是** `Result`——它们被假定一定能拿到（拿不到说明宿主根本没初始化好，属于更上层的错误）。

#### 4.1.3 源码精读

`World` trait 的定义在 [`crates/typst-library/src/lib.rs:60-98`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L60-L98)。去掉文档注释后的骨架如下（示例代码：精简版，仅保留签名）：

```rust
#[comemo::track]
pub trait World: Send + Sync {
    fn library(&self) -> &LazyHash<Library>;
    fn book(&self) -> &LazyHash<FontBook>;
    fn main(&self) -> FileId;
    fn source(&self, id: FileId) -> FileResult<Source>;
    fn file(&self, id: FileId) -> FileResult<Bytes>;
    fn font(&self, index: usize) -> Option<Font>;
    fn today(&self, offset: Option<Duration>) -> Option<Datetime>;
}
```

几个要点：

- **`Send + Sync` 上界**：`World` 必须能安全地跨线程共享。因为 Typst 在布局等阶段会用多线程并行处理，`World` 会被多个线程同时读取。
- **`#[comemo::track]`**：这个注解会让 `comemo` 为 trait 生成「可追踪（tracked）」版本。`compile` 内部拿到的不是 `&dyn World` 本体，而是 `world.track()` 得到的 `Tracked<dyn World>`（见 [`crates/typst/src/lib.rs:79`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L79) 与 `compile_impl` 形参 [`crates/typst/src/lib.rs:99-103`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L99-L103)）。这样一来，对相同 `FileId` 的 `source()` 调用就能被缓存复用，这是增量编译的前提。细节留到 u2/u3。
- **`today` 的 `offset` 参数**：`None` 表示「取本地日期」，`Some(duration)` 表示「取 UTC 日期再按该偏移调整」。返回 `None` 时，Typst 脚本里的 `datetime` 函数会报错——这给了宿主「不提供日期」的合法选项（比如为了可复现构建而固定关闭时间）。

`World` 上方有一段很关键的文档注释（[`crates/typst-library/src/lib.rs:44-58`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L44-L58)），它直接说明了**缓存职责为何放在 `World` 一侧**：

```rust
/// All loading functions (`main`, `source`, `file`, `font`) should perform
/// internal caching so that they are relatively cheap on repeated invocations
/// with the same argument. ...
///
/// The compiler doesn't do the caching itself because the world has much more
/// information on when something can change. For example, fonts typically don't
/// change and can thus even be cached across multiple compilations (for
/// long-running applications like `typst watch`). Source files on the other
/// hand can change and should thus be cleared after each compilation. ...
```

这段话翻译过来就是：

- 加载类方法（`main`/`source`/`file`/`font`）**自己**要做内部缓存，使「用相同参数反复调用」很廉价。
- 编译器**不**替你缓存，因为 **`World` 比编译器更清楚「什么东西什么时候会变」**：
  - **字体**通常不变，可以**跨多次编译**缓存（`typst watch` 这种长驻进程里尤其划算）。
  - **源码**会变，应当**每轮编译后清空**缓存。
  - **高级客户端**（如 LSP）甚至可以**保留源码缓存**、用 `Source::edit` 就地修改，以获得更好的增量性能。

这就是本讲最核心的设计取舍：**把缓存策略下放给最了解变更时机的那一层（宿主），而不是由对文件系统一无所知的编译器来强行统一管理。**

再看 `font` 方法上的一段注释（[`crates/typst-library/src/lib.rs:82-88`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L82-L88)）：

```rust
/// Note that the index is not guaranteed to be in bounds of the font book
/// returned by this world's `book()` function. This is the case because
/// this function may be invoked with indices from an outdated or different
/// font book during incremental compilation validation.
```

它提醒实现者：`font(index)` 的 `index` **可能越界**，因为在增量编译的校验阶段，编译器可能拿着一个**过时的**字体簿索引来试探。所以返回类型才是 `Option<Font>`——越界就返回 `None`，编译器能优雅处理，而不是 `panic`。这是「增量编译」对接口形状的反向影响，很值得品味。

#### 4.1.4 代码实践

**实践目标**：用「源码阅读」方式，把 `compile_impl` 消费 `World` 的几个调用点找出来，体会「编译器只在真正需要时才向 `World` 索取」。

**操作步骤**：

1. 打开 [`crates/typst/src/lib.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs)，定位 `compile_impl`（[第 99 行起](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L99-L131)）。
2. 注意它的形参是 `world: Tracked<dyn World + '_>`（[第 100 行](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L100)）——即「被 comemo 追踪过的 `World`」。
3. 跟踪三处对 `World` 的调用：
   - `let library = world.library();`（[第 104 行](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L104)）——开头取一次标准库。
   - `let main = world.main();`（[第 117 行](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L117)）——取主文件 id。
   - `world.source(main)`（[第 118-120 行](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L118-L120)）——按 id 取主源码，失败时还会带上 `hint_invalid_main_file` 的友好提示。

**需要观察的现象**：注意 `book` / `font` / `file` / `today` 在 `compile_impl` 顶层**并没有**直接出现——它们是在后续求值、布局的更深处（由 `typst_eval::eval` 在 [第 123 行](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L123-L131) 被调用后）才按需触发的。

**预期结果**：你能说出「编译器入口只显式用了 `library` / `main` / `source` 三个方法，其余四个是惰性、按需触发的」。

> 待本地验证：如果你想确认 `book`/`font` 的调用点，可以在仓库根目录用搜索工具查 `world.book(` / `world.font(` / `.book()`，会发现它们分散在字体解析、布局等子 crate 中。本实践不要求运行。

#### 4.1.5 小练习与答案

**练习 1**：`library()`、`book()`、`main()` 三个方法的返回值都**不是** `Result`，而 `source()`、`file()` 是。为什么这样设计？

> **参考答案**：前三者是宿主在「开始编译之前」就该准备好的基础设置（标准库、字体目录、主文件指向）；如果它们拿不到，说明宿主根本没初始化好，属于上层错误，不该塞进编译流程当 `Result` 处理。而 `source`/`file` 是「按 id 现取」的，可能因为文件不存在、无权限等真正失败，所以是 `FileResult`。

**练习 2**：为什么 `font(index)` 返回 `Option<Font>` 而不是 `Font`，并且注释里特意提醒「index 可能越界」？

> **参考答案**：因为在增量编译的校验阶段，编译器可能用一个**过时的**字体簿索引去调用 `font()`，该索引在当前 `book()` 里可能已经越界。返回 `Option` 让越界情况返回 `None` 被优雅处理，而不是 `panic` 中断。

### 4.2 `world_impl!` 宏：让智能指针与引用自动满足 `World`

#### 4.2.1 概念说明

`compile` 的签名是 `compile<T>(world: &dyn World)`（[`crates/typst/src/lib.rs:74`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L74)）。但现实中，宿主的 `World` 实现常常被包在智能指针里传递：

- 用 `Arc<MyWorld>` 在多线程间共享；
- 用 `Box<dyn World>` 做堆上的 trait 对象；
- 或者只有一个临时的 `&MyWorld` 引用。

问题来了：`MyWorld` 实现了 `World`，但 `Arc<MyWorld>` **不会自动**实现 `World`——Rust 的 trait 不会「穿透」智能指针自动转发。每次要拿到 `&dyn World`，就得先解引用再取借用，写起来啰嗦。

`world_impl!` 宏就是为了消除这份啰嗦：它一次性为 `Box<W>`、`Arc<W>`、`&W` 三种「指针形态」生成 blanket 实现（对任意 `W: World` 都成立的实现），把每个方法调用转发给内部的 `W`。这样一来，你手里的 `Arc<MyWorld>` 本身**就**是一个 `World`，可以直接传给需要 `&dyn World` 的地方。

#### 4.2.2 核心流程

宏的展开逻辑很直白，对任意指针类型 `$ptr`：

```
对 W: World，为 $ptr 实现 World：
    每个方法(self, ...) -> 转发调用 self.deref().同名方法(...)
```

也就是「**全部方法逐一委托给解引用后的内部值**」。因为 `Box` / `Arc` / `&` 都实现了 `Deref` 且 `Deref::Target = W`，所以 `self.deref()` 能拿到内部的 `&W`，再调用其 `World` 方法即可。

调用三次宏，分别覆盖三种指针：

```rust
world_impl!(W for std::boxed::Box<W>);
world_impl!(W for std::sync::Arc<W>);
world_impl!(W for &W);
```

#### 4.2.3 源码精读

宏定义在 [`crates/typst-library/src/lib.rs:100-132`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L100-L132)（示例代码：精简版，省略部分方法）：

```rust
macro_rules! world_impl {
    ($W:ident for $ptr:ty) => {
        impl<$W: World> World for $ptr {
            fn library(&self) -> &LazyHash<Library> {
                self.deref().library()
            }
            fn book(&self) -> &LazyHash<FontBook> {
                self.deref().book()
            }
            // ... main / source / file / font / today 同样转发到 self.deref()
        }
    };
}

world_impl!(W for std::boxed::Box<W>);
world_impl!(W for std::sync::Arc<W>);
world_impl!(W for &W);
```

三处实例化见 [`crates/typst-library/src/lib.rs:134-136`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L134-L136)。

要点：

- 文件顶部导入了 `use std::ops::{Deref, Range};`（[第 29 行](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L29)），`Deref` 正是 `self.deref()` 所需。
- 用宏而不是手写三遍，是因为每个实现体都是「7 个方法逐一转发」的重复样板，宏让「加一个方法要改 4 个地方」缩成「改 trait + 改宏各一处」。
- 注意这是 `impl<$W: World> World for $ptr`，即「对任意实现了 `World` 的 `W`，指针 `$ptr` 也实现 `World`」——典型的 blanket impl 模式。

#### 4.2.4 代码实践

**实践目标**：理解「指针形态自动满足 `World`」带来的便利。

**操作步骤**：

1. 假设你写了一个 `struct MyWorld;` 并实现了 `World` 的 7 个方法。
2. 思考下面三种写法，哪几种能直接传给 `typst::compile::<PagedDocument>(...)`：

```rust
// 示例代码：仅说明调用形态，不可运行
let w1 = MyWorld { /* ... */ };
compile::<PagedDocument>(&w1);              // 用 &MyWorld

let w2: Arc<MyWorld> = Arc::new(MyWorld { /* ... */ });
compile::<PagedDocument>(&w2);              // 用 &Arc<MyWorld>

let w3: Box<dyn World> = Box::new(MyWorld { /* ... */ });
compile::<PagedDocument>(&w3);              // 用 &Box<dyn World>
```

**需要观察的现象 / 预期结果**：三种都能编译通过。因为 `&W`、`Arc<W>`、`Box<W>` 都被 `world_impl!` 实现了 `World`，再对它们取 `&` 得到 `&dyn World` 自然成立。关键是体会：**多亏了这套 blanket impl，宿主不必为了「我的 `World` 装在 `Arc` 里」而再写一遍转发代码。**

> 待本地验证：若你想亲手验证，可在仓库里找一个真实的 `World` 实现（如 `typst-cli` 里的 `SystemWorld`）看看它是被 `Box` 还是 `Arc` 包装后传给 `compile` 的。

#### 4.2.5 小练习与答案

**练习 1**：为什么不直接写 `impl<W: World + ?Sized> World for &W {}` 而非要用宏？

> **参考答案**：其实对 `&W` 这一种类型，手写 blanket impl 也可以；但 `World` 有 7 个方法，对 `Box<W>`、`Arc<W>`、`&W` **三**种类型就要手写 \(7 \times 3 = 21\) 个转发方法。用宏把「逐方法转发」的样板参数化，只需写一次模板、实例化三次，维护时改一处即可，避免大量重复。

**练习 2**：宏里的 `self.deref()` 为什么能拿到内部的 `&W`？

> **参考答案**：`Box<W>`、`Arc<W>`、`&W` 都实现了 `Deref`，且 `Deref::Target` 就是 `W`（或其引用）。`self.deref()` 因此返回 `&W`，随后调用 `&W` 上的 `World` 方法即可完成转发。

### 4.3 `WorldExt::range`：把 span 翻译成字节区间

#### 4.3.1 概念说明

除了 `World` 本身的 7 个方法，Typst 还提供了一个**扩展 trait** `WorldExt`，它给所有 `World` 实现额外加了一个方法 `range`。它的作用是：**把一个 `Span`（源码位置标记）翻译成「字节区间 `Range<usize>`」**，主要给诊断（diagnostic）定位使用——比如报错时要知道「这段错误对应源码的第几到第几个字节」。

为什么是「扩展」而不直接放进 `World`？因为 `range` 不是「宿主必须提供的外部资源」，而是**可以基于已有的 `source()` 方法算出来**。把它做成 `WorldExt` 的 blanket impl（对所有 `World` 自动提供），宿主就不用为实现 `World` 还额外再写一个 `range`。

#### 4.3.2 核心流程

`range` 的逻辑是对 `DiagSpan` 的三种形态做模式匹配：

```
range(span):
    match span 的种类:
        Detached（游离 span，不指向任何文件） → 返回 None
        Number { id, num, sub_range }（按行/节点定位）→
            先用 self.source(id) 取出该文件源码，
            再交给 Source::range(num, sub_range) 算出字节区间
        Range { id, range }（已经是字节区间）→ 直接返回 range
```

注意第二条分支会调用 `self.source(id)`——也就是说 `range` **本质上依赖 `World::source`**，这是它「派生自 `World` 而非独立」的又一佐证：取不到源码（`source(id)` 返回 `Err`）就拿不到区间，于是返回 `None`。

#### 4.3.3 源码精读

`WorldExt` 定义在 [`crates/typst-library/src/lib.rs:139-144`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L139-L144)，其 blanket 实现在 [`crates/typst-library/src/lib.rs:146-156`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L146-L156)：

```rust
pub trait WorldExt {
    /// Get the byte range for a span.
    ///
    /// Returns `None` if the `Span` does not point into any file.
    fn range(&self, span: impl Into<DiagSpan>) -> Option<Range<usize>>;
}

impl<T: World + ?Sized> WorldExt for T {
    fn range(&self, span: impl Into<DiagSpan>) -> Option<Range<usize>> {
        match span.into().get() {
            DiagSpanKind::Detached => None,
            DiagSpanKind::Number { id, num, sub_range } => {
                self.source(id).ok()?.range(num, sub_range)
            }
            DiagSpanKind::Range { id: _, range } => Some(range),
        }
    }
}
```

要点：

- `impl<T: World + ?Sized> WorldExt for T`：**任何**实现了 `World` 的类型（含 `?Sized` 的动态类型）自动获得 `range`。这就是「扩展方法」的 Rust 写法。
- `span: impl Into<DiagSpan>`：接受任何能转成 `DiagSpan` 的类型（比如旧的 `Span`），向前兼容多种 span 表示。
- `self.source(id).ok()?`：`source()` 失败时 `ok()` 把 `FileResult<Source>` 转成 `Option<Source>`，`?` 在 `None` 时直接让 `range` 返回 `None`。

> 名词解释——**`Span` / `DiagSpan`**：编译器在语法树每个节点上贴的「位置标签」。`DiagSpan` 是它的诊断用变体，有三种 `DiagSpanKind`：`Detached`（不依附任何文件）、`Number`（按节点编号/行定位）、`Range`（直接是字节区间）。`range()` 的职责就是把这三种统一归约成「字节区间或 `None`」。

#### 4.3.4 代码实践

**实践目标**：用一段「源码阅读型」追踪，理解 `range` 如何依赖 `World::source`。

**操作步骤**：

1. 阅读 [`crates/typst-library/src/lib.rs:146-156`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs#L146-L156) 的 `range` 实现。
2. 假设有两个 span：
   - span A 是 `DiagSpanKind::Number { id, num, sub_range }`，且 `id` 对应的文件**存在**。
   - span B 是 `DiagSpanKind::Number { id, .. }`，但 `id` 对应的文件**不存在**（`source(id)` 返回 `Err`）。
3. 分别推演 `world.range(spanA)` 与 `world.range(spanB)` 的返回值。

**需要观察的现象 / 预期结果**：

- span A：`self.source(id).ok()` 得到 `Some(source)`，再调用 `source.range(num, sub_range)` 返回某个 `Some(0..10)` 之类的区间。
- span B：`self.source(id).ok()` 得到 `None`，`?` 提前返回，`range` 返回 `None`。

这说明 `range` 的成功与否，**完全取决于 `World::source` 能否取到对应文件**。

> 待本地验证：本实践为推理型，无需运行。若想验证 `Source::range` 的细节，可去 `typst-syntax` 中查阅 `Source::range` 方法（不在本 crate 范围）。

#### 4.3.5 小练习与答案

**练习 1**：`range` 为什么不放进 `World` trait 本身，而要做成 `WorldExt`？

> **参考答案**：`range` 不是宿主必须「提供」的外部数据，而是可以**基于 `World::source` 派生计算**出来的。放进 `World` 会让每个宿主都被迫手写一遍；做成 `WorldExt` 的 blanket impl，则对所有 `World` 自动生效，宿主零负担。

**练习 2**：当 `span` 是 `DiagSpanKind::Range { id, range }` 时，`range()` 会去读文件吗？

> **参考答案**：不会。`Range` 变体里**已经携带了字节区间**，`range()` 直接 `Some(range)` 返回，根本不调用 `self.source(id)`。只有 `Number` 变体才需要读源码来换算区间。注意此分支里 `id` 被写成 `id: _`，表示「知道有 id 但用不上」。

## 5. 综合实践

**任务**：草拟一个最小的 `MyWorld` 结构体（不必完全可运行），列出实现 `World` 的 7 个方法分别需要持有的字段；并为每个字段写注释，说明在 `typst watch`（长驻重编译）场景下，它应当**跨编译缓存**还是**每轮清空**。

这是一个把本讲三个模块串起来的设计练习：你要综合「7 个方法各提供什么」「缓存职责在 `World` 一侧」「`watch` 与一次性编译的差异」三方面知识。

**参考骨架**（示例代码：仅示意字段与缓存策略，不可直接编译）：

```rust
// 示例代码：最小 MyWorld 草稿
struct MyWorld {
    // —— library(): 标准库。构建昂贵且几乎不变 ——
    // watch 场景：跨编译缓存（进程启动时构建一次）
    library: LazyHash<Library>,

    // —— book() + font(): 字体目录与字体数据。通常长期不变 ——
    // watch 场景：跨编译缓存（用户极少在 watch 中途换字体）
    book: LazyHash<FontBook>,
    fonts: Vec<Font>,

    // —— main(): 主文件 id。由宿主每轮指定 ——
    // watch 场景：每轮重新确定（用户可能切换主文件），但本身极轻量
    main_id: FileId,

    // —— source(): 源码。用户最常改动 ——
    // watch 场景：每轮清空后重读，确保拿到最新内容
    // （高级客户端如 LSP 可保留并用 Source::edit 就地更新，换取增量性能）
    sources: HashMap<FileId, Source>,

    // —— file(): 二进制资源（图片等）。改动感略低于源码 ——
    // watch 场景：可按「文件修改时间」缓存命中，未变则复用、变了才重读
    files: HashMap<FileId, Bytes>,

    // —— today(): 当前日期。影响可复现性 ——
    // watch 场景：按需刷新；若要可复现构建，可固定为一个值（不刷新）
    today: Option<Datetime>,
}
```

**操作步骤**：

1. 对照 4.1.2 的「7 个方法」表，逐个确认上面的字段是否能支撑对应方法的返回类型。
2. 给每个字段标注缓存策略，并**用一句话说明理由**（参考 4.1.3 里那段官方文档注释的思路）。
3. 思考一个进阶问题：如果要把这个 `MyWorld` 改造成「LSP 友好」，你会让 `sources` 字段的策略怎么变？

**预期结果**：你能产出一张「字段 → 支撑的方法 → watch 缓存策略 → 理由」的四列表，并说清「源码每轮清、字体跨编译留」这条核心原则。

> 待本地验证：本实践为设计型，无需运行。若想对照真实实现，可在 `typst-cli` 中阅读 `SystemWorld` 的字段与它的缓存注解，看官方是如何落地这套策略的。

## 6. 本讲小结

- `World` 是**嵌入 Typst 编译器时唯一必须实现的环境接口**；编译器「只算不取」，所有外部资源（库、字体、源码、文件、日期）都经 `World` 注入，使 `compile` 成为关于 `World` 的纯函数。
- `World` 共 **7 个方法**：`library`（标准库）、`book`（字体目录）、`main`（主文件 id）、`source`（源码）、`file`（二进制）、`font`（字体数据）、`today`（日期）；其中加载类方法返回 `Result`/`Option` 以表达失败与越界。
- **缓存职责下放给 `World`**：编译器不做缓存，因为它不知道「什么东西何时会变」；字体可跨编译缓存、源码应每轮清空、LSP 可就地编辑源码以获更好增量——策略由最了解变更时机的宿主决定。
- `#[comemo::track]` 让 `World` 方法调用可被增量缓存，`compile` 内部用的是 `world.track()` 得到的 `Tracked<dyn World>`。
- `world_impl!` 宏为 `Box<W>` / `Arc<W>` / `&W` 生成 blanket 实现，让智能指针与引用**自动**满足 `World`，免去手写样板转发。
- `WorldExt::range` 是基于 `World::source` 派生的扩展方法，把 `Span` 归约为字节区间，专供诊断定位使用。

## 7. 下一步学习建议

到这里，你已经知道「编译器需要宿主提供什么」。下一篇 **u1-l3《调用 compile() 完成一次编译》** 会把 `World` 真正接上 `compile`：精读 `pub fn compile<T>(world: &dyn World) -> Warned<SourceResult<T>>` 的签名、泛型 `T: Output` 如何决定产出类型（`PagedDocument` / `HtmlDocument`）、`Warned` 如何同时携带结果与告警，并给出「构造 `World` → 调用 `compile` → 处理 `Warned`」的完整骨架。

进阶方向（u2 之后）会回到 `#[comemo::track]` 这条线，讲透 `compile_impl` 的稳定化循环如何依赖 `Tracked<dyn World>` 做缓存校验；那时你对 `World` 的理解会从「接口契约」升级到「增量编译的输入锚点」。

建议继续阅读的源码：

- [`crates/typst-library/src/lib.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/lib.rs) 的 `World` / `world_impl!` / `WorldExt`（本讲核心）。
- `typst-cli` 中的 `SystemWorld`（真实宿主实现，对照本讲的缓存策略）。
- `crates/typst/src/lib.rs` 中 `compile` 与 `compile_impl` 的形参 `Tracked<dyn World>`（衔接 u1-l3）。
