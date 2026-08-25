# 模型规格与部署拓扑：92B/505B 与 1P1D 等形态

> 本讲属于单元 1「入门：认识项目并跑通第一个推理服务」，承接 u1-l1 建立的全景认知（P/D/C 三类节点、PD 分离、四大组件），把视角落到**部署时真正要填写的那些文件**上：inventory 与部署模板。

## 1. 本讲目标

学完本讲，你应该能够：

1. 区分 openPangu-2.0-Flash（92B）与 openPangu-2.0-Pro（505B）对应的 ansible 配置目录，并能从模板文件名反推出「拓扑 + 规格 + 精度 + 特性」四要素。
2. 逐字段解释 ansible inventory 中 `P`/`D`/`C` 分组以及 `node_rank`、`kv_rank`、`port_offset`、`host_ip`、`ascend_rt_visible_devices` 的含义。
3. 根据端口计算公式，手工推出任意一台节点实际监听的 `node_port` 与 `api_port`。
4. 说清 BF16 与 INT8（w8a8）两套模板的差异来源。
5. 看懂 1P1D、3P1D、2P1D、4P81D16 四种拓扑各自需要几台机器、inventory 结构有什么不同。

## 2. 前置知识

本讲不要求你写过 ansible，但需要几个通俗概念：

- **inventory（主机清单）**：一个 YAML 文件，告诉 ansible「这次部署涉及哪些机器、每台机器扮演什么角色、带哪些变量」。可以类比成一份「演出名单」：谁是演员（机器 IP）、谁演什么角色（P/D/C）、每个角色什么台词（节点变量）。
- **playbook（剧本）与 tag**：部署模板 yml 是剧本，按 `--tags run_docker`、`--tags run_server,run_proxy` 这样的标签分幕执行（u1-l4 会实操）。
- **Jinja2 表达式**：inventory 里的 `"{{ global_port_base + port_offset.P + kv_rank * 10 }}"` 是模板表达式，ansible 执行时会代入真实数值算出结果。本讲的端口推导就是在「人肉执行」这些表达式。
- **端口与进程**：一台机器上每个监听进程独占一个端口。PD 分离会同时拉起多个 vLLM 服务进程和 proxy 进程，所以必须提前规划好端口，避免冲突。
- **A3 / 昇腾 910C**：本仓的参考硬件。一台 A3 服务器有 16 张 NPU 卡（inventory 中 `0,1,...,15`）。

回顾 u1-l1 的关键结论（本讲直接使用）：P 节点做 Prefill（计算密集），D 节点做 Decode（访存密集），C 节点跑 nginx + proxy 作统一入口；KV Cache 经 LLMDataDistConnector 从 P 传到 D。**inventory 就是这套角色分工在部署层面的落地。**

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md) | BF16 部署总说明：规格表、inventory/模板修改要点、启动命令 |
| [README_INT8.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README_INT8.md) | INT8 部署总说明：额外的 A2 镜像、3P1D/4P81D16 形态、omni-cache 特性 |
| [tools/ansible/92B/omni_infer_inventory_used_for_1P1D.yml](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_inventory_used_for_1P1D.yml) | 92B 最小拓扑：1 个 P + 1 个 D（本讲的主线样例） |
| [tools/ansible/92B/omni_infer_inventory_used_for_3P1D.yml](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_inventory_used_for_3P1D.yml) | 92B 多 P 拓扑：3 个单机 P 实例 + 1 个 D（实践任务样例） |
| [tools/ansible/505B/omni_infer_inventory_used_for_2P1D.yml](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_inventory_used_for_2P1D.yml) | 505B 拓扑：2 个**双机** P 实例 + 1 个四机 D 实例（讲 `node_rank` 的关键样例） |
| [tools/ansible/505B/omni_infer_inventory_used_for_4P81D16.yml](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_inventory_used_for_4P81D16.yml) | 505B 大形态：4 个单机 P 实例 + 1 个双机 D 实例（omni-cache 推荐形态） |
| [tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml) | BF16 服务模板；本讲引用它「消费 inventory 变量」的两段代码 |

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：**部署规格** → **ansible inventory** → **端口规划** → **拓扑形态与 BF16/INT8 模板差异**。

### 4.1 部署规格：92B 与 505B 的双目录体系

#### 4.1.1 概念说明

本仓支持两个模型规格，ansible 配置按规格分成两个平行目录：

- **openPangu-2.0-Flash（92B）**：轻量规格，配置在 `tools/ansible/92B/`。
- **openPangu-2.0-Pro（505B）**：大规格，配置在 `tools/ansible/505B/`。

为什么要分目录？因为两个规格的**典型拓扑、机器数量、并行参数、显存预算**都不同：92B 一两台 A3 就能跑，505B 动辄需要 6～8 台。规格决定了「合理的部署形态」，所以直接用目录隔离，避免一份配置里堆满 if-else。

BF16 与 INT8（w8a8）两套模板的差异也源于规格：INT8 量化后权重与 KV Cache 占用更小，同样的硬件能塞下**更大的批量、更多的并发序列**，因此模板中的 `--max-num-seqs`、`--max-num-batched-tokens` 等参数整体调大（详见 4.4）。

#### 4.1.2 核心流程

为一个模型选部署配置的决策链：

```text
你的权重是什么规格？
├── 92B (Flash)  → tools/ansible/92B/
└── 505B (Pro)   → tools/ansible/505B/
         │
         ▼
你的权重是什么精度？
├── BF16 原始权重  → 选 *_bf16_open.yml 模板
└── W8A8 INT8 权重 → 选 *_w8a8_open.yml / *_int8_open.yml 模板
         │
         ▼
你有多少机器 / 要开什么特性？（omni-cache？A2 硬件？）
         │
         ▼
得到组合：inventory（拓扑） + 模板（参数）
```

模板文件名的阅读公式：

```text
omni_infer_server_template_performance{拓扑}{规格}_{精度}_{变体}.yml
                                    │        │       │      │
                          1P1D/3P1D/2P1D/4P1D  92B/505B bf16/w8a8/int8  open / open_omni_cache / A2_w8a8_open
```

#### 4.1.3 源码精读

BF16 README 的规格表给出了两个规格的「默认答案」——92B 配 1P1D（2 机），505B 配 2P1D（8 机）：

[README.md:L9-L12](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L9-L12) —— 模型与配置说明表：`92B → tools/ansible/92B/，典型部署 1P1D（2机A3）`；`505B → tools/ansible/505B/，典型部署 2P1D（8机A3）`。

INT8 README 的同位置表格则扩展了可选形态，明确写出 3P1D 与 4P81D16 两种更大拓扑：

[README_INT8.md:L9-L12](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README_INT8.md#L9-L12) —— 92B 支持 `1P1D（2机A3/A2）、3P1D（4机A3）`；505B 支持 `2P1D（8机A3）、4P81D16（6机A3）`。

INT8 的量化工具链入口也在这份 README 里：

[README_INT8.md:L130-L132](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README_INT8.md#L130-L132) —— 「INT8量化」小节指向 `tools/quant/jointfix/README.md`（u8 单元会专门精读，本讲只需知道 w8a8 权重由 jointfix 产出）。

目前 `tools/ansible/` 下实际存在的 12 个 yml 文件（可用 `git ls-files tools/ansible` 复核）按命名规律归类：

| inventory（拓扑） | 模板（参数） |
| --- | --- |
| `92B/…used_for_1P1D.yml` | 92B：`performance1P1D_92B_bf16_open`、`performance1P1D_92B_w8a8_open` |
| `92B/…used_for_1P1D_A2.yml` | 92B：`performance1P1D_92B_A2_w8a8_open`（A2 硬件变体） |
| `92B/…used_for_3P1D.yml` | 92B：`performance3P1D_92B_w8a8_open_omni_cache` |
| `505B/…used_for_2P1D.yml` | 505B：`performance2P1D_505B_bf16_open`、`performance2P1D_505B_int8_open` |
| `505B/…used_for_4P81D16.yml` | 505B：`performance4P1D_505B_int8_open_omni_cache` |

注意两个规律：① **inventory 与模板是自由组合的**（README_INT8 的命令就演示了用 3P1D inventory 配 3P1D 模板）；② omni-cache 特性通过**换一份模板**开启，inventory 不变（[README_INT8.md:L186-L200](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README_INT8.md#L186-L200)）。

#### 4.1.4 代码实践

1. **实践目标**：建立「文件名 ↔ 部署含义」的直觉，确认每个规格可用的组合。
2. **操作步骤**：
   ```bash
   cd /path/to/openPangu-2.0-Infer
   git ls-files tools/ansible
   ```
   把输出按「inventory / 模板」两列归类，再为每个模板文件名做四要素拆解（拓扑、规格、精度、变体）。
3. **需要观察的现象**：文件总数 12 个；505B 没有 `w8a8` 字样而是用 `int8`；92B 多出一个 `A2` 变体；带 `omni_cache` 后缀的模板各有两份。
4. **预期结果**：与上面 4.1.3 的归档表一致。若发现文件与表不符，说明仓库已更新，请以 `git ls-files` 实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：文件名 `omni_infer_server_template_performance3P1D_92B_w8a8_open_omni_cache.yml` 的每个片段分别代表什么？

**答案**：`performance` 是固定前缀；`3P1D` = 3 个 Prefill 实例 + 1 个 Decode 实例；`92B` = openPangu-2.0-Flash 规格；`w8a8` = 权重为 W8A8 INT8 量化版本；`open` = 开源版配置；`omni_cache` = 开启 omni-cache 主机内存 KV 池特性。

**练习 2**：为什么 BF16 README 里没有出现 3P1D，INT8 README 里却有？

**答案**：BF16 权重与 KV Cache 占用大，92B BF16 的推荐形态就是最小的 1P1D；INT8 量化释放了显存与内存带宽，机器稍多时可以升级到 3P1D 获得更高 Prefill 吞吐，因此更大拓扑写在 INT8 README 中。

### 4.2 ansible inventory：P/D/C 三类节点逐字段精读

#### 4.2.1 概念说明

inventory 是 ansible 的「机器清单 + 角色分配表」。本仓所有 inventory 都是同一骨架：

- 顶层 `all.vars`：全组共享的全局变量（端口基数、组间端口偏移）。
- `children` 下三个组：`P`（Prefill 节点）、`D`（Decode 节点）、`C`（proxy 节点）。
- 每个 host 携带自己的变量，模板（playbook）按 `group_names` 判断当前机器属于哪个组，再决定执行哪段任务。

各字段含义：

| 字段 | 含义 | 谁在用 |
| --- | --- | --- |
| `ansible_host` | 这台机器的 SSH 地址（ansible 实际连的 IP） | ansible 本身 |
| `host_ip` | 该机器所属**逻辑实例的主节点 IP**（见 4.4 的双机实例） | 模板拼 API 列表时去重 |
| `node_rank` | 机器在**同一实例内部**的序号（0 起） | api_port 计算、组内通信 |
| `kv_rank` | 该 **P 实例**在 KV 传输域里的全局序号（0 起） | node_port/api_port 计算、kv-transfer-config |
| `node_port` | 该实例的**通信端口**（llmdatadist 组网用，Jinja2 算出） | 传给 `pd_run.sh` 的 `NODE_PORT` |
| `api_port` | 该实例 **vLLM API server 端口**（Jinja2 算出） | proxy 的 upstream 列表 |
| `ascend_rt_visible_devices` | 容器内可见的 NPU 卡号列表（A3 单机 16 卡） | 决定张量并行度 |
| `role: "C"` | 显式标记 proxy 角色 | C 节点任务 |

区分两个「rank」是本讲最容易混淆的点，一句话记法：**`kv_rank` 区分不同的 P 实例（跨机），`node_rank` 区分同一实例内的不同机器（实例内）。**

#### 4.2.2 核心流程

部署执行时 inventory 数据的流动：

```text
ansible-playbook -i inventory.yml 模板.yml --tags run_server,run_proxy
        │
        ├─ ansible 读 all.vars：global_port_base=8000, base_api_port=9000,
        │                        proxy_port=7000, port_offset={P:0, D:100}
        ├─ 对 P 组每台机器：解析 node_port/api_port 模板表达式 → 具体数字
        ├─ 模板任务用 when: "'P' in group_names" 判断角色，选 prefill/decode 任务
        └─ 把 NODE_PORT / API_PORT / KV_RANK / NODE_RANK 等注入环境变量，
           交给 pd_run.sh → vllm serve
```

#### 4.2.3 源码精读

先看全局变量区，这是所有端口计算的「公理」：

[tools/ansible/92B/omni_infer_inventory_used_for_1P1D.yml:L3-L14](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_inventory_used_for_1P1D.yml#L3-L14) —— `global_port_base: 8000`、`base_api_port: 9000`、`proxy_port: 7000`，以及 `port_offset: {P: 0, D: 100}`，注释写明 P 组实际占用 8000-8099、D 组占用 8100-8199。`ansible_user: root` 和 `StrictHostKeyChecking=no` 则让 ansible 能免密直连各节点。

再看 P 组单节点的完整写法：

[tools/ansible/92B/omni_infer_inventory_used_for_1P1D.yml:L16-L25](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_inventory_used_for_1P1D.yml#L16-L25) —— 单 P 节点 `p0`：`kv_rank: 0`，`node_port` 与 `api_port` 都是表达式（1P1D 只有一个 P 实例，公式里直接 `+ kv_rank`，没有 ×10 的块预留），`ascend_rt_visible_devices` 列出全部 16 张卡。

D 组与 C 组：

[tools/ansible/92B/omni_infer_inventory_used_for_1P1D.yml:L27-L35](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_inventory_used_for_1P1D.yml#L27-L35) —— D 节点 `d0` **没有 `kv_rank`**：一个拓扑只有一个 D 实例，它的 `node_port` 固定为 `8000 + port_offset.D`，`api_port` 用 `node_rank` 递增。

[tools/ansible/92B/omni_infer_inventory_used_for_1P1D.yml:L37-L43](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_inventory_used_for_1P1D.yml#L37-L43) —— C 节点只有 `proxy_port + node_rank` 一个端口（proxy 是 nginx 入口，不参与 KV 传输，所以无需 kv/通信端口）。README 明确说 **proxy 节点 IP 设为 P 节点 IP**（[README.md:L57](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L57)），即 C 与某台 P 机同机部署。

这些字段最终被模板消费。92B BF16 模板的 prefill 启动任务：

[tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L832-L850](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L832-L850) —— 任务「Run the Omniai service for prefill instances」把 inventory 里的 `node_port`、`api_port`、`kv_rank`、`node_rank`、`host_ip` 原样注入环境变量（`NODE_PORT`、`API_PORT`、`KV_RANK`、`NODE_RANK`、`HOST_IP`），并**用 `ascend_rt_visible_devices.split(',') | length` 现场算出张量并行度** `PREFILL_TENSOR_PARALLEL_SIZE`（16 卡 → TP=16）。`when: "'P' in group_names"` 就是「角色判断」：只有 P 组的机器才执行这条任务。

#### 4.2.4 代码实践

1. **实践目标**：用 ansible 自带工具渲染 inventory，让机器替你「执行」Jinja2 端口表达式，验证人肉推导。
2. **操作步骤**（在装有 ansible 的执行机上，无需 NPU）：
   ```bash
   # 看拓扑结构（分组树）
   ansible-inventory -i tools/ansible/92B/omni_infer_inventory_used_for_3P1D.yml --graph

   # 看单个主机解析后的全部变量（以 p1 为例）
   ansible-inventory -i tools/ansible/92B/omni_infer_inventory_used_for_3P1D.yml --host p1
   ```
3. **需要观察的现象**：`--graph` 输出 `all` → `P` → `P0|P1|P2` → `p0|p1|p2` 的树；`--host p1` 输出的 `node_port`/`api_port` 已是具体数字而不是表达式。
4. **预期结果**：`p1` 的 `kv_rank=1`、`node_rank=0`，`node_port=8010`、`api_port=9010`（推导见 4.3）。具体输出格式随 ansible 版本略有差异，数字部分**待本地验证**后与你 4.3 的手算结果对照。

#### 4.2.5 小练习与答案

**练习 1**：`kv_rank` 和 `node_rank` 分别在什么维度上编号？

**答案**：`kv_rank` 给**不同的 P 实例**编号（跨机器的全局序号，参与 KV 传输域），一个拓扑里每个 P 实例一个；`node_rank` 给**同一逻辑实例内部的多台机器**编号（例如 2P1D 中 P0 实例由两台机器组成，两台的 `node_rank` 分别是 0 和 1，`kv_rank` 都是 0）。

**练习 2**：为什么 C 节点没有 `ascend_rt_visible_devices` 和 `kv_rank`？

**答案**：C 节点只跑 nginx + omni-proxy 做请求转发，既不加载模型（不需要 NPU 卡列表，也就不算张量并行度），也不参与 KV Cache 传输（不需要 kv_rank / 通信端口），只需要一个 `proxy_port`。

**练习 3**：把 inventory 里 `p0` 的 `ansible_host` 改成新机器 IP，但忘了改 `host_ip`，会发生什么？

**答案**：模板生成 proxy upstream 列表时要求 `ansible_host == host_ip` 才登记（见 4.4.3 引用的 L591-L596 过滤逻辑），两者不一致会导致这台 P 机器不出现在 `PREFILL_API_SERVER_LIST` 里，proxy 无法把请求转发给它。README 也特别提醒「注意 `ansible_host` 和 `host_ip` 都要修改为部署的 IP 地址」（[README.md:L57](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L57)）。

### 4.3 端口规划：三级端口体系与 kv_rank×10 的预留

#### 4.3.1 概念说明

一次 PD 分离部署会在不同机器上拉起多个进程，每类进程用一类端口：

| 端口类别 | 基数 | 用途 |
| --- | --- | --- |
| `proxy_port`（7000） | 7000 | C 节点 nginx 入口，客户端 curl 打这里 |
| `node_port`（8000 段） | 8000 + offset | 实例通信端口（llmdatadist 组网，交给 `NODE_PORT`） |
| `api_port`（9000 段） | 9000 + offset | 每个实例的 vLLM API server，proxy 按 `ip:api_port` 转发 |

`port_offset`（P=0，D=100）把 8000 段劈成两个子段：P 组 8000-8099、D 组 8100-8199，**P 和 D 即使同机部署也不会撞端口**。

多 P 时的关键设计是 `kv_rank * 10`：每个 P 实例预留 10 个端口的小块，块内再用 `node_rank` 细分。这样「实例数」与「实例内机器数」两个维度都扩容空间，而不用改任何基数。

#### 4.3.2 核心流程

3P1D 拓扑（多 P 的通用公式）：

\[ \text{node\_port}(P_i) = \text{global\_port\_base} + \text{port\_offset.P} + 10 \times \text{kv\_rank}_i = 8000 + 10\,\text{kv\_rank}_i \]

\[ \text{api\_port}(P_i) = \text{base\_api\_port} + \text{port\_offset.P} + 10 \times \text{kv\_rank}_i + \text{node\_rank}_i = 9000 + 10\,\text{kv\_rank}_i + \text{node\_rank}_i \]

D 侧（单实例）：

\[ \text{node\_port}(D) = 8000 + 100 = 8100, \qquad \text{api\_port}(D) = 9000 + 100 + \text{node\_rank} \]

C 侧：\( \text{port} = 7000 + \text{node\_rank} \)。

代入 3P1D inventory 的每台机器：

| 主机 | 组 | kv_rank | node_rank | node_port | api_port |
| --- | --- | --- | --- | --- | --- |
| p0 | P0 | 0 | 0 | 8000 | 9000 |
| p1 | P1 | 1 | 0 | 8010 | 9010 |
| p2 | P2 | 2 | 0 | 8020 | 9020 |
| d0 | D0 | —（无此字段） | 0 | 8100 | 9100 |
| c0 | C | — | 0 | 7000（proxy_port） | — |

对照 1P1D：P 公式退化为 `+ kv_rank`（没有 ×10），p0 的 node_port/api_port 仍是 8000/9000；D、C 完全一致（8100/9100、7000）。

#### 4.3.3 源码精读

端口公式的源头在 3P1D inventory 的 P 组：

[tools/ansible/92B/omni_infer_inventory_used_for_3P1D.yml:L19-L28](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_inventory_used_for_3P1D.yml#L19-L28) —— P0 子组的 `p0`：`node_port: "{{ global_port_base + port_offset.P + kv_rank * 10 }}"`、`api_port: "{{ base_api_port + port_offset.P + kv_rank * 10 + node_rank }}"`。这就是 4.3.2 两条公式的出处。

[tools/ansible/92B/omni_infer_inventory_used_for_3P1D.yml:L29-L48](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_inventory_used_for_3P1D.yml#L29-L48) —— P1（`kv_rank: 1`）与 P2（`kv_rank: 2`）套同一公式，只有 `kv_rank` 递增——三个 P 实例天然得到 8000/8010/8020 三个互不冲突的通信端口。

D 组公式只用 `port_offset.D` 与 `node_rank`：

[tools/ansible/92B/omni_infer_inventory_used_for_3P1D.yml:L50-L60](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_inventory_used_for_3P1D.yml#L50-L60) —— `d0` 的 `node_port: "{{ global_port_base + port_offset.D }}"` 固定 8100；`api_port` 随 `node_rank` 增长（多机 D 实例每台一个 API 端口）。

api_port 的消费端在 92B BF16 模板「Register all values」任务：

[tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L582-L614](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L582-L614) —— 遍历 `groups['P']` 把每台 P 机器拼成 `ansible_host:api_port` 组成 `PREFILL_API_SERVER_LIST`（proxy 的 prefill upstream 列表）；遍历 `groups['D']` 拼 `ip:api_port@卡数` 组成 `DECODE_API_SERVER_LIST_ALL`。注意 D 侧默认值写死 `9100`（L605）——正是 `9000 + 100` 的 D 组 api_port 基数，与 inventory 公式互相印证。

#### 4.3.4 代码实践

1. **实践目标**：不依赖 ansible，纯手工「执行」Jinja2 表达式，算出 3P1D 全部节点的端口。
2. **操作步骤**：
   1. 打开 [tools/ansible/92B/omni_infer_inventory_used_for_3P1D.yml](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_inventory_used_for_3P1D.yml)，记下 `all.vars` 的四个基数（8000 / 9000 / 7000 / offset）。
   2. 对 p0、p1、p2、d0、c0 逐个代入公式，填出 4.3.2 那张表的空格。
   3. 再算一遍 1P1D（注意公式没有 ×10），对比 p0 的结果是否相同（应同为 8000/9000）。
3. **需要观察的现象**：每台 P 机器间隔正好 10；D 的通信端口与所有 P 都隔了 100。
4. **预期结果**：与 4.3.2 的表格完全一致。若某台机器端口与表不符，先检查你是否用了 1P1D 的「无 ×10」公式去算 3P1D。

#### 4.3.5 小练习与答案

**练习 1**：3P1D 中 `p2` 的 `node_port` 和 `api_port` 是多少？如果要扩成 4P1D，新 P 实例的端口是多少？

**答案**：`p2` 的 `kv_rank=2`，`node_port = 8000 + 0 + 2×10 = 8020`，`api_port = 9000 + 0 + 2×10 + 0 = 9020`。扩成 4P1D 时新实例 `kv_rank=3`，端口为 8030 / 9030，仍在 P 组的 8000-8099 段内，无需改动任何基数。

**练习 2**：为什么 P 组公式用 `kv_rank` 而 D 组公式用 `node_rank`？

**答案**：端口要保证「不同实例不冲突、同实例不同机器也不冲突」。P 侧实例数可变（1P/2P/3P/4P），每个实例一个 `kv_rank`，用 `kv_rank×10` 划块；D 侧一个拓扑只有一个 D 实例，`node_port` 固定 8100 即可，机器间的差异只体现在 `api_port` 的 `node_rank` 上。

**练习 3**：客户端的 curl 请求最终打到哪个端口？它和 `node_port`、`api_port` 有什么关系？

**答案**：打到 C 节点的 `proxy_port`（默认 7000，[README.md:L154-L160](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L154-L160)）。proxy 再把请求转发到各实例的 `api_port`（9000 段），`node_port`（8000 段）只服务于实例间的 KV/组网通信，客户端完全不接触。

### 4.4 拓扑形态与 BF16/INT8 模板差异

#### 4.4.1 概念说明

四种典型拓扑对应两种 inventory 组织方式：

- **每个 P 子组 = 一个独立的单机 P 实例**（1P1D、3P1D、4P81D16）：`P0`/`P1`/… 各含一个 host，`kv_rank` 各不相同。
- **每个 P 子组 = 一个多机 P 实例**（2P1D）：子组内放多台机器（`node_rank` 0..k，共享同一 `kv_rank`），多机张量并行跑一个大实例。

拓扑名与机器数的换算（含与 C 同机的 P 主节点）：

| 拓扑 | inventory 结构 | 机器数（README 口径） |
| --- | --- | --- |
| 1P1D | P 1 台 + D 1 台 | 2 机 A3 |
| 3P1D | P 3 台 + D 1 台 | 4 机 A3 |
| 2P1D | P 2 组×2 台 + D 1 组×4 台 | 8 机 A3 |
| 4P81D16 | P 4 组×1 台 + D 1 组×2 台 | 6 机 A3 |

BF16 与 INT8（w8a8）模板的差异**不是两份完全不同的脚本**，而是同一骨架上的参数调整：INT8 量化后权重与 KV Cache 更省，同样的卡能承载更大并发，所以批量类参数整体上调，并追加 `--kv-cache-dtype li_int8_ds_mla` 让 KV Cache 也用 INT8 存储。注意两份模板的模型计算精度仍是 `--dtype bfloat16`——文件名里的 w8a8 指的是**权重本身是 W8A8 量化权重**（由 `MODEL_PATH` 指向 jointfix 量化产物决定，vLLM 从模型 `config.json` 的量化配置读取），模板参数只是配套调优。

#### 4.4.2 核心流程

「拓扑 → inventory」的翻译规则：

```text
N 个 P 实例？
├── 每个实例单机 → P 下建 N 个子组，每组 1 host，kv_rank = 0..N-1
└── 每个实例 M 机 → P 下建 N 个子组，每组 M host，
                    组内 node_rank = 0..M-1 且 kv_rank 相同，
                    组内所有机器的 host_ip 填该组首机的 ansible_host
D 实例同理（一个 D 子组，可含多机）
C 固定单节点（与某个 P 节点同机）
```

`host_ip` 填「实例主节点 IP」的动机：proxy 的 upstream 列表要的是**实例级**端点而不是机器级端点，多机实例只需登记一次（由主节点对外提供 API）。

#### 4.4.3 源码精读

2P1D 的 P0 子组是「双机实例」的教科书样例：

[tools/ansible/505B/omni_infer_inventory_used_for_2P1D.yml:L18-L35](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_inventory_used_for_2P1D.yml#L18-L35) —— P0 组两台机器：`p0`（`node_rank: 0`）与 `p1`（`node_rank: 1`），二者 `kv_rank` 都是 0（同一个 P 实例）。关键细节：`p1` 的 `ansible_host` 是 `127.0.0.2`，但 `host_ip` 仍填 `127.0.0.1`（L34）——即 P0 实例主节点 `p0` 的 IP。

[tools/ansible/505B/omni_infer_inventory_used_for_2P1D.yml:L36-L53](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_inventory_used_for_2P1D.yml#L36-L53) —— P1 组同构：`p2`（主）+ `p3`，`kv_rank: 1`。两台机器 api_port 分别是 9000/9001（P0）与 9010/9011（P1），`node_rank` 维度的端口细分在这里生效。

[tools/ansible/505B/omni_infer_inventory_used_for_2P1D.yml:L57-L86](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_inventory_used_for_2P1D.yml#L57-L86) —— D0 组四台机器 `d0..d3`（`node_rank` 0..3），所有机器的 `host_ip` 都填 `127.0.0.5`（首机 `d0` 的 IP）：505B 的 D 是一个四机大实例。

模板侧「为什么 host_ip 要这么填」的答案：

[tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L586-L598](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L586-L598) —— 生成 `PREFILL_API_SERVER_LIST` 时只有满足 `ansible_host == host_ip` 的机器才会被登记：多机实例里只有主节点通过这道过滤，从机被自然去重，proxy 拿到的是「一实例一端点」的干净列表。

4P81D16（omni-cache 推荐形态）则回到「单机 P 实例」结构：

[tools/ansible/505B/omni_infer_inventory_used_for_4P81D16.yml:L18-L57](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_inventory_used_for_4P81D16.yml#L18-L57) —— P0～P3 四个子组各含一台机器，`kv_rank` 0..3，通信端口 8000/8010/8020/8030。

[tools/ansible/505B/omni_infer_inventory_used_for_4P81D16.yml:L59-L76](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_inventory_used_for_4P81D16.yml#L59-L76) —— D0 组两台机器（`d0`、`d1`）。README_INT8 对该形态的解释是「4 个单机组 P 实例，1 个双机组 D 实例」（[README_INT8.md:L190-L192](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README_INT8.md#L190-L192)）；命名中的「16」在模板里能找到对应线索——decode 侧 `export OMNI_CACHE_LOCAL_DP_SIZE=16`（[tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml:L289](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L289)），「81/16」的完整并行规格推导将在 u4 的 kv-transfer 讲义展开。

最后看 BF16 与 w8a8 模板的具体差异（两文件行号一一对应，可用 diff 复现）：

- Prefill 参数（[bf16 模板:L92](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L92) vs [w8a8 模板:L92](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_w8a8_open.yml#L92)）：`--max-num-batched-tokens` 16384 → 32768，`--max-num-seqs` 4 → 12，并新增 `--kv-cache-dtype li_int8_ds_mla`；
- 显存水位（L95）：`GPU_UTIL` 0.8 → 0.85；
- Decode 参数（[bf16:L202](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L202) vs [w8a8:L202](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_w8a8_open.yml#L202)）：`--max-num-seqs` 3 → 4，图捕获尺寸 `[12]` → `[16]`，同样追加 KV INT8；
- proxy 侧（[bf16:L290-L291](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L290-L291) vs [w8a8:L290-L291](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_w8a8_open.yml#L290-L291)）：`--omni-proxy-prefill-max-num-seqs` 4 → 12、decode 3 → 4，与引擎侧批量保持一致。

两份模板其余结构（inventory 消费、docker 拉起、proxy 部署）完全相同——**换精度 = 换权重 + 换参数档位，不改部署骨架**。

#### 4.4.4 代码实践

1. **实践目标**：亲手用 diff 找出 BF16 与 w8a8 模板的全部差异行，验证 4.4.3 的清单。
2. **操作步骤**：
   ```bash
   cd tools/ansible/92B
   diff omni_infer_server_template_performance1P1D_92B_bf16_open.yml \
        omni_infer_server_template_performance1P1D_92B_w8a8_open.yml
   ```
3. **需要观察的现象**：diff 只报告 4 处变化（L92、L95、L202、L290-291），全部是参数值调整与 `--kv-cache-dtype li_int8_ds_mla` 的新增，没有任何任务结构变化。
4. **预期结果**：与 4.4.3 列出的四条差异一一对应。可再对比 505B 的 `2P1D_505B_bf16_open.yml` 与 `2P1D_505B_int8_open.yml`，观察大规格上同样的规律（具体差异行**待本地验证**）。

#### 4.4.5 小练习与答案

**练习 1**：2P1D inventory 中 `p1` 的 `host_ip` 为什么填 `127.0.0.1` 而不是自己的 `ansible_host` `127.0.0.2`？

**答案**：`host_ip` 表示「该机器所属逻辑实例的主节点 IP」。P0 实例由 `p0`（主）和 `p1` 组成，两台的 `host_ip` 都应指向主节点 `p0`。模板生成 `PREFILL_API_SERVER_LIST` 时用 `ansible_host == host_ip` 过滤（模板 L591-L596），这样多机实例只登记主节点一次，proxy upstream 不会出现重复端点。

**练习 2**：w8a8 模板把 `--max-num-seqs` 从 4 调到 12、`--max-num-batched-tokens` 从 16384 调到 32768，依据是什么？

**答案**：W8A8 量化把权重（和 `--kv-cache-dtype li_int8_ds_mla` 下的 KV Cache）的显存占用压到 BF16 的一半左右，同容量 HBM 能装下更多并发序列与更大批量，所以模板同步上调批量参数与 `GPU_UTIL` 水位来兑现量化收益。

**练习 3**：一个 4P81D16 集群最少需要几台 A3？分别扮演什么角色？

**答案**：6 台。4 台各跑一个单机 P 实例（P0～P3，`kv_rank` 0..3），2 台组成一个双机 D 实例（D0 组 `d0`、`d1`）；C 节点与某个 P 节点（如 `p0`）同机，不额外占机器。

## 5. 综合实践

**任务：对照 1P1D 与 3P1D 两份 inventory，画出 3P1D 的节点拓扑图，标出每个 P 节点的 `kv_rank`、`node_port` 与 `api_port` 计算结果。**

### 步骤

1. 重读 [tools/ansible/92B/omni_infer_inventory_used_for_1P1D.yml](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_inventory_used_for_1P1D.yml) 与 [tools/ansible/92B/omni_infer_inventory_used_for_3P1D.yml](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_inventory_used_for_3P1D.yml)，注意两者 P 组公式的差别（是否 `kv_rank * 10`）。
2. 按 4.3.2 的公式逐台计算 3P1D 五个主机的 `node_port` / `api_port`。
3. 画拓扑图：客户端 → C 节点（7000）→ 三个 P 的 api_port；三个 P 的 KV Cache 汇入 D 节点。
4. 用 4.2.4 的 `ansible-inventory --host p1` 交叉验证你的手算结果。

### 参考答案（拓扑图与端口表）

```text
                        客户端 curl
                            │  POST /v1/chat/completions
                            ▼
              ┌─────────────────────────────┐
              │ C 节点 c0（与 p0 同机）        │
              │ nginx + omni-proxy  :7000    │
              └──────┬──────────┬────────┬───┘
                     │ :9000    │ :9010  │ :9020      ← api_port
              ┌──────▼───┐ ┌────▼─────┐ ┌▼─────────┐
              │ P0 p0    │ │ P1 p1    │ │ P2 p2    │   Prefill 实例（各 1 机 16 卡）
              │ kv_rank 0│ │ kv_rank 1│ │ kv_rank 2│
              │ node_port│ │ node_port│ │ node_port│
              │   = 8000 │ │   = 8010 │ │   = 8020 │   ← node_port = 8000 + 10×kv_rank
              │ api 9000 │ │ api 9010 │ │ api 9020 │
              └──────┬───┘ └────┬─────┘ └┬─────────┘
                     │          │         │
                     └──── KV Cache（LLMDataDistConnector）────┘
                                  │
                                  ▼ :8100（node_port）/ :9100（api_port）
              ┌─────────────────────────────┐
              │ D 节点 d0（1 机 16 卡）        │   Decode 实例
              └─────────────────────────────┘
```

| 主机 | 角色 | kv_rank | node_rank | node_port | api_port |
| --- | --- | --- | --- | --- | --- |
| c0 | C（proxy） | — | 0 | 7000 | — |
| p0 | P0 实例 | 0 | 0 | 8000 | 9000 |
| p1 | P1 实例 | 1 | 0 | 8010 | 9010 |
| p2 | P2 实例 | 2 | 0 | 8020 | 9020 |
| d0 | D0 实例 | — | 0 | 8100 | 9100 |

机器总数：4 台 A3（3 台 P + 1 台 D，C 与 p0 同机），与 [README_INT8.md:L11](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README_INT8.md#L11) 的「3P1D（4机A3）」一致。

## 6. 本讲小结

- ansible 配置按模型规格分成 `tools/ansible/92B/`（Flash）与 `tools/ansible/505B/`（Pro）两个平行目录；模板文件名编码了「拓扑 + 规格 + 精度 + 特性」四要素，inventory 与模板可自由组合。
- inventory 用 `P`/`D`/`C` 三个 children 组分配角色；`kv_rank` 给不同 P 实例编号（跨机），`node_rank` 给同一实例内的机器编号（实例内），`host_ip` 指向实例主节点，`ascend_rt_visible_devices`（16 卡）被模板现场换算成张量并行度。
- 端口是三级体系：proxy 7000 段（客户端入口）、node_port 8000 段（实例组网通信，P 用 `kv_rank×10` 划块、D 占 8100）、api_port 9000 段（proxy 转发目标，D 基数 9100）。
- 两种 inventory 组织方式：单机实例（每组 1 台，如 3P1D/4P81D16）与多机实例（每组多台共享 `kv_rank`，如 2P1D 的双机 P、四机 D）；多机实例靠 `host_ip` + 模板的 `ansible_host == host_ip` 过滤实现「一实例一端点」。
- BF16 与 w8a8 模板只差 4 处参数（批量上调、`GPU_UTIL` 提高、KV Cache 转 `li_int8_ds_mla`、proxy 并发同步上调），部署骨架完全一致；w8a8 指权重由 jointfix 量化产出，计算精度仍是 bfloat16。

## 7. 下一步学习建议

本讲你已经能读懂「机器怎么分组、端口怎么算」。下一讲 **u1-l4《实战：用 ansible 拉起第一个 1P1D BF16 服务》** 会把本讲的 inventory 与模板真正跑起来：配置 ssh 免密、`--tags run_docker` 建容器、`--tags run_server,run_proxy` 拉服务。届时你会看到本讲的端口计算在日志里逐一变成真实监听。

想提前延伸的读者可以：

- 通读 [tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml) 的 `environment:` 段，数一数有多少变量在 u1-l4 之前必须修改（答案在 [README.md:L78-L98](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L78-L98)）。
- 思考：3P1D 里有 3 个 `kv_rank`，D 节点没有 `kv_rank`，那 KV 传输域里 D 的编号从哪来？线索在 505B 模板 decode 侧的 `--kv-rank ${PREFILL_POD_NUM}`（[tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml:L339-L340](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_server_template_performance4P1D_505B_int8_open_omni_cache.yml#L339-L340)），完整答案在 u4 单元。
