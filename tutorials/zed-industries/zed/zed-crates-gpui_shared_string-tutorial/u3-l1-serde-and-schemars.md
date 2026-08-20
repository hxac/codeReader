# serde 与 schemars：让字符串类型透明序列化

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂并掌握 `SharedString` 的手写 `Serialize` 实现——一行 `serializer.serialize_str(self.as_ref())` 如何让它在任何 serde 数据格式里都表现为普通字符串。
2. 读懂并掌握手写 `Deserialize` 实现——「先按 `String` 反序列化，再用 `new` 构造」的两行模式，以及它带来的错误语义与内存成本。
3. 理解 `JsonSchema` 实现为何把 `inline_schema`、`schema_name`、`json_schema` 三个方法全部委托给 `String`：让生成的 JSON Schema 里根本不出现 `SharedString` 这个名字。
4. 了解 schemars 在 Zed 中的真实用途：设置编辑器的 JSON Schema、MCP / agent 工具的输入 schema、UI inspector 的样式 schema，以及 `SharedString` 如何 invisibly 地流经这些链路。

本讲是 u2-l2「透明代理」哲学在序列化维度的收官：`Deref`/`AsRef`/`Borrow` 让 `SharedString` 在 Rust 类型系统里像 `str`，本讲的三个 impl 让它在 JSON 世界里也像 `str`。

## 2. 前置知识

### 2.1 序列化与 serde 是什么

**序列化（serialization）** 是把内存里的数据结构变成可存储、可传输的字节流（如 JSON 文本）；**反序列化（deserialization）** 是反过来。Rust 生态的事实标准库是 [serde](https://serde.rs)，它把这件事拆成两对 trait：

| trait | 实现者 | 职责 |
| --- | --- | --- |
| `Serialize` | 数据类型（如 `SharedString`） | 告诉 serde「我这个类型由哪些值组成」 |
| `Serializer` | 格式库（如 `serde_json`） | 决定值如何被写出（JSON、TOML、MessagePack……） |
| `Deserialize` | 数据类型 | 告诉 serde「如何从输入构造我」 |
| `Deserializer` | 格式库 | 决定输入如何被读取、类型如何校验 |

这个拆分带来的关键性质是**格式无关**：一个类型只要实现一次 `Serialize`/`Deserialize`，就能被 `serde_json`、`toml`、`postcard` 等任何格式库读写。本讲看到的 `serializer.serialize_str(...)` 就是「类型只声明：我是一个字符串」，具体怎么转义、怎么编码，完全交给格式库。

### 2.2 JSON Schema 与 schemars 是什么

**JSON Schema** 是一种用 JSON 描述「合法 JSON 数据长什么样」的规范，例如 `{"type": "string"}` 表示「这里应该是一个字符串」。Zed 用它做两件事：

- 给**设置编辑器**提供校验与自动补全（你的 `settings.json` 里每个键的合法取值就来自它）；
- 给 **MCP / agent 工具**描述输入参数的形状（发给大模型的工具定义里附带的 `input_schema`）。

[schemars](https://docs.rs/schemars) 则是「Rust 类型 → JSON Schema」的自动生成器。一个类型实现 `JsonSchema` trait 后，`schemars::schema_for!(T)` 或 `generator.root_schema_for::<T>()` 就能产出它的 schema。Zed 工作区锁定的版本是 `schemars = { version = "1.0", features = ["indexmap2"] }`（见 [Cargo.toml:794](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/Cargo.toml#L794)），1.0 版的 `JsonSchema` trait 有三个方法：`inline_schema`、`schema_name`、`json_schema`，本讲 4.3 会逐个精读。

### 2.3 为什么一个 UI 字符串类型要关心这些

回顾 u1-l1：`SharedString` 是 Zed 里传递 UI 文本的基础类型。而 Zed 的大量配置面（主题字体、agent 面板字体、markdown 预览字体……）的值都是 `SharedString`，这些值要写进 `settings.json`、要在设置编辑器里补全、要在工具 schema 里描述。于是它必须回答三个问题：

- **写出去**是什么形态？（`Serialize`）
- **读进来**怎么构造、错误怎么报？（`Deserialize`）
- **自我描述**时长什么样？（`JsonSchema`）

本讲标题里的「透明」就是答案：三个问题的回答全部是「我就是个普通字符串」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/gpui_shared_string/gpui_shared_string.rs` | 本讲主角。L42-L54 是 `JsonSchema` impl，L194-L201 是 `Serialize` impl，L203-L211 是 `Deserialize` impl |
| `crates/gpui_shared_string/Cargo.toml` | 声明 `schemars.workspace = true`、`serde.workspace = true`、`smol_str = "0.3.6"` 三个依赖 |
| `Cargo.toml`（仓库根） | workspace 级依赖版本：schemars 1.0、serde 1.0.221、serde_json 1.0.144 |
| `crates/settings/src/settings_store.rs` | Zed 设置系统生成 JSON Schema 的入口 `SettingsStore::json_schema`，其参数结构里直接装着 `&[SharedString]` |
| `crates/theme_settings/src/settings.rs` | `ThemeSettings` 中多个 `Option<SharedString>` 设置字段（agent 字体、markdown 预览字体等） |
| `crates/gpui/src/style.rs` | `TextStyle.font_family: SharedString`，经 `#[refineable(...)]` 携带序列化与 JsonSchema 能力 |
| `crates/json_schema_store/src/json_schema_store.rs` | 为 UI inspector 生成 `StyleRefinement` 的 JSON Schema |
| `crates/settings_ui/src/settings_ui.rs` | 设置编辑器 UI：为 `SharedString` 字段注册文本输入框渲染器 |
| `crates/context_server/src/listener.rs`、`crates/agent/src/thread.rs` | schemars 的另外两个真实用途：MCP 工具与 agent 工具的输入 schema |

## 4. 核心概念与源码讲解

### 4.1 Serialize：写出去就是普通字符串

#### 4.1.1 概念说明

`Serialize` 负责回答「这个类型的值如何被写出」。serde 的 `Serializer` trait 提供了一组基础原语：`serialize_str`、`serialize_u64`、`serialize_seq`……类型实现者从中挑选并调用。`SharedString` 的选择是调用 `serialize_str`——即声明「我是一个字符串」，这与 `&str`、`String` 的做法完全一致，因此在任何输出里两者不可区分。

为什么手写而不是 `#[derive(Serialize)]`？derive 的工作方式是逐字段委托：newtype 只有一个字段，所以 derive 出来的行为会**完全由内层 `SmolStr` 的 serde 实现决定**。而本 crate 在 [crates/gpui_shared_string/Cargo.toml:13](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/Cargo.toml#L13) 声明 `smol_str = "0.3.6"` 时未开启任何 feature，没有把 smol_str 的 serde 集成纳入自己的构建；更重要的是架构层面的理由——u2-l1 讲过，文档里的「currently backed by a `SmolStr`」承诺底层可替换。**序列化形态是公共契约**（`settings.json`、主题文件都依赖它），手写把这层契约钉死为「字符串」，与内层表示解耦：将来换成别的实现，磁盘上的 JSON 一个字节都不用变。

#### 4.1.2 核心流程

序列化 `Config { name: SharedString }` 时的调用链：

```text
serde_json::to_string(&config)
  └─ Config 的 derive(Serialize) 逐字段调用
      └─ SharedString::serialize(serializer)          ← 手写 impl
          └─ self.as_ref()                            ← 借出 &str（AsRef<str>，见 u2-l2）
          └─ serializer.serialize_str(&str)           ← 声明「我是字符串」
              └─ serde_json 按 JSON 规则转义并写出
```

关键点：

- `as_ref()` 返回的是底层字节的 `&str` **借用视图**，零复制、零新分配；
- 序列化路径上没有任何中间 `String` 被构造；
- 转义、编码等细节由格式库决定，所以换 TOML / MessagePack 也照常工作。

#### 4.1.3 源码精读

先看依赖与导入。[crates/gpui_shared_string/Cargo.toml:10-13](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/Cargo.toml#L10-L13) 声明了三个依赖：`schemars` 与 `serde` 通过 workspace 继承版本，`smol_str` 直接写死版本且不带 feature。[Cargo.toml:794-798](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/Cargo.toml#L794-L798) 是 workspace 侧的真实版本声明。对应地，源文件顶部导入这两个 trait：

[crates/gpui_shared_string/gpui_shared_string.rs:7-8](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L7-L8) 引入 `JsonSchema` 与 `Deserialize`/`Serialize`，说明本 crate 只使用 trait、不使用 serde 的 derive 宏。

正文实现（位于文件末尾）：

> [crates/gpui_shared_string/gpui_shared_string.rs:194-201](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L194-L201) 手写的 `Serialize`：把自身借出为 `&str` 后调用 `serialize_str`，即「把自己当作普通字符串写出」。

```rust
impl Serialize for SharedString {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: serde::Serializer,
    {
        serializer.serialize_str(self.as_ref())
    }
}
```

四个细节值得圈出来：

1. `S: serde::Serializer` 是**泛型**序列化器——不绑定 JSON，任何 serde 格式都适用。
2. `self.as_ref()` 走的是 u2-l2 精读过的 [`AsRef<str>` 实现](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L62-L66)（返回 `&self.0` 并经解引用强转成 `&str`）。u2-l2 的「透明代理」在这里被序列化路径复用。
3. 返回 `Result<S::Ok, S::Error>` 是 serde 的标准签名；写字符串本身几乎不会失败，错误通道主要留给格式库（如 IO）。
4. 对比 derive：如果写成 `#[derive(Serialize)]`，生成代码大致是 `self.0.serialize(serializer)`——行为将取决于 `SmolStr` 是否/如何实现 serde，而不是这里显式钉死的 `serialize_str`。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `SharedString` 序列化后在 JSON 里与普通字符串毫无差别。

**操作步骤**（沿用 u1-l2 建立的独立练习项目，注意把它放在 zed 仓库目录之外；若必须放在仓库内，在练习项目的 `Cargo.toml` 里加一个空的 `[workspace]` 表，避免被 zed 的 workspace 吞掉）：

1. 新建项目 `shared-string-serde`，`Cargo.toml` 如下（`path` 按你本地 zed 克隆位置调整）：

   ```toml
   [package]
   name = "shared-string-serde"
   version = "0.1.0"
   edition = "2021"

   [dependencies]
   gpui_shared_string = { path = "../zed/crates/gpui_shared_string" }
   serde = { version = "1", features = ["derive"] }
   serde_json = "1"
   schemars = "1.0"
   ```

2. 编写 `src/main.rs`（示例代码）：

   ```rust
   use gpui_shared_string::SharedString;
   use schemars::JsonSchema;
   use serde::{Deserialize, Serialize};

   #[derive(Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
   struct Config {
       name: SharedString,
   }

   fn main() {
       let config = Config { name: "Zed".into() };
       let json = serde_json::to_string(&config).unwrap();
       println!("序列化结果: {json}");
   }
   ```

3. 运行 `cargo run`。

**需要观察的现象**：`Config` 的字段类型是 `SharedString`，但输出里没有任何类型痕迹。

**预期结果**：`序列化结果: {"name":"Zed"}`——与字段类型直接写 `String` 时的输出逐字节相同。（待本地验证）

#### 4.1.5 小练习与答案

**练习 1**：把 `name` 换成包含中文与引号的文本（如 `format!("Zed \"编辑器\" {}", 1).into()`），序列化输出会是什么？

**答案**：serde_json 按标准 JSON 规则正常转义引号、原样输出 UTF-8 中文，例如 `{"name":"Zed \"编辑器\" 1"}`。因为 `serialize_str` 走的就是所有字符串共用的序列化原语，`SharedString` 不引入任何额外包装或转义规则。

**练习 2**：`serde_json::to_value(&config)` 会得到什么？

**答案**：`Value::Object`，其中 `name` 的值是 `Value::String("Zed")`。这说明在 serde 的数据模型（`Value`）里，`SharedString` 与 `String` 不可区分——「透明」不仅发生在最终文本，也发生在中间表示层。

**练习 3**：这条路径上有几次堆分配？

**答案**：零次。`as_ref()` 只是借用视图，`serialize_str` 直接消费 `&str`；分配（如果格式库需要）发生在 serde_json 自己的输出缓冲区里，与 `SharedString` 无关。

### 4.2 Deserialize：先按 String 读入，再构造 SharedString

#### 4.2.1 概念说明

`Deserialize` 回答「如何从输入构造我」，这是三个 impl 里最有内容的一个。它采用 serde 里非常常见的**委托模式**：目标类型 `SharedString` 并不自己解析输入，而是把活儿原样转给 `String::deserialize`——`String` 已经把「读取一个字符串、校验类型、处理转义」做得尽善尽美，复用它意味着：

- **错误语义与 `String` 完全一致**：JSON 里给数字会得到与 `String` 字段相同的「类型不匹配」错误；
- **格式支持与 `String` 完全一致**：任何能反序列化字符串的格式都能直接用；
- **实现只有两行**，正确性几乎不可能出错。

拿到 `String` 后，再用 u1-l2 讲过的 `SharedString::new(&s)` 完成构造。`impl<'de>` 中的 `'de` 是输入数据的生命周期参数；本实现立即把内容转入自己的存储、不保留任何借用，因此对任意 `'de` 都成立（serde 术语里的 owned 反序列化）。

#### 4.2.2 核心流程

```text
serde_json::from_str::<Config>(r#"{"name":"Zed"}"#)
  └─ Config 的 derive(Deserialize) 逐字段调用
      └─ SharedString::deserialize(deserializer)        ← 手写 impl
          └─ String::deserialize(deserializer)?          ← 完整复用 String 的解析与校验
          └─ SharedString::new(&s)                       ← u1-l2 的构造路径
              ├─ len ≤ 23 字节 → 内联存储，零堆分配
              └─ 否则 → 堆分配并复制字节
          └─ s 在语句结束被丢弃（String 缓冲区释放）
```

内存成本可以写成：反序列化 \( = 1 \) 次 `String` 堆分配 \( + n \) 字节复制（\( n \le 23 \) 时进内联缓冲，否则伴随一次堆分配）。长字符串场景下会短暂存在「String 缓冲区 + SmolStr 堆存储」两份拷贝，随后前者释放。这是**用一点内存换两行实现的清晰**——与仓库编码准则里「正确性与清晰优先」一致。练习 3 会给出零中间分配的替代写法作对比。

#### 4.2.3 源码精读

> [crates/gpui_shared_string/gpui_shared_string.rs:203-211](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L203-L211) 手写的 `Deserialize`：把反序列化整体委托给 `String`，再用 `new` 构造自身。

```rust
impl<'de> Deserialize<'de> for SharedString {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        let s = String::deserialize(deserializer)?;
        Ok(SharedString::new(&s))
    }
}
```

逐行拆解：

1. `D: serde::Deserializer<'de>` 与 `Serialize` 的泛型 `S` 对称——格式库的读取端，同样格式无关。
2. `String::deserialize(deserializer)` 是**同一个 deserializer 的透传**（不是先把输入读成 `Value`），所以没有中间 JSON 树，效率与直接反序列化 `String` 字段相同。
3. `?` 把 `String` 反序列化失败的 `D::Error` 原样上传——错误信息（例如输入是数字时）与 `String` 字段完全一致，`SharedString` 不加工、不吞掉错误。
4. `SharedString::new(&s)` 正是 [crates/gpui_shared_string/gpui_shared_string.rs:31-34](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L31-L34) 的运行时构造入口，借用型来源必然复制字节（u2-l3 的结论在这里再次出现）。

#### 4.2.4 代码实践

**实践目标**：验证「往返一致」与「错误语义与 String 相同」两件事。

**操作步骤**：在 4.1.4 的项目里追加（示例代码）：

```rust
#[test]
fn roundtrip_and_reject() {
    let config = Config { name: "Zed".into() };

    // 往返：序列化 → 反序列化，结果与原值相等
    let json = serde_json::to_string(&config).unwrap();
    let parsed: Config = serde_json::from_str(&json).unwrap();
    assert_eq!(parsed, config);

    // 错误输入：name 给数字，应复用 String 的类型校验并报错
    let err = serde_json::from_str::<Config>(r#"{"name": 123}"#).unwrap_err();
    println!("错误信息: {err}");
}
```

然后运行 `cargo test -- --nocapture`。

**需要观察的现象**：往返断言通过；错误用例打印出 serde_json 的类型不匹配错误。

**预期结果**：断言通过；错误信息形如 `invalid type: integer \`123\`, expected a string at line 1 column 10`（具体文案随 serde_json 版本略有差异，待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：输入 `{"age": 1}`（缺 `name` 字段）会发生什么？错误由谁报出？

**答案**：报 `missing field \`name\`` 一类的错误，由 `Config` 上的 `derive(Deserialize)` 报出，与 `SharedString` 无关——字段缺失是容器层的责任，字段类型错误才是 `SharedString`（经 `String`）的责任。这条责任边界在排错时很有用。

**练习 2**：为什么实现里不直接写 `SharedString::deserialize` 内联解析，而是经过 `String`？

**答案**：复用。`String::deserialize` 覆盖了各格式下字符串读取的全部细节（JSON 转义、TOML 引号规则……）与类型校验；自己解析需要手写 `Visitor`，代码量约 15 行且容易遗漏分支。两行委托换来与 `String` 完全一致的行为，是典型的「正确性优先」取舍。

**练习 3（进阶）**：写一个不经过中间 `String` 的 `Visitor` 版本。

**答案**（示例代码，仅供对比，不是项目原有实现）：

```rust
use serde::de::{Deserializer, Visitor};

impl<'de> Deserialize<'de> for SharedString {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        struct SharedStringVisitor;

        impl<'de> Visitor<'de> for SharedStringVisitor {
            type Value = SharedString;

            fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                write!(f, "a string")
            }

            fn visit_str<E>(self, v: &str) -> Result<Self::Value, E> {
                Ok(SharedString::new(v))
            }
        }

        deserializer.deserialize_str(SharedStringVisitor)
    }
}
```

它省掉了那次 `String` 堆分配（`visit_str` 拿到 `&str` 后直接构造），代价是十五行样板。Zed 选择了两行版本——对配置文件这种低频路径，清晰远比一次分配重要。

### 4.3 JsonSchema：三个方法全部委托 String

#### 4.3.1 概念说明

`JsonSchema` 让类型能被 schemars 描述成 JSON Schema。这里的难点与 serde 不同：**derive 在这里根本不可用**。`#[derive(JsonSchema)]` 要求每个字段类型都实现 `JsonSchema`，而本 crate 的内层 `SmolStr` 是第三方类型——schemars 只为标准库类型（及通过自身 feature 开启的少数第三方类型）提供实现，`SmolStr` 不在其列。所以必须手写，而手写时的选择耐人寻味：**不描述自己，而是委托 `String`**。

schemars 1.0 的 `JsonSchema` trait 有三个方法，本实现逐一转发给 `String` 的同名方法：

| 方法 | 作用 | 委托给 `String` 的效果 |
| --- | --- | --- |
| `json_schema` | 生成 schema 本体 | 得到字符串类型描述（`{"type": "string"}`） |
| `schema_name` | schema 被放进 `$defs` 时使用的定义名 | 即使被引用，名字也是 `String` 的，schema 里不出现 `SharedString` 字样 |
| `inline_schema` | schema 在使用处内联展开，还是收进 `$defs` 用 `$ref` 引用 | 与 `String` 完全一致 |

三个方法全部委托的效果是一种彻底的**身份替换**：对 schema 的任何消费者（设置编辑器、JSON Schema 校验器、大模型工具调用）而言，`SharedString` 字段与 `String` 字段生成的 schema 不可区分。这与 4.1/4.2 的 serde 行为、u2-l2 的 `Borrow` 契约一脉相承——「透明」不是偶然，而是刻意维护的公共契约。

#### 4.3.2 核心流程

以 `schemars::schema_for!(Config)`（`Config` 含 `name: SharedString`）为例：

```text
schema_for!(Config)
  └─ generator 遍历 Config 的字段
      └─ SharedString::json_schema(generator)      ← 手写 impl
          └─ String::json_schema(generator)        ← 委托
              └─ 返回 {"type": "string"}
  └─ 汇总为 {"type":"object", "properties":{"name":{"type":"string"}}, ...}
```

`json_schema` 接收 `&mut SchemaGenerator` 而不是返回独立值，是因为生成器要沿途收集类型定义（`$defs`）并支持递归类型的 `$ref` 引用；对 `String` 只是无状态透传，但 trait 方法签名是统一的。

#### 4.3.3 源码精读

> [crates/gpui_shared_string/gpui_shared_string.rs:42-54](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L42-L54) 手写的 `JsonSchema`：三个方法全部转发给 `String` 的同名实现，让 `SharedString` 在 schema 中与 `String` 完全同形。

```rust
impl JsonSchema for SharedString {
    fn inline_schema() -> bool {
        String::inline_schema()
    }

    fn schema_name() -> Cow<'static, str> {
        String::schema_name()
    }

    fn json_schema(generator: &mut schemars::SchemaGenerator) -> schemars::Schema {
        String::json_schema(generator)
    }
}
```

三个细节：

1. 这段 impl 位于结构体定义之后不远处的文件前段（L42），而 serde 的两个 impl 在文件末尾（L194 起）——纯粹是文件组织习惯，无行为差异。
2. `schema_name` 返回 `Cow<'static, str>`（借用或拥有的静态字符串），避免为固定名字分配。
3. 注意这里**没有**委托 `SmolStr`（它也没有可委托的实现），更没有自定义名字——若自定义名字，一旦某处 schema 以 `$ref` 引用此类型，消费者就会看到一个凭空多出的「SharedString」概念，透明性即被破坏。委托 `String` 是把「我就是字符串」贯彻到 schema 维度的唯一途径。

#### 4.3.4 代码实践

**实践目标**：亲眼确认生成的 schema 中 `name` 就是普通 `string` 类型，不出现 `SharedString`。

**操作步骤**：在 4.1.4 的项目里把 `main` 的结尾追加（示例代码）：

```rust
    let schema = schemars::schema_for!(Config);
    println!("{}", serde_json::to_string_pretty(&schema).unwrap());
```

运行 `cargo run`。

**需要观察的现象**：打印出的 schema 中 `properties.name` 的描述；通篇找不到 `SharedString` 字样。

**预期结果**：大致形如（`$schema` 行与 `required` 的有无取决于 schemars 的 draft 设置，具体以本地输出为准，待本地验证）：

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "type": "object",
  "required": ["name"],
  "properties": {
    "name": { "type": "string" }
  }
}
```

`schemars::schema_for!` 这个宏正是 Zed 自己在用的 API（见 4.4.3 引用的 agent 工具 schema 代码）。

#### 4.3.5 小练习与答案

**练习 1**：给 `Config` 增加 `tags: Vec<SharedString>` 字段，重新生成 schema，观察什么变了。

**答案**：`properties.tags` 变成数组类型（`{"type":"array", "items": {"type":"string"}}` 之类），其中 `items` 依旧是字符串描述。透明性沿容器递归传导——容器层描述结构，元素层依旧「就是字符串」。

**练习 2**：如果把 `schema_name` 改成返回 `"SharedString"`，会有什么后果？

**答案**：当某个外层类型的 `inline_schema` 为 false（或被 schemars 设置为引用模式）时，此类型的 schema 会被收进 `$defs` 并以 `SharedString` 为名被 `$ref` 引用。下游 schema 消费者（编辑器补全、校验器、大模型）就会看到一个额外的类型概念，且将来若重命名类型或更换内层实现，schema 发生破坏性变化。委托 `String` 则把 schema 契约与类型名解耦。

**练习 3**：为什么 `json_schema` 的参数是 `&mut SchemaGenerator`，而 `inline_schema`、`schema_name` 没有参数？

**答案**：`inline_schema`/`schema_name` 是关于类型的静态元信息，不依赖生成过程；`json_schema` 是真正「生成」的动作，生成器要在遍历过程中登记定义、处理递归引用，因此需要可变访问。理解这个分工有助于阅读 schemars 的其他实现。

### 4.4 生态连接：从 SharedString 到 Zed 的 settings.json

#### 4.4.1 概念说明

前三个模块讲了「怎么做」，这个模块回答「谁在用」。`SharedString` 的三个序列化 impl 不是孤芳自赏，它们支撑着 Zed 的三条真实链路：

1. **设置系统**：`ThemeSettings` 里 `agent_ui_font_family`、`markdown_preview_font_family` 等字段的类型是 `Option<SharedString>`，用户写在 `settings.json` 里的字符串经 `Deserialize` 进入程序；设置编辑器用来校验与补全的 JSON Schema 经 `JsonSchema` 生成。
2. **样式系统**：`gpui::TextStyle` 的 `font_family` 字段是 `SharedString`，其精化类型携带 `Serialize`/`Deserialize`/`JsonSchema`，UI inspector 据此生成样式 schema。
3. **工具协议**：MCP 工具与 agent 工具的 `Input` 类型要求实现 `JsonSchema`，生成的 schema 随工具定义发给外部进程或大模型。

把本讲三个 impl 放在一起看，「透明三件套」各司其职：

| impl | JSON 世界里的角色 | 关键一行 |
| --- | --- | --- |
| `Serialize` | 写出去是字符串 | `serializer.serialize_str(self.as_ref())` |
| `Deserialize` | 读进来按 String | `String::deserialize(deserializer)?` |
| `JsonSchema` | 自我描述是 string | `String::json_schema(generator)` |

#### 4.4.2 核心流程

以「设置编辑器里 agent 面板字体设置」为例的完整链路：

```text
用户 settings.json 写入 "agent_ui_font_family": "Fira Code"
  └─ 设置解析：ThemeSettings 的内容结构逐字段反序列化
      └─ SharedString::deserialize                    ← 4.2 的手写 impl
          └─ String::deserialize → SharedString::new
  └─ 程序内：ThemeSettings::agent_ui_font_family() 返回 &SharedString 供渲染使用

设置编辑器打开时：
  SettingsStore::json_schema(params)
  └─ root_schema_for::<UserSettingsContent>()         ← schemars 遍历全部设置类型
      └─ 遇到 Option<SharedString> 字段
          └─ SharedString::json_schema → String 的 {"type":"string"}
  └─ 该 schema 驱动编辑器的校验与自动补全
```

#### 4.4.3 源码精读

**设置侧的 `SharedString` 字段**。[crates/theme_settings/src/settings.rs:55-68](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/theme_settings/src/settings.rs#L55-L68) 定义了 `ThemeSettings` 中一组 `Option<SharedString>` 字段（agent UI 字体、agent 缓冲区字体、markdown 预览字体等）——这些就是 `settings.json` 里对应键的反序列化目标类型。

**schema 生成入口**。[crates/settings/src/settings_store.rs:1266-1274](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/settings/src/settings_store.rs#L1266-L1274) 是 `SettingsStore::json_schema` 的开头：用 draft2019-09 设置构造 `schemars::SchemaGenerator`，先让 `UserSettingsContent` 登记全部设置类型；[crates/settings/src/settings_store.rs:1313-1316](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/settings/src/settings_store.rs#L1313-L1316) 最后以 `root_schema_for::<UserSettingsContent>()` 收尾产出完整 schema。过程中遇到的每个 `SharedString` 字段都经 4.3 的 impl 变成 `{"type":"string"}`。

有意思的是，schema 生成器的参数本身就装着 `SharedString`：[crates/settings/src/settings_store.rs:275-285](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/settings/src/settings_store.rs#L275-L285) 的 `SettingsJsonSchemaParams` 里，已安装主题名与图标主题名的类型正是 `&'a [SharedString]`——它们随后被用来把主题类设置的 schema 收窄成字符串枚举（[crates/settings/src/settings_store.rs:1284-1291](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/settings/src/settings_store.rs#L1284-L1291)）。注意这个收窄发生在 settings 层的 `replace_subschema`，而不是改 `SharedString` 本身的 schema——通用基础类型保持「任意字符串」，需要枚举约束的是具体设置项，分层清晰。

**UI 渲染侧**。[crates/settings_ui/src/settings_ui.rs:532-535](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/settings_ui/src/settings_ui.rs#L532-L535) 为设置表单注册各类型的渲染器，其中 `SharedString` 与 `String` 一样交给 `render_text_field`——序列化世界的透明性延续到了设置 UI：`SharedString` 字段就是一个文本输入框。

**样式系统**。[crates/gpui/src/style.rs:435-443](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/style.rs#L435-L443) 的 `TextStyle` 以 `font_family: SharedString` 携带字体族，其 `#[refineable(Debug, PartialEq, Serialize, Deserialize, JsonSchema)]` 标注把序列化与 schema 能力交给宏生成的精化类型；[crates/gpui/src/style.rs:294-296](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/style.rs#L294-L296) 显示 `StyleRefinement` 聚合了 `TextStyleRefinement` 字段。于是 [crates/json_schema_store/src/json_schema_store.rs:570-578](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/json_schema_store/src/json_schema_store.rs#L570-L578) 为 UI inspector 生成 `gpui::StyleRefinement` 的 schema 时，`font_family` 经由本讲的 impl 呈现为字符串类型。

**工具协议**。schemars 在 Zed 里不只服务设置：[crates/context_server/src/listener.rs:87-92](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/context_server/src/listener.rs#L87-L92) 为 MCP 工具的 `Input` 生成 `input_schema`（随工具定义发给外部进程）；[crates/agent/src/thread.rs:5088-5094](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/agent/src/thread.rs#L5088-L5094) 要求 agent 工具的 `Input` 实现 `Deserialize + Serialize + JsonSchema` 并用 `schemars::schema_for!` 生成描述。任何工具输入结构里的 `SharedString` 字段，同样经本讲的三个 impl 表现为普通字符串。

#### 4.4.4 代码实践

**实践目标**：用源码阅读的方式走通「settings.json 的一个字符串键 → schema 里的 string 类型」这条链路，不依赖编译 Zed（完整构建较重）。

**操作步骤**：

1. 打开 [crates/theme_settings/src/settings.rs:55-68](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/theme_settings/src/settings.rs#L55-L68)，选一个字段（如 `agent_ui_font_family`），记下它的类型 `Option<SharedString>`。
2. 跳到 [crates/settings/src/settings_store.rs:1266](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/settings/src/settings_store.rs#L1266)，通读 `json_schema` 函数到 L1316，找出两处关键调用：`UserSettingsContent::json_schema(&mut generator)`（L1272）与 `root_schema_for::<UserSettingsContent>()`（L1314）。
3. 回到 [crates/gpui_shared_string/gpui_shared_string.rs:42-54](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_shared_string/gpui_shared_string.rs#L42-L54)，确认 `Option<SharedString>` 字段最终经由 `String::json_schema` 得到 `{"type":"string"}`（`Option` 只是在外面包一层 nullable 语义）。
4. 画出这条链路的调用图（参考 4.4.2 的格式），并在旁边标注每一步分别属于哪个 crate。

**需要观察的现象**：三个 crate（`theme_settings`、`settings`、`gpui_shared_string`）如何通过 serde/schemars 的 trait 约定协作，而没有直接函数调用。

**预期结果**：你得到的链路图中，`gpui_shared_string` 只出现一次（最后的 `JsonSchema` impl），其余环节都只「依赖类型」而不「依赖实现」——这正是 trait 作为扩展点的价值。若想进一步验证（可选、较重）：在本机完整构建 Zed 后打开设置编辑器，对 `agent_ui_font_family` 触发补全，观察候选值为字符串（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：主题名（`ThemeName`）的 schema 被 `replace_subschema` 收窄成字符串枚举，为什么这个收窄不放进 `gpui_shared_string`？

**答案**：因为「合法主题名」依赖运行时安装了哪些主题，是 settings 层的动态语义；而 `SharedString` 是跨模块通用基础类型，它的 schema 语义就是「任意字符串」。把动态收窄放进基础类型会污染所有其他使用场景（比如工具输入里的自由文本字段）。

**练习 2**：`SettingsJsonSchemaParams` 里 `theme_names` 的类型是 `&[SharedString]` 而语言名是 `&[String]`，这暗示了什么？

**答案**：主题名在程序内被长期保存并作为廉价克隆的共享值传递（主题注册表的生命周期），所以选 `SharedString`；语言名在此处只做一次性拼接，`String` 足够。这类「同处一个结构、按使用方式选型」的细节是选型的活教材——类型选择跟随数据的生命周期与克隆频率（呼应 u2-l1 的成本分析）。

**练习 3**：MCP 工具 schema（`context_server`）与设置 schema（`settings_store`）对 schemars 的用法有何不同？

**答案**：设置 schema 用 draft2019-09 并叠加自定义 transform（`DefaultDenyUnknownFields`、`AllowTrailingCommas`），服务于编辑器校验与补全；MCP 工具 schema 用 draft07 且设置 `inline_subschemas = true`（避免 `$ref`，因为外部进程没有上下文解析引用），服务于跨进程协议。同一套 `JsonSchema` impl 能同时喂饱两种场景，正是「委托 `String`」这种最朴素形态的优势——不带任何私有结构。

## 5. 综合实践

把 4.1–4.3 的碎片拼成一个完整的小程序（示例代码）。在 4.1.4 的项目里，把 `src/main.rs` 替换为：

```rust
use gpui_shared_string::SharedString;
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

#[derive(Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
struct Config {
    name: SharedString,
    tags: Vec<SharedString>,
}

fn main() {
    // 1. 构造：String 与 &str 两条 From 路径（u2-l3）
    let config = Config {
        name: "Zed".into(),
        tags: vec![String::from("editor").into(), "rust".into()],
    };

    // 2. 序列化：应为纯字符串值的 JSON
    let json = serde_json::to_string_pretty(&config).unwrap();
    println!("序列化结果:\n{json}");

    // 3. 反序列化：往返后与原值相等
    let parsed: Config = serde_json::from_str(&json).unwrap();
    assert_eq!(parsed, config);
    println!("往返一致 ✓");

    // 4. schema：name 与 tags.items 都应是 string，且不出现 SharedString 字样
    let schema = schemars::schema_for!(Config);
    let schema_json = serde_json::to_string_pretty(&schema).unwrap();
    println!("JSON Schema:\n{schema_json}");
    assert!(!schema_json.contains("SharedString"));
    println!("schema 中无 SharedString 痕迹 ✓");
}

#[test]
fn rejects_non_string_value() {
    let err = serde_json::from_str::<Config>(r#"{"name": 123, "tags": []}"#).unwrap_err();
    // 错误应来自 String 的类型校验：整数不是字符串
    assert!(err.to_string().contains("invalid type"));
}
```

**操作步骤**：

1. `cargo run` 观察三段输出：JSON 文本、往返断言、schema 文本。
2. `cargo test` 验证类型校验复用 `String` 语义。
3. 附加实验：把 `name` 字段类型改成 `String` 再跑一次，对比两份输出应完全一致——这就是「透明」的可证伪表述。

**预期结果**：

- JSON 中 `name` 与 `tags` 的元素都是普通字符串值；
- 往返断言与 `!contains("SharedString")` 断言通过；
- 与 `String` 版本的输出逐字节一致（具体 schema 形态随 schemars draft 设置略有差异，待本地验证）。

## 6. 本讲小结

- `Serialize` 是一行 `serializer.serialize_str(self.as_ref())`：借出 `&str` 视图、零分配、格式无关，输出与 `String` 不可区分；手写而非 derive，把「序列化为字符串」定成不随内层实现（SmolStr 可替换）漂移的公共契约。
- `Deserialize` 采用「委托 `String` 再 `new` 构造」的两行模式：错误语义与格式支持完全继承 `String`，代价是 \( 1 \) 次中间 `String` 分配加一次字节复制——清晰优先的取舍。
- `JsonSchema` 因 `SmolStr` 无现成实现而必须手写，三个方法（`inline_schema`/`schema_name`/`json_schema`）全部委托 `String`，让 schema 中根本不出现 `SharedString` 概念。
- 三个 impl 合成「透明三件套」，与 u2-l2 的 `Deref`/`AsRef`/`Borrow` 一样，都是刻意维护的「我就是字符串」契约。
- 下游真实链路：`ThemeSettings` 的 `Option<SharedString>` 设置字段、`gpui::TextStyle.font_family`、MCP 与 agent 工具的输入 schema，全部经由这三个 impl 以普通字符串形态出现在 JSON 世界。
- Zed 内部对 schemars 的两种用法对照（设置 schema 用 draft2019-09 加 transform，MCP 工具 schema 用 draft07 加内联）说明：最朴素的 schema 形态才有最广的兼容性。

## 7. 下一步学习建议

本讲把 `SharedString` 的全部源码（212 行）讲完了：构造与读取（u1-l2）、封装与存储（u2-l1）、代理 trait（u2-l2）、转换矩阵（u2-l3）、比较语义（u2-l4）、序列化三件套（本讲）。下一讲 **u3-l2「在 GPUI 中渲染：SharedString 作为 UI 元素」** 将走出这个 crate，精读 `crates/gpui/src/elements/text.rs` 中 `impl Element for SharedString`——看这个字符串类型如何直接成为 UI 文本元素、`.child("文本")` 背后的转换链，以及 `request_layout` 里那次 `self.clone()` 为何可以接受（答案就藏在 u2-l1 的 O(1) 克隆里）。

继续阅读的建议路径：

1. `crates/settings/src/settings_store.rs` 的 `json_schema` 全函数——看 transform（`DefaultDenyUnknownFields`、`AllowTrailingCommas`）如何后处理 schemars 的输出；
2. `crates/gpui/src/style.rs` 的 `#[refineable]` 用法——宏如何为类型生成携带 serde/JsonSchema 的精化结构；
3. `crates/language_model_core/src/tool_schema.rs`——schema 如何按大模型格式（如 Claude 的 tool_use）被进一步改写。
