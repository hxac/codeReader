# 初始化 API 与三种 Bootstrap 模式

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐字段说出 `aclshmemx_init_attr_t`（必选属性）与 `aclshmem_init_optional_attr_t`（可选属性）中每个成员的含义与默认值。
2. 区分 `ACLSHMEMX_INIT_WITH_DEFAULT`、`ACLSHMEMX_INIT_WITH_MPI`、`ACLSHMEMX_INIT_WITH_UNIQUEID` 三种 bootstrap 模式各自的依赖条件、适用场景，以及它们在源码中被分发到哪个 bootstrap 插件。
3. 理解 uniqueid 的「PE 0 生成 + 外部信道广播」使用范式，能独立写出完整的 UID 模式初始化代码。
4. 亲手编写并运行一个 MPI 模式、4 个 PE 的最小初始化程序，验证每个 PE 的 `my_pe` 互不相同。
5. 掌握 `aclshmemx_set_qp_num` 的引擎约束（本轮已扩展为支持 ROCE 与 UDMA）、取值范围、以及「初始化前配置、实例存活期冻结、最后一个实例 finalize 后复位」的生命周期规则。

本讲建立在 u1-l4 之上。u1-l4 已经给出了 `aclInit → aclrtSetDevice → aclshmemx_init_attr → 业务 → shmem_finalize` 的调用骨架和「初始化是集体操作」的结论；本讲不再重复骨架，而是把放大镜对准骨架中最复杂的一环——**初始化属性结构体、bootstrap 模式选择与 QP 数配置**。

> 更新说明：随 Ascend950 RDMA 多 QP 能力落地（commit `8d1d777`），`aclshmemx_set_qp_num` 从「仅支持 UDMA」扩展为「支持 ROCE 与 UDMA」，本讲相应新增 4.4 节，并刷新了全部永久链接。

## 2. 前置知识

### 2.1 什么是 Bootstrap（引导建链）

SHMEM 程序启动时是 N 个**互相不认识**的独立进程：每个进程只知道自己的设备号，不知道其他 PE 在哪台机器、监听哪个端口。初始化的第一件事就是把这群进程「组织起来」——交换彼此的地址信息、建立控制面连接。这一步在源码里叫 **bootstrap（引导）**。

「谁来组织」是个现实问题：有的用户用 `mpirun` 拉起进程（天然有 MPI 可以帮忙）；有的用户用自研训练框架（框架自己有组网信道）；有的用户只是用脚本循环启动进程（谁也不帮，只能自建服务）。SHMEM 因此提供三种 bootstrap 模式，对应这三种「谁来当介绍人」的现实。

### 2.2 插件式动态加载（dlopen / dlsym）

bootstrap 的具体实现被做成独立的 `.so` 插件。主库在初始化时用 `dlopen` 打开对应插件、用 `dlsym` 取出约定好的入口函数。这样做的好处是：MPI 依赖不会被强加给不用 MPI 的用户——只有选了 MPI 模式才会加载 MPI 插件。

### 2.3 MPI 最小常识

本讲的实践需要一点 MPI 基础，只需认识 4 个调用：

| MPI 调用 | 作用 |
| --- | --- |
| `MPI_Init` / `MPI_Finalize` | 启动 / 结束 MPI 运行时 |
| `MPI_Comm_rank` / `MPI_Comm_size` | 查询自己在通信域里的编号 / 总进程数 |
| `MPI_Bcast` | 从根进程向所有进程广播一段内存 |

### 2.4 版本号的编码习惯

SHMEM 用公式 \( \text{version} = (\text{major} \ll 16) + \text{sizeof}(\text{结构体}) \) 给属性结构体编版本号：高 16 位是主版本，低 16 位塞进了结构体字节数。这样库在校验时能直接发现「用户头文件和库版本的结构体大小对不上」这类 ABI 不匹配问题（具体校验分支在 u2-l2 源码走读中确认）。

### 2.5 什么是 QP（Queue Pair，队列对）

QP 是 RDMA 系通信的基本单位：一条 QP 由「发送队列 + 接收队列」组成，两端各持一半，构成一条点对点传输通道。默认情况下，SHMEM 为**每个对端（peer）只建 1 条 QP**——所有发往同一个 PE 的流量都挤在这条队列上。当单条 QP 的吞吐成为瓶颈时，可以为每个 peer 建多条 QP，让流量分摊到多条队列并行传输（典型场景是 Ascend950 上的 ROCE / UDMA 大带宽通信）。`aclshmemx_set_qp_num` 就是用来在**初始化之前**告诉库「每个 peer 要建几条 QP」的配置接口。QP 的建链细节在 u5-l3 / u5-l7 展开，本讲只关注「怎么配置、何时能配置」。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [include/host/shmem_host_def.h](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host/shmem_host_def.h) | 本讲主战场之一：初始化属性结构体、bootstrap 枚举、uniqueid 结构体、错误码、常量（含 `ACLSHMEM_MAX_QP_NUM`）的定义 |
| [include/host/init/shmem_host_init.h](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host/init/shmem_host_init.h) | 本讲另一主战场：`aclshmemx_init_attr`、`aclshmemx_get_uniqueid`、`aclshmemx_set_attr_uniqueid_args`、`aclshmemx_set_qp_num`、`shmem_finalize` 等初始化 API 的声明 |
| [src/host/init/shmem_init.cpp](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp) | 上述 API 的实现：QP 配置全局量与冻结/复位生命周期、属性合入、初始化主流程入口 |
| [src/host/init/backends/shmemi_init_backend.h](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/backends/shmemi_init_backend.h) | `entity_member` 结构：QP 数随实体（entity）暂存的中转站 |
| [src/host/init/backends/shmem_init_backend.cpp](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/backends/shmem_init_backend.cpp) | `bind_aclshmem_entity` / `create_entity`：把 QP 配置从全局量装进 `TransportOptions` 交给传输层 |
| [examples/init/main.cpp](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/examples/init/main.cpp) | 五种初始化模式的最小可运行示例（一份代码 + 条件编译分派） |
| [examples/init/run.sh](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/examples/init/run.sh) | 把 `-mode`/`-pesize` 参数翻译成 `cmake -DRUN_MODE` 再编译、拉起进程的脚本 |
| [src/host/init/bootstrap/shmemi_bootstrap.cpp](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/bootstrap/shmemi_bootstrap.cpp) | 三种模式在库内部的总分发点（本讲只看到「选插件」这一层，深入建链细节留到 u2-l3） |
| [src/host/bootstrap/shmemi_bootstrap_mpi.cpp](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/bootstrap/shmemi_bootstrap_mpi.cpp) | MPI bootstrap 插件实现（barrier/allgather 直接转调 MPI） |
| [include/host_device/shmem_common_types.h](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host_device/shmem_common_types.h) | `data_op_engine_type_t` 引擎枚举（`option_attr` 与 `set_qp_num` 都会用到） |
| [include/host/team/shmem_host_team.h](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host/team/shmem_host_team.h) | `aclshmem_my_pe` / `aclshmem_n_pes` 声明（实践任务要用） |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **属性结构体**：`aclshmemx_init_attr_t` 与可选属性（对应 `shmem_host_def.h`）。
2. **三种 bootstrap 模式**：枚举含义、源码分发、各模式适用场景。
3. **uniqueid 的生成与广播**：UID 模式的标准四步范式。
4. **QP 数配置**：`aclshmemx_set_qp_num` 的引擎约束、校验顺序与生命周期。

---

### 4.1 模块一：初始化属性结构体 aclshmemx_init_attr_t

#### 4.1.1 概念说明

`aclshmemx_init_attr` 只有两个参数：一个模式标志、一个属性指针。所有「每个 PE 都可能不一样」的信息——我是谁（`my_pe`）、总共几个人（`n_pes`）、对称堆多大（`local_mem_size`）、去哪里找介绍人（`ip_port` / `comm_args`）——全部打包在 `aclshmemx_init_attr_t` 里一次性传入。

理解这个结构体的关键，是分清**必选属性**和**可选属性**两层：外层 `aclshmemx_init_attr_t` 是必选层；内层嵌了一个 `option_attr`（可选层），带默认值，多数场景不用动。

#### 4.1.2 核心流程

用户视角的填写流程：

```text
定义 aclshmemx_init_attr_t attributes          # 可零初始化（有默认成员初始化器）
├── 必填：my_pe / n_pes / local_mem_size
├── 按模式填：ip_port（Default）或 comm_args（UID/MPI）
├── 可选：option_attr（版本/引擎/超时，不改则用默认值）
└── 可选：instance_id（多实例，默认 0，本讲不展开，见 u8-l1）
调用 aclshmemx_init_attr(bootstrap_flags, &attributes)
```

#### 4.1.3 源码精读

**(1) 必选属性结构体**，定义在 `shmem_host_def.h`：

[include/host/shmem_host_def.h:L181-L195](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host/shmem_host_def.h#L181-L195) 逐字段含义如下（注释见 [L166-L180](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host/shmem_host_def.h#L166-L180)）：

| 字段 | 类型 | 含义与注意点 |
| --- | --- | --- |
| `my_pe` | `int` | 当前进程的 PE 编号。**必须由用户自己保证全局唯一**，SHMEM 不校验两个进程是否填了同一个 `my_pe` |
| `n_pes` | `int` | 全部 PE 总数，所有进程必须填同一个值 |
| `ip_port[64]` | `char[]` | Default 模式的通信服务端地址，格式 `tcp://IP:端口`（长度上限由 `ACLSHMEM_MAX_IP_PORT_LEN=64` 限制，见 [L70-L72](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host/shmem_host_def.h#L70-L72)）。注意端口必须大于 1024（后面源码会验证） |
| `local_mem_size` | `uint64_t` | 对称堆可分配容量（字节）。u2-l4 将展开讲它如何变成堆 |
| `option_attr` | 结构体 | 可选属性，见下表；默认值直接写在成员初始化器里 |
| `comm_args` | `void*` | bootstrap 阶段的通信参数，**不同模式内容不同**：UID 模式是 uid 状态指针、MPI 模式可以是 `nullptr` 或 `MPI_Comm*`。这就是它设计成 `void*` 的原因 |
| `instance_id` | `uint64_t` | 多实例场景的实例编号，默认 0 |

**(2) 可选属性结构体**：

[include/host/shmem_host_def.h:L155-L162](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host/shmem_host_def.h#L155-L162) 定义了 `aclshmem_init_optional_attr_t`：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `version` | `int` | 版本号，按 2.4 节公式编码 |
| `data_op_engine_type` | `data_op_engine_type_t` | 启用哪些通信引擎（按位组合） |
| `shm_init_timeout` | `uint32_t` | 初始化超时（秒） |
| `shm_create_timeout` | `uint32_t` | 共享内存堆创建超时（秒） |
| `control_operation_timeout` | `uint32_t` | 控制面操作超时（秒） |
| `sockFd` | `int32_t` | 预先申请好的 socket fd（用于 Default 模式提前占端口，避免竞态；0 表示不用） |

引擎枚举定义在 [include/host_device/shmem_common_types.h:L78-L84](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host_device/shmem_common_types.h#L78-L84)：MTE=0x01、SDMA=0x02、ROCE=0x04、UDMA=0x08，是位掩码，可用 `ACLSHMEM_DATA_OP_MTE | ACLSHMEM_DATA_OP_ROCE` 组合启用多引擎（各引擎的平台约束在 u1-l2 已讲：UDMA 仅 Ascend950、SDMA 仅 A3）。**注意区分两处引擎参数的用途**：`option_attr.data_op_engine_type` 决定「初始化时启用哪些引擎」，而 4.4 节的 `aclshmemx_set_qp_num(engine, ...)` 决定「某个引擎每个 peer 建几条 QP」——前者是开关，后者是并行度。

**(3) 默认值从哪来**：三个超时的默认值 `DEFAULT_TIMEOUT = 120`（秒）、uid 缓冲区长度 `ACLSHMEM_UNIQUE_ID_INNER_LEN = 124`、以及 QP 数上限 `ACLSHMEM_MAX_QP_NUM = 32`，都定义在 [include/host/shmem_host_def.h:L29-L34](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host/shmem_host_def.h#L29-L34)。u1-l4 里「缺一个 PE 其余进程阻塞 120 秒」的现象，根源就是这个默认超时；`ACLSHMEM_MAX_QP_NUM` 则是 4.4 节 QP 配置的值域上界。

**(4) 初始化 API 本尊**：

[include/host/init/shmem_host_init.h:L136-L147](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host/init/shmem_host_init.h#L136-L147) 声明了 `aclshmemx_init_attr`。注意注释里这句建议：属性可以自建，但**推荐用 `aclshmemx_set_attr_uniqueid_args()` 构造**，自建结构体填错字段会导致初始化失败。同文件还有几个本讲会用到的伙伴接口：

- [include/host/init/shmem_host_init.h:L91-L92](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host/init/shmem_host_init.h#L91-L92)：`aclshmemx_init_status()` 查询初始化状态（配合 [shmem_host_def.h:L118-L123](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host/shmem_host_def.h#L118-L123) 的四态枚举：未初始化 / 堆已建 / 已初始化 / 非法）。
- [include/host/init/shmem_host_init.h:L176-L196](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host/init/shmem_host_init.h#L176-L196)：`aclshmem_finalize()` 逆序释放当前实例。
- [include/host/init/shmem_host_init.h:L149-L174](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host/init/shmem_host_init.h#L149-L174)：`aclshmemx_init_attr_with_buffers()` 用户自带 buffer 建堆的变体，本讲只需知道存在，u8-l3 专门讲。
- [include/host/init/shmem_host_init.h:L120-L134](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host/init/shmem_host_init.h#L120-L134)：`aclshmemx_set_qp_num()` QP 数配置接口，4.4 节专门讲。

#### 4.1.4 代码实践：手工填一份属性并逐字段核对

1. **实践目标**：不看讲义，能凭理解写出必选属性的每一个字段。
2. **操作步骤**：
   - 打开 [examples/init/main.cpp](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/examples/init/main.cpp) 的 `RUN_WITH_DEFAULT` 分支，阅读辅助函数 [examples/init/main.cpp:L313-L336](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/examples/init/main.cpp#L313-L336)——这是官方示例给出的「手工填属性」参考写法：拷贝 `ip_port`、填 `my_pe`/`n_pes`/`local_mem_size`、逐项赋 `option_attr`、把 uid 指针挂到 `comm_args`。
   - 对照上面的字段表，给每一行赋值写一句注释，说明「这个值将被库的哪一步使用」。
3. **需要观察的现象**：`option_attr` 的赋值用了 `{attr_version, ACLSHMEM_DATA_OP_MTE, DEFAULT_TIMEOUT, DEFAULT_TIMEOUT, DEFAULT_TIMEOUT}`，只给了 5 个初值——第 6 个字段 `sockFd` 被省略，聚合初始化会自动补 0。
4. **预期结果**：你能回答「为什么 `comm_args` 是 `void*`」以及「哪三个字段是 Default 模式独有的关键输入（`ip_port`）而 UID 模式不用填它」。
5. 本实践为源码阅读型，无需运行环境。

#### 4.1.5 小练习与答案

**练习 1**：`option_attr.version` 的推荐值 `(1 << 16) + sizeof(aclshmem_init_optional_attr_t)` 中，`sizeof` 部分起到什么作用？

**答案**：把结构体大小编进版本号。用户头文件与库版本不一致时，结构体大小往往也不同，库据此可以在校验时发现 ABI 不匹配，而不是读错字段的偏移。

**练习 2**：示例 `RUN_WITH_MPI` 分支（[main.cpp:L283-L285](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/examples/init/main.cpp#L283-L285)）里 `option_attr` 第一个字段填的是 `0`，与推荐编码不同。这对我们写代码有什么提示？

**答案**：示例代码并非处处使用推荐默认值；库对 `version=0` 的接受程度属于实现细节（待本地验证）。更稳妥的做法是遵循头文件注释的建议——用 `aclshmemx_set_attr_uniqueid_args` 构造属性，或至少采用结构体自带的默认成员初始化器，不要照抄示例里的 `0`。

---

### 4.2 模块二：三种 Bootstrap 模式与源码分发

#### 4.2.1 概念说明

三种模式回答同一个问题——**「谁替这群互不相识的进程互相介绍」**：

| 模式（枚举值） | 介绍人 | 关键输入 | 适用场景 |
| --- | --- | --- | --- |
| `ACLSHMEMX_INIT_WITH_DEFAULT`（`1<<0`） | SHMEM 自带的 Config Store 插件：PE 0 起一个 KV 服务，其他 PE 连上来 | `ip_port` 优先；无有效 `ip_port` 时回退用 `comm_args` 里的 uid | 脚本循环拉起进程、没有 MPI 的环境；也常与 UID 混用（示例 `uid_default`） |
| `ACLSHMEMX_INIT_WITH_MPI`（`1<<1`） | 用户进程中已初始化的 MPI 运行时 | `comm_args` 可为 `nullptr`（用 `MPI_COMM_WORLD`）或 `MPI_Comm*` | 已经用 `mpirun` 拉起、且愿意让 MPI 继续存在的作业 |
| `ACLSHMEMX_INIT_WITH_UNIQUEID`（`1<<3`） | Config Store 插件 + 一张「入场券」（uid） | `comm_args` 必须是**有效的** uid（内部校验其地址族） | 训练框架（头文件注释写作 PTA）自己有控制信道、只需 SHMEM 提供一个可广播的凭证 |

#### 4.2.2 核心流程

库内部分发发生在 `aclshmemi_bootstrap_init(flags, attr)`：

```text
aclshmemx_init_attr(flags, attr)
  └─ aclshmemi_bootstrap_init(flags, attr)
       ├─ flags & UNIQUEID?  → 插件 = aclshmem_bootstrap_config_store.so
       │                      校验 comm_args 是有效 uid（地址族为 AF_INET/AF_INET6），否则报错
       ├─ flags & MPI?       → 插件 = aclshmem_bootstrap_mpi.so
       │                      comm_args 可为空（默认 MPI_COMM_WORLD）
       ├─ flags & DEFAULT?   → 插件 = aclshmem_bootstrap_config_store.so
       │                      ip_port 合法 → 直接用属性里的 ip_port 建服务
       │                      否则 comm_args 有效 uid → 回退 UID 路径
       │                      两者皆无 → ACLSHMEM_INVALID_PARAM
       └─ 其他              → Unknown Type，报错
  之后 dlopen 插件 → dlsym 取 "aclshmemi_bootstrap_plugin_init" → 调用插件完成建链
```

注意判断顺序是 `UNIQUEID → MPI → DEFAULT` 的 `if / else if` 链，**一个 flags 只应设置一位**。

#### 4.2.3 源码精读

**(1) 模式枚举**：

[include/host/shmem_host_def.h:L106-L112](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host/shmem_host_def.h#L106-L112) 定义了 bootstrap 枚举。两个值得注意的细节：

- `DEFAULT` 的注释写明优先级：`Priority is ipport > uid (comm_args)`——`ip_port` 优先，uid 是备胎。
- 位掩码序列是 `1<<0`、`1<<1`、`1<<3`——`1<<2` 缺位（历史保留，原因待确认）。

**(2) 分发实现**：

[shmemi_bootstrap.cpp:L262-L312](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/bootstrap/shmemi_bootstrap.cpp#L262-L312) 是三种模式的总分发。逐段看：

- [L270-L279](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/bootstrap/shmemi_bootstrap.cpp#L270-L279)（UNIQUEID 分支）：把 `attr->my_pe/n_pes` 存入全局引导句柄，然后调用 `is_uid_args_valid`（实现在 [L184-L193](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/bootstrap/shmemi_bootstrap.cpp#L184-L193)）——它把 `comm_args` 强转成内部 uid 状态并检查地址族是不是 `AF_INET`/`AF_INET6`，不合法直接报 `ACLSHMEM_INVALID_PARAM`。所以「UID 模式忘了广播 uid、拿全零结构体去初始化」会在这里被拦下。
- [L280-L283](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/bootstrap/shmemi_bootstrap.cpp#L280-L283)（MPI 分支）：只选插件名并透传 `comm_args`，不做校验——合法性由 MPI 自己保证。
- [L284-L308](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/bootstrap/shmemi_bootstrap.cpp#L284-L308)（DEFAULT 分支）：先用 `is_valid_ip_port_url`（实现在 [L195-L260](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/bootstrap/shmemi_bootstrap.cpp#L195-L260)）校验 `attr->ip_port`——要求 `tcp://` 或 `tcp6://` 前缀、端口在 \( (1024, 65535] \) 区间、IP 是合法 IPv4/IPv6 或主机名；合法就带上 `sockFd` 与超时直接用；不合法再看 `comm_args` 是否为有效 uid 作回退；都无效才报错。

**(3) 插件名与加载**：

[shmemi_bootstrap.cpp:L23-L26](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/bootstrap/shmemi_bootstrap.cpp#L23-L26) 用宏写死了两个插件名（`aclshmem_bootstrap_mpi.so`、`aclshmem_bootstrap_config_store.so`）和插件入口符号名；[L327-L333](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/bootstrap/shmemi_bootstrap.cpp#L327-L333) 负责实际 `dlopen`。报错信息也提醒了部署约束：**插件 so 必须和 `aclshmem.so` 在同一目录**。

**(4) MPI 插件长什么样**：

[shmemi_bootstrap_mpi.cpp:L89-L95](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/bootstrap/shmemi_bootstrap_mpi.cpp#L89-L95) 是插件入口：`mpi_comm` 为 `NULL` 就用 `MPI_COMM_WORLD`，否则解引用取用户传入的 `MPI_Comm`。控制面的三个原语全是直译：[L25-L32](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/bootstrap/shmemi_bootstrap_mpi.cpp#L25-L32) barrier→`MPI_Barrier`、[L34-L42](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/bootstrap/shmemi_bootstrap_mpi.cpp#L34-L42) allgather→`MPI_Allgather`、[L44-L52](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/bootstrap/shmemi_bootstrap_mpi.cpp#L44-L52) alltoall→`MPI_Alltoall`。也就是说 **MPI 模式下 SHMEM 的初始化控制面就是借用 MPI 集合通信**，不再自建 TCP 服务。

**(5) run.sh 如何拉起不同模式**：

- [run.sh:L106-L130](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/examples/init/run.sh#L106-L130) 把 `-mode` 名字映射成 `MODE_ID`（default=1、mpi=2、uid=3、uid_multi=4、uid_default=5、uid_multi_stress=6）。
- [run.sh:L152-L154](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/examples/init/run.sh#L152-L154) 把 `MODE_ID` 作为 `cmake -DRUN_MODE` 传入，编译期决定 `main.cpp` 走哪个 `#ifdef` 分支（**换模式必须重编译**，u1-l4 的结论在源码上的落点）。
- [run.sh:L176-L189](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/examples/init/run.sh#L176-L189)：mpi/uid 系模式统一用 `mpirun` 拉起（有 hostfile 则跨机）。
- [run.sh:L190-L209](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/examples/init/run.sh#L190-L209)：default 模式由脚本 for 循环直接启动进程，PE 编号在示例内由 `f_pe + device_id` 算出（[main.cpp:L344-L349](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/examples/init/main.cpp#L344-L349)）——「谁来分配 PE 编号」三种模式各不相同，正是 4.2.1 表格的直观体现。

**(6) 示例代码里的 Default 分支调用点**：

[examples/init/main.cpp:L352-L362](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/examples/init/main.cpp#L352-L362)：先准备一个全零 uid（`ACLSHMEM_UNIQUEID_INITIALIZER`，其内部字节全 0，地址族为 0，通不过 `is_uid_args_valid`），再用 `test_set_attr` 填好 `ip_port`，最后 `aclshmemx_init_attr(ACLSHMEMX_INIT_WITH_DEFAULT, &attributes)`——于是走的是「`ip_port` 合法」那条路。而 [main.cpp:L78-L131](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/examples/init/main.cpp#L78-L131) 的 `uid_default` 分支填了有效 uid 但 `ip_port` 留空、仍传 `DEFAULT` 标志——正好走「回退 uid」那条路。两个分支合起来把 DEFAULT 模式的两条路径都演示了。

#### 4.2.4 代码实践：用 run.sh 切换模式并观察拉起方式差异

1. **实践目标**：直观感受「同一份 `main.cpp`，模式不同、拉起方式不同」。
2. **操作步骤**：
   - 在有 CANN 环境的机器上（构建方法见 u1-l2），进入 `examples/init`；
   - 执行 `bash run.sh -mode mpi -pesize 4`；再执行 `bash run.sh -mode default -pesize 4`；
   - 阅读脚本输出中 `=== Launch executable ===` 之后的部分。
3. **需要观察的现象**：mpi 模式输出 `run mpirun ...`，由 `mpirun -np 4` 拉起 4 个进程；default 模式输出多行 `Starting process 0/4...`，由脚本后台循环启动。
4. **预期结果**：两种模式最终都打印 4 个 `pe X: shmem init SUCCESS`。若无 NPU 环境，可用 `bash run.sh -mode mpi -build` 只验证编译（待本地验证）。
5. 注意：uid 系模式还需要 `SHMEM_UID_SESSION_ID` 等环境变量（[run.sh:L135-L144](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/examples/init/run.sh#L135-L144)），缺了会话 ID 时 UID 模式可能无法完成建链（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ACLSHMEMX_INIT_WITH_MPI` 模式不需要 `ip_port`，也不需要 uid？

**答案**：MPI 模式下，进程组已经由 `mpirun` + `MPI_Init` 组织完毕，SHMEM 的 bootstrap 控制面（barrier/allgather 等）直接转调 `MPI_Barrier`/`MPI_Allgather` 完成，不需要再自建 TCP KV 服务，因此既没有服务端地址（`ip_port`）也没有入场券（uid）。

**练习 2**：用户误把空字符串 `ip_port` 和全零 uid 一起传给 DEFAULT 模式，会发生什么？

**答案**：`is_valid_ip_port_url` 对空串返回 false，回退检查 `is_uid_args_valid`——全零 uid 的地址族是 0（`AF_UNSPEC`），也不是 `AF_INET/AF_INET6`，于是走进 [shmemi_bootstrap.cpp:L303-L308](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/bootstrap/shmemi_bootstrap.cpp#L303-L308) 的报错分支，返回 `ACLSHMEM_INVALID_PARAM`，初始化失败。

**练习 3**：`aclshmemi_bootstrap_pre_init`（[shmemi_bootstrap.cpp:L121-L157](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/bootstrap/shmemi_bootstrap.cpp#L121-L157)）对 MPI 模式直接返回错误，这说明什么？

**答案**：预初始化路径只服务于 Config Store 系插件（UNIQUEID/DEFAULT 都加载 `aclshmem_bootstrap_config_store.so`）；MPI 插件没有对应的 pre_init 语义（它依赖用户进程里已就绪的 MPI 环境，无需预加载）。也侧面印证：MPI 模式与 Config Store 系模式的生命周期管理是两套逻辑。

---

### 4.3 模块三：UniqueID 的生成与广播

#### 4.3.1 概念说明

uniqueid（uid）可以理解为一张**会议入场券**：里面除了版本、`my_pe`、`n_pes`，还有 124 字节的内部信息（服务端地址、会话魔数等，对用户透明）。典型的生产者是训练框架——头文件注释里写「This function need run with PTA」（PTA 即 PyTorch 训练代理场景）：框架进程在 PE 0 上生成一张券，通过**框架自己的信道**（示例里借用 MPI_Bcast）分发给所有 PE，各 PE 拿着同一张券完成初始化。

它与 DEFAULT 模式的区别在于：DEFAULT 模式里 SHMEM 自己起服务、用户给地址；UID 模式里凭证由 SHMEM 签发、分发由外部信道负责。

#### 4.3.2 核心流程

UID 模式的标准四步（对应示例代码的固定套路）：

```text
① 仅 PE 0：status = aclshmemx_get_uniqueid(&uid)        # 生成入场券
② 所有 PE：外部信道广播 uid（示例用 MPI_Bcast，框架可用任意方式）
③ 所有 PE：aclshmemx_set_attr_uniqueid_args(my_pe, n_pes,
                local_mem_size, &uid, &attributes)        # 券 → 属性
④ 所有 PE：aclshmemx_init_attr(ACLSHMEMX_INIT_WITH_UNIQUEID,
                &attributes)                              # 凭券入场
```

#### 4.3.3 源码精读

**(1) uid 结构体**：

[include/host/shmem_host_def.h:L207-L212](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host/shmem_host_def.h#L207-L212) 定义 `aclshmemx_uniqueid_t`：`version` + `my_pe` + `n_pes` + `internal[124]`（124 来自 [L30](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host/shmem_host_def.h#L30) 的常量）。配套的版本常量在 [L233](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host/shmem_host_def.h#L233)，初始化宏 `ACLSHMEM_UNIQUEID_INITIALIZER` 在 [L242-L245](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host/shmem_host_def.h#L242-L245)——先把 uid 清成「已知状态」再等 PE 0 填充，是避免脏内存的正确姿势。

**(2) 两个 API 的契约**：

- [include/host/init/shmem_host_init.h:L94-L101](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host/init/shmem_host_init.h#L94-L101)：`aclshmemx_get_uniqueid(uid)` 生成 uid。注释明确「需要与 PTA 一起运行」——即它只管签发，不管分发。
- [include/host/init/shmem_host_init.h:L103-L118](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host/init/shmem_host_init.h#L103-L118)：`aclshmemx_set_attr_uniqueid_args(my_pe, n_pes, local_mem_size, uid, aclshmem_attr)` 把 uid 与身份信息合入属性结构体，是头文件推荐的标准属性构造方式。

**(3) set_attr_uniqueid_args 的实现**（为什么 `comm_args` 能直接被 bootstrap 校验）：

[src/host/init/shmem_init.cpp:L598-L613](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L598-L613) 里，该函数先做参数范围断言（`local_mem_size <= ACLSHMEM_MAX_LOCAL_SIZE`、`n_pes <= ACLSHMEM_MAX_PES`、`my_pe < ACLSHMEM_MAX_PES`），然后**把 `aclshmemx_uniqueid_t*` 直接强转为内部 `aclshmemi_bootstrap_uid_state_t*` 存进 `comm_args`**，再填 `my_pe/n_pes/local_mem_size`。也就是说：`get_uniqueid` 生成的 124 字节 `internal` 区域，实际承载着 bootstrap 需要的连接状态（含合法的 sockaddr，这正是 `is_uid_args_valid` 检查地址族的依据）；`internal` 对用户不透明，但库内外对同一段内存有两种视角——这是理解「uid 即凭证」的钥匙。

**(4) 四步范式的完整示例**：

[examples/init/main.cpp:L23-L53](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/examples/init/main.cpp#L23-L53)（`RUN_WITH_UNIQUEID` 分支）把四步完整走了一遍：

- [L29-L34](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/examples/init/main.cpp#L29-L34)：`MPI_Init` 后用 `MPI_Comm_rank/size` 拿 `pe/pe_size`——UID 模式下 PE 编号同样可以由外部运行时分配；
- [L37-L39](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/examples/init/main.cpp#L37-L39)：`aclInit` + `device_id = pe % g_npu` 轮流占用 NPU；
- [L41-L47](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/examples/init/main.cpp#L41-L47)：定义属性与 `ACLSHMEM_UNIQUEID_INITIALIZER` 初始化的 uid，**只有 `pe == 0` 调 `aclshmemx_get_uniqueid`**；
- [L49](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/examples/init/main.cpp#L49)：`MPI_Bcast(&uid, sizeof(aclshmemx_uniqueid_t), MPI_UINT8_T, 0, MPI_COMM_WORLD)` 把整张券按字节广播出去；
- [L50-L53](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/examples/init/main.cpp#L50-L53)：每个 PE 各自 `set_attr_uniqueid_args` + `init_attr(ACLSHMEMX_INIT_WITH_UNIQUEID)`。

顺带一提 [shmemi_bootstrap.cpp:L319-L324](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/bootstrap/shmemi_bootstrap.cpp#L319-L324)：UID 模式还会从 uid 魔数派生一个 16 位 `session_magic`，用于隔离同一批进程里连续多轮 init/finalize 的连接——这是 u8-l1 多实例与压力测试（`uid_multi_stress` 模式）能正确工作的基础之一。

#### 4.3.4 代码实践：编写 MPI 模式 4 PE 最小程序（本讲核心实践·上）

1. **实践目标**：参照 `RUN_WITH_MPI` 分支，独立编写（或改写示例为）一个 MPI 模式初始化 4 个 PE 的最小程序，并验证各 PE 打印的 `my_pe` 互不相同。（本实践将在 4.4.4 继续扩展 QP 配置部分。）
2. **操作步骤**：
   - 进入 `examples/init`，把 [main.cpp:L265-L310](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/examples/init/main.cpp#L265-L310) 的 `RUN_WITH_MPI` 分支复制为 `mpi_my_pe_demo.cpp`，在 `shmem init SUCCESS` 打印之前插入身份查询（声明见 [include/host/team/shmem_host_team.h:L80-L89](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host/team/shmem_host_team.h#L80-L89)）：

     ```cpp
     // 示例代码：在初始化成功后查询身份
     std::cout << "pe " << pe << ": aclshmem_my_pe() = " << aclshmem_my_pe()
               << ", aclshmem_n_pes() = " << aclshmem_n_pes() << std::endl;
     ```

   - 参考 `CMakeLists.txt` 中现有目标的写法为它加一个编译目标（同样定义 `RUN_WITH_MPI` 宏、链接 shmem 与 MPI），或最简方式：直接修改 `main.cpp` 该分支后用 `bash run.sh -mode mpi -pesize 4` 重新编译运行（run.sh 每次会重编，见 [run.sh:L146-L166](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/examples/init/run.sh#L146-L166)）。
   - 运行 `bash run.sh -mode mpi -pesize 4`（或 `mpirun -np 4 ./build/bin/init_examples 4`）。
3. **需要观察的现象**：4 个进程各打印一行，`aclshmem_my_pe()` 的取值是 0/1/2/3 的某个排列，且与该进程 `MPI_Comm_rank` 得到的 `pe` 一致；每个进程的 `aclshmem_n_pes()` 都是 4。
4. **预期结果**：4 行输出覆盖 0~3 全部四个编号、无重复；随后各自打印 `shmem init SUCCESS` 与 `demo run success`。`my_pe` 与 `n_pes` 只能在 `aclshmemx_init_attr` 成功之后调用（u1-l4 结论）。若无 NPU 环境，编译通过即为完成代码部分，运行结果待本地验证。
5. **思考题（选做）**：把插入的查询语句挪到 `aclshmemx_init_attr` 之前会发生什么？结合 `ACLSHMEM_NOT_INITED` 错误码（[shmem_host_def.h:L86-L100](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host/shmem_host_def.h#L86-L100)）记下实际现象（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `aclshmemx_get_uniqueid` 只在 PE 0 调用？如果每个 PE 都自己生成一张券会怎样？

**答案**：所有 PE 必须持**同一张**券才能在同一个 Config Store 会话里会合；券内含服务端地址与会话信息。若各 PE 各自生成，各自拿到的地址/会话不一致，bootstrap 校验或建链阶段就会失败或互相找不到（具体失败点可在 u2-l3 建链时序中确认）。

**练习 2**：广播 uid 时为什么按 `sizeof(aclshmemx_uniqueid_t)` 整体按字节传（`MPI_UINT8_T`），而不是逐字段传？

**答案**：`internal[124]` 对用户不透明，用户不应解析或拆分它；整结构体按字节广播保证券的内容原样到达，也不会因未来版本扩展内部字段而破坏用户代码。

**练习 3**：`set_attr_uniqueid_args` 做了哪三类范围检查？分别对应头文件注释里的哪句话？

**答案**：`local_mem_size <= ACLSHMEM_MAX_LOCAL_SIZE`、`n_pes <= ACLSHMEM_MAX_PES`、`my_pe < ACLSHMEM_MAX_PES`，见 [shmem_init.cpp:L602-L604](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L602-L604)。对应 [shmem_host_init.h:L106-L110](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host/init/shmem_host_init.h#L106-L110) 注释中「my_pe 必须小于最大 PE 数、n_pes 不得超过最大 PE 数、local_mem_size 必须小于最大本地内存」三句。

---

### 4.4 模块四：aclshmemx_set_qp_num 与 QP 数配置（本轮新增能力）

#### 4.4.1 概念说明

`aclshmemx_set_qp_num(engine, qp_num)` 是一个**进程级、必须在初始化之前调用**的配置接口：它告诉库「engine 这个引擎为每个 peer 建几条 QP」。

三个关键认知：

1. **引擎约束**：本版本支持 `ACLSHMEM_DATA_OP_ROCE` 与 `ACLSHMEM_DATA_OP_UDMA` 两个引擎（旧版本只支持 UDMA，ROCE 是本轮 Ascend950 多 QP 改造新加的）。传其他引擎（如 MTE）返回 `ACLSHMEM_NOT_SUPPORTED`。
2. **生命周期约束**：只能在**没有任何 SHMEM 实例存活**时配置。一旦初始化成功，配置即被**冻结**（再调用返回 `ACLSHMEM_NOT_SUPPORTED`）；最后一个实例 finalize 之后，ROCE 和 UDMA 的 QP 数都被**复位为 1**，可以重新配置。
3. **一致性约束**：取值必须在 \[1, `ACLSHMEM_MAX_QP_NUM`=32\]，且**所有 PE 必须配置相同的值**——QP 数影响各 PE 之间交换的连接元数据布局，不一致会产生不兼容的元数据（建链/校验阶段出问题，见 u5-l3 / u5-l7）。

它与 `option_attr.data_op_engine_type` 是正交的两件事：后者决定「初始化启用哪些引擎」（开关），前者决定「某引擎每个 peer 建几条 QP」（并行度）。

#### 4.4.2 核心流程

**配置的生命周期状态机**：

```text
┌─────────────────────────────────────────────┐
│ 进程启动（无实例存活）                        │
│ g_udma_qp_config.qpNum = 1（默认）            │
│ g_rdma_qp_config.qpNum = 1（默认）            │
└──────────────────┬──────────────────────────┘
                   │ aclshmemx_set_qp_num(ROCE/UDMA, n) ← 唯一可配置窗口
                   ▼
        aclshmemx_init_attr 成功 → g_qp_config_frozen = true
                   │
┌──────────────────▼──────────────────────────┐
│ 实例存活期：配置冻结                          │
│ 再调 set_qp_num → ACLSHMEM_NOT_SUPPORTED     │
└──────────────────┬──────────────────────────┘
                   │ 最后一个实例 finalize（多实例下须全部退出）
                   ▼
┌─────────────────────────────────────────────┐
│ 两个 QP 配置复位为 1、冻结解除，可再次配置     │
└─────────────────────────────────────────────┘
```

**函数内部的校验顺序**（新版实现，顺序有意为之）：

```text
加锁（与 init/finalize 同一把互斥量，保证串行）
 ① qp_num 不在 [1, 32]        → ACLSHMEM_INVALID_VALUE（-2）
 ② 实例存活（配置已冻结）      → ACLSHMEM_NOT_SUPPORTED（-100）
 ③ engine == ROCE             → 写 g_rdma_qp_config.qpNum，返回 SUCCESS
   engine == UDMA             → 写 g_udma_qp_config.qpNum，返回 SUCCESS
   其他引擎                    → ACLSHMEM_NOT_SUPPORTED（-100）
```

注意：**值域检查排在引擎检查之前**——哪怕传了不支持的引擎，只要 `qp_num=0` 也先报 `INVALID_VALUE`。旧版实现（仅 UDMA 时代）是先拒绝非 UDMA 引擎再查值域，本轮改造把顺序对调了，用返回码区分「数值非法」与「引擎不支持」更清晰。

**配置的传递链路**（本讲看到「装车」为止，「发运」在 u5 讲展开）：

```text
aclshmemx_set_qp_num(engine, n)
  └─ 进程级全局：g_rdma_qp_config / g_udma_qp_config（shmem_init.cpp:L103-L104）
       └─ aclshmemi_init_attr_impl → bind_aclshmem_entity(..., g_udma_qp_config,
                                       g_rdma_qp_config.qpNum)（shmem_init.cpp:L1003-L1005）
            └─ entity_member.rdma_qp_num / udma_qp_num（shmemi_init_backend.h:L49-L52）
                 └─ create_entity → transport_options.rdmaQpConfig.qpNum /
                                     udmaQpConfig.qpNum（shmem_init_backend.cpp:L258-L261）
                      └─ 传输管理器按 qpNum 为每个 peer 建多条 QP
                         （u5-l3 / u5-l7 展开建链与 QP 数一致性校验）
```

#### 4.4.3 源码精读

**(1) API 声明与契约**：

[include/host/init/shmem_host_init.h:L120-L134](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host/init/shmem_host_init.h#L120-L134) 声明了 `aclshmemx_set_qp_num(engine, qp_num)`。`@note` 注释写明了全部四条契约：本版本支持 ROCE 与 UDMA（[L123](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host/init/shmem_host_init.h#L123)）；须在无实例初始化时调用、存活期冻结、最后一个实例 finalize 后 ROCE 与 UDMA 的 QP 数都复位为 1（[L124-L126](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host/init/shmem_host_init.h#L124-L126)）；值域 \[1, `ACLSHMEM_MAX_QP_NUM`\] 且所有 PE 必须一致，不一致会产生不兼容的元数据布局（[L126-L127](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host/init/shmem_host_init.h#L126-L127)）；进程内线程安全、且与初始化/终结串行化（[L127-L128](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host/init/shmem_host_init.h#L127-L128)）。

**(2) 值域上界常量**：

[include/host/shmem_host_def.h:L33-L34](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host/shmem_host_def.h#L33-L34) 定义 `ACLSHMEM_MAX_QP_NUM = 32`——每个 peer 连接的 QP 数上限。

**(3) 函数实现与校验顺序**：

[src/host/init/shmem_init.cpp:L615-L637](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L615-L637) 是 `aclshmemx_set_qp_num` 的完整实现：

- [L617](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L617) 先拿 `g_aclshmem_ctx_mutex`——与实例上下文同一把锁，这就是「与初始化/终结串行化」的实现方式；
- [L618-L621](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L618-L621) 值域检查，辅助函数 `is_valid_rdma_qp_num` 定义在 [L176](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L176)：`qp_num >= 1 && qp_num <= ACLSHMEM_MAX_QP_NUM`。注意这个函数**名字里带 rdma，实际是通用的值域检查**（UDMA 也复用它），命名是改造时的历史痕迹，读源码时不要被误导；
- [L622-L625](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L622-L625) 冻结检查：`g_qp_config_frozen` 为真即返回 `ACLSHMEM_NOT_SUPPORTED`；
- [L627-L634](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L627-L634) 按引擎分发：ROCE 写 `g_rdma_qp_config.qpNum`，UDMA 写 `g_udma_qp_config.qpNum`，其他引擎返回 `ACLSHMEM_NOT_SUPPORTED`。

**(4) 两个进程级配置全局量与冻结标志**：

[src/host/init/shmem_init.cpp:L103-L105](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L103-L105) 定义了 `g_udma_qp_config`、`g_rdma_qp_config`（`TransportOptions::RdmaQpConfig` 类型，本轮新增）与 `g_qp_config_frozen`。它们是 `static` 文件级变量——所以 QP 配置是**进程级**而非实例级的：多实例共享同一份配置（这正是「任一实例存活期间都冻结」的原因，多实例见 u8-l1）。

**(5) 冻结发生在初始化成功的收尾处**：

[src/host/init/shmem_init.cpp:L1032-L1040](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L1032-L1040)：`aclshmemi_init_attr_impl` 在 `is_aclshmem_initialized = true`、init 成功标记置位之后才执行 `g_qp_config_frozen = true`（[L1039](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L1039)）。也就是说**初始化失败的路径不会冻结配置**，用户修好参数后无需 finalize 即可重新 `set_qp_num` 再试。

**(6) 配置装进实体并交给传输层**：

- [src/host/init/shmem_init.cpp:L1003-L1005](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L1003-L1005)：初始化主流程调用 `bind_aclshmem_entity` 时，把 `g_udma_qp_config`（整个结构体）和 `g_rdma_qp_config.qpNum` 一并传入——这是 QP 配置离开全局量、进入实体管理的交接点。
- [src/host/init/backends/shmemi_init_backend.h:L49-L52](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/backends/shmemi_init_backend.h#L49-L52)：`entity_member` 用 `udma_qp_num{1}` / `rdma_qp_num{1}` 暂存两份 QP 数（默认值都是 1）。
- [src/host/init/backends/shmem_init_backend.cpp:L258-L261](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/backends/shmem_init_backend.cpp#L258-L261)：`create_entity` 把它们装进 `transport_options.udmaQpConfig.qpNum` 与 `transport_options.rdmaQpConfig.qpNum`，随传输选项交给传输层。传输管理器如何按 qpNum 为每个 peer 建多条 QP、以及各 PE QP 数不一致时的 `CheckQpNumConsistency` 校验，属于 u5-l3 / u5-l7 的内容。

**(7) 复位发生在最后一个实例 finalize 时**：

[src/host/init/shmem_init.cpp:L1153-L1156](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L1153-L1156)：`aclshmemi_finalize_impl` 在 `is_last_instance`（实例计数归零，见 [L1140](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L1140)）为真时，把 `g_rdma_qp_config` 整体重置为默认构造（qpNum 回到 1）、`g_udma_qp_config` 同样复位、`g_qp_config_frozen` 置 false——「finalize 后可重新配置」的承诺就落在这三行。多实例下只要还有一个实例存活，这段就不会执行，配置继续保持冻结。

#### 4.4.4 代码实践：配置 QP 数并观察返回值（本讲核心实践·下）

1. **实践目标**：在 4.3.4 的 MPI 模式最小程序基础上，体验 `aclshmemx_set_qp_num` 的调用时机与返回值规律。
2. **操作步骤**：
   - 在 `aclshmemx_init_attr` **之前**插入（示例代码）：

     ```cpp
     // 示例代码：初始化前配置 QP 数（需包含 host/init/shmem_host_init.h）
     int rc_roce = aclshmemx_set_qp_num(ACLSHMEM_DATA_OP_ROCE, 4);  // 预期 0
     int rc_udma = aclshmemx_set_qp_num(ACLSHMEM_DATA_OP_UDMA, 4);  // 预期 0
     int rc_bad_value = aclshmemx_set_qp_num(ACLSHMEM_DATA_OP_ROCE, 0);   // 预期 -2
     int rc_over_max = aclshmemx_set_qp_num(ACLSHMEM_DATA_OP_ROCE, 33);   // 预期 -2
     int rc_bad_engine = aclshmemx_set_qp_num(ACLSHMEM_DATA_OP_MTE, 4);   // 预期 -100
     std::cout << "pe " << pe << ": roce=" << rc_roce << ", udma=" << rc_udma
               << ", zero=" << rc_bad_value << ", over_max=" << rc_over_max
               << ", mte=" << rc_bad_engine << std::endl;
     ```

   - 在 `aclshmemx_init_attr` **成功之后**再补一次调用（示例代码）：

     ```cpp
     // 示例代码：实例存活期再配置，预期被冻结拒绝
     int rc_frozen = aclshmemx_set_qp_num(ACLSHMEM_DATA_OP_ROCE, 2);  // 预期 -100
     ```

   - 用 `bash run.sh -mode mpi -pesize 4` 重新编译运行（注意 `ACLSHMEM_DATA_OP_*` 枚举来自 `shmem_common_types.h`，经 `shmem_host_init.h` 间接包含）。
3. **需要观察的现象**：每个 PE 的第一行输出中，前两个调用返回 `0`（`ACLSHMEM_SUCCESS`），`zero`/`over_max` 返回 `-2`（`ACLSHMEM_INVALID_VALUE`），`mte` 返回 `-100`（`ACLSHMEM_NOT_SUPPORTED`）；初始化成功后那次调用返回 `-100`。错误码数值对照 [shmem_host_def.h:L86-L100](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host/shmem_host_def.h#L86-L100)。
4. **预期结果**：

   | 调用 | 时机 | 预期返回 |
   | --- | --- | --- |
   | `set_qp_num(ROCE, 4)` | init 前 | `0`（SUCCESS） |
   | `set_qp_num(UDMA, 4)` | init 前 | `0`（SUCCESS） |
   | `set_qp_num(ROCE, 0)` / `(ROCE, 33)` | init 前 | `-2`（INVALID_VALUE） |
   | `set_qp_num(MTE, 4)` | init 前 | `-100`（NOT_SUPPORTED） |
   | `set_qp_num(ROCE, 2)` | init 成功后 | `-100`（NOT_SUPPORTED，已冻结） |

   两点说明：(a) 配置成功不代表该引擎会被使用——是否创建 ROCE/UDMA 传输管理器由 `option_attr.data_op_engine_type` 与平台支持共同决定，`set_qp_num` 只负责记录配置并随实体传递（消费端行为见 u5-l1 / u5-l7）；(b) 示例默认 `option_attr` 只启用 MTE，此时 QP 配置「记录了但没被消费」，返回值仍为 0，属正常现象（待本地验证）。若无 NPU 环境，编译通过即为完成代码部分，运行结果待本地验证。
5. **思考题（选做）**：把 `set_qp_num(ROCE, 4)` 只写在 PE 0、其他 PE 不写（或写成不同值），初始化会发生什么？先记下你的推断，答案在 u5-l7 的 QP 数一致性校验中验证。

#### 4.4.5 小练习与答案

**练习 1**：`is_valid_rdma_qp_num` 这个名字带 `rdma` 的函数，为什么 UDMA 也能用它校验？

**答案**：看函数体（[shmem_init.cpp:L176](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L176)）：它只做 `qp_num >= 1 && qp_num <= ACLSHMEM_MAX_QP_NUM` 的通用值域检查，与引擎无关。命名是本轮 ROCE 多 QP 改造引入时的历史痕迹，实际语义是「QP 数合法性检查」，ROCE 与 UDMA 共用。

**练习 2**：为什么头文件强调「QP 数必须在所有 PE 上一致」？

**答案**：QP 数决定了每个 peer 的连接资源数量与交换的元数据布局（头文件 note 原文：inconsistent values produce incompatible metadata layouts，[shmem_host_init.h:L126-L127](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/include/host/init/shmem_host_init.h#L126-L127)）。各 PE 不一致时对端按不同布局解释元数据，建链或一致性校验阶段会失败——`set_qp_num` 本身不做跨 PE 校验（它只是本地记录），一致性靠后续建链阶段的校验兜底（u5-l3 / u5-l7）。

**练习 3**：进程里先 init 实例 A，再 finalize 实例 A，随后 `set_qp_num(UDMA, 8)` 会成功吗？如果是多实例场景（A 存活时又 init 了 B，先 finalize A 呢）？

**答案**：单实例场景会成功——A finalize 后是最后一个实例，配置复位为 1、冻结解除（[shmem_init.cpp:L1153-L1156](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L1153-L1156)）。多实例场景：finalize A 后实例 B 仍存活，`is_last_instance` 为假，不执行复位且 `g_qp_config_frozen` 仍为真，`set_qp_num` 返回 `ACLSHMEM_NOT_SUPPORTED`——必须等**最后一个**实例退出。

## 5. 综合实践

把四个模块串起来，做一张**「模式 × 属性 × QP 配置」决策卡**，并用它指导一次真实的模式迁移：

1. **整理决策卡**（纸面作业）：画一张三行（DEFAULT / MPI / UNIQUEID）× 五列（谁分配 PE 编号 / 必填字段 / `comm_args` 内容 / 加载的插件 / 拉起方式）的表格，每格填代码依据（文件:行号）。参考答案行——MPI 行：`MPI_Comm_rank` 分配 PE 编号；必填 `my_pe/n_pes/local_mem_size`；`comm_args` 可为 `nullptr` 或 `MPI_Comm*`；插件 `aclshmem_bootstrap_mpi.so`；`mpirun` 拉起。
2. **动手迁移**：把 4.3.4 的 MPI 版程序改成 UID 版——按 4.3.2 四步范式替换初始化段（PE 0 生成券、`MPI_Bcast` 广播、`set_attr_uniqueid_args`、`init_attr(ACLSHMEMX_INIT_WITH_UNIQUEID)`），其余不动；保留 4.4.4 插入的 `set_qp_num` 调用。
3. **验证**：`bash run.sh -mode uid -pesize 4` 运行，确认 `aclshmem_my_pe()` 输出仍是 0~3 无重复，且 `set_qp_num` 各调用的返回值与 4.3.4（MPI 模式）完全一致——证明 **QP 配置与 bootstrap 模式正交**，换模式不影响 QP 配置行为。注意 uid 模式需要 `SHMEM_UID_SESSION_ID`（[run.sh:L135-L138](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/examples/init/run.sh#L135-L138) 会自动导出会话 ID）。运行结果待本地验证。
4. **进阶观察**：对照改前改后两次运行，注意 UID 模式下 `ip_port` 字段留空也能成功——用它反推 4.2 中分发逻辑的回退关系。

## 6. 本讲小结

- `aclshmemx_init_attr_t` 是必选属性（`my_pe/n_pes/ip_port/local_mem_size/comm_args/instance_id`）+ 可选属性 `option_attr`（版本/引擎位掩码/三个超时/sockFd）的两层结构，可选层有默认值（引擎默认 MTE、超时默认 120 秒）。
- 三种 bootstrap 模式本质是三种「介绍人」：DEFAULT 由 SHMEM 的 Config Store 插件自建服务（`ip_port` 优先、uid 回退）；MPI 模式直接把控制面集合通信转调 MPI；UNIQUEID 由 SHMEM 签发入场券、外部信道分发。
- 模式分发在 `aclshmemi_bootstrap_init` 的 `if/else if` 链中按 UNIQUEID→MPI→DEFAULT 顺序选插件（`aclshmem_bootstrap_config_store.so` / `aclshmem_bootstrap_mpi.so`），插件通过 dlopen/dlsym 加载，且必须与 `aclshmem.so` 同目录。
- UID 标准四步：PE 0 `get_uniqueid` → 外部信道广播 → 人人 `set_attr_uniqueid_args`（内部把 uid 指针强转后挂到 `comm_args`）→ `init_attr(UNIQUEID)`；uid 的 124 字节 `internal` 承载不透明的连接状态，必须整结构体原样传递。
- `aclshmemx_set_qp_num`（本轮起支持 ROCE 与 UDMA）是进程级配置：init 前调用、值域 \[1, 32\]、全 PE 必须一致；校验顺序为先值域（INVALID_VALUE）再冻结（NOT_SUPPORTED）后引擎分发；配置经 `bind_aclshmem_entity` → `entity_member` → `transport_options.rdmaQpConfig/udmaQpConfig` 传给传输层。
- QP 配置生命周期：初始化成功时冻结（失败不冻结）、最后一个实例 finalize 后复位为 1 并解除冻结；与 bootstrap 模式正交。
- `run.sh` 的 `-mode` 经 `cmake -DRUN_MODE` 决定条件编译分支，换模式必重编译；`-pesize` 在 mpi/uid 系模式交给 `mpirun -np`，在 default 模式由脚本循环拉起。
- `aclshmem_my_pe()/aclshmem_n_pes()` 只能在初始化成功后调用，是验证「PE 世界是否正确建立」的最直接手段。

## 7. 下一步学习建议

本讲只看到「模式被分发到哪个插件」和「QP 配置被记录并装车」为止。下一讲 **u2-l2 初始化全流程源码走读** 将进入 `src/host/init/shmem_init.cpp`，沿 `aclshmemx_init_attr` 的调用链走完 bootstrap 建链 → HYBM 建堆 → 子模块就绪三个阶段（包括 `bind_aclshmem_entity` 交接点与 `g_qp_config_frozen` 冻结点在主流程中的位置），并理解 `shmem_finalize` 的逆序释放；随后 **u2-l3 Bootstrap 控制面** 会拆开 `aclshmem_bootstrap_config_store.so`，讲清 PE 0 的 KV 服务与 TCP 星型拓扑。QP 配置的「发运」侧——`TransportOptions::RdmaQpConfig` 如何被传输管理器消费、按 qpNum 建链与 QP 数一致性校验——安排在 **u5-l1 传输层架构** 与 **u5-l7 Ascend950 RDMA 多 QP 机制**。建议先自行通读 [src/host/init/shmem_init.cpp:L598-L637](https://github.com/gitcode.com/cann/shmem/blob/c4f9363aff65dcde56b565cf4d9937483d872e55/src/host/init/shmem_init.cpp#L598-L637) 附近代码，带着「属性结构体的每个字段、QP 配置的每个值在哪里被消费」这个问题进入下一讲。
