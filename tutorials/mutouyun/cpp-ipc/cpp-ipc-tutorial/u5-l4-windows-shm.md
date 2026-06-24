# Windows 共享内存后端与 Global 前缀

## 1. 本讲目标

本讲承接 u5-l3「POSIX 共享内存后端」，把目光转向 Windows 平台。读完后你应当能够：

- 理解 Windows 的「内核对象 + 句柄（HANDLE）」模型，以及它为何与 POSIX 的「fd + 磁盘文件」模型根本不同。
- 读懂 `shm_win.cpp`：`acquire` 如何用 `CreateFileMapping` / `OpenFileMapping` 创建或打开命名映射，`get_mem` 如何用 `MapViewOfFile` 映射、用 `VirtualQuery` 探测真实大小。
- 解释 `remove(char const * name)` 为何是一个「Do Nothing.」空操作，而 POSIX 版本必须调用 `shm_unlink`。
- 认识 `Global\` 命名前缀的作用：让 Session 0 里的 Windows 服务与用户会话里的普通进程能够共享同一块内存。

本讲只讲 **Windows 后端的实现细节**，不再重复 u5-l1 的 `shm::handle` 公共 API 与跨进程引用计数原理（两套后端在这部分完全一致）。

## 2. 前置知识

在进入源码前，先建立两个 Windows 特有的基础概念。它们是理解 `shm_win.cpp` 与 POSIX 后端差异的钥匙。

### 2.1 内核对象与 HANDLE

Windows 的共享内存、互斥量、信号量、事件等，都是「内核对象（kernel object）」。进程拿到的是一个不透明句柄 `HANDLE`，而不是像 POSIX 那样的整数文件描述符 `fd`。内核对象由操作系统统一管理生命周期：

- 对象可以被「命名」，名字是进程间寻找彼此的钥匙。
- 每个打开它的进程各持有一个 `HANDLE`，对象内部维护一个**引用计数**：只有当所有 `HANDLE` 都被 `CloseHandle` 关闭、且所有映射视图都被 `UnmapViewOfFile` 解除后，对象才会被系统回收。

这一点和 POSIX 截然不同：POSIX 的 `shm_open` 会在 `/dev/shm` 下创建一个**真实的磁盘文件**，这个文件即便所有进程都退出了依然存在（直到 `shm_unlink` 或机器重启）。这个差异正是本讲实践任务「为何 Windows 的 `remove(name)` 是空操作」的答案根源。

### 2.2 页面文件支撑的文件映射（page-file backed）

Windows 创建共享内存用的是 `CreateFileMapping`，它的第一个参数是文件句柄 `hFile`。当传入特殊的 `INVALID_HANDLE_VALUE` 时，这块内存**不对应任何磁盘文件**，而是由系统页面文件（page file）支撑——也就是说它本质上是「纯内存」，没有文件名、没有磁盘残留。

### 2.3 会话命名空间（Session namespace）

Windows 是多会话系统：系统服务运行在 **Session 0**（隔离、无界面），而每个登录用户各占一个 Session 1、2……。内核对象的名字默认只在「当前会话」可见（相当于 `Local\` 前缀）。要让 Session 0 的服务与 Session N 的用户进程共享同名对象，必须给名字加上 `Global\` 前缀，把它放进**全局（会话无关）命名空间**。这正是 `win_service` demo 里 `prefix{"Global\\"}` 的来历。

> 小贴士：如果你写过 Linux，可以粗略地把 `Global\` 理解成「让对象名跨 session 0 与用户会话可见」，相当于强制把对象放进一个所有进程都能找到的公共目录。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/libipc/platform/win/shm_win.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/shm_win.cpp) | **本讲主角**：Windows 共享内存后端的全部实现 |
| [src/libipc/platform/win/to_tchar.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/to_tchar.h) | 名字字符集转换：`std::string` → `TCHAR`（Unicode 下转 `std::wstring`） |
| [src/libipc/platform/win/get_sa.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/get_sa.h) | 构造安全属性（NULL DACL），让任意进程都能访问命名映射 |
| [src/libipc/platform/posix/shm_posix.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/shm_posix.cpp) | 对照组：POSIX 后端，用于解释 `remove` 的差异 |
| [include/libipc/shm.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/shm.h) | 公共接口：`id_t`、`create`/`open` 模式常量、`acquire` 声明 |
| [demo/win_service/service/main.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/win_service/service/main.cpp) | Windows 服务 demo：演示 `Global\` 前缀的真实用法 |

`shm_win.cpp` 与 `shm_posix.cpp` 由 u5-l2 讲到的 `platform.cpp` 在编译期二选一，两者对外签名完全一致，内部各异——这就是 libipc「统一接口、各自实现、编译期分流」的体现。

## 4. 核心概念与源码讲解

### 4.1 HANDLE 与 id_info_t：Windows 句柄模型

#### 4.1.1 概念说明

u5-l3 讲 POSIX 后端时，`id_info_t` 存了 `fd_`、`mem_`、`size_` 还有 `name_`。Windows 后端也有一个同名的 `id_info_t`，但字段不同——最关键的是用 `HANDLE h_` 替代了 `fd_`，而且**不存 `name_`**。

为什么 Windows 的 `id_info_t` 不需要 `name_`？因为 POSIX 的 `remove(name)` 要靠名字去删 `/dev/shm` 里的磁盘文件，必须把名字记下来；而 Windows 的 `remove(name)` 是空操作（见 4.4），根本用不到名字，存了也是浪费。

至于嵌入式引用计数 `info_t::acc_`，两套后端**完全一致**：一个塞在共享内存最后 4 字节的 `atomic<int32_t>`，靠 `MAP_SHARED` / 文件映射让所有进程共享同一个计数。

#### 4.1.2 核心流程

Windows 句柄模型的生命周期可以概括为：

1. `acquire`：`CreateFileMapping` / `OpenFileMapping` 拿到 `HANDLE`，存进 `id_info_t::h_`。
2. `get_mem`：用 `HANDLE` 调 `MapViewOfFile` 得到本进程的映射指针 `mem_`，并对末尾计数器 `fetch_add`。
3. `release`：`fetch_sub` 计数器、`UnmapViewOfFile` 解除映射、`CloseHandle` 关闭句柄。

> 与 POSIX 的关键差异：POSIX 在 `get_mem` 里 `mmap` 之后立刻 `close(fd)`（fd 不再需要）；Windows 却把 `HANDLE` 一直保留到 `release` 才 `CloseHandle`。原因见 4.4——只要还有一个 `HANDLE` 打开，命名对象就仍然注册在内核命名空间里，别的进程才能 `OpenFileMapping` 找到它。

#### 4.1.3 源码精读

计数器结构与 POSIX 后端逐字一致：

[src/libipc/platform/win/shm_win.cpp:24-32](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/shm_win.cpp#L24-L32) 定义了 `info_t`（4 字节原子计数器 `acc_`）和 `id_info_t`（`HANDLE h_` + 映射指针 `mem_` + 尺寸 `size_`）。注意这里**没有** `name_` 字段，对照 POSIX 版本的 `id_info_t` 即可体会差异。

尺寸对齐与计数器定位也和 POSIX 一模一样——把用户请求大小向上对齐到 4 字节、再追加 4 字节计数器：

```cpp
constexpr std::size_t calc_size(std::size_t size) {
    return ((((size - 1) / alignof(info_t)) + 1) * alignof(info_t)) + sizeof(info_t);
}
```

[src/libipc/platform/win/shm_win.cpp:34-40](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/shm_win.cpp#L34-L40) 是 `calc_size` 与 `acc_of`。设用户请求 \(N\) 字节、对齐 \(a=\) `alignof(info_t)` \(=4\)，则总映射大小为：

\[
\text{calc\_size}(N) = \left\lceil \frac{N}{a} \right\rceil \cdot a + a
\]

用户拿到的是起始指针（零偏移），最后 4 字节是计数器，互不侵犯。

公共接口层面，`id_t` 就是个不透明指针 `void*`，`create`/`open` 是两个位标志：

[include/libipc/shm.h:11-18](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/shm.h#L11-L18) 定义 `using id_t = void*;` 与 `create = 0x01`、`open = 0x02`，以及默认 `mode = create | open` 的 `acquire`。

#### 4.1.4 代码实践

**实践目标**：体会「同一份 `shm.h` 接口、两份 `id_info_t` 实现」的编译期分派。

**操作步骤**：
1. 打开 `shm_win.cpp` 与 `shm_posix.cpp`，并排对比两者的匿名命名空间里的 `info_t` 与 `id_info_t` 定义。
2. 记下字段差异：POSIX 有 `fd_` + `name_`，Windows 有 `HANDLE h_` 且无 `name_`。

**预期结果**：你会发现 `info_t`、`calc_size`、`acc_of` 三者在两个文件里**逐字相同**；差异只出现在 `id_info_t` 与具体系统调用上。这印证了 u5-l2 的「统一接口、各自实现」。

#### 4.1.5 小练习与答案

**练习 1**：Windows 的 `id_info_t` 为什么不存 `name_`？

**参考答案**：因为 Windows 的命名文件映射对象由页面文件支撑、没有磁盘文件，`remove(name)` 是空操作，不需要靠名字去删任何东西；而 POSIX 必须靠 `name_` 去 `shm_unlink` `/dev/shm` 下的磁盘文件。

**练习 2**：`alignof(info_t)` 在两个后端里都是多少？为什么 `calc_size` 要向上对齐再加 4？

**参考答案**：`alignof(info_t) = alignof(std::atomic<std::int32_t>) = 4`。向上对齐保证计数器落在 4 字节对齐地址上（原子操作对齐要求），再加 4 是把计数器放在用户区域之后，使返回给用户的指针零偏移且计数器不侵入用户区。

---

### 4.2 Create/OpenFileMapping：创建与打开文件映射

#### 4.2.1 概念说明

`acquire` 是 Windows 后端的入口，它的任务是把 u5-l1 的 `mode`（`create` / `open` / `create|open`）翻译成 Windows 系统调用，并返回一个 `id_info_t`。三个关键 API：

- `CreateFileMapping(INVALID_HANDLE_VALUE, ...)`：创建（或打开已存在的）命名映射对象。`INVALID_HANDLE_VALUE` 表示由页面文件支撑。它返回的 `HANDLE` 可能是「新建」也可能是「已存在」的——要靠 `GetLastError()` 返回 `ERROR_ALREADY_EXISTS` 来区分。
- `OpenFileMapping(...)`：**只能**打开已存在的命名映射，不存在则失败。
- `GetLastError() == ERROR_ALREADY_EXISTS`：这是 Windows 判断「对象已存在」的标准手段，相当于 POSIX 的 `O_EXCL` 语义。

注意：`CreateFileMapping` 在「对象已存在」时**不会失败**，而是返回一个指向现有对象的句柄。所以「严格只创建（mode == create）」的语义需要代码自己检查 `ERROR_ALREADY_EXISTS` 并主动关闭句柄。

#### 4.2.2 核心流程

`acquire(name, size, mode)` 的分派逻辑：

```
mode == open ?
 ├─ 是 → OpenFileMapping()；失败（句柄为 NULL）返回 nullptr
 └─ 否（create 或 create|open）
     ├─ CreateFileMapping(INVALID_HANDLE_VALUE, get_sa(), PAGE_READWRITE|SEC_COMMIT, 大小=calc_size(size), 名字)
     ├─ err = GetLastError()
     ├─ 若 mode==create 且 err==ERROR_ALREADY_EXISTS → 关闭句柄、置 NULL（严格创建失败）
     └─ 句柄为 NULL → 返回 nullptr，否则 $new<id_info_t>() 存 h_ 与 size_，返回之
```

注意 Windows 在创建时就把 `calc_size(size)` 作为映射大小传给 `CreateFileMapping`——这是和 POSIX 最大的流程差异之一：POSIX 的 `acquire` 只 `shm_open` 拿 fd，真正定大小要等到 `get_mem` 里 `ftruncate`；Windows 则在 `acquire` 阶段就把大小焊死。

#### 4.2.3 源码精读

名字要先经过 `to_tchar` 转成 Windows 期望的 `TCHAR` 字符串（Unicode 编译下是宽字符 `wchar_t`）：

[src/libipc/platform/win/shm_win.cpp:53-79](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/shm_win.cpp#L53-L79) 是 `acquire` 的核心分派。`open` 模式走 `OpenFileMapping`；其余模式走 `CreateFileMapping(INVALID_HANDLE_VALUE, detail::get_sa(), PAGE_READWRITE | SEC_COMMIT, 0, alloc_size, name)`，并在 `mode == create && err == ERROR_ALREADY_EXISTS` 时关闭句柄、令其失败。

`to_tchar` 的转换逻辑（Unicode 下 UTF-8→UTF-16）在：

[src/libipc/platform/win/to_tchar.h:42-75](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/to_tchar.h#L42-L75)。当 `TCHAR == char`（非 Unicode）时原样返回 `std::string`；当 `TCHAR == wchar_t`（`UNICODE` 定义）时用 `MultiByteToWideChar(CP_UTF8, ...)` 转成 `std::wstring`。源码注释里也提到了 `codecvt` 在 C++17 已弃用、故改用 Win32 API。

`get_sa()` 提供的安全属性值得一看——它给映射对象设了一个 **NULL DACL**，即允许任意进程（包括不同账户的服务）访问，这对跨会话共享是必须的：

[src/libipc/platform/win/get_sa.h:8-34](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/get_sa.h#L8-L34)。`sa_initiator` 构造时 `SetSecurityDescriptorDacl(&sd_, TRUE, NULL, FALSE)`——第 3 参数为 `NULL` 即「NULL DACL」，表示不设访问限制、人人可访问；`get_sa()` 返回这个静态单例的 `SECURITY_ATTRIBUTES*`，直接喂给 `CreateFileMapping`。

#### 4.2.4 代码实践

**实践目标**：理解 `ERROR_ALREADY_EXISTS` 如何实现 POSIX 的 `O_EXCL` 语义。

**操作步骤**：
1. 在 `shm_win.cpp` 第 68-74 行，对照阅读 `err = GetLastError()` 与 `if ((mode == create) && (err == ERROR_ALREADY_EXISTS))`。
2. 翻到 `shm_posix.cpp` 第 69-71 行，看 POSIX 如何用 `flag |= O_CREAT | O_EXCL` 实现同样的「严格创建」。

**预期结果**：两套代码用各自的平台机制表达同一个语义——「只要对象已存在，create 模式就失败」。Windows 靠「创建后查错误码」事后判断，POSIX 靠「打开时带 `O_EXCL` 原子保证」事前拒绝。

#### 4.2.5 小练习与答案

**练习 1**：如果 `mode = create | open`（默认值）调用 `acquire`，而对象已存在，会发生什么？

**参考答案**：`CreateFileMapping` 返回指向**现有对象**的句柄（不是新建），`GetLastError()` 返回 `ERROR_ALREADY_EXISTS`。但此时 `mode != create`（它是 `create|open`），所以不会进入关闭分支，函数正常返回该句柄。这正是「不存在则建、存在则用」的默认语义。

**练习 2**：为什么 `CreateFileMapping` 传 `INVALID_HANDLE_VALUE` 而不是一个真实文件句柄？

**参考答案**：传 `INVALID_HANDLE_VALUE` 表示这块映射由**系统页面文件支撑**，是纯内存、无磁盘文件。这正是 Windows 共享内存不需要 `shm_unlink`、`remove(name)` 是空操作的根本原因。

---

### 4.3 MapViewOfFile 与大小探测

#### 4.3.1 概念说明

`get_mem` 负责把 `HANDLE` 映射成本进程可读写的内存指针，并维护引用计数。这里有一个 Windows 特有的难题：**打开者不知道映射有多大**。

- POSIX 用 `fstat(fd)` 直接读文件大小，精确无误。
- Windows 的打开者只有一个 `HANDLE`，且 `MapViewOfFile` 的最后一个大小参数传 `0` 表示「映射整个对象」。要知道真实大小，得靠 `VirtualQuery` 去问系统这块内存区域（`MEMORY_BASIC_INFORMATION.RegionSize`）到底多大。

另外，和 POSIX 一样，`acquire` 阶段**不**增加引用计数，真正 `fetch_add` 发生在 `get_mem` 里。

#### 4.3.2 核心流程

```
get_mem(id):
  若已映射 (mem_ != null) → 直接返回缓存指针
  MapViewOfFile(h_, FILE_MAP_ALL_ACCESS, 0, 0, 0)   // 最后一个 0 = 映射整个对象
  VirtualQuery(mem) → RegionSize
  若 size_ == 0（打开者）→ size_ = RegionSize - sizeof(info_t)   // 推算用户可见大小
  否则（创建者）→ 保持 acquire 时存的 size_
  fetch_add 计数器，返回 mem
```

#### 4.3.3 源码精读

映射与探测的完整逻辑：

[src/libipc/platform/win/shm_win.cpp:111-147](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/shm_win.cpp#L111-L147)。注意三段：已映射则直接复用 `mem_`（避免重复映射）；否则 `MapViewOfFile` 后用 `VirtualQuery` 探测；最后 `fetch_add` 计数器。

关键的「打开者推算大小」逻辑：

[src/libipc/platform/win/shm_win.cpp:126-145](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/shm_win.cpp#L126-L145)。`MapViewOfFile(ii->h_, FILE_MAP_ALL_ACCESS, 0, 0, 0)` 最后参数 `0` 表示映射整个对象；随后 `VirtualQuery` 拿到 `mem_info.RegionSize`；当 `ii->size_ == 0`（即以 `open` 模式进来、创建时大小未知）时，令 `size_ = RegionSize - sizeof(info_t)` 还原用户可见大小；最后对末尾计数器 `fetch_add(1, release)`。

> 细节提示：`RegionSize` 可能被系统向上取整到页边界（通常 4KB），所以「打开者推算出的 size_」可能略大于创建者当初请求的精确值。POSIX 靠 `fstat` 读到的是文件确切字节数，因而是精确的。这是 Windows 后端一个可接受的小近似——用户区只会更大、不会更小。

`release` 的对照实现：

[src/libipc/platform/win/shm_win.cpp:149-170](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/shm_win.cpp#L149-L170)。`fetch_sub` 计数器（返回旧值）、`UnmapViewOfFile` 解除映射、`CloseHandle(ii->h_)` 关闭句柄、`mem::$delete(ii)` 释放 `id_info_t`。注意 Windows 的 `release` **无论计数器是否归零，都只做 unmapping + 关句柄**，没有任何「删文件」步骤——这和 POSIX `release` 在计数归零时 `shm_unlink` 形成鲜明对照（见 4.4 实践）。

#### 4.3.4 代码实践

**实践目标**：对比 Windows 与 POSIX 在「定大小」与「查大小」上的流程差异。

**操作步骤**：
1. 在 `shm_win.cpp` 第 65 行看到：创建时大小在 `acquire` 里通过 `alloc_size = calc_size(size)` 直接交给 `CreateFileMapping`。
2. 在 `shm_posix.cpp` 第 151-156 行看到：POSIX 的 `acquire` 不定大小，真正 `ftruncate` 发生在 `get_mem` 里。
3. 对照 Windows 第 132-140 行的 `VirtualQuery` 探测 与 POSIX 第 139-144 行的 `fstat` 探测。

**预期结果**：你会画出两条不同的链路——Windows「acquire 定大小 / get_mem 用 VirtualQuery 查」，POSIX「acquire 只 open / get_mem 用 ftruncate 定 + fstat 查」。两者都靠「`size_==0` 判断是否为打开者」来切换创建/打开分支。

#### 4.3.5 小练习与答案

**练习 1**：`MapViewOfFile` 最后一个参数传 `0` 是什么意思？为什么不传具体大小？

**参考答案**：传 `0` 表示映射**整个**文件映射对象（从偏移 0 到末尾）。打开者事先不知道对象大小，故用 `0` 让系统映射全部，再通过 `VirtualQuery` 探测实际区域大小。

**练习 2**：Windows 后端中，`acquire` 不增计数、`get_mem` 才 `fetch_add`。如果某进程只 `acquire` 拿到 id 却从不 `get_mem`，计数器状态如何？

**参考答案**：计数器未被增加。该进程若直接 `release` 会执行 `fetch_sub`，导致计数被多减一次（可能误判为最后使用者）。这正是接口约定「`get_mem` 才真正占有、`sub_ref` 只减计数」的原因——使用时必须配对，参见 u5-l1。

---

### 4.4 Global 前缀跨会话与 remove 空操作

#### 4.4.1 概念说明

本模块是本讲的两个「点睛」结论：

1. **`Global\` 前缀让跨会话共享成为可能**。Windows 服务在 Session 0，用户程序在 Session 1+，默认命名空间互相隔离。加 `Global\` 前缀把对象放进全局命名空间，双方才能用同一个名字找到同一块内存。

2. **Windows 的 `remove(name)` 是一个空操作**。因为页面文件支撑的命名映射**没有磁盘文件**，对象的生命周期完全由「还有没有打开的句柄/映射视图」决定——最后一个引用关闭时系统自动回收。没有文件可删，所以 `remove(name)` 只能 `// Do Nothing.`。

这两点共同解释了实践任务的核心：为什么 POSIX 需要 `shm_unlink` 而 Windows 不需要。

#### 4.4.2 核心流程

**名字如何带上 `Global\`**：用户在构造 channel 时传 `ipc::prefix{"Global\\"}`，它经 `make_prefix` 与 `__IPC_SHM__` 分隔符、组件标签拼接，最终 `Global\` 保留在名字最前面，一路传到 `acquire` → `to_tchar` → `CreateFileMapping` / `OpenFileMapping`。

**对象何时消失**：

```
进程 A: CreateFileMapping("Global\\__IPC_SHM__...")  → 内核对象引用计数 = 1
进程 A: MapViewOfFile                                  → 视图引用 +1
进程 B: OpenFileMapping("Global\\__IPC_SHM__...")     → 句柄 +1（计数 = 2）
进程 B: MapViewOfFile                                  → 视图 +1
...
进程 A: UnmapViewOfFile + CloseHandle                  → 计数减
进程 B: UnmapViewOfFile + CloseHandle                  → 计数减到 0 → 系统自动回收对象
```

无需、也无法 `remove`——对象自己会在引用归零时消失。

#### 4.4.3 源码精读

`remove` 的两个重载都在文件末尾：

[src/libipc/platform/win/shm_win.cpp:172-188](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/shm_win.cpp#L172-L188)。`remove(id_t)` 内部只是转调 `release(id)`；而 `remove(char const * name)` 在做完名字校验后，函数体只有一行注释 `// Do Nothing.`——这就是 Windows 后端对「按名字删除」的回答。

对照 POSIX 后端的同名函数，差异一目了然：

[src/libipc/platform/posix/shm_posix.cpp:181-190](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/shm_posix.cpp#L181-L190) 是 POSIX `release` 在计数归零（旧值 ≤ 1）时执行的 `munmap` + `shm_unlink`——**真的去删 `/dev/shm` 文件**。

[src/libipc/platform/posix/shm_posix.cpp:212-229](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/shm_posix.cpp#L212-L229) 是 POSIX `remove(name)`，它实实在在地调用 `::shm_unlink(op_name.c_str())` 删除磁盘文件——与 Windows 的空操作形成完美对照。

`Global\` 前缀的真实使用场景在 Windows 服务 demo 里：

[demo/win_service/service/main.cpp:165-188](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/demo/win_service/service/main.cpp#L165-L188)。服务工作线程里：

```cpp
ipc::channel ipc_r{ipc::prefix{"Global\\"}, "service ipc r", ipc::sender};
ipc::channel ipc_w{ipc::prefix{"Global\\"}, "service ipc w", ipc::receiver};
```

服务（Session 0）与客户端（用户会话）用带 `Global\` 前缀的两条命名通道做请求-响应。若去掉 `Global\`，两边落在不同会话命名空间，`OpenFileMapping` 会找不到对象、通信失败。

前缀拼接机制本身（`Global\` 为何能留在名字最前）在：

[src/libipc/mem/resource.h:33-37](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/mem/resource.h#L33-L37)。`make_prefix(prefix, "__IPC_SHM__", args...)` 把 `prefix`（即 `Global\`）放在最前，于是最终共享内存对象名形如 `Global\__IPC_SHM__QU_CONN__<名字>...`，`Global\` 恰好位于内核命名空间前缀的位置。

#### 4.4.4 代码实践

**实践目标**：解释「为何 Windows 的 `remove(name)` 是空操作、而 POSIX 需要 `shm_unlink`」。

**操作步骤**：
1. 打开 `shm_win.cpp` 第 181-188 行，确认 `remove(name)` 函数体是空的（`// Do Nothing.`）。
2. 打开 `shm_posix.cpp` 第 212-229 行，确认 POSIX `remove(name)` 调用了 `shm_unlink`。
3. 回到 `shm_win.cpp` 第 66-67 行，确认 `CreateFileMapping` 的第一个参数是 `INVALID_HANDLE_VALUE`（页面文件支撑、无磁盘文件）。
4. 回到 `shm_posix.cpp` 第 77 行，确认 POSIX 用 `shm_open` 创建了一个真实的 `/dev/shm` 文件。

**需要观察的现象 / 解释要点**：
- POSIX：`shm_open` 在 `/dev/shm` 下落了一个真实文件。这个文件**不会**因为所有进程退出而自动消失（进程崩溃时更不会），所以必须靠 `shm_unlink` 主动删除，否则会泄漏磁盘空间。这正是 u5-l1 提到的「嵌入式计数防不住崩溃，泄漏需 `clear_storage` 兜底」。
- Windows：`CreateFileMapping(INVALID_HANDLE_VALUE, ...)` 由页面文件支撑，**没有磁盘文件**。命名对象的存活完全取决于「是否还有打开的 `HANDLE` 或映射视图」——最后一个引用关闭时系统自动回收，无物可删。因此 `remove(name)` 只能空操作。

**预期结果**：你能用一句话讲清差异——「POSIX 共享内存是磁盘文件，需 `unlink`；Windows 共享内存是内核对象，引用归零自动消失，无需 `unlink`」。运行验证：若在 Windows 上反复启停服务/客户端而不 `clear_storage`，不会在磁盘留下残留文件（无文件可留）；而在 Linux 上若进程崩溃、又从不调用 `clear_storage`，`/dev/shm` 下会累积残留文件（可用 `ls /dev/shm` 观察，**待本地验证**）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 Windows 服务 demo 必须用 `prefix{"Global\\"}`，而不能直接用普通名字？

**参考答案**：Windows 服务运行在 Session 0，客户端运行在用户会话（Session 1+）。普通名字默认只在当前会话（`Local\`）可见，跨会话无法共享。`Global\` 前缀把命名对象放进全局、会话无关的命名空间，服务端与客户端才能用同一个名字找到同一块共享内存。

**练习 2**：既然 Windows 的 `remove(name)` 是空操作，那 u5-l1 提到的「进程崩溃导致泄漏」问题在 Windows 上还存在吗？

**参考答案**：不存在磁盘文件泄漏（因为压根没文件）。Windows 命名对象在所有句柄与映射视图都关闭/解除后被系统自动回收；进程崩溃时其句柄会被 OS 自动关闭，只要没有别的进程仍持有引用，对象就会消失。但若崩溃时有别的进程仍持有有效引用并长期不释放，对象会一直驻留内存直到这些进程也退出——这和 POSIX 的磁盘泄漏是不同性质的「残留」。

---

## 5. 综合实践

**任务：画出 Windows 共享内存的完整生命周期，并标注与 POSIX 的三处关键差异。**

请结合本讲源码完成以下源码阅读型实践：

1. **追踪一条名字的生命线**：从 demo 里 `ipc::prefix{"Global\\"}` 出发，依次标注它经过 `make_prefix`（拼接 `__IPC_SHM__`）、`acquire`、`to_tchar`（转宽字符）、最终到达 `CreateFileMapping` / `OpenFileMapping` 的全过程。说明 `Global\` 为何始终在名字最前。

2. **画出句柄与计数的时序**：在一张图上标出两个进程 A（创建者）、B（打开者）各自调用 `acquire` → `get_mem` → `release` 的顺序，并标注每一步后「嵌入式计数器 `acc_`」的值变化。重点说明：为何 A 在 `acquire` 后计数仍是初值、`get_mem` 后才 +1；为何 B 的 `get_mem` 能靠 `VirtualQuery` 推算出大小。

3. **定位三处关键差异**：在时序图旁列出 Windows 相对 POSIX 的三处根本不同——
   - 句柄模型：`HANDLE`（保留到 release）vs `fd`（mmap 后即 close）；
   - 大小机制：`acquire` 时 `CreateFileMapping` 定大小 + `VirtualQuery` 查 vs `get_mem` 时 `ftruncate` 定 + `fstat` 查；
   - 删除语义：`remove(name)` 空操作 vs `shm_unlink` 删磁盘文件。

4. **思考题（待本地验证）**：如果你要在 Windows 上写一个「确保上次崩溃残留被清理」的启动逻辑，调用 `shm::handle::clear_storage(name)` 还有意义吗？结合 4.4 的结论给出判断。（提示：`clear_storage` 最终走到 `remove(name)`，而它是空操作——所以 Windows 上这层兜底实际上是 no-op，崩溃残留靠 OS 自动回收句柄来化解。）

## 6. 本讲小结

- Windows 共享内存后端 `shm_win.cpp` 与 POSIX 后端对外签名完全一致，差异全在内部系统调用：用 `HANDLE` 句柄模型替代 `fd` 文件模型。
- `acquire` 用 `CreateFileMapping(INVALID_HANDLE_VALUE, ...)` 创建页面文件支撑的命名映射（`open` 模式走 `OpenFileMapping`）；`ERROR_ALREADY_EXISTS` 实现 POSIX `O_EXCL` 的「严格创建」语义；`get_sa()` 提供的 NULL DACL 让任意进程可访问。
- `get_mem` 用 `MapViewOfFile(…, 0)` 映射整个对象，再用 `VirtualQuery` 的 `RegionSize` 探测真实大小，弥补「打开者不知大小」的难题；引用计数 `fetch_add` 发生在这里而非 `acquire`。
- Windows 后端**不存 `name_`**、**不在 `release` 里删文件**，因为页面文件支撑的映射无磁盘文件——这是 `remove(name)` 成为空操作（`// Do Nothing.`）的根本原因，与 POSIX 必须 `shm_unlink` 删 `/dev/shm` 文件截然不同。
- `Global\` 前缀把命名对象放进会话无关的全局命名空间，使 Session 0 的服务与用户会话的客户端能共享同名对象，这是 `win_service` demo 的核心。

## 7. 下一步学习建议

本讲完成了共享内存层在两大平台的落地。接下来建议：

- **进入同步原语层（U6）**：共享内存只是「同一块内存」，跨进程协作还需要锁与等待。下一讲 u6-l1 将讲解 `rw_lock.h` 里的 `spin_lock`、`rw_lock` 与 `yield`/`sleep` 渐进退避——你会看到本讲引用计数器用到的 `acquire`/`release`/`acq_rel` 内存序在锁里如何发挥作用。
- **回顾健壮性话题（u8-l2 预告）**：本讲提到「Windows 命名对象靠 OS 自动回收句柄化解崩溃残留」，但跨进程**锁**崩溃恢复是另一回事（Windows 的 abandoned mutex、POSIX 的 robust mutex），留待 u8-l2 深入。
- **延伸阅读**：可对照阅读 `src/libipc/platform/win/mutex.h`、`condition.h`、`semaphore.h`，它们同样采用「Win32 API + `Global\` 风格命名 + 嵌入共享内存」的模式，是本讲思路在同步原语上的复刻。
