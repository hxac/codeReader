# 部署一个测试集群与 admin_cli

## 1. 本讲目标

学完本讲后，你应当能够：

1. 说出 3FS 测试集群里 **monitor / admin_cli / mgmtd / meta / storage / FUSE client** 这几类进程各自的职责，并能解释为什么它们必须按固定顺序启动。
2. 看懂 3FS「三类配置文件」的设计——`*_launcher.toml`（引导配置）、`*_app.toml`（节点身份）、`*_main.toml`（运行时配置），并理解为什么运行时配置要由 mgmtd 统一托管、用 `admin_cli set-config` 上传。
3. 理解 `admin_cli` 这个运维工具的内部结构：它如何把一行文本命令派发到具体 handler，为什么能用 `< file` 批量喂命令，以及为什么各类 client 都是「懒构造」的。
4. 亲手走通 `init-cluster` 这一步，并能用 `list-nodes / list-chains / list-chain-tables` 验证集群状态。

本讲是「从零上手」阶段的收尾篇。它不要求你理解任何服务的内部实现，只要求你建立一张全景图：**集群由哪些进程组成、它们如何发现彼此、配置从哪里来**。后续单元（u2~u8）才会带你进入各服务的源码内部。

## 2. 前置知识

动手前请先确认以下概念（前两讲已铺垫的会简略带过）：

- **服务进程（service）**：3FS 把不同职责拆成独立进程，如 `mgmtd_main`、`meta_main`、`storage_main`、`hf3fs_fuse_main`，它们都是上一讲 `build/bin` 下编译出的二进制。
- **节点（node）与 NodeID**：每个加入集群的进程有一个全局唯一的 `node_id`（mgmtd 用 `1`、meta 用 `100`、storage 用 `10001~10005`）。NodeID 是路由与心跳的基本单位。
- **FoundationDB（FDB）**：分布式事务型 KV 存储。3FS 用它做两件事：① 存 meta 的文件元数据；② 存 mgmtd 的集群路由信息与托管配置。`fdb.cluster` 文件描述如何连上 FDB。
- **ClickHouse**：列式数据库，3FS 用它存监控指标。本讲只在「建监控表」一步用到它。
- **systemd**：Linux 服务管理器。3FS 为每个进程提供一个 `.service` 单元，用 `systemctl start xxx` 拉起。
- **RDMA / RoCE**：远程直接内存访问，绕过 CPU 拷贝的高速网络。3FS 服务间默认走 `RDMA://` 地址（也支持 TCP）。本讲把 `RDMA://192.168.1.1:8000` 当作 mgmtd 地址常量即可。

> 名词速查：`chain table`（链表）、`chain`（链）、`target`（存储目标）是 3FS 数据放置的三层结构。本讲你只需知道「建集群最后一步要创建一批 target、把它们组成 chain、再把 chain 组织成 chain table」；完整数据模型留给 u3-l4。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [deploy/README.md](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/README.md) | 官方部署手册，本讲的「脚本」。含硬件规格、服务清单、Step 0~8、FAQ。 |
| [configs/mgmtd_main_launcher.toml](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/configs/mgmtd_main_launcher.toml) | mgmtd 的引导配置（cluster_id、fdb.clusterFile、IB 设备）。 |
| [configs/storage_main.toml](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/configs/storage_main.toml) | storage 服务的运行时配置（监控、线程池、target 路径、KV 存储参数）。 |
| [configs/hf3fs_fuse_main_launcher.toml](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/configs/hf3fs_fuse_main_launcher.toml) | FUSE 客户端的引导配置（cluster_id、mountpoint、token_file、mgmtd 地址）。 |
| [deploy/systemd/mgmtd_main.service](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/systemd/mgmtd_main.service) | mgmtd 的 systemd 单元，展示 `--launcher_cfg` / `--app-cfg` 传参。 |
| [deploy/systemd/storage_main.service](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/systemd/storage_main.service) | storage 的 systemd 单元，含 RDMA 需要的 `LimitMEMLOCK=infinity`。 |
| [src/client/bin/admin_cli.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/bin/admin_cli.cc) | `admin_cli` 二进制的 `main`，懒构造各类 client 并派发命令。 |
| [src/client/cli/admin/registerAdminCommands.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/registerAdminCommands.cc) | 集中注册所有 admin 子命令的入口。 |
| [src/client/cli/admin/InitCluster.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/InitCluster.cc) | `init-cluster` 实现：写根目录布局 + 各服务初始配置。 |
| [src/client/cli/admin/ListNodes.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/ListNodes.cc) | `list-nodes` 实现：从 mgmtd 拉路由信息打印节点表。 |
| [src/client/cli/admin/SetConfig.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/SetConfig.cc) | `set-config` 实现：把一份配置文本上传给 mgmtd。 |
| [src/client/cli/common/Dispatcher.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/common/Dispatcher.cc) | 命令派发核心：单条、`;` 批量、交互式 REPL 三种模式。 |

> 注意区分：仓库里还有一个 [src/tools/admin.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/tools/admin.cc)，那是带 `--set_dir_layout` / `--create_with_layout` 旧式 flag 的精简工具。**部署手册里用的 `admin_cli` 是 `src/client/bin/admin_cli.cc`**，它才支持 `init-cluster`、`list-nodes` 等完整命令集。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 部署步骤**、**4.2 配置托管**、**4.3 admin_cli**。

### 4.1 部署步骤：六节点集群的启动顺序

#### 4.1.1 概念说明

3FS 是一个由多种进程协作的分布式系统。部署它的核心难点不是「装软件」，而是「**让这些进程按正确顺序被发现、被配置**」。

部署手册（[deploy/README.md](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/README.md)）以一个 `cluster_id = stage` 的六节点集群为例（[deploy/README.md:3](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/README.md#L3)），节点角色如下：

| Node | 角色 | IP | 关键资源 |
|------|------|----|---------|
| meta | 控制 + 元数据 | 192.168.1.1 | FDB、ClickHouse、mgmtd、meta、FUSE 客户端 |
| storage1~5 | 纯数据 | 192.168.1.2~.6 | 各 16 块 SSD、storage 服务 |

完整硬件规格见 [deploy/README.md:7-16](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/README.md#L7-L16)。注意 meta 节点没有 SSD（不存数据），storage 节点不跑 FDB——这是一种典型的「**计算/控制与存储分离**」布局。

为什么要有严格的启动顺序？因为进程之间存在**启动期依赖**：

```
FDB / ClickHouse(外部)  ←  所有 3FS 进程都依赖 FDB；监控依赖 ClickHouse
        │
   monitor_collector     ←  其余进程要把指标上报给它
        │
      admin_cli          ←  纯客户端工具，随时可装
        │
       mgmtd             ←  集群「目录服务」，必须先 init-cluster 并启动
        │
    meta / storage       ←  启动后向 mgmtd 注册、心跳续租
        │
 建 target/chain/chain_table  ←  数据放置拓扑，要有 storage 节点才能建
        │
     FUSE client         ←  要拿到完整路由信息才能挂载
```

一句话：**mgmtd 是全集群的「发现服务」，谁要加入集群都得先找到它**；而 mgmtd 自己又要先被 `init-cluster` 初始化（往 FDB 写入根目录布局和各服务初始配置）才能工作。

#### 4.1.2 核心流程

部署手册把整个过程切成 Step 0~8。用伪流程概括（命令均来自手册原文）：

```
Step 0  构建 3FS，产物在 build/bin（见上一讲）
Step 1  在 ClickHouse 建监控表：clickhouse-client -n < deploy/sql/3fs-monitor.sql
Step 2  部署 monitor_collector（meta 节点），填 ClickHouse 连接，systemctl 启动
Step 3  在所有节点装 admin_cli + fdb.cluster，设置 cluster_id="stage"
Step 4  部署 mgmtd（meta 节点）
          4a. admin_cli init-cluster --mgmtd ... 1 1048576 16   ← 关键！初始化集群
          4b. systemctl start mgmtd_main
          4c. admin_cli list-nodes 验证
Step 5  部署 meta：set-config 上传 meta_main.toml → systemctl start meta_main → list-nodes
Step 6  在 5 个 storage 节点：格式化 SSD → set-config 上传 storage_main.toml → 启动 → list-nodes
Step 7  创建 admin 用户、storage targets、chains、chain table（用 data_placement 脚本生成命令）
Step 8  部署 FUSE 客户端：set-config 上传 hf3fs_fuse_main.toml → 启动 → 检查 mount
```

其中 Step 4a 的 `init-cluster` 是整个部署的「点睛之笔」。手册给出命令并解释了三个位置参数（[deploy/README.md:144-154](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/README.md#L144-L154)）：

```
init-cluster --mgmtd /opt/3fs/etc/mgmtd_main.toml  1  1048576  16
                                             ↑       ↑     ↑      ↑
                                           链表ID  chunk   stripe
                                                   size    size
```

- `1`：chain table ID（链表编号）。
- `1048576`：chunk size = 1 MiB（文件被切成多大一块）。
- `16`：stripe size（条带大小，决定数据如何跨链打散）。

这三个数字会被写进**根目录的 Layout**（文件布局），是整个文件系统的「出厂参数」，详见 4.1.3 节源码。

> 注意 `init-cluster` 是**弱幂等**操作。手册 FAQ 明确警告：如果之后改了 `mgmtd_main.toml` 导致 mgmtd 起不来，需要**清空全部 FoundationDB 数据再重跑 `init-cluster`**（[deploy/README.md:345-350](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/README.md#L345-L350)）。所以这一步要一次做对。

#### 4.1.3 源码精读

**① 服务清单：哪个二进制配哪些配置文件**

手册在 Step 0 给出一张总表（[deploy/README.md:45-52](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/README.md#L45-L52)），揭示了「一个服务 = 一个二进制 + 多份配置」的结构。以 mgmtd 为例，它需要三份配置：

```
mgmtd_main_launcher.toml   ← 引导（cluster_id、fdb.clusterFile）
mgmtd_main.toml            ← 运行时（监控、线程池、mgmtd 业务参数）
mgmtd_main_app.toml        ← 节点身份（node_id = 1）
fdb.cluster                ← 如何连上 FoundationDB
```

**② systemd 如何拉起服务**

systemd 单元展示了二进制的启动参数。mgmtd 的单元（[deploy/systemd/mgmtd_main.service:7-9](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/systemd/mgmtd_main.service#L7-L9)）：

```ini
[Service]
LimitNOFILE=1000000
ExecStart=/opt/3fs/bin/mgmtd_main --launcher_cfg /opt/3fs/etc/mgmtd_main_launcher.toml --app-cfg /opt/3fs/etc/mgmtd_main_app.toml
Type=simple
```

启动时显式传入 `--launcher_cfg`（引导配置）和 `--app-cfg`（节点身份）。三种服务的单元差异值得对比：

| 单元 | 特有配置 | 原因 |
|------|---------|------|
| [mgmtd_main.service:8](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/systemd/mgmtd_main.service#L8) | `--launcher_cfg` + `--app-cfg` | 服务有节点身份 |
| [storage_main.service:7-10](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/systemd/storage_main.service#L7-L10) | 额外有 `LimitMEMLOCK=infinity`、`TimeoutStopSec=5m` | RDMA 需要锁定内存页（pin memory） |
| [hf3fs_fuse_main.service:7-8](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/systemd/hf3fs_fuse_main.service#L7-L8) | 只传 `--launcher_cfg` | 客户端没有「节点身份」概念 |

`LimitMEMLOCK=infinity` 这个细节很关键：RDMA 传输要求发送/接收缓冲区驻留在物理内存（不能被换出），所以需要解除 memlock 限制。这种「launcher + app 两阶段配置」的统一骨架会在 u2-l1 详细展开。

**③ `init-cluster` 到底往 FDB 里写了什么**

这是本模块最重要的源码。`init-cluster` 的参数解析（[src/client/cli/admin/InitCluster.cc:32-44](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/InitCluster.cc#L32-L44)）：

```cpp
auto getParser() {
  argparse::ArgumentParser parser("init-cluster");
  parser.add_argument("chaintableid").scan<'u', uint32_t>();
  parser.add_argument("chunksize").scan<'u', uint32_t>();
  parser.add_argument("stripesize").scan<'u', uint32_t>();
  parser.add_argument("--mgmtd", "--mgmtd-config-path");
  parser.add_argument("--meta", "--meta-config-path");
  parser.add_argument("--storage", "--storage-config-path");
  parser.add_argument("--fuse", "--fuse-config-path");
  ...
}
```

它做两件事。第一件是**初始化文件系统的根目录布局**（[src/client/cli/admin/InitCluster.cc:46-70](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/InitCluster.cc#L46-L70)）：

```cpp
auto chainAlloc = std::make_unique<meta::server::ChainAllocator>(nullptr);
auto rootLayout = meta::Layout::newEmpty(tableId, chunksize, stripesize);
auto op = meta::server::MetaStore::initFileSystem(*chainAlloc, rootLayout);
auto handler = [&](kv::IReadWriteTransaction &txn) -> CoTryTask<void> {
  co_return co_await op->run(txn);
};
auto commitRes = co_await kv::WithTransaction(getRetryStrategy())
                     .run(env.kvEngineGetter()->createReadWriteTransaction(), std::move(handler));
```

它构造一个空的根目录 `Layout`（由 tableId/chunksize/stripesize 描述），在一个 FDB 读写事务里写下去。注意 `kv::WithTransaction(...).run(...)` 这个包装：它带了一个重试策略（[InitCluster.cc:30](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/InitCluster.cc#L30) 的 `FDBRetryStrategy({1_s, 10, false})`，即首等 1 秒、最多重试 10 次），事务冲突会自动重试——这是 3FS 操作 FDB 的标准写法，u2-l6 会专题讲解。

第二件是**把各服务的初始配置写入 FDB**（[src/client/cli/admin/InitCluster.cc:119-175](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/InitCluster.cc#L119-L175)）。`handleInitCluster` 依次对 `--mgmtd/--meta/--storage/--fuse` 指定的配置文件调用 `handleInitConfig`，把它们作为版本号 1 的初始配置存进 mgmtd 的 store（[InitCluster.cc:133-172](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/InitCluster.cc#L133-L172)）。这就是「**配置托管**」的起点——下一节详述。

#### 4.1.4 代码实践

> **实践目标**：在阅读层面走通 `init-cluster`，确认它写了哪两类数据。

**操作步骤（源码阅读型）**：

1. 打开 [src/client/cli/admin/InitCluster.cc:119-175](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/InitCluster.cc#L119-L175)，找到 `handleInitCluster`。
2. 数一数它调用了几次 `handleInitConfig`，分别针对哪几种 `flat::NodeType`。
3. 对照手册的 `init-cluster` 命令（只传了 `--mgmtd`），推断：**在手册的部署流程里，meta/storage/fuse 的初始配置是谁、在什么时候第一次写进 FDB 的？**（提示：看 Step 5/6/8 的 `set-config`。）

**需要观察的现象 / 预期结果**：

- 你会发现 `init-cluster` 在手册里**只带了 `--mgmtd`**，所以它只写了「根目录布局 + mgmtd 配置」。meta/storage/fuse 的配置是后续各自用 `set-config` 第一次上传的。
- 这解释了为什么 `init-cluster` 之后、第一个服务（mgmtd）就能起来：mgmtd 自己的配置已经在 FDB 里了。

**待本地验证**：若你在真实集群跑 `init-cluster`，stdout 应打印类似 `Init filesystem, root directory layout: chain table 1, chunksize 1048576, stripesize 16`（对应 [InitCluster.cc:64-67](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/InitCluster.cc#L64-L67)）与 `Init config for MGMTD version 1`（对应 [InitCluster.cc:115](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/InitCluster.cc#L115)）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `monitor_collector` 必须在 mgmtd/meta/storage 之前启动？

> **答案**：其余所有服务都会把运行指标上报给 monitor（配置里 `[common.monitor.reporters.monitor_collector]` 指向它）。若 monitor 没起来，服务启动时上报会失败、日志噪声大，且后续无法在 ClickHouse 里查到指标。它本身不依赖任何 3FS 服务，只依赖 ClickHouse，所以排在最前。

**练习 2**：手册 FAQ 说「单节点集群无法用 `gen_chain_table.py` 生成」（[deploy/README.md:353-357](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/README.md#L353-L357)），但部署时可以「在一台机器上跑多个 storage 服务」来绕过。这利用了 3FS 的什么特性？

> **答案**：3FS 的复制单位是 chain（默认 3 副本），要求至少有 2 个以上不同的 storage 节点（NodeID 不同）。把多个 storage 进程部署在同一台机器、分配不同 NodeID（如 10001、10002…），集群就认为它们是不同节点，从而满足副本数要求。这只是测试技巧，生产环境绝不能把副本放在同一物理机上。

---

### 4.2 配置托管：为什么运行时配置要由 mgmtd 统一管

#### 4.2.1 概念说明

3FS 每个服务有**三类配置文件**，理解它们的分工是排障的关键：

| 配置文件 | 内容 | 谁来读 | 是否被 mgmtd 托管 |
|---------|------|-------|------------------|
| `*_launcher.toml` | **引导信息**：`cluster_id`、`fdb.clusterFile`、mgmtd 地址、IB 设备 | 进程启动第一时间读 | ❌ 否（本地） |
| `*_app.toml` | **节点身份**：`node_id` 等 | 进程启动时读 | ❌ 否（本地） |
| `*_main.toml` | **运行时配置**：监控、线程池、业务参数、target 路径等 | 向 mgmtd **拉取** | ✅ 是（存 FDB） |

为什么要这么设计？因为「运行时配置」需要**全集群一致 + 可热更新**。如果每个节点各存一份 `*_main.toml`，改一个参数（比如某个超时）就得登录所有机器改文件、重启服务。3FS 的做法是：把运行时配置集中存在 FDB 里（由 mgmtd 代管），节点启动时向 mgmtd 拉取最新版本，改配置时只需 `admin_cli set-config` 上传新版本，各节点自动感知并热加载（热更新机制在 u2-l5 专题讲解）。

而 `launcher.toml` 不能被托管，因为它本身就是「**怎么找到 mgmtd、怎么连上 FDB**」的引导信息——这是个先有鸡还是先有蛋的问题：你不可能用一个还没配好的客户端去拉取自己的引导配置。所以引导配置必须在本地磁盘上。

#### 4.2.2 核心流程

配置的生命周期：

```
                  ┌──────────────── FDB（mgmtd store）────────────────┐
                  │  ConfigInfo(version=1, type=STORAGE, "...")        │
                  │  ConfigInfo(version=2, type=STORAGE, "...")  ← 新版│
                  └───────────────────────┬───────────────────────────┘
                                          │ set-config 上传 / 启动时拉取
        ┌─────────────────────────────────┼──────────────────────────────┐
        ▼                                 ▼                              ▼
   admin_cli                         storage 节点                     meta 节点
  (运维改配置)                     (启动时拉取 main.toml)          (启动时拉取 main.toml)

  本地文件（不托管）：              本地文件（不托管）：
   *_launcher.toml (cluster_id,        *_launcher.toml
   fdb.clusterFile, mgmtd 地址)        *_app.toml (node_id)
```

- **上传**：`admin_cli set-config --type STORAGE --file storage_main.toml` → mgmtd 把文件内容存为 FDB 里的一个新版本（version 递增）。
- **拉取**：服务进程启动时，用 launcher 里的 mgmtd 地址连上 mgmtd，拉取本类型最新的 `*_main.toml`，与本地 launcher/app 配置合并后生效。
- **改配置的正确姿势**：手册 FAQ 明确「All config files are managed by mgmtd. If any `*_main.toml` is updated, the modified file should be uploaded using `admin_cli set-config`.」（[deploy/README.md:360-364](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/README.md#L360-L364)）——直接改服务本地的 `main.toml` 不会生效（会被 mgmtd 的托管版本覆盖）。

#### 4.2.3 源码精读

**① 引导配置长什么样——以 mgmtd 为例**

[configs/mgmtd_main_launcher.toml](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/configs/mgmtd_main_launcher.toml) 顶部（[L1-L7](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/configs/mgmtd_main_launcher.toml#L1-L7)）：

```toml
allow_dev_version = true
cluster_id = ''          # 部署时改成 "stage"
use_memkv = false

[fdb]
casual_read_risky = false
clusterFile = ''         # 部署时改成 '/opt/3fs/etc/fdb.cluster'
```

注意 `cluster_id` 和 `fdb.clusterFile` 在模板里都是空串——这正是手册 Step 4 要求手填的两项（[deploy/README.md:131-138](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/README.md#L131-L138)）。mgmtd 比较特殊：它不需要 `mgmtd_server_addresses`（因为它自己就是 mgmtd）；而 meta/storage/fuse 的 launcher 里都有 `[mgmtd_client] mgmtd_server_addresses = ["RDMA://192.168.1.1:8000"]`。

**② 客户端引导配置——FUSE 例子**

[configs/hf3fs_fuse_main_launcher.toml](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/configs/hf3fs_fuse_main_launcher.toml) 顶部（[L1-L4](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/configs/hf3fs_fuse_main_launcher.toml#L1-L4)）：

```toml
allow_other = true
cluster_id = ''       # "stage"
mountpoint = ''       # '/3fs/stage'
token_file = ''       # '/opt/3fs/etc/token.txt'
```

它的 mgmtd 客户端段（[L86-L95](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/configs/hf3fs_fuse_main_launcher.toml#L86-L95)）：

```toml
[mgmtd_client]
accept_incomplete_routing_info_during_mgmtd_bootstrapping = true
auto_heartbeat_interval = '10s'
enable_auto_heartbeat = false        # 客户端不主动心跳
enable_auto_refresh = true           # 但会自动刷新路由信息
mgmtd_server_addresses = []          # 部署时填 ["RDMA://192.168.1.1:8000"]
```

这里有个值得记的细节：`enable_auto_heartbeat = false` 但 `enable_auto_refresh = true`——客户端不像服务那样靠心跳续租，而是定期主动刷新路由信息（routing info）。这与服务的租约模型不同（u3-l2 详述）。

**③ 运行时配置长什么样——storage 的 target 路径**

部署 storage 时最关键的一步是填 `target_paths`，告诉 storage 把数据放在哪 16 块 SSD 上。模板里默认是空（[configs/storage_main.toml:451-457](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/configs/storage_main.toml#L451-L457)）：

```toml
[server.targets]
allow_disk_without_uuid = false
collect_all_fds = true
create_engine_path = true
space_info_cache_timeout = '5s'
target_num_per_path = 0
target_paths = []      # 部署时填 16 块 SSD 路径
```

部署时按手册 Step 6.4（[deploy/README.md:247-249](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/README.md#L247-L249)）填入 `["/storage/data1/3fs", ..., "/storage/data16/3fs"]`。这份 `storage_main.toml` 改完后，**不是直接放到 storage 节点就生效**，而是要用 `set-config` 上传给 mgmtd（见下一小节）。

**④ 配置托管——`set-config` 的实现**

把一份配置文本上传给 mgmtd 的核心就一行（[src/client/cli/admin/SetConfig.cc:60](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/SetConfig.cc#L60)）：

```cpp
auto res = co_await env.mgmtdClientGetter()->setConfig(env.userInfo, t, *loadFileRes, desc);
```

它前面的 `typeMappings`（[SetConfig.cc:13-20](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/SetConfig.cc#L13-L20)）把命令行的 `--type` 字符串映射成 `flat::NodeType`：

```cpp
const std::map<String, flat::NodeType> typeMappings = {
    {"MGMTD", flat::NodeType::MGMTD},
    {"META",  flat::NodeType::META},
    {"STORAGE", flat::NodeType::STORAGE},
    {"CLIENT", flat::NodeType::CLIENT},
    {"CLIENT_AGENT", flat::NodeType::CLIENT},
    {"FUSE", flat::NodeType::FUSE},
};
```

所以手册里 `set-config --type STORAGE` / `--type META` / `--type FUSE` 用的就是这张表。返回值是新分配的 `ConfigVersion`（[SetConfig.cc:64](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/SetConfig.cc#L64)），版本号单调递增，节点据此判断是否需要重新拉取。

#### 4.2.4 代码实践

> **实践目标**：理解改配置的正确流程，避免「改了本地文件却没生效」的坑。

**操作步骤（源码阅读型）**：

1. 在 [configs/storage_main.toml:409-418](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/configs/storage_main.toml#L409-L418) 找到 `[server.storage]` 段，其中 `max_concurrent_rdma_reads = 256`（[L413](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/configs/storage_main.toml#L413)）。
2. 假设你想把它从 256 调成 512。写出正确的操作序列。

**预期结果（正确姿势）**：

```bash
# 1. 在本地编辑 storage_main.toml，改 max_concurrent_rdma_reads = 512
# 2. 用 admin_cli 把改后的文件上传给 mgmtd（而不是直接重启 storage 服务）
/opt/3fs/bin/admin_cli -cfg /opt/3fs/etc/admin_cli.toml \
  --config.mgmtd_client.mgmtd_server_addresses '["RDMA://192.168.1.1:8000"]' \
  "set-config --type STORAGE --file /opt/3fs/etc/storage_main.toml"
# 输出新的 ConfigVersion，各 storage 节点自动感知并热加载
```

这条命令对应手册 Step 6.5（[deploy/README.md:250-253](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/README.md#L250-L253)）。

**待本地验证**：真实集群里，上传后用 `list-nodes` 观察各 storage 节点的 `ConfigVersion` 列，应从旧版本号跳到新版本号，且 `ConfigStatus` 显示 `UPTODATE`（该列含义见 4.3.3 节 `list-nodes` 源码）。

#### 4.2.5 小练习与答案

**练习 1**：如果你直接 `vim` 改了某 storage 节点本地的 `/opt/3fs/etc/storage_main.toml` 然后 `systemctl restart storage_main`，新参数会生效吗？

> **答案**：不会（或不可靠）。storage 启动时会向 mgmtd 拉取该类型最新版本的 `main.toml` 并以它为准，本地的改动会被托管版本覆盖。正确做法是用 `set-config` 上传，让 mgmtd 把它存成新版本。

**练习 2**：为什么 `*_launcher.toml` 不能也托管起来、让 mgmtd 统一下发？

> **答案**：因为 launcher 里装的是「如何连上 mgmtd、如何连上 FDB」的引导信息。进程启动的第一步就是要用 launcher 去连 mgmtd——如果连这个都要从 mgmtd 拉，就陷入了「要先连上 mgmtd 才能拿到连 mgmtd 的地址」的死循环。所以引导信息必须在本地磁盘。

---

### 4.3 admin_cli：集群的万能瑞士军刀

#### 4.3.1 概念说明

`admin_cli` 是 3FS 提供的**命令行运维工具**，几乎所有手工运维动作（初始化集群、建 target、改配置、查节点、查链、查路由、跑压测……）都通过它完成。它本身**不存任何状态**，只是一个「瘦客户端」：连上 mgmtd/FDB/meta/storage，把你的命令翻译成对它们的 RPC 或事务。

它的二进制源码是 [src/client/bin/admin_cli.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/bin/admin_cli.cc)。

> 小贴士：`/opt/3fs/bin/admin_cli -cfg ... help` 会列出全部子命令（手册 Step 3，[deploy/README.md:115-119](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/README.md#L115-L119)）。

#### 4.3.2 核心流程

`admin_cli` 一次执行的内部流程：

```
main(argc, argv)
  │
  ├─ config.init(&argc, &argv)        ← gflags 消费所有 --config.xxx 标志
  │     (admin_cli.toml 是默认配置；--config.mgmtd_client... 是命令行覆盖)
  │
  ├─ 构造 AdminEnv，里面放一堆「懒构造」的 getter：
  │     kvEngineGetter      ← 第一次用时才建 FDB 引擎
  │     clientGetter        ← 第一次用时才建 net::Client
  │     mgmtdClientGetter   ← 用时才建 MgmtdClientForAdmin，并 refreshRoutingInfo
  │     metaClientGetter / storageClientGetter / coreClientGetter
  │
  ├─ cmd = argv 剩余部分拼接   （若全是 flag，gflags 吃光后只剩非 flag 参数）
  │
  ├─ Dispatcher dispatcher
  ├─ registerAdminCommands(dispatcher)   ← 注册全部子命令
  └─ dispatcher.run(env, ..., cmd, ...)  ← 三种模式：
         · cmd 为空：进入交互式 REPL（linenoise），逐行读 stdin
              ↳ 所以 `admin_cli ... < cmd.txt` 能把文件当脚本喂进去
         · cmd 非空：按 ';' 切分，逐条执行
```

关键设计点：**所有 client 都是懒构造（lazy）的**。这意味着 `init-cluster`（只用 FDB，不用 meta/storage）不会去连 meta/storage；而 `list-nodes` 只用 mgmtdClient。这样不同命令各取所需，启动开销小，也避免了「mgmtd 还没起就强行连」的尴尬。

#### 4.3.3 源码精读

**① `main`：懒构造 + 命令派发**

[src/client/bin/admin_cli.cc:82-238](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/bin/admin_cli.cc#L82-L238) 是 `main` 全貌。先看懒构造的样板——以 mgmtdClient 为例（[admin_cli.cc:126-139](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/bin/admin_cli.cc#L126-L139) 的 `ensureMgmtdClient`）：

```cpp
auto ensureMgmtdClient = [&] {
  [[maybe_unused]] static bool inited = [&] {
    ensureClient();
    mgmtdClient = std::make_shared<MgmtdClientForAdmin>(...);
    folly::coro::blockingWait(mgmtdClient->start(...));
    folly::coro::blockingWait(mgmtdClient->refreshRoutingInfo(/*force=*/false));
    return true;
  }();
};
```

利用 `static bool inited` 的函数局部静态变量技巧，保证每个 client 只在第一次被调用时构造一次。然后把这个守卫包进 getter（[admin_cli.cc:157-162](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/bin/admin_cli.cc#L157-L162)）：

```cpp
env.mgmtdClientGetter = [&] {
  ensureMgmtdClient();
  if (auto ri = mgmtdClient->getRoutingInfo(); !ri || !ri->raw())
    throw StatusException(Status(MgmtdClientCode::kRoutingInfoNotReady));
  return mgmtdClient;
};
```

也就是说，任何需要 mgmtd 的命令在拿到 client 前，都会先确认「路由信息已就绪」，否则抛 `kRoutingInfoNotReady`。这就是为什么 `list-nodes` 在 mgmtd 没起时会立刻报这个错。

命令字符串的拼装和派发在末尾（[admin_cli.cc:209-227](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/bin/admin_cli.cc#L209-L227)）：

```cpp
std::string cmd = argc > 1 ? fmt::format("{}", fmt::join(&argv[1], &argv[argc], " ")) : "";
...
Dispatcher dispatcher;
auto result = folly::coro::blockingWait([&]() -> CoTryTask<void> {
  auto res = co_await registerAdminCommands(dispatcher);
  ...
  co_return co_await dispatcher.run(env, [&env] { return env.currentDir; }, cmd,
                                    config.verbose(), config.profile(),
                                    config.break_multi_line_command_on_failure());
}());
```

注意 `cmd` 来自 **gflags 吃剩的 argv**。手册里很多命令长这样：

```bash
admin_cli -cfg ... --config.mgmtd_client.mgmtd_server_addresses '["RDMA://..."]' "list-nodes"
```

`--config.xxx` 全是 flag，被 gflags 消费；最后只剩 `"list-nodes"` 这个非 flag 参数进入 `cmd`。

**② 命令注册中心**

[src/client/cli/admin/registerAdminCommands.cc:71-138](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/registerAdminCommands.cc#L71-L138) 把 60 多个 handler 一个个注册进 Dispatcher。本讲关心的几个（[L73-L85](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/registerAdminCommands.cc#L73-L85)）：

```cpp
CO_RETURN_ON_ERROR(co_await registerInitClusterHandler(dispatcher));      // init-cluster
CO_RETURN_ON_ERROR(co_await registerUploadChainTableHandler(dispatcher)); // upload-chain-table
CO_RETURN_ON_ERROR(co_await registerListChainTablesHandler(dispatcher));  // list-chain-tables
CO_RETURN_ON_ERROR(co_await registerListChainsHandler(dispatcher));       // list-chains
...
CO_RETURN_ON_ERROR(co_await registerListNodesHandler(dispatcher));        // list-nodes
...
CO_RETURN_ON_ERROR(co_await registerSetConfigHandler(dispatcher));        // set-config
CO_RETURN_ON_ERROR(co_await registerGetConfigHandler(dispatcher));        // get-config
```

每个子命令都是 `src/client/cli/admin/` 下一个独立 `.cc` 文件（如 `InitCluster.cc`、`ListNodes.cc`），各自实现 `getParser()`（定义参数）和 `handle()`（执行逻辑），再用 `dispatcher.registerHandler(getParser, handle)` 注册。这是典型的「**命令模式 + 注册表**」结构——想新增 admin 命令，只需写一个新 `.cc` 并在此处加一行注册（u8-l4 会讲怎么仿照模板加命令）。

**③ 命令派发器：单条、批量、REPL 三种模式**

[src/client/cli/common/Dispatcher.cc:218-294](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/common/Dispatcher.cc#L218-L294) 的 `run` 方法分两个分支。

`cmd` 为空时进入交互式 REPL，逐行从 stdin 读（[Dispatcher.cc:238-257](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/common/Dispatcher.cc#L238-L257)）：

```cpp
if (cmd.empty()) {
  std::string line;
  for (;;) {
    auto input = linenoise(fmt::format("{} > ", promptGetter()).c_str());
    if (!input) break;
    linenoiseHistoryAdd(input);
    line = input;
    linenoiseFree(input);
    ...
    auto res = co_await runLine(env, *this, line);
    print(printer, res);
  }
}
```

`linenoise` 从 stdin 读一行。当 stdin 被重定向成文件（`admin_cli ... < create_target_cmd.txt`）时，它就逐行读文件——**这就解释了手册 Step 7.3 为什么能用 `< output/create_target_cmd.txt` 一次性创建大量 target**（[deploy/README.md:284-287](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/README.md#L284-L287)）：文件里每行一条 `create-target ...` 命令，逐条喂进 REPL。

`cmd` 非空时按 `;` 切分批量执行（[Dispatcher.cc:258-292](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/common/Dispatcher.cc#L258-L292)）：

```cpp
} else {
  while (!cmd.empty()) {
    auto it = cmd.find_first_of(';');
    if (it == std::string_view::npos) {
      auto res = co_await runLine(env, *this, cmd);   // 最后一条
      ...
    } else {
      auto line = cmd.substr(0, it);                   // 取出一条
      auto res = co_await runLine(env, *this, line);
      ...
      cmd = cmd.substr(it + 1);                        // 剩下的继续
      if (breakMultiLineCommandOnFailure && res.hasError()) break;  // 失败可中断
    }
  }
}
```

所以你也可以把多条命令用 `;` 拼成一个参数：`"list-nodes; list-chains"`。

**④ `list-nodes`：从 mgmtd 拉路由信息**

实践要用的 `list-nodes` 逻辑很短（[src/client/cli/admin/ListNodes.cc:61-88](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/ListNodes.cc#L61-L88)）：

```cpp
auto mgmtdClient = env.mgmtdClientGetter();
CO_RETURN_ON_ERROR(co_await mgmtdClient->refreshRoutingInfo(/*force=*/true));  // 强制刷新
auto routingInfo = mgmtdClient->getRoutingInfo();
const auto &nodes = routingInfo->raw()->nodes;
table.push_back({"Id","Type","Status","Hostname","Pid","Tags","LastHeartbeatTime","ConfigVersion","ReleaseVersion"});
auto configVersionsRes = co_await mgmtdClient->getConfigVersions();
...  // 排序后逐个 printNode
```

它先**强制刷新路由信息**（`force=true`），再读 `routingInfo->raw()->nodes`。这正是「mgmtd 是集群的目录服务」的体现：节点列表不在 admin_cli 本地，而是实时从 mgmtd 拉。

打印的列里，`ConfigVersion` 一列尤其实用（[ListNodes.cc:33-56](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/ListNodes.cc#L33-L56)）：它对比节点上报的版本和 mgmtd 的最新版本，输出 `版本号(UPTODATE/DIRTY/FAILED)`。`DIRTY` 表示节点还没拉到最新配置——这是验证 `set-config` 是否已生效的直观指标。

**⑤ `list-chain-tables` / `list-chains`：验证数据放置拓扑**

`list-chain-tables` 同样先强制刷新路由信息，再遍历 `routingInfo->raw()->chainTables`，统计每张链表的 chain 数与副本数（[src/client/cli/admin/ListChainTables.cc:22-46](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/ListChainTables.cc#L22-L46)），表头是 `{"ChainTableId","ChainTableVersion","ChainCount","ReplicaCount","Desc"}`（[L25](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/ListChainTables.cc#L25)）。

`list-chains` 则深入到每条链，统计链内 serving/syncing 的 target 数来判定链状态（[ListChains.cc:29-60](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/ListChains.cc#L29-L60)）：

```cpp
size_t serving = 0, syncing = 0;
for (const auto &t : ci.targets) {
  const auto &ti = targets.at(t.targetId);
  row.push_back(fmt::format("{}({}-{})", t.targetId.toUnderType(),
                            magic_enum::enum_name(ti.publicState),
                            magic_enum::enum_name(ti.localState)));
  switch (ti.publicState) { case PS::SERVING: ++serving; break; ... }
}
if (syncing)              status = "SYNCING";
else if (serving == ci.targets.size()) status = "SERVING";
else if (serving == 0)    status = "UNAVAILABLE";
else                      status = fmt::format("SERVING({}/{})", serving, ci.targets.size());
```

每个 target 显示成 `id(publicState-localState)` 的形态。这条逻辑在下面的实践里会用到。

#### 4.3.4 代码实践

> **实践目标**：用 `list-nodes / list-chains / list-chain-tables` 验证集群状态，并能读懂输出。

**操作步骤（在有集群的环境；无集群则做源码阅读型变体）**：

手册在 Step 4/5/6/7 反复用到这一组命令（以 `list-nodes` 为例，[deploy/README.md:160-163](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/README.md#L160-L163)）：

```bash
/opt/3fs/bin/admin_cli \
  -cfg /opt/3fs/etc/admin_cli.toml \
  --config.mgmtd_client.mgmtd_server_addresses '["RDMA://192.168.1.1:8000"]' \
  "list-nodes"
```

部署完成后，依次运行三条命令验证：

| 命令 | 期望看到 | 对应源码表头 |
|------|---------|------------|
| `list-nodes` | 1 个 mgmtd、1 个 meta、5 个 storage，`Status` 为 `HEALTHY`，`ConfigVersion(...)` 为 `UPTODATE` | [ListNodes.cc:73-74](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/ListNodes.cc#L73-L74) |
| `list-chain-tables` | 一张 chain table（id=1），`ChainCount`/`ReplicaCount` 非零，`Desc=stage` | [ListChainTables.cc:25](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/ListChainTables.cc#L25) |
| `list-chains` | 每条链 `Status=SERVING`，每个 target 显示 `id(SERVING-UP_TO_DATE)` | [ListChains.cc:107](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/ListChains.cc#L107) 与 [L32-L35](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/ListChains.cc#L32-L35) |

**源码阅读型变体（无需集群）**：打开 `list-chains` 的 `printChain`（[ListChains.cc:29-60](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/ListChains.cc#L29-L60)），回答：**一条 3 副本的链，当其中一个 target 故障下线时，`list-chains` 会显示什么状态？**

**预期结果**：3 个 target 中 2 个 `SERVING`、1 个非 serving，`serving(2) < targets.size(3)` 且 `serving != 0`，所以状态为 `SERVING(2/3)`（见 [ListChains.cc:52-60](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/ListChains.cc#L52-L60)）。这正是 CRAQ「写全读任何」还能继续提供读写服务的体现。

**待本地验证**：真实集群里的实际输出格式与列宽以本地运行为准。

#### 4.3.5 小练习与答案

**练习 1**：手册 Step 7.3 用 `admin_cli ... < output/create_target_cmd.txt` 一次创建大量 target。请解释这种「重定向文件」用法为什么能 work，走了 `Dispatcher::run` 的哪个分支？

> **答案**：命令行里所有参数都是 `--config.xxx` flag（被 gflags 消费），没有非 flag 的命令，所以 `cmd` 为空，进入 [Dispatcher.cc:238-257](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/common/Dispatcher.cc#L238-L257) 的空 `cmd` 分支——交互式 REPL。该分支用 `linenoise` 逐行读 stdin；stdin 被重定向成文件后，就变成「逐行读文件里的命令并执行」，于是文件里每行一条 `create-target` 命令被依次执行。

**练习 2**：`list-nodes` 的输出里某 storage 节点显示 `ConfigVersion` 为 `5(DIRTY)`，这代表什么？该怎么处理？

> **答案**：`DIRTY` 表示该节点当前运行的配置版本是 5，但 mgmtd 上该类型的最新版本已经更高（节点还没拉到/还没热加载成功）。参考 [ListNodes.cc:33-50](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/ListNodes.cc#L33-L50)。通常等几秒（节点会周期性 refresh）就会变成 `UPTODATE`；若长期 `DIRTY` 或 `FAILED`，需查节点日志看热更新是否报错。

**练习 3**：为什么 `admin_cli` 把 client 都做成「懒构造」，而不是在 `main` 一开始就全部建好？

> **答案**：不同命令依赖的组件不同：`init-cluster` 只用 FDB（此时 mgmtd 还没起，绝不能去连 mgmtd）；`list-nodes` 只用 mgmtd；`stat`/`create` 才用 meta。懒构造让每个命令只初始化自己需要的 client，既降低启动延迟，又避免「服务还没起就被强行连接」的错误。

---

## 5. 综合实践

**任务**：在阅读层面「彩排」一次完整的 `init-cluster` + 验证流程，把三个模块串起来。若条件允许，再在真实集群上跑一遍。

假设你已按手册 Step 0~3 装好了二进制、admin_cli 和 FDB，现在要完成 Step 4 的核心动作。请按顺序回答并写出命令：

1. **（部署步骤）** mgmtd 启动前，必须先用 admin_cli 做什么？这个动作往 FDB 写了哪两类数据？请引用 [InitCluster.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/InitCluster.cc) 中的函数名作答。

2. **（配置托管）** mgmtd 起来后，要部署 meta 服务。meta 的运行时配置（`meta_main.toml`）如何进入集群？写出那条 `set-config` 命令，并说明为什么不能只改本地文件。

3. **（admin_cli）** 全部服务就绪后，用一条 admin_cli 命令同时执行 `list-nodes` 和 `list-chain-tables`（提示：用 `;`），并说明这条命令走的是 `Dispatcher::run` 的哪个分支。

**参考答案要点**：

1. 先跑 `admin_cli "init-cluster --mgmtd /opt/3fs/etc/mgmtd_main.toml 1 1048576 16"`。它写两类数据：① 根目录布局（`handleInitFileSystem`，[InitCluster.cc:46-70](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/InitCluster.cc#L46-L70)）；② mgmtd 的初始配置（`handleInitConfig`，[InitCluster.cc:72-117](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/InitCluster.cc#L72-L117)）。

2. `admin_cli ... "set-config --type META --file /opt/3fs/etc/meta_main.toml"`（手册 Step 5.3，[deploy/README.md:196-198](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/README.md#L196-L198)）。不能只改本地文件，因为 meta 启动时会向 mgmtd 拉取最新托管版本并以它为准（见 4.2 节）。

3. `admin_cli -cfg ... "list-nodes; list-chain-tables"`。因为 `cmd` 非空，走 [Dispatcher.cc:258-292](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/common/Dispatcher.cc#L258-L292) 的 `;` 切分分支，逐条执行。

> 真实集群演练：若条件允许，按 [deploy/README.md](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/README.md) 完整走一遍 Step 0~8，并在每步后用 `list-nodes` 观察 `ConfigVersion` 的递增与 `UPTODATE` 翻转——这是检验「配置托管」机制是否正常工作的最直接方式（实际结果待本地验证）。

## 6. 本讲小结

- 3FS 集群由 monitor / admin_cli / mgmtd / meta / storage / client 等进程组成，**启动有严格顺序**：外部依赖（FDB/ClickHouse）→ monitor → admin_cli → mgmtd（须先 `init-cluster`）→ meta/storage（向 mgmtd 注册）→ 建 target/chain/chain-table → FUSE 客户端。
- 每个服务有三类配置：`*_launcher.toml`（引导，本地）、`*_app.toml`（节点身份，本地）、`*_main.toml`（运行时，**由 mgmtd 托管在 FDB**）。改运行时配置必须用 `set-config` 上传，不能直接改本地文件。
- `init-cluster` 做两件事：写入根目录布局（chunkSize/stripeSize/chainTableId）和各服务的初始配置版本；它是弱幂等操作，改错 `mgmtd_main.toml` 后需要清空 FDB 重跑。
- systemd 单元差异反映了进程特性：storage 多了 `LimitMEMLOCK=infinity`（RDMA 需要锁内存页），FUSE 客户端只传 `--launcher_cfg`（无节点身份）。
- `admin_cli` 是无状态瘦客户端，用懒构造的 mgmtd/meta/storage client + 一个 `Dispatcher` 把文本命令派发到 60+ 个 handler；命令可单条、`;` 批量、或重定向文件逐行执行。
- `list-nodes` / `list-chains` / `list-chain-tables` 都是从 mgmtd **强制刷新路由信息**后打印，是验证集群状态的标准三板斧；`list-nodes` 的 `ConfigVersion(DIRTY/UPTODATE)` 列尤其适合验证配置是否已下发到位。
- 排障入口：`journalctl` 看 stdout/stderr，`/var/log/3fs/*.log` 看服务日志（[deploy/README.md:367-374](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/README.md#L367-L374)）。

## 7. 下一步学习建议

到这里你已能「把 3FS 跑起来并用 admin_cli 查看」。接下来建议：

1. **建立端到端的全局视图**：学 **u1-l4（四大组件与端到端请求链路总览）**，把 open/read/write 如何穿越 client→meta→storage→mgmtd 串成一张时序图。本讲只讲了「静态拓扑」，u1-l4 讲「动态数据流」。
2. **理解服务骨架**：本讲反复出现的「launcher + app 两阶段配置」其实是所有 3FS 服务的统一启动框架，**u2-l1（TwoPhaseApplication 与 ServerLauncher）** 会拆开讲它。
3. **深入配置热更新**：本讲只说「set-config 后节点会感知」，**u2-l5（配置系统与热更新）** 会讲 `ConfigBase` 的声明式配置与热更新回调机制。
4. **深入 mgmtd**：本讲的 `init-cluster` / `list-nodes` / `set-config` 都是 mgmtd 的客户端操作，**u3 单元** 会从 mgmtd 服务端视角讲清路由信息、租约选举、链表模型与 target 状态机。

推荐继续阅读的源码：先把 [deploy/README.md](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/deploy/README.md) 的 FAQ 通读一遍，再浏览 [src/client/cli/admin/](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/registerAdminCommands.cc) 目录里几个简单命令（`ListNodes.cc`、`GetConfig.cc`、`Stat.cc`）——它们是理解 admin_cli 工作原理的最佳样本。
