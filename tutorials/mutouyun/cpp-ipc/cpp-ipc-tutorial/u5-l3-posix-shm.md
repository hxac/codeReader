# POSIX 共享内存后端

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚在 Linux/FreeBSD/QNX 上，libipc 是用哪几个 POSIX 系统调用来「开一段跨进程共享内存」的，以及它们的先后顺序。
- 读懂 `shm_posix.cpp` 这 233 行后端，解释 `shm_open` / `ftruncate` / `mmap` / `shm_unlink` 各自承担的职责。
- 理解 `id_info_t` 这个进程本地的小结构如何把 `fd`、映射指针、尺寸和名字串起来。
- 解释 `calc_size` 为什么要对齐、引用计数 `info_t::acc_` 为什么放在共享内存的**末尾 4 字节**。
- 说明 `release` 如何用 `fetch_sub` 的返回值判断「我是不是最后一个使用者」，并在最后一个时调用 `shm_unlink` 删掉磁盘文件。

本讲是 u5-l1（`shm::handle` 公共 API 与跨进程引用计数）和 u5-l2（平台检测与后端分派）的落地篇：前两讲告诉你**接口长什么样**、**由谁分派**，本讲告诉你**在 POSIX 平台上这些接口内部到底调了什么**。

## 2. 前置知识

在进入源码前，先用一段话建立 POSIX 共享内存的直觉。

> 在 Linux 上，`shm_open("/foo", ...)` 会在 `/dev/shm/` 下创建（或打开）一个名为 `foo` 的「内存文件」。`ftruncate` 给它设定长度，`mmap(..., MAP_SHARED, fd, 0)` 把这个文件映射进进程地址空间。因为映射用的是 `MAP_SHARED`，所以**所有 mmap 同一个文件的进程，看到的是同一块物理内存**——这就是「跨进程共享内存」的全部魔法。最后 `shm_unlink("/foo")` 删掉 `/dev/shm/foo` 这个文件名（映射还能继续用，直到所有人都 `munmap`）。

如果你对下面几个概念还不熟，建议先补一下：

- **文件描述符 fd**：Unix「一切皆文件」的句柄。`shm_open` 返回一个 fd，它指向那个内存文件。
- **mmap 与 MAP_SHARED**：把文件（或内存文件）「铺」进进程的虚拟地址空间。`MAP_SHARED` 表示写操作会回写到共享对象上，从而被其他映射者看见。
- **`fetch_sub` / `fetch_add` 的返回值**：原子操作返回的是**修改之前**的旧值。这是理解 `release` 判定「最后一个使用者」的关键。
- **内存序 acquire/release/acq_rel**：u5-l1 已铺垫，本讲会看到它们的具体用法。

另外请回顾 u5-l1 的两个核心结论，本讲会反复用到：

1. 引用计数器 `info_t::acc_` 是一个 4 字节的 `std::atomic<int32_t>`，**直接嵌入共享内存末尾**，靠 `MAP_SHARED` 让所有进程共享。
2. `acquire` 只负责 open，**不**增计数；`get_mem` 才 `fetch_add` 增计数；`release` 才 `fetch_sub` 减计数。

## 3. 本讲源码地图

本讲的核心文件只有一个，但会顺带引用几个把它「串起来」的文件。

| 文件 | 角色 |
| --- | --- |
| [src/libipc/platform/posix/shm_posix.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/shm_posix.cpp) | **本讲主角**。POSIX 后端全部实现：`acquire`/`get_mem`/`get_ref`/`sub_ref`/`release`/`remove`，匿名命名空间里藏着 `info_t`/`id_info_t`/`calc_size`/`acc_of` 四个内部积木。 |
| [include/libipc/shm.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/shm.h) | 公共接口声明。`id_t = void*`、`create`/`open` 模式枚举、`handle` 类。本讲后端函数的签名都来自这里。 |
| [src/libipc/shm.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/shm.cpp) | 桥接层。`handle::acquire` 在这里调用 `shm::acquire` + `shm::get_mem`，把后端接到用户面向的 RAII 句柄上。 |
| [src/libipc/platform/platform.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/platform.cpp) | 编译期分派器。u5-l2 讲过：它用 `#if defined(LIBIPC_OS_LINUX) || ...` 在编译期只 `#include` 一份 `shm_posix.cpp`。 |
| [src/libipc/queue.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h) | 调用方之一。`queue_conn::open` 用 `shm::handle` 申请 `sizeof(Elems)` 字节，让无锁队列躺在共享内存上。 |

读源码前请记住一张「函数到系统调用」的对应表，后面所有精读都围绕它展开：

| libipc 后端函数 | 主要 POSIX 系统调用 |
| --- | --- |
| `acquire` | `shm_open`（+ `fchmod`） |
| `get_mem` | `fstat` 或 `ftruncate` → `mmap` → `close` |
| `get_ref` | 原子 `load`（读末尾计数） |
| `sub_ref` | 原子 `fetch_sub`（只减计数） |
| `release` | 原子 `fetch_sub` → `munmap`（→ 可能 `shm_unlink`） |
| `remove(id)` | `release` + 强制 `shm_unlink` |
| `remove(name)` | 直接 `shm_unlink` |

## 4. 核心概念与源码讲解

### 4.1 id_info_t 与 fd 管理：进程本地的共享内存句柄

#### 4.1.1 概念说明

打开一段共享内存其实需要记三件事：**这个内存文件叫什么名字**、**它当前有多大**、**它映射到了进程地址空间的哪里**。除此之外，在真正 `mmap` 之前，还需要一个临时的**文件描述符 fd**。

`shm_posix.cpp` 用两个小结构来组织这些信息：

- `info_t`：只含一个 4 字节的原子计数器 `acc_`。它**不是进程本地的**，而是要被「塞进共享内存末尾」、被所有进程共享的。
- `id_info_t`：**进程本地**的句柄结构，对外用 `id_t`（即 `void*`，见 [shm.h:11](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/shm.h#L11)）暴露。

#### 4.1.2 核心流程

`id_info_t` 在共享内存的整个生命周期里经历这样的状态流转：

```
acquire 阶段:  fd_ = 有效fd,  mem_ = null,  size_ = 请求大小(或0),  name_ = "/xxx"
get_mem 阶段:  fd_ = -1,      mem_ = 映射基址, size_ = calc_size(...), name_ = "/xxx"
release 阶段:  munmap(mem_),  然后 $delete(id_info_t 自身)
```

注意 **fd 是一次性的**：`get_mem` 调完 `mmap` 会立刻 `close(fd)` 并把 `fd_` 置 `-1`。因为 POSIX 语义下，`mmap` 之后那个 fd 就不再需要了——映射已经独立存活。所以 `fd_` 只在「open 之后、mmap 之前」的短暂时窗里有意义。

#### 4.1.3 源码精读

两个结构的定义与辅助函数都在匿名命名空间里（内部链接，仅本编译单元可见）：

[shm_posix.cpp:23-40](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/shm_posix.cpp#L23-L40) — 定义了 `info_t`（计数器）、`id_info_t`（fd/指针/尺寸/名字）、`calc_size`（尺寸对齐）和 `acc_of`（定位末尾计数器）。

```cpp
struct info_t {
    std::atomic<std::int32_t> acc_;
};

struct id_info_t {
    int         fd_   = -1;
    void*       mem_  = nullptr;
    std::size_t size_ = 0;
    std::string name_;
};
```

`id_info_t` 用 `mem::$new<id_info_t>()` 在堆上构造（见 [shm_posix.cpp:90](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/shm_posix.cpp#L90)），用 `mem::$delete(ii)` 释放（见 [shm_posix.cpp:191](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/shm_posix.cpp#L191)）。这两个是 libipc 自研的带类型擦除析构的分配接口（u7-l3 会详讲），这里只需当成 `new`/`delete` 看待。

#### 4.1.4 代码实践

**实践目标**：确认「fd 在 mmap 后被关闭」这一关键事实，理解为何后续不需要再持有 fd。

**操作步骤**：

1. 打开 [shm_posix.cpp 的 get_mem](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/shm_posix.cpp#L122-L168)，定位第 157 行的 `mmap` 与第 162 行的 `::close(fd)`、第 163 行的 `ii->fd_ = -1`。
2. 在终端运行 `man 2 mmap`，阅读 NOTES 段关于「close 不会解除映射」的说明。

**需要观察的现象**：`close(fd)` 紧跟在 `mmap` 之后、`ii->mem_ = mem` 之前，三者顺序固定。

**预期结果**：你应当能用自己的话解释——映射建立后，进程地址空间里那块内存由内核的 VMA（虚拟内存区域）维护，与 fd 解绑，所以 fd 可以立即关闭。

> 本步骤为源码阅读型实践，运行 `man` 命令的输出以本地为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `id_info_t` 要单独保存一份 `name_`，而不是每次需要时重新拼？

**参考答案**：因为 `release`/`remove` 在最后要调 `shm_unlink(name)` 删磁盘文件，此时进程可能已经不记得原始的名字参数；把名字和句柄绑在一起，保证任何时候都能找到对应的 `/dev/shm` 文件来清理。

**练习 2**：`id_info_t::mem_` 为 `nullptr` 代表哪几种状态？

**参考答案**：两种——(a) `acquire` 之后还没 `get_mem`（只 open 了 fd，没映射）；(b) 已经被 `release` 释放过（不过此时 `id_info_t` 本身也已被 `$delete`，理论上不该再访问）。后端函数里大量 `if (ii->mem_ == nullptr)` 的判空正是为了拦截 (a) 这种误用。

---

### 4.2 shm_open：创建与打开共享内存对象

#### 4.2.1 概念说明

`shm_open` 是 POSIX 共享内存的入口。它和 `open` 长得很像，区别是它操作的不是磁盘上的普通文件，而是 `/dev/shm`（tmpfs）下的一个「内存文件」。它的行为由一组标志位控制：

- `O_CREAT`：不存在则创建。
- `O_EXCL`：必须配合 `O_CREAT` 使用，表示「只允许我创建；如果已存在就失败」。二者组合可以实现「原子地确认我是不是创建者」。
- `O_RDWR`：读写权限。

libipc 的公共 API 用 `create`/`open` 两个模式位（[shm.h:13-16](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/shm.h#L13-L16)）表达意图，后端负责把它们翻译成上述标志位。

#### 4.2.2 核心流程

`acquire` 的核心是**根据 mode 拼标志位**，再调用 `shm_open`。三种模式的翻译关系如下：

```
mode == open           → O_RDWR                （只打开，不存在就失败，size 置 0）
mode == create         → O_RDWR | O_CREAT | O_EXCL  （独占创建，已存在则失败）
mode == create | open  → O_RDWR | O_CREAT      （默认：有则打开，无则创建）
```

注意 `create | open`（默认值 `0x03`）走的是 `switch` 的 `default` 分支，即「宽松」语义：不存在就建、存在就开。而单独的 `create`（`0x01`）反而是「严格独占」。这个对应关系容易搞反，务必记住。

#### 4.2.3 源码精读

标志位拼装与 `shm_open` 调用在 [shm_posix.cpp:47-95](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/shm_posix.cpp#L47-L95)：

```cpp
// 保证名字以 '/' 开头（POSIX 推荐 /somename 形式）
std::string op_name;
if (name[0] == '/') op_name = name;
else               op_name = std::string{"/"} + name;

int flag = O_RDWR;
switch (mode) {
case open:        size = 0;                break;  // 只打开
case create:      flag |= O_CREAT | O_EXCL; break;  // 独占创建
default:          flag |= O_CREAT;          break;  // 创建或打开
}
int fd = ::shm_open(op_name.c_str(), flag, /*mode 0666*/ ...);
if (fd == -1) { /* 失败：open 模式下文件不存在不算错误，不记日志 */ return nullptr; }
::fchmod(fd, /*0666*/ ...);
auto ii = mem::$new<id_info_t>();
ii->fd_ = fd; ii->size_ = size; ii->name_ = std::move(op_name);
return ii;
```

几个要点：

- **强制前导斜杠**：代码注释引用了 man 手册「portable use 应使用 `/somename` 形式」。libipc 内部名字由 `make_prefix` 生成（形如 `__IPC_SHM__CC_CONN__ipc`，见 [resource.h:34-37](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/resource.h#L34-L37)），不带 `/`，所以这里统一补一个。最终落到 `/dev/shm/__IPC_SHM__CC_CONN__ipc`。
- **权限位 `0666`**：`S_IRUSR|S_IWUSR|S_IRGRP|S_IWGRP|S_IROTH|S_IWOTH`，让同机其他用户也能访问，并通过 `fchmod` 再设一次（防止 umask 影响）。
- **`size` 的去向**：`open` 模式把 `size` 清零（稍后 `get_mem` 会用 `fstat` 探测真实尺寸）；`create`/默认模式保留 `size`（稍后 `get_mem` 用它做 `ftruncate`）。
- **失败处理**：`open` 模式且 `errno == ENOENT`（文件不存在）时不记 error 日志——因为「试着打开、不存在」是正常的探测行为，不算错误（见 [shm_posix.cpp:82](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/shm_posix.cpp#L82)）。

#### 4.2.4 代码实践

**实践目标**：验证「`create | open` 走 default 分支、单独 `create` 走独占分支」这一对应关系，并理解 `O_EXCL` 的并发意义。

**操作步骤**：

1. 在 [shm_posix.cpp:62-76](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/shm_posix.cpp#L62-L76) 旁标注：`create|open(0x03)` 不命中 `case open(0x02)` 也不命中 `case create(0x01)`，落入 `default`。
2. （可选，待本地验证）在 Linux 上写两行 C：`shm_open("/x", O_CREAT|O_EXCL, 0666)` 调两次，第二次应当返回 `-1` 且 `errno == EEXIST`。

**预期结果**：你会确认——当两个进程同时 `acquire` 同一个新名字时，若都用默认 `create|open`，两者都能成功（一个建一个开）；若都用纯 `create`（`O_EXCL`），则恰好一个成功一个 `EEXIST` 失败，这就是「原子地选出唯一创建者」的机制。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `acquire` 在 `shm_open` 失败时，要特判 `open != mode || ENOENT != errno` 才记 error 日志？

**参考答案**：因为「用 `open` 模式去打开一个还不存在的对象」是 libipc 的正常探测路径（比如先试 open、失败再 create），`ENOENT` 是预期内的结果，记成 error 会污染日志；其他失败（权限不足、名字非法等）才是真错误，需要记录。

**练习 2**：`create | open`（默认）和单独的 `open`，在「对象已存在」时行为一样吗？

**参考答案**：是的，对象已存在时两者都只是打开（`O_RDWR`），都成功；区别只在「对象不存在」时——默认模式会创建它，而 `open` 模式返回失败。所以默认模式是「幂等的拿取」，`open` 是「严格依赖它已存在」。

---

### 4.3 mmap 映射、ftruncate 设大小与 calc_size 对齐

#### 4.3.1 概念说明

`shm_open` 只是建了一个**长度为 0** 的内存文件（新建时）。要让它变成「N 字节的共享内存」，必须：

1. 用 `ftruncate(fd, size)` 把文件**撑到** `size` 字节。
2. 用 `mmap(NULL, size, PROT_READ|PROT_WRITE, MAP_SHARED, fd, 0)` 把这 `size` 字节映射进来。

这里有一个分工：**只有创建者才 `ftruncate`**（设尺寸），**打开者用 `fstat` 探测**尺寸。否则两个进程同时 `ftruncate` 同一个文件会互相覆盖、产生竞态。libipc 的 `get_mem` 用 `size_ == 0`（由 `acquire` 的 `open` 模式设定）来区分这两种角色。

另一个精妙的设计是 `calc_size`：用户请求的 `size` 不会原样使用，而是**向上对齐到 4 字节边界，再追加 4 字节**留给末尾的引用计数器。

#### 4.3.2 核心流程

`get_mem` 的判定树（见 [shm_posix.cpp:122-168](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/shm_posix.cpp#L122-L168)）：

```
get_mem(id, &size):
  若已映射(mem_ != null): 直接返回缓存指针            # 重复 get_mem 幂等
  若 fd_ == -1:           失败
  若 size_ == 0 (open 模式):
      fstat(fd) → 读真实文件大小 → 校验(size > 4 且 4 对齐)
  否则 (create 模式):
      size_ = calc_size(size_)   # 对齐 + 预留计数器
      ftruncate(fd, size_)       # 创建者撑大小
  mem = mmap(NULL, size_, RW, MAP_SHARED, fd, 0)
  close(fd); fd_ = -1            # fd 用完即弃
  mem_ = mem
  acc_of(mem, size_).fetch_add(1, release)   # ★ 计数 +1，并发布本进程的写入
  return mem
```

`calc_size` 的数学含义：

\[
\text{calc\_size}(s) = \left\lceil \frac{s}{a} \right\rceil \cdot a \;+\; \text{sizeof(info\_t)}, \quad a = \text{alignof(info\_t)}
\]

其中 \(a\) 通常是 4（`std::atomic<int32_t>` 的对齐）。公式 `(((size-1)/a)+1)*a` 是经典的「向上取整到 a 的倍数」写法，最后再加 `sizeof(info_t)`（也是 4）作为尾部计数器空间。

#### 4.3.3 源码精读

对齐计算与计数器定位：

[shm_posix.cpp:34-40](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/shm_posix.cpp#L34-L40) — `calc_size` 与 `acc_of`：

```cpp
constexpr std::size_t calc_size(std::size_t size) {
    return ((((size - 1) / alignof(info_t)) + 1) * alignof(info_t)) + sizeof(info_t);
}

inline auto& acc_of(void* mem, std::size_t size) {
    return reinterpret_cast<info_t*>(
        static_cast<ipc::byte_t*>(mem) + size - sizeof(info_t))->acc_;
}
```

`acc_of(mem, size)` 的含义：计数器位于映射区**末尾** `sizeof(info_t)` 字节处，即地址 `mem + size - 4`。由于 `size` 本身就是 `calc_size` 算出来的「已含计数器」的总长，这个偏移正好落在追加的那 4 字节上，不会侵入用户数据区。

映射与尺寸设定：

[shm_posix.cpp:138-167](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/shm_posix.cpp#L138-L167) — `open` 模式用 `fstat`、`create` 模式用 `calc_size`+`ftruncate`，随后统一 `mmap`+`close`+`fetch_add`：

```cpp
if (ii->size_ == 0) {                          // open 模式：探测真实尺寸
    struct stat st;
    if (::fstat(fd, &st) != 0) { ...return nullptr; }
    ii->size_ = static_cast<std::size_t>(st.st_size);
    if ((ii->size_ <= sizeof(info_t)) || (ii->size_ % sizeof(info_t))) {
        log.error(...); return nullptr;        // 校验：必须 >4 且 4 对齐
    }
} else {                                       // create 模式：设定尺寸
    ii->size_ = calc_size(ii->size_);
    if (::ftruncate(fd, static_cast<off_t>(ii->size_)) != 0) { ...return nullptr; }
}
void* mem = ::mmap(nullptr, ii->size_, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
if (mem == MAP_FAILED) { ...return nullptr; }
::close(fd);
ii->fd_ = -1; ii->mem_ = mem;
if (size != nullptr) *size = ii->size_;
acc_of(mem, ii->size_).fetch_add(1, std::memory_order_release);   // 计数 +1
return mem;
```

**为什么引用计数放在末尾**（本讲学习目标之一）：

1. **返回指针零偏移**：用户（如 `queue_conn::open`，见 [queue.h:38-41](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h#L38-L41)）拿到的指针就是 `mmap` 的基址 `mem`，可以直接 `static_cast<Elems*>(mem)` 做 placement-new，无需任何偏移计算。若计数器放在头部，则返回值要偏移 4 字节，所有调用方的指针运算都要跟着改。
2. **天然不侵入用户数据**：用户数据区 `[0, 请求size)` 与计数器区 `[size-4, size)` 由 `calc_size` 的「先对齐再追加」保证永不重叠。
3. **与对齐协同**：用户区被对齐到 4 字节，计数器紧随其后，整块 `size_` 也是 4 的整数倍，`open` 模式下的校验 `ii->size_ % sizeof(info_t) == 0` 才成立。

`get_mem` 末尾的 `fetch_add(1, std::memory_order_release)` 是 u5-l1 所述「get_mem 才增计数」的落地点；用 `release` 是为了让本进程对共享内存的初始化写入（如 `elem_array::init`）在计数自增**之前**对其他进程可见。

#### 4.3.4 代码实践

**实践目标**：手算 `calc_size`，验证「用户数据区与计数器不重叠」，并确认 `acc_of` 偏移正确。

**操作步骤**：

1. 假设 `queue_conn::open` 为一个 `sizeof(Elems) == 200` 的无锁队列申请共享内存。
2. 代入 `calc_size(200)`：\(a=4\)，\(\lceil 200/4 \rceil \cdot 4 + 4 = 200 + 4 = 204\)。
3. 计算 `acc_of` 偏移：\(204 - 4 = 200\)，即计数器在 `[200, 204)` 这 4 字节。
4. 确认用户区 `[0, 200)` 与计数器区 `[200, 204)` 无重叠。

**需要观察的现象**：用户区正好等于 `sizeof(Elems)`，计数器紧贴其后。

**预期结果**：无论 `sizeof(Elems)` 是多少，只要 `calc_size` 把它向上对齐到 4 的倍数再加 4，计数器就永远在用户区之外。这也是 `open` 模式 `fstat` 校验 `size_ % sizeof(info_t) == 0` 的依据。

> 本步骤为纸笔演算型实践，结果可立即自验。

#### 4.3.5 小练习与答案

**练习 1**：若用户请求 `size = 1`，`calc_size(1)` 等于多少？用户区和计数器区分别在哪？

**参考答案**：\(\lceil 1/4 \rceil \cdot 4 + 4 = 4 + 4 = 8\)。用户区被对齐到 `[0, 4)`（实际只用第 0 字节，后 3 字节是填充），计数器在 `[4, 8)`。即便请求只有 1 字节，实际占用 8 字节。

**练习 2**：为什么 `open` 模式下要校验 `ii->size_ % sizeof(info_t) == 0`？

**参考答案**：因为创建者用 `calc_size` 写入的文件大小一定是 4 的整数倍（对齐 + 追加都是 4）。打开者通过 `fstat` 读到的尺寸若不是 4 的倍数，说明这块共享内存不是 libipc 创建的、或被外部破坏过，属于不合法状态，必须拒绝映射，否则 `acc_of` 定位计数器会越界。

---

### 4.4 嵌入式引用计数与 release / shm_unlink

#### 4.4.1 概念说明

多个进程映射同一块共享内存时，谁该在最后负责 `shm_unlink` 删掉 `/dev/shm` 文件？libipc 的答案是「嵌入式引用计数 + fetch_sub 返回值判定」：

- 每个 `get_mem`（映射）让末尾计数器 `+1`。
- 每个 `release`（释放）让计数器 `-1`，并读取**减之前**的旧值。
- 若旧值 `<= 1`，说明减完就是 `0`，**我是最后一个**，于是 `munmap` + `shm_unlink`（删文件）；否则只 `munmap`（删自己的映射，文件留给别人）。

这套机制有三个相关函数，职责要分清（u5-l1 已建立概念，本讲落到代码）：

- `get_ref`：只读计数（`load`），不改动。
- `sub_ref`：只减计数（`fetch_sub`），**不 munmap、不删文件**——用于「我想退出引用，但映射交给别人管」的场景（对应 `handle::sub_ref` / `detach`）。
- `release`：减计数 **且** `munmap`，必要时 `shm_unlink` **且** `$delete(id_info_t)`——是完整的「礼貌退场」。

#### 4.4.2 核心流程

`release` 的判定逻辑（见 [shm_posix.cpp:170-193](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/shm_posix.cpp#L170-L193)）：

```
release(id):
  ret = acc_of.fetch_sub(1, acq_rel)        # 返回减之前的旧值
  若 ret <= 1:                               # 减完为 0，我是最后一个
      munmap(mem_, size_)
      若 name_ 非空: shm_unlink(name_)       # 删 /dev/shm 文件
  否则:                                       # 还有别人在用
      munmap(mem_, size_)                    # 只删我的映射
  $delete(id_info_t)
  return ret
```

`fetch_sub` 返回旧值是关键：旧值 `1` 表示「这次减完变 0」，即自己是最后一人。对比若返回新值，则要判 `== 0`；这里用旧值 `<= 1` 是等价的、且更直观（「在我之前还剩几个引用」）。

#### 4.4.3 源码精读

只读与只减的两个轻量函数：

[shm_posix.cpp:97-120](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/shm_posix.cpp#L97-L120) — `get_ref` 用 `acquire` 读、`sub_ref` 用 `acq_rel` 减：

```cpp
std::int32_t get_ref(id_t id) {
    ...
    return acc_of(ii->mem_, ii->size_).load(std::memory_order_acquire);
}

void sub_ref(id_t id) {
    ...
    acc_of(ii->mem_, ii->size_).fetch_sub(1, std::memory_order_acq_rel);
}
```

完整的 `release`：

[shm_posix.cpp:170-193](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/shm_posix.cpp#L170-L193) — 减计数、按返回值决定是否删文件：

```cpp
std::int32_t release(id_t id) noexcept {
    ...
    std::int32_t ret = -1;
    auto ii = static_cast<id_info_t*>(id);
    if (ii->mem_ == nullptr || ii->size_ == 0) {
        log.error(...);                                  # 无效 id，只记日志
    }
    else if ((ret = acc_of(...).fetch_sub(1, std::memory_order_acq_rel)) <= 1) {
        ::munmap(ii->mem_, ii->size_);                   # 最后一人：解除映射
        if (!ii->name_.empty()) {
            int unlink_ret = ::shm_unlink(ii->name_.c_str());  # 并删磁盘文件
            ...
        }
    }
    else ::munmap(ii->mem_, ii->size_);                  # 非最后一人：仅解除自己的映射
    mem::$delete(ii);                                    # 释放进程本地句柄
    return ret;
}
```

两个 `remove` 重载提供「强制清理」：

[shm_posix.cpp:195-229](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/shm_posix.cpp#L195-L229) — `remove(id)` 先 `release` 再无条件 `shm_unlink`；`remove(name)` 直接按名字 `shm_unlink`：

```cpp
void remove(id_t id) noexcept {
    ...
    auto name = std::move(ii->name_);
    release(id);                            # 先礼貌释放
    if (!name.empty()) ::shm_unlink(name.c_str());  # 再强制删文件
}

void remove(char const * name) noexcept {
    ...
    ::shm_unlink(op_name.c_str());          # 仅按名字删文件，不动任何运行时映射
}
```

**关键设计点：**

- **`release` 与 `remove(id)` 的区别**：`release` 只在「计数归零」时才 `shm_unlink`（礼貌，照顾仍在用的进程）；`remove(id)` 无论计数多少都强制删文件（暴力，用于确定要彻底清理）。
- **`remove(name)` 是纯文件操作**：它不接触任何 `id_info_t` 或映射，只删 `/dev/shm` 里的文件名。这就是 `handle::clear_storage`（[shm.cpp:102-107](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/shm.cpp#L102-L107)）的底层，用来扫除「进程崩溃后残留的孤儿文件」——因为崩溃时根本来不及走 `release`。
- **`acq_rel` 内存序**：`fetch_sub` 用 `acq_rel`——`release` 保证本进程的共享内存写入在计数减少前已发布（最后一个进程 `shm_unlink` 时能看到全部数据）；`acquire` 保证读到其他进程最新的计数值。
- **崩溃的局限**：嵌入式计数无法应对进程 `kill -9`——计数不会减，文件不会删。这就是 u5-l1 强调的「需要 `clear_storage` 兜底」的根因，本讲从 POSIX 角度再次印证。

#### 4.4.4 代码实践

**实践目标**：用 `fetch_sub` 的返回值推演两个进程依次 `release` 时，谁负责删文件。

**操作步骤**：

1. 假设进程 A 和进程 B 都 `get_mem` 了同一块共享内存，计数 `acc_ = 2`。
2. 进程 A 先 `release`：`fetch_sub(1)` 返回旧值 `2`，`2 > 1`，所以 A 只 `munmap`，**不删文件**。计数变为 `1`。
3. 进程 B 后 `release`：`fetch_sub(1)` 返回旧值 `1`，`1 <= 1`，所以 B `munmap` **且 `shm_unlink`** 删文件。计数变为 `0`。

**需要观察的现象**：删除文件的 `shm_unlink` 只发生在「最后一个 release」上。

**预期结果**：文件 `/dev/shm/__IPC_SHM__...` 在 B 退出后才消失。若 A、B 都正常退出但顺序相反，结论对称——永远是最后退出的那个进程删文件。

> 本步骤为推演型实践，可在 4.5 综合实践中通过 `ls /dev/shm` 实地观察。

#### 4.4.5 小练习与答案

**练习 1**：`release` 里 `fetch_sub` 返回 `-1`（错误路径）以外的值时，返回给调用方的是什么？

**参考答案**：返回的是 `fetch_sub` 的旧值，即「本次减计数之前」的引用数。调用方（如 `handle::release`）可据此判断自己是否是最后使用者。注意 `shm.cpp` 的 `handle::release` 把这个值直接透传给用户。

**练习 2**：如果进程 A 持有映射时被 `kill -9`，`acc_` 会怎样？`/dev/shm` 文件会怎样？B 正常 `release` 时会发生什么？

**参考答案**：`kill -9` 不会执行任何用户态代码，`acc_` 不会减（仍为创建时的值，比如从 2 停在 2，A 那一份永远没减）。`/dev/shm` 文件因为没人 `shm_unlink` 而**残留**。B `release` 时 `fetch_sub` 返回旧值 `2`（`2 > 1`），只 `munmap` 自己，**不删文件**——文件成为孤儿，必须靠 `handle::clear_storage` → `remove(name)` 显式清理。这就是 libipc 把 `clear_storage` 设计成「按名字扫地」的原因。

---

## 5. 综合实践

**任务**：在 Linux 上实地观察一块 libipc 共享内存从「创建 → 映射 → 残留 → 清理」的完整生命周期，把第 4 节的四个模块串起来。

**操作步骤**：

1. **构建带 demo 的库**（参考 u1-l2）：
   ```bash
   cmake -S . -B build -DLIBIPC_BUILD_DEMOS=ON
   cmake --build build -j
   ```
2. **启动接收端**（它会阻塞在 `recv`，持续持有共享内存映射）：
   ```bash
   ./build/bin/recv ipc    # "ipc" 是通道名
   ```
3. **观察 `/dev/shm`**（新开终端）：
   ```bash
   ls -l /dev/shm/
   ```
   预期看到一组以 `__IPC_SHM__` 开头的文件（如 `CC_CONN__...`、`WT_CONN__...`、`RD_CONN__...`、`AC_CONN__...` 以及队列本体）。记录它们的**大小**，对照 4.3 节验证是否都是 4 的倍数（`calc_size` 对齐 + 尾部计数器的证据）。
4. **对照命名规则**：把某个文件名拆开，确认它是 `make_prefix` 用 `__IPC_SHM__` 分隔符拼出的 `前缀__IPC_SHM__组件__IPC_SHM__通道名`，再被 `acquire` 补上前导 `/`。
5. **模拟崩溃**：`kill -9 <recv 的 PID>`，然后再次 `ls -l /dev/shm/`。预期文件**仍然残留**（4.4 节练习 2 的实证——`kill -9` 不触发 `release`/`shm_unlink`）。
6. **清理孤儿**：在另一个小程序里调用 `ipc::shm::handle::clear_storage("ipc")`（或对应名字），再次 `ls -l /dev/shm/` 验证文件被删。

**需要观察的现象**：

- 步骤 3：进程运行时，`/dev/shm` 里出现多个 `__IPC_SHM__*` 文件，大小均为 4 的倍数。
- 步骤 5：`kill -9` 后文件不消失。
- 步骤 6：`clear_storage` 后文件消失。

**预期结果**：你能用本讲学到的 `shm_open`（创建文件）、`ftruncate`/`mmap`（撑大小并映射）、`calc_size`（4 对齐 + 尾部计数器，故文件大小是 4 的倍数）、`release`/`shm_unlink`（最后一人删文件、崩溃则残留）这一整条链，完整解释 `/dev/shm` 里这些文件的产生与消失。

> 说明：本实践依赖一个可用的 Linux 环境与图形/多终端能力。若在本机构建或运行结果与预期不符，以本地实际现象为准（待本地验证）。即便无法运行，步骤 4 的命名拆解与 4.3/4.4 节的推演也可独立完成。

## 6. 本讲小结

- POSIX 后端 `shm_posix.cpp` 用 `shm_open` 建内存文件、`ftruncate` 撑大小、`mmap(MAP_SHARED)` 映射、`shm_unlink` 删文件，这四件套构成了 libipc 在 Linux/FreeBSD/QNX 上的共享内存地基。
- `acquire` 把公共的 `create`/`open` 模式翻译成 `O_CREAT`/`O_EXCL` 标志：默认 `create|open` 是「有则开、无则建」，单独 `create` 才是「独占创建」，单独 `open` 是「必须已存在」。
- **只有创建者 `ftruncate` 设尺寸，打开者用 `fstat` 探测**，靠 `size_ == 0` 区分两种角色，避免竞态。
- `calc_size` 把请求大小向上对齐到 4 字节再追加 4 字节，使引用计数器 `info_t::acc_` 放在映射区**末尾**，既保证返回指针零偏移，又让用户数据区与计数器永不重叠。
- `release` 用 `fetch_sub` 的**旧值**判定「最后一人」：旧值 `<= 1` 时才 `munmap` + `shm_unlink` 删文件，否则只删自己的映射；`remove(id)` 无条件删文件，`remove(name)` 是纯文件扫地，专治进程崩溃留下的孤儿。
- 嵌入式计数器挡不住 `kill -9`，崩溃后的 `/dev/shm` 残留必须靠 `handle::clear_storage` 兜底——这是从 POSIX 角度印证 u5-l1 的设计取舍。

## 7. 下一步学习建议

- **下一讲 u5-l4（Windows 共享内存后端）**：对比 `shm_win.cpp` 的 `CreateFileMapping`/`MapViewOfFile`，重点看为什么 Windows 的 `remove(name)` 是空操作、以及 `Global\` 前缀如何跨会话命名。两套后端签名一致、内部各异，正是 u5-l2「统一接口、各自实现」的体现。
- **横向回顾 u5-l1**：带着本讲对 `fetch_add`/`fetch_sub` 的具体理解，重读 `handle` 的 RAII 与引用计数语义，确认「`acquire` 不增计数、`get_mem` 才增、`release` 才减」在每个后端都成立。
- **向下游延伸**：本讲的 `shm::handle` 是 [queue.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/queue.h) 中无锁队列的载体。学完 u5-l4 后，可进入 U4（无锁循环队列），看 `queue_conn::open` 如何把 `elem_array` 整个 `placement-new` 到本讲映射出来的这块内存上。
- **深入同步层**：本讲只覆盖「数据通路」用的共享内存；libipc 的 `mutex`/`condition`/`waiter` 同样躺在共享内存上（U6），它们的跨进程健壮锁（robust mutex）与本讲的引用计数清理是两套互补的崩溃恢复机制，值得对照阅读。
