# u5-l6 AICore 直驱引擎低阶接口

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清「高阶数据面接口」与「engine 低阶直驱接口」两层 API 的边界：谁屏蔽引擎细节、谁暴露引擎细节。
2. 读懂 `src/device/gm2gm/shmem_device_rma.hpp` 中高阶接口按 `topo_list[pe]` 分派到 SDMA/UDMA/MTE/ROCE 的代码，理解高阶接口内部其实就是「调 engine 接口 + 补 quiet」。
3. 掌握四种引擎各自的代表性低阶能力：MTE 的非连续拷贝与 `mte_quiet`、SDMA 的 `cmo_nbi` 预取与 `notify_record`、UDMA 的 QP 直发/聚合提交/relay 绕路、RDMA 的 WQE 直写。
4. 重点掌握本轮新增的 QP-specific ROCE 接口 `aclshmemx_roce_qp_put_nbi / qp_get_nbi / qp_quiet`：`qp_idx` 的语义、发送队列的索引计算、与普通 `roce_*` 接口在参数上的差异，以及为什么多 QP 能提升小消息带宽。
5. 在编写 kernel 时能判断该用哪一层接口：要可移植性用高阶，要性能与并行度用 engine 直驱。

## 2. 前置知识

- **高阶数据面接口**：`aclshmem_putmem / getmem / *_put / *_get` 这类按元素类型展开的接口。它们只关心「把多少数据从哪个 PE 搬到哪里」，不关心底下用了哪个引擎，接口内部根据初始化时下发的 `device_state->topo_list[pe]` 自动选择引擎（详见 u4-l2）。
- **engine 低阶直驱接口**：`include/device/gm2gm/engine/` 目录下的 `aclshmemx_mte_* / aclshmemx_sdma_* / aclshmemx_udma_* / aclshmemx_roce_*` 系列。它们直接对应一个具体通信引擎的一条指令（WQE/SQE/DMA 指令），把引擎参数（UB 暂存区、sync_id、QP 编号）暴露给 kernel 开发者。
- **几个反复出现的参数**：
  - `buf`：`__ubuf__` 指针，指向 Unified Buffer（UB）中的一块暂存区。引擎发 WQE 前常先把 WQE 内容在 UB 上拼好，再一次性搬入 GM 的发送队列。
  - `sync_id`：硬件事件 ID，用于 MTE3 流水线同步（例如等待「UB 写完 → GM 读」这类跨部件依赖）。
  - `qp_idx`：对同一个远端 PE 建立的多条 QP（Queue Pair，可靠连接队列对）中的编号，是多 QP 并行时选择「走哪条队列」的参数。
- **quiet（静默/保序）**：所有 `_nbi` 接口都是异步的——正常返回只代表请求已提交，不代表数据已到达。必须调用对应引擎的 `quiet` 轮询完成队列（CQ），才能保证对端可见（回顾 u4-l5）。
- **引擎平台约束**（承接 u1-l2/u5-l1）：MTE 走片内互联、不支持跨节点；SDMA 仅特定平台；UDMA 仅 Ascend950；RDMA（RoCE）用于跨节点；QP-specific ROCE 接口仅 XSCALE 后端（编译期由 `ACLSHMEMI_RDMA_K_BACKEND_XSCALE` 宏锁定）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/device/gm2gm/engine/shmem_device_mte.h](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/device/gm2gm/engine/shmem_device_mte.h) | MTE 引擎直驱接口声明：连续/非连续 get/put、quiet、atomic 家族 |
| [include/device/gm2gm/engine/shmem_device_sdma.h](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/device/gm2gm/engine/shmem_device_sdma.h) | SDMA 引擎直驱接口声明：get/put、cmo 预取、quiet、notify_record |
| [include/device/gm2gm/engine/shmem_device_udma.h](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/device/gm2gm/engine/shmem_device_udma.h) | UDMA 引擎直驱接口声明：get/put、QP 直发、聚合提交（defer/submit）、relay 绕路、quiet |
| [include/device/gm2gm/engine/shmem_device_rdma.h](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/device/gm2gm/engine/shmem_device_rdma.h) | RDMA（RoCE）引擎直驱接口声明：get/put、批量聚合、quiet、team 同步、atomic；本轮新增 `roce_qp_*` QP-specific 接口 |
| [src/device/gm2gm/engine/shmem_device_rdma.hpp](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/device/gm2gm/engine/shmem_device_rdma.hpp) | RDMA 引擎 kernel 侧实现：QP 信息获取、WQE 读写、CQ 轮询、`roce_qp_*` 实现 |
| [src/device/gm2gm/engine/shmemi_device_rdma.h](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/device/gm2gm/engine/shmemi_device_rdma.h) | kernel 侧 QP 元数据结构：`aclshmemi_rdma_info`（qp_num、SQ/RQ/CQ 指针）、`aclshmemi_rdma_sq_ctx`（发送队列上下文） |
| [src/device/gm2gm/shmem_device_rma.hpp](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/device/gm2gm/shmem_device_rma.hpp) | 高阶 RMA 的引擎分派实现：`aclshmem_getmem/putmem` 及类型化变如何挑选引擎 |
| [src/device/shmemi_device_meta.h](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/device/shmemi_device_meta.h) | device 元数据访问：`aclshmemi_get_qp_info_address` 取 QP 信息区地址 |
| [include/host_device/shmem_common_types.h](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host_device/shmem_common_types.h) | `aclshmem_device_host_state_t`：`topo_list` 与四种引擎 config 的下发结构 |
| [tests/unittest/device/mem/rdma_mem/qp_specific_apis_kernel.cpp](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/tests/unittest/device/mem/rdma_mem/qp_specific_apis_kernel.cpp) | QP-specific ROCE 接口的 device 侧测试 kernel（本轮新增） |
| [tests/unittest/host/mem/rdma_mem/qp_specific_apis_host_test.cpp](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/tests/unittest/host/mem/rdma_mem/qp_specific_apis_host_test.cpp) | 对应 host 侧测试：`aclshmemx_set_qp_num` 配置与拉起 kernel |

## 4. 核心概念与源码讲解

### 4.1 接口分层：高阶数据面如何落到 engine 直驱层

#### 4.1.1 概念说明

SHMEM 的 device 侧数据面 API 分两层：

- **高阶层（`include/device/gm2gm/shmem_device_rma.h` 等）**：`aclshmem_putmem / aclshmem_uint8_put / aclshmem_getmem_nbi` 这类接口。它们按 `device_state->topo_list[pe]` 里登记的拓扑能力自动挑引擎，用户不需要知道这条链路走 MTE 还是 RoCE。优点是可移植、语义完整（阻塞版本内部补了 quiet）；缺点是你无法干预「用哪条 QP、怎么分批、要不要绕路」。
- **engine 直驱层（`include/device/gm2gm/engine/`）**：`aclshmemx_mte_* / sdma_* / udma_* / roce_*`。一个接口对应一个引擎动作，参数里直接出现 UB 暂存区、sync_id、qp_idx。适合极致性能场景：自己控制流水线、自己选 QP 并行、自己合并 WQE。

两层的关系不是「替换」而是「叠加」：高阶层在实现里就是调用 engine 层，再按语义补齐同步。所以读 engine 层代码也是理解高阶接口行为的最短路径。

#### 4.1.2 核心流程

高阶 `get` 的内部分派流程（`put` 同构）：

```text
aclshmem_getmem(dst, src, elem_size, pe)
  ├─ aclshmemi_get_state() 取 device 全局状态
  ├─ if SDMA 可达(pe)   → aclshmemx_sdma_get_nbi(...) + aclshmemx_sdma_quiet(...)
  ├─ elif UDMA 可达(pe) → aclshmemi_udma_get_default_nbi(...) + aclshmemx_udma_quiet(pe)
  ├─ elif topo_list[pe] & MTE  → aclshmemx_mte_get_nbi(...)  + aclshmemx_mte_quiet()
  └─ elif topo_list[pe] & ROCE → aclshmemx_roce_get_nbi(...) + aclshmemx_roce_quiet(pe, buf, sync_id)
```

注意三点：

1. 分派依据是**每个 PE 一份**的 `topo_list[pe]` 位掩码，不是全局开关——同一个程序里对片内 PE 走 MTE、对跨节点 PE 走 RoCE 是常态。
2. 阻塞版本（不带 `_nbi` 的高阶接口）= 「engine 的 `_nbi` 接口 + 对应 quiet」，同步是高阶层补的，engine 层本身全部是异步语义。
3. 各引擎的 UB 暂存区和 sync_id 来自初始化时下发的 `device_state->sdma_config / udma_config / mte_config / rdma_config`，高阶接口自动读取；engine 直驱时你可以改用自己 kernel 里申请的 UB（避开共享冲突）。

#### 4.1.3 源码精读

高阶 `aclshmem_getmem` 的完整分派实现（读引擎、挑分支、补 quiet 一步到位）：

- [src/device/gm2gm/shmem_device_rma.hpp:L108-L145](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/device/gm2gm/shmem_device_rma.hpp#L108-L145)：`aclshmem_getmem` 依次探测 SDMA → UDMA → MTE → ROCE 四个分支；每个分支从 `device_state` 取该引擎的 `aclshmem_ub / ub_size / sync_id` 配置，调用对应 engine 的 `_nbi` 接口后立刻调用该引擎的 quiet，把异步引擎包成同步语义。

类型化接口（如 `aclshmem_uint8_get`）用同一个宏批量生成，分派逻辑完全相同：

- [src/device/gm2gm/shmem_device_rma.hpp:L147-L182](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/device/gm2gm/shmem_device_rma.hpp#L147-L182)：`ACLSHMEM_GET_TYPENAME_MEM` 宏为 11 种元素类型展开同样的四分支分派，最后由 `ACLSHMEM_TYPE_FUNC` 逐类型实例化。这说明「引擎选择」逻辑只写一遍，类型维度靠宏复制。

UDMA 分支的默认包装（展示高阶层如何固定引擎参数）：

- [src/device/gm2gm/shmem_device_rma.hpp:L99-L106](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/device/gm2gm/shmem_device_rma.hpp#L99-L106)：`aclshmemi_udma_put_default_nbi` 从 `device_state->udma_config` 读共享 UB 与 sync_id 后调用 `aclshmemx_udma_put_nbi<T>`——高阶路径使用的是共享默认配置，而 engine 直驱可以换用自己的 UB。

引擎配置与拓扑的下发载体：

- [include/host_device/shmem_common_types.h:L372-L420](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host_device/shmem_common_types.h#L372-L420)：`aclshmem_device_host_state_t` 中 `topo_list[ACLSHMEM_MAX_PES]`（L395 附近）保存每个 PE 的可用引擎位掩码，`mte_config / sdma_config / rdma_config / udma_config`（L417-L420）保存各引擎的 UB 地址与 sync_id。初始化阶段由 Host 写入，kernel 侧只读。

#### 4.1.4 代码实践

1. **实践目标**：不运行程序，仅靠源码确认「高阶 `aclshmem_uint8_put` 在 ROCE 路径上等价于哪两步 engine 调用」。
2. **操作步骤**：
   - 打开 [src/device/gm2gm/shmem_device_rma.hpp](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/device/gm2gm/shmem_device_rma.hpp)，找到 [L381 定义的 `ACLSHMEM_PUT_TYPENAME_MEM` 宏](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/device/gm2gm/shmem_device_rma.hpp#L381-L413)（阻塞 put 版本，与 4.1.3 引用的 get 宏对称）。
   - 只看 ROCE 分支，写下它调用的 engine 函数名和参数来源。
   - 再对照 [src/device/gm2gm/engine/shmem_device_rdma.hpp:L401-L417](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/device/gm2gm/engine/shmem_device_rdma.hpp#L401-L417) 中 `aclshmemx_roce_put_nbi` 的实现，确认引擎内部使用的 `qp_idx` 值。
3. **需要观察的现象**：ROCE 分支实际是 `aclshmemx_roce_put_nbi(dst, src, 共享UB, elem_size, pe, sync_id)` + `aclshmemx_roce_quiet(pe, 共享UB, sync_id)` 两步；且 engine 实现里写死 `qp_idx = 0`。
4. **预期结果**：你能在笔记里写出这条等价链，并回答「高阶接口永远只用到 QP0」——这正是 4.4 节 QP-specific 接口存在的理由。本实践为源码阅读型，无需运行环境。

#### 4.1.5 小练习与答案

**练习 1**：为什么高阶 `aclshmem_getmem`（阻塞语义）内部调用的却是带 `_nbi` 的异步 engine 接口？

**答案**：engine 层所有搬运接口统一是异步提交语义（返回只代表 WQE 已进发送队列）。阻塞语义由高阶层「engine `_nbi` + 同引擎 quiet」组合实现，这样同一套 engine 接口既能服务阻塞高阶接口，也能服务 `_nbi` 高阶接口（后者把 quiet 留给用户）。

**练习 2**：如果同一 kernel 里既用高阶接口又用 engine 直驱接口且共用 `rdma_config` 里的 UB，可能出什么问题？

**答案**：两个调用路径会共用同一块 UB 暂存区拼 WQE，内容互相覆盖导致 WQE 损坏。头文件注释明确提示：并发场景应使用带显式 `buf/sync_id` 参数的重载，engine 直驱时最好用 kernel 自己 `TPipe::InitBuffer` 申请的 UB。

### 4.2 MTE 与 SDMA 直驱：非连续拷贝、CMO 预取与事件通知

#### 4.2.1 概念说明

- **MTE（Memory Transfer Engine）** 是 AICore 自带的搬运单元，走片内互联，延迟低但不跨节点。它的直驱接口除了连续拷贝，还提供**非连续拷贝**（`non_contiguous_copy_param` 描述源/目的的块长与步长），这是高阶 `iput/iget/putbits` 的底层实现。`aclshmemx_mte_quiet` 负责"清指令流水 + 刷数据 cache 到 GM"。
- **SDMA** 是独立的片间拷贝引擎（仅特定平台）。它的直驱层有三个高阶层没有暴露的特色低阶能力：`cmo_nbi`（L2 cache 管理操作，目前支持 PREFETCH 预取）、`notify_record`（STARS 事件通知记录，配合核间同步）、以及需要显式配置的 `set_sdma_config`。

#### 4.2.2 核心流程

MTE 非连续 put 的使用流程：

```text
1. 组装 non_contiguous_copy_param{length, repeat, src_ld, dst_ld}
   （length=每块元素数，repeat=块数，src_ld/dst_ld=源/目的相邻块起点间距）
2. aclshmemx_mte_put_nbi(dst, src, ub, ub_size, copy_params, pe, sync_id)
   → 内部按 ub_size 切 block，逐块下发 MTE3 指令
3. aclshmemx_mte_quiet() 等全部指令落地
```

SDMA 直驱的典型前置步骤是配置，这是四个引擎中唯一需要显式 `set_config` 的：

```text
1. aclshmemx_set_sdma_config(offset, ub_size, sync_id)   ← 声明本核要用的 UB 区
2. aclshmemx_sdma_get_nbi / put_nbi(...)                 ← 搬运，参数与 MTE 类似但多 ub_size
3. aclshmemx_sdma_quiet(buf, ub_size, sync_id)           ← 等 SQE 完成
（可选）aclshmemx_cmo_nbi(src, elem_size, CMO_TYPE_PREFETCH, ...)  ← 预取远端数据进 L2
（可选）aclshmemx_sdma_notify_record(buf, ub_size, sync_id)        ← 记录 STARS 通知事件
```

#### 4.2.3 源码精读

MTE 连续/非连续 get 与 quiet 的声明：

- [include/device/gm2gm/engine/shmem_device_mte.h:L42-L44](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/device/gm2gm/engine/shmem_device_mte.h#L42-L44)：连续版 `aclshmemx_mte_get_nbi`，参数表里 `ub_size` 显式给出 UB 暂存区大小（字节）。
- [include/device/gm2gm/engine/shmem_device_mte.h:L58-L61](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/device/gm2gm/engine/shmem_device_mte.h#L58-L61)：非连续版用 `const non_contiguous_copy_param&` 替换 `elem_size`，这是高阶 `iget/iput/putbits` 的能力来源。
- [include/device/gm2gm/engine/shmem_device_mte.h:L160-L163](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/device/gm2gm/engine/shmem_device_mte.h#L160-L163)：`aclshmemx_mte_quiet()` 无参数——它清空指令流水并刷 cache，不像 RDMA/SDMA 那样需要轮询远端完成队列，因为 MTE 是本核发起的同步指令流。
- [include/device/gm2gm/engine/shmem_device_mte.h:L255-L281](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/device/gm2gm/engine/shmem_device_mte.h#L255-L281)：`aclshmemx_mte_atomic_add` 的注释给出平台×类型支持矩阵（如 `uint32_t/int64_t/uint64_t` 仅 Ascend950 支持），并说明 950 上不同类型走 Scalar 单元还是 MTE3、需要的 event 同步点——engine 层注释是了解硬件细节的一手资料。

SDMA 的特色低阶接口：

- [include/device/gm2gm/engine/shmem_device_sdma.h:L19-L26](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/device/gm2gm/engine/shmem_device_sdma.h#L19-L26)：`aclshmemx_set_sdma_config`，UB 至少 64 字节且 64 字节对齐。
- [include/device/gm2gm/engine/shmem_device_sdma.h:L99-L101](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/device/gm2gm/engine/shmem_device_sdma.h#L99-L101)：`aclshmemx_cmo_nbi`——对 GM 做 L2 cache 管理操作，注释明确当前仅支持 `CMO_TYPE_PREFETCH`。这是「先把远端要读的数据拉进 L2、再发起读」这类优化的入口。
- [include/device/gm2gm/engine/shmem_device_sdma.h:L118-L134](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/device/gm2gm/engine/shmem_device_sdma.h#L118-L134)：`aclshmemx_sdma_quiet` 的两个重载（LocalTensor / 裸指针），保证此前所有 SQE 完成。
- [include/device/gm2gm/engine/shmem_device_sdma.h:L137-L153](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/device/gm2gm/engine/shmem_device_sdma.h#L137-L153)：`aclshmemx_sdma_notify_record`，AIV 直接驱动 STARS 的通知记录，用于核间事件同步。

#### 4.2.4 代码实践

1. **实践目标**：理解 MTE 非连续拷贝参数如何被高阶 `iput/iget` 使用。
2. **操作步骤**：
   - 阅读 [src/device/gm2gm/shmem_device_rma.hpp:L184-L214](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/device/gm2gm/shmem_device_rma.hpp#L184-L214) 中 `aclshmem_##NAME##_iget` 的实现，观察它如何把 `sst/dst/nelems` 换算成 `non_contiguous_copy_param{nelems, 1, sst, dst}`，再按 `ub_size` 切 block 调 MTE。
   - 用文字推演：`iget(dst, src, sst=4, dst=2, nelems=100)` 时每块读 4 个元素、写 2 个元素间隔的含义。
3. **需要观察的现象**：`copy_params.length` 对应高阶接口的"每个 strided 单位的元素数"，`repeat` 是重复次数；block 大小受 `ub_size / sizeof(T) / length` 与 4095 上限约束。
4. **预期结果**：能画出「源按 sst 步长、目的按 dst 步长」的搬运示意图。运行验证依赖 NPU 环境，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`aclshmemx_mte_quiet()` 为什么不需要 `pe` 和 `buf` 参数，而 `aclshmemx_sdma_quiet` 需要？

**答案**：MTE 指令在本核指令流内完成，quiet 只需清空本核流水并刷 cache；SDMA 是独立引擎，SQE 提交到引擎队列后需要一块 UB 工作区去轮询/等待完成状态，且按引擎语义可能区分目标，因此要传 `buf/ub_size/sync_id`。

**练习 2**：想在正式 get 之前把远端 PE 一段对称内存预热进 L2，用哪个接口？它属于高阶层还是 engine 层？

**答案**：用 `aclshmemx_cmo_nbi(src, elem_size, CMO_TYPE_PREFETCH, buf, ub_size, sync_id)`，它是 SDMA engine 层特有的 cache 管理操作，高阶数据面没有对应封装。

### 4.3 UDMA 直驱：QP 直发、聚合提交与 relay 绕路

#### 4.3.1 概念说明

UDMA 是 Ascend950 的跨节点引擎（依赖 HCOMM 通道资源，见 u5-l4）。它的直驱层是四个引擎中能力最丰富的一层：

1. **基本 get/put `_nbi`**：模板参数 `WQE_PIPE` 选择 WQE（Work Queue Element）的发布通路——`PIPE_MTE3`（默认）先在 UB 暂存 WQE 再一次性搬进 SQ 环形队列；`PIPE_S` 由标量单元直接写、可忽略 `buf/sync_id`。
2. **QP 直发 `udma_qp_*`**：多一个 `qp_idx` 参数，直接指定用哪条 QP 发这个 WQE，配合 `aclshmemx_udma_qp_quiet(pe, qp_idx)` 只等这条 QP。
3. **聚合提交（defer/submit）**：通过 `aclshmemx_defer_t / aclshmemx_submit_t` 两个「动作标签」把多个操作攒成一批，最后一次 `submit` 发布，减少发布次数。
4. **relay 绕路 `udma_relay_*`**：新增 `relay_pe` 参数，让报文先经第三方的端口路径转发到目的 PE，把同一对节点间的流量摊到多条物理链路上提高聚合带宽（需 `ACLSHMEM_RELAY_SUPPORT=ON` 编译）。

#### 4.3.2 核心流程

一次 QP 直发 + 单 QP quiet 的标准时序：

```text
Host: aclshmemx_set_qp_num(ACLSHMEM_DATA_OP_UDMA, q)   ← init 前配置 q 条 QP（1~32，全组一致）
Kernel:
  for i in 0..q-1 并行（每块/每核负责一条 QP）:
      aclshmemx_udma_qp_put_nbi(dst, src, ub, n, pe, qp_idx=i, sync_id)
        ├─ (PIPE_MTE3) 在 ub 拼好 WQE（≥ ACLSHMEM_UDMA_MTE_STAGING_UB_SIZE 字节）
        ├─ DataCopyPad 把 WQE 写入 pe 第 i 条 QP 的 SQ 环
        └─ MTE3→S 事件同步后返回（仅代表"已提交"）
      aclshmemx_udma_qp_quiet(pe, i)                     ← 只等第 i 条 QP 的请求完成
```

聚合提交批的规则（头文件契约）：一个批共用一个 `aclshmemx_submit_state_t`；批内所有调用必须同操作类型、同 PE 参数、同 buf 基址与 sync_id；QP 版本要求同批同 `qp_idx`（不一致时以 submit 调用传入的为准）；批大小 n（含 submit 那一次）必须小于 SQ 环深，UB 容量至少 `64 * n` 字节。

#### 4.3.3 源码精读

- [include/device/gm2gm/engine/shmem_device_udma.h:L21-L40](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/device/gm2gm/engine/shmem_device_udma.h#L21-L40)：`@anchor udma_submit_action_contract` 完整写出聚合提交契约，包括 `buf` 至少 `64*n` 字节、submit 失败会 abort kernel 且不重置状态。
- [include/device/gm2gm/engine/shmem_device_udma.h:L69-L71](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/device/gm2gm/engine/shmem_device_udma.h#L69-L71)：基本 `aclshmemx_udma_get_nbi` 立即提交版，`WQE_PIPE` 默认 `PIPE_MTE3`，单请求上限 256 MB。
- [include/device/gm2gm/engine/shmem_device_udma.h:L88-L111](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/device/gm2gm/engine/shmem_device_udma.h#L88-L111)：defer（攒批）与 submit（提交整批）两个重载，仅靠最后一个参数 `aclshmemx_defer_t / aclshmemx_submit_t` 区分行为。
- [include/device/gm2gm/engine/shmem_device_udma.h:L402-L404](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/device/gm2gm/engine/shmem_device_udma.h#L402-L404)：`aclshmemx_udma_qp_put_nbi`——对比基本版多了 `qp_idx`；注释强调 qp 范围校验只在 debug 构建做，release 下调用者必须自己保证合法。
- [include/device/gm2gm/engine/shmem_device_udma.h:L505-L507](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/device/gm2gm/engine/shmem_device_udma.h#L505-L507)：`aclshmemx_udma_relay_put_nbi`——`relay_pe` 指定转发路径；L469-L476 的前置条件（`pe != relay_pe`、两者都不等于自身等）不满足时静默不发送（debug 构建直接 abort），且 void 返回值让调用者无法感知，必须提前校验参数。
- [include/device/gm2gm/engine/shmem_device_udma.h:L906-L925](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/device/gm2gm/engine/shmem_device_udma.h#L906-L925)：`aclshmemx_udma_quiet(int pe)` 等 PE 全部请求；`aclshmemx_udma_qp_quiet(int pe, uint32_t qp_idx)` 只等一条 QP。注意 L921-L925 的条件编译：`ACLSHMEM_RELAY_SUPPORT` 打开时 qp_quiet 被 `= delete` 禁用。

#### 4.3.4 代码实践

1. **实践目标**：弄清 `WQE_PIPE=PIPE_MTE3` 与 `PIPE_S` 两条发布路径的差异与取舍。
2. **操作步骤**：
   - 精读 [include/device/gm2gm/engine/shmem_device_udma.h:L56-L68](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/device/gm2gm/engine/shmem_device_udma.h#L56-L68) 中对 `WQE_PIPE` 的说明，再读 L308-L329 QP get 的注释。
   - 列表对比：UB 占用、是否需要 sync_id、提交延迟、适用消息规模。
   - 结合 u8-l9 的 `tp_allreduce_udma` 示例（其 baseline 用 `aclshmemx_udma_put_nbi` + `udma_quiet`）看默认路径在真实业务中的用法。
3. **需要观察的现象**：`PIPE_S` 不占 UB、无 MTE3 事件开销，但逐字标量写 HBM；`PIPE_MTE3` 借 MTE3 整块搬运 WQE，适合批量。
4. **预期结果**：形成一张两行对照表。运行层面的对比需 950 环境，**待本地验证**；接口行为可从注释与实现直接确认。

#### 4.3.5 小练习与答案

**练习 1**：relay put 的 `(pe, relay_pe)` 传了非法组合（例如 `relay_pe == pe`），调用返回后程序会怎样？

**答案**：release 构建下什么也观察不到——接口静默不提交任何 WQE 就返回（void 返回值无法报错）；debug 构建下直接 abort kernel。所以调用前必须自行校验四个前置条件（两个都 < rankCount、互不相等、都不等于自身）。

**练习 2**：为什么开了 `ACLSHMEM_RELAY_SUPPORT` 后 `aclshmemx_udma_qp_quiet` 会被 `= delete`？

**答案**：见 [shmem_device_udma.h:L921-L925](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/device/gm2gm/engine/shmem_device_udma.h#L921-L925) 的条件编译：relay 模式下按 QP 静默等待与绕路转发路径的完成语义冲突，编译期直接删除该重载逼使用者改用 `aclshmemx_udma_quiet(pe)`。这是「用删除函数表达约束」的典型手法。

### 4.4 RDMA（RoCE）直驱与 QP-specific 接口（本轮新增重点）

#### 4.4.1 概念说明

RDMA engine 层接口是 kernel 直接「写 WQE 进 SQ 环形队列、再轮询 CQ」的最底层通道。此前普通 `aclshmemx_roce_put_nbi / get_nbi` 在实现里**固定走 QP0**，`aclshmemx_roce_quiet` 则**遍历该 PE 的全部 QP** 轮询——多 QP 配置（`aclshmemx_set_qp_num(ACLSHMEM_DATA_OP_ROCE, q)`，承接 u5-l3/u5-l7）对普通接口只体现在 quiet 的范围上，数据面仍挤在 QP0 一条队列。

本轮新增的 QP-specific 接口补上了缺失的另一半：

- `aclshmemx_roce_qp_put_nbi / qp_get_nbi`：显式传 `qp_idx`，用指定 QP 发 WQE；
- `aclshmemx_roce_qp_quiet`：只等指定 QP 完成。

这样 kernel 里就能按 `qp_idx` 切分并行（例如 AIV 的第 i 个 block 固定用第 `i % q` 条 QP），多条 RC QP 各自保序、互相不打断，缓解单队列按序提交的瓶颈，小消息带宽显著提升。三组接口都仅支持 XSCALE 后端，非 XSCALE 编译期 `static_assert` 报错。

#### 4.4.2 核心流程

device 侧 QP 元数据的取用与 SQ 定位：

```text
1. aclshmemi_qp_info_fetch()
   → 从 device 元数据区读出 aclshmemi_rdma_info{qp_num, sq_ptr, rq_ptr, scq_ptr, rcq_ptr, mem_ptr}
2. 定位 (pe, qp_idx) 的发送队列上下文：
     sq_ctx = sq_ptr + (pe * qp_num + qp_idx) * sizeof(aclshmemi_rdma_sq_ctx)
   即 SQ 上下文数组按 [PE 数][qp_num] 二维铺开，行主序。
3. aclshmemx_roce_qp_put_nbi:
   aclshmem_ptr(dst, pe) 地址翻译 → 在 buf 上拼 64/32 位两张 UB tensor
   → aclshmemi_roce_write(..., pe, qp_idx, len, ...) 按 WQE 模板写 SQ 并敲 doorbell
4. aclshmemx_roce_qp_quiet(pe, qp_idx):
   读 sq_ctx->head_addr（生产者索引），dcci_cachelines 刷新缓存
   → aclshmemi_roce_poll_cq(pe, qp_idx, cur_head, ...) 轮询完成队列直到追平
```

SQ 上下文地址计算可写成：

\[ \text{sq\_ctx}(pe, qp\_idx) = \text{sq\_ptr} + (pe \times qp\_num + qp\_idx) \times \text{sizeof}(\text{aclshmemi\_rdma\_sq\_ctx}) \]

device 侧队列线性编号即 \( pe \times qp\_num + qp\_idx \)，与 u5-l3 讲的传输层建链布局一一对应。

#### 4.4.3 源码精读

**声明层（本轮新增的 QP-specific 接口）**：

- [include/device/gm2gm/engine/shmem_device_rdma.h:L315-L317](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/device/gm2gm/engine/shmem_device_rdma.h#L315-L317)：`aclshmemx_roce_qp_put_nbi` 裸指针重载——参数表比普通版（L293-L295）多一个 `qp_idx`，注释明确"supported only on XSCALE backend"且 `qp_idx` 必须小于配置的 RDMA QP 数。
- [include/device/gm2gm/engine/shmem_device_rdma.h:L337-L340](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/device/gm2gm/engine/shmem_device_rdma.h#L337-L340)：GlobalTensor/LocalTensor 重载，kernel 里用张量风格调用。
- [include/device/gm2gm/engine/shmem_device_rdma.h:L86-L88](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/device/gm2gm/engine/shmem_device_rdma.h#L86-L88) 与 [L108-L111](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/device/gm2gm/engine/shmem_device_rdma.h#L108-L111)：`aclshmemx_roce_qp_get_nbi` 的两种重载。
- [include/device/gm2gm/engine/shmem_device_rdma.h:L512-L527](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/device/gm2gm/engine/shmem_device_rdma.h#L512-L527)：`aclshmemx_roce_qp_quiet` 两种重载。对比普通 `aclshmemx_roce_quiet`（L499-L500）只有 `(pe, buf, sync_id)`，QP 版参数序变为 `(pe, qp_idx, buf, sync_id)`。
- 顺带对照普通接口的批量聚合版本：[include/device/gm2gm/engine/shmem_device_rdma.h:L176-L205](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/device/gm2gm/engine/shmem_device_rdma.h#L176-L205)，`aclshmemx_defer_t / aclshmemx_submit_t` 动作参数实现攒批；注意其注释写明聚合 NBI 目前"one active batch, and QP0"——聚合路径仍是 QP0，多 QP 并行请用 `qp_*` 立即提交版。

**实现层**：

- [src/device/gm2gm/engine/shmem_device_rdma.hpp:L34-L38](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/device/gm2gm/engine/shmem_device_rdma.hpp#L34-L38)：`aclshmemi_qp_info_fetch` 经 `aclshmemi_get_qp_info_address(0)` 取 QP 信息区；后者在 [src/device/shmemi_device_meta.h:L73-L82](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/device/shmemi_device_meta.h#L73-L82) 从 device 元数据区按实例 id 读出指针。
- [src/device/gm2gm/engine/shmemi_device_rdma.h:L24-L31](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/device/gm2gm/engine/shmemi_device_rdma.h#L24-L31)：`aclshmemi_rdma_info`，注释写明 sq/rq/scq/rcq 四个数组均为 `[PE_NUM][qp_num]` 二维布局——这是 4.4.2 公式的出处。
- [src/device/gm2gm/engine/shmemi_device_rdma.h:L40-L57](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/device/gm2gm/engine/shmemi_device_rdma.h#L40-L57)：`aclshmemi_rdma_sq_ctx`，含环形缓冲 `buf_addr/depth/wqe_size`、生产者/消费者索引 `head_addr/tail_addr`、doorbell 模式与 hns1825 专有字段。
- [src/device/gm2gm/engine/shmem_device_rdma.hpp:L58-L71](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/device/gm2gm/engine/shmem_device_rdma.hpp#L58-L71)：**QP 版 quiet 的实现**——L66 按 `(pe * qp_num + qp_idx)` 定位 SQ 上下文，L68 `dcci_cachelines` 刷掉 head 的缓存副本，L70 只对这一条 QP 调 `aclshmemi_roce_poll_cq`。
- [src/device/gm2gm/engine/shmem_device_rdma.hpp:L73-L86](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/device/gm2gm/engine/shmem_device_rdma.hpp#L73-L86)：**普通 quiet 的实现**——`for (qp_idx = 0; qp_idx < qp_num; qp_idx++)` 遍历该 PE 全部 QP 逐条轮询。两段代码并排读，「普通接口与 QP 数的关系只在这一个循环」一目了然。
- [src/device/gm2gm/engine/shmem_device_rdma.hpp:L419-L438](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/device/gm2gm/engine/shmem_device_rdma.hpp#L419-L438)：`aclshmemx_roce_qp_put_nbi` 实现——L423 `static_assert` 锁 XSCALE；L426 `aclshmem_ptr(dst, pe)` 做对称地址翻译（实现见 [L178-L188](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/device/gm2gm/engine/shmem_device_rdma.hpp#L178-L188)：堆内偏移 + 对端堆基址）；L427-L434 把 `buf` 切成 32/64 位两张 VECOUT tensor 当 WQE 暂存；L435 把用户 `qp_idx` 原样传给 `aclshmemi_roce_write`。对照普通版 [L401-L417](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/device/gm2gm/engine/shmem_device_rdma.hpp#L401-L417)，L416 写死 `qp_idx = 0`，其余完全一致——QP 版就是「把 0 换成参数」。
- [src/device/gm2gm/engine/shmem_device_rdma.hpp:L593-L609](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/device/gm2gm/engine/shmem_device_rdma.hpp#L593-L609)：`aclshmemx_roce_qp_quiet` 实现，直接透传 `(pe, qp_idx)`。

**测试佐证（本轮新增用例）**：

- [tests/unittest/device/mem/rdma_mem/qp_specific_apis_kernel.cpp:L35-L42](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/tests/unittest/device/mem/rdma_mem/qp_specific_apis_kernel.cpp#L35-L42)：`get_qp_num_or_zero` 从 `aclshmemi_qp_info_fetch()->qp_num` 读运行时 QP 数——kernel 不需要硬编码 QP 数。
- [tests/unittest/device/mem/rdma_mem/qp_specific_apis_kernel.cpp:L73-L87](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/tests/unittest/device/mem/rdma_mem/qp_specific_apis_kernel.cpp#L73-L87)：并行的关键三行——`qp_idx = peer % qp_num`（L77）把 peer 映射到 QP；`if (qp_idx != AscendC::GetBlockIdx()) continue;`（L78）让 AIV 第 i 个 block 只负责 QP=i 的发送；随后 `aclshmemx_roce_qp_put_nbi` + `aclshmemx_roce_qp_quiet` 成对出现。
- [tests/unittest/host/mem/rdma_mem/qp_specific_apis_host_test.cpp:L79](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/tests/unittest/host/mem/rdma_mem/qp_specific_apis_host_test.cpp#L79)：host 侧先 `aclshmemx_set_qp_num(ACLSHMEM_DATA_OP_ROCE, REQUESTED_QP_NUM)` 配置 QP 数再初始化，测试入口在 L105 `TestShmemRdmaQpSpecificApis`。

#### 4.4.4 代码实践

1. **实践目标**：亲手从测试代码里提炼「qp_idx 如何映射到具体 QP、如何切分到多核」。
2. **操作步骤**：
   - 通读 [tests/unittest/device/mem/rdma_mem/qp_specific_apis_kernel.cpp](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/tests/unittest/device/mem/rdma_mem/qp_specific_apis_kernel.cpp) 的 `roce_qp_put_nbi_raw_impl`（L45-L90）与 tensor 版（L139-L184）。
   - 回答：`MIN_QP_NUM = 2` 时单 QP 环境测试如何自行跳过？`is_active_qp_block`（L24）为什么用 `GetBlockIdx() < qp_num` 截断多余的核？
   - 再对照 host 测试 [qp_specific_apis_host_test.cpp:L105-L108](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/tests/unittest/host/mem/rdma_mem/qp_specific_apis_host_test.cpp#L105-L108)，看非 XSCALE 后端如何 `GTEST_SKIP`。
3. **需要观察的现象**（如有 950 + XSCALE 环境可运行 UT 验证，否则源码推导）：每个 block 至多负责 `ceil((N-1)/q)` 个 peer 的发送；`peer % qp_num` 的取模分布使各 QP 负载均衡。
4. **预期结果**：写出映射规则「block i ⇄ qp_idx = i ⇄ peers 满足 peer % q == i」。运行验证**待本地验证**。
5. 进阶（可选）：把 `qp_idx = peer % qp_num` 改成 `qp_idx = (peer / qp_num) % qp_num`，推演负载分布变化，说明哪种映射在 peer 数 < q 时更优。

#### 4.4.5 小练习与答案

**练习 1**：普通 `aclshmemx_roce_put_nbi(dst, src, buf, elem_size, pe, sync_id)` 与 `aclshmemx_roce_qp_put_nbi(..., pe, qp_idx, sync_id)` 在参数与行为上各差什么？

**答案**：参数上 QP 版仅多一个 `qp_idx`（位于 `pe` 之后）；行为上普通版在实现里固定 `qp_idx = 0`（[shmem_device_rdma.hpp:L416](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/device/gm2gm/engine/shmem_device_rdma.hpp#L416)），QP 版把该参数交给 `aclshmemi_roce_write` 选中指定 SQ。另外 QP 版带 XSCALE `static_assert`，普通版无此编译期约束。

**练习 2**：`aclshmemx_roce_quiet(pe, buf, sync_id)` 与 `aclshmemx_roce_qp_quiet(pe, qp_idx, buf, sync_id)` 的等待范围有何不同？各适合什么场景？

**答案**：普通版遍历 `pe` 的全部 `qp_num` 条 QP 逐条轮询 CQ（实现 [L73-L86](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/device/gm2gm/engine/shmem_device_rdma.hpp#L73-L86)），适合「接下来要读该 PE 全部数据」的粗粒度同步；QP 版只轮询 `(pe, qp_idx)` 一条队列（[L58-L71](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/device/gm2gm/engine/shmem_device_rdma.hpp#L58-L71)），等待范围小得多，适合多 QP 并行中每个 block 只确认自己那条队列的细粒度同步。

**练习 3**：`aclshmemx_roce_barrier` 的注释说它「first performs a quiet operation on all QPs for all PEs in the team」。结合本讲知识说明为什么 barrier 必须用全 QP quiet 而不能用 qp quiet？

**答案**：barrier 要保证进入同步点之前本 PE 发出的**所有**RDMA 操作都已在对端可见，而此前各操作可能散布在任意 QP 上（普通接口走 QP0、QP-specific 接口可能用了任意 `qp_idx`），只有遍历 team 内每个 PE 的全部 QP 轮询才算覆盖完整，随后才能安全做 dissemination 同步。实现见 [src/device/gm2gm/engine/shmem_device_rdma.hpp:L116-L147](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/device/gm2gm/engine/shmem_device_rdma.hpp#L116-L147) 中 barrier 对每个 peer 调全 QP 版 `aclshmemi_roce_quiet(peer, ...)`。

## 5. 综合实践

**任务：制作「四引擎低阶接口分层对照表」并标注 roce_qp_\* 差异**（对应本讲规格中的实践任务，纯源码阅读即可完成）。

1. **实践目标**：为 MTE/RDMA/SDMA/UDMA 各挑一个代表性低阶接口，说明它完成高阶 RMA（以 `aclshmem_getmem` 的四分支为参照）中的哪一步，整理成一张表；再单独标出 `roce_qp_*` 与普通 `roce_*` 接口的参数差异。
2. **操作步骤**：
   - 参照 4.1.3 的分派源码，为每个引擎填一行，建议选题如下（可替换）：
     - MTE：`aclshmemx_mte_get_nbi`（含非连续版本），完成后补 `aclshmemx_mte_quiet()`；
     - RDMA：`aclshmemx_roce_qp_put_nbi`（本轮新增），完成后补 `aclshmemx_roce_qp_quiet`；
     - SDMA：`aclshmemx_sdma_get_nbi`（前置 `aclshmemx_set_sdma_config`），完成后补 `aclshmemx_sdma_quiet`；
     - UDMA：`aclshmemx_udma_qp_put_nbi`（或 relay 版），完成后补 `aclshmemx_udma_qp_quiet`。
   - 每行记录五列：接口（含文件行号永久链接）｜高阶 RMA 中的角色（地址翻译？WQE 提交？quiet？）｜必传引擎参数（buf/ub_size/sync_id/qp_idx/relay_pe）｜平台与编译约束｜单请求上限。
   - 追加一张小表专门对比：`roce_put_nbi` vs `roce_qp_put_nbi`、`roce_get_nbi` vs `roce_qp_get_nbi`、`roce_quiet` vs `roce_qp_quiet`，逐参数标注「新增」「顺序变化」（quiet 的 `qp_idx` 插在 `buf` 之前）与「默认值差异」。
3. **需要观察的现象**：整理过程中你会发现四引擎 quiet 的「等待对象」各不相同（本核流水 / SQE / 全 QP CQ / 单 QP CQ），这正是高阶接口必须逐引擎补同步的原因。
4. **预期结果**：两张表 + 一段结论（何时用高阶、何时直驱）。参考结论方向：业务逻辑优先高阶；需要多 QP 并行、relay 绕路、CMO 预取、聚合提交或自定义 UB 布局时下探到 engine 层，并严格遵守各接口的 quiet 配对。
5. 有 950 环境时可加做验证：运行 `examples/shmem_perftest/rdma_perftest` 的多 QP 模式对比单 QP 带宽（详见 u8-l8），把数字补进结论；无环境则标注**待本地验证**。

## 6. 本讲小结

- device 侧数据面分两层：高阶接口按 `device_state->topo_list[pe]` 自动挑引擎并补齐 quiet；engine 直驱层把 UB 暂存、sync_id、qp_idx 等引擎参数交给 kernel 开发者。
- 高阶阻塞接口 = 「engine `_nbi` 接口 + 同引擎 quiet」，分派逻辑在 `src/device/gm2gm/shmem_device_rma.hpp` 中按 SDMA→UDMA→MTE→ROCE 顺序探测，类型化变体由宏批量生成。
- 四引擎低阶能力各有特色：MTE 非连续拷贝（`non_contiguous_copy_param`）；SDMA 的 `cmo_nbi` 预取与 `notify_record`（且唯一需要显式 `set_sdma_config`）；UDMA 的 QP 直发、defer/submit 聚合与 relay 绕路；RDMA 的 WQE 直写与批量聚合。
- 本轮新增 QP-specific ROCE 接口 `aclshmemx_roce_qp_put_nbi / qp_get_nbi / qp_quiet`：多一个 `qp_idx` 参数（quiet 版还改变了参数顺序），仅 XSCALE 后端可用；普通接口实现写死 QP0，普通 quiet 遍历全部 QP。
- device 侧按 \( pe \times qp\_num + qp\_idx \) 定位 SQ 上下文；测试 kernel 用 `qp_idx = peer % qp_num` + `GetBlockIdx()` 过滤实现「一核一 QP」并行，是多 QP 提升小消息带宽的典型用法。

## 7. 下一步学习建议

- 下一讲 **u5-l7 Ascend950 RDMA 多 QP 机制**：从 Host 侧 `aclshmemx_set_qp_num` 配置、`TransportOptions::RdmaQpConfig` 传递，到 `device_rdma_transport_manager_v2` 按 qpNum 建链与一致性校验，把本讲的 device 侧视角补成全链路。
- 若想看 UDMA 直驱的端到端业务用法，直接进入 **u8-l9 TP=2 AIV-UDMA AllReduce 与 Tailcut**，其中 baseline 模式用 `aclshmemx_udma_put_nbi + udma_quiet`、tailcut 模式用 `aclshmemx_udma_relay_put_nbi` 绕路分流。
- 性能视角可同步阅读 **u8-l8 性能测试与调优**：`rdma_perftest` 本轮新增的多 QP 并行模式正是本讲 `roce_qp_*` 接口的压测载体。
- 建议顺带精读 [docs/api/atomic_api_sync_async_comparison.md](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/docs/api/atomic_api_sync_async_comparison.md)，它从 atomic 角度复述了「topo_list 分派 + quiet 补同步」的同一套分层逻辑。
