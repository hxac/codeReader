# KVCache 子系统

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `src/kv` 下的 `KVStore` 抽象是什么、它在 3FS 里被谁消费（提示：它是**本地嵌入式 KV 引擎**，不是某种分布式服务）。
- 看懂 `get / put / remove / iterateKeysWithPrefix`、`BatchOperations`、`Iterator` 这组统一接口，以及 `KVStore::create` 工厂如何按 `type` 选择三种后端（LevelDB / RocksDB / MemDB）。
- 对比三种后端的实现差异、配置项差异与适用场景。
- 理解 README 里「KVCache for Inference」这个**工作负载**的读写与 GC（remove ops）特征，并能解释为什么推理 KVCache 可以用 3FS 替代 DRAM 缓存。

> ⚠️ 一个必须先澄清的认知：`src/kv` 的 `KVStore` 是 **storage 节点上嵌在进程里的本地 KV 引擎**（主要被 C++ 路径的 `ChunkMetaStore` 用来存 chunk 元数据）。而 README 宣传的「推理 KVCache」是一个**面向用户的分布式工作负载**——它通过 3FS 的文件/存储接口（client → storage）读写，**并没有**一个叫 `kvcache` 的独立服务模块。本讲 4.1、4.2 讲前者（引擎抽象与多后端），4.3 讲后者（工作负载），并把二者诚实地分清。

## 2. 前置知识

- **KV（key-value）存储**：一种最简单的数据模型，把数据看成「键 → 值」的映射，支持按 key 精确查找、按 key 前缀范围扫描。LevelDB / RocksDB 都是业界成熟的「LSM-Tree」嵌入式 KV 引擎。
- **LSM-Tree 与写放大**：写入先落到内存的 memtable，攒够后顺序刷盘成 SST 文件，后台再 compaction 合并。优点是写吞吐高，代价是读放大与空间放大。理解这一点就能理解后面 `KVStore::Config` 里大量调优参数的用意。
- **嵌入式 vs 服务型**：LevelDB/RocksDB 是**库**，被链接进你的进程、读写本地磁盘上的一个目录；不像 Redis/MySQL 是独立服务。`KVStore` 抽象的就是这种「库形态」。
- **CRAQ 与 storage 数据面**：本讲依赖 [u5-l1](u5-l1-storage-overview.md)。3FS 的 chunk 元数据存在 `ChunkMetaStore`，而 `ChunkMetaStore` 内部就持有一个 `KVStore` 实例。
- **`Result<T>` 错误模型**：3FS 全项目用返回值而非异常传递错误（见 [u2-l3](u2-l3-coroutine-and-pools.md)）。本讲里你会反复看到 `Result<std::string>`、`Result<Void>`、`RETURN_ON_ERROR`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/kv/KVStore.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/KVStore.h) | 抽象基类：定义统一接口、`Config`、`Options`、`BatchOperations`、`Iterator` 与静态工厂 `create`。 |
| [src/kv/KVStore.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/KVStore.cc) | 工厂实现：按 `Options::type` 分派到三种后端。 |
| [src/kv/RocksDBStore.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/RocksDBStore.h) / [.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/RocksDBStore.cc) | RocksDB 后端：高性能、可调优最丰富。 |
| [src/kv/LevelDBStore.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/LevelDBStore.h) / [.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/LevelDBStore.cc) | LevelDB 后端：轻量、配置项少，3FS 的默认元数据后端。 |
| [src/kv/MemDBStore.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/MemDBStore.h) | 内存后端：`std::map` + 互斥锁，仅用于单测，无持久化。 |
| [src/storage/store/PhysicalConfig.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/store/PhysicalConfig.h) | 演示 `KVStore::Type` 在真实配置里的默认值。 |
| [tests/kv/TestKVStore.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/tests/kv/TestKVStore.cc) | 唯一一份「如何用 `KVStore`」的端到端示例，也是本讲实践的基础。 |
| [README.md](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/README.md) | 「KVCache for Inference」工作负载的官方说明与性能数字。 |

## 4. 核心概念与源码讲解

### 4.1 KVStore 接口：一份统一契约

#### 4.1.1 概念说明

3FS 在 storage 节点上需要持久化**本地、嵌入式**的 key-value 数据（最典型的是 chunk 元数据）。为了不被某一种引擎绑死，它在 LevelDB、RocksDB、内存实现之上抽象出一层 `KVStore`。

设计要点是「**一份接口，三种实现，工厂选择**」：

- 上层（如 `ChunkMetaStore`）只依赖抽象基类指针 `std::unique_ptr<kv::KVStore>`，对底层是 LevelDB 还是 RocksDB 无感。
- 后端选择由配置项 `type` 在创建时决定，运行期不再切换。
- 接口刻意保持小而通用：点查 `get`、前缀扫描 `iterateKeysWithPrefix`、写 `put`、删 `remove`，加上批处理 `BatchOperations` 与游标 `Iterator`。

这套抽象与 [u2-l6](u2-l6-fdb-and-transactions.md) 讲的 `IKVEngine`（面向 FoundationDB 的分布式事务 KV）**不是一回事**：`IKVEngine` 是跨节点、有事务语义的全局元数据存储；`KVStore` 是单机、无事务、嵌入式引擎。两者名字都带 KV，但层级完全不同，不要混淆。

#### 4.1.2 核心流程

一次「写并读回」的逻辑流程：

```
调用方拿到 KVStore* ──put(key,val)──► 引擎内部 memtable（可能 sync 落 WAL）
                                          │
调用方 ──get(key)──► 引擎查 memtable/SST ──► Result<std::string>
                                          │
需要原子多键写入 ──createBatchOps()──► BatchOperations
                       put(..)/remove(..) 多次
                       commit()  ◄── 一次原子写批
```

错误处理统一走 `Result<T>`：`get` 找不到返回 `kKVStoreNotFound`，真正的 I/O 错误返回 `kKVStoreGetError` / `kKVStoreSetError`，打开失败返回 `kKVStoreOpenFailed`。调用方用 `RETURN_ON_ERROR` 链式传播。

#### 4.1.3 源码精读

`KVStore` 是纯抽象基类，三类后端 `Type` 用一个枚举列出：

[文件路径:KVStore.h:17-19](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/KVStore.h#L17-L19) —— 定义 `KVStore` 与 `enum class Type { LevelDB, RocksDB, MemDB }`。

四条核心读写接口都是纯虚函数，签名清晰：

[文件路径:KVStore.h:71](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/KVStore.h#L71) —— `get(key)` 返回 `Result<std::string>`，找不到时是 `kKVStoreNotFound` 而非异常。

[文件路径:KVStore.h:75-78](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/KVStore.h#L75-L78) —— `iterateKeysWithPrefix(prefix, limit, func, nextValidKey)`：前缀范围扫描，对每个命中键值对回调 `func`，并用 `nextValidKey` 支持「分页」。这条接口是后续讲到的 12 字节前缀优化、布隆过滤器发挥作用的入口。

[文件路径:KVStore.h:81](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/KVStore.h#L81) / [KVStore.h:84](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/KVStore.h#L84) —— `put(key, value, sync=false)` 与 `remove(key)`。注意 `put` 带一个 `sync` 形参，可单次强制刷盘。

批处理 `BatchOperations` 把若干 put/remove 攒成一个原子写批：

[文件路径:KVStore.h:87-106](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/KVStore.h#L87-L106) —— 内嵌类 `BatchOperations`（`put/remove/clear/commit/destroy`），用 `unique_ptr` + 自定义 `Deleter` 管理生命周期（析构走 `destroy()`）。写批的原子性是上层（如 chunk 元数据的「新元数据 + 释放旧块」一次性落盘）所依赖的关键性质。

游标 `Iterator` 提供顺序遍历：

[文件路径:KVStore.h:109-126](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/KVStore.h#L109-L126) —— `seek/seekToFirst/seekToLast/next/valid/key/value/status`，同样以 `unique_ptr` + `Deleter` 包装。

最后是工厂入口：

[文件路径:KVStore.h:129](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/KVStore.h#L129) —— `static std::unique_ptr<KVStore> create(const Config&, const Options&)`，唯一对外构造方式。

#### 4.1.4 代码实践

**实践目标**：用现成单测快速跑通三种后端，亲眼看到「同一份接口、三种实现」。

**操作步骤**（源码阅读型 + 本地可选运行）：

1. 打开 [tests/kv/TestKVStore.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/tests/kv/TestKVStore.cc)，它是 `TestWithParam<KVStore::Type>`，参数化测试。
2. 阅读 [TestKVStore.cc:10-50](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/tests/kv/TestKVStore.cc#L10-L50)：
   - 用一个临时目录 + `createIfMissing=true` 创建 store；
   - `put("hello","world")` 后 `get` 能读回；
   - 用 `createBatchOps()` 在一个写批里 `put("hello2",...)` + `remove("hello")`，`commit()` 后 `hello` 消失、`hello2` 存在——演示批处理原子性；
   - `createIterator()->seekToFirst()` 遍历，只剩 `hello2`。
3. 看 [TestKVStore.cc:51-53](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/tests/kv/TestKVStore.cc#L51-L53) 的 `INSTANTIATE_TEST_SUITE_P`，三种 `Type` 各跑一遍同一份用例。
4. （可选）本地构建后运行：`build/bin/test_kv --gtest_filter='*Normal*'`（具体二进制名以 `tests/kv/CMakeLists.txt` 的注册为准，**待本地验证**）。

**需要观察的现象**：三种后端行为完全一致——这正是抽象层存在的意义。

**预期结果**：三条参数化用例全绿，证明接口契约被三种实现一致地满足。

#### 4.1.5 小练习与答案

**练习 1**：`get` 找不到 key 时，为什么不抛异常、而是返回 `Result<std::string>`？

> **参考答案**：3FS 用 `Result<T>` 把错误当返回值传（见 [u2-l3](u2-l3-coroutine-and-pools.md)），「key 不存在」是 KV 引擎的**正常业务结果**（用 `kKVStoreNotFound` 表示），而非异常控制流；这样调用方可以用 `if (!result)` 直接分支，无需 try/catch。

**练习 2**：`BatchOperations` 的析构为什么用自定义 `Deleter` 调 `destroy()`，而不是直接 `delete`？

> **参考答案**：因为 RocksDB/LevelDB 后端用 `ObjectPool` 复用 `BatchOperations` 对象（见 [RocksDBStore.cc:20](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/RocksDBStore.cc#L20)），`destroy()` 是「归还对象池」而非「删除」，自定义 `Deleter` 让 `unique_ptr` 既能 RAII 管理生命周期、又走池化路径，避免频繁分配。

### 4.2 多后端实现：LevelDB / RocksDB / MemDB

#### 4.2.1 概念说明

三种后端面向不同场景：

- **LevelDB**：Google 出品的轻量 LSM 引擎，配置项少、稳定。是 3FS 的**默认**本地元数据后端（见 4.2.3 的 `PhysicalConfig`）。
- **RocksDB**：Facebook 在 LevelDB 基础上fork 出的高性能引擎，调优项极其丰富（多线程 compaction、布隆过滤器、前缀索引、两级索引、block cache……）。3FS 的 Rust chunk engine 也另起一份独立的 RocksDB 实例存元数据（见 [u6-l3](u6-l3-chunk-meta-rocksdb.md)），但那不是本讲的 `KVStore`。
- **MemDB**：`std::map<std::string,std::string>` + 一把 `std::mutex`，纯内存、无持久化，**仅供单测**。让你在没有磁盘的环境里也能跑通基于 `KVStore` 的逻辑。

三者通过 `KVStore::create` 工厂按 `Options::type` 选用，上层无感切换。

#### 4.2.2 核心流程

工厂的分派逻辑极简：

```
KVStore::create(config, options):
   switch (options.type):
     LevelDB  -> LevelDBStore::create(config, options)   # 打开磁盘目录
     RocksDB  -> RocksDBStore::create(config, options)    # 打开磁盘目录
     MemDB    -> make_unique<MemDBStore>(config)          # 仅需 config，无路径
```

注意 `Options::type` 默认是 `RocksDB`（见 [KVStore.h:63](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/KVStore.h#L63)），但 `Config::type` 默认是 `LevelDB`（见 [KVStore.h:21](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/KVStore.h#L21)）。**真正决定用哪个的是 `Options::type`**，单测里显式把 `options.type = config.type()` 对齐（[TestKVStore.cc:16-17](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/tests/kv/TestKVStore.cc#L16-L17)）。这是一个容易踩的坑。

#### 4.2.3 源码精读

**工厂分派**：

[文件路径:KVStore.cc:10-25](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/KVStore.cc#L10-L25) —— `create()` 按 `options.type` 三分支；非法类型打 `ERR` 日志并返回 `nullptr`，MemDB 不需要路径。

**RocksDB 后端的读写**（最值得精读，调优最丰富）：

[文件路径:RocksDBStore.cc:33-46](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/RocksDBStore.cc#L33-L46) —— `get`：`NotFound` 映射成 `kKVStoreNotFound`，其他错误映射成 `kKVStoreGetError`。

[文件路径:RocksDBStore.cc:79-89](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/RocksDBStore.cc#L79-L89) —— `put`：`options.sync = config_.sync_when_write() || sync`，即「全局配置要 sync 或本次显式要 sync」才强制刷盘。`sync_when_write` 是 `CONFIG_HOT_UPDATED_ITEM`（[KVStore.h:23](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/KVStore.h#L23)），可热更新，方便在「持久化优先」与「吞吐优先」间切换。

[文件路径:RocksDBStore.cc:48-76](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/RocksDBStore.cc#L48-L76) —— `iterateKeysWithPrefix`：用 `readahead_size`（默认 2MB，可热更新）、`prefix_same_as_start`（仅当 `rocksdb_enable_prefix_transform` 且前缀 ≥12 字节才开）。配合 `init` 里的 12 字节定长前缀 + 布隆过滤器，让「按前缀扫一批 chunk 元数据」既快又省内存。

[文件路径:RocksDBStore.cc:111-121](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/RocksDBStore.cc#L111-L121) —— 写批 `commit()` 走 `db_->Write(options, &writeBatch_)`，一次原子提交。`writeBatch_` 由 [RocksDBStore.cc:105-109](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/RocksDBStore.cc#L105-L109) 的 `put/remove` 填充。

**RocksDB 的 `init`（调优项全景）**：

[文件路径:RocksDBStore.cc:156-207](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/RocksDBStore.cc#L156-L207) —— 这里把 `KVStore::Config` 里几十个 `rocksdb_*` 配置翻译成 RocksDB 的 `Options`，关键点：
- [L173-175](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/RocksDBStore.cc#L173-L175)：12 字节定长前缀 `NewFixedPrefixTransform(rocksdbPrefixLen)`（`rocksdbPrefixLen=12`，见 [RocksDBStore.cc:28](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/RocksDBStore.cc#L28)）；
- [L176-179](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/RocksDBStore.cc#L176-L179)：布隆过滤器（默认 10 bits/key）；
- [L183-187](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/RocksDBStore.cc#L183-L187)：可共享的 LRU block cache（默认 8GB）；
- [L189](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/RocksDBStore.cc#L189)：两级索引 `kTwoLevelIndexSearch`；
- [L171](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/RocksDBStore.cc#L171)：压缩默认**关闭**（`kNoCompression`）——对元数据这种小价值、又追求低延迟的场景，宁可省 CPU 也不压缩；
- [L195](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/RocksDBStore.cc#L195)：WAL 恢复模式 `kTolerateCorruptedTailRecords`，崩溃后容忍尾部损坏记录以尽快恢复。

**LevelDB 后端**：结构同构，但调参项少得多——只有 `sst_file_size`、`write_buffer_size`、`block_cache_size`、`shared_block_cache`、`iterator_fill_cache`（[LevelDBStore.cc:158-181](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/LevelDBStore.cc#L158-L181)）。写批 `commit` 还多一个小优化：`ApproximateSize()==0` 直接跳过（[LevelDBStore.cc:111-113](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/LevelDBStore.cc#L111-L113)）。

**MemDB 后端**：全部在头文件里，一个 `std::map` + 一个 `std::mutex` 串起所有接口。例如 `get`：

[文件路径:MemDBStore.h:16-23](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/MemDBStore.h#L16-L23) —— 加锁查 `std::map`，找不到返回 `kKVStoreNotFound`。`iterateKeysWithPrefix` 用 `lower_bound` 起步、按前缀续扫（[MemDBStore.h:26-43](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/MemDBStore.h#L26-L43)），完全模拟 LSM 的前缀语义，方便测试。

**真实消费方与默认后端**：`ChunkMetaStore` 持有 `std::unique_ptr<kv::KVStore> kv_`（[ChunkMetaStore.h:146](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/store/ChunkMetaStore.h#L146)），通过 `create/load` 按配置打开（[ChunkMetaStore.h:35-40](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/store/ChunkMetaStore.h#L35-L40)）。而 `PhysicalConfig.kv_store_type` 默认就是 `LevelDB`：

[文件路径:PhysicalConfig.h:23](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/store/PhysicalConfig.h#L23) —— `kv_store_type` 默认 `kv::KVStore::Type::LevelDB`，即 C++ chunk 元数据路径出厂用 LevelDB。

#### 4.2.4 代码实践

**实践目标**：对比三种后端的实现差异与适用场景，并理解「默认用 LevelDB」背后的取舍。

**操作步骤**（源码阅读型）：

1. 用一张表横向对比三种后端（自己填写「持久化/调优丰富度/线程模型/用途」四列）。
2. 在 [KVStore.h:20-60](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/KVStore.h#L20-L60) 里数一数：`leveldb_*` 配置项有几个、`rocksdb_*` 有几个，体会「RocksDB 后端能调的远比 LevelDB 多」。
3. 找到默认后端：从 [PhysicalConfig.h:23](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/store/PhysicalConfig.h#L23) 看，C++ 元数据路径默认 LevelDB；想换 RocksDB 需在 `target.toml` 的 `PhysicalConfig` 里改 `kv_store_type`。

**预期结果（参考对比表）**：

| 后端 | 持久化 | 调优丰富度 | 线程/并发 | 典型用途 |
| --- | --- | --- | --- | --- |
| LevelDB | 是（磁盘） | 少（~6 项） | 单线程 compaction | 3FS 默认本地元数据后端，够用、稳 |
| RocksDB | 是（磁盘） | 多（~25 项） | 多线程 compaction | 高负载/需精细调优；Rust chunk engine 也用 RocksDB（独立实例） |
| MemDB | 否（内存） | 无 | 一把互斥锁 | 单测、无磁盘环境 |

#### 4.2.5 小练习与答案

**练习 1**：为什么 3FS 默认元数据后端选 LevelDB 而非 RocksDB？

> **参考答案**：chunk 元数据是小而频繁的访问，LevelDB 单线程 compaction、配置简单、足够稳定，能满足元数据路径的需求且运维心智负担低；当负载需要更强吞吐与精细调优时，再切到 RocksDB。这是一种「够用即可、按需升级」的工程取舍。

**练习 2**：`MemDB` 既然不持久化、还要不要支持 `BatchOperations`？为什么？

> **参考答案**：要。`MemDB` 的存在是为了让上层逻辑（如 `ChunkMetaStore` 的批处理）能在无磁盘环境单测，必须完整实现 `BatchOperations` 才能保证「测试覆盖到的代码路径与生产一致」。其 `commit()` 在锁内顺序应用 `writeBatch_`（[MemDBStore.h:73-83](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/MemDBStore.h#L73-L83)）。

### 4.3 KVCache 读写：推理工作负载与 GC 的 remove-ops

#### 4.3.1 概念说明

现在把视角从「单机引擎」抬到「分布式工作负载」。README 描述的 **KVCache for Inference** 是这样一件事：

> 在大模型（LLM）推理中，decoder 每一层都会算出前面所有 token 的 key/value 向量。重复计算这些向量很浪费，于是把它们**缓存**起来复用，称为 KVCache。

传统做法是把 KVCache 放在 GPU/DRAM 里。但模型变大、上下文变长后，单机 DRAM 装不下。3FS 提供的方案是：**把 KVCache 当作 3FS 上的数据来读写**——用一份共享的、容量远超 DRAM 的 SSD + RDMA 存储层，替代每机本地 DRAM 缓存。

再次强调：这里**没有一个叫 `kvcache` 的服务模块**。推理进程（KVCache client）通过 3FS 的客户端（FUSE 或原生 USRBIO，见 [u7-l2](u7-l2-fuse-daemon.md)/[u7-l3](u7-l3-usrbio-zero-copy.md)）读写文件，请求最终落到 storage 节点的 SSD（数据面 `StorageOperator`，见 [u5-l1](u5-l1-storage-overview.md)）。本讲的 `src/kv` `KVStore` 只在 storage 单机内部服务 chunk 元数据，并不直接承载 KVCache 的业务数据。

#### 4.3.2 核心流程

KVCache 工作负载的读写特征，决定了 3FS 哪些设计被「吃满」：

```
推理进程（KVCache client）
   │  写：把新算出的 K/V 向量存起来       ──► 高吞吐顺序/大块写
   │  读：复用历史 K/V 向量                ──► 高吞吐读（峰值 ~40 GiB/s/节点）
   │  GC：上下文窗口滑动/会话结束          ──► 大量「过期条目删除」= remove ops
   ▼
3FS client（USRBIO 零拷贝，RDMA 直填用户内存）
   ▼
storage（CRAQ 链式复制，SSD 落盘；chunk 元数据经 KVStore）
```

两个关键特征对应 README 的两张图：

1. **读吞吐**：单节点 1×400Gbps 网卡下峰值可达 **40 GiB/s**（[README.md:48](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/README.md#L48)）。这正是 USRBIO 零拷贝 + RDMA Write 单边传输 + CRAQ「写全读任何」共同释放的能力（见 [u7-l3](u7-l3-usrbio-zero-copy.md)、[u5-l3](u5-l3-write-path-craq.md)）。
2. **GC 的 remove-ops IOPS**：KVCache 条目会过期，必须高速删除。README 专门给了一张「removing ops from GC」的 IOPS 图（[README.md:48](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/README.md#L48)、[README.md:51](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/README.md#L51)）。在 3FS 里，「删 KVCache 条目」最终落到「删 chunk」，而删除在元数据层就是 `KVStore::remove` 或写批里的 `BatchOperations::remove`——所以本讲 4.1 讲的 `remove` 接口、4.2 讲的「RocksDB/LevelDB 删除走 tombstone + compaction」，正是 GC 高 IOPS 得以成立的底层机制之一。

#### 4.3.3 源码精读

README 对 KVCache 的定位与性能描述：

[文件路径:README.md:17](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/README.md#L17) —— 「KVCache for Inference：提供比 DRAM 缓存更具成本效益的替代方案，吞吐高、容量大得多」。

[文件路径:README.md:45-48](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/README.md#L45-L48) —— KVCache 是优化 LLM 推理的技术；峰值读吞吐 40 GiB/s；并给出同期的 GC remove-ops IOPS 图。注意两图描述的是**同一套 KVCache 客户端**的读与删除。

GC remove-ops 与本讲引擎的呼应：删除最终要落到 `KVStore::remove` 这类原语。以 RocksDB 后端为例：

[文件路径:RocksDBStore.cc:92-102](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/RocksDBStore.cc#L92-L102) —— `remove` 即 `db_->Delete`，受 `sync_when_write` 控制。LSM 引擎的删除是「写一条 tombstone」，真正回收空间靠后台 compaction——这正是「高 IOPS 删除」能在引擎层成立的机理（批量 tombstone + 异步 compaction）。批处理删除则更高效：

[文件路径:RocksDBStore.cc:109](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/RocksDBStore.cc#L109) —— `BatchOperations::remove` 把多条删除攒进一个 `WriteBatch` 一次提交，是高 IOPS 删除的关键路径。

> 说明：这里展示的是「引擎层如何支持高效删除」这一通用机制，用以解释 KVCache GC 为何能跑高 remove-IOPS；3FS 端到端的 KVCache 删除链路（client → meta unlink → GC → storage chunk 回收）涉及 [u4-l5](u4-l5-length-session-gc.md) 的延迟删除与 [u6-l3](u6-l3-chunk-meta-rocksdb.md) 的物理块回收，超出本讲 `src/kv` 范围，此处点到为止。

#### 4.3.4 代码实践

**实践目标**：解释「推理 KVCache 为何能用 3FS 替代 DRAM 缓存」，并把理由锚定到本讲讲过的接口与后端特性。

**操作步骤**（分析型实践）：

1. 读 [README.md:45-48](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/README.md#L45-L48)，记下峰值读吞吐（40 GiB/s）与「GC remove-ops IOPS」两个指标。
2. 结合本讲，写出「3FS 替代 DRAM」成立的三个技术支点（见预期结果）。
3. （延伸）思考：如果 KVCache 的 GC 删除达不到所需 IOPS，本讲哪个后端、哪个接口最值得调？提示：RocksDB 后端 + `BatchOperations::remove` 批量删除，必要时调 `sync_when_write=false` 换吞吐。

**预期结果（替代 DRAM 的三条理由）**：

- **容量**：DRAM 单机 TB 级、昂贵；3FS 聚合海量 SSD，容量随节点线性扩展，单价远低于 DRAM。
- **吞吐**：USRBIO 零拷贝 + RDMA，单节点读峰值 40 GiB/s，逼近 DRAM 的可用带宽量级，满足推理复用 KV 的时延需求。
- **可承受的删除开销**：KVCache 条目高频过期，3FS 的删除最终落到 LSM 引擎的 tombstone + compaction（`KVStore::remove` / `BatchOperations::remove`），以高 remove-IOPS 支撑 GC，不会像 DRAM 那样「容量满了就 OOM」，而是稳定地边删边写。

一条诚实的边界：3FS 延迟高于本地 DRAM（数据要走网络 + SSD），所以 KVCache 替代 DRAM 是「**用更高延迟换大得多的容量与更低的单位成本**」的权衡，适合「装得下比 DRAM 多得多的历史上下文」这类场景，而非对延迟极致敏感的 hottest 层。

#### 4.3.5 小练习与答案

**练习 1**：README 给的 KVCache 两张图（读吞吐、GC remove-IOPS）为什么**必须**同时看，而不能只看读吞吐？

> **参考答案**：KVCache 是「边写边删」的负载——上下文窗口滑动、会话结束都会产生大量过期条目。只看读吞吐会忽略删除压力：如果引擎/系统撑不住 GC 的 remove-IOPS，缓存会被垃圾填满、读吞吐也随之崩塌。两图同框正是强调「读得快」与「删得动」必须同时成立。

**练习 2**：本讲的 `src/kv` `KVStore` 与推理 KVCache 的「业务数据」是同一份东西吗？

> **参考答案**：不是。`KVStore` 是 storage 节点上**本地嵌入式**引擎，主要存 chunk **元数据**（`ChunkMetaStore` 持有它）；推理 KVCache 的**业务数据**（K/V 向量）是 3FS 文件里的大块数据，落在 SSD 的 chunk 中，经 CRAQ 复制。两者是「元数据引擎」与「业务数据」的关系，分属不同层级，不要因都叫「KV」而混淆。

## 5. 综合实践

把三个最小模块串起来：**为一个假设的「本地元数据存储」选型并验证**。

1. **选型**：假设你要给一个单机服务加一个本地持久化 KV，写多读少、偶尔需要原子多键更新。结合 4.2 的对比表，你会选 LevelDB 还是 RocksDB？写出理由（提示：默认 LevelDB 够用；若写压力大、需多线程 compaction 与精细调优则 RocksDB）。
2. **接口验证**：参照 [TestKVStore.cc:10-50](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/tests/kv/TestKVStore.cc#L10-L50)，写出一段调用序列：`create` → `put` → `get` → `createBatchOps`（含一次 `put` 和一次 `remove`）→ `commit` → `createIterator()->seekToFirst()` 遍历。说明它对三种 `Type` 都成立。
3. **接到工作负载**：用 4.3 的三支点（容量/吞吐/可承受删除），写 200 字解释「为什么把 KVCache 从 DRAM 搬到 3FS 后，GC 的高 remove-IOPS 不会成为瓶颈」，并指出该 remove 最终落到本讲的哪个接口（`KVStore::remove` / `BatchOperations::remove`）。

> 若本地已构建 3FS，可把第 2 步写成基于 `KVStore::create` 的最小 C++ 片段（标注「示例代码」）跑在 `MemDB` 上验证逻辑，再切到 `LevelDB`/`RocksDB` 比较落地文件结构（`待本地验证`）。

## 6. 本讲小结

- `src/kv` 的 `KVStore` 是 **storage 节点上的本地嵌入式 KV 引擎抽象**，不是分布式服务；提供 `get/put/remove/iterateKeysWithPrefix` + `BatchOperations` + `Iterator` 一套统一契约。
- 三种后端 `LevelDB / RocksDB / MemDB` 由工厂 `KVStore::create` 按 `Options::type` 选择；`MemDB` 仅用于单测，`LevelDB` 是 C++ chunk 元数据路径的默认后端（`PhysicalConfig.kv_store_type`）。
- RocksDB 后端调优最丰富：12 字节定长前缀、布隆过滤器、两级索引、共享 LRU block cache、默认不压缩、WAL 容忍尾部损坏；LevelDB 后端结构同构但参数精简。
- 删除走 LSM 的 tombstone + compaction，`BatchOperations::remove` 可批量提交——这是高 IOPS 删除的引擎层机理。
- README 的「KVCache for Inference」是一个**分布式工作负载**（峰值读 40 GiB/s/节点 + GC remove-IOPS），通过 3FS 客户端/存储接口承载，**没有**独立的 `kvcache` 服务模块。
- 推理 KVCache 能用 3FS 替代 DRAM，靠的是「容量（聚合 SSD）、吞吐（USRBIO+RDMA）、可承受的高速删除」三支点，本质是用更高延迟换大容量与低成本。

## 7. 下一步学习建议

- 想看 `KVStore` 在生产里如何被消费：读 [u6-l3](u6-l3-chunk-meta-rocksdb.md)（Rust chunk engine 用独立 RocksDB 存 chunk 元数据），以及 `src/storage/store/ChunkMetaStore.cc`（C++ 路径如何用 `KVStore` 存 chunk 元数据并做空间回收）。
- 想理解 KVCache 业务数据的端到端删除链路：接 [u4-l5](u4-l5-length-session-gc.md)（meta 的延迟删除与 GC）与 [u5-l1](u5-l1-storage-overview.md)（storage 的空间回收 worker）。
- 想理解 KVCache 的高吞吐读怎么实现：接 [u7-l3](u7-l3-usrbio-zero-copy.md)（USRBIO 零拷贝）与 [u5-l2](u5-l2-read-path-aio.md)（读路径与 AIO）。
- 想动手调后端：试着在一份 `target.toml` 里把 `kv_store_type` 从 `LevelDB` 改成 `RocksDB`，对照 [KVStore.h:33-59](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/kv/KVStore.h#L33-L59) 的 `rocksdb_*` 配置项调整 block cache / 前缀长度，观察 `iterateKeysWithPrefix` 的行为差异（`待本地验证`）。
