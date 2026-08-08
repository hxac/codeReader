# PD 分离与外部推理引擎

## 1. 本讲目标

本讲解决两个相互关联的「部署拓扑」问题：

1. **当 rollout 的瓶颈不在「一把梭」的均匀引擎上时**——比如多轮 agent 把 prompt 越拖越长、decode 耗时严重不均——如何把 prefill 和 decode 拆成两组各自调优的引擎？
2. **当推理根本不由 slime 训练任务拉起时**——比如推理由一个独立集群、独立 SGLang 环境托管——slime 如何只起一个 router 就「接管」这些已经跑起来的引擎，并继续完成权重同步？

读完本讲你应该能够：

- 理解 **PD 分离（Prefill-Decode disaggregation）** 为什么对多轮 / 长尾 agent 有吞吐收益，以及 slime 用 `--prefill-num-servers` 与 `--sglang-config` 两条路径描述它。
- 掌握 **会话亲和路由（session affinity / consistent_hashing）**：`session_id` 如何变成 `X-SMG-Routing-Key` 头，让同一会话的多次请求落到同一引擎以复用前缀缓存（prefix cache）。
- 认识 **外部引擎（external engines）** 模式：slime 用 `/server_info` 发现已运行的引擎，自己只起 router，把训练集群与推理集群彻底解耦。
- 能为一个「训练只跑 Megatron、推理由外部独立集群托管」的部署列出 slime 端必须配置的关键参数。

## 2. 前置知识

本讲建立在你已学完 **u8-l1（SGLang 拓扑与 sglang-config）** 的认知之上。请先回忆：

- **router（路由器）**：slime 用 SGLang 的 Model Gateway（`sgl-router`）做前端负载路由器，rollout 函数把请求发给 router，router 再转发给后端某个引擎。每个模型挂一个独立 router。
- **`SglangConfig` / `ModelConfig` / `ServerGroupConfig`**：`--sglang-config` YAML 的三层结构。`ServerGroupConfig.worker_type` 决定引擎角色，`num_gpus_per_engine` 经 \(\text{tp\_size}=\text{num\_gpus\_per\_engine}//\text{pp\_size}\) 推出 TP 大小。
- **权重同步（u5-l1/u5-l2）**：训练后把 Megatron 权重单向搬运并注入推理引擎，有 `full`/`delta` × `nccl`/`disk` 四象限。

本讲再补两个术语：

- **prefill / decode**：一次大模型生成的两个阶段。**prefill** 是「吃进整段 prompt、算出第一组 token」，**计算密集**（GPU 算力打满）；**decode** 是「逐 token 续写」，**访存密集**（反复读 KV cache，算力常闲置）。两者对 TP 大小、显存配比的最优解相反，所以拆开各自调优。
- **前缀缓存（prefix cache）**：SGLang 把已算过的 prompt 前缀的 KV cache 留在引擎里，下次同样前缀直接复用，跳过 prefill。多轮 agent 里「上一轮对话 + 新一句话」共享长长的前缀，缓存命中收益巨大。

一句话点题：**u8-l1 解决「slime 自己怎么描述并拉起复杂拓扑」，本讲解决「拓扑里最常用的 PD 形态」以及「拓扑不由 slime 拉起、只由 slime 接管」这两种进阶部署。**

## 3. 本讲源码地图

本讲涉及的文件按职责分成三组：

| 文件 | 作用 |
|------|------|
| `slime/backends/sglang_utils/sglang_config.py` | `from_prefill_num_servers`：把简单的 `--prefill-num-servers` 翻译成等价的 PD 配置；`has_pd_disaggregation` 探测某模型是否含 PD 组。 |
| `slime/ray/rollout.py` | 拓扑物化器：`_start_router` 在检测到 PD 时打开 `pd_disaggregation=True`；`start_rollout_servers` 的 external 分支走 `start_external_rollout_servers`；prefill 引擎额外分配 `disaggregation_bootstrap_port`。 |
| `slime/backends/sglang_utils/external.py` | **本讲主角**。`discover_external_engines` 用 `/server_info` 发现并推断外部引擎；`apply_external_engine_info_to_args` 把拓扑写回 args；`ExternalRolloutServer` 是只起 router 的「接管」占位对象。 |
| `slime/backends/sglang_utils/sglang_engine.py` | `_init_external`：外部引擎模式下 SGLangEngine 不拉子进程，而是去校验已运行引擎的 server_args 并把自己注册进 router。 |
| `slime/rollout/sglang_rollout.py` | 会话亲和路由的请求侧：给每个 sample 分配 `session_id`，在 `router_policy==consistent_hashing` 时塞进 `X-SMG-Routing-Key` 头。 |
| `slime/backends/sglang_utils/arguments.py` | 定义 `--prefill-num-servers` / `--sglang-config` 并做三组互斥校验。 |
| `slime/utils/arguments.py` | 定义 `--rollout-external-engine-addrs`，并在校验阶段触发外部引擎发现。 |
| `slime/ray/placement_group.py` | external 模式下 rollout 不占本地 GPU 槽位，placement group 只为 actor 预约卡。 |

一句话导航：**配置（PD）→ `rollout.py` 起带 `pd_disaggregation` 的 router；外部引擎 → `external.py` 发现后只起 router + 薄壳 SGLangEngine 接管。**

## 4. 核心概念与源码讲解

### 4.1 PD 分离拓扑：prefill 与 decode 拆分

#### 4.1.1 概念说明

默认情况下，一个 SGLang 引擎是 `regular` 类型——它同时做 prefill 和 decode。这在「短、单轮」任务上没问题，但 RL rollout 往往不是这样：

- **多轮 agent**：每一轮把上一轮的完整对话历史塞回 prompt，prompt 越滚越长，prefill 成本线性增长。
- **decode 长尾**：有些样本生成几千 token，decode 阶段把单个引擎拖很久，整批 rollout 的耗时被最慢的那条样本卡住。
- **资源需求相反**：prefill 想用**小 TP**（提高单卡吞吐，算力打满）；decode 想用**大 TP**（降低单 token 延迟，靠多卡分担访存）。

PD 分离就是把这两阶段拆给两组不同的引擎：`prefill` 引擎只处理 prompt、`decode` 引擎只负责续写。它们各自配 TP / 显存 / 上下文长度，独立扩缩容。slime 用 SGLang Model Gateway 的 `pd_disaggregation=True` 模式把两者接起来——router 把请求先发给 prefill 引擎算出首 token 和 KV cache，再把 KV cache 通过高速互联（如 NVLink/RDMA）「迁移」给 decode 引擎继续生成。

#### 4.1.2 核心流程

PD 拓扑从「声明」到「跑起来」的流程：

1. **声明拓扑**：用 `--sglang-config`（推荐）或简化的 `--prefill-num-servers`。两种方式最终都产出一份 `SglangConfig`，里面某模型的 `server_groups` 同时含 `prefill` 与 `decode` 组。
2. **解析校验**：`_resolve_sglang_config` 加载 YAML，断言「所有组 num_gpus 之和 == `--rollout-num-gpus`」。
3. **PD 探测**：`ModelConfig.has_pd_disaggregation` 检查该模型是否含 `prefill`/`decode` 组。
4. **起 router**：`_start_router(has_pd_disaggregation=True)` 把 `router_args.pd_disaggregation` 置 `True`，让 sgl-router 进入 PD 调度模式。
5. **分配端口**：prefill 引擎比 regular 多分一个 `disaggregation_bootstrap_port`，用于 prefill/decode 之间建立 KV 迁移通道。
6. **互斥约束**：同一模型内不许 `regular` 与 `prefill`/`decode` 混用；`--sglang-config`、`--prefill-num-servers`、`--rollout-external-engine-addrs` 三者两两互斥。

简单路径 `--prefill-num-servers N` 的语义是：把 `N` 个「每引擎占 `rollout_num_gpus_per_engine` 张卡」的引擎划给 prefill，剩余卡全部划给 decode。即：

\[
\text{prefill\_gpus}=N \times \text{rollout\_num\_gpus\_per\_engine},\qquad
\text{decode\_gpus}=\text{rollout\_num\_gpus}-\text{prefill\_gpus}
\]

#### 4.1.3 源码精读

**简化路径的翻译器** —— `from_prefill_num_servers` 把一个整数展开成 prefill+decode 两组配置：

[sglang_config.py:182-199](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_config.py#L182-L199)

这段代码说明：`--prefill-num-servers` 只是 `--sglang-config` 的语法糖，它构造一个名为 `default` 的单模型，含一个 `prefill` 组和一个 `decode` 组。注意第 188 行的 `assert decode_gpus > 0`——pre填不能用光所有卡，必须给 decode 留位置。

**PD 探测** —— `ModelConfig.has_pd_disaggregation` 是个轻量属性：

[sglang_config.py:102-104](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_config.py#L102-L104)

只要任意一个 `server_group` 的 `worker_type` 落在 `("prefill", "decode")` 里就为真。`start_rollout_servers` 正是用它决定要不要把 router 切到 PD 模式。

**router 切换 PD 模式** —— `_start_router` 是关键开关：

[rollout.py:1050-1059](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L1050-L1059)

第 1050-1051 行在 `has_pd_disaggregation` 为真时设置 `router_args.pd_disaggregation = True`，sgl-router 据此进入「prefill 先算、KV 迁移给 decode 续写」的调度。第 1056 行 `disable_circuit_breaker = True` 是 PD 专属容错调整：PD 的 KV 迁移走 RDMA，高负载下偶发 PCIe 争抢会导致迁移超时，router 默认会把超时的 decode worker 标记为「死」并熔断，但这只是瞬时压力不是真死机，所以关掉熔断器。

**prefill 引擎的额外端口** —— 物化引擎时，prefill 比 regular 多占一个引导端口：

[rollout.py:999-1000](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L999-L1000)

`disaggregation_bootstrap_port` 是 prefill 和 decode 两组引擎建立 KV 迁移通道时的握手端口，仅 prefill 工人需要。

**互斥校验** —— `--sglang-config` 与 `--prefill-num-servers` 不能同时用：

[arguments.py:183-185](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/arguments.py#L183-L185)

理由是 `--prefill-num-servers` 本质上是 `--sglang-config` 的子集，同时给两个会产生两份拓扑而冲突。官方建议新部署一律用 `--sglang-config`。

#### 4.1.4 代码实践

**实践目标**：用纯 CPU 单元测试验证 `from_prefill_num_servers` 的算式，加深对「整数 → 两组配置」的理解。

**操作步骤**（源码阅读型实践）：

1. 打开 `tests/utils/test_sglang_config.py`，找到针对 `from_prefill_num_servers` 的测试用例，读懂它构造的 `args`（`rollout_num_gpus`、`prefill_num_servers`、`rollout_num_gpus_per_engine`）。
2. 在本地写一段最小调用（**示例代码**，非项目原有代码）：

   ```python
   from types import SimpleNamespace
   from slime.backends.sglang_utils.sglang_config import SglangConfig

   args = SimpleNamespace(
       rollout_num_gpus=16,
       prefill_num_servers=1,
       rollout_num_gpus_per_engine=4,
   )
   cfg = SglangConfig.from_prefill_num_servers(args)
   for g in cfg.models[0].server_groups:
       print(g.worker_type, g.num_gpus)
   ```

**需要观察的现象**：打印应为 `prefill 4` 和 `decode 12`（即 \(1 \times 4\) 与 \(16 - 4\)）。

**预期结果**：把 `prefill_num_servers` 改成 `4`，会触发 `assert decode_gpus > 0` 失败（\(4 \times 4 = 16\)，decode 为 0），抛出 `No decode GPUs`。这印证了 prefill 不能吃光所有卡的约束。

> 待本地验证：若你环境里 `rollout_num_gpus_per_engine` 取值不同，按上面公式手算后与打印对照即可。

#### 4.1.5 小练习与答案

**练习 1**：为什么 PD 分离时，prefill 组倾向于用更小的 TP，而 decode 组倾向于用更大的 TP？

**参考答案**：prefill 是计算密集，单卡算力已被打满，增大 TP 只是把本可在单卡完成的算术摊薄到多卡、反而引入通信开销，所以小 TP（多引擎）吞吐更高；decode 是访存密集，单卡算力闲置，增大 TP 让多卡共同承担 KV cache 读取、降低单 token 延迟，所以大 TP 更优。

**练习 2**：`--prefill-num-servers 2 --rollout-num-gpus-per-engine 2 --rollout-num-gpus 8` 时，prefill 和 decode 各占几张卡？

**参考答案**：prefill = \(2 \times 2 = 4\) 张，decode = \(8 - 4 = 4\) 张。

---

### 4.2 会话亲和路由：consistent_hashing 与前缀缓存复用

#### 4.2.1 概念说明

PD 分离解决的是「单次请求内 prefill/decode 拆分」。会话亲和（session affinity）解决的是「多次请求间的前缀复用」，尤其针对多轮 agent：同一会话的第 2、3、4 轮请求都包含第 1 轮的完整历史，前缀高度重叠。

如果每轮请求都被随机路由到不同引擎，那么每个引擎都得重新 prefill 一遍同样的长历史——前缀缓存形同虚设。**会话亲和**保证同一会话的所有请求落到同一引擎，该引擎持有的 KV cache 在后续轮次直接命中，省掉重复 prefill。

slime 通过两个机制实现它：

- 给每个 sample 分配一个唯一 `session_id`；
- 当 `--router-policy consistent_hashing` 时，把 `session_id` 作为 `X-SMG-Routing-Key` HTTP 头传给 router，sgl-router 用**一致性哈希**把这个 key 确定性地映射到某个后端引擎。

一致性哈希的好处是：增删引擎时只有部分 key 需要重新映射，缓存大面积失效的风险被压到最低。

#### 4.2.2 核心流程

1. **分配 session_id**：`generate_and_rm_group` 进入时，给组内每个尚未有 `session_id` 的 sample 用 `uuid4` 生成一个。
2. **请求带头**：调用 SGLang `/generate` 时，若 `router_policy == consistent_hashing` 且 sample 有 `session_id`，则在请求头加 `X-SMG-Routing-Key: <session_id>`。
3. **router 路由**：sgl-router 对该 key 做一致性哈希，把同一 key 的请求固定到同一引擎。
4. **缓存复用**：该引擎保留前缀的 KV cache，后续同会话请求命中 prefix cache，跳过 prefill。

关键点：路由策略由 `--router-policy` 控制，可选 `round_robin` / `consistent_hashing` / `cache_aware`（默认）。只有选了 `consistent_hashing` 才会发送路由头。

#### 4.2.3 源码精读

**自动分配 session_id** —— 在整组采样前为每个 sample 盖戳：

[sglang_rollout.py:310-313](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L310-L313)

每个 sample 拿到一个独立的 UUID 作为会话标识。GRPO 里同一 prompt 的多条采样各自有独立 `session_id`（它们前缀相同但属于不同会话）。

**按策略发送路由头** —— 只在 consistent_hashing 下生效：

[sglang_rollout.py:194-198](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L194-L198)

注意第 197 行的双重条件：必须有 `session_id` **且** `router_policy == consistent_hashing` 才发头。这是 router 策略与请求侧的契约——选别的策略时这个头是多余的。

**router 策略参数** —— 三选一的 `--router-policy`：

参考 [docs/en/advanced/sglang-config.md:294-301](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/advanced/sglang-config.md#L294-L301) 描述的三种策略：`round_robin`（轮询）、`consistent_hashing`（会话亲和）、`cache_aware`（缓存感知，默认）。

> 补充：在 agent adapter 路径（u7-l2）里，`session_id` 还被用作 `X-SMG-Routing-Key` 把同一 agent 会话的多轮 turn 钉在同一引擎（见 `slime/agent/adapters/common.py:472`），与本讲机制同源。

#### 4.2.4 代码实践

**实践目标**：通过阅读代码确认「路由头只在 consistent_hashing 下发送」，并理解它对前缀缓存的影响。

**操作步骤**（源码阅读型实践）：

1. 读 `sglang_rollout.py:194-201`，确认 `headers` 在不满足条件时为 `None`，进而 `post(url, payload, headers=None)` 不带任何自定义头。
2. 对照 `slime/rollout/sglang_streaming_rollout.py:88-89`，确认流式路径里有完全相同的判定（`sample.session_id and router_policy == "consistent_hashing"`），说明这一契约在所有生成路径上一致。

**需要观察的现象**：当 `--router-policy` 不是 `consistent_hashing` 时，所有请求都不带路由 key，router 用默认策略（如 cache_aware）自由分配。

**预期结果**：你能用自己的话回答——「为什么多轮 agent 推荐配 `--router-policy consistent_hashing`？」（答案：让同一会话的后续轮次命中同一引擎的前缀缓存，避免重复 prefill 漫长的对话历史。）

#### 4.2.5 小练习与答案

**练习 1**：如果用默认的 `cache_aware` 而不是 `consistent_hashing`，多轮 agent 还能复用前缀缓存吗？

**参考答案**：能，但效率不稳定。`cache_aware` 会让 router 倾向于把请求发给「已有最多匹配前缀」的引擎，可是它不保证同一会话一定去同一引擎；当多个会话前缀相似时可能互相挤占、缓存抖动。`consistent_hashing` 用确定性哈希把会话钉死，命中率更稳。两者不互斥，可按负载取舍。

**练习 2**：为什么 `session_id` 要用 UUID 而不是用 prompt 的哈希？

**参考答案**：GRPO 里同一 prompt 会派生多条独立采样，它们前缀相同但属于「不同会话」，需要各自独立路由以分散负载；用 UUID 保证每条采样都是独立会话。若用 prompt 哈希，同一 prompt 的所有采样会全挤到一个引擎，失去并行度。

---

### 4.3 外部引擎发现：discover_external_engines 与 /server_info

#### 4.3.1 概念说明

到目前为止，所有 SGLang 引擎都由 slime 训练任务自己拉起。但生产中常见另一种形态：**推理服务由另一套系统独立部署和拥有生命周期**——可能是另一个 Ray 集群、手动预热好的 SGLang 进程、或第三方编排器托管的推理服务。这时 slime 不该再去拉引擎，而应该「连接」它们。

slime 用 `--rollout-external-engine-addrs host1:port1 host2:port2 ...` 描述这种部署。核心思路是：

- 外部引擎已经在跑，暴露了标准 SGLang HTTP 服务；
- slime 查询每个引擎的 `/server_info` 端点，**自动推断**它的 GPU 数、TP/PP/EP 并行规模、以及 worker 类型（`regular` / `prefill` / `decode`）；
- slime 据此把拓扑写回 args（`rollout_num_engines`、`rollout_num_gpus`），随后只起一个 router 并把这些引擎注册进去。

最大的工程价值在于**环境与硬件解耦**：外部引擎可以用完全独立的 SGLang 容器、独立集群，甚至不同型号 / 厂商的 GPU。slime 只依赖 HTTP 端点和所选权重同步通道（disk/nccl），不依赖训练侧的 Python / Megatron / Ray 环境。

#### 4.3.2 核心流程

外部引擎的「发现 → 接管」流程：

1. **传地址**：命令行给 `--rollout-external-engine-addrs`。
2. **校验阶段触发发现**：`slime_validate_args` 里设 `args.rollout_external = True`，并调用 `apply_external_engine_info_to_args`（非 `debug_train_only` 时）。
3. **逐个探测**：`discover_external_engines` 对每个地址归一化 → 查 `/server_info` 或 `/get_server_info` → 推断 `worker_type`、`num_gpus`、`tp/pp/ep` → 组装成 `ExternalEngineInfo`。
4. **写回 args**：把 `infos` 存进 `args.rollout_external_engine_infos`，设 `rollout_num_engines` 与 `rollout_num_gpus`（= 各引擎 GPU 之和）。
5. **起 router + 薄壳**：`start_external_rollout_servers` 只起 router，再为每个引擎建一个**不占 GPU**（`num_gpus=0`）的薄壳 SGLangEngine actor 去连接它、注册进 router。

#### 4.3.3 源码精读

**地址归一化与校验** —— 接受 `host:port` 或带 scheme 的 URL，强制要求 HTTP 且必须有 host 和 port：

[external.py:48-59](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/external.py#L48-L59)

没带 `://` 会自动补 `http://`，并去尾斜杠。IPv6 必须用方括号。这保证后续 `urlparse` 能稳定拆出 host 和 port。

**查询 server_info** —— 两个端点做兼容兜底：

[external.py:74-83](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/external.py#L74-L83)

新版 SGLang 用 `/server_info`，旧版用 `/get_server_info`，依次尝试，都失败才报错。这是 slime 适配多版本 SGLang 的典型手法。

**推断 worker_type** —— 这是 PD 与 external 的交汇点：

[external.py:86-92](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/external.py#L86-L92)

外部引擎是否算 PD 工人，完全由它自己上报的 `server_info` 决定：`encoder_only` → `encoder`；`disaggregation_mode` 为 `prefill`/`decode` → 对应类型；否则 `regular`。这意味着**外部引擎也可以是 PD 拓扑**，slime 会据此让 router 进入 PD 模式（见 4.4.3）。

**核心发现函数** —— 组装 `ExternalEngineInfo`：

[external.py:95-120](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/external.py#L95-L120)

注意第 105 行 `num_gpus` 的推断有三级回退：优先 `num_gpus`/`num_gpus_per_engine`，否则用 \(tp\_size \times pp\_size\) 兜底。第 106-107 行还抓取 PD 专用的 `disaggregation_bootstrap_port`。这些字段随后都进了不可变（`frozen=True`）的 `ExternalEngineInfo`。

**ExternalEngineInfo 的并行配置** —— 把 server_info 里多种可能的字段名归一：

[external.py:29-42](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/external.py#L29-L42)

`parallel_config` 同时容忍长名（`tensor_parallel_size`）和短名（`tp_size`），并把 `tp_size` 在缺省时回退到 \(num\_gpus // pp\_size\)。这反映 SGLang 字段命名在不同版本间漂移过，slime 必须都接得住。

**写回 args 并打印摘要** —— 校验阶段就完成发现：

[external.py:123-147](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/external.py#L123-L147)

第 133-135 行把推断结果写回 args：`rollout_num_engines` 取引擎个数，`rollout_num_gpus` 取各引擎 GPU 总和。这两步意味着——**外部模式下 `--rollout-num-gpus` 不用手填，slime 会按实际探测覆盖它**。

**触发点在 slime 主校验里** —— 见 [arguments.py:1842-1845](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1842-L1845)：第 1842 行定义 `rollout_external` 标志，第 1844-1845 行在非 `debug_train_only` 时立即跑发现，把外部引擎的存在性、可达性提前暴露在启动早期，而不是等到起服务才报错。

#### 4.3.4 代码实践

**实践目标**：模拟一次外部引擎发现，确认 slime 能从 `server_info` 推断出拓扑。

**操作步骤**（源码阅读 + 本地模拟型实践）：

1. 读 `discover_external_engines` 与 `get_server_info`，理清它需要哪些字段。
2. 本地起一个返回固定 JSON 的假 HTTP 服务（**示例代码**，非项目原有代码），返回一个 `regular` 引擎的 server_info：

   ```python
   # 用任意方式（如 python -m http.server 配合自定 handler）
   # 让 GET /server_info 返回：
   {"tp_size": 2, "pp_size": 1, "num_gpus": 2}
   ```

3. 调用（**示例代码**）：

   ```python
   from slime.backends.sglang_utils.external import discover_external_engines
   infos = discover_external_engines(["127.0.0.1:<你的端口>"])
   print(infos[0].worker_type, infos[0].num_gpus, infos[0].parallel_config)
   ```

**需要观察的现象**：`worker_type` 应为 `regular`（因为没有 `disaggregation_mode`），`num_gpus` 为 2，`parallel_config` 为 `{'tp_size': 2, 'pp_size': 1, 'ep_size': 1, 'moe_dp_size': 1}`。

**预期结果**：若把假返回改成 `{"disaggregation_mode": "prefill", "tensor_parallel_size": 2, "pipeline_parallel_size": 1, "num_gpus": 2, "disaggregation_bootstrap_port": 30100}`，则 `worker_type` 变 `prefill`、`is_pd_worker` 为 True、`disaggregation_bootstrap_port` 被抓出。

> 待本地验证：本实践需要能起本地 HTTP 服务的环境；若不具备，可改为纯阅读：对照 4.3.3 的字段回退表，说明为何同一份 server_info 在新旧字段名下都能被正确推断。

#### 4.3.5 小练习与答案

**练习 1**：为什么外部模式下不需要（也不该）手动设 `--rollout-num-gpus`？

**参考答案**：因为引擎已在外部跑起来，真实 GPU 数只有引擎自己最清楚。`apply_external_engine_info_to_args` 通过 `/server_info` 探测出每个引擎的 `num_gpus` 并求和覆盖 `rollout_num_gpus`，手填反而可能与实际不符、导致后续资源分配错误。

**练习 2**：`--rollout-external-engine-addrs` 与 `--sglang-config` 为什么互斥？

**参考答案**：它们拥有不同的生命周期边界——`--sglang-config` 让 slime **拥有并拉起**引擎，YAML 描述拓扑；`--rollout-external-engine-addrs` 让外部系统**拥有**引擎，slime 只负责发现与接管。同时给两者会产生两份互相冲突的拓扑。见 [arguments.py:179-181](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/arguments.py#L179-L181)。

---

### 4.4 ExternalRolloutServer：slime 只起 router 的「接管」模式

#### 4.4.1 概念说明

`discover_external_engines` 解决的是「知道有哪些引擎」。`ExternalRolloutServer` 与 `start_external_rollout_servers` 解决的是「如何把这些引擎编进 slime 的 rollout 体系」。

关键认识：**外部模式下 slime 完全不拉引擎子进程，只起一个 router**。为每个外部引擎创建的 `SGLangEngine` actor 是个「薄壳」——它 `num_gpus=0`（不占本地 GPU），不 `launch_server_process`，而是走 `_init_external`：去校验那个外部进程的 server_args 与 slime 期望一致，然后把自己注册进 router。这样从 router 视角看，外部引擎与 slime 自拉的引擎没区别，权重同步、abort、flush 等流程都能复用。

`ExternalRolloutServer` 本身是个**被动的数据容器**：它持有引擎句柄、GPU 计数与偏移、并行配置、router 地址，但容错与 offload 全是 no-op——因为外部引擎的生命周期归外部系统，slime 既不负责重启它们（`recover` 只打警告），也不能 offload/onload 它们占的卡（那些卡根本不在 slime 集群里）。

#### 4.4.2 核心流程

`start_external_rollout_servers` 的执行流程：

1. **读 infos**：从 args 取回校验阶段探测好的 `ExternalEngineInfo` 列表。
2. **起 router**：调 `start_router(args, has_pd_disaggregation=any(info.is_pd_worker ...))`——只要有任一外部引擎是 PD 工人，router 就切 PD 模式。
3. **逐引擎建薄壳**：为每个引擎建一个 `SGLangEngine` Ray actor（`num_cpus=0.2, num_gpus=0`），传 `worker_type`、`base_gpu_id=0`、`num_gpus_per_engine=info.num_gpus`。
4. **init 连接**：用 `external_engine_init_kwargs(info)` 拼参数，远程调 `engine.init`，让薄壳去校验并注册。
5. **登记路由表**：`args.sglang_model_routers = {"default": (router_ip, router_port)}`，让 rollout 函数能按模型名找到 router。
6. **placement group 只留 actor 卡**：外部引擎不占 slime 的 placement group 槽位。

权重同步方面，外部模式复用 u5 的四象限选型，但倾向 `disk`（尤其 `delta + disk`）：因为训练集群与外部推理集群往往跨机 / 跨 DC、无法组 NCCL，靠共享文件系统传 HF 检查点或增量 delta 最稳。注意 `delta` 模式不支持 `--colocate`（见 [docs/en/advanced/external-rollout-engines.md:103](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/advanced/external-rollout-engines.md#L103)）。

#### 4.4.3 源码精读

**ExternalRolloutServer 是被动容器** —— offload/recover 全是 no-op：

[external.py:150-182](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/external.py#L150-L182)

第 170 行 `recover` 明确「外部引擎不支持容错」，第 172-182 行 offload/onload/onload_weights/onload_kv 全返回空列表。这传递了一个清晰契约：slime 不拥有这些引擎，因此不假装能管理它们的显存或生命周期。

**只起 router + 薄壳引擎** —— `start_external_rollout_servers`：

[external.py:195-234](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/external.py#L195-L234)

三个要点：第 202 行按「是否有 PD 工人」决定 router 的 PD 模式（PD 与 external 的交汇）；第 213-223 行建的 actor `num_gpus=0`、`base_gpu_id=0`，纯属本地控制句柄，不消耗 slime 集群 GPU；第 228-234 行远程调 `init`，参数来自 `external_engine_init_kwargs`。

**薄壳的 init 参数** —— prefill 工人额外带引导端口：

[external.py:62-71](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/external.py#L62-L71)

普通引擎只给 `dist_init_addr`/`host`/`port`；prefill 引擎额外带 `disaggregation_bootstrap_port`，让薄壳能正确描述自己接管的那个 prefill 进程。

**薄壳引擎的真实连接动作** —— `_init_external` 不拉进程、只校验 + 注册：

[sglang_engine.py:169-187](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_engine.py#L169-L187)

第 169 行按 `rollout_external` 分流；`_init_external`（174-187）做两件事：把外部进程实际暴露的 server_args 与 slime 期望值逐字段比对（177-183，防止「你接的引擎跟你以为的不是同一个配置」），然后 `_register_to_router` 把它登记进 router。对应的，`shutdown` 在外部模式下直接 return（[sglang_engine.py:313-315](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_engine.py#L313-L315)）——slime 不会去杀不属于它的外部进程。

**placement group 不为外部引擎留卡** —— 只留 actor 的卡：

[placement_group.py:106-109](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/placement_group.py#L106-L109)

外部模式下返回 `(actor_num_gpus, actor_num_gpus)`——总卡数等于 actor 卡数，`rollout_offset` 也等于 actor 卡数，意味着 rollout 区段在本地不占任何 bundle。这与 4.4.1 的「引擎在远端」一致：slime 集群只放训练工人。

#### 4.4.4 代码实践

**实践目标**：理清一次 `update_weights` 在外部 + disk 模式下的端到端路径，确认 slime 只通过 HTTP 触发外部引擎换权重。

**操作步骤**（源码阅读 + 跟踪调用链型实践）：

1. 回顾 u5-l2 的 `UpdateWeightFromDiskDelta`（或 `UpdateWeightFromDisk`）：训练工人把权重写成检查点（或 delta）发布到共享目录。
2. 跟踪这些类最终调用的是外部引擎的 HTTP 端点 `update_weights_from_disk`（必要时先 `/pull_weights` 把检查点拉到每台主机本地）。
3. 结合 [docs/en/advanced/external-rollout-engines.md:58-80](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/advanced/external-rollout-engines.md#L58-L80) 的「Update From Disk」章节，确认这条路径**不要求训练 GPU 与推理 GPU 同型号、同厂商**，只要求双方看见同一共享文件系统路径。

**需要观察的现象**：整条链路里，slime 对外部引擎的全部操作都经由 HTTP 端点（`/server_info` 发现、`/generate` 推理、`update_weights_from_disk` 换权重、`/pull_weights` 拉检查点），没有 NCCL 直连（在 disk 模式下）。

**预期结果**：你能画出「训练集群（Megatron 写 checkpoint 到共享 FS）→ 外部集群（每台主机 `/pull_weights` 拉到本地 NVMe → `update_weights_from_disk` 热加载）」的数据流图，并指出 slime 在外部集群侧只需要 router + 薄壳 actor，不跑任何训练代码。

> 待本地验证：若你想确认 `/pull_weights` 端点确实来自 slime 的 sglang 补丁而非上游原生，可在 `docker/patch` 或 sglang 补丁目录里搜索该端点。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `ExternalRolloutServer.recover` 只打警告而不做任何恢复？

**参考答案**：外部引擎的生命周期由外部部署系统拥有，slime 既不知道也不该干预它们的重启策略。如果 slime 自作主张去「恢复」，可能与外部编排器的状态冲突。所以它选择明确声明「不支持容错」，把责任边界划清。

**练习 2**：外部模式下，slime 集群里仍要起哪些进程？它们各占多少 GPU？

**参考答案**：要起（1）一个 router（CPU 进程，不占 GPU）；（2）每个外部引擎一个薄壳 `SGLangEngine` actor（`num_gpus=0`，不占 GPU，仅作 HTTP 控制句柄）。训练工人（Megatron actor/critic）照常占 actor 卡。推理算力全部在外部集群，slime 集群的 GPU 只服务训练。

---

## 5. 综合实践

**任务**：设计一个「推理由外部独立集群托管、训练集群只跑 Megatron」的部署，写出 slime 端需要配置的关键参数并解释每条的作用。

**背景设定**：你在一个数据中心训一个千亿参数模型，但该中心 GPU 紧张，推理由合作伙伴的另一个集群（不同 S3 + NVMe）托管。两边能看见同一份共享对象存储，但组不成 NCCL 组。推理侧已用 `python -m sglang.launch_server` 在两台机器上各起了一个 SGLang 服务（`hostA:10090`、`hostB:10091`），都是 TP=8 的 regular 引擎。

**请输出**（可在笔记里完成）：

1. **最小启动命令的关键参数**（示例骨架，**示例代码**，非项目原有命令）：

   ```bash
   python train.py \
     --rollout-external-engine-addrs hostA:10090 hostB:10091 \
     --update-weight-mode delta \
     --update-weight-transport disk \
     --update-weight-disk-dir s3://shared/delta-updates \
     --update-weight-local-checkpoint-dir /local/nvme/rollout-ckpt \
     --hf-checkpoint /path/to/actor \
     ... (Megatron 模型参数、训练参数省略)
   ```

2. **逐条解释**：
   - `--rollout-external-engine-addrs`：声明外部引擎地址，触发 `/server_info` 发现，slime 据此推断 `rollout_num_gpus=16`（2 × 8），不再需要 `--rollout-num-gpus`。
   - `--update-weight-transport disk`（而非 `nccl`）：跨集群组不成 NCCL 组，只能走共享文件系统。
   - `--update-weight-mode delta`（而非 `full`）：千亿模型全量检查点太大，每轮全量传会让权重同步 dominates 训练循环；delta 只传变化字节。
   - `--update-weight-disk-dir`：双方必须看见同一路径；引擎侧经 `/pull_weights` 把发布物拉到本地再热加载。
   - `--update-weight-local-checkpoint-dir`：让每台引擎主机先拉到本地 NVMe，避免每个 rank 都读共享存储（对象存储后端时尤其重要）。
   - 注意：**不能**加 `--colocate`（外部 + delta 不支持 colocate，见部署清单第 103 行），也**不能**同时给 `--sglang-config`（互斥）。

3. **对照检查**：打开 [docs/en/advanced/external-rollout-engines.md:95-104](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/advanced/external-rollout-engines.md#L95-L104) 的 Deployment Checklist，逐条核对你的设计是否满足（地址可达、环境独立、共享路径双方可见、容错归属外部、互斥规则）。

**预期结果**：你能说清「slime 集群这一侧只起 router + 薄壳 actor + Megatron 工人，所有推理算力和 SGLang 环境都在外部集群；权重每轮经 delta + 共享 FS 单向流过去」。如果合作伙伴的引擎换成 PD 拓扑（prefill/decode 分离的独立进程），你只需把它们地址一起塞进 `--rollout-external-engine-addrs`，slime 会从各自 `server_info` 的 `disaggregation_mode` 自动识别并切 router 的 PD 模式——这正是本讲三个模块 PD、发现、接管 的合流之处。

## 6. 本讲小结

- **PD 分离**把计算密集的 prefill 与访存密集的 decode 拆成两组引擎，各自配 TP / 显存，靠 router 的 `pd_disaggregation=True` 调度、prefill 引擎额外的 `disaggregation_bootstrap_port` 建 KV 迁移通道；`--prefill-num-servers` 是 `--sglang-config` 的简化语法糖，两者互斥。
- **会话亲和**用 `--router-policy consistent_hashing` 配合 `session_id` → `X-SMG-Routing-Key` 头，把同一会话钉在同一引擎以复用前缀缓存，对多轮 / 长尾 agent 尤其重要；只有该策略下才发路由头。
- **外部引擎发现** `discover_external_engines` 通过 `/server_info` 自动推断每个外部引擎的 GPU 数、TP/PP/EP 与 worker_type（含 PD 识别），并覆盖 `rollout_num_gpus`，做到「不用手填拓扑」。
- **ExternalRolloutServer 接管模式**下 slime 只起 router 和 `num_gpus=0` 的薄壳 SGLangEngine（`_init_external` 校验 + 注册），引擎的容错与显存全归外部系统；placement group 只为 actor 留卡。
- **环境与硬件解耦**是外部模式的最大价值：外部 SGLang 可用独立容器、独立集群、甚至不同 GPU 厂商；权重同步倾向 `delta + disk`，跨集群 / 跨 DC 靠共享文件系统而非 NCCL。
- **三条互斥线**：`--sglang-config`、`--prefill-num-servers`、`--rollout-external-engine-addrs` 两两不能同用；此外 `delta` 模式不支持 `--colocate`。

## 7. 下一步学习建议

- **接 u8-l3（参数体系全景）**：本讲反复出现的 `--rollout-external-engine-addrs`、`--router-policy`、`--update-weight-*` 都是经过 `parse_args` 三阶段合并与校验的，下一讲会讲清这些参数如何从命令行流到 SGLang `ServerArgs` 与 `RouterArgs`，以及在 `slime_validate_args` 里何时触发外部引擎发现。
- **回看 u5-l2（三种权重传输）**：本讲的「delta + disk 跨集群同步」正是 `UpdateWeightFromDiskDelta` 的三态机与 xor/overwrite 编码在外部部署上的应用，建议对照阅读，理解 baseline 快照如何支持跨 DC 的增量链。
- **延伸阅读**：[docs/en/advanced/external-rollout-engines.md](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/advanced/external-rollout-engines.md) 末尾引用的 Composer 2 报告描述了「训练 / 推理异步、权重写共享 S3、delta 压缩、跨区下载重建」的同款生产形态，可作为本讲拓扑的工业印证。
