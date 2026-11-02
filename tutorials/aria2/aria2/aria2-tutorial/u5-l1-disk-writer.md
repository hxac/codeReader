# u5-l1 DiskWriter 抽象：文件写入的统一接口

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `DiskWriter` 接口的全部方法，并解释「定位写」`writeData(data, len, offset)` 与「截断新建」`initAndOpenFile()` 两组接口各自适用的场景。
2. 精读 `AbstractDiskWriter` 的实现：文件描述符的生命周期、三种打开方式的语义差异、部分写入（partial write）的处理、磁盘满与普通 I/O 错误的分级，以及 mmap 直写路径。
3. 理解 `DiskWriterFactory` 体系如何按任务类型选择写入器：磁盘文件用 `DefaultDiskWriter`，magnet 元数据、`--follow-torrent=mem`、RPC 报文等场景用内存型特例（`ByteArrayDiskWriter`、`BencodeDiskWriter`、`XmlRpcDiskWriter`）。
4. 对照 `RequestGroup` 的源码，说清 `--allow-overwrite` 与 `--auto-file-renaming` 两个选项分别在「创建写入器之前」和「打开写入器之时」由哪些代码路径生效，并亲手验证重名文件时 aria2 的三种行为（覆盖、改名、报错退出）。

## 2. 前置知识

本讲是磁盘 I/O 单元（单元 5）的第一讲，往下扎根到「字节最终怎么落到文件」这一层。阅读前请确认理解以下概念（都在前面讲义出现过，这里只做一句话回顾）：

- **piece 与 segment**（u3-l4）：aria2 把文件切成 piece（HTTP 默认 1M），每条连接认领一个 segment 往里写数据。多条连接**同时**写同一个文件的不同区间，所以底层写入接口必须支持「指定偏移写」，而不能只支持顺序追加。
- **PieceStorage 与 DiskAdaptor**（u3-l1）：`RequestGroup` 是任务容器，`PieceStorage` 以 piece 为粒度管理下载状态，真正落盘时把逻辑偏移交给 `DiskAdaptor`（单文件或多文件布局，下一讲 u5-l3 详述），`DiskAdaptor` 再把数据转交给本讲的主角 `DiskWriter`。
- **RAII**（u1-l3）：资源获取即初始化，对象析构时自动释放资源。`AbstractDiskWriter` 的析构函数会兜底关闭文件。
- **fd（文件描述符）**：POSIX 系统里进程打开文件后拿到的整数句柄，`open/write/read/close/lseek` 都围绕它进行。
- **错误码体系**（u1-l3）：aria2 用 `error_code::Value` 枚举给每种失败一个编号，例如 `FILE_ALREADY_EXISTS = 13`、`NOT_ENOUGH_DISK_SPACE = 9`，它们就是命令行退出码。

一个帮助建立直觉的问题：**为什么需要 DiskWriter 这一层抽象，而不直接在下载命令里调 `write()`？** 因为「写入目标」并不总是磁盘文件——magnet 链接要先从 peer 那里下载种子元数据（一段 bencode 字节流），它的去处是内存里的解析器而不是硬盘；RPC 服务器收到的 XML 请求体 likewise 不落盘。把「接收一段带偏移的字节」抽象成接口，磁盘、内存、解析器就能共用同一套下载流水线。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/BinaryStream.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BinaryStream.h) | 最底层抽象：可按偏移读写的字节流（4 个纯虚/默认方法） |
| [src/DiskWriter.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DiskWriter.h) | 在 BinaryStream 之上追加「文件生命周期」契约的接口 |
| [src/AbstractDiskWriter.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractDiskWriter.h) / [.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractDiskWriter.cc) | 骨架实现：持有 fd 与 mmap 状态，实现除 `initAndOpenFile` 外的所有方法 |
| [src/DefaultDiskWriter.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultDiskWriter.h) / [.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultDiskWriter.cc) | 具体写入器：真磁盘文件，只补一个 `initAndOpenFile` |
| [src/DiskWriterFactory.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DiskWriterFactory.h) | 工厂接口：`newDiskWriter(filename)` |
| [src/DefaultDiskWriterFactory.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultDiskWriterFactory.h) / [.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultDiskWriterFactory.cc) | 默认工厂：产出 `DefaultDiskWriter` |
| [src/AnonDiskWriterFactory.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AnonDiskWriterFactory.h) | 模板工厂：忽略文件名，产出无参构造的内存型写入器 |
| [src/ByteArrayDiskWriter.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ByteArrayDiskWriter.h) / [.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ByteArrayDiskWriter.cc) | 内存型写入器：`std::stringstream` 当文件 |
| [src/ValueBaseDiskWriter.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ValueBaseDiskWriter.h) / [src/BencodeDiskWriter.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BencodeDiskWriter.h) | 内存型特例：写入的字节直接喂给 bencode/JSON 解析器 |
| [src/RequestGroup.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc) | 使用方：决定文件名策略（覆盖/改名/报错）、创建并打开写入器 |
| [src/DefaultPieceStorage.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc) / [src/MultiDiskAdaptor.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiDiskAdaptor.cc) | 使用方：调用工厂创建写入器并挂到 DiskAdaptor 上 |
| [src/download_helper.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc) / [src/MemoryPreDownloadHandler.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MemoryPreDownloadHandler.h) | 使用方：magnet / follow-torrent=mem 场景注入内存工厂 |
| [test/DefaultDiskWriterTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DefaultDiskWriterTest.cc) / [test/ByteArrayDiskWriterTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/ByteArrayDiskWriterTest.cc) | 单元测试：最小调用示例 |

## 4. 核心概念与源码讲解

### 4.1 DiskWriter 接口：从 BinaryStream 到文件写入契约

#### 4.1.1 概念说明

aria2 把「可写字节流」拆成两层接口：

- **`BinaryStream`** 只回答一个问题：「给我一个偏移量，我能读/写一段字节」。它有四个方法：`writeData`（定位写）、`readData`（定位读）、`truncate`（截断到指定长度，默认空实现）、`allocate`（预分配空间，默认空实现）。这个抽象足够通用，`DiskAdaptor`、`DiskWriter` 都实现了它，所以上层代码可以隔着 `BinaryStream*` 无差别地对待「整个多文件布局」和「单个磁盘文件」。
- **`DiskWriter`** 继承 `BinaryStream`，追加「文件生命周期」的契约：打开（三种方式）、关闭、查大小，以及一组**可选能力开关**（只读模式、mmap、丢弃缓存、强制刷盘），可选能力的默认实现是「什么都不做」——子类按需覆盖，调用方不必关心对方是否支持。

为什么打开文件要分三个方法？因为「目标文件已存在/不存在」时 aria2 想做的事完全不同：

| 方法 | 文件不存在时 | 文件已存在时 | 典型调用场景 |
| --- | --- | --- | --- |
| `initAndOpenFile(totalLength)` | 创建 | **截断为 0**（重新下载） | 全新下载、`--allow-overwrite=true` 覆盖 |
| `openExistingFile(totalLength)` | 抛 `FILE_OPEN_ERROR`(15) | 读写（或只读）打开 | 断点续传、`-c`、校验已有文件 |
| `openFile(totalLength)` | 转调 `initAndOpenFile` | 读写打开 | 不确定文件是否存在时的通用入口 |

`totalLength` 参数在这两个文件里只是透传给子类（aria2 当前实现基本忽略它），真正的长度处理交给文件预分配子系统（u5-l4）。

#### 4.1.2 核心流程

一次下载中写入器的使用时序（伪代码）：

```text
RequestGroup::createInitialCommand
  ├─ adjustFilename()                    # 先裁定文件名（改名/报错/放行覆盖）
  ├─ initPieceStorage()
  │    └─ DiskWriterFactory::newDiskWriter(path)   # 再用最终路径创建写入器
  └─ createCheckIntegrityEntry()
       └─ loadAndOpenFile()
            ├─ 有 .aria2 控制文件 ──────→ writer->openExistingFile()   # 续传
            ├─ -c 且文件 ≤ 总长 ────────→ writer->openExistingFile()
            ├─ --check-integrity 开启 ──→ writer->openExistingFile()
            └─ 其他 ───────────────────→ writer->initAndOpenFile()     # 截断新建

下载过程中（每个 piece 写盘）:
  Piece::writeData → DiskAdaptor::writeData → DiskWriter::writeData(data, len, offset)

下载结束/任务挂起:
  writer->closeFile()（或写入器析构时兜底关闭）
```

关键点：**策略（用什么名字、覆盖还是续传）全部在创建/打开写入器之前决定**，`DiskWriter` 本身不做任何策略判断，它只是忠实的执行者。

#### 4.1.3 源码精读

先看两个接口的完整定义。[BinaryStream.h:L44-L61](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BinaryStream.h#L44-L61) 定义了按偏移读写的四个方法，其中 `truncate` 和 `allocate` 给了空的默认实现，让「不支持截断/预分配」的实现（比如内存缓冲区）不必被迫写空函数：

```cpp
class BinaryStream {
public:
  virtual ~BinaryStream() = default;
  virtual void writeData(const unsigned char* data, size_t len,
                         int64_t offset) = 0;
  virtual ssize_t readData(unsigned char* data, size_t len, int64_t offset) = 0;
  virtual void truncate(int64_t length) {}
  virtual void allocate(int64_t offset, int64_t length, bool sparse) {}
};
```

注意 `writeData` 的第三个参数 `offset` 是**逻辑文件偏移**而非当前位置——这是多连接分段下载能并发写同一文件的前提。

[DiskWriter.h:L46-L91](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DiskWriter.h#L46-L91) 在此之上追加文件生命周期契约：

```cpp
class DiskWriter : public BinaryStream {
public:
  // 文件存在则截断为 0，然后打开
  virtual void initAndOpenFile(int64_t totalLength = 0) = 0;
  virtual void openFile(int64_t totalLength = 0) = 0;
  // TODO we have to examine the return value of close()
  virtual void closeFile() = 0;
  // 文件不存在则可能抛异常
  virtual void openExistingFile(int64_t totalLength = 0) = 0;
  virtual int64_t size() = 0;
  // 以下为可选能力，默认实现为空
  virtual void enableReadOnly() {}
  virtual void disableReadOnly() {}
  virtual void enableMmap() {}
  virtual void dropCache(int64_t len, int64_t offset) {}
  virtual void flushOSBuffers() {}
};
```

三个打开方法的语义在注释里写得明确：`initAndOpenFile` 是「已存在则截断到 0」，`openExistingFile` 是「不存在则抛异常」，`openFile` 的折中行为在 `AbstractDiskWriter` 里实现（见 4.2.3）。`closeFile` 上方的 TODO 注释（[DiskWriter.h:L61](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DiskWriter.h#L61)）坦承关闭失败目前被忽略——这是阅读真实项目源码时常见的「已知债务」标记。

可选能力开关的设计值得留意：`enableReadOnly()` 之后 `openExistingFile()` 会改用只读打开，这让 aria2 可以对「已经下完只做种」的文件用只读介质（如只读挂载）做种；`enableMmap()` 允许上层（`--enable-mmap` 选项，见 [prefs.cc:L348](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/prefs.cc#L348)）请求 mmap 直写路径；`dropCache` 实现为 `posix_fadvise(POSIX_FADV_DONTNEED)`，由 DiskAdaptor 的读路径按需调用（如 [MultiDiskAdaptor.cc:L396-L397](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiDiskAdaptor.cc#L396-L397)），读完即丢弃页缓存、避免污染系统 cache。

#### 4.1.4 代码实践

**实践目标**：不用运行任何程序，仅凭接口定义画出「写入器能力矩阵」，训练从接口读设计的功夫。

1. 打开 `src/BinaryStream.h` 与 `src/DiskWriter.h`，列出两个接口的全部方法。
2. 对每个方法回答三问：它是纯虚、带默认实现的虚函数还是非虚函数？它属于「读写数据」还是「生命周期/能力开关」？哪个可选能力会改变它的行为？
3. 在 `src/` 下执行 `grep -rn "enableMmap()" src/ --include=*.cc | head`（或用编辑器全局搜索 `enableMmap`），找到是谁在什么条件下调用了它。
4. **需要观察的现象**：`enableMmap` 的调用点应能追到选项处理（`--enable-mmap`）与 `DirectDiskAdaptor`。
5. **预期结果**：你得到一张 12 行左右的方法清单表，并能指出其中 5 个是可空实现的「软能力」。调用链的具体位置若与预期不符，以实际搜索结果为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `BinaryStream::writeData` 必须带 `offset` 参数，而很多网络库的写接口是顺序追加？

**答案**：aria2 的多连接/多源下载里，每条连接负责文件的不同 segment（u3-l4），各连接以任意顺序、任意交错到达数据。写入接口必须支持「写到逻辑偏移 X」才能让 -s16 的 16 条连接同时写同一文件互不干扰。`ByteArrayDiskWriter` 甚至要靠填充 `'\0'` 来伪造「空洞」（见 4.3.3），正是因为它收到的偏移可能跳着来。

**练习 2**：`initAndOpenFile` 和 `openExistingFile` 的语义差在哪？`openFile` 为什么单独存在？

**答案**：`initAndOpenFile` 是破坏性的——已存在的文件会被截断为 0，从头重下；`openExistingFile` 是非破坏性的——只打开已有文件用于续传/校验，文件不存在则抛 `FILE_OPEN_ERROR`(15)。`openFile` 是「不确定文件是否存在」时的折中入口：先按 `openExistingFile` 试，捕获 `ENOENT` 后退回 `initAndOpenFile`（实现在 `AbstractDiskWriter::openFile`，见 4.2.3）。BT 任务的 `pieceStorage_->getDiskAdaptor()->openFile()` 走的就是这条折中路。

**练习 3**：接口注释里的 `// TODO we have to examine the return value of close()` 说明了什么设计权衡？

**答案**：`closeFile()` 被声明为 `void`，关闭文件时操作系统层面可能报错（如 NFS 上 flush 失败），但接口无法向上传达。aria2 选择在下载主流程已经结束、错误已无法补救的时点静默忽略关闭错误，只保证不泄漏 fd（析构函数兜底）。这属于「失败已不可恢复时优先保证资源释放」的典型工程取舍。

### 4.2 AbstractDiskWriter：fd 生命周期与定位写实现

#### 4.2.1 概念说明

`AbstractDiskWriter` 是模板方法模式的骨架实现：它持有真正的状态——文件名 `filename_`、文件描述符 `fd_`（POSIX 下是 `int`，MinGW 下是 `HANDLE`，用条件编译统一）、mmap 相关的 `mapaddr_/maplen_`——并实现了接口里除 `initAndOpenFile` 之外的**全部**方法。子类（如 `DefaultDiskWriter`）只需补上「如何新建文件」这一个方法，其余打开、读写、截断、预分配、关闭逻辑全部复用。

为什么 `initAndOpenFile` 留给子类？看 [AbstractDiskWriter.h:L69-L70](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractDiskWriter.h#L69-L70)，骨架把 `createFile(int addFlags)` 放在 `protected` 区——「创建新文件」可能涉及额外动作（比如加密文件系统要先建容器），不同写入器创建标志不同，所以这一步开放给子类定制，`DefaultDiskWriter` 的实现只有一行。

这一层要重点理解三件事：

1. **部分写入（partial write）是常态**：`write(fd, buf, len)` 完全可能只写出一部分（信号中断、内核缓冲满），必须循环写满为止。
2. **错误分级决定下载命运**：磁盘满（`ENOSPC`）抛 `DownloadFailureException`（错误码 9）**立即终止整个下载**；其他 I/O 错误抛 `RecoverableException`（错误码 17），走可重试出口（u2-l3 的三级异常出口）。
3. **mmap 是透明加速路径**：`--enable-mmap` 开启后，首次写入时把整个文件映射进内存，此后 `writeData` 变成一次 `memcpy`，`seek`+`write` 系统调用完全消失；映射失败会自动降级回 fd 路径，绝不报错。

#### 4.2.2 核心流程

**打开与关闭的状态迁移**：

```text
初始: fd_ = A2_BAD_FD
  │
  ├─ openExistingFile() ──→ openFileWithFlags(O_RDWR 或 O_RDONLY)
  │                          失败 → 抛 FILE_OPEN_ERROR(15)
  ├─ createFile() ────────→ mkdirs(父目录) + openFileWithFlags(O_CREAT|O_RDWR|O_TRUNC)
  │                          失败 → 抛 FILE_CREATE_ERROR(16)
  └─ openFile() ──┬─ 先 openExistingFile()
                 └─ 捕获 ENOENT → createFile()
关闭: closeFile() ──→ 先 munmap(若有映射) → close(fd_) → fd_ = A2_BAD_FD
兜底: ~AbstractDiskWriter() 析构函数调用 closeFile()
```

**一次 writeData 的执行路径**：

```text
writeData(data, len, offset)
  ├─ ensureMmapWrite(len, offset)     # mmap 模式：懒加载/扩容/降级
  ├─ writeDataInternal(data, len, offset)
  │    ├─ mmap 可用 → std::copy_n(data, len, mapaddr_ + offset)，返回
  │    └─ 否则 → seek(offset)
  │              循环 write(fd, ...) 直到写满（EINTR 重试、部分写入继续）
  └─ 返回值 < 0？
       ├─ ENOSPC → DOWNLOAD_FAILURE_EXCEPTION3(NOT_ENOUGH_DISK_SPACE=9)  # 整组停机
       └─ 其他   → DL_ABORT_EX3(FILE_IO_ERROR=17)                       # 可重试出口
```

#### 4.2.3 源码精读

**构造与析构**。[AbstractDiskWriter.cc:L59-L74](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractDiskWriter.cc#L59-L74) 初始化 fd 为 `A2_BAD_FD`（哨兵值，表示「未打开」），析构函数调用 `closeFile()`——这是 RAII 的标准写法：即使上层忘记关闭，写入器对象销毁时 fd 也不会泄漏。

```cpp
AbstractDiskWriter::AbstractDiskWriter(const std::string& filename)
    : filename_(filename), fd_(A2_BAD_FD), /* ... */ readOnly_(false),
      enableMmap_(false), mapaddr_(nullptr), maplen_(0) {}
AbstractDiskWriter::~AbstractDiskWriter() { closeFile(); }
```

**折中的 openFile**。[AbstractDiskWriter.cc:L109-L129](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractDiskWriter.cc#L109-L129) 先尝试打开已有文件，只在「文件不存在」（POSIX 的 `ENOENT`、Windows 的 `ERROR_FILE_NOT_FOUND`/`ERROR_PATH_NOT_FOUND`）这一个错误上退回新建，其他错误（如权限不足）原样上抛：

```cpp
void AbstractDiskWriter::openFile(int64_t totalLength) {
  try {
    openExistingFile(totalLength);
  }
  catch (RecoverableException& e) {
    if (/* e.getErrNum() == ENOENT (平台相关) */) {
      initAndOpenFile(totalLength);
    }
    else { throw; }
  }
}
```

**平台无关的 open 封装**。[AbstractDiskWriter.cc:L212-L233](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractDiskWriter.cc#L212-L233)（POSIX 分支）用 `a2open` 打开（`a2io.h` 里的宽字符适配宏），处理 `EINTR` 重试、把 fd 设为 `close-on-exec`（防止 aria2 未来 exec 子进程时泄漏 fd），macOS 上还额外设置 `F_NOCACHE` 减少页缓存占用；打开失败统一抛出带文件名和 `strerror` 的异常：

```cpp
int openFileWithFlags(const std::string& filename, int flags,
                      error_code::Value errCode) {
  int fd;
  while ((fd = a2open(utf8ToWChar(filename).c_str(), flags, OPEN_MODE)) == -1 &&
         errno == EINTR)
    ;
  if (fd < 0) {
    int errNum = errno;
    throw DL_ABORT_EX3(errNum,
        fmt(EX_FILE_OPEN, filename.c_str(), util::safeStrerror(errNum).c_str()),
        errCode);
  }
  util::make_fd_cloexec(fd);
  return fd;
}
```

**三种打开方式**。[AbstractDiskWriter.cc:L236-L255](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractDiskWriter.cc#L236-L255)：`openExistingFile` 按 `readOnly_` 标志选 `O_RDONLY` 或 `O_RDWR`；`createFile` 先用 `util::mkdirs` 递归创建父目录（所以 `-d a/b/c` 不需要目录预先存在），再以 `O_CREAT | O_RDWR | O_TRUNC` 打开——`O_TRUNC` 就是「覆盖已有文件」的真正执行者：

```cpp
void AbstractDiskWriter::createFile(int addFlags) {
  assert(!filename_.empty());
  util::mkdirs(File(filename_).getDirname());
  fd_ = openFileWithFlags(filename_,
                          O_CREAT | O_RDWR | O_TRUNC | O_BINARY | addFlags,
                          error_code::FILE_CREATE_ERROR);
}
```

**定位写核心**。[AbstractDiskWriter.cc:L257-L291](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractDiskWriter.cc#L257-L291) 的 `writeDataInternal` 有两条路：mmap 命中时一次 `std::copy_n` 拷进映射区（零系统调用）；否则 `seek(offset)` 后**循环** `write` 直到写满，内层还要再防一层 `EINTR`：

```cpp
ssize_t AbstractDiskWriter::writeDataInternal(const unsigned char* data,
                                              size_t len, int64_t offset) {
  if (mapaddr_) {
    std::copy_n(data, len, mapaddr_ + offset);
    return len;
  }
  else {
    ssize_t writtenLength = 0;
    seek(offset);
    while ((size_t)writtenLength < len) {
      ssize_t ret = 0;
      while ((ret = write(fd_, data + writtenLength, len - writtenLength)) ==
                 -1 &&
             errno == EINTR)
        ;
      if (ret == -1) return -1;
      writtenLength += ret;      // 部分写入：继续写剩余部分
    }
    return writtenLength;
  }
}
```

`seek`（[AbstractDiskWriter.cc:L323-L339](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractDiskWriter.cc#L323-L339)）就是 `lseek(fd, offset, SEEK_SET)`，失败抛 `FILE_IO_ERROR`。注意每次写入都先 seek——写入器不维护「当前位置」状态，天然线程语义清晰（aria2 单线程事件循环内也避免了隐式游标错乱）。

**错误分级**。[AbstractDiskWriter.cc:L444-L465](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractDiskWriter.cc#L444-L465) 的 `writeData` 是对外入口，先给 mmap 一次机会，再按错误类型分流：磁盘满抛 `DownloadFailureException`（对照 u2-l3：这属于「整组停机」级异常，重试没有意义——空间不会自己长出来）；其余 I/O 错误抛可恢复的 `DL_ABORT_EX3`：

```cpp
void AbstractDiskWriter::writeData(const unsigned char* data, size_t len,
                                   int64_t offset) {
  ensureMmapWrite(len, offset);
  if (writeDataInternal(data, len, offset) < 0) {
    int errNum = fileError();
    if (isDiskFullError(errNum)) {
      throw DOWNLOAD_FAILURE_EXCEPTION3(errNum, /* ... */,
          error_code::NOT_ENOUGH_DISK_SPACE);      // = 9，致命
    }
    else {
      throw DL_ABORT_EX3(errNum, /* ... */,
          error_code::FILE_IO_ERROR);              // = 17，可恢复
    }
  }
}
```

**截断与预分配**。[AbstractDiskWriter.cc:L481-L500](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractDiskWriter.cc#L481-L500) 的 `truncate` 就是 `ftruncate(fd, length)`（Windows 用 `SetFilePointerEx` + `SetEndOfFile` 绕过 2GB 限制）；[AbstractDiskWriter.cc:L502-L576](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractDiskWriter.cc#L502-L576) 的 `allocate` 按 `sparse` 参数和平台条件编译选择 `truncate` / `fallocate` / `posix_fallocate` / `F_PREALLOCATE` / `SetFileValidData`——这一段是 u5-l4（文件预分配）的主角，本讲只需知道入口在这里。

**mmap 懒加载**。[AbstractDiskWriter.cc:L341-L428](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractDiskWriter.cc#L341-L428) 的 `ensureMmapWrite` 体现「尽力而为、失败降级」：零长度文件不映射（`munmap` 尺寸为 0 会 `EINVAL`）；32 位系统上文件超过 `size_t` 上限不映射；写偏移超出映射长度则解除映射并永久关闭 mmap；任何一步 `mmap` 失败只记 WARN 日志并把 `enableMmap_` 置 false，下载继续走 fd 路径。

**关闭**。[AbstractDiskWriter.cc:L131-L167](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractDiskWriter.cc#L131-L167) 的 `closeFile` 顺序是先 `munmap`（若有映射，失败仅记日志）再 `close(fd_)`，最后把 fd 归位 `A2_BAD_FD` 使 closeFile 可重入。

**DefaultDiskWriter 补完最后一块**。[DefaultDiskWriter.cc:L39-L46](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultDiskWriter.cc#L39-L46) 整个具体类只有一个非平凡方法：

```cpp
void DefaultDiskWriter::initAndOpenFile(int64_t totalLength) { createFile(); }
```

**写入器在调用链中的位置**。[AbstractSingleDiskAdaptor.cc:L56-L77](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractSingleDiskAdaptor.cc#L56-L77) 显示 DiskAdaptor 层只是把打开/写请求原样转发给 DiskWriter——本讲的写入器是数据落盘前的最后一站。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：搞清 `--allow-overwrite` 与 `--auto-file-renaming` 在「创建写入器之前」和「打开写入器之时」分别由哪些代码路径生效，并实际验证重名文件时 aria2 的三种行为。

**第一步：源码侧——画出单文件下载的裁定时序**。以一次普通 HTTP 下载（`aria2c URL -o out.bin`）为线索，按下面顺序精读（均在 [src/RequestGroup.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc)）：

1. `createInitialCommand` 的单文件分支 [RequestGroup.cc:L465-L482](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L465-L482)：注意 **`adjustFilename`（L475）在 `initPieceStorage`（L476）之前**——改名发生在创建写入器之前，所以写入器拿到的路径已经是改名后的最终路径。
2. [RequestGroup.cc:L676-L716](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L676-L716) `adjustFilename`：无控制文件且不走续传/校验时进入 `shouldCancelDownloadForSafety`。
3. [RequestGroup.cc:L766-L778](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L766-L778) `shouldCancelDownloadForSafety`：**`--allow-overwrite=true` 在这里直接 `return` 放行**，随后 `loadAndOpenFile` 会走到 `initAndOpenFile` → `createFile` 的 `O_TRUNC` 完成覆盖（「放行决定」在创建写入器后、打开时兑现）。
4. [RequestGroup.cc:L780-L825](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L780-L825) `tryAutoFileRenaming`：**`--auto-file-renaming=false` 在这里抛 `FILE_ALREADY_EXISTS`(13)**；为 true 时从 `file.1.ext` 试到 `file.9999.ext`，找到一个「不存在，或存在但有配套 .aria2 控制文件」的名字就改写 `FileEntry` 的路径。
5. [RequestGroup.cc:L731-L763](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L731-L763) `loadAndOpenFile`：按「有 .aria2 → `openExistingFile`；`-c` 且文件不大于总长 → `openExistingFile`；`--check-integrity` → `openExistingFile`；否则 → `initAndOpenFile`」选择打开方式。
6. 多文件与 BT 分支的对应检查在 [RequestGroup.cc:L431-L445](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L431-L445) 与 [RequestGroup.cc:L512-L519](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L512-L519)：文件存在且无控制文件时，除非 `--check-integrity`/`--allow-overwrite`/`--bt-seed-unverified` 之一开启，否则抛 `MSG_FILE_ALREADY_EXISTS`。该报错文案见 [message.h:L117](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/message.h#L117)。

**第二步：运行侧——验证三种行为**（需要一个可下载的小文件 URL，用 `http://example.com/` 之类均可；以下命令在空目录执行）：

```bash
# 场景 A：默认行为（--auto-file-renaming 默认 true，见 OptionHandlerFactory.cc L110-L112）
./src/aria2c -o out.bin "URL"          # 第一次，生成 out.bin
./src/aria2c -o out.bin "URL"          # 第二次，观察是否生成 out.bin.1
ls out.bin*

# 场景 B：禁用改名 → 报错退出
./src/aria2c -o out.bin --auto-file-renaming=false "URL"; echo "exit=$?"

# 场景 C：显式允许覆盖
./src/aria2c -o out.bin --auto-file-renaming=false --allow-overwrite=true "URL"; echo "exit=$?"
ls out.bin*    # 确认没有 .1 文件，out.bin 被重建
```

**需要观察的现象**：场景 A 出现 `out.bin.1`（且日志有「已改名」的 NOTICE）；场景 B 打印 `File ... exists, but a control file(*.aria2) does not exist...` 并以 `exit=13` 退出；场景 C 没有 `.1` 文件、`out.bin` 被覆盖重下、`exit=0`。

**预期结果**：三个退出码/产物与上述一致；再对照源码说明——B 场景的报错来自 `tryAutoFileRenaming` 的 `throw`（错误码 13 = `FILE_ALREADY_EXISTS`，见 [error_code.h:L59](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/error_code.h#L59)），C 场景的「覆盖」动作最终由 `AbstractDiskWriter::createFile` 的 `O_TRUNC` 完成。若你的 aria2 尚未编译，可先做源码侧画图，运行部分标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`writeDataInternal` 里的 `while ((size_t)writtenLength < len)` 循环去掉会怎样？

**答案**：POSIX `write` 只保证「至多写入 len 字节」，实际可能少写（内核缓冲受限、被信号打断后返回已写部分）。去掉循环会导致 piece 写入不完整且不报错，文件出现「洞」，随后哈希校验失败或数据静默损坏。同理内层 `EINTR` 重试也不能省——被信号打断时 `write` 返回 -1 且 `errno == EINTR`，这不算错误。

**练习 2**：为什么磁盘满要抛 `DownloadFailureException` 而不是像其他 I/O 错误那样走可重试出口？

**答案**：对照 u2-l3 的三级异常出口：可重试异常（`DlRetryEx`）会触发换 IP/等一会儿再试，但磁盘满不会因为重试而好转，继续重试只会无限循环刷日志。所以用「整组停机」级的 `DownloadFailureException`（错误码 `NOT_ENOUGH_DISK_SPACE` = 9）立即终止该下载，把决定权交还给用户（清理空间后重跑）。

**练习 3**：`--enable-mmap` 时 mmap 失败，下载会失败吗？

**答案**：不会。[AbstractDiskWriter.cc:L419-L423](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractDiskWriter.cc#L419-L423) 里 `mmap` 失败只记一条 WARN 日志并把 `enableMmap_` 置 false，之后所有写入自动走 `seek`+`write` 的 fd 路径；映射区被写穿（偏移超出 `maplen_`）时也是先 `munmap` 再降级。mmap 在 aria2 中是「能用则用」的优化，不是正确性前提。

### 4.3 DiskWriterFactory 与内存型特例：按任务选写入器

#### 4.3.1 概念说明

接口定了，谁来决定「这个任务用哪种写入器」？答案是工厂。`DiskWriterFactory` 是只有一个方法的极简接口：

```cpp
virtual std::unique_ptr<DiskWriter> newDiskWriter(const std::string& filename) = 0;
```

工厂体系一共有三层：

| 工厂 | 产出 | 忽略文件名？ | 使用场景 |
| --- | --- | --- | --- |
| `DefaultDiskWriterFactory` | `DefaultDiskWriter`（磁盘文件） | 否 | 一切普通下载（默认工厂） |
| `AnonDiskWriterFactory<T>`（模板） | 任意无参构造的 `T` | **是** | 内存型写入器的通用壳 |
| `ByteArrayDiskWriterFactory` / `BencodeDiskWriterFactory`（typedef） | `ByteArrayDiskWriter` / `BencodeDiskWriter` | 是 | magnet 元数据、tracker 响应、`--follow-torrent=mem` |

`AnonDiskWriterFactory` 的巧妙之处在于：内存型写入器根本不需要文件名（字节进内存），所以模板工厂把 `filename` 参数直接丢弃。这样一来，**注入工厂 = 改变下载目的地**，下载流水线（命令链、segment、piece）一行都不用改。

「内存型特例」的两类代表：

- **`ByteArrayDiskWriter`**：`std::stringstream` 假扮文件。写入超过 15 MiB（`15_m` 字面量，[ByteArrayDiskWriter.h:L52](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ByteArrayDiskWriter.h#L52)）会抛异常——防止恶意 tracker 塞一个无限大的响应把内存吃爆。
- **`ValueBaseDiskWriter<Parser>`**（[ValueBaseDiskWriter.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ValueBaseDiskWriter.h)）：写入的字节**直接流进解析器**。`BencodeDiskWriter` 就是 `ValueBaseDiskWriter<BencodeParser>` 的 typedef（[BencodeDiskWriter.h:L45](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BencodeDiskWriter.h#L45)）。RPC 侧的 XML-RPC 请求体同理用 `XmlRpcDiskWriter`（[HttpServer.cc:L391](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServer.cc#L391)，直接构造、不经工厂）。这类写入器**忽略 offset 顺序消费**、不支持读——它们是最极端的「DiskWriter 形状，解析器内核」。

#### 4.3.2 核心流程

工厂的注入与消费链路：

```text
谁注入工厂？
  ├─ 默认：不注入 → DefaultPieceStorage/UnknownLengthPieceStorage
  │        构造时自备 DefaultDiskWriterFactory（DefaultPieceStorage.cc:84）
  ├─ magnet 任务：download_helper.cc:292 → ByteArrayDiskWriterFactory
  │        （下载的 .torrent 元数据进内存，收齐后由后处理器解析派生真任务）
  ├─ tracker 响应：TrackerWatcherCommand.cc:392-393 → ByteArrayDiskWriterFactory
  └─ --follow-torrent=mem：MemoryPreDownloadHandler 把工厂换成 BencodeDiskWriterFactory

谁消费工厂？
  RequestGroup::initPieceStorage（RequestGroup.cc:601-611）
    └─ 把 diskWriterFactory_ 注入 PieceStorage
         └─ DefaultPieceStorage::initStorage（DefaultPieceStorage.cc:644-645）
              ├─ 单文件：DirectDiskAdaptor + factory->newDiskWriter(路径)
              └─ 多文件：MultiDiskAdaptor（内部逐文件用本地
                 DefaultDiskWriterFactory，MultiDiskAdaptor.cc:181-194）
```

注意多文件任务的细节：`MultiDiskAdaptor` **不用** `RequestGroup` 注入的工厂，而是在自己的 `openFile` 里为每个需要的文件新建一个 `DefaultDiskWriter`（[MultiDiskAdaptor.cc:L181-L194](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiDiskAdaptor.cc#L181-L194)）——一个种子 N 个文件就有 N 个写入器实例，各自持有 fd。这也是为什么内存工厂只对「单文件任务」有意义（元数据任务都是单文件）。

#### 4.3.3 源码精读

**工厂接口**。[DiskWriterFactory.h:L47-L53](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DiskWriterFactory.h#L47-L53) 只有一个纯虚方法，返回 `std::unique_ptr` 明确转移所有权：

```cpp
class DiskWriterFactory {
public:
  virtual ~DiskWriterFactory() = default;
  virtual std::unique_ptr<DiskWriter>
  newDiskWriter(const std::string& filename) = 0;
};
```

**默认工厂**。[DefaultDiskWriterFactory.cc:L41-L45](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultDiskWriterFactory.cc#L41-L45) 把文件名塞给 `DefaultDiskWriter` 构造函数：

```cpp
std::unique_ptr<DiskWriter>
DefaultDiskWriterFactory::newDiskWriter(const std::string& filename) {
  return make_unique<DefaultDiskWriter>(filename);
}
```

**匿名模板工厂**。[AnonDiskWriterFactory.h:L45-L56](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AnonDiskWriterFactory.h#L45-L56) 的 `newDiskWriter` 干脆无视 `filename`，直接无参构造模板参数类型：

```cpp
template <class DiskWriterType>
class AnonDiskWriterFactory : public DiskWriterFactory {
public:
  virtual std::unique_ptr<DiskWriter>
  newDiskWriter(const std::string& filename) CXX11_OVERRIDE {
    return make_unique<DiskWriterType>();   // filename 被有意丢弃
  }
};
```

于是两个内存工厂都只是一行 typedef（[ByteArrayDiskWriterFactory.h:L43](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ByteArrayDiskWriterFactory.h#L43) 与 [BencodeDiskWriterFactory.h:L45](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BencodeDiskWriterFactory.h#L45)）。

**消费点：单文件任务**。[DefaultPieceStorage.cc:L634-L646](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc#L634-L646) 在 `initStorage` 里按文件数二选一；单文件路径用注入的工厂创建写入器并交给 `DirectDiskAdaptor`：

```cpp
void DefaultPieceStorage::initStorage() {
  if (downloadContext_->getFileEntries().size() == 1) {
    auto directDiskAdaptor = std::make_shared<DirectDiskAdaptor>();
    /* ... */
    directDiskAdaptor->setDiskWriter(
        diskWriterFactory_->newDiskWriter(directDiskAdaptor->getFilePath()));
    diskAdaptor_ = std::move(directDiskAdaptor);
  }
  else {
    auto multiDiskAdaptor = std::make_shared<MultiDiskAdaptor>();
    /* 多文件不经过注入的工厂，见 MultiDiskAdaptor.cc:181 */
  }
}
```

**注入点一：magnet 元数据任务**。[download_helper.cc:L285-L294](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc#L285-L294) 创建 magnet 的「元数据下载任务」时注入 `ByteArrayDiskWriterFactory` 并标记 `markInMemoryDownload()`——这个任务下载的其实是 .torrent 文件的字节流，落进内存后由 `UTMetadataPostDownloadHandler` 解析（u6-l9 详述），再派生真正的文件下载任务：

```cpp
rg->addPostDownloadHandler(
    download_handlers::getUTMetadataPostDownloadHandler());
rg->setDiskWriterFactory(std::make_shared<ByteArrayDiskWriterFactory>());
rg->markInMemoryDownload();
```

**注入点二：--follow-torrent=mem**。[MemoryPreDownloadHandler.h:L43-L54](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MemoryPreDownloadHandler.h#L43-L54) 是 PreDownloadHandler（u3-l3 的 pre 处理器）：在任务启动前把工厂换成 `BencodeDiskWriterFactory`，同时关闭预分配与本地文件检查、并发降为 1——种子文件本身会被解析成 `DownloadContext` 而不是存到磁盘：

```cpp
virtual void execute(RequestGroup* requestGroup) const {
  auto dwf = std::make_shared<DiskWriterFactoryType>();
  requestGroup->setDiskWriterFactory(dwf);
  requestGroup->setFileAllocationEnabled(false);
  requestGroup->setPreLocalFileCheckEnabled(false);
  requestGroup->markInMemoryDownload();
  requestGroup->setNumConcurrentCommand(1);
}
```

**内存写入器如何伪装文件**。[ByteArrayDiskWriter.cc:L59-L77](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ByteArrayDiskWriter.cc#L59-L77) 的 `writeData` 要自己处理「偏移跳着来」：若写入偏移超过当前长度（前面有洞），先补 `'\0'` 再写；同时用 `maxLength_`（默认 15 MiB）防内存爆炸：

```cpp
void ByteArrayDiskWriter::writeData(const unsigned char* data,
                                    size_t dataLength, int64_t offset) {
  if (offset + dataLength > maxLength_) {
    throw DL_ABORT_EX(fmt("Maximum length(%lu) exceeded.", /* ... */));
  }
  int64_t length = size();
  if (length < offset) {           // 有洞：补 '\0'
    buf_.seekp(length, std::ios::beg);
    for (int64_t i = length; i < offset; ++i) buf_.put('\0');
  }
  else { buf_.seekp(offset, std::ios::beg); }
  buf_.write(reinterpret_cast<const char*>(data), dataLength);
}
```

**解析器内核的写入器**。[ValueBaseDiskWriter.h:L47-L94](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ValueBaseDiskWriter.h#L47-L94)：打开文件等于重置解析器，写入等于喂数据（**offset 被显式忽略**），`finalize()` 收尾，`getResult()` 取出解析好的 `ValueBase` 树：

```cpp
virtual void writeData(const unsigned char* data, size_t len,
                       int64_t offset) CXX11_OVERRIDE {
  // Return value is ignored here but handled in finalize()
  parser_.parseUpdate(reinterpret_cast<const char*>(data), len);
}
int finalize() { return parser_.parseFinal(0, 0); }
std::unique_ptr<ValueBase> getResult() { return psm_.getResult(); }
```

`size()` 恒返回 0、`readData` 恒返回 0、`closeFile` 为空——这印证了 4.1 的观点：接口契约里真正的硬性要求只有「接收带偏移的字节流」，其余方法都可以是空壳。

#### 4.3.4 代码实践

**实践目标**：用测试代码验证内存型写入器的行为边界，体会「同一接口、两种内核」。

1. 阅读 [test/DefaultDiskWriterTest.cc:L23-L29](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DefaultDiskWriterTest.cc#L23-L29)——全仓库最短测试之一：构造 `DefaultDiskWriter` → `enableReadOnly()` → `openExistingFile()` → 断言 `size()` 等于 4 KiB。它演示了磁盘写入器的最小用法。
2. 打开 [test/ByteArrayDiskWriterTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/ByteArrayDiskWriterTest.cc)，找出它对 `writeData`「先写偏移 10、再写偏移 0」这类跳跃写入的断言，验证 4.3.3 中「补 '\0' 造洞」的逻辑。
3. 仿照上述风格，写一段示例代码（**示例代码**，非项目原有）：

```cpp
// 示例代码：验证 ByteArrayDiskWriter 的空洞填充与长度上限
#include "ByteArrayDiskWriter.h"
auto dw = std::make_unique<aria2::ByteArrayDiskWriter>(100);  // 上限 100 字节
dw->initAndOpenFile();
dw->writeData(reinterpret_cast<const unsigned char*>("AB"), 2, 8); // 偏移 8 起
printf("size=%lld\n", (long long)dw->size());   // 预期 10（8 个 '\0' + "AB"）
try {
  dw->writeData(reinterpret_cast<const unsigned char*>("X"), 1, 100);
} catch (aria2::Exception& e) {
  printf("caught: %s\n", e.what());             // 预期 Maximum length exceeded
}
```

4. **需要观察的现象**：`size()` 打印 10；写偏移 100（超过上限）抛出 `Maximum length(100) exceeded`。
5. **预期结果**：与源码推导一致；若想实际编译运行，可把它挂进 `test/` 仿照现有测试的组织方式（u9-l5 会讲测试体系），否则标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：magnet 下载元数据的任务为什么不直接写一个 `.torrent` 文件到磁盘，而要走内存工厂？

**答案**：元数据任务的产物只是一个中间结果——拿到 infohash 对应的种子字典后，立刻由 `UTMetadataPostDownloadHandler` 解析成 `DownloadContext`，再派生真正的文件下载任务（u6-l9）。中间字节流没有保存价值；用 `ByteArrayDiskWriterFactory` 让整条下载流水线（HTTP/BT 命令链、piece 管理）原样复用，只是目的地从 fd 换成了 `stringstream`。同理 tracker 的 announce 响应也是一次性的。而 `--follow-torrent=mem` 时用户明确说「不要保存种子文件」，也走这条路。

**练习 2**：多文件 BT 任务为什么不能通过注入内存工厂让所有文件都进内存？

**答案**：`DefaultPieceStorage::initStorage`（DefaultPieceStorage.cc:634-662）只在**单文件**分支消费注入的工厂；多文件分支创建 `MultiDiskAdaptor`，后者在 `openFile` 时为每个 `FileEntry` 用**本地** `DefaultDiskWriterFactory` 建磁盘写入器（MultiDiskAdaptor.cc:181-194）。所以注入内存工厂只影响单文件任务；且元数据任务必然是单文件，两者正好匹配。

**练习 3**：`ValueBaseDiskWriter` 忽略 `offset` 参数为什么不会出 bug？

**答案**：它的唯一使用场景是「顺序到达、一次解析」的元数据/RPC 报文：`MemoryPreDownloadHandler` 会把并发命令数降为 1，`markInMemoryDownload` 让任务不参与分段调度，因此数据必然按 0,1,2,… 的顺序单调推进，offset 无信息量。而 `initAndOpenFile` 的语义（截断为 0）被映射为「重置解析器」，恰好符合「重新开始接收」的意图。这是接口适配使用场景、而非追求全功能的典型案例。

## 5. 综合实践

**任务：给一次下载的「磁盘轨迹」写一份完整档案。**

选择任意一个可下载的小文件（HTTP 即可），完成下面四步，把本讲三个模块串起来：

1. **预测**：先不运行，写出你预测的完整时序——从 `createInitialCommand` 决定文件名（工厂尚未创建），到 `initPieceStorage` 创建 `DefaultDiskWriter`（此时 `filename_` 是什么？），到 `loadAndOpenFile` 选择哪种打开方式，到下载中每个 piece 经 `writeData(data, len, offset)` 落盘，到 `closeFile` 收尾。标注每一步发生在 [src/RequestGroup.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc) / [src/DefaultPieceStorage.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultPieceStorage.cc) / [src/AbstractDiskWriter.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractDiskWriter.cc) 的哪一行。
2. **观察覆盖三态**：按 4.2.4 第二步的三个场景（默认改名 / 禁改名报错 / 允许覆盖）运行并记录产物与退出码，与 [error_code.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/error_code.h) 对照。
3. **加日志**：在 `AbstractDiskWriter::createFile` 与 `writeDataInternal` 各加一行 `A2_LOG_DEBUG`（打印文件名、offset、len——日志用法见 u9-l3），重编译后带 `-l log.txt --log-level=debug` 运行，从日志里数一数同一个文件被 `writeData` 了多少次、offset 是否递增有序（提示：`-s1` 时有序，`-s4` 时会交错），验证「定位写支撑多连接」的说法。
4. **换目的地**：把同一份日志开着，再跑一个 magnet 链接（`aria2c "magnet:?xt=urn:btih:..."`），确认元数据阶段磁盘上**没有**新文件出现——因为 [download_helper.cc:L292](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc#L292) 注入的是内存工厂，`AbstractDiskWriter` 的日志一次都不会打印。

产出：一份时序图（步骤 1）、一张三态对照表（步骤 2）、一页日志分析（步骤 3、4）。其中步骤 3、4 依赖本地编译运行，无法完成时在对应条目标注「待本地验证」，但源码侧结论应能全部落笔。

## 6. 本讲小结

- `DiskWriter` = `BinaryStream`（按偏移读写）+ 文件生命周期契约（三种打开方式语义各异：`initAndOpenFile` 截断新建、`openExistingFile` 只开已有、`openFile` 折中回退），外加一组默认为空的可选能力（只读、mmap、丢缓存、刷盘）。
- `AbstractDiskWriter` 是骨架实现：持 fd 与 mmap 状态，`createFile`（mkdirs + `O_CREAT|O_TRUNC`）留给子类；定位写 = `seek` + 循环 `write`（防部分写入与 EINTR）；磁盘满抛致命异常（码 9）立即停机，其他 I/O 错误抛可恢复异常（码 17）；mmap 失败自动降级，析构函数兜底关闭文件。
- `--allow-overwrite` 的「放行」裁定在 `shouldCancelDownloadForSafety`（创建写入器后、打开前），真正的覆盖动作发生在 `initAndOpenFile` → `createFile` 的 `O_TRUNC`；`--auto-file-renaming`（默认 true）在 `tryAutoFileRenaming` 里改写 `FileEntry` 路径——**改名发生在写入器创建之前**，设为 false 则抛 `FILE_ALREADY_EXISTS`(13)。
- `DiskWriterFactory` 体系用「注入工厂 = 改变下载目的地」实现同一流水线多目的地：默认 `DefaultDiskWriterFactory` 写磁盘；`AnonDiskWriterFactory<T>` 模板忽略文件名产出内存型写入器；magnet 元数据（`ByteArrayDiskWriterFactory`）、`--follow-torrent=mem`（`BencodeDiskWriterFactory`）、RPC（`XmlRpcDiskWriter`）都是内存特例。
- 多文件任务不消费注入的工厂：`MultiDiskAdaptor` 为每个 `FileEntry` 自建 `DefaultDiskWriter`，一个种子 N 个文件 = N 个写入器实例。

## 7. 下一步学习建议

本讲解决的是「单个文件怎么写」，两讲之后你将看到这块拼图如何嵌入全链路：

1. **下一讲 u5-l2「PieceStorage：piece 级别的数据管理」**：看 piece 的 bitfield 如何决定「哪段数据可以交给本讲的 `writeData`」，以及 `DefaultPieceStorage` 如何持有写入器并管理完成状态。
2. **u5-l3「DiskAdaptor：单文件与多文件布局适配」**：本讲埋下的伏笔——`DirectDiskAdaptor` 与 `MultiDiskAdaptor` 如何把多文件任务的逻辑偏移翻译成「哪个写入器的哪个偏移」。
3. **u5-l4「文件预分配」**：本讲 4.2.3 中一笔带过的 `AbstractDiskWriter::allocate`（ftruncate/fallocate/posix_fallocate/F_PREALLOCATE）是那一讲的主角。
4. 延伸阅读：[test/ByteArrayDiskWriterTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/ByteArrayDiskWriterTest.cc) 与 [test/DefaultDiskWriterTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/DefaultDiskWriterTest.cc) 是两类写入器的最小用例，写自己的实验代码前值得先读。
