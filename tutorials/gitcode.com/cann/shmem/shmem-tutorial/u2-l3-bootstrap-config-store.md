# Bootstrap 控制面：Config Store 与 TCP 星型拓扑

## 1. 本讲目标

上一讲（u2-l2）我们走读了 `aclshmemx_init_attr` 的三阶段初始化流程，其中第一阶段「Bootstrap 建链」只留下了一句话：*加载插件、建立 CPU 控制面、产出 `g_boot_handle` 上的 allgather/barrier 能力*。本讲就钻进这个黑盒，学完后你应当能够：

1. 说清 Default / UniqueID 模式下的控制面拓扑：**以 PE 0 为中心的 TCP 星型结构**，PE 0 进程内嵌一个 KV（key-value）服务，其余 PE 都是 TCP 客户端。
2. 掌握 bootstrap 插件的 **dlopen 动态加载机制**：`aclshmem_bootstrap_config_store.so` 是如何被发现、加载、并以统一函数指针协议接入主库的。
3. 理解 barrier / allgather 这两个控制面集合通信**是如何只用 6 个 KV 原语（SET/GET/ADD/APPEND/REMOVE/CAS）在一张内存哈希表上实现的**，以及它们在 init / malloc / finalize 中被谁调用。
4. 能对照源码画出 4 个 PE 的建链时序图，并标注 `TcpConfigStore` 与 `AccStoreServer` 各自所在的进程。

## 2. 前置知识

### 2.1 为什么需要一个「控制面」

SHMEM 的数据面（put/get/AMO）走 NPU 上的 RDMA/SDMA/UDMA 引擎，速度快但要求**所有 PE 的地址、rank 信息在建链前就已确定**——引擎自己不会「谈判」。因此在数据面跑起来之前，必须先有一个慢速但通用的通道，让各 PE：

- 互相发现（谁在哪个 IP:端口）；
- 交换对称堆的 slice 信息（u2-l5 的主题）；
- 在关键步骤上对齐进度（barrier）、收集信息（allgather）。

这个通道只跑在 **CPU 和普通 TCP** 上，与 NPU 无关，所以叫 **CPU 控制面**。它只在初始化/分配/finalize 等低频路径上使用，性能不敏感，但**必须绝对可靠**。

### 2.2 KV 表如何模拟集合通信

PE 0 内存里有一张 KV 表（`std::map` 风格），支持远程原子操作。两个经典协议：

- **barrier（栅栏）**：每个 PE 到达后对同一个 key 做原子 `+1`，服务端返回**加完后的值** \(val\)。第 \(val\) 个到达的 PE 知道自己是第几个；当 \(val = n\)（总 PE 数）时，最后到达者写入「完成」标志，其余 PE 自旋 `GET` 这个标志直到出现。
- **allgather（全收集）**：每个 PE 把「自己的编号 + 数据」追加（`APPEND`）到同一个 key，服务端返回追加后的**总长度**；当总长度达到 \((4 + s) \times n\)（4 字节 rank 前缀 + 每人 \(s\) 字节负载）时，最后完成者写「完成」标志，随后所有 PE `GET` 整个 value 并按 rank 排序拆开。

可以看到，集合通信被降解成了**带返回值的单边 KV 操作 + 一个完成标志位**——这是本讲最核心的设计思想。

### 2.3 需要的背景概念

| 术语 | 含义 | 来源 |
|------|------|------|
| PE | 参与通信的一个进程，编号 `my_pe`，总数 `n_pes` | u1-l1 |
| `g_boot_handle` | 主库持有的全局 bootstrap 句柄，内含 barrier/allgather 函数指针 | u2-l2 |
| dlopen/dlsym | Linux 动态库加载 / 查符号接口，运行时把 `.so` 装进本进程 | 本讲 4.1 |
| 插件（plugin） | 独立编译的 bootstrap 实现 `.so`，主库按模式选一个加载 | 本讲 4.1 |
| 控制面 barrier（`aclshmemi_control_barrier_all`） | init/malloc/free/finalize 中对全组 PE 的对齐点 | u2-l2 |

## 3. 本讲源码地图

> 注意：manifest 与部分文档把 `shmemi_bootstrap.cpp` 写在 `src/host/bootstrap/` 下，**实际路径是 `src/host/init/bootstrap/shmemi_bootstrap.cpp`**（加载器属于 init 子系统）；而 config store 插件实现在 `src/host/bootstrap/` 下。以仓库实际文件为准。

| 文件 | 角色 |
|------|------|
| [src/host/init/bootstrap/shmemi_bootstrap.cpp](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/init/bootstrap/shmemi_bootstrap.cpp) | **插件加载器**：按 flags 选插件、dlopen/dlsym、驱动 plugin_init/plugin_pre_init，是主库与插件之间的「桥」 |
| [src/host/bootstrap/shmemi_bootstrap_config_store.cpp](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/shmemi_bootstrap_config_store.cpp) | **Config Store 插件入口**：`plugin_init`/`plugin_pre_init`/barrier/allgather/finalize 回调实现 |
| [src/host/utils/shmemi_host_types.h](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/utils/shmemi_host_types.h) | `aclshmemi_bootstrap_handle_t` 句柄结构定义（插件与主库的共同契约） |
| [src/host/bootstrap/config_store/store_factory.cpp](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/config_store/store_factory.cpp) | `CreateStore` 工厂：构造 `TcpConfigStore` 并启动 |
| [src/host/bootstrap/config_store/store_tcp_config.cpp](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/config_store/store_tcp_config.cpp) | `TcpConfigStore`：每个 PE 都有的 Store 客户端；PE 0 额外内嵌 `AccStoreServer` |
| [src/host/bootstrap/config_store/acc_links/csrc/acc_tcp_server_default.cpp](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/config_store/acc_links/csrc/acc_tcp_server_default.cpp) | 传输引擎 `AccTcpServer`：connect 重试循环与握手（client/server 共用此类） |
| [src/host/bootstrap/config_store/store_net_group_engine.cpp](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/config_store/store_net_group_engine.cpp) | `SmemNetGroupEngine`：KV 上的 `GroupBarrier` / `GroupAllGather` 协议 |
| [docs/principles/config_store_bootstrap.md](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/docs/principles/config_store_bootstrap.md) | 官方详解文档（本讲的姊妹篇，含配图；个别参数与当前代码有出入，见 4.3.4 的提示） |

## 4. 核心概念与源码讲解

### 4.1 Bootstrap 插件机制：dlopen 加载与模式分派

#### 4.1.1 概念说明

SHMEM 有三种 bootstrap 模式（u2-l1），对应两套实现：

| 模式 | 插件 `.so` | 控制面 |
|------|-----------|--------|
| `ACLSHMEMX_INIT_WITH_UNIQUEID` | `aclshmem_bootstrap_config_store.so` | PE 0 的 KV 服务 + TCP 星型 |
| `ACLSHMEMX_INIT_WITH_DEFAULT` | `aclshmem_bootstrap_config_store.so` | 同上（ip_port 优先，UID 回退） |
| `ACLSHMEMX_INIT_WITH_MPI` | `aclshmem_bootstrap_mpi.so` | 直接复用 MPI 的集合通信 |

主库 `libshmem.so` **并不静态链接**这些实现，而是在运行时根据用户传入的 flags `dlopen` 对应插件，再用 `dlsym` 拿到约定名字的入口函数。这样做的好处：

1. **可裁剪**：没有 MPI 环境的部署不需要链接 MPI；
2. **协议解耦**：插件只需实现同一组函数指针（barrier/allgather/finalize...），主库其余代码完全不感知后端差异；
3. **独立演进**：config_store 子目录是一个相当大的自包含网络栈（约 30 个文件），单独成库便于维护。

主库与插件之间的「契约」就是 `aclshmemi_bootstrap_handle_t`——一个装满函数指针和会话参数的结构体，双方都包含 `shmemi_host_types.h`，定义见 [src/host/utils/shmemi_host_types.h:37-67](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/utils/shmemi_host_types.h#L37-L67)：

```cpp
typedef struct aclshmemi_bootstrap_handle {
    void        *bootstrap_state;              // 插件私有状态（config_store 指向 ConfigStoreState）
    aclshmemi_bootstrap_init_ops_t *pre_init_ops;
    int (*finalize)(aclshmemi_bootstrap_handle *boot_handle);
    int (*allgather)(const void *sendbuf, void *recvbuf, int size, aclshmemi_bootstrap_handle *boot_handle);
    int (*barrier)(aclshmemi_bootstrap_handle *boot_handle);
    void (*global_exit)(int status, aclshmemi_bootstrap_handle *boot_handle);
    int32_t     mype;                          // 本 PE 编号
    int32_t     npes;                          // 总 PE 数
    int32_t     sockFd;                        // PE 0 预绑定的监听 fd（无则 -1）
    int32_t     timeOut;                       // 建链超时参数
    int32_t     timeControlOut;                // 控制面操作超时（秒）
    bool use_attr_ipport;                      // Default 模式是否直接用 attr->ip_port
    uint16_t session_magic;                    // 连接魔数，隔离多次 init/finalize
    char ipport[ACLSHMEM_MAX_HANDLE_IP_PORT_LEN]; // "tcp://ip:port" 形式的服务地址
} aclshmemi_bootstrap_handle_t;
```

主库持有它的全局实例 `g_boot_handle`（声明在 [src/host/init/shmemi_init.h:22-24](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/init/shmemi_init.h#L22-L24)）。插件启动时把**自己的回调填进这个句柄**，之后主库调用 `g_boot_handle.barrier(...)` 就等于调用了插件实现——一次朴素的 C 风格多态。

#### 4.1.2 核心流程

`aclshmemi_bootstrap_init(flags, attr)`（由 [src/host/init/shmem_init.cpp:954](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/init/shmem_init.cpp#L954) 在初始化第一阶段调用）的流程：

```text
aclshmemi_bootstrap_init(flags, attr)
  ├─ ① 按 flags 分派插件名 + 校验参数
  │     UNIQUEID → plugin = config_store.so，comm_args 必须是合法 UID（否则报错返回）
  │     MPI      → plugin = bootstrap_mpi.so
  │     DEFAULT  → plugin = config_store.so
  │                ├─ attr->ip_port 合法（tcp://host:port 且 port>1024）→ use_attr_ipport=true，
  │                │   拷贝 sockFd / 超时 / ipport 进句柄
  │                ├─ 否则 comm_args 是合法 UID → 回退 UID 路径
  │                └─ 都不行 → ACLSHMEM_INVALID_PARAM
  ├─ ② 推导 session_magic（UID 模式取 uid.magic 低 16 位；否则用默认 0x0ACC）
  ├─ ③ dlopen 插件（先按主库所在目录拼绝对路径，失败再按名字搜 LD_LIBRARY_PATH）
  ├─ ④ dlsym 取 "aclshmemi_bootstrap_plugin_init" 入口
  └─ ⑤ plugin_init(comm_args, &g_boot_handle) → 插件建链并注册回调
       成功后 g_boot_handle.is_bootstraped = true
```

另有一条 **pre-init** 路径：`aclshmemi_bootstrap_pre_init`（[shmemi_bootstrap.cpp:121-157](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/init/bootstrap/shmemi_bootstrap.cpp#L121-L157)）供 `aclshmemx_get_uniqueid` 在正式 init 之前调用，它 dlsym 的是 `aclshmemi_bootstrap_plugin_pre_init`，让插件提前注册 `get_unique_id` 回调（见 4.2.3 的 `config_store_get_unique_id`）。

#### 4.1.3 源码精读

**插件名与符号名是两份宏约定的协议**——插件侧必须导出同名函数，否则 dlsym 失败：

- [shmemi_bootstrap.cpp:23-27](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/init/bootstrap/shmemi_bootstrap.cpp#L23-L27)：定义 `aclshmem_bootstrap_config_store.so` / `aclshmem_bootstrap_mpi.so` 与入口符号 `aclshmemi_bootstrap_plugin_init` / `..._pre_init`。

- [shmemi_bootstrap.cpp:58-85](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/init/bootstrap/shmemi_bootstrap.cpp#L58-L85)：`safe_dlopen` 先用 `dladdr` 拿到**主库自身**的磁盘路径，切出目录，拼出 `主库目录/插件名.so` 优先加载；失败才退回普通 `dlopen(so_name)` 走系统搜索路径。这保证插件与 `libshmem.so` 总是成套安装、成套加载。

- [shmemi_bootstrap.cpp:284-308](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/init/bootstrap/shmemi_bootstrap.cpp#L284-L308)：Default 分支的三级决策——合法 `ip_port` 直接用；否则用 UID 参数回退（`use_attr_ipport=false`）；两者皆无则返回 `ACLSHMEM_INVALID_PARAM`。其中 `is_valid_ip_port_url`（[L195-260](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/init/bootstrap/shmemi_bootstrap.cpp#L195-L260)）要求 URL 必须是 `tcp://host:port` 或 `tcp6://[host]:port`，端口必须大于 1024 且 host 是合法 IP 或 hostname。

- [shmemi_bootstrap.cpp:319-325](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/init/bootstrap/shmemi_bootstrap.cpp#L319-L325)：从 UID 的 `magic` 字段低 16 位推导 `session_magic`。它的用途是**隔离同一端口上反复 init/finalize 的会话**：上一次会话残留的半开连接会在握手中因 magic 不匹配被拒绝（默认魔数 `SHMEMI_DEFAULT_CONN_MAGIC = 0x0ACC`，定义于 [shmemi_host_types.h:19](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/utils/shmemi_host_types.h#L19)）。

- [shmemi_bootstrap.cpp:334-353](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/init/bootstrap/shmemi_bootstrap.cpp#L334-L353)：`dlsym` 取 `plugin_init` 并调用；任何一步失败都 `safe_dlclose` 回滚并返回错误——这就是 u2-l2 提到的 `init_abort_guard` 本地回滚的起点之一。

#### 4.1.4 代码实践

**实践：验证「插件导出符号 ⇄ 加载器 dlsym 符号」的契约**

1. **实践目标**：亲手确认 `aclshmem_bootstrap_config_store.so` 确实导出了加载器要找的两个符号，理解 dlopen 契约不是纸面约定。
2. **操作步骤**：
   - 在构建产物安装目录中定位插件（`libshmem.so` 所在目录，通常还需 `source install/set_env.sh`，见 u1-l2）；
   - 执行 `nm -D <插件路径> | grep aclshmemi_bootstrap_plugin`；
   - 对照 [shmemi_bootstrap.cpp:26-27](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/init/bootstrap/shmemi_bootstrap.cpp#L26-L27) 的两个宏，确认符号名逐字符一致；
   - 再执行 `nm -D <插件路径> | grep -c ' T '` 感受一下插件导出了多少函数。
3. **需要观察的现象**：`nm -D` 输出中应出现 `aclshmemi_bootstrap_plugin_init` 与 `aclshmemi_bootstrap_plugin_pre_init`（类型字母 `T`，表示代码段全局符号）。
4. **预期结果**：两个符号都能找到；如果在没有 CANN 环境的机器上没有编译产物，可改用纯源码实践——在 [shmemi_bootstrap_config_store.cpp](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/shmemi_bootstrap_config_store.cpp) 中 `grep` 这两个函数名，确认插件侧函数签名与加载器中的 `typedef` 一致。
5. 编译产物相关现象**待本地验证**（依赖 u1-l2 的构建环境）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `safe_dlopen` 要先用 `dladdr` 拼绝对路径，而不是直接 `dlopen("aclshmem_bootstrap_config_store.so")`？

**答案**：直接按名字 dlopen 会走系统搜索路径（`LD_LIBRARY_PATH`、`/usr/lib` 等），可能加载到**另一个版本**的插件，与主库符号布局不匹配；用主库自身路径拼绝对路径保证插件与 `libshmem.so` 来自同一安装目录、同一版本，避免符号错配。只有按绝对路径加载失败时才退回按名搜索（[shmemi_bootstrap.cpp:58-85](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/init/bootstrap/shmemi_bootstrap.cpp#L58-L85)）。

**练习 2**：Default 模式下用户既没填合法 `ip_port`，也没传 UID，会发生什么？

**答案**：`aclshmemi_bootstrap_init` 在 DEFAULT 分支第三级判断处直接返回 `ACLSHMEM_INVALID_PARAM`（[shmemi_bootstrap.cpp:303-308](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/init/bootstrap/shmemi_bootstrap.cpp#L303-L308)），初始化失败，不会尝试建链。

**练习 3**：MPI 模式为什么不加载 config_store 插件？

**答案**：MPI 模式下进程组已经由 `mpirun` 建好，`MPI_Comm` 本身就提供 barrier/allgather，控制面直接复用 MPI 即可（分派见 [shmemi_bootstrap.cpp:280-283](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/init/bootstrap/shmemi_bootstrap.cpp#L280-L283)，插件选择 `aclshmem_bootstrap_mpi.so`），无需再自建 TCP KV 服务。

---

### 4.2 Config Store 插件入口：plugin_init 的四步

#### 4.2.1 概念说明

`aclshmemi_bootstrap_plugin_init`（插件侧入口，实现于 [shmemi_bootstrap_config_store.cpp:276-362](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/shmemi_bootstrap_config_store.cpp#L276-L362)）在每个 PE 上执行，负责把一个空的 `g_boot_handle` 变成「可 barrier、可 allgather」的可用控制面。它维护插件私有状态 `ConfigStoreState`（[L40-43](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/shmemi_bootstrap_config_store.cpp#L40-L43)）：

```cpp
struct ConfigStoreState {
    shm::store::StorePtr store_ = nullptr;            // KV 客户端（PE 0 内含服务端）
    shm::store::SmemGroupEnginePtr group_engine_ = nullptr; // 集合通信引擎
};
```

`handle->bootstrap_state` 指向它，barrier/allgather 回调 later 从这里取回引擎——函数指针 + 私有状态，就是插件的全部记忆。

#### 4.2.2 核心流程

```text
aclshmemi_bootstrap_plugin_init(comm, handle)          [每个 PE 都执行]
  ├─ ① g_store_ref++（多实例引用计数，见 4.5）
  ├─ ② new ConfigStoreState 挂到 handle->bootstrap_state
  ├─ ③ 若 use_attr_ipport == false（UID 路径）：
  │      从 UID 结构体拼出 "tcp://ip:port" / "tcp6://[ip]:port"
  │      handle->sockFd = uid 的 inner_sockFd（PE 0 预绑定的监听 fd）
  │      handle->timeOut = handle->timeControlOut = DEFAULT_TIMEOUT(120)
  ├─ ④ 配置 TLS（默认关闭）→ init_config_store(handle)    → 得到 store_
  ├─ ⑤ init_group_engine(handle)                          → 得到 group_engine_
  └─ ⑥ 注册回调：barrier / allgather / finalize / global_exit（alltoall 置 nullptr）
```

关键点：**无论哪个 PE、无论 IP 来自 attr 还是 UID，走到第 ④ 步时 `handle->ipport` 都指向同一个地址——PE 0 公布的服务地址**。这是星型拓扑的根。

#### 4.2.3 源码精读

- **UID → ipport 的翻译**（[shmemi_bootstrap_config_store.cpp:296-330](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/shmemi_bootstrap_config_store.cpp#L296-L330)）：UID 里的 `addr` 是 `sockaddr_in/in6` 结构，这里用 `inet_ntop` 转字符串，按 IPv4/IPv6 分别拼 `tcp://ip:port` / `tcp6://[ip]:port` 写入 `handle->ipport`，同时继承 `inner_sockFd`（PE 0 生成 UID 时已经 bind 好的监听 socket，见下）与默认超时 `DEFAULT_TIMEOUT = 120`（定义于 [include/host/shmem_host_def.h:32](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/include/host/shmem_host_def.h#L32)）。

- **UID 的生成**（`config_store_get_unique_id`，[L45-73](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/shmemi_bootstrap_config_store.cpp#L45-L73)）：这就是 `aclshmemx_get_uniqueid` 的真身。优先读环境变量 `SHMEM_UID_SESSION_ID`（固定 `host:port`，适合平台调度器统一分配）；否则从网卡取本机 IP（可用 `SHMEM_UID_SOCK_IFNAME` 指定网卡），**并随手 bind 一个可用端口**得到 `inner_sockFd`——所以「PE 0 生成 UID」这个动作同时完成了「PE 0 预占监听端口」，之后 listener 直接在这个 fd 上 `listen`，不存在端口被抢的窗口。生成后还调用 `shmem_get_uid_magic` 填充 magic 字段（[L66-71](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/shmemi_bootstrap_config_store.cpp#L66-L71)），供 4.1 的 `session_magic` 使用。注意：**库只生成 UID，把 UID 广播给各 PE 是用户程序自己的责任**（u2-l1 的标准四步）。

- **pre_init 注册**（`aclshmemi_bootstrap_plugin_pre_init`，[L76-94](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/shmemi_bootstrap_config_store.cpp#L76-L94)）：把 `config_store_get_unique_id` 挂到 `handle->pre_init_ops->get_unique_id`。这一步发生在 dlopen 之后、建链之前——生成 UID 当然不需要网络，但需要插件在场。

- **建 Store**（`init_config_store`，[L221-253](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/shmemi_bootstrap_config_store.cpp#L221-L253)）：先 `ExtractIpPortFromUrl` 把 `handle->ipport` 解析回 ip/port，然后按 `mype` 分岔——**`my_pe == 0` 以 server 身份建 Store，其余以 client 身份建 Store**：

  ```cpp
  if (handle->mype == 0) {
      state->store_ = StoreFactory::CreateStore(option.ip, option.port, /*isServer=*/true, 0, -1, sock_fd, magic);
  } else {
      state->store_ = StoreFactory::CreateStore(option.ip, option.port, /*isServer=*/false, handle->mype, -1, -1, magic);
  }
  ```

- **建组引擎**（`init_group_engine`，[L255-274](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/shmemi_bootstrap_config_store.cpp#L255-L274)）：给 Store 套两层**前缀命名空间**（外层 `SHM_(0)_` 由这里加，内层 `S_` 由引擎自己加，见 4.4.3），再用 `SmemGroupOption{rankSize=npes, rank=mype, timeoutMs=timeControlOut*1000, dynamic=false}` 创建 `SmemNetGroupEngine`。`control_operation_timeout`（秒）在这里 ×1000 变成 barrier/allgather 的毫秒级超时。

- **回调注册**（[L354-358](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/shmemi_bootstrap_config_store.cpp#L354-L358)）：`handle->barrier = config_store_bootstrap_barrier; handle->allgather = ...; handle->finalize = ...; handle->global_exit = ...`。从这一刻起，主库任何 `g_boot_handle.barrier(&g_boot_handle)` 调用都会落到 KV 协议上。

#### 4.2.4 代码实践

**实践：用日志观察 plugin_init 在各 PE 上的分岔**

1. **实践目标**：确认「PE 0 走 server 分支、其余 PE 走 client 分支」不是文档口说，而是运行时可观察的事实。
2. **操作步骤**：
   - 按 [docs/debug/log_debug.md](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/docs/debug/log_debug.md) 开启 INFO 级日志（具体环境变量以该文档为准）；
   - 以 default 模式运行 `examples/init`（`bash examples/init/run.sh`，见 u1-l4）；
   - 在各 PE 输出中 `grep` 以下三行日志：`init_config_store: rank=...`（[shmemi_bootstrap_config_store.cpp:241-243](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/shmemi_bootstrap_config_store.cpp#L241-L243)）、`starting as SERVER` / `connecting as CLIENT`（见 4.3.3）、`pe N: bootstrap plugin initialized successfully`（[L360](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/shmemi_bootstrap_config_store.cpp#L360)）。
3. **需要观察的现象**：只有 rank=0 的进程打印 `isServer=true`；所有 PE 都会打印 `connecting as CLIENT`（含 PE 0 自己）；每个 PE 最终都打印 `bootstrap plugin initialized successfully`。
4. **预期结果**：日志行与源码分支一一对应，即完成一次「源码 ↔ 运行时」互证。
5. 运行输出**待本地验证**（需要昇腾环境）。

#### 4.2.5 小练习与答案

**练习 1**：UID 模式下，PE 3 的 `handle->ipport` 是怎么来的？和 PE 0 的一样吗？

**答案**：一样。用户程序把 PE 0 生成的完整 UID 字节广播给所有 PE，各 PE 经 `aclshmemx_set_attr_uniqueid_args` 放进 `comm_args`；`plugin_init` 中 `use_attr_ipport == false` 分支把 UID 里的 addr 结构翻译成 `tcp://ip:port` 字符串写入 `handle->ipport`（[L296-330](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/shmemi_bootstrap_config_store.cpp#L296-L330)）——来源相同，结果自然相同。

**练习 2**：`init_group_engine` 中 `timeoutMs` 是从哪个用户可见参数推导出来的？

**答案**：`aclshmemx_init_attr_t.option_attr.control_operation_timeout`（秒）。它先被存进 `handle->timeControlOut`，在 `init_group_engine` 中乘 1000 转毫秒填入 `SmemGroupOption.timeoutMs`（[L268-269](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/shmemi_bootstrap_config_store.cpp#L268-L269)），用于 barrier/allgather 中 GET 等待完成标志的超时。

**练习 3**：为什么 `plugin_init` 里要先 `init_config_store` 再 `init_group_engine`，顺序不能反？

**答案**：组引擎是对 Store 的封装（在其上加 key 前缀和集合通信协议），构造 `SmemNetGroupEngine` 需要传入已就绪的 `state->store_`；Store 未建立时引擎无从创建（[L267-270](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/shmemi_bootstrap_config_store.cpp#L267-L270)）。这也是 u2-l2 说的「依赖决定顺序」在插件内部的体现。

---

### 4.3 星型建链：PE 0 的 KV 服务与所有 PE 的 TCP 客户端

#### 4.3.1 概念说明

建链的产物是一个星型拓扑：

```text
            ┌──────┐  TCP   ┌──────┐
            │ PE 1 │ ─────▶ │      │
            └──────┘        │ PE 0 │  AccStoreServer（KV 表 + listener）
            ┌──────┐  TCP   │      │  TcpConfigStore（client，连自己）
            │ PE 2 │ ─────▶ │      │
            └──────┘        └──────┘
            ┌──────┐  TCP      ▲
            │ PE 3 │ ──────────┘
            └──────┘
   每个非 0 PE：只有 TcpConfigStore（纯 client）
```

三个容易混淆的名字先厘清：

| 类名 | 所在进程 | 职责 |
|------|---------|------|
| `TcpConfigStore` | **每个 PE**（含 PE 0） | Store 抽象的实现：对外提供 Set/Get/Add/Append/...；内部持有一个 TCP client 引擎；PE 0 的实例**额外**持有 `AccStoreServer` |
| `AccStoreServer` | **仅 PE 0** | KV 服务端：listener + 内存 KV 表 + 各类消息 handler |
| `AccTcpServer` | 每个 PE | TCP 传输引擎（名字带 server 但 client 也用它），提供 `ConnectToPeerServer`、握手、收发 |

一个反直觉的设计：**PE 0 也是一个 client**。它创建 `AccStoreServer` 起 listener 之后，仍会和其它 PE 一样调用同一个 `ConnectToPeerServer` 连向 `serverIp:serverPort`（通常就是本机地址）。好处是 PE 0 的 KV 读写与其它 PE 走完全相同的代码路径，协议上没有特权分支。

#### 4.3.2 核心流程

每个 PE 上 `CreateStore → TcpConfigStore::Startup` 的统一流程：

```text
StoreFactory::CreateStore(ip, port, isServer, rankId, connMaxRetry, sockFd, magic)
  └─ new TcpConfigStore(...)            // 只存参数，不碰网络
  └─ store->Startup(tlsOption, connMaxRetry)
       ├─ retryMaxTimes = connMaxRetry < 0 ? CONNECT_RETRY_MAX_TIMES(20000) : connMaxRetry
       ├─ accClient_ = AccTcpServer::Create()          // 所有 PE：创建 client 传输引擎
       ├─ [仅 isServer_] accServer_ = AccStoreServer(ip, port, sockFd, magic)
       │                  accServer_->Startup()         // bind + listen，listener 就绪
       ├─ 注册响应/断链回调
       ├─ AccClientStart()                               // 启动 client 引擎
       └─ ConnectToPeerServer(serverIp_, serverPort_,   // 所有 PE（含 PE 0）connect 同一地址
                              connReq{rankId, magic}, retryMaxTimes, accClientLink_)
            └─ 循环：socket → TCP_NODELAY → connect()
               成功 → SO_RCVTIMEO=1800s → Handshake(发送 AccConnReq，等 AccConnResp)
               失败 → usleep(1ms) → 重试，直到 retryMaxTimes 次
```

注意顺序：**PE 0 先把 listener 起起来，再去 connect 自己**。这保证「先有服务后有客户」，PE 0 的自连接不会撞上「服务未就绪」。

#### 4.3.3 源码精读

- **工厂**（[store_factory.cpp:39-67](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/config_store/store_factory.cpp#L39-L67)）：`CreateStore` 构造 `TcpConfigStore`（构造函数只存参数，见 [store_tcp_config.cpp:130-139](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/config_store/store_tcp_config.cpp#L130-L139)），做一次 TLS 选项初始化，然后 `Startup`。失败时通过 `failedReason_` 带回错误码（如端口被占 `SM_RESOURCE_IN_USE`）。

- **Startup 的分岔**（[store_tcp_config.cpp:164-228](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/config_store/store_tcp_config.cpp#L164-L228)）：`isServer_` 为真时先创建并启动 `AccStoreServer`（打印 `Rank 0 starting as SERVER on ip:port`）；随后**无论是否 server**，统一注册回调、启动 client、发起 `ConnectToPeerServer`（打印 `Rank N connecting as CLIENT to ip:port`）。`connReq.rankId` 填自己的 PE 编号、`connReq.magic` 填会话魔数——这是服务端登记连接身份的依据。

- **connect 重试循环**（[acc_tcp_server_default.cpp:541-609](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/config_store/acc_links/csrc/acc_tcp_server_default.cpp#L541-L609)）：每轮 `CreateSocket → setsockopt(TCP_NODELAY) → connect()`；成功则设置 `SO_RCVTIMEO = ACC_LINK_RECV_TIMEOUT`（1800 秒）后进入 `Handshake`；失败 `usleep(1000)`（1ms）再试，最多 `maxRetryTimes` 次。重试的存在是因为各 PE 进程启动有先有后——非 PE 0 可能先于 PE 0 到达，此时只能原地等 listener 出现。

- **请求-响应匹配**（`SendMessageBlocked`，[store_tcp_config.cpp:501-533](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/config_store/store_tcp_config.cpp#L501-L533)）：每次 KV 操作分配单调递增的 `seqNo`，注册等待上下文后 `NonBlockSend`，阻塞到响应按**同一 seqNo** 唤醒。连接断开时 `LinkBrokenHandler` 把所有 pending 请求置失败。这就是「单条 TCP 连接上串行化所有 KV 操作」的同步层。

- **当前代码的一个细节**：[shmemi_bootstrap_config_store.cpp:244-251](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/shmemi_bootstrap_config_store.cpp#L244-L251) 中 PE 0 与非 PE 0 的 `CreateStore` **都传 `connMaxRetry = -1`**，即都用默认上限 `CONNECT_RETRY_MAX_TIMES = 20000` 次（常量见 [store_tcp_config.cpp:21](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/config_store/store_tcp_config.cpp#L21)，在 [L167](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/config_store/store_tcp_config.cpp#L167) 生效；代码注释说明这是有意为之，避免把「秒」误当「次数」用）。

#### 4.3.4 代码实践

**实践：画 4 个 PE 的建链时序图（本讲核心实践任务）**

1. **实践目标**：把 4.3.2 的文字流程落成一张 4-PE 时序图，并正确标注 `TcpConfigStore` 与 `AccStoreServer` 所在的进程。
2. **操作步骤**：
   - 以 [docs/principles/config_store_bootstrap.md](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/docs/principles/config_store_bootstrap.md) 第二节的调用链为纲，对照本讲 4.2.2/4.3.2 逐步推导；
   - 画 4 条垂直生命线（PE 0、PE 1、PE 2、PE 3），按时间从上到下标出：各 PE `plugin_init` → PE 0 `bind+listen`（AccStoreServer）→ 各 PE `ConnectToPeerServer`（含 PE 0 连自己）→ 服务端 accept + 握手（校验 magic）→ 各 PE `init_group_engine`；
   - 用虚线框把「PE 0 进程内的 `TcpConfigStore` + `AccStoreServer`」和「PE 1/2/3 进程内的 `TcpConfigStore`」分别圈出来。
3. **需要观察的现象**（自我检查）：图中应出现 **4 条 TCP 连接**（3 个远端 PE + PE 0 自连）；所有 connect 的目的地址相同；`AccStoreServer` 只出现在 PE 0 的框里。
4. **预期结果**：与下图一致（参考答案）：

```text
   PE 0 进程                              PE 1            PE 2            PE 3
   ─────────────────────────────────────────────────────────────────────────────
   plugin_init
   ├ init_config_store(isServer=true)
   │  └ TcpConfigStore::Startup
   │     ├ AccStoreServer::Startup
   │     │  bind + listen ◆ listener 就绪
   │     ├ AccClientStart
   │     └ ConnectToPeerServer ──┐(连自己, rankId=0)
   │        accept ◀─────────────┤握手(magic 校验)
   │                             │
   │                             │◀─ connect ────── plugin_init
   │        accept ◀─────────────┤握手               ├ init_config_store(isServer=false)
   │                             │                   │  └ TcpConfigStore::Startup
   │                             │◀───────────────── connect ──────────┤
   │        accept ◀─────────────┤握手                │  ...(PE 2、PE 3 同理)
   │                             │                   │
   └ init_group_engine           └ init_group_engine └ ... ────────────┘
      ★ 此后 4 个 PE 在同一张 KV 表上 barrier/allgather
```

5. 图中 accept/握手的先后顺序取决于进程启动顺序（连接完成顺序不确定），这是正常现象；**待本地验证**：可用 `ss -tnp | grep <端口>` 在 init 期间观察 PE 0 的 LISTEN 与 4 条 ESTABLISHED。

> **文档与代码的差异提示**：官方文档 `config_store_bootstrap.md` §五/§10.1 写「非 PE 0 用 `shm_init_timeout` 作为 connect 重试上限、PE 0 固定 60 次、失败 sleep(1)」。**当前代码已改为**：两侧统一传 `-1` 走 `CONNECT_RETRY_MAX_TIMES(20000)`，重试间隔为 `usleep(1ms)`（见 4.3.3 引用的行号）。读文档时请以源码为准——这也印证了 u1-l3 的提醒：文档可能滞后。

#### 4.3.5 小练习与答案

**练习 1**：为什么 PE 0 也要作为 client 连接自己？

**答案**：让 PE 0 的 KV 读写走与其它 PE 完全相同的路径（`SendMessageBlocked` → TCP → 服务端 handler → 响应），协议实现只有一套，没有「本进程特权调用」分支；代价是本机回环上多一条连接，换取实现的一致性与可测试性（见 [store_tcp_config.cpp:204-227](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/config_store/store_tcp_config.cpp#L204-L227)）。

**练习 2**：非 PE 0 的 PE 比 PE 0 先启动会发生什么？

**答案**：它的 `connect()` 会失败（连接拒绝/超时），于是进入 1ms 间隔的重试循环，最多 `CONNECT_RETRY_MAX_TIMES`(20000) 次，等 PE 0 的 listener 就绪后即可连上（[acc_tcp_server_default.cpp:560-605](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/config_store/acc_links/csrc/acc_tcp_server_default.cpp#L560-L605)）。这就是各 PE 启动顺序解耦的机制。

**练习 3**：`session_magic` 在建链的哪一步起作用？

**答案**：握手阶段。client 在 `AccConnReq` 中携带 magic（[store_tcp_config.cpp:215-217](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/config_store/store_tcp_config.cpp#L215-L217)），服务端 accept 后校验，不匹配则拒绝连接。这能隔离同一端口上「上一次 init/finalize 循环残留的旧连接/旧客户端」——它们的 magic 是上一会话的。

---

### 4.4 KV 表上的集合通信：GroupBarrier 与 GroupAllGather

#### 4.4.1 概念说明

建链完成后，控制面的价值全部兑现为两个回调：`handle->barrier` 与 `handle->allgather`（包装函数见 [shmemi_bootstrap_config_store.cpp:184-219](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/shmemi_bootstrap_config_store.cpp#L184-L219) 与 [L163-182](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/shmemi_bootstrap_config_store.cpp#L163-L182)）。它们都由 `SmemNetGroupEngine` 实现，核心技巧是：

1. **每次集合操作用一个唯一的序号 `sn` 命名 key**，避免上一轮 barrier 的残留值污染下一轮（barrier 用自增 `barrierGroupSn_`，allgather 用 `allGatherGroupSn_`）；
2. **用 KV 的返回值当计数器**：`Add` 返回加完的值（第几个到达）、`Append` 返回追加后总长度（已收到多少字节）；
3. **最后到达者负责写完成标志**，其余 PE 用带超时的 `Get` 等这个标志；
4. **首个到达者顺手清理两轮前的旧 key**（`REMOVE_INTERVAL = 2`，定义于 [store_net_group_engine.h:24](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/config_store/store_net_group_engine.h#L24)），防止 KV 表无限膨胀。

key 的完整形态是两层前缀 + 版本/序号 + 后缀，例如 barrier 的到达计数 key：

\[ \text{key} = \underbrace{\text{SHM\_(0)\_}}_{\text{会话/实例层}} \underbrace{\text{S\_}}_{\text{静态组层}} \{\text{groupVersion}\}\_\{\text{sn}\}\_\text{BA} \]

`S_` 表示静态组（bootstrap 用 `dynamic=false` 创建）；`D_` 前缀的动态组（Join/Leave）不属于 bootstrap 路径，本讲不展开。

#### 4.4.2 核心流程

**GroupBarrier**（每个 PE 各执行一次）：

```text
sn = ++barrierGroupSn_
key_arrive = "{ver}_{sn}_BA";  key_done = "{ver}_{sn}_BW"

1. val = ADD(key_arrive, 1)          # val ∈ [1, n]，即本 PE 的到达序号
2. if val == 1 and sn > 2:           # 首个到达者清理两轮前的旧 key
3. if val == n:                      # 最后到达者
       SET(key_done, "ok")
4. GET(key_done, timeout=option_.timeoutMs)   # 全体等待 "ok"
5. 校验取到的值 == "ok"，返回成功；超时则报错
```

**GroupAllGather**（`sendSize` 字节负载从每个 PE 收集到所有 PE）：

```text
sn = ++allGatherGroupSn_
key_data = "{ver}_{sn}_GA";  key_done = "{ver}_{sn}_GW"

1. blob = [rank: u32][sendBuf]        # 4 字节 PE 编号前缀 + 负载
2. val = APPEND(key_data, blob)       # val = 追加后的总字节数
3. if val == (sendSize+4) * n:        # 最后完成者
       SET(key_done, "ok")
4. GET(key_done, timeoutMs) 等待 "ok"
5. whole = GET(key_data)              # 取回整段拼接数据
6. 按 blob 内嵌的 rank 排序，展开到 recvBuf[rank * sendSize]
```

完成条件就是一条不等式：收到总字节 \(val = (s + 4) \times n\)，其中 \(s\) 为每 PE 负载字节数。

#### 4.4.3 源码精读

- **双层前缀**：外层 `SHM_(0)_` 在 `init_group_engine` 添加（[shmemi_bootstrap_config_store.cpp:266-267](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/shmemi_bootstrap_config_store.cpp#L266-L267)）；内层 `S_`/`D_` 在 `SmemNetGroupEngine::Create` 添加（[store_net_group_engine.cpp:65-78](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/config_store/store_net_group_engine.cpp#L65-L78)）。`PrefixStore` 是装饰器：给底层 Store 的所有 key 自动加前缀，不同引擎实例互不踩踏。

- **GroupBarrier 主体**（[store_net_group_engine.cpp:80-134](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/config_store/store_net_group_engine.cpp#L80-L134)）：`Add(addKey, 1, val)` 拿到达序号；`val == size` 时 `Set(waitKey, "ok")`（`SMEM_GROUP_SET_STR` 即字符串 `"ok"`，定义于 [L27](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/config_store/store_net_group_engine.cpp#L27)）；全体 `Get(waitKey, getVal, option_.timeoutMs)` 带超时等待并校验。注意 **`Get` 在服务端是可阻塞的**——key 不存在时请求会挂起直到该 key 被 SET 或超时，所以「等待完成标志」不需要客户端自旋轮询。

- **旧 key 清理**（[L100-108](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/config_store/store_net_group_engine.cpp#L100-L108)）：仅 `val == 1`（首个到达者）且 `sn > REMOVE_INTERVAL` 时，删除两轮前的 `_BA`/`_BW`。滞后两轮而不是当轮删，是因为当轮可能还有 PE 没完成 `Get`。

- **GroupAllGather 主体**（[store_net_group_engine.cpp:207-280](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/config_store/store_net_group_engine.cpp#L207-L280)）：`GatherFillRank` 把 4 字节 rank 写进 blob 头部（[L136-140](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/config_store/store_net_group_engine.cpp#L136-L140)）；`Append` 返回累计长度；`val == input.size() * size` 判完成；最后 `SortGatherRecv`（[L142-157](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/config_store/store_net_group_engine.cpp#L142-L157)）按每段内嵌 rank **排序**后展开——因为 APPEND 的到达顺序是任意的，必须靠数据自带的 rank 还原次序。

- **谁来调用它们**：主库侧的封装是 `aclshmemi_control_barrier_all()`，一行代码直通插件（[src/host/init/backends/shmem_init_backend.cpp:885](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/init/backends/shmem_init_backend.cpp#L885)）：

  ```cpp
  int aclshmemi_init_backend::aclshmemi_control_barrier_all() { return g_boot_handle.barrier(&g_boot_handle); }
  ```

  它在 [shmem_init.cpp](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/init/shmem_init.cpp) 中被调用的位置串起了整个生命周期：init 建堆后（L1025 附近，见 [L1025](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/init/shmem_init.cpp#L1025)）、`aclshmem_malloc`/`free` 返回前（L1078 附近）、finalize 拆堆前（[L1125-1126](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/init/shmem_init.cpp#L1125-L1126)）。这就是 u2-l2 说的「malloc 内含控制面 barrier」的落点，也是 u2-l4「各 PE 必须同序同大小分配」的机制保证。

  另外 `config_store_bootstrap_barrier` 在 `npes == 1` 时直接返回（[shmemi_bootstrap_config_store.cpp:198-201](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/shmemi_bootstrap_config_store.cpp#L198-L201)）——单 PE 无需网络同步。

#### 4.4.4 代码实践

**实践：手工模拟一次 4-PE GroupBarrier**

1. **实践目标**：不依赖 NPU，用纸面推演彻底吃透 barrier 协议中「到达序号」与「完成标志」的配合。
2. **操作步骤**：
   - 设 `n_pes = 4`、`groupVersion = 0`，假设到达顺序为 PE 2 → PE 0 → PE 3 → PE 1；
   - 对每个 PE 按到达顺序填写下表：`ADD("0_1_BA",1)` 的返回值 `val`、此时是否触发 `SET("0_1_BW","ok")`、`GET("0_1_BW")` 何时返回；
   - 再推演第 3 轮（`sn=3`）时首个到达者会清理哪两个 key（提示：`REMOVE_INTERVAL=2`）；
   - 最后对照 [store_net_group_engine.cpp:80-134](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/config_store/store_net_group_engine.cpp#L80-L134) 检查每一步。
3. **需要观察的现象**：前 3 个到达的 PE 会「卡」在 `GET`，直到第 4 个（PE 1）的 `val == 4` 触发 `SET` 才集体放行。
4. **预期结果**（参考答案，第 1 轮）：

   | 到达顺序 | PE | `val` | 动作 |
   |---|---|---|---|
   | 1 | PE 2 | 1 | 清理逻辑不触发（`sn=1 ≤ 2`）；阻塞在 `GET` |
   | 2 | PE 0 | 2 | 阻塞在 `GET` |
   | 3 | PE 3 | 3 | 阻塞在 `GET` |
   | 4 | PE 1 | 4 | `SET("0_1_BW","ok")`，随后自己的 `GET` 立即返回 |

   第 3 轮（`sn=3`）首个到达者清理 `0_1_BA` 与 `0_1_BW`。

5. 本实践为纯推演，无需运行环境；若想验证，可在有环境机器上开启日志后观察 `groupBarrier successfully, key: ...`（[store_net_group_engine.cpp:130](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/config_store/store_net_group_engine.cpp#L130)），日志中的 key 编号会随 barrier 轮次递增。

#### 4.4.5 小练习与答案

**练习 1**：如果某个 PE 在 barrier 中途崩溃，其余 PE 会怎样？

**答案**：崩溃的 PE 不会再 `ADD`，`val` 永远到不了 `n`，无人 `SET` 完成标志；其余 PE 的 `GET` 会等到 `option_.timeoutMs`（即 `control_operation_timeout × 1000` 毫秒）超时后返回失败，初始化/ finalize 报错退出。这也是 u1-l4「缺一个 PE 其余进程阻塞至超时」的底层机制。

**练习 2**：allgather 中为什么每个 PE 的数据前要附 4 字节 rank？直接按到达顺序展开不行吗？

**答案**：不行。TCP 连接独立、服务端处理并发到达，`APPEND` 的先后顺序与 PE 编号无关、每次运行都可能不同；不内嵌 rank 就无法知道某段数据属于谁。`SortGatherRecv` 正是按 blob 头部的 rank 排序后按 `recvBuf + preSize * i` 摆放（[store_net_group_engine.cpp:142-157](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/config_store/store_net_group_engine.cpp#L142-L157)）。

**练习 3**：为什么清理旧 key 要滞后两轮（`REMOVE_INTERVAL = 2`）而不是当轮立即删？

**答案**：当轮 barrier 中慢的 PE 可能还没执行完自己的 `GET`，当轮删会破坏其等待；滞后两轮时，可以认为所有 PE 早已离开那轮操作（每轮 barrier/allgather 本身就是全组对齐点），删除是安全的（[store_net_group_engine.cpp:99-108](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/config_store/store_net_group_engine.cpp#L99-L108)）。

---

### 4.5 Finalize 与多实例引用计数

#### 4.5.1 概念说明

控制面的拆除同样是对称的：各 PE 在 finalize 中先释放堆等本地资源，经一次控制面 barrier 对齐（确保没人还要用控制面），最后才拆 bootstrap（调用位置见 [shmem_init.cpp:1125-1126](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/init/shmem_init.cpp#L1125-L1126)）。插件侧的 `config_store_bootstrap_finalize` 要解决一个多实例问题：一个进程里可以先后（或嵌套）创建多个 SHMEM 实例（u8-l1），它们**共享** `StoreFactory` 的全局 TLS/线程资源——只有最后一个实例才能做全局清理。

#### 4.5.2 核心流程

```text
config_store_bootstrap_finalize(handle)
  ├─ g_store_ref--                            # 与 plugin_init 的 ++ 配对
  ├─ 释放 pre_init_ops
  ├─ 若 state->store_ != nullptr 且 g_store_ref == 0：
  │      StoreFactory::DestroyStore()          # 最后一个实例：全局清理（TLS 线程等）
  │    否则：跳过全局清理（打印 ref 计数）
  └─ group_engine_ = nullptr; store_ = nullptr; delete state

主库侧：aclshmemi_bootstrap_finalize()
  └─ handle->finalize(&g_boot_handle) → 上述函数；随后 dlclose 插件
```

#### 4.5.3 源码精读

- **引用计数**（[shmemi_bootstrap_config_store.cpp:36-38](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/shmemi_bootstrap_config_store.cpp#L36-L38)）：`g_store_ref` 是静态计数，`plugin_init` 加一（[L279](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/shmemi_bootstrap_config_store.cpp#L279)）、finalize 减一（[L131](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/shmemi_bootstrap_config_store.cpp#L131)），全程持 `g_store_mutex`。

- **条件全局清理**（[L146-158](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/shmemi_bootstrap_config_store.cpp#L146-L158)）：只有计数归零才 `DestroyStore`，否则只打印 `Store ref count is N, Skip Global DestroyStore`；随后释放本实例的引擎、Store 引用与 `ConfigStoreState`。

- **加载器侧收尾**（[shmemi_bootstrap.cpp:358-365](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/init/bootstrap/shmemi_bootstrap.cpp#L358-L365)）：`aclshmemi_bootstrap_finalize` 调用句柄上的 `finalize` 回调，置 `is_bootstraped = false`，并 `dlclose` 插件——与 4.1 的 dlopen 严格对称。

#### 4.5.4 代码实践

**实践：跟踪 finalize 的对称性**

1. **实践目标**：验证「plugin_init 计数 +1 / finalize 计数 -1 / dlclose」三者的对称关系。
2. **操作步骤**：
   - 通读 [shmemi_bootstrap.cpp:327-355](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/init/bootstrap/shmemi_bootstrap.cpp#L327-L355)（dlopen + plugin_init）与 [L358-365](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/init/bootstrap/shmemi_bootstrap.cpp#L358-L365)（finalize + dlclose），列出每条错误路径是否都正确 `safe_dlclose`；
   - 再列出 `g_store_ref` 的所有增减点（提示：各只有一处，均在持锁区间）。
3. **需要观察的现象**：错误路径（dlsym 失败、plugin_init 失败）都会先 `aclshmemi_bootstrap_free()` 关闭句柄再返回错误；正常路径由 finalize 关闭。
4. **预期结果**：得到一张「资源 ↔ 获取点 ↔ 释放点」对照表，任何一行都不缺释放点。
5. 无需运行环境，纯源码阅读即可完成。

#### 4.5.5 小练习与答案

**练习 1**：进程内两个 SHMEM 实例先后 init/finalize，第一个 finalize 时会执行 `DestroyStore` 吗？

**答案**：不会。第一个 finalize 后 `g_store_ref` 从 2 减到 1，非零则跳过全局清理，只释放该实例自己的 `ConfigStoreState` 与引擎/Store 引用；第二个实例 finalize 时计数归零才做全局清理（[shmemi_bootstrap_config_store.cpp:146-158](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/shmemi_bootstrap_config_store.cpp#L146-L158)）。

**练习 2**：为什么 finalize 拆 bootstrap 之前必须先做一次控制面 barrier？

**答案**：barrier 保证全体 PE 都已进入 finalize、都不再使用控制面之后，PE 0 才能安全关闭 listener；否则先到的 PE 关闭服务会让后到的 PE 的 KV 请求全部失败（调用位置 [shmem_init.cpp:1125-1126](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/init/shmem_init.cpp#L1125-L1126)，机制细节见 [docs/principles/config_store_bootstrap.md](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/docs/principles/config_store_bootstrap.md) 第十一节）。

## 5. 综合实践

**任务：给 4-PE 建链过程做一次「源码级全链路导览」并产出三份工件。**

在完成本讲各模块实践的基础上，把整条链串起来：

1. **时序图**（4.3.4 的产出）：Config Store 模式下 4 个 PE 从 `aclshmemx_init_attr` 进入、到 `init_group_engine` 完成的完整时序图，进程分列，标注：
   - `TcpConfigStore`：**4 个 PE 进程各一个**；
   - `AccStoreServer`（含 KV 表与 listener）：**仅 PE 0 进程**；
   - 4 条 TCP 连接的方向与统一的目的地址、握手时的 magic 校验。
2. **KV 协议推演表**（4.4.4 的产出）：init 收尾那次 `control_barrier_all` 在 4 个 PE 上的 ADD 返回值与放行时刻，以及随后第 3 轮 barrier 触发的旧 key 清理。
3. **对照检验**：在有昇腾环境时运行 `examples/init`（4 PE，default 或 uniqueid 模式），开启日志，把以下日志行按时间排序贴到时序图上对应位置：
   - `init_config_store: rank=.../... ip=... port=... magic=... isServer=...`
   - `Rank 0 starting as SERVER on ...` / `Rank N connecting as CLIENT to ...`
   - `pe N: bootstrap plugin initialized successfully`
   - `groupBarrier successfully, key: SHM_(0)_S_..._BW, size: 4, ...`

   若日志中 `groupBarrier` 的 key 编号随初始化阶段推进而递增（如 `_1_`、`_2_`），说明 barrier 轮次与 u2-l2 的三阶段对上了——把每个编号标注到 init 流程图的对应阶段。

验收标准：不看讲义，能向别人讲清「一条 barrier 请求从 `g_boot_handle.barrier` 出发，经过哪几个类、哪条 TCP 连接、哪张 KV 表、哪个 key，最后如何返回」。运行部分**待本地验证**。

## 6. 本讲小结

- Default/UniqueID 模式的控制面是**以 PE 0 为中心的 TCP 星型拓扑**：PE 0 进程内嵌 `AccStoreServer`（listener + 内存 KV 表），**每个 PE（含 PE 0）**都有一个 `TcpConfigStore` 客户端，通过同一个 `ConnectToPeerServer` 连向同一地址。
- 主库与 bootstrap 实现通过 **dlopen/dlsym 插件机制**解耦：按 flags 选择 `aclshmem_bootstrap_config_store.so` 或 `aclshmem_bootstrap_mpi.so`，以 `aclshmemi_bootstrap_handle_t` 中的函数指针为契约（barrier/allgather/finalize/global_exit）。
- `plugin_init` 四步走：UID→ipport 翻译 → `init_config_store`（PE 0 建 server、其余建 client）→ `init_group_engine`（前缀 `SHM_(0)_` + `S_`）→ 注册回调；`session_magic` 从 UID 推导，用于隔离多次 init/finalize 会话。
- **barrier = ADD 计数 + 最后到达者 SET 完成标志 + 全体带超时 GET**；**allgather = 带(rank: u32)前缀的 APPEND + 总长度判完成 + 取回排序展开**；KV 原语的返回值就是集合通信的同步信号。
- 这些集合通信经 `aclshmemi_control_barrier_all()` 被 init 建堆、`aclshmem_malloc/free`、finalize 复用——这是「各 PE 同序同大小分配」与「对称集体调用」的机制根源。
- finalize 与 plugin_init 严格对称：`g_store_ref` 引用计数保护多实例共享的全局 Store 资源，归零才全局清理，最后 dlclose 插件。
- 官方文档 `config_store_bootstrap.md` 的个别参数（connect 重试上限/间隔）与当前代码有出入，**以源码为准**。

## 7. 下一步学习建议

控制面就绪后，下一讲（u2-l4）回到主线：**对称内存堆 API**——`aclshmem_malloc` 如何在控制面 barrier 的保护下保证各 PE 堆偏移一致，`heap_base` 与 `mem_type` 的语义。再往后（u2-l5）深入 HYBM 堆内部，看 `exchange_slice` 如何**使用本讲的 allgather** 交换各 rank 的内存段信息——那正是本讲 KV 协议最重要的消费者。若想先横向扩展，可阅读 [docs/principles/config_store_bootstrap.md](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/docs/principles/config_store_bootstrap.md) 的第八节（KV 消息帧格式与 SET/GET/ADD/APPEND/REMOVE/CAS 的完整语义）和 [src/host/bootstrap/config_store/store_tcp_config_server.cpp](https://github.com/gitcode.com/cann/shmem/blob/7ed686e5da60eb6008639916adba0f835cdbf4a6/src/host/bootstrap/config_store/store_tcp_config_server.cpp) 的服务端 handler，理解 `Get` 的服务端阻塞等待是如何用 timer 线程实现的。
