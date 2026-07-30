# FontStore 与懒加载机制

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `FontStore` 用什么样的数据结构「按序收录」字体，以及它为什么把**元数据**和**字体本体**分开存放。
- 读懂 `FontSlot` + `OnceLock` 这套「先登记、首次访问才真正读盘」的懒加载机制，并能解释为什么多次访问同一个字体不会重复读盘。
- 理解 `FontSource` trait 的设计意图，区分它的两种内置实现：已经加载好的 `Font`（只是克隆自己）和磁盘上的 `FontPath`（真正读文件）。
- 自己实现一个 `FontSource`，并用代码验证「只有第一次访问才触发 `load`」这一行为。

本讲只聚焦 `fonts.rs` 中与「收录 + 懒加载」相关的核心类型，**不**展开字体发现（`embedded`/`scan`/`system`）的细节——那是下一讲 u2-l2 的主题。

## 2. 前置知识

在进入源码前，先建立三个直觉。如果你已学过 u1-l3「模块地图与 World 契约」，这些会很容易接上。

**（1）字体 = 元数据 + 字节本体。**
Typst 在排版时，绝大多数时候只需要字体的「元数据」（叫什么名字、是衬线还是无衬线、字重多少、覆盖哪些字符），只有在**真正要把字形画到页面上**时才需要字体文件的真实字节。把这两件事拆开，就能做到「先 cheap 地登记上千个字体的元数据，等用到哪个再读哪个」。

**（2）`World::font(index)` 是按索引提问的。**
回顾 u1-l3：编译器通过 `World` trait 向外界要字体，调用形式是 `font(index: usize) -> Option<Font>`——它给出一个**整数下标**，要你返回对应的字体。这个下标从哪来？从 `World::book()` 返回的 `FontBook` 里来：`FontBook` 是一份「元数据索引表」，编译器在里面挑选合适的字体，记下下标，再用这个下标回过头来调 `font(index)` 取真身。所以「book 里登记的顺序」和「font(index) 取出的顺序」必须严格一致。

**（3）`OnceLock` 是「一次性初始化」的同步原语。**
`std::sync::OnceLock<T>` 是标准库提供的类型：它内部要么「空」要么「已装了一个值」，且 `get_or_init(closure)` 保证 closure **全局只被调用一次**，并发调用会阻塞等待、绝不会重复执行。本讲的核心技巧，就是给每个字体挂一个 `OnceLock<Option<Font>>`，把昂贵的「读盘 + 解析」塞进那个只跑一次的 closure 里。

> 名词速查：
> - `FontInfo`：字体的元数据（族名、样式、字重等），cheap。
> - `Font`：字体本体，内部用 `Arc` 包裹，克隆很便宜（见后文）。
> - `FontBook`：所有字体元数据的「黄页」，供编译器检索。
> - `LazyHash<FontBook>`：带惰性哈希缓存的 `FontBook`，可直接用来实现 `World::book`。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件：

| 文件 | 作用 |
| --- | --- |
| [crates/typst-kit/src/fonts.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/fonts.rs) | 定义 `FontStore`、`FontSlot`、`FontSource` trait、`FontPath`，以及字体发现函数。本讲只读前半部分。 |

为了看清楚「谁在用它」，还会引用两处外部文件作为佐证：

| 文件 | 作用 |
| --- | --- |
| [crates/typst-cli/src/world.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs) | `SystemWorld` 把 `FontStore` 接进 `World::book` / `World::font`，是 `FontStore` 最重要的使用者。 |
| [crates/typst-library/src/text/font/mod.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/mod.rs) | `Font` 类型的定义，确认 `Font::new` / `Font::iter` / `Font::info` / `Font::clone` 的签名。 |

---

## 4. 核心概念与源码讲解

### 4.1 FontStore：按序收录字体的容器

#### 4.1.1 概念说明

`FontStore` 是字体的「收纳盒」。它的职责非常克制：

1. **按调用顺序**收录一个个字体条目（一个条目 = 「字体来源」+「它的元数据」）。
2. 把每条元数据塞进一张共享的 `FontBook`（黄页），对外通过 `book()` 暴露。
3. 对外提供 `font(index)`：按下标取出（必要时加载）字体本体。

关键设计点：**元数据立刻入册，字体本体先不碰**。也就是说，调用 `push` 时不读盘、不解析字节，只做两件 cheap 的事——把 `FontInfo` 追加进 `FontBook`，把「字体来源」存进一个槽位（`FontSlot`，见 4.2）等以后再说。`FontBook` 里的下标与槽位数组的下标天然对齐（都是 `push` 的先后顺序），这就保证了「book 检索得到的下标」能直接喂给 `font(index)`。

#### 4.1.2 核心流程

`FontStore` 的生命周期可以画成三段：

```text
            ┌─────────────── 建立阶段（cheap）───────────────┐
 new()  ──► │ book = 空 FontBook ; slots = 空 Vec            │
            └────────────────────────────────────────────────┘
                                  │  push((source, info)) 可多次调用
                                  ▼
            ┌─────────────── 收录阶段（cheap）───────────────┐
            │ book.push(info)        // 元数据立刻入册        │
            │ slots.push(FontSlot{source, OnceLock::空})      │
            └────────────────────────────────────────────────┘
                                  │  编译器开始排版
                                  ▼
            ┌─────────────── 服务阶段（按需 expensive）──────┐
            │ book()      → 直接返回 &book（给编译器检索）   │
            │ font(i)     → 触发 slots[i] 的懒加载（见 4.2） │
            └────────────────────────────────────────────────┘
```

建立和收录都很轻；真正可能昂贵的「读盘 + 解析」被推迟到服务阶段，而且只发生在**被访问到**的那个槽位上。

#### 4.1.3 源码精读

`FontStore` 的结构体只有两个字段：

```rust
pub struct FontStore {
    book: LazyHash<FontBook>,
    slots: Vec<FontSlot>,
}
```

> 见 [fonts.rs:24-27](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/fonts.rs#L24-L27)：`book` 是对外暴露的元数据黄页，`slots` 是与之一一对应的字体槽位数组。两个字段都按 `push` 顺序增长，下标对齐。

收录逻辑全部落在 `push` 上，`extend` 只是循环 `push`：

```rust
pub fn push(&mut self, entry: (impl FontSource, FontInfo)) {
    self.book.push(entry.1);
    self.slots
        .push(FontSlot { source: Box::new(entry.0), font: OnceLock::new() });
}
```

> 见 [fonts.rs:38-43](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/fonts.rs#L38-L43)。注意三件事：① 参数是元组 `(impl FontSource, FontInfo)`，「来源」在前、「元数据」在后；② 元数据 `entry.1` 立刻塞进 `book`；③ 来源被装进 `Box<dyn FontSource>` 存入 `FontSlot`，同时挂一个**全新的、空的** `OnceLock`——此时完全没有调用 `load()`。

对外服务的方法很薄。`book()` 直接返回黄页引用，可直接当作 `World::book` 的实现：

```rust
pub fn book(&self) -> &LazyHash<FontBook> { &self.book }
```

> 见 [fonts.rs:55-61](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/fonts.rs#L55-L61)。

`font(index)` 把工作下放给对应槽位的 `get()`（4.2 节会看到它才是懒加载的真正发生地）：

```rust
pub fn font(&self, index: usize) -> Option<Font> {
    self.slots.get(index)?.get()
}
```

> 见 [fonts.rs:63-71](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/fonts.rs#L63-L71)。越界时 `slots.get(index)` 返回 `None`，于是整个方法优雅地返回 `None`，对应 `World::font` 契约里「下标不存在就返回 `None`」。

`source(index)` 是个容易被忽略的方法：它返回该下标对应的 `&dyn FontSource`，**注意它不会触发加载**——只是把槽位里那个来源对象的引用递出来，方便上层在不读盘的前提下检视来源（比如 4.4 节会看到 `FontPath` 的路径信息）：

```rust
pub fn source(&self, index: usize) -> Option<&dyn FontSource> {
    Some(&*self.slots.get(index)?.source)
}
```

> 见 [fonts.rs:73-76](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/fonts.rs#L73-L76)。记住这个区别：`source()` 不加载，`font()` 才加载。

**它与 World 契约的衔接**。在 typst-cli 的 `SystemWorld` 里，这两个方法几乎被原样转发：

```rust
fn book(&self) -> &LazyHash<FontBook> { self.fonts.book() }
...
fn font(&self, index: usize) -> Option<Font> { self.fonts.font(index) }
```

> 见 [world.rs:122-124](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L122-L124) 与 [world.rs:138-140](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L138-L140)。这印证了 u1-l3 的结论：`FontStore` 就是 CLI 实现 World 字体回调的现成积木。

> 顺带一提：CLI 还在 `FontStore` 外面又套了一层 `LazyLock<FontStore>`（见 [world.rs:31](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L31)），把「字体发现」（扫描系统、扫描目录）也推迟到第一次访问时才执行（可用 `scan_fonts` 强制触发，见 [world.rs:112-114](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/world.rs#L112-L114)）。所以 CLI 实际上有**两层**惰性：外层推迟「发现整张字体表」，内层（本讲的 `OnceLock`）推迟「读取单个字体字节」。本讲聚焦内层。

#### 4.1.4 代码实践

**实践目标**：亲手把字体收进 `FontStore`，体会「元数据立刻可查、字节按需加载」。

**操作步骤**（示例代码）：

```rust
// Cargo.toml 需要开启 typst-kit 的 embedded-fonts 特性，并依赖 typst-library
//   typst-kit = { path = "...", features = ["embedded-fonts"] }
//   typst-library = { path = "..." }
use typst_kit::fonts::FontStore;

fn main() {
    let mut store = FontStore::new();

    // embedded() 返回已经加载好的 (Font, FontInfo) 迭代器，逐条收录
    store.extend(typst_kit::fonts::embedded());

    // book() 此刻已包含全部内置字体的元数据（cheap，未触发任何额外读盘）
    let _book = store.book();

    // 取第一个字体本体——这才是「可能昂贵」的一步
    let first = store.font(0).expect("至少应有一个内置字体");
    println!("第一个字体的 PostScript 名: {:?}", first.post_script_name());

    // 越界下标优雅返回 None
    assert!(store.font(usize::MAX).is_none());
}
```

**需要观察的现象**：`book()` 在 `font(0)` 之前就已经能提供完整元数据；`font(usize::MAX)` 返回 `None` 而不是 panic。

**预期结果**：打印出一个内置字体（如 DejaVu Sans Mono）的 PostScript 名；越界断言通过。

> 说明：`Font::post_script_name()` 返回 `Option<String>`，是 typst-library 公开 API（见 [font/mod.rs:101](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/mod.rs#L101)）。如本地无法运行，请标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `FontBook` 的下标可以直接当作 `font(index)` 的参数？

> **参考答案**：因为 `push` 时元数据和槽位是**同一次**追加进 `book` 与 `slots` 的，两者长度始终相等、顺序一致。所以「book 中第 i 条元数据」对应的正是「slots 中第 i 个槽位」，下标天然对齐。

**练习 2**：调用 `store.source(0)` 会不会触发字体字节的读取？

> **参考答案**：不会。`source()` 只是返回槽位里 `Box<dyn FontSource>` 的引用（[fonts.rs:73-76](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/fonts.rs#L73-L76)），既不调用 `load()`，也不碰 `OnceLock`。只有 `font()` → `FontSlot::get()` 才会触发加载。

---

### 4.2 FontSlot 与 OnceLock 懒加载

#### 4.2.1 概念说明

`FontSlot` 是 `FontStore` 内部的私有类型，也是整个懒加载机制的心脏。每个槽位持有两样东西：

- `source: Box<dyn FontSource>`——「这个字体从哪儿来、怎么读」的来源对象；
- `font: OnceLock<Option<Font>>`——一个「最多被填一次」的缓存格。

思路是：**收录时只放来源、缓存格留空；第一次有人要字体时，才调用来源的 `load()` 把结果填进缓存格；之后所有人共享这格里的值。** 缓存格装的是 `Option<Font>` 而不是 `Font`，是为了表达「加载过了，但加载失败/没有字体」这种终态——失败也会被记住，避免每次都重试一次注定失败的读盘。

为什么用 `OnceLock` 而不是 `RefCell` 或 `Mutex`？因为 `World` 在编译期间会被多个线程并发访问（Typst 用 rayon 做并行排版）。`OnceLock` 是 `Sync` 的，允许多线程安全地「读取或一次性初始化」，且初始化逻辑全局只跑一次——既线程安全，又把读盘成本严格压到一次。

#### 4.2.2 核心流程

单个槽位的状态迁移极其简单，只有「未初始化 → 已初始化」一步：

```text
  font = OnceLock::空（未初始化）
              │
              │  首次调用 get()
              │  get_or_init(|| source.load())
              ▼
  font = 已装入 Option<Font>（已初始化，终身不变）
              │
              │  之后任何调用 get()
              ▼
  直接返回缓存里的 Option<Font>，不再调用 load()
```

把成本摊到多次访问上，单字体访问的摊还代价可写作：

\[ \text{cost}(\text{access}) = \begin{cases} O(\text{read+parse}) & \text{首次访问} \\ O(1) & \text{之后所有访问} \end{cases} \]

也就是说，无论某个字体被编译器问多少次，真正的读盘+解析全局只发生一次。对一份只用到几十个字体、却登记了上千个候选字体的文档，这能省下大量无谓的 I/O。

#### 4.2.3 源码精读

`FontSlot` 的定义：

```rust
struct FontSlot {
    source: Box<dyn FontSource>,
    font: OnceLock<Option<Font>>,
}
```

> 见 [fonts.rs:85-89](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/fonts.rs#L85-L89)。注意 `font` 字段的类型是 `OnceLock<Option<Font>>`——外层 `OnceLock` 管「有没有初始化过」，内层 `Option` 管「加载的结果是成功还是空」。

懒加载的全部魔法集中在 `get()` 这一行：

```rust
impl FontSlot {
    fn get(&self) -> Option<Font> {
        self.font.get_or_init(|| self.source.load()).clone()
    }
}
```

> 见 [fonts.rs:91-97](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/fonts.rs#L91-L97)。逐字拆解：
> - `get_or_init(closure)`：若缓存格已填，直接返回里面的引用；若未填，**在锁保护下**执行 closure，把结果存进去再返回。closure 全局只跑一次。
> - closure 是 `|| self.source.load()`：这才真正调用了来源对象的 `load()`，也就是 4.3、4.4 要讲的「读盘/克隆」动作。
> - 末尾 `.clone()`：`get_or_init` 返回的是 `&Option<Font>`，这里克隆出一份 `Option<Font>` 返回。因为 `Font` 内部是 `Arc`（见 [font/mod.rs:40-41](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/mod.rs#L40-L41)，`Font(Arc<FontInner>)`），所以这次 clone 只是增加一次引用计数，非常 cheap。

把这条调用链串起来：`World::font(i)` → `FontStore::font(i)` → `slots[i].get()` → `OnceLock::get_or_init(|| source.load())`。除第一次外，每次都只走「读缓存 + clone」这条快路径。

#### 4.2.4 代码实践

**实践目标**：通过源码阅读，确认「`font()` 触发加载、`source()` 不触发」这一对区别，并理解缓存命中后的快路径。

**操作步骤**：

1. 打开 [fonts.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/fonts.rs)，定位 `FontSlot::get`（[第 94-96 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/fonts.rs#L94-L96)）。
2. 沿调用链向上：`FontStore::font`（[第 69-71 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/fonts.rs#L69-L71)）会调到 `get()`；而 `FontStore::source`（[第 74-76 行](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/fonts.rs#L74-L76)）只取 `&self.source`，**不**调 `get()`。
3. 在 4.3 节的可运行实践中，你会用「计数器」亲眼看到 `load()` 只被调用一次——那正是 `get_or_init` 在起作用。

**需要观察的现象**：调用链上 `load()` 只出现在 `FontSlot::get` 这一处；`source()` 的实现里完全没有 `load` 字样。

**预期结果**：能在源码中清晰指出「触发加载的唯一入口是 `FontSlot::get` 里的 `self.source.load()`」。

#### 4.2.5 小练习与答案

**练习 1**：缓存格为什么是 `OnceLock<Option<Font>>` 而不是 `OnceLock<Font>`？

> **参考答案**：为了区分「还没加载过」和「加载过了、但结果是空」两种状态。外层 `OnceLock` 表达「有没有初始化」，内层 `Option` 表达「加载成功与否」。若用 `OnceLock<Font>`，就无法缓存「这个来源确实没有字体」的结论，每次访问都会重试一次注定失败的 `load()`。

**练习 2**：多个线程同时第一次访问同一个 `FontSlot`，`load()` 会被调用几次？

> **参考答案**：恰好一次。`OnceLock::get_or_init` 在内部用同步原语保证：第一个进入的线程执行 closure，其余线程阻塞等待，结果就绪后共享。这正是 `FontSource` 必须满足 `Send + Sync` 的原因。

---

### 4.3 FontSource trait：字体来源的抽象

#### 4.3.1 概念说明

`FontSource` 是「字体从哪儿来」的抽象，只有一个方法：

```rust
pub trait FontSource: Send + Sync + Any {
    fn load(&self) -> Option<Font>;
}
```

> 见 [fonts.rs:99-103](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/fonts.rs#L99-L103)。

理解三个细节：

- **只有一个方法 `load()`**：来源对象的全部职责，就是「按需交出一个 `Font`」。返回 `Option` 表示可能失败（文件丢了、不是合法字体等）。
- **`Send + Sync`**：因为 `FontSlot` 会在多线程下被并发访问（4.2 已述），来源必须能跨线程共享。
- **`Any`**：让来源对象支持运行时类型检视（downcast）。配合 `FontStore::source()` 返回的 `&dyn FontSource`，上层可以在不加载的前提下，把来源 downcast 成具体类型（比如取出 `FontPath` 里的路径，用于依赖追踪）。

正因为这是个 trait，`FontStore` 才能做到「收录时统一存放 `Box<dyn FontSource>`，加载时再各自表现」。typst-kit 内置了两种实现：已经加载好的 `Font`，和磁盘上的 `FontPath`（4.4 节）。你也可以自己实现它（见下方实践）。

#### 4.3.2 核心流程

```text
   收录时                    首次访问时
   ───────                   ──────────────────────
   具体来源 ──Box化──► dyn FontSource ──load()──► Option<Font>
   （Font / FontPath /       （被 get_or_init 缓存）
    你的自定义来源）
```

#### 4.3.3 源码精读

最朴素的实现是 `Font` 自己——它已经是一个加载好的字体，`load()` 只需克隆自己：

```rust
impl FontSource for Font {
    fn load(&self) -> Option<Font> {
        Some(self.clone())
    }
}
```

> 见 [fonts.rs:105-109](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/fonts.rs#L105-L109)。因为 `Font` 内部是 `Arc`（[font/mod.rs:40-41](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/mod.rs#L40-L41)），`clone` 只是加引用计数。这也是 `embedded()` 返回 `(Font, FontInfo)` 能直接 `extend` 进 `FontStore` 的原因——`Font` 本身就满足 `FontSource`。

这个 impl 揭示了一个重要事实：**对 `embedded()` 这类字体，「懒加载」几乎不省钱**，因为来源已经是完整的 `Font`，`load()` 只是 clone。懒加载真正大显身手的是 `FontPath` 这类**磁盘来源**——下一节详述。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：实现一个带「计数器」的自定义 `FontSource`，把字体字节放在内存里，用 `FontStore::push` 收录它，然后多次调用 `font(0)`，**亲眼看到 `load()` 只被调用一次**。

**操作步骤**（示例代码）：

```rust
// Cargo.toml（示例）：
//   typst-kit      = { path = "...", features = ["embedded-fonts"] }
//   typst-library  = { path = "..." }
//   typst-assets   = { path = "...", features = ["fonts"] }   // 用来拿真实字体字节
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::Arc;
use typst_kit::fonts::{FontSource, FontStore};
use typst_library::foundations::Bytes;
use typst_library::text::{Font, FontInfo};

/// 一个会「计数 + 打印」的自定义字体来源：把字节放在内存里，
/// 每次 load() 都自增计数器，以验证它究竟被调了几次。
struct CountingSource {
    data: Vec<u8>,
    index: u32,
    load_count: Arc<AtomicU32>,
}

impl FontSource for CountingSource {
    fn load(&self) -> Option<Font> {
        let n = self.load_count.fetch_add(1, Ordering::SeqCst);
        println!("   >> load() 被调用了（第 {} 次）", n + 1);
        Font::new(Bytes::new(self.data.clone()), self.index)
    }
}

fn main() {
    // 1) 取得真实字体字节：借助 typst-assets 的内置字体
    let data = typst_assets::fonts().next().expect("至少有一个内置字体").to_vec();

    // 2) 解析出对应的 FontInfo，作为「元数据」入册
    let probe = Font::new(Bytes::new(data.clone()), 0).expect("字体可解析");
    let info: FontInfo = probe.info().clone();

    let load_count = Arc::new(AtomicU32::new(0));
    let source = CountingSource {
        data,
        index: 0,
        load_count: load_count.clone(),
    };

    // 3) 收录：元数据立刻进 book，来源进 slot，此时 load_count 仍是 0
    let mut store = FontStore::new();
    store.push((source, info));
    println!("push 之后，load_count = {}", load_count.load(Ordering::SeqCst));

    // 4) 连续访问三次
    for i in 1..=3 {
        println!("第 {} 次调用 font(0)：", i);
        let _ = store.font(0);
    }

    println!("最终 load_count = {}", load_count.load(Ordering::SeqCst));
}
```

**需要观察的现象**：`push` 之后 `load_count` 为 `0`（证明收录不触发加载）；三次 `font(0)` 只有第一次打印 `>> load()`；最终 `load_count = 1`。

**预期结果**：输出形如

```text
push 之后，load_count = 0
第 1 次调用 font(0)：
   >> load() 被调用了（第 1 次）
第 2 次调用 font(0)：
第 3 次调用 font(0)：
最终 load_count = 1
```

这直接验证了 `OnceLock::get_or_init` 把 `load()` 压成一次的效果。若本地缺 `typst-assets`，可改用 `std::fs::read` 读取任意 `.ttf`/`.otf` 字体文件得到 `data`；其余代码不变。

> 说明：上述为示例代码。`Font::new(data: Bytes, index: u32) -> Option<Self>` 与 `Font::info(&self) -> &FontInfo` 的签名见 [font/mod.rs:63](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/mod.rs#L63) 与 [font/mod.rs:96](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/mod.rs#L96)。`Bytes::new` 接受 `Vec<u8>`（见 [fonts.rs:114-115](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/fonts.rs#L114-L115) 里 `Bytes::new(data)`，`data` 来自 `fs::read`）。如本地运行结果与预期不符，请标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `FontSource` 要加 `Any` bound？

> **参考答案**：`Any` 允许把 `&dyn FontSource` 运行时 downcast 回具体类型。典型用途：上层通过 `FontStore::source(i)` 拿到 `&dyn FontSource` 后，把它 downcast 成 `&FontPath`，取出磁盘路径——全程不调用 `load()`，不读盘，就能拿到路径信息做依赖追踪（typst-cli 的 `dependencies()` 就需要这类信息）。

**练习 2**：如果把上面 `CountingSource` 的 `load()` 改成「每次都返回 `None`」，多次访问 `font(0)` 时 `load()` 会被调用几次？

> **参考答案**：仍然只有一次。`OnceLock` 缓存的是 `Option<Font>`，`None` 同样是「已初始化」的合法值，会被原样缓存。这正是用 `Option<Font>` 而非 `Font` 的好处——失败结果也被记住，不会反复重试。

**练习 3**：对 `embedded()` 产生的字体（来源就是 `Font`），懒加载省下了什么？

> **参考答案**：几乎什么都没省。因为 `Font` 的 `load()` 只是 `self.clone()`（[fonts.rs:105-109](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/fonts.rs#L105-L109)），第一次和后续成本都只是引用计数。懒加载的价值主要体现在磁盘来源（`FontPath`）上。

---

### 4.4 FontPath：磁盘来源的内置实现

#### 4.4.1 概念说明

`FontPath` 是 typst-kit 自带的、面向「字体文件在磁盘上」的来源类型。它记录两样东西：文件路径，以及该字体在**字体集合**（一个 `.ttc` 里多个字体）中的下标。它的 `load()` 才是真正会「读盘 + 解析」的实现——也正因为如此，把它和 `OnceLock` 配合，才体现出懒加载的全部价值：扫描时只记路径（cheap），用到时才真正读字节（expensive，且只读一次）。

#### 4.4.2 核心流程

```text
   FontPath { path, index }
            │
            │  load()
            ▼
   fs::read(path)  ──►  Vec<u8>      // 真正的磁盘 I/O（可能失败 → 返回 None）
            │
            ▼
   Font::new(Bytes::new(data), index) ──► Option<Font>   // 解析字体
```

整个过程被一个 `typst_timing::TimingScope` 包裹，用于性能追踪（如果你启用了 `timer`，会在追踪文件里看到名为 `load font` 的区段）。

#### 4.4.3 源码精读

`FontPath` 的结构：

```rust
#[derive(Debug)]
pub struct FontPath {
    pub path: PathBuf,
    pub index: u32,
}
```

> 见 [fonts.rs:119-127](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/fonts.rs#L119-L127)。两个字段都是 `pub`：`path` 是字体文件路径，`index` 是字体集合中的下标（单字体文件则用 `0`）。

它的 `FontSource` 实现：

```rust
impl FontSource for FontPath {
    fn load(&self) -> Option<Font> {
        let _scope = typst_timing::TimingScope::new("load font");
        let data = fs::read(&self.path).ok()?;
        Font::new(Bytes::new(data), self.index)
    }
}
```

> 见 [fonts.rs:111-117](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/fonts.rs#L111-L117)。逐句：
> - `let _scope = typst_timing::TimingScope::new("load font");`——打开一个计时区段，变量名 `_scope` 表示「我们不读它的值，只利用它的 `Drop`：离开作用域时自动记录这段耗时」。这是 typst 生态里性能埋点的惯用法。
> - `fs::read(&self.path).ok()?`——同步读整个文件到 `Vec<u8>`；失败（文件不存在、无权限等）转成 `None` 返回。
> - `Font::new(Bytes::new(data), self.index)`——用字节和集合下标解析出 `Font`；若不是合法字体，`Font::new` 自身返回 `None`（见 [font/mod.rs:63-77](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/mod.rs#L63-L77)）。

把 4.2、4.3、4.4 连起来看，就得到了 `FontPath` 的完整懒加载图景：扫描阶段（下一讲 u2-l2 的 `scan`/`system`）只产生大量 `FontPath` 与对应 `FontInfo`，`push` 进 `FontStore` 时不读盘；编译期间真正用到某个字体时，`FontSlot::get` → `FontPath::load` 才第一次、也是唯一一次读那一份字节。

#### 4.4.4 代码实践

**实践目标**：通过源码阅读，理解 `FontPath::load` 的两处失败点，以及 `_scope` 计时惯用法。

**操作步骤**：

1. 阅读 [fonts.rs:111-117](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/fonts.rs#L111-L117)，列出 `load()` 可能返回 `None` 的所有路径。
2. 思考：如果同一个 `FontPath` 被两次 `push` 进 `FontStore`（下标 i 和 j），且文档都用到它们，磁盘会被读几次？
3. （可选）修改设想：如果把 `let _scope = …` 这一行删掉，功能是否会变化？性能观测会变化什么？

**需要观察的现象**：`load()` 有两处可能产出 `None`——`fs::read` 失败（`.ok()?`）、`Font::new` 解析失败。

**预期结果**：
1. 两处失败点：文件读取失败、字节不是合法字体。
2. 同一 `FontPath` 被 `push` 两次会形成**两个独立 `FontSlot`**，各自有自己的 `OnceLock`，因此磁盘会被读**两次**（`OnceLock` 是「每槽位一次」，不是「每路径一次」）。这是一个容易踩的误解，值得记住。
3. 删掉 `_scope` 不影响正确性，只是 `load font` 这个区段不再出现在性能追踪里。

> 说明：本实践为「源码阅读 + 推理型」，结论 2 若要在本地验证，可仿照 4.3 的计数器思路，写一个记录路径被读次数的来源，分别 push 两次后都访问，观察读次数。

#### 4.4.5 小练习与答案

**练习 1**：`FontPath::index` 字段什么时候不是 `0`？

> **参考答案**：当 `path` 指向一个**字体集合**文件（如 `.ttc`，TrueType Collection，一个文件内含多个字体）时，`index` 用来选取其中的第几个字体。单字体文件则用 `0`。`Font::new` 的 `index` 参数会传给 `ttf_parser::Face::parse`（见 [font/mod.rs:73](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/text/font/mod.rs#L73)）。

**练习 2**：`let _scope = typst_timing::TimingScope::new("load font");` 这行代码是靠什么机制计时的？

> **参考答案**：靠 `Drop` trait。`TimingScope` 在创建时记录起始时间，在变量离开作用域被销毁（drop）时计算经过的耗时并上报。变量名前缀 `_` 表示不使用其值，但仍然保留其生命周期到作用域结束——这正是 RAII 计时的惯用法。

---

## 5. 综合实践

把本讲的四个最小模块串起来，完成下面这个「迷你字体仓库」任务。

**任务**：编写一个小程序，模拟「登记若干字体、按需加载、统计真实读盘次数」。

要求：

1. 用 `FontStore::new()` 建一个空仓库。
2. 收录**三类**字体条目，各至少一个：
   - 一个 `Font`（直接来自 `fonts::embedded()`，来源是已加载字体）；
   - 一个自定义的 `CountingSource`（仿 4.3.4，把字节放内存，并计数 `load()`）；
   - 一个 `FontPath`（指向磁盘上某个真实字体文件，如 `/usr/share/fonts/.../*.ttf`；没有的话可省略此项并说明）。
3. 在收录完成后，打印一次「各来源的 load 计数」，确认它们都是 `0`（收录不触发加载）。
4. 调用 `font(0)`、`font(1)`（、`font(2)`）各两次，再次打印计数。
5. 用一段文字解释观察到的现象，回答：哪类来源的 `load()` 被调用过？被调用了几次？为什么 `Font` 类来源即便被「加载」也很 cheap？

**验收要点**：

- 收录后所有计数为 0；
- 无论访问几次，每个槽位的 `load()` 至多被调用一次；
- 能指出「`Font` 的 `load()` 只是 clone（[fonts.rs:105-109](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/fonts.rs#L105-L109)），而 `FontPath` 的 `load()` 才真正读盘（[fonts.rs:111-117](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/fonts.rs#L111-L117)」。

> 若本地没有合适的磁盘字体文件，可只做前两类来源，并标注「FontPath 项待本地验证」。

## 6. 本讲小结

- `FontStore` 是字体的「收纳盒」：用 `book: LazyHash<FontBook>` + `slots: Vec<FontSlot>` 两个字段，把**元数据**与**来源**按同一顺序收录，保证 `book` 的下标可直接喂给 `font(index)`。
- 收录（`push`/`extend`）非常 cheap：元数据立刻入册，来源装进 `Box<dyn FontSource>` 存入 `FontSlot`，并挂一个空的 `OnceLock`——**全程不读盘**。
- 懒加载的心脏是 `FontSlot::get` 里的 `self.font.get_or_init(|| self.source.load())`：`OnceLock` 保证 `load()` 全局只跑一次，结果（含失败结果 `None`）被缓存，后续访问只走「读缓存 + clone」的快路径。
- `FontSource` trait（`load() -> Option<Font>`，带 `Send + Sync + Any`）统一了字体来源；内置两种实现：`Font`（克隆自己，cheap）与 `FontPath`（读盘 + 解析，expensive，外裹 `TimingScope` 计时）。
- `source(index)` 与 `font(index)` 的关键区别：前者只返回 `&dyn FontSource`，**不触发加载**；后者才会触发加载。
- 真正能体现懒加载价值的是磁盘来源 `FontPath`——这也是下一讲「字体发现」要大量产出的类型。

## 7. 下一步学习建议

本讲只讲了「字体怎么存、怎么按需加载」，还没讲「字体从哪儿来」。下一讲 **u2-l2《字体发现三连：embedded / scan / system》** 会接着讲 `fonts.rs` 后半部分的三个顶层函数：

- [`embedded()`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/fonts.rs#L134-L142)：从 `typst-assets` 取内置字体；
- [`scan(path)`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/fonts.rs#L162-L166) 与 [`system()`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-kit/src/fonts.rs#L147-L157)：借助 `fontdb` 递归扫描目录、发现系统字体，产出本讲反复出现的 `(FontPath, FontInfo)`。

建议阅读顺序：先复习本讲的 `FontStore::extend`，再读 u2-l2 的三个发现函数，体会「发现 → 收录 → 懒加载」这条完整链条。如果想顺带了解性能追踪里 `load font` 区段的去向，可预习 u8-l1《Timer 性能追踪》。
