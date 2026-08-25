# u7-l1 OmniCache 原理与部署

## 1. 本讲目标

本讲是 omni-cache 单元的第一讲。学完后你应该能够：

1. 说清 OmniCache 的 KV 卸载模型：KV Cache 如何从 P 节点 HBM 卸载到主机内存、经网络传输、再被 D 节点加载，以及这与 u4-l2 精读的 LLMDataDistConnector 直传路径有何不同。
2. 解释主机内存池（hugetlbfs）作为中间缓存层的价值：为什么它能同时提升「最大序列长度」「并发数」和「多轮对话的 APC 命中率」。
3. 说出 producer（Prefill）与 consumer（Decode）两侧环境变量的差异，理解 `ENABLE_OMNI_CACHE`、`ENABLE_HOST_MAPPING` 等开关的含义。
4. 掌握 hugepage 的准备步骤，能独立使用 `tools/setup/` 下的两个脚本完成主机内存池预留。
5. 按用户指南在两个容器里手工拉起 producer 与 consumer，发送多轮对话请求并从日志确认 KV 命中。

本讲只讲「是什么、怎么部署」；OmniCacheConnector 四个协作类的源码精读留给下一讲 u7-l2。

## 2. 前置知识

阅读本讲前，你需要具备以下认知（均来自前置讲义）：

- **PD 分离与 KV 传输配置**（u4-l1/u4-l2）：Prefill 节点算 KV Cache，Decode 节点拿着 KV 逐 token 生成；`--kv-transfer-config` 是一个 JSON，含 `kv_connector`、`kv_role`、`kv_rank`、`kv_parallel_size` 四个顶层字段；本仓库基线部署用 `LLMDataDistConnector` 走 RoCE 直传 KV。
- **vLLM 插件机制**（u2-l1）：omni-npu 通过 pyproject.toml 的 entry points 被零侵入加载；`VLLM_PLUGINS` 环境变量逐字点名要加载的插件。omni-cache 用的是同一套机制。
- **HBM 与主机内存**：昇腾 NPU 卡上的高带宽显存叫 HBM，容量有限（通常几十 GB 量级）且要同时放模型权重和 KV Cache；服务器主机动辄数百 GB 到数 TB，但访问慢。KV Cache 管理的本质就是在「快而小」与「大而慢」之间搬数据。
- **APC（前缀缓存）**：多轮对话中，第二轮的 prompt 包含第一轮的全部内容，若这些 token 的 KV 还在缓存里就无需重算，这叫前缀命中（u6-l2 的 radix tree 就是干这个的）。
- **术语澄清**：本仓库文档里的「OX」指 omni-cache 内部的 KV 传输引擎（源码位于 `omni_cache/cache/transfer_engine/`），「hugetlbfs」是 Linux 的大页文件系统，下一节详述。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `components/omni-cache/README.md` | 组件门面文档：定位、安装、快速开始 |
| `components/omni-cache/docs/USER_GUIDE.md` | 用户指南：环境准备、启动流程、常见问题 |
| `components/omni-cache/docs/CONFIG_REFERENCE.md` | 配置参考：全部环境变量与 kv-transfer-config 字段 |
| `components/omni-cache/pyproject.toml` | 插件注册声明（entry points） |
| `components/omni-cache/omni_cache/plugin.py` | 插件统一注册入口 `register()` |
| `components/omni-cache/omni_cache/connector/connector.py` | `OmniCacheConnector` 门面类（本讲只看骨架） |
| `components/omni-cache/omni_cache/connector/scheduler/decode.py` | D 侧调度器，含 KV 命中日志 |
| `components/omni-cache/omni_cache/reuse_rate_logger.py` | KV 复用率 Prometheus 指标 |
| `components/omni-cache/tools/setup/set_hugepage_limit.sh` | 调整内核 2MB 大页数量 |
| `components/omni-cache/tools/setup/setup_hugetlbfs_2MB.sh` | 预留大页 + 挂载 hugetlbfs + 建池文件 |
| `components/omni-cache/examples/run_server_p.sh` / `run_server_d.sh` | 最小 PD 启动骨架（注意：基线 LLMDataDist 模式） |
| `components/omni-cache/examples/pangu_v2_pd/launch_prefill.sh` / `launch_decode.sh` | OmniCache 模式的一键启动脚本（Pangu V2 hybrid） |
| `components/omni-cache/examples/pangu_v2_pd/configs/base.sh` | 启动脚本的默认配置基座 |
| `components/omni-cache/examples/pangu_v2_pd/README.md` | 单节点 1P1D 拓扑与变量覆盖表 |

一个必须先说的**事实澄清**：讲义规格让读者「参照 `examples/run_server_p.sh` 与 `run_server_d.sh`」，但通读源码后确认，这两个脚本写死的是 `LLMDataDistConnector`（基线直传模式），并不是 OmniCache 模式。真正体现 OmniCache 全部启动参数的，是 `examples/pangu_v2_pd/` 下的 `launch_prefill.sh` / `launch_decode.sh`（USER_GUIDE 第四节也明确指向它们）。本讲把前者当作「最小 PD 骨架」讲清结构，把后者当作「OmniCache 完整参照」讲清参数。

## 4. 核心概念与源码讲解

### 4.1 KV 卸载模型

#### 4.1.1 概念说明

回忆 u4-l2 的基线数据路径：P 节点在 HBM 里算出 KV，`LLMDataDistConnector` 通过 RoCE 把 KV 块直接搬到 D 节点的 HBM。这条路径有两个代价：

1. P 侧算完的 KV 在「等 D 来取」期间一直占用 HBM——基线靠延迟释放缓解，但块终究要在两侧 HBM 各存一份；
2. HBM 容量是硬上限，KV 装不下，调度器就必须抢占或拒绝请求，直接限制最大序列长度与并发数。

OmniCache 的思路是在两侧 HBM 之外引入**主机内存池作为中间层**。README 里一句话说清了数据路径：

> Prefill 完成后，KV Cache 从 HBM 卸载到主机内存并通过 OX 发送；Decode 接收后从主机内存加载到 HBM 完成推理。

由此得到三条收益（README 同段）：

- 主机内存池显著降低 P/D 两侧 KV 对 HBM 的压力 → **序列长度与并发数提升**；
- KV 在主机内存里**持久化**（不随请求结束立刻丢弃）→ 多轮对话场景 **APC 命中率大幅提升**；
- D 侧还可以更进一步：`ENABLE_HOST_MAPPING=1` 时连「加载到 HBM」都省了，DSA 的 indexer 留在 HBM，其余 KV 由 NPU 通过 MMU 直接从主机内存读——HBM 压力再降一档。

用一张数据路径对比图概括：

```text
基线 LLMDataDist：
  P: HBM(KV) ──RoCE──> D: HBM(KV)

OmniCache：
  P: HBM(KV) ──卸载──> P: 主机内存池(hugetlbfs) ──OX──> D: 主机内存池 ──加载/MMU直读──> D: HBM
                        │                                                │
                        └──────────── 持久化，供后续请求 APC 命中 ────────────┘
```

#### 4.1.2 核心流程

一次多轮对话在 OmniCache 模式下的完整生命周期：

1. **第 1 轮请求**：客户端 → proxy → 路由到 P。P 完成 prefill，KV 写入 HBM。
2. **卸载（offload）**：P 侧 connector 把 KV 从 HBM 拷贝到本机 hugetlbfs 内存池（`/dev/hugepages/omni_cache_p` 文件背后的 2MB 大页）。
3. **传输**：D 侧通过 OX 引擎按块拉取（P 侧监听 `BASE_PORT=16077` 起始的数据端口，控制面走 `ZMQ_BASE_PORT=16555` 起始的 ZMQ）。
4. **加载或直读**：D 侧把收到的 KV 放进本机主机内存池（`omni_cache_d`）；`ENABLE_HOST_MAPPING=1` 时 attention kernel 经 NPU MMU 直接读主机内存，仅 DSA indexer 留 HBM。
5. **D 解码**：D 只需对新 token 做 decode，生成结果返回客户端。
6. **第 2 轮请求**：prompt 含第 1 轮全部内容。KV 仍在两侧主机池中持久保存，P 侧可复用已算 KV（少算 prefill），D 侧直接命中已传 KV——这就是「多轮对话 APC 命中率提升」的来源。

容量直觉可以用一个粗略估算表达。Pangu V2 hybrid 的 DSA 注意力每 token 的 KV 开销为 576（kv_lora + k_pe）+ 128（indexer）≈ 704 字节（含 SWA 对齐），则主机池可缓存的 token 量级为：

\[
N_{\text{token}} \approx \frac{\text{MAP\_SIZE\_BYTES}}{704\ \text{B/token}} = \frac{536{,}870{,}912{,}000}{704} \approx 7.6 \times 10^{8}
\]

即默认 500 GiB 主机池约可承载 7.6 亿 token 的 KV——这是任何单卡 HBM 都给不出的量级（此为量级估算，实际受布局与对齐影响）。

#### 4.1.3 源码精读

**（1）定位陈述：README 的两句话**

[components/omni-cache/README.md:L13-L17](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/README.md#L13-L17)

```text
OmniCache 是面向 vLLM 的 PD 分离 KV Cache 管理插件。它在 Prefill 节点与 Decode 节点之间
建立高效的 KV Cache 传输通道：Prefill 完成后，KV Cache 从 HBM 卸载到主机内存并通过 OX 发送；
Decode 接收后从主机内存加载到 HBM 完成推理。
核心优势：以主机内存池（hugetlbfs）作为中间缓存层……
```

这段是整个组件的「宪法」，后面所有配置都服务于这两句话。

**（2）插件如何被 vLLM 发现：entry points**

[components/omni-cache/pyproject.toml:L38-L43](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/pyproject.toml#L38-L43)

```toml
[project.entry-points."vllm.platform_plugins"]
omni-cache = "omni_cache.plugin:register"
...
[project.entry-points."omni.kv_connectors"]
omni-cache = "omni_cache.connector:register_connectors"
```

omni-cache 注册了两个关键入口：`vllm.platform_plugins` 组的 `omni-cache`（vLLM 启动时加载，与 u2-l1 讲过的 omni-npu 插件同机制，这就是 `run_server_p.sh` 里 `VLLM_PLUGINS="omni-npu,omni-cache"` 能生效的原因）；以及 omni-npu 自定义的 `omni.kv_connectors` 组，把 `OmniCacheConnector` 注册进连接器工厂，使 `kv-transfer-config` 里的字符串 `"OmniCacheConnector"` 能解析到实现类。

**（3）统一注册入口：plugin.register()**

[components/omni-cache/omni_cache/plugin.py:L763-L771](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/omni_cache/plugin.py#L763-L771)

```python
def register() -> None:
    """Unified registration entry for the omni-cache plugin."""
    logger.info("omni_cache: starting unified registration (connector/cache/ox)")
    _register_kv_connectors()
    _init_cache()
    _init_ox()
    _register_attn_plugins()
    _init_diagnostics()
```

vLLM 加载插件时调用此函数，依次完成：注册 KV 连接器、初始化缓存管理、初始化 OX 传输引擎、注册注意力插件、初始化诊断工具。这五行就是 omni-cache 五大子系统的清单。

**（4）连接器门面：OmniCacheConnector**

[components/omni-cache/omni_cache/connector/connector.py:L33-L64](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/omni_cache/connector/connector.py#L33-L64)

```python
class OmniCacheConnector(KVConnectorBase_V1, SupportsHMA):
    def __init__(self, vllm_config, role, kv_cache_config=None):
        ...
        if vllm_config.model_config.is_deepseek_mla:
            vllm_config.kv_transfer_config.kv_parallel_size = 1
        self.is_prefill = vllm_config.kv_transfer_config.kv_role == "kv_producer"
        if self.is_prefill:
            self._init_prefill_config(vllm_config)
        else:
            self._init_decode_config()
        self._init_role(role, vllm_config)
```

三个要点：它实现 vLLM 的 `KVConnectorBase_V1` 接口（与 LLMDataDistConnector 同一抽象，所以可无缝互换），并额外混入 `SupportsHMA` 标记（主机内存访问支持接口，具体方法集以 vLLM 侧定义为准，待确认）；按 `kv_role` 二分 prefill/decode 配置；再按 vLLM 传入的 `role`（SCHEDULER/WORKER）组装协作对象——这与 u4-l2 的「角色 × kv_role」二维分派完全同构，细节留待 u7-l2。

**（5）KV 命中的可观测点**

- [components/omni-cache/omni_cache/connector/scheduler/decode.py:L94-L98](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/omni_cache/connector/scheduler/decode.py#L94-L98)：`get_num_new_matched_tokens` 钩子里的 debug 日志，打印 `num_computed_tokens`（已命中、无需重算的 token 数）与 `kv_transfer_params`——这是从日志确认 KV 命中的第一手证据。
- [components/omni-cache/omni_cache/reuse_rate_logger.py:L19-L49](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/omni_cache/reuse_rate_logger.py#L19-L49)：把每层 `reuse_rate` 以 Prometheus Gauge `vllm:omni_cache_reuse_rate` 暴露，可从 `/metrics` 端点持续观测命中率。
- [components/omni-cache/omni_cache/connector/connector.py:L82-L92](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni_cache/connector/connector.py#L82-L92)：P 侧启动时打出 `[DYNAMIC-TOPO] P-side computed ox_shard_list=...` 日志，确认 OX 分片拓扑计算结果。

#### 4.1.4 代码实践

**实践目标**：不启动推理服务，仅通过源码追踪 + 入口点检查，验证「插件注册 → 连接器类」这条链路真实存在。

**操作步骤**：

1. 在已部署镜像的容器内执行（参照 u2-l1 的手法）：

```bash
python3 -c "
from importlib.metadata import entry_points
for group in ('vllm.platform_plugins', 'omni.kv_connectors'):
    print('==', group)
    for ep in entry_points(group=group):
        print(' ', ep.name, '->', ep.value)
"
```

2. 再验证插件函数可加载且连接器类可导入：

```bash
python3 -c "
from omni_cache.plugin import register
print('register ok:', register)
from omni_cache.connector.connector import OmniCacheConnector
print('connector ok:', OmniCacheConnector)
"
```

3. 对照 `plugin.py:763-771` 的五个 `_register_*`/`_init_*` 调用，在源码里各找一处定义位置，抄成清单。

**需要观察的现象**：第一个命令应列出 `omni-cache -> omni_cache.plugin:register` 与 `omni-cache -> omni_cache.connector:register_connectors`；第二个命令打印两个类/函数对象而不报 ImportError。

**预期结果**：entry points 与 pyproject.toml 声明逐字一致，说明部署容器里的 omni-cache 包安装正确。若第二个命令失败而第一个成功，说明包安装不完整（缺少源码目录）。本实践不依赖 NPU，可在任意装有该包的环境执行；无环境时按源码走读完成第 3 步即可（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：基线 LLMDataDist 路径下，KV 在「P 等 D 取走」期间存放在哪里？OmniCache 把它挪到了哪里，换来什么？

**答案**：基线下 KV 存放在 P 的 HBM（靠 u4-l2 的延迟释放机制压占用）；OmniCache 把它卸载到 P 的主机内存（hugetlbfs 池），HBM 腾出来给计算和新请求用，因此能支撑更长序列与更高并发；同时主机内存池中的 KV 是持久的，多轮对话可命中。

**练习 2**：`ENABLE_HOST_MAPPING=1` 时，D 侧哪些数据留在 HBM、哪些从主机内存读？为什么这样分工？

**答案**：DSA 的 indexer KV 留在 HBM，其余 KV 数据通过 NPU MMU 从主机内存池直接读取（见 `docs/CONFIG_REFERENCE.md` 核心开关表与 `examples/pangu_v2_pd/README.md` 覆盖表）。indexer 是每步选 token 都要打分的小张量（128 B/token），对延迟敏感故留 HBM；主体 KV（576 B/token）大而对访问延迟相对不敏感，放主机换容量。

**练习 3**：默认 500 GiB 主机池、每 token 704 字节，估算可缓存 token 数；若 `MAP_SIZE_BYTES` 翻倍到 1 TiB，容量变为多少？

**答案**：\(536{,}870{,}912{,}000 / 704 \approx 7.6 \times 10^{8}\) 个 token；翻倍即约 \(1.5 \times 10^{9}\) 个 token（估算值，实际受对齐与布局影响）。

### 4.2 hugetlbfs：主机内存池的载体

#### 4.2.1 概念说明

**hugetlbfs 是什么**：Linux 内核提供的大页文件系统。普通内存页 4KB，hugetlbfs 以 2MB（或更大）页为单位管理内存，通过 `/dev/hugepages` 挂载点暴露为一个特殊文件系统——对它 `truncate` + `mmap` 得到的不是磁盘文件，而是**锁定在物理内存、不会被换出、物理上连续的大页内存**。

**OmniCache 为什么必须用它**，而不是普通 `malloc` 或 `/dev/shm`：

1. **容量与驻留**：主机池要几百 GiB 且内容绝不能被内核换出到磁盘（KV 一旦换出，MMU 直读路径直接失效），大页天然锁定；
2. **稳定低延迟**：2MB 页让 TLB（地址翻译缓存）覆盖范围是 4KB 页的 512 倍，NPU MMU 频繁直读主机内存时缺页开销显著降低；
3. **跨进程共享**：P/D 各自的多个 worker 进程要共享同一块 KV 池，文件级 mmap 共享语义正好；
4. **可寻址**：OX 传输引擎与 MMU 映射都需要稳定的物理地址视图，hugetlbfs 文件提供了这个锚点。

USER_GUIDE 给出的默认规模是「2MB HugePages 管理主机端 KV Cache 内存池（默认 500 GiB）」。

#### 4.2.2 核心流程

主机内存池的准备分两层，对应两个脚本：

```text
第一层（内核参数）：set_hugepage_limit.sh
  告诉内核总共预留多少个 2MB 大页（写 /sys/kernel/mm/hugepages/hugepages-2048kB/nr_hugepages）
  自动模式：总内存 − 预留(10%, 夹在 [20GB, 200GB])，上限 1228GB
  例：1TB 机器 → 预留 100GB 给系统 → 大页池 924GB → 约 48 万页

第二层（文件系统）：setup_hugetlbfs_2MB.sh
  ① reserve_pages：向 sysfs 写目标页数，重试至多 10 次
  ② mount_hugetlbfs：mount -t hugetlbfs -o pagesize=2M,mode=0770 none /dev/hugepages
  ③ create_mmap_file：truncate 出 MAP_SIZE_BYTES 大小的池文件（如 omni_cache_p）
  ④ zero-fill：python mmap 逐页写 0 —— 强制内核立刻分配每一页并清零
```

zero-fill 一步值得强调：truncate 出的 hugetlbfs 文件是**稀疏**的，页要到首次访问才真正分配；逐页写零既保证页全部落地（运行期不会因分配失败而崩溃），也清掉了上一轮服务残留的旧 KV——脏数据混入新会话是正确性事故。

页数换算公式（`need_pages_from_size`）：

\[
\text{pages} = \left\lceil \frac{\text{MAP\_SIZE\_BYTES}}{2\,\text{MiB}} \right\rceil
\]

默认 500 GiB 池：\( 536{,}870{,}912{,}000 / 2{,}097{,}152 = 256{,}000 \) 页，恰好整除。

实际部署中**不需要手工跑第二层**：`launch_prefill.sh` / `launch_decode.sh` 在 `ENABLE_OMNI_CACHE=1` 时会自动调用它（见 4.3.3）。

#### 4.2.3 源码精读

**（1）用户指南的 HugePage 章节**

[components/omni-cache/docs/USER_GUIDE.md:L33-L49](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/docs/USER_GUIDE.md#L33-L49)

给出推荐流程：`sudo bash tools/setup/set_hugepage_limit.sh`（或 `--target-pages 1048576` 手动指定），再用 `grep HugePages_ /proc/meminfo` 验证。

**（2）set_hugepage_limit.sh 的自动计算**

[components/omni-cache/tools/setup/set_hugepage_limit.sh:L69-L80](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/tools/setup/set_hugepage_limit.sh#L69-L80)

```bash
total_mem_kb=$(grep MemTotal /proc/meminfo | awk '{print $2}')
...
reserve_gb=$(( total_mem_gb / 10 ))      # 给系统留 10%
(( reserve_gb < 20 )) && reserve_gb=20    # 至少 20GB
(( reserve_gb > 200 )) && reserve_gb=200  # 至多 200GB
max_huge_gb=1228                          # 大页池上限 ~1.2 TiB
huge_gb=$(( total_mem_gb - reserve_gb ))
```

读 `/proc/meminfo` 的 MemTotal，按「10% 且夹在 [20, 200] GB」给系统留量，大页池封顶 1228 GB。注意该脚本只改**数量**，不挂载、不建文件——它是「每月开机一次」的系统级设置。

**（3）setup_hugetlbfs_2MB.sh 的四步**

[components/omni-cache/tools/setup/setup_hugetlbfs_2MB.sh:L8-L15](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/tools/setup/setup_hugetlbfs_2MB.sh#L8-L15)：可调参数——挂载点 `/dev/hugepages`、页大小 2048KB、文件名 `OMNI_FILE`（默认 `omni_cache`）、池大小 `MAP_SIZE_BYTES`（默认 1 TiB）、是否写零 `ZERO_FILL`。

[components/omni-cache/tools/setup/setup_hugetlbfs_2MB.sh:L67-L76](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/tools/setup/setup_hugetlbfs_2MB.sh#L67-L76)：定位 `hugepages-2048kB` sysfs 目录并实现「字节数 → 页数」向上取整换算。

[components/omni-cache/tools/setup/setup_hugetlbfs_2MB.sh:L114-L128](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/tools/setup/setup_hugetlbfs_2MB.sh#L114-L128)：挂载逻辑——若挂载点已是 hugetlbfs 则保留复用，否则先卸载非 hugetlbfs 的旧挂载再 `mount -t hugetlbfs -o pagesize=2M,mode=0770`。

[components/omni-cache/tools/setup/setup_hugetlbfs_2MB.sh:L132-L184](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/tools/setup/setup_hugetlbfs_2MB.sh#L132-L184)：建池文件——删旧文件、`truncate -s $size`、`chmod 660`，随后用内嵌 Python 对文件做 `MAP_SHARED` mmap 并逐页写零（带进度条），确保每页真实分配且内容干净。

**（4）两侧池文件名的区分**

[components/omni-cache/examples/pangu_v2_pd/launch_prefill.sh:L50-L53](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/examples/pangu_v2_pd/launch_prefill.sh#L50-L53) 与 [components/omni-cache/examples/pangu_v2_pd/launch_decode.sh:L51-L55](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/examples/pangu_v2_pd/launch_decode.sh#L51-L55)：P 侧池文件默认 `omni_cache_p`，D 侧默认 `omni_cache_d`；D 侧还可选第二个池 `omni_cache_decode_dsa`（DSA Split 二级池，默认关闭，容量按主池 80% 计算）。文件名不同，P/D 同机部署时两池互不覆盖。

**（5）容量参数的落点**

[components/omni-cache/examples/pangu_v2_pd/configs/base.sh:L64-L67](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/examples/pangu_v2_pd/configs/base.sh#L64-L67)：`OMNI_CACHE_LAYER_BYTES`（默认 16 GiB，每层 KV buffer 预算，影响每 die 的 `num_blocks`）、`MAP_SIZE_BYTES`（默认 500 GiB，池文件大小）、`KV_CACHE_MEMORY_BYTES`（按 `OMNI_CACHE_LAYER_BYTES * HYBRID_ATTN_GROUP_SIZE` 计算——hybrid 注意力按组共享 KV，只需为组代表层留预算）。

#### 4.2.4 代码实践

**实践目标**：在一台普通 Linux 机器（无需 NPU）上完成大页预留与验证，理解两个脚本的分工。

**操作步骤**：

1. 查看当前大页状态：

```bash
grep HugePages_ /proc/meminfo
```

2. 用手动页数模式预留少量大页（测试机请用小值，如 100 页 ≈ 200MB，避免吃光内存）：

```bash
sudo bash components/omni-cache/tools/setup/set_hugepage_limit.sh --target-pages 100
grep HugePages_ /proc/meminfo   # 期望 Nr_Hugepages=Hugepages_Free=100
```

3. 跑完整建池脚本（把池缩小到 200MB，跳过长时间写零）：

```bash
cd components/omni-cache
sudo env MAP_SIZE_BYTES=209715200 OMNI_FILE=tutorial_pool ZERO_FILL=1 \
    bash tools/setup/setup_hugetlbfs_2MB.sh
ls -lh /dev/hugepages/          # 期望看到 tutorial_pool 200M
```

4. 结束后回收：

```bash
sudo rm -f /dev/hugepages/tutorial_pool
sudo bash components/omni-cache/tools/setup/set_hugepage_limit.sh --target-pages 0
```

**需要观察的现象**：第 2 步后 `HugePages_Total` 变为 100；第 3 步终端出现「Reserving 2MB HugePages」进度条与「Zero-fill progress」进度条，最后打印 `HugePages setup completed successfully!`；`free -g` 中可用内存下降约 200MB（大页一旦预留即从普通可用内存中扣除，这与 u7-l3 要讲的「切回普通部署需归还大页」直接相关）。

**预期结果**：`/dev/hugepages/tutorial_pool` 存在且大小 200MB；`mount | grep hugetlbfs` 可见挂载点。若 `HugePages_Total` 迟迟达不到目标（内存碎片化），脚本会重试 10 次后报错，此时重启机器后再试是最可靠的解法。本实践已在逻辑层面核对着脚本源码推演；具体输出待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`set_hugepage_limit.sh` 与 `setup_hugetlbfs_2MB.sh` 各负责什么？重复执行 `setup_hugetlbfs_2MB.sh` 安全吗？

**答案**：前者只写 sysfs 的 `nr_hugepages`，决定内核总共留多少 2MB 页；后者做「预留（必要时）+ 挂载 + 建池文件 + 写零」全套。后者可重复执行：挂载步骤检测到已有 hugetlbfs 挂载会保留；池文件步骤会先删除旧文件再重建（源码 `create_mmap_file` 开头 `rm -f`）——注意这意味着重复执行会清空池中已有 KV。

**练习 2**：为什么建池后必须逐页写零，而不是直接开始服务？

**答案**：hugetlbfs 文件 truncate 后是稀疏的，页在首次访问才分配；若运行期才触碰页，可能因当时内存碎片导致分配失败。预先写零既把所有页真实落地，又清除了上一轮进程残留的旧 KV 数据，避免脏数据破坏正确性。

**练习 3**：1 TiB 内存的服务器跑自动模式 `set_hugepage_limit.sh`，大页池多大、多少页？

**答案**：预留量 = 1024/10 = 102.4 GB（在 [20,200] 区间内取 102.4），大页池 = 1024 − 102.4 = 921.6 GB（未到 1228 GB 上限），约 \(921.6 \times 1024 / 2 = 471{,}859\) 页（取整按脚本字节数换算）。

### 4.3 启动参数：环境变量、kv-transfer-config 与启动脚本

#### 4.3.1 概念说明

OmniCache 的启动参数分三层，作用域从大到小：

| 层 | 载体 | 管什么 |
|----|------|--------|
| 进程级 | 环境变量（`ENABLE_OMNI_CACHE` 等） | 功能开关、内存池路径与容量、网卡、HBM 布局 |
| 拓扑级 | `--kv-transfer-config` JSON | 连接器类型、P/D 角色、kv_rank 与并行规模 |
| 引擎级 | `vllm serve` 其余参数 | TP/DP、批大小、图模式等（u1-l4/u4-l4 已讲） |

其中**producer 与 consumer 两侧的环境变量差异**是本模块的核心考点，一句话版本：

- P（producer）：`ENABLE_OMNI_CACHE=1`、`ENABLE_HOST_MAPPING=0`（Prefill 不做主机映射）；
- D（consumer）：`ENABLE_OMNI_CACHE=1`、`ENABLE_HOST_MAPPING=1`（MMU 直读主机池）、外加 `VLLM_WORKER_MULTIPROC_METHOD=fork`（DP 多进程按 fork 派生，保证子进程继承已映射的池）。

#### 4.3.2 核心流程

从「裸 vllm serve」到「OmniCache 服务」的装配顺序：

```text
1. set_hugepage_limit.sh（一次性系统设置）
2. export ENABLE_OMNI_CACHE=1
   export ENABLE_HOST_MAPPING=0        # P 侧；D 侧为 1
   export VLLM_WORKER_MULTIPROC_METHOD=fork   # 仅 D 侧
3. vllm serve <model> \
     --kv-transfer-config '{"kv_connector":"OmniCacheConnector",
                            "kv_role":"kv_producer" | "kv_consumer",
                            "kv_rank":<P 固定 0；D 的 DP_i 取 i+1>,
                            "kv_parallel_size":<见下文>,
                            "kv_connector_extra_config":{"p_node_list":[...],
                                                         "kv_producer_dp_size":1}}'
4. （一键路径）bash examples/pangu_v2_pd/launch_pd.sh
     └─ launch_prefill.sh / launch_decode.sh 自动完成 1~3，含 hugetlbfs 预留
```

kv_rank 规则与 u4-l1 基线一致（P 固定 0、D 按 DP 递增），但 **kv_parallel_size 与基线不同**，且仓库内三个出处说法有出入，必须如实列出：

| 出处 | kv_parallel_size 取值 |
|------|----------------------|
| README/USER_GUIDE 快速开始（OmniCache 模式） | 固定 `1` |
| `docs/CONFIG_REFERENCE.md` 字段表 | 「OmniCache 模式为 1」 |
| `examples/pangu_v2_pd/launch_decode.sh` 实际代码 | OmniCache 模式取 `DECODE_DP_SIZE + 1`，基线模式取 1 |
| `connector.py` 运行时 | `is_deepseek_mla` 模型强制改写为 1 |

结论：文档声明与示例脚本存在不一致。工程上应以你实际使用的启动脚本产出的 JSON 为准（部署后可在日志的 KV_TRANSFER_CONFIG 回显中核对，方法同 u4-l1）；这一差异本身是个很好的「文档漂移」案例——源码与脚本永远优先于 README。

#### 4.3.3 源码精读

**（1）用户指南定义的两侧环境变量**

[components/omni-cache/docs/USER_GUIDE.md:L57-L70](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/docs/USER_GUIDE.md#L57-L70)：P 侧两行（`ENABLE_OMNI_CACHE=1`、`ENABLE_HOST_MAPPING=0`）；D 侧三行（多出 `ENABLE_HOST_MAPPING=1` 与 `VLLM_WORKER_MULTIPROC_METHOD=fork`）。[components/omni-cache/docs/USER_GUIDE.md:L78-L90](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/docs/USER_GUIDE.md#L78-L90)：两份 kv-transfer-config 模板，并明确「多 DP 时每个实例 kv_rank 递增（2, 3, ..., DECODE_DP_SIZE）」。

**（2）核心开关表**

[components/omni-cache/docs/CONFIG_REFERENCE.md:L5-L10](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/docs/CONFIG_REFERENCE.md#L5-L10)：四个核心变量的权威定义——`ENABLE_OMNI_CACHE`（总开关，0 回退 LLMDataDist 基线）、`ENABLE_HOST_MAPPING`（主机 mmap 别名映射）、`P_NODE_LIST`（P 节点 IP，单机逗号分隔、多机分号分隔）、`OMNI_CACHE_LOCAL_DP_SIZE`（单机 DP 数，用于切分主机池）。

**（3）最小骨架：run_server_p.sh**

[components/omni-cache/examples/run_server_p.sh:L3-L36](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/examples/run_server_p.sh#L3-L36)

```bash
export GLOO_SOCKET_IFNAME=enp23s0f3
export HCCL_INTRA_ROCE_ENABLE=1
export HCCL_INTRA_PCIE_ENABLE=0
VLLM_PLUGINS="omni-npu,omni-cache" vllm serve "$model" \
    ...
    --kv-transfer-config '{
        "kv_connector": "LLMDataDistConnector",
        "kv_role": "kv_producer",
        "kv_rank": 0,
        "kv_parallel_size": 1
    }' \
    --enforce-eager &> "${log_file}"
```

这是全仓库最短的 PD 启动样本：先设网卡与 HCCL 通信参数，再以 `VLLM_PLUGINS` 同时加载 omni-npu（平台适配）与 omni-cache 两个插件。**注意它写的是 `LLMDataDistConnector`**——把它改成 OmniCache 模式只需三步：kv_connector 换成 `OmniCacheConnector`、加上 4.3.1 的环境变量、提前完成大页准备。`run_server_d.sh` 与之逐行对称，仅 `kv_role` 为 `kv_consumer`、`kv_rank` 为 1（[run_server_d.sh:L30-L35](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/examples/run_server_d.sh#L30-L35)）。

**（4）OmniCache 完整参照：launch_prefill.sh**

[components/omni-cache/examples/pangu_v2_pd/launch_prefill.sh:L7-L14](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/examples/pangu_v2_pd/launch_prefill.sh#L7-L14)：脚本头注释声明双模式——`ENABLE_OMNI_CACHE=0` 走 LLMDataDistConnector 基线，`=1`（默认）走 OmniCacheConnector + hugetlbfs。**同一脚本切换两种 KV 传输方案**是这套示例的设计精髓。

[components/omni-cache/examples/pangu_v2_pd/launch_prefill.sh:L49-L61](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/examples/pangu_v2_pd/launch_prefill.sh#L49-L61)：P 侧派生默认值——池文件 `omni_cache_p`、`ENABLE_HOST_MAPPING=0`、强制 `ENFORCE_EAGER=1`（呼应 u5-l2 的「P 侧 enforce-eager」生产结论）、`NUM_GPU_BLOCKS_OVERRIDE=50000`、`OMNI_CACHE_PACKED_HBM=1`（HBM block 与 host block 解耦独立管理）。

[components/omni-cache/examples/pangu_v2_pd/launch_prefill.sh:L167-L185](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/examples/pangu_v2_pd/launch_prefill.sh#L167-L185)：两段关键逻辑——`ENABLE_OMNI_CACHE=1` 时自动调 `setup_hugetlbfs_2MB.sh` 预留主机池（传 `MAP_SIZE_BYTES` 与 `OMNI_FILE`），随后按开关把 `KV_CONNECTOR` 选为 `OmniCacheConnector` 或 `LLMDataDistConnector` 并 printf 拼装 JSON。

[components/omni-cache/examples/pangu_v2_pd/launch_prefill.sh:L230-L233](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/examples/pangu_v2_pd/launch_prefill.sh#L230-L233)：OmniCache 模式追加 `--num-gpu-blocks-override` 与 `--kv-cache-memory-bytes`，显式钉死 HBM 侧 block 数与 KV 内存预算。

**（5）D 侧差异：launch_decode.sh**

[components/omni-cache/examples/pangu_v2_pd/launch_decode.sh:L50-L63](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/examples/pangu_v2_pd/launch_decode.sh#L50-L63)：D 侧派生默认——池文件 `omni_cache_d`、DSA 二级池默认容量（主池 80%）、`ENABLE_HOST_MAPPING=1`、`NUM_GPU_BLOCKS_OVERRIDE=11800`（远小于 P 侧 50000：D 侧 KV 大头在主机池，HBM 只留 indexer 与工作集）。

[components/omni-cache/examples/pangu_v2_pd/launch_decode.sh:L223-L239](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/examples/pangu_v2_pd/launch_decode.sh#L223-L239)：DP 展开——循环 `DECODE_DP_SIZE` 次，每个 rank 独占一段 NPU die、端口从 `PORT_BASE` 递增；OmniCache 模式下 `kv_psize=$((DECODE_DP_SIZE + 1))`，`kv_rank` 取 `rank+1`（4.3.2 表格中「不一致」的一侧即在此处）。

[components/omni-cache/examples/pangu_v2_pd/launch_decode.sh:L269-L274](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/examples/pangu_v2_pd/launch_decode.sh#L269-L274)：一个容易被忽略的语义——OmniCache 模式下 D 侧**强制** `--no-enable-prefix-caching`：vLLM 自带的 HBM 前缀缓存被关闭，多轮复用完全由主机内存池承接（与 `ENABLE_PREFIX_CACHING` 的组合仅在基线模式生效）。

**（6）配置基座与拓扑**

[components/omni-cache/examples/pangu_v2_pd/configs/base.sh:L17-L28](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/examples/pangu_v2_pd/configs/base.sh#L17-L28)：全部 OmniCache 开关的默认值（`ENABLE_OMNI_CACHE=1`、`OMNI_CACHE_LOCAL_DP_SIZE=8`、`ENABLE_OMNI_CACHE_DSA_SPLIT=0` 等）；[L51-L62](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/examples/pangu_v2_pd/configs/base.sh#L51-L62)：P（TP=8）与 D（DP=8、TP=1、`DEVICE_START=8`、fork）两套形态默认。[examples/pangu_v2_pd/README.md:L8-L13](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/examples/pangu_v2_pd/README.md#L8-L13)：单节点 16 卡拓扑——P0 占卡 0-7（TP8、端口 8000），D0 占卡 8-15（DP8、TP1、端口 8082-8089），proxy 在 P 容器内监听 7150。

**（7）常见问题速查**

[components/omni-cache/docs/USER_GUIDE.md:L147-L171](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/docs/USER_GUIDE.md#L147-L171)：四个高频故障——`libhccl.so` 找不到（未 source CANN 环境）、EJ0003 端口占用（`docker restart` 容器等待 30 秒）、Decode OOM（调小 `NUM_GPU_BLOCKS_OVERRIDE` 或 `OMNI_CACHE_LAYER_BYTES`）、Prefill hidden state 不一致（P 侧 `enable_moe_agrs` 必须为 `false`，否则 decoder layer 2 起 hidden state 出现差异）。

#### 4.3.4 代码实践

**实践目标**：把最小骨架 `run_server_p.sh` 改造成 OmniCache 模式的 producer 启动脚本，检验对三层参数的掌握。

**操作步骤**：

1. 复制 `components/omni-cache/examples/run_server_p.sh` 为 `run_server_p_omni.sh`（放在容器内任意可写目录，**不要改动仓库源码**）。
2. 做四处修改（以下为示例代码）：

```bash
# ① 环境变量层：文件头部追加
export ENABLE_OMNI_CACHE=1
export ENABLE_HOST_MAPPING=0

# ② 拓扑层：替换 kv-transfer-config
--kv-transfer-config '{
    "kv_connector": "OmniCacheConnector",
    "kv_role": "kv_producer",
    "kv_rank": 0,
    "kv_parallel_size": 1,
    "kv_connector_extra_config":{"p_node_list":["<本机IP>"],"kv_producer_dp_size":1}
}'

# ③ 内存层：显式声明池参数
export OMNI_CACHE_MMAP_FILE=omni_cache_p
export MAP_SIZE_BYTES=536870912000

# ④ 前置：先完成大页准备（或确认 launch 脚本已做）
sudo bash tools/setup/set_hugepage_limit.sh
```

3. 不真正启动，先做语法与参数自检：`bash -n run_server_p_omni.sh`，并把 kv-transfer-config JSON 单独抽出用 `python3 -m json.tool` 校验合法性。
4. 对照 `launch_prefill.sh:L178-L185` 检查：你拼的 JSON 与脚本 printf 模板产出的字段集是否一致（差异即遗漏）。

**需要观察的现象**：`bash -n` 无语法错误；JSON 校验通过；与 `launch_prefill.sh` 的模板 diff 后，差异只剩 `p_node_list` 的取值来源（脚本从 `P_NODE_LIST` 环境变量展开，你手写了固定 IP）与 `kv_parallel_size`（脚本 P 侧默认同样为 1，一致）。

**预期结果**：得到一份可在真实环境执行的 OmniCache producer 脚本。真实启动行为（日志中出现 `[DYNAMIC-TOPO] P-side computed ox_shard_list=...`）待本地在 NPU 环境验证。

#### 4.3.5 小练习与答案

**练习 1**：`ENABLE_OMNI_CACHE=0` 时整套启动脚本会发生哪三处变化？

**答案**：① 连接器从 `OmniCacheConnector` 换回 `LLMDataDistConnector`（launch_prefill/decode 的 `KV_CONNECTOR` 分支）；② 跳过 hugetlbfs 预留（脚本打印 "skipping hugetlbfs reservation"）；③ D 侧 `ENABLE_HOST_MAPPING` 强制为 0、`kv_psize` 取 1，且 vLLM 的 `--enable-prefix-caching` 恢复为可选项。即退回 u4-l1/u4-l2 的基线部署。

**练习 2**：为什么 D 侧要 `VLLM_WORKER_MULTIPROC_METHOD=fork` 而 P 侧不需要？

**答案**：D 侧是 DP=8 的多进程形态（一 die 一进程）。fork 让 worker 子进程直接继承父进程已完成的主机内存池 mmap 映射，各 DP 进程得以共享同一 hugetlbfs 池的地址视图；P 侧是 TP=8 单引擎（mp 后端）且不做主机映射，故无需此设置。（依据：该变量仅出现在 USER_GUIDE 的 Decode 段落与 base.sh 的 Decode-specific 默认中。）

**练习 3**：D 侧 `NUM_GPU_BLOCKS_OVERRIDE=11800` 远小于 P 侧 `50000`，原因是什么？

**答案**：OmniCache 模式下 D 侧的 KV 主体放在主机内存池，HBM 仅承载 DSA indexer 与工作集（`ENABLE_HOST_MAPPING=1` 时其余 KV 经 MMU 直读主机），因此 vLLM 调度器的 HBM block 上限可以压得很低，把 HBM 让给模型权重与激活。

## 5. 综合实践

把三个模块串起来：**在两个容器里手工拉起 OmniCache 的 producer 与 consumer，发送多轮对话请求并从日志确认 KV 命中**。参照物是 USER_GUIDE 的启动流程、`run_server_p.sh`/`run_server_d.sh` 的骨架，以及 `examples/pangu_v2_pd/` 的完整脚本。

**实践目标**：端到端跑通一次 OmniCache PD 服务，并用日志与指标证明第二轮请求命中了第一轮的 KV。

**操作步骤**（假设两台 16 卡机器 A、B，已部署推理镜像；单机也可用两个容器模拟，参照 `launch_containers.sh` 的容器划分）：

1. **两侧共同准备**：进容器后安装/确认 omni-cache（`pip install -e . --no-build-isolation`），source CANN 环境（`source /usr/local/Ascend/ascend-toolkit/set_env.sh`，否则报 `libhccl.so` 找不到）。
2. **大页准备**（两台各自执行）：

```bash
sudo bash tools/setup/set_hugepage_limit.sh          # 自动按内存计算
grep HugePages_ /proc/meminfo                        # 确认 Nr_Hugepages 达标
```

3. **A 机拉起 producer**（参照 4.3.4 改造的脚本或直接用 USER_GUIDE 命令）：

```bash
export ENABLE_OMNI_CACHE=1
export ENABLE_HOST_MAPPING=0
vllm serve /path/to/model \
    --host 0.0.0.0 --port 8000 --tensor-parallel-size 8 \
    --kv-transfer-config '{"kv_connector":"OmniCacheConnector","kv_role":"kv_producer","kv_rank":0,"kv_parallel_size":1,"kv_connector_extra_config":{"p_node_list":["<A机IP>"],"kv_producer_dp_size":1}}'
```

   首次启动会自动执行 hugetlbfs 预留（若用 `launch_prefill.sh`）或需要你手工先跑 `setup_hugetlbfs_2MB.sh`（若用裸命令）。
4. **B 机拉起 consumer**（多 DP 逐 rank 启动，`kv_rank` 递增）：

```bash
export ENABLE_OMNI_CACHE=1
export ENABLE_HOST_MAPPING=1
export VLLM_WORKER_MULTIPROC_METHOD=fork
for rank in $(seq 0 7); do
    vllm serve /path/to/model \
        --host 0.0.0.0 --port $((8082 + rank)) \
        --tensor-parallel-size 1 \
        --data-parallel-size 8 --data-parallel-rank $rank \
        --kv-transfer-config '{"kv_connector":"OmniCacheConnector","kv_role":"kv_consumer","kv_rank":'"$((rank + 1))"',"kv_parallel_size":1,"kv_connector_extra_config":{"p_node_list":["<A机IP>"],"kv_producer_dp_size":1}}' &
done
```

5. **健康检查**：`curl http://127.0.0.1:8000/health` 与 `curl http://127.0.0.1:8082/health` 均返回 200。若需完整请求链路，可按 `examples/pangu_v2_pd/launch_proxy.sh` 在 A 机起 proxy（默认 7150 端口）统一入口。
6. **多轮对话请求**：通过 proxy（或按你的编排层）发送同一会话的两轮请求，第二轮 prompt 完整包含第一轮的 prompt 与回答（注意请求体 `model` 字段须等于 `SERVED_MODEL_NAME`，理由见 u1-l5）。
7. **确认 KV 命中**（三处证据）：

```bash
# D 侧日志：命中 token 数（需 VLLM_LOGGING_LEVEL=DEBUG）
grep "get_num_new_matched_tokens" decode_0.log
# 期望第二轮请求的 num_computed_tokens > 0，量级 ≈ 第一轮 prompt 长度

# P 侧日志：连接器拓扑与 OX 分片
grep "DYNAMIC-TOPO" prefill/serving.log

# 指标：每层 KV 复用率（启用 metrics 后）
curl http://127.0.0.1:8082/metrics | grep vllm:omni_cache_reuse_rate
```

**需要观察的现象**：第二轮请求的 D 侧日志中 `num_computed_tokens` 显著大于 0（首轮通常为 0）；`reuse_rate` 指标出现非零值；P 侧第二轮的 prefill 计算量明显小于首轮（响应耗时下降）。

**预期结果**：两轮对话均正常返回且内容一致性好；上述三处证据至少两处可复现，即可判定 KV 命中成立。若第二轮 `num_computed_tokens` 恒为 0，按顺序排查：池文件是否被重建（重复执行 setup 脚本会清池）、`P_NODE_LIST`/`p_node_list` 是否指向正确 IP、D 侧 `kv_rank` 是否递增且不与 P 冲突。本综合实践需要真实 NPU 与模型权重，全部步骤的运行输出**待本地验证**。

## 6. 本讲小结

- OmniCache 的 KV 卸载模型：KV 沿「P HBM → P 主机内存池 → OX 网络 → D 主机内存池 → D HBM（或 MMU 直读）」流动，主机内存池既卸掉了两侧 HBM 压力（提升序列长度与并发），又因 KV 持久化大幅提升多轮对话 APC 命中率。
- hugetlbfs 是主机池的载体：2MB 大页锁定物理内存、TLB 友好、可多进程共享；准备分两层——`set_hugepage_limit.sh` 定内核页数（自动模式预留 10%、夹 [20,200]GB、上限 1228GB），`setup_hugetlbfs_2MB.sh` 做预留+挂载+建池文件+逐页写零；`launch_*.sh` 在 `ENABLE_OMNI_CACHE=1` 时自动调用后者。
- 启动参数分三层：环境变量（P 侧 `ENABLE_HOST_MAPPING=0`，D 侧 `=1` 且加 `VLLM_WORKER_MULTIPROC_METHOD=fork`）、`kv-transfer-config`（`OmniCacheConnector` + kv_rank 规则 P 固定 0 / D 按 DP_i=i+1，`kv_parallel_size` 文档与示例脚本存在不一致、以实际脚本回显为准）、以及 `NUM_GPU_BLOCKS_OVERRIDE` 等 HBM 预算参数（D 侧远小于 P 侧）。
- `examples/run_server_p/d.sh` 是 LLMDataDist 基线的最小骨架，改三处（连接器名、环境变量、大页前置）即成 OmniCache 版本；`examples/pangu_v2_pd/launch_*.sh` 才是 OmniCache 完整参照，且同一脚本用 `ENABLE_OMNI_CACHE` 即可在基线/OmniCache 间切换。
- KV 命中的可观测证据有三处：D 侧 `get_num_new_matched_tokens` 日志的 `num_computed_tokens`、P 侧 `[DYNAMIC-TOPO]` 拓扑日志、`vllm:omni_cache_reuse_rate` Prometheus 指标。
- OmniCache 模式下 D 侧强制关闭 vLLM 自带 `--enable-prefix-caching`，多轮复用完全由主机内存池承接。

## 7. 下一步学习建议

下一讲 **u7-l2 OmniCacheConnector 源码结构**将打开本讲当作黑盒的连接器内部：prefill 侧 worker 的卸载实现、decode 侧 `kv_loader`/`process_manager` 的加载流程、两侧 scheduler 的配合，并与 u4-l2 的 LLMDataDistConnector 做接口级对比。建议先自行浏览 `components/omni-cache/omni_cache/connector/` 目录（`prefill/worker.py`、`decode/kv_loader.py`、`decode/process_manager.py`、`scheduler/prefill.py`、`scheduler/decode.py`），带着「KV 从 HBM 落到主机池的那行代码在哪」的问题进入下一讲。配置与大页运维的完整细节则留待 u7-l3。
