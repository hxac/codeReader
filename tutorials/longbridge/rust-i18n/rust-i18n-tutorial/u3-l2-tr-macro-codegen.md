# _tr! 宏的解析与代码生成

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 `_tr!` 这个过程宏如何用 `syn` 把一串 token 解析成内存中的 `Tr` 结构。
2. 看懂 `Argument` 如何统一解析 `key = value`、`key => value`、`key = value : {:spec}` 三种写法，以及名字既可以是标识符也可以是字符串字面量。
3. 解释 `filter_arguments` 如何把 `locale`、`_minify_key*` 这些「系统参数」从普通插值参数里剥离出来，保证它们**不会**被当成 `%{name}` 占位符去替换。
4. 掌握 `into_token_stream` 在「无参数」「有参数」两种情况下分别生成的两条不同代码路径，并能手写出展开骨架。

本讲是「运行时翻译机制」单元的第二讲，承接上一讲 **u3-l1（`t!` 宏的完整调用链）**。u3-l1 讲了 `_tr!` 在三跳调用链里的**位置**（第三跳），并点到了「`into_token_stream` 按 minify/有无参数分多条分支生成查找代码」和「`filter_arguments` 剥离系统参数」。本讲则钻进 `_tr!` 的**内部**，把 `Tr`/`Argument`/`Value`/`filter_arguments`/`into_token_stream` 五个最小模块逐一落到源码上。回退链（territory 回退、`_rust_i18n_lookup_fallback`）留待 **u3-l4**，占位符替换的字节级状态机留待 **u3-l3**。

## 2. 前置知识

### 2.1 过程宏的「解析 → 持有 → 生成」三段式

一个 `#[proc_macro]` 函数的典型结构是三段（回顾 u2-l1、u3-l1）：

1. **解析（Parse）**：把 `proc_macro::TokenStream` 喂给 `parse_macro_input!(input as T)`，触发 `T` 的 `syn::parse::Parse` 实现，得到一个内存中的结构体 `T`。
2. **持有（Hold）**：结构体 `T` 保存这次宏调用的「内存模型」——有哪些字段、各是什么值。
3. **生成（Codegen）**：把 `T` 转回 `TokenStream`，通常通过一个 `into_token_stream(self)` 方法，用 `quote!` 拼出等价 Rust 代码。

`_tr!` 正是这个三段式的教科书例子：`Tr` 是「持有」的结构，`Tr::parse` 是「解析」，`Tr::into_token_stream` 是「生成」。本讲按这个顺序展开。

### 2.2 `ParseStream` 的 fork / advance_to 探测手法

`syn` 的解析是「试探式」的。`ParseStream` 有一个 `fork()` 方法，它会**复制一个游标**；你可以先在 fork 上尝试解析，**失败就丢弃 fork**、输入游标不动；**成功就调用 `advance_to(&fork)`** 把主游标推进到 fork 的位置。这让我们能写出「先试 A，不行再试 B」的回退逻辑而不污染原输入。`Value::parse`、`Messsage::parse`、`Argument::try_ident` 都用了这个手法。

> 术语提示：`tt`（token tree）、过程宏、`ParseStream` 这些在 u3-l1 已建立，本讲直接使用。

## 3. 本讲源码地图

本讲几乎全部内容都集中在一个文件里：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [crates/macro/src/tr.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs) | `_tr!` 的解析（Parse）与代码生成（Codegen） | `Value`、`Argument`、`Messsage`、`Tr`、`filter_arguments`、`into_token_stream` 全部在这里 |
| [crates/macro/src/lib.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs) | 过程宏 crate 入口 | 仅引用 `_tr` 入口 `parse_macro_input!(input as tr::Tr)`（u3-l1 已讲） |
| [tests/integration_tests.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/integration_tests.rs) | 集成测试 | 提供 `=`、`=>`、`locale=`、`name=>` 等真实调用样例 |

一句话记住 `tr.rs` 的分工：**`Value` 是「值」的叶子，`Argument` 是「键值对」，`Messsage` 是「首个参数」，`Tr` 是把三者打包并负责剥离系统参数和生成代码的总容器**。

## 4. 核心概念与源码讲解

下面按「自底向上的数据类型 → 自顶向下的控制流」顺序，拆成五个最小模块：

1. **4.1 Value**：一个参数值的统一表示（表达式 / 标识符 / 空）。
2. **4.2 Argument**：解析 `key = value` / `key => value` / `key = value : {:spec}` 三种形态。
3. **4.3 Tr 容器与 Parse 入口**：把 `Messsage + Arguments` 打包成 `Tr`。
4. **4.4 filter_arguments**：剥离 `locale` / `_minify_key*` 系统参数。
5. **4.5 into_token_stream**：无参数 vs 有参数两条代码路径。

### 4.1 Value：参数值的统一表示

#### 4.1.1 概念说明

`t!` 调用里，参数的「值」来源很杂：可能是字符串字面量 `"Jason"`、数字 `123`、表达式 `1 + 2`、宏调用 `format!("hi")`、也可能是标识符（变量名）`name`。`Value` 就是为这些异质值定义的**统一枚举**，是整个 `tr.rs` 里最底层的积木。

注意：`Value` 描述的是「参数的值」，**不是**消息 key 本身。消息 key 由 `Messsage` 持有（4.3 节）。

#### 4.1.2 核心流程

`Value` 有三个变体，配合一组判断/转换方法：

```
Value
├── Empty        ← 默认占位，实际解析中很少出现
├── Expr(Expr)   ← 表达式：字面量、数字、1+2、format!(..)、元组 ……
└── Ident(Ident) ← 标识符：裸变量名

辅助方法：
├── is_expr_lit_str()       → 是否是「字符串字面量」（minify_key 的字面量分支要看它）
├── is_expr_tuple()         → 是否是二元元组（minify_key 的元组分支要看它）
├── to_string()             → 若是字符串字面量，返回其字符串值；否则 None
└── to_tupled_token_streams()→ 若是二元元组，拆出 (first, last) 两个 token 流
```

解析时 `Value::parse` 用「fork 探测」先试表达式、再试标识符：

```
1. fork → 尝试解析 Expr；成功则 advance_to，返回 Value::Expr
2. 否则 fork → 尝试解析 Ident；成功则 advance_to，返回 Value::Ident
3. 都失败 → 报错 "Expected a expression or an identifier"
```

#### 4.1.3 源码精读

`Value` 枚举定义在 [crates/macro/src/tr.rs:L7-L13](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L7-L13)，三个变体中 `Empty` 标了 `#[default]`：

```rust
#[derive(Clone, Debug, Default)]
pub enum Value {
    #[default]
    Empty,
    Expr(Expr),
    Ident(Ident),
}
```

判断「是不是字符串字面量」的 `is_expr_lit_str` 在 [crates/macro/src/tr.rs:L16-L23](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L16-L23)——它做两层模式匹配：先确认是 `Expr::Lit`，再确认字面量种类是 `Lit::Str`。`into_token_stream` 的 minify_key 字面量分支（4.5 节）就靠它判断能否在编译期算短键。

`Value::parse` 的 fork 探测在 [crates/macro/src/tr.rs:L83-L97](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L83-L97)：

```rust
impl syn::parse::Parse for Value {
    fn parse(input: syn::parse::ParseStream) -> syn::parse::Result<Self> {
        let fork = input.fork();
        if let Ok(expr) = fork.parse::<Expr>() {
            input.advance_to(&fork);
            return Ok(expr.into());
        }
        let fork = input.fork();
        if let Ok(expr) = fork.parse::<Ident>() {
            input.advance_to(&fork);
            return Ok(expr.into());
        }
        Err(input.error("Expected a expression or an identifier"))
    }
}
```

> 为什么先试 `Expr` 再试 `Ident`？因为 `Expr` 的解析范围更广，会把裸标识符也吃掉。先试更「贪心」的类型，能优先把 `format!("hi")`、`1 + 2` 这类识别成表达式。`Value` 还实现了 `ToTokens`（[tr.rs:L70-L81](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L70-L81)），其中对 `Expr::Path` 会自动加 `&` 取引用，这是后面 `format!(..., value)` 能拿到 `&str` 的小技巧。

#### 4.1.4 代码实践

**实践目标**：对照两种调用，判断 `msg` 的 `Value` 变体与 `to_string()` 返回值。

**操作步骤**：阅读 [crates/macro/src/tr.rs:L32-L39](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L32-L39) 的 `to_string`，回答下表（示例代码）：

| 调用 | `Value` 变体 | `to_string()` 返回 |
| --- | --- | --- |
| `t!("hello")` | `Expr(Expr::Lit(Lit::Str))` | `Some("hello")` |
| `t!(format!("hi"))` | `Expr(Expr::Call)` | `None`（非字符串字面量） |
| `t!(some_var)`（`some_var` 是 `String`） | `Ident` 或 `Expr(Expr::Path)` | `None` |

**需要观察的现象**：只有「字符串字面量」才会让 `to_string()` 返回 `Some`。

**预期结果**：这解释了为什么 minify_key 的「编译期算短键」分支只能对**字面量**生效（4.5 节）——动态变量/表达式拿不到字符串值，只能在运行时哈希。

> 待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`Value::Empty` 在什么场景下会出现？

**参考答案**：`Empty` 是 `#[default]` 占位，出现在用 `..Self::new()` 或 `Default::default()` 构造但尚未真正解析填充字段时（如 `Messsage::default()`）。正常解析路径里 `Messsage::parse` 一定会填上 `Expr` 或 `Ident`，所以 `Empty` 几乎只在初始化瞬间存在。

**练习 2**：`to_tupled_token_streams`（[tr.rs:L41-L55](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L41-L55)）要求元组有几个元素？

**参考答案**：恰好 2 个（`expr_tuple.elems.len() == 2`），拆出 `(first, last)`。这是 minify_key 的「`(key, msg)` 元组」写法专用，详细算法在 u6 系列。

### 4.2 Argument：解析三种键值对形态

#### 4.2.1 概念说明

`Argument` 是「一个命名参数」的内存模型，对应 `t!("...", name = "Jason")` 里 `name = "Jason"` 这样的一对。它要兼容三种用户写法：

1. **`key = value`**：最常见的等号赋值，如 `name = "Jason"`。
2. **`key => value`**：箭头写法，如 `name => "Jason"`，语义与 `=` 完全等价（只是书写风格，源于 ruby-i18n 习惯）。
3. **`key = value : {:spec}`**：在值后追加 `:` 与一个**带花括号的格式说明符**，如 `sn = 123 : {:08}`，让该值按说明符格式化（零填充等）。

此外，`key` 既可以是标识符 `name`，也可以是字符串字面量 `"name"`（便于动态/含点的键名）。

#### 4.2.2 核心流程

`Argument::parse` 按固定顺序消费四段：

```
1. 跳过前导逗号（容错：允许 name = "Jason",,  这种多逗号写法）
2. 解析名字：先试 Ident，失败再试 字符串字面量（LitStr）
3. 解析分隔符：先试 `=>`，否则试 `=`，都没有就报错
4. 解析值：input.parse::<Value>()
5.（可选）若遇到 `:` 且其后跟 `{...}`，把花括号内的 token 拼成 specifiers 字符串
```

关键点：specifiers 只把**花括号内部**的内容（如 `:08` 里的 `:08`）捕获下来，生成阶段再把它包回 `{:08}`。这与 `format!` 的格式串语法对齐。

#### 4.2.3 源码精读

`Argument` 结构在 [crates/macro/src/tr.rs:L99-L104](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L99-L104)，三个字段——名字、值、可选的格式说明符：

```rust
#[derive(Clone, Default)]
pub struct Argument {
    pub name: String,
    pub value: Value,
    pub specifiers: Option<String>,
}
```

核心解析逻辑在 [crates/macro/src/tr.rs:L133-L176](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L133-L176)。先看名字与分隔符两段：

```rust
// 跳过前导逗号
while input.peek(Token![,]) { let _ = input.parse::<Token![,]>()?; }
// 名字：标识符 或 字符串字面量
let name = Self::try_ident(input)
    .or_else(|_| Self::try_literal(input))
    .map_err(|_| input.error("Expected a `string` literal or an identifier"))?;
// 分隔符：=> 或 =
if input.peek(Token![=>]) {
    let _ = input.parse::<Token![=>]()?;
} else if input.peek(Token![=]) {
    let _ = input.parse::<Token![=]>()?;
} else {
    return Err(input.error("Expected `=>` or `=`"));
}
let value = input.parse()?;   // 解析 Value
```

注意 `try_ident` / `try_literal`（[tr.rs:L118-L130](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L118-L130)）也用了 fork 探测：先试标识符，失败回退试字符串字面量。这就是 `name => "Jason"` 和 `"name" => "Jason"` 都合法的原因。

specifiers 的解析在 [crates/macro/src/tr.rs:L153-L169](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L153-L169)：

```rust
let specifiers = if input.peek(Token![:]) {
    let _ = input.parse::<Token![:]>()?;
    if input.peek(Brace) {
        let content;
        let _ = syn::braced!(content in input);
        let mut specifiers = String::new();
        while let Ok(s) = content.parse::<proc_macro2::TokenTree>() {
            specifiers.push_str(&s.to_string());
        }
        Some(specifiers)
    } else { None }
} else { None };
```

对 `sn = 123 : {:08}`，`syn::braced!` 捕获外层花括号的**内部**，即 `:08`（冒号也是内容的一部分），所以 `specifiers = Some(":08")`。生成阶段会把它包成 `{:08}`（见 4.5 节）。

> 真实用例佐证：[tests/i18n_minify_key.rs:L40](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/i18n_minify_key.rs#L40) 的 `t!(msg, name => & i : {} )` 同时演示了 `=>` 分隔符和 `{}`（空内容）说明符——此时 `specifiers = Some("")`。

#### 4.2.4 代码实践

**实践目标**：对三种写法，分别说出 `Argument` 的 `name`、分隔符、`specifiers` 字段值。

**操作步骤**：手算下表（示例代码，假设已解析完成）：

| 用户写法 | `name` | 分隔符 | `specifiers` |
| --- | --- | --- | --- |
| `name = "Jason"` | `"name"` | `=` | `None` |
| `name => "Jason"` | `"name"` | `=>` | `None` |
| `"name" => "Jason"` | `"name"` | `=>` | `None` |
| `count = 7 : {:05}` | `"count"` | `=` | `Some(":05")` |
| `name => &i : {}` | `"name"` | `=>` | `Some("")` |

**需要观察的现象**：`=>` 与 `=` 在结构体里**没有任何字段记录区别**——解析完之后两者等价，只影响键值对语义是否被当成插值变量。

**预期结果**：specifiers 只在出现 `: {...}` 时为 `Some`，且保存的是花括号**内部**字符串。

> 待本地验证：可在 [tests/integration_tests.rs:L211-L212](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/integration_tests.rs#L211-L212) 看到 `=` 与 `=>`、标识符与字面量混用的真实断言。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `name` 既要支持标识符又要支持字符串字面量？

**参考答案**：标识符方便写 `name = "Jason"`；但有些插值键名含点或特殊字符（如 `"user.name"`），不是合法标识符，这时只能用字符串字面量 `"user.name" => ...`。两种来源都允许，让插值占位符名更灵活。

**练习 2**：如果用户写 `count = 7 : (05)`（冒号后是圆括号而非花括号），`specifiers` 会是什么？

**参考答案**：`None`。代码在 `input.peek(Brace)` 处只认花括号 `{`，圆括号不匹配，于是进入 `else { None }` 分支——冒号被消费，但说明符被丢弃（不会报错，只是忽略）。这说明格式说明符**必须**用花括号包裹。

### 4.3 Tr 容器与 Parse 入口

#### 4.3.1 概念说明

`Tr` 是整个 `_tr!` 宏调用的**总容器**——一次 `t!(...)` 调用解析后产出的就是它。它把「消息（首个参数）」和「其余命名参数」打包在一起，并额外承载从系统参数里剥离出来的 `locale` 与 `minify_key` 系列。`Tr::parse` 是入口，`Tr::into_token_stream` 是出口（4.5 节），`Tr::filter_arguments` 是中间的清洗步骤（4.4 节）。

#### 4.3.2 核心流程

`Tr::parse` 的解析顺序是固定的三步：

```
1. input.parse::<Messsage>()          ← 消费第一个参数（消息 key 或字面量）
2. input.parse::<Option<Token![,]>>() ← 看有没有逗号
   └─ 有逗号 → input.parse::<Arguments>() 消费其余命名参数
   └─ 无逗号 → Arguments::default()（空）
3. result.filter_arguments()          ← 剥离 locale / _minify_key*（详见 4.4）
```

其中 `Messsage`（注意源码里是三个 `s` 的拼写 `Messsage`，是仓库里保留的拼写）代表「宏的第一个位置参数」，它内部也持有一个 `Value`。

#### 4.3.3 源码精读

`Tr` 结构在 [crates/macro/src/tr.rs:L262-L270](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L262-L270)：

```rust
pub(crate) struct Tr {
    pub msg: Messsage,
    pub args: Arguments,
    pub locale: Option<Value>,
    pub minify_key: bool,
    pub minify_key_len: usize,
    pub minify_key_prefix: String,
    pub minify_key_thresh: usize,
}
```

- `msg`：首个位置参数（消息）。
- `args`：除系统参数外的「普通插值参数」，最终喂给 `replace_patterns`。
- `locale`、`minify_key*`：从 `args` 里**剥离**出来的系统参数（4.4 节），初始值来自 `Tr::new`（[tr.rs:L273-L283](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L273-L283)），其中 minify 系列默认值取自 `rust_i18n_support` 的 `DEFAULT_MINIFY_KEY_*` 常量。

`Messsage` 结构在 [crates/macro/src/tr.rs:L224-L229](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L224-L229)（注意字段 `key` 标了 `#[allow(dead_code)]`，实际从不写入，只是占位）：

```rust
#[derive(Default)]
pub struct Messsage {
    #[allow(dead_code)]
    key: proc_macro2::TokenStream,
    val: Value,
}
```

`Messsage::parse`（[tr.rs:L254-L259](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L254-L259)）同样用 fork 探测：先试 `Expr`（覆盖字面量、`format!(..)`、元组），失败再试 `Ident`（裸变量）。

`Tr::parse` 入口在 [crates/macro/src/tr.rs:L475-L495](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L475-L495)：

```rust
impl syn::parse::Parse for Tr {
    fn parse(input: syn::parse::ParseStream) -> syn::parse::Result<Self> {
        let msg = input.parse::<Messsage>()?;
        let comma = input.parse::<Option<Token![,]>>()?;
        let args = if comma.is_some() {
            input.parse::<Arguments>()?
        } else {
            Arguments::default()
        };
        let mut result = Self { msg, args, ..Self::new() };
        result.filter_arguments()?;
        Ok(result)
    }
}
```

`Arguments` 本身只是 `Vec<Argument>` 的包装，其解析用 `parse_terminated` 按逗号切分（[tr.rs:L214-L222](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L214-L222)）。`..Self::new()` 用默认值填满 `locale`/`minify_key*`，随后 `filter_arguments` 再按实际传入覆盖。

#### 4.3.4 代码实践

**实践目标**：跟踪 `t!("messages.hello", locale = "zh-CN", "name" => "Jason")` 在 `Tr::parse` 里的解析顺序。

**操作步骤**：

1. 第一个 token `"messages.hello"` → `Messsage::parse` 走 `try_exp` 分支，得到 `msg.val = Value::Expr(字面量 "messages.hello")`。
2. 遇到逗号 → `comma = Some`，进入 `Arguments::parse`。
3. `Arguments` 按逗号切出两个 `Argument`：`locale = "zh-CN"` 和 `"name" => "Jason"`，此时它们**都**还在 `args` 里。
4. 调 `filter_arguments`（4.4 节）——`locale` 被搬走、`"name"` 留下。

**需要观察的现象**：解析阶段 `locale` 和 `name` 平起平坐地待在 `args` 里，**直到 `filter_arguments` 才把它们分流**。

**预期结果**：解析完成后 `Tr.args` 只剩 `name`，`Tr.locale = Some(值 "zh-CN")`。

> 待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`Messsage.key` 字段为什么标了 `#[allow(dead_code)]`？

**参考答案**：它被定义出来但从不在 `Messsage::try_exp`/`try_ident` 里赋值（两者都写 `key: Default::default()`），后续 `into_token_stream` 也只读 `msg.val`。它是个遗留/占位字段，加 `#[allow(dead_code)]` 是为了压住「未使用字段」警告。这也提示读者：真正参与代码生成的只有 `val`。

**练习 2**：`Tr::parse` 里为什么先解析 `Messsage` 再解析 `Arguments`，而不是反过来？

**参考答案**：因为 `_tr!` 的语法是「位置参数在前，命名参数在后」——第一个位置参数是消息，逗号之后才是若干 `key = value`。这种顺序与绝大多数 Rust 宏（如 `println!`、`format!`）一致，符合直觉。

### 4.4 filter_arguments：剥离 locale 与 _minify_key* 系统参数

#### 4.4.1 概念说明

回顾 u3-l1：`__rust_i18n_t!` 会把 `_minify_key`、`_minify_key_len`、`_minify_key_prefix`、`_minify_key_thresh` 四个**系统参数**自动注入每次 `_tr!`；同时用户也可能传 `locale = "zh-CN"`。这些参数**不是**插值变量——它们控制「用哪个 locale 查」「key 怎么算」，**绝不能**被当成 `%{locale}`、`%{_minify_key}` 那样的占位符去替换。

`filter_arguments` 就是干这件事的：它遍历所有 `Argument`，把名字属于「系统参数」的挑出来，存进 `Tr` 的对应字段，然后用 `retain` 把它们从 `args` 里**删掉**。这样 `args` 里剩下的就只有真正的插值变量，能安全地交给 `replace_patterns`。

这是本讲指定的核心实践任务所聚焦的机制。

#### 4.4.2 核心流程

```
filter_arguments():
  第一遍 for：遍历每个 arg，按 name 分流
    "locale"            → self.locale = Some(arg.value)
    "_minify_key"       → self.minify_key = parse_minify_key(arg.value)
    "_minify_key_len"   → self.minify_key_len = parse_minify_key_len(arg.value)
    "_minify_key_prefix"→ self.minify_key_prefix = parse_minify_key_prefix(arg.value)
    "_minify_key_thresh"→ self.minify_key_thresh = parse_minify_key_thresh(arg.value)
    其它                → 忽略（留在 args 里）
  第二步 retain：把上述 5 个名字从 args 里移除
```

关键结论：**经过 `filter_arguments` 后，`args` 里只剩插值变量，`locale` 永远不会出现在 `keys` 数组里**，因此后续 `replace_patterns` 不会把 `%{locale}` 误替换成 locale 字符串。

#### 4.4.3 源码精读

`filter_arguments` 在 [crates/macro/src/tr.rs:L342-L376](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L342-L376)：

```rust
fn filter_arguments(&mut self) -> syn::parse::Result<()> {
    for arg in self.args.iter() {
        match arg.name.as_str() {
            "locale" => { self.locale = Some(arg.value.clone()); }
            "_minify_key" => { self.minify_key = Self::parse_minify_key(&arg.value)?; }
            "_minify_key_len" => { self.minify_key_len = Self::parse_minify_key_len(&arg.value)?; }
            "_minify_key_prefix" => { self.minify_key_prefix = Self::parse_minify_key_prefix(&arg.value)?; }
            "_minify_key_thresh" => { self.minify_key_thresh = Self::parse_minify_key_thresh(&arg.value)?; }
            _ => {}
        }
    }
    self.args.as_mut().retain(|v| {
        !["locale", "_minify_key", "_minify_key_len", "_minify_key_prefix", "_minify_key_thresh"]
            .contains(&v.name.as_str())
    });
    Ok(())
}
```

两段逻辑：

- **分流**：`match` 把系统参数挑出来。其中 `locale` 只存原始 `Value`（保留 token，供生成阶段直接引用），而 `_minify_key*` 还要**进一步解析类型**——`_minify_key` 要 bool 或 `"true"/"false"/"yes"/"no"`（[tr.rs:L285-L304](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L285-L304)），`_minify_key_len`/`_minify_key_thresh` 要整数，`_minify_key_prefix` 要字符串。
- **retain 删除**：用 `Vec::retain` 反向筛选，凡名字在「黑名单」里的一律删掉。删完之后 `args` 就干净了。

> 注意 `Tr::parse`（[tr.rs:L491](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L491)）在构造完 `result` 后**立即**调用 `filter_arguments`，所以到达 `into_token_stream` 时 `args` 已经不含系统参数了。

#### 4.4.4 代码实践

**实践目标（本讲指定任务）**：说明 `_tr!` 如何识别并处理 `locale` 这个特殊参数、如何通过 `filter_arguments` 把它从后续 `replace_patterns` 的参数列表里移除。

**操作步骤**：以 [tests/integration_tests.rs:L213-L216](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/integration_tests.rs#L213-L216) 的真实用例为对象：

```rust
t!("messages.hello", locale = "zh-CN", "name" => "Jason")
// 期望：用 zh-CN 查 "messages.hello"，再把 %{name} 换成 Jason
```

1. **解析阶段**：`Arguments` 解析出两条记录：`locale = "zh-CN"`、`"name" => "Jason"`，此时二者都在 `args` 里。
2. **filter 分流**：`for` 循环遇到 `name == "locale"` → `self.locale = Some(Value("zh-CN"))`；遇到 `name == "name"` → `match` 落入 `_ => {}`，**不动**。
3. **retain 删除**：`locale` 命中黑名单被删，`name` 保留。`args` 现在只剩 `name => "Jason"`。
4. **生成阶段（4.5）**：`keys = ["name"]`、`values = [format!("{}", "Jason")]`、`locale` 被 `into_token_stream` 直接引用成字面量 `"zh-CN"` 传给 `_rust_i18n_try_translate`。

**需要观察的现象**：`locale` 从未进入 `keys` 数组，所以即便译文里有 `%{locale}` 占位符，也**不会**被替换成 `"zh-CN"`——它纯粹作为「查哪个 locale」的参数使用。

**预期结果**：

- `replace_patterns(&translated, &["name"], &[format!({}, "Jason")])` —— `keys` 里没有 `locale`。
- 查找发生在 `crate::_rust_i18n_try_translate("zh-CN", "messages.hello")`。
- 译文 `"你好，%{name}！"` 最终变成 `"你好，Jason！"`，与测试断言一致。

> 待本地验证：可用 `RUST_I18N_DEBUG=1 cargo build` 打印生成代码，确认 `locale` 出现在 `_rust_i18n_try_translate` 的参数里、而不在 `keys`/`values` 数组里。

#### 4.4.5 小练习与答案

**练习 1**：如果用户故意写 `t!("hi", locale = "en")`，而译文里恰好有 `%{locale}` 占位符，会发生什么？

**参考答案**：`locale` 被 `filter_arguments` 剥离，不会出现在 `keys` 里，所以 `replace_patterns` 找不到匹配的键，会**原样保留** `%{locale}` 这段文本（即不替换）。locale 仅作为查找参数，不参与插值。

**练习 2**：`_minify_key` 接受哪些形式的值？

**参考答案**：见 `parse_minify_key`（[tr.rs:L285-L304](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L285-L304)）：布尔字面量 `true`/`false`，或字符串 `"true"`/`"false"`/`"yes"`/`"no"`（`yes`/`true` 视为开，`no`/`false` 视为关）。其它形式报错。注意 `__rust_i18n_t!` 注入的是 `_minify_key = #minify_key`，其中 `#minify_key` 是编译期 bool 插值（u3-l1），所以日常走的是布尔分支。

### 4.5 into_token_stream：无参数 vs 有参数两条代码路径

#### 4.5.1 概念说明

`into_token_stream` 是 `_tr!` 的「出口」：把清洗后的 `Tr` 翻译成一段等价 Rust 表达式。它先决定两件事——**key 怎么算**（受 `minify_key` 影响，4 个分支）和 **locale 用哪个**——再按 **`args` 是否为空**选两条代码路径：

- **无参数路径**：命中译文直接返回；miss 返回原消息值。
- **有参数路径**：命中后多一步 `replace_patterns` 替换 `%{name}` 占位符；miss 则对原值做替换。

无论哪条，查表入口都是同一个 `crate::_rust_i18n_try_translate(locale, &msg_key)`（u3-l1 讲过它内部就是 `_RUST_I18N_BACKEND.translate()`）。本讲聚焦「两条路径如何构造 `keys`/`values`」，占位符替换的字节级实现留待 u3-l3。

#### 4.5.2 核心流程

```
into_token_stream:
  A. 算 (msg_key, msg_val)：minify_key 的 4 分支（默认 false → msg_key = &msg_val）
  B. 算 locale：有 → 引用值；无 → &rust_i18n::locale()
  C. 构造 keys/values 两个数组：
       keys[i]   = arg.name
       values[i] = format!("{{spec}}", arg.value)   // spec 由 specifiers 决定
  D. 分支：
       args.is_empty()  → 无参数路径（不调用 replace_patterns）
       否则             → 有参数路径（命中/miss 都调用 replace_patterns）
```

C 步是本模块的关键：`specifiers` 决定 `format!` 的格式串——`Some(":08")` → `format!("{:08}", v)`，`None` → `format!("{}", v)`。`keys` 和 `values` **按下标对齐**，一起传给 `replace_patterns`。

#### 4.5.3 源码精读

`keys`/`values` 的构造在 [crates/macro/src/tr.rs:L418-L431](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L418-L431)：

```rust
let keys: Vec<_> = self.args.keys().iter().map(|v| quote! { #v }).collect();
let values: Vec<_> = self.args.as_ref().iter().map(|v| {
    let value = &v.value;
    let sepecifiers = v.specifiers.as_ref()
        .map_or("{}".to_owned(), |s| format!("{{{}}}", s));
    quote! { format!(#sepecifiers, #value) }
}).collect();
```

注意 `format!("{{{}}}", s)` 这个表达式本身是**过程宏 crate 编译期**执行的：`{{` 是字面 `{`、`{}` 格式化 `s`、`}}` 是字面 `}`，所以 `s = ":08"` → `"{:08}"`、`s = ""` → `"{}"`。这与 4.2 节「specifiers 存花括号内部内容」的设计严丝合缝。

**无参数路径**在 [crates/macro/src/tr.rs:L433-L445](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L433-L445)：

```rust
if self.args.is_empty() {
    quote! {
        {
            let msg_val = #msg_val;
            let msg_key = #msg_key;
            if let Some(translated) = crate::_rust_i18n_try_translate(#locale, &msg_key) {
                translated.into()
            } else {
                #logging
                rust_i18n::CowStr::from(msg_val).into_inner()
            }
        }
    }
}
```

**有参数路径**在 [crates/macro/src/tr.rs:L446-L465](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L446-L465)，比无参数多出 `keys`/`values` 两个数组，并在命中与 miss 两个分支里都调用 `replace_patterns`：

```rust
} else {
    quote! {
        {
            let msg_val = #msg_val;
            let msg_key = #msg_key;
            let keys = &[#(#keys),*];
            let values = &[#(#values),*];
            {
                if let Some(translated) = crate::_rust_i18n_try_translate(#locale, &msg_key) {
                    let replaced = rust_i18n::replace_patterns(&translated, keys, values);
                    std::borrow::Cow::from(replaced)
                } else {
                    #logging
                    let replaced = rust_i18n::replace_patterns(
                        rust_i18n::CowStr::from(msg_val).as_str(), keys, values);
                    std::borrow::Cow::from(replaced)
                }
            }
        }
    }
}
```

> `#logging` 来自 `log_missing()`（[tr.rs:L378-L388](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L378-L388)）：开启 `log-miss-tr` feature 时，miss 会 `log::warn!` 一条 `missing: ...`；未开启则是空 token 流。

minify_key 的 4 分支（`msg_key` 的来源）在 [crates/macro/src/tr.rs:L390-L413](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L390-L413)。默认 `minify_key = false`，走最后一个 `else`：`msg_key = &msg_val`，即原样用字面量当 key。另外三个分支（字面量编译期算短键、元组拆分、动态值运行时哈希）属 minify_key 主题，详见 u6-l2/u6-l3。

#### 4.5.4 代码实践

**实践目标**：给定一个带格式说明符的调用，手算 `keys`/`values` 数组，并指出 `locale` 为何不出现。

**操作步骤**：对 `t!("You have %{count} messages.", locale = "zh-CN", count = 7 : {:05})`（改编自 [src/lib.rs:L133](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L133) 的文档示例与 [tests/integration_tests.rs:L166-L180](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/integration_tests.rs#L166-L180)），写出有参数路径展开骨架：

```rust
{
    let msg_val = "You have %{count} messages.";
    let msg_key = &msg_val;                      // 默认未开 minify_key
    let keys = &["count"];                        // ← locale 不在这里
    let values = &[format!("{:05}", 7)];          // specifiers ":05" → "{:05}"
    {
        // locale 被 filter_arguments 剥离后，直接当查找参数
        if let Some(translated) = crate::_rust_i18n_try_translate("zh-CN", &msg_key) {
            let replaced = rust_i18n::replace_patterns(&translated, keys, values);
            std::borrow::Cow::from(replaced)
        } else {
            let replaced = rust_i18n::replace_patterns(
                rust_i18n::CowStr::from(msg_val).as_str(), keys, values);
            std::borrow::Cow::from(replaced)
        }
    }
}
```

**需要观察的现象**：

- `values[0]` 是 `format!("{:05}", 7)`，求值为 `"00007"`（零填充到 5 位）。
- `keys` 数组只有 `"count"`，**没有** `"locale"`——正是 4.4 节 `filter_arguments` 的功劳。
- 译文 `"你收到了 %{count} 条新消息。"` 经 `replace_patterns` 后，`%{count}` 被换成 `"00007"`。

**预期结果**：`format!("{:05}", 7) == "00007"`；最终译文形如 `"你收到了 00007 条新消息。"`（实际译文措辞以 `locales/zh-CN.yml` 为准）。

> 待本地验证：可参照 [tests/integration_tests.rs:L175](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/integration_tests.rs#L175) 的 `t!("messages.other", locale = "zh-CN", count = 1 + 2,)` 用例运行 `cargo test`。

#### 4.5.5 小练习与答案

**练习 1**：`values` 数组里每个元素为什么都要被 `format!(...)` 包一层，而不是直接放原始值？

**参考答案**：为了统一类型并支持格式说明符。`replace_patterns` 的 `values` 参数是 `&[String]`（见 [src/lib.rs:L45](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L45)），而原始值可能是 `i32`、`&str`、表达式 `1 + 2` 等异质类型。用 `format!("{}", v)` 或 `format!("{:05}", v)` 既把任意类型转成 `String`，又顺带把 `: {:05}` 这样的说明符作用上去。

**练习 2**：无参数路径里，命中译文返回 `translated.into()`，这里 `translated` 是什么类型？

**参考答案**：`translated` 是 `_rust_i18n_try_translate` 返回的 `Option<Cow<'r, str>>` 解包后的 `Cow<str>`。命中静态后端时它是 `Cow::Borrowed`（零拷贝，借用字面量，见 u2-l4）。`.into()` 把它转成 `t!` 最终的返回类型（通常是 `String` 或 `Cow<str>`，取决于上下文）。

**练习 3**：为什么有参数路径的 miss 分支里，还要对原 `msg_val` 调用一次 `replace_patterns`？

**参考答案**：因为即使译文表里 miss（没找到翻译），用户传入的插值参数仍可能有意义——比如 `t!("Hello, %{name}", name="Jason")` 在没有翻译文件时，应当输出 `"Hello, Jason"` 而非带占位符的原串。所以 miss 时用原消息值当作「译文」做一次占位符替换，保证即便不翻译也能得到可读文本。

## 5. 综合实践

**任务**：把本讲四个模块串起来，完整跟踪一次带 `locale`、`=>`、格式说明符的复杂调用，并验证 `locale` 被正确剥离。

**步骤**：

1. 在 `examples/app`（或自有项目）里准备译文。在 `locales/en.yml` 与 `locales/zh-CN.yml` 里加入（v1 格式）：
   ```yaml
   # en.yml
   order:
     summary: "Order #%{sn} for %{name}"
   # zh-CN.yml
   order:
     summary: "订单 #%{sn}，收件人 %{name}"
   ```
2. 在 `main.rs` 写：
   ```rust
   i18n!("locales");
   fn main() {
       rust_i18n::set_locale("en");
       println!(
           "{}",
           t!("order.summary", locale = "zh-CN", "name" => "Alice", sn = 42 : {:06})
       );
   }
   ```
3. **解析阶段手算**：列出 `Tr::parse` 之后、`filter_arguments` 之前的 `args`（应含 `locale`、`name`、`sn` 三项）。
4. **filter 之后手算**：确认 `args` 只剩 `name`、`sn`；`self.locale = Some("zh-CN")`。
5. **codegen 手算**：写出 `keys`、`values` 数组，指出 `locale` 出现在 `_rust_i18n_try_translate(...)` 的哪个参数位、为何不在 `keys` 里。
6. 运行 `cargo run`，确认输出 `订单 #000042，收件人 Alice`。

**验收标准**：

- 能说清 `format!("{:06}", 42) == "000042"` 的来由（`specifiers = ":06"` → `"{:06}"`）。
- 能指出 `locale = "zh-CN"` 经过 `filter_arguments` 后**只**作为查找参数、**不**进入 `replace_patterns` 的 `keys`，所以译文里的 `%{name}`、`%{sn}` 被替换，而不会有人误把 `%{locale}`（如果存在）替换掉。
- 能用 `RUST_I18N_DEBUG=1 cargo build` 对照生成代码逐行验证。

> 待本地验证。

## 6. 本讲小结

- `Value`（[tr.rs:L7-L13](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L7-L13)）是参数「值」的统一枚举（`Empty`/`Expr`/`Ident`），靠 fork 探测解析；`to_string()` 只对字符串字面量返回 `Some`，这决定了 minify_key 的编译期短键只能对字面量生效。
- `Argument`（[tr.rs:L133-L176](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L133-L176)）统一解析 `key = value`、`key => value`、`key = value : {:spec}` 三种形态：名字可是标识符或字符串字面量，分隔符 `=`/`=>` 等价，specifiers 存花括号**内部**内容（如 `:08`）。
- `Tr`（[tr.rs:L262-L270](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L262-L270)）是总容器，`Tr::parse`（[tr.rs:L475-L495](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L475-L495)）按「Messsage → 可选逗号 → Arguments → filter_arguments」顺序消费 token。
- `filter_arguments`（[tr.rs:L342-L376](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L342-L376)）把 `locale`、`_minify_key*` 五个系统参数从 `args` **分流并 retain 删除**，确保它们绝不进入 `replace_patterns` 的 `keys`/`values`——这是 `locale` 不会被当占位符误替换的根本保证。
- `into_token_stream`（[tr.rs:L390-L466](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L390-L466)）按 `args.is_empty()` 选**无参数**（命中直接返回）与**有参数**（命中/miss 都调 `replace_patterns`）两条路径；`values` 每个元素都被 `format!("{{spec}}", v)` 包一层，把类型统一成 `String` 并应用格式说明符。
- 三种用户写法（`=`/`=>`/`: spec`）、两类参数（系统/插值）、两条代码路径，在 `_tr!` 内部被 `Value`/`Argument`/`Tr`/`filter_arguments`/`into_token_stream` 五个模块协作消化，最终都收敛到同一个查表入口 `crate::_rust_i18n_try_translate(locale, &msg_key)`。

## 7. 下一步学习建议

本讲讲清了 `_tr!` 的「解析与代码生成」，但有意把两个相邻话题留到后面：

1. **占位符替换的字节级实现** → **u3-l3（变量插值与格式化说明符）**：本讲里 `replace_patterns(&translated, keys, values)` 只当黑盒调用，下一讲钻进 [src/lib.rs:L45-L91](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/src/lib.rs#L45-L91) 的字节扫描状态机，看 `%{name}` 是怎么被精确定位与替换的。
2. **查找失败后的回退** → **u3-l4（翻译回退机制）**：本讲里 `_rust_i18n_try_translate` 内部的 territory 回退（`zh-Hant-CN → zh-Hant → zh`）和显式 `fallback` 列表都是黑盒，u3-l4 会按 RFC 4647 思路逐级展开。
3. **minify_key 的 4 个 codegen 分支** → **u6-l2（`_minify_key!` 与 `tkv!` 宏）** 和 **u6-l3（短键在 `t!` 与提取器中的协作）**：本讲 4.5 节只点了「默认 `else` 分支」，另外三条（字面量编译期算键、元组拆分、动态值运行时哈希）的算法在 u6 系列详讲。

阅读源码时，建议把本讲的 [crates/macro/src/tr.rs:L133-L176](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L133-L176)（Argument 解析）与 [crates/macro/src/tr.rs:L342-L376](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/tr.rs#L342-L376)（filter_arguments）对照 [tests/integration_tests.rs:L206-L217](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/integration_tests.rs#L206-L217) 的 `test_t_with_hash_args` 一起看，那条测试同时覆盖了 `=`、`=>`、字面量键名、`locale=` 四种用法，是本讲最好的「可运行断言」。
