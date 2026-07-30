# FileSlot 状态机与缓冲区复用

## 1. 本讲目标

在 u3-l1 里，我们把 `FileSlot` 当成一个「会缓存、会复用缓冲」的**黑盒**：只关心它对外提供 `source()` / `file()` 两个方法，按下不表内部细节。本讲就把它拆开。

学完本讲你应该能够：

1. 画出 `FileSlot` 的 `Empty → Loaded → Parsed` 三态状态机，并说清 `file()` 与 `source()` 各自如何推进状态、哪些情况下「不推进、直接命中缓存」。
2. 解释 `reset()` 为什么**不清空内存**，而是把旧的 `Source` 作为 `stale` 留下来，让下一轮编译通过 `Source::replace()` **就地编辑**——这是增量编译性能的关键。
3. 讲透 UTF-8 BOM 处理：为什么带 BOM 的文件无法让 `file()` 与 `source()` 共享同一块底层缓冲区，而不带 BOM 的文件却可以做到指针级共享。
4. 理解 `Bytes::into_string()` / `Bytes::from_string()` 这对「所有权搬家」操作如何在无 BOM 路径上实现零拷贝的缓冲复用，以及在 UTF-8 解码失败时如何「不丢字节」。
5. 顺着测试 `test_file_store_storage_reuse` 与 `test_file_store_bom` 把上述机制亲手验证一遍。

本讲属于 **advanced** 难度，因为它同时涉及状态机设计、内存复用技巧和 UTF-8 边界条件。代码量很小，但每一行都有用意。

## 2. 前置知识

### 2.1 承接 u3-l1：FileSlot 是什么、住在哪

回顾 u3-l1 的结论：`FileStore<L>` 把「记住已读文件」与「字节从哪来」分成两层。缓存层内部长这样：

```rust
pub struct FileStore<L> {
    loader: L,
    slots: Mutex<FxHashMap<FileId, FileSlot>>,  // 每个 FileId 对应一个 FileSlot
}
```

`FileStore` 的所有读取都汇聚到一个私有入口 `slot(id, closure)`，它靠 `map.entry(id).or_default()` 保证「同一个 `FileId` 永远对应同一个 `FileSlot`」。所以 `FileSlot` 就是**单个文件在缓存里的一条记录**，它的全部职责是：记住这个文件「现在加载到哪一步了」「字节/源码存了没有」「能不能复用上一轮留下的旧源码」。

u3-l1 把 `FileSlot` 当黑盒，本讲打开黑盒。

### 2.2 三个会用到的底层类型

| 类型 | 在本讲的作用 | 定义位置 |
|------|------------|----------|
| `Source` | 一段 UTF-8 文本 + 行号/语法树索引。支持 `new(id, text)` 构造、`replace(new)` 增量编辑、`text()` 取文本 | typst-syntax 的 [source.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/source.rs#L36-L41) |
| `Bytes` | 原始字节，`Arc` 包裹、clone 廉价。关键方法是 `into_string()`（尝试夺取底层 `String`/`Vec` 的所有权）和 `from_string(source)`（让 `Bytes` 直接由一个字符串/源码「背后支撑」，零拷贝） | typst-library 的 [bytes.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/bytes.rs#L46) |
| `FileResult<T>` | `Result<T, FileError>`，注意它**允许把错误也缓存起来** | typst-library |

一句话提醒：`Bytes` 和 `Source` 都是**引用计数**的廉价类型。`clone()` 只是增加一个 `Arc` 计数，不拷贝内容。本讲所有的「复用」「共享」都是在这个前提下才有意义的——否则每次都深拷贝，复用就无从谈起。

### 2.3 一个关键事实：Source 实现了 AsRef<str>

这一点是后面理解「缓冲共享」的钥匙，所以单独拎出来。`Source` 实现了 `AsRef<str>`，并且 `as_ref()` 就是返回 `self.text()`：

[source.rs:151-155](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/source.rs#L151-L155)（`Source` 的 `AsRef<str>` 实现）：

```rust
impl AsRef<str> for Source {
    fn as_ref(&self) -> &str {
        self.text()
    }
}
```

正因为如此，`Bytes::from_string(source)` 才合法（它要求 `T: AsRef<str>`），也才能让 `Bytes` 直接「贴」在 `Source` 的文本缓冲上。**记住这个事实，第 4 节会用到。**

## 3. 本讲源码地图

| 文件 | 在本讲的作用 |
|------|------------|
| `crates/typst-kit/src/files.rs` | **绝对核心**。`FileSlot` 枚举、`file()`/`source()`/`reset()`/`accessed()`、BOM 处理逻辑，以及四个佐证测试，全部在这里。本讲 90% 的引用来自此文件。 |
| `crates/typst-library/src/foundations/bytes.rs` | `Bytes::into_string()` / `from_string()` 的实现，是理解「无 BOM 路径如何零拷贝复用」的依据。 |
| `crates/typst-syntax/src/source.rs` | `Source::new()` / `replace()` / `text()` 的实现，是理解「stale source 就地编辑」与「指针共享」的依据。 |

> 提示：`FileSlot` 是 `files.rs` 里的**私有 `enum`**（没有 `pub`），外部只能通过 `FileStore` 的方法间接操作它。本讲为了讲清机制，直接进入它的私有实现。

## 4. 核心概念与源码讲解

### 4.1 FileSlot 三态枚举与 Stale\<Source\>

#### 4.1.1 概念说明

「缓存一个文件」听起来简单——存下来不就行了？但 typst 的需求让这件事变得微妙：

- 同一个文件，**既可能被当 `Source` 读（解析 `.typ`），也可能被当 `Bytes` 读（当原始数据）**。两种请求可能任意顺序、任意次数到来。
- `source()` 比较贵（要解析 + 编号行号），`file()` 很便宜。我们不想为了一个 `file()` 请求就去解析。
- 下一轮编译时，文件内容可能只是「改了一点点」，这时重新从头解析很浪费——最好能复用上一轮的 `Source`，**只重解析变化的那一段**。
- 读文件可能失败（`NotFound`、非 UTF-8）。失败的结果也要被记住，否则反复读盘。

这四点决定了 `FileSlot` 必须是一个**有状态机**的东西，而不是一个简单的 `Option<Bytes>`。typst 的答案是三个状态：

1. **`Empty`**：什么实质内容都没加载。但可能「兜里揣着」上一轮 reset 留下的旧源码（`stale`），等会儿好用它就地编辑。
2. **`Loaded`**：被当作 `file()` 读过（拿到字节了），但还没被解析成 `Source`。也可能兜着 stale。
3. **`Parsed`**：已经被解析成 `Source`（或确定是 UTF-8 错误），并且字节也齐了。这是**终态**——此后 `file()` 和 `source()` 都直接命中缓存。

`Stale<Source>` 是一个类型别名，本质上就是 `Option<Source>`，表示「上一轮留下的、可能过时但可复用的源码」。它的存在是「就地编辑」优化的载体。

#### 4.1.2 核心流程

三态之间的迁移由 `file()`、`source()`、`reset()` 三个方法驱动。先给一张全景图（`↦` 表示「方法调用导致迁移」，`·` 表示「命中缓存、不迁移」）：

```
                    reset()  (任意状态 → Empty，把 Parsed(Ok) 的 source 存为 stale)
                       ▲
                       │
   ┌───────────────────┴──────────────────────────────────┐
   │                                                       │
   ▼                file()                     source()
 Empty(stale) ──────────────► Loaded(Bytes, stale) ──────────────► Parsed(Source, Bytes)
   │   (无论 load 成功/失败,                      │   (Loaded(Ok) → 解析 → Parsed；
   │    结果都进 Loaded 的 FileResult)            │    Loaded(Err) → 不迁移, 返回缓存错误)
   │                                              │
   │              source()                        │
   └──────────── 直接跳到 ──────────────────────►│
            (Empty 且 load 成功: 一跳到 Parsed)     │
                                                 │
                              file() / source()  │  ← 终态: 命中缓存, 只 clone
                              ──────────────────►│
```

读图要点：

- **`file()` 只会把 `Empty` 推进到 `Loaded`**，对 `Loaded`/`Parsed` 都是命中缓存（克隆返回）。
- **`source()` 会一路推进到 `Parsed`**：从 `Empty`（load 成功时）或 `Loaded(Ok)` 出发，最终都落到 `Parsed`。唯一例外是 `Loaded(Err)`——错误已缓存，直接返回，不再尝试。
- **`Empty` + `source()` 可以「一跳到 `Parsed`」**，不必先经过 `Loaded`。这点很容易被忽略，但 u3-l1 的「先 file 后 source」测试恰恰走的是另一条路。
- **`reset()` 是唯一的「回退」操作**，但它不丢内存，而是回收 `Parsed(Ok)` 里的 `Source` 作为 stale。

#### 4.1.3 源码精读

先看枚举定义本身，[files.rs:128-154](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L128-L154) 是 `FileSlot` 及其三个变体的文档与定义。注意每段文档注释都写明了「会迁移到哪个状态」，这是阅读状态机最权威的线索：

```rust
enum FileSlot {
    // 可能揣着上一轮 reset 留下的 stale source。
    // file() 请求 → 迁移到 Loaded；
    // source() 请求且能加载 → 迁移到 Parsed，否则 → Loaded。
    Empty(Stale<Source>),

    // 被当作 file() 读过，但还没解析成 source。仍可能揣着 stale。
    // source() 请求 → 迁移到 Parsed。
    Loaded(FileResult<Bytes>, Stale<Source>),

    // 被当作 source() 读过（也可能被 file() 读过）。
    // 尽可能让 bytes 由 source 背后支撑（Bytes::from_string(source)），
    // 这样 file() 与 source() 共用同一块内存。
    // 注意：带 UTF8-BOM 的数据做不到这点，因为 source 会剥掉 BOM、file 要保留 BOM。
    Parsed(Result<Source, Utf8Error>, Bytes),
}
```

几个设计细节值得点出：

- `Loaded` 里存的是 `FileResult<Bytes>` 而非 `Bytes`——**错误也被缓存**。这正是 u3-l1 里 `e.bin` 返回 `NotFound` 后必须 `reset()` 才会重试的原因。
- `Parsed` 里存的是 `Result<Source, Utf8Error>`——表示「解析失败（非 UTF-8）」这一终态也要被记住。`Utf8Error` 表示「这文件的字节不是合法 UTF-8，没法当源码」。
- `Parsed` 同时保留了 `Bytes`。也就是说，哪怕一个 `.bin` 二进制文件被错误地当成 `source()` 请求了（解析失败），它的原始字节仍然留在 `bytes` 字段里，后续 `file()` 还能正常返回。**解析失败不丢字节**。

接着是 `Stale<Source>` 这个类型别名，[files.rs:156-159](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L156-L159)：

```rust
/// Holds a source that is not up to date, but may be updated to the newest
/// state for better incremental performance than parsing and numbering it
/// from scratch.
type Stale<T> = Option<T>;
```

它就是 `Option<T>`，但起名 `Stale`（陈旧的）来传达语义：「这是我留着的一份额外信息，不是当前数据本身」。`Empty` 和 `Loaded` 里都各带一份 `Stale<Source>`，因为不管你现在加载到哪一步，只要上一轮有过 `Parsed(Ok)`，这份旧 source 就一直「兜着」，直到被 `source()` 消费掉用于就地编辑。

还有一个判断标志 `accessed()`，[files.rs:162-165](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L162-L165)：

```rust
fn accessed(&self) -> bool {
    !matches!(self, Self::Empty(_))
}
```

只要不是 `Empty`，就算「本轮被访问过」。这就是 `FileStore::dependencies()` 过滤依赖的依据（见 u3-l1）：`reset()` 把所有 slot 打回 `Empty`，于是 `accessed()` 全为假；本轮再次被 `file()`/`source()` 触碰的 slot 才会离开 `Empty`，从而被 `dependencies()` 收集。

#### 4.1.4 代码实践

**实践目标**：用代码确认「`Empty` 是初始态、`accessed()` 在 `Empty` 时为假」。

**操作步骤**：

1. 阅读 `FileSlot` 的 `Default` 实现，[files.rs:240-244](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L240-L244)：

   ```rust
   impl Default for FileSlot {
       fn default() -> Self {
           Self::Empty(None)   // 初始：空槽，且没有 stale
       }
   }
   ```

   它说明 `slot()` 里 `entry(id).or_default()` 新建的槽，一定是 `Empty(None)`——既没加载、也没有可复用的旧 source（因为这是第一次见到的 `FileId`，历史上不可能有过 `Parsed`）。

2. （**示例代码**，非项目原有）若想亲手验证 `accessed()`，可在 `files.rs` 的 `mod tests` 内临时加一个断言：

   ```rust
   // 示例代码：仅供理解，不要提交
   let mut slot = FileSlot::default();          // Empty(None)
   assert!(!slot.accessed());                   // 初始未被访问
   // slot 是私有的，这里只是示意其行为
   ```

   由于 `FileSlot` 是私有类型，这段代码无法在外部 crate 编译，仅用于在 `files.rs` 内部测试模块里理解。

**需要观察的现象**：`default()` 产出 `Empty(None)`；`accessed()` 对它返回 `false`。

**预期结果**：断言通过。具体能否在你本地直接编译运行，**待本地验证**（取决于是否在 `mod tests` 内）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Loaded` 变体里存的是 `FileResult<Bytes>`，而不是 `Bytes`？

> **答**：为了把**加载错误也缓存**起来。比如 `NotFound`：第一次 `file()` 读不到，状态变成 `Loaded(Err)`；之后再次请求同一文件，直接返回缓存的错误，不再读盘。代价是文件后来若被创建，必须 `reset()` 才能重新尝试（见 u3-l1 的 `e.bin`）。

**练习 2**：`Parsed(Result<Source, Utf8Error>, Bytes)` 里同时保留 `Bytes`，是为了应对什么场景？

> **答**：应对「一个二进制文件先被 `source()` 请求、解析失败、之后又被 `file()` 请求」的场景。解析失败时 `Result` 是 `Err(Utf8Error)`，但原始 `Bytes` 仍被保留，于是 `file()` 能照常返回字节。**解析失败不丢数据**。

---

### 4.2 file() 与 source()：状态如何推进

#### 4.2.1 概念说明

`file()` 和 `source()` 是 `FileSlot` 上仅有的两个「读」方法。它们的共性是：

- **命中缓存时只克隆、不迁移状态**（`Bytes`/`Source` 克隆廉价）。
- **未命中时才真正干活**：调 `loader.load(id)` 取字节，并把状态往前推一格。

差别在于「推到哪里」：

- `file()` 很「克制」：只要把字节弄到手就行，所以它最多把 `Empty` 推到 `Loaded`，**绝不解析**。
- `source()` 很「彻底」：它的目标就是 `Parsed`，所以它会一路走到底——取字节、剥 BOM、解码 UTF-8、构造 `Source`，最后落到 `Parsed`。

这种分工带来一个性能上的好处：如果某个文件**只**被 `file()` 请求（比如一张图片），它永远停在 `Loaded`，**不会触发解析的开销**。解析只发生在真正需要 `Source` 的时候。

#### 4.2.2 核心流程

`file()` 的迁移规则（对照 4.1.2 的图）：

```
file(id):
  Empty(stale)        → loader.load(id) → Loaded(load结果, 取走的stale)；返回结果
  Loaded(result, _)   → （命中）返回 result.clone()
  Parsed(_, bytes)    → （命中）返回 bytes.clone()
```

`source()` 的迁移规则（重点：它总是奔向 `Parsed`）：

```
source(id):
  Empty(stale):
    load Ok  → 取得 (bytes, stale)，向下进入「构造 Source」流程 → 最终 Parsed
    load Err → Loaded(Err, stale)，返回错误   ← 注意这里落到了 Loaded 而非 Parsed
  Loaded(Ok(_), _)    → 取出 (bytes, stale)，向下构造 Source → Parsed
  Loaded(Err(err), _) → （命中错误）返回 err.clone()
  Parsed(source, _)   → （命中）返回 source.clone()?
```

特别留意 `Empty(load Ok) → Parsed` 这条「一跳」路径：它**绕过了 `Loaded`**。这是因为既然你都要 `source()` 了，就没必要先在 `Loaded` 停一脚——直接把字节拿来解析、落到 `Parsed` 最省事。

#### 4.2.3 源码精读

先看 `file()`，[files.rs:176-187](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L176-L187)：

```rust
fn file(&mut self, loader: &impl FileLoader, id: FileId) -> FileResult<Bytes> {
    match self {
        Self::Empty(stale) => {
            let result = loader.load(id);
            *self = Self::Loaded(result.clone(), mem::take(stale));
            result
        }
        Self::Loaded(result, _) => result.clone(),
        Self::Parsed(_, bytes) => Ok(bytes.clone()),
    }
}
```

逐行看 `Empty` 分支：

1. `loader.load(id)` 真正取字节（唯一会触发 IO 的地方）。
2. `mem::take(stale)` 把兜里的 stale source **取走**（`Option::take` 把值搬出来、原位置成 `None`），跟着搬进 `Loaded`。可见 stale 是「跟着状态机一起搬」的，不会丢。
3. 状态变成 `Loaded(result.clone(), stale)`。注意连**错误结果**也 `clone` 进去了。
4. 返回 `result`。

`Loaded` 与 `Parsed` 分支都是纯命中：克隆返回，`self` 不变。

再看 `source()` 的前半段（取字节 + 拿 stale），[files.rs:189-207](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L189-L207)：

```rust
fn source(&mut self, loader: &impl FileLoader, id: FileId) -> FileResult<Source> {
    // 若已有 source 或错误，直接返回；否则加载/取出字节和可能的 stale source
    let (bytes, stale) = match self {
        Self::Empty(stale) => match loader.load(id) {
            Ok(bytes) => (bytes, mem::take(stale)),   // ← 不赋值 self，向下进入构造流程
            Err(err) => {
                *self = Self::Loaded(Err(err.clone()), mem::take(stale));
                return Err(err);                       // ← Empty(load Err) 落到 Loaded(Err)
            }
        },
        Self::Loaded(Ok(_), _) => match mem::take(self) {
            Self::Loaded(Ok(bytes), stale) => (bytes, stale),
            _ => unreachable!(),
        },
        Self::Loaded(Err(err), _) => return Err(err.clone()),   // 命中错误，不迁移
        Self::Parsed(source, _) => return Ok(source.clone()?),  // 命中，不迁移
    };
    // ……下面是「构造 Source」流程（4.3、4.4 精读）……
```

注意这段代码精妙地用一个 `match` 同时做了三件事：(1) 命中 `Parsed`/`Loaded(Err)` 就提前 `return`；(2) 否则取出 `(bytes, stale)` 这两个值，统一交给后面的「构造 Source」流程。无论你是从 `Empty` 还是 `Loaded(Ok)` 来的，最后都汇流到同一段构造逻辑——**避免重复代码**。

汇流之后，构造流程的结尾是 [files.rs:235-236](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L235-L236)：

```rust
*self = Self::Parsed(result.clone(), bytes);
Ok(result?)
```

无论前面走了哪条分支，最后都把状态写回 `Parsed`。`result` 可能是 `Ok(Source)` 也可能是 `Err(Utf8Error)`，两者都被装进 `Parsed` 这个终态。

#### 4.2.4 代码实践

**实践目标**：验证「`Empty` + `source()` 可以一跳到 `Parsed`，且 `file()` 只推到 `Loaded`」。

**操作步骤**：

1. 阅读测试 `test_file_store_source_via_file`，[files.rs:370-375](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L370-L375)：

   ```rust
   fn test_file_store_source_via_file() {
       let store = FileStore::new(TestLoader(1));
       store.file(id("a.typ")).must_be(A_TEXT);     // Empty → Loaded
       store.source(id("a.typ")).must_be(A_TEXT);    // Loaded(Ok) → Parsed
   }
   ```

   这条是「先 file 后 source」：`Empty → Loaded → Parsed`，走了两格。

2. 再阅读 `test_file_store_storage_reuse`，[files.rs:387-395](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L387-L395)：

   ```rust
   fn test_file_store_storage_reuse() {
       let store = FileStore::new(TestLoader(1));
       let a_source = store.source(id("a.typ")).unwrap();  // 直接 source：Empty → Parsed（一跳！）
       let a_file = store.file(id("a.typ")).unwrap();      // Parsed 命中缓存
       // ...
   }
   ```

   这条是「直接 source」：`Empty → Parsed`，**一跳**，没有经过 `Loaded`。

3. 运行两个测试：

   ```bash
   cargo test -p typst-kit test_file_store_source_via_file test_file_store_storage_reuse
   ```

**需要观察的现象**：两条路径都通过，内容一致；但状态迁移轨迹不同（一个两格、一个一格）。`file()` 在「直接 source」场景里根本没机会把状态推到 `Loaded`。

**预期结果**：两个测试都 `... ok`。

#### 4.2.5 小练习与答案

**练习 1**：假设一个文件**只**被 `file()` 请求过多次，它的 `FileSlot` 处于什么状态？有没有产生 `Source`？

> **答**：处于 `Loaded(Ok(bytes), stale)`，只发生过一次 `loader.load`，后续全是缓存命中。**没有产生 `Source`，也没做任何解析**。这正是「按需解析」的好处。

**练习 2**：`source()` 在 `Empty` 且 `load` 失败时，为什么落到 `Loaded(Err)` 而不是留在 `Empty`？

> **答**：因为加载这一步**确实发生过了**（虽然失败了）。把错误存进 `Loaded(Err)`，下次再问就直接返回缓存错误、不再读盘；同时也让 `accessed()` 变真（因为不再是 `Empty`），从而被 `dependencies()` 收集——「尝试过读取的文件」也算一种依赖。

---

### 4.3 reset() 与就地编辑：stale source 的复用

#### 4.3.1 概念说明

typst 有一个高频场景：**`typst watch` 连续编译**。每次源码一改，就重新编译。最朴素的实现是「每轮都 new 一个全新的 `FileStore`」，但这样每轮都要重新解析所有 `.typ` 文件、重新编号行号——而实际上文件往往只改了几个字。

typst-kit 的优化思路是：**不要重建，而是 reset**。`reset()` 把每个 `FileSlot` 打回 `Empty`（这样 `accessed()` 重置为假、`dependencies()` 会重新统计），但**悄悄把上一轮 `Parsed(Ok)` 里的 `Source` 留下来**，塞进 `Empty` 的 `stale` 字段。等下一轮 `source()` 再次访问这个文件时，发现兜里有一份旧 source，就用 `Source::replace(new_text)` **只重解析变化的那一段**，而不是从头解析。

这就是 u3-l1 文档里那句「source files will be edited in place with updated data, leading to improved incremental compilation performance」的落点。本节把它彻底讲清。

#### 4.3.2 核心流程

`reset()` 的动作：

```
reset():
  遍历每个 FileSlot:
    用 mem::take 把当前值搬出来（原位置变 Default=Empty(None)）
      若原来是 Parsed(Ok(source), _) → 把这个 source 作为 stale
      否则（Empty / Loaded / Parsed(Err)）→ stale = None
    把 slot 设为 Empty(stale)
```

注意：只有 `Parsed(Ok)` 的 source 被回收为 stale。`Loaded` 里的字节、`Parsed(Err)` 都被丢弃——因为只有「成功解析过的 source」才值得复用，字节下一轮重新 load 就好（它可能已经变了）。

下一轮 `source()` 消费 stale 的动作（在 4.4 的「构造 Source」流程里，Branch A）：

```
若兜里有 stale source:
  new = 解码后的新文本（已剥 BOM）
  stale.replace(new)   ← 增量编辑：只重解析 old/new 的 diff 部分
  复用这份 source 作为结果
```

增量编辑带来的收益可以这样理解。设一个文件长度为 \(n\)，改动了一个长度为 \(d\) 的小片段。从头重新解析的成本与 \(n\) 相关，而 `replace()` 只重解析受影响的一小块：

\[
\text{全量解析成本} \sim O(n) \quad\gg\quad \text{replace 成本} \sim O(d) \quad (d \ll n)
\]

这正是 `Stale<Source>` 存在的全部理由。

#### 4.3.3 源码精读

先看 `FileStore::reset()`，[files.rs:101-116](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L101-L116)（在 u3-l1 已贴过，这里聚焦它和 stale 的关系）：

```rust
pub fn reset(&mut self) {
    #[expect(clippy::iter_over_hash_type, reason = "order does not matter")]
    for slot in self.slots.get_mut().values_mut() {
        slot.reset();
    }
}
```

它只是把每个 slot 交给 `FileSlot::reset()`，真正干活的是后者，[files.rs:167-174](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L167-L174)：

```rust
fn reset(&mut self) {
    let stale = match mem::take(self) {
        Self::Parsed(Ok(source), _) => Some(source),   // 只回收成功解析的 source
        _ => None,
    };
    *self = Self::Empty(stale);
}
```

- `mem::take(self)`：把 `*self` 整个搬走（`self` 临时变成 `Default::default()` 即 `Empty(None)`），返回旧值。这是 Rust 里「原地替换」的惯用法，避免借用冲突。
- 只有旧状态是 `Parsed(Ok(source), _)` 时，才把 `source` 提取为 `Some`；其余情况（含 `Parsed(Err)`）都是 `None`。
- 最后 `*self = Empty(stale)`，状态回退到 `Empty`，但兜里揣着可复用的 source（若有）。

再看消费端——`Source::replace()` 的语义，[source.rs:80-96](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/source.rs#L80-L96)：

```rust
/// Performs a naive (suffix/prefix-based) diff of the old and new text
/// to produce the smallest single edit that transforms old into new and
/// then calls edit() with it.
pub fn replace(&mut self, new: &str) -> Range<usize> {
    let _scope = typst_timing::TimingScope::new("replace source");
    let Some((prefix, suffix)) = self.0.lines.replacement_range(new) else {
        return 0..0;
    };
    let old = self.text();
    let replace = prefix..old.len() - suffix;
    let with = &new[prefix..new.len() - suffix];
    self.edit(replace, with)
}
```

`replace` 做的事：找到新旧文本**公共前缀**和**公共后缀**，从而定位出「中间真正变化的那一段」，然后只对那一段调用 `edit()` 做**增量重解析**（[source.rs:104-112](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/source.rs#L104-L112) 的 `edit` 会增量 reparse）。这就是为什么「就地编辑」能比「从头解析」快得多。

最后看 `source()` 里消费 stale 的那段（Branch A），[files.rs:213-219](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L213-L219)：

```rust
let (result, bytes) = if let Some(mut source) = stale {
    let result = str::from_utf8(without_bom.unwrap_or(&bytes)).map(|new| {
        // 兜里有 stale source → 复用它，就地编辑
        source.replace(new);
        source
    });
    (result, bytes)   // bytes 仍是原始加载的字节
} else { /* Branch B / C，见 4.4 */ };
```

注意两点：

1. 复用 stale 时，调的是 `source.replace(new)`，**不会**走「从头 new 一个 Source」的路径，省掉了全量解析和行号编号。
2. 返回的 `bytes` 是**原始加载的字节**（未被消费）。也就是说，走 stale 路径时，`source`（被编辑过）与 `bytes`（原始）**并不共享内存**——但这没关系，这条路径的优化目标是「增量解析速度」，不是「缓冲共享」。

#### 4.3.4 代码实践

**实践目标**：观察 `reset()` 如何把 `Parsed(Ok)` 的 source 回收为 stale，并在下一轮被复用。

**操作步骤**：

1. 阅读 `test_file_store_cycles`，[files.rs:398-418](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L398-L418)。聚焦与 stale 直接相关的几行：

   ```rust
   store.source(id("d.typ")).must_be("1");   // 第一轮：d.typ 解析成 source（文本 "1"）
   // ... dependencies 收集 ...
   store.loader_mut().0 = 5;                 // 改 loader，模拟「文件内容变了」
   store.reset();                            // reset：d.typ 的 Parsed(Ok) 被回收为 stale
   store.source(id("d.typ")).must_be("5");   // 第二轮：复用 stale，replace("5")，得到新文本
   ```

2. 运行：

   ```bash
   cargo test -p typst-kit test_file_store_cycles
   ```

3. 想象第二轮 `source(id("d.typ"))` 内部发生了什么：`Empty(Some(旧source))` → load 得到字节 `"5"` → 走 Branch A → `source.replace("5")` → 得到文本为 `"5"` 的新 source。

**需要观察的现象**：第二轮 `source` 返回 `"5"`，且 `loader.load` 被重新调用（因为 reset 后内容要重新读），但 `Source` 是复用的——不是 `Source::new` 重新构造的。

**预期结果**：测试通过，`must_be("5")` 成立。

> 说明：仅从测试断言无法直接观察到「replace 被调用而非 new」。若想确认，可在 `files.rs` 的 Branch A 临时加一行 `eprintln!("reused stale via replace");`，再跑测试观察输出。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`reset()` 会回收 `Parsed(Err(Utf8Error))` 里的东西吗？

> **答**：不会。`FileSlot::reset` 只 `match` `Parsed(Ok(source), _)`，其余（包括 `Parsed(Err)`、`Loaded`、`Empty`）都得到 `stale = None`。因为一个 UTF-8 解析失败的 source 没有可复用的解析结果。

**练习 2**：为什么 stale 路径（Branch A）不追求 `bytes` 与 `source` 共享内存？

> **答**：因为这条路径的核心收益是**增量解析**（`source.replace` 只重解析 diff），而非零拷贝。`bytes` 是原始加载字节，`source` 是被就地编辑过的——二者内容可能已经不同（文本被改了），自然无法共享。缓冲共享只在「从头构造、且无 BOM」的 Branch C 才有可能（见 4.4）。

---

### 4.4 UTF-8 BOM 处理与底层缓冲复用

#### 4.4.1 概念说明

这是本讲最精巧的部分。先说目标：当一个文件**没有 stale source 可复用**（首次加载，或 reset 后内容是全新的）时，`source()` 要从零构造一个 `Source`。这时 typst-kit 想顺便做一件聪明事——**让构造出来的 `Source` 和缓存的 `Bytes` 共用同一块底层内存**，这样以后 `file()` 和 `source()` 指向同一份内容，省一半内存、还省一次拷贝。

但这件事有个拦路虎：**UTF-8 BOM**（Byte Order Mark，字节序标记，三字节 `\xef\xbb\xbf`）。很多 Windows 编辑器保存 UTF-8 文件时会自动加这个头部。typst 处理它的策略是：

- 当作 `Source` 时，**剥掉 BOM**——源码文本里不应该有这个标记。
- 当作 `Bytes`（原始文件）时，**保留 BOM**——`file()` 必须返回文件的真实字节。

于是对带 BOM 的文件：`source` 的文本是「去掉 BOM」的，`bytes` 是「带 BOM」的，**两者内容不同**，自然没法共用同一块内存。这就是为什么「带 BOM 的文件无法复用同一块缓冲区」。

`source()` 的「构造 Source」流程因此分成三条分支：

| 分支 | 条件 | 做法 | bytes 与 source 是否共享内存 |
|------|------|------|------------------------------|
| **A：stale 复用** | 兜里有旧 source | `source.replace(new)` 增量编辑 | 否（见 4.3） |
| **B：有 BOM** | 无 stale + 字节以 BOM 开头 | 从「剥掉 BOM 的字节」克隆出一个 `String` 构造 `Source` | **否**（内容不同） |
| **C：无 BOM** | 无 stale + 字节无 BOM | `bytes.into_string()` 夺取底层 `String`/`Vec` 所有权 → 构造 `Source` → `Bytes::from_string(source)` 反向让 bytes 贴回 source | **是**（零拷贝共享） |

C 还藏着一个边界处理：如果 `into_string()` 失败（字节不是合法 UTF-8），它会把原始字节**原样还回来**（装在 `IntoStringError.bytes` 里），于是 `Parsed(Err, bytes)` 仍保留了字节——这就是 4.1 说的「解析失败不丢字节」的实现。

#### 4.4.2 核心流程

完整的「构造 Source」三岔口（位于 `source()` 取得 `(bytes, stale)` 之后）：

```
              bytes 是否以 BOM 开头？
                    │
        ┌───── 有 stale source? ─────┐
        │ 是                         │ 否
        ▼                            ▼
   Branch A: replace(new)      bytes 以 BOM 开头?
   (增量编辑; bytes 原样保留)        │
                              ┌─ 是 ─┴─ 否 ─┐
                              ▼             ▼
                         Branch B       Branch C
                         剥 BOM,         bytes.into_string()
                         克隆 String     ├─ Ok → Bytes::from_string(source)  [共享!]
                         构造 Source     └─ Err → (Utf8Error, 还回的 bytes)   [不丢字节]
```

分支 A、B、C 都最终汇流到 [files.rs:235-236](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L235-L236) 的 `*self = Parsed(result, bytes); Ok(result?)`。

#### 4.4.3 源码精读

整段「构造 Source」逻辑在 [files.rs:209-236](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L209-L236)。先看 BOM 常量的定义与检测，[files.rs:209-210](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L209-L210)：

```rust
const UTF8_BOM: &[u8] = b"\xef\xbb\xbf";
let without_bom = bytes.strip_prefix(UTF8_BOM);   // Some(剩余字节) 若以 BOM 开头，否则 None
```

`strip_prefix` 返回 `Option<&[u8]>`：`Some(rest)` 表示有 BOM（`rest` 是去掉它之后的字节），`None` 表示没有。

然后是三岔口，[files.rs:213-233](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L213-L233)：

```rust
let (result, bytes) = if let Some(mut source) = stale {
    // —— Branch A：复用 stale source ——
    let result = str::from_utf8(without_bom.unwrap_or(&bytes)).map(|new| {
        source.replace(new);   // 增量编辑（见 4.3）
        source
    });
    (result, bytes)            // bytes 原样保留

} else if let Some(rest) = without_bom {
    // —— Branch B：有 BOM，无法共享 ——
    // 把「剥掉 BOM 的字节」转成 UTF-8 字符串，再克隆成一个独立 String 来构造 Source
    (str::from_utf8(rest).map(|text| Source::new(id, text.into())), bytes)
    // ↑ bytes 仍是带 BOM 的原始字节；Source 文本是无 BOM 的 → 内容不同，不能共享

} else {
    // —— Branch C：无 BOM，尝试零拷贝共享 ——
    match bytes.into_string().map(|text| Source::new(id, text)) {
        Ok(source) => (Ok(source.clone()), Bytes::from_string(source)),  // bytes 反贴到 source 上
        Err(err) => (Err(err.error), err.bytes),                          // UTF-8 失败，但字节保住了
    }
};

*self = Self::Parsed(result.clone(), bytes);
Ok(result?)
```

**Branch C 是最值得品味的一行**：

```rust
Ok(source) => (Ok(source.clone()), Bytes::from_string(source)),
```

它一连做了三件事，全靠「所有权搬家」实现零拷贝：

1. `bytes.into_string()`：`bytes` 此刻是 `loader.load()` 返回的、由 `Bytes::new(Vec<u8>)` 包出来的字节（见 `TestLoader`，[files.rs:426-439](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L426-L439)）。`into_string()` 在引用计数为 1 时，**直接把底层的 `Vec<u8>` 夺出来**转成 `String`（顺便校验 UTF-8），不拷贝（[bytes.rs:118-137](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/bytes.rs#L118-L137)）。
2. `Source::new(id, text)`：`text` 这个 `String` 的所有权被搬进 `Source`，成为源码文本的底层缓冲（[source.rs:36-41](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/source.rs#L36-L41)）。
3. `Bytes::from_string(source)`：把刚构造的 `source` 反向塞回一个新的 `Bytes`（[bytes.rs:72-77](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/bytes.rs#L72-L77)）。因为 `Source: AsRef<str>`（见 2.3），这个 `Bytes` 的 `as_bytes()` 就是 `source.text().as_bytes()`——**和 source 的文本指向同一块内存**。

结果：`Parsed(Ok(source), bytes)` 里，`bytes` 与 `source` 共用同一块底层 `String` 缓冲。这正是 `test_file_store_storage_reuse` 用指针相等来断言的东西。

**`Err` 分支同样精妙**：`into_string()` 失败时返回 `IntoStringError { bytes, error }`（[bytes.rs:377-380](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/bytes.rs#L377-L380)）。代码用 `(Err(err.error), err.bytes)` 把**原始字节要回来**装进 `Parsed`，于是即便 source 解析失败，`file()` 仍能返回完整字节。一寸数据都不浪费。

#### 4.4.4 代码实践

**实践目标**：梳理 `test_file_store_storage_reuse` 与 `test_file_store_bom`，画出请求 BOM 文件时的状态迁移图，并解释 BOM 文件为何无法共享缓冲。这是本讲的核心实践任务。

**操作步骤**：

1. 先读两个测试与它们的测试数据，[files.rs:378-395](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L378-L395) 与常量 [files.rs:420-424](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/files.rs#L420-L424)：

   ```rust
   const A_TEXT: &str = "Hello from A";
   const B_DATA: &[u8] = b"\xef\xbb\xbfHello from B";   // 带 BOM 的原始字节
   const B_TEXT: &str = "Hello from B";                  // 剥掉 BOM 后的文本

   // 测试 1：无 BOM，验证缓冲共享（指针相等）
   fn test_file_store_storage_reuse() {
       let store = FileStore::new(TestLoader(1));
       let a_source = store.source(id("a.typ")).unwrap();
       let a_file = store.file(id("a.typ")).unwrap();
       a_file.must_be(A_TEXT);
       a_source.must_be(A_TEXT);
       assert!(std::ptr::eq(a_file.as_slice().as_ptr(), a_source.text().as_ptr()));
   }

   // 测试 2：有 BOM，验证 file 保留 BOM、source 剥掉 BOM
   fn test_file_store_bom() {
       let store = FileStore::new(TestLoader(1));
       store.file(id("b.typ")).must_be(B_DATA);   // file 返回带 BOM 的完整字节
       store.source(id("b.typ")).must_be(B_TEXT); // source 返回无 BOM 的文本
   }
   ```

2. **画出请求 BOM 文件 `b.typ` 时的状态迁移图**。按测试 `test_file_store_bom` 的调用顺序（先 `file` 后 `source`）：

   ```
   初始: Empty(None)
     │ store.file(id("b.typ"))   → loader.load 得 B_DATA（带 BOM）
     ▼                              Empty(stale=None) → Loaded(Ok(B_DATA), None)
   Loaded(Ok(B_DATA), None)
     │ store.source(id("b.typ")) → 取出 (bytes=B_DATA, stale=None)
     │                              without_bom = Some("Hello from B" 的字节)
     │                              走 Branch B（有 BOM，无 stale）：
     │                                Source 由「剥掉 BOM 的克隆字符串」构造 → 文本 B_TEXT
     │                                bytes 仍是 B_DATA（带 BOM）
     ▼                              → Parsed(Ok(source=B_TEXT), bytes=B_DATA)
   Parsed(Ok(B_TEXT), B_DATA)        ← file() 返回 B_DATA，source() 返回 B_TEXT
   ```

3. **解释「为什么带 BOM 的文件无法复用同一块缓冲区」**：

   关键在于 `Parsed` 的两个字段对同一文件内容持有**两种不同的表示**：

   - `bytes`（供 `file()`）= `B_DATA` = `"\xef\xbb\xbf" + "Hello from B"`，**含 BOM**。
   - `source.text()`（供 `source()`）= `B_TEXT` = `"Hello from B"`，**不含 BOM**。

   两者长度差 3 个字节、内容不同。既然内容不同，就不可能指向同一块内存。对比无 BOM 的 `a.typ`：`bytes` 与 `source.text()` 内容完全相同，于是 Branch C 用 `Bytes::from_string(source)` 让它们指向同一个 `String`，`test_file_store_storage_reuse` 的 `std::ptr::eq(...)` 断言才能成立。

4. 运行两个测试确认：

   ```bash
   cargo test -p typst-kit test_file_store_bom test_file_store_storage_reuse
   ```

**需要观察的现象**：
- `test_file_store_bom`：`file()` 返回 `B_DATA`（带 BOM），`source()` 返回 `B_TEXT`（不带 BOM），两者内容不同。
- `test_file_store_storage_reuse`：`file()` 与 `source()` 返回内容相同，且**两个切片的首字节指针完全相等**（`std::ptr::eq` 为真）。

**预期结果**：两个测试均 `... ok`。指针相等只在无 BOM 路径成立。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `test_file_store_storage_reuse` 里的 `a.typ` 换成一个以 BOM 开头的文件，`std::ptr::eq(...)` 还会成立吗？为什么？

> **答**：不成立。带 BOM 时走 Branch B，`source` 的文本是「剥掉 BOM 的克隆字符串」，与 `bytes`（带 BOM）内容不同、指向不同的内存。`ptr::eq` 比较的是两块不同内存的首地址，必然为假。这正是 `test_file_store_bom` 与 `test_file_store_storage_reuse` 形成「对照组」的意义。

**练习 2**：Branch C 的 `Err(err) => (Err(err.error), err.bytes)` 这一行，去掉 `err.bytes`、改成丢弃字节，会有什么后果？

> **答**：一个二进制文件若被当成 `source()` 请求，UTF-8 解码会失败。若丢弃 `err.bytes`，则 `Parsed(Err, _)` 里的 `bytes` 为空或丢失，之后 `file()` 再请求同一文件时，会从 `Parsed(_, bytes)` 返回错误的（空的）字节，而不是原始内容。保留 `err.bytes` 确保「解析失败也不丢数据」，后续 `file()` 仍能返回完整字节。

**练习 3**：为什么 Branch C 能做到零拷贝共享，而 Branch B 做不到？根本差异在哪？

> **答**：根本差异在于**是否需要修改字节内容**。Branch C（无 BOM）里 `source` 直接使用原始字节（`bytes.into_string()` 夺取底层 `Vec`），内容不变，所以能让 `Bytes` 反贴回 `source`、共享同一块内存。Branch B（有 BOM）里 `source` 必须使用「剥掉 BOM 之后」的内容，与原始字节不同，只能克隆一份独立字符串，无法共享。

---

## 5. 综合实践

把本讲的状态机、stale 复用、BOM 处理、缓冲共享串起来，完成一个「对照实验」式的小任务。

**任务**：在 typst-kit 的测试模块里（或一个依赖 typst-kit 的新 cargo 项目里），构造两个文件——一个无 BOM（`plain.typ`）、一个带 BOM（`bom.typ`）——对它们分别走「`source()` → `file()`」序列，观察并解释缓冲共享与否的差异，再跑一轮 `reset()` 观察 stale 复用。

**操作步骤**（**示例代码**，非项目原有；API 以本仓库 HEAD 为准，**待本地验证**）：

```rust
// 示例代码：对照无 BOM 与带 BOM 两种文件的缓冲行为
use typst_kit::files::{FileLoader, FileStore};
use typst_library::foundations::Bytes;
use typst_syntax::{FileId, RootedPath, VirtualPath, VirtualRoot};

// 一个能按路径返回「无 BOM」或「带 BOM」字节的 loader
struct DemoLoader;
impl FileLoader for DemoLoader {
    fn load(&self, id: FileId) -> typst_library::diag::FileResult<Bytes> {
        Ok(match id.vpath().get_without_slash() {
            // 无 BOM：source 与 file 内容一致，可共享
            "plain.typ" => Bytes::new(Vec::from("Hello plain".as_bytes())),
            // 带 BOM：source 剥 BOM、file 保留 BOM，不可共享
            "bom.typ"   => Bytes::new(Vec::from(b"\xef\xbb\xbfHello bom".as_ref())),
            p => return Err(typst_library::diag::FileError::NotFound(p.into())),
        })
    }
}

fn id(path: &str) -> FileId {
    RootedPath::new(VirtualRoot::Project, VirtualPath::new(path).unwrap()).intern()
}

fn main() {
    let store = FileStore::new(DemoLoader);

    // —— 无 BOM 路径：source 与 file 应指针相等 ——
    let s1 = store.source(id("plain.typ")).unwrap();
    let f1 = store.file(id("plain.typ")).unwrap();
    println!("plain: ptr shared = {}",
        std::ptr::eq(f1.as_slice().as_ptr(), s1.text().as_ptr())); // 预期 true

    // —— 带 BOM 路径：内容不同，指针不相等 ——
    let f2 = store.file(id("bom.typ")).unwrap();      // 先 file，返回带 BOM 字节
    let s2 = store.source(id("bom.typ")).unwrap();    // 再 source，文本已剥 BOM
    println!("bom  : file starts with BOM = {}",
        f2.as_slice().starts_with(b"\xef\xbb\xbf"));  // 预期 true
    println!("bom  : source has no BOM   = {}",
        !s2.text().as_bytes().starts_with(b"\xef\xbb\xbf")); // 预期 true
}
```

**需要观察并解释的现象**：

1. `plain` 的 `ptr shared` 为 `true`——因为无 BOM，走了 Branch C，`Bytes::from_string(source)` 让两者共享同一块 `String`。
2. `bom` 的 `file starts with BOM` 为 `true`、`source has no BOM` 为 `true`——因为走 Branch B，source 文本被剥掉 BOM，bytes 保留 BOM，二者内容不同、内存不同。
3. 把 `DemoLoader` 的内容改一改，再 `store.reset()` 后重新 `source(id("plain.typ"))`：第一轮构造的 `Source` 会被回收为 stale，第二轮走 Branch A 的 `source.replace(new)` 增量编辑——可通过在 `files.rs` Branch A 加 `eprintln` 确认（**待本地验证**）。

**预期结果**：上述三个布尔值分别打印 `true`、`true`、`true`。具体导入路径与 `FileId` 构造 API **待本地验证**。

**进阶思考**：为什么 typst-kit 宁可对 BOM 文件放弃缓冲共享，也要剥掉 BOM？因为 Typst 的源码解析器不期望文本里有 BOM，若把它留在 `Source` 文本里，行号、span、错误定位都会偏移 3 个字节。所以「剥 BOM 给 source、留 BOM 给 file」是正确性优先的选择，缓冲共享只能让位。

## 6. 本讲小结

- `FileSlot` 是一个三态状态机：`Empty(Stale<Source>)` → `Loaded(FileResult<Bytes>, Stale<Source>)` → `Parsed(Result<Source, Utf8Error>, Bytes)`。`Parsed` 是终态，此后 `file()`/`source()` 全部命中缓存。
- `file()` 很克制，最多把 `Empty` 推到 `Loaded`，绝不解析；`source()` 一路奔向 `Parsed`，且 `Empty(load Ok)` 可「一跳到 `Parsed`」绕过 `Loaded`。错误也会被缓存（`Loaded(Err)` / `Parsed(Err)`）。
- `reset()` 不清内存，而是用 `mem::take` 把 `Parsed(Ok)` 的 `Source` 回收为 `Stale<Source>`（即 `Option<Source>`），塞回 `Empty`。下一轮 `source()` 用 `Source::replace(new)` **增量编辑**（只重解析 diff），这是 `typst watch` 增量编译快的根源。
- UTF-8 BOM 是 `\xef\xbb\xbf`。`source()` 对它「剥掉」，`file()` 对它「保留」。于是带 BOM 的文件，source 文本与 bytes 内容不同，**无法共享缓冲**；无 BOM 文件则内容一致，可以共享。
- 「构造 Source」分三岔：A（有 stale → `replace` 增量编辑）、B（有 BOM → 克隆独立字符串）、C（无 BOM → `bytes.into_string()` 夺所有权 + `Bytes::from_string(source)` 反贴，实现零拷贝共享；UTF-8 失败时用 `err.bytes` 保住原始字节）。
- `Source: AsRef<str>`（返回 `self.text()`）是 Branch C 能让 `Bytes` 与 `Source` 共享内存的钥匙——`test_file_store_storage_reuse` 用 `std::ptr::eq` 断言了这一点。

## 7. 下一步学习建议

- **u3-l3 SystemFiles 与 FsRoot**：本讲只关心 `FileSlot` 怎么缓存和复用，没管「字节到底从哪个磁盘路径读」。下一讲看 `SystemFiles` 如何按 `id.root()` 分发到项目根或包根，以及 `FsRoot::resolve` 如何把虚拟路径映射到真实路径、拒绝 `..` 逃逸。
- **u7-l2 文件监视 watcher**：带着本讲对 `reset()` / `dependencies()` 的理解去读 watcher——正是 `reset()` 后 `dependencies()` 的结果，决定了 watcher 要监视哪些文件，构成「编译产出依赖 → 监视依赖 → 文件变化 → reset 重编译」的闭环。
- **回看 typst-syntax 的 `Source::edit`/`reparse`**：想彻底理解 4.3 的「增量编辑」为什么快，可以顺着 `Source::replace` → `edit` → `reparse` 读下去，看它如何只对受影响区间重解析语法树。
- 若想确认 `FileSlot` 在真实集成里的表现，回到 typst-cli 的 [SystemWorld](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L25-L38)，看 `FileStore<SystemFiles>` 在 `reset()`、`source()`、`file()` 上的调用时机。
