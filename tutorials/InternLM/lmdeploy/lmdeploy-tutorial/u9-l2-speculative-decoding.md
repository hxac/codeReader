# 投机解码 spec_decode

## 1. 本讲目标

学完本讲，你应当能够：

- 说清投机解码（speculative decoding）「先草拟、再验证、按概率接受/拒绝」的 draft-verify 流程，以及它为什么能在不改变输出分布的前提下加速推理。
- 读懂 `lmdeploy/pytorch/spec_decode/` 目录下三个核心模块的分工：`spec_agent`（编排器）、`reject_sampler`（验证算法）、`guided_spec_helper`（与结构化解码的结合）。
- 在源码中追踪出一次 decode 步里 `proposal → verify → accept/reject` 的完整数据流，并解释接受/拒绝判定的数学依据。
- 学会通过 CLI 参数 `--speculative-algorithm` 启用 EAGLE / DeepSeek-MTP 等草稿模型。

本讲是 U9 高级特性篇，承接 u4-l2（异步推理循环 EngineLoop）。你需要已经知道 PyTorch 引擎每一步 forward 由 EngineLoop 驱动、prefill/decode 两阶段、以及持续批处理的基本概念。

## 2. 前置知识

### 2.1 为什么需要投机解码

LLM 自回归生成的瓶颈是 **逐 token 解码**：每生成一个 token，就要把整个大模型（target model）跑一遍 forward，而每次 forward 只产出 1 个 token，GPU 算力大量闲置（访存受限，memory-bound）。

投机解码的核心想法是：

1. 用一个 **小而快的草稿模型（draft model / proposer）** 先「猜」出接下来的 N 个 token；
2. 把这 N 个草稿 token **拼到当前序列后面**，让 **大模型（target）一次性 forward 验证这 N+1 个位置**（并行验证，parallel verification）；
3. 用 **拒绝采样（rejection sampling）** 决定接受其中多少个草稿 token：接受一个匹配的前缀，在第一个不匹配处停下并改用一个修正 token，最后再补一个「奖励 token（bonus）」。

关键收益：target 一次 forward 本来就要算，现在一次 forward 可以「顺带」验证 N 个位置，若草稿命中率高，就等于一次 forward 产出了多个 token——把访存开销摊薄到多个 token 上，吞吐随之提升。

关键约束：拒绝采样的设计保证 **最终输出分布与纯 target 采样完全一致**，即「加速但不改结果」。本讲 4.3 会用数学说明这一点。

### 2.2 几个术语

| 术语 | 含义 |
|------|------|
| target model（目标模型） | 真正负责生成的大模型，如 Qwen2.5-7B |
| draft model / proposer（草稿模型） | 轻量预测头，产出候选 token。lmdeploy 支持 `eagle` / `eagle3` / `deepseek_mtp` / `qwen3_5_mtp` |
| `num_spec_tokens` | 每步草拟的 token 数（也叫 `num_speculative_tokens`） |
| bonus token（奖励 token） | 全部草稿都被接受后，target 在最后一个位置额外产出的「白送」token |
| rejection sampling（拒绝采样） | 依据 target 与 draft 的概率比决定接受/拒绝的算法 |
| guided decoding（结构化解码） | 用语法（JSON/正则）约束输出，lmdeploy 用 xgrammar 实现 |

### 2.3 承接 u4-l2

u4-l2 讲到 EngineLoop 的 `main_loop` 单步完成「取输入→forward→回收」。本讲的投机解码就嵌在 **每一步 forward 内部**：在 ModelAgent（引擎面负责跑模型的对象）里挂了一个 `spec_agent`，它在 target forward 之后做拒绝采样、在采样之后跑草稿模型为下一步备好草稿。所以本讲是 EngineLoop「单步」内部的展开。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| [spec_decode/\_\_init\_\_.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/__init__.py) | `build_spec_agent()` 工厂：开启时返回 `SpecModelAgent`，关闭时返回空实现 `BaseSpecModelAgent` |
| [spec_decode/base.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/base.py) | `BaseSpecModelAgent` 空实现（关闭投机解码时的 no-op 桩） |
| [spec_decode/spec_agent.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/spec_agent.py) | **核心编排器 `SpecModelAgent`**：草稿前向 + 拒绝采样 |
| [spec_decode/reject_sampler.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/reject_sampler.py) | **验证算法 `RejectionSampler`** 与 Triton kernel |
| [spec_decode/guided_spec_helper.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/guided_spec_helper.py) | **结构化解码适配器 `GuidedSpecHelper`** |
| [spec_decode/proposers/base.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/proposers/base.py) | `BaseSpecProposer` 草稿模型基类、`SPEC_PROPOSERS` 注册表 |
| [spec_decode/proposers/deepseek_mtp.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/proposers/deepseek_mtp.py) | MTP 草稿实现（eagle/eagle3 的父类） |
| [pytorch/config.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py) | 引擎内部 `SpecDecodeConfig` |
| [messages.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py) | 用户面 `SpeculativeConfig` |
| [cli/utils.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/utils.py) | CLI 参数 `--speculative-algorithm` 等 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：4.1 编排器 `spec_agent`（含 draft-verify 完整数据流）、4.2 验证算法 `reject_sampler`、4.3 结构化解码适配 `guided_spec_helper`。

### 4.1 spec_agent：草稿编排与 draft-verify 主循环

#### 4.1.1 概念说明

`SpecModelAgent` 是投机解码的「总指挥」。它本身不跑任何模型数学，而是协调三件事：

1. **草稿生成（proposal）**：驱动草稿模型（proposer）自回归地生成 N 个候选 token；
2. **目标验证（verify）**：把草稿 token 拼进序列，让 target 一次 forward 验证；
3. **接受/拒绝（accept/reject）**：调用 `rejection_sampler` 判定保留多少个草稿 token、产出 bonus。

它挂在主引擎的 `ModelAgent` 上。当投机解码关闭时，`build_spec_agent()` 返回的是空实现 `BaseSpecModelAgent`——它的所有方法都是 no-op，`is_enabled()` 返回 `False`，于是主引擎走普通单 token 解码路径。这就是「一个开关、两条路径」的设计：

[spec_decode/\_\_init\_\_.py:L7-L39](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/__init__.py#L7-L39) —— 根据是否启用与 TP 拓扑选择 `SpecModelAgent` 或 `BaseSpecModelAgent`。注意 L20 的判断：当主模型 TP>1 但草稿 TP=1 时，只有 `rank % main_tp == 0` 的卡才真正建草稿模型，其余卡空跑（草稿不切分，省显存）。

`BaseSpecModelAgent` 的桩实现见 [base.py:L28-L58](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/base.py#L28-L58)，其中 L53 `self.rejection_sampler = RejectionSampler()` 在基类里就建好了——即使不用，采样器对象也预先存在。

#### 4.1.2 核心流程：proposal → verify → accept/reject

把投机解码的每一步展开，数据流如下（以 decode 阶段、`num_spec_tokens = N` 为例）：

```text
              ┌──────────────── 上一步产出的「最后接受 token」的 hidden state ─────────────────┐
              │                                                                          │
              ▼                                                                          │
   ┌───────────────────────┐   逐 token 自回归 N 次                                       │
   │ 1. 草稿生成 proposal  │ ───────────────────►  draft_token_ids: [N]                   │
   │   proposer.get_outputs│   (每步把上一草稿喂回，复用 target hidden states)             │
   └───────────────────────┘                                                              │
              │                                                                          │
              │  把 N 个草稿 token 拼到当前 token 后                                       │
              ▼                                                                          │
   ┌───────────────────────┐   一次 forward 验证 N+1 个位置                                │
   │ 2. 目标验证 verify    │ ───────────────────►  target_logits: [batch, N+1, vocab]     │
   │   target.forward      │   (主引擎 ModelAgent 完成，本讲只读其产出)                     │
   └───────────────────────┘                                                              │
              │                                                                          │
              ▼                                                                          │
   ┌───────────────────────┐   对比 draft 与 target 分布                                  │
   │ 3. 接受/拒绝 accept   │ ───────────────────►  output_token_ids: [batch, N+1]         │
   │   rejection_sampler   │                        num_rejected_tokens: [batch]          │
   └───────────────────────┘                        next_token_ids (bonus): [batch]       │
              │                                                                          │
              ▼                                                                          │
        把接受前缀 + bonus 追加进序列，进入下一步 ─────────────────────────────────────────┘
```

注意 lmdeploy 把「草稿生成」安排在 **当步采样之后**（为下一步备草稿），形成 1 步预取流水。这在源码里的体现见 4.1.3。

#### 4.1.3 源码精读

**(a) 两个入口方法**

`SpecModelAgent` 对外暴露两个 async 入口，分别对应「验证 + 接受/拒绝」与「草稿生成」：

[spec_agent.py:L588-L592](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/spec_agent.py#L588-L592) —— `async_sampling_logits` 包装 `_rejection_sampling`，做 target logits 的采样与拒绝采样（即流程图的第 3 步）。

[spec_agent.py:L684-L692](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/spec_agent.py#L684-L692) —— `async_model_forward` 先把主模型输入「偏移 1」改造成草稿输入，再调 `_async_model_forward` 跑草稿（即流程图的第 1 步）。

这两个入口由主引擎 `ModelAgent` 在一步内先后调用，顺序见 [engine/model_agent/agent.py:L694-L699](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/model_agent/agent.py#L694-L699)：先 `async_sampling_logits`（验证本步），再 `spec_agent.async_model_forward`（为下一步备草稿）。

**(b) 草稿输入构造：整体左移 1 位**

草稿模型的任务是「预测下一个 token」，所以它的输入要相对 target 整体 **左移一位**：把 target 序列去掉第一个 token，在最末位放上「上一步接受的 token」。这正是 MTP（Multi-Token Prediction）的输入对齐方式：

[spec_agent.py:L265-L272](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/spec_agent.py#L265-L272) 说明：非分块的普通分支里，`input_ids[:, :-1] = input_ids[:, 1:]`（左移），再 `input_ids[:, last_token_indices] = next_token_ids`（末位填上一步接受的 token）。该函数还处理长上下文分块（first/middle/last chunk）的跨块拼接，逻辑较繁琐，初读只需记住「左移 1 位」的主线。

**(c) 草稿自回归循环**

`_async_model_forward` 是草稿生成的主体：第一步基于 target hidden states 出第 1 个草稿，随后 `loop_count = num_spec_tokens - 1` 次把草稿喂回继续生成：

[spec_agent.py:L642-L672](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/spec_agent.py#L642-L672) —— `for loop_idx in range(loop_count)` 反复 `_forward_impl`（草稿 forward）并 `proposer.get_outputs` 取草稿 token，最后 `torch.cat(draft_tokens_li)` 拼成 `[batch, num_spec_tokens]` 的 `output_draft_token_ids`。

每一步草稿是怎么从 hidden states 变成 token 的？看最基础的 MTP proposer：

[proposers/deepseek_mtp.py:L17-L42](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/proposers/deepseek_mtp.py#L17-L42) —— `get_outputs` 取 target 末位 hidden states → `get_logits` → **argmax**（草稿总是贪心取最大）→ 返回 `draft_token_ids`。注意 L35-L40：草稿侧也要应用结构化解码的 bitmask 并 accept token，这部分留给 4.3。

`Eagle` 只是 `DeepseekMTP` 的别名（[proposers/eagle.py:L7-L9](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/proposers/eagle.py#L7-L9)），`Eagle3` 则多了「小词表 → 大词表」的映射（draft vocab 翻译）。

**(d) 草稿模型从哪来：配置链路**

用户面用 `SpeculativeConfig`（[messages.py:L726-L737](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L726-L737)）描述：`method`（算法名）、`model`（草稿权重路径）、`num_speculative_tokens`。引擎内部把它转成 `SpecDecodeConfig`（[config.py:L601-L607](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py#L601-L607)），其中 `from_config`（L609-L659）会用草稿路径单独构造一个 `ModelConfig` 与一份 `CacheConfig`（草稿模型也要 KV cache）。

`SpecModelAgent.__init__` 用 `build_specdecode_proposer(specdecode_config)` 按方法名从注册表 `SPEC_PROPOSERS` 选草稿实现类：

[spec_agent.py:L171-L174](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/spec_agent.py#L171-L174) —— 构造草稿与一个空的 `GuidedSpecHelper`（结构化解码支持，后续由 ModelAgent 注入真实管理器）。

注册与查找见 [proposers/base.py:L165-L172](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/proposers/base.py#L165-L172)（mmengine Registry，按 `method` 名查类）。

#### 4.1.4 代码实践：梳理 draft-verify 数据流

1. **实践目标**：在不跑模型的前提下，仅靠阅读源码画出一次 decode 步里 `proposal → verify → accept/reject` 的调用链，并标出每一步产出/消费的张量。
2. **操作步骤**：
   - 打开 `lmdeploy/pytorch/engine/model_agent/agent.py`，定位 `_step_postprocess_with_output`（约 L694-L699），确认两行调用顺序：先 `async_sampling_logits`、后 `spec_agent.async_model_forward`。
   - 打开 `lmdeploy/pytorch/spec_decode/spec_agent.py`，分别在 `async_sampling_logits`（L588）与 `async_model_forward`（L684）上各打一个断点或加一行 `logger.debug`。
   - 阅读 `_async_model_forward`（L594-L682）的 `for loop_idx in range(loop_count)` 循环，确认草稿是「自回归」生成的。
3. **需要观察的现象**：理论上每步 decode 中，`async_sampling_logits` 会先返回本步的 `output_token_ids`（长度 `N+1`），随后 `async_model_forward` 返回下一步要用的 `output_draft_token_ids`（长度 `N`）。
4. **预期结果**：你应当能画出一张「target forward → rejection sampling → draft forward」三段循环图，并指出 `ARSpecExtraInputs`（[strategies/ar_spec/model_agent.py:L21-L44](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/strategies/ar_spec/model_agent.py#L21-L44)）里的 `target_logits`、`output_draft_token_ids`、`num_rejected_tokens`、`output_token_ids` 分别在流程图的哪一段被写入。
5. 若本地无 GPU 或无草稿权重，以上为「源码阅读型实践」，**待本地验证** 实际日志。

#### 4.1.5 小练习与答案

**练习 1**：为什么主模型 TP>1、草稿 TP=1 时，只有部分 rank 真正建草稿模型？
**答案**：见 [\_\_init\_\_.py:L20-L21](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/__init__.py#L20-L21)。草稿模型很小、不切分（TP=1），若每张卡都建一份会重复占显存；只让 `rank % main_tp == 0` 的「主卡」建草稿，验证后的 token 再广播给其余卡即可。

**练习 2**：草稿模型生成 token 时用 argmax，那它支持 `temperature > 0` 的随机采样吗？
**答案**：草稿侧本身 **只做贪心 argmax**（见 [deepseek_mtp.py:L39](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/proposers/deepseek_mtp.py#L39)）。随机性完全由 target 侧的拒绝采样承担（见 4.3），草稿只负责「提议」。这也是为什么拒绝采样要精心设计以保证最终分布等于 target 分布。

---

### 4.2 reject_sampler：接受/拒绝判定算法

#### 4.2.1 概念说明

`RejectionSampler` 解决的问题是：给定 target 在 N+1 个位置上的 logits、N 个草稿 token、以及（可选的）草稿概率，**决定接受哪些草稿、拒绝后用什么 token 替补、以及是否追加 bonus**。

它实现了论文 [Accelerating Large Language Model Decoding with Speculative Sampling](https://arxiv.org/abs/2211.17192) 的拒绝采样算法。核心区分两种采样模式：

- **贪心（greedy）**：target 用 `top_k=1`（即 argmax）。此时判定退化为「草稿 token 是否等于 target argmax」，接受匹配前缀，首个不匹配处改用 target argmax，全匹配则加 bonus。
- **随机（random）**：target 做概率采样。此时用概率比 `target_prob / draft_prob` 做接受判定，拒绝时从残差分布采样一个「修正 token」。

为什么残差采样能保证分布正确？这是投机解码最精妙的数学：

设草稿分布为 \(p(x)\)（draft）、目标分布为 \(q(x)\)（target）。对一个候选 token \(x\)：

- **接受** 的概率为 \(\min\!\left(1, \dfrac{q(x)}{p(x)}\right)\)，贡献的概率质量是 \(p(x)\cdot\min\!\left(1,\dfrac{q(x)}{p(x)}\right)=\min(p(x),q(x))\)；
- **拒绝并从残差重采样** 的分布正比于 \(\max(0,\,q(x)-p(x))\)，贡献 \(\max(0,\,q(x)-p(x))\)。

两者相加：

\[
\min(p(x),q(x)) + \max(0,\,q(x)-p(x)) = q(x)
\]

即最终输出分布恰好等于 \(q(x)\)。这就是「加速但不改结果」的数学保证。

#### 4.2.2 核心流程

`rejection_sample` 的总流程（[reject_sampler.py:L115-L230](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/reject_sampler.py#L115-L230)）：

1. **判定采样策略**（L146-L154）：用 `sampling_inputs.max_top_k == 1` 判断是否全部贪心；否则按每序列的 `top_k` 区分贪心/随机（同一个 batch 可混跑）。
2. **贪心分支**（L165-L176）：若 batch 中有贪心序列，跑 `rejection_greedy_sample_kernel`；若全是贪心，直接返回。
3. **计算 target 概率**（L179）：`target_probs = target_logits.softmax(...)`。
4. **生成均匀随机数**（L182-L186）与 **Gumbel-max 噪声**（L189-L213）：用于随机接受判定与残差重采样。
5. **随机拒绝分支**（L216-L228）：`rejection_random_sample_kernel`。
6. **抽取结果**（L230）：`_extract_outputs` 统计每序列接受数、被拒数、最后一个 token。

输出张量 `output_token_ids` 形状为 `[batch, num_spec_tokens + 1]`，被拒位置填占位符 `PLACEHOLDER_TOKEN_ID = -1`（[reject_sampler.py:L10](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/reject_sampler.py#L10)）。

#### 4.2.3 源码精读

**(a) 入口与派发**

[reject_sampler.py:L23-L51](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/reject_sampler.py#L23-L51) —— `RejectionSampler.forward` 只是薄包装，转调 `rejection_sample`。注意输入约定：`target_logits` 已经过 `FusedLogitsProcessor` 处理（温度/top-k/top-p 已施加，见 u2-l2）。

**(b) 贪心判定 kernel**

[reject_sampler.py:L233-L274](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/reject_sampler.py#L233-L274) 是一个 Triton kernel，grid 为 `(batch_size,)`，每个 program 处理一个请求。逻辑直白：

```text
rejected = False
for pos in range(num_spec_tokens):
    if not rejected:
        把 target_argmax[pos] 写入输出
        if draft_token[pos] != target_argmax[pos]:
            rejected = True        # 首个不匹配，之后全停
if not rejected:                    # 全部匹配
    把 bonus_token 写入最后一个槽
```

关键点：贪心模式下「接受 = 草稿等于 target argmax」，首个不匹配处之后的位置即便草稿恰好又对了也不接受（因为序列已经分歧）。若全对，则额外写入 bonus。

> 旁注：文件里还有一个纯 PyTorch 版本 `torch_greedy_rejection_sample`（[L55-L94](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/reject_sampler.py#L55-L94)），逻辑等价但向量化，便于读者对照理解 kernel 的语义；实际运行走 Triton kernel。

**(c) 随机判定 kernel**

[reject_sampler.py:L277-L332](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/reject_sampler.py#L277-L332) —— `rejection_random_sample_kernel`，每个位置的核心判定：

```text
draft_prob  = draft_probs[pos, draft_token]   # 草稿给这个 token 的概率
target_prob = target_probs[pos, draft_token]  # target 给这个 token 的概率
uniform     = uniform_probs[pos]              # ~ U[0,1)

if draft_prob > 0 and (target_prob / draft_prob) >= uniform:
    接受 draft_token          # 以概率 min(1, q/p) 接受
else:
    拒绝，改用 recovered_token[pos]   # 从残差分布采样
    rejected = True
```

当 `draft_probs` 为 `None`（草稿不提供概率）时，`draft_prob` 取 1（L310-L313），接受条件退化为 `target_prob >= uniform`——仍能保证正确性，只是接受率略低。

**(d) 残差重采样：Gumbel-max trick**

拒绝时的 `recovered_token` 来自 [reject_sampler.py:L335-L402](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/reject_sampler.py#L335-L402) 的 `sample_recovered_tokens_kernel`。它要从分布 \(\max(0,\,q(x)-p(x))\)（归一化后）采样一个 token，用的是 **Gumbel-max 技巧**：

> 若对每个 \(x\) 计算 \(\log \pi(x) + g_x\)（\(g_x\) 是标准 Gumbel 噪声），取 argmax 得到的样本服从 \(\pi\)。

这里用指数分布的倒数 `inv_q` 作为等价 Gumbel 噪声（L189-L195：`q.exponential_(); inv_q = q.reciprocal()`），然后 `score = prob * inv_q`，argmax 即得样本。其中 `prob = max(target_prob - draft_prob, 0)`（L387）正是残差。L366-L396 用分块（`BLOCK_SIZE=8192`）遍历词表求 argmax，以适配大词表。

**(e) 在 spec_agent 里如何被调用**

回到 [spec_agent.py:L482-L492](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/spec_agent.py#L482-L492)：把 target 的 `processed_logits`（除 bonus 位外的前 N 位）`view` 成 `[batch, num_spec, vocab]`，连同草稿 token `output_draft_token_ids` 与 bonus `next_token_ids` 一起喂给 `self.rejection_sampler`，拿回 `output_token_ids`、`num_rejected_tokens` 与新的 `next_token_ids`。

#### 4.2.4 代码实践：阅读接受/拒绝判定逻辑

1. **实践目标**：用一个手算的 toy 例子，对照源码确认贪心与随机两种判定分别会接受几个草稿 token。
2. **操作步骤**：
   - 构造假设输入：`num_spec_tokens = 3`，草稿 token `[10, 20, 30]`，target argmax `[10, 99, 30]`。手算贪心结果：接受位置 0（10==10），位置 1 不匹配（20≠99）停下并改用 99，位置 2 不再接受，无 bonus。
   - 打开 [reject_sampler.py:L233-L274](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/reject_sampler.py#L233-L274) 的 `rejection_greedy_sample_kernel`，逐步代入，确认输出为 `[10, 99, -1]`（占位符表示该位置不产出），`num_rejected_tokens` 含义见 `_extract_outputs`（L97-L112）：`num_rejected = num_spec_tokens + 1 - num_accepted`。
   - 再读随机分支 [L277-L332](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/reject_sampler.py#L277-L332)，回答：若 `draft_prob=0.5, target_prob=0.4, uniform=0.7`，是否接受？（`0.4/0.5=0.8 >= 0.7`，接受。）
3. **需要观察的现象**：通过手算与源码逐行对照，确认你对接受/拒绝与 bonus 追加时机的理解。
4. **预期结果**：贪心下「首个不匹配即停」、随机下「按 `q/p` 概率接受、拒绝则用残差样本」两条规则你能口述清楚。
5. **待本地验证**：若本地有 GPU 与支持投机解码的模型，可启用 `LMDEPLOY_LOG_LEVEL=DEBUG` 跑一次，对照日志里的 `num_rejected_tokens` 与手算结果。

#### 4.2.5 小练习与答案

**练习 1**：`output_token_ids` 中出现 `-1`（`PLACEHOLDER_TOKEN_ID`）表示什么？下游如何处理？
**答案**：表示该位置被拒绝、不产出有效 token。下游 `ARSpecStoppingCriteria.step`（[strategies/ar_spec/model_agent.py:L113-L137](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/strategies/ar_spec/model_agent.py#L113-L137)）用 `valid_tokens = token_ids > -1` 过滤掉占位符，只把有效 token 追加进序列。

**练习 2**：为什么随机分支要用 Gumbel-max 而不是直接 `torch.multinomial`？
**答案**：Gumbal-max 的 argmax 写法天然适合在 Triton kernel 里 **分块遍历大词表**（见 L366-L396 的 `BLOCK_SIZE` 循环），且与拒绝判定 kernel 解耦——可以先在 `sample_recovered_tokens_kernel` 里为每个 (batch, pos) 预算好「替补 token」，再在 `rejection_random_sample_kernel` 里按需取用，避免在判定 kernel 内再做一次完整词表采样。

---

### 4.3 guided_spec_helper：结构化解码与投机解码的协作

#### 4.3.1 概念说明

结构化解码（guided decoding / constrained decoding）用语法（JSON Schema、正则等）约束模型输出，lmdeploy 用 xgrammar 的 `GrammarMatcher` 实现：每一步在采样前用 bitmask 屏蔽掉非法 token。

把结构化解码 **单独** 用没问题，但与投机解码 **同时** 用会冲突。`GuidedSpecHelper` 的类文档把四点冲突讲得很清楚：

[guided_spec_helper.py:L15-L34](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/guided_spec_helper.py#L15-L34) —— 投机解码需要：① 跨 N+1 个位置 **串行** 地施加 bitmask（普通解码只 1 个位置）；② **fork（复制）** matcher，避免污染原件，因为 target 验证用的是假设性的草稿分支；③ 接受判定 **由拒绝采样驱动** 而非直接 argmax；④ **草稿词表翻译**（Eagle3 用小词表，bitmask 要从 target 词表翻译过去）。

`GuidedSpecHelper` 就是为这四点专门写的适配层。它包装一个 `GuidedDecodingManager`（来自 `engine/guided_process.py`），所有方法在 `guided_manager=None` 或无活动 processor 时都是 **no-op**，所以调用方无需写 `if guided_helper:` 守卫。

#### 4.3.2 核心流程

`GuidedSpecHelper` 的方法按「草稿侧 / target 侧 / 接受侧」三组组织：

| 方法 | 侧 | 作用 |
|------|----|------|
| `prepare_bitmask` + `apply_bitmask` | 草稿侧 | 为草稿 logits 生成并施加语法 bitmask（草稿贪心取 argmax 前用） |
| `accept_draft_tokens` | 草稿侧 | 在 **forked** matcher 上接受草稿 token，推进草稿分支状态 |
| `apply_serial_bitmask` | target 侧 | fork 出 matcher，对 N+1 个位置 **逐个** 施加 bitmask 并用草稿 token 推进 fork（原件不变） |
| `accept_rejection_sampled_tokens` | 接受侧 | 在 **原件** matcher 上接受「拒绝采样后真正被采纳的 token + bonus」 |

关键设计：**target 验证用 fork，最终接受改原件**。因为 target 验证时草稿 token 可能被拒，不能直接推进原件 matcher；只有拒绝采样确定真正接受的 token 后，才把原件 matcher 推进到正确状态。

#### 4.3.3 源码精读

**(a) target 侧串行 bitmask（fork 保护原件）**

[guided_spec_helper.py:L124-L166](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/guided_spec_helper.py#L124-L166) —— `apply_serial_bitmask`：

```text
forked = {idx: proc.fork() for ...}      # L151 复制 matcher，原件不动
for pos in range(num_expand):             # N+1 个位置逐个处理
    用 forked 当前状态填 bitmask
    apply 到 scores_3d[:, pos, :]         # 屏蔽该位置非法 token
    if pos < num_spec_tokens:
        用 draft_token[pos] 推进 forked   # 注意：用草稿 token，不是 argmax
```

注释（L142-L146）特别强调：target 的 logits 是 **以草稿 token 为条件** 的，所以 fork 要用 `draft_token_ids` 推进而非取 argmax。

**(b) 接受侧：按拒绝采样结果推进原件**

[guided_spec_helper.py:L172-L214](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/guided_spec_helper.py#L172-L214) —— `accept_rejection_sampled_tokens` 是「收尾」：拒绝采样已经算出每序列接受了 `num_spec_tokens - num_rejected` 个草稿 token + 1 个 bonus。于是对 **原件** matcher：

```text
for 每个序列 idx:
    n_valid_draft = num_spec_tokens - num_rejected[idx]
    for pos in range(n_valid_draft):
        accept_token(processor, output_token_ids[idx, pos])   # 接受的草稿
    accept_token(processor, next_token_ids[idx])              # bonus
```

这样原件 matcher 就跳过了被拒的假设性分支，停在「真正发生」的状态上，供下一步使用。CPU 化（`.cpu()`）与 `asyncio.to_thread`（L198-L214）是为了把 xgrammar 的 CPU 工作丢到线程池，不阻塞 async 事件循环。

**(c) 在 spec_agent 里的串联**

target 侧的串行 bitmask 由 `_guided_spec_logits_process` 调用（[spec_agent.py:L548-L580](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/spec_agent.py#L548-L580)），它先让 `FusedLogitsProcessor` 处理 logits，再调 `guided_helper.apply_serial_bitmask` 把 grammar 掩码逐位叠加上去。

接受侧则在拒绝采样完成后立即调用（[spec_agent.py:L498-L504](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/spec_agent.py#L498-L504)），把原件 matcher 推进到正确状态。

**(d) 草稿词表翻译（Eagle3）**

Eagle3 用一个 **小词表** 草稿模型，其输出 token 要经 `draft_id_to_target_id` 映射回 target 大词表（见 [eagle3.py:L121-L122](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/proposers/eagle3.py#L121-L122)）。相应地，target 词表的 bitmask 也要 **翻译** 到草稿小词表，才能在草稿 argmax 前施加——这就是 `Eagle3._translate_bitmask`（[eagle3.py:L54-L87](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/proposers/eagle3.py#L54-L87)，逐位搬运 int32 bitmask）。`GuidedSpecHelper.apply_bitmask` 本身设备无关，翻译由各 proposer 自行处理（见类文档 L74-L76）。

#### 4.3.4 代码实践：理解 fork 与原件的分工

1. **实践目标**：搞清「为什么 target 验证要 fork matcher，而最终接受要改原件」。
2. **操作步骤**：
   - 阅读 [guided_spec_helper.py:L124-L166](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/guided_spec_helper.py#L124-L166)（`apply_serial_bitmask`），确认 L151 的 `proc.fork()` 与 L149 的「原件不被修改」。
   - 阅读 [spec_agent.py:L494-L504](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/spec_agent.py#L494-L504) 的注释：「Forked matchers were used during processing, so originals are still at pre-step state.」
   - 假设一个场景：N=3，草稿全错、全部被拒，bonus 被 target 采纳。问：这一步对原件 matcher accept 了多少个草稿 token、几个 bonus？
3. **需要观察的现象**：理解「验证阶段假设性地走完 N 个草稿分支（在 fork 上），但只有拒绝采样认定的真实路径才回写到原件」这一设计。
4. **预期结果**：全拒场景下 `num_rejected=3`，`n_valid_draft = 3-3 = 0`，所以原件上 accept 0 个草稿 + 1 个 bonus（即 [guided_spec_helper.py:L207-L212](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/guided_spec_helper.py#L207-L212) 的 for 循环不执行、只执行最后的 bonus accept）。
5. **待本地验证**：可构造一个 JSON-mode 请求，观察输出始终合法（说明 grammar 与 spec 共同生效）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `GuidedSpecHelper` 的所有方法都在「无 manager / 无 processor」时直接 return？
**答案**：见 [L31-L34](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/guided_spec_helper.py#L31-L34)。这是「空对象模式（null object）」：让 `spec_agent` 在「开了投机解码但没开结构化解码」的常见情况下，无需到处写 `if guided:` 分支，代码更简洁。

**练习 2**：Eagle3 的草稿词表比 target 小，这会给结构化解码带来什么额外工作？
**答案**：grammar matcher 产出的 bitmask 是按 **target 大词表** 编码的，而草稿 logits 在 **小词表** 上。所以必须先把 bitmask 翻译到草稿词表（`_translate_bitmask`，[eagle3.py:L54-L87](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/proposers/eagle3.py#L54-L87)），才能在草稿 argmax 前施加约束；普通 eagle/deepseek_mtp 草稿与 target 同词表，无需此步。

## 5. 综合实践

**任务**：把本讲三个模块串起来，用 CLI 启用投机解码并解读一次请求的内部流程。

1. 阅读用户面配置 [messages.py:L726-L737](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L726-L737) 与 CLI 参数 [cli/utils.py:L790-L802](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/utils.py#L790-L802)，确认四个可选算法 `eagle / eagle3 / deepseek_mtp / qwen3_5_mtp` 与两个配套参数 `--speculative-draft-model`、`--speculative-num-draft-tokens`。
2. 若本地有配套草稿权重与 GPU，尝试启动（**待本地验证**，命令仅供参考）：
   ```bash
   lmdeploy serve api_server <target_model> \
       --speculative-algorithm eagle \
       --speculative-draft-model <draft_model> \
       --speculative-num-draft-tokens 4
   ```
3. 用同一 prompt、同一 `temperature`，对比「开启 spec」与「关闭 spec」两版的输出文本是否一致（验证「加速但不改分布」），并比较吞吐差异。
4. 开启 `LMDEPLOY_LOG_LEVEL=DEBUG`，在 [spec_agent.py:L405-L546](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/spec_agent.py#L405-L546) 的 `_rejection_sampling` 中观察每步的 `num_rejected_tokens` 分布，估算实际接受率（accept rate = `1 - mean(num_rejected) / (num_spec+1)`）。
5. 写一段小结：接受率越高加速越明显；草稿模型与 target 越「像」，接受率越高——这正是 EAGLE/MTP 类方法训练草稿模型的目标。

> 说明：若不具备运行条件，本任务可降级为「源码阅读型」——只完成第 1、5 步，即阅读配置与撰写小结，跳过实测。

## 6. 本讲小结

- 投机解码用「小草稿模型提议 + 大模型一次并行验证 + 拒绝采样接受」把访存开销摊薄到多个 token，**且不改变输出分布**。
- `SpecModelAgent` 是编排器：`async_model_forward` 跑草稿（自回归生成 N 个 token），`async_sampling_logits` 跑拒绝采样；关闭时由 `BaseSpecModelAgent` 空桩兜底，主引擎退回普通解码。
- 草稿输入相对 target **左移 1 位**，草稿侧 **总是 argmax**，随机性完全由 target 侧的拒绝采样承担。
- `RejectionSampler` 分贪心（首处不匹配即停）与随机（按 `target_prob/draft_prob` 接受，拒绝则用 Gumbel-max 从残差重采样）两路，数学上保证输出分布等于 target。
- `GuidedSpecHelper` 解决结构化解码与投机解码的四处冲突：跨 N+1 位串行 bitmask、fork 保护原件 matcher、拒绝采样驱动接受、Eagle3 草稿词表翻译。
- 设计哲学：空对象模式（no-op stub）、Triton kernel 加速批量判定、CPU 工作 `to_thread` 不阻塞 async 循环。

## 7. 下一步学习建议

- **U9 其它高级特性**：本讲只讲了「单机投机解码」。若想看 KV cache 的复用机制，可接着读 u9-l3（Prefix 缓存与 BlockTrie）；若关心多卡，可读 u9-l4（张量并行）。
- **草稿模型实现**：本讲的 `DeepseekMTP` 是 MTP 风格草稿。可深入 [proposers/qwen3_5_mtp.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/proposers/qwen3_5_mtp.py) 看它为何与 target 共享 dist context（见 [base.py:L21-L22](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/spec_decode/base.py#L21-L22)）。
- **结构化解码本身**：`GuidedSpecHelper` 包装的 `GuidedDecodingManager` 在 `lmdeploy/pytorch/engine/guided_process.py`，可顺藤阅读 xgrammar 集成。
- **回归引擎主线**：若想确认 spec_agent 如何嵌入 EngineLoop 单步，重读 u4-l2 并对照 [engine/model_agent/agent.py:L475-L519](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/model_agent/agent.py#L475-L519) 的 `_async_model_forward` 与 `async_sampling_logits`。
