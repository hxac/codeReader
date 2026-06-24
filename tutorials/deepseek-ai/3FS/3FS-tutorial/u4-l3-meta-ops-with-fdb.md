# 用 FoundationDB 事务实现元数据操作

> 所属单元：u4 元数据服务 meta · 依赖讲义：[u4-l2](u4-l2-inode-direntry-encoding.md)（Inode/DirEntry 的 KV 编码）、[u2-l6](u2-l6-fdb-and-transactions.md)（FDB 客户端与事务封装）

## 1. 本讲目标

学完本讲，你应当能够：

- 区分 3FS meta 服务里**只读事务**（如 `stat` / `lookup` / `listdir`）与**读写事务**（如 `open(O_CREAT)` / `remove` / `rename`）两种姿态，并知道为什么大部分读要用「快照读」。
- 读懂 `RenameOp` / `RemoveOp` / `OpenOp` 这类**多 key 原子操作**是如何在一个 FDB 事务里「全有或全无」地完成的。
- 理解 **冲突范围（conflict range）**：为什么 `PathResolveOp` 用快照读，又在提交前手动 `addIntoReadConflict`；以及 `set` / `clear` 为什么天然进写冲突范围。
- 说清**并发冲突触发自动重试**的完整链路：`OperationDriver` 重试循环 → `FDBRetryStrategy::onError` → `commit_unknown_result` 与幂等保护。

本讲只讲「元数据操作如何跑在 FDB 事务上」，不讲 inode key 的字节布局（见 u4-l2），也不讲 FDB C API 封装细节（见 u2-l6）。

## 2. 前置知识

在进入源码前，先建立三条直觉。

**① meta 服务无状态，事务是唯一的元数据操作边界。**

meta 进程内存里没有业务元数据，inode、目录项、session、布局全部存在 FoundationDB（下称 FDB）里。一次元数据 RPC 的本质是：**开一个 FDB 事务 → 在事务里读改写若干 key → 提交（commit）**。崩溃/重启不丢数据，因为数据本就在 FDB。

**② FDB 是乐观并发控制（optimistic concurrency）的 KV。**

读写事务在提交时，FDB 会做冲突检测：

- 你**读过**（且进了读冲突范围）的 key，如果在你的「读版本」之后被别的事务改过 → 提交失败，返回冲突错误；
- 你**写过**（`set` / `clear`）的 key，自动进入写冲突范围。

因此「我依赖哪些 key 不被别人改」必须显式声明，这就是**读冲突范围**。没有声明就等于「我不在乎它被改」，读到的可能是旧值。

**③ 快照读 vs 冲突读。**

FDB 提供两种读：

| 读法 | 进读冲突范围？ | 可能读到旧值？ | 适用场景 |
|------|--------------|--------------|---------|
| `get` / `getRange`（冲突读） | 是 | 否（事务一致快照） | 读了就要据此写，必须保证一致 |
| `snapshotGet` / `snapshotGetRange`（快照读） | 否 | 是（可读到提交版本之前的旧值） | 纯展示、或随后会显式补冲突 |

这条区分贯穿整个 meta 服务的实现，是本讲最重要的概念之一。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/common/kv/ITransaction.h` | 事务抽象：`IReadOnlyTransaction` / `IReadWriteTransaction`，定义 `snapshotGet` / `get` / `addReadConflict` / `set` / `clear` / `commit`。 |
| `src/common/kv/WithTransaction.h` | 通用「事务 + 重试」封装模板（meta 用的是自己的 `OperationDriver`，但语义一致）。 |
| `src/fdb/FDBRetryStrategy.h` | FDB 重试策略：退避、`maxRetryCount`、`maybe_committed` 处理。 |
| `src/meta/store/Operation.h` | `Operation` / `ReadOnlyOperation` 基类，以及核心重试驱动 `OperationDriver`。 |
| `src/meta/store/PathResolve.h` | 路径解析 `PathResolveOp`，**全程用快照读**，不进读冲突范围。 |
| `src/meta/store/Inode.h` / `Inode.cc` | `snapshotLoad`（快照） vs `load`（冲突读）；`addIntoReadConflict`。 |
| `src/meta/store/DirEntry.h` / `DirEntry.cc` | `addIntoReadConflict`；`store`(`set`) / `remove`(`clear`)。 |
| `src/meta/store/ops/Stat.cc` | **只读操作**的样板：`StatOp : ReadOnlyOperation`。 |
| `src/meta/store/ops/Open.cc` | **读写操作**的样板：`OpenOp`，含 `isReadOnly()` 判定与 `BEGIN_WRITE()` 宏。 |
| `src/meta/store/ops/Remove.cc` | 删除操作：空目录直接删、非空走 GC、递归删除。 |
| `src/meta/store/ops/Rename.cc` | 最复杂的原子操作：环路检测、占位冲突范围、原子的「删源 + 删目的 + 建目的」。 |
| `src/meta/service/MetaOperator.cc` | `runOp`：创建 FDB 事务、构造 `OperationDriver`、启动重试循环。 |

---

## 4. 核心概念与源码讲解

本讲对应三个最小模块：**读写事务**、**原子操作**、**冲突重试**。

### 4.1 读写事务：只读与读写的两种姿态（读写事务模块）

#### 4.1.1 概念说明

meta 把每个元数据 RPC 映射成一个 `Operation<Rsp>` 对象。`Operation` 基类有两个姿态：

- **读写操作**（`Operation<Rsp>`）：`run(IReadWriteTransaction &)`，**可以** `set` / `clear` / `commit`。`create` / `open(O_WRONLY)` / `remove` / `rename` 都是它。
- **只读操作**（`ReadOnlyOperation<Rsp>`，继承 `Operation`）：`run(IReadOnlyTransaction &)`，**不会**提交，也不会写。`stat` / `lookup` / `listdir` / `batchStat` 都是它。

为什么这么分？因为 FDB 里只读事务代价更低：它不需要拿写锁、不参与冲突检测的写半边、还能用 GRV cache 加速。3FS 让能确定只读的操作显式声明只读，省掉提交开销。

> 关键术语：
> - **`IReadOnlyTransaction`**：只能 `snapshotGet` / `get` / `getRange`，不能 `set` / `clear` / `commit`。
> - **`IReadWriteTransaction`**（继承前者）：额外有 `addReadConflict` / `set` / `clear` / `commit`。
> - **`isReadOnly()`**：每个 `Operation` 自己声明；`OperationDriver` 据此决定是否提交。

#### 4.1.2 核心流程

一次元数据 RPC 的执行路径（承接 u4-l1）：

```
MetaOperator.runOp
  → kvEngine_.createReadWriteTransaction()          // 总是建读写事务
  → OperationDriver(op, req, deadline).run(txn, ...)
       └─ while (未超时 && 未成功):
            ├─ runAndCommit(txn, op)                 // 跑 op.run(txn)，成功且非只读则 commit
            ├─ op.retry(err)                         // 清理本次产生的 events
            └─ strategy.onError(txn, err)            // 退避 + reset，决定是否再试
```

注意两点：

1. **事务对象总是 `IReadWriteTransaction`**。即使 `stat` 是只读的，底层 txn 也是读写事务；只不过 `ReadOnlyOperation` 的 `run(IReadWriteTransaction&)` 只把它当 `IReadOnlyTransaction&` 用，且 `runAndCommit` 不会调 `commit`。这是「能力收窄」而非「换了对象」。
2. **只读操作永不提交**。`OperationDriver::runAndCommit` 里只有 `!readonly` 才 `txn.commit()`。

#### 4.1.3 源码精读

**事务抽象——快照读与冲突读的区别写在接口注释里。**

[ITransaction.h:34-45](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/kv/ITransaction.h#L34-L45) 定义 `IReadOnlyTransaction`，其中 `get` 默认 fallback 到 `snapshotGet`：

```cpp
virtual CoTryTask<std::optional<String>> snapshotGet(std::string_view key) = 0;
virtual CoTryTask<std::optional<String>> get(std::string_view key) {
  co_return co_await snapshotGet(key);  // 默认不进冲突范围
}
```

而读写事务**重写** `get`，让它真正进读冲突范围。注释说得很直白：

[ITransaction.h:88-110](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/kv/ITransaction.h#L88-L110)（`IReadWriteTransaction` 与读冲突声明 API）：

```cpp
// 冲突读：读的同时声明「我依赖这个 key」
CoTryTask<std::optional<String>> get(std::string_view key) override = 0;

// 只声明读冲突范围，但不真正读（用于「读了快照，但要补冲突」）
virtual CoTryTask<void> addReadConflict(std::string_view key) = 0;
virtual CoTryTask<void> addReadConflictRange(std::string_view begin, std::string_view end) = 0;

virtual CoTryTask<void> set(std::string_view key, std::string_view value) = 0;   // 进写冲突范围
virtual CoTryTask<void> clear(std::string_view key) = 0;                          // 进写冲突范围
virtual CoTryTask<void> commit() = 0;
```

**`Operation` 与 `ReadOnlyOperation` 的分层。**

[Operation.h:46-53](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/Operation.h#L46-L53)：读写操作的 `run` 接收 `IReadWriteTransaction`；

[Operation.h:134-146](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/Operation.h#L134-L146)：只读操作把 `IReadWriteTransaction` 向下转型为 `IReadOnlyTransaction`，且 `isReadOnly()` 恒为 `true`：

```cpp
template <typename Rsp>
class ReadOnlyOperation : public Operation<Rsp> {
  bool isReadOnly() final { return true; }
  virtual CoTryTask<Rsp> run(IReadOnlyTransaction &) = 0;
  CoTryTask<Rsp> run(IReadWriteTransaction &txn) final {
    co_return co_await run(static_cast<IReadOnlyTransaction &>(txn));  // 能力收窄
  }
};
```

**只读操作的样板：`StatOp`。**

[Stat.cc:31-65](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/Stat.cc#L31-L65)：`stat` 继承 `ReadOnlyOperation`，`run(IReadOnlyTransaction &)` 只做路径解析 + 读 inode，全程快照读，不提交：

```cpp
class StatOp : public ReadOnlyOperation<StatRsp> {
  CoTryTask<StatRsp> run(IReadOnlyTransaction &txn) override {
    CHECK_REQUEST(req_);
    auto stat = co_await resolve(txn, req_.user)
                    .inode(req_.path, req_.flags, !config().allow_stat_deleted_inodes());
    CO_RETURN_ON_ERROR(stat);
    ...
    co_return StatRsp(std::move(*stat));
  }
};
```

**读写操作的样板：`OpenOp` 的 `isReadOnly()` 与 `BEGIN_WRITE()`。**

`open` 很有意思——它**可能只读，也可能读写**，取决于 flags。[Open.cc:39-42](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/Open.cc#L39-L42)：

```cpp
bool isReadOnly() final {
  return !req_.session.has_value() && req_.flags.accessType() == AccessType::READ
      && !req_.flags.contains(O_TRUNC) && !req_.flags.contains(O_CREAT);
}
```

即：纯 `O_RDONLY`、不带 session、不 truncate、不 create 的 `open` 才算只读。

`OpenOp::run` 整体接收 `IReadWriteTransaction`，但大部分子步骤只读。当真正需要写时（`O_TRUNC` 替换文件、写打开建 session、清 SUID/SGID 位），用一个 `BEGIN_WRITE()` 宏显式「升级」并断言当前确实是读写事务。[Open.cc:18-24](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/Open.cc#L18-L24)：

```cpp
#define BEGIN_WRITE()                                                                              \
  if (this->isReadOnly()) { ... co_return makeError(MetaCode::kFoundBug, ...); }                   \
  auto &rwTxn = dynamic_cast<IReadWriteTransaction &>(txn);
```

这里 `dynamic_cast<IReadWriteTransaction &>` 是把之前以 `IReadOnlyTransaction&` 传参的同一个 txn 对象「还原」回读写事务引用——因为底层对象本就是读写事务（见 4.1.2 第 1 点）。用法见 [Open.cc:152-166](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/Open.cc#L152-L166)：写打开时建 session，并按需清 sticky 位。

**`runOp` 如何创建事务并启动驱动。**

[MetaOperator.cc:71-87](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/service/MetaOperator.cc#L71-L87)：每次 `runOp` 都新建一个读写事务，再交给 `OperationDriver`：

```cpp
auto txn = kvEngine_->createReadWriteTransaction();   // 总是读写事务
auto op = ((*metaStore_).*func)(std::forward<Arg>(arg));
auto driver = OperationDriver(*op, arg, deadline);
co_return co_await driver.run(std::move(txn), createRetryConfig(), config_.readonly(), config_.grv_cache());
```

#### 4.1.4 代码实践

**实践目标**：直观对比一个只读 op 与一个读写 op 的「事务姿态」。

**操作步骤**：

1. 打开 `src/meta/store/ops/Stat.cc`，确认 `StatOp` 继承 `ReadOnlyOperation`，且 `run` 的参数类型是 `IReadOnlyTransaction &`。在源码里搜不到任何 `set` / `clear` / `commit`。
2. 打开 `src/meta/store/ops/Open.cc`，确认 `OpenOp` 继承 `Operation<Rsp>`（读写），`run` 参数是 `IReadWriteTransaction &`，并在源码里找到至少两处 `BEGIN_WRITE();` 的使用。
3. 打开 `src/meta/store/Operation.h`，看 `OperationDriver::runAndCommit`（见 4.3.3）里 `if (!result.hasError() && !readonly) co_await txn.commit();`，体会「只读永不提交」。

**需要观察的现象**：只读 op 全程没有 `commit`，读写 op 在 `run` 成功返回后由 `OperationDriver` 统一 `commit`（而非在 op 内部 `commit`）。

**预期结果**：`stat` / `lookup` 这类请求对 FDB 是只读事务，`open(O_CREAT)` / `remove` / `rename` 是读写事务并最终 `commit`。

> 待本地验证：若你能跑起测试集群，可在 FDB 的 `transaction_stats` 里观察到 `stat` 不产生写、`create` 产生写。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `OpenOp` 不直接继承 `ReadOnlyOperation`，而是用 `isReadOnly()` 在运行期判断？

**参考答案**：因为 `open` 的只读/读写属性由**请求 flags 决定**（`O_RDONLY` / `O_TRUNC` / `O_CREAT` / 是否带 session），同一份代码路径要同时服务两种情况。继承关系是编译期固定的，无法表达「同一个 op 有时只读有时读写」，所以用运行期 `isReadOnly()` 配合 `BEGIN_WRITE()` 动态切换。

**练习 2**：`ReadOnlyOperation::run(IReadWriteTransaction&)` 把参数 `static_cast` 成 `IReadOnlyTransaction&`，这安全吗？

**参考答案**：安全。`IReadWriteTransaction` 公有继承 `IReadOnlyTransaction`，向上转型（向基类）是安全的；这里只是收窄可见接口，对象本身没变。底层 txn 仍是读写事务，只是只读 op 不去用它的写方法。

---

### 4.2 原子操作与冲突范围：rename / remove 的全有或全无（原子操作模块）

#### 4.2.1 概念说明

**原子操作**的核心：在一个 FDB 事务里改多个 key，要么全部提交、要么全部回滚——FDB 事务的 ACID 保证「全有或全无」。这在文件系统里至关重要：

- `rename` 要同时**删源目录项**、**建目的目录项**、必要时**改源 inode 的 parent/name**、**删被覆盖的目的项**。如果删了源却没建目的，文件就「丢」了。
- `remove` 一个非空目录要递归清理所有子项 + 自己的目录项 + 自己的 inode，必须原子。

而要让原子性在**并发**下成立，必须靠**冲突范围**：

- **读冲突范围**：声明「我这次操作的结论依赖这些 key 当前没被改」。FDB 在提交时检查：若这些 key 在你读过之后被别的事务改了，你的提交失败。
- **写冲突范围**：`set` / `clear` 自动登记。

3FS meta 的设计精髓在于：**路径解析（`PathResolveOp`）全程用快照读（不进冲突范围）以追求性能，但在真正要写的操作里，提交前手动把「我依赖的父目录 inode 和目录项」补进读冲突范围**。这样既享受了快照读的低延迟，又保证了并发正确性。

#### 4.2.2 核心流程

以 `RenameOp::run` 为例（POSIX 语义：目的若存在且为文件/空目录则覆盖）：

```
1. 并发解析 src、dst 两条路径（collectAll，都是快照读）
2. 一系列前置校验：
   - dst 是否已带本请求 uuid → 幂等去重（已执行过，直接返回）
   - src 必须存在；src/dst 是否同一项（无操作）；moveToTrash 不覆盖已有文件
   - dst 若是目录必须为空
   - 若 src 是目录：checkLoop（防把目录移进自己的子孙）
3. 权限检查（对 src/dst 父目录写权限、目录锁、sticky bit）
4. 【关键】补读冲突范围：
   - src 父 inode、src 目录项
   - dst 父 inode、dst 目录项（新名字）
5. 改写（这些 key 自动进写冲突范围）：
   - 若 src 是目录：更新 src inode 的 parent/name 并 store
   - remove src 目录项（clear）
   - removeDst：覆盖目的项（文件走 GC removeEntry、空目录删 inode、符号链接减 nlink）
   - store 新的 dst 目录项（set）
6. （由 OperationDriver 在 op 返回成功后）commit
```

第 4 步是理解原子性的钥匙：**补进去的这些读冲突范围，正是第 5 步那些写操作的「并发哨兵」**。任何并发事务只要改了这些「哨兵 key」，本次 rename 就会在提交时冲突，然后整体重试。

#### 4.2.3 源码精读

**为什么 `PathResolveOp` 不进冲突范围——注释直接点明。**

[PathResolve.h:22-27](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/PathResolve.h#L22-L27)：

```cpp
/**
 * PathResolveOp always use snapshotLoad, so it won't add any key into read conflict set.
 * User should add keys into read conflict set manually if needed.
 */
```

对应的 `snapshotLoad` 与 `load` 的区别，注释也写得清楚。[Inode.h:55-64](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/Inode.h#L55-L64)：

```cpp
// The difference of `snapshotLoad` and `load` is the former won't add key of inode into read conflict set.
static CoTryTask<std::optional<Inode>> snapshotLoad(IReadOnlyTransaction &txn, InodeId id);
static CoTryTask<std::optional<Inode>> load(IReadOnlyTransaction &txn, InodeId id);

CoTryTask<void> addIntoReadConflict(IReadWriteTransaction &txn) {
  co_return co_await txn.addReadConflict(packKey());   // 只声明，不读
}
```

底层差异在 [Inode.cc:63-65](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/Inode.cc#L63-L65)：`loadImpl` 按 `SNAPSHOT` 模板参数选 `snapshotGet`（不冲突）或 `get`（冲突）：

```cpp
auto func = SNAPSHOT ? &IReadOnlyTransaction::snapshotGet : &IReadOnlyTransaction::get;
auto result = co_await (txn.*func)(packKey(id));
```

**`rename` 的并发解析与前置校验。**

[Rename.cc:241-245](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/Rename.cc#L241-L245)：用 `collectAll` 并发解析两条路径（注意 `AT_SYMLINK_NOFOLLOW`，rename 不跟随符号链接）：

```cpp
auto [srcResult, dstResult] =
    co_await folly::coro::collectAll(resolve(txn, req_.user).path(req_.src, AtFlags(AT_SYMLINK_NOFOLLOW)),
                                     resolve(txn, req_.user).path(req_.dest, AtFlags(AT_SYMLINK_NOFOLLOW)));
```

[Rename.cc:248-255](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/Rename.cc#L248-L255) 是**幂等去重**的关键：若目的项已存在且其 `uuid` 等于本请求 `uuid`，说明上次重试其实已提交（只是没收到回包），直接返回，不重复执行。这处理了 `commit_unknown_result` 场景（见 4.3）。

**祖先环路检测：`checkLoop`。**

把目录移进它自己的子孙会形成环路，必须拒绝。[Rename.cc:71-103](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/Rename.cc#L71-L103)：加载 dst 的全部祖先，逐个检查是否命中 src：

```cpp
auto dstAncestors = std::vector<Inode>();
CO_RETURN_ON_ERROR(co_await Inode::loadAncestors(txn, dstAncestors, dstResult.getParentId()));
for (auto &ancestor : dstAncestors) {
  if (ancestor.id == srcResult.dirEntry->id) {
    // try to move directory into it's descendent
    co_return makeError(StatusCode::kInvalidArg, "try to move directory into it's descendent");
  }
  if (ancestor.nlink == 0) { co_return makeError(MetaCode::kNotFound); }  // 移进已删目录
  ...
}
```

注意 `loadAncestors` 用的是 `IReadWriteTransaction`，会读冲突（向上找父目录链路，这是 rename 正确性的根基，不能被并发改动蒙混过关）。

**补读冲突范围——原子性的「哨兵」。**

[Rename.cc:301-306](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/Rename.cc#L301-L306) 是本模块最核心的几行：

```cpp
// NOTE: add src/dst's parent inode and dirEntry into read conflict set.
CO_RETURN_ON_ERROR(co_await Inode(srcResult->getParentId()).addIntoReadConflict(txn));   // src 父 inode
CO_RETURN_ON_ERROR(co_await srcResult->dirEntry->addIntoReadConflict(txn));              // src 目录项
CO_RETURN_ON_ERROR(co_await Inode(dstResult->getParentId()).addIntoReadConflict(txn));   // dst 父 inode
CO_RETURN_ON_ERROR(
    co_await DirEntry(dstResult->getParentId(), req_.dest.path->filename().native())
        .addIntoReadConflict(txn));                                                        // dst 新名字目录项
```

**改写——这些 `set` / `clear` 自动进写冲突范围。**

[Rename.cc:312-331](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/Rename.cc#L312-L331)：目录要改 parent/name 后 `store`，删源目录项，覆盖目的项，建新目的项：

```cpp
if (srcEntry.isDirectory()) {
  inode.asDirectory().parent = dstResult->getParentId();
  inode.asDirectory().name = req_.dest.path->filename().native();
  CO_RETURN_ON_ERROR(co_await inode.store(txn));          // set：源 inode（目录）
}
CO_RETURN_ON_ERROR(co_await srcEntry.remove(txn));         // clear：源目录项
auto removeDstResult = co_await removeDst(txn, *dstResult, dstInode);  // clear：被覆盖的目的项
...
DirEntry newDstEntry(dstResult->getParentId(), req_.dest.path->filename().native());
CO_RETURN_ON_ERROR(co_await newDstEntry.store(txn));       // set：新目的目录项
```

其中 `DirEntry::store` 走 `txn.set`、`DirEntry::remove` 走 `txn.clear`，见 [DirEntry.cc:144-176](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/DirEntry.cc#L144-L176)（`store` 在 L160 `txn.set`，`remove` 在 L176 `txn.clear`）。

**`remove` 的原子删除（空目录分支）。**

[Remove.cc:145-162](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/Remove.cc#L145-L162)：空目录直接删目录项 + inode，同样补读冲突范围，保证原子：

```cpp
if (auto empty = *result; empty) {
  CO_RETURN_ON_ERROR(co_await entry.addIntoReadConflict(txn));   // 目录项哨兵
  CO_RETURN_ON_ERROR(co_await inode.addIntoReadConflict(txn));   // inode 哨兵
  CO_RETURN_ON_ERROR(co_await entry.remove(txn));                 // clear 目录项
  CO_RETURN_ON_ERROR(co_await inode.remove(txn));                 // clear inode
  ...
}
```

非空文件/目录不立即删 inode，而是交给 `gcManager().removeEntry`（延迟删除 + GC，见 u4-l5），但仍在一个事务里完成目录项的移除与 GC 入队，保持原子。

#### 4.2.4 代码实践

**实践目标**（本讲指定的核心实践）：分析 `rename(/a/f1, /b/f2)`（假设 `/b/f2` 已存在且为文件）在事务中读写了哪些 key、设置了哪些冲突范围，并说明何种并发会触发重试。

**操作步骤**：

1. 读 [Rename.cc:236-361](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/Rename.cc#L236-L361) 的 `run`，逐行列出对 `txn` 的调用。
2. 按「读（快照）/ 读冲突声明 / 写」三类整理下表（key 用 u4-l2 的前缀表示：`INOD` = inode，`DENT` = 目录项）：

   | 类别 | key（前缀示意） | 来源代码 |
   |------|----------------|---------|
   | 快照读（不冲突） | `INOD+f1_inode`、`INOD+f2_inode`、src/dst 路径上的祖先 inode、各 `DENT` 项 | `resolve(...).path(...)` 经 `snapshotLoad`；`snapshotLoadInode` |
   | 读冲突声明 | `INOD+a_dir_inode`、`DENT+a_dir+f1`、`INOD+b_dir_inode`、`DENT+b_dir+f2` | [Rename.cc:301-306](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/Rename.cc#L301-L306) |
   | 写（`set`/`clear`，自动写冲突） | `clear DENT+a_dir+f1`、GC 覆盖 `f2`、`set DENT+b_dir+f2`（带本请求 uuid） | [Rename.cc:322-331](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/Rename.cc#L322-L331)；若 f1 是目录还有 `set INOD+f1_inode`（L315-318） |

3. 思考：什么样的并发操作会让本次 rename 在 `commit` 时冲突？

**需要观察的现象 / 预期结果**：

- 任何**并发事务**在本次 rename 的「读版本之后、提交之前」改动了上述四个「读冲突哨兵 key」之一并先提交，本次 rename 提交就会失败（`not_committed` / 冲突），随后被 `OperationDriver` 整体重试。
- 典型触发场景：
  1. 另一个 `rename` 或 `create` 也在 `/b` 下创建/删除 `f2`（改了 `DENT+b_dir+f2` 或 `INOD+b_dir_inode`）；
  2. 另一个 `remove` 删了 `/a/f1` 或 `/a` 下其他项导致 `INOD+a_dir_inode` 变化；
  3. 另一个 `rename` 把别的文件也改名进 `/a` 或 `/b`。
- 因为整段操作在**一个事务**里，冲突后是「删源 + 删目的 + 建目的」**整体重试**，绝不会出现「源删了、目的没建」的中间态。

> 这是源码阅读型实践，无需运行集群即可完成。若要验证，可在测试里构造两个并发 rename 打到同一目的目录，观察 FDB 日志中的 `commit_unknown_result` / 冲突重试计数（见 4.3）。

#### 4.2.5 小练习与答案

**练习 1**：`PathResolveOp` 全程快照读，会不会导致 rename 读到「过期的目录结构」而做错决定？

**参考答案**：快照读读到的是事务开始时刻的一致快照，**事务内部一致**（同一 txn 里多次读同一个 key 结果相同）。rename 据此做完判断后，会在第 4 步把依赖的父目录/目录项补进读冲突范围。若提交前这些 key 被并发改动，提交冲突 → 整体重试，读到的新快照会反映最新结构。所以最终结果正确，代价是可能重试。

**练习 2**：为什么 `rename` 删源目录项、建目的目录项不用显式 `addReadConflict`，而是靠 `remove`/`store` 自带的写冲突范围？

**参考答案**：读冲突范围回答「我依赖谁不变」，写冲突范围回答「我要改谁」。删/建目录项是**本事务要改的 key**，由 `set`/`clear` 自动进**写**冲突范围即可；别的并发事务若也想改这些 key，会在**它**的提交时因写-写冲突而失败。而父目录 inode、目录项的存在性是「我**读了并据此判断**、但本身不写」的依赖，所以必须显式补**读**冲突范围。两者配合才能挡住所有并发竞态。

---

### 4.3 冲突重试：OperationDriver 与 FDBRetryStrategy（冲突重试模块）

#### 4.3.1 概念说明

事务冲突不可避免，但 3FS 把它对上层**完全透明**：op 只管写「业务逻辑」，冲突后的退避、重置、重试由 `OperationDriver` + `FDBRetryStrategy` 包办。整个 meta 服务的并发正确性，正是建立在「冲突 → 自动重试」之上——这就是上一节敢用快照读 + 补冲突的底气。

重试要解决三个问题：

1. **怎么发现冲突**：FDB 在 `commit` 时返回冲突/超时错误（`TransactionCode` 1xxx 段，如 `kConflict=1001`、`kTooOld=1003`、`kMaybeCommitted=1006`）。
2. **怎么退避**：直接猛重试会雪崩，需要指数退避 + 抖动。
3. **`commit_unknown_result`（maybe_committed）怎么办**：FDB 有时无法判断事务到底提交了没有。盲目重试非幂等操作会重复执行（如重复建文件）。3FS 用**幂等标记 + uuid 去重**解决。

> 关键术语：
> - **`OperationDriver`**：每个 op 的重试主循环，叠加 `maxRetryCount`（默认 10）与 `deadline_`（`operation_timeout`）双保险。
> - **`FDBRetryStrategy::onError`**：退避策略。对真 FDB 事务，优先交给 `fdb_transaction_on_error`（它既退避又 reset 事务、还告诉你是否该重试）；对内存 KV（测试用）走自带退避。
> - **`needIdempotent` / `Idempotent`**：op 自陈是否幂等；幂等操作会把 `(clientId, requestId) → 结果` 存进 FDB，重试时先查再决定是否执行。
> - **`commit_unknown_result`**：提交了但不确定成功。对幂等操作可安全重试；对非幂等操作，3FS 默认不重试（避免重复副作用），由 `retryMaybeCommitted` 控制。

#### 4.3.2 核心流程

`OperationDriver::run` 的重试循环：

```
while (true):
  if deadline 到了: break（返回上次的错误）
  result = runAndCommit(txn, op)            // 跑 op.run，成功且非只读则 commit
  if result 成功: break
  op.retry(err)                             // 清掉本次产生的 event/trace（重试要重新生成）
  retry = strategy.onError(txn, err)        // 退避 + reset，返回是否继续
  if retry 出错: result = retry 的错; break
```

退避公式（`defaultBackoff` 分支）：

\[
t_0 = 10\,\text{ms},\quad t_n = \min(t_{\max},\ 2 \cdot t_{n-1}),\quad \text{sleep} = t_n \cdot \frac{U(80,120)}{100}
\]

其中 \( t_{\max} \) 默认 \( 1\,\text{s} \)，\( U(80,120) \) 是 80~120 的均匀随机数（抖动，避免多客户端同步重试）。对真 FDB，退避细节交给 `fdb_transaction_on_error`，但上限由 `FDB_TR_OPTION_MAX_RETRY_DELAY`（设为 `maxBackoff`）约束。

#### 4.3.3 源码精读

**`OperationDriver::run`——重试主循环。**

[Operation.h:178-198](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/Operation.h#L178-L198)：

```cpp
while (true) {
  if (deadline_ && deadline_.value() <= SteadyClock::now()) {   // 超时兜底
    break;
  }
  result = co_await runAndCommit(*txn, operation_, duplicate);
  if (ErrorHandling::success(result)) { break; }                 // 成功退出
  operation_.retry(result.error());                              // 清理 events（见 Operation.h:55）
  auto retry = co_await strategy.onError(txn.get(), result.error());
  if (retry.hasError()) { result = makeError(retry.error()); break; }
  recorder.retry()++;
}
```

`operation_.retry(err)` 调用的是 [Operation.h:55](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/Operation.h#L55) 的 `clearEvents()`——因为上次试跑产生的事件/trace 在重试后会重复，必须清掉，成功提交后再由 `finish()` 统一落盘（[Operation.h:57-69](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/Operation.h#L57-L69)）。

**`runAndCommit`——幂等包装与提交。**

[Operation.h:221-250](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/Operation.h#L221-L250)：若 op 声明幂等（`needIdempotent` 返回 true），执行前先 `IDEMPOTENT_CHECK`（用 `clientId+requestId` 查已存结果），执行后把结果也存进 FDB 再提交。这样 `commit_unknown_result` 后的重试能直接查到上次结果而**不重复执行业务逻辑**：

```cpp
auto idem = !readonly && operation_.needIdempotent(clientId, requestId);
if (idem) {
  IDEMPOTENT_CHECK();                                  // 查是否已执行
  auto result = co_await handler(txn);
  if (result) {
    CO_RETURN_ON_ERROR(co_await Idempotent::store(txn, clientId, requestId, result));  // 存结果
    CO_RETURN_ON_ERROR(co_await txn.commit());
  }
  ...
} else {
  auto result = co_await handler(txn);
  if (!result.hasError() && !readonly) { CO_RETURN_ON_ERROR(co_await txn.commit()); }
}
```

哪些 op 声明幂等？`rename` 在 `moveToTrash` 或配置 `idempotent_rename` 开启时（[Rename.cc:63-69](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/Rename.cc#L63-L69)），`remove` 在递归删除或配置开启时（[Remove.cc:52-62](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/Remove.cc#L52-L62)）。这两个最怕重复执行的操作都有兜底。

**`FDBRetryStrategy::onError`——退避 + 是否继续。**

[FDBRetryStrategy.h:60-83](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDBRetryStrategy.h#L60-L83)：先看是否超过 `maxRetryCount`（默认 10）或是否事务类错误，再分真 FDB / 内存 KV 两条退避路径：

```cpp
if (retry_ >= config_.maxRetryCount) { co_return makeError(...); }          // 默认重试 10 次
if (!TransactionHelper::isTransactionError(error)) { co_return makeError(...); }  // 非事务错（如 kNotFound）不重试
...
auto *fdbTransaction = dynamic_cast<FDBTransaction *>(txn);
if (fdbTransaction) { co_return co_await fdbBackoff(fdbTransaction, ...); }
else { co_return co_await defaultBackoff(txn, ...); }
```

**注意**：像 `kNotFound`、`kNoPermission` 这类**业务错误不是事务错误**，`onError` 直接返回不重试——否则会无限重试一个注定失败的请求。只有冲突、`too_old`、超时这类「重试有可能成功」的才重试。

**`fdbBackoff`——把退避交给 FDB，并按 `retryMaybeCommitted` 区分。**

[FDBRetryStrategy.h:85-109](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDBRetryStrategy.h#L85-L109)：

```cpp
FDBErrorPredicate predict =
    config_.retryMaybeCommitted ? FDB_ERROR_PREDICATE_RETRYABLE : FDB_ERROR_PREDICATE_RETRYABLE_NOT_COMMITTED;
if (!fdb_error_predicate(predict, errcode)) { co_return makeError(...); }   // 不可重试则放弃
auto ok = co_await txn->onError(errcode);    // FDB 官方推荐：既退避又 reset，并返回是否可重试
```

`retryMaybeCommitted` 由 `operation_.retryMaybeCommitted()` 设置（[Operation.h:160](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/Operation.h#L160)）。对**非幂等**操作，遇到 `maybe_committed` 不重试（宁可让上层收到错误，也不冒重复执行的风险）；对**幂等**操作则可安全重试。

**`defaultBackoff`——测试用内存 KV 的自带退避。**

[FDBRetryStrategy.h:111-126](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDBRetryStrategy.h#L111-L126)：`txn.reset()` 清掉旧读写集，按上面公式退避：

```cpp
if (!TransactionHelper::isRetryable(error, config_.retryMaybeCommitted)) { co_return makeError(...); }
txn->reset();
auto duration = Duration(backoff_ / 100 * folly::Random::rand32(80, 120));   // 抖动 80%~120%
co_await folly::coro::sleep(duration.asUs());
```

#### 4.3.4 代码实践

**实践目标**：把「冲突 → 重试」的完整链路在源码里走一遍，并预测重试次数上限。

**操作步骤**：

1. 从 [MetaOperator.cc:83-86](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/service/MetaOperator.cc#L83-L86) 出发，跟踪 `OperationDriver(...).run(...)`。
2. 在 [Operation.h:178-198](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/Operation.h#L178-L198) 标注三处退出点：① `deadline_` 超时；② `runAndCommit` 成功；③ `strategy.onError` 返回错误。
3. 在 [FDBRetryStrategy.h:27-31](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDBRetryStrategy.h#L27-L31) 读默认 `Config`：`maxBackoff=1_s`、`maxRetryCount=10`、`retryMaybeCommitted=true`。

**需要观察的现象 / 预期结果**：

- 一个因冲突而失败的事务最多重试 **10 次**（`maxRetryCount`），或到 `operation_timeout` 为止，取先到者。
- 业务错误（`kNotFound` / `kNoPermission`）**立即返回，不重试**；只有事务类错误才重试。
- 重试前 `op.retry()` 会清空上次的事件/trace，保证最终日志不重复。

> 待本地验证：`src/meta` 下应有针对 `OperationDriver` 的单元测试（如 fault injection 路径，见 [MetaOperator.cc:73-75](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/service/MetaOperator.cc#L73-L75) 的 `FaultInjection::clone()`），可在 debug 构建里人为注入冲突观察重试计数。

#### 4.3.5 小练习与答案

**练习 1**：假如两个客户端并发 `rename`，把不同文件都改名进同一目的目录 `/d`，最终两个都会成功吗？

**参考答案**：两个 rename 都会对 `INOD+d_inode`（`/d` 的 inode）和各自的目的目录项补读冲突范围。FDB 串行化提交：先提交的成功，后提交的因其读冲突哨兵（`/d` 的 inode）已被前者改而**冲突失败**，随后整体重试。重试时读到新快照，重新补冲突、重新提交，最终两个文件都会出现在 `/d` 下（只是目的名字不同时；若目的名字相同则后者的前置校验会发现已存在并按 POSIX 覆盖/报错）。结论：最终一致、无数据损坏，代价是其中一次重试。

**练习 2**：为什么 `remove` 在递归删除时要声明幂等（`needIdempotent` 返回 true）？

**参考答案**：递归删除涉及清理大量子项，是最怕「重复执行」的操作之一。若提交返回 `commit_unknown_result`，不知删没删成：不重试则可能残留；重试则可能对已删项再删一遍（虽然 `kNotFound` 通常可接受，但中间状态难料）。声明幂等后，`(clientId, requestId)` 的结果会先存 FDB，重试时 `IDEMPOTENT_CHECK` 命中就直接返回上次结果，避免重复执行整个递归删除。

---

## 5. 综合实践

把三个模块串起来，完成一次「带并发预测的 rename 全链路分析」。

**场景**：客户端 A 执行 `mv /a/f1 /b/f2`（`/b/f2` 已存在且为普通文件），与此同时客户端 B 执行 `touch /b/f3`（即 `open(O_CREAT)` 在 `/b` 下新建 `f3`）。

**任务**：

1. **读写事务判定**：分别判断两个操作是只读还是读写事务，依据 [Open.cc:39-42](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/Open.cc#L39-L42) 的 `isReadOnly()` 规则。（预期：A 的 rename 是读写；B 的 `O_CREAT` 因带 `O_CREAT` flag → `isReadOnly()` 为 false → 读写。）
2. **原子操作分析**：列出 A 的 rename 读写的 key 与四个读冲突哨兵（参考 4.2.4 的表）。列出 B 的 create 读写的 key（新建 `DENT+b_dir+f3`、`INOD+f3_inode`），以及它在 [Open.cc:220-228](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/Open.cc#L220-L228) `createInodeAndEntry` 里补的读冲突范围（父 inode `INOD+b_dir_inode` + 新目录项 `DENT+b_dir+f3`）。
3. **冲突预测**：A 和 B 都对 `INOD+b_dir_inode`（`/b` 的 inode）声明了读冲突。问：谁先提交谁后提交？后者会发生什么？
4. **重试推演**：假设 A 先提交成功，B 提交时冲突。请按 [Operation.h:178-198](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/Operation.h#L178-L198) 推演 B 的重试：`op.retry()` 清事件 → `strategy.onError` 退避（真 FDB 走 `fdb_transaction_on_error`，含 reset）→ 重新 `runAndCommit` → 此时读到 `/b` 已被 A 改后的快照，重新补冲突、重新提交成功。
5. **幂等校验**：若 B 的 create 在某次重试中返回 `commit_unknown_result`，`O_CREAT` 是否声明了幂等？查 [Open.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/Open.cc) 是否重写了 `needIdempotent`（预期：未重写，默认非幂等；因此 `retryMaybeCommitted` 决定是否重试 `maybe_committed`，对应 [FDBRetryStrategy.h:96-101](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fdb/FDBRetryStrategy.h#L96-L101)）。

**交付物**：一张时序图 + 一份「key 读写与冲突范围对照表」+ 一段对「谁冲突、谁重试、最终是否一致」的结论。

> 这是一个纯源码阅读 + 推理型综合实践，无需运行集群。若想验证，可在 3FS 的 meta 单元测试里用 `FaultInjection`（[MetaOperator.cc:73-75](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/service/MetaOperator.cc#L73-L75)）注入冲突，观察重试计数与最终结果。

## 6. 本讲小结

- meta 的每个元数据 RPC = 一个 `Operation`，在**一个 FDB 事务**里完成；只读 op（`ReadOnlyOperation`，如 `stat`）永不提交，读写 op（如 `open`/`remove`/`rename`）由 `OperationDriver` 统一 `commit`。
- **快照读（`snapshotGet`/`snapshotLoad`）不进冲突范围、代价低；冲突读（`get`/`load`）进读冲突范围**。`PathResolveOp` 全程快照读换性能，写操作再手动 `addIntoReadConflict` 补「哨兵」。
- **原子性 = 一个事务里多 key 的全有或全无**。`rename` 是样板：删源 + 覆盖目的 + 建目的 + 改目录 inode，全部在一个事务，冲突则整体重试，绝不留中间态。
- **读冲突范围挡「我依赖谁不变」，写冲突范围（`set`/`clear` 自动）挡「我要改谁」**，两者配合挡住所有并发竞态。
- **冲突重试对上层透明**：`OperationDriver` 重试循环（`maxRetryCount=10` + `operation_timeout`）+ `FDBRetryStrategy` 退避；业务错误（`kNotFound` 等）不重试，只有事务类错误重试。
- **`commit_unknown_result` 靠幂等保护**：`rename`/`remove` 等危险操作用 `(clientId,requestId)` 存结果 + `IDEMPOTENT_CHECK` 去重；非幂等操作按 `retryMaybeCommitted` 决定是否重试 `maybe_committed`。

## 7. 下一步学习建议

- **u4-l4（文件数据布局与链分配）**：本讲的 `open`/`create` 成功后会返回 inode 的 `layout`（chainTableId/stripeSize/shuffle seed），下一讲讲这个 layout 是怎么由 `ChainAllocator` 轮询选链生成的。
- **u4-l5（动态文件长度、FileSession 与 GC）**：本讲多次提到 `createSession`（[Open.cc:235-270](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/ops/Open.cc#L235-L270)）与 `gcManager().removeEntry` 的延迟删除，下一讲系统讲解 FileSession 生命周期与 GC。
- **延伸阅读**：对照 `src/common/kv/WithTransaction.h` 的通用重试模板与本讲的 `OperationDriver`，体会 meta 为什么自己写驱动（要叠加 events/trace 清理、幂等、batch 等额外语义）。
