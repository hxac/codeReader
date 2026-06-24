# shm::handle 公共 API 与跨进程引用计数

## 1. 本讲目标

本讲是「共享内存基础」单元的第一讲。前面几个单元（u1~u4）讲的 `route`/`channel`、消息分片、无锁队列，底层都建立在同一块物理基石之上——**共享内存（shared memory）**。本讲就要把这块基石挖出来单独讲清楚。

具体来说，学完本讲你应该能够：

- 说清楚 `ipc::shm::handle` 这个 RAII 类型对外暴露了哪些接口、各自做什么。
- 区分 `acquire` 的 `create` / `open` / `create | open` 三种模式的行为差异。
- 理解「为什么引用计数要嵌在共享内存的最后 4 个字节里」，以及它如何在没有额外内核对象的情况下实现跨进程协调。
- 准确区分 `release()`、`clear()`、`clear_storage(name)` 三个看似都在「清理」的接口的语义差异，并知道 POSIX 与 Windows 在删除后端上的本质不同。

本讲只讲「公共 API + 引用计数机制」，**不展开** POSIX/Windows 后端的具体系统调用细节（那是 u5-l3 / u5-l4 的任务），也**不展开**平台检测与后端分派（那是 u5-l2 的任务）。

## 2. 前置知识

在开始之前，请确认你理解以下几个概念（若不熟，可先回顾 u1-l4 与 u2-l2）：

- **共享内存 IPC 的本质**：两个进程把同一块物理内存页映射到各自独立的虚拟地址空间，于是双方对这块内存的读写对彼此立即可见。它是所有「零拷贝」IPC 的底座。
- **RAII**：C++ 的资源获取即初始化——在构造函数里获取资源、在析构函数里释放资源，保证异常安全。u2-l2 的 `buffer`、u2-l3 的 `chan_wrapper` 都是 RAII 类型。
- **PIMPL（pointer to implementation）**：对外只暴露一个不透明指针 `p_`，把真正的成员藏到 `.cpp` 里，从而隔离实现、稳定 ABI。u2-l2 已经讲过这套手法。
- **`std::atomic` 与内存序**：`fetch_add` / `fetch_sub` / `load` 配合 `acquire`/`release`/`acq_rel` 内存序，实现无锁的并发计数。本讲会用到，但细节不是重点。

一个关键的直觉先建立起来：**在共享内存里做引用计数，最省事的办法不是另开一个文件或内核对象来计数，而是把那个计数器本身放进共享内存**——因为每个打开同一名字的进程都映射到了同一块物理页，计数器天然就是「全局共享」的。本讲的核心就是讲透这个设计。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| [include/libipc/shm.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/shm.h) | 共享内存公共头。声明了底层 C 风格函数（`acquire`/`get_mem`/`release`/`remove`/`get_ref`/`sub_ref`）和高级 RAII 类 `handle`。 |
| [src/libipc/shm.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/shm.cpp) | `handle` 类的实现（PIMPL 藏在这里），把对底层函数的调用包装成 RAII 生命周期。 |
| [src/libipc/platform/posix/shm_posix.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/shm_posix.cpp) | POSIX 后端，是引用计数真正落地的地方（`info_t`、`calc_size`、`acc_of`）。本讲读它来理解计数器机制。 |
| [src/libipc/platform/win/shm_win.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/shm_win.cpp) | Windows 后端，用来对比「为什么 Windows 的 `remove(name)` 是空操作」。 |
| [src/libipc/utility/pimpl.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/utility/pimpl.h) | PIMPL 辅助模板，解释 `handle` 如何持有并销毁内部 `handle_` 对象。 |
| [test/test_shm.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/test/test_shm.cpp) | 项目的共享内存测试套件，本讲多处用它佐证行为。 |

阅读顺序建议：先扫一眼 `shm.h` 建立 API 全貌 → 读 `shm.cpp` 看 `handle` 怎么委托 → 下钻 `shm_posix.cpp` 看引用计数落地 → 最后对照 `shm_win.cpp`。

---

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

1. `handle` 的 RAII 与 PIMPL 接口
2. `acquire` 的 create / open 两种模式
3. 嵌入式跨进程引用计数
4. `release` / `remove` / `clear` / `clear_storage` 的语义差异

### 4.1 handle 的 RAII 与 PIMPL 接口

#### 4.1.1 概念说明

`ipc::shm::handle` 是用户面向的共享内存句柄类型。它的职责可以用一句话概括：**「拿到一块共享内存，记住它的名字/尺寸/指针，并在自己析构时把引用还回去」**。

它和 u2-l2 的 `buffer`、u2-l3 的 `chan_wrapper` 一样，采用 PIMPL：

- 对外只暴露一个不透明指针 `handle_* p_`，真正的成员（名字、尺寸、内存指针、底层 `id_t`）藏在一个内部类 `handle_` 里。
- 这样做的好处是：头文件 `shm.h` 不需要 `#include <string>`、不需要暴露平台相关的 `id_info_t`，ABI 稳定。

但要注意：`shm.h` 里其实有**两套** API：

- 一套是底层 C 风格的自由函数 `shm::acquire / get_mem / release / remove / get_ref / sub_ref`，它们直接操作一个 `id_t`（其实就是 `void*`）。
- 一套是高级 RAII 类 `handle`，它内部持有 `id_t` 并把上述函数包装成成员函数。

本讲的 4.1、4.2 主要看高级 `handle`；4.3、4.4 必须下钻到底层函数，因为引用计数和清理逻辑真正发生在那里。

#### 4.1.2 核心流程

`handle` 的生命周期大致是：

```
构造（带 name+size）
  └─ acquire() → shm::acquire() 拿到 id_t → shm::get_mem() 映射并自增引用计数
       └─ 此后 get() 返回内存指针，ref() 查计数，sub_ref() 手动减计数

析构（或显式 release()）
  └─ release() → detach() 把 id 取出并清空成员 → shm::release() 自减引用计数
       └─ 若是最后一个引用：munmap + 删除磁盘文件
```

关键设计点：构造即连接、析构即释放（RAII）；`get()` 永远返回同一块映射内存的起始指针，`size()` 报告这块内存的大小。

#### 4.1.3 源码精读

先看公共头里 `handle` 的完整声明（注意它把「底层函数」和「RAII 类」放在一起）：

[include/libipc/shm.h:49-82](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/shm.h#L49-L82) — `handle` 类声明。可以看到三组接口：访问器（`valid/size/name/get/ref`）、生命周期（构造/析构/`acquire`/`release`/`clear`/`clear_storage`）、以及低级的 `attach`/`detach`（直接转交原始 `id_t`，后面 4.4 会讲）。

注意 `id_t` 就是 `void*`：

[include/libipc/shm.h:11-16](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/shm.h#L11-L16) — `id_t = void*` 以及 `create=0x01`/`open=0x02` 两个模式位（4.2 详讲）。

再看 `handle` 的内部实现类 `handle_`：

[src/libipc/shm.cpp:14-21](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/shm.cpp#L14-L21) — `handle::handle_` 持有四个成员：底层 `id_t id_`、内存指针 `m_`、名字 `n_`（`std::string`）、尺寸 `s_`。它继承自 `pimpl<handle_>`。

这里有个细节值得点明：因为 `handle_` 含有一个 `std::string n_`，其 `sizeof` 远大于一个指针（8 字节），所以 PIMPL 辅助模板走的是「堆分配」分支——`make()` 用 `mem::$new<handle_>()` 在堆上构造，`clear()` 用 `mem::$delete` 释放：

[src/libipc/utility/pimpl.h:37-45](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/utility/pimpl.h#L37-L45) — 对「较大」的实现类，`make_impl` 走堆分配、`clear_impl` 走堆释放。

于是默认构造和析构就很清晰：

[src/libipc/shm.cpp:23-25](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/shm.cpp#L23-L25) — 默认构造只 `make()` 出一个空的 `handle_`（所有成员都是默认初值：`id_=nullptr`、`m_=nullptr`、`s_=0`），此时 `valid()` 为 false。

[src/libipc/shm.cpp:37-40](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/shm.cpp#L37-L40) — 析构先 `release()`（把共享内存引用还回去），再 `p_->clear()`（释放堆上的 `handle_`）。这就是 RAII 的核心：**栈上 `handle` 一消失，共享内存的引用计数就自动减一**。

访问器都很直白，`valid()` 以「内存指针非空」为准：

[src/libipc/shm.cpp:51-69](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/shm.cpp#L51-L69) — `valid/size/name` 直接读 `handle_` 成员；`ref()` 委托给底层 `shm::get_ref`，`sub_ref()` 委托给 `shm::sub_ref`。

`handle` 是 move-only 类型，move 构造与赋值都通过 `swap` 交换那个唯一的 `p_` 指针：

[src/libipc/shm.cpp:32-49](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/shm.cpp#L32-L49) — 注意 `operator=(handle rhs)` 的右值是**按值传入**的（move 构造产生），再 `swap`，等价于 move 赋值。

#### 4.1.4 代码实践

**实践目标**：验证 `handle` 的 RAII 行为——构造即映射、析构即释放、move 后源对象失效。

**操作步骤**（这是源码阅读 + 断言理解型实践，基于项目测试 [test/test_shm.cpp:222-260](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/test/test_shm.cpp#L222-L260)）：

阅读 `HandleDefaultConstructor`、`HandleConstructorWithParams`、`HandleMoveConstructor` 三个测试，然后回答：

1. 默认构造的 `handle`，`valid()`/`size()`/`get()` 各是什么？
2. 带参构造 `shm::handle h(name, 1024)` 后，`valid()`、`size()` 与 `1024` 是什么关系（`==` 还是 `>=`）？
3. `shm::handle h2(std::move(h1))` 之后，`h1.valid()` 是 true 还是 false？

**预期结果 / 答案**：

1. `valid()==false`、`size()==0`、`get()==nullptr`。
2. 是 `>=` 关系——`size()` 会包含引用计数占用的尾部字节（见 4.3），所以可能略大于请求值。测试用的是 `EXPECT_GE(h.size(), size)` 而非 `EXPECT_EQ`。
3. `h1.valid()==false`——move 后源对象交出了 `p_`。

> 若要在本机实际运行：按 u1-l2 用 `cmake -DLIBIPC_BUILD_TESTS=ON` 构建，再执行 `test/test-ipc --gtest_filter=ShmTest.Handle*`。无法运行则标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`handle` 的析构函数里为什么是先 `release()` 再 `p_->clear()`，顺序能反吗？

**答案**：不能反。`release()` 需要读取 `handle_` 里的 `id_`、`m_`、`s_` 来调用底层 `shm::release`；若先 `clear()`（`mem::$delete(p_)`）销毁了 `handle_`，这些成员就成了悬空内存。所以必须先还共享内存引用，再释放本地的 `handle_` 堆对象。

**练习 2**：为什么 `handle` 头文件里完全看不到 `std::string`、`#include <string>`，却能存名字？

**答案**：因为 `n_` 这个 `std::string` 成员藏在 `.cpp` 的内部类 `handle_` 里（PIMPL）。头文件只前向声明了 `class handle_;`，`std::string` 的完整定义只在 `shm.cpp` 里可见。

---

### 4.2 acquire 的 create / open 两种模式

#### 4.2.1 概念说明

共享内存是「按名字寻址」的：两个进程用同一个名字 `acquire`，就能拿到同一块内存。但「这块内存之前存不存在」有两种可能，于是 libipc 用两个模式位来精确表达意图：

- `create`（`0x01`）：**我希望新建**。若该名字已存在，则失败（POSIX 用 `O_CREAT | O_EXCL`）。
- `open`（`0x02`）：**我希望打开已存在的**。若不存在，则失败。
- `create | open`（`0x03`，默认值）：**存在就打开、不存在就新建**——最宽松的「给我一块」语义。

`shm::handle` 的构造函数和 `acquire` 成员的第三个参数 `mode` 默认就是 `create | open`，所以平时写 `shm::handle h("foo", 1024)` 用的是最宽松模式。

#### 4.2.2 核心流程

`handle::acquire` 的流程：

```
acquire(name, size, mode):
  1. 校验 name 非空、size 非零，否则返回 false
  2. release()            ← 先释放本 handle 之前可能持有的资源（可重复 acquire）
  3. id = shm::acquire(name, size, mode)   ← 后端按 mode 走 create/open 分支
  4. 若 id 为空，返回 false
  5. 记下 id、name，调 shm::get_mem(id, &s_) 映射内存
  6. 返回 valid()（即映射成功与否）
```

后端 `shm::acquire` 根据 `mode` 选择系统调用的标志位（POSIX 是 `shm_open` 的 `O_*` 标志，Windows 是 `CreateFileMapping` 还是 `OpenFileMapping`）。

#### 4.2.3 源码精读

模式位的定义：

[include/libipc/shm.h:13-16](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/shm.h#L13-L16) — `create=0x01`、`open=0x02`，二者按位或得到 `0x03`。

`handle::acquire` 的实现，注意它**先 release 再 acquire**，因此对同一个 handle 重复调用是安全的：

[src/libipc/shm.cpp:71-90](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/shm.cpp#L71-L90) — 校验 → `release()` → `shm::acquire` → 记名 → `shm::get_mem` 映射 → 返回 `valid()`。

POSIX 后端如何把 mode 翻译成 `shm_open` 标志：

[src/libipc/platform/posix/shm_posix.cpp:62-79](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/shm_posix.cpp#L62-L79) — 三分支：`open`（`size` 置 0，纯打开）；`create`（`O_CREAT | O_EXCL`，存在即失败）；`default` 即 `create|open`（仅 `O_CREAT`，存在则打开已有对象）。注意 `O_EXCL` 只在严格 `create` 时加上。

Windows 后端的等价逻辑：

[src/libipc/platform/win/shm_win.cpp:56-79](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/shm_win.cpp#L56-L79) — `open` 走 `OpenFileMapping`；其余走 `CreateFileMapping`，其中严格 `create` 时若 `GetLastError()==ERROR_ALREADY_EXISTS` 则关闭句柄并失败。

#### 4.2.4 代码实践

**实践目标**：理解三种模式对「对象是否已存在」的敏感度。

**操作步骤**：阅读测试 [test/test_shm.cpp:464-479](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/test/test_shm.cpp#L464-L479)（`HandleModes`），它依次用 `create`、`open`、`create|open` 连同一个名字。

**需要观察的现象**：第一个用 `create` 成功新建；第二个用 `open` 打开已存在的成功；第三个用 `create|open` 也成功。

**预期结果**：三个 handle 全部 `valid()==true`。若把第一个改成 `open`（对象还不存在），则应失败、`valid()==false`（见 `AcquireOpenNonExistent` 测试 [test/test_shm.cpp:58-66](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/test/test_shm.cpp#L58-L66)）。

#### 4.2.5 小练习与答案

**练习 1**：为什么默认 `mode = create | open` 而不是单纯 `create`？

**答案**：因为 `handle` 经常被多个进程独立构造去连同一块内存。若默认是严格 `create`，第二个进程构造时就会因「对象已存在」而失败。`create|open` 让「第一个进程新建、后续进程打开」自动适配，无需调用方区分先后顺序。

**练习 2**：在 POSIX 后端，`open` 模式时为什么要把 `size` 置为 0？

**答案**：打开已存在对象时，调用方请求的 size 无意义（实际大小由当初创建者决定）。置 0 是个哨兵，告诉 `get_mem`「别用 ftruncate 改大小，而是用 fstat 读出真实大小」（见 4.3 的 `get_mem` 分支）。

---

### 4.3 嵌入式跨进程引用计数

#### 4.3.1 概念说明

这是本讲最核心的模块。问题是：**多进程共享一块内存，谁负责在「最后一个进程用完」时把它从系统里删掉？**

如果第一个进程用完就删，会坑到还在用的其他进程；如果谁都不删，内存对象会泄漏（POSIX 下是 `/dev/shm` 里的文件残留，直到重启）。

解法是引用计数：每有一个进程映射这块内存，计数器 `+1`；每有一个进程释放，计数器 `-1`；**当计数归零（即这次 `release` 前，计数恰好为 1）时，由这次 release 顺手删除磁盘文件**。

libipc 的巧妙之处在于：**它不另开一个文件或内核对象来存这个计数器，而是把计数器塞进共享内存自己的最后 4 个字节**。这样：

- 每个打开同一名字的进程，映射到的物理页是同一页，于是大家看到的是**同一个**计数器。
- 不需要额外的初始化协议——新建时 `ftruncate` 把文件清零，计数器天然从 0 开始，第一个 `get_mem` 把它 `fetch_add` 到 1。
- `std::atomic` 的 `fetch_add`/`fetch_sub` 在 `MAP_SHARED` 内存上跨进程是可靠的（依赖 CPU 缓存一致性），所以无需加锁。

#### 4.3.2 核心流程

计数器是一个只含一个原子整数的结构 `info_t`：

```
info_t { std::atomic<std::int32_t> acc_; }   // 仅 4 字节
```

实际映射大小 = 「用户请求大小向上对齐到 4」+ 4（计数器）。计数器地址 = 映射起点 + 大小 − 4，即整块内存的**最后一个 `info_t`**：

\[
\text{mapped\_size} = \big(\lfloor(\text{size}-1)/4\rfloor + 1\big)\times 4 \;+\; 4
\]

\[
\text{counter\_addr} = \text{base} + \text{mapped\_size} - \text{sizeof(info\_t)}
\]

关键流程：

```
get_mem(id):       ← 映射内存时
  mmap(...) 得到 mem
  acc_of(mem, size).fetch_add(1, release)   ← 引用 +1

release(id):       ← 释放时
  ret = acc_of(mem, size).fetch_sub(1, acq_rel)   ← 返回「减之前的值」
  if (ret <= 1):          ← 减之前是 1，说明我是最后一个
      munmap(...)
      shm_unlink(name)    ← 删掉磁盘文件（POSIX）
  else:
      munmap(...)         ← 还有别人在用，只解除我自己的映射
  free(id_info_t)         ← 释放本地 id 结构
```

注意 `fetch_sub` 返回的是**减法之前的旧值**。所以「减之前是 1」就意味着「减完就是 0、我是最后一个」，这时才删文件。这是一个经典的「最后一个负责清理」模式。

另一个要点：`acquire` **不会**自增计数器，`get_mem` 才会。所以「acquire 到 id 但没 get_mem」时，`get_ref` 读到的是 0。

#### 4.3.3 源码精读

计数器结构与地址计算（POSIX 后端）：

[src/libipc/platform/posix/shm_posix.cpp:23-40](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/shm_posix.cpp#L23-L40) — `info_t`（4 字节原子）、`calc_size`（向上对齐到 `alignof(info_t)=4` 再加 `sizeof(info_t)=4`）、`acc_of`（定位到末尾的计数器）。

`get_mem` 里的映射 + 自增。注意它分两条路径：`size==0`（open 模式）用 `fstat` 读真实大小，`size>0`（create 模式）用 `ftruncate` 设大小：

[src/libipc/platform/posix/shm_posix.cpp:133-167](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/shm_posix.cpp#L133-L167) — `ftruncate`（设大小，新文件被清零）、`mmap`（`MAP_SHARED` 映射）、最后一行 `acc_of(...).fetch_add(1, release)` 自增计数器。**这就是「嵌入计数」自增的唯一发生点**。

`release` 里的自减与「最后一个负责删」：

[src/libipc/platform/posix/shm_posix.cpp:170-193](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/shm_posix.cpp#L170-L193) — `fetch_sub(1, acq_rel)` 返回旧值 `ret`；`ret <= 1` 时 `munmap` + `shm_unlink`（删文件），否则只 `munmap`；最后 `mem::$delete(ii)` 释放本地 id 结构。注意 Windows 后端的 `release` **没有** `shm_unlink`：

[src/libipc/platform/win/shm_win.cpp:149-170](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/shm_win.cpp#L149-L170) — 同样 `fetch_sub` 自减并 `UnmapViewOfFile`，但只 `CloseHandle`，不删除命名对象。原因见 4.4。

`get_ref` / `sub_ref` 这两个底层函数（`handle::ref()`/`sub_ref()` 委托给它们）：

[src/libipc/platform/posix/shm_posix.cpp:97-120](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/shm_posix.cpp#L97-L120) — `get_ref` 用 `load(acquire)` 读计数；`sub_ref` 用 `fetch_sub(acq_rel)` 减一（但不 munmap、不删文件——它只是「手动减计数」）。

#### 4.3.4 代码实践

**实践目标**：用一块共享内存做跨进程自增计数器，亲眼看到引用计数随 `get_mem`/`release` 涨落。

**操作步骤**：

1. 阅读测试 [test/test_shm.cpp:163-194](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/test/test_shm.cpp#L163-L194)（`ReferenceCount`）。它用 `get_ref` 验证：acquire 之后、get_mem 之前，计数是 0；get_mem 之后是 1；再 open+get_mem 第二个 id 后是 2；release 第二个后……
2. 自己写一个两 handle 版本（模拟两进程）：

```cpp
// 示例代码：基于 shm::handle 的引用计数观察
#include "libipc/shm.h"
#include <cstdio>

int main() {
    const char* name = "my_counter_demo";
    ipc::shm::handle h1(name, 256);          // create|open，内部 get_mem → ref=1
    std::printf("h1 ref = %d\n", h1.ref());  // 预期 1

    ipc::shm::handle h2(name, 256, ipc::shm::open);  // open 同一块 → ref=2
    std::printf("h2 ref = %d\n", h2.ref());  // 预期 2

    // 用 h1 在共享内存里写一个自增计数
    int* p = static_cast<int*>(h1.get());
    *p = *p + 1;
    std::printf("h2 sees value = %d\n", *static_cast<int*>(h2.get())); // 预期 1，零拷贝可见
}
// h2、h1 离开作用域：ref 2→1（h2 释放，只 munmap）、1→0（h1 释放，munmap + 删文件）
```

**需要观察的现象**：

- `h1.ref()` 打印 1，`h2.ref()` 打印 2——证明计数器被两个 handle 共享。
- `h2` 能读到 `h1` 写入的值——证明两块映射指向同一物理页。
- 程序结束后，POSIX 下 `ls /dev/shm/` 不应再有名为 `my_counter_demo` 的文件——证明最后一个释放者删除了它。

**预期结果**：输出 `h1 ref = 1`、`h2 ref = 2`、`h2 sees value = 1`。若不运行，则计数涨落与文件清理行为标注「待本地验证」。可借助测试 [test/test_shm.cpp:482-498](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/test/test_shm.cpp#L482-L498)（`MultipleHandles`，两 handle 互写可见）佐证。

#### 4.3.5 小练习与答案

**练习 1**：为什么计数器放在共享内存「末尾」而不是「开头」？

**答案**：放在末尾可以让用户数据区从偏移 0 开始连续——`handle::get()` 返回的就是用户数据的起点，用户无需跳过一个头部。计数器地址用 `base + size - 4` 计算，对用户透明。此外末尾追加对「按用户请求大小向上对齐」的实现最简单。

**练习 2**：假设进程 A `get_mem` 后计数=1，进程 B `get_mem` 后计数=2。此时 A 崩溃（没来得及 release），之后 B 正常 release。会发生什么？

**答案**：B 的 `fetch_sub` 把计数从 2 减到 1（旧值 2，`ret=2 > 1`），所以 B 只 munmap 自己的映射，**不会删文件**——因为计数器还显示有 1 个引用（A 残留的）。结果是磁盘文件泄漏。这正是 libipc 仍提供 `remove(name)` / `clear_storage(name)` 这类「按名字强制删」接口的原因（见 4.4）。这也说明：**嵌入式引用计数防不住进程崩溃**，健壮场景需配合后续讲义提到的命名清理。

**练习 3**：`sub_ref` 和 `release` 都减计数，区别是什么？

**答案**：`sub_ref`（底层 `shm::sub_ref`）**只** `fetch_sub` 一下，不 munmap、不删文件、不释放 id 结构——它是「手动微调计数」的逃生口。`release` 是完整的释放：减计数 + munmap +（若归零）删文件 + 释放 id 结构。`handle::sub_ref()` 暴露前者，`handle::~handle()` 走后者。

---

### 4.4 release / remove / clear / clear_storage 的语义差异

#### 4.4.1 概念说明

`shm::handle` 提供了一组看起来都在「清理」的接口，初学者很容易混淆。本模块把它们一次厘清。先给结论表：

| 接口 | 作用 | 是否减引用计数 | 是否删磁盘文件 | handle 之后是否有效 |
|------|------|:---:|:---:|:---:|
| `~handle()` / `release()` | 正常释放（RAII 路径） | 是 | 仅当**最后一个**时删 | 否（`detach` 后清空） |
| `clear()` | 强制清理本 handle 持有的资源 | 是（经 release） | **无条件**删（POSIX） | 否 |
| `clear_storage(name)`（静态） | 按名字删残留文件 | 否（不动任何 id/映射） | 删名字（POSIX） | 不涉及（静态） |
| `attach(id)` / `detach()` | 转移原始 id，**不碰计数** | 否 | 否 | detach 后无效 |

还有两个底层 `remove` 重载（`handle::clear` / `clear_storage` 内部调用）：

- `shm::remove(id)`：先 `release(id)`，再**无条件** `shm_unlink`（删文件）。
- `shm::remove(name)`：**只** `shm_unlink(name)`，不影响任何活动的映射或 id 结构。

#### 4.4.2 核心流程

三者的清理强度递增：

```
release()        → 「礼貌退场」：减自己的引用；只在没人再用时才收拾磁盘
clear()          → 「我走了，顺便把门锁拆了」：减引用 + 无论还有谁都用 shm_unlink 删名字
clear_storage(n) → 「扫地」：不碰任何运行中的资源，只对磁盘上的名字做 shm_unlink
```

POSIX 与 Windows 在「删文件」上有本质差异，这是理解 `clear_storage` / `remove(name)` 跨平台行为的关键：

- **POSIX**：共享内存是 `/dev/shm` 下的命名文件，`shm_unlink(name)` 删除该名字。已映射的进程不受影响（映射仍有效，直到它们自己 munmap）。
- **Windows**：命名文件映射对象没有「磁盘文件」概念，名字的生命周期等同于「最后一个内核句柄关闭」。所以 Windows 后端的 `remove(name)` 是**空操作**——它没法按名字删一个还活着的映射对象。

#### 4.4.3 源码精读

`handle::release`：判空后把 id 取出（`detach`）交给底层 `shm::release`：

[src/libipc/shm.cpp:92-95](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/shm.cpp#L92-L95) — `id_==nullptr` 时返回 -1；否则 `shm::release(detach())`。`detach` 会清空所有成员，所以 release 后 handle 失效。

`handle::clear`：调用 `shm::remove(detach())`，即「release + 无条件 shm_unlink」：

[src/libipc/shm.cpp:97-100](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/shm.cpp#L97-L100) — 注意它**不**先 release 再 remove（那会 double-free id），而是直接走 `shm::remove(id)`，后者内部已含 release。

`handle::clear_storage`（静态）：只按名字删磁盘文件：

[src/libipc/shm.cpp:102-107](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/shm.cpp#L102-L107) — 委托 `shm::remove(name)`，完全不碰任何 `id` 或映射。

底层两个 `remove` 重载（POSIX）：

[src/libipc/platform/posix/shm_posix.cpp:195-229](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/shm_posix.cpp#L195-L229) — `remove(id)`：先 `release(id)` 再无条件 `shm_unlink`（删名字，但已有映射仍有效）；`remove(name)`：纯 `shm_unlink`。

Windows 后端的对比——`remove(name)` 是空操作：

[src/libipc/platform/win/shm_win.cpp:181-188](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/shm_win.cpp#L181-L188) — 注释 `// Do Nothing.`。Windows 无法在句柄还开着时按名字删除映射对象。`remove(id)` 也只是调 `release` 而已（[shm_win.cpp:172-179](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/shm_win.cpp#L172-L179)）。

最后看 `attach` / `detach`——它们是绕开引用计数的「原始 id 转交」通道：

[src/libipc/shm.cpp:113-127](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/shm.cpp#L113-L127) — `attach` 先 `release()` 自己旧的，再接管传入的 id 并 `get_mem` 取指针（**不再 fetch_add**）；`detach` 把 id 原样吐出并清空成员（**不再 fetch_sub**）。所以 attach/detach 是把一个 id 在两个 handle 之间「搬家」，计数不变。测试 [test/test_shm.cpp:417-435](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/test/test_shm.cpp#L417-L435) 演示了这对用法。

#### 4.4.4 代码实践

**实践目标**：区分 `release()`（礼貌退场）与 `clear_storage(name)`（按名字强制扫地），并理解 POSIX 下「名字被删但映射仍活」。

**操作步骤**：

1. 阅读测试 [test/test_shm.cpp:386-399](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/test/test_shm.cpp#L386-L399)（`HandleClearStorage`）：先用一个作用域内的 `handle` 创建并立即析构，再调 `clear_storage(name)` 清扫，最后尝试 `open` 同名。
2. 自己写（示例代码）：

```cpp
// 示例代码：观察 clear_storage 的「扫地」语义
#include "libipc/shm.h"
#include <cstdio>

int main() {
    const char* name = "sweep_demo";
    {
        ipc::shm::handle h(name, 256);   // create|open，ref=1
        std::printf("in-scope ref = %d\n", h.ref());
    } // h 析构 → ref 1→0 → 自动删文件（POSIX）

    // 此时文件本已被最后一个 release 删掉了；clear_storage 是「确保」无残留的兜底
    ipc::shm::handle::clear_storage(name);   // 即使文件已不在也安全（shm_unlink 失败仅记日志）
    std::printf("swept.\n");
}
```

**需要观察的现象**：

- 作用域结束时 `ref` 从 1 降到 0，最后一个释放者已删文件。
- 之后 `clear_storage` 再调一次 `shm_unlink`，即使文件已不存在也只记录一条日志、不崩溃（见 [shm_posix.cpp:212-229](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/shm_posix.cpp#L212-L229) 的错误处理）。

**预期结果**：打印 `in-scope ref = 1`、`swept.`，程序正常退出。Windows 平台上 `clear_storage` 实际是空操作，文件对象随最后一个 handle 析构自动消失——跨平台行为差异请标注「待本地验证（Windows）」。

#### 4.4.5 小练习与答案

**练习 1**：调用 `h.clear()` 之后还能继续用 `h.get()` 吗？

**答案**：不能。`clear()` 内部 `detach()` 把 `m_` 置空、`id_` 置空，此后 `valid()` 为 false、`get()` 返回 `nullptr`。`clear()` 是终结性操作。

**练习 2**：为什么 POSIX 需要 `shm_unlink`，而 Windows 的 `remove(name)` 是空操作？

**答案**：POSIX 的共享内存是 `/dev/shm` 下一个真实的命名文件，必须显式 `shm_unlink` 删除，否则重启前一直残留。Windows 的命名文件映射对象没有独立磁盘文件，其「名字」只在至少有一个内核句柄打开时才存在——所有句柄关闭后对象自动消失，所以没有「按名字删除」这个动作可做，`remove(name)` 只能是 no-op。

**练习 3**：`attach(id)` 之后，引用计数会变成多少？

**答案**：不变。`attach` 不调用 `fetch_add`，它只是接管一个已存在的 id（连同其已映射的内存指针）。所以如果传入的 id 之前 `get_mem` 过（计数已是 N），attach 后仍是 N。配套地，`detach` 也不 `fetch_sub`——这一对用于「把 id 搬到另一个 handle/线程」而不重复计数。

---

## 5. 综合实践

把本讲四个模块串起来：写一个**双进程共享计数器**，综合验证 acquire 模式、引用计数、跨进程可见性与清理语义。

**任务**：

1. 进程 A（`writer`）：用 `shm::handle h("cnt", sizeof(int))` 以默认 `create|open` 创建一块共享内存，把 `*get<int>()` 初始化为 0，然后循环自增并打印，每次自增后用 `ref()` 打印当前引用计数；循环若干次后退出（handle 析构）。
2. 进程 B（`reader`）：在 A 运行期间用 `shm::handle h("cnt", sizeof(int), ipc::shm::open)` 打开同一块内存，读取并打印计数器值与 `ref()`（应看到 ref 因 B 的加入而 +1）；退出后让 A 观察到 ref 回落。
3. 在 A、B 都退出后，命令行执行 `ls /dev/shm/cnt`（POSIX）确认文件已被最后一个释放者删除；若残留，用 `ipc::shm::handle::clear_storage("cnt")` 兜底清理。

**关注点**：

- 两进程看到的 `ref()` 是否一致（验证嵌入计数器跨进程共享）。
- B 是否能读到 A 写入的最新值（验证零拷贝可见）。
- 最后退出者是否删除了磁盘文件（验证「最后一个负责清理」）。
- 若某进程被 `kill -9`，文件是否会泄漏（验证 4.3 练习 2 的结论）。

> 这是对 `route`/`channel` 等高级 API 底层基石的「白盒」实践。本任务无现成 demo，需自行编写并编译（链接 `ipc` 目标，见 u1-l2）。若无法在本机运行双进程，可降级为单进程内两个 `handle`（如 4.3.4 示例）并标注「待本地验证（双进程）」。

## 6. 本讲小结

- `ipc::shm::handle` 是共享内存的 RAII 句柄，用 PIMPL 把名字/尺寸/指针/底层 `id_t` 藏在 `handle_` 里；构造即映射、析构即释放。
- `acquire` 的 `mode` 有 `create` / `open` / `create|open`（默认）三种，分别表示「必须新建」「必须已存在」「存在就开、不存在就建」，由后端翻译成平台系统调用标志。
- **引用计数器嵌在共享内存的最后 4 字节**（`info_t::acc_`），靠 `MAP_SHARED` 让所有进程看到同一原子量；`get_mem` 时 `fetch_add`，`release` 时 `fetch_sub`，旧值 ≤1 的那个进程负责删磁盘文件。
- `acquire` 不增计数、`get_mem` 才增；`release` 是完整释放，`sub_ref` 只减计数不拆映射。
- `release()`=礼貌退场（仅最后一人删文件）、`clear()`=强制删本 id、`clear_storage(name)`=按名字扫地（不动运行中映射）；POSIX 靠 `shm_unlink` 删 `/dev/shm` 文件，Windows 的 `remove(name)` 是空操作。
- `attach`/`detach` 是绕开计数的原始 id 转交通道；嵌入式计数防不住进程崩溃，泄漏场景需 `clear_storage` 兜底。

## 7. 下一步学习建议

本讲只读了 POSIX/Windows 后端的「计数与清理」片段，刻意回避了系统调用细节与平台分派机制。接下来：

- **u5-l2 平台检测与后端分派**：去看 `detect_plat.h` 的 `LIBIPC_OS_*` 宏如何驱动 [platform.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/platform.cpp) 在编译期 `#include` 进 `shm_posix.cpp` 或 `shm_win.cpp`，理解本讲两个后端是如何「同一份 `shm.h` 接口、两份实现」串起来的。
- **u5-l3 POSIX 共享内存后端**：精读 `shm_open`/`ftruncate`/`mmap`/`shm_unlink` 的完整调用序列与 `fstat` 探测大小的细节。
- **u5-l4 Windows 共享内存后端**：精读 `CreateFileMapping`/`MapViewOfFile`/`VirtualQuery` 与 `Global\` 前缀跨会话命名。
- 之后 u6（同步原语）会回到「如何在共享内存里放互斥量/条件变量」，与本讲的「在共享内存里放计数器」是同一套思路的延伸，可对照阅读。

> 建议边读边在本机跑 `test/test_shm.cpp`（`cmake -DLIBIPC_BUILD_TESTS=ON`），用断言校验你对引用计数涨落的预测。
