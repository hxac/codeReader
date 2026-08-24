# OmniCache 配置参考与大页内存运维

## 1. 本讲目标

学完本讲，你应该能够：

1. 按「核心开关 / kv-transfer-config / 内存与 HugePage / HBM 布局 / DSA Split / 端口 / 诊断」七大类说出 OmniCache 的配置项全集，并知道每个变量在哪一层被消费。
2. 分清「代码默认值、文档默认值、ansible 模板实际值」三层默认值的优先级，避免拿着文档去排障却看不到对应行为。
3. 讲清楚 `set_hugepage_limit.sh`（宿主机持久层）与 `setup_hugetlbfs_2MB.sh`（运行时建池层）两个脚本的分工，理解大页「为什么分配不上去、为什么释放不掉」。
4. 独立完成「启用 OmniCache → 切回普通部署」的全流程，包括重启容器与 `--target-pages 262144` 恢复大页上限的操作与验证方法。

本讲是 omni-cache 单元的收尾篇：u7-l1 讲了原理与部署入口，u7-l2 讲了 connector 源码结构，本讲把视角拉回**配置面与运维面**——生产上真正天天打交道的部分。

## 2. 前置知识

- **环境变量的三层传递**：回顾 u1-l4，变量沿 `play environment → docker exec -e → 脚本 export` 三层传递；本讲的几乎所有 OmniCache 配置都走这条链。
- **hugetlbfs 与 2MB 大页**：Linux 允许把物理内存按 2MB（而非默认 4KB）粒度预留出来，挂载成一种特殊文件系统 `hugetlbfs`。程序在其中的文件上做 `mmap`，拿到的就是大页背书的内存。对 KV Cache 这种「上百 GiB、长期驻留、随机访问」的负载，大页能显著减少页表项数量与 TLB miss。两个关键内核接口：
  - `/sys/kernel/mm/hugepages/hugepages-2048kB/nr_hugepages`：预留的 2MB 页总数，**写它就是改内核全局状态**；
  - `/etc/sysctl.conf` 中的 `vm.nr_hugepages`：让这个预留**重启后仍然生效**。
- **PD 分离与 OmniCache 数据通路**：回顾 u7-l1，KV 沿「P 侧 HBM → P 主机内存池 → OX 传输 → D 主机内存池 → D HBM 或 MMU 直读」流动；主机内存池就是本讲要运维的大页文件。
- **ENABLE_HOST_MAPPING（MMU 直读）**：`1` 时 D 侧 DSA indexer 留在 HBM、其余 KV 由 NPU 通过 MMU 直接读主机内存，省一次 HBM 拷贝。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [components/omni-cache/docs/CONFIG_REFERENCE.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/docs/CONFIG_REFERENCE.md) | OmniCache 配置项权威参考，按七类组织全部环境变量与 kv-transfer-config 字段 |
| [components/omni-cache/tools/setup/set_hugepage_limit.sh](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/tools/setup/set_hugepage_limit.sh) | 宿主机级脚本：计算并持久化 2MB 大页上限（写 sysctl + sysfs） |
| [components/omni-cache/tools/setup/setup_hugetlbfs_2MB.sh](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/tools/setup/setup_hugetlbfs_2MB.sh) | 运行时级脚本：预留页数、挂载 hugetlbfs、创建池文件并写零；被 ansible 模板在容器内自动调用 |
| [tools/ansible/92B/omni_infer_server_template_performance3P1D_92B_w8a8_open_omni_cache.yml](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance3P1D_92B_w8a8_open_omni_cache.yml) | 92B 3P1D w8a8 + OmniCache 生产模板，本讲的「配置实际值」来源 |
| [README_INT8.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README_INT8.md) | INT8 部署主文档，含「开启 omni-cache 特性」与「切回前的处理」两节 |
| [components/omni-cache/omni_cache/cache/core/constants.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/omni_cache/cache/core/constants.py) | 代码层默认值兜底（`os.getenv` 的第二参数），三层默认值的底层 |
| [components/omni-cache/docs/USER_GUIDE.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/docs/USER_GUIDE.md) | 手工拉起指南，交叉印证 HugePage 准备步骤与 P/D 环境变量建议 |

## 4. 核心概念与源码讲解

### 4.1 配置参考：OmniCache 配置项全集与「三层默认值」

#### 4.1.1 概念说明

OmniCache 的配置面完全由**环境变量 + `--kv-transfer-config` JSON** 构成，没有独立配置文件。CONFIG_REFERENCE.md 把它们分成七类：

1. **核心开关**：`ENABLE_OMNI_CACHE`（总开关，`0` 回退 vLLM 原生 LLMDataDistConnector）、`ENABLE_HOST_MAPPING`（MMU 直读）、`P_NODE_LIST`（P 节点 IP 列表）、`OMNI_CACHE_LOCAL_DP_SIZE`（单机本地 DP 并行度）。
2. **kv-transfer-config**：连接器名、角色、rank 与 `kv_connector_extra_config`（`p_node_list`、`kv_producer_dp_size`）。
3. **内存与 HugePage**：`OMNI_CACHE_MMAP_FILE/PATH`（池文件名与路径）、`MAP_SIZE_BYTES`（池总容量）、`OMNI_CACHE_LAYER_BYTES`（每层 HBM 预算）、`NUM_GPU_BLOCKS_OVERRIDE`（调度器 block 上限）。
4. **HBM 布局（仅 P）**：`OMNI_CACHE_PACKED_HBM`。
5. **DSA Split 二级池（仅 D）**：`ENABLE_OMNI_CACHE_DSA_SPLIT` 等四个变量。
6. **网络端口**：`BASE_PORT`（16077）、`ZMQ_BASE_PORT`（16555）。
7. **诊断调试**：KV dump、mock 调度、传输校验等十余个变量（生产不设）。

本模块最重要的心智模型是**三层默认值**。同一个变量在三处各有默认，优先级为：

```text
ansible 模板显式设置（最高） > 手工 export / 文档建议值 > 代码 os.getenv 兜底（最低）
```

以 `ENABLE_HOST_MAPPING` 为例：代码兜底是 `1`（[constants.py:L27](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/omni_cache/cache/core/constants.py#L27)），文档建议「P 侧 0、D 侧 1」，而 ansible 模板的 play 级默认是 `0`（[模板 L20](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance3P1D_92B_w8a8_open_omni_cache.yml#L20)）——三层互不相同。排障时必须先确认「现在到底生效的是哪一层的值」。

#### 4.1.2 核心流程

一个变量的生效路径（承接 u1-l4 的三层传递）：

```text
ansible play environment（模板 L19-L31，可被 -e 覆盖）
        │
        ▼
docker exec -e ...（模板 L475-L545，选择性地注入容器）
        │
        ▼
run_vllm_server_prefill/decode_cmd 脚本内的 if 分支再次 export / 覆盖
        │
        ▼
omni_cache Python 代码 os.getenv 读取（constants.py、base.py 等）
```

模板中 `if [[ "${ENABLE_OMNI_CACHE:-1}" == "1" ]]` 分支决定了**整套 OmniCache 变量只在开关闭合时才被 export**；开关断开时只 export 三个「归零变量」并回退 `LLMDataDistConnector`。因此「启用/关闭」不是增删一堆变量，而是切换一整段脚本分支。

#### 4.1.3 源码精读

**（1）核心开关表**——[components/omni-cache/docs/CONFIG_REFERENCE.md:L5-L10](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/docs/CONFIG_REFERENCE.md#L5-L10) 给出四个总控变量的取值、默认与适用端。注意 `P_NODE_LIST` 的格式约定：单机实例内逗号分隔、多机实例间分号分隔（[L12-L20](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/docs/CONFIG_REFERENCE.md#L12-L20)）。ansible 模板不用手填这个变量——它用 Jinja2 从 inventory 的 P 组自动推导（见本模块第 4 点）。

**（2）内存与 HugePage 变量**——[CONFIG_REFERENCE.md:L84-L98](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/docs/CONFIG_REFERENCE.md#L84-L98) 定义了池的三要素：文件名（`omni_cache_p`/`omni_cache_d`）、路径（`/dev/hugepages/<文件名>`）、大小（默认 500 GiB）。其中 `NUM_GPU_BLOCKS_OVERRIDE` 带一条硬约束：

\[ N_{blocks} < \frac{\text{OMNI\_CACHE\_LAYER\_BYTES}}{DP_{local} \times \text{nbytes}(kv\_cache\_block)} \]

即调度器 block 上限不能超过每层预算按本地 DP 与单 block 字节数摊薄后的容量，否则运行时会越界。

**（3）DSA Split 二级池**——[CONFIG_REFERENCE.md:L121-L126](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/docs/CONFIG_REFERENCE.md#L121-L126)：开启后 decode 侧额外建一个只存 DSA KV 的二级 hugepage 文件，容量默认取主池的 80%（`MAP_SIZE_BYTES * 80 / 100`），每次 OX pull 完成后用 `aclrtMemcpyAsync` 把 KV 段从主池异步拷到二级池，attention kernel 从更窄的二级池读数据。

**（4）ansible 模板的真实取值**——模板 play 级 environment（[模板 L19-L31](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance3P1D_92B_w8a8_open_omni_cache.yml#L19-L31)）声明了全部 OmniCache 变量并用 `| default(...)` 提供模板级默认，例如 `ENABLE_OMNI_CACHE` 默认 `1`、`BASE_PORT` 默认 `16077`。P 组 IP 列表则由 `p_node_list_computed`（[模板 L464-L472](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance3P1D_92B_w8a8_open_omni_cache.yml#L464-L472)）从 P 组 host 的 `host_ip` 去重后以分号连接自动算出，再经 `-e 'P_NODE_LIST={{ p_node_list_computed }}'` 注入容器（[L495](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance3P1D_92B_w8a8_open_omni_cache.yml#L495)、[L529](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance3P1D_92B_w8a8_open_omni_cache.yml#L529)）。

prefill 分支的关键行（[模板 L133-L161](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance3P1D_92B_w8a8_open_omni_cache.yml#L133-L161)）：

```bash
if [[ "${ENABLE_OMNI_CACHE:-1}" == "1" ]]; then
  export ENABLE_OMNI_CACHE=1
  export ENABLE_HOST_MAPPING=0                      # P 侧硬编码关掉 MMU 直读
  export OMNI_CACHE_MMAP_FILE="${OMNI_CACHE_PREFILL_MMAP_FILE:-omni_cache_p}"
  export OMNI_CACHE_LAYER_BYTES="${OMNI_CACHE_LAYER_BYTES:-68719476736}"   # 64GB
  export MAP_SIZE_BYTES="${MAP_SIZE_BYTES:-1288490188800}"                 # 1200GB
  export OMNI_CACHE_PACKED_HBM=1
  ...
  export KV_CACHE_MEMORY_BYTES=$(( OMNI_CACHE_LAYER_BYTES * HYBRID_ATTN_GROUP_SIZE ))
  KV_CONNECTOR="OmniCacheConnector"
  KV_PARALLEL_SIZE=1
```

这段代码做了三件事：导出全套池参数、把「每层预算 × 混合注意力分组数 17」换算成 vLLM 的 `--kv-cache-memory-bytes`（`HYBRID_ATTN_GROUP_SIZE=17` 与 u3-l2 讲过的 DSA/SWA 分组相关，此处只需当作乘数）、把连接器切到 `OmniCacheConnector` 并将 `KV_PARALLEL_SIZE` 写死为 1。decode 分支（[模板 L265-L301](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance3P1D_92B_w8a8_open_omni_cache.yml#L265-L301)）结构对称，但取值不同：`OMNI_CACHE_LAYER_BYTES` 默认 48GB、`MAP_SIZE_BYTES` 默认 1000GB、`ENABLE_HOST_MAPPING` 保留外部传入值（默认 0）、`OMNI_CACHE_LOCAL_DP_SIZE=16`，且 `KV_PARALLEL_SIZE=$((dp + 1))`（dp 为 decode DP 数，16 卡即 17）。这与 CONFIG_REFERENCE「OmniCache 模式 kv_parallel_size 为 1」的描述不一致——u7-l1 已给出结论：**以脚本运行日志的回显为准**。

**（5）代码层兜底**——[constants.py:L14-L27](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/omni_cache/cache/core/constants.py#L14-L27)：`OMNI_CACHE_LAYER_BYTES` 代码默认仅 4 GiB，`ENABLE_HOST_MAPPING` 代码默认为 `1`。池路径的消费点在 [base.py:L139-L141](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/omni_cache/cache/core/base.py#L139-L141)：`BaseOmniCache` 类属性 `MEMMAP_PATH = os.environ.get("OMNI_CACHE_MMAP_PATH", "/dev/hugepages/omni_cache")`。把这些与文档、模板放在一起，就得到完整的「三层默认值」对照表。

#### 4.1.4 代码实践

**实践目标**：制作一张「三层默认值对照表」，掌握用源码考古确认配置真实取值的方法。本实践**只需读代码，不需要 NPU**。

1. 打开 CONFIG_REFERENCE.md，抄下 `MAP_SIZE_BYTES`、`OMNI_CACHE_LAYER_BYTES`、`NUM_GPU_BLOCKS_OVERRIDE`、`ENABLE_HOST_MAPPING` 的文档默认值。
2. 在 3P1D 模板中 grep 这四个变量，记录 P/D 两个分支各自的模板值（提示：P 分支在 L133-L161，D 分支在 L265-L301；`NUM_GPU_BLOCKS_OVERRIDE` 在 P 分支 L146 有链式默认，任务级默认在 [L994](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance3P1D_92B_w8a8_open_omni_cache.yml#L994) 与 [L1024](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance3P1D_92B_w8a8_open_omni_cache.yml#L1024)）。
3. 在 `components/omni-cache/omni_cache/` 下用 `grep -rn "os.getenv(\"变量名\"" .` 找到代码兜底值。
4. 把三列并排填表，标出「三层不一致」的行。

**需要观察的现象**：`MAP_SIZE_BYTES` 三层分别是 500 GiB / 1200GB(P) 与 1000GB(D) / 无代码兜底（池大小只影响建池脚本，Python 侧不读）；`ENABLE_HOST_MAPPING` 三层分别是 P:0,D:1 / 模板统一 0 / 代码 1。

**预期结果**：至少发现 3 处三层不一致。结论：**生产模板部署时，文档默认值基本不生效，一切以模板 if 分支的 export 与运行日志回显为准**。若在真实环境核对日志输出，该步待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`kv_connector_extra_config` 里的 `p_node_list` 与环境变量 `P_NODE_LIST` 是什么关系？
**答案**：`P_NODE_LIST` 是 ansible/脚本层的环境变量形态（分号/逗号分隔字符串），脚本把它展开为列表后填进 `--kv-transfer-config` JSON 的 `kv_connector_extra_config.p_node_list` 字段，最终被 OmniCacheConnector 内部消费（见 [CONFIG_REFERENCE.md:L53-L58](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/docs/CONFIG_REFERENCE.md#L53-L58)）。同一个信息、两个载体，脚本负责转换。

**练习 2**：为什么 `NUM_GPU_BLOCKS_OVERRIDE` 必须小于 `OMNI_CACHE_LAYER_BYTES / (DP_SIZE_LOCAL * nbytes(kv_cache_block))`？
**答案**：`OMNI_CACHE_LAYER_BYTES` 是每 die 每层可用的 HBM/池预算，除以「本地 DP 数 × 单 block 字节数」才是每个 DP rank 实际能容纳的 block 数；`NUM_GPU_BLOCKS_OVERRIDE` 是告诉 vLLM 调度器的 block 上限，若超出物理容量，调度器会把请求排进不存在的 block，导致越界或启动校验失败。

**练习 3**：生产模板把 P 侧 `ENABLE_HOST_MAPPING` 硬编码为 0，而 USER_GUIDE 建议 D 侧设 1 并配 `VLLM_WORKER_MULTIPROC_METHOD=fork`（[USER_GUIDE.md:L64-L70](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/docs/USER_GUIDE.md#L64-L70)）。想在模板部署中打开 D 侧 MMU 直读，应该怎么做？
**答案**：P 侧分支是硬编码 `export ENABLE_HOST_MAPPING=0`（只影响 P，无需改）；D 侧分支写的是 `export ENABLE_HOST_MAPPING="${ENABLE_HOST_MAPPING:-0}"`，保留外部传入值，且 play 级默认是 `{{ enable_host_mapping | default('0') }}`，所以执行 ansible-playbook 时加 `-e enable_host_mapping=1` 即可让 D 侧闭合 MMU 直读；`fork` 相关变量同理需通过模板 environment 或 `-e` 注入。改后以 D 侧日志回显确认。

### 4.2 大页运维：两个脚本的分工与 set_hugepage_limit.sh 精读

#### 4.2.1 概念说明

OmniCache 的主机内存池建立在大页之上，涉及两个名字很像、层级完全不同的脚本：

| 脚本 | 层级 | 职责 | 持久性 | 谁调用 |
|------|------|------|--------|--------|
| `set_hugepage_limit.sh` | 宿主机内核配置层 | 决定内核**总共预留多少** 2MB 大页（写 sysfs + sysctl） | 永久（重启保留） | 人工 `sudo` 执行，部署前/切回时 |
| `setup_hugetlbfs_2MB.sh` | 运行时建池层 | 在既有预留内**挂载文件系统、创建池文件并写零** | 直到容器重建 | ansible 模板在容器内自动调用 |

二者是「池子的总闸」与「在池子里装水桶」的关系。第二个脚本只能**调大或维持**预留页数（详见下文 `reserve_pages` 的取 max 逻辑），**不能调小**；调小必须走第一个脚本的「先写 0 释放再写目标」流程。这正是 README_INT8 切回流程要单独执行 `set_hugepage_limit.sh` 的原因。

#### 4.2.2 核心流程

`set_hugepage_limit.sh` 主流程（六步）：

```text
解析 --target-pages（缺省则自动计算）
  → root 检查
  → 自动计算：预留系统保留量，得出目标页数
  → 找出并杀死占用 2MB 大页的进程（否则 nr_hugepages 改不动）
  → 写 /etc/sysctl.conf 持久化 vm.nr_hugepages
  → 写 sysfs：先写 0 释放旧池，sleep 3，再写目标值
  → 重试验证（最多 5 次，每次 sleep 2），最后 grep /proc/meminfo 展示
```

自动计算的公式（设物理内存为 \( M \) GiB）：

\[
\text{reserve} = \text{clamp}(M/10,\ 20,\ 200), \quad
\text{huge} = \min(M - \text{reserve},\ 1228), \quad
\text{pages} = \frac{\text{huge} \times 1024}{2} = 512\,\text{huge}
\]

即给系统保留 10%（下限 20GB、上限 200GB），大页总量再封顶约 1.2 TiB。以 2 TiB（2048GB）物理内存为例：reserve = 200，huge = min(1848, 1228) = 1228，pages = 628736。

`setup_hugetlbfs_2MB.sh` 主流程：

```text
由 MAP_SIZE_BYTES 换算页数（向上取整）或使用位置参数传入的页数
  → reserve_pages：target = max(当前值, wanted)，写 sysfs，重试 10 次
  → 挂载 hugetlbfs 到 /dev/hugepages（已挂载且类型正确则复用）
  → 删除旧池文件，truncate 创建新文件（大小 = MAP_SIZE_BYTES）
  → 写零：python mmap 逐页写 0，强制每一页真实分配并清零
```

页数换算公式：\[ \text{pages} = \lceil \text{bytes} / 2\,\text{MiB} \rceil \]。模板取值下：P 侧 1200GiB → 恰好 614400 页；D 侧 1TiB（模板注释写 1000GB）→ 恰好 524288 页；DSA Split 开启时二级池 = 主池 80% ≈ 419431 页，两池合计 `DSA_TOTAL_PAGES` = 943719 页。

#### 4.2.3 源码精读

**（1）自动计算与封顶**——[set_hugepage_limit.sh:L69-L88](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/tools/setup/set_hugepage_limit.sh#L69-L88)：从 `/proc/meminfo` 读 `MemTotal`，按上节公式算出 reserve 与 huge（L76 的 `max_huge_gb=1228` 即 1.2 TiB 封顶），若可用内存不足以覆盖保留量则直接报错退出。手动指定 `--target-pages` 时跳过计算，仅打印换算结果（[L86-L88](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/tools/setup/set_hugepage_limit.sh#L86-L88)）。

**（2）杀占用进程**——[set_hugepage_limit.sh:L91-L112](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/tools/setup/set_hugepage_limit.sh#L91-L112)：扫描 `/proc/*/smaps` 中含 `KernelPageSize: 2048 kB` 的进程，列出 PID 与命令行并要求交互确认（必须回答 `yes`）后才 `kill -9`。原因写在 L91 注释里：**有进程占着大页时 `nr_hugepages` 可能调不动**。这也解释了 README_INT8 切回流程第一步为何是「重启容器」——把容器里的 OmniCache 进程清掉，大页才处于可释放状态。

**（3）持久化 + 写 0 再写目标**——[set_hugepage_limit.sh:L114-L140](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/tools/setup/set_hugepage_limit.sh#L114-L140)：先改 `/etc/sysctl.conf` 的 `vm.nr_hugepages`（重启保留），再对 sysfs 先写 `0`（释放旧池）、`sleep 3`、写目标值。先写 0 是关键技巧：内核对「缩小预留」不会主动回收已分配页，归零强制释放后再按新目标重新分配。[L142-L155](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/tools/setup/set_hugepage_limit.sh#L142-L155) 循环重试 5 次确认实际分配量达标，失败时提示「可能内存碎片化，建议重启机器再跑」；[L157-L163](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/tools/setup/set_hugepage_limit.sh#L157-L163) 最后 `grep HugePages_ /proc/meminfo` 展示结果。

**（4）运行时建池：只增不减的预留**——[setup_hugetlbfs_2MB.sh:L80-L110](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/tools/setup/setup_hugetlbfs_2MB.sh#L80-L110)：`reserve_pages` 取 `target = current > wanted ? current : wanted`——**永远不会缩小预留**，只会在需要更多时继续写 sysfs。页数换算函数 [L72-L76](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/tools/setup/setup_hugetlbfs_2MB.sh#L72-L76) 做向上取整。挂载逻辑 [L113-L128](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/tools/setup/setup_hugetlbfs_2MB.sh#L113-L128) 在已挂载时校验文件系统类型必须是 `hugetlbfs`，否则卸载重挂。

**（5）建池与写零**——[setup_hugetlbfs_2MB.sh:L131-L184](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/tools/setup/setup_hugetlbfs_2MB.sh#L131-L184)：`truncate` 在 hugetlbfs 上创建指定大小的池文件（删除同名旧文件），随后内嵌 Python 用 `mmap` 逐页写零。写零不是洁癖：hugetlbfs 文件是**稀疏**的，不写零页就不会真实分配，且可能残留上一次服务的数据——逐页触碰强制每个大页落地并清零。

**（6）模板里的自动调用点**——prefill 侧 [模板 L154-L155](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance3P1D_92B_w8a8_open_omni_cache.yml#L154-L155) 用 `MAP_SIZE_BYTES=... OMNI_FILE=... bash setup_hugetlbfs_2MB.sh` 建主池；decode 侧 [L287-L296](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance3P1D_92B_w8a8_open_omni_cache.yml#L287-L296) 先建主池，`ENABLE_OMNI_CACHE_DSA_SPLIT=1` 时再按「主池页数 + 二级池页数」调用第二次（`DSA_TOTAL_PAGES` 作为位置参数），一次把两个池的预留顶到位。注意容器是 `--privileged=true` 启动的（[L49](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance3P1D_92B_w8a8_open_omni_cache.yml#L49)），因此容器内写 sysfs 生效的是**宿主机内核**的大页全局状态——容器边界挡不住这个副作用。

#### 4.2.4 代码实践

**实践目标**：在任意一台 Linux 机器（无需 NPU、无需改动系统）上完成大页观察与页数推算。

1. 只读观察当前大页状态：

   ```bash
   grep HugePages_ /proc/meminfo
   cat /sys/kernel/mm/hugepages/hugepages-2048kB/nr_hugepages 2>/dev/null || echo "内核无 2MB 大页目录"
   mount | grep hugetlbfs || echo "未挂载 hugetlbfs"
   ```

2. 用 bash 复算模板页数（与脚本同款整数运算）：

   ```bash
   bash -c 'MAP=1288490188800; echo "P 主池页数: $(( (MAP + 2*1024*1024 - 1) / (2*1024*1024) ))"'
   bash -c 'MAP=1099511627776; DSA=$(( MAP * 80 / 100 )); \
     PRI=$(( (MAP + 2097151) / 2097152 )); DSA_P=$(( (DSA + 2097151) / 2097152 )); \
     echo "D 主池: $PRI 页, DSA 池: $DSA_P 页, 合计: $((PRI + DSA_P)) 页"'
   bash -c 'echo "262144 页 = $(( 262144 * 2 / 1024 )) GiB"'
   ```

3. 若手头机器可以 sudo 且愿意做一次真实变更：`sudo bash components/omni-cache/tools/setup/set_hugepage_limit.sh --target-pages 1000`，观察交互提示与最终 `grep HugePages_` 输出；结束后再 `sudo ... --target-pages 0` 归还（会触发杀进程确认，谨慎选择没有重要业务的机器）。此步待本地验证。

**需要观察的现象**：第 1 步中 `HugePages_Total/Free/RSV` 三个计数值；第 2 步三个算式的输出。

**预期结果**：P 主池 614400 页（=1200GiB）；D 主池 524288 页、DSA 池 419431 页、合计 943719 页；`262144 页 = 512 GiB`——这正是 README_INT8 切回命令选择 262144 的含义：约 512 GiB，足以覆盖文档默认 500 GiB 池，又把上千 GiB 的生产预留压回「常规默认」水平。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `setup_hugetlbfs_2MB.sh` 无法把预留页数从 614400 降到 262144？
**答案**：`reserve_pages` 中 `target=$(( current > wanted ? current : wanted ))`，目标取当前值与请求值的较大者，只会增不会减；缩小预留必须先归零释放（`set_hugepage_limit.sh` 的「写 0 → sleep → 写目标」）再重新分配。

**练习 2**：`truncate` 出来的 hugetlbfs 文件为什么要逐页写零？
**答案**：hugetlbfs 文件是稀疏的，`truncate` 只设定了长度并不触发物理页分配；且池文件复用同名旧文件时可能残留上一轮 KV 数据。逐页写零既强制每个大页真实分配（避免运行期缺页抖动），又保证池内容干净（见脚本 L145-L147 注释）。

**练习 3**：`set_hugepage_limit.sh` 自动模式下，一台 1024GB 内存的机器会算出多少页？
**答案**：reserve = clamp(102.4, 20, 200) = 102（bash 整数除法 1024/10=102）；huge = min(1024−102, 1228) = 922；pages = 922×1024/2 = 472064 页（约 922 GiB）。

### 4.3 部署切换：启用 OmniCache 与切回普通部署

#### 4.3.1 概念说明

3P1D omni_cache 模板是「一份模板、两种形态」：`ENABLE_OMNI_CACHE` 闭合走 OmniCacheConnector + hugetlbfs 池，断开则回退 vLLM 原生 `LLMDataDistConnector`（KV 直接 RoCE 直传，不占大页）。开关由 ansible 额外变量控制：`-e enable_omni_cache=0` 即可让同一份模板跑基线形态。

**切换的核心矛盾在大页，不在服务本身**。服务进程停了，内核里 `vm.nr_hugepages` 的预留还在（sysctl 持久化 + sysfs 当前值），这些内存对普通部署来说「看得见用不着」——`free` 里体现为 used/reserved，普通进程拿不到。所以 README_INT8 专门有一节「从 OmniCache 服务切换到其他配置前的处理」，操作目标只有一个：把大页预留恢复到常规水平。

#### 4.3.2 核心流程

**启用（3P1D，四机 A3：3 个单机 P 实例 + 1 个单机 D 实例）**：

```text
1. 配好 3P1D inventory（P0/P1/P2 各 kv_rank 0/1/2，D0 一台，C 放 P0 机器）
2. 修改模板 environment 的必填项（LOG_PATH、MODEL_PATH、DOCKER_IMAGE_ID、容器名）
3. ansible-playbook -i omni_infer_inventory_used_for_3P1D.yml \
       omni_infer_server_template_performance3P1D_92B_w8a8_open_omni_cache.yml \
       --tags run_docker,run_server,run_proxy
4. run_server 阶段容器内自动执行 setup_hugetlbfs_2MB.sh 建池并 register_connectors()
```

**切回普通部署（README_INT8 三步）**：

```text
1. 在所有跑过 OmniCache 的相关容器上重启容器 → 释放服务占用的大页
2. 在代码根目录执行：
   bash omni-cache/tools/setup/set_hugepage_limit.sh --target-pages 262144
   → 归零旧预留（杀残留进程需确认）→ 重新分配 512 GiB 并写 sysctl 持久化
3. 之后即可用同一容器或其他模板跑普通配置服务
```

**验证闭环**：切换前后各执行一次 `free -g` 与 `grep HugePages_ /proc/meminfo`，对比可用内存与大页计数的变化。

#### 4.3.3 源码精读

**（1）启用命令与推荐形态**——[README_INT8.md:L186-L200](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README_INT8.md#L186-L200)：92B 用 `omni_infer_server_template_performance3P1D_92B_w8a8_open_omni_cache.yml`（推荐 3P1D），505B 用 `performance4P1D_505B_int8_open_omni_cache.yml`（推荐 4P81D16）；一条命令带 `run_docker,run_server,run_proxy` 三个 tag 拉起全链路。

**（2）切回前的处理**——[README_INT8.md:L202-L213](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README_INT8.md#L202-L213) 给出上面流程图的两步操作，并明确警告：不先释放大页，「可能导致后续服务可用内存不足或启动失败」。`--target-pages 262144` 即 512 GiB，被称为「恢复为默认值」。

**（3）模板的回退分支**——prefill 侧 else 分支 [模板 L162-L166](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance3P1D_92B_w8a8_open_omni_cache.yml#L162-L166) 与 decode 侧 else 分支 [L302-L306](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance3P1D_92B_w8a8_open_omni_cache.yml#L302-L306) 结构相同：`export ENABLE_OMNI_CACHE=0`、`export ENABLE_HOST_MAPPING=0`、`KV_CONNECTOR="${KV_CONNECTOR:-LLMDataDistConnector}"`——不建池、不注册 OmniCacheConnector，一切回到 u4 单元讲过的 LLMDataDist 链路。

**（4）连接器注册的时序保障**——启用分支在建池之后、`pd_run.sh` 之前执行 `python -c "from omni_cache.connector import register_connectors; register_connectors()"`（prefill [L157](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance3P1D_92B_w8a8_open_omni_cache.yml#L157)、decode [L298](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance3P1D_92B_w8a8_open_omni_cache.yml#L298)）。这一步把 `OmniCacheConnector` 注册进 vLLM 的连接器工厂（机制同 u4-l2 的 register.py），保证随后 `pd_run.sh` 拼出的 `--kv-transfer-config` 里的名字能被解析。

**（5）inventory 与 3P1D**——[omni_infer_inventory_used_for_3P1D.yml:L17-L60](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_inventory_used_for_3P1D.yml#L17-L60)：P 组按 P0/P1/P2 分组，各含一台机器，`kv_rank` 分别 0/1/2（`PREFILL_POD_NUM` 即不同 `host_ip` 的数量 = 3）；D 组单机 16 卡。api_port 按 `9000 + kv_rank*10` 展开，P0/P1/P2 分别落在 9000/9010/9020——切回基线形态时这些端口与 rank 规则保持不变，变得只有 KV 传输链路。

#### 4.3.4 代码实践

**实践目标**：不改任何机器，用源码走查一遍「切换可行性」，产出一份可执行的切换清单。

1. 在模板中定位两处 `if [[ "${ENABLE_OMNI_CACHE:-1}" == "1" ]]`（L133、L265），分别列出 if/else 两个分支 export 的变量差集，确认：else 分支不会触碰任何 `MAP_SIZE_BYTES`/`setup_hugetlbfs` 相关代码。
2. 追一遍变量入口：play 级 `ENABLE_OMNI_CACHE: "{{ enable_omni_cache | default('1') }}"`（[L19](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance3P1D_92B_w8a8_open_omni_cache.yml#L19)）→ docker exec `-e ENABLE_OMNI_CACHE=$ENABLE_OMNI_CACHE`（[L496](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance3P1D_92B_w8a8_open_omni_cache.yml#L496)、[L530](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance3P1D_92B_w8a8_open_omni_cache.yml#L530)）→ 脚本 if 分支。写出「用 `-e enable_omni_cache=0` 跑基线」的完整命令。
3. 写出切回三步命令（容器重启、set_hugepage_limit、验证），并标注每步的前置条件（如 sudo、交互确认 `yes`、需在代码根目录执行）。
4. 若有真实环境：先 `--tags run_docker,run_server,run_proxy` 拉起 OmniCache 形态，再演练切回。此步待本地验证。

**需要观察的现象**：第 1 步应得出「if 分支比 else 分支多出约 15 个 export 与 1 次建池调用」的结论；第 4 步（如有环境）观察切回后 `run_prefill.log` 中 `KV_CONNECTOR` 回显值变为 `LLMDataDistConnector`。

**预期结果**：切换清单能落在一张纸上；基线命令形如 `ansible-playbook -i ...3P1D.yml ...omni_cache.yml --tags run_docker,run_server,run_proxy -e enable_omni_cache=0`（注意仍需先完成大页释放，否则基线服务可用内存不足）。

#### 4.3.5 小练习与答案

**练习 1**：为什么切回流程要求「重启容器」而不是只停服务进程？
**答案**：大页释放的前提是没有进程占着大页页面。`set_hugepage_limit.sh` 自己也会扫描并 `kill -9` 占用进程，但容器内可能残留 vLLM worker、OX 传输线程等多种持有 mmap 的进程；重启容器一次性清空进程空间，保证归零预留时没有占用残留，也避免误杀（脚本的 kill 是交互确认式的，依赖人工判断）。

**练习 2**：切回后不执行 `set_hugepage_limit.sh --target-pages 262144` 会发生什么？
**答案**：内核仍按 sysctl/sysfs 里的旧值（例如 P 侧 614400 页 ≈ 1.2 TiB）预留大页。这部分内存对普通部署不可用，表现为机器内存被大量占走，轻则 vLLM 可分配 KV 显存/内存不足、吞吐下降，重则服务启动失败——正是 README_INT8 警告的场景。

**练习 3**：`--target-pages 262144` 之后，如果后续又要启用 OmniCache，还需要再跑一次大页准备吗？
**答案**：需要，但通常不用手工跑 `set_hugepage_limit.sh`：ansible 模板的 `setup_hugetlbfs_2MB.sh` 会按 `MAP_SIZE_BYTES` 自动把预留顶上去（只增不减的 `reserve_pages`）。`set_hugepage_limit.sh` 的自动模式（保留 10%、封顶 1.2TiB）适合首次初始化或想一次性给足预留的场景。

## 5. 综合实践

**任务：完整演练「启用 → 切回」闭环，量化大页对宿主机内存的影响。**（需要 4 台 A3 机器与 3P1D 环境；以下步骤待本地验证。）

1. **基线采样**（部署前，在每台将跑 P/D 的机器上）：

   ```bash
   free -g | tee /tmp/mem_before_deploy.txt
   grep HugePages_ /proc/meminfo | tee -a /tmp/mem_before_deploy.txt
   ```

2. **启用 OmniCache**：改好 inventory 与模板必填项后执行

   ```bash
   ansible-playbook -i omni_infer_inventory_used_for_3P1D.yml \
     omni_infer_server_template_performance3P1D_92B_w8a8_open_omni_cache.yml \
     --tags run_docker,run_server,run_proxy
   ```

   跟踪 `run_prefill.log`/`run_decode.log`，确认出现 `Reserving 2MB HugePages`、`Zero-filling`、`KV_TRANSFER_CONFIG` 回显且其中 `kv_connector` 为 `OmniCacheConnector`；等服务就绪后发一轮多轮对话请求，用 `num_computed_tokens` 日志与 `omni_cache_reuse_rate` 指标确认 KV 命中（指标口径见 u7-l1）。

3. **占用采样**：重复第 1 步命令存为 `mem_with_omnicache.txt`，重点看 `HugePages_Total/Rsvd` 与 `free` 的 used 变化（P 机应多出约 1200GiB 预留、D 机约 1000GiB，取决于机器内存是否够分配）。

4. **切回**：按 README_INT8 流程——在所有相关容器上重启容器；然后在代码根目录执行

   ```bash
   bash omni-cache/tools/setup/set_hugepage_limit.sh --target-pages 262144
   ```

   注意脚本会要求对占用大页的残留进程输入 `yes` 确认击杀；结束后记录 `grep HugePages_ /proc/meminfo`。

5. **对照报告**：三份采样并排，回答三个问题——大页预留使每台机器可用内存减少多少？切回后是否恢复？`262144` 页（512 GiB）与文档默认 500 GiB 池的关系是什么？

## 6. 本讲小结

- OmniCache 配置面 = 七类环境变量 + `--kv-transfer-config` JSON；同一变量存在「代码 os.getenv 兜底 < 文档默认 < ansible 模板显式值」三层默认，排障以模板 if 分支的 export 与运行日志回显为权威。
- 3P1D omni_cache 模板里 `ENABLE_OMNI_CACHE` 是总开关：闭合时建 hugetlbfs 池、`register_connectors()`、连接器切到 `OmniCacheConnector`；断开时三行归零并回退 `LLMDataDistConnector`。
- 大页运维是两层脚本分工：`set_hugepage_limit.sh` 管内核预留总量（sysctl 持久化、杀占用进程、写 0 释放再写目标、可增可减）；`setup_hugetlbfs_2MB.sh` 管建池（页数只增不减、挂载、truncate、逐页写零），由模板在容器内自动调用，且特权容器内的修改即宿主机内核修改。
- 页数换算 \(\lceil \text{bytes}/2\text{MiB} \rceil\)：模板 P 主池 614400 页、D 主池 524288 页、DSA 二级池为主池 80%（419431 页，两者合计 943719 页）；自动模式的预留公式为系统保留 \(\text{clamp}(M/10,20,200)\)、大页封顶 1228 GiB。
- 切回普通部署的三步：重启容器 → `set_hugepage_limit.sh --target-pages 262144`（512 GiB，恢复默认水平）→ 验证 `free -g` 与 `HugePages_` 计数；不做这一步，上千 GiB 预留会一直占着宿主机内存。
- `NUM_GPU_BLOCKS_OVERRIDE` 受每层预算约束（须小于 `OMNI_CACHE_LAYER_BYTES / (DP_local × block字节数)`）；`KV_CACHE_MEMORY_BYTES = OMNI_CACHE_LAYER_BYTES × HYBRID_ATTN_GROUP_SIZE(17)` 是模板把池预算换算进 vLLM 的桥梁。

## 7. 下一步学习建议

- 下一讲进入 **u8-l1「W8A8 量化与 jointfix 工具架构」**：本讲模板名中的 `w8a8` 正是 jointfix 的产物，学完量化链路后可以回到本讲模板，理解 `--dtype bfloat16` 与 INT8 权重共存的原因。
- 想继续深挖 omni-cache 本体，建议按 u7-l2 的源码地图补读 `omni_cache/connector/decode/kv_loader.py` 与 `process_manager.py`（后者是 `ENABLE_HOST_MAPPING` 的消费点之一，[process_manager.py:L216](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/omni_cache/connector/decode/process_manager.py#L216)），把「配置项 → 消费代码」逐个钉死。
- 若你负责生产运维，建议把综合实践产出的三份内存采样扩展成上线检查清单，并结合 u10-l4 的 505B 全特性部署方案，把「大页预算 = 拓扑中每台 P/D 机器的池大小之和」纳入拓扑规划。
