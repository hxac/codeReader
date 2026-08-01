# 多生命周期与 arena 内存分配

## 1. 本讲目标

本讲是专家单元（u3）的内存专题。前面几讲我们一直把 `State` 当作一个「可变工作台」来用，却刻意回避了它身上最让 Rust 学习者头疼的两个细节：**四个生命周期参数** 和 **arena 内存池**。学完本讲，你应当能够：

1. 说清 `State<'a, 'x, 'y, 'z>` 为什么需要四个**独立**的生命周期参数，以及 `&mut` 的不变性（invariance）在其中扮演的角色。
2. 掌握 `Arenas` 三块内存（`content` / `styles` / `bump`）的分工，理解「生命周期延长（lifetime extension）」这一核心手法。
3. 解释 `store` / `store_slice` 如何把临时的 owned 数据「搬」进 arena、换得一个 `'a` 的引用，以及 `store_slice` 为什么用 `BumpVec` 而不是 `alloc_slice_copy`。

本讲承接 u2-l1（`State` 状态机与分组栈）。在那讲里我们把 `State` 的字段分成了「配置型」与「工作区」两类；本讲不再讲字段的语义，而是钻进这些字段背后的**类型签名**与**内存布局**。

## 2. 前置知识

本讲假设你已经熟悉 u1/u2 系列建立的 `realize` / `visit` / `Pair` / `StyleChain` 等概念。此外需要一点 Rust 进阶概念，这里先用大白话点一下，后文遇到再展开：

- **生命周期（lifetime）**：引用的合法存活区间，写在 `'a` 这样的撇号里。
- **变体（variance）**：编译器判断「更长/更短生命周期的引用能否互相替换」的规则。常见三种——协变（covariant，可放宽）、逆变（contravariant）、不变（invariant，必须精确相等）。
- **`&mut T` 的不变性**：可变引用对它涉及的类型和生命周期都要求**精确相等**，这是本讲一切复杂性的根源。
- **arena（竞技场分配器）**：一种「先一口气分配、最后一次性回收」的内存分配策略，分配是 O(1) 的指针递增，回收是 O(1) 的整块丢弃，省去了逐个 `free` 的簿记开销。

数学记号上，本讲用 `≤` 表示「生命周期不短于」（即子类型关系 `': ` `'short ≤ 'long`）。不变性的本质就是它只接受等号、拒绝不等号。

## 3. 本讲源码地图

本讲只精读两个文件，但会顺带印证若干下游调用方：

| 文件 | 作用 |
| --- | --- |
| [crates/typst-realize/src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs) | realize 主逻辑：`State` 定义、`realize`/`visit` 入口、`store`/`store_slice`、`visit_styled` 中的 arena 分配 |
| [crates/typst-library/src/routines.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/routines.rs) | `Arenas`、`Pair`、`RealizationKind`、`FragmentKind` 的定义，以及 `realize` 作为函数指针的声明 |

下游调用方（印证 arena 的生命周期）：`typst-layout`（flow/inline/pages/math）、`typst-bundle`、`typst-library` 的 math resolver。

## 4. 核心概念与源码讲解

### 4.1 为什么需要四个生命周期：`'a` / `'x` / `'y` / `'z`

#### 4.1.1 概念说明

`State` 的完整签名是 `State<'a, 'x, 'y, 'z>`——**四个**生命周期参数。对一个结构体来说这很反常。本模块给出了两点权威解释，直接写在源码注释里：

> Sadly, we need that many lifetimes because `&mut` references are invariant and it would force the lifetimes of e.g. engine and locator to be equal if they shared a lifetime.
>
> The only interesting lifetime is `'a`, which is that of the content that comes in and goes out.

要理解这段话，先抓住三个事实：

1. **`State` 始终通过 `&mut State` 被修改**。`visit`、`finish`、所有 `visit_*_rules` 与 `finish_*` 都拿 `&mut State`。而 `&mut T` 不仅对 `T` 不变，也对它携带的生命周期不变。于是 `&mut State<'a,'x,'y,'z>` 会把这四个参数**全部冻成不变**。

2. **`State` 内部装了好几个「各自独立的可变借用」**：
   - `engine: &'x mut Engine<'y>` —— 借走 engine 的可变引用；
   - `locator: &'x mut SplitLocator<'z>` —— 借走 locator 的可变引用；
   - `kind: RealizationKind<'x>` —— 其内部也持有 `&mut DocumentInfo` / `&mut FragmentKind`（见 u1-l2）。

3. **每个可变借用的「外层借用时长」与「引用对象内部的生命周期」是两码事**。`Engine<'y>` 内部还引用着 World/Library 等，这些数据的存活时长 `'y` 与「一次 realize 调用借走 engine 的那短短一瞬 `'x`」互不相干，通常 `'y` 比 `'x` 长得多。

把这三点合起来：如果偷懒只写一个生命周期，让所有字段共享它，不变性就会强迫「engine 的内部生命周期 = locator 的内部生命周期 = 借用时长」。但调用端根本凑不出这个等式——`Engine` 引用的 World 的生命周期，跟「我现在借走它跑一次 realize」毫无关系。解决办法就是**给每个需要独立变化的维度一个独立参数**：`'x` 管借用时长，`'y` 管 Engine 内部，`'z` 管 SplitLocator 内部。

四个生命周期的角色分工如下：

| 参数 | 含义 | 谁用它 |
| --- | --- | --- |
| `'a` | **内容/输出**的生命周期：进来的 `content`、出去的 `Vec<Pair<'a>>`、arena 的存活期 | `arenas`、`sink`、`Pair` |
| `'x` | **借用时长**：engine/locator/kind 这些可变借用一共被借多久（≈ 一次 realize 调用） | `engine`、`locator`、`kind`、`rules`、`groupings` |
| `'y` | `Engine<'y>` 的**内部生命周期**（Engine 所引用的 World/Library 等） | `engine` |
| `'z` | `SplitLocator<'z>` 的**内部生命周期** | `locator` |

> 注：`engine` 与 `locator` **共享** `'x` 作为外层借用时长，这是合理的——调用方在一次 realize 里同时借走两者，时长天然相同。它们真正需要分开的是各自的**内层** `'y` 与 `'z`。换句话说，共享 `'x` 没问题，把 `'y`/`'z` 也并进 `'x` 才会出事。

最后一句关键结论：**只有 `'a` 对外可见**。`fn realize` 只泛型在 `'a` 上，其余三个对调用方完全透明。

#### 4.1.2 核心流程

`fn realize` 对外暴露的生命周期约束极其简洁，内部却实例化了一个四参数的 `State`：

```text
fn realize<'a>(..., arenas: &'a Arenas, content: &'a Content, styles: StyleChain<'a>)
    -> SourceResult<Vec<Pair<'a>>>       // 只承诺 'a
{
    let mut s = State { ... };            // 内部: State<'a, 'x, 'y, 'z>
    visit(&mut s, content, styles)?;       // &mut State 把四个参数都冻成不变
    finish(&mut s)?;
    Ok(s.sink)                             // 输出 Vec<Pair<'a>>
}
```

用一个子类型约束来概括不变性的代价：

\[
\text{不变} \;\Longleftrightarrow\; \text{只接受}\; 'x = 'y,\quad\text{拒绝}\; 'x \leq 'y\;\text{或}\; 'y \leq 'x
\]

若 `engine` 与 `locator` 共享同一参数，编译器会要求它们的「内部生命周期」与「借用时长」满足上式的精确相等；把三者拆成 `'x/'y/'z`，每条 `&mut` 只在自己的不变槽里被约束，彼此不再互相绑架。

#### 4.1.3 源码精读

`State` 的定义与那段著名注释在 [crates/typst-realize/src/lib.rs:76-108](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L76-L108)——这段注释说明「四个生命周期的必要性」与「只有 `'a` 重要」两点。注意每个字段的参数选择：`arenas`/`sink` 走 `'a`，`engine`/`locator`/`kind`/`rules`/`groupings` 走 `'x`（外层借用），而 `Engine<'y>`、`SplitLocator<'z>` 各自带一个内层参数：

```rust
struct State<'a, 'x, 'y, 'z> {
    kind: RealizationKind<'x>,
    engine: &'x mut Engine<'y>,
    locator: &'x mut SplitLocator<'z>,
    arenas: &'a Arenas,
    sink: Vec<Pair<'a>>,
    rules: &'x [&'x GroupingRule],
    groupings: ArrayVec<Grouping<'x>, MAX_GROUP_NESTING>,
    // ...三个 bool 工作区标志
}
```

对外入口 [crates/typst-realize/src/lib.rs:43-50](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L43-L50) 只声明了 `'a` 一个生命周期参数；内部构造 `State` 时，`'x/'y/'z` 由编译器从调用上下文推断（这里它们被「抹掉」为 `'_`）。这正是注释里说的「不需要在 `fn realize` 上硬写约束、把负担转嫁给调用方」的实现方式——复杂度被封在 crate 内部。

`RealizationKind` 本身也带一个生命周期参数 `'x`（因为它持有可变引用），见 [crates/typst-library/src/routines.rs:153-169](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/routines.rs#L153-L169)：

```rust
pub enum RealizationKind<'a> {  // 这里的 'a 在 State 里对应 'x
    Bundle,
    Document { info: &'a mut DocumentInfo },
    Fragment { kind: &'a mut FragmentKind },
    Par,
    Math,
}
```

顺带一提，`Grouped` 视图比 `State` 还多一个参数，是 **五个**：[crates/typst-realize/src/lib.rs:164-169](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L164-L169) 多出的 `'s` 是「借用 `State` 本身的那一小段时间」，用于把 `&'s mut State` 在 `finish_*` 收尾函数之间短暂地移交（见 u2-l7 的 `Grouped::end()`）。这是同一套不变性逻辑的延伸：多一道可变借用，就多一个生命周期参数。

#### 4.1.4 代码实践

**目标**：用一张表把「字段 → 生命周期」的对应关系在源码里逐一核实，建立对四个参数的肌肉记忆。

**操作步骤**：

1. 打开 [crates/typst-realize/src/lib.rs:86-108](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L86-L108)，逐字段抄下它用的是 `'a` 还是 `'x`。
2. 对 `engine`/`locator` 两行，分别点出「外层借用是哪个参数」「内层类型自带的又是哪个参数」。
3. 自查：把 `Engine<'y>` 改想成 `Engine<'x>`（即合并 `'y` 进 `'x`），然后解释为什么这会要求「调用方借走 engine 的时长」等于「Engine 引用的 World 的存活时长」——一个明显荒谬的等式。

**需要观察的现象**：你会确认 `arenas` 与 `sink` 都绑在 `'a` 上（它们决定了输出引用的合法性），而所有「借来用一下」的字段都在 `'x` 上。

**预期结果**：填出的表与本讲 4.1.1 的角色表完全一致。无需运行，纯源码阅读型实践。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `engine: &'x mut Engine<'y>` 和 `locator: &'x mut SplitLocator<'z>` 合并成共享一个生命周期 `State<'a>`（即写成 `&'a mut Engine<'a>` 与 `&'a mut SplitLocator<'a>`），具体哪一条约束会在调用端被打破？

**答案**：`&'a mut Engine<'a>` 强制 Engine 的**内部**生命周期（它引用的 World/Library 的存活期）等于**借用时长** `'a`。但调用方只是「短暂借走 engine 跑一次 realize」，而 World 的存活期横跨整个编译过程，两者根本不相等；不变性又拒绝放宽，于是调用端无法满足约束、编译失败。这正是源码注释「would force the lifetimes … to be equal」的含义。

**练习 2**：为什么 `fn realize` 的签名里只有 `'a`，没有 `'x/'y/'z`？调用方是不是「看不见」后三者？

**答案**：是的。`'x/'y/'z` 只在 crate 内部实例化 `State` 时出现，`fn realize` 用 `State<'a, '_, '_, '_>` 把它们抹掉（[lib.rs:243](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L243) 的 `&mut State<'a, '_, '_, '_>` 即是）。调用方只需提供 `'a`（arena + content 的存活期），其余由编译器推断。这样把复杂度封在 crate 内、保持调用端简洁，正是注释说的「不在 `fn realize` 上硬写约束」的收益。

---

### 4.2 arenas 的三分工：`content` / `styles` / `bump`

#### 4.2.1 概念说明

`realize` 的输出是 `Vec<Pair<'a>>`，而 `Pair<'a> = (&'a Content, StyleChain<'a>)`——**清一色的引用**。可 show 规则会**不断生产全新的 owned `Content`**（如用户 `show heading: it => …` 的返回值、`finish_par` 合成的 `ParElem`）。这些 owned 数据若随栈帧消亡，就根本没法以 `&'a` 的形式塞进 sink。

解决方案是 **生命周期延长（lifetime extension）**：把 owned 数据搬进一个存活到 `'a` 的 arena，换得一个 `&'a` 的引用。这个 arena 就是 `Arenas`，它由**调用方**创建并传入，存活到调用方处理完输出为止：

```rust
pub struct Arenas {
    pub content: typed_arena::Arena<Content>,   // owned 内容
    pub styles: typed_arena::Arena<Styles>,     // owned 样式
    pub bump: bumpalo::Bump,                     // Copy 杂物
}
```

三块内存按「数据是否需要析构」分工：

| 字段 | 类型 | 存什么 | 为什么是它 |
| --- | --- | --- | --- |
| `content` | `typed_arena::Arena<Content>` | owned `Content`（show 产物、合成元素） | `Content` 拥有堆内存、**需要 Drop**；`typed_arena` 会在自身 drop 时正确调用每个元素的析构 |
| `styles` | `typed_arena::Arena<Styles>` | owned `Styles`（`visit_styled` 里 `into_owned` 的样式） | 同理，`Styles` 需要 Drop |
| `bump` | `bumpalo::Bump` | 一切 **`Copy`** 的东西（`StyleChain`、`Pair` 切片、`LocationKey` 集合） | `bumpalo` **不调用析构**，只适合 Copy/平凡数据；但分配是 O(1) 指针递增、回收是整块丢弃 |

> 关键对比：`typed_arena` 会 Drop、地址稳定（分块分配、永不搬迁已分配元素）；`bumpalo` 不 Drop、但极快且可整块回收。所以「需要析构的 owned」进 typed arena，「纯 Copy」进 bump。

为什么不能用普通 `Vec<Content>`？因为 `Vec` 在 `push` 时可能扩容搬迁，先前发出去的 `&Content` 会瞬间悬空；而 `typed_arena` 一旦分配就**钉死在原地**，`&'a Content` 对整个 `'a` 都有效。这是「为什么必须用 arena 而非 Vec」的根本原因。

#### 4.2.2 核心流程

「生命周期延长」在 realize 里有两条主干路径：

```text
路径 A：单件内容延长（store）
    owned Content ──arena.content.alloc──▶ &'a Content ──▶ 塞进 sink

路径 B：样式链延长（visit_styled 中）
    owned Styles ──arena.styles.alloc──▶ &'a Styles ──▶ outer.chain(&&'a Styles)
    StyleChain(本身 Copy) ──arena.bump.alloc──▶ &'a StyleChain
```

`store` 只关心 `arenas`（一个 `'a` 的共享引用），所以它的 `impl` 只绑定 `'a`：

```rust
impl<'a> State<'a, '_, '_, '_> {
    fn store(&self, content: Content) -> &'a Content {
        self.arenas.content.alloc(content)   // 返回值绑在 arena 的 'a 上，而非 &self
    }
}
```

注意一个小巧思：`store` 虽然签名是 `&self`，返回的 `&'a Content` 却**不**受这次 `&self` 借用约束——因为 `self.arenas` 本身是 `&'a Arenas`（一个可复制的共享引用），`typed_arena::alloc` 把返回值的生命周期钉在 arena 上。于是 `visit_show_rules` 可以放心地把 show 规则产物 `store` 后立刻塞进 sink。

#### 4.2.3 源码精读

`Arenas` 定义与「必须在处理输出期间存活」的注释在 [crates/typst-library/src/routines.rs:182-193](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/routines.rs#L182-L193)；同文件的 `Pair` 类型别名 [routines.rs:195-196](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/routines.rs#L195-L196) 说明输出单元是 `(&'a Content, StyleChain<'a>)`。

`store` 与 `store_slice` 的实现集中在 [crates/typst-realize/src/lib.rs:204-220](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L204-L220)：

```rust
impl<'a> State<'a, '_, '_, '_> {
    fn store(&self, content: Content) -> &'a Content {
        self.arenas.content.alloc(content)
    }
    fn store_slice(&self, pairs: &[Pair<'a>]) -> BumpVec<'a, Pair<'a>> {
        let mut vec = BumpVec::new_in(&self.arenas.bump);
        vec.extend_from_slice_copy(pairs);
        vec
    }
}
```

`store` 的最大用户是 `visit_show_rules`：show 规则产物若不是借用，就用 `store` 延长后再回灌流水线（[lib.rs:406-409](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L406-L409)）；`finish_par`/`finish_cites`/`finish_list_like` 合成出新元素后也都靠 `s.store(elem)` 把它送回 `visit`（如 [lib.rs:1202](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1202)）。

`visit_styled` 里同时用到了三块 arena，是观察「三分工」的最佳现场，见 [lib.rs:662-667](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L662-L667)：

```rust
// outer 是 StyleChain（Copy），进 bump
let outer = s.arenas.bump.alloc(outer);
// local 若是 owned Styles，进 typed styles arena
let local = match local {
    Cow::Borrowed(map) => map,
    Cow::Owned(owned) => &*s.arenas.styles.alloc(owned),
};
```

这段正是「Copy 进 bump、owned 析构型进 typed arena」的教科书示例。正则 show 规则冷路径 `visit_regex_match` 也是同一套（[lib.rs:1466-1467](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1466-L1467)：`bump.alloc(styles)` 放 `StyleChain`、`styles.alloc(revocation)` 放 `Styles`）。

`StyleChain` 确实是 `Copy`（[styles.rs:564](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L564) `#[derive(Default, Copy, Clone, Hash)]`），所以 `Pair` 也是 `Copy`，这才能整片搬进 bump。

#### 4.2.4 代码实践

**目标**：用插桩统计一次 realize 里三块 arena 各被分配了多少次，直观感受「哪条路径最热」。

**操作步骤**（临时改源码做实验，验证后还原；勿提交）：

1. 在 [lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs) 顶部加三个原子计数器：
   ```rust
   use std::sync::atomic::{AtomicUsize, Ordering};
   static STORE_N: AtomicUsize = AtomicUsize::new(0);      // content arena
   static SLICE_N: AtomicUsize = AtomicUsize::new(0);      // bump BumpVec
   static STYLED_N: AtomicUsize = AtomicUsize::new(0);     // visit_styled 分配
   ```
2. 在 `store` 首行 `STORE_N.fetch_add(1, Ordering::Relaxed);`；在 `store_slice` 首行 `SLICE_N.fetch_add(1, Ordering::Relaxed);`；在 `visit_styled` 两处 arena 分配（[lib.rs:663](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L663) 的 `bump.alloc(outer)` 与 [lib.rs:666](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L666) 的 `styles.alloc`）后各 `STYLED_N.fetch_add(1, Ordering::Relaxed);`。
3. 在 `fn realize` 的 `Ok(s.sink)` 之前打印三者，例如 `eprintln!("store={} slice={} styled={}", STORE_N.load(Ordering::Relaxed), SLICE_N.load(Ordering::Relaxed), STYLED_N.load(Ordering::Relaxed));`。
4. 编译并排版一个含 show 规则与列表的最小文档，例如：
   ```typst
   #show heading: it => it.body
   = 标题
   - 一项
   - 二项
   正文段落。
   ```

**需要观察的现象**：`store` 次数大致 = 「show 规则改写的元素数 + 合成的 `ParElem`/`ListElem` 数」；`styled` 次数随带样式元素递增；`slice` 次数与分组收尾次数相关。

**预期结果**：待本地验证。预测三个计数都为正、且 `store` 在有 show 规则时明显增多，印证「show 产物必须经 arena 延长生命周期」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Content` 和 `Styles` 必须进 `typed_arena`，而 `StyleChain` 与 `Pair` 切片进 `bump`？

**答案**：`Content`/`Styles` 拥有堆内存、有析构逻辑，需要 arena 在 drop 时正确调用 `Drop`——`typed_arena` 会，而 `bumpalo` 不会。`StyleChain` 与 `Pair` 都是 `Copy`（无析构、按位复制），正好符合 `bumpalo` 「不调用析构」的使用前提，又能享受 O(1) 分配与整块回收。

**练习 2**：`fn store(&self, content: Content) -> &'a Content` 的返回值生命周期是 `'a` 而非「与 `&self` 相同」，为什么能成立？

**答案**：因为 `self.arenas` 的类型是 `&'a Arenas`——一个共享引用，本身可随意复制。`typed_arena::Arena::alloc` 把返回引用的生命周期钉在 arena 的借用（`&'a Arenas`）上，与 `store` 自身的 `&self` 借用无关。所以即便 `store` 调用结束、`&self` 释放，返回的 `&'a Content` 依然有效。

---

### 4.3 BumpVec 复用策略：`store_slice` 为什么不用 `alloc_slice_copy`

#### 4.3.1 概念说明

`store_slice` 把 sink 的一段 `&[Pair<'a>]` 复制到一个 bump 里的 `BumpVec<'a, Pair<'a>>`。它的用途是「**临时快照**」：分组收尾时先存下 sink 尾部、截断 sink、处理分组、再把快照里的元素重新 `visit` 一遍。这些快照用完即弃。

bumpalo 提供了两种批量存放 Copy 切片的方式：

- `bump.alloc_slice_copy(values)` → 返回 `&mut [T]`，**永久占据** bump 空间，直到整个 `Bump` 被 drop/reset。
- `BumpVec::new_in(bump)` + `extend_from_slice_copy` → 返回一个 `BumpVec`，**drop 时若它仍是 bump 栈顶**，就把空间还给 bump，供后续分配复用。

源码注释一语道破选择 `BumpVec` 的理由：

> By using a `BumpVec` instead of a `alloc_slice_copy` we can reuse the space if no other bump allocations have been made by the time the `BumpVec` is dropped.

由于 `store_slice` 的结果都是短命快照、且往往在循环中反复产生（每个分组收尾一次），用 `BumpVec` 能让连续的快照**复用同一块 bump 内存**，把峰值内存压低；而 `alloc_slice_copy` 会让每一次快照都永久堆在 bump 里，直到 realize 结束。

#### 4.3.2 核心流程

bump 的回收是「栈式」的：只有**最近一次**分配（栈顶）被 drop 时，空间才能被后续分配复用。`store_slice` 的典型用法恰好满足「用完马上 drop、中间不夹别的 bump 分配」：

```text
每次分组收尾：
  elems = store_slice(&sink[start..])   // 在 bump 栈顶开一段
  sink.truncate(start)
  for elem in elems { visit(...) }       // 消费快照（visit 内部可能再分配）
  // elems 在此 drop —— 若它仍是栈顶，bump 回收这段空间
  // 下一轮 store_slice 可复用同一块内存
```

`Pair` 是 `Copy`，所以 `extend_from_slice_copy` 是纯按位复制，没有析构开销，回收时也不需要逐个 drop。

#### 4.3.3 源码精读

`store_slice` 见 [crates/typst-realize/src/lib.rs:210-219](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L210-L219)，注释说明了复用动机。它有三个调用点，全是「存快照 → 截断 sink → 重新 visit」的模式：

1. **neutral 元素分段** [lib.rs:859](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L859)：`finish_innermost_grouping` 把混杂的 neutral/非 neutral 元素切片分段处理（u2-l7）。
2. **正则命中冷路径** [lib.rs:1250](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1250)：`visit_textual` 存下文本元素再交给 `visit_regex_match` 切片（u2-l10）。
3. **尾部暂存** [lib.rs:951](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L951)：`finish_grouping` 存下分组之后的 tail，收尾完再 `visit` 回去（u3-l1）。

另一处 bump 复用典范是 `tag_set`，它用 `collect_in::<BumpVec<_>>` 把 `LocationKey` 集合放进 bump（[lib.rs:985-994](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L985-L994)），同样是「算完即弃」的临时集合，与 `store_slice` 同理。

> 印证：这套「arena 延长 + BumpVec 复用」的 idiom 并非 typst-realize 独有。`typst-library` 的 math resolver 用同一个 `Arenas` 实现了一模一样的 `store` / `store_styles` / `store_chain`（[math/ir/resolve.rs:65-87](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/resolve.rs#L65-L87)），说明这是跨 crate 复用的标准手法。

#### 4.3.4 代码实践

**目标**：通过阅读 + 推理，对比 `BumpVec` 与 `alloc_slice_copy` 在 `store_slice` 场景下的内存差异。

**操作步骤**：

1. 读 [lib.rs:210-219](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L210-L219)，把 `store_slice` 的实现改成等价的 `self.arenas.bump.alloc_slice_copy(pairs)`（返回 `&'a [Pair<'a>]`，调用点相应解引用）。
2. 不实际编译，而是推演：一个含 N 个分组的文档，原来每轮 `store_slice` 的 `BumpVec` drop 后能复用栈顶空间；改成 `alloc_slice_copy` 后，N 段快照会**全部堆积**在 bump 里直到 realize 结束。
3. 结合 4.2.4 的插桩（`SLICE_N` 计数），估算在最坏情况下 bump 峰值占用从「≈1 段」涨到「≈N 段」。

**需要观察的现象**：理解「栈顶回收」是 BumpVec 省内存的唯一前提；一旦中间插入其它 bump 分配（如 `visit` 内部的 `bump.alloc`），栈顶就不再是该 BumpVec，这段空间便无法立即复用——这正是注释里「if no other bump allocations have been made」的限定。

**预期结果**：能口头说清「为什么 `store_slice` 选 `BumpVec` 而非 `alloc_slice_copy`」：因为快照短命、希望栈式复用；而 `alloc_slice_copy` 是「只进不出」的永久占用。待本地验证改动后峰值内存的变化（可用 `dhat` 或 bump 统计工具）。

#### 4.3.5 小练习与答案

**练习 1**：`BumpVec` 在 drop 时能回收空间的前提是「仍是 bump 栈顶」。在 `store_slice` 的实际调用语境里，这个前提为什么**通常**成立？

**答案**：`store_slice` 的结果 `elems` 通常在同一个函数作用域里被消费（`for elem in elems { visit(...) }`）并随即 drop。虽然 `visit` 内部可能再触发 bump 分配、把栈顶顶上去，但只要快照在「没有长期持有其它 bump 分配」的情况下 drop，回收就仍有机会生效；即便偶尔不成立，退化为 `alloc_slice_copy` 的行为也不影响正确性，只损失一点复用收益。这是一种「尽力而为」的优化。

**练习 2**：`Pair` 必须是 `Copy`，`store_slice` 才能用 `extend_from_slice_copy`。请结合 `StyleChain` 的定义说明为什么 `Pair` 满足这一条件。

**答案**：`Pair<'a> = (&'a Content, StyleChain<'a>)`。`&'a Content` 是引用、天然 `Copy`；`StyleChain` 在 [styles.rs:564](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L564) 标注了 `#[derive(..., Copy, Clone, ...)]`（它只持引用 `head: &'a [...]` 与 `tail: Option<&'a Self>`）。元组两边都 `Copy`，故 `Pair` 是 `Copy`，可按位复制进 bump。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成一次「内存视角的 realize 走读」。

**任务**：为下面这个文档跑一遍 realize，预测并验证三块 arena 的使用情况，再写一段说明把「四生命周期 + arena 三分工 + BumpVec 复用」讲清楚。

```typst
#set page(width: 100pt)
#show emph: set text(red)
这是 *一段* 带 #highlight[样式] 的正文。
- 列表项一
- 列表项二
```

**步骤**：

1. **插桩**：按 4.2.4 的方法给 `store`/`store_slice`/`visit_styled` 的 arena 分配加原子计数器，并在 `fn realize` 返回前打印。
2. **跑文档**：用 typst CLI 编译，捕获 `eprintln!` 输出。记录 `store` / `slice` / `styled` 三个数字。
3. **解释生命周期**：对照 [lib.rs:86-108](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L86-L108) 写一段话，指明 `engine`/`locator`/`arenas`/`sink` 分别用了哪个生命周期参数，并说明「为什么不能用一个参数合并它们」（用 `&mut` 不变性解释）。
4. **解释 arena 分工**：写明 `content`/`styles`/`bump` 各接收哪类数据、为什么 `Content` 不能进 `bump`、为什么不能用普通 `Vec`（提示：扩容搬迁会让 `&'a Content` 悬空）。
5. **解释 BumpVec 复用**：说明 `store_slice` 为何选 `BumpVec` 而非 `alloc_slice_copy`，以及栈顶回收的前提。

**预期结果**：待本地验证。你应当看到 `store` 在遇到 `*一段*`（emph show-set）、`#highlight[样式]`、合成 `ParElem`、合成 `ListElem` 时各贡献若干次；`slice` 在段落与列表分组收尾时各贡献若干次；`styled` 在每个带样式片段处递增。说明文字应覆盖本讲小结的全部要点。

## 6. 本讲小结

- `State<'a, 'x, 'y, 'z>` 的四个生命周期是 `&mut` **不变性**逼出来的：`State` 始终经 `&mut` 修改，使所有参数变为不变；engine/locator 各自带独立的内层生命周期（`'y`/`'z`），不能并入借用时长 `'x`，否则调用端凑不出精确等式。只有 `'a` 对外可见。
- `Arenas` 三块内存按「是否需要析构」分工：owned 的 `Content`/`Styles` 进 `typed_arena`（会 Drop、地址钉死），纯 `Copy` 的 `StyleChain`/`Pair`/`LocationKey` 进 `bumpalo`（不 Drop、极快、整块回收）。
- **生命周期延长**是 realize 能输出引用清单的关键：`store` 把 owned 内容搬进 arena 换得 `&'a Content`，show 规则产物与合成元素因此能进入 sink。
- 不能用普通 `Vec`：`Vec` 扩容会搬迁元素、令已发出的 `&'a Content` 悬空；`typed_arena` 永不搬迁已分配元素，引用对整个 `'a` 有效。
- `store_slice` 选 `BumpVec` 而非 `alloc_slice_copy`：快照短命，`BumpVec` 在仍是栈顶时 drop 可把空间还给 bump 供复用，压低峰值内存；`alloc_slice_copy` 则永久占用。
- 这套「四生命周期 + arena 三分工 + BumpVec 复用」是跨 crate 的标准 idiom，math resolver 等处复用同一 `Arenas` 模式。

## 7. 下一步学习建议

- **横向印证**：读 [typst-library/src/math/ir/resolve.rs:65-87](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/math/ir/resolve.rs#L65-L87)，对照本讲的 `store`，体会同一 `Arenas` 在不同 crate 里的复用。
- **arena 生命周期边界**：去看下游调用方如何创建与销毁 `Arenas`，例如 [typst-layout/src/flow/mod.rs:151-152](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-layout/src/flow/mod.rs#L151-L152)、[typst-bundle/src/lib.rs:167-177](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-bundle/src/lib.rs#L167-L177)，理解「arena 必须存活到输出处理完毕」在调用端如何兑现（这是 u3-l5 集成讲义的内容）。
- **下一讲 u3-l4**：从内存细节回到行为层面，深入对比五种 `RealizationKind` 选用哪张规则表、`outside` 初值差异，以及 Fragment 的行内回退——你会再次用到本讲对 `'a`（输出生命周期）与 `kind`（`'x` 借用）的理解。
