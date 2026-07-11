# 代码目录结构与组织

> 本讲属于「入门层：从零认识 LMCache」，承接 [u1-l1 LMCache 是什么](u1-l1-project-overview.md) 与 [u1-l2 安装、构建与运行方式](u1-l2-install-and-run.md)。
> 前两讲解决了「LMCache 是什么」和「怎么装、怎么跑」，本讲解决「代码长什么样、东西放在哪」。

## 1. 本讲目标

学完本讲，你应当能够：

1. 画出 `lmcache/` 顶层模块树，并用一句话说出每个目录的职责。
2. 区分两套并存的组织方式：新的 `lmcache/v1/` 架构与残留的 legacy `lmcache/storage_backend/`。
3. 准确定位四类代码所在位置：引擎核心、推理引擎集成层 `integration/`、面向应用的 `sdk/`、命令行 `cli/`。
4. 在 `lmcache/v1/` 的几十个子目录里，一眼挑出属于「新多进程（MP）架构」的三个目录。

## 2. 前置知识

### 2.1 什么是 Python「包」与「模块」

- 一个 `.py` 文件就是一个 **模块（module）**。
- 一个含有 `__init__.py` 的目录就是一个 **包（package）**，包可以嵌套（`lmcache.v1.storage_backend`）。
- `__init__.py` 是包的「入口脚本」，**当你 `import` 这个包时，它会被最先执行**。因此很多初始化逻辑（比如设备检测、版本号、日志器）都写在顶层包的 `__init__.py` 里。

> 所以读懂 `lmcache/__init__.py`，就等于读懂了「`import lmcache` 之后到底发生了什么」。

### 2.2 为什么要区分 v1 与 legacy

回顾 [u1-l1](u1-l1-project-overview.md)：LMCache 正处在从旧架构向新架构演进的阶段。仓库里同时存在：

- **新架构** `lmcache/v1/`：当前主力代码，包含引擎、分布式存储、多进程架构等。
- **legacy** `lmcache/storage_backend/`：旧存储后端代码，目前**只剩下一个 `serde/` 子目录**（KV 序列化），其余存储逻辑都已迁移到 `v1/`。

这不是「重复」，而是「迁移尚未完成的痕迹」。看代码时，**默认进 `v1/`**，只有遇到 `cachegen` 相关的序列化逻辑才需要回头看 legacy `storage_backend/serde/`。

### 2.3 「镜像」约定：docs/design 与 lmcache/ 一一对应

CLAUDE.md 与 `docs/design/README.md` 规定：**`docs/design/` 目录镜像 `lmcache/` 包树**。也就是说，源码 `lmcache/<path>/` 对应设计文档 `docs/design/<path>/`。

例如：

| 源码模块 | 设计文档位置 |
|---|---|
| `lmcache/cli/` | `docs/design/cli/` |
| `lmcache/v1/distributed/l2_adapters/` | `docs/design/v1/distributed/l2_adapters/` |

所以读任何一段代码前，**先看 `docs/design/` 下有没有同路径的设计文档**。详见 [docs/design/README.md:7-12](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/docs/design/README.md#L7-L12)。并非每个模块都有设计文档，缺失目录只表示「还没有独立文档」。

## 3. 本讲源码地图

本讲主要在「目录层面」阅读，下面是会逐一讲解的关键位置：

| 路径 | 类型 | 作用 |
|---|---|---|
| `lmcache/__init__.py` | 文件 | 顶层包入口：设备检测、算子后端加载、版本号 |
| `lmcache/v1/` | 目录 | **新架构核心**，本仓库绝大部分活跃代码 |
| `lmcache/v1/__init__.py` | 文件 | 几乎为空，仅作命名空间标记 |
| `lmcache/storage_backend/` | 目录 | legacy 残留，仅剩 `serde/` |
| `lmcache/integration/` | 目录 | 与 vLLM / SGLang / TensorRT-LLM 的集成层 |
| `lmcache/sdk/` | 目录 | 面向应用的高层封装（SDK） |
| `lmcache/cli/` | 目录 | `lmcache` 命令行工具 |
| `examples/README.md` | 文件 | 按 Tier 分类的可运行示例索引，是「读目录」的最佳辅助地图 |

## 4. 核心概念与源码讲解

本讲把目录拆成 **4 个最小模块** 来讲，对应 `__init__.py` → `v1/` → `integration/` → `sdk/+cli/`。

---

### 4.1 顶层包入口 lmcache/__init__.py：设备检测与算子后端

#### 4.1.1 概念说明

`import lmcache` 是所有使用方（vLLM 集成、SDK、CLI、server）的起点。这个包入口只做一件**最重要的事**：**在导入期就把「当前机器用什么硬件、用什么算子后端」决定下来**。

它解决两个问题：

1. **硬件中立**：同一份代码要能跑在 CUDA / Intel XPU / 华为 HPU / 摩尔线程 MUSA / 纯 CPU 上，需要一个统一的设备抽象。
2. **算子降级**：性能关键路径用 C++/CUDA 扩展（`c_ops`），但无 GPU 或无 torch 的环境（比如纯 CLI 诊断）要能退化为 Python 实现，不能直接崩。

#### 4.1.2 核心流程

`import lmcache` 时的执行顺序：

```text
1. 导入 platform 的三个符号：get_backend / torch_dev / torch_device_type
2. 读取 __version__
3. 调用 get_backend(torch_device_type) 探测算子后端
   ├── 拿到后端 → 用它覆盖 sys.modules["lmcache.c_ops"]
   └── 拿不到(None) → 警告并进入 "CLI-only mode"
```

关键点：第 3 步用 `sys.modules` 替换是一种 **monkey patch**——所有 `import lmcache.c_ops` 的地方，实际上拿到的是被替换后的合并模块（以 Python fallback 为基类，有硬件实现就覆盖）。

#### 4.1.3 源码精读

先看导入期就执行的设备抽象引入：

[lmcache/__init__.py:12-14](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/__init__.py#L12-L14) —— 顶层包从 `lmcache.v1.platform` 引入三个统一符号：`get_backend`（按设备取算子后端）、`torch_dev`（统一设备对象）、`torch_device_type`（设备类型字符串）。注意：**设备抽象本身也定义在新架构 `v1/platform/` 下**，legacy 代码反而要依赖 v1 的 platform，这说明 platform 已经是新架构的公共底座。

再看决定运行模式的后端加载逻辑：

[lmcache/__init__.py:26-36](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/__init__.py#L26-L36) —— `_ops = get_backend(torch_device_type)` 探测后端：若成功，把合并模块塞进 `sys.modules["lmcache.c_ops"]`（Python fallback 在前、硬件实现在后覆盖）；若返回 `None`，则打印警告，进入「CLI-only mode（未安装 torch/numba）」。这正是为什么无 GPU 主机也能跑 `lmcache` 诊断命令。

> 小贴士：`python_ops_fallback.py` 就是上面提到的「Python fallback 基类」，注释里写明它是 CUDA 算子的 Python 实现版本，见 [lmcache/python_ops_fallback.py:1-4](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/python_ops_fallback.py#L1-L4)。

#### 4.1.4 代码实践

1. **实践目标**：亲手观察「设备检测 + 后端覆盖」在导入期发生。
2. **操作步骤**：
   - 在仓库根目录执行 `python -c "import lmcache; print(lmcache.torch_device_type); print(lmcache.__version__)"`。
   - 再执行 `python -c "import sys, lmcache; print(sys.modules.get('lmcache.c_ops'))"`，看 `c_ops` 被替换成了什么模块。
3. **需要观察的现象**：第一条命令会打印当前设备类型（如 `cuda` 或 `cpu`）和版本号；第二条会打印一个模块对象，而非报错。
4. **预期结果**：有 GPU + torch 时 `torch_device_type` 为 `cuda`；纯 CPU / 无 torch 时会看到 4.1.3 中那条 CLI-only 警告，且 `c_ops` 退化到 Python fallback。
5. **若无法确定运行结果**：待本地验证（取决于本机是否安装 torch 与 GPU）。

#### 4.1.5 小练习与答案

**练习 1**：为什么顶层 `lmcache/__init__.py` 要从 `lmcache.v1.platform` 而不是从某个 legacy 模块引入设备符号？
**答案**：因为设备抽象（`platform/`）已经迁移到新架构，成为整个仓库（含 legacy）的公共底座；统一从 `v1/platform` 引入，能保证 legacy 与 v1 看到的是同一个设备视图。

**练习 2**：`sys.modules["lmcache.c_ops"] = _ops` 这一行如果不执行，会发生什么？
**答案**：其它模块 `import lmcache.c_ops` 时拿到的就不是合并后的后端，可能导致缺少硬件加速实现或直接 `ImportError`；这行 monkey patch 是「优先用硬件实现、否则用 Python fallback」的关键。

---

### 4.2 新架构核心 lmcache/v1/（及与 legacy storage_backend 的关系）

#### 4.2.1 概念说明

`lmcache/v1/` 是仓库的**主战场**。它把 KV cache 管理拆成若干职责清晰的子目录：引擎核心、存储后端、分布式存储、设备/内存、计算与编解码、传输通道、多进程架构、服务与协议等。

一个容易忽略的事实：[lmcache/v1/__init__.py:1-2](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/v1/__init__.py#L1-L2) **几乎是空的**（只有 SPDX 许可头）。也就是说，`v1/` 的「意义」不在它的 `__init__.py`，而在它的**子目录树本身**。所以本模块的「源码精读」其实是「子目录精读」。

> 与 legacy 的关系：旧存储后端 `lmcache/storage_backend/` 现在只剩 `serde/`（见 4.2.3 末尾）。**新增/改动存储逻辑一律在 `v1/` 内**。

#### 4.2.2 核心流程：按职责给 v1/ 子目录分组

`v1/` 下子目录很多，先按功能分成 8 组来记：

| 组别 | 子目录 / 文件 | 一句话职责 |
|---|---|---|
| **① 多进程（MP）架构（新）** | `multiprocess/` `mp_coordinator/` `mp_observability/` | 把 KV cache 管理独立成 daemon，并跨实例协调与观测 |
| ② 引擎核心 | `cache_engine.py` `cache_interface.py` `ec_engine.py` `manager.py` | LMCacheEngine 及其 store/retrieve/lookup 公共 API |
| ③ 配置 | `config.py` `config_base.py` | 动态生成配置类、环境变量与 YAML 覆盖 |
| ④ 存储 | `storage_backend/` `distributed/` | v1 存储后端；新的分布式 L1/L2 存储架构 |
| ⑤ 设备与内存 | `platform/` `gpu_connector/` `memory_management.py` `memory_allocators/` | 多硬件抽象、GPU 连接器、内存格式与分配器 |
| ⑥ 计算与编解码 | `compute/` `kv_codec/` | CacheBlend 非前缀复用、attention、KV 编解码 |
| ⑦ 传输 | `transfer_channel/` | PD 分离场景下的 KV 传输通道 |
| ⑧ 服务与协议 | `server/` `api_server/` `internal_api_server/` `offload_server/` `protocol.py` `rpc/` | 独立服务进程与二进制/HTTP/RPC 协议 |

> 记忆口诀：**「MP 架构」= multiprocess + mp_coordinator + mp_observability 三个 `mp` 开头的目录**（外加 `multiprocess`），它们就是大纲第三单元要专题讲的内容。

#### 4.2.3 源码精读（子目录逐组点名）

**① 多进程（MP）架构（新）**——这是项目最新、最重要的方向：

- `multiprocess/`：per-worker 的 MP runtime，含 server/client、消息队列 `mq.py`、`futures.py`、CUDA IPC / 共享内存 `posix_shm.py`、`protocol.py`。
- `mp_coordinator/`：作为独立 uvicorn 服务的「跨实例协调器」，含 `app.py`、`registrar.py`、`registry.py`、`blend_directory.py`，负责 peer 发现与 blend lookup。
- `mp_observability/`：MP 架构的事件总线、metrics、trace 子系统（自带 `README.md`）。

**④ 存储**——注意区分两层存储代码：

- `v1/storage_backend/`：单机分层后端，含 `abstract_backend.py`、`local_cpu_backend.py`、`local_disk_backend.py`、`remote_backend.py`、`pd_backend.py`、`gds_backend.py`、`nixl_storage_backend.py`、`p2p_backend.py`，以及编排它们的 `storage_manager.py`。
- `v1/distributed/`：**更新的**分布式 L1/L2 存储架构，含 `api.py`、`tiers.py`、`storage_controller.py`、`storage_controllers/`、`l2_adapters/`（十多种远端后端）、`serde/`、`eviction_policy/`、`quota_manager.py`。

> 对比 legacy：`lmcache/storage_backend/` 旧目录现在只剩 `serde/`（`cachegen` 序列化：`serde.py`、`cachegen_encoder.py`、`cachegen_decoder.py`、`cachegen_basics.py`），其余均已迁出。这是「v1 vs legacy」最直观的体现。

**⑤ 设备与内存**：

- `platform/`：按硬件分子目录注册——`cpu/` `cuda/` `hpu/` `musa/` `xpu/`，外加 `_registry.py` 注册表。
- `gpu_connector/`：把不同引擎/硬件的 KV 布局抽象成统一接口，含 `gpu_connectors.py` 与 `kv_format/` 布局检测。
- `memory_allocators/`：多种分配器（`mixed_`、`paged_tensor_`、`pin_`、`lazy_`、`host_` 等）。

**⑥ 计算与编解码**：

- `compute/`：`blend/`（CacheBlend 非前缀复用）、`attention/`、`models/`、`positional_encoding.py`。
- `kv_codec/`：`asym_k16_v8.py` 等编解码。

**② 引擎核心**（本组先记名字，[u1-l6](u1-l6-engine-public-api.md) 会精讲）：

- `cache_engine.py`：`LMCacheEngine` 主类所在地。
- `cache_interface.py`：对外接口与请求类型（如 `LMCacheModelRequest`）。
- `config.py` / `config_base.py`：配置系统（[u1-l5](u1-l5-configuration-system.md) 精讲）。

#### 4.2.4 代码实践（本讲的主实践）

1. **实践目标**：用 `ls` 把 `v1/` 一级子目录列出来，给每个子目录写一句话职责，并标出「新 MP 架构」三个目录。
2. **操作步骤**：
   ```bash
   ls -1 lmcache/v1/
   ```
   对照 4.2.2 的分组表，把输出里的每个条目归到 ① ~ ⑧ 之一。
3. **需要观察的现象**：你会看到约 40 个条目（目录 + 少量 `.py` 文件），其中能明确找到 `multiprocess`、`mp_coordinator`、`mp_observability` 三个 `mp` 相关目录。
4. **预期结果**：手绘一张表，左列是 `ls` 的每一项，右列是「① MP 架构 / ② 引擎核心 / … / ⑧ 服务与协议 / 工具类」；`multiprocess` `mp_coordinator` `mp_observability` 三项必须标注「★ 新 MP 架构」。
5. **若无法确定运行结果**：本实践只读目录，结果稳定可复现，无需 GPU。

#### 4.2.5 小练习与答案

**练习 1**：仓库里同时存在 `lmcache/v1/storage_backend/` 和 `lmcache/storage_backend/`，它们是什么关系？
**答案**：前者是新架构的单机分层后端（含 abstract/local_cpu/local_disk/remote/pd/gds/nixl/p2p 等），是当前主力；后者是 legacy 残留，目前只剩 `serde/`（cachegen 序列化）。看存储代码默认进 `v1/storage_backend/`。

**练习 2**：`v1/distributed/` 与 `v1/storage_backend/` 都是存储相关，为什么要分开？
**答案**：`storage_backend/` 偏「单机分层后端 + storage_manager 编排」；`distributed/` 是更新的「L1/L2 两级分布式存储」架构，引入了 `tiers.py`、`l2_adapters/`、`eviction_policy/`、`quota_manager.py` 等跨节点/多租户能力，是更上层的存储抽象。

---

### 4.3 引擎集成层 lmcache/integration/

#### 4.3.1 概念说明

LMCache 本身**不是推理引擎**（见 [u1-l1](u1-l1-project-overview.md)），它要「贴」到 vLLM、SGLang、TensorRT-LLM 等引擎上才能工作。`lmcache/integration/` 就是这些**对接代码**的归宿。

每个子目录对应一个推理引擎：引擎会暴露钩子（比如 vLLM 的 KVConnector 框架），LMCache 在钩子里实现「存 KV / 取 KV / 查命中」。

#### 4.3.2 核心流程

```text
推理引擎（vLLM / SGLang / TensorRT-LLM）
        │  暴露 KV cache 相关钩子
        ▼
integration/<engine>/   ← 本模块：把 LMCacheEngine 接到引擎钩子上
        │  调用
        ▼
lmcache/v1/cache_engine.py  （LMCacheEngine 公共 API）
```

#### 4.3.3 源码精读

`lmcache/integration/` 的一级结构：

- `vllm/`：对接 vLLM（当前主力），含 `lmcache_connector_v1.py`、`vllm_v1_adapter.py`、`vllm_multi_process_adapter.py`、`lmcache_mp_connector*.py`（多版本兼容），以及面向旧版的 `lmcache_connector_v1_085.py`。
- `sglang/`：对接 SGLang。
- `tensorrt_llm/`：对接 TensorRT-LLM。
- `base_service_factory.py`：服务工厂抽象（不同引擎的集成共享一套服务装配逻辑）。
- `request_telemetry/`：请求级遥测。
- `integration/__init__.py`：基本为空，仅作命名空间，见 [lmcache/integration/__init__.py:1-2](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/integration/__init__.py#L1-L2)。

> 想确认某个引擎怎么用？去看 `examples/`。`examples/README.md` 把示例按 Tier 分类，其中明确列出了 SGLang 集成等框架对接示例，见 [examples/README.md:62-70](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/examples/README.md#L62-L70)（Tier 4 — Framework Integrations）。

#### 4.3.4 代码实践

1. **实践目标**：建立「集成代码 ↔ 示例 ↔ 引擎」三者的对应关系。
2. **操作步骤**：
   - 执行 `ls -1 lmcache/integration/` 与 `ls -1 lmcache/integration/vllm/`。
   - 在 `examples/README.md` 里搜索 `sgl_integration`，找到它对应哪个集成子目录。
3. **需要观察的现象**：`integration/vllm/` 里文件最多（主力引擎），而 `sglang/`、`tensorrt_llm/` 相对小；示例目录与集成目录名字大致对应。
4. **预期结果**：列出一张表——「引擎名 → integration/ 子目录 → examples/ 示例」，至少填入 vLLM 与 SGLang 两行。
5. **若无法确定运行结果**：本实践为只读目录 + 读 README，结果稳定。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `integration/vllm/` 里有 `lmcache_mp_connector_0180.py`、`lmcache_mp_connector_0201.py` 这样带版本号的文件？
**答案**：不同 vLLM 版本的 KVConnector 接口有差异，LMCache 用按版本分文件的方式做兼容，每个文件对应一个 vLLM 版本的 MP 连接器实现。

**练习 2**：如果我新增了对「TGI」引擎的支持，代码应该放在哪？
**答案**：在 `lmcache/integration/` 下新建 `tgi/` 子目录，实现该引擎钩子到 `LMCacheEngine` 的适配；必要时在 `base_service_factory.py` 的框架内复用装配逻辑。

---

### 4.4 应用层 lmcache/sdk/ 与 lmcache/cli/

#### 4.4.1 概念说明

`sdk/` 与 `cli/` 都面向「使用方」，但形态不同：

- `sdk/`：**给程序调用的库**，提供高层封装，应用代码 `from lmcache.sdk import ...` 即可使用。
- `cli/`：**给人调用的命令行**，对应 [u1-l2](u1-l2-install-and-run.md) 里注册的 `lmcache` 入口脚本。

#### 4.4.2 核心流程

`cli/` 的执行链：

```text
pyproject.toml [project.scripts] lmcache
        │
        ▼
lmcache/cli/main.py: main()       ← 注册到 argparse
        │  遍历 ALL_COMMANDS
        ▼
lmcache/cli/commands/<cmd>.py     ← 每个子命令一个模块
```

#### 4.4.3 源码精读

先看 SDK 的对外导出。`sdk/` 目录有 `batch.py`、`kvcache.py`、`stream.py` 与 `wrapper/`，对应用户最常用的高层接口；其包入口明确导出 `LMCacheKVCacheContext` 与 `KVCacheSDKError`：

[lmcache/sdk/__init__.py:4-10](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/sdk/__init__.py#L4-L10) —— `Public LMCache SDK helpers`，从 `lmcache.sdk.kvcache` 导出 `KVCacheSDKError`、`LMCacheKVCacheContext` 两个公共符号。这是「应用层最薄的一层入口」。

再看 CLI 的派发逻辑。CLI 入口遍历一个**显式注册**的命令清单 `ALL_COMMANDS`（注意：是显式清单，不是自动扫描子类）：

[lmcache/cli/main.py:20-31](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/cli/main.py#L20-L31) —— `main()` 先打印 banner，构造 argparse，再用 `for cmd in ALL_COMMANDS: cmd.register(subparsers)` 把每个子命令挂上去；命令清单来自 [lmcache/cli/main.py:14](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/lmcache/cli/main.py#L14) 处 `from lmcache.cli.commands import ALL_COMMANDS`。

`cli/commands/` 下每个文件就是一个子命令模块（如 `ping.py`、`coordinator.py`、`server.py`、`describe.py`、`kvcache.py`，以及 `bench/`、`query/`、`quota/`、`trace/`、`tool/` 等子命令组）。

> 区分三个「应用层入口」：`sdk/`（库，给程序调用）、`cli/`（`lmcache` 命令，给人用）、以及 [u1-l2](u1-l2-install-and-run.md) 里另外注册的 `lmcache_server` / `lmcache_controller` 两个 daemon 服务脚本（实现在 `v1/server/` 与 `v1/mp_coordinator/` 一带，详见 [u1-l4](u1-l4-entry-points.md)）。

#### 4.4.4 代码实践

1. **实践目标**：把「命令行里看到的子命令」与「源码里的命令模块」对上号。
2. **操作步骤**：
   - 执行 `lmcache --help`（若未安装则 `python -m lmcache.cli.main --help` 或直接读 `cli/main.py`）。
   - 执行 `ls -1 lmcache/cli/commands/`，把帮助里出现的子命令名与目录里的文件/子目录对应起来。
   - 再执行 `python -c "from lmcache.sdk import LMCacheKVCacheContext; print(LMCacheKVCacheContext)"`，确认 SDK 导出可用。
3. **需要观察的现象**：`--help` 列出的子命令数量与 `cli/commands/` 里的命令模块数量大致吻合；SDK 符号能被导入。
4. **预期结果**：写出「子命令 → 模块文件」对照表；记录 SDK 导出对象的完整路径。
5. **若无法确定运行结果**：`lmcache --help` 是否可用取决于是否已 `pip install`（见 [u1-l2](u1-l2-install-and-run.md)）；未安装时可直接读源码完成对照。

#### 4.4.5 小练习与答案

**练习 1**：`cli/main.py` 是怎么决定有哪些子命令的？
**答案**：通过显式清单 `ALL_COMMANDS`（从 `lmcache.cli.commands` 导入），`main()` 用 `for cmd in ALL_COMMANDS: cmd.register(subparsers)` 把它们逐个挂到 argparse 上，而非自动扫描目录。

**练习 2**：一个应用想在 Python 代码里直接复用 LMCache，应该 import `lmcache.cli` 还是 `lmcache.sdk`？
**答案**：用 `lmcache.sdk`（库入口，导出 `LMCacheKVCacheContext` 等）。`lmcache.cli` 是命令行，给人用、不适合在程序里调用。

---

## 5. 综合实践：画一张完整的目录地图

把本讲四个最小模块串起来，画一张「使用方 → 入口 → 目录」的全景图。

**实践目标**：用自己的话画出 LMCache 的目录组织全景图，并能解释「为什么这么分」。

**操作步骤**：

1. 执行下面的命令，收集全部一手信息：
   ```bash
   ls -1 lmcache/
   ls -1 lmcache/v1/
   ls -1 lmcache/integration/
   ls -1 lmcache/sdk/ lmcache/cli/ lmcache/cli/commands/
   ls -1 lmcache/storage_backend/        # 确认 legacy 只剩 serde
   ```
2. 画一张树状图，顶层是 `lmcache/`，向下分出：`__init__.py`（入口/设备检测）、`v1/`（新架构，标注 8 个组别）、`integration/`（按引擎分）、`sdk/`（库）、`cli/`（命令行）、`storage_backend/`（legacy，标注「仅剩 serde」）。
3. 在 `v1/` 子树里用 `★` 标出三个「新 MP 架构」目录：`multiprocess`、`mp_coordinator`、`mp_observability`。
4. 在图上用箭头标出调用方向：`integration/ → v1/cache_engine`、`sdk/ → v1/cache_engine`、`cli/main.py → cli/commands/*`。

**需要观察的现象**：你会直观看到 `v1/` 体量远大于其它目录；legacy `storage_backend/` 只剩一个子目录；`integration/` 按引擎切分。

**预期结果**：一张可保存、可维护的「目录地图」，后续读任何模块前先在这张图上定位。

**若无法确定运行结果**：本实践全部为只读目录操作，结果稳定可复现，无需 GPU 或额外依赖。

## 6. 本讲小结

- `lmcache/__init__.py` 是顶层入口：在导入期完成**设备检测**与**算子后端加载**，无 GPU/无 torch 时进入 CLI-only 模式。
- 真正的活跃代码在 `lmcache/v1/`，可按 8 个功能组记忆；其中 `multiprocess/`、`mp_coordinator/`、`mp_observability/` 三个目录是**新多进程（MP）架构**。
- legacy `lmcache/storage_backend/` 现在只剩 `serde/`，新增存储逻辑一律进 `v1/`（`v1/storage_backend/` 单机分层、`v1/distributed/` 分布式 L1/L2）。
- `lmcache/integration/` 按推理引擎切分（vLLM / SGLang / TensorRT-LLM），负责把 `LMCacheEngine` 接到各引擎钩子上。
- `lmcache/sdk/` 是面向程序的库入口（导出 `LMCacheKVCacheContext` 等），`lmcache/cli/` 是命令行（`main()` 遍历显式清单 `ALL_COMMANDS`）。
- `docs/design/` 镜像 `lmcache/` 包树，读代码前先在同路径找设计文档。

## 7. 下一步学习建议

- 想搞清楚「三个进程入口脚本分别落到哪段代码」→ 下一讲 [u1-l4 进程入口与启动方式](u1-l4-entry-points.md)。
- 想了解配置怎么生成与覆盖 → [u1-l5 配置系统](u1-l5-configuration-system.md)。
- 想看到第一条 store/retrieve 调用链 → [u1-l6 LMCacheEngine 公共 API](u1-l6-engine-public-api.md)。
- 建议同步翻阅 `examples/README.md`，按 Tier 1 → Tier 4 顺序跑示例，把本讲的「目录地图」和「可运行示例」一一对应起来。
