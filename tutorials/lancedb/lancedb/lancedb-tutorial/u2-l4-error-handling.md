# 错误处理：Error 与 Result

## 1. 本讲目标

本讲只聚焦一个最小模块：**error（错误处理）**。读完本讲后，你应该能够：

1. 说清楚 LanceDB 为什么不用 `String` 或 `Box<dyn Error>` 来报错，而是定义一个自己的 `Error` 枚举。
2. 看懂 `rust/lancedb/src/error.rs` 中各类错误变体分别代表什么场景。
3. 理解 `pub type Result<T> = std::result::Result<T, Error>;` 这条类型别名的作用。
4. 掌握「直接构造变体」「`From` 转换 + `?` 传播」「按变体 `match`」这三种在源码里反复出现的错误处理模式。
5. 能在自己的代码里故意触发一次错误，并判断它属于哪个变体。

本讲承接 [u1-l1 项目总览与定位](u1-l1-project-overview.md)：你已经知道 LanceDB 是「一个 Rust 核心 + 多语言薄绑定」的架构。本讲进入 Rust 核心最底层的横切关注点之一——错误，它被前面讲过的 connection、table、query 等几乎所有模块依赖。

## 2. 前置知识

在进入源码前，先用通俗语言铺垫几个 Rust 与错误处理的基础概念。如果你已经熟悉，可以跳到第 3 节。

### 2.1 `Result<T, E>` 与「失败也是值」

Rust 没有 `try/throw` 异常机制。函数如果可能失败，它的返回值就是一个 `Result` 枚举：

```rust
enum Result<T, E> {
    Ok(T),   // 成功，携带值 T
    Err(E),  // 失败，携带错误 E
}
```

「成功」和「失败」都是普通的值，编译器强制你处理 `Err` 分支。这比异常更可控，但也带来一个问题：**错误类型 `E` 该怎么设计？**

### 2.2 为什么不直接用 `String` 报错

最偷懒的做法是让 `E = String`，出错时返回一段描述文字。但这样做有三个致命缺点：

- **无法区分错误种类**：调用方拿到一个字符串，很难写代码判断「这是表不存在，还是参数非法」。字符串匹配（`msg.contains("not found")`）既脆弱又丑陋。
- **丢失原始错误链**：底层 `lance::Error`、`arrow_schema::ArrowError` 被转成字符串后，原始的堆栈/上下文信息就丢了。
- **无法携带结构化字段**：比如「表已存在」这个错误，你自然想带上表名 `name`，字符串做不到。

所以库的通行做法是定义一个**自定义枚举**，每个变体代表一类错误，并可以在变体里携带结构化字段（表名、路径、底层 source 等）。

### 2.3 `?` 运算符与 `From` 转换

`?` 是 Rust 错误传播的语法糖。下面两段代码等价：

```rust
// 写法 A：手动 match + return
let v = match may_fail() {
    Ok(v) => v,
    Err(e) => return Err(e),
};

// 写法 B：用 ?
let v = may_fail()?;
```

`?` 还有一个隐藏能力：如果当前函数的返回错误类型是 `MyError`，而被调函数返回的错误是 `OtherError`，只要存在 `impl From<OtherError> for MyError`，`?` 就会**自动调用 `From` 把错误转换过去**。这正是 LanceDB 用大量 `From` 实现来「统一」各家第三方错误的关键，第 4.3 节会详细讲。

### 2.4 `snafu` 是什么

`snafu` 是 Rust 生态里一个错误处理库，通过过程宏（`#[derive(Snafu)]`）自动为你的枚举生成：

- 每个变体的 `Display` 实现（错误打印成什么样的人话）。
- 每个变体的「上下文选择器（context selector）」，可以用 `.context()?` 风格优雅地给错误附加上下文。
- 可选的 `whatever` 宏，用于快速生成「带消息」的临时错误。

LanceDB 的 `Error` 就是用 `snafu` 派生的，所以你会看到 `#[snafu(display("..."))]` 这样的标注。

## 3. 本讲源码地图

本讲涉及的关键文件及其作用：

| 文件 | 作用 |
|---|---|
| [rust/lancedb/src/error.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/error.rs) | **核心**。定义 `Error` 枚举、`Result` 别名、`BoxError` 类型，以及一堆 `From` 转换实现。本讲 90% 的内容都围绕它。 |
| [rust/lancedb/src/lib.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs) | 在根部 `pub use error::{Error, Result};` 把错误类型重新导出为 crate 级公开 API。 |
| [rust/lancedb/src/connection.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs) | 连接模块，是触发 `InvalidInput`、`TableNotFound` 的典型现场。 |
| [rust/lancedb/src/database/listing.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/listing.rs) | 本地后端实现，演示 `TableAlreadyExists`、`TableNotFound` 如何被构造和如何被 `match` 处理。 |
| [rust/lancedb/src/utils/mod.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/utils/mod.rs) | 表名校验，触发 `InvalidTableName`。 |

## 4. 核心概念与源码讲解

本讲把 error 模块拆成 4 个递进的小模块来讲解。

### 4.1 Result 类型别名：为什么需要统一的错误类型

#### 4.1.1 概念说明

LanceDB 是一个库（library），会被 Python/Node/Java 绑定以及其他 Rust 项目调用。库的作者希望所有公开 API 返回的失败都用**同一种错误类型**，这样：

- 调用方只需 `match` 一种类型，API 一致性好。
- 错误信息统一、可序列化、可分类。
- 第三方底层错误（Arrow、Lance、object_store、HTTP 等）可以被「吸收」进同一个类型里。

为此 LanceDB 先定义了一个类型别名 `BoxError`，再定义主类型 `Error`，最后给出 `Result` 别名。

#### 4.1.2 核心流程

整个错误体系从下到上分三层：

```text
第三方的各种错误                LanceDB 自定义 Error 枚举         Result<T> 别名
(ArrowError / lance::Error      （按场景分几十个变体）            = Result<T, Error>
 / object_store::Error ...)              ▲                              ▲
        │                                 │ From 转换                    │
        └──────────►  from_box_error  ────┘                              │
                                   （解包/归一）                          │
                                                                         │ 所有公开 API 用它
```

任何底层错误最终都会被「翻译」成 `Error` 的某个变体；所有公开函数返回 `Result<T>`（即 `std::result::Result<T, Error>`）。

#### 4.1.3 源码精读

先看最顶层的类型别名与导出：

[rust/lancedb/src/error.rs:10](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/error.rs#L10) 定义了 `BoxError`——一个「任意可发送的错误」的盒子，用于存放那些不需要细分的第三方错误：

```rust
pub(crate) type BoxError = Box<dyn std::error::Error + Send + Sync>;
```

`Send + Sync` 约束保证它可以在异步运行时（tokio）的多个线程间传递。

[rust/lancedb/src/error.rs:95](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/error.rs#L95) 给出 crate 级 `Result` 别名：

```rust
pub type Result<T> = std::result::Result<T, Error>;
```

有了它，库内的函数签名就能写成 `async fn open_table(...) -> Result<Table>`，而不是冗长的 `Result<Table, lancedb::Error>`。

[rust/lancedb/src/lib.rs:194](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs#L194) 把这两个名字重新导出，让外部可以直接写 `lancedb::Error`、`lancedb::Result`：

```rust
pub use error::{Error, Result};
```

#### 4.1.4 代码实践

**实践目标**：确认 `Result` 别名确实指向 `Error`，并体会「类型别名」带来的简洁。

**操作步骤**：

1. 在仓库根目录运行 `cargo check --features remote --tests`（参考 [u1-l2 仓库结构、技术栈与构建运行](u1-l2-repo-build-run.md)）确保能编译。
2. 在 `rust/lancedb/src/` 下随便挑一个公开异步函数（例如 `connection.rs` 里的 `execute`），观察它的签名结尾是 `-> Result<...>`。
3. 在你自己的临时测试里写一行，故意把类型写全做对比：

```rust
// 示例代码：仅用于理解别名，不必加入项目
fn _demo() -> lancedb::Result<()> {
    // 下面两行等价，体会 Result 别名的简洁
    let _a: lancedb::Result<()> = Ok(());
    let _b: std::result::Result<(), lancedb::Error> = Ok(());
    Ok(())
}
```

**需要观察的现象**：编译能通过，说明 `Result<()>` 就是 `std::result::Result<(), Error>` 的缩写。

**预期结果**：编译通过；你会直观感受到「别名 = 省去重复书写固定错误类型」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `pub type Result<T>` 这一行删掉，仓库里大量函数签名会怎样？

**参考答案**：所有写 `-> Result<T>` 的地方都会编译报错，提示找不到 `Result`。因为它们依赖的就是这条别名，别名只是「缩写」，背后真正的类型是 `std::result::Result<T, Error>`。

**练习 2**：为什么 `BoxError` 要加 `Send + Sync` 约束？

**参考答案**：LanceDB 大量使用异步（tokio），错误经常要跨 `await` 点、跨任务/线程传递。`Send + Sync` 保证这个装箱错误能安全地在多线程间移动与共享，否则无法被存进 `Error` 并从异步函数返回。

---

### 4.2 Error 枚举全景：变体分类

#### 4.2.1 概念说明

`Error` 是一个由 `snafu` 派生的枚举，每个变体代表一类典型错误场景。理解这些变体，等于理解了 LanceDB 「会以哪些方式失败」。我们按用途把它们分成四大类：

1. **输入/参数错误**：用户传错了东西（非法表名、缺参、非法 SQL）。
2. **资源状态错误**：表/数据库/索引的存在性冲突（不存在、已存在）。
3. **运行时/系统错误**：目录创建失败、超时、内部运行错误、不支持的操作。
4. **第三方错误透传**：把 Arrow、Lance、object_store、HTTP 等外部错误包进自己的枚举。

#### 4.2.2 核心流程

枚举本身只是一个「标签 + 数据」的容器。当某个函数检测到错误条件时，它就构造一个对应的变体返回；调用方拿到 `Err(Error::某变体)` 后，可以：

- 直接向上传播（`?`）。
- 按变体 `match`，对特定错误做特殊处理（例如「表已存在就改用打开」）。
- 打印给用户看（`Display` 实现决定文字）。

每个变体上的 `#[snafu(display("..."))]` 决定了它打印成人话时的格式，例如 `InvalidInput` 打印成 `Invalid input, {message}`。

#### 4.2.3 源码精读

[rust/lancedb/src/error.rs:12-93](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/error.rs#L12-L93) 是整个枚举定义。逐段看：

```rust
#[derive(Debug, Snafu)]
#[snafu(visibility(pub(crate)))]
pub enum Error {
```

`#[derive(Snafu)]` 让 snafu 自动生成 `Display`、`Error` 实现与上下文选择器；`#[snafu(visibility(pub(crate)))]` 表示这些自动生成的辅助构造器是 crate 内可见（控制 snafu 生成物的可见性，不影响你直接用结构体字面量构造变体）。

下面挑几个有代表性的变体看：

**输入/参数类**（[error.rs:15-18](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/error.rs#L15-L18)）：

```rust
#[snafu(display("Invalid table name (\"{name}\"): {reason}"))]
InvalidTableName { name: String, reason: String },
#[snafu(display("Invalid input, {message}"))]
InvalidInput { message: String },
```

`InvalidInput` 是最通用的「参数不对」错误，只有一个 `message`；`InvalidTableName` 更具体，额外带 `name` 和 `reason`。

**资源状态类**（[error.rs:19-31](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/error.rs#L19-L31)）：

```rust
#[snafu(display("Table '{name}' was not found"))]
TableNotFound { name: String, source: BoxError },
...
#[snafu(display("Table '{name}' already exists"))]
TableAlreadyExists { name: String },
```

注意 `TableNotFound` 带了一个 `source: BoxError`，用来保留「为什么认为它不存在」的底层原因（例如底层 Lance 抛了 `NotFound`）。

**运行时类**（[error.rs:32-42](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/error.rs#L32-L42)）：

```rust
CreateDir { path: String, source: std::io::Error },
Schema { message: String },
Runtime { message: String },
Timeout { message: String },
```

其中 `CreateDir` 直接把 `std::io::Error` 作为 `source`，保留 IO 错误链。

**第三方透传类**（[error.rs:44-93](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/error.rs#L44-L93)）：把 Arrow/Lance/object_store/HTTP 等错误直接装进来。其中 `Http`、`Retry` 是 `#[cfg(feature = "remote")]` 条件编译——只有在启用远程后端 feature 时才存在，这与 [u1-l2](u1-l2-repo-build-run.md) 讲的「远程后端是可选 feature」一致：

```rust
#[cfg(feature = "remote")]
#[snafu(display("Http error: (request_id={request_id}) {source}"))]
Http {
    #[snafu(source(from(reqwest::Error, Box::new)))]
    source: Box<dyn std::error::Error + Send + Sync>,
    request_id: String,
    status_code: Option<reqwest::StatusCode>,
},
```

最后两个特殊变体（[error.rs:84-92](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/error.rs#L84-L92)）值得注意：

```rust
/// External error pass through from user code.
#[snafu(transparent)]
External { source: BoxError },
#[snafu(whatever, display("{message}"))]
Other {
    message: String,
    #[snafu(source(from(Box<dyn std::error::Error + Send + Sync>, Some)))]
    source: Option<Box<dyn std::error::Error + Send + Sync>>,
},
```

- `External`：`transparent` 表示「透明包装」——它的 `Display`/`source` 直接用被包装错误的内容，常用于透传用户自定义嵌入函数抛出的错误。
- `Other`：`whatever` 变体，用于「带一条消息」的临时错误，可选地附带一个底层 source。

#### 4.2.4 代码实践

**实践目标**：把每个变体的「人话」打印出来，建立变体 ↔ 文字的直觉。

**操作步骤**：

1. 在 `rust/lancedb/tests/` 或一个临时 example 里，写一小段代码直接构造几个变体并打印：

```rust
// 示例代码：演示变体的 Display 输出，可放入 examples/error_display.rs
use lancedb::Error;

#[tokio::main]
async fn main() {
    let e1 = Error::InvalidInput { message: "missing region".into() };
    let e2 = Error::TableAlreadyExists { name: "my_table".into() };
    let e3 = Error::InvalidTableName {
        name: "bad name!".into(),
        reason: "Table names can only contain alphanumeric characters...".into(),
    };
    println!("{}", e1);
    println!("{}", e2);
    println!("{}", e3);
}
```

2. 注意：要在 `rust/lancedb/examples/` 下新建文件，并确保 `Error` 变体的构造在 crate 内可见（变体本身是 `pub`，可直接用字面量构造）。
3. 运行 `cargo run --example error_display --features remote`。

**需要观察的现象**：终端分别打印出 `Invalid input, missing region`、`Table 'my_table' already exists`、`Invalid table name ("bad name!"): Table names can only contain alphanumeric characters...`。

**预期结果**：输出与每个变体 `#[snafu(display(...))]` 模板完全对应，验证「变体 = 错误种类 + 结构化字段 + 人话模板」。

> 若不便于新建 example，可改为在第 4.4 节的「触发真实错误」实践中，把 `println!("{:?}", err)` 换成 `println!("{}", err)` 观察 `Display` 输出。

#### 4.2.5 小练习与答案

**练习 1**：`Http` 和 `Retry` 两个变体为什么用 `#[cfg(feature = "remote")]` 包起来？

**参考答案**：因为它们依赖 `reqwest::Error`、`reqwest::StatusCode`，这些类型只有在开启 `remote` feature 时才会被拉入依赖。本地（进程内）用户根本用不到 HTTP，没必要为它编译进一堆 HTTP 客户端代码。这是 [u1-l2](u1-l2-repo-build-run.md) 讲的「远程后端做成可选 feature」在错误定义上的体现。

**练习 2**：`TableNotFound` 和 `TableAlreadyExists` 这两个变体，谁需要带 `source`，为什么？

**参考答案**：`TableNotFound` 带 `source`，因为「表不存在」通常是底层存储/Lance 报出来的（如 `lance::Error::NotFound`），保留 `source` 能让调试时看到根因；而 `TableAlreadyExists` 往往是 LanceDB 自己在 create 模式下判断出来的，不需要底层 source，只带表名即可。

---

### 4.3 第三方错误转换：From 实现与 from_box_error 解包

#### 4.3.1 概念说明

LanceDB 内部大量调用 Arrow、Lance、DataFusion、object_store 等库，它们各有各的错误类型。如果不做转换，LanceDB 的每个函数都得 `match` 一堆不同的错误类型，签名也会五花八门。

Rust 的解决方案是：**为 `Error` 实现一系列 `From<第三方错误>`**。这样一来，借助 `?` 运算符的自动转换能力，底层错误会「无缝」变成 `lancedb::Error`，调用链保持整洁。

但简单的「直接包装」还不够——有时底层错误里其实**包着另一个 LanceDB 自己的 `Error`**（例如用户自定义的嵌入函数抛了 `lancedb::Error`，被 Arrow 包装成 `ArrowError::ExternalError`）。LanceDB 写了一个 `from_box_error` 函数，专门做「逐层解包」，尽量还原最原始的错误类型。

#### 4.3.2 核心流程

转换有两条路径：

```text
路径 A（已知类型，编译期 From）:
   ArrowError ──From<ArrowError>──► Error::Arrow { source }
   （若 ArrowError::ExternalError 则进路径 B 解包）

路径 B（运行期解包，from_box_error）:
   Box<dyn Error> ──► 依次尝试 downcast：
       · 自己的 Error？      → 拆出还原（含 External 再解一层）
       · lance::Error？       → Wrapped 则解包，否则转 Error::Lance
       · ArrowError？         → ExternalError 则解包，否则转 Error::Arrow
       · DataFusionError？    → Arrow/External 则解包，否则转 External
       · 都不匹配             → 包成 Error::External
```

核心思想：**能还原成具体类型就还原，不能还原就装进 `External`**，避免「层层包装后丢失原始信息」。

#### 4.3.3 源码精读

**简单的 `From` 实现**——以 `object_store::Error` 为例，[error.rs:168-172](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/error.rs#L168-L172)：

```rust
impl From<object_store::Error> for Error {
    fn from(source: object_store::Error) -> Self {
        Self::ObjectStore { source }
    }
}
```

有了它，任何返回 `object_store::Error` 的调用，在 LanceDB 函数里只需 `obj_store_op()?` 就能自动变成 `Error::ObjectStore`。

**带分支的 `From` 实现**——以 `ArrowError` 为例，[error.rs:97-104](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/error.rs#L97-L104)：

```rust
impl From<ArrowError> for Error {
    fn from(source: ArrowError) -> Self {
        match source {
            ArrowError::ExternalError(source) => Self::from_box_error(source),
            _ => Self::Arrow { source },
        }
    }
}
```

普通的 `ArrowError` 直接装进 `Error::Arrow`；但如果是 `ExternalError`（说明里面包了别的错误，很可能就是用户嵌入函数的错），就交给 `from_box_error` 去解包。

`From<lance::Error>`（[error.rs:118-127](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/error.rs#L118-L127)）也同理：底层的 `Wrapped` 和 `External` 变体会被解包，其余装进 `Error::Lance`。

**核心解包函数 `from_box_error`**，[error.rs:129-166](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/error.rs#L129-L166)。它是一连串「尝试向下转型（downcast）」：

```rust
fn from_box_error(mut source: Box<dyn std::error::Error + Send + Sync>) -> Self {
    source = match source.downcast::<Self>() {
        Ok(e) => match *e {
            // 自己的 Error 被包回来了 → 直接还原；若是 External 则再解一层
            Self::External { source } => return Self::from_box_error(source),
            other => return other,
        },
        Err(source) => source,
    };
    // 接着依次尝试 downcast::<lance::Error>()、ArrowError、DataFusionError ...
    // 都不匹配时：
    Self::External { source }
}
```

关键点是「递归解包」：如果剥出来的还是 `External`，就再调一次自己，直到露出真正的内核。`downcast` 失败（`Err`）就把原盒子传给下一个尝试。

> 小知识：`downcast` 是 Rust trait 对象（`dyn Error`）提供的运行期类型检查能力，本质是问「你实际是不是某个具体类型？」是就拿到具体值，不是就放回去。

#### 4.3.4 代码实践

**实践目标**：理解「直接 `?` 就能跨类型传播」的便利性。这是一个**源码阅读型实践**。

**操作步骤**：

1. 打开 [rust/lancedb/src/table.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs)，搜索形如 `.schema()` 或调用底层 `Dataset` 的地方。
2. 观察这些调用底层 Lance 的代码几乎都是直接用 `?`，而**没有显式 `map_err`**。例如调用 `dataset.count_rows(..)?` 时，`lance::Error` 会自动经 `From<lance::Error>` 变成 `lancedb::Error`。
3. 对比：如果没有 `From<lance::Error>`，每处都得写 `.map_err(Error::from)?`，代码会臃肿很多。

**需要观察的现象**：大量底层调用都靠 `?` 一带而过，错误类型在「无声」中被转换。

**预期结果**：你能找到至少一处 `lance::Error`（或 `ArrowError`）通过 `?` 自动变成 `lancedb::Result` 的调用点，并解释其背后是哪个 `From` 实现生效。

#### 4.3.5 小练习与答案

**练习 1**：假设用户自定义的嵌入函数返回了一个 `lancedb::Error::InvalidInput`，它会被 Arrow 包装成 `ArrowError::ExternalError`，最终被 LanceDB 的某段代码捕获。这个错误会被还原成 `Error::InvalidInput` 还是 `Error::Arrow`？为什么？

**参考答案**：会被还原成 `Error::InvalidInput`。因为 `From<ArrowError>` 遇到 `ExternalError` 会调用 `from_box_error`；该函数第一次 `downcast::<Self>()` 成功，发现是 `Error::InvalidInput`（非 `External`），直接返回 `other`，于是原始变体被还原，而不是被错误地包成 `Error::Arrow`。

**练习 2**：为什么 `from_box_error` 在解出 `External { source }` 后要「再调一次自己」？

**参考答案**：因为 `External` 只是一层透明包装，真正的错误还在它里面的 `source` 里。直接返回 `External` 等于没解包；递归调用自己可以继续往下剥，直到露出非 `External` 的内核。

---

### 4.4 错误的触发与传播：三种实战模式

#### 4.4.1 概念说明

定义好 `Error` 之后，更重要的是「在源码里如何使用它」。LanceDB 里反复出现三种模式，掌握它们就读懂了大半个错误处理代码：

1. **直接构造变体**：检测到条件不满足时，用结构体字面量 `Error::某变体 { 字段 }` 构造，常用 `ok_or` / `ok_or_else` 搭配。
2. **`From` + `?` 自动传播**：调用返回第三方错误的函数，直接 `?`。
3. **按变体 `match` 做差异化处理**：拿到错误后，对特定变体走特殊分支（如「表已存在就改用打开」）。

#### 4.4.2 核心流程

```text
检测错误条件 ──► Error::InvalidInput{..} / ok_or_else(|| Error::..)
                          │
                          ▼ return Err(...)
                  调用方收到 Err(Error::..)
                          │
            ┌─────────────┼─────────────┐
            ▼             ▼             ▼
        直接 ? 向上    map_err 改类型   match 按变体分流
       （From 自动转换）              （TableAlreadyExists? 走打开）
```

#### 4.4.3 源码精读

**模式 1：直接构造变体。** 连接云端时缺 `region`/`api_key` 会触发 `InvalidInput`，[connection.rs:921-926](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L921-L926)：

```rust
let region = options.region.ok_or_else(|| Error::InvalidInput {
    message: "A region is required when connecting to LanceDb Cloud".to_string(),
})?;
let api_key = options.api_key.ok_or_else(|| Error::InvalidInput {
    message: "An api_key is required when connecting to LanceDb Cloud".to_string(),
})?;
```

`ok_or_else` 把 `Option` 转成 `Result`：`None` 时用闭包构造的 `Error::InvalidInput`，`?` 再把它向上抛。

表名校验也是这个模式，[utils/mod.rs:87-103](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/utils/mod.rs#L87-L103)：空名或含非法字符时构造 `Error::InvalidTableName { name, reason }`。

**模式 2：`From` + `?` 自动传播。** 见 4.3 节，底层 Lance/Arrow 调用直接用 `?`，这里不再赘述。

**模式 3：按变体 `match`。** 这是最能体现「自定义枚举价值」的场景——根据错误种类做不同处理。[listing.rs:1061-1073](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/listing.rs#L1061-L1073) 在建表时，如果撞上「表已存在」，就改走「打开已有表」的逻辑：

```rust
match NativeTable::create_table(...).await {
    Ok(table) => Ok(Arc::new(table)),
    Err(Error::TableAlreadyExists { .. }) => {
        self.handle_table_exists(&request.name, ...).await
    }
    Err(err) => Err(err),
}
```

如果错误类型是 `String`，这里就只能写 `if msg.contains("already exists")`，既脆弱又丑。有了枚举，`Err(Error::TableAlreadyExists { .. })` 一行就说清楚了。

另一个例子是「把底层错误翻译成业务错误」：[listing.rs:733-741](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/listing.rs#L733-L741)，删除表时把底层的 `lance::Error::NotFound` 翻译成对用户更有意义的 `Error::TableNotFound`：

```rust
.map_err(|err| match err {
    lance::Error::NotFound { .. } => Error::TableNotFound {
        name: name.clone(),
        source: Box::new(err),
    },
    _ => Error::from(err),
})?;
```

注意这里的「兜底」`_ => Error::from(err)`：匹配不上的就交给 `From` 去处理，避免遗漏。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：故意触发两种典型错误（非法表名 + 重复表名），捕获并判断它们的 `Error` 变体类型，直观体会「自定义错误相比 `String` 的优势」。

**操作步骤**：

1. 在 `rust/lancedb/examples/` 下新建文件 `error_trigger.rs`（示例代码）：

```rust
// 示例代码：触发并匹配 LanceDB 错误
use lancedb::{connect, Error};

#[tokio::main]
async fn main() {
    let tmp = tempfile::tempdir().unwrap();
    let db = connect(tmp.path().to_str().unwrap())
        .execute()
        .await
        .unwrap();

    // 场景 1：非法表名（含空格）
    match db.create_table("bad name!", std::iter::empty::<arrow_array::RecordBatch>())
        .execute()
        .await
    {
        Ok(_) => println!("意外成功"),
        Err(e) => match &e {
            Error::InvalidTableName { name, reason } => {
                println!("[捕获 InvalidTableName] name={name}, reason={reason}");
            }
            other => println!("[捕获其他变体] {:?}", other),
        },
    }

    // 场景 2：重复建表
    // 先建一张合法表，再用默认 create 模式再建同名表
    // （此处省略首次建表代码，参考 examples/simple.rs）
    // 触发后应匹配到 Error::TableAlreadyExists { name }
}
```

2. 先完成场景 1 的编译运行。在 `Cargo.toml` 的 `[[example]]` 段确认 `tempfile`、`arrow_array` 已可用（参考 [u1-l3 第一个程序](u1-l3-first-program.md) 中 simple.rs 的依赖）。
3. 运行：`cargo run --example error_trigger --features remote`。

**需要观察的现象**：

- 场景 1 打印出 `[捕获 InvalidTableName] ...`，说明非法表名被精确分类为 `InvalidTableName` 变体。
- （若你补全场景 2）应打印出 `TableAlreadyExists`，且能取到其中的 `name` 字段。

**预期结果**：你能用 `match` 精确区分不同错误种类，并读取结构化字段（`name`、`reason`）。这正是自定义枚举相对 `String` 的核心优势：**可分类、可携带结构化数据、匹配由编译器保证穷尽**。

> 如果你暂时不方便编译运行 example，可改为**阅读型实践**：打开 [database/namespace.rs:1232-1265](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/namespace.rs#L1232-L1265) 中的测试，那里有 `Err(Error::TableNotFound { name, .. }) => ...` 和 `Err(other) => panic!("Expected TableNotFound, got: {:?}", other)` 的断言，展示了项目自己如何「断言错误变体」。这等价于运行实践的预期结果。

#### 4.4.5 小练习与答案

**练习 1**：为什么 [listing.rs:733-741](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/listing.rs#L733-L741) 里要写 `_ => Error::from(err)` 作为兜底，而不是只处理 `NotFound`？

**参考答案**：因为删除目录时除了「不存在」之外，还可能因为权限、IO、网络（云存储）等原因失败。只处理 `NotFound` 会导致其他错误无人处理、编译报「match 不穷尽」（因为 `err` 不是 `Error` 枚举而是 `lance::Error`，实际是逻辑遗漏）。`_ => Error::from(err)` 把所有未特判的错误统一交给 `From` 转换，既保证穷尽，又不丢信息。

**练习 2**：请用一句话总结「自定义 Error 枚举相比返回 `String`」的三大好处。

**参考答案**：①可按变体精确分类与 `match`，由编译器保证穷尽；②每个变体能携带结构化字段（表名、路径、底层 source）；③通过 `From` 实现保留并串联原始错误链，便于诊断根因。

## 5. 综合实践

把本讲四个小模块串起来，完成下面这个「错误处理侦探」任务：

1. **触发**：写一段代码，依次触发 `InvalidInput`（如用 `db://` 云端 URI 但不给 `api_key`/`region`，参考 [connection.rs:921-926](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L921-L926)）与 `InvalidTableName`（用含空格的表名，参考 [utils/mod.rs:87-103](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/utils/mod.rs#L87-L103)）。
2. **分类**：用 `match` 把两个错误分别匹配到正确的变体，打印变体名。
3. **追踪链路**（阅读型）：对其中任意一个错误，沿调用链回溯——它是「直接构造（模式 1）」还是「`From` 转换（模式 2）」产生的？底层有没有更原始的 `source`？
4. **反思**：如果错误类型是 `String`，你刚才的「分类」和「取字段」还能这么写吗？把对照写进一句话总结。

> 提示：场景 1（云端缺 api_key）需要 `--features remote` 才能走到 `connection.rs` 那段代码；不开 remote 时用 `db://` 会得到 `Error::Runtime`（因为远程模块未编译）。这本身就是一个值得记录的观察。

## 6. 本讲小结

- LanceDB 用 `pub type Result<T> = std::result::Result<T, Error>;`（[error.rs:95](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/error.rs#L95)）让所有公开 API 失败类型统一，并由 [lib.rs:194](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs#L194) 导出为 `lancedb::Error` / `lancedb::Result`。
- `Error` 是 `snafu` 派生的枚举（[error.rs:12-93](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/error.rs#L12-L93)），变体分四类：输入/参数、资源状态、运行时/系统、第三方透传；`Http`/`Retry` 受 `remote` feature 条件编译控制。
- 一系列 `From<第三方错误>` 实现（Arrow/Lance/DataFusion/object_store 等）让 `?` 能自动转换错误类型，调用链保持整洁。
- `from_box_error`（[error.rs:129-166](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/error.rs#L129-L166)）通过 `downcast` 逐层解包，尽量还原被层层包装的原始错误（如用户嵌入函数抛的 `Error` 被 Arrow 包回的情况）。
- 源码中三种典型用法：直接构造变体（`ok_or_else(|| Error::..)`）、`From` + `?` 自动传播、按变体 `match` 做差异化处理（如「表已存在就改用打开」）。
- 自定义枚举相比 `String` 的核心优势：可分类、可携带结构化字段、匹配由编译器保证穷尽、并保留原始错误链。

## 7. 下一步学习建议

本讲讲清楚了「错误如何被定义、转换、传播」。接下来建议：

- 进入 [u3 查询与搜索](u3-l1-query-base-select.md) 系列，你会看到 `query` / `nearest_to` 等大量返回 `Result` 的 API，届时可以回头检验本讲的错误传播模式。
- 关注 [u5-l2 数据增删改与模式演化](u5-l2-crud-schema-evolution.md)，其中 `merge_insert` 的 upsert 流程会用到本讲的 `TableAlreadyExists` / `TableNotFound` 的 `match` 模式。
- 进阶可阅读 [u7-l1 Python 绑定](u7-l1-python-bindings.md)，看看 Rust 的 `Error` 是如何被 PyO3 转换成 Python 异常对外抛出的——那是错误跨越语言边界的最后一环。
