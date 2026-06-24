# Chunk Engine 总览与 C++/Rust FFI

## 1. 本讲目标

3FS 的 storage 服务要把数据真正写到 SSD 上。落盘执行者有两个：旧的纯 C++ `ChunkStore`，以及新一代用 Rust 编写的 **chunk engine**。本讲只聚焦后者，目标是让读者学完后能够：

- 说清 chunk engine 在 storage 服务里的位置，以及它 `meta / alloc / file` 三层各自管什么。
- 看懂 `cxx.rs` 这一层 FFI（Foreign Function Interface，跨语言调用接口）桥接：C++ 怎么拿到 Rust 对象、Rust 怎么把结果与错误回传。
- 理解 `get / update / commit` 这一组接口在多线程下的安全访问模型——为什么 C++ 侧多个写线程可以安全地并发调用同一个 Rust 引擎。

本讲是 u6（Chunk Engine）单元的开篇，承接 u5-l1（storage 服务总览与启动）。它只讲「引擎的骨架与跨语言边界」；具体怎么分配物理块（u6-l2）、怎么用 RocksDB 存元数据（u6-l3）留给后续两讲。

## 2. 前置知识

在进入源码前，先用通俗语言解释几个本讲会用到的概念。

- **FFI（Foreign Function Interface）**：不同编程语言互相调用对方函数的机制。3FS 用 C++ 写主体，但 storage 的落盘核心用 Rust 写，二者通过 FFI 衔接。
- **cxx**：一个专门用于「安全地做 C++/Rust FFI」的库。你在一个 `#[cxx::bridge]` 块里声明两边共享的类型和函数签名，cxx 会自动生成 C++ 头文件（`cxx.rs.h`）和 Rust 绑定，保证跨语言调用时内存布局一致、不踩未定义行为。
- **Rust 的 `Box` 与 `Arc`**：`Box<T>` 是独占的堆对象（类似 `std::unique_ptr`），`Arc<T>` 是引用计数的共享对象（类似 `std::shared_ptr`）。本讲会反复看到「`Arc::into_raw` 把指针交给 C++、`Arc::from_raw` 收回」这种所有权交接手法。
- **chunk 与 target**：回顾 u5-l1，一个 storage 进程管理多个 **target**（存储目标），每个 target 对应一条复制链上的一个副本；文件数据被切成固定大小的 **chunk** 落到 target 上。chunk engine 就是「一个 target 在单机上的物理化身」。
- **CRAQ 双版本**：回顾 u5-l3，每个 chunk 有 `updateVer`（待确认）和 `commitVer`（已提交）。chunk engine 在物理层维护这套版本号语义。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 语言 | 作用 |
|------|------|------|
| [src/storage/chunk_engine/src/lib.rs](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/lib.rs) | Rust | crate 根，声明 `alloc / core / cxx / file / meta / types / utils` 七个子模块并统一导出。 |
| [src/storage/chunk_engine/src/core/engine.rs](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/core/engine.rs) | Rust | `Engine` 结构体与 `open / get / update_chunk / commit_chunk` 等核心编排逻辑。 |
| [src/storage/chunk_engine/src/cxx.rs](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/cxx.rs) | Rust | FFI 桥接层：`#[cxx::bridge]` 声明 + `update_raw_chunk / commit_raw_chunk` 等给 C++ 调用的适配函数。 |
| [src/storage/chunk_engine/Cargo.toml](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/Cargo.toml) | TOML | 声明 `crate-type = ["lib", "staticlib"]`，把 Rust 编成静态库供 C++ 链接。 |
| [src/storage/chunk_engine/build.rs](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/build.rs) | Rust | 构建脚本，调用 `cxx_build::bridge` 生成 C++ 头文件。 |
| [src/storage/store/ChunkEngine.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/store/ChunkEngine.h) | C++ | C++ 侧的薄封装：把 storage 的 `UpdateJob / AioReadJob` 翻译成 Rust 的 `UpdateReq`，并解析返回。 |
| [src/storage/store/ChunkEngine.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/store/ChunkEngine.cc) | C++ | `ChunkEngine::update / commit` 两个静态方法的实现。 |
| [src/storage/store/StorageTargets.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/store/StorageTargets.cc) | C++ | 启动期创建 `chunk_engine::Engine` 并以 `rust::Box` 持有。 |
| [src/storage/store/StorageTarget.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/store/StorageTarget.h) / [StorageTarget.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/store/StorageTarget.cc) | C++ | 持有 `engine_` 裸指针，并在读/写路径上用 `useChunkEngine()` 在「Rust 引擎」与「旧 C++ ChunkStore」之间二选一。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**引擎结构**（4.1）、**FFI 桥接**（4.2）、**线程安全接口**（4.3）。

### 4.1 引擎结构：meta / alloc / file 三层

#### 4.1.1 概念说明

chunk engine 的 Rust crate 用 `lib.rs` 顶层声明了七个子模块。其中真正构成「引擎」的是三层：

- **`meta` 层**：负责持久化。chunk 的元数据（位置、长度、校验和、版本号）存在嵌入式 KV 数据库 RocksDB 里。这一层回答「某个 chunk_id 的元数据是什么、落在磁盘哪里」。
- **`alloc` 层**：负责物理块分配。SSD 空间被预先切成若干固定大小的「块（chunk position）」，分配器在内存里用位图管理哪些块已用、哪些空闲。这一层回答「新数据写到磁盘的哪一块」。
- **`file` 层**：负责把块组织成文件。多个块聚成一个 `cluster`，多个 `cluster` 聚成 `clusters`，最终落到磁盘上的数据文件。

把持久化（meta）、空间管理（alloc）、物理文件（file）拆成三层，是为了让「改内存状态」和「持久化事件」解耦：分配先在内存完成、再异步落盘，读操作只拿内存引用就能安全访问数据。

#### 4.1.2 核心流程

`Engine::open` 是引擎的入口，它把三层装配起来。简化后的流程：

```text
EngineConfig{ path, create, prefix_len }
        │
        ▼
1. MetaStore::open(path/meta)          ── 打开 RocksDB，加载已有元数据
2. occupy_uncommitted_positions()      ── 恢复上次崩溃前「写了但没提交」的块
3. Allocators::new(path, meta_store)   ── 从 meta 重建内存分配位图
4. LockMap<chunk_id, ChunkArc>(1<<20, 256 分片)  ── 建 meta_cache
5. 恢复未提交的 writing_list
6. upgrade_version()                   ── 版本兼容升级
        │
        ▼
   返回 Engine（所有字段都是 Arc，可被多线程共享）
```

注意第 4 步：`meta_cache` 是一个容量 `1<<20`（约 100 万）、分 256 片的锁映射表。它就是 README 里说的「MetaCache：内存里的 `chunk_id -> chunk_info` 映射」。256 分片意味着最多 256 把细粒度锁，不同 chunk 的读写互不阻塞。

#### 4.1.3 源码精读

先看 `Engine` 结构体——它所有字段都是 `Arc`，因此可以廉价克隆、被多个线程共享（这也正是 `#[derive(Clone)]` 的含义）：

`Engine` 把三层组件作为字段聚合在一起，每层都用 `Arc` 共享：[src/storage/chunk_engine/src/core/engine.rs:18-28](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/core/engine.rs#L18-L28)

```rust
#[derive(Clone)]
pub struct Engine {
    pub meta_store: Arc<MetaStore>,                      // meta 层：RocksDB 持久化
    pub allocators: Allocators,                          // alloc 层：物理块分配器
    pub meta_cache: Arc<LockMap<Bytes, ChunkArc>>,       // 内存缓存：chunk_id -> chunk
    pub workers: Arc<Mutex<Vec<Worker>>>,                // 后台分配线程
    pub allow_to_allocate: Arc<AtomicBool>,              // 是否允许分配新空间
    pub metrics: Arc<Metrics>,
    pub prefix_len: usize,                               // key 前缀长度（= sizeof(ChainId)）
    pub writing_list: Arc<WritingList>,                  // 正在写但未提交的 chunk
}
```

`open` 方法按流程装配这三层：[src/storage/chunk_engine/src/core/engine.rs:31-56](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/core/engine.rs#L31-L56)。其中 `MetaStore::open` 打开 `path/meta` 下的 RocksDB，`Allocators::new` 接收 `meta_store` 以便分配事件能落盘，`meta_cache` 用 `1 << 20` 容量、256 分片创建。

模块声明在 crate 根，三层一目了然：[src/storage/chunk_engine/src/lib.rs:1-7](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/lib.rs#L1-L7)

```rust
mod alloc;
mod core;
mod cxx;
mod file;
mod meta;
mod types;
mod utils;
```

> 说明：`cxx` 模块不是业务层，而是 FFI 桥接（4.2 节）；`types` 与 `utils` 是公共数据结构与工具。真正的三层是 `meta / alloc / file`，外加 `core`（编排）。

#### 4.1.4 代码实践

**实践目标**：建立对 chunk engine 三层目录结构的直觉。

**操作步骤**：

1. 在仓库根目录执行 `ls src/storage/chunk_engine/src/`，对照本节看到 `alloc core cxx file meta types utils` 七个目录。
2. 进入 `meta/`、`alloc/`、`file/` 三个目录，分别看一眼其中的文件名（不需要读懂实现）。
3. 打开 [src/storage/chunk_engine/README.md](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/README.md) 的 Design 一节。

**需要观察的现象**：`meta/` 下有 `meta_store.rs / meta_key.rs / rocksdb.rs`（对应持久化）；`alloc/` 下有 `allocators.rs / chunk_allocator.rs / group_allocator.rs`（对应空间分配）；`file/` 下有 `cluster.rs / clusters.rs`（对应物理文件组织）。

**预期结果**：你能用一句话说出每个目录的职责，并与本节「meta / alloc / file 三层」一一对应。

#### 4.1.5 小练习与答案

**练习 1**：`Engine` 为什么用 `#[derive(Clone)]`？克隆一个 Engine 代价大吗？

> **参考答案**：因为 `Engine` 的所有字段都是 `Arc`（或内含 `Arc` 的类型）。克隆 `Arc` 只是原子地给引用计数 +1，不复制底层数据，代价极小。这让 storage 服务的多个写线程可以各持有一份 `Engine` 副本、共享同一份底层状态。

**练习 2**：`meta_cache` 为什么要分 256 片（shard）？

> **参考答案**：分片把一把全局大锁拆成 256 把细粒度小锁。不同 `chunk_id` 经过哈希落到不同分片，读写不同 chunk 时互不阻塞，从而支撑高并发。

---

### 4.2 FFI 桥接：cxx.rs

#### 4.2.1 概念说明

Rust 写的引擎不能直接被 C++ 调用，需要一座「桥」。3FS 用的是 [cxx](https://cxx.rs/) 库，桥的两端约定好：

- **共享的类型**：在 `#[cxx::bridge]` 块里声明，cxx 保证两边内存布局一致。比如 `UpdateReq`、`RawMeta` 这些结构体，Rust 和 C++ 看到的是「同一段内存的两种视角」。
- **`extern "Rust"`**：标记「这是 Rust 实现、给 C++ 调用」的函数与类型。cxx 会据此生成 C++ 头文件 `cxx.rs.h`，C++ `#include` 它就能调用。
- **适配函数（adapter）**：`update_raw_chunk` 这类带 `_raw` 后缀的函数不是业务逻辑，而是把 Rust 的「优雅返回 `Result<T>`」翻译成 C++ 友好的「输出参数 + 原始指针」。

为什么需要适配层？因为 Rust 习惯用 `Result<T, E>` 表达成败，而 C++ 跨 FFI 直接传 `Result` 很麻烦。3FS 的做法是：FFI 函数返回一个裸指针（成功）或空指针（失败），并用一个 `Pin<&mut CxxString> error` 输出参数带回错误字符串，再用结构体里的 `out_*` 字段回传业务结果。

#### 4.2.2 核心流程

以一次「写」为例，从 C++ 到 Rust 再回来的完整数据流：

```text
C++ ChunkEngine::update(job)
   │  把 UpdateJob 字段填进 chunk_engine::UpdateReq{}
   │  key = chainId(8B) + chunkId
   ▼
engine.update_raw_chunk(key_slice, &mut req, &mut error)      ← cxx 生成的不安全签名
   │  (Rust 侧适配函数)
   ▼
Engine::update_chunk(chunk_id, &mut req)                       ← 真正的业务逻辑
   │  成功 → Box::into_raw(WritingChunk) 返回 *mut
   │  失败 → req.out_error_code = 数字码; 返回 null
   ▼
C++ 拿到 *mut WritingChunk（或检测 error 非空）
   │  从 req.out_commit_ver / out_chain_ver / out_checksum 读结果
   ▼
稍后 C++ ChunkEngine::commit → engine.commit_raw_chunk(ptr, ...)
   │  Rust 侧 Box::from_raw 收回所有权，落盘
```

关键的跨语言数据结构有三个：

1. **`UpdateReq`**：请求 + 输出合一的结构体。输入字段（`is_truncate / update_ver / chain_ver / data...`）由 C++ 填，输出字段（`out_commit_ver / out_chain_ver / out_checksum / out_error_code / out_non_existent`）由 Rust 回填。
2. **`RawMeta`**：chunk 元数据的「跨语言视图」，字段布局与 Rust 的 `ChunkMeta` 用 `static_assertions` 强制对齐对等。
3. **裸指针 `*mut WritingChunk` / `*const Chunk`**：跨语言传递的「未提交写句柄」与「只读 chunk 引用」，靠 `Arc::into_raw` / `Box::into_raw` 交出，靠对应回收函数归还。

#### 4.2.3 源码精读

**共享类型与桥声明**：整个 `#[cxx::bridge]` 块定义了跨语言契约。`UpdateReq` 是请求与输出合一的典范——注意末尾的 `out_*` 字段：[src/storage/chunk_engine/src/cxx.rs:368-397](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/cxx.rs#L368-L397)

```rust
#[::cxx::bridge(namespace = "hf3fs::chunk_engine")]
pub mod ffi {
    struct UpdateReq {
        // —— C++ 填的输入 ——
        is_truncate: bool,
        is_remove: bool,
        is_syncing: bool,
        update_ver: u32,
        chain_ver: u32,
        checksum: u32,
        length: u32,
        offset: u32,
        data: u64,            // 实为裸指针，指向 length 字节数据
        // —— Rust 回填的输出 ——
        out_non_existent: bool,
        out_error_code: u16,
        out_commit_ver: u32,
        out_chain_ver: u32,
        out_checksum: u32,
    }
    ...
}
```

> 注意 `data: u64` 的注释（源码第 381-383 行）特别强调它其实是一个裸指针，必须在 `update_chunk()` 执行期间保持有效——目前安全是因为数据在该函数内被同步消费。

**Rust 暴露给 C++ 的方法清单**：`extern "Rust"` 块列出所有可被 C++ 调用的函数，cxx 据此生成 C++ 头文件：[src/storage/chunk_engine/src/cxx.rs:457-472](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/cxx.rs#L457-L472)

```rust
extern "Rust" {
    type Engine;
    fn create(path: &str, create: bool, prefix_len: usize,
              error: Pin<&mut CxxString>) -> *mut Engine;
    fn release(engine: Box<Engine>);
    ...
    fn get_raw_chunk(&self, chunk_id: &[u8], error: Pin<&mut CxxString>) -> *const Chunk;
    fn update_raw_chunk(&self, chunk_id: &[u8], req: Pin<&mut UpdateReq>,
                        error: Pin<&mut CxxString>) -> *mut WritingChunk;
    unsafe fn commit_raw_chunk(&self, new_chunk: *mut WritingChunk,
                               sync: bool, error: Pin<&mut CxxString>);
    ...
}
```

**`create`：构造引擎并交出所有权**：返回 `*mut Engine`，C++ 侧用 `rust::Box<Engine>::from_raw` 包成独占指针持有：[src/storage/chunk_engine/src/cxx.rs:10-23](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/cxx.rs#L10-L23)。失败时往 `error` 写信息并返回空指针——这是整个 FFI 层统一的错误约定。

**`update_raw_chunk`：把 `Result` 翻译成指针 + 错误码**：这是适配层的典型。成功返回 `Box::into_raw` 的裸指针；失败时把 Rust 的 `Error` 枚举映射成与 C++ `StorageCode` 对齐的数字：[src/storage/chunk_engine/src/cxx.rs:143-170](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/cxx.rs#L143-L170)

```rust
fn update_raw_chunk(...) -> *mut WritingChunk {
    match self.update_chunk(chunk_id, &mut req) {
        Ok(chunk) => Box::into_raw(Box::new(chunk)),
        Err(e) => {
            error.push_str(&e.to_string());
            req.out_error_code = match e {
                Error::ChecksumMismatch(_) => 4080,        // ChecksumMismatch
                Error::ChainVersionMismatch(_) => 4081,   // ChainVersionMismatch
                Error::ChunkAlreadyExists => 4084,        // ChunkAlreadyExists
                Error::ChunkCommittedUpdate(_) => 4008,   // ChunkCommittedUpdate
                Error::ChunkMissingUpdate(_) => 4007,     // ChunkMissingUpdate
                Error::NoSpace => 7021,                   // NoSpace
                ...                                        // 其余分支同理
            };
            std::ptr::null_mut()
        }
    }
}
```

**C++ 侧的薄封装**：`ChunkEngine::update` 把 storage 的 `UpdateJob` 翻译成 `UpdateReq`，调用 `update_raw_chunk`，再从 `out_*` 字段读结果填回 `job.result()`：[src/storage/store/ChunkEngine.cc:15-80](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/store/ChunkEngine.cc#L15-L80)。关键三行是边界穿越点：

```cpp
chunk_engine::UpdateReq req{};
// ... 把 job 的字段填进 req（chain_ver / update_ver / checksum / data 指针 ...）
std::string error{};
auto chunk = engine.update_raw_chunk(toSlice(key), req, error);   // ← 跨入 Rust
result.updateVer = result.commitVer = ChunkVer{req.out_commit_ver}; // ← 读 Rust 回填
result.commitChainVer = ChainVer{req.out_chain_ver};
if (UNLIKELY(!error.empty())) {
  return makeError(req.out_error_code, std::move(error));          // ← 错误码跨语言对齐
}
```

**构建侧**：Rust 侧声明 `crate-type = ["lib", "staticlib"]`，才能编出可被 C++ 链接的静态库：[src/storage/chunk_engine/Cargo.toml:8-9](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/Cargo.toml#L8-L9)。构建脚本 `build.rs` 调 `cxx_build::bridge("src/cxx.rs")` 生成 C++ 头：[src/storage/chunk_engine/build.rs:1-4](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/build.rs#L1-L4)。C++ 工程再经 `add_crate(chunk_engine)` 宏把静态库接入（见 [src/storage/CMakeLists.txt:1](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/CMakeLists.txt#L1)，回顾 u1-l2）。

#### 4.2.4 代码实践

**实践目标**：亲手追踪一次「写请求」从 C++ 字段填入到 Rust 回填结果的全过程，标注跨语言边界的数据结构。

**操作步骤**：

1. 打开 [src/storage/store/ChunkEngine.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/store/ChunkEngine.cc) 的 `update` 函数（第 15-80 行）。
2. 找到第 32 行 `chunk_engine::UpdateReq req{}`，逐行往下，记录每个 `req.xxx = ...` 是从 `UpdateJob` 的哪个字段来的。
3. 找到第 60 行 `engine.update_raw_chunk(...)`——这是 C++→Rust 的边界。在笔记上画一条竖线标记此处。
4. 跳到 [src/storage/chunk_engine/src/cxx.rs](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/cxx.rs) 第 143-170 行的 `update_raw_chunk`，确认它把 `req` 透传给 `update_chunk`。
5. 回到 ChunkEngine.cc 第 61-67 行，记录 Rust 通过 `req.out_commit_ver / out_chain_ver / out_checksum` 回填了什么。

**需要观察的现象**：`req` 这个结构体在跨边界前被 C++ 写入输入字段，跨边界后被 Rust 写入 `out_*` 字段，C++ 再读出来。同一个结构体走了个来回。

**预期结果**：你能画出一张表，左列是 `req` 的输入字段及来源，右列是 `out_*` 输出字段及含义，中间标注 `update_raw_chunk` 这道语言边界。**待本地验证**：若想确认错误码对齐，可在 Rust 的 `match` 里临时加日志（仅阅读型实践，不要求改源码）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 FFI 函数失败时不直接 `panic!`，而是返回空指针 + 写 `error` 字符串？

> **参考答案**：跨 FFI 边界的 `panic` 是未定义行为（会 abort 或踩坏栈）。3FS 用「空指针 + out_error_code + error 字符串」这种 C 风格约定传递失败，既安全又能让 C++ 侧用统一的 `makeError` 重建错误对象。

**练习 2**：`UpdateReq` 为什么把输入和 `out_*` 输出字段塞在同一个结构体里？

> **参考答案**：cxx 跨语言传递一个 `Pin<&mut UpdateReq>` 即可让两边读写同一块内存。输入输出合一避免了「再定义一个返回类型、再做一次跨语言序列化」的开销，是一次 FFI 调用同时完成「请求下行 + 结果上行」。

---

### 4.3 线程安全接口：get / update / commit

#### 4.3.1 概念说明

storage 服务的写流量由按磁盘分队的 `UpdateWorker` 串行化（回顾 u5-l3），但**多个磁盘、多个 target、读写两类流量**仍会从不同 C++ 线程并发调用同一个 Rust 引擎。chunk engine 必须在「没有粗粒度全局锁」的前提下保证线程安全。它的设计依赖四件套：

1. **`Arc` 引用计数守护 chunk 生命周期**：读操作拿到一个 `Arc<Chunk>`（指向物理块位置），只要这个 `Arc` 还在，对应的磁盘空间就不会被回收。于是「读」与「并发的写/删」天然无冲突——读到的永远是拿引用那一刻的有效快照。
2. **`LockMap` 分片锁做 per-chunk 串行**：`meta_cache` 是 256 分片的锁映射表，对同一个 `chunk_id` 的操作会被同一把分片锁串行，不同 chunk 则完全并行。
3. **`writing_list` 跟踪未提交写**：一个 chunk 处于「写了但没 commit」期间，会被登记进 `writing_list`，防止同一 chunk 被重复 `update`。
4. **RocksDB `WriteBatch` 做原子持久化**：新元数据与旧块释放记录在同一个写批里，要么全成功要么全失败。

理解这套模型后，`get / update_chunk / commit_chunk` 三个接口就是「读、写、提交」三步的编排者。

#### 4.3.2 核心流程

三个接口的分工（`update` = `update_chunk` + 立即 `commit_chunk`，是「一步写」的快捷方式）：

```text
get(chunk_id)                       —— 读路径
  ├─ meta_cache 命中? → 返回 Arc<Chunk>（仅克隆 Arc，O(1)）
  └─ 未命中 → meta_store.get_chunk_meta → allocator.reference → 存入 cache → 返回 Arc

update_chunk(chunk_id, req)         —— 写路径（产出 WritingChunk，尚未对外可见）
  1. 校验 checksum / chain_ver / chunk_ver（CRAQ 版本语义）
  2. 取旧 chunk；按情况选 copy_on_write / safe_write / 全新 allocate
  3. 登记进 writing_list（同 chunk 重复写会报 InvalidArg）
  4. meta_store.persist_writing_chunk（先落盘「正在写」标记，崩溃可恢复）
  5. 返回 WritingChunk（持有新 chunk 与释放旧 chunk 的职责）

commit_chunk(WritingChunk, sync)    —— 提交路径（对外可见）
  1. 在 meta_cache 的分片锁保护下
  2. meta_store.move_chunk / add_chunk（RocksDB WriteBatch 原子写：新元数据 + 旧块释放）
  3. meta_cache 替换为新 Arc<Chunk>
  4. WritingChunk 标记 commit_succ
```

这里体现了 README 强调的核心安全保证：**「读」拿 `Arc` 引用、「写」产出新块再原子替换**。在读完成之前，旧块因为还有 `Arc` 引用而不会被回收，所以读写无冲突。

#### 4.3.3 源码精读

**`get`：先查内存缓存，未命中才读 RocksDB**：`get_with_entry` 在分片锁（`entry_by_ref`）保护下，命中则克隆 `Arc` 返回，未命中则查 `meta_store` 并填缓存：[src/storage/chunk_engine/src/core/engine.rs:184-209](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/core/engine.rs#L184-L209)

```rust
pub fn get(&self, chunk_id: &[u8]) -> Result<Option<ChunkArc>> {
    let mut entry = self.meta_cache.entry_by_ref(chunk_id);   // 取分片锁
    self.get_with_entry(chunk_id, &mut entry)
}

fn get_with_entry(...) -> Result<Option<ChunkArc>> {
    match entry.get() {
        Some(chunk) => Ok(Some(chunk.clone())),   // 命中：只克隆 Arc
        None => {
            let meta = self.meta_store.get_chunk_meta(chunk_id)?;  // 未命中：查 RocksDB
            ...
            let chunk = Arc::new(allocator.reference(meta, true));
            entry.insert(chunk.clone());          // 填缓存
            Ok(Some(chunk))
        }
    }
}
```

> `entry_by_ref` 来自 `lockmap` crate，提供「按 key 哈希到分片、返回持有锁的 entry」的语义，正是 256 分片锁的入口。

**`update_chunk`：CRAQ 版本校验 + copy_on_write/allocate 分支**：函数较长，核心是先做版本检查、再按旧 chunk 状态选写法。版本检查保证 CRAQ 不变量 `commitVer ≤ updateVer ≤ commitVer+1`：[src/storage/chunk_engine/src/core/engine.rs:351-376](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/core/engine.rs#L351-L376)

```rust
// 3. check version.
if req.chain_ver < req.out_chain_ver {
    return Err(Error::ChainVersionMismatch(...));   // 链版本倒退
}
let new_chunk_ver = if req.is_syncing {
    req.update_ver                                  // 数据恢复：直接覆盖
} else if req.update_ver > 0 {
    if req.update_ver <= req.out_commit_ver {
        return Err(Error::ChunkCommittedUpdate(...));  // 重复写已提交版本
    } else if req.update_ver > req.out_commit_ver + 1 {
        return Err(Error::ChunkMissingUpdate(...));    // 跳号，缺中间版本
    }
    req.update_ver
} else {
    req.out_commit_ver + 1                             // 自动递增
};
```

随后登记 `writing_list` 并 `persist_writing_chunk`，返回 `WritingChunk`：[src/storage/chunk_engine/src/core/engine.rs:441-479](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/core/engine.rs#L441-L479)。`writing_list` 用 `Entry::Occupied` 检测「该 chunk 已在写」，重复写直接 `InvalidArg`，这是 per-chunk 的并发保护。

**`commit_chunk`：在分片锁下原子替换**：取 `meta_cache` 的 entry 锁，把旧块 move 成新块（`move_chunk` 内部用 RocksDB WriteBatch 同时写新元数据与释放旧块），再替换缓存里的 `Arc`：[src/storage/chunk_engine/src/core/engine.rs:501-518](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/core/engine.rs#L501-L518)

```rust
// update rocksdb under lock protection.
let mut entry = self.meta_cache.entry_by_ref(chunk_id);
match self.get_with_entry(chunk_id, &mut entry)? {
    Some(old_chunk) => {
        self.meta_store.move_chunk(chunk_id, old_chunk.meta(), new_chunk.meta(), sync)?;
    }
    None => {
        self.meta_store.add_chunk(chunk_id, new_chunk.meta(), sync)?;
    }
}
entry.insert(new_chunk.clone());   // 原子替换缓存中的 Arc
```

> 注意注释 `// update rocksdb under lock protection.`：RocksDB 写与缓存替换被同一把分片锁串行，保证「外部可见的新 chunk」与「已落盘的元数据」一致。

**C++ 侧如何并发持有引擎**：`StorageTargets` 启动期为每个磁盘路径创建一个引擎，用 `rust::Box<chunk_engine::Engine>` 持有：[src/storage/store/StorageTargets.cc:52-71](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/store/StorageTargets.cc#L52-L71)。关键三行：

```cpp
auto engine = chunk_engine::create(engine_path.c_str(), create, sizeof(ChainId), error);
...
co_return rust::Box<chunk_engine::Engine>::from_raw(engine);   // C++ 独占持有
```

注意第三个参数 `sizeof(ChainId)` 即 `prefix_len`——它决定了 `writing_list` 按「chainId 前缀」分桶，与 C++ 侧 key 拼成 `chainId + chunkId` 的布局完全吻合（见 ChunkEngine.cc 第 27-29 行）。

随后 `StorageTarget` 持有一个裸指针 `engine_`，在读/写路径上用 `useChunkEngine()` 在 Rust 引擎与旧 C++ `ChunkStore` 之间二选一：成员声明 [src/storage/store/StorageTarget.h:169](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/store/StorageTarget.h#L169)，写路径分流 [src/storage/store/StorageTarget.cc:307-316](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/store/StorageTarget.cc#L307-L316)

```cpp
void StorageTarget::updateChunk(UpdateJob &job, folly::CPUThreadPoolExecutor &executor) {
  if (job.type() == UpdateType::COMMIT) {
    if (useChunkEngine()) {
      job.setResult(ChunkEngine::commit(*engine_, job, config_.kv_store().sync_when_write()));
    } else {
      job.setResult(ChunkReplica::commit(chunkStore_, job));
    }
  } else {
    auto result = useChunkEngine()
                      ? ChunkEngine::update(*engine_, job)        // ← 走 Rust 引擎
                      : ChunkReplica::update(chunkStore_, job, executor);  // ← 旧 C++ 实现
    ...
  }
}
```

> 这解释了为什么 chunk engine 的 FFI 接口要设计得和旧 `ChunkReplica` 一一对应：它是 storage 落盘后端的「可替换实现」，由配置开关切换。

#### 4.3.4 代码实践

**实践目标**：验证 chunk engine 的「读写无冲突」设计——读操作拿到的 `Arc` 引用，能在并发写/删下保持有效。

**操作步骤**（阅读型实践）：

1. 打开 [src/storage/chunk_engine/src/core/engine.rs](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/core/engine.rs) 的测试 `test_engine_concurrent_update_and_get`（第 1672-1704 行）。
2. 阅读它的结构：一个 `commit_thread` 循环 `write → remove`，另一个 `get_thread` 循环 `get`，两者并发跑 2 秒。
3. 结合本节讲的 `Arc` 守护机制，解释为什么 `get_thread` 不会因为 `remove` 而读到悬空数据。

**需要观察的现象**：测试没有加任何全局锁，却能在「边写边删边读」下安全通过。

**预期结果**：你能用本节的四件套（`Arc` 引用守护、`LockMap` 分片锁、`writing_list`、WriteBatch）解释这个并发测试为何安全——读线程拿到的 `Arc<Chunk>` 即使在 `remove` 之后，物理块也因引用计数未归零而不会被回收。**待本地验证**：若本地装好 Rust 工具链，可在 `src/storage/chunk_engine` 下执行 `cargo test test_engine_concurrent_update_and_get` 实跑确认。

#### 4.3.5 小练习与答案

**练习 1**：`commit_chunk` 里为什么要先 `meta_store.move_chunk` 再 `entry.insert`？反过来会怎样？

> **参考答案**：必须先落盘再换缓存。如果先 `entry.insert`（对外可见新 chunk）再落盘，一旦落盘前崩溃，缓存里有新 chunk、磁盘上是旧元数据，重启后元数据丢失。先落盘则保证「对外可见的新 chunk 必然已持久化」，崩溃也最多回到旧版本。而且两者在同一把分片锁下，不会被其他线程看到中间态。

**练习 2**：C++ 侧持有的是 `engine_`（`chunk_engine::Engine *` 裸指针），而 `StorageTargets` 持有的是 `rust::Box<Engine>`。这会不会有生命周期问题？

> **参考答案**：不会。`StorageTargets` 持有 `rust::Box` 是所有权的真正归属（RAII，析构时调 Rust 的 `release`）；`StorageTarget::engine_` 只是从 Box 借出的裸指针，其生命周期严格短于 `StorageTargets`。只要 `StorageTargets` 不析构，`engine_` 就有效——这是典型的「拥有者 vs 借用者」关系。

---

## 5. 综合实践

把三个模块串起来，完成本讲规格要求的实践：**绘制从 C++ `ChunkEngine::update` 到 Rust `engine.update_raw_chunk` 的完整调用链，标注跨语言边界的数据结构**。

具体做法：

1. **准备一张白纸或文本文件**，画出以下纵向调用栈，每层标注文件名与行号：

   ```text   StorageTarget::updateChunk(job)            [StorageTarget.cc:316]
        │  useChunkEngine() 为真
        ▼
   ChunkEngine::update(*engine_, job)        [ChunkEngine.cc:15]
        │  拼 key = chainId(8B) + chunkId
        │  构造 chunk_engine::UpdateReq req{...}
        ▼
   ════════ 语言边界（C++ → Rust）══════════
   engine.update_raw_chunk(key_slice, &mut req, &mut error)   [cxx.rs:143]
        │  Arc/Box 裸指针 + Pin<&mut UpdateReq> + Pin<&mut CxxString>
        ▼
   Engine::update_chunk(chunk_id, &mut req)  [engine.rs:295]
        │  版本校验 → copy_on_write/safe_write/allocate
        │  登记 writing_list → persist_writing_chunk
        ▼
   返回 *mut WritingChunk（或 null + out_error_code）
   ════════ 语言边界（Rust → C++）══════════
        ▼
   ChunkEngine::update 读 req.out_commit_ver / out_chain_ver / out_checksum
   job.chunkEngineJob().set(engine, chunk)    [ChunkEngine.cc:73]
   ```

2. **在边界两侧标注跨语言数据结构**：
   - `UpdateReq`（输入 + `out_*` 输出合一，cxx 共享内存布局）。
   - `key`（`chainId + chunkId`，`prefix_len = sizeof(ChainId) = 8`）。
   - `*mut WritingChunk`（Rust 用 `Box::into_raw` 交出，C++ 稍后在 `commit` 时用 `commit_raw_chunk`→`Box::from_raw` 归还）。
   - `error`（`Pin<&mut CxxString>`，失败时回传字符串）。

3. **补全提交半链**：接着画出 `StorageTarget::updateChunk` 中 `job.type() == COMMIT` 分支 → `ChunkEngine::commit`（[ChunkEngine.cc:82](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/store/ChunkEngine.cc#L82)）→ `engine.commit_raw_chunk`（[cxx.rs:172](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/cxx.rs#L172)）→ `Engine::commit_chunk`（[engine.rs:481](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/src/core/engine.rs#L481)）。

4. **对照检验**：在你的图上找到「`Arc` 引用守护」与「`LockMap` 分片锁」出现的位置，确认它们都在 Rust 侧（`get_with_entry` / `commit_chunk` 的 `entry_by_ref`），C++ 侧完全不操心并发——这正是 FFI 设计成功的关键：把线程安全封装在 Rust 引擎内部，C++ 只当它是线程安全黑盒。

**预期结果**：一张标注完整的端到端调用链图，能清楚区分「C++ 薄封装层」「cxx FFI 边界」「Rust 业务层」三段，并说出每段跨边界传递的数据结构。这是后续阅读 u6-l2（物理块分配器）与 u6-l3（RocksDB 元数据）的地图。

## 6. 本讲小结

- chunk engine 是 storage 落盘后端的 Rust 实现，由 **meta（RocksDB 持久化）/ alloc（物理块分配）/ file（文件组织）** 三层加 `core` 编排构成；`Engine` 所有字段都是 `Arc`，可被多线程廉价共享。
- 它通过 **cxx** 库做 FFI：`#[cxx::bridge]` 声明共享类型（`UpdateReq`/`RawMeta`）与 `extern "Rust"` 方法，`build.rs` 生成 C++ 头，`Cargo.toml` 声明 `staticlib` 产出静态库。
- 适配层（`update_raw_chunk` 等带 `_raw` 后缀的函数）把 Rust 的 `Result<T>` 翻译成 C 风格的「裸指针 + `out_error_code` + `error` 字符串」，错误码与 C++ `StorageCode` 数字对齐。
- `UpdateReq` 输入与 `out_*` 输出合一，一次 `Pin<&mut UpdateReq>` 往返同时完成请求下行与结果上行。
- 跨语言所有权靠 `Arc::into_raw`/`Box::into_raw` 交出裸指针、靠 `release_raw_chunk`/`commit_raw_chunk` 归还，`rust::Box<Engine>` 是 C++ 侧的真正拥有者。
- 线程安全靠四件套：**`Arc` 引用守护 chunk 生命周期**（读写无冲突）、**`LockMap` 256 分片锁**（per-chunk 串行）、**`writing_list`**（防重复写）、**RocksDB WriteBatch**（原子持久化）；这套机制全封装在 Rust 内部，C++ 侧以裸指针当线程安全黑盒使用。

## 7. 下一步学习建议

本讲只看了「引擎骨架与 FFI 边界」，还没进任何一层的实现。建议按以下顺序继续：

1. **u6-l2 物理块分配器**：进入 `alloc` 层，看 `Allocators / ChunkAllocator / GroupAllocator` 如何用 256 位位图管理 11 种块大小、如何 `fallocate` 扩容、如何做 copy_on_write。这是 `update_chunk` 第 416 行 `self.allocators.allocate(...)` 的内部。
2. **u6-l3 Chunk 元数据与 RocksDB**：进入 `meta` 层，看 `meta_store.rs / meta_key.rs / rocksdb.rs` 的 key 编码、MergeOp 与 WriteBatch 原子提交。这是 `commit_chunk` 第 506 行 `meta_store.move_chunk(...)` 的内部。
3. 回顾 **u5-l3 写路径与 CRAQ**：本讲的版本校验（`ChainVersionMismatch / ChunkCommittedUpdate / ChunkMissingUpdate`）正是 CRAQ 双版本不变量在物理层的强制执行，两讲互为印证。
4. 若想看一个「脱离 storage 服务、独立跑」的引擎示例，可读 [src/storage/chunk_engine/examples/chunk_viewer.rs](https://github.com/deepseek-ai-3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/chunk_engine/examples/chunk_viewer.rs)，它不经过 FFI，直接用 Rust API 操作引擎，有助于剥离 FFI 复杂度理解纯业务逻辑。
