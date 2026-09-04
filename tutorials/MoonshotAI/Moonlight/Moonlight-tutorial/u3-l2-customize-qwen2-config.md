# 定制你的玩具模型：Qwen2Config 配置详解

## 1. 本讲目标

学完本讲，你应该能够：

1. 掌握「用代码构造 `Qwen2Config` 再实例化模型」这条路线，理解它与 `from_pretrained` 加载预训练权重的本质区别。
2. 逐项说出 `get_model_and_dataloader` 中那份配置里每个字段的含义，分清哪些字段决定参数量、哪些只是行为开关。
3. 理解 `tie_word_embeddings=True`（权重共享）、`rms_norm_eps`（归一化数值稳定项）等关键配置的作用。
4. 能独立缩放模型规模（层数、隐层宽度、FFN 宽度）以适配自己的算力，并会估算参数量、每步 FLOPs 与吞吐。
5. 处理「换分词器导致 vocab_size 不匹配」的问题，并知道何时必须删除 token 缓存。

本讲是专家层的「模型定制」讲，承接 u1-l3（训练主循环）与 u1-l4（数据管线），并与 u2-l1（参数分组）、u3-l1（效率度量）形成交叉印证。

## 2. 前置知识

### 2.1 从预训练权重加载 vs 从配置构造

transformers 生态里有两条获得模型的路径：

| 路径 | 写法 | 权重来源 | 典型用途 |
|---|---|---|---|
| 预训练加载 | `AutoModelForCausalLM.from_pretrained("Qwen/...")` | 下载 checkpoint | 推理、微调 |
| 配置构造 | `Qwen2Config(...)` → `Qwen2ForCausalLM(config)` | 随机初始化 | 从零预训练、做实验 |

toy_train.py 走的是第二条路：它要比较的是**优化器**，模型本身必须从零训练，所以根本不需要预训练权重。注意一个容易混淆的点：config 里的 `torch_dtype="bfloat16"` 只是元数据（主要供 `from_pretrained(torch_dtype="auto")` 使用），直接 `Qwen2ForCausalLM(config)` 构造出的权重默认是 float32（待本地验证：打印 `next(model.parameters()).dtype` 确认）。

### 2.2 一个 decoder-only Transformer 的「形状自由度」

回顾 u1-l1 讲过的 Qwen2 结构，每层 = 注意力块 + FFN 块 + 两个 RMSNorm。决定形状的自由度只有几个：

- 隐层宽度 \( H \)（`hidden_size`）：所有层的「公共走廊」宽度。
- 层数 \( L \)（`num_hidden_layers`）：堆多少个相同的块。
- FFN 宽度 \( I \)（`intermediate_size`）：SwiGLU 中间层宽度，`gate/up` 把 \( H \) 升到 \( I \)，`down` 再降回来。
- 注意力头数与 KV 头数：\( H \) 被切成 `num_attention_heads` 份，每份 \( d_{\text{head}} = H / n_{\text{heads}} \)。

其余字段（激活函数、eps、RoPE 基频等）不改变参数量，只改变数值行为。

### 2.3 你将从本讲带走的核心直觉

> **玩具模型里，词嵌入矩阵常常是最大的单一参数块；而 Muon 的形状自适应学习率（u2-l4）保证了无论你把矩阵改成什么形状，更新量级都自动对齐——这就是「改配置做实验」在这个仓库里特别安全的原因。**

## 3. 本讲源码地图

本讲涉及的关键文件（仓库极简，全部聚焦在一个源码文件的一个函数内）：

| 文件 | 本讲关注的区域 | 作用 |
|---|---|---|
| [examples/toy_train.py](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py) | [L242-L284](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L242-L284) | `get_model_and_dataloader`：本讲主角，含 tokenizer 加载与 Qwen2Config 构造 |
| [examples/toy_train.py](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py) | [L26-L36](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L26-L36) | token 缓存与样本数计算：换分词器时的缓存陷阱 |
| [examples/toy_train.py](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py) | [L320-L346](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L320-L346) | 命令行参数与调用点：`--hidden_size` 是唯一暴露的形状旋钮 |
| [requirements.txt](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/requirements.txt) | 全文 | 锁定 transformers==4.49.0，字段行为以该版本为准 |
| [README.md](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md) | [L47-L65](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L47-L65) | 性能表脚注「参数量统计不含 embedding」，是本讲参数量口径的出处 |

## 4. 核心概念与源码讲解

本讲的最小模块：

1. Qwen2Config 字段含义
2. 模型规模控制
3. tokenizer 与 vocab 对齐
4. 参数量与吞吐评估

### 4.1 Qwen2Config 字段含义

#### 4.1.1 概念说明

`Qwen2Config` 是一个纯数据的「模型图纸」：它不包含任何权重，只描述架构超参。`Qwen2ForCausalLM(config)` 读图施工，按图纸随机初始化出全部权重。理解图纸的最好方式是把 22 个字段分成三类：

- **结构字段**：改变任何一个都会改变参数张量的形状，从而改变参数量。
- **数值行为字段**：不改变形状，只改变前向计算的数值特性。
- **杂项/元数据字段**：特殊 token id、序列化用的 `model_type` 等。

#### 4.1.2 核心流程

`get_model_and_dataloader` 的装配顺序：

```text
load_dataset(语料)
    │
    ├─ model_name == "qwen" → Qwen2Tokenizer.from_pretrained("Qwen/Qwen2.5-0.5B")
    │       （分词器：只用它的词表，不用它的模型权重）
    │
    ├─ MoonDataset(语料, 分词器) ──→ DataLoader(batch_size=16, shuffle=True)
    │
    └─ Qwen2Config(22 个关键字参数) ──→ Qwen2ForCausalLM(config)
            （随机初始化，float32，尚在 CPU 上）
```

#### 4.1.3 源码精读

先看函数骨架——tokenizer 与 config 是**两个独立来源**，对齐责任在读者手里：

- [examples/toy_train.py:L247-L252](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L247-L252)：当 `model_name == "qwen"` 时，从 `Qwen/Qwen2.5-0.5B` 加载 **Qwen2Tokenizer**——注意这里只借用了 0.5B 模型的分词器（词表 151936），并没有加载它的模型权重；其他模型名直接断言报错。
- [examples/toy_train.py:L253-L254](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L253-L254)：用该分词器构建 `MoonDataset`（u1-l4 精读过的定长分块），DataLoader 固定 `batch_size=16`。
- [examples/toy_train.py:L256-L281](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L256-L281)：手工构造 `Qwen2Config` 并立即 `Qwen2ForCausalLM(config)` 实例化——这是本讲的主战场。
- [examples/toy_train.py:L329-L331](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L329-L331)：`__main__` 里的调用点，`args.hidden_size` 是从命令行传进来的唯一形状参数。

下面逐类拆解 22 个字段。**结构字段**（决定参数量）：

| 字段 | 值 | 作用与注意点 |
|---|---|---|
| `hidden_size`（[L262](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L262)） | 参数 `hidden_size`（CLI 默认 1024） | 所有层的公共宽度 \( H \)。必须能被头数 16 整除（u1-l2 已确认），否则构造时报错。 |
| `num_hidden_layers`（[L269](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L269)） | 12 | 层数 \( L \)，写死在源码里，改它需要编辑脚本。 |
| `num_attention_heads`（[L268](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L268)） | 16 | 查询头数；\( d_{\text{head}} = H/16 \)。 |
| `num_key_value_heads`（[L270](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L270)） | 16 | KV 头数。**与查询头数相等 → 未使用 GQA**，k/v 投影也是满秩的 \( H \times H \) 方阵。真正的 Qwen2.5-0.5B 用的是 2 个 KV 头。 |
| `intermediate_size`（[L264](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L264)） | 4864 | SwiGLU FFN 宽度 \( I \)，gate/up/down 三个矩阵的公共维度。 |
| `vocab_size`（[L279](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L279)） | 151936 | 词表大小，决定嵌入矩阵行数；必须与分词器对齐（见 4.3）。 |
| `tie_word_embeddings`（[L274](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L274)） | True | 输入嵌入与输出 lm_head **共享同一块权重**，省下 \( V \times H \) 个参数。u2-l1 精读分组时确认过：正因共享，`lm_head` 过滤条件在玩具模型里实际是针对 untied 模型的防御性代码。 |

**数值行为字段**（不改参数量）：

| 字段 | 值 | 作用 |
|---|---|---|
| `hidden_act`（[L261](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L261)） | "silu" | FFN 激活：`down(silu(gate(x)) * up(x))`，即 SwiGLU。 |
| `rms_norm_eps`（[L271](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L271)） | 1e-06 | RMSNorm 分母里的防零小量：\( x / \sqrt{\tfrac{1}{H}\sum_i x_i^2 + \varepsilon} \)。太小可能下溢、太大损害归一化精度，Qwen 系一律取 1e-6。 |
| `initializer_range`（[L263](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L263)） | 0.02 | 随机初始化的正态标准差。 |
| `attention_dropout`（[L258](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L258)） | 0.0 | 注意力权重上的 dropout，训练态也关闭。 |
| `rope_theta`（[L272](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L272)） | 1000000.0 | RoPE 基频，Qwen2.5 长文本配置的取值（基频越大，远距离衰减越慢）。 |
| `torch_dtype`（[L275](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L275)） | "bfloat16" | 元数据；直接构造时权重仍是 float32（见 2.1）。 |

**RoPE 窗口与杂项字段**：

| 字段 | 值 | 作用 |
|---|---|---|
| `max_position_embeddings`（[L265](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L265)） | 513 | RoPE 位置编码覆盖的最大位置数。MoonDataset 窗口是 512（[L17](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L17)），513 恰好留了 1 的裕量——窗口长度不应超过它。 |
| `use_sliding_window` / `sliding_window` / `max_window_layers`（[L266](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L266)、[L273](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L273)、[L278](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L278)） | False / 1024 / 12 | 滑动窗口注意力开关组。`use_sliding_window=False` 使另外两个在本配置下不生效；且序列长 512 < 1024，即使开启也不会截断。 |
| `use_mrope`（[L277](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L277)） | False | 多模态分段 RoPE 开关（Qwen2-VL 血缘字段），纯文本训练用不到。 |
| `use_cache`（[L276](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L276)） | True | 推理时的 KV cache 开关，训练循环不涉及。 |
| `bos_token_id` / `eos_token_id`（[L259-L260](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L259-L260)） | 151643 | Qwen 词表的 `<\|endoftext\|>` id，供生成时用；换词表时需同步换（见 4.3）。 |
| `model_type`（[L267](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L267)） | "qwen2" | 序列化与 `AutoModel` 注册表识别用的架构名。 |

一个有意思的观察：这份配置的很多取值（`intermediate_size=4864`、`tie_word_embeddings=True`、`rope_theta=1e6`）与 Qwen2.5-0.5B 官方 config.json 一致，但结构性字段被改小改整——12 层（原 24 层）、16 头 MHA（原 14 头 + 2 KV 头的 GQA）、513 位置（原 32768）。可以把它理解为「借 0.5B 的数值配置、盖一间 1/4 大小的毛坯房」。

#### 4.1.4 代码实践

**实践目标**：不动模型，先单独把「图纸」拿在手里看。

**操作步骤**（示例代码，可在仓库根目录的 python 交互环境中运行）：

```python
# 示例代码：只构造配置，不构建模型
import sys; sys.path.insert(0, "examples")
from transformers import Qwen2Config

config = Qwen2Config(
    hidden_size=512, num_hidden_layers=6, num_attention_heads=16,
    num_key_value_heads=16, intermediate_size=1536, vocab_size=151936,
    tie_word_embeddings=True, max_position_embeddings=513,
)
print(config.num_hidden_layers, config.hidden_size)        # 6 512
print(config.head_dim if hasattr(config, "head_dim") else "head_dim 由模型侧计算")
print(config.to_dict().keys())                              # 看全部字段
```

**需要观察的现象**：配置对象可以被独立创建和打印；`to_dict()` 里除了你传入的字段，还有一批带默认值的字段。

**预期结果**：能打印出字段字典；`hidden_size=512` 与头数 16 满足整除关系，构造不会报错。

**待本地验证**：`head_dim` 是否作为属性存在于 config（不同 transformers 版本行为不同），以本地 transformers==4.49.0 实测为准。

#### 4.1.5 小练习与答案

**练习 1**：`tie_word_embeddings=True` 相对 `False` 省了多少参数（用默认 hidden_size=1024）？

**答案**：省 \( 151936 \times 1024 = 155{,}582{,}464 \) 个参数（约 1.56 亿）。这就是为什么 u2-l1 数参数时嵌入矩阵只数了一次。

**练习 2**：把 `rms_norm_eps` 从 1e-6 改成 1e-2，参数量会变吗？训练 loss 呢？

**答案**：参数量完全不变（RMSNorm 的可学习向量形状不变，eps 只是数值常量）；训练 loss 大概率变差，因为归一化被 eps 过度主导，缩放因子趋近常数，削弱了归一化效果。

**练习 3**：`use_sliding_window=False` 时，`sliding_window=1024` 和 `max_window_layers=12` 还有作用吗？

**答案**：没有实际作用——开关关闭时这两个字段不参与前向逻辑；且即便开启，序列长 512 小于窗口 1024 也不会截断。

### 4.2 模型规模控制

#### 4.2.1 概念说明

「缩放模型规模」就是同时调 \( H \)、\( L \)、\( I \) 三个旋钮，并保证约束成立：

1. **整除约束**：\( H \bmod n_{\text{heads}} = 0 \)（头数固定 16，所以 \( H \) 必须是 16 的倍数）。若使用 GQA，还需头数能被 KV 头数整除。
2. **位置约束**：MoonDataset 窗口 `max_length`（默认 512）≤ `max_position_embeddings`（513）。
3. **口径约束**：改模型不影响数据管线与调度器——cosine 总步数仍由 `len(train_loader)` 决定（u1-l4），每步 token 数仍固定为 8192（u3-l1）。

三个旋钮里，命令行只暴露了 `hidden_size`；`num_hidden_layers` 和 `intermediate_size` 写死在函数里，**改它们必须编辑源码**。做实验时的推荐姿势是先复制一份脚本（如 `examples/toy_train_small.py`），保住原脚本作为基线（u3-l1 的对比实验、u3-l5 的二次开发都要回到基线）。

#### 4.2.2 核心流程

```text
想缩放模型
    │
    ├─ 只改宽度？ ──→ python3 examples/toy_train.py --hidden_size 512 ...
    │                 （token 缓存 .bin 与模型无关，可复用）
    │
    └─ 改层数 / FFN 宽度？ ──→ 复制脚本 → 编辑 Qwen2Config 两行 → 运行副本
```

宽度的连锁反应：\( H \) 变小会让注意力矩阵、FFN、嵌入**同时**变窄；而 \( L \)、\( I \) 只影响各自的块。参数量对它们的依赖见 4.4 的公式。

#### 4.2.3 源码精读

- [examples/toy_train.py:L325](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L325)：`--hidden_size` 参数，默认 1024——唯一暴露给命令行的形状旋钮；README 的示例命令（[README.md:L131-L137](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L131-L137)）用的 896 恰是 Qwen2.5-0.5B 的原始宽度。
- [examples/toy_train.py:L262](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L262)：`hidden_size=hidden_size` 把命令行的值注入图纸——嵌入、注意力、FFN、Norm 的形状全部随之而定。
- [examples/toy_train.py:L269](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L269)、[L264](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L264)：`num_hidden_layers=12`、`intermediate_size=4864` 写死。缩小模型时改这两行；注意 `max_window_layers=12`（[L266](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L266)）虽然数值上等于层数，但滑动窗口关闭时无需联动修改。
- [examples/toy_train.py:L17](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L17) 与 [L265](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L265)：窗口 512 与位置上限 513 的配对。若你把窗口调大到超过 513，transformers 的 RoPE 缓存可能按需重算而不报错（待本地验证），但建议同步调大 `max_position_embeddings` 以保持配置语义一致。
- [examples/toy_train.py:L336-L337](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L336-L337)：模型构造在 CPU、`.to(device)` 在优化器创建之后——u1-l3 已确认顺序安全（优化器状态惰性创建）。缩放模型不需要动这段。

还有一个与 u2-l4 的漂亮联动：缩小 \( H \) 后，Muon 组里所有矩阵形状都变了，但 `adjust_lr_for_muon` 的 \( 0.2\sqrt{\max(A,B)} \) 缩放会自动补偿形状变化，使更新 RMS 仍统一为 \( 0.2\eta \)。**改配置缩模型不需要重新调 Muon 的学习率量级**——这正是 Moonlight 论文「开箱即用」主张的直接受益场景。

#### 4.2.4 代码实践

**实践目标**：亲手触发一次整除约束，看清报错长什么样。

**操作步骤**：

1. 运行 `python3 examples/toy_train.py --hidden_size 1000 --optimizer adamw`（1000 不是 16 的倍数）。
2. 观察报错发生在哪个阶段（数据分词之后、模型构造处）。
3. 换成 `--hidden_size 1008`（16 的倍数，头维 63）再跑，确认能进入训练循环。

**需要观察的现象**：第一次运行在 `Qwen2ForCausalLM(config)` 构造时抛出断言/维度错误（具体报错形式随 transformers 版本而异，待本地验证）；token 缓存 `.bin` 在两次运行间被正常复用（模型形状不影响缓存）。

**预期结果**：1000 失败、1008 成功，从而确认「\( H \) 必须是头数 16 的倍数」这一约束来自模型侧而非数据侧。

#### 4.2.5 小练习与答案

**练习 1**：只把层数从 12 改成 24，训练总步数、每步 token 数、参数量各怎么变？

**答案**：总步数不变（由 `len(train_loader)` 决定，见 [L344](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L344)）；每步 token 数不变（16×512=8192）；非嵌入参数量线性翻倍（每层结构相同）。

**练习 2**：为什么 README 说 Moonlight 16B 与 DSV2-Lite 是「15.29B 总参数 / 2.24B 激活参数」两套口径？

**答案**：MoE 模型每层有多个专家但每个 token 只激活部分专家：总参数是全部专家之和，激活参数是单 token 前向实际经过的参数。性能表脚注（[README.md:L65](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L65)）还注明两套口径均排除 embedding——与本讲 4.4 的口径一致。

**练习 3**：想做一个 GQA 版玩具模型（省 KV 参数），改哪两个字段？

**答案**：把 `num_key_value_heads` 调小（如 2，需整除 `num_attention_heads`），同时保证 \( H \) 仍被查询头数整除。k/v 投影形状变为 \( H \times (2 d_{\text{head}}) \)，Muon 的形状自适应缩放会自动适配新形状（u2-l4）。

### 4.3 tokenizer 与 vocab 对齐

#### 4.3.1 概念说明

模型与分词器是两套独立的东西，靠 **vocab_size** 这一个数字握手：

```text
分词器：把文本映射为 id ∈ [0, V_tok)
模型：  嵌入矩阵有 V_cfg 行，查表要求 id ∈ [0, V_cfg)

对齐条件：V_tok ≤ V_cfg（最好相等）
```

- 若 \( V_{\text{tok}} > V_{\text{cfg}} \)：训练时会直接 `index out of range` 崩溃。
- 若 \( V_{\text{tok}} < V_{\text{cfg}} \)：**能跑但浪费**——嵌入矩阵有一堆永远查不到的行（本仓库默认 Qwen 分词器 151936 与 `vocab_size=151936` 严格相等，是最干净的形态）。

换分词器还牵动两件容易遗忘的事：`bos/eos_token_id` 是 Qwen 词表的 151643，换词表后语义失效；以及 u1-l4 埋过的雷——**token 缓存键只含数据集名**。

#### 4.3.2 核心流程

```text
换分词器
    │
    ├─ 1. 删除旧的 {dataset_name}.bin（缓存键不含分词器！）
    ├─ 2. 修改 tokenizer 加载分支（L248-L250）
    ├─ 3. 把 config 的 vocab_size 改成新词表大小（L279）
    └─ 4. 顺手更新 bos/eos_token_id（L259-L260）
```

若只做第 2 步不做第 1 步，`MoonDataset` 会把**旧分词器的 token 流**喂给新词表的模型——不报错但数据全错，这是本脚本最隐蔽的坑。

#### 4.3.3 源码精读

- [examples/toy_train.py:L248-L250](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L248-L250)：`Qwen2Tokenizer.from_pretrained("Qwen/Qwen2.5-0.5B")`——词表 151936，与 [L279](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L279) 的 `vocab_size=151936` 一一对应，这个相等关系是脚本能跑通的隐含前提，没有任何代码检查它。
- [examples/toy_train.py:L27-L28](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L27-L28)：缓存判断 `os.path.exists(f"{self.dataset_name}.bin")` → `torch.load(...)`。键里**只有数据集名**：改 `hidden_size` 可复用缓存（token 与模型无关），换分词器则必须手动删缓存。
- [examples/toy_train.py:L30-L33](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L30-L33)：首次分词逐篇 `tokenizer.encode(text, add_special_tokens=True)` 后 `torch.save`——换词器后这里会重新执行并覆盖写新缓存。
- [examples/toy_train.py:L59](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L59)（Newton-Schulz 的 `assert len(G.shape) == 2`）与 [L136](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L136)（`assert p.ndim == 2`）：与 vocab 无关，但提醒我们 Muon 组只收二维矩阵——换小词表后嵌入矩阵变小，仍走 AdamW 分支，分组逻辑（u2-l1）不变。

#### 4.3.4 代码实践

**实践目标**：体验「换词表」两种方向的不对称失败/浪费。

**操作步骤**（示例代码，改在复制的脚本上）：

```python
# 示例代码：把 tokenizer 分支换成 GPT-2（词表 50257）
from transformers import AutoTokenizer
tokenizer = AutoTokenizer.from_pretrained("gpt2")
```

1. 先不删缓存、不改 `vocab_size` 直接跑：观察它「正常」运行——但喂进模型的是 Qwen 分词器缓存的旧 token 流（错误示范）。
2. `rm openwebtext-100k.bin`，仍不改 `vocab_size=151936`：重新分词后能正常训练（50257 < 151936 不越界），这是「能跑但浪费」——嵌入有约 10 万行死权重。
3. 再把 `vocab_size` 改为 50257，并按需调整 `bos/eos_token_id`（GPT-2 的 eos 为 50256）：这是干净对齐的版本，嵌入参数从 \( 151936 \times H \) 降到 \( 50257 \times H \)。

**需要观察的现象**：第 1 步不重分词、loss 与换词器前完全一致（证明缓存未失效）；第 2 步日志中初始 loss 仍约 \( \ln(151936) \approx 11.93 \)（u1-l2 的理论值），第 3 步初始 loss 应变为 \( \ln(50257) \approx 10.83 \)。

**预期结果**：初始 loss 的对数差恰好反映词表大小差异——这是「词表真的换掉了」最直接的信号。

**待本地验证**：GPT-2 分词器在此脚本下的具体 loss 数值（均匀初始分布下的理论值如上，实际初始化略有偏差）。

#### 4.3.5 小练习与答案

**练习 1**：为什么换分词器后不删 `.bin` 不会报错？这比报错更危险吗？

**答案**：因为缓存键只含数据集名（[L27](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L27)），旧 token id 都在合法范围内、形状也不变，于是静默地用「旧词表的语义 + 新模型的嵌入行」训练。比报错更危险——实验结论会悄悄失真。

**练习 2**：把 `vocab_size` 误设为 50000（小于 Qwen 词表 151936）会发生什么？

**答案**：分词后大量 id ≥ 50000，嵌入查表时 `index out of range` 崩溃（在第 2 步就会暴露，而非静默错误）。

### 4.4 参数量与吞吐评估

#### 4.4.1 概念说明

动手改模型之前先会「心算」模型，是快速实验的核心能力。记 \( H \) 隐层宽、\( I \) FFN 宽、\( L \) 层数、\( V \) 词表，Qwen2 单层参数（MHA、q/k/v 带 bias、o 与 MLP 不带 bias，SwiGLU）：

\[
N_{\text{layer}} = \underbrace{3(H^2 + H)}_{q,k,v\ \text{投影+bias}} + \underbrace{H^2}_{o\ \text{投影}} + \underbrace{3HI}_{\text{gate,up,down}} + \underbrace{2H}_{\text{两个 RMSNorm}}
\]

全模型（嵌入共享，lm_head 不另计）：

\[
N_{\text{total}} = V \times H + L \times N_{\text{layer}} + H_{\text{final norm}}
\]

两个常用口径：

- **吞吐**：每步固定消耗 `batch_size × max_length = 16 × 512 = 8192` 个 token（u3-l1），故 tokens/s \( = 8192 / t_{\text{step}} \)。
- **训练 FLOPs**：按 Kaplan 惯例 \( \text{FLOPs} \approx 6 N_{\text{non-embed}} D \)（前向 2 + 反向 4，乘非嵌入参数量与 token 数）。这是 u3-l1「阈值到达步数法」的理论底座。

#### 4.4.2 核心流程

```text
改配置 → 用公式心算 N_total（30 秒）
       → 跑通后用 sum(p.numel()) 对账（1 分钟）
       → 测 t_step → 换算 tokens/s 与 FLOPs/s
       → 对比 GPU 峰值算出 MFU（模型算力利用率）
```

#### 4.4.3 源码精读

先代入本脚本的两组配置做心算（纯算术，可手工验证）：

**默认模型**（\( H=1024, I=4864, L=12, V=151936 \)，对应 [L262-L279](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L262-L279) 的写死值 + CLI 默认）：

- 单层：\( 3 \times (1024^2 + 1024) + 1024^2 + 3 \times 1024 \times 4864 + 2 \times 1024 = 19{,}141{,}632 \)
- 嵌入：\( 151936 \times 1024 = 155{,}582{,}464 \)
- 总量：\( 155{,}582{,}464 + 12 \times 19{,}141{,}632 + 1024 \approx 385.3\,\text{M} \)，其中嵌入占约 40%

**实践小模型**（\( H=512, I=1536, L=6 \)）：

- 单层：\( 3 \times (512^2 + 512) + 512^2 + 3 \times 512 \times 1536 + 2 \times 512 = 3{,}410{,}432 \)
- 嵌入：\( 151936 \times 512 = 77{,}791{,}232 \)
- 总量：\( 77{,}791{,}232 + 6 \times 3{,}410{,}432 + 512 = 98{,}254{,}336 \approx 98.3\,\text{M} \)，**嵌入占约 79%**

这解释了 README 性能表脚注（[README.md:L65](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L65)）为什么强调参数统计排除 embedding：玩具尺度下嵌入一家独大，含嵌入的「参数量」几乎不能反映模型能力。把小模型数据套进 FLOPs 公式：每步 \( \approx 6 \times 20.46\,\text{M} \times 8192 \approx 1.0\,\text{TFLOPs} \)（默认模型约 11.3 TFLOPs/步）。

结构上的交叉验证（呼应 u2-l1）：每层 7 个二维矩阵（q/k/v/o/gate/up/down），12 层 → Muon 组 84 个参数；6 层 → 42 个，AdamW 组从 26 个（1 嵌入 + 25 个 norm 向量）降到 14 个（1 + 13）。改层数时可以直接用 \( 7L \) 与 \( 2L+1 \) 预报分组数目。

#### 4.4.4 代码实践

**实践目标**：写一个 10 行的「对账 + 计时」仪表（u1-l3 仪表盘的扩展）。

**操作步骤**（示例代码，加在模型创建之后、训练循环之前；计时加在循环内）：

```python
# 示例代码：参数对账
n_embed   = model.model.embed_tokens.weight.numel()
n_total   = sum(p.numel() for p in model.parameters())
print(f"total={n_total:,}  embed={n_embed:,}  "
      f"non-embed={n_total - n_embed:,}  embed%={n_embed/n_total:.1%}")
# 期望（6 层 / 512 / 1536）：total=98,254,336  embed=77,791,232

# 示例代码：每步计时（放在 for step, batch in enumerate(train_loader) 内）
import time
if step == 0:
    torch.cuda.synchronize() if torch.cuda.is_available() else None
    t0 = time.perf_counter()
if step == 20:  # 跳过前几步的编译/预热开销
    torch.cuda.synchronize() if torch.cuda.is_available() else None
    t_step = (time.perf_counter() - t0) / 20
    print(f"tokens/s={8192/t_step:,.0f}  "
          f"train FLOPs/s≈{6*(n_total-n_embed)*8192/t_step/1e12:.2f}T")
```

**需要观察的现象**：`total` 与公式逐位一致（若差几十个，多半是 q/k/v 的 bias 项没算进去）；`tokens/s` 与 FLOPs/s 随 `--optimizer` 不同略有差异（Muon 的 Newton-Schulz 有额外矩阵乘开销，见 u2-l2）。

**预期结果**：小模型每步 FLOPs 理论值约 1.0 TFLOPs；用实测 FLOPs/s 除以 GPU 峰值即得 MFU——玩具脚本的 MFU 通常很低（无混合精度、无梯度检查点优化），这本身就是很好的观察点。

**待本地验证**：具体 tokens/s 与 MFU 数值依赖本机 GPU。

#### 4.4.5 小练习与答案

**练习 1**：默认模型（385M 参数，含嵌入）每步消耗多少训练 FLOPs？

**答案**：\( 6 \times (385.3\,\text{M} - 155.6\,\text{M}) \times 8192 \approx 6 \times 229.7\,\text{M} \times 8192 \approx 11.3\,\text{TFLOPs} \)。注意 \( N \) 用非嵌入口径。

**练习 2**：为什么缩小模型后嵌入占比反而从 40% 升到 79%？

**答案**：嵌入 \( VH \) 只随 \( H \) 线性缩小一次，而层参数近似随 \( H^2 \)（注意力）与 \( HI \)、\( L \) 缩小了多倍——词表 \( V \) 没变，嵌入成为愈发显眼的大块头。换小词表（4.3）是给它「减肥」的正道。

**练习 3**：用本讲公式验证 README「Moonlight 与 DSV2-Lite 同为 15.29B 总参数」这一对照是否公平。

**答案**：公平——两者总参数、激活参数、训练 token 数（5.7T）与词表口径都相同（[README.md:L47-L53](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L47-L53)），唯一主要变量是优化器（AdamW vs Muon），这正是论文对照实验的设计（u1-l1）。

## 5. 综合实践

**任务**：把玩具模型缩成「6 层 / hidden 512 / FFN 1536」的小模型，完成一次带测量报告的完整训练，再换词表修一遍对齐流程。

**步骤**：

1. **复制脚本**：`cp examples/toy_train.py examples/toy_train_small.py`（保留原脚本作为基线，u3-l5 还要用）。
2. **改配置**：在 `toy_train_small.py` 的 Qwen2Config 里改三处——`num_hidden_layers=12→6`（[L269](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L269)）、`intermediate_size=4864→1536`（[L264](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L264)）；`hidden_size` 用命令行 `--hidden_size 512` 传入。
3. **训练两种优化器**：
   ```bash
   python3 examples/toy_train_small.py --optimizer adamw --hidden_size 512 --lr 1e-3
   python3 examples/toy_train_small.py --optimizer muon  --hidden_size 512 --lr 1e-3
   ```
4. **对账**：加入 4.4.4 的参数统计代码，核对 `total=98,254,336`、`embed=77,791,232`、分组数 Muon 42 / AdamW 14。
5. **计时**：加入 4.4.4 的每步计时，记录两种优化器的 tokens/s。
6. **换词表**：按 4.3.4 把分词器换成 GPT-2——依次完成「删缓存 → 改 vocab_size=50257 → 改 eos_token_id=50256」，重跑并确认初始 loss 从约 11.93 降到约 10.83。
7. **写报告**（建议格式）：

| 配置 | 总参数 | 非嵌入参数 | tokens/s (adamw) | tokens/s (muon) | 初始 loss | 100 步后窗口 loss |
|---|---|---|---|---|---|---|
| 默认 12L/1024/4864/Qwen | （待填） | 229.7M | （待填） | （待填） | ~11.93 | （待填） |
| 小模型 6L/512/1536/Qwen | 98.3M | 20.5M | （待填） | （待填） | ~11.93 | （待填） |
| 小模型 + GPT-2 词表 | （待填） | （待填） | （待填） | （待填） | ~10.83 | （待填） |

**预期结果**：小模型每步明显更快、loss 下降起点相同（词表不变时初始 loss 只由 vocab_size 决定）；换 GPT-2 词表后参数量下降约 \( (151936-50257)\times 512 \approx 52\,\text{M} \)。所有「待填」项依赖本机算力与随机性，属**待本地验证**。

**思考题**（选做）：小模型上 Muon 相对 AdamW 的优势幅度，与默认模型相比变大了还是变小了？这与你对「Muon 收益随规模增长」的预期一致吗？（提示：结合 u3-l1 的效率度量方法设计判断标准。）

## 6. 本讲小结

- 模型来自「图纸」而非权重：`Qwen2Config(...)` → `Qwen2ForCausalLM(config)` 随机初始化（float32），`torch_dtype` 只是元数据；22 个字段可分结构 / 数值 / 杂项三类，只有结构字段改变参数量。
- 规模三旋钮 \( H/L/I \) 中只有 `hidden_size` 暴露为 CLI（[L325](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L325)），改层数与 FFN 宽度需编辑脚本；硬约束是 \( H \) 整除头数 16、窗口 512 ≤ 位置上限 513。
- `tie_word_embeddings=True` 共享嵌入与 lm_head，省 \( V \times H \) 参数，也是 u2-l1 中 lm_head 过滤为防御性代码的原因。
- 分词器与模型靠 `vocab_size` 握手：词表大于它会崩溃、小于它则静默浪费；换分词器必须删 `{dataset}.bin` 缓存（缓存键不含分词器），并同步更新 `vocab_size` 与 `bos/eos_token_id`。
- 参数量可用心算公式 \( N = VH + L(3(H^2{+}H) + H^2 + 3HI + 2H) \) 预报：默认模型约 385M（嵌入 40%），6L/512/1536 小模型约 98.3M（嵌入 79%）——嵌入主导是玩具模型的常态，也是论文口径排除 embedding 的原因。
- 改形状做实验在 Moonlight 里格外安全：u2-l4 的形状自适应学习率让任何 \( H \) 下的更新 RMS 都自动一致，无需按形状重调 Muon 学习率。

## 7. 下一步学习建议

- **u3-l1（对比实验）**：用本讲的仪表（参数对账 + tokens/s + MFU）重新武装你的优化器对比报告，把「效率」从步数口径升级到 FLOPs 口径。
- **u3-l3（推理）**：体验另一条路径——`from_pretrained` 加载 Moonlight-16B-A3B 真权重，对比本讲的「配置构造」路线在 dtype、设备分配上的差异。
- **u3-l5（二次开发）**：把本讲的「复制脚本再改」升级为规范的扩展点改造（新增优化器分支、接入新数据集），并建立与基线的回归对照。
- **源码延伸**：对照 transformers 4.49.0 中 `models/qwen2/configuration_qwen2.py` 与 `modeling_qwen2.py`，验证本讲关于 head_dim 断言、q/k/v bias、RoPE 缓存重算的三处「待本地验证」。
