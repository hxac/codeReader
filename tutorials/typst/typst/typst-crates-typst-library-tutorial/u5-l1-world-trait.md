# World trait 与资源加载

## 1. 本讲目标

本讲关注 Typst 编译器与「外部世界」之间的那一道接口——`World` trait。读完本讲，你应该能够：

- 说清 `World` trait 在编译流程中的角色：它是「编译环境」的抽象，编译器需要的一切资源都通过它获取。
- 逐个列出 `World` 的七个方法，并按「配置 / 资源 / 时间」三类理解它们的职责。
- 解释 `#[comemo::track]` 这个属性为什么是增量编译的根基，以及为什么 `Engine` 里持有的是 `Tracked<dyn World>`。
- 读懂 `world_impl!` 宏如何让 `Box<W>`、`Arc<W>`、`&W` 也能当作 `World` 使用。
- 理解 `WorldExt` 这个辅助 trait 提供的 `range` 方法，以及 `Source` / `Bytes` / `Font` 为何都是「廉价克隆」的引用计数类型。
- 讲明白一条贯穿全讲的设计取舍：**缓存职责交给 `World` 的实现者，而不是交给编译器本身**，并据此解释「字体可跨编译缓存、源文件却要每次清理」。

## 2. 前置知识

本讲默认你已经读过：

- **u1-l3 标准库的装配**：了解 `Library` 的七字段结构（`routines` / `global` / `math` / `styles` / `rules` / `std` / `features`），知道 `library()` 返回的 `&LazyHash<Library>` 就是它。
- **u2-l2 容器类型**：见过 `Bytes`（内部是 `Arc<LazyHash<dyn Bytelike>>`）和引用计数 + 写时复制的思路。

此外，先建立三个直觉：

1. **编译器不直接碰磁盘。** Typst 把「读文件、找字体、拿当前时间」这些会依赖运行环境的操作，全部抽象成一组 trait 方法。编译器只对 trait 编程，至于这些数据到底来自文件系统、内存、网络还是数据库，编译器不关心。
2. **增量编译靠记忆化。** Typst 支持「只重排变化部分」（`typst watch`、语言服务器都依赖它）。其底层 `comemo` 会记录每个计算的输入，输入没变就直接复用旧结果。`World` 恰好是这套记忆化最大的输入源之一，所以它被 `#[comemo::track]` 标注。
3. **「环境」与「上下文」是两件事。** `World` 是相对稳定的外部环境（一次编译里基本不变）；`Engine` 是活跃的编译上下文（携带本次编译的进度、警告、递归路由等）。本讲只讲 `World`，`Engine` 留给 u5-l2。

## 3. 本讲源码地图

本讲几乎全部围绕 crate 根文件 `src/lib.rs` 展开：

| 文件 | 作用 | 本讲用到什么 |
| --- | --- | --- |
| [src/lib.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs) | crate 根，定义 `World`、`world_impl!`、`WorldExt` | 本讲主体 |
| [src/engine.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs) | `Engine` 编译上下文 | 看 `Engine` 如何持有 `Tracked<dyn World>` |
| [src/diag.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/diag.rs) | 诊断与文件错误 | `FileResult` / `FileError` |
| [src/foundations/bytes.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/bytes.rs) | `Bytes` 字节视图 | 确认它是引用计数类型 |
| [src/text/font/mod.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/mod.rs) | `Font` 类型 | 确认它是 `Arc` 包装 |
| [src/text/font/book.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/book.rs) | `FontBook` 字体元数据 | 看 `book()` 返回什么 |

> 名词提醒：`FileId`、`Source`、`Span`、`DiagSpan` 这些类型来自 `typst-syntax` crate，本讲按需提及，不深入。

## 4. 核心概念与源码讲解

本讲按三个最小模块拆分：`World` trait 本身、`world_impl!` 宏、`WorldExt` 辅助 trait。三者关系是：`World` 是接口，`world_impl!` 让智能指针也能实现这个接口，`WorldExt` 在接口之上补一个便捷方法。

### 4.1 World trait：编译环境的抽象接口

#### 4.1.1 概念说明

把 Typst 编译想象成一个纯函数：输入是「源代码 + 字体 + 标准库 + 配置」，输出是「排版好的文档 / PDF / HTML」。`World` trait 就是这个纯函数的「输入盒」——它定义了编译器要取走哪些输入：

```
        ┌─────────────────────────── World ───────────────────────────┐
        │  library()      标准库（函数、元素、样式默认值）              │
        │  book()         字体目录（有哪些字体、各自叫什么）            │
        │  main()         主文件 id（编译入口是哪个文件）              │
        │  source(id)     某个 .typ 源文件（带语法树）                 │
        │  file(id)       任意二进制文件（图片、数据文件等）           │
        │  font(index)    字体目录中第 index 个字体                    │
        │  today(offset)  当前日期（供 datetime 函数使用）            │
        └─────────────────────────────────────────────────────────────┘
                            编译器只调用这些方法
```

编译器本身**不知道**这些数据从哪来。一个命令行实现可以从文件系统读，一个语言服务器可以从内存里维护的虚拟文件读，一个 Web 实现可以从网络拉取——只要它们都实现了 `World` 的这七个方法，编译器就能工作。

有两条关键设计约定，源码里写在 trait 上方的文档注释里（见 4.1.3）：

1. **`source` / `file` / `font` 这些加载方法应该自己做内部缓存**，使得「对同一参数反复调用」足够廉价。
2. **编译器不替你做缓存**，因为「什么时候该失效」这件事，`World` 实现者最清楚。

这两条直接决定了第 4.1.4 节的实践结论。

#### 4.1.2 核心流程

编译期，`World` 的调用链大致如下：

```
宿主程序 (typst-cli / LSP / 嵌入式)
   │
   │  1. 实现 World（提供 library/book/main/source/file/font/today）
   │  2. 调用编译入口
   ▼
编译驱动 (typst crate)
   │
   │  3. world.track()  →  Tracked<'_, dyn World>
   │  4. 构造 Engine { world, library, introspector, traced, sink, route }
   ▼
求值 / 收敛 / 排版 (typst-eval / typst-realize / typst-layout …)
   │
   │  5. 需要源代码 → engine.world.source(id)
   │     需要字体   → engine.world.font(index)
   │     需要数据   → engine.world.file(id)
   ▼
   comemo 记忆化：记录「这次计算用到了 source(42)、font(7)」
   下次编译若这些输入未变，直接复用旧结果 → 增量编译
```

第 3 步里的 `track()` 来自 `comemo`。`World` trait 上方的 `#[comemo::track]` 属性会把整个 trait 改造成「可被追踪」的形态：它的方法不再通过普通动态分派调用，而是经由一层 `Tracked` 包装，所有入参出参都被 `comemo` 记账。这正是增量编译能自动失效依赖的根基（详细机制见 u12-l2 性能与并发）。

对应在 `Engine` 结构体里，`world` 字段的类型就是被追踪后的 trait 对象，而不是裸的 `&dyn World`：

[src/engine.rs:17-36](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L17-L36) —— 注意第 20 行 `world: Tracked<'a, dyn World + 'a>`，以及第 26 行的注释说明 `library` 字段为何要「提前取出一次」：因为 `world.library()` 是一次 tracked 调用，而它被访问得极其频繁，直接缓存它的返回值能省掉追踪开销。

#### 4.1.3 源码精读

整段 `World` 定义（含文档与 `#[comemo::track]`）如下，位于 crate 根：

[src/lib.rs:44-98](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L44-L98) —— 这段定义了「排版发生的环境」，并约定加载方法需自带缓存、`Source`/`Bytes`/`Font` 均为廉价克隆的引用计数类型。

把七个方法分三类看最清楚：

**配置类（编译开始前就已确定）**

```rust
fn library(&self) -> &LazyHash<Library>;   // 标准库，见 u1-l3
fn book(&self) -> &LazyHash<FontBook>;      // 字体目录
fn main(&self) -> FileId;                   // 主文件的身份号
```

`library()` 返回的就是 u1-l3 讲的 `Library`（用 `LazyHash` 包装以缓存哈希值，方便 `comemo` 比对是否变化）。`book()` 返回的 `FontBook` 是「family 名 → 字体索引」的元数据表（见 [src/text/font/book.rs:11-16](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/book.rs#L11-L16)）。`main()` 返回的 `FileId` 是 Typst 内部标识文件的「虚拟路径号」（含包信息），不直接是操作系统路径。

**资源类（按需加载，需自行缓存）**

```rust
fn source(&self, id: FileId) -> FileResult<Source>;
fn file(&self, id: FileId) -> FileResult<Bytes>;
fn font(&self, index: usize) -> Option<Font>;
```

- `source()` 加载一个 `.typ` 源文件，返回带语法树的 `Source`。
- `file()` 加载任意二进制文件（图片、csv、json 等），返回 `Bytes`。文档明确要求：凡是 `source()` 能成功的 id，`file()` 也必须成功，可以用 `Bytes::from_string` 把已有 `Source` 零拷贝转成 `Bytes`。
- `font()` 按索引取字体。注意它的返回是 `Option<Font>` 而非 `FileResult`——文档解释：增量编译校验时，可能用「旧的或别的 `FontBook`」里的索引来调用，所以索引越界是合法情况，返回 `None` 即可（见 [src/lib.rs:82-88](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L82-L88)）。

`FileResult` 就是 `Result<T, FileError>`，错误种类（找不到 / 无权限 / 是目录 / 非 UTF-8 / 包未下载等）定义在诊断模块：

[src/diag.rs:602-626](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/diag.rs#L602-L626) —— `FileResult<T> = Result<T, FileError>`，`FileError` 是编译期能向用户报告的「文件相关错误」。

**时间类**

```rust
fn today(&self, offset: Option<Duration>) -> Option<Datetime>;
```

返回当前日期。`offset` 为 `None` 时取本地日期，为 `Some(d)` 时取「UTC + 该偏移」的日期。返回 `None` 会让 Typst 的 `datetime` 函数报错——这让宿主可以主动控制「我不提供时间」（例如纯函数的离线编译为了可复现性，故意不给时间）。

最后强调那条属性：

[src/lib.rs:59-60](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L59-L60) —— `#[comemo::track]` 标注让 `World` 成为可追踪 trait，这是 `Engine` 能持有 `Tracked<dyn World>`、以及增量编译自动失效依赖的前提。

#### 4.1.4 代码实践

**实践目标**：亲手「设计」一个最小 `World` 实现，理解它的最低要求与缓存取舍。这是源码阅读 + 设计型实践，不需要运行完整编译器。

**操作步骤**：

1. 打开 [src/lib.rs:44-98](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L44-L98)，逐行读 `World` 的文档注释和七个方法签名。
2. 在纸上或一个草稿文件里，写一个最小实现的骨架（**示例代码**，非项目原有代码）：

   ```rust
   // 示例代码：最小 World 实现的骨架设计
   struct MyWorld {
       library: LazyHash<Library>,        // 编译前用 Library::build() 造好
       book: LazyHash<FontBook>,          // 字体目录，通常全局只造一次
       fonts: Vec<Font>,                  // index → Font，跨编译可复用
       sources: HashMap<FileId, Source>,  // 本次编译用到的源文件
       main_id: FileId,
   }

   impl World for MyWorld {
       fn library(&self) -> &LazyHash<Library> { &self.library }
       fn book(&self) -> &LazyHash<FontBook> { &self.book }
       fn main(&self) -> FileId { self.main_id }
       fn source(&self, id: FileId) -> FileResult<Source> {
           self.sources.get(&id).cloned().ok_or(FileError::NotFound(/*…*/))
       }
       fn file(&self, id: FileId) -> FileResult<Bytes> { /* 读磁盘或查缓存 */ }
       fn font(&self, index: usize) -> Option<Font> { self.fonts.get(index).cloned() }
       fn today(&self, _offset: Option<Duration>) -> Option<Datetime> { None }
   }
   ```

3. 对照文档注释，检查你的实现是否满足「凡 `source` 成功的 id，`file` 也成功」这一约束。

**需要观察的现象**：

- `source` / `font` 的返回类型（`Source` / `Font`）虽然是值，但因为内部是引用计数，`.cloned()` 只是增加一个引用计数，**不复制底层数据**。
- `today` 可以直接返回 `None`，这是合法且常见的做法（用于可复现的离线编译）。

**预期结果**：你能列出实现一个最小 `World` 必须提供的恰好七个方法，并能说出哪些数据适合长期缓存、哪些需要每次清理（见下）。

**为何字体可跨编译缓存、源文件却要每次清理**：回到 [src/lib.rs:51-58](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L51-L58) 的注释——「编译器自己不做缓存，因为 `World` 更清楚什么时候会变」。字体几乎不变，`typst watch` 这类长期运行的应用可以把字体缓存跨多次编译保留；源文件会被用户编辑，因此每次编译后应清理，或像语言服务器那样保留并在原地 `Source::edit` 以获得更好的增量性能。一句话：**失效策略是数据相关的，只有 `World` 实现者拥有「这条数据会不会变」的信息，所以缓存决策权下放给它**。

> 说明：本实践是设计/阅读型，未真正调用 `cargo build`。若你想验证骨架能否编译，需把 `Library`、`FontBook` 等依赖补齐；运行结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`font()` 为什么返回 `Option<Font>` 而不是 `FileResult<Font>`？

**参考答案**：因为增量编译校验阶段，`comemo` 可能用一个「旧的或不同的 `FontBook`」产生的索引来调用 `font(index)`，此时索引可能越界。这不是错误，而是正常的「输入可能不一致」情况，返回 `None` 表示「没这个字体」即可。注释见 [src/lib.rs:82-88](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L82-L88)。

**练习 2**：`file()` 的文档为什么要求「凡 `source()` 成功的 id，`file()` 也成功」？

**参考答案**：编译器在处理 `#include` / `#read` 等时，对同一个文件 id 可能既需要 `Source`（作为代码）又需要 `Bytes`（作为数据）。为保证两者一致、避免「能当代码却读不出字节」的矛盾，`World` 实现必须保证 `source` 可达的 id 在 `file` 也可达；注释还贴心指出可用 `Bytes::from_string` 把已有 `Source` 零拷贝转成 `Bytes`，见 [src/lib.rs:75-80](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L75-L80)。

**练习 3**：如果把 `#[comemo::track]` 从 `World` 上去掉，会发生什么？

**参考答案**：`World` 将无法再以 `Tracked<dyn World>` 形式被 `Engine` 持有，`comemo` 也就无法追踪「某次计算读取了哪些 source/font/file」，增量编译的自动依赖失效机制将失效——每次都得全量重算。该属性是增量编译能跑起来的前提（见 [src/lib.rs:59](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L59) 与 [src/engine.rs:20](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L20)）。

### 4.2 world_impl! 宏：让智能指针自动实现 World

#### 4.2.1 概念说明

`World: Send + Sync` 是一个 `dyn`-safe 的 trait，但 Rust 里我们经常用智能指针持有它：`Box<W>`（独占堆）、`Arc<W>`（共享堆）、`&W`（借用）。如果每次都要为这三种包装手写一遍「转发实现」，会非常啰嗦。

`world_impl!` 宏解决的就是这个样板代码问题：**只要 `W: World`，那么 `Box<W>`、`Arc<W>`、`&W` 也自动是 `World`**，所有方法都透明地转发给内部的 `W`。

这背后的能力是 `Deref`：`Box<T>`、`Arc<T>`、`&T` 都实现了 `Deref`，宏里每个方法体都是一句 `self.deref().同名方法()`，把调用「穿透」到真正的 `W` 上。

#### 4.2.2 核心流程

宏的展开过程可以用伪代码描述：

```
宏调用：world_impl!(W for SomeSmartPointer<W>)
  │
  │  对 trait 的 7 个方法，各自生成：
  │      fn method(&self, ...) -> ... {
  │          self.deref().method(...)   // 透传给底层 W
  │      }
  ▼
得到：impl<W: World> World for SomeSmartPointer<W> { … }
```

然后在文件里用三行实例化它，分别覆盖 `Box`、`Arc`、`&`：

```rust
world_impl!(W for std::boxed::Box<W>);
world_impl!(W for std::sync::Arc<W>);
world_impl!(W for &W);
```

这样，无论上层把 `World` 放在哪种智能指针里，编译器代码里只需面向 `dyn World` 编程，调用处用一个 `.track()` 就能得到 `Tracked<dyn World>`。

#### 4.2.3 源码精读

宏定义位于 trait 紧接其后：

[src/lib.rs:100-132](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L100-L132) —— 定义 `world_impl!` 宏，对 `World` 的全部七个方法各生成一句 `self.deref().xxx()` 的透传实现。

关键片段（只列前两个方法示意，其余同理）：

```rust
macro_rules! world_impl {
    ($W:ident for $ptr:ty) => {
        impl<$W: World> World for $ptr {
            fn library(&self) -> &LazyHash<Library> { self.deref().library() }
            fn book(&self) -> &LazyHash<FontBook>   { self.deref().book() }
            // …main / source / file / font / today 同样转发…
        }
    };
}
```

注意 `use std::ops::{Deref, Range};` 这行 import（[src/lib.rs:29](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L29)）里的 `Deref` 正是为此服务的。

接着是三处实例化：

[src/lib.rs:134-136](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L134-L136) —— 为 `Box<W>`、`Arc<W>`、`&W` 三种智能指针各实例化一次宏，使它们都成为 `World`。

#### 4.2.4 代码实践

**实践目标**：用宏展开的视角，确认 `Arc<W>` 上调用 `World` 方法确实只是穿透到底层 `W`。

**操作步骤**：

1. 读 [src/lib.rs:100-132](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L100-L132) 的宏体，确认 7 个方法**全部**都是 `self.deref().方法()`，没有任何额外逻辑。
2. 在脑中把 `world_impl!(W for std::sync::Arc<W>)` 展开：它会生成 `impl<W: World> World for Arc<W> { fn source(&self, id) -> FileResult<Source> { self.deref().source(id) } … }`。
3. 想象调用点 `let world: Arc<MyWorld>; … world.source(id)`——实际执行的是 `(*world).source(id)`。

**需要观察的现象**：

- 透传实现**不引入新的缓存、不做校验**——任何「智能指针层」的 `World` 与底层 `World` 行为完全一致。
- 因此，如果你想要缓存，必须在**底层 `W` 的实现里**做，套一层 `Arc` 不会凭空获得缓存能力。

**预期结果**：你理解了「`World` 可被任何常见智能指针持有」完全来自这三个宏实例化，且这一层是零开销的纯转发。

#### 4.2.5 小练习与答案

**练习 1**：为什么宏里用的是 `self.deref()` 而不是直接 `(*self)`？

**参考答案**：两者等价（`*self` 也会触发 `Deref`），显式写 `self.deref()` 是为了读起来更清楚地表达「我在做 deref 转发」。统一成 `.deref().方法()` 让七个方法体形式一致、便于宏批量生成。`Deref` 在 [src/lib.rs:29](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L29) 引入。

**练习 2**：如果我自定义了一个智能指针 `struct MyBox<T>(T)` 并实现了 `Deref`，它能用这个宏吗？

**参考答案**：能。只要 `MyBox<W>` 满足 `Deref<Target = W>` 且 `W: World`，就可以再写一行 `world_impl!(W for MyBox<W>);` 来获得 `World` 实现。这正是宏用 `$ptr:ty` 泛型化指针类型的设计意图。

### 4.3 WorldExt：span 反查与廉价克隆

#### 4.3.1 概念说明

`WorldExt` 是一个对**所有** `World` 实现自动生效的辅助 trait（blanket impl）：它给 `World` 补了一个 `range` 方法，用于把一个 span（代码位置标记）翻译成「文件里的字节区间」。

为什么需要它？诊断信息、调试定位都依赖 span。Typst 的 span 可以是「数字坐标」（指向某个 `Source` 里的节点 + 子区间）或「直接的字节区间」。要把前者还原成字节区间，就必须回到对应的 `Source` 文件去查——而拿 `Source` 正是 `World::source()` 的事。于是 `WorldExt::range` 把「span → 找文件 → 算字节区间」这一串逻辑封成一句调用。

同时，这一节顺带落实 `World` 文档里反复强调的「廉价克隆」承诺，确认三个资源类型确实是引用计数的：

- `Source`（来自 `typst-syntax`，文档明确其引用计数，见 [src/lib.rs:48](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L48)）。
- `Bytes`：内部 `Arc<LazyHash<dyn Bytelike>>`，见 [src/foundations/bytes.rs:46](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/bytes.rs#L46)（u2-l2 已详述）。
- `Font`：内部 `Arc<FontInner>`，注释直言「cheap to clone and hash」，见 [src/text/font/mod.rs:39-41](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/mod.rs#L39-L41)。

正因为这三者克隆成本接近一次原子加，`World` 的方法才敢直接返回值类型而非引用，又不必担心拷贝大开销。

#### 4.3.2 核心流程

`range` 把 span 还原成字节区间的分派逻辑：

```
range(span)
   │
   │  span.into().get()  得到 DiagSpanKind
   ▼
 ┌───────────────┬─────────────────────────────┬───────────────────────┐
 │ Detached      │ Number { id, num, sub_range}│ Range { range }       │
 │ (脱离的 span) │ (指向某 Source 的节点)      │ (已是字节区间)        │
 ├───────────────┼─────────────────────────────┼───────────────────────┤
 │ 返回 None     │ self.source(id).ok()?       │ 直接返回 Some(range)  │
 │               │   .range(num, sub_range)    │                       │
 └───────────────┴─────────────────────────────┴───────────────────────┘
```

三种情况对应 span 的三种来源：脱离任何文件（返回 `None`）、指向某 `Source` 节点（去 `World` 取该文件再算区间）、已经是字节区间（直接用）。

#### 4.3.3 源码精读

`WorldExt` 的定义与 blanket 实现：

[src/lib.rs:138-156](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L138-L156) —— 定义 `WorldExt` 辅助 trait 并用 `impl<T: World + ?Sized> WorldExt for T` 让所有 `World` 自动获得 `range` 方法。

精读 `range` 方法体：

```rust
fn range(&self, span: impl Into<DiagSpan>) -> Option<Range<usize>> {
    match span.into().get() {
        DiagSpanKind::Detached => None,                              // 脱离文件
        DiagSpanKind::Number { id, num, sub_range } => {
            self.source(id).ok()?.range(num, sub_range)              // 回 World 取 source
        }
        DiagSpanKind::Range { id: _, range } => Some(range),         // 已是字节区间
    }
}
```

注意第二分支里那个 `self.source(id)`——这就是 `WorldExt` 反过来调用 `World::source` 的地方：辅助方法本质上是对 `World` 既有方法的组合封装。`?` 在 `source` 失败（返回 `Err`）时直接让整个 `range` 返回 `None`，表示「找不到这个 span 对应的字节区间」。

`Range` 来自 `std::ops`（[src/lib.rs:29](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L29)），就是普通的半开区间 `start..end`。

#### 4.3.4 代码实践

**实践目标**：把「缓存职责分工」与「引用计数廉价克隆」两条主线串起来，确认你理解 `World` 的性能模型。

**操作步骤**：

1. 打开 [src/lib.rs:44-58](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L44-L58)，逐句读这段文档注释，圈出三处关键断言：
   - 「加载方法应做内部缓存」；
   - 「`Source`/`Bytes`/`Font` 都是引用计数、廉价克隆」；
   - 「编译器不做缓存，因为 world 更清楚何时会变；字体可跨编译缓存，源文件应每次清理」。
2. 分别打开下面三处，验证「引用计数」属实：
   - `Bytes`：[src/foundations/bytes.rs:46](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/bytes.rs#L46) —— `Arc<LazyHash<dyn Bytelike>>`。
   - `Font`：[src/text/font/mod.rs:39-41](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/mod.rs#L39-L41) —— `Font(Arc<FontInner>)`，注释「cheap to clone and hash」。
   - `WorldExt::range` 里 `self.source(id).ok()?`：[src/lib.rs:146-156](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L146-L156) —— 它拿到 `Source` 后随即 `.range(...)`，并不持有，正好呼应「source 调用要廉价」。
3. 写一段话回答：既然编译器不做缓存，为什么 `source`/`file`/`font` 反复调用还能廉价？（答案要点：宿主在 `World` 实现里缓存 + 返回值是引用计数类型。）

**需要观察的现象**：

- `Source` / `Bytes` / `Font` 的 `.clone()` 都不复制底层大数据，只增加一个 `Arc` 引用计数。
- `WorldExt::range` 之所以敢随手调 `source(id)` 而不顾忌开销，正是因为上面两点。

**预期结果**：你能自洽地解释 Typst 的资源缓存模型——「缓存归宿主管、廉价克隆归类型设计、增量失效归 `comemo`」，三者各司其职。

#### 4.3.5 小练习与答案

**练习 1**：`WorldExt` 为什么用 blanket impl（`impl<T: World + ?Sized> WorldExt for T`）而不是让每个 `World` 自己实现？

**参考答案**：因为 `range` 的实现完全由 `World::source` 推导得出，没有任何「实现者自定义」的空间；用 blanket impl 让所有 `World`（包括 4.2 里那三种智能指针形式的 `World`）自动免费获得这个方法，免去重复实现。见 [src/lib.rs:146](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L146)。

**练习 2**：`WorldExt::range` 里，`DiagSpanKind::Number` 分支如果 `self.source(id)` 返回 `Err`，会发生什么？

**参考答案**：`.ok()?` 会把 `Err` 转成 `None` 并提前返回，整个 `range` 返回 `None`，表示「无法定位该 span 的字节区间」。这是合理的降级——找不到文件就不报位置，而不是抛错中断。

**练习 3**：`Font` 的文档说它「cheap to clone **and hash**」，为什么 `hash` 也要廉价？

**参考答案**：`Font` 经由 `World::font` 返回，是 `comemo` 追踪的输入之一；`comemo` 判断「输入是否变化」要计算并比对哈希，若 `Font` 的哈希是 O(n) 的，每次增量校验都要重算整个字体数据的哈希，代价极高。`Arc` 包装让指针相等可直接判「同一字体」，配合 `Font` 派生的 `Hash` 使哈希成本接近 O(1)。这与 u2-l2 里 `LazyHash` 缓存哈希的动机一致。

## 5. 综合实践

**任务**：把本讲三个最小模块串起来，为「一个最小可用的 `World`」写一份设计说明文档（不要求可运行，重在理解）。

要求涵盖：

1. **字段设计**：列出你的 `World` 结构体需要哪些字段（提示：`library`、`book`、`main_id`，以及分别存放 `sources` / `files` / `fonts` 的容器）。
2. **七个方法的实现策略**：逐个说明它从哪个字段取数据，以及是否/如何缓存。重点回答：
   - `source()` 与 `file()` 用什么数据结构缓存？（如 `HashMap<FileId, Source>`）
   - `font()` 为什么可以直接返回 `self.fonts.get(index).cloned()`，而 `font()` 的 `Option` 返回值意味着什么？
   - `today()` 你打算返回什么？为什么？（提示可复现性）
3. **缓存生命周期**：明确写出哪些字段「跨编译保留」（如 `fonts`、`book`、`library`），哪些「每次编译后清理」（如 `sources`、`files`），并引用 [src/lib.rs:51-58](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L51-L58) 作为依据。
4. **持有方式**：说明你会用 `Arc<MyWorld>` 还是 `Box<MyWorld>` 交给编译器，并解释为什么这无所谓（引用 4.2 的 `world_impl!`）。
5. **追踪接入**：解释在交给 `Engine` 之前，`World` 要经历一次 `.track()`（变成 `Tracked<dyn World>`），并说明这步对增量编译的意义（引用 [src/engine.rs:20](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L20)）。

**交付物**：一份不超过一页的中文设计说明，包含一个结构体字段表和一段「缓存生命周期」说明。

> 这是源码阅读 + 设计型综合实践；若你想把骨架真正编译通过，需要补齐 `Library::build()`、`FontBook` 构造等依赖，运行结果**待本地验证**。

## 6. 本讲小结

- `World` trait（[src/lib.rs:44-98](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L44-L98)）是「编译环境」的抽象，编译器需要的一切——标准库 `library()`、字体目录 `book()`、主文件 `main()`、源文件 `source()`、二进制文件 `file()`、字体 `font()`、当前日期 `today()`——都从它获取；它被分为配置 / 资源 / 时间三类方法。
- `#[comemo::track]`（[src/lib.rs:59](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L59)）让 `World` 成为可追踪 trait，`Engine` 因此能持有 `Tracked<dyn World>`（[src/engine.rs:20](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L20)），这是增量编译自动失效依赖的根基。
- **缓存职责下放给 `World` 实现者**：编译器不做缓存，因为只有 `World` 知道数据何时会变——字体可跨编译缓存，源文件应每次清理或原地 `Source::edit`（[src/lib.rs:51-58](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L51-L58)）。
- `world_impl!` 宏（[src/lib.rs:100-136](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L100-L136)）靠 `Deref` 让 `Box<W>`、`Arc<W>`、`&W` 透明转发地自动实现 `World`，是零开销的样板代码消除。
- `WorldExt`（[src/lib.rs:138-156](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L138-L156)）通过 blanket impl 给所有 `World` 补一个 `range` 方法，把 span 还原成字节区间，必要时回 `World::source()` 取文件。
- `Source` / `Bytes` / `Font` 都是引用计数类型（`Bytes` 见 [src/foundations/bytes.rs:46](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/bytes.rs#L46)、`Font` 见 [src/text/font/mod.rs:39-41](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/mod.rs#L39-L41)），克隆近乎免费，所以 `World` 的方法能放心返回值类型。

## 7. 下一步学习建议

- **u5-l2 Engine、Route、Sink 与 Traced**：本讲只点了 `Engine` 的 `world` 字段，下一讲完整拆解 `Engine` 这个活跃编译上下文——`Route` 如何防循环导入、`Sink` 如何收集警告与延迟错误、`Traced` 如何跟踪被检视的 span。
- **u5-l3 诊断系统**：`WorldExt::range` 返回的字节区间最终喂给诊断显示；下一讲讲 `bail!` / `error!` / `warning!` 与 `SourceResult` / `FileResult` 的全景。
- **u5-l4 Routines 与 crate 分离机制**：理解了「编译器靠 `World` 拿数据」之后，再看「编译器靠 `Routines` 函数指针表调行为」，两者共同构成 `typst-library` 不依赖行为 crate 的设计闭环。
- **u12-l2 性能与并发**：深入 `comemo::track` / `tracked` 的记忆化原理、`LazyHash` 为何被大量使用、`singleton!` 单例缓存，把本讲的「增量编译根基」讲透。
- 想看一个真实的 `World` 实现，可去仓库根的 `crates/typst-cli`（命令行宿主）与语言服务器相关 crate 里搜索 `impl World for`，对照本讲的「最小实现」理解生产级实现如何组织缓存（这部分在 `typst-library` 之外，作为延伸阅读）。
