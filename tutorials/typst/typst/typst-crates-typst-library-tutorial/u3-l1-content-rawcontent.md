# Content 与 RawContent：文档内容的内核

## 1. 本讲目标

本讲是「Content 与元素系统」单元的第一讲。学完本讲，你应当能够：

- 说清楚 `Content` 类型在 Typst 中的核心地位——为什么「所有标记和函数调用的产物都是 content」。
- 理解 `Content` 与底层 `RawContent` 的分层关系：一个是面向用户的稳定外壳，一个是手写胖指针 + 引用计数的高性能内核。
- 掌握 content 如何携带 `span`/`label`/`location`/生命周期标记等元信息，并能解释「label 是如何让 `query` 找到元素的」。
- 看懂 `+` 运算符如何把多个 content 拼接成 `SequenceElem`，以及 `StyledElem` 如何把样式挂在内容上。
- 学会阅读 `Content` 上的字段访问方法（`get`/`field`/`fields`/`has`/`at`），并区分两种字段访问错误。

本讲只讲 `Content` 本身的表示与拼接，**不**深入元素能力（vtable/`Element`/`NativeElement`，那是下一讲 u3-l2）和样式系统的折叠机制（u4）。

## 2. 前置知识

在开始前，请确保你已经理解以下概念（它们来自前两个单元）：

- **`Value` 枚举**（u2-l1）：Typst 运行时所有值的总和类型。其中有一个变体就叫 `Content`，即 `Value::Content(Content)`。本讲讲的就是这个变体背后的真实类型。
- **`Label`**（u2-l2）：一个内部用 `PicoStr` 表示、靠字符串驻留做到 O(1) 克隆/比较/哈希的「标签」类型，形如 `<intro>`。本讲会用到它。
- **容器与引用计数**（u2-l2）：`Array`/`Dict`/`Bytes` 都靠「引用计数 + 写时复制」实现廉价克隆。本讲的 `RawContent` 是同一思想的手写极致版。
- **cast 三段式**（u2-l3）：`Reflect`/`IntoValue`/`FromValue`。本讲会出现 `IntoValue for T: NativeElement`，把任意元素变成 `Value::Content`。
- **标准库装配**（u1-l3）：`#[ty]`/`#[scope]`/`#[func]` 等宏如何把 Rust 定义注册进标准库。

几个本讲会用到的 Rust 术语，先做通俗解释：

- **胖指针（fat pointer）**：普通指针只存「地址」；胖指针额外存一段「类型信息」。Rust 的 `&dyn Trait` 就是编译器生成的胖指针（数据指针 + vtable 指针）。Typst 没用编译器自带的，而是**手写**了一套等价机制，以便塞进更多自定义信息。
- **引用计数（reference counting, 类似 `Arc`）**：一块堆内存上记一个数字 `refs`，每克隆一次 +1，每销毁一次 -1，降到 0 时才真正释放。这样多个所有者可以共享同一份数据，克隆只花 O(1)。
- **写时复制（copy-on-write, CoW）**：克隆时先共享，等到真正要修改的那一刻，才复制出独占的一份。Typst 的 `make_unique` 就是这个机制。
- **vtable（虚函数表）**：一张「函数指针表」，让类型被擦除后仍能调用原本的方法。本讲只需建立「它存在」的印象，细节在 u3-l2。

## 3. 本讲源码地图

本讲聚焦 `foundations/content/` 子目录。它被拆成多个文件，本讲主要读前两个：

| 文件 | 作用 | 本讲是否精读 |
| --- | --- | --- |
| `src/foundations/content/mod.rs` | 定义对外类型 `Content`、`SequenceElem`、`StyledElem`，以及 `+`/序列化等行为 | ✅ 精读 |
| `src/foundations/content/raw.rs` | 定义底层 `RawContent`：手写胖指针、引用计数、`Header`/`Inner`/`Meta` | ✅ 精读 |
| `src/foundations/content/field.rs` | 字段访问器 `Field`、`Settable`、以及字段访问错误 `FieldAccessError` | 部分引用 |
| `src/foundations/content/vtable.rs` | 自定义 vtable，支撑类型擦除后的方法分发 | 轻点（细节留到 u3-l2） |
| `src/foundations/content/packed.rs` | `Packed<T>` 包装，把擦除后的 content 安全地取回具体元素类型 | 轻点（细节留到 u3-l3） |
| `src/foundations/content/element.rs` | `Element` 句柄、`NativeElement` trait | 留到 u3-l2 |
| `src/foundations/selector.rs` | `Selector`，含 `Selector::Label`，串起「label → query」 | 引用关键一行 |

读源码时请记住这条主线：**用户和编译器只和 `Content` 打交道；`Content` 把所有脏活累活转发给 `RawContent`；`RawContent` 用手写胖指针管理一坨堆内存。**

## 4. 核心概念与源码讲解

### 4.1 Content：文档内容的统一表示

#### 4.1.1 概念说明

在 Typst 里，你写下的每一段标记（`*Hello*`、`= 标题`、`#rect[...]`），以及绝大多数函数调用的返回值，归根结底都是**同一个类型**——`Content`。它的官方文档注释开宗明义：

> A piece of document content. ... This type is at the heart of Typst.

可以把 `Content` 想象成「一个文档元素盒子」。一个 content 盒子里可能装着：

- 一个具体的元素（一段文字、一个矩形、一个标题）；
- 一串元素（`SequenceElem`，由 `+` 拼接而来）；
- 一个带样式的元素（`StyledElem`，由 set/show 规则产生）。

`Content` 之所以能成为「万能胶水」，是因为它对类型做了**擦除**：盒子不知道自己装的是 `TextElem` 还是 `RectElem`，只持有一个类型标识 `Element` 和一段数据指针。需要具体类型时，再通过 `to_packed::<T>()`「试取回」。这种设计让 content 可以被任意拼接、放进容器、统一序列化，而不必为每种元素写一份容器。

`Content` 还是一个**一等类型**：它被 `#[ty(scope, cast)]` 注册为标准库类型 `content`，于是你可以在 Typst 脚本里写 `type([*Hi*])` 得到 `content`，并调用它的方法 `.func()`、`.fields()`、`.at()` 等。

#### 4.1.2 核心流程

一个 content 的「一生」大致是：

```text
源码标记 / 函数调用
        │  解析 & 求值（typst-eval，行为在别的 crate）
        ▼
   构造一个具体元素 E（如 HeadingElem）
        │  Content::new(E)  或  E.pack()（经 IntoValue）
        ▼
        Content  ──┐
                   ├── 用 + 拼成 SequenceElem
                   ├── 用 .styled() 包成 StyledElem
                   ├── 用 .labelled() / .spanned() / .located() 贴元信息
                   │
        ▼  realize / layout（行为在别的 crate）
      Frame 帧树（最终输出）
```

关键点：**构造和拼接都在本 crate 完成**，而「把 content 变成像素」这种重活交给行为 crate（见 u5-l4 的 Routines）。本讲关注构造、拼接、贴元信息这三件事。

#### 4.1.3 源码精读

**类型定义：一层透明外壳。**

[src/foundations/content/mod.rs:81-84](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L81-L84) —— `Content` 被标注为 `#[ty(scope, cast, since="forever")]`（注册为标准库类型），派生 `Clone/PartialEq/Hash`，并用 `#[repr(transparent)]` 包裹一个 `raw::RawContent`：

```rust
#[ty(scope, cast, since = "forever")]
#[derive(Clone, PartialEq, Hash)]
#[repr(transparent)]
pub struct Content(raw::RawContent);
```

`repr(transparent)` 意味着 `Content` 和 `RawContent` 在内存里**完全等价**——这点很关键，它让 vtable 那套 unsafe 转换可以在 `Content`/`RawContent`/`Packed<T>` 之间自由进行（u3-l2 详述）。派生的 `Clone/PartialEq/Hash` 直接转给 `RawContent` 的同名实现。

**构造：从元素到 content。**

[src/foundations/content/mod.rs:87-95](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L87-L95) —— `new` 和 `empty`：

```rust
pub fn new<T: NativeElement>(elem: T) -> Self {
    Self(raw::RawContent::new(elem))
}

pub fn empty() -> Self {
    singleton!(Content, SequenceElem::default().pack()).clone()
}
```

`empty()` 不是每次 `new` 一个空盒子，而是返回一个**全局单例**——一个空的 `SequenceElem`。因为「空内容」极其常见（函数默认返回值、空拼接），共享一份能省下海量分配。`singleton!` 宏（见 u12-l2）负责「只造一次、之后只克隆」。

[src/foundations/content/mod.rs:790-794](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L790-L794) —— 还有一条更常用的入口：任何 `NativeElement` 都能 `into_value()` 自动打包成 content：

```rust
impl<T: NativeElement> IntoValue for T {
    fn into_value(self) -> Value {
        Value::Content(self.pack())
    }
}
```

`self.pack()` 就是「把元素装进 Content」。这意味着在 Rust 侧写 `HeadingElem::new(...).pack()` 就能得到一个 `Content`，正是 `strong()`/`emph()` 等便捷方法的做法（见 4.1.4）。

**拼接：`SequenceElem` 的角色。**

[src/foundations/content/mod.rs:644-665](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L644-L665) —— `+` 运算符的实现，这是本讲最重要的一段：

```rust
impl Add for Content {
    type Output = Self;
    fn add(self, mut rhs: Self) -> Self::Output {
        let mut lhs = self;
        match (lhs.to_packed_mut::<SequenceElem>(), rhs.to_packed_mut::<SequenceElem>()) {
            (Some(seq_lhs), Some(rhs)) => { seq_lhs.children.extend(rhs.children.iter().cloned()); lhs }
            (Some(seq_lhs), None)      => { seq_lhs.children.push(rhs); lhs }
            (None, Some(rhs_seq))      => { rhs_seq.children.insert(0, lhs); rhs }
            (None, None)               => Self::sequence([lhs, rhs]),
        }
    }
}
```

它按「左右两边是不是已经是 SequenceElem」分四种情况，目的是**尽量复用已有的序列、避免层层嵌套**：

| 左 (lhs) | 右 (rhs) | 做法 |
| --- | --- | --- |
| Sequence | Sequence | 把 rhs 的孩子追加到 lhs 末尾 |
| Sequence | 非 Sequence | 把 rhs 作为一个孩子 push 进 lhs |
| 非 Sequence | Sequence | 把 lhs 插到 rhs 序列的最前面 |
| 非 Sequence | 非 Sequence | 新建一个两元素序列 |

这里 `to_packed_mut::<SequenceElem>()` 会触发写时复制（4.2 会讲），所以「在已有序列上追加」是安全的。

[src/foundations/content/mod.rs:238-248](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L238-L248) —— 上面的「新建序列」走 `sequence()`，它还有两个优化分支：

```rust
pub fn sequence(iter: impl IntoIterator<Item = Self>) -> Self {
    let vec: Vec<_> = iter.into_iter().collect();
    if vec.is_empty() { Self::empty() }
    else if vec.len() == 1 { vec.into_iter().next().unwrap() }
    else { SequenceElem::new(vec).into() }
}
```

空 → 返回单例空内容；恰好一个 → 直接返回那一个（不包多余的序列）；多个才真正建 `SequenceElem`。`Sum for Content`（[mod.rs:703-707](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L703-L707)）和 `repeat`（[mod.rs:350-353](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L350-L353)）都复用它。

[src/foundations/content/mod.rs:721-727](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L721-L727) —— `SequenceElem` 自身极简，只有一个 `children: Vec<Content>`：

```rust
#[elem(Debug, Repr)]
pub struct SequenceElem {
    #[required]
    pub children: Vec<Content>,
}
```

**带样式：`StyledElem`。**

[src/foundations/content/mod.rs:758-767](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L758-L767) —— 另一个内置元素 `StyledElem`，把「一段内容」和「一组样式」绑在一起：

```rust
#[elem(Debug, Repr, PartialEq)]
pub struct StyledElem {
    #[required] pub child: Content,
    #[required] pub styles: Styles,
}
```

[src/foundations/content/mod.rs:364-386](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L364-L386) —— `styled()` 会先尝试「合并到已有的 StyledElem」以避免嵌套，否则才新建一个：

```rust
pub fn styled(mut self, style: impl Into<Style>) -> Self {
    if let Some(style_elem) = self.to_packed_mut::<StyledElem>() {
        style_elem.styles.apply_one(style.into());
        self
    } else {
        self.styled_with_map(style.into().into())
    }
}
```

这与 `+` 的优化思路一致：**能合并就合并，尽量保持 content 树扁平**。样式的具体语义（StyleChain/fold）留到 u4。

#### 4.1.4 代码实践

**实践目标**：亲手观察 `+` 拼接如何产生 `SequenceElem`，以及 `empty`/单元素/多元素三种情况的不同。

这是一个**可运行的 Typst 实践**（需要本地安装 typst CLI）。

1. **操作步骤**：新建文件 `content-practice.typ`，内容如下（`repr` 会打印 content 的内部表示）：

   ```typst
   #let a = [Hello]
   #let b = [World]
   #repr(a)        // 单元素，不包序列
   #repr(a + b)    // 拼成 sequence
   #repr((a + b) + [!])  // 复用已有序列
   #repr([])
   ```

2. **运行**：`typst compile content-practice.typ`（或 `typst c`），打开生成的 PDF 查看文本输出。

3. **观察现象**：
   - `repr(a)` 应显示 `[Hello]`（文本元素），**不会**是 `sequence(...)`。
   - `repr(a + b)` 应显示形如 `sequence([Hello], [World])`。
   - `(a + b) + [!]` 应显示三个元素**扁平**在一个 `sequence(...)` 里，而不是 `sequence(sequence(...), ...)`——印证了 4.1.3 表格中「Sequence + 非 Sequence → push」的合并优化。
   - `repr([])` 应显示 `[]`。

4. **预期结果**：四个输出分别呈现「单元素 / 两元素序列 / 三元素扁平序列 / 空内容」。若 `(a+b)+[!]` 出现了嵌套 `sequence`，说明你的理解有偏差，请回到 4.1.3 对照 `Add` 的四种分支。

5. 如果你暂时无法运行 typst CLI，可改为**源码阅读型实践**：打开 [mod.rs:644-665](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L644-L665)，对 `(a + b) + c`（左侧已是 Sequence、右侧 `c` 非 Sequence）手动走一遍 `(Some(seq_lhs), None)` 分支，确认它把 `c` push 进了 `a+b` 的同一个序列。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Content::empty()` 要用 `singleton!` 返回单例，而不是每次 `SequenceElem::default().pack()`？

**参考答案**：空内容在编译过程中出现得极其频繁（空函数体、空拼接、缺失字段等）。若每次都新分配一块堆内存，会产生海量短命分配。`singleton!` 让全局只存在一份空 content，所有「空」都克隆自它——而克隆只是引用计数 +1（见 4.2），近乎零成本。

**练习 2**：`a + b + c`（三者都是普通文本元素）经过 `Add` 后，最终是几个 `SequenceElem`？为什么？

**参考答案**：只有 **1 个** `SequenceElem`，`children` 里依次是 `a, b, c`。计算 `a + b` 时走 `(None, None)` 分支新建一个含 `[a, b]` 的序列；再 `+ c` 时左侧已是 Sequence、右侧不是，走 `(Some, None)` 分支把 `c` 直接 push 进同一个序列，不产生新的嵌套序列。

---

### 4.2 RawContent：手写胖指针与引用计数

#### 4.2.1 概念说明

`Content` 只是个透明外壳，真正的复杂度全在 `RawContent`。为什么 Typst 不直接用 `Arc<dyn SomeElementTrait>`，而要**手写**一套等价物？源码注释给出了答案：

[src/foundations/content/raw.rs:14-18](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/raw.rs#L14-L18)

> The `ptr` + `elem` fields implement a fat pointer setup similar to an `Arc<Inner<dyn Trait>>`, but in a manual way, allowing us to have a custom vtable.

三个动机：

1. **自定义 vtable**：Typst 的 vtable 不只能放方法，还能放**普通数据**（字段名、文档、字段子表），并且能存「字段子 vtable 切片」。这样访问字段元信息不必动态分发，更快。
2. **指针判等代替 `TypeId`**：因为 vtable 是 `static` 变量，比较两个元素的类型只需比较 vtable 指针，`is::<T>()` 免动态分发（见 [vtable.rs:8-12](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/vtable.rs#L8-L12)）。
3. **跳过 weak 计数**：标准 `Arc` 同时维护强/弱两个计数，Typst 不需要弱引用，手写版可以省掉这部分开销。

一句话：`RawContent` 是一个**为 Typst 元素系统量身定制的、去掉冗余、塞入额外元数据的 `Arc<dyn Element>`**。

#### 4.2.2 核心流程

`RawContent` 的内存布局由三层结构组成：

```text
RawContent（栈上，定长）
  ├── ptr : NonNull<Header>   ── 数据指针（指向堆上的 Header）
  ├── elem: Element           ── 胖指针的「类型」部分（指向 static vtable）
  └── span: Span              ── 源码位置（频繁访问，故单独存放）

堆上 Inner<E>（repr(C)）
  ├── header : Header         ── 所有元素共享的头部
  │     ├── refs : AtomicUsize   引用计数
  │     ├── meta  : Meta         label/location/lifecycle
  │     └── hash  : HashLock     data 部分哈希的惰性缓存
  └── data   : E               具体元素（如 HeadingElem）
```

关键不变量（源码反复强调）：`ptr` 既能当 `Header*` 用，也能当 `Inner<E>*` 用，因为 `Inner<E>` 是 `repr(C)` 且第一个字段就是 `Header`，依 C 标准「指向结构体的指针等于指向其首成员的指针」。`elem` 必须始终等于所存数据的真实类型 `E::ELEM`，否则会用错 vtable——这是整套 unsafe 的核心约束。

生命周期里几个关键动作：

- **克隆**：`refs += 1`，复制栈上的三个字段（`ptr`/`elem`/`span`），不碰堆。O(1)。
- **可变访问**：先 `make_unique`，若 `refs > 1` 则深拷贝出独占副本（写时复制）。
- **销毁**：`refs -= 1`，若降为 0 则调用元素专属的 `drop` 回收堆内存。
- **取回具体类型**：`is::<E>()` 用指针判等；通过后才 `unsafe data::<E>()` 读取。

#### 4.2.3 源码精读

**栈上结构。**

[src/foundations/content/raw.rs:19-45](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/raw.rs#L19-L45) —— `RawContent` 三字段，注释详细解释了 `ptr` 为何能在 `Header*` 与 `Inner<E>*` 间自由转换（依赖 C 标准 §6.7.2.1-13）：

```rust
pub struct RawContent {
    ptr: NonNull<Header>,   // 指向堆上 Inner<E> 的首成员 Header
    elem: Element,          // 类型标识 = 指向 static ContentVtable
    span: Span,
}
```

**堆上结构。**

[src/foundations/content/raw.rs:51-59](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/raw.rs#L51-L59) —— `Inner<E>` 必须是 `repr(C)` 才能保证首成员偏移为 0：

```rust
#[repr(C)]
struct Inner<E> {
    header: Header,  // 必须是第一个字段
    data: E,         // 如 E = HeadingElem
}
```

[src/foundations/content/raw.rs:62-74](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/raw.rs#L62-L74) —— `Header` 是「所有元素共享的头部」，含引用计数、元信息、哈希锁：

```rust
struct Header {
    refs: AtomicUsize,  // 引用计数，行为同 Arc
    meta: Meta,
    hash: HashLock,     // 惰性缓存 data 的哈希
}
```

[src/foundations/content/raw.rs:76-88](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/raw.rs#L76-L88) —— `Meta` 是本讲「字段与元信息」模块的主角，先看其字段：

```rust
pub(super) struct Meta {
    pub label: Option<Label>,
    pub location: Option<Location>,
    pub lifecycle: SmallBitSet,  // bit0=prepared, bitn=guarded against n-th show recipe
}
```

**分配与初始化。**

[src/foundations/content/raw.rs:90-121](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/raw.rs#L90-L121) —— `new` 给一份默认 Meta（无 label、无 location、空生命周期、detached span），`create` 才真正 `Box::into_raw` 分配 `Inner<E>` 并把裸指针存进 `ptr`：

```rust
fn create<E: NativeElement>(data: E, meta: Meta, hash: HashLock, span: Span) -> Self {
    let raw = Box::into_raw(Box::<Inner<E>>::new(Inner {
        header: Header { refs: AtomicUsize::new(1), meta, hash },
        data,
    }));
    let non_null = unsafe { NonNull::new_unchecked(raw) };
    let ptr = non_null.cast::<Header>();   // Inner<E>* → Header*
    Self { ptr, elem: E::ELEM, span }
}
```

注意 `refs` 初始化为 1（自己这一份），并把 `Inner<E>*` cast 成 `Header*`——这正是「首成员偏移为 0」的实战用法。

**引用计数：Clone 与 Drop。**

[src/foundations/content/raw.rs:319-332](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/raw.rs#L319-L332) —— 克隆只做 `refs.fetch_add(1)` 然后复制三个栈字段，并防范「病态程序」导致的计数溢出：

```rust
impl Clone for RawContent {
    fn clone(&self) -> Self {
        let prev = self.header().refs.fetch_add(1, Ordering::Relaxed);
        if prev > isize::MAX as usize { ref_count_overflow(self.ptr, self.elem, self.span); }
        Self { ptr: self.ptr, elem: self.elem, span: self.span }
    }
}
```

[src/foundations/content/raw.rs:334-352](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/raw.rs#L334-L352) —— Drop 先 `refs -= 1`，只有降到 0 时才用元素的 `drop` 函数指针真正回收（与 `Arc::drop` 同样的 `Release/Acquire` 内存序）：

```rust
impl Drop for RawContent {
    fn drop(&mut self) {
        if self.header().refs.fetch_sub(1, Ordering::Release) != 1 { return; }
        atomic::fence(Ordering::Acquire);
        unsafe { self.handle_mut().drop(); }   // 元素专属析构
    }
}
```

`drop_impl`（[raw.rs:123-140](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/raw.rs#L123-L140)）把指针 cast 回 `Inner<E>*` 还原成 `Box` 再 drop，保证调用正确的元素析构。

**写时复制：make_unique。**

[src/foundations/content/raw.rs:211-218](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/raw.rs#L211-L218) —— 任何可变操作前先确保独占：

```rust
fn make_unique(&mut self) {
    if self.header().refs.load(Ordering::Relaxed) > 1 {
        *self = self.handle().clone();   // refs>1 才深拷贝
    }
}
```

`header_mut()`/`data_mut()`/`meta_mut()` 都先调它。这就是为什么 4.1 里 `to_packed_mut` 可以安全地「在共享序列上 push」——若有别人共享，会先复制一份独占的。

**哈希的惰性缓存。**

[src/foundations/content/raw.rs:363-371](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/raw.rs#L363-L371) —— `Hash` 把 `elem`、`meta`、`span` 直接哈希，而 `data` 部分用 `HashLock` 缓存（首次才算，之后复用）：

```rust
impl Hash for RawContent {
    fn hash<H: Hasher>(&self, state: &mut H) {
        self.elem.hash(state);
        let header = self.header();
        header.meta.hash(state);
        header.hash.get_or_insert_with(|| self.handle().hash()).hash(state);
        self.span.hash(state);
    }
}
```

为什么费心缓存哈希？因为 Typst 大量类型派生 `Hash`（comemo 增量记忆化依赖它，见 u12-l2），而元素可能很大、被反复哈希。`HashLock` 让「算一次、用多次」。一旦 `data_mut()` 修改了元素，[raw.rs:196-209](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/raw.rs#L196-L209) 会 `hash.reset()` 让缓存失效。

**类型擦除下的相等。**

[src/foundations/content/raw.rs:354-361](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/raw.rs#L354-L361) —— `PartialEq` 先要求两边是同种元素（`handle_pair` 仅在 `elem` 相同时返回 `Some`），再用 vtable 的 `eq`；若元素没提供专门 `eq`，就退化为逐字段比较：

```rust
impl PartialEq for RawContent {
    fn eq(&self, other: &Self) -> bool {
        let Some(handle) = self.handle_pair(other) else { return false };
        handle.eq().unwrap_or_else(|| handle.fields().all(|handle| handle.eq()))
    }
}
```

**Send/Sync。**

[src/foundations/content/raw.rs:373-382](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/raw.rs#L373-L382) —— 手写 `unsafe impl Send/Sync`，靠一个编译期辅助函数 `_ensure_send_sync` 强制 `NativeElement: Send + Sync`（rayon 并行编译的前提，见 u12-l2）。

#### 4.2.4 代码实践

**实践目标**：通过阅读源码自带的 miri 测试，理解「克隆只是引用计数 +1」与「可变访问触发写时复制」。

这是一个**源码阅读型实践**（结合测试断言理解行为）。

1. **操作步骤**：打开 [raw.rs:391-428](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/raw.rs#L391-L428) 的 `test_miri`。逐行阅读这段测试。

2. **重点观察以下断言对应的底层行为**：

   ```rust
   let mut first = HeadingElem::new(TextElem::packed("Hi!")).with_offset(2).pack();
   let hash1 = typst_utils::hash128(&first);          // (a) 首次计算 data 哈希并缓存
   first.set_location(Location::new(10));             // (b) 修改 meta，触发 make_unique？
   let second = first.clone();                        // (c) 克隆：refs +1，不深拷贝
   first.materialize(styles);                         // (d) 改了 data 字段 → hash.reset()
   ```

   - (a) `hash128(&first)` 会触发 `HashLock::get_or_insert_with`，把 HeadingElem 的 data 哈希缓存进 `Header.hash`。
   - (c) `first.clone()` 走 `RawContent::clone`，只 `fetch_add(1)`，`first` 与 `second` **共享同一块堆内存**。
   - (d) 因为 `refs == 2`，`materialize` 内部的可变访问会先 `make_unique` 深拷贝，使 `first` 与 `second` 分道扬镳。
   - 最终断言 `assert_ne!(first, second)` 与 `assert_ne!(hash1, typst_utils::hash128(&first))`：两者不再相等，且 first 的哈希因 data 被改而变化（缓存已失效重算）。

3. **预期结果**：你能向自己解释清楚「为什么 `first.clone()` 之后改 `first` 不会影响 `second`」——答案是 `make_unique` 在 `refs > 1` 时深拷贝。

4. **可选运行**：若本地装了 Miri（`cargo +nightly miri test -p typst-library` 在相关测试上），可实际执行该测试验证「无未定义行为」。无法运行则标注为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `RawContent` 的 `span` 单独放在栈上结构里，而不像 `label`/`location` 那样放进堆上的 `Meta`？

**参考答案**：`span` 在编译/诊断过程中被**极其频繁**地读取（每个元素都要带上源码位置用于报错和追踪）。把它放在栈上的 `RawContent` 里，读取它只需一次栈访问，不必解引用 `ptr` 跳到堆上。而 `label`/`location` 访问频率低得多，放进堆上 `Meta` 共享即可，省下每个 content 的栈体积。

**练习 2**：`PartialEq` 在两个 `Content` 类型不同时（如一个 `HeadingElem`、一个 `TextElem`）返回什么？为什么不需要逐字段比较？

**参考答案**：返回 `false`。`handle_pair` 仅在 `self.elem == other.elem`（即指向同一个 static vtable）时返回 `Some`；类型不同直接 `return false`，连 vtable 的 `eq` 都不调用。这正是「vtable 指针判等」带来的高效短路。

---

### 4.3 字段与元信息：span / label / location / 生命周期 / 序列化

#### 4.3.1 概念说明

一个 content 除了「装着什么元素」，还随身带着三类附加信息，它们都由 `Content` 上的方法暴露：

- **span**：源码位置（哪个文件、哪几行）。报错和调试追踪靠它。来源是 `typst_syntax::Span`。
- **label**：用户写的 `<intro>` 标签。它是 `query`、`@intro` 引用、`selector` 定位元素的依据。
- **location**：排版后的「文档内坐标」。只有被 `query`/show 规则返回过的元素才会拥有它，普通 content 上是 `None`。
- **lifecycle**：一个位集合（`SmallBitSet`），记录这个元素在 realize 阶段的状态——是否已 prepared、对第几条 show 规则免疫。

此外，content 的**字段**（fields）是用户可见的数据面：`rect.width`、`heading.level` 都是字段访问。本模块解释字段如何被读取、`label` 如何伪装成一个字段，以及 content 如何被序列化。

理解 `label` 尤其重要，因为它直接回答了实践任务里的问题：**content 如何携带 label 供 query 使用**。

#### 4.3.2 核心流程

**元信息的存储位置一览：**

| 信息 | 存储位置 | 读方法 | 写方法 |
| --- | --- | --- | --- |
| span | `RawContent.span`（栈上） | `span()` | `spanned()`（仅当当前为 detached 才覆盖） |
| label | `Header.meta.label`（堆上） | `label()` | `labelled()` / `set_label()` |
| location | `Header.meta.location`（堆上） | `location()` | `located()` / `set_location()` |
| lifecycle | `Header.meta.lifecycle`（堆上） | `is_prepared()` / `is_guarded()` | `mark_prepared()` / `guarded()` |

**label → query 的数据流：**

```text
用户写 [Hello] <intro>
   │  解析：把 <intro> 附给最近的非空白元素（Unlabellable 机制，见 u2-l2 的 Label）
   ▼
Content::labelled(Label)  →  写入 Header.meta.label = Some(<intro>)
   │
   ▼  query(<intro>) 时（行为在 typst-realize/introspection）
Selector::Label(<intro>).matches(target)  ==  (target.label() == Some(<intro>))
   │  即 selector.rs:140 那一行
   ▼
匹配成功的 content 被收集进 query 结果，并带上 location
```

所以「content 携带 label」的本质就是：**label 是存在 `Header.meta` 里的一个 `Option<Label>`，`query` 通过 `Content::label()` 读它、再和选择器里的 label 比较相等**。`location` 则在元素被排版分配位置后由编译器回填，让结果可被进一步定位。

**字段访问的两条路径：**

- 按 ID（`get(id, styles)`）：编译器内部和宏生成代码用，最快。
- 按名字（`get_by_name(name)` / `field_by_name`）：用户脚本 `elem.field_name` 走这条，需先 `field_id(name)` 查 ID。

两种错误：`Unknown`（元素根本没有这个字段）与 `Unset`（字段存在但当前未赋值，多见于 settable/synthesized 字段）。

#### 4.3.3 源码精读

**span：首次有效优先。**

[src/foundations/content/mod.rs:102-113](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L102-L113) —— `spanned` 只在当前 span 是 detached（无来源）时才覆盖，这保证**元素的源码位置来自它最初诞生的地方**，不会被后续包装篡改：

```rust
pub fn spanned(mut self, span: Span) -> Self {
    if self.0.span().is_detached() {
        *self.0.span_mut() = span;
    }
    self
}
```

`Span::detached()` 是「不指向任何文件」的特殊 span（见 [typst-syntax/src/span.rs:112-113](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/span.rs#L112-L113)）。注意 `strong()`/`emph()` 等便捷方法都会先取 `self.span()` 再 `.spanned(span)` 还回去（见 [mod.rs:464-468](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L464-L468)），让被强调的内容仍指向原始源码位置。

**label 与 location。**

[src/foundations/content/mod.rs:115-145](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L115-L145) —— label 和 location 的读写都是直奔 `Meta`：

```rust
pub fn label(&self) -> Option<Label> { self.0.meta().label }
pub fn set_label(&mut self, label: Label) { self.0.meta_mut().label = Some(label); }
pub fn set_location(&mut self, location: Location) { self.0.meta_mut().location = Some(location); }
```

`located()` 的文档明确点出 location 的用途：让元素可被链接、与 `Location::variant` 配合（[mod.rs:131-140](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L131-L140)）。

**label 是个「伪字段」。**

[src/foundations/content/mod.rs:168-192](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L168-L192) —— `get` 在最前面特判 `id == 255` 直接返回 label：

```rust
pub fn get(&self, id: u8, styles: Option<StyleChain>) -> Result<Value, FieldAccessError> {
    if id == 255 && let Some(label) = self.label() {
        return Ok(label.into_value());
    }
    match self.0.handle().field(id) { /* ... */ }
}
```

[src/foundations/content/mod.rs:194-210](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L194-L210) —— `get_by_name` 同样特判名字 `"label"`：

```rust
pub fn get_by_name(&self, name: &str) -> Result<Value, FieldAccessError> {
    if name == "label" {
        return self.label().map(|l| l.into_value()).ok_or(FieldAccessError::Unknown);
    }
    /* ... */
}
```

也就是说，**255 是 label 的保留字段 ID，`"label"` 是它的保留名**——于是任何 content 都能用 `content.label` 访问到标签，就像它有个叫 label 的字段一样。同理 `location` 也由 [mod.rs:596-604](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L596-L604) 的 `#[func] location()` 暴露为方法。

**label 怎样被 query 用上。**

[src/foundations/selector.rs:140](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/selector.rs#L140) —— `Selector::Label` 的匹配就是一行，直接调用我们刚读过的 `Content::label()`：

```rust
Self::Label(label) => target.label() == Some(*label),
```

这就把 4.3.2 的数据流坐实了：`query(<intro>)` 最终落到「遍历所有 content，对每个调 `label()` 与 `<intro>` 比相等」。（query 的调度与收敛在 u9 详述。）

**生命周期位。**

[src/foundations/content/mod.rs:147-166](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L147-L166) —— `lifecycle` 的 bit 0 表示 prepared，bit n 表示对第 n 条 show 规则免疫（防止 show 规则无限递归）：

```rust
pub fn is_prepared(&self) -> bool { self.0.meta().lifecycle.contains(0) }
pub fn mark_prepared(&mut self) { self.0.meta_mut().lifecycle.insert(0); }
pub fn is_guarded(&self, index: RecipeIndex) -> bool { self.0.meta().lifecycle.contains(index.0) }
pub fn guarded(mut self, index: RecipeIndex) -> Self { self.0.meta_mut().lifecycle.insert(index.0); self }
}
```

这部分主要服务于 realize 阶段（行为 crate），本讲只需知道「这些标记也存在 Meta 里」。

**字段访问与错误。**

[src/foundations/content/mod.rs:217-229](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L217-L229) —— `field`/`field_by_name` 把 `FieldAccessError` 转成带元素名的中文友好消息：

```rust
pub fn field(&self, id: u8) -> StrResult<Value> {
    self.get(id, None).map_err(|e| e.message(self, self.elem().field_name(id).unwrap()))
}
```

[src/foundations/content/field.rs:569-601](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/field.rs#L569-L601) —— 两种错误及其消息：

```rust
pub enum FieldAccessError { Unknown, Unset }

impl FieldAccessError {
    pub fn message(self, content: &Content, field: &str) -> EcoString {
        match self {
            FieldAccessError::Unknown => eco_format!("{elem} does not have field {field}"),
            FieldAccessError::Unset   => eco_format!("field {field} in {elem} is not known at this point"),
        }
    }
}
```

`Unknown` = 「这元素压根没这个字段」；`Unset` = 「字段存在（如某个 settable 字段），但此刻还没被赋值/解析」。`#[func] at(field, default)`（[mod.rs:560-572](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L560-L572)）允许用 `default:` 兜底 `Unset`。

**序列化。**

[src/foundations/content/mod.rs:709-719](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L709-L719) —— content 序列化成一个 map：先是 `"func"`（元素名），再铺开所有字段：

```rust
impl Serialize for Content {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.collect_map(
            iter::once(("func".into(), self.func().name().into_value())).chain(self.fields()),
        )
    }
}
```

`fields()`（[mod.rs:582-594](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L582-L594)）遍历 vtable 里的字段子表，把已赋值的字段连同 `label`（若有）收成一个 `Dict`。

#### 4.3.4 代码实践

**实践目标**：亲手验证「content 通过 label 被 query 找到」，并解释完整数据链路。这是本讲规格指定的核心实践。

**可运行的 Typst 实践**：

1. **操作步骤**：新建 `label-practice.typ`：

   ```typst
   #let result = query(<intro>)
   [文档里找到 #result.len() 个带 <intro> 标签的元素] \
   #result.at(0).func()   // 应输出 heading
   #result.at(0).location() // 应输出某个 location 值（非 none）

   = 引言 <intro>
   这是一段正文。
   ```

   > 注意：`query` 是「上下文敏感」操作，在真实文档中通常需要包在 `context` 或放在文档末尾才能拿到稳定结果（详见 u9）。上面的写法在文档开头调用时，结果可能为空；若如此，请把 `query` 那段移到 `= 引言 <intro>` 之后。

2. **运行**：`typst c label-practice.typ`。

3. **观察现象**：`result` 是一个数组，每个元素都是一个 `content`，且其 `.func()` 显示它原本是 `heading`。关键是 `result.at(0).location()` **不再是 `none`**——这正是 `Content::location()` 文档所说「只有 query/show 返回的 content 才有 location」。

4. **结合源码解释数据链路**（这是本实践的精髓，写成你的笔记）：
   - `<intro>` 在解析时被附给 `= 引言` 这个 heading，内部调用 `Content::set_label(Label)` 写入 `Header.meta.label`（[mod.rs:127-129](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L127-L129)）。
   - `query(<intro>)` 用 `Selector::Label(<intro>)` 遍历文档，对每个元素执行 `target.label() == Some(<intro>)`（[selector.rs:140](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/selector.rs#L140)），读的就是刚写入的 `meta.label`。
   - 匹配命中的 content 被回填 `location`（排版坐标）后返回，所以 `result.at(0).location()` 有值。

5. **预期结果**：你能用自己的话完整复述「label 从用户书写，到 `Meta.label`，再到 `Selector::Label` 匹配」这条链路。若 `query` 返回空或 `location()` 为 `none`，多半是调用位置在标签之前（收敛未完成），调整位置后再试。

#### 4.3.5 小练习与答案

**练习 1**：用户脚本里写 `rect.width`，背后依次调用了 `Content` 的哪些方法？可能得到哪两种字段错误？

**参考答案**：走 `get_by_name("width")` → 先特判 `"label"`（不是）→ `elem().field_id("width")` 查到 ID → `handle().field(id)` 取字段句柄 → `.get()`。若该元素根本没有 `width` 字段，得到 `FieldAccessError::Unknown`（消息「... does not have field width」）；若字段存在但当前未赋值（例如未显式传入、也未由 set 规则命中且是 synthesized 字段），得到 `FieldAccessError::Unset`（消息「field width ... is not known at this point」）。

**练习 2**：为什么 `location()` 对「普通 content」返回 `none`，只对 query/show 返回的 content 有效？

**参考答案**：location 表示元素在**排版后**文档中的坐标。普通 content 在求值阶段还不存在「它在第几页第几行」这种信息——坐标是 realize/layout 阶段才分配的（由 introspector/locator 负责，见 u9）。只有被 query 收集、或被 show 规则处理后回填了 `Meta.location` 的 content，才携带这个坐标。源码上 `set_location` 由编译器在排版后调用，普通构造路径（`RawContent::new`）给的默认 `Meta.location` 就是 `None`。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个**综合源码追踪任务**（可配合 typst CLI 观察现象）。

**任务背景**：你想理解一行简单的 Typst 代码 `= 标题 <t> + [附注]` 在 `typst-library` 内部经历了什么。

**要求**：

1. **拼接路径**：追踪 `+` 如何把「标题 content」和「`[附注]`」合并。打开 [mod.rs:644-665](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L644-L665)，判断这两个操作数分别是不是 `SequenceElem`，说出会走哪一个 `match` 分支、最终 content 树长什么样（应是一个含两个孩子的 `SequenceElem`）。

2. **元信息归属**：`<t>` 这个 label 最终存在内存的哪一处？请精确到「`RawContent.ptr` 指向的堆上 `Inner<HeadingElem>` 的 `header.meta.label`」。说明用 [mod.rs:127-129](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L127-L129) 的 `set_label` 论证。

3. **引用计数推演**：若你对这个 content 调用 `.clone()`（例如 show 规则把它传给一个函数），`Header.refs` 如何变化？堆内存是否被复制？后续若某处 `to_packed_mut` 想修改它，会发生什么？用 [raw.rs:319-332](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/raw.rs#L319-L332) 与 [raw.rs:211-218](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/raw.rs#L211-L218) 论证。

4. **可选运行验证**：把上述表达式放进一个 `.typ` 文件，用 `repr` 打印它的结构，用 `query(<t>)` 验证 label 确实附在了 heading 上而非附注上（label 附给「最近非空白元素」，见 u2-l2 的 Label）。

完成这个任务后，你应该能把「用户看到的一行代码」与「`Content`/`RawContent` 的内存表示与引用计数行为」一一对应起来。

## 6. 本讲小结

- **`Content` 是 Typst 的万能内容类型**：所有标记与函数调用的产物都是它。它用 `#[repr(transparent)]` 包裹 `RawContent`，是一层稳定、面向用户的外壳（[mod.rs:81-84](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L81-L84)）。
- **`+` 运算符与 `SequenceElem`**：拼接时按「两边是否已是序列」分四种情况，**尽量复用已有序列、保持扁平**；`sequence()` 还会把空/单元素特判掉（[mod.rs:644-665](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L644-L665)、[mod.rs:238-248](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L238-L248)）。`StyledElem` 同理合并样式避免嵌套。
- **`RawContent` 是手写的 `Arc<dyn Element>`**：栈上三字段（`ptr`/`elem`/`span`）+ 堆上 `Inner<E>`（`Header` + `data`），靠 C 标准首成员偏移为 0 在指针间 cast；自带引用计数、写时复制、哈希惰性缓存（[raw.rs:19-45](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/raw.rs#L19-L45)、[raw.rs:319-352](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/raw.rs#L319-L352)）。
- **元信息分处存放**：`span` 在栈上（高频访问），`label`/`location`/`lifecycle` 在堆上 `Header.meta`（[raw.rs:62-88](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/raw.rs#L62-L88)）。`spanned` 采用「首次有效优先」。
- **label 是 query 的纽带**：label 存于 `Meta.label`，被特判为保留字段 ID 255 / 名 `"label"`；`query(<x>)` 通过 `Selector::Label` 调 `Content::label()` 比相等来定位元素（[selector.rs:140](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/selector.rs#L140)）。
- **字段访问有两条路径、两种错误**：按 ID / 按名字；`Unknown`（无此字段）与 `Unset`（字段未赋值）（[field.rs:569-601](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/field.rs#L569-L601)）。序列化时 content 变成 `{func: 名字, ...字段}` 的 map。

## 7. 下一步学习建议

本讲只讲了 content 的「表示与拼接」，刻意回避了两个关键问题：

1. **类型擦除后如何安全地取回具体元素、调用它的方法？** 这涉及 `Element` 句柄、`NativeElement` trait、自定义 `ContentVtable` 与能力（capability）查询。请接着学 **u3-l2《Element、NativeElement 与能力 vtable》**，它会解释 `to_packed::<T>()`、`can::<C>()`、`with::<dyn Trait>()` 背后的 vtable 机制。
2. **元素的字段是怎么用 `#[elem]` 宏定义出来的？** `required`/`default`/`ghost`/`fold`/`parse` 这些标注、`Settable`、`Packed<T>` 包装，请学 **u3-l3《elem 宏、字段系统与 Packed》**。建议以 `src/model/heading.rs` 为真实样本对照阅读。

之后，u3-l4 会讲 `#[func]` 宏如何把 Rust 函数变成标准库函数，从而补全「元素」与「函数」两条注册路径的全貌。样式如何作用于这些字段（StyleChain/fold/resolve）则是整个 u4 单元的主题。
