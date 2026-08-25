# u1-l5 请求测试、日志跟踪与常见问题排查

## 1. 本讲目标

上一讲（[u1-l4](u1-l4-first-deploy-bf16.md)）我们已经用 ansible 把 1P1D BF16 服务拉了起来。本讲把这套服务当「靶场」，学完之后你应当能够：

1. 使用 `curl` 向 proxy 端口（默认 7000）发送 OpenAI 兼容的 `/v1/chat/completions` 请求，并正确填写 `model` 字段。
2. 说清楚部署目录下四类日志文件（`run_prefill.log`、`run_decode.log`、`server_N.log`、`nginx_*.log`）各自是谁写的、先看哪个。
3. 通过日志确认服务就绪（`Application startup complete`）、跟踪一次请求在各层的足迹。
4. 掌握 HCCL 多机通信失败的定位与修复方法（`HCCL_SOCKET_IFNAME`），并建立一套自己的排障决策树。

## 2. 前置知识

本讲不需要新的工程知识，但有几个概念先用大白话过一遍：

- **OpenAI 兼容 API**：业界事实标准的 HTTP 接口约定。任何服务只要实现了 `POST /v1/chat/completions`（对话补全）这套请求/响应格式，OpenAI 的 SDK、各类客户端就能直接用。vLLM 引擎原生实现这套接口，本项目的 proxy 也原样转发它。
- **请求体字段**：`model`（要访问的模型名，由服务端决定合法值）、`messages`（对话历史数组）、`max_tokens`（最多生成多少 token）、`temperature`/`top_p`/`top_k`（采样参数）、`stream`（是否流式返回）。
- **流式（SSE）**：`stream: true` 时，服务端不等全部生成完，而是边生成边推送若干 `data: {...}` 文本块（Server-Sent Events 格式），最后以 `data: [DONE]` 结束。非流式则等生成完毕一次性返回完整 JSON。
- **HTTP 状态码**：`200` 成功；`404` 通常表示路径或模型名不对；`5xx` 通常是服务端内部错误。排障时先看状态码，再看响应体里的错误信息。
- **HCCL**：Huawei Collective Communication Library，昇腾的集合通信库，地位类似 GPU 世界的 NCCL，负责 AllGather、AllReduce 这类多卡/多机算子通信。多机通信要走机器的以太网卡（通常是 RoCE 网卡）；当一台机器有多块网卡时，HCCL 自己「猜」网卡可能猜错，建链就会失败或超时——这正是本讲要修的头号问题。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `README.md` | 「推理服务拉起」「发请求测试」两节是本讲的主依据：日志跟踪命令、HCCL 修复方法、curl 请求样例都在这里 |
| `tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml` | 1P1D BF16 部署模板：定义了所有日志文件的落盘位置、proxy 监听端口、「等待服务就绪」的判断条件、网卡自动探测任务 |
| `tools/ansible/92B/omni_infer_inventory_used_for_1P1D.yml` | 节点清单：`proxy_port: 7000` 与 C 节点的定义 |
| `tools/scripts/pd_run.sh` | P/D 服务的最终启动脚本：导出 GLOO/TP 网卡变量、HCCL 超时参数，并调用 `start_api_servers.py` |
| `tools/scripts/start_api_servers.py` | 逐个拉起 vLLM API server 的 Python 脚本：`server_N.log` 就是它打开的 |
| `components/omni-npu/examples/serve-pd-disaggregate.sh` | omni-npu 自带的手工部署示例：里面能看到 `HCCL_SOCKET_IFNAME` 的标准用法，作为参照 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**OpenAI 兼容 API**、**日志跟踪**、**常见故障排查**。

### 4.1 OpenAI 兼容 API：把第一发请求打到 proxy

#### 4.1.1 概念说明

PD 分离架构对客户端必须透明：使用方不应该关心「prefill 在哪台机器、decode 在哪台机器」。实现这一点的就是 C 节点上的 nginx + omni-proxy——它是整套系统**唯一**对客户端负责完整 PD 流转的入口。本模板中 proxy 采用 `sequential`（顺序）策略：一个请求先被送往 prefill 侧端点算出 prompt 的 KV Cache，再被送往 decode 侧端点逐 token 续写，最后把结果还给客户端（策略细节在 [u6-l1](u6-l1-proxy-overview.md) 展开）。

请求里最容易踩坑的字段是 `model`：它必须与部署模板 `environment` 中的 `SERVED_MODEL_NAME` 完全一致（92B 默认 `openPangu-2.0-Flash`，505B 默认 `openPangu-2.0-Pro`），否则引擎会认为访问了不存在的模型。

#### 4.1.2 核心流程

一次请求的完整路径：

```text
客户端 curl
   │  HTTP POST，端口 7000（C 节点 = proxy）
   ▼
nginx + omni-proxy（sequential 策略）
   │  ① 选一个 prefill 上游（P 机 api_port，9000 段）
   │  ② prefill 计算 prompt KV 后，再选 decode 上游（D 机 api_port，9100 段，共 16 个 DP server）
   ▼
按 stream 取值返回：
   ├─ stream=false → 生成完毕，一次性返回完整 JSON
   └─ stream=true  → 边生成边推送 data: {...} 块，最后 data: [DONE]
```

proxy 监听端口的推导链（从配置到生效）：

```text
inventory: proxy_port: 7000
   → C 节点 node_port = proxy_port + node_rank = 7000
   → run_docker 时以 -e PROXY_NODE_PORT=$NODE_PORT 写入容器
   → run_proxy_cmd 里 omni_proxy.sh --listen-port "$PROXY_NODE_PORT"
```

#### 4.1.3 源码精读

**① 官方 curl 样例（本模块最重要的代码）**

[README.md:L152-L176](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L152-L176) 给出了标准测试请求：向 `${MASTER_NODE_IP}:7000` 发 POST，`model` 填 `openPangu-2.0-Flash`（须与 `SERVED_MODEL_NAME` 一致），`stream: false`。README 同时注明：`MASTER_NODE_IP` 取 inventory 中 C 节点的 `ansible_host`，端口对应 `proxy_port`（默认 7000）。

**② proxy 端口与入口的定义**

- [omni_infer_inventory_used_for_1P1D.yml:L10](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_inventory_used_for_1P1D.yml#L10) 定义 `proxy_port: 7000`；[L37-L43](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_inventory_used_for_1P1D.yml#L37-L43) 定义 C 节点的 `node_port = proxy_port + node_rank`，即 7000。
- [omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L246-L291](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L246-L291) 是 proxy 启动脚本：第 277 行 `--listen-port "$PROXY_NODE_PORT"` 决定监听端口，第 288 行 `--omni-proxy-pd-policy sequential` 决定「先 prefill 后 decode」的顺序调度策略——这就是 4.1.2 流程图中 proxy 两步转发的依据。

**③ `model` 字段合法值的来源**

[omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L18](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L18) 在 `environment` 中声明 `SERVED_MODEL_NAME: "openPangu-2.0-Flash"`。它经 `docker exec -e` 传入容器，最终成为 `vllm serve --served-model-name` 的取值（见 [start_api_servers.py:L185](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/start_api_servers.py#L185)），vLLM 只接受与该名字匹配的 `model` 字段。

**④ 顺带一提：思考输出会被切分**

P/D 脚本都启用了 `--reasoning-parser pangu` 并配置 `<think>`/`</think>` 标记（[模板 L92-L94](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L92-L94)），因此若模型输出包含思考过程，响应中会把思考与正式回答拆成不同字段。机制细节在 [u5-l4](u5-l4-parsers-and-thinking.md) 讲解，本讲只需把它当作观察点。

#### 4.1.4 代码实践：stream=false 与 stream=true 对比

**实践目标**：验证服务端到端可用，并直观感受两种返回模式的差异。

**操作步骤**（在任一能连通 C 节点的机器上执行）：

1. 非流式请求（即 README 原样示例）：

   ```bash
   curl -X POST http://${MASTER_NODE_IP}:7000/v1/chat/completions \
       -H "Content-Type: application/json" \
       -d '{
           "model": "openPangu-2.0-Flash",
           "messages": [{"role": "user", "content": "Who are you?"}],
           "max_tokens": 512,
           "temperature": 1,
           "top_p": 1.0,
           "top_k": -1,
           "stream": false
       }'
   ```

2. 流式请求（示例代码：基于上例仅把 `stream` 改为 `true`，并加 `-N` 关闭 curl 缓冲，否则可能看不到逐步输出）：

   ```bash
   curl -N -X POST http://${MASTER_NODE_IP}:7000/v1/chat/completions \
       -H "Content-Type: application/json" \
       -d '{
           "model": "openPangu-2.0-Flash",
           "messages": [{"role": "user", "content": "Who are you?"}],
           "max_tokens": 512,
           "stream": true
       }'
   ```

3. 再故意发一个 `model` 字段错误的请求（示例代码，如把模型名写成 `gpt-4`），记录返回的状态码与错误信息。

**需要观察的现象**：

- 非流式：curl 阻塞一段时间后一次性打印完整 JSON，结构包含 `choices[0].message.content` 与 `usage`（token 计数）。
- 流式：终端逐步滚出多行 `data: {...}`，每行只带增量文本（`delta`），最后一行是 `data: [DONE]`；第一个 chunk 到达明显早于非流式的完整返回。
- 两种模式下若模型输出了思考内容，观察思考与回答是否被拆到不同字段（如 `reasoning_content` 与 `content`）。
- 错误模型名：返回非 200 状态码与错误提示。

**预期结果**：两种请求都拿到模型回答；流式首包更快；错误模型名被服务端拒绝。

**待本地验证**：具体的字段形态（如 `reasoning_content` 是否出现、错误信息的原文）取决于引擎版本与模型输出，本文不代运行，请以实际响应为准。

#### 4.1.5 小练习与答案

**练习 1**：请求发到了 7000 端口，为什么不能直接把客户端指向 P 机的 9000 端口或 D 机的 9100 端口？

**答案**：7000 是 proxy，它以 sequential 策略负责「先 prefill 后 decode」的完整编排；9000/9100 段是 proxy 与各引擎实例之间的内部端口，直连单侧引擎不能完成 PD 全流程，因此排障时也不能拿直连内部端口的结果代替对 7000 的验证。

**练习 2**：把 `model` 写错会发生什么？合法取值由哪个变量决定？

**答案**：会被拒绝（非 200 响应）；合法取值由模板 `environment` 里的 `SERVED_MODEL_NAME` 决定，经 `docker exec -e` 与 `--served-model-name` 一路传给 vLLM，请求体的 `model` 必须与它完全一致（见 [README.md:L156](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L156)）。

**练习 3**：`top_k: -1` 是什么意思？

**答案**：vLLM 约定 `-1` 表示不启用 top-k 过滤（在全部词表上采样），与 `top_p: 1.0`、`temperature: 1` 组合即「不做额外截断」的默认采样配置。

### 4.2 日志跟踪：四类日志文件与关键里程碑

#### 4.2.1 概念说明

上一讲已建立「先看 `run_*.log` 再看 `server_N.log`」的直觉，本讲把它补全成一张**日志地图**。所有日志都落在 `LOG_PATH/<inventory_hostname>/` 目录下（每台节点一个子目录，例如 `p0/`、`d0/`、`c0/`），日志分四层：

| 层 | 文件 | 写入者 | 看什么 |
| --- | --- | --- | --- |
| 脚本层 | `run_prefill.log` / `run_decode.log` | 启动脚本的 shell 重定向 | pd_run.sh 的配置打印、python 报错栈——**任何拉起失败先看这里** |
| 引擎层 | `server_0.log` … `server_N.log` | `start_api_servers.py` 为每个 API server 打开 | vLLM 引擎里程碑：权重加载、KV connector 初始化、`Application startup complete` |
| 代理层 | `nginx_error.log` / `nginx_access.log` | omni-proxy | 客户端请求是否到达、被转发到哪个上游 |
| 汇总层 | 执行机 `LOG_PATH_IN_EXECUTOR/` | ansible `fetch_log` 任务 | 把各机日志拉回控制机统一排查 |

#### 4.2.2 核心流程

日志产生的链路（结合上一讲的环境变量三层传递理解）：

```text
ansible run_server tag
  → 生成 vllm_run_for_p.sh / vllm_run_for_d.sh 并 docker exec 执行
      → 脚本末尾调用 pd_run.sh，stdout/stderr 重定向到 run_prefill.log / run_decode.log   ← 脚本层
          → pd_run.sh 调 start_api_servers.py --log-dir ${LOG_PATH}/<host>
              → 为第 rank 个 server 打开 server_{rank}.log，vllm serve 的输出全部写入      ← 引擎层
ansible run_proxy tag
  → omni_proxy.sh --log-file nginx_error.log --access-log-file nginx_access.log            ← 代理层
ansible --tags fetch_log
  → 把各机 LOG_PATH/<host>/ 同步回执行机 LOG_PATH_IN_EXECUTOR/<host>/                      ← 汇总层
```

判断服务就绪不看进程数，而看引擎层日志中的固定标志：`Application startup complete`——连 ansible 自己都是用 `grep` 这行文本来等待就绪的（见下）。

#### 4.2.3 源码精读

**① 脚本层日志的重定向位置**

[omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L121-L148](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L121-L148)：P 节点脚本在第 121 行调用 `pd_run.sh`，第 148 行 `> ${LOG_PATH}/{{ inventory_hostname }}/run_prefill.log 2>&1 &` 把标准输出与错误都并入该文件。D 节点对应 [L215-L244](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L215-L244)，写出 `run_decode.log`。注意结尾的 `&`：脚本是后台运行的，ansible 命令返回≠服务就绪，必须看日志。

**② server_N.log 是谁打开的**

[start_api_servers.py:L210-L223](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/start_api_servers.py#L210-L223)：第 211 行 `log_file = open(os.path.join(log_dir, f"server_{rank}.log"), "w")` 按 rank 为每个 API server 建日志，第 218-223 行用 `subprocess.Popen` 启动 `vllm serve` 并把 stdout/stderr 都重定向进去。脚本自己也在 [L229-L232](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/start_api_servers.py#L229-L232) 提示你 `tail -f {log_dir}/server_*.log` 实时观察。若某个 server 进程挂了，监控循环会打印「Check {log_dir}/server_{i}.log for details」（[L338-L344](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/start_api_servers.py#L338-L344)）——这也是一条排障入口。1P1D 中 P 机只有 1 个 server（TP16），D 机有 16 个 DP server，即 `server_0.log` 到 `server_15.log`。

**③ 「就绪标志」的权威出处**

[omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L908-L921](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L908-L921)：ansible 在容器内 `grep -q "Application startup complete" .../server_0.log`，命中即认为服务就绪，300 秒超时则报错退出。我们手工验证就绪应使用同一判据（该任务默认仅在开启 `proc_bind` 时执行，见第 925-927 行的 `when` 条件，但判据本身通用）。README 的跟踪命令对应 [README.md:L141-L144](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L141-L144)。

**④ 代理层日志的路径参数**

[omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L280-L282](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L280-L282)：`omni_proxy.sh` 的 `--log-file` 与 `--access-log-file` 分别指向 `nginx_error.log` 与 `nginx_access.log`（在 C 节点即 P 机的日志目录下）。

**⑤ 两个影响日志内容的开关**

- decode 侧 `EXTRA_ARGS` 含 `--disable-log-requests`（[L202](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L202)），所以 D 机 server 日志默认不打印请求内容；想在引擎层看请求足迹，优先看 P 机日志。
- `VLLM_LOGGING_LEVEL` 由 pd_run.sh 导出（[pd_run.sh:L33](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L33)、[L306](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L306)），默认 INFO；排障想看更多细节可按其帮助说明（[L85](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L85)）改为 DEBUG。

**⑥ 汇总层：fetch_log**

[omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L1091-L1107](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L1091-L1107)：`--tags fetch_log` 会把各机的 `LOG_PATH/<host>/` 整体拉回执行机的 `LOG_PATH_IN_EXECUTOR/<host>/`，多机排障时免去逐台 scp。

#### 4.2.4 代码实践：跟踪一次请求在三层日志中的足迹

**实践目标**：建立「一个请求会在哪些日志留下痕迹」的具象认知。

**操作步骤**：

1. 在 P 机（兼 C 节点）开两个终端：

   ```bash
   # 终端 A：proxy 访问日志
   tail -f /path/to/server/log/p0/nginx_access.log
   # 终端 B：prefill 引擎日志
   tail -f /path/to/server/log/p0/server_0.log
   ```

2. 在 D 机开一个终端（路径换成你的 `LOG_PATH`）：

   ```bash
   tail -f /path/to/server/log/d0/server_0.log
   ```

3. 发送 4.1.4 的流式请求，同时观察三个窗口。

**需要观察的现象**：请求时刻附近，`nginx_access.log` 新增一条对该请求的访问记录；P 机 `server_0.log` 出现接收/处理该请求的日志；D 机日志因 `--disable-log-requests` 默认安静，但吞吐/引擎统计类日志仍在滚动。

**预期结果**：三个窗口都能看到与请求时间点对应的滚动，且 proxy 日志的时间戳最早。

**待本地验证**：各层日志的具体行文（字段、格式）随引擎版本变化，请以实际输出为准；若某层完全无动静，说明请求没到达该层，正好用 4.3 的决策树继续定位。

#### 4.2.5 小练习与答案

**练习 1**：`run_prefill.log` 和 `server_0.log` 是什么关系？拉起失败先看哪个？

**答案**：前者是 P 节点启动脚本（最终调用 pd_run.sh）的 shell 重定向输出，后者是 `start_api_servers.py` 为 rank 0 的 vLLM API server 单独打开的引擎日志，前者包含后者的启动发起过程。拉起失败先看 `run_prefill.log`——脚本层错误（参数拼错、python 异常栈）最先出现在这里；它没异常再看 `server_0.log` 的引擎层。

**练习 2**：如何用一条命令判断服务是否就绪？

**答案**：仿照 ansible 的判据 `grep -q "Application startup complete" ${LOG_PATH}/<host>/server_0.log && echo ready`；连续观察则用 `tail -f` 并等这行出现（[模板 L913](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L913)）。

**练习 3**：为什么 D 机 16 个 server 的日志里看不到请求内容？

**答案**：decode 侧 `EXTRA_ARGS` 显式带了 `--disable-log-requests`（[L202](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L202)），vLLM 因此不打印请求日志；如需观察，去掉该参数重跑 `run_server` 即可（注意生产环境日志量）。

### 4.3 常见故障排查：HCCL 多机通信失败与修复

#### 4.3.1 概念说明

PD 分离下，P 与 D 是两台机器，多卡/多机算子通信由 HCCL 完成，跨机流量走以太网卡（通常为 RoCE）。机器上往往有多块网卡（管理网卡、业务网卡等），HCCL 自动选网卡时可能选中不通的那块，表现为**建链超时或直接失败**，服务拉不起来。README 给出的修复手段就是显式告诉 HCCL 用哪块网卡：

```bash
export HCCL_SOCKET_IFNAME=enp23s0f3   # 网卡名通过 ifconfig 获取
```

注意两套「网卡变量」分工不同：`GLOO_SOCKET_IFNAME`/`TP_SOCKET_IFNAME` 管 vLLM 框架层（数据并行/tensor 并行的辅助通信）用的网卡，ansible 部署时会自动探测并传入；`HCCL_SOCKET_IFNAME` 管昇腾集合通信库用的网卡，**ansible 模板默认不设**，需要时手工加。

#### 4.3.2 核心流程

服务异常时的排障决策树（自上而下，先便宜后昂贵）：

```text
请求失败 / 服务异常
│
├─ ① curl 7000 连不通？
│     → 看 C 节点 nginx_error.log、容器是否存活（docker ps）
├─ ② 连通但立即报错（4xx）？
│     → 多半是 model 字段 ≠ SERVED_MODEL_NAME，或请求体格式问题
├─ ③ 服务没就绪（server_0.log 迟迟没有 Application startup complete）？
│     → run_prefill.log / run_decode.log 找脚本层报错
│     → 卡在分布式/通信初始化且长时间无进展 → 疑似 HCCL 建链问题 → ④
├─ ④ HCCL 多机通信失败？
│     → ifconfig（或 ip -4 route list 0/0）确认正确网卡名
│     → 在 run_vllm_server_prefill_cmd / decode_cmd 中 export HCCL_SOCKET_IFNAME=<网卡名>
│     → 重跑 --tags run_server（必要时先 --tags stop_server 清理残留进程）
└─ ⑤ 单机正常、仅跨机异常？
      → 检查两机该网卡互通（ping 对端网卡 IP）、防火墙、以及
         P/D 两侧脚本里的网卡名是否一致
```

配套的运维 tag：`stop_server`（杀掉容器内 python/ray 进程，[模板 L730-L780](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L730-L780)）、`fetch_log`（回收日志）、`clean_up`/`run_docker`（重建容器）。

#### 4.3.3 源码精读

**① 官方修复建议的唯一出处**

[README.md:L146-L150](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L146-L150)：「如果遇到多机通信问题导致拉起失败，可以在 `run_vllm_server_prefill_cmd` 和 `run_vllm_server_decode_cmd` 增加以下变量解决：`export HCCL_SOCKET_IFNAME=enp23s0f3 # 通过ifconfig获取`」。注意加的位置：这两个变量块在模板 `vars` 段内（P 侧 [L62 起](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L62-L100)，D 侧 [L150 起](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L150-L175)），与其他 `export HCCL_*` 放一起即可。

**② 框架层网卡是自动探测的**

[omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L817-L827](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L817-L827)：playbook 用 `ip -4 route list 0/0 | awk '{print $5}' | head -1` 取默认路由网卡，存为 `default_interface`，随后以 `SOCKET_IFNAME` 环境变量传给 P/D 启动任务（[L842](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L842)、[L873](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L873)），最终进入 pd_run.sh 的 `--gloo-socket-ifname`/`--tp-socket-ifname`（[模板 L126-L127](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L126-L127)）。这条命令也是你手工查网卡时该用的命令。

**③ pd_run.sh 对网卡与 HCCL 超时的处理**

- [pd_run.sh:L31-L32](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L31-L32) 定义 `GLOO_SOCKET_IFNAME`/`TP_SOCKET_IFNAME` 默认值，帮助文本（[L83-L84](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L83-L84)）同样给出 `ip -4 route list 0/0 | awk '{print $5}' | head -n 1` 的查询命令；[L304-L305](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L304-L305) 将二者导出给子进程。
- [pd_run.sh:L331-L332](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L331-L332) 导出 `HCCL_CONNECT_TIMEOUT=1800`、`HCCL_EXEC_TIMEOUT=120`。含义：建链超时给到 30 分钟——所以网卡选错时故障常表现为「长时间卡住然后超时」，而不是立刻报错；定位时要沉得住气，或临时调小超时做快速复现（见下方实践）。
- P/D 脚本自己还设了 `HCCL_BUFFSIZE`（P 侧 100、D 侧 1200）等 HCCL 调优参数（[模板 L66-L70](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L66-L70)、[L154-L159](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L154-L159)），这是 HCCL 相关变量的既有先例——把 `HCCL_SOCKET_IFNAME` 加在同一处最自然。

**④ omni-npu 官方示例中的标准用法（参照物）**

[components/omni-npu/examples/serve-pd-disaggregate.sh:L63-L71](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/examples/serve-pd-disaggregate.sh#L63-L71)：omni-npu 自带的手工部署示例同时导出 `GLOO_SOCKET_IFNAME` 与 `HCCL_SOCKET_IFNAME` 为同一块网卡，并设置 `HCCL_INTRA_ROCE_ENABLE=1`。可见「两套网卡变量指到同一块正确网卡」就是官方推荐形态。

#### 4.3.4 代码实践：复现 HCCL 通信失败并用 HCCL_SOCKET_IFNAME 修复

> 本实践需要真实的多机部署环境（1P1D 两台 A3）；没有环境时可先做「源码阅读版」：只完成步骤 1、2、6，在脑内推演。

**实践目标**：亲手制造并修复一次 HCCL 网卡选择错误，掌握这类故障的完整闭环。

**操作步骤**：

1. **记录正确的网卡名**（在 P、D 两机分别执行并记录）：

   ```bash
   ifconfig        # 或 ip -4 route list 0/0 | awk '{print $5}' | head -n 1
   ```

2. **确认基线可用**：按 4.1.4 发一次请求成功，并记下 `server_0.log` 中 `Application startup complete` 的时间戳。

3. **注入故障**：编辑模板 `omni_infer_server_template_performance1P1D_92B_bf16_open.yml`，在 `run_vllm_server_prefill_cmd` 与 `run_vllm_server_decode_cmd` 的 export 区（4.3.3 ③ 所示位置）各加一行错误网卡（示例代码）：

   ```bash
   export HCCL_SOCKET_IFNAME=lo   # lo 为本机回环，跨机必然不通；也可写一个不存在的名字
   ```

   为了不等待 30 分钟超时，可顺手把两脚本中的 `HCCL_CONNECT_TIMEOUT=1800` 临时调小（如 120）。

4. **重启服务并观察失败**：

   ```bash
   ansible-playbook -i omni_infer_inventory_used_for_1P1D.yml \
     omni_infer_server_template_performance1P1D_92B_bf16_open.yml \
     --tags stop_server,run_server
   ```

   然后盯住 P 机 `tail -f ${LOG_PATH}/p0/run_prefill.log` 与 `server_0.log`。

5. **修复**：把第 3 步改成正确网卡名（`export HCCL_SOCKET_IFNAME=<步骤1查到的名字>`，恢复超时值），重跑 `--tags stop_server,run_server`，重新 grep `Application startup complete`，再按 4.1.4 发请求验证。

6. **沉淀**：把故障态与恢复态的日志片段各留一份，标注时间戳与网卡名，形成你的排障案例库。

**需要观察的现象**：故障态下，`server_0.log` 长时间停在分布式/通信初始化阶段，最终出现 HCCL 建链超时或失败类错误；进程可能反复重启或退出，`run_*.log` 里能看到 python 侧的异常栈。修复态下，初始化顺利走完，出现就绪标志。

**预期结果**：注入错误网卡 → 服务拉起失败（卡住后超时）；改回正确网卡 → 服务恢复就绪且请求成功。

**待本地验证**：具体的 HCCL 报错原文、卡住的具体日志阶段与实际等待时长取决于 CANN/HCCL 版本及网络环境，本文未代运行；`stop_server` tag 会 `kill -9` 容器内 python/ray 进程，重跑前确认没有别的业务在用该容器。

#### 4.3.5 小练习与答案

**练习 1**：`GLOO_SOCKET_IFNAME` 和 `HCCL_SOCKET_IFNAME` 有何区别？ansible 部署时它们分别怎么取值？

**答案**：前者管 vLLM 框架层辅助通信（gloo）网卡，后者管昇腾集合通信库 HCCL 的建链网卡。ansible 部署时前者由 playbook 用默认路由命令自动探测并经 `--gloo-socket-ifname`/`--tp-socket-ifname` 传入 pd_run.sh（[模板 L817-L827](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L817-L827)）；后者模板默认不设，出现多机通信失败时按 README 手工添加。

**练习 2**：为什么网卡配错时故障常表现为「卡住很久才报错」而不是立刻失败？

**答案**：pd_run.sh 默认导出 `HCCL_CONNECT_TIMEOUT=1800`（[pd_run.sh:L331](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L331)，模板脚本同样设 1800），建链要等满超时才放弃。排障实践里可临时调小以快速复现。

**练习 3**：服务拉起失败，你按决策树查到 `run_decode.log` 里有 python 异常栈，下一步该做什么？

**答案**：先读栈顶确定是脚本层/参数问题还是底层通信问题：若是通信初始化阶段的 HCCL 错误，按 4.3.4 流程核对并显式设置 `HCCL_SOCKET_IFNAME` 后用 `--tags stop_server,run_server` 重启；若是其他异常（路径、权限、参数），回到 u1-l4 的环境变量三层传递链检查对应配置，修复后同样重启验证。

## 5. 综合实践

**任务：给你的 1P1D 服务做一次「全链路体检」，产出一份个人排障 runbook。**

在已完成 u1-l4 部署的两台 A3 上，依次完成并记录：

1. **就绪体检**：在 P、D 两机分别 grep `Application startup complete`，记录两机就绪的时间差。
2. **请求体检**：执行 4.1.4 的三发请求（非流式/流式/错误模型名），保存三份完整响应，标注：状态码、首包时间、总耗时、流式 chunk 数。
3. **日志足迹**：按 4.2.4 同时 tail 三个日志窗口，把「同一请求」在 `nginx_access.log`、P 机 `server_0.log`、D 机 `server_0.log`（若有输出）中的对应行摘录下来，画出时间线。
4. **故障演练**：完成 4.3.4 的复现与修复，保留故障/恢复两态的日志证据。
5. **汇总**：用 `--tags fetch_log` 把全部日志拉回执行机，写一页 runbook：包含端口连通性检查命令、日志地图（4.2.1 的表）、排障决策树（4.3.2）、以及你环境里验证过的正确网卡名。

预期结果：runbook 能让另一位同事在你不在场时独立完成「验证服务 → 定位一层故障 → 修复 HCCL 网卡问题」全流程。无真机环境时，1-4 步替换为「源码阅读版」：写出每步应观察的文件与预期标志位即可，并标注待本地验证。

## 6. 本讲小结

- 客户端只认 proxy 的 7000 端口（inventory `proxy_port` 决定，经 `PROXY_NODE_PORT` 传给 `omni_proxy.sh --listen-port`）；proxy 以 sequential 策略完成「先 prefill 后 decode」的编排，请求体的 `model` 必须与 `SERVED_MODEL_NAME` 完全一致。
- 流式与非流式的差异是体验层最重要的观察点：非流式一次性返回完整 JSON，流式以 `data: {...}` 增量块推进、以 `data: [DONE]` 收尾；排障测试流式时记得 `curl -N`。
- 日志地图四层：`run_prefill.log`/`run_decode.log`（脚本层，失败首看）、`server_N.log`（引擎层，P 机 1 个、D 机 16 个，由 `start_api_servers.py` 创建）、`nginx_error/access.log`（代理层）、`fetch_log` 拉回的汇总层；就绪判据是 `server_0.log` 中的 `Application startup complete`——与 ansible 内部判据一致。
- 排障决策树自上而下：proxy 连通性 → 4xx（多为 model 字段）→ 就绪标志与脚本层日志 → HCCL 通信初始化 → 跨机网卡互通。
- HCCL 多机通信失败的标准修复：在模板 `run_vllm_server_prefill_cmd`/`run_vllm_server_decode_cmd` 中 `export HCCL_SOCKET_IFNAME=<ifconfig 查到的网卡名>` 后重跑 `run_server`；框架层网卡（GLOO/TP）ansible 会自动探测，两者应指向同一块正确网卡。
- HCCL 建链超时默认给到 1800 秒，网卡配错常表现为「长时间卡住后超时」；复现实验可临时调小超时加快迭代。

## 7. 下一步学习建议

- 单元 1 至此收尾：你已经能独立部署并运维一个 1P1D 服务。建议回头把三份 README（`README.md`、`README_EN.md`、`README_INT8.md`）的排障段落对照通读一遍，英文版与 INT8 版措辞差异里常藏着额外线索。
- 想知道请求在 P、D 之间「搬运」时 KV Cache 如何跨节点传输，进入 [u4-l1：PD 分离与 KV 传输配置全景](u4-l1-pd-kv-transfer-config.md)，本讲反复出现的 `kv-transfer-config` 参数将在那里被逐字段拆解。
- 好奇 proxy 如何决定把请求发给哪个 prefill/decode 上游，进入 [u6-l1：Omni Proxy 架构与快速上手](u6-l1-proxy-overview.md)；本讲的 `sequential` 策略只是它两种调度模式之一。
- 如果你在排障中对容器内 `omni-npu` 如何接管 vLLM 产生兴趣，下一单元 [u2-l1：vLLM 插件体系与 omni-npu 的三个入口](u2-l1-vllm-plugin-entry.md) 从 `VLLM_PLUGINS` 环境变量讲起。
