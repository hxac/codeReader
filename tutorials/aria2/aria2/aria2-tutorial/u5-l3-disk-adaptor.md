# DiskAdaptor：单文件与多文件布局适配

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 aria2 为什么要在 PieceStorage 与 DiskWriter 之间再加一层 DiskAdaptor，它把什么问题隔离了。
2. 画出「一次跨文件的 writeData(data, len, offset)」在 MultiDiskAdaptor 中被切分到各个物理文件的完整过程，并说清楚二分查找、字节切分与懒打开三个环节。
3. 解释 `--select-file` 选中的文件与它首尾相邻的文件「共享 piece」时，MultiDiskAdaptor 的正反两趟扫描如何决定哪些未选中文件仍然要创建 DiskWriter、哪些文件干脆不落盘。
4. 说出单文件任务与多文件任务分别由谁（`DefaultPieceStorage::initStorage`）选择哪个适配器，以及打开文件数上限是如何被控制在 100 以内的。

## 2. 前置知识

本讲是磁盘 I/O 单元（单元 5）的第三讲，默认你已读过前两讲：

- **逻辑偏移空间**（u3-l2）：多文件任务（典型是 BT 种子）把所有文件按 `FileEntry::offset` 首尾相接，拼成一条从 0 开始的连续字节流。每个 `FileEntry` 用三个量描述自己在这条流上的位置：起点 `offset`、长度 `length`、终点 `getLastOffset()`（即 \( offset + length \)）。上层所有读写都用这条流上的偏移（称为**全局偏移 / goff**）。
- **piece 与 block**（u3-l4、u5-l2）：逻辑偏移空间按 piece 切分（HTTP 默认 1MB，BT 由种子给定），piece 是校验与分配的单位。关键点：**piece 边界是按全局偏移计算的，与文件边界无关**，所以一个 piece 可能同时覆盖两个文件的各一半。
- **DiskWriter**（u5-l1）：单 个物理文件的写入器，提供 `initAndOpenFile`（截断新建）/ `openFile`（不存在则创建）/ `openExistingFile`（只开已有）三种打开方式，以及按偏移读写。本讲的适配器就是要把「一堆 DiskWriter」包装成「一个逻辑上的字节流」。
- **过滤位图**（u5-l2）：`--select-file` 落到 PieceStorage 层是一张「过滤位图」，只有被选中文件覆盖的 piece 会被下载。本讲讲的是这件事在磁盘层的另一半故事。

另外需要一点 C++ 阅读常识：`std::upper_bound` 是二分查找；`std::unique_ptr` 独占所有权；纯虚函数加默认空实现（`virtual void f() {}`）表示「该能力可选，子类可以不实现」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/BinaryStream.h` | 最小读写契约：`writeData`/`readData` 按全局偏移读写 |
| `src/DiskAdaptor.h` / `.cc` | 适配器抽象基类，聚合 `FileEntry` 列表，定义打开/预分配/截尾等纯虚接口 |
| `src/AbstractSingleDiskAdaptor.h` / `.cc` | 单文件骨架：持有一个 DiskWriter 与总长，把所有调用直通转发 |
| `src/DirectDiskAdaptor.h` / `.cc` | 单文件具体实现，提供 `getFilePath()` |
| `src/MultiDiskAdaptor.h` / `.cc` | 多文件适配器，含 `DiskWriterEntry` 与跨文件切分逻辑（本讲主角） |
| `src/DefaultPieceStorage.cc` | `initStorage()`：按文件数选择 Direct 或 Multi 适配器 |
| `src/RequestGroup.cc` | 调用 `initStorage()` 并挂接 `OpenedFileCounter` |
| `src/DownloadContext.cc` / `src/download_helper.cc` | `--select-file` 如何传导为 `FileEntry::requested` 标志 |
| `src/OpenedFileCounter.cc` | 全局打开文件数上限控制 |
| `test/MultiDiskAdaptorTest.cc` | 多文件布局的权威测试样例，附直观的字节布局图 |

## 4. 核心概念与源码讲解

### 4.1 DiskAdaptor 接口：逻辑偏移与磁盘布局之间的变形缝

#### 4.1.1 概念说明

先想一个场景：BT 种子下载完一个 piece，要把它写盘。这个 piece 的数据可能一半属于 `dir/file1.avi`、一半属于 `dir/file2.srt`。而上层的调用者——PieceStorage（u5-l2）、校验器（u5-l5）、预分配器（u5-l4）、写盘缓存（u5-l6）——只懂全局偏移，它们只想说一句「把这块数据写到全局偏移 X 处」。

如果让每个调用者都自己去查「偏移 X 落在哪个文件、文件内偏移是多少、会不会跨界」，这套逻辑会在四五处重复。DiskAdaptor 就是为此抽出来的**变形缝**（适配层）：它向实现 `BinaryStream` 接口，把「一个连续的逻辑偏移空间」翻译成「磁盘上的一个或一串物理文件」。调用者拿到的 `DiskAdaptor` 对单文件任务和多文件任务完全同构。

#### 4.1.2 核心流程

类层次结构如下：

```text
BinaryStream                  （读写契约：writeData / readData / truncate / allocate）
    └── DiskAdaptor           （加上：打开关闭、FileEntry 聚合、预分配、截尾、utime…）
            ├── AbstractSingleDiskAdaptor   （单文件骨架：一个 DiskWriter + totalLength）
            │       └── DirectDiskAdaptor   （提供 getFilePath()，即"直接映射"）
            └── MultiDiskAdaptor            （多文件：N 个 DiskWriterEntry 拼成一个流）
```

把基类接口按职责分组，可以得到这张全景表：

| 职责组 | 方法 | 说明 |
| --- | --- | --- |
| 字节流读写 | `writeData` / `readData` / `truncate` / `allocate` | 继承自 BinaryStream，全局偏移语义 |
| 文件生命周期 | `openFile` / `openExistingFile` / `initAndOpenFile` / `closeFile` / `fileExists` / `size` | 三种打开方式对应 u5-l1 讲过的三种语义 |
| 预分配 | `fileAllocationIterator` / `setFileAllocationMethod` | 供 u5-l4 的 FileAllocationCommand 消费 |
| 可选能力 | `enableReadOnly` / `enableMmap` | 基类给默认空实现，即「能力可选」 |
| 收尾清理 | `cutTrailingGarbage` / `utime` | 截掉比元数据更长的尾部；恢复种子的时间戳 |
| 缓存配合 | `readDataDropCache` / `writeCache` / `flushOSBuffers` | 与 u5-l6 的 WrDiskCache 对接 |
| 资源控制 | `tryCloseFile` / `setOpenedFileCounter` | 控制全局打开文件数 |
| 元数据 | `setFileEntries` / `getFileEntries` | 适配器持有 FileEntry 列表以获知布局 |

#### 4.1.3 源码精读

先看最小契约 BinaryStream——只有两个纯虚方法加两个默认空实现：

[BinaryStream.h:L44-L61](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BinaryStream.h#L44-L61) 定义了 `writeData`（写失败抛异常）与 `readData`（返回实际读取数），两者都带 `int64_t offset` 参数。这个 offset 就是跨文件的全局偏移——接口层面根本没有「文件」的概念。

再看基类 DiskAdaptor 的骨架：

[DiskAdaptor.h:L53-L79](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DiskAdaptor.h#L53-L79) 声明类继承自 `BinaryStream`，并集中定义了三种打开方式（`openFile` / `openExistingFile` / `initAndOpenFile`）、`closeFile`、`fileExists`、`size` 这组文件生命周期纯虚接口——与 u5-l1 中 DiskWriter 的三种打开语义一一对应，适配器把它们从「单个文件」放大到「整个布局」。

[DiskAdaptor.h:L87-L130](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DiskAdaptor.h#L87-L130) 是其余能力的声明：`fileAllocationIterator`（预分配迭代器工厂）、`enableReadOnly`/`enableMmap`（默认空实现即可选能力）、`cutTrailingGarbage`（文件比元数据长时截尾）、`utime`（恢复时间戳）、`readDataDropCache`/`writeCache`（配合写盘缓存）、`tryCloseFile`（按需关闭文件）。注意 [DiskAdaptor.h:L76-L85](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DiskAdaptor.h#L76-L85) 的 `setFileEntries` 是模板成员，直接把 `DownloadContext` 的 FileEntry 列表灌进来——适配器正是靠这份列表才知道逻辑偏移空间长什么样。

最后看基类的实现文件，它会颠覆「基类总有一堆公共逻辑」的预期：

[DiskAdaptor.cc:L41-L43](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DiskAdaptor.cc#L41-L43) 全文只有构造函数（把分配方式默认设为 `FILE_ALLOC_ADAPTIVE`）和析构函数。DiskAdaptor 是纯接口层，所有真实行为都压在两个派生分支上。

#### 4.1.4 代码实践

**实践目标**：不动手编译，仅通过头文件建立 DiskAdaptor 接口全景，并识别「可选能力」。

**操作步骤**：

1. 打开 `src/DiskAdaptor.h`，把 4.1.2 那张职责表盖住，自己重新归类一遍全部方法。
2. 用编辑器搜索 `= 0;`（纯虚）与 `{}`（默认空实现），数一数各有几个。
3. 对照 `git log --oneline -- src/DiskAdaptor.h | head -5`，看看这个接口最近一次变动是何时、改了什么。

**需要观察的现象**：纯虚方法（必须实现）与空实现方法（可选能力）的数量对比；`tryCloseFile` 的默认返回值是 0（「我这边没有可关的文件」）。

**预期结果**：纯虚方法约 13 个，空实现默认方法 5 个（`enableReadOnly`、`disableReadOnly`、`isReadOnlyEnabled`、`enableMmap`、`flushOSBuffers`，外加 `tryCloseFile`）。能够说出「mmap、只读、主动 flush 都是可选能力，只有实现了的子类才真正生效」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `writeData`/`readData` 定义在 BinaryStream 而不是 DiskAdaptor 自己声明？

**答案**：读写是「字节流」的通用契约，与是否为磁盘布局无关。Piece 写入方（如 `Piece::writeData` 委托的存储层）和校验器只需要依赖最小接口 BinaryStream，不必耦合 DiskAdaptor 的文件管理方法——接口隔离原则的直接体现。

**练习 2**：`enableMmap()` 在基类里是空实现，这透露了什么设计信息？

**答案**：mmap 是可选能力，并非所有 DiskWriter 后端/平台都支持。调用方（如 `DefaultPieceStorage`）无须做类型判断即可安全调用；不支持时静默无效果，回退到常规 read/write 路径（u5-l1 讲过 mmap 失败自动降级）。

---

### 4.2 AbstractSingleDiskAdaptor 与 DirectDiskAdaptor：单文件的一对一映射

#### 4.2.1 概念说明

单文件任务（绝大多数 HTTP/FTP 下载、单文件种子）的映射关系是平凡的：**全局偏移 == 文件内偏移**。所以这一层几乎没有逻辑，就是把你对「布局」的调用原样转发给唯一的 DiskWriter。

那为什么要分两层？因为骨架类只知道「我有一个 DiskWriter、总长是多少」，却不知道**文件路径从哪来**——路径来源因场景而异，于是 `getFilePath()` 被声明为纯虚，交给具体的 `DirectDiskAdaptor` 实现：它从 `fileEntries_.front()`（第一个也是唯一的 FileEntry）取路径。「Direct」指的是「直接映射」，而不是「直连磁盘」。

#### 4.2.2 核心流程

```text
openFile() ──────────► diskWriter_->openFile(totalLength_)
openExistingFile() ──► diskWriter_->openExistingFile(totalLength_)
initAndOpenFile() ───► diskWriter_->initAndOpenFile(totalLength_)   （totalLength_ 作为期望长度传下去）
writeData(d,l,off) ──► diskWriter_->writeData(d,l,off)              （偏移原样透传，无需翻译）
readData(d,l,off) ───► diskWriter_->readData(d,l,off)
cutTrailingGarbage() ► 若磁盘文件 > totalLength_ 则 diskWriter_->truncate(totalLength_)
```

`totalLength_` 有两个消费点：作为三种打开方式的期望长度传给 DiskWriter（配合 u5-l1 讲过的截断/回退判定），以及作为 `cutTrailingGarbage` 的截断目标。

#### 4.2.3 源码精读

[AbstractSingleDiskAdaptor.h:L45-L49](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractSingleDiskAdaptor.h#L45-L49) 三个成员变量概括了这一层的全部状态：一个 `diskWriter_`、一个 `totalLength_`、一个只读标志。而 [AbstractSingleDiskAdaptor.h:L98-L98](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractSingleDiskAdaptor.h#L98-L98) 把 `getFilePath()` 留成纯虚——这就是「骨架与具体分离」的唯一原因。

透传逻辑一目了然：

[AbstractSingleDiskAdaptor.cc:L56-L77](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractSingleDiskAdaptor.cc#L56-L77) 三种打开方式都只做一件事：把 `totalLength_` 作为参数传给 DiskWriter 对应的打开方法；`writeData` 连参数都不改，原样转发。注意没有空指针保护——`setDiskWriter` 必须在任何 open 之前被调用（4.4 会看到调用顺序保证）。

预分配工厂方法按当前分配方式选择迭代器：

[AbstractSingleDiskAdaptor.cc:L123-L139](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractSingleDiskAdaptor.cc#L123-L139) 用一个 switch 把 `FILE_ALLOC_FALLOC`（fallocate 系统调用）、`FILE_ALLOC_TRUNC`（ftruncate）、默认的 `FILE_ALLOC_ADAPTIVE` 分别映射到三种 FileAllocationIterator——这正是 u5-l4 文件预分配讲的「三种分配迭代器」的诞生地。`HAVE_SOME_FALLOCATE` 宏由 configure 探测结果决定（u1-l2）。

截尾逻辑：

[AbstractSingleDiskAdaptor.cc:L155-L160](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractSingleDiskAdaptor.cc#L155-L160) 若磁盘上的实际文件比 `totalLength_` 长（例如续传时元数据变了），就把它截回期望长度。

最后是具体类的全部「个性」：

[DirectDiskAdaptor.cc:L41-L44](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DirectDiskAdaptor.cc#L41-L44) `getFilePath()` 返回 `getFileEntries().front()->getPath()`——从基类聚合的 FileEntry 列表里取第一个的路径。另外 [DirectDiskAdaptor.cc:L46-L55](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DirectDiskAdaptor.cc#L46-L55) 实现了 `utime`，仅对这一个文件设置时间戳，成功返回 1。

#### 4.2.4 代码实践

**实践目标**：用运行日志验证「单文件任务选用 DirectDiskAdaptor、多文件任务选用 MultiDiskAdaptor」这条分支。

**操作步骤**：

1. 确认已按 u1-l2 编译出 `./src/aria2c`。
2. 执行一次单文件 HTTP 下载并开 debug 日志：

   ```bash
   ./src/aria2c --log=- --log-level=debug -d /tmp/a2-select \
     https://example.com/index.html 2>&1 | grep -i "Instantiating"
   ```

3. 再用任意一个多文件种子（可用 4.3.4 的方法自制）执行同样命令并 grep。

**需要观察的现象**：日志里出现的分别是 `Instantiating DirectDiskAdaptor` 还是 `Instantiating MultiDiskAdaptor`。

**预期结果**：单文件任务打印 `Instantiating DirectDiskAdaptor`，多文件种子打印 `Instantiating MultiDiskAdaptor`——这两行日志分别产生于 `DefaultPieceStorage::initStorage` 的两个分支（见 4.4.3）。涉及真实网络与种子，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`AbstractSingleDiskAdaptor` 为什么不自己实现 `getFilePath()`（比如也从 `fileEntries_.front()` 取）？

**答案**：路径来源被设计为可替换点：`DirectDiskAdaptor` 从 FileEntry 取，但其他场景（测试替身、内存型任务）可自行提供路径；骨架类只封装「一个写入器 + 总长」的公共转发逻辑，把「路径怎么定」这一变化点留成纯虚。

**练习 2**：`totalLength_` 在哪些方法里被消费？分别起什么作用？

**答案**：三种 open 方法把它作为期望长度传给 DiskWriter（参与 u5-l1 的截断/创建/回退判定）；`cutTrailingGarbage` 用它作为截断目标；`fileAllocationIterator` 用它作为预分配的终点长度。

---

### 4.3 MultiDiskAdaptor：把一串文件拼成一个逻辑偏移空间

#### 4.3.1 概念说明

多文件种子才是这一层真正要解决的难题。BT 协议以 piece 为单位传输和校验，而 piece 边界由总长除以 piece 长度得到，**与文件边界没有任何对齐关系**。用测试文件里的真实布局举例（piece 长度为 2）：

```text
全局偏移:  0    2    4    6    8    10   12   14   16 …
           ++--++--++--++--++--++--++--++--
| file0 (0 字节, 偏移 0)
*************** file1 (15 字节, 偏移 0)
               ******* file2 (7 字节, 偏移 15)
                      | file3 (0 字节, 偏移 22)
                      ** file4 (2 字节, 偏移 22)
```

（该图直接来自 [MultiDiskAdaptorTest.cc:L52-L81](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/MultiDiskAdaptorTest.cc#L52-L81) 的 `createEntries`。）

此时会出现两类问题：

1. **写跨界**：一次 `writeData(data, 20, 10)` 要落在 file1 的后 5 字节、file2 的全部 7 字节、file4 的前 8 字节里——一个逻辑写请求被拆成三个物理写。
2. **piece 跨文件**（`--select-file` 的核心矛盾）：假设只选了 file2 下载，但覆盖偏移 14–16 的那个 piece 一半属于 file1、一半属于 file2。BT 按 piece 校验，必须把整个 piece 的数据都写盘，于是**未选中的 file1 也得有地方放它尾巴上那 1 个字节**。过滤位图（u5-l2）保证这个跨界 piece 会被下载；本讲的 MultiDiskAdaptor 则负责给 file1 这种「未选中但跨界」的文件准备好写入器。

#### 4.3.2 核心流程

MultiDiskAdaptor 的成员极简（见 [MultiDiskAdaptor.h:L98-L104](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiDiskAdaptor.h#L98-L104)）：piece 长度、`DiskWriterEntry` 数组、已打开条目表、只读标志。每个 `DiskWriterEntry`（[MultiDiskAdaptor.h:L46-L91](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiDiskAdaptor.h#L46-L91)）= 一个 FileEntry + 一个 DiskWriter + `open_`/`needsFileAllocation_`/`needsDiskWriter_` 三个标志。

**写入主流程**（`writeData`，伪代码）：

```text
writeData(data, len, offset):
    entry = findFirstDiskWriterEntry(offset)      # upper_bound 二分定位起始文件
    fileOffset = offset - entry.offset            # 换算为文件内偏移
    rem = len
    从 entry 起顺序遍历后续文件:
        writeLength = min(rem, entry.length - fileOffset)   # 截断到本文件末尾
        openIfNot(entry, openFile)                          # 懒打开
        entry.diskWriter.writeData(data + (len-rem), writeLength, fileOffset)
        rem -= writeLength
        fileOffset = 0                            # 下一个文件从 0 开始
        if rem == 0: break
```

**`--select-file` 下的扫描流程**（`resetDiskWriterEntries`）：

1. 为每个 FileEntry 建 `DiskWriterEntry`，`needsFileAllocation` 初值 = `isRequested()`。
2. **正向扫描**：维护 `lastOffset` = 「最近一个被选中文件的最后字节所在 piece 的右端点」，即

   \[ P_{end} = \left\lfloor \frac{L_{last}-1}{P} \right\rfloor \cdot P + P \]

   其中 \(L_{last}\) 为选中文件的全局终点、\(P\) 为 piece 长度。若未选中文件的起点 \(\lt P_{end}\)，说明它的**头部**落进了与前面选中文件共享的 piece → 置 `needsDiskWriter`（只写不分配）。
3. **反向扫描**：维护 `lastOffset` = 「最近一个被选中文件的起点所在 piece 的左端点」，即 \(P_{start} = \lfloor O_{first}/P \rfloor \cdot P\)。若未选中文件的尾部越过 \(P_{start}\)（或零长文件恰好落在边界上），说明它的**尾部**落在共享 piece 里 → 置 `needsFileAllocation`（要分配，源码注释指出「尤其对不支持稀疏文件的文件系统」）。
4. **创建写入器**：仅当 `needsFileAllocation || needsDiskWriter || fileExists()` 才为该文件 new 一个 DiskWriter。三个条件都不满足的未选中文件——**什么都没有，连文件都不会被创建**。

**打开文件管理**：`MultiDiskAdaptor::openFile` 会把所有**有写入器**的条目全部打开（保证零长文件被创建）；读写途中的 `openIfNot` 负责懒打开并把条目登记进 `openedDiskWriterEntries_`；`tryCloseFile` 从中随机挑几个关闭，配合 `OpenedFileCounter` 把全局打开文件数压在 [MultiDiskAdaptor.h:L113](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiDiskAdaptor.h#L113-L113) 定义的 `DEFAULT_MAX_OPEN_FILES = 100` 以内。

#### 4.3.3 源码精读

先看「未选中文件如何被跳过」的三道闸门。

第一道：条目打开动作有空指针保护——

[MultiDiskAdaptor.cc:L79-L93](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiDiskAdaptor.cc#L79-L93) 中 `DiskWriterEntry::openFile`/`openExistingFile` 都以 `if (diskWriter_)` 开头：没有写入器的条目调用 open 是空操作，`open_` 保持 false，文件不会被创建。这就是跳过的执行点。

第二道：正反两趟扫描判定跨界——

[MultiDiskAdaptor.cc:L142-L161](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiDiskAdaptor.cc#L142-L161) 正向扫描：遍历中遇到被选中的文件就把 `lastOffset` 推进到其尾字节所在 piece 的右端点（零长文件不影响 `lastOffset`）；遇到未选中且起点小于 `lastOffset` 的文件，打日志 `needs DiskWriter` 并置标志。源码注释点明：与「最后一 个 piece」共享的文件不需要分配，只需要 DiskWriter。

[MultiDiskAdaptor.cc:L163-L179](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiDiskAdaptor.cc#L163-L179) 反向扫描：从最后一个文件倒着走，遇到被选中文件把 `lastOffset` 设为其起点所在 piece 的左端点；未选中文件若尾部越过它（`lastOffset < getLastOffset()`）或是落在边界上的零长文件（`lastOffset <= getOffset()`），置 `needsFileAllocation`。源码注释：我们需要该文件的最后一部分，所以需要文件分配，尤其对不支持稀疏文件的文件系统。

第三道：写入器按需创建——

[MultiDiskAdaptor.cc:L181-L194](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiDiskAdaptor.cc#L181-L194) 只有 `needsFileAllocation() || needsDiskWriter() || fileExists()` 三者其一成立才通过 `DefaultDiskWriterFactory` 创建写入器，并打日志 `Creating DiskWriter for filename=...`。未选中、不跨界、磁盘上又不存在的文件被彻底跳过。

接着看跨文件写入的三个辅助函数与主流程：

[MultiDiskAdaptor.cc:L281-L325](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiDiskAdaptor.cc#L281-L325) 三个匿名命名空间的辅助函数：`isInRange`（判断全局偏移是否落在某条目区间 \([offset, lastOffset)\) 内）、`calculateLength`（把剩余字节数截断到本文件末尾）、`findFirstDiskWriterEntry`（用 `std::upper_bound` 按 `OffsetCompare` 二分定位起始条目，前移一位后做范围校验，越界抛 `EX_FILE_OFFSET_OUT_OF_RANGE`）。因为 `diskWriterEntries_` 与 FileEntry 列表同序，而 FileEntry 本身按 offset 升序排列，二分查找才成立。

[MultiDiskAdaptor.cc:L337-L358](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiDiskAdaptor.cc#L337-L358) `writeData` 主体：定位首条目 → 换算文件内偏移 → 循环内依次做「截长、懒打开、防御校验、写入、扣减 rem、fileOffset 归零」。注意 [MultiDiskAdaptor.cc:L328-L335](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiDiskAdaptor.cc#L328-L335) 的 `throwOnDiskWriterNotOpened` 是一道防御闸门：如果数据真的落到一个没有写入器的文件区间（正常流程下过滤位图已在上游挡住，不该发生），会抛出「DiskWriter for offset=... is not opened」直接暴露 bug 而不是静默丢数据。

[MultiDiskAdaptor.cc:L372-L411](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiDiskAdaptor.cc#L372-L411) `readData` 与 writeData 同构，但多两层处理：内层 `while` 循环应对单文件内的**短读**（`nread == 0` 即提前返回已读总量），以及 `dropCache` 分支（校验大文件时读一块丢一块缓存，见 u5-l5）。

再看打开与关闭的管理：

[MultiDiskAdaptor.cc:L213-L230](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiDiskAdaptor.cc#L213-L230) `openIfNot`：未打开则先经 `OpenedFileCounter::ensureMaxOpenFileLimit(1)` 确保全局打开数不超限，再执行传入的成员函数指针（openFile 或 initAndOpenFile），并登记进 `openedDiskWriterEntries_`；已打开则直接命中（源码中留有 Cache HIT/MISS 的注释日志）。

[MultiDiskAdaptor.cc:L232-L261](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiDiskAdaptor.cc#L232-L261) 三种打开方式都先 `resetDiskWriterEntries()` 重建全部条目；`openFile`/`initAndOpenFile` 会**立即**遍历打开所有条目——注释写明原因：保证零长文件被创建（`DiskWriter::openFile` 语义是不存在则创建）；`openExistingFile` 则一个都不开，完全留给后续读写懒打开。目录创建不在这里做——注释指出 `util::mkdir()` 在 `AbstractDiskWriter::createFile()` 中调用。

[MultiDiskAdaptor.cc:L197-L211](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiDiskAdaptor.cc#L197-L211) `tryCloseFile`：从 `openedDiskWriterEntries_` 里随机挑最多 `numClose` 个关闭——随机而非 LRU，实现极简。

最后两个收尾方法也体现「requested」的过滤：

[MultiDiskAdaptor.cc:L481-L493](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiDiskAdaptor.cc#L481-L493) `utime` 只对 `isRequested()` 的文件设置时间戳；[MultiDiskAdaptor.cc:L413-L420](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiDiskAdaptor.cc#L413-L420) `writeCache` 把写盘缓存（u5-l6）里暂存的各数据块按全局偏移逐块调 `writeData` 落盘。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：用 `--select-file` 下载多文件种子中的第 2 个文件，亲眼验证「未选中的邻居文件因共享 piece 被创建并写入少量跨界数据，其余未选中文件根本不落盘」。

**操作步骤**：

1. 准备一个多文件种子。任选其一：
   - 用 `mktorrent` 自制：建一个含 3–5 个小文件（每个约 100–300 KB）的目录，执行 `mktorrent -l 18 -o test.torrent ./mydir/`（`-l 18` 即 256 KB piece，保证多个文件共享同一 piece）；
   - 或使用任意公开的多文件种子。
2. 先完整下载一次作对照组：

   ```bash
   ./src/aria2c -d /tmp/bt-full test.torrent
   find /tmp/bt-full -type f -printf '%s %p\n' | sort -k2
   ```

3. 换目录做 `--select-file` 实验组，开 debug 日志：

   ```bash
   ./src/aria2c --select-file=2 -d /tmp/bt-sel --log=- --log-level=debug \
     test.torrent 2>&1 | tee bt-sel.log
   find /tmp/bt-sel -type f -printf '%s %p\n' | sort -k2
   ```

4. 从日志中检索三组关键行（它们分别来自 4.3.3 引用的三段代码）：

   ```bash
   grep -E "needs DiskWriter|needs file allocation|Creating DiskWriter" bt-sel.log
   ```

**需要观察的现象**：

- 与完整下载相比，实验组目录里大多数未选中文件**不存在**；
- 与第 2 个文件首尾相邻、共享 piece 的文件**存在**，且大小通常远小于一个 piece（只装了跨界 piece 里属于自己的那几个字节）；
- `bt-sel.log` 中能找到与这些邻居文件对应的 `needs DiskWriter`（头部跨界，前邻）或 `needs file allocation`（尾部跨界，后邻）日志行，以及每个真正创建的写入器的 `Creating DiskWriter for filename=...` 行；
- 零长文件即使未被选中也会被创建（`openFile` 全量打开的设计意图）。

**预期结果**：目录结构差异与日志行一一对应，能对照 [MultiDiskAdaptor.cc:L181-L194](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiDiskAdaptor.cc#L181-L194) 解释「每个文件为什么有/没有写入器」。涉及真实种子与 BT 网络，**待本地验证**（自制本地种子并配合做种端可离线验证）。

#### 4.3.5 小练习与答案

**练习 1**：布局为 file1\[0,15)、file2\[15,22)、file3\[22,29)，执行 `writeData(data, 20, 10)`，三个文件各写多少字节？文件内偏移各是多少？

**答案**：起始定位到 file1（10 落在 \[0,15)），fileOffset=10。file1：截断到末尾，写 5 字节（文件内偏移 10）；rem=15、fileOffset 归零。file2：写 7 字节（偏移 0）；rem=8。file3：写 8 字节（偏移 0）；rem=0 结束。对照 [MultiDiskAdaptorTest.cc:L288-L335](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/MultiDiskAdaptorTest.cc#L288-L335) 的 `testWriteData`，它用 9 文件布局断言了同样的切分行为，包括「写偏移 10 处 14 字节后 file2 恰好被写满 7 字节」。

**练习 2**：一个未被 `--select-file` 选中的文件，要满足什么条件才会得到 DiskWriter？

**答案**：三个条件任一成立（[MultiDiskAdaptor.cc:L182-L184](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiDiskAdaptor.cc#L182-L184)）：① 反向扫描判定其尾部与某个选中文件的**首 piece** 重叠（`needsFileAllocation`）；② 正向扫描判定其头部与某个选中文件的**尾 piece** 重叠（`needsDiskWriter`）；③ 该文件在磁盘上已存在（已存在的文件需要写入器来执行截断等操作）。

**练习 3**：`MultiDiskAdaptor::openFile` 为什么把所有条目（包括未选中的）都立即打开一遍，而不是等写入时懒打开？

**答案**：源码注释（[MultiDiskAdaptor.cc:L238-L242](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiDiskAdaptor.cc#L238-L242)）写明：调用 `DiskWriterEntry::openFile` 是为了**保证零长文件被创建**——种子里常有 0 字节文件，没有写入器的条目这一步是空操作，有写入器但内容为空的文件则借此真实落盘。懒打开只服务有数据的区间，零长文件永远等不到写请求。

---

### 4.4 适配器的选择与装配：initStorage 与打开文件数控制

#### 4.4.1 概念说明

最后一个问题：谁决定用哪个适配器？答案是 PieceStorage 的初始化函数。u5-l2 讲过 `DefaultPieceStorage` 按 piece 管账；它在 `initStorage()` 里顺带完成「选择并装配 DiskAdaptor」。判据非常朴素：**FileEntry 条目数是否为 1**——注意判据是「文件条目数」而不是「是否 BT」：单文件种子同样走 DirectDiskAdaptor，而多文件 HTTP 下载在 aria2 的任务模型里根本不存在（每个文件各自一个 RequestGroup）。

本模块还要补上 `--select-file` 的完整传导链，以及多任务并发时的打开文件数控制。

#### 4.4.2 核心流程

```text
命令行 --select-file=2
  └─ download_helper: parseIntSegments(PREF_SELECT_FILE) → SegList
       └─ DownloadContext::setFileFilter: 逐 FileEntry 设置 requested 标志（单文件/未指定则全部 true）
            └─ RequestGroup::initPieceStorage → DefaultPieceStorage::initStorage
                 ├─ 文件数 == 1 → DirectDiskAdaptor（+ setTotalLength + setDiskWriter）
                 └─ 文件数  > 1 → MultiDiskAdaptor（+ setFileEntries + setPieceLength）
            └─ 挂接 OpenedFileCounter（来自 RequestGroupMan，全局共享）

写入路径触发时：MultiDiskAdaptor::resetDiskWriterEntries 读取每个 FileEntry 的
requested 标志做正反两趟跨界扫描（4.3），决定谁有写入器。
```

#### 4.4.3 源码精读

选择逻辑的全部代码只有二十几行：

[DefaultPieceStorage.cc:L634-L660](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L634-L660) `initStorage()`：文件条目数为 1 时创建 `DirectDiskAdaptor`，依次 `setTotalLength` → `setFileEntries` → `setDiskWriter(diskWriterFactory_->newDiskWriter(getFilePath()))`——注意**必须先 setFileEntries 再 setDiskWriter**，因为 `DirectDiskAdaptor::getFilePath()` 要解引用 `fileEntries_.front()`；否则走 `MultiDiskAdaptor` 分支，`setFileEntries` 之外还要 `setPieceLength`（跨界扫描依赖它）。最后按 `--file-allocation` 选项设置 `FILE_ALLOC_FALLOC`/`FILE_ALLOC_TRUNC`。

调用点与计数器挂接：

[RequestGroup.cc:L613-L617](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L613-L617) 任务初始化时调用 `initStorage()`，随后把 `RequestGroupMan` 上的全局 `OpenedFileCounter` 挂给刚建好的适配器——这就是多任务共享同一个「打开文件预算」的接缝。上文 L606–L611 的 else 分支显示长度未知的流式下载改用 `UnknownLengthPieceStorage`（u5-l2），不走这套选择。

`--select-file` 的传导链两端：

[download_helper.cc:L212-L214](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc#L212-L214) 创建 BT 任务时把 `PREF_SELECT_FILE` 的值解析成整数段列表交给 `setFileFilter`。[DownloadContext.cc:L136-L161](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadContext.cc#L136-L161) `setFileFilter` 的实现：没有过滤条件或单文件任务时**全部置 true**；否则按下标精确匹配，选中置 true、其余置 false。而 [FileEntry.h:L126-L134](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FileEntry.h#L126-L134) 就是消费端读到的 `getOffset()`/`getLastOffset()`（= \( offset + length \)）/`isRequested()` 三个访问器——MultiDiskAdaptor 的两趟扫描全部建立在这三个值上。

打开文件数的闭环：

[OpenedFileCounter.cc:L53-L99](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OpenedFileCounter.cc#L53-L99) `ensureMaxOpenFileLimit`：打开新文件前检查「已打开数 + 新增数」是否超限；超了就从随机位置起环形遍历所有 RequestGroup，调用各自 DiskAdaptor 的 `tryCloseFile` 收回名额。结合 4.3.3 的 `openIfNot`（打开前必经此函数）与 `MultiDiskAdaptor::tryCloseFile`（随机关闭），一个「全局预算 + 随机回收」的轻量方案就闭环了——避免上千文件的种子把进程的文件描述符耗尽。

#### 4.4.4 代码实践

**实践目标**：用现成单元测试验证 4.3 的跨界扫描结论，而不必真的跑 BT 下载。

**操作步骤**：

1. 阅读 [MultiDiskAdaptorTest.cc:L83-L138](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/MultiDiskAdaptorTest.cc#L83-L138) 的 `testResetDiskWriterEntries` 前两组用例（piece 长度在 [setUp](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/MultiDiskAdaptorTest.cc#L35-L39) 中设为 2）。
2. 拿出纸笔，对照 L52–L81 的 9 文件布局图，自己推演：若 `fileEntries[0]->setRequested(false)`（只有 file0 不选），哪些条目会拿到 DiskWriter？
3. 再推演 file0、file1 同时不选时，file1 为什么仍要写入器且 `needsFileAllocation`？
4. 运行整个测试套件验证（构建方法见 u1-l2，测试可执行文件为 `test/aria2c`，由 `AllTest.cc` 驱动全部 CppUnit 套件）：

   ```bash
   make check
   ```

**需要观察的现象**：你的手推结果与 `CPPUNIT_ASSERT` 断言是否一致；特别留意断言旁的注释（如 `// Because entries[1] spans entries[0]`）。

**预期结果**：第一组里 file0 不选时，由于 file1（偏移 0、长 15）的头部 piece 覆盖了 file0 的位置，file0 依然拿到写入器；第二组里 file0、file1 都不选时 `entries[0]` 无写入器，但 file1 因与 file2 共享尾部 piece 而 `needsFileAllocation` 为真。测试通过即证明 4.3 的两趟扫描模型正确。

#### 4.4.5 小练习与答案

**练习 1**：`initStorage` 用什么判据选择适配器？为什么单文件种子也会用 DirectDiskAdaptor？

**答案**：判据是 `downloadContext_->getFileEntries().size() == 1`。单文件种子的逻辑偏移空间就是那一个文件，全局偏移与文件内偏移天然相等，不存在切分问题，直通层足够；piece 机制照样生效，只是不需要跨文件翻译。

**练习 2**：aria2 同时下载两个各含 500 个文件的种子，为什么不会打开 1000 个文件描述符？

**答案**：每个 MultiDiskAdaptor 在 `openIfNot` 里打开文件前都会调用共享的 `OpenedFileCounter::ensureMaxOpenFileLimit(1)`，超限（默认预算 100，见 `DEFAULT_MAX_OPEN_FILES`）就从随机 RequestGroup 起环形遍历、调用 `tryCloseFile` 随机关闭已打开文件收回名额，新写入发生时会再被懒打开。

**练习 3**：把 `--select-file=2` 从命令行传到 `resetDiskWriterEntries`，中间经过哪几站？

**答案**：`option` 的 `PREF_SELECT_FILE` → `util::parseIntSegments` 解析为 SegList → `DownloadContext::setFileFilter` 逐 FileEntry 设置 `requested_` 标志（单文件或未指定时全部置 true）→ MultiDiskAdaptor 聚合同一批 FileEntry，`resetDiskWriterEntries` 中 `createDiskWriterEntry` 与正反两趟扫描读取 `isRequested()`。

---

## 5. 综合实践

**任务：手推一个迷你多文件布局的全过程。**

不用电脑（最后可选验证），按下表构造一个三文件任务：

| 文件 | 偏移 | 长度 |
| --- | --- | --- |
| fileA | 0 | 10 |
| fileB | 10 | 10 |
| fileC | 20 | 10 |

piece 长度 \(P = 8\)。请依次完成：

1. **画 piece 边界**：写出全部 piece 的全局区间（应为 \[0,8)、\[8,16)、\[16,24)、\[24,30)），并标出每个 piece 覆盖了哪些文件。
2. **推演 `--select-file=2`（仅 fileB 选中）**：
   - 正向扫描：\(P_{end} = \lfloor (20-1)/8 \rfloor \cdot 8 + 8 = 24\)，fileC 起点 20 < 24 → `needsDiskWriter`；
   - 反向扫描：\(P_{start} = \lfloor 10/8 \rfloor \cdot 8 = 8\)，fileA 的终点 10 > 8 → `needsFileAllocation`；
   - 结论：fileA 被分配（其 8–10 字节落在与 fileB 共享的 piece \[8,16) 内）、fileC 只拿写入器（其 20–24 字节落在 piece \[16,24) 内）。请核对手算与公式一致。
3. **推演一次跨界写**：执行 `writeData(data, 20, 5)`，给出每个文件收到的字节数与文件内偏移（应为 fileA 写 5 字节 @5、fileB 写 10 字节 @0、fileC 写 5 字节 @0）。
4. **（可选，需本地构建）** 仿照 `test/MultiDiskAdaptorTest.cc` 的 `testWriteData`，把上面的布局写成一段独立的小测试程序（用 `FileEntry(path, length, offset)` 构造、`MultiDiskAdaptor::setPieceLength(8)`、断言三个文件落盘内容），编译运行验证前三问的答案。**待本地验证**。

这个任务把本讲三条主线——逻辑偏移空间、piece 跨界、跨文件切写——全部串了一遍。

## 6. 本讲小结

- DiskAdaptor 是 PieceStorage/DiskWriter 之间的「变形缝」：向上以 BinaryStream 契约暴露一个连续的逻辑偏移空间，向下把全局偏移翻译成单文件直通或跨文件切分。
- 单文件任务走 `AbstractSingleDiskAdaptor`（骨架：一个 DiskWriter + totalLength_）+ `DirectDiskAdaptor`（提供 getFilePath），读写是零翻译的直通；预分配迭代器的三分支 switch 也在这里。
- MultiDiskAdaptor 用 `DiskWriterEntry` 数组承载多文件布局：写入时二分定位首文件、按文件尾截长、懒打开、跨文件循环，直到剩余字节耗尽。
- piece 边界与文件边界不对齐是 `--select-file` 的核心矛盾：正反两趟扫描让「未选中但与选中文件共享首/尾 piece」的邻居获得写入器（必要时还分配），其余未选中文件连磁盘文件都不会创建；零长文件例外地总会被创建。
- 适配器的选择发生在 `DefaultPieceStorage::initStorage`，判据是 FileEntry 条目数是否为 1；`--select-file` 经 `setFileFilter` 落为每个 FileEntry 的 `requested` 标志，再被两趟扫描消费。
- 打开文件数由全局共享的 `OpenedFileCounter` 控制：打开前查预算，超限则随机遍历各任务的 `tryCloseFile` 回收，避免大种子耗尽文件描述符。

## 7. 下一步学习建议

本讲补完了「piece → 磁盘」的最后一环，建议按顺序继续：

1. **u5-l4 文件预分配**：本讲两次出现的 `fileAllocationIterator` 与 `FILE_ALLOC_FALLOC/TRUNC/ADAPTIVE` 三种方法，将在下一讲展开为独立的命令调度阶段，MultiDiskAdaptor 对应的 `MultiFileAllocationIterator` 会按「仅分配 requested 文件」的策略工作。
2. **u5-l5 校验与哈希**：校验器通过 `readDataDropCache` 按全局偏移逐块读回数据——你会看到本讲的 readData 切分逻辑在真实链路上的另一个消费者。
3. **u5-l6 写盘缓存**：`writeCache(const WrDiskCacheEntry*)` 如何把内存中攒好的散块按 goff 批量灌进本讲的 writeData。
4. 想巩固本讲内容，可重读 [MultiDiskAdaptorTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/MultiDiskAdaptorTest.cc#L83-L271) 的 `testResetDiskWriterEntries` 全部 6 组断言——它是两趟跨界扫描最精确的规格说明。
