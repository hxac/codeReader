# Python TransferEngine API 详解

> 适用阶段：intermediate（建议先完成 `u2-l1`，并看过 `u1-l4` 的 Python 速览）
> 本讲聚焦 `TransferEnginePy` 通过 pybind11 暴露给 Python 的那一层 API：同步/异步、批量、CUDA 流触发的传输，以及托管内存（managed buffer）、内存注册、通知、探针等辅助接口。读完本讲，你会清楚地知道 `from mooncake.engine import TransferEngine` 之后到底能调哪些方法、它们在 C++ 侧如何实现、什么时候该用哪一个。

---

## 1. 本讲目标

学完本讲，你应当能够：

1. 说出 `TransferEnginePy` 暴露的四大类传输 API：**同步单条 / 同步批量 / 异步批量 / CUDA 流触发**，并知道每类在 C++ 层对应哪个函数。
2. 解释**托管内存（managed buffer）**：它为什么是「已注册的」，内部用的是什么样的 buddy 分配器，何时会退化成一次性大块分配。
3. 写出一次**异步批量传输**的完整流程：`batch_transfer_async_*` 返回 `batch_id` → 轮询 `get_batch_transfer_status` → 自动释放批次。
4. 理解 `transfer_*_on_cuda` 系列如何借助 `cudaLaunchHostFunc` 把传输「挂」到 CUDA 流上、实现和前序 kernel 的同步。
5. 区分「失败哨兵」：同步接口返回 `int`（0 成功 / -1 失败），异步接口返回 `batch_id`（0 表示失败）。
6. 知道 `register_memory` / `get_first_buffer_address` / `get_notifies` / `send_probe` 这些辅助接口各自的用途。

---

## 2. 前置知识

在进入源码前，先建立几个直觉。

### 2.1 C++ 门面与 Python 绑定的关系

在 `u2-l1` 里我们见过 C++ 侧的「门面」`TransferEngine`，它的标准调用顺序是：

```
init → installTransport → registerLocalMemory → openSegment
     → allocateBatchID → submitTransfer → getTransferStatus → freeBatchID
```

这一串对 Python 用户来说太底层、太啰嗦了。于是 Mooncake 在外面再包了一层 **`TransferEnginePy`**（适配器/Adaptor），它把上面这串步骤封装成几个「一句话就能用」的 Python 方法：

- 想做一次同步读？直接 `engine.transfer_sync_read(...)`，它会自己 `allocateBatchID` + `submitTransfer` + 轮询 + `freeBatchID`。
- 想要异步？`batch_id = engine.batch_transfer_async_write(...)` 立刻返回，你再 `engine.get_batch_transfer_status([batch_id])` 轮询。

> 一句话：`TransferEnginePy` 是 `TransferEngine` 的「便利封装」，让 Python 用户不必手写 batch 生命周期管理。

### 2.2 两种返回值约定（重要）

读源码时一定要分清两种返回值风格，否则会把「成功」和「失败」搞反：

- **同步接口**返回 `int`：`0` 表示成功，`-1` 表示失败（C 风格）。
- **异步接口**返回 `batch_id`（一个 `uint64_t`）：`0` 是**失败哨兵**。这一点在源码注释里写得很清楚，因为 `batch_id_t` 是无符号的，如果用 `-1` 会在 Python 里显示成一个巨大的数 `2^64 - 1`，所以故意用 `0` 表示失败。详见 [transfer_engine_py.cpp:609-611](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L609-L611)。

### 2.3 GIL 与内存分配器

- 所有可能阻塞的传输方法（`transfer_sync*`、`batch_transfer_*`、`get_batch_transfer_status`、`*_on_cuda` 等）开头都有一句 `pybind11::gil_scoped_release release;`，意思是「我要长时间干 CPU/IO 活，先放开 Python 全局锁，别卡住别的 Python 线程」。
- 托管内存用哪个分配器，取决于 `initialize` 时传入的 `protocol`（如 `nvlink` 用 MNNVL 的 pinned memory 分配器，否则用普通 `malloc`）。这一点直接决定了 `allocate_managed_buffer` 拿到的内存是否适合某种硬件传输。

### 2.4 三个核心类型回顾

来自 `transport.h`（详见 `u2-l1`），本讲会反复用到：

- `TransferRequest`：一次读/写的描述 —— `opcode`(READ/WRITE)、`source`(本地地址)、`target_id`(目标段)、`target_offset`(目标偏移)、`length`。见 [transport.h:60-71](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/transport/transport.h#L60-L71)。
- `TransferStatusEnum`：传输状态机 `{WAITING, PENDING, INVALID, CANCELED, COMPLETED, TIMEOUT, FAILED}`。见 [transport.h:73-81](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/transport/transport.h#L73-L81)。
- `BatchDesc`：批次的内部描述，含 `task_list`、`start_timestamp` 等，`BatchID` 实际上就是指向它的指针被强转成的整数。见 [transport.h:328-349](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/transport/transport.h#L328-L349)。

---

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `mooncake-integration/transfer_engine/transfer_engine_py.cpp` | **pybind 绑定 + 适配器实现** | 所有 Python 方法的 C++ 实现、`PYBIND11_MODULE` 注册表 |
| `mooncake-integration/transfer_engine/transfer_engine_py.h` | **适配器头文件** | `TransferEnginePy` 类声明、buddy 分配器常量、`TransferOpcode`/`TransferNotify` |
| `mooncake-transfer-engine/include/transport/transport.h` | **核心类型** | `TransferRequest`/`TransferStatusEnum`/`TransferStatus`/`BatchDesc`/`BatchID` |
| `mooncake-transfer-engine/include/transfer_engine.h` | **门面 API** | `submitTransfer`/`allocateBatchID`/`getBatchTransferStatus` 等签名 |
| `mooncake-transfer-engine/include/memory_location.h` | **内存位置常量** | `kWildcardLocation = "*"` |
| `mooncake-transfer-engine/example/kvcache_prefix_bench.py` | **官方示例** | 真实工程里如何用 `TransferEngine` 做跨节点 KV cache 拉取与吞吐基准 |

> 下文所有永久链接均指向当前 HEAD `1f7f71a18a9dc48e9901d8293c5c3625ba166939`。

---

## 4. 核心概念与源码讲解

### 4.1 TransferEngine Python 绑定全貌

#### 4.1.1 概念说明

Python 侧的 `from mooncake.engine import TransferEngine`，背后对应的是一个名为 `engine` 的 pybind11 模块（模块名由 `PYBIND11_MODULE(engine, m)` 决定）。这个模块里：

- **能力标志（capability flags）**：`SUPPORT_EFA` / `SUPPORT_HIP` / `SUPPORT_MNNVL` / `SUPPORT_INTRA_NVLINK` / `SUPPORT_CUDA`，是编译期 `#ifdef` 出来的布尔值，告诉你当前这颗 wheel 支持哪些硬件协议。
- **两个小类型**：`TransferOpcode`（`Read=0`/`Write=1`）和 `TransferNotify`（带 `name`、`msg` 两个字段）。
- **主类**：`TransferEngine`（对应 C++ 的 `TransferEnginePy`），挂着一堆方法。

理解这一层之后，你就能解释一个常见困惑：「为什么同样的 `TransferEngine` 类，在有的机器上能用 `transfer_write_on_cuda`、有的机器上报 `AttributeError`？」——因为那些 `*_on_cuda` 方法包在 `#ifdef USE_CUDA` 里，编译时没开 CUDA 就根本不会注册到模块上。见 [transfer_engine_py.cpp:1180-1200](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L1180-L1200)。

#### 4.1.2 核心流程：初始化一条引擎

`initialize(local_hostname, metadata_server, protocol, device_name)` 的流程是：

1. 根据 `protocol` 选内存分配器（`initMemoryAllocator`）。
2. 解析 `metadata_server` 连接串（支持 `etcd://...`、`P2PHANDSHAKE` 等形式）。
3. 委托给 `initializeExt`：创建底层 `TransferEngine`（非 EFA 构建下 `auto_discover=true`，会自动发现并装 RDMA；EFA 构建则关掉自动发现、手动装 TCP/EFA transport）。
4. 调 `engine_->init(...)` 连上元数据服务。
5. 初始化 buddy 空闲链表 `free_list_`。

#### 4.1.3 源码精读

**能力标志 + 主类型注册**（节选）：

[transfer_engine_py.cpp:1085-1130](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L1085-L1130) — 注册 `SUPPORT_*` 标志、`TransferOpcode` 枚举、`TransferNotify` 结构体，以及 `TransferEngine` 类的 `initialize`/`initialize_ext`/`get_rpc_port` 等入口。

`initializeExt` 里有两处关键约束值得记下：

- [transfer_engine_py.cpp:184-188](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L184-L188) —— 显式拒绝 `xgmi` 协议，提示用 `hip` 代替。
- [transfer_engine_py.cpp:196-250](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L196-L250) —— EFA 构建下手动安装 transport（RDMA QP 在 EFA 设备上创建会失败，所以要绕开自动发现）。

**内存分配器选择** `initMemoryAllocator`：

[transfer_engine_py.cpp:53-102](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L53-L102) —— `nvlink`→`NvlinkTransport::allocatePinnedLocalMemory`，`hip`→`HipTransport::...`，`nvlink_intra`→`IntraNodeNvlinkTransport::...`，其余走普通 `malloc/free`。这决定了托管内存的「物理属性」。

**超时设置**（构造函数）：

[transfer_engine_py.cpp:104-112](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L104-L112) —— 读环境变量 `MC_TRANSFER_TIMEOUT`（最小 5 秒），默认 30 秒，存为 `transfer_timeout_nsec_`。后续所有同步传输的超时上限都基于它。

#### 4.1.4 代码实践

**实践目标**：确认当前安装的 wheel 支持哪些协议，并理解初始化的输入输出。

```python
# probe_caps.py  —— 示例代码
from mooncake import engine  # 直接 import 子模块查看能力标志

print("SUPPORT_CUDA :", engine.SUPPORT_CUDA)
print("SUPPORT_MNNVL:", engine.SUPPORT_MNNVL)
print("SUPPORT_EFA  :", engine.SUPPORT_EFA)

from mooncake.engine import TransferEngine
e = TransferEngine()
# 单机 P2P + TCP，device_name 留空
ret = e.initialize("127.0.0.1:0", "P2PHANDSHAKE", "tcp", "")
print("initialize ret =", ret)
print("rpc port =", e.get_rpc_port())
```

操作步骤与观察：

1. 确认 `SUPPORT_*` 输出与你的编译选项一致（如普通 CPU 构建应为 `SUPPORT_CUDA=False`）。
2. `initialize` 返回 `0` 表示成功。
3. 如果在不支持 CUDA 的构建上调用 `e.transfer_write_on_cuda`，会抛 `AttributeError` —— 这正是 `#ifdef USE_CUDA` 的体现。

> 是否能跑通取决于环境是否装好 `mooncake` 包；若无硬件/包，请按「源码阅读型实践」理解输出含义，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `transfer_write_on_cuda` 在某些 wheel 上不存在？
**答案**：它包在 `#ifdef USE_CUDA` 内，编译时未开启 `USE_CUDA` 就不会通过 pybind 注册到 `engine` 模块，因此 Python 侧看不到该方法。可通过 `engine.SUPPORT_CUDA` 提前判断。

**练习 2**：`initialize("...", "P2PHANDSHAKE", "nvlink", "")` 与 `"tcp"` 拿到的托管内存有什么本质区别？
**答案**：`initMemoryAllocator` 会把 `nvlink` 映射到 MNNVL 的 pinned memory 分配器（适合 GPU/NVLink 传输），而 `tcp` 走普通 `malloc`。前者是页锁定（pinned）内存，后者不是。

---

### 4.2 托管内存（Managed Buffer）：buddy 分配器

#### 4.2.1 概念说明

`allocate_managed_buffer(length)` 给你一块**已经注册好**的内存（可直接用于传输），你不用自己 `register_memory`。它的好处有两个：

1. **省事**：分配即注册，`free_managed_buffer` 时由引擎统一回收。
2. **快**：内部用 **buddy（伙伴）分配器**从预分配的大块里切分，避免每次传输都去做昂贵的内存注册（RDMA 注册几百 MB 动辄上百毫秒）。

buddy 分配器把空闲内存按 **slab 等级**管理，等级对应一组 2 的幂次大小：

```text
kSlabSizeKB = {8, 16, 32, 64, 128, 256, 512, 1024,
               2*1024, 4*1024, 8*1024, 16*1024,
               32*1024, 64*1024, 128*1024, 256*1024}  // 单位 KB
```

见 [transfer_engine_py.h:36-41](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.h#L36-L41)。也就是说最小 8KB、最大 256MB（`kMaxClassId=15` 对应 256MB）。超过 256MB 的请求会走「大块直通」路径，单独分配+注册。

#### 4.2.2 核心流程

`allocateManagedBuffer(length)` 的逻辑：

1. `findClassId(length)`：找到能容纳 `length` 的**最小** slab 等级；若 `length > 256MB` 返回 `-1`。
2. 若该等级空闲链表为空，递归调用 `doBuddyAllocate`：从更大等级切一块下来对半分。
3. 若 `class_id < 0`（超大请求），直接 `allocateRawBuffer(length)` 并记入 `large_buffer_list_`。
4. 返回缓冲区地址（`uintptr_t`）。

`doBuddyAllocate` 是经典递归：到最高等级 `kMaxClassId` 时，分配一整块 `kDefaultBufferCapacity = 2GB` 的原始内存（`allocateRawBuffer`），切成 256MB 的片塞进最高等级空闲链表；否则向上一级要一块，对半劈成两块塞进当前等级。

`freeManagedBuffer(addr, length)`：注意它需要**同样的 `length`** 才能正确归还 —— 用 `findClassId(length)` 找回等级，把地址 push 回该等级链表；大块则从 `large_buffer_list_` 移除并真正 unregister+free。

#### 4.2.3 源码精读

**buddy 等级查找**：

[transfer_engine_py.cpp:269-274](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L269-L274) —— `findClassId`：从高到低扫，找到第一个 `size > 1024 * kSlabSizeKB[i]` 的等级返回 `i+1`，即最小可容纳等级。

**递归切分**：

[transfer_engine_py.cpp:276-296](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L276-L296) —— `doBuddyAllocate`：到顶分配 2GB 原始块并切成 256MB 片；否则从 `class_id+1` 取一块，对半劈成两块压入当前等级。

**分配入口 / 释放入口**：

[transfer_engine_py.cpp:298-326](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L298-L326) —— `allocateManagedBuffer`（加锁、查等级、必要时切分、返回地址）与 `freeManagedBuffer`（按 `length` 找回等级归还；大块单独 unregister）。注意大块路径会 `insert` 进 `large_buffer_list_`。

**原始块分配（分配+注册原子化）**：

[transfer_engine_py.cpp:258-267](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L258-L267) —— `allocateRawBuffer`：用 `allocateMemory` 分配，再用 `registerLocalMemory(..., kWildcardLocation)` 注册到「任意位置」，注册失败就 free 掉、返回 `nullptr`。这就是托管内存「分配即注册」的来源。

> `kWildcardLocation = "*"` 见 [memory_location.h:41](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/memory_location.h#L41)，表示不限定 NUMA/设备位置。

#### 4.2.4 代码实践

**实践目标**：亲手验证 buddy 分配器对大小请求的分级与回收。

```python
# probe_buddy.py  —— 示例代码
from mooncake.engine import TransferEngine

e = TransferEngine()
e.initialize("127.0.0.1:0", "P2PHANDSHAKE", "tcp", "")

# 小请求：落在最小 8KB 等级（即使你只要 1 字节）
small = e.allocate_managed_buffer(1)
# 中等请求：落在某个 2 的幂等级
mid = e.allocate_managed_buffer(1 * 1024 * 1024)   # 1MB -> 1MB 等级
# 超大请求：超过 256MB，走大块直通
big = e.allocate_managed_buffer(300 * 1024 * 1024) # 300MB

print("small addr:", hex(small))
print("mid addr  :", hex(mid))
print("big addr  :", hex(big))

# 回收时务必传【同样的 length】，否则等级算错
e.free_managed_buffer(small, 1)
e.free_managed_buffer(mid, 1 * 1024 * 1024)
e.free_managed_buffer(big, 300 * 1024 * 1024)
```

观察现象与预期：

1. 三次分配都应返回非 0 地址（`0` 表示失败）。
2. `small` 与 `mid` 很可能落在相邻地址（来自同一块 2GB slab 的不同切片）；`big` 是独立大块。
3. 如果 `free_managed_buffer` 传错 `length`，不会立刻报错，但会污染空闲链表 —— 这正是该接口的「坑」。

> 无硬件时这可在单机 TCP 构建上运行；若包未安装，按源码阅读理解，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：请求 `allocate_managed_buffer(1)` 实际占用多少 slab？
**答案**：`findClassId(1)` 返回 `0`，对应 8KB slab。即使只要 1 字节，也分配一整块 8KB（内部碎片）。buddy 分配器牺牲空间换分配速度。

**练习 2**：为什么 `free_managed_buffer` 必须传 `length`，而普通 `free(ptr)` 不用？
**答案**：buddy 分配器按等级回收，需要 `length` 通过 `findClassId` 算出归还到哪个等级的空闲链表；大块还要据此决定走 `large_buffer_list_` 路径。普通 free 因为底层 malloc 自带元数据记录大小，而这里是自定义池，不带。

---

### 4.3 同步传输 API（transfer_sync_read / write）

#### 4.3.1 概念说明

同步传输 = 「提交后立刻阻塞等待，直到这次传输完成才返回」。这是最简单的用法，适合「我就搬这一次，搬完再说」的场景。`kvcache_prefix_bench.py` 里跨节点拉 KV cache 的基准测试，主体就是反复调用 `transfer_sync_read`。

四个公开方法其实是两两配对的薄封装：

- `transfer_sync_write(target, buffer, peer_buffer_address, length, transport_hint="")`
- `transfer_sync_read(...)`
- 它们都委托给内部的 `transferSync(..., opcode, ...)`，只是 `opcode` 不同。见 [transfer_engine_py.cpp:344-360](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L344-L360)。

参数含义：

- `target_hostname`：对端节点名（即对端 `initialize` 时的 `local_hostname`）。
- `buffer`：**本地**内存地址（`uintptr_t`，可以是 `allocate_managed_buffer` 的返回值，或 `register_memory` 过的地址，或 tensor 的 `data_ptr()`）。
- `peer_buffer_address`：**对端**段内的字节偏移地址（通常由 `get_first_buffer_address` 拿到起点再加偏移）。
- `length`：字节数。
- `transport_hint`：TENT 专用，普通构建留空即可。

#### 4.3.2 核心流程

`transferSync` 的执行过程（[transfer_engine_py.cpp:396-489](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L396-L489)）：

1. `gil_scoped_release` 放开 GIL。
2. 查 `handle_map_` 缓存：若已为该 `target_hostname` 开过段就直接复用 `SegmentHandle`，否则 `openSegment` 并缓存。
3. `max_retry = numContexts() + 1`（遍历所有本地 RNIC 上下文各试一次）。
4. 循环重试：`allocateBatchID(1)` → 构造单个 `TransferRequest` → `submitTransfer`。
5. 内层 `while`：`getTransferStatus(batch_id, 0, status)` 轮询单个任务状态：
   - `COMPLETED` → `freeBatchID`，返回 `0`（成功）。
   - `FAILED` → 释放，跳出重试。
   - `TIMEOUT` → 记日志，跳出。
6. 超时保护：`timeout = transfer_timeout_nsec_ + length`（约按 1GiB/s 估算），超时返回 `-1`。
7. 若 `submitTransfer` 失败且 `CheckSegmentStatus` 也不 ok，会 `closeSegment` + `removeSegmentDesc` + 清缓存，避免持续命中坏段。

#### 4.3.3 源码精读

**段句柄缓存**（所有传输方法共用的模式）：

[transfer_engine_py.cpp:403-416](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L403-L416) —— 加锁查 `handle_map_`，命中复用，未命中 `openSegment` 并写回缓存。这就是「第一次调用慢（要建连接）、后续调用快」的原因。

**构造单请求并提交**：

[transfer_engine_py.cpp:426-446](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L426-L446) —— 每次 `allocateBatchID(1)`，填 `entry.{opcode,length,source,target_id,target_offset,advise_retry_cnt,transport_hint}`，根据是否有 `notify` 选 `submitTransfer` 或 `submitTransferWithNotify`。

**轮询状态机**：

[transfer_engine_py.cpp:461-486](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L461-L486) —— 对 `COMPLETED/FAILED/TIMEOUT` 三态分别处理，并用基于长度的超时上限兜底。

#### 4.3.4 代码实践

**实践目标**：复刻 `kvcache_prefix_bench.py` 里「连接预热 + 同步读」的核心模式，观察首调用与稳态的延迟差。

阅读 [kvcache_prefix_bench.py:284-293](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/example/kvcache_prefix_bench.py#L284-L293) —— 官方示例在正式测前先做 3 次小数据 `transfer_sync_read` 预热连接，正是为了避免 `openSegment`/建链开销污染第一次测量。

操作步骤（两终端，单机 TCP）：

```python
# sync_read_probe.py  —— 示例代码（initiator 侧核心片段）
import time
from mooncake.engine import TransferEngine

e = TransferEngine()
e.initialize("127.0.0.1:12346", "P2PHANDSHAKE", "tcp", "")

recv = e.allocate_managed_buffer(64 * 1024 * 1024)          # 64MB 接收缓冲
remote = e.get_first_buffer_address("127.0.0.1:<target端口>") # 对端池起点

# 预热
for _ in range(3):
    e.transfer_sync_read("127.0.0.1:<target端口>", recv, remote, 1024)

# 计时
t0 = time.perf_counter()
ret = e.transfer_sync_read("127.0.0.1:<target端口>", recv, remote, 64 * 1024 * 1024)
dt = time.perf_counter() - t0
print("ret =", ret, "耗时 %.3f s, 吞吐 %.2f GB/s" % (dt, 0.064 / dt))
```

观察现象：

1. 预热阶段第一次调用会明显慢（建链）。
2. 正式读 `ret` 应为 `0`。
3. 吞吐与机器间带宽相关；单机回环通常很高（GB/s 级）。

> 需要一个 target 侧进程先 `initialize` + 大块 `register_memory` 后挂起（参考 `kvcache_prefix_bench.py` 的 `run_target`）。无对端时标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`transfer_sync_read` 返回 `-1` 一定意味着数据传输出错吗？
**答案**：不一定。它也可能因为段状态异常而主动 `closeSegment` 并清缓存（见 [transfer_engine_py.cpp:447-459](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L447-L459)），或因为超过基于 `length` 的超时上限。需要结合日志判断。

**练习 2**：为什么要 `max_retry = numContexts() + 1`？
**答案**：源码注释（[transfer_engine_py.cpp:418-424](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L418-L424)）说明：一次提交会被派发到与某个本地 RNIC 关联的 worker，若该 RNIC 连不上任何对端 RNIC 就会失败。多试几次能轮换到不同的本地 context，提高成功率（标注为 workaround，未来版本会改）。

---

### 4.4 异步批量传输（batch_transfer_async + get_batch_transfer_status）

#### 4.4.1 概念说明

同步 API 的痛点是「全程阻塞」：传输期间 Python 线程什么都干不了，也无法同时发多个传输。异步批量 API 解决这个问题：

- `batch_transfer_async_write/read(...)` 提交后**立刻返回一个 `batch_id`**，传输在后台进行。
- 你攒多个 `batch_id`，用 `get_batch_transfer_status([id1, id2, ...])` **一次性轮询**，全部完成后该方法返回 `0`，并在内部自动 `freeBatchID`。
- 返回的 `batch_id` 本质是指向 `BatchDesc` 的指针强转成的整数（见 [transport.h:102-104](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/transport/transport.h#L102-L104)），所以异步接口能用它反查 `task_list`、`start_timestamp`。

另外还有一组「单条异步」接口：`transfer_submit_write` 返回 `batch_id`，配合 `transfer_check_status(batch_id)` 轮询（返回 `1`=完成、`-1`=失败、`-2`=超时、`0`=进行中）。

#### 4.4.2 核心流程

`batchTransferAsync`（[transfer_engine_py.cpp:596-660](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L596-L660)）：

1. 放开 GIL；查/建段句柄（失败返回 `0` 哨兵）。
2. 校验三个数组长度一致，否则返回 `0`。
3. 为每个元素构造 `TransferRequest`。
4. `allocateBatchID(batch_size)`，把当前时间戳写进 `batch_desc->start_timestamp`（供后续轮询算超时）。
5. `submitTransfer(batch_id, entries)`；成功就 `break` 返回 `batch_id`，失败 `freeBatchID` 后返回 `0`。
6. **不轮询、不等待** —— 这是与同步版的关键区别。

`getBatchTransferStatus(batch_ids)`（[transfer_engine_py.cpp:662-721](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L662-L721)）：

1. 先遍历每个 `batch_id`，累加其 `task_list` 里所有 slice 的 `length`，得到该批次的 `total_length`，进而算出每批次的超时上限 `total_length + transfer_timeout_nsec_`。
2. 进入 `while (!timeout_table.empty() && !failed_or_timeout)` 循环：
   - 对每个在途批次调 `getBatchTransferStatus(batch_id, status)`。
   - `COMPLETED` → `freeBatchID`，从待办表移除。
   - `FAILED` → 置 `failed_or_timeout`。
   - `TIMEOUT` → 记日志（继续等，直到按时间判定真超时）。
   - 超过该批次时间预算 → 置 `failed_or_timeout`。
3. 若有失败/超时，把仍在途的批次全部 `freeBatchID`，返回 `-1`；否则返回 `0`。

> 设计要点：`getBatchTransferStatus` 会在 COMPLETED 时**自动释放**批次，调用方无需再 `free`；但失败时它也会兜底释放剩余批次，避免泄漏。

#### 4.4.3 源码精读

**异步提交 + 记录起始时间戳**：

[transfer_engine_py.cpp:643-660](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L643-L660) —— 注意 `batch_desc->start_timestamp = start_ts`，这一笔记录是后面 `getBatchTransferStatus` 算超时的依据（`current_ts - start_timestamp > entry.second`）。

**轮询多批次并自动释放**：

[transfer_engine_py.cpp:682-721](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L682-L721) —— 主循环；`remove_ids` 收集已完成批次统一擦除；失败分支统一释放残留批次。

**单条异步的等价物**：

[transfer_engine_py.cpp:723-776](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L723-L776) —— `transferSubmitWrite` + `transferCheckStatus`，状态返回值语义：`1` 完成、`-1` 失败、`-2` 超时、`0` 仍在进行（注意 `TIMEOUT` 时**不** free，让调用方决定是否重试）。

#### 4.4.4 代码实践

**实践目标**：发起一个异步批量写，轮询直到完成，理解 `batch_id` 的生命周期。

```python
# async_probe.py  —— 示例代码（initiator 侧核心片段）
import time
from mooncake.engine import TransferEngine

e = TransferEngine()
e.initialize("127.0.0.1:12346", "P2PHANDSHAKE", "tcp", "")

# 1) 用托管内存准备本地缓冲（已注册）
bufs = [e.allocate_managed_buffer(4 * 1024 * 1024) for _ in range(4)]  # 4 个 4MB
# 2) 取对端地址，构造对端偏移列表
remote = e.get_first_buffer_address("127.0.0.1:<target端口>")
peer_addrs = [remote + i * (4 * 1024 * 1024) for i in range(4)]
lengths = [4 * 1024 * 1024] * 4

# 3) 异步批量写 —— 立刻返回 batch_id
bid = e.batch_transfer_async_write(
    "127.0.0.1:<target端口>", bufs, peer_addrs, lengths)
print("batch_id =", bid)
assert bid != 0, "异步提交失败（0 为失败哨兵）"

# 4) 轮询直到完成（COMPLETED 时内部会自动 freeBatchID）
t0 = time.perf_counter()
while True:
    ret = e.get_batch_transfer_status([bid])
    if ret == 0:
        break
    if ret == -1:
        print("传输失败/超时"); break
    time.sleep(0.001)  # 避免空转
print("异步完成，耗时 %.3f s" % (time.perf_counter() - t0))

# 5) 回收本地缓冲
for b in bufs:
    e.free_managed_buffer(b, 4 * 1024 * 1024)
```

观察现象：

1. `batch_transfer_async_write` 几乎立刻返回（提交开销）。
2. `get_batch_transfer_status` 在 COMPLETED 后返回 `0`，此时 `batch_id` 已被释放。
3. 若再次用同一个 `bid` 轮询会出问题（double free）—— 别这么做。

> 需 target 侧先就绪；无对端时标注「待本地验证」。吞吐对比见第 5 节综合实践。

#### 4.4.5 小练习与答案

**练习 1**：`batch_transfer_async_write` 返回 `0` 是成功还是失败？为什么不用 `-1`？
**答案**：是**失败**。因为 `batch_id_t` 是无符号 `uint64_t`，`-1` 在 Python 里会被显示成 `18446744073709551615`（\(2^{64}-1\)），极具误导性，所以源码刻意用 `0` 作失败哨兵。见 [transfer_engine_py.cpp:609-611](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L609-L611)。

**练习 2**：`get_batch_transfer_status` 返回后，我还需要手动释放 `batch_id` 吗？
**答案**：不需要。该方法在 COMPLETED 时已对每个完成批次调用 `freeBatchID`，在失败/超时时也会兜底释放所有在途批次（[transfer_engine_py.cpp:714-718](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L714-L718)）。重复释放会导致问题。

**练习 3**：异步为什么可能比同步快？
**答案**：异步让你可以「提交下一批的同时上一批还在传」，实现**流水线重叠**；`get_batch_transfer_status` 还能一次轮询多个批次，减少轮询开销。注意：单个批次、串行提交时，异步并不比同步快，收益来自并发与重叠。

---

### 4.5 CUDA 流同步传输（transfer_*_on_cuda）

#### 4.5.1 概念说明

在 GPU 推理场景里，数据搬运必须和 CUDA kernel 的执行顺序对齐：「等前面所有 kernel 算完，再发起这次传输」。`transfer_*_on_cuda` 系列就是为此设计的：它把一次传输**挂到一条 CUDA 流上**，让 CUDA 驱动在「流中前序操作都完成时」自动触发传输。

> 仅在编译开启 `USE_CUDA` 时存在（`#ifdef USE_CUDA`），否则这些方法不会注册到 Python。

四个对外方法（都是 `batchTransferOnCuda` 的薄封装）：

- `transfer_write_on_cuda(target, buffer, peer_buffer_address, length, stream_ptr=0, transport_hint="")`
- `transfer_read_on_cuda(...)`
- `batch_transfer_write_on_cuda(target, buffers, peer_buffer_addresses, lengths, stream_ptr=0, ...)`
- `batch_transfer_read_on_cuda(...)`

`stream_ptr` 是 `cudaStream_t` 句柄转成的整数（PyTorch 里可用 `torch.cuda.current_stream().cuda_stream` 拿到）。

#### 4.5.2 核心流程

`batchTransferOnCuda`（[transfer_engine_py.cpp:897-954](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L897-L954)）：

1. 放开 GIL；查/建段句柄（失败抛 `runtime_error`，因为返回类型是 `void`）。
2. 校验三个数组长度一致。
3. 构造 `TransferRequest` 列表，累加 `total_bytes`。
4. `allocateBatchID(batch_size)`，`new` 一个 `TransferOnCudaContext{engine, batch_id, entries, total_bytes}`。
5. `cudaLaunchHostFunc(stream, transfer_on_cuda_callback, ctx)` —— **关键**：往流里插一个「主机回调」。
6. 返回（不阻塞）。真正的传输发生在回调里。

CUDA 驱动在流中前序操作完成后，调用回调 `transfer_on_cuda_callback`（[transfer_engine_py.cpp:837-881](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L837-L881)）：

1. `submitTransfer(ctx->batch_id, ctx->requests)`。
2. 循环 `getBatchTransferStatus` 直到 `COMPLETED`（回调内部是**同步等待**的）。
3. 成功 → `freeBatchID` + `delete ctx`。
4. 失败 → `_exit(1)`。**注意**：这是 CUDA 驱动线程里的回调，无法把异常/错误码抛回主程序，失败意味着后续流操作所依赖的数据没到位，系统已不一致，故直接 `_exit(1)` 终止进程。

#### 4.5.3 源码精读

**挂载到 CUDA 流**：

[transfer_engine_py.cpp:941-953](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L941-L953) —— `cudaLaunchHostFunc` 是 CUDA Runtime API，它把一个主机函数排进指定流，保证该函数在「流中所有先前操作完成后」才被驱动线程调用。`cudaLaunchHostFunc` 失败时清理 `ctx` 与 `batch_id` 并抛异常。

**回调内同步等待**：

[transfer_engine_py.cpp:840-881](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L840-L881) —— 提交后在回调线程里 `while` 轮询直到 COMPLETED/FAILED/TIMEOUT；任何失败都 `goto error_exit` → `_exit(1)`。

**上下文结构体**：

[transfer_engine_py.cpp:821-826](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L821-L826) —— `TransferOnCudaContext` 持有 `engine`（`shared_ptr`，保证回调期间引擎存活）、`batch_id`、`requests`、`total_bytes`。

#### 4.5.4 代码实践

**实践目标**：理解 `stream_ptr` 的来源，阅读官方示例体会「挂流」语义。

PyTorch 里拿 `stream_ptr` 的标准方式（示例代码）：

```python
import torch
stream = torch.cuda.current_stream()
stream_ptr = stream.cuda_stream   # 即 cudaStream_t 句柄整数
```

调用模式（示例代码，需 CUDA 构建 + GPU）：

```python
e.transfer_write_on_cuda(
    "127.0.0.1:<target端口>",
    local_gpu_buf_ptr,        # GPU 内存指针（tensor.data_ptr()）
    remote_addr, length,
    stream_ptr=stream_ptr,
)
# 该调用立刻返回；传输会在 stream 前序 kernel 完成后由驱动触发
```

操作步骤与观察：

1. 先确认 `engine.SUPPORT_CUDA` 为 `True`。
2. `transfer_write_on_cuda` 应立刻返回（非阻塞）。
3. 若想在 Python 侧确认传输完成，需对 `stream` 做 `torch.cuda.stream_synchronize(stream)`（因为传输在回调里同步等待，回调完成后流才推进）。
4. 若传输失败，整个进程会 `_exit(1)` 退出 —— 这是设计行为，不是 bug。

> 需要 GPU + CUDA 构建的 wheel；无此环境时按源码阅读理解，标注「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `batchTransferOnCuda` 失败时抛异常，而 `batchTransferAsync` 失败时返回 `0`？
**答案**：`batchTransferOnCuda` 的返回类型是 `void`，无法用返回值表达失败，所以用 `std::runtime_error`（见 [transfer_engine_py.cpp:910-913](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L910-L913)）；而 `batchTransferAsync` 返回 `batch_id_t`，用 `0` 哨兵即可。

**练习 2**：回调里传输失败为什么要 `_exit(1)` 而不是抛异常？
**答案**：回调运行在 CUDA 驱动线程里，异常无法穿越线程边界传回主程序；且失败意味着后续流操作依赖的数据缺失，系统已不一致。源码注释（[transfer_engine_py.cpp:873-880](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L873-L880)）明确说明用 `_exit(1)` 立即终止以避免未定义行为。

---

### 4.6 辅助接口：内存注册、通知、探针、拓扑

#### 4.6.1 概念说明

除了「搬数据」，`TransferEnginePy` 还暴露一组辅助接口：

- **内存注册**：`register_memory` / `unregister_memory` / `batch_register_memory` / `batch_unregister_memory` —— 让你自己管理注册（比如注册 PyTorch tensor、mmap 出来的大页内存）。`kvcache_prefix_bench.py` 就是用 `register_memory` 注册几十 GB 的 KV cache 池。
- **`get_first_buffer_address(segment_name)`**：查对端某段的第一个 buffer 地址 —— 这是发起传输前拿到「对端落点」的标准方式。
- **`get_notifies()`**：拉取对端发来的通知（`name` + `msg`），配合 `transfer_sync(..., notify=...)` 的带通知传输使用。
- **`send_probe(peer_server_name)`**：向对端发一个 JSON-RPC 探针，验证可达性（返回 0 成功）。源码注释提到它被 SGLang 用于「失败会话黑名单恢复」。
- **`get_local_topology(device_name)`**：探测本机拓扑（HCA 列表等），返回字符串。
- **`write_bytes_to_buffer` / `read_bytes_from_buffer`**：纯本地的 `memcpy` / 读出 bytes，调试用。
- **`warmup_efa_segment(segment_name)`**：EFA 专用，预连接每个 (本地 context, 对端 NIC) 对，避免首次提交卡在握手。

#### 4.6.2 核心流程

以 `register_memory` 为例（[transfer_engine_py.cpp:802-806](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L802-L806)）：把地址、容量、location 直接交给 `engine_->registerLocalMemory`。批量版 `batchRegisterMemory`（[transfer_engine_py.cpp:778-789](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L778-L789)）构造 `BufferEntry` 列表后调 `registerLocalMemoryBatch`，适合一次注册很多块（比逐个注册快）。

`get_first_buffer_address`（[transfer_engine_py.cpp:1023-1032](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L1023-L1032)）：`openSegment` 后从元数据里取 `segment_desc->buffers[0].addr`；段不存在或无 buffer 返回 `0`。

`send_probe`（[transfer_engine_py.cpp:1065-1069](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L1065-L1069)）：委托 `engine_->getMetadata()->sendProbe(peer)`，返回 0 表示对端可达。

#### 4.6.3 源码精读

**`get_first_buffer_address` 的真实用法**：

[kvcache_prefix_bench.py:275-282](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/example/kvcache_prefix_bench.py#L275-L282) —— initiator 连上 target 后，靠 `engine.get_first_buffer_address(args.target_server_name)` 拿到对端 KV cache 池的起点地址；若返回 `0` 直接报错退出（说明对端没注册或没连上）。

**`register_memory` 的真实用法**：

[kvcache_prefix_bench.py:203-209](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/example/kvcache_prefix_bench.py#L203-L209) —— target 侧先 `mmap` 出大页内存，再 `register_memory(pool_addr, pool_bytes)`，并打印注册耗时（大块注册可达数秒）。

**批量注册签名**：

[transfer_engine_py.cpp:778-789](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L778-L789) —— `batchRegisterMemory(buffer_addresses, capacities, location="*")`。

#### 4.6.4 代码实践

**实践目标**：体验「自己分配 + 自己注册」的流程（不走 managed buffer），对比它与托管分配的区别。

```python
# register_probe.py  —— 示例代码（单机，纯本地，可验证注册语义）
import ctypes, ctypes.util
from mooncake.engine import TransferEngine

def mmap_cpu(n):
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    libc.mmap.restype = ctypes.c_void_p
    ptr = libc.mmap(None, n, 0x3, 0x22, -1, 0)  # PROT_RW | MAP_PRIVATE|ANON
    return ptr

e = TransferEngine()
e.initialize("127.0.0.1:0", "P2PHANDSHAKE", "tcp", "")

ptr = mmap_cpu(32 * 1024 * 1024)        # 自己分配 32MB
ret = e.register_memory(ptr, 32 * 1024 * 1024)  # 自己注册
print("register ret =", ret)            # 期望 0
# ... 用 ptr 做传输 ...
e.unregister_memory(ptr)                # 自己注销（析构前必须调用）
```

观察现象：

1. `register_memory` 返回 `0` 表示成功。
2. 头文件注释（[transfer_engine_py.h:188-189](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.h#L188-L189)）强调：`unregister_memory` 必须在引擎析构**之前**调用，否则可能出问题。

> 单机 TCP 构建即可跑通注册/注销；无包时标注「待本地验证」。

#### 4.6.5 小练习与答案

**练习 1**：`get_first_buffer_address` 返回 `0` 可能是什么原因？
**答案**：段不存在（对端未 `openSegment`/未注册）、或段的 `buffers` 为空（见 [transfer_engine_py.cpp:1027-1031](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L1027-L1031)）。通常意味着对端还没准备好。

**练习 2**：什么时候该用 `batch_register_memory` 而不是循环调 `register_memory`？
**答案**：当要一次性注册很多块内存时。批量版内部走 `registerLocalMemoryBatch`，能合并开销，比逐个注册更高效。见 [transfer_engine_py.cpp:778-789](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L778-L789)。

---

## 5. 综合实践

**任务**：仿照 `kvcache_prefix_bench.py` 的双节点模式，编写一个 `sync_vs_async.py`，对比「同步批量写」与「异步批量写 + 流水线」两种方式搬同样多数据的吞吐。要求用到本讲学过的：托管内存分配、异步批量提交、`get_batch_transfer_status` 轮询、`get_first_buffer_address` 取对端地址。

**设计要点**（关键代码骨架，示例代码）：

```python
# sync_vs_async.py  —— 示例代码骨架（initiator 侧）
import time
from mooncake.engine import TransferEngine

CHUNK = 4 * 1024 * 1024          # 4MB / 块
N = 16                            # 共 16 块 = 64MB
TARGET = "127.0.0.1:<target端口>"

e = TransferEngine()
e.initialize("127.0.0.1:12346", "P2PHANDSHAKE", "tcp", "")

# 1) 托管内存分配本地缓冲（已注册）
bufs = [e.allocate_managed_buffer(CHUNK) for _ in range(N)]
remote = e.get_first_buffer_address(TARGET)
peer = [remote + i * CHUNK for i in range(N)]
lens = [CHUNK] * N

# 预热连接
e.transfer_sync_write(TARGET, bufs[0], peer[0], 1024)

# ---- 方式 A：同步批量（阻塞，一次搬完 16 块）----
t0 = time.perf_counter()
e.batch_transfer_sync_write(TARGET, bufs, peer, lens)
dt_sync = time.perf_counter() - t0

# ---- 方式 B：异步批量（提交即返回，再统一轮询）----
bid = e.batch_transfer_async_write(TARGET, bufs, peer, lens)
t0 = time.perf_counter()
while e.get_batch_transfer_status([bid]) != 0:
    time.sleep(0.0005)
dt_async = time.perf_counter() - t0

total_gb = (N * CHUNK) / 1e9
print("同步批量: %.3f s, %.2f GB/s" % (dt_sync, total_gb / dt_sync))
print("异步批量: %.3f s, %.2f GB/s" % (dt_async, total_gb / dt_async))

for b in bufs:
    e.free_managed_buffer(b, CHUNK)
```

**操作步骤**：

1. 先在一个终端起 target（参考 `kvcache_prefix_bench.py` 的 `run_target`：`initialize` + 大块 `register_memory` 后 `signal.pause()`），记下它的 `serving` 端口。
2. 把脚本里 `<target端口>` 替换为该端口，运行 initiator 脚本。
3. 观察两种方式的吞吐。

**预期与思考**：

1. 单批次、串行提交时，同步与异步的端到端吞吐通常**接近**（异步的收益不在单批次）。
2. 若把方式 B 改成「**提交第 k 批的同时轮询第 k-1 批**」（真正的流水线），异步吞吐会明显高于同步 —— 这才是异步的价值所在。可作为进阶改造：把 64MB 拆成多批，用 `get_batch_transfer_status([多个 bid])` 一次轮询。
3. 若 `batch_transfer_async_write` 返回 `0`，说明提交失败，应停止。

> 无双节点 / 无包环境时，本实践可降级为「源码阅读型」：对照 [transfer_engine_py.cpp:596-660](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L596-L660) 与 [transfer_engine_py.cpp:662-721](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L662-L721) 理解为何异步能做流水线，标注「待本地验证」。

---

## 6. 本讲小结

- `TransferEnginePy` 是 C++ `TransferEngine` 的 Python 便利封装：它把「分配 batch → 提交 → 轮询 → 释放」的生命周期封装成几类一句话 API。
- **返回值约定**：同步接口返回 `int`（0 成功 / -1 失败）；异步接口返回 `batch_id`（**0 是失败哨兵**，因为 `batch_id_t` 是无符号）。
- **托管内存** `allocate_managed_buffer` 内部是 buddy 分配器（8KB~256MB 共 16 级 slab，超大块走直通），分配即注册；`free_managed_buffer` 必须传回**相同 length** 以正确归还等级。
- **同步传输**会自己 `openSegment`（带缓存）、`submitTransfer`、轮询到终态、`freeBatchID`，并有基于 length 的超时与多 context 重试。
- **异步批量传输**提交即返回 `batch_id`；`get_batch_transfer_status([ids])` 一次轮询多个批次，并在 COMPLETED/失败时自动释放批次。异步的真正价值在于**流水线重叠**。
- **CUDA 流传输**通过 `cudaLaunchHostFunc` 把传输挂到流上，回调内同步等待；失败时 `_exit(1)`，因为驱动线程回调无法抛异常。
- **辅助接口**：`register_memory`/`batch_register_memory` 自管注册、`get_first_buffer_address` 取对端落点、`send_probe` 探测可达性、`get_notifies` 拉通知、`warmup_efa_segment` 预热 EFA。

---

## 7. 下一步学习建议

1. **向下看传输层**：`batch_transfer_async` 最终调到 `TransferEngine::submitTransfer`，建议进入 `u3-l1`（Transport Base / Slice / Batch）理解 `TransferRequest` 是如何被切成 `Slice`、分发给具体 transport 的。
2. **看具体 transport**：TCP（`u3-l3`）、RDMA（`u3-l2`）如何兑现这些 batch 请求；理解不同 protocol 下 `register_memory` 的代价差异。
3. **CUDA 集成进阶**：结合推理框架（如 SGLang/vLLM）里 KV cache 迁移的真实用法，体会 `transfer_*_on_cuda` 与计算流重叠的设计动机。
4. **元数据与段**：进入 `u2-l2`（Transfer Metadata）理解 `openSegment` / `get_first_buffer_address` 背后的全局目录机制。
5. **TENT 新栈**：`transport_hint` 参数只有在 `USE_TENT` 构建下才生效（见 `parseTransportHint`，[transfer_engine_py.cpp:328-342](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L328-L342)）；可在 `u4` 系列了解 TENT 的传输选择器。
