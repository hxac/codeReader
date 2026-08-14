# 快速上手：跑通第一个 AllReduce 示例

## 1. 本讲目标

学完本讲，你应该能够：

1. 独立编译并运行 `examples/01_communicators` 下的 AllReduce 示例。
2. 掌握两种通信域初始化方式：基于 root info（`HcclGetRootInfo` + `HcclCommInitRootInfoConfig`）与基于 rank table（`HcclCommInitClusterInfoConfig`）。
3. 理解 `ACLCHECK` / `HCCLCHECK` 错误检查宏的写法，以及「下发到流 + 流同步」的异步执行语义。
4. 能动手修改示例（例如把归约类型从 `HCCL_REDUCE_SUM` 改为 `HCCL_REDUCE_MAX`）并预测输出。

本讲是整个手册第一次「让代码真正跑起来」的讲义。前面三讲我们建立了架构认知（u1-l1）、构建方式（u1-l2）和代码地图（u1-l3），本讲用官方示例把 HCCL/HCOMM 的对外接口「用一遍」，为后续进入 `src/` 内部实现建立体感。

## 2. 前置知识

### 2.1 什么是 AllReduce

AllReduce（全归约）是集合通信中最常用的算子之一：通信域内每个 rank 各持有一份输入，经过某种归约运算（求和、求最大值等）后，**每个 rank 都拿到相同的归约结果**。数学上，设通信域内有 \( n \) 个 rank，第 \( i \) 个 rank 的输入向量为 \( x_i \)，则 AllReduce 执行：

\[
y = \bigoplus_{i=0}^{n-1} x_i
\]

其中 \( \bigoplus \) 是逐元素的归约运算（如加法、取最大值），所有 rank 最终都得到 \( y \)。

示例中每个 rank 的输入是 `0, 1, 2, ..., 7`（共 8 个 float），用 `HCCL_REDUCE_SUM` 归约时，8 个 rank 的第 0 个元素相加是 \( 0+0+\cdots+0=0 \)，第 1 个元素是 \( 1+1+\cdots+1=8 \)，以此类推，最终每个 rank 输出 `[0 8 16 24 32 40 48 56]`。

### 2.2 通信域、rank 与 root info

- **通信域（communicator）**：一组 rank 的集合加上它们之间的连接资源。所有集合通信操作都必须在某个通信域内执行。
- **rank**：通信域内每个参与者的编号，从 0 开始。
- **root info**：由一个 rank（通常是 rank 0，称为 root）调用 `HcclGetRootInfo()` 生成的一段标识信息（本仓库中固定为 4108 字节，见 [include/hccl/hccl_types.h:L112-L124](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_types.h#L112-L124)），里面包含 Device IP、Device ID 等建链所需信息。它必须被分发给通信域内所有 rank，各 rank 才能据此互相发现并建立连接。

关键问题是：**root info 怎么广播出去？** HCCL/HCOMM 自己不管这件事，由用户的拉起机制负责——示例中用的是 MPI 的 `MPI_Bcast`。这也是 `rank_info_detect` 模块（u2-l5 会深入）在库内部完成的事：各 rank 拿到相同的 root info 后，库内部再通过 socket 等方式完成详细的信息交换。

### 2.3 ACL 运行时与「流」的异步语义

示例大量使用 `aclrt*` 接口（ACL 是昇腾计算语言运行时）。两个关键概念：

- **Device 内存与 Host 内存**：NPU 上的内存（`aclrtMalloc`）和主机 CPU 内存（`aclrtMallocHost`）是两块独立地址空间，需要 `aclrtMemcpy` 显式拷贝。
- **流（stream）**：ACL 中的任务队列。`HcclAllReduce` 只是把通信任务**下发**到流上就立刻返回，并不等待完成；必须调用 `aclrtSynchronizeStream(stream)` 阻塞等待，才能保证结果已写入 `recvBuf`。这是初学者最常见的坑——漏掉同步就去读结果，读到的是旧数据。

### 2.4 MPI 在示例中的角色

示例用 MPI 只做两件事：拉起 N 个进程（`mpirun -n 8`），以及广播 root info（`MPI_Bcast`）。AllReduce 本身完全由 HCCL 完成，MPI 不参与数据面。换句话说，MPI 是示例的「脚手架」，不是依赖项。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [examples/01_communicators/01_one_device_per_process/main.cc](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/examples/01_communicators/01_one_device_per_process/main.cc) | 主示例：每进程一个 NPU，root info 方式初始化通信域 + AllReduce |
| [examples/01_communicators/02_one_device_per_process_rank_table/main.cc](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/examples/01_communicators/02_one_device_per_process_rank_table/main.cc) | 对比示例：rank table 文件方式初始化通信域 |
| [examples/01_communicators/03_one_device_per_pthread/main.cc](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/examples/01_communicators/03_one_device_per_pthread/main.cc) | 对比示例：每线程一个 NPU，单进程多线程通信域 |
| [examples/README.md](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/examples/README.md) | 示例目录索引 |
| [examples/01_communicators/01_one_device_per_process/README.md](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/examples/01_communicators/01_one_device_per_process/README.md) | 环境准备、编译执行命令、预期输出 |
| [examples/01_communicators/01_one_device_per_process/Makefile](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/examples/01_communicators/01_one_device_per_process/Makefile) | 编译与 `make test` 运行入口 |
| [include/hccl/hccl_comm.h](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_comm.h) | 通信域初始化/销毁/查询接口声明（HCOMM 提供 weak symbol 实现） |
| [include/hccl/hccl_types.h](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_types.h) | `HcclRootInfo`、`HcclCommConfig`、数据类型/归约类型枚举定义 |

一个容易混淆的点：示例里 `#include <hccl/hccl.h>` 并调用 `HcclAllReduce`，但本仓库 `include/hccl/` 下并没有 `hccl.h` 这个文件——它来自 CANN 安装目录（`$ASCEND_HOME_PATH/include`）。本仓库（HCOMM）提供的是 `hccl_comm.h` 等头文件中声明的一批 **weak symbol 接口的实现**（u1-l3 已讲过弱符号机制），链接时 `-lhccl` 就把这些实现接了进来。可以在仓库中 grep 验证：`HcclAllReduce` 的声明不在本仓库 `include/` 中，只有 legacy 内部接口 `HcclAllReduceInner` 出现在 `pkg_inc/legacy/hccl/hccl_inner.h:22`。

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

1. 通信域初始化方式一：root info（示例 01 主线）
2. 通信域配置结构 HcclCommConfig 与 HcclCommConfigInit
3. AllReduce 执行与流同步：ACLCHECK/HCCLCHECK 与异步语义
4. 初始化方式对比：rank table（示例 02）与多线程（示例 03）

### 4.1 通信域初始化方式一：root info

#### 4.1.1 概念说明

「root info 方式」是跨进程拉起集群时最通用的初始化方法：指定一个 rank 作为 root，由它生成一份 4108 字节的标识信息，通过任意外部渠道（MPI、torch.distributed、kubejob 等）分发给所有 rank，然后每个 rank 各自调用 `HcclCommInitRootInfoConfig` 完成建链。它的好处是**不依赖任何共享文件系统**——信息交换完全由用户的拉起框架负责，库只要求「大家拿到同一份 root info」。

#### 4.1.2 核心流程

示例 01 的 `main()` 流程可以概括为：

```text
MPI_Init                          # 拉起 N 个进程
  ├─ MPI_Comm_size / MPI_Comm_rank  # 得到进程总数 procSize 与本进程编号 procRank
  ├─ devId = procRank; devCount = procSize   # 进程 i 独占设备 i
  ├─ aclInit + aclrtSetDevice       # ACL 初始化并绑定本进程的 NPU
  ├─ 若本进程是 rootRank(=0)：HcclGetRootInfo(&rootInfo)   # 生成 root info
  ├─ MPI_Bcast(rootInfo) + MPI_Barrier                # 广播 root info 并对齐进度
  ├─ HcclCommConfigInit(&config) + 按需改配置
  ├─ HcclCommInitRootInfoConfig(devCount, &rootInfo, devId, &config, &hcclComm)
  ├─ Sample(&args)                 # 执行一次 AllReduce 并打印
  └─ HcclCommDestroy → aclrtResetDevice → aclFinalize → MPI_Finalize
```

注意一个细节：只有 rank 0 调用 `HcclGetRootInfo`，其他 rank 的 `rootInfo` 是未初始化的栈变量，随后被 `MPI_Bcast` 覆盖为 rank 0 的内容。这就是「生成 → 广播 → 各自初始化」三步。

#### 4.1.3 源码精读

root info 的生成与广播，见 [examples/01_communicators/01_one_device_per_process/main.cc:L112-L121](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/examples/01_communicators/01_one_device_per_process/main.cc#L112-L121)：这段代码将 rank0 作为 root 节点——只有 `devId == rootRank` 的进程调用 `HcclGetRootInfo`，随后用 `MPI_Bcast` 把 4108 字节的 root info 广播给所有进程，并用 `MPI_Barrier` 保证所有进程都拿到信息后再进入初始化。

```cpp
HcclRootInfo rootInfo;
uint32_t rootRank = 0;
if (devId == rootRank) {
    HCCLCHECK(HcclGetRootInfo(&rootInfo));
}
MPI_Bcast(&rootInfo, HCCL_ROOT_INFO_BYTES, MPI_CHAR, rootRank, MPI_COMM_WORLD);
MPI_Barrier(MPI_COMM_WORLD);
```

通信域初始化调用，见 [examples/01_communicators/01_one_device_per_process/main.cc:L130-L132](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/examples/01_communicators/01_one_device_per_process/main.cc#L130-L132)：所有进程用同一份 root info、自己的 rank 编号和同一份配置，各自调用 `HcclCommInitRootInfoConfig` 创建通信域句柄 `hcclComm`。

```cpp
HcclComm hcclComm;
HCCLCHECK(HcclCommInitRootInfoConfig(devCount, &rootInfo, devId, &config, &hcclComm));
```

这两个接口在库侧的声明位于 [include/hccl/hccl_comm.h:L74-L87](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_comm.h#L74-L87)（`HcclGetRootInfo`）与 [include/hccl/hccl_comm.h:L100-L102](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_comm.h#L100-L102)（`HcclCommInitRootInfoConfig`，带 config 版本）。注意每个声明末尾的 `HCOMM_WEAK_SYMBOL`——正式包里这些符号由 HCOMM 的 `src/coll_communicator_mgr/api_c_adpt/` 适配层提供强实现（u2-l1 会跟踪这条调用链）。

`HcclRootInfo` 本体只是一个不透明的字节数组，见 [include/hccl/hccl_types.h:L112-L124](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_types.h#L112-L124)：`HCCL_ROOT_INFO_BYTES = 4108`，`HcclRootInfo` 内部就是 `char internal[4108]`，对外完全屏蔽了内部字段（Device IP、Device ID 等），避免 ABI 耦合。

销毁侧，见 [examples/01_communicators/01_one_device_per_process/main.cc:L141-L145](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/examples/01_communicators/01_one_device_per_process/main.cc#L141-L145)：`HcclCommDestroy(hcclComm)` 销毁通信域，随后按 `aclrtResetDevice → aclFinalize → MPI_Finalize` 的顺序释放资源。释放顺序是固定的：通信域必须先于设备重置销毁，否则会访问已释放的设备资源。

#### 4.1.4 代码实践

**实践目标**：跑通示例 01，亲眼看一次 AllReduce 输出。

**操作步骤**（依据示例自身的 [README.md](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/examples/01_communicators/01_one_device_per_process/README.md)）：

1. 环境准备（需要昇腾硬件 + CANN 安装）：
   ```bash
   source /usr/local/Ascend/cann/set_env.sh   # 设置 CANN 环境变量
   export MPI_HOME=/usr/local/mpich           # 按实际 MPI 安装路径设置
   ```
   注意：若使用本源码仓自编译的 HCOMM run 包，还需按 README「关闭验签」章节关闭驱动安全验签（自编译 tar.gz 子包不含签名头）。
2. 编译与运行：
   ```bash
   cd examples/01_communicators/01_one_device_per_process
   make
   make test N=8    # Ascend 950PR/950DT 单机 2 卡场景用 N=2
   ```

**需要观察的现象**：8 个进程各打印一行 `rankId: x, output: [...]`，所有行内容相同。

**预期结果**（README 给出的参考输出）：

```text
rankId: 0, output: [ 0 8 16 24 32 40 48 56 ]
...（8 行完全相同）
```

因为每个 rank 的输入都是 `[0,1,...,7]`，8 份求和后每位是 `[0,8,16,...,56]`。

**若无昇腾硬件**：无法运行，此步标注「待本地验证」。可退而做源码阅读型实践——通读 `main.cc` 并对照本讲流程图，手工标注每一行代码属于「初始化 / 数据准备 / 通信 / 清理」哪个阶段。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `MPI_Bcast` 那一行删掉，程序会发生什么？

**答案**：rank 0 之外的所有进程持有未初始化的栈上 `rootInfo`（内容是随机残留值），它们调用 `HcclCommInitRootInfoConfig` 时会拿着错误的建链信息去初始化，通常表现为初始化超时或返回错误码；即使碰巧不崩溃，通信域也无法正确建立。这说明了 root info「必须全通信域一致」的契约。

**练习 2**：为什么 `MPI_Barrier(MPI_COMM_WORLD)` 要放在 `MPI_Bcast` 之后、初始化之前？

**答案**：`MPI_Bcast` 本身对非 root 进程也是同步的（都要等到广播完成才返回），barrier 是再加一层保险，确保**所有**进程都拿到完整 root info 后才同时进入 `HcclCommInitRootInfoConfig`。库内部初始化阶段各 rank 会互相等待（建链握手），若某个 rank 严重滞后，可能导致其他 rank 初始化超时。

**练习 3**：`HcclGetRootInfo` 为什么只在 rank 0 调用，而不是每个 rank 都调用？

**答案**：root info 的作用是「唯一的一份建链凭据」。每个 rank 各自生成一份会导致信息互不一致，通信域无法建立。协议约定由 root 生成一份、广播给所有人，大家在同一份凭据下会合。

### 4.2 通信域配置结构 HcclCommConfig 与 HcclCommConfigInit

#### 4.2.1 概念说明

`HcclCommInitRootInfoConfig` 比 `HcclCommInitRootInfo` 多接收一个 `HcclCommConfig*` 配置结构，允许用户在创建通信域时定制 buffer 大小、确定性计算、通信域名称等行为。这个结构体有个非常值得学习的设计：**头部 24 字节是版本协商区**（size + magicWord + version），配合尾部不断追加字段的方式实现「旧程序读新库、新程序读旧库」的 ABI 兼容。这是 HCOMM 中反复出现的通用手法（u3-l4 的 ChannelDesc 也用同样思路）。

#### 4.2.2 核心流程

使用配置的标准三步：

```text
1. HcclCommConfigInit(&config)   # inline 函数，填默认值 + 写入 size/magic/version
2. 按需覆盖个别字段              # 只改关心的项，其余保持 NOT_SET 哨兵值
3. 把 &config 传给 HcclCommInitRootInfoConfig
```

#### 4.2.3 源码精读

示例中的用法见 [examples/01_communicators/01_one_device_per_process/main.cc:L123-L129](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/examples/01_communicators/01_one_device_per_process/main.cc#L123-L129)：先 `HcclCommConfigInit` 初始化，再覆盖 buffer 大小（1024MB）、开启确定性计算、设置通信域名字 `"comm_1"`。

```cpp
HcclCommConfig config;
HcclCommConfigInit(&config);
config.hcclBufferSize = 1024;      // 单位 MB，默认 200
config.hcclDeterministic = 1;      // 默认 0（关闭）
std::strcpy(config.hcclCommName, "comm_1");
```

`HcclCommConfigInit` 是头文件里的 `static inline` 函数，见 [include/hccl/hccl_comm.h:L202-L240](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_comm.h#L202-L240)：它先把结构体头部强转成 `configInfo_t`（`size`/`magicWord`/`version`），写入当前编译时的大小和版本号，再把每个业务字段重置为「未设置」哨兵值（如 `HCCL_COMM_BUFFSIZE_CONFIG_NOT_SET = 0xffffffff`）。库侧拿到 config 后先校验 magicWord 与 version，再按 size 判断用户程序是按哪个版本的布局填充的，从而安全读取公共前缀字段。

结构体定义见 [include/hccl/hccl_types.h:L143-L154](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_types.h#L143-L154)：开头是 `char reserved[HCCL_COMM_CONFIG_INFO_BYTES]`（24 字节协商区，见 L126-L128 的 `HCCL_COMM_CONFIG_INFO_BYTES = 24`、`HCCL_COMM_CONFIG_MAGIC_WORD = 0xf0f0f0f0`、`HCCL_COMM_CONFIG_VERSION = 11`），后面依次排列 `hcclBufferSize`、`hcclDeterministic`、`hcclCommName` 等字段。

#### 4.2.4 代码实践

**实践目标**：体会「NOT_SET 哨兵值」机制。

**操作步骤**：

1. 在示例 01 中，把 `config.hcclBufferSize = 1024;` 注释掉，重新编译运行，观察输出是否仍正确。
2. 再故意把 `hcclBufferSize` 设为 0（README 说明取值需 ≥ 1），运行观察错误行为。

**需要观察的现象**：第 1 步程序正常运行（未设置时库用默认值 200MB）；第 2 步预期初始化阶段报错（具体错误码形式待本地验证）。

**预期结果**：理解「用户没设置的项以 0xffffffff 传给库，库侧回退默认值」的约定；非法值则在配置校验阶段被拦截。

#### 4.2.5 小练习与答案

**练习 1**：为什么不建议用户直接 `memset(&config, 0, sizeof(config))` 代替 `HcclCommConfigInit`？

**答案**：memset 会把头部协商区清零——magicWord 变成 0、size 变成 0，库侧校验 magic/版本会失败；同时 `hcclBufferSize` 等字段被清成 0 而不是 `0xffffffff` 哨兵值，库无法区分「用户显式设了 0」和「用户没设置」。必须用 `HcclCommConfigInit`。

**练习 2**：`HCCL_COMM_CONFIG_VERSION = 11` 意味着这个结构体已经演进过至少 11 个版本，为什么旧版本程序仍能链接新版本的库？

**答案**：结构体头部 24 字节固定不变，新字段只追加在尾部；用户程序编译时记录的 `size` 比库认知的小，库只读取 `size` 覆盖范围内的公共字段，越界部分按未设置处理。这就是「头部协商 + 尾部扩展」的 ABI 兼容手法。

### 4.3 AllReduce 执行与流同步：ACLCHECK/HCCLCHECK 与异步语义

#### 4.3.1 概念说明

通信域就绪后，示例的 `Sample()` 函数演示了完整的「数据准备 → 下发通信 → 同步 → 读取结果 → 释放资源」生命周期。这一段的两个学习点：一是 `ACLCHECK`/`HCCLCHECK` 两个宏的防御式错误处理写法；二是 HCCL 接口「下发到流、立即返回」的异步语义——`HcclAllReduce` 返回成功只代表任务入队成功，不代表计算完成。

#### 4.3.2 核心流程

```text
aclrtMalloc(sendBuf/recvBuf)          # Device 侧收发缓冲
aclrtMallocHost(hostBuf) + 填初值      # Host 侧准备输入 0~7
aclrtMemcpy(H2D)                      # 输入搬到 Device
aclrtCreateStream(stream)
HcclAllReduce(sendBuf, recvBuf, ...)  # 任务下发到 stream，立即返回
aclrtSynchronizeStream(stream)        # 阻塞等待通信完成 ← 关键
aclrtMemcpy(D2H) + 打印               # 结果搬回 Host
释放：sendBuf/recvBuf/hostBuf/stream
```

#### 4.3.3 源码精读

错误检查宏定义见 [examples/01_communicators/01_one_device_per_process/main.cc:L22-L36](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/examples/01_communicators/01_one_device_per_process/main.cc#L22-L36)：`ACLCHECK` 检查 ACL 接口返回值是否为 `ACL_SUCCESS`，`HCCLCHECK` 检查 HCCL 接口返回值是否为 `HCCL_SUCCESS`，失败时打印文件名、行号和错误码并立刻向上返回。`do { } while (0)` 包裹保证宏在 `if/else` 等任何语法位置都安全展开。三个示例都复制了这两个宏——生产代码中建议用统一头文件，示例为了自包含才各自内联。

AllReduce 调用与同步见 [examples/01_communicators/01_one_device_per_process/main.cc:L71-L74](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/examples/01_communicators/01_one_device_per_process/main.cc#L71-L74)：

```cpp
HCCLCHECK(HcclAllReduce(sendBuf, recvBuf, count, HCCL_DATA_TYPE_FP32,
                        HCCL_REDUCE_SUM, ctx->comm, stream));
ACLCHECK(aclrtSynchronizeStream(stream));
```

`HcclAllReduce` 的 7 个参数依次是：发送缓冲 Device 指针、接收缓冲 Device 指针、元素个数（注意是元素数不是字节数）、数据类型、归约类型、通信域句柄、流。紧接着的 `aclrtSynchronizeStream` 是必选项——注释里明确写着「阻塞等待任务流中的集合通信任务执行完成」。归约类型枚举定义在 [include/hccl/hccl_types.h:L74](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_types.h#L74)（`HCCL_REDUCE_MAX = 2`），数据类型枚举 `HCCL_DATA_TYPE_FP32` 等也在同一文件。

资源释放顺序见 [examples/01_communicators/01_one_device_per_process/main.cc:L89-L94](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/examples/01_communicators/01_one_device_per_process/main.cc#L89-L94)：Device 内存、Host 内存、stream 依次释放；通信域的销毁则在 `main()` 中 `Sample` 返回之后（L142）。

另一个细节：L77 的 `std::this_thread::sleep_for(std::chrono::seconds(device))` 只是让不同 rank 的打印错开时间、便于阅读输出，与通信正确性无关。

#### 4.3.4 代码实践

**实践目标**：验证异步语义——去掉同步会发生什么。

**操作步骤**：

1. 把 `aclrtSynchronizeStream(stream)` 一行注释掉，重新编译运行（多运行几次）。
2. 观察打印的 `recvBuf` 内容。

**需要观察的现象**：输出可能仍是正确值（通信恰好在 D2H 拷贝前完成了），也可能出现部分旧数据/全 0，且多次运行结果不稳定。因为 D2H 拷贝本身也下发到默认流，行为取决于任务实际完成时机。

**预期结果**：直觉上「删了同步还能对」不等于「正确」——这是典型的数据竞争。结论：任何读取通信结果的代码前必须有流同步（或依赖同一 stream 上的后续任务保序）。此实验需要真实硬件，标注「待本地验证」。

**无硬件替代实践**：阅读 [examples/01_communicators/01_one_device_per_process/Makefile:L51-L54](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/examples/01_communicators/01_one_device_per_process/Makefile#L51-L54) 的 `test` 目标，理解 `mpirun -n $(N) ./one_device_per_process` 如何把同一份可执行文件拉起 N 份、每份通过 `MPI_Comm_rank` 获得不同身份。

#### 4.3.5 小练习与答案

**练习 1**：`HcclAllReduce` 的 `count` 参数传的是字节数还是元素个数？传错会怎样？

**答案**：元素个数。示例中 `count = devCount`（8 个 float），`mallocSize = count * sizeof(float)`。若误传字节数（32），库会按 32 个 float 处理，读越界并归约出错误结果。

**练习 2**：`ACLCHECK` 宏里的 `do { } while (0)` 起什么作用？

**答案**：把多条语句包成一条语句，使宏可以安全地用在 `if (cond) ACLCHECK(x); else ...` 这类要求单语句的语法位置，同时保留局部作用域。这是 C/C++ 多语句宏的标准写法。

**练习 3**：为什么 sendBuf/recvBuf 用 `aclrtMalloc`（Device 内存）而不是 malloc 的 Host 内存？

**答案**：HCCL 集合通信算子在 Device 侧执行（AICPU/AIV/CCU 等通信引擎），输入输出必须位于 Device 内存，库直接通过 Device 间链路（HCCS/RDMA 等）搬运，不经过 Host。Host 内存只用于准备输入和打印输出，通过 `aclrtMemcpy` 显式搬运。

### 4.4 初始化方式对比：rank table（示例 02）与多线程（示例 03）

#### 4.4.1 概念说明

`examples/README.md`（[L5-L9](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/examples/README.md#L5-L9)）列出的三个示例分别对应三种典型的通信域建立姿势：

| 示例 | 初始化接口 | 拉起方式 | root info 分发 |
| --- | --- | --- | --- |
| 01_one_device_per_process | `HcclCommInitRootInfoConfig` | MPI 多进程 | MPI_Bcast |
| 02_one_device_per_process_rank_table | `HcclCommInitClusterInfoConfig` | MPI 多进程 | rank table 文件（共享存储） |
| 03_one_device_per_pthread | `HcclCommInitRootInfoConfig` | 单进程多线程 | 进程内共享同一份指针 |

rank table 方式的核心差异：不再需要 root info 广播，而是**所有 rank 读同一个 JSON 文件**（文件里写好了每个 rank 的 IP、device 等信息），库从文件中获得建链信息。代价是要求共享文件系统；好处是与拉起框架解耦，K8s 等调度器可以直接生成 rank table 下发。

#### 4.4.2 核心流程

示例 02 相对 01 的差异点（其余代码几乎完全一致）：

```text
aclrtGetSocName()                       # 查询芯片型号
  ├─ 型号含 "Ascend950" → 用 rank_table_v2.json
  └─ 否则               → 用 rank_table.json
HcclCommInitClusterInfoConfig(rankTableFile, devId, &config, &hcclComm)
  # 对比 01：HcclCommInitRootInfoConfig(devCount, &rootInfo, devId, &config, &hcclComm)
```

示例 03 相对 01 的差异点：

```text
单进程：main 里 aclrtGetDeviceCount 查设备数
root info 只生成一次（Host 内存），所有线程共享同一指针
每个线程：aclrtSetDevice(i) → 各自 HcclCommInitRootInfoConfig → 各自 AllReduce
         → 通信域销毁在各线程内完成（不同于 01 在 main 中）
```

#### 4.4.3 源码精读

示例 02 的 rank table 路径选择见 [examples/01_communicators/02_one_device_per_process_rank_table/main.cc:L113-L120](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/examples/01_communicators/02_one_device_per_process_rank_table/main.cc#L113-L120)：先用 `aclrtGetSocName()` 取芯片名，Ascend950 系列选择 `rank_table_v2.json`（v2 格式），其他型号选择 `rank_table.json`，两个 JSON 文件与 `main.cc` 同目录（可在仓库 `examples/01_communicators/02_one_device_per_process_rank_table/` 下看到 `rank_table.json` 与 `rank_table_v2.json`）。

初始化调用见 [examples/01_communicators/02_one_device_per_process_rank_table/main.cc:L128-L130](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/examples/01_communicators/02_one_device_per_process_rank_table/main.cc#L128-L130)：

```cpp
HcclComm hcclComm;
HCCLCHECK(HcclCommInitClusterInfoConfig(rankTableFile, devId, &config, &hcclComm));
```

第一个参数从「root info 指针」换成了「cluster info 文件路径」，且不再需要 `nRanks` 参数——rank 数量等信息都在 JSON 文件里。该接口声明见 [include/hccl/hccl_comm.h:L48-L49](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_comm.h#L48-L49)（无 config 版本 `HcclCommInitClusterInfo` 在 L36）。

示例 03 的共享 root info 见 [examples/01_communicators/03_one_device_per_pthread/main.cc:L121-L140](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/examples/01_communicators/03_one_device_per_pthread/main.cc#L121-L140)：main 线程先 `aclrtSetDevice(0)` 并生成一份 root info 存在 Host 内存，然后为每块 NPU 起一个 `std::thread`；每个线程内部（L56-L78）自己 `aclrtSetDevice(i)`、自己调用 `HcclCommInitRootInfoConfig` 建立属于该设备的通信域、自己执行 AllReduce 并销毁。由于线程共享进程地址空间，「广播 root info」退化为传同一个指针。

顺带一提：仓库根的 [examples/build.sh](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/examples/build.sh#L22-L28) 会遍历所有示例目录执行 `make && make test`，但**显式跳过** 01 和 02 两个目录（见 L24-L25 的跳过名单）——推测因为它们需要真实多卡集群环境，不适合在自动构建中执行；03（单进程多线程）则参与自动构建验证。这也是「无硬件时优先看 03」的提示。

#### 4.4.4 代码实践（本讲综合指定任务第一部分）

**实践目标**：把示例 01 的 AllReduce 归约类型改为 `HCCL_REDUCE_MAX`，验证输出。

**操作步骤**：

1. 复制示例目录（避免改动源码仓）：`cp -r examples/01_communicators/01_one_device_per_process /tmp/my_allreduce`
2. 修改 `main.cc` 第 72 行（[L72](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/examples/01_communicators/01_one_device_per_process/main.cc#L72)）：
   ```cpp
   HCCLCHECK(HcclAllReduce(sendBuf, recvBuf, count, HCCL_DATA_TYPE_FP32,
                           HCCL_REDUCE_MAX, ctx->comm, stream));   // SUM → MAX
   ```
3. 编译运行：`make && make test N=8`

**需要观察的现象**：所有 rank 打印相同结果。

**预期结果**：每个 rank 的输入都是 `[0,1,...,7]`，8 份逐元素取最大值仍为 `[0,1,...,7]`：

```text
rankId: x, output: [ 0 1 2 3 4 5 6 7 ]
```

要让结果更可区分，可进一步把 L62 的初始化改为 `tmpHostBuff[i] = static_cast<float>(i + device);`（rank r 的输入为 `[r, r+1, ..., r+7]`），此时 MAX 的预期输出为 `[7 8 9 10 11 12 13 14]`。此任务需要真实昇腾环境，「待本地验证」；无硬件时可手工推导两版预期输出并写成断言。

**对比任务第二部分**：diff 两个示例的 `main()`，确认唯一实质差异是「root info 生成+广播（01 的 L112-L121）」被「rank table 路径选择（02 的 L113-L120）」替换、初始化接口从 `HcclCommInitRootInfoConfig` 换成 `HcclCommInitClusterInfoConfig`，`Sample()` 部分逐字节相同。打开 `rank_table.json` / `rank_table_v2.json` 观察两代格式的差异。

#### 4.4.5 小练习与答案

**练习 1**：示例 02 中如果所有进程使用不同的 rank table 文件路径（内容一致），还能工作吗？如果文件内容不一致呢？

**答案**：路径不同没关系，库只读文件内容；内容不一致则各 rank 对集群的认知互相矛盾，初始化阶段建链失败或超时。契约仍是「所有 rank 看到同一份集群描述」。

**练习 2**：示例 03 里为什么每个线程要各自 `aclrtSetDevice(device)`？

**答案**：ACL 的设备绑定是线程私有的（thread-local），线程必须先声明自己操作哪块设备，后续该线程的 `aclrtMalloc`、流创建、`HcclAllReduce` 才会作用在对应 NPU 上。main 线程的 `aclrtSetDevice(0)` 只影响 main 线程。

**练习 3**：三种方式各自适合什么场景？

**答案**：root info（01）适合有现成分发机制的框架（MPI、torch.distributed），不依赖共享存储，是训练框架中最常用的方式；rank table（02）适合调度器能统一下发配置文件的场景（如 K8s 挂载 ConfigMap），与拉起框架解耦；多线程（03）适合单进程推理/小型单机任务，省去进程间通信，但受限于单进程地址空间，示例源码中该接口注释也说明单进程多卡方式不支持跨机。

## 5. 综合实践

把本讲内容串起来做一个小任务——**实现一个「自定义输入 + 双归约验证」的示例**：

1. 以示例 01 为模板复制一份（不动源码仓）。
2. 修改输入初始化：rank r 的第 i 个元素为 \( r \times 8 + i \)（即每个 rank 输入互不相同）。
3. 先后执行两次 AllReduce：一次 `HCCL_REDUCE_SUM`，一次 `HCCL_REDUCE_MAX`，分别同步并打印，中间可用 `HcclBarrier`（接口见 [include/hccl/hccl_comm.h:L158](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_comm.h#L158)）分隔。
4. 手工推导 8 卡时两种归约的预期输出（SUM 第 i 位为 \( \sum_{r=0}^{7}(8r+i) = 224 + 8i \)，即 `[224 232 240 ... 280]`；MAX 第 i 位为 \( 56+i \)，即 `[56 57 58 ... 63]`），与实际输出对照。
5. 把通信域配置中的 `hcclCommName` 改成 `"my_comm"`，并用 `HcclGetCommName`（[include/hccl/hccl_comm.h:L123](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_comm.h#L123)）读回来打印，验证配置生效路径。

本实践覆盖：通信域初始化、配置结构、AllReduce 参数语义、流同步、查询接口与销毁流程。需要真实昇腾环境，「待本地验证」；无硬件时完成第 4 步的纸面推导与第 5 步的接口调用链梳理同样是有效练习。

## 6. 本讲小结

- 通信域初始化有两条主路径：**root info 方式**（`HcclGetRootInfo` 生成 → 外部机制广播 → `HcclCommInitRootInfoConfig` 各 rank 初始化）与 **rank table 方式**（`HcclCommInitClusterInfoConfig` 直接读共享 JSON 文件），另有单进程多线程变体。
- root info 是 4108 字节的不透明字节数组（`include/hccl/hccl_types.h:L112-L124`），其分发是用户框架的责任，库只要求全通信域一致。
- `HcclCommConfig` 采用「头部 24 字节 size/magic/version 协商 + 尾部字段追加」的 ABI 兼容设计，必须用 `HcclCommConfigInit` 初始化，未设置项保持 `0xffffffff` 哨兵值。
- `HcclAllReduce` 按「元素个数 + 数据类型 + 归约类型」描述操作，任务是**异步下发到 stream** 的，读取结果前必须 `aclrtSynchronizeStream`。
- `ACLCHECK`/`HCCLCHECK` 是 `do{}while(0)` 包裹的「检查-打印-返回」错误处理宏，三个示例各自内联了一份。
- 释放顺序固定：通信域销毁 → 设备重置（`aclrtResetDevice`）→ ACL 去初始化（`aclFinalize`）。

## 7. 下一步学习建议

本讲只把 HCCL 的对外接口「当黑盒用」了一遍。下一讲 **u2-l1《HCCL C 接口与通信域生命周期》** 将打开这个黑盒：从 `HcclCommInitRootInfoConfig` 的 weak symbol 声明（[include/hccl/hccl_comm.h:L100-L102](https://github.com/gitcode.com/cann/hcomm/blob/29cd856550383ec133aa2f9259ae9a1f1764e4ec/include/hccl/hccl_comm.h#L100-L102)）出发，进入 `src/coll_communicator_mgr/api_c_adpt/coll_comm_c_adpt.cc`，跟踪 C 接口如何转接到内部实现并创建通信域对象。建议预先浏览：

- `src/coll_communicator_mgr/api_c_adpt/` 目录下的文件名，猜一猜每个 C 接口对应的适配函数。
- 示例中 `MPI_Bcast` 之后、`HcclCommInitRootInfoConfig` 返回之前，库内部各 rank 到底交换了什么——这正是 u2-l5 `rank_info_detect` 模块的主题。
