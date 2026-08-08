# 智能体 RL 路线图与接口选择

## 1. 本讲目标

本讲是 slime 智能体 RL（Agentic RL）主题的**入门与路线图**。读完本讲，你应当能够：

- 说清楚 slime 为什么**不另起一个 agent 框架**，而是把多轮工具调用、沙箱执行、环境反馈直接接入已有的「rollout → buffer → training」闭环。
- 面对一类 agent 任务（多轮检索、沙箱代码 agent、多智能体、上下文压缩），能照着官方路线图表选出**第一个该用的接口组合**，并说出「何时该升级到 `--rollout-function-path`」。
- 理解智能体工作的一个根本原则：**环境是字符串/消息/工具观测，但训练目标必须保持 token-based**，并用 `loss_mask` 区分「模型生成的 token」与「环境注入的 token」。
- 看懂 slime 旗舰示例 `coding_agent_rl` 如何用 `--custom-generate-function-path` + 测试打分奖励 + fan-out 把一条真实 coding agent 轨迹变成可训练样本。

本讲只建立**决策框架与心智模型**，不展开适配器内部（u7-l2）、fan-out 细节（u7-l3）、流式/异步（u7-l4）——这些是后续讲义的主题。本讲承接 u6-l2（你已经会写一个 `custom_generate` 函数并标注 `loss_mask`），把它放到「智能体」这个更大的语境里。

## 2. 前置知识

在进入本讲前，请确认你已理解以下概念（前序讲义已建立）：

- **slime 三模块闭环**（u2-l1）：rollout 产数据 → data buffer 桥接 → training 消费 → 权重单向同步回 rollout。智能体 RL 不会绕过这个闭环，而是替换 rollout 内部的「肉」。
- **`Sample` 数据结构**（u3-l1）：它是闭环里流动的核心数据载体，关键字段包括 `tokens`、`response_length`、`loss_mask`、`reward`、`rollout_log_probs`、`status`。
- **custom-generate 函数机制**（u6-l2）：你已经知道 `--custom-generate-function-path` 只替换默认 rollout 流水线里「单样本生成」这一工位，签名是 `async def custom_generate(args, sample, sampling_params) -> Sample | list[Sample]`，并用 `loss_mask` 的 1/0 区分模型 token 与环境 token。
- **`rollout_id` 同源约束**（u6-l2）：一次执行拆成多段时，兄弟 `Sample` 必须共享同一个 `rollout_id`，否则会被重复计数。

如果你对「智能体（agent）」这个词本身还陌生：这里的 agent 指**能让大模型在多步里调用工具、读写沙箱、与环境多轮交互，最终产出一条可训练轨迹**的工作流。典型例子是「让模型像程序员一样改代码、跑测试、按测试通过与否给奖励」。

## 3. 本讲源码地图

本讲主要阅读官方文档与一个旗舰示例，源码范围适中：

| 文件 | 作用 |
| :--- | :--- |
| [docs/en/get_started/agent.md](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/agent.md) | 智能体 RL 官方路线图：给出「目标 → 推荐入口」表与推荐集成模式。 |
| [docs/en/get_started/customization.md](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/customization.md) | 全部定制化接口参考，含「智能体工作流」一节如何映射到具体接口。 |
| [examples/coding_agent_rl/README.md](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/coding_agent_rl/README.md) | 旗舰示例文档：沙箱、coding agent、`git diff`、测试打分、fan-out 全流程说明。 |
| [examples/coding_agent_rl/generate.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/coding_agent_rl/generate.py) | 旗舰示例的 `generate()` 实现，是「`--custom-generate-function-path` + fan-out」的最佳范本。 |
| [examples/search-r1/generate_with_search.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/search-r1/generate_with_search.py) | 多轮检索生成函数，展示了最朴素的「生成→调工具→拼观测」循环与 `loss_mask` 标注。 |
| [slime/utils/types.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/types.py) | `Sample` 定义，其中 `rollout_id` 字段是 fan-out 的契约核心。 |

> 提示：`examples/` 下的真实文件可直接阅读；文档里的 `docs/en/_examples_synced/` 是同步展示副本。

---

## 4. 核心概念与源码讲解

### 4.1 智能体 RL 为什么不另起炉灶：路线图与接口层级

#### 4.1.1 概念说明

很多团队做「带工具/沙箱的 RL」时，习惯**另写一个独立的 agent 训练框架**：自己写多轮循环、自己管 buffer、自己接训练器。slime 的立场正好相反——agent 训练**不应该是新框架**，而是已有「采样→训练→权重同步」闭环上的一类数据生成工作流。

支撑这个立场的，是 slime 已经具备的两块能力（见 [agent.md:3](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/agent.md#L3)）：

1. **高性能训练 + SGLang rollout 服务**：多轮 agent 的上下文更长、请求更碎、长尾更重，需要能撑住吞吐的推理后端。
2. **可插拔的数据生成接口**：你可以把「一条 agent 执行」写成函数注入进去，而不用碰训练核心。

于是 slime 适合多轮工具调用、沙箱交互、子智能体分支、上下文压缩、基于测试的奖励等场景。本节要建立的就是「拿到一个 agent 任务，第一个该用哪个接口」的路线图。

#### 4.1.2 核心流程：按「目标」选入口

官方路线图用一张「目标 → 推荐入口」表来帮你决策。下面是它的精神提炼（对应 [agent.md:9-17](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/agent.md#L9-L17) 与 [customization.md:38-45](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/customization.md#L38-L45)）：

| 你的目标 | 推荐接口 | 说明 |
| :--- | :--- | :--- |
| 为每个样本跑一个自定义 agent 循环（工具调用、RAG、浏览器/终端/沙箱交互） | `--custom-generate-function-path` | 复用 slime 默认 rollout 外层循环，只换「单样本生成」工位。 |
| 实现验证器奖励、基于测试的奖励、环境成功检查、外部奖励服务 | `--custom-rm-path` | 单独管「打分」，与生成分离。 |
| 一次 prompt 产出多个训练样本（子智能体、多智能体、上下文压缩分段） | custom-generate 的 **fan-out 返回**（`list[Sample]`） | 不必替换整条 rollout 函数。 |
| 别让长尾 agent rollout 卡住训练 | 升级到 `--rollout-function-path`（全异步） | 默认循环的调度不够用时才走这一步。 |

**最重要的结论**（见 [agent.md:19-21](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/agent.md#L19-L21) 与 [customization.md:36](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/customization.md#L36)）：

> 大多数智能体任务，**从 `--custom-generate-function-path` 加 `--custom-rm-path` 开始**；只有当默认 rollout 循环不够用时，才覆盖整条 rollout 函数。

这条原则背后的逻辑是**接口层级**：四个主接口是包裹关系，越外层越「重」。

```text
--data-source-path          （原料：从哪领 prompt）
        │
        ▼
--rollout-function-path     （整条流水线：默认是 sglang_rollout.generate_rollout）
        │  ▼ 内部包裹
        ├── --custom-generate-function-path   （单样本生成工位：跑你的 agent 循环）
        ├── --custom-rm-path                  （打分工位：算奖励）
        └── 各种 filter / hook                 （过滤、后处理）
```

换**外层**（如换成 `--rollout-function-path`）会让内层的挂载点失效——因为你接管了整条流水线，默认循环里那些「自动调用 custom-generate / custom-rm」的代码就不再运行了。所以**能用工位接口（内层）就别用流水线接口（外层）**，这样新增的 agent 逻辑能最大程度复用 slime 已有的过采样、动态过滤、奖励分发、partial rollout 等机制。

#### 4.1.3 源码精读：路线图的两张表

官方路线图的「Where To Start」表直接给出入口指引（[agent.md:9-17](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/agent.md#L9-L17)），紧接着的「Recommended Integration Pattern」段落（[agent.md:19-27](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/agent.md#L19-L27)）讲了三件事：

1. **agent 执行 → 可训练 Sample**：`custom_generate` 把一次 agent 执行转成 slime 可训练的 `Sample`——填 `tokens`、`response_length`、`loss_mask`、`status`，再直接填 `reward` 或交给 `--custom-rm-path` 算（[agent.md:21](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/agent.md#L21)）。
2. **训练目标保持 token based**：agent 工作流可能用字符串、聊天消息、工具调用、环境事件来表达，但训练目标应当始终是 token；保留模型实际采样的 token id，用 `loss_mask` 区分可训练的模型输出与 prompt/模板/工具观测/环境文本（[agent.md:23](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/agent.md#L23)）。这条是 4.3 节的主题。
3. **fan-out 与升级条件**：一次 rollout 对应一个训练样本就返回单个 `Sample`；若要拆成多段（子智能体轨迹、压缩前后分段），返回 `list[Sample]` 且所有兄弟样本设同一个 `rollout_id`；只有需要替换整条 rollout 编排时才动 `--rollout-function-path`（[agent.md:25-27](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/agent.md#L25-L27)）。

`customization.md` 里还有一张同构的「需要做 X → 用 Y」表（[customization.md:38-45](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/customization.md#L38-L45)），它额外点出两个易被忽略的辅助接口：

- `--rollout-data-postprocess-path` / `--custom-convert-samples-to-train-data-path`：当你需要附加自定义 loss mask、metadata，或把 agent 输出翻译成训练数据格式时使用。
- `slime.utils.trace_utils`：调试长耗时的自定义生成、验证器调用、工具调用、沙箱步骤时用（u8-l4 详述）。

把这两张表当成「接口字典」，遇到具体需求时反查即可。

#### 4.1.4 代码实践：为四类任务选接口

**实践目标**：把路线图内化成直觉，避免一上来就写整条 rollout 函数。

**操作步骤**：

1. 阅读上面两张表，理解每个接口负责的「工位」。
2. 对下面四类 agent 任务，分别写出「首选接口组合 + 是否需要 fan-out」：
   - (a) 一个数学题 agent：模型先思考，遇到不会的公式就调用计算器工具，最后给答案，按答案对错打分。
   - (b) 一个多智能体辩论系统：一个「主智能体」派出两个「子智能体」各自独立完成子任务，主智能体再综合；每个子智能体的轨迹都要参与训练。
   - (c) 一个网页浏览 agent：每一步生成动作 → 浏览器返回页面文本 → 模型继续，长尾差异极大（有的样本 2 步结束，有的 30 步）。
   - (d) 一个代码补全 agent：单轮生成，但奖励来自「能否通过隐藏单元测试」。

**需要观察的现象**：你应该发现 (a)(d) 都只需要 `--custom-generate-function-path` + `--custom-rm-path`；(b) 需要额外 fan-out（多段轨迹同 `rollout_id`）；(c) 因为长尾严重，可能需要升级到全异步的 `--rollout-function-path`。

**预期结果**：能复述「先工位、后流水线」的原则，并指出 (c) 是唯一可能需要升级外层的场景。待本地验证：若不确定 (c) 的默认循环能否支撑，可先用 custom-generate 跑一个小 rollout 实测再决定。

#### 4.1.5 小练习与答案

**练习 1**：为什么官方反复强调「先 custom-generate + custom-rm，最后才 rollout-function-path」？如果一上来就用 `--rollout-function-path`，你会失去什么？

> **参考答案**：因为四个主接口是包裹关系，覆盖最外层的 `--rollout-function-path` 等于接管整条流水线，默认 `sglang_rollout` 里那些「自动调用 custom-generate / custom-rm、做过采样补数、跑动态过滤、处理 partial rollout」的代码就不再运行了，你必须自己重写这些机制。用工位接口（内层）能最大程度复用 slime 已有能力，改动面最小、升级成本最低。

**练习 2**：把「需要给 agent 输出加自定义 metadata」「需要按测试通过率打分」「需要换 prompt 数据来源」分别对应到接口。

> **参考答案**：自定义 metadata 可用 `--rollout-data-postprocess-path` 或 `--custom-convert-samples-to-train-data-path`；按测试通过率打分用 `--custom-rm-path`；换 prompt 来源用 `--data-source-path`。

---

### 4.2 custom-generate：把一次 agent 执行变成可训练 Sample

#### 4.2.1 概念说明

`--custom-generate-function-path` 是智能体 RL 的**主入口**。从 u6-l2 你已经知道它只替换默认 rollout 流水线最底层的「单样本生成」工位，外层调度（过采样、奖励分发、过滤）都由 slime 默认循环接管。在智能体语境下，这个工位的工作变成：**跑完一次完整的 agent 执行，把它整理成一个或多个可训练的 `Sample`**。

这里的关键认知转变是：在普通单轮 RL 里，「一次生成」就是模型吐一段回答；而在 agent RL 里，「一次生成」可能是**几十轮工具调用、几次沙箱执行、若干子智能体派发**——但这些对 slime 来说，最终都坍缩成「填好字段的 `Sample`」。slime 不关心你的 agent 内部有多复杂，只关心你返回的 `Sample` 是否合法。

#### 4.2.2 核心流程：旗舰示例的四阶段流水线

`coding_agent_rl` 是 slime 离真实软件工程最近的端到端 agent 示例（见 [README.md:3](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/coding_agent_rl/README.md#L3)）。它的 `generate()` 是一个**四阶段编排器**（见 [generate.py:5-7](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/coding_agent_rl/generate.py#L5-L7) 的 docstring）。用伪代码描述一次样本的处理：

```text
输入: base_sample (含 prompt / metadata.image / metadata.workdir / problem_statement)

1. 准备阶段
   - get_metadata(base_sample)         # 解析数据集行 → md（含沙箱镜像、工作目录、判分协议）
   - open_session(session_id, ...)     # 在 adapter 上开一个会话（用于前缀缓存路由）

2. 沙箱 + agent 执行阶段
   - boot_agent_sandbox(image)         # 拉起一个全新 E2B 沙箱，装 Node + claude-code CLI
   - prepare_workspace(sb, workdir)    # 写入 PROBLEM_STATEMENT.md、跑 pre_commands
   - HARNESS.run(sb, ..., adapter_url) # claude-code 以 adapter 为"Anthropic 端点"跑工具循环
   - git_diff(sb, workdir)             # 抓取 agent 产生的 git diff

3. 打分阶段
   - run_evaluation(md, diff_text)     # 在第二个干净沙箱里跑测试，按协议给 reward

4. 轨迹导出阶段
   - finish_session(session_id, reward)# adapter 把整条多轮轨迹切成若干可训练 Sample
   - 返回 list[Sample]                 # fan-out: 一条轨迹可能对应多个训练样本
```

注意三个 slime 特色的设计：

- **打分与生成分离**：reward 不是在生成函数内部硬编码，而是由独立的 `run_evaluation`（本质是 `--custom-rm-path` 的等价物，这里直接在 generate 里调用并填进 `reward`）算出，再随 `finish_session` 注入每段样本。
- **adapter 是「字符串进、token 出」的桥梁**：claude-code 用 Anthropic 协议说话，adapter 把每轮消息历史渲染成 `input_ids` 调 SGLang，并保留模型实际采样的 token（4.3 节详述）。
- **fan-out 在最后一步发生**：`finish_session` 返回 `list[Sample]`，一条轨迹可能被切成多段。

#### 4.2.3 源码精读：generate() 的关键代码点

先看签名与 adapter 选择。`generate()` 是一个 `async` 函数，签名比 u6-l2 的最简形式多了一个 `evaluation` 形参（用于区分训练/评估路径）：

[examples/coding_agent_rl/generate.py:182-186](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/coding_agent_rl/generate.py#L182-L186) —— 这是 `--custom-generate-function-path` 指向的入口，`evaluation` 形参让同一个函数既能训练又能评估。

adapter 的选择由环境变量 `SWE_AGENT` 决定，体现了「适配器是可替换层」（[generate.py:45-52](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/coding_agent_rl/generate.py#L45-L52)）：`claude_code` 配 `AnthropicAdapter`，`codex` 配 `OpenAIAdapter`。

会话开启是 agent 多轮的关键准备（[generate.py:194-199](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/coding_agent_rl/generate.py#L194-L199)）：`session_id` 写回 `base_sample.session_id`，并调 `adapter.open_session(...)`。这个 `session_id` 稍后会被 adapter 作为 `X-SMG-Routing-Key` 传给 SGLang，让同一会话的多轮请求落到同一个 worker，复用前缀缓存（见 [agent.md:55](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/agent.md#L55)）——这是多轮 agent 提升吞吐的重要细节。

最关键的轨迹导出与 fan-out（[generate.py:237-245](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/coding_agent_rl/generate.py#L237-L245)）：`finish_session` 把 `reward` 传进去，返回 `samples`（一个 list）。如果返回空，就当作 abort 处理（[generate.py:246-247](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/coding_agent_rl/generate.py#L246-L247)）。最终 `return samples`（[generate.py:268](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/coding_agent_rl/generate.py#L268)）——这就是 fan-out 返回。

整个 `generate()` 还被一个 `asyncio.timeout(rollout_guard_sec)`（[generate.py:202](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/coding_agent_rl/generate.py#L202)）包裹，超时就返回 abort 样本（[generate.py:270-272](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/coding_agent_rl/generate.py#L270-L272)）。这是 agent RL 必备的护栏：agent 执行的耗时方差极大，必须给单样本一个墙上时钟上限。

#### 4.2.4 代码实践：追踪一条样本的命运

**实践目标**：建立「agent 执行 → Sample」的完整因果链直觉。

**操作步骤**：

1. 打开 [generate.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/coding_agent_rl/generate.py)，从 `async def generate(...)`（第 182 行）开始读。
2. 在一张纸上画四个方框：**准备 / 沙箱执行 / 打分 / 轨迹导出**。
3. 把第 186–268 行的每一步（`get_metadata`、`open_session`、`boot_agent_sandbox`、`prepare_workspace`、`HARNESS.run`、`git_diff`、`run_evaluation`、`finish_session`）归到对应方框。
4. 标出哪一步会改写 `sample.tokens / loss_mask / reward / status`（提示：都在 `finish_session` 返回的 samples 里，以及 abort 分支的 `_abort_result`）。

**需要观察的现象**：你会发现 slime 框架本身（外层 rollout 循环）**完全不感知**沙箱、claude-code、git diff 这些概念；对它来说，`generate()` 就是一个返回 `Sample` 的黑盒。

**预期结果**：能用一句话说清——「slime 只负责调度与训练，agent 的所有复杂性都被封装在 `custom_generate` 这个黑盒里。」

#### 4.2.5 小练习与答案

**练习 1**：`coding_agent_rl` 的奖励是「测试通过率」，但它并没有用 `--custom-rm-path` 单独传一个奖励函数，而是直接在 `generate()` 里算 reward 并随 `finish_session` 注入。这两种做法 slime 都支持吗？

> **参考答案**：支持。slime 允许在 `custom_generate` 内部直接填 `sample.reward`，也可以把打分完全外包给 `--custom-rm-path`。`coding_agent_rl` 选择前者，因为它的奖励依赖沙箱里跑测试这种重状态操作，与生成强耦合，放进同一函数更内聚；而像数学答案判分这种无状态的规则奖励，更适合用独立的 `--custom-rm-path`，让生成函数保持纯粹。

**练习 2**：为什么 `generate()` 要把 `session_id` 写回 `base_sample.session_id`？

> **参考答案**：为了多轮前缀缓存路由。adapter 会把 `session_id` 作为 `X-SMG-Routing-Key` 传给 SGLang，使同一会话的后续请求落到同一 worker，命中前缀缓存，显著降低多轮 agent 的重复 prefill 开销（见 [agent.md:55](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/agent.md#L55)）。

---

### 4.3 token-based 训练目标与 loss_mask 区分原则

#### 4.3.1 概念说明

这是智能体 RL 里**最容易被忽视、却最致命**的一类正确性问题，必须单独成节。

矛盾的根源：**agent 的环境是字符串/消息/工具观测**。claude-code 发的是 Anthropic Messages 请求，收到的是文本/thinking/tool-use 块，工具观测、压缩后的消息都是以字符串形式回传的。但**训练目标必须是 token**——一条轨迹只有当「被优化的 token 就是 rollout 模型实际采样的 token」时，才是合法的 RL 目标。

如果你图省事，把 agent 跑出来的字符串 `response` 重新分词当成训练序列，就会出大问题：重新分词可能产生与采样时**不同的 token**，导致 token 与它对应的 logprob 错位，梯度算的是「不是模型真正生成过的 token」。这叫**重分词漂移（re-tokenization drift）**。

slime 的解法是**「字符串进、token 出（string in, token out）」契约**（见 [coding_agent_rl/README.md:142-159](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/coding_agent_rl/README.md#L142-L159)）：

- 每轮把消息历史用模型 chat template 渲染成 `input_ids` 发给 SGLang；
- SGLang 调用时带 `return_logprob=True`，adapter 记录这轮的 `prompt_ids`、采样的 `output_ids`、逐 token 的 rollout logprob；
- 训练导出时，直接用这些保存下来的 token id 组装样本；解码出的 `response` 字段只是给人看的「侧车」，**绝不重新分词**来恢复训练序列。

#### 4.3.2 核心流程：loss_mask 如何区分「谁该被训练」

即便 token 来源是对的，agent 轨迹里也不是所有 token 都该被训练。`loss_mask`（与回复等长的 0/1 数组）承担这个区分职责。结合 [coding_agent_rl/README.md:161-177](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/coding_agent_rl/README.md#L161-L177) 与 [agent.md:23](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/agent.md#L23)，标注规则如下：

| token 来源 | loss_mask | 含义 |
| :--- | :--- | :--- |
| 模型本轮新生成的输出（带 rollout logprob） | `1` | 可训练：这是策略要优化的对象 |
| prompt / 系统模板 | `0` | 不可训练：是条件，不是动作 |
| 工具调用返回的观测文本 | `0` | 不可训练：来自环境，不是模型生成 |
| 后续轮里拼上去的 user/tool 上下文 | `0` | 不可训练：环境注入 |
| **采样来源无法证明的 token**（漂移断点后的残留前缀） | `0` | **强制不可训练**：正确性护栏 |

最后一条是 slime 的**正确性护栏**，非常关键（[coding_agent_rl/README.md:170-177](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/coding_agent_rl/README.md#L170-L177)）：当后续某轮的 prompt 与之前采样的输出在 token 层面不再匹配（漂移 cut 进了某个模型输出 turn 的中间），slime 会保留继续 agent 所需的上下文，但**不给那些采样来源已无法证明的 token 反向传播**——把这一整段保留前缀的 `loss_mask` 置 0。这样既能维持 agent 的对话连续性，又不会污染梯度。

至于为什么 loss_mask 之外还要保证 token 来源正确，可以用一个简单的概率语言来理解。RL 策略梯度对每个 token \(t_i\) 的损失权重正比于它在该 token 上的对数概率 \(\log \pi_\theta(t_i)\)。只有当 \(t_i\) 真的是当前策略（或行为策略）采样出来的，重要性采样比 \(\rho_i = \exp(\log\pi_\theta(t_i) - \log\pi_{\text{rollout}}(t_i))\) 才有意义；一旦 \(t_i\) 是重新分词凭空产生的，\(\log\pi_{\text{rollout}}(t_i)\) 就对不上，梯度方向会被污染。这正是 slime 坚持 string-in/token-out 的根本原因。

#### 4.3.3 源码精读：search-r1 里的 loss_mask 标注

`coding_agent_rl` 用 adapter 自动管理 loss_mask（u7-l2 详述），而 `search-r1` 用最朴素的手写循环展示这个原则，更适合学习。看 [examples/search-r1/generate_with_search.py:179-244](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/search-r1/generate_with_search.py#L179-L244) 的多轮循环。

模型生成 token 时，标注为可训练（[generate_with_search.py:219-226](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/search-r1/generate_with_search.py#L219-L226)）：

```python
loss_mask += [1] * len(cur_response_token_ids)
sample.append_response_tokens(
    args,
    tokens=cur_response_token_ids,
    log_probs=cur_response_log_probs if SEARCH_R1_CONFIGS["return_logprob"] else None,
    trainable=True,                      # 模型生成 → 可训练
    ...
)
```

工具观测（检索结果）token 时，标注为不可训练（[generate_with_search.py:243-244](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/search-r1/generate_with_search.py#L243-L244)）：

```python
loss_mask += [0] * len(obs_tokens_ids)
sample.append_response_tokens(args, tokens=obs_tokens_ids, trainable=False)  # 环境文本 → 不训练
```

这段代码还藏着一个关于 token-based 原则的重要教训（见 [generate_with_search.py:93-98](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/search-r1/generate_with_search.py#L93-L98) 与 [164-177](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/search-r1/generate_with_search.py#L164-L177)）：**当要采集 logprob 时，绝不能对 SGLang 返回的字符串做后处理再重新分词**，因为 (1) 无法对应截断 token/logp 数组，(2) 重新分词会产生不同的 token。所以该示例在 `return_logprob=True` 时直接用 `output_token_logprobs` 里的原始 token id（[generate_with_search.py:209-210](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/search-r1/generate_with_search.py#L209-L210)），并靠注入 stop tag 让引擎在工具边界自然停止，从根上保持 token/logp 对齐。

> 上面的代码片段为「示例代码（节选自真实项目，省略了无关分支）」，目的是突出 `trainable=True/False` 的对照，并非完整可运行文件。

#### 4.3.4 代码实践：对照 search-r1 标注 loss_mask

**实践目标**：亲手走一遍「生成→调工具→拼观测」的 loss_mask 标注，建立肌肉记忆。

**操作步骤**：

1. 打开 [generate_with_search.py:179-244](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/search-r1/generate_with_search.py#L179-L244)。
2. 假设一次执行产生两轮：
   - 第 1 轮：模型生成 `<search>query</search>`（设为 8 个 token），检索回 50 个 token 的观测。
   - 第 2 轮：模型生成 `<answer>42</answer>`（设为 5 个 token）。
3. 手写出最终的 `response_token_ids` 总长、`loss_mask` 数组（用 1/0 序列表示），以及哪些位置是可训练的。
4. 找到代码里把整个 `loss_mask` 赋给 `sample.loss_mask` 的那行（提示：[generate_with_search.py:259](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/search-r1/generate_with_search.py#L259)）。

**需要观察的现象**：可训练 token 只有 \(8 + 5 = 13\) 个，而总回复长度是 \(8 + 50 + 5 = 63\) 个——环境观测占了绝大部分却不参与训练。

**预期结果**：写出形如 `[1,1,1,1,1,1,1,1, 0×50, 1,1,1,1,1]` 的 loss_mask，并能解释为什么观测段必须是 0。

#### 4.3.5 小练习与答案

**练习 1**：假如你在 custom_generate 里为了「清洗格式」，把 SGLang 返回的 `response` 字符串 strip 了一下再重新分词当作训练 token，开启 TIS 时会发生什么？

> **参考答案**：重新分词可能产生与采样时不同的 token，导致 token 与其 rollout logprob 错位。TIS 用 \(\rho_i = \exp(\log\pi_\theta(t_i) - \log\pi_{\text{rollout}}(t_i))\) 修正 off-policy，但此时 \(\log\pi_{\text{rollout}}(t_i)\) 对的是「假 token」，比值无意义，梯度被污染。正确做法是像 search-r1 那样直接用引擎返回的原始 token id（[generate_with_search.py:209-210](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/search-r1/generate_with_search.py#L209-L210)），清洗只在不开 logprob 时才允许。

**练习 2**：coding_agent_rl 里，当某轮 prompt 与之前模型输出在 token 层面不再匹配时，slime 为什么保留上下文却把那段 loss_mask 置 0？

> **参考答案**：为了在「维持 agent 对话连续性」与「不污染梯度」之间取得平衡。保留上下文让 agent 能继续工作；但那些 token 的采样来源已经无法证明（漂移断点），如果还让它们参与训练，反向传播会作用在「不是模型真正生成过的 token」上，所以强制 `loss_mask=0` 切断梯度（[coding_agent_rl/README.md:170-177](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/coding_agent_rl/README.md#L170-L177)）。

---

### 4.4 何时升级到 rollout-function-path

#### 4.4.1 概念说明

前三节都在鼓励你用工位接口（`custom-generate` / `custom-rm`）。但确实存在「默认 rollout 循环不够用」的场景，这时才该升级到最外层的 `--rollout-function-path`。

升级的判据来自 [agent.md:27](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/agent.md#L27) 和 [customization.md:42](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/customization.md#L42)：**只有当逐样本的定制不够时**，才覆盖整条 rollout 编排。具体触发条件有四类。

#### 4.4.2 核心流程：升级触发条件清单

| 触发条件 | 为什么 custom-generate 做不到 | 对应示例 |
| :--- | :--- | :--- |
| **自定义数据源调度** | 你需要按任务难度/优先级跨 rollout 安排 prompt，而非默认的 epoch 回环 | `--data-source-path`（仍内层） |
| **跨 rollout 的后台队列** | 上一轮没收完的长尾样本要排到后台，与下一轮训练重叠 | `examples/fully_async` |
| **全异步生成** | 不能让单个长尾 agent 样本阻塞整批；需要后台线程池持续投喂 | `examples/fully_async`（`fully_async_rollout`） |
| **塞不进默认逐样本结构** | 你的工作流不是「一个 prompt → 一组采样」的形状 | `examples/multi_agent`（`--rollout-function-path` 模式） |

注意「升级」不是全有或全无：有时只需要换 `--data-source-path`（仍是内层），不必动整条 rollout 函数。真正需要 `--rollout-function-path` 的，通常是**调度与并发模型本身**要改。

一个常见误区：因为「我的 agent 很复杂」就升级。**复杂度不是升级理由**——`coding_agent_rl` 的 agent 极其复杂（沙箱、claude-code、git diff、测试打分），但它依然只用了 `--custom-generate-function-path`，因为它仍能塞进「一个 prompt → 一次执行 → 返回 Sample」的形状。只有**形状**不符合默认循环时才升级。

#### 4.4.3 源码精读：升级的代价与边界

`--rollout-function-path` 的契约（[customization.md:51-67](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/customization.md#L51-L67)）：

```python
def generate_rollout(args, rollout_id, data_source, evaluation=False)
    -> RolloutFnTrainOutput | RolloutFnEvalOutput
```

默认实现是 `slime.rollout.sglang_rollout.generate_rollout`（[customization.md:53](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/customization.md#L53)）。一旦你换成自己的函数，你就**接管了整条编排**——过采样补数、动态过滤、partial rollout 续传、abort、奖励分发（这些是 u3-l2/u3-l3/u3-l5 的内容）都要你自己实现或显式调用。这就是为什么官方把它排在最后。

`examples/multi_agent` 是一个用 `--rollout-function-path` 的多智能体范例（[customization.md:47](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/customization.md#L47) 与 [agent.md:13](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/agent.md#L13)）；`examples/fully_async` 则面向「长尾 agent rollout 不能阻塞训练」的场景（[agent.md:14](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/agent.md#L14)）。它们的内部细节分别在 u7-l3、u7-l4 展开，本节只需记住：**它们是升级路径的参考点，不是入门路径**。

#### 4.4.4 代码实践：升级判断题

**实践目标**：练就「该不该升级」的判断力，避免过早优化。

**操作步骤**：对下面每个场景，判断「用 custom-generate 即可」还是「需要升级到 rollout-function-path」，并说一句理由。

1. 我的 agent 每次会调用 5–20 次工具，耗时从 10 秒到 5 分钟不等，但每轮 rollout 内能容忍这些样本一起跑完。
2. 我的 agent 有 30% 的样本要跑 10 分钟以上，严重拖慢每轮 rollout，我想让训练不必等最慢的样本。
3. 我想让「上一轮没跑完的高难度样本」自动排到下一轮继续，而不是丢弃。
4. 我的 agent 内部逻辑很简单，只是想换一种 prompt 抽样顺序（先易后难）。

**需要观察的现象**：(1) 虽然 agent 复杂、长尾重，但仍在默认循环形状内，custom-generate 够用；(2)(3) 涉及并发模型与跨轮续传，是升级信号（指向 fully_async / partial rollout）；(4) 只需换 `--data-source-path`，不必动 rollout 函数。

**预期结果**：能区分「复杂度（不升级）」「形状不符（升级 rollout）」「仅调度需求（换 data-source）」三种情形。待本地验证：若你不确定某场景的默认循环能否支撑，可以先按 custom-generate 跑一个小 rollout 实测。

#### 4.4.5 小练习与答案

**练习 1**：`coding_agent_rl` 的 agent 非常复杂，却只用 `--custom-generate-function-path`。这说明「升级到 rollout-function-path」的真正判据是什么？

> **参考答案**：判据是**工作流形状是否符合默认循环**（一个 prompt → 一次执行 → 返回 Sample），而不是 agent 的复杂度或耗时。`coding_agent_rl` 虽然内部有沙箱、CLI、git diff、测试打分，但它仍能塞进逐样本结构，所以用工位接口即可。只有当需要改调度/并发模型（全异步、跨轮后台队列）或形状不符（非「一 prompt 一组采样」）时才升级。

**练习 2**：如果我只是想改 prompt 的抽样顺序，应该用哪个接口？为什么不用 `--rollout-function-path`？

> **参考答案**：用 `--data-source-path` 实现自定义 `DataSource`。因为它只换「原料供给」这一内层工位，仍复用默认 rollout 循环的全部能力；用 `--rollout-function-path` 会不必要地接管整条编排，把过采样、过滤、partial rollout 都重写一遍，得不偿失。

---

## 5. 综合实践

**任务**：为「带沙箱执行的代码 agent」选择合适的接口组合并说明理由；指出哪一步需要 fan-out 返回多 Sample。

这个任务其实就是 `coding_agent_rl` 的设计复盘。请按下面步骤完成：

### 步骤 1：需求拆解

假设你要训练一个代码 agent，每个样本的流程是：

1. 拉起一个隔离沙箱，里面是某个 GitHub 仓库的 checkout；
2. agent（大模型 + 工具）在沙箱里读代码、改代码，产出 `git diff`；
3. 在第二个干净沙箱里跑测试套件，按通过率给 0–1 的 reward；
4. 把整条 agent 轨迹变成训练样本。

### 步骤 2：选接口组合（写出你的选择与理由）

请在笔记里回答：

- **生成**：用 `--custom-generate-function-path` 还是 `--rollout-function-path`？为什么？
- **打分**：用 `--custom-rm-path` 独立函数，还是在 generate 里直接填 reward？为什么？
- **多轮 token 管理**：靠手写循环（如 search-r1）还是用 `slime.agent.adapters` 的 adapter？为什么？
- **是否需要升级**：这个场景需要全异步或跨轮续传吗？给出判据。

> 参考答案要点（对照真实示例自行核对）：
> - 生成用 `--custom-generate-function-path`（见 [README.md:84](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/coding_agent_rl/README.md#L84)，即 `--custom-generate-function-path examples.coding_agent_rl.generate.generate`），因为工作流仍符合「一 prompt → 一次执行 → 返回 Sample」的形状，无需升级。
> - 打分在 generate 内部直接算（`run_evaluation` 后随 `finish_session` 注入 reward），因为奖励依赖沙箱跑测试这种重状态操作，与生成强耦合。
> - 用 `AnthropicAdapter`（claude-code 说 Anthropic 协议），让 adapter 自动处理 string-in/token-out 与 loss_mask，避免手写多轮 token 管理的重分词陷阱。
> - 默认循环通常够用；只有当测试沙箱的长尾严重拖慢每轮 rollout 时，才考虑 fully_async（见 4.4）。

### 步骤 3：指出 fan-out 发生在哪一步

读 [generate.py:237-268](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/coding_agent_rl/generate.py#L237-L268) 与 [README.md:182-186](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/coding_agent_rl/README.md#L182-L186)，回答：

- fan-out 在哪个函数调用处发生？（提示：`finish_session` 返回 `list[Sample]`。）
- 为什么一条轨迹会被切成多段？（提示：子智能体派发、上下文压缩会让 prompt 前缀分叉，每个 root-to-leaf 链是一条 Sample。）
- 多段样本必须共享什么字段？不共享会怎样？（提示：`rollout_id`；不共享会被重复计数、奖励放大。）

### 步骤 4：验证 fan-out 的契约

打开 [slime/utils/types.py:99-106](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/types.py#L99-L106)，确认 `rollout_id` 字段的 docstring：

> Compact / subagent paths that split one rollout execution into multiple training samples should set the same `rollout_id` on every sibling, so loss aggregation averages within the rollout instead of over-counting it.

再对照 [README.md:184-185](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/coding_agent_rl/README.md#L184-L185) 的 fan-out 语义：每条轨迹的总 reward 按 `reward / K` 分摊到各段，`rollout_id` 共享，使得 per-rollout-mean 的 loss 归约仍把整条轨迹算作一次 rollout。

**完成标志**：你能用一段话讲清「这个代码 agent 用 custom-generate + 内联打分 + adapter + 必要时 fan-out 同 rollout_id」的完整选型理由，并指出 fan-out 发生在轨迹导出阶段。

## 6. 本讲小结

- slime **不为 agent 另起框架**：多轮工具/沙箱/环境反馈是一类数据生成工作流，接入已有的 rollout → buffer → training 闭环，靠可注入接口实现。
- **首选组合**是 `--custom-generate-function-path` + `--custom-rm-path`；只有当默认 rollout 循环的形状/调度/并发不够用时，才升级到 `--rollout-function-path`。
- 接口是**包裹层级**：外层（rollout-function）覆盖会让内层（custom-generate / custom-rm）挂载点失效，所以能用工位接口就别用流水线接口。
- 训练目标必须保持 **token-based**：agent 环境是字符串/消息/工具观测，但训练序列要用模型实际采样的 token，绝不能重新分词；`loss_mask` 的 1/0 区分模型生成与 prompt/模板/工具观测/环境文本。
- **正确性护栏**：当多轮 prompt 与之前采样输出在 token 层面漂移时，slime 保留上下文但把那段 `loss_mask` 置 0，不为来源不明的 token 反向传播。
- **fan-out**：一次 agent 执行可返回 `list[Sample]`，所有兄弟样本必须共享同一个 `rollout_id`，使 per-rollout-mean 归约不重复计数，总 reward 按 `reward / K` 分摊。

## 7. 下一步学习建议

本讲建立了智能体 RL 的**决策框架**。接下来按你关心的方向深入：

- **想搞懂 adapter 如何把 Anthropic/OpenAI 协议变成可训练 token**：进入 **u7-l2 Agent 运行时适配器**，精读 `slime/agent/adapters/` 与 `TrajectoryManager`，看 string-in/token-out 与 loss_mask 是如何被自动管理的。
- **想深入 fan-out 与轨迹分段**：进入 **u7-l3 多样本 fan-out 与轨迹分段训练**，结合 `examples/multi_agent` 与 `coding_agent_rl` 的子智能体/压缩分段，研究 `rollout_id` 同源聚合与 reward 分摊的实现。
- **关心长尾与吞吐**：进入 **u7-l4 流式、全异步与部分回滚 rollout**，对比 streaming、fully_async、partial rollout 三种高级数据流。
- **想看一个能跑的最小 agent 例子**：先读 [examples/search-r1/generate_with_search.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/search-r1/generate_with_search.py)，它是手写多轮循环 + loss_mask 的最佳入门范本。
- **服务端吞吐优化**（多轮 agent 上下文长、长尾重）：阅读 [docs/en/advanced/pd-disaggregation.md](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/advanced/pd-disaggregation.md) 与 sglang-config 文档（u8-l1/u8-l2）。
