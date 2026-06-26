# 连接 Connection 与 ConnectBuilder

## 1. 本讲目标

连接（Connection）是使用 LanceDB 的第一步：你必须先拿到一个 `Connection`，才能建表、查表、搜索。本讲围绕 `rust/lancedb/src/connection.rs` 这一个文件，把「连接是怎么建立起来的」讲透。学完后你应该能够：

- 说出 `connect()` 入口函数与 `ConnectBuilder` 构建器之间的关系；
- 用 `api_key`、`region`、`storage_options`、`read_consistency_interval` 等方法配置一个连接；
- 根据 URI 的前缀（本地路径 / `s3://` / `gs://` / `db://`）判断它会走本地后端还是远程后端；
- 理解 `Connection` 作为一个轻量「数据库句柄」是如何委托到底层 `Database` trait 实现的。

本讲只覆盖一个最小模块：**connection**。

## 2. 前置知识

在学习本讲前，建议你已经读完 **u1-l3（第一个程序）**，对 `connect → create_table → query → delete` 这条主线有整体印象。本讲会用到以下几个基础概念：

- **Builder 模式（构建器模式）**：Rust 里常见的 API 设计。凡是有副作用（比如真正发起网络/磁盘 IO）的操作，往往不会直接用一堆参数调用，而是先返回一个「构建器」对象，让你链式地 `.方法()` 配置，最后再 `.execute().await` 真正执行。LanceDB 几乎所有 IO 操作都遵循这个套路。
- **trait 对象（`Arc<dyn Database>`）**：`Connection` 内部并不直接持有某个具体类型，而是持有一个 `Arc<dyn Database>`，即「某个实现了 `Database` trait 的对象的智能指针」。这样本地后端和远程后端可以塞进同一个外壳，对外暴露统一的接口。
- **URI（统一资源标识符）**：形如 `s3://bucket/path`、`db://mydb`、`/tmp/data` 的字符串，用来指明数据库「在哪里」。
- **feature flag（特性开关）**：LanceDB 的远程后端是可选功能，需要在编译时开启 `remote` feature。详见 u1-l2。

> 提示：如果你对 trait 对象和 Builder 模式还不熟，没关系——本讲会用最直白的方式说明它们在这里起了什么作用。

## 3. 本讲源码地图

本讲几乎全部围绕下面这一个文件展开，辅以少量周边文件：

| 文件 | 作用 |
| --- | --- |
| [rust/lancedb/src/connection.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs) | **核心**。定义 `connect()` 入口、`ConnectBuilder`、`ConnectRequest`、`Connection` 句柄，以及本地/远程分流逻辑。 |
| [rust/lancedb/src/lib.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs) | 把 `connect`、`Connection` 等符号 re-export 到 crate 根，使用户能写 `lancedb::connect(...)`。 |
| [rust/lancedb/src/remote/db.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/db.rs) | 定义远程选项键名常量（`OPT_REMOTE_API_KEY` 等），`api_key`/`region` 最终存到这些键里。 |
| [rust/lancedb/src/database.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database.rs) | 定义 `Database` trait（`Connection` 内部委托的目标）与 `ReadConsistency` 枚举。 |
| [rust/lancedb/src/database/listing.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/listing.rs) | 本地后端 `ListingDatabase` 的实现，`execute()` 在非远程路径下会调用它。 |
| [rust/lancedb/examples/simple.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/examples/simple.rs) | 最小可运行示例，演示 `connect().execute()` 与 `table_names()` 的真实用法。 |

## 4. 核心概念与源码讲解

本讲只讲一个最小模块 **connection**，但把它拆成四个递进的小节：先看入口与构建器，再看 URI 如何分流到不同后端，再看 `Connection` 句柄本身，最后看凭证与存储选项如何透传。

### 4.1 connect() 入口与 ConnectBuilder 构建器模式

#### 4.1.1 概念说明

在 u1-l3 里你已经见过这样的代码：

```rust
let db = connect("data/sample-lancedb").execute().await?;
```

这里 `connect(...)` 并没有真正建立连接，它只是返回一个 **构建器** `ConnectBuilder`。你可以在 `.execute()` 之前继续链式地追加配置（比如 `api_key`、`storage_options`），最后调用 `.execute().await` 才真正发起连接、拿到 `Connection`。

这种「先收集配置、最后一口气执行」的设计有两个好处：

1. **可演进**：以后新增配置项时，只需给 `ConnectBuilder` 加一个方法，调用方代码不用大改（相比一长串位置参数友好得多）。
2. **延迟执行**：所有真正有副作用的操作（建客户端、解析 URI、可能的网络握手）都集中在 `execute()` 里，调用链清晰、易于测试。

#### 4.1.2 核心流程

用伪代码描述 `connect → ConnectBuilder → Connection` 的过程：

```
connect(uri)                     # 1. 入口函数，返回 ConnectBuilder
  └─ ConnectBuilder::new(uri)    #    内部装一个 ConnectRequest（初始全默认）

builder                          # 2. 链式配置，每步都返回 self
  .api_key(...)                  #    把凭证写进 request.options
  .region(...)                   #    把区域写进 request.options
  .storage_options(...)          #    把存储选项写进 request.options
  .read_consistency_interval(...)#    设置读取一致性间隔

builder.execute().await          # 3. 根据 URI 前缀分流
  ├─ uri 以 "db" 开头 ──────────► execute_remote()  → RemoteDatabase   (需 remote feature)
  ├─ manifest_enabled == true ──► ListingDatabase::connect_manifest_enabled_namespace_database
  └─ 否则（本地/对象存储）──────► ListingDatabase::connect_with_options
  ⇒ 统一包装成 Connection
```

#### 4.1.3 源码精读

入口函数 `connect` 非常薄，只是 `ConnectBuilder::new` 的语法糖，并把它 re-export 到 crate 根：

[connection.rs:986-988](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L986-L988) —— 定义 `connect(uri)`，直接构造一个 `ConnectBuilder`。

[lib.rs:338](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs#L338) 与 [lib.rs:193](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs#L193) —— 把 `connect` 与 `Connection` 暴露到 crate 根，所以用户能直接写 `lancedb::connect(...)`、`lancedb::Connection`。

`ConnectBuilder` 内部其实只持有两样东西：一个待执行的 `ConnectRequest`，以及一个可选的自定义嵌入注册表：

[connection.rs:666-670](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L666-L670) —— `ConnectBuilder` 结构体定义，字段是 `request` 和 `embedding_registry`。

`ConnectBuilder::new` 把请求初始化成全默认值，关键字段包括 `uri`、`options`（一个 `HashMap<String, String>`）、`read_consistency_interval`、`manifest_enabled`，以及在开启 `remote` feature 时的 `client_config`：

[connection.rs:677-692](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L677-L692) —— `ConnectBuilder::new`：用传入的 URI 初始化一个全默认的 `ConnectRequest`。

`ConnectRequest` 本身就是「所有连接配置的集合体」，其中 `uri` 字段的文档注释列出了 LanceDB 接受的 URI 格式：

[connection.rs:613-664](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L613-L664) —— `ConnectRequest` 结构体。注意 `uri` 字段的文档列出了本地路径、`s3://`/`gs://`、`db://` 三类 URI（见 4.2 节）。

#### 4.1.4 代码实践

**实践目标**：亲手验证「`connect()` 只是返回构建器，`.execute()` 才真正建立连接」。

**操作步骤**（源码阅读型，无需运行）：

1. 打开 [simple.rs:24-27](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/examples/simple.rs#L24-L27)，确认 `connect(uri).execute().await?` 这一行。
2. 在 `connection.rs` 中定位 `pub fn connect`（986 行）和 `ConnectBuilder::new`（677 行），观察前者如何把 URI 传给后者。
3. 在脑中把 `connect("data/sample-lancedb")` 这一步拆成两步：
   ```rust
   let builder: ConnectBuilder = connect("data/sample-lancedb"); // 此刻尚未连接
   let db: Connection = builder.execute().await?;                 // 这里才真正连接
   ```

**需要观察的现象**：`connect(...)` 这一行即便去掉 `.execute()`，编译也能通过（`ConnectBuilder` 本身不做 IO）；只有 `.execute()` 返回 `Future`，需要 `.await`。

**预期结果**：你能清楚指出「配置阶段」与「执行阶段」的分界线在 `.execute()`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `connect(...)` 不直接返回 `Connection`，而要先返回一个 `ConnectBuilder`？

> **参考答案**：因为连接需要大量可选配置（凭证、区域、存储选项、一致性等），用构建器模式可以链式追加这些配置、且未来易于扩展；同时把真正有副作用的连接动作集中到 `.execute()`，便于复用配置、延迟执行和测试。

**练习 2**：`ConnectBuilder::new` 里默认把 `manifest_enabled` 设成了什么值？

> **参考答案**：`false`（见 687 行）。即默认走传统的目录列举式本地后端，不启用 manifest 命名空间模式。

---

### 4.2 URI 形式与本地/远程分流

#### 4.2.1 概念说明

LanceDB 支持两类截然不同的「数据库位置」：

- **本地/对象存储**（native）：数据直接写在本地磁盘，或写在 S3、GS、Azure、OSS 等对象存储上。进程内直接读写文件，类似 SQLite。这类由 `ListingDatabase` 实现。
- **远程**（remote / LanceDB Cloud）：数据托管在云端服务，客户端通过 HTTP 与之通信。这类由 `RemoteDatabase` 实现，且需要开启 `remote` feature。

 LanceDB 靠 **URI 的前缀** 来决定走哪条路：以 `db` 开头的 URI（即 `db://dbname`）走远程，其余走本地/对象存储。

#### 4.2.2 核心流程

`execute()` 的分流逻辑极其简短，是理解整个连接机制的关键：

```
execute():
  if uri.starts_with("db"):          # 例如 "db://mydb"
      execute_remote()               #   走 RemoteDatabase（需 region + api_key）
  else if manifest_enabled:
      ListingDatabase::connect_manifest_enabled_namespace_database()
  else:                              # "/tmp/data"、"s3://..."、"gs://..."
      ListingDatabase::connect_with_options()
```

注意判断条件是 `starts_with("db")` 而不是 `starts_with("db://")`——实践中我们总是写 `db://`，但代码确实只看前两个字符。

对于远程路径，`execute_remote()` 会从 `options` 里取出 `region` 和 `api_key`，**缺一不可**，否则返回 `Error::InvalidInput`：

- 没有 `region` → `Error::InvalidInput { message: "A region is required ..." }`
- 没有 `api_key` → `Error::InvalidInput { message: "An api_key is required ..." }`

而如果项目编译时 **没有** 开启 `remote` feature，却用了 `db://` URI，会走另一个 `execute_remote` 分支，返回 `Error::Runtime`，提示需要启用 `remote` feature。

#### 4.2.3 源码精读

`ConnectRequest::uri` 字段的文档注释正式列出了三种 URI 形式：

[connection.rs:614-622](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L614-L622) —— `uri` 字段及其文档：本地路径、`s3://`/`gs://` 对象存储、`db://` 云端。

分流的核心在 `execute()` 方法：

[connection.rs:954-977](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L954-L977) —— `execute()`：用 `uri.starts_with("db")` 判断走远程还是本地，最后都包装成 `Connection`。

远程分支在开启 `remote` feature 时的实现（校验 region/api_key，构造 `RemoteDatabase`）：

[connection.rs:913-944](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L913-L944) —— `execute_remote()`（remote feature 版）：解析选项、强制要求 region 与 api_key、构造 `RemoteDatabase`。

未开启 feature 时的兜底实现，返回一个清晰的错误：

[connection.rs:946-952](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L946-L952) —— `execute_remote()`（无 remote feature 版）：直接报错「需要启用 remote feature」。

本地默认分支最终落到 `ListingDatabase::connect_with_options`：

[listing.rs:464-466](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/listing.rs#L464-L466) —— `ListingDatabase::connect_with_options`：接收 `ConnectRequest`，解析 URI 与选项，建立本地/对象存储后端。

#### 4.2.4 代码实践

**实践目标**：用一个占位 API key 连接一个 `db://` 云端 URI，体会「客户端构造」与「真实网络请求」是两件事。

**操作步骤**：

1. 在一个临时 Rust 项目（或 `rust/lancedb` 的 examples 目录里新建文件）中写：

   ```rust
   // 示例代码：演示 db:// 连接的构造阶段（不依赖真实云端）
   use lancedb::connect;

   #[tokio::main]
   async fn main() -> lancedb::Result<()> {
       // 占位的 api_key 与 region，仅用于让 execute_remote 通过本地校验
       let db = connect("db://my_database")
           .api_key("placeholder-key")
           .region("us-east-1")
           .execute()
           .await?;
       println!("uri = {}", db.uri());
       Ok(())
   }
   ```

2. 编译时必须开启 remote feature，例如：

   ```bash
   cargo run --example <your_example> --features remote
   ```

**需要观察的现象**：

- `execute().await` 会返回 `Ok`，因为 `RemoteDatabase::try_new` 只在本地解析 URL、构造 HTTP 客户端与请求头，**并不发起网络请求**（见 [remote/db.rs:261-322](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/db.rs#L261-L322)）。
- `db.uri()` 打印出 `db://my_database`。
- 但如果你接着调用 `db.table_names().execute().await`，由于没有真实云端服务器，会发起一次注定失败的 HTTP 请求并报错。

**预期结果**：`uri = db://my_database` 成功打印；`table_names()` 失败。这正好说明「连接句柄的构造」与「真正的网络通信」是分离的。若你手头没有真实 LanceDB Cloud 端点，`table_names()` 的具体错误信息为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果我写 `connect("db://x").execute().await` 但忘了调用 `.api_key(...)`，会发生什么？

> **参考答案**：`execute_remote()` 在 [connection.rs:924-926](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L924-L926) 处发现 `api_key` 为 `None`，返回 `Error::InvalidInput`，提示「An api_key is required when connecting to LanceDb Cloud」。

**练习 2**：为什么用 `db://` 却没开 `remote` feature 时，报的是 `Error::Runtime` 而不是 `Error::InvalidInput`？

> **参考答案**：因为没开 feature 时根本编译不进远程实现，分流到的是 [connection.rs:946-952](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L946-L952) 这个兜底版本，它无法去校验凭证，只能笼统地返回 `Error::Runtime`，告诉你需要启用 `remote` feature。

---

### 4.3 Connection 句柄与常用操作（uri、table_names）

#### 4.3.1 概念说明

`Connection` 是建立连接后你一直持有的「数据库句柄」。它非常轻量——内部只有一个 `Arc<dyn Database>`（指向真正的后端实现）和一个嵌入注册表。它实现了 `Clone`，所以你可以随意克隆传给不同任务。

`Connection` 自己几乎不包含逻辑，绝大多数方法都只是把调用**委托**给底层的 `Database` trait 对象。这种「薄外壳 + trait 对象」的设计，让本地后端和远程后端对外长得一模一样。

#### 4.3.2 核心流程

`Connection` 的两个最常用方法：

```
Connection::uri()         → 委托 internal.uri()        返回 &str
Connection::table_names() → 返回 TableNamesBuilder      （又是构建器！）
                            .execute() → internal.table_names(request) → Vec<String>
```

`table_names()` 同样遵循构建器模式：它返回一个 `TableNamesBuilder`，支持 `.limit()`、`.start_after()`、`.namespace()`，最后 `.execute()` 才真正去后端列表。返回结果按字典序升序排列。

#### 4.3.3 源码精读

`Connection` 结构体只有两个字段：

[connection.rs:374-379](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L374-L379) —— `Connection` 结构体：`internal: Arc<dyn Database>` 加 `embedding_registry`。注意它 derive 了 `Clone`。

`uri()` 是最薄的委托：

[connection.rs:398-401](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L398-L401) —— `Connection::uri()`：直接调用 `self.internal.uri()`。

`table_names()` 返回一个构建器而不是直接返回结果：

[connection.rs:408-415](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L408-L415) —— `Connection::table_names()`：构造 `TableNamesBuilder`，附带说明结果按字典序升序、支持分页。

`TableNamesBuilder` 提供 `limit` / `start_after` / `namespace`，`execute` 时把请求交给后端：

[connection.rs:75-116](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L75-L116) —— `TableNamesBuilder`：链式配置分页与命名空间，`execute()` 委托 `Database::table_names`。

而 `Database` trait 定义了后端必须实现的能力，`uri()` 是其中之一：

[database.rs:207-213](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database.rs#L207-L213) —— `Database` trait：要求实现 `uri()`、`read_consistency()`、`table_names()` 等。

> 补充：`Connection` 还提供 `create_table`、`open_table`、`drop_table`、`clone_table`、`create_namespace` 等方法（[connection.rs:417-609](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L417-L609)），它们无一例外都委托给 `internal`，本讲不展开，留待 u2-l2/u2-l3。

#### 4.3.4 代码实践

**实践目标**：连接一个本地目录，用 `uri()` 与 `table_names()` 感受 `Connection` 句柄。

**操作步骤**：

1. 在 `rust/lancedb` 下直接跑官方示例（它正好演示了这两步）：

   ```bash
   cargo run --example simple
   ```

2. 阅读对应源码 [simple.rs:24-31](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/examples/simple.rs#L24-L31)：先 `connect(uri).execute()`，再 `db.table_names().execute()`。

**需要观察的现象**：

- 程序会在 `data/sample-lancedb` 下创建本地数据库目录。
- 第一行 `table_names()` 打印通常是 `[]`（建表之前为空）。
- 之后示例会建表、加数据、搜索，最后 `drop_table`。

**预期结果**：你能看到 `table_names()` 在建表前后从空列表变成包含 `"my_table"` 的列表（参见示例中后续的建表逻辑）。

#### 4.3.5 小练习与答案

**练习 1**：`Connection::table_names()` 为什么不直接返回 `Vec<String>`，而要返回一个 `TableNamesBuilder`？

> **参考答案**：因为列表操作支持分页（`limit`、`start_after`）和指定命名空间（`namespace`），用构建器可以让这些可选项链式表达；同时保持「配置在 Builder、执行在 `execute()`」的一致风格。`execute()` 内部再委托 `Database::table_names(request)`（见 [connection.rs:111-116](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L111-L116)）。

**练习 2**：`Connection` 能否在多个 tokio 任务之间共享？

> **参考答案**：可以。`Connection` derive 了 `Clone`，且内部用 `Arc<dyn Database>` 持有后端，克隆代价很低、可安全跨线程/任务共享。

---

### 4.4 storage_options 与远程凭证透传

#### 4.4.1 概念说明

很多时候连接还伴随一堆「底层配置」：访问 S3 的 AK/SK、超时时间、自定义 endpoint 等。LanceDB 用一个统一的键值对机制 `storage_options`（存储选项）来承载它们，最终透传给底层 `object_store` / Lance / 远程客户端。

远程凭证 `api_key` / `region` / `host_override` 也是同样的套路——它们并不是 `ConnectBuilder` 上的独立字段，而是被写进 `request.options` 这个 `HashMap<String, String>` 里，用一组约定好的键名（如 `remote_database_api_key`）来标识。

#### 4.4.2 核心流程

```
ConnectBuilder
  .api_key(k)      → options["remote_database_api_key"] = k     （仅 remote feature）
  .region(r)       → options["remote_database_region"] = r       （仅 remote feature）
  .host_override(h)→ options["remote_database_host_override"] = h（仅 remote feature）
  .storage_option(k, v)        → options[k] = v
  .storage_options([(k,v)...]) → 逐个写入 options

execute_remote():
  1. apply_env_defaults()：若没显式设置，从环境变量补默认值（如 AZURE_STORAGE_ACCOUNT_NAME）
  2. RemoteDatabaseOptions::parse_from_map(options)：把 HashMap 解析成结构化选项
  3. 取出 region / api_key / storage_options，构造 RemoteDatabase
```

也就是说，`api_key` 这类便捷方法只是「往 options 写一条约定键」的语法糖，真正的解析发生在 `execute_remote()`。

#### 4.4.3 源码精读

`api_key`、`region`、`host_override` 三个方法都标注了 `#[cfg(feature = "remote")]`，且都是往 `request.options` 插入约定键：

[connection.rs:694-741](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L694-L741) —— `api_key` / `region` / `host_override`：把值写进 `options`，键名分别是 `OPT_REMOTE_API_KEY` / `OPT_REMOTE_REGION` / `OPT_REMOTE_HOST_OVERRIDE`。

这些键名常量定义在远程模块：

[remote/db.rs:84-87](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/db.rs#L84-L87) —— `OPT_REMOTE_API_KEY` 等常量定义。

通用存储选项方法对本地和远程都适用：

[connection.rs:803-822](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L803-L822) —— `storage_option` / `storage_options`：把任意键值对写进 `request.options`。

环境变量兜底逻辑：开启 remote feature 时，若用户没显式提供某些选项，会从环境变量补默认值。目前唯一一条映射是把 `AZURE_STORAGE_ACCOUNT_NAME` 映射成存储选项 `azure_storage_account_name`：

[connection.rs:672-674](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L672-L674) —— `ENV_VARS_TO_STORAGE_OPTS` 常量：环境变量到存储选项键名的映射表。

[connection.rs:899-911](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L899-L911) —— `apply_env_defaults`：遍历映射表，仅当 options 中尚未包含该键时才用环境变量补值。

最终在 `execute_remote()` 里把这些 options 解析、分流：

[connection.rs:917-937](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L917-L937) —— `execute_remote` 的主体：先合并环境变量默认值，再 `parse_from_map`，取出 region/api_key 与 storage_options，构造 `RemoteDatabase`。

> 另一个常用配置是 `read_consistency_interval`（读取一致性间隔），它决定多久检查一次其他进程的更新：不设则不检查（默认，读最快）；设为 0 表示强一致；设为非零值表示最终一致。详见 [connection.rs:860-883](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L860-L883)，对应的概念会在 u5-l3「版本管理与时间旅行」深入展开。

#### 4.4.4 代码实践

**实践目标**：用 `storage_options` 传一组（占位）S3 凭证，观察它们如何进入连接配置。

**操作步骤**（源码阅读型 + 可选运行）：

1. 参考 crate 文档里的写法（[lib.rs:55-66](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs#L55-L66)）：

   ```rust
   // 示例代码：演示 storage_options 的链式写法
   let db = lancedb::connect("data/sample-lancedb")
       .storage_options([
           ("aws_access_key_id", "some_key"),
           ("aws_secret_access_key", "some_secret"),
       ])
       .execute()
       .await?;
   ```

2. 在 [connection.rs:803-822](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L803-L822) 处确认这两个键值最终被插进 `request.options`。
3. 跟踪 `apply_env_defaults`（899 行）的逻辑：如果你在 shell 里 `export AZURE_STORAGE_ACCOUNT_NAME=foo` 但代码里没显式设置 `azure_storage_account_name`，它会被自动补进远程连接的选项。

**需要观察的现象**：

- `storage_options` 接受任意键值对，LanceDB 不在构建阶段校验键名是否合法——校验/使用发生在底层后端。
- 环境变量兜底**只**对远程（remote feature）连接生效（`apply_env_defaults` 带 `#[cfg(feature = "remote")]`）。

**预期结果**：你能讲清楚「`api_key`/`region` 是便捷语法糖，本质也是写进 options；真正消费这些 options 的是后端」。若你本地没有真实 S3 凭证，写入占位值后 `execute()` 对本地路径仍能成功（本地后端忽略未识别的存储选项），实际连接对象存储是否成功为「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`api_key("x")` 和 `storage_option("remote_database_api_key", "x")` 效果一样吗？

> **参考答案**：一样。`api_key` 内部就是把值插到 `options[OPT_REMOTE_API_KEY]`（[connection.rs:702-708](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L702-L708)），而 `OPT_REMOTE_API_KEY` 的值就是字符串 `"remote_database_api_key"`（[remote/db.rs:85](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/db.rs#L85)）。前者只是更易读的语法糖。

**练习 2**：为什么 `api_key` 方法上有 `#[cfg(feature = "remote")]`，而 `storage_options` 没有？

> **参考答案**：`api_key`/`region`/`host_override` 是 LanceDB Cloud 专属概念，没有 remote feature 时这些方法没有意义；而 `storage_options` 对本地对象存储（S3/GS 等）同样需要，所以它是无条件提供的。

---

## 5. 综合实践

把本讲四个小节串起来，完成下面这个综合任务（源码阅读 + 本地运行相结合）：

**任务**：写一个小程序，分别建立「本地连接」和「云端连接（占位凭证）」，对比两者的 `uri()` 输出与 `table_names()` 行为，并解释差异。

**建议步骤**：

1. 在 `rust/lancedb/examples/` 下新建一个示例文件（例如 `connect_compare.rs`），内容大致如下（**示例代码**）：

   ```rust
   use lancedb::connect;

   #[tokio::main]
   async fn main() -> lancedb::Result<()> {
       // —— 本地连接 ——
       let local_db = connect("data/compare-local").execute().await?;
       println!("local uri  = {}", local_db.uri());
       println!("local names = {:?}", local_db.table_names().execute().await?);

       // —— 云端连接（占位凭证，仅演示构造阶段） ——
       let cloud_db = connect("db://my_database")
           .api_key("placeholder-key")
           .region("us-east-1")
           .execute()
           .await?;
       println!("cloud uri  = {}", cloud_db.uri());

       // 这一步会发起真实 HTTP 请求，没有真实端点时会失败：
       match cloud_db.table_names().execute().await {
           Ok(names) => println!("cloud names = {names:?}"),
           Err(e) => println!("cloud table_names 失败（预期，因为无真实端点）：{e}"),
       }
       Ok(())
   }
   ```

2. 编译并运行（注意云端部分需要 `remote` feature）：

   ```bash
   cargo run --example connect_compare --features remote
   ```

3. 对照源码回答三个问题：
   - 本地连接走的是 `execute()` 里哪一条分支？（提示：[connection.rs:968-969](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L968-L969)）
   - 云端连接的 `api_key("placeholder-key")` 最终被存到哪个键、由谁消费？
   - 为什么 `cloud_db.uri()` 能成功打印，而 `cloud_db.table_names()` 却失败？

**验收标准**：

- 能清楚说出本地与远程两条分流路径；
- 能解释「连接构造（无网络）」与「真正发起请求」的差异；
- 知道 `api_key`/`region`/`storage_options` 都最终落在 `request.options` 这个 HashMap 里。

> 说明：如果你无法访问真实 LanceDB Cloud，云端 `table_names()` 的具体错误信息请标注为「待本地验证」；本任务的核心是理解分流与配置透传，而非真正连上云端。

## 6. 本讲小结

- `connect(uri)` 只是入口语法糖，返回一个 `ConnectBuilder`；真正的连接发生在 `.execute().await`，这是贯穿 LanceDB 的「构建器 + execute」风格。
- `execute()` 用 **URI 前缀** 分流：以 `db` 开头走远程 `RemoteDatabase`（需 region + api_key），否则走本地/对象存储的 `ListingDatabase`；`manifest_enabled` 会再切出一条命名空间分支。
- 没开启 `remote` feature 却用 `db://`，会得到 `Error::Runtime`；远程缺 `region`/`api_key` 会得到 `Error::InvalidInput`。
- `Connection` 是轻量、可克隆的「数据库句柄」，内部持有 `Arc<dyn Database>`，几乎所有方法（`uri`、`table_names`、`create_table`…）都委托给底层 trait 对象，使本地与远程对外接口一致。
- `api_key`/`region`/`host_override` 是把值写进 `request.options` 的语法糖，键名由 `remote/db.rs` 的常量约定；`storage_options` 则是通用的键值透传通道，最终交给底层后端。
- 远程连接还支持从环境变量兜底（如 `AZURE_STORAGE_ACCOUNT_NAME`），以及 `read_consistency_interval` 控制读取新鲜度。

## 7. 下一步学习建议

- **u2-l2「Table 抽象层」**：连接拿到后，下一步就是 `open_table` / `create_table` 返回的 `Table` 句柄。建议接着阅读 `rust/lancedb/src/table.rs`，理解 `Table`、`BaseTable`、`NativeTable` 三层抽象，以及 `Connection::open_table`（[connection.rs:452-465](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L452-L465)）如何与 `OpenTableBuilder` 配合。
- **u6-l1「对象存储抽象」** 与 **u6-l2「Database trait 与命名空间」**：想深入理解 `storage_options` 最终如何被 `object_store` 消费、`ListingDatabase` 与 `RemoteDatabase` 如何实现同一个 `Database` trait，可跳读 `rust/lancedb/src/io/` 与 `rust/lancedb/src/database/`。
- **u5-l3「版本管理与时间旅行」**：本讲提到的 `read_consistency_interval` 在那里会有完整展开。
- 建议同时打开 `connection.rs` 的测试模块（[connection.rs:1208-1573](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L1208-L1573)），其中 `test_connect`、`test_table_names`、`test_open_table` 等用例是最好的「可运行文档」。
