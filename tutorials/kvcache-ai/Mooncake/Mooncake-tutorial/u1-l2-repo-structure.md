# 仓库目录结构与组件地图

## 1. 本讲目标

本讲是阅读 Mooncake 源码的「地图课」。学完后你应当能够：

1. 说出仓库顶层每个 `mooncake-*` 目录对应的是哪个子系统。
2. 区分 **Transfer Engine（传输引擎）**、**Mooncake Store（分布式 KV 存储）**、**EP（专家并行）**、**PG（进程组）**、**P2P Store** 各自的职责边界。
3. 在仓库里快速定位每个子系统的源码目录、头文件目录与构建入口（`CMakeLists.txt`）。
4. 看懂顶层 `CMakeLists.txt` 是如何用一组 `WITH_*` 开关把这些子系统「拼装」起来的。
5. 用树形图和文件计数，对一个陌生的大型 C++/多语言仓库建立规模直觉。

本讲是 **u1-l1** 的延续：u1-l1 帮你建立了「Mooncake 是什么」的宏观认知，本讲带你把这份认知落到具体的目录与文件上。

## 2. 前置知识

在开始前，请确认你已经理解以下几个概念（u1-l1 已铺垫）：

- **KVCache（键值缓存）**：大语言模型推理过程中产生的中间张量，是 Mooncake 最核心的「货物」。
- **Prefill / Decode 分离**：把长 prompt 的预填充阶段和逐 token 的解码阶段拆到不同机器上，Mooncake 专门为此设计。
- **Disaggregated KVCache Pool（解耦的 KVCache 池）**：把缓存从推理引擎里抽出来，放到独立的存储节点上，复用 CPU/DRAM/SSD 资源。README 对此有一句精炼描述：

  > [README.md:77](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/README.md#L77) — Mooncake features a KVCache-centric disaggregated architecture ... leverages the underutilized CPU, DRAM, and SSD resources of the GPU cluster to implement a disaggregated KVCache pool.

- **CMake**：Mooncake 用 CMake 组织构建。顶层 `CMakeLists.txt` 通过 `add_subdirectory(...)` 把每个子系统挂进构建树，并用 `option(WITH_XXX ...)` 决定是否编译某个子系统。你只需要记住两个关键字：`option`（开关）和 `add_subdirectory`（挂载子目录）。

如果你对这些概念还陌生，建议先回到 u1-l1 复习「整体架构」部分，再继续本讲。

## 3. 本讲源码地图

本讲涉及的「源码」其实主要是仓库自身的目录组织与构建脚本，而非算法实现。下面是要点到的关键文件：

| 文件 / 目录 | 作用 |
|-------------|------|
| `README.md` | 项目总览，包含各子系统的官方一句话定位（TE / Store / EP / PG 等）。本讲大量引用其中的章节标题作为「权威定义」。 |
| `CMakeLists.txt`（顶层） | 整个仓库的构建总入口，定义所有 `WITH_*` 开关，并按顺序 `add_subdirectory` 各子系统。 |
| `mooncake-common/` | 公共代码与构建基础设施（共享头文件、`.cmake` 模块、etcd/k8s 的 wrapper）。 |
| `mooncake-transfer-engine/` | **Transfer Engine**：高性能数据传输框架，整个项目的基石。 |
| `mooncake-store/` | **Mooncake Store**：建立在 Transfer Engine 之上的分布式 KVCache 存储。 |
| `mooncake-ep/` | **Mooncake EP**：面向大规模 MoE 推理的专家并行（expert parallelism）内核。 |
| `mooncake-pg/` | **Mooncake PG**：可作为 PyTorch `torch.distributed` 后端的进程组（process group）。 |
| `mooncake-p2p-store/` | **P2P Store**：演示用 Transfer Engine 在节点间点对点传输的轻量示例（Go 实现）。 |
| `mooncake-rl/` | 强化学习示例，演示如何用 Store 在 rollout 与训练引擎之间传数据。 |
| `mooncake-store/src/master.cpp` | Store 的 `mooncake_master` 可执行文件入口，本讲用它说明「构建入口如何落到一个 `main()`」。 |

> 提示：上面这些目录在仓库里都真实存在，你可以用 `ls` 逐一对照。后续章节会逐个展开。

## 4. 核心概念与源码讲解

本讲按三个最小模块组织：**顶层目录划分**、**子系统职责**、**构建入口 CMakeLists.txt**。

### 4.1 顶层目录划分

#### 4.1.1 概念说明

一个成熟的开源项目通常不会把所有代码塞进一个目录，而是按「子系统 / 关注点」切分成若干顶层目录。Mooncake 仓库的顶层可以分成三类：

1. **核心子系统目录**：以 `mooncake-` 开头，每个对应一个可独立构建的能力（传输、存储、EP、PG 等）。
2. **支撑性目录**：`docs/`、`scripts/`、`docker/`、`benchmarks/`、`monitoring/`、`extern/`、`image/` 等，提供文档、脚本、容器、依赖与素材。
3. **工程治理文件**：`README.md`、`CMakeLists.txt`、`LICENSE-APACHE`、`CONTRIBUTING.md`、`MAINTAINERS.md`、`.pre-commit-config.yaml`、`dependencies.sh`、`requirements-dev.txt` 等。

其中最关键的是第一类。本讲的全部注意力都集中在 `mooncake-*` 目录上——它们就是 Mooncake 的「组件地图」。

#### 4.1.2 核心流程

当你 `git clone` 仓库后，建立组件地图的标准流程是：

```text
git clone → cd Mooncake → ls
        │
        ├─ 看到 mooncake-transfer-engine  → 记下：传输引擎（最底层）
        ├─ 看到 mooncake-store            → 记下：KVCache 存储（建在传输引擎上）
        ├─ 看到 mooncake-ep / mooncake-pg → 记下：MoE 专家并行 + 进程组
        ├─ 看到 mooncake-p2p-store        → 记下：点对点传输示例
        ├─ 看到 mooncake-common           → 记下：公共代码 + 构建工具
        └─ 看到 mooncake-rl               → 记下：强化学习示例
```

一个判断「目录是不是核心子系统」的实用准则：**它是否在顶层 `CMakeLists.txt` 里被 `add_subdirectory` 引用**。被引用的就是会被编译进构建产物的正式组件；没被引用的（如 `mooncake-rl`）通常是纯示例或上层应用。

#### 4.1.3 源码精读

仓库顶层确实并列着这些 `mooncake-*` 目录（以下结构为在本讲 HEAD 下 `ls` 的真实结果）：

```text
Mooncake/
├── mooncake-common/            # 公共代码 + .cmake 构建模块
├── mooncake-transfer-engine/   # 传输引擎（TE）
├── mooncake-store/             # 分布式 KVCache 存储
├── mooncake-ep/                # 专家并行（EP）
├── mooncake-pg/                # 进程组（PG）
├── mooncake-p2p-store/         # 点对点传输示例（Go）
├── mooncake-rl/                # 强化学习示例（Python）
├── mooncake-integration/       # 集成测试与 Python allocator 辅助
├── mooncake-wheel/             # Python wheel 打包配置（pyproject.toml）
├── CMakeLists.txt              # 顶层构建入口
├── README.md                   # 项目总览
└── ...
```

> 说明：上面是示例树形图（标注为「示例代码」仅指排版），目录名与层级均来自真实仓库，不含虚构内容。

每个子系统内部普遍遵循一个固定的小布局，记住它就能在任何一个 `mooncake-*` 目录里快速定位：

- `include/`：对外公开头文件（库的「接口」）。
- `src/`：实现源码（`.cpp` / `.cu` / `.go`）。
- `tests/`（或 `test/`）：单元测试。
- `CMakeLists.txt`：该子系统的构建脚本。
- 部分子系统还有 `benchmark/`、`example/`、`setup.py`（Python 扩展）、`rust/`（Rust 绑定）、`go/`（Go 绑定）。

以 Store 为例，它的 `include/` 目录列出的 `master_service.h`、`client_service.h`、`storage_backend.h` 等头文件，正是这个子系统对外暴露能力的地方——名字本身就泄露了职责（master 主控、client 客户端、storage backend 存储后端）。

#### 4.1.4 代码实践

**实践目标**：亲手画出仓库的顶层组件地图，并为一行注解。

**操作步骤**：

1. 在仓库根目录执行 `ls -d mooncake-*`（只看 `mooncake-` 开头的目录）。
2. 把输出整理成一张表，第一列是目录名，第二列用一句话写它的职责（先凭名字猜，再对照第 4.2 节订正）。

**需要观察的现象**：你会看到 9 个左右 `mooncake-*` 目录；其中 `mooncake-transfer-engine`、`mooncake-store`、`mooncake-common` 体积明显更大。

**预期结果**：得到一张与本讲「3. 本讲源码地图」表格类似的对照表。

**如果无法运行**：如果你暂时没有本地环境，可直接阅读仓库页面 `https://github.com/kvcache-ai/Mooncake` 的根目录文件列表完成对照（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：在仓库根目录下，哪个目录最可能存放「供所有子系统复用的工具头文件与 CMake 模块」？

**答案**：`mooncake-common/`。它包含 `include/`（如 `default_config.h`、`duration_utils.h`）和多个 `.cmake` 模块（如 `common.cmake`、`FindGLOG.cmake`）。

**练习 2**：仓库里既有 `mooncake-transfer-engine` 又有 `mooncake-store`，二者谁的层级更靠下（依赖另一方）？

**答案**：`mooncake-store` 依赖 `mooncake-transfer-engine`。Store 是「建在 Transfer Engine 之上」的存储层（详见 4.2）。

---

### 4.2 子系统职责

#### 4.2.1 概念说明

Mooncake 不是单一程序，而是「一个底座 + 多个上层能力」的组合。理解职责分工，比记住代码细节更重要，因为它决定了你遇到一个需求时该去哪个目录找答案。下面按「自底向上」的依赖顺序介绍各子系统。

#### 4.2.2 核心流程

Mooncake 的能力栈可以这样自下而上看：

```text
        ┌─────────────────────────────────────────────┐
   上层  │ mooncake-ep / mooncake-pg  (MoE 并行/进程组) │
   应用  │ mooncake-rl  (强化学习示例)                  │
        ├─────────────────────────────────────────────┤
   存储  │ mooncake-store  (分布式 KVCache 存储)        │
        ├─────────────────────────────────────────────┤
   传输  │ mooncake-transfer-engine  (数据传输引擎)     │  ← 项目基石
        ├─────────────────────────────────────────────┤
   公共  │ mooncake-common  (共享头 + 构建工具)          │
        └─────────────────────────────────────────────┘
```

旁路的轻量组件：`mooncake-p2p-store`（点对点传输示例）直接建在 Transfer Engine 上，不经过 Store。

#### 4.2.3 源码精读

README 用清晰的章节标题给出了每个子系统的官方定义，这是最权威的「职责说明」，我们逐条引用：

**(a) Transfer Engine（TE）——项目基石**

> [README.md:90-92](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/README.md#L90-L92) — `### Transfer Engine (TE)`：The core of Mooncake is the Transfer Engine (TE), a high-performance data transfer framework ... a unified interface for batched data movement across diverse storage, network, and accelerator environments.

要点：TE 提供统一的数据搬运接口，支持 TCP / RDMA / NVLink / EFA / NVMe-oF 等多种传输协议。它的源码在 `mooncake-transfer-engine/`，其中 `src/transport/` 下按协议分目录（`rdma_transport/`、`tcp_transport/`、`nvlink_transport/`、`nvmeof_transport/` 等），命名即职责。

**(b) Mooncake Store——分布式 KVCache 存储**

> [README.md:114-116](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/README.md#L114-L116) — `### Mooncake Store`：a high-performance distributed key-value cache storage engine designed for LLM inference. Built on the Transfer Engine ...

要点：Store **建立在 Transfer Engine 之上**，负责 KVCache 与模型权重的存储、复制、淘汰与高带宽传输；支持 DRAM / SSD 多级缓存。它的主控进程入口在 `mooncake-store/src/master.cpp`（见 4.3）。

**(c) Mooncake EP 与 PG——专家并行 + 进程组**

> [README.md:133-135](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/README.md#L133-L135) — `### Mooncake EP and Process Group (PG)`：extend Mooncake from high-performance data movement to fault-tolerant distributed execution for large-scale MoE inference.

要点：
- **EP（Expert Parallelism）**：适配 DeepEP 风格的专家并行 dispatch/combine，带 `active_ranks` 感知，能在部分 rank 失效时绕开它继续服务。源码在 `mooncake-ep/`，含 CUDA 内核（`.cu`）。
- **PG（Process Group）**：可注册为 PyTorch `torch.distributed` 后端，提供集合通信原语，并能检测失效 rank、上报、恢复。源码在 `mooncake-pg/`。

**(d) P2P Store——点对点传输示例**

`mooncake-p2p-store/` 是一个**点对点**传输的轻量实现，源码为 Go（`src/p2pstore/` 下的 `core.go`、`metadata.go`、`transfer_engine.go` 等），通过 Transfer Engine 的绑定在节点间直接搬数据。它常被用来演示「不经主控的节点对节点传输」。注意：它默认不编译，需要 `WITH_P2P_STORE=ON`。

**(e) mooncake-common——公共底座**

体量小但位置关键：包含少量共享头文件（`include/default_config.h`、`duration_utils.h`、`environ.h`）和一组 `.cmake` 构建模块（`common.cmake`、`FindGLOG.cmake`、`FindJsonCpp.cmake`、`SetupPython.cmake` 等），还托管 etcd、k8s-lease 的 wrapper。几乎所有子系统都会链接到它。

**(f) mooncake-rl——强化学习示例**

仅含 `examples/rl_samples.py`，是一份演示脚本，展示如何用 `mooncake.store.MooncakeDistributedStore` 在 rollout 引擎与训练引擎之间传数据。文件首行注释明确写道：这是一个 dummy example，用于演示 Store 在分布式 RL 中的用法。它**不参与 C++ 构建**。

把以上信息浓缩成一张「职责—定位」速查表：

| 子系统 | 一句话职责 | 源码目录 | 是否默认编译 |
|--------|-----------|----------|--------------|
| Transfer Engine (TE) | 跨协议/跨硬件的高性能数据传输底座 | `mooncake-transfer-engine/` | 是（`WITH_TE=ON`） |
| Mooncake Store | 建在 TE 上的分布式 KVCache 存储 | `mooncake-store/` | 是（`WITH_STORE=ON`） |
| Mooncake EP | 大规模 MoE 的专家并行内核 | `mooncake-ep/` | 否（`WITH_EP=OFF`） |
| Mooncake PG | PyTorch 进程组后端 + 容错恢复 | `mooncake-pg/` | 否（随 `WITH_EP`） |
| P2P Store | 点对点传输示例（Go） | `mooncake-p2p-store/` | 否（`WITH_P2P_STORE=OFF`） |
| common | 共享头 + CMake 模块 + wrapper | `mooncake-common/` | 是（始终） |
| RL 示例 | RL 中用 Store 传数据的演示脚本 | `mooncake-rl/` | 不编译（纯脚本） |

> 注：表中「是否默认编译」的开关值来自顶层 `CMakeLists.txt` 的 `option(...)`，见 4.3。

#### 4.2.4 代码实践

**实践目标**：用文件计数对每个子系统的「规模与语言构成」建立直觉。

**操作步骤**：在仓库根目录对每个子系统分别统计 `.cpp`、`.h/.hpp`、`.cu`、`.go`、`.py`、`.rs` 文件数量，例如：

```bash
find mooncake-transfer-engine -name '*.cpp' | wc -l
find mooncake-transfer-engine \( -name '*.h' -o -name '*.hpp' \) | wc -l
```

**需要观察的现象 / 预期结果**（本讲 HEAD 下的实测值，仅作参考；行数会随版本变化）：

| 子系统 | `.cpp` | `.h/.hpp` | `.cu` | 其他主要语言 |
|--------|-------:|----------:|------:|-------------|
| mooncake-transfer-engine | 186 | 146 | — | py≈11, rust |
| mooncake-store | 164 | 136 | — | rust≈7, go |
| mooncake-ep | 2 | — | 1 | py（CUDA 扩展） |
| mooncake-pg | 6 | — | 1 | py（CUDA 扩展） |
| mooncake-p2p-store | 0 | — | — | **go≈7** |
| mooncake-common | 5 | 3 | — | cmake 模块、go wrapper |
| mooncake-rl | 0 | 0 | — | py=1 |

**如何解读**：
- TE 与 Store 的 `.cpp`+`.h` 都在 300 个量级，是项目的绝对主体；其余子系统要小得多。
- `mooncake-p2p-store` 没有 `.cpp` 是正常的——它是 **Go 实现**，所以应该去看 `.go` 文件。
- `mooncake-ep`/`mooncake-pg` 的 C++ 文件极少，因为它们的核心是 **CUDA 内核（`.cu`）+ Python 扩展（`setup.py` / `BuildEpExt.cmake`）**，靠 PyTorch 扩展机制构建，而不是普通 C++ 库。

> 这些数字是本讲 HEAD（`945f3e61`）下的真实统计；你本地重新跑一次命令，数字可能略有不同。

#### 4.2.5 小练习与答案

**练习 1**：如果你要给 Transfer Engine 增加一种新的网络传输协议，应该改哪个目录？

**答案**：`mooncake-transfer-engine/src/transport/`。该目录已经按协议分子目录（`rdma_transport/`、`tcp_transport/` 等），新增协议就照此模式新建一个子目录。

**练习 2**：`mooncake-p2p-store` 为什么 `.cpp` 文件数为 0，它还能工作吗？

**答案**：能。它是用 **Go** 写的（`src/p2pstore/*.go`），通过 Transfer Engine 的 Go 绑定调用底层能力，所以没有 `.cpp` 实现文件是预期内的，不代表它缺失实现。

**练习 3**：EP 和 PG 的代码量都不大，为什么它们仍被列为正式子系统？

**答案**：因为它们解决的是「大规模 MoE 推理的容错分布式执行」这一独立关注点，并且通过顶层 `CMakeLists.txt` 的 `WITH_EP` 开关被正式纳入构建（详见 4.3）；其核心是 CUDA 内核 + PyTorch 扩展，不能用 `.cpp` 行数衡量其价值。

---

### 4.3 构建入口 CMakeLists.txt

#### 4.3.1 概念说明

顶层 `CMakeLists.txt` 是整个仓库的「总装线」。它的核心思想是：**用一组布尔开关（`option`）决定要编译哪些子系统，再用 `add_subdirectory` 把选中的子系统挂进构建树**。这种设计的好处是——你可以只编译自己关心的部分，避免拉起 CUDA、Go、Rust 等全部工具链。

#### 4.3.2 核心流程

构建的总流程可以概括为：

```text
cmake ..
  │
  ├─ 1. 读 option(WITH_TE ...) / option(WITH_STORE ...) ...   # 定义开关与默认值
  ├─ 2. add_subdirectory(mooncake-common)                    # 公共底座（始终）
  ├─ 3. if(WITH_TE)  -> add_subdirectory(mooncake-transfer-engine)
  ├─ 4. if(WITH_STORE) -> add_subdirectory(mooncake-store)
  ├─ 5. if(WITH_STORE_RUST) -> add_subdirectory(mooncake-store/rust)
  ├─ 6. if(WITH_EP) -> 注册 mooncake_ep_ext / mooncake_pg_ext 自定义目标
  ├─ 7. add_subdirectory(mooncake-integration)               # 集成测试
  └─ 8. if(WITH_P2P_STORE) -> add_subdirectory(mooncake-p2p-store)
```

关键的依赖约束也写在脚本里，例如 `WITH_STORE_RUST=ON` 要求 `WITH_STORE=ON`（因为 Rust 绑定要包裹 Store 的 C++ 库）。

#### 4.3.3 源码精读

**(a) 一组 `WITH_*` 开关定义了子系统的默认编译状态**

> [CMakeLists.txt:15-22](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/CMakeLists.txt#L15-L22) — 定义 `WITH_TE`、`WITH_STORE`、`WITH_STORE_GO`、`WITH_P2P_STORE`、`WITH_RUST_EXAMPLE`、`WITH_STORE_RUST`、`WITH_EP`、`USE_NOF` 等开关及其默认值。

从默认值就能看出项目的「重心」：`WITH_TE` 和 `WITH_STORE` 默认 `ON`（传输与存储是主线），而 `WITH_EP`、`WITH_P2P_STORE` 默认 `OFF`（需要额外环境，按需开启）。

**(b) 公共底座先被挂载**

> [CMakeLists.txt:71](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/CMakeLists.txt#L71) — `add_subdirectory(mooncake-common)`。它不带 `if` 守卫，说明 common 是**无条件**编译的底座。

**(c) TE 与 Store 按开关挂载**

> [CMakeLists.txt:75-84](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/CMakeLists.txt#L75-L84) — `if(WITH_TE)` 时挂载 `mooncake-transfer-engine`；`if(WITH_STORE)` 时打印 "Mooncake Store will be built" 并挂载 `mooncake-store`，同时把各自的 `include/` 加入头文件搜索路径。

**(d) EP / PG 用自定义目标构建 Python 扩展**

> [CMakeLists.txt:95-104](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/CMakeLists.txt#L95-L104) — `if(WITH_EP)` 分支：在非 IDE 模式下，通过 `add_custom_target(mooncake_ep_ext ...)` 调用 `mooncake-ep/BuildEpExt.cmake` 来构建 PyTorch CUDA 扩展（`mooncake_pg_ext` 同理）。这正是 EP/PG「没有大量 `.cpp`」的原因——它们走的是 Python 扩展构建路径。

**(e) P2P Store 的开关挂载**

> [CMakeLists.txt:187-190](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/CMakeLists.txt#L187-L190) — `if(WITH_P2P_STORE)` 时挂载 `mooncake-p2p-store`。

**(f) 构建入口如何落到一个真实的 `main()`**

子系统挂进构建树后，会产出可执行文件。以 Store 的主控进程为例：

> [mooncake-store/src/CMakeLists.txt:271](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/src/CMakeLists.txt#L271) — `add_executable(mooncake_master master.cpp)`：把 `master.cpp` 编译成名为 `mooncake_master` 的可执行程序。

这个 `mooncake_master` 就是 Store 的「主控服务」入口。它的 `main()` 位于：

> [mooncake-store/src/master.cpp:951](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/src/master.cpp#L951) — `int main(int argc, char* argv[])`：解析命令行参数（gflags）、加载配置，然后根据是否启用 HA 走两条不同的启动路径。

具体来说，`main()` 末尾有两条分支：

- **HA 模式**（高可用，多 master 主备）：
  > [mooncake-store/src/master.cpp:1110-1112](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/src/master.cpp#L1110-L1112) — 构造 `MasterServiceSupervisor` 并由它接管启动流程（主备选举、故障切换）。
- **非 HA 模式**（单 master）：
  > [mooncake-store/src/master.cpp:1116-1124](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/src/master.cpp#L1116-L1124) — 直接创建一个 `coro_rpc_server`（基于 coro_rpc 的协程 RPC 服务器），如果环境变量 `MC_RPC_PROTOCOL=rdma` 则调用 `server.init_ibv()` 启用 RDMA，然后注册 RPC 服务并 `server.start()`。

这条「顶层 `CMakeLists.txt` → 子系统 `add_subdirectory` → `add_executable` → `main()`」的链路，就是从仓库结构追踪到一个可运行程序的标准路径。

#### 4.3.4 代码实践

**实践目标**：从构建脚本出发，追踪 Store 主控程序 `mooncake_master` 是如何被定义并启动的。

**操作步骤（源码阅读型实践，无需编译）**：

1. 打开顶层 [CMakeLists.txt:80-84](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/CMakeLists.txt#L80-L84)，确认 `WITH_STORE` 默认 `ON`，因此 `mooncake-store` 会被挂载。
2. 打开 [mooncake-store/src/CMakeLists.txt:271](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/src/CMakeLists.txt#L271)，看到 `add_executable(mooncake_master master.cpp)`——这告诉你产物名是 `mooncake_master`，源是 `master.cpp`。
3. 打开 [mooncake-store/src/master.cpp:951](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/src/master.cpp#L951) 的 `main()`，顺着读到最后，确认它在 HA 与非 HA 两条路径中分别启动了什么。

**需要观察的现象**：
- `main()` 里有一长串 `DEFINE_*`（gflags）声明，例如 [master.cpp:66-67](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-store/src/master.cpp#L66-L67) 定义了 `port`（默认 50051）等参数——这就是 `mooncake_master` 可用的命令行开关来源。
- 启动路径取决于 `enable_ha`：开则走 supervisor，不开则直接起 `coro_rpc_server`。

**预期结果**：你能用一句话回答「`mooncake_master` 这个程序从哪来、入口在哪、怎么启动」。

**进阶（可选，需本地编译环境）**：执行 README 给出的标准构建流程（`mkdir build && cd build && cmake .. && make -j`），构建完成后在 `build/` 下找到 `mooncake_master` 二进制；运行 `./mooncake_master --help` 查看本步骤观察到的那些 `DEFINE_*` 开关。若本地无 RDMA/CUDA 等环境，此步可跳过（待本地验证）。

> 参考 README 的构建说明：[README.md:262-279](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/README.md#L262-L279)（`git clone` → `dependencies.sh` → `cmake ..` → `make -j`）。

#### 4.3.5 小练习与答案

**练习 1**：默认配置下（`cmake ..` 不加任何 `-D`），哪些子系统会被编译？

**答案**：`mooncake-common`（无条件）、`mooncake-transfer-engine`（`WITH_TE=ON`）、`mooncake-store`（`WITH_STORE=ON`）、`mooncake-store/rust`（`WITH_STORE_RUST=ON`）以及 `mooncake-integration`。EP、PG、P2P Store 默认关闭。

**练习 2**：为什么 `WITH_STORE_RUST=ON` 时脚本会强制要求 `WITH_STORE=ON`？

**答案**：因为 Rust 绑定是对 Store C++ 库的封装（FFI），没有 Store 就没有可绑定 的底层实现。这个约束在 [CMakeLists.txt:86-92](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/CMakeLists.txt#L86-L92) 中以 `message(FATAL_ERROR ...)` 显式检查。

**练习 3**：`mooncake_master` 这个可执行文件名，是 README 写的，还是源码里定义的？

**答案**：是源码定义的，见 `mooncake-store/src/CMakeLists.txt` 的 `add_executable(mooncake_master master.cpp)`。这也是「构建入口」之所以重要的体现——产物名和入口文件都藏在子系统的 `CMakeLists.txt` 里。

## 5. 综合实践

**综合任务：手工绘制一份《Mooncake 组件地图》一页纸。**

请综合本讲三个模块的内容，完成以下产出：

1. **目录树（模块 4.1）**：在仓库根目录运行 `ls -d mooncake-*`，把结果画成树形图，每个 `mooncake-*` 目录后面写一行中文注解（职责）。
2. **依赖关系（模块 4.2）**：用箭头标出依赖方向，确认「common → TE → Store」「TE → P2P Store」「TE/PG → EP」这类关系。
3. **规模标注（模块 4.2）**：用 `find ... | wc -l` 统计每个子系统的 `.cpp`/`.h` 数量，把数字标在树形图旁边，挑出「最大」和「最小」的两个子系统。
4. **构建开关（模块 4.3）**：打开顶层 `CMakeLists.txt`，把每个 `mooncake-*` 目录对应的 `WITH_*` 开关及其默认值（ON/OFF）列成一张表。
5. **入口追踪（模块 4.3）**：任选 `mooncake_master`，写出它的「顶层开关 → 子系统挂载 → `add_executable` → `main()` 行号」完整链路。

**验收标准**：完成后，你应该能不查资料就回答出——「我想看 KVCache 的存储与淘汰逻辑去哪个目录？我想加一种新网络协议去哪个目录？我想只编译传输引擎不编译存储，该设哪个开关？」这三问的答案分别对应 `mooncake-store/`、`mooncake-transfer-engine/src/transport/`、`-DWITH_STORE=OFF`。

> 若本地无完整编译环境，第 4、5 步仍可纯靠阅读源码完成；只有「实际 `make`」这一动作需要环境（待本地验证）。

## 6. 本讲小结

- Mooncake 顶层由若干 `mooncake-*` 目录组成，每个目录是一个可独立构建的子系统；此外还有 `docs/`、`scripts/`、`docker/`、`extern/` 等支撑性目录。
- 自底向上的能力栈是：**common（公共）→ Transfer Engine（传输底座）→ Store（分布式 KVCache 存储）→ EP/PG（MoE 并行与进程组）**；P2P Store 与 RL 示例是建在 TE/Store 之上的旁路应用。
- 各子系统内部普遍遵循 `include/` + `src/` + `tests/` + `CMakeLists.txt` 的固定布局，记住它就能快速定位任何 `mooncake-*` 目录。
- 顶层 `CMakeLists.txt` 用一组 `WITH_*` 开关 + `add_subdirectory` 决定「编译什么」；TE 与 Store 默认开，EP/PG/P2P 默认关。
- EP、PG、P2P Store 是多语言的：EP/PG 靠 CUDA 内核 + Python 扩展构建，P2P Store 是 Go 实现——**不能仅凭 `.cpp` 文件数判断一个子系统的体量**。
- 构建入口最终会落到子系统的 `CMakeLists.txt` 中的 `add_executable`（如 `mooncake_master`），再到具体的 `main()`——这是从仓库结构追踪到可运行程序的标准路径。

## 7. 下一步学习建议

有了组件地图之后，建议按以下顺序深入：

1. **先攻 Transfer Engine**：它是整个项目的基石，理解了「批量数据搬运 + 多协议 + 拓扑感知」，后面的 Store 才讲得通。可先读 `mooncake-transfer-engine/include/transfer_engine.h` 与 `src/transfer_engine.cpp`。
2. **再读 Store 的主控**：以本讲追踪到的 `mooncake-store/src/master.cpp` 的 `main()` 为起点，看 master 如何注册 RPC、管理 segment 与淘汰。
3. **按需看 EP/PG**：只有当你关心大规模 MoE 推理的容错时才深入 `mooncake-ep/`、`mooncake-pg/`。
4. **下一讲建议**：进入对 **Transfer Engine 内部结构**（metadata、transport、topology）的专题讲解，把本讲的「地图」细化为「TE 的内部地图」。

在继续之前，建议你亲手完成第 5 节的综合实践——把这张组件地图内化成自己的肌肉记忆，后续阅读任何具体实现时都能随时定位「我现在在地图的哪一层」。
