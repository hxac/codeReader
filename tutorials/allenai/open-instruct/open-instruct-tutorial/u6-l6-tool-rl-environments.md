# 工具调用 RL：environments 与 ToolRL

> 对应大纲：u6-l6（高级层）。依赖 u6-l2（GRPO 损失与优势计算），并与 u6-l3（可验证奖励与 verifier 注册表）相衔接。

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 open-instruct 工具/环境体系的三个基类（`Tool` / `RLEnvironment` / `TextRLEnvironment`）各自适合什么场景，以及一次多轮 rollout 的完整状态机。
2. 掌握 `EnvCall`、`StepResult`、`RolloutState` 三个核心数据结构，理解每步奖励如何经 `LastRewardAggregator` / `SumRewardAggregator` 汇总成最终标量，再喂给 GRPO 的优势计算。
3. 读懂工具解析器（parsers）如何把模型文本变成 `EnvCall`、如何把环境输出格式化回对话，以及 MCP 服务器如何被封装成一个通用工具。
4. 精读 ToolRL 离线打分：调用名多重集重叠 + 参数贪心匹配的数学形式，以及本轮（PR #2250）`score_response` 新增的可选 `action` 参数——格式文本与已选定动作文本可分离传入，默认路径行为不变。

## 2. 前置知识

- **多轮 rollout**：u6-l1/u6-l2 里 GRPO 对每个 prompt 采样一组完整回答；启用工具/环境后，「一次回答」变成「模型生成 → 工具/环境执行 → 观察写回对话 → 继续生成」的循环，直到 `done=True` 或达到 `max_steps`。
- **EnvCall / Observation**：仓库通过 `openenv` 依赖包（`openenv.core.env_server.types`）引入 `Action` / `Observation` / `State` 三个 pydantic 基类型；本讲的 `EnvCall` 与 `StepResult` 都是在其上做的薄封装。
- **奖励的两个来源**：环境/工具在每一步给出的**过程奖励**（`StepResult.reward`），与 rollout 结束后由验证器给出的**最终奖励**（u6-l3 的 `apply_verifiable_reward`）。两者在 `RewardConfig.build` 里合并（见 4.3）。
- **思考边界**：Olmo 系模型输出形如 `<think>…</think>动作` 的结构。u6-l3 已介绍 MILES 正在统一的「共享判分输入」：思考边界只切一次，验证器通过 `selected_input` / `grading_*` 声明自己消费哪一段。本讲第 4.3 节的 `action` 参数正是这条契约在 ToolRL 上的落点（完整机制在 u8-l12）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [open_instruct/environments/base.py](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/base.py) | `EnvCall`、`StepResult`、`RolloutState` 与 `RLEnvironment` / `TextRLEnvironment` / `BaseEnvConfig` 抽象基类 |
| [open_instruct/environments/tools/utils.py](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/tools/utils.py) | `Tool` 无状态工具基类、`EnvsConfig` 配置、`coerce_args` 参数类型矫正、工具调用统计 |
| [open_instruct/environments/tools/parsers.py](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/tools/parsers.py) | `ToolParser` 抽象与 legacy / vLLM / DR-Tulu 三族解析器及注册表 |
| [open_instruct/environments/tools/generic_mcp.py](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/tools/generic_mcp.py) | `GenericMCPTool`：把任意 MCP 服务器的单个工具封装成 `Tool`，含重试与工具发现 |
| [open_instruct/environments/pool.py](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/pool.py) | `EnvironmentPool`：Ray actor 池，acquire/release 语义支撑并发 rollout |
| [open_instruct/environments/examples.py](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/examples.py) | `CounterEnv`、`GuessNumberEnv`（工具型）与 `WordleTextEnv`（文本型）三个示例环境 |
| [open_instruct/toolrl_utils.py](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/toolrl_utils.py) | ToolRL 离线打分：目标校验、调用解析、`_call_score` 语义打分与 `score_response` 总入口 |
| [open_instruct/miles/rewards/toolrl.py](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/rewards/toolrl.py) | MILES 对 ToolRL 的绑定：`async_call` / `async_call_parts` 与 grading 契约声明 |
| [open_instruct/ground_truth_utils.py](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py) | `RewardAggregator` 家族与 `RewardConfig.build`：每步奖励与验证器奖励的合并点 |
| [tests/test_environments.py](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/tests/test_environments.py) | 环境契约测试：reset 返回值、随机游走 20 步、Wordle 文本环境行为 |
| [docs/algorithms/tool_training.md](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/algorithms/tool_training.md) | 用户视角的工具/环境训练指南：脚本、参数、解析器选择 |

## 4. 核心概念与源码讲解

### 4.1 RLEnvironment 抽象：rollout 状态机的骨架

#### 4.1.1 概念说明

工具和环境回答同一个问题：「模型生成了一段文本，外部世界如何回应？」仓库把它抽象成三个基类：

| 基类 | 适用场景 | 子类需要实现 |
| --- | --- | --- |
| `Tool` | 无状态请求/响应集成（代码执行、网页搜索） | `step`（基类已提供默认 `reset`/`state`） |
| `RLEnvironment` | 有状态任务，模型通过**函数调用**行动（猜数字、沙箱） | `reset`、`step`、`state` |
| `TextRLEnvironment` | 有状态任务，环境直接读取模型**整段原始文本**（Wordle、模拟用户） | `_reset`、`text_step` |

关键设计：每个 rollout 从池里拿到一个**全新实例**，所以你可以放心把回合状态存在 `self` 上；`reset` 负责初始化并返回初始观察 + 工具定义（告诉模型可以调用哪些函数）。

#### 4.1.2 核心流程

一次启用工具/环境的 rollout（对照 [tool_training.md:L41-L53](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/algorithms/tool_training.md#L41-L53)）：

```
1. 从数据集样本构建 prompt（含工具定义，若 pass_tools_to_chat_template）
2. 模型生成一轮文本
3. 解析器提取结构化工具调用 → EnvCall；
   文本环境则把整段文本包成影子 EnvCall(args={"text": ...})
4. 工具/环境执行，返回 StepResult
5. StepResult.result 格式化后追加回对话（角色由 response_role 决定）
6. 重复 2-5，直到 done=True 或达到 max_steps
7. 每步奖励按 --reward_aggregator 聚合，再与最终验证器奖励合并
```

这是一个典型的「环境-智能体」交互循环：`(s_t, a_t) → r_t, o_t`，其中 `a_t` 是 `EnvCall`，`(r_t, o_t)` 打包在 `StepResult` 里，整条轨迹的累积量落进 `RolloutState`。

#### 4.1.3 源码精读

**EnvCall 与 StepResult**（[base.py:L13-L25](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/base.py#L13-L25)）：`EnvCall` 在 openenv 的 `Action` 上加了 `id/name/args` 三个字段；`StepResult` 在 `Observation` 上声明 `result`（写回对话的观察文本）与 `reward`（该步奖励，默认 0.0）。`done` 与 `metadata` 从 `Observation` 继承——从示例环境（如 `done=True, metadata={"guesses": ...}`）与测试断言（`r.done`）的用法可以确认。

**RolloutState**（[base.py:L28-L40](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/base.py#L28-L40)）：一个 rollout 的累积账本——`rewards` 列表（每步一个）、`step_count`、`done`、工具输出/错误/耗时、超时标志和 `tool_call_stats`。4.3 节会看到 `rewards` 正是聚合器的输入。

**RLEnvironment 抽象契约**（[base.py:L43-L82](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/base.py#L43-L82)）：三个抽象方法定义了环境生命周期——`reset` 返回 `(初始 StepResult, 工具定义列表)`；`step` 接收 `EnvCall` 返回 `StepResult`；`state` 返回可序列化的 `State`。`setup` 在训练开始时调用一次（用于资源初始化），`get_metrics` 供训练日志读取自定义指标，`get_tool_definitions` 以 OpenAI function calling 格式暴露动作空间。注意 `response_role = "tool"`：观察默认以 tool 角色写回对话。

**TextRLEnvironment**（[base.py:L85-L119](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/base.py#L85-L119)）：调度管线把模型整段输出装进影子 `EnvCall` 的 `args["text"]`，`step` 只做一次转发（[L117-L119](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/base.py#L117-L119)），子类实现 `text_step`；`response_role` 默认改为 `"user"`，营造「用户回话」的多轮对话感；`reset` 恒返回空工具列表（[L112-L115](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/base.py#L112-L115)）。

**BaseEnvConfig**（[base.py:L122-L135](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/base.py#L122-L135)）：每个环境配一个 dataclass 配置，`tool_class` 类变量把配置与实现类配对，构造参数（API 端点、游戏参数等）放这里，而非塞进 `EnvCall` 接口；`pool_size` 可按工具覆盖全局并发度。

**参考实现 GuessNumberEnv**（[examples.py:L109-L185](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/examples.py#L109-L185)）：`reset(number=...)` 设定秘密数字并返回提示；`step` 对每次猜测给出**稠密的过程奖励**——接近度 `1 - distance/max_distance`（[L164-L166](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/examples.py#L164-L166)），猜中给 1.0 并 `done=True`。注意它演示了「有状态」：秘密数字与猜测次数都存在实例上。

**环境池**（[pool.py:L15-L58](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/pool.py#L15-L58)）：`EnvironmentPool` 是一个异步 Ray actor，构造时批量创建环境 actor 并全部调用 `setup`；`acquire` 从 `asyncio.Queue` 取一个空闲 actor（带超时报警），`release` 归还并唤醒等待者。并发 rollout 各自独占一个环境实例，这正是「状态可存在 self 上」的保障。

#### 4.1.4 代码实践

**实践 A：运行环境契约测试**

1. 实践目标：确认环境契约测试可跑，理解它验证了什么。
2. 操作步骤：`uv run pytest tests/test_environments.py -v`（Wordle 用例会经 nltk 下载词表）。
3. 需要观察的现象：`TestEnvironmentReset`（[tests/test_environments.py:L13-L31](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/tests/test_environments.py#L13-L31)）对三个环境断言 reset 返回 `(StepResult, list)` 且文本环境工具列表为空；`TestEnvironments`（[L34-L52](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/tests/test_environments.py#L34-L52)）用 `_random_env_call` 按工具 schema 随机填参游走 20 步，`done` 后自动 reset。
4. 预期结果：全部通过（依赖 openenv/nltk 就绪；本机未验证，待本地验证）。

**实践 B：实现你自己的最小环境并用 SumRewardAggregator 聚合**

1. 实践目标：把 `RLEnvironment` 契约落地成一个新环境（如「猜骰子」：秘密点数 1-6，猜中得 1.0，偏差越大过程奖励越低），并手工走一条 rollout 记录 `StepResult`。
2. 操作步骤（以下为示例代码，仿照 [examples.py:L109-L185](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/examples.py#L109-L185) 的 GuessNumberEnv 编写）：

```python
# 示例代码：dice_env.py
import asyncio
from open_instruct.environments.base import BaseEnvConfig, EnvCall, RLEnvironment, StepResult

class DiceEnv(RLEnvironment):
    config_name = "dice"
    _tools = [{"type": "function", "function": {
        "name": "roll_guess", "description": "Guess the dice value (1-6)",
        "parameters": {"type": "object",
                       "properties": {"value": {"type": "integer"}},
                       "required": ["value"]}}}]
    def __init__(self, **kw):
        self._secret, self._done = 0, False
    @classmethod
    def get_tool_definitions(cls): return cls._tools
    async def reset(self, **kw):
        self._secret, self._done = int(kw["value"]), False
        return StepResult(result="Guess the dice value (1-6)."), self._tools
    async def step(self, call: EnvCall) -> StepResult:
        g = int(call.args.get("value", 0))
        if g == self._secret:
            self._done = True
            return StepResult(result="Correct!", reward=1.0, done=True)
        return StepResult(result=f"{g} is off by {abs(g - self._secret)}.",
                          reward=0.2 * (3 - abs(g - self._secret)) / 3)
    def state(self): return {}  # 简化：无外部状态

async def main():
    from open_instruct.ground_truth_utils import LastRewardAggregator, SumRewardAggregator
    env = DiceEnv()
    _, tools = await env.reset(value=5)
    rewards = []
    for guess in (2, 4, 5):                       # 模拟模型的三次行动
        r = await env.step(EnvCall(id="1", name="roll_guess", args={"value": guess}))
        rewards.append(r.reward)
        if r.done: break
    print(rewards, SumRewardAggregator()(rewards), LastRewardAggregator()(rewards))

asyncio.run(main())
```

3. 需要观察的现象：`rewards` 收集了三步过程奖励；`SumRewardAggregator` 求和、`LastRewardAggregator` 取末步（猜中那步的 1.0）。
4. 预期结果：`sum` 聚合值 ≈ `0.2*(3-3)/3 + 0.2*(3-1)/3 + 1.0 = 0 + 0.133… + 1.0`；`last` 聚合值 = 1.0。具体数值待本地验证。
5. 若要把该环境接入训练，还需仿照 [examples.py:L188-L193](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/examples.py#L188-L193) 补一个 `DiceEnvConfig(BaseEnvConfig)` 并注册到工具注册表（`TOOL_REGISTRY`，见 [tool_training.md:L31](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/algorithms/tool_training.md#L31)）。

#### 4.1.5 小练习与答案

1. **练习**：为什么 `TextRLEnvironment` 的 `response_role` 是 `"user"` 而 `Tool` 是 `"tool"`？
   **答案**：工具型环境的观察是「函数返回值」，语义上属于对话中的 tool 角色；文本环境（如 Wordle）的反馈是对模型文本的「回应」，用 user 角色能营造自然的多轮对话格式，与聊天模板兼容（见 [base.py:L85-L100](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/base.py#L85-L100) 的类文档与 [tests/test_environments.py:L121-L122](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/tests/test_environments.py#L121-L122) 的断言）。
2. **练习**：`WordleTextEnv` 的奖励 `1.0 + 1.0/total_turns + format_reward`（[examples.py:L316-L322](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/examples.py#L316-L322)）中三个部分各奖励什么？
   **答案**：1.0 是猜中的任务奖励；`1.0/total_turns` 是回合效率奖励（越早猜中越高）；`format_reward = 0.2 * (带标签回合数/总回合数)` 是格式合规奖励（[examples.py:L252-L254](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/examples.py#L252-L254)）。测试 [tests/test_environments.py:L83-L96](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/tests/test_environments.py#L83-L96) 固定了这两个数值。
3. **练习**：如果两个 rollout 并发拿到同一个 `GuessNumberEnv` 实例会发生什么？仓库如何避免？
   **答案**：秘密数字与猜测次数会互相踩踏。`EnvironmentPool` 用 acquire/release 语义保证同一时刻一个实例只被一个 rollout 持有（[pool.py:L44-L55](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/pool.py#L44-L55)）。

### 4.2 工具解析器与 MCP 集成：从模型文本到 EnvCall

#### 4.2.1 概念说明

解析器（parser）是模型与环境之间的「翻译层」，负责两件事：从模型生成文本中**提取工具调用**（产出 `EnvCall`），以及把环境输出**格式化**回对话（套上聊天模板的角色包装）。仓库支持三族解析器：`legacy`（XML 标签）、`vllm_*`（包装 vLLM 原生解析器，官方推荐）、`dr_tulu`（DR-Tulu 风格）。MCP（Model Context Protocol）则解决另一个问题：不写代码就能把任意外部 MCP 服务器的工具接入训练。

#### 4.2.2 核心流程

```
模型文本
  └─ ToolParser.get_tool_calls(text) → list[EnvCall]     # 提取
       └─ EnvironmentPool.acquire → env.step(EnvCall)      # 执行（4.1）
            └─ StepResult.result
                 └─ ToolParser.format_tool_outputs(...)    # 按角色模板包装写回对话
```

对 MCP 工具，`step` 内部是：矫正参数类型 → 建立连接（http/sse/stdio）→ `session.call_tool` → 抽取文本结果；可重试错误按指数退避重试，超时单列。

#### 4.2.3 源码精读

**ToolParser 抽象**（[parsers.py:L31-L61](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/tools/parsers.py#L31-L61)）：两个核心方法 `get_tool_calls`（文本 → 调用）与 `_format_tool_outputs`（输出 → 对话文本）；`format_tool_outputs` 公共入口做防御性字符串矫正并清除 fast tokenizer 的 Rust 后端拒绝的空字节（[L42-L56](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/tools/parsers.py#L42-L56)）。

**legacy 解析器**（[parsers.py:L64-L118](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/tools/parsers.py#L64-L118)）：把 `<tool_name>content</tool_name>` 标签内容填进工具的第一个必选参数；只适合单字符串参数的工具，`</tool_name>` 自动成为停止串。

**vLLM 包装与注册表**（[parsers.py:L127-L193](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/tools/parsers.py#L127-L193) 与 [L225-L264](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/tools/parsers.py#L225-L264)）：`VllmToolParser` 委托 vLLM 原生解析器（Hermes、Llama3 JSON、Olmo3 pythonic、Qwen3.5 XML）提取调用，再对 JSON 解析失败的参数记警告跳过（[L176-L185](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/tools/parsers.py#L176-L185)）；每个 `VllmParserConfig` 携带**按角色的输出模板**（如 hermes 的 tool 角色包 `<|im_start|>tool
<tool_response>
…`）与输出后缀（重新开启 assistant 回合）。`create_tool_parser`（[L340-L374](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/tools/parsers.py#L340-L374)）按类型名分发，`get_available_parsers`（[L335-L337](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/tools/parsers.py#L335-L337)）列出全部合法名字。

**Tool 基类**（[tools/utils.py:L384-L433](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/tools/utils.py#L384-L433)）：无状态工具只需实现 `step`；默认 `reset` 直接返回工具自身的 OpenAI 格式定义（`get_openai_tool_definitions`，[L315-L331](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/tools/utils.py#L315-L331)）。`coerce_args`（[L359-L381](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/tools/utils.py#L359-L381)）按 JSON schema 把模型给的参数矫正成期望类型（如字符串 `"3"` → 整数 3），MCP 工具执行前就会调用它。

**GenericMCPTool**（[generic_mcp.py:L66-L171](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/tools/generic_mcp.py#L66-L171)）：三种传输（http 流式 / SSE / stdio 子进程，[L26-L42](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/tools/generic_mcp.py#L26-L42)）；`step`（[L116-L171](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/tools/generic_mcp.py#L116-L171)）对 `ConnectionError/TimeoutError/OSError` 做**指数退避重试**（`backoff * 2^attempt`），`asyncio.wait_for` 控制总超时且超时不重试、单独返回 `timeout=True` 元数据；其余异常立即以错误元数据返回。配置类 `GenericMCPToolConfig`（[L174-L224](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/tools/generic_mcp.py#L174-L224)）的 `expand_tools` 能连上服务器 `list_tools` 一次性发现全部工具并**展开成每工具一个配置**（[L211-L224](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/tools/generic_mcp.py#L211-L224)）——注释里明确假设训练期间工具列表不变。

**EnvsConfig**（[tools/utils.py:L34-L107](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/tools/utils.py#L34-L107)）：`--tools` / `--tool_call_names` / `--tool_configs` 三个 CLI 参数的载体，`__post_init__` 校验三者长度一致并把 JSON 字符串解析成 `ParsedEnvConfig` 三元组；`max_steps`（默认 5）就是 rollout 状态机的步数上限。

#### 4.2.4 代码实践

1. 实践目标：不启动任何服务，纯阅读弄清「同一个环境输出在不同解析器下写回对话的文本长什么样」。
2. 操作步骤：
   - 打开 [parsers.py:L225-L264](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/tools/parsers.py#L225-L264)，对每个注册表条目抄下 `role_templates["tool"]` 与 `output_postfix`；
   - 手工把观察文本 `{"temp": 72}` 代入 `{output}` 占位符，对比 hermes / llama3_json / olmo3 / qwen3_xml 四种包装；
   - 回答：为什么 `vllm_qwen3_xml` 的 `output_postfix` 以 `<think>
` 结尾（提示：注释 [L253-L255](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/tools/parsers.py#L253-L255)）？
3. 需要观察的现象：同一观察在不同模型家族的聊天模板里需要完全不同的包装；qwen3_xml 回合后重新打开 `<think>` 块。
4. 预期结果：写出一张四行对照表；qwen3 的默认是思考模式，所以下一回合以思考开头（这会与 u6-l3 讲过的「思考前缀在 prompt 中」情形呼应）。
5. 若本地装好 vllm，可再运行 `uv run python -c "from open_instruct.environments.tools.parsers import get_available_parsers; print(get_available_parsers())"` 验证注册表（待本地验证）。

#### 4.2.5 小练习与答案

1. **练习**：`--tools python serper_search --tool_call_names code search` 中两列名字分别用在哪？
   **答案**：`python`/`serper_search` 用于在注册表里查找实现类；`code`/`search` 是模型在函数调用接口里看到并输出的名字（[tool_training.md:L92-L108](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/algorithms/tool_training.md#L92-L108)），便于换后端而不改模型习惯。
2. **练习**：MCP 工具调用超时后为什么不像连接错误那样重试？
   **答案**：`step` 里 `asyncio.TimeoutError` 分支直接返回带 `timeout=True` 元数据的空结果（[generic_mcp.py:L138-L148](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/tools/generic_mcp.py#L138-L148)）；重试只针对 `ConnectionError/TimeoutError/OSError` 这类**可重试异常**（注意这里的 `TimeoutError` 是 socket 级超时，在可重试列表中，而 `asyncio.TimeoutError`（wait_for 触发）走单列分支——两者在 Python 3.11+ 虽是同一类型，但代码按捕获顺序先匹配 `asyncio.TimeoutError` 分支，因此 wait_for 超时不重试）。此处行为以源码分支为准，细究时可待本地验证。
3. **练习**：想给训练加一个「天气 MCP 服务器」的全部工具，最少要写什么？
   **答案**：一行配置：`--tools generic_mcp --tool_configs '{"server_url": "https://…/mcp", "transport": "http"}'`，`expand_tools` 会在启动时发现并展开成每工具一个 `GenericMCPToolConfig`（[generic_mcp.py:L211-L224](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/tools/generic_mcp.py#L211-L224)）。

### 4.3 ToolRL 离线打分与奖励聚合

#### 4.3.1 概念说明

前面两节是「在线」奖励：环境在交互中给分。ToolRL 则是**离线**打分器——不执行任何工具，只比较「模型实际发出的调用序列」与「数据集里标注的参考调用序列」，给格式分 + 语义分。它改编自 qiancheng0/ToolRL（Apache-2.0），文件头注明了改动：无第三方依赖、显式固定默认奖励设置、结构化诊断、原生 Olmo XML 调用解析、规范化目标，且**只对思考块之后的调用打分**（[toolrl_utils.py:L1-L10](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/toolrl_utils.py#L1-L10)）。

与之配套的是奖励聚合器：环境给了每步奖励、验证器给了最终奖励，GRPO 需要一个标量——`LastRewardAggregator`（稀疏奖励环境，只认最后一步）与 `SumRewardAggregator`（稠密奖励环境，逐步累加）。

**本轮更新（PR #2250）**：`score_response` 新增可选关键字参数 `action`。此前动作文本只能从 `prediction` 里自己切（在第一个 `</think>` 后取）；现在外部可以传入「已选定的动作文本」，格式检查仍用原始完整输出。这是 MILES 统一判分输入（u8-l12）的一部分：思考边界由共享选择器只切一次，ToolRL 的 correctness 组件直接消费选定的 response，format 组件继续看完整边界。**不传 `action` 时默认路径行为完全不变。**

#### 4.3.2 核心流程

`score_response`（[toolrl_utils.py:L129-L170](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/toolrl_utils.py#L129-L170)）的执行顺序：

```
1. validate_target：校验参考目标（版本 olmo-toolrl-v1、工具 schema、调用合法性）
2. 剥掉结尾的 <|im_end|>/<|endoftext|> 终止符（只剥一次，保留参数内部的）
3. think_prefix_in_prompt=True 时补回 "<think>" 前缀
4. 格式判定：整串匹配 <think>…</think>\s*(.*) 且 think 标签恰好一对
5. 选定动作文本：
   - 新增：外部传入 action → 直接使用（仅 strip）
   - 默认：在第一个 </think> 后切取；若该区域又出现 <think> 则清零
6. 有参考调用 → 用严格解析器（原生 XML 或 pythonic）解析动作文本，
   解析失败 → format=0 且 correctness=-3；
   无参考调用 → 只查格式（动作非空且不含工具类标签），correctness=0
7. 返回 ToolRLScore(format + correctness, format, correctness)
```

语义分 `_call_score` 的数学形式（[toolrl_utils.py:L71-L93](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/toolrl_utils.py#L71-L93)）：

- **调用名匹配**用多重集 Jaccard 重叠。设期望名 multiset 为 \(E\)、预测名为 \(P\)：

  \[ \text{name\_overlap} = \frac{|E \cap P|}{|E \cup P|} \]

  （`_overlap` 用 `Counter` 的交并与实现，[L64-L68](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/toolrl_utils.py#L64-L68)；并集为空时约定为 1。）

- **参数匹配**对每个期望调用，在**同名**未占用预测里贪心选匹配分最高的：参数键集合的 Jaccard 重叠 + 值完全相等的键数。

- 归一化。设累计匹配分为 \(s\)，满分基数为 \(p = 1 + \sum_{c \in E}(1 + |\text{params}(c)|)\)，则：

  \[ \text{correctness} = -3 + 6 \cdot \frac{s}{p} \]

  全对时 correctness = +3，加上 format 分 1，总分恰为 4——这就是文档所说「ToolRL 发布分值在有调用行上取值 [-3, 4]」的来源（[miles/rewards/toolrl.py:L3-L5](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/rewards/toolrl.py#L3-L5)）。

奖励聚合与合并发生在 `RewardConfig.build`（[ground_truth_utils.py:L1380-L1451](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L1380-L1451)）：

```
turn_rewards = rollout_state["rewards"]          # 每步环境奖励
turn_rewards[-1] += verifier_score               # 验证器奖励叠加到最后一步
raw_score = aggregator(turn_rewards)             # last 或 sum
```

聚合后的标量进入 u6-l2 讲过的 GRPO 组内标准化优势计算，驱动策略更新。

#### 4.3.3 源码精读

**ToolRLScore**（[toolrl_utils.py:L20-L24](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/toolrl_utils.py#L20-L24)）：冻结 dataclass，把总分与两个分量（`format_score`/`correctness_score`）分开返回，供诊断。

**validate_target**（[toolrl_utils.py:L96-L126](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/toolrl_utils.py#L96-L126))：参考目标必须是 `{"format": "olmo-toolrl-v1", "calls": [...], "tools": [...], "tool_format": "olmo_xml_v1"|"olmo_pythonic_v1", "think_prefix_in_prompt": bool}`；**坏目标是数据错误，直接抛 `ValueError`，而不是惩罚模型**（测试 `test_bad_target_raises_instead_of_punishing_model`，[test_toolrl.py:L276-L277](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/test_toolrl.py#L276-L277)）。带版本的格式串防止旧 JSON 格式数据被静默打分。

**score_response 主体与本轮新增的 action 参数**（[toolrl_utils.py:L129-L151](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/toolrl_utils.py#L129-L151)）：签名新增 `action: str | None = None`（[L129-L131](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/toolrl_utils.py#L129-L131)）。关键三行：

```python
selected_action = action is not None
if action is None:
    action = prediction.split("</think>", 1)[1] if "</think>" in prediction else prediction
action = action.strip()
if not selected_action and "<think>" in action:
    action = ""
```

（[L146-L151](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/toolrl_utils.py#L146-L151)）外部传入时**跳过本地切分与「动作区含 `<think>` 则清零」的守卫**——因为共享选择器已经保证传入的是选定的 delivered response，不需要也不应该再切一次；格式判定（第 144-145 行）仍然作用在原始 `prediction` 上，边界标记数不对照样扣格式分。

**严格调用解析**（[tool_call_parsing.py:L241-L271](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/rewards/tool_call_parsing.py#L241-L271)）：`parse_native_calls` 要求动作文本的**所有非空白内容**都是完整的 `<tool_call><function=名><parameter=键>值</parameter>…</function></tool_call>` 调用——并行调用各自独立包装，重复参数、畸形尾部一律 fail closed 抛异常，进而在 `score_response` 里落为 `format=0, correctness=-3`（[toolrl_utils.py:L152-L161](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/toolrl_utils.py#L152-L161)）。这是「离线奖励的严格解析器，不是宽容的 serving 解析器」（其文档字符串原文）。

**RewardAggregator 家族**（[ground_truth_utils.py:L1088-L1106](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L1088-L1106)）：`LastRewardAggregator` 取 `rewards[-1]`（空则 0），`SumRewardAggregator` 求和。选择由 CLI 参数 `--reward_aggregator` 控制（`data_loader.py` 中声明，[data_loader.py:L457-L458](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/data_loader.py#L457-L458)，默认 `"last"`），并在 `RewardConfig.build` 里实例化（[ground_truth_utils.py:L1377-L1384](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L1377-L1384)）。合并点在 [L1434-L1451](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L1434-L1451)：验证器分数叠加到末步奖励后统一聚合——所以 `sum` 模式下环境过程奖励与任务奖励都计入，`last` 模式下历史过程奖励被丢弃。

**MILES 消费侧：async_call_parts**（[miles/rewards/toolrl.py:L60-L69](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/rewards/toolrl.py#L60-L69)）：`ToolRLVerifier` 声明为复合（composite）判分器（[L34-L40](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/rewards/toolrl.py#L34-L40)）：`correctness` 组件是 answer/response 目标，`format` 组件是 format/full 目标。共享分发器解析出各组件的输入后，`async_call_parts` 把**完整文本**（format 组件输入）作 `prediction`、**选定响应**（correctness 组件输入）作 `action` 传给 `_grade`——该方法本体不再切任何文本（其注释原文："this method never splits text"）。传统的 `async_call`（[L53-L58](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/rewards/toolrl.py#L53-L58)）不传 action，走默认路径。回归测试 [test_miles_grading_toolrl.py:L22-L40](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/test_miles_grading_toolrl.py#L22-L40) 固定了新行为：对 `<think>work</think>old action</think>delivered`，重复标记仍判格式失败，但动作选择与其他判分器共享（选到 `delivered`，而不是旧的「第一个标记之后」区域）。

**分数缩放**（[miles/rewards/toolrl.py:L71-L97](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/rewards/toolrl.py#L71-L97)）：`_grade` 默认把 [-3, 4] 仿射映射到 [0, 1]（`unit_scale=True`，`score = (score+3)/7`），因为 MILES 其余奖励都在 [0, 1]；组内这是正缩放，保持排序与相对间距。诊断字段保留原始分值与 `full_success`、`all_calls_required` 等。

#### 4.3.4 代码实践

**实践：对比 `score_response` 传入与不传 `action` 的打分差异**

1. 实践目标：亲手验证 `action` 参数的语义——外部传入的动作文本直接用于语义判分，而格式判定仍看原始输出。
2. 操作步骤（示例代码，可直接保存为脚本运行）：

```python
# 示例代码：action_param_demo.py
from open_instruct import toolrl_utils

TOOLS = [{"type": "function", "function": {
    "name": "giveaways", "description": "games",
    "parameters": {"type": "object",
                   "properties": {"platform": {"type": "string"}}}}}]
reference = {
    "format": "olmo-toolrl-v1", "tool_format": "olmo_xml_v1",
    "tools": TOOLS,
    "calls": [{"name": "giveaways", "parameters": {"platform": "xbox"}}],
}
ACTION = '<tool_call>\n<function=giveaways>\n<parameter=platform>xbox</parameter>\n</function>\n</tool_call>'

# 场景一：干净的完整输出（不传 action，走默认本地切分）
clean = "<think>plan</think>\n" + ACTION
print(toolrl_utils.score_response(clean, reference))
# 预期：ToolRLScore(score=4.0, format_score=1.0, correctness_score=3.0)

# 场景二：思考边界重复（多出一个 </think>）——默认路径
messy = "<think>plan</think>draft</think>\n" + ACTION
print(toolrl_utils.score_response(messy, reference))
# 预期：本地在第一个 </think> 切分 → 动作区含杂质 → 解析失败
#       correctness=-3.0，format=0.0（标签数不为一对）→ score=-3.0

# 场景三：同样的 messy 输出，但外部传入已选定的动作文本
print(toolrl_utils.score_response(messy, reference, action=ACTION))
# 预期：格式仍失败（原始输出标签不成对）→ format=0.0；
#       但语义用传入的 ACTION 解析成功 → correctness=3.0 → score=3.0
```

3. 需要观察的现象：场景二与场景三的 `format_score` 都是 0（格式看原始边界），但 `correctness_score` 一个 -3、一个 +3——差别只在于动作文本由谁选定。
4. 预期结果：如注释所示（场景一 4/1/3，场景二 -3/0/-3，场景三 3/0/3）。数值推演：单调用全对时 \(p = 1 + (1+1) = 3\)，\(s = 1_{\text{名字}} + 1_{\text{键}} + 1_{\text{值}} = 3\)，correctness \(= -3 + 6 \times 3/3 = 3\)。待本地验证。
5. 再运行 `uv run pytest open_instruct/test_toolrl.py open_instruct/test_miles_grading_toolrl.py -v`，重点读 `test_reasoning_only_and_malformed_tail_do_not_supply_actions`（[test_toolrl.py:L238-L243](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/test_toolrl.py#L238-L243)）与 `test_toolrl_and_other_graders_share_last_marker_action`（[test_miles_grading_toolrl.py:L22-L40](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/test_miles_grading_toolrl.py#L22-L40)），对照你观察到的行为。

#### 4.3.5 小练习与答案

1. **练习**：期望两次调用 `giveaways(xbox)`、`giveaways(android)`，模型只发出了 `giveaways(xbox)`。手工算 correctness。
   **答案**：名字重叠 \(1/2\)；xbox 调用匹配分 2（键重叠 1 + 值相等 1），android 无同名可配；\(s = 0.5 + 2 = 2.5\)，\(p = 1 + 2 + 2 = 5\)，correctness \(= -3 + 6 \times 2.5/5 = 0\)。这正是测试断言 `missing.correctness_score == pytest.approx(0)`（[test_toolrl.py:L57-L63](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/test_toolrl.py#L57-L63)）。
2. **练习**：为什么「无参考调用」的目标只查格式、绝不查答案事实？
   **答案**：目标里没有可验证的事实依据，ToolRL 是**调用匹配器**而不是事实核查器；文档字符串明确「No-call targets grade plain answer format only, never factual correctness」（[toolrl_utils.py:L134](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/toolrl_utils.py#L134)），对应分支在 [L162-L168](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/toolrl_utils.py#L162-L168)（correctness 恒 0，且动作里出现 `</tool_call>` 之类标签即格式失败）。
3. **练习**：`sum` 聚合器下，把 `verification_reward` 从 10 调到 20 会如何改变 4.1 节 DiceEnv 三步奖励的相对权重？
   **答案**：验证器分数叠加在**最后一步**（[ground_truth_utils.py:L1439-L1442](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/ground_truth_utils.py#L1439-L1442)），因此末步奖励被放大，过程奖励（前两步的接近度分）在总和里的占比下降——调大任务奖励就是在压低过程奖励的话语权。

## 5. 综合实践

**任务：为「带工具调用的猜数字」搭一条迷你奖励链路。**

把本讲三个模块串起来，全程不需要 GPU：

1. **环境侧**（4.1）：写一个 `AdditionQuizEnv(RLEnvironment)`——`reset` 给出一道两位数加法题，模型用 `answer(value=...)` 工具作答，答对 `reward=1.0, done=True`，答错给 `0.1 * (1 - |diff|/100)` 的过程奖励并允许重试（上限 3 次，超过后 `done=True` 且 `reward=-0.5`）。补上对应的 `BaseEnvConfig` 子类。
2. **解析器侧**（4.2）：不必接真实模型——写一个 `fake_model(prompt, turn)` 函数，返回 legacy 格式的调用文本 `<answer>42</answer>`，用 `OpenInstructLegacyToolParser.get_tool_calls`（[parsers.py:L104-L118](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/tools/parsers.py#L104-L118)）把它变成 `EnvCall`，再用 `format_tool_outputs` 把环境观察包回对话文本，循环到 `done`。
3. **打分侧**（4.3）：整个 rollout 结束后，收集所有 `StepResult.reward` 到一个列表，分别用 `LastRewardAggregator` 与 `SumRewardAggregator` 聚合并对比；再按 4.3.4 的参考目标格式构造一个「期望模型第一步就调用 `answer`」的 ToolRL 目标，对 rollout 的完整文本跑一次 `score_response`（可自行构造含 `<think>` 的输出体会 `action` 参数）。
4. **验收**：打印每步的 `(动作, 观察文本, reward)`、两种聚合值与 ToolRL 三元组分数；解释为什么这个环境更适合 `sum` 聚合（稠密过程奖励不应被丢弃）。
5. 完整脚本可运行于纯 CPU 环境；具体输出待本地验证。

## 6. 本讲小结

- 工具/环境体系由三个基类分层：`Tool`（无状态）、`RLEnvironment`（函数调用驱动的有状态环境）、`TextRLEnvironment`（直接消费模型原始文本）；`reset/step/state` 构成回合生命周期，`EnvironmentPool` 的 acquire/release 保证每 rollout 独享实例。
- 一次 rollout 是「生成 → 解析 → 执行 → 观察 写回」的循环，直到 `done=True` 或 `max_steps`；观察以 `response_role` 指定的角色回到对话。
- 解析器负责双向翻译：`get_tool_calls` 提取 `EnvCall`，`format_tool_outputs` 按模型家族的角色模板包装观察；推荐 `vllm_*` 族；`GenericMCPTool` 经 `expand_tools` 可把整个 MCP 服务器展开成一组工具，带指数退避重试与独立超时分支。
- ToolRL 离线打分 = 格式分（边界成对、恰一对 think 标签）+ 语义分（名字多重集 Jaccard + 同名贪心参数匹配，归一化到 [-3, 3]）；坏参考目标抛异常而非惩罚模型。
- 每步环境奖励与最终验证器奖励在 `RewardConfig.build` 合并：验证器分叠加末步后按 `last`/`sum` 聚合，结果进入 GRPO 优势计算。
- 本轮更新（PR #2250）：`score_response` 新增可选 `action` 参数——外部传入已选定动作文本时跳过本地思考边界切分，格式判定仍用原始输出；MILES 的 `async_call_parts` 复合判分以此让 correctness 与其他判分器共享同一选定 response，默认路径行为不变。

## 7. 下一步学习建议

- **u6-l7 / u6-l8**：代码类可验证奖励与沙箱执行、`execution_client` 协议——另一种「工具」形态（执行服务 + 判分适配）。
- **u8-l12（MILES 统一判分输入）**：`action` 参数的完整背景——`split_response`/`ResponseParts`、`GradingPolicy` 的 objective×scope×require_response 契约与 schema 2 选择表。
- **u6-l3 回顾**：把本讲的 `async_call_parts` 放回 `VerifierFunction` 注册表与 `grading_*` 声明的大图里理解。
- 源码延伸阅读：[open_instruct/environments/generic_sandbox.py](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/environments/generic_sandbox.py)（沙箱环境如何组合多个工具）与 [scripts/train/debug/envs/guess_number_1gpu.sh](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/scripts/train/debug/envs/guess_number_1gpu.sh)（把本讲环境真正跑进 GRPO 训练的最小脚本）。
