# 分布式推理架构与层切分

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 ds4 分布式推理里 **coordinator（协调者）** 与 **worker（工作者）** 两种角色的职责边界，以及为什么 coordinator 必须拥有第 0 层。
- 解释 `--layers A:B` 与 `--layers A:output` 这两种写法的区别，理解 **output head（输出头）归谁所有** 这条核心设计取舍，以及慢链路上 `--layers 20:42` 为何存在。
- 把「层切分」这条线索从命令行参数一路追到 u3-l2 讲过的 `weights_bind`：明白同一份权重绑定代码如何只绑定、只 mmap 映射本进程负责的那一段层，从而让一个放不进单机的模型被拆到多台机器上。

本讲只讲**架构与切分**（谁负责什么、层怎么分、output head 归谁），不展开协议帧（HELLO/WORK/RESULT）、流水线与 hash 校验——那是下一讲 u9-l4 的内容。

## 2. 前置知识

- **Transformer 是一层一层堆起来的**。DeepSeek V4 一共 43 层（见 u4-l1）。一个 token 从 embedding 出发，依次穿过第 0 层、第 1 层……直到最后一层，最后一层的输出再过一个 **output head（也叫 lm_head，本质是一个大矩阵）** 投影成「词表上每个词的得分」即 logits，最后采样挑出下一个 token。
- **层与层之间流动的是 hidden state（隐藏状态）**。在本讲里你可以把它理解为一个定长向量：层 N 的输出 = 层 N+1 的输入。
- **权重绑定（weights binding）**：u3-l2 讲过，ds4 打开引擎时会做一次 `weights_bind`，把 GGUF 里按字符串命名的张量填进 `ds4_layer_weights` 这张语义指针表。本讲的关键是：这张表可以**只填一段层**。
- **mmap 零拷贝**：u3-l1 讲过，权重不拷进内存，而是用 mmap 映射，按需页入。本讲里，每个进程只 mmap 映射自己那一段层的张量区间，这就是「拆开放进多台机器」的内存来源。
- **prefill 与 decode**：u1-l5/ u6-l1 讲过，prefill 是一次性填一段 prompt 进 KV 缓存，decode 是逐 token 自回归生成。

一个直觉：分布式推理 = **把 43 层像流水线一样切成几段，每段放在一台机器上，机器之间用 TCP 传递 hidden state**。coordinator 是你直接对话的那台机器，它负责分词、采样、拥有最前面一段层；worker 负责中间和后面的一段段层。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `ds4_distributed.h` | 分布式子系统对外的**窄头**：类型与函数声明 | 角色枚举、`ds4_dist_session` 声明、「分布式是引擎后端」的设计声明 |
| `ds4_distributed.c` | 分布式的全部实现（约 8400 行） | 角色分发、HELLO 注册校验、路由组建、`--layers` 解析、把切分翻译给引擎 |
| `ds4.h` | 引擎公共边界 | `ds4_distributed_role` / `ds4_distributed_layers` / `ds4_distributed_options`，以及 `ds4_engine_options` 里的 `load_slice` 字段族 |
| `ds4.c` | 引擎核心 | `weights_bind` 如何按切片只绑定一段层、`ds4_engine_open` 如何把 `--layers` 翻译成 `load_slice` |
| `README.md` | 用户向文档 | 「Distributed Inference」章节的两机配置示例与链路取舍 |

> 提醒：分布式是**编译进 CORE_OBJS 的引擎后端**，不是一个独立前端。`ds4`、`ds4-server`、`ds4-agent`、`ds4-eval`、`ds4-bench` 都能通过 `--role`/`--layers` 进入选式（见 u1-l3 的源码地图）。

## 4. 核心概念与源码讲解

### 4.1 角色与 dist_session：coordinator 与 worker 的职责划分

#### 4.1.1 概念说明

ds4 的分布式只有两种角色：

- **coordinator（协调者）**：你直接对话的那台机器。它拥有**分词器、prompt、采样器**，并且**必须拥有从第 0 层开始的一段层**（即负责 token embedding 与最前面几层）。它是请求的入口与出口。
- **worker（工作者）**：连接到 coordinator 的机器。它只负责**中间或末尾的一段层**，每台 worker 自己持有**自己那段层的 KV 缓存切片**。

一个关键设计声明写在头文件最上方：**「分布式推理是一个引擎后端，而不是一套独立的前端 API」**。也就是说，CLI / 服务器 / agent 这些前端调用的仍然是普通的 `ds4_session_*` 接口；coordinator 一侧用一个 `ds4_dist_session` 把这些调用**沿路由分发**到各 worker，对前端透明。只有 `--role worker` 和一次性 `--role coordinator -p ...` 这两种「服务模式」才会直接调 `ds4_dist_run()`。

> 见 [ds4_distributed.h:10-15](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.h#L10-L15) —— 这段注释是理解整个子系统定位的钥匙：前端 API 不变，分布式藏在 session 后端里。

为什么 coordinator 必须拥有第 0 层？因为 **prompt 的 token 序列只在 coordinator 手里**：第 0 层之前要把 token id 查表变成 embedding 向量（`token_embd`），这件事天然只能由「拿着 prompt、拿着分词器」的 coordinator 做。代码里这条约束被显式校验（见 4.2.3）。

#### 4.1.2 核心流程

一次分布式推理的高层流程：

```text
[worker 启动] --connect--> [coordinator]
   发送 HELLO：报告自己的 layer_start/layer_end、是否带 output head、
              model_id、量化档、ctx 容量、数据端口

[coordinator 收集 HELLO] --> 组建一条覆盖 0..output 的「路由」(route)
   路由就绪 = coordinator 的 0..local_end + 一串 worker 的区间，
              首尾相接覆盖到最后一层 + output head

[前端发起一次 sync/eval]
   coordinator 算自己那段层(0..K-1) -> 把 hidden state 发给第一个 worker
   worker1 算自己那段(K..L)        -> 直接转发给 worker2（不经 coordinator 中转）
   worker2 算自己那段(L..end)       -> 产出 logits 回传给 coordinator
   coordinator 采样 -> 得到下一个 token
```

两个要点：

1. **worker 之间直连转发，coordinator 不当中转站**。README 形象地说：若 coordinator 是 A，请求一来，激活流走 `A -> B -> C -> 回到 A`。这样 hidden state 不必两次穿过 coordinator 的网络。
2. **每个 worker 持有自己的 KV 切片**。KV 缓存不是集中存放，而是「谁算那几层，谁就存那几层的 KV」。

数据结构上有两组对称的状态：

- **coordinator 一侧**：`ds4_dist_coordinator_state` 持有 worker 注册表（链表 `workers`）、本机负责的层区间 `local_start/local_end`、以及「本机能否算 output head」的 `local_can_output_head` 标志。
- **worker 一侧**：`ds4_dist_worker_state` 持有按 session id 索引的 KV 会话链表 `sessions`（`ds4_dist_worker_session`），让多个独立调用者不会共享同一条 token 时间线。

整个 coordinator 会话对象 `ds4_dist_session` 把上面这些连同监听 socket、accept 线程、路由计划包在一起。

#### 4.1.3 源码精读

**角色枚举与配置包**定义在引擎公共头里，前端、引擎、分布式三方共用同一份：

[ds4.h:67-92](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L67-L92) —— 定义了 `ds4_distributed_role`（NONE/COORDINATOR/WORKER）、`ds4_distributed_layers`（一段层的 start/end/has_output）、以及聚合它们的 `ds4_distributed_options`（含监听地址、coordinator 地址、prefill chunk/window、activation 位宽等）。

**coordinator 的运行时状态**与**worker 注册表条目**：

[ds4_distributed.c:208-244](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L208-L244) —— `ds4_dist_worker_entry` 是一台已注册 worker 的「名片」（fd、对端地址、model_id、量化档、layer_start/layer_end、has_output/has_hidden、ctx_size、n_layers、数据端口 listen_port）；`ds4_dist_coordinator_state` 是 coordinator 的核心：本机层区间 `local_start/local_end`、`local_has_output`、`local_can_output_head`、prefill/activation 参数、互斥锁 `mu`、worker 链表 `workers`。

[ds4_distributed.c:258-276](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L258-L276) —— `ds4_dist_worker_session`（worker 侧每条会话：session_id、滚动 token 前缀 hash、绑定的 `ds4_session`）与 `ds4_dist_worker_state`（worker 侧全局：本机层区间、是否带 output、监听 fd、会话链表）。

**coordinator 会话对象**本身：

[ds4_distributed.c:386-398](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L386-L398) —— `struct ds4_dist_session` 内嵌 `ds4_dist_coordinator_state state`，加上监听 fd、accept 线程、当前路由计划 `plan`、`plan_ready`、session/request 计数器。注意它把 coordinator 状态**内联**进来，而非指针持有——coordinator 会话就是 coordinator 状态的容器。

**创建 coordinator 会话**时把本机层区间固化下来：

[ds4_distributed.c:5389-5427](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L5389-L5427) —— `ds4_dist_session_create` 要求 `role == COORDINATOR`，开监听 socket，然后填 `local_start = opt->layers.start`、`local_end = dist_resolved_layer_end(opt, n_layers)`、`local_has_output = opt->layers.has_output`，并把 `local_can_output_head` 设成 `ds4_engine_has_output_head(engine)`（本机是否真的加载了 output head，见 4.3）。最后 detach 一个 accept 线程专门接 worker 的 HELLO。

**角色分发入口**：

[ds4_distributed.c:8391-8413](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L8391-L8413) —— `ds4_dist_run` 先做选项与「层区间对该模型是否合法」的校验，屏蔽 SIGPIPE，再按 `role` 分发到 `dist_run_coordinator`（一次性 `-p` 模式）或 `dist_run_worker`（worker 常驻循环）。注意 worker 把 `ctx_size` 透传进去，因为它需要按 coordinator 协商的容量分配 KV。

#### 4.1.4 代码实践

**实践目标**：用结构体字段印证「coordinator 持注册表、worker 持 KV 切片、coordinator 必须拥有第 0 层」这三句话。

**操作步骤**：

1. 打开 [ds4_distributed.c:208-244](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L208-L244)，在 `ds4_dist_coordinator_state` 里找到 `workers` 链表字段，确认「worker 注册表只存在于 coordinator 一侧」。
2. 打开 [ds4_distributed.c:258-276](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L258-L276)，在 `ds4_dist_worker_state` 里找到 `sessions` 链表，确认「每条会话的 KV（`ds4_session *session`）存在 worker 一侧」。
3. 打开 [ds4_distributed.c:8368-8389](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L8368-L8389) 的 `dist_validate_layers_for_model`，找到那条「coordinator 的 layer range 必须从 0 开始」的校验（`opt->layers.start != 0` 即报错）。

**需要观察的现象**：coordinator 状态里**没有**「按 session 存的 KV」字段，而 worker 状态里**没有**「worker 注册表」字段——两组状态严格对称、各管各的。

**预期结果**：你能用一句话说清——「coordinator 是路由的组建者与采样的执行者，worker 是无状态的层计算服务（相对 coordinator 而言），KV 切片分散在各 worker 上」。

> 本实践为源码阅读型，无需运行；若想看运行时效果，可在 coordinator 上加 `--debug`，它会打印路由组建结果（见 4.2.4）。

#### 4.1.5 小练习与答案

**练习 1**：如果 coordinator 不拥有第 0 层（比如 `--layers 5:10`），系统会怎样？

**参考答案**：会被 `dist_validate_layers_for_model` 拒绝（coordinator 的 layer range 必须从 0 开始）。即使绕过校验也无法工作：prompt 的 token id 必须先经 `token_embd` 查表变成 embedding 才能进入第 0 层，而 token 序列与分词器只在 coordinator 手里。

**练习 2**：为什么 worker 之间要「直连转发」，而不是让 coordinator 当中转站？

**参考答案**：hidden state 在 `A -> B -> C -> A` 的链路上，若由 coordinator 中转，每个 hop 都要绕回 A，A 的网络与 CPU 成为瓶颈，延迟翻倍。直连让数据走最短路径，coordinator 只在首尾参与。

---

### 4.2 --layers 区间与 output head 归属

#### 4.2.1 概念说明

`--layers` 选定本机负责的层区间，有两种写法：

- `--layers A:B`：**闭区间**，负责第 A 层到第 B 层（含两端）。例如 `10:20` = 第 10..20 层。
- `--layers A:output`：从第 A 层一直到**最后一层 + output head**。`output` 这个关键字表示「把输出头也一起接管」。

> 见 [README.md:287-288](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L287-L288) —— 官方对区间语义的说明：`10:20` 含两端，`N:output` 含末层加输出头。

**output head 归谁所有**是本讲最核心的设计取舍。output head（lm_head）把最后一层的 hidden state 投影成词表上的 logits。它必然存在某一台机器上。两种典型分配：

| 模式 | 谁拥有 output head | 末层算完后回传什么 | 适用场景 |
| --- | --- | --- | --- |
| `--layers N:output`（**默认/推荐**） | **末位 worker** | logits（直接由 worker 产出） | 正常快速链路 |
| `--layers N:42`（末层但不带 output） | **coordinator** | hidden state（coordinator 本地算 logits） | 极慢/按流量计费的链路 |

为什么默认让末位 worker 拥有 output head？因为这样**「算最后一层」与「算 output head」在同一台机器上完成**，避免把整批 final hidden state 跨网送到别处再做一次矩阵乘。README 原话：「This avoids returning a full final hidden-state batch after prefill and lets the final worker produce the logits directly.」

那为什么又提供 `--layers 20:42` 这种「末层归 worker、output head 归 coordinator」的模式？因为**生成（decode）是逐 token 的、严格自回归**，每个 token 都要跨机一跳。这一跳回传的数据量：

\[ \text{每 token 回传} = \begin{cases} \text{logits：词表维度（十几万）个浮点} & \text{若末位 worker 出 logits}\\ \text{hidden state：隐藏维度（几千）个浮点} & \text{若回传 hidden state} \end{cases} \]

由于**词表维度远大于隐藏维度**，在慢链路上回传 hidden state（让 coordinator 本地算 logits）每个 token 的字节更少，于是 decode 延迟更低。代价是：coordinator 要**加载并持有 output head 权重**、并承担最后的投影计算。这就是 README 说的「trading extra coordinator work for smaller per-token replies」。

> 一句话：`N:output` 优化 prefill（避免大批 hidden state 跨网），是常态；`N:42` 优化慢链路上的 decode（每 token 回传更小），是让步。

#### 4.2.2 核心流程

`--layers` 从字符串到「本机层区间」的解析与校验：

```text
argv: "--layers" "31:output"
   |
   v
dist_parse_layers("31:output")      # 拆冒号；右段=="output" => has_output=true, end=UINT32_MAX
   => ds4_distributed_layers{ start=31, end=UINT32_MAX, has_output=true, set=true }
   |
   v
dist_resolved_layer_end(opt, n_layers)   # 把 "output" 折算成真实末层下标
   => has_output ? (n_layers - 1) : end     # 43 层模型 => 42
   |
   v
coordinator 收到 worker 的 HELLO 时再校验：
   - 区间合法（start<=end< n_layers）
   - 若 has_output，则 layer_end+1 必须等于 n_layers（带 output 头就必须真的到末层）
```

路由组建时，「是否覆盖到 output」决定路由是否就绪。coordinator 先看自己：`local_start==0` 才有资格当起点；再看能否把 `[local_end+1 .. 末层]` 用一串 worker 首尾相接补齐；最后看 output head：末位 worker 带 output 头，**或** coordinator 自己 `local_can_output_head`（即它加载了 output head，对应 `N:42` 模式）。

#### 4.2.3 源码精读

**解析 `--layers`**：

[ds4_distributed.c:8036-8076](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L8036-L8076) —— `dist_parse_layers`：必须有且仅有一个冒号；左段解析成 `start`；右段若等于字符串 `"output"` 则 `has_output=true` 且 `end=UINT32_MAX`，否则解析成数字 `end` 并要求 `end >= start`。`UINT32_MAX` 是「output」的哨兵值，后续靠 `dist_resolved_layer_end` 折算成真实末层。

**把 `output` 折算成真实末层下标**：

[ds4_distributed.c:529-532](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L529-L532) —— `dist_resolved_layer_end`：`has_output` 时返回 `n_layers - 1`，否则返回 `opt->layers.end`。这就是 `31:output` 在 43 层模型上等价于「31..42 + output head」的来源。

**HELLO 注册时的区间校验**（coordinator 侧）：

[ds4_distributed.c:4214-4232](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L4214-L4232) —— 校验 worker 上报的区间合法，并强制「带 output 头就必须到末层」：`has_output && layer_end + 1 != n_layers` 即拒绝。这保证了一个 worker 不能撒谎说「我有 output 头」却其实没算到最后一层。

**coordinator 必须从第 0 层开始**：

[ds4_distributed.c:8368-8389](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L8368-L8389) —— `dist_validate_layers_for_model`：`role == COORDINATOR && layers.start != 0` 即报错「coordinator layer range must start at layer 0」。

**路由组建：覆盖到 output 才算就绪**：

[ds4_distributed.c:1933-1989](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L1933-L1989) —— `dist_worker_route_cmp` 把 worker 按 `layer_start` 升序排（带 output 的优先）；`dist_worker_route_candidate_ok` 判断一个 worker 能否作为下一跳（关键：若 worker 算到末层却**不带** output 头，则要求 coordinator 自己 `local_can_output_head`，否则这个 worker 无法终结路由——这正是 `N:42` 模式得以成立的判定，见第 1950 行）；`dist_route_search_workers` 是一个递归回溯：从 `next`（= 本机 `local_end+1`）出发，找一个 `layer_start==next` 的 worker，递归找它之后的下一跳，直到某 worker `layer_end >= last`（覆盖到末层）即成功。

[ds4_distributed.c:2009-2031](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L2009-L2031) —— 路由就绪判定：`complete = (local_start == 0)`，再递归补齐后续层；`has_output` 取「末位 worker 带 output 头」**或**「coordinator `local_can_output_head`」。第 2057-2068 行还在调试输出里补一句 `-> local output`，标明「末层算完后的 output head 落在 coordinator 本地」——这就是 `--layers 20:42` 在日志里的样子。

**路由就绪的对外查询**：

[ds4_distributed.c:5489-5502](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L5489-L5502) —— `ds4_dist_session_route_ready` 尝试构建一次「探测路由」：建得出来返回 1（就绪），建不出来返回 0（还缺 worker），配置错误返回 -1。`ds4-bench` 就是靠它等到一条完整路由再开跑。

#### 4.2.4 代码实践

**实践目标**：对照两机 PRO Q4 配置示例，解释「为什么末位 worker 通常拥有 output head」，并说清 `--layers 20:42` 在慢链路上的取舍。

**操作步骤**：

1. 阅读 [README.md:363-386](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L363-L386) 的最小两机配置：coordinator `--layers 0:30`、worker `--layers 31:output`。注意 worker 用了 `output` 关键字。
2. 阅读 [README.md:381-386](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L381-L386) 对 `--layers 20:42` 的说明，以及 [README.md:388-406](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L388-L406) 的「Network Link Comparison」表（Thunderbolt 5 / WiFi / Internet 三档链路下 prefill 与 generation 速度的剧烈差异）。
3. 回到代码 [ds4_distributed.c:1944-1952](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L1944-L1952)，看清 `dist_worker_route_candidate_ok` 第 1950 行：`w->layer_end >= last && !w->has_output && !state->local_can_output_head` 时该 worker **不能**终结路由——这正是 `20:42`（末层无 output 头）必须配合「coordinator 自己能出 logits」才能成立的原因。

**需要观察的现象 / 预期结果**：

- 默认配置 worker 写 `31:output`：末位 worker 既算末层又算 output head，logits 在 worker 本地产出，coordinator 直接收 logits 去采样。prefill 时不必把整批 final hidden state 跨网送回。
- `--layers 20:42`：末位 worker 只算到末层（42），不带 output 头；coordinator 加载 output head。每个生成的 token，worker 回传的是 hidden state（几千维）而非 logits（十几万维），在 WiFi/VPN 这种高延迟链路上 decode 显著更快（对照表中 Internet 链路 generation 仅 3.63 t/s，对回传字节极度敏感）。
- 代价：coordinator 要加载并持有 output head 权重，并承担最后那一次投影计算。

> 「待本地验证」：具体每跳回传字节数与时间，可用 coordinator 的 `--debug` 打印的 per-hop telemetry（`input_bytes` / `output_bytes` / `eval_usec` / `downstream_wait_usec`，见 [ds4_distributed.c:141-152](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L141-L152)）在真实链路上比对 `N:output` 与 `N:42` 两种模式。

#### 4.2.5 小练习与答案

**练习 1**：`--layers 31:output` 在一个 43 层（0..42）的模型上，等价于负责哪些层？

**参考答案**：负责第 31..42 层（共 12 层），**外加 output head**。`output` 经 `dist_resolved_layer_end` 折算成 `n_layers - 1 = 42`，`has_output=true` 让该进程同时绑定 lm_head。

**练习 2**：能否让一台 worker 写 `--layers 20:30`（不带 output）却又声称自己是末位 worker？会发生什么？

**参考答案**：HELLO 校验只要求「带 output 头就必须到末层」（[ds4_distributed.c:4221](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L4221)），`20:30` 不带 output 头是合法的 worker 片段。但它在路由组建时**无法终结路由**（`layer_end=30 < last=42`），coordinator 必须再找到覆盖 `31..output` 的后续 worker，否则 `route_ready` 返回 0，路由不完整、推理不开始。

**练习 3**：为什么 `--layers 20:42`（末层归 worker、output 头归 coordinator）对「按流量计费/高延迟」链路更友好，却不是默认推荐？

**参考答案**：decode 逐 token 进行，每个 token 末段都要回传一份数据给 coordinator 采样。回传 hidden state（几千维）比回传 logits（词表十几万维）字节少得多，高延迟链路上每 token 延迟更低。但代价是 coordinator 要持有 output head 权重并做投影，且 prefill 时末段 hidden state 要跨网送到 coordinator——在快速链路（如 Thunderbolt 5）上这些代价不划算，所以默认仍是 `N:output`。

---

### 4.3 层切分如何映射到 weights_bind / load_slice

#### 4.3.1 概念说明

前两模块讲了「谁负责哪段层」的逻辑层；本模块讲它的**物理落地**：一段层区间如何变成「这台机器只绑定、只 mmap 映射自己那一段张量」。

关键洞察：**分布式复用了 u3-l2 的同一份权重绑定代码**。ds4 没有为分布式单独写一套模型加载，而是在 `ds4_engine_open` 之前，把 `--layers` 翻译成引擎选项里的 `load_slice` 字段族，让原本「绑定全部 43 层」的 `weights_bind` 自动收窄成「只绑定 `[start, end]` 这一段」。

四个引擎选项字段（[ds4.h:116-120](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L116-L120)）是这条翻译链的落点：

| 字段 | 含义 |
| --- | --- |
| `load_slice` | 是否启用层切片模式（true = 只绑一段） |
| `load_layer_start` | 起始层（含） |
| `load_layer_end` | 结束层（含）；`UINT32_MAX` 表示「到末层」 |
| `load_output` | 是否绑定 output head |

`UINT32_MAX` 这个哨兵值在两层之间传递「output」语义：解析层把 `"output"` 存成 `end=UINT32_MAX`，引擎打开时再折算成真实末层。

#### 4.3.2 核心流程

```text
命令行:  --layers 31:output
              |
              |  ds4_dist_parse_cli_arg -> dist_parse_layers
              v
ds4_distributed_layers{ start=31, end=UINT32_MAX, has_output=true }
              |
              |  ds4_dist_prepare_engine_options   (模型加载之前调用)
              v
engine_options.load_slice       = true
engine_options.load_layer_start = 31
engine_options.load_layer_end   = UINT32_MAX   (= has_output ? UINT32_MAX : end)
engine_options.load_output      = true
              |
              |  ds4_engine_open  ->  weights_bind(load_slice=true, ...)
              v
weights_bind 的层循环从 start=31 跑到 end=末层；
  - start != 0 => 不要求 token_embd（embedding 在 coordinator 那台）
  - load_output=true => 绑定 lm_head（output head）
GPU model map 也只映射 [31..末层+output] 这几个张量 span
              |
              v
进程只 mmap 了 GGUF 里属于自己那段层的字节 => 内存约为整模型的一段
```

两个推论：

1. **`token_embd`（token embedding 矩阵）只在 coordinator 绑定**（因为只有 coordinator 的 `start==0`）。worker 不需要它——worker 收到的是 hidden state，不是 token id。
2. **mmap 区间跟着切片收窄**。每个进程的物理内存占用 ≈ 自己那段层的权重大小，所以一个 430GB 的 PRO Q4 模型能被两台 512GB 机器各扛一半。这正是 README 说的「each process maps only its own layer slice」。

> 对 coordinator 还有一条特殊待遇：`load_output_optional`。当 coordinator 自己 `--layers 0:30`（不带 output）时，引擎仍**可选地**绑定 output head（如果 GGUF 里有），这样它就具备 `local_can_output_head` 能力，从而支持 `--layers 20:42` 这种「末层归 worker、logits 归 coordinator」的慢链路模式。是否真的绑定了，由 `ds4_engine_has_output_head(engine)` 在运行时回答。

#### 4.3.3 源码精读

**把 `--layers` 翻译成 `load_slice` 字段族**（模型加载**之前**调用）：

[ds4_distributed.c:8346-8366](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L8346-L8366) —— `ds4_dist_prepare_engine_options`：先校验选项，把 `opt->distributed = *opt` 拷进引擎选项；若分布式启用，置 `engine->load_slice=true`、`load_layer_start=opt->layers.start`、`load_layer_end = has_output ? UINT32_MAX : end`、`load_output = has_output`。这是「`--layers` → `load_slice`」的官方翻译点。

**引擎打开时再次确认切片，并区分 coordinator/worker 的 output 头策略**：

[ds4.c:25588-25602](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L25588-L25602)（位于 `ds4.c`） —— `ds4_engine_open` 里：若分布式角色已设且 `layers.set`，重新从 `opt->distributed.layers` 推导 `load_slice/load_layer_start/load_layer_end/load_output`，并多算一个 `load_output_optional = (role == COORDINATOR)`。这个「可选 output 头」让 coordinator 即使写了 `0:30` 也能绑上 lm_head，从而具备出 logits 的能力。

**`weights_bind` 按切片收窄层循环**（u3-l2 讲过的同一个函数）：

[ds4.c:4070-4089](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L4070-L4089)（位于 `ds4.c`） —— 当 `load_slice=true`：`start = load_layer_start`、`end = (load_layer_end==UINT32_MAX ? DS4_N_LAYER-1 : load_layer_end)`，并做越界校验；`require_token_embd = (start == 0)`——**只有负责第 0 层的进程才必须绑定 embedding**。于是 worker（start≠0）跳过 embedding，coordinator（start==0）绑定它。

**把权重绑定的结果交给 `weights_bind`**：

[ds4.c:25631-25637](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L25631-L25637)（位于 `ds4.c`） —— `weights_bind(&e->weights, &e->model, load_slice, load_layer_start, load_layer_end, load_output, load_output_optional)`。注意最后一个参数 `load_output_optional`：coordinator 传 true（output 头可绑可不绑），worker 传 false。

**GPU model map 也只映射切片内的张量 span**（内存节省的物理来源）：

[ds4.c:25835-25880](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L25835-L25880)（位于 `ds4.c`） —— 当 `load_slice` 时，用 `weights_model_map_spans(&e->weights, load_layer_start, load_layer_end, map_output, &spans)` 只算出本段层 + （可能的）output 头那几个张量区间，再把 mmap 的 model map **限制在这些 span 上**。日志会打印 `restricting <backend> model map to layers A:B (N spans, X GiB tensor span)`——这就是「每台机器只映射自己一半」的直接证据。

#### 4.3.4 代码实践

**实践目标**：跟踪 `--layers 31:output` 从命令行一直到 `weights_bind`，亲手验证「worker 不绑 embedding、绑 output head、只 mmap 一段」。

**操作步骤**：

1. 从 [ds4_distributed.c:8036-8076](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L8036-L8076) 的 `dist_parse_layers` 出发，确认 `31:output` 解析成 `{start=31, end=UINT32_MAX, has_output=true}`。
2. 跳到 [ds4_distributed.c:8346-8366](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L8346-L8366) 的 `ds4_dist_prepare_engine_options`，确认它把上面这组值翻译成 `load_slice=true / load_layer_start=31 / load_layer_end=UINT32_MAX / load_output=true`。
3. 进入 `ds4.c` 的 `ds4_engine_open`（[ds4.c:25588-25602](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L25588-L25602)），注意 `load_output_optional` 在 worker 角色下为 false。
4. 最后看 `weights_bind`（[ds4.c:4070-4089](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L4070-L4089)）：`start=31≠0` ⇒ `require_token_embd=false`（**不绑 embedding**）；层循环只跑 31..末层；`load_output=true` ⇒ 绑定 lm_head。

**需要观察的现象 / 预期结果**：你能填出这张表——

| 进程 | `--layers` | 绑定 token_embd? | 绑定哪些层 | 绑定 output head? | `load_output_optional` |
| --- | --- | --- | --- | --- | --- |
| coordinator | `0:30` | 是（start==0） | 0..30 | 可选（GGUF 有则绑） | true |
| worker | `31:output` | 否 | 31..42 | 是 | false |

> 「待本地验证」：在有 PRO Q4 分片 GGUF 的两机环境，启动 worker 时观察 stderr 的 `restricting <backend> model map to layers 31:output (N spans, X GiB)` 日志，确认 X 约为整模型的一半。

#### 4.3.5 小练习与答案

**练习 1**：为什么一台 `--layers 31:output` 的 worker **不加载** `token_embd`？

**参考答案**：`weights_bind` 里 `require_token_embd = (start == 0)`（[ds4.c:4088](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L4088)）。worker 的 `start=31≠0`，所以不要求 embedding。原因：worker 接收的输入是 coordinator 算好的 hidden state，不是 token id，根本用不到 embedding 查表。

**练习 2**：`load_layer_end = UINT32_MAX` 这个哨兵值为什么需要存在？直接存「末层下标 42」不行吗？

**参考答案**：解析层（`dist_parse_layers`）发生在**还不知道模型有多少层**的时候——`--layers` 是命令行参数，模型要等 `ds4_engine_open` 才打开。所以解析时只能用 `UINT32_MAX` 表示「到末层」，等知道了 `n_layers` 再由 `dist_resolved_layer_end` / `weights_bind` 折算成真实下标。这是「配置时刻」与「加载时刻」之间的桥。

**练习 3**：`load_output_optional` 对 coordinator 为 true、对 worker 为 false，这背后对应 4.2 讲的哪种模式？

**参考答案**：对应 `--layers 20:42` 慢链路模式。此时 coordinator 写的是 `0:30`（不带 output），但 `load_output_optional=true` 让它**仍然可以**绑定 output head（若 GGUF 提供），于是 `local_can_output_head=true`，coordinator 能在末位 worker 回传 hidden state 后本地算出 logits。worker 的 `load_output_optional=false`，因为它要么带 output（`N:output`，绑 lm_head）、要么不带（`N:42`，不绑），没有「可选」一说。

---

## 5. 综合实践

**任务**：为一个 43 层（0..42）的模型设计一份「三机分布式 + 慢链路」配置，并预言每台机器会绑定哪些权重。

**背景**：假设你有三台机器：A（coordinator，快速本地）、B（与 A 之间是 Thunderbolt 5）、C（与 B 之间是高延迟 VPN）。模型放不进任何单机，必须三机分担；且最后一段到 C 的链路很慢。

**要求**：

1. 给出 A/B/C 三台机器的 `--role` 与 `--layers` 参数，使路由能覆盖 0..42 + output head。
2. 解释你把 output head 放在哪台机器、为什么（结合 4.2 的两种模式与「慢链路」这一约束）。
3. 用 4.3 的映射规则，预言每台机器是否绑定 `token_embd`、绑定哪几层、是否绑定 output head。
4. 写出 coordinator 上 `--debug` 应当打印出的路由形如 `local 0:K -> hostB:port ... -> hostC:port ... -> local output`（或不含 `local output`，取决于你的设计）。

**参考设计（一种合理方案）**：

- 让「慢」的 C 只承担中间一段（比如 `15:27`），避免它成为自回归 decode 的关键末跳；把末层 + output head 交给与 coordinator 快连的 B。
- 若 B 到 A 足够快：A `--layers 0:14`，B `--layers 28:output`，C `--layers 15:27`。output head 在 B（`N:output` 模式，prefill 友好）。路由：`local 0:14 -> C 15:27 -> B 28:output`。
- 若连 B 到 A 的回传也想省字节：可让 B `--layers 28:42`、A `--layers 0:14` 且依赖 coordinator 的 `load_output_optional` 出 logits（`N:42` 模式）。此时路由末尾会出现 `-> local output`，每 token 回传 hidden state 而非 logits，慢段（C→B）的回传也变小。
- `token_embd`：仅 A 绑定（A 的 start==0）；B、C 不绑。output head：方案一在 B，方案二在 A。每台机器的 mmap 区间约对应各自那段层的权重大小。

> 「待本地验证」：真实三机环境下，用 coordinator 的 `--debug` 核对路由组建日志与每跳 `input_bytes/output_bytes`，确认你的预言与实际一致；尤其是 output head 归属不同时，末跳回传字节数的差异。

## 6. 本讲小结

- ds4 分布式只有两种角色：**coordinator**（拥有分词/采样/prompt 与从第 0 层起的头一段，是请求入口出口）与 **worker**（拥有中间或末尾一段层，各自持有自己的 KV 切片）。worker 之间直连转发，coordinator 不当中转。
- **分布式是引擎后端，不是独立前端 API**：前端仍用普通 `ds4_session_*`，coordinator 用 `ds4_dist_session` 把调用沿路由分发；只有 `--role worker` 与一次性 `coordinator -p` 才直接调 `ds4_dist_run()`。
- `--layers A:B` 是闭区间；`A:output` 表示「到末层 + output head」。`output` 在解析时存成哨兵 `UINT32_MAX`，加载时由 `dist_resolved_layer_end` 折算成真实末层。
- **output head 归属**是核心取舍：默认末位 worker 拥有（`N:output`，prefill 友好）；慢链路上可用 `N:42` 让 coordinator 持有 output head、每 token 回传更小的 hidden state，代价是 coordinator 多扛权重与计算。
- 层切分**复用 u3-l2 的同一份 `weights_bind`**：`ds4_dist_prepare_engine_options` 把 `--layers` 翻译成 `load_slice/load_layer_start/load_layer_end/load_output`，引擎打开时层循环与 mmap 区间都收窄到本段层——这就是「放不进单机的模型被拆到多机」的物理来源。`token_embd` 只在 coordinator（start==0）绑定。
- 路由就绪 = coordinator 从 0 起 + 一串 worker 首尾相接覆盖到末层 + output head 有归属（末位 worker 带头，或 coordinator `local_can_output_head`）。

## 7. 下一步学习建议

本讲把「架构与切分」讲完了——谁负责什么、层怎么分、output head 归谁、切分如何落地到权重绑定。下一讲 **u9-l4（分布式协议、路由与流水线）** 会钻进协议细节：

- HELLO/WORK/RESULT 帧的字节布局（本讲见到的 `ds4_dist_hello_fixed` / `ds4_dist_work_fixed` 等结构体如何在 TCP 上编解码）。
- 控制连接（注册）与数据连接（传活）的分离。
- prefill 流水线（coordinator 算 chunk N+1、worker 算 chunk N 的重叠）与生成自回归为何不能流水线。
- 滚动 token 前缀 hash 校验、worker 掉线后 coordinator 如何重放重建 KV。

建议继续阅读：

- [ds4_distributed.c:36-71](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L36-L71) 的消息常量与 work/result 标志位，为 u9-l4 的协议帧做铺垫。
- [README.md:464-485](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L464-L485) 的「Distributed protocol overview」一节，结合本讲建立的角色与切分心智模型去读，会顺畅很多。
- 若想看流水线在代码里的开关，预习 `ds4_dist_session_sync` 里 `dist_coordinator_can_pipeline_prefill` 的判定（[ds4_distributed.c:5504-5538](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_distributed.c#L5504-L5538)）。
