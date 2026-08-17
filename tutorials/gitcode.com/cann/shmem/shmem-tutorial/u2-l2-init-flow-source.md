# 初始化全流程源码走读

> 本讲是第 2 单元「核心概念：初始化与对称内存」的第 2 讲。上一讲（u2-l1）我们站在**使用者视角**看懂了 `aclshmemx_init_attr` 的参数与三种 Bootstrap 模式；本讲钻进这个函数内部，沿源码把初始化拆成 **Bootstrap 建链 → HYBM 建堆 → 子模块就绪** 三个阶段，并走读 finalize 的逆序释放过程，最后梳理 **QP 配置（ROCE/UDMA）在实例存活期间冻结、最后一个实例 finalize 后复位** 的生命周期。

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `aclshmemx_init_attr` 内部三个阶段的**先后顺序**，以及为什么必须是这个顺序（Bootstrap 是控制面、建堆要在控制面之上交换元数据、子模块依赖堆）。
2. 在 `src/host/init/shmem_init.cpp` 中**准确定位**每个阶段的函数入口和调用行号。
3. 解释 finalize 为什么按 init 的**逆序**释放资源，以及两道 `aclshmemi_control_barrier_all` 分别卡在哪两个关键位置。
4. 读懂 `g_state`、`init_manager`、`g_boot_handle` 这几个全局变量在初始化中的角色。
5. 描述 `g_udma_qp_config` / `g_rdma_qp_config` 的完整生命周期：何时通过 `aclshmemx_set_qp_num` 配置、何时在 init 成功后被冻结、何时随 `bind_aclshmem_entity` 下发到传输层、何时在最后一个实例 finalize 后复位。

## 2. 前置知识

本讲默认你已读过 u1-l4（init 示例）和 u2-l1（初始化 API 与三种 Bootstrap 模式）。在此基础上补充几个源码阅读需要的基础概念：

- **控制面 vs 数据面**：控制面是 CPU 进程之间的 TCP/MPI 连接，只在 init/finalize 和内部同步时使用；数据面是 NPU 之间的通信引擎（MTE/RDMA/SDMA/UDMA）。初始化的经典思路是「**先用控制面把数据面的路修好**」——本讲的三阶段正是这个思路的落地。
- **对称调用（集体操作）**：init/finalize 必须所有 PE 都参与。这解释了为什么初始化流程里到处出现 `allgather` 和 `barrier`——任何一个 PE 掉队，其他 PE 就会卡在集合操作上直至超时（默认 120 秒）。
- **全局状态 `g_state`**：SHMEM 库用一个全局结构体 `aclshmem_device_host_state_t`（实例名 `g_state`）保存 my_pe、堆基址、team 池等所有关键信息，几乎所有接口都直接读它。它是理解 init 流程的「主线」。
- **QP（Queue Pair，队列对）**：RDMA/UDMA 类引擎每条连接由一对收发队列构成。为单个 peer 建多条 QP 可以让多个传输并行占用网卡资源、提升带宽。QP 数是**进程级**配置：库用两个全局变量（`g_udma_qp_config` / `g_rdma_qp_config`）保存，init 成功后被「冻结」，最后一个实例 finalize 后才复位。本讲只讲它的生命周期，多 QP 建链细节留给 u5-l7。
- **scope guard（作用域守卫）**：C++ 惯用法，构造时注册一个回调，出了作用域自动执行（除非显式 `release()`）。SHMEM 用它实现「初始化中途失败时回滚已申请的资源」，本讲会碰到两个：`ctx_guard` 和 `init_abort_guard`。
- **引用计数共享**：多实例场景下 `init_manager`（init 后端对象）被所有实例共享，用一个计数 `g_init_manager_count` 管理，归零才真正销毁。单实例（`instance_id=0`）时这个计数恒为 1，可以暂时忽略多实例细节（u8-l1 专讲）。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| `src/host/init/shmem_init.cpp` | init/finalize 主实现，**本讲主角** | `aclshmemi_init_attr_impl`（三阶段）、`aclshmemi_finalize_impl`（逆序释放）、`aclshmemx_set_qp_num`（QP 配置）、全局状态定义 |
| `src/host/init/shmemi_init.h` | 内部头文件，导出全局变量与内部函数声明 | `g_state`、`init_manager` 等 extern 声明，看清「谁被谁引用」 |
| `src/host/init/backends/shmemi_init_backend.h` | init 后端类 `aclshmemi_init_backend` 的接口 | 建堆四件套 `reserve_heap`/`setup_heap`/`remove_heap`/`release_heap` 的声明位置；`entity_member` 中的 QP 配置字段 |
| `src/host/init/backends/shmem_init_backend.cpp` | init 后端实现 | `bind_aclshmem_entity` 如何把 QP 配置存入 entity；`create_entity` 如何传给传输层 |
| `src/host/transport/transport_def.h` | 传输层公共定义 | `TransportOptions` 及内嵌的 `RdmaQpConfig` / `UdmaQpConfig` |
| `src/host/init/bootstrap/shmemi_bootstrap.cpp` | Bootstrap 插件加载 | `aclshmemi_bootstrap_init` / `aclshmemi_bootstrap_finalize` 入口（细节下一讲 u2-l3 展开） |
| `src/host/mem/shmem_mm.cpp` | 对称堆分配器 | `memory_manager_initialize`：堆建好后分配器如何就位 |
| `src/host/team/shmem_team.cpp` / `src/host/sync/shmemi_sync.cpp` | team 与同步子模块 | `aclshmemi_team_init` / `aclshmemi_sync_init` 被调用的位置 |
| `docs/principles/init_finalize.md` | 官方流程文档 | 与源码互相印证，含流程配图 |

## 4. 核心概念与源码讲解

### 4.1 初始化总体骨架：一个约 150 行的函数统治一切

#### 4.1.1 概念说明

对外 API `aclshmemx_init_attr` 只是一个薄壳，真正的工作全部在静态函数 `aclshmemi_init_attr_impl` 里（约 150 行）。这个函数把三阶段线性串联：

1. **阶段一 Bootstrap**：加载 bootstrap 插件、建立 PE 之间的 CPU 控制面连接，之后才能做集体通信。
2. **阶段二 HYBM 建堆**：创建 init 后端 `init_manager`，经 `reserve_heap`（预留虚拟地址）→ `setup_heap`（分配物理内存并跨 PE 交换、mmap）建好对称堆；建堆时顺带把 QP 配置绑定到后端实体。
3. **阶段三子模块就绪**：在堆上初始化分配器、signal、team、sync 等子模块，把 `g_state` 同步到 device 元数据区，最后控制面 barrier 对齐后宣布初始化成功，并**冻结 QP 配置**。

顺序不可颠倒的原因很直觉：**建堆时的 slice 交换要靠 Bootstrap 的 allgather/barrier**（阶段二依赖阶段一）；**子模块要在堆上分配自己的控制对象**（阶段三依赖阶段二）。

#### 4.1.2 核心流程

```text
aclshmemx_init_attr(bootstrap_flags, attributes)          ← 对外入口（互斥锁保护）
  └─ aclshmemi_init_attr_impl(...)
       ├─ [0] 多实例上下文 create/set（instance_id != 0 时生效）
       ├─ [0'] 注册 init_abort_guard（失败回滚）
       ├─ [1] 参数校验：init_status 须为 NOT_INITIALIZED → check_attr → version_compatible
       ├─ [2] ===== 阶段一：Bootstrap =====
       │      aclshmemi_bootstrap_init()   → g_boot_handle 就绪
       │      aclshmemi_state_init_attr()  → 填 g_state.mype/npes/heap_size，创建 default_stream
       ├─ [3] new aclshmemi_init_backend() （g_init_manager_count++）
       ├─ [4] ===== 阶段二：HYBM 建堆 =====
       │      bind_aclshmem_entity（携带 g_udma_qp_config 与 g_rdma_qp_config.qpNum）
       │      → init_device_state
       │      → reserve_heap → setup_heap（g_state.heap_base 就位）
       ├─ [5] ===== 阶段三：子模块就绪 =====
       │      memory_manager_initialize →（可选 host 侧 reserve_heap）
       │      → signal_init → team_init → sync_init
       │      → is_aclshmem_initialized = true
       │      → prof_util_init → update_device_state
       ├─ [6] aclshmemi_control_barrier_all()  ← 全体 PE 对齐后才返回成功
       └─ [7] g_qp_config_frozen = true        ← QP 配置冻结，实例存活期间不可再改
```

用一张表对照三个阶段与源码位置（本讲后续小节逐一展开）：

| 阶段 | 关键调用 | 所在源码行（shmem_init.cpp） |
|------|----------|------------------------------|
| 前置校验 | `check_attr` 等 | L953-L960 |
| 一：Bootstrap | `aclshmemi_bootstrap_init` | L963 |
| 一：状态初始化 | `aclshmemi_state_init_attr` | L991 |
| 二：后端创建 | `new aclshmemi_init_backend` | L996-L1000 |
| 二：建堆 | `bind_aclshmem_entity`（含 QP 配置）→ `init_device_state` → `reserve_heap` → `setup_heap` | L1003-L1019 |
| 三：子模块 | `memory_manager_initialize` / `signal_init` / `team_init` / `sync_init` | L1022-L1031 |
| 收尾 | `update_device_state` → `aclshmemi_control_barrier_all` → 冻结 QP 配置 | L1034-L1039 |

#### 4.1.3 源码精读

先看对外入口如何转调内部实现——`aclshmemx_init_attr` 是单行转发，`aclshmemx_init_attr_with_buffers`（user buffer heap 模式，u8-l3 详讲）也走同一个 impl，仅 `InitHeapMode` 不同：

- [src/host/init/shmem_init.cpp:1045-1048](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L1045-L1048)：`aclshmemx_init_attr` 以 `DEFAULT_HEAP` 模式转发到 `aclshmemi_init_attr_impl`，所有真实逻辑都在 impl 里。

impl 的开头做三件事：加互斥锁、创建/切换实例上下文、注册失败回滚守卫：

- [src/host/init/shmem_init.cpp:899-914](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L899-L914)：`g_aclshmem_ctx_mutex` 上锁保证单线程进入；`aclshmemi_instance_ctx_create` + `aclshmemx_instance_ctx_set_impl` 处理多实例（单实例 id=0 时前者直接返回成功）；`ctx_guard` 保证新建实例失败时销毁刚创建的上下文。

- [src/host/init/shmem_init.cpp:916-950](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L916-L950)：`init_abort_guard` 是本函数的「安全网」——一组布尔标记（`bootstrap_initialized`、`heap_reserved` 等）记录推进到哪一步，若最终 `init_succeeded` 仍为 false，就**按依赖关系逆序**释放已占用的资源（先拆堆、再拆 bootstrap、最后减引用计数）。注意它特意打印 "releasing partial resources without synchronization"：中途失败时**不做**跨 PE 同步，避免一个 PE 失败把其他 PE 全拖死在 barrier 上。

理解流程前还要认识几个主角。全局变量都定义在 shmem_init.cpp 顶部，通过 `shmemi_init.h` 导出给其他编译单元：

- [src/host/init/shmem_init.cpp:87-105](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L87-L105)：`g_state`（host/device 共享状态：mype、npes、heap_base、team_pools…）、`g_state_host`（纯 host 侧状态，含 default_stream）、`g_boot_handle`（Bootstrap 句柄，携带 barrier/allgather 函数指针）、`init_manager`（init 后端）与引用计数 `g_init_manager_count`；紧随其后的就是 QP 配置三件套——`g_udma_qp_config`、`g_rdma_qp_config`（本轮新增）与冻结标志 `g_qp_config_frozen`。

- [src/host/init/shmemi_init.h:18-36](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmemi_init.h#L18-L36)：extern 声明上述全局变量（QP 配置是 static，**不**导出——它只属于 init 模块内部），以及 `update_device_state`、`aclshmemi_control_barrier_all` 等内部函数。读大型 C++ 项目时，这类内部头文件就是「模块间契约清单」。

入口附近还有一个容易忽略的防重入检查：

- [src/host/init/shmem_init.cpp:953-960](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L953-L960)：先经 `aclshmemx_init_status()` 确认当前状态是 `NOT_INITIALIZED`（禁止重复 init），再设日志级别、`check_attr` 校验参数（my_pe/n_pes 范围、local_mem_size、超时非零、引擎位掩码合法）、`version_compatible` 检版本。

- [src/host/init/shmem_init.cpp:586-596](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L586-L596)：`aclshmemx_init_status` 用 `is_aclshmem_created` / `is_aclshmem_initialized` 两个布尔区分三态：未初始化 / 堆已建（中间态）/ 完全可用。配合 u2-l1 讲过的 `SHM_CREATED` 中间态语义理解。

#### 4.1.4 代码实践

**实践目标**：亲手把三阶段的调用位置在源码里「钉」出来，形成肌肉记忆。

**操作步骤**：

1. 打开 `src/host/init/shmem_init.cpp`，定位 `aclshmemi_init_attr_impl`（L895 起）。
2. 用编辑器书签或纸笔，在以下 5 个位置做标记：
   - `aclshmemi_bootstrap_init`（L963）
   - `init_manager->reserve_heap()`（L1012）
   - `init_manager->setup_heap()`（L1019）
   - `memory_manager_initialize`（L1023）
   - `aclshmemi_control_barrier_all()`（L1035）
3. 在每个标记旁用一句话注释该步骤「依赖前一步的什么产出」，例如 bootstrap 的产出是 `g_boot_handle` 上的 allgather/barrier 能力。

**需要观察的现象**：标记完成后你会发现三个阶段之间没有任何「空闲地带」——每一步都直接消费上一步的产出，这正是线性三阶段设计的体现。

**预期结果**：得到一张与 4.1.2 流程图对应的「行号标注表」。此实践纯源码阅读，无需运行环境。

#### 4.1.5 小练习与答案

**练习 1**：如果在 `aclshmemx_init_attr` 之前已经成功初始化过一次（且未 finalize），再次调用会发生什么？

**答案**：L953-L955 的 `ACLSHMEM_CHECK_RET(aclshmemx_init_status() != ACLSHMEM_STATUS_NOT_INITIALIZED, ...)` 会命中——`is_aclshmem_created` 已为 true，状态不再是 `NOT_INITIALIZED`，函数带 `ACLSHMEM_INNER_ERROR` 返回，并提示 "do not call init interface repeatedly"。这也是 `init_abort_guard` 会顺手把 `is_aclshmem_initialized` 复位为 false 的原因之一（失败路径清理状态）。

**练习 2**：`init_abort_guard` 的回滚为什么强调「without synchronization」？

**答案**：初始化是集体操作，若某个 PE 失败后还去控制面 barrier 等其他 PE，而其他 PE 可能正常推进甚至成功，失败方就会卡到超时。所以失败路径只做**本地**资源回收（拆堆、卸 bootstrap、减计数），不做跨 PE 同步；官方文档也要求所有 PE 对称调用，避免走到这条路径。

**练习 3**：`g_state` 和 `g_state_host` 为什么要分成两个结构体？

**答案**：`g_state`（`aclshmem_device_host_state_t`）是 host/device **共享**的状态，最终会被 `update_device_state` 拷贝到 NPU 元数据区供算子读取，字段布局必须对 device 可见且版本受控（开头的 version 字段）；`g_state_host`（default_stream 等）是纯 CPU 侧运行时状态，device 不需要。分体避免把 host 私有字段泄漏进 device 镜像。

### 4.2 阶段一：Bootstrap 建链与本地状态初始化

#### 4.2.1 概念说明

Bootstrap 解决的问题是：**初始化开始时，各 PE 之间互不相识**——不知道彼此地址，没有任何通信手段。u2-l1 讲过三种模式（DEFAULT/MPI/UNIQUEID），它们最终都收敛到同一个入口 `aclshmemi_bootstrap_init`：按 flags 选择插件（Config Store 或 MPI）、`dlopen` 加载、拿到一组函数指针（`barrier`、`allgather`、`global_exit`…）填进 `g_boot_handle`。从此以后，**init 流程中所有跨 PE 协作都通过 `g_boot_handle` 完成**，直到 finalize 才拆除。

阶段一还包括一个本地动作 `aclshmemi_state_init_attr`：把用户参数写进 `g_state` 并创建默认 stream。它不涉及通信，但必须在建堆之前完成，因为建堆要用 `heap_size`。

#### 4.2.2 核心流程

```text
aclshmemi_bootstrap_init(flags, attributes)      ← src/host/init/bootstrap/shmemi_bootstrap.cpp:262
   ├─ 按 flags 分派：UNIQUEID → MPI → DEFAULT（DEFAULT 无 ip_port 时回退 UID）
   ├─ dlopen("aclshmem_bootstrap_config_store.so" 或 "aclshmem_bootstrap_mpi.so")
   ├─ plugin_init(...) → 建链（Config Store 模式下 PE 0 起 KV 服务，其余 PE 连上）
   └─ g_boot_handle.is_bootstraped = true        ← :354

aclshmemi_state_init_attr(attributes, heap_size) ← 本地：填 g_state + 创建 stream
```

成功后 `g_boot_handle` 上可用的关键能力：

| 能力 | 用途（本讲范围内） |
|------|--------------------|
| `allgather` | 建堆时交换 slice/entity 描述符；user buffer heap 校验各 PE 布局一致 |
| `barrier` | init/finalize 的控制面同步（`aclshmemi_control_barrier_all` 底层） |
| `global_exit` | 异常时全组退出 |

#### 4.2.3 源码精读

- [src/host/init/shmem_init.cpp:962-964](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L962-L964)：阶段一的调用点——`aclshmemi_bootstrap_init(bootstrap_flags, attributes)`，成功后置 `bootstrap_initialized = true` 供失败守卫使用。这一行就是「控制面建链」在主流程中的全部投影，其余细节封装在 bootstrap 模块（u2-l3 专讲）。

- [src/host/init/bootstrap/shmemi_bootstrap.cpp:262-354](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/bootstrap/shmemi_bootstrap.cpp#L262-L354)：bootstrap 模块的入口区间，从按 flags 分派插件到 `g_boot_handle.is_bootstraped = true`（L354）。

- [src/host/init/shmem_init.cpp:966](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L966)：堆大小计算 `heap_size = attributes->local_mem_size + ACLSHMEM_EXTRA_SIZE`——用户申请的堆之外还要加一段「额外区」，存放堆分配器元数据、同步计数器等内部控制对象（L50-L52 的 `static_assert` 保证额外区足够大）。

- [src/host/init/shmem_init.cpp:154-167](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L154-L167)：`aclshmemi_state_init_attr` 把 `my_pe`/`n_pes`/`heap_size` 写入 `g_state`，并 `aclrtCreateStream` 创建 default_stream 存入 `g_state_host`（finalize 时销毁）。

#### 4.2.4 代码实践

**实践目标**：用日志验证阶段一确实发生，并观察 bootstrap 插件的选择。

**操作步骤**：

1. 在环境变量中设置 `export SHMEM_LOG_LEVEL=INFO`（该变量的读取逻辑见 [src/host/init/shmem_init.cpp:1217-1237](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L1217-L1237)，它优先于代码里设置的 ERROR 级别）。
2. 按 u1-l4 的方式运行 `examples/init`（`bash examples/init/run.sh` 或 `scripts/run_examples.sh`）。
3. 在输出中搜索 init 成功日志：主流程末尾 L1036 会打印 `"The ACLSHMEM pe: <N> init success."`。

**需要观察的现象**：每个 PE 进程都打印一条 init success；INFO 级别下能看到 bootstrap 建链相关日志。

**预期结果**：两个 PE 的输出交错出现，最终各自有一行 init success。**待本地验证**：具体 INFO 日志行内容与 CANN/库版本相关，无 NPU 环境时可改做源码阅读——在 `shmemi_bootstrap.cpp` 的 L262-L354 区间找出 `is_bootstraped = true` 之前的三步（分派、加载、初始化）分别对应的函数调用。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `aclshmemi_state_init_attr` 要放在 `aclshmemi_bootstrap_init` 之后、建堆之前，而不是放在函数最开头？

**答案**：它的输入 `heap_size` 依赖 `local_mem_size + ACLSHMEM_EXTRA_SIZE` 的计算（L966），这个计算本身不依赖 bootstrap；但把它放在 bootstrap 之后是流程编排的结果——真正硬性的约束只有一条：`g_state.heap_size` 必须在 `reserve_heap`（要预留这么大的虚拟地址区间）之前就位。放在这里使「远端协作（bootstrap）→ 本地状态 → 建堆」的语义顺序清晰。

**练习 2**：`ACLSHMEM_EXTRA_SIZE` 这段「额外区」是给谁用的？

**答案**：给库内部控制对象用的：堆分配器元数据、同步池与同步计数器（`sync_pool`/`sync_counter`，见 L50-L52 的 static_assert：`512 + SYNC_POOL_SIZE + SYNC_COUNTERS_SIZE <= ACLSHMEM_EXTRA_SIZE`）。所以两台 PE 即使 `local_mem_size` 相同，实际预留的堆也统一比用户值大一截，保证对称性不被内部对象破坏。

### 4.3 阶段二：HYBM 建堆——从后端创建到 heap_base 就位

#### 4.3.1 概念说明

这是初始化中最「重」的阶段，目标是让每个 PE 拿到一块**对称堆**：同序同大小 malloc 时堆内偏移一致，远端地址 = 对端堆基址 + 本地偏移。执行者不是 shmem_init.cpp 本身，而是 init 后端对象 `init_manager`（类型 `aclshmemi_init_backend`），shmem_init.cpp 只负责编排四个调用：

1. **`bind_aclshmem_entity`**：登记实体成员，把 init 参数、`g_state`、`g_boot_handle` 以及 **QP 配置**（`g_udma_qp_config` 与 `g_rdma_qp_config.qpNum`）绑定到后端——相当于把「工具包」递给建堆工人。
2. **`init_device_state`**：首次调用时初始化 HYBM 底层库并映射约 32MB 的元数据区（不是用户堆），之后控制面 barrier 对齐。
3. **`reserve_heap`**：`hybm_create_entity` 创建内存域 + `hybm_reserve_mem_space` 预留一段 GVA（全局虚拟地址），只占地址不占物理内存。
4. **`setup_heap`**：真正分配物理内存（slice），经 `exchange_slice` 把本 PE 的 slice 描述符用 bootstrap 的 `allgather` 广播给所有 PE，再 `hybm_import`+`hybm_mmap` 把远端 slice 映射进本 PE 地址空间，最后计算 `g_state.heap_base` 并置 `is_aclshmem_created = true`。

「先 reserve 后 setup」两步走的好处：GVA 预留可以做整段对齐，而物理内存分配与跨 PE 交换在第二步集中完成，失败路径也容易只回退 setup 部分。

#### 4.3.2 核心流程

```text
[后端就位]
  g_init_manager_count++;  若 init_manager == nullptr 则 new aclshmemi_init_backend()

[建堆四步]
  bind_aclshmem_entity(attributes, &g_state, &g_boot_handle,
                       user_buffer_heap_input, g_udma_qp_config, g_rdma_qp_config.qpNum)
      └─ entity_map_ 登记 entity_member，绑定参数/状态/QP 数（elem->udma_qp_num / elem->rdma_qp_num）
  init_device_state()
      └─ hybm_init（首次）→ 映射 ~32MB 元数据区 → control_barrier_all
  reserve_heap()
      └─ create_entity → hybm_reserve_mem_space → hbm_gva（预留 GVA）
  setup_heap()
      ├─ hybm_alloc_local_memory → slice（物理内存）
      ├─ exchange_slice：hybm_export → allgather（控制面!）→ hybm_import → barrier
      ├─ exchange_entity：流程同上（descLen==0 时跳过）
      ├─ hybm_mmap → 远端 slice 映射进本 PE
      ├─ 分配 p2p_*/rdma_*/sdma_* 堆基址数组；reach_info_init → topo_list
      ├─ heap_base = hbm_gva + ALIGN_UP(heap_size, ACLSHMEM_HEAP_ALIGNMENT_SIZE) * my_pe
      └─ g_state.is_aclshmem_created = true
```

其中 `create_entity` 内部会把 entity 上保存的 QP 数填进 `TransportOptions`（`udmaQpConfig.qpNum` / `rdmaQpConfig.qpNum`）再交给 HYBM 建实体——这是 QP 配置离开 init 模块、进入传输层的唯一出口（4.6 节展开）。

注意 `exchange_slice` 里那个 `allgather`——**阶段二对阶段一的依赖就体现在这一行**：没有 bootstrap 控制面，slice 交换无从谈起。各 PE 堆基址按 `my_pe` 错开：

\[ \text{heap\_base}_{pe} = \text{hbm\_gva} + \lceil \text{heap\_size}/A \rceil \cdot A \cdot pe, \quad A = \text{ACLSHMEM\_HEAP\_ALIGNMENT\_SIZE} \]

每个 PE 在自己的 GVA 视角里都能「看到」全组 N 份堆，第 pe 段是自己的——这就是后续 RMA 寻址「对端基址 + 本地偏移」的物理基础（u2-l5 详讲）。

#### 4.3.3 源码精读

- [src/host/init/shmem_init.cpp:993-1000](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L993-L1000)：后端创建——引用计数自增；`init_manager` 为空才 `new`，已存在（多实例共享）则复用并打日志。

- [src/host/init/shmem_init.cpp:1002-1008](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L1002-L1008)：`bind_aclshmem_entity` 绑定实体并置 `entity_bound = true`——注意实参列表最后两项 `g_udma_qp_config` 与 `g_rdma_qp_config.qpNum`，两个引擎的 QP 数都在这里一次性交给后端；随后 `init_device_state` 初始化 HYBM 与元数据区。两步各推进一个失败守卫标记。

- [src/host/init/shmem_init.cpp:1012-1019](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L1012-L1019)：`reserve_heap` 与 `setup_heap` 的调用点。`reserve_status` 先接住返回值：user buffer heap 模式下经 `aclshmemi_collective_status_gate`（见下）做全组状态汇聚，普通模式直接检查；`heap_reserved = true` 后才允许 `setup_heap`。

- [src/host/init/shmem_init.cpp:553-575](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L553-L575)：`aclshmemi_collective_status_gate`——把本 PE 的状态码用 bootstrap 的 `allgather` 收齐，**任何一个 PE 失败就整体失败**。这是「集体操作要么全成要么全败」语义的通用工具（finalize 的 `remove_heap` 也复用它）。

- [src/host/init/backends/shmemi_init_backend.h:60-77](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/backends/shmemi_init_backend.h#L60-L77)：后端类的建堆接口全景——`init_device_state`/`finalize_device_state`、`update_device_state`、四个堆生命周期方法（`reserve_heap`/`setup_heap`/`remove_heap`/`release_heap`，均带 `mem_type` 默认参数区分 device/host 侧）、`bind_aclshmem_entity`/`release_aclshmem_entity`（签名末尾的两个 QP 参数：`const UdmaQpConfig&` 与 `uint32_t rdma_qp_num`）。**这一段头文件就是阶段二与 finalize 的「菜单」**，具体实现（`exchange_slice`、`heap_base` 计算）在 `shmem_init_backend.cpp`，u2-l5 详讲。

- [src/host/init/backends/shmemi_init_backend.h:51-52](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/backends/shmemi_init_backend.h#L51-L52)：`entity_member` 中的两个 QP 字段 `udma_qp_num{1}` / `rdma_qp_num{1}`（后者为本轮新增），默认值都是 1——不调用 `aclshmemx_set_qp_num` 时每个 peer 只建一条 QP，行为与旧版本兼容。

#### 4.3.4 代码实践

**实践目标**：从 shmem_init.cpp 出发，追出 `setup_heap` 的实现位置，验证「slice 交换走 bootstrap allgather」这一说法。

**操作步骤**：

1. 在 `src/host/init/backends/shmemi_init_backend.h` 找到 `setup_heap` 声明（L65）。
2. 用 Grep 在 `src/host` 下搜索 `::setup_heap` 或 `exchange_slice`，定位实现文件。
3. 阅读实现中 `exchange_slice` 附近的代码，确认其中是否调用了 `g_boot_handle`（或其成员 `allgather`）上的函数。

**需要观察的现象**：`exchange_slice` 的实现里出现对 bootstrap 句柄 allgather 的调用，与 `docs/principles/init_finalize.md` §3.3.6.4 的描述一致。

**预期结果**：确认调用链 `setup_heap → exchange_slice → (bootstrap) allgather/barrier`，并能说出这条链证明了「阶段二依赖阶段一」。此实践为源码阅读型，无需 NPU。

#### 4.3.5 小练习与答案

**练习 1**：`reserve_heap` 和 `setup_heap` 为什么拆成两步而不是一步到位？

**答案**：reserve 只在 GVA 空间里圈一段对齐的虚拟地址（不占物理内存），成本低且各 PE 可独立完成；setup 才分配物理 slice 并做跨 PE 交换（昂贵且需要控制面同步）。拆开后：① GVA 预留可整段对齐，简化 `heap_base` 按 pe 错开的布局计算；② 失败回滚粒度更细——`init_abort_guard` 里 `heap_reserved` 为 true 时先 `remove_heap` 再 `release_heap`（L930-L933），正对应两步的逆操作；③ host 侧堆（d2h 场景）复用同一对接口但只 reserve、lazy setup（L1025-L1028 注释）。

**练习 2**：如果把 `setup_heap` 中 `is_aclshmem_created = true` 提前到 `reserve_heap` 之后，会有什么问题？

**答案**：`is_aclshmem_created` 是「堆已建」的状态位，`aclshmemx_init_status` 靠它区分 `NOT_INITIALIZED` 与 `SHM_CREATED`。提前置位会让「只预留了地址、还没有物理内存和映射」的中间状态对外显示为已建堆；若此时另一线程查询状态或重复 init，防重入检查（L953）会拒绝 init，但状态语义失真——官方文档明确 `setup_heap` 末尾才置位（该标志在 backend 实现中设置，文档 §3.1 有说明）。

**练习 3**：阶段二里出现了几次控制面同步？分别在哪个调用里？

**答案**：至少两次显式/隐式同步：`init_device_state` 末尾有一次 `aclshmemi_control_barrier_all`（文档 §3.3.6.2），保证全组 HYBM 元数据区都就绪；`setup_heap` 的 `exchange_slice`/`exchange_entity` 内部各含 `allgather + barrier`（文档 §3.3.6.4）。若走 user buffer heap 模式，`reserve_heap` 的返回值还会经 `aclshmemi_collective_status_gate` 做一次全组状态汇聚（shmem_init.cpp L1013-L1017）。

### 4.4 阶段三：子模块就绪与收尾——从分配器到控制面 barrier

#### 4.4.1 概念说明

堆建好后，SHMEM 还不是「可用」状态：堆上要跑一个**分配器**（管理 first-fit 空闲块，支撑 `aclshmem_malloc`），还要初始化若干**子模块**——signal（同步信号缓冲）、team（通信域池）、sync（平台同步原语）。这些子模块大多要在**对称堆上**分配自己的控制对象（例如 signal 就是 `aclshmem_malloc(512)` 出来的），所以它们既依赖堆（阶段二），其内部 malloc 又会触发控制面 barrier 保持各 PE 偏移一致——三阶段在这里完成闭环。

收尾四件事：置 `is_aclshmem_initialized = true`（状态机进入完全可用）、`prof_util_init`（profiling 就绪）、`update_device_state`（把 `g_state` 拷贝到 NPU 元数据区，让算子能读到 my_pe/heap_base）、**全组 barrier 对齐**——确保所有 PE 都走完上述步骤后 `aclshmemx_init_attr` 才返回。返回即意味着「任何人都可以开始用 SHMEM API 了」。最后还有一个不起眼但重要的动作：`g_qp_config_frozen = true`，把 QP 配置冻结（4.6 节展开）。

#### 4.4.2 核心流程

```text
[子模块就绪]
  memory_manager_initialize(heap_base + external_bytes, heap_size - external_bytes)
      └─ 在对称堆上建 first-fit 分配器（user buffer 模式下起点后移 external_bytes）
  （可选，支持 d2h 时）reserve_heap(HOST_SIDE)   ← host 堆只预留，首次 host malloc 才 setup
  aclshmemi_signal_init()    └─ aclshmem_malloc(512) → g_state.signal_addr，清零信号区
  aclshmemi_team_init(mype, npes) └─ ACLSHMEM_TEAM_WORLD、team_pool、同步池
  aclshmemi_sync_init()

[收尾]
  g_state.is_aclshmem_initialized = true
  prof_util_init(&g_host_profs, &g_state)
  update_device_state()      └─ g_state → device 元数据区（hybm_set_extra_context）
  aclshmemi_control_barrier_all()   ← 全组对齐，init 返回 SUCCESS
  g_qp_config_frozen = true         ← QP 配置冻结（L1039）
  释放 init_abort_guard / ctx_guard（成功路径不回滚）
```

一个值得注意的细节：`signal_init` 里调用了 `aclshmem_malloc`——这是**初始化流程内部对用户 API 的复用**。它之所以可行，是因为分配器在它之前已就位；它同时也意味着 init 过程本身就在消耗对称堆偏移（每个 PE 都同样消耗 512 字节，对称性保持）。

#### 4.4.3 源码精读

- [src/host/init/shmem_init.cpp:1022-1023](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L1022-L1023)：分配器就位——`memory_manager_initialize` 的基地址是 `heap_base + external_bytes`（user buffer 模式下 external_bytes 为外部 buffer 总量，默认堆为 0），大小相应扣减。分配器实现位于 [src/host/mem/shmem_mm.cpp:15](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/mem/shmem_mm.cpp#L15)。

- [src/host/init/shmem_init.cpp:1025-1031](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L1025-L1031)：可选 host 侧堆预留 + 三个子模块初始化的调用序列：`aclshmemi_signal_init`（本文件 L657）、`aclshmemi_team_init`（[src/host/team/shmem_team.cpp:149](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/team/shmem_team.cpp#L149)）、`aclshmemi_sync_init`（[src/host/sync/shmemi_sync.cpp:26](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/sync/shmemi_sync.cpp#L26)）。注意主流程只做编排，实现分散在各子模块文件——这是「编排层/实现层分离」的典型。

- [src/host/init/shmem_init.cpp:657-672](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L657-L672)：`aclshmemi_signal_init` 全文——512 对齐 malloc 一块信号区，`aclrtMemset` 清零 `ACLSHMEM_SIGNAL_SIZE` 字节；失败时调用配套的 `aclshmemi_signal_finalize`（L648-L655）回滚。

- [src/host/init/shmem_init.cpp:1032-1039](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L1032-L1039)：收尾六连——置位 `is_aclshmem_initialized`、profiling 初始化、`update_device_state` 同步到 device、**控制面 barrier**、打印成功日志、`exception_guard.release()` 后置 `init_succeeded = true` 并**冻结 QP 配置**（`g_qp_config_frozen = true`）。这几行是「子模块就绪 → 全组一致 → 宣告成功 → 锁定配置」的最短路径。

- [src/host/init/shmem_init.cpp:581-584](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L581-L584)：`update_device_state` 的转发实现——把 `g_state` 整体交给 `init_manager` 拷贝到 device 元数据区。device 侧算子读到的 mype/heap_base 就是这份拷贝（u4-l1 详讲镜像机制）。

- [src/host/init/shmem_init.cpp:577](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L577)：`aclshmemi_control_barrier_all` 同样是一行转发到 `init_manager`，最终落到 bootstrap 句柄的 barrier 函数上——**控制面贯穿 init 全程**。

#### 4.4.4 代码实践

**实践目标**：验证「init 返回后各 PE 的堆信息一致可用」——直接调用 u2-l4 将要学的 heap 查询接口，或者用日志观察。

**操作步骤**：

1. 复制 `examples/init/main.cpp` 为自己的实验程序（不修改源码树，拷贝到仓库外或 examples 外的临时目录）。
2. 在 `aclshmemx_init_attr` 成功返回后、`aclshmem_finalize` 之前，追加打印：

```cpp
// 示例代码：打印初始化产出（需包含 host/shmem_host_def.h 等头文件）
printf("pe %d / %d, heap_base=%p, heap_size=0x%lx\n",
       aclshmem_my_pe(), aclshmem_n_pes(),
       (void*)0 /* heap_base 需经 aclshmemx_get_heap_base 获取，此处示意 */, 0UL);
```

3. 以 2 个 PE 运行（参考 u1-l4 的 run.sh 用法）。

**需要观察的现象**：两个 PE 各打印一行，`my_pe` 不同（0 和 1）、`n_pes` 相同；堆信息查询接口（如 `aclshmemx_get_heap_base`）返回非空且各 PE 堆大小一致。

**预期结果**：`init success` 日志出现在打印之前（因为 L1036 的日志先于函数返回）。**待本地验证**：需要 NPU + CANN 环境编译运行；无环境时可改做源码阅读——在 `include/host` 下找到堆基址查询接口的声明，并确认其数据来源正是 `g_state`。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `is_aclshmem_initialized = true`（L1032）在 `update_device_state`（L1034）**之前**置位，而不是全部做完再置位？

**答案**：`is_aclshmem_initialized` 表达「子模块已就绪、API 可用」；`update_device_state` 与最后的 barrier 属于收尾动作。提前置位让（同进程内）在 barrier 等待期间理论上已可查询状态为 IS_INITIALIZED。但对外语义上真正的「init 返回成功」仍以函数返回值为准——最后一个 barrier 保证返回时全组一致。对比 finalize：那里 `is_aclshmem_initialized = false` 在拆除堆**之前**置位（L1099），方向恰好相反——先宣布不可用再拆家，防止拆除过程中还有 API 调用进来。

**练习 2**：init 主流程最后一个 `aclshmemi_control_barrier_all`（L1035）删掉会怎样？

**答案**：编译无恙，但语义被破坏：PE 0 可能先返回并立刻对 PE 1 的对称堆做 put，而 PE 1 还没执行完 `update_device_state`（算子读到的 device 副本可能还是旧值），或尚未进入「可接收 RMA」的状态。该 barrier 正是文档 §1.5 强调「各 PE 对称进入」的落实点：**init 返回 = 全组都完成了全部步骤**。

**练习 3**：`aclshmemi_signal_init` 若 malloc 失败，会发生什么？

**答案**：L657-L672 中 malloc 返回 0 时打 ERROR 日志并返回 `ACLSHMEM_INNER_ERROR`；主流程的 `ACLSHMEM_CHECK_RET(aclshmemi_signal_init())`（L1029）拦截后函数走失败路径——`init_abort_guard` 触发，逆序释放已建的堆、bootstrap 与后端，`init_succeeded` 保持 false，`g_state.is_aclshmem_initialized` 复位为 false。

### 4.5 finalize：逆序释放与两道控制面 barrier

#### 4.5.1 概念说明

`aclshmem_finalize` / `aclshmemx_finalize`（带 instance_id）都转发到 `aclshmemi_finalize_impl`。释放顺序基本是 init 的**镜像**：init 是「控制面 → 堆 → 子模块」，finalize 是「子模块 → 堆 → 控制面」。逆序的理由是依赖方向不变：子模块住在堆上（先拆子模块才能拆堆），堆的交换靠控制面（先拆堆才能拆控制面）。

与 init 不同，finalize 有**两道**控制面 barrier，各有使命：

- **第一道（L1088，入口处）**：确认全体 PE 都已进入 finalize。防止 PE 0 已开始拆堆、PE 1 还在对 PE 0 的堆做 RMA。
- **第二道（L1135，拆 bootstrap 前）**：确保所有 PE 都完成了各自本地的堆拆除，才允许任何一方拆掉 Config Store——否则先拆 store 的 PE 会让后来者连不上控制面而卡死。

此外 finalize 有一个 init 没有的特点：**清理步骤尽量不中断**。用 `recordCleanupStatus` 把每步的错误记下来继续拆，最后汇总返回第一个错误——「尽力拆干净」优于「遇到错误就放弃」。全部拆完、且这是**最后一个实例**时，还会把两个 QP 配置复位为默认值并解冻（见 4.6 节）。

#### 4.5.2 核心流程

```text
aclshmem_finalize() / aclshmemx_finalize(id)      ← 互斥锁保护
  └─ aclshmemi_finalize_impl(instance_id)
       ├─ [0] 目标实例非当前活动实例时 ctx_set 切换（校验 id 存在）
       ├─ [1] init_manager == nullptr → 清标志、销毁实例上下文，提前返回
       ├─ [2] ★barrier ①：aclshmemi_control_barrier_all()   ← 全体都已进入 finalize
       ├─ [3] 逆序拆子模块：team_finalize → signal_finalize → memory_manager_destroy
       ├─ [4] is_aclshmem_initialized = false（宣布不可用，拆家开始）
       ├─ [5] 拆堆：remove_heap（+全组状态汇聚 gate）→ release_heap
       │        → finalize_device_state →（可选 host 侧）→ release_aclshmem_entity
       ├─ [6] 拆 stream：aclrtSynchronizeStream + aclrtDestroyStream
       ├─ [7] ★barrier ②：aclshmemi_control_barrier_all()   ← 全部拆完才能拆控制面
       ├─ [8] aclshmemi_bootstrap_finalize()（store 引用计数归零才 DestroyStore）
       ├─ [9] g_init_manager_count--，归零则 delete init_manager
       └─ [10] 销毁实例上下文；最后一个实例时复位 g_rdma_qp_config/g_udma_qp_config 并解冻
```

与 init 三阶段对照：

| init（正序） | finalize（逆序） |
|--------------|------------------|
| 子模块 init（team/signal/分配器） | team_finalize → signal_finalize → memory_manager_destroy |
| 建堆（bind → device_state → reserve → setup） | remove_heap → release_heap → finalize_device_state → release_entity |
| bootstrap_init（控制面） | barrier ② → bootstrap_finalize |
| init 成功后冻结 QP 配置（L1039） | 最后一个实例 finalize 后复位并解冻 QP 配置（L1153-L1156） |

#### 4.5.3 源码精读

- [src/host/init/shmem_init.cpp:1161-1173](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L1161-L1173)：两个对外 finalize 入口——`aclshmemx_finalize(instance_id)` 释放指定实例，`aclshmem_finalize()` 释放当前活动实例（取 `g_instance_ctx->id`），都持同一把互斥锁后进入 impl。

- [src/host/init/shmem_init.cpp:1066-1088](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L1066-L1088)：impl 开头——必要时切换实例上下文（顺带校验 id 存在）；`init_manager` 为空说明本进程没有活跃实例，清标志销毁上下文即返回；随后 **barrier ①**（L1088）。

- [src/host/init/shmem_init.cpp:1090-1099](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L1090-L1099)：逆序拆子模块并**先置 `is_aclshmem_initialized = false`**。L1095-L1098 的注释解释了为何 `is_aclshmem_created` 要保留到最后：清理失败时应报 `SHM_CREATED`（半拆状态）而不是伪装成完全初始化/完全终结。

- [src/host/init/shmem_init.cpp:1101-1131](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L1101-L1131)：`recordCleanupStatus` lambda（记错不中断）+ 堆拆除序列：`remove_heap` 的结果除本地检查外还经 `aclshmemi_collective_status_gate` 全组汇聚（L1115-L1117）——**拆堆也是集体操作**；随后 `release_heap`、`finalize_device_state`、可选 host 侧、`release_aclshmem_entity`、同步并销毁 default_stream。

- [src/host/init/shmem_init.cpp:1133-1136](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L1133-L1136)：**barrier ②** 与 bootstrap 拆除——L1133-L1134 注释点明动机「no rank can start the next init while another rank's store is still alive」（也为「finalize 后重新 init」的循环使用场景兜底）。

- [src/host/init/shmem_init.cpp:1138-1157](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L1138-L1157)：引用计数减一，归零才 `delete init_manager`；按 `firstCleanupStatus` 汇报成功或首个错误；销毁实例上下文；**最后一个实例**时把 `g_rdma_qp_config` 与 `g_udma_qp_config` 都复位为默认构造（qpNum=1）并置 `g_qp_config_frozen = false`——与 init 成功路径的冻结逻辑（L1039）首尾呼应，构成 QP 配置的完整生命周期（见 4.6）。

#### 4.5.4 代码实践

**实践目标**：体会「finalize 是集体操作」——故意让一个 PE 不调用 finalize，观察超时行为。

**操作步骤**：

1. 拷贝 `examples/init/main.cpp`，在 finalize 调用外包裹条件：仅 `my_pe == 0` 时执行 `aclshmem_finalize()`，PE 1 跳过（示例代码，实验用途）：

```cpp
// 示例代码：制造不对称 finalize（仅实验，业务代码禁止这样写）
if (aclshmem_my_pe() == 0) {
    aclshmem_finalize();
} else {
    sleep(200); // PE 1 迟迟不进入 finalize
}
```

2. 以 2 PE 运行，观察 PE 0 的日志输出与进程状态。

**需要观察的现象**：PE 0 在 finalize 的 barrier 上等待 PE 1；超过 `control_operation_timeout`（默认 120 秒）后报错/超时日志，进程不会干净退出。

**预期结果**：复现文档 §6「finalize barrier 超时/卡住」现象，确认两道 barrier 任何一道都可能成为卡点。**待本地验证**：需要 NPU 环境运行；无环境时可做源码阅读——在 `aclshmemi_finalize_impl` 中数出 `aclshmemi_control_barrier_all` 的出现次数（应为 2 次：L1088 与 L1135），并分别写出两处的守护目标。

#### 4.5.5 小练习与答案

**练习 1**：finalize 中 `remove_heap` 的返回值为什么要过一次 `aclshmemi_collective_status_gate`？

**答案**：`remove_heap` 释放的是**对称堆**——本 PE 拆除失败意味着全组的堆状态不一致（别的 PE 可能已拆、映射关系残缺），此时继续各自为政会让错误雪上加霜。gate 用 allgather 把各 PE 的拆除结果收齐，任何一方失败则全组统一报错，配合 barrier ② 之后才拆控制面，保证错误路径上控制面仍然可用以完成同步。

**练习 2**：若 finalize 里某个清理步骤（如 `release_heap`）失败，函数会立即返回吗？

**答案**：不会。`recordCleanupStatus` 只记录第一个非 SUCCESS 状态并打 ERROR 日志，流程继续执行后续清理（拆 entity、销毁 stream、barrier、拆 bootstrap……），最后统一返回 `firstCleanupStatus`。这样能把资源拆到最干净的程度，避免「一步失败、满盘皆留」的泄漏。

**练习 3**：`aclshmem_finalize()` 与 `aclshmemx_finalize(instance_id)` 的区别是什么？

**答案**：前者作用于**当前活动实例**（`g_instance_ctx->id`，经 `aclshmemx_instance_ctx_set` 切换后可变）；后者显式指定实例，若目标不是当前活动实例，impl 开头会先 `aclshmemx_instance_ctx_set_impl(instance_id)` 切换全局变量再拆（顺带校验 id 存在）。单实例程序两者等价；多实例程序官方推荐用带 id 的版本。

### 4.6 QP 配置的生命周期：配置 → 冻结 → 下发 → 复位

#### 4.6.1 概念说明

QP（Queue Pair）数决定了每个 peer 之间并行建多少条传输队列。本轮版本把 `aclshmemx_set_qp_num` 从「仅支持 UDMA」扩展为**同时支持 ROCE 与 UDMA** 两个引擎，于是 init 模块里有了两条独立的配置：`g_udma_qp_config` 与 `g_rdma_qp_config`（本轮新增）。两者共享同一套生命周期规则：

1. **配置窗口**：只能在**没有任何实例处于初始化状态时**调用 `aclshmemx_set_qp_num`；取值范围 \([1, \text{ACLSHMEM\_MAX\_QP\_NUM}]\)（即 1~32），且**全组各 PE 必须配成同一个值**——不一致会导致各 PE 的引擎元数据布局互不兼容。
2. **冻结**：init 成功的最后一步置 `g_qp_config_frozen = true`，此后再调用 set 接口返回 `ACLSHMEM_NOT_SUPPORTED`。注意冻结是「进程内只要有任一实例存活」就生效，配置本身是进程级的、不随实例隔离。
3. **下发**：`bind_aclshmem_entity` 把两个 QP 数存进 `entity_member`，`create_entity` 再填入 `TransportOptions` 交给 HYBM/传输层——建实体时就按 qpNum 为每个 peer 建足额 QP。
4. **复位**：最后一个实例 finalize 时（`g_init_manager_count` 归零），两个配置都复位为默认值（qpNum=1）并解冻，允许下一轮 init 前重新配置。

为什么要有「冻结」而不是每次 init 重新读？因为 QP 数在建实体时就被「烧」进了传输层元数据；实例存活期间改配置，会让已建立的连接组与新配置不一致。

#### 4.6.2 核心流程

```text
[配置] aclshmemx_set_qp_num(engine, qp_num)        ← 任意时刻（未冻结时）
   ├─ 校验 qp_num ∈ [1, 32]  → 否则 ACLSHMEM_INVALID_VALUE
   ├─ 校验未冻结（g_qp_config_frozen == false）→ 否则 ACLSHMEM_NOT_SUPPORTED
   └─ engine 分派：ROCE → g_rdma_qp_config.qpNum；UDMA → g_udma_qp_config.qpNum；
                   其他引擎 → WARN + ACLSHMEM_NOT_SUPPORTED

[冻结] init 成功收尾：g_qp_config_frozen = true     ← shmem_init.cpp L1039

[下发] bind_aclshmem_entity(..., g_udma_qp_config, g_rdma_qp_config.qpNum)
   ├─ shmem_init_backend.cpp: elem->udma_qp_num / elem->rdma_qp_num
   └─ create_entity: transport_options.udmaQpConfig.qpNum / rdmaQpConfig.qpNum
        → hybm_create_entity_with_transport_options（按 qpNum 建 QP，u5-l3/u5-l7 详讲）

[复位] 最后一个实例 finalize：
   g_rdma_qp_config = RdmaQpConfig{};  g_udma_qp_config = UdmaQpConfig{};
   g_qp_config_frozen = false;                     ← shmem_init.cpp L1153-L1156
```

#### 4.6.3 源码精读

- [src/host/init/shmem_init.cpp:103-105](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L103-L105)：QP 配置三件套的定义——`g_udma_qp_config`、`g_rdma_qp_config`（本轮新增，类型为 `TransportOptions::RdmaQpConfig` 的内嵌结构）与冻结标志 `g_qp_config_frozen`，均为 static，只属于 init 模块。

- [src/host/init/shmem_init.cpp:615-637](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L615-L637)：`aclshmemx_set_qp_num` 全文——本轮重写后的结构：先经 `is_valid_rdma_qp_num` 校验取值（L618），再查冻结（L622），最后按引擎分派（L627-L634）：`ACLSHMEM_DATA_OP_ROCE` 写 `g_rdma_qp_config`、`ACLSHMEM_DATA_OP_UDMA` 写 `g_udma_qp_config`，其他引擎 WARN 后返回 `ACLSHMEM_NOT_SUPPORTED`。注意**校验顺序**：qp_num 合法性优先于引擎判断，即使传错引擎也会先收到 INVALID_VALUE（若 qp_num 非法）。

- [src/host/init/shmem_init.cpp:176](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L176)：`is_valid_rdma_qp_num`——一行谓词，上界 `ACLSHMEM_MAX_QP_NUM` 定义于 [src/host/shmem_host_def.h:34](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/shmem_host_def.h#L34)，当前为 32。

- [include/host/init/shmem_host_init.h:119-134](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host/init/shmem_host_init.h#L119-L134)：`aclshmemx_set_qp_num` 的对外声明与契约注释——明确支持 ROCE 与 UDMA、必须在实例初始化前调用、冻结与复位规则、**各 PE 取值必须一致**（否则元数据布局不兼容），以及线程安全语义。

- [src/host/transport/transport_def.h:50-61](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/transport_def.h#L50-L61)：`TransportOptions` 结构——内嵌 `RdmaQpConfig`（L54-L56，本轮新增）与成员 `udmaQpConfig`（L60）。init 模块的 QP 配置最终就落到这两个字段上，交给传输层消费。

- [src/host/init/backends/shmem_init_backend.cpp:78-98](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/backends/shmem_init_backend.cpp#L78-L98)：`bind_aclshmem_entity` 实现的头部——L98 把 `rdma_qp_num` 存入 `elem->rdma_qp_num`（`udma_qp_num` 同理在上一行），QP 数从此挂在 entity 上，随实体生命周期存在。

- [src/host/init/backends/shmem_init_backend.cpp:260-261](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/backends/shmem_init_backend.cpp#L260-L261)：`create_entity` 中把 entity 上的两个 QP 数填进 `transport_options`，随后经 `hybm_create_entity_with_transport_options`（L283/L291）传入 HYBM——**这是 QP 配置离开 init 模块的唯一出口**。传输管理器如何按 qpNum 建多条 QP 并做全组一致性校验，属于 u5-l3/u5-l7 的主题。

- [src/host/init/shmem_init.cpp:1151-1157](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L1151-L1157)：finalize 的复位动作——`is_last_instance` 为真时，`g_rdma_qp_config` 与 `g_udma_qp_config` 先后复位为默认构造（qpNum=1），再解冻。多实例场景下只有最后一个实例退出才触发，中间实例的 finalize 不影响配置。

#### 4.6.4 代码实践

**实践目标**：验证 QP 配置的冻结语义——init 之后再改配置应当被拒绝。

**操作步骤**：

1. 阅读测试 [tests/unittest/host/init/init_host_test.cpp:574-583](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/tests/unittest/host/init/init_host_test.cpp#L574-L583) 的 `TestSetRdmaQpNumBeforeInit` 用例：合法值 {1, 2, 4, 8, 32} 断言返回 `ACLSHMEM_SUCCESS`，0 与 33 断言返回 `ACLSHMEM_INVALID_VALUE`——正好覆盖 `is_valid_rdma_qp_num` 的边界；UDMA 版用例（L585-L594）结构完全相同。
2. 复制 `examples/init/main.cpp` 为实验程序（放在源码树外），在 `aclshmemx_init_attr` 成功返回后追加：

```cpp
// 示例代码：init 后尝试改 QP 数，预期被冻结检查拒绝
int ret = aclshmemx_set_qp_num(ACLSHMEM_DATA_OP_ROCE, 4);
printf("set_qp_num after init returns %d (expect non-zero NOT_SUPPORTED)\n", ret);
```

3. 在 `aclshmem_finalize` 之后再调用一次同样代码，观察返回值变化。官方已有一条端到端用例验证这条路径：[tests/unittest/host/init/init_host_test.cpp:603-608](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/tests/unittest/host/init/init_host_test.cpp#L603-L608) 的 `TestQpReconfigurationAfterLastFinalize`（内部经 `test_qp_reconfiguration_after_last_finalize` 拉起进程验证「最后一个实例 finalize 后 QP 可重新配置」）。
4. 无 NPU 环境时，做源码阅读版：在 shmem_init.cpp 中标出 `g_udma_qp_config` / `g_rdma_qp_config` 的全部出现点（定义 L103-L104、写入 L628/L630、下发 L1004-L1005、复位 L1154-L1155），并注明每处发生在 init/finalize 的哪个阶段。

**需要观察的现象**：init 之后调用返回非 0（`ACLSHMEM_NOT_SUPPORTED`），日志出现 "QP configuration cannot be changed while an ACLSHMEM instance is initialized."；finalize 之后调用返回 0（配置已解冻）。

**预期结果**：两次调用返回值不同，验证「实例存活期间冻结、最后一个实例 finalize 后解冻」的生命周期。**待本地验证**：需要 NPU 环境编译运行；源码阅读版无条件完成。

#### 4.6.5 小练习与答案

**练习 1**：PE 0 配置 ROCE QP 数为 4、PE 1 忘记配置（保持默认 1），会发生什么？

**答案**：`aclshmemx_set_qp_num` 本身是本地调用、不会立刻报错；但两个 PE 建实体时传给传输层的 `rdmaQpConfig.qpNum` 不同，引擎元数据布局互不兼容——这正是头文件注释（shmem_host_init.h L123-L127）警告「inconsistent values produce incompatible metadata layouts」的场景。各 PE 的 QP 数一致性由传输管理器在建链阶段校验（u5-l7 讲 `CheckQpNumConsistency`），通常表现为建链失败或初始化报错。**教训：QP 数必须在所有 PE 上配置成同一个值。**

**练习 2**：为什么 `g_rdma_qp_config` 复位放在「最后一个实例 finalize」而不是「每个实例 finalize」？

**答案**：QP 配置是**进程级**的，且被所有实例共享的 `init_manager`/entity 消费。若中间实例 finalize 就复位，剩余存活实例的传输层仍在按旧 qpNum 运行，配置与实际状态脱节；而且复位后用户可能改配置，下一个新实例 init 时会与共存实例的元数据冲突。所以必须等到进程内**没有任何实例**（`g_init_manager_count == 0`）才复位解冻——这与 `delete init_manager` 放在同一条件分支里，语义自洽。

**练习 3**：当前 `aclshmemx_set_qp_num` 把 qp_num 校验放在引擎分派之前，这带来什么行为差异？

**答案**：若用户既传了不支持的引擎又传了非法 qp_num（如 0 或 100），旧版会先因引擎不支持返回 NOT_SUPPORTED，新版会先因 qp_num 越界返回 INVALID_VALUE——错误码更精确地指向「值本身不合法」这一更基础的问题，便于排错。这也提醒我们读源码时不能只看函数「支持什么」，还要看**检查的先后顺序**。

## 5. 综合实践

**任务：制作你自己的「SHMEM 初始化流程标注图」**（对应本讲规格中的实践任务）。

1. **标注**：打开 [src/host/init/shmem_init.cpp](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp)，在 `aclshmemi_init_attr_impl`（L895-L1043）中用三种颜色（或三类注释 `// [P1]` `// [P2]` `// [P3]`）标出：
   - `[P1]` Bootstrap 阶段：`aclshmemi_bootstrap_init`（L963）、`aclshmemi_state_init_attr`（L991）；
   - `[P2]` 建堆阶段：`new aclshmemi_init_backend`（L997）、`bind_aclshmem_entity`（L1003-L1005）、`init_device_state`（L1007）、`reserve_heap`（L1012）、`setup_heap`（L1019）；
   - `[P3]` 子模块阶段：`memory_manager_initialize`（L1023）、`aclshmemi_signal_init`（L1029）、`aclshmemi_team_init`（L1030）、`aclshmemi_sync_init`（L1031）、`update_device_state`（L1034）、`aclshmemi_control_barrier_all`（L1035）、`g_qp_config_frozen = true`（L1039）。
2. **标注 QP 配置流转**：在图上另起一条虚线「QP 配置轨道」，标出 `g_udma_qp_config` / `g_rdma_qp_config` 的四个关键节点——写入（`aclshmemx_set_qp_num`，L615-L637，init 之前）、下发（`bind_aclshmem_entity` 实参，L1003-L1005）、冻结（L1039）、复位（finalize 末尾 L1153-L1156），并注明复位仅在最后一个实例时发生。
3. **画图**：仿照本讲 4.1.2 的流程图，手绘（或用工具画）一张包含 init 与 finalize 双列的对照图：左列 init 三阶段自上而下，右列 finalize 自上而下，用箭头标出「逆序」关系，并**在每一步旁注明关键函数名**；QP 配置轨道跨越两列。
4. **验证**：对照 `docs/principles/init_finalize.md` §3.1 的步骤表检查你的标注是否有遗漏；把文档表格中没有、但你在源码里发现的有价值细节（例如两道 barrier、`init_abort_guard`、QP 冻结）补充到你的图上——这正是「文档滞后于代码」时以源码为准的练习。
5. **加分项**（需 NPU 环境，**待本地验证**）：设置 `SHMEM_LOG_LEVEL=INFO` 重跑 `examples/init`，把实际日志行映射到图中的对应步骤。

**交付物**：一张标注好的流程图（含 QP 配置轨道）+ 一份「函数名 → 源码行号」清单。

## 6. 本讲小结

- `aclshmemx_init_attr` 是薄壳，真正的主流程是 `aclshmemi_init_attr_impl`（shmem_init.cpp L895-L1043），按 **Bootstrap 建链 → HYBM 建堆 → 子模块就绪** 三阶段线性推进，顺序由依赖关系决定：建堆要靠控制面交换 slice，子模块要住在堆上。
- 阶段一产出 `g_boot_handle`（barrier/allgather 能力）与填好的 `g_state`；阶段二由 `init_manager`（`aclshmemi_init_backend`）完成 `bind_aclshmem_entity → init_device_state → reserve_heap → setup_heap`，堆基址按 `my_pe` 在预留 GVA 上错开；阶段三在堆上就位分配器/signal/team/sync，`update_device_state` 把 `g_state` 镜像到 NPU 元数据区，最后控制面 barrier 后 init 才返回。
- init 用 `init_abort_guard` 等 scope guard 实现**失败路径的本地逆序回滚**（不做跨 PE 同步），用 `is_aclshmem_created` / `is_aclshmem_initialized` 两个布尔维护三态状态机。
- finalize 是 init 的镜像逆序，且有**两道** `aclshmemi_control_barrier_all`：入口处确认全组进入 finalize，拆 bootstrap 前确认全组拆完堆；清理步骤「记错不中断」，最后汇总返回。
- QP 配置（ROCE/UDMA）是**进程级**生命周期：init 前经 `aclshmemx_set_qp_num` 配置（1~32、全组一致）→ init 成功时冻结（L1039）→ `bind_aclshmem_entity` 时经 `TransportOptions` 下发传输层 → 最后一个实例 finalize 后复位解冻（L1153-L1156）。多 QP 建链细节见 u5-l7。
- 全局状态 `g_state`/`g_state_host`/`g_boot_handle`/`init_manager` 定义在 shmem_init.cpp、经 `shmemi_init.h` 导出，是贯穿所有模块的「主线变量」；多实例时 `init_manager` 由 `g_init_manager_count` 引用计数共享（u8-l1 详讲）。

## 7. 下一步学习建议

- **下一讲 u2-l3（Bootstrap 控制面：Config Store 与 TCP 星型拓扑）**：本讲我们把 `aclshmemi_bootstrap_init` 当黑盒，下一讲打开它——dlopen 插件加载、PE 0 的 KV 服务、TCP 星型建链与基于键值表的 barrier/allgather 实现。
- **u2-l4 / u2-l5（对称内存堆）**：本讲阶段二止步于 `setup_heap` 的调用点，这两讲深入 `hybm_mem_segment` / `hybm_mem_slice`，弄清 slice 交换与 mmap 如何造就「同偏移」的对称地址。
- **u5-l3 / u5-l7（传输层与 RDMA 多 QP）**：本讲只追踪了 QP 配置从 set 接口到 `TransportOptions` 的「上半程」；这两讲接续「下半程」——传输管理器如何按 qpNum 为每个 peer 建多条 QP、做全组一致性校验，以及 kernel 侧如何按 qp_idx 直驱指定 QP。
- **延伸阅读**：`docs/principles/init_finalize.md` 的 §3.3 分步说明与 `src/host/init/backends/` 下的实现（`shmem_init_backend.cpp`），把本讲的编排层与实现层拼成完整拼图；多实例机制留待 u8-l1。
