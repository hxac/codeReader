# 智能体 RL 路线图与接口选择

> 单元 U7 · 智能体 RL 与高级数据流 · 第 1 讲
> 依赖讲义：u6-l2（自定义生成函数 custom-generate）

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清楚 slime 为什么**不另起一个「agent 框架」**，而是把多轮工具调用、沙箱交互、环境反馈直接接入已有的「采样→训练→权重同步」闭环。
2. 面对「带沙箱执行的代码 agent」「多轮检索问答」「多智能体」这类需求，能从路线图里选出正确的**接口组合**（默认走 `--custom-generate-function-path` + `--custom-rm-path`，必要时才升级到 `--rollout-function-path`）。
3. 理解 agent 工作流虽然「说」的是字符串、工具调用、环境事件，但**训练目标始终是 token-based 的**：保留模型真实采样的 token id，用 `loss_mask` 把「可训练的模型输出」与「不可训练的环境/工具文本」分开。
4. 知道一次 agent 执行可以 fan-out 成多个训练段（subagent / 压缩分段），并说出兄弟样本必须共享同一个 `rollout_id` 的契约。

本讲是 U7 单元的**导览课**：它给整张地图和决策树，具体的 adapter 内部、轨迹分段、流式异步细节留给 u7-l2 ~ u7-l4 展开。

## 2. 前置知识

阅读本讲前，你需要具备以下认知（来自前置讲义）：

- **三模块闭环**（u2-l1）：rollout 用当前模型生成数据 → data buffer → training 消费 → 同步权重回 rollout。本讲要解决的问题是「agent 这种复杂的数据生成方式，从闭环的哪个口子接进去」。
- **Sample 数据结构**（u3-l1）：`Sample` 是贯穿闭环的数据载体，关键字段有 `tokens` / `response_length` / `loss_mask` / `rollout_log_probs` / `reward` / `rollout_id` / `status`。
- **自定义生成函数 custom-generate**（u6-l2）：`--custom-generate-function-path` 只替换默认 rollout 流水线**最底层**的「单样本生成」工位，签名是 `async def custom_generate(args, sample, sampling_params) -> Sample | list[Sample]`，由 `load_function` 解析。本讲就是在此基础上讨论 agent 场景。
- **rollout-function 与四个主接口的层级关系**（u6-l1）：`--data-source-path`（原料）→ `--rollout-function-path`（整条流水线）→ `--custom-generate-function-path`（生成工位）+ `--custom-rm-path`（打分工位）。换外层会让内层挂载点失效，所以**优先用工位接口**。

几个对初学者可能陌生的术语，先解释清楚：

| 术语 | 含义 |
| :--- | :--- |
| **agent / 智能体** | 能多轮调用工具、读写沙箱、根据环境反馈继续决策的语言模型工作流。它「说」的不再是「prompt 进、response 出」一句话，而是「prompt → 生成 → 调工具 → 拿观测 → 再生成 → …」的循环。 |
| **沙箱（sandbox）** | 一个隔离的执行环境（如容器、E2B），让 agent 在里面跑代码、改文件，而不污染宿主机。 |
| **token-based 训练目标** | RL 优化的对象始终是「逐 token 的对数概率」，而不是字符串。所以无论 agent 内部用什么协议通信，最终落到 `Sample.tokens` 上的必须是真实采样的 token id。 |
| **loss_mask** | 与回复等长的 0/1 数组。模型自己生成的 token 标 1（参与梯度），工具/环境注入的 token 标 0（不参与梯度）。 |
| **fan-out（扇出）** | 一次 agent 执行产生**多个**可训练段，最终 `generate` 返回 `list[Sample]` 而不是单个 `Sample`。 |

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| :--- | :--- |
| `docs/en/get_started/agent.md` | **智能体 RL 路线图**：决策表「目标→推荐入口」、推荐集成范式、agent 运行时 adapter、serving 与性能建议、参考示例索引。本讲的「地图」就来自这里。 |
| `docs/en/get_started/customization.md` | **定制化接口总览**：21+ 个 `--xxx-path` 接口表，以及「智能体工作流如何接入」小节、custom-generate fan-out 契约与代码示例。 |
| `slime/utils/types.py` | `Sample` dataclass：`loss_mask`、`rollout_id` 字段，以及维护 token/logp/loss_mask 三者对齐的 `append_response_tokens` 方法。这是「token-based 训练目标」的落点。 |
| `slime/rollout/sglang_rollout.py` | 默认 rollout 流水线：`generate_and_rm` 内部对 `custom_generate` 的分发逻辑，以及 `generate` 返回 `list[Sample]` 时如何逐样本算奖励。 |
| `slime/utils/misc.py` | `load_function`：把 import 路径字符串解析成函数对象的 4 行核心实现。 |
| `examples/search-r1/generate_with_search.py` | **多轮检索 agent** 示范：`max_turns` 循环里交替「生成（loss_mask=1）→ 搜索（loss_mask=0）」，是理解 token-based 训练目标的最佳范本。 |
| `examples/coding_agent_rl/generate.py` | **带沙箱的代码 agent** 示范：四段式编排（准备沙箱→跑 harness→取 git diff→跑测试打分→finish_session 导出样本），并演示 fan-out。 |
| `slime/agent/adapters/common.py` | `BaseAdapter` 协议适配器基类：`open_session` / `finish_session` 生命周期，`call_sglang_generate` 把「消息进、采样 token 出」落到实处。 |

## 4. 核心概念与源码讲解

### 4.1 智能体 RL 路线图：把 agent 接入闭环而非另起框架

#### 4.1.1 概念说明

很多 RL 训练框架面对「多轮工具调用」「沙箱执行」这类 agent 场景时，会**单独造一个 agent 子系统**，和主训练循环割裂。slime 的设计哲学恰恰相反：它的文档在 [agent.md](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/agent.md) 一开篇就点明——slime 适合做 agent 训练的优势，正是「**高性能训练 + SGLang rollout 服务 + 可插拽数据生成接口**」三者的结合，而不是另起炉灶。

换句话说，agent 工作流（多轮工具、沙箱、环境反馈、基于测试的奖励）是**一类特殊的数据生成工作流**，它通过**已有的定制化接口**插进闭环，slime 并不要求你学一套新的 agent 框架。这一点 [customization.md](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/customization.md) 说得最直白：

> Agentic workflows ... are an important class of data generation workflows. They plug into slime through the existing customization interfaces; **slime does not require a separate agent framework**.

这条路线的内核是 u6-l1 已经建立的「骨架写死、血肉可换」原则——「采样→训练→权重同步」的骨架是固定的，agent 的复杂性全部塞进**可替换的数据生成函数**里。

#### 4.1.2 核心流程：一张决策表

下面这张表综合了 agent.md 的「Where To Start」和 customization.md 的「智能体工作流」两张决策表，是本讲最该记住的东西：

| 你要做的事 | 选哪个接口 | 备注 |
| :--- | :--- | :--- |
| 为每个样本跑自定义 agent 循环、工具调用、RAG、沙箱/浏览器/终端交互、多轮生成 | `--custom-generate-function-path` | **首选**，复用默认 rollout 外层循环 |
| 算验证器奖励、基于测试的奖励、环境成功判定、规则奖励、调外部奖励服务 | `--custom-rm-path` | 与 custom-generate 配对，是 agent 默认组合 |
| 一个 prompt 的 rollout 拆成多个可训练段（subagent、压缩分段） | custom-generate 返回 `list[Sample]`（fan-out） | 兄弟样本必须共享 `rollout_id` |
| 替换整条 rollout 编排（自定义调度、跨 rollout 后台队列、全异步生成） | `--rollout-function-path` | **最后手段**，默认循环不够用时才用 |
| 控制任务采样、缓冲、重排队、自定义 prompt 源 | `--data-source-path` | 与生成解耦 |
| 长尾 agent rollout 不想阻塞训练 | `--rollout-function-path` 全异步（见 examples/fully_async） | 长尾样本耗时差异大时考虑 |

**默认组合**（customization.md 反复强调）：绝大多数 agent 任务，**从 `--custom-generate-function-path` 加 `--custom-rm-path` 开始**，只有当默认 rollout 循环不够用时，才覆盖整个 rollout 函数。

#### 4.1.3 源码精读：路线图原文

决策表的两处源头如下。第一处是 agent.md 的「Where To Start」表，它把常见 agent 目标直接映射到入口接口：

[docs/en/get_started/agent.md:7-27](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/agent.md#L7-L27) — 这段先给「目标→推荐入口」表，紧接的「Recommended Integration Pattern」小节给出本讲的三个核心结论：

1. 大多数 agent 任务应从 `--custom-generate-function-path` 开始，把一次 agent 执行转成可训练 `Sample`。
2. 训练目标应保持 **token-based**：保留模型采样的 token id，用 `loss_mask` 区分可训练的模型输出与 prompt/模板/工具观测/环境文本。
3. 一次 rollout 拆成多段时返回 `list[Sample]`，所有兄弟样本设同一个 `rollout_id`，slime 会把它们当成同一 rollout 一起切分和聚合 loss，而不会重复计数。

第二处是 customization.md 的「智能体工作流」小节，它把同样的决策落成接口表：

[docs/en/get_started/customization.md:32-47](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/customization.md#L32-L47) — 注意它在末尾点名了三个原生示例：`examples/search-r1`（多轮工具，走 custom-generate）、`examples/multi_agent`（多智能体，同样走 custom-generate 并 fan-out）、`examples/fully_async`（长尾 agent 生成，走全异步 rollout）。

#### 4.1.4 代码实践：把决策表用起来

**实践目标**：熟练使用决策表，给具体需求选对接口。

**操作步骤**：

1. 阅读上面两张源码表（agent.md L7-L27、customization.md L32-L47）。
2. 为下面 4 个需求各选一个接口，并写一句话理由：
   - (a) agent 在沙箱里改完代码，要跑单元测试决定奖励。
   - (b) 一次 agent 跑得很慢，想让它和下一轮训练重叠。
   - (c) agent 在第 3 轮触发了上下文压缩，压缩前后都想训练。
   - (d) 你的 prompt 来自一个需要按难度排序的自定义数据集。

**需要观察的现象**：（纯阅读型实践，无运行现象）

**预期答案**：
- (a) `--custom-rm-path`（基于测试的奖励）。
- (b) `--rollout-function-path`（全异步），因为长尾 agent 要用 `examples/fully_async` 的模式，光靠 custom-generate 仍受默认循环调度约束。
- (c) custom-generate 返回 `list[Sample]`（fan-out），压缩前后作为两个段、共享 `rollout_id`。
- (d) `--data-source-path`（自定义 prompt 源/调度）。

#### 4.1.5 小练习与答案

**练习 1**：agent.md 说「slime does not require a separate agent framework」。请用一句话解释，假如 slime 真的另起了一个 agent 子系统，会和当前设计在「权重同步」上产生什么矛盾？

**答案**：另起的 agent 子统若独立生成数据，就脱离了 slime 的 on-policy 闭环——它手里的数据是用旧权重采的，训练后权重单向同步回 rollout 的回路就会断在「agent 子系统」这一层，无法保证采样数据来自当前策略。

**练习 2**：决策表里 `--custom-generate-function-path` 和 `--rollout-function-path` 都是「改生成」，为什么默认优先选前者？

**答案**：custom-generate 只替换最底层的「单样本生成」工位，外层的过采样补数、动态过滤、奖励计算、abort 机制全部保留且自动可用；换成 rollout-function-path 等于接管整条流水线，这些能力都得自己重写，成本高、易出错。只有当默认循环的逐样本结构根本装不下你的工作流（如跨 rollout 后台队列、全异步），才值得升级。

---

### 4.2 custom-generate 入口：把一次 agent 执行变成可训练 Sample

#### 4.2.1 概念说明

决策表告诉我们首选 `--custom-generate-function-path`，那它到底在闭环里扮演什么角色？回顾 u6-l2：custom-generate 是默认 rollout 流水线（`sglang_rollout.py` 的 `generate_and_rm`）**最底层**的工位。它只负责「拿到一个 prompt、跑完一次 agent 执行、产出可训练的 `Sample`」——而**调度、过采样、奖励计算、过滤**都由外层默认逻辑包办。

对 agent 场景，这意味着：你只需要把「一次 agent 执行」翻译成 slime 看得懂的 `Sample`（填好 `tokens`、`response_length`、`loss_mask`、`status`，奖励可自己填或交给 `--custom-rm-path`），其余一概不用管。

#### 4.2.2 核心流程：custom-generate 在默认流水线里的接驳点

```
RolloutManager.generate
  └─ generate_rollout（默认外层循环：过采样补数 + 动态过滤）
       └─ generate_and_rm_group
            └─ generate_and_rm（中层：限流 + 奖励）
                 └─ 【custom-generate 在这里被调用】  ← 你的 agent 逻辑
                      └─ 返回 Sample 或 list[Sample]
                 └─ apply_rollout_sample_hooks（样本钩子）
                 └─ 若返回 list[Sample]：逐样本算奖励（batched_async_rm）
                 └─ 若返回单个 Sample 且 reward 为空：单样本算奖励（async_rm）
```

关键点是：custom-generate 的返回值会**原样向上传**。返回单个 `Sample`，走单样本奖励路径；返回 `list[Sample]`（fan-out），框架会逐样本补奖励（见 4.4）。奖励可以由框架算，也可以在 custom-generate 内部直接填好 `sample.reward`——若已填，框架就不再重复计算。

#### 4.2.3 源码精读：分发逻辑

`custom_generate` 的真实接驳点在 `sglang_rollout.py` 的 `generate_and_rm` 里。这里有两段最关键：

[slime/rollout/sglang_rollout.py:248-260](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L248-L260) — 分发逻辑：先用 `sample.generate_function_path`（逐样本，可来自 eval 数据集配置）压过全局 `args.custom_generate_function_path`；若指定了自定义路径，就用 `load_function` 解析成函数并 `await` 调用，否则退回内置 `generate`。注意它通过 `inspect.signature` 检测函数是否声明了 `evaluation` 形参，借此区分训练/评估调用——你的 custom-generate **可选**地声明这个形参。

紧接着是 fan-out 的奖励处理：

[slime/rollout/sglang_rollout.py:268-278](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L268-L278) — 当 custom-generate 返回的是 `list[Sample]`（`isinstance(sample, list)`），框架挑出 `reward is None` 的样本用 `batched_async_rm` 批量算奖励并写回。这就是「fan-out 也能享受默认奖励」的实现。

而把 import 路径字符串变成函数对象的，是只有 4 行核心逻辑的 `load_function`：

[slime/utils/misc.py:38-47](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/misc.py#L38-L47) — 用 `rpartition(".")` 切出最后一点作属性名、其余作模块路径，`importlib.import_module` 导入后 `getattr` 取属性，加 `@cache` 记忆化。所有 `--xxx-path` 接口（包括 custom-generate、custom-rm、rollout-function）都走这一条路，所以**你的 agent 代码只需是个可 import 的模块，命令行填它的 import 路径即可**。

#### 4.2.4 代码实践：读 search-r1 的多轮循环

**实践目标**：通过真实示例看清「custom-generate = 一个 async 函数 + 一个多轮循环」。

**操作步骤**：

1. 打开 [examples/search-r1/generate_with_search.py:145-274](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/search-r1/generate_with_search.py#L145-L274)，定位 `async def generate(args, sample, sampling_params) -> Sample`。
2. 注意 L179 的 `for _turn_idx in range(SEARCH_R1_CONFIGS["max_turns"]):`——这就是 agent 的多轮循环。
3. 注意 L173 的 `_stop_tags = ["</search>", "</answer>"]`：注释（L164-L172）解释了为什么要强制推理引擎在工具/答案边界停止——否则引擎会在闭合标签后继续吐垃圾 token，既被错误训练又破坏格式奖励。
4. 这个文件末尾 L277-293 还附了一个 `reward_func`，对应 `--custom-rm-path`，示范了「custom-generate + custom-rm」的标准组合。

**需要观察的现象**：（源码阅读型实践，无运行现象）

**预期结果**：你会得出结论——search-r1 的 `generate` 本质就是「在默认 rollout 外层循环里，把最底层的单样本生成换成了一个带搜索工具的多轮循环」，外层的过采样、过滤、奖励统计全都还在。这正是 4.1 决策表里「首选 custom-generate」的活样本。

#### 4.2.5 小练习与答案

**练习 1**：你的 custom-generate 已经在内部根据 agent 执行结果算好了奖励并写进 `sample.reward`。框架还会再算一次奖励吗？

**答案**：不会。看 sglang_rollout.py L273 与 L283：框架只对 `reward is None` 的样本算奖励。你填好之后它跳过。

**练习 2**：为什么 custom-generate 的函数可以**选择**声明 `evaluation` 形参？这个机制是怎么实现的？

**答案**：因为 sglang_rollout.py L255 用 `inspect.signature(custom_generate_func).parameters` 检测函数签名里有没有 `evaluation`；有就传 `evaluation=evaluation`，没有就只传 `(args, sample, sampling_params)`。这让同一个函数既能区分训练/评估，又能保持简单签名，是「契约可选扩展」的典型做法。

---

### 4.3 token-based 训练目标与 loss_mask

#### 4.3.1 概念说明

这是本讲最容易被忽视、却最关键的一条原则。agent 工作流内部可能用各种花哨的形式通信——字符串、chat 消息、工具调用 JSON、环境观测、框架事件——但 **slime 的训练目标始终是 token-based 的**。也就是说，最终落到 `Sample.tokens` 上、喂给 Megatron 算梯度的，必须是**模型真实采样的 token id 序列**。

为什么必须如此？因为 RL 优化的是「逐 token 的对数概率」。如果你把 agent 的字符串输出拿去重新分词（re-tokenize），新得到的 token 序列可能和引擎真实生成的 token 不一致，于是：

- token 与它对应的 `rollout_log_probs`（行为策略对数概率）对不齐，重要性采样修正（TIS/off-policy）就全错了；
- 训练的 token 序列根本不是模型当时「真实说过的话」，梯度方向就错了。

因此 agent.md 反复强调：**保留模型采样的 token id**，用 `loss_mask` 把序列里「哪些是模型生成的（可训练）、哪些是 prompt/模板/工具观测/环境文本（不可训练）」分开。这正是 search-r1 示例里那段大段注释（L93-L98）警告的事：**收集 logp 时绝不能对引擎返回的字符串做后处理**。

#### 4.3.2 核心流程：loss_mask 怎么标注一次多轮 agent 轨迹

一次多轮 agent 执行产出的回复序列，是「模型生成」与「环境注入」交替的：

```
回复序列： [模型生成段1] [工具观测1] [模型生成段2] [工具观测2] ... [模型生成段N]
loss_mask：  1 1 1 1 1    0 0 0 0     1 1 1 1 1    0 0 0 0       1 1 1 1 1
rollout_log_probs：真实logp 真实logp... 填0 填0... 真实logp... 填0... 真实logp...
```

规则只有两条：

1. **模型生成的 token**：`loss_mask=1`，必须带上引擎返回的真实 `rollout_log_probs`。
2. **工具/环境注入的 token**：`loss_mask=0`，`rollout_log_probs` 填 0（占位，保持长度对齐，但因为 mask=0 不会进梯度）。

这套对齐不是靠你手写维护的——`Sample.append_response_tokens` 方法把 token、logp、loss_mask 三者的一致性封装好了。

#### 4.3.3 源码精读：append_response_tokens 与 loss_mask 字段

先看 `loss_mask` 字段本身和它的「长度不变量」：

[slime/utils/types.py:119](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/types.py#L119) — `loss_mask: list[int] | None = None`，与回复序列等长。

[slime/utils/types.py:418-425](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/types.py#L418-L425) — `_validate_response_metadata_lengths` 强制 `loss_mask`、`rollout_log_probs` 的长度都等于 `response_length`。三者一旦不对齐直接抛错，杜绝「token 与 logp 错位」这类隐蔽 bug。

真正干活的 `append_response_tokens`：

[slime/utils/types.py:253-281](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/types.py#L253-L281) — 方法签名的关键字参数 `trainable: bool = True` 就是 4.3.2 规则的开关。看它内部的校验：
- L276-L277：`trainable=True` 的 token **必须**带 `log_probs`，否则抛错（「trainable response tokens require rollout log probabilities」）。
- L278-L281：`trainable=False` 的 token **禁止**带 `log_probs`，框架自动填 `[0.0] * len(tokens)`。

[slime/utils/types.py:286-302](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/types.py#L286-L302) — 真正写 `loss_mask` 的两行在 L292：`self.loss_mask += [1 if trainable else 0] * len(tokens)`，以及 L302 追加 logp。模型生成走 `1`，环境文本走 `0`，完全机械化，不可能标错。

再看 search-r1 是怎么用它的——这是「交替标注」的最清晰范例：

[examples/search-r1/generate_with_search.py:217-244](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/search-r1/generate_with_search.py#L217-L244) — 一个循环回合里：
- L219 `loss_mask += [1] * len(cur_response_token_ids)`，L220-L226 用 `append_response_tokens(..., trainable=True, log_probs=..., meta_info=...)` 记录模型生成段；
- L243 `loss_mask += [0] * len(obs_tokens_ids)`，L244 用 `append_response_tokens(args, tokens=obs_tokens_ids, trainable=False)` 记录搜索观测段。

两段对照，正是 4.3.2 流程图的一次代码落地。注意 L240 的观测 token 是用 `tokenizer(next_obs)` 自己分词的（因为观测是工具返回的纯文本，模型从没「说过」），而模型生成段用的是引擎 `output_token_logprobs` 里直接取的 token id（L209）——这就是「保留模型真实采样 token」的体现。

#### 4.3.4 代码实践：手算一次多轮轨迹的 loss_mask

**实践目标**：用纸笔把一次两轮搜索轨迹的 token 序列、loss_mask、rollout_log_probs 三列对齐，彻底理解 token-based 训练目标。

**操作步骤**：

1. 假设模型第 1 轮生成 5 个 token（带真实 logp `[a,b,c,d,e]`），然后搜索引擎返回一段 4 token 的观测；第 2 轮模型再生成 3 个 token（logp `[f,g,h]`）。
2. 仿照 search-r1，写出三个等长列表：`response_token_ids`、`loss_mask`、`rollout_log_probs`。

**需要观察的现象**：三个列表长度是否相等？观测段的 logp 是什么？

**预期结果**：

```
response_token_ids : [t1,t2,t3,t4,t5, o1,o2,o3,o4, t6,t7,t8]
loss_mask          : [ 1, 1, 1, 1, 1,  0, 0, 0, 0,  1, 1, 1]   长度 12
rollout_log_probs  : [ a, b, c, d, e,0.0,0.0,0.0,0.0, f, g, h]  长度 12
```

三者都长 12。观测段的 logp 被填成 `0.0`（占位），但因 `loss_mask=0` 不会进梯度。这就是「token-based + loss_mask」的全部秘密。

#### 4.3.5 小练习与答案

**练习 1**：如果你图省事，把 agent 多轮的整段回复字符串拼起来重新 `tokenizer(text)` 得到 token，再统一赋 `loss_mask=[1]*N`，会有哪两个严重后果？

**答案**：①重新分词得到的 token 与引擎真实采样的 token 不一致，`rollout_log_probs`（来自引擎）和 token 序列错位，重要性采样/TIS 修正失效；②工具观测段被标成 `loss_mask=1`，模型会被迫「学习」它根本没生成过的环境文本，梯度方向错误。

**练习 2**：search-r1 为什么在收集 logp 时（`return_logprob=True`）禁用了 `postprocess_responses`（见 L93-L108）？

**答案**：`postprocess_responses` 会截断字符串（如切到 `</search>` 为止），改了字符串就没法对齐地截断 token/logp 数组，重新分词又会导致 token 变化。所以一旦要 logp，就只能用引擎原始 token、不能碰字符串——这也反过来说明「保留模型真实采样 token」是硬约束。

---

### 4.4 fan-out 与「何时升级到 rollout-function-path」

#### 4.4.1 概念说明

前面三节覆盖了「一次 agent 执行 → 一个 Sample」和「token-based 目标」。但 agent 场景里有一种常见情况：**一次 rollout 执行天然要拆成多个可训练段**。例如：

- **subagent**：主 agent 派出一个子 agent 跑一段轨迹，主、子轨迹都想训练。
- **上下文压缩（compaction）**：上下文太长被压缩，压缩前的「冻结链」和压缩后的「续写」各成一段。
- **多智能体**：一次执行产生多个 agent 的轨迹。

这时 custom-generate **不需要**升级到 rollout-function-path——它可以直接返回 `list[Sample]`，这叫 **fan-out（扇出）**。唯一的硬契约是：**同一次 rollout 产出的所有兄弟样本，必须共享同一个 `rollout_id`**。

那到底什么时候才该升级到 `--rollout-function-path`？agent.md 给的判据是：**只有当你需要替换整条 rollout 编排时**——比如自定义数据源调度、跨 rollout 的后台队列、全异步生成，或者工作流根本塞不进默认 `sglang_rollout` 的「逐样本」结构。

#### 4.4.2 核心流程：fan-out 契约与 reward 分摊

fan-out 的两个要点：

1. **rollout_id 同源**：`Sample.rollout_id` 默认为 `None`，下游回退到 `index`（所以默认路径里「一次执行=一个样本」时 `rollout_id == index`）。当你拆成多段时，必须显式给所有兄弟设同一个 `rollout_id`，slime 才会把它们当作同一 rollout 来切分训练步、聚合 loss，而**不会**把它们算成多次独立 rollout（否则同一 rollout 的奖励被重复计数、loss 被放大）。
2. **reward 分摊**：若一条轨迹只有一个总奖励 `R` 却拆成 `K` 段，常见做法是每段分 `R / K`，避免同一 rollout 奖励被放大 `K` 倍。

升级到 rollout-function-path 的判据可以归结为一句话：**默认外层循环（过采样补数 + 动态过滤 + 逐样本调度）能否装下你的工作流？** 能 → 用 custom-generate（必要时 fan-out）；不能（要跨 rollout 后台队列、全异步、非逐样本结构）→ 用 `--rollout-function-path`。

#### 4.4.3 源码精读：rollout_id 字段、fan-out 示例与升级判据

先看 `rollout_id` 字段的官方注释，它把契约讲得很透：

[slime/utils/types.py:99-106](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/types.py#L99-L106) — 注释明确：默认路径下「一次执行=一个训练样本」，`rollout_id` 回退到 `index`；而「压缩/subagent 路径把一次执行拆成多个训练样本」时，**必须给每个兄弟设同一个 `rollout_id`**，这样 loss 聚合会在 rollout 内部平均，而不是把这次 rollout 重复计数。

customization.md 给的 fan-out 代码骨架正是对这条契约的落地：

[docs/en/get_started/customization.md:99-117](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/customization.md#L99-L117) — 示例把每个 segment 拷一份、填好各自的 token/loss_mask/reward，关键是 `rollout_id = sample.rollout_id if sample.rollout_id is not None else sample.index`，然后每个兄弟都设 `s.rollout_id = rollout_id`；末尾 L117 给出 reward 分摊建议「assigning `reward / K` to each segment」。

多智能体示例则展示了 fan-out 在真实代码里的样子：

[examples/multi_agent/rollout_with_multi_agents.py:16-33](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/types.py#L99-L106) — `generate_with_multi_agents` 声明返回 `list[Sample]`，内部把多 agent 执行的结果收集成 `samples` 列表后 `random.shuffle(samples)` 返回。注意它的接驳方式：根据 [examples/multi_agent/README.md](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/multi_agent/README.md)，它通过 `--custom-generate-function-path examples.multi_agent.rollout_with_multi_agents.generate_with_multi_agents` 接入——也就是说，**即使是多智能体这种复杂场景，slime 的默认推荐也仍是 custom-generate + fan-out，而非 rollout-function-path**。

（订正一处文档措辞：customization.md L47 把 multi_agent 描述为「`--rollout-function-path`-based」，但该示例的 README 与代码实际用的是 `--custom-generate-function-path`。以代码与 README 为准。）

带沙箱的代码 agent 示例（coding_agent_rl）演示了 fan-out 的最完整形态：

[examples/coding_agent_rl/generate.py:182-268](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/coding_agent_rl/generate.py#L182-L268) — `generate` 是四段式编排：准备沙箱（L203-L204）→ 跑 harness（L205-L212）→ 取 git diff（L213）→ 跑评估打分（L215-L219）→ 调 `state.adapter.finish_session(...)` 导出样本（L237-L245）。`finish_session` 返回 `samples`（一个 `list[Sample]`），它的中间件把一条轨迹切成 `subagent` / `wipe` / `final` 段——这正是 agent.md L69-L71 说的 agent fan-out 训练。注意失败/超时路径 L270-L280 统一走 `_abort_result`：

[examples/coding_agent_rl/generate.py:316-333](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/coding_agent_rl/generate.py#L316-L333) — `_abort_result` 把样本标记为 `ABORTED`、`remove_sample=True`、`loss_mask=[0]`、`reward=0.0`，并始终以 `list[Sample]` 形状返回（注释「fan-out generate function always yields」）。这说明：**一旦你声明了 fan-out，所有返回路径（包括异常）都必须保持 `list[Sample]` 形状**，否则上层 `isinstance(sample, list)` 分支会错乱。

最后是升级判据的原文：

[docs/en/get_started/agent.md:27](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/agent.md#L27) — 「Reach for `--rollout-function-path` only when you need to replace the whole rollout orchestration」，并列出常见理由：自定义数据源调度、跨 rollout 后台队列、全异步生成、塞不进默认逐样本结构的工作流。

#### 4.4.4 代码实践：用 adapter 的 finish_session 理解 fan-out 产出

**实践目标**：搞清「一次 agent 会话如何被导出成多个可训练 Sample」，为综合实践做铺垫。

**操作步骤**：

1. 读 [slime/agent/adapters/common.py:245-276](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/adapters/common.py#L245-L276) 的 `finish_session`：它先 `shutdown_session` 排空在途请求，再调 `self.manager.get_trajectory(sid, base_sample=..., reward=..., max_sample_tokens=...)` 把会话轨迹**线性化成多个 Sample**，最后用 tokenizer 解码每段的 `.response`。返回的是 `list`。
2. 对照 coding_agent_rl 的 L237-L245：`finish_session` 的返回值直接成了 `generate` 的 fan-out 结果，`reward` 被传进去由 trajectory manager 分摊到各段。
3. 注意 `finish_session` 是**幂等**的（L259 注释）：对已 pop 的 sid 再调返回 `[]`，这是 rollout 失败重试时的重要安全网。

**需要观察的现象**：（源码阅读型实践，无运行现象）

**预期结果**：你会看到 fan-out 的产出源头——adapter 把多轮会话树线性化成带 loss_mask 的多个训练段，每段都继承同一个 `base_sample`（从而同 `rollout_id`），reward 在 `get_trajectory` 里分摊。这正是 u7-l2、u7-l3 要深入讲的 adapter 与轨迹分段，本讲只点出它与 fan-out 契约的衔接。

#### 4.4.5 小练习与答案

**练习 1**：你的 custom-generate 声明返回 `list[Sample]`（fan-out）。某次 agent 执行抛了异常，你 catch 后想返回单个 `Sample` 标记失败。这样做安全吗？

**答案**：不安全。coding_agent_rl 的 `_abort_result`（L316-L333）特意用 `[sample]` 包成列表返回，注释明说「fan-out generate function always yields [list shape]」。一旦你有时返回 list、有时返回单个 Sample，上层 `isinstance(sample, list)`（sglang_rollout.py L268）的分支就会时而走批量奖励、时而走单样本奖励，行为不一致。**fan-out 函数的所有返回路径都必须保持 list 形状**。

**练习 2**：请用一句话区分「custom-generate 返回 list[Sample]」和「升级到 rollout-function-path」的触发条件。

**答案**：返回 `list[Sample]` 是因为「一次执行要拆成多个可训练段」，仍复用默认外层循环；升级到 `--rollout-function-path` 是因为「默认外层循环（逐样本调度 + 过采样补数）根本装不下你的工作流」（如要跨 rollout 后台队列、全异步），必须接管整条编排。

---

## 5. 综合实践

**任务**：为「**带沙箱执行的代码 agent**」选择合适的接口组合并说明理由；指出哪一步需要 fan-out 返回多 Sample。

> 这是本讲规格里要求的代码实践任务。下面给出「设计 + 源码核对」两步，使其既可操作又可验证。

### 步骤一：根据决策表选接口

参照 4.1 的决策表与 coding_agent_rl 真实做法，给出推荐组合并说明理由：

| 需求 | 选用接口 | 理由 |
| :--- | :--- | :--- |
| 每个样本要：启动隔离沙箱 → agent 用工具改代码 → 多轮与 SGLang 交互 | `--custom-generate-function-path` | 复用默认 rollout 外层循环（过采样、过滤、abort），只替换最底层单样本生成；这正是 coding_agent_rl 的做法（见其 README 与 `generate.py`）。 |
| 改完代码后，跑单元测试，按「测试是否通过」打分 | `--custom-rm-path`（或直接在 generate 内填 reward） | 基于测试的奖励是典型 agent 奖励；coding_agent_rl 选择在 `generate` 内部跑 `swe.run_evaluation` 算出 reward 后传给 `finish_session`。 |
| 多轮 agent 请求要走前缀缓存、同一会话路由到同一 worker | `--router-policy consistent_hashing` + 稳定 `session_id` | 见 agent.md L62、adapters 用 `X-SMG-Routing-Key` 传 session_id（u7-l2 详述）。 |
| agent 轨迹会被中间件切成 subagent / wipe / final 多段，每段都要训练 | custom-generate 返回 `list[Sample]`（fan-out） | 见 4.4，无需升级到 rollout-function-path。 |

**结论**：首选 `--custom-generate-function-path` + `--custom-rm-path`（或在 generate 内填 reward），辅以 consistent_hashing 路由；**不需要** `--rollout-function-path`，因为默认外层循环足以容纳「逐样本启动沙箱」的结构。

### 步骤二：指出哪一步 fan-out

阅读 [examples/coding_agent_rl/generate.py:237-245](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/coding_agent_rl/generate.py#L237-L245)：

```python
samples = await state.adapter.finish_session(
    session_id,
    base_sample=base_sample,
    reward=float(reward),
    extra_metadata={...},
)
```

**fan-out 发生在「会话导出」这一步**：`finish_session` 把一次 agent 会话的轨迹树线性化成 `list[Sample]`（subagent / wipe / final 多段），所有段共享同一个 `base_sample`（从而同 `rollout_id`），总 reward 在 `get_trajectory` 内分摊。`generate` 随后直接 `return samples`（L268），完成 fan-out。

### 步骤三（可选，需 GPU/沙箱环境）：跑起来核对

若你已有 slime 训练环境与 E2B 沙箱凭据：

1. 阅读 `examples/coding_agent_rl/README.md`，按其要求导出 `SWE_AGENT`、`ADAPTER_PUBLIC_HOST`、`SWE_EVAL_PROTOCOL` 等环境变量。
2. 用 `--custom-generate-function-path examples.coding_agent_rl.generate.generate` 启动一次小规模 rollout。
3. 在日志里找 `[coding_agent_rl] ... segments=N`（generate.py L259-L267），确认 `segments > 1`——这就是 fan-out 生效的证据。

> 若无沙箱环境，本实践停留在「步骤一 + 步骤二」的源码阅读与设计层面即可，结论同样成立（**待本地验证**：实际 segments 数取决于轨迹是否触发压缩/subagent）。

## 6. 本讲小结

- **不另起 agent 框架**：slime 把 agent 工作流当作「一类特殊的数据生成工作流」，通过已有的定制化接口接入闭环，权重同步回路因此天然不断裂。
- **接口选择决策树**：默认走 `--custom-generate-function-path` + `--custom-rm-path`；只有当默认外层循环（逐样本调度 + 过采样补数）装不下工作流时，才升级到 `--rollout-function-path`。
- **custom-generate 是最底层工位**：它只替换「单样本生成」，外层的过采样、动态过滤、奖励计算、abort 全部保留；返回单个 `Sample` 走单样本奖励，返回 `list[Sample]` 走批量奖励。
- **训练目标始终 token-based**：必须保留模型真实采样的 token id，绝不能对引擎返回的字符串重新分词；用 `loss_mask` 把模型生成 token（mask=1、带真实 logp）与环境/工具 token（mask=0、logp 填 0）分开，三者长度由 `append_response_tokens` / `_validate_response_metadata_lengths` 强制对齐。
- **fan-out 契约**：一次 rollout 拆多段时返回 `list[Sample]`，所有兄弟共享同一个 `rollout_id`，总 reward 按 `R/K` 分摊；fan-out 函数的所有返回路径（含异常）都必须保持 list 形状。
- **加载机制统一**：所有 `--xxx-path` 接口都走 `load_function`（4 行核心：`rpartition` → `import_module` → `getattr` + `@cache`），你的 agent 代码只需是个可 import 的模块。

## 7. 下一步学习建议

本讲给了智能体 RL 的**地图与决策树**，但刻意没有深入任何一条路径的内部。接下来建议按顺序学习：

1. **u7-l2 Agent 运行时适配器（Anthropic / OpenAI）**：本讲 4.4.4 只点了 `finish_session`，下一讲深入 `BaseAdapter` / `AnthropicAdapter` / `OpenAIAdapter`，搞清「消息进、采样 token 出」的协议适配与前缀缓存路由（`X-SMG-Routing-Key`）。
2. **u7-l3 多样本 fan-out 与轨迹分段训练**：本讲只讲了 fan-out 的**契约**，下一讲深入 `TrajectoryManager` 如何把多轮会话树线性化成多段、rollout_id 同源聚合在 train-step 里如何落地、coding_agent_rl 的 subagent/wipe/final 切分细节。
3. **u7-l4 流式、全异步与部分回滚 rollout**：本讲决策表里「长尾 agent」指向 `--rollout-function-path` 的全异步模式，下一讲对比 streaming / fully_async / partial-rollout 三种高级数据流。
4. 若你想先动手写一个最小的 agent custom-generate，可回到 `examples/search-r1/generate_with_search.py`（本讲 4.2.4 已导读）作为模板，它比 coding_agent_rl 简单得多，适合作为第一个练手对象。
