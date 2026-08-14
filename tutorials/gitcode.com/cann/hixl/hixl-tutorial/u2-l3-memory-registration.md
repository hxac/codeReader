# 内存注册与解注册：零拷贝的前提

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚「为什么零拷贝单边传输必须先注册内存」——注册到底让引擎记住了什么、后续被谁使用。
2. 正确使用 `RegisterMem` / `DeregisterMem` 与 `MemDesc` / `MemType` / `MemHandle` 三个关键类型，理解 MemHandle 是不透明句柄。
3. 读懂引擎内部两级内存登记：`Segment` 区间抽象（client handler 侧按 MemType 聚合地址区间）与 `HixlMemStore` 内存台账（CS 侧记录 server/client 两端内存区域并在传输前做访问校验）。
4. 独立完成实践任务：扩展 quickstart 样例，注册一块 `MEM_DEVICE` 与一块 `MEM_HOST` 内存，分别传输并对比行为，最后正确解注册。

## 2. 前置知识

本讲建立在前几讲的概念之上，先快速复习并补充两个新名词：

- **零拷贝单边传输**：HIXL 的传输是数据在用户内存之间直达（DMA 搬运），引擎不中转、不缓冲。这带来一个直接后果——引擎必须**事先知道**哪些内存可以被对端直接读写，否则任何一个野指针地址都可能被下发到硬件。
- **注册内存（memory registration）**：RDMA 等单边通信技术的通用概念。用户把一段内存的「地址 + 长度」登记到通信库，通信库据此完成两件事：① 让硬件/驱动把这段内存映射为远端可寻址（例如锁页、建立地址映射）；② 在本地建立一份「已授权内存清单」，后续每次传输先查清单，防止未注册地址被误用。
- **`MemType`（u2-l2 已学）**：`MEM_DEVICE` 表示昇腾设备上的显存，`MEM_HOST` 表示主机侧锁页内存。两者的注册后处理不同——host 内存可能需要额外做「主机虚拟地址 → 设备可见地址」的映射（本讲 4.3 会看到 `register_dev_addr`）。
- **`MemHandle`（u2-l2 已学）**：`void*` 不透明句柄，注册成功后由引擎返回，用户只保存、只在解注册时原样传回，**不要解引用、不要猜测其内容**。
- **server 与 client 的记忆点（u1-l3 已学）**：HIXL 里「server/client」指 engine 角色（`local_engine` 带端口即为 server），与「谁注册内存」无关——**两端都要注册自己的内存**，quickstart 样例里 client 和 server 各调用了一次 `RegisterMem`。

一个贯穿全讲的区间数学：判断两个内存区间 \([s, e)\) 与 \([r_s, r_e)\) 是否重叠，条件是

\[ s < r_e \land r_s < e \]

这个式子会在 `HixlMemStore` 的重叠检查里原样出现。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `include/hixl/hixl.h` | 公开接口声明：`RegisterMem` / `DeregisterMem` 及注释契约 |
| `src/hixl/engine/hixl_impl.cc` | `Hixl` 外壳与 `HixlImpl`：参数门卫检查后转发给引擎 |
| `src/hixl/engine/hixl_engine.cc` | `HixlEngine`：持有 `mem_map_`（handle → MemHandleInfo），解注册前检查链路状态 |
| `src/hixl/engine/hixl_server.cc` | `HixlServer`：去重检查后调用 CS C 接口 `HixlCSServerRegMem` |
| `src/hixl/cs/hixl_cs.cc` | CS 层 C 风格接口薄封装 |
| `src/hixl/cs/hixl_cs_client.cc` | client 侧内存注册与 server 内存导入的落地处 |
| `src/hixl/common/segment.h` / `segment.cc` | `Segment`：按 MemType 聚合的地址区间集合 |
| `src/hixl/engine/ub_client_handler.cc` | 用 `Segment` 维护本地/远端内存区间的使用者 |
| `src/hixl/cs/hixl_mem_store.h` / `hixl_mem_store.cc` | `HixlMemStore`：CS 侧 server/client 两端内存台账与访问校验 |
| `examples/cpp/hixl_example_quickstart.cpp` | 实践基准样例 |

## 4. 核心概念与源码讲解

### 4.1 内存注册：RegisterMem / DeregisterMem 的语义与调用链

#### 4.1.1 概念说明

`RegisterMem` 回答的问题是：**「这段内存，我授权引擎和对端在传输中直接访问」**。注册之后：

- 引擎为本地区间建立登记（后文的 `mem_map_`、`Segment`、`HixlMemStore`）；
- 建链时，本端已注册内存的信息会随链路协商同步到对端，对端据此能直接用地址发起 READ/WRITE；
- 后续每次传输前，引擎会校验传输描述里的地址确实落在已注册区间内，未注册地址会被拒绝（返回 `PARAM_INVALID`），这是一道防止野指针下发给硬件的安全闸。

`DeregisterMem` 是逆操作：撤销授权、清理登记。它有一个容易被忽略的约束——**必须先断开所有 client 链路才能解注册**（源码见 4.1.3 第 3 段），因为对端可能还握着这段内存的地址。

#### 4.1.2 核心流程

一次 `RegisterMem(desc, MEM_DEVICE, handle)` 的下沉路径：

```text
Hixl::RegisterMem            (外壳：日志 + 转发)
  └─ HixlImpl::RegisterMem   (门卫：engine 已初始化？addr 非空？)
      └─ HixlEngine::RegisterMem   (持锁；登记 mem_map_：handle → {handle, mem, type})
          └─ HixlServer::RegisterMem
              ├─ 溢出检查：addr + len 是否溢出 uint64
              ├─ 重复检查：与 handle_to_addr_ 中已有区间比对
              │    ├─ 完全相同 → 幂等，直接返回已有 handle
              │    └─ 重叠但不相同 → 报错
              └─ HixlCSServerRegMem  (CS C 接口 → HixlCSServer::RegisterMem，落到传输层注册)
```

建链（`Connect`）时，`HixlEngine` 会把 `mem_map_` 里登记的全部内存收集为 `mem_info_list` 交给 client 侧（见 `hixl_engine.cc` 中 `BuildClientConfig` 的调用），后续由 client handler 转成 `Segment` 区间并注册到 CS client（4.2 详述）。

解注册路径对称：`Hixl::DeregisterMem → HixlImpl（handle 非空检查）→ HixlEngine（client 链路必须已全部断开）→ HixlServer（查表、调 HixlCSServerUnregMem、清表）`。

#### 4.1.3 源码精读

**第 1 段：公开契约。** 头文件注释明确了参数含义与 handle 的用途——「注册成功返回的内存 handle，可用于内存解注册」：

[include/hixl/hixl.h:54-67](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl.h#L54-L67)
这段代码声明了 `RegisterMem(const MemDesc &mem, MemType type, MemHandle &mem_handle)` 与 `DeregisterMem(MemHandle mem_handle)`：`mem` 描述要注册哪块内存，`type` 声明内存类型，`mem_handle` 是出参句柄。

**第 2 段：门卫检查。** `HixlImpl` 在转发前做了两层校验——引擎已初始化、地址非空，任何一条不满足都直接返回错误而不会碰引擎内部状态：

[src/hixl/engine/hixl_impl.cc:112-126](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_impl.cc#L112-L126)
`HixlImpl::RegisterMem` 依次检查 `engine_ != nullptr`、`engine_->IsInitialized()`、`mem.addr != nullptr`，然后调用 `engine_->RegisterMem`；`DeregisterMem` 对称地检查 `mem_handle != nullptr`。这与 u2-l1 讲过的「外壳只做日志与门卫」风格一致。

**第 3 段：引擎层登记与解注册约束。** `HixlEngine` 把 `{handle, mem, type}` 存入 `mem_map_`，并在解注册前强制「所有 client 已断开」：

[src/hixl/engine/hixl_engine.cc:101-141](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_engine.cc#L101-L141)
注册时先调 `server_.RegisterMem` 拿到真实 handle，再 `mem_map_.emplace` 登记一条 `MemHandleInfo`；解注册时若 handle 不在表中则幂等返回 `SUCCESS`，而若 `client_manager_.IsEmpty()` 为假（仍有活跃链路）则返回 `FAILED` 并提示「All clients must be disconnected before deregistration」。注意整个函数在 `mutex_` 与 ACL context guard 保护下执行，注册/解注册是线程安全的。

**第 4 段：去重与溢出检查。** `HixlServer::RegisterMem` 在调 CS 接口之前先自查：

[src/hixl/engine/hixl_server.cc:102-130](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/hixl_server.cc#L102-L130)
先用 `ge::AddOverflow` 防止 `addr + len` 溢出 uint64（构造 `AddrInfo{start_addr, end_addr}`），再调 `CheckAddrOverlap` 与 `handle_to_addr_` 比对：完全相同的区间幂等返回已有 handle；否则把 `MemType` 映射为 `COMM_MEM_TYPE_DEVICE/HOST` 装进 `CommMem`，调用 `HixlCSServerRegMem` 完成真正的注册。

**第 5 段：CS C 接口薄封装。**

[src/hixl/cs/hixl_cs.cc:57-73](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs.cc#L57-L73)
`HixlCSServerRegMem` / `HixlCSServerUnregMem` 就是 u1-l4 提到的 `extern "C"` 风格 CS 接口：handle 转型、判空、转发到 `HixlCSServer` 对象方法。引擎层（C++ 类接口）与 CS 层（C 接口）在这里对接。

**第 6 段：样例中的标准用法。** quickstart 两端各自注册，顺序是「先注册、再交换地址」：

[examples/cpp/hixl_example_quickstart.cpp:174-198](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e9bb6c8929477/examples/cpp/hixl_example_quickstart.cpp#L174-L198)
server 侧（行 181-188）：`aclrtMalloc` 分配 device 内存 → 填充 `ctx.desc.addr/len` → 拷入测试数据 → `RegisterMem(ctx.desc, MEM_DEVICE, ctx.handle)` → 才通过 socket 把地址发给 client。注释「避免未注册地址被 client 提前使用」点明了这个顺序的意义。

[examples/cpp/hixl_example_quickstart.cpp:120-137](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_quickstart.cpp#L120-L137)
清理函数 `Finalize` 展示了正确顺序：先 `close(fd)`，再 `DeregisterMem(ctx.handle)`（client 链路此时已断开，满足引擎约束），再 `aclrtFree` 释放内存，最后 `engine.Finalize()`。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：验证「未注册的地址会被传输接口拒绝」，从而理解注册是安全闸而不只是性能优化。
2. **操作步骤**：
   - 通读 `examples/cpp/hixl_example_quickstart.cpp` 的 `RunClient`（行 153-172），注意 `RegisterMem` 在 `Connect` **之前**完成；
   - 做一个思想实验改编（或本地改编后编译）：把行 159 的 `RegisterMem` 调用注释掉，直接 `Connect` + `TransferSync`；
   - 对照 `src/hixl/engine/ub_client_handler.cc` 行 575-582 附近对 `GetMemType(local_segments_, op.local_addr, ...)` 的检查——传输描述中的地址必须能在 segment 中找到对应 MemType，否则返回 `PARAM_INVALID`。
3. **需要观察的现象**：`TransferSync` 返回非 `SUCCESS`，错误信息指向地址未注册/未找到；进程不会崩溃，说明校验在用户态拦截了。
4. **预期结果**：恢复 `RegisterMem` 后样例恢复正常。（在真实昇腾环境上的实际错误码值：待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：同一块内存连续调用两次 `RegisterMem`，第二次会发生什么？
**答案**：`HixlServer::RegisterMem` 中的 `CheckAddrOverlap` 判定为完全相同区间时幂等返回**已有 handle**并记录日志 `Memory already registered, returning existing handle`，接口返回 `SUCCESS`（见 `hixl_server.cc` 行 111-119）。但注意 CS 层 `HixlMemStore::RecordMemory` 对 client 侧重复注册返回 `PARAM_INVALID`（见 4.3），两层语义并不相同。

**练习 2**：先 `RegisterMem`、再 `Connect`、然后直接 `DeregisterMem`，会发生什么？
**答案**：`HixlEngine::DeregisterMem` 检查 `client_manager_.IsEmpty()`，只要仍有活跃 client 链路就返回 `FAILED` 并提示先断开所有链路（`hixl_engine.cc` 行 127-131）。必须先 `Disconnect` 再解注册——quickstart 的 `Finalize` 正是这个顺序。

**练习 3**：`MemHandle` 里存的是什么？能打印它的值吗？
**答案**：它是 `void*` 不透明句柄，内容由引擎定义（指向内部登记结构），用户不应解引用或假设其布局；打印指针值本身（如样例中的 `%p` 日志）是允许的，仅用于日志追踪。

### 4.2 segment：按 MemType 聚合的地址区间抽象

#### 4.2.1 概念说明

`Segment` 回答的问题是：**「对每一种内存类型，当前有哪些地址区间是被授权的？」**。它是最朴素的登记结构——一个 `MemType` 加一个 `(start, end)` 区间的有序列表。传输发起前，client handler 用它反查「这个地址属于哪类内存」，进而决定走哪条链路（device 内存走 HCCS 设备链路、host 内存走对应链路），这就是 u3-l2 将讲的 handler 选路依据之一。

关键设计：**每种 MemType 最多一个 Segment 对象，多次注册的区间合并进同一个 Segment**（`AddRange` 支持任意多个不重叠区间），所以查询时面对的是「每类内存一张区间表」，而不是「每次注册一条记录」。

#### 4.2.2 核心流程

```text
注册（client 侧本地内存）:
  UbClientHandler::RegisterMem(mem_info)
    ├─ 在 local_segments_ 中找 MemType 相同的 Segment
    │    ├─ 找到   → seg->AddRange(addr, len)     # 区间并入已有表
    │    └─ 没找到 → new Segment(type) + AddRange # 该类型第一块内存
    └─ （继续构造 CommMem 交给 CS 层，见 4.3）

建链（远端内存）:
  UbClientHandler 收到对端交换来的 mem_info 列表
    └─ BuildRemoteSegmentsFromMemInfo
         ├─ MEM_DEVICE 的所有区间并入一个 device_seg
         ├─ MEM_HOST   的所有区间并入一个 host_seg
         └─ 存入 remote_segments_

查询:
  Contains(start, end) —— 判断 [start, end] 是否被若干已注册区间联合覆盖
```

`Contains` 的覆盖判断值得注意：由于 `AddRange` 不合并相邻区间，一次传输的区间可能横跨多条登记记录，所以算法从 `start` 出发向右「贪心扩张」`max_reached`：先看左边是否有区间覆盖 `start`，再逐个吸收起点落在当前覆盖边界（允许 `it->first <= max_reached + 1`，即紧邻可拼）的区间，直到 `max_reached >= end` 或断开。

#### 4.2.3 源码精读

**第 1 段：Segment 类定义。**

[src/hixl/common/segment.h:18-31](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/segment.h#L18-L31)
`Segment` 只有四个方法：`AddRange(start, len)` 登记、`RemoveRange(start, end)` 注销、`Contains(start, end)` 查询覆盖、`GetMemType()` 取类型；内部数据是 `vector<pair<uint64_t, uint64_t>>`（按 start 有序的 `[start, end)` 列表）。

**第 2 段：登记与幂等。**

[src/hixl/common/segment.cc:17-42](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/segment.cc#L17-L42)
`AddRange` 先做地址溢出检查（`len > UINT64_MAX - start` 则返回 `PARAM_INVALID`），再用 `std::upper_bound` 找到插入位置保持有序；若已存在完全相同的 `(start, end)` 记录日志后幂等返回 `SUCCESS`。

**第 3 段：区间覆盖查询。**

[src/hixl/common/segment.cc:61-107](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/common/segment.cc#L61-L107)
`Contains` 先反向遍历找一个 `second > start` 的左邻区间确认起点被覆盖（`covered_start`），然后正向扫描：只要当前区间起点 `<= max_reached + 1`（紧邻可拼接）就继续扩张 `max_reached`，最终以 `max_reached >= end` 判定整个 `[start, end]` 是否被覆盖。这允许传输区间横跨多次注册的相邻内存块。

**第 4 段：谁在维护 Segment。** client handler 是 `Segment` 的使用者，本地与远端各一组：

[src/hixl/engine/ub_client_handler.cc:293-309](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/ub_client_handler.cc#L293-L309)
`UbClientHandler::RegisterMem` 在 `local_segments_` 中按 `MemType` 查找，找到就把新注册的 `(addr, len)` 并入，找不到则新建一个 `Segment`——印证了「每类内存一张区间表」的设计。

[src/hixl/engine/ub_client_handler.cc:270-291](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/engine/ub_client_handler.cc#L270-L291)
`BuildRemoteSegmentsFromMemInfo` 处理**对端**随链路交换过来的内存信息：同样按 `MEM_DEVICE`/`MEM_HOST` 各建一个 Segment，逐条 `AddRange`，存入 `remote_segments_`。行 188 附近的注释说明这些信息是通过复用 ctrl socket 从 server 拉取的——这就是「注册的内存信息如何被对端知道」的答案。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：亲手验证 `Segment::Contains` 的「跨区间拼接」行为。
2. **操作步骤**：
   - 阅读 `src/hixl/common/segment.cc` 的 `Contains`，重点关注行 93 的 `if (it->first > (max_reached + 1)) break;`；
   - 在纸上模拟：`ranges_ = {(100, 200), (200, 300), (400, 500)}`，查询 `Contains(150, 250)` 与 `Contains(150, 450)`；
   - 若本地有编译条件，可写一个仅依赖 `segment.cc` 的小测试（示例代码，非项目原有）：
     ```cpp
     hixl::Segment seg(hixl::MemType::MEM_DEVICE);
     seg.AddRange(100, 100);  // (100, 200)
     seg.AddRange(200, 100);  // (200, 300)
     printf("%d %d\n", seg.Contains(150, 250), seg.Contains(150, 450)); // 期望 1 0
     ```
3. **需要观察的现象**：横跨两条相邻登记记录的区间判定为包含，中间有空洞的判定为不包含。
4. **预期结果**：`Contains(150, 250)` 返回 true，`Contains(150, 450)` 返回 false。（独立编译该小测试的工程配置：待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Segment` 要按 `MemType` 分开建，而不是一张大表？
**答案**：传输选路需要知道「地址属于哪类内存」——device 内存与 host 内存可用的链路和地址转换方式不同（host 地址还需经 `register_dev_addr` 转换，见 4.3）。按类型分表后，查询命中即可直接得到 MemType（`ub_client_handler.cc` 行 594 附近的 `GetMemType` 就是遍历对应表做 `Contains`）。

**练习 2**：`AddRange` 插入时为什么用 `upper_bound` 而不是 `push_back`？
**答案**：保持 `ranges_` 按 start 有序，是 `Contains` / `RemoveRange` 中二分查找（`upper_bound` / `lower_bound`）的前提；无序表会把查询退化为线性扫描。

**练习 3**：对端是怎么知道我注册了哪些内存的？
**答案**：注册信息登记在本端（`mem_map_` / `local_segments_`）；建链时通过控制面（复用 ctrl socket 拉取 server 内存信息，见 `ub_client_handler.cc` 行 188 注释与 `BuildRemoteSegmentsFromMemInfo`）交换到对端，对端据此构建 `remote_segments_`。这也是 quickstart 中「先注册、后交换地址、再建链」顺序的深层原因。

### 4.3 HixlMemStore：CS 侧的两端内存台账与访问校验

#### 4.3.1 概念说明

`Segment` 服务于 client handler 的选路；`HixlMemStore` 则是 **CS 通信服务内部的台账**，与 client 绑定（类注释：一个 Client 有一个 memstore 对象），同时记录两件事：

1. **server 侧内存**（`server_regions_`）：对端（server 角色）注册、经链路交换导入后由 client 登记的内存——即「对端授权我访问的内存」；
2. **client 侧内存**（`client_regions_`）：本端自己注册的内存——即「我提供给传输用的本地缓冲」。

它承担三项职责：**登记/注销**（`RecordMemory`/`UnrecordMemory`）、**注册前重叠检查**（`CheckMemoryForRegister`）、**传输前访问校验与地址转换**（`ValidateMemoryAccess`/`BatchValidateMemoryAccess`/`BatchConvertHostAddr`）。其中 host 内存的 `register_dev_addr` 字段是理解 D2rH/H2rD 类传输的钥匙：设备发起 DMA 不能直接用主机虚拟地址，必须用注册时得到设备侧映射地址，传输前逐条替换。

#### 4.3.2 核心流程

```text
登记（两条来源）:
  A. 本端注册  → HixlCSClient::RegMemLocked
       ├─ CheckMemoryForRegister(false, addr, size)  # 与 client_regions_ 比对，禁止重叠
       ├─ local_endpoint_->RegisterMem(...)          # 落到 endpoint/传输层
       ├─ 若是 host 内存且 endpoint 需要 VA 映射
       │    → HostRegisterProxy 查得 register_dev_addr
       └─ mem_store_.RecordMemory(false, addr, size, is_host_mem, register_dev_addr)
  B. 对端内存导入 → ImportOneDesc（建链时收到 server 的内存描述列表）
       ├─ ep->MemImport(export_desc, ...)            # 导入对端内存 → 本地可用 CommMem
       └─ store->RecordMemory(true, mem.addr, ...)   # 登记到 server_regions_

传输前校验（每次下发传输任务）:
  BatchValidateMemoryAccess(list_num, desc_list)
    └─ 对每条 desc：
         ├─ CheckMemoryForAccess(true,  remote_buf, len)  # server 地址须落在 server_regions_
         └─ CheckMemoryForAccess(false, local_buf,  len)  # client 地址须落在 client_regions_
  BatchConvertHostAddr(list_num, desc_list)
    └─ 对每条 desc 的两端：若是 host 内存，把 host 虚拟地址替换为
       register_dev_addr + (原地址 - 区间起始地址)
```

重叠判定的核心就是第 2 节的公式：区间 \([s,e)\) 与已登记 \([r_s,r_e)\) 冲突当且仅当 \( s < r_e \land r_s < e \)，且完全相同的区间豁免（允许幂等重注册）。由于 `server_regions_`/`client_regions_` 是以地址为 key 的 `std::map`，检查只需与 `lower_bound` 相邻的两个区间比较，复杂度 \( O(\log n) \)。

#### 4.3.3 源码精读

**第 1 段：台账的数据结构。**

[src/hixl/cs/hixl_mem_store.h:19-35](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_mem_store.h#L19-L35)
`MemoryRegion` 记录四个字段：`addr`（起始地址）、`size`、`is_host_mem`、`register_dev_addr`（host 内存注册后得到的设备侧映射地址，非空时有效）。类注释说明了定位：与 client 绑定，记录 client 侧 endpoint 分配的内存和 server 侧分配的内存。

**第 2 段：登记与两侧不同的重复语义。**

[src/hixl/cs/hixl_mem_store.cc:48-70](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_mem_store.cc#L48-L70)
`RecordMemory` 用 `is_server` 区分两张表。server 侧（对端内存导入）重复登记同一地址时幂等返回 `SUCCESS`——因为多条链路可能导入同一块 server 内存；client 侧（本端注册）重复登记则返回 `PARAM_INVALID`——本端内存应当先注销再重注册。这正是 4.1 练习 1 提到的两层语义差异的出处。

**第 3 段：注册前的重叠检查。**

[src/hixl/cs/hixl_mem_store.cc:89-137](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_mem_store.cc#L89-L137)
`CheckMemoryForRegister` 用 `lower_bound(check_addr)` 定位，只与「起点不小于待查地址」及其前一个区间做 `overlaps` 判定（行 113：`is_overlap = (s < re) && (rs < e)`，行 114 完全相同区间豁免）。函数返回 `true` 表示**不允许**注册（重叠），日志会打印与之冲突的已注册区间。

**第 4 段：传输前的访问校验。**

[src/hixl/cs/hixl_mem_store.cc:139-175](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_mem_store.cc#L139-L175)
`CheckMemoryForAccess` 同样二分定位后只查相邻两个区间；若单区间不完全包含，还会走 `CheckMergedRegionsAccess`（行 177-218）：把**地址连续且 register_dev_addr 也连续**的相邻登记区间拼接成一个大区间再判定——这与 `Segment::Contains` 的拼接思想一致，但要求更严格（device 映射地址也必须连续）。

[src/hixl/cs/hixl_mem_store.cc:220-238](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_mem_store.cc#L220-L238)
`ValidateMemoryAccess(server_addr, mem_size, client_addr)` 依次校验 server 端、client 端地址都在各自台账中，任一失败返回 `PARAM_INVALID` 并指明是哪一侧未注册。批量版本 `BatchValidateMemoryAccess`（行 260-281）逐条做同样检查。

**第 5 段：host 地址转换。**

[src/hixl/cs/hixl_mem_store.cc:283-317](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_mem_store.cc#L283-L317)
`ConvertHostAddr` 对每条传输描述分别处理远端与近端：`FindMemoryRegion` 找到所属登记区间后，若 `is_host_mem` 为真，则把传输地址替换为 `register_dev_addr + offset`（offset = 原地址 − 区间起始地址），并累加 host 计数。批量版本在行 319-333。这说明：**用户传给 HIXL 的始终是主机虚拟地址，设备侧地址由引擎内部替换**，用户无需关心映射细节。

**第 6 段：两条登记来源的入口。**

[src/hixl/cs/hixl_cs_client.cc:385-416](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L385-L416)
`HixlCSClient::RegMemLocked`（本端注册路径）：先 `CheckMemoryForRegister` 拒绝重叠，再调 `local_endpoint_->RegisterMem` 落到传输层；若注册的是 host 内存且 endpoint 需要 VA 映射，则通过 `HostRegisterProxy::GetRegisteredDeviceAddrByDev` 查得 `register_dev_addr`，最后 `RecordMemory(false, ...)` 登记进 client 台账。

[src/hixl/cs/hixl_cs_client.cc:156-191](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/src/hixl/cs/hixl_cs_client.cc#L156-L191)
`ImportOneDesc`（对端内存导入路径，建链时被 `ImportAllDescs` 逐条调用）：`ep->MemImport` 把对端导出的内存描述转换为本地可用的 `CommMem`，随后 `RecordMemory(true, ...)` 登记进 server 台账。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：追踪一次「未注册地址的传输被拦截」的完整校验链，理解错误码的来源。
2. **操作步骤**：
   - 从 `HixlMemStore::BatchValidateMemoryAccess`（`hixl_mem_store.cc` 行 260-281）出发，向上用 Grep 找到它的调用者（在 CS 侧传输下发路径中）；
   - 再向下读 `CheckMemoryForAccess` 与 `CheckMergedRegionsAccess`，注意「区间拼接要求 addr 与 register_dev_addr 都连续」（`hixl_mem_store.cc` 行 29-46 的 `CheckRegionsContiguous`）；
   - 整理成一张调用链笔记：`传输入口 → BatchValidateMemoryAccess → CheckMemoryForAccess → (可选) CheckMergedRegionsAccess → PARAM_INVALID`。
3. **需要观察的现象**：校验失败时日志会精确指出是 server 侧还是 client 侧、哪个地址、多长未注册。
4. **预期结果**：得到一张可复用的错误排查流程图——遇到 `PARAM_INVALID` 且日志含 "memory is not registered" 时，先检查对应侧是否调过 `RegisterMem`、区间是否覆盖传输长度。

#### 4.3.5 小练习与答案

**练习 1**：`RecordMemory` 对 server 侧重复登记幂等、对 client 侧重复登记报错，为什么语义要分开？
**答案**：server 侧条目来自「对端内存导入」，多条链路/多次建链可能重复导入同一块对端内存，幂等才不会误伤；client 侧条目来自本端显式 `RegisterMem`，同一地址重复注册通常意味着用户侧 bug（或忘记 `DeregisterMem`），报错 `PARAM_INVALID` 更安全（见 `hixl_mem_store.cc` 行 52-68）。

**练习 2**：注册一块 host 内存后，用户在 `TransferOpDesc` 里填的主机地址，引擎内部会怎么处理？
**答案**：传输下发前 `BatchConvertHostAddr` → `ConvertHostAddr` 会查到该地址所属的 `MemoryRegion`，若 `is_host_mem` 为真，则替换为 `register_dev_addr + (原地址 - 区间起始地址)`，即设备侧映射地址；用户全程只使用主机虚拟地址。

**练习 3**：`CheckMemoryForRegister` 为什么只比较 `lower_bound` 相邻的两个区间就够了？
**答案**：`server_regions_`/`client_regions_` 是以起始地址为 key 的有序 `std::map`。若待查区间与某个已登记区间重叠，那个区间的起点要么是第一个 `>= check_addr` 的区间（它可能覆盖 check_addr 的前段），要么是其前驱（它可能覆盖 check_addr 的后段）；起点更早的其他区间必然整体位于前驱之前，不可能延伸越过前驱起点还与 check_addr 重叠——除非与前驱自身重叠，而那种登记状态本身已被之前的注册检查排除。（严格证明依赖「已登记区间两两不重叠」这一不变量。）

## 5. 综合实践

把本讲三个模块串成一个任务：**基于 quickstart 样例扩展为「双内存类型注册 + 传输对比 + 正确释放」**。

任务描述（示例代码改编思路，改自 `examples/cpp/hixl_example_quickstart.cpp`）：

1. **准备**：在 `EngineCtx` 中增加第二组缓冲字段（`host_buf` / `host_handle` / `host_desc`）。server 侧用 `aclrtMallocHost` 分配一块 `kBufSize` 的锁页内存并填充数据，client 侧同样分配一块接收缓冲。
2. **注册**：两端各自调用两次 `RegisterMem`——device 内存用 `MEM_DEVICE`，host 内存用 `MEM_HOST`。对照 4.1 的调用链，知道每次调用会在 `mem_map_`、`local_segments_`、client 台账各留下一笔登记。
3. **地址交换**：参照样例的 `ExchangeAddr`，把 device 与 host 两个地址都通过 socket 发给 client。
4. **传输对比**：client 建链后分别对两块内存执行 `TransferSync(READ, ...)`：一次 `local_addr`/`remote_addr` 用 device 地址，一次用 host 地址。观察：
   - 两次传输是否都成功（与机器链路配置 `protocol_desc` 有关，若 host 路径失败请对照 u1-l5 的 `hixl_example_d2rh.cpp` 检查链路选项）；
   - 用 `memcmp` 分别校验两块接收缓冲的内容；
   - 若失败，对照 4.3.4 的排查图定位是哪一侧、哪块内存的校验未通过。
5. **正确释放**：严格按顺序执行 `Disconnect` → 两次 `DeregisterMem`（先 device 后 host，或反之均可，但都必须在断链后）→ `aclrtFree`/`aclrtFreeHost` → `engine.Finalize()`。对照 4.1.3 第 3 段理解为什么顺序不能乱。
6. **验证登记行为（选做）**：在断链前尝试 `DeregisterMem`，预期返回失败并看到「All clients must be disconnected」日志；再用同一块内存重复 `RegisterMem`，观察返回的 handle 是否与第一次相同（对应 4.1.5 练习 1）。

运行方式（需两张互通 device，参考 u1-l2/u1-l3）：

```bash
# 终端 1
./hixl_example_quickstart --role=server
# 终端 2
./hixl_example_quickstart --role=client
```

本综合实践在真实昇腾环境的具体输出（尤其 host 内存路径的传输结果）：**待本地验证**。

## 6. 本讲小结

- `RegisterMem(MemDesc, MemType, MemHandle&)` 是零拷贝的授权动作：引擎据此建立登记、同步对端、并在传输前校验地址，`MemHandle` 是仅用于解注册的不透明句柄。
- 调用链五层下沉：`Hixl`（外壳）→ `HixlImpl`（门卫）→ `HixlEngine`（`mem_map_` 登记、断链约束）→ `HixlServer`（溢出/重复检查）→ CS 层 C 接口（真实注册）。
- 解注册有硬约束：必须先 `Disconnect` 所有链路；重复注册同一区间在引擎层幂等返回原 handle。
- `Segment` 是「每类内存一张有序区间表」，支持多区间拼接覆盖判定，供 client handler 查地址类型与选路。
- `HixlMemStore` 是 CS 侧两端台账：server 区间来自建链时的内存导入（幂等），client 区间来自本端注册（禁重叠）；传输前逐条做访问校验，host 内存地址会被替换为 `register_dev_addr + offset` 的设备侧映射地址。
- 顺序合同贯穿始终：**先注册 → 再交换地址 → 再建链 → 再传输 → 先断链 → 再解注册**。

## 7. 下一步学习建议

下一讲 **u2-l4《建链与断链：Connect/Disconnect 及异步版本》** 将接着本讲的「注册信息如何随链路同步」往下讲：`HixlClient` 与 `ClientManager` 如何管理多条远端链路、`ConnectAsync` + `GetAsyncConnectStatus` 的异步状态机。建议提前浏览 `src/hixl/engine/hixl_client.h` 与 `src/hixl/engine/client_manager.h`，并留意其中对内存信息（`MemInfo`）的传递——那正是本讲 `Segment::AddRange` 数据的来源。如果想先了解 CS 台账如何被用于真实传输，可顺带预习 `src/hixl/cs/transfer_pool.cc` 的入口校验调用点（u4-l3 展开）。
