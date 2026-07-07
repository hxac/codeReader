# 分布式协议、路由与流水线

## 1. 本讲目标

本讲承接 u9-l3（分布式推理架构与层切分）。上一讲回答了「谁负责哪几层、层如何切片加载」；本讲回答「层切好之后，多台机器之间到底用什么协议传数据、如何组成一条完整链路、如何让 prefill 跑得更快、生成为什么不快，以及 worker 掉线了怎么办」。

读完本讲，你应当能够：

- 说清 ds4 分布式协议的两种连接（控制连接 / 数据连接）和四种核心帧（`HELLO` / `WORK` / `RESULT` / 快照帧）。
- 解释 coordinator 如何把若干 worker 注册信息「拼」成一条覆盖全部层的 route，以及为什么中间 worker 可以直接转发给下一个 worker，而无需 coordinator 中转。
- 描述 prefill 的流水线（chunk N 与 chunk N+1 重叠）与生成阶段为何只能严格自回归，二者速度差异的根本原因。
- 理解滚动 token-prefix hash 如何充当 KV 状态指纹，以及「传输失败丢路由」与「KV/hash 不匹配重放」这两类故障的不同恢复路径。

## 2. 前置知识

本讲假设你已经掌握：

- **层切分**：`--layers A:B` 把 transformer 的 43 层切成若干闭区间，coordinator 持第 0 层起的一段并独占分词器与采样器（见 u9-l3）。
- **prefill 与 decode**：prefill 是一次性把整段提示填进 KV 缓存，decode 是逐 token 自回归生成（见 u1-l5、u6-l1）。
- **layer-major 图**：单机时一个 chunk 的推理图覆盖全部层（见 u5-l2、u6-l1）。
- **DSV4 payload**：会话 KV 状态的磁盘序列化格式（见 u8-l3）。
- 一点点 **TCP** 与 **端序（大端/小端）** 常识：协议帧在网络上用大端（network byte order），用 `htonl/ntohl` 转换。

几个术语先统一：

- **coordinator**：发起请求、持分词器与采样器、必持从第 0 层起一段的进程。
- **worker**：持中间或末尾一段层、各自维护自己那片 KV 的进程。
- **route**：一条从 coordinator 本地层末尾开始、首尾相接覆盖到最后一层（外加 output head 归属）的链路。
- **hop（跳）**：链路上的一个 worker。
- **span**：一次 `WORK` 帧要处理的 token 区间（`pos0 .. pos0+n_tokens`）。

## 3. 本讲源码地图

本讲几乎全部落在 `ds4_distributed.c`（约 8400 行，是 ds4 最大的单文件之一），辅以头文件与 README。

| 文件 | 作用 |
| --- | --- |
| [ds4_distributed.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c) | 协议帧定义、wire 编解码、coordinator 注册/路由、prefill 流水线、生成 eval、worker 端 work 处理、hash 校验、故障恢复、快照收发。全部在这里。 |
| [ds4_distributed.h](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.h) | 公共边界：声明 `ds4_dist_session` 与 `ds4_dist_session_sync/eval/save_payload/load_payload` 等。注释强调「分布式是引擎后端而非独立前端」。 |
| [README.md](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md) | `Distributed Inference`（概念、配置、链路对比表）与 `Distributed protocol overview`（协议层概述）两节是本讲的最佳向导。 |

头文件开篇一句点明设计取向——分布式不是独立前端，而是「编译进引擎的后端」，前端仍调用普通的 `ds4_session_*`：

[ds4_distributed.h:10-14](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.h#L10-L14) —— 这段注释说明：只有 worker / coordinator 一次性服务模式才直接调 `ds4_dist_run()`，CLI / server / agent 都透过普通 session API，由 coordinator 侧的 `ds4_dist_session` 沿路由透明分发。

## 4. 核心概念与源码讲解

### 4.1 协议帧与路由组建

#### 4.1.1 概念说明

把多台机器串成一条推理流水线，首先要解决两个问题：「我怎么知道谁在线、各自能算哪几层」和「算的时候数据怎么走」。

ds4 用**两种连接**分别回答这两个问题：

1. **控制连接（control connection）**：每台 worker 启动时主动连到 coordinator，并保持一条长连接。worker 在这条连接上发一个 `HELLO` 帧，把自己的「身份和能力」汇报上去：model id、量化档、负责的层区间 `[layer_start, layer_end]`、是否带 output head、上下文容量、以及**数据端口**。之后这条连接主要用于注册与心跳式存活探测，不传重数据。
2. **数据连接（data connection）**：真正干活时，coordinator 与 worker、worker 与 worker 之间另开低延迟 TCP 连接，跑 `WORK` / `RESULT` 帧。重数据（hidden state、logits）只走数据连接。

为什么分两套？因为控制连接是「注册表」，要稳定长存、随时能重组路由；数据连接是「临时管道」，可以按需建立、用完即弃，且为了低延迟会单独优化（如设置 `TCP_NODELAY`）。

coordinator 拿到一组 `HELLO` 注册后，要判断能否拼出一条**覆盖全部层**的 route。判定条件是（承接 u9-l3）：

- coordinator 本地从第 0 层起。
- 一串 worker **首尾相接**：第一个 worker 的 `layer_start` 紧接 coordinator 本地段的 `layer_end+1`，第二个紧接第一个的 `layer_end+1`，以此类推，直到覆盖最后一层。
- **output head 有归属**：最后一个 hop 要么自己持 output head（`has_output`），要么把最终 hidden state 回送给「能算 output head」的 coordinator。

route 一旦就绪，数据流就是 **worker-to-worker 直连**：coordinator 算完自己的本地段，把 hidden state 发给第一个 worker；第一个 worker 算完直接发给第二个 worker……最后一个 worker 把 logits（或 hidden state）发回 coordinator。coordinator **不当中转**。这就是 README 里那句「activations will flow in `A -> B -> C -> back to A`」。

#### 4.1.2 核心流程

注册与组路的心智模型：

```text
worker 启动
   │  TCP connect → coordinator 控制端口
   ▼
发 HELLO（model_id, quant_bits, layer_start/end, has_output, has_hidden,
         ctx_size, n_layers, listen_port）
   │
   ▼
coordinator 注册表新增一条；丢弃同 host+同层段的旧条目（stale worker）
   │
   ▼（每次注册变化都重算）
coordinator 路由搜索：从 local_end+1 递归找一条首尾相接、覆盖到 n_layers-1、
                   且 output head 有归属的 worker 链
   │
   ├── 不完整 → 记日志 "route incomplete; next needed layer N"，等更多 worker
   └── 完整   → 记日志 "complete route ready: local 0:X -> B .. -> C 31:output"
```

`WORK` 帧在线上的形态（关键字段）：

```text
frame_header(magic=DS4D, type=WORK, bytes)
ds4_dist_work_fixed {
    session_id, request_id,        // 标识一次会话与一次请求
    prefix_hash, result_hash,      // 本 span 前后的 token-prefix 指纹（见 4.3）
    pos0, n_tokens,                // 要处理的 token 区间
    layer_start, layer_end,        // 本 hop 负责的层
    flags,                         // INPUT_HC / OUTPUT_LOGITS / RESET_SESSION / ACK_ONLY
    token_bytes, input_hc_bytes, input_hc_bits,
    route_count, route_index,      // ★ 整条路由随帧下发，worker 知道自己在第几跳
    route_bytes
}
payload: tokens[] + input_hidden_state[] + route_blob[]
```

`route_blob` 是整条 route 的紧凑编码（每个 hop 一条 `ds4_dist_route_fixed` + host 字符串）。把它**塞进每一个 `WORK` 帧**是 worker-worker 直连的关键：任何一个 worker 收到帧后，都能从 `route_index` 读到「自己」，从 `route_index+1` 读到「下一个 worker」的地址，从而直接 connect 并转发，完全不需要 coordinator 介入。

#### 4.1.3 源码精读

**协议常量与 wire 记录。** 所有帧类型与标志位集中定义在文件开头：

[ds4_distributed.c:44-70](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L44-L70) 定义了 magic `0x44533444`（ASCII "DS4D"）、四种核心帧类型 `HELLO/ERROR/WORK/RESULT`、若干快照帧、`WORK` 的四个标志位、`RESULT` 的三种返回类型，以及本讲后文要反复用到的两个收包错误码：

- `DS4_DIST_RECV_TRANSPORT_ERROR = 1`：socket 层失败（对端断了）。
- `DS4_DIST_RECV_REMOTE_ERROR = 2`：对端逻辑层报错（KV/hash 不匹配等）。

这两条常量是 4.3 节故障分类的根。

**帧头与各 fixed 结构。** 每个帧都以一个 12 字节帧头开头（`magic + type + bytes`）：

[ds4_distributed.c:72-76](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L72-L76) —— `ds4_dist_frame_header`。`HELLO` 与 `WORK` 的 fixed 结构随后给出：

[ds4_distributed.c:78-89](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L78-L89) —— `ds4_dist_hello_fixed`，worker 上报的全部「身份与能力」字段。

[ds4_distributed.c:91-112](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L91-L112) —— `ds4_dist_work_fixed`。注意其中 `prefix_hash/result_hash`（4.3 节的指纹）、`flags`（含 `RESET_SESSION`、`ACK_ONLY`）、以及随帧下发的 `route_count/route_index/route_bytes`。`RESULT` 帧结构见 [ds4_distributed.c:128-139](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L128-L139)（含 `status`、`result_kind`、遥测 `telemetry_count`、`payload_bytes`）。

**HELLO 注册与陈旧 worker 丢弃。** coordinator 收到一个控制连接后，调 `dist_coordinator_add_worker` 把它登记进注册表：

[ds4_distributed.c:1856-1931](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L1856-L1931) —— 这段做两件事。其一，**丢陈旧条目**（L1892-L1908）：如果同一个 `peer_host + model_id + layer_start/end + has_output` 已经存在，就先摘掉旧的（一个 worker 重启后用同一个端口/层段重新注册，旧条目作废）。其二，把新条目挂到链表头，并 `state->generation++`（一个单调递增的「路由版本号」，用来判断缓存的路由计划是否还有效）。注册完若开了 `--debug` 就打印当前路由计划。

**路由搜索（递归回溯）。** coordinator 怎么从一堆 worker 里拼出一条合法链？核心是 `dist_route_search_workers`：

[ds4_distributed.c:1944-1989](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L1944-L1989) —— 先把 worker 按 `layer_start` 排序（见 `dist_worker_route_cmp` L1933），再从 `next = local_end+1` 起，**递归地**找 `layer_start == next` 的候选 worker：找到就压进 `path`，若它的 `layer_end` 已 `>= last`（最后一层）则成功；否则以 `child_missing = layer_end+1` 为新的 `next` 继续往下找（L1973-L1983）；失败则回溯（`(*path_len)--`）。`dist_worker_route_candidate_ok`（L1944-L1952）做候选过滤：若该 worker 不是末跳就必须能产 hidden state（`has_hidden`），若是末跳且不带 output head，则要求 coordinator 本身能算 output head（`local_can_output_head`）。

这是一段典型的「首尾相接链」回溯搜索：它保证链上任一相邻 worker 的层段不重叠不留空，且 output head 一定有归属。

**worker-worker 直连：route 随帧下发。** worker 收到 `WORK` 后如何知道下一个 worker 在哪？答案藏在帧里：

[ds4_distributed.c:7330-7354](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L7330-L7354) —— worker 从 `route_blob` 里按 `work.route_index` 取出「自己这一跳」（`current_route`），并据此判定 `has_next = route_index+1 < route_count`（L7333）。若是中间跳，就再取 `next_route`，算完后把结果直接发给它。coordinator 完全不参与中转。这也解释了为什么 `route_blob` 必须塞进每个 `WORK` 帧：每个 worker 都需要独立看到整条链。

README 的协议概述用一段话浓缩了上面全部机制：

[README.md:464-485](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L464-L485) —— 「two kinds of connections」「control TCP … send a `HELLO`」「work then moves over low-latency TCP data connections」「Middle workers can forward directly to the next worker」「The final worker returns logits to the coordinator, or ACKs for non-final prefill chunks」。最后一句还点明协议当前**无加密无鉴权**、非 release-stable，必须同 commit 构建、跑在可信机器与可信网络上——这是分布式模式的硬约束。

#### 4.1.4 代码实践

**实践目标**：在不真正联网的前提下，亲手在源码里「复盘」一次三机路由 A→B→C 是怎么拼出来的。

**操作步骤**：

1. 打开 `ds4_distributed.c`，定位 `dist_coordinator_add_worker`（L1856）与 `dist_route_search_workers`（L1954）。
2. 假设配置为 coordinator A：`--layers 0:19`，worker B：`--layers 20:35`，worker C：`--layers 36:output`，模型共 43 层（`n_layers=43`，`last=42`）。
3. 在脑中（或纸上）模拟：B、C 各发一个 `HELLO`；`add_worker` 把它们挂进链表；`report_plan` 排序后从 `next=20` 起搜索：
   - `next=20` 命中 B（`layer_start==20`），压入 path；B 的 `layer_end=35 < 42`，以 `child_missing=36` 递归。
   - `next=36` 命中 C（`layer_start==36`），压入 path；C 的 `layer_end=42 >= last` 且 `has_output`，返回成功。
4. 把 `--debug` 会打印的那行计划写下来，应形如：`local 0:19 -> B:PORT Q4 20:35 -> C:PORT Q4 36:output`。

**需要观察的现象**：路由计划的日志格式（`local ... -> host:port Qn start:end`），以及 `output` 这个特殊字样只出现在持 output head 的末跳。

**预期结果**：你能复现 `dist_coordinator_report_plan`（L1991）拼出的字符串结构，并指出每一段对应哪个 `ds4_dist_worker_entry` 字段。

> 待本地验证：若有两台以上可信机器，可按 README 的 Minimal two-host configuration 实跑一次 coordinator `--debug`，对照 stderr 里 `complete route ready:` 那行。本实践不要求实跑。

#### 4.1.5 小练习与答案

**Q1**：如果 worker B 注册了 `--layers 20:35`，但 worker C 还没上线（`36:output` 缺失），`--debug` 会打印什么？

**答**：会打印 `route incomplete; next needed layer 36`（见 L2075）。路由搜索在 `next=36` 处找不到任何 `layer_start==36` 的候选，`missing` 记为 36，`complete=false`。

**Q2**：为什么 `route_blob` 要随每个 `WORK` 帧下发，而不是只在建链时发一次？

**答**：因为 worker-worker 直连要求每个中间 worker 都能独立看到「下一个 worker」的地址（`route_index+1`）。若只在建链时下发，则每个 worker 必须各自记住全链，状态分散且难以随路由变化同步；随帧下发让路由信息无状态、自包含，worker 收到任意一帧即可工作。

**Q3**：`dist_worker_route_candidate_ok` 为什么要求「非末跳必须 `has_hidden`」？

**答**：中间跳算完后要把 hidden state 转发给下一跳，若该 worker 不能产出 hidden state（例如某些精简配置），链就断了。只有末跳可以二选一：要么自己产 logits（`has_output`），要么把 hidden state 回送给能算 output head 的 coordinator。

---

### 4.2 prefill 流水线与生成自回归

#### 4.2.1 概念说明

分布式推理有两种工作模式，速度命运截然不同：

- **prefill 可以流水线加速**。长提示被切成多个 chunk，coordinator 可以在算 chunk N+1 的本地段的同时，让 worker 算 chunk N——像工厂流水线一样让多 GPU 重叠工作。这是分布式 prefill 能比单机**更快**的根本原因。
- **生成只能严格自回归**。token N+1 必须等 token N 产生 logits 并采样后才能开始，而每个 token 都得整条 route 算完才有 logits。所以每生成一个 token 至少要付一次「跨机激活跳」，生成必然**慢于**单机。

README 给出实测对比（两台 M5 Max，Thunderbolt 5）：

| Prompt | 单机参考 | 两机分布式 | 加速比 |
| ---: | ---: | ---: | ---: |
| 9421 tokens | 421.70 t/s | 582.22 t/s | 1.38x |
| 63819 tokens | 353.62 t/s | 654.79 t/s | 1.85x |

而生成从单机 30.59 t/s 掉到分布式 24.67 t/s（**慢 19.4%**）。一句话：**分布式为「塞下更大模型」和「加速长 prefill」而生，不为加速 decode。**

关键概念：

- **prefill chunk**：prefill 的切分单位，默认 4096 token（PRO 档 8192），由 `--dist-prefill-chunk` / 环境变量控制，但 4096 是「规范值」。
- **flow window（流窗）**：允许同时在链路上「在飞」的 chunk 数，由 `--dist-prefill-window` 控制（上限 64）。它是流水线深度的安全阀：窗口太大可能压垮 worker 内存或让失败回滚代价过高。
- **send depth（发送深度）**：coordinator 本地「已算好但还没发出去」的 chunk 槽位数，默认 2（可被 `DS4_DIST_PREFILL_SEND_DEPTH` 调到 1–8）。它决定了 coordinator 能提前算几个 chunk 来喂流水线。
- **ACK_ONLY**：非最后一个 prefill chunk 不需要回送 logits，worker 只回一个 ACK 让流水线继续推进；只有最后一个 chunk（或生成 token）才回 logits。

#### 4.2.2 核心流程

**prefill 流水线**（多 chunk，coordinator A → worker B → worker C → 回 A）：

```text
coordinator A 本地：       算 chunk0 本地段  算 chunk1 本地段  算 chunk2 ...
                                │                │              │
发送线程 (depth=2)         ──→ 发 chunk0    ──→ 发 chunk1  ──→ 发 chunk2 ...
                                                    (chunk0 与 chunk1 在链路上重叠)
worker B：                       收 chunk0 算    收 chunk1 算    ...
                                    │               │
worker C（末跳, output）：        算→产 logits    算→产 logits
                                    │ ACK/logits     │
结果读取线程 (A)              ←── 收 chunk0 结果 ←── 收 chunk1 结果 ...
                             (用 expected_hashes 逐 chunk 校验 result_hash)
flow window 控制：在飞 chunk 数 ≤ window
```

流水线的本质：coordinator 的「生产」（算本地段 + 发送）与 worker 的「消费」（算远程段）**重叠**。chunk N 在 worker 上算的同时，coordinator 已经在算 chunk N+1 的本地段并准备发送。

**生成自回归**（每个 token 串行走完整条 route）：

```text
对每个要生成的 token：
  coordinator A 算本地段（1 个 token）→ 发 hidden state 给 B
  worker B 算自己的层段 → 转发给 C
  worker C 算完 → 回送 logits 给 A
  coordinator A 采样 → 得到下一个 token
（token N+1 必须等 token N 的 logits 回来才能开始）
```

每 token 至少一整圈跨机往返——这就是生成慢的原因，也是高 ping 链路（WiFi/VPN）对生成伤害最大的原因（README 的 Network Link Comparison 表）。

#### 4.2.3 源码精读

**何时走流水线？** `dist_coordinator_can_pipeline_prefill` 是个简洁的门：

[ds4_distributed.c:3421-3439](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L3421-L3439) —— 启用条件：未被 `DS4_DIST_DISABLE_PREFILL_PIPELINE` 关掉、token 数超过一个 chunk（`n_tokens > chunk_cap`，即至少两个 chunk 才值得流水线）、有 worker、首跳连接活着、末跳要么持 output head、要么回送 hidden 给能算 output head 的 coordinator。**只有一个 chunk 的 prefill 或单个 token 的 decode 不走流水线**，落到下文的 `eval_span`。

**expected_hashes：逐 chunk 的滚动指纹。** 流水线启动前，coordinator 预先把每个 chunk 边界处的「token-prefix hash」算好存进 `reader.expected_hashes`：

[ds4_distributed.c:3585-3602](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L3585-L3602) —— 从 `span_start` 起用滚动 hash 累加每个 chunk 的 token，得到该 chunk 结束后的 prefix hash。结果读取线程随后用它逐 chunk 校验返回的 `result_hash`（见 4.3.3 的 L3375），任何一 chunk 漂移都能立刻发现。

**流水线主循环：生产 + 发送 + 流窗。** 真正的重叠发生在这里：

[ds4_distributed.c:3636-3689](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L3636-L3689) —— 循环按 `chunk_cap` 切片。每一轮：先 `dist_prefill_reader_wait_flow_window` 等到「在飞 chunk 数 < window」（L3644，这是流水线深度闸门）；算 coordinator 本地段（`ds4_session_eval_layer_slice`，L3669）；把 hidden state 装进一个 sender slot（`prefix_hash/result_hash` 已预算，L3689）；sender 线程异步把它发给首跳 worker。与此同时，另一条 reader 线程在后台收末跳的 RESULT。生产与消费因此并行。

**发送深度。** coordinator 能提前备几个 chunk：

[ds4_distributed.c:468-481](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L468-L481) —— 默认 `depth=2`，可被环境变量 `DS4_DIST_PREFILL_SEND_DEPTH` 覆盖到 1–8，且不超过 chunk 总数。这是 sender 的 slot 数。

**生成（与单 chunk prefill）走 eval_span。** 每个 token 整条 route 串行：

[ds4_distributed.c:2670-2766](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L2670-L2766) —— `dist_coordinator_eval_span` 先算 coordinator 本地段（L2733），若 `plan->count != 0` 则把 hidden state 通过 `dist_coordinator_eval_remote_on_fd` 发给首跳并**阻塞等**末跳回 logits（L2749）。注意它一次性算 `n_tokens` 个 token：生成时 `n_tokens=1`（见 `ds4_dist_session_eval` L5655-L5666 传 `&token, 1`），所以每 token 一圈往返；小 suffix prefill 时 `n_tokens` 可能是一小段，但只要 ≤ 一个 chunk 就走这条串行路径。

**两个 session 入口如何分流。** `ds4_dist_session_sync`（prefill）与 `ds4_dist_session_eval`（decode）是 coordinator 侧对前端的统一入口：

[ds4_distributed.c:5504-5604](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L5504-L5604) —— `sync` 里：若 `checkpoint` 是 prompt 的前缀且后缀够大（`can_pipeline_prefill`），走流水线 `prefill_prompt_pipelined`（L5532）；否则按 chunk 串行调 `eval_span`（L5570）。无论哪条路失败，都进 `rebuild_from_transcript` 恢复（L5546、L5583）。

[ds4_distributed.c:5637-5689](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L5637-L5689) —— `eval` 只处理单个 token（先把 token 追加进一份 transcript 副本，L5651-L5653），调 `eval_span` 走完整条 route，失败同样进 `rebuild_from_transcript`（用含新 token 的 transcript 重放）。

#### 4.2.4 代码实践

**实践目标**：理解为什么「`--dist-prefill-window` 与 `--dist-prefill-chunk` 是路径旋钮而非纯性能旋钮」，以及它们和 u6-l1 的 chunk 边界约束如何呼应。

**操作步骤**：

1. 阅读 README 对这两个旋钮的说明：[README.md:433-437](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L433-L437)。
2. 在 `ds4_distributed.c` 里找到 `dist_coordinator_prefill_chunk_cap`（L3441），看清 chunk 大小的来源优先级：`--dist-prefill-chunk` 实参 > 环境变量 `DS4_DIST_PREFILL_CHUNK` > 默认。
3. 回顾 u6-l1 的结论：单机 chunk 边界同时是 KV checkpoint 推进点、logit 写出点与磁盘冷存对齐点；复现官方向量必须把 chunk 钉死在 2048。
4. 推演：若把 `--dist-prefill-chunk` 从 4096 改成 2048，每个 chunk 在 worker 上最终化压缩行的时间表会变，`expected_hashes` 的边界也会变。

**需要观察的现象**：`chunk_cap` 同时决定 (a) 流水线切片粒度、(b) 滚动 hash 的边界、(c) KV 压缩行最终化时机。

**预期结果**：你能解释「为什么改 chunk 不只是改性能——它改变了 KV 状态在每一步的精确落点」。这与 u6-l1 的「chunk 是路径旋钮」结论在分布式下依然成立，只是边界现在跨了机。

> 待本地验证：分布式实跑需多机，本实践为源码阅读型。

#### 4.2.5 小练习与答案

**Q1**：生成阶段为什么不能用 prefill 那种流水线？

**答**：因为生成严格自回归——token N+1 的输入依赖 token N 采样得到的 token id，而采样需要 token N 的 logits，logits 要等整条 route 算完才有。每 token 至少一圈跨机往返，无法重叠。

**Q2**：`can_pipeline_prefill` 为什么要求 `n_tokens > chunk_cap`？

**答**：流水线的收益来自「chunk N 与 chunk N+1 重叠」。若总 token 数 ≤ 一个 chunk，根本没有第二个 chunk 可重叠，流水线退化为串行，反而多了线程与流窗的开销，所以直接走 `eval_span`。

**Q3**：`ACK_ONLY` 标志解决了什么问题？

**答**：prefill 的非末 chunk 不需要 logits（只有最后一个 chunk 才要采样出首个生成 token），但流水线需要每个 chunk 都有一个「完成回执」才能推进流窗。`ACK_ONLY` 让中间 chunk 的末跳只回一个轻量 ACK（`RESULT_ACK`）而非完整 logits，既推进流水线又不浪费带宽。

---

### 4.3 滚动 hash 校验与故障恢复

#### 4.3.1 概念说明

分布式把 KV 缓存**分散**在多机上：每个 worker 只有自己的层片 KV。这带来一个隐患——**KV 状态不一致**。比如：

- 一个 worker 重启了，它的 KV 归零，但 coordinator 以为它还在「位置 N」。
- 网络抖动导致某个 chunk 的 `WORK` 帧没到，worker 算了别的，KV 停在错误位置。
- 路由里换了一个 worker，新 worker 的 KV 与旧 worker 完全无关。

如果不去检测，worker 会基于错误的 KV 继续算，产出**静默错误**的 logits——这是最坏情况。ds4 用两道防线：

**第一道：滚动 token-prefix hash。** 给每个 worker 维护一个 64 位「KV 状态指纹」——就是「当前 KV 对应的 token 序列」的 hash。coordinator 每发一个 `WORK` 帧，都把「本 span 之前」和「本 span 之后」的 token-prefix hash 写进帧（`prefix_hash` / `result_hash`）。worker 干活前先核对：自己的指纹是否等于帧里的 `prefix_hash`；不等就**拒绝干活**并报错。这样「重启后停在位置 0 的 worker」绝不可能悄悄接受「位置 N」的工作。

这个 hash 用的是 **FNV-1a**（不是密码学 hash，只是一个紧凑的不变量），对 token id 的小端 4 字节做 `xor-then-multiply`：

\[ h_0 = 14695981039346656037_{10} \quad(\text{offset basis}) \]
\[ h_{k} = (h_{k-1} \oplus \mathrm{byte}_k) \times 1099511628211_{10} \quad(\text{prime}) \]

每个 token 贡献 4 个字节（小端），故一个 token 的更新是连续 4 次 `xor-multiply`。它的好处是**可滚动累加**：已知前 `n` 个 token 的 hash，再加一段 token 就能得到前 `n+m` 个 token 的 hash，无需从头算——这正是 `prefix_hash → result_hash` 能随帧下发的数学基础。

**第二道：两类故障，两种恢复。** worker 报错分两种根因，恢复策略截然不同：

| 故障类型 | 根因 | 错误码 | 恢复策略 |
| --- | --- | --- | --- |
| 传输失败 | socket 断了、对端进程没了 | `DS4_DIST_RECV_TRANSPORT_ERROR=1` | **丢路由**：把失联 worker 从注册表摘掉，等一个兼容 worker 重连重组路由，再重放 |
| 逻辑失败 | KV/hash 不匹配、层段不符、model id 不符 | `DS4_DIST_RECV_REMOTE_ERROR=2` | **保路由**：worker 还活着，只是 KV 状态错了，原地用 token 历史**重放**重建 KV |

两种情况最后都会调 `rebuild_from_transcript`——把当前完整的 token 转录本（transcript）重新 prefill 一遍。区别只在「重放前要不要先重组路由」。

#### 4.3.2 核心流程

worker 端每个 `WORK` 的处理（精简）：

```text
收 WORK 帧
  ├─ 若 flags & RESET_SESSION：重置本地 KV 切片，token_hash = INIT（全新开始）
  ├─ 否则若 token_hash 无效：从本地 token 时间线重算 token_hash
  ├─ 若 token_hash != work.prefix_hash：
  │      报错 "worker KV prefix hash mismatch"（REMOTE_ERROR）→ 触发重放
  ├─ eval 本地层段
  └─ 成功：token_hash = work.result_hash（指纹前移）
     失败：token_hash_valid = false（下次必须重算/拒绝）
```

coordinator 端收到一次 eval/prefill 的返回码后：

```text
rc = eval_span(...) 或 prefill_pipeline(...)
if rc != 0:
    forget_route = (rc != DS4_DIST_RECV_REMOTE_ERROR)
    # 传输失败(1) → forget_route=true：摘掉坏 worker、重组路由
    # 逻辑失败(2) → forget_route=false：路由没坏，只重放
    rebuild_from_transcript(transcript, forget_route):
        if forget_route: forget_route_workers(plan); rebuild plan
        prefill_prompt(整段 transcript)   # 用 RESET_SESSION 从头重建所有 worker 的 KV
```

#### 4.3.3 源码精读

**滚动 hash 实现。** FNV-1a 常量与更新函数：

[ds4_distributed.c:1489-1502](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L1489-L1502) —— 注释明确说「这不是安全原语，只是一个紧凑的会话不变量，让分布式 worker 能在干活前拒绝同位置但不同前缀的 KV 状态」。`offset basis` 与 `prime` 是标准 FNV-1a 64 位常量；每个 token 拆成 4 字节小端逐字节混入。前缀 hash 的便捷计算见 [ds4_distributed.c:1509-1527](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L1509-L1527)。

**worker 端的 hash 闸门。** 这是「重启 worker 不能静默接受旧位置工作」的核心防线：

[ds4_distributed.c:7444-7476](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L7444-L7476) —— 三段逻辑：① 若 `RESET_SESSION` 标志置位（coordinator 主动要求从零开始），重置本地层 KV 切片并把 `token_hash` 归零（L7454）；② 否则若本地 `token_hash` 失效，从本地 token 时间线现算一遍（L7456-L7467）；③ 核对 `session->token_hash != work_prefix_hash` 就报 `"worker KV prefix hash mismatch"` 并返回（L7469-L7475）——这是一个**逻辑错误**（走 REMOTE_ERROR 路径，触发保路由重放）。成功 eval 后指纹前移到 `result_hash`（见 [ds4_distributed.c:7491-7496](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L7491-L7496)），失败则置 `token_hash_valid=false`，逼下次重算或拒绝。

**收包侧的错误分类。** coordinator 收 `RESULT` 时如何区分两类故障：

[ds4_distributed.c:2450-2461](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L2450-L2461) —— `result.status != 0`（worker 在帧里显式报了错）就返回 `DS4_DIST_RECV_REMOTE_ERROR`（2）；而更早的 `dist_read_full` 返回 0（socket 读失败，见 L2442-L2446）返回普通 `1`，即 `TRANSPORT_ERROR`。这两条返回值决定了恢复分支。

**恢复总入口。** `dist_coordinator_rebuild_from_transcript`：

[ds4_distributed.c:2948-2987](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L2948-L2987) —— 日志区分两种触发（`route failure` vs `KV mismatch`，L2960-L2963）。若 `forget_route` 为真（传输失败）：调 `dist_coordinator_forget_route_workers` 摘掉坏 worker、释放旧 plan、`ensure_route` 等待并重建一条新路由（L2964-L2969）；若为假（逻辑失败）但 plan 空，也尝试补路由（L2970-L2973）。最后无论如何都用 `prefill_prompt` 把**整段 transcript** 重放一遍（L2975-L2985）——重放时第一个 chunk 会带 `RESET_SESSION`，让所有 worker 从零重建 KV。

**「丢路由」的实现。** `dist_coordinator_forget_route_workers`：

[ds4_distributed.c:2102-2133](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L2102-L2133) —— 遍历当前 plan 的每一跳，在注册表里按 `(host, port, layer_start/end, has_output)` 精确匹配，摘掉并关闭 fd，`generation++` 让缓存的路由失效。摘掉的 worker 必须等它（或一个兼容者）重新发 `HELLO` 才能回到路由——这就是 README 说的「drops the route and waits for a replacement worker」。

**调用点的统一模式。** 前文 4.2.3 的 `sync`/`eval` 里，每次失败都遵循同一个模式，例如：

[ds4_distributed.c:5546-5562](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L5546-L5562) —— `forget_route` 实参取 `prefill_rc != DS4_DIST_RECV_REMOTE_ERROR`。即：只有「逻辑错误」保路由，其余（传输错误、本地错误）都丢路由。`eval` 里的对应调用见 [ds4_distributed.c:5668-5686](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L5668-L5686)。

**README 的恢复条款。** 这段话浓缩了上述全部机制，值得逐句对照源码：

[README.md:449-462](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L449-L462) —— 「worker disconnects → removes from active route」「later calls report an incomplete route until a compatible worker reconnects」「keeps the token history and can rebuild worker KV state by replaying the prefix」「rolling 64-bit token-prefix hash … a restarted worker at position 0 cannot silently accept work for position N」「Ctrl+C … cooperative … waits for the current distributed token or prefill chunk to drain」。最后一句点出协作式中断：coordinator 不会在一个 chunk 中途把 worker 的 KV 切成两半，而是等当前在飞的 token/chunk 排空再交还控制权。

#### 4.3.4 代码实践

**实践目标**：亲眼追踪一次「worker 重启后旧位置工作被拒」的判定路径。

**操作步骤**：

1. 在 `ds4_distributed.c` 定位三处：`dist_token_hash_update`（L1495）、worker hash 闸门（L7444-L7476）、`rebuild_from_transcript`（L2948）。
2. 假设 worker B 已算到位置 1000（`token_hash` = H(前 1000 token)），随后 B 进程被 kill 并重启。重启后 B 的 `token_hash_valid = false`、本地 KV 为空。
3. coordinator 此刻发来一个 `pos0=1000` 的 `WORK`（`prefix_hash` = H(前 1000 token)），**不带** `RESET_SESSION`。
4. 推演 B 的执行：`token_hash_valid==false` → 从（空的）本地时间线现算 hash → 得到的 hash 与 `work.prefix_hash` 不等 → 报 `"worker KV prefix hash mismatch"`。
5. 推演 coordinator：收到 `REMOTE_ERROR` → `forget_route=false` → 不摘 B → `rebuild_from_transcript` 用整段 transcript 重放，第一个 chunk 带 `RESET_SESSION` → B 这次 `token_hash` 归零、从位置 0 正确重建 KV。

**需要观察的现象**：mismatch 报错是**逻辑错误**而非传输错误；恢复时不丢路由，而是原地重放。

**预期结果**：你能说清「为什么重启 worker 不会污染输出」——因为 hash 闸门先于任何层计算拦截了它。

> 待本地验证：多机实跑可选；本实践为源码阅读型，重在推演 hash 比较 `session->token_hash != work_prefix_hash` 这一行（L7469）的后果。

#### 4.3.5 小练习与答案

**Q1**：为什么用 FNV-1a 而不是 SHA-256？

**答**：因为这里不需要抗碰撞的安全性，只需要一个「KV 状态变了指纹就大概率变」的紧凑不变量。FNV-1a 是 64 位、可滚动累加、计算极快，正好满足「随每个 `WORK` 帧预算 `prefix_hash/result_hash`」的需求；SHA-256 既慢又不便滚动累加。源码注释（L1489-L1491）明确点出「not a security primitive」。

**Q2**：传输失败与逻辑失败的恢复，哪个更便宜？为什么？

**答**：逻辑失败（KV mismatch）更便宜——路由没坏，worker 还活着，只需 `RESET_SESSION` 重放 transcript 重建 KV。传输失败更贵——要先丢 worker、等兼容 worker 重连（可能要等很久）、重组路由，然后还是得重放。

**Q3**：`RESET_SESSION` 标志为什么必须存在？没有它会怎样？

**答**：重放恢复时，worker 的 KV 处于「错误的前缀状态」，必须先清空才能从位置 0 重建。`RESET_SESSION` 让 worker 调 `ds4_session_layer_slice_reset` 并把 `token_hash` 归零（L7444-L7455），保证后续 `prefix_hash`（= INIT）能与 worker 状态匹配。没有它，重放的第一个 chunk 又会触发 hash mismatch，陷入死循环。

---

## 5. 综合实践

把三个模块串起来，完成本讲规格里要求的那张图与那段分析。

**任务**：阅读 [README.md:464-485](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L464-L485)（Distributed protocol overview）与 `ds4_distributed.c`，画出一个 prefill chunk 在三机链路 `A（coordinator）→ B（中间 worker）→ C（末跳 worker，持 output head）→ A` 上的完整帧流转，并说明 worker B 掉线后 coordinator 如何重建 KV。

**建议步骤**：

1. **画帧流图**。一个 prefill chunk（`pos0`、`n_tokens` 个 token）的生命周期：
   - A 本地：`ds4_session_eval_layer_slice` 算自己的层段，产出 hidden state。
   - A → B：发 `WORK` 帧（含 `prefix_hash/result_hash`、`route_blob`、hidden state、`ACK_ONLY` 或 `OUTPUT_LOGITS`）。
   - B：核对 `token_hash == prefix_hash` → 算自己的层段 → 从 `route_blob` 读出「下一跳 C」→ 把 `RESULT`（hidden state）直接发给 C（不经 A）。
   - C：算完末层 + output head → 产 logits → `RESULT`（`result_kind=LOGITS`）发回 A。
   - A 的结果读取线程：核对 `result_hash == expected_hashes[i]`，取出 logits。
2. **标注流水线重叠**。在同一张图上画出 chunk N 在 B/C 上算的同时，A 已经在算 chunk N+1 的本地段——这是 prefill 加速的来源。指出 `flow window` 控制着「同时在飞」的 chunk 数。
3. **分析 B 掉线**。设 B 在算 chunk N 时进程崩溃：
   - A 收到 socket 失败 → `DS4_DIST_RECV_TRANSPORT_ERROR`（L2450 之前的 `dist_read_full` 路径）。
   - `eval`/`sync` 调 `rebuild_from_transcript(forget_route=true)`（L5668 / L5546）。
   - `forget_route_workers` 把 B 从注册表摘掉、`generation++`（L2102-L2133）。
   - 后续调用 `route_ready` 返回 0（路由不完整），`ds4-bench` 等会等待，直到一个 `layer_start/end` 兼容的 worker 重新 `HELLO`（比如 B 重启后重新注册）。
   - 路由补全后，`rebuild_from_transcript` 用整段 transcript 重放（`RESET_SESSION`），A、B、C 全部从位置 0 重建 KV，hash 重新对齐，恢复继续生成。
4. **对比 C 掉线**：C 是末跳，掉线后 output head 没人算；若 coordinator 配的是 `--layers 0:42`（自己能算 output head，`local_can_output_head`），可以由 coordinator 收回 hidden state 自己算 logits；否则必须等一个新的持 output head 的 worker。把这个区别写进你的分析。

**预期产物**：一张帧流图（含流水线重叠标注）+ 一段「B 掉线后的恢复时序」文字。重点是说清「丢路由（传输失败）」与「保路由重放（逻辑失败）」两条路径的分工。

> 待本地验证：若有多台可信机器，可故意 `kill` 一个 worker 观察 coordinator 的 `--debug` 日志（会看到 `forgot failed route worker` 与随后的 `route incomplete` / `complete route ready`）。无机器时为源码阅读型实践。

## 6. 本讲小结

- ds4 分布式用**两种连接**：控制连接跑 `HELLO` 注册，数据连接跑 `WORK/RESULT`。重数据只走数据连接，控制连接只维护注册表。
- coordinator 用递归回溯把若干 worker 的层段**首尾相接**拼成一条覆盖全部层、output head 有归属的 route；`route_blob` 随每个 `WORK` 帧下发，使中间 worker 能**直接转发给下一跳**，coordinator 不中转。
- **prefill 可流水线加速**：coordinator 算 chunk N+1 的同时 worker 算 chunk N，由 `flow window` 控制在飞 chunk 数、`expected_hashes` 逐 chunk 校验；**生成严格自回归**，每 token 一圈跨机往返，必然慢于单机。
- 滚动 **FNV-1a token-prefix hash** 是 KV 状态指纹，worker 干活前先核对，使「重启后停在位置 0 的 worker」绝不可能静默接受「位置 N」的工作。
- 故障分两类：**传输失败丢路由**（等兼容 worker 重连重组），**逻辑失败保路由**（原地重放 transcript）；两者最后都用 `RESET_SESSION` 重放整段 transcript 重建 KV。
- 协议当前**无加密无鉴权、非 release-stable**，必须同 commit 构建、跑在可信机器与可信网络上；协作式 Ctrl+C 会让当前 token/chunk 排空再交还控制，避免把 worker KV 切成两半。

## 7. 下一步学习建议

- **持久化与拓扑无关性**：本讲的快照帧（`SNAPSHOT_*`）只一笔带过。下一站读 `ds4_dist_session_save_payload/load_payload`（ds4_distributed.c）配合 u8-l3 的 DSV4 格式，理解「save 时 coordinator 把各 worker 的层片张量汇流成一个普通 payload，load 时再按当前路由切回去分发，磁盘文件不保留分布式拓扑」。
- **回到单机 chunk 约束**：本讲反复出现「chunk 是路径旋钮」。若尚未读透，回头精读 u6-l1，理解 chunk 边界为何同时是 KV checkpoint 推进点、logit 写出点与磁盘冷存对齐点——分布式下这一切只是跨了机。
- **评测与基准**：u10-l4 / u10-l5 讲 `ds4-eval` 与 `ds4-bench`，它们都接入了 `--role` 分布式选项。可结合本讲，理解为什么 `ds4-bench` 要「等路由就绪」、为什么分布式行专测 prefill 吞吐。
- **想贡献分布式代码**：先读 `AGENT.md` 与 `CONTRIBUTING.md`，分布式属于受保护的「四大路径」之一，任何改动都要跑官方向量回归（见 u11-l4）。
