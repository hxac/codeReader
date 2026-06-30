# Key Expression：Zenoh 的地址空间

> 承接《u2-l1 打开一个 Session》。上一讲我们用 `zenoh::open(config)` 拿到了 `Session`，但还没有真正回答一个更基础的问题：**Zenoh 用什么来「定位」一条数据？** 答案就是本讲的主角——**Key Expression（键表达式，简称 KE）**。它是 Zenoh 的「地址空间」，决定了你的发布会被谁收到、你的订阅能匹配到谁。

## 1. 本讲目标

学完本讲，你应当能够：

1. 理解 Key Expression 的斜杠路径语法，以及通配符 `*`（单层）与 `**`（多层）的语义。
2. 区分三种存储形式 `keyexpr`、`OwnedKeyExpr`、`KeyExpr` 的所有权模型，知道何时用哪一个。
3. 理解「规范化（canonization）」为什么是 Key Expression 的强约束，并知道哪些写法会被自动改写。
4. 会用 `includes` / `intersects` 判断两个 Key Expression 之间的集合关系（包含 / 相交）。

## 2. 前置知识

在开始前，建议你已经具备（若不熟悉也没关系，本讲会顺带解释）：

- **集合与字符串的关系直觉**：一条 Key Expression 本质上表示「一批 key 的集合」。例如 `robot/sensor/*` 不是某一个 key，而是「`robot/sensor/` 下任意一个 chunk」这一整类 key。
- **Rust 的所有权与借用**：`&str`（借用字符串切片）、`String`（拥有堆字符串）、`Arc<str>`（引用计数共享字符串）、`Cow<str>`（可借可有的写时复制）。本讲会反复和这四种模型做类比。
- **glob 通配符**：如果你用过 shell 的 `*.rs` 或 `**/*.rs`，本讲的 `*` / `**` 含义非常接近。
- 已阅读《u2-l1》，知道 `Session` 与 `Config` 的存在。

一个核心心智模型，先记在心里：

> **在 Zenoh 里，你不把消息发给「一个 IP 地址」，而是发给「一个 Key Expression」。匹配（谁收得到）完全由两端 Key Expression 是否相交决定。**

## 3. 本讲源码地图

本讲涉及的关键源码文件如下：

| 文件 | 作用 |
| --- | --- |
| [commons/zenoh-keyexpr/src/lib.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-keyexpr/src/lib.rs) | `zenoh-keyexpr` 内部 crate 的总入口，说明「KE 是 Zenoh 的地址空间」并提供三种存储风味。 |
| [commons/zenoh-keyexpr/src/key_expr/borrowed.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-keyexpr/src/key_expr/borrowed.rs) | 定义 `keyexpr`（借用的核心类型）、语法校验、`intersects`/`includes`/`relation_to`、`SetIntersectionLevel`。 |
| [commons/zenoh-keyexpr/src/key_expr/owned.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-keyexpr/src/key_expr/owned.rs) | 定义 `OwnedKeyExpr`（`Arc<str>` 风味）与构造器。 |
| [commons/zenoh-keyexpr/src/key_expr/canon.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-keyexpr/src/key_expr/canon.rs) | 实现 `Canonize` trait 与规范化算法。 |
| [commons/zenoh-keyexpr/src/key_expr/include.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-keyexpr/src/key_expr/include.rs) | `includes` 的核心算法（`LTRIncluder`）。 |
| [commons/zenoh-keyexpr/src/key_expr/intersect/mod.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-keyexpr/src/key_expr/intersect/mod.rs) | `intersects` 的核心算法（`ClassicIntersector`）。 |
| [zenoh/src/api/key_expr.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/key_expr.rs) | 公开 API 的 `KeyExpr`（`Cow` 风味），可携带 Session 声明优化。 |
| [zenoh/src/lib.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs) | 把上述类型重新导出为公开的 `zenoh::key_expr` 模块门面。 |

> 提醒（参见《u1-l3》《u1-l4》）：`commons/zenoh-keyexpr` 是**内部 crate**，不保证稳定；写应用时请通过 `zenoh::key_expr::...` 这个门面使用。但读源码理解原理时，主角恰恰在 `commons/zenoh-keyexpr` 里。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**KeyExpr/keyexpr（三种存储形式）** → **canonicalization（规范化）** → **includes/intersects（集合关系）**。

### 4.1 KeyExpr/keyexpr：三种存储形式

#### 4.1.1 概念说明

一条 Key Expression 在内存里要被「存」起来。Zenoh 针对不同生命周期场景，提供了三种存储形式，可以类比为 Rust 标准库里三种字符串：

| Zenoh 类型 | 类比标准库类型 | 特点 |
| --- | --- | --- |
| `keyexpr` | `str` | 不可变的借用类型，无所有权，编译期已知是合法 KE。 |
| `OwnedKeyExpr` | `Arc<str>` | 拥有所有权、可廉价克隆（引用计数）、可长期持有。 |
| `KeyExpr` | `Cow<str>` | 可借可拥有，**额外**携带 Zenoh 内部的「声明优化」上下文。 |

源码注释本身就把这层类比写得非常清楚：

- 三种风味的总述：[commons/zenoh-keyexpr/src/lib.rs:29-36](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-keyexpr/src/lib.rs#L29-L36) 说明 `keyexpr`≈`str`、`OwnedKeyExpr`≈`Arc<str>`、`KeyExpr`≈`Cow<str>` 并携带路由优化上下文。
- 公开门面的同款说明：[zenoh/src/lib.rs:300-316](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L300-L316)。

#### 4.1.2 核心流程

三种类型之间的「依赖关系」是一条单向链，理解它就能掌握所有权流向：

```text
                       deref            deref
   keyexpr (str)  <────────  OwnedKeyExpr (Arc<str>)
       ▲                            
       │ deref                       
   KeyExpr (Cow + 声明上下文) ──into_owned──> OwnedKeyExpr
```

要点：

1. **三者都 `Deref` 到 `keyexpr`**。也就是说，`intersects` / `includes` 这些「真正干活」的方法其实只定义在 `keyexpr` 上，另外两个类型通过自动解引用「免费」获得它们。所以无论你手里拿的是哪种，写法都一样。
2. **`keyexpr` 是地基**：它是 `#[repr(transparent)]` 的新类型，包裹的就是一个 `str`，零额外开销。
3. **`KeyExpr` 最「重」但也最强**：它可能是借用、可能是拥有，还可能记录「这条 KE 已经在某个 `Session` 上声明过」的优化信息（`KeyExprWireDeclaration`），用于让 Zenoh 在网络上用更短的标识符代替完整字符串发送（后续内部架构讲义会展开）。

#### 4.1.3 源码精读

**`keyexpr` 的定义**——注意它只是 `str` 的透明包装：

```rust
// commons/zenoh-keyexpr/src/key_expr/borrowed.rs
#[allow(non_camel_case_types)]
#[repr(transparent)]
#[derive(PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct keyexpr(str);
```

参见 [commons/zenoh-keyexpr/src/key_expr/borrowed.rs:47-50](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-keyexpr/src/key_expr/borrowed.rs#L47-L50)。`repr(transparent)` 保证它在内存布局上和 `str` 完全一致，可以安全地在两者间 `transmute`（见同文件 `from_str_unchecked`）。

> 命名提示：`keyexpr` 故意写成全小写（`#[allow(non_camel_case_types)]`），就是为了让它在视觉上接近 `str`——它就是一个「保证合法的 str」。

**`OwnedKeyExpr` 的定义**——`Arc<str>` 风味：

```rust
// commons/zenoh-keyexpr/src/key_expr/owned.rs
#[derive(Clone, PartialEq, Eq, Hash, serde::Deserialize)]
#[serde(try_from = "String")]
pub struct OwnedKeyExpr(pub(crate) Arc<str>);
```

参见 [commons/zenoh-keyexpr/src/key_expr/owned.rs:30-33](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-keyexpr/src/key_expr/owned.rs#L30-L33)。`Clone` 只是增加引用计数，因此把 `OwnedKeyExpr` 放进结构体、跨线程传递都很便宜。注意 `#[serde(try_from = "String")]`：反序列化时会走校验，保证从配置文件读出来的字符串一定是合法 KE。

**`KeyExpr` 的定义**——公开 API 的 `Cow` 风味：

```rust
// zenoh/src/api/key_expr.rs
#[derive(Clone, Debug, serde::Deserialize, serde::Serialize)]
#[serde(from = "OwnedKeyExpr")]
#[serde(into = "OwnedKeyExpr")]
pub struct KeyExpr<'a>(pub(crate) KeyExprInner<'a>);
```

参见 [zenoh/src/api/key_expr.rs:85-89](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/key_expr.rs#L85-L89)。它带生命周期参数 `'a`：内部可能是 `Borrowed`（借用别人的 `keyexpr`），也可能是 `Owned`（自己拥有 `OwnedKeyExpr`）。它还 `Deref` 到 `keyexpr`：

```rust
// zenoh/src/api/key_expr.rs
impl std::ops::Deref for KeyExpr<'_> {
    type Target = keyexpr;
    fn deref(&self) -> &Self::Target { /* 取出内部的 keyexpr */ }
}
```

参见 [zenoh/src/api/key_expr.rs:97-105](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/key_expr.rs#L97-L105)。正因为这条 `Deref`，后面你才能直接在 `KeyExpr` 上调用 `includes` / `intersects`。

**安全的构造入口** `KeyExpr::new`：

```rust
// zenoh/src/api/key_expr.rs
pub fn new<T, E>(t: T) -> Result<Self, E>
where Self: TryFrom<T, Error = E> {
    Self::try_from(t)
}
```

参见 [zenoh/src/api/key_expr.rs:143-148](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/key_expr.rs#L143-L148)。它本质是 `TryFrom` 的语法糖——传入字符串不合法时会返回 `Err`。`lib.rs` 顶部给了一个最小用法示范：用 `sensor.join("**")` 构造 `robot/sensor/**`，再断言它 `includes` 子表达式，参见 [zenoh/src/lib.rs:332-341](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L332-L341)。

#### 4.1.4 代码实践

**实践目标**：用一段最小 Rust 代码，亲手感受三种存储形式的差异（所有权 / 克隆）。

**操作步骤**（这是源码阅读 + 类型对照型实践，不需要网络）：

1. 新建一个依赖 `zenoh`（默认 feature 即可）的小 crate。
2. 阅读并对照下表，确认三种类型各自 deref 到 `keyexpr`：

```rust
// 示例代码（非项目原有，用于练习）
use zenoh::key_expr::{keyexpr, OwnedKeyExpr, KeyExpr};

// 1) 借用形式：keyexpr ≈ &str，不拥有数据
let borrowed: &keyexpr = keyexpr::new("robot/sensor").unwrap();

// 2) 拥有形式：OwnedKeyExpr ≈ Arc<str>，可廉价 clone
let owned: OwnedKeyExpr = OwnedKeyExpr::new("robot/sensor").unwrap();
let _cloned = owned.clone(); // 只增加引用计数

// 3) Cow 形式：KeyExpr，公开 API 推荐使用
let ke: KeyExpr = KeyExpr::new("robot/sensor").unwrap();
println!("as_str = {}", ke.as_str()); // 通过 deref 到 keyexpr 再到 str
```

3. 把 `borrowed`、`owned`、`ke` 三者都打印出来，观察它们字符串内容一致。

**需要观察的现象**：三者打印结果都是 `robot/sensor`；`owned.clone()` 不会产生新的堆分配（引用计数）。

**预期结果**：编译通过，输出 `as_str = robot/sensor`。若想验证引用计数行为，可在 clone 前后观察 `Arc::strong_count`（需访问内部字段，可借助 `OwnedKeyExpr: From<OwnedKeyExpr> for Arc<str>`，参见 [owned.rs:161-165](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-keyexpr/src/key_expr/owned.rs#L161-L165)）。运行结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么不直接用 `String` 存 Key Expression，而要专门造三个类型？

> **答案**：因为 KE 有严格的语法/规范化不变量（下节讲）。用 `String` 无法在类型层面保证「这串字符一定是合法且规范的 KE」；而 `keyexpr` / `OwnedKeyExpr` / `KeyExpr` 在构造时已经校验过，类型即证明，后续无需反复校验，既安全又快。

**练习 2**：`KeyExpr` 为什么带生命周期 `'a`，而 `OwnedKeyExpr` 不带？

> **答案**：`OwnedKeyExpr` 内部是 `Arc<str>`，自给自足，生命周期独立于任何外部借用，故无需参数；`KeyExpr` 可能是「借用某段 `keyexpr`」的形式（`KeyExprInner::Borrowed`），此时它不能活过被借用的数据，所以需要 `'a` 来表达这个约束。

### 4.2 Canonicalization：规范化是强约束

#### 4.2.1 概念说明

Key Expression 是一种「集合语言」：**同一个集合可能有好几种写法**。例如 `a/**/**/b` 和 `a/**/b` 表示完全相同的 key 集合。如果允许两种写法并存，那么判断「两条 KE 是否相等」就会变成复杂的集合运算，而不是简单的字符串比较。

Zenoh 的设计选择是：**强制所有 Key Expression 处于「规范形（canon form）」**，使得「字符串相等」等价于「集合相等」。这就是 `keyexpr` 的核心不变量之一，源码注释原文是：

> Key expression must be in canon-form (this ensures that key expressions representing the same set are always the same string).

参见 [commons/zenoh-keyexpr/src/key_expr/borrowed.rs:38-40](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-keyexpr/src/key_expr/borrowed.rs#L38-L40)。

#### 4.2.2 核心流程

先看语法规则（哪些字符串能成为合法 KE），再看规范化会做哪些改写。

**语法规则**（来自 [borrowed.rs:34-46](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-keyexpr/src/key_expr/borrowed.rs#L34-L46)）：

1. KE 是以 `/` 分隔的非空 UTF-8 chunk 列表。例如 `a/b/c`。
2. 不能以 `/` 开头或结尾，也不能含 `//`（即不允许空 chunk）。
3. 禁止出现字符 `#`、`$`（除 `$*` 写法外）、`?`。这几个字符在 Zenoh 协议里有特殊用途（`?` 用于 Selector，见《u4-l3》）。
4. 必须是规范形。

**通配符**：

- `*`：匹配**恰好一个** chunk。`a/*` 匹配 `a/b`、`a/c`，但不匹配 `a` 或 `a/b/c`。
- `**`：匹配**任意数量**（含 0 个）chunk。`a/**` 匹配 `a`、`a/b`、`a/b/c`……
- `$*`：DSL 级别的「chunk 内通配」（在单个 chunk 内部匹配），属于进阶用法，规范化后通常会被改写成 `*`。

**规范化规则**（源码在 `canon.rs`，并在测试里逐条列出，参见 [canon.rs:132-168](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-keyexpr/src/key_expr/canon.rs#L132-L168)）：

| 原写法 | 规范形 | 规则 |
| --- | --- | --- |
| `a/$*$*/b` | `a/$*/b` | 连续的 `$*` 合并成一个 `$*`。 |
| `a/**/**/b` | `a/**/b` | 连续的 `**` chunk 合并成一个 `**`。 |
| `a/$*/b` | `a/*/b` | 独占一个 chunk 的 `$*` 等价于 `*`。 |
| `a/**/*` | `a/*/**` | `**` 后跟 `*` 时，必须改成 `*` 在前、`**` 在后。 |

伪代码描述校验与规范化的大致控制流：

```text
输入字符串 s
  ├─ 若 s 为空 或 以 '/' 结尾          => 拒绝(EmptyChunk)
  ├─ 逐字节扫描，对每个 chunk 校验：
  │     ├─ '*' 只能出现在 chunk 开头
  │     ├─ "**" 后只能是 '/' 或结尾
  │     ├─ '$' 只能出现在 "$*" 中
  │     └─ 出现 '#' 或 '?'            => 拒绝(SharpOrQMark)
  └─ 校验通过：返回 &keyexpr（零拷贝）

构造为 OwnedKeyExpr / KeyExpr 时，可选择 autocanonize：
     先就地 canonize（合并通配、调整顺序），再校验
```

#### 4.2.3 源码精读

**校验主逻辑**：`TryFrom<&str> for &keyexpr`。开头先挡掉空串和尾斜杠：

```rust
// commons/zenoh-keyexpr/src/key_expr/borrowed.rs
fn try_from(value: &'a str) -> Result<Self, Self::Error> {
    use KeyExprError::*;
    if value.is_empty() || value.ends_with('/') {
        return Err(EmptyChunk.into_err(value));
    }
    // ...逐字节扫描...
}
```

参见 [commons/zenoh-keyexpr/src/key_expr/borrowed.rs:784-788](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-keyexpr/src/key_expr/borrowed.rs#L784-L788)。后续的 `while` 循环（[L796-877](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-keyexpr/src/key_expr/borrowed.rs#L796-L877)）用一个状态机式的字节扫描检查 `*` / `**` / `$*` 的位置合法性，关键分支如下：

```rust
b'*' if i != chunk_start => return Err(StarInChunk.into_err(value)), // * 只能在 chunk 开头
b'#' | b'?' => return Err(SharpOrQMark.into_err(value)),              // 禁用 # 和 ?
```

参见 [commons/zenoh-keyexpr/src/key_expr/borrowed.rs:810-811](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-keyexpr/src/key_expr/borrowed.rs#L810-L811) 与 [L872-873](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-keyexpr/src/key_expr/borrowed.rs#L872-L873)。所有错误归类在 `KeyExprError` 枚举里，每种都带可读的中文/英文提示：

```rust
// commons/zenoh-keyexpr/src/key_expr/borrowed.rs
enum KeyExprError {
    LoneDollarStar = -1,
    SingleStarAfterDoubleStar = -2,
    DoubleStarAfterDoubleStar = -3,
    EmptyChunk = -4,
    StarInChunk = -5,
    DollarAfterDollar = -6,
    SharpOrQMark = -7,
    UnboundDollar = -8,
}
```

参见 [commons/zenoh-keyexpr/src/key_expr/borrowed.rs:752-762](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-keyexpr/src/key_expr/borrowed.rs#L752-L762)。比如 `**/*` 这种非规范写法对应 `SingleStarAfterDoubleStar`，错误提示明确告诉你要改成 `*/**`（[L770](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-keyexpr/src/key_expr/borrowed.rs#L770)）。

**规范化算法**：`Canonize` trait + `canonize()` 函数。trait 定义见 [canon.rs:18-20](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-keyexpr/src/key_expr/canon.rs#L18-L20)，核心是就地改写字节：

```rust
// commons/zenoh-keyexpr/src/key_expr/canon.rs
pub trait Canonize {
    fn canonize(&mut self);
}
// 为 &mut str 与 String 都实现了 Canonize，就地改写，不额外分配
```

参见 [canon.rs:96-115](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-keyexpr/src/key_expr/canon.rs#L96-L115)。规范的 4 条规则在单元测试里逐条断言，例如把 `**/*` 改写成 `*/**`：

```rust
// commons/zenoh-keyexpr/src/key_expr/canon.rs
let mut s = String::from("hello/**/*");
s.canonize();
assert_eq!(s, "hello/*/**");
```

参见 [canon.rs:165-168](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-keyexpr/src/key_expr/canon.rs#L165-L168)。

**用户侧入口** `KeyExpr::autocanonize`：

```rust
// zenoh/src/api/key_expr.rs
pub fn autocanonize<T, E>(mut t: T) -> Result<Self, E>
where Self: TryFrom<T, Error = E>, T: Canonize {
    t.canonize();
    Self::new(t)
}
```

参见 [zenoh/src/api/key_expr.rs:199-206](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/key_expr.rs#L199-L206)。当你不确定输入字符串是否规范时，用 `autocanonize` 代替 `new`，它会先就地规范化再校验。注意 `new` 不会自动规范化——传入非规范字符串会直接报错（见 `keyexpr::new` 文档，[borrowed.rs:53-58](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-keyexpr/src/key_expr/borrowed.rs#L53-L58)）。

> 一个常被忽略的细节：`autocanonize` 是**零额外分配**的——它在原字符串的字节缓冲上就地改写，尾部剩余字节用 `\0` 填充或截断（[canon.rs:98-104](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-keyexpr/src/key_expr/canon.rs#L98-L104)）。这也是它性能友好的原因。

#### 4.2.4 代码实践

**实践目标**：亲手触发规范化，观察「等价集合被统一成同一字符串」。

**操作步骤**：

1. 阅读测试 `autocanon`（[borrowed.rs:915-920](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-keyexpr/src/key_expr/borrowed.rs#L915-L920)），它断言 `hello/**/*` 规范化后是 `hello/*/**`。
2. 自己写一段（示例代码，非项目原有）：

```rust
// 示例代码（非项目原有，用于练习）
use zenoh::key_expr::KeyExpr;

// 非规范写法：连续 ** 与 **/* 顺序
let a = KeyExpr::autocanonize(String::from("robot/**/**/sensor/*")).unwrap();
// 规范写法
let b = KeyExpr::new("robot/**/sensor/*").unwrap();
println!("autocanonize = {a}");
println!("new          = {b}");
assert_eq!(a.as_str(), b.as_str()); // 规范化后字符串相等
```

3. 再尝试一个**非法**输入，观察报错：

```rust
// 示例代码（非项目原有，用于练习）
let bad = KeyExpr::new("robot/**/*"); // **/* 是非规范形
println!("{:?}", bad.err());           // 预期：校验失败
```

**需要观察的现象**：

- 第 2 步：两行打印完全相同（`robot/**/sensor/*`，因为连续 `**` 合并；若你写的串含 `**/*`，会进一步被改成 `*/**`）。
- 第 3 步：`KeyExpr::new("robot/**/*")` 返回 `Err`，错误信息会提示 `**/*` 必须改为 `*/**`。

**预期结果**：第 2 步断言通过；第 3 步打印出非 `None` 的错误。运行结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：把 `a/b$*$*/c` 规范化后会得到什么？为什么？

> **答案**：`a/b$*/c`。因为同一 chunk 内连续的 `$*` 合并成一个 `$*`（见 [canon.rs:135-138](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-keyexpr/src/key_expr/canon.rs#L135-L138)）。注意这里是 chunk 内的 `$*`，不是独占 chunk，所以不会被进一步替换成 `*`。

**练习 2**：下列哪些字符串能被 `KeyExpr::new` 接受（即本身已规范）？
`a/b/c`、`/a/b`、`a//b`、`a/**`、`a/**/*`、`a/b?c`。

> **答案**：只有 `a/b/c` 和 `a/**` 被接受。`/a/b` 以 `/` 开头、`a//b` 含空 chunk、`a/**/*` 是非规范形、`a/b?c` 含禁用字符 `?`，全部被拒。

### 4.3 includes / intersects：集合关系判断

#### 4.3.1 概念说明

回到「Key Expression = 集合」这个心智模型。两个集合之间有四种基本关系，Zenoh 用 `SetIntersectionLevel` 枚举刻画（[borrowed.rs:705-729](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-keyexpr/src/key_expr/borrowed.rs#L705-L729)）：

| 关系 | 数学含义 | Zenoh 判断方法 | 举例 |
| --- | --- | --- | --- |
| Disjoint（不相交） | \(A \cap B = \varnothing\) | `!intersects` | `a/**` 与 `b/**` |
| Intersects（相交） | \(A \cap B \neq \varnothing\) | `intersects` | `a/*` 与 `*/a`（交于 `a/a`） |
| Includes（包含） | \(B \subseteq A\) | `includes` | `a/**` includes `a/b/**` |
| Equals（相等） | \(A = B\) | 字符串相等即可 | `a/*` 与 `a/*` |

注意包含关系蕴含相交：\(B \subseteq A \Rightarrow A \cap B = B \neq \varnothing\)。源码里把这四者排成偏序 `Disjoint < Intersects < Includes < Equals`（[borrowed.rs:731-738](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-keyexpr/src/key_expr/borrowed.rs#L731-L738)），于是可以 `level >= Includes` 这样写。

**这两个方法为什么重要？** 因为 Zenoh 网络的所有「匹配」本质就是在问相交/包含：订阅者声明 `a/**`，发布者发 `a/b/c`，网络要判断「这条消息该不该送给这个订阅者」——就是一次 `intersects` 判断。

#### 4.3.2 核心流程

公开 API 只暴露两个方法（定义在 `keyexpr` 上，`KeyExpr`/`OwnedKeyExpr` 经 deref 使用）：

```rust
pub fn intersects(&self, other: &Self) -> bool; // 是否有交集
pub fn includes(&self, other: &Self) -> bool;   // self 是否包含 other
```

内部各自委托给一个预置的算法对象：

```text
keyexpr::intersects(self, other)
   └─> DEFAULT_INTERSECTOR.intersect(self, other)   // ClassicIntersector
keyexpr::includes(self, other)
   └─> DEFAULT_INCLUDER.includes(self, other)       // LTRIncluder
```

`DEFAULT_INTERSECTOR` 与 `DEFAULT_INCLUDER` 是模块级常量。算法都按 `/` 把表达式切成 chunk，再逐 chunk 比对通配符。`includes` 采用「从左到右（LTR）」的包含判定：遇到 `**` 时尝试匹配任意多 chunk，遇到普通 chunk 则要求两侧精确匹配。

补充：`relation_to` 一次性返回 `SetIntersectionLevel`，但它比分别调用 `intersects`/`includes` 慢，且需要 `unstable` feature（见 [borrowed.rs:93-110](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-keyexpr/src/key_expr/borrowed.rs#L93-L110)）。日常用 `intersects`/`includes` 即可。

#### 4.3.3 源码精读

**两个公开方法**：

```rust
// commons/zenoh-keyexpr/src/key_expr/borrowed.rs
/// Returns true if the keyexprs intersect, i.e. there exists at least one key
/// which is contained in both of the sets defined by self and other.
pub fn intersects(&self, other: &Self) -> bool {
    use super::intersect::Intersector;
    super::intersect::DEFAULT_INTERSECTOR.intersect(self, other)
}

/// Returns true if self includes other, i.e. the set defined by self contains
/// every key belonging to the set defined by other.
pub fn includes(&self, other: &Self) -> bool {
    use super::include::Includer;
    super::include::DEFAULT_INCLUDER.includes(self, other)
}
```

参见 [commons/zenoh-keyexpr/src/key_expr/borrowed.rs:81-91](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-keyexpr/src/key_expr/borrowed.rs#L81-L91)。注意 `intersects` 的语义是「至少存在一个 key 同时属于两个集合」。

**`DEFAULT_INTERSECTOR` 与 `DEFAULT_INCLUDER` 的定义**：

```rust
// commons/zenoh-keyexpr/src/key_expr/intersect/mod.rs
pub const DEFAULT_INTERSECTOR: ClassicIntersector = ClassicIntersector;
```

参见 [intersect/mod.rs:21](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-keyexpr/src/key_expr/intersect/mod.rs#L21)。

```rust
// commons/zenoh-keyexpr/src/key_expr/include.rs
pub const DEFAULT_INCLUDER: LTRIncluder = LTRIncluder;
```

参见 [include.rs:16](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-keyexpr/src/key_expr/include.rs#L16)。`LTRIncluder` 在处理 `**`（双通配 chunk）时会递归地「吃掉」右侧若干 chunk，核心片段如下：

```rust
// commons/zenoh-keyexpr/src/key_expr/include.rs
if lchunk == DOUBLE_WILD {
    if (lempty && !right.has_verbatim()) || (!lempty && self.includes(lrest, right)) {
        return true;
    }
    // ...
}
```

参见 [include.rs:41-51](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-keyexpr/src/key_expr/include.rs#L41-L51)。这正对应「`**` 可以匹配任意多 chunk」的语义。

**关系枚举与偏序**：

```rust
// commons/zenoh-keyexpr/src/key_expr/borrowed.rs
pub enum SetIntersectionLevel {
    Disjoint,
    Intersects,
    Includes,
    Equals,
}
```

参见 [borrowed.rs:722-729](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-keyexpr/src/key_expr/borrowed.rs#L722-L729)，偏序断言见 [borrowed.rs:731-738](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-keyexpr/src/key_expr/borrowed.rs#L731-L738)。

#### 4.3.4 代码实践

**实践目标**：用 `includes` / `intersects` 验证一组 Key Expression 之间的集合关系。

**操作步骤**（纯本地计算，无需网络）：

```rust
// 示例代码（非项目原有，用于练习）
use zenoh::key_expr::KeyExpr;

fn main() {
    let star = KeyExpr::new("robot/sensor/*").unwrap();   // 单层通配
    let temp = KeyExpr::new("robot/sensor/temp").unwrap(); // 具体 key
    let all  = KeyExpr::new("robot/**").unwrap();          // 多层通配

    // 1) 单层通配是否包含具体 key？
    println!("robot/sensor/* includes robot/sensor/temp : {}", star.includes(&temp));
    // 2) 二者是否相交？
    println!("robot/sensor/* intersects robot/sensor/temp : {}", star.intersects(&temp));
    // 3) 多层通配是否包含单层通配？
    println!("robot/** includes robot/sensor/* : {}", all.includes(&star));
}
```

**需要观察的现象**：三条打印都应为 `true`。

**预期结果**：

```text
robot/sensor/* includes robot/sensor/temp : true
robot/sensor/* intersects robot/sensor/temp : true
robot/** includes robot/sensor/* : true
```

解释：`*` 匹配单个 chunk `temp`，所以 `robot/sensor/*` 既相交于、也包含 `robot/sensor/temp`；`**` 能匹配任意多 chunk，因此 `robot/**` 包含 `robot/sensor/*`（匹配掉 `sensor/*` 这两段）。运行结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`a/*` 与 `*/a` 是否相交？相交在哪个具体 key 上？

> **答案**：相交，交于 `a/a`。`a/*` 匹配 `a/a`（`*`→`a`），`*/a` 也匹配 `a/a`（`*`→`a`），二者都覆盖 `a/a`，故 `intersects` 为真。这正是源码注释里的经典例子（[borrowed.rs:44](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-keyexpr/src/key_expr/borrowed.rs#L44)）。

**练习 2**：`a/**` 是否 includes `a/b/**`？反过来呢？

> **答案**：`a/**` includes `a/b/**` 为真（`**` 可匹配 `b/**`）；反过来 `a/b/**` includes `a/**` 为假（`a/**` 能匹配 `a/c`，但 `a/b/**` 不能匹配 `a/c`，后者缺 `b` 这一段）。参见源码注释 [borrowed.rs:45](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-keyexpr/src/key_expr/borrowed.rs#L45)。

## 5. 综合实践

把本讲三个模块串起来，完成下面的「机器人传感器」地址空间设计任务。这是本讲规格里指定的核心实践。

**任务**：用 `KeyExpr::new` 构造 `robot/sensor/*` 与 `robot/sensor/temp`，调用 `includes` 与 `intersects` 验证二者关系；再构造 `robot/**`，验证它能 `includes` 前者；最后用 `autocanonize` 处理一个非规范写法，确认规范化后字符串与规范形一致。把所有结果打印出来。

**参考实现**（示例代码，非项目原有）：

```rust
// 示例代码（非项目原有）
use zenoh::key_expr::KeyExpr;

fn main() {
    // —— 模块 4.1：构造三种形式 ——
    let pattern = KeyExpr::new("robot/sensor/*").unwrap();
    let concrete = KeyExpr::new("robot/sensor/temp").unwrap();
    println!("pattern = {pattern}, concrete = {concrete}");

    // —— 模块 4.3：集合关系判断 ——
    println!("includes   ? {}", pattern.includes(&concrete));   // true
    println!("intersects ? {}", pattern.intersects(&concrete));  // true

    let any = KeyExpr::new("robot/**").unwrap();
    println!("robot/** includes robot/sensor/* ? {}", any.includes(&pattern)); // true

    // —— 模块 4.2：规范化 ——
    let messy = KeyExpr::autocanonize(String::from("robot/**/**/sensor/*")).unwrap();
    let tidy = KeyExpr::new("robot/**/sensor/*").unwrap();
    println!("autocanonize = {messy}");
    assert_eq!(messy.as_str(), tidy.as_str()); // 规范化后与规范形相等
    println!("canon check passed");
}
```

**预期输出**：

```text
pattern = robot/sensor/*, concrete = robot/sensor/temp
includes   ? true
intersects ? true
robot/** includes robot/sensor/* ? true
autocanonize = robot/**/sensor/*
canon check passed
```

> 想再深入一点？把 `any` 改成 `robot/sensor/**`（即 `pattern` 的「父集合」另一写法），重新验证它 `includes` `concrete`；再用 `relation_to`（需启用 `unstable` feature）对比四种关系枚举。运行结果待本地验证。

## 6. 本讲小结

- Key Expression 是 Zenoh 的地址空间，表示「一批 key 的集合」，匹配完全由集合关系决定。
- 三种存储形式各有定位：`keyexpr`≈`str`（借用，无开销）、`OwnedKeyExpr`≈`Arc<str>`（拥有，可廉价克隆）、`KeyExpr`≈`Cow<str>`（公开 API，可携带 Session 声明优化）；三者都 `Deref` 到 `keyexpr`。
- 语法上以 `/` 分隔非空 chunk，禁用 `#`、`?`、空 chunk、首尾斜杠；通配符 `*` 匹配单层、`**` 匹配任意多层。
- 规范化（canonization）是强约束，保证「字符串相等 ⟺ 集合相等」；用 `new` 必须传规范字符串，用 `autocanonize` 可自动改写。
- `intersects` 判断是否有交集，`includes` 判断是否为子集；二者分别由 `ClassicIntersector` 与 `LTRIncluder` 实现，定义在 `keyexpr` 上。
- 安全构造走 `KeyExpr::new`/`autocanonize`；`from_str_unchecked` 系列是 `unsafe`，仅限能自行保证不变量的场景。

## 7. 下一步学习建议

- **下一讲《u2-l3 配置系统与 WhatAmI 三种角色》**：本讲只讲了「地址怎么写」，下一讲回到 `Config`，看 Zenoh 节点的三种角色（router/peer/client）如何通过配置决定，以及 `DEFAULT_CONFIG.json5` 的结构。
- **横向衔接《u3-1 Pub/Sub 基础》**：Key Expression 是 Pub/Sub 匹配的依据，学完 Pub/Sub 后你会真切看到「发布端 key 与订阅端 key 相交即送达」。
- **延伸阅读源码**：想理解匹配算法的实现细节，可精读 [commons/zenoh-keyexpr/src/key_expr/include.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-keyexpr/src/key_expr/include.rs) 与 [intersect/classical.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-keyexpr/src/key_expr/intersect/classical.rs)；想了解「把值绑定到 KE」的专用数据结构，可看 `keyexpr_tree`（`unstable` feature）。
