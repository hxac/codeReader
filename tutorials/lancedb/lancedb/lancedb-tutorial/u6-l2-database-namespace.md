# Database trait 与命名空间模型

## 1. 本讲目标

本讲承接 u6-l1（对象存储抽象与存储选项），把视线从「字节怎么落到存储」上移一层，落到「**表的集合怎么被组织和管理**」上。存储层回答的是「读写一个对象」，而本讲回答的是「给定一个数据库，我怎么知道它有哪些表、表放在哪个路径下、表与表之间有没有层级关系」。学完本讲后，你应该能够：

- 说清楚 `Database` trait 在 LanceDB 中扮演的角色——它是 `Connection` 内部那块 `Arc<dyn Database>` 的契约，把「表集合的管理」抽象成一组统一方法。
- 区分 LanceDB 的三套 `Database` 实现：默认的 **`ListingDatabase`**（文件系统列举）、基于 `lance-namespace` 的 **`LanceNamespaceDatabase`**（多级命名空间）、以及远程的 **`RemoteDatabase`**（留给 u6-l3）。
- 理解一个反直觉但关键的事实：**`ListingDatabase` 内部自带一个 `LanceNamespaceDatabase`**——命名空间不是 listing 的对立面，而是它更深的底座。
- 说清楚 listing 模式与 namespace 模式在「**表的物理位置由谁决定**」上的根本差异：listing 由客户端按约定拼路径，namespace 由服务端（`declare_table`）分配位置。

## 2. 前置知识

- **trait 对象（trait object）**：用 `Arc<dyn SomeTrait>` 持有一个「只暴露 trait 方法、隐藏具体类型」的对象。u2-l2 已讲过 `Arc<dyn BaseTable>`，本讲的 `Arc<dyn Database>` 是同一种套路，只是抽象层级更高（管一群表，而不是单张表）。
- **Builder 模式 + `.execute()`**：贯穿 LanceDB 的风格——凡涉及 IO 的操作都先返回一个配置器，调用 `.execute().await` 才真正生效。u2-l1 的 `ConnectBuilder` 就是典型。
- **目录列举（directory listing）**：文件系统提供「列出一个目录下有哪些条目」的原语。LanceDB 最朴素的建库方式就是：一个目录 = 一个数据库，目录下每个 `xxx.lance` 子目录 = 一张表。要「列出表名」，只需列一次目录。
- **命名空间（namespace）**：一种把资源按层级组织的机制，类似文件系统的目录树或数据库的 schema（如 PostgreSQL 的 `public.my_table`）。LanceDB 的命名空间是多级的（`["ns1", "ns2"]`），由独立的 `lance-namespace` 抽象提供。

> **关键心智模型**：`Connection` 是个**外壳**。你调用的 `create_table`、`table_names`、`create_namespace` 等方法，几乎都是一行转发给内部的 `Arc<dyn Database>`。至于这个 trait 对象背后到底是 `ListingDatabase` 还是 `LanceNamespaceDatabase`，由连接时的 **URI 前缀** 和 **`manifest_enabled` 开关** 决定，对用户完全透明。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `rust/lancedb/src/database.rs` | 定义 `Database` trait、各 Request 结构（`OpenTableRequest`/`CreateTableRequest` 等）、`ReadConsistency` 枚举，是本讲的**契约层**。 |
| `rust/lancedb/src/database/listing.rs` | 实现 `ListingDatabase`：表即文件夹、靠目录列举发现表；同时把命名空间相关操作**委托**给内嵌的 `LanceNamespaceDatabase`。 |
| `rust/lancedb/src/database/namespace.rs` | 实现 `LanceNamespaceDatabase`：把表管理**委托**给 `lance-namespace` 客户端（`declare_table`/`describe_table`/`list_tables` …），支持多级命名空间。 |
| `rust/lancedb/src/database/read_freshness.rs` | namespace 后端独有的读新鲜度机制（写时 bump 基线、读时注入时间戳头）。u5-l3 已讲，本讲只做衔接。 |
| `rust/lancedb/src/connection.rs` | `connect()`/`connect_namespace()` 入口、`ConnectBuilder::execute()` 里「**选哪种 Database**」的分流逻辑，以及 `Connection` 结构体本身。 |
| `rust/lancedb/src/lib.rs` | 重新导出 `connect` 与 `connect_namespace` 两个顶层入口。 |

## 4. 核心概念与源码讲解

本讲覆盖两个最小模块：**`database`**（统一契约 `Database` trait）与 **`database (listing/namespace)`**（两套实现及其差异）。下面分三节讲。

### 4.1 Database trait：表集合的统一抽象

#### 4.1.1 概念说明

到目前为止，你已经熟悉「表」这个抽象（u2-l2 的 `Table`/`BaseTable`/`NativeTable`）。但一个数据库从来不只一张表——它管理着**一组表及其元数据**（表名、所在位置、命名空间层级……）。LanceDB 用一个 `Database` trait 把「表集合的管理」抽象出来：

```
Connection  （对外句柄：用户直接打交道）
    │  持有
    ▼
Arc<dyn Database>  （契约：list/create/open/drop 表 + 管命名空间）
    │  具体实现（三选一，连接时决定）
    ▼
ListingDatabase | LanceNamespaceDatabase | RemoteDatabase
```

为什么要单独抽一个 `Database` trait？源码注释在 [database.rs:4-15](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database.rs#L4-L15) 给了四条理由：表可能以不同顺序排在 S3 上、可能由独立应用管理、可能由 Postgres 之类的系统托管、也可能用自定义表实现（如远程表）。把「表从哪来、放哪去」做成可替换的 trait，就能在不改用户 API 的前提下切换后端。

> 一句话：`Table`（u2-l2）抽象的是「**一张表怎么读写**」，`Database`（本讲）抽象的是「**一堆表怎么被发现和管理**」。

#### 4.1.2 核心流程

`Connection` 把几乎所有方法都委托给内部的 `Arc<dyn Database>`。一次 `connect(uri).execute()` 之后，运行时是这样分工的：

```
connect(uri)
   └─► ConnectBuilder::execute()
            │  按 uri 前缀 / manifest_enabled 分流
            ├── uri 以 "db" 开头        ─► RemoteDatabase        （u6-l3）
            ├── manifest_enabled = true ─► LanceNamespaceDatabase（manifest 模式）
            └── 其它（本地/对象存储）   ─► ListingDatabase        （默认）

Connection { internal: Arc<dyn Database>, embedding_registry }
   └─ 用户调用 create_table/table_names/create_namespace …
          └─► 一行转发 internal.<同名方法>()
```

`Database` trait 的方法可分三组：

1. **表管理**：`list_tables` / `create_table` / `open_table` / `clone_table` / `rename_table` / `drop_table` / `drop_all_tables`。
2. **命名空间管理**：`list_namespaces` / `create_namespace` / `drop_namespace` / `describe_namespace`。
3. **自省与一致性**：`uri()` / `read_consistency()` / `as_any()` / `namespace_client()` / `namespace_client_config()`。

其中命名空间那一组是后加的，所有请求/响应类型（`ListNamespacesRequest` 等）直接复用 `lance-namespace::models`，见 [database.rs:23-27](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database.rs#L23-L27) 的 import——这说明「命名空间」概念本身就是从 `lance-namespace` 借来的。

#### 4.1.3 源码精读

**① `Database` trait 的定义** —— [database.rs:203-278](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database.rs#L203-L278)：

```rust
#[async_trait::async_trait]
pub trait Database:
    Send + Sync + std::any::Any + std::fmt::Debug + std::fmt::Display + 'static
{
    fn uri(&self) -> &str;
    async fn read_consistency(&self) -> Result<ReadConsistency>;
    async fn list_namespaces(&self, request: ListNamespacesRequest)
        -> Result<ListNamespacesResponse>;
    async fn create_namespace(&self, request: CreateNamespaceRequest)
        -> Result<CreateNamespaceResponse>;
    // … drop_namespace / describe_namespace / table_names(deprecated) …
    async fn list_tables(&self, request: ListTablesRequest) -> Result<ListTablesResponse>;
    async fn create_table(&self, request: CreateTableRequest) -> Result<Arc<dyn BaseTable>>;
    async fn clone_table(&self, request: CloneTableRequest) -> Result<Arc<dyn BaseTable>>;
    async fn open_table(&self, request: OpenTableRequest) -> Result<Arc<dyn BaseTable>>;
    async fn rename_table(&self, cur_name: &str, new_name: &str,
        cur_namespace_path: &[String], new_namespace_path: &[String]) -> Result<()>;
    async fn drop_table(&self, name: &str, namespace_path: &[String]) -> Result<()>;
    async fn drop_all_tables(&self, namespace_path: &[String]) -> Result<()>;
    fn as_any(&self) -> &dyn std::any::Any;
    async fn namespace_client(&self) -> Result<Arc<dyn LanceNamespace>>;
    async fn namespace_client_config(&self) -> Result<(String, HashMap<String, String>)>;
}
```

中文说明：trait 要求实现者同时是 `Send + Sync + Any + Debug + Display + 'static`——能跨线程、能向下转型（`as_any`，呼应 u2-l2 讲过的 downcast）、能打印。注意 `create_table`/`open_table` 返回的是 `Arc<dyn BaseTable>`，正好接上 u2-l2 的 Table 三层抽象。两个「namespace_client」方法很特别：它们让任何 `Database` 都能**吐出一个等价的命名空间客户端**，是实现 listing 与 namespace 互通的桥梁（4.3 节细讲）。

**② `Connection` 只是块外壳** —— [connection.rs:376-379](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L376-L379)：

```rust
pub struct Connection {
    internal: Arc<dyn Database>,
    embedding_registry: Arc<dyn EmbeddingRegistry>,
}
```

中文说明：`Connection` 只有两个字段——一个 `Database` trait 对象、一个嵌入函数注册表（u8-l1 讲）。所有表/命名空间方法都是对 `self.internal` 的转发，例如 [connection.rs:552-559](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L552-L559) 的 `create_namespace`、[connection.rs:560-565](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L560-L565) 的 `drop_namespace`，函数体都只有 `self.internal.xxx(request).await` 一行。

**③ 连接时「选哪种 Database」的分流** —— [connection.rs:955-977](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L955-L977)：

```rust
pub async fn execute(self) -> Result<Connection> {
    if self.request.uri.starts_with("db") {
        self.execute_remote()                       // RemoteDatabase（u6-l3）
    } else if self.request.manifest_enabled {
        let internal = Arc::new(
            ListingDatabase::connect_manifest_enabled_namespace_database(&self.request).await?,
        );                                           // → LanceNamespaceDatabase
        /* … */
    } else {
        let internal = Arc::new(ListingDatabase::connect_with_options(&self.request).await?);
        /* … */                                      // 默认 ListingDatabase
    }
}
```

中文说明：三条分支正好对应三种实现。注意中间那条 `manifest_enabled`——它名字叫 `connect_manifest_enabled_namespace_database`，但**返回的其实是 `LanceNamespaceDatabase`**（见 4.3 节 [listing.rs:445-456](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/listing.rs#L445-L456)），并且保留目录列举做迁移兜底。`manifest_enabled` 开关本身定义在 [connection.rs:638-645](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L638-L645) 的 `ConnectRequest` 上。

**④ 两个顶层入口** —— [lib.rs:337-340](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs#L337-L340)：

```rust
/// Connect to a database
pub use connection::connect;
/// Connect to a namespace-backed database
pub use connection::connect_namespace;
```

中文说明：`connect(uri)` 是大多数人用的入口（按 URI 自动选后端）；`connect_namespace(ns_impl, properties)` 是显式接入命名空间的入口（4.3 节细讲），二选其一都能拿到一个 `Connection`。

#### 4.1.4 代码实践

**实践目标**：确认「换 URI / 换开关 → 换不同的 `Database` 实现，但 `Connection` 用法不变」。

**操作步骤**：

1. 写一个最小程序，用两种方式连同一个本地目录，分别打印 `Connection` 的 `Display`（它内部就是 `Database` 的 `Display`）：

```rust
// 示例代码：观察 Connection 内部的 Database 实现
use lancedb::connect;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let a = connect("/tmp/db_play").execute().await?;             // 默认 listing
    println!("listing      => {}", a);                            // 打印 ListingDatabase(uri=…)

    // manifest 模式：换一个目录以免污染上面的 listing 库
    let b = connect("/tmp/db_play_ns").manifest_enabled(true).execute().await?;
    println!("manifest     => {}", b);                            // 打印 LanceNamespaceDatabase
    Ok(())
}
```

2. 想看「具体是哪种结构体」，可在测试里用 `as_any().downcast_ref`（参考 4.1.5 的练习思路）。

**需要观察的现象**：两次打印的字符串不同——默认连接会显示 `ListingDatabase(uri=…)`，而 `manifest_enabled` 连接显示 `LanceNamespaceDatabase`。

**预期结果**：证明同一个 `Connection` 类型背后是不同的 `Database` 实现。具体 `Display` 输出文案以本地版本为准——待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `create_table`/`open_table` 的返回类型是 `Arc<dyn BaseTable>` 而不是 `Table`？
**答案**：因为 `Database` 是核心层抽象，它只该返回「表的能力契约」`BaseTable`（u2-l2 讲过），不该耦合到面向用户的高层句柄 `Table`。`Table`（带 `database`、`embedding_registry` 等上下文）是在 `connection.rs`/`table.rs` 那一层把 `Arc<dyn BaseTable>` 包装出来的。这种分层让核心 `Database` 与用户层 `Table` 解耦。

**练习 2**：`Database` trait 上为什么有 `as_any()` 方法？
**答案**：trait 对象 `Arc<dyn Database>` 会「擦除」具体类型，但有时调用方需要知道它到底是 `ListingDatabase` 还是 `LanceNamespaceDatabase`（比如测试里要断言分流结果）。`as_any()` 返回 `&dyn Any`，配合 `downcast_ref::<T>()` 即可恢复具体类型——这正是 u2-l2 讲过的「向下转型」模式在 Database 层的复用。仓库里 [connection.rs:1290-1320](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L1290-L1320) 附近的测试 `test_connect_with_manifest_enabled_uses_directory_namespace` 就是这样断言的。

---

### 4.2 ListingDatabase：文件系统列举模式

#### 4.2.1 概念说明

`ListingDatabase` 是 LanceDB 最朴素、零依赖的建库方式：**一个目录 = 一个数据库，表就是其中的子文件夹**。它的名字来自「**list directory**」——发现表名靠的是「列一次目录」，而不是查一张元数据表。

源码注释在 [listing.rs:219-235](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/listing.rs#L219-L235) 用一张图说清了布局：

```
/data
  /table1.lance      ← 一张表
  /table2.lance      ← 另一张表
```

于是就有两个表 `table1`、`table2`。`LANCE_FILE_EXTENSION` 常量（[listing.rs:42](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/listing.rs#L42)）定义了这个 `.lance` 后缀。

> **核心特征**：listing 模式下，**表的物理位置由客户端按约定拼出来**——`uri + "/" + name + ".lance"`，没有任何「中央目录」记录表清单。表存在与否 = 对应文件夹存在与否。这种「无元数据服务」的设计让它能直接跑在任何对象存储（S3/GS/Azure/本地）上，无需额外组件。

#### 4.2.2 核心流程

`ListingDatabase` 的几条关键路径：

```
连接 connect_with_options(request)
   ├─ 解析 URI、抽出 query（如 ?engine=…）
   ├─ ObjectStore::from_uri_and_params 选后端（u6-l1 讲过）
   ├─ 本地后端则 try_create_dir 建库目录
   ├─ 同时 connect_namespace_database() 建一个内嵌 LanceNamespaceDatabase   ← 关键
   └─ 装进 ListingDatabase { …, namespace_database }

列出表 list_tables(request)
   ├─ request 里有命名空间 id？ ─► 委托给内嵌 namespace_database.list_tables()
   └─ 否则在根目录 ─► object_store.read_dir(base_path)，过滤出 *.lance 的文件名

建表 create_table(request)
   ├─ request.namespace_path 非空？ ─► 委托给内嵌 namespace_database.create_table()
   └─ 否则在根目录 ─► table_uri(name) = uri/name.lance ─► NativeTable::create(...)
```

最值得记住的是那条 **「有命名空间就委托给内嵌 namespace 数据库」** 的规则。这意味着：**即便你用的是默认的 listing 连接，只要操作发生在子命名空间里，实际执行的就退化成了 `LanceNamespaceDatabase`。** 这是 listing 与 namespace「不是对立而是嵌套」的根源。

#### 4.2.3 源码精读

**① `ListingDatabase` 的结构** —— [listing.rs:236-263](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/listing.rs#L236-L263)：

```rust
pub struct ListingDatabase {
    object_store: Arc<ObjectStore>,      // 选好的后端（u6-l1）
    query_string: Option<String>,
    pub(crate) uri: String,
    pub(crate) base_path: object_store::path::Path,
    pub(crate) store_wrapper: Option<Arc<dyn WrappingObjectStore>>,  // u6-l1 讲过
    read_consistency_interval: Option<std::time::Duration>,
    storage_options: HashMap<String, String>,
    storage_options_provider: Option<Arc<dyn StorageOptionsProvider>>,
    new_table_config: NewTableConfig,
    session: Arc<lance::session::Session>,
    namespace_database: Arc<LanceNamespaceDatabase>,  // ← 内嵌的命名空间数据库
}
```

中文说明：注意最后一个字段 `namespace_database`。一个 `ListingDatabase` 在构造时就**同时建好了一个 `LanceNamespaceDatabase`**（见 [listing.rs:572-579](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/listing.rs#L572-L579) 的 `connect_namespace_database`），用 `"dir"` 实现指向同一个根。这就是 listing 能「免费」支持命名空间操作的原因。

**② 列举表名 = 列目录** —— [listing.rs:978-1025](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/listing.rs#L978-L1025)：

```rust
async fn list_tables(&self, request: ListTablesRequest) -> Result<ListTablesResponse> {
    if request.id.as_ref().map(|v| !v.is_empty()).unwrap_or(false) {
        return self.namespace_database().list_tables(request).await;  // 子命名空间 → 委托
    }
    let mut f = self.object_store.read_dir(self.base_path.clone()).await?
        .iter().map(Path::new)
        .filter(|path| path.extension().and_then(|e| e.to_str())
                         .map(|e| e == LANCE_EXTENSION).unwrap_or(false))   // 只留 *.lance
        .filter_map(|p| p.file_stem().and_then(|s| s.to_str().map(String::from)))
        .collect::<Vec<String>>();
    f.sort();
    // … 分页：page_token / limit …
    Ok(ListTablesResponse { tables: f, page_token: next_page_token })
}
```

中文说明：根目录下列表，就是 `read_dir` 后按扩展名过滤、取文件名 stem、排序、再分页。注意 `request.id` 非空（指定了命名空间）就**转交给内嵌 namespace 数据库**——这是 listing/namespace 互通的第二处体现。`table_names`（已废弃）走的是同一套 `read_dir` 逻辑，见 [listing.rs:944-976](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/listing.rs#L944-L976)。

**③ 表的物理位置由客户端拼** —— `table_uri`，见 [listing.rs:672-702](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/listing.rs#L672-L702)：

```rust
fn table_uri(&self, name: &str) -> Result<String> {
    validate_table_name(name)?;
    let mut uri = self.uri.clone();
    /* 补路径分隔符：URI 用 '/'，本地路径用平台分隔符 */
    if !ends_with_separator {
        uri.push(if has_scheme { '/' } else { std::path::MAIN_SEPARATOR });
    }
    uri.push_str(&format!("{}.{}", name, LANCE_FILE_EXTENSION));   // name.lance
    if let Some(query) = self.query_string.as_ref() { uri.push('?'); uri.push_str(query); }
    Ok(uri)
}
```

中文说明：listing 模式的「表在哪」完全由这条函数算出——把库名、表名、`.lance` 后缀拼起来，再透传连接的 query string。没有服务端、没有目录服务。

**④ 建表：拼路径 + 委托 Lance** —— [listing.rs:1027-1074](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/listing.rs#L1027-L1074)：

```rust
async fn create_table(&self, request: CreateTableRequest) -> Result<Arc<dyn BaseTable>> {
    if !request.namespace_path.is_empty() {
        return self.namespace_database().create_table(request).await;  // 子命名空间 → 委托
    }
    let table_uri = request.location.clone()
        .unwrap_or_else(|| self.table_uri(&request.name).unwrap());     // 算出位置
    let write_params = self.prepare_write_params(/* 继承 storage_options、new_table_config */);
    match NativeTable::create(&table_uri, &request.name, /* … */).await {
        Ok(table) => Ok(Arc::new(table)),
        Err(Error::TableAlreadyExists { .. }) => self.handle_table_exists(/* 按 mode 处理 */).await,
        Err(err) => Err(err),
    }
}
```

中文说明：先按 mode 决定「已存在怎么办」（`Create` 报错 / `ExistOk` 打开并校验 schema / `Overwrite` 覆盖），再调用 u2-l2 讲过的 `NativeTable::create` 把数据写到算好的 `table_uri`。`prepare_write_params`（[listing.rs:796-859](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/listing.rs#L796-L859)）把连接级的 `storage_options`/`new_table_config` 注入每张新表的写参数——这正是 u6-l1 讲过的「连接级配置被表继承」的落点。

**⑤ 「不支持」的边界** —— listing 模式把改名做成显式不支持，见 [listing.rs:1219-1239](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/listing.rs#L1219-L1239)：

```rust
async fn rename_table(&self, _cur_name: &str, _new_name: &str, /* … */) -> Result<()> {
    /* namespace_path 非空 → NotSupported */
    Err(Error::NotSupported { message: "rename_table is not supported in LanceDB OSS".into() })
}
```

中文说明：因为 listing 没有元数据服务，「改名」要重命名一整个对象存储前缀（代价大、跨后端行为不一），所以 OSS 版直接拒绝。这正衬托出 namespace 模式为何存在——它有服务端元数据，改名只是改一条记录。

#### 4.2.4 代码实践

**实践目标**：用默认 listing 连接建几张表，去文件系统里**亲眼看到 `xxx.lance` 目录布局**，理解「表 = 文件夹」。

**操作步骤**：

1. 写一个最小程序，建两张表并列出：

```rust
// 示例代码：观察 listing 的目录布局
use lancedb::connect;
use arrow_schema::{DataType, Field, Schema};
use std::sync::Arc;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let db = connect("/tmp/db_play").execute().await?;
    let schema = Arc::new(Schema::new(vec![Field::new("id", DataType::Int32, false)]));
    db.create_empty_table("alpha", schema.clone()).execute().await?;
    db.create_empty_table("beta",  schema).execute().await?;
    println!("tables = {:?}", db.table_names().execute().await?);  // 期望 ["alpha","beta"]
    Ok(())
}
```

2. 程序跑完后，用 `ls /tmp/db_play` 查看磁盘布局（在本地终端执行，非程序内）：

```bash
ls -1 /tmp/db_play
# 预期看到：
# alpha.lance
# beta.lance
```

**需要观察的现象**：磁盘上出现 `alpha.lance` 与 `beta.lance` 两个目录；`table_names()` 按字典序返回它们。

**预期结果**：直观印证「一个库目录、每张表一个 `.lance` 子目录」。若你用的是 `s3://bucket/db` 这样的 URI，则用对应的对象存储列举工具也能看到同样的前缀布局。

> **待本地验证**：`create_empty_table` 的确切方法签名（是否需要 schema、参数顺序）依 `lancedb` 版本而定，请以本地 `lancedb` crate 文档为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 listing 模式下 `rename_table` 会返回 `NotSupported`，而 namespace 模式可以？
**答案**：listing 没有「表清单」的元数据服务，表存在与否完全等于对象存储前缀是否存在；改名等价于「把一整个前缀下的所有文件搬到新前缀」，跨后端（尤其 S3 没有「目录 rename」原子操作）代价大且语义模糊，所以 OSS 版禁止。namespace 模式有服务端元数据，改名只是更新一条 `rename_table` 记录，由服务端协调，因此可行。

**练习 2**：`ListingDatabase` 为什么要在结构体里内嵌一个 `LanceNamespaceDatabase`？
**答案**：因为命名空间操作（`create_namespace`/`list_namespaces`）和「在子命名空间里建表/列表」需要一个真正的命名空间实现来支撑。listing 选择在构造时同时建好一个指向**同一个根目录**的 `dir` 型 namespace 数据库，遇到命名空间相关请求就委托给它。这样 listing 用户无需换连接、换 API，就能「免费」获得命名空间能力——体现了「命名空间是 listing 的更深底座」。

**练习 3**：在 listing 模式下，「表是否存在」是怎么判断的？
**答案**：没有显式的「存在性表」。建表时算出 `table_uri`，调用 `NativeTable::create`，若该路径下已有数据集，底层 Lance 会回 `TableAlreadyExists` 类错误，`create_table` 再 `match` 成 `Error::TableAlreadyExists`（见 4.2.3 ④）。列表时则靠 `read_dir` 现列。即「存在性」是由文件系统状态隐式表达的。

---

### 4.3 LanceNamespaceDatabase 与多级命名空间

#### 4.3.1 概念说明

`LanceNamespaceDatabase` 把「表集合的管理」**整个委托**给一个外部的 `lance-namespace` 客户端（`Arc<dyn LanceNamespace>`）。`lance-namespace` 是一个独立抽象，定义了一组「表/命名空间的元数据 API」：`declare_table`（声明一张表、拿到它的存储位置）、`describe_table`、`list_tables`、`create_namespace` 等。它有多种实现，由一个 `ns_impl` 字符串选择：

- `"dir"` → **`DirectoryNamespace`**：用目录结构模拟命名空间，根目录由 `root` 属性指定。
- `"rest"` → **`RestNamespace`**：通过 HTTP 调用 LanceDB Cloud / 兼容的命名空间服务端（与 u6-l3 的远程后端同源）。

与 listing 最大的区别在于：**namespace 模式下，表的物理位置由服务端（`declare_table` 的返回值）决定，而不是客户端拼路径。** 客户端只负责「声明表 → 拿到服务端给的位置 → 把数据写到那个位置」。这一改变带来三个能力：

1. **多级命名空间**：表可以放在任意层级的命名空间下，如 `["team_a", "ml", "experiments"]`。
2. **集中元数据**：表清单、位置、属性由服务端统一管理，支持 `rename_table` 这类 listing 做不到的操作。
3. **托管版本（managed_versioning）**：开启后，表的提交（manifest）也由命名空间服务端托管（`ExternalManifestCommitHandler`），而不是本地提交。

> **`manifest_enabled` 是什么？** 它是 listing 与 namespace 之间的「迁移桥」：在本地连接上开启后，LanceDB 返回一个**以 manifest（目录命名空间清单）为唯一真相**的 `LanceNamespaceDatabase`，同时保留目录列举做兼容迁移（[connection.rs:638-645](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L638-L645)）。它让老 listing 库平滑过渡到 namespace 元数据模型。

#### 4.3.2 核心流程

namespace 模式有两条连接入口，但殊途同归：

```
入口 A：connect_namespace("dir"|"rest", {properties…})
   └─► ConnectNamespaceBuilder::execute()
          └─► LanceNamespaceDatabase::connect(ns_impl, properties, …)
                 ├─ 用 lance_namespace_impls::ConnectBuilder 按 ns_impl 建客户端
                 ├─ 装上 ReadFreshnessContextProvider（读新鲜度，u5-l3）
                 └─ 装进 LanceNamespaceDatabase { namespace, … }

入口 B：connect(uri).manifest_enabled(true)
   └─► ListingDatabase::connect_manifest_enabled_namespace_database(request)
          └─► 同样调 LanceNamespaceDatabase::connect_with_new_table_config(...)
                （impl="dir"，properties 里带 root + manifest_enabled=true）
```

**建表流程（最关键）**：

```
LanceNamespaceDatabase::create_table(request)
   ├─ table_id = namespace_path ++ [name]              // 如 ["ns","sub","my_table"]
   ├─ 按 mode 分支（Create / Overwrite / ExistOk）
   ├─ declare_table(DeclareTableRequest{id: table_id}) → 服务端返回 {location, storage_options, managed_versioning}
   │     └─ 冲突时：describe_table 判断「已写满」还是「只声明没写」→ 区分真冲突
   ├─ apply_new_table_config（存储版本/v2 manifest/stable row ids，同 listing）
   ├─ managed_versioning? → 装 ExternalManifestCommitHandler（提交也托管）
   └─ NativeTable::create_from_namespace(namespace, &location, …)  // 写到服务端给的位置
          .with_freshness(table_freshness(…))           // 接上读新鲜度基线
```

对比 listing 的 `create_table`：listing 是「自己算 `table_uri` 再写」，namespace 是「问服务端要 `location` 再写」。这一步 `declare_table` 是两种模型的分水岭。

#### 4.3.3 源码精读

**① `LanceNamespaceDatabase` 的结构** —— [namespace.rs:63-85](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/namespace.rs#L63-L85)：

```rust
pub struct LanceNamespaceDatabase {
    namespace: Arc<dyn LanceNamespace>,          // 委托对象（dir / rest）
    storage_options: HashMap<String, String>,
    read_consistency_interval: Option<std::time::Duration>,
    session: Option<Arc<lance::session::Session>>,
    uri: String,
    pushdown_operations: HashSet<NamespaceClientPushdownOperation>,  // 哪些操作下推到服务端
    ns_impl: String,                             // "dir" / "rest"
    ns_properties: HashMap<String, String>,
    new_table_config: NewTableConfig,
    freshness_baselines: FreshnessBaselines,     // 读新鲜度基线（u5-l3）
    delimiter: String,                           // 拼 object_id 的分隔符，默认 "$"
}
```

中文说明：核心是 `namespace: Arc<dyn LanceNamespace>`——所有表/命名空间操作最终都调它的方法。`pushdown_operations` 决定哪些操作（查询、建表）下推到服务端执行而非本地（见 [connection.rs:996-1003](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L996-L1003) 的枚举）。

**② 连接：按 `ns_impl` 建客户端** —— [namespace.rs:147-189](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/namespace.rs#L147-L189)：

```rust
pub(crate) async fn connect_with_new_table_config(
    ns_impl: &str, ns_properties: HashMap<String,String>, /* … */
) -> Result<Self> {
    let mut builder = ConnectBuilder::new(ns_impl);           // lance_namespace_impls
    for (key, value) in ns_properties.clone() {
        builder = builder.property(key, value);               // 如 root=…、manifest_enabled=true
    }
    /* session */
    builder = builder.context_provider(Arc::new(ReadFreshnessContextProvider::new(
        freshness_baselines.clone(), read_consistency_interval,    // 读新鲜度
    )));
    let namespace = builder.connect().await.map_err(|e| Error::InvalidInput {
        message: format!("Failed to connect to namespace: {:?}", e),
    })?;
    /* … 装进 Self … */
}
```

中文说明：`lance_namespace_impls::ConnectBuilder::new(ns_impl)` 按 `"dir"`/`"rest"` 选实现，把 properties（如 `dir` 必需的 `root`、`rest` 的 endpoint）喂给它，再 `.connect()` 拿到一个 `Arc<dyn LanceNamespace>`。读新鲜度的 context provider 在建客户端**之前**就装好，确保后续读请求能带上新鲜度头。

**③ 建表：`declare_table` 拿位置** —— [namespace.rs:351-518](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/namespace.rs#L351-L518) 的核心片段：

```rust
let mut table_id = request.namespace_path.clone();
table_id.push(request.name.clone());                        // 完整 id
/* … mode 分支 … */
let declare_request = DeclareTableRequest { id: Some(table_id.clone()), ..Default::default() };
let (location, initial_storage_options, managed_versioning) = {
    match self.namespace.declare_table(declare_request).await {
        Ok(response) => (
            response.location.ok_or(/* … Runtime: location missing … */)?,
            response.storage_options.or_else(|| Some(self.storage_options.clone())).filter(|o| !o.is_empty()),
            response.managed_versioning,
        ),
        Err(e) if /* Create 模式 + TableAlreadyExists */ => {
            // 冲突消歧：describe_table 看是否已有 version+schema
            // 有 → 真 TableAlreadyExists；无（只声明没写）→ 继续
        }
        Err(e) => return Err(map_namespace_lance_error(e, &request.name)),
    }
};
/* … apply_new_table_config … */
if managed_versioning == Some(true) {
    let external_store = LanceNamespaceExternalManifestStore::for_table_uri(/* … */)?;
    params.commit_handler = Some(Arc::new(ExternalManifestCommitHandler { external_manifest_store: Arc::new(external_store) }));
}
let native_table = NativeTable::create_from_namespace(
    self.namespace.clone(), &location, &request.name, /* … */
).await?.with_freshness(self.table_freshness(&request.namespace_path, &request.name));
```

中文说明：这段信息量很大，三个要点：(1) 表的位置 `location` 来自 `declare_table` 返回，不是客户端拼；(2) `declare_table` 冲突时用 `describe_table` 区分「真已存在」与「声明了但还没写数据」两种情况，避免误报；(3) `managed_versioning` 开启时，连 manifest 提交都走 `ExternalManifestCommitHandler`（提交托管给命名空间服务端）。最后 `.with_freshness(...)` 把这张表接到读新鲜度基线上（u5-l3 讲过）。

**④ 打开表：让命名空间解析位置** —— [namespace.rs:520-535](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/namespace.rs#L520-L535)：

```rust
async fn open_table(&self, request: OpenTableRequest) -> Result<Arc<dyn BaseTable>> {
    let native_table = NativeTable::open_from_namespace(
        self.namespace.clone(), &request.name, request.namespace_path.clone(),
        /* … */,
    ).await?.with_freshness(self.table_freshness(&request.namespace_path, &request.name));
    Ok(Arc::new(native_table))
}
```

中文说明：`open_from_namespace` 让命名空间客户端去查这张表在哪、什么 schema，再打开。注意「表不存在」要被正确映射成 `Error::TableNotFound`（而非笼统的 Runtime 错误），这是 [namespace.rs:1204-1242](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/namespace.rs#L1204-L1242) 测试 `test_namespace_table_not_found` 守护的回归点。

**⑤ 表的全局 id** —— 命名空间下的表有一个由 `namespace_path ++ [name]` 用 `$` 拼成的全局 id，见测试断言 [namespace.rs:805](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/namespace.rs#L805)：

```rust
assert_eq!(table.id(), "test_ns$test_table");   // 命名空间$表名
```

分隔符默认是 `$`（[namespace.rs:60](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/namespace.rs#L60) 的 `DEFAULT_NAMESPACE_DELIMITER`），可通过 `delimiter` 属性覆盖；这个 id 也是读新鲜度基线的 key（[namespace.rs:193-198](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/namespace.rs#L193-L198) 的 `table_freshness`）。

**⑥ listing 把命名空间操作委托出去** —— 这是两种模型互通的关键，见 [listing.rs:903-908](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/listing.rs#L903-L908)：

```rust
async fn list_namespaces(&self, request: ListNamespacesRequest) -> Result<ListNamespacesResponse> {
    self.namespace_database().list_namespaces(request).await    // ListingDatabase → 内嵌 namespace db
}
```

中文说明：`ListingDatabase` 的 `list_namespaces`/`create_namespace`/`describe_namespace` 全部一行委托给内嵌的 `LanceNamespaceDatabase`（同样的还有 `drop_namespace` 等）。这就是为什么默认 listing 连接也能 `create_namespace`——它用的是同一个 `dir` 命名空间后端。测试 [listing.rs:2413-2455](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/listing.rs#L2413-L2455) `test_listing_database_namespace_operations` 完整演示了在 listing 连接上建 `parent/child` 两级命名空间。

#### 4.3.4 代码实践

**实践目标**：用显式的 `connect_namespace` 入口，建一个**多级命名空间**并在其中建表，对比它和 listing 在「表归属」上的差异。

**操作步骤**：

1. 写一个最小程序，用 `"dir"` 实现连接，创建子命名空间 `test_ns` 并在其中建表：

```rust
// 示例代码：namespace 模式下的多级命名空间
use lancedb::connect_namespace;
use lancedb::database::CreateTableMode;
use arrow_array::{Int32Array, RecordBatch};
use arrow_schema::{DataType, Field, Schema};
use std::collections::HashMap;
use std::sync::Arc;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut props = HashMap::new();
    props.insert("root".to_string(), "/tmp/db_ns_play".to_string());  // dir 实现必需
    let conn = connect_namespace("dir", props).execute().await?;

    // 建子命名空间 test_ns
    conn.create_namespace(lance_namespace::models::CreateNamespaceRequest {
        id: Some(vec!["test_ns".into()]),
        ..Default::default()
    }).await?;

    // 在 test_ns 下建表
    let schema = Arc::new(Schema::new(vec![Field::new("id", DataType::Int32, false)]));
    let batch = RecordBatch::try_new(schema, vec![Arc::new(Int32Array::from(vec![1, 2, 3]))])?;
    conn.create_table("my_table", batch)
        .namespace(vec!["test_ns".into()])   // ← 指定命名空间
        .execute().await?;

    // 列出 test_ns 下的表
    let names = conn.table_names()
        .namespace(vec!["test_ns".into()])
        .execute().await?;
    println!("test_ns 下的表 = {:?}", names);   // 期望 ["my_table"]
    Ok(())
}
```

2. 对照仓库测试阅读行为：[namespace.rs:757-815](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/namespace.rs#L757-L815) 的 `test_namespace_create_table_basic` 做的就是这件事，并断言了 `table.namespace() == ["test_ns"]`、`table.id() == "test_ns$test_table"`。

**需要观察的现象**：在 `test_ns` 命名空间下列表能看到 `my_table`；而在根命名空间（不传 `.namespace(...)`）下列表**看不到**它——表被「关」在子命名空间里了。

**预期结果**：`test_ns 下的表 = ["my_table"]`，根命名空间列表为空（或只含根级表），印证命名空间对表的隔离。

> **待本地验证**：`connect_namespace`、`create_table(...).namespace(...)`、`table_names(...).namespace(...)` 的确切签名与所需 import（`lancedb::database::CreateTableMode`、`lance_namespace::models::*`）依版本而定，请以本地 crate 文档为准；`lance_namespace` crate 是否需要对应用户可见也请确认。

#### 4.3.5 小练习与答案

**练习 1**：listing 的 `create_table` 与 namespace 的 `create_table`，在「表位置由谁决定」上有什么本质区别？
**答案**：listing 由**客户端**决定——`table_uri()` 把 `uri/name.lance` 拼出来，直接写。namespace 由**服务端**决定——客户端先 `declare_table` 声明，服务端返回 `location`，客户端再把数据写到那个 `location`。因此 namespace 模式可以让服务端集中管理位置、做重命名、做托管提交，而 listing 做不到。

**练习 2**：`declare_table` 冲突时（返回 TableAlreadyExists 错误），代码为什么要再调一次 `describe_table`？
**答案**：因为「声明过」不等于「写满了」。namespace 模式把「声明表」（拿到位置）和「写数据」（写 location）分成两步，一个表可能处于「已声明但还没写数据」的中间态。冲突时用 `describe_table` 看它是否已有 `version` 和 `schema`：有 → 表真的写满了，报 `TableAlreadyExists`；无 → 只是空壳声明，可以继续写。这个消歧逻辑见 [namespace.rs:421-457](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/namespace.rs#L421-L457)。

**练习 3**：`manifest_enabled(true)` 连接和 `connect_namespace("dir", …)` 连接，最终用的是同一种 `Database` 实现吗？
**答案**：是，都是 `LanceNamespaceDatabase`，impl 都是 `"dir"`。区别只在 properties：`manifest_enabled` 路径会额外塞 `manifest_enabled=true` 与 `dir_listing_to_manifest_migration_enabled=true`（[listing.rs:302-318](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/listing.rs#L302-L318)），让 manifest 成为表元数据的唯一真相、并开启目录列举兼容迁移。二者殊途同归于 `LanceNamespaceDatabase::connect_with_new_table_config`。

---

## 5. 综合实践

**任务**：用 listing 模式与 namespace 模式分别连一个库，建同样的两张表，对比两者在「表组织」上的差异，并用源码解释你看到的现象。这正是本讲规格里要求的实践任务。

**步骤**：

1. **listing 模式**：用 `connect("/tmp/cmp_listing")` 建表 `t_a`、`t_b`，然后 `ls /tmp/cmp_listing` 观察到 `t_a.lance`、`t_b.lance` 两个并列目录（无层级）。

2. **namespace 模式**：用 `connect_namespace("dir", {"root": "/tmp/cmp_ns"})` 建子命名空间 `ns1`，在 `ns1` 下建表 `t_a`，再在根下建表 `t_b`。分别列出 `ns1` 下与根下的表，观察同名表 `t_a` 能否在 `ns1` 与根下**同时存在**而不冲突。

```rust
// 示例代码：综合对比骨架（关键片段）
use lancedb::{connect, connect_namespace};
use std::collections::HashMap;

// —— listing ——
let listing = connect("/tmp/cmp_listing").execute().await?;
listing.create_empty_table("t_a", schema.clone()).execute().await?;
listing.create_empty_table("t_b", schema).execute().await?;
println!("listing root = {:?}", listing.table_names().execute().await?);

// —— namespace ——
let mut props = HashMap::new();
props.insert("root".to_string(), "/tmp/cmp_ns".to_string());
let ns = connect_namespace("dir", props).execute().await?;
ns.create_namespace(CreateNamespaceRequest { id: Some(vec!["ns1".into()]), ..Default::default() }).await?;
ns.create_table("t_a", batch.clone()).namespace(vec!["ns1".into()]).execute().await?;  // ns1/t_a
ns.create_table("t_a", batch).execute().await?;                                          // root/t_a（同名，不冲突）
println!("ns1 下 = {:?}", ns.table_names().namespace(vec!["ns1".into()]).execute().await?);
println!("根 下 = {:?}", ns.table_names().execute().await?);
```

3. **源码解释**：回到本讲，用以下三个知识点解释现象：
   - listing 的 `list_tables` 走 `read_dir` + 过滤 `.lance`（[listing.rs:978-1025](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/listing.rs#L978-L1025)），所以表是**扁平并列**的。
   - namespace 的表 id 是 `namespace_path ++ [name]`（[namespace.rs:351-353](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/namespace.rs#L351-L353)），不同命名空间下的同名表 id 不同（`ns1$t_a` vs `t_a`），所以**同名不冲突**。
   - 二者都由同一个 `LanceNamespaceDatabase`（`"dir"` 实现）支撑——listing 只是在根命名空间上做目录列举的快捷方式。

**预期结果**：

| 维度 | listing 模式 | namespace 模式 |
| --- | --- | --- |
| 表的物理位置 | 客户端拼 `uri/name.lance` | 服务端 `declare_table` 返回 |
| 表名层级 | 扁平（一个目录下并列） | 多级（`ns1` 下可再有表） |
| 同名表 `t_a` | 全库唯一，建第二次会 `TableAlreadyExists` | 不同命名空间下可重名 |
| `rename_table` | 不支持（OSS） | 支持（服务端改记录） |

> **待本地验证**：表 中的「同名表」行为，请以本地 `dir` 命名空间实现的实际行为为准——理论上 id 不同应可重名，但 `dir` 实现是否在文件系统层做了额外约束需实测确认。

## 6. 本讲小结

- `Database` trait（[database.rs:203-278](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database.rs#L203-L278)）是「表集合管理」的统一契约；`Connection`（[connection.rs:376-379](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L376-L379)）只是块外壳，几乎所有方法都一行转发给内部的 `Arc<dyn Database>`。
- 连接时分三种后端：默认 `ListingDatabase`、`manifest_enabled` 与 `connect_namespace` 走 `LanceNamespaceDatabase`、`db://` 走 `RemoteDatabase`（u6-l3）。分流在 [connection.rs:955-977](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L955-L977)。
- `ListingDatabase`（[listing.rs:236-263](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/listing.rs#L236-L263)）是「表即文件夹、靠目录列举发现表」的零依赖模型；表的物理位置由客户端 `table_uri` 拼（[listing.rs:672-702](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/listing.rs#L672-L702)），无元数据服务、不支持 `rename_table`。
- `ListingDatabase` 内嵌一个 `LanceNamespaceDatabase`（[listing.rs:262](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/listing.rs#L262)），把命名空间操作和子命名空间内的表操作委托给它——**命名空间是 listing 更深的底座，而非对立面**。
- `LanceNamespaceDatabase`（[namespace.rs:63-85](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/namespace.rs#L63-L85)）把表管理整个委托给 `lance-namespace` 客户端（`"dir"`/`"rest"`）；建表时先 `declare_table` 拿服务端分配的位置再写（[namespace.rs:351-518](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/namespace.rs#L351-L518)），支持多级命名空间、`rename_table` 与托管提交（`managed_versioning`）。
- 表的全局 id 形如 `ns1$t_a`（分隔符默认 `$`），既是命名空间内表的唯一标识，也是读新鲜度基线的 key（承接 u5-l3）。

## 7. 下一步学习建议

- **u6-l3（远程后端：HTTP 客户端与重试）** 是本讲的天然续集：`RemoteDatabase`/`RemoteTable` 是第三套 `Database` 实现，它把本讲的 `declare_table`/`list_tables` 等 API 转成 HTTP 往返，并通过 `ServerVersion` 做能力探测、`retry` 做重试。读完它，你就能凑齐「本地 listing / 本地 namespace / 远程」三种后端的全景。
- 若你想看 `lance-namespace` 抽象本身（`LanceNamespace` trait、`DirectoryNamespace`/`RestNamespace` 实现、各种 Request/Response 模型），它们在独立的 `lance-namespace` 与 `lance-namespace-impls` crate 里，不在本仓库 `rust/lancedb` 内——本讲止于「LanceDB 如何委托」，已为你划清边界。
- 想理解 `managed_versioning` 开启后「提交也托管」的具体机制，可顺读 `lance::io::commit::namespace_manifest::LanceNamespaceExternalManifestStore` 与 `ExternalManifestCommitHandler`——它们解释了 manifest 如何经由命名空间服务端提交（呼应 u5-l1 的 optimize 与 u5-l3 的版本管理）。
