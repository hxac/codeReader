# 导出侧的投机解码

## 1. 本讲目标

本讲承接 [u7-l1 投机解码策略](u7-l1-speculative-decoding-strategies.md)（C++ 运行时侧），把视角切回 **Python 导出端**，讲清楚一件事：

> 一个训练好的「草稿模型检查点」，是怎么被翻写成 C++ 构建器能消费的 ONNX 图与 sidecar 的？

学完后你应该能够：

- 说清 `_resolve_model_variant` 如何用一组布尔开关 + 检查点自带字段，裁决出当前导出的是 base 还是 draft、是哪一种投机解码变体；
- 复述 EAGLE3 / MTP / DFlash / Gemma4-MTP 四类 draft 各自的 **key remap（权重键重命名）规则**，并理解为什么要 remap；
- 说清 **draft-to-target（d2t）词表映射** 是什么、从哪来、在导出流程的哪一步落盘成 sidecar；
- 区分四类 draft 在「草稿来源（独立检查点 / 派生 / 配对）」「权重共享（lm_head / embedding / KV）」上的设计差异。

## 2. 前置知识

阅读本讲前，请确认你已经理解以下内容（它们在依赖讲义中已建立）：

- **投机解码的 base / draft 双模型结构**（u7-l1）：base 模型负责「验证」，draft 模型负责「提前猜一批 token」。两者分别导出成两份 ONNX，再分别构建成 `spec_base.engine` 与 `spec_draft.engine`。
- **`AutoModel.from_pretrained` 的两段式流程**（u2-l2）：先 `config = load_model_config(model_dir)` 选模型类，再 `load_weights` 把 safetensors 权重灌进空壳 `nn.Module`。
- **权重加载阶段的 key remap**（u2-l4）：`load_weights` 接受一个 `key_remap(key) -> str | None` 回调，返回 `None` 表示跳过该键、返回新字符串表示重命名，借此把检查点里的「训练命名」翻译成模型期望的「参数命名」。
- **`make_linear` 与量化**（u2-l3）：线性层的具体精度由 `module_quant_type` 决定，draft 模型同样走这条统一入口。

一句话回顾：导出端面对的草稿检查点，是各家训练框架（EAGLE3 官方、z-lab DFlash、Qwen MTP、Gemma assistant）按各自习惯命名的产物，命名约定与 EdgeLLM 模型类的参数树并不一致。**变体解析决定「建哪个空壳模型」，key remap 决定「把检查点里哪个权重塞进空壳的哪个参数」**——本讲的两条主线。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `tensorrt_edgellm/model.py` | 变体解析 `_resolve_model_variant`、四个 key remap 函数、DFlash lm_head 兜底加载逻辑 |
| `tensorrt_edgellm/models/eagle3/modeling_eagle3_draft.py` | EAGLE3 draft 的 `nn.Module`（fc / 单解码层 / lm_head / d2t buffer）与 ONNX 导出规格 |
| `tensorrt_edgellm/models/dflash/modeling_dflash_draft.py` | DFlash draft 的 `nn.Module`（fc / 5 个缓存解码层 / lm_head）与 ONNX 导出规格 |
| `tensorrt_edgellm/config.py` | `is_eagle3_draft` 判据、`make_mtp_draft_config` / `make_dflash_draft_config` 配置派生 |
| `tensorrt_edgellm/scripts/export.py` | 导出编排：阶段表 thinker / mtp_draft / dflash_draft |
| `tensorrt_edgellm/checkpoint/checkpoint_utils.py` | `write_runtime_artifacts`：把 d2t buffer 写成 `d2t.safetensors` sidecar |

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：**变体解析**、**EAGLE3 draft**、**DFlash draft**。MTP 与 Gemma4-MTP 的 remap 规则会在「变体解析」模块里以对比表的形式一并覆盖。

### 4.1 变体解析：`_resolve_model_variant` 如何裁决 base / draft 角色

#### 4.1.1 概念说明

`AutoModel.from_pretrained` 是所有上层调用（CLI、Python API、VLM 子模型、各 draft 导出）的统一入口（u2-l2）。一个检查点目录喂进来，它必须先回答一个问题：**这个检查点要被当成什么角色导出？** 是普通 LLM，还是某一种投机解码的 base 或 draft？

答案是 `_resolve_model_variant`。它返回一个字符串「变体标签」，下游据此挑选模型类、装配 key remap、决定要不要派生配置。整个 `from_pretrained` 的后半段（选类、加载、后处理）都是围绕这个标签分支展开的。

关键直觉：**变体分为「自报告」与「开关驱动」两类**。EAGLE3 draft 是唯一「自报告」的——检查点自带 `draft_vocab_size` 字段，导出器读出来就知道它是 EAGLE3 draft，不需要任何命令行开关；其余变体（mtp base/draft、dflash base/draft、gemma4-mtp base/draft、eagle base）都靠布尔开关驱动。

#### 4.1.2 核心流程

`_resolve_model_variant` 的裁决分两步：

1. **互斥校验**（一堆 `raise ValueError`）：确认用户没有同时打开两个互相冲突的开关。例如 `eagle_base` 与 `mtp_base` 不能同时为真，dflash 与 eagle/mtp 也不能混用，gemma4-mtp 与其它所有变体互斥。
2. **优先级裁决**（一连串 `if ... return`）：按固定优先级返回唯一标签。

伪代码：

```text
# 第一步：互斥校验（任意冲突立即报错）
assert not (eagle_base and mtp_base)
assert not (eagle_base and mtp_draft)
assert not (mtp_base and mtp_draft)
assert not (dflash_base and dflash_draft)
assert not (dflash_*  and (eagle/mtp))
assert not (gemma4_mtp_* and 其它变体)
assert not (gemma4_mtp_base and gemma4_mtp_draft)

# 第二步：优先级裁决（自上而下，命中即返回）
if config.is_eagle3_draft:        return "eagle3_draft"   # 检查点自报告，最高优先
if gemma4_mtp_draft:              return "gemma4_mtp_draft"
if dflash_draft:                  return "dflash_draft"
if dflash_base:                   return "dflash_base"
if mtp_draft:                     return "mtp_draft"
if mtp_base:                      return "mtp_base"
if gemma4_mtp_base:               return "gemma4_mtp_base"
if eagle_base:                    return "eagle_base"
return "llm"                                              # 普通模型
```

优先级表（从高到低）：

| 优先级 | 标签 | 触发条件 | 草稿来源 |
|--------|------|----------|----------|
| 1 | `eagle3_draft` | `config.draft_vocab_size is not None`（自报告） | 独立 EAGLE3 检查点 |
| 2 | `gemma4_mtp_draft` | `--mtp --mtp-draft-dir`（gemma4 目标） | 配对的 assistant 检查点 |
| 3 | `dflash_draft` | `--dflash-draft --dflash-draft-dir` | 独立 z-lab DFlash 检查点 |
| 4 | `dflash_base` | `--dflash-base` / `--dflash-tree-base` | base 检查点本身 |
| 5 | `mtp_draft` | `--mtp`（Qwen3.5 单检查点） | 从 base 检查点派生 |
| 6 | `mtp_base` | `--mtp`（Qwen3.5 单检查点） | base 检查点本身 |
| 7 | `gemma4_mtp_base` | `--mtp`（gemma4 目标） | base 检查点本身 |
| 8 | `eagle_base` | `--eagle-base` | base 检查点本身 |
| 9 | `llm` | 以上都不命中 | — |

注意一个反直觉点：`is_eagle3_draft` 排在所有开关之前。这意味着 **哪怕你错误地把一个 EAGLE3 draft 检查点配上了 `--mtp` 等开关，它也会被当作 EAGLE3 draft**（前提是没触发互斥校验里的 mtp 相关断言）。

#### 4.1.3 源码精读

互斥校验与优先级裁决的完整实现：

[`_resolve_model_variant` 的互斥校验](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L485-L512) —— 逐对检查冲突开关，任一冲突直接 `raise ValueError`，保证同一时刻只有一个投机解码家族生效。

[`_resolve_model_variant` 的优先级裁决](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L513-L533) —— 关键是第 513 行 `if config.is_eagle3_draft:` 排在最前，EAGLE3 draft 由检查点自报告；其余按 `gemma4_mtp_draft → dflash_draft → dflash_base → mtp_draft → mtp_base → gemma4_mtp_base → eagle_base → llm` 的固定顺序短路返回。

EAGLE3 draft 的「自报告」判据，定义在 `ModelConfig` 的属性上：

[`is_eagle3_draft` 属性](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L699-L701) —— 只要 `draft_vocab_size` 字段非 `None` 即判定为 EAGLE3 draft。该字段在配置解析阶段直接从检查点 `config.json` 读出：

[解析 `draft_vocab_size`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L873-L875) —— `llm_dict.get("draft_vocab_size", None)`，缺失则保持 `None`（即非 EAGLE3 draft）。

拿到标签后，`from_pretrained` 用一串 `if/elif` 分支挑模型类、配 key remap。EAGLE3 draft 分支如下：

[选 Eagle3DraftModel 并装配 `_eagle3_key_remap`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L247-L253) —— 仅当调用方未自带 `key_remap` 时，才用 EdgeLLM 内置的 `_eagle3_key_remap`，这给了上层（如量化流水线）覆盖命名规则的机会。

**四类 draft 的 key remap 规则对比表**（这是本模块的核心知识点）：

| 变体 | remap 函数 | 跳过（返回 None） | 重命名 |
|------|-----------|-------------------|--------|
| `eagle3_draft` | `_eagle3_key_remap` | `t2d.*`（保留 `d2t`）、`target_model.*`（多目标训练产物） | `midlayer.*`→`layers.0.*`；`qkv_proj.{q,k,v}_proj`→`{q,k,v}_proj`；`._pre_quant_scale`→`.pre_quant_scale` |
| `mtp_draft` | `_mtp_key_remap` | 除 `mtp.*`、`lm_head.weight`、（tie 时）embedding 外的全部 | `mtp.` 前缀剥离；tie 时 `model.embed_tokens.weight`→`lm_head.weight` |
| `dflash_draft` | `_dflash_key_remap` | 含 `rotary_emb` 的键（RoPE 是计算量、非学习参数） | 无（其余键原样保留） |
| `gemma4_mtp_draft` | 无（`key_remap=None`） | — | assistant 检查点命名已与模型类对齐，直接加载 |

源码精读这四个函数：

[`_eagle3_key_remap`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L536-L558) —— 注意第 549 行 `if "t2d" in key and "d2t" not in key`：`t2d`（target-to-draft，训练用的反向映射）被丢弃，`d2t`（draft-to-target，运行时要用的正向映射）被保留。

[`_mtp_key_remap`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L763-L777) —— MTP draft 的权重在单检查点里以 `mtp.` 前缀存放，剥离后即可塞进 draft 模型；词表头在 tie 模式下从 embedding 表「借」。

[`_dflash_key_remap`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L756-L760) —— DFlash 检查点的命名与 draft 模型树基本对齐，只需剔除 `rotary_emb` 这类不该加载的缓存。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：亲手走一遍「变体标签 → 模型类 → key remap」的派生链，验证优先级表。

**操作步骤**：

1. 在 `model.py` 中定位 [`from_pretrained`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L124-L238) 的参数列表，数一下一共有几个 `*_base` / `*_draft` 布尔开关。
2. 顺着 [`variant = _resolve_model_variant(...)`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L238-L245) 往下读，记录每个 `elif variant == "..."` 分支挑选的 `model_class` 与装配的 `key_remap`。
3. 重点观察 [`mtp_draft` 分支](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L254-L268)：它在选类前先调了 `make_mtp_draft_config(config)` 派生配置，并用闭包把 `tie_word_embeddings` 捕获进 `key_remap`。

**需要观察的现象 / 预期结果**：

- base 变体（`mtp_base` / `dflash_base` / `gemma4_mtp_base` / `eagle_base`）几乎都不改 `model_class`（仍用注册表或默认 `CausalLM`），只通过置 `config.xxx_base = True` 让默认模型在导出时「长出」额外的 tree-attention 输入与 hidden_states 输出。
- draft 变体几乎都换 `model_class` 并装配专属 `key_remap`，因为 draft 的参数树与默认 `CausalLM` 不同。
- `gemma4_mtp_draft` 分支不装配 key remap，但额外置了 `shares_target_kv=True`、`has_own_kv_cache=False`（承接 u7-l1「Gemma4-MTP 与目标共享 KV」的设计）。

若你不确定某个分支的行为，可直接标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：假设有人把一个带 `draft_vocab_size` 的 EAGLE3 draft 检查点，错误地用 `--mtp` 导出。会发生什么？

**参考答案**：会先在 `_resolve_model_variant` 的互斥校验里命中 [`if config.is_eagle3_draft: if mtp_base or mtp_draft: raise`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L513-L517)，抛出 `"EAGLE3 draft checkpoints cannot be loaded as Qwen3.5 MTP variants."`，导出中止。

**练习 2**：为什么 DFlash 的 key remap 几乎是「空操作」，而 EAGLE3 的却要改这么多名字？

**参考答案**：DFlash draft 是 z-lab 专门为 EdgeLLM 这条流水线发布的检查点，其张量命名已与 `DFlashDraftModel` 的参数树对齐，只需剔除 `rotary_emb` 这类非学习缓存；而 EAGLE3 draft 来自学术界原始训练框架（HF EAGLE3 repo），用的是 `midlayer.` / `qkv_proj.` 等自有命名，必须翻译成 EdgeLLM 期望的 `layers.0.` / 拆分的 `q_proj` 等。

---

### 4.2 EAGLE3 draft 导出与权重 remap

#### 4.2.1 概念说明

EAGLE3 是树形投机解码（u7-l1）：draft 模型读 base 模型若干层的隐状态，条件于这些隐状态 + 上一步 draft 隐状态，逐层深度优先扩展出一棵候选 token 树，交 base 用树注意力验证。

EAGLE3 draft 的特别之处在于：

1. **它是唯一自报告的变体**——检查点的 `config.json` 自带 `draft_vocab_size`，导出器据此自动识别（见 4.1）。
2. **它没有专属导出阶段**——在 `scripts/export.py` 的阶段表里没有 `eagle3_draft` 这一行。你只需把 EAGLE3 draft 检查点目录当作普通模型目录喂给 `tensorrt-edgellm-export`，它会走默认的 `thinker` 阶段，在 `from_pretrained` 内部被自动识别为 draft。
3. **它有一个非参数 buffer `d2t`**——draft-to-target 词表映射，是 EAGLE3 训练时为「压缩草稿词表」而生的产物。

#### 4.2.2 核心流程

EAGLE3 draft 的导出数据流：

```text
EAGLE3 draft 检查点（HF repo，含 d2t / t2d / target_model / midlayer 等训练产物）
        │
        │  tensorrt-edgellm-export <draft_dir> <out>   （无任何 spec 开关）
        ▼
from_pretrained:
  config.is_eagle3_draft == True   （因 draft_vocab_size 非 None）
  → variant = "eagle3_draft"
  → model_class = Eagle3DraftModel
  → key_remap    = _eagle3_key_remap   （丢 t2d/target_model，midlayer→layers.0）
        │
        │  load_weights 把保留的键灌进空壳
        ▼
Eagle3DraftModel（含已填好的 d2t buffer）
        │
        │  export_onnx → dynamo 导出 model.onnx
        │  write_runtime_artifacts → 抽出 d2t 写成 d2t.safetensors
        ▼
out/llm/model.onnx + out/llm/d2t.safetensors + out/llm/config.json ...
```

#### 4.2.3 源码精读

EAGLE3 draft 模型的参数树与检查点 layout 对齐关系，写在类文档字符串里：

[`Eagle3DraftModel` 模块树与检查点 layout](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/eagle3/modeling_eagle3_draft.py#L294-L307) —— 注意模块树是 `fc / layers.0 / norm / lm_head / d2t`，其中 `layers.0` 对应检查点的 `midlayer`（由 `_eagle3_key_remap` 翻译）；并明确 `embed_tokens` 不存在——draft 不自查表，而是接收 C++ 运行时从 base embedding 算好的 `inputs_embeds`。

构造函数里两个关键点：

[`fc` 投影器与 `lm_head`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/eagle3/modeling_eagle3_draft.py#L316-L332) —— `fc` 把 base 的 3 层隐状态拼接（`target_hidden * 3`）投到 draft 隐空间；`lm_head` 输出维度是 `draft_vocab_size`（压缩后的草稿词表，通常远小于目标词表）。

[`d2t` buffer 注册](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/eagle3/modeling_eagle3_draft.py#L334-L335) —— 以 `torch.zeros` 占位注册为 `int32` buffer，等 `load_weights` 从检查点 `d2t` 键填入真实映射。

前向的「融合」步骤：

[`fc` 融合 + draft 隐状态相加](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/eagle3/modeling_eagle3_draft.py#L355-L360) —— `hidden_states = fc(hidden_states_from_base)` 后与 `hidden_states_from_draft` 相加，再进单解码层。这是 EAGLE3「条件于 base 隐状态」的核心。

EAGLE3 注意力的一个特殊形状约束——Q/K/V 的输入特征维是 `2 * hidden_size`：

[`qkv_in_features = hidden_size * 2`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/eagle3/modeling_eagle3_draft.py#L76-L104) —— 因为解码层在进 attention 前，会把归一化后的 `inputs_embeds` 与 `hidden_states` 在特征维拼接成 `[batch, seq, 2*hidden]`（见 [`Eagle3DecoderLayer.forward` 的拼接](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/eagle3/modeling_eagle3_draft.py#L229-L234)），所以 Q/K/V 投影的输入必须容纳这个拼接结果。

**d2t 的来龙去脉（本讲重点之一）**：

d2t 是一个 `[draft_vocab_size]` 的整型映射表，把 draft 词表里的第 i 个 token 映射到目标词表里的某个 token id。它是 EAGLE3 训练阶段为「用一个更小的草稿词表加速 draft 前向」而训练出来的，**作为训练产物存在检查点的 `d2t` 键里**。导出阶段做两件事：

1. **加载**：`_eagle3_key_remap` 保留 `d2t` 键（丢弃反向的 `t2d`），`load_weights` 把它灌进模型的 `d2t` buffer（占位零张量被真实映射覆盖）。
2. **再导出为 sidecar**：`write_runtime_artifacts` 在导出末尾把 buffer 抽出来单写一个文件：

[写出 `d2t.safetensors`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/checkpoint_utils.py#L911-L916) —— 把 `model.d2t` 搬到 CPU、转 `int32`，用 `save_file` 写成 `d2t.safetensors`，供 C++ 运行时在采样后做「draft token → target token」翻译。

同时，draft 的运行时配置里会写明 draft 词表大小与 base 隐状态拼接宽度：

[EAGLE3 draft 运行时配置字段](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/checkpoint_utils.py#L524-L530) —— `draft_vocab_size` 与 `base_model_hidden_size = target_hidden * 3`（因为 fc 吃的是 3 层拼接）。

> **关键澄清**：d2t **映射本身**是训练产物，导出端不生成它，只是「读进来 → 再写出去」。所谓「d2t 在导出阶段产出」，准确说是「在 `write_runtime_artifacts` 阶段落盘成 sidecar」，而非「导出阶段计算出映射」。

#### 4.2.4 代码实践

**实践目标**：追踪 EAGLE3 draft 从检查点到可加载权重的 remap 路径，并定位 d2t 的落盘点。

**操作步骤**：

1. 阅读官方 EAGLE3 draft 检查点的 layout（[类文档字符串已列出](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/eagle3/modeling_eagle3_draft.py#L25-L38)），记下训练产物键：`d2t`、`fc.weight`、`lm_head.weight`、`norm.weight`、`midlayer.*`。
2. 对每个键，手动套用 [`_eagle3_key_remap`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L536-L558)，写出 remap 后的键名。
3. 对照 [`Eagle3DraftModel.__init__`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/eagle3/modeling_eagle3_draft.py#L309-L335) 的模块树，确认每个 remap 后的键都能找到归属参数/buffer。
4. 定位 [`write_runtime_artifacts` 写 d2t 的行](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/checkpoint_utils.py#L911-L916)，确认它只对 `model.d2t` 存在时才写。

**预期结果（手动 remap 表）**：

| 检查点原始键 | remap 后 | 归属 | 说明 |
|--------------|----------|------|------|
| `d2t` | `d2t` | buffer `d2t` | 保留（`"d2t" not in key` 为 False，不跳过） |
| `t2d` | `None`（跳过） | — | 反向映射，运行时不用 |
| `target_model.*` | `None`（跳过） | — | 多目标训练产物 |
| `midlayer.input_layernorm.weight` | `layers.0.input_layernorm.weight` | 层 0 的 norm | 前缀替换 |
| `midlayer.self_attn.qkv_proj.q_proj.weight` | `layers.0.self_attn.q_proj.weight` | 层 0 的 q_proj | 前缀替换 + 拆 qkv 嵌套 |
| `fc.weight` | `fc.weight` | fc 投影器 | 原样 |
| `lm_head.weight` | `lm_head.weight` | lm_head | 原样 |

若你手头有真实 EAGLE3 检查点，可用 `safetensors` 的 `safe_open` 列出全部键，逐一套 remap 验证；若无则按上表理解即可。

#### 4.2.5 小练习与答案

**练习 1**：EAGLE3 draft 为什么不需要在 `scripts/export.py` 的阶段表里加一行 `eagle3_draft`？

**参考答案**：因为它走默认的 `thinker` 阶段——把 draft 检查点目录当普通模型目录传给 `export`，`from_pretrained` 内部凭 `draft_vocab_size` 自动识别（[`is_eagle3_draft`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L699-L701)），换用 `Eagle3DraftModel` 并装配 `_eagle3_key_remap`。相比之下 MTP/DFlash draft 是「派生/配对」的，需要专门的阶段函数（`_export_mtp_draft` / `_export_dflash_draft`）从 base 检查点拉取信息。

**练习 2**：`d2t` 为什么用 `register_buffer` 而不是 `nn.Parameter` 或独立的 sidecar 加载逻辑？

**参考答案**：d2t 是「不可训练的查表数据」，不是梯度参数，故用 buffer。用 buffer 的好处是它能像普通权重一样被 `load_weights` 按 key 灌入（键名同为 `d2t`），统一走 [`_set_tensor`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/loader.py) 路径，无需为它写特例加载代码；导出末尾再由 `write_runtime_artifacts` 抽出写成 sidecar。

---

### 4.3 DFlash draft 导出与 lm_head 共享

#### 4.3.1 概念说明

DFlash 是块状 / Jacobi 式投机解码（u7-l1）：draft 一次前向直接生成一整个 block 的候选 token。与 EAGLE3 不同，DFlash draft 是 z-lab **专门配对发布**的独立检查点，且其草稿架构是「模型族专属」的——draft 会消费 base 选定若干层的拼接隐状态，并把目标 K/V 增量更新进自己的 draft KV 缓存。

DFlash draft 导出有两个核心议题：

1. **配置从哪来**：draft 的 `target_layer_ids`、`block_size`、`mask_token_id` 藏在 draft 检查点的 `dflash_config` 里，要由 `make_dflash_draft_config` 解析出来。
2. **lm_head 共享**：老的 DFlash draft 检查点可能不带 `lm_head.*`，需要从 base 检查点「借」lm_head（甚至继承其量化布局）。

#### 4.3.2 核心流程

DFlash draft 导出数据流：

```text
base 检查点（提供 target_layer_ids 兜底 / 量化 lm_head 兜底）
   +
DFlash draft 检查点（z-lab，含 dflash_config / 自身权重，可能缺 lm_head）
        │
        │  tensorrt-edgellm-export <base_dir> <out> --dflash-draft --dflash-draft-dir <draft_dir>
        ▼
from_pretrained:
  dflash_draft=True, dflash_draft_dir=<draft_dir>
  → variant = "dflash_draft"
  → make_dflash_draft_config(draft_dir)   解析 dflash_config + draft 量化
  → model_class = DFlashDraftModel
  → key_remap    = _dflash_key_remap       （仅丢 rotary_emb）
  → 若 draft 缺 lm_head：pre_repack_hook 从 base 加载 lm_head
        │
        ▼
export_onnx → model.onnx（落到 dflash_draft/ 子目录）
```

注意：命令行里 `--dflash-draft` 触发后，[`_draft_only = args.dflash_draft`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py#L2685)，只有 dflash_draft 阶段会跑，thinker 等阶段被跳过。

#### 4.3.3 源码精读

DFlash draft 模型的参数树：

[`DFlashDraftModel` 模块树](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/dflash/modeling_dflash_draft.py#L306-L318) —— `fc`（融合多目标层隐状态）/ `hidden_norm` / `layers.0..4`（5 个缓存解码层）/ `norm` / `lm_head`（与 base 共享）。

两个强约束。第一，`fc` 必须保持 dense FP16：

[`fc` 强制 FP16 校验](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/dflash/modeling_dflash_draft.py#L325-L334) —— 因为目标隐状态数值范围很大（注释指出 Qwen3-8B 的某些首 token 通道 `abs` 可超 2e4），fc 投影必须在 FP32 下做，若 fc 本身被量化就会丢精度。

第二，前向里 fc 投影被强制升 FP32 计算：

[fc 投影全程 FP32](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/dflash/modeling_dflash_draft.py#L368-L376) —— 先把输入与权重都 `.to(torch.float32)` 再做 `F.linear`，注释强调「把已经溢出的 FP16 输出再转回 FP32 就太晚了」。该类还设了 [`match_fp32_elementwise_initializers = True`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/dflash/modeling_dflash_draft.py#L317) 让导出器对相关 initializer 做 FP32 逐元素匹配修正。

DFlash 的注意力是「缓存路径」：先用 `dflash_target_kv_cache_update` 插件把目标 K/V 增量写进 draft KV 缓存，再用 `attention_plugin` 跑 proposal 自注意力（开 tree attention）：

[`DFlashCachedAttention.forward` 的两段式](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/dflash/modeling_dflash_draft.py#L157-L202) —— 目标 delta 的 K/V 投影后经 `k_norm`，再调 `dflash_target_kv_cache_update` 更新缓存；proposal 自身的 Q/K/V 另算，与更新后的缓存一起进 `attention_plugin`。

**配置派生**：`make_dflash_draft_config` 从 draft 检查点读 `dflash_config`，并解析 draft 自身的量化（如 NVFP4）：

[`make_dflash_draft_config`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L1119-L1173) —— 读 `target_layer_ids`（默认 `[1,8,15,22,29]`）、`block_size`（默认 16）、`mask_token_id`（默认 248070），置 `is_dflash_draft_flag=True`，并调 `_parse_quant` 让 `make_linear` 产出正确的量化 Linear 类。

**lm_head 共享（本模块难点）**：导出端先探测 draft 检查点是否自带 lm_head：

[`_checkpoint_has_dflash_lm_head`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L745-L753) —— 对 draft 检查点的每个键套 `_dflash_key_remap`，若 remap 后存在以 `lm_head.` 开头的键，则判定 draft 自带 lm_head。

随后在 `from_pretrained` 的 dflash_draft 分支里，按是否自带 lm_head 分两条路：

[dflash_draft 分支的 lm_head 处理](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L270-L287) —— 若 `draft_has_lm_head` 为假，先调 `_inherit_dflash_lm_head_quant` 把 base 的 lm_head 量化布局镜像到 draft 配置（让空壳长出正确形状的量化 lm_head），再在后面挂一个 `pre_repack_hook` 从 base 加载实际权重。

镜像量化布局的意义：

[`_inherit_dflash_lm_head_quant`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L433-L467) —— 仅当 base 的 lm_head 非 FP16 时才动作：把 base 的量化类型、group_size、gptq 零点偏移写进 draft 的 `layer_overrides["lm_head"]`，使 draft 的 lm_head 空壳与 base 侧的张量布局一致，后续 `_load_dflash_lm_head` 才能正确拷贝量化张量。

真正从 base 拷贝 lm_head 权重的是 `pre_repack_hook`：

[挂载 `_load_dflash_lm_head` 为 pre_repack_hook](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L380-L392) —— 在 `load_weights` 主流程跑之前执行，从 base 检查点读取 lm_head（dense 走 [`_load_dflash_lm_head`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L561-L658)，量化走 `_load_dflash_quantized_lm_head`），优先用显式 `lm_head.weight`，tie 模式才回退到 embedding。

> 设计动机：DFlash draft 是为 base 量身配对的，draft 复用 base 的输出头天经地义——既省一份大词表 lm_head 的存储/显存，又保证 draft 与 base 用同一套 logits 空间。老检查点缺 lm_head 时，导出端负责「补齐」。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：理清 DFlash draft 的 lm_head 「探测 → 镜像量化 → 借权重」三步链。

**操作步骤**：

1. 读 [`_checkpoint_has_dflash_lm_head`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L745-L753)，确认它对「自带 lm_head」与「缺失 lm_head」的判定差异。
2. 读 [`dflash_draft 分支`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L270-L287)，回答：当 draft 自带 lm_head 时，`_inherit_dflash_lm_head_quant` 还会执行吗？（答案：不会，`if not draft_has_lm_head` 守卫。）
3. 读 [`_load_dflash_lm_head`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L561-L658) 的源选择优先级：① 显式 `lm_head.weight`；② 仅当 `tie_word_embeddings=True` 才回退 embedding；③ 否则报错。

**需要观察的现象 / 预期结果**：

- 自带 lm_head 的 draft（如带 NVFP4 lm_head 的量化 draft）：直接由通用 `load_weights` 加载，导出日志会打 `DFlash lm_head source: draft checkpoint buffers`（见 [`model.py:401-404`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L401-L404)）。
- 缺 lm_head 的老 draft：由 `pre_repack_hook` 从 base 补齐，dense 走 embedding 回退或显式 lm_head，量化走 base 的量化 sidecar 张量。

若你不确定某条路径，明确标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 DFlash draft 的 `fc` 必须排除在量化之外，而 lm_head 却可以被量化？

**参考答案**：`fc` 直接吃 base 的目标隐状态，数值动态范围极大（Qwen3-8B 某些通道 abs 超 2e4），量化会立刻溢出丢精度，故构造期就 [`raise ValueError`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/dflash/modeling_dflash_draft.py#L330-L334) 强制 FP16。lm_head 吃的是 draft 解码层经 norm 后的隐状态，数值范围受控，可量化（NVFP4 lm_head 已验证可行），且与 base 共享同一量化 lm_head 还能省显存。

**练习 2**：`make_dflash_draft_config` 为什么要自己调 `_parse_quant`，而不是像 MTP 那样从 base 继承？

**参考答案**：DFlash draft 是**独立**的配对检查点，它自带量化配置（如 z-lab 发布的 NVFP4 draft 在 draft 目录里有 `hf_quant_config.json`），其量化与 base 无关，必须从 draft 目录解析；而 Qwen3.5 MTP draft 是从 base 检查点**派生**的，draft 权重就在 base 里（`mtp.` 前缀），自然继承 base 的量化。

---

## 5. 综合实践

**任务**：以 EAGLE3 draft 为对象，完整复述「检查点训练产物 → 可加载权重 → sidecar」的全链，并对比 DFlash 在每一步的差异。本任务是本讲实践任务（追踪 EAGLE3 draft 导出路径并解释 d2t 产出阶段）的扩展版。

**步骤**：

1. **识别角色**。假设你拿到一个 EAGLE3 draft 检查点目录。在不加任何 `--eagle-*` / `--mtp` / `--dflash-*` 开关的情况下直接 `tensorrt-edgellm-export <draft_dir> <out>`。
   - 追踪：`load_model_config` 读出 `draft_vocab_size` → [`is_eagle3_draft`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/config.py#L699-L701) 为真 → [`_resolve_model_variant`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L513-L518) 返回 `"eagle3_draft"`。

2. **重映射权重**。对照 4.2.4 的手动 remap 表，说明 `_eagle3_key_remap` 如何把 `midlayer.*`、`qkv_proj.*`、`t2d`、`target_model.*` 分别处理。重点说清：`t2d` 被丢、`d2t` 被留（[`第 549 行条件`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L549)）。

3. **定位 d2t 产出阶段**。回答：d2t 映射是训练产物（非导出计算），在导出流程的 **`write_runtime_artifacts` 阶段**被从 buffer 抽出写成 [`d2t.safetensors`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/checkpoint_utils.py#L911-L916) sidecar；同时把 `draft_vocab_size` / `base_model_hidden_size` 写进运行时 [`config.json`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/checkpoint_utils.py#L524-L530)。

4. **横向对比 DFlash**。对 DFlash draft 重做 1-3 步，回答：
   - 角色识别靠开关（`--dflash-draft`）而非自报告；
   - key remap 几乎空操作（仅丢 `rotary_emb`），但多了一步「探测 lm_head → 镜像量化 → 从 base 借权重」；
   - 无 d2t 概念（DFlash 用完整目标词表，[`speculative-decoding.md` 明确说 DFlash 不用 d2t`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/user_guide/examples/speculative-decoding.md#L515-L515)）。

**预期产出**：一张四列对比表（步骤 / EAGLE3 / DFlash / 关键源码链接），能向他人讲清「同一个 `from_pretrained` 入口如何按变体分流处理四类完全不同的草稿检查点」。若你能在本机用最小 EAGLE3 draft（如 `AngelSlim/Qwen3-1.7B_eagle3`）跑一次 `export` 并在输出目录确认 `d2t.safetensors` 存在，则最佳；否则按源码阅读完成即可，并标注「待本地验证」。

## 6. 本讲小结

- **变体解析是入口**：`_resolve_model_variant` 用「互斥校验 + 优先级短路」从一堆布尔开关与检查点字段里裁决唯一变体标签；EAGLE3 draft 是唯一自报告变体（`draft_vocab_size`），其余皆开关驱动。
- **四类 remap 规则各异**：EAGLE3 改名最多（`midlayer→layers.0`、拆 `qkv_proj`、丢 `t2d`/`target_model`）；MTP 剥 `mtp.` 前缀并在 tie 时借 embedding 当 lm_head；DFlash 几乎空操作（仅丢 `rotary_emb`）；Gemma4-MTP 不 remap。
- **EAGLE3 draft 走默认 thinker 阶段**：无专属导出阶段，靠 `is_eagle3_draft` 自动识别；其 `d2t` 是训练产物，在 `write_runtime_artifacts` 阶段落盘成 `d2t.safetensors`。
- **DFlash draft 是配对独立检查点**：配置（`target_layer_ids`/`block_size`/`mask_token_id`）从 draft 的 `dflash_config` 读；lm_head 可从 base 借（探测 → 镜像量化 → pre_repack_hook 加载）；`fc` 强制 FP16 以容纳大动态范围的目标隐状态。
- **draft-to-target（d2t）词表映射**：仅 EAGLE3 用，把压缩草稿词表的 token 翻译回目标词表；导出端只搬运不计算，落盘后由 C++ 运行时在采样后消费。
- **MTP vs DFlash 的来源差异决定配置策略**：MTP draft 从 base 派生（继承 base 量化），DFlash draft 独立（从自身目录解析量化）。

## 7. 下一步学习建议

- **回到运行时侧验证闭环**：阅读 [u7-l1](u7-l1-speculative-decoding-strategies.md) 的 `eagleDecoder.cpp` / `dflashDecoder.cpp`，确认本讲导出的 `d2t.safetensors`、`spec_draft.engine`、共享 lm_head 如何被 `decodeStep` 消费。
- **量化对 draft 的影响**：结合 [u3-l2 量化 CLI](u3-l2-quantization-cli-and-formats.md) 与 [u3-l3 量化权重格式](u3-l3-quantized-weight-formats.md)，阅读 `tensorrt_edgellm/quantization/models/eagle3_draft.py` 与 `dflash_draft.py`，理解 draft 量化（fp8/nvfp4）如何在导出前改变检查点，进而影响本讲的 `make_dflash_draft_config` / `_inherit_dflash_lm_head_quant`。
- **端到端跑通**：照 `docs/source/user_guide/examples/speculative-decoding.md` 的 EAGLE3 与 DFlash 示例，分别完成 export → build → inference，把本讲讲的每一份 sidecar（`d2t.safetensors`、共享 lm_head、draft `config.json`）与构建/推理命令的参数对应起来。
- **新模型接入**：若你想接入一种新的草稿架构，重点参照 4.1 的变体解析扩展点（新增一个布尔开关 + 在 `_resolve_model_variant` 加互斥校验与优先级分支）与 4.2/4.3 的「模型类 + key remap」配对模式。
