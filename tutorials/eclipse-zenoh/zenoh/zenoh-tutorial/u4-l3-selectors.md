# Selector 与查询参数

## 1. 本讲目标

本讲承接《u4-l1 Queryable 与 Query》《u4-l2 Get 与 Querier》，专门讲清楚「一条查询到底带了多少附加信息」。学完后你应当能够：

- 看懂 Selector 的 URL 式写法 `key_expr?name=value;...`，并能手写、手解析一条 Selector 字符串。
- 用 `Parameters` 的 `get / values / iter / insert` 读写查询参数，并理解它「字符串之上的零拷贝视图」本质。
- 区分两类参数：给 Queryable 用的**自定义参数**（如 `day=...;limit=...`），以及由 Zenoh 库自身处理的**标准化参数**（`_time`、`_anyke`）。
- 在 Queryable 端用 `query.parameters()` 取出参数并据此过滤/计算返回值。

## 2. 前置知识

在进入本讲前，请确认你已掌握以下概念（均来自前置讲义）：

- **Key Expression（KE）**：Zenoh 的「地址空间」，一条 KE 表示一批 key 的集合，匹配由两端 KE 的相交关系决定（见《u2-l2》）。
- **Query / Reply 链路**：请求方用 `Session::get` 或 `Querier` 发起查询，应答方用 `Queryable` 接收 `Query` 并 `reply`（见《u4-l1》《u4-l2》）。
- **`Cow<'a, T>`（写时复制）**：Rust 标准库类型，要么借用一段已有数据（`Borrowed`），要么拥有自己的一份（`Owned`）。本讲里 `Selector` 和 `Parameters` 都基于它实现「能借用就借用、需要改时再分配」的零拷贝语义。
- **builder 模式**：`get(...)` 返回 builder，需 `.await`/`.wait()` 才真正 resolve（见《u2-l1》）。

一句话定位：**Selector = Key Expression + Parameters**。Key Expression 回答「查哪些 key」，Parameters 回答「按什么条件查」。本讲只讲后半部分。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [zenoh/src/api/selector.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/selector.rs) | 定义公开类型 `Selector`，把 KeyExpr 与 Parameters 拼成「URL」，并提供字符串解析与标准化参数（`_time`/`_anyke`）的读写 trait。 |
| [commons/zenoh-protocol/src/core/parameters.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/parameters.rs) | 定义 `Parameters` 类型与一整套对 `a=b;c=d|e` 格式字符串的零拷贝解析函数（`iter`/`get`/`values`/`insert`...）。是 Selector 的「参数半边」的全部实现。 |
| [examples/examples/z_get.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_get.rs) | 官方查询示例，展示如何把一个 Selector 字符串经 clap 解析后交给 `session.get`。 |
| [examples/examples/z_queryable.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_queryable.rs) | 官方应答示例，展示在 Queryable 端用 `query.selector()` 打印收到的查询。本讲的实践会改写它来解析参数。 |
| [zenoh/src/api/queryable.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/queryable.rs) | `Query` 类型的访问器 `selector()` / `parameters()` / `key_expr()`，是 Queryable 端取参数的入口。 |

---

## 4. 核心概念与源码讲解

### 4.1 Selector：把 Key Expression 和参数拼成「URL」

#### 4.1.1 概念说明

到目前为止，我们发起查询时用的都是「纯 key expression」，例如 `session.get("demo/example/**")`。但现实中的查询往往还带条件：「查 2024-01-01 这一天、最多 10 条温度记录」。这些条件不属于 key expression（key 是「在哪里」，条件是「按什么筛」），于是 Zenoh 把它们挂在 key expression 后面，形成一条形似 URL 的字符串：

```
demo/weather/history?day=2024-01-01;limit=10
└──── key expr ────┘ └──────── 参数 ────────┘
```

`?` 之前是合法的 Key Expression，`?` 之后是 Parameters。这就是 **Selector**——**选择器**：它完整描述了「这次操作关心哪些 key、以及在这些 key 上附加什么条件」。

Selector 的用途不止过滤，官方文档列出了四种（[selector.rs:27-33](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/selector.rs#L27-L33)）：

1. 向 Queryable 传 RPC 参数（把查询当成一次远程过程调用）；
2. 按值过滤；
3. 按元数据过滤（如时间戳）；
4. 在 REST API 里给 Zenoh 本身传参。

#### 4.1.2 核心流程

Selector 的「字符串 ⇄ 结构」双向流转可以这样画：

```
        ┌───────────────  字符串形式  ───────────────┐
        │   "a/b?x=1;y=2"                            │
        ▼                                            ▼
   TryFrom / FromStr  ───────►   Selector {         }  ──────► Display / fmt
   （按第一个 '?' 切两段）         key_expr:   KeyExpr,          （拼回 "a/b?x=1;y=2"）
                                  parameters: Parameters,
```

构造一条 Selector 有三种姿势（[selector.rs:86-111](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/selector.rs#L86-L111)）：

- `Selector::owned(ke, params)`：拥有 key 和参数的所有权。
- `Selector::borrowed(&ke, &params)`：只借用，适合「打印一对 key+参数」而不想搬动数据。
- 从字符串解析：`s.try_into()` 或 `s.parse()`。

最常用的是**从字符串解析**——这也是 `z_get` 示例经 clap 接收命令行参数的方式。

#### 4.1.3 源码精读

**结构定义**。`Selector` 就是两个字段，都包在 `Cow` 里，因此既能借用又能拥有（[selector.rs:61-67](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/selector.rs#L61-L67)）：

```rust
pub struct Selector<'a> {
    pub(crate) key_expr: Cow<'a, KeyExpr<'a>>,
    pub(crate) parameters: Cow<'a, Parameters<'a>>,
}
```

三个访问器把内部 `Cow` 透明地交出去（[selector.rs:69-83](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/selector.rs#L69-L83)）：`key_expr()` 返回 `&KeyExpr`，`parameters()` 返回 `&Parameters`，`split()` 消耗自己、拆成 `(KeyExpr, Parameters)` 两个所有权值。

**字符串解析**。核心逻辑是「找到第一个 `?`，左边当 key expression，右边当 parameters」（[selector.rs:236-250](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/selector.rs#L236-L250)）：

```rust
impl<'a> TryFrom<&'a str> for Selector<'a> {
    fn try_from(s: &'a str) -> Result<Self, Self::Error> {
        match s.find('?') {
            Some(qmark_position) => {
                let params = &s[qmark_position + 1..];
                Ok(Selector::owned(
                    KeyExpr::try_from(&s[..qmark_position])?,
                    params,
                ))
            }
            None => Ok(KeyExpr::try_from(s)?.into()),
        }
    }
}
```

要点：

- 用 `find('?')` 只切**第一个** `?`；`?` 之后的内容即便再含 `?` 也全部算作 parameters。
- 没有 `?` 时，整串就是一个 key expression，参数为空（走 `KeyExpr::try_from(s)?.into()`，见 [selector.rs:310-317](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/selector.rs#L310-L317) 的 `From<KeyExpr>`，parameters 取空）。
- key expression 部分仍要符合 KE 规范（见《u2-l2》），否则 `KeyExpr::try_from` 报错。

**字符串化（Display）**。把两半拼回去，仅当参数非空时才加 `?`（[selector.rs:206-214](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/selector.rs#L206-L214)）：

```rust
impl std::fmt::Display for Selector<'_> {
    fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        write!(f, "{}", self.key_expr)?;
        if !self.parameters.is_empty() {
            write!(f, "?{}", self.parameters.as_str())?;
        }
        Ok(())
    }
}
```

因此 `Selector` 的「解析 → 打印」是可往返（round-trip）的，前提是你不重复定义同名参数。

> 注意：官方文档明确把「同名参数定义两次」列为**未定义行为**（[selector.rs:42-43](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/selector.rs#L42-L43)）。后文的 `get` 只会返回匹配到的第一个，所以写参数时请保证名字唯一。

**z_get 如何接收 Selector**。示例把 Selector 当成普通命令行参数，clap 会自动调用 `FromStr` 完成解析（[z_get.rs:80-96](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_get.rs#L80-L96)）：

```rust
struct Args {
    #[arg(short, long, default_value = "demo/example/**")]
    selector: Selector<'static>,   // clap 经 FromStr 解析
    ...
}
```

随后直接交给 `session.get(&selector)`（[z_get.rs:34-46](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_get.rs#L34-L46)）。也就是说，你完全可以用 `-s "a/b?x=1"` 把参数直接写在命令行里。

#### 4.1.4 代码实践

**目标**：体会「Selector = key expression + parameters」的切分。

**步骤**（源码阅读型，不需运行）：

1. 打开 [selector.rs:222-256](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/selector.rs#L222-L256)，对比 `TryFrom<String>` 与 `TryFrom<&str>` 两个实现。
2. 用下表手算下列字符串切分后得到的 `key_expr` 与 `parameters`：

   | 输入字符串 | key_expr | parameters |
   | --- | --- | --- |
   | `a/b` | `a/b` | （空） |
   | `a/b?` | `a/b` | （空，`?` 后为空串） |
   | `a/b?x=1` | `a/b` | `x=1` |
   | `a/b?x=1;y=2;z` | `a/b` | `x=1;y=2;z` |

3. 想一想：为什么 `a/b?` 与 `a/b` 解析出的 `parameters` 都判为「空」？（提示：见下文 4.2 的 `is_empty` 与 `iter` 对空片段的过滤。）

**预期结果**：你能口头复述「按第一个 `?` 切两段」这条唯一规则，并解释 `?` 后为空时等价于没有参数。

#### 4.1.5 小练习与答案

**练习 1**：字符串 `a?b?c=1` 会被解析成什么？

**答案**：`find('?')` 命中**第一个** `?`，于是 `key_expr = "a"`，`parameters = "b?c=1"`。注意 `?` 在参数部分没有任何特殊含义，它只是一个普通字符。

**练习 2**：为什么 `Selector` 的两个字段都用 `Cow` 而不是直接用 `KeyExpr` / `Parameters`？

**答案**：为了让「借用」与「拥有」两种用法共用一个类型——从字符串解析时需要拥有（`Cow::Owned`），而从已有 `&KeyExpr`、`&Parameters` 构造时可以零分配借用（`Cow::Borrowed`，见 [selector.rs:98-103](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/selector.rs#L98-L103)）。这避免了打印、转发时无谓的字符串拷贝。

---

### 4.2 Parameters：参数的字符串解析与读写

#### 4.2.1 概念说明

Selector 的「参数半边」全部由 `Parameters` 类型承担。它本质上是一个**架在字符串之上的「类 HashMap」视图**：底层只有一段 `Cow<'s, str>`（[parameters.rs:260](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/parameters.rs#L260)），但提供了 `get` / `iter` / `insert` 等 HashMap 风格的方法，在**不分配哈希表**的前提下读写键值对。

这种设计的动机是性能与零拷贝：网络上传来的参数本来就是一段字符串，与其先解析成 `HashMap<String, String>` 再用，不如直接在原串上切片读取，省去大量小字符串分配。`Parameters` 就是这种「懒解析」的封装。

参数串的格式由三个分隔符定义（[parameters.rs:32-34](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/parameters.rs#L32-L34)）：

| 分隔符 | 常量名 | 作用 |
| --- | --- | --- |
| `;` | `LIST_SEPARATOR` | 分隔不同的键值对 |
| `=` | `FIELD_SEPARATOR` | 分隔一对中的「键」与「值」 |
| `\|` | `VALUE_SEPARATOR` | 分隔同一个键的多个值 |

其文法（grammar）可写成：

```
parameters := "" | pair (";" pair)*
pair       := name ["=" value]      ; 没有 "=" 时，值为空串
value      := token ("|" token)*    ; "|" 把一个值拆成多段
```

#### 4.2.2 核心流程

读取参数的内部管线（以 `x=1;y=2;z` 为例）：

```
"x=1;y=2;z"
   │  split(';')                  → ["x=1", "y=2", "z"]
   │  filter(非空)                → ["x=1", "y=2", "z"]
   │  split_once('=')  逐对       → [("x","1"), ("y","2"), ("z","")]
   ▼
iter() : DoubleEndedIterator<Item = (&str, &str)>
```

关键点：

- `iter` 跳过空片段（`.filter(|p| !p.is_empty())`），所以末尾多余的分号会被忽略。
- 每对用 `split_once('=')` 切：找到**第一个** `=`，左边是键、右边是值；没有 `=` 时值为空串（[parameters.rs:36-44](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/parameters.rs#L36-L44)）。于是 `p1=x=y` 被解析成键 `p1`、值 `x=y`（值里可以含 `=`）。
- 一个键的值若含 `|`，可用 `values()` 拆成多段，例如 `c=3|4|5` → `["3","4","5"]`。

#### 4.2.3 源码精读

**核心函数 `iter`**（[parameters.rs:47-51](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/parameters.rs#L47-L51)）：

```rust
pub fn iter(s: &str) -> impl DoubleEndedIterator<Item = (&str, &str)> + Clone {
    s.split(LIST_SEPARATOR)
        .filter(|p| !p.is_empty())
        .map(|p| split_once(p, FIELD_SEPARATOR))
}
```

它返回的是一个**双向迭代器**（`DoubleEndedIterator`），意味着你可以 `.next_back()` 从尾部取，也能 `.rev()` 反转。所有切片都是对原串 `s` 的借用——零分配。

**取单个值 `get`**（[parameters.rs:96-98](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/parameters.rs#L96-L98)）就是在 `iter` 上找第一个匹配键：

```rust
pub fn get<'s>(s: &'s str, k: &str) -> Option<&'s str> {
    iter(s).find(|(key, _)| *key == k).map(|(_, value)| value)
}
```

**多值 `values`**（[parameters.rs:101-112](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/parameters.rs#L101-L112)）：先 `get` 取到值的整段，再用 `|` 切开。键不存在时返回一个空的同类型迭代器。

**`Parameters` 类型的方法**把这些自由函数包成方法（[parameters.rs:262-360](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/parameters.rs#L262-L360)），其中最常用的几个：

```rust
pub fn get<K>(&'s self, k: K) -> Option<&'s str>            // 取值
pub fn values<K>(&'s self, k: K) -> impl ...<Item=&'s str>  // 取多值（按 | 拆）
pub fn iter(&'s self) -> impl ...<Item=(&'s str,&'s str)>   // 遍历全部键值对
pub fn contains_key<K>(&self, k: K) -> bool                 // 是否存在键
pub fn insert<K,V>(&mut self, k: K, v: V) -> Option<String> // 插入/覆盖，返回旧值
pub fn remove<K>(&mut self, k: K) -> Option<String>         // 删除键
```

注意 `insert` / `remove` 会触发 `Cow::Owned` 的分配——只要发生**写**操作，就必然把底层字符串物化成 owned（[parameters.rs:310-319](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/parameters.rs#L310-L319)）。纯读则始终借用。

**末尾分隔符的容错**。从字符串构造 `Parameters` 时会先 `trim_end_matches` 掉末尾的 `;`、`=`、`|`（[parameters.rs:362-369](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/parameters.rs#L362-L369)），所以 `"p1=v1;p2=v2;"` 与 `"p1=v1;p2=v2"` 等价。源码自带的文档示例完整展示了 `get`/`values`/`iter`/`from_iter` 的用法（[parameters.rs:232-258](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/parameters.rs#L232-L258)），值得逐行读一遍。

**与 HashMap 互转**。`Parameters` 还实现了 `From<HashMap>` 与 `Into<HashMap>`（[parameters.rs:434-471](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/parameters.rs#L434-L471)），当你确实需要 O(1) 随机查询或去重时，可以一次性转成 `HashMap<String, String>` 再用。

#### 4.2.4 代码实践

**目标**：用单元测试断言验证你对参数解析规则的理解。这是「阅读测试理解行为」型实践。

**步骤**：

1. 打开 [parameters.rs:485-544](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/parameters.rs#L485-L544) 的 `test_parameters`，逐条核对下面的断言（摘自该测试）：

   ```rust
   // 没有 "=" → 值为空串
   Parameters::from("p1")  == Parameters::from(&[("p1", "")][..]);
   // 第一个 "=" 才算分隔符，值里可以再有 "="
   Parameters::from("p1=x=y;p2=a==b")
       == Parameters::from(&[("p1","x=y"),("p2","a==b")][..]);
   // 末尾多余分隔符被忽略
   Parameters::from("p1=v1;p2=v2;|=")
       == Parameters::from(&[("p1","v1"),("p2","v2")][..]);
   ```

2. 运行这条测试（**待本地验证**）：

   ```bash
   cargo test -p zenoh-protocol --lib core::parameters::tests::test_parameters
   ```

**需要观察的现象**：测试应当通过；若你改动某条断言（比如把 `"p1=x=y"` 的期望值写成 `("p1","x")`），测试应当失败——这正好印证「值保留第一个 `=` 之后的所有字符」。

**预期结果**：你能不假思索地回答「`a;b=c;` 解析出几对、各是什么」（答：两对：`("a","")`、`("b","c")`）。

#### 4.2.5 小练习与答案

**练习 1**：给定 `unit=celsius|fahrenheit;day=2024-01-01`，分别用 `get("unit")`、`values("unit")`、`get("day")` 得到什么？

**答案**：

- `get("unit")` → `Some("celsius|fahrenheit")`（整段值，含 `|`）。
- `values("unit").collect::<Vec<_>>()` → `["celsius", "fahrenheit"]`（按 `|` 拆开）。
- `get("day")` → `Some("2024-01-01")`。

**练习 2**：为什么说 `Parameters` 是「懒解析」？写操作（`insert`）会带来什么副作用？

**答案**：因为读操作（`get`/`iter`/`values`）从不预先构造哈希表，而是每次在原串上用 `split` + `find` 现场切片，返回对原串的借用。只有写操作（`insert`/`remove`/`extend`）才会把 `Cow` 物化成 `Owned` 并重新拼接出一段新字符串（见 [parameters.rs:310-319](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/parameters.rs#L310-L319)），这就是「读零拷贝、写才分配」的来源。

---

### 4.3 查询参数：标准化的 `_time` / `_anyke` 与自定义参数

#### 4.3.1 概念说明

参数有两类主人：

1. **给 Queryable 用的自定义参数**：由你（应用开发者）定义语义，例如 `day=...;limit=...`。Zenoh 只负责把它原样搬运到对端，怎么用完全由 Queryable 决定。本讲的实践任务就属此类。
2. **给 Zenoh 库自身用的标准化参数**：Zenoh 预留了一批「以下划线开头」的名字，由库自己解释，用来控制查询行为本身，而不是传给 Queryable 的业务逻辑。

为了避免两类参数撞名，官方约定：**以非字母数字字符（如下划线）开头的参数名保留给 Zenoh**；自定义参数应使用普通字母数字开头，最好加前缀以防与其他 Queryable 冲突（[selector.rs:45-50](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/selector.rs#L45-L50)）。

目前标准化的两个参数（[selector.rs:52-60](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/selector.rs#L52-L60)）：

| 参数名 | 含义 | 稳定性 |
| --- | --- | --- |
| `_time` | 只关心落在某**时间范围**内的值，值需符合 Zenoh Time DSL | `[unstable]` |
| `_anyke` | 放宽「应答 key 必须与查询 key 相交」的限制，允许任意 key 的应答 | 稳定（但由 `accept_replies` 设置） |

#### 4.3.2 核心流程

两类参数在源码里的分工：

```
                          Selector.parameters (一段字符串)
                                    │
            ┌───────────────────────┴────────────────────────┐
            ▼                                                ▼
   ZenohParameters trait                           自定义参数
   （库自身解释）                                  （Queryable 业务解释）
   ─ set/get _time      → TimeRange               ─ query.parameters().get("day")
   ─ set/has _anyke     → ReplyKeyExpr::Any       ─ query.parameters().iter()
```

`_time` 和 `_anyke` 不需要你手写字符串去拼，Zenoh 提供了 `ZenohParameters` trait 的方法来安全地读写它们；自定义参数则直接用 4.2 学到的 `Parameters::get` / `iter`。

#### 4.3.3 源码精读

**常量定义**。两个标准化参数名都是 `pub(crate)` 常量（[selector.rs:139-141](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/selector.rs#L139-L141)）：

```rust
pub(crate) const REPLY_KEY_EXPR_ANY_SEL_PARAM: &str = "_anyke";
#[zenoh_macros::unstable]
pub(crate) const TIME_RANGE_KEY: &str = "_time";
```

注意 `_anyke` 没有标 unstable，而 `_time` 与整个 `ZenohParameters` trait 都被 `#[zenoh_macros::unstable]` 门控——这正对应了「`_time` 的 Time DSL 仍是 unstable」这一事实。

**`ZenohParameters` trait**（[selector.rs:143-167](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/selector.rs#L143-L167)）给 `Parameters` 提供了四个方法：

```rust
pub trait ZenohParameters {
    fn set_time_range<T: Into<Option<TimeRange>>>(&mut self, time_range: T);
    fn set_reply_key_expr_any(&mut self);
    fn time_range(&self) -> Option<ZResult<TimeRange>>;
    fn reply_key_expr_any(&self) -> bool;
}
```

其实现就是把键值对塞进/读出 Parameters（[selector.rs:169-198](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/selector.rs#L169-L198)）。例如 `set_reply_key_expr_any` 就是 `insert("_anyke", "")`：

```rust
fn set_reply_key_expr_any(&mut self) {
    self.insert(REPLY_KEY_EXPR_ANY_SEL_PARAM, "");
}
```

`_anyke` 解决的是《u4-l1》提到的 `ReplyKeyExpr` 问题：默认情况下应答的 key 必须与查询 key 相交，否则发送端报错；带上 `_anyke`（等价于 builder 上调用 `accept_replies(ReplyKeyExpr::Any)`）就放开了这条限制。这一对应关系在 [query.rs:230-257](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/query.rs#L230-L257) 的 `ReplyKeyExpr` 文档里有完整说明。

**Queryable 端如何取参数**。这才是自定义参数真正被「消费」的地方。`Query` 提供了三个访问器（[queryable.rs:193-233](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/queryable.rs#L193-L233)）：

```rust
pub fn selector(&self) -> Selector<'_>           // key + 参数，借用拼成
pub fn key_expr(&self) -> &KeyExpr<'static>      // 只取 key 半边
pub fn parameters(&self) -> &Parameters<'static> // 只取参数半边 ← 本讲主角
```

`selector()` 内部就是用 4.1 里见过的 `Selector::borrowed` 把 key 和参数零拷贝拼起来（[queryable.rs:193-195](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/queryable.rs#L193-L195)）。所以在 Queryable 里读自定义参数，只要：

```rust
let day = query.parameters().get("day");
```

**公开导出**。`Selector`、`Parameters` 都在 `zenoh::query` 模块下重新导出，方便你 `use zenoh::query::{Selector, Parameters}`（[lib.rs:645-666](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L645-L666)）；`ZenohParameters` trait 则需 unstable feature。

#### 4.3.4 代码实践（本讲的主实践）

**目标**：发起一个带 `day=2024-01-01;limit=10` 的 get 请求，Queryable 端解析全部参数并据此过滤「数据库」，最后把解析到的参数全部打印出来。

> 以下为**示例代码**（基于 [z_queryable.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_queryable.rs) 改写），不是仓库原有文件。

**步骤 1：写一个会解析参数的 Queryable**（存为 `examples/examples/z_queryable_weather.rs`）：

```rust
// 示例代码：基于 examples/examples/z_queryable.rs 改写
use zenoh::Config;

#[tokio::main]
async fn main() {
    zenoh::init_log_from_env_or("error");
    let session = zenoh::open(Config::default()).await.unwrap();

    let key = "demo/weather/history";
    let queryable = session.declare_queryable(key).await.unwrap();

    // 模拟一个微型「时序数据库」
    let records = vec![
        ("2024-01-01", 12.5),
        ("2024-01-01", 13.0),
        ("2024-01-01", 12.8),
        ("2024-01-02", 15.1),
    ];

    while let Ok(query) = queryable.recv_async().await {
        let params = query.parameters();

        // (1) 把解析到的全部参数打印出来
        println!(">> 收到参数：");
        for (k, v) in params.iter() {
            println!("     {k} = {v:?}");
        }

        // (2) 读取自定义参数 day / limit
        let day = params.get("day").unwrap_or_default().to_string();
        let limit: usize = params.get("limit")
            .and_then(|s| s.parse().ok())
            .unwrap_or(usize::MAX);

        // (3) 据此过滤并返回
        let picked: Vec<_> = records.iter()
            .filter(|(d, _)| d == &day)
            .take(limit)
            .collect();
        let body = if picked.is_empty() {
            format!("没有 {day} 的记录")
        } else {
            format!("{picked:?}")
        };
        query.reply(key, body).await.unwrap();
    }
}
```

**步骤 2：用 `z_get` 发起带参数的查询**。`z_get` 的 `--selector`/`-s` 接受完整 Selector 字符串（[z_get.rs:80-96](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_get.rs#L80-L96)）：

```bash
# 终端 1：启动 Queryable（需先把上面的示例加入 examples/Cargo.toml 的 [[example]]）
cargo run --example z_queryable_weather

# 终端 2：发起查询。注意整个 selector 必须加引号，否则 shell 会把 ';' 当命令分隔符
cargo run --example z_get -- -s "demo/weather/history?day=2024-01-01;limit=10"
```

**需要观察的现象**：

- 终端 1（Queryable）应打印：
  ```
  >> 收到参数：
       day = "2024-01-01"
       limit = "10"
  ```
  这说明 `;` 把参数串切成了 `day=2024-01-01` 与 `limit=10` 两对。
- 终端 2（get）应收到 3 条 2024-01-01 的记录（`limit=10` 放宽，但当天只有 3 条）。
- 把查询改成 `?day=2024-01-01;limit=2`，应只收到 2 条（`take(2)` 生效）。
- 把查询改成 `?day=2024-01-03`，应收到错误体「没有 2024-01-03 的记录」。

**预期结果 / 待本地验证**：以上为根据源码逻辑推导的行为；两端能否互通取决于网络发现（同机默认 peer + multicast scouting 通常可达，见《u2-l1》）。若不通，可在两端加 `-e tcp/127.0.0.1:7447` 显式连到同一个 zenohd。具体运行输出**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么不建议把自己的参数命名为 `_time` 或 `_limit`？

**答案**：因为以下划线开头的参数名被 Zenoh 预留给库自身使用（[selector.rs:45-50](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/selector.rs#L45-L50)）。`_time` 已经是标准化时间范围参数；`_limit` 虽暂未使用，但未来可能被占用，从而与你的业务逻辑冲突。自定义参数应使用字母数字开头，最好加业务前缀，如 `app_limit`。

**练习 2**：想在 get 时同时按时间范围过滤，应该用哪个标准化参数？需要什么 feature？

**答案**：用 `_time` 参数，其值是 Zenoh Time DSL 描述的时间范围（如 `[now(-10m)..now()]`）。它由 `ZenohParameters::set_time_range` / `time_range` 读写，该 trait 受 `#[zenoh_macros::unstable]` 门控（[selector.rs:140-145](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/selector.rs#L140-L145)），因此需要开启 `unstable` feature。后续的 `_time` 行为细节会在《u6-l4 Timestamp》中展开。

---

## 5. 综合实践

把本讲三块内容串起来，完成一个「带条件的历史查询」端到端小任务：

1. **数据准备**：写一个 Queryable，内部维护一个 `Vec<(String day, String city, f64 temp)>`，声明在 `weather/**` 上。
2. **多参数解析**：让它支持三个自定义参数——`day`（精确匹配日期）、`city`（精确匹配城市）、`unit`（值为 `celsius|fahrenheit`，多值）。收到查询时：
   - 用 `query.parameters().iter()` 打印全部参数；
   - 用 `get("day")`、`get("city")` 过滤；
   - 用 `values("unit")` 判断是否要把摄氏温度换算成华氏并返回。
3. **查询端**：用 `z_get` 发送 `weather/history?day=2024-01-01;city=beijing;unit=celsius|fahrenheit`，验证 Queryable 端打印的参数与你预期一致，且返回值做了单位换算。
4. **对比实验**：再发一次 `weather/history?day=2024-01-01`（不带 city/unit），观察「缺省参数」时你的 Queryable 如何处理（提示：`get` 返回 `None`，应给出合理默认，而不是崩溃）。

这个任务覆盖了本讲的全部要点：手写 Selector 字符串、用 `iter` 全量打印、用 `get` 取单值、用 `values` 取多值、处理参数缺失，以及理解「自定义参数 vs 标准化参数」的边界。

---

## 6. 本讲小结

- **Selector = Key Expression + Parameters**，字符串形式形如 `key_expr?name=value;...`，按**第一个** `?` 切两段；没有 `?` 时参数为空。
- **Parameters** 是架在一段字符串上的「类 HashMap 零拷贝视图」：三个分隔符 `;` `=` `|` 分别切「对」「键值」「多值」；读操作（`get`/`iter`/`values`）全部借用原串、零分配，写操作（`insert`/`remove`）才会物化。
- 解析规则的关键细节：每对按**第一个** `=` 切（值里可含 `=`），无 `=` 则值为空串，空片段与末尾多余分隔符被忽略。
- 参数分两类：**自定义参数**（给 Queryable 业务用，建议字母数字开头并加前缀）与**标准化参数**（以下划线开头、由 Zenoh 库自身解释，目前有 `_time` [unstable] 与 `_anyke`）。
- Queryable 端通过 `query.parameters()` 拿到 `&Parameters`，再用 `get` / `iter` / `values` 消费——这是查询条件真正「落地」的地方。

## 7. 下一步学习建议

- **时间维度**：本讲的 `_time` 参数只是点到为止。它依赖的 Timestamp 与 HLC（混合逻辑时钟）是《u6-l4 Timestamp：时间戳与 HLC》的主题，建议接着读，理解时间范围如何在存储/查询合并去重中发挥作用。
- **应答 key 限制**：`_anyke` 对应的 `ReplyKeyExpr` 与查询合并（Consolidation）在《u4-l1》《u4-l2》已有铺垫，可回头对照 `QueryTarget` / `QueryConsolidation` 一起理解「查询问谁、怎么合并」。
- **进到内核**：如果你想看 Parameters 在网络上是如何被编码传输的（`WireExpr` 携带参数），可以进入专家层的《u10-l1 协议消息模型》与《u10-l2 Zenoh080 线编码》，那会解释这段字符串如何变成字节流。
