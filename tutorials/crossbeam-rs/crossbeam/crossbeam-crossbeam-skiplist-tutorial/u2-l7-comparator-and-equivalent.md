# Comparator 与 Equivalent：灵活的键比较

## 1. 本讲目标

在前两讲里，我们已经读过了 `Node` 的内存布局（u2-l5）和 epoch 内存回收（u2-l6）。你可能注意到一个反复出现的细节：`base.rs` 里几乎每一个 `impl` 块都带着一个 `C: Comparator<K>` 约束，`get` / `lower_bound` 这些「读」方法还额外要求 `C: Comparator<K, Q>`。本讲就来回答这个一直被悬置的问题：**`Comparator` 到底是什么？它凭什么能让 `SkipMap<String, _>.get("abc")` 用一个 `&str` 查到 `String` 键？又凭什么能让我们自定义排序？**

学完本讲你应当掌握：

1. `Equivalent` / `Comparable` 两个 trait 以及它们基于 `Borrow` + `Ord` 的 blanket 实现，理解「用 `&str` 查 `String` 键」的精确类型机制。
2. `Comparator` / `Equivalator` 两个 trait 与默认实现 `BasicComparator`，理解为什么比较器要带一个 `&self` 参数，以及 `BasicComparator` 是如何把两层设计桥接起来的。
3. 能动手实现一个自定义 `Comparator`（例如大小写不敏感），并用 `SkipMap::with_comparator` 改变键的排序与相等判定。

本讲只读 `comparator.rs` 与 `equivalent.rs` 两个小文件（各约 50–100 行），并对照它们在 `base.rs` / `map.rs` 里的接入点，**不涉及任何无锁并发算法**。并发算法留到第三单元。

## 2. 前置知识

### 2.1 先有直觉：标准库的 `Borrow` 查表问题

假设你有一个 `BTreeMap<String, V>`，想查一个键。你手上往往只有一个 `&str`（比如来自命令行参数、配置文件、HTTP 路径）。如果每次查询都要 `s.to_string()` 分配一个新 `String`，既慢又别扭。标准库的解法是：

```rust
// std::collection 的查表方法（示意）
pub fn get<Q: ?Sized>(&self, key: &Q) -> Option<&V>
where
    K: Borrow<Q>,
    Q: Ord,            // 对有序 map 还要求 Ord
```

这里的机关是 `K: Borrow<Q>`：只要存储的键类型 `K` 能被「借用」成查询类型 `Q`（例如 `String: Borrow<str>`、`Vec<u8>: Borrow<[u8]>`、`Box<T>: Borrow<T>`），就可以直接用 `&Q` 去查。`String` 实现了 `Borrow<str>`，所以 `map.get("abc")` 合法。

> 术语：`Borrow<T>` 是标准库 trait，表示「某种拥有型（owned）类型可以被安全地当作 `&T` 来看」。`Borrow` 的核心契约是「借用视图与拥有视图的 `Eq`/`Ord`/`Hash` 必须一致」，这正是查表能正确工作的前提。

### 2.2 crossbeam-skiplist 的两层设计

`crossbeam-skiplist` 把「键怎么比较」拆成了**两层**，这是本讲的核心心智模型：

| 层次 | 文件 | 比较逻辑由谁决定 | 触发方式 |
|------|------|------------------|----------|
| 第一层 | `equivalent.rs` | **类型驱动**：由键类型 `K` 与查询类型 `Q` 的 `impl` 决定 | 编译期，靠 trait bound |
| 第二层 | `comparator.rs` | **运行时驱动**：由比较器对象 `C` 的 `&self` 决定 | 运行时，靠 `with_comparator` 传值 |

`BasicComparator` 是第二层的一个「回退实现」：它自己不发明比较逻辑，而是**委托给第一层**，从而在没有自定义比较器时，行为与 `BTreeMap` 完全一致。当你传入自定义 `Comparator`（如大小写不敏感）时，则**绕过第一层**，用第二层自己的逻辑取代。

用一个式子概括两层之间的委托关系（`—▸` 表示「依赖于」）：

\[
\text{BasicComparator} : \text{Comparator}\langle K, Q\rangle
\;\xrightarrow{\text{委托}}\;
K : \text{Comparable}\langle Q\rangle
\;\xrightarrow{\text{blanket impl}}\;
K : \text{Borrow}\langle Q\rangle \;\wedge\; Q : \text{Ord}
\]

下面三节就按这条链路自底向上读：先读第一层（4.1），再读第二层与桥接（4.2），最后看它们如何接入跳表（4.3）。

## 3. 本讲源码地图

| 文件 | 行数 | 作用 |
|------|------|------|
| [src/equivalent.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/equivalent.rs) | 54 | 第一层：`Equivalent` / `Comparable` trait 及基于 `Borrow` 的 blanket 实现 |
| [src/comparator.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/comparator.rs) | 96 | 第二层：`Equivalator` / `Comparator` trait 与默认实现 `BasicComparator` |
| [src/base.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs) | — | 把 `C: Comparator<K, Q>` 接入 `get` / `search` / `lower_bound` 等方法 |
| [src/map.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs) | — | `SkipMap<K, V, C = BasicComparator>` 与 `with_comparator` 入口 |
| [tests/map.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/tests/map.rs) | — | `comparator` 测试：`CaseComparator` 大小写敏感/不敏感的实战示例 |
| [tests/base.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/tests/base.rs) | — | `comparable_get` 测试：手写 `Equivalent` / `Comparable` 实现自定义查询类型 |

两个文件都无 `#[cfg]` 门控，在 `lib.rs` 中被无条件 `pub mod`（见 [src/lib.rs:268-269](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/lib.rs#L268-L269)），所以无论开不开 `std` / `alloc`，比较器总是可用。

## 4. 核心概念与源码讲解

### 4.1 第一层：Equivalent / Comparable —— Borrow 驱动的默认比较

#### 4.1.1 概念说明

`equivalent.rs` 定义两个 trait：`Equivalent`（判等）与 `Comparable`（判序）。注意它们的方法签名——**`self` 是存储的键 `K`，参数 `key: &Q` 是查询值**：

```rust
pub trait Equivalent<Q: ?Sized> {
    fn equivalent(&self, key: &Q) -> bool;
}

pub trait Comparable<Q: ?Sized>: Equivalent<Q> {
    fn compare(&self, key: &Q) -> Ordering;
}
```

也就是说，「比较」被建模成「键 `K` 自己回答：我和这个查询 `Q` 是否相等 / 谁大谁小」。`Comparable` 是 `Equivalent` 的子 trait（多了 `compare`），这与标准库里「有序 map 既要判等又要判序」的需求对应。

> 一个值得注意的设计细节（源码顶部注释点明了）：这里的类型参数顺序与上游 [`equivalent` crate](https://github.com/indexmap-rs/equivalent) 是**对调**的。在 `equivalent` crate 里，`Equivalent<K>` 由「查询类型」来实现（`self` 是查询值）；而在这里，`Equivalent<Q>` 由「键类型」来实现（`self` 是键）。注释链接到的 [issue #5](https://github.com/indexmap-rs/equivalent/issues/5) 说明这种对调是为了规避类型推断歧义。这也是为什么下面的 blanket impl 读起来像「给 `K` 实现，去比较 `&Q`」，方向「反」着写。

#### 4.1.2 核心流程

这两个 trait 各自只有**一个 blanket 实现**，把工作转嫁给标准库的 `Borrow` + `Eq` / `Ord`：

```text
对任意 K, Q 满足  K: Borrow<Q>  且  Q: Ord   ⇒   自动得到  K: Comparable<Q> + Equivalent<Q>

Equivalent::equivalent(self=&K, key=&Q)   =  PartialEq::eq(self.borrow(), key)
Comparable::compare   (self=&K, key=&Q)   =  Ord::cmp      (self.borrow(), key)
```

这就是「用 `&str` 查 `String`」的机制：

- `String` 实现了 `Borrow<str>`，`str: Ord`，所以自动得到 `String: Comparable<str>`。
- 因此 `BasicComparator`（4.2 节）可以充当 `Comparator<String, str>`，`get("abc")`（`Q = str`）合法。

#### 4.1.3 源码精读

`Equivalent` trait 与判等方法的定义（仅一个方法）：

[`Equivalent<Q>` 的定义](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/equivalent.rs#L18-L21)：`self` 是键，`key: &Q` 是查询，返回是否相等。

判等的 blanket 实现，委托给 `Borrow` + `PartialEq`：

[blanekt `Equivalent` 实现](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/equivalent.rs#L23-L32)：只要 `K: Borrow<Q>` 且 `Q: Eq`，就把 `&K` 借用成 `&Q` 再用 `==` 比较。

`Comparable` trait（`Equivalent` 的子 trait，多了判序）：

[`Comparable<Q>` 的定义](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/equivalent.rs#L40-L43)：返回 `Ordering`，供跳表二分定位使用。

判序的 blanket 实现，委托给 `Borrow` + `Ord`：

[blanekt `Comparable` 实现](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/equivalent.rs#L45-L54)：要求 `Q: Ord`，用 `Ord::cmp(self.borrow(), key)` 得到全序。

这两段代码极短，但它们是整个库「开箱即用即像 `BTreeMap`」的全部秘密：默认情况下，跳表里所有的比较最终都落到这两行的 `self.borrow()` 上。

#### 4.1.4 代码实践

**实践目标**：亲手验证「用 `&str` 查 `String` 键」能用，并理解它依赖的 `Borrow` 链。

**操作步骤**（在你的测试文件或 `examples/` 里加一段，示例代码）：

```rust
// 示例代码：验证 Borrow 驱动的查询
use crossbeam_skiplist::SkipMap;

let map: SkipMap<String, i32> = SkipMap::new();
map.insert("alpha".to_string(), 1);
map.insert("beta".to_string(), 2);

// 用 &str 直接查 String 键，无需 to_string()
assert_eq!(*map.get("beta").unwrap().value(), 2);
assert!(map.contains_key("alpha"));
assert!(!map.contains_key("gamma"));
```

**需要观察的现象**：上面 `get("beta")`、`contains_key("alpha")` 都能编译通过并命中，尽管我们传的是 `&'static str` 而存储的是 `String`。

**预期结果**：断言全部成立。原理上，`map.get::<str>("beta")` 触发约束链：默认比较器 `BasicComparator: Comparator<String, str>` 需要 `String: Comparable<str>`，而后者由 blanket impl 自动满足（`String: Borrow<str>` 且 `str: Ord`）。

> 待本地验证：受运行环境限制，本讲无法替你执行 `cargo test`。请在本机运行后确认断言通过。

#### 4.1.5 小练习与答案

**练习 1**：`SkipMap<Vec<u8>, V>.get(&[1u8, 2][..])` 能编译吗？为什么？

**参考答案**：能。`Vec<u8>` 实现了 `Borrow<[u8]>`，`[u8]: Ord`，所以 blanket impl 自动给出 `Vec<u8>: Comparable<[u8]>`，进而 `BasicComparator: Comparator<Vec<u8>, [u8]>` 成立，`get` 的 `Q = [u8]` 合法。

**练习 2**：为什么 `Equivalent` 的 blanket 实现要求 `Q: Eq`，而 `Comparable` 的要求 `Q: Ord`？

**参考答案**：判等只需「相等」语义，对应标准库 `Eq`；判序需要「全序」语义，对应 `Ord`。`Comparable` 是 `Equivalent` 的子 trait 且更强，因此有序容器（跳表）依赖 `Comparable`，需要 `Ord`。

---

### 4.2 第二层：Comparator / Equivalator / BasicComparator —— 运行时可定制的比较

#### 4.2.1 概念说明

第一层的比较逻辑在编译期就被「焊死」在类型上了。如果你想要**运行时**改变比较方式（例如一个开关切换大小写敏感，或允许用户传入 `Box<dyn Comparator>` 定制排序），就需要一个**带状态**的比较器对象。这正是 `comparator.rs` 提供的三个东西：

- `Equivalator<L, R>`：判等 trait，方法 `equivalent(&self, lhs, rhs)`。
- `Comparator<L, R>: Equivalator<L, R>`：判序 trait，方法 `compare(&self, lhs, rhs)`，是 `Equivalator` 的子 trait。
- `BasicComparator`：一个空的 ZST（零大小类型），是库的**默认比较器**，它把第二层委托回第一层。

关键差别在于 `&self`：相比第一层「键自己回答」，第二层是「比较器对象回答」。这让比较行为可以随对象状态、或随 `dyn` 动态分发而改变。

> 术语：「`Equivalator`」是 `Equivalent` + 表示「执行者」的 `-ator` 后缀，与 `Comparator` 对仗。两者都是 `?Sized` 类型参数 `L` / `R`，默认 `R = L`（即左右同类型）。

一个强约束写在源码注释里：**同一个比较器的 `Comparator` 与 `Equivalator` 实现必须对「哪些输入相等」达成一致**。否则跳表的「先用 `compare` 二分定位、再用 `equivalent` 确认命中」两步就会自相矛盾（见 4.3.3）。

#### 4.2.2 核心流程

第二层与第一层、与跳表的关系：

```text
                      ┌─────────────────────────────┐
SkipMap<K,V,C>  ─────▶│  C : Comparator<K>           │   每个方法都带这个约束
                      │  C : Comparator<K, Q>        │   读方法额外要求（查询类型 Q）
                      └──────────────┬──────────────┘
                                     │ 调用
                                     ▼
                      C.compare(&k1, &k2)  /  C.equivalent(&k1, &q)
                                     │
              ┌──────────────────────┴───────────────────────┐
              ▼  (C = BasicComparator 时)                     ▼  (C = 自定义比较器时)
   委托给第一层 K: Comparable<Q>                        直接执行自己的逻辑
   （即 Borrow + Ord，BTreeMap 行为）                  （可大小写不敏感、自定义排序等）
```

`BasicComparator` 的两个 `impl` 就是上图中「委托」那条边。

#### 4.2.3 源码精读

`Equivalator` trait，注意 `&self`：

[`Equivalator<L, R>` 的定义](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/comparator.rs#L48-L51)：带 `&self`，返回布尔；文档注释（[第 7-21 行](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/comparator.rs#L7-L21)）给出了一个大小写不敏感的 `[u8]` 比较示例。

`Comparator` trait（`Equivalator` 的子 trait，多了判序）：

[`Comparator<L, R>` 的定义](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/comparator.rs#L66-L69)：返回 `Ordering`，且文档强调其 `Equivalator` 实现必须与之「对哪些输入相等」一致。

默认比较器 `BasicComparator`：

[`BasicComparator` 结构体](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/comparator.rs#L75-L76)：`#[derive(Clone, Copy, Debug, Default)]` 的 ZST，所以它作为默认泛型参数 `C = BasicComparator` 不占空间、可用 `Default::default()` 构造。

把第二层桥接回第一层的两个 `impl`：

[`BasicComparator: Equivalator<K,Q>` 委托给 `Equivalent`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/comparator.rs#L78-L86)：要求 `K: Equivalent<Q>`，直接转发调用。

[`BasicComparator: Comparator<K,Q>` 委托给 `Comparable`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/comparator.rs#L88-L96)：要求 `K: Comparable<Q>`，直接转发调用。

正因为这两个 `impl` 的约束只是 `K: Equivalent<Q>` / `K: Comparable<Q>`（对任意 `K, Q`），`BasicComparator` 才能充当**任意** `Comparator<K, Q>`——只要键类型在第一层「恰好」满足对应 trait（而第一层又由 `Borrow` blanket 兜底）。这就是默认比较器能通吃所有合法键的根因。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：实现一个大小写不敏感的比较器，用 `SkipMap::with_comparator` 构造 map，验证插入 `"Abc"` 后用 `"abc"` 能查到同一键，且迭代顺序由自定义比较决定。

**依据**：项目自带的 `tests/map.rs::comparator` 测试就是用 `CaseComparator { case_sensitive: bool }` 做这件事，我们参考它的写法（[tests/map.rs:967-1028](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/tests/map.rs#L967-L1028)）。

**操作步骤**（示例代码，仿照真实测试）：

```rust
// 示例代码：大小写不敏感的 str 比较器
use std::cmp::Ordering;
use std::borrow::Borrow;
use crossbeam_skiplist::{SkipMap, comparator::{Comparator, Equivalator}};

struct CaseInsensitive;

impl<L: Borrow<str> + ?Sized, R: Borrow<str> + ?Sized> Equivalator<L, R> for CaseInsensitive {
    fn equivalent(&self, lhs: &L, rhs: &R) -> bool {
        // 都转小写后逐字符比较
        lhs.borrow().to_lowercase() == rhs.borrow().to_lowercase()
    }
}

impl<L: Borrow<str> + ?Sized, R: Borrow<str> + ?Sized> Comparator<L, R> for CaseInsensitive {
    fn compare(&self, lhs: &L, rhs: &R) -> Ordering {
        lhs.borrow().to_lowercase().cmp(&rhs.borrow().to_lowercase())
    }
}

let s: SkipMap<String, u32, _> = SkipMap::with_comparator(CaseInsensitive);
s.insert("Abc".to_string(), 1);

// 用小写的 "abc" 查到 "Abc"
assert!(s.get("abc").is_some());
assert_eq!(*s.get("abc").unwrap().value(), 1);
assert_eq!(s.len(), 1);

// 迭代顺序由「小写后」的字典序决定，而非 String 的默认 Ord
s.insert("Banana".to_string(), 2);
s.insert("apple".to_string(), 3);
let keys: Vec<&String> = s.iter().map(|e| e.key()).collect();
// 三个键小写后： "abc"(Abc) < "apple" < "banana"(Banana)
assert_eq!(keys, vec!["Abc", "apple", "Banana"]);
```

> 说明：这里比较器实现成 `impl<L: Borrow<str> + ?Sized, R: Borrow<str> + ?Sized>`，而不是字面的 `Comparator<&str>`。原因是 `SkipMap` 存储 `String` 键、却要用 `&str` 查询，所以比较器必须能同时处理「左操作数借自 `String`」「右操作数借自 `&str`」两种情况——`Borrow<str>` 约束正好统一了它们。这与项目测试 [tests/map.rs:978-1002](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/tests/map.rs#L978-L1002) 的写法一致。

**需要观察的现象**：

1. `s.get("abc")` 能命中 `"Abc"`（大小写不敏感判等）。
2. `s.len() == 1`：因为 `Abc` 与后续若有 `ABC` 会被判为「相等」，触发 `insert` 的「先删旧再插新」替换语义（见 u1-l2）。真实测试在 `case_sensitive: false` 下插 `abc` 再插 `ABC` 得到 `len == 1` 且值为后插入的 `2`（[tests/map.rs:1017-1027](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/tests/map.rs#L1017-L1027)）。
3. 迭代顺序按「小写后字典序」排，与 `String` 默认 `Ord`（大写字母排在小写前）不同。

**预期结果**：三条断言全部成立。

> 待本地验证：请在本机用 `cargo test` 或放入 `examples/` 后 `cargo run --example <name>` 确认。项目自带的 `comparator` 测试已覆盖等价场景，可直接 `cargo test --test map comparator` 作为参照。

#### 4.2.5 小练习与答案

**练习 1**：如果把上面 `CaseInsensitive` 的 `compare` 改成「仍用大小写敏感的 `cmp`」，但 `equivalent` 保持大小写不敏感，会发生什么？

**参考答案**：违反「`Comparator` 与 `Equivalator` 必须一致」的契约。跳表搜索时，`compare` 可能把 `"Abc"` 与 `"abc"` 判为 `Greater`（因 `'A' < 'a'` 反过来……实际 `'A'(0x41) < 'a'(0x61)`，所以 `"Abc" < "abc"`，判为 `Less`），从而沿着错误方向二分；随后 `equivalent` 又判它们相等，导致「定位到的位置」与「判等结果」矛盾，查询行为不确定。这就是注释里强约束的意义。

**练习 2**：为什么 `BasicComparator` 要派生 `Default`？

**参考答案**：因为 `SkipMap::new()` 的实现是 `Self::with_comparator(Default::default())`（[src/map.rs:543](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L543)），需要 `C: Default`。`BasicComparator` 是 ZST，`Default` 天然为零成本。

---

### 4.3 接入跳表：with_comparator 与 `C: Comparator<K, Q>` 约束

#### 4.3.1 概念说明

前两节定义了「比较是什么」，本节看它如何**接入数据结构**。要点有三：

1. **比较器是结构体的一个字段**：`SkipList`（base 层）和 `SkipMap` 都有一个泛型参数 `C`，默认 `BasicComparator`，作为字段 `comparator: C` 存着（[src/base.rs:478-479](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L478-L479)）。
2. **构造入口**：`SkipMap::with_comparator(C)` 把比较器传进去（[src/map.rs:59-63](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L59-L63)）；`new()` 等价于 `with_comparator(Default::default())`。
3. **两种约束**：写操作（`insert` / `front` / `iter`）只要求 `C: Comparator<K>`；读操作（`get` / `contains_key` / `lower_bound` / `upper_bound`）额外要求 `C: Comparator<K, Q>`，这个 `Q` 就是查询类型，正是 `&str` 查 `String` 的入口。

#### 4.3.2 核心流程

一次 `map.get("abc")` 的约束解析与调用链：

```text
map.get::<str>("abc")                     // map.rs:271，约束 C: Comparator<K, str>
  └─ inner.get(key, guard)                // base.rs:569，约束 C: Comparator<K, str>
       ├─ search_bound(Included(key), ..) // 二分定位：内部用 comparator.compare(&c.key, key)
       │     └─ self.comparator.compare(&c.key, key)   // base.rs:986
       └─ 若定位到节点 n：
             self.comparator.equivalent(&n.key, key)    // base.rs:576，二次确认是否真命中
```

二分定位用 `compare`（判序），最终确认用 `equivalent`（判等）——这就是 4.2.1 提到的「两步法」，也是两个 trait 必须一致的工程原因。

#### 4.3.3 源码精读

`SkipList` 的泛型默认值与比较器字段：

[`SkipList<K,V,C = BasicComparator>` 结构体与 `comparator` 字段](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L468-L479)：默认 `C = BasicComparator`，把比较器作为普通字段存储。

base 层的 `with_comparator` 构造（还要传 `Collector`，见 u2-l6）：

[`SkipList::with_comparator(collector, comparator)`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L493-L503)：把外部传入的比较器与 collector 一并存入。

`get` 方法——读路径的约束 `C: Comparator<K, Q>` 与二次确认：

[`SkipList::get` 的签名与 `equivalent` 二次确认](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L568-L585)：先用 `search_bound` 定位，再用 `self.comparator.equivalent(&n.key, key)` 确认命中（第 576 行），不等则返回 `None`。这里的 `Q` 正是查询类型。

`search_position` 中的二分比较调用：

[搜索循环中的 `comparator.compare(&c.key, key)`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L984-L993)：根据 `Greater`/`Equal`/`Less` 决定下降一层、记录命中、还是继续向右。这是跳表期望 O(log n) 查找的比较核心。

map 层的 `get` 与 `with_comparator`（人体工学封装，把 `Guard` 藏起来）：

[`SkipMap::with_comparator`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L59-L63)：用 `epoch::default_collector()` 构造 base 层，传入用户比较器。

[`SkipMap::get` 的约束 `C: Comparator<K, Q>`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L271-L278)：内部 `epoch::pin()` 拿临时 `Guard`，转调 `inner.get`，`try_pin_loop` 把 `RefEntry` 转 `Entry`（句柄细节见 u4-l12）。

> 一个有意思的类型约束出现在 `Range` 上：`C: Comparator<K> + Comparator<K, Q>`（[src/base.rs:1981](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L1981)）。区间迭代既要「键与键之间」比较（`Comparator<K>`），又要「键与区间端点 `Q`」比较（`Comparator<K, Q>`），所以两个约束并存。

#### 4.3.4 代码实践

**实践目标**：跟踪一次 `map.get` 的调用链，确认比较器的两个方法分别在哪一步被调用；并验证自定义比较器会改变迭代顺序。

**操作步骤**（源码阅读型实践）：

1. 打开 [src/map.rs:271-278](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/map.rs#L271-L278) 的 `SkipMap::get`，确认它转调 `self.inner.get(key, guard)`。
2. 跟到 [src/base.rs:568-585](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L568-L585)，看到第 575 行 `search_bound` 与第 576 行 `equivalent`。
3. 进一步在 `search_bound` → `search_position` 中找到 [第 986 行 `comparator.compare`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/src/base.rs#L984-L993)。
4. 运行 4.2.4 的 `CaseInsensitive` 示例，把 `keys` 的断言改成与 `String` 默认 `Ord` 一致的预期（即不使用自定义比较器时的顺序），对比两种顺序的差异。

**需要观察的现象**：

- 用默认 `SkipMap::<String,_>::new()` 时，迭代顺序按 `String` 的 `Ord`（ASCII 序，大写在小写前）。
- 用 `with_comparator(CaseInsensitive)` 时，顺序变为小写后的字典序。
- 两者不同，证明比较器确实驱动了跳表的排序，而非只是查询时的一个旁路判断。

**预期结果**：自定义比较器下顺序为 `["Abc", "apple", "Banana"]`（小写后 `"abc" < "apple" < "banana"`）；默认比较器下顺序为 `["Abc", "Banana", "apple"]`（`'B'(0x42) < 'a'(0x61)`，故 `Banana` 排在 `apple` 前）。

> 待本地验证：本机运行确认两种顺序的对比。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `front` / `iter` 只要求 `C: Comparator<K>`，而 `get` 要求 `C: Comparator<K, Q>`？

**参考答案**：`front` / `iter` 只在「键与键之间」比较，左操作数与右操作数都是 `&K`，所以 `Comparator<K>`（即 `R = L = K`）足够。`get` 要用任意查询类型 `Q`（如 `&str` 查 `String`）去比较，所以需要更强的 `Comparator<K, Q>`。

**练习 2**：`search_position` 用 `compare` 已经能定位到「等于」的位置，为什么 `get` 还要再调一次 `equivalent` 确认？

**参考答案**：`search_bound` 用 `Bound::Included(key)` 定位到「第一个 `>= key` 的节点」，它返回的节点可能只是「下界」而非「精确命中」（例如查 `key=5` 而最小的是 `7`）。因此需要再用 `equivalent` 判定该节点的键是否真的等于查询键，不等的返回 `None`。两步法分别承担「定位」与「确认」职责。

---

## 5. 综合实践

把本讲的三层知识串起来，完成下面这个综合任务：

**任务**：为 `SkipMap<String, u32>` 实现一个「字符串长度升序、长度相同按小写字典序」的比较器 `LengthThenCi`，并完成以下验证。

要求：

1. 同时实现 `Equivalator<String, str>`（或更泛化的 `L: Borrow<str>` 版本）和 `Comparator`，保证二者一致。
2. 用 `SkipMap::with_comparator(LengthThenCi)` 构造 map。
3. 插入 `"cat"`、`"Dog"`、`"xy"`、`"ab"`、`"Bird"`。
4. 验证：
   - `get("dog")` 能命中 `"Dog"`（长度相同 + 小写后相等）。
   - `get("CAT")` 能命中 `"cat"`。
   - 迭代顺序为：`ab`、`xy`（长度 2 在前，小写后 `ab < xy`），然后 `cat`、`Dog`（长度 3，小写后 `cat < dog`），最后 `Bird`（长度 4）。
5. 用一段文字解释：为什么长度不同时直接比长度、长度相同时再比小写字符串，这个逻辑必须**同时**出现在 `compare` 与 `equivalent` 中，否则会出现 4.2.5 练习 1 那种「定位与判等矛盾」的 bug。

**参考思路**（伪代码，非项目原有代码）：

```rust
// 示例代码：仅供参考的参考思路
fn key(s: &str) -> (usize, String) { (s.len(), s.to_lowercase()) }
// compare:  key(lhs).cmp(&key(rhs))
// equivalent: key(lhs) == key(rhs)
```

**完成标志**：所有断言通过，并能用自己的话讲清「比较器是结构体字段 + `C: Comparator<K, Q>` 约束 + 定位用 compare / 确认用 equivalent」这条主线。

> 待本地验证：综合实践的运行结果请在本机确认。

## 6. 本讲小结

- 比较能力被拆成**两层**：`equivalent.rs` 的 `Equivalent` / `Comparable`（类型驱动，靠 `Borrow` + `Ord` blanket 兜底）与 `comparator.rs` 的 `Equivalator` / `Comparator`（运行时驱动，带 `&self`）。
- **「用 `&str` 查 `String`」** 的机制：`String: Borrow<str>` + `str: Ord` ⇒ blanket 给出 `String: Comparable<str>` ⇒ `BasicComparator: Comparator<String, str>` ⇒ `get` 的 `Q = str` 合法。
- `BasicComparator` 是 ZST 默认比较器，它的两个 `impl` 把第二层**委托**回第一层，于是默认行为与 `BTreeMap` 一致。
- 自定义 `Comparator` **绕过第一层**，用运行时逻辑取代，可改变排序与相等判定（如大小写不敏感）。
- 接入跳表：比较器作为 `C` 字段存于结构体，`with_comparator` 注入；读方法约束 `C: Comparator<K, Q>`，其中 `Q` 是查询类型。
- 查询走「两步法」：`search_position` 用 `compare` 二分定位，`get` 用 `equivalent` 二次确认命中——所以 `Comparator` 与 `Equivalator` 必须对「相等」一致。

## 7. 下一步学习建议

本讲把「键比较」这条横切关注点讲完了，接下来可以回到 `base.rs` 的并发算法主线：

- **u3-l8 搜索算法**：精读 `search_bound` / `search_position` / `next_node`，看 4.3.3 里那个 `comparator.compare` 是如何配合多层塔、被标记节点的 `continue 'search` 重启一起工作的。
- **u3-l9 标记指针与逻辑删除**：理解逻辑删除如何与搜索交错，以及 `help_unlink` 的协助清理。
- 若对「自定义查询类型」感兴趣，可先读 [tests/base.rs:906-960](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-skiplist/tests/base.rs#L906-L960) 的 `comparable_get` 测试——它手写 `Equivalent` / `Comparable`，用一个零拷贝的 `FooRef<'a>` 视图去查 `Foo` 键，是第一层 trait 的进阶用法。

本讲义覆盖的最小模块：`Equivalent` / `Comparable` 及其 `Borrow` blanket 实现，`Equivalator` / `Comparator` / `BasicComparator` 的运行时比较器设计，以及比较器通过 `with_comparator` 与 `C: Comparator<K, Q>` 约束接入跳表读路径的机制。
