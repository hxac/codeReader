# 模型格式转换与第三方生态对接

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 MiniMind 的两种权重格式——原生 `torch .pth` 与 `transformers` 文件夹——各自的形态、加载方式与适用场景。
- 读懂 `scripts/convert_model.py` 里的四个核心转换函数，并理解「为什么 MiniMind 的 torch 权重能直接塞进 `Qwen3ForCausalLM` 并 `strict=True` 加载」。
- 掌握 `convert_merge_base_lora` 如何把「基模 + LoRA 增量」合并成一份无分支的标准权重。
- 知道向 `llama.cpp` / `ollama` / `vllm` / `SGLang` / `MNN` 这五类第三方框架对接的关键链路与命令。

本讲是「推理部署、评测与生态对接」单元的第一讲，承接 u3-l1（模型骨架）与 u6-l2（LoRA 训练），把训练产出的 `.pth` 权重「翻译」成生态通用格式，是后续 u8-l2（OpenAI 兼容 API）、u8-l3（WebUI / 评测）的前置。

## 2. 前置知识

### 2.1 两种权重格式

训练脚本（如 `train_pretrain.py`、`train_full_sft.py`）产出的权重是**原生 torch 格式**：一个 `.pth` 文件，本质是 `torch.save(state_dict, path)` 存下的「参数名 → 张量」字典。它只能被「知道这套命名规则」的自己人加载（`MiniMindForCausalLM.load_state_dict`）。

而生态里的 `llama.cpp`、`vllm`、`ollama`、`transformers` 都不认识 MiniMind 的私有命名，它们只认一套**通用格式**：一个文件夹，里面有 `config.json`（结构配置）、`model.safetensors` 或 `pytorch_model.bin`（权重）、`tokenizer.json` / `tokenizer_config.json`（分词器）。这套格式由 `transformers` 库定义，因为 MiniMind 的结构对齐了 Qwen3，所以转成 Qwen3 命名后，所有第三方框架都能直接吃。

### 2.2 权重命名对齐：为什么能 strict 加载

`convert_torch2transformers` 里有一行很关键：`qwen_model.load_state_dict(state_dict, strict=True)`。`strict=True` 意味着「参数名和形状必须逐个对得上，多一个少一个都报错」。这之所以能成功，是因为 MiniMind 在命名上有意照搬了 Qwen3：

| MiniMind 原生命名 | Qwen3 命名 | 说明 |
|---|---|---|
| `model.embed_tokens.weight` | `model.embed_tokens.weight` | 词嵌入 |
| `model.layers.{l}.self_attn.q_proj.weight` | 同名 | Q 投影 |
| `model.layers.{l}.self_attn.q_norm.weight` | 同名 | 每头 RMSNorm |
| `model.layers.{l}.input_layernorm.weight` | 同名 | Pre-Norm |
| `model.layers.{l}.mlp.gate_proj.weight` | 同名 | SwiGLU gate |
| `lm_head.weight` | 同名（与 embed 共享） | tie_word_embeddings |

也就是说，从 u3-l1 起 MiniMind 选择的 `embed_tokens / self_attn.{q,k,v,o}_proj / mlp.{gate,up,down}_proj / input_layernorm` 这套名字，就是为了今天这步转换铺路。命名一致 = 权重可直接搬运。

### 2.3 LoRA 增量回顾

承接 u6-l1/u6-l2：LoRA 不改基模权重 `W`，而是旁挂一个低秩增量 `BA`（`B` 为 0 初始化、`A` 高斯初始化），前向 `y = Wx + BAx`。训练只存增量 `.lora.` 参数（文件极小）。本讲的 `convert_merge_base_lora` 要做的就是把 `BA` 加回 `W`，导出一份「没有 LoRA 分支」的干净权重，便于第三方框架部署。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [scripts/convert_model.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/convert_model.py) | 本讲主角，4 个转换函数 + `__main__` 入口 |
| [model/model_minimind.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py) | 模型结构与命名（`MiniMindConfig` / `MiniMindForCausalLM` / `FeedForward` / `MOEFeedForward`） |
| [model/model_lora.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_lora.py) | `apply_lora` / `merge_lora`，LoRA 合并的底层实现 |
| [README.md](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md) | 第三方框架对接命令（llama.cpp / ollama / vllm / SGLang / MNN） |
| [eval_llm.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/eval_llm.py) | `init_model`，展示两种格式的加载分支 |

> ⚠️ 运行位置：`convert_model.py` 内部用 `../model/`、`../out/`、`../minimind-3` 这类**相对上级目录**的路径，因此必须在 `scripts/` 目录下执行：`cd scripts && python convert_model.py`（README 第 814 行亦是如此）。

---

## 4. 核心概念与源码讲解

### 4.1 两种格式与加载分支

#### 4.1.1 概念说明

MiniMind 同时维护两套权重，是因为它们服务两类读者：

- **torch `.pth`**：服务项目自己。文件小、加载快、能叠加 LoRA，`eval_llm.py` 默认走这条路。缺点是「只有自己人懂」。
- **transformers 文件夹**：服务整个开源生态。`config.json` 自描述结构，任何 `AutoModelForCausalLM.from_pretrained` 都能加载，是通往 vllm / ollama / llama.cpp 的「通用入场券」。

README 明确：主线发布的开源模型**默认以 transformers 格式提供**；若手头只有 torch 权重，需先 `torch2transformers` 转换（[README.md:1617-1618](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L1617-L1618)）。

#### 4.1.2 核心流程

`eval_llm.py` 的 `init_model` 用一个字符串判定走哪条分支：

```text
if 'model' in args.load_from:     # 原生 torch 分支
    构造 MiniMindForCausalLM 空壳 → load_state_dict(torch权重) → 可选叠加 LoRA
else:                              # transformers 分支
    AutoModelForCausalLM.from_pretrained(路径)   # 自动读 config.json
```

torch 分支下，文件名由 `{weight}_{hidden_size}{_moe?}.pth` 拼接（如 `full_sft_768.pth`、`pretrain_512_moe.pth`），命名规则与 u1-l3、u4-l1 完全一致。

#### 4.1.3 源码精读

[eval_llm.py:12-30](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/eval_llm.py#L12-L30) 是两条分支的判定与加载：

- 第 14 行 `if 'model' in args.load_from`：`--load_from model`（默认值）命中 torch 分支；`--load_from ./minimind-3` 这种目录名不含 `model` 子串则走 transformers 分支。这是个朴素但有效的字符串判定。
- 第 22 行 `ckp = f'./{save_dir}/{weight}_{hidden_size}{moe_suffix}.pth'`：torch 权重的命名拼接，`save_dir` 默认 `out`。
- 第 23 行 `strict=True`：torch 分支对自己的命名有完全把握，严格加载。
- 第 28 行 `AutoModelForCausalLM.from_pretrained(args.load_from, trust_remote_code=True)`：transformers 分支，`trust_remote_code` 允许加载带自定义代码的模型。

注意第 30 行 `.half()`：两条分支最终都转 fp16 推理。这一点在转换时很重要——转换函数同样默认 `dtype=torch.float16`。

#### 4.1.4 代码实践

**目标**：直观看到两种格式的差异。

**步骤**：

1. 如果你已训练出 `out/full_sft_768.pth`（或下载了该权重），用 Python 打印它的键：
   ```python
   import torch
   sd = torch.load('out/full_sft_768.pth', map_location='cpu')
   print(list(sd.keys())[:5])     # 如 ['model.embed_tokens.weight', ...]
   print(sd['model.embed_tokens.weight'].dtype, sd['model.embed_tokens.weight'].shape)
   ```
2. 对比一个 transformers 格式目录（如下载的 `minimind-3/`）的文件清单：应当看到 `config.json`、`model.safetensors`（或 `.bin`）、`tokenizer.json`、`tokenizer_config.json`。

**观察现象**：torch 权重是「单文件 + 纯张量字典」；transformers 是「多文件 + 自描述配置」。

**预期结果**：能复述二者在「文件形态」与「加载方式」上的两点区别。

> 待本地验证：本仓库 `out/` 默认为空，需先训练或下载权重才能完成第 1 步。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `init_model` 用 `'model' in load_from` 而不是判断路径是否存在 `config.json`？

**答案**：这是一种最小侵入的快捷判定——`--load_from model` 是项目约定的「原生权重占位符」，而真实目录名（`minimind-3`、`./xxx`）一般不会恰好含 `model` 子串。判定 `config.json` 更严谨，但需要额外文件 IO，这里取舍了简洁。

**练习 2**：torch 分支用 `strict=True`，transformers 分支却没有显式 strict 参数，二者加载的「严格度」谁更高？

**答案**：表面看 torch 分支更严，但实际上 `from_pretrained` 内部也会校验 state_dict 与模型结构，缺/多键会告警甚至报错；只是语义不同——torch 分支是「我要确保命名 100% 对齐」，transformers 分支是「按 config.json 重建结构再灌权重」。在本项目中两者都要求命名一致。

---

### 4.2 torch → transformers：convert_torch2transformers 与 Qwen3 对齐

#### 4.2.1 概念说明

`convert_model.py` 里其实有两个「torch→transformers」函数，目标不同：

- `convert_torch2transformers_minimind`：转成**保留 MiniMind 自定义类**的 transformers 格式（依赖 `model_minimind.py` 的远程代码）。适合继续在 transformers 生态里用 `MiniMindForCausalLM`。
- `convert_torch2transformers`：转成**原生 Qwen3 / Qwen3-MoE 格式**（`Qwen3ForCausalLM` / `Qwen3MoeForCausalLM`）。这才是通往 vllm / ollama / llama.cpp 的关键——这些框架内置了对 Qwen3 的支持，无需 MiniMind 的自定义代码。

`__main__` 默认调用的是后者（Qwen3 格式），这也是 README 主推的发布格式。本节重点讲 `convert_torch2transformers`。

#### 4.2.2 核心流程

```text
1. torch.load(.pth) 得到 state_dict
2. 从 lm_config 构造 common_config（vocab/hidden/heads/rope_theta/tie...）
3. 用 common_config 实例化 Qwen3Config → Qwen3ForCausalLM（Dense）
   或 Qwen3MoeConfig → Qwen3MoeForCausalLM（MoE）
4. [仅 MoE + transformers≥5.0] 把每层 per-expert 的 gate_proj/up_proj 融合成 gate_up_proj
5. qwen_model.load_state_dict(state_dict, strict=True)   # 命名对齐，直接灌入
6. 转 fp16 → save_pretrained(目标目录)
7. 拷贝 tokenizer
8. [transformers≥5.0] 修补 tokenizer_config.json 与 config.json 的兼容字段
```

第 4 步是 MoE 独有的「权重布局重排」，第 8 步是 transformers 5.0 的兼容补丁，下面分别精读。

#### 4.2.3 源码精读

**① 配置映射**：[scripts/convert_model.py:40-71](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/convert_model.py#L40-L71)。`common_config` 把 `MiniMindConfig` 的字段逐一搬到 Qwen3 的字段名上（二者本就对齐），Dense 用 `Qwen3Config(use_sliding_window=False, sliding_window=None)`（关闭滑动窗口，因为 MiniMind 不用），MoE 额外传 `num_experts / num_experts_per_tok / moe_intermediate_size / norm_topk_prob`。

**② strict 加载**：第 81 行 `qwen_model.load_state_dict(state_dict, strict=True)`。这是命名对齐的「验收关」——若 u3 系列讲义里的任何一个投影层名字与 Qwen3 不一致，这一步立刻报错。

**③ MoE 权重重排**（transformers ≥ 5.0）：[scripts/convert_model.py:72-79](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/convert_model.py#L72-L79)。MiniMind 的 MoE 每个 expert 是独立三个矩阵 `experts.{e}.{gate_proj,up_proj,down_proj}`；而 transformers 5.0 的 `Qwen3MoeForCausalLM` 把每个 expert 的 `gate_proj` 与 `up_proj` 沿维度 1 拼成一个融合矩阵 `experts.gate_up_proj`，`down_proj` 则沿新维度堆叠成 `experts.down_proj`。重排伪码：

```text
new_sd = 保留所有非 experts.* 的键（gate.weight 也保留）
对每一层 l:
    gate_up = cat([ stack(experts.{e}.gate_proj for e) ],     # [E, inter, hidden]
                  [ stack(experts.{e}.up_proj   for e) ], dim=1)  # 拼成 [E, inter, 2*hidden] 的融合形态
    down    = stack(experts.{e}.down_proj for e)              # [E, hidden, inter]
```

这就是「同一份参数，换一种排布」——数值不变，但形状从「per-expert 散装」变成「batched 矩阵」，匹配 Qwen3-MoE 的 fused 算子。

**④ transformers 5.0 兼容补丁**：[scripts/convert_model.py:89-95](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/convert_model.py#L89-L95)。这段对生成的 json 做两处修补：

- `tokenizer_config.json`：补 `"tokenizer_class": "PreTrainedTokenizerFast"` 与空 `extra_special_tokens`，确保 5.0 能正确实例化快速分词器。
- `config.json`：显式写回 `rope_theta`，并把 `rope_scaling` 置 `None`、删除 `rope_parameters`。原因是 transformers 5.0 会把 RoPE 部分参数拆进 `rope_parameters`，而 MiniMind 的 YaRN 外推由自身的 `precompute_freqs_cis` 在推理时按 `inference_rope_scaling` 开关处理（见 u3-l3），转成原生 Qwen3 后若不清理，框架可能按自己的 YaRN 语义二次缩放位置编码，导致输出错乱。置空即「关闭框架侧的位置缩放，交回 MiniMind 语义」。

**⑤ 反向转换**：[scripts/convert_model.py:99-102](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/convert_model.py#L99-L102) 的 `convert_transformers2torch` 极简——`AutoModelForCausalLM.from_pretrained` 加载后把 `state_dict` 全部 `.cpu().half()` 存成 `.pth`。反向之所以简单，是因为加载侧（`AutoModelForCausalLM`）已把所有兼容细节处理妥当。

**⑥ jinja / json 模板互转**：[scripts/convert_model.py:115-125](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/convert_model.py#L115-L125)。`chat_template` 在 `tokenizer_config.json` 里是一个被 JSON 转义的字符串字段，单独抽取出来就是一段 `.jinja` 文件；反之亦然。`convert_json_to_jinja` 从 json 抽模板写盘，`convert_jinja_to_json` 把 jinja 文本 `json.dumps` 转义后打印成可粘贴回配置的字段串。这在你需要用 ollama 的 `TEMPLATE`（go 模板）替换 transformers 的 jinja、或反向移植模板时很有用。

#### 4.2.4 代码实践

**目标**：把一份 torch 权重转成 transformers（Qwen3）格式，并验证 config.json 被正确修补。

**步骤**：

1. 确认 `out/full_sft_768.pth` 存在；在 `scripts/convert_model.py` 的 `__main__` 中（[scripts/convert_model.py:128-134](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/convert_model.py#L128-L134)）确认 `lm_config` 与你的权重一致（`hidden_size=768, num_hidden_layers=8, use_moe=False`），目标目录 `../minimind-3`。
2. 执行：
   ```bash
   cd scripts && python convert_model.py
   ```
3. 打开生成的 `minimind-3/config.json`，检查 `rope_theta` 是否为 `1000000.0`、`rope_scaling` 是否为 `null`、是否**不含** `rope_parameters` 字段（若你的 transformers ≥ 5.0）。

**观察现象**：终端打印「模型参数: 64.x 百万」；`minimind-3/` 下出现 `config.json`、权重文件、`tokenizer.json`、`tokenizer_config.json`。

**预期结果**：转换后的目录可被 `AutoModelForCausalLM.from_pretrained('./minimind-3', trust_remote_code=True)` 成功加载（虽然此时是 Qwen3 命名，但 MiniMind 结构与 Qwen3 一致，可不依赖远程代码）。

> 待本地验证：需先具备 `out/full_sft_768.pth`；本仓库默认不含训练产物。

#### 4.2.5 小练习与答案

**练习 1**：为什么 MoE 在 transformers ≥ 5.0 时要做 `gate_up_proj` 融合，而 Dense 不用？

**答案**：Dense 的 `FeedForward` 本来就有独立的 `gate_proj` 和 `up_proj`，与 Qwen3 Dense 命名一致，无需重排。MoE 则因为 transformers 5.0 的 `Qwen3Moe` 实现给每个 expert 用了 fused 的 `gate_up_proj` 矩阵（一次矩阵乘同时算 gate 和 up，更高效），所以要把 MiniMind 散装的 per-expert 三矩阵重新拼装成融合形态才能 strict 加载。

**练习 2**：转换后 `config.json` 里删掉 `rope_parameters`、置空 `rope_scaling`，会不会让模型失去长文本外推能力？

**答案**：会「在原生 Qwen3 框架侧」失去框架自带的 YaRN，但这正是目的——MiniMind 的 YaRN 由 `inference_rope_scaling` 在自身 `precompute_freqs_cis` 中实现。清理这些字段是为了避免框架与 MiniMind 各自的 RoPE 语义打架。若在第三方框架里需要外推，应在**该框架**的 config 中按其 YaRN 约定重新配置（README 第 1540 行提到对 transformers 格式可在 config.json 中加长度外推配置）。

---

### 4.3 LoRA 合并导出：convert_merge_base_lora

#### 4.3.1 概念说明

u6-l2 训练出的 LoRA 权重（如 `lora_identity_768.pth`）只是「增量」，推理时需要「基模 + LoRA」组合加载（`eval_llm.py --weight full_sft --lora_weight lora_identity`）。但 vllm、ollama 这类框架**不支持** MiniMind 的 monkey-patch 式 LoRA 旁路——它们只认一份完整的标准权重。

解决办法是：把增量 `BA` 永久「焊」进基模权重 `W`，导出一份无 LoRA 分支的完整模型。这正是 `convert_merge_base_lora` 做的事（README 第 811-815 行的官方说明）。

合并的数学本质：

\[
W' = W + BA,\quad W\in\mathbb{R}^{d\times d},\ B\in\mathbb{R}^{d\times r},\ A\in\mathbb{R}^{r\times d}
\]

合并后前向从 \(y = Wx + BAx\) 退化回 \(y = W'x\)，与普通 dense 模型无异。

#### 4.3.2 核心流程

```text
1. 实例化空壳 MiniMindForCausalLM
2. load_state_dict(基模 .pth, strict=False)        # 载入基模 W
3. apply_lora(model)                                # 挂上 lora 旁路（此时 BA 随机）
4. merge_lora(model, lora_path, merged_path):
     a. load_lora(lora_path)                        # 把训练好的 BA 灌进旁路
     b. 遍历所有 Linear：
          - 默认拷贝原 W 到新 state_dict
          - 若该层挂了 lora：W' = W + (B @ A)，覆盖写入
     c. 保存为新的 .pth（不含任何 .lora. 键）
```

#### 4.3.3 源码精读

**① 顶层组装**：[scripts/convert_model.py:105-112](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/convert_model.py#L105-L112)。三步：载基模 → `apply_lora` 挂旁路 → `merge_lora` 落盘。注意基模用 `strict=False` 容错（合并时模型刚 `apply_lora` 挂了 `.lora.` 子模块，但此时还没灌权重，与基模 state_dict 不完全匹配，故宽松加载）。

**② apply_lora 注入**：[model/model_lora.py:21-32](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_lora.py#L21-L32)。遍历所有 `in_features == out_features` 的方阵 `nn.Linear`（默认配置下恰好是每层的 `q_proj` / `o_proj`），用 `setattr` 挂一个 `lora` 子模块，并把 `forward` 替换成 `原输出 + lora(x)`。关键是 `forward_with_lora` 用默认参数 `layer1=original_forward, layer2=lora` 显式绑定闭包实例，避免循环变量捕获陷阱（详见 u6-l1）。

**③ merge_lora 焊接**：[model/model_lora.py:56-65](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_lora.py#L56-L65)。这是合并的核心：

```python
state_dict = {k: v.cpu().half() for k, v in raw_model.state_dict().items() if '.lora.' not in k}  # ① 抛弃所有 LoRA 增量键
for name, module in raw_model.named_modules():
    if isinstance(module, nn.Linear) and '.lora.' not in name:
        state_dict[f'{name}.weight'] = module.weight.data.clone().cpu().half()                    # ② 重新拷贝原 W
        if hasattr(module, 'lora'):
            state_dict[f'{name}.weight'] += (module.lora.B.weight.data @ module.lora.A.weight.data).cpu().half()  # ③ W += B@A
torch.save(state_dict, save_path)
```

第 ③ 行 `(B.weight @ A.weight)` 就是把 \(BA\) 加回 \(W\)。注意 `B.weight` 形状 `[d, r]`、`A.weight` 形状 `[r, d]`，相乘得 `[d, d]` 与 `W` 同形，逐元素相加。合并后的 state_dict **不含任何 `.lora.` 键**，是一份纯净的标准权重，可被任何框架当作普通 dense 模型加载。

> 一个细节：第 ① 步先整体拷贝时已排除 `.lora.` 键，但第 ② 步又对所有 Linear 重新 `clone()` 拷贝一次原 `module.weight`——这是因为挂了 lora 的 Linear，其 `module.weight` 仍是干净的基模 `W`（LoRA 只改 forward，没改 `W` 本身），重新克隆确保存的是最新值，再由第 ③ 行加回 `BA`。没挂 lora 的普通 Linear（如 `k_proj`/`v_proj`/`gate_proj`）则只走第 ② 步原样保存。

#### 4.3.4 代码实践

**目标**：把 `full_sft_768.pth` + `lora_identity_768.pth` 合并成 `merge_identity_768.pth`，并验证合并权重等价于「基模 + LoRA」组合。

**步骤**：

1. 在 `scripts/convert_model.py` 的 `__main__` 中注释掉 `convert_torch2transformers(...)`，取消注释 `convert_merge_base_lora(...)` 三行（[scripts/convert_model.py:136-140](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/scripts/convert_model.py#L136-L140)），确认三个路径指向 `full_sft_768.pth`、`lora_identity_768.pth`、`merge_identity_768.pth`。
2. 执行 `cd scripts && python convert_model.py`。
3. 等价性验证（示例代码）：
   ```python
   # 方式A：合并权重直接加载
   m_a = MiniMindForCausalLM(cfg); m_a.load_state_dict(torch.load('out/merge_identity_768.pth'), strict=True)
   # 方式B：基模 + LoRA 组合
   m_b = MiniMindForCausalLM(cfg); m_b.load_state_dict(torch.load('out/full_sft_768.pth'), strict=True)
   apply_lora(m_b); load_lora(m_b, 'out/lora_identity_768.pth')
   # 同一输入比对输出
   import torch
   x = torch.randint(0, 6400, (1, 8))
   print(torch.allclose(m_a(x).logits, m_b(x).logits, atol=1e-3))
   ```

**观察现象**：`merge_identity_768.pth` 的键集合与 `full_sft_768.pth` **完全相同**（都不含 `.lora.`），但数值不同（已吸收增量）。

**预期结果**：方式 A 与方式 B 的 logits 应数值一致（`allclose` 为 True，受 fp16 精度影响可能有微小误差）。

> 待本地验证：需先具备 `full_sft_768.pth` 与 `lora_identity_768.pth` 两份权重。

#### 4.3.5 小练习与答案

**练习 1**：合并后导出的权重，能否再用 `eval_llm.py --lora_weight` 叠加新的 LoRA？

**答案**：能。合并权重结构上等同普通基模（无 `.lora.` 键），完全可以当作新的「基模」再走一遍 `apply_lora` + 训练 + 组合推理。这也是「先合并旧 LoRA，再训新 LoRA」的串联微调思路。

**练习 2**：`merge_lora` 里为什么必须 `load_lora(lora_path)` 之后才能合并？跳过这步行不行？

**答案**：不行。`apply_lora` 挂上的 `B` 是 0 初始化、`A` 是随机初始化，此时 `BA = 0`。只有 `load_lora` 把训练好的增量灌入，`B@A` 才是真实学到的 `ΔW`。跳过会导致 `W' = W + 0 = W`，相当于没合并。

---

### 4.4 第三方框架对接：llama.cpp / ollama / vllm / SGLang / MNN

#### 4.4.1 概念说明

转换成 transformers（Qwen3）格式后，对接第三方框架基本只剩「命令行」问题。五类框架各定位不同：

| 框架 | 定位 | 输入格式 | 是否需 CUDA |
|---|---|---|---|
| **vllm** | 高吞吐服务端 | transformers 目录 | 是 |
| **SGLang** | 低延迟高吞吐引擎（含 RL rollout） | transformers 目录 | 是 |
| **llama.cpp** | 轻量 C++ / CPU 友好 / 量化 | GGUF | 否 |
| **ollama** | 本地一键运行 | GGUF | 否 |
| **MNN** | 端侧（手机/Mac）部署 | MNN（HQQ 量化） | 否 |

对接链路可归纳为两条：

```text
链路 A（服务端）：torch .pth --convert_torch2transformers--> transformers 目录 --> vllm / SGLang
链路 B（本地/端侧）：transformers 目录 --> GGUF（llama.cpp） --> ollama ；或 --> MNN
```

#### 4.4.2 核心流程

**vllm / SGLang**：直接吃 transformers 目录。SGLang 还在 RL 训练里充当 rollout 引擎（见 u7-l2）。

**llama.cpp → ollama**：llama.cpp 不读 transformers，需要先用其自带脚本把 HF 格式转成 GGUF（可选量化），ollama 再以 GGUF 为原料。因 MiniMind 词表是私有训练的（6400），llama.cpp 的 `convert_hf_to_gguf.py` 不内置其词表类型，需手动指定复用一个兼容项（README 用 `qwen2`）。

#### 4.4.3 源码精读（命令）

**① SGLang 启动**（[README.md:1663-1673](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L1663-L1673)）：
```bash
python -m sglang.launch_server --model-path /path/to/model --attention-backend triton --host 0.0.0.0 --port 8998
```
`--attention-backend triton` 是关键，因为 MiniMind 用了 Q/K 的 per-head RMSNorm（`q_norm`/`k_norm`），需要 triton 后端支持自定义注意力。SGLang 同样可作 RL rollout（README 第 1213-1217 行：`--rollout_engine sglang`）。

**② vllm 启动**（[README.md:1675-1685](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L1675-L1685)）：
```bash
vllm serve /path/to/model --model-impl transformers --served-model-name "minimind" --port 8998
```
`--model-impl transformers` 显式声明用 transformers 实现（而非 vllm 自家的 Qwen3 实现），更稳妥。

**③ llama.cpp 转换链**（[README.md:1687-1734](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L1687-L1734)）：
```bash
# 0. 先在 convert_hf_to_gguf.py 的 get_vocab_base_pre 末尾加：
#    if res is None: res = "qwen2"      # 为私有词表指定兼容类型
# 1. HF -> GGUF
python convert_hf_to_gguf.py /path/to/minimind-model
# 2. 量化（可选）
./build/bin/llama-quantize xxxx.gguf xxxx.q8.gguf Q8_0
# 3. 推理
./build/bin/llama-cli -m xxxx.gguf
```
第 0 步是「为 MiniMind 私有词表打补丁」——`get_vocab_base_pre` 通过词表指纹识别模型类型，识别不到（MiniMind 不在白名单）就返回 `None`，需手动兜底为 `qwen2`（结构与词表分词风格兼容）。

**④ ollama 加载**（[README.md:1736-1832](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L1736-L1832)）：在模型目录建 `minimind.modelfile`，`FROM` 指向上一步的 GGUF，并写一段 `TEMPLATE`（go 模板，等价于 transformers 的 jinja chat_template，含 `<think>` 与 `<tool_call>` 分支）和 stop/temperature 等参数，然后：
```bash
ollama create -f minimind.modelfile minimind-local
ollama run minimind-local
```
也可直接 `ollama run jingyaogong/minimind-3` 用作者预发布版。

**⑤ MNN 端侧导出**（[README.md:1855-1869](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L1855-L1869)）：
```bash
cd MNN/transformers/llm/export
python llmexport.py --path /path/to/模型路径/ --export mnn --hqq --dst_path 模型路径-mnn
```
`--hqq` 导出 4-bit 量化模型，便于在手机端运行。

#### 4.4.4 代码实践

**目标**：把转换好的 `minimind-3` 目录分别用 vllm 与 ollama 加载，做一次推理并与 `eval_llm.py` 对比。

**步骤**：

1. 先按 4.2.4 完成 torch→transformers 转换，得到 `minimind-3/`。
2. vllm 路线：
   ```bash
   vllm serve ./minimind-3 --model-impl transformers --served-model-name "minimind" --port 8998
   # 另开终端用 OpenAI SDK 调用
   curl http://localhost:8998/v1/chat/completions -H 'Content-Type: application/json' \
     -d '{"model":"minimind","messages":[{"role":"user","content":"你是谁？"}]}'
   ```
3. ollama 路线（需先经 llama.cpp 转 GGUF）：
   ```bash
   # 在 convert_hf_to_gguf.py 加 qwen2 兜底后
   python convert_hf_to_gguf.py ./minimind-3
   # 建 modelfile（FROM 指向生成的 gguf），再
   ollama create -f minimind.modelfile minimind-local
   ollama run minimind-local
   ```
4. 同一条 prompt（如「你是谁？」）用 `python eval_llm.py --load_from ./minimind-3` 也跑一次，对比三处回答。

**观察现象**：三者回答风格应接近（同一份权重），但 vllm/ollama 因默认采样参数与 `eval_llm.py`（temperature=0.85, top_p=0.95）不同，逐字可能不一致；vllm/ollama 首字延迟更低、吞吐更高。

**预期结果**：vllm 与 ollama 都能成功加载并产出连贯中文回答；与 `eval_llm.py` 在「语义」上一致即视为通过。

> 待本地验证：vllm/ollama/llama.cpp 均需额外安装；本仓库不提供这些运行环境。

#### 4.4.5 小练习与答案

**练习 1**：为什么对接 llama.cpp 时要在 `get_vocab_base_pre` 里兜底 `res = "qwen2"`，而对接 vllm 却不需要？

**答案**：vllm 直接读 transformers 目录，词表信息已在 `tokenizer.json` 中自描述，无需识别。llama.cpp 的 `convert_hf_to_gguf.py` 则靠 `get_vocab_base_pre` 用词表指纹猜模型类型以套用对应 BPE 规则，MiniMind 不在其白名单会得 `None`，故需手动指定一个分词风格兼容的 `qwen2` 兜底。

**练习 2**：SGLang 为何强制 `--attention-backend triton`？

**答案**：MiniMind 在 Attention 内对每个头做了 `q_norm`/`k_norm`（per-head RMSNorm，见 u3-l2），这是较新的结构细节。默认的 flash/cUDA 后端未必支持这类自定义归一化，triton 后端可灵活表达，故推荐用它以保证数值与 MiniMind 原生实现一致。

---

## 5. 综合实践

把本讲内容串起来，完成一次「从 torch 训练产物到端侧可运行」的完整转换链：

1. **准备**：确认有 `out/full_sft_768.pth`（Dense）与（可选）`out/lora_identity_768.pth`。
2. **合并 LoRA**（若做了垂域 LoRA）：编辑 `scripts/convert_model.py` 的 `__main__`，启用 `convert_merge_base_lora`，得到 `merge_identity_768.pth`；用 4.3.4 的等价性脚本验证它与「基模+LoRA」输出一致。
3. **转 transformers**：把 `__main__` 的 `torch_path` 指向上一步合并后的权重（或直接用 `full_sft_768.pth`），运行 `convert_torch2transformers`，产出 `minimind-3/` 目录；检查 `config.json` 的 `rope_scaling`/`rope_parameters` 已被清理。
4. **对接一个服务端框架**：用 vllm 或 SGLang 加载 `minimind-3/`，发一条 chat 请求。
5. **对比**：同 prompt 下用 `eval_llm.py --load_from ./minimind-3` 跑一次，确认转换无损（语义一致）。

> 待本地验证：整条链路需训练权重 + 第三方框架环境，建议在本地逐步完成并记录每步产物。

## 6. 本讲小结

- MiniMind 维护**两种权重格式**：torch `.pth`（自用、可叠 LoRA）与 transformers 文件夹（生态通用）；`eval_llm.init_model` 用 `'model' in load_from` 字符串判定分支。
- `convert_torch2transformers` 把 torch 权重转成**原生 Qwen3 / Qwen3-MoE 格式**，因命名对齐故能 `strict=True` 直接加载；MoE 在 transformers ≥ 5.0 下需把 per-expert 的 `gate_proj/up_proj` 融合成 `gate_up_proj`。
- transformers 5.0 兼容补丁会清理 `config.json` 的 `rope_scaling`/`rope_parameters`，避免框架与 MiniMind 自身 RoPE 语义冲突。
- `convert_merge_base_lora` 用 `apply_lora` + `merge_lora` 把增量 \(BA\) 焊回基模 \(W\)，导出无 `.lora.` 分支的标准权重，让 vllm/ollama 等不支持 monkey-patch LoRA 的框架也能部署垂域微调模型。
- 第三方对接分两条链路：**服务端**（vllm / SGLang 直接读 transformers 目录）、**本地/端侧**（llama.cpp 转 GGUF → ollama，或 MNN 导出）；llama.cpp 需为 MiniMind 私有词表在 `get_vocab_base_pre` 兜底 `qwen2`。
- `convert_jinja_to_json` / `convert_json_to_jinja` 负责 chat_template 在 jinja 文件与 tokenizer_config.json 字段之间的互转，便于在 ollama 的 go 模板与 transformers 的 jinja 间移植。

## 7. 下一步学习建议

- 转成 transformers 格式后，下一讲 **u8-l2（OpenAI 兼容 API 服务）** 会讲解如何用 FastAPI 把模型包装成 `/v1/chat/completions` 端点，实现流式输出、思考内容与工具调用——这是把转换后的模型「服务化」的直接延续。
- 若你对 RL 后训练的推理引擎感兴趣，可回顾 **u7-l2（Rollout 引擎）**：SGLang 在那里作为 `SGLangRolloutEngine` 通过 HTTP 同步权重并返回 logprob，与本讲的 SGLang 启动命令呼应。
- 想深入理解为何转换能 strict 加载，建议重读 **u3-l1（Config 与骨架）** 的命名设计；想复习 LoRA 合并的数学，回看 **u6-l1（LoRA 实现）**。
- 进阶可研究 `convert_torch2transformers_minimind`（保留自定义类）与 `convert_torch2transformers`（原生 Qwen3）的差异，理解 `trust_remote_code` 在部署中的取舍。
