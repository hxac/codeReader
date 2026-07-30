# FileStore 与 FileLoader 抽象

## 1. 本讲目标

本讲进入 typst-kit 的「文件与源码加载子系统」。学完后你应该能够：

1. 说清楚 `FileStore` 解决了什么问题——为什么实现 `World` 时不能「每次被问到就读一次盘」。
2. 掌握 `FileStore` 对外的四个入口：`source()`、`file()`、`dependencies()`、`reset()`，以及它们各自被设计出来的动机。
3. 理解 `FileLoader` trait 的职责：它是唯一一个「可插拔」的字节来源接口，决定「字节从哪里来」。
4. 看懂 typst-cli 如何用一个 `FileStore<SystemFiles>` 把这两个抽象接上 `World` 契约。
5. 亲手写一个返回固定字节的 `FileLoader`，并用它驱动一次完整的「读源码 / 读文件 / 列依赖」流程。

本讲只讲**抽象层**。`FileSlot` 内部的三态状态机放在 u3-l2 精读；`SystemFiles` 与 `FsRoot` 的磁盘路径解析放在 u3-l3 精读。本讲把 `FileSlot` 当作一个「会缓存、会复用缓冲」的黑盒来对待。

## 2. 前置知识

### 2.1 World 契约里和「文件」相关的两个回调

在 u1-l3 我们已经建立了 `World` trait 的全景。和本讲直接相关的是这两个带参数的回调：

```rust
// typst-library/src/lib.rs
fn source(&self, id: FileId) -> FileResult<Source>;
fn file(&self, id: FileId) -> FileResult<Bytes>;
```

[typst-library/src/lib.rs:60](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L60) 定义了 `World` trait，其中 [source 在 L73](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L73)、[file 在 L80](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L80)。

它们的特点是：

- **按需提问**：编译器遇到 `#include "utils.typ"` 才会问「给我 utils.typ 的 Source」；遇到 `image("logo.png")` 才会问「给我 logo.png 的 Bytes」。这和 `library()`/`book()` 这种「无参数、一次性」的回调不同。
- **会被反复问**：增量编译、多线程求值时，同一个文件可能在一次编译里被问到很多次。

### 2.2 三个关键类型（u1-l3 已建立，这里复习）

| 类型 | 含义 | 定义位置 |
|------|------|----------|
| `FileId` | 与磁盘路径解耦的「虚拟文件标识」，带一个 `root()`（`Project` 或 `Package`）和一个 `vpath()`（根内的虚拟路径） | typst-syntax |
| `FileResult<T>` | 就是 `Result<T, FileError>` | [typst-library/src/diag.rs:602](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/diag.rs#L602) |
| `Source` | 一段 UTF-8 文本 + 行号/列号索引，用于解析 Typst 代码和定位错误 | typst-syntax |
| `Bytes` | 原始字节，支持零拷贝共享 | typst-library |

一句话区分 `Source` 与 `Bytes`：`.typ` 文件要被「读懂」，所以读成 `Source`；图片、字体、二进制数据只被「搬运」，所以读成 `Bytes`。

## 3. 本讲源码地图

| 文件 | 在本讲的作用 |
|------|------------|
| `crates/typst-kit/src/files.rs` | **核心**。定义 `FileStore`、`FileLoader` trait、`FileSlot`，以及一个测试用的 `TestLoader`。本讲几乎所有代码引用都来自这里。 |
| `crates/typst-cli/src/world.rs` | **装配实例**。typst-cli 的 `SystemWorld` 把 `FileStore<SystemFiles>` 接到 `World` 上，是理解抽象如何被「用起来」的最佳样本。 |
| `crates/typst-library/src/lib.rs` | `World` trait 定义，复习 `source`/`file` 契约。 |

> 提示：本讲引用的 `files.rs` 在 typst-kit 中是**常驻代码**（不被 feature 开关门禁），所以无需启用任何 feature 即可使用 `FileStore` / `FileLoader`。只有 `SystemFiles` / `FsRoot` 需要 `system-files` 特性（见 u3-l3）。

## 4. 核心概念与源码讲解

### 4.1 FileStore：按 FileId 缓存的文件容器

#### 4.1.1 概念说明

先想一个最朴素的 `World` 实现：

```rust
fn source(&self, id: FileId) -> FileResult<Source> {
    let bytes = std::fs::read(id 对应的磁盘路径)?;  // 每次都读盘！
    Ok(Source::new(id, String::from_utf8(bytes)?))
}
```

这能跑，但有两个问题：

1. **没有缓存**：编译器问 100 次同一个文件，就读 100 次盘。增量编译时这是灾难。
2. **来源写死了**：字节只能来自磁盘。如果你想做语言服务器（内存里编辑中的文件）、做测试（返回固定字节）、做沙箱（禁止直接读盘），就无处下手。

typst-kit 的解法是把这两件事拆成**两层**：

- **缓存层 `FileStore`**：负责「记住读过的文件」，对外只暴露 `source()` / `file()`。它**不关心字节从哪来**。
- **来源层 `FileLoader`**：一个只有一个方法的 trait，负责「按 `FileId` 给我字节」。它是**唯一可插拔**的部分。

`FileStore` 是泛型的：`FileStore<L>`，`L` 就是你选的 `FileLoader`。换来源 = 换泛型参数，缓存逻辑一行都不用改。这正是「缓存与策略分离」的经典设计。

#### 4.1.2 核心流程

`FileStore` 的结构非常薄，真正的活都在一个私有助手 `slot()` 里：

```
World::source(id) / World::file(id)
        │  （typst-cli 直接转发）
        ▼
FileStore::source(id) / FileStore::file(id)
        │
        ▼
FileStore::slot(id, 闭包)          ← 所有读取的唯一入口
        │  ① 锁住 self.slots (Mutex)
        │  ② map.entry(id).or_default() 取出/新建该 id 的 FileSlot
        ▼
FileSlot::source(loader, id)       ← 黑盒，详见 u3-l2
FileSlot::file (loader, id)
        │  命中缓存 → 直接 clone 返回（不再调 loader）
        │  未命中   → loader.load(id) 取字节 → 推进状态
        ▼
Source / Bytes
```

关键点：**`slot()` 用 `entry(id).or_default()` 保证「同一个 FileId 永远对应同一个 FileSlot」**，这是缓存命中语义的基石。整个读取过程（含 `loader.load()`）都在持锁状态下进行，所以同一时刻只有一个线程在读文件——牺牲了并发来换取实现的简单与正确。

记一笔缓存收益：设一次编译里某文件被访问 \(k\) 次，磁盘 IO 只发生 1 次：

\[
\text{IO 次数}=\bigl|\{\,id : id\ \text{被访问}\,\}\bigr|\quad\neq\quad\text{总访问次数}
\]

其余 \(k-1\) 次只是从缓存里 `clone`（`Bytes`/`Source` 的 clone 都是廉价引用计数拷贝）。

#### 4.1.3 源码精读

先看 `FileStore` 的定义与构造：

[files.rs:19-39](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L19-L39) 是 `FileStore` 的文档注释与结构体定义。这段文档说得很直白：它是「为大多数客户端准备的恰当抽象层级」——如果你只想「按需喂字节」，用它就对了；只有语言服务器这种需要自己管理 Source 生命周期的场景才需要绕过它。

```rust
#[derive(Default)]
pub struct FileStore<L> {
    loader: L,
    slots: Mutex<FxHashMap<FileId, FileSlot>>,
}
```

- `loader: L` —— 你塞进来的字节来源。
- `slots: Mutex<FxHashMap<FileId, FileSlot>>` —— 缓存表。键是 `FileId`，值是 `FileSlot`（u3-l2 精读的状态机）。用 `parking_lot::Mutex` 而非 `RefCell`，因为 `World` 必须是 `Send + Sync`（typst 用 rayon 多线程编译）。

[files.rs:41-48](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L41-L48) 是构造与取用：

```rust
pub fn new(loader: L) -> Self {
    Self { loader, slots: Mutex::new(FxHashMap::default()) }
}
```

注意 `new()` 之后 `slots` 是**空的**——不做任何预加载，纯懒加载，第一次访问某 `id` 才会插入条目。`loader()` / `loader_mut()` / `into_loader()` 分别提供只读、可变、所有权抽取三种访问 loader 的方式。

再看唯一的私有读取入口 `slot()`：

[files.rs:118-125](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L118-L125)：

```rust
fn slot<F, T>(&self, id: FileId, f: F) -> FileResult<T>
where
    F: FnOnce(&mut FileSlot) -> FileResult<T>,
{
    let mut map = self.slots.lock();
    f(map.entry(id).or_default())
}
```

`source()` 和 `file()` 都会把真正的活包成一个闭包 `f` 传进来，由 `slot()` 负责「定位到正确的 FileSlot」。这样两个公开方法共享同一套锁与查找逻辑，避免重复。

#### 4.1.4 代码实践

**实践目标**：确认 `FileStore` 把「loader + slots」组合起来，且现有测试可跑通。

**操作步骤**：

1. 在 typst 仓库根目录运行 typst-kit 的文件相关测试：

   ```bash
   cargo test -p typst-kit test_file_store_
   ```

2. 打开 `crates/typst-kit/src/files.rs` 的测试模块（[files.rs:362-469](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L362-L469)），观察 `test_file_store_source_via_file`（[L370-L375](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L370-L375)）如何用 `FileStore::new(TestLoader(1))` 构造实例。

**需要观察的现象**：测试通过；构造时没有触发任何 IO（`TestLoader` 只在被 `load()` 时才查表）。

**预期结果**：`cargo test` 报告 `test_file_store_source_via_file ... ok` 等多条通过。

#### 4.1.5 小练习与答案

**练习 1**：`FileStore` 为什么用 `Mutex<FxHashMap>` 而不是 `RefCell<FxHashMap>`？

> **答**：`World` trait 要求 `Send + Sync`（[lib.rs:60](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L60)），typst 用 rayon 做多线程编译。`RefCell` 不是 `Sync`，跨线程共享会编译失败；`parking_lot::Mutex` 提供线程安全的内部可变性。

**练习 2**：调用 `FileStore::new(loader)` 之后，`slots` 里有几条记录？

> **答**：0 条。`new()` 用 `FxHashMap::default()` 初始化空表，所有条目都在首次访问对应 `id` 时由 `slot()` 的 `entry(id).or_default()` 懒插入。

---

### 4.2 source() 与 file()：两种取数入口

#### 4.2.1 概念说明

`World` 有 `source()` 和 `file()` 两个回调，`FileStore` 自然也对外提供同名的两个方法。它们都接收一个 `FileId`，区别只在**返回类型**：

- `source(id) -> FileResult<Source>`：把文件当成「Typst 代码」来读，需要 UTF-8 文本 + 行号索引。编译器解析 `.typ` 时走这里。
- `file(id) -> FileResult<Bytes>`：把文件当成「原始字节」来读，不假设是文本。加载图片/数据时走这里。

为什么是两个方法而不是一个「万能读」？因为同一份文件**可能两种视角都被需要**：一个 `.typ` 文件既要被解析（`Source`），理论上也能被 `#read()` 当原始字节读（`Bytes`）。所以 `FileStore` 要能对同一个 `id` 同时服务两种请求，并且——这是 u3-l2 的精彩之处——**尽量让两者共享同一块底层缓冲区**，避免一份内容存两份。

#### 4.2.2 核心流程

```
FileStore::source(id) ─┐
                       ├─→ slot(id, |s| s.source(&loader, id))
FileStore::file  (id) ─┘    slot(id, |s| s.file  (&loader, id))
                                  │
                                  ▼
                         FileSlot 决定：
                          - 已有 Source/Bytes → 直接 clone（命中缓存）
                          - 只有空槽         → loader.load(id) 取字节
                          - 推进到 Loaded/Parsed 状态（详见 u3-l2）
```

两个方法各自委托给 `FileSlot` 上同名的方法（[FileSlot::source](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L189-L237) 与 [FileSlot::file](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L176-L187)）。本讲把 `FileSlot` 当黑盒，只需记住：**先请求的视角会触发加载，后请求的视角命中缓存。**

#### 4.2.3 源码精读

两个公开方法都只有三行，且形状完全对称：

[files.rs:65-71](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L65-L71) `source()`：

```rust
/// Retrieves the given file id as a Typst source.
/// Can directly be used to implement World::source.
pub fn source(&self, id: FileId) -> FileResult<Source> {
    self.slot(id, |slot| slot.source(&self.loader, id))
}
```

[files.rs:73-79](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L73-L79) `file()`：

```rust
/// Retrieves the given file id as a raw file.
/// Can directly be used to implement World::file.
pub fn file(&self, id: FileId) -> FileResult<Bytes> {
    self.slot(id, |slot| slot.file(&self.loader, id))
}
```

文档注释里那句 **"Can directly be used to implement World::source/file"** 是点睛之笔——它就是为你实现 `World` 而设计的。看 typst-cli 怎么用的：

[typst-cli/src/world.rs:130-136](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L130-L136)：

```rust
fn source(&self, id: FileId) -> FileResult<Source> {
    self.files.source(id)
}
fn file(&self, id: FileId) -> FileResult<Bytes> {
    self.files.file(id)
}
```

`SystemWorld` 的 `impl World` 里，这两个回调**原封不动地转发**给 `self.files`（类型是 `FileStore<SystemFiles>`，见 [world.rs:33](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L33)）。这就是抽象被「接上契约」的全过程。

至于「共享底层缓冲区」：当文件没有 UTF-8 BOM 时，`FileSlot` 会用 `Bytes::from_string(source)` 让 `Bytes` 直接指向 `Source` 内部的字符串，于是 `file()` 和 `source()` 指向同一块内存。测试 `test_file_store_storage_reuse`（[files.rs:387-395](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L387-L395)）甚至用 `std::ptr::eq(...)` 断言两块切片的指针完全相同。细节留到 u3-l2。

#### 4.2.4 代码实践

**实践目标**：验证「同一 `FileId` 先 `file()` 后 `source()` 都返回正确内容，且第二次命中缓存」。

**操作步骤**：

1. 阅读测试 `test_file_store_source_via_file`（[files.rs:370-375](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L370-L375)）：

   ```rust
   let store = FileStore::new(TestLoader(1));
   store.file(id("a.typ")).must_be(A_TEXT);     // 触发加载
   store.source(id("a.typ")).must_be(A_TEXT);    // 命中缓存，转成 Source
   ```

2. 运行：

   ```bash
   cargo test -p typst-kit test_file_store_source_via_file
   ```

**需要观察的现象**：第一次 `file()` 时 `TestLoader` 被调用一次；第二次 `source()` 不再读 loader，而是把已有字节转成 `Source`。

**预期结果**：测试通过，两个视角内容一致。

#### 4.2.5 小练习与答案

**练习 1**：为什么需要把同一个文件既能读成 `Source` 又能读成 `Bytes`？给一个真实场景。

> **答**：一个 `utils.typ` 会被 `#include` 解析（需要 `Source`），但也可能被 `#read("utils.typ", encoding: none)` 当原始字节读（需要 `Bytes`）。`FileStore` 要对同一 `id` 同时支持两种请求。

**练习 2**：`source()` 和 `file()` 共享同一块底层缓冲区的前提是什么？

> **答**：文件内容没有 UTF-8 BOM。因为 BOM 在构造 `Source` 时会被剥离，但 `Bytes` 必须保留原始字节，二者内容不同就无法共用同一块内存（见 u3-l2 与测试 `test_file_store_bom`）。

---

### 4.3 dependencies() 与 reset()：增量重编译的依赖追踪

#### 4.3.1 概念说明

`source()` / `file()` 解决了「读」，但 typst 还有一个核心场景：**`typst watch`**——文件一改就重新编译。这要求回答两个问题：

1. 「这次编译到底碰过哪些文件？」——决定要监视哪些文件、缓存是否失效。
2. 「下次编译前，怎么让旧结果失效，同时又不浪费已经分配的内存？」

这就是 `dependencies()` 和 `reset()` 的职责。它们和 `source()`/`file()` 一起构成一个**编译循环**：

\[ \underbrace{\text{reset()}}_{\text{标记 stale}} \;\longrightarrow\; \underbrace{\text{compile (多次 source/file)}}_{\text{边编译边记录 accessed}} \;\longrightarrow\; \underbrace{\text{dependencies()}}_{\text{收集被访问的 id}} \]

注意 `dependencies()` 和 `reset()` 都是 `&mut self`（而 `source()`/`file()` 是 `&self`）——因为它们要改写 slots 的状态。

#### 4.3.2 核心流程

每个 `FileSlot` 有一个 `accessed()` 标志（[files.rs:162-165](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L162-L165)）：只要它不是 `Empty`，就算「被访问过」。整个循环：

```
reset()
  └─ 把每个 slot 调用 FileSlot::reset()
       └─ 若之前是 Parsed(Ok(source), _)，保留这个 source 作为「stale」
          否则丢弃；状态统一回到 Empty(stale)
       └─ 此时 accessed() 全部变 false

compile()
  └─ 每次调用 source()/file()，对应 slot 被加载/读取
       └─ 一旦离开 Empty，accessed() 变 true

dependencies()
  └─ 过滤 slots 里 accessed()==true 的，取出它们的 FileId
```

关键设计：**`reset()` 不清空 slots、不释放内存**，而是保留旧的 `Source` 作为 `stale`（[files.rs:167-174](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L167-L174)）。下次编译时 `FileSlot::source` 会用 `source.replace(new)` **就地编辑**这个 stale source，而不是重新解析+重新编号行号。这就是「在 place 编辑以提升增量编译性能」（文档原话，见 [files.rs:108-110](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L108-L110)）。细节同样在 u3-l2。

#### 4.3.3 源码精读

[files.rs:81-99](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L81-L99) `dependencies()`：

```rust
pub fn dependencies(&mut self) -> (&L, impl Iterator<Item = FileId> + '_) {
    let iter = self
        .slots
        .get_mut()
        .iter()
        .filter(|(_, slot)| slot.accessed())
        .map(|(&id, _)| id);
    (&self.loader, iter)
}
```

两点值得注意：

1. **它同时返回 `(&L, Iterator)`**——为什么要把 loader 一起返回？因为 `FileId` 是「虚拟」的，要把它解析成磁盘路径必须借 loader（typst-cli 里就是 `loader.resolve(id)`）。而 `resolve` 又需要借用 loader，与遍历 slots 的可变借用冲突，所以干脆把 loader 的引用一起交出来。typst-cli 是这么用的（[world.rs:98-101](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L98-L101)）：

   ```rust
   pub fn dependencies(&mut self) -> impl Iterator<Item = PathBuf> + '_ {
       let (loader, deps) = self.files.dependencies();
       deps.filter_map(|id| loader.resolve(id).ok())
   }
   ```

2. **顺序是「任意」的**——文档明确警告（[files.rs:88-90](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L88-L90)）：因为遍历的是哈希表，结果顺序不确定，需要稳定顺序就得自己 `sort`。

[files.rs:101-116](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L101-L116) `reset()`：

```rust
pub fn reset(&mut self) {
    for slot in self.slots.get_mut().values_mut() {
        slot.reset();
    }
}
```

它遍历所有 slot 调用 `FileSlot::reset()`，把状态打回 `Empty`，但保留可复用的 stale source。typst-cli 的 `SystemWorld::reset()`（[world.rs:104-107](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L104-L107)）把文件重置和时间重置打包在一起。

#### 4.3.4 代码实践

**实践目标**：观察「reset 前后 dependencies 的变化」，理解增量循环。

**操作步骤**：

1. 阅读 `test_file_store_cycles`（[files.rs:398-418](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L398-L418)）。它的核心动作：

   ```rust
   store.source(id("a.typ"));          // 访问 a
   store.source(id("d.typ"));          // 访问 d
   store.file(id("e.bin"));            // 访问 e（返回 NotFound）
   assert_eq!(deps(&store), ["a.typ", "d.typ", "e.bin"]);

   store.loader_mut().0 = 5;           // 改 loader 状态
   store.reset();                      // 重置
   store.source(id("d.typ"));          // 重新只访问 d
   store.file(id("e.bin"));            // 重新只访问 e
   assert_eq!(deps(&store), ["d.typ", "e.bin"]);   // a 不见了
   ```

2. 运行 `cargo test -p typst-kit test_file_store_cycles`。

**需要观察的现象**：第一轮 deps 包含 `a.typ`；`reset()` 后第二轮**没有**重新访问 `a.typ`，于是它从 deps 中消失——`reset()` 之后只有「再次被访问」的文件才会出现。

**预期结果**：测试通过，验证了「reset 清空 accessed 标志、dependencies 只反映本次编译」。

**特别留意**：`e.bin` 在 `loader.0==1` 时返回 `NotFound`，而这个**错误也被缓存了**（进入 `Loaded(Err)` 状态，见 4.4）。这也是为什么 `reset()` 必须存在——否则错误的 NotFound 永远不会被重新尝试。

#### 4.3.5 小练习与答案

**练习 1**：`reset()` 之后、尚未重新编译时，立刻调用 `dependencies()` 会得到什么？

> **答**：空迭代器。`reset()` 把所有 slot 打回 `Empty`，`accessed()` 全为 false，没有任何 id 通过过滤。

**练习 2**：既然要重新加载，为什么 `reset()` 不直接 `slots.clear()` 清空整个表？

> **答**：因为要保留旧的 `Source` 作为 stale，让下一轮编译做「就地编辑」（`source.replace(new)`）而不是重新解析和编号，从而提升增量编译性能。`clear()` 会丢掉这些可复用的 source。详见 u3-l2。

---

### 4.4 FileLoader trait：插拔你自己的字节来源

#### 4.4.1 概念说明

`FileStore` 自己不知道「字节从哪来」，这件事完全委托给 `FileLoader` trait。它是整个文件子系统里**唯一面向用户扩展**的接口。trait 只有一个方法：

```rust
fn load(&self, id: FileId) -> FileResult<Bytes>;
```

「给我一个 `FileId`，还我一段字节。」就这么简单。你想从哪来就从哪来：

- 读磁盘 → typst-kit 内置的 `SystemFiles`（u3-l3）。
- 返回内存里编辑中的文本 → 语言服务器。
- 返回固定字节 → 单元测试（本讲的 `TestLoader`）。
- 从网络/虚拟文件系统来 → 你的自定义实现。

trait 文档（[files.rs:246-255](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L246-L255)）特别提醒：实现 `load()` 时通常要**先 `match id.root()`**，判断这个文件属于「项目」还是「某个包」，再决定去哪读。因为 `FileId` 是虚拟的，它的 `root()` 决定了它在哪个命名空间里。

#### 4.4.2 核心流程

```
FileSlot 发现缓存未命中
        │
        ▼
loader.load(id)                          ← 唯一扩展点
        │
        ├─ match id.root()
        │     VirtualRoot::Project       → 从项目根目录读
        │     VirtualRoot::Package(spec) → 从包存储读（先 obtain 到 FsRoot）
        │
        ▼
返回 Bytes / Err(FileError)
        │
        ▼
FileSlot 把结果(含错误)缓存进状态机
```

typst-cli 的 `SystemFiles::load` 就是这个形状（[world.rs:260-270](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L260-L270)）：先特判 stdin/empty，否则 `self.root(id)?.load(id.vpath())`——`root(id)` 内部正是按 `id.root()` 分发到项目根或包根（u3-l3 精读）。

#### 4.4.3 源码精读

[files.rs:256-266](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L256-L266) `FileLoader` trait 定义：

```rust
pub trait FileLoader {
    /// Load the data for the given file ID.
    /// Generally, here you'll want to match on the root() of the id ...
    fn load(&self, id: FileId) -> FileResult<Bytes>;
}
```

紧接着为 `Box<F>` 和 `Arc<F>` 提供了透明转发实现（[files.rs:268-278](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L268-L278)）：

```rust
impl<F: FileLoader> FileLoader for Box<F> {
    fn load(&self, id: FileId) -> FileResult<Bytes> { (**self).load(id) }
}
impl<F: FileLoader> FileLoader for Arc<F> {
    fn load(&self, id: FileId) -> FileResult<Bytes> { (**self).load(id) }
}
```

这意味着 `FileStore<Box<dyn FileLoader>>`、`FileStore<Arc<MyLoader>>` 都能直接用——你可以把 loader 装箱做动态分发，也可以用泛型做静态分发。两种用法零成本互换。

测试里的 `TestLoader` 是最简实现（[files.rs:426-439](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L426-L439)）：

```rust
struct TestLoader(usize);

impl FileLoader for TestLoader {
    fn load(&self, id: FileId) -> FileResult<Bytes> {
        Ok(match id.vpath().get_without_slash() {
            "a.typ" => Bytes::new(Vec::from(A_TEXT)),
            "d.typ" => Bytes::from_string(format!("{}", self.0)),
            "e.bin" if self.0 > 3 => Bytes::from_string(E_TEXT),
            path => return Err(FileError::NotFound(path.into())),
        })
    }
}
```

它根据虚拟路径名返回不同字节，`self.0` 是一个可变计数器（测试通过 `loader_mut().0 = 5` 改它，模拟「文件内容变了」）。注意 **`NotFound` 也是 `FileResult`**——`FileSlot` 会把这个错误缓存进 `Loaded(Err)` 状态，下次再问直接返回同一个错误（这正是 4.3 里 `e.bin` 的行为）。

#### 4.4.4 代码实践

**实践目标**：实现一个返回固定字节的 `FileLoader`，验证它能驱动 `FileStore`。

**操作步骤**：

下面是一段**示例代码**（非项目原有代码），可以放进一个新的 cargo 项目或 typst-kit 的测试模块里试跑：

```rust
// 示例代码：一个总是返回固定字节的 FileLoader
use typst_kit::files::{FileLoader, FileStore};
use typst_library::diag::FileResult;
use typst_library::foundations::Bytes;
use typst_syntax::{
    FileId, RootedPath, VirtualPath, VirtualRoot,
};

struct HelloLoader;

impl FileLoader for HelloLoader {
    fn load(&self, _id: FileId) -> FileResult<Bytes> {
        // 无论什么 id，都返回同一段文本
        Ok(Bytes::from_string("Hello, FileStore!".into()))
    }
}

// 构造一个「项目根下的 main.typ」FileId（照搬测试里的 id() 助手）
fn main_id() -> FileId {
    RootedPath::new(
        VirtualRoot::Project,
        VirtualPath::new("main.typ").unwrap(),
    )
    .intern()
}

fn main() {
    let store = FileStore::new(HelloLoader);
    // 用同一 id 读两种视角
    let src = store.source(main_id()).unwrap();
    let bin = store.file(main_id()).unwrap();
    println!("source: {}", src.text());
    println!("file  : {:?}", std::str::from_utf8(bin.as_slice()).unwrap());
}
```

**需要观察的现象**：`source()` 和 `file()` 都打印出 `Hello, FileStore!`；`HelloLoader::load` 全程只被调用一次（可在 `load` 里加一行 `println!("load called")` 验证）。

**预期结果**：输出两行相同文本，且 `load called` 只出现一次（第二次命中缓存）。关于 `FileId` 的确切导入路径与构造 API，**待本地验证**（不同 typst 版本可能微调）。

#### 4.4.5 小练习与答案

**练习 1**：当 `FileLoader::load` 返回 `Err(FileError::NotFound)` 时，`FileStore` 会缓存这个错误吗？这会带来什么后果？

> **答**：会。`FileSlot` 会进入 `Loaded(Err)` 状态，后续对同一 `id` 的访问直接返回缓存的错误，不再调用 loader。后果是：如果文件后来创建了，必须先 `reset()` 才能重新尝试加载——这正是 `test_file_store_cycles` 里 `e.bin` 需要 reset 的原因。

**练习 2**：若希望「项目文件读磁盘、包文件读别处」，`load()` 内部该怎么写？

> **答**：`match id.root() { VirtualRoot::Project => /* 用项目 FsRoot 读 id.vpath() */, VirtualRoot::Package(spec) => /* 先 obtain 包的 FsRoot，再读 */ }`。typst-kit 的 `SystemFiles` 和 typst-cli 的 `SystemFiles::load` 都是这么分发的，详见 u3-l3。

---

## 5. 综合实践

把本讲的四个抽象串起来，完成一个「最小 World 文件层」模拟。

**任务**：写一个能记录「被读取次数」的自定义 `FileLoader`，构造 `FileStore`，模拟两次编译循环，观察 `dependencies()` 与缓存的配合。

**操作步骤**（示例代码，非项目原有）：

```rust
use std::sync::atomic::{AtomicUsize, Ordering};
// ... 导入同 4.4.4 ...

struct CountingLoader { count: AtomicUsize }

impl FileLoader for CountingLoader {
    fn load(&self, id: FileId) -> FileResult<Bytes> {
        self.count.fetch_add(1, Ordering::SeqCst);
        Ok(Bytes::from_string(format!("v{}", self.count.load(Ordering::SeqCst))))
    }
}

fn main() {
    let mut store = FileStore::new(CountingLoader { count: AtomicUsize::new(0) });
    let a = id("a.typ"); let b = id("b.typ");

    // ===== 第一次「编译」=====
    store.source(a);          // 触发 load
    store.source(a);          // 命中缓存，不触发 load
    store.file(b);            // 触发 load
    let (_, deps1) = store.dependencies();
    let deps1: Vec<_> = deps1.collect();
    println!("第 1 次 deps 数量 = {}", deps1.len());   // 预期 2

    // ===== reset 并「重新编译」=====
    store.reset();
    store.source(a);          // 重新访问 a
    let (_, deps2) = store.dependencies();
    let deps2: Vec<_> = deps2.collect();
    println!("第 2 次 deps 数量 = {}", deps2.len());   // 预期 1（只剩 a）

    // ===== 观察：file(b) 在 reset 后未再访问，故不在 deps2 =====
}
```

**需要观察并解释的现象**：

1. 第一次 `deps` 数量为 2（a、b 都被访问过），尽管 `a` 被请求了两次——验证**缓存**（load 只在首次发生）。
2. `reset()` 后只重新访问 `a`，第二次 `deps` 数量为 1——验证**依赖追踪只反映本次编译**。
3. 在 `CountingLoader::load` 里打印计数，确认第一次编译触发 2 次 load（a、b 各一次），第二次编译只触发 1 次 load（a，因为 reset 清了 accessed 但内容仍要重新走 loader 一次）。

**预期结果**：上述三个数字分别打印 `2`、`1`，load 计数最终为 `3`。具体输出格式**待本地验证**。

**进阶思考**：如果在上面的第二次编译里把 `store.source(a)` 换成什么都不做，`deps2` 会是多少？（答：0，因为 reset 后没有任何访问。）再把这个问题和 u7-l2 的 `Watcher` 联系起来：watcher 正是用 `dependencies()` 的结果决定「监视哪些文件」。

## 6. 本讲小结

- `FileStore<L>` 是「缓存 + 懒加载」层：按 `FileId` 在 `Mutex<FxHashMap<FileId, FileSlot>>` 里记住已加载的文件，`loader.load()` 只在缓存未命中时发生。
- 所有读取都走私有入口 `slot(id, closure)`，靠 `entry(id).or_default()` 保证「同一 id 永远对应同一 FileSlot」，这是缓存命中语义的基石。
- `source()` 返回 `Source`（文本+行号），`file()` 返回 `Bytes`（原始字节）；两者可共享同一块缓冲区（无 BOM 时）。typst-cli 的 `World::source/file` 直接转发给它们。
- `dependencies()` 收集「自上次 reset 起被访问过的 FileId」，并连 loader 一起返回以便解析路径；顺序任意。
- `reset()` 不清空内存，而是把 slot 打回 `Empty` 并保留 stale source 供就地编辑——这是增量编译性能的关键，细节在 u3-l2。
- `FileLoader` 是唯一扩展点，单方法 `load(id) -> FileResult<Bytes>`；实现时通常 `match id.root()` 区分项目/包。`Box`/`Arc` 透明转发让动态/静态分发自由切换。

## 7. 下一步学习建议

- **u3-l2 FileSlot 状态机**：本讲把 `FileSlot` 当黑盒，下一讲拆开它的 `Empty → Loaded → Parsed` 三态机，精读 BOM 处理与底层缓冲复用的优化技巧。
- **u3-l3 SystemFiles 与 FsRoot**：本讲只点到 `SystemFiles::load` 按 `root()` 分发，下一讲看它如何把虚拟路径解析为磁盘真实路径、如何阻止路径逃逸。
- **u7-l2 文件监视 watcher**：带着本讲对 `dependencies()` 的理解去读 watcher，体会「编译产出依赖 → watcher 监视依赖」的完整闭环。
- 若想看 `World` 全貌，回看 typst-cli 的 [SystemWorld](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L25-L38)，确认 `FileStore` 在七个回调里占了几席。
