# 使用 Moonlight-16B-A3B 进行推理

## 1. 本讲目标

u1、u2 两个单元以及 u3 的前两讲，我们一直在「训练侧」读代码：数据管线、优化器、玩具模型配置。本讲切换到「使用侧」——把 Moonlight 真正训练出来的 16B-A3B 权重加载起来，跑一次推理。这一侧的「源码」不再是 `examples/toy_train.py`，而是 README 中的官方推理代码，以及权重仓库里随模型发布的配置与建模代码。

学完本讲，你应该能够：

1. 说清楚 Moonlight 发布了哪两个官方权重（base 与 Instruct）、各自适合什么用法，并能从 README 的下载表定位到 HuggingFace 仓库。
2. 掌握 `AutoModelForCausalLM.from_pretrained` 加载 Moonlight 的完整流程，说清楚 `torch_dtype="auto"`、`device_map="auto"`、`trust_remote_code=True` 三个参数各自做什么，以及为什么 Moonlight **必须** `trust_remote_code=True`。
3. 理解 chat template 的机制：Instruct 模型的对话格式如何由 `tokenizer_config.json` 中的一段 Jinja 模板定义、`add_generation_prompt=True` 追加了什么、生成在哪里停止。
4. 会给 16B 模型算「显存账」：bf16 权重约 32 GB，MLA 压缩后的 KV cache 每 token 约 30 KB；据此判断自己的显卡能否跑、跑不动时有什么退路。
5. 了解 Moonlight 与 DeepSeek-V3 同构带来的部署便利（vLLM / SGLang 可直接服务），以及中间检查点（intermediate checkpoints）的发布情况与查阅方式。

## 2. 前置知识

本讲默认你已完成 u1 单元（尤其是 u1-l1 项目总览）。用到的已有概念快速回顾：

- **Moonlight 是什么**（u1-l1）：用 Muon 优化器、5.7T tokens 训练的 3B/16B MoE 模型，与 DeepSeek-V2-Lite 同规模同数据量，MMLU 70.0 对 58.3（[README.md:47-65](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L47-L65)）。
- **MoE**：每个 token 只激活一小部分专家，因此「总参数 16B、激活参数约 3B」——推理时占显存按 16B 算，算力按约 3B 算。
- **仓库结构**（u1-l1）：本仓库的核心资产是 Moonlight.pdf（技术报告）、README、`examples/toy_train.py`（训练示例）与 Muon 优化器代码；**模型权重不在本仓库**，存放在 HuggingFace。

本讲的新术语：

- **预训练（base）模型 vs 指令微调（Instruct）模型**：base 模型只学过「续写文本」，给定 `"1+1=2, 1+2="` 会续写 `3`；Instruct 模型在 base 之上做过指令微调（SFT，Supervised Fine-Tuning），学会了按「对话格式」回答问题。两者的正确用法不同（4.3 节展开）。
- **tokenizer（分词器）**：把文本切成 token id 序列。Moonlight 使用 TikToken 系分词器（与 Kimi 系列同源），词表大小 163840（约 16 万）。
- **safetensors**：HuggingFace 推荐的安全权重格式（按张量名偏移索引，不做任意代码反序列化），Moonlight 权重分片以 `.safetensors` 发布。
- **trust_remote_code（远端代码）**：允许 `from_pretrained` 下载并**执行**模型仓库里附带的 Python 代码（模型结构、分词器实现）。这是本讲的一个重要概念，4.2 节详细讲。
- **auto_map**：`config.json` 里的一个字段，把 transformers 的自动类（`AutoConfig` / `AutoModelForCausalLM` / `AutoTokenizer`）映射到仓库内自定义代码文件中的类——它是 `trust_remote_code` 的实现载体。
- **device_map（设备映射）**：把模型的不同层放到不同设备（GPU/CPU）的方案；`device_map="auto"` 由 accelerate 库按各卡显存自动切分。
- **torch_dtype**：权重加载后的数值精度。`torch_dtype="auto"` 表示沿用 `config.json` 里 `torch_dtype` 字段指定的精度（Moonlight 是 `bfloat16`，每个参数 2 字节）。
- **自回归生成与 KV cache**：`model.generate` 每步用已生成的全部历史 token 预测下一个 token；KV cache 把历史 token 的 Key/Value 缓存起来避免重复计算。Moonlight 沿用 DeepSeek-V3 的 MLA（Multi-head Latent Attention），把 KV 压缩到很低维度，cache 极小（4.2 节算账）。
- **chat template（对话模板）**：一段 Jinja2 模板字符串，存于 `tokenizer_config.json`，把 `[{role, content}, ...]` 的消息列表渲染成模型训练时见过的对话文本格式。
- **量化（quantization）**：把 bf16 权重压到 4-bit 等更低精度以省显存的技术（如 bitsandbytes 的 NF4），是小显存跑大模型的常见退路。

关于运行环境，两处官方信息要分清：

- README 推理环境（[README.md:82](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L82)）：python=3.10、torch>=2.1.0、**transformers=4.48.2**；
- 仓库 `requirements.txt` 是**训练侧**依赖（[requirements.txt:1-6](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/requirements.txt#L1-L6)：torch 2.6.0、transformers 4.49.0 等），且**不含 accelerate**——而 `device_map="auto"` 需要 accelerate。做推理时建议独立建环境：`transformers`、`accelerate`（必装）、`bitsandbytes`（仅量化时需要）。

## 3. 本讲源码地图

| 文件 | 本讲关注点 | 关键位置 |
|---|---|---|
| `README.md` | 模型下载表（两个权重仓库）；官方推理代码（base 与 Instruct 两段）；推荐环境；vLLM/SGLang 生态说明；中间检查点章节 | L69-78（下载表）、L82（环境）、L84-102（base 推理）、L104-126（Instruct 推理）、L128（引擎生态）、L139-140（中间检查点） |
| [Moonlight_intermediate_checkpoints.pdf](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/Moonlight_intermediate_checkpoints.pdf) | 中间检查点的官方发布说明（于 2025-02-28、commit `9c7a5a9` 单独加入仓库）。讲义编写环境无法提取其文本，具体清单在本文中标注「待确认」，请以 PDF 原文与 HuggingFace 页面为准 | 全文 |

此外，本讲需要「外读」两份**不在本 GitHub 仓库里**、但随模型发布在 HuggingFace 仓库中的文件（下文引用时均注明为外部资源）：

- [moonshotai/Moonlight-16B-A3B-Instruct 的 config.json](https://huggingface.co/moonshotai/Moonlight-16B-A3B-Instruct/blob/main/config.json)——模型结构超参，是 `trust_remote_code` 与「DeepSeek-V3 同构」的直接证据；
- [同仓库的 tokenizer_config.json](https://huggingface.co/moonshotai/Moonlight-16B-A3B-Instruct/blob/main/tokenizer_config.json)——特殊 token 表与 chat template 原文。

这两份文件会随官方更新而变化（不像本仓库 permalink 锁定在 HEAD），引用内容以本讲写作时点为准。

## 4. 核心概念与源码讲解

### 4.1 模型下载：两个官方权重仓库

#### 4.1.1 概念说明

Moonlight 对外发布两个权重仓库，都在 HuggingFace 的 `moonshotai` 组织下：

| 仓库 | 性质 | 正确用法 |
|---|---|---|
| `moonshotai/Moonlight-16B-A3B` | 预训练（base）模型，Muon + 5.7T tokens 训练的直接产物 | 文本续写、少样本模式、作为继续预训练/自行微调的起点 |
| `moonshotai/Moonlight-16B-A3B-Instruct` | 在 base 之上做过指令微调的对话模型 | 套 chat template 做问答助手（本讲综合实践用它） |

命名里的 **16B-A3B**：「16B」指总参数量（权重占显存按它算），「A3B」指每个 token 激活的参数量约 3B（前向计算量按它算）。这是 MoE 特有的「容量与算力解耦」：显存吃 16B 的，速度接近 3B 的。

一个容易困惑的数字问题：README 的性能对照表里写 Moonlight「Total Params 15.29B」「Activated Param 2.24B」，与「16B / A3B」名字对不上。其实表格脚注已说明口径——参数统计**不含嵌入（embedding）参数**（[README.md:65](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L65)）。拿 `config.json` 的数字补上嵌入就能对齐（详见 4.1.3 的算术）：15.29B + 嵌入相关的约 0.67B ≈ 15.96B ≈ 16B；同理 2.24B 是不含嵌入的激活量，名字里的 A3B 是含嵌入口径的名义值（此口径解释为合理推断，待确认）。

每个权重仓库里不只有权重分片（safetensors），还有一组**代码文件**：`config.json`、`configuration_deepseek.py`、`modeling_deepseek.py`、`tokenization_moonshot.py`、`tokenizer_config.json` 等。模型结构代码随权重走——这是 4.2 节 `trust_remote_code` 的前提。

#### 4.1.2 核心流程

以 `from_pretrained("moonshotai/Moonlight-16B-A3B-Instruct")` 为例，下载与解析的时序：

1. 按 repo id 向 HuggingFace Hub 解析仓库，先取 `config.json`；
2. 读到 `auto_map` 字段 → 知道结构定义在仓库内的 `modeling_deepseek.py` 等文件中（需 `trust_remote_code=True` 才会下载执行）；
3. 下载 tokenizer 相关文件（`tokenization_moonshot.py`、`tokenizer_config.json` 等）与权重分片（多个 `.safetensors`，合计约 32 GB，见 4.2 的账本）；
4. 所有文件缓存在本机 `~/.cache/huggingface/hub/`（可通过 `HF_HOME` 环境变量改位置），第二次加载直接命中缓存；
5. 若想先下载后离线使用，可用 `huggingface-cli download` 提前拉取整个仓库或指定文件。

#### 4.1.3 源码精读

**下载表：两个权重的官方入口。** [README.md:69-78](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L69-L78) 给出 Model Download 表格：两行分别是 Moonlight（base，L75）与 Moonlight-Instruct（L76），规格完全相同——总参数 16B、激活 3B、上下文 8K，各配一个 HuggingFace 链接。注意「Context Length 8K」：`config.json` 中 `max_position_embeddings=8192`，**提示 + 生成加起来不能超过 8192 token**，这决定了 `max_new_tokens` 的上限要给提示留余量。

**权重规模数字的出处。** [README.md:47-51](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L47-L51) 的对照表列出 Moonlight 与 DSV2-Lite 同为 2.24B 激活 / 15.29B 总量（同构模型，口径一致）；[L65](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L65) 的脚注 `†The reported parameter counts exclude the embedding parameters.` 说明了不含嵌入的口径。

**用 config.json 把 15.29B 补成 16B（外部资源，非本仓库文件）。** [config.json](https://huggingface.co/moonshotai/Moonlight-16B-A3B-Instruct/blob/main/config.json) 中关键超参：

| 字段 | 值 | 含义 |
|---|---|---|
| `architectures` | `["DeepseekV3ForCausalLM"]` | 模型类名——DeepSeek-V3 的类 |
| `model_type` | `"deepseek_v3"` | 模型类型标识 |
| `hidden_size` / `num_hidden_layers` | 2048 / 27 | 隐层宽度 / 层数 |
| `vocab_size` | 163840 | 词表约 16 万 |
| `n_routed_experts` / `n_shared_experts` / `num_experts_per_tok` | 64 / 2 / 6 | 每层 64 个路由专家 + 2 个共享专家，每 token 选 6 个路由专家 |
| `first_k_dense_replace` | 1 | 前 1 层用稠密 FFN，其余 26 层是 MoE |
| `moe_intermediate_size` / `intermediate_size` | 1408 / 11264 | 专家 FFN 宽度 / 稠密层 FFN 宽度 |
| `kv_lora_rank` / `qk_rope_head_dim` | 512 / 64 | MLA 压缩 KV 维度（4.2 算 KV cache 账要用） |
| `max_position_embeddings` | 8192 | 上下文 8K |
| `tie_word_embeddings` | false | 输出头与嵌入**不**共享（各占一份参数） |
| `torch_dtype` | `"bfloat16"` | `torch_dtype="auto"` 最终加载成 bf16 的依据 |

嵌入参数量：\(163840 \times 2048 \approx 3.36\times10^8\)。`tie_word_embeddings=false` 意味着输入嵌入与输出头各一份，合计

\[
15.29\,\text{B} \;+\; 2 \times 0.336\,\text{B} \;\approx\; 15.96\,\text{B} \;\approx\; 16\,\text{B}
\]

正好对上「16B」的命名。这张表同时也是 4.4 节「与 DeepSeek-V3 同构」的直接证据。

#### 4.1.4 代码实践

**实践目标**：不下载约 32 GB 的权重，只拉取并检视配置文件，亲眼确认 `architectures`、`auto_map` 与 MoE 超参（为 4.2 的 `trust_remote_code` 埋好证据）。

**操作步骤**（示例命令，两个模型均为公开 MIT 权重，无需 token；待本地验证）：

```bash
# 示例命令：只下载指定小文件到本地 HF 缓存
huggingface-cli download moonshotai/Moonlight-16B-A3B-Instruct \
  --include "config.json" "tokenizer_config.json" "generation_config.json"
```

或用 Python（示例代码）：

```python
# 示例代码：仅拉取 config.json 并打印关键字段
from huggingface_hub import hf_hub_download
import json

path = hf_hub_download("moonshotai/Moonlight-16B-A3B-Instruct", "config.json")
cfg = json.load(open(path))
for k in ["architectures", "model_type", "auto_map", "torch_dtype",
          "n_routed_experts", "n_shared_experts", "num_experts_per_tok",
          "num_hidden_layers", "max_position_embeddings", "tie_word_embeddings"]:
    print(f"{k} = {cfg.get(k)}")
```

**需要观察的现象**：`architectures` 是 `DeepseekV3ForCausalLM`；存在 `auto_map` 字段且指向 `configuration_deepseek` / `modeling_deepseek` 模块；`torch_dtype` 为 `bfloat16`。

**预期结果**：控制台逐行打印上表中的值；缓存目录（默认 `~/.cache/huggingface/hub/models--moonshotai--Moonlight-16B-A3B-Instruct/`）下出现 `snapshots/<hash>/config.json`。若想核对 `auto_map` 的完整内容，直接打开该缓存文件即可。（具体打印格式待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：为什么要发布 base 和 Instruct 两个仓库，而不是只发一个？
**答案**：两者的能力与用途不同。base 是预训练直接产物，只会「续写」，适合做续写/少样本实验、复现论文基准、或作为自定义微调的起点；Instruct 在其上做了指令微调，按对话格式作答，适合直接当助手用。给 base 套 chat template 属于分布外输入，效果差；拿 Instruct 做续写式 few-shot 也不再是它的最优用法。分开发布让两类用户各取所需。

**练习 2**：README 下载表写 16B，性能表写 15.29B，矛盾吗？
**答案**：不矛盾。性能表有脚注注明统计**不含嵌入参数**；用 `config.json` 的 `vocab_size=163840`、`hidden_size=2048` 算出嵌入约 0.336B，又因 `tie_word_embeddings=false` 输出头再占 0.336B，15.29 + 0.67 ≈ 15.96B ≈ 16B。16B 是含嵌入的名义总量，15.29B 是不含嵌入的严格口径。

**练习 3**：`max_position_embeddings=8192` 对生成调用意味着什么？
**答案**：提示 token 数 + `max_new_tokens` 不能超过 8192。README 的 Instruct 示例用 `max_new_tokens=500`（[README.md:123](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L123)），对几十字的提示而言远在限制之内；若构造超长提示，需相应调小 `max_new_tokens`，否则可能报位置越界或被截断。

### 4.2 transformers 推理：三个关键参数与显存账本

#### 4.2.1 概念说明

`AutoModelForCausalLM.from_pretrained(model_path, torch_dtype="auto", device_map="auto", trust_remote_code=True)` 这一行里有三个必须吃透的参数：

1. **`trust_remote_code=True`**：transformers 内置支持的模型结构是有限的。Moonlight 发布时，`deepseek_v3` 结构尚未（或刚）进入 transformers 主干，因此官方把 `modeling_deepseek.py`（模型定义）、`configuration_deepseek.py`（配置类）、`tokenization_moonshot.py`（分词器）随权重放在模型仓库里，并在 `config.json` 的 `auto_map` 中登记：

   ```json
   "auto_map": {
     "AutoConfig": "configuration_deepseek.DeepseekV3Config",
     "AutoModelForCausalLM": "modeling_deepseek.DeepseekV3ForCausalLM"
   }
   ```

   （外部资源：[config.json](https://huggingface.co/moonshotai/Moonlight-16B-A3B-Instruct/blob/main/config.json)）`trust_remote_code=True` 就是授权 transformers 下载并执行这些远端 Python 文件。**注意分词器也一样**：`tokenizer_config.json` 里 `tokenizer_class` 是自定义的 `TikTokenTokenizer`，且有自己的 `auto_map`，所以 [README.md:95](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L95) 加载分词器时同样传了 `trust_remote_code=True`。安全提醒：这等于执行别人的代码，**只对可信来源开启**；Moonlight 官方仓库属于可信来源。

2. **`torch_dtype="auto"`**：按 `config.json` 的 `torch_dtype`（`bfloat16`）加载。若不传，老版本 transformers 默认以 float32 加载——参数量翻 4 倍字节数，约 64 GB，单卡直接爆显存。这个参数实质上是显存第一道闸门。

3. **`device_map="auto"`**：由 accelerate 按各 GPU 的空闲显存自动决定每层放哪张卡（放不下时溢出到 CPU，但会极慢）。单卡也建议写上——它会自动把整个模型搬进显存。多卡小显存场景（如 2×48GB）靠它自动层间切分。

生成侧的三个概念：`model.generate(..., max_new_tokens=N)` 自回归地逐 token 生成直到产出终止符（eos）或达到 N；KV cache 让历史 token 的 K/V 不必重算；`tokenizer.batch_decode` 把 id 序列还原成文本（默认**包含提示本身**且**保留特殊 token**，见 4.3.3 的观察点）。

#### 4.2.2 核心流程

加载与一次生成的全过程：

```
from_pretrained(model_path, torch_dtype="auto", device_map="auto", trust_remote_code=True)
   ├─ 拉 config.json → 读到 auto_map（需要远端代码）
   ├─ 下载并 import modeling_deepseek.py / configuration_deepseek.py
   ├─ 按 config 构造 DeepseekV3ForCausalLM 空模型
   ├─ 下载 safetensors 分片 → 以 bfloat16 载入权重
   └─ accelerate 按 device_map="auto" 把各层放到 GPU
tokenizer(prompt) → input_ids
model.generate(input_ids, max_new_tokens=N)
   └─ 循环：前向（含 KV cache 增量）→ 采样下一个 id → 追加 → 遇 eos 停止
tokenizer.batch_decode → 文本
```

**显存账本**（估算，单位按 1 GB = 10⁹ 字节）：

权重大头：

\[
\text{权重显存} \;\approx\; N_{\text{total}} \times b
\;=\; 15.96\times10^9 \times 2\,\text{B} \;\approx\; 31.9\,\text{GB}
\]

其中 \(b\) 是每参数字节数（bf16 为 2）。

KV cache 这一项，Moonlight 因 MLA 而极小：每 token 每层只需缓存压缩后的潜在向量与 RoPE 部分，维度为 \(512 + 64 = 576\)（按 DeepSeek-V3 建模代码缓存 `kv_lora_rank + qk_rope_head_dim` 的惯例估算），于是

\[
576 \times 2\,\text{B} \times 27\,\text{层} \approx 30.4\,\text{KB/token}
\quad\Rightarrow\quad
8192\,\text{token} \approx 249\,\text{MB}
\]

即满 8K 上下文的 KV cache 也不到 0.3 GB——这是 MLA 相传统多头注意力量级级的节省（未计入反量化缓冲与激活，待本地验证）。

由此得出可行的硬件配置（权重 31.9 GB + 激活/缓存/框架开销）：

| 硬件 | bf16 直接加载 | 说明 |
|---|---|---|
| 1×80 GB（A100/H100） | ✅ 宽裕 | 官方代码原样可跑 |
| 2×48 GB | ✅ | `device_map="auto"` 自动切分 |
| 4×24 GB（3090/4090） | ✅ | 同上，跨卡通信慢一些 |
| 1×24 GB | ❌ bf16 / ✅ 4-bit | 权重压到约 8 GB（\(15.96\text{B} \times 0.5\,\text{B}\)），用 bitsandbytes NF4（待本地验证） |
| 1×32 GB | 边界 | 权重 31.9 GB 几乎占满，激活放不下，实际不可行或需量化 |

#### 4.2.3 源码精读

**base 模型的官方推理代码。** [README.md:84-102](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L84-L102)：

```python
model_path = "moonshotai/Moonlight-16B-A3B"                    # L88
model = AutoModelForCausalLM.from_pretrained(
    model_path,
    torch_dtype="auto",                                        # L91
    device_map="auto",                                         # L92
    trust_remote_code=True,                                    # L93
)
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)   # L95

prompt = "1+1=2, 1+2="                                         # L97
inputs = tokenizer(prompt, return_tensors="pt",
                   padding=True, truncation=True).to(model.device)   # L98
generated_ids = model.generate(**inputs, max_new_tokens=100)   # L99
response = tokenizer.batch_decode(generated_ids)[0]            # L100
```

逐点说明：

- **L88**：用 repo id 直接收 base 模型；也可以换成 `huggingface-cli download` 后的本地路径。
- **L89-94**：4.2.1 讲透的三个参数。
- **L97-98**：base 模型**不套对话模板**，直接给一段「示例在前、待续在后」的提示——`1+1=2, 1+2=` 就是让模型续写 `3`。`padding=True, truncation=True` 为批量推理预留（单条字符串其实用不上）。`.to(model.device)` 把输入搬到模型所在设备——`device_map="auto"` 下模型可能分散在多卡，输入需与首层设备一致。
- **L99**：`**inputs` 把 `input_ids`、`attention_mask` 一起展开传入；`max_new_tokens=100` 续写上限。
- **L100**：`batch_decode(generated_ids)[0]` 解码的是**提示 + 生成**的完整序列，且默认保留特殊 token——所以打印出来的第一段就是你的提示本身，不是模型「复述」了问题。

**Instruct 模型的官方推理代码。** [README.md:104-126](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L104-L126)（完整引用与逐行讲解在 4.3.3，两段的差异也在 4.3.3 列表对比）。环境建议见 [README.md:82](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L82)。

#### 4.2.4 代码实践

**实践目标**：在 base 模型上复现 README 的续写示例，顺带记录显存峰值，验证 4.2.2 的账本。

**操作步骤**（示例代码，保存为独立脚本 `infer_base.py`，不改动仓库源码；需一张 ≥80 GB 的卡，或按 4.2.2 表改为多卡/量化）：

```python
# 示例代码：base 模型续写 + 显存峰值测量
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

model_path = "moonshotai/Moonlight-16B-A3B"
model = AutoModelForCausalLM.from_pretrained(
    model_path, torch_dtype="auto", device_map="auto", trust_remote_code=True,
)
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)

prompt = "1+1=2, 1+2="
inputs = tokenizer(prompt, return_tensors="pt",
                   padding=True, truncation=True).to(model.device)
generated_ids = model.generate(**inputs, max_new_tokens=100)
print(tokenizer.batch_decode(generated_ids)[0])

if torch.cuda.is_available():
    for i in range(torch.cuda.device_count()):
        print(f"cuda:{i} 峰值显存 = "
              f"{torch.cuda.max_memory_allocated(i)/1e9:.1f} GB")
```

**需要观察的现象**：首次运行会先下载数十 GB 权重（耐心+磁盘）；生成文本以 `3` 开头继续算术续写；解码结果开头是你给的提示原文；打印的峰值显存与 31.9 GB + 开销的估算对照。

**预期结果**：续写出一串算术续写式文本（base 模型不会「回答问题」，只会顺着写，具体内容待本地验证）；单 80 GB 卡峰值显存约 33-36 GB（待本地验证）。**若没有大显存设备**：完成本脚本即可，转而做 4.3.4 的分词器实践与下面的估算练习——

**估算练习**：把 `torch_dtype` 换成 `float32`（即不传该参数时老版本的默认行为）需要多少权重显存？答：\(15.96 \times 4 \approx 63.8\) GB，80 GB 单卡放完权重后激活余量也很紧张——这就是该参数作为「显存第一道闸门」的意义。

#### 4.2.5 小练习与答案

**练习 1**：`trust_remote_code=True` 具体放行了什么？风险与对策是什么？
**答案**：放行 transformers 下载并执行模型仓库里的自定义 Python——由 `config.json` 的 `auto_map` 指定的 `configuration_deepseek.py`、`modeling_deepseek.py`，以及 `tokenizer_config.json` 指定的 `tokenization_moonshot.py`。风险是执行不可信代码（模型仓库被篡改即任意代码执行）。对策：仅对可信来源（如 Moonlight 官方 `moonshotai` 组织）开启；更高安全要求时可先人工审阅这些文件、固定 revision（commit hash）后再加载。

**练习 2**：去掉 `device_map="auto"` 会发生什么？
**答案**：模型不再自动放置——默认整体落在 CPU 上（显存够也不上卡），推理极慢；需手动 `model.to("cuda")`。多卡场景下手动切分也很繁琐。`device_map="auto"` 借助 accelerate 按空闲显存自动做层间切分，是 16B 模型在有限硬件上落位的正解。另注意它依赖 accelerate 库（不在本仓库 `requirements.txt` 中，需另装）。

**练习 3**：24 GB 单卡想跑 Moonlight，路线是什么？大致显存多少？
**答案**：4-bit 量化（如 `BitsAndBytesConfig(load_in_4bit=True, bfloat16)` 传入 `from_pretrained` 的 `quantization_config`）。权重大约 \(15.96\text{B} \times 0.5\,\text{B/参数} \approx 8\) GB，加上量化元数据、激活与 KV cache，24 GB 单卡可行（具体数值待本地验证）。代价是精度略降与量化/反量化开销，做评测或产品前应与 bf16 结果对比。

### 4.3 chat template：Instruct 模型的对话协议

#### 4.3.1 概念说明

Instruct 模型在微调阶段见到的输入不是裸文本，而是一种固定的对话格式：系统段、用户段、助手段各由特殊 token 包裹。**chat template 就是这种格式的机读定义**——一段存在 `tokenizer_config.json` 里的 Jinja2 模板，把 Python 侧的消息列表

```python
messages = [
    {"role": "system", "content": "..."},
    {"role": "user",   "content": "..."},
]
```

渲染成模型训练时见过的那种字符串。`tokenizer.apply_chat_template(messages, ...)` 一步完成「渲染 + 分词」，返回 `input_ids`。

理解 chat template 的三个关键点：

1. **格式即协议**。推理时的格式必须与微调时一致，否则是分布外输入，回答质量崩塌。这就是「为什么不能对 Instruct 模型直接 `tokenizer("问题")`」的原因，也是「为什么不能对 base 模型套模板」的原因（base 没见过这些特殊 token 的组合）。
2. **`add_generation_prompt=True`**：在渲染结果末尾追加「轮到助手说话」的前缀（`<|im_assistant|>assistant<|im_middle|>`），让模型从这里开始生成回答。多轮对话时把历史消息一起传入即可由模板展开。
3. **模板是数据不是代码**。它就写在 `tokenizer_config.json` 里，任何一个文本编辑器都能查看；换一家模型，模板就换一套特殊 token 与格式。

#### 4.3.2 核心流程

Moonlight 的模板（外部资源：[tokenizer_config.json](https://huggingface.co/moonshotai/Moonlight-16B-A3B-Instruct/blob/main/tokenizer_config.json)，原文为单行，此处加换行便于阅读）：

```jinja
{%- for message in messages -%}
  {%- if loop.first and messages[0]['role'] != 'system' -%}
    <|im_system|>system<|im_middle|>You are a helpful assistant<|im_end|>
  {%- endif -%}
  {%- if message['role'] == 'system' -%}<|im_system|>{%- endif -%}
  {%- if message['role'] == 'user' -%}<|im_user|>{%- endif -%}
  {%- if message['role'] == 'assistant' -%}<|im_assistant|>{%- endif -%}
  {{ message['role'] }}<|im_middle|>{{ message['content'] }}<|im_end|>
{%- endfor -%}
{%- if add_generation_prompt -%}<|im_assistant|>assistant<|im_middle|>{%- endif -%}
```

渲染规则逐条读：

- 每条消息：先放对应角色的特殊 token（`<|im_system|>` / `<|im_user|>` / `<|im_assistant|>`），再写角色名明文、`<|im_middle|>`（分隔符）、内容、`<|im_end|>`（消息结束）。
- 若首条消息不是 system，自动注入默认系统提示 `You are a helpful assistant`。
- `add_generation_prompt=True` 时在末尾追加助手前缀，模型由此开始生成。

特殊 token 与 id（同一文件中登记）：`<|im_end|>`=163586、`<|im_user|>`=163587、`<|im_assistant|>`=163588、`<|im_system|>`=163594、`<|im_middle|>`=163601；`[BOS]`=163584、`[EOS]`=163585、`[PAD]`=163838。

**生成在哪里停止**：`config.json` 的 `eos_token_id=163586`，即 `<|im_end|>`——模型生成完回答会输出该 token，`generate` 据此停止。注意 `tokenizer_config.json` 里 tokenizer 层的 `eos_token` 是 `[EOS]`（163585），与模型生成用的 eos 不是同一个：**控制生成停止的是模型 config 的 eos**。模板本身不含 `[BOS]`，加载后 `input_ids` 是否在最前面额外加 `[BOS]` 取决于自定义分词器实现（待确认，可在实践中打印首个 id 验证是否为 163584）。

#### 4.3.3 源码精读

**Instruct 模型的官方推理代码。** [README.md:104-126](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L104-L126)：

```python
model_path = "moonshotai/Moonlight-16B-A3B-Instruct"           # L109
model = AutoModelForCausalLM.from_pretrained(
    model_path,
    torch_dtype="auto",                                        # L111
    device_map="auto",                                         # L112
    trust_remote_code=True,                                    # L113
)
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)   # L116

messages = [
    {"role": "system",
     "content": "You are a helpful assistant provided by Moonshot-AI."},   # L119
    {"role": "user", "content": "Is 123 a prime?"},            # L120
]
input_ids = tokenizer.apply_chat_template(
    messages, add_generation_prompt=True, return_tensors="pt").to(model.device)   # L122
generated_ids = model.generate(inputs=input_ids, max_new_tokens=500)   # L123
response = tokenizer.batch_decode(generated_ids)[0]            # L124
```

与 base 段（L84-102）逐项对比——两段代码的差异就是「两种模型的用法差异」：

| 环节 | base（L84-102） | Instruct（L104-126） |
|---|---|---|
| model_path | `Moonlight-16B-A3B`（L88） | `Moonlight-16B-A3B-Instruct`（L109） |
| 输入构造 | 裸提示 + `tokenizer(...)`（L97-98） | 消息列表 + `apply_chat_template(...)`（L118-122） |
| 生成长度 | `max_new_tokens=100`（L99） | `max_new_tokens=500`（L123）——回答问题比续写算术需要更长预算 |
| generate 传参 | `**inputs`（含 attention_mask） | `inputs=input_ids`（L123） |

按 4.3.2 的模板手工渲染 L118-121 的消息（首条是 system，故不注入默认提示）：

```
<|im_system|>system<|im_middle|>You are a helpful assistant provided by Moonshot-AI.<|im_end|><|im_user|>user<|im_middle|>Is 123 a prime?<|im_end|><|im_assistant|>assistant<|im_middle|>
```

模型从末尾开始生成，直到产出 `<|im_end|>`（163586）停止。

**L124 的一个观察点**：`batch_decode` 默认 `skip_special_tokens=False`，且解码的是完整序列（提示 + 生成），所以 README 脚本打印的 `response` 里**能看到全部模板标记**——开头是 `<|im_system|>system<|im_middle|>...`。想要纯回答：

```python
# 示例代码：只取新生成部分并去掉特殊 token
new_ids = generated_ids[:, input_ids.shape[-1]:]               # 切掉提示
print(tokenizer.batch_decode(new_ids, skip_special_tokens=True)[0])
```

（示例代码，非 README 原文。）

#### 4.3.4 代码实践

**实践目标**：不加载 32 GB 权重、只用分词器，在 CPU 上完整跑通「消息 → 模板渲染 → token id」这一步，并核对 4.3.2 的手工渲染结果。这是**无 GPU 也能真实运行**的实践。

**操作步骤**（示例代码，保存为 `preview_chat_template.py`；分词器仅几 MB，任何笔记本可跑）：

```python
# 示例代码：仅加载分词器，预览 chat template 的渲染结果
from transformers import AutoTokenizer

model_path = "moonshotai/Moonlight-16B-A3B-Instruct"
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)

messages = [
    {"role": "system", "content": "You are a helpful assistant provided by Moonshot-AI."},
    {"role": "user", "content": "123 是质数吗？"},   # 综合实践要用的中文问题
]

# 1) 只渲染不切词：直接看模板输出
text = tokenizer.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)
print("=== 渲染结果 ===")
print(text)

# 2) 渲染并切词：拿到真正喂给模型的 id
ids = tokenizer.apply_chat_template(messages, add_generation_prompt=True,
                                    return_tensors="pt")
print("=== token 形状与首尾 id ===")
print(ids.shape)                 # 期望 [1, 序列长度]
print(ids[0, 0].item(), "开头（163594=<|im_system|>?）")
print(ids[0, -1].item(), "结尾（163601=<|im_middle|>?）")

# 3) 首条不写 system，看模板是否注入默认系统提示
ids2 = tokenizer.apply_chat_template([messages[1]], add_generation_prompt=True,
                                     tokenize=False)
print("=== 无 system 时的渲染 ===")
print(ids2)
```

**需要观察的现象**：

- 第 1 步打印的字符串应与 4.3.3 的手工渲染一致（仅用户内容换成中文问题）；
- 第 2 步首 id 应为 163594（`<|im_system|>`）、末 id 应为 163601（`<|im_middle|>`，生成前缀的末 token）——若首 id 是 163584（`[BOS]`），说明该分词器实现会自动补 BOS，正好回答 4.3.2 留的待确认问题；
- 第 3 步开头应出现 `You are a helpful assistant`（默认系统提示被注入）。

**预期结果**：三条全部符合即证明你对模板的理解正确；第 1、3 步的输出是确定性的（同一段 Jinja 模板），可直接与本文手工渲染对照；token 数量取决于中文问题的切词（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`add_generation_prompt=False` 会发生什么？
**答案**：模板不再追加 `<|im_assistant|>assistant<|im_middle|>`，序列停在用户消息的 `<|im_end|>` 之后。模型从这个位置「续写」的是下一条消息的开头，而不是以助手身份作答——它会自己再起一段（比如再写一个 `<|im_user|>` 之类的内容），得不到期望的回答。生成场景必须置 True。

**练习 2**：为什么 README 打印的 `response` 里能看到 `<|im_user|>` 这些标记？怎么拿到干净的回答？
**答案**：两个原因叠加——`batch_decode` 默认不跳过特殊 token，且 `generated_ids` 是「提示 + 生成」的完整序列。干净做法：先用 `generated_ids[:, input_ids.shape[-1]:]` 切掉提示部分，再 `batch_decode(..., skip_special_tokens=True)`。

**练习 3**：把 README 示例的 system 消息删掉，模型收到的输入里还有系统段吗？
**答案**：有。模板首段检测到「第一条消息不是 system」时会自动插入默认系统提示 `You are a helpful assistant`。所以删不删 system，格式都完整，只是系统内容从 Moonshot 版提示换成默认提示。

### 4.4 推理引擎生态与中间检查点

#### 4.4.1 概念说明

**同构带来的部署便利**。README 明说：「Moonlight has the same architecture as DeepSeek-V3, which is supported by many popular inference engines, such as VLLM and SGLang」。这句话的工程含义是：推理引擎按**结构**支持模型，而不是逐个品牌适配。Moonlight 的 `config.json` 里 `model_type="deepseek_v3"`、`architectures=["DeepseekV3ForCausalLM"]`，超参（MLA 的 `kv_lora_rank`、MoE 的 64 专家 top-6 等）全是 DeepSeek-V3 的字段——任何已经支持 DeepSeek-V3 的 vLLM / SGLang 版本，无需新写适配就能加载 Moonlight。

**两类推理栈的分工**：

| | transformers | vLLM / SGLang |
|---|---|---|
| 定位 | 研究与验证（与训练代码同栈，改得动） | 生产服务（高吞吐、高并发） |
| 关键技术 | 朴素 generate | 连续批处理、PagedAttention、张量并行等 |
| 适合场景 | 本讲的教学实验、评测脚本、魔改 | 对外提供 API、批量推理 |

**中间检查点（intermediate checkpoints）**。论文摘要承诺「We also release the pretrained, instruction-tuned, and intermediate checkpoints to support future research」（[README.md:19](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L19)）——除了首尾两个权重，还发布训练**过程中**的存档。对研究者，这是难得的资产：可以观察 Muon 训练动态（loss 轨迹、专家负载、benchmark 随 token 数的演化），做优化器分析、早停策略、模型合并等研究。

#### 4.4.2 核心流程

选择推理路径的决策树：

```
要跑 Moonlight 推理
├─ 目的 = 研究 / 教学 / 调试？
│    └─ transformers + trust_remote_code（本讲 4.2/4.3 的路径）
│        ├─ 显存 ≥ 32GB+开销 → bf16 原样加载
│        └─ 显存不足 → 4-bit 量化 或 多卡 device_map="auto"
└─ 目的 = 服务 / 批量生产？
     └─ vLLM / SGLang（DeepSeek-V3 支持已就绪）
         └─ 按引擎文档起服务，注意确认所装版本对 deepseek_v3 的支持

要研究训练过程？
└─ 查阅 Moonlight_intermediate_checkpoints.pdf 与 HuggingFace 页面
    （README L139-140 的 "Coming soon..." 文案滞后，以 PDF 与 HF 页面为准）
```

#### 4.4.3 源码精读

**同构声明的原文。** [README.md:128](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L128)：`Moonlight has the same architecture as DeepSeek-V3, which is supported by many popular inference engines, such as VLLM and SGLang. As a result, our model can also be easily deployed using these tools.`。它的证据链就是 4.1.3 引过的 config 字段：`model_type="deepseek_v3"` + `architectures=["DeepseekV3ForCausalLM"]`（外部资源：[config.json](https://huggingface.co/moonshotai/Moonlight-16B-A3B-Instruct/blob/main/config.json)）。

**中间检查点的发布状态：README 文案与仓库实物的「时间差」。** 这是 u1-l1 已建立、本讲从推理使用者角度再确认的一处细节：

- 承诺发布：[README.md:19](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L19)（摘要）；
- 章节文案：[README.md:139-140](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L139-L140)——`## Intermediate Checkpoints` / `To support ongoing research efforts, we will soon release our intermediate checkpoints. Coming soon...`；
- 仓库实物：[Moonlight_intermediate_checkpoints.pdf](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/Moonlight_intermediate_checkpoints.pdf) 已在仓库中，git 历史显示它于 2025-02-28 由 commit `9c7a5a9`（"add intermediate ckpts"）单独加入——即**发布说明已成文，README 章节文案却停留在 "Coming soon"**。

裁决方式（承接 u1-l1 的结论）：以 PDF 与 HuggingFace 页面为准，README 该段文案滞后。PDF 的具体内容（ checkpoint 的步数清单、对应的 HuggingFace 仓库名、获取方式）在讲义编写环境中无法提取文本，**待确认**——请直接打开 PDF 原文，或到 [`moonshotai` 组织页](https://huggingface.co/moonshotai) 检索。

**部署命令**：README 只给了 transformers 路线，未给 vLLM/SGLang 命令。以下为示例（具体参数以所装版本的官方文档为准，待本地验证）：

```bash
# 示例命令：vLLM 起服务（以 vLLM 官方文档为准）
pip install vllm
vllm serve moonshotai/Moonlight-16B-A3B-Instruct --trust-remote-code
```

```bash
# 示例命令：SGLang 起服务（以 SGLang 官方文档为准）
pip install "sglang[all]"
python -m sglang.launch_server --model-path moonshotai/Moonlight-16B-A3B-Instruct \
  --trust-remote-code
```

#### 4.4.4 代码实践

**实践目标**：确认本机推理引擎对 `deepseek_v3` 结构的支持情况；无法装引擎时做一份离线「同构核验」。

**操作步骤**（示例命令，任选其一）：

1. 引擎检查（有 GPU 环境）：

   ```bash
   # 示例命令：查看 vLLM 版本，并在其文档/源码中确认 deepseek_v3 支持
   python3 -c "import vllm; print(vllm.__version__)"
   ```

   然后在 vLLM（或 SGLang）的官方支持模型列表中查找 DeepSeek-V3——找到即等于找到 Moonlight 的部署路径。

2. 离线同构核验（无 GPU 也可完成）：从 HuggingFace 分别下载 Moonlight 与 `deepseek-ai/DeepSeek-V2-Lite` 的 `config.json`（后者结构与 Moonlight 最接近的上一代），逐字段比对 MoE 与 MLA 超参（`n_routed_experts`、`num_experts_per_tok`、`kv_lora_rank`、`qk_rope_head_dim` 等），填一张对照表。

**需要观察的现象**：路线 1 中版本号是否 ≥ 支持 DeepSeek-V3 的版本；路线 2 中两者的 `kv_lora_rank`/`qk_rope_head_dim` 等 MLA 字段是否一致、MoE 字段（专家数、top-k）是否一致或仅规模不同。

**预期结果**：路线 1 打印一个版本号，支持性以官方文档为准（待本地验证）；路线 2 得到一张字段对照表——Moonlight 与 DSV2-Lite 应呈现「同族不同规模」的模式（MLA 字段同、MoE 规模接近），而与 DeepSeek-V3 完全同 `model_type`（字段级一致性待确认，作为练习结论的一部分）。

#### 4.4.5 小练习与答案

**练习 1**：「与 DeepSeek-V3 同构」在配置文件里体现为什么？为什么这等于「vLLM/SGLang 开箱可用」？
**答案**：体现为 `model_type="deepseek_v3"` 与 `architectures=["DeepseekV3ForCausalLM"]`，以及整套 DeepSeek-V3 风格的 MLA/MoE 超参字段。推理引擎的适配工作是「按结构写一份建模与算子支持」，DeepSeek-V3 已被 vLLM/SGLang 支持，Moonlight 的权重与配置在该结构下即插即用，无需为「Moonlight」这个名字单独适配。

**练习 2**：什么场景该用 transformers，什么场景该用 vLLM/SGLang？
**答案**：transformers 适合研究、教学、调试与评测——与训练生态同栈、代码可读可改（本讲的实验都是它）；vLLM/SGLang 适合服务与批量生产——连续批处理与 PagedAttention 大幅提升并发吞吐。同硬件下后者吞吐通常高一个量级（具体倍数待本地验证）。

**练习 3**：README L139-140 写 "Coming soon"，仓库里却有中间检查点 PDF，以哪个为准？依据是什么？
**答案**：以 PDF 与 HuggingFace 页面为准。依据是 git 历史：PDF 于 2025-02-28 由 commit `9c7a5a9` 加入仓库，说明发布说明已成文，只是 README 章节文案没有同步更新（u1-l1 已建立此结论）。这是「文档滞后于实物」的常见案例——裁决时看时间戳更新的实物证据。

## 5. 综合实践

把四个模块串成一个任务：**用 chat template 问 Moonlight-16B-A3B-Instruct「123 是否为质数」，生成并校验回答**。按硬件条件三级回退，至少完成一级。

**完整脚本**（示例代码，保存为 `infer_instruct.py`；对应任务书要求的实践）：

```python
# 示例代码：Moonlight-16B-A3B-Instruct 对话推理（README L106-125 的本地化扩展）
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

model_path = "moonshotai/Moonlight-16B-A3B-Instruct"
model = AutoModelForCausalLM.from_pretrained(
    model_path,
    torch_dtype="auto",        # 按 config 加载为 bfloat16
    device_map="auto",         # accelerate 自动切分放置
    trust_remote_code=True,    # 执行仓库内的 modeling/tokenization 远端代码
)
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)

messages = [
    {"role": "system", "content": "You are a helpful assistant provided by Moonshot-AI."},
    {"role": "user", "content": "123 是质数吗？请说明理由。"},
]

# 先看渲染结果（4.3 的知识），再生成
print("=== 提示渲染 ===")
print(tokenizer.apply_chat_template(messages, add_generation_prompt=True, tokenize=False))

input_ids = tokenizer.apply_chat_template(
    messages, add_generation_prompt=True, return_tensors="pt").to(model.device)
generated_ids = model.generate(inputs=input_ids, max_new_tokens=500)

# README 原样的输出（含提示与特殊 token）
print("=== 完整解码 ===")
print(tokenizer.batch_decode(generated_ids)[0])

# 只取新生成部分并去掉特殊 token
new_ids = generated_ids[:, input_ids.shape[-1]:]
print("=== 回答 ===")
print(tokenizer.batch_decode(new_ids, skip_special_tokens=True)[0])

for i in range(torch.cuda.device_count()):
    print(f"cuda:{i} 峰值显存 = {torch.cuda.max_memory_allocated(i)/1e9:.1f} GB")
```

**三级回退**：

- **A 级（有 ≥80 GB 单卡或 ≥2×48GB / 4×24GB 多卡）**：直接运行上述脚本（待本地验证）。
- **B 级（只有 24 GB 单卡）**：给 `from_pretrained` 增加 `quantization_config=BitsAndBytesConfig(load_in_4bit=True, bfloat16)`（需 `pip install bitsandbytes`），其余不变；预期权重约 8 GB（待本地验证）。
- **C 级（无 GPU）**：跑 4.3.4 的 `preview_chat_template.py`（CPU 可完整运行），并完成下面的显存估算书面题——这正是任务书允许的「完成可运行脚本并估算所需显存」路径。

**C 级估算题**（答案即交付物的一部分）：

1. bf16 权重显存：\(15.96\times10^9 \times 2\,\text{B} \approx 31.9\,\text{GB}\)；
2. 500 个新 token 的 KV cache（提示 + 生成约 100+500 token，按 600 token 计）：\(600 \times 30.4\,\text{KB} \approx 18\,\text{MB}\)（MLA 压缩后的量级，待本地验证）；
3. 结论：**总需求 ≈ 32 GB 出头，单张 80 GB 卡从容，2×48GB 或 4×24GB 靠 `device_map="auto"` 切分可行，24 GB 单卡需 4-bit 量化**。

**观察清单与预期结果**：

- 渲染结果与 4.3.3 手工渲染一致（特殊 token、默认系统提示逻辑）；
- 完整解码开头能看到 `<|im_system|>system<|im_middle|>...`，`=== 回答 ===` 段是干净文本；
- 生成在 `<|im_end|>`（id 163586）处自然停止，`max_new_tokens=500` 只是上限；
- 数学内容：123 = 3 × 41，**不是质数**——模型应给出这一结论及理由（具体表述待本地验证）；
- 峰值显存与账本对照（A 级约 33-36 GB/80GB 卡，待本地验证）。

## 6. 本讲小结

- Moonlight 发布 base（`Moonlight-16B-A3B`，续写用法）与 Instruct（`Moonlight-16B-A3B-Instruct`，对话用法）两个权重（[README.md:69-78](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L69-L78)）；「16B」含嵌入（15.29B + 约 0.67B 嵌入与输出头 ≈ 15.96B），「A3B」是激活参数的名义值，README 表格的 2.24B 是不含嵌入口径。
- 加载三参数各司其职：`trust_remote_code=True` 放行执行 `auto_map` 指定的仓库内建模/分词代码（`DeepseekV3ForCausalLM`、`TikTokenTokenizer`），`torch_dtype="auto"` 落到 config 的 bfloat16（显存第一道闸门），`device_map="auto"` 由 accelerate 自动切分放置（需另装 accelerate）。
- 显存账本：bf16 权重约 31.9 GB；MLA 把 KV cache 压到每 token 约 30 KB（\( (512+64)\times2\,\text{B}\times27 \) 层），满 8K 上下文也不到 0.3 GB；24 GB 单卡的退路是 4-bit 量化（约 8 GB 权重）。
- chat template 是「存在 `tokenizer_config.json` 里的 Jinja 模板」：特殊 token（`<|im_user|>` 等）包裹每条消息，首条非 system 时自动注入默认系统提示，`add_generation_prompt=True` 追加助手前缀，生成在 `<|im_end|>`（eos 163586）停止；README 的 `batch_decode` 输出含提示与特殊 token，切掉提示再 `skip_special_tokens=True` 才是干净回答。
- 与 DeepSeek-V3 同构（`model_type="deepseek_v3"`）使 vLLM / SGLang 开箱可用（[README.md:128](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L128)）：transformers 用于研究调试，引擎用于生产吞吐。
- 中间检查点：摘要承诺发布（L19）、README 章节仍写 "Coming soon"（L139-140）、仓库已含发布说明 PDF（commit `9c7a5a9`）——以 PDF 与 HuggingFace 页面为准，具体清单待确认。

## 7. 下一步学习建议

- **下一讲 u3-l4（走向分布式：ZeRO-1 式 Muon 与 Megatron 集成）**：本讲在「用模型」，下一讲回到「练模型」的最深处——论文如何把 Muon 以内存最优的方式扩展到大规模分布式训练；README 中 "memory optimal and communication efficient" 的承诺（[README.md:29](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L29)）将在那一讲兑现。
- **u3-l5（为 Moonlight 贡献代码）**：若你想给仓库提 PR，本讲对 README 推理章节与权重仓库结构的了解是直接前置。
- **延伸阅读一（模型内部）**：把 [modeling_deepseek.py](https://huggingface.co/moonshotai/Moonlight-16B-A3B-Instruct/blob/main/modeling_deepseek.py)（HuggingFace 仓库内文件）当作下一份精读材料——重点找 MoE 路由（sigmoid 打分 + noaux_tc top-k）与 MLA 的 KV cache 写入逻辑，把本讲 4.2/4.3 的估算逐处对上源码。
- **延伸阅读二（同构对照）**：下载 `deepseek-ai/DeepSeek-V2-Lite` 的 config 与 Moonlight 逐字段比对（4.4.4 的离线核验），体会「同族不同规模」的配置设计；再读 Moonlight.pdf 的模型结构章节对照。
