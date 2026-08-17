# u4-l2 gm2gm 高阶 RMA 接口与 kernel 编写

> 本讲为增量更新版本：已合并提交 `1e7fffb fix(udma): route local RMA through MTE` 带来的语义变化——高阶 put/get 对**本 PE** 的访问一律走 MTE 分支，不再可能被分派到 UDMA。相关源码行号与永久链接均已按当前 HEAD 刷新。

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 device 侧 gm2gm 高阶 RMA 接口（put/get 家族）有哪些变体，以及它们是如何用宏批量生成的。
2. 理解 gm2gm（Global Memory 到 Global Memory）数据通路：kernel 里一次 `aclshmem_putmem` 调用背后发生了什么。
3. 掌握高阶接口按 `topo_list[pe]` 自动选引擎的规则（SDMA → UDMA → MTE → ROCE 四分支），特别是 **`pe == my_pe` 时被强制走 MTE 的守卫逻辑**——因为 UDMA 不支持自发送（self-send）。
4. 结合 `examples/rdma_demo` 学会在 AscendC kernel 中直接发起跨 PE 数据搬运，并独立编写一个两 PE 数据搬运 kernel。

## 2. 前置知识

本讲假设你已学过 u4-l1（Device 编程模型与全局状态下发）和 u2-l4（对称内存堆）。下面把关键概念快速串一遍：

- **gm2gm 数据通路**：数据从本 PE 的 Global Memory（NPU 上的 HBM）直达远端 PE 的 Global Memory。这是 device 侧最常用的数据面，与之相对的 ub2gm（Unified Buffer 到 Global Memory）将在 u4-l6 讲解。
- **`__gm__` 与 `__ubuf__` 指针**：昇腾 CCE/AscendC 编程模型中，`__gm__` 修饰指向 Global Memory 的指针（地址空间全局可见），`__ubuf__` 修饰指向本核 Unified Buffer 的指针。SHMEM 的 device 接口全部以 `__gm__` 指针为参数。
- **对称地址**：各 PE 的对称堆「堆内偏移一致」。put 时 `dst` 是对称地址，库负责换算成目标 PE 上的实际地址。换算关系可概括为：

  \[ \text{远端地址} = \text{堆基址表}[\,pe\,] + (\text{dst} - \text{本地堆基址}) \]

- **device 全局状态 `device_state`**：u4-l1 讲过，Host 初始化完成后会把 `mype`、`npes`、各 PE 堆基址、引擎配置等序列化到 device 可读的全局内存区，kernel 内通过 `aclshmemi_get_state()` 拿到它。本讲的引擎选择就依赖其中的 `topo_list` 字段。
- **高阶接口 vs 低阶直驱接口**：高阶接口（`aclshmem_putmem` 等）屏蔽引擎细节、自动选引擎；低阶接口（`include/device/gm2gm/engine/` 下的 `aclshmemx_mte_*`、`aclshmemx_roce_*`、`aclshmemx_udma_*` 等）由用户显式指定引擎。本讲主角是前者，后者在 u5-l6 详述。
- **通信引擎两族**：MTE（Memory Transfer Engine，AICore 自带搬运单元）与 xDMA 族（ROCE/SDMA/UDMA，平台支持受限：UDMA 仅 Ascend950，SDMA 仅 A3，见 u1-l2）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/device/gm2gm/shmem_device_rma.h](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/include/device/gm2gm/shmem_device_rma.h) | 高阶 RMA 接口声明层：用宏批量声明 put/get 家族，含完整接口文档注释 |
| [src/device/gm2gm/shmem_device_rma.hpp](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/shmem_device_rma.hpp) | 高阶 RMA 实现层：引擎分派宏（含本 PE 守卫）与四分支选择逻辑 |
| [src/device/gm2gm/shmemi_device_rma.cpp](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/shmemi_device_rma.cpp) | Host 侧 RMA API 下发到 device kernel 的入口（`aclshmemi_prepare_and_post_rma`） |
| [examples/rdma_demo/main.cpp](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/examples/rdma_demo/main.cpp) | 示例 Host 侧：初始化、准备对称数据、发起 kernel、校验结果 |
| [examples/rdma_demo/rdma_demo_kernel.cpp](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/examples/rdma_demo/rdma_demo_kernel.cpp) | 示例 kernel 侧：在 AICore kernel 内直接发起跨 PE 搬运 |
| [examples/rdma_demo/run.sh](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/examples/rdma_demo/run.sh) | 多进程拉起脚本 |
| [include/host_device/shmem_common_types.h](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/include/host_device/shmem_common_types.h) | `aclshmem_device_host_state_t` 定义（`mype`、`topo_list`、各引擎配置） |
| [src/host/init/backends/shmem_init_backend.cpp](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/init/backends/shmem_init_backend.cpp) | 初始化时填充 `topo_list`（引擎位图）的位置 |
| [src/host/entity/mem_entity_default.cpp](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/entity/mem_entity_default.cpp) | Host 侧按 rank 计算可达引擎集合 `CanReachDataOperators`（本轮起不对本 rank 通告 UDMA） |
| [tests/unittest/device/mem/udma_mem/udma_mem_kernel.cpp](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/device/mem/udma_mem/udma_mem_kernel.cpp) | UT：验证本 PE 守卫（kernel 内注入 UDMA topo 位后高阶接口仍走 MTE） |
| [tests/unittest/host/mem/udma_mem/udma_mem_host_test.cpp](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/host/mem/udma_mem/udma_mem_host_test.cpp) | UT：平台门控（非 Ascend950 跳过）的写法范例 |

> 说明：规划中列出的 `src/device/gm2gm/shmem_device_rma.cpp` 在仓库中实际不存在，device 侧实现位于同名的 `.hpp` 头文件中（高阶接口全部以 `ACLSHMEM_DEVICE` 内联函数形式实现，供 kernel 编译单元直接包含），本讲按真实文件讲解。

## 4. 核心概念与源码讲解

### 4.1 高阶 RMA 接口家族与宏生成模式

#### 4.1.1 概念说明

「高阶」的含义是：调用者只说「把这块数据 put 到 PE X / 从 PE X get 回来」，不关心底下用哪个引擎、用哪块 UB 中转、怎么等完成。所有细节由库根据运行时状态自动决定。

接口家族按维度展开：

| 维度 | 取值 | 例子 |
| --- | --- | --- |
| 方向 | put / get | `aclshmem_putmem` / `aclshmem_getmem` |
| 类型化 | 13 种类型 | `aclshmem_int32_put`、`aclshmem_float_get` |
| 位宽 | 8/16/32/64/128 | `aclshmem_put8`、`aclshmem_get64` |
| 跨步（strided） | iput / iget | `aclshmem_int32_iput` |
| 阻塞/非阻塞 | 阻塞 / `_nbi` | `aclshmem_putmem` / `aclshmem_putmem_nbi` |
| 参数形态 | 裸指针 / `GlobalTensor` / `non_contiguous_copy_param` | 见 `.h` 中三组 `_nbi` 重载 |
| 单元素 | `_p` / `_g` | `aclshmem_int32_p`、`aclshmem_float_g` |

这么多变体不可能手写，于是 SHMEM 用「宏生成宏」的模式：一个类型清单宏 `ACLSHMEM_TYPE_FUNC` 罗列 13 种 `(名字, 类型)` 组合，再把它套在不同的「函数模板宏」上，一次性生成整族函数。声明与实现共享同一份类型清单，保证两侧永远一致。

#### 4.1.2 核心流程

以生成 typed put 家族为例：

```text
ACLSHMEM_TYPE_FUNC(FUNC)          # 类型清单：FUNC(half,half); FUNC(float,float); ... 共 13 项
        │
        ├── 套在 ACLSHMEM_PUT_TYPENAME_MEM 上 → .h 中生成 13 个声明 / .hpp 中生成 13 个定义
        ├── 套在 ACLSHMEM_GET_TYPENAME_MEM 上 → 13 个 get
        ├── 套在 ACLSHMEM_PUT_TYPENAME_MEM_NBI 上 → 13 个 put_nbi
        └── ...
```

每个生成出的函数内部都遵循同一个骨架：**取全局状态 → 按优先级选引擎 → 调引擎低阶接口（阻塞版紧跟 quiet）**。

#### 4.1.3 源码精读

先看类型清单宏，声明层与实现层各有一份、内容完全一致：

- [include/device/gm2gm/shmem_device_rma.h:42-55](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/include/device/gm2gm/shmem_device_rma.h#L42-L55)：声明层的 `ACLSHMEM_TYPE_FUNC`，罗列 half/float/double/int8~uint64/char/bfloat16 共 13 种类型。头文件注释里附有「NAME ↔ TYPE」对照表。
- [src/device/gm2gm/shmem_device_rma.hpp:51-64](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/shmem_device_rma.hpp#L51-L64)：实现层的同名宏。

再看「函数模板宏」。声明层以 typed put 为例：

- [include/device/gm2gm/shmem_device_rma.h:365-370](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/include/device/gm2gm/shmem_device_rma.h#L365-L370)：`ACLSHMEM_PUT_TYPENAME_MEM(NAME, TYPE)` 展开为 `aclshmem_NAME_put(__gm__ TYPE* dst, __gm__ TYPE* src, uint32_t elem_size, int32_t pe)` 的声明。注意参数语义：put 的 `dst` 必须是**对称地址**（会被换算到 pe 上），`src` 是本地任意 GM 地址；`elem_size` 是**元素个数**。头文件 [include/device/gm2gm/shmem_device_rma.h:338-364](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/include/device/gm2gm/shmem_device_rma.h#L338-L364) 的注释明确写了这一地址约定。

实现层对应的宏体就是真正的四分支分派（下一小节精读）：

- [src/device/gm2gm/shmem_device_rma.hpp:381-416](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/shmem_device_rma.hpp#L381-L416)：`ACLSHMEM_PUT_TYPENAME_MEM` 的实现体，末尾 [src/device/gm2gm/shmem_device_rma.hpp:416](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/shmem_device_rma.hpp#L416) 一行 `ACLSHMEM_TYPE_FUNC(ACLSHMEM_PUT_TYPENAME_MEM);` 即刻展开出 13 个函数定义。

非类型化的 `aclshmem_putmem` / `aclshmem_put##BITS` 则直接复用 typed 版本：

- [src/device/gm2gm/shmem_device_rma.hpp:489-499](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/shmem_device_rma.hpp#L489-L499)：`aclshmem_put##BITS` 把「元素个数」换算成字节数后转调 `aclshmem_putmem`（`elem_size * (BITS/8)`），可见 `putmem` 的第三参是**字节数**、typed 变体的第三参是**元素个数**。
- [include/device/gm2gm/shmem_device_rma.h:885](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/include/device/gm2gm/shmem_device_rma.h#L885)：声明层末尾 `#include "gm2gm/shmem_device_rma.hpp"` 把实现层整体纳入——这就是为什么实现文件是 `.hpp` 而不是 `.cpp`：高阶接口是 `ACLSHMEM_DEVICE` 函数，必须随 kernel 编译单元一起编译进算子。

单元素 `_p` / `_g` 是特例，不走引擎分派，直接用 `aclshmem_ptr` 把对称地址换算成远端地址后读写并 `dcci_cacheline` 刷缓存行：

- [src/device/gm2gm/shmem_device_rma.hpp:66-74](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/shmem_device_rma.hpp#L66-L74)：`_p` 的实现；地址换算入口 `aclshmem_ptr` 声明于 [include/device/gm2gm/engine/shmem_device_mte.h:27-28](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/include/device/gm2gm/engine/shmem_device_mte.h#L27-L28)。

#### 4.1.4 代码实践

**实践目标**：用 grep 数清 put 家族的变体数量，验证「宏生成」的理解。

1. 操作步骤：
   - 在仓库根目录执行 `grep -c "define shmem_put_" include/device/gm2gm/shmem_device_rma.h`，统计 `#define shmem_put_*` 短名别名的数量。
   - 再执行 `grep -n "ACLSHMEM_TYPE_FUNC(ACLSHMEM_PUT" src/device/gm2gm/shmem_device_rma.hpp`，看 typed put 有几组展开（阻塞、nbi、detailed-nbi、tensor-nbi、tensor-detailed-nbi）。
   - 对照 `.h` 中 `ACLSHMEM_SIZE_FUNC`（位宽清单，位于 `device/shmem_def.h`，可 `grep -n "ACLSHMEM_SIZE_FUNC" include/device/shmem_def.h` 找到）计算位宽变体数。
2. 需要观察的现象：别名数量与类型清单 13 项的关系；不同 `_nbi` 重载组之间的参数差异。
3. 预期结果：`shmem_put_*_mem` 与 `shmem_put_*_mem_nbi` 别名各 13 条（half/float/double/int8~uint64/char/bfloat16），与类型清单一一对应。（待本地验证具体 grep 计数输出。）

#### 4.1.5 小练习与答案

**练习 1**：为什么声明层和实现层各有一份 `ACLSHMEM_TYPE_FUNC`，而不是共用一份？
**答案**：`.h` 被 Host 与 Device 两侧、以及众多 kernel 编译单元包含；`.hpp` 只应在 device 编译（含 `kernel_operator.h` 的 AscendC 环境）下被纳入。两份清单若分离会产生声明/实现漂移，所以两处内容刻意保持逐项一致——这本身也是一种「以重复换解耦」的取舍。

**练习 2**：`aclshmem_putmem(dst, src, 1024, pe)` 与 `aclshmem_int32_put(dst, src, 1024, pe)` 的第三参含义相同吗？
**答案**：不同。前者 `elem_size` 是**字节数**（位宽变体 `aclshmem_put32` 转调它时会乘上 `BITS/8`，见 `shmem_device_rma.hpp:489-499`）；后者是**元素个数**（1024 个 int32 = 4096 字节），由引擎层按 `sizeof(TYPE)` 换算。

**练习 3**：`_p`/`_g` 单元素接口为什么可以不走四分支分派？
**答案**：单元素读写只是一次 GM load/store，`aclshmem_ptr` 换算地址后直接访问即可（配合 `dcci_cacheline` 保证可见性），不需要 DMA 引擎，也就无需选引擎。

### 4.2 引擎选择的数据来源：device_state 与 topo_list

#### 4.2.1 概念说明

高阶接口「自动选引擎」依据的是两样东西：

1. **编译期开关**：`ACLSHMEM_UDMA_SUPPORTED`（仅 `__NPU_ARCH__ == 3510` 即 Ascend950 为 1）、`ACLSHMEM_TRANSPORT_SDMA_SUPPORTED`。平台不支持时对应分支在编译期就被裁剪成永假。
2. **运行期拓扑位图 `topo_list`**：`topo_list[pe]` 是一个 8 位掩码，记录「从本 PE 看，到 pe 可用哪些引擎」。位定义在 [include/host/shmem_host_def.h:130-133](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/include/host/shmem_host_def.h#L130-L133)：`ACLSHMEM_TRANSPORT_MTE = 1<<0`、`ROCE = 1<<1`、`SDMA = 1<<2`、`UDMA = 1<<3`。

`topo_list` 住在 device 全局状态里，是 u4-l1「Host 状态下发」的一部分。它描述的是**可达性**：同一块对称堆，到不同 PE 可能走不同引擎（同卡邻居走 MTE/SDMA，跨机走 ROCE）。

#### 4.2.2 核心流程

`topo_list` 的生成链路：

```text
初始化（Host 侧）
  MemEntityDefault::CanReachDataOperators(remoteRank)     # 按 rank 算可达引擎集合
        │  MTE/SDMA/UDMA 可达性同源：SdmaReaches(remoteRank)
        │  本轮新增：remoteRank == 本 rank 时不再通告 UDMA
        ▼
  shmem_init_backend 建堆阶段：对每个 i ∈ [0, npes)
        hybm_entity_reach_types(hbm_entity, i, ...)
        ▼  把 HYBM_DOP_TYPE_* 逐位映射为 ACLSHMEM_TRANSPORT_*
  host_state->topo_list[i] 填好 ──► update_device_state 镜像到 device
        ▼
kernel 内：device_state = aclshmemi_get_state()
        ▼
  ACLSHMEM_*_TRANSPORT_ENABLED(device_state, pe)   # 读 topo_list[pe] 的对应位
```

#### 4.2.3 源码精读

- 状态载体：[include/host_device/shmem_common_types.h:377-426](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/include/host_device/shmem_common_types.h#L377-L426) 定义 `aclshmem_device_host_state_t`。关键字段：[L379](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/include/host_device/shmem_common_types.h#L379) 的 `mype`（本 PE 编号，4.3 节守卫要用它）、[L394-395](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/include/host_device/shmem_common_types.h#L394-L395) 的 `topo_list[ACLSHMEM_MAX_PES]`、[L417-420](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/include/host_device/shmem_common_types.h#L417-L420) 的 mte/sdma/rdma/udma 四份引擎配置（各含 UB 中转地址、UB 大小、同步事件号）。
- 填充点：[src/host/init/backends/shmem_init_backend.cpp:469-491](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/init/backends/shmem_init_backend.cpp#L469-L491)。对每个 rank 调 `hybm_entity_reach_types` 拿到可达类型，然后把 `HYBM_DOP_TYPE_MTE/RDMA/SDMA/UDMA` 逐位映射进 `topo_list[i]`（[L478-489](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/init/backends/shmem_init_backend.cpp#L478-L489)）；同函数 [L476](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/init/backends/shmem_init_backend.cpp#L476) 还顺带按 `ALIGN_UP(heap_size, 对齐)` 步长排布各 PE 的堆基址——这就是对称地址换算的基址表来源（承接 u2-l4/u2-l5）。
- 可达性计算：[src/host/entity/mem_entity_default.cpp:926-945](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/entity/mem_entity_default.cpp#L926-L945) 的 `CanReachDataOperators`。注意 [L929](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/entity/mem_entity_default.cpp#L929) 的注释：`sdmaReach` 为真则 MTE 也可达——**MTE、SDMA、UDMA 的可达性同源**，这是理解 4.3 节缺陷的钥匙。
- 编译期开关：[src/device/gm2gm/engine/shmem_device_udma.hpp:22-26](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/engine/shmem_device_udma.hpp#L22-L26) 定义 `ACLSHMEM_UDMA_SUPPORTED`（`__NPU_ARCH__ == 3510` 才为 1）；[src/device/gm2gm/shmem_device_rma.hpp:22-24](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/shmem_device_rma.hpp#L22-L24) 在未被引擎头定义时兜底为 0。SDMA 的对应开关在 [src/device/gm2gm/engine/shmem_device_sdma.hpp:20-22](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/engine/shmem_device_sdma.hpp#L20-L22)。

#### 4.2.4 代码实践

**实践目标**：把 `topo_list` 的生成链画成一张图，并标注本 rank 与远端 rank 的差异。

1. 操作步骤：从 `MemEntityDefault::CanReachDataOperators` 出发，经 `hybm_entity_reach_types`、`shmem_init_backend` 的填充循环，到 `aclshmemi_get_state()` 的消费点，逐个函数在源码中定位并抄下文件名与行号。
2. 需要观察的现象：对 `remoteRank == rankId`（自己），`SdmaReaches` 返回什么？由此 MTE 位是否置位？UDMA 位呢？
3. 预期结果：本 rank 的 `topo_list[my_pe]` 含 MTE 位（本地拷贝永远可达）但**不含 UDMA 位**；远端 rank 在 Ascend950 上可达时含 UDMA 位。这张图是下一节守卫逻辑的因果起点。

#### 4.2.5 小练习与答案

**练习 1**：`topo_list` 是全局唯一的还是每 PE 一份？
**答案**：每 PE 一份。它回答的是「从本 PE 看」的可达性，随 `aclshmem_device_host_state_t` 一起下发到本 PE 的 device 全局内存，因此 kernel 里读的是本 PE 视角。

**练习 2**：为什么 ROCE 位不依赖 `SdmaReaches`？
**答案**：`CanReachDataOperators` 中 RDMA 只看初始化时用户开启的引擎位掩码（`options_.bmDataOpType & HYBM_DOP_TYPE_DEVICE_RDMA`）——RoCE 走网络，可达性由建链保证，与片内/互联的 SDMA 可达性判定是两套机制；而 MTE/SDMA/UDMA 共享 `SdmaReaches` 这个「物理邻居可达」判定。

### 4.3 四分支引擎分派与本 PE 守卫：UDMA 不支持自发送

#### 4.3.1 概念说明

每个高阶 put/get 的实现体都是同一条分派链，优先级固定为 **SDMA → UDMA → MTE → ROCE**：

- SDMA、UDMA 是片内/片间的 DMA 类高速引擎，前置条件最苛刻：**编译期开关为真 且 topo 位为真**；
- MTE、ROCE 只看 topo 位。MTE 是 AICore 自带的搬运单元，本 rank 永远可达，天然充当本地拷贝的兜底；ROCE 走网络，排最后。

**本轮修复的缺陷**：MTE 与 UDMA 共用 SDMA 可达性判定，而 UDMA 在分派链中排在 MTE 之前。对本 PE 自己（`pe == my_pe`）：`SdmaReaches` 为真 → 旧逻辑同时标记 MTE|UDMA → UDMA 先命中 → 本地拷贝被送进 UDMA。但 **UDMA 不支持自发送（self-send）**，这条路径是错的。

修复采用两层防御（纵深防御）：

1. **Topo 层（第一层）**：Host 侧 `CanReachDataOperators` 不再为本 rank 通告 UDMA，本 rank 仅经 MTE 可达——让正常初始化下 `topo_list[my_pe]` 根本不含 UDMA 位。
2. **高阶接口层（第二层）**：分派宏 `ACLSHMEM_UDMA_TRANSPORT_ENABLED` 增加 `(PE) != mype` 校验——即使 topo 数据异常含本 rank 的 UDMA 位（如测试中人为注入），高阶 put/get 仍回落 MTE 分支。

#### 4.3.2 核心流程

修复后的分派逻辑（以阻塞 put/get 为例的伪代码）：

```text
function aclshmem_putmem(dst, src, size, pe):
    state = aclshmemi_get_state()
    if SDMA_SUPPORTED 且 topo_list[pe] 有 SDMA 位:
        sdma_put_nbi(...);  sdma_quiet(...)          # 阻塞版 = nbi + 立即 quiet
    else if UDMA_SUPPORTED 且 pe != state.mype 且 topo_list[pe] 有 UDMA 位:   # ← 本 PE 守卫
        udma_put_nbi(...);  udma_quiet(pe)
    else if topo_list[pe] 有 MTE 位:
        mte_put_nbi(...);   mte_quiet()
    else if topo_list[pe] 有 ROCE 位:
        roce_put_nbi(...);  roce_quiet(...)
```

两个关键点：

- **pe == my_pe 时**：第一层防线使 topo 无 UDMA 位；即便有，第二层守卫也令 UDMA 分支为假 → 落到 MTE 分支完成本地拷贝。
- **阻塞与 `_nbi` 的差别只在「是否紧跟 quiet」**：`_nbi` 版本发起后立刻返回，完成保证交给调用者（配合 `aclshmemx_mte_quiet` / `aclshmemx_udma_quiet` 等，详见 u4-l5）。

#### 4.3.3 源码精读

分派宏定义（本讲最核心的两行）：

- [src/device/gm2gm/shmem_device_rma.hpp:26-30](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/shmem_device_rma.hpp#L26-L30)：`ACLSHMEM_SDMA_TRANSPORT_ENABLED(STATE, PE)` 与 `ACLSHMEM_UDMA_TRANSPORT_ENABLED(STATE, PE)`。对比可知 UDMA 宏多了中间的 `((PE) != (STATE)->mype)`——**这正是 `1e7fffb` 提交加入的自发送守卫**：只要目标是本 PE，整个表达式恒为假，UDMA 分支被跳过。宏在文件末尾 [L942-943](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/shmem_device_rma.hpp#L942-L943) `#undef`，作用域被限制在本文件内。

阻塞版 `aclshmem_getmem` 的四分支（putmem 结构完全对称）：

- [src/device/gm2gm/shmem_device_rma.hpp:108-145](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/shmem_device_rma.hpp#L108-L145)：[L112](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/shmem_device_rma.hpp#L112) SDMA 分支（取 `sdma_config` 的 UB 与事件号，`sdma_get_nbi` + `sdma_quiet`）；[L121-125](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/shmem_device_rma.hpp#L121-L125) UDMA 分支（经 `aclshmemi_udma_get_default_nbi` 取 `udma_config`，再 `udma_quiet(pe)`）；[L126-135](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/shmem_device_rma.hpp#L126-L135) MTE 分支（`mte_config` + `mte_get_nbi` + `mte_quiet()`）；[L136-144](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/shmem_device_rma.hpp#L136-L144) ROCE 分支。
- [src/device/gm2gm/shmem_device_rma.hpp:342-379](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/shmem_device_rma.hpp#L342-L379)：阻塞版 `aclshmem_putmem`，分支顺序 SDMA（[L346](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/shmem_device_rma.hpp#L346)）→ UDMA（[L355](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/shmem_device_rma.hpp#L355)）→ MTE（[L360](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/shmem_device_rma.hpp#L360)）→ ROCE（[L370](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/shmem_device_rma.hpp#L370)）。
- 非阻塞版：`aclshmem_getmem_nbi` 在 [L577-610](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/shmem_device_rma.hpp#L577-L610)、`aclshmem_putmem_nbi` 在 [L880-914](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/shmem_device_rma.hpp#L880-L914)，同样使用这两个宏，只是不跟 quiet。typed 与 GlobalTensor 版本（如 [L612-642](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/shmem_device_rma.hpp#L612-L642)、[L811-854](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/shmem_device_rma.hpp#L811-L854)）一律复用守卫宏——所以**整族高阶 RMA 入口都受本 PE 保护**。
- UDMA 分支的封装：[src/device/gm2gm/shmem_device_rma.hpp:90-106](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/shmem_device_rma.hpp#L90-L106) 的 `aclshmemi_udma_get/put_default_nbi` 从 `udma_config` 取默认 UB 暂存区与 sync_id，再调低阶 `aclshmemx_udma_*_nbi`。

Host 侧第一层防线：

- [src/host/entity/mem_entity_default.cpp:939-942](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/entity/mem_entity_default.cpp#L939-L942)：`if (remoteRank != options_.rankId && sdmaReach && ...)` 才置 `HYBM_DOP_TYPE_DEVICE_UDMA`，注释写明 "UDMA does not support self-send; keep the local rank reachable through MTE only."。本 rank 仍会因 `sdmaReach` 拿到 MTE 位（[L929-932](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/entity/mem_entity_default.cpp#L929-L932)），本地拷贝通路完好。

UT 如何验证第二层防线（很好的测试范式）：

- [tests/unittest/device/mem/udma_mem/udma_mem_kernel.cpp:145-175](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/device/mem/udma_mem/udma_mem_kernel.cpp#L145-L175)：kernel `UDMAHighLevelLocalRmaTest` 先保存原值，然后**强行把本 PE 的 topo 位或上 UDMA**（[L153-155](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/device/mem/udma_mem/udma_mem_kernel.cpp#L153-L155)，改完 `dcci_cacheline` 刷缓存行保证可见），再对本 PE 依次调阻塞/nbi 的 put/get（[L166-171](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/device/mem/udma_mem/udma_mem_kernel.cpp#L166-L171)），nbi 后用 `aclshmemx_mte_quiet()` 收尾——**用 MTE 的 quiet 就能等到完成，恰好证明实际执行的是 MTE 分支而非 UDMA**。最后恢复 topo 原值（[L173-174](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/device/mem/udma_mem/udma_mem_kernel.cpp#L173-L174)）。
- 平台门控：[tests/unittest/host/mem/udma_mem/udma_mem_host_test.cpp:443-452](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/host/mem/udma_mem/udma_mem_host_test.cpp#L443-L452) 用 `aclrtGetSocName()` 检测 Ascend950，否则 `GTEST_SKIP()` 跳过。

一个值得注意的边界：**低阶直驱 UDMA 接口没有这层守卫**。同文件里低阶 UDMA 用例的循环显式 `if (peer == rank) continue;` 跳过自己（如 [tests/unittest/device/mem/udma_mem/udma_mem_kernel.cpp:132-135](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/device/mem/udma_mem/udma_mem_kernel.cpp#L132-L135)）；另外 put_signal（SO）路径的 UDMA 判断（[src/device/gm2gm/shmem_device_so.hpp:99](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/shmem_device_so.hpp#L99)）仍只看 topo 位，依赖第一层防线兜底。这印证了一个设计原则：**「不做自发送」的保证在高阶 RMA 是强保证（双层），在低阶直驱是使用约定（由调用者自律）**。

#### 4.3.4 代码实践

**实践目标**：解释「为什么对本 PE 的 `aclshmem_putmem` 不会选中 UDMA」，并用 UT 验证结论。

1. 操作步骤：
   - 精读 [src/device/gm2gm/shmem_device_rma.hpp:29-30](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/shmem_device_rma.hpp#L29-L30) 的宏，写出 `pe == mype` 时宏展开的布尔表达式并化简。
   - 对照 [src/host/entity/mem_entity_default.cpp:939-942](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/host/entity/mem_entity_default.cpp#L939-L942)，说明正常初始化下 `topo_list[my_pe]` 为什么没有 UDMA 位。
   - 阅读 UT `TestShmemUDMAHighLevelLocalRma`：它故意破坏第一层防线（注入 UDMA 位），为什么测试仍应通过？
2. 需要观察的现象：宏在 `pe == mype` 时短路为假；UT kernel 中 `aclshmemx_mte_quiet()` 能正确等待本 PE nbi 操作完成。
3. 预期结果：`aclshmem_putmem(dst, src, size, my_pe)` 稳定落入 MTE 分支；在 Ascend950 环境运行该 UT 通过，其他平台被 `GTEST_SKIP` 跳过。（运行结果待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：既然 Host 侧已经不为本 rank 通告 UDMA，为什么还要在 device 宏里再加 `(PE) != mype`？
**答案**：纵深防御。topo 数据是 Host 生成后下发的运行期数据，可能因逻辑回归、异常注入或未来改动而重新含本 rank 的 UDMA 位；宏守卫在消费端把「本 PE 不进 UDMA」变成高阶接口的不变量，两层任一生效结果都正确。UT 正是通过破坏第一层来专门验证第二层。

**练习 2**：把分派链中 UDMA 与 MTE 的顺序对调（MTE 在前），本 PE 问题会消失吗？这样做有什么代价？
**答案**：会让本 PE 落到 MTE 而看似修复，但代价是**远端**可达 UDMA 的拷贝也全部退化为 MTE，丢掉 UDMA 的带宽/卸载优势。正确做法不是降优先级，而是精确排除 `pe == mype` 这一种情形——这正是修复采用的条件守卫而非调序。

**练习 3**：`aclshmem_putmem`（阻塞）和 `aclshmem_putmem_nbi` 在四分支实现上的唯一差别是什么？
**答案**：阻塞版每个分支在发起 `_nbi` 之后**紧跟对应引擎的 quiet**（如 UDMA 分支的 `aclshmemx_udma_quiet(pe)`、MTE 分支的 `aclshmemx_mte_quiet()`）；`_nbi` 版本发起后立即返回，完成保证由调用者后续补 quiet（对照 [shmem_device_rma.hpp:342-379](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/shmem_device_rma.hpp#L342-L379) 与 [L880-914](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/shmem_device_rma.hpp#L880-L914)）。

### 4.4 rdma_demo 实战：在 AscendC kernel 中发起跨 PE 搬运

#### 4.4.1 概念说明

`examples/rdma_demo` 演示了完整的「Host 准备 → kernel 搬运 → Host 校验」闭环：

- Host 侧以 **ROCE 引擎**初始化、在对称堆上 malloc 一块 1024 字节缓冲，把本 PE 的数据 `aclrtMemcpy` 到自己的段；
- kernel `device_all_gather_test` 实现简易 AllGather：每个 PE 把自己的段**推（put）**给其他所有 PE；
- Host 侧把整块缓冲拷回并逐段校验。

注意一个教学细节：该示例 kernel 用的是**低阶直驱接口** `aclshmemx_roce_put_nbi`（与初始化开启的 ROCE 引擎对应），而非高阶 `aclshmem_putmem`。它展示的是「kernel 内直接发起跨 PE 搬运」的骨架；本讲实践会让你把这个骨架换成高阶接口，体会两者的差别（高阶无需自备引擎判断与 WQE UB，也不必显式指定引擎）。另外注意 kernel 循环里 `if (i == my_rank) continue;` 跳过自己——自己的段本来就已在本地，无需搬运；这与 4.3 节「本 PE 拷贝走 MTE」形成呼应：如果改用高阶接口，即使不跳过自己也能正确完成（走 MTE）。

#### 4.4.2 核心流程

```text
run.sh：for i in [0, num_pes)：后台拉起 ./build/bin/rdma_demo num_pes i tcp://… num_pes 0 0
   └─ main.cpp：aclInit → aclrtSetDevice → aclshmemx_init_attr（引擎位 = ACLSHMEM_DATA_OP_ROCE）
        → aclshmem_malloc(1024)（对称堆，各 PE 同序同大小 → 偏移一致）
        → aclrtMemcpy 把本 PE 输入写入自己的段
        → allgather_demo(1, stream, ptr, size)：下发 kernel
             kernel：aclshmemx_roce_barrier_all            # 对齐各 PE 起步
                     for i ≠ my_pe：aclshmemx_roce_put_nbi(对端同偏移地址, 本地源, UB, len, i)
                     aclshmemx_roce_barrier_all            # 等全员推完
        → aclrtSynchronizeStream → 拷回整块缓冲 → 逐段校验 = 10 + pe → finalize
```

#### 4.4.3 源码精读

Host 侧：

- [examples/rdma_demo/main.cpp:42-46](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/examples/rdma_demo/main.cpp#L42-L46)：填充 `aclshmemx_init_attr_t` 后设置 `option_attr.data_op_engine_type = ACLSHMEM_DATA_OP_ROCE`，以 DEFAULT 模式初始化（三要素回顾见 u1-l4/u2-l1）。
- [examples/rdma_demo/main.cpp:48-58](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/examples/rdma_demo/main.cpp#L48-L58)：`aclshmem_malloc(1024)` 建对称缓冲；每个 PE 的输入值为 `pe_id + 10`，写入自己段 `ptr + my_pe * trans_size * sizeof(int32_t)`。
- [examples/rdma_demo/main.cpp:61-68](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/examples/rdma_demo/main.cpp#L61-L68)：`block_dim=1` 下发 kernel、同步流、把 `n_pes` 段整块拷回 Host。
- [examples/rdma_demo/main.cpp:70-84](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/examples/rdma_demo/main.cpp#L70-L84)：逐段抽查，第 `i` 段应恒等于 `10 + i`；失败则走完整逆序清理并返回 -1（顺序与初始化严格对称，呼应 u2-l2）。

Kernel 侧：

- [examples/rdma_demo/rdma_demo_kernel.cpp:18-30](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/examples/rdma_demo/rdma_demo_kernel.cpp#L18-L30)：kernel 签名 `[[bisheng::core_ratio(0, 1)]] __global__ __aicore__`；先 `TPipe::InitBuffer` 分配一块 `VECOUT` LocalTensor（注释要求 ≥128 字节，ROCE 任务下发需要）；再调 `aclshmem_my_pe()` / `aclshmem_n_pes()`——kernel 内查询身份的这两个 device 接口读的正是 device 全局状态（承接 u4-l1）。
- [examples/rdma_demo/rdma_demo_kernel.cpp:31-38](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/examples/rdma_demo/rdma_demo_kernel.cpp#L31-L38)：循环对每个 `i ≠ my_rank` 调 `aclshmemx_roce_put_nbi`。注意 dst 与 src 用了**同一个地址表达式** `gva + message_length * my_rank`：src 按本地堆解释、dst 会被换算到 `i` 的堆上同偏移处——这就是对称地址的用法。
- [examples/rdma_demo/rdma_demo_kernel.cpp:43-46](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/examples/rdma_demo/rdma_demo_kernel.cpp#L43-L46)：Host 可调的启动器 `allgather_demo`，用 `<<<block_dim, nullptr, stream>>>` 语法把 kernel 挂到 stream 上。
- 两道 `aclshmemx_roce_barrier_all()`（[L30](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/examples/rdma_demo/rdma_demo_kernel.cpp#L30)、[L40](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/examples/rdma_demo/rdma_demo_kernel.cpp#L40)）：第一道保证所有 PE 都写完自己的段再开始推，第二道保证全员推完再让 Host 校验。

运行脚本与 Host 下发链（补充两块拼图）：

- [examples/rdma_demo/run.sh:40-49](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/examples/rdma_demo/run.sh#L40-L49)：设置 `SHMEM_UID_SESSION_ID` 后循环后台拉起 `num_pes` 个进程，第 `i` 个的命令行参数即「总 PE 数、自身 PE 号、ipport、NPU 数、f_pe、f_npu」——与 [main.cpp:96-106](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/examples/rdma_demo/main.cpp#L96-L106) 的参数解析一一对应。
- [src/device/gm2gm/shmemi_device_rma.cpp:33-41](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/shmemi_device_rma.cpp#L33-L41)：Host 侧 RMA API 的 device 内核入口——`aclshmemi_putmem` 等 `ACLSHMEM_GLOBAL` kernel 内部转调 `aclshmem_uint8_put_nbi` / `aclshmem_uint8_put`，即**高阶接口本体**；[L124-188](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/shmemi_device_rma.cpp#L124-L188) 的 `aclshmemi_prepare_and_post_rma` 按 op 与是否连续选择 kernel 并用 `<<<block_size, 0, acl_strm>>>` 下发。这说明 u3-l1 学过的 Host 侧 `aclshmem_putmem` 与本讲的 device 高阶接口是**同一套实现**的两层壳。

#### 4.4.4 代码实践

**实践目标**：跑通 rdma_demo，理解搬运方向；再仿写一个「反向 kernel」：PE1 向 PE0 的对称缓冲写数据、Host 侧校验；最后把搬运改成高阶接口并覆盖「向自己 put」的用例。

1. 操作步骤：
   1. 按 u1-l2 完成 `bash scripts/build.sh` 并 `source install/set_env.sh`；
   2. 运行 `bash examples/rdma_demo/run.sh -pes 2`，观察两个进程的输出；
   3. 阅读方向分析：kernel 中谁把数据推给谁？`dst == src` 同地址表达式分别落在哪个 PE 的堆上？
   4. 仿写反向 kernel（**示例代码，非项目原有代码**，可新建 `examples/` 下的本地试验目录，不要改动源码仓库文件）：

   ```cpp
   // 示例代码：反向 + 高阶接口演示（kernel 侧）
   #include "kernel_operator.h"
   #include "shmem.h"

   extern "C" [[bisheng::core_ratio(0, 1)]] __global__ __aicore__ void reverse_put_kernel(
       GM_ADDR buf, uint32_t elem_count)
   {
       int64_t my_rank = aclshmem_my_pe();
       __gm__ int32_t* base = (__gm__ int32_t*)buf;
       if (my_rank == 1) {
           // PE1：把 buf 的第 0 段（本 PE 视角）写到 PE0 的对称地址上 → 高阶接口自动选引擎
           aclshmem_int32_put(base, base + elem_count, elem_count, /*pe=*/0);
       }
       if (my_rank == 0) {
           // 对照实验：向自己 put，验证本 PE 拷贝走 MTE 守卫路径仍正确
           aclshmem_putmem(base + elem_count, base, elem_count * sizeof(int32_t), /*pe=*/0);
           aclshmemx_mte_quiet();  // nbi 语义收尾；阻塞接口本身已含 quiet
       }
   }
   ```

   Host 侧复用 `rdma_demo/main.cpp` 的骨架：两 PE 各 `aclshmem_malloc` 同尺寸缓冲、PE1 先用 `aclrtMemcpy` 填入已知模式（如 `100 + i`）、下发 kernel、同步后 PE0 拷回校验第 1 段等于该模式。
   5. 引擎守卫解读：回到 [src/device/gm2gm/shmem_device_rma.hpp:29-30](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/shmem_device_rma.hpp#L29-L30)，按 4.3.4 的三个问题写下自己的解释。
2. 需要观察的现象：
   - `run.sh` 下两个进程各自打印 `check transport result success, relative pe=0/1` 与 `[SUCCESS]`；
   - 反向 kernel 中 PE0 收到的数据等于 PE1 写入的模式；`pe == my_pe` 的那次 put 结果同样正确；
   - 把高阶 `aclshmem_int32_put` 的 `pe` 参数换成 `my_pe`，在 Ascend950 上也不会走 UDMA（可对照 UT 的注入手法自证）。
3. 预期结果：AllGather 校验全部通过；反向用例数据一致；本 PE put 行为正确。**运行输出待本地验证**（本讲义编写环境无 NPU，未能实际执行）。

#### 4.4.5 小练习与答案

**练习 1**：rdma_demo kernel 里为什么必须有第一道 `aclshmemx_roce_barrier_all()`？
**答案**：没有它的话，快的 PE 可能在慢的 PE 还没 `aclrtMemcpy` 完自己段之前就把「别人的段」拷走（AllGather 每人都要读所有段），读到旧值。第一道 barrier 把「各 PE 写自己段」与「各 PE 推数据」两个阶段隔开，是典型的两阶段集合通信模式。

**练习 2**：示例 kernel 用 `aclshmemx_roce_put_nbi` 而不是 `aclshmem_putmem`，两者的准备工作者差在哪？
**答案**：低阶接口需要调用者自己保证引擎可用（初始化开了 ROCE 位）、自己 `InitBuffer` 一块 ≥128 字节的 UB 供任务下发（WQE）；高阶接口从 `device_state` 的 `rdma_config`/`mte_config` 等字段自动取 UB 与事件号，按 `topo_list` 自动选引擎，调用者只给「地址 + 长度 + pe」。

**练习 3**：Host 侧 `aclshmem_putmem`（u3-l1）最终会执行到本讲的哪段代码？
**答案**：经 `aclshmemi_prepare_and_post_rma`（[shmemi_device_rma.cpp:124-188](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/shmemi_device_rma.cpp#L124-L188)）下发 `aclshmemi_putmem` kernel，kernel 内转调 `aclshmem_uint8_put`（[L38-41](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/src/device/gm2gm/shmemi_device_rma.cpp#L38-L41)），最终进入 `shmem_device_rma.hpp` 的四分支分派——Host API 与 device 高阶接口共用同一套引擎选择逻辑。

## 5. 综合实践

设计一个**两 PE 双向握手 + 自发送对照**的小任务，串起本讲全部知识点：

1. **任务**：PE0 与 PE1 各持一段 `N` 个 int32 的对称缓冲。kernel 内：PE0 用高阶 `aclshmem_int32_put` 把自己的段发给 PE1；PE1 等待（可用 u4-l4 将学的 `signal_wait_until`，或先用第二道 barrier 简化）后，把收到的数据加 1 再发回 PE0；随后**每个 PE 都对自己 put 一次**（`pe = my_pe`）。Host 侧校验：PE0 收到 `原值 + 1`，PE1 收到原值，两 PE 自发送段也正确。
2. **要求**：
   - 只用高阶接口，不直接调 `aclshmemx_*` 低阶函数（自发送后的收尾 quiet 除外）；
   - 记录每步观察：哪次调用走了哪个分支？依据是什么（`topo_list` + 编译期开关 + 守卫宏）？
   - 在 Ascend950 环境可加做：仿照 [tests/unittest/device/mem/udma_mem/udma_mem_kernel.cpp:153-155](https://github.com/gitcode.com/cann/shmem/blob/9afc3913406c7645448feca8cd65bd06bb9e61ce/tests/unittest/device/mem/udma_mem/udma_mem_kernel.cpp#L153-L155) 注入本 PE UDMA 位，验证自发送仍走 MTE。
3. **产出**：一段 kernel + Host 代码、一张「调用 → 分支」对照表、一段对 `(PE) != mype` 守卫必要性的文字论证。运行结果待本地验证。

## 6. 本讲小结

- gm2gm 高阶 RMA 是宏生成的接口族：13 种类型 × put/get × 阻塞/nbi × 指针/Tensor/跨步，声明在 `include/device/gm2gm/shmem_device_rma.h`，实现在 `src/device/gm2gm/shmem_device_rma.hpp`（`.h` 末尾直接 `#include` 实现层）。
- 引擎自动选择 = **编译期开关**（`ACLSHMEM_UDMA_SUPPORTED` 等）∧ **运行期 `topo_list[pe]` 位图**；位图由 Host 侧 `CanReachDataOperators` 计算并在建堆阶段随 `device_state` 下发。
- 分派优先级固定 **SDMA → UDMA → MTE → ROCE**；阻塞版 = `_nbi` 发起 + 立即对应引擎的 quiet。
- **本轮关键语义（`1e7fffb`）**：UDMA 不支持自发送。双层防御——Host 不为本 rank 通告 UDMA；分派宏 `ACLSHMEM_UDMA_TRANSPORT_ENABLED` 增加 `(PE) != mype` 守卫，使对本 PE 的 put/get 一律回落 MTE，即使 topo 数据异常也安全。
- `rdma_demo` 给出 kernel 内跨 PE 搬运的完整骨架（对称地址 + barrier + 低阶直驱接口）；高阶接口可替换低阶调用并自动获得引擎选择与本 PE 保护。
- Host 侧 `aclshmem_putmem` 与 device 高阶接口同源：前者经 `aclshmemi_prepare_and_post_rma` 下发 kernel，kernel 内转调 `aclshmem_uint8_put` 走同一四分支。

## 7. 下一步学习建议

- **u4-l3（AMO）**：同在 gm2gm 目录下的原子操作家族，宏生成模式与本讲如出一辙，可对照阅读 `shmem_device_amo.hpp`。
- **u4-l4 / u4-l5**：kernel 内同步原语（signal/wait）与内存保序（quiet 体系）——综合实践里「等数据到齐」一步的正解。
- **u5-l1 / u5-l6**：从传输层管理器视角回看 `topo_list` 的生产端（`CanReachDataOperators`、`TransportManager`），并系统学习 engine 目录下的低阶直驱接口，理解高阶/低阶的完整分层。
- 若你手头是 Ascend950 环境，强烈建议跑一遍 `TestShmemUDMAHighLevelLocalRma`（UT）与 `examples/tp_allreduce_udma`（u8-l9 会讲），把本讲的守卫语义放到真实 UDMA 链路上验证。
