# stdio FILE 模型与文件 I/O

## 1. 本讲目标

本讲把前面学到的两条线索汇合到一处：上一单元（u8-l1）讲清了「LLVM-libc 如何用 `OSUtil` 把一次系统调用封装成 `ErrorOr`」，更早的 u7-l2 讲清了「printf_core 的 `Writer` 如何把格式化结果送到一个输出汇」。这一讲要回答的是：**`fopen`/`fwrite`/`fread` 这些大家最熟悉的「带缓冲文件 I/O」到底是怎么搭起来的？**

具体地，学完本讲你应该能够：

1. 解释公开的 `::FILE` 为什么是一个**不透明类型（opaque type）**，以及实现代码如何用 `reinterpret_cast` 把它与内部的 `File` 类互相转换。
2. 看懂 `File` 类如何用四个**函数指针**把「平台无关的缓冲逻辑」和「平台相关的系统调用」解耦。
3. 说清三种缓冲模式（全缓冲 `_IOFBF`、行缓冲 `_IOLBF`、无缓冲 `_IONBF`）在源码里是如何分派的，以及缓冲区状态机如何运转。
4. 描述 `src/stdio/generic/` 与 `src/stdio/linux/`（以及 `gpu/`、`baremetal/`）的分工，并理解 CMake 的「平台优先、generic 兜底」选择机制。
5. 跟踪一条完整的调用链：`fwrite` 入口 → `File::write` 缓冲 → `platform_write` → `linux_syscalls::write` → `syscall_impl`（u8-l1 的汇编系统调用）→ Linux 内核。

## 2. 前置知识

在进入源码前，先用通俗语言对齐几个概念。

- **流（stream）与 FILE**：C 标准把一个打开的文件抽象成一个「字节流」，并用一个 `FILE *` 指针代表它。`FILE` 的内部结构标准并不规定，每个 libc 实现可以自由设计。
- **缓冲（buffering）**：每次读写都直接发起系统调用代价很高（用户态↔内核态切换）。libc 通常在用户态维护一块缓冲区，写时先攒在缓冲里，读时一次多读一些，从而减少系统调用次数。C 标准定义了三种缓冲模式：
  - `_IOFBF`（fully buffered，全缓冲）：缓冲区满才真正输出。
  - `_IOLBF`（line buffered，行缓冲）：遇到换行符 `\n` 就把已攒的内容输出（典型用于终端）。
  - `_IONBF`（no buffering，无缓冲）：每次写都立即输出，不攒。
- **不透明类型**：头文件里只写 `typedef struct FILE FILE;` 而不暴露 `struct FILE` 的字段，调用者只能拿到一个指针，不能直接读写其内部。这样 libc 可以自由改变内部布局而不破坏调用者代码。
- **`reinterpret_cast`**：C++ 的强制类型转换，按位重新解释指针类型。LLVM-libc 用它把公开的 `::FILE *` 当作内部的 `File *` 来操作——这两者其实是同一个对象。
- **函数指针作为「钩子」**：在一个类里存放几个函数指针，由子类或平台代码在构造时填入具体函数。这样同一个类可以挂接不同的底层实现。本讲的 `File` 类正是用这个手法把缓冲逻辑与系统调用分离。

本讲承接 u8-l1（OSUtil 的 `syscall_impl` 与 `ErrorOr` 错误返回约定）和 u7-l2（printf_core 的 `Writer`/`WriteBuffer` 抽象）。你会看到：printf 的输出最终也是经本讲的 `File::write` 走到内核的。

## 3. 本讲源码地图

本讲涉及的源码分三层，由上到下离硬件越来越近：

| 层 | 文件 | 作用 |
| --- | --- | --- |
| 公开类型 | `hdr/types/FILE.h`、`include/llvm-libc-types/FILE.h` | 定义公开的 `::FILE`，Full/Overlay 两种构建模式下来源不同 |
| 入口点壳 | `src/stdio/fopen.h`、`src/stdio/generic/fopen.cpp` | `fopen` 的声明与实现（薄壳） |
| 入口点壳 | `src/stdio/fwrite.h`、`src/stdio/generic/fwrite.cpp` | `fwrite` 的声明与实现（薄壳） |
| 缓冲内核 | `src/__support/File/file.h`、`src/__support/File/file.cpp` | 平台无关的 `File` 类与全部缓冲逻辑 |
| 平台特化 | `src/__support/File/linux/file.h`、`src/__support/File/linux/file.cpp` | Linux 的 `LinuxFile` 子类与 `openfile` 实现 |
| 系统调用 | `src/__support/OSUtil/linux/syscall_wrappers/write.h` | 把 `write(2)` 封装成 `ErrorOr<ssize_t>` |
| 构建组织 | `src/stdio/CMakeLists.txt`、`src/__support/File/CMakeLists.txt` | 平台子目录选择与 `platform_file` 别名 |
| 测试范例 | `test/src/stdio/fopen_test.cpp` | `fopen`/`fwrite`/`fread` 端到端测试 |

记住这条主线：**入口点壳很薄，真正的缓冲逻辑在 `__support/File/file.cpp`，真正碰硬件的是 `__support/File/linux/file.cpp` 经 `OSUtil` 发起的系统调用。**

## 4. 核心概念与源码讲解

### 4.1 FILE 抽象：公开的不透明类型与内部的 File 类

#### 4.1.1 概念说明

C 标准规定 `fopen` 返回一个 `FILE *`，但**从不规定 `FILE` 长什么样**。传统 libc（glibc、musl）会在内部定义一个字段繁多的 `struct FILE`。LLVM-libc 的做法更干净：

- 对外，`FILE` 是一个**不透明类型**——头文件里只写 `typedef struct FILE FILE;`，调用者完全看不到内部字段。
- 对内，LLVM-libc 定义了一个 C++ 类 [`File`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/File/file.h#L43-L51)，承载缓冲区、锁、模式标志、以及四个**函数指针**。
- 入口点函数（如 `fwrite`）拿到 `::FILE *` 后，用 `reinterpret_cast<File *>` 把它当作内部的 `File` 来操作。也就是说，**`::FILE *` 和 `File *` 指向同一个对象**，只是公开面与内部面的两种「视角」。

这套设计的妙处在于：`File` 类本身是**平台无关**的，它不知道自己挂在什么操作系统上；真正与平台相关的是那四个函数指针（写、读、定位、关闭），由各平台的子类在构造时填入。于是「缓冲算法」与「系统调用」被彻底解耦——同一份 `file.cpp` 的缓冲代码，在 Linux 上挂 Linux 的系统调用、在 GPU 上挂 GPU 的实现。

> 与 u1-l4 的呼应：正因为 `FILE` 的内部布局是 LLVM-libc 的私有 ABI，`fopen` 这类函数**不能放进 Overlay 模式**的 `libllvmlibc.a`（否则会和系统 libc 的 `FILE` 布局冲突）。代理头 `hdr/types/FILE.h` 在 Full 与 Overlay 间切换的就是这个 `FILE` 的来源。

#### 4.1.2 核心流程

打开并使用一个文件的数据流：

```text
fopen(path, mode)                          [入口点壳 src/stdio/generic/fopen.cpp]
   │
   └─► openfile(path, mode)                [平台实现 src/__support/File/linux/file.cpp]
          │  1. File::mode_flags(mode)  把 "r"/"w"/"a"/"+" 解析成位标志
          │  2. linux_syscalls::open(...) 发起 open(2) 拿到 fd
          │  3. new LinuxFile(fd, buffer, _IOFBF, ...)  构造对象，填入 4 个函数指针
          │  4. File::add_file(file)     挂进全局文件链表
          └─► 返回 file (类型 File*)

fwrite(buf, size, nmemb, ::FILE *stream)   [入口点壳 src/stdio/generic/fwrite.cpp]
   │
   └─► reinterpret_cast<File *>(stream)
          └─► File::write(buf, size*nmemb)  [缓冲内核 src/__support/File/file.cpp]
                 └─► (见 4.2、4.4)
```

注意 `fopen` 返回的是 `File *`，但被 `reinterpret_cast<::FILE *>` 包成了公开类型；`fwrite` 再反向 `reinterpret_cast<File *>` 还原。这一来一回，公开的不透明指针与内部的 C++ 对象就建立了对应关系。

#### 4.1.3 源码精读

先看公开类型。在 Full 模式下，`FILE` 来自自包含类型头：

[`include/llvm-libc-types/FILE.h:18-20`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/include/llvm-libc-types/FILE.h#L18-L20) —— 这就是 `FILE` 的全部定义，只有一句不透明 `typedef`，调用者拿不到任何字段。

而代理头 [`hdr/types/FILE.h:12-20`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/hdr/types/FILE.h#L12-L20) 用 `#ifdef LIBC_FULL_BUILD` 决定来源：Full 用上面的自包含定义，Overlay 回退到系统头文件。这与 u3-l2 讲的代理头切换机制完全一致。

再看内部的 `File` 类。它的公开接口里有一组静态方法管理一个**全局文件链表**（`add_file`/`remove_file`/`get_first_file`），以及一系列面向用户的缓冲读写方法：

[`src/__support/File/file.h:43-51`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/File/file.h#L43-L51) —— 类定义开头，`list_all` 与 `list_lock` 是全局链表的静态成员。

类的真正「钩子」是这四个函数指针类型，分别对应写、读、定位、关闭：

[`src/__support/File/file.h:60-68`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/File/file.h#L60-L68) —— 注意它们都把 `File *` 作为第一个参数，签名与「平台相关系统调用薄封装」吻合。

这四个类型对应的私有字段就是函数指针本身：

[`src/__support/File/file.h:98-104`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/File/file.h#L98-L104) —— `platform_write`/`platform_read`/`platform_seek`/`platform_close`。谁构造 `File`，谁就负责填这四个指针。

构造函数一次性把缓冲区、模式、四个函数指针都设好：

[`src/__support/File/file.h:176-188`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/File/file.h#L176-L188) —— 注意它是 `constexpr`，目的是让 `stdout` 这种全局文件对象能在编译期就构造好，避免「静态初始化顺序灾难」（注释里点明了这一点）。末尾调用 `adjust_buf()` 处理「可读但没给缓冲」的边界（见 4.2）。

那么谁来调用这个构造函数？在 Linux 上是 `LinuxFile` 子类：

[`src/__support/File/linux/file.h:20-32`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/File/linux/file.h#L20-L32) —— `LinuxFile` 继承 `File`，额外只多存一个 `int fd`（文件描述符），并在构造时把 `linux_file_write/read/seek/close` 四个函数填进父类的函数指针。`get_fd()` 让平台实现能取回 fd 去发起系统调用。

最后看入口点如何把两个面缝合起来。`fopen` 的实现：

[`src/stdio/generic/fopen.cpp:18-26`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/stdio/generic/fopen.cpp#L18-L26) —— 调用 `openfile`（由 `platform_file` 库提供，见 4.3/4.4），失败则按 u4-l3 的模式设 `libc_errno` 并返回 `nullptr`；成功则 `reinterpret_cast<::FILE *>` 把内部 `File *` 包成公开类型返回。`openfile` 的声明见 [`file.h:387-391`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/File/file.h#L387-L391)，注释点明「由 platform_file 库实现」。

`fwrite` 的实现则是反向拆包：

[`src/stdio/generic/fwrite.cpp:19-30`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/stdio/generic/fwrite.cpp#L19-L30) —— 把 `::FILE *stream` 强转为 `File *`，调用其 `write(buffer, size*nmemb)`，再把返回的字节数除以 `size` 换算成「写完整成员数」（C 标准要求 `fwrite` 返回的是完整成员数，不是字节数）。失败时设 `libc_errno`。

#### 4.1.4 代码实践

**目标**：亲眼确认「`::FILE *` 与 `File *` 是同一个对象」这条关键事实。

**操作步骤**（源码阅读型）：

1. 打开 [`src/stdio/generic/fopen.cpp`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/stdio/generic/fopen.cpp)，确认 `return reinterpret_cast<::FILE *>(result.value())`——这里 `result.value()` 是 `File *`，被强转成了 `::FILE *`，没有任何拷贝或包装。
2. 打开 [`src/stdio/generic/fwrite.cpp`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/stdio/generic/fwrite.cpp)，确认 `reinterpret_cast<LIBC_NAMESPACE::File *>(stream)->write(...)`——同一个指针被强转回来。
3. 因为 `LinuxFile` 公开继承 `File`，一个 `LinuxFile` 对象的地址同时是合法的 `File *`、也是合法的 `::FILE *`（不透明指针不关心指向类型）。

**需要观察的现象**：两个 `reinterpret_cast` 都是「零成本」的位级重解释——对象自始至终只有一份，就是 `openfile` 里 `new LinuxFile(...)` 出来的那个。

**预期结果**：你能用一句话回答「为什么 `fwrite` 拿到一个 `::FILE *` 就敢直接当 `File *` 用」——因为它俩本来就是同一个指针，只是公开/内部两种视角。

#### 4.1.5 小练习与答案

**练习 1**：为什么不把 `struct FILE` 的字段直接暴露在公共头里，而要做成不透明类型？

**参考答案**：暴露字段会把内部布局变成公共 ABI，之后任何字段调整都会破坏已编译的调用者代码；不 opaque 化也使「Overlay 模式回退系统 `FILE`」无法实现。不透明类型让 LLVM-libc 可以自由演进 `File` 类，同时让 `FILE` 在 Full/Overlay 间切换来源（见代理头 `hdr/types/FILE.h`）。

**练习 2**：`LinuxFile` 相比父类 `File` 多了什么？为什么必须由子类而不是 `File` 自己来存这个字段？

**参考答案**：多了 `int fd`（文件描述符）。`fd` 是 Unix 概念，GPU/baremetal 等平台未必有 fd；把它放进平台无关的 `File` 会污染基类。由 `LinuxFile` 子类持有，既保持了 `File` 的平台中立，又让 `linux_file_write` 等函数能 `reinterpret_cast<LinuxFile *>` 取回 fd 去发系统调用。

---

### 4.2 缓冲 I/O：三种缓冲模式与缓冲区状态机

#### 4.2.1 概念说明

`File` 类最核心的价值不是「存 fd」，而是**在用户态做缓冲**。它的私有字段维护着一个小型状态机：

```text
uint8_t *buf;     // 指向缓冲区
size_t bufsize;   // 缓冲区容量（默认 1024，见 DEFAULT_BUFFER_SIZE）
int bufmode;      // _IOFBF / _IOLBF / _IONBF
size_t pos;       // 当前读/写位置（在 buf 里的下标）
size_t read_limit;// 读模式下的有效数据上界
bool eof, err;    // 文件结束 / 错误标志
FileOp prev_op;   // 上一次操作是 NONE/READ/WRITE/SEEK
```

字段见 [`src/__support/File/file.h:114-141`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/File/file.h#L114-L141)。

`pos` 与 `read_limit` 复用同一片 `buf`：写模式下 `pos` 是已写入字节数；读模式下 `pos` 是已读出位置、`read_limit` 是已从内核拉取的有效数据上界。`prev_op` 之所以要记，是因为读写切换、seek、flush 都需要知道「上一次在干什么」才能正确处理缓冲里残留的数据。

三种缓冲模式的差别，在源码里就是一个 `switch`：

- **`_IONBF`（无缓冲）**：写时若有残留先冲刷，然后直接把本次数据 `platform_write` 出去，再立即 `flush`。
- **`_IOFBF`（全缓冲）**：优先把数据拷进 `buf`，攒满才冲刷；若本次数据太大（超过「剩余空间 + 一整个缓冲」）则退化为直接写。
- **`_IOLBF`（行缓冲）**：在数据里找最后一个 `\n`，换行之前的部分立即写出（不缓冲），换行之后的部分按全缓冲处理。

注意 `fopen` 打开普通文件时默认用 `_IOFBF`（见 4.4 的 `openfile`），终端流才典型地用 `_IOLBF`。

#### 4.2.2 核心流程

一次 `File::write(data, len)` 的完整流程：

```text
write(data, len)                         // 加锁版本
 ├─ FileLock(this)                       // RAII 取锁（依赖 __support/threads/mutex）
 └─ write_unlocked(data, len)            // 真正干活
      ├─ 检查/设置 orientation（字节 vs 宽字符）
      └─ write_unlocked_impl(data, len)
           ├─ 若 !write_allowed() → 置 err，返回 EBADF
           ├─ prev_op = WRITE
           ├─ switch(bufmode):
           │    _IONBF → write_unlocked_nbf + flush
           │    _IOFBF → write_unlocked_fbf
           │    _IOLBF → write_unlocked_lbf
           └─ 真正输出时调用 platform_write(this, buf, n)  // 见 4.4
```

全缓冲 `write_unlocked_fbf` 用「分割点」算法决定多少数据进缓冲、多少直接写。设缓冲剩余空间为 `bufspace = bufsize - pos`：

\[
\text{split\_point} = \min(len,\ bufspace)
\]

- 若 \(\;len > bufspace + bufsize\;\)：数据大到「连缓冲都省不了」，直接走无缓冲写出。
- 否则把前 `split_point` 字节（primary）拷进缓冲；若还有剩余（remainder）且缓冲正好满了，就 `platform_write` 把缓冲清空，再把 remainder 要么拷进空缓冲、要么（太大时）直接写出。

读路径 `read_unlocked_fbf` 是对偶：先从缓冲里 `copy_data_from_buf` 拷出已有数据；不够时，若还要的量大于 `bufsize` 就直接 `platform_read`，否则一次性拉取「一整缓冲」再从缓冲里分发——这是典型的「预读」策略，用一次系统调用填满缓冲，供后续多次小读取复用。

#### 4.2.3 源码精读

加锁与无锁版本的成对设计：

[`src/__support/File/file.h:191-206`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/File/file.h#L191-L206) —— `write()` 内部用 `FileLock l(this);`（RAII，构造时 `lock()`、析构时 `unlock()`）再委托 `write_unlocked()`。`flockfile`/`funlockfile` 公开的就是这对锁。`read`/`read_unlocked` 同理。

缓冲模式分派（全缓冲/行缓冲/无缓冲三选一）：

[`src/__support/File/file.cpp:73-91`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/File/file.cpp#L73-L91) —— `write_unlocked_impl` 先检查 `write_allowed()`（对应 `r`/`w`/`a`/`+` 标志，见 [`file.h:157-166`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/File/file.h#L157-L166)），再按 `bufmode` 三分派。无缓冲分支写完会**立即 `flush_unlocked()`**，符合「无缓冲 = 立即可见」语义。

全缓冲的「分割点」核心算法：

[`src/__support/File/file.cpp:113-185`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/File/file.cpp#L113-L185) —— 关键步骤：① 算 `bufspace`；② `len > bufspace + bufsize` 时退化为 `write_unlocked_nbf`；③ 用 `cpp::span`（u4-l2 讲过的安全视图）切出 `primary` 与 `remainder` 两段；④ `inline_memcpy`（u5-l2 讲过的内存构建块）把 primary 拷进缓冲；⑤ 若 remainder 非空则 `platform_write(this, buf, write_size)` 冲刷缓冲，再处理 remainder。这里 `platform_write` 的调用点就是缓冲层与平台层的**唯一接缝**。

行缓冲的换行查找：

[`src/__support/File/file.cpp:187-205`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/File/file.cpp#L187-L205) —— 从后往前找最后一个 `\n`，没找到就按全缓冲处理；找到则换行点之前（含换行）走无缓冲立即写出并 `flush`，之后的部分走全缓冲。

冲刷逻辑（写时写出缓冲、读时回退多余预读）：

[`src/__support/File/file.cpp:441-460`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/File/file.cpp#L441-L460) —— `flush_unlocked` 根据 `prev_op` 区分：写则把 `buf[0..pos)` 经 `platform_write` 输出并 `pos=0`；读则用 `platform_seek` 把「多预读的 `read_limit - pos` 字节」退回去（对管道等不可定位文件忽略 seek 错误，符合 POSIX）。

#### 4.2.4 代码实践

**目标**：用一个具体数字走一遍 `write_unlocked_fbf` 的分割逻辑，确认你理解了「多少进缓冲、多少直接写」。

**操作步骤**（纸笔推演型）：

1. 假设 `bufsize = 1024`，当前 `pos = 1000`（缓冲里已攒 1000 字节），调用 `write(data, 5000)`。
2. 计算：`bufspace = 1024 - 1000 = 24`；`bufspace + bufsize = 24 + 1024 = 1048`。
3. 判断：`len(5000) > 1048` 成立吗？成立 → 直接退化走 `write_unlocked_nbf`（先冲刷已有 1000 字节，再把 5000 字节直接 `platform_write`），**完全不进缓冲**。
4. 再假设改为 `write(data, 50)`：`len(50) ≤ 1048`，`split_point = min(50, 24) = 24`。前 24 字节（primary）拷进缓冲 `buf[1000..1024)`，`pos` 变 1024（满了）；remainder = 26 字节，触发 `platform_write(buf, 1024)` 冲刷，`pos=0`；26 < 1024，于是把 26 字节拷进空缓冲，`pos=26`，返回 `len=50`。

**需要观察的现象**：第二种情形下一次用户 `write(50)` **没有立刻发起系统调用写那 50 字节**——只有缓冲满时那一次 `platform_write(buf,1024)`，剩余 26 字节静静躺在缓冲里，等下次写满或 `fflush`/`fclose` 才真正落盘。

**预期结果**：你能解释「为什么全缓冲能减少系统调用次数」——多次小写被攒成一次大写。**待本地验证**：可对照 [`file.cpp:113-185`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/File/file.cpp#L113-L185) 的变量名逐步代入数值，确认每一步与上面一致。

#### 4.2.5 小练习与答案

**练习 1**：`prev_op` 字段为什么必须存在？`flush_unlocked` 是如何利用它的？

**参考答案**：因为读和写共用同一片 `buf`，必须知道「上一次是读还是写」才能正确处理残留数据。`flush_unlocked` 据此分两种情况：若 `prev_op == WRITE`，要把缓冲里已写的数据 `platform_write` 输出；若 `prev_op == READ`，则要用 `platform_seek` 把多预读的字节退回内核（因为缓冲里读过的位置和内核文件位置不一致）。

**练习 2**：`adjust_buf()`（构造函数末尾调用）为什么要在一个「可读但没有缓冲」的文件上塞一个 4 字节的 `ungetc_buf`？

**参考答案**：C 标准要求即便用户不提供缓冲，也要至少支持一次 `ungetc`（把一个字符「退回」流里）。`adjust_buf` 在「可读且 `buf` 为空」时把 `buf` 指向内部的 4 字节 `ungetc_buf`（够放一个宽字符），从而满足这条语义；注释里三种情形说明了它不会改变用户可见的缓冲行为。见 [`file.h:359-379`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/File/file.h#L359-L379)。

---

### 4.3 平台子目录：generic 与 linux/gpu/baremetal 的分工

#### 4.3.1 概念说明

`src/stdio/` 下有两类实现目录：

- **`generic/`**：放**平台无关**的入口点壳。`fopen.cpp`、`fwrite.cpp`、`fread.cpp` 等都只做「强转 + 调 `File` 方法 + 设 errno」，不知道自己跑在什么 OS 上。
- **`<os>/`（如 `linux/`）**：放**必须碰平台**的入口点。例如 `linux/stdin.cpp`/`stdout.cpp`/`stderr.cpp`（标准流需要预先绑定的 fd）、`linux/fdopen.cpp`（把现成 fd 包成 `FILE*`）、`linux/remove.cpp`/`rename.cpp`（需要 `unlink`/`rename` 系统调用）。还有 `gpu/`、`baremetal/` 目录各自处理特殊目标。

`__support/File/` 也是同样的两层布局：根目录的 `file.cpp` 是平台无关的缓冲内核；`__support/File/linux/file.cpp` 是 Linux 特化（提供 `openfile`、`LinuxFile`、四个 `linux_file_*` 函数）；`gpu/`、`baremetal/` 各有等价物。

关键问题：**CMake 如何为某个入口点在「平台版」与「generic 版」之间选一个？** 答案是一个名叫 `add_stdio_entrypoint_object` 的辅助函数（以及类似的 `add_generic_entrypoint_object`）：**「平台优先，generic 兜底」**。

#### 4.3.2 核心流程

构建期（CMake 配置时）的目标选择逻辑：

```text
add_stdio_entrypoint_object(fwrite)            # 想注册公开入口点 fwrite
 │
 ├─ 若存在目标 libc.src.stdio.${LIBC_TARGET_OS}.fwrite   (如 libc.src.stdio.linux.fwrite)
 │     → 把 fwrite 建成它的 ALIAS（用平台版）
 │
 └─ 否则若存在目标 libc.src.stdio.generic.fwrite
       → 把 fwrite 建成 generic.fwrite 的 ALIAS（用兜底版）
```

也就是说，**哪个平台实现了同名目标就用哪个，没实现就回退到 `generic`**。这样新平台只需把「必须特化」的少数函数放进自己的 `<os>/` 目录，其余自动继承 `generic` 实现。

同样，`__support/File/CMakeLists.txt` 把 `platform_file` 建成一个指向当前 OS 实现的 ALIAS：

```text
set(target_file libc.src.__support.File.${LIBC_TARGET_OS}.file)   # 如 ...File.linux.file
add_object_library(platform_file ALIAS ${target_file})
```

于是入口点壳只需 `DEPENDS libc.src.__support.File.platform_file`，就自动拿到了「当前平台的 openfile / 四个钩子函数」，无需关心具体是 Linux 还是 GPU。

#### 4.3.3 源码精读

stdio 的「平台优先、generic 兜底」选择器：

[`src/stdio/CMakeLists.txt:3-19`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/stdio/CMakeLists.txt#L3-L19) —— `add_stdio_entrypoint_object` 用 `if(TARGET libc.src.stdio.${LIBC_TARGET_OS}.${name})` 判断是否有平台版，有则 ALIAS 到平台版，否则 ALIAS 到 `generic.${name}`。

平台子目录的纳入顺序：

[`src/stdio/CMakeLists.txt:21-24`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/stdio/CMakeLists.txt#L21-L24) —— 先 `add_subdirectory(${LIBC_TARGET_OS})`（若目录存在），再 `add_subdirectory(generic)`。平台版目标因此先于 generic 注册，上面的选择器才能「优先匹配到平台版」。

`platform_file` 别名的定义（这是 4.1 里「`openfile` 由谁实现」的答案）：

[`src/__support/File/CMakeLists.txt:53-64`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/File/CMakeLists.txt#L53-L64) —— 若当前 OS 子目录存在则 `add_subdirectory(${LIBC_TARGET_OS})`，再把 `platform_file` 建成指向 `libc.src.__support.File.<os>.file` 的 ALIAS。Linux 上就是 `...File.linux.file`（即 `linux/file.cpp` 编译出的目标）。

`fopen` 在 generic 目录的注册，`DEPENDS` 里同时拉了缓冲内核与平台文件：

[`src/stdio/generic/CMakeLists.txt:175-185`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/stdio/generic/CMakeLists.txt#L175-L185) —— 注意 `DEPENDS` 含 `libc.src.__support.File.file`（缓冲内核）与 `libc.src.__support.File.platform_file`（平台 `openfile`）。这两条 `DEPENDS` 正是 u2-l3 讲过的「同时承担构建顺序与头文件路径传播」。

#### 4.3.4 代码实践

**目标**：判断若干 stdio 入口点该放在 `generic/` 还是 `linux/`，并说出理由。

**操作步骤**（分类练习型）：

1. 浏览 [`src/stdio/`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/stdio) 与 [`src/stdio/linux/`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/stdio/linux) 两个目录的文件清单。
2. 对下面每个函数，判断它「是否必须碰平台底层」，进而判断应放在哪一层。

**需要观察的现象 / 预期分类**：

| 函数 | 实现位置 | 理由 |
| --- | --- | --- |
| `fwrite` | `generic/` | 只做强转 + 调 `File::write`，平台无关 |
| `fread` | `generic/` | 只做强转 + 调 `File::read`，平台无关 |
| `fopen` | `generic/`（壳）+ `__support/File/linux/`（`openfile`） | 壳平台无关；真正打开文件由 `platform_file` 的 `openfile` 完成 |
| `stdin`/`stdout`/`stderr` | `linux/` | 标准流要在进程启动时绑定具体 fd（0/1/2），属平台行为 |
| `fdopen` | `linux/` | 直接消费一个现成的 fd，是 Unix 概念 |
| `remove`/`rename` | `linux/` | 需要 `unlink`/`rename` 系统调用 |

**预期结果**：你能总结出一条规律——**「只调 `File` 方法的」放 generic，「需要 fd / 特定系统调用 / 启动期绑定」的放 `<os>/`**。**待本地验证**：可逐一打开上表中的 `.cpp` 文件，确认 generic 版确实只 `reinterpret_cast` + 调 `File` 方法。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `stdout` 不能也放进 `generic/`，像 `fwrite` 那样写一个平台无关的实现？

**参考答案**：因为 `stdout` 是一个**全局 `FILE *` 对象**，它的 `File` 实例必须在程序启动时就绑定到一个具体的输出汇（Linux 上是 fd 1）。这个「绑定 fd 1」的动作是平台相关的，所以 `stdout.cpp` 必须放在 `linux/`（以及 `gpu/`、`baremetal/` 各自的等价实现）里。`fwrite` 之所以能 generic，是因为它操作的是「已经构造好的 `File *`」，不关心这个 `File` 是怎么来的。

**练习 2**：`platform_file` 为什么是一个 ALIAS 而不是一个有自己源文件的库？

**参考答案**：因为不同平台提供 `openfile`/钩子函数的源文件不同（Linux 是 `File/linux/file.cpp`，GPU 是 `File/gpu/...`）。用 ALIAS 把 `platform_file` 指向「当前 OS 的那个目标」，上游入口点就能统一写 `DEPENDS libc.src.__support.File.platform_file` 而无需用 `if/else` 区分 OS——这是一处典型的「用别名抹平平台差异」的构建手法。

---

### 4.4 与 OSUtil 的衔接：从 platform_write 到 syscall

#### 4.4.1 概念说明

4.2 里反复出现的 `platform_write(this, buf, n)` 到底调用的是什么？答案就是本模块的主题：**它调用的是平台子类填进来的函数指针**。在 Linux 上，这个指针指向 `linux_file_write`，后者调用 `linux_syscalls::write`，再后者调用 u8-l1 讲过的 `syscall_impl<ssize_t>(SYS_write, ...)`——最终发出 x86_64 的 `syscall` 指令（或 aarch64 的 `svc`、riscv 的 `ecall`）进入内核。

这样就把整条链路接通了：

```text
用户代码 fwrite(...)
   │  [入口点壳，generic]
   ▼
File::write  ──[加锁]──► write_unlocked ──[缓冲]──► platform_write(this, buf, n)
   │  [缓冲内核，__support/File/file.cpp，平台无关]
   ▼
linux_file_write(f, data, size)        ← File 构造时由 LinuxFile 填入的函数指针
   │  reinterpret_cast<LinuxFile*>(f) 取 fd
   ▼
linux_syscalls::write(fd, data, size)   ← OSUtil syscall wrapper
   │  if (ret < 0) return Error(-ret);
   ▼
syscall_impl<ssize_t>(SYS_write, fd, buf, count)   ← u8-l1 的内联汇编系统调用
   │
   ▼
Linux 内核 (sys_write)
```

错误传播方向也完全沿用 u8-l1/u4-l3 建立的约定：内核出错返回负 `errno` → `linux_syscalls::write` 用 `ret < 0` 判错、取反成正 `errno` 包进 `Error` → `linux_file_write` 把它映射成 `FileIOResult{0, error}` → 缓冲层据此置 `err` 标志 → `fwrite` 入口点把 `result.error` 写进 `libc_errno` 并返回。**内部传错用 `ErrorOr`/`FileIOResult`，对外报告用 `errno`，两层解耦**——这正是 u4-l3 的端到端模式在文件 I/O 上的复现。

#### 4.4.2 核心流程

`openfile`（`fopen` 的真正实现）在 Linux 上做四件事：解析模式、发起 `open(2)`、分配缓冲、构造 `LinuxFile` 并登记进全局链表。其中**构造 `LinuxFile` 这一步**就把四个钩子函数（`linux_file_write` 等）焊进了 `File` 的函数指针——这是「平台相关」与「平台无关」两部分接合的瞬间。

写数据的真正系统调用发生在缓冲层决定冲刷时。以全缓冲为例（4.2 的 `write_unlocked_fbf`）：缓冲满了 → `platform_write(this, buf, write_size)` → 经函数指针跳到 `linux_file_write` → `linux_syscalls::write(lf->get_fd(), data, size)` → `syscall_impl(SYS_write, ...)`。

#### 4.4.3 源码精读

钩子函数 `linux_file_write`——注意它如何强转取 fd 再发起系统调用：

[`src/__support/File/linux/file.cpp:29-36`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/File/linux/file.cpp#L29-L36) —— 把 `File *f` 强转为 `LinuxFile *` 取 `fd`，调用 `linux_syscalls::write`。失败（`!ret`）时返回 `FileIOResult{0, ret.error()}`，成功返回写入字节数。`read`/`seek`/`close` 三个兄弟函数结构完全一样（同文件 38-61 行）。

`openfile`——`fopen` 在 Linux 上的真正实现：

[`src/__support/File/linux/file.cpp:63-112`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/File/linux/file.cpp#L63-L112) —— 关键步骤：① `File::mode_flags(mode)` 把 `"r"/"w"/"a"/"+"` 解析成 `OpenMode` 位标志（见 [`file.cpp:513-556`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/File/file.cpp#L513-L556)）；② 据此拼出 Linux 的 `open_flags`（`O_CREAT|O_TRUNC` 等）；③ `linux_syscalls::open(path, open_flags, OPEN_MODE)` 发起 `open(2)`，权限默认 `0666`；④ `new uint8_t[DEFAULT_BUFFER_SIZE]` 分配 1024 字节缓冲；⑤ `new LinuxFile(fd, buffer, DEFAULT_BUFFER_SIZE, _IOFBF, true, modeflags)` 构造对象——**默认全缓冲**；⑥ `File::add_file(file)` 登记进全局链表。

系统调用薄封装 `linux_syscalls::write`——这就是 u8-l1 讲的 `syscall_impl` 的具体消费者：

[`src/__support/OSUtil/linux/syscall_wrappers/write.h:22-27`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/OSUtil/linux/syscall_wrappers/write.h#L22-L27) —— 一行 `syscall_impl<ssize_t>(SYS_write, fd, buf, count)` 发起系统调用，再用 `ret < 0` 判错、取反包成 `Error`。这里 `SYS_write` 来自内核头 `<sys/syscall.h>`，`syscall_impl` 来自 u8-l1 的 [`OSUtil/linux/syscall.h`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/OSUtil/linux/syscall.h)。整段代码与 u8-l1 末尾「`syscall_wrappers` 返回 `ErrorOr`」的描述严丝合缝。

**与 u7-l2 的衔接**：printf 的输出最终也走这条 `File::write` 路径。`vfprintf` 把格式化结果通过 printf_core 的 `Writer` 排出，而 `Writer` 的输出钩子 `file_write_hook` 调用的正是 `File::write_unlocked`：

[`src/__support/printf_core/vfprintf_internal.h:65-69`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/printf_core/vfprintf_internal.h#L65-L69) —— `file_write_hook` 把 `::FILE *fp` 强转为 `File *`，调用其 `write_unlocked`（同文件 L39-L41 的内部 `fwrite_unlocked` 转发）。于是 u7-l2 的 `FlushingBuffer`（见同文件 L89）经此钩子把格式化字节交给本讲的缓冲 `File`，再经 `platform_write` 落到内核。**printf 与 fwrite 共享同一条「缓冲 → 系统调用」管道**。

#### 4.4.4 代码实践（本讲主实践）

**目标**：跟踪 `fwrite` 从入口点到 Linux 内核的完整调用链，标注每一层所在的文件与职责。这是本讲规格指定的核心实践任务。

**操作步骤**（调用链跟踪型）：

1. **入口点壳**：读 [`src/stdio/generic/fwrite.cpp:19-30`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/stdio/generic/fwrite.cpp#L19-L30)。确认它 `reinterpret_cast<File*>` 后调 `stream->write(buffer, size*nmemb)`。
2. **缓冲加锁**：跳到 [`src/__support/File/file.h:194-197`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/File/file.h#L194-L197)，确认 `write()` 加锁后委托 `write_unlocked`。
3. **模式分派**：跳到 [`src/__support/File/file.cpp:73-91`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/File/file.cpp#L73-L91)，确认默认 `_IOFBF` 走 `write_unlocked_fbf`。
4. **缓冲冲刷**：跳到 [`file.cpp:152`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/File/file.cpp#L152)（缓冲满时的 `platform_write(this, buf, write_size)`）。这就是缓冲层与平台层的接缝。
5. **平台钩子**：跳到 [`src/__support/File/linux/file.cpp:29-36`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/File/linux/file.cpp#L29-L36)，确认 `linux_file_write` 取 fd 后调 `linux_syscalls::write`。
6. **系统调用薄封装**：跳到 [`src/__support/OSUtil/linux/syscall_wrappers/write.h:22-27`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/OSUtil/linux/syscall_wrappers/write.h#L22-L27)，确认它调 `syscall_impl<ssize_t>(SYS_write, ...)`——这正衔接上 u8-l1 讲的汇编系统调用。

**需要观察的现象**：六层调用，每一层都只做一件小事，且平台相关代码被压缩在最底两层（`linux/file.cpp` 与 `OSUtil`）。上面四层（入口壳、加锁、分派、缓冲）完全平台无关。

**预期结果**：你能画出上面的调用链图，并回答「`fwrite` 最终如何通过 OSUtil 真正写出数据」——经 `File` 的函数指针 `platform_write` 跳到 `linux_file_write`，再由 `linux_syscalls::write` 调 u8-l1 的 `syscall_impl(SYS_write)` 进入内核。**待本地验证**：可参照 [`test/src/stdio/fopen_test.cpp:20-46`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/test/src/stdio/fopen_test.cpp#L20-L46) 的端到端测试，在本地构建后用 `strace` 观察一次 `fwrite` 触发的 `write(2)` 系统调用次数（全缓冲下应远少于 `fwrite` 调用次数）。

#### 4.4.5 小练习与答案

**练习 1**：`linux_file_write` 为什么要先 `reinterpret_cast<LinuxFile *>(f)` 而不能直接用 `f`？

**参考答案**：因为它需要 `fd` 才能发起 `write(fd, ...)` 系统调用，而 `fd` 是 `LinuxFile` 子类才有的字段，父类 `File` 看不到。强转回子类才能取到 fd。这也解释了为什么「存 fd」这件事必须由平台子类承担。

**练习 2**：如果要把 `fwrite` 的数据最终写到一个 GPU 的内存缓冲区（而非 Linux 文件），需要改动哪一层？

**参考答案**：只需提供一个新的 `__support/File/gpu/file.cpp`（实现 `gpu_file_write` 等钩子与 `openfile`），并在其中把 `LinuxFile` 换成 GPU 版的子类、把 `linux_syscalls::write` 换成 GPU 的等价操作。缓冲内核 `__support/File/file.cpp`、入口点壳 `src/stdio/generic/fwrite.cpp` 都**不用改**——这正是函数指针解耦带来的可移植性。这也是 `gpu/` 目录真实存在的意义。

**练习 3**：一次 `fwrite(buf, 1, 3, fp)`（fp 为全缓冲、缓冲尚空）发生后，会立即触发 `write(2)` 系统调用吗？

**参考答案**：不会。3 字节远小于 1024 字节缓冲，`write_unlocked_fbf` 会把 3 字节拷进缓冲、`pos=3` 后直接返回，不调 `platform_write`。要等到缓冲写满、或调用 `fflush`/`fclose` 时，才会经 `linux_file_write` 发起 `write(2)`。`fclose` 里 [`file.h:254-277`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/File/file.h#L254-L277) 正是先冲刷残留缓冲再 `platform_close`。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「**fopen → fwrite → fclose → fread 往返**」的全链路剖析。

**任务**：参照测试 [`test/src/stdio/fopen_test.cpp`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/test/src/stdio/fopen_test.cpp)（它先 `fopen("w")` 写一个字符串，再 `fopen("r")` 读回比对），为这次往返填写下表，每一行都要指明**所在文件、所属层（公开类型/入口壳/缓冲内核/平台特化/OSUtil）、平台相关性**：

| 阶段 | 调用 | 文件 | 层 | 平台相关？ |
| --- | --- | --- | --- | --- |
| 打开 | `fopen` 壳 | `src/stdio/generic/fopen.cpp` | 入口壳 | 否 |
| 打开 | `openfile` | `src/__support/File/linux/file.cpp` | 平台特化 | 是 |
| 打开 | `linux_syscalls::open` | `src/__support/OSUtil/linux/syscall_wrappers/open.h` | OSUtil | 是 |
| 写 | `fwrite` 壳 | `src/stdio/generic/fwrite.cpp` | 入口壳 | 否 |
| 写 | `File::write` / `write_unlocked_fbf` | `src/__support/File/file.cpp` | 缓冲内核 | 否 |
| 写 | `linux_file_write` | `src/__support/File/linux/file.cpp` | 平台特化 | 是 |
| 写 | `linux_syscalls::write` | `OSUtil/.../write.h` | OSUtil | 是 |
| 关闭 | `File::close`（先冲刷） | `src/__support/File/file.h` | 缓冲内核 | 否 |
| 读 | `fread` 壳 → `File::read` → `read_unlocked_fbf` | `generic/fread.cpp` + `file.cpp` | 入口壳+缓冲 | 否 |
| 读 | `linux_file_read` → `linux_syscalls::read` | `linux/file.cpp` + `OSUtil` | 平台+OSUtil | 是 |

**检查点**：

1. 解释为什么「写」阶段用户调了 `fwrite` 却**可能没有**立即产生 `write(2)`（答：全缓冲攒数据），而 `fclose` 阶段**一定会**产生一次 `write(2)`（冲刷残留缓冲，见 [`file.h:257-263`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/File/file.h#L257-L263)）。
2. 指出整条链路里「平台无关」与「平台相关」的分界线在哪一行代码（答：缓冲层调用 `platform_write(this, buf, n)` 的瞬间，见 [`file.cpp:152`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/File/file.cpp#L152)）。
3. 说出错误从内核传到用户程序的路径：内核负 `errno` → `Error(-ret)` → `FileIOResult{0,error}` → `err=true` → `libc_errno = result.error`（参考 u4-l3）。

**预期产出**：一张填满的表格加三段解释。**待本地验证**：若有本地构建环境，用 `strace -e trace=openat,write,read,close ./test` 跑该测试，对比系统调用次数与 `fwrite`/`fread` 调用次数，直观感受缓冲的效果。

## 6. 本讲小结

- **不透明的 `::FILE` 与内部的 `File`**：公开类型只是 `typedef struct FILE FILE;`，实现代码用 `reinterpret_cast` 在 `::FILE *` 与 `File *` 之间互转——二者是同一个对象。
- **函数指针解耦平台**：`File` 类用 `platform_write/read/seek/close` 四个函数指针把「平台无关的缓冲逻辑」与「平台相关的系统调用」分离；`LinuxFile` 子类在构造时填入这四个钩子。
- **三种缓冲模式**：`File::write` 按 `bufmode` 分派到 `_IOFBF`（全缓冲，分割点算法）/`_IOLBF`（行缓冲，按 `\n`）/`_IONBF`（无缓冲，立即冲刷）；缓冲区状态由 `pos`/`read_limit`/`prev_op` 维护。
- **平台子目录分工**：`generic/` 放平台无关入口壳，`<os>/` 放必须碰平台的实现；CMake 用「平台优先、generic 兜底」的 `add_stdio_entrypoint_object` 选择，`platform_file` 别名抹平 OS 差异。
- **与 OSUtil 的衔接**：`fwrite` → `File::write`（缓冲）→ `platform_write` → `linux_file_write` → `linux_syscalls::write` → u8-l1 的 `syscall_impl(SYS_write)` → 内核；错误按 u4-l3/u8-l1 的约定层层翻译，内部用 `ErrorOr`/`FileIOResult`、对外用 `errno`。
- **与 u7-l2 的汇合**：printf 的 `Writer` 经 `file_write_hook` 调 `File::write_unlocked`，于是 `fprintf` 与 `fwrite` 共享同一条「缓冲 → 系统调用」管道。

## 7. 下一步学习建议

- **承接 u9（内存管理与并发）**：本讲的 `File` 类已经用到了 `__support/threads/mutex`（`FileLock` 的底层）。u9-l2 会深入讲解 `raw_mutex`/`futex` 如何实现这把锁，建议读完后回头确认 `File::mutex` 的构造参数（`timed=false, recursive=false, ...`）对应了哪种锁语义。
- **标准流的启动期构造**：本讲提到 `stdout`/`stdin`/`stderr` 必须在启动时绑定 fd。建议阅读 `src/stdio/linux/stdout.cpp` 与 u8-l2 的 `do_start`，理解「全局 `File` 对象如何在 `main` 之前就绑好 fd 1」。
- **`fopencookie` 与自定义流**：`src/stdio/fopencookie.cpp` 展示了如何不经过 `openfile`、由用户自定义四个钩子来构造一个 `File`，这是函数指针解耦设计的另一面实例，可作为本讲的延伸阅读。
- **跨平台对照**：浏览 `src/__support/File/gpu/` 与 `src/__support/File/baremetal/`（若存在），对比它们如何替换 `linux_file_*` 钩子，体会「换平台 ≈ 换钩子 + 换 config」的可移植性，为 u11-l2（特殊目标）做铺垫。
