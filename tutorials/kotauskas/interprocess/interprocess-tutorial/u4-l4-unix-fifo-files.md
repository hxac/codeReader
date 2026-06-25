# Unix FIFO 文件

## 1. 本讲目标

前面几讲我们一直在围绕 local socket 展开：它是 interprocess 在底层原语之上构造的「跨平台抽象」。本讲换个视角，来看一个 **interprocess 没有给它套抽象外壳、几乎原样暴露** 的原语——Unix 的 **FIFO 文件**（也就是 Unix 意义上的「命名管道」）。

interprocess 对 FIFO 的封装只有一个函数 `create_fifo()`，读、写、删除则完全交给标准库的 `std::fs::File` 与 `std::fs::remove_file`。正因为封装这么薄，FIFO 是理解「IPC 原语本身长什么样」的最佳入口，也能帮我们看清 local socket / unnamed pipe / FIFO 三者各自的位置。

学完本讲，你应当能够：

1. 说清 **FIFO 文件**是什么：它是一个落在文件系统里的（伪）文件，行为像一条单向字节管道，可被互不相关的进程通过一个已知路径访问。
2. 区分 FIFO 与另外两种容易混淆的原语——**unnamed pipe**（匿名管道）和 **Windows named pipe**（Windows 命名管道），并理解为什么 interprocess 特意把 Unix 版本叫做「FIFO 文件」而不是「named pipe」。
3. 读懂 `create_fifo()` 如何把一个 Rust `Path` 翻译成 C 字符串、调用 libc 的 `mkfifo`、再用 `OrErrno` 把「成功/失败」翻译成 `io::Result`。
4. 掌握 `mode` 参数与进程 `umask` 的关系，并知道为什么文档建议「除非有特殊需求，否则传 `0o777`」。
5. 能够用标准 `File` 打开一个 FIFO，在一个进程写、另一个进程读，结束后用 `remove_file` 清理。

> ⚠️ 本讲全部内容**只在 Unix 平台有效**。`fifo_file` 模块由 `#[cfg(unix)]` 门控，Windows 上根本不编译；实践代码也需要在 Linux / macOS / BSD 等 Unix 系统上运行。

## 2. 前置知识

- **进程间通信（IPC）**：两个独立进程之间交换数据的机制。本讲的 FIFO 是其中一种「文件型」IPC。
- **管道（pipe）**：内核维护的一段缓冲区，一头写入、另一头读出，数据按写入顺序流出，是**字节流**（不保留消息边界）。
- **匿名管道（unnamed pipe）**（见 u5-l1）：由 `pipe()` 创建，只通过句柄/文件描述符访问，一端关闭即失效，典型用于父子进程通信。
- **文件描述符（fd）/ `RawFd`**：Unix 里「一切皆文件」，打开的文件、管道、socket 都用一个整数 fd 表示。
- **`io::Result`**：Rust 标准库的「可能失败」返回类型，`Ok(T)` 表示成功、`Err(io::Error)` 表示失败。本讲会看到 interprocess 如何把 C 风格的「返回 `-1` 表示出错」翻译成它。
- **`umask`**：每个进程持有一个权限屏蔽字，创建文件/目录时，最终权限 = 你请求的权限 **去掉** umask 中置位的那些位。
- **「命名管道」一词的歧义**（见 u1-l1）：Windows 的 named pipe 和 Unix 的 FIFO 都被口语称作「named pipe」，但二者能力差异巨大。interprocess 为避免混淆，把 Unix 版本一律称作 **FIFO 文件**。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/os/unix.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix.rs) | Unix 平台模块总入口：声明子模块（`pub mod fifo_file;`）并在模块文档里定位 FIFO 的概念。 |
| [src/os/unix/fifo_file.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/fifo_file.rs) | 本讲主角：整个模块只导出一个函数 `create_fifo()`，内部调用 `libc::mkfifo`。 |
| [src/misc.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/misc.rs) | 私有工具：`OrErrno` trait 与 `true_val_or_errno`，把「布尔返回值 + `errno`」翻译成 `io::Result`。 |

> 本讲的「最小模块」是 `os::unix::fifo_file`。`src/misc.rs` 里的 `OrErrno` 是理解它错误处理风格所必需的支撑，但属于全库共用的工具，不是 FIFO 专属。

## 4. 核心概念与源码讲解

### 4.1 FIFO 文件是什么：命名管道与文件系统的交汇

#### 4.1.1 概念说明

先把三种「管道类」原语摆在一起对比，FIFO 的定位就清楚了：

| 原语 | 是否在文件系统里 | 方向 | 连接关系 | 谁能用 |
|------|------------------|------|----------|--------|
| **unnamed pipe**（u5-l1） | 否，只有句柄/fd | 单向 | 一发一收 | 必须有亲缘（如父子）进程，靠句柄继承传递 |
| **FIFO 文件**（本讲） | 是，是个文件 | 单向 | 一发一收 | 任意进程，只要知道路径就能打开 |
| **Windows named pipe**（u4-l3） | 路径形如 `\\.\pipe\名字`，但非真文件 | 双工，可保留消息边界 | 一个服务端 + 多个客户端并发连接 | 任意进程，按名字连接 |

可以看到 FIFO 的关键特征是「**单向 + 落在文件系统 + 凭路径访问**」：

- **单向**：数据只能从写端流向读端，一条 FIFO 不能既读又写（想做双向通信，就开两条 FIFO）。
- **落在文件系统**：`create_fifo()` 之后，`ls` 能看到一个文件（类型标识为 `p`，即 pipe），`stat` 它的文件类型是「FIFO」而非普通文件或目录。
- **凭路径访问**：因为是文件，**两个互不相干、谁也没有 spawn 谁的进程**也能通过约定好的路径通信——这正是它相对 unnamed pipe 的最大优势。

interprocess 在模块文档里还点明了 FIFO 的两个**使用约束**（这是 FIFO 固有的语义，不是 interprocess 加的）：

- 如果有**额外的接收者**也打开同一条 FIFO，它什么也读不到；
- 如果有**额外的发送者**同时写入，数据会**不可预测地混杂**在一起，变得不可用。

也就是说，FIFO 的设计意图就是「**方便地用一条像管道一样工作的路径，把恰好两个应用连起来，仅此而已**」。

#### 4.1.2 核心流程

一条 FIFO 从创建到销毁的典型生命周期：

```
进程 A: create_fifo("/tmp/myfifo", 0o777)   // 在文件系统里种下一个 FIFO 文件
              │
              │  （此时 FIFO 已存在，但还没有任何写者/读者打开它）
              ▼
进程 B (发送端): File::create 或 OpenOptions::new().write(true).open("/tmp/myfifo")
进程 C (接收端): File::open("/tmp/myfifo")
              │
              │  内核把写入的字节流缓冲，按顺序交给读端
              ▼
进程 B 写、进程 C 读 ……  （字节流，无消息边界）
              │
              ▼
通信结束: 任意一方 remove_file("/tmp/myfifo")   // 当普通文件删掉
```

#### 4.1.3 源码精读

interprocess 在 `os::unix.rs` 的模块文档里，开篇就给 FIFO 下了定义，并把它和 unnamed pipe、Windows named pipe 做了切割：

[src/os/unix.rs:L4-L10](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix.rs#L4-L10) —— 文档说明 FIFO 是「单向字节通道、行为像文件、但因落在文件系统上而可被无关应用访问」，并强调它「在所有受支持的系统上都可用」。

`fifo_file` 作为公共子模块在这里被声明（Unix 专有，故整个 `os::unix` 都在 `#[cfg(unix)]` 之下）：

[src/os/unix.rs:L20](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix.rs#L20) —— `pub mod fifo_file;` 把 FIFO 模块导出为 `interprocess::os::unix::fifo_file`。

FIFO 模块自身的文档进一步把它和 Windows named pipe 对比：Windows 的 named pipe 更接近 Unix domain socket（多连接、双工、可保留消息边界），而 Unix 的「named pipe」即 FIFO，只是单向、无消息边界的字节文件。这正是 interprocess 特意改名「FIFO 文件」的原因：

[src/os/unix/fifo_file.rs:L1-L20](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/fifo_file.rs#L1-L20) —— 模块文档对比 Windows named pipe 与 Unix FIFO，并给出「额外接收者读不到、额外发送者会让数据混杂」的约束，以及「用 `create_fifo()` 创建、用标准 `File` 读写、用 `remove_file()` 删除」的用法小结。

#### 4.1.4 代码实践

**实践目标**：用肉眼确认 FIFO 确实是文件系统里的一个「文件」，并且文件类型是 FIFO（而非普通文件）。

**操作步骤**：

1. 在一个 Unix 终端里，写一个最小程序调用 `interprocess::os::unix::fifo_file::create_fifo`。
2. 运行它，然后**不要关闭终端**，用 `ls -l` 和 `stat` 查看创建出来的文件。

示例代码（**非项目原有代码，仅为本次实践编写**）：

```rust
// 实践代码，需在 Unix 上运行；Cargo.toml 依赖 interprocess
use interprocess::os::unix::fifo_file::create_fifo;

fn main() -> std::io::Result<()> {
    let path = "/tmp/ipc_tut_fifo";
    create_fifo(path, 0o777)?;
    println!("已创建 FIFO：{}", path);
    println!("现在在另一个终端运行： ls -l {}  和  stat {}", path, path);
    // 注意：这里先不删除，留给你观察。程序退出后 FIFO 文件依然存在。
    Ok(())
}
```

**需要观察的现象**：

- `ls -l` 输出的权限前缀是 `p`（如 `prw-r--r--`），这个 `p` 就表示文件类型是 **pipe / FIFO**。
- `stat` 输出里会有一行类似 `File: /tmp/ipc_tut_fifo`，以及 `Size: 0`，类型描述为 fifo。

**预期结果**：你能看到一个大小为 0、类型为 FIFO 的「文件」静静地躺在 `/tmp` 下——它不会因为创建它的进程退出而消失。

**待本地验证**：不同系统的 `stat` 文案略有差异，以你机器上的实际输出为准。观察完记得 `rm /tmp/ipc_tut_fifo` 清理。

#### 4.1.5 小练习与答案

**练习 1**：为什么 interprocess 把 Unix 的 named pipe 称作「FIFO 文件」，而不是直接叫 named pipe？

> **答案**：因为 Windows 的 named pipe 和 Unix 的 FIFO 能力差异巨大（前者多连接、双工、可保留消息边界，接近 Unix domain socket；后者单向、无消息边界）。如果都叫 named pipe，读者极易混淆。改名「FIFO 文件」能消除歧义。

**练习 2**：如果两个发送者同时往一条 FIFO 写，会发生什么？为什么 interprocess 文档说这「不可用」？

> **答案**：两次 `write` 的字节会按内核调度顺序交错拼接到同一个字节流里，接收端无法区分哪些字节来自哪个发送者，数据被污染，故不可用。FIFO 的语义模型就是「一发一收」。

---

### 4.2 `create_fifo` 与 `mkfifo`：mode 与 umask

#### 4.2.1 概念说明

`create_fifo()` 是 interprocess 对 FIFO 暴露的**唯一**函数。它的签名极其朴素：

```rust
pub fn create_fifo<P: AsRef<Path>>(path: P, mode: mode_t) -> io::Result<()>
```

- `path`：FIFO 在文件系统里的位置，接受任何能 `AsRef<Path>` 的类型（`&str`、`String`、`PathBuf`……）。
- `mode`：希望赋予该 FIFO 的权限位，类型 `mode_t`（即 libc 的 `mode_t`，通常是 `u32`）。
- 返回 `io::Result<()>`：成功为 `Ok(())`，失败把 `errno` 包成 `io::Error`。

它底层只做一件事——调用 POSIX 的 [`mkfifo(pathname, mode)`](https://pubs.opengroup.org/onlinepubs/9699919799/functions/mkfifo.html)。

关于 `mode`，最关键的一点是：**最终落地的权限 = `mode & ~umask`**。`umask` 是进程级的权限屏蔽字，会把你请求的权限位「砍掉」一部分。例如进程 `umask` 是 `0o022`（常见默认值），那么：

\[
\text{最终权限} = \texttt{mode} \;\&\; \sim\texttt{umask} = \texttt{0o777} \;\&\; \sim\texttt{0o022} = \texttt{0o755}
\]

所以文档建议：**除非你有明确的权限需求，否则把 `mode` 设为 `0o777`**，让 `umask` 去做合理的裁剪——这正是 `shell` 里 `mkfifo 名字`（不传 mode）的等价行为。

#### 4.2.2 核心流程

`create_fifo("...", 0o777)` 在 interprocess 内部的执行过程：

```
create_fifo(path, mode)                       // 公共入口，泛型擦除为 _create_fifo
  └─ _create_fifo(path: &Path, mode)          // 私有实现
       1. CString::new(path.as_os_str().as_bytes())   // 路径转 C 字符串（遇内部 NUL 则 Err）
       2. unsafe { libc::mkfifo(c_str_ptr, mode) }    // 系统调用，返回 0 成功 / -1 失败
       3. (返回值 != -1).true_val_or_errno(())        // 翻译成 io::Result<()>
            ├─ 成功(返回 0): Ok(())
            └─ 失败(返回 -1): Err(io::Error::last_os_error())  // 读 errno
```

#### 4.2.3 源码精读

公共入口只是把泛型 `P: AsRef<Path>` 擦除成具体的 `&Path`，这是 Rust 里常见的「泛型外壳 + 私有具体实现」二段式：

[src/os/unix/fifo_file.rs:L38-L40](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/fifo_file.rs#L38-L40) —— `create_fifo` 把 `path.as_ref()` 交给私有 `_create_fifo`，避免泛型把真正逻辑膨胀多份。

真正的系统调用在这几行，是本讲最核心的代码：

[src/os/unix/fifo_file.rs:L41-L45](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/fifo_file.rs#L41-L45) —— `_create_fifo` 先用 `CString::new` 把 `OsStr` 字节序列转成 NUL 结尾的 C 字符串（路径里若含内部 NUL 字节则直接 `Err`），再 `unsafe` 调用 `libc::mkfifo`，用 `(返回值 != -1).true_val_or_errno(())` 翻译结果。

逐个看点：

- `path.as_os_str().as_bytes()`：Unix 上 `OsStr` 就是字节序列，直接取字节。
- `CString::new(...)?`：要求字节里**没有内部 NUL**（NUL 是 C 字符串的结束符）。路径里出现内部 NUL 会在这里变成 `io::Error`（`NulError` 转换而来）。
- `path.as_bytes_with_nul().as_ptr().cast()`：拿到带结尾 NUL 的字节缓冲区指针，`.cast()` 成 `mkfifo` 要的 `*const c_char`。
- `libc::mkfifo(...) != -1`：POSIX 规定 `mkfifo` 成功返回 `0`、失败返回 `-1` 并置 `errno`。这里用 `!= -1` 判定成功。
- `true_val_or_errno(())`：成功就给 `Ok(())`，失败就取 `io::Error::last_os_error()`（即 `errno`）包成 `Err`。

`true_val_or_errno` 来自全库共用的 `OrErrno` trait，定义在 `src/misc.rs`：

[src/misc.rs:L29-L36](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/misc.rs#L29-L36) —— `OrErrno` trait 声明了 `true_or_errno` / `false_or_errno` 两个核心方法，以及 `true_val_or_errno` 这个「成功时返回固定值」的便捷封装。

[src/misc.rs:L37-L45](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/misc.rs#L37-L45) —— 对任意 `ToBool` 类型实现 `OrErrno`：真值走 `Ok(f())`，假值走 `Err(io::Error::last_os_error())`。这就是 interprocess 把 C 风格「返回 -1 + errno」统一翻译成 `io::Result` 的标准模式，`create_fifo` 只是它的一个用例。

#### 4.2.4 代码实践

**实践目标**：直观感受 `mode` 与 `umask` 的关系——传 `0o777`，实际落地的权限却被 `umask` 砍掉一部分。

**操作步骤**：

1. 先查当前 shell 的 `umask`（运行 `umask`，常见为 `0022`）。
2. 运行 4.1.4 的程序（传 `0o777`）创建 `/tmp/ipc_tut_fifo`。
3. `ls -l /tmp/ipc_tut_fifo` 看权限。

**需要观察的现象与预期结果**：

- 若 `umask` 是 `0022`，你大概率看到 `prwxr-xr-x`（即 `0o755`），而不是 `0o777`。这正是 `0o777 & ~0o022 = 0o755`。
- 若想得到完全的 `0o777`，需要在程序里先用 `libc::umask(0)` 清掉屏蔽字（**注意：这会影响整个进程的 umask，属示例探索行为，生产代码慎用**）。

**待本地验证**：你的实际 `umask` 与发行版/用户配置有关，以本机为准。

#### 4.2.5 小练习与答案

**练习 1**：`create_fifo` 为什么不直接接受 `&str`，而要 `P: AsRef<Path>`？

> **答案**：`AsRef<Path>` 是标准库的惯用约定，让调用方可以传 `&str`、`String`、`&Path`、`PathBuf`、`&OsStr` 等多种类型而无需手动转换，API 更通用（和 `std::fs` 的函数风格一致）。

**练习 2**：若路径里含内部 NUL 字节，`create_fifo` 会在哪一步失败？返回什么？

> **答案**：在 `CString::new` 这一步失败，`?` 把 `NulError` 转成 `io::Error` 返回 `Err`——根本到不了 `mkfifo` 系统调用。

---

### 4.3 用标准 `File` 打开、读写与删除 FIFO

#### 4.3.1 概念说明

interprocess 只负责「创建」FIFO，**读、写、删除都复用标准库**——这是 FIFO 作为「文件」的最大便利：

- **读端（接收者）**：`File::open(path)`，得到一个只读 `File`，对其 `Read` 即可取出字节。
- **写端（发送者）**：用 `OpenOptions::new().write(true).open(path)`（或 `File::create`）得到一个只写 `File`，对其 `Write` 即可写入字节。
- **删除**：`std::fs::remove_file(path)`，和删普通文件一模一样。

FIFO 有两条**固有的运行期行为**（POSIX 语义，非 interprocess 规定，实践时务必留意，否则极易「卡住」）：

1. **打开即阻塞**：`open` 一个 FIFO **只读**时，会阻塞到有进程把它**只写**打开为止；反之亦然。所以发送端和接收端谁先启动都行，但**双方都要存在**，先到的那一方会卡在 `open` 上等另一方。
2. **末位写者关闭 → 读端收到 EOF**；**向没有读端的 FIFO 写 → 触发 `SIGPIPE`**（在 Rust 里表现为 `write` 返回 `Err`，通常是 `BrokenPipe`）。

正因为第 1 点，本讲的「两个进程」实践不能把读写放在同一个进程的同一个线程里顺序执行（那样会死锁在 `open` 上），而要**真正开两个进程或两个线程**。

#### 4.3.2 核心流程

一次完整的「写—读—删」：

```
[进程 W 发送端]                          [进程 R 接收端]
OpenOptions::new().write(true)           File::open("/tmp/myfifo")
  .open("/tmp/myfifo")   ──阻塞等待──>     ──阻塞等待──<   (双方 open 同时就绪后都返回)
write_all(b"hello\n")      ──字节流──>     read 到 "hello\n"
flush / 关闭 File          ──EOF───>       read 返回 0 (EOF)
                                         remove_file("/tmp/myfifo")   // 收尾
```

#### 4.3.3 源码精读

FIFO 模块文档的「Usage」小节明确说明了这套「创建用 `create_fifo`、读写用标准 `File`、删除用 `remove_file`」的用法，这也是 interprocess 不再为 FIFO 提供专用读写类型的原因：

[src/os/unix/fifo_file.rs:L16-L20](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/unix/fifo_file.rs#L16-L20) —— 文档「Usage」一节：`create_fifo()` 负责「建」，打开 FIFO 用标准 `File`（要么只发送、要么只接收），删除则和普通文件一样用 `remove_file()`。

注意这里没有任何「读写」源码可精读——因为 interprocess 根本没写。这本身就是一个重要的设计信息：**FIFO 的读写完全等价于普通文件 I/O**，标准库的 `Read` / `Write` trait、`BufReader` / `BufWriter` 缓冲包装都可以直接拿来用。

#### 4.3.4 代码实践

**实践目标**：亲手完成 interprocess 文档建议的完整用法——`create_fifo` 建管道，在**两个独立进程**间完成一次「写—读」，最后 `remove_file` 清理。

**操作步骤**：

1. 把下面这段「示例代码」拆成发送端 `writer` 和接收端 `reader` 两个二进制（或写成一个程序、用命令行参数区分角色）。
2. 在**两个终端**分别运行：先启动哪一个都可以（先启动的会卡在 `open` 等另一个）。
3. 观察接收端打印出的内容，确认数据从发送端流到了接收端。

示例代码（**非项目原有代码，仅为本次实践编写**）：

```rust
// sender.rs —— 发送端
use interprocess::os::unix::fifo_file::create_fifo;
use std::{
    fs::OpenOptions,
    io::Write,
    path::Path,
};

const FIFO: &str = "/tmp/ipc_tut_handshake";

fn main() -> std::io::Result<()> {
    // 发送端负责创建 FIFO（如果已存在，create_fifo 会返回 AlreadyExists，可忽略）
    if !Path::new(FIFO).exists() {
        create_fifo(FIFO, 0o777)?;
    }
    // 只写打开 —— 会阻塞，直到接收端把它只读打开
    let mut f = OpenOptions::new().write(true).open(FIFO)?;
    writeln!(f, "你好，FIFO！这是一条单向消息")?;
    f.flush()?;        // 确保刷到内核缓冲
    // f 在此 drop，接收端随后会读到 EOF
    println!("sender: 已写入并关闭");
    Ok(())
}
```

```rust
// receiver.rs —— 接收端
use std::{fs::File, io::Read};

const FIFO: &str = "/tmp/ipc_tut_handshake";

fn main() -> std::io::Result<()> {
    // 只读打开 —— 会阻塞，直到发送端把它只写打开
    let mut f = File::open(FIFO)?;
    let mut buf = String::new();
    f.read_to_string(&mut buf)?;   // 末位写者关闭后读到 EOF，正常返回
    println!("receiver 收到: {}", buf.trim());

    // 收尾：当作普通文件删除（任一方都可以做）
    std::fs::remove_file(FIFO)?;
    println!("receiver: 已清理 FIFO");
    Ok(())
}
```

> 提示：如果你想用「一个程序」演示，也可以在 `main` 里 `std::thread::spawn` 一个线程当发送端、主线程当接收端——这样不必开两个终端，但要先 `create_fifo` 再启动线程。

**需要观察的现象**：

1. 先启动 `sender`，它会停住（卡在只写 `open`，等接收端）；此时另开终端启动 `receiver`，双方几乎同时解除阻塞。
2. `receiver` 打印出 `receiver 收到: 你好，FIFO！这是一条单向消息`，说明字节确实单向流过去了。
3. `receiver` 随后 `remove_file`，`ls /tmp/ipc_tut_handshake` 应提示文件不存在。

**预期结果**：两个进程通过一个文件系统里的 FIFO 完成一次单向通信，并被清理掉。

**待本地验证**：终端调度与你启动两端的先后顺序会影响「谁先卡住」的现象；如果先把两端都跑起来再观察，`open` 的阻塞通常一闪而过。若先运行 `receiver`、再运行 `sender`，结果是等价的。

#### 4.3.5 小练习与答案

**练习 1**：为什么不能在**同一个线程**里先 `File::open`（只读）再 `write`（写）同一条 FIFO 来做自测？

> **答案**：FIFO 是**单向**的，一条 FIFO 只能要么只读、要么只写打开，不能既读又写；而且 `open` 会对只读端阻塞到有只写端出现。单线程里既当读端又当写端会死锁（自己等自己），所以必须用两个进程/线程、或干脆开两条 FIFO。

**练习 2**：发送端写完关闭 `File` 后，接收端的 `read_to_string` 会一直阻塞吗？

> **答案**：不会。当 FIFO 的最后一个写者关闭（发送端 `File` 被 drop）后，读端的 `read` 会返回 `0`，即 EOF，`read_to_string` 据此正常结束。

**练习 3**：如果没有接收端，发送端却往 FIFO 里 `write`，会发生什么？

> **答案**：内核会向发送端进程投递 `SIGPIPE`，默认终止进程；在 Rust 中 `write` 调用通常表现为返回 `io::ErrorKind::BrokenPipe` 的 `Err`（Rust 运行时把 `SIGPIPE` 处理成错误返回而非默认动作）。

## 5. 综合实践

把本讲三块内容串起来，做一个「**带超时与并发接收者验证**」的小实验，加深对 FIFO 单向、单发单收、`umask` 的理解：

1. 写一个程序，调用 `create_fifo("/tmp/ipc_tut_final", 0o777)` 创建 FIFO，并打印创建后的实际权限（用 `std::fs::metadata` + `PermissionsExt::mode`），对比 `0o777` 与 `umask` 的关系。
2. 启动**两个接收端**进程同时 `File::open` 只读同一条 FIFO；再启动一个发送端写入一句问候。观察哪个接收端拿到了数据、哪个什么也拿不到（验证 4.1 提到的「额外接收者读不到」）。
3. 单独写一个只读打开、但**永远不启动发送端**的接收端，用 `timeout 3 ./receiver` 跑 3 秒，确认它一直阻塞在 `open` 上（验证 4.3 的「打开即阻塞」）。
4. 全部结束后用 `remove_file` 清理 FIFO，并 `ls` 确认它已消失。

> 这个实践综合用到：`create_fifo` + `mode`/`umask`（4.2）、标准 `File` 读写（4.3）、FIFO 单发单收语义（4.1）。如果你愿意，还可以对比 unnamed pipe（u5-l1）的做法，体会「凭路径访问」vs「凭句柄继承」的区别。

## 6. 本讲小结

- **FIFO 文件**是 Unix 的「命名管道」：落在文件系统里的（伪）文件、**单向字节流**、无消息边界，可被任意进程凭路径访问——这是它相对 unnamed pipe 的核心优势。
- interprocess 特意把它叫「FIFO 文件」而非 named pipe，是为了避免和**能力完全不同的 Windows named pipe**（多连接、双工、可保留消息边界）混淆。
- interprocess 对 FIFO 的封装**只有一个函数** `create_fifo(path, mode)`，底层是 libc 的 `mkfifo`；读写删除全部复用标准库的 `File` / `OpenOptions` / `remove_file`。
- `mode` 会被进程的 `umask` 屏蔽，**最终权限 = `mode & ~umask`**，故文档建议无特殊需求时传 `0o777`。
- 错误处理走全库共用的 `OrErrno` / `true_val_or_errno` 模式：把「系统调用返回 `-1` + `errno`」统一翻译成 `io::Result`。
- FIFO 有两条固有运行期行为：**只读/只写 `open` 会阻塞到对端出现**；**末位写者关闭则读端 EOF、无读端则写端 BrokenPipe**——做两个进程的实践时务必留意，否则容易卡死。

## 7. 下一步学习建议

- 想看「**凭句柄而非凭路径**」的管道？继续学习 **u5-l1（匿名管道基础：pipe() 与 Sender/Recver）**，对照理解 unnamed pipe 与 FIFO 的取舍（一个适合父子进程、一个适合无关进程）。
- 想理解 interprocess **为什么给 local socket 套抽象、却对 FIFO 原样暴露**？回顾 **u2-l1（Local Socket 的设计哲学）**，体会「哪些原语值得抽象、哪些直接暴露」的判断依据。
- 对 `mkfifo` 这类系统调用的 unsafe 封装与 `OrErrno` 模式感兴趣？可预习 **u9-l1（unsafe FFI 封装层）**，那里会系统讲解 `c_wrappers` 与全 crate 的 `unsafe` lint 策略。
- 想从「使用」回到「跨平台抽象」？回到 **u4-l1 / u4-l2**，对比 Unix 后端（UDS）与 Windows 后端（named pipe）如何各自实现 local socket，而 FIFO 则始终是 Unix 专有、不参与这层抽象。
