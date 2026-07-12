# Agentic RL：多轮工具调用与延迟奖励

## 1. 本讲目标

本讲是「强化学习后训练」单元的收官篇。在 u7-l4 里，我们把 GRPO/CISPO 的「采样→奖励→优势→损失→更新」串成了一条**单轮**链路：一个问题、一次回答、一个奖励。但真实世界里的智能体不是这样工作的——它会**多步交互**：先想、再调用工具、看到工具返回的结果、再继续想、再回答。

学完本讲，你应当能够：

1. 说清「多轮 Tool-Use rollout」的循环结构：生成 `tool_call` → 执行工具 → 把 observation 拼回上下文 → 续写，直到模型给出最终答案或达到轮数上限。
2. 解释 Agentic RL 的「延迟整轮奖励」\(R(\tau)=R_{\text{answer}}+R_{\text{tool}}+R_{\text{format}}+R_{\text{rm}}-R_{\text{unfinished}}\) 是如何在 `calculate_rewards` 里被拆成「无工具分支」与「有工具分支」两套打分逻辑的。
3. 理解 `rollout_single` 里 `response_mask` 的精妙设计：**只有策略模型生成的 token 才打 1（参与 loss），环境注入的 observation token 打 0（只作上下文）**。
4. 看懂 `rl_train_epoch` 如何把 u7-l4 的 GRPO/CISPO 损失**原封不动**地接到多轮轨迹上——同样的小组归一化优势、同样的 ratio、同样的 token 级 KL、同样的 `loss_type` 分支。
5. 理解若干工程细节：`gt` 校验、工具合法性检查、`SIGALRM` 超时、completion_mask 在首个 EOS 后截断、序列超长时**从左侧截断**保留最近上下文。

---

## 2. 前置知识

本讲默认你已掌握前置讲义中的以下概念（不会重复展开）：

- **u7-l1 / u7-l4 的 PO 统一框架**：所有 xxPO 都在优化「策略项 × 优势项 − 正则项」，DPO/GRPO/CISPO 只是三项的填法不同。本讲的损失函数与 u7-l4 的 GRPO/CISPO **完全相同**，区别只在「数据怎么来」和「奖励怎么算」。
- **u7-l2 的 Rollout 引擎**：`RolloutEngine.rollout(...)` 返回 `RolloutResult`（含 `completion_ids` / `per_token_logps` / `completions`），`update_policy(model)` 把训练侧的新权重同步回采样侧。本讲的 `rollout_single` 会反复调用它。
- **u7-l3 的奖励模型**：`LMForRewardModel.get_score(messages, response)` 返回一个 \([-3,3]\) 的连续分数，本讲在「无工具」分支里用作 \(R_{\text{rm}}\)。
- **u2-l1 的 chat_template 与工具标记**：`<tool_call>` / `</tool_call>` / `<tool_response>` / `</tool_response>` / `<think>` / `</think>` 均为 `special=False` 的可训练标记；`tools` 挂在 system 消息上、`tool_calls` 挂在 assistant 消息上，由 jinja 模板展开成结构化片段。

两个本讲才出现的新术语先点一下：

- **轨迹（trajectory）\(\tau\)**：一次 rollout 产生的多轮交互序列 \(\tau=(a_1,o_1,a_2,o_2,\dots,a_T)\)，其中 \(a_t\) 是模型动作、\(o_t\) 是环境观测。Agentic RL 优化的对象是整条 \(\tau\)，而不是单轮 \(y\)。
- **延迟奖励（delayed reward）**：奖励不在每一轮生成后立即给出，而是**整条轨迹结束后一次性结算**。梯度也在整轮结束后才回传。

> 一句话总结：本讲 = u7-l4 的 GRPO/CISPO 损失 + u7-l2 的 rollout 引擎 + 「多轮工具交互」与「延迟可验证奖励」这两层新设定。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `trainer/train_agent.py` | Agentic RL 的**全部**核心逻辑（工具定义、rollout、奖励、训练循环） | 5 个最小模块都在这里 |
| `dataset/lm_dataset.py` | 数据集类 | `AgentRLDataset`、`parse_conversations` |
| `model/tokenizer_config.json` | 分词器与 chat_template | `<tool_call>` / `<tool_response>` / `<think>` 标记与 jinja 渲染 |
| `trainer/rollout_engine.py` | 训推分离的采样引擎（u7-l2 详解） | 本讲只把它当「黑盒调用」：`rollout(...)` 与 `update_policy(...)` |
| `trainer/trainer_utils.py` | 训练公共工具 | `LMForRewardModel.get_score`（u7-l3 详解） |

本讲涉及的关键函数（均在 `trainer/train_agent.py`）：

| 函数 | 行号 | 职责 |
| --- | --- | --- |
| `rep_penalty` | L34 | 文本 n-gram 重复惩罚 |
| `parse_tool_calls` | L77 | 用正则从文本里抠出 `<tool_call>` JSON |
| `execute_tool` | L84 | 执行工具，带 `SIGALRM` 超时保护 |
| `rollout_single` | L98 | 单条样本的多轮 rollout（本讲核心） |
| `rollout_batch` | L159 | 把 `rollout_single` 扩到 batch × num_gen |
| `validate_gt_in_text` | L183 | 校验答案文本是否命中 gt |
| `calculate_rewards` | L188 | 延迟整轮奖励计算（本讲核心） |
| `rl_train_epoch` | L242 | GRPO/CISPO 更新主循环（本讲核心） |

---

## 4. 核心概念与源码讲解

### 4.1 数据与轨迹：AgentRLDataset 与 gt 校验目标

#### 4.1.1 概念说明

在普通 SFT / RLAIF 里，一条样本是「问 → 答」。而 Agentic RL 的样本多了两样东西：

1. **工具集合 \(\mathcal{T}\)**：挂在 `system` 消息的 `tools` 字段上，告诉模型「你可以调用哪些函数」。
2. **gt（ground truth）**：一个**可验证**的最终目标列表，例如数学题的正确答案 `["9472"]`。它只在「算奖励」时用，**绝不**喂给模型。

于是样本变成了三元组 \((x,\mathcal{T},gt)\)，而模型要优化的不再是一个回答 \(y\)，而是一条多轮轨迹 \(\tau\)。`AgentRLDataset` 的职责非常薄：它**不分词、不补全**，只把原始 jsonl 读进来，整理成 `{messages, tools, gt}` 交给后续 rollout。

#### 4.1.2 核心流程

`AgentRLDataset.__getitem__` 做三件事：

1. 读一条样本的 `conversations` 与 `gt`。
2. 调 `parse_conversations`：从 `system` 消息里抠出 `tools` 字段（json 字符串要 `json.loads`），并把消息列表**去掉最后一条**（`messages[:-1]`）。
3. 返回 `{'messages': ..., 'tools': ..., 'gt': ...}`。

> **关键点：为什么去掉最后一条消息？** `agent_rl.jsonl` 的最后一条通常是「黄金 assistant 回答」（数据制作时采样自更大的模型）。但 Agentic RL 要让**当前策略**自己生成轨迹，所以黄金答案必须被丢弃——它只用来在数据制作阶段定义 `gt`，训练时不再可见。这与 u2-l2 中 `RLAIFDataset` 丢弃最后一轮、把答案留空的「懒」思路一脉相承。

#### 4.1.3 源码精读

`AgentRLDataset` 用最朴素的方式逐行读 jsonl（不走 `load_dataset`），因为它不需要任何预处理：

[dataset/lm_dataset.py:L226-L252](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py#L226-L252) — `AgentRLDataset`：逐行 `json.loads` 缓存到 `self.samples`；`__getitem__` 直接返回 messages+tools+gt 三件套，**不做分词**。

`parse_conversations` 抽取 tools 并丢弃最后一条消息：

[dataset/lm_dataset.py:L239-L247](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py#L239-L247) — 抠出 `system.tools`，返回 `messages[:-1], tools`。

数据格式（参考 README 的 Tool Calling 示例，agent_rl 在此基础上**额外加 `gt` 字段**）形如：

```jsonl
{
  "conversations": [
    {"role": "system", "content": "# Tools ...", "tools": "[{...calculate_math...}]"},
    {"role": "user", "content": "帮我算一下 256 乘以 37 等于多少"},
    {"role": "assistant", "content": "256 乘以 37 等于 9472。"}
  ],
  "gt": ["9472"]
}
```

> 提醒：`agent_rl.jsonl` / `agent_rl_math.jsonl` 在仓库里是占位文件（需自行下载，README 记载为 86MB / 18MB），`git clone` 后这两个文件为空。下面的实践若需真实数据，请先按 README 指引获取。

#### 4.1.4 代码实践

**目标**：验证 `AgentRLDataset` 确实丢弃了最后一条消息、并正确抽取了 tools。

**步骤**：

1. 在项目根目录写一段最小脚本（示例代码，非项目原有）：

```python
# 例：检查 AgentRLDataset 的返回结构（示例代码）
import sys, json
sys.path.append('.')
from transformers import AutoTokenizer
from dataset.lm_dataset import AgentRLDataset

# 构造一条临时 jsonl（如果你有真实 agent_rl.jsonl，直接换路径）
sample = {
    "conversations": [
        {"role": "system", "content": "# Tools", "tools": '[{"type":"function","function":{"name":"calculate_math"}}]'},
        {"role": "user", "content": "256 * 37 = ?"},
        {"role": "assistant", "content": "等于 9472。"}  # 黄金答案，应被丢弃
    ],
    "gt": ["9472"]
}
open('/tmp/mini_agent.jsonl', 'w', encoding='utf-8').write(json.dumps(sample, ensure_ascii=False))

tok = AutoTokenizer.from_pretrained('./model', trust_remote_code=True)
ds = AgentRLDataset('/tmp/mini_agent.jsonl', tok)
item = ds[0]
print('消息数:', len(item['messages']))      # 预期 2（system+user），最后一条 assistant 被丢
print('最后一条角色:', item['messages'][-1]['role'])  # 预期 user
print('tools 数:', len(item['tools']))        # 预期 1
print('gt:', item['gt'])                      # 预期 ['9472']
```

2. **需要观察的现象**：`messages` 长度为 2（不包含黄金 assistant 回答）；`tools` 被成功从字符串解析成 list；`gt` 原样保留。

3. **预期结果**：模型侧永远看不到「等于 9472」，但 `gt` 里保留了 `["9472"]` 供奖励函数校验。

> 若手头没有可用的 tokenizer 权重目录，此脚本需待本地验证；逻辑层面可仅阅读源码确认 `messages[:-1]` 的存在。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `parse_conversations` 里的 `messages[:-1]` 改成 `messages`（不丢最后一条），会发生什么？

**答案**：黄金 assistant 回答会进入 `messages`，rollout 时它就成了上文的一部分。模型相当于「看到了答案再续写」，轨迹与 gt 高度耦合，RL 失去意义（退化为有泄漏的 SFT）。所以这一刀切是必须的。

**练习 2**：`gt` 为什么必须是 list（如 `["9472"]`）而不是单个字符串？

**答案**：因为 `calculate_rewards` 里用 `len(gt)` 做归一化（`2.5 * len(verified) / len(gt)`），并允许**多个可接受答案**（如 `["9472", "9,472"]`）。list 形式天然支持部分命中给部分分。

---

### 4.2 工具解析与执行：parse_tool_calls / execute_tool

#### 4.2.1 概念说明

模型生成的是**自然语言文本**，但它被期望在其中嵌入结构化的 `<tool_call>{...}</tool_call>` 片段。要执行工具，必须先把这段 JSON 从文本里「抠」出来，再去「调度」对应的函数。

MiniMind 在 `train_agent.py` 里内置了一个**沙盒工具世界**：6 个模拟工具（计算、单位换算、天气、时间、汇率、翻译），各自有写死的 `MOCK_RESULTS` 模拟返回值、`CHECK_ARGS` 参数校验规则。这样无需联网、无需外部依赖就能跑通整条 Agentic RL，体现了项目「大道至简、可复现」的风格。

两个关键设计：

- **容错的解析**：模型生成的 JSON 可能残缺，`parse_tool_calls` 用 `try/except` 跳过解析失败的片段，**绝不因一个坏 call 崩掉整条 rollout**。
- **超时保护**：`calculate_math` 内部用 `eval`（注意已用 `{"__builtins__": {}}` 沙盒化），为防止恶意/死循环表达式（如 `9**9**9`）卡住训练，`execute_tool` 用 `SIGALRM` 设了 **1 秒**硬超时。

#### 4.2.2 核心流程

```
文本 new_text
   │
   ▼
parse_tool_calls：re.findall(r'<tool_call>(.*?)</tool_call>', ...)
   │   逐段 json.loads，失败则跳过
   ▼
得到 calls = [{"name": ..., "arguments": ...}, ...]
   │
   ▼  对每个 call
execute_tool(name, args):
   ├─ MOCK_RESULTS.get(name)  → 找不到工具返回 None
   ├─ signal.alarm(1)         → 1 秒后抛 TimeoutError
   ├─ fn(args)                → 执行模拟函数
   └─ signal.alarm(0)         → finally 里取消闹钟
```

#### 4.2.3 源码精读

工具定义与模拟执行结果（注意 `calculate_math` 的 `eval` 沙盒）：

[trainer/train_agent.py:L40-L47](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_agent.py#L40-L47) — `TOOLS`：6 个工具的 OpenAI 风格函数签名，定义了「工具合法名集合」。

[trainer/train_agent.py:L57-L74](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_agent.py#L57-L74) — `MOCK_RESULTS`（模拟返回值的 lambda 字典）与 `CHECK_ARGS`（每个工具的参数合法性校验 lambda）。

`parse_tool_calls`：用正则 + `json.loads` 容错抠取：

[trainer/train_agent.py:L77-L82](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_agent.py#L77-L82) — 对每个 `<tool_call>...</tool_call>` 片段尝试 `json.loads`，失败则静默跳过，返回解析成功的 call 列表。

`execute_tool`：带 `SIGALRM` 超时的执行器：

[trainer/train_agent.py:L84-L95](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_agent.py#L84-L95) — `signal.alarm(1)` 设 1 秒闹钟，超时抛 `TimeoutError` 被外层 `except` 吞掉返回 `None`；`finally` 里 `signal.alarm(0)` 取消闹钟（即便正常返回也要取消，否则闹钟会误伤后续逻辑）。

> 提醒：`SIGALRM` 是 **Unix 专用**信号，Windows 上 `signal.alarm` 不存在，这也是 train_agent.py 顶部特意 `import datasets` 解决 Windows DLL 冲突（issue #771）之外的另一个跨平台注意点。

#### 4.2.4 代码实践

**目标**：亲手验证 `parse_tool_calls` 的容错性与 `execute_tool` 的超时保护。

**步骤**：

```python
# 例：工具解析与执行（示例代码）
import sys
sys.path.append('.')
from trainer.train_agent import parse_tool_calls, execute_tool

# ① 正常 + 残缺 JSON 混合
text = ('<tool_call>{"name": "calculate_math", "arguments": {"expression": "256*37"}}</tool_call>'
        '<tool_call>{坏的 json}</tool_call>')
calls = parse_tool_calls(text)
print('解析到的 call 数:', len(calls))  # 预期 1（坏的被跳过）
print('执行结果:', execute_tool(calls[0]['name'], calls[0]['arguments']))  # 预期 {'result': '9472'}

# ② 超时保护：构造一个超大的幂运算
big = parse_tool_calls('<tool_call>{"name":"calculate_math","arguments":{"expression":"9**9**9"}}</tool_call>')
import time
t0 = time.time()
r = execute_tool(big[0]['name'], big[0]['arguments'])
print('耗时 %.2fs, 结果:' % (time.time() - t0), r)  # 预期约 1s 后返回 None
```

**需要观察的现象**：

- 残缺 JSON 不影响合法 call 的解析（容错）。
- `9**9**9` 这种天文数字运算不会卡死，约 1 秒后返回 `None`（超时被吞）。

**预期结果**：第一段输出 `{'result': '9472'}`；第二段耗时略大于 1 秒、返回 `None`。

> 待本地验证：超时的精确耗时取决于机器负载与 `eval` 抢占时机；逻辑上一定是「1 秒闹钟触发 → 返回 None」。

#### 4.2.5 小练习与答案

**练习 1**：`execute_tool` 找不到工具名时返回 `None`，调用方（`rollout_single`）会怎么处理这个 `None`？

**答案**：见 [train_agent.py:L143](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_agent.py#L143)，`result_str = (json.dumps(result) if result else '{"error": "tool not found"}')[:2048]`。`None` 会被渲染成 `{"error": "tool not found"}` 拼回上下文，模型会「看到」自己调了一个不存在的工具，从而在奖励阶段被扣分（工具不合法）。

**练习 2**：为什么 `signal.alarm(0)` 必须放在 `finally` 而不是 `try` 末尾？

**答案**：若 `fn(args)` 抛出异常（含 `TimeoutError`），`try` 末尾的代码不会执行，闹钟会残留并在 1 秒后误触发、杀死后续主流程。`finally` 保证无论成功、异常还是超时都取消闹钟。

---

### 4.3 多轮 Rollout：生成 → 执行 → 拼回 → 续写

#### 4.3.1 概念说明

`rollout_single` 是本讲**最核心**的函数。它把一个 prompt 展开成一条完整的多轮轨迹。与 u7-l4 GRPO「问一次、答一次」不同，这里是一个循环：每轮模型可能产出 `tool_call`，执行后把 observation 拼回，再让模型续写，直到：

- 模型某一轮**没有**生成任何 `tool_call`（说明它给出了最终自然语言答案）→ `break`；
- 或达到 `max_turns`（默认 3）→ 标记 `unfinished = True`（要扣分）。

整条 rollout 过程中最精妙的设计是 **`response_mask`**：它标记序列里每一个 token「是否由策略模型生成」。因为多轮轨迹里混杂了两类 token：

- **策略 token**（mask=1）：模型自己生成的回答、tool_call 文本——这些要参与 loss，是 RL 真正要优化的动作。
- **环境 token**（mask=0）：工具返回的 observation、以及下一轮的 `<|im_start|>assistant\n<think>...` 前缀——这些是「给定」的上下文，模型不需要为它们负责，不应该算 loss。

#### 4.3.2 核心流程

```
rollout_single(messages, tools, max_turns=3):
  prompt_ids = None; response_ids/mask/old_logps = []
  open_thinking = random() < thinking_ratio   # 整条 rollout 用同一个思考开关
  for turn in range(max_turns):
      context = apply_chat_template(messages, add_generation_prompt=True, tools, open_thinking)
      context_ids = tokenize(context)
      if prompt_ids is None: prompt_ids = context_ids      # 只在首轮冻结 prompt 边界
      rollout_result = rollout_engine.rollout(prompt=context, ...)   # u7-l2 的引擎
      new_ids, new_logps = 过滤掉 pad/eos 的 token
      ── 这些是策略 token ──
      response_ids.extend(new_ids);  mask.extend([1]*len);  old_logps.extend(new_logps)
      calls = parse_tool_calls(new_text)
      if not calls: break                                   # 给出最终答案，结束
      if turn == max_turns-1: unfinished = True             # 用满轮数还没收尾
      messages.append(assistant: new_text)
      for call in calls:
          result = execute_tool(call.name, call.arguments)
          messages.append(tool: result_str)                 # 把 observation 加入对话
      # 重新渲染（含工具结果），add_generation_prompt 视 unfinished 决定
      observe_ids = tokenize(apply_chat_template(messages, ...))
      obs_delta = observe_ids[len(prompt_ids)+len(response_ids):]   # 新增的环境 token
      ── 这些是环境 token ──
      response_ids.extend(obs_delta);  mask.extend([0]*len);  old_logps.extend([0.0]*len)
  return final_output, final_context, prompt_ids, response_ids, response_mask, response_old_logps, all_outputs, unfinished
```

关键不变量：`len(response_ids) == len(response_mask) == len(response_old_logps)`，三者逐 token 对齐。

#### 4.3.3 源码精读

首轮冻结 prompt 边界、调用引擎采样：

[trainer/train_agent.py:L107-L131](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_agent.py#L107-L131) — 每轮重新渲染 `context`（因为 messages 在变）；首轮把 `context_ids` 存为 `prompt_ids`；调 `rollout_engine.rollout` 得到 `completion_ids` 与 `per_token_logps`；过滤掉 pad/eos 后，把策略 token 以 **mask=1** 追加。

判断是否还有 tool_call，没有则提前结束；把 assistant 文本与 tool 结果追加进 messages：

[trainer/train_agent.py:L132-L144](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_agent.py#L132-L144) — `unfinished = turn == max_turns - 1` 标记「用满轮数仍未给出无工具的最终答案」；`execute_tool` 的结果最多取 2048 字符（`[:2048]`），防止天文数字结果撑爆 tokenizer。

重新渲染并把**环境增量**以 mask=0 追加（本段是最精妙之处）：

[trainer/train_agent.py:L146-L153](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_agent.py#L146-L153) — `obs_delta = observe_ids[current_len:]` 取「相比 prompt+已有 response 多出来的 token」，即工具 observation 与下一轮 assistant 前缀；这些 token 的 mask 与 old_logps 全部填 0，表示「不训练、概率不作数」。

`rollout_batch` 把单条扩到 batch × num_generations（GRPO 需要同一 prompt 采多条）：

[trainer/train_agent.py:L159-L180](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_agent.py#L159-L180) — 对每个 prompt 复制 `num_gen` 份独立 rollout（`msgs_copy = [dict(m) for m in messages]`，深拷贝防止污染），把所有结果按 batch 维度拍平返回。

> 注意 `open_thinking = random.random() < thinking_ratio`（[L106](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_agent.py#L106)）在循环外只抽一次：整条多轮轨迹共用同一个思考开关，避免「上一轮思考、下一轮直答」的上下文不一致。默认 `thinking_ratio=0.1`（见 main 的 args）。

#### 4.3.4 代码实践

**目标**：用纸笔（或脚本）追踪一条「问 → tool_call → observation → 答」的 2 轮轨迹，画出 `response_mask` 的形状。

**步骤**：

1. 假设 prompt 渲染后是 30 个 token（`prompt_ids` 长度 30）。
2. 第 1 轮模型生成 12 个 token 的 `<tool_call>{...}</tool_call>`（无 eos）→ mask 追加 12 个 `1`。
3. 执行工具，重新渲染后总长度变成 30 + 12 + 8（observation+下一轮前缀）→ `obs_delta` 是 8 个 token → mask 追加 8 个 `0`。
4. 第 2 轮模型生成 15 个 token 的最终答案（含 eos，被过滤）→ mask 追加 15 个 `1`，无 tool_call → `break`。

**需要观察的现象**：最终 `response_mask` = `[1]*12 + [0]*8 + [1]*15`，长度 35。`response_ids` 同长。`prompt_ids` 始终是 30。

**预期结果**：

```
prompt_ids  : 30 个（mask 不计入，pack 时前面补 0）
response    : 12(策略) + 8(环境) + 15(策略) = 35 个
mask        : 111111111111 00000000 111111111111111
              |  tool_call | observation | 最终答案 |
```

> 这正解释了 4.5 节里 `completion_mask` 为何要逐 token 区分——只有 `1` 的位置才进 loss。本实践为源码阅读型，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：为什么 observation token 的 `old_logps` 也填 `0.0` 而不是真实概率？

**答案**：因为环境 token 不是策略生成的，它没有「采样时刻的对数概率」可言；而且它们在 loss 里会被 `completion_mask=0` 完全屏蔽，`old_logps` 取什么值都不影响结果。填 0 只是占位，保证三列表等长对齐。

**练习 2**：如果模型在第 1 轮就生成了最终答案（无 tool_call），`unfinished` 是什么值？走了几次循环？

**答案**：`unfinished` 保持初始值 `False`（[L105](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_agent.py#L105)），循环只执行 1 次就在 `if not calls: break` 处退出。这正是「无工具分支」奖励要处理的情形。

---

### 4.4 延迟整轮奖励：calculate_rewards

#### 4.4.1 概念说明

整条轨迹结束后，`calculate_rewards` 给它**一个标量**奖励。这是「延迟」的本质：不论轨迹经历了几轮，最终只结算一次。README 把它写成：

\[
R(\tau)=R_{\text{answer}}+R_{\text{tool}}+R_{\text{format}}+R_{\text{rm}}-R_{\text{unfinished}}
\]

代码层面，奖励分**两大分支**，取决于「整条轨迹里有没有出现过 tool_call」：

- **无工具分支**（模型直接回答，没用工具）：用**模型打分**（Reward Model）+ 格式分。这是 RLAIF 式的连续奖励，对应 \(R_{\text{rm}}\) 与 \(R_{\text{format}}\)。
- **有工具分支**（模型至少调了一次工具）：用**可验证信号**（gt 命中 + 工具合法性 + 格式闭合）。这是 RLVR 式的规则奖励，对应 \(R_{\text{tool}}\) 与 gt 命中分，**不再调 RM**。

这个分叉非常聪明：**有 gt 就用可验证奖励（便宜、准确），没 gt（或模型没走工具路径）才退回到 RM（贵、有噪声）**。这与 u7-l3「奖励稀疏时优先用连续奖励」的理念一致，但更进了一步——直接用 gt 做硬校验。

#### 4.4.2 核心流程

```
对每条 completion（共 batch*num_gen 条）:
  reward = 0
  turn_answers = 每轮去掉 <think>...</think> 后的内容
  answer = turn_answers[-1]                          # 最后一轮的实质回答
  tool_calls = 收集所有轮里的 tool_call
  reward -= 0.5 * 标签不闭合数（<tool_call> 与 </tool_call> 数量差）
  ── if 没有 tool_calls ──
  │   reward += 长度分（5~800 字符 +0.5，否则 -0.5）
  │   if 有 </think>:
  │       reward += 思考长度分（20~300 字符 +1，否则 -0.5）
  │       reward += 思考闭合分（恰好 1 个 </think> +0.25，否则 -0.25）
  │   reward += reward_model.get_score(messages, answer)   # RM 分
  │   reward -= rep_penalty(answer)                        # 重复惩罚
  ── else（有 tool_calls）──
  │   valid_call_count = 工具名合法 且 参数通过 CHECK_ARGS 的 call 数
  │   tool_gap = |valid - len(gt)| + max(0, 多余的非法 call)
  │   reward += tool_gap==0 ? 0.5 : -0.5*tool_gap          # 工具对齐分
  │   final_text = 最后一次 </tool_call> 之后的文本（最终结论）
  │   verified = validate_gt_in_text(final_text, gt)       # gt 命中
  │   reward += 2.5 * len(verified) / len(gt)             # gt 分（部分命中部分给）
  │   if unfinished: reward -= 0.5
  │   reward -= rep_penalty(final_text)
  reward = clip(reward, -3.0, +3.0)                        # 总分截断
```

两个易错点先点破：

- **`turn_answers` 会剥掉 `<think>` 内容**（`turn.split('</think>', 1)[-1]`），因为思考过程不算「答案」，只有思考后的正文才参与 gt 校验与 RM 打分。
- **gt 分是部分给分**：`2.5 * len(verified) / len(gt)`，命中一半就给一半分，而不是非 0 即 1。这在小模型上能避免奖励过于稀疏（呼应 u7-l3）。

#### 4.4.3 源码精读

`validate_gt_in_text`：用正则抽取文本里的所有数字，与 gt 做**字符串包含 + 数值近似（1e-6）**双重匹配：

[trainer/train_agent.py:L183-L186](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_agent.py#L183-L186) — 同时支持文本型 gt（`s.lower() in text.lower()`）与数值型 gt（`abs(float(g) - n) < 1e-6`），返回命中的 gt 集合。

`calculate_rewards` 的「无工具分支」（长度/思考/RM/重复惩罚）：

[trainer/train_agent.py:L203-L218](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_agent.py#L203-L218) — 只有这一分支会调 `reward_model.get_score`；各类小分累加后用 `max(min(reward, 3.0), -3.0)` 截断到 \([-3,3]\)。

`calculate_rewards` 的「有工具分支」（工具对齐 / gt 校验 / 未完成扣分）：

[trainer/train_agent.py:L220-L238](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_agent.py#L220-L238) — `valid_call_count` 同时检查「名字在合法工具集」与「参数通过 `CHECK_ARGS`」；`tool_gap` 既惩罚「该用几个工具却用了几个」的不符，也惩罚「多余/非法的 call」；gt 命中给大头分 `2.5 * len(verified)/len(gt)`；`unfinished` 扣 0.5。

`rep_penalty`：基于 3-gram 重复率的惩罚，封顶 0.5：

[trainer/train_agent.py:L34-L37](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_agent.py#L34-L37) — `(重复 n-gram数 / 总 n-gram数)` 映射到一个 \([0, 0.5]\) 的惩罚，文本越重复扣得越多。

> RM 打分本身也做了 \([-3,3]\) 截断（见 [trainer_utils.py:L177](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/trainer_utils.py#L177)），加上总分再截断一次，保证奖励尺度稳定，利于 GRPO 的组内归一化。

#### 4.4.4 代码实践

**目标**：手工构造两种 completion，验证「无工具」与「有工具」两分支的奖励差异。

**步骤**：

```python
# 例：手工算奖励（示例代码）
import sys; sys.path.append('.')
from trainer.train_agent import calculate_rewards

# prompts 这里只在「无工具分支 + RM」时被解析成 messages，本例不开 RM 故可传空
prompts = ['']

# ① 无工具、长度合适、带闭合 think
comp_no_tool = '<think>\n算一下\n</think>\n\n256 乘以 37 等于 9472。'
r1 = calculate_rewards(prompts, [comp_no_tool], gt_batch=[[]], tools_batch=[None],
                       num_gen=1, reward_model=None)
print('无工具奖励:', r1.tolist())   # 预期：长度+0.5, 思考长度+1, 闭合+0.25, 减 rep → 约 +1.7

# ② 有工具、命中 gt
comp_tool = ('<tool_call>{"name":"calculate_math","arguments":{"expression":"256*37"}}</tool_call>'
             '结果是 9472。')
r2 = calculate_rewards(prompts, [comp_tool], gt_batch=[['9472']], tools_batch=[None],
                       num_gen=1, reward_model=None)
print('有工具奖励:', r2.tolist())   # 预期：tool_gap 非法(工具集 None)扣分, 但 gt 命中 +2.5
```

> 注意：本例为了不依赖真实 RM，传 `reward_model=None`、`tools_batch=[None]`（无合法工具集，故 `valid_call_count` 恒为 0）。真实训练里 `tools_batch` 来自数据、`reward_model` 来自 InternLM2-Reward。

**需要观察的现象**：

- 无工具样本拿到的是「格式 + 长度」类小分（数量级 ~1）。
- 有工具样本即便工具不合法（本例工具集为空），只要 `final_text` 里出现 `9472`，gt 命中分 `+2.5` 就会显著拉高奖励。

**预期结果**：两个奖励值都在 \([-3,3]\) 内；有工具样本因命中 gt 明显更高。具体数值待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么「有工具分支」里不再调用 Reward Model？

**答案**：因为有了 gt 这个**可验证、精确**的信号，就没必要再用有噪声的 RM。RM 主要用来兜底「没有 gt、模型也没走工具」的纯对话情形。这也呼应 README 所说 Agent 任务更偏 environment-based reward。

**练习 2**：`tool_gap = abs(valid_call_count - len(gt)) + max(0, len(tool_calls) - valid_call_count)` 里第二项惩罚的是什么？

**答案**：惩罚「多余的非法工具调用」。`len(tool_calls) - valid_call_count` 是非法 call 数（名字不对或参数不全）。第一项惩罚「调用数量与 gt 期望不符」，第二项惩罚「乱调工具」。两者相加，鼓励模型**精准地调用恰好够用的合法工具**。

---

### 4.5 GRPO/CISPO 更新：rl_train_epoch

#### 4.5.1 概念说明

拿到延迟奖励后，剩下的更新逻辑**与 u7-l4 的 GRPO/CISPO 完全相同**。`rl_train_epoch` 要做的是：

1. **采样**：对 batch 里每个 prompt 采 `num_generations` 条轨迹（`rollout_batch`）。
2. **打包**：把每条轨迹的 `prompt_ids + response_ids` 拼成训练序列，带上 mask 与 old_logps；超长则**从左截断**。
3. **回算当前 logps**：用最新策略模型前向，取出位移后的逐 token 对数概率。
4. **回算参考 logps**：用冻结的 ref 模型算 token 级 KL。
5. **构造 completion_mask**：把 rollout 的 mask 位移对齐，并在**首个 EOS 之后清零**。
6. **算优势**：GRPO 式组内归一化 \(A=(R-\mu)/(\sigma+\epsilon)\)。
7. **算损失**：按 `loss_type` 走 cispo 或 grpo 分支，与 u7-l4 一模一样。
8. **反传 + 更新 + 同步权重**：每 `accumulation_steps` 步更新一次优化器，并在 save_interval 把新权重 `update_policy` 同步回 rollout 引擎。

#### 4.5.2 核心流程

```
for step, batch in loader:
    # ① 采样（no_grad）
    completions, ..., turn_outputs, unfinished = rollout_batch(...)

    # ② 打包（左截断到 max_total_len）
    ids = prompt_ids + response_ids
    mask = [0]*len(prompt) + response_mask
    old_logps = [0]*(len(prompt)-1) + response_old_logps
    if len(ids) > max_total_len: 从右侧保留最近的 max_total_len

    # ③ 当前策略 logps（位移：logits[:-1] 对 input_ids[1:]）
    res = model(input_ids, attention_mask)
    per_token_logps = log_softmax(res.logits[:,:-1,:]).gather(input_ids[:,1:])

    # ④ 参考 logps（no_grad）
    ref_per_token_logps = compute_per_token_logps(ref_model, ...)

    # ⑤ completion_mask：位移 + 首个 EOS 后清零
    completion_mask = full_response_masks[:,1:]
    completion_mask = 在每行第一个 EOS 之后置 0
    valid_rows = completion_mask.sum(1) > 0

    # ⑥ 优势（GRPO 组内归一化）
    advantages = (rewards - mean) / (std + 1e-4)

    # ⑦ 损失（cispo / grpo 二选一，同 u7-l4）
    loss = -(策略项 × 优势 − β × KL)  逐 token，再按 completion_mask 加权平均
    loss.backward(); 每 N 步 optimizer.step()

    # ⑧ 同步权重回 rollout 引擎
    if save_interval: rollout_engine.update_policy(model)
```

#### 4.5.3 源码精读

采样 + 打包 + 左截断（多轮轨迹容易超长，保留最近的工具结果最重要）：

[trainer/train_agent.py:L250-L271](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_agent.py#L250-L271) — `rollout_batch` 用 `torch.no_grad()`；打包时 `old_logps = [0.0] * max(len(p)-1, 0) + old_lp` 用来对齐位移（预测位置比输入少 1）；超长取 `[-max_total_len:]`，且 `old_logps` 同步取 `[-(len(ids)-1):]`。

当前策略前向 + 回算 logps + ref 模型 logps：

[trainer/train_agent.py:L275-L283](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_agent.py#L275-L283) — `model_unwrapped` 先剥 DDP 外壳；`aux_loss` 仅在 MoE 时取（Dense 取 0）；ref 模型在 `no_grad` 下用 `compute_per_token_logps`（u7-l2 详解）算逐 token 对数概率。

completion_mask 的位移 + 首个 EOS 截断（这段是工程要点）：

[trainer/train_agent.py:L285-L293](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_agent.py#L285-L293) — `completion_mask` 由 rollout 的 `full_response_masks` 位移 1 位得到（对齐「预测下一个 token」的位置）；`is_eos` 找到每行首个 EOS，`pos <= eos_idx` 把 EOS 之后的位置清零，避免训练「模型已经停了之后」的 padding/续写 token；`valid_rows` 跳过完全没有可训练 token 的轨迹。

GRPO 组内归一化优势（同 u7-l4）：

[trainer/train_agent.py:L314-L317](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_agent.py#L314-L317) — `grouped_rewards.view(-1, num_generations)` 按同一 prompt 的多条轨迹分组；`std` 用 `unbiased=False`；`\epsilon=1e-4` 防止退化组除零。

损失（cispo / grpo 分支，与 u7-l4 逐行一致）：

[trainer/train_agent.py:L319-L332](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_agent.py#L319-L332) — `per_token_kl = exp(kl) - kl - 1`（k3 估计，无偏且恒非负）；`ratio = exp(logps - old_logps)`；cispo 用 `clamp(ratio, max=epsilon_high).detach() * A * logps`（梯度走 logπ 不走被夹的 ratio）；grpo 用经典的 `min(r·A, clip(r)·A)`；最终用 `completion_mask` 逐 token 加权、按行求均值后再对有效行求均值。`valid_rows.any() else per_token_loss.sum()*0.0` 是 DDP 兼容兜底——即便没有有效行也要保留前向-反向计算图，否则 DDP 的梯度同步会挂起（呼应最近的 `[fix] ddp exit hang` 提交）。

保存权重 + 把新权重同步回 rollout 引擎：

[trainer/train_agent.py:L351-L364](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_agent.py#L351-L364) — 主进程保存推理权重 `agent_{dim}{_moe?}.pth`（half + cpu）与续训 checkpoint；随后 `rollout_engine.update_policy(model)` 把新策略同步给采样侧（torch 引擎是换引用、sglang 引擎是写盘 + HTTP 通知重载，见 u7-l2）。

> 学习率默认 `3e-7`（见 main 的 args，[L380](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_agent.py#L380)），与 GRPO/PPO 一样远小于 SFT 的 1e-5，因为 RL 阶段只做微调、不能破坏 SFT 已有能力。

#### 4.5.4 代码实践

**目标**：阅读 `rl_train_epoch`，对照 u7-l4 画出「与 GRPO 相同 / 与 GRPO 不同」两张表。

**步骤**：

1. 打开 [trainer/train_agent.py:L242-L371](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_agent.py#L242-L371) 与 u7-l4 的 `grpo_train_epoch`。
2. 逐段比对，填写下表（参考答案见下方）：

| 环节 | u7-l4 GRPO | 本讲 Agentic RL | 是否相同 |
| --- | --- | --- | --- |
| 采样 | 单轮 rollout | `rollout_batch` 多轮 + mask 区分策略/环境 token | ❌ 不同 |
| 奖励 | `calculate_rewards`（长度/think/重复/RM） | `calculate_rewards`（多一套 gt 校验 + 工具对齐） | ❌ 不同 |
| 优势计算 | `(r-μ)/(σ+ε)` | 同左 | ✅ 相同 |
| ratio / KL | `exp(logps-old)` / k3 估计 | 同左 | ✅ 相同 |
| cispo/grpo 损失分支 | `loss_type` 二选一 | 同左 | ✅ 相同 |
| completion_mask | 单轮首 EOS 截断 | 多轮 mask + 首 EOS 截断 + 左截断 | 形似但来源不同 |
| 同步 rollout 引擎 | `update_policy` | `update_policy` | ✅ 相同 |

3. **需要观察的现象**：损失数值层面的代码（优势、ratio、KL、loss 分支）**逐行相同**；差异全部集中在「数据怎么来（rollout）」和「奖励怎么算（gt/工具）」两端。

**预期结果**：你能得出结论——**Agentic RL 不是新算法，而是 GRPO/CISPO 在多轮工具轨迹 + 可验证奖励上的应用**。这正是 MiniMind 设计的简洁之处：一套 PO 更新骨架，喂不同的 rollout/reward 即可。

> 本实践为源码阅读型，无需运行。

#### 4.5.5 小练习与答案

**练习 1**：序列超长时为什么要**从左截断**（`ids[-max_total_len:]`）而不是从右？

**答案**：多轮轨迹里，**最近一轮的工具结果与最终答案在序列末尾**，它们对当前奖励与梯度最关键；而最早的 prompt 已经过时。从左截断丢弃的是「最古老的上下文」，保住的是「最相关的近期交互」。

**练习 2**：`valid_rows.any() else per_token_loss.sum() * 0.0` 这行兜底若删掉，在多卡 DDP 下会出什么问题？

**答案**：当某个 rank 的整批样本都没有可训练 token（如全部 unfinished 且无有效 completion）时，`if valid_rows.any()` 为假，直接不构造 loss 就不会反传；但其他 rank 仍在反传，DDP 在 `backward()` 时等待所有 rank 同步梯度，挂在的 rank 永远等不到 → 训练卡死（即提交记录里的 `[fix] ddp exit hang`）。`* 0.0` 保证即使没有有效行也产生一次空反传，维持 DDP 同步闭环。

---

## 5. 综合实践：跑一次 Agentic RL 并评测工具调用成功率

本实践把本讲全部模块串起来：用 `agent_rl_math.jsonl` 训练若干步，再用 `eval_toolcall.py` 对比 `agent` 与 `full_sft` 在数学 ToolUse 任务上的表现。

### 5.1 准备

1. 按 README 获取数据集 `agent_rl_math.jsonl`（18MB，仓库内为占位空文件）放入 `./dataset/`。
2. 确保已有 `full_sft_768.pth`（u5-l2 产物）作为 RL 起点，位于 `./out/`。
3. 确保有 `full_sft` 对应的 Reward Model 权重（默认 `../../internlm2-1_8b-reward`，可按 README 获取；若无，可在脚本里把 `reward_model_path` 指向一个本地 reward 模型目录）。

### 5.2 训练若干步

```bash
cd trainer
# 单卡，跑少量步观察；默认 loss_type=cispo，rollout_engine=torch
python train_agent.py \
    --data_path ../dataset/agent_rl_math.jsonl \
    --from_weight full_sft \
    --save_weight agent \
    --batch_size 2 \
    --num_generations 4 \
    --epochs 1 \
    --save_interval 20 \
    --log_interval 1 \
    --debug_mode
```

**需要观察的日志字段**（来自 [L347](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_agent.py#L347)）：

- `Reward`：整批平均奖励，期望随训练缓慢上升。
- `KL`：策略相对 ref 的 token 级 KL，应保持小量（如 < 0.05），过大说明策略跑偏。
- `GrpStd` / `AdvStd`：组内奖励标准差，**这是判断 RL 是否还能学的关键**（u7-l3）：若 `GrpStd` 趋近 0，说明同 prompt 的多条采样得分几乎一样，优势≈0，梯度消失。
- `AvgLen`：平均回答长度，监控是否退化成短答或无限复读。

开 `--debug_mode` 后，每 `debug_interval` 步会打印每条轨迹的完整 context、completion、reward（[L295-L312](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_agent.py#L295-L312)），可肉眼检查「tool_call → tool_response → 最终答案」的结构是否正确。

> 训练耗时与显存：Agentic RL 需同时常驻 policy、ref、reward 三个模型，且多轮 rollout 会拉长序列，显存压力大于 GRPO。若 OOM，先调小 `--num_generations` 与 `--max_total_len`。

### 5.3 评测对比

```bash
cd scripts
# ① 评测 RL 后的 agent 权重
python eval_toolcall.py --backend local --weight agent --hidden_size 768
# 选 [0] 自动测试，它会跑 TEST_CASES（含数学、时间、天气、汇率等）

# ② 同样条件下评测 full_sft 基线对比
python eval_toolcall.py --backend local --weight full_sft --hidden_size 768
```

**需要观察的现象**：

- 对「256 乘以 37」「把100公里换算成英里」这类有明确答案的 case，`agent` 权重应当更频繁地**主动调用 `calculate_math` / `unit_converter`**，并在工具返回后给出正确结论；`full_sft` 可能直接「心算」而算错，或调用工具的格式不稳定。
- 统计两种权重在 TEST_CASES 上的「工具调用 + 最终答案正确」成功率。

**预期结果**：`agent` 在带 gt 的可验证任务上成功率应高于 `full_sft`。具体数值待本地验证（取决于 RL 步数与数据规模）。

> 若无 GPU 跑完整训练，可退化为「源码阅读型实践」：阅读 `eval_toolcall.py` 的 `run_case`（[scripts/eval_toolcall.py:L177-L199](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/eval_toolcall.py#L177-L199)），对比它与 `train_agent.py` 的 `rollout_single` 在「生成 → parse → execute → 拼回」循环上的异同——评测脚本是**贪心/采样单条**、训练脚本是**带温度采样多条 + mask**。

---

## 6. 本讲小结

- **Agentic RL = GRPO/CISPO 损失 + 多轮工具轨迹 + 延迟可验证奖励**。损失更新骨架与 u7-l4 逐行相同，差异全在 rollout 与 reward 两端。
- **数据三元组 \((x,\mathcal{T},gt)\)**：`AgentRLDataset` 丢弃黄金 assistant 回答（`messages[:-1]`），只保留 `gt` 供奖励校验；模型必须自己生成轨迹。
- **多轮 rollout 的核心是 `response_mask`**：策略 token 打 1（参与 loss），环境 observation token 打 0（只作上下文），让模型只为自己的动作负责。
- **工具调用靠正则 + JSON 解析 + 沙盒执行**：`parse_tool_calls` 容错、`execute_tool` 用 `SIGALRM` 1 秒超时、`eval` 用空 `__builtins__` 沙盒化。
- **延迟奖励分两叉**：无工具走 RM + 格式分（RLAIF 式），有工具走 gt 命中 + 工具合法性（RLVR 式）；总奖励截断到 \([-3,3]\)。
- **工程细节护航**：序列超长从左截断保近期上下文、completion_mask 在首个 EOS 后清零、`valid_rows` 为空时 `*0.0` 兜底防 DDP 挂起、save_interval 用 `update_policy` 同步权重回 rollout 引擎。

---

## 7. 下一步学习建议

1. **跑通 u7 全单元的对比实验**：用同一份 `full_sft` 起点，分别跑 DPO（u7-l1）、GRPO（u7-l4）、PPO（u7-l5）、Agentic RL（本讲），记录 reward/KL/显存曲线，直观体会四种后训练方法的代价与收益差异。
2. **接入 SGLang rollout 引擎**：按 README 用 `--rollout_engine sglang` 重跑本讲训练，对比 torch 引擎与 sglang 引擎在多轮 rollout 下的吞吐差异（u7-l2 详解了 `update_weights_from_disk` 的同步流程）。
3. **扩展工具集与奖励维度**：在 `TOOLS` / `MOCK_RESULTS` / `CHECK_ARGS` 里新增一个工具（如「查股票」），并在 `calculate_rewards` 里加一条对应的奖励规则，体会「环境即奖励」的扩展方式。
4. **进入 u8 部署单元**：训练得到的 `agent_*.pth` 可用 u8-l1 的 `convert_model.py` 转 transformers 格式，或用 u8-l2 的 `serve_openai_api.py` 暴露为带 `tool_calls` 字段的 OpenAI 兼容 API，最终在 u8-l3 的 `web_demo.py` 里做可视化的多轮工具对话。
5. **阅读延伸**：本讲的「整轮延迟奖励」是同步模式（采样完一批再更新）；若感兴趣异步 rollout buffer、更长的多轮编排与长期记忆，可结合 README 第 7 章关于 environment-based reward 的讨论进一步研究。
