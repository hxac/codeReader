# 生成与 Chat：在 OLMo-core 里做自回归推理

## 1. 本讲目标

训练好的模型最终要用来「说话」。本讲拆解 OLMo-core 的推理侧——`generate/` 包，学完后你应该能够：

1. 读懂 `TransformerGenerationModule.generate_batch()` 的自回归解码主循环：prefill 与 decode 两个阶段、EOS/停止词处理、`logits_to_keep=1` 的省显存技巧。
2. 说清 `sampling.py` 的四级采样流水线：贪心 → temperature 缩放 → top-k 截断 → top-p（核）采样，以及它们各自如何改写下一个 token 的概率分布。
3. 理解 KV cache 的数据结构（`KVCacheManager` 的四个 buffer）、哪些注意力后端支持它、以及左填充（left-padding）批次如何经 `cache_leftpad` 进入 flash attention 内核。
4. 会用 `TransformerGenerationModule.from_checkpoint()` 从一个训练 checkpoint（本地目录或 URL）组装推理模块，并用 `python -m olmo_core.generate.chat` 交互式聊天。
5. 能判断什么时候该用 OLMo-core 生成、什么时候该换 Hugging Face `generate` 或 vLLM。

本讲承接 u3-l1（Transformer 前向主链路）、u3-l3（注意力后端）与 u6-l4（checkpoint 体系）：生成循环的每一步都是「一次只有 1 个 token 的前向」，而加载权重复用的正是分布式 checkpoint 的读路径。

## 2. 前置知识

### 2.1 自回归生成：prefill 与 decode

语言模型本质上是「给定前文，预测下一个 token」的函数 \( p(x_{t+1} \mid x_1, \dots, x_t) \)。生成文本就是反复调用它：

```
prompt = [t1, t2, t3]
循环：把序列喂进模型 → 取最后一个位置的 logits → 挑出一个 token → 拼到序列尾部 → 重复
```

这个过程分两个性质很不一样的阶段：

| 阶段 | 输入长度 | 每步计算量 | 说明 |
| --- | --- | --- | --- |
| prefill（预填充） | 整个 prompt（可能几千 token） | 一次大方阵前向 | 只为了拿最后 1 个位置的 logits |
| decode（解码） | 每次只 1 个新 token | 一次向量前向 | 重复成百上千次，是延迟的主体 |

这解释了推理的两个核心优化方向：**别把整段序列反复重算**（KV cache 解决）与**别把整个词表的 logits 全物化**（`logits_to_keep` 解决）。

### 2.2 采样策略的直觉

模型在每个位置输出的是全词表上的 logits \( z \in \mathbb{R}^{V} \)，采样决定「怎么从 z 挑出下一个 token」：

- **贪心（greedy）**：永远挑 \(\arg\max_v z_v\)。确定、可复现，但容易生成重复、呆板的文本。
- **temperature（温度）**：先把 logits 除以 \( T \) 再 softmax。概率分布变为

  \[
  p_T(v) = \frac{\exp(z_v / T)}{\sum_{v'} \exp(z_{v'} / T)}
  \]

  \( T < 1 \) 分布更尖（更保守），\( T > 1 \) 更平（更冒险），\( T = 0 \) 退化为贪心。
- **top-k**：只保留 logits 最高的 k 个候选，其余置 \( -\infty \)（softmax 后概率为 0）。
- **top-p（核采样）**：不固定个数，而是保留「累计概率首次超过 p 的最小候选集」。分布尖时候选少，分布平时候选多，比 top-k 更自适应。

### 2.3 KV cache：为什么 decode 不用重算历史

注意力要求当前 token 对**全部历史 token** 的 K、V 做加权。若每步都重算整段序列，生成 \( L \) 个 token 的总代价是 \( O(L^2) \) 次前向量的 K/V 投影。但历史 token 的 K、V 只依赖历史本身，不会变——所以可以把每层的 K、V 存进一块预分配的显存（KV cache），decode 时只算新 token 的 1 列 K/V 追加进去，注意力直接在 cache 上做。代价是显存：cache 形状为 \( (\text{batch}, \text{max_seq_len}, \text{num_kv_heads}, \text{head_dim}) \)。

### 2.4 你需要回忆的前置概念

- **Transformer 前向**（u3-l1）：`Transformer.forward(input_ids)` 在不传 labels 时直接返回 logits 张量。
- **注意力后端**（u3-l3）：SDPA 这一步委托给可插拔 backend（torch / flex / flash_2/3/4 / te），KV cache 支持是各后端的能力差异之一。
- **分布式 checkpoint**（u6-l4）：checkpoint 目录里有 `config.json`（rank 0 写的完整实验配置）和 `model_and_optim/` 下的 DCP 分片；`from_checkpoint` 两者都要读。
- **Config 体系**（u2-l1）：`GenerationConfig` 是普通 `@dataclass` Config，字段校验在 `__post_init__ → validate()` 里完成。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/olmo_core/generate/generation_module/generation_module.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/generate/generation_module/generation_module.py) | `GenerationModule` 抽象基类：与 `Stateful` 对接的最小推理合同 |
| [src/olmo_core/generate/generation_module/config.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/generate/generation_module/config.py) | `GenerationConfig`：生成参数（采样、长度、停止、cache 开关） |
| [src/olmo_core/generate/sampling.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/generate/sampling.py) | 采样策略：`greedy_selection` / `top_k_filtering` / `top_p_filtering` / `select_next_token` |
| [src/olmo_core/generate/generation_module/transformer/generation_module.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/generate/generation_module/transformer/generation_module.py) | `TransformerGenerationModule`：自回归主循环 `generate_batch`、checkpoint 加载与权重平均 |
| [src/olmo_core/generate/generation_module/transformer/config.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/generate/generation_module/transformer/config.py) | `TransformerGenerationModuleConfig`：可序列化的推理模块配置（beta 功能） |
| [src/olmo_core/generate/chat.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/generate/chat.py) | 交互式聊天 CLI：`python -m olmo_core.generate.chat` |
| [src/olmo_core/nn/attention/kv_cache.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/kv_cache.py) | `KVCacheManager`：KV cache 的显存分配与簿记 |

联动文件（本讲引用但不展开）：

| 文件 | 本讲用到的部分 |
| --- | --- |
| [src/olmo_core/nn/attention/__init__.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/__init__.py) | `Attention.sdpa()` 里 cache 的读写钩子、`init_kv_cache_manager()` |
| [src/olmo_core/nn/attention/backend.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/backend.py) | 各后端的 `assert_supports_kv_cache()`、flash 后端的 cache 前向 |
| [src/olmo_core/data/utils.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/utils.py) | `attention_mask_to_cache_leftpad()`：左填充掩码 → cache 偏移 |
| [src/olmo_core/generate/utils.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/generate/utils.py) | `selective_log_softmax()`：只取被选中 token 的 logprob |
| [src/olmo_core/nn/transformer/model.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py) | `Transformer.forward()` 的 `logits_to_keep` 参数 |
| [src/olmo_core/nn/lm_head.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/lm_head.py) | `_select_hidden_for_logits()`：投影前裁剪隐状态 |

## 4. 核心概念与源码讲解

### 4.1 GenerationModule 抽象与 GenerationConfig

#### 4.1.1 概念说明

训练侧有 `TrainModule`（u6-l2），推理侧对应的就是 `GenerationModule`。它刻意做得**极薄**：不规定怎么生成、也不持有生成配置，只约定「一个能从分布式 checkpoint 装载自身权重的对象」。这样推理模块就能直接复用 u6-l4 讲过的 DCP 状态读写基建——`Stateful` 协议 + `state_dict()` / `load_state_dict()` 两个方向。

真正决定「怎么生成」的是 `GenerationConfig`，它把采样、长度上限、停止条件、cache 开关全部收进一个可序列化的 Config——延续全库「一切皆 Config」的模式（u2-l1）。

#### 4.1.2 核心流程

```
GenerationConfig(构造) → __post_init__ → validate()  # 非法组合立即报错
GenerationModule(ABC)  → state_dict() / load_state_dict()   # 权重进出 checkpoint
                       → state_dict_to_load(metadata)        # 加载前定制要读的 state
                       → dp_process_group                    # 数据并行推理时的进程组
```

#### 4.1.3 源码精读

基类全文只有约 20 行：

[generation_module.py:L12-L30](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/generate/generation_module/generation_module.py#L12-L30) —— `GenerationModule` 继承 `Stateful`，只声明 `dp_process_group` 属性（默认返回 `None`，表示用默认进程组）、可选重写的 `state_dict_to_load()`，以及抽象方法 `load_state_dict()`。

再看配置。字段分四组：

[config.py:L14-L42](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/generate/generation_module/config.py#L14-L42) —— `GenerationConfig` 的全部字段：`pad_token_id` / `eos_token_id` 必填；`max_length`（总长上限）与 `max_new_tokens`（新增 token 上限，优先级更高）二选一或都不给；采样参数 `do_sample` / `temperature` / `top_k` / `top_p`；`use_cache` 控制 KV cache；`stop_token_ids` 支持额外的停止词。

[config.py:L47-L66](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/generate/generation_module/config.py#L47-L66) —— `validate()` 在构造期就拦截非法配置：pad 不能等于 eos、`top_k` 必须为正或 -1、`top_p` 必须落在 (0, 1]、temperature 非负等。这是「配置即合同」的又一处体现——坏配置在构造时爆炸，而不是生成到一半才炸。

还有一个重要语义：`do_sample=False` 时**忽略** temperature/top-k/top-p，直接贪心（见下一模块 `select_next_token` 的第一个分支）。另外，`generate_batch()` 支持在调用时用关键字参数覆盖任意生成参数（内部走 Config 的 `replace()` 生成新配置），测试 `test_generation_config_overrides` 验证了这一行为（见 [generation_module_test.py:L167-L190](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/generate/generation_module/transformer/generation_module_test.py#L167-L190)）。

#### 4.1.4 代码实践

**实践目标**：用纯 CPU 上的配置构造，感受 `validate()` 的拦截规则与字段语义。

1. 操作步骤（示例代码，仅需安装 olmo_core，无需 GPU）：

```python
from olmo_core.generate.generation_module.config import GenerationConfig

# 合法配置
gc = GenerationConfig(pad_token_id=0, eos_token_id=1,
                      max_new_tokens=32, do_sample=True,
                      temperature=0.7, top_p=0.9)
print(gc)

# 依次放开下面各行，观察各自抛出的 ValueError：
# GenerationConfig(pad_token_id=0, eos_token_id=0)          # pad == eos
# GenerationConfig(pad_token_id=0, eos_token_id=1, top_p=1.5)  # top_p 越界
# GenerationConfig(pad_token_id=0, eos_token_id=1, top_k=0)    # top_k 非法（0 既不是正数也不是 -1）
# GenerationConfig(pad_token_id=0, eos_token_id=1, max_new_tokens=0)  # 非正长度
```

2. 需要观察的现象：非法配置在**构造时**（而非生成时）抛出 `ValueError`，错误信息里带具体字段名与当前值。
3. 预期结果：合法配置正常打印；四行注释逐个取消注释后各抛一种 `ValueError`。**待本地验证**（具体错误文案以运行结果为准）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `GenerationModule` 基类几乎不定义任何生成逻辑，而 `Trainer`（u6-l1）也把计算全部委托给 train module？两者共同体现了什么设计原则？

**答案**：两者都是「编排与计算分离」——基类只定义与外部系统（checkpoint、训练循环）的接口合同，具体实现（`TransformerGenerationModule` / `TransformerTrainModule`）负责算法。这使框架可以支持新的模型类型（如非 Transformer 的序列模型）而不动基建；`prepare_inference_cache` 的注释甚至明确提到 Mamba 这类「有 cache 状态但不是 KV cache」的模型。

**练习 2**：`max_length` 和 `max_new_tokens` 同时设置时哪个生效？

**答案**：`max_new_tokens` 优先。`generate_batch` 里先检查 `max_new_tokens is not None`，用它加上 prompt 长度算出总长 `max_length`；只有 `max_new_tokens` 为 None 时才直接采用配置里的 `max_length`（见 4.3.3 引用的 L198-L203）。若两者都为 None，则一直生成到 EOS/停止词——但开启 `use_cache` 时这会被显式禁止（cache 必须预分配确定的长度）。

### 4.2 采样策略：sampling.py

#### 4.2.1 概念说明

`sampling.py` 是自包含的纯函数模块（只 import torch），实现「logits → 下一个 token」的全部策略。它既是 `generate_batch` 每步调用的核心，也可以单独拿来实验——对应的测试 `src/test/generate/sampling_test.py` 不需要任何 GPU。

#### 4.2.2 核心流程

`select_next_token` 是唯一入口，内部按序执行：

```
输入 logits (…, V)
1. do_sample=False 或 temperature=0 → 直接 argmax（贪心短路）
2. 检查 NaN（有 NaN 立即 assert 失败并报告比例与所在行）
3. logits /= temperature                      # 分布锐化/平滑
4. top_k != -1 → top_k_filtering              # 只留最高 k 个
5. top_p != 1.0 → top_p_filtering             # 只留累计概率 ≥ p 的核
6. softmax → torch.multinomial 抽 1 个样本
```

注意顺序：**先除温度、再过滤**。由于 temperature 是单调正缩放，先除后滤与先滤后除选出的候选集相同，但对被保留 logits 的相对数值有影响——实现选择了更常见的「缩放后过滤」。

#### 4.2.3 源码精读

[sampling.py:L4-L12](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/generate/sampling.py#L4-L12) —— `greedy_selection`：沿词表维取 `argmax`，确定性、可复现。

[sampling.py:L15-L32](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/generate/sampling.py#L15-L32) —— `top_k_filtering`：用 `torch.topk` 找第 k 大的值作阈值，低于它的位置全部置 \( -\infty \)。两个边界处理：`top_k <= 0` 直接返回原 logits（所以 -1 表示「不过滤」）；`top_k` 超过词表大小时取 `min(top_k, V)`，同样等于不过滤。

[sampling.py:L35-L61](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/generate/sampling.py#L35-L61) —— `top_p_filtering` 是四段里最精巧的：按降序排序 → softmax → 累计概率 `cumsum` → 标记「累计已超过 p」的位置。关键是 L51 与 L54 两次修正：第一个位置永不被删（保证至少留 1 个候选），以及掩码右移一格——**让跨过阈值的那一个 token 也保留**，这样保留的才是「累计概率首次 ≥ p 的最小集合」。最后 `scatter` 回原始下标顺序，被删位置填 \( -\infty \)。

[sampling.py:L90-L112](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/generate/sampling.py#L90-L112) —— `select_next_token` 主体：L90-L91 贪心短路（`do_sample=False` **或** `temperature=0` 都触发贪心，所以 temperature=0 是贪心的等价写法）；L93-L103 的 NaN 防御会在 logits 含 NaN 时给出详细的 assert 信息（NaN 比例、出问题的 batch 行号）——这是排查「推理管线里混入坏权重/坏输入」的第一道哨兵；L105-L109 缩放与两级过滤；L111-L112 用指定 dtype 做 softmax 再 `multinomial` 抽样（dtype 参数用于防溢出）。

配套的单元测试是理解这些边界的最好材料：[sampling_test.py:L23-L43](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/generate/sampling_test.py#L23-L43)（top-k 的 -1/0/超界不过滤）与 [sampling_test.py:L45-L68](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/generate/sampling_test.py#L45-L68)（top-p 的「跨阈值 token 也保留」语义，注释里手算了 softmax 值）。

#### 4.2.4 代码实践

**实践目标**：用可控的合成 logits 直观看到 top-k / top-p 各自砍掉了哪些候选。

1. 操作步骤（示例代码，纯 CPU、只依赖 torch 与 olmo_core）：

```python
import torch
from olmo_core.generate.sampling import top_k_filtering, top_p_filtering, select_next_token

torch.manual_seed(0)
logits = torch.log(torch.tensor([[0.4, 0.3, 0.2, 0.1]]))  # softmax 后恰为 [0.4, 0.3, 0.2, 0.1]

print(top_k_filtering(logits, top_k=2))   # 期望只留前两个，后两个为 -inf
print(top_p_filtering(logits, top_p=0.5)) # 0.4 < 0.5，0.4+0.3=0.7 >= 0.5 → 留前两个
print(select_next_token(logits, do_sample=False))            # 贪心 → token 0
for _ in range(6):
    print(select_next_token(logits, do_sample=True, temperature=1.0, top_p=0.5).item())
# 采样结果只会是 0 或 1；长期频率应接近 0.4:0.3 = 57%:43%
```

2. 需要观察的现象：过滤后张量里被剔除的位置是 `-inf`；`top_p=0.5` 与 `top_k=2` 在这个分布上恰好留下同一批候选；采样 6 次只出现 0 和 1。
3. 预期结果：与 `sampling_test.py` L47-L54 的断言一致（`top_p=0.5` 保留 logits 0.4 与 0.3 两项）。采样频率是随机的，但候选集是确定的。**待本地验证**（multinomial 的具体抽样序列取决于随机种子）。

#### 4.2.5 小练习与答案

**练习 1**：把 `top_p_filtering` 里 L54 的掩码右移（`sorted_indices_to_remove[..., 1:] = sorted_indices_to_remove[..., :-1].clone()`）删掉，行为会怎么变？

**答案**：右移的目的就是「保留跨过阈值的那个 token」。删掉后，累计概率**刚好跨过** p 的 token 会被误删，实际保留集合的累计概率可能略低于 p——不再是严格的「最小候选集」。测试 L47-L54 的例子会失败：top_p=0.5 时第一个 token 累计概率只有 0.4 < 0.5，原本靠右移保留的第二个 token 会被删掉，只剩 1 个候选。

**练习 2**：为什么 NaN 检查放在贪心短路之后、而不是函数最开头？

**答案**：贪心路径走 `argmax`，即便 logits 有 NaN 也能给出确定结果（NaN 不参与 argmax 的最大值比较），而真正的病灶在训练/权重侧，用贪心解码仍可继续排查；只有要**采样**时 NaN 才会污染 softmax 与 multinomial，因此防御性检查只设在采样分支。同时这也是性能考量——NaN 检查需要 `.item()` 同步，贪心路径完全避免。

**练习 3**：temperature=0.7 与 top_p=0.9 同时设置时，候选集和最终分布分别由谁决定？

**答案**：候选集由 top-p 决定（温度不改变 logits 的排序，正数缩放是单调的）；最终分布在候选集内由温度重塑——温度先除在 logits 上，softmax 在过滤后的 logits 上计算。直觉记忆：**top-k/top-p 决定「谁能上场」，temperature 决定「上场后怎么分球」**。

### 4.3 TransformerGenerationModule 的自回归主循环

#### 4.3.1 概念说明

`TransformerGenerationModule` 是 `GenerationModule` 的 Transformer 实现，核心是 `generate_batch()`：一个 while 循环，每圈「一次前向 + 一次采样 + 一次拼接」。它同时处理：prefill/decode 的输入切换、已结束序列的 EOS 强制、多停止词、logits/logprobs 的按需收集、以及一套吞吐统计（TTFT / decode TPS 等）。

#### 4.3.2 核心流程

`generate_batch` 的骨架伪代码：

```
初始化：generation_config.replace(**kwargs)  # 调用级覆盖
        max_length = prompt_len + max_new_tokens 或 config.max_length 或 None
        finished = 全 False 向量；generated = input_ids
若 use_cache：prepare_inference_cache(batch, max_length)   # 预分配 KV cache
否则：      free_inference_cache()

while 未到 max_length 且 not finished.all():
    is_first_forward = (generated 长度 == prompt_len)      # prefill 判定
    输入 = generated 全部        （prefill 或 不用cache）
         = generated[:, -1:]    （decode + cache）         # 只喂最后 1 个 token
    next_logits = model(输入, logits_to_keep=1)            # 只留最后 1 个位置的 logits
    next_tokens = select_next_token(next_logits, 采样参数)
    next_tokens[finished 行] = eos                          # 已结束的序列强制 EOS
    finished |= (next_tokens 命中 eos 或 stop_token_ids)
    generated = cat([generated, next_tokens])

可选：completions_only 时裁掉 prompt 部分；打印吞吐统计
```

#### 4.3.3 源码精读

[generation_module.py:L49-L88](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/generate/generation_module/transformer/generation_module.py#L49-L88) —— 构造函数：解析设备（默认 `get_default_device()`，无 GPU 时自动落到 CPU/MPS）、可选构建 `world_mesh` 并调用 `parallelize_model`（与 u6-l2 讲的是同一个函数，推理时也可以开 DP / float8 / compile）。注意 L68-L71：并行配置只在分布式环境下合法。

[generation_module.py:L108-L123](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/generate/generation_module/transformer/generation_module.py#L108-L123) —— `prepare_inference_cache` / `free_inference_cache`：对每个 block 的 attention 初始化或重置 `kv_cache_manager`（不存在则建、存在则 reset 复用显存）；`free` 则把它置 None 释放。方法名刻意叫 inference **cache** 而非 kv_cache，为非 KV-cache 类模型（如 Mamba 的状态缓存）留余地。

[generation_module.py:L171-L219](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/generate/generation_module/transformer/generation_module.py#L171-L219) —— 循环前的准备段：L174 用 `replace(**generation_kwargs)` 实现调用级参数覆盖；L198-L203 计算 `max_length`（`max_new_tokens` 优先）；L205-L209 若给了注意力掩码则换算成 `prefill_cache_leftpad`（左填充，见 4.4）；L211-L219 按需准备或释放 cache——注意 L213-L216：**开了 `use_cache` 就必须给出 `max_length` 或 `max_new_tokens`**，因为 cache 显存必须按确定的最大长度预分配。

[generation_module.py:L229-L247](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/generate/generation_module/transformer/generation_module.py#L229-L247) —— 循环头与 prefill/decode 切换：L231 用「当前长度是否等于 prompt 长度」判定 prefill；L232-L235 决定喂进模型的是全序列（prefill 或无 cache 模式）还是只有最后 1 个 token（decode + cache）；L243-L247 调 `self.model(..., logits_to_keep=1)`，**只让 LM head 计算最后 1 个位置的 logits**——这个参数顺着 [model.py:L710-L758](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py#L710-L758) 的 `forward` 传到 lm_head，在投影**之前**先把隐状态裁到最后 1 步（见 [lm_head.py:L61-L65](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/lm_head.py#L61-L65) 的 `h = h[:, -logits_to_keep:, :]`），避免物化整个 `(B, S, V)` 的 logits 张量。

[generation_module.py:L249-L274](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/generate/generation_module/transformer/generation_module.py#L249-L274) —— 每步的采样与终止簿记：L249-L255 调用 `select_next_token`；L257-L262 按需收集 logits 或**只对被选中 token** 的 logprob（后者经 [generate/utils.py:L4-L20](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/generate/utils.py#L4-L20) 的 `selective_log_softmax`，注释说明 torch.compile 会优化掉完整 log_softmax 的物化，比 `return_logits` 省得多）；L264-L271 对**上一圈就已结束**的行强制填 EOS token，然后检测本圈是否命中 eos / `stop_token_ids` 并更新 `finished`；L274 把新 token 拼回 `generated`。循环持续到达到 `max_length` 或**所有**序列结束（`finished.all()`）——批内最长的序列决定整批的循环次数。

[generation_module.py:L325-L328](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/generate/generation_module/transformer/generation_module.py#L325-L328) —— `completions_only=True` 时把返回张量裁掉 prompt 部分；logits/logprobs 天然只含 decode 步，不受影响。另外 L276-L323 的计时段会在 `log_timing=True`（默认）时打印 setup / prefill（TTFT）/ decode 各阶段耗时与 tokens/sec——这份统计也是综合实践的观测对象。

#### 4.3.4 代码实践

**实践目标**：在纯 CPU 上用随机初始化的小模型跑通一次自回归生成，并观察采样参数的影响。

1. 操作步骤（示例代码；仿照测试里的 [small_transformer_config](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/generate/generation_module/transformer/generation_module_test.py#L34-L41)，但用 CPU 可跑的 torch 后端 + 关闭 cache）：

```python
import torch
from olmo_core.config import DType
from olmo_core.nn.attention import AttentionBackendName
from olmo_core.nn.transformer import TransformerConfig
from olmo_core.generate import GenerationConfig, TransformerGenerationModule
from olmo_core.utils import seed_all

seed_all(0)
config = TransformerConfig.llama_like(
    d_model=128, n_heads=4, n_layers=2, vocab_size=512,
    attn_backend=AttentionBackendName.torch, dtype=DType.float32,
)
model = config.build()   # 随机权重即可观察机制，无需训练

gen = TransformerGenerationModule(
    model=model,
    generation_config=GenerationConfig(
        pad_token_id=0, eos_token_id=1, max_new_tokens=24, use_cache=False,
    ),
    device=torch.device("cpu"),
)

input_ids = torch.randint(2, 100, (2, 8))          # batch=2, prompt=8
out, _, lp = gen.generate_batch(input_ids, do_sample=False, return_logprobs=True)
print(out.shape)      # (2, 8+实际生成步数)
print(out[:, :8].equal(input_ids))   # True：prompt 原样保留

# 同样的输入再采样新版：换 temperature / top_k / top_p 对比
sampled, _, _ = gen.generate_batch(input_ids, do_sample=True,
                                   temperature=1.5, top_k=50, top_p=0.9)
print(sampled)
```

2. 需要观察的现象：贪心两次运行输出**完全一致**（贪心确定）；采样版输出与贪心不同，且调大 temperature 后（可用 `temperature=2.0` 再跑一次）输出差异更明显；日志末尾打印 `GENERATION STATISTICS`（prefill/decode 分段耗时——CPU 上数值很小但结构可见）。
3. 预期结果：输出形状第二维 = 8 + 实际生成步数（若中途命中 EOS 会提前结束）；`out[:, :8]` 与输入逐元素相等。随机模型生成的是无意义 token 序列——这正常，我们观察的是**机制**而非文本质量。**待本地验证**（具体 token 序列取决于权重与种子）。
4. 常见坑：不要在 CPU 上设 `use_cache=True`——torch 后端不支持 KV cache（见下一模块），`prepare_inference_cache` 会经 `assert_supports_kv_cache()` 抛错。

#### 4.3.5 小练习与答案

**练习 1**：`use_cache=False` 时每个 decode 步的注意力计算量随序列增长如何变化？开 cache 后呢？

**答案**：不开 cache 时每步把**全序列**喂进模型，第 \( t \) 步要算 \( t \) 个 query 对 \( t \) 个 key 的注意力，总代价 \( \sum_t O(t^2) = O(L^3) \) 量级的注意力计算；开 cache 后每步只有 1 个新 query，对 cache 里 \( t \) 个 key 做一次 \( O(t) \) 的注意力，总计 \( O(L^2) \)，且省掉了历史 K/V 投影的重复计算。这正是测试 `test_generation_cache_consistency` 存在的意义：两种模式必须产出**相同**的结果，只允许性能不同。

**练习 2**：L265 对已结束序列强制填的是 EOS token；而测试 [generation_module_test.py:L122-L131](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/generate/generation_module/transformer/generation_module_test.py#L122-L131) 断言「首个 EOS 之后应全是 pad token」。请追踪代码，说明这两者在什么情况下会分歧。

**答案**：分歧只发生在「某行提前结束、而批内其他行还在生成」时——此时该行后续位置被 L265 强制为 EOS（eos=1），而测试期望的是 pad（pad=0）。只有当 EOS 恰好出现在最后一个位置、或全程没有命中 EOS 时两者才不冲突。随机初始化的小模型在短短几步内命中特定 EOS token 的概率很低，所以这条断言路径很少被触发——这是一个很好的「测试断言与实现语义未必同步」的阅读案例：判断以代码为准，必要时向上游求证。

**练习 3**：为什么 `return_logprobs` 比 `return_logits` 省显存？

**答案**：`return_logits` 每步保存完整的 `(B, 1, V)` logits 并在最后 cat 成 `(B, T, V)`——V 通常是 5 万量级；`return_logprobs` 只对**被选中的那 1 个 token** 取 logprob（`selective_log_softmax` + gather），每步只存 `(B, 1)`，最后是 `(B, T)`。做 RLHF/打分类应用只需要被选 token 的 logprob，应始终选后者。

### 4.4 KV cache：KVCacheManager 与后端支持矩阵

#### 4.4.1 概念说明

KV cache 的实现横跨三层：`KVCacheManager`（显存与簿记）→ `Attention`（在 sdpa 前后挂钩子）→ 具体 attention backend（真正读写 cache 的内核调用）。OLMo-core 的设计要点是：**cache 由模块「挂载」而非模型自带**——`Attention.kv_cache_manager` 默认是 None（不缓存），只有推理代码显式调用 `init_kv_cache_manager` 后，前向才走缓存路径。这保证同一个模型类训练/推理两用。

#### 4.4.2 核心流程

```
prepare_inference_cache(batch, max_len)
  └─ 每个 block 的 Attention.init_kv_cache_manager()
       └─ backend.assert_supports_kv_cache()      # 能力检查，不支持即抛错
       └─ new KVCacheManager(batch, max_len, n_kv_heads, head_dim, device)

prefill（第一次前向）:
  Attention.sdpa() → kv_cache_manager.record_leftpad(cache_leftpad)   # 记录左填充
                  → backend(q,k,v, kv_cache_manager=...)              # 内核把 K/V 写入 cache
                  → kv_cache_manager.update_seqlen(q.shape[1])         # 游标前移

decode（后续每步）:
  Attention.forward() → start_pos = kv_cache_manager.current_position()  # RoPE 用游标定位
                     → 只算新 token 的 q/k/v，k/v 追加进 cache
```

#### 4.4.3 源码精读

[kv_cache.py:L11-L52](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/kv_cache.py#L11-L52) —— `KVCacheManager` 是个极简 `nn.Module`，用 4 个 `persistent=False` 的 buffer 持有全部状态：`k_cache` / `v_cache`（形状 `(batch, max_seq_len, num_kv_heads, head_dim)`，注意 **n_kv_heads 而非 n_heads**——GQA 下 KV 头更少，cache 显存随之缩小，见 u3-l3）、`cache_leftpad`（每行左填充数）、`cache_seqlens`（**标量**游标，全批共享同一写入位置）。`persistent=False` 意味着这些 buffer 不进 state_dict——cache 是易失的推理期状态，不该被 checkpoint 污染。

[kv_cache.py:L54-L71](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/kv_cache.py#L54-L71) —— 两个关键簿记方法：`record_leftpad` 在 prefill 时记下每行的填充起点（供 flash attention 内核把有效 KV 对齐到 cache 右端）；`update_seqlen` 把游标加上本步长度——注释特意强调 flash-attn 内核把 `cache_seqlens` 解释为 **cache 内的绝对写入下标**，而非「扣除填充后的逻辑长度」，这是与左填充配合的关键约定。

[kv_cache.py:L101-L118](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/kv_cache.py#L101-L118) —— `reset` 的复用策略：若现有 cache 尺寸够用（`is_reusable`：batch 相等且 `max_seq_len` 不小于需求）就只清零，否则 `reallocate` 重新分配（L73-L99，先显式释放旧张量再重建 buffer，避免双份峰值显存）。连续多轮对话不会反复分配显存。

注意力侧的三个挂钩：[attention/__init__.py:L1312-L1330](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/__init__.py#L1312-L1330) 的 `init_kv_cache_manager`（能力检查 + 按本层 head 配置建 manager；scalable-softmax 与 KV cache 互斥）；[attention/__init__.py:L832-L893](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/__init__.py#L832-L893) 的 `sdpa`（进入 backend 前 `record_leftpad`、把 manager 传给 backend、出来后 `update_seqlen`）；以及 [attention/__init__.py:L1072](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/__init__.py#L1072) 在 forward 里用 `current_position()` 作为 RoPE 的 `start_pos`——decode 步的位置编码靠 cache 游标对齐。

后端能力矩阵（各后端覆写 `assert_supports_kv_cache`，默认实现直接抛错，见 [backend.py:L228-L235](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/backend.py#L228-L235)）：

| 后端 | KV cache | 位置 |
| --- | --- | --- |
| [TorchAttentionBackend](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/backend.py#L317-L319) | ✗ 抛错 | L317-L319 |
| [FlexAttentionBackend](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/backend.py#L595-L597) | ✗ 抛错 | L595-L597 |
| [FlashAttention2Backend](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/backend.py#L821-L823) | ✓ | L821-L823 |
| [FlashAttention3Backend](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/backend.py#L1048-L1050) | ✓ | L1048-L1050 |
| [FlashAttention4Backend](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/backend.py#L1247-L1249) | ✓ | L1247-L1249 |
| [TEAttentionBackend](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/backend.py#L1401-L1403) | ✗ 抛错 | L1401-L1403 |

以 flash_2 为例的真实读写路径：[backend.py:L903-L922](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/backend.py#L903-L922) —— `dispatch_flash_attn_with_kvcache` 直接拿 `kv_cache_manager.k_cache` / `v_cache` 作为输出缓冲（注释：updated **in-place**），并把 `cache_leftpad` 与展开后的 `cache_seqlens` 一并传给内核；同时 L904-L907 拒绝「KV cache + 上下文并行」的组合。

左填充批次的换算在 [data/utils.py:L973-L1003](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/data/utils.py#L973-L1003) 的 `attention_mask_to_cache_leftpad`：先校验掩码确属「前缀填充」（一旦出现有效 token 后面不许再有 padding，否则抛错），再对每行取第一个有效位置的下标作为 leftpad。这解释了 `generate_batch` 文档里「attention_mask 必须是左填充掩码」的约定——右填充会让 cache 里的 KV错位。

#### 4.4.4 代码实践

**实践目标**：通过测试与源码对照，掌握「cache 开关 × 后端选择」的可行性边界，并理解等价性测试的意义。

1. 操作步骤：
   - 精读 [test_generation_cache_consistency](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/generate/generation_module/transformer/generation_module_test.py#L356-L385)：同一个 flash_2 模型对同一段 124 token 的输入分别以 `use_cache=False/True` 生成（都用 `seed_all(0)` 重置随机源），最后断言 token 与 logits **逐元素相等**（L384-L385）。
   - 回答：为什么该测试标了 `@requires_flash_attn_2`？对照上面的后端矩阵——torch 后端 `assert_supports_kv_cache` 会抛错，测不了 cache 路径。
   - 解释 [chat.py:L14-L15](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/generate/chat.py#L14-L15) 的 Mac M4 示例为何加 `--attention-backend torch --no-use-cache`。
2. 需要观察的现象：无 GPU 环境下该测试被跳过（`@requires_gpu`，跳过机制见 u1-l2）；源码层面三个挂钩（record → kernel → update）在 4.4.2 的流程里各就各位。
3. 预期结果：Mac M4 无 CUDA、flash-attn 不可用，注意力构造时会回落 torch 后端（见 u3-l3 讲过的 CPU 回落逻辑），而 torch 后端不支持 KV cache，所以必须同时关掉 `use_cache`，用「每步重算全序列」换正确性。**GPU 上的等价性断言结果待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`cache_seqlens` 为什么是**标量**而不是每行一个向量的 `(batch,)` 张量？

**答案**：因为 `generate_batch` 的循环对整批同步推进——每步所有行（含已结束的行，它们被强制 EOS 后照样写入 cache）都前进 1 个位置，全批共享一个写入下标即可。注意它与 `cache_leftpad`（`(batch,)` 向量）分工不同：leftpad 记录各行有效序列在 cache 行内的**起点**（各行 prompt 长度可以不同），seqlens 记录全局写入**游标**（每步 +1）。kv_cache.py L58-L62 的注释还点出：flash-attn 内核把它解释为 cache 序列维的绝对下标。

**练习 2**：GQA（`n_kv_heads < n_heads`）对 KV cache 显存有什么影响？算一个数：batch=1、max_seq_len=8192、head_dim=128、n_heads=32、n_kv_heads=8、bfloat16。

**答案**：cache 只存 KV 头，显存 \( 2 \times 8192 \times 8 \times 128 \times 2\text{字节} \approx 32\,\text{MB} \) 每层；若不用 GQA（32 个 KV 头）则约 128 MB 每层，32 层模型就是 4 GB 对 1 GB 的差距。这也是长上下文推理偏爱 GQA 的直接原因。

### 4.5 从 checkpoint 到聊天：from_checkpoint 与 chat.py

#### 4.5.1 概念说明

有了生成循环，还差「权重从哪来」。`TransformerGenerationModule.from_checkpoint()` 把「读配置 → 建模型 → 装权重」串成一步，输入可以是一个训练 checkpoint 目录，甚至是一个 URL（经 `cached_path` 支持远程）。`chat.py` 则在其上包了一层 REPL：tokenizer 加载、Jinja2 聊天模板、多轮对话历史、rich 终端渲染。

#### 4.5.2 核心流程

```
from_checkpoint(checkpoint_dir)
  1. rank0 读 checkpoint/config.json → TransformerConfig + TokenizerConfig（读到即广播）
  2. 未给 generation_config 时，用 tokenizer 的 pad/eos 构造默认值
  3. dtype 覆盖：遍历配置树把所有带 dtype 的子配置改写
  4. attention_backend 覆盖：同理改写所有 sequence_mixer/attention 的 backend
  5. transformer_config.build() → cls(model, generation_config) 
  6. load_checkpoint()：优先 model_and_optim/，找不到再退回目录本身
```

chat.py 主循环：读用户输入 →（/quit、/clear、/help 命令）→ 追加到 conversation_history → `apply_chat_template` 渲染成 prompt → tokenize → `generate_batch` → decode → 渲染回复气泡 → 回复入历史，如此往复。

#### 4.5.3 源码精读

[generation_module.py:L431-L447](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/generate/generation_module/transformer/generation_module.py#L431-L447) —— 从 checkpoint 的 `config.json` 里**只**取 `model`（TransformerConfig）与 `dataset.tokenizer` 两块反序列化（注释说明：刻意不加载整个 ExperimentConfig，避免触发与推理无关的校验）。这正是 u2-l3 讲过的「最终配置随 checkpoint 落盘」闭环的消费端。

[generation_module.py:L481-L497](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/generate/generation_module/transformer/generation_module.py#L481-L497) —— 两个推理期覆盖器：`dtype` 用 `transformer_config.apply(...)` 遍历整棵配置树统一改精度（典型用法：训练 bf16、推理也想 bf16）；`attention_backend` 同样递归下钻到每个 `sequence_mixer`/`attention` 节点替换 backend（chat.py 的 `--attention-backend torch` 就走到这里）。

[generation_module.py:L330-L387](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/generate/generation_module/transformer/generation_module.py#L330-L387) —— `load_checkpoint` 的两级查找：先试 `checkpoint_dir/model_and_optim/`（训练 checkpoint 的模型分片所在，u6-l4），元数据不存在则退回 `checkpoint_dir` 本身（兼容「只导出模型权重」的目录）。找到后走 u6-l4 讲过的 DCP `load_state_dict`（支持 pre_download 远程 checkpoint、多线程加载），最后 `self.load_state_dict(state_dict)` 灌进模型。

[generation_module.py:L515-L604](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/generate/generation_module/transformer/generation_module.py#L515-L604) —— `from_checkpoints` 是彩蛋功能：传入**多个** checkpoint 时在 CPU 上逐个加载、做**权重平均**（model soup）。L557-L565 用增量均值 `target.mul_((i-1)/i).add_(source, alpha=1/i)` 原地累加，避免同时持有所有模型；对应测试 [test_from_checkpoints_weight_averaging](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/generate/generation_module/transformer/generation_module_test.py#L543-L594) 验证平均后再生成的输出等于手动平均权重的输出。

chat.py 侧：[chat.py:L165-L179](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/generate/chat.py#L165-L179) 从 checkpoint 的 `config.json` 取 `dataset.tokenizer` 还原 `TokenizerConfig`（失败则回落 dolma2）；[chat.py:L43-L44](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/generate/chat.py#L43-L44) 默认聊天模板只是**朴素拼接消息**（无系统提示、无助手开头标记）——OLMo 基座模型不是对话微调模型，这个「最简模板」是刻意选择，可用 `--chat-template` 换成真正的对话模板；[chat.py:L421-L436](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/generate/chat.py#L421-L436) 一轮对话的核心四步：`apply_chat_template(tokenize=False)` 渲染 → `tokenizer.encode` → `generate_batch(completions_only=True, log_timing=False)` → `tokenizer.decode`；入口在 [chat.py:L460-L462](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/generate/chat.py#L460-L462)，`prepare_cli_environment()` 先行（u1-l4 见过它设置日志与随机种子）。

#### 4.5.4 代码实践

**实践目标**：跑通官方 CLI，对一个公开 checkpoint 做交互式推理；无下载条件时完成「配置阅读」替代任务。

1. 操作步骤（有网络与 GPU 的环境）：

```bash
# 模块 docstring 里的官方示例（chat.py L6-L15）
python -m olmo_core.generate.chat path/to/checkpoint --max-new-tokens 512 --temperature 0.7

# 贪心解码
python -m olmo_core.generate.chat path/to/checkpoint --max-new-tokens 256 --do-sample False

# Mac / 无 CUDA 环境：torch 后端 + 关闭 KV cache
python -m olmo_core.generate.chat https://olmo-checkpoints.org/ai2-llm/Olmo-3-1025-7B/stage2/step47684/ \
    --attention-backend torch --no-use-cache
```

2. 替代任务（源码阅读型，无需下载）：在 [chat.py:L182-L291](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/generate/chat.py#L182-L291) 的 argparse 定义里找出：哪个参数控制「生成到 EOS 还是固定长度」、哪个参数在无 flash-attn 机器上必须成对出现、`--max-length` 与 `--max-new-tokens` 同时给时哪个生效（提示：追到 [chat.py:L315-L325](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/generate/chat.py#L315-L325) 的 `max_new_tokens=args.max_new_tokens if args.max_length is None else None`）。
3. 需要观察的现象：启动时先打印 Tokenizer Info 与 Generation Config 两块面板；每轮输入后进入 `Generating response...` 状态（`console.status` 动画）；生成结束以气泡面板展示回复。
4. 预期结果：贪心模式下同一句问话两次回复**逐字相同**；采样模式（默认 `do_sample=True, top_p=0.7`）下回复有变化。**待本地验证**（需要能访问 checkpoint 存储与相应硬件）。

#### 4.5.5 小练习与答案

**练习 1**：`from_checkpoint` 为什么只解析 `config.json` 的 `model` 和 `dataset.tokenizer` 两块，而不是整个 `ExperimentConfig.from_dict`？

**答案**：完整 ExperimentConfig 还包含 trainer、data_loader、optim 等**推理用不到**的子树；其中很多字段带构造期校验（例如数据路径存在性、并行配置合法性），在推理机上逐项满足既无必要也容易失败。只取需要的两块既省事又健壮（KeyError 时给出明确的 `OLMoConfigurationError`，见 L444-L447）。

**练习 2**：`from_checkpoints([...])` 的权重平均与「分别生成再投票」有什么本质区别？平均后模型的 vocab_size、层数需要一致吗？

**答案**：权重平均是**参数空间**的融合（逐张量算术均值），得到单个新模型后一次生成；它在数学上不等价于输出空间的集成。因此所有 checkpoint 的结构必须严格一致——代码里 `assert target_tensor.shape == source_tensor.shape`（L561）就是硬约束；词表、层数自然也必须相同。这正对应训练末段把多个 anneal checkpoint 平均成最终发布权重的常见做法。

**练习 3**：OLMo-core 生成、Hugging Face `transformers` 的 `model.generate()`、vLLM 各适合什么场景？

**答案**：三者的取舍大致是——**OLMo-core 生成**：与训练栈零转换成本（直接读 DCP checkpoint、复用同一套 Config 与注意力实现）、支持 DP 多卡批推理与 float8，适合训练管线内的抽查、消融、坐标检查类自测；但 KV cache 依赖 flash-attn 系后端，批调度相对朴素。**HF generate**：生态最广（量化、适配器、对话模板齐全），适合单机调试与下游微调后的评估，但需先把权重转成 HF 格式（见 u10-l5 的转换脚本）。**vLLM**：专为高吞吐服务设计（PagedAttention、连续批处理），生产部署首选，但要求模型结构在其支持列表内。选型口诀：**训练侧自测用 OLMo-core，生态集成用 HF，在线服务用 vLLM**。

## 5. 综合实践

设计一个「生成行为实验台」，把本讲的采样、循环与统计串起来。以下为示例代码（CPU 可跑，随机权重；有 GPU 可换 bfloat16 + flash_2 + use_cache=True）：

```python
# gen_playground.py
import torch
from olmo_core.config import DType
from olmo_core.nn.attention import AttentionBackendName
from olmo_core.nn.transformer import TransformerConfig
from olmo_core.generate import GenerationConfig, TransformerGenerationModule
from olmo_core.utils import seed_all

def build(seed=0):
    seed_all(seed)
    config = TransformerConfig.llama_like(
        d_model=128, n_heads=4, n_layers=2, vocab_size=512,
        attn_backend=AttentionBackendName.torch, dtype=DType.float32)
    return TransformerGenerationModule(
        model=config.build(),
        generation_config=GenerationConfig(
            pad_token_id=0, eos_token_id=1, max_new_tokens=32, use_cache=False),
        device=torch.device("cpu"))

gen = build()
prompt = torch.randint(2, 100, (1, 8))

# 任务 1：贪心的确定性 —— 同一 prompt 生成两次，断言完全一致
a, _, _ = gen.generate_batch(prompt, do_sample=False, log_timing=False)
b, _, _ = gen.generate_batch(prompt, do_sample=False, log_timing=False)
print("greedy deterministic:", torch.equal(a, b))

# 任务 2：采样参数扫掠 —— 对比三组参数的输出差异与 logprob
for name, kw in [
    ("low-T",  dict(do_sample=True, temperature=0.3, top_k=-1, top_p=1.0)),
    ("high-T", dict(do_sample=True, temperature=2.0, top_k=-1, top_p=1.0)),
    ("nucleus", dict(do_sample=True, temperature=1.0, top_p=0.5)),
]:
    out, _, lp = gen.generate_batch(prompt, log_timing=False, **kw)
    print(f"{name:>8}: len={out.shape[1]-8}, mean_logprob={lp.mean():.3f}")

# 任务 3：让统计说话 —— 打开 log_timing，观察 prefill(TTFT) 与 decode 两段耗时
gen.generate_batch(prompt, do_sample=False, log_timing=True)
```

检验点：

1. 任务 1 应打印 `True`；若把 `do_sample=False` 换成 `do_sample=True, temperature=0.0`，结果应同样确定（temperature=0 等价贪心）。
2. 任务 2 中 low-T 的平均 logprob 通常最高（分布尖、模型更「自信」）；nucleus 组的输出应与 low-T 组不同。具体数值**待本地验证**。
3. 任务 3 的 `GENERATION STATISTICS` 里，prompt 只有 8 个 token，prefill 段很短；把 prompt 加长到 256（`torch.randint(2, 100, (1, 256))`）再跑，prefill 耗时占比应明显上升——这正是 prefill/decode 两阶段差异的直接观测。
4. 进阶（GPU）：仿照 `test_generation_cache_consistency` 用 flash_2 后端验证 `use_cache=True/False` 输出逐元素相等，并对比两版的 decode TPS。

## 6. 本讲小结

- **生成循环**：`generate_batch` 是「前向 + 采样 + 拼接」的 while 循环；prefill 喂全序列、decode 只喂最后 1 个 token（开 cache 时），`logits_to_keep=1` 让 LM head 只算最后 1 个位置的 logits，避免物化整段词表张量。
- **采样流水线**：`select_next_token` = 贪心短路 → NaN 哨兵 → temperature 缩放 → top-k → top-p → multinomial；top-p 用「累计概率 + 掩码右移」保留跨阈值 token，保证是最小核集合。
- **KV cache 三层结构**：`KVCacheManager`（4 个非持久 buffer：k/v/leftpad/seqlens）→ `Attention`（record → backend → update 游标，decode 的 RoPE 用游标定位）→ backend 内核（仅 flash_2/3/4 支持；torch/flex/te 会 `assert_supports_kv_cache` 抛错，CPU 上只能 `use_cache=False`）。
- **终止与批量语义**：循环直到 `max_length` 且 `finished.all()`；提前结束的行被强制填 EOS 继续陪跑；`max_new_tokens` 优先于 `max_length`；开 cache 必须给出确定长度。
- **checkpoint 消费端**：`from_checkpoint` 只从 `config.json` 取 model 与 tokenizer 两块配置，支持 dtype/backend 递归覆盖与 URL 远程加载；`from_checkpoints` 附带增量式权重平均（model soup）。
- **选型边界**：OLMo-core 生成胜在「训练栈零转换」，生产服务交给 vLLM，生态集成交给 HF——三者在 KV cache 实现与批调度能力上差距明显。

## 7. 下一步学习建议

- **u10-l5（checkpoint 转换）**：想让 HF/vLLM 消费 OLMo-core 训出的权重，就离不开 `examples/huggingface/convert_checkpoint_to_hf.py` 与 `nn/conversion/` 的状态映射框架——本讲的 `from_checkpoint` 是它的镜像方向。
- **u10-l1（float8 训练）**：`TransformerGenerationModule` 构造参数里的 `float8_config` 与训练侧共用同一套 `float8/ao.py` 基建，可对照阅读推理侧低精度与训练侧低精度的差异。
- **源码延伸**：`src/test/generate/generation_module/transformer/generation_module_test.py` 后半段（L388 起）有分布式（FSDP）批推理的测试，是理解 `dp_process_group` 与多卡 generate 的最好入口；`TransformerGenerationModuleConfig`（本讲 4.5 未展开的 [transformer/config.py:L26-L96](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/generate/generation_module/transformer/config.py#L26-L96)）则是把推理模块纳入 Config 体系的 beta 尝试。
- **方法论延伸**：prefill/decode 两阶段成本模型（TTFT vs throughput）是所有推理引擎的核心权衡；读完本讲后看 vLLM 的 continuous batching 与 PagedAttention 文档，会发现自己已经能读懂大部分设计动机。
