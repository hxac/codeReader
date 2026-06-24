# FoundationDB 客户端与事务封装

## 1. 本讲目标

本讲承接 u2-l1（服务骨架），深入 3FS 与 FoundationDB（下称 FDB）打交道的「最后一层胶水」。读完本讲，你应该能够：

1. 说出 3FS 如何把 FDB 的 C API 封装成 RAII 风格的 C++ 对象（`fdb::DB` / `fdb::Transaction`），以及它们如何用 `folly::coro::Task` 变成可 `co_await` 的协程。
2. 读懂 `IKVEngine` → `IReadOnlyTransaction` / `IReadWriteTransaction` 这套抽象接口，并解释「快照读」「冲突读」「读/写冲突范围」三者的区别。
3. 画出一次 meta 写事务在 FDB 中形成读/写冲突范围、提交失败后经 `FDBRetryStrategy` 自动退避重试的完整流程。

FDB 是 3FS 元数据（meta）与集群管理（mgmtd）路由信息的唯一事实来源，理解这一层是后续 u4（meta 服务）与 u3（mgmtd）的前提。

## 2. 前置知识

- **FoundationDB 是什么**：一个分布式、强一致（linearizable）、带 ACID 事务的 Key-Value 存储。它的核心抽象是「事务（transaction）」——一段对若干 key 的读 + 写操作，要么整体提交成功，要么整体失败。3FS 把所有元数据（inode、目录项、链表、配置版本……）都作为 KV 存在 FDB 里，从而让 meta / mgmtd 服务本身「无状态」。
- **乐观并发控制（OCC）**：FDB 用的是乐观事务。事务在提交时，FDB 会检查「这个事务读过的 key，从它读取之后到现在，有没有被别的事务改过」。如果被改过，就拒绝提交（`not_committed` / 冲突）。要避免「读到旧数据却以为是最新的」，事务必须主动声明「我关心这些 key」——也就是「冲突范围」。
- **协程基础**：本讲大量出现 `CoTryTask<T>`、`co_await`，它们是 3FS 基于 folly 协程封装的异步原语（见 u2-l3）。你可以暂时把 `co_await f()` 理解为「异步等待 FDB 返回结果，期间不阻塞线程」。
- **状态码命名约定**：3FS 用一组 `StatusCode`（见 `StatusCodeDetails.h`）统一所有错误。事务类错误归在 `1xxx` 段，命名空间是 `TransactionCode`，例如 `kConflict=1001`、`kTooOld=1003`、`kMaybeCommitted=1006`。本讲会反复用到它们。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/fdb/FDB.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDB.h) | 把 FDB C API 封装成 RAII 的 `fdb::DB` 与 `fdb::Transaction`，以及把 `FDBFuture*` 转成协程 `Task` 的 `Result` 模板。 |
| [src/fdb/FDB.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDB.cc) | `DB` / `Transaction` 的实现：网络线程启动、`commit`、`onError`、`addConflictRange` 等转发到 C API。 |
| [src/fdb/FDBContext.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDBContext.cc) | 进程级 FDB 引导：选定 API 版本、设置网络选项、拉起 `fdb_run_network` 网络线程、按 cluster file 创建 `DB`。 |
| [src/common/kv/IKVEngine.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/kv/IKVEngine.h) | 最底层抽象：一个 KV 引擎能「创建只读事务」与「创建读写事务」。 |
| [src/common/kv/ITransaction.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/kv/ITransaction.h) | 事务的抽象接口 `IReadOnlyTransaction` / `IReadWriteTransaction`，以及 `TransactionHelper` 工具函数。 |
| [src/common/kv/ITransaction.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/kv/ITransaction.cc) | `TransactionHelper` 实现：错误归类、是否可重试、`keyAfter`、按前缀列举等。 |
| [src/fdb/FDBKVEngine.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDBKVEngine.h) | FDB 对 `IKVEngine` 的实现：用 `fdb::DB` 造出 `FDBTransaction`。 |
| [src/fdb/FDBTransaction.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDBTransaction.h) / [.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDBTransaction.cc) | FDB 对 `IReadWriteTransaction` 的实现：读、写、冲突范围、提交、`onError`，以及 FDB 错误码到 `TransactionCode` 的映射。 |
| [src/fdb/FDBRetryStrategy.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDBRetryStrategy.h) | 事务重试策略：指数退避、交给 FDB `onError` 退避或本地 fallback 退避。 |
| [src/common/utils/StatusCodeDetails.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/StatusCodeDetails.h) | 所有状态码定义，事务错误在 `1xxx` 段。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**① FDB 封装**（C API → C++ RAII → 协程）、**② 事务抽象**（`IKVEngine`/`ITransaction` 接口与冲突范围语义）、**③ 重试策略**（`FDBRetryStrategy` 与 meta 的重试主循环）。

### 4.1 FDB 封装：把 C API 变成可 `co_await` 的 RAII 对象

#### 4.1.1 概念说明

FDB 官方只提供 C API（`fdb_c.h`），它的编程模型是「返回一个 `FDBFuture*`，你给它注册回调，回调里再取结果」。这种风格写起来很啰嗦，也容易忘记释放资源。3FS 在 `src/fdb/FDB.h` 里做了一层薄而关键的封装，目标是：

- **资源安全**：`FDBDatabase*`、`FDBTransaction*`、`FDBFuture*` 都用 `std::unique_ptr` + 自定义 deleter 管理，析构即释放。
- **协程友好**：每个会等待的操作（`get` / `commit` / `onError` …）都返回 `folly::coro::Task<...Result>`，调用方可以直接 `co_await`。
- **错误统一**：把 `fdb_error_t`（一个 `int`）收敛进 3FS 自己的 `Result` / `Status` 体系。

封装分三层对象：进程级的网络与 `fdb::DB`、事务对象 `fdb::Transaction`、以及承载异步结果的 `Result` 模板。

#### 4.1.2 核心流程

一次「打开 DB → 建事务 → get → 拿到值」的流程：

```text
进程启动:
  FDBContext 构造
    -> DB::selectAPIVersion(710)        # 锁定 wire 协议版本
    -> DB::setNetworkOption(...)        # 外部 client、trace、退避等
    -> DB::setupNetwork()               # 初始化 FDB 网络线程运行时
    -> 新起 std::thread { DB::runNetwork() }   # 真正跑事件循环，阻塞直到 stopNetwork

需要 DB 时:
  FDBContext::getDB()
    -> fdb_create_database(clusterFile)  # 读 cluster file 连接到 FDB 集群
    -> 返回 fdb::DB（持 FDBDatabase*）

每次事务:
  fdb::Transaction tr(db)
    -> fdb_database_create_transaction  # 造一个 FDB 事务句柄
  co_await tr.get(key)
    -> fdb_transaction_get  返回 FDBFuture*
    -> Result::toTask(future)
         注册 coroCallback -> Baton.post()
         co_await baton      # 挂起协程，等 FDB 网络线程回调
         extractValue()      # fdb_future_get_value 取出字节
```

注意一个常被忽略的点：**FDB 的网络是「自己跑在一个线程里」的**。`fdb_run_network()` 必须一直阻塞运行，所有 `FDBFuture` 的完成通知都从那个网络线程来；3FS 在 `FDBContext` 构造时就把它起好了。

#### 4.1.3 源码精读

**进程级引导**（[src/fdb/FDBContext.cc:L28-L82](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDBContext.cc#L28-L82)）：构造函数里依次 `selectAPIVersion` → 按配置设置若干 `FDB_NET_OPTION_*` → `setupNetwork()` → 起一个名为 `fdb_net` 的线程跑 `runNetwork()`。`#define FDB_API_VERSION 710` 写在 [FDB.h:L13](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDB.h#L13)。`getDB()` 则按 cluster file 创建 `fdb::DB`：

```cpp
// src/fdb/FDBContext.cc:92-100
DB FDBContext::getDB() const {
  DB db(config_.clusterFile(), config_.readonly());   // fdb_create_database
  CHECK_FDB_ERR(db.error(), "Failed to get fdb::DB instance.");
  if (config_.casual_read_risk()) {
    CHECK_FDB_ERR(db.setOption(FDB_DB_OPTION_TRANSACTION_CAUSAL_READ_RISKY), ...);
  }
  return db;
}
```

> `readonly()` 很关键：它最终会阻止 `commit`（见 4.1.3 末尾），admin_cli / fsck 等只读工具据此防误写。

**`fdb::DB` 与 `fdb::Transaction` 是 RAII 包装**（[src/fdb/FDB.h:L92-L140](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDB.h#L92-L140) 是 `DB`，[L142-L204](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDB.h#L142-L204) 是 `Transaction`）。两者的核心都是「持有一个 C 句柄 + 自定义 deleter」：

```cpp
// src/fdb/FDB.h:130-140（节选）
struct FDBDatabaseDeleter {
  void operator()(FDBDatabase *db) const { db ? fdb_database_destroy(db) : void(); }
};
std::unique_ptr<FDBDatabase, FDBDatabaseDeleter> db_;
```

`Transaction` 的方法基本是一行转发到 C API，例如 `commit`（[src/fdb/FDB.cc:L246-L255](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDB.cc#L246-L255)）：

```cpp
// src/fdb/FDB.cc:246-255
Task<EmptyResult> Transaction::commit() {
  if (UNLIKELY(readonly_)) {
    // Prevent tools like admin_cli or fsck from mistakenly modifying data
    XLOGF(CRITICAL, "disallow call commit on a read-only FDBContext!!!");
    EmptyResult result;
    result.error_ = 1000; /* operation failed */
    co_return result;
  }
  co_return co_await EmptyResult::toTask(fdb_transaction_commit(tr_.get()));
}
```

> 注意 `readonly_` 的拦截：这就是 admin_cli 不会误改数据的保护。`fdb_transaction_commit` 仍返回 `FDBFuture*`，交给 `EmptyResult::toTask`。

**`Result` 模板把 `FDBFuture*` 变成协程**（[src/fdb/FDB.h:L58-L90](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDB.h#L58-L90) 声明，[src/fdb/FDB.cc:L111-L139](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDB.cc#L111-L139) 实现）。这是整层封装的「心脏」：

```cpp
// src/fdb/FDB.cc:111-139（关键片段）
template <class T, class V>
Task<T> Result<T, V>::toTask(FDBFuture *f) {
  T result;
  result.future_.reset(f);
  folly::coro::Baton baton;
  result.error_ = fdb_future_set_callback(f, coroCallback, &baton);  // 注册回调
  ...
  co_await baton;                 // FDB 网络线程 post() 后从这里恢复
  ...
  result.error_ = fdb_future_get_error(f);
  ...
  result.extractValue();          // 子类特化：取 int64 / key / value / kv 数组
  co_return result;
}
```

回调 `coroCallback` 极简——`baton.post()` 唤醒协程（[src/fdb/FDB.cc:L106-L109](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDB.cc#L106-L109)）。每个返回类型有对应的 `extractValue` 特化，例如取值（[src/fdb/FDB.cc:L34-L43](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDB.cc#L34-L43)）调用 `fdb_future_get_value`。

由此派生出若干类型别名（[src/fdb/FDB.h:L82-L90](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDB.h#L82-L90)）：`Int64Result`、`ValueResult`、`KeyValueArrayResult`、`EmptyResult` 等，分别对应 get/getRange/commit/onError 的返回。

#### 4.1.4 代码实践

**实践目标**：理解「FDB 网络线程必须先起来，事务才能跑通」这一前提，以及 `readonly` 拦截。

**操作步骤**（源码阅读型实践，无需真实 FDB 集群）：

1. 打开 [src/fdb/FDBContext.cc:L76-L81](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDBContext.cc#L76-L81)，确认 `setupNetwork()` 之后立刻 `std::thread{runNetwork}`。思考：如果把这两行顺序反过来（先起线程跑 `runNetwork`，再 `setupNetwork`）会怎样？——`runNetwork` 会报错，因为运行时尚未初始化。
2. 打开 [src/fdb/FDB.cc:L246-L255](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDB.cc#L246-L255)，追踪 `readonly_` 是怎么被置位的：它来自 `fdb::Transaction(DB &db)` 构造时拷贝的 `db.readonly()`（[FDB.h:L144-L151](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDB.h#L144-L151)），而 `DB::readonly_` 来自 `FDBContext::getDB` 里 `config_.readonly()`。
3. 用 `Grep` 全仓库搜索 `setReadonly(true)`（[FDBKVEngine.h:L39](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDBKVEngine.h#L39)），找到谁会把引擎切到只读（admin_cli 启动 / fsck）。

**需要观察的现象**：只读引擎下任何 `commit` 都不会真正调用 `fdb_transaction_commit`，而是直接返回 `error_=1000` 并打 `CRITICAL` 日志。

**预期结果**：你能口述出「FDB 封装层 = 进程级网络引导 + RAII 句柄 + 协程化 Future」三件套，并解释 `readonly` 防误写发生在 `Transaction::commit` 的入口。

#### 4.1.5 小练习与答案

**练习 1**：`fdb::Transaction::get` 返回的 `Task<ValueResult>`，如果 FDB 报错（比如 `transaction_too_old`），错误信息存在 `Result` 的哪个字段？调用方怎么拿到？
**答案**：存在 `Result::error_`（[FDB.h:L78](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDB.h#L78)），通过 `ValueResult::error()` 读取（[FDB.h:L61](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDB.h#L61)）。注意 `toTask` 在出错时不会 `extractValue`，直接 `co_return result`（[FDB.cc:L132-L135](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDB.cc#L132-L135)）。

**练习 2**：为什么 3FS 要把 `coroCallback` 写成只 `baton.post()` 一行，而不在回调里直接取值？
**答案**：回调跑在 FDB 的网络线程上，应尽快返回；真正的 `fdb_future_get_*` 取值放在协程恢复后的 `extractValue()` 里执行，避免阻塞网络线程，也便于把取值错误纳入协程的正常错误处理路径。

### 4.2 事务抽象：`IKVEngine` / `ITransaction` 与冲突范围

#### 4.2.1 概念说明

3FS 不想让上层（meta、mgmtd）直接依赖 FDB——测试时希望能用内存 KV（`MemKV`）替换 FDB。于是它抽象出两层接口：

- [`IKVEngine`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/kv/IKVEngine.h)：一个引擎，能造只读事务和读写事务。
- [`IReadOnlyTransaction`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/kv/ITransaction.h#L34-L86) / [`IReadWriteTransaction`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/kv/ITransaction.h#L88-L112)：事务本身能读、能写、能提交。

这里有几个对理解 3FS 元数据并发至关重要的术语：

- **快照读 `snapshotGet`**：读一个 key 的当前值，但**不**记入冲突范围。它不会让本事务因这个 key 而失败，但代价是你读到的可能是「提交时刻已被改过」之前的快照。适合「我只是看看，不强求最新」的场景。
- **冲突读 `get`**：读的同时把这个 key 记入「读冲突范围」。提交时 FDB 会保证「我读到的值在整个事务期间没被别人改过」，否则提交失败（冲突）。适合「读后写」（read-modify-write）场景。
- **读冲突范围 / 写冲突范围**：FDB 用 key range（左闭右开）来界定关心范围。一个事务读哪些 range、写哪些 range，决定了它和别的事务是否会「撞」。

> 一句话区分：`snapshotGet` = 看一眼不管；`get`/`addReadConflict` = 我要基于这个值做决定，谁改了它就让我重试。

#### 4.2.2 核心流程

一次 meta 写事务（如 `unlink`）在接口层的标准用法：

```text
auto txn = engine.createReadWriteTransaction();      // IKVEngine -> IReadWriteTransaction

// 1. 解析路径：用 snapshotGet 读父目录 inode（只看不改）
// 2. 读后写：get(entry) 或 addReadConflict(entryKey)
//      -> 记入读冲突范围，保证别人改了就冲突
// 3. 写：txn.set(inodeKey, newInodeBytes) / txn.clear(entryKey)
//      -> FDB 自动记入写冲突范围
// 4. co_await txn.commit()
//      -> FDB 检查读冲突范围未被改动 + 应用写集 -> 提交或 not_committed
```

FDB 的乐观并发判断可以粗略写成：

\[
\text{commit ok} \iff \forall r \in \text{readRanges},\; \text{version}(r)\text{ 在事务期间未前进}
\]

而写操作（`set`/`clear`）默认会把自己涉及的 key 加入「写冲突范围」，所以**通常不需要显式加写冲突范围，只需关心读冲突范围**。

#### 4.2.3 源码精读

**`IKVEngine` 极简**（[src/common/kv/IKVEngine.h:L8-L15](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/kv/IKVEngine.h#L8-L15)）——只有两个纯虚方法：

```cpp
class IKVEngine {
 public:
  virtual std::unique_ptr<IReadOnlyTransaction> createReadonlyTransaction() = 0;
  virtual std::unique_ptr<IReadWriteTransaction> createReadWriteTransaction() = 0;
};
```

**`IReadOnlyTransaction` / `IReadWriteTransaction` 的关键区别**在注释里点明（[src/common/kv/ITransaction.h:L92-L95](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/kv/ITransaction.h#L92-L95)）：

```cpp
// The difference of `snapshotGet` and `get` is the former needs no conflict
// validation and hence won't cause a read-write transaction fail.
CoTryTask<std::optional<String>> get(std::string_view key) override = 0;
...
virtual CoTryTask<void> addReadConflict(std::string_view key) = 0;
virtual CoTryTask<void> addReadConflictRange(std::string_view begin, std::string_view end) = 0;
```

`IReadOnlyTransaction` 还提供了两个常用工具：`KeySelector`（[ITransaction.h:L62-L69](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/kv/ITransaction.h#L62-L69)，用 key + 是否包含来描述范围端点）和 `GetRangeResult`（[L71-L78](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/kv/ITransaction.h#L71-L78)，带 `hasMore` 支持分页）。

**FDB 对这两个接口的实现**：`FDBKVEngine`（[src/fdb/FDBKVEngine.h:L18-L42](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDBKVEngine.h#L18-L42)）造事务：

```cpp
// src/fdb/FDBKVEngine.h:23-31
std::unique_ptr<IReadOnlyTransaction> createReadonlyTransaction() override {
  return createReadWriteTransaction();   // 注意：只读也造读写事务
}
std::unique_ptr<IReadWriteTransaction> createReadWriteTransaction() override {
  fdb::Transaction tr(db_);
  if (UNLIKELY(tr.error())) return nullptr;
  return std::make_unique<FDBTransaction>(std::move(tr));
}
```

> 一个反直觉点：3FS 的「只读事务」在 FDB 后端下其实也是一个 `FDBTransaction`（潜在可写），只是上层承诺不调 `commit`/`set`。这样的好处是只读路径和读写路径共用同一套实现，切换成本低。

**`FDBTransaction` 怎么实现快照读 vs 冲突读**：差别只在传给 `fdb_transaction_get` 的 `snapshot` 参数（[src/fdb/FDBTransaction.cc:L246-L301](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDBTransaction.cc#L246-L301)）：

```cpp
// src/fdb/FDBTransaction.cc:246-254（冲突读 get）
CoTryTask<std::optional<String>> FDBTransaction::get(std::string_view key) {
  ...
  auto result = co_await tr_.get(key);              // snapshot=false
  ...
}
// src/fdb/FDBTransaction.cc:293-301（快照读 snapshotGet）
CoTryTask<std::optional<String>> FDBTransaction::snapshotGet(std::string_view key) {
  ...
  auto result = co_await tr_.get(key, /* snapshot = */ true);   // 不进冲突范围
  ...
}
```

**显式加读冲突范围**：`addReadConflict` 把单个 key 扩成一个 `[key, keyAfter(key))` 的 range（左闭右开），再交给 FDB（[src/fdb/FDBTransaction.cc:L303-L324](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDBTransaction.cc#L303-L324)）：

```cpp
// src/fdb/FDBTransaction.cc:303-313
CoTryTask<void> FDBTransaction::addReadConflict(std::string_view key) {
  ...
  String endKey = TransactionHelper::keyAfter(key);      // key 后追加一个 '\0'
  fdb::KeyRangeView range{key, endKey};
  auto result = tr_.addConflictRange(range, FDBConflictRangeType::FDB_CONFLICT_RANGE_TYPE_READ);
  ...
}
```

`keyAfter` 的实现就是「在 key 末尾追加 `\0`」让 range 严格覆盖该 key（[src/common/kv/ITransaction.cc:L33-L40](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/kv/ITransaction.cc#L33-L40)）。最终落到 C API `fdb_transaction_add_conflict_range`（[src/fdb/FDB.cc:L279-L281](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDB.cc#L279-L281)）。

**真实用法**：meta 在「读后写」前都会显式 `addIntoReadConflict`。例如 `Remove` 操作删除目录项和 inode 前（[src/meta/store/ops/Remove.cc:L153-L156](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/Remove.cc#L153-L156)）：

```cpp
CO_RETURN_ON_ERROR(co_await entry.addIntoReadConflict(txn));   // 目录项进读冲突范围
CO_RETURN_ON_ERROR(co_await inode.addIntoReadConflict(txn));   // inode 进读冲突范围
CO_RETURN_ON_ERROR(co_await entry.remove(txn));                // clear（自动进写冲突范围）
CO_RETURN_ON_ERROR(co_await inode.remove(txn));
```

而 `DirEntry::addIntoReadConflict` 内部就是调 `txn.addReadConflict(packKey())`（[src/meta/store/DirEntry.h:L78-L83](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/DirEntry.h#L78-L83)）。这样：如果在你读 entry 之后、提交之前，有人改了这个 entry（比如并发 `rename`），FDB 提交时就会发现 entry 的读冲突范围被破坏，回 `not_committed`，于是触发重试（见 4.3）。

#### 4.2.4 代码实践

**实践目标**：能在一篇说明里讲清「一个 meta 写事务在 FDB 里形成了哪些读/写冲突范围」。

**操作步骤**：

1. 打开 [src/meta/store/ops/Remove.cc:L145-L162](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/Remove.cc#L145-L162)（删空目录分支）。
2. 列出本事务涉及的 key（用 `packKey()` 的语义）：父目录项 DENT、被删目录的 inode INOD。
3. 标注每个 key 是「读冲突范围」（`addIntoReadConflict`）还是「写冲突范围」（`remove` → `clear`）。
4. 回答：如果两个客户端同时 `unlink` 同一个文件，第二个提交时会发生什么？——它的读冲突范围（该 entry）已被第一个事务的写改动，FDB 返回 `not_committed` → `TransactionCode::kConflict` → 重试，重试时 `resolve` 发现 entry 已不存在，返回 `kNotFound`。

**需要观察的现象**：并发删除同一文件时，不报数据损坏，而是先冲突重试、再得到确定的「不存在」结果。

**预期结果**：写出一段说明（参考 4.2.2 流程图），明确「`snapshotGet` 不进冲突范围、`get`/`addReadConflict` 进读冲突范围、`set`/`clear` 进写冲突范围」三类语义，并能指出 Remove 事务各 key 的归属。**若无法本地跑 FDB，明确标注「待本地验证」并发场景。**

#### 4.2.5 小练习与答案

**练习 1**：`FDBKVEngine::createReadonlyTransaction` 返回的其实是 `FDBTransaction`（可写）。这样会不会让「只读」事务误写？
**答案**：不会破坏正确性，但依赖上层自律。真正的硬保护在 `fdb::Transaction::commit` 里对 `readonly_` 的拦截（[FDB.cc:L247-L253](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDB.cc#L247-L253)）——只读引擎下 `commit` 直接失败。

**练习 2**：`TransactionHelper::keyAfter("abc")` 返回什么？为什么用它做 range 右端？
**答案**：返回 `"abc\0"`（[ITransaction.cc:L33-L40](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/kv/ITransaction.cc#L33-L40)）。FDB range 是左闭右开 `[begin, end)`，要让范围恰好覆盖 key 本身且不包含 `key+"任何后缀"` 的更长 key，最稳妥就是把 key 末尾加一个 `\0` 作为 end，这样 `key < end` 但 `key+'x' >= end` 不成立的前提是字节序——`\0` 是最小的可追加字节。

**练习 3**：什么时候该用 `snapshotGet` 而不是 `get`？
**答案**：当读的值只用于「展示/判断」而不参与本事务的写决策、且能容忍读到稍旧版本时用快照读，能减少不必要的冲突、提高并发度。比如 `listdir` 只读列举；而「读 inode → 改 nlink → 写回」必须用 `get`/`addReadConflict`。

### 4.3 重试策略：`FDBRetryStrategy` 与冲突重试

#### 4.3.1 概念说明

乐观并发注定会有冲突——两个事务同时改重叠的 key，必有一个提交失败。FDB 的设计哲学是「冲突不可怕，重试就好」，并提供了一个标准接口 `fdb_transaction_on_error(err)`：它接收一个错误码，内部判断该错误是否可重试，若是则做合适的退避并重置事务状态，返回 success 让你重跑事务体。

3FS 在 [`FDBRetryStrategy`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDBRetryStrategy.h) 里做了两件事：

1. **错误归类**：把 FDB 的 `fdb_error_t` 映射成 3FS 的 `TransactionCode`（`convertError`），并判断哪些可重试。
2. **退避调度**：优先把退避决策交给 FDB 的 `onError`（它最懂自己），不行再用本地指数退避兜底。

同时它还处理一个微妙情况——`maybe_committed`：事务可能已经提交成功，只是结果丢失（网络抖动）。对这类错误是否重试，取决于业务能否容忍「同一个写执行两次」（幂等）。3FS 用 `retryMaybeCommitted` 开关控制。

#### 4.3.2 核心流程

meta 的重试主循环（在 `OperationDriver::run`，[src/meta/store/Operation.h:L156-L208](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/Operation.h#L156-L208)）是理解整条重试链的最佳入口：

```text
strategy.init(txn)                      # 设置 MAX_RETRY_DELAY，重置计数
while (true):
  if 超过 deadline: break               # 4.3 里最终的超时保护
  result = runAndCommit(txn, op)        # 跑事务体 + co_await txn.commit()
  if result 成功: break
  op.retry(result.error())              # 业务侧清理（清空已记的 events）
  retry = strategy.onError(txn, error)  # 关键：决定退避 & 是否继续
  if retry 出错: break                  # 不可重试 / 重试次数耗尽
  recorder.retry()++
return result
```

`strategy.onError` 内部（[src/fdb/FDBRetryStrategy.h:L59-L109](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDBRetryStrategy.h#L59-L109)）：

```text
onError(txn, error):
  if retry >= maxRetryCount(默认10): 直接返回错误
  if 不是事务类错误(isTransactionError==false): 直接返回错误
  // 进 fdbBackoff:
  errcode = txn.errcode()
  predicate = retryMaybeCommitted ? RETRYABLE : RETRYABLE_NOT_COMMITTED
  if fdb_error_predicate(predicate, errcode) 为假: 不可重试，返回错误
  ok = co_await txn.onError(errcode)     # 交给 FDB 自己退避/重置
  if !ok: 返回错误
  // SCOPE_EXIT: backoff = min(maxBackoff, backoff*2); retry++
  return Void  # 继续下一轮 while
```

退避时间的演化（本地 fallback 分支，[L123](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDBRetryStrategy.h#L123)）是带抖动的指数退避：

\[
\text{backoff}_{n+1} = \min(\text{maxBackoff},\; 2 \cdot \text{backoff}_n),\quad \text{backoff}_0 = 10\text{ms}
\]
\[
\text{sleep} = \frac{\text{backoff}}{100} \times \text{Uniform}(80,120)
\]

抖动（80%–120%）是为了避免多个冲突事务在同一时刻一起重试再次撞车。

#### 4.3.3 源码精读

**错误码映射 `convertError`**（[src/fdb/FDBTransaction.cc:L162-L204](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDBTransaction.cc#L162-L204)）把 FDB 的 `fdb_error_t` 翻成 3FS 的 `TransactionCode`。最关键的几条：

```cpp
// src/fdb/FDBTransaction.cc:166-184（节选）
case error_code_not_committed:              return TransactionCode::kConflict;       // 冲突，必重试
case error_code_commit_unknown_result:      return TransactionCode::kMaybeCommitted; // 可能已提交
case error_code_transaction_too_old:        return TransactionCode::kTooOld;          // 5s 前的版本太老
case error_code_batch_transaction_throttled:
case error_code_tag_throttled:              return TransactionCode::kThrottled;       // 被限流
```

这些 `TransactionCode` 的数值定义在 [src/common/utils/StatusCodeDetails.h:L74-L85](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/StatusCodeDetails.h#L74-L85)（`kConflict=1001`、`kMaybeCommitted=1006`、`kTooOld=1003` 等），都落在 `1xxx` 事务段。

**哪些错误算「事务错误」、哪些「可重试」**（[src/common/kv/ITransaction.cc:L8-L31](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/kv/ITransaction.cc#L8-L31)）：

```cpp
// src/common/kv/ITransaction.cc:8-10
bool TransactionHelper::isTransactionError(const Status &error) {
  return StatusCode::typeOf(error.code()) == StatusCodeType::Transaction;   // 1xxx 段
}
// src/common/kv/ITransaction.cc:12-31
bool TransactionHelper::isRetryable(const Status &error, bool allowMaybeCommitted) {
  switch (error.code()) {
    case kConflict: case kThrottled: case kTooOld: case kRetryable:
    case kResourceConstrained: case kProcessBehind: case kFutureVersion:
      return true;
    case kMaybeCommitted: return allowMaybeCommitted;   // 由开关决定
    case kNetworkError: case kCanceled: return false;
  }
  return false;
}
```

> 注意 `kNetworkError`/`kCanceled` 被判为「不可重试」——网络错误交给 FDB `onError` 内部处理更合适，不应在本地盲目重跑。

**`FDBRetryStrategy::onError` 的双路退避**（[src/fdb/FDBRetryStrategy.h:L59-L126](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDBRetryStrategy.h#L59-L126)）。优先走 `fdbBackoff`：用 `fdb_error_predicate` 判断可重试性，再 `co_await txn.onError(errcode)` 把退避交给 FDB（[L85-L109](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDBRetryStrategy.h#L85-L109)）。`FDBTransaction::onError` 转发到 `fdb_transaction_on_error` 并记录冲突/其他重试计数与退避时延（[src/fdb/FDBTransaction.cc:L404-L414](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDBTransaction.cc#L404-L414)）：

```cpp
// src/fdb/FDBTransaction.cc:404-414
CoTask<bool> FDBTransaction::onError(fdb_error_t errcode) {
  if (errcode == error_code_not_committed) retryConflict.addSample(1);   // 冲突计数
  else retryOther.addSample(1);                                          // 其他重试计数
  auto begin = SteadyClock::now();
  auto ret = co_await tr_.onError(errcode);                              // fdb_transaction_on_error
  retryBackoff.addSample(SteadyClock::now() - begin);                    // 退避时长埋点
  co_return ret.error() == 0;
}
```

`predicate` 的选择依赖 `retryMaybeCommitted`（[src/fdb/FDBRetryStrategy.h:L96-L97](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDBRetryStrategy.h#L96-L97)）：`RETRYABLE` 会把「可能已提交」也算可重试，`RETRYABLE_NOT_COMMITTED` 不会。这个开关在 `OperationDriver::run` 里由 `operation_.retryMaybeCommitted()` 设置（[Operation.h:L160](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/Operation.h#L160)）——只有幂等操作才允许对 `maybe_committed` 重试。

**`init` 阶段设置 FDB 的最大退避**（[src/fdb/FDBRetryStrategy.h:L40-L57](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDBRetryStrategy.h#L40-L57)）把 `config_.maxBackoff` 通过 `FDB_TR_OPTION_MAX_RETRY_DELAY` 告诉 FDB，让 FDB 自己的 `onError` 退避不超过这个上限；同时把本地 `retry_`、`backoff_` 复位。

**重试次数与超时的双保险**：`FDBRetryStrategy::Config` 默认 `maxRetryCount=10`、`maxBackoff=1s`（[L27-L31](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDBRetryStrategy.h#L27-L31)）；而 `OperationDriver::run` 还有 `deadline_` 做最终超时（[Operation.h:L180-L183](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/Operation.h#L180-L183)），超时返回 `MetaCode::kOperationTimeout`。两者共同保证「既不会无限重试，也不会把单个请求拖太久」。

#### 4.3.4 代码实践

**实践目标**：能讲清「一个 meta 写事务冲突时如何被自动重试」，并定位监控指标。

**操作步骤**：

1. 打开重试主循环 [src/meta/store/Operation.h:L176-L198](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/Operation.h#L176-L198)，对照 4.3.2 的伪代码走一遍：`runAndCommit` 失败 → `operation_.retry` 清理 → `strategy.onError` 退避 → 回到循环顶。
2. 理解 `runAndCommit` 的幂等保护（[Operation.h:L221-L250](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/Operation.h#L221-L250)）：对带 `clientUuid/requestUuid` 的请求，提交前先写幂等记录（`Idempotent::store`），这样即使 `maybe_committed` 后重试，也能返回首次的结果而不重复执行副作用。
3. 用 `Grep` 查 `fdb.retry_conflict` / `fdb.retry_other` / `fdb.retry_backoff`（[FDBTransaction.cc:L151-L153](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDBTransaction.cc#L151-L153)），这是观察冲突频率的监控埋点。
4. 若有本地 FDB 集群：用两个 `admin_cli`/脚本同时对同一目录并发 `unlink` 同一文件，观察 meta 日志中的 `Transaction error` / `Request ... failed, error ... conflict` 与重试记录。**无集群则标注「待本地验证」。**

**需要观察的现象**：高并发下 meta 日志出现 `TransactionCode::kConflict`，但请求最终成功（或返回确定的 `kNotFound`），不会出现数据不一致。

**预期结果**：写出一段说明，覆盖「冲突→`not_committed`→`kConflict`→`isRetryable=true`→`onError` 退避→重跑事务体」整条链路，并指出 `retryMaybeCommitted` 只对幂等操作开启。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `kConflict` 一定可重试，而 `kMaybeCommitted` 要看开关？
**答案**：`kConflict`（`not_committed`）意味着事务**确定没提交**，重跑安全；`kMaybeCommitted`（`commit_unknown_result`）意味着**可能已提交**，盲目重跑会重复执行副作用，只有幂等操作（3FS 用 `Idempotent` 记录去重）才允许，故由 `retryMaybeCommitted` 控制（[ITransaction.cc:L23-L24](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/kv/ITransaction.cc#L23-L24)）。

**练习 2**：`FDBRetryStrategy` 同时存在 `fdbBackoff` 和 `defaultBackoff` 两条路，何时走哪条？
**答案**：当传入的 `txn` 是真正的 `FDBTransaction` 时走 `fdbBackoff`，把退避完全交给 `fdb_transaction_on_error`（[FDBRetryStrategy.h:L77-L83](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDBRetryStrategy.h#L77-L83)）；否则（如测试用的 `MemTransaction`）走 `defaultBackoff`，用本地 `sleep(backoff*抖动)` 兜底（[L111-L126](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDBRetryStrategy.h#L111-L126)）。

**练习 3**：`OperationDriver::run` 里既有 `maxRetryCount` 又有 `deadline_`，两者是什么关系？
**答案**：`maxRetryCount`（默认 10）限制重试次数，`deadline_` 限制总时长（超时返回 `kOperationTimeout`）。循环里每轮先查 `deadline_`（[Operation.h:L180-L183](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/Operation.h#L180-L183)），是兜底；`maxRetryCount` 在 `onError` 内（[FDBRetryStrategy.h:L61-L64](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDBRetryStrategy.h#L61-L64)）。先到哪个就停。

## 5. 综合实践

**任务**：以 `Remove`（删除文件/目录）为样本，画一张「从 RPC 到 FDB 提交（含冲突重试）」的完整时序图，并写一份 300 字说明。

要求覆盖：

1. **分层穿越**：`MetaOperator` → `OperationDriver::run` → `RemoveOp::run(IReadWriteTransaction&)` → `FDBTransaction` → `fdb::Transaction` → C API。
2. **冲突范围**：在 `Remove` 事务里标出哪些 key 进读冲突范围（`addIntoReadConflict`）、哪些进写冲突范围（`remove`→`clear`），参考 [Remove.cc:L153-L156](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/Remove.cc#L153-L156)。
3. **失败重试**：假设提交时另一个并发 `rename` 改了同一个 entry，画出 `not_committed → convertError(kConflict) → isTransactionError → onError → 退避 → 重跑` 的路径，引用 [Operation.h:L185-L197](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/Operation.h#L185-L197) 与 [FDBRetryStrategy.h:L59-L83](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDBRetryStrategy.h#L59-L83)。
4. **幂等保护**：说明 `Remove`（递归删除）为何开启 `needIdempotent`（[Remove.cc:L52-L62](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/Remove.cc#L52-L62)），以及它如何让 `maybe_committed` 也能安全重试。

**交付物**：一张时序图（可用文字/伪图）+ 300 字说明。无需运行集群，但若你断言某运行期行为（如「第二次重试会得到 kNotFound」），无法本地验证处请标注「待本地验证」。

## 6. 本讲小结

- 3FS 用 `fdb::DB` / `fdb::Transaction` 把 FDB 的 C API 封装成 RAII 对象，并用 `Result::toTask` 把 `FDBFuture*` 注册回调 + `Baton` 变成可 `co_await` 的协程；FDB 网络必须由 `FDBContext` 先 `setupNetwork` + 起线程 `runNetwork`。
- `IKVEngine` → `IReadOnlyTransaction` / `IReadWriteTransaction` 这层抽象让 meta/mgmtd 不直接耦合 FDB（测试可换 `MemKV`）；关键语义是「快照读不进冲突范围、`get`/`addReadConflict` 进读冲突范围、`set`/`clear` 进写冲突范围」。
- `FDBTransaction` 用 `convertError` 把 `fdb_error_t` 映射成 `TransactionCode`（`1xxx` 段，`kConflict=1001` 等），`TransactionHelper::isRetryable` 决定哪些可重试；`kMaybeCommitted` 受 `retryMaybeCommitted` 开关控制，只有幂等操作才开。
- `FDBRetryStrategy::onError` 优先把退避交给 FDB 的 `fdb_transaction_on_error`（它最懂自己的退避策略），否则用带抖动的指数退避兜底；`OperationDriver::run` 是 meta 侧的重试主循环，叠加 `maxRetryCount` 与 `deadline_` 双重保护。

## 7. 下一步学习建议

- **进入 meta 服务**：本讲的 `ITransaction` 与重试循环是 u4-l1（meta 服务总览）与 u4-l3（用 FDB 事务实现元数据操作）的直接前置。建议接着读 [src/meta/store/Operation.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/Operation.h) 全文，把 `OperationDriver` 与各 `ops/*.cc` 串起来。
- **理解 key 编码**：冲突范围以 key range 表达，下一步看 u4-l2（Inode 与 DirEntry 的 KV 编码），理解 `packKey()` 怎么把 inode id / parent id 编成字节，以及为何用 little-endian。
- **对比其它后端**：浏览 [src/common/kv/mem/MemKV.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/kv/mem/MemKV.h) 与 `MemTransaction`，看同一套 `IReadWriteTransaction` 接口在内存实现里如何手工做 OCC 冲突检测（`checkConflict`），加深对 FDB 行为的理解。
- **深入 FDB 本身**：若有兴趣，对照 [FDBTransaction.cc:L162-L204](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDBTransaction.cc#L162-L204) 注释里指向的 FDB 源码 `NativeAPI.actor.cpp:Transaction::onError`，看 FDB 客户端如何为不同错误选择退避时长。
