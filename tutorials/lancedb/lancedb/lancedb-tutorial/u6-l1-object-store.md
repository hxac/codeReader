# 对象存储抽象与存储选项

## 1. 本讲目标

本讲是「存储后端与架构」单元的起点，承接 u2-l1（连接 Connection 与 ConnectBuilder）。学完本讲后，你应该能够：

- 说清楚 LanceDB 为什么自己**几乎不写存储代码**，而是把读写委托给底层 `lance` 与 `object_store` crate。
- 解释 `object_store` crate 提供的统一 `ObjectStore` 抽象，以及如何用一个 **URI 前缀**（本地路径 / `s3://` / `gs://` / `az://`）分流到不同的物理后端。
- 掌握 `storage_options` 这条「键值透传通道」如何从用户 API 一路流到 `lance`，用来传递凭证与配置。
- 认识 `WrappingObjectStore` 这个**存储扩展点**，并通过它理解 LanceDB 内置的两个真实实现：写镜像 `MirroringObjectStore`（生产用）与 IO 统计 `io_tracking`（测试用）。

## 2. 前置知识

- **对象存储（Object Storage）**：一种以「键（key/path）→ 字节块（value）」为单位存取数据的存储模型，S3、Google Cloud Storage、Azure Blob 都属此类。它的接口通常只有 `get`（读）、`put`（写）、`list`（列举）、`delete`（删除）几个原语。
- **`object_store` crate**：Apache Arrow 生态的一个 Rust crate，用一个统一的 `ObjectStore` trait 抽象了本地文件系统、S3、GS、Azure 等多种后端。LanceDB / Lance 复用它，从而「换后端只换实现、不改业务代码」。
- **trait 对象与包装（Wrapper）模式**：用 `Arc<dyn ObjectStore>` 持有一个「不知道具体是 S3 还是本地」的存储对象，再在外面包一层「装饰器」加行为（比如统计 IO）。这就是本讲的 `WrappingObjectStore`。
- **Feature flag（特性开关）**：Rust 的条件编译机制。LanceDB 用它把「连 S3 需要的 AWS SDK」做成**可选依赖**，默认不编译，按需开启。

> 关键心智模型：**LanceDB 是个「薄壳」**。它的 `io` 模块本身只有寥寥几行，真正的存储逻辑都在 `lance` crate 里。本讲我们要学的，正是这个「薄壳」如何把用户的配置（URI + `storage_options`）和扩展（`WrappingObjectStore`）正确地转交给底层。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `rust/lancedb/src/io.rs` | LanceDB 的 IO 模块入口，**仅一行**声明子模块，体现「薄壳」定位。 |
| `rust/lancedb/src/io/object_store.rs` | 唯一的子模块：实现写镜像包装器 `MirroringObjectStore` / `MirroringObjectStoreWrapper`。 |
| `rust/lancedb/src/io/object_store/io_tracking.rs` | **仅测试编译**的 IO 统计包装器 `IoTrackingStore` / `IoStatsHolder`，是理解 `WrappingObjectStore` 的最佳参考实现。 |
| `rust/lancedb/src/connection.rs` | 用户侧的 `storage_options(...)` / `session(...)` 等 builder 方法，是存储配置的**入口**。 |
| `rust/lancedb/src/database/listing.rs` | 把 `storage_options` 装进 `ObjectStoreParams` 并交给 `lance` 的 `ObjectStore::from_uri_and_params`，是透传机制的**中转站**。 |
| `rust/lancedb/src/lib.rs` | 重新导出 `Session` 与 `ObjectStoreRegistry`，供高级用户自建会话。 |
| `rust/lancedb/Cargo.toml` | 声明 `aws` / `gcs` / `azure` 等存储后端 feature。 |
| `rust/lancedb/src/table/dataset.rs` | 测试代码展示了如何把一个 IO 统计包装器接进 `WriteParams`，是最直接的「接线」范例。 |

## 4. 核心概念与源码讲解

本讲覆盖两个最小模块：**`io`**（统一存储抽象与 URI 分流）与 **`io (object_store)`**（存储选项透传 + `WrappingObjectStore` 扩展点）。下面拆成三节讲。

### 4.1 统一存储抽象：object_store 与 URI 分流

#### 4.1.1 概念说明

LanceDB 把数据写到哪儿？答案不是「LanceDB 自己写」，而是委托给底层 `lance` crate，而 `lance` 又构建在 `object_store` crate 之上。于是存储层形成一条链：

```
用户 API (connect / create_table / query)
        │  委托
        ▼
lance::io::ObjectStore  （按 URI 选后端、做读取调度）
        │  调用
        ▼
object_store::ObjectStore  （统一 trait：get/put/list/delete）
        │  具体实现
        ▼
LocalFileSystem / AmazonS3 / GoogleCloudStorage / MicrosoftAzure ...
```

这条链的好处是：**业务代码只认 `ObjectStore` trait**，至于底层是本地磁盘还是 S3，由一个 URI 决定，对上层完全透明。这也是为什么 LanceDB 既能在进程内像 SQLite 一样跑（本地后端），又能连 LanceDB Cloud（远程后端，见 u6-l3）。

LanceDB 自己的 `io` 模块薄到什么程度？整个入口文件只有一行有效声明：

```rust
// io.rs 全部「业务」内容
pub mod object_store;
```

参见 [rust/lancedb/src/io.rs:4](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/io.rs#L4) —— 这一行声明了唯一的子模块 `object_store`。换言之，LanceDB 在 IO 层**不重新实现存储**，只在该子模块里提供少量「装饰器」式的扩展实现（见 4.3 节）。

#### 4.1.2 核心流程

存储后端的「选型」由 URI 前缀驱动，核心是 `lance` 提供的 `ObjectStore::from_uri_and_params`。它做两件事：

1. **解析 URI scheme**：本地路径（无 scheme 或 `file://`）→ `LocalFileSystem`；`s3://` → `AmazonS3`；`gs://` → `GoogleCloudStorage`；`az://` → `MicrosoftAzure`。
2. **查询/注册对象存储**：通过一个 `ObjectStoreRegistry`（对象存储注册表）缓存已建好的后端实例，避免重复建连。
3. **应用 `ObjectStoreParams`**：把 `storage_options` 等配置应用到选中的后端上（凭证、endpoint、并发度等）。

> **谁来决定开哪些后端？——Feature flag。** `object_store` crate 对每个云后端都是可选依赖。LanceDB 的 `Cargo.toml` 用一组 feature 做了「透传」：开启 `aws` feature，等于同时开启 `lance/aws`、`lance-io/aws`、`object_store/aws` 等。默认 `default = []`，什么云后端都不带，纯本地用户零负担。

#### 4.1.3 源码精读

**① Feature flag 是纯透传** —— 见 [rust/lancedb/Cargo.toml:110-131](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/Cargo.toml#L110-L131)。注意每个云 feature 都是「一条线打通」多个 crate：

```toml
[features]
default = []
aws = [
    "lance/aws",
    "lance-io/aws",
    "lance-namespace-impls/dir-aws",
    "object_store/aws",
]
gcs = ["lance/gcp", "lance-io/gcp", "lance-namespace-impls/dir-gcp"]
azure = [
    "lance/azure", "lance-io/azure",
    "lance-namespace-impls/dir-azure",
    "lance-namespace-impls/credential-vendor-azure",
]
```

中文说明：开一个 `aws`，就把 `object_store` 的 S3 实现、`lance` 的 S3 支持、命名空间目录后端的 S3 支持一并拉进来。LanceDB 自己**没有**任何一行 S3 专用代码。

**② URI 分流真正发生的地方** —— 见 [rust/lancedb/src/database/listing.rs:541-556](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/listing.rs#L541-L556)：

```rust
let os_params = ObjectStoreParams {
    storage_options_accessor: /* 见 4.2 节 */,
    ..Default::default()
};
let (object_store, base_path) = ObjectStore::from_uri_and_params(
    session.store_registry(),   // ObjectStoreRegistry：缓存已建后端
    &plain_uri,                 // 如 "s3://bucket/path" 或本地路径
    &os_params,
).await?;
if object_store.is_local() {
    Self::try_create_dir(&plain_uri) /* 本地则自动建目录 */;
}
```

中文说明：`from_uri_and_params` 拿到 URI 和注册表后，自己决定建 `LocalFileSystem` 还是 `AmazonS3`，并把 `os_params` 里的 `storage_options` 应用上去。`is_local()` 用来判断是否本地后端，本地后端会自动创建目录。

**③ `ObjectStoreRegistry` 与 `Session` 的公开导出** —— 见 [rust/lancedb/src/lib.rs:342-344](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs#L342-L344)：

```rust
/// Re-export Lance Session and ObjectStoreRegistry for custom session creation
pub use lance::session::Session;
pub use lance_io::object_store::ObjectStoreRegistry;
```

中文说明：LanceDB 把 `Session` 和 `ObjectStoreRegistry` 作为公开类型重新导出，供「想自建会话、复用缓存」的高级场景使用（见 4.1.4 实践）。注意：运行时「活的」注册表实例保存在 `Session` 内部，而 `Session` 在连接时被吃进 `Connection`，普通用户拿不到；要观察它，需要在连接前**自己注入一个 `Session`**。

#### 4.1.4 代码实践

**实践目标**：亲手走一遍「URI 前缀 → 后端类型」的分流，并尝试用 `ObjectStoreRegistry` 感知注册的后端。

**操作步骤**：

1. 写一个最小的 Rust 程序（依赖 `lancedb`，开启 `aws` feature），分别用本地路径与 `memory://` 连接、建表、查询：

```rust
// 示例代码：最小分流观察
use lancedb::connect;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // (a) 本地目录后端
    let local = connect("/tmp/lancedb_play").execute().await?;
    local.create_empty_table("t1", arrow_schema::Schema::empty())
         .execute().await?;
    println!("local tables: {:?}", local.table_names().execute().await?);

    // (b) 内存后端（常用于测试，无需落盘）
    let mem = connect("memory://").execute().await?;
    mem.create_empty_table("t2", arrow_schema::Schema::empty())
       .execute().await?;
    println!("memory tables: {:?}", mem.table_names().execute().await?);
    Ok(())
}
```

2. 若想观察 `ObjectStoreRegistry`，可在连接前自建并注入 `Session`（参考 [rust/lancedb/src/connection.rs:894-895](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L894-L895) 的 `.session(...)` 方法）：

```rust
// 示例代码：注入自定义 Session，以便事后访问其注册表
use lancedb::{connect, Session};
use std::sync::Arc;

let session = Arc::new(Session::default());
let db = connect("memory://")
    .session(Arc::clone(&session))   // 传入会话，自己保留一份克隆
    .execute().await?;
// 建表后，底层后端会被注册进 session.store_registry()
// 具体「列出已注册 store」的 API 名称需对照 lance_io 版本——待本地验证。
```

**需要观察的现象**：本地路径连接会自动创建 `/tmp/lancedb_play` 目录；`memory://` 则一切发生在内存里、进程结束即消失。

**预期结果**：两次连接都能成功 `table_names()`，分别打印出 `["t1"]` / `["t2"]`，证明不同 URI 前缀确实分流到了不同后端，而**用户 API 完全一致**。

> **待本地验证**：`Session::store_registry()` 返回的对象存储注册表，其「枚举已注册 store / 打印 store 类型」的具体方法签名依 `lance_io` 版本而定，请在本地的 `lance_io` 文档里确认后再调用，不要照搬方法名。

#### 4.1.5 小练习与答案

**练习 1**：如果不开启任何存储 feature（`default = []`），能否连接 `s3://bucket/path`？为什么？
**答案**：不能正常工作。`object_store` 的 S3 实现是可选依赖，不开 `aws` feature 时 S3 相关代码不会被编译进来，连接会在「构造 `AmazonS3` 后端」这一步失败或报不支持。这也正是 LanceDB 把云后端做成可选 feature 的初衷——让纯本地用户免于编译庞大的云 SDK。

**练习 2**：为什么 LanceDB 的 `io.rs` 只有一行 `pub mod object_store;`？
**答案**：因为 LanceDB 是个「薄壳」，真正的存储读写由 `lance` 与 `object_store` crate 负责。LanceDB 的 IO 层只提供少数「装饰器」式扩展（写镜像、IO 统计），不重新实现存储原语，所以模块体量极小。

---

### 4.2 storage_options 透传机制

#### 4.2.1 概念说明

连上对象存储往往需要一堆配置：AWS 的 `access_key_id` / `secret_access_key` / `region`、自定义 endpoint、并发度、超时……这些参数**没有固定的字段集合**，不同后端要的键不一样，而且还会随云厂商演进。LanceDB 的解法是：**不发明新结构，直接用一张 `HashMap<String, String>` 当作通用通道**，这就是 `storage_options`。

它的本质是一条「键值透传管道」：用户在 `connect()` 上填的键值对，原封不动地流过 LanceDB，最终交给 `lance`/`object_store` 去解释。这样 LanceDB 永远不必为「新加一个 S3 配置项」而改代码——只更新文档即可。文档里也明确指引：<https://docs.lancedb.com/storage/>（见源码注释中的多处 `See available options at`）。

> 一句话：`storage_options` 是「我不知道你要什么，但我会一字不差地替你转达」。

#### 4.2.2 核心流程

`storage_options` 的完整旅程：

```
connect(uri).storage_options([("k","v"), ...])      用户入口
        │  写入
        ▼
ConnectRequest.options: HashMap<String,String>      连接级配置
        │  解析
        ▼
ListingDatabaseOptions.storage_options              剥离掉「建表配置项」后剩下的键值
        │  打包
        ▼
ObjectStoreParams { storage_options_accessor }      装进 lance 的参数结构
        │  交给
        ▼
ObjectStore::from_uri_and_params(...)               lance 按后端解释这些键值
```

其中有两个值得注意的设计：

1. **「建表配置项」要被剔除**：`request.options` 里混着「存储选项」和「新表配置选项」（如存储版本）。解析时要先认出新表配置键，剩下的才算 `storage_options`。
2. **动态凭证刷新（`StorageOptionsProvider`）**：云凭证会过期。对于长时间运行的任务，LanceDB 支持「动态提供者」——凭证过期时自动刷新。这在 4.2.3 的 `set_storage_options_provider` 体现。

#### 4.2.3 源码精读

**① 用户入口** —— [rust/lancedb/src/connection.rs:803-822](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L803-L822)：

```rust
/// Set an option for the storage layer.
/// See available options at <https://docs.lancedb.com/storage/>
pub fn storage_option(mut self, key: impl Into<String>, value: impl Into<String>) -> Self {
    self.request.options.insert(key.into(), value.into());
    self
}
/// Set multiple options for the storage layer.
pub fn storage_options(mut self, pairs: impl IntoIterator<Item = (impl Into<String>, impl Into<String>)>) -> Self {
    for (key, value) in pairs {
        self.request.options.insert(key.into(), value.into());
    }
    self
}
```

中文说明：两个 builder 方法只是把键值对塞进 `request.options`，不做任何解释。`storage_option`（单个）和 `storage_options`（多个）是同一机制的两个粒度。

**② 剔除建表配置项，分离出 `storage_options`** —— [rust/lancedb/src/database/listing.rs:113-127](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/listing.rs#L113-L127)：

```rust
// We just assume that any options that are not new table config options are storage options
let storage_options = map.iter()
    .filter(|(key, _)| {
        key.as_str() != OPT_NEW_TABLE_STORAGE_VERSION
            && key.as_str() != OPT_NEW_TABLE_V2_MANIFEST_PATHS
            && key.as_str() != OPT_NEW_TABLE_ENABLE_STABLE_ROW_IDS
    })
    .map(|(key, value)| (key.clone(), value.clone()))
    .collect();
```

中文说明：把三个「新表配置键」过滤掉，剩下的统统归为 `storage_options`。这是一种「白名单排除」式的分类——简单且向后兼容（新增存储键无需改这里）。

**③ 合并与打包装进 `ObjectStoreParams`** —— `merge_storage_options` 把连接级和表级（`OpenTableBuilder` 可覆盖）的选项合并，见 [rust/lancedb/src/database/listing.rs:44-62](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/listing.rs#L44-L62)：

```rust
fn merge_storage_options(store_params: &mut ObjectStoreParams, pairs: ...) {
    let mut options = store_params.storage_options().cloned().unwrap_or_default();
    for (key, value) in pairs { options.insert(key, value); }
    let accessor = ... StorageOptionsAccessor::with_static_options(options);
    store_params.storage_options_accessor = Some(Arc::new(accessor));
}
```

中文说明：合并后的键值最终不是直接放进 `ObjectStoreParams.storage_options`，而是包进一个 `StorageOptionsAccessor`（访问器）。访问器支持「静态选项」与「动态提供者」两种模式。

**④ 动态凭证刷新** —— `set_storage_options_provider`，见 [rust/lancedb/src/database/listing.rs:64-73](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/listing.rs#L64-L73)，对应的 builder 方法是连接级的 `storage_options_provider`（[connection.rs:233-242](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L233-L242)）与表级的同名方法。它把一个 `StorageOptionsProvider` 挂到访问器上，使长任务能在凭证过期时自动刷新。

> **迁移提示**：早期版本的 `aws_creds(...)` 方法已被标记为 `#[deprecated]`，建议改用 `storage_options` 传递（见 [connection.rs:786-788](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/connection.rs#L786-L788)）。这正印证了「统一通道」取代「专用字段」的演进方向。

#### 4.2.4 代码实践

**实践目标**：感受 `storage_options` 是一条「不被解释、原样透传」的通道。

**操作步骤**：

1. 连接一个本地目录，故意塞进一个**自定义的、底层并不认识**的键值对：

```rust
// 示例代码：透传观察
use lancedb::connect;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let db = connect("/tmp/so_play")
        .storage_options([
            ("my_made_up_key", "hello"),
            // 真实场景这里会是 aws_access_key_id / endpoint_override 等
        ])
        .execute().await?;
    db.create_empty_table("t", arrow_schema::Schema::empty()).execute().await?;
    println!("ok, tables: {:?}", db.table_names().execute().await?);
    Ok(())
}
```

2. 阅读源码确认：你在 u2-l1 学到的 `connect(...).api_key(...)`、`.region(...)` 其实也是写进 `request.options`（键名由 `remote/db.rs` 约定），与 `storage_options` 走的是**同一条** `HashMap` 通道。

**需要观察的现象**：程序应当正常建表、列出表名，不会因为那个臆造的键而报错。

**预期结果**：本地后端会忽略不认识的键；这恰好说明 LanceDB 不校验键的合法性，而是把校验责任下放给真正认识它们的后端（S3 后端才会去读 `aws_*` 键）。

> **待本地验证**：对本地后端传入臆造键是否完全静默忽略，取决于 `object_store::local::LocalFileSystem` 的实现；若它对未知键报错，则该现象需以本地运行为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 LanceDB 用 `HashMap<String,String>` 而不是一个有明确字段的结构体来承载存储配置？
**答案**：因为配置项集合随云后端和版本演进，且不同后端要的键完全不同。用通用键值通道，LanceDB 永远不必为新配置项改代码或发版本，只需更新文档；真正的解释权下放给 `lance`/`object_store`。

**练习 2**：连接级的 `storage_options` 与表级 `OpenTableBuilder.storage_options(...)` 是什么关系？
**答案**：连接级选项会被该连接打开的所有表**继承**；表级 `storage_options` 可在单表上**覆盖**连接级（同名键以表级为准）。合并逻辑就在 4.2.3 的 `merge_storage_options` 中，其文档注释明确写道「Options already set on the connection will be inherited by the table, but can be overridden here」。

---

### 4.3 WrappingObjectStore 扩展点：写镜像与 io_tracking

#### 4.3.1 概念说明

LanceDB 在存储链上插入了最后一个扩展点：**`WrappingObjectStore`**。它来自 `lance::io`，接口极简——给定一个「真实后端」`target`，返回一个包了外壳的 `ObjectStore`：

```
wrap(store_prefix, target) -> Arc<dyn ObjectStore>
```

也就是说，在 `object_store::ObjectStore`（真实后端）和 `lance` 的读取逻辑之间，你可以塞进一层「装饰器」，拦截每一次读写。LanceDB 正是用这个机制实现了两个真实需求：

- **写镜像（`MirroringObjectStore`）**：把写操作同时复制一份到一个「更快但不够持久」的副存储——追求低延迟与高持久之间的平衡。这是**生产功能**。
- **IO 统计（`io_tracking`）**：统计读写了多少次、多少字节——用于性能测试与回归。这是**测试专用**（`#[cfg(test)]`）。

这两者都向我们展示了 `WrappingObjectStore` 的典型用法，是理解「如何在存储链中插入自定义行为」的最佳教材。

#### 4.3.2 核心流程

**写镜像的工作流**（见 `MirroringObjectStore`）：

```
写入一个对象 X
   ├── X 是 manifest(_latest.manifest) 吗？
   │     是 → 只写主存储（manifest 必须保证持久性）
   │     否 → 先写副存储（快），再写主存储（稳）   ← 双写
读取对象 X
   └── 只从主存储读（保证读到已提交的最新值）
删除对象 X
   └── 先删副存储（忽略 NotFound），再删主存储
```

设计要点：manifest 文件被特殊对待（`primary_only`），因为它是提交一致性的关键，绝不能只写到不够持久的副存储。

**IO 统计的工作流**（见 `IoTrackingStore`）：

```
每次 put_opt    → record_write(bytes)
每次 get_opt    → record_read(range.end - range.start)
每次 get_ranges → record_read(各段字节数之和)
multipart 分片  → 每个 put_part 记一次 write
所有计数累加进共享的 Mutex<IoStats>
读取统计时      → incremental_stats() 「取走」当前累计值并清零
```

`incremental_stats` 用 `std::mem::take` 把统计「取走」——这样调用方拿到的是「自上次调用以来的增量」，便于分段度量。

#### 4.3.3 源码精读

**① 写镜像的结构与文档** —— [rust/lancedb/src/io/object_store.rs:21-25](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/io/object_store.rs#L21-L25) 与 [rust/lancedb/src/io/object_store.rs:48-56](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/io/object_store.rs#L48-L56)：

```rust
#[derive(Debug)]
struct MirroringObjectStore {
    primary: Arc<dyn ObjectStore>,
    secondary: Arc<dyn ObjectStore>,
}
```
```rust
/// An object store that mirrors write to secondary object store first
/// and than commit to primary object store.
/// This is meant to mirror writes to a less-durable but lower-latency store.
/// We have primary store that is durable but slow, and a secondary
/// store that is fast but not as durable.
/// Note: this object store does not mirror writes to *.manifest files
```

中文说明：镜像存储持有 `primary`（持久但慢）与 `secondary`（快但不够持久）两个后端。文档点明动机与「manifest 不镜像」的约束。

**② 写入的双写逻辑** —— [rust/lancedb/src/io/object_store.rs:58-72](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/io/object_store.rs#L58-L72)：

```rust
async fn put_opts(&self, location: &Path, bytes: PutPayload, options: PutOptions) -> Result<PutResult> {
    if location.primary_only() {
        self.primary.put_opts(location, bytes, options).await
    } else {
        self.secondary.put_opts(location, bytes.clone(), options.clone()).await?;
        self.primary.put_opts(location, bytes, options).await
    }
}
```

中文说明：manifest（`primary_only`）只写主存储；其它对象先写副、再写主。注意 `bytes.clone()`/`options.clone()` 是因为要写两次。

**③ 为什么 manifest 特殊** —— [rust/lancedb/src/io/object_store.rs:42-46](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/io/object_store.rs#L42-L46)：

```rust
impl PrimaryOnly for Path {
    fn primary_only(&self) -> bool {
        self.filename().unwrap_or("") == "_latest.manifest"
    }
}
```

中文说明：文件名为 `_latest.manifest` 的就是 Lance 的提交指针，必须只落主存储以保持久性。

**④ 包装器的 `wrap` 实现** —— [rust/lancedb/src/io/object_store.rs:178-185](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/io/object_store.rs#L178-L185)：

```rust
impl WrappingObjectStore for MirroringObjectStoreWrapper {
    fn wrap(&self, _store_prefix: &str, primary: Arc<dyn ObjectStore>) -> Arc<dyn ObjectStore> {
        Arc::new(MirroringObjectStore { primary, secondary: self.secondary.clone() })
    }
}
```

中文说明：`wrap` 把传入的真实后端当作 `primary`，配上自己持有的 `secondary`，返回一个镜像存储。这正是「装饰器」的标准写法。

> **如何启用写镜像？** 通过在 URI 上加查询参数 `?mirrored_store=<副路径>`（仅本地、非 Windows）。解析逻辑见 [rust/lancedb/src/database/listing.rs:485-504](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/database/listing.rs#L485-L504)：它从 URL query 里捞出 `MIRRORED_STORE` 键，构造一个 `LocalFileSystem` 副存储，包成 `MirroringObjectStoreWrapper` 接入写路径。

**⑤ IO 统计是测试专用** —— [rust/lancedb/src/io/object_store.rs:18-19](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/io/object_store.rs#L18-L19)：

```rust
#[cfg(test)]
pub mod io_tracking;
```

中文说明：`io_tracking` 整个子模块只在测试构建里编译，**不是公开 API**。它是 LanceDB 自己做性能回归用的工具，但代码公开，是最好的 `WrappingObjectStore` 参考实现。

**⑥ 统计结构与会话级共享** —— [rust/lancedb/src/io/object_store/io_tracking.rs:18-24](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/io/object_store/io_tracking.rs#L18-L24) 与 [io_tracking.rs:47-51](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/io/object_store/io_tracking.rs#L47-L51)：

```rust
#[derive(Debug, Default)]
pub struct IoStats {
    pub read_iops: u64,   // 读次数
    pub read_bytes: u64,  // 读字节数
    pub write_iops: u64,
    pub write_bytes: u64,
}
impl IoStatsHolder {
    pub fn incremental_stats(&self) -> IoStats {
        std::mem::take(&mut self.0.lock().expect("failed to lock IoStats"))
    }
}
```

中文说明：`IoStats` 四个计数器；`IoStatsHolder` 内部是一个 `Arc<Mutex<IoStats>>`，`incremental_stats` 取走并清零，得到增量统计。`Arc` 让多个被包装的 store 共享同一份计数。

**⑦ 在读写处记账** —— [io_tracking.rs:84-92](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/io/object_store/io_tracking.rs#L84-L92)（写）与 [io_tracking.rs:106-113](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/io/object_store/io_tracking.rs#L106-L113)（读）：

```rust
async fn put_opts(&self, location: &Path, bytes: PutPayload, opts: PutOptions) -> OSResult<PutResult> {
    self.record_write(bytes.content_length() as u64);
    self.target.put_opts(location, bytes, opts).await
}
async fn get_opts(&self, location: &Path, options: GetOptions) -> OSResult<GetResult> {
    let result = self.target.get_opts(location, options).await;
    if let Ok(result) = &result {
        let num_bytes = result.range.end - result.range.start;
        self.record_read(num_bytes);
    }
    result
}
```

中文说明：记账发生在「转发给真实后端 `self.target` 前后」——先（或后）更新计数，再（或已）执行真正的 IO。注意 `get_opts` 只在成功时才记读字节数。

**⑧ 如何把包装器接进 LanceDB** —— 测试 `test_iops_open_strong_consistency` 给出了标准接线，见 [rust/lancedb/src/table/dataset.rs:568-594](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/dataset.rs#L568-L594)：

```rust
let io_stats = IoStatsHolder::default();
let table = db.create_empty_table("test", schema)
    .write_options(WriteOptions {
        lance_write_params: Some(WriteParams {
            store_params: Some(lance::io::ObjectStoreParams {
                object_store_wrapper: Some(Arc::new(io_stats.clone())),
                ..Default::default()
            }),
            ..Default::default()
        }),
    })
    .execute().await.unwrap();
io_stats.incremental_stats();        // 清零，丢弃建表期间的统计
table.schema().await.unwrap();       // 触发一次读
let stats = io_stats.incremental_stats();
assert_eq!(stats.read_iops, 1);      // 读 schema 只需 1 次 IO
```

中文说明：包装器通过 `WriteOptions.lance_write_params.store_params.object_store_wrapper` 接入。`WriteOptions` 的定义见 [rust/lancedb/src/table.rs:229-237](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L229-L237)。这个测试本身就是一个绝佳的「源码阅读型实践」（见 4.3.4）。

#### 4.3.4 代码实践

**实践目标**：跑通官方提供的 IO 统计测试，亲眼看到「读 schema 只花 1 次 IO」的回归断言；再理解如何写一个属于自己的 `WrappingObjectStore`。

**操作步骤**：

1. 在仓库根目录运行（参考 CLAUDE.md 的测试命令约定）：

```bash
cargo test --features remote -p lancedb \
  --lib table::dataset::tests::test_iops_open_strong_consistency -- --nocapture
```

2. 对照源码 [table/dataset.rs:568-594](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/dataset.rs#L568-L594) 阅读这条测试：它建表→清零统计→读一次 schema→断言 `read_iops == 1`。

3. **（进阶，示例代码）** 仿照 `io_tracking`，写一个只打印日志的最小包装器（标注为示例，非项目原有代码）：

```rust
// 示例代码：最小化的日志型 WrappingObjectStore（需依赖 lance 与 object_store 两个 crate）
use std::sync::Arc;
use lance::io::WrappingObjectStore;

pub struct LoggingWrapper;
impl WrappingObjectStore for LoggingWrapper {
    fn wrap(&self, _prefix: &str, target: Arc<dyn object_store::ObjectStore>) -> Arc<dyn object_store::ObjectStore> {
        println!("[io] wrapping object store");
        target   // 最简版：不改变行为，只演示接入点；可进一步包成自己的 ObjectStore 实现
    }
}
// 接线方式同上：ObjectStoreParams { object_store_wrapper: Some(Arc::new(LoggingWrapper)) }
```

**需要观察的现象**：测试通过，且 `--nocapture` 下能看到 `read_iops == 1` 被断言成立。

**预期结果**：`test_iops_open_strong_consistency` 通过——这证明 `WrappingObjectStore` 已正确插入存储链，`incremental_stats()` 捕获到了真实的读次数。

> **待本地验证**：`LoggingWrapper` 需在你的 `Cargo.toml` 里同时引入 `lancedb`、`lance`、`object_store` 三个 crate 才能编译；具体版本以仓库 `Cargo.lock` 锁定的为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `MirroringObjectStore` 对 `_latest.manifest` 只写主存储、不镜像？
**答案**：manifest 是 Lance 的提交指针，决定了「哪个版本是已提交的最新版本」。若它只写到不够持久的副存储就崩溃，会破坏提交一致性；因此它必须只落持久的主存储。读取也只从主存储读，确保读到已提交的最新值。

**练习 2**：`incremental_stats()` 为什么用 `std::mem::take` 而不是返回当前值的拷贝？
**答案**：`take` 会把内部统计「取走」并重置为零。这样每次调用得到的都是「自上次调用以来的增量」，便于分段度量（例如「建表后清零，再做一次查询，统计这次查询的 IO」）。若只返回拷贝，累计值会越来越大，难以隔离单次操作的开销。

**练习 3**：`io_tracking` 模块被 `#[cfg(test)]` 包住意味着什么？普通用户能用它吗？
**答案**：意味着它只在测试构建（`cargo test`）下编译，不在发布库里。普通用户无法 `use` 它。但 LanceDB 重新导出了底层的 `WrappingObjectStore`（经 `lance` crate）和 `ObjectStoreParams`，用户可以照着 `io_tracking` 的写法**自己实现**一个等价的包装器，通过 `write_options` 接入。

---

## 5. 综合实践

**任务**：把本讲三个要点（统一存储抽象 + `storage_options` 透传 + `WrappingObjectStore` 扩展）串成一个小实验，量化「读一次 schema 花了多少 IO」。

**步骤**：

1. 用 `storage_options` 连接 `memory://`（统一存储抽象的内存后端），并设置强一致性：

```rust
// 示例代码：综合实践骨架
use std::time::Duration;
use std::sync::Arc;
use lancedb::connect;

let db = connect("memory://")
    .storage_options([("allow_unsafe_rewrite", "true")]) // 演示透传：一个臆造键
    .read_consistency_interval(Duration::ZERO)
    .execute().await?;
```

2. 按 4.3.4 的方式，构造一个 IO 统计包装器（直接复用项目的 `IoStatsHolder`，需在测试上下文；或自行实现一个最小 `WrappingObjectStore`），通过 `WriteOptions { lance_write_params }` 接入，建一张带向量列的小表。

3. 建表后调用一次 `incremental_stats()` 清零；接着执行一次 `table.schema().await`；再调用 `incremental_stats()`，打印 `read_iops` 与 `read_bytes`。

4. 把 `schema()` 换成一次 `query().limit(10).execute()`，重复步骤 3，对比两次的 IO 次数差异，体会「读元数据」与「读数据」在存储层的开销区别。

**预期结果**：读 schema 的 `read_iops` 应当很小（官方断言为 1，见 [table/dataset.rs:592-594](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/dataset.rs#L592-L594)）；读数据行的 IO 次数会明显更多。

> **待本地验证**：具体数值取决于 Lance 版本与表大小；若你的 `read_iops` 不等于 1，请以本地实测为准并思考可能的原因（如缓存预热、版本检查策略变化）。

## 6. 本讲小结

- LanceDB 的 `io` 模块是「薄壳」——入口 [io.rs:4](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/io.rs#L4) 只声明一个子模块；真正的存储读写由 `lance` + `object_store` crate 负责。
- 统一的 `ObjectStore` trait 加 **URI 前缀分流**（`from_uri_and_params`）决定了走本地还是 S3/GS/Azure；后端开关由 `Cargo.toml` 的 feature 透传控制（`aws`/`gcs`/`azure` 等）。
- `storage_options` 是一条 `HashMap<String,String>` 透传通道：用户键值原样流过 LanceDB（剔除建表配置项后），交给底层后端解释；`StorageOptionsProvider` 还支持云凭证动态刷新。
- `WrappingObjectStore` 是存储链上的扩展点，用装饰器模式在真实后端外包一层；两个真实实现是写镜像 `MirroringObjectStore`（manifest 不镜像、双写、读主）与测试专用 IO 统计 `io_tracking`（`incremental_stats` 取走增量）。
- 包装器经 `WriteOptions.lance_write_params.store_params.object_store_wrapper` 接入；`Session` 与 `ObjectStoreRegistry` 被重新导出，供高级会话场景使用。

## 7. 下一步学习建议

- 本讲只覆盖了「本地/对象存储后端」的抽象。下一讲 **u6-l2（Database trait 与命名空间模型）** 会讲解 LanceDB 如何在存储之上组织「表的集合」，区分 listing（文件系统列举）与 namespace（多级命名空间）两种模式。
- 若你对远程云端感兴趣，可跳到 **u6-l3（远程后端：HTTP 客户端与重试）**，看 `RemoteDatabase`/`RemoteTable` 如何把同一套 API 转成 HTTP 往返——注意远程后端是 `remote` feature 控制的另一条链。
- 想深入存储本身的算法（如 Lance 的列式编码、版本提交），建议直接阅读 `lance` crate 的源码；本讲止于「LanceDB 如何转交」，已为你划清了边界。
