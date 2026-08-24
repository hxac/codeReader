# pd_run.sh 与多机多 API Server 拉起细节

## 1. 本讲目标

学完本讲，你应该能够：

- 按「llmdatadist 专属 / 昇腾专属 / mock / 多 API Server / vLLM 框架 / PD 分离」六大类别，说出 `pd_run.sh` 每个参数的作用与去向。
- 讲清楚 `pd_run.sh` 内部的四段式结构：参数解析 → 组装 KV 传输 JSON → export 环境变量 → 调用 `start_api_servers.py`。
- 理解 rank table 文件在 llm_datadist 组网中的角色：它是 P/D 实例互相寻址的"花名册"，`pd_run.sh` 只负责把路径交给环境变量。
- 掌握「一个 API server 一个 DP」的设计：decode 侧 16 个单卡 server 如何靠 `VLLM_DP_RANK`、`server_offset` 与 `ASCEND_RT_VISIBLE_DEVICES` 排成一张全局 DP 编号表，而 prefill 侧是单个 TP16 引擎。
- 了解多机实例（如 2P1D 的跨机 P 实例）走 Ray 分支的行为差异，以及服务拉起后 `bind_cpu.sh` 的绑核收尾。

## 2. 前置知识

**bash 长选项解析。** `pd_run.sh` 没有使用 `getopts`（它只支持单字符选项），而是手写了一个 `case` 分派函数处理 `--xxx value` 形式的长选项。你只需要知道 bash 里 `$1`、`$2` 是脚本参数、`shift 2` 表示"丢掉当前两个参数、后面的参数顶上来"即可。

**heredoc。** `<<EOF ... EOF` 语法可以把多行文本原样赋给一个变量，`pd_run.sh` 用它拼装 `kv-transfer-config` 的 JSON 字符串。这在 u4-l1 已经出现过，本讲只回顾它在脚本里的位置。

**环境变量是跨进程的"传纸条"。** bash 里 `export` 过的变量会自动传给子进程。`pd_run.sh` 启动 `start_api_servers.py`（Python 子进程），Python 用 `os.getenv` 就能读到；Python 再用 `subprocess.Popen(env=...)` 启动 `vllm serve`，纸条继续往下传。整条部署链路（ansible → docker exec → pd_run.sh → start_api_servers.py → vllm serve）就是靠这张"纸条链"贯通的。

**DP 与 TP 的区别。** TP（张量并行）是把一个模型的算子切开放到多张卡上，卡间高频通信；DP（数据并行）是复制多个完整引擎、各自服务不同请求。本项目中：P 节点是 1 个 API server × TP16（16 卡合力算 prefill）；D 节点是 16 个 API server × 每个 TP1（每张卡独立 decode），这 16 个 server 在 vLLM 内部又组成一个规模为 16 的 DP 集群，共享调度视图。

**Ray。** 分布式计算框架，常被 vLLM 用作多机执行器后端（`--distributed-executor-backend ray`）。Ray 集群有 head（主）和 worker（从）两种角色，worker 通过 `ray start --address=<head_ip>:6379` 加入。

**进程绑核（CPU affinity）。** 用 `taskset -pc <cpu> <pid>` 把一个进程"钉"在某个 CPU 核上，避免它在核间漂移导致 cache 失效。NPU 推理的 host 侧进程（如调度、通信线程）对延迟敏感，绑核是常规性能手段。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `tools/scripts/pd_run.sh` | 部署链路的最后一棒：接收 ansible 模板拼好的全部参数，组装 KV 传输 JSON，export 组网环境变量，最终调用 `start_api_servers.py` |
| `tools/scripts/start_api_servers.py` | 按 `--num-servers` 拉起 N 个 `vllm serve` 进程，为每个进程分配 DP 编号、NPU 卡与 API 端口，写各自日志并监控存活 |
| `tools/scripts/bind_cpu.sh` | 服务拉起后按 `/tmp/process/proc_trace.txt` 中的进程信息，把 Worker 等进程绑定到空闲 CPU 核 |
| `tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml` | ansible 模板：`run_vllm_server_prefill_cmd` / `run_vllm_server_decode_cmd` 两个变量就是"调用 pd_run.sh 的完整体"，本讲反复与它对照 |
| `components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py` | omni-npu 侧唯一消费 `DECODE_POD_NUM` 环境变量的位置，用于计算 P 侧起始 rank |

## 4. 核心概念与源码讲解

### 4.1 bash 参数解析：pd_run.sh 的参数全景

#### 4.1.1 概念说明

ansible 模板里的 `run_vllm_server_prefill_cmd` 和 `run_vllm_server_decode_cmd`（见 u1-l4）最终会在容器里执行一条几百行的 `bash pd_run.sh --xxx ... --yyy ...` 命令。`pd_run.sh` 是这条命令的唯一接收端，它把约 40 个长参数分拣到六个类别，再分三路送出去：

1. **变成命令行参数**传给 `start_api_servers.py`（模型路径、TP、端口等）；
2. **变成环境变量**传给 llm_datadist / HCCL / vLLM（ranktable 路径、网卡名、超时等）；
3. **拼进 `kv-transfer-config` JSON**（u4-l1 已详讲的四字段）。

把参数分类记忆，是读懂这个 446 行脚本的钥匙。

#### 4.1.2 核心流程

```text
ansible 模板变量展开
        │
        ▼
docker exec ... bash vllm_run_for_p.sh / vllm_run_for_d.sh   （模板 copy 出来的脚本）
        │  内部再调用：
        ▼
bash pd_run.sh --role prefill --kv-role kv_producer ... 约 25 个参数
        │
        ├─① 默认参数赋值（L5-56，六大类注释分组）
        ├─② while 循环 + parse_long_option 逐对消费 --key value（L257-271）
        ├─③ heredoc 拼出 KV_TRANSFER_CONFIG JSON（L274-282）
        ├─④ export 约 30 个环境变量（L284-340）
        ├─⑤ echo 全量回显（L342-385，排障第一入口）
        └─⑥ common_operations：调 start_api_servers.py（L390-413）
              或：多机实例先走 Ray 组网分支（L415-446）
```

六类参数一览（按脚本默认值区的注释分组）：

| 类别 | 代表参数 | 去向 |
| --- | --- | --- |
| llmdatadist 专属 | `--role`、`--prefill-pod-num`、`--local-decode-server-ip-list`、`--vllm-llmdatadist-zmq-port`、ranktable 两个路径参数 | export 成环境变量，主要被 llm_datadist 运行库消费 |
| 昇腾专属 | `--hcc-intra-roce-enable`、`--ascend-rt-visible-devices`、`--hccl-buffsize`、`--hccl-op-expansion-mode` | export，被 HCCL / CANN 运行时消费 |
| mockModel 配置 | `--random-mode`、`--kv-cache-mod`、`--forward-time` | 默认注释掉，export 语句也被注释（L294-296），仅性能摸底用 |
| Multi-API Server | `--num-servers`、`--num-dp`、`--server-offset`、`--master-ip`、`--master-port`、`--base-api-port` | 传给 `start_api_servers.py` |
| vLLM 框架 | `--model-path`、`--tp`、`--max-model-len`、`--served-model-name`、`--gloo-socket-ifname`、`--extra-args` | 前者传给 python，网卡名 export，`--extra-args` 最终拼进 `vllm serve` |
| PD 分离 | `--kv-connector`、`--kv-role`、`--kv-rank`、`--kv-engine-id`、`--kv-parallel-size` | 拼进 `KV_TRANSFER_CONFIG` JSON |

#### 4.1.3 源码精读

**（1）默认值区与帮助文本。** 脚本开头 L5-56 集中定义全部默认值，注释就是天然的分类标签；[pd_run.sh:L5-L56](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L5-L56) 这段里有两个值得注意的默认值：`KV_CONNECTOR` 默认 `AscendHcclConnectorV1`（而本部署的 ansible 会在命令行显式覆盖为 `LLMDataDistConnector`，呼应 u4-l1 的结论）；`MODEL_PATH` 默认空字符串，说明它几乎必须由调用方传入。完整帮助文本在 [pd_run.sh:L59-L106](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L59-L106)，其中对 `--server-offset` 的说明（"For dual-node A3, set to 16 on d_2 instance"）是理解多机 D 实例编号的官方线索。

**（2）长选项解析：一个 case 搞定一切。** [pd_run.sh:L109-L253](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L109-L253) 的 `parse_long_option` 函数对每个选项名做一次赋值，例如 `--kv-rank)` 分支把 `$2` 写进 `KV_RANK`。主循环在 [pd_run.sh:L257-L271](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L257-L271)：凡是 `--` 开头的参数就调用解析函数并 `shift 2`。这带来一个隐含约束——**每个选项必须跟一个值**，`--enable-mtp` 这种开关式旗标无法直接传入，必须写成 `--xxx 1`；真正的开关都塞进了 `--extra-args`。注意 L141-144 的 `--ascend-rt-visible-devices` 分支：它同时把标志变量 `ascend_rt_set` 置 1，供后面条件 export 使用。

**（3）heredoc 拼 KV JSON。** [pd_run.sh:L273-L282](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L273-L282) 用 `cat <<EOF` 把六个 shell 变量装配成四字段 JSON：字符串字段 `kv_connector`/`kv_role` 带引号、数字字段 `kv_rank`/`kv_parallel_size` 不带引号。字段语义与规模推导规则在 u4-l1 已详细讲过，此处只强调它的位置——JSON 在这里生成后，一路作为字符串透传到 `vllm serve --kv-transfer-config`。

**（4）export 的三批环境变量。** 第一批是 llmdatadist 组网变量，[pd_run.sh:L285-L292](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L285-L292) 无条件 export 八个变量（两个 ranktable 路径、两个 IP 列表、ROLE、两个 POD_NUM、ZMQ 端口）。第二批是昇腾变量，[pd_run.sh:L298-L303](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L298-L303) 中 `ASCEND_RT_VISIBLE_DEVICES` 只有在命令行显式传过时才 export 并回显——1P1D 模板的 prefill 侧正是通过 `--ascend-rt-visible-devices "${PREFILL_SERVER_LIST}"` 传入 16 卡列表（[模板 L137](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L137)），而 decode 侧不传，改由 `start_api_servers.py` 按卡数自动分配（见 4.3）。第三批在 [pd_run.sh:L311-L340](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L311-L340)：注意 L312-313 **无条件覆盖**了 `VLLM_USE_V1=1` 与 `VLLM_WORKER_MULTIPROC_METHOD=fork`——即使你在命令行传 `--vllm-use-v1 0` 也会被这里改回 1，这是排障时容易踩的坑；L331-336 设定 HCCL 建链超时 1800 秒、执行超时 120 秒，并开启 `TNG_HOST_COPY`（随路拷贝）与 `AUTO_USE_UC_MEMORY`（双页表 PD 分离）两个 NPU 传输优化开关；L322-329 则是两个条件 export：`HCCL_OP_EXPANSION_MODE` 非空才导出、`HCCL_BUFFSIZE` 大于 0 才导出——这解释了为什么模板里 prefill 传 100、decode 传 1200 两种不同 buffsize。

**（5）配置回显。** [pd_run.sh:L342-L385](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L342-L385) 把所有变量连同拼好的 `KV_TRANSFER_CONFIG` 整体打印。ansible 模板把 pd_run.sh 的输出重定向到 `run_prefill.log` / `run_decode.log`，所以这段回显就是 u1-l5 所说"排障先看脚本层日志"时最先看到的内容。

**（6）交给 Python。** [pd_run.sh:L390-L413](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L390-L413) 的 `common_operations` 是唯一的"干正事"函数：L391-394 先判断 `NUM_SPECULATIVE_TOKENS` 非零则追加 `--enable-mtp` 旗标（MTP 投机解码，见 u3-l5），然后以 `python start_api_servers.py --num-servers ... --kv-transfer-config "$KV_TRANSFER_CONFIG" ...` 启动多 server 拉起器。

#### 4.1.4 代码实践

**实践目标**：不依赖任何真实环境，验证参数解析行为与配置回显。

**操作步骤**：

1. 进入仓库的 `tools/scripts/` 目录。
2. 执行 `bash pd_run.sh --help`，观察六类参数的帮助输出。
3. 执行一条"干跑"命令（不传 model-path 也无妨，脚本在调用 python 前不会校验）：

```bash
# 示例代码（仅演示参数解析，不真正拉起服务）
bash pd_run.sh --role decode --kv-rank 1 --kv-parallel-size 2 --hccl-buffsize 200 2>&1 | head -60
```

**需要观察的现象**：终端（或日志）中的 `==== Current Configuration ====` 段里，`ROLE: decode`、`KV_TRANSFER_CONFIG` JSON 中 `"kv_rank": 1`、`HCCL_BUFFSIZE: 200` 是否与你传入的一致；再对比不传 `--hccl-buffsize` 时该行是否消失（条件 export 的效果）。

**预期结果**：`--help` 正常打印；干跑命令打印完整配置块后继续往下执行 python（因缺少模型而报错属正常）。若手头没有 bash 环境，此步「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 ansible 模板可以给 prefill 与 decode 传完全不同的 `HCCL_BUFFSIZE`（100 vs 1200）而互不干扰？

**答案**：`pd_run.sh` 的全部状态都是脚本内 shell 变量，每次 `bash pd_run.sh ...` 都是一个全新进程、从默认值区重新开始；且 P、D 两类容器本就是不同容器、各自执行自己的 `vllm_run_for_p.sh` / `vllm_run_for_d.sh`，环境隔离。

**练习 2**：如果你在命令行传了 `--vllm-use-v1 0`，最终 vLLM 进程里 `VLLM_USE_V1` 是多少？为什么？

**答案**：仍是 1。参数解析确实把变量改成了 0，但 [pd_run.sh:L312](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L312) 在 export 段无条件写 `export VLLM_USE_V1=1`，覆盖了任何命令行取值。想真正关闭 V1 需要改脚本。

**练习 3**：`--enable-expert-parallel` 这类不带值的 vLLM 旗标为什么不能直接作为 `pd_run.sh` 的参数传入？

**答案**：主循环对 `--` 开头的参数一律 `shift 2`（[pd_run.sh:L262-L265](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L262-L265)），会把下一个参数名当成它的值吞掉；旗标统一塞进 `--extra-args "--enable-expert-parallel ..."` 字符串，由 `start_api_servers.py` 拆开后追加给 `vllm serve`。

### 4.2 ranktable 组网：llm_datadist 的寻址花名册

#### 4.2.1 概念说明

PD 分离要跨节点搬 KV Cache，llm_datadist（CANN 提供的传输库）在建链之前必须回答三个问题：网络上有哪些实例？每个实例内部有哪些进程、各自监听哪个端口？我是谁（在全局中的编号）？回答这些问题的数据结构就是 **rank table**——一份 JSON 格式的"花名册"，记录每个 server 的 IP、端口与全局 rank。`ROLE`、`PREFILL_POD_NUM`、两个 decode IP 列表则是配套的"自我介绍"。

有两份花名册：

- `RANK_TABLE_FILE_PATH`：本实例（本 P 或本 D）的局部花名册；
- `GLOBAL_RANK_TABLE_FILE_PATH`：把 P/D 所有实例合并后的全局花名册（默认值文件名 `global_ranktable_merge.json` 中的 merge 即此意）。

#### 4.2.2 核心流程

```text
pd_run.sh --global-rank-table-path X --rank-table-path Y
        │  export GLOBAL_RANK_TABLE_FILE_PATH / RANK_TABLE_FILE_PATH
        ▼
vllm serve → omni-npu LLMDataDistConnector → llmdatadist_manager_v1
        │  初始化 llm_datadist StateManager
        ▼
llm_datadist 运行库读取两份 ranktable + ROLE + POD_NUM
        │  据此计算每个远端实例的 cluster_id（u4-l2/u4-l3 已讲编码规则）
        ▼
decode 侧按花名册向 prefill 侧发起 register_link 建链
```

配套变量各司其职：`ROLE=prefill/decode` 标识实例职能；`PREFILL_POD_NUM`/`DECODE_POD_NUM` 声明两类实例的数量；`LOCAL_DECODE_SERVER_IP_LIST` 是**本 D 实例**涉及的 IP（逗号分隔、顺序须与 ranktable 一致），`GLOBAL_DECODE_SERVER_IP_LIST` 是**所有 D 实例** IP 列表的拼接（分号分隔），多 D 实例部署时二者才会不同。

#### 4.2.3 源码精读

**（1）参数与帮助。** [pd_run.sh:L63-L66](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L63-L66) 的帮助文本写明了两个 ranktable 参数的用途约定：局部表"通常是 `local_ranktable_{IP}_rank.json`，跨机 D 实例用 `local_ranktable_merge*.json`"。而 L7-L8 的默认值是相对路径 `1p1d_save_dir/global_ranktable_merge.json` 与 `save_dir/local_ranktable.json`——**检索整个仓库后可以确认：所有 ansible 模板都没有显式传这两个参数**（包括 505B 目录下的模板），即生产部署全部使用默认路径，ranktable 文件需要在这些相对路径（相对容器内 `pd_run.sh` 的工作目录 `/workspace/omniinfer/tools/scripts`）下存在。仓库内没有生成这两个文件的脚本，它们由 llm_datadist 侧机制生成或需预先准备——**具体生成方式待本地验证**。

**（2）export 与消费方。** [pd_run.sh:L285-L292](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L285-L292) 把八个组网变量导出。用 Grep 在 `components/omni-npu` 里检索会发现：omni-npu 的 Python 代码**几乎不直接消费**这些变量（ranktable 路径、IP 列表、ROLE 的直接消费者是容器内的 llm_datadist 运行库，该库不在本仓库），唯一的例外在 [llmdatadist_manager_v1.py:L499-L504](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py#L499-L504)：`_get_cluster_id_list` 读取 `DECODE_POD_NUM` 环境变量，代入 `get_p_start_rank(...)` 计算 D 侧应连接的 P 侧起始 rank——这正是 D 实例数量影响寻址的代码落点。

**（3）模板侧的传参。** 1P1D 模板的 prefill 与 decode 调用都传了 `--local-decode-server-ip-list "$SERVER_IP_LIST" --global-decode-server-ip-list "$SERVER_IP_LIST"`（[模板 L123-L124](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L123-L124)），1 个 D 实例时两个列表相同，印证了帮助文本"For 1d scenarios, same as LOCAL_DECODE_SERVER_IP_LIST"的说法。`SERVER_IP_LIST` 由 ansible 在部署时探测注入。

**（4）跨脚本的 ROLE 二次消费。** `ROLE` 除了给 llm_datadist，还被 `start_api_servers.py` 读取（[start_api_servers.py:L191](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/start_api_servers.py#L191) 的 `os.getenv('ROLE', 'prefill') == 'decode'`），用来决定 decode 侧是否追加 DP 参数——这是"一张纸条两个收件人"的典型例子。

#### 4.2.4 代码实践

**实践目标**：在真实部署中找到 ranktable 文件并核对 pd_run.sh 回显。

**操作步骤**：

1. 按 u1-l4 拉起服务（或使用已有环境）。
2. 在 D 容器内执行：

```bash
docker exec -it <容器名> bash
cd /workspace/omniinfer/tools/scripts
grep -E "RANK_TABLE|SERVER_IP_LIST|ROLE" <LOG_PATH>/<D机IP>/run_decode.log | head
ls -l save_dir/ 1p1d_save_dir/ 2>/dev/null
```

**需要观察的现象**：`run_decode.log` 的配置回显区打印出的两个 ranktable 路径与默认值一致；`save_dir/`（或 `1p1d_save_dir/`）下是否存在 `local_ranktable*.json` 等文件、内容里是否能看到本机 IP 与端口。

**预期结果**：能找到 ranktable 文件并看到 IP/端口记录；若目录不存在，说明该环境的 ranktable 由 llm_datadist 在别的路径生成或机制不同——记下实际路径，这正是「待本地验证」的部分。

#### 4.2.5 小练习与答案

**练习 1**：`GLOBAL_DECODE_SERVER_IP_LIST` 用分号分隔、`LOCAL_DECODE_SERVER_IP_LIST` 用逗号分隔，为什么格式不同？

**答案**：局部列表只需平铺一台/一组机器的 IP（逗号即可）；全局列表是"多个局部列表的拼接"，每个 D 实例贡献一个逗号分隔的子列表，子列表之间再用分号隔开，两级分隔符避免歧义（[pd_run.sh:L66](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L66) 的帮助文本明确写了这一约定）。

**练习 2**：把 `--prefill-pod-num` 从 1 改成 2 但不改动其它参数，KV 传输会发生什么？

**答案**：`PREFILL_POD_NUM` 被 export 后参与 llm_datadist 组网与寻址（配合 u4-l1 讲过的 `KV_PARALLEL_SIZE = PREFILL_POD_NUM + 1`）。若实际只有一个 P 实例而声明两个，组网视图与真实拓扑不一致，D 侧可能向不存在的实例寻址导致建链失败或超时；正确做法是同时调整 inventory 与 KV 参数（u1-l3 的多 P 拓扑）。

### 4.3 多 API server：start_api_servers.py 与 server_offset

#### 4.3.1 概念说明

P 和 D 对"进程数"的需求完全不对称：P 要 16 卡合力算一个长 prompt（1 个 server，TP16）；D 要 16 张卡各自独立 decode（16 个 server，每个 TP1）。`start_api_servers.py` 就是"按份数复制 vllm serve"的复印机，它的核心设计是 **dp_per_server = 1——一个 API server 就是一个 DP rank**。

在单机 16 卡场景这很自然：第 `rank` 个 server 用第 `rank` 张卡、监听第 `base_api_port + rank` 个端口、DP 编号就是 `rank`。但当 **D 实例跨机**（一台机器装不下、两台各 16 卡组成一个 32 卡 D 实例）时，两台机器各自跑 `start_api_servers.py`，各自的 `rank` 都从 0 数起，全局 DP 编号就会撞车。`server_offset` 就是给"第二台机器"加上偏移量的：本机 server 的全局 DP 编号 = 本机内 rank + 偏移。

#### 4.3.2 核心流程

```text
start_api_servers.py --num-servers N --num-dp D --server-offset O --tp T ...
        │
        ├─ 校验：num_dp 缺省取 num_servers；num_dp ≥ num_servers 否则报错
        │
        └─ for rank in 0..N-1：
             ├─ 环境变量：VLLM_DP_SIZE=D（全局总数）
             │           VLLM_DP_RANK=VLLM_DP_RANK_LOCAL= rank + O/tp
             │           VLLM_DP_MASTER_IP/PORT（DP 集群的会合点）
             │           ASCEND_RT_VISIBLE_DEVICES = [rank*tp*pp, (rank+1)*tp*pp)
             ├─ 从 base-api-port 起找一个空闲端口
             ├─ 拼装 vllm serve 命令（基础参数 + 条件参数）
             │     ├─ ROLE=decode 且 D>1 → 追加 --data-parallel-size D --data-parallel-rank
             │     ├─ --enable-mtp       → 追加 --speculative_config JSON
             │     ├─ kv_transfer_config → 追加 --kv-transfer-config
             │     └─ extra_args 拆分后追加
             ├─ subprocess.Popen 启动，stdout/stderr → log_dir/server_{rank}.log
             └─ 注册清理回调（进程退出时 terminate 全部 server）
```

全局 DP 编号的计算式为：

\[ \text{VLLM\_DP\_RANK} = \text{rank} + \left\lfloor \frac{\text{server\_offset}}{\text{tp}} \right\rfloor \]

除以 `tp` 是因为 offset 按"卡数"累计，而每个 server 占 `tp` 张卡；decode 侧 `tp=1` 时退化为直接相加。

#### 4.3.3 源码精读

**（1）入口与校验。** [start_api_servers.py:L252-L301](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/start_api_servers.py#L252-L301) 是 argparse 入口：`--num-servers` 默认 2、`--num-dp` 默认 None；L296-297 规定 `num_dp` 未给时等于 `num_servers`，L298-301 规定 `num_dp < num_servers` 直接抛 `ValueError`（每个 server 至少占一个 DP）。

**（2）"一 server 一 DP"的硬编码。** [start_api_servers.py:L113-L114](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/start_api_servers.py#L113-L114) 写死 `dp_per_server = 1`，注释直言"current we want one api server one DP"。这决定了 DP 规模的全部推导方式。

**（3）每个 server 的环境变量。** [start_api_servers.py:L153-L161](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/start_api_servers.py#L153-L161) 在循环内 copy 父环境并追加五个 `VLLM_DP_*` 变量与 `ASCEND_RT_VISIBLE_DEVICES`。注意 L161 的卡分配公式 `range(rank*tp*pp, (rank+1)*tp*pp)`：tp=1 时第 rank 个 server 拿第 rank 张卡——这就是 decode 侧不在 pd_run.sh 传 `--ascend-rt-visible-devices` 的原因，分配权下沉到了这里。

**（4）端口探测。** [start_api_servers.py:L44-L55](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/start_api_servers.py#L44-L55) 的 `find_available_port` 从 `base_port` 起依次尝试 bind，最多试 `max_attempts` 次；L163-L172 在循环中用它给每个 server 找端口，并把 `api_port_start` 更新为"已用端口 + 1"，保证 N 个 server 端口互不冲突——这解释了 u1-l3 讲过的"D 机 9001 起连续 16 个 api_port"。

**（5）vllm serve 命令拼装。** 基础命令在 [start_api_servers.py:L174-L188](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/start_api_servers.py#L174-L188)：固定携带 `--trust-remote-code`、`--block-size 128`、`--tensor-parallel-size`、`--data-parallel-address/-rpc-port`（DP 集群会合点，即 master-ip/master-port）、`--port`、`--served-model-name`、`--max-model-len` 等。条件追加部分最关键的是 [start_api_servers.py:L189-L195](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/start_api_servers.py#L189-L195)：**只有 `ROLE=decode` 且 `total_dp_size > 1` 时**才追加 `--data-parallel-size` 与 `--data-parallel-rank`——即 decode 侧 16 个 server 组成一个 vLLM DP 集群、共享外部 LB 视图；prefill 侧（ROLE=prefill 或单 DP）不加，每个 server 是独立引擎。这正是 u1-l4 说"P 为 TP16 单引擎、D 为 16 个 DP server"的代码依据。随后的 L196-L208 依次追加 MTP 配置、kv-transfer-config、extra_args、additional-config。

**（6）extra-args 的字符串拆分。** `pd_run.sh` 把一堆 vLLM 参数塞进一个带引号的字符串，[start_api_servers.py:L63-L83](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/start_api_servers.py#L63-L83) 的 `process_extra_args` 负责拆开：按 `--` 切分、还原每个旗标；含空格的参数里，`--compilation-config` 与 `--reasoning-config` 被特殊处理为"整段作为一个值"（L63-L70），这就是模板里 `--compilation-config {"level": 3, ...}` 这类带空格 JSON 能安全穿过两层脚本的原因。其它带空格的值按普通空格切分——所以 extra-args 里不要放这两个之外的带空格参数。

**（7）日志与进程管理。** [start_api_servers.py:L210-L233](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/start_api_servers.py#L210-L233)：每个 server 的 stdout/stderr 合并写入 `log_dir/server_{rank}.log`（u1-l5 讲的"引擎层日志 server_N.log"的命名源头），`Popen` 后台启动；L215-L217 还会把完整命令行打印出来（日志里 `Server N on port P>>>vllm serve ...` 一行，是核对最终命令的最佳位置）。主循环 [start_api_servers.py:L333-L346](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/start_api_servers.py#L333-L346) 每秒轮询，任一 server 退出即打印其退出码并整体退出，配合 `weakref.finalize` 注册的清理回调（L226-L227） terminate 其余进程。

**（8）server_offset 的上游：ansible 累加。** 模板 decode 侧用 `--server-offset ${config_dict[$HOST]:-0}`（[模板 L224](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L224)）按主机名取偏移；`config_dict` 由 Jinja2 变量 `server_offset_dict` 渲染（[模板 L191-L195](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L191-L195)），其值 `DECODE_SERVER_OFFSET_BY_GROUP` 在 [模板 L667-L675](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L667-L675) 构造：按 D 组主机顺序累加每台机器的卡数（`ascend_rt_visible_devices` 逗号数 + 1）——第一台 D 机 offset=0，第二台 offset=第一台卡数（如 16）。单机 1P1D 时只有一台 D 机，offset 恒为 0。decode 侧的 `NUM_SERVERS` 与 `dp` 也来自同一份卡数信息（[模板 L185-L186](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L185-L186)、[L874](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L874)）。

#### 4.3.4 代码实践

**实践目标**：从部署日志反推 server_offset 与卡分配。

**操作步骤**：

1. 在 D 机上打开引擎日志目录，执行：

```bash
grep -h "on port" <LOG_PATH>/<D机IP>/server_*.log 2>/dev/null | head -3
# 若 server_N.log 尚无该行，改看 run_decode.log：
grep -E "on port|ASCEND_RT_VISIBLE_DEVICES" <LOG_PATH>/<D机IP>/run_decode.log | head -10
```

2. 找到 `Starting API server 0 on port XXXX` 与 `Server 0 on port XXXX>>>vllm serve ...` 行。
3. 在该 `vllm serve` 命令行里确认：`--tensor-parallel-size 1`、`--data-parallel-size 16`、`--data-parallel-rank 0`、`--port 9001`。

**需要观察的现象**：server_0 与 server_15 的日志中 `--data-parallel-rank` 分别是 0 和 15、`--port` 分别是 9001 与 9016（若 base-api-port 为 9001）、`--kv-transfer-config` 完全相同。

**预期结果**：16 个 server 的命令行仅在 `--data-parallel-rank`、`--port` 和环境变量 `ASCEND_RT_VISIBLE_DEVICES`（单卡编号递增）三处不同，其余参数逐字一致；1P1D 单机场景所有 rank 均无偏移。无环境时此步「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：双机 A3 组成一个 D 实例（每机 16 卡、tp=1），第二台机器应以什么 `--server-offset` 启动？它上面 server_3 的全局 DP rank 是多少？

**答案**：offset=16（帮助文本 [pd_run.sh:L79](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L79) 与 ansible 累加逻辑共同印证）。全局 DP rank = 3 + 16/1 = 19。

**练习 2**：为什么 prefill 侧的 `vllm serve` 命令里看不到 `--data-parallel-size`？

**答案**：[start_api_servers.py:L191](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/start_api_servers.py#L191) 的条件是 `ROLE == 'decode' and total_dp_size > 1`；prefill 侧 ROLE=prefill（模板 [L131](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L131) 传 `--role "prefill"`），并且 NUM_SERVERS 用 pd_run.sh 默认值 1（模板未传），所以它是单个 TP16 独立引擎、不参与 DP 组网。

**练习 3**：`--num-servers 8 --num-dp 4` 会发生什么？

**答案**：直接抛 `ValueError`（[start_api_servers.py:L298-L301](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/start_api_servers.py#L298-L301)），因为一 server 一 DP 的设计下 DP 总数不能小于 server 数。

### 4.4 多机 Ray 分支与 bind_cpu 绑核收尾

#### 4.4.1 概念说明

单机实例里 `pd_run.sh` 跑到 `common_operations` 就结束了；但多机实例（如 2P1D 的跨机 P 实例，见 u1-l3）需要先把多台机器"缝"成一个 vLLM 集群，脚本在最后一段（L415-446）为此准备了一条 Ray 分支。判定信号不是命令行参数，而是 docker exec 注入的环境变量 `NODE_IP_LIST`——它列出实例的全部节点 IP，含逗号即多机。

服务拉起后，ansible 还会对 P/D 容器各执行一次 `bind_cpu.sh`：读取运行期生成的进程清单 `/tmp/process/proc_trace.txt`，把每个 Worker 进程钉到规划好的 CPU 核上，减少跨核迁移带来的尾延迟。这是部署的最后一道工序。

#### 4.4.2 核心流程

```text
NODE_IP_LIST 含逗号？
 ├─ 是，且 IP == HOST_IP（本机是实例主节点）
 │     ray start --head --num-gpus=$NUM_SERVERS → sleep 10 → common_operations
 │     （server 只在 head 节点拉起，经 Ray 调度到各机）
 ├─ 是，且 IP != HOST_IP（本机是从节点）
 │     循环尝试 ray start --address=$HOST_IP:6379（每 5 秒一次，最多 300 秒）
 │     成功即退出——本机不执行 common_operations
 └─ 否（单机）
       直接 common_operations
```

`bind_cpu.sh` 的绑核规划：把 CPU 按 20 核一组划分（320 核机型分 16 组、180 核机型分 9 组），先剔除每组首核（0、20、40…，为 `CPU_AFFINITY_MODE=2` 预留），再扫描系统中已绑核的进程占用的核；`local_rank = N` 的 Worker 进程优先分到第 N 组内剩余的空闲核，其它进程（非 D 角色的 Tokenizer 等）从全局空闲核里顺序分配。

#### 4.4.3 源码精读

**（1）Ray 分支判定与 head 行为。** [pd_run.sh:L415-L446](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L415-L446)：L415 用 `tr -cd ',' | wc -c` 数逗号个数判断多机；head 节点（`IP == HOST_IP`）先 `export RAY_USAGE_STATS_ENABLED=0`、`ray start --head --num-gpus=$NUM_SERVERS`、`sleep 10` 等集群成形，再执行 `common_operations`。注意 `NODE_IP_LIST`、`IP`、`HOST_IP` 三个变量并非 pd_run.sh 的命令行参数，而是 ansible 通过 `docker exec -e` 注入的（[模板 L374-L388](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L374-L388)），这是"命令行参数之外的第二条传参通道"。与之配套，模板 prefill 脚本在多机时会把执行器切到 Ray 并放大 TP（[模板 L104-L117](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L104-L117)）：追加 `--distributed-executor-backend ray` 到 EXTRA_ARGS，并令 `PREFILL_TENSOR_PARALLEL_SIZE *= node_count`（两机各 16 卡 → TP32），同时导出 `RAY_EXPERIMENTAL_NOSET_ASCEND_RT_VISIBLE_DEVICES=1`、`RAY_CGRAPH_get_timeout=7200` 两个 Ray+NPU 协作所需变量；单机则走 `mp` 后端（u1-l4 讲过"单机 P 排在 D 后启动"）。

**（2）从节点的重试连接。** [pd_run.sh:L421-L443](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L421-L443)：从节点以 `ray start --address='$HOST_IP:6379'` 加入集群，失败则每 5 秒重试、累计 300 秒后报错退出。从节点连上后脚本结束、不再启动任何 server——vLLM 的 Ray 后端会自动在 worker 节点上拉起 worker 进程。

**（3）bind_cpu 的输入与角色校验。** [bind_cpu.sh:L5-L9](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/bind_cpu.sh#L5-L9)：输入是运行期进程清单 `proc_trace.txt`（由 profiler 的 proc_bind 机制写入，每行含 `pid=... tag=... local_rank=...` 字段），`ROLE` 环境变量只接受 `P` 或 `D`、缺省为 P——注意这里的取值集合与 pd_run.sh 的 `prefill/decode` 不同，是绑核脚本自己的约定。ansible 在 run_server 之后对 P、D 容器分别执行它（[模板 L934-L978](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L934-L978)）。

**（4）20 核一组与核位腾挪。** [bind_cpu.sh:L13-L30](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/bind_cpu.sh#L13-L30) 先按 `lscpu` 推断总核数（≥319 按 320、≥179 按 180），再把 0、20、40… 等每组首核从空闲池剔除。Worker 绑核主循环在 [bind_cpu.sh:L102-L120](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/bind_cpu.sh#L102-L120)：`local_rank=N` 的 Worker 被分到第 N 组（核区间 \[20N, 20N+19\]）内从高到低找到的第一个空闲核，`taskset -pc` 生效；其余进程的兜底循环在 [bind_cpu.sh:L122-L135](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/bind_cpu.sh#L122-L135)，其中 L124 对 `ROLE=D` 跳过 Tokenizer 进程（decode 侧分词进程保持自由调度）。

#### 4.4.4 代码实践

**实践目标**：验证 bind_cpu.sh 的核分组逻辑（纯逻辑推演，无需 NPU）。

**操作步骤**：

1. 阅读 [bind_cpu.sh:L102-L120](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/bind_cpu.sh#L102-L120)，手工模拟一台 320 核机器：`local_rank=0` 与 `local_rank=1` 的 Worker 各会被分配到哪个核区间？
2. 在有 bash 的机器上构造一个最小 `proc_trace.txt`：

```bash
# 示例代码（手工模拟，不依赖 NPU）
mkdir -p /tmp/process
cat > /tmp/process/proc_trace.txt <<'EOF'
pid=12345 tag=Worker local_rank=0
pid=12346 tag=Worker local_rank=1
EOF
ROLE=P bash bind_cpu.sh && cat /tmp/process/bind_cpu.log
```

（模拟进程不存在时脚本会 `[SKIP]`，属预期。）

**需要观察的现象**：`bind_cpu.log` 中两行 `Worker pid=... local_rank=N -> cpu X`，X 分别落在 \[0,19\] 与 \[20,39\] 区间内且不是 0 或 20（被剔除的组首核）。

**预期结果**：local_rank 与核区间一一对应，验证"每 Worker 一个 20 核领地"的设计。若在无 bash 环境推演，以 L107 的 `start=lr*20` 公式纸上验证即可。

#### 4.4.5 小练习与答案

**练习 1**：Ray 分支里从节点为什么"连上就退出"、不执行 `common_operations`？

**答案**：多机实例的 API server 与引擎协调进程只在 head 节点启动一次；从节点只需加入 Ray 集群（[pd_run.sh:L421-L443](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L421-L443)），vLLM 的 Ray 执行器后端随后自动在从节点创建 worker 进程。若从节点也跑 `common_operations`，会启动重复的 server。

**练习 2**：多机 P 实例中，`NODE_IP_LIST` 为 `ip1,ip2` 时 `PREFILL_TENSOR_PARALLEL_SIZE` 如何变化？为什么？

**答案**：`node_count = 逗号数 + 1 = 2`，TP 变为原值 ×2（如 16×2=32，[模板 L113-L114](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L113-L114)）。因为两个节点共 32 张卡要织进同一个 TP 组，且执行器切到 Ray 后由 Ray 负责把 32 个 worker 铺到两台机器。

**练习 3**：`bind_cpu.sh` 为什么要先把 0、20、40… 这些核从空闲池剔除？

**答案**：注释写明"suit for CPU_AFFINITY_MODE=2"（[bind_cpu.sh:L24-L25](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/bind_cpu.sh#L24-L25)）：每组首核预留给其它绑核模式/系统用途，避免 Worker 与之争抢；这也与 pd_run.sh 中被注释掉的 `CPU_AFFINITY_CONF=2`（[pd_run.sh:L318-L320](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L318-L320)）相呼应。

## 5. 综合实践

**任务**：不看 ansible 模板，为一个 1P1D 拓扑的 **decode 侧**手写一条最小可用的 `pd_run.sh` 调用命令，再与模板实际生成的命令做 diff，找出你遗漏的参数并归类。

**步骤**：

1. **先写自己的版本**。对照本讲 4.1 的参数表，按以下清单补全命令（占位符 `<>` 自行填值）：

```bash
# 示例代码（最小 decode 侧调用，ranktable 走默认路径）
cd /workspace/omniinfer/tools/scripts
bash pd_run.sh \
  --role decode \
  --kv-role kv_consumer \
  --kv-connector LLMDataDistConnector \
  --kv-rank 1 --kv-engine-id 1 --kv-parallel-size 2 \
  --prefill-pod-num 1 \
  --local-decode-server-ip-list <D机IP> \
  --global-decode-server-ip-list <D机IP> \
  --num-servers 16 --num-dp 16 --server-offset 0 \
  --tp 1 \
  --master-ip <D机IP> --master-port 8100 --base-api-port 9001 \
  --model-path <MODEL_PATH> \
  --served-model-name openPangu-2.0-Flash \
  --max-model-len <MODEL_LEN_MAX_DECODE> \
  --log-dir <LOG_PATH>/<D机IP>
```

   推导依据自检：`--kv-rank` 为何是 1（u4-l1：D 侧固定取 `PREFILL_POD_NUM`）？`--num-servers` 为何是 16（4.3：卡数）？`--tp` 为何是 1？

2. **再取模板的真版本**。在部署机执行 `cat $SCRIPTS_PATH/vllm_run_for_d.sh`（或读仓库模板 `run_vllm_server_decode_cmd` 段，[模板 L150-L244](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L150-L244)），把其中 `bash pd_run.sh \` 之后的部分抄成第二个文件。

3. **diff 对比**：

```bash
diff <(grep -oE '^ *--[a-z-]+' my_cmd.txt | tr -d ' ' | sort) \
     <(grep -oE '^ *--[a-z-]+' template_cmd.txt | tr -d ' ' | sort)
```

4. **归类遗漏**：把 diff 出的差异参数逐个归入 4.1 的六类表格，并回答：哪些是"缺了就无法工作"（如 `--extra-args` 里的 `--distributed-executor-backend mp`、`--enable-expert-parallel`），哪些是"性能/特性开关"（如 `--vllm-enable-mc2`、`--num-speculative-tokens`、`--hccl-*`）？

**预期结果**：你的最小版约 15 个参数，模板真版约 25 个；核心差异集中在 `--extra-args`（一大串引擎行为开关）、`--additional-config`（低时延与图编译配置，见 u5-l1/u5-l2）、`--gpu-util`、`--vllm-enable-mc2`、`--num-speculative-tokens` 与两个 `--hccl-*` 参数。若能准确预言其中 10 个以上的用途，说明本讲目标已达成。此实践无需真实 NPU，diff 与归类在纯阅读环境下即可完成；实际执行命令「待本地验证」。

## 6. 本讲小结

- `pd_run.sh` 是部署链路的收口：六类约 40 个长参数经 `case` 分派函数逐对消费（每选项必带值），分三路输出——命令行传给 `start_api_servers.py`、环境变量传给 llm_datadist/HCCL、四字段拼进 `kv-transfer-config` JSON。
- export 段存在硬编码覆盖（`VLLM_USE_V1=1`、`VLLM_WORKER_MULTIPROC_METHOD=fork`）与条件导出（`HCCL_BUFFSIZE>0`、`ASCEND_RT_VISIBLE_DEVICES` 仅显式传时），排障时以"Current Configuration"回显为准。
- ranktable 是 llm_datadist 组网的花名册：`pd_run.sh` 只负责 export 两个路径与 `ROLE`/POD_NUM/IP 列表，直接消费者是容器内的 llm_datadist 运行库；omni-npu 侧仅 `llmdatadist_manager_v1.py` 读取 `DECODE_POD_NUM` 计算 P 侧起始 rank。仓库内无 ranktable 生成脚本，生产模板全部使用默认相对路径。
- 多 API server 的核心恒等式是"一 server 一 DP"：第 rank 个 server 拿第 rank 张卡、监听递增端口；decode 侧（ROLE=decode 且 DP>1）才追加 `--data-parallel-size/-rank` 组成 DP 集群，prefill 侧是单个 TP16 独立引擎。
- 跨机 D 实例靠 `server_offset` 消除全局 DP 编号撞车（VLLM_DP_RANK = rank + offset/tp），offset 由 ansible 按各机卡数累加生成 `{host: offset}` 映射注入模板。
- 多机实例走 Ray 分支（判据是 docker exec 注入的 `NODE_IP_LIST` 含逗号）：head 起 `ray --head` 后统一拉起 server，从节点仅连 6379 集群；服务起来后 `bind_cpu.sh` 按"每 Worker 20 核领地"完成绑核收尾。

## 7. 下一步学习建议

本讲把"ansible 模板 → pd_run.sh → start_api_servers.py → vllm serve"的完整拉起链走通了，至此单元 4（PD 分离与 KV 传输链路）收官。建议：

- 回顾本单元四讲的闭环：u4-l1 的配置面（kv-transfer-config 四字段）→ u4-l2 的 `LLMDataDistConnector` 四类协作 → u4-l3 的端口矩阵 → 本讲的拉起脚本。可重读 `tools/scripts/pd_run.sh` 全文，验证能否为每一行归队到某讲的内容。
- 下一单元（u5）进入性能与功能机制：`--additional-config '{"enable_low_latency": true}'` 与 `--extra-args` 里成串的开关将分别落地为模型最佳实践配置系统（u5-l1）与图编译机制（u5-l2）；本讲模板 decode 侧 EXTRA_ARGS 中的 `--compilation-config {"level": 3, ...}` 正是 u5-l2 的入口。
- 若对部署侧更感兴趣，可先跳到 u6-l3（proxy 的 ansible 接入），把 `run_proxy_cmd` 与本讲的 `run_vllm_server_prefill/decode_cmd` 三者对照，看 C 节点如何消费本讲拉起的 16 个 api_port。
