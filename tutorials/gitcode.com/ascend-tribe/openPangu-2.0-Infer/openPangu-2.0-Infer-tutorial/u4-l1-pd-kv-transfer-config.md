# u4-l1 PD 分离与 KV 传输配置全景

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `role`（prefill/decode）与 `kv_role`（kv_producer/kv_consumer）是两个不同层面的角色参数，各自被谁消费。
2. 逐字段解释 `kv-transfer-config` JSON 的四个字段（`kv_connector`、`kv_role`、`kv_rank`、`kv_parallel_size`）的取值规则。
3. 追踪一条完整的配置传递链：ansible 模板变量 → `docker exec -e` 环境变量 → `pd_run.sh` 参数 → `KV_TRANSFER_CONFIG` JSON → `start_api_servers.py` → `vllm serve --kv-transfer-config`。
4. 对任意「NP1D」拓扑，独立推导每个 P 节点与 D 节点的 `kv_rank`、`KV_PARALLEL_SIZE` 以及随之推导出的端口。

本讲只讲**配置面**：这些参数是什么、从哪来、怎么算。KV 在网络里到底怎么传（LLMDataDistConnector 的源码）是下一讲 u4-l2 的内容。

## 2. 前置知识

### 2.1 KV Cache 为什么需要「传输」

Transformer 自回归解码时，每生成一个新 token 都要回看全部历史 token 的 Key/Value 投影，这些投影被缓存下来就是 KV Cache。在 PD 分离架构里（回顾 u1-l1）：

- **P（Prefill）节点**负责处理整段 prompt，计算密集，产出了 prompt 对应的全部 KV Cache；
- **D（Decode）节点**负责逐 token 生成，访存密集，但它需要 P 算好的那份 KV Cache 才能接着算。

于是「P 算完的 KV 如何搬到 D」就是一条独立的传输链路，vLLM 用 **KV Connector** 抽象这条链路，而这条链路的「组网身份」就由本讲的几个参数描述。

### 2.2 用「对讲机频道」理解 kv_rank 与 kv_parallel_size

把 KV 传输链路想象成一个会议室：

- `kv_parallel_size` = 会议室里总共几个成员（所有 P 实例 + 1 个 D 侧集群）；
- `kv_rank` = 每个成员在会议室里的座位号，必须互不相同且从 0 连续编号；
- `kv_role` = 这个成员是「讲者」（producer，发 KV）还是「听者」（consumer，收 KV）。

1P1D 里会议室有 2 个人：P 坐 0 号位讲，D 坐 1 号位听。3P1D 里则是 4 个人：三个 P 分别坐 0/1/2 号位讲，D 坐 3 号位听。

### 2.3 你需要的一点 bash / vLLM 前置

- **heredoc**：`cat <<EOF ... EOF` 可以把一段含变量展开的文本赋给 shell 变量，`pd_run.sh` 用它拼 JSON。
- **vLLM CLI 参数**：`vllm serve` 接受 `--kv-transfer-config '<JSON字符串>'`，vLLM 启动时把它反序列化成内部的 KV 传输配置对象，并按 `kv_connector` 字段的名字到注册表里查找对应的 Connector 类。
- 承接 u1-l4 的心智模型：环境变量沿「play `environment` → task `environment`/`docker exec -e` → 脚本内 `export`」三层传递，本讲的 `KV_CONNECTOR`、`KV_RANK` 等都走这条路。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml) | 1P1D BF16 服务模板，本讲主战场：`run_vllm_server_prefill_cmd` 与 `run_vllm_server_decode_cmd` 两段脚本生成 PD 两侧的全部 KV 参数 |
| [tools/scripts/pd_run.sh](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh) | PD 分离统一拉起脚本：解析 `--kv-*` 参数，拼装 `KV_TRANSFER_CONFIG` JSON，最终调 `start_api_servers.py` |
| [tools/scripts/start_api_servers.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/start_api_servers.py) | 多 API Server 启动器：把 JSON 字符串原样拼进每条 `vllm serve` 命令 |
| [tools/ansible/92B/omni_infer_inventory_used_for_1P1D.yml](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_inventory_used_for_1P1D.yml) | 1P1D 节点清单：`kv_rank` 在这里定义 |
| [tools/ansible/92B/omni_infer_inventory_used_for_3P1D.yml](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_inventory_used_for_3P1D.yml) | 3P1D 节点清单：三个 P 组 `P0/P1/P2`，本讲实践的拓扑 |
| [tools/ansible/505B/omni_infer_inventory_used_for_2P1D.yml](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_inventory_used_for_2P1D.yml) | 2P1D（505B）节点清单：多机 P 实例的写法，综合实践使用 |
| [components/omni-npu/src/omni_npu/connector/register.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/register.py) | 把 `LLMDataDistConnector` 这个名字注册进 vLLM 的 KVConnectorFactory，是 JSON 里 `kv_connector` 字符串的「落点」 |
| [README.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md) | 顶层部署文档，交代 inventory 与模板的关系 |

## 4. 核心概念与源码讲解

### 4.1 PD 角色：role 与 kv_role 的双层分工

#### 4.1.1 概念说明

部署脚本里有两个名字很像的「角色」参数，初学者最容易混淆：

| 参数 | 取值 | 消费者 | 描述的是什么 |
| --- | --- | --- | --- |
| `--role` | `prefill` / `decode` | llmdatadist 组网（底层传输库） | 本实例在 PD 集群里的**职能**：做预填充还是做解码 |
| `--kv-role` | `kv_producer` / `kv_consumer` | vLLM KV Connector | 本实例在 KV 传输中的**方向**：产出 KV 还是接收 KV |

在当前部署形态里两者总是成对出现（prefill↔kv_producer、decode↔kv_consumer），但它们是**两个独立参数**，分别进入不同的子系统。`pd_run.sh` 的帮助文本对二者各有一行说明：

- [pd_run.sh:L67](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L67)：`--role` 的帮助——"Instance role type. Use 'prefill' for P, 'decode' for D"；
- [pd_run.sh:L95](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L95)：`--kv-role` 的帮助——"kv role (p: kv_producer, d: kv_consumer)"。

#### 4.1.2 核心流程

P 侧脚本的角色装配：

```text
模板 vars: run_vllm_server_prefill_cmd
  ├── kv_role="kv_producer"                  # shell 变量赋值
  └── bash pd_run.sh --role "prefill" \
                     --kv-role ${kv_role}    # 两个角色参数分别传入
```

D 侧脚本的角色装配：

```text
模板 vars: run_vllm_server_decode_cmd
  └── bash pd_run.sh --role "decode" \
                     --kv-role "kv_consumer" # 直接硬编码字符串
```

两份脚本随后由 ansible 按 inventory 分组生成并投递：P 组机器拿到 `vllm_run_for_p.sh`，D 组机器拿到 `vllm_run_for_d.sh`（生成动作见模板 L797-814，回顾 u1-l4）。

#### 4.1.3 源码精读

**P 侧：先赋变量再引用。** 在 `run_vllm_server_prefill_cmd` 中，`kv_role` 是一个 shell 变量：

- [omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L102](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L102)：`kv_role="kv_producer"`——P 实例在 KV 链路中是生产者。
- [omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L131-L132](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L131-L132)：`--role "prefill"` 与 `--kv-role ${kv_role}` 两个参数并排传给 `pd_run.sh`，这就是「职能」与「方向」分层的地方。

**D 侧：直接硬编码。** 在 `run_vllm_server_decode_cmd` 中：

- [omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L228-L229](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L228-L229)：`--role "decode"` 与 `--kv-role "kv_consumer"`——D 实例是消费者，字符串直接写死，不经变量。

**pd_run.sh 的默认值。** 若调用方不传这两个参数，脚本有自己的兜底：

- [pd_run.sh:L11](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L11)：`ROLE="prefill"`；
- [pd_run.sh:L44](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L44)：`KV_ROLE="kv_producer"`。

默认值描述的正是「单机自测一个 P」的形态。生产模板里两侧都显式传参，默认值实际不生效——但手工调用 `pd_run.sh` 做实验时要意识到：**不传 `--kv-role` 时你拿到的默认是 producer**。

**参数解析。** `pd_run.sh` 用一个逐项 `case` 的解析器消费这些长选项：

- [pd_run.sh:L123-L124](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L123-L124)：`--role` 分支写入 `ROLE`；
- [pd_run.sh:L208-L209](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L208-L209)：`--kv-role` 分支写入 `KV_ROLE`。

解析完成后两者被 `export` 给子进程（[pd_run.sh:L289](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L289) 的 `export ROLE`），其中 `ROLE` 还会进一步影响 `start_api_servers.py` 的行为——见 [start_api_servers.py:L191](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/start_api_servers.py#L191)：`os.getenv('ROLE', 'prefill') == 'decode' and total_dp_size > 1` 时才追加 `--data-parallel-size/--data-parallel-rank`。也就是说 `role` 不只给 llmdatadist 用，还悄悄决定了 decode 侧多 DP server 的命令行形态。

#### 4.1.4 代码实践

**实践目标**：用 grep 在源码里把 `role` 与 `kv-role` 的全部出现点捞出来，确认「两侧成对、来源不同」这一结论。

**操作步骤**（在仓库根目录即可，无需 NPU）：

1. 执行：
   ```bash
   grep -n -E '\'--role|\'--kv-role|kv_role=' tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml
   ```
2. 再执行：
   ```bash
   grep -n -E '^ROLE=|^KV_ROLE=|--role\)|--kv-role\)' tools/scripts/pd_run.sh
   ```
3. （可选，需已部署环境）在部署机上查看 pd_run.sh 的配置回显：
   ```bash
   grep -n -A2 "ROLE:" /path/to/server/log/<P节点名>/run_prefill.log
   grep -n "KV_TRANSFER_CONFIG" /path/to/server/log/<D节点名>/run_decode.log
   ```

**需要观察的现象**：第 1 步应命中 4 处——P 侧 L102（赋值）、L131-L132（传参），D 侧 L228-L229（硬编码传参）；第 2 步应命中默认值与 case 分支。

**预期结果**：源码 grep 部分可立即验证；第 3 步日志中 P 机 `ROLE: prefill` 且 JSON 内 `"kv_role": "kv_producer"`，D 机 `ROLE: decode` 且 `"kv_consumer"`——日志部分**待本地验证**（需要一套已拉起的服务）。

#### 4.1.5 小练习与答案

**练习 1**：`--role` 和 `--kv-role` 分别描述什么？为什么说它们是两个层面？

**答案**：`--role`（prefill/decode）描述实例在 PD 集群中的职能，主要供 llmdatadist 组网与 `start_api_servers.py` 判断 DP 形态使用；`--kv-role`（kv_producer/kv_consumer）描述实例在 vLLM KV 传输中的方向。当前部署总是成对出现，但它们是独立参数、进不同子系统，理论上可独立变化。

**练习 2**：如果不传 `--kv-role` 直接手工运行 `pd_run.sh`，实例会以什么角色启动？这个默认值在生产部署里会生效吗？

**答案**：以 `kv_producer` 启动（`pd_run.sh` L44 默认值）。生产部署中 ansible 模板 P/D 两侧都显式传 `--kv-role`，默认值不生效；只有手工裸调脚本时才会落到默认值。

**练习 3**：若误把 D 侧的 `--kv-role` 写成 `kv_producer`，预计会发生什么？

**答案**：D 侧引擎会以生产者角色初始化 KV Connector，不再执行拉取 KV 的逻辑，proxy 转发来的 decode 请求在 D 侧缺少前置 KV，服务无法正常完成续写。具体报错形态**待本地验证**，但「方向配反则链路断」这一结论由角色语义可直接推出。

### 4.2 kv-transfer-config：四字段 JSON 的装配流水线

#### 4.2.1 概念说明

vLLM 用一个 JSON 字符串承载全部 KV 传输配置，即 `vllm serve --kv-transfer-config '<JSON>'`。在这套部署里它只含四个字段：

| 字段 | 类型 | 含义 | 本部署中的取值来源 |
| --- | --- | --- | --- |
| `kv_connector` | 字符串 | Connector 注册名 | 模板 `environment` 的 `KV_CONNECTOR`（默认 `LLMDataDistConnector`） |
| `kv_role` | 字符串 | 生产者/消费者 | P 侧 `kv_producer`，D 侧 `kv_consumer` |
| `kv_rank` | 数字 | KV 并行组内座位号 | P 侧取 inventory 的 `kv_rank`；D 侧取 `PREFILL_POD_NUM` |
| `kv_parallel_size` | 数字 | KV 并行组总规模 | 两侧统一按 `PREFILL_POD_NUM + 1` 计算 |

值得注意的一个细节：`pd_run.sh` 自己的默认 `KV_CONNECTOR` 是 `AscendHcclConnectorV1`（[pd_run.sh:L42](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L42)），而 ansible 模板始终用 `environment` 里的 `KV_CONNECTOR: "LLMDataDistConnector"` 显式覆盖（[omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L17](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L17)，README 在 [README.md:L86](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L86) 亦如此说明）。**以模板传参为准**，脚本默认值只是兜底。

#### 4.2.2 核心流程

一条 JSON 的完整旅程（以 P 侧为例）：

```text
ansible play environment:  KV_CONNECTOR="LLMDataDistConnector"
        │  (task environment / docker exec -e 注入容器)
        ▼
容器内 P 脚本:  bash pd_run.sh --kv-connector ${KV_CONNECTOR} \
                                 --kv-role kv_producer \
                                 --kv-rank ${KV_RANK} \
                                 --kv-engine-id ${KV_RANK} \
                                 --kv-parallel-size ${KV_PARALLEL_SIZE}
        │  (parse_long_option 逐项写入 shell 变量)
        ▼
pd_run.sh:  KV_TRANSFER_CONFIG=$(cat <<EOF ... EOF)   ← 用 heredoc 拼 JSON
        │  (common_operations 函数)
        ▼
start_api_servers.py:  --kv-transfer-config "$KV_TRANSFER_CONFIG"
        │  (逐个 API server 拼命令行)
        ▼
vllm serve --kv-transfer-config '{"kv_connector":...}'   ← vLLM 按 kv_connector
                                                          名字查注册表实例化
```

注册表的「名字→类」映射由 omni-npu 提前登记：`register_connectors()` 把 `"LLMDataDistConnector"` 这个字符串映射到 `omni_npu.connector.llmdatadist_connector_v1.LLMDataDistConnector` 类（[register.py:L37-L46](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/register.py#L37-L46)），注册是防御式的——名字已存在时跳过（[register.py:L20-L26](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/register.py#L20-L26)）。所以 JSON 里写的名字必须与注册名**逐字符一致**，拼错即找不到 Connector。

#### 4.2.3 源码精读

**JSON 在这里诞生。** `pd_run.sh` 参数解析完毕后，用 heredoc 拼出 `KV_TRANSFER_CONFIG`：

- [pd_run.sh:L273-L282](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L273-L282)：
  ```bash
  KV_TRANSFER_CONFIG=$(cat <<EOF
  {
      "kv_connector": "$KV_CONNECTOR",
      "kv_role": "$KV_ROLE",
      "kv_rank": $KV_RANK,
      "kv_parallel_size": $KV_PARALLEL_SIZE
  }
  EOF
  )
  ```
  注意引号的差异：`kv_connector`/`kv_role` 是字符串字段，变量带引号展开；`kv_rank`/`kv_parallel_size` 是数字字段，**不带引号**展开，保证落进 JSON 的是裸数字。这是一个易碎点——若把数字字段也加引号，JSON 里会变成字符串，vLLM 反序列化时的类型校验将不通过（基于 vLLM 配置对象为强类型字段这一通用行为）。

**JSON 在这里被转交。** `common_operations()` 是 P/D 共用的收尾函数，把 JSON 传给 Python 启动器：

- [pd_run.sh:L390-L413](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L390-L413)：函数体调用 `python start_api_servers.py ...`，其中 [pd_run.sh:L407](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L407) 一行 `--kv-transfer-config "$KV_TRANSFER_CONFIG"` 完成交接。同函数还顺带处理 MTP（`NUM_SPECULATIVE_TOKENS ≠ 0` 时追加 `--enable-mtp`，对应 u3-l5 讲过的投机解码开关）。

**JSON 在这里进入 vllm serve。** `start_api_servers.py` 为每个 API server 拼 `vllm serve` 命令：

- [start_api_servers.py:L153-L208](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/start_api_servers.py#L153-L208)：按 `rank in range(num_servers)` 循环，基础命令在 L174-L188 构建；
- [start_api_servers.py:L198-L199](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/start_api_servers.py#L198-L199)：`if kv_transfer_config: cmd.extend(["--kv-transfer-config", str(kv_transfer_config)])`——JSON 字符串**原样**追加，每个 API server 进程都拿到同一份；
- [start_api_servers.py:L276](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/start_api_servers.py#L276)：命令行入口 `--kv-transfer-config` 的 argparse 定义（默认空串，即「不做 KV 传输」的普通单机形态）。

**排障时的回显。** `pd_run.sh` 启动时会把全部配置打印一遍，`KV_TRANSFER_CONFIG` 也在其中：

- [pd_run.sh:L343-L385](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L343-L385)：`==== Current Configuration ====` 段落，其中 [pd_run.sh:L373](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L373) 专门回显 `KV_TRANSFER_CONFIG`。这份回显会随重定向写进 `run_prefill.log`/`run_decode.log`（模板 L148/L244 的 `> ... 2>&1`），是部署后核对配置的第一入口。

#### 4.2.4 代码实践

**实践目标**：不动 NPU、不部署服务，在本地把 `pd_run.sh` 的 JSON 装配段抽出来做成一个「配置生成器」，直观看到四个参数如何变成一份 JSON。

**操作步骤**：

1. 新建 `practice_kv_config.sh`（**示例代码**，摘自 pd_run.sh L273-L282 并参数化）：
   ```bash
   #!/bin/bash
   # 示例代码：模拟 pd_run.sh 的 KV_TRANSFER_CONFIG 装配
   KV_CONNECTOR="${1:-LLMDataDistConnector}"
   KV_ROLE="${2:-kv_producer}"
   KV_RANK="${3:-0}"
   KV_PARALLEL_SIZE="${4:-2}"
   KV_TRANSFER_CONFIG=$(cat <<EOF
   {
       "kv_connector": "$KV_CONNECTOR",
       "kv_role": "$KV_ROLE",
       "kv_rank": $KV_RANK,
       "kv_parallel_size": $KV_PARALLEL_SIZE
   }
   EOF
   )
   echo "$KV_TRANSFER_CONFIG"
   ```
2. 依次运行并观察输出：
   ```bash
   bash practice_kv_config.sh LLMDataDistConnector kv_producer 0 2
   bash practice_kv_config.sh LLMDataDistConnector kv_consumer 1 2
   ```
3. 用 Python 校验产物是合法 JSON 且字段类型正确：
   ```bash
   bash practice_kv_config.sh LLMDataDistConnector kv_consumer 1 2 | python3 -c "import json,sys; print(json.load(sys.stdin))"
   ```

**需要观察的现象**：`kv_rank`/`kv_parallel_size` 输出为裸数字（无引号），`kv_connector`/`kv_role` 带引号；`json.load` 能成功解析。

**预期结果**：第二条命令输出 `{'kv_connector': 'LLMDataDistConnector', 'kv_role': 'kv_consumer', 'kv_rank': 1, 'kv_parallel_size': 2}`，其中 rank/size 是 `int` 而非 `str`。此实践纯本地可完成；与真机日志回显的对照**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 heredoc 里 `kv_rank` 不能写成 `"$KV_RANK"`？

**答案**：`kv_rank` 与 `kv_parallel_size` 在 JSON 中是数字类型，不加引号才能展开成裸数字字面量；加了引号就变成字符串，vLLM 反序列化配置时类型校验不通过。

**练习 2**：`KV_CONNECTOR` 这个环境变量经历了哪几跳才变成 JSON 里的 `kv_connector` 字段？

**答案**：play `environment`（模板 L17）→ `docker exec -e KV_CONNECTOR=...` 注入 P/D 容器（模板 L387/L409）→ 容器内脚本以 `${KV_CONNECTOR}` 传 `--kv-connector`（模板 L141/L237）→ `pd_run.sh` 写入 shell 变量 `KV_CONNECTOR`（L202-L203）→ heredoc 展开（L276）→ JSON 字符串 → `start_api_servers.py` 拼进 `vllm serve`（L198-L199）→ vLLM 按名字到 KVConnectorFactory 注册表查类（`register.py` L37-L46 已注册 `LLMDataDistConnector`）。

**练习 3**：`--kv-transfer-config` 是逐 server 复制还是只给某一个 server？

**答案**：逐 server 复制。`start_api_servers.py` 在 `for rank in range(num_servers)` 循环内为每条 `vllm serve` 命令都追加同一份 JSON（L198-L199 在循环体内）；因此 1P1D 的 D 侧 16 个 DP server 每个进程启动时携带的 `kv-transfer-config` 完全相同（`kv_rank` 均为 `PREFILL_POD_NUM`），D 侧作为一个整体占据 KV 并行组的一个成员位。

### 4.3 并行规模计算：kv_rank、kv_parallel_size 与 PREFILL_POD_NUM

#### 4.3.1 概念说明

多 P 扩容（3P1D、4P1D…）时，一个请求的 prompt 只会被某一个 P 实例处理，但每个 P 实例都必须在 KV 并行组里有唯一身份，D 侧才知道去哪拉 KV。编号规则如下：

- **P 实例的 `kv_rank`**：在 inventory 中为每个 P 实例手工指定，从 0 开始连续编号。同一实例内的多台机器共享同一个 `kv_rank`（用 `node_rank` 区分），不同实例绝不重号。
- **D 侧的 `kv_rank`**：不做配置，由模板统一取 `PREFILL_POD_NUM`，即排在所有 P 之后，等于组内最后一个座位号。
- **`KV_PARALLEL_SIZE`**：P/D 两侧用同一个算式得出：
  \[ \text{KV\_PARALLEL\_SIZE} = \text{PREFILL\_POD\_NUM} + 1 \]
  直觉：所有 P 实例各占一席，D 侧集群整体占最后一席。由此 D 的 `kv_rank` 也满足 \( \text{kv\_rank}(D) = \text{KV\_PARALLEL\_SIZE} - 1 \)，与 `pd_run.sh` 帮助文本里 `kv_rank (p_num/d_num-1)` 的写法吻合（[pd_run.sh:L96](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L96)）。

**`PREFILL_POD_NUM` 的精确语义**：不是「P 组机器数」，而是 **P 组中不同 `host_ip` 的数量**——同一实例的多台机器 `host_ip` 都填实例主节点 IP，去重后正好等于实例数。

一个容易踩的命名陷阱：模板里的 `PREFILL_SERVER_LIST` **并不是 P 服务器 IP 列表**，而是当前 P 实例的 NPU 设备号列表（ansible 任务把它设为 `ascend_rt_visible_devices`，见 [omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L890](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L890)），容器内用于 `--ascend-rt-visible-devices "${PREFILL_SERVER_LIST}"`（模板 L137）。P 脚本 L88 还有一个去掉逗号的小写 `prefill_server_list` 变量，在当前模板中**并未被后续消费**，属历史遗留，读代码时不要被它误导（其历史用途待确认）。真正决定 KV 组规模的是 `PREFILL_POD_NUM`。

#### 4.3.2 核心流程

ansible 在 play 的 fact 阶段统一计算全局规模，再分发到各节点：

```text
"Register all values" (模板 L582-687, delegate_to: localhost)
  ├── PREFILL_POD_NUM  = groups['P'] 各 host 的 host_ip 去重计数
  ├── DECODE_POD_NUM   = groups['D'] 各 host 的 host_ip 去重计数
  └── （同时算出 PREFILL_API_SERVER_LIST 等给 proxy 用的清单）
        │
        ▼ 以 task environment 形式注入各节点任务
P 节点任务 (L884-903 单机实例分支):
  KV_RANK = inventory 的 kv_rank ──► pd_run.sh --kv-rank/--kv-engine-id ${KV_RANK}
  容器内脚本: KV_PARALLEL_SIZE=$((PREFILL_POD_NUM + 1))   # 模板 L89
        │
        ▼
D 节点任务 (L861-881):
  容器内脚本: KV_PARALLEL_SIZE=$((PREFILL_POD_NUM + 1))   # 模板 L189
  --kv-rank ${PREFILL_POD_NUM} --kv-engine-id ${PREFILL_POD_NUM}  # 模板 L234-L235
```

端口也由 `kv_rank` 推导（inventory 内联表达式）：P 实例 \(i\) 的 `node_port = 8000 + 10i`、`api_port = 9000 + 10i`，即每个 P 实例在 8000/9000 段里按 `kv_rank × 10` 划走一个 10 端口的小块；D 固定用 8100/9100。P 脚本还会在 `api_port + 100` 上开一个 ZMQ 端点广播 KV 缓存事件（[omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L90-L91](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L90-L91)），供 omni-proxy 做前缀缓存感知，细节留待 u4-l3/u6。

特例说明：omni_cache 变体模板（如 3P1D w8a8 omni_cache）在启用 OmniCache 时会把 `KV_CONNECTOR` 换成 `OmniCacheConnector` 并将 P 侧 `KV_PARALLEL_SIZE` 置 1（KV 改走主机内存池，不再用 llmdatadist 组网），规则不同，留待 u7 展开，本讲只讲标准 LLMDataDist 路径。

#### 4.3.3 源码精读

**PREFILL_POD_NUM 的计算。** ansible 在 localhost 上统计 P 组的不同 `host_ip` 数：

- [omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L616-L623](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L616-L623)：`groups['P'] | map('extract', hostvars) | map(attribute='host_ip') | unique | length`——四步管道「取 P 组 → 抽 hostvars → 取 host_ip 属性 → 去重计数」。计算结果经 debug 任务回显（L712-728），部署时可先在这里核对规模。

**两侧的 KV_PARALLEL_SIZE 算式。** P 与 D 的容器内脚本各自独立算出同一个值：

- [omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L89](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L89)：P 脚本 `KV_PARALLEL_SIZE=$((PREFILL_POD_NUM + 1))`；
- [omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L189](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L189)：D 脚本同样的算式。两侧必须一致，否则同一 KV 组对总规模的认知分裂，传输无法配对。

**P 侧 rank 的取值。** `KV_RANK` 由 ansible 从 inventory 抽出注入任务：

- [omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L898](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L898)：单机实例分支的 `KV_RANK: "{{ kv_rank }}"`（多机实例分支在 L845 同样注入）；
- 经 `docker exec -e KV_RANK=$KV_RANK`（[L383](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L383)）进入容器；
- 最终 [L138-L139](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L138-L139) 以 `--kv-rank ${KV_RANK}`、`--kv-engine-id ${KV_RANK}` 传出——engine id 与 rank 同值，一个实例一个传输引擎。

**D 侧 rank 的取值。** 不读 inventory，直接用规模算：

- [omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L234-L235](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L234-L235)：`--kv-rank ${PREFILL_POD_NUM}`、`--kv-engine-id ${PREFILL_POD_NUM}`——1P1D 中即 `kv_rank=1`，恰好是 `KV_PARALLEL_SIZE(2) - 1`。

**inventory 里 kv_rank 的定义与端口推导。**

- 1P1D：[omni_infer_inventory_used_for_1P1D.yml:L16-L25](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_inventory_used_for_1P1D.yml#L16-L25)——唯一 P 实例 `kv_rank: 0`，`node_port = 8000 + 0 + kv_rank`、`api_port = 9000 + 0 + kv_rank`。
- 3P1D：[omni_infer_inventory_used_for_3P1D.yml:L16-L48](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_inventory_used_for_3P1D.yml#L16-L48)——`P0/P1/P2` 三个子组，`kv_rank` 分别为 0/1/2，端口表达式换成 `kv_rank * 10`；D 组在 [L50-L60](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_inventory_used_for_3P1D.yml#L50-L60)，不设 `kv_rank`（D 的 rank 由模板按 `PREFILL_POD_NUM` 算出，无需配置）。
- 2P1D（多机实例）：[omni_infer_inventory_used_for_2P1D.yml:L18-L35](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_inventory_used_for_2P1D.yml#L18-L35)——`P0` 组内 `p0`（node_rank 0）与 `p1`（node_rank 1）两台机器 **`kv_rank` 同为 0**、`host_ip` 同为实例主节点；`P1` 组（L36-53）两台机器 `kv_rank` 同为 1。「一实例一 rank」由此得证。

**多机 P 实例对 rank 之外的连带影响。** 当同一实例跨机（`NODE_IP_LIST` 含逗号）时，P 脚本切换到 ray 后端并把 TP 乘上机器数：

- [omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L104-L117](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L104-L117)：统计 `NODE_IP_LIST` 逗号数判断跨机，跨机时 `PREFILL_TENSOR_PARALLEL_SIZE=$(( PREFILL_TENSOR_PARALLEL_SIZE * node_count ))`；单机走 `mp` 后端（承接 u1-l4 的结论：单机 P 实例 mp、多机 ray）。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：为 **3P1D** 拓扑手工推导并生成 4 份 `kv-transfer-config`（三个 P 节点 + 一个 D 节点），验证 \( \text{KV\_PARALLEL\_SIZE} = \text{PREFILL\_POD\_NUM} + 1 \) 的推导。

**操作步骤**：

1. **读 inventory 取输入**。打开 [omni_infer_inventory_used_for_3P1D.yml](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_inventory_used_for_3P1D.yml)，记录三个 P 实例的 `kv_rank`（0/1/2）与 `host_ip`（127.0.0.1/127.0.0.2/127.0.0.3，互不相同）。
2. **算规模**。`PREFILL_POD_NUM` = P 组不同 `host_ip` 数 = 3，故 `KV_PARALLEL_SIZE = 3 + 1 = 4`；D 侧 `kv_rank = PREFILL_POD_NUM = 3`。
3. **生成 JSON**。复用 4.2.4 的 `practice_kv_config.sh`（示例代码）：
   ```bash
   bash practice_kv_config.sh LLMDataDistConnector kv_producer 0 4   # P0
   bash practice_kv_config.sh LLMDataDistConnector kv_producer 1 4   # P1
   bash practice_kv_config.sh LLMDataDistConnector kv_producer 2 4   # P2
   bash practice_kv_config.sh LLMDataDistConnector kv_consumer 3 4   # D
   ```
4. **推导端口并交叉核对**。按 inventory 表达式 `node_port = 8000 + kv_rank×10`、`api_port = 9000 + kv_rank×10 + node_rank` 算出 P0/P1/P2 的 `node_port` 8000/8010/8020、`api_port` 9000/9010/9020，D 的 8100/9100；再算 P 侧 KV 事件端口（`api_port + 100`）为 9100/9110/9120。
5. （可选，需真机）按 3P1D 部署后核对日志：在三台 P 机与 D 机的 `run_prefill.log`/`run_decode.log` 中找 `==== Current Configuration ====` 段的 `KV_TRANSFER_CONFIG` 回显，与手写 JSON 逐字段 diff。

**需要观察的现象**：四份 JSON 仅 `kv_role` 与 `kv_rank` 两处不同，`kv_connector` 与 `kv_parallel_size` 完全一致；`kv_rank` 取值恰为 0、1、2、3 连续无重。

**预期结果**：P0/P1/P2/D 的 JSON 分别为：

```json
{"kv_connector": "LLMDataDistConnector", "kv_role": "kv_producer", "kv_rank": 0, "kv_parallel_size": 4}
{"kv_connector": "LLMDataDistConnector", "kv_role": "kv_producer", "kv_rank": 1, "kv_parallel_size": 4}
{"kv_connector": "LLMDataDistConnector", "kv_role": "kv_producer", "kv_rank": 2, "kv_parallel_size": 4}
{"kv_connector": "LLMDataDistConnector", "kv_role": "kv_consumer", "kv_rank": 3, "kv_parallel_size": 4}
```

（实际回显为多行缩进格式，字段相同。）第 1-4 步本地即可完成；第 5 步日志核对**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：4P1D 拓扑下各节点的 `kv_rank` 与 `KV_PARALLEL_SIZE` 是多少？

**答案**：四个 P 实例依次 `kv_rank = 0/1/2/3`（inventory 中 `P0..P3` 各自指定），`KV_PARALLEL_SIZE = 4 + 1 = 5`，D 侧 `kv_rank = PREFILL_POD_NUM = 4`，恰为组内最后一个座位号。

**练习 2**：2P1D（505B）里 `p0` 与 `p1` 两台机器的 `kv_rank` 为什么都是 0？

**答案**：它们属于同一个 P 实例——inventory 中两台机器的 `host_ip` 同为实例主节点 IP，`node_rank` 0/1 区分实例内机器。KV 并行组的成员是「实例」而不是「机器」，因此共享 `kv_rank=0`；`PREFILL_POD_NUM` 按 `host_ip` 去重计数也正是这个原因（此拓扑下 = 2，`KV_PARALLEL_SIZE = 3`）。

**练习 3**：若 3P1D 的三个 P 节点被误配成相同的 `kv_rank: 0`，会直接影响哪些资源？

**答案**：其一，端口——`node_port`/`api_port` 表达式都以 `kv_rank` 为变量，三台 P 机会算出相同的 8000/9000（跨机不冲突但失去区分度）；其二，KV 组身份——同一并行组内出现重复 rank，llmdatadist 组网成员身份冲突，KV 传输无法正确路由到各 P 实例；同时 D 侧 `kv_rank=3` 不变，但组内 0 号位被三方争用。配置规则「P 实例间 `kv_rank` 从 0 连续不重」必须遵守。

## 5. 综合实践

**任务：为 505B 2P1D 形态产出一份《KV 传输配置说明页》**，把本讲三个模块（角色、JSON 装配、规模计算）串起来。

背景：2P1D 是 openPangu-2.0-Pro 的典型形态（回顾 u1-l1/u1-l3），每个 P 实例跨 2 台机器，inventory 见 [omni_infer_inventory_used_for_2P1D.yml](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/505B/omni_infer_inventory_used_for_2P1D.yml)。

要求产出三样东西：

1. **一张节点配置表**：列出 `p0/p1/p2/p3/d0..d3/c0` 每台机器的 `role`、`kv_role`、`kv_rank`、`node_rank`、`host_ip`、`node_port`、`api_port`。关键点：`p0/p1` 同属 P0 实例（`kv_rank=0`），`p2/p3` 同属 P1 实例（`kv_rank=1`）；D 组 4 台机器同一 `host_ip`，是一个多机 D 实例。
2. **三份 kv-transfer-config JSON**（P0、P1、D）：`PREFILL_POD_NUM=2`，`KV_PARALLEL_SIZE=3`，P0/P1 的 `kv_rank` 为 0/1，D 为 2。用 4.2.4 的脚本生成后附在文档里。
3. **一段验证方案**：说明部署后如何核对——(a) 在各机 `run_prefill.log`/`run_decode.log` 的 `KV_TRANSFER_CONFIG` 回显中核对四字段；(b) 按端口推导核对监听（P 实例 `node_port` 8000/8010、`api_port` 9000/9010，D 侧 8100/9100 段）；(c) 观察多机 P 实例会走 ray 分支（模板 L104-L117），此时 `PREFILL_TENSOR_PARALLEL_SIZE` 会乘上机器数 2。真机验证部分**待本地验证**。

参考答案要点：`PREFILL_POD_NUM` 的统计对象是 `groups['P']` 的 `host_ip` 去重（模板 L616-L623 的管道），2P1D 中 P 组 4 台机器只有 2 个不同 `host_ip`，故得 2；三个成员的 rank 为 0、1、2，无重号、连续、D 在末位——满足本讲总结的全部三条规则。

## 6. 本讲小结

- `role`（prefill/decode，llmdatadist 组网职能）与 `kv_role`（kv_producer/kv_consumer，vLLM KV 传输方向）是两个独立参数，模板中 P 侧经变量传入、D 侧硬编码，`pd_run.sh` 对两者各有默认值（均为 prefill/kv_producer 方向）。
- `kv-transfer-config` 是一个四字段 JSON：`kv_connector`/`kv_role` 为字符串、`kv_rank`/`kv_parallel_size` 为数字，由 `pd_run.sh` 的 heredoc 装配（L273-L282），经 `start_api_servers.py` 原样拼进每条 `vllm serve` 命令（L198-L199），`kv_connector` 的名字必须与 omni-npu 注册表中的注册名逐字符一致。
- 规模三公式：`PREFILL_POD_NUM` = P 组不同 `host_ip` 数；`KV_PARALLEL_SIZE = PREFILL_POD_NUM + 1`（两侧同式独立计算）；P 实例 `kv_rank` 由 inventory 从 0 连续指定、D 侧固定取 `PREFILL_POD_NUM`（即末位 rank）。
- 「一实例一 rank」：多机 P 实例内所有机器共享 `kv_rank`，用 `node_rank` 区分机器；`node_port`/`api_port` 也随 `kv_rank×10` 划块推导。
- 阅读陷阱两处：`PREFILL_SERVER_LIST` 装的是设备号列表而非服务器列表（P 脚本 L88 的小写转换变量当前未被消费）；`pd_run.sh` 默认 `KV_CONNECTOR=AscendHcclConnectorV1` 与模板实际使用的 `LLMDataDistConnector` 不同，以模板传参为准。
- 部署后核对配置的第一入口是 `run_prefill.log`/`run_decode.log` 中 `==== Current Configuration ====` 段的 `KV_TRANSFER_CONFIG` 回显。

## 7. 下一步学习建议

本讲只解决了「配置面」：参数怎么算、JSON 怎么拼。接下来：

1. **u4-l2 LLMDataDistConnector 源码精读**——进入 `components/omni-npu/src/omni_npu/connector/`，看这四个字段在 Connector 内部如何变成真实的 KV 收发：Prefill/Decode 两侧的 Scheduler 与 Worker 四个类如何配合完成异步传输，`kv_engine_id` 如何对应 llmdatadist 的传输引擎。
2. **u4-l3 通信矩阵**——把本讲推导的端口（8000 段、9000 段、KV 事件端口）与 ZMQ 心跳、RoCE 传输端口一起对照 omni-npu README 的端点矩阵，形成完整的端口视图。
3. **u4-l4 pd_run.sh 全参数**——本讲只消费了 `--kv-*` 一组参数，`pd_run.sh` 还有 llmdatadist 组网、昇腾、多 API Server 三大类参数，以及 `--server-offset` 在多机 D 实例里的用法。
