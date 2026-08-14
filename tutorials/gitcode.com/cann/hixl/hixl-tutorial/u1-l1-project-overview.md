# HIXL 是什么：项目定位、架构与核心概念

## 1. 本讲目标

学完本讲，你应该能够：

1. 用自己的话说出 HIXL（Huawei Xfer Library）是什么、解决什么问题。
2. 说出 HIXL 的典型业务场景：大模型 PD 分离（Prefill/Decode 分离）、KV Cache 传输、RL 后训练参数切换、模型参数缓存等。
3. 区分仓库中三个组件的职责边界：HIXL Engine、LLM-DataDist、Python 绑定。
4. 理解「单边零拷贝通信」这个核心概念，以及 HCCS / RDMA 多链路、D2D / D2H / H2D 多种内存传输路径的含义。

本讲是整套学习手册的第一篇，不要求你写过任何通信代码，我们会从 README 和文档出发，一步步建立对项目的整体认识。

## 2. 前置知识

在阅读本讲之前，你不需要熟悉 HIXL 本身，但下面几个通俗概念会帮助你更快理解：

- **点对点传输**：两台机器（或两张卡）之间直接搬数据，不经过中心节点，就像两个人直接递东西，而不是先放到一个公共仓库再取。
- **Device 与 Host**：在昇腾（以及 GPU）语境里，Host 指 CPU 侧（主机内存），Device 指 AI 加速卡侧（设备显存）。
- **D2D / D2H / H2D**：描述数据搬运的方向——
  - D2D：Device 到 Device（卡到卡，比如两张昇腾卡之间传 KV Cache）；
  - D2H：Device 到 Host（卡到主机内存）；
  - H2D：Host 到 Device（主机内存到卡）。
- **双边 vs 单边通信**：双边通信（比如常见的 collective 通信）要求收发两端都参与调用接口；单边通信只需要一端发起操作，即可直接读/写远端内存，远端进程「什么都不用做」。
- **KV Cache**：大模型推理时，Prompt 阶段计算出的 Key/Value 缓存。PD 分离架构里，Prefill 节点（负责处理输入）要把 KV Cache 传给 Decode 节点（负责逐 token 生成），这个传输量非常大，是 HIXL 最重要的应用场景。
- **HCCS 与 RDMA**：两种高速互联方式。HCCS（Huawei Cache Coherence System）是华为芯片间的高速互联总线，适合同主机/超节点内的卡间通信；RDMA（Remote DMA）绕过远端操作系统内核直接访问远端内存，适合跨主机网络传输。

## 3. 本讲源码地图

本讲以文档和公开头文件为主，帮你建立全局观，暂不深入实现细节：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/README.md) | 项目门面：定位、核心优势、性能数据、目录结构、快速入门入口 |
| [AGENTS.md](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/AGENTS.md) | 仓库工作指引：组件职责、关键目录、构建与测试命令 |
| [docs/README.md](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/README.md) | 资料书架总览：开发指南、技术文章、接口文档入口 |
| [docs/zh/guide/introduction.md](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/guide/introduction.md) | 开发指南简介：C++/Python 两种使用场景的说明 |
| [include/hixl/hixl.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl.h) | HIXL Engine 的公开 C++ API 头文件（本讲只看接口轮廓） |
| [include/llm_datadist/llm_datadist.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h) | LLM-DataDist 的公开 C++ API 头文件（本讲只看接口轮廓） |

## 4. 核心概念与源码讲解

### 4.1 HIXL 项目定位与典型业务场景

#### 4.1.1 概念说明

HIXL（Huawei Xfer Library）是面向昇腾芯片的**单边通信库**。一句话概括它的定位：**在集群中的多张 AI 加速卡/多台主机之间，提供简单、可靠、高效的点对点数据传输能力**。它站在「多 AI 应用」和「多传输链路」中间，充当桥梁：

- 向上，为应用暴露极简的 API（核心调用只有 10 余个），应用不需要关心底层用的是 HCCS 还是 RDMA、对面是 A2 还是 A3 芯片；
- 向下，屏蔽昇腾系列芯片的硬件差异，自动选择合适的高速互联链路。

README 中列举的典型业务场景包括：

1. **大模型 PD 分离**：Prefill 节点与 Decode 节点分离部署，中间用 HIXL 传 KV Cache；
2. **RL 后训练参数切换**：强化学习训练中不同推理实例间的参数快速搬运；
3. **模型参数缓存**：把模型参数在集群节点间按需传输、缓存。

#### 4.1.2 核心流程

从使用者视角看，一次典型的 HIXL 数据传输流程是：

```text
初始化（声明我是谁，server 端监听端口）
        │
        ▼
注册内存（把要传输的本地内存「登记」给引擎，获得远端可见性）
        │
        ▼
建链（Connect 到远端 engine，交换内存注册信息）
        │
        ▼
传输（单边 READ/WRITE：本地地址 ↔ 远端地址直接搬数据）
        │
        ▼
清理（解注册内存、断链、Finalize）
```

注意这个流程里远端只需要「初始化 + 注册内存」，传输阶段远端可以完全投入计算——这正是单边通信适合「通信与计算重叠」的原因。

#### 4.1.3 源码精读

项目定位的权威表述在 README 概述一节：

- [README.md:31](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/README.md#L31) —— 明确 HIXL 是「灵活、高效的昇腾单边通信库，面向集群场景提供简单、可靠、高效的点对点数据传输能力」，并列出 PD 分离、RL 后训练参数切换、模型参数缓存等业务场景。
- [README.md:35-L37](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/README.md#L35-L37) —— 三大核心优势：单边零拷贝、屏蔽硬件差异兼容多链路（带宽最高 119GB/s）、极简 API 且深度对接 Mooncake/DeepLink/vLLM/SGLang 生态。

AGENTS.md 用更工程化的语言复述了同样的定位：

- [AGENTS.md:7](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/AGENTS.md#L7) —— HIXL 是面向昇腾芯片的单边通信库，支持 HCCS 和 RDMA 协议进行点对点 D2D/D2H/H2D 数据传输，并通过 pybind11 提供 Python 绑定。

开发指南的简介页则从「用户如何使用」的角度补充了场景说明：

- [docs/zh/guide/introduction.md:10-L11](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/guide/introduction.md#L10-L11) —— C++ 场景下 HIXL 提供「纯粹的基于本地地址和远端地址的传输能力」（支持 D2D/D2H/H2D）；LLM-DataDist 接口则提供链路管理和 KV Cache 管理，Prompt 与 Decode 可以双向推送、拉取 Cache。

#### 4.1.4 代码实践

**实践 1：从官方文档提炼项目一句话定位**

1. 实践目标：能不借助任何资料，用两三句话向同事介绍 HIXL 是什么。
2. 操作步骤：
   - 通读 [README.md](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/README.md) 的「概述」「核心优势」「核心组件」三节；
   - 再读 [docs/zh/guide/introduction.md](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/guide/introduction.md)（很短，两段话加一张表）；
   - 用自己的话写下：HIXL 解决什么问题？给谁用？凭什么比直接用网络库/集合通信库更合适？
3. 需要观察的现象：README 中提到的带宽数据（HCCS 119GB/s、RDMA 22GB/s）和场景关键词（PD 分离、KV Cache、Mooncake、vLLM、SGLang）。
4. 预期结果：形成一段 100 字以内的项目定位描述。例如（仅供参考）：「HIXL 是昇腾上的单边零拷贝通信库，一张卡可以不经远端 CPU 参与直接读写远端内存，屏蔽 HCCS/RDMA 链路差异，主要用来做大模型 PD 分离场景的 KV Cache 传输。」

#### 4.1.5 小练习与答案

**练习 1**：HIXL 的三个典型业务场景是什么？其中与 LLM 推理最直接相关的是哪个？

<details>
<summary>参考答案</summary>

三个场景：大模型 PD 分离、RL 后训练参数切换、模型参数缓存（见 [README.md:31](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/README.md#L31)）。与 LLM 推理最直接相关的是 PD 分离：Prefill 节点把算好的 KV Cache 通过 HIXL 传给 Decode 节点。
</details>

**练习 2**：README 说 HIXL「在多 AI 应用和多传输链路之间建立了桥梁」，这句话怎么理解？

<details>
<summary>参考答案</summary>

「多 AI 应用」指上层使用者：vLLM、SGLang、Mooncake、DeepLink 等框架；「多传输链路」指底层硬件路径：HCCS、RDMA 等互联协议。HIXL 向上提供统一的极简 API，向下自动适配不同链路和不同代际的昇腾芯片，让应用无需为每种硬件写适配代码。
</details>

### 4.2 单边零拷贝通信与多链路

#### 4.2.1 概念说明

**单边零拷贝（One-Sided Zero-Copy）** 是 HIXL 最核心的技术卖点，拆开看是两个词：

- **单边（One-Sided）**：一次传输只由一端发起。发起方直接把数据从本地内存写到远端内存（WRITE），或者把远端内存的数据读回本地（READ）。远端节点不需要调用任何接口、不需要起接收线程，甚至可以正在忙着算矩阵乘。这为「通信与计算重叠掩盖」提供了基础——通信不再阻塞双方的 CPU/NPU。
- **零拷贝（Zero-Copy）**：数据在用户内存之间直接传输，不在库内部再经过中间缓冲区倒手。好处是既省内存带宽（不搬多余的一趟），又省内存容量（不需要额外分配 staging buffer）。

**多链路**指 HIXL 原生支持多种高速互联协议：

| 链路 | 特点 | 典型场景 |
|------|------|----------|
| HCCS | 华为芯片间高速互联总线，带宽极高 | 同主机/超节点内卡间，README 记录 128M 数据带宽可达 119GB/s |
| RDMA | 绕过远端内核的网络直接内存访问 | 跨主机传输，README 记录带宽可达 22GB/s |

此外 README 的 Latest News 还提到了更多链路形态（如超节点内 FabricMem 模式、Host RoCE、A3 超平面 D2rH 直传等），这些属于进阶主题，后续单元会专门讲解，本讲只需建立「HIXL 支持多种链路、并按场景自动适配」的认识。

#### 4.2.2 核心流程

对比一下双边通信和单边零拷贝在一次「A 把数据给 B」时的差异：

```text
双边通信（如传统集合通信）：
  A: send(buf_a)  ──┐
                    ├── 双方都要调用接口、共同参与同步
  B: recv(buf_b)  ──┘

HIXL 单边零拷贝：
  B: RegisterMem(buf_b)        ← B 只需提前注册内存（一次性的）
  A: Connect(B)                ← A 建链后拿到 B 的内存布局信息
  A: Transfer(WRITE, buf_a → remote_addr)
                                ← 传输时 B 完全不参与，可继续计算
```

这就是为什么 PD 分离场景偏爱单边通信：Decode 节点可以一边解码生成 token，一边「被动」接收 Prefill 节点推来的 KV Cache。

#### 4.2.3 源码精读

- [README.md:35](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/README.md#L35) —— 官方对单边零拷贝的完整定义：「在本地内存数据准备就绪之后，通过单边操作完成向远端内存的直接数据传输。该机制无需远端节点执行任何操作……零拷贝能力实现用户内存间的直接数据传输，避免冗余数据搬运」。
- [README.md:36](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/README.md#L36) —— 多链路能力：「原生支持 RDMA、HCCS 等多种高速互联协议，通信带宽最高可达 119GB/s，可实现跨架构设备（如 A2 系列与 A3 系列昇腾芯片）的无缝高速互联」。
- [README.md:48-L52](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/README.md#L48-L52) —— 性能数据：昇腾 A3 芯片、128M 数据，HCCS 链路带宽 119GB/s，RDMA 链路带宽 22GB/s。
- [AGENTS.md:7](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/AGENTS.md#L7) —— 「支持通过 HCCS 和 RDMA 协议进行点对点 D2D/D2H/H2D 数据传输」，点明了链路（HCCS/RDMA）与传输方向（D2D/D2H/H2D）两个正交的维度。

单边 READ/WRITE 的语义在 HIXL Engine 的公开头文件中就有体现——传输接口的参数直接叫 `operation`，取值是「读远端」或「写远端」：

- [include/hixl/hixl.h:117-L120](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl.h#L117-L120) —— `Transfer` 接口注释写明 `operation` 表示「将远端内存读到本地或者将本地内存写到远端」，且操作对象是「批量操作的本地以及远端地址」。这就是单边 READ / WRITE 的 API 雏形（接口全貌在单元 2 精读）。

#### 4.2.4 代码实践

**实践 2：把「传输方向 × 链路类型」整理成一张二维表**

1. 实践目标：分清 D2D/D2H/H2D（内存路径维度）和 HCCS/RDMA（链路维度）这两组正交概念。
2. 操作步骤：
   - 在 [README.md](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/README.md) 和 [AGENTS.md](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/AGENTS.md) 中标出所有出现 D2D/D2H/H2D 和 HCCS/RDMA 的位置；
   - 画一张 3×2 的表：行是 D2D/D2H/H2D，列是 HCCS/RDMA，在每个格子里填写你推测的典型业务场景（例如 D2D×HCCS：同主机两张卡间传 KV Cache）；
   - 标注哪些格子是 README 明确支持的，哪些是你推测的（推测的注明「待确认」）。
3. 需要观察的现象：README 没有把所有「方向×链路」组合都明确列出，你需要区分「文档明说」和「合理推测」。
4. 预期结果：一张二维矩阵表 + 每格的依据标注。本练习为纯源码/文档阅读型实践，无需运行环境。

#### 4.2.5 小练习与答案

**练习 1**：为什么说单边通信有利于「通信与计算重叠」？

<details>
<summary>参考答案</summary>

因为传输只需要一端发起，远端节点不需要执行任何接收操作，可以在通信进行的同时继续做计算。例如 Decode 节点一边逐 token 生成，一边由 Prefill 节点单边把 KV Cache 写进来（见 [README.md:35](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/README.md#L35)）。
</details>

**练习 2**：「零拷贝」省掉了什么？带来什么收益？

<details>
<summary>参考答案</summary>

省掉了库内部中间缓冲区的数据倒手（用户内存 → staging buffer → 网络 → staging buffer → 用户内存 变成 用户内存 ↔ 网络 直接传输）。收益：降低内存带宽占用（少搬几趟）、减少内存容量消耗（不用额外分配缓冲区）。见 [README.md:35](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/README.md#L35)。
</details>

**练习 3**：HCCS 和 RDMA 各适合什么场景？README 给出的带宽数据分别是多少？

<details>
<summary>参考答案</summary>

HCCS 适合同主机/超节点内的卡间互联（带宽高，128M 数据 119GB/s）；RDMA 适合跨主机网络传输（128M 数据 22GB/s）。数据见 [README.md:48-L52](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/README.md#L48-L52)。
</details>

### 4.3 核心组件一：HIXL Engine（底层传输引擎）

#### 4.3.1 概念说明

**HIXL Engine** 是仓库的核心传输引擎，源码位于 `src/hixl/`，公开头文件位于 `include/hixl/`（以及 `include/cs/`）。它的职责可以概括为三件事：

1. **基础传输接口**：提供注册内存、建链、传输（READ/WRITE）、通知等最基础的通信原语；
2. **多内存类型支持**：D2D、D2H、H2D 等不同内存路径统一在一套 API 下；
3. **多链路兼容与集群适配**：兼容 HCCS、RDMA 等协议，同构/异构集群都能跑，集群动态扩缩容时能快速完成链路适配与资源调度。

你可以把 HIXL Engine 理解为「发动机」：它性能强、适应性强，但操作偏底层——用户直接面对的是「本地地址 + 远端地址」这样的裸内存描述，需要自己管理内存布局。

#### 4.3.2 核心流程

HIXL Engine 暴露的 C++ 入口是 `hixl::Hixl` 类，其接口按功能可分为五组：

```text
hixl::Hixl
├── 生命周期     Initialize / Finalize
├── 内存管理     RegisterMem / DeregisterMem
├── 链路管理     Connect / Disconnect（及 Async 版本 + 状态查询）
├── 数据传输     Transfer（同步）/ TransferAsync + GetTransferStatus（异步）
└── 通知机制     SendNotify / GetNotifies
```

一个有意思的细节：`Initialize` 的 `local_engine` 参数同时决定了本进程的角色——带上端口就是 server（监听），不带端口就是 client。这解释了 HIXL 样例常见的 server/client 双进程模型（下一讲会实际跑）。

#### 4.3.3 源码精读

- [README.md:43](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/README.md#L43) —— 官方对 HIXL Engine 的定义：「作为核心传输引擎，提供了基础传输接口，支持多种类型内存传输，比如 D2D、D2H、H2D。同时兼容多种传输协议，包括 HCCS、RDMA 等」。
- [AGENTS.md:11](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/AGENTS.md#L11) —— 实现位置：HIXL Engine 在 `src/hixl/`，是「底层传输引擎，支持多种内存类型和传输协议」。
- [include/hixl/hixl.h:26](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl.h#L26) —— 公开 API 的唯一入口类 `hixl::Hixl`，全部能力都挂在这一个类上，体现了「极简 API」的设计。
- [include/hixl/hixl.h:39-L46](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl.h#L39-L46) —— `Initialize` 接口：`local_engine` 是 Hixl 的唯一标识（ipv4 为 `host_ip:host_port` 或 `host_ip`，ipv6 为 `[host_ip]:host_port` 或 `[host_ip]`），且注释明确「当设置 host_port 且 host_port > 0 时代表当前 Hixl 作为 server 端，需要对配置端口进行监听」——一个参数决定角色。
- [include/hixl/hixl.h:54-L67](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl.h#L54-L67) —— `RegisterMem` / `DeregisterMem`：注册内存是零拷贝传输的前提，注册后远端才能「看见」这段内存。
- [include/hixl/hixl.h:70-L114](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl.h#L70-L114) —— `Connect` / `Disconnect` / `ConnectAsync` / `GetAsyncConnectStatus` 等链路管理接口，同步与异步版本成对出现。

#### 4.3.4 代码实践

**实践 3：数一数「极简 API」到底有多少个接口**

1. 实践目标：验证 README「接口数量精简至 10 余个核心调用」的说法，并对 Hixl 类的能力分组建立直觉。
2. 操作步骤：
   - 打开 [include/hixl/hixl.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl.h)，通读整个 `Hixl` 类（文件不长）；
   - 统计所有 public 成员函数（构造/析构除外），按「生命周期 / 内存 / 链路 / 传输 / 通知」五类分组列表；
   - 对每个接口抄一句注释里的关键语义（例如 `RegisterMem`：注册成功返回内存 handle，可用于解注册）。
3. 需要观察的现象：接口总数；同步接口与异步接口是否成对；`GetAsyncConnectStatus` 是否有单个远端和批量两个重载。
4. 预期结果：一张接口分组清单。本实践为纯源码阅读型，无需编译环境；接口行为验证留到单元 2。

#### 4.3.5 小练习与答案

**练习 1**：`Initialize("192.168.1.10:9999", options)` 和 `Initialize("192.168.1.11", options)` 这两种写法的区别是什么？

<details>
<summary>参考答案</summary>

前者带端口（host_port > 0），该 Hixl 实例作为 server 端，会监听 9999 端口；后者不带端口，作为 client 端，不监听。规则见 [include/hixl/hixl.h:40-L44](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl.h#L40-L44) 的接口注释。
</details>

**练习 2**：为什么传输前必须先 `RegisterMem`？

<details>
<summary>参考答案</summary>

单边零拷贝要求发起方能直接定位并访问远端内存，这需要远端先把内存「登记」给引擎（注册时可指定内存类型），建链时双方交换注册信息，之后发起方拿到远端地址才能执行 READ/WRITE。不注册的内存对远端不可见，也无法被 DMA 类机制直接访问（参见 [include/hixl/hixl.h:54-L60](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl.h#L54-L60)；引擎内部实现将在单元 2 展开）。
</details>

### 4.4 核心组件二：LLM-DataDist（上层 KV Cache 传输）与 Python 绑定

#### 4.4.1 概念说明

**LLM-DataDist** 构建在 HIXL Engine 之上（源码 `src/llm_datadist/`，头文件 `include/llm_datadist/`），提供**带 KV Cache 语义**的数据传输接口。两者的分工：

| 维度 | HIXL Engine | LLM-DataDist |
|------|-------------|--------------|
| 抽象层次 | 底层传输引擎 | 上层业务接口 |
| 操作对象 | 裸内存（本地地址 + 远端地址） | Cache（CacheDesc、CacheIndex 等 KV Cache 概念） |
| 典型接口 | RegisterMem / Connect / Transfer | SetRole / LinkLlmClusters / AllocateCache / PushKvCache / PullKvCache |
| 目标用户 | 需要精确控制内存的框架/中间件 | vLLM、SGLang 等推理引擎 |

换句话说，HIXL Engine 让你「搬字节」，LLM-DataDist 让你「搬 KV Cache」——它把内存注册、地址寻址这些细节包掉，暴露出 Prompt/Decoder 角色、Cache 分配与注册、Push/Pull 这类贴合推理引擎心智模型的接口。

**Python 绑定**（源码 `src/python/`）是第三个组件：用 pybind11 把 HIXL 与 LLM-DataDist 的能力包装成 Python 包，让 Python 生态（vLLM/SGLang 的周边工具、Mooncake 等）可以直接调用。README 中「提供完善的 C++/Python 语言接口支持」指的就是它。

#### 4.4.2 核心流程

PD 分离场景下两个组件协作的简化视图：

```text
┌────────────────────────── 推理引擎（vLLM / SGLang） ──────────────────────────┐
│  Prompt 侧                                      Decode 侧                    │
│  LlmDataDist: SetRole / AllocateCache           LlmDataDist: SetRole          │
│  PushKvCache ──┐                                PullKvCache ◄─┐             │
└────────────────┼───────────────────────────────────────────────┼─────────────┘
                 │              （LLM-DataDist 层，KV Cache 语义） │
                 ▼                                               ▼
┌──────────────────────────── HIXL Engine 层（内存语义） ────────────────────────┐
│  RegisterMem → Connect → 单边 WRITE/READ（批量地址描述）                        │
└──────────────────────────────────┬─────────────────────────────────────────────┘
                                   ▼
┌──────────────────────── HCCS / RDMA / ... （物理链路） ─────────────────────────┐
└────────────────────────────────────────────────────────────────────────────────┘
```

Prompt 侧调用 `PushKvCache`，LLM-DataDist 把它翻译成 HIXL Engine 的单边 WRITE；Decode 侧调用 `PullKvCache`，则翻译成单边 READ。这与 docs/zh/guide/introduction.md 中「Decode 和 Prompt 可以双向拉取、推送 Cache」的描述一致。

#### 4.4.3 源码精读

- [README.md:44](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/README.md#L44) —— 官方对 LLM-DataDist 的定义：「基于 HIXL Engine 构建，提供了一套携带 KV Cache 语义的数据传输接口。可快速、灵活对接 vLLM、SGLang 等推理引擎」。
- [AGENTS.md:11-L12](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/AGENTS.md#L11-L12) —— 两个组件的实现位置：HIXL Engine 在 `src/hixl/`，LLM-DataDist 在 `src/llm_datadist/`；Python 绑定在 `src/python/`（另见 [AGENTS.md:20](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/AGENTS.md#L20)）。
- [AGENTS.md:7](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/AGENTS.md#L7) —— 「通过 pybind11 提供 Python 绑定」，点明 Python 组件的实现技术。
- [docs/README.md:34-L39](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/README.md#L34-L39) —— 接口文档入口分成 HIXL C++ 接口、HIXL_CS C 接口、LLM-DataDist C++ 接口和 LLM-DataDist Python 接口，与「多组件、多语言」的结构一一对应。
- [include/llm_datadist/llm_datadist.h:33-L42](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h#L33-L42) —— LLM-DataDist 的初始化选项，如 `OPTION_LISTEN_IP_INFO`（监听信息）、`OPTION_DEVICE_ID`（设备号）、`OPTION_TRANSFER_BACKEND`（传输后端，HIXL 即后端之一）——可以看出它的配置粒度是「推理实例」而非「裸内存」。
- [include/llm_datadist/llm_datadist.h:43-L62](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h#L43-L62) —— LLM-DataDist 自己的一套错误码（`LLM_KV_CACHE_NOT_EXIST`、`LLM_NOT_YET_LINK` 等），从名字就能看出其语义层级是 Cache 与链路，而非字节与地址。
- [docs/zh/guide/introduction.md:11](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/guide/introduction.md#L11) —— Python 场景说明：通过 Python 的 LLM-DataDist 接口在 CacheManager 模式下进行链路管理和 KV Cache 管理，支持单边建链和双边建链，支持 D2D、D2H 和 H2D。

#### 4.4.4 代码实践

**实践 4（本讲综合实践前置步骤）：对比两套头文件的「词汇表」**

1. 实践目标：从公开头文件的命名差异中，直观感受两个组件的抽象层次差异。
2. 操作步骤：
   - 打开 [include/hixl/hixl.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl.h) 和 [include/llm_datadist/llm_datadist.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h)；
   - 各挑出 8~10 个「高频名词」（类名、接口名、选项名），填入对照表，例如：
     - HIXL：`MemDesc`、`MemHandle`、`RegisterMem`、`Connect`、`Transfer`……
     - LLM-DataDist：`ListenIpInfo`、`TransferBackend`、`LLM_KV_CACHE_NOT_EXIST`、……
   - 在表格右侧写一句话：每套词汇服务于什么心智模型？
3. 需要观察的现象：HIXL 的词汇围绕「内存与链路」，LLM-DataDist 的词汇围绕「集群、角色与 Cache」。
4. 预期结果：一张双列词汇对照表。纯源码阅读型实践，无需运行环境。

#### 4.4.5 小练习与答案

**练习 1**：如果一个推理引擎想迁移到昇腾平台做 PD 分离，选 LLM-DataDist 还是直接用 HIXL Engine？为什么？

<details>
<summary>参考答案</summary>

一般选 LLM-DataDist：它提供 KV Cache 语义（Cache 分配/注册、Push/Pull），贴合推理引擎的心智模型，且官方说明可快速对接 vLLM、SGLang（见 [README.md:44](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/README.md#L44)）。只有在需要自定义内存布局、构建分布式内存池等场景（如 Mooncake 这类传输框架），才直接使用 HIXL Engine 的地址级接口（见 [docs/zh/guide/introduction.md:10](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/guide/introduction.md#L10)）。
</details>

**练习 2**：Python 绑定位于哪个源码目录？用什么技术实现？

<details>
<summary>参考答案</summary>

`src/python/`，使用 pybind11 实现（见 [AGENTS.md:7](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/AGENTS.md#L7) 与 [AGENTS.md:20](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/AGENTS.md#L20)）。
</details>

## 5. 综合实践

**任务：绘制一张 HIXL 分层架构草图，并为每个核心组件写一句职责概括。**

这是本讲规格中指定的主实践任务，纯文档/源码阅读型，无需昇腾硬件。

1. 实践目标：把本讲四个模块的知识（定位、单边零拷贝与多链路、HIXL Engine、LLM-DataDist/Python 绑定）整合到一张图上。
2. 操作步骤：
   - 通读 [README.md](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/README.md) 与 [docs/README.md](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/README.md)；
   - 对照 README「目录结构」一节（[README.md:61-L88](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/README.md#L61-L88)），在图中为每个组件标注其源码目录（`src/hixl/`、`src/llm_datadist/`、`src/python/`）和头文件目录（`include/hixl/` 等）；
   - 画出如下分层（自上而下）：**应用层（vLLM / SGLang / Mooncake）→ LLM-DataDist（KV Cache 语义）→ HIXL Engine（内存语义传输引擎）→ 物理链路（HCCS / RDMA）**，并在 LLM-DataDist 与 HIXL Engine 之间标注「调用/构建于其上」的关系；Python 绑定作为侧挂的适配层画在 LLM-DataDist 与应用之间；
   - 在图旁用自己的话为每个核心组件写一句不超过 30 字的职责概括。
3. 需要观察的现象：README 的目录结构与你画的层是否一一对应；`include/adxl/` 这个目录在 README 目录树里存在但本讲未展开——在图上打个问号标记「待后续单元确认」（它属于已废弃的旧接口体系，将在高级单元讲解）。
4. 预期结果：一张手绘或文本绘制的分层架构草图 + 四句职责概括。参考方向（请务必用自己的话重写）：
   - 应用：消费传输能力的推理/训练框架；
   - LLM-DataDist：把「搬字节」包装成「搬 KV Cache」；
   - HIXL Engine：屏蔽硬件差异，用单边零拷贝在多链路上搬字节；
   - HCCS/RDMA：实际承载数据的物理通道。

## 6. 本讲小结

- HIXL（Huawei Xfer Library）是面向昇腾芯片的单边通信库，为集群场景提供简单、可靠、高效的点对点传输，典型场景是 PD 分离中的 KV Cache 传输。
- **单边零拷贝**是核心机制：一端发起即可直接读写远端内存，远端无需参与，且数据在用户内存间直接传输，不经过中间缓冲区，支撑通信与计算重叠。
- HIXL 原生支持 **HCCS / RDMA 多链路**与 **D2D / D2H / H2D 多种内存路径**，并屏蔽 A2/A3 等不同代际芯片差异，README 记录带宽最高 119GB/s。
- 仓库包含三个组件：**HIXL Engine**（`src/hixl/`，底层内存语义引擎）、**LLM-DataDist**（`src/llm_datadist/`，KV Cache 语义上层接口，对接 vLLM/SGLang）、**Python 绑定**（`src/python/`，pybind11）。
- HIXL Engine 的公开入口是单一 `hixl::Hixl` 类，接口按「生命周期 / 内存 / 链路 / 传输 / 通知」分组；`Initialize` 的 `local_engine` 参数是否带端口决定了 server/client 角色。

## 7. 下一步学习建议

- **下一讲（u1-l2）**：《构建与测试：从源码到可执行样例》——学习 CANN 环境准备、`build.sh` 编译流程和 `tests/run_test.sh` 测试执行，为后续所有动手实践做准备。
- **延伸阅读（本讲范围内）**：
  - [docs/README.md](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/README.md) 的技术文章列表，特别是《HIXL：昇腾生态KV传输性能优化利器》与 SGLang+Mooncake+HIXL PD 分离实践两篇，能加深对业务场景的理解；
  - [docs/zh/guide/README.md](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/guide/README.md) 的开发指南目录，了解 C++/Python 两条学习线。
- **提前预习性源码**（下一单元会精读）：[include/hixl/hixl.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl.h) 与 [include/hixl/hixl_types.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl_types.h)，重点关注 `MemDesc`、`TransferOp` 等数据结构的定义。
