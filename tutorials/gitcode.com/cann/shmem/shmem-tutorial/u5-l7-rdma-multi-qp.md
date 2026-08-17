# u5-l7 Ascend950 RDMA 多 QP 机制

## 1. 本讲目标

本讲把本轮新增的「RDMA 多 QP」能力当成一条完整链路来读：从用户在初始化前调用的一行配置，到传输层为每个对端建出 q 条 QP，再到 kernel 里按 `qp_idx` 直驱其中某一条。学完本讲，你应该能够：

1. 掌握 `aclshmemx_set_qp_num` 的配置规则：支持哪些引擎、取值范围 `[1, 32]`、必须在整个 SHMEM 实例初始化**之前**调用、实例存活期间冻结、最后一个实例 finalize 后自动复位为 1。
2. 说清 `TransportOptions::RdmaQpConfig` 这份配置如何从进程级全局变量出发，经 `bind_aclshmem_entity` 快照、backend 暂存，最终传进 `RdmaTransportManagerV2::OpenDevice`。
3. 读懂 `device_rdma_transport_manager_v2` 按 `qpNum` 建链的全过程：channel 总数公式、稳定命名规则、以及用 bootstrap allgather 做的「全组 QP 数一致性校验」为什么会直接导致初始化失败。
4. 在 kernel 中正确使用 QP-specific ROCE 接口 `aclshmemx_roce_qp_put_nbi / qp_get_nbi / qp_quiet`：理解 `qp_idx` 到具体 SQ 上下文的索引公式 `pe × qp_num + qp_idx`，学会「一核一 QP」的并行写法，并明白普通 `roce_quiet`（遍历全部 QP）与 `roce_qp_quiet`（只等指定 QP）的差别。

## 2. 前置知识

本讲是 advanced 层次，直接建立在三讲之上，先快速回顾要用到的结论：

- **QP 与 RC 语义**（u5-l3）：QP（Queue Pair）是 RoCE 可靠连接的一组队列，包含发送队列 SQ、接收队列 RQ 与完成队列 CQ。RC 模型下同一个 SQ 上的 WQE（工作队列元素）**按提交顺序处理**——这正是单 QP 成为小消息瓶颈的原因：所有核的请求挤一条队列，深度有限的在途窗口限制了并行度。开 q 条 QP 就是给这个对端开 q 条互不影响流水线。
- **V1/V2 两版 RDMA 传输管理器**（u5-l3）：`TT_HCCP`（RoCE 引擎）在编译期分派两版实现，`RdmaTransportManagerV2` 面向 Ascend950，走 HCOMM 的 endpoint/channel 模型建链；本讲的多 QP 逻辑全部在 V2 中实现。
- **engine 直驱层与 QP-specific 接口**（u5-l6）：`include/device/gm2gm/engine/` 下的接口绕过高阶封装直接驱动引擎；其中 QP-specific ROCE 接口显式多一个 `qp_idx` 参数，仅 XSCALE（云脉）后端支持（编译期由 `ACLSHMEMI_RDMA_K_BACKEND_XSCALE` 宏锁定，非 XSCALE 构建中 `qpNum != 1` 会被直接拒绝）。
- **bootstrap allgather**（u2-l3）：初始化第一阶段建立的 CPU 控制面提供 `allgather` 集合通信（Config Store 模式下靠 KV 表 APPEND 实现）。本讲的 QP 数一致性校验就复用这条控制面。
- **配置是进程级的**（u5-l1）：`aclshmemx_set_qp_num` 写的是进程级全局配置，SHMEM 多实例共享同一份 QP 数；因此取值必须在实例存活期间冻结，这一点与 `TransportOptions` 的「bind 时快照」语义配合理解。

一个贯穿全讲的记号：设 PE 总数为 \( n \)、每对连接的 QP 数为 \( q \)，则本 rank 需要建的 channel 总数为

\[ C = (n-1)\,q \]

而 device 侧定位某条发送队列上下文的线性索引为

\[ \mathrm{idx}(pe, k) = pe \cdot q + k,\quad k \in [0, q) \]

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/host/init/shmem_host_init.h](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host/init/shmem_host_init.h) | `aclshmemx_set_qp_num` 的公开声明与契约注释（引擎约束、冻结规则、全组一致要求） |
| [include/host/shmem_host_def.h](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host/shmem_host_def.h) | `ACLSHMEM_MAX_QP_NUM = 32`：QP 数统一上限 |
| [src/host/init/shmem_init.cpp](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp) | `aclshmemx_set_qp_num` 实现、进程级配置变量、init 时快照下发与冻结、finalize 后复位 |
| [src/host/init/backends/shmem_init_backend.cpp](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/backends/shmem_init_backend.cpp) | `bind_aclshmem_entity` 暂存 QP 数，创建 entity 时填入 `TransportOptions` |
| [src/host/transport/transport_def.h](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/transport_def.h) | `TransportOptions` 与内嵌的 `RdmaQpConfig` / `UdmaQpConfig`：控制面配置进入传输层的唯一载体 |
| [src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp) | V2 管理器：`OpenDevice` 读取并校验 qpNum、`Connect` 按 qpNum 建 channel、`CheckQpNumConsistency` 全组校验、`FillRdmaInfo` 下发 device 元数据 |
| [include/device/gm2gm/engine/shmem_device_rdma.h](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/device/gm2gm/engine/shmem_device_rdma.h) | `aclshmemx_roce_qp_put_nbi / qp_get_nbi / qp_quiet` 声明（raw 指针与 Tensor 两种重载） |
| [src/device/gm2gm/engine/shmem_device_rdma.hpp](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/device/gm2gm/engine/shmem_device_rdma.hpp) | kernel 侧实现：SQ 上下文索引计算、QP 版与普通版 quiet 的差异 |
| [src/device/gm2gm/engine/shmemi_device_rdma.h](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/device/gm2gm/engine/shmemi_device_rdma.h) | `aclshmemi_rdma_info`：device 侧 QP 元数据布局（qp_num + 四类队列指针数组） |
| [src/device/shmemi_device_meta.h](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/device/shmemi_device_meta.h) | `aclshmemi_get_qp_info_address`：kernel 取 QP 信息区地址的入口 |
| [tests/unittest/host/mem/rdma_mem/qp_specific_apis_host_test.cpp](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/tests/unittest/host/mem/rdma_mem/qp_specific_apis_host_test.cpp) | host 侧 UT：`set_qp_num(ROCE, 2)` 后拉起四个 QP-specific kernel 用例 |
| [tests/unittest/device/mem/rdma_mem/qp_specific_apis_kernel.cpp](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/tests/unittest/device/mem/rdma_mem/qp_specific_apis_kernel.cpp) | device 侧测试 kernel：「一核一 QP」映射 `qp_idx = peer % qp_num` 的标准范例 |
| [examples/shmem_perftest/rdma_perftest/](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/examples/shmem_perftest/rdma_perftest/README.md) | 本轮重写的性能样例：支持 `-q N -i -1` 多 QP 并行与 `-i K` 单 QP 诊断模式 |

## 4. 核心概念与源码讲解

### 4.1 配置入口：aclshmemx_set_qp_num

#### 4.1.1 概念说明

多 QP 的第一站是一个 Host 侧纯配置接口：`aclshmemx_set_qp_num(engine, qp_num)`。它不建链、不碰设备，只把「每个 peer 连接要建几条 QP」这个数字写进进程级全局配置。要理解它的三条硬规则：

1. **引擎白名单**：本版本仅支持 `ACLSHMEM_DATA_OP_ROCE` 与 `ACLSHMEM_DATA_OP_UDMA` 两个引擎（后者走结构完全同构的 `UdmaQpConfig`，本讲不展开）。
2. **取值范围**：\( 1 \le qp\_num \le \texttt{ACLSHMEM\_MAX\_QP\_NUM} = 32 \)。q=1 就是默认的单 QP 行为，完全向后兼容。
3. **生命周期约束**：必须在**没有任何 SHMEM 实例存活**时调用（通常放在 `aclshmemx_init_attr` 之前）；任一实例初始化成功后配置冻结、再调用返回错误；直到**最后一个**实例 finalize 之后才复位为 1 并可重新配置。

为什么必须「全组一致」？因为 device 元数据区是按 \( n \times q \) 的定长数组布局的（见 4.3），两个 PE 的 q 不同会导致数组错位——这不是性能建议，而是正确性前提，传输层会用 allgather 强制校验（见 4.3.3）。

#### 4.1.2 核心流程

```text
用户程序（初始化前）
  └─ aclshmemx_set_qp_num(ACLSHMEM_DATA_OP_ROCE, q)
       ├─ 加锁 g_aclshmem_ctx_mutex（与 init/finalize 串行化，进程内线程安全）
       ├─ 校验 1 ≤ q ≤ 32            → 失败返回 ACLSHMEM_INVALID_VALUE
       ├─ 检查 g_qp_config_frozen     → 已冻结返回 ACLSHMEM_NOT_SUPPORTED
       ├─ engine == ROCE → g_rdma_qp_config.qpNum = q
       ├─ engine == UDMA → g_udma_qp_config.qpNum = q
       └─ 其他引擎      → 返回 ACLSHMEM_NOT_SUPPORTED

初始化成功（aclshmemi_init_attr_impl 末尾）
  └─ g_qp_config_frozen = true        ← 配置冻结

最后一个实例 finalize 完成
  └─ g_rdma_qp_config / g_udma_qp_config 复位为默认(qpNum=1)
     g_qp_config_frozen = false       ← 解冻，可重新配置
```

#### 4.1.3 源码精读

公开声明与契约注释在 [include/host/init/shmem_host_init.h:L121-L134](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host/init/shmem_host_init.h#L121-L134)——注释明确写了「实例存活期间冻结、最后一个实例 finalize 后复位为 1、全组必须一致，不一致会产生不兼容的元数据布局」：

```c
ACLSHMEM_HOST_API int aclshmemx_set_qp_num(data_op_engine_type_t engine, uint32_t qp_num);
```

上限常量在 [include/host/shmem_host_def.h:L34](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host/shmem_host_def.h#L34)：`constexpr uint32_t ACLSHMEM_MAX_QP_NUM = 32;`。

实现体在 [src/host/init/shmem_init.cpp:L615-L637](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L615-L637)——一次锁 + 三段校验/分派，注意冻结检查在引擎分派**之前**，所以即使传错引擎，冻结状态下也先报「不能改配置」：

```cpp
int aclshmemx_set_qp_num(data_op_engine_type_t engine, uint32_t qp_num)
{
    std::lock_guard<std::mutex> lock(g_aclshmem_ctx_mutex);
    if (!is_valid_rdma_qp_num(qp_num)) {          // 1 ≤ q ≤ 32
        return ACLSHMEM_INVALID_VALUE;
    }
    if (g_qp_config_frozen) {                     // 实例存活期间冻结
        return ACLSHMEM_NOT_SUPPORTED;
    }
    if (engine == ACLSHMEM_DATA_OP_ROCE) {
        g_rdma_qp_config.qpNum = qp_num;
    } else if (engine == ACLSHMEM_DATA_OP_UDMA) {
        g_udma_qp_config.qpNum = qp_num;
    } else {
        return ACLSHMEM_NOT_SUPPORTED;
    }
    return ACLSHMEM_SUCCESS;
}
```

两个进程级变量的定义在 [src/host/init/shmem_init.cpp:L103-L105](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L103-L105)（`g_udma_qp_config`、`g_rdma_qp_config`、`g_qp_config_frozen` 三个 static 全局）；取值校验函数是 [src/host/init/shmem_init.cpp:L176](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L176) 的一行 `is_valid_rdma_qp_num`。

冻结与复位的两个时刻：init 全链路成功后置位 [src/host/init/shmem_init.cpp:L1039](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L1039)（`g_qp_config_frozen = true;`，注意它在 `init_succeeded = true` 之后，失败回滚路径不会留下冻结状态）；finalize 中仅当 `g_init_manager_count` 归零（最后一个实例）才复位 [src/host/init/shmem_init.cpp:L1153-L1157](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L1153-L1157)：

```cpp
if (is_last_instance) {
    g_rdma_qp_config = shm::transport::TransportOptions::RdmaQpConfig{}; // qpNum 回到 1
    g_udma_qp_config = shm::transport::UdmaQpConfig{};
    g_qp_config_frozen = false;
}
```

#### 4.1.4 代码实践

**实践目标**：不看答案，仅凭源码写出 `aclshmemx_set_qp_num` 在四种调用场景下的返回值，验证你对生命周期规则的理解。

**操作步骤**：

1. 阅读 [include/host/init/shmem_host_init.h:L121-L134](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host/init/shmem_host_init.h#L121-L134) 的注释与 [src/host/init/shmem_init.cpp:L615-L637](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L615-L637) 的实现。
2. 对以下四个场景分别推断返回值：
   - 场景 A：`aclshmemx_init_attr` 之前调用 `set_qp_num(ACLSHMEM_DATA_OP_ROCE, 4)`；
   - 场景 B：init 成功之后再调用 `set_qp_num(ACLSHMEM_DATA_OP_ROCE, 4)`；
   - 场景 C：init 之前调用 `set_qp_num(ACLSHMEM_DATA_OP_ROCE, 0)`；
   - 场景 D：init 之前调用 `set_qp_num(ACLSHMEM_DATA_OP_SDMA, 4)`。
3. 再看 [src/host/init/shmem_init.cpp:L1153-L1157](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L1153-L1157)，回答：双实例场景下 finalize 掉实例 1（还剩实例 0 存活）后，能否重新 `set_qp_num`？

**需要观察的现象 / 预期结果**：A 成功（0）；B 返回 `ACLSHMEM_NOT_SUPPORTED`（冻结）；C 返回 `ACLSHMEM_INVALID_VALUE`（越界）；D 返回 `ACLSHMEM_NOT_SUPPORTED`（引擎不在白名单）。双实例问题：不能，`is_last_instance` 为假时不复位、不解冻。以上为源码推断结论；若在有 XSCALE 后端的 NPU 环境运行真实程序验证，属**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么把 `qp_num` 上限设为 32 而不是任意大？
**答案**：每条 QP 要占用独立的 SQ/RQ/SCQ/RCQ 队列与 device 元数据数组空间，总占用按 \( n \times q \) 线性增长（见 4.3.3 的 `ReserveRdmaInfoSpace`）；32 条在收益（并行流水线数）与资源（队列内存、NIC 连接数、建链时间）之间取平衡，同时 32 是 2 的幂，便于取模映射。

**练习 2**：`set_qp_num` 为什么要在内部加 `g_aclshmem_ctx_mutex` 锁？
**答案**：它是进程级全局配置，而初始化/终结会在主流程读取并快照这份配置（bind 时）。锁把「配置写入」与 init/finalize 串行化，避免一个线程正在 bind 快照 `g_rdma_qp_config`、另一个线程同时改写它造成竞态；这也是头文件注释声明「线程安全且与初始化/终结串行化」的实现依据。

### 4.2 配置传递链路：RdmaQpConfig 从全局变量到传输管理器

#### 4.2.1 概念说明

`set_qp_num` 只是写了进程级全局变量，真正消费它的是传输层。中间隔着两层中转：

- **`TransportOptions`** 是「控制面配置进入传输层的唯一载体」（回顾 u5-l1），本轮在其内部新增了内嵌结构 `RdmaQpConfig{ uint32_t qpNum{1}; }`，与既有的 `UdmaQpConfig` 同构。
- **init backend 的 `entity_member`** 充当快照暂存区：bind 时把全局配置抄进 per-instance 的成员，创建 entity 时再填进 `TransportOptions`。这样即使全局变量之后被复位，已建实例的传输层行为也不受影响（配合 4.1 的冻结规则，存活期内本来也不允许改）。

#### 4.2.2 核心流程

```text
g_rdma_qp_config.qpNum = q                 (4.1: set_qp_num 写入)
        │
        ▼  aclshmemi_init_attr_impl
bind_aclshmem_entity(..., g_udma_qp_config, g_rdma_qp_config.qpNum)
        │  快照进 entity_member
        ▼
elem->rdma_qp_num = q                      (backend 暂存，随实例存活)
        │  创建 entity（InitTransManager 路径）
        ▼
transport_options.rdmaQpConfig.qpNum = elem->rdma_qp_num
        │  hybm_create_entity_with_transport_options
        ▼
RdmaTransportManagerV2::OpenDevice(options)
        └─ qpNum_ = options.rdmaQpConfig.qpNum   (4.3 消费)
```

#### 4.2.3 源码精读

载体结构定义在 [src/host/transport/transport_def.h:L46-L70](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/transport_def.h#L46-L70)——`TransportOptions` 内嵌 `RdmaQpConfig` 与 `UdmaQpConfig`，默认 `qpNum=1`；`operator<<` 也把两个 QP 数打进了日志，方便在建链日志里直接核对配置：

```cpp
struct TransportOptions {
    uint32_t rankId;
    uint32_t rankCount;
    ...
    struct RdmaQpConfig {
        uint32_t qpNum{1};
    } rdmaQpConfig{};
    ...
    UdmaQpConfig udmaQpConfig{};
};
```

init 主流程把全局配置交给 backend 的调用点在 [src/host/init/shmem_init.cpp:L1002-L1005](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L1002-L1005)：`init_manager->bind_aclshmem_entity(attributes, &g_state, &g_boot_handle, ..., g_udma_qp_config, g_rdma_qp_config.qpNum)`。

backend 侧的签名与暂存在 [src/host/init/backends/shmem_init_backend.cpp:L78-L98](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/backends/shmem_init_backend.cpp#L78-L98)——`elem->udma_qp_num = udma_qp_config.qpNum; elem->rdma_qp_num = rdma_qp_num;` 两行完成快照。

真正填进 `TransportOptions` 的位置在 [src/host/init/backends/shmem_init_backend.cpp:L255-L261](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/backends/shmem_init_backend.cpp#L255-L261)，紧接着同一函数内会把 `transport_options` 传给 `hybm_create_entity_with_transport_options` 创建内存实体并拉起传输层：

```cpp
shm::transport::TransportOptions transport_options;
transport_options.rankId = attributes->my_pe;
transport_options.rankCount = attributes->n_pes;
...
transport_options.udmaQpConfig.qpNum = elem->udma_qp_num;
transport_options.rdmaQpConfig.qpNum = elem->rdma_qp_num;
```

#### 4.2.4 代码实践

**实践目标**：不看本节流程图，独立用 grep 串出「全局配置 → 传输管理器成员变量」的完整传递链，并记录每一跳的文件与行号。

**操作步骤**：

1. 执行 `grep -rn "rdmaQpConfig" src/` 与 `grep -rn "rdma_qp_num" src/`。
2. 对每一处命中判断它属于链路的哪一跳：写入（`set_qp_num`）、快照（bind）、填载体（backend）、消费（`OpenDevice`）。
3. 把结果整理成「文件:行号 → 角色」的清单，再与 4.2.2 的流程图对照。
4. 附加一问：若把 `set_qp_num` 放在 init 之后再调用（必然失败），`elem->rdma_qp_num` 会受影响吗？

**需要观察的现象 / 预期结果**：grep 应恰好命中四个角色各一处消费点（`shmem_init.cpp` 写入与传参、`shmem_init_backend.cpp` 暂存与填载体、`device_rdma_transport_manager_v2.cpp` 消费）；第 4 问答案是不受影响——bind 的快照发生在 init 主流程中，失败调用在冻结检查处就被拦截，根本写不到全局变量。本实践为纯源码阅读，不依赖运行环境。

#### 4.2.5 小练习与答案

**练习 1**：为什么不把 `qpNum` 直接放进 `hybm_options`，而要单独设计 `TransportOptions::RdmaQpConfig`？
**答案**：职责分离——`hybm_options` 描述内存实体（堆类型、引擎位掩码、VA 空间），`TransportOptions` 描述传输层建链参数（rank、NIC、QP 数）。QP 数只被传输管理器消费，放进 `TransportOptions` 让传输层无需感知 HYBM 内存语义；同时内嵌结构体（而非散的 uint32 字段）便于日后按引擎成组扩展（UDMA 已复用同一模式）。

**练习 2**：多实例场景下，实例 0 建链用 q=4、之后新建实例 1，实例 1 能用不同的 q 吗？
**答案**：不能。配置是进程级的：实例 0 存活期间 `g_qp_config_frozen = true`，`set_qp_num` 被拒绝；实例 1 bind 时快照到的仍是同一份 q=4。只有全部实例 finalize 后配置才复位。

### 4.3 建链与一致性校验：device_rdma_transport_manager_v2

#### 4.3.1 概念说明

`RdmaTransportManagerV2` 是多 QP 的「施工队」。它在三个时机消费 `qpNum`：

1. **`OpenDevice`**：读取并二次校验（范围 + 后端限制），存入成员 `qpNum_`。
2. **`Connect`**：先做全组 QP 数一致性校验，然后为每个远端 rank 建 q 条 channel——总数 \( C = (n-1)q \)，每条 channel 挂 1 个 SQ + 1 个 CQ（`roceAttr.queueNum = 1`）。
3. **`FillRdmaInfo`**：把所有 channel 的 SQ/CQ 上下文、MR 密钥组装成 `AiQpRMAQueueInfo`（device 侧即 `aclshmemi_rdma_info`）下发到 device，队列数组按 \( \mathrm{idx} = pe \cdot q + k \) 排布。

一个容易忽略的平台约束：多 QP **仅 XSCALE 后端**支持。`OpenDevice` 中用编译期宏拦截——非 XSCALE 构建里 `qpNum != 1` 直接返回 `ACLSHMEM_NOT_SUPPORTED`；device 侧的 `aclshmemx_roce_qp_*` 实现里也有对应的 `static_assert`（见 4.4.3）。也就是说 Ascend950 平台上，多 QP 是否可用由编译期选择的 RDMA 后端（XSCALE/云脉 vs HNS_1825）决定。

**为什么多 QP 能提带宽**：RC 连接保证同一 SQ 上的 WQE 按序完成，单 QP 时所有 AIV 核的请求共用一条深度有限的在途窗口，小消息场景下提交带宽成为瓶颈；q 条 QP 提供 q 条独立流水线，理论聚合吞吐近似 \( \min(q \cdot B_1,\ B_{\mathrm{NIC}}) \)，其中 \( B_1 \) 为单 QP 吞吐、\( B_{\mathrm{NIC}} \) 为网卡线速。

#### 4.3.2 核心流程

```text
OpenDevice(options)
  ├─ qpNum_ = options.rdmaQpConfig.qpNum
  ├─ 校验 0 < qpNum_ ≤ 32                     → 否则 INVALID_PARAM
  ├─ 非 XSCALE 构建且 qpNum_ != 1              → NOT_SUPPORTED（编译期拦截）
  └─ CreateEndpoint + 取监听端口 + 预留 device 元数据区

Connect()
  ├─ CheckQpNumConsistency()                   ← bootstrap allgather 全组校验
  ├─ channelNum = (rankCount-1) × qpNum_       ← 溢出检查
  ├─ for remoteRank (跳过自己):
  │    for qpIdx in [0, qpNum_):
  │       填 HcommChannelDesc：RoCE 地址、双 memHandle(主堆+atomic)、
  │         queueNum=1、server/client 按 rankId 大小分派、
  │         channelName = "aclshmem-r<小id>-r<大id>-q<qpIdx>"
  ├─ HcommChannelCreate 一次性建 channelNum 条（COMM_ENGINE_AIV）
  ├─ 轮询全部 channel 状态至 READY（超时按 channelNum 缩放）
  └─ FillRdmaInfo() 组装 AiQpRMAQueueInfo 并拷贝到 device
```

#### 4.3.3 源码精读

**OpenDevice 读取与双重校验**，[src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp:L265-L278](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp#L265-L278)：

```cpp
rankId_ = options.rankId;
rankCount_ = options.rankCount;
role_ = options.role;
qpNum_ = options.rdmaQpConfig.qpNum;          // 消费 4.2 传来的配置
if (qpNum_ == 0 || qpNum_ > ACLSHMEM_MAX_QP_NUM) {
    return ACLSHMEM_INVALID_PARAM;            // 传输层兜底再校验一次
}
#if !defined(ACLSHMEMI_RDMA_K_BACKEND_XSCALE)
if (qpNum_ != 1U) {
    return ACLSHMEM_NOT_SUPPORTED;            // 多 QP 仅 XSCALE
}
#endif
```

**全组一致性校验**，[src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp:L728-L753](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp#L728-L753)——复用初始化第一阶段的 `g_boot_handle.allgather`（u2-l3 的控制面）把各 PE 的 `qpNum_` 收拢成数组逐一比对，任何一个不一致都返回 `ACLSHMEM_INVALID_PARAM`，init 因此失败。单 rank 场景直接跳过：

```cpp
Result RdmaTransportManagerV2::CheckQpNumConsistency() const
{
    if (rankCount_ <= 1) { return ACLSHMEM_SUCCESS; }
    if (g_boot_handle.allgather == nullptr) { return ACLSHMEM_INNER_ERROR; }

    std::vector<uint32_t> allQpNums(rankCount_, 0);
    auto ret = g_boot_handle.allgather(&qpNum_, allQpNums.data(), sizeof(qpNum_), &g_boot_handle);
    ...
    for (uint32_t rank = 0; rank < rankCount_; ++rank) {
        if (allQpNums[rank] != qpNum_) {
            SHM_LOG_ERROR("... inconsistent rdma qp num: local=" << qpNum_ << ", rank[" << rank << "]=" << ...);
            return ACLSHMEM_INVALID_PARAM;
        }
    }
    return ACLSHMEM_SUCCESS;
}
```

**按 qpNum 建 channel**，[src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp:L552-L564](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp#L552-L564)（总数计算与溢出防护）与 [src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp:L590-L642](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp#L590-L642)（双层循环：外层遍历远端 rank、内层遍历 qpIdx）。关键片段：

```cpp
const uint64_t peerCount = static_cast<uint64_t>(rankCount_ - 1);
const uint64_t channelNum64 = peerCount * static_cast<uint64_t>(qpNum_);
...
const bool isServer = (rankId_ < remoteRank);          // 小 id 当 server（与 u5-l3 规则一致）
for (uint32_t qpIdx = 0; qpIdx < qpNum_; ++qpIdx) {
    auto& desc = channelDescs[chIdx];
    ...
    desc.roceAttr.queueNum = 1;                        // 每 channel 恰 1 个 SQ + 1 个 CQ
    desc.role = isServer ? HCOMM_SOCKET_ROLE_SERVER : HCOMM_SOCKET_ROLE_CLIENT;
    desc.port = isServer ? devicePort_ : remotePort;
    channelNames_[chIdx] = BuildStableChannelName(rankId_, remoteRank, qpIdx);
    ++chIdx;
}
```

**稳定命名**，[src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp:L41-L46](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp#L41-L46)：`"aclshmem-r<serverRank>-r<clientRank>-q<qpIdx>"`——两端独立计算得到同一名字，且与枚举顺序无关，这就是「稳定」的含义。

**device 元数据的内存布局**由 [src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp:L1094-L1128](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp#L1094-L1128) 预留，大小按 \( n \times q \) 线性增长：

```cpp
auto oneQpSize = 2U * (sizeof(AiQpRMAWQ) + sizeof(AiQpRMACQ)) + sizeof(RdmaMemRegionInfo);
const uint64_t qpEntryCount = static_cast<uint64_t>(rankCount_) * qpNum_;
qpInfoSize_ = sizeof(AiQpRMAQueueInfo) + oneQpSize * qpEntryCount;
```

**队列数组的排布与索引**，[src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp:L836-L844](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp#L836-L844)：`count = qpNum_`，sq/rq/scq/rcq 四个数组各 \( n \times q \) 项、mr 数组按 rank 一项。填充时对每个远端 rank 的每条 QP 写入对应槽位，[src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp:L966-L967](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp#L966-L967)（channel 数组索引：`remoteOrdinal * qpNum_ + qpIdx`，其中 `remoteOrdinal` 是去掉自己之后的序号）与 [src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp:L1035](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp#L1035)（SQ 上下文索引：`remoteRank * qpNum_ + qpIdx`，**按 rank 编号**不压缩自己）：

```cpp
CopyAiWQInfo(copyInfo->sq[static_cast<uint64_t>(remoteRank) * qpNum_ + qpIdx], sqContexts[0]);
```

注意这两个索引公式不同：channel 顺序数组要跳过自己，SQ/RQ/CQ 上下文数组给自己也留了槽（本 rank 槽位用于自环读写）。最终 `FillRdmaInfo`（[src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp:L865-L889](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_rdma/device_rdma_transport_manager_v2.cpp#L865-L889)）用一次 `AclrtMemcpy` 把整块元数据拷入 device，kernel 侧经 `aclshmemi_get_qp_info_address(0)`（[src/device/shmemi_device_meta.h:L73-L82](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/device/shmemi_device_meta.h#L73-L82)）拿到它并按 `aclshmemi_rdma_info` 布局解释。

#### 4.3.4 代码实践

**实践目标**：画出「2 节点 × 每节点 2 PE、q=2」场景下 PE0 视角的 channel 与 SQ 上下文布局图，并推演 QP 数不一致时的失败行为。

**操作步骤**：

1. 按 `channelNum = (rankCount-1) × qpNum` 算出 PE0 要建的 channel 数（这里 rankCount=4，应为 6）。
2. 列表写出 6 条 channel 的名字（套用 `BuildStableChannelName`）与 server/client 角色、PE0 视角的角色分派（PE0 对所有远端都是小 id，应全为 server）。
3. 写出 SQ 上下文数组（4×2=8 项）中索引 0、3、7 对应的 (pe, qp_idx)。
4. 推演：若 PE0 设 q=2、PE1 设 q=1，`CheckQpNumConsistency` 在哪个 PE 上报错、错误码是什么、init 的最终结果如何。

**需要观察的现象 / 预期结果**：channel 名形如 `aclshmem-r0-r1-q0/q1`、`aclshmem-r0-r2-q*`、`aclshmem-r0-r3-q*`；SQ 索引 0=(pe0,k0)、3=(pe1,k1)、7=(pe3,k1)；不一致场景中**每个 PE** 都会在自己的 `CheckQpNumConsistency` 里发现某 rank 与本地不同（例如 PE0 发现 rank1=1≠2），返回 `ACLSHMEM_INVALID_PARAM`，该错误沿建堆调用链上抛导致所有 PE 初始化失败——这正是「元数据布局不兼容必须拦截在 init 阶段」的体现。建链日志（`SHM_LOG_INFO` 打印每条 channel 的 rank/qp/role/port/name）可在真实环境核对，属**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：q 从 1 提到 8、n=16 时，每个 rank 的 channel 数与 device 元数据中 SQ 项数各是多少？
**答案**：channel 数 \( C = (16-1) \times 8 = 120 \)；SQ 数组项数 \( n \times q = 16 \times 8 = 128 \)。两者不同因为 channel 只建在对端（不含自己），而 SQ/RQ/CQ 上下文数组给自己也留了槽位。

**练习 2**：为什么一致性校验放在 `Connect` 开头而不是 `OpenDevice` 里？
**答案**：`OpenDevice` 只看本地 options，属于纯本地阶段；而校验需要全组通信（allgather），必须等 bootstrap 控制面就绪且各 rank 的网络信息已通过 `Prepare` 交换之后。`Connect` 是建链的集体性阶段，把 allgather 放在这里能保证在任何一端真正发起 HCOMM 建链**之前**拦截不一致，避免两端按不同 q 建出对不上的 channel 再超时失败。

**练习 3**：`HcommChannelCreate` 一次性提交 120 条 channel 与逐条创建相比有什么好处？
**答案**：一次调用让 HCOMM 内部并行握手、批量分配队列资源，建链延迟近似与 q 线性而非平方增长；配合按 channel 数缩放的轮询超时（`CHANNEL_STATUS_POLL_TIMEOUT_PER_CH_MS × channelNum`），多 QP 场景的建链总时长可控。

### 4.4 kernel 直驱：aclshmemx_roce_qp_* 接口

#### 4.4.1 概念说明

配置与建链都就绪后，最后一公里在 kernel 里。u5-l6 已经从「接口分层」角度介绍过 QP-specific 接口，本讲聚焦它的**机制细节**：

- 三个接口 `aclshmemx_roce_qp_put_nbi` / `aclshmemx_roce_qp_get_nbi` / `aclshmemx_roce_qp_quiet`，每个都有 raw `__gm__*/__ubuf__*` 指针与 `GlobalTensor/LocalTensor` 两种重载，均比普通版多一个 `qp_idx` 参数（取值须小于配置的 QP 数）。
- kernel 侧定位队列上下文靠一条公式：从 device 元数据区取 `aclshmemi_rdma_info`（含 `qp_num` 与 SQ 数组指针），目标 SQ 的地址为 `sq_ptr + (pe * qp_num + qp_idx) * sizeof(aclshmemi_rdma_sq_ctx)`。
- 普通 `aclshmemx_roce_put_nbi` 内部就是把 `qp_idx` 写死为 0——QP 版与普通版共享同一条 `aclshmemi_roce_write` 底层路径，差异只在队列选择；quiet 则分叉成两个实现：普通版 `roce_quiet` **循环等全部 q 条 QP**，QP 版 `roce_qp_quiet` 只轮询指定那条的 CQ。

#### 4.4.2 核心流程

```text
kernel 内发起一次 QP 直驱 put：
  1. rdma_info = aclshmemi_qp_info_fetch()        ← 元数据区（含 qp_num）
  2. sq_ctx = sq_ptr + (pe*qp_num + qp_idx)*sizeof(ctx)
  3. 在 UB 上拼 WQE → 写入该 SQ 的环形缓冲 → 敲 doorbell
  4. aclshmemx_roce_qp_quiet(pe, qp_idx, buf, sync_id)
       读该 SQ 的 producer index → dcci 刷缓存
       → 只轮询 (pe, qp_idx) 对应 CQ 直到完成
```

多核并行的标准姿势是「一核一 QP」：block b 只处理 `qp_idx == b` 的传输任务，block 数 ≥ qp_num（多余的 block 直接空转）。UT kernel 用的映射是 `qp_idx = peer % qp_num`，perftest 用的是 `selected_qp = multi_core ? block_id : op_idx % qp_num`（多核按块号、单核按操作序号轮询）。

#### 4.4.3 源码精读

接口声明的三组位置（raw 与 Tensor 重载紧邻）：

- get：[include/device/gm2gm/engine/shmem_device_rdma.h:L68-L111](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/device/gm2gm/engine/shmem_device_rdma.h#L68-L111)，注释明确「仅 XSCALE 后端支持」并警告 RDMA 下同一 PE 的并发 RMA/AMO 不受支持；
- put：[include/device/gm2gm/engine/shmem_device_rdma.h:L300-L340](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/device/gm2gm/engine/shmem_device_rdma.h#L300-L340)；
- quiet：[include/device/gm2gm/engine/shmem_device_rdma.h:L491-L527](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/device/gm2gm/engine/shmem_device_rdma.h#L491-L527)，普通版语义是「等该 PE 全部 WQE 完成」，QP 版是「只等指定 QP」。

device 元数据结构与取址入口：[src/device/gm2gm/engine/shmemi_device_rdma.h:L24-L31](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/device/gm2gm/engine/shmemi_device_rdma.h#L24-L31) 定义 `aclshmemi_rdma_info{ qp_num; sq_ptr; rq_ptr; scq_ptr; rcq_ptr; mem_ptr; }`（队列数组尺寸都是 `[PE_NUM][qp_num]`），取址函数在 [src/device/gm2gm/engine/shmem_device_rdma.hpp:L34-L38](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/device/gm2gm/engine/shmem_device_rdma.hpp#L34-L38)：

```cpp
ACLSHMEM_DEVICE __gm__ aclshmemi_rdma_info* aclshmemi_qp_info_fetch()
{
    return (__gm__ aclshmemi_rdma_info*)(aclshmemi_get_qp_info_address(0));
}
```

**两个 quiet 的实现差异**，[src/device/gm2gm/engine/shmem_device_rdma.hpp:L58-L86](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/device/gm2gm/engine/shmem_device_rdma.hpp#L58-L86)。QP 版定位单条 SQ 后只轮询它的 CQ；普通版外面包了一层 `for (qp_idx = 0; qp_idx < qp_num; qp_idx++)` 把该 PE 的全部 QP 逐条等完：

```cpp
// QP 版：只等 (pe, qp_idx) 这一条
__gm__ aclshmemi_rdma_sq_ctx* sq_context =
    (__gm__ aclshmemi_rdma_sq_ctx*)(rdma_info->sq_ptr + (pe * qp_num + qp_idx) * sizeof(aclshmemi_rdma_sq_ctx));
auto sq_pi_addr = sq_context->head_addr;
dcci_cachelines((__gm__ uint8_t*)sq_pi_addr, 8);
uint32_t cur_head = *(__gm__ uint32_t*)(sq_pi_addr);
aclshmemi_roce_poll_cq<...>(pe, qp_idx, cur_head, ...);

// 普通版：循环等该 pe 的全部 qp_num 条
for (uint32_t qp_idx = 0; qp_idx < qp_num; qp_idx++) { ...同上... }
```

**普通 put 把 qp_idx 写死为 0**，[src/device/gm2gm/engine/shmem_device_rdma.hpp:L401-L417](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/device/gm2gm/engine/shmem_device_rdma.hpp#L401-L417)（`aclshmemi_roce_write(..., pe, 0, ...)`），QP 版把参数透传并用 `static_assert` 锁死 XSCALE 后端，[src/device/gm2gm/engine/shmem_device_rdma.hpp:L419-L438](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/device/gm2gm/engine/shmem_device_rdma.hpp#L419-L438)：

```cpp
template <typename T>
ACLSHMEM_DEVICE void aclshmemx_roce_qp_put_nbi(
    __gm__ T* dst, __gm__ T* src, __ubuf__ T* buf, uint32_t elem_size, int pe, uint32_t qp_idx, uint32_t sync_id)
{
    static_assert(ACLSHMEMI_K_RDMA_BACKEND == aclshmemi_rdma_backend_t::XSCALE || sizeof(T) == 0,
                  "aclshmemx_roce_qp_put_nbi only supports XSCALE backend");
    ...
    aclshmemi_roce_write((__gm__ uint8_t*)ptr, (__gm__ uint8_t*)src, pe, qp_idx, elem_size * sizeof(T), ...);
}
```

**「一核一 QP」的测试范例**，[tests/unittest/device/mem/rdma_mem/qp_specific_apis_kernel.cpp:L45-L90](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/tests/unittest/device/mem/rdma_mem/qp_specific_apis_kernel.cpp#L45-L90)（put 用例）。`qp_num` 直接从 device 元数据读（[L35-L42](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/tests/unittest/device/mem/rdma_mem/qp_specific_apis_kernel.cpp#L35-L42) 的 `get_qp_num_or_zero`），块激活判断在 [L24](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/tests/unittest/device/mem/rdma_mem/qp_specific_apis_kernel.cpp#L24)（`GetBlockIdx() < qp_num`）：

```cpp
for (int64_t peer = 0; peer < pe_size; ++peer) {
    if (peer == my_pe) { continue; }
    uint32_t qp_idx = static_cast<uint32_t>(peer % qp_num);   // peer 到 QP 的哈希映射
    if (qp_idx != AscendC::GetBlockIdx()) { continue; }        // 本块只管自己那条 QP
    aclshmemx_roce_qp_put_nbi(dst_addr, src_addr, ..., static_cast<int>(peer), qp_idx, sync_id);
    aclshmemx_roce_qp_quiet(static_cast<uint32_t>(peer), qp_idx, ..., sync_id);
}
roce_qp_barrier_all(ub_local, sync_id);                        // kernel 级 barrier 收尾
```

配套 host 侧用例 [tests/unittest/host/mem/rdma_mem/qp_specific_apis_host_test.cpp:L76-L102](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/tests/unittest/host/mem/rdma_mem/qp_specific_apis_host_test.cpp#L76-L102)：init 前设 `set_qp_num(ROCE, 2)`（[L79](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/tests/unittest/host/mem/rdma_mem/qp_specific_apis_host_test.cpp#L79)），随后跑 put/get × raw/Tensor 四个 kernel，校验每个 PE 的缓冲区里都能看到所有 rank 的数据段；非 XSCALE 构建整个用例 `GTEST_SKIP`（[L28-L32](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/tests/unittest/host/mem/rdma_mem/qp_specific_apis_host_test.cpp#L28-L32)、[L105-L109](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/tests/unittest/host/mem/rdma_mem/qp_specific_apis_host_test.cpp#L105-L109)）。

#### 4.4.4 代码实践

**实践目标**：说清测试中 `qp_idx` 如何映射到具体 QP——这是本讲指定的核心实践任务的第一半。

**操作步骤**：

1. 通读 [tests/unittest/device/mem/rdma_mem/qp_specific_apis_kernel.cpp:L45-L90](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/tests/unittest/device/mem/rdma_mem/qp_specific_apis_kernel.cpp#L45-L90) 的 put 实现，追踪三个量：`qp_num` 从哪来、`qp_idx` 怎么算、`GetBlockIdx()` 起什么作用。
2. 对照 [src/device/gm2gm/engine/shmem_device_rdma.hpp:L58-L71](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/device/gm2gm/engine/shmem_device_rdma.hpp#L58-L71)，写出 `(peer=3, qp_idx=1, qp_num=2)` 时 kernel 实际访问的 SQ 上下文索引。
3. 思考并回答：测试要求 `qp_num ≥ 2`（`MIN_QP_NUM`，[L15](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/tests/unittest/device/mem/rdma_mem/qp_specific_apis_kernel.cpp#L15)），若把 host 测试的 `REQUESTED_QP_NUM` 改成 1，kernel 会发生什么？
4. 第二半（性能侧）：在有 XSCALE 后端的双卡环境运行 `examples/shmem_perftest/rdma_perftest/run.sh`，先跑默认单 QP，再跑多 QP 并行模式 `-q 4 -i -1 --metric bw`（脚本参数见 [run.sh:L146-L150](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/examples/shmem_perftest/rdma_perftest/run.sh#L146-L150)），对比多档消息大小下的带宽；无 NPU 环境时改为对比两种模式在 kernel 里的接口调用差异（见下面预期结果最后一条）。

**需要观察的现象 / 预期结果**：

- `qp_num` 来自 `aclshmemi_qp_info_fetch()->qp_num`，即 init 时 `FillRdmaInfo` 下发的 `count` 字段，kernel 无需任何硬编码；`qp_idx = peer % qp_num` 是「peer 哈希到 QP」的映射；`GetBlockIdx()` 让 block b 只处理映射到 b 的那些 peer——「一核一 QP」由此实现，多 block 天然并行、互不抢队列。
- `(peer=3, qp_idx=1, qp_num=2)` 的 SQ 索引为 \( 3 \times 2 + 1 = 7 \)。
- `REQUESTED_QP_NUM=1` 时 `qp_num < MIN_QP_NUM` 成立，kernel 直接 return，输出缓冲保持初始值，host 侧 `check_all_rank_pattern` 断言失败——这正说明该 UT 就是为「多条 QP 真正被不同方向使用」设计的。
- 多 QP 带宽对比：按 [README.md:L88-L96](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/examples/shmem_perftest/rdma_perftest/README.md#L88-L96) 的语义，`-q 4 -i -1` 下 4 个 QP 各自独立传输完整数据量，小消息档位带宽应明显高于单 QP（单 QP 模式下 perftest 还会自动启用 XSCALE 聚合提交优化，见 [rdma_perftest_kernel.cpp:L18-L37](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/examples/shmem_perftest/rdma_perftest/rdma_perftest_kernel.cpp#L18-L37) 中 `qp_num==1` 走普通 `roce_put_nbi`、否则走 `roce_qp_put_nbi` 的分派）。具体数值属**待本地验证**；接口调用差异可以纯源码确认：单 QP 默认模式 = `aclshmemx_roce_put_nbi(..., pe, sync_id)` + `aclshmemx_roce_quiet(pe, buf, sync_id)`（无 qp_idx、quiet 遍历全部 QP），多 QP 并行模式 = host 侧 `set_qp_num(ROCE, N)` + `block_dim = N`（[main.cpp:L141-L142](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/examples/shmem_perftest/rdma_perftest/main.cpp#L141-L142)、[L170](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/examples/shmem_perftest/rdma_perftest/main.cpp#L170)）+ kernel 每个 block 调 `aclshmemx_roce_qp_put_nbi(..., pe, block_id, sync_id)` 与 `aclshmemx_roce_qp_quiet(pe, block_id, ...)`（[rdma_perftest_kernel.cpp:L60-L89](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/examples/shmem_perftest/rdma_perftest/rdma_perftest_kernel.cpp#L60-L89)）。

#### 4.4.5 小练习与答案

**练习 1**：kernel 里把数据发往 `(pe, qp_idx)` 与发往 `(pe, 0)`（普通接口）混用会怎样？
**答案**：功能上可行——两者最终都走 `aclshmemi_roce_write`，只是落在同一 peer 的不同 SQ。但收尾语义要小心：普通 `aclshmemx_roce_quiet(pe, ...)` 会等该 pe 的**全部** QP 完成，所以混用后调一次普通 quiet 即可保证整体完成；若只调 `roce_qp_quiet(pe, 0, ...)`，走其他 QP 的传输不保证完成，读回可能读到旧值。另外头文件警告 RDMA 下同一 PE 的并发 RMA/AMO 不受支持，混用时更要依赖 quiet 划界。

**练习 2**：perftest 的 `rdma_perf_put_nbi` 里 `selected_qp` 有三种取法（`qp_index` 显式指定 / `multi_core` 用 block_id / 否则 `op_idx % qp_num`），分别对应什么使用场景？
**答案**：显式指定（`-i K`）是单 QP 诊断模式，固定走第 K 条 QP 用于对比或排障；`multi_core` 用 block_id 是多 QP 并行模式（`-q N -i -1`），每个 block 独占一条 QP，吞吐最大；单核按 `op_idx % qp_num` 轮询是在一个核上把连续操作轮流撒到多条 QP，缓解单 SQ 的按序提交瓶颈，是不方便开多 block 时的替代写法。

**练习 3**：为什么 `roce_qp_quiet` 实现里轮询 CQ 前要先 `dcci_cachelines` 刷新 SQ 的 head 地址？
**答案**：SQ 的 producer index（head）由网卡硬件更新在 device 全局内存，AICore 核上可能缓存了旧值；`dcci`（data cache clean & invalidate）强制丢弃本地缓存行、重新从 GM 读，保证拿到的 `cur_head` 反映硬件真实进度，否则可能提前认为已完成而返回。

## 5. 综合实践

**任务：给一条「配置→建链→直驱」的完整链路做一次源码导览，并产出单 QP 与多 QP 的对比说明。**

1. **链路导览**（纯阅读，任何环境可做）：从 `aclshmemx_set_qp_num` 出发，依次在源码中标注以下 7 个站点并记录 `文件:行号`：
   `set_qp_num` 写全局 → `bind_aclshmem_entity` 快照 → backend 填 `TransportOptions.rdmaQpConfig` → `RdmaTransportManagerV2::OpenDevice` 校验并保存 → `CheckQpNumConsistency` 全组校验 → `Connect` 双层循环建 \( (n-1)q \) 条 channel → `FillRdmaInfo` 按 \( pe \cdot q + k \) 布局下发 → kernel `aclshmemi_qp_info_fetch` 读回。把 7 个站点画成一张纵向流程图，旁边标注每一步失败时的返回码（INVALID_VALUE / NOT_SUPPORTED / INVALID_PARAM / INNER_ERROR）。
2. **对比实验**（需 XSCALE 后端双卡环境，无则做接口差异整理）：
   - 用 `examples/shmem_perftest/rdma_perftest/run.sh` 分别跑 `--metric bw` 默认单 QP、`-q 4 -i -1` 多 QP 并行、`-q 4 -i 0` 单 QP 诊断三组，消息档位取 `--exponent-range 8 22`，记录各档带宽。
   - 回答两个分析题：(a) 多 QP 相对单 QP 的加速比在哪个消息档位最明显？为什么小消息受益更大？(b) `-q 4 -i 0` 的结果为什么更接近单 QP 默认模式而不是多 QP 模式？
3. **产出**：一份不超过一页的对比说明，包含链路图、三组数据（或接口调用差异表）与两句结论（一句关于正确性——QP 数为什么必须全组一致；一句关于性能——多 QP 的收益来源与适用消息规模）。

预期分析答案：(a) 小消息档位最明显，因为瓶颈在单 SQ 的按序提交/在途窗口而非网卡线速，多 QP 直接放大提交并行度；大消息时单 QP 已能吃满线速，多 QP 收益趋近 0 甚至因分流开销略降。(b) `-i 0` 只启用 0 号 QP 一条流水线，并发度与单 QP 相同（且关闭了聚合提交优化），所以带宽形态接近单 QP；它的价值在于隔离排查某条具体 QP。数值部分待本地验证。

## 6. 本讲小结

- `aclshmemx_set_qp_num(engine, qp_num)` 是多 QP 的唯一配置入口：仅支持 ROCE/UDMA、取值 \([1, 32]\)、必须在 init 前调用，实例存活期间冻结，最后一个实例 finalize 后复位为 1。
- 配置经「全局变量 → `bind_aclshmem_entity` 快照（`elem->rdma_qp_num`）→ `TransportOptions::RdmaQpConfig` → `OpenDevice`」四跳到达传输层，`TransportOptions` 仍是控制面配置进入传输层的唯一载体。
- `RdmaTransportManagerV2::Connect` 用 bootstrap allgather 强制全组 QP 数一致（不一致即 init 失败），随后按 \( (n-1)q \) 条 channel 建链，每条 channel 恰一组 SQ/CQ，命名 `aclshmem-r<server>-r<client>-q<idx>` 保证两端一致。
- device 元数据按 \( pe \cdot q + qp\_idx \) 的线性索引排布 SQ/RQ/SCQ/RCQ 数组；kernel 经 `aclshmemi_qp_info_fetch()` 拿到 `qp_num` 与数组指针，无需任何硬编码。
- QP-specific 接口与普通接口共享底层 `aclshmemi_roce_write/read`，差别只在队列选择：普通版写死 `qp_idx=0`、quiet 遍历全部 QP；QP 版显式传 `qp_idx`、quiet 只等指定队列——「一核一 QP」（`qp_idx = peer % qp_num` 或 `block_id`）是多 QP 并行的标准写法。
- 多 QP 仅 XSCALE（云脉）后端可用，非 XSCALE 构建在 `OpenDevice` 编译期拦截（`qpNum != 1` 返回 NOT_SUPPORTED），device 侧实现另有 `static_assert` 双保险。

## 7. 下一步学习建议

- 下一讲 u8-l6（测试体系）会再次遇到本讲的两个 UT 文件：可以去读 `init_host_test.cpp` 中 `TestSetRdmaQpNumBeforeInit` 一类用例，看 host 侧如何为「配置生命周期」写断言，并尝试为某个尚未覆盖的 QP 相关分支补用例。
- 若想看多 QP/引擎直驱在真实业务形态下的用法，继续 u8-l9（TP=2 AIV-UDMA AllReduce）：UDMA 的多 QP 与本讲 ROCE 的多 QP 配置链路完全同构（`UdmaQpConfig`），可对照理解「同一机制、两个引擎」的设计复用。
- 性能侧继续 u8-l8（性能测试与调优），把本讲综合实践得到的带宽数据扩展成完整的消息规模-带宽曲线，并结合 `docs/debug/profiling.md` 采集 profile 定位瓶颈在提交端还是网卡端。
- 想深挖 UDMA 侧多 QP 与 relay 绕路的读者，可直接对照阅读 [src/host/transport/device_udma/device_udma_transport_manager.cpp:L285](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/transport/device_udma/device_udma_transport_manager.cpp#L285) 附近消费 `udmaQpConfig.qpNum` 的代码，验证它与本讲 ROCE 链路的同构性。
