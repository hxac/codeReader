# 3FS 是什么：项目总览与设计理念

## 1. 本讲目标

本讲是整本学习手册的第一篇，目标是让你在 **不看任何一行代码细节** 的前提下，建立起对 3FS（Fire-Flyer File System）的全局认知。读完本讲，你应该能够：

- 说清楚 3FS 要解决什么问题、服务于哪些典型的 AI 工作负载；
- 记住 3FS 的四大组件（cluster manager、metadata service、storage service、client），以及它们各自的一句话职责；
- 理解 3FS 两个最关键的设计取舍：为什么用 **CRAQ 强一致复制**、为什么用 **无状态元数据服务 + 事务型 KV 存储**；
- 读懂 README 中给出的几个性能数字，并理解它们背后集群的规模。

本讲只引用两个文档文件：`README.md` 与 `docs/design_notes.md`。它们是 3FS 的「门面」与「设计自白书」，后续每一篇讲义都会回到这里寻找依据。

## 2. 前置知识

本讲面向零基础读者，但有几个名词最好先有个直觉，没有也没关系，我们随讲随补：

- **分布式文件系统**：把文件分散存到很多台机器上，但对使用者来说就像用本地磁盘一样。常见的例子有 HDFS、CephFS。
- **SSD 与 NVMe**：SSD 是固态硬盘，NVMe 是一种高速访问 SSD 的协议。AI 训练的数据量极大，普通硬盘根本喂不饱 GPU，所以 3FS 的目标是「榨干 SSD 的带宽」。
- **RDMA / InfiniBand / RoCE**：RDMA（Remote Direct Memory Access）是一种「绕过对方 CPU、直接读写对方内存」的高速网络技术。InfiniBand（IB）和 RoCE 是承载 RDMA 的两种网络。你可以把它理解为「比普通 TCP 快一个数量级、且延迟极低的网络」。
- **复制（Replication）**：把一份数据存多份（通常 3 份），这样坏一台机器数据还在。难点在于多份之间如何保持一致。
- **事务型 KV 存储（Transactional KV Store）**：一种像「大字典」一样的数据库，key 查 value，且支持事务（一组操作要么全做、要么全不做）。3FS 用的是 FoundationDB（简称 FDB）。
- **FUSE**：Filesystem in Userspace，让你在「用户态」写一个文件系统，而不用改操作系统内核。3FS 的客户端主要就是以 FUSE 守护进程的形式运行。

如果你对上面某些词还陌生，完全没关系——本讲会用通俗的方式再次解释。

## 3. 本讲源码地图

本讲不深入代码实现，只读两个文档，它们是理解整个项目的「地图」：

| 文件 | 作用 | 本讲用它来 |
| :--- | :--- | :--- |
| `README.md` | 项目首页：定位、特性、性能指标、构建与运行入口 | 了解 3FS 解决的问题、工作负载、性能数字 |
| `docs/design_notes.md` | 设计自白书：四大组件、CRAQ、元数据存储、数据放置、故障恢复 | 理解架构、组件职责与核心设计取舍 |

后续讲义会逐步打开 `src/` 下的源码，本讲先把这两份文档读透。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**项目背景**、**组件划分**、**性能指标**。

### 4.1 项目背景

#### 4.1.1 概念说明

3FS 的全称是 **Fire-Flyer File System（萤火虫文件系统）**，是 DeepSeek 开源的一套**面向 AI 训练与推理的高性能分布式文件系统**。README 第一句就给出了它的定位：

> The Fire-Flyer File System (3FS) is a high-performance distributed file system designed to address the challenges of AI training and inference workloads.

它的核心思路是：利用**现代 SSD 的吞吐**和 **RDMA 网络的带宽**，构建一个**共享存储层**，让上层应用不必关心「数据在哪个节点上」也能高速读写。这种「不关心数据位置」的访问方式，文档里叫做 **locality-oblivious（位置无关）**——这是 3FS 区别于很多「计算与存储绑定」系统的关键。

要理解 3FS 为什么存在，先看它服务的四类典型工作负载（README 称之为 Diverse Workloads）：

1. **Data Preparation（数据准备）**：数据分析流水线的产物，被组织成层次化目录，方便管理大量中间结果。
2. **Dataloaders（数据加载）**：训练时需要对样本做随机访问。3FS 让多个计算节点能**随机读取**训练样本，省去了预先「切分 + 打乱」数据集的麻烦。
3. **Checkpointing（检查点）**：大规模训练时需要**高吞吐并行**地保存模型检查点。
4. **KVCache for Inference（推理 KV 缓存）**：大模型推理时缓存已生成 token 的 key/value 向量。3FS 提供了一种**比 DRAM 更便宜、容量大得多**的替代方案。

这四类负载有一个共同点：**既要高吞吐，又要能被很多机器同时访问同一份数据**。这正是分布式文件系统擅长、而单机 SSD 做不到的场景。

#### 4.1.2 核心流程

3FS 的「项目背景」可以用一句话流程概括：

```
现代 SSD 吞吐很高 + RDMA 网络带宽很大
        ↓
把成百上千块 SSD、成百上千台存储节点的带宽聚合起来
        ↓
形成一个「位置无关」的共享存储层
        ↓
支撑 AI 训练/推理的四类典型负载
```

关键的「聚合」思想体现在 README 对 **Disaggregated Architecture（分离式架构）** 的描述：它把「数千块 SSD 的吞吐」和「数百个存储节点的网络带宽」组合到一起。 disaggregated（分离）的含义是：存储资源不再依附于某个计算节点，而是独立成一个池，任何计算节点都能访问。

#### 4.1.3 源码精读

先看 README 开头对 3FS 的整体定义与三大特性：

[README.md:6](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/README.md#L6) —— 给出 3FS 的定位：面向 AI 训练/推理的高性能分布式文件系统，基于现代 SSD 与 RDMA 网络，提供共享存储层。

[README.md:8-11](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/README.md#L8-L11) —— 列出三大特性。其中：

- 第 9 行 **Disaggregated Architecture**：聚合数千块 SSD 与数百节点的带宽，应用可「位置无关」地访问存储。
- 第 10 行 **Strong Consistency**：用 CRAQ 实现强一致，让应用代码简单、易推理。
- 第 11 行 **File Interfaces**：用事务型 KV 存储（如 FoundationDB）支撑无状态元数据服务；文件接口人人会用，无需学新 API。

再看四类工作负载：

[README.md:13-17](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/README.md#L13-L17) —— 依次说明 Data Preparation、Dataloaders、Checkpointing、KVCache for Inference 四类负载。

#### 4.1.4 代码实践（源码阅读型）

> 本讲是入门篇，不要求你运行任何命令；实践以「读 + 画 + 写」为主。

**实践目标**：把 README 里的「特性」和「工作负载」对应起来。

**操作步骤**：

1. 打开 `README.md`，定位到第 8–17 行。
2. 画一张两列表格：左列是三大特性（Disaggregated / Strong Consistency / File Interfaces），右列写「这个特性主要服务于哪类工作负载」。
3. 例如：Dataloaders 的随机读最依赖哪两个特性？（提示：位置无关访问 + 高吞吐。）

**需要观察的现象**：你会发现四类负载并不是平均地依赖三个特性——有些负载更吃吞吐，有些更吃「文件接口」带来的易用性。

**预期结果**：你能用自己的话说出「3FS 的每个设计选择，背后都对应着一类真实的 AI 负载需求」。

#### 4.1.5 小练习与答案

**练习 1**：3FS 名字里的「Fire-Flyer」和它服务的场景有什么关系？（开放题）

**参考答案**：「Fire-Flyer」是 DeepSeek 自家 AI 训练硬件/集群的代号（萤火虫，寓意大量小个体聚成强大整体）。3FS 正是为这类大规模 AI 训练/推理集群设计的存储底座，名字点明了它的服务对象。

**练习 2**：KVCache for Inference 为什么说 3FS 是「DRAM 的替代」？

**参考答案**：推理时 KVCache 通常放在 GPU/CPU 的 DRAM 里，容量有限且昂贵。3FS 把 KVCache 落到由大量 SSD 组成的分布式文件系统上，容量远大于单机 DRAM、成本低得多，同时还能提供高吞吐读，因此是一种高性价比的替代方案（见 `README.md` 第 17 行）。

---

### 4.2 组件划分

#### 4.2.1 概念说明

3FS 由 **四个组件** 构成，它们全部连接在同一张 RDMA 网络（InfiniBand 或 RoCE）上。`design_notes.md` 开篇就给出了这张「全家福」：

> The 3FS system has four components: cluster manager, metadata service, storage service and client.

下面用一张表先建立直觉（名词先用一句话解释，细节在后续讲义展开）：

| 组件 | 中文名 | 一句话职责 |
| :--- | :--- | :--- |
| **cluster manager** | 集群管理器 | 集群的「管家」：维护成员关系、分发集群配置（谁在线、链表长什么样）。 |
| **metadata service** | 元数据服务 | 文件系统的「大脑」：处理 open/create 等操作，实现文件语义。**无状态**，元数据全存 FoundationDB。 |
| **storage service** | 存储服务 | 文件数据的「仓库」：管理本地若干 SSD，提供 chunk 存储接口，用 CRAQ 保证强一致。 |
| **client** | 客户端 | 应用的「入口」：分 FUSE 客户端（易用）和原生客户端（高性能）。 |

#### 4.2.2 核心流程

四个组件之间通过 **心跳（heartbeat）** 和 **配置分发** 协作，整体流程如下：

```
        ┌───────────────────────┐
        │   cluster manager     │   ← 多实例部署，选一个当 primary
        │  （集群管家/配置中心）   │
        └───────┬───────┬───────┘
      心跳上报  │       │  下发集群配置(routing info)
   ┌───────────┘       └──────────────┐
   ↓                                  ↓
┌──────────────┐   ┌────────────────────────┐
│ metadata svc │   │     storage service    │
│  （无状态）    │   │  管理 SSD + CRAQ 复制   │
│ 元数据在 FDB  │   │  文件被切成 chunk 多副本 │
└──────┬───────┘   └───────────┬────────────┘
       │                       │
       │   client 联系 meta 拿「文件布局」   │
       │   client 直连 storage 读写「数据」  │
       └───────────┬───────────┘
                   ↓
            ┌────────────┐
            │   client   │  ← FUSE 客户端 / 原生客户端
            │  （应用入口）│
            └────────────┘
```

关键协作点：

1. **心跳 = 续租**：metadata 和 storage 服务周期性地向 cluster manager 发心跳。cluster manager 据此感知成员的在线状态，并把最新的集群配置（路由信息）分发给各方。
2. **元数据操作走 meta**：应用做 open/create 等操作时，client 联系 metadata service；因为 meta 无状态，client 可以连任意一个 meta 实例。
3. **数据读写直连 storage**：拿到文件布局后，client 自己就能算出 chunk 落在哪些 storage 上，直接和 storage 通信，**不再让 meta 介入数据热路径**。
4. **cluster manager 的高可用**：多个 cluster manager 实例部署，选举一个当 primary；primary 挂了，另一个会被提升为新 primary。集群配置存在可靠的外部存储里（生产环境直接复用 FoundationDB，以减少依赖）。

#### 4.2.3 源码精读

[docs/design_notes.md:5](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/docs/design_notes.md#L5) —— 明确四大组件，且都连在 RDMA 网络（InfiniBand 或 RoCE）上。

[docs/design_notes.md:7](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/docs/design_notes.md#L7) —— 讲 cluster manager：metadata/storage 发心跳；manager 处理成员变更并分发配置；多实例选 primary，primary 故障则提升另一个；配置存于可靠的分布式协调服务（生产环境复用文件元数据同款 KV 存储，即 FDB）。

[docs/design_notes.md:9](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/docs/design_notes.md#L9) —— 讲 metadata service：处理 open/create 等元数据操作，实现文件系统语义；**无状态**，因为元数据存在事务型 KV 存储（如 FDB）里；client 可连任意 meta 实例。

[docs/design_notes.md:11](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/docs/design_notes.md#L11) —— 讲 storage service：管理若干本地 SSD，提供 chunk store 接口；用 CRAQ 保证强一致；CRAQ 的 **write-all-read-any（写全读任意）** 能充分释放 SSD 与 RDMA 的吞吐；一个 3FS 文件被切成等大的 chunk，在多块 SSD 上多副本存储。

[docs/design_notes.md:13](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/docs/design_notes.md#L13) —— 讲 client：提供两种客户端，FUSE 客户端（上手门槛低，多数应用用）与原生客户端（性能敏感的应用用）。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：理解「为什么 metadata service 要做成无状态」。

**操作步骤**：

1. 阅读 `docs/design_notes.md` 第 9 行与第 61–63 行（File metadata store 一节开头）。
2. 在文档中找出「stateless（无状态）」出现的几处，记录每处给出的理由。
3. 思考：如果 meta 服务是有状态的，client 在某个 meta 实例宕机时会遇到什么麻烦？

**需要观察的现象**：文档把「无状态」和「可无缝升级/重启」「请求失败可自动切换到其他实例」直接挂钩。

**预期结果**：你能写出一句话——「元数据全部交给 FDB 这类事务存储，meta 服务自己不持有状态，所以可以随便重启、随便扩缩容、随便故障切换」。

#### 4.2.5 小练习与答案

**练习 1**：client 读写文件数据时，要不要经过 metadata service？为什么？

**参考答案**：**通常不需要**。client 在 open 时从 meta 拿到文件布局（layout），之后就能自己算出 chunk id 与所在的 chain，直接和 storage 通信读写。这种设计把 meta 排除在「数据热路径」之外，避免 meta 成为吞吐瓶颈（见 `docs/design_notes.md` 第 59 行）。

**练习 2**：cluster manager 的集群配置存在哪里？为什么 3FS 生产环境要这样选？

**参考答案**：通常存在可靠的分布式协调服务（如 ZooKeeper/etcd）；但 3FS 生产环境选择**复用存文件元数据的同一个事务型 KV 存储（FoundationDB）**，目的是减少外部依赖、简化运维（见 `docs/design_notes.md` 第 7 行）。

---

### 4.3 性能指标

#### 4.3.1 概念说明

光说「高性能」不够，README 给出了三个有说服力的 benchmark 数字。理解这些数字的关键，是先看懂**集群规模**——3FS 的吞吐是靠「堆机器 + 堆 SSD + 堆带宽」线性扩展出来的。

需要先建立的几个量纲直觉：

- **TiB/s**：每秒 TiB 量级的数据吞吐。1 TiB ≈ 1024 GiB。6.6 TiB/s 是非常夸张的聚合吞吐。
- **Gbps**：网络带宽单位（每秒吉比特）。200 Gbps 的 IB 网卡是高端 AI 集群的标配。
- **TiB/min**：GraySort 这种排序任务的吞吐单位（每分钟处理多少 TiB）。
- **IOPS**：每秒 IO 操作数，常用来衡量小请求（如 KVCache 的 GC 删除）的处理能力。

#### 4.3.2 核心流程

三个 benchmark 的逻辑可以归纳为「**规模 → 吞吐**」：

```
1. 峰值读吞吐（Peak throughput）
   集群：180 个存储节点 × (2×200Gbps IB 网卡 + 16 块 14TiB NVMe SSD)
   客户端：500+ 个节点 × 1×200Gbps IB 网卡
   结果：聚合读吞吐 ≈ 6.6 TiB/s（且伴有训练后台流量）

2. GraySort（大规模排序）
   集群：25 个存储节点 + 50 个计算节点
   任务：对 110.5 TiB 数据分 8192 个分区排序
   结果：30 分 14 秒完成，平均吞吐 3.66 TiB/min

3. KVCache（推理缓存）
   结果：读吞吐峰值达 40 GiB/s（单节点 1×400Gbps 网卡）
```

这里有一个贯穿全文的「线性扩展」目标：3FS 的读写吞吐应当**随 SSD 数量和 client↔storage 之间的对分网络带宽（bisection bandwidth）线性增长**。上面 180 节点跑出 6.6 TiB/s，正是这个目标的体现。

#### 4.3.3 源码精读

[README.md:28-34](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/README.md#L28-L34) —— Peak throughput：180 个存储节点（每节点 2×200Gbps IB 网卡 + 16 块 14TiB NVMe SSD），500+ 客户端节点，聚合读吞吐约 **6.6 TiB/s**；并指向 `benchmarks/fio_usrbio` 这个基于 USRBIO 的 fio 引擎用于压测。

[README.md:36-43](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/README.md#L36-L43) —— GraySort：用 smallpond 在 25 存储节点 + 50 计算节点的集群上，对 **110.5 TiB** 数据分 8192 区排序，**30 分 14 秒**完成，平均 **3.66 TiB/min**。

[README.md:45-51](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/README.md#L45-L51) —— KVCache：读吞吐峰值达 **40 GiB/s**，同时给出 GC（remove ops）的 IOPS 曲线，说明 3FS 既能高吞吐读、也能扛住大量小删除。

再看设计目标中关于「线性扩展」的表述：

[docs/design_notes.md:99-101](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/docs/design_notes.md#L99-L101) —— chunk 存储系统的设计目标：即使出现存储介质故障，也要追求尽可能高的带宽；3FS 的读写吞吐应**随 SSD 数量与 client↔storage 对分带宽线性扩展**；应用以「位置无关」的方式访问。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：把「集群规模」和「吞吐数字」对应起来，体会「线性扩展」。

**操作步骤**：

1. 重读 `README.md` 第 30 行，记录三个数字：存储节点数（180）、每节点 SSD 数（16）、聚合吞吐（6.6 TiB/s）。
2. 估算「平均每块 SSD 贡献了多少读吞吐」：\( \frac{6.6 \text{ TiB/s}}{180 \times 16} \approx 2.3 \text{ GiB/s} \) 每块 SSD 量级。（这是粗略估算，实际还受网络、副本读分摊等影响，**待本地验证**。）
3. 对比 GraySort 的集群规模（25 节点）与吞吐（3.66 TiB/min），感受「节点少则吞吐小」的线性关系。

**需要观察的现象**：吞吐大致与「SSD 总数 × 单盘带宽」成正比，这正是「分离式架构 + 线性扩展」想要的结果。

**预期结果**：你能解释为什么 3FS 强调「aggregating the throughput of thousands of SSDs」——因为单盘吞吐有上限，唯有靠数量堆出总量。

#### 4.3.5 小练习与答案

**练习 1**：6.6 TiB/s 是在「纯净环境」下测得的吗？

**参考答案**：**不是**。README 明确说明该读压测是在「with background traffic from training jobs（带有训练任务的后台流量）」的条件下测得的（见 `README.md` 第 30 行），说明这个数字更贴近真实生产场景。

**练习 2**：为什么 KVCache 的 benchmark 除了「读吞吐」还要专门画一张「GC IOPS」图？

**参考答案**：KVCache 场景下会不断有过期缓存被删除，产生大量小粒度的 remove 操作。读吞吐再高，如果 GC（删除）跟不上，系统也会被拖垮。所以同时给出「读吞吐峰值 40 GiB/s」和「GC IOPS」两张图，是为了证明 3FS 在 KVCache 场景下「读得快、也删得动」（见 `README.md` 第 47–48 行）。

---

## 5. 综合实践

本讲的综合实践把你刚学到的「组件划分」和「设计取舍」串起来。请完成下面两件事：

### 任务一：画出四大组件关系图

1. 准备一张白纸或绘图工具。
2. 画出 **cluster manager、metadata service、storage service、client** 四个方框。
3. 用带箭头的连线标注它们之间的关键交互，至少包含：
   - metadata / storage 向 cluster manager 发**心跳**；
   - cluster manager 向各方**下发集群配置（routing info）**；
   - client 向 metadata service 请求**文件布局**（open/create）；
   - client 直连 storage service **读写数据**。
4. 在图边注明：所有组件都跑在 **RDMA 网络** 上。

> 提示：可参考本讲 4.2.2 节的示意图，但请用**自己的话**重新画一遍，并标注每条线代表「谁主动找谁、传了什么」。

### 任务二：写一段 200 字说明——为什么 3FS 选择文件接口而非对象存储？

阅读 `docs/design_notes.md` 第 15–23 行（File system interfaces 一节），结合下面三个论点，用自己的话写一段约 200 字的中文说明：

- **原子目录操作**（atomic directory manipulation）：对象存储只能用 key 里的 `/` 模拟目录，无法原子地移动/递归删除目录；3FS 内部应用常见「建临时目录 → 写文件 → 整体移到目标位置」的模式，且小文件多时递归删除至关重要。
- **符号链接 / 硬链接**（symbolic and hard links）：用来为动态更新的数据集做轻量快照。
- **熟悉的接口**（familiar interface）：文件接口人人会用，无需学新 API；很多数据集本就是 CSV/Parquet 文件，迁移成本低。

**预期产物**：一张组件关系图 + 一段 200 字说明。完成后，你就建立了 3FS 的全局认知，可以进入下一篇讲义了。

## 6. 本讲小结

- 3FS 是 DeepSeek 开源的**面向 AI 训练/推理的高性能分布式文件系统**，靠聚合大量 SSD 与 RDMA 带宽提供「位置无关」的共享存储层。
- 它服务于四类典型负载：**数据准备、Dataloader、Checkpoint、推理 KVCache**。
- 它由 **四大组件** 构成：cluster manager（管家/配置中心）、metadata service（无状态元数据）、storage service（SSD + CRAQ 复制）、client（FUSE / 原生）。
- 两个核心设计取舍：**CRAQ 强一致复制**（write-all-read-any，充分释放吞吐）与**无状态元数据 + 事务型 KV 存储（FoundationDB）**（可无缝升级、自动故障切换）。
- 性能上追求**线性扩展**：180 节点读压测约 **6.6 TiB/s**；GraySort **110.5 TiB / 30 分 14 秒**；KVCache 峰值读 **40 GiB/s**。
- 本讲只读了两个文档（`README.md`、`docs/design_notes.md`），它们是后续所有源码讲义的「地图」。

## 7. 下一步学习建议

本讲建立了全局视图，下一步建议：

1. **先认识仓库与构建**：阅读下一篇讲义 `u1-l2-repo-and-build.md`（仓库结构与构建系统），学会如何把 3FS 编译出来。
2. **再动手部署**：接着看 `u1-l3-deploy-and-admin-cli.md`，用 `deploy/README.md` 搭一个测试集群，跑通 `admin_cli`。
3. **建立端到端链路图**：然后读 `u1-l4-end-to-end-flow.md`，把 open/read/write 经过哪些组件串起来。
4. **带着问题进入源码**：当你能回答「client 读写数据为什么不用经过 meta」时，就可以进入第二单元（公共基础设施）和第三到五单元（mgmtd / meta / storage 的源码精读）了。

建议在本讲结束时，确认自己能脱口而出「四大组件 + 两个设计取舍 + 三个性能数字」，再继续往下学。
