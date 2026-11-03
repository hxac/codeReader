# 文件预分配：从 truncate 到 fallocate

## 1. 本讲目标

学完本讲，你应该能够：

1. 按顺序说出文件预分配阶段的完整协作链：`CheckIntegrityEntry::proceedFileAllocation` → `FileAllocationMan`（SequentialPicker 停车场）→ `FileAllocationDispatcherCommand`（dispatcher）→ `FileAllocationCommand`（worker）→ `prepareForNextAction`（回到下载命令链）。
2. 对比 `--file-allocation` 三种真实分配手段——`falloc`、`trunc`、`prealloc`（内部名 adaptive，运行时再探测回退）——分别落到哪些系统调用、各自的代价与适用文件系统。
3. 解释为什么文件分配要被设计成「下载前的一个独立排队阶段」，而不是下载命令顺手做掉：全局串行化、进度可见、可被 Ctrl-C 打断、失败有独立错误出口。

本讲只讲「预分配」这一个阶段。校验阶段（CheckIntegrityEntry 的另一半）在 u5-l5 展开，BT 侧的 BtSetup 在单元 6 展开。

## 2. 前置知识

### 2.1 为什么要预分配磁盘空间

多连接下载的本质是「多个连接各自往文件的不同偏移写数据」。如果文件从 0 字节开始边下边长，文件系统就要在下载过程中不断临时分配新块，这些块在磁盘上往往不连续——文件碎片化，机械磁盘寻址变慢，写放大增加。

预分配（preallocation）的做法是：**在下载开始前，一次性把整个文件的空间圈出来**。好处有三个：

- 拿到尽量连续的磁盘块，减少碎片；
- 把「磁盘满了」这个错误提前到下载第 0 秒暴露，而不是下到 90% 才失败；
- 让后续写盘速度更平稳（不用边写边等文件系统分配）。

### 2.2 三种「圈地」手段与稀疏文件

操作系统提供了三类把文件「撑到指定大小」的办法，代价截然不同：

| 手段 | 典型系统调用 | 是否真正占块 | 速度 |
| --- | --- | --- | --- |
| 截断到目标长度 | `ftruncate(fd, len)` | 否（产生**稀疏文件**，中间是「洞」，读到 0） | 极快，纯元数据操作 |
| 预分配指定区间 | `fallocate(fd, 0, off, len)` / `posix_fallocate` | 是（块被真正分配并清零） | 新文件系统（ext4 带 extents、xfs、btrfs）上几乎瞬时；老文件系统可能退化 |
| 手工写零 | 循环 `write()` 零缓冲 | 是 | 最慢， \( N=\lceil L/B\rceil \) 次写（\( L \) 文件长度，\( B \) 每次写块大小） |

「稀疏文件」是理解本讲的第一个关键词：`ftruncate` 撑大的文件在 `ls -l` 里看起来是 2GB，`du -h` 里却只占几 KB——因为洞不占数据块。它解决了「占位」问题，但没解决「碎片」问题，这正是 `trunc` 与 `falloc` 的本质区别。

### 2.3 本讲在全局中的位置

本讲连接前面四讲的概念：

- **u3-l1** 介绍过 RequestGroupEntry 与 SequentialPicker「停车场」：任务可以被挂起排队。本讲的 `FileAllocationMan` 就是停车场的第二个使用者（第一个是校验队列 CheckIntegrityMan）。
- **u5-l1** 讲过 DiskWriter 的打开/截断/定位写。本讲用到的 `BinaryStream::allocate()/truncate()` 是同一层接口的两个扩展能力。
- **u5-l3** 讲过 DiskAdaptor 与 DiskWriterEntry 的 `needsFileAllocation/needsDiskWriter` 标志。本讲回答「这些标志最终被谁消费」。
- **u2-l1 / u2-l3** 的事件循环、Realtime 状态与「命令返回 false 必须自我重排」的约定，在本讲的 worker 命令里得到直接应用。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/FileAllocationEntry.cc/.h` | 预分配阶段的任务条目（挂号单），包装一个 FileAllocationIterator |
| `src/StreamFileAllocationEntry.cc` | HTTP/FTP 等流式协议的条目：分配完成后生成分段下载命令 |
| `src/BtFileAllocationEntry.cc` | BT 条目：分配完成后装配整套 BT 运行时（BtSetup） |
| `src/CheckIntegrityEntry.cc` | `proceedFileAllocation()`：决定任务进不进预分配队列 |
| `src/RequestGroup.cc` | `needsFileAllocation()` 闸门；`createInitialCommand` 把任务送进校验/分配流水线 |
| `src/FileAllocationMan.h` | 预分配队列，仅是 `SequentialPicker<FileAllocationEntry>` 的类型别名 |
| `src/SequentialDispatcherCommand.h` | dispatcher 命令的通用模板（校验队列同样复用） |
| `src/FileAllocationDispatcherCommand.cc` | 从队列取条目，为它铸造 FileAllocationCommand |
| `src/FileAllocationCommand.cc/.h` | worker 命令：循环推进分配直到完成 |
| `src/RealtimeCommand.cc` | worker 的基类：实时状态、numCommand 计数、异常出口 |
| `src/FileAllocationIterator.h` | 分配迭代器抽象接口（4 个纯虚函数） |
| `src/TruncFileAllocationIterator.cc` | `trunc`：一次 ftruncate |
| `src/FallocFileAllocationIterator.cc` | `falloc`：一次 fallocate |
| `src/AdaptiveFileAllocationIterator.cc` | `prealloc` 默认值：先 4KB 试探 fallocate，失败回退写零 |
| `src/SingleFileAllocationIterator.cc` | 写零回退实现（256KB 一块） |
| `src/MultiFileAllocationIterator.cc` | 多文件任务的分配迭代器（逐文件套用上述三种） |
| `src/AbstractSingleDiskAdaptor.cc` | 按 DiskAdaptor 上的分配方法选择迭代器 |
| `src/DefaultPieceStorage.cc` | 把 `--file-allocation` 选项翻译成 DiskAdaptor 的内部方法 |
| `src/AbstractDiskWriter.cc` | 系统调用层：allocate/truncate 的四平台分支 |
| `src/OptionHandlerFactory.cc`、`src/usage_text.h` | `--file-allocation` 选项的注册与帮助文本 |
| `src/ConsoleStatCalc.cc` | 控制台 `[FileAlloc:...]` 进度行 |
| `test/SingleFileAllocationIteratorTest.cc` 等 | 三份迭代器单元测试 |

## 4. 核心概念与源码讲解

### 4.1 FileAllocationEntry：预分配阶段的挂号单

#### 4.1.1 概念说明

一次下载在 aria2 内部被拆成三个**串行的排队阶段**：

```text
校验阶段（CheckIntegrityMan 停车场）
        │ onDownloadIncomplete()
        ▼
预分配阶段（FileAllocationMan 停车场）   ←── 本讲
        │ prepareForNextAction()
        ▼
下载命令链（CreateRequestCommand → …，见 u4-l1）
```

`FileAllocationEntry` 就是「预分配阶段」里代表一个下载任务的条目对象。它做三件事：

1. **持有分配迭代器**：构造时向 PieceStorage 的 DiskAdaptor 要一个 `FileAllocationIterator`，之后所有分配推进、进度查询都委托给它；
2. **携带协议专属的「下一步」**：分配完成后干什么，由子类 `StreamFileAllocationEntry`（流式协议）和 `BtFileAllocationEntry`（BT）分别实现；
3. **实现 ProgressAwareEntry 接口**：让控制台能显示当前分配到多少字节。

#### 4.1.2 核心流程

一个任务是怎么拿到这张挂号单的？

```text
RequestGroup::createInitialCommand()
  │ 创建 CheckIntegrityEntry（Stream 或 Bt 子类；即使不开 --check-integrity 也会创建，
  │ 只是不排队、立即走 onDownloadIncomplete）
  ▼
校验结束 → CheckIntegrityEntry::proceedFileAllocation()
  │ 询问 RequestGroup::needsFileAllocation()：
  │   ① --file-allocation != none
  │   ② totalLength >= --no-file-allocation-limit（默认 5M）
  │   ③ 分配迭代器未 finished（现有文件还没达到目标大小）
  ├─ 三条全满足 → new Stream/BtFileAllocationEntry → FileAllocationMan.pushEntry()（排队）
  └─ 否则      → 直接 entry->prepareForNextAction()，跳过预分配阶段
```

#### 4.1.3 源码精读

**入口闸门：进不进预分配队列，由这一段决定。**

[src/CheckIntegrityEntry.cc:84-94](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/CheckIntegrityEntry.cc#L84-L94)

```cpp
void CheckIntegrityEntry::proceedFileAllocation(
    std::vector<std::unique_ptr<Command>>& commands,
    std::unique_ptr<FileAllocationEntry> entry, DownloadEngine* e)
{
  if (getRequestGroup()->needsFileAllocation()) {
    e->getFileAllocationMan()->pushEntry(std::move(entry));  // 排队
  }
  else {
    entry->prepareForNextAction(commands, e);                // 跳过本阶段
  }
}
```

这段代码是校验阶段与预分配阶段的衔接点：`needsFileAllocation()` 为真就把 entry 压进 `FileAllocationMan` 队列，为假就直接调用 `prepareForNextAction` 进入下载（不产生任何分配命令）。

**闸门的三个条件在这里逐条求值。**

[src/RequestGroup.cc:1163-1169](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L1163-L1169)

```cpp
bool RequestGroup::needsFileAllocation() const
{
  return isFileAllocationEnabled() &&                       // ① 选项 != none
         option_->getAsLLInt(PREF_NO_FILE_ALLOCATION_LIMIT) <=
             getTotalLength() &&                            // ② 总长达标（默认阈值 5M）
         !pieceStorage_->getDiskAdaptor()->fileAllocationIterator()->finished();
}                                                           // ③ 尚未分配完成
```

注意两点：条件①来自 RequestGroup 构造时的一行初始化——[src/RequestGroup.cc:157](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L157) 的 `fileAllocationEnabled_ = option_->get(PREF_FILE_ALLOCATION) != V_NONE;`；条件③里 `fileAllocationIterator()` **每次调用都会新建一个临时迭代器又立即销毁**，对单文件任务它等价于「磁盘上现有文件大小是否已等于总长」的纯查询（`finished()` 的实现见 4.3.3），没有副作用。

**entry 的构造：一次性向 DiskAdaptor 要迭代器并终身持有。**

[src/FileAllocationEntry.cc:44-51](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FileAllocationEntry.cc#L44-L51)

```cpp
FileAllocationEntry::FileAllocationEntry(
    RequestGroup* requestGroup, std::unique_ptr<Command> nextCommand)
    : RequestGroupEntry{requestGroup, std::move(nextCommand)},
      fileAllocationIterator_{requestGroup->getPieceStorage()
                                  ->getDiskAdaptor()
                                  ->fileAllocationIterator()}  // 在此取迭代器
{
}
```

之后 `getCurrentLength()/getTotalLength()/finished()/allocateChunk()` 四个方法全部一行委托给这个迭代器（[src/FileAllocationEntry.cc:55-73](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FileAllocationEntry.cc#L55-L73)）。基类声明的两个身份也值得一看：[src/FileAllocationEntry.h:51-54](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FileAllocationEntry.h#L51-L54) 显示它同时继承 `RequestGroupEntry`（可进停车场）与 `ProgressAwareEntry`（可报告进度），而 [src/FileAllocationEntry.h:70-73](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FileAllocationEntry.h#L70-L73) 把 `prepareForNextAction` 留成纯虚函数交给协议子类。

**谁创建这两类子类 entry：**

- 流式协议（HTTP/FTP/SFTP/Metalink 直链）：校验发现文件未完成时——[src/StreamCheckIntegrityEntry.cc:53-65](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/StreamCheckIntegrityEntry.cc#L53-L65) 中 `onDownloadIncomplete` 构造 `StreamFileAllocationEntry` 并交给 `proceedFileAllocation`。注意它把 `popNextCommand()` 暂存的命令一并塞进 entry——这是「被停车场寄存的下一棒」。
- BT：[src/BtCheckIntegrityEntry.cc:67-73](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtCheckIntegrityEntry.cc#L67-L73) 构造 `BtFileAllocationEntry`，但**不带 nextCommand**（传 `nullptr`）——因为 BT 的下载命令要等分配完成后由 `BtSetup` 现场装配。

顺带说明：即使不开 `--check-integrity`，普通下载也会经过这条流水线——[src/RequestGroup.cc:525-526](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L525-L526) 无条件创建 `StreamCheckIntegrityEntry`，在 [src/RequestGroup.cc:537-552](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L537-L552) 里判断不需要校验时直接调用 `entry->onDownloadIncomplete(commands, e)` 立刻流入预分配衔接点。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「预分配阶段存在与否」的差别，并把控制台进度行与源码对应起来。

1. 准备一个 64MB 以上的测试文件（超过默认 `--no-file-allocation-limit=5M` 才会触发分配），并起一个本地 HTTP 服务（示例命令，在专门的临时目录里执行）：

   ```bash
   mkdir -p /tmp/alloc-lab/www && cd /tmp/alloc-lab/www
   head -c 67108864 /dev/zero > big.bin        # 64MB 定长文件
   python3 -m http.server 8000
   ```

2. 分别用两种方式下载（另开终端，下载目录每次清空）：

   ```bash
   cd /tmp/alloc-lab
   /path/to/aria2c --file-allocation=prealloc -d run1 http://127.0.0.1:8000/big.bin
   /path/to/aria2c --file-allocation=none    -d run2 http://127.0.0.1:8000/big.bin
   ```

3. 需要观察的现象：`prealloc` 那次在下载真正开始前，进度行会出现形如 `[FileAlloc:#xxxx 32MiB/64MiB(50%)]` 的字段；`none` 那次不会出现（闸门条件①直接失败，entry 走了 `prepareForNextAction` 分支）。
4. 预期结果：两次最终文件大小一致、校验一致；区别只在下载开始前的那段停顿。
5. 若本地文件系统预分配瞬时完成，`[FileAlloc]` 可能一闪而过——可改用 `--file-allocation=prealloc` 且在较慢的介质上测试，或直接看下一步 4.2.4 的日志法。以上现象**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：把 `--file-allocation` 设为 `none` 之后，`FileAllocationEntry` 还会被创建吗？

答案：会。`StreamCheckIntegrityEntry::onDownloadIncomplete` 依然 `make_unique<StreamFileAllocationEntry>` 并调用 `proceedFileAllocation`，但 `needsFileAllocation()` 的条件①（`V_NONE` 检查）不满足，于是走 else 分支直接 `prepareForNextAction`，entry 随即销毁，不进停车场、不产生任何分配命令。

**练习 2**：一个 3MB 的文件用默认选项（`prealloc`、`--no-file-allocation-limit=5M`）下载，会预分配吗？

答案：不会。闸门条件②要求 `5M <= totalLength`，3MB 不达标，直接跳过预分配阶段。想让小文件也分配可以把 `--no-file-allocation-limit` 调小（选项默认值定义在 [src/OptionHandlerFactory.cc:535](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L535)）。

**练习 3**：为什么 `BtFileAllocationEntry` 构造时不传 `nextCommand`，而流式的要传？

答案：流式协议的命令链在任务创建时就已知道下一棒（CreateRequestCommand 一族），进停车场前把它寄存在 entry 里，出来后接着用；BT 的运行时对象（PeerStorage、BtInteractive 等）必须等文件分配完成、任务即将激活时才由 `BtSetup().setup()` 装配（见 [src/BtFileAllocationEntry.cc:69](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtFileAllocationEntry.cc#L69)），所以没有可寄存的下一棒。

### 4.2 FileAllocationCommand 调度：一个 dispatcher 与一串瞬时 worker

#### 4.2.1 概念说明

预分配队列里可能同时排着多个任务，但 aria2 **同一时刻只让一个任务做分配**。原因很实际：默认的 `prealloc` 在老文件系统上会退化为真实写零（见 4.3），多个任务并行写零会把磁盘打爆；而且分配属于「下载前的准备」，串行化实现最简单、进度最好展示。

实现上是一对角色：

- **dispatcher（常驻）**：`FileAllocationDispatcherCommand`，一个例程命令（routine command，见 u2-l1），每轮主循环醒来一次，只做一件事——如果停车场上没有「正在处理」的条目且队列非空，就取出队首条目，为它铸造一个 worker 命令；
- **worker（瞬时）**：`FileAllocationCommand`，接到条目后自我循环推进分配，干完即销毁；销毁时把条目从停车场取下，dispatcher 下一轮才能取下一个。

一个常驻的 dispatcher 对一串先后继起的瞬时 worker，这就是本讲学习目标里「dispatcher 与 entry 的协作方式」。

#### 4.2.2 核心流程

```text
引擎装配（DownloadEngineFactory）
  └─ FileAllocationMan（空停车场）+ FileAllocationDispatcherCommand 注册为例程命令
        │  每轮主循环：
        ├─ 下载全部结束/请求停机 → dispatcher 退出（返回 true 销毁）
        ├─ 队列空 或 已有 picked 条目 → 什么都不做
        └─ 否则 pickNext() 取队首 entry → createCommand() 铸造 worker
             │
             ▼
FileAllocationCommand（worker，RealtimeCommand）
  ├─ executeInternal():
  │    ├─ 任务请求停机 → 返回 true（销毁）
  │    ├─ entry->allocateChunk()   ← 推进一段分配
  │    ├─ entry->finished()？
  │    │    ├─ 是 → 日志"NN seconds to allocate …"，prepareForNextAction()
  │    │    │        生成下载命令 → 返回 true（销毁）
  │    │    └─ 否 → addCommand(this) 自我重排 → 返回 false
  │    └─ 异常（磁盘满等）→ handleException：记错误码 → 返回 true（销毁）
  └─ 析构函数 → 停车场 dropPickedEntry()，dispatcher 下一轮取下一个条目
```

#### 4.2.3 源码精读

**引擎装配时注册 dispatcher。**

[src/DownloadEngineFactory.cc:161-168](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngineFactory.cc#L161-L168)

```cpp
e->setFileAllocationMan(make_unique<FileAllocationMan>());
e->setCheckIntegrityMan(make_unique<CheckIntegrityMan>());
...
e->addRoutineCommand(make_unique<FileAllocationDispatcherCommand>(
    e->newCUID(), e->getFileAllocationMan().get(), e.get()));
```

`FileAllocationMan` 本体只是个类型别名——[src/FileAllocationMan.h:45](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FileAllocationMan.h#L45) 的 `typedef SequentialPicker<FileAllocationEntry> FileAllocationMan;`，即 u3-l1 见过的「停车场」模板：内部一个 `deque` 待办队列加一个 `pickedEntry_` 单槽（取放语义见 [src/SequentialPicker.h:60-75](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SequentialPicker.h#L60-L75)，`pickNext` 把队首搬进单槽、`dropPickedEntry` 清空单槽）。单槽的存在天然保证了「同时只有一个条目在处理」。

**dispatcher 的每轮动作：模板方法在基类，取条目铸造 worker 在子类。**

[src/SequentialDispatcherCommand.h:67-80](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SequentialDispatcherCommand.h#L67-L80)

```cpp
virtual bool execute() CXX11_OVERRIDE
{
  if (e_->getRequestGroupMan()->downloadFinished() || e_->isHaltRequested()) {
    return true;                                  // 引擎收工，dispatcher 自灭
  }
  if (picker_->hasNext() && !picker_->isPicked()) {
    e_->addCommand(createCommand(picker_->pickNext()));  // 取队首→铸造 worker
    e_->setNoWait(true);                          // 别睡，立刻跑下一轮
  }
  e_->addRoutineCommand(std::unique_ptr<Command>(this)); // 自我重排，下轮再来
  return false;
}
```

[src/FileAllocationDispatcherCommand.cc:52-59](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FileAllocationDispatcherCommand.cc#L52-L59)

```cpp
std::unique_ptr<Command>
FileAllocationDispatcherCommand::createCommand(FileAllocationEntry* entry)
{
  cuid_t newCUID = getDownloadEngine()->newCUID();
  A2_LOG_INFO(fmt(MSG_FILE_ALLOCATION_DISPATCH, newCUID));  // "Dispatching FileAllocationCommand for CUID#N."
  return make_unique<FileAllocationCommand>(newCUID, entry->getRequestGroup(),
                                            getDownloadEngine(), entry);
}
```

这段代码为新 worker 申请了新的 cuid 并打 INFO 日志（消息模板在 [src/message.h:130-132](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/message.h#L130-L132)）。注意校验队列的 `CheckIntegrityDispatcherCommand` 复用同一个模板基类，只是铸造的 worker 不同。

**worker 的自循环与三个出口。**

[src/FileAllocationCommand.cc:68-91](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FileAllocationCommand.cc#L68-L91)

```cpp
bool FileAllocationCommand::executeInternal()
{
  if (getRequestGroup()->isHaltRequested()) {
    return true;                                   // 出口一：任务被停止
  }
  fileAllocationEntry_->allocateChunk();           // 推进一段（多则 256KB 写零，少则一次系统调用）
  if (fileAllocationEntry_->finished()) {
    A2_LOG_DEBUG(fmt(MSG_ALLOCATION_COMPLETED, /* 耗时秒数 */, /* 总长 */));
    std::vector<std::unique_ptr<Command>> commands;
    fileAllocationEntry_->prepareForNextAction(commands, getDownloadEngine());
    getDownloadEngine()->addCommand(std::move(commands));  // 下载命令入场
    getDownloadEngine()->setNoWait(true);
    return true;                                   // 出口二：完成，销毁
  }
  else {
    getDownloadEngine()->addCommand(std::unique_ptr<Command>(this));
    return false;                                  // 未完：自我重排（u2-l3 约定）
  }
}
```

worker 每轮只推进一步 `allocateChunk()`（步长由迭代器决定，见 4.3），然后要么完成要么自我重排——这正是 u2-l3 讲过的「返回 false 必须已 addCommandSelf」约定。分步推进的意义：`falloc` 的单次系统调用可能很慢（老文件系统），但 `prealloc` 回退到写零时每步只写 256KB 就把控制权还给主循环，Ctrl-C、其他下载、进度刷新都不被饿死。

**worker 的基类给它加了三件装备。**

[src/RealtimeCommand.cc:42-64](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RealtimeCommand.cc#L42-L64) 中构造函数调用 `setStatusRealtime()`（绕过 u2-l1 说的每秒闸门，尽量每轮执行）、`increaseNumCommand()`（保持 RequestGroup 的命令计数非零，任务不会被误判结束），`execute()` 每次先 `e_->setNoWait(true)`（不让引擎 poll 时睡眠）再把 `executeInternal` 包进 try/catch。异常落入 [src/FileAllocationCommand.cc:93-101](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FileAllocationCommand.cc#L93-L101)：

```cpp
bool FileAllocationCommand::handleException(Exception& e)
{
  getRequestGroup()->setLastErrorCode(e.getErrorCode(), e.what());
  A2_LOG_ERROR_EX(fmt(MSG_FILE_ALLOCATION_FAILURE, getCuid()), e);
  ...
  return true;                                     // 出口三：失败，销毁
}
```

分配失败（典型如磁盘满，错误码 `NOT_ENOUGH_DISK_SPACE`，见 4.3.3）会记到任务的 lastErrorCode 并结束本 worker。

**最关键的一行在析构函数里。**

[src/FileAllocationCommand.cc:63-66](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FileAllocationCommand.cc#L63-L66)

```cpp
FileAllocationCommand::~FileAllocationCommand()
{
  getDownloadEngine()->getFileAllocationMan()->dropPickedEntry();
}
```

无论 worker 从哪个出口销毁（完成、失败、停机），析构都会清掉停车场的单槽——entry 的生命周期与 worker 命令严格绑定。dispatcher 下一轮发现 `isPicked()` 为假且队列非空，才会取下一个条目。全局串行化就是靠「单槽 + 析构放行」这一对机制实现的。

**进度如何上屏。**

[src/ConsoleStatCalc.cc:349-367](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ConsoleStatCalc.cc#L349-L367) 从 `getFileAllocationMan()->getPickedEntry()` 取当前 entry，打印 `[FileAlloc:#GID cur/total(pct)]`，若队列还有排队者再追加 `(+N)`。entry 的 `getCurrentLength/getTotalLength` 正是委托给 4.1.3 里那个迭代器。

#### 4.2.4 代码实践

**实践目标**：用日志把 dispatcher→worker→完成这条链完整走一遍，验证 4.2.2 的流程图。

1. 操作步骤（沿用 4.1.4 的本地服务）：

   ```bash
   cd /tmp/alloc-lab
   /path/to/aria2c --file-allocation=prealloc -l=alloc.log --log-level=info \
       -d run3 http://127.0.0.1:8000/big.bin
   grep -nE "Dispatching FileAllocationCommand|seconds to allocate|allocat" alloc.log
   ```

2. 需要观察的现象：日志中应出现 `Dispatching FileAllocationCommand for CUID#N.`（dispatcher 铸造 worker 的那一刻，INFO 级别）；若日志级别为 debug，还能看到 `NN seconds to allocate 67108864 byte(s)`（MSG_ALLOCATION_COMPLETED）。也可以在 debug 级别下看到 4.3.3 提到的 "Testing file system supports fallocate." 探测行。
3. 预期结果：Dispatch 日志出现在任何下载请求日志（CUID 连接类日志）之前，证明分配是下载前的独立阶段。
4. 若想数 worker 自循环了多少轮：可在 [src/FileAllocationCommand.cc:73](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FileAllocationCommand.cc#L73) 的 `allocateChunk()` 前加一行计数日志重新编译（改源码请用副本目录，勿动仓库）。回退写零时轮数应约等于 \( \lceil 64\mathrm{MB}/256\mathrm{KB}\rceil = 256 \) 轮。具体数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果 dispatcher 在 worker 还没跑完时又取了一个条目，会发生什么？源码里哪一行防止了这件事？

答案：会出现两个 worker 同时分配、停车场单槽被覆盖的混乱。防止它的是 [src/SequentialDispatcherCommand.h:72](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SequentialDispatcherCommand.h#L72) 的 `picker_->hasNext() && !picker_->isPicked()`——只要单槽里还有 picked entry（即上一个 worker 未销毁），dispatcher 就不取新条目。

**练习 2**：worker 为什么继承 `RealtimeCommand` 而不是普通的 `AbstractCommand`？

答案：分配是纯本地 I/O，不碰 socket、不需要 EventPoll 注册。RealtimeCommand 提供「每轮循环尽量执行」（setStatusRealtime 绕过每秒闸门）+「不让引擎睡眠」（setNoWait）+「计入 RequestGroup 的 numCommand 计数防误判完成」三件装备，恰好是分配 worker 需要的全部；AbstractCommand 那套 segment/socket/超时机制反而是负担。

**练习 3**：按下 Ctrl-C（优雅停机）时，正在分配的 worker 会怎样？

答案：主循环的 afterEachIteration 会置停机状态（u2-l1），RequestGroup 的 `isHaltRequested()` 变真，worker 下一轮 `executeInternal` 开头即返回 true 销毁，析构 `dropPickedEntry` 放行；dispatcher 下一轮也因 `isHaltRequested()` 返回 true 自灭。已分配的那部分文件保留在磁盘上，续传时由 `finished()` 检查判定「还需不需要继续分配」。

### 4.3 三种分配迭代器：falloc / trunc / adaptive（及写零回退）

#### 4.3.1 概念说明

命令层（dispatcher/worker）只认 `FileAllocationIterator` 的四个方法：`allocateChunk() / finished() / getCurrentLength() / getTotalLength()`（接口见 [src/FileAllocationIterator.h:48-54](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FileAllocationIterator.h#L48-L54)）。**怎么分配**完全下沉到迭代器一族：

- `TruncFileAllocationIterator`——`trunc`：一次 `ftruncate`，稀疏占位；
- `FallocFileAllocationIterator`——`falloc`：一次 `fallocate`，真实分配；
- `AdaptiveFileAllocationIterator`——`prealloc`（**默认值**）：先花 4KB 试探文件系统支不支持 fallocate，支持就转投 falloc 快车道，不支持就回退 `SingleFileAllocationIterator` 写零；
- `MultiFileAllocationIterator`——多文件任务的壳，逐个文件套用上面三种之一。

`--file-allocation=none` 不是第五种迭代器：它在闸门处就让任务跳过了整个阶段。

#### 4.3.2 核心流程

从命令行字符串到系统调用的完整映射链：

```text
--file-allocation=falloc|trunc|prealloc|none   （OptionHandlerFactory 注册，短选项 -a，默认 prealloc）
        │
        ▼ DefaultPieceStorage::initStorage() 末尾翻译
DiskAdaptor::FILE_ALLOC_FALLOC | FILE_ALLOC_TRUNC | FILE_ALLOC_ADAPTIVE（内部枚举，三值）
        │
        ▼ AbstractSingleDiskAdaptor::fileAllocationIterator() 按枚举 new 迭代器
Falloc / Trunc / Adaptive(→试探→Falloc 或 Single 写零) 迭代器
        │
        ▼ 迭代器调用 BinaryStream::allocate(offset, len, sparse) / truncate(len)
AbstractDiskWriter 平台分支：
  sparse=true            → ftruncate / SetEndOfFile(MinGW)
  Linux + HAVE_FALLOCATE → fallocate(2)
  其他 POSIX             → posix_fallocate
  macOS                  → fcntl(F_PREALLOCATE) + ftruncate
  MinGW                  → SetFileValidData（需特权）
```

#### 4.3.3 源码精读

**第一步：选项注册。** [src/OptionHandlerFactory.cc:319-334](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L319-L334) 用 ParameterOptionHandler 注册 `--file-allocation`，默认值 `V_PREALLOC`，候选值 `none/prealloc/trunc`，外加条件编译的 `falloc`：

```cpp
OptionHandler* op(new ParameterOptionHandler(
    PREF_FILE_ALLOCATION, TEXT_FILE_ALLOCATION, V_PREALLOC,
    {V_NONE, V_PREALLOC, V_TRUNC,
#ifdef HAVE_SOME_FALLOCATE
     V_FALLOC
#endif // HAVE_SOME_FALLOCATE
    },
    'a'));
```

注意：如果编译时没有任何 fallocate 家族函数（`HAVE_SOME_FALLOCATE` 未定义），`falloc` 根本不会出现在候选值里，传了会在选项解析阶段被拒——这就是帮助文本里「falloc may not be available…」的出处（完整帮助文本在 [src/usage_text.h:133-151](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/usage_text.h#L133-L151)，其中明确建议 ext4(extents)/btrfs/xfs/NTFS(MinGW) 用 falloc，警告 ext3/FAT32 上 falloc 与 prealloc 一样慢且会完全阻塞 aria2）。

**第二步：翻译成内部三值枚举。** [src/DefaultPieceStorage.cc:656-661](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L656-L661)

```cpp
if (option_->get(PREF_FILE_ALLOCATION) == V_FALLOC) {
  diskAdaptor_->setFileAllocationMethod(DiskAdaptor::FILE_ALLOC_FALLOC);
}
else if (option_->get(PREF_FILE_ALLOCATION) == V_TRUNC) {
  diskAdaptor_->setFileAllocationMethod(DiskAdaptor::FILE_ALLOC_TRUNC);
}
```

没有分支命中（`prealloc` 或 `none`）就保持 [src/DiskAdaptor.h:55-59](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DiskAdaptor.h#L55-L59) 定义的默认值 `FILE_ALLOC_ADAPTIVE`——`none` 无所谓，反正到不了分配阶段。

**第三步：按枚举铸造迭代器。** [src/AbstractSingleDiskAdaptor.cc:123-139](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractSingleDiskAdaptor.cc#L123-L139)

```cpp
switch (getFileAllocationMethod()) {
#ifdef HAVE_SOME_FALLOCATE
case (DiskAdaptor::FILE_ALLOC_FALLOC):
  return make_unique<FallocFileAllocationIterator>(diskWriter_.get(), size(), totalLength_);
#endif
case (DiskAdaptor::FILE_ALLOC_TRUNC):
  return make_unique<TruncFileAllocationIterator>(diskWriter_.get(), size(), totalLength_);
default:
  return make_unique<AdaptiveFileAllocationIterator>(diskWriter_.get(), size(), totalLength_);
}
```

三个迭代器的第二、三参都是「现有文件大小（=起始 offset）」与「目标总长」。多文件任务则走 [src/MultiDiskAdaptor.cc:449-453](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiDiskAdaptor.cc#L449-L453)，返回 `MultiFileAllocationIterator`。

**第四步：三个迭代器各自的「一步」。**

`trunc`——一次调用，稀疏占位（[src/TruncFileAllocationIterator.cc:47-51](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TruncFileAllocationIterator.cc#L47-L51)）：

```cpp
void TruncFileAllocationIterator::allocateChunk()
{
  stream_->allocate(0, totalLength_, true);   // 第三参 sparse=true
  offset_ = totalLength_;
}
```

`sparse=true` 让底层直接走截断路径（见第五步源码），既撑大也收缩，把文件精确定型到 `totalLength_`。瞬时完成，但块并未真正分配。

`falloc`——一步到位真实分配（[src/FallocFileAllocationIterator.cc:49-59](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FallocFileAllocationIterator.cc#L49-L59)）：

```cpp
void FallocFileAllocationIterator::allocateChunk()
{
  if (offset_ < totalLength_) {
    stream_->allocate(offset_, totalLength_ - offset_, false);  // 一次分完剩余全部
    offset_ = totalLength_;
  }
  else {
    stream_->truncate(totalLength_);   // 文件已比目标大 → 收缩
    offset_ = totalLength_;
  }
}
```

`adaptive`（默认 `prealloc`）——先用 4KB 试探再选路（[src/AdaptiveFileAllocationIterator.cc:56-90](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AdaptiveFileAllocationIterator.cc#L56-L90)）：

```cpp
if (!allocator_) {
#ifdef HAVE_FALLOCATE
  try {
    if (offset_ < totalLength_) {
      int64_t len = std::min(totalLength_ - offset_, static_cast<int64_t>(4_k));
      stream_->allocate(offset_, len, false);      // 试探：只分配 4KB
      offset_ += len;                              // 试探成功的部分不回滚，后续接着分
    }
    allocator_ = make_unique<FallocFileAllocationIterator>(stream_, offset_, totalLength_);
  }
  catch (RecoverableException& e) {
    // 文件系统不支持 → 回退写零
    auto salloc = make_unique<SingleFileAllocationIterator>(stream_, offset_, totalLength_);
    salloc->init();
    allocator_ = std::move(salloc);
  }
#else
  /* 直接回退写零 */
#endif
  allocator_->allocateChunk();
}
```

为什么试探只分 4KB？因为 `fallocate` 失败要等系统调用返回才知道，若一口气分几个 GiB 而文件系统不支持，可能白等很久；4KB 的代价可以忽略。试探成功的那 4KB 计入 `offset_`，剩余交给 Falloc 迭代器一次分完。**这就是默认值 `prealloc` 的真面目：新文件系统上近似 `falloc`，老文件系统上退化为写零。**

写零回退 `SingleFileAllocationIterator`——256KB 一块（[src/SingleFileAllocationIterator.cc:48-49](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SingleFileAllocationIterator.cc#L48-L49) 定义 `BUFSIZE = 256_k`、`ALIGNMENT = 512`）：

```cpp
void SingleFileAllocationIterator::allocateChunk()      // L88-L97
{
  stream_->writeData(buffer_, BUFSIZE, offset_);        // 写一块零
  offset_ += BUFSIZE;
  if (totalLength_ < offset_) {
    stream_->truncate(totalLength_);                    // 最后一块越界 → 收回来
    offset_ = totalLength_;
  }
}
```

它的 `init()`（[src/SingleFileAllocationIterator.cc:70-86](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SingleFileAllocationIterator.cc#L70-L86)）分配一块 512 字节对齐的 256KB 零缓冲（`posix_memalign` 可用时对齐分配），并**每个进程只打一次** NOTICE 日志：`Allocating disk space. Use --file-allocation=none to disable it.`——如果你在老文件系统上见过这条提示，来源就是这里。总写盘次数为 \( N=\lceil L/262144\rceil \) 次。

**第五步：系统调用层的平台矩阵。** 迭代器调用的 `stream_->allocate(offset, len, sparse)` / `truncate(len)` 最终落到 [src/AbstractDiskWriter.cc:481-576](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractDiskWriter.cc#L481-L576)：

- `truncate`（[L481-500](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractDiskWriter.cc#L481-L500)）：POSIX 走 `a2ftruncate(fd_, length)`，MinGW 因其 `ftruncate` 处理不了 2GB 以上文件改用 `seek + SetEndOfFile`；
- `allocate` 的稀疏分支（[L507-518](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractDiskWriter.cc#L507-L518)）：MinGW 先尝试 `FSCTL_SET_SPARSE` 把文件标记为稀疏，随后统一 `truncate(offset + length)` 收工——这就是 `trunc` 迭代器的物理终点；
- `allocate` 的真实分配分支按平台四选一：MinGW 用 `SetFileValidData`（[L519-530](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractDiskWriter.cc#L519-L530)）；macOS 用 `fcntl(F_PREALLOCATE)` 先连续后非连续、再 `ftruncate` 落盘（[L531-548](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractDiskWriter.cc#L531-L548)）；Linux 有 `fallocate(2)`（[L549-562](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractDiskWriter.cc#L549-L562)，`HAVE_FALLOCATE`，同时就是 adaptive 试探所依赖的那个调用，循环重试 `EINTR`）；否则 `posix_fallocate`（[L563-571](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractDiskWriter.cc#L563-L571)）。失败时统一抛 `DL_ABORT_EX3`，并用 `isDiskFullError` 把磁盘满映射为 `NOT_ENOUGH_DISK_SPACE`、其余映射 `FILE_IO_ERROR`——正是 4.2.3 里 worker `handleException` 收到的那个错误码。

**多文件任务的壳。** [src/MultiFileAllocationIterator.cc:63-127](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiFileAllocationIterator.cc#L63-L127) 遍历 DiskWriterEntries：跳过没有写入器或 `needsFileAllocation()` 为假的条目；对 `size() < fileEntry->getLength()` 的文件，**新建一个专用 DiskWriter**（不复用条目原有的写入器——注释解释：SingleFileAllocationIterator 无法重开被 OpenedFileCounter 关掉的文件，见 u5-l3 的 100 文件上限），按 DiskAdaptor 上的分配方法给这个文件套同款单文件迭代器（[L102-117](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiFileAllocationIterator.cc#L102-L117) 的 switch 与第四步的 switch 同构）；当前文件 `finished()` 后 `closeFile` 前进到下一个（[L65-77](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiFileAllocationIterator.cc#L65-L77)）。这也解释了 4.1.3 闸门条件③对多文件任务的语义：全新迭代器只要面对非空条目列表就 `finished()` 为假（[L129-133](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiFileAllocationIterator.cc#L129-L133)），所以多文件任务几乎总会进预分配阶段，再由这里逐文件判断谁真的需要分配。

**Windows 特例值得一提。** [src/RequestGroup.cc:622-649](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L622-L649)（MinGW 编译时）会为 `falloc` 尝试获取 `SE_MANAGE_VOLUME_NAME` 特权，并打警告：`SetFileValidData` 会把**未初始化的磁盘残留数据**暴露进下载文件，有保密风险，介意时改用 `prealloc`。这是「同一选项在不同平台语义不同」的典型样本，也呼应 u1-l1「功能与平台绑定」的结论。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：用 `time` + `strace` 拿到四种分配方式的系统调用证据，验证 4.3.2 的映射表。

1. 操作步骤（沿用 4.1.4 的 64MB 本地文件；建议在 ext4/xfs 上做，文件系统不同结论可能不同）：

   ```bash
   cd /tmp/alloc-lab
   for m in none trunc falloc prealloc; do
     rm -rf run-$m && mkdir run-$m
     /usr/bin/time -v strace -f -e trace=fallocate,ftruncate,write,openat \
       -o trace-$m.log \
       /path/to/aria2c --file-allocation=$m -x1 -s1 -d run-$m \
       http://127.0.0.1:8000/big.bin 2> time-$m.log
   done
   for m in none trunc falloc prealloc; do
     echo "=== $m ==="; grep -cE 'fallocate|ftruncate' trace-$m.log
   done
   ```

   （若 aria2 编译时未启用 fallocate 家族，`--file-allocation=falloc` 会在解析参数时报错，这本身就是一个验证点。）

2. 需要观察的现象（对照 `trace-*.log`）：
   - `none`：下载前**没有任何** `fallocate/ftruncate`，只有下载数据本身的 `write`；
   - `trunc`：恰有一次 `ftruncate`（把文件定型到 64MB），没有 `fallocate`；此时 `du -h run-trunc/big.bin` 应显著小于 64M（稀疏），下载完成后才被写满；
   - `falloc`：恰有一次 `fallocate`（无 4KB 试探），随后进入下载；
   - `prealloc`：ext4/xfs 上应看到**先一次 4KB 的 `fallocate`（试探），再一次大额 `fallocate`**；在不支持 fallocate 的文件系统上则看不到第二次 `fallocate`，取而代之的是约 256 次、每次 262144 字节的 `write`（写零回退）。
3. 对照 `/usr/bin/time -v` 的 Elapsed time 与 Minor/Major page faults：四种方式总耗时的差异主要出现在「下载开始之前」。
4. 预期结果整理成对比表：

   | 方式 | 预分配期系统调用 | 是否真实占块 | 预分配耗时（相对） | 备注 |
   | --- | --- | --- | --- | --- |
   | none | 无 | 否 | 0 | 无碎片优化、不提前发现磁盘满 |
   | trunc | 1 次 ftruncate | 否（稀疏） | ≈0 | 定长占位 |
   | falloc | 1 次 fallocate | 是 | 极短（新 fs） | 老文件系统上可能很慢且阻塞全局 |
   | prealloc | 4KB 试探 fallocate + 1 次 fallocate（或 256 次 write） | 是 | 视文件系统 | 默认值，自适应 |

5. 以上各文件系统上的具体表现**待本地验证**；若与预期不符，请回到 4.3.3 的源码分支逐一排查（例如 strace 中 `fallocate` 返回 `EOPNOTSUPP` 即触发 adaptive 回退）。

#### 4.3.5 小练习与答案

**练习 1**：`prealloc` 与 `falloc` 都以 `fallocate` 收场，为什么还要存在两个选项？

答案：`falloc` 是「硬性承诺」——文件系统不支持就直接失败；`prealloc` 是「尽力而为」——Adaptive 迭代器先花 4KB 试探（[src/AdaptiveFileAllocationIterator.cc:62-67](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AdaptiveFileAllocationIterator.cc#L62-L67)），不支持就静默回退写零。所以默认值用 `prealloc` 保证任何环境都能跑，而明确知道自己文件系统能力的用户可以选 `falloc` 拿确定的行为。

**练习 2**：`trunc` 撑出来的文件是稀疏的，`ls -l` 与 `du -h` 各显示多大？下载完成后呢？

答案：下载前 `ls -l` 显示 64MB（逻辑长度），`du -h` 只显示几 KB~几十 KB（洞不占块）；下载完成后数据块被逐段写入，`du -h` 逐渐逼近 64MB。可以据此判断「trunc 只是占位」。

**练习 3**：`FallocFileAllocationIterator::allocateChunk` 里为什么还有一个 `truncate(totalLength_)` 分支？什么情况会走到？

答案：当磁盘上现有文件已经**大于**目标长度（`offset_ >= totalLength_`，例如上一次下载的目标更大、或用户换了较小的 `-o` 续传同一文件）时，需要的不是扩张而是收缩，于是走 `truncate` 把文件裁回 `totalLength_`。`TruncFileAllocationIterator` 用 `allocate(0, totalLength_, true)` 一次性覆盖了扩张与收缩两种情况，而 falloc 迭代器必须显式区分。

## 5. 综合实践

把 4.3.4 的实验扩成一份完整的「预分配行为报告」，要求证据链闭环：

1. **对象**：改用 512MB~1GB 的本地测试文件（阈值以下触发不了分配；也不要太大，写零回退可能要等几分钟）。
2. **四种方式各跑一次**，每次保留三样证据：`strace` 过滤日志、`/usr/bin/time -v` 输出、aria2 的 `-l=xx.log --log-level=debug` 日志。
3. **日志↔源码对齐**：在 debug 日志中找出并标注三行——`Dispatching FileAllocationCommand for CUID#N.`（dispatcher 铸造 worker，[src/FileAllocationDispatcherCommand.cc:56](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FileAllocationDispatcherCommand.cc#L56)）、`Testing file system supports fallocate.` / `File system supports fallocate.` 或 `File system does not support fallocate.`（adaptive 试探，[src/AdaptiveFileAllocationIterator.cc:61-73](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AdaptiveFileAllocationIterator.cc#L61-L73)）、`Allocating disk space. Use --file-allocation=none to disable it.`（写零回退的一次性提示，[src/SingleFileAllocationIterator.cc:75-77](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SingleFileAllocationIterator.cc#L75-L77)）。
4. **交付**：一张五行对比表（none/trunc/falloc/prealloc-成功/prealloc-回退，若你的环境只能呈现四行，说明原因），列包含：预分配期系统调用计数、`time` 的墙钟耗时、`du` 前后对比、日志证据行号、对应的源码分支（文件:行号）。
5. **附加题**：用 `--file-allocation=trunc` 下载到一半 Ctrl-C，再以同参数重启续传，对照 [src/RequestGroup.cc:1163-1169](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L1163-L1169) 解释为什么第二次启动几乎立刻通过预分配闸门（提示：`finished()` 检查的是「现有文件大小是否已达总长」，trunc 第一次就已经把文件定型了）。

## 6. 本讲小结

- 文件预分配是校验阶段与下载命令链之间的**独立排队阶段**：`proceedFileAllocation` 按三条件闸门（选项非 none、总长达 `--no-file-allocation-limit`、迭代器未 finished）决定进队或跳过。
- 队列 `FileAllocationMan` 就是 u3-l1 的 SequentialPicker 停车场：单槽保证**全局同一时刻只有一个任务在分配**；常驻 dispatcher 每轮取队首铸造 worker，worker 析构 `dropPickedEntry` 放行下一个。
- worker `FileAllocationCommand` 是 RealtimeCommand：每轮推进 `allocateChunk()` 一步，完成则 `prepareForNextAction` 生成下载命令，失败则记错误码退出；写零回退时每步仅 256KB，保证 Ctrl-C 与其他任务不被饿死。
- 三种分配手段层层下沉：`--file-allocation` 字符串 → `DiskAdaptor` 三值内部枚举 → 迭代器一族 → `AbstractDiskWriter::allocate/truncate` 的平台矩阵（ftruncate / fallocate / posix_fallocate / F_PREALLOCATE / SetFileValidData）。
- 默认值 `prealloc` 的真名是 adaptive：4KB 试探 fallocate，可用则转快车道，不可用回退写零并打一次性 NOTICE；`trunc` 只做稀疏占位不真实占块；`falloc` 在 MinGW 上还有暴露磁盘残留数据的安全警示。
- 多文件任务由 `MultiFileAllocationIterator` 逐文件套用单文件迭代器，并遵循 u5-l3 的 needsFileAllocation/OpenedFileCounter 机制。

## 7. 下一步学习建议

本讲讲完了「下载前」的最后一段准备。接下来：

- **u5-l5 校验与哈希**：本讲反复出现的 `CheckIntegrityEntry`、`CheckIntegrityMan` 停车场与 `onDownloadIncomplete/onDownloadFinished` 回调将在那一讲完整展开，两个阶段共用同一套 SequentialDispatcherCommand 骨架，对照阅读收益最大。
- **u5-l6 写盘缓存**：预分配解决「空间」，写缓存解决「写放大」，两者共同构成磁盘 I/O 策略。
- 若你走 BT 路线：**u6-l2 BtRegistry 与 BtSetup** 会接上本讲 4.1.3 埋下的伏笔——`BtFileAllocationEntry::prepareForNextAction` 里那行 `BtSetup().setup()` 到底装配了哪些对象。
- 测试佐证：`test/SingleFileAllocationIteratorTest.cc`、`test/FallocFileAllocationIteratorTest.cc`、`test/MultiFileAllocationIteratorTest.cc` 展示了如何在无真实下载的情况下驱动迭代器，是理解「迭代器无状态依赖、可独立测试」的好材料。
