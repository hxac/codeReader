# Fleet 仿真器

## 1. 本讲目标

本讲讲解 vLLM Semantic Router 仓库里的舰队仿真器 `src/fleet-sim`（包名 / 命令名 `vllm-sr-sim`）。学完后你应当能够：

- 说清 fleet-sim 在整个项目里的定位：它是一个**离线容量规划与路由策略评估工具**，不是运行时在线路由器进程。
- 看懂它的源码组织：`core/`（仿真引擎）、`gpu_profiles` + `workload`（硬件与流量建模）、`routing`（可插拔路由器）、`optimizer`（优化器），以及 `run_sim.py` 这条 CLI 入口。
- 理解它的离散事件仿真（DES）内核如何把「一台 GPU」抽象成 M/G/c 排队队列、用 KV-cache 槽位当服务台、用抢占最长序列还原 vLLM 的 PagedAttention 行为。
- 掌握优化器 `FleetOptimizer` 的「两阶段」套路：先用解析公式（Erlang-C / Kimura）快速筛出候选舰队，再用 DES 精确验证前几名。
- 用 `run_sim.py` 跑一次仿真，说明 GPU 画像、工作负载 CDF 如何影响输出，并准确指出 `optimizer/base.py` 的职责边界（管什么、不管什么）。

## 2. 前置知识

本讲依赖 u2-l1 建立的「信号→投影→决策」心智模型，但要先做一个**关键区分**：

- **运行时的 SR（Go 路由器）**：在 Envoy ExtProc 控制面里，对**真实流量**逐条做信号抽取、投影、决策、模型分发。
- **fleet-sim（本讲的 Python 工具）**：在真实流量之外，对**假设的 GPU 舰队**做建模与仿真，回答「我要多少块、什么型号的 GPU、用什么路由策略，才能在给定工作负载下满足 SLO 又最省钱」。

二者共享同一个思想内核——**把不同性质的请求路由到不同模型/池子（Mixture-of-Models）**——但前者是「在线决策」，后者是「离线规划」。fleet-sim 甚至内置了一个 `SemanticRouter` 路由算法，让你在仿真里复现线上的语义分类路由策略，做到「先离线 benchmark、再上线」。

阅读本讲前最好了解这些排队论常识（看不懂公式不影响理解流程）：

- **M/G/c 队列**：到达过程是马尔可夫（M，即泊松），服务时间是一般分布（G），有 c 个并行服务台。LLM 推理里 c = KV-cache 槽位数。
- **TTFT（Time-To-First-Token）**：从请求到达到第一个 token 生成的时间，是本讲最关心的延迟指标。
- **CDF（累积分布函数）**：工作负载用「token 数 → 累积占比」的折线来表示，例如「64 token 占 0.83%、512 token 占 28.9% ……」。
- **离散事件仿真（DES）**：按时间顺序逐个处理「请求到达」「服务完成」事件来推进时钟，而不是把时间切成固定小步。

## 3. 本讲源码地图

本讲涉及的文件都在 `src/fleet-sim/` 下，按职责分层：

| 文件 | 作用 |
|---|---|
| [`run_sim.py`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/run_sim.py) | 统一 CLI 入口 `vllm-sr-sim`，用 argparse 注册 optimize/simulate/whatif/pareto/compare-routers/disagg/grid-flex/tok-per-watt/simulate-fleet/serve 子命令 |
| [`server.py`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/server.py) | `serve` 子命令的薄启动器，委托给 `run_sim.main(["serve", ...])` 起 uvicorn |
| [`fleet_sim/core/fleet.py`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/fleet_sim/core/fleet.py) | DES 顶层协调器 `Fleet` + 配置 dataclass（`PoolConfig`/`FleetConfig`）+ 结果对象 `FleetSimResult` |
| [`fleet_sim/core/pool.py`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/fleet_sim/core/pool.py) | 同构 GPU 实例池 `Pool`，含负载均衡（least_queue / round_robin / least_loaded） |
| [`fleet_sim/core/instance.py`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/fleet_sim/core/instance.py) | 单 GPU 实例 `Instance`：M/G/c 队列、KV-cache 预算、抢占、TTFT 计算 |
| [`fleet_sim/core/request.py`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/fleet_sim/core/request.py) | `Request` 数据类与生命周期状态机（PENDING→QUEUED→PREFILLING→DECODING→DONE） |
| [`fleet_sim/gpu_profiles/profiles.py`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/fleet_sim/gpu_profiles/profiles.py) | 预置 GPU 画像 A100_80GB / H100_80GB / A10G（手工标定的 W/H/KV/功耗曲线） |
| [`fleet_sim/gpu_profiles/protocol.py`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/fleet_sim/gpu_profiles/protocol.py) | `GpuProfile` Protocol：仿真引擎唯一依赖的 GPU 接口 |
| [`fleet_sim/workload/synthetic.py`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/fleet_sim/workload/synthetic.py) | 工作负载生成：`CdfWorkload`（按 CDF 采样长度）+ `PoissonWorkload`（泊松到达） |
| [`fleet_sim/routing/base.py`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/fleet_sim/routing/base.py) | 路由器抽象基类 `BaseRouter` |
| [`fleet_sim/routing/length_based.py`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/fleet_sim/routing/length_based.py) | 按长度路由 `LengthRouter`（双池短/长分流的默认实现） |
| [`fleet_sim/routing/semantic_router.py`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/fleet_sim/routing/semantic_router.py) | 语义分类路由 `SemanticRouter`，连接 u2-l1 的在线路由理念 |
| [`fleet_sim/optimizer/base.py`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/fleet_sim/optimizer/base.py) | 核心优化器 `FleetOptimizer`：两阶段（解析筛选 + DES 验证） |
| [`fleet_sim/optimizer/analytical.py`](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/fleet_sim/optimizer/analytical.py) | 解析公式：Erlang-C、Kimura P99 等待、GPU 服务率标定、最小 GPU 数求解 |

## 4. 核心概念与源码讲解

### 4.1 仿真器总览与命令行入口

#### 4.1.1 概念说明

fleet-sim 是一个**纯 Python 的离线工具**，回答部署前最贵的两个问题：

1. **容量规划（sizing）**：要承载 λ req/s 的流量、满足 P99 TTFT ≤ SLO，最少需要几块 GPU？哪种型号？
2. **路由策略评估**：把流量按长度分两个池、或用语义分类分多池、或加压缩（Compress-and-Route），哪种路由更划算？

它有两种形态：一个 **CLI**（`vllm-sr-sim`，由 `run_sim.py` 实现）和一个 **HTTP 服务**（`serve` 子命令，由 FastAPI + uvicorn 实现）。后者是 dashboard 面板的 sidecar——`vllm-sr serve` 默认会把 fleet-sim 作为兄弟容器（sibling container）拉起在共享运行网络上，让面板跨容器代理它，而不需要重打 router 镜像。

#### 4.1.2 核心流程

CLI 的核心是一条「命令分发」流水线：

1. `main(argv)` 用 argparse 建子命令，每个子命令绑定一个 `cmd_xxx` 处理函数。
2. 每个处理函数做四件事：`load_cdf` 读工作负载 → 建配置/优化器对象 → 跑计算 → 打印报告（可选 `--out` 存 JSON）。
3. `serve` 是特例：它不跑仿真，而是把请求转给 FastAPI app（`fleet_sim.api.app:app`），由 uvicorn 托管。

子命令一览：

| 子命令 | 作用 |
|---|---|
| `optimize` | 求满足 SLO 的最低成本舰队（两阶段） |
| `simulate` | 仿真一个**固定**的双池舰队（给定 n_s / n_l） |
| `whatif` | 扫描到达率 λ，或多 GPU 型号横评 |
| `pareto` | 扫描所有 CDF 断点作为 B_short，画出阈值-成本-延迟帕累托前沿 |
| `compare-routers` | 在同一舰队上比较多种路由算法 |
| `disagg` | 分离 prefill/decode 池的优化器 |
| `grid-flex` | 需求响应下的功耗-延迟权衡（GPU-to-Grid） |
| `tok-per-watt` | GPU 能效（token/瓦）对比 |
| `simulate-fleet` | 仿真任意 N 池舰队（JSON 配置） |
| `serve` | 启动 HTTP 服务 |

#### 4.1.3 源码精读

CLI 的 GPU 型号注册表把字符串名映射到画像对象，是所有子命令选 GPU 的入口：

[run_sim.py:85-91](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/run_sim.py#L85-L91) —— `GPU_REGISTRY` 把 `"a100"/"h100"/"a10g"` 映射到 `A100_80GB`/`H100_80GB`/`A10G` 画像对象；`load_cdf` 把 JSON 解析成 `(token, frac)` 列表。

[run_sim.py:97-138](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/run_sim.py#L97-L138) —— `cmd_optimize` 是 optimize 子命令的处理函数：建 `FleetOptimizer`、按 `[1.0, 1.1, …, gamma_max]` 枚举压缩系数 γ、调用 `optimize()` 拿 `OptimizationReport` 并 `print_report()`。

[run_sim.py:986-1001](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/run_sim.py#L986-L1001) —— `main()` 与公共参数 `add_common`：`--cdf`、`--lam`、`--slo`、`--b-short` 是贯穿大部分子命令的公共参数。

`serve` 子命令体现了「薄壳」风格——它只负责拉起 uvicorn，真正的 API 路由在 `fleet_sim.api.app`：

[run_sim.py:440-462](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/run_sim.py#L440-L462) —— `cmd_serve` 延迟导入 uvicorn（缺失则提示装 API extras），设置示例 trace 环境变量，再 `uvicorn.run("fleet_sim.api.app:app", ...)`。

`server.py` 只是它的等价薄启动器：

[server.py:10-15](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/server.py#L10-L15) —— 直接调 `run_sim.main(["serve", *sys.argv[1:]])`，方便 `python server.py --port 8080` 这样的用法。

#### 4.1.4 代码实践

1. **实践目标**：把 fleet-sim 装成本地命令，浏览所有子命令。
2. **操作步骤**：

   ```bash
   cd src/fleet-sim
   pip install -e .          # 装成可编辑包，注册 vllm-sr-sim 命令
   vllm-sr-sim --version
   vllm-sr-sim --help        # 列出全部子命令
   vllm-sr-sim optimize --help
   ```
3. **需要观察的现象**：`--help` 会列出 optimize/simulate/whatif/pareto/compare-routers/disagg/grid-flex/tok-per-watt/simulate-fleet/serve 十个子命令；`optimize --help` 会显示 `--cdf/--lam/--slo/--b-short/--gamma-max/--verify-top/--n-sim-req` 等参数。
4. **预期结果**：`--version` 打印版本号（来自包元数据，未安装时回退 `0.0.0`）；命令注册成功。
5. 待本地验证（取决于 Python 环境是否就绪）。

#### 4.1.5 小练习与答案

**练习**：`serve` 子命令和 `optimize` 子命令的本质区别是什么？

**答案**：`optimize` 是**一次性计算**——读 CDF、跑优化器、打印报告后进程退出；`serve` 是**长驻服务**——起一个 HTTP server，dashboard 通过 REST API 反复调用它做 what-if 分析。前者是批处理，后者是 RPC 后端。

---

### 4.2 仿真核心：分层 DES 事件引擎

#### 4.2.1 概念说明

仿真引擎采用经典的**分层离散事件仿真（DES）**，三层结构对应硬件三层：

- `Fleet`：整个舰队，跑全局事件循环，把请求交给路由器。
- `Pool`：一组**同型号** GPU 实例，做池内负载均衡。
- `Instance`：**单块 GPU**，建模为 M/G/c 队列，c = KV-cache 槽位数。

关键直觉：**把 KV-cache 槽位当成「服务台」**。vLLM 用 PagedAttention 让一块 GPU 同时服务多个并发序列，所以「一块 GPU 有几个槽」就等价于「这台机器有几个服务台」。当所有槽满时，请求排队；当 KV 块预算（`total_kv_blks`）不够时，会**抢占**当前最长的活跃序列，把它踢回队首——这正是 vLLM 的 preemption 行为。

#### 4.2.2 核心流程

事件循环（在 `Fleet.run`）每一步做：

1. 算「下一个事件时间」= min(下一个到达时间, 所有 Pool 的下一个完成时间)。
2. 把所有 Pool 的时钟推进到该时间（`advance_to`）。
3. 把所有已到达的请求交给路由器 `router.route(req)`，得到 `pool_id`，再 `pool.route(req)` 入队。
4. 循环直到到达耗尽且所有队列排空，最后给 10 分钟「排空窗口」。

`Instance` 内部则是 M/G/c 的微观：请求进队 → 有空槽就 `_start_next` 算服务时间并排完成事件 → 时间推进到完成事件时释放槽与 KV 块 → KV 不够就抢占最长序列。

服务时间建模（见 `instance.py` 顶部文档）的核心公式——decode 单轮延迟随并发线性增长：

\[ H_{eff} = H \cdot \frac{\bar{L}_{seq}}{\text{calibration\_ctx}} \quad\text{（注意力开销 ∝ 序列长度）} \]

\[ t_{iter}^{decode} = W + H_{eff}\cdot n_{active} \quad\text{（受显存带宽限制）} \]

\[ S_{raw} = t_{prefill}\cdot \lceil L_{in}/\text{chunk} \rceil + L_{out}\cdot t_{iter}^{decode} \]

而 TTFT 是排队等待加上 prefill 时间：

\[ \text{TTFT} = W_{queue} + \lceil L_{in}/\text{chunk} \rceil \cdot t_{prefill\_iter}(n_{active}) \]

为了和解析 M/G/c 模型保持一致，调度用的「有效服务时间」折算到满槽：

\[ S_{eff} = S_{raw}(\text{full } n_{slots}) / n_{slots} \]

#### 4.2.3 源码精读

请求的生命周期状态机是引擎的字段约定：

[request.py:9-15](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/fleet_sim/core/request.py#L9-L15) —— `RequestState` 枚举：`PENDING → QUEUED → PREFILLING → DECODING → DONE`。

[request.py:67-95](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/fleet_sim/core/request.py#L67-L95) —— `ttft`、`e2e_latency`、`tpot`、`queue_wait` 四个派生属性：引擎只填时间戳，指标由这些属性算出。

`Fleet.run` 的核心事件循环：

[fleet.py:122-194](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/fleet_sim/core/fleet.py#L122-L194) —— 注意 while 条件是「还有未到达的请求 或 任意 Pool 还在忙」，每轮取 min(到达, 完成) 作为推进点；路由返回 `None` 或未知 pool 时请求被标记 `DONE`（丢弃）；末尾给 600s 排空窗口。

池内负载均衡的三种策略在 `Pool._pick_instance`：

[pool.py:83-100](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/fleet_sim/core/pool.py#L83-L100) —— `least_queue` 选队最短的、`least_loaded` 选（活跃+排队）最少的、`round_robin` 轮询。

单实例的 KV-cache 抢占（最还原 vLLM 的部分）：

[instance.py:217-253](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/fleet_sim/core/instance.py#L217-L253) —— `_start_next` 在 KV 块预算不够时，循环抢占当前最长的活跃序列（`victim = max(self._active_reqs, key=...)`），把它放回队首、释放其槽与块，直到能放下新请求。

[instance.py:283-300](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/fleet_sim/core/instance.py#L283-L300) —— 物理完成时间用真实活跃槽数算（决定 TPOT），调度完成事件用满槽折算（`s_eff = s_raw_full / n_slots`），二者分离是为了和 M/G/c 队列模型自洽。

聚合指标 `FleetSimResult`：

[fleet.py:236-272](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/fleet_sim/core/fleet.py#L236-L272) —— `p99_ttft_ms`、`slo_compliance(t_slo_ms)`、`mean_utilisation` 是后续路由比较与优化器验证直接消费的指标。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：用一条 `simulate` 命令跑通完整 DES，观察输出字段，并定位这些字段在源码里如何被算出。
2. **操作步骤**：

   ```bash
   cd src/fleet-sim
   vllm-sr-sim simulate \
     --cdf data/azure_cdf.json \
     --lam 200 --n-s 40 --n-l 20 \
     --b-short 6144 --n-req 50000 --slo 500
   ```
3. **需要观察的现象**：输出分 fleet 汇总（Total GPUs、Annualised cost、Fleet P99 TTFT、SLO compliance、Mean utilisation）和逐池明细（short/long 各自 P99 TTFT、队列等待、利用率、SLO 达标率）。
4. **预期结果**：Azure CDF 在 6144 token 处累积占比约 0.976（见 4.3 的 `cdf_eval`），所以绝大多数流量进 short 池，short 池利用率高、long 池偏闲；seed=42 使结果可复现。具体数值**待本地验证**。
5. 跑完后对照 `fleet.py` 的 `summary()`（`fleet.py:274-295`）确认每个打印字段对应哪个聚合函数。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Instance` 要把「物理完成时间」和「调度完成时间」分成 `s_raw` 和 `s_eff` 两个字段？

**答案**：TPOT（每 token 延迟）反映真实硬件体验，必须用**实际**活跃槽数算 `s_raw`；而 M/G/c 排队模型假设每个槽是独立服务台，调度器需要**满槽**口径的 `s_eff` 才能让 DES 的排队分布与解析模型自洽。一个管「物理延迟真实性」，一个管「排队一致性」。

**练习 2**：当 KV 块预算耗尽时，`Instance` 抢占哪条请求？为什么？

**答案**：抢占**当前最长**的活跃序列（`max(..., key=lambda r: r.l_in + r.l_out)`）。因为长序列占的 KV 块最多，踢掉它能一次腾出最多空间、最快缓解内存压力——这与 vLLM 的最长序列优先抢占策略一致。

---

### 4.3 硬件画像与工作负载建模

#### 4.3.1 概念说明

仿真要可信，必须把「硬件」和「流量」都建模准。fleet-sim 用两个抽象把它们解耦：

- **GPU 画像 `GpuProfile`**：一块 GPU 跑某个模型的性能特征。用 `ManualProfile` 手工标定，或用 `ComputedProfile` 从硬件规格 + 模型规格算出来。核心是两个延迟常数 **W**（每轮固定开销）和 **H**（每序列额外开销），加上 KV-cache 几何（块大小、总块数、最大槽位）和**功耗曲线**（用于 grid-flex / tok-per-watt）。
- **工作负载**：用经验 CDF 描述「请求长度分布」，再用泊松过程生成到达流。

`GpuProfile` 被刻意定义成**结构化 Protocol**（鸭子类型）——仿真引擎只认这个接口，不关心常数是手工填的还是算出来的，这让画像来源可替换。

#### 4.3.2 核心流程

GPU 单轮 decode 延迟建模为线性叠加：

\[ t_{iter}^{decode}(n_{active}) = W + H_{eff}\cdot n_{active},\quad H_{eff}=H\cdot\frac{\bar L_{seq}}{\text{calibration\_ctx}} \]

W 是「常数项」（权重读取、控制开销），\(H\cdot n_{active}\) 是「随并发线性增长项」（KV cache 读取、注意力），所以并发越高、每轮越慢。序列长度缩放 \(H_{eff}\) 让「服务短请求的池」比「服务长请求的池」在同样槽位下更快。

功耗用 logistic 曲线建模（用于电网需求响应分析）：

\[ P(b) = \frac{P_{nominal}-P_{idle}}{1 + e^{-k(\log_2 b - x_0)}} + P_{idle} \]

其中 b 是并发请求数（≈ vLLM `max_num_seqs`），k 控制过渡陡峭程度，\(2^{x_0}\) 是饱和批量。H100 的功耗参数是**实测拟合**（HIGH 质量），A100 是「一个锚点 + 投影」（FAIR），A10G 是纯投影（LOW，需校准）。

工作负载侧：`CdfWorkload.sample_length()` 用逆变换采样从 CDF 抽一个总长度，按 `l_in_frac`/`l_out_frac`（默认 0.8/0.2）切成输入/输出；`PoissonWorkload.generate()` 用 `expovariate(lam)` 生成到达间隔，组成 `(arrival_time, Request)` 流。

#### 4.3.3 源码精读

`GpuProfile` Protocol 定义了引擎唯一依赖的四个方法：

[protocol.py:14-97](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/fleet_sim/gpu_profiles/protocol.py#L14-L97) —— `iter_latency` / `prefill_iter_latency` / `n_slots` / `service_time` 是引擎和优化器唯一调用的成员，注释明确说明「常数是手工填的还是从第一性原理算出的，对仿真核心无关紧要」。

三个预置画像的常数对比（Llama-3-70B，8-GPU TP 标定）：

[profiles.py:43-79](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/fleet_sim/gpu_profiles/profiles.py#L43-L79) —— `A100_80GB`：W=0.0080、H=0.00065、chunk=512、max_slots=128、cost_per_hr=2.21、功耗 FAIR 质量。

[profiles.py:81-106](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/fleet_sim/gpu_profiles/profiles.py#L81-L106) —— `H100_80GB`：W=0.0040（更快）、H=0.00032、max_slots=256、cost_per_hr=4.02（更贵）、功耗 HIGH 质量（实测拟合 G2G 论文图 2）。

对比 A100 vs H100：H100 的 W、H 都更小（更快），槽位更多（256 vs 128），但 `cost_per_hr` 几乎翻倍——这正是 `whatif --gpu-compare` 要权衡的「快但贵 vs 慢但便宜」。

工作负载建模：

[synthetic.py:34-82](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/fleet_sim/workload/synthetic.py#L34-L82) —— `CdfWorkload.__init__` 读 CDF、设 `category_mix`（默认 prose 60%/code 25%/rag 15%，给 C&R 压缩用）；`sample_request` 抽长度、切 in/out、抽类别，构造 `Request`。

[synthetic.py:104-127](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/fleet_sim/workload/synthetic.py#L104-L127) —— `PoissonWorkload.generate` 用 `expovariate(lam)` 累加得到泊松到达流。

数据格式（见 `data/README.md` 与 `azure_cdf.json`）：JSON 数组，每项是 `[token_length, cumulative_fraction]`，按 token 升序、累积占比 ∈ [0,1]。Azure trace 是 28K 生产请求、p90=4.2K token。

#### 4.3.4 代码实践

1. **实践目标**：感受「GPU 画像 + 工作负载」如何共同决定输出——同负载下换 GPU 型号，看舰队规模与成本怎么变。
2. **操作步骤**：

   ```bash
   cd src/fleet-sim
   # A100：默认单型号
   vllm-sr-sim whatif --cdf data/azure_cdf.json \
     --lam-range 100 200 400 --slo 500 --b-short 6144
   # 横评 A100 / H100 / A10G
   vllm-sr-sim whatif --cdf data/azure_cdf.json \
     --lam-range 200 --slo 500 --b-short 6144 \
     --gpu-compare a100 h100 a10g
   ```
3. **需要观察的现象**：每个 λ 会输出满足 SLO 的最优 (n_s, n_l, total, 年成本, γ*, P99)；横评会并排打印三种 GPU 的 GPU 数/年成本/每小时成本/P99/SLO，并算相对基线的成本倍数。
4. **预期结果**：H100 单卡更快、槽位更多，所以同样 λ 下 GPU 数更少，但单卡更贵，年成本可能更高或更低（取决于利用率拐点）；A10G 廉价但慢，高 λ 下可能无法满足 SLO。具体数字**待本地验证**。
5. 把 H100 的 `cost_per_hr=4.02`、A100 的 `2.21`、A10G 的 `1.01` 与 `max_slots` 256/128/64 对应起来，解释输出的 GPU 数差异。

#### 4.3.5 小练习与答案

**练习**：为什么 A10G 的功耗参数被标注为 LOW 质量、并附「使用前需经验校准」的警告？

**答案**：H100 的功耗有 ML.ENERGY Benchmark v3.0 实测数据（HIGH），A100 有一个实测锚点可投影（FAIR），而 A10G 没有任何公开的 batch-vs-power 测量数据，所有参数（k、x0、P_nominal）都是从 TDP、显存带宽、模型大小纯投影出来的。在电网需求响应（grid-flex）这种对功耗精度敏感的场景里，未校准的投影可能误导决策，所以源码强烈建议先用真实数据校准再用。

---

### 4.4 路由策略与可插拔路由器

#### 4.4.1 概念说明

fleet-sim 的 `routing/` 包实现了多种可插拔路由器，全部继承自 `BaseRouter`，对外只有一个方法 `route(req) -> pool_id`。这让「评估不同路由策略」变成换一个 `router_type` 字符串：

| 路由器 | 路由依据 |
|---|---|
| `LengthRouter` | 按 token 长度（最短适配池 / 阈值二分） |
| `CompressAndRouteRouter` | 压缩（C&R）+ 路由：边缘请求压缩后塞进短池 |
| `SemanticRouter` | 用户提供的分类函数（语义/嵌入/规则） |
| `ModelRouter` | 请求自带的 `model_id` 标签 |
| `LeastLoadedRouter` / `RandomRouter` / `SpilloverRouter` | 负载/随机/溢出基线 |

其中 `LengthRouter` 是主力（双池短/长分流的默认实现），`SemanticRouter` 是连接 u2-l1 在线路由理念的桥梁——你在仿真里塞一个分类函数，就能离线 benchmark 语义路由策略。

#### 4.4.2 核心流程

`Fleet._build` 在起仿真时用字符串名实例化路由器，并注入运行时池引用：

```
router_cls = getattr(routing, config.router_type)
router = router_cls(pools=..., **config.router_kwargs)
if hasattr(router, "set_pools"): router.set_pools(live_pools)
```

`LengthRouter` 的逻辑很朴素：把池按 `max_ctx` 升序排，请求总长度 ≤ 阈值进第一个（最短）池，否则进最大的池——即「能装下的最小池」最短适配策略。`SemanticRouter` 则把决策外包给一个 `classify_fn(req) -> pool_id`，函数返回 `None` 或未知池时落到 `default_pool`。

#### 4.4.3 源码精读

[base.py:11-31](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/fleet_sim/routing/base.py#L11-L31) —— `BaseRouter` 抽象基类：`route(req)` 是唯一抽象方法，`pools` 以有序 dict 传入（顺序即优先级）。

[length_based.py:19-54](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/fleet_sim/routing/length_based.py#L19-L54) —— `LengthRouter`：构造时按 `max_ctx` 升序，`route` 里先试阈值二分、再退回最短适配，超容量兜底到最大池。

[semantic_router.py:81-101](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/fleet_sim/routing/semantic_router.py#L81-L101) —— `SemanticRouter`：默认 `classify_fn` 就是读 `req.model_id`，可注入任意分类函数；注释强调若分类器本身很慢（如嵌入查找），要把其延迟从 SLO 里扣除，以免扭曲排队动态。

完整的路由比较脚本是个很好的学习材料：

[routing_comparison.py:90-124](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/examples/routing_comparison.py#L90-L124) —— 该示例先用 `FleetOptimizer.sweep_analytical(gamma=1.0)` 把舰队「尺寸定死」，再在**同一舰队**上横跑四种路由器（同构/池路由/C&R/随机），这样比较的是纯路由质量而非舰队大小差异。

#### 4.4.4 代码实践

1. **实践目标**：用 `compare-routers` 在同一舰队上比较路由算法，验证「池路由 vs 随机」的差距。
2. **操作步骤**：

   ```bash
   cd src/fleet-sim
   vllm-sr-sim compare-routers --cdf data/azure_cdf.json \
     --lam 200 --n-s 50 --n-l 10 --b-short 6144 --slo 500 --n-req 30000
   # 或直接跑示例脚本（先尺寸化再横评四种路由）
   python3 examples/routing_comparison.py
   ```
3. **需要观察的现象**：输出一张表，列出 LengthRouter / CompressAndRouteRouter / RandomRouter 的 P99 TTFT、SLO%、利用率。
4. **预期结果**：LengthRouter（池路由）P99 显著优于 RandomRouter（随机），因为随机会把长请求塞进短池或反之，造成排队失衡；C&R 因压缩边缘请求进一步降本。具体数字**待本地验证**。
5. 对照 `run_sim.py:386-437`（`cmd_compare_routers`）确认它比较的是哪三种路由器及各自 kwargs。

#### 4.4.5 小练习与答案

**练习**：为什么 `routing_comparison.py` 要先用 `sweep_analytical(gamma=1.0)` 把舰队尺寸定死，再横评路由器？

**答案**：为了让对比只反映**路由策略本身**的质量，而非舰队大小的差异。如果每种路由器各自优化舰队大小，那 P99 差异就混入了「你给它的 GPU 数不同」这个因素。固定舰队后，P99/利用率差异纯粹来自「同一批 GPU 上不同分流方式的好坏」。

---

### 4.5 优化器：分析 sizing 与 DES 验证两阶段

#### 4.5.1 概念说明

`optimizer/base.py` 的 `FleetOptimizer` 是整个 fleet-sim 的「大脑」，回答部署前最贵的问题：**给定工作负载 CDF、到达率 λ、SLO，最少要几块 GPU？用哪种压缩系数 γ？**

它的核心设计是**两阶段（two-phase）**：

1. **解析筛选（analytical sweep）**：用闭式排队公式（Erlang-C / Kimura M/G/c）对每个 γ 值快速估算「最少 GPU 数」，几毫秒一个点，扫几十个 γ。这是快但近似的。
2. **DES 验证（verify top-N）**：只对解析阶段成本最低的前 N 个候选，跑一个**内联的轻量 DES**（slot 级 heap 模型），得到精确 P99 TTFT。这是慢但准的。

这样做比「对所有配置都跑 DES」快得多——DES 一跑就是几万请求，而解析公式是瞬时算的。两阶段还带来一个有用的副产物：`OptimizationReport` 同时保留 `analytical` 和 `simulated` 两套结果，可以对比「解析预测 vs 仿真实测」的偏差。

#### 4.5.2 核心流程

解析阶段（`sweep_analytical`）对每个 γ：

1. 用 `cdf_eval(cdf, B_short)` 算短请求占比 α_base；压缩系数 γ 决定边缘请求（介于 B_short 与 γ·B_short 之间）能被压进短池的比例，得有效短池占比：

   \[ \alpha' = \min(1,\ \alpha_{base} + \text{borderline\_frac}\cdot p_c) \]

2. 按比例分流到两池：\(\lambda_s=\alpha'\lambda,\ \lambda_l=(1-\alpha')\lambda\)。
3. `calibrate` 从 CDF 切片采样估算每池 GPU 的服务率 μ 与变异系数 \(c_v^2\)、槽位数 n_slots。
4. `min_gpus_analytical` 用 Kimura 公式迭代求「P99 等待 ≤ SLO 且利用率 ≤ ρ_max」的最小 GPU 数，再除以节点可用率（可靠性裕度）向上取整。
5. 算成本（GPU 数 × `cost_per_hr` × 8760），记一条 `SweepResult`，按成本排序。

可靠性裕度用 M/M/1 修复队列建模单节点稳态可用率：

\[ A = \frac{1}{1 + r_f\cdot \text{MTTR}_{days}} = \frac{\text{MTTF}}{\text{MTTF}+\text{MTTR}} \]

GPU 数按 \(1/A\) 放大，保证即便有 \((1-A)\) 比例节点在修也能满足 SLO。

解析公式（`analytical.py`）核心是 Kimura (1994) 的 M/G/c P99 等待时间：

\[ W_{99} = \frac{\ln(C/0.01)}{2(c\mu-\lambda)/(1+c_v^2)} \]

其中 \(C(c,a)\) 是 Erlang-C 的「需要等待概率」\(P(W_q>0)\)，\(a=\lambda/\mu,\ \rho=a/c\)。`erlang_c` 用对数域求和保证数值稳定。

验证阶段（`optimize` 调 `_run_des`）对 top-N 候选各跑一个堆式 M/G/c DES：每个 KV 槽当一个独立服务台，模拟 n 个请求，去掉前 1/5 预热，取 TTFT 的 P99，覆盖解析阶段的预估值。

#### 4.5.3 源码精读

`SweepResult` 是一条候选配置的数据点：

[base.py:44-62](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/fleet_sim/optimizer/base.py#L44-L62) —— 含 γ、n_s、n_l、total_gpus、cost_per_hr、annualised_cost_kusd、两池 P99、`slo_met`、`source`（`"analytical"` 或 `"simulated"`）。

两阶段总入口：

[base.py:322-393](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/fleet_sim/optimizer/base.py#L322-L393) —— `optimize`：先 `sweep_analytical` 得 candidates，再对前 `verify_top_n` 个各调 `_run_des`，把模拟 P99 覆盖回 `SweepResult`（source 标为 `"simulated"`），装进 `OptimizationReport`。

解析筛选的关键计算（含压缩分流与可靠性放大）：

[base.py:185-252](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/fleet_sim/optimizer/base.py#L185-L252) —— `sweep_analytical`：`alpha_prime = min(1, alpha_base + borderline_frac * p_c)`，`lam_s/lam_l` 按比例分流，`min_gpus_analytical` 求裸 GPU 数，`n_s = ceil(n_s_raw / node_avail)` 放大可靠性裕度。源码明确承认「泊松细分对长度路由只是近似（长度路由产生相关子流），由 DES 步骤经验性验证」。

可靠性建模函数及预置常数：

[base.py:67-135](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/fleet_sim/optimizer/base.py#L67-L135) —— `node_availability(r_f, mttr_hours)` 与三个预置常数（A100 RSC-1 快/慢路径、H100 5% 过配规则）。注释强调 MTTR 主要由**故障类型**而非 GPU 型号决定。

内联轻量 DES `_run_des`（注意：它与 `core/` 里那套完整事件 DES **不是**同一份代码，是专供验证的简化版）：

[base.py:395-520](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/fleet_sim/optimizer/base.py#L395-L520) —— 用 `heapq` 维护 `c = n_gpu × n_slots` 个槽位的释放时刻，每次 pop 最早释放的、算 slot_wait+prefill，去掉前 1/5 预热，取 P99。

解析公式实现：

[analytical.py:44-63](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/fleet_sim/optimizer/analytical.py#L44-L63) —— `erlang_c` 用 log-sum-exp 求和保证数值稳定。

[analytical.py:66-80](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/fleet_sim/optimizer/analytical.py#L66-L80) —— `p99_wait`：Kimura M/G/c P99 等待公式，\(C\le 0.01\) 时直接返回 0（几乎不排队）。

[analytical.py:134-164](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/fleet_sim/optimizer/analytical.py#L134-L164) —— `min_gpus_analytical`：迭代 GPU 数直到 P99 等待 ≤ SLO（`c_slo`），再与利用率上限约束（`c_rho`）取 max。

报告对象与「最佳」选取：

[base.py:526-613](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/fleet-sim/fleet_sim/optimizer/base.py#L526-L613) —— `OptimizationReport.best_analytical`/`best_simulated` 在满足 SLO 的候选里取成本最低者，`print_report` 打印 γ 扫描表与相对 γ=1.0 基线的节省百分比。

#### 4.5.4 代码实践（核心实践任务）

1. **实践目标**：跑一次 `optimize`，说明输入的 GPU 画像与工作负载如何影响输出，并指出 `optimizer/base.py` 的职责边界。
2. **操作步骤**：

   ```bash
   cd src/fleet-sim
   vllm-sr-sim optimize \
     --cdf data/azure_cdf.json \
     --lam 200 --slo 500 --b-short 6144 \
     --verify-top 3 --n-sim-req 30000
   ```
3. **需要观察的现象**：先打印 `[1/2] Analytical sweep` 表（每个 γ 的 α'、n_s、n_l、total、年成本、P99_s、P99_l、是否达标），再打印 `[2/2] DES verification` 对 top-3 候选的仿真 P99，最后是 Fleet Optimization Report 的最佳解析解与最佳仿真解、γ 扫描节省表。
4. **预期结果**：
   - **工作负载影响**：Azure CDF 在 6144 token 处累积占比高（约 0.976），所以 α_base 很大，短池吃下绝大多数流量、n_l 很小；λ 越大所需 GPU 越多、年成本越高。
   - **GPU 画像影响**：换 `--gpu-short h100` 后单卡更快、槽位更多，n_s 下降但单卡更贵，年成本取决于拐点。
   - **γ 影响**：γ 从 1.0 升到 ~1.5 时，更多边缘请求被压进短池，长池 GPU 数下降，年成本可能先降后升（压缩红利 vs 短池过载）。
   - 具体数值**待本地验证**。
5. **`optimizer/base.py` 的职责边界**（重点）：它只负责「给定 CDF + λ + GPU 画像 + SLO，求聚合两池（短/长，可选 C&R 压缩 γ）的最优 (γ, n_s, n_l) 与成本」——即**容量规划决策**。它**不负责**：① 真实流量的在线路由（那是运行时 Go 路由器的事）；② GPU 画像常数的来源（那是 `gpu_profiles` 包的事，`base.py` 只把 `GpuProfile` 当不透明接口消费）；③ 完整多池 DES 的微观实现（它自带的 `_run_des` 是验证专用的简化版，真正的事件 DES 在 `core/` 包里）；④ 其它部署形态——prefill/decode 分离（`disagg.py`）、电网需求响应（`grid_flex.py`）、token/瓦特能效（`tpw.py`）、阈值 Pareto（`threshold.py`）都是同目录的**兄弟模块**，逻辑彼此独立。一句话：`base.py` 是「聚合两池的最小成本 sizing + 验证」这一件事的负责人，其它部署形态与能耗/能效分析各有归属。

#### 4.5.5 小练习与答案

**练习 1**：为什么优化器要先解析筛选再只验证 top-N，而不是直接对所有配置跑 DES？

**答案**：DES 要模拟几万请求，单次就要可观时间；而解析公式瞬时算出。先用解析公式扫整个 γ 空间把成本最低的几个挑出来，再只对这几个跑 DES 精确验证，能在不损失最终决策质量的前提下把总耗时从「几十次 DES」降到「几次 DES」。

**练习 2**：`node_avail=1.0`（默认）和 `node_avail=0.95` 时，求出的 GPU 数有什么关系？

**答案**：`n = ceil(n_raw / node_avail)`。默认 1.0 时不放大；0.95 时每个池的 GPU 数约放大 \(1/0.95 ≈ 1.053\) 倍（向上取整），相当于约 5% 过配，保证即便 5% 节点在修也能满足 SLO——这正是源码里 H100 的 `H100_AVAIL_5PCT=0.95` 规则的由来。

**练习 3**：`min_gpus_analytical` 除了 P99 SLO 约束，为什么还要加一个利用率上限 ρ_max=0.85？

**答案**：Kimura 的 M/G/c 近似在 ρ→1 时误差急剧增大（排队发散）。加 ρ_max 既保护近似精度，也避免把舰队设计在「踩着过载边缘」的高利用率区间。

---

## 5. 综合实践

把本讲五个模块串起来，完成一次「从工作负载到舰队决策」的完整闭环：

1. **规划**：跑 `vllm-sr-sim optimize --cdf data/azure_cdf.json --lam 200 --slo 500 --b-short 6144 --verify-top 3 --n-sim-req 30000`，记下报告里「Best (simulated)」的 (γ, n_s, n_l, total, 年成本, 两池 P99)。
2. **交叉验证**：把推荐的 n_s/n_l 喂给 `vllm-sr-sim simulate --cdf data/azure_cdf.json --lam 200 --n-s <推荐 n_s> --n-l <推荐 n_l> --b-short 6144 --n-req 50000`，用完整事件 DES 复核舰队 P99，与 `optimize` 的 DES P99 对比（口径接近、请求数不同，应有合理偏差）。
3. **横评硬件**：加 `whatif --gpu-compare a100 h100 a10g`，判断预算该买 H100（快但贵）还是 A100（慢但便宜）。
4. **验证路由策略**：用上一步定下的 (n_s, n_l) 跑 `compare-routers`，对比池路由与 C&R 压缩路由的 P99/SLO 差距。
5. **复盘**（写入学习笔记）：
   - 工作负载 CDF 的形状（短请求占比）如何决定 α_base，进而决定 n_s/n_l 的比例？
   - GPU 画像的 W/H/max_slots/cost_per_hr 如何同时影响 GPU 数和年成本？
   - `optimize`（分析+轻量 DES）与 `simulate`（完整事件 DES）的 P99 为何可能不同？哪个更可信？

如果环境允许，再试 `vllm-sr-sim serve` 起服务，在浏览器打开 `http://localhost:8000/api/docs` 看 OpenAPI，理解 fleet-sim 作为 dashboard sidecar 的服务形态。

> 提示：看 `summary` JSON 里的 `mean_utilisation`——若某池利用率贴近 ρ_max=0.85，说明该池是瓶颈，调参时应优先扩容它。

## 6. 本讲小结

- **定位**：fleet-sim（`vllm-sr-sim`）是**离线**的 GPU 舰队容量规划与路由策略评估工具，与运行时 SR 路由器是「规划 vs 在线」的关系；可作 CLI 或 dashboard 的 HTTP sidecar 运行。
- **三层组织**：`core/`（分层 DES 引擎 Fleet→Pool→Instance）、`gpu_profiles` + `workload`（硬件与流量建模）、`routing` + `optimizer`（可插拔路由与两阶段优化）。
- **DES 内核**：把单 GPU 建模为 M/G/c 队列，KV-cache 槽位当服务台，靠抢占最长序列还原 vLLM PagedAttention；物理完成时间（`s_raw`）与调度有效时间（`s_eff`）分离以兼顾真实性与排队一致性；TTFT = 排队等待 + prefill 时间。
- **建模解耦**：GPU 画像抽成 `GpuProfile` Protocol（W/H 线性延迟 + KV 几何 + 功耗 logistic 曲线），工作负载用经验 CDF + 泊松到达，二者可独立替换。
- **优化器两阶段**：解析（Erlang-C/Kimura）快速筛选 γ 空间，内联轻量 DES 只验证成本最低的 top-N，并用节点可用率 \(A\) 放大可靠性裕度。
- **职责边界**：`optimizer/base.py` 只做「给定 CDF/λ/GPU/SLO 求聚合两池最优舰队」的容量决策，不碰在线路由、不碰画像常数来源、不碰完整多池 DES、也不碰 disagg/grid-flex/tpw/threshold 等兄弟优化器。

## 7. 下一步学习建议

- **回到在线路由**：对照 u2-l1（信号-投影-决策心智模型）与本讲的 `SemanticRouter`，体会「离线 benchmark 路由策略 → 上线运行时路由」的闭环。
- **推理后端与成本**：阅读 u6-l3（模型运行时、库存与定价）与 u8-l4（嵌入提供者），看运行时 SR 如何用 `modelpricing` 做成本感知选择——与 fleet-sim 的成本最优化是互补的两层。
- **可观测性闭环**：阅读 u11-l4（可观测性），思考线上 TTFT/TPOT 指标如何反哺 fleet-sim 的 CDF 与 GPU 画像标定（把生产 trace 转成 CDF 再喂回仿真）。
- **扩展阅读源码**：想深入可继续读 `optimizer/disagg.py`（分离 prefill/decode 池）、`optimizer/grid_flex.py`（电网需求响应）、`gpu_profiles/builder.py`（从硬件+模型规格自动算画像），它们都是本讲三模块的延伸，也印证了 `base.py` 的职责边界。
