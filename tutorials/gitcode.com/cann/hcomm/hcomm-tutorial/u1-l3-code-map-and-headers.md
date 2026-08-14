# 目录结构与代码地图：从 include 到 src

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 HCOMM 仓库中 `include/`、`pkg_inc/`、`src/` 三大目录的职责差异，理解"对外头文件 / 包间接口 / 内部实现"三个层次。
2. 在 `include/` 与 `pkg_inc/` 中快速定位自己需要的接口（通信域管理、资源、原语）。
3. 理解 `src/legacy/` 目录在新旧架构并存中的角色，避免把兼容代码误读为新架构。
4. 建立一张从"接口声明"到"内部实现"的代码地图，为后续深入源码打基础。

## 2. 前置知识

本讲是纯"读目录、认头文件"的地图课，不需要写代码，但需要理解几个 C/C++ 工程术语：

- **头文件（.h / .hpp）**：C/C++ 中声明函数、结构体、宏的文件。使用者只需 `#include` 头文件并链接库，就能调用其中声明的接口。头文件是"合同"，实现是"履约"。
- **API（Application Programming Interface）**：库暴露给外部使用者的函数集合。例如 `HcclCommInitRootInfo` 就是一个 API。
- **ABI（Application Binary Interface）**：比 API 更底层，关注的是二进制层面的兼容性——结构体每个字段在第几个字节、大小是多少。两个独立编译的模块（例如 host 侧 so 与 device 侧算子）要共享同一块内存时，必须对结构体布局达成 ABI 层面的共识。
- **弱符号（weak symbol）**：`__attribute__((weak))` 修饰的函数声明。链接时如果没有找到强符号实现，程序不会报链接错误，而是运行时通过 `dlsym` 动态加载。第 1 讂提过 HCCL 通过 `dlsym` 加载 HCOMM 接口，弱符号正是这种"可插拔"设计的基础。
- **通信域（Communicator）**：一组参与集合通信的进程/NPU 的抽象，是 HCCL/HCOMM 最核心的对象。
- **控制面 / 数据面**：第 1 讲建立的核心心智模型——`coll_communicator_mgr` 管通信域与拓扑（控制面），`base_comm` 管资源与数据搬运（数据面）。

## 3. 本讲源码地图

| 文件/目录 | 作用 |
|---|---|
| `include/` | 对外（面向最终用户与算子开发者）公开的头文件，按 `hccl/`、`hcomm_*.h`、`ccu/` 三层组织 |
| `include/hccl/hccl_comm.h` | 通信域生命周期 C 接口：初始化、销毁、查询、配置 |
| `include/hcomm_res.h` | 数据面资源 C 接口：Endpoint、内存注册、线程分配 |
| `include/hcomm_channel.h` | 通道描述符 `HcommChannelDesc` 及通道配置接口 |
| `pkg_inc/` | 包间接口头文件：给 CANN 内部其他组件（跨包/跨侧）使用的内部约定 |
| `pkg_inc/hcomm/hcomm_res_entity_defs.h` | 定义 `ChannelEntity` 等实体结构体，是 host 侧与 device 侧共享内存布局的 ABI 契约 |
| `pkg_inc/legacy/hccl/` | 旧架构遗留的包间头文件（`hccl_ex.h`、`hcom.h` 等） |
| `src/base_comm/` | 数据面实现：`resources`（资源）+ `primitives`（原语） |
| `src/coll_communicator_mgr/` | 控制面实现：`communicator`、`rank_graph`、`config_mgr`、`team` 等 9 个子目录 |
| `src/legacy/` | 旧架构兼容代码：`ascend910`（A2&A3）与 `ascend950`（A5） |
| `CMakeLists.txt` | 定义头文件如何被安装到 `include/` 与 `pkg_inc/` 两个安装目录 |

## 4. 核心概念与源码讲解

### 4.1 仓库三大目录：include / pkg_inc / src

#### 4.1.1 概念说明

打开仓库根目录，与代码相关的目录一共有四个：

```text
hcomm/
├── include/     ← 对外头文件：装进 CANN Toolkit 的 include 目录，最终用户可见
├── pkg_inc/     ← 包间接口：装进独立的 pkg_inc 目录，CANN 内部组件之间共享
├── src/         ← 内部实现：base_comm / coll_communicator_mgr / legacy 三大块
└── experimental/ ← 实验性代码（第 1 讲已介绍，本讲不展开）
```

三者最本质的区别是**受众不同**：

- `include/` 的受众是**所有开发者**——训练框架开发者、通信算子开发者都会直接 `#include` 这些头文件。
- `pkg_inc/` 的受众是**CANN 内部其他软件包**（以及 device 侧代码）。它安装到独立的 `pkg_inc` 安装目录，接口约定可以随内部需要演进，不向外部承诺长期稳定。
- `src/` 的受众**只有编译器**——里面的头文件（如 `src/base_comm/hcomm_res_mgr.h`）是内部实现细节，随时可以重构，不受兼容性约束。

#### 4.1.2 核心流程

安装时，CMakeLists.txt 把两组头文件分发到两个不同的安装目录：

```text
CMakeLists.txt install 规则
    ├── include/hccl/*.h、include/hcomm_*.h ──→ ${INSTALL_INCLUDE_DIR}/hccl/ 或 hcomm/
    ├── include/ccu/*.hpp                    ──→ ${INSTALL_INCLUDE_DIR}/hcomm/ccu/
    ├── pkg_inc/hccl/*.h、pkg_inc/legacy/**  ──→ ${INSTALL_PKG_INCLUDE_DIR}/hccl/
    └── pkg_inc/hcomm/*.h                    ──→ ${INSTALL_PKG_INCLUDE_DIR}/hcomm/
```

其中 `INSTALL_PKG_INCLUDE_DIR` 定义为独立的 `pkg_inc` 目录：

[cmake/config.cmake:39](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/cmake/config.cmake#L39) —— 把包间头文件的安装目录设为 `${CMAKE_SYSTEM_PROCESSOR}-linux/pkg_inc`，与对外头文件的安装目录分开。

对外的 hcomm 资源头文件安装规则：

[CMakeLists.txt:185-192](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/CMakeLists.txt#L185-L192) —— 将 `hcomm_res.h`、`hcomm_res_defs.h`、`hcomm_channel.h`、`hcomm_team_defs.h` 安装到 `include/hcomm/` 下，这些就是数据面对外承诺的接口面。

包间头文件的安装规则：

[CMakeLists.txt:246-252](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/CMakeLists.txt#L246-L252) —— 将 `hcomm_primitives_expt.h`、`hcomm_res_entity_defs.h`、`hcomm_team_entity_defs.h`、`hcomm_exception.h` 安装到 `pkg_inc/hcomm/` 下，受众是 CANN 内部组件而非最终用户。

#### 4.1.3 源码精读

`include/` 顶层一览（可用 `ls include` 验证）：

```text
include/
├── ccu/                  ← CCU 算子开发者的 C++ 原语头文件（16 个 .hpp/.h）
├── hccl/                 ← L1/L2 层 C 接口（hccl_comm.h、hccl_res.h、hccl_team.h 等 9 个）
├── hcomm_channel.h       ← L3 通道描述符
├── hcomm_primitives.h    ← L3 原语
├── hcomm_res.h           ← L3 资源
├── hcomm_res_defs.h      ← L3 公共类型定义
└── hcomm_team_defs.h     ← Team 相关类型定义
```

`pkg_inc/` 顶层一览：

```text
pkg_inc/
├── hccl/      ← hccl_res_expt.h、hccl_diag.h、hcomm_diag.h（实验/诊断包间接口）
├── hcomm/     ← hcomm_res_entity_defs.h、hcomm_team_entity_defs.h、hcomm_exception.h、
│                 hcomm_primitives_expt.h、ccu/ccu_primitives_impl.h
└── legacy/hccl/ ← 旧架构 8 个头文件（base.h、hccl_ex.h、hcom.h、workflow.h 等）
```

`src/` 顶层一览：

```text
src/
├── base_comm/             ← 数据面：common / primitives / resources / dfx + hcomm_res_mgr
├── coll_communicator_mgr/ ← 控制面：api_c_adpt / communicator / config_mgr / rank_graph /
│                            rank_info_detect / resource_mgr / team / common / dfx
└── legacy/                ← ascend910 / ascend950 旧架构兼容
```

#### 4.1.4 代码实践

1. **实践目标**：亲手验证三大目录的边界，而不是背结论。
2. **操作步骤**：
   - 在仓库根目录执行 `ls include hccl`、`ls pkg_inc/hcomm`、`ls src/base_comm src/coll_communicator_mgr src/legacy`，核对上面的目录树。
   - 打开 [CMakeLists.txt:180-252](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/CMakeLists.txt#L180-L252)，数一数：进入 `include/` 安装目录的头文件有多少个？进入 `pkg_inc/` 的有多少个？
3. **需要观察的现象**：`include/ccu/` 下 16 个头文件全部进入安装列表，而 `src/` 下任何头文件都不在安装列表中。
4. **预期结果**：能回答"如果我要新增一个对外接口，应该把声明放在哪个目录、还要改哪个构建文件"。

> 本实践为纯阅读型，无需硬件，也无需编译。

#### 4.1.5 小练习与答案

**练习 1**：`pkg_inc` 与 `include` 里的头文件都是"公开"的，它们的本质区别是什么？

**答案**：受众与稳定性承诺不同。`include` 面向所有外部开发者，接口需保持兼容；`pkg_inc` 是 CANN 内部组件之间的约定（"包间"接口），安装到独立目录，仅供内部跨包/跨侧共享，不向最终用户承诺稳定。

**练习 2**：为什么 `src/` 下的头文件不出现在 CMakeLists.txt 的 install 列表中？

**答案**：`src/` 头文件是实现细节（如 `src/base_comm/hcomm_res_mgr.h` 是内部管理器类），库的使用者只依赖 `include/` 声明的接口；不安装内部头文件可以让实现自由重构而不破坏使用方。

**练习 3**：`pkg_inc/legacy/hccl/` 中的头文件为什么单列一个 `legacy` 子目录？

**答案**：它们属于旧架构（ascend910/ascend950 流程）的包间约定，与 `pkg_inc/hcomm/`（新架构包间接口）分开存放，提醒读者这是兼容路径，不承载新特性。

---

### 4.2 include/ 对外头文件分层：hccl_comm.h 与 hcomm_res.h

#### 4.2.1 概念说明

`include/` 内部又按受众分成三层，第 1 讲提过 L1/L2/L3 的分层，本讲落到具体文件：

| 层次 | 文件 | 受众 | 内容 |
|---|---|---|---|
| L2 通信域管理 | `include/hccl/hccl_comm.h` | 训练框架/集合通信用户 | 通信域生命周期接口 |
| L2 资源与拓扑 | `include/hccl/hccl_res.h`、`hccl_team.h`、`hccl_rank_graph.h` 等 | 通信算子开发者 | 线程、Team、拓扑查询 |
| L3 原语与基础资源 | `include/hcomm_res.h`、`hcomm_channel.h`、`hcomm_primitives.h` | 通信算子开发者（新开放） | Endpoint、内存、通道、原语 |

两个代表文件：

- **`hccl_comm.h`** 是最传统的一层：`HcclCommInitRootInfo`、`HcclCommDestroy` 等接口和 NCCL 的 `ncclCommInitRank` 是同一抽象层级，面向"我要做 AllReduce"的用户。
- **`hcomm_res.h`** 是新开放的数据面底层资源层：Endpoint、内存注册、线程分配，面向"我自己写通信算子"的开发者。

#### 4.2.2 核心流程

一个接口从声明到被用户调用的路径：

```text
用户代码
  #include <hccl/hccl_comm.h>          （对外头文件，合同）
        ↓ 链接期
  弱符号声明（HCOMM_WEAK_SYMBOL）
        ↓ 运行期 dlsym 动态加载（第 1 讲介绍的 HCCL↔HCOMM 解耦机制）
  HCOMM 库内部实现（src/coll_communicator_mgr/...，履约）
```

注意 `hccl_comm.h` 中几乎所有接口声明都带 `HCOMM_WEAK_SYMBOL` 宏，这就是"可被动态加载替换"的机关。

#### 4.2.3 源码精读

弱符号宏定义：

[include/hccl/hccl_comm.h:19-21](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_comm.h#L19-L21) —— 把 `HCOMM_WEAK_SYMBOL` 定义为 `__attribute__((weak))`，让接口声明成为弱符号，运行时才能被 `dlsym` 找到的强实现替换。

通信域初始化接口族（文件中共 17 个弱符号接口）：

[include/hccl/hccl_comm.h:36-102](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_comm.h#L36-L102) —— 依次声明 `HcclCommInitClusterInfo`（rank table 文件方式）、`HcclCreateSubCommConfig`（子通信域）、`HcclGetRootInfo` + `HcclCommInitRootInfo`（root info 方式）及其带 `Config` 的变体。这就是第 4 讲示例程序会用到的初始化入口。

配置初始化的内联辅助函数：

[include/hccl/hccl_comm.h:202-240](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_comm.h#L202-L240) —— `HcclCommConfigInit` 是一个 `static inline` 函数，直接在头文件里实现：把 `HcclCommConfig` 结构体的每个字段填上默认值（如 `hcclDeterministic`、`hcclExecTimeOut` 等）。这说明**头文件里也可以有实现**，短小的工具函数内联在头文件中可以随头文件分发给所有使用方。

再看数据面资源层 `hcomm_res.h`，整个文件只有约 15 个接口，全部是资源生命周期操作：

[include/hcomm_res.h:21-23](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hcomm_res.h#L21-L23) —— `HcommEndpointCreate/HcommEndpointDestroy`：端点（通信端口资源）的创建与销毁。

[include/hcomm_res.h:32-42](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hcomm_res.h#L32-L42) —— `HcommEndpointGetDescNum/GetDescs`：经典的"两段式查询"接口设计——先查数量、再按数量申请数组取内容。注释是中文的，这在 `include/hcomm_*.h` 系列中很常见，说明这批接口是面向国内算子开发者新开放的。

[include/hcomm_res.h:44-55](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hcomm_res.h#L44-L55) —— `HcommMemReg/HcommMemUnreg/HcommMemExport/HcommMemImport/HcommMemUnimport`：内存注册与跨进程交换五连。本端注册内存 → 导出描述符 → 对端导入，这条链路是远端直接内存访问（单边读写）的前提，第 3 单元会详细拆解。

[include/hcomm_res.h:57-64](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hcomm_res.h#L57-L64) —— `HcommThreadAlloc/HcommThreadFree/HcommThreadResGetInfo`：通信线程资源的分配与查询。注意这里的"线程"是数据面执行通信原语的载体，与操作系统线程不完全等价（第 3 单元展开）。

通道描述符（属于 L3，但本讲先认识它的位置）：

[include/hcomm_channel.h:33-70](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hcomm_channel.h#L33-L70) —— `HcommChannelDesc` 结构体：包含 ABI 头、远端端点描述、notify 数量、内存句柄数组、socket 角色，以及一个按协议（roce/hccs/ub/ubMem）取值的 union。注释明确说明"结构体末尾扩展需要自增版本号"——这是 ABI 兼容的关键设计，第 3 单元第 4 讲专门讲。

#### 4.2.4 代码实践

1. **实践目标**：通过 grep 亲手感受两层的规模与风格差异。
2. **操作步骤**：
   - 执行 `grep -c HCOMM_WEAK_SYMBOL include/hccl/hccl_comm.h`，确认弱符号接口数量。
   - 执行 `grep -n "^extern" include/hcomm_res.h`，列出全部资源接口。
   - 对比两者的注释风格：`hccl_comm.h` 是英文 doxygen，`hcomm_res.h` 混有中文 `@brief`。
3. **需要观察的现象**：`hcomm_res.h` 中所有接口都以 `Hcomm` 前缀 + 资源名词开头（Endpoint/Mem/Thread），命名高度规整。
4. **预期结果**：能仅凭接口名推断出资源类型与操作类型（Create/Destroy/Reg/Import...）。

> 本实践为源码阅读型，无需硬件。

#### 4.2.5 小练习与答案

**练习 1**：`HcclCommInitRootInfo` 和 `HcclCommInitClusterInfo` 分别对应哪种初始化方式？

**答案**：前者是 root info 方式——rank 0 调 `HcclGetRootInfo` 生成握手信息并广播给所有 rank；后者是 cluster info（rank table 文件）方式——从文件读取各 rank 的地址信息完成建域。u1-l4 示例课会分别演示。

**练习 2**：`HcommEndpointGetDescNum` 和 `HcommEndpointGetDescs` 为什么要设计成两个接口？

**答案**：调用方事先不知道设备上有多少个 Endpoint 描述符，无法确定数组该开多大。先调 GetDescNum 拿到数量，再按数量申请数组调 GetDescs，是 C 接口中经典的"两段式查询"模式，避免固定上限也避免多次 realloc。

**练习 3**：为什么 `hccl_comm.h` 的接口要声明为弱符号，而 `hcomm_res.h` 的接口不用？

**答案**：`hccl_comm.h` 的接口历史上由 HCCL 算子库经 `dlsym` 动态加载调用，弱符号保证使用方在链接期不依赖 HCOMM 的强实现（可以运行时再绑定）；`hcomm_res.h` 是新开放的算子开发接口，由算子开发者直接链接 HCOMM 库使用，不需要这种可插拔机制。

---

### 4.3 pkg_inc/ 包间接口：hcomm_res_entity_defs.h 的 ABI 契约

#### 4.3.1 概念说明

`pkg_inc` 解决的是一个更刁钻的问题：**两个独立编译的模块如何共享同一段内存的布局约定**。

场景是这样的：HCOMM 在 host 侧（CPU 上的库代码）创建好 channel，把 `ChannelEntity` 结构体拷贝到 device 显存；CCU/AIV 等设备侧通信算子（另一个编译产物，甚至是另一个团队用另一套工具链编译的）从显存读这个结构体并按字段使用。两边必须对"`localNotifyNum` 在第几个字节、union 里 ub 和 roce 各怎么解释"达成**字节级**一致——这就是 ABI 契约。

`pkg_inc/hcomm/hcomm_res_entity_defs.h` 就是这份契约的书面形式。它被安装到 `pkg_inc` 目录，供 device 侧编译环境（如算子工程）包含，所以叫"包间接口"——HCOMM 包与其他 CANN 包之间的接口。

#### 4.3.2 核心流程

```text
host 侧（src/ 下编译进 libhcom.so）
    HcommChannelCreate ...
        ↓ 生成 ChannelEntity（256B 定长）
        ↓ hrtMalloc 在 device 显存分配 totalChannels * sizeof(ChannelEntity)
        ↓ 逐个 D2H→D 拷贝到连续显存
device 侧（算子，独立编译，#include <pkg_inc 的 hcomm_res_entity_defs.h>）
    按同一份结构体定义解析显存中的 ChannelEntity → 拿到通知/缓冲/队列上下文
```

关键点：两侧不是"调用对方的函数"，而是"共享对方的内存"，因此契约必须在结构体布局层面（ABI），而不只是函数签名层面（API）。

#### 4.3.3 源码精读

协议判别枚举（tagged union 的"tag"）：

[pkg_inc/hcomm/hcomm_res_entity_defs.h:24-28](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/pkg_inc/hcomm/hcomm_res_entity_defs.h#L24-L28) —— `ProtectionType` 区分 ROCE 与 UB 两种内存保护域类型。device 侧读内存前必须先看 tag，才知道 union 里装的是 `lkey/rkey`（ROCE 的键值对）还是 `tokenId/tokenValue`（UB 的令牌）。

tagged union 的第一个例子：

[pkg_inc/hcomm/hcomm_res_entity_defs.h:56-69](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/pkg_inc/hcomm/hcomm_res_entity_defs.h#L56-L69) —— `ProtectionInfo`：`type` 字段判别类型，union 内 ROCE 用 `lkey/rkey`（本端/远端访问键），UB 用 `tokenId/tokenValue`，末尾还有 `uint8_t raws[24]` 兜底缓存，整个结构体注释标明固定 32 字节。**定长 + 尾部 raw 兜底**是跨侧结构体的通用设计：字段布局永远不因协议不同而改变总大小。

channel 实体本体：

[pkg_inc/hcomm/hcomm_res_entity_defs.h:171-188](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/pkg_inc/hcomm/hcomm_res_entity_defs.h#L171-L188) —— `ChannelEntity`：以 `CommAbiHeader abiHeader` 开头（版本信息，与 `HcommChannelDesc` 的 ABI 头呼应），随后是引擎、协议、通知/缓冲/队列数量，再是指向 `RegedNotifyEntity`、`RegedBufferEntity`、`SqContext`、`CqContext` 数组的指针，末尾 `reserve[160]` 预留，整体定长 256 字节。

host 侧写入方（消费这份契约的内部代码）：

[src/coll_communicator_mgr/team/hcomm/hcomm_team_mgr.cc:172-186](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/coll_communicator_mgr/team/hcomm/hcomm_team_mgr.cc#L172-L186) —— `HcclTeamChannelsCreate` 的内部实现里，先 `hrtMalloc` 分配 `totalChannels * sizeof(ChannelEntity)` 的显存，再逐个 channel 把 `ChannelHandle` 指向的 `ChannelEntity` 本体 D2D 拷贝到连续显存对应偏移。这正是上面流程图 host 侧那一半的真实代码。它 include 的正是 `pkg_inc/hcomm/hcomm_res_entity_defs.h`。

除 `hcomm_res_entity_defs.h` 外，`pkg_inc/hcomm/` 下还有三个包间头文件，各自承载一类内部约定：

- `hcomm_team_entity_defs.h` —— Team 相关实体结构。
- `hcomm_exception.h` —— 异常/错误通知的包间定义。
- `ccu/ccu_primitives_impl.h` —— CCU 原语的内部实现入口，配合 `include/ccu/*.hpp`（对外 C++ 原语）形成"对外模板 + 对内实现"的两层。

#### 4.3.4 代码实践

1. **实践目标**：验证"同一份头文件被 host 实现代码与 device 侧代码共同包含"。
2. **操作步骤**：
   - 执行 `grep -rn "hcomm_res_entity_defs" src/ pkg_inc/ --include=*.h --include=*.cc -l`，列出所有包含者。
   - 观察结果里既有 `src/coll_communicator_mgr/team/hcomm/hcomm_team_mgr.cc`（host 侧管理器），也有 `src/base_comm/resources/endpoint_pairs/channels/aiv/aiv_urma_transport.h`、`src/base_comm/resources/endpoint_pairs/channels/aicpu/aicpu_ts_roce_channel_v2.h`（设备侧通道实现）。
3. **需要观察的现象**：包含者横跨控制面（team 管理器）与数据面（endpoint_pairs 下的 aiv/aicpu 通道），说明这份契约是全仓库共享的。
4. **预期结果**：理解"一处定义、多处按 ABI 消费"——修改 `ChannelEntity` 布局会影响所有这些文件，且必须同步升级版本号。

> 本实践为源码阅读型，无需硬件。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ChannelEntity` 要做成固定 256 字节、末尾还留 `reserve[160]`？

**答案**：跨侧共享内存要求布局绝对稳定。定长使数组式分配（`totalChannels * sizeof(ChannelEntity)`）和偏移寻址简单可靠；尾部预留让未来新增字段时只消耗 reserve 空间、不改变总大小，配合 ABI 版本号即可实现向后兼容。

**练习 2**：`ProtectionInfo` 里 ROCE 的 `lkey/rkey` 各是什么？

**答案**：RDMA/RoCE 内存注册后得到的访问键：`lkey`（local key）用于本端访问该注册内存，`rkey`（remote key）供远端通过单边读写（RDMA WRITE/READ）访问。对端拿到 rkey 才能直接写这块内存。

**练习 3**：如果我要给 `ChannelEntity` 新增一个字段，正确的做法是什么？

**答案**：优先占用 `reserve` 区域（总大小不变）；同时自增 `CommAbiHeader` 中的版本号，并在读取方按版本号判断字段是否存在——与本讲 4.2 节 `HcommChannelDesc` 的 ABI v1→v3 演进（`HCOMM_CHANNEL_DESC_ABI_V1_SIZE`）是同一套手法。

---

### 4.4 src/ 三大子目录与 legacy 的角色

#### 4.4.1 概念说明

`src/` 下只有三个目录，但代表三个时代/三种角色：

```text
src/
├── coll_communicator_mgr/  ← 新架构·控制面（第 2 单元主线）
├── base_comm/              ← 新架构·数据面（第 3 单元主线）
└── legacy/                 ← 旧架构兼容层（第 6 单元展开）
    ├── ascend910/          ← A2&A3 平台旧流程：algorithm/framework/hccd/platform 等
    └── ascend950/          ← A5 平台旧流程：framework/interface/service/unified_platform 等
```

关键认知（承接第 1 讲）：**新特性只进 `base_comm` 与 `coll_communicator_mgr`**。`legacy` 的存在是为了让老平台（已在现网运行）的 HCCL 继续工作，属于"维护态"代码。

`base_comm` 内部再分两个子模块：

```text
src/base_comm/
├── hcomm_res_mgr.h/.cc   ← 数据面资源总管理器（对上承接 hcomm_res.h 接口）
├── resources/            ← 资源实现：endpoints、reged_mems、endpoint_pairs(channels)、
│                           comm_engine_res(线程)、ccu、hccp、southbound_adpt
├── primitives/           ← 原语实现：aicpu、api_c_adpt、launch_context
├── common/ 与 dfx/       ← 公共工具与维测
```

`coll_communicator_mgr` 的 9 个子目录则对应控制面的职责切分：`api_c_adpt`（C 接口适配）、`communicator`（通信域对象）、`config_mgr`（配置）、`rank_graph`（拓扑）、`rank_info_detect`（建链探测）、`resource_mgr`（资源管理）、`team`（Team 机制）、`common`、`dfx`——这正好是第 2 单元各讲的目录地图。

#### 4.4.2 核心流程

一个 C 接口调用在 `src/` 内的典型穿透路径（以通信域接口为例）：

```text
用户调 HcclCommInitRootInfo(...)（include/hccl/hccl_comm.h 声明）
    ↓
src/coll_communicator_mgr/api_c_adpt/   ← C 接口适配层：C 声明 → 内部 C++ 对象
    ↓
src/coll_communicator_mgr/communicator/ ← CollComm 通信域对象、CollCommMgr 管理器
    ↓ （创建数据面资源时）
src/base_comm/hcomm_res_mgr             ← 数据面资源总入口
    ↓
src/base_comm/resources/...             ← 具体资源实现（endpoints/reged_mems/...）
```

依赖方向严格自上而下（第 1 讲的架构铁律）：`coll_communicator_mgr` → `base_comm`，绝不反向。

#### 4.4.3 源码精读

数据面资源总管理器：

[src/base_comm/hcomm_res_mgr.h:1](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/base_comm/hcomm_res_mgr.h#L1) —— `HcommResMgr` 位于 `src/base_comm/` 根上，是 `include/hcomm_res.h` 中那批资源接口的内部实现归宿，把 Endpoint/内存/线程/通道资源统一管理（第 3 单元第 1 讲精读）。

legacy 下的平台目录：

[src/legacy/ascend910](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend910) —— A2&A3 平台旧流程，含 `algorithm`（旧算法实现）、`framework`、`hccd`、`platform` 等子目录，是完整一套旧集合通信实现。

[src/legacy/ascend950](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/src/legacy/ascend950) —— A5 平台旧流程，含 `interface`（新旧架构桥接的适配层）、`unified_platform`、`service` 等。其中 `interface` 目录是识别"legacy 如何被桥接到新架构"的关键线索（第 6 单元第 4 讲会精读）。

构建上，legacy 是按平台条件编译进产物的（不是死代码）：

[CMakeLists.txt:194-213](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/CMakeLists.txt#L194-L213) —— `HCCL_PKG_HEAD` 列表把 `pkg_inc/legacy/hccl/` 下 8 个旧架构头文件（`base.h`、`hccl_ex.h`、`hcom.h`、`workflow.h` 等）一并随包安装到 `pkg_inc/hccl/` 目录，说明现网仍有组件依赖 legacy 的包间头文件——这就是"新旧并存"的实际含义。

#### 4.4.4 代码实践

1. **实践目标**：给 `src/` 画一张"新/旧架构"边界图，防止后续读源码时迷路。
2. **操作步骤**：
   - 执行 `ls src/coll_communicator_mgr`，把 9 个子目录与下表对照：

   | 子目录 | 职责 | 对应讲义 |
   |---|---|---|
   | api_c_adpt | C 接口适配层 | u2-l1 |
   | communicator | 通信域对象与管理器 | u2-l2 |
   | config_mgr | 配置管理 | u2-l3 |
   | rank_graph | 拓扑查询 | u2-l4 |
   | rank_info_detect | 建链探测 | u2-l5 |
   | team | Team 机制 | u2-l6 |
   | resource_mgr / common / dfx | 资源/公共/维测 | 分散在各讲 |

   - 执行 `ls src/legacy/ascend950`，找到 `interface` 目录并用 `ls` 查看其中的文件，感受"桥接层"的存在。
3. **需要观察的现象**：`src/coll_communicator_mgr` 的子目录名与第 2 单元讲义编号几乎一一对应——大纲就是按目录结构拆的。
4. **预期结果**：拿到任何一个文件路径（如 `src/base_comm/resources/endpoints/endpoint.h`），能立刻说出它属于哪一面、哪一层、是否新架构。

> 本实践为源码阅读型，无需硬件。

#### 4.4.5 小练习与答案

**练习 1**：`src/legacy/ascend950/interface` 目录的存在意义是什么？

**答案**：它是旧架构与新架构之间的桥接/适配层：旧调用入口经它转接到新架构实现，使 A5 平台在不大改上层的情况下平滑迁移。识别这个边界能避免把 legacy 调用链误当成新架构主链路。

**练习 2**：路径 `src/base_comm/primitives/aicpu/aicpu_task_cache_c_adpt.cc` 属于哪一面、什么职责？

**答案**：属于数据面（base_comm）的 primitives 子模块，位于 aicpu 子目录，是 AICPU 侧任务缓存原语的 C 接口适配实现（u5-l5 会精读）。判断依据：`base_comm` → 数据面；`primitives` → 原语；`*_c_adpt` 后缀 → C 接口适配层。

**练习 3**：为什么 HCOMM 要保留两套（新旧）实现而不是直接删掉 legacy？

**答案**：现网仍有运行 ascend910/ascend950 平台的集群，其 HCCL 依赖旧流程与旧包间头文件（`pkg_inc/legacy/hccl/` 仍随包发布）。删除会破坏兼容；隔离在 `src/legacy/` 下则可以让新架构（base_comm + coll_communicator_mgr）独立演进、互不污染。

## 5. 综合实践

**任务：制作你自己的「HCOMM 接口速查表」**（本讲正式实践任务）。

1. **实践目标**：把本讲的目录地图转化为一份可长期使用的接口索引表。
2. **操作步骤**：
   1. 从 `include/` 中挑 5 个接口，建议覆盖三个层次，例如：
      - `HcclCommInitRootInfo`（[include/hccl/hccl_comm.h:86-87](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_comm.h#L86-L87)，通信域管理）
      - `HcclCommDestroy`（[include/hccl/hccl_comm.h:167](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_comm.h#L167)，通信域管理）
      - `HcommEndpointCreate`（[include/hcomm_res.h:21](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hcomm_res.h#L21)，资源）
      - `HcommMemReg`（[include/hcomm_res.h:44-45](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hcomm_res.h#L44-L45)，资源）
      - `HcclThreadAcquireWithConfig`（[include/hccl/hccl_res.h:85](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_res.h#L85)，资源）
   2. 从 `pkg_inc/` 中挑 5 个"接口/实体"，建议：
      - `ChannelEntity`（`hcomm_res_entity_defs.h`，实体契约）
      - `ProtectionInfo`、`RegedBufferEntity`、`SqContext`（同文件，实体契约）
      - `hcclCommDfx` 或 `hcomm_diag.h` 中的诊断接口（维测包间接口，可自行 grep 选取）
   3. 为每个条目记录 4 列信息，形成如下格式的表：

   | 接口/实体 | 所在头文件 | 层次（通信域管理/资源/原语/实体契约） | 用途一句话 |
   |---|---|---|---|
   | `HcclCommInitRootInfo` | include/hccl/hccl_comm.h | 通信域管理 | 用 root info 创建通信域 |
   | ... | ... | ... | ... |

3. **需要观察的现象**：整理过程中会发现 `include/hccl/`、`include/hcomm_*`、`pkg_inc/hcomm/` 三组文件的前缀与命名规律（`Hccl*` 控制面、`Hcomm*` 数据面、`*Entity/*Defs` 包间契约）。
4. **预期结果**：一张 10 行左右的速查表（Markdown 文件即可，建议存在本讲义同目录之外的个人笔记中），后续单元读源码时用它快速定位接口。

> 本实践为源码阅读型，全程无需硬件、无需编译。

## 6. 本讲小结

- 仓库三大目录按受众分层：`include/`（对外）、`pkg_inc/`（CANN 包间）、`src/`（内部实现），CMakeLists.txt 用两条 install 规则把它们分发到不同安装目录。
- `include/hccl/hccl_comm.h` 承载通信域生命周期接口（17 个弱符号接口），弱符号机制支撑 HCCL 运行时 `dlsym` 动态加载 HCOMM。
- `include/hcomm_res.h` 是新开放的数据面资源层：Endpoint 两段式查询、内存注册/导入五连、线程分配，命名规整（`Hcomm` + 资源名 + 操作）。
- `pkg_inc/hcomm/hcomm_res_entity_defs.h` 是 host 侧与 device 侧共享内存布局的 ABI 契约：tagged union + 定长 + reserve 兜底 + 版本号。
- `src/` 三分：`coll_communicator_mgr`（控制面，9 个子目录）、`base_comm`（数据面，resources + primitives）、`legacy`（ascend910/ascend950 维护态兼容，不承载新特性）。
- 依赖方向严格自上而下：控制面 → 数据面；`base_comm/primitives` 与 `resources` 是数据面的两大子模块。

## 7. 下一步学习建议

- 下一讲（u1-l4）将用本讲认识的 `HcclCommInitRootInfo`/`HcclCommInitClusterInfo` 接口跑通第一个 AllReduce 示例，把"接口表"变成"可运行程序"。
- 想先深入数据面接口的读者，可提前浏览 [include/hcomm_primitives.h](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hcomm_primitives.h)，那里是第 3 单元原语课的主战场。
- 对 ABI 兼容设计感兴趣的读者，建议精读 [include/hcomm_channel.h:24-70](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hcomm_channel.h#L24-L70) 中版本号与 `HCOMM_CHANNEL_DESC_ABI_V1_SIZE` 的注释，为 u3-l4 做准备。
