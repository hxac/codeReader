# ref 事务与 ref-cache

> 本讲承接 u5-l1（`ref_store` 虚表抽象与公共 API）与 u5-l2（files / packed / reftable 三种后端的磁盘格式）。前面两讲回答的是「引用长什么样、存在哪」；本讲回答两个更上层的问题：**「多个引用如何被一起、原子地修改」**，以及 **「读引用时，files 后端用什么内存结构缓存它们」**。

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清一次 `git update-ref` 在源码里经历的「事务生命周期」，理解 `OPEN → PREPARED → CLOSED` 三态机。
2. 解释 files 后端用 `.lock` 文件实现排他锁、用 `commit_ref` 的原子改名实现单引用原子写的原理。
3. 描述 `ref-cache` 这棵内存目录树如何懒加载、如何用二分查找定位引用、如何与 `merge/overlay` 迭代器组合。
4. 看懂 `debug.c` 的 trace 包装器如何在不改业务代码的前提下给事务加可观测性。
5. 自己用伪代码画出一次事务「提交成功」与「中途失败回滚」的流程。

## 2. 前置知识

在进入源码前，先建立三点直觉。

### 2.1 为什么引用更新需要「事务」

一次 `git push` 可能同时改 `refs/heads/main`、`refs/remotes/...`、`refs/tags/...` 等多个引用。如果逐个改、改到一半崩溃，仓库就会出现「部分引用已更新、部分还是旧值」的撕裂状态，这对一个版本控制系统是致命的。所以 git 把「多个引用的修改」打包成一个**事务（transaction）**，要求它要么整体生效、要么整体不生效——这就是「原子性」。

数据库领域有经典的两阶段提交（2PC）：**第一阶段「准备」抢锁、校验、把新值写好但先不公开；第二阶段「提交」把准备好的值原子地公开**。git 的 ref 事务正是这个模型。

### 2.2 「乐观并发」与 old_oid 校验

git 允许你在更新时声明「我以为这个引用现在的值是 `old_oid`」。事务在持有锁后会校验引用的真实旧值是否等于你给的 `old_oid`，不等就拒绝。这就是**比较并交换（Compare-And-Swap，CAS）**：用一次读到的旧值做凭证，保证「读—算—写」之间没有被别人插队。它是 push、rebase 等场景实现安全并发的基石。

### 2.3 为什么读引用需要「缓存」

files 后端把每个松散引用存成 `.git/refs/` 下的一个小文件。一次 `git for-each-ref` 要遍历成百上千个引用，如果每读一个就 `open/read/close` 一个文件，系统调用开销极大。于是 files 后端在内存里维护了一棵**镜像目录树**（ref-cache），把磁盘上的引用一次性读进来缓存住，后续查找走内存。

> 注意：ref-cache 只是 **files 后端**读松散引用时的内存缓存。reftable 后端有自己的快照机制（见 u5-l2），不使用这棵树。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [refs/refs-internal.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/refs-internal.h) | 事务内部数据结构：`struct ref_update`、`struct ref_transaction`、三态枚举、后端虚表 `struct ref_storage_be` 中事务函数指针的定义。 |
| [refs.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c) | 事务的**后端无关公共实现**：`ref_store_transaction_begin` / `prepare` / `commit` / `abort` / `free`，含状态机校验与事务钩子。 |
| [refs/files-backend.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/files-backend.c) | files 后端的**事务落地与文件锁**：`lock_ref_oid_basic` 抢锁、`files_transaction_prepare/finish/abort`、`files_transaction_cleanup` 释放锁。 |
| [refs/ref-cache.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/ref-cache.c) 与 [refs/ref-cache.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/ref-cache.h) | files 后端的**内存引用缓存**：目录树、懒加载、二分查找、缓存迭代器。 |
| [refs/iterator.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/iterator.c) | 引用迭代器的**组合原语**：`merge_ref_iterator`、`overlay_ref_iterator`、`prefix_ref_iterator`，把多个后端的迭代器拼成一个有序流。 |
| [refs/debug.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/debug.c) | `GIT_TRACE_REFS` 的**装饰器包装**：用一个 `debug_ref_store` 把真实后端包起来，给每个事务调用打 trace。 |
| [builtin/update-ref.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/update-ref.c) | `git update-ref` 子命令，事务 API 的典型调用方（含 `--stdin` 批量模式）。 |

---

## 4. 核心概念与源码讲解

### 4.1 ref 事务机制：begin / prepare / commit / abort

#### 4.1.1 概念说明

`struct ref_transaction` 是「一批引用修改」的容器。你可以往里塞任意多个 `ref_update`（每个描述「把某个引用从 old 改成 new」），然后一次性提交。它有三条不变量：

- **批量**：一次提交可含多个引用更新，对调用方而言像一次操作。
- **原子**：`prepare` 阶段若任何一个引用拿不到锁或 old 校验失败，整个事务回滚，不留半成品。
- **可观测**：在 `preparing` / `prepared` / `committed` / `aborted` 四个时机可触发钩子（见 4.1.3）。

事务用一个三态枚举约束合法调用顺序，定义在内部头文件里：

[refs/refs-internal.h:190-213](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/refs-internal.h#L190-L213) —— 注释完整说明了三个状态各自允许的后续动作，是理解整个生命周期的钥匙。

| 状态 | 含义 | 允许的下一步 |
| --- | --- | --- |
| `OPEN` | 刚创建，仍可追加 `ref_update` | `prepare` / `commit`（会自动 prepare）/ `abort` / `free` |
| `PREPARED` | 已 `prepare`：所有引用已上锁、old 已校验、新值已写入锁文件 | `commit` / `abort` |
| `CLOSED` | 已终结（提交完成或已回滚） | 只能 `free` |

#### 4.1.2 核心流程

事务的完整生命周期是一条单向状态流：

```text
                 begin()                 add_update()*           prepare()
   ┌──────────┐ ─────────► ┌──────────┐ ──────────► ┌──────────┐ ─────────► ┌──────────┐
   │  (无)    │            │   OPEN   │              │   OPEN   │            │ PREPARED  │
   └──────────┘            └──────────┘              └──────────┘            └──────────┘
                                 │                                                │
                                 │ commit()                                       │ commit()
                                 │ (内部自动 prepare)                              │
                                 ▼                                                ▼
                            ┌──────────┐                                     ┌──────────┐
                            │   ...    │                                     │  CLOSED  │
                            └──────────┘                                     └──────────┘
```

任一时刻调用前都会先 `switch (state)` 校验合法性，非法调用直接 `BUG()`（即内部编程错误，直接 abort 进程）。

**prepare 阶段做四件事**（这是原子性的关键）：

1. 排序 `refnames`，拒绝同一事务里对同一引用的重复更新。
2. 跑 `preparing` 事务钩子（用户可在此拒绝整个事务）。
3. 调用后端 `be->transaction_prepare`：**抢全部引用的锁、校验 old 值、把新值写进锁文件**。任一失败即返回错误码。
4. 跑 `prepared` 钩子；钩子若拒绝则自动 `abort`。

**commit 阶段**调用后端 `be->transaction_finish`：写 reflog、`commit_ref`（把锁文件原子改名为正式引用）、删除该删的引用。

**abort 阶段**调用后端 `be->transaction_abort`：释放全部锁、撤销尚未公开的修改。

> 关于「失败」的粒度：`prepare` 失败是干净的（一个锁都没公开成，直接释放）。而 `finish` 失败可能发生在部分引用已 `commit_ref` 之后——这部分 git 主要靠「先全部准备、最后统一提交」的两段式把窗口压到最小，单引用的原子性由 4.2 的 `.lock` 改名保证。

#### 4.1.3 源码精读

**事务与单条更新的数据结构。** 一个事务持有一个 `ref_update` 指针数组；每条 update 用 flag 位表达「有新值要写」「要校验旧值」等意图：

[refs/refs-internal.h:88-159](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/refs-internal.h#L88-L159) —— `struct ref_update`。注意 `new_oid`/`old_oid` 仅在 `REF_HAVE_NEW`/`REF_HAVE_OLD` 置位时才有意义；置 `old_oid` 为 null_oid 表示「要求该引用原本不存在」（用于创建新引用时的冲突检测）。

[refs/refs-internal.h:24-47](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/refs-internal.h#L24-L47) —— flag 位定义，`REF_HAVE_NEW (1<<2)` 与 `REF_HAVE_OLD (1<<3)` 是本讲最关键的两个。

[refs/refs-internal.h:232-243](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/refs-internal.h#L232-L243) —— `struct ref_transaction`：`updates[]` 数组、`state` 状态、`backend_data`（后端私有数据，files 后端用它存锁映射）、`rejections`（批量模式下被「软拒绝」的更新索引）。

**后端虚表里的事务三件套。** u5-l1 讲过 `ref_store` 靠 `be` 虚表分派；事务的 prepare/finish/abort 就是虚表里的三个槽：

[refs/refs-internal.h:435-445](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/refs-internal.h#L435-L445) —— 三个 `typedef`：`ref_transaction_prepare_fn` / `ref_transaction_finish_fn` / `ref_transaction_abort_fn`。

[refs/refs-internal.h:574-576](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/refs-internal.h#L574-L576) —— 它们被挂在 `struct ref_storage_be` 里，files / reftable 各自填一套实现。

**公共 API：begin。** 只做分配与初始化，状态为 `OPEN`，本身不碰磁盘：

[refs.c:1221-1237](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L1221-L1237) —— `ref_store_transaction_begin`。若传了 `REF_TRANSACTION_ALLOW_FAILURE`，额外分配 `rejections` 结构以支持「部分失败」的批量更新。

**公共 API：prepare。** 状态机校验 + 钩子 + 委托后端：

[refs.c:2682-2731](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L2682-L2731) —— `ref_transaction_prepare`。重点看三处：① 开头 `switch (state)` 只允许从 `OPEN` 进入，其余 `BUG()`；② `run_transaction_hook(transaction, "preparing")` 与 `"prepared"`，钩子返回非 0 则立即 `abort` 并 `die`；③ 真正的「抢锁 + 校验」在 `refs->be->transaction_prepare(refs, transaction, err)`（具体实现在 4.2）。

**公共 API：commit。** 若还停在 `OPEN`，先自动 `prepare`，再委托后端 `finish`：

[refs.c:2760-2788](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L2760-L2788) —— `ref_transaction_commit`。注意 `finish` 成功且非「初始事务」时才触发 `committed` 钩子（初始事务用于 `git init` 建仓，此时钩子基础设施还没就绪）。

**公共 API：abort 与 free。** abort 区分状态：`OPEN` 直接 free、`PREPARED` 调后端释放锁：

[refs.c:2733-2758](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L2733-L2758) —— `ref_transaction_abort`，末尾统一 `ref_transaction_free` 并跑 `aborted` 钩子。

[refs.c:1239-1275](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c#L1239-L1275) —— `ref_transaction_free`。注意它对 `PREPARED` 状态直接 `BUG("free called on a prepared reference transaction")`——**已准备的事务必须显式 commit 或 abort，绝不能直接 free**，否则会留下泄漏的 `.lock` 文件。

**观察事务：debug.c 装饰器。** 这是个非常优雅的设计。当设置了 `GIT_TRACE_REFS`，git 不去每个后端里加 trace，而是用一个 `debug_ref_store` 把真实后端**包一层**：

[refs/debug.c:15-33](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/debug.c#L15-L33) —— `maybe_debug_wrap_ref_store`：若 trace 未开启就原样返回真实 store；否则构造一个 `debug_ref_store`，其 `be` 指向一份 `refs_be_debug` 虚表副本（名字借真实后端）。

[refs/debug.c:58-70](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/debug.c#L58-L70) —— `debug_transaction_prepare`：先把 `transaction->ref_store` 改回真实后端（否则后续 finish 会再次绕回 debug 形成环），调真实实现的 `transaction_prepare`，然后 `trace_printf` 打印返回值与错误信息。这是**装饰器模式（Decorator Pattern）**的教科书级实现，让可观测性与业务逻辑彻底解耦。

[refs/debug.c:436-472](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/debug.c#L436-L472) —— `refs_be_debug` 虚表，注释明确要求「files 后端有的每个函数，这里都要有一个 wrapper」。

#### 4.1.4 代码实践

**实践目标：** 用 `git update-ref --stdin` 观察一个真实的批量事务，并用 `GIT_TRACE_REFS` 看 debug.c 打出的调用轨迹。

**操作步骤：**

1. 在任意仓库里执行下面这条「批量原子更新」（NUL 分隔的 stdin 协议，`start` 开启显式事务、`prepare` 进入准备态、`commit` 提交）：

   ```bash
   printf 'start\nupdate refs/heads/tmp1 HEAD\nupdate refs/heads/tmp2 HEAD\nprepare\ncommit\n' \
     | tr '\n' '\0' | git update-ref --stdin -z
   ```

2. 开启引用 trace 再跑一次，观察事务的 prepare / finish 调用：

   ```bash
   GIT_TRACE_REFS=1 git update-ref refs/heads/tmp3 HEAD 2>&1 | head -40
   ```

3. 阅读调用方源码 [builtin/update-ref.c:706-808](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/builtin/update-ref.c#L706-L808)，对照 `--stdin` 的状态机：`UPDATE_REFS_OPEN/STARTED/PREPARED/CLOSED` 四态与 `ref_transaction_commit`/`abort` 的对应关系。

**需要观察的现象：**

- 步骤 1 两条 `update` 在同一次提交里原子生效；可用 `git rev-parse refs/heads/tmp1 refs/heads/tmp2` 验证二者都已更新。
- 步骤 2 的 trace 输出里应能看到形如 `transaction_prepare: 0 ""`、逐条 `0: refs/heads/tmp3 <old> -> <new>`、`finish: 0` 的行——这正是 `debug_transaction_prepare` / `print_transaction` 打出来的。

**预期结果：** 三个临时引用被创建；trace 里清晰可见「prepare → finish」两阶段。

> 若你的 git 未用 `GIT_TRACE_REFS` 编译支持或输出为空，属正常，可只做步骤 1 的行为观察——其余结论标注为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1：** 为什么 `ref_transaction_free` 遇到 `PREPARED` 状态要 `BUG()`，而不是默默释放？

**参考答案：** `PREPARED` 状态意味着后端已经抢到了若干 `.lock` 文件并把新值写进锁文件。如果直接 free 而不走 abort，这些锁文件不会被回滚删除，会永久残留在磁盘上阻塞后续更新。这是**资源泄漏**级别的编程错误，git 选择用 `BUG()` 让进程立刻崩溃暴露问题，而不是悄悄吞掉。

**练习 2：** `ref_transaction_commit` 在 `OPEN` 状态被调用时会做什么？

**参考答案：** 它会先自动调用 `ref_transaction_prepare`（抢锁、校验、写锁文件），prepare 成功后再调后端 `transaction_finish` 完成公开。也就是说 `commit` 是「prepare + finish」的语法糖，方便只想要一次性提交的简单调用方。

---

### 4.2 ref-lock：用 `.lock` 文件实现排他锁

#### 4.2.1 概念说明

事务的「原子性」最终要落到**单个引用**如何被安全地改写。files 后端的办法朴素而有效：**给每个要改的引用配一个 `.lock` 兄弟文件作为排他标志**。例如要改 `refs/heads/main`，就先创建 `refs/heads/main.lock`：

- `.lock` 存在期间，任何其它进程想改同一个引用都会因创建 `.lock` 失败而知难而退——这是「互斥」。
- 新内容先写进 `.lock`，写好后把 `.lock` **原子改名**为正式引用名——这是「原子公开」。

在 POSIX 文件系统上，`rename(2)` 对同一文件系统内的条目是原子的：要么看到旧文件、要么看到新文件，不会有中间态。git 正是利用这一点把「单引用更新」做成原子的。多引用的原子性则靠 4.1 的「全部 prepare 抢锁成功才进入 finish」来逼近。

#### 4.2.2 核心流程

files 后端的事务实现把锁的生命周期与事务阶段精确对应：

```text
 prepare() 阶段                       finish() 阶段                abort()/cleanup()
 ┌─────────────────────┐              ┌──────────────────┐         ┌─────────────────┐
 │ 遍历每个 update:    │              │ 遍历每个 update: │         │ 遍历每个 update:│
 │  lock_ref_oid_basic │   全部锁     │  写 reflog       │  逐个   │  unlock_ref     │
 │   创建 <ref>.lock   │─成功后─►     │  commit_ref      │─改名─►  │  rollback .lock │
 │  校验 old_oid       │              │   (<ref>.lock →  │         │  (unlink .lock) │
 │  写 new_oid 到 .lock│              │    <ref>)        │         │                 │
 └─────────────────────┘              └──────────────────┘         └─────────────────┘
        任一失败 ──► 立即 cleanup 释放已抢到的锁（全回滚）
```

关键设计：prepare 阶段**一次只持有一个打开的锁文件**（注释见 4.2.3），避免事务包含上千引用时耗尽文件描述符；新值先全部写好，finish 时才逐个原子改名公开。

#### 4.2.3 源码精读

**锁对象。** 一个 `ref_lock` 对应一个被锁住的引用：

[refs/files-backend.c:75-80](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/files-backend.c#L75-L80) —— `struct ref_lock`。内嵌一个 `struct lock_file lk`（u14-l2 会专讲 lockfile 机制）、`old_oid` 记录抢锁时读到的旧值、`count` 引用计数（因为同一个引用的更新与 reflog 更新可能共用一把锁）。

**抢锁。** `lock_ref_oid_basic` 是给单个引用上锁的核心：

[refs/files-backend.c:1261-1309](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/files-backend.c#L1261-L1309) —— 流程是：① 若引用不存在（`old_oid` 为 null），先调 `refs_verify_refname_available` 确认没有名字冲突；② 用 `raceproof_create_file(ref_file.buf, create_reflock, ...)` 创建 `<ref>.lock`，`raceproof_` 前缀表示它能容忍「文件被并发删掉」这类竞态并重试；③ 创建成功后再 `refs_resolve_ref_unsafe` 读出当前旧值存入 `lock->old_oid`，供后续 old 校验。失败走 `error_return` 调 `unlock_ref` 回滚。

**释锁（回滚）。** 引用计数到 0 才真正回滚：

[refs/files-backend.c:699-707](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/files-backend.c#L699-L707) —— `unlock_ref`：`count--`，归零则 `rollback_lock_file`（删除 `.lock`）并释放结构。注意它删的是 `.lock`，**正式引用文件丝毫未动**——这正是 abort 干净的原因。

**prepare 主循环：抢全部锁。** 这是「原子性」落地的地方：

[refs/files-backend.c:3010-3031](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/files-backend.c#L3010-L3031) —— 注释写明策略：**「Acquire all locks... Only keep one lockfile open at a time to avoid running out of file descriptors」**。循环对每个 update 调 `lock_ref_for_update`；任一失败：若是「软拒绝」（名字冲突等可跳过的），`ref_transaction_maybe_set_rejected` 标记后 `continue`；否则 `goto cleanup` 全量回滚。

**finish：逐个原子改名公开。** 

[refs/files-backend.c:3345-3385](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/files-backend.c#L3345-L3385) —— `files_transaction_finish` 的更新段：先 `parse_and_write_reflog` 写日志，再 `clear_loose_ref_cache(refs)`（4.3 讲，写后让缓存失效），最后 `commit_ref(lock)` 把 `.lock` 改名为正式引用。`commit_ref` 失败会 `unlock_ref` 并返回 `REF_TRANSACTION_ERROR_GENERIC`。

**cleanup：统一的锁回收。** 无论 abort 还是失败，都走这个函数：

[refs/files-backend.c:2909-2945](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/files-backend.c#L2909-L2945) —— `files_transaction_cleanup`：遍历每个 update，对其 `backend_data`（即 `ref_lock`）调 `unlock_ref` 释放锁、清理空父目录；再 abort 嵌套的 packed 事务、释放 packed 锁、清空 `ref_locks` strmap，最后把状态置 `CLOSED`。

[refs/files-backend.c:3475-3484](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/files-backend.c#L3475-L3484) —— `files_transaction_abort` 就是简单委托给 `files_transaction_cleanup`。

#### 4.2.4 代码实践

**实践目标：** 亲手制造一个 `.lock` 文件并观察它如何阻塞其它更新，再观察它如何被回滚清理。

**操作步骤：**

1. 制造并验证更新会创建 `.lock`（极短时间内）。先创建一个引用：

   ```bash
   git update-ref refs/heads/lock-demo HEAD
   ```

2. 手动预先占住锁，模拟另一个进程正在更新：

   ```bash
   touch .git/refs/heads/lock-demo.lock
   ```

3. 尝试更新同一个引用，应失败：

   ```bash
   git update-ref refs/heads/lock-demo HEAD   # 预期报 "unable to create lock file" 之类错误
   ```

4. 解除手动锁后再更新，应成功：

   ```bash
   rm .git/refs/heads/lock-demo.lock
   git update-ref refs/heads/lock-demo HEAD   # 预期成功
   ```

**需要观察的现象：** 步骤 3 因 `.lock` 已存在而被 `raceproof_create_file` 拒绝；步骤 4 解锁后成功。结合 [refs/files-backend.c:1261-1309](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/files-backend.c#L1261-L1309) 理解：`create_reflock` 创建 `.lock` 失败即走 `error_return → unlock_ref → 返回 NULL`，事务 prepare 收到 NULL 锁后整体回滚。

**预期结果：** `.lock` 的存在与否直接决定更新能否进行，验证了「`.lock` 即排他标志」。

> 若仓库默认用 reftable 后端（`extensions.refstorage` 为 reftable），`.git/refs/` 下不会有松散文件与 `.lock`，本实践将无现象——可改用 `git init --ref-format files demo` 建一个 files 后端仓库再试。

#### 4.2.5 小练习与答案

**练习 1：** 为什么 git 用「写 `.lock` 再 rename」而不是「直接覆写引用文件」？

**参考答案：** 直接覆写在写到一半时被中断（崩溃、断电），会留下半个内容的损坏引用文件，且无法判断原值是什么。而「写临时 `.lock` + 原子 rename」保证：要么完全没动旧文件、要么已经完整换成新文件；rename 失败或进程崩溃，残留的只是 `.lock` 临时文件，正式引用始终是完整一致的旧值。这是用「临时文件 + 原子改名」换取崩溃一致性的经典做法。

**练习 2：** `ref_lock.count` 这个引用计数解决什么问题？

**参考答案：** 同一个引用可能既要做值更新、又要写 reflog，这两步在实现里可能各自「持」一次锁。用 `count` 计数后，只有当所有使用者都 `unlock_ref`（`count` 归零）才真正 `rollback_lock_file`，避免 reflog 还没写完锁就被提前释放。

---

### 4.3 ref-cache：分层内存缓存与迭代器组合

#### 4.3.1 概念说明

`ref-cache` 是 files 后端读松散引用时的内存缓存。它把磁盘上的 `.git/refs/` 目录层次**镜像**成一棵内存目录树：每个目录是一个 `ref_dir`，每个引用或子目录是一个 `ref_entry`。这棵树有两个关键特性：

- **懒加载**：一开始只建根目录的空壳，哪个子目录被访问才去磁盘读它（`REF_INCOMPLETE` 标志位标记「还没读」）。
- **按字典序组织**：每个 `ref_dir` 内的条目按名字排序，查找走二分（`bsearch`），是 \( O(\log n) \)。

读引用有两种典型路径：精确查一个名字（`search_ref_dir` 二分）、遍历全部（`cache_ref_iterator` 深度优先）。当 files 后端要把「松散引用」和「packed-refs」合并输出时，再把两路迭代器用 `overlay` / `merge` 组合成一路有序流——这就是 [refs/iterator.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/iterator.c) 的职责。

#### 4.3.2 核心流程

**缓存的建立与查询**：

```text
   create_ref_cache()              get_ref_dir(dir)               search_ref_dir(dir, name)
   ┌──────────────┐                ┌────────────────────┐         ┌─────────────────────┐
   │ 建根 ref_dir │ ──访问某目录──► │ 若 REF_INCOMPLETE: │ ──查找─►│ sort_ref_dir (按需)  │
   │ 标 REF_IN-   │                │   fill_ref_dir()   │         │ bsearch 二分定位     │
   │ COMPLETE     │                │   读磁盘该目录     │         │ 返回下标 or -1       │
   └──────────────┘                │ 清 REF_INCOMPLETE  │         └─────────────────────┘
                                   └────────────────────┘
```

**写后失效**：files 后端每次成功写入引用都会 `clear_loose_ref_cache`，即把整个 `loose` 缓存指针置空，下次读时重建——简单粗暴但正确，因为引用写操作远少于读操作。

**迭代器组合**：松散迭代器与 packed 迭代器各自已按字典序产出引用；`overlay_ref_iterator_begin` 把两者做**并集**（重名取前者），`merge_ref_iterator_begin` 用一个 `select` 回调决定**如何交错**（例如 worktree 引用如何覆盖 common 引用）。

#### 4.3.3 源码精读

**缓存主体结构。** 

[refs/ref-cache.h:18-29](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/ref-cache.h#L18-L29) —— `struct ref_cache`：`root` 指向根 `ref_entry`，`fill_ref_dir` 是懒加载回调（读磁盘松散引用用），由后端在创建缓存时注入。

[refs/ref-cache.h:145-157](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/ref-cache.h#L145-L157) —— `struct ref_entry`：用 `flag` 区分是「引用」还是「目录」（`REF_DIR = 0x10`），并用一个 `union` 让同一结构体既能存 `ref_value`（oid + referent）又能存 `ref_dir`（子条目数组）。`name` 是全限定名（如 `refs/heads/master` 或带尾斜杠的目录名 `refs/heads/`），这让深度优先遍历天然按字典序产出。

[refs/ref-cache.h:74-89](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/ref-cache.h#L74-L89) —— `struct ref_dir`：`entries[]` 指针数组；`sorted` 记录「前多少条已排好序」，新增条目先无序追加、需要时才整体排序，避免每加一条就排一次。

**懒加载触发点。** 

[refs/ref-cache.c:21-34](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/ref-cache.c#L21-L34) —— `get_ref_dir`：只要访问一个目录条目且它带 `REF_INCOMPLETE`，就调 `cache->fill_ref_dir` 从磁盘把该目录读进来，然后清掉 `REF_INCOMPLETE`。这是「按需展开」的核心。

**创建缓存。** 

[refs/ref-cache.c:50-59](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/ref-cache.c#L50-L59) —— `create_ref_cache`：建一个根 `ref_entry`（名为空串 `""`），标 `REF_INCOMPLETE`，等待首次访问时触发整树懒加载。

**二分查找。** 

[refs/ref-cache.c:132-150](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/ref-cache.c#L132-L150) —— `search_ref_dir`：先 `sort_ref_dir`（仅当 `sorted < nr`），再用 `bsearch` 按名字定位。比较函数 `ref_entry_cmp_sslice` 用 `strncmp` 配合末尾字节比较，正确处理「名字前缀」匹配（目录名带尾斜杠这一约定在这里发挥作用）。定位复杂度为

\[
T_{\text{lookup}} = O(\log n)
\]

其中 \(n\) 为该目录下条目数。

**缓存迭代器（深度优先遍历）。** 

[refs/ref-cache.c:381-436](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/ref-cache.c#L381-L436) —— `cache_ref_iterator_advance`：用一个「层栈」`levels[]` 模拟深度优先；遇到目录就 `get_ref_dir` 后压栈下钻、遇到引用就产出并返回。由于每个目录在产出前都会 `sort_ref_dir`、且目录名带尾斜杠保证排序正确，整棵树的产出**天然全局有序**。

**预填（prime）。** 

[refs/ref-cache.c:284-319](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/ref-cache.c#L284-L319) —— `prime_ref_dir`：按 `prefix` 递归地把相关子目录提前读进来，用于「已知要遍历一大片」时一次性预热，避免迭代中途频繁触发懒加载。

**写后失效。** 

[refs/files-backend.c:104](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/files-backend.c#L104) —— `clear_loose_ref_cache`（函数定义起始行）：把 `files_ref_store.loose` 整个释放置空。它在 [refs/files-backend.c:3376](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/files-backend.c#L3376) 与 [refs/files-backend.c:3451](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/files-backend.c#L3451) 的 finish 路径里被调用，保证写入后读到的总是最新值。

**迭代器组合原语。** 缓存迭代器产出的是「某一层后端」的引用；要把多后端合并，需要组合迭代器：

[refs/iterator.c:81-95](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/iterator.c#L81-L95) —— `struct merge_ref_iterator`：持有两个子迭代器 `iter0/iter1` 和一个 `select` 回调；`advance` 时让回调决定「这次产出谁、要不要顺手跳过另一个的重复项」。

[refs/iterator.c:97-138](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/iterator.c#L97-L138) —— `ref_iterator_select`：合并 worktree 与 common 引用的策略——worktree 同名引用**遮蔽** common 引用（`ITER_SELECT_0_SKIP_1`），common 中的 per-worktree 引用要跳过（属于别的工作树），shared 引用正常产出。这套 `ITER_SELECT_*` 选择语义是迭代器组合的「胶水」。

[refs/iterator.c:294](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/iterator.c#L294) —— `overlay_ref_iterator_begin`：并集特例，用于把松散迭代器叠在 packed 迭代器之上（同名取松散，呼应 u5-l2 的「松散优先」读路径）。

#### 4.3.4 代码实践

**实践目标：** 观察 ref-cache 的「写后失效」与懒加载如何影响一次遍历，并对照源码确认缓存行为。

**操作步骤：**

1. 清空并预热缓存观察：执行一次遍历，再用 `strace` 看它读了哪些 `refs/` 文件：

   ```bash
   strace -f -e openat git for-each-ref 2>&1 | grep -c "refs/"
   ```

2. 紧接着**立刻**再跑一次，对比打开的 refs 文件数（缓存是否仍在内存取决于进程生命周期；重点是理解单进程内首次访问触发 `fill_ref_dir`）：

   ```bash
   strace -f -e openat git for-each-ref 2>&1 | grep -c "refs/"
   ```

3. 阅读懒加载函数 [refs/ref-cache.c:21-34](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/ref-cache.c#L21-L34) 与二分查找 [refs/ref-cache.c:132-150](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs/ref-cache.c#L132-L150)，回答：单次 `for-each-ref` 内，每个目录的松散文件大概被读几次？为什么「写后失效」不会让单次命令内反复重建缓存？

**需要观察的现象：** 一次 `for-each-ref` 进程内，每个目录只被读一次（懒加载 + `REF_INCOMPLETE` 清除后不再重读）。

**预期结果：** 能用 `REF_INCOMPLETE` 标志位的置位/清除解释「读一次即缓存」；能说明 `clear_loose_ref_cache` 只在**写**之后触发，读流程内部不会反复失效。

> strace 计数因系统、glibc 缓存、引用数量而异，若数字不可复现，重点放在「对照源码解释机制」，计数结论标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1：** 为什么 `ref_entry.name` 存**全限定名**（如 `refs/heads/master`）而不是相对父目录的短名？注释里给出了理由，请复述。

**参考答案：** 因为迭代回调函数历史上一直假定「传给它的名字字符串在整个迭代期间有效且是全名」。若只存短名，遍历时要在运行中拼出全名，既增加开销又会破坏「回调拿到的字符串不会被释放」这一既有约定。用空间（存更长字符串）换简洁与兼容。

**练习 2：** `overlay_ref_iterator_begin(loose, packed)` 与 `merge_ref_iterator_begin(loose, packed, select)` 的区别是什么？

**参考答案：** `overlay` 是「并集」：两个子迭代器都已各自有序，front（松散）与 back（packed）重名时取 front，实现简单，正好对应「松散优先、packed 兜底」。`merge` 更通用：由一个 `select` 回调动态决定如何交错两个流，能表达「遮蔽」「跳过某一方」等复杂策略（如 worktree 覆盖 common）。`overlay` 本质上是 `merge` 的一个特化。

---

## 5. 综合实践

把本讲三个模块串起来：**用伪代码描述一次 `git update-ref refs/heads/x <newoid>` 的完整事务流程，并分别给出「全部成功」与「old_oid 校验失败回滚」两条路径。**

要求：

1. 先调用 `ref_store_transaction_begin` 开事务（`OPEN`）。
2. `ref_transaction_add_update` 加入一条带 `REF_HAVE_NEW | REF_HAVE_OLD` 的 update。
3. 进入 `ref_transaction_prepare`：标出「调 `files_transaction_prepare` → `lock_ref_oid_basic` 创建 `refs/heads/x.lock` → 读旧值 → 比对 `old_oid`」这一串步骤。
4. 成功路径：`ref_transaction_commit` → `files_transaction_finish` → 写 reflog → `clear_loose_ref_cache` → `commit_ref` 把 `.lock` 改名为正式引用 → 状态 `CLOSED`。
5. 失败路径：在 prepare 的 old 校验处失败 → 返回错误 → 公共层 `abort`（或调用方主动 abort）→ `files_transaction_abort` → `files_transaction_cleanup` → `unlock_ref` 删除 `.lock`，正式引用保持旧值不变 → 状态 `CLOSED`。
6. 在每一步标注涉及的源码文件与函数（refs.c / files-backend.c / ref-cache.c），并用一句话说明「为什么这一步对原子性/一致性必不可少」。

完成后，可对照本讲 4.1.2、4.2.2 的流程图自检是否覆盖了状态机迁移、锁的获取与释放、缓存的失效这三个关键点。

> 这是一道「源码阅读型」综合实践，不要求运行命令，重点是能把三条调用链（公共事务 API、files 后端锁、ref-cache 失效）在一张图里对齐。

## 6. 本讲小结

- **事务三态机**：`OPEN → PREPARED → CLOSED`，由 [refs.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c) 的公共 API 用 `switch(state)` 严格守护；`PREPARED` 态必须显式 commit/abort，不能直接 free。
- **两阶段提交**：`prepare` 抢全部锁 + 校验 old + 写锁文件（原子性的关键，失败即全回滚）；`finish` 写 reflog + `commit_ref` 原子改名公开。
- **`.lock` 文件锁**：files 后端用 `<ref>.lock` 作排他标志，靠 `raceproof_create_file` 创建、`rename` 原子公开、`rollback` 删除回滚；`count` 引用计数支持「更新 + reflog」共用一把锁。
- **乐观并发**：`REF_HAVE_OLD` + `old_oid` 实现 CAS，push/rebase 等借此安全并发。
- **ref-cache**：files 后端读松散引用的内存目录树，`REF_INCOMPLETE` 驱动懒加载、`bsearch` 实现 \(O(\log n)\) 查找、写后 `clear_loose_ref_cache` 整树失效重建。
- **迭代器组合**：`cache_ref_iterator` 深度优先产出有序流，`overlay`/`merge` 迭代器把松散与 packed、worktree 与 common 合并；`debug.c` 用装饰器模式给事务加 trace 而不侵入业务。

## 7. 下一步学习建议

- **向下深入锁机制**：本讲的 `struct lock_file` 与 `rollback_lock_file` 来自 lockfile 子系统，建议接着学 **u14-l2（临时文件与文件锁）**，弄清 `.lock` 临时文件的注册表、原子改名与崩溃恢复细节。
- **横向对比后端**：reftable 后端不用 `.lock` + 松散文件，而是靠二进制块与栈式追加实现原子更新（见 u5-l2）。对比 files 与 reftable 的事务实现，能加深对「不同存储格式如何实现原子性」的理解。
- **跟进钩子与并发**：本讲提到的 `preparing/prepared/committed/aborted` 事务钩子（`run_transaction_hook`）可用于仓库策略（如拒绝 force-push），可阅读 [refs.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/refs.c) 中 `run_transaction_hook` 的实现与文档 `Documentation/githooks.txt` 的 `reference-transaction` 一节。
- **练习写测试**：参考 t 目录里 `t1400-update-ref.sh`、`t1404-reflog.sh` 等用例，看上游如何用 shell 端到端测试事务的原子性与回滚（呼应 u15 测试单元）。
