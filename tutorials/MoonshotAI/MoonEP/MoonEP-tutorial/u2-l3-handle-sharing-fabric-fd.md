# u2-l3 跨进程共享：fabric 句柄与 POSIX fd

## 1. 本讲目标

上一讲（u2-l2）我们搞清楚了 NVLink 对称内存的「分配-映射」两步模型：每个 rank 用 `cuMemCreate` 在自己 GPU 上分配一段物理显存，再把它映射进一块全组连续的虚拟地址。但有一个环节当时被刻意跳过了——`nvl_dist_alloc` 返回的 **shareable 句柄**是怎么从 rank A 的进程送到 rank B 的进程手里的？

本讲专讲这个「送句柄」的问题。学完后你应该能够：

1. 说清 **POSIX fd 与 fabric handle 的本质区别**：一个是内核对象引用（不可序列化），一个是 64 字节不透明字节串（可序列化），以及由此决定的两种传输通道。
2. 读懂 `_exchange_ipc_fds`：理解 SCM_RIGHTS 在 unix datagram socket 上传 fd 的内核语义，以及两个 `dist.barrier` 各自保证什么。
3. 读懂 `_use_fabric_for_group`：掌握 `MOONEP_MEM_HANDLE_TYPE=auto/fabric/fd` 三种模式的探测、协商与回退逻辑，理解为什么探测必须做一次真实的试分配。

## 2. 前置知识

### 2.1 fd 是什么：编号、钥匙与打开文件描述

- **fd（文件描述符）**是一个小整数，它是**当前进程** fd 表的下标。fd 表的每一项指向内核里的一个「打开文件描述（open file description）」。
- 关键性质：**fd 的编号只在同一操作系统实例（同一内核）内有意义**。rank 0 进程里的 fd=7，拿到 rank 1 进程里毫无意义——对方的 fd 表里 7 号可能指向完全无关的东西。
- 所以「把 fd 发给另一个进程」**不可能通过发送编号本身实现**。无论用 NCCL 传张量还是 TCP 发字节，传过去的都只是一个数字，不携带任何内核引用。
- UNIX 提供的正统机制是 **SCM_RIGHTS**：通过 unix domain socket 的 `sendmsg`，把 fd 作为辅助数据（ancillary data / 控制消息 cmsg）交给内核，由**内核**把发送方 fd 指向的打开文件描述**复制**一份插入接收方进程的 fd 表。接收方 `recvmsg` 返回时拿到的是一个**自己进程内的新编号**，指向同一份内核对象——相当于「复制一把钥匙挂到对方家的钥匙架上」。

### 2.2 unix domain socket 与 SOCK_DGRAM

- `AF_UNIX` socket 是同一操作系统内进程间通信的机制，地址是文件系统里的一个路径（如 `/tmp/moonep_ipc_xxx/rank_3`）。
- MoonEP 用的是 `SOCK_DGRAM`（数据报）：每条 `sendmsg` 是一条**独立、有边界**的消息，辅助数据随消息一起原子送达。接收方一次 `recvmsg` 就能同时拿到「4 字节正文 + 一个 fd」，不会像流式 socket 那样发生半条消息的粘包错位。

### 2.3 fabric handle 是什么

- `CUmemFabricHandle` 是 CUDA VMM 的另一种可共享句柄：**64 字节的不透明字节串**（`cuMemExportToShareableHandle` 用 `CU_MEM_HANDLE_TYPE_FABRIC` 导出得到）。
- 它不是内核引用，而是一个**由 NVLink fabric（跨节点 NVLink 网络，通常由 fabric manager 管理）背书的全局名字**。只要发送方与接收方在同一个 NVLink fabric 域内，接收方把这 64 字节原样交给 `cuMemImportFromShareableHandle` 就能导入同一份物理分配——**跨节点也成立**。
- 因为它就是普通字节，**可以走任何字节通道**：NCCL 集合通信、TCP、写文件都行。这和 fd 形成鲜明对比。

### 2.4 承接 u2-l2

u2-l2 已建立的结论（本讲直接使用，不再展开）：

- `nvl_dist_alloc(shape, dtype, use_fabric)` 返回三元组 `(keepalive, shareable, owned_handle)`，其中 `shareable` 就是本讲要分发的句柄：`use_fabric=True` 时是 `uint8[64]` 张量，否则是装着 fd 的 `int64` 标量张量。
- `nvl_dist_map` 把全组句柄按 rank 顺序映射成连续 VA，且要求句柄张量是 **CPU 连续张量**。
- Python 侧的 padding（`pad_dim0_for_alignment`）用的是 fd/fabric 两类句柄粒度的**最大值**，因此无论后续探测选中哪种句柄，对齐都不会失效。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注的符号 |
| --- | --- | --- |
| [moonep/buffer.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py) | 句柄分发的全部 Python 侧逻辑（本讲主角） | `_HANDLE_TYPE_ENV`、`_use_fabric_for_group`、`_all_gather_shareables`、`_broadcast_shareable`、`_exchange_ipc_fds`、`_map_nvl_dist_tensor` |
| [csrc/nvl_shared_buffer.cuh](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/nvl_shared_buffer.cuh) | 句柄导出/导入与能力探测的 C++ 实现 | `kFabricHandleBytes`、`nvl_export_shareable`、`nvl_import_shareable`、`nvl_fabric_supported` |
| [csrc/bindings.cu](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/bindings.cu) | pybind11 导出层 | `FABRIC_HANDLE_BYTES`、`nvl_fabric_supported` 的模块级 docstring |

本讲不涉及 `api.py`——句柄分发完全封装在 `buffer.py` 内部，`Buffer` 构造函数感知不到 fd/fabric 的存在。

## 4. 核心概念与源码讲解

### 4.1 fd 通道：`_exchange_ipc_fds` 与 SCM_RIGHTS

#### 4.1.1 概念说明

问题：同节点的 R 个进程各自 `nvl_dist_alloc` 得到一个 fd，每个进程都需要拿到**其他所有进程**的 fd，才能调 `nvl_dist_map` 完成映射。fd 无法用张量通信传递（2.1 节），只能用 SCM_RIGHTS。

`_exchange_ipc_fds` 的设计要点：

- **每个 rank 一个收件箱**：组内 rank 0 建一个共享临时目录，广播路径；每个 rank 在里面 bind 一个 `rank_{i}` 的 unix datagram socket。
- **发送者可以是子集**：参数 `sender_ranks` 声明哪些 rank 会发 fd（其余 rank 只收不发）。MoonEP 有三个调用场景——对称内存（全员互发）、组播对象（只有 rank 0 发）、单 owner 缓冲（只有 owner 发）——一个函数通吃。
- **正文只带 4 字节来源标识**：`payload = struct.pack("<i", local_rank)`。接收方靠它知道收到的 fd 属于哪个 rank，用它作 `fds` 字典的键。fd 本身放在 cmsg 里由内核处理。
- **两个 barrier 夹住消息交换**：第一个保证「所有收件箱都 bind 好了才允许发」（发到一个不存在的路径会直接报错）；第二个（在 `finally` 里）保证「所有人都收完了才允许关 socket、关原始 fd、删目录」。

#### 4.1.2 核心流程

以「全员互发」（`sender_ranks = range(world_size)`，即 `_map_nvl_dist_tensor` 的调用方式）为例，R 个 rank 的时序：

```
rank0                                rank1 .. rankR-1
  │ mkdtemp("moonep_ipc_*")            │
  │◄──── broadcast_object_list(dir) ───┤        (1) 广播目录路径
  │ socket(AF_UNIX, DGRAM)             │ socket(...)
  │ bind(dir/rank_0)                   │ bind(dir/rank_1) ...
  │◄──── dist.barrier() ───────────────┤        (2) 收件箱全部就绪
  │ sendmsg(payload=0, SCM_RIGHTS=fd0) │          → 发给每个 rank_i（含自己）
  │        ...                         │ sendmsg(payload=i, SCM_RIGHTS=fd_i) ...
  │                                     │
  │ recvmsg 循环，直到收满 R 个 fd       │ recvmsg 循环 ...   (3) 内核 dup fd 进本进程
  │◄──── dist.barrier() ───────────────┤        (4) 全员已收完 → 安全清理
  │ rmtree(dir)（仅 rank0）             │
  │ 返回 fds = {源rank: fd}             │          (5) 调用方随后 os.close(原始fd)
```

注意 (3)：内核投递 SCM_RIGHTS 时会**在接收进程里新建一个 fd**，编号与发送方的原编号无关。所以发送方 barrier (4) 之后就能放心 `os.close` 自己的 fd——接收方持有的已经是独立的复制引用。

#### 4.1.3 源码精读

函数契约写在 docstring 里，值得整段读一遍：

[moonep/buffer.py:L113-L129](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L113-L129)
`_exchange_ipc_fds` 的签名与文档：`sender_ranks` 中的 rank 必须把自己的 fd 作为 `local_fd` 传入（其余传 `None`）；每个 rank 从每个 sender 收一个 fd；内核通过 SCM_RIGHTS 把 fd **dup** 进接收进程，因此返回的 fd 归本进程所有，导入后必须由调用方关闭；发送方自己的 fd 在函数返回后即可关闭——**尾部的 barrier 保证了所有对端都已拿到复制**。

第一步，目录的创建与广播：

[moonep/buffer.py:L130-L138](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L130-L138)
组内 rank 0 用 `tempfile.mkdtemp(prefix="moonep_ipc_")` 建目录，`broadcast_object_list` 把路径广播给全组（注意 `src` 用 `dist.get_global_rank` 把组内 rank 0 翻译成全局 rank——MoonEP 的 EP 组可能是全局进程组的一个子组）。

第二步，收件箱与第一个 barrier：

[moonep/buffer.py:L140-L144](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L140-L144)
每个 rank 创建 `AF_UNIX + SOCK_DGRAM` socket，bind 到 `dir/rank_{local_rank}`，设置 **120 秒超时**（防止某个 rank 崩溃时其余人无限阻塞）。`dist.barrier` 之后才允许任何人发送——注释 `# All sockets must be bound before any send.` 点明了原因：向一个尚未 bind 的 unix socket 路径 `sendmsg` 会失败。

第三步，发送：

[moonep/buffer.py:L146-L153](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L146-L153)
发送方构造两条数据：正文 `payload` 是本 rank 编号的小端 int32（4 字节）；辅助数据 `anc` 是一条 `(SOL_SOCKET, SCM_RIGHTS, 打包的 fd)` 控制消息。然后对**包括自己在内**的所有 `world_size` 个收件箱逐个 `sendmsg`。发给自己也是必要的——`_map_nvl_dist_tensor` 需要全组（含本 rank）的 fd 列表。

第四步，接收循环：

[moonep/buffer.py:L155-L164](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L155-L164)
循环 `recvmsg` 直到收满 `len(sender_ranks)` 个 fd。`recvmsg(16, socket.CMSG_SPACE(4))` 的两个参数分别是：16 字节正文缓冲（够放 4 字节 payload）、一条携带 4 字节数据（一个 int fd）的 cmsg 所需空间。从正文前 4 字节解出 `src_rank`，再从 cmsg 里解出**接收进程内新建的** fd 编号，存进 `fds[src_rank]`。若收到没有 fd 的消息则抛 `RuntimeError`。

第五步，清理与第二个 barrier：

[moonep/buffer.py:L165-L172](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L165-L172)
`finally` 块：先关自己的 socket，再 `dist.barrier`，然后仅 rank 0 删除整个临时目录。barrier 放在 `finally` 而不是正常路径末尾，是为了**任何 rank 抛异常时组也不会死锁在后续集合通信上**（各 rank 都会经过同一个 barrier 再退出）。此 barrier 之后：所有接收方的 fd 都已在各自的 fd 表里（内核已复制），发送方的原始 fd 与目录里的 socket 文件都可以安全销毁。

调用方如何使用返回的 fd（以对称内存为例）：

[moonep/buffer.py:L196-L212](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L196-L212)
fd 路径的 `_map_nvl_dist_tensor` 分支：把 `shareable` 里的本 rank fd 取出来，经 `_exchange_ipc_fds` 换到全组 fd 字典，按 rank 顺序排成 `int64` 张量交给 `nvl_dist_map(use_fabric=False)`（C++ 侧对每行 fd 调 `cuMemImportFromShareableHandle`），`finally` 里**关闭包括本 rank 在内的所有 fd**——句柄已经导入并映射完成，fd 用完即弃。

#### 4.1.4 代码实践：亲手用 SCM_RIGHTS 传一次 fd

这个实践**不需要 GPU、不需要构建 moonep**，只用 Python 标准库，在任何机器上都能完成。

1. **实践目标**：用最小可运行例子验证 2.1 节的论断——SCM_RIGHTS 传的不是编号，而是内核复制出来的新引用。

2. **操作步骤**：创建 `scm_rights_demo.py`（示例代码，独立于 MoonEP）：

   ```python
   # scm_rights_demo.py —— 用 SCM_RIGHTS 在两个进程间传 fd（示例代码）
   import multiprocessing as mp
   import os, socket, struct, tempfile

   def child(sock_path):
       s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
       s.bind(sock_path + ".c")          # 子进程收件箱
       ready.put(None)                    # 告诉父进程：可以发了
       msg, anc, _f, _a = s.recvmsg(16, socket.CMSG_SPACE(4))
       for lvl, typ, data in anc:
           if lvl == socket.SOL_SOCKET and typ == socket.SCM_RIGHTS:
               fd = struct.unpack("<i", data[:4])[0]
               print(f"[child ] 收到 fd={fd}，本进程 pid={os.getpid()}")
               with os.fdopen(fd, "rb") as f:        # 用收到的 fd 读同一个文件
                   print(f"[child ] 经 fd 读到内容: {f.read().decode()!r}")
       s.close()

   if __name__ == "__main__":
       d = tempfile.mkdtemp(prefix="scm_demo_")
       sock_path = os.path.join(d, "c")
       # 父进程写一个临时文件，拿到 fd
       path = os.path.join(d, "secret.txt")
       with open(path, "w") as f:
           f.write("hello from parent")
       pf = os.open(path, os.O_RDONLY)
       print(f"[parent] 原始 fd={pf}，本进程 pid={os.getpid()}")
       ctx = mp.get_context("fork")
       ready = ctx.Queue()
       p = ctx.Process(target=child, args=(sock_path,)); p.start()
       ready.get()                                  # 等收件箱 bind 完成（barrier 的雏形）
       s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
       s.sendmsg([struct.pack("<i", 0)],
                 [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("<i", pf))],
                 0, sock_path + ".c")
       p.join(); s.close(); os.close(pf)
   ```

3. **需要观察的现象**：子进程打印的 fd 编号与父进程的 `pf` **几乎必然不同**（两个独立进程的 fd 表各自分配编号），但子进程用它仍能读到 `hello from parent`。

4. **预期结果**：验证「fd 编号跨进程无意义、SCM_RIGHTS 复制的是引用」。这正是 `_exchange_ipc_fds` 里 `fds[src_rank]` 存的值与发送方 `local_fd` 不同的原因，也是 docstring 要求「caller 必须关闭收到的 fd」的原因——它们是接收进程 fd 表里的新条目，不关就泄漏。（待本地验证：fd 的具体编号因系统而异。）

5. **延伸对照**：把 `sendmsg` 的 ancillary 部分删掉、只发 `payload`，再运行——子进程会在 `for ... in anc` 循环里什么也收不到，对应 [moonep/buffer.py:L163-L164](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L163-L164) 抛出 `"received IPC message without an fd"` 的分支。

#### 4.1.5 小练习与答案

**练习 1**：能否把 fd 编号装进张量，用 `dist.broadcast` 发给其他 rank？为什么？

**答案**：不能。fd 编号只是发送进程 fd 表的下标，广播出去只是一个小整数，接收进程里这个编号指向的对象与 VMM 分配毫无关系（甚至可能无效）。传 fd 必须让内核参与复制引用，即 SCM_RIGHTS；这正是 `_exchange_ipc_fds` 存在的理由。

**练习 2**：[moonep/buffer.py:L144](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L144) 与 [L169](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L169) 各有一个 `dist.barrier`，分别保证什么？

**答案**：第一个保证「所有收件箱 socket 都已 bind」，否则向未 bind 的路径 sendmsg 会失败；第二个保证「所有 rank 都已完成 recvmsg、内核已把 fd 复制进各自 fd 表」，此后发送方关闭原始 fd、rank 0 删除临时目录都是安全的——不会有任何消息还悬在路上、也不再有接收方依赖发送方的 fd。

**练习 3**：为什么选 `SOCK_DGRAM` 而不是 `SOCK_STREAM`？

**答案**：数据报每条消息有边界，一次 `recvmsg` 恰好对应一次 `sendmsg`，正文（4 字节 rank 标识）与 cmsg（fd）原子地绑定在一起；流式 socket 的字节流没有消息边界，若分多次收，正文与辅助数据可能错位，代码会复杂得多。

### 4.2 fabric 通道：64 字节句柄的字节分发

#### 4.2.1 概念说明

fabric handle 与 fd 的根本差异在 2.3 节已经给出：**它是不透明字节串，不是内核引用**。这带来三个直接后果：

1. **可以走任何传输**。MoonEP 直接复用 `torch.distributed` 的集合通信（NCCL）来收集全组的句柄——代码量从 60 行（`_exchange_ipc_fds`）降到 10 行（`_all_gather_shareables`）。
2. **跨节点可用**。fd 只在同一内核内有效，多节点训练（每节点独立操作系统）时 unix socket 根本连不到对端；fabric handle 由 NVLink fabric 全局命名，`dist` 的网络（如 TCP/IB）把它当普通字节运过去即可。这是多节点 NVLink fabric 支持（提交 `39859eb`）依赖的基础。
3. **无需清理**。fd 是有限资源、必须 close；fabric handle 只是 64 字节拷贝，用完丢弃即可，没有「泄漏」概念。对比 fd 路径里小心翼翼的 `os.close` 时机（[moonep/buffer.py:L196-L212](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L196-L212) 的三处 close），fabric 路径 [L185-L194](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L185-L194) 一处 close 都没有。

代价是**硬件前提更强**：节点必须接入同一 NVLink fabric 域、驱动与 fabric manager 就绪（见 4.3 的探测）。

#### 4.2.2 核心流程

fabric 模式下「全员互发」的时序，与 fd 模式对比：

```
每个 rank（无角色差异，无 socket，无目录）:
  nvl_dist_alloc(use_fabric=True)
      → shareable = uint8[64]（CPU 张量，内容是 CUmemFabricHandle.data）
  shareable.cuda()                              # 搬上 GPU
  all_gather_into_tensor([R,64])                # NCCL 集合通信一次收齐
  gathered.cpu()                                # 搬回 CPU（nvl_dist_map 要求 CPU 张量）
  nvl_dist_map(shareables=[R,64], use_fabric=True)
      → 第 i 行 memcpy 回 CUmemFabricHandle → cuMemImportFromShareableHandle
      → cuMemMap → cuMemRelease（导入句柄用完即弃，映射持有引用）
```

除 `nvl_dist_map` 内部循环外**没有任何显式 barrier**——集合通信 `all_gather_into_tensor` 本身就是全组同步点。

#### 4.2.3 源码精读

先看 C++ 侧句柄长什么样、如何导出导入：

[csrc/nvl_shared_buffer.cuh:L152-L153](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/nvl_shared_buffer.cuh#L152-L153)
`kFabricHandleBytes = sizeof(CUmemFabricHandle::data)`，即 64；它经 [csrc/bindings.cu:L14](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/bindings.cu#L14) 导出为 Python 常量 `FABRIC_HANDLE_BYTES`，`buffer.py` 开缓冲区时直接引用它，两端永远一致。

[csrc/nvl_shared_buffer.cuh:L158-L175](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/nvl_shared_buffer.cuh#L158-L175)
`nvl_export_shareable` 按模式导出：fabric 分支 `cuMemExportToShareableHandle` 得到 `CUmemFabricHandle`，64 字节原样 `memcpy` 进 `uint8[64]` 张量；fd 分支导出 int fd，存进 `int64` 标量张量——**同一个函数返回两种「shareable」，Python 侧的传输方式由它决定**。

[csrc/nvl_shared_buffer.cuh:L198-L217](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/nvl_shared_buffer.cuh#L198-L217)
`nvl_import_shareable` 是镜像：fabric 分支从句柄张量第 `index` 行 `memcpy` 回 `CUmemFabricHandle` 再导入；fd 分支把 `int64` 转回 `intptr_t` 当 fd 导入。注意 [L180-L182](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/nvl_shared_buffer.cuh#L180-L182) 的 `nvl_check_handles` 断言句柄必须是 **CPU 连续张量**——这解释了下面 Python 侧 `.cpu()` 的必要性。

Python 侧的两个分发助手：

[moonep/buffer.py:L60-L70](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L60-L70)
`_all_gather_shareables`：把本 rank `uint8[64]` 句柄 view 成 `[1,64]` 搬上 GPU，`all_gather_into_tensor` 收成 `[world_size,64]`，再 `.cpu()` 交给 `nvl_dist_map`。fabric handle 在这里是**纯数据**——NCCL 不需要理解它，运字节即可。

[moonep/buffer.py:L73-L86](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L73-L86)
`_broadcast_shareable`：单 owner 场景（见下）用 `dist.broadcast` 把 owner 的 64 字节发给全组；非 owner 的 rank 传 `local_handle=None`，只需准备一个空缓冲参与同一集合通信。

两条通道在 `_map_nvl_dist_tensor` 的分岔（fd 分支已在 4.1.3 精读）：

[moonep/buffer.py:L175-L194](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L175-L194)
`use_fabric=True` 时一行收集、一行映射：`_all_gather_shareables` + `nvl_dist_map(use_fabric=True)`，没有任何 fd 生命周期要管理。

同样的「两通道」模式在另外两个调用点被复用（读一遍即可，细节属于 u2-l4）：

- [moonep/buffer.py:L311-L326](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L311-L326)：组播对象的句柄分发——fabric 用 `_broadcast_shareable`（只有 root 有句柄），fd 用 `_exchange_ipc_fds(local_fd, [0], ...)`（sender 只有 rank 0）。
- [moonep/buffer.py:L363-L384](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L363-L384)：单 owner 缓冲（`create_nvl_single_owner_tensor`）——fabric 用 `_broadcast_shareable(shareable, owner_rank, ...)`，fd 用 `_exchange_ipc_fds(local_fd, [owner_rank], ...)`。两处都体现了 `sender_ranks` 参数「子集发送」的设计意图。

#### 4.2.4 代码实践：验证「字节就能过集合通信」

1. **实践目标**：亲手确认 fabric handle 的传输本质是「按字节搬运的集合通信」，并复刻 `_all_gather_shareables` 的形状与内容逻辑。

2. **操作步骤**：写 `gather_bytes_demo.py`（示例代码）。注意原函数 `.cuda()` 要求 GPU + NCCL，这里用 **gloo 后端在纯 CPU** 上复刻同一逻辑，单机即可运行：

   ```python
   # gather_bytes_demo.py —— 用 gloo 复刻 _all_gather_shareables（示例代码）
   # 运行：torchrun --nproc_per_node=2 gather_bytes_demo.py
   import os, torch, torch.distributed as dist

   FABRIC_HANDLE_BYTES = 64          # 对应 _C.FABRIC_HANDLE_BYTES

   def all_gather_shareables_cpu(local_handle, group=None):
       world_size = dist.get_world_size(group=group)
       gathered = torch.empty(world_size, FABRIC_HANDLE_BYTES, dtype=torch.uint8)
       dist.all_gather_into_tensor(gathered, local_handle.view(1, FABRIC_HANDLE_BYTES), group=group)
       return gathered                 # CPU 版无需 .cpu() 往返；原函数在 cuda 上 gather 后回 CPU

   if __name__ == "__main__":
       dist.init_process_group("gloo")             # CPU 后端，模拟 NCCL 的字节搬运
       rank = dist.get_rank(); world = dist.get_world_size()
       torch.manual_seed(1234 + rank)              # 每个 rank 造一个不同的"伪句柄"
       local = torch.randint(0, 256, (FABRIC_HANDLE_BYTES,), dtype=torch.uint8)
       out = all_gather_shareables_cpu(local)
       print(f"rank{rank}: gathered shape={tuple(out.shape)}, "
             f"row{rank} matches local={torch.equal(out[rank], local)}, "
             f"rows differ={not torch.equal(out[0], out[1])}")
       dist.destroy_process_group()
   ```

3. **需要观察的现象**：每个 rank 打印 `gathered shape=(2, 64)`；`row{rank} matches local=True`（自己那行原样回来）；两行内容不同（对方的句柄确实到了）。

4. **预期结果**：证明 64 字节句柄经过集合通信后逐字节无损。与 fd 对照——你可以试着把 4.1.4 里的 fd 编号塞进这个 `local` 张量发给对方，对方拿到的只是一个无意义的整数，无法 `os.fdopen` 它。这就是「fabric 走 dist、fd 走 SCM_RIGHTS」的实验依据。

#### 4.2.5 小练习与答案

**练习 1**：fabric 路径为什么一处 `os.close` 都没有，而 fd 路径有三处 close？

**答案**：fabric handle 是值语义的 64 字节拷贝，Python 张量被 GC 即回收，没有内核资源挂账；fd 是打开文件描述的引用，发送方的原始 fd（[L199](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L199)）与接收方收到的每个 fd（[L210-L212](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L210-L212)）都必须显式 close，否则进程 fd 表泄漏。

**练习 2**：`_broadcast_shareable` 里非 owner 的 rank 传 `local_handle=None` 也要参加 `dist.broadcast`。如果非 owner 直接跳过这次调用会怎样？

**答案**：集合通信要求组内所有 rank 都调用同一集合原语、在同一个调用序上汇合。任何一个 rank 缺席，其余 rank 的 `broadcast` 会永远等不到对端——挂死或超时。所以即使没有内容可发，也必须带着空缓冲参加。

**练习 3**：`_all_gather_shareables` 为什么最后要 `.cpu()`？

**答案**：`nvl_dist_map` 内部的 `nvl_check_handles` 断言句柄张量必须是 CPU 连续张量（[csrc/nvl_shared_buffer.cuh:L180-L182](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/nvl_shared_buffer.cuh#L180-L182)）；而 gather 走 NCCL 又要求张量在 GPU 上。于是路径是 CPU→GPU→gather→CPU。

### 4.3 模式选择与探测：`_use_fabric_for_group`

#### 4.3.1 概念说明

两条通道各有所长，谁来决定用哪条？环境变量 `MOONEP_MEM_HANDLE_TYPE`（该变量目前只在源码注释中说明，README 未提及）：

| 取值 | 语义 | 失败行为 |
| --- | --- | --- |
| `fd` | 强制 POSIX fd 通道 | 多节点下无法工作（fd 到不了其他节点） |
| `fabric` | 强制 fabric 通道 | 组内**任一**设备不支持时直接 `assert` 报错 |
| `auto`（默认） | 全组设备都支持 fabric → 用 fabric；否则回退 fd | 静默回退，不报错 |

两个容易忽略的要点：

1. **决策必须全组一致**。rank 0 用 fabric 导出、rank 1 用 fd 导出，句柄格式对不上，`nvl_dist_map` 会直接失败。所以探测不能各自为政：每个 rank 查询本设备支持性后要**用集合通信汇总**，全组得到同一个 `use_fabric` 布尔值。
2. **「设备支持」不等于「当下可用」**。设备属性说支持 fabric 句柄，但如果 fabric manager 没起、fabric 不通，真正 `cuMemCreate` 时才会失败。提交 `7745ffa`（"Fix fabric support probing"）正是为此把探测从「只查属性」升级为「真实试分配一次再释放」。

另外注意源码注释与实现的一处细微出入：[moonep/buffer.py:L25-L27](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L25-L27) 的注释说 auto 在「组跨节点且设备支持」时选 fabric，但实现是**只要全组支持就选 fabric**，与是否跨节点无关（单机 8 卡若支持 fabric 也走 fabric）。这不影响正确性——单节点上 fabric 通道同样工作——只是注释描述的是设计动机（多节点**必须** fabric），实现选择了更简单的判据。

#### 4.3.2 核心流程

`_use_fabric_for_group(group)` 的决策树（伪代码）：

```text
读 MOONEP_MEM_HANDLE_TYPE（默认 "auto"，统一转小写）
├─ 非法取值 → assert 失败（必须 auto/fabric/fd）
├─ "fd"    → 返回 False（不探测，最快路径）
└─ 探测本设备：supported = nvl_fabric_supported()
    ├─ world_size == 1（未初始化 dist 也按 1）：
    │     unsupported = [] if supported else [0]
    └─ world_size > 1：
          supported 装进 cuda uint8 张量
          all_gather_into_tensor 收齐全组
          unsupported = 值为 0 的 rank 列表
    ├─ "fabric" → assert unsupported 为空，返回 True
    └─ "auto"   → 返回 (unsupported 为空)
```

C++ 侧 `nvl_fabric_supported()` 的三级探测：

```text
1. cuDeviceGetAttribute(CU_DEVICE_ATTRIBUTE_HANDLE_TYPE_FABRIC_SUPPORTED)
   查询失败（老驱动不认识该属性）或值为 0 → False
2. 用 CU_MEM_HANDLE_TYPE_FABRIC 查推荐分配粒度
   失败 → False
3. 按该粒度真实 cuMemCreate 一次再 cuMemRelease
   失败 → False；成功 → True
```

#### 4.3.3 源码精读

[moonep/buffer.py:L25-L28](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L25-L28)
常量定义与模式注释：auto 是默认值，fd 显式短路，fabric 强制。这是全库唯一读取 `MOONEP_MEM_HANDLE_TYPE` 的位置。

[moonep/buffer.py:L31-L37](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L31-L37)
读环境变量并 `assert` 合法取值；`mode == "fd"` 直接返回 False——注意这里**不做任何探测**，`nvl_fabric_supported()` 根本不会被调用。

[moonep/buffer.py:L39-L48](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L39-L48)
探测的收集阶段：`nvl_fabric_supported()` 查本设备；`world_size` 取自该 group（dist 未初始化时按 1，方便单进程使用）。多 rank 时把 0/1 标志装进 **cuda 上的 uint8 张量**做 `all_gather_into_tensor`（走 NCCL，因此 EP 组需要有 GPU 的后端），`(gathered == 0).nonzero()` 得到不支持的 rank 列表 `unsupported`；单 rank 时直接 `[0] if not supported else []`，把「自己」当作组内 rank 0 处理，复用同一条判断逻辑。

[moonep/buffer.py:L50-L57](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L50-L57)
分支收口：`fabric` 模式要求 `unsupported` 为空，否则 assert 报错并列出问题 rank；`auto` 模式返回 `not unsupported`——有一个 rank 不支持就整体回退 fd。回退之所以「免费」，是因为 u2-l2 讲过：padding 用的是两类句柄粒度的最大值，fd/fabric 谁被选中，分配大小都已对齐。

[csrc/nvl_shared_buffer.cuh:L41-L66](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/nvl_shared_buffer.cuh#L41-L66)
C++ 侧的探测原语。L47-L50 查设备属性，注释点名「老驱动不认识该属性」的坑；L52-L65 是 `7745ffa` 修复补上的部分：构造一个带 `CU_MEM_HANDLE_TYPE_FABRIC` 的分配属性，先查粒度、再**真实分配一次**并立刻释放。只查属性是不够的——属性只说明硬件/驱动理论上支持，fabric manager 未就绪等环境问题只有在真正 `cuMemCreate` 时才暴露。

最后看它在分配流程中的位置（上一讲已画过全景，这里只标出本讲的两个点）：

[moonep/buffer.py:L217-L241](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L217-L241)
`create_nvl_dist_tensor` 第一步就是 `_use_fabric_for_group(group)`，随后 `nvl_dist_alloc(use_fabric=use_fabric)`、`_map_nvl_dist_tensor(..., use_fabric, ...)`——决策一次、贯穿始终，保证全组每一步的句柄类型一致。

#### 4.3.4 代码实践：枚举 MOONEP_MEM_HANDLE_TYPE 的全部分支

这个实践**不需要 GPU 与 `moonep._C`**：把 `_use_fabric_for_group` 的决策逻辑原样复刻，用桩替掉 CUDA 探测与 dist 集合通信，离线跑出完整决策表。

1. **实践目标**：对 `(mode, 各 rank 支持性)` 的每种组合，预测并验证 `_use_fabric_for_group` 的返回值或报错。

2. **操作步骤**：创建 `handle_mode_matrix.py`（示例代码，逻辑逐行对照 buffer.py L31-L57）：

   ```python
   # handle_mode_matrix.py —— 复刻 _use_fabric_for_group 的决策矩阵（示例代码）
   import itertools, os

   class FakeDist:                       # 桩：模拟 dist 的 world_size 与 all_gather
       def __init__(self, supported_flags): self.f = supported_flags
       def get_world_size(self, group=None): return len(self.f)
       def all_gather_into_tensor(self, gathered, local, group=None):
           gathered.copy_(self.f)         # 直接把全组标志写进 gathered

   def use_fabric_for_group(fake_supported, group=None, dist_stub=None, fake_rank_flags=None):
       """逐行对照 moonep/buffer.py:_use_fabric_for_group。"""
       mode = os.environ.get("MOONEP_MEM_HANDLE_TYPE", "auto").lower()
       assert mode in ("auto", "fabric", "fd"), (
           f"MOONEP_MEM_HANDLE_TYPE must be one of auto/fabric/fd, got {mode!r}")
       if mode == "fd":
           return False
       supported = fake_supported()
       world_size = dist_stub.get_world_size(group=group)
       if world_size == 1:
           unsupported = [] if supported else [0]
       else:
           gathered = fake_rank_flags()               # 模拟 all_gather 的结果
           unsupported = (gathered == 0).nonzero().flatten().tolist()
       if mode == "fabric":
           assert not unsupported, (
               f"MOONEP_MEM_HANDLE_TYPE=fabric, but fabric memory handles are "
               f"unsupported on group ranks {unsupported}.")
           return True
       return not unsupported

   import torch
   cases = []                                          # (world, 本rank支持, 全组标志)
   for world in (1, 4):
       for flags in itertools.product([1, 0], repeat=world):
           cases.append((world, bool(flags[0]), torch.tensor(flags, dtype=torch.uint8)))
   for mode in ("auto", "fabric", "fd", "FABRIC"):     # FABRIC 验证 lower() 归一化
       os.environ["MOONEP_MEM_HANDLE_TYPE"] = mode
       for world, sup, flags in cases:
           try:
               r = use_fabric_for_group(lambda s=sup: s, dist_stub=FakeDist(flags.tolist()),
                                        fake_rank_flags=lambda f=flags: f.clone())
               print(f"mode={mode:7s} world={world} flags={flags.tolist()} -> {r}")
           except AssertionError as e:
               print(f"mode={mode:7s} world={world} flags={flags.tolist()} -> AssertionError: {str(e)[:60]}...")
   os.environ["MOONEP_MEM_HANDLE_TYPE"] = "websocket"  # 非法值
   try:
       use_fabric_for_group(lambda: True, dist_stub=FakeDist([1]),
                            fake_rank_flags=lambda: torch.ones(1, dtype=torch.uint8))
   except AssertionError as e:
       print(f"mode=websocket -> AssertionError: {e}")
   ```

3. **需要观察的现象**：`mode=fd` 的所有行都返回 False（flags 根本不影响）；`mode=fabric` 在 flags 含 0 的行抛 AssertionError 且报错信息列出不支持的具体 rank；`mode=auto` 在 flags 全 1 时 True、含 0 时 False；`mode=FABRIC` 与 `fabric` 行为一致（`.lower()` 生效）；`websocket` 触发取值断言。

4. **预期结果**：输出是一张覆盖全部分支的决策表，与源码四个分支一一对应。重点体会两点：**决策粒度是整组而非单 rank**（auto 下一个 rank 不支持、全组回退）；**fabric 是强制契约而 auto 是尽力而为**。（待本地验证：具体打印顺序因用例枚举顺序而异。）

5. **对照真机**（可选，需已构建 `moonep._C` 且有 GPU）：`python -c "import moonep._C as C; print(C.nvl_fabric_supported())"` 直接看本机探测结果；再用 `MOONEP_MEM_HANDLE_TYPE=fabric` 在不支持 fabric 的机器上构造 `Buffer`，预期在探测处就报错。

#### 4.3.5 小练习与答案

**练习 1**：`mode="fabric"` 时为什么不学 auto 那样回退 fd，而是直接 assert 崩掉？

**答案**：`fabric` 是用户显式声明的强制契约——用户多半是在多节点场景下写的，此时 fd 通道根本不可能工作，静默回退只会把问题推迟到更难排查的地方（fd 传不过节点，`nvl_dist_map` 或后续通信莫名失败）。fail fast 报出「哪些 rank 不支持」反而是唯一有用的行为。

**练习 2**：为什么 `nvl_fabric_supported` 不停在设备属性查询（L47-L50），还要真实 `cuMemCreate` 一次（L52-L65）？

**答案**：设备属性只反映驱动/硬件的能力，不反映 fabric 环境是否就绪（fabric manager 是否运行、fabric 是否连通）。提交 `7745ffa` 修复前的实现只查属性，会在属性说支持、实际分配失败的机器上误判——auto 模式选了 fabric 后在 `nvl_dist_alloc` 里崩掉。真实试分配一次再立即释放，把「能力」和「当下可用」合并成一次验证，代价只是一次最小粒度的分配。

**练习 3**：把探测放在 Python（组内 all_gather 协商）而不是塞进 C++ 一次性完成，好处是什么？

**答案**：C++ 扩展只提供**本设备**的无通信原语（`nvl_fabric_supported` 查单卡）；而决策需要**全组一致**，天然要集合通信——这正是 `torch.distributed` 已有的能力。分层之后，C++ 保持无状态、可单测，跨 rank 协商留在 Python 侧与 group 语义（子组、global rank 换算）打交道，职责清晰。

## 5. 综合实践

把本讲三个模块串起来：写一个 `handle_share_trace.py`，**不依赖 GPU**，输出三种产物：

1. **fd 模式时序图**：按 4.1.2 的时序，脚本按 rank 循环打印「谁在何时做什么」（mkdtemp → broadcast → bind → barrier#1 → sendmsg×R → recvmsg×R → barrier#2 → rmtree → close 原始 fd → nvl_dist_map → close 全部 fd）。文本形如 `[t=3] rank1 bind(dir/rank_1)`，时间步自定即可。
2. **fabric 模式时序图**：按 4.2.2 打印（alloc → cuda → all_gather → cpu → dist_map 导入/映射/释放），并标注「无 fd、无 socket、无目录」。
3. **决策表**：内嵌 4.3.4 的复刻函数，跑出 `MOONEP_MEM_HANDLE_TYPE × 各 rank 支持性` 的完整矩阵。

验收标准（自检）：

- 两张时序图中，fd 模式恰有 2 个 barrier、fabric 模式 0 个显式 barrier（all_gather 自带同步）；
- fd 模式的 `close` 出现在 barrier#2 **之后**（发送方原始 fd）与 `nvl_dist_map` 之后（接收方 fd），fabric 模式无任何 close；
- 决策表覆盖 `auto/fabric/fd` × {全支持, 部分支持, 全不支持} × {单 rank, 多 rank}。

若手头有多 GPU 机器，可进一步（待本地验证）：分别设 `MOONEP_MEM_HANDLE_TYPE=fd` 与 `auto` 跑一次 `tests/test_e2e.py`，两种模式都应通过——这验证了句柄通道对上层完全透明。

## 6. 本讲小结

- `nvl_dist_alloc` 导出的 shareable 句柄有两种形态：**POSIX fd**（`int64` 标量，内核引用，仅限同节点）与 **fabric handle**（`uint8[64]` 不透明字节串，可跨节点、可序列化）。
- fd 通道 `_exchange_ipc_fds` 用 unix datagram socket + **SCM_RIGHTS** 让内核把 fd 复制进每个接收进程；正文 4 字节携带来源 rank；两个 barrier 分别保证「收件箱就绪后才发送」和「全员收完才关 fd/删目录」。
- fabric 通道只是**字节搬运**：`_all_gather_shareables` 把 64 字节句柄当普通张量走 `all_gather_into_tensor`，无需任何资源清理。
- `_use_fabric_for_group` 读取 `MOONEP_MEM_HANDLE_TYPE=auto/fabric/fd`，用 cuda 张量 all_gather 汇总全组支持性：fd 直接短路；fabric 要求全组支持否则报错；auto 全组支持则 fabric、否则静默回退 fd（padding 用两类粒度最大值，回退无代价）。
- `nvl_fabric_supported` 的探测是**三级**的：设备属性 → fabric 粒度查询 → 真实试分配再释放（提交 `7745ffa` 加固），确保「属性说支持」等于「现在真能用」。
- 同一套「两通道 + 子集发送者」设计被三个调用点复用：对称内存（全员互发）、组播对象（rank 0 发）、单 owner 缓冲（owner 发）。

## 7. 下一步学习建议

句柄送到对端之后，`nvl_dist_map` 把全组物理内存拼成连续 VA 的工作在 u2-l2 已经讲完。下一讲 **u2-l4「组播视图与 meta_buf 共享布局」**处理这条链路的最后一块拼图：`meta_buf` 之上叠加的 **NVSwitch 组播（multimem）视图**——`nvl_multicast_create/import/add_device/bind_map` 四件套如何复用本讲的两条句柄通道分发组播对象（[moonep/buffer.py:L277-L335](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L277-L335)），以及 `meta_buf` 内各共享区段的偏移布局。学完 u2-l4，u2 单元的内存地基就完整了，届时可以带着「一次写、全 rank 可见」的组播语义进入 u3 规划器。

若想先巩固本讲，建议重读 `_exchange_ipc_fds` 的 docstring（[moonep/buffer.py:L120-L129](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L120-L129)）并默写 fd 的所有权流转：谁创建、谁复制、谁导入、谁关闭、何时关闭——这是本讲最值得刻进肌肉记忆的契约。
