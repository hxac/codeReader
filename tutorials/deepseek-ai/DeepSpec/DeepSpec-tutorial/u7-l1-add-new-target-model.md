# 扩展实践：为一个新目标模型族接入 DeepSpec

## 1. 本讲目标

学完本讲，你应该能够：

1. 脱口而出「为一个新目标模型族接入 DeepSpec」需要新建和修改的**全部文件清单**，不遗漏任何一处注册点。
2. 解释 `build_draft_config` 这个「合同转换器」如何把目标模型 config 派生成草稿 config，以及为什么 `architectures` 字段是贯穿训练与评估的「总线」。
3. 解释 `initialize_embeddings_and_head` 这个接入点为什么存在、由谁调用、新模型族需要满足什么形状契约。
4. 写出一个最小 trainer 子类与 evaluator 子类——各只需覆盖一个类属性或一个方法。
5. 独立完成一份以 Llama（或任意 HF 模型族）为目标的接入设计文档。

本讲是第 7 单元（扩展与二次开发实战）的第一篇。前面六个单元你已经读完了数据流水线、训练框架、三种草稿算法与评估系统；本讲把这些知识「倒过来用」：不再是理解现有代码，而是回答「如果我想让 DeepSpec 加速一个它还不支持的目标模型，我要动哪些地方」。

## 2. 前置知识

本讲假设你已完成 u4（DSpark 建模）与 u6（评估系统）。下面几个概念会反复用到，先简要回顾与补足：

- **模型族（model family）**：基于 Hugging Face `transformers` 的预训练家族，如 Qwen3、Gemma4。DeepSpec 中「模型相关层」指 `deepspec/modeling/dspark/<family>/` 下的 modeling 与 config，「模型无关层」指 `common.py`、`loss.py`、`markov_head.py` 等只操作抽象张量的模块（见 u4-l5）。
- **模板方法模式**：`BaseTrainer` 与 `BaseEvaluator` 固化流程骨架，把「模型族差异」下沉为少数几个钩子方法，子类只填空、不重写流程（见 u3-l1、u6-l1）。
- **配置即代码**：训练配置是普通 Python 文件，里面可以直接存放类对象（如 `trainer_cls=Qwen3DSparkTrainer`），`load_config` 用 `importlib` 执行它并收集顶层名字（见 u1-l4）。
- **`AutoConfig` / `AutoModel` 的分发机制**（本讲新补足）：transformers 的 `AutoConfig.from_pretrained(path)` 会读取 checkpoint 里的 `config.json`；`AutoModelForCausalLM.from_pretrained(path)` 则按 `config.json` 的 `architectures` 列表（如 `["Qwen3ForCausalLM"]`）在注册表中查找同名模型类来实例化。DeepSpec 评估侧的 `EVALUATORS` 字典正是模仿了这套「按架构名分发」的思路——理解这一点，就理解了为什么新模型族必须在 `build_draft_config` 里写入自己的架构名。
- **`from_pretrained` 的前提**：草稿模型类要能用 `SomeModel.from_pretrained(checkpoint)` 加载，它必须继承对应家族的 `PreTrainedModel`（如 `Qwen3PreTrainedModel`），这样权重键名映射、`post_init` 等基础设施才是现成的。

## 3. 本讲源码地图

| 文件 | 作用 | 在接入任务中的角色 |
|---|---|---|
| `deepspec/modeling/dspark/qwen3/config.py` | Qwen3 草稿 config 派生 | **新建** `<family>/config.py` 的模板 A |
| `deepspec/modeling/dspark/gemma4/config.py` | Gemma4 草稿 config 派生（含嵌套 text_config 处理） | 模板 B（多模态/嵌套 config 时参考） |
| `deepspec/modeling/dspark/qwen3/modeling.py` | Qwen3DSparkModel 本体 | **新建** `<family>/modeling.py` 的模板 |
| `deepspec/trainer/dspark_trainer.py` | 两个 DSpark trainer 子类 | **新建/扩展** trainer 子类的模板 |
| `deepspec/trainer/base_trainer.py` | 训练骨架，含 `build_models` | 理解 `initialize_embeddings_and_head` 调用点（不改） |
| `eval.py` | 评估入口 | **修改**：在 `EVALUATORS` 注册新 Evaluator |
| `deepspec/eval/dspark/evaluator.py` | DSpark 评估器 | **扩展**：一行子类 + `draft_model_cls` |
| `deepspec/data/parser.py` | 聊天模板注册表 | **修改**：注册新家族的 `ChatTemplate` |
| `scripts/data/prepare_target_cache.py` | 目标缓存生成 | **可能修改**：`_get_target_backbone` 的模型族特判 |
| `config/dspark/dspark_qwen3_4b.py` | Qwen3 训练配置 | **新建** `config/dspark/<family>_<size>.py` 的模板 |
| `deepspec/modeling/dspark/common.py` | 模型无关层（`validate_target_layer_ids` 等） | 只复用，不修改 |
| `deepspec/utils/constant/public.py` | 模型名与目录常量 | **修改**：加一行模型常量 |

## 4. 核心概念与源码讲解

### 4.1 接入面总览：改动清单与容易被遗漏的第七处

#### 4.1.1 概念说明

DeepSpec 的代码按「算法（dspark/eagle3）× 模型族（qwen3/gemma4）」双维度组织。算法逻辑（损失、markov 头、锚点采样、投机解码主循环、拒绝采样）全部沉在模型无关层；模型族差异被压缩到少数几个文件里。这意味着接入一个新目标模型族是**加法而非改法**：你几乎不修改任何共享代码，只是沿着 Qwen3/Gemma4 踩出的路径再走一遍。

具体地说，接入一个新家族（以 DSpark 算法为例）需要：

**新建 4 个文件：**

1. `deepspec/modeling/dspark/<family>/__init__.py` —— 导出模型类与 `build_draft_config`；
2. `deepspec/modeling/dspark/<family>/config.py` —— draft config 派生；
3. `deepspec/modeling/dspark/<family>/modeling.py` —— 草稿模型本体；
4. `config/dspark/dspark_<family>_<size>.py` —— 训练配置。

**修改 4 处注册：**

5. `deepspec/trainer/dspark_trainer.py` —— 新增 trainer 子类（或仅在 `deepspec/trainer/__init__.py` 补导出）；
6. `eval.py` —— 在 `EVALUATORS` 字典加一行；
7. `deepspec/data/parser.py` —— 在 `TEMPLATE_REGISTRY` 注册聊天模板；
8. `deepspec/utils/constant/public.py` —— 加模型名常量（可选但推荐）。

**外加一处容易被遗漏的特判：**

9. `scripts/data/prepare_target_cache.py` 的 `_get_target_backbone` —— 只有当目标模型的 backbone **不是** `model.layers` 这一标准布局（如 Gemma4 的嵌套 `language_model`）时才需要改。

#### 4.1.2 核心流程

新家族接入后的「首次训练」全链路：

```text
config/dspark/dspark_<family>.py        训练配置：指明目标模型名、target_layer_ids、trainer_cls
        │
        ▼
train.py ──► load_config ──► BaseTrainer.__init__
        │
        ▼
BaseTrainer.build_models                 骨架（不改）
        ├── AutoConfig.from_pretrained(目标模型)      ── 拿到 target_config
        ├── <Family>DSparkTrainer._build_draft_model ── 钩子 1：调 build_draft_config + 实例化模型
        ├── AutoModelForCausalLM.from_pretrained(目标, device="cpu")
        └── draft_model.initialize_embeddings_and_head(embed, lm_head, freeze=True)
        │                                                ── 接入点：冻结复用目标模型词嵌入与输出头
        ▼
CacheDataset(读目标缓存) ──► run_batch（钩子 2）──► compute_dspark_loss（模型无关）
        │
        ▼
save_checkpoint（权重里带着 architectures=["<Family>DSparkModel"]）
        │
        ▼
eval.py ──► EVALUATORS[architectures[0]] ──► <Family>DSparkEvaluator（draft_model_cls 一行子类）
```

#### 4.1.3 源码精读

先看「一行子类」能做到什么程度。Gemma4 的 trainer 只覆盖了一个方法：

- [deepspec/trainer/dspark_trainer.py:L42-L48](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/dspark_trainer.py#L42-L48)：`Gemma4DSparkTrainer` 继承 `Qwen3DSparkTrainer`，仅把 `_build_draft_model` 换成 Gemma4 版 `build_draft_config` + `Gemma4DSparkModel`。训练步 `run_batch`、损失组合、数据整理器全部原样继承——因为它们只依赖 `DSparkForwardOutput` 这份形状合同，与模型族无关。

再看那处容易被遗漏的特判：

- [scripts/data/prepare_target_cache.py:L55-L63](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_target_cache.py#L55-L63)：`_get_target_backbone` 按 `model_type` 分派——Gemma4 走嵌套的 `language_model`，其余模型直接取 `model` 属性。**如果你的新目标模型是标准 `LlamaForCausalLM` 布局（backbone 叫 `model`、decoder 层叫 `model.layers`），这里一行都不用改**；这正是默认分支 `getattr(target_model, "model", target_model)` 存在的意义。
- [scripts/data/prepare_target_cache.py:L66-L70](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_target_cache.py#L66-L70)：`_get_target_hidden_size` 同样按 `model_type` 特判——Gemma4 的 `hidden_size` 藏在 `text_config` 里。新家族若 config 是扁平的（Qwen3/Llama 风格），无需改动。

#### 4.1.4 代码实践

**实践目标**：在动手写任何代码之前，先用「只读仓库」的方式把改动面数清楚。

**操作步骤**：

1. 打开 `eval.py` 的 `EVALUATORS` 字典与 `deepspec/trainer/__init__.py` 的 `__all__`，数一数当前每种算法 × 模型族组合各占了几个条目。
2. 用 `grep -rn "gemma4" scripts/data/prepare_target_cache.py` 找出数据侧的全部 Gemma4 特判，判断它们是「布局特判」还是「算法特判」。
3. 对照上面的 9 条清单，在纸上为「Llama 目标模型」逐条标注：新建 / 修改 / 无需改动。

**需要观察的现象**：Gemma4 在整个仓库中出现的位置远多于「两个文件」——这正是 u4-l5 所说「接入一个新家族的改动面」的具体形状。

**预期结果**：你会得出结论——Llama（标准布局、扁平 config）的改动面比 Gemma4 更小，因为两处 `model_type` 特判都可以走默认分支。**待本地验证**（步骤 2 的 grep 输出条数取决于仓库版本）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 Eagle3 算法也接上新家族，4.1.1 的清单会膨胀成什么样子？

**答案**：模型相关层要再加一套 `deepspec/modeling/eagle3/<family>/`（modeling + config + `__init__.py`）、`deepspec/trainer/eagle3_trainer.py` 里加一个子类、`eval.py` 的 `EVALUATORS` 加一行 `<Family>Eagle3Model` 键、`config/eagle3/` 加一份训练配置；4.1.1 中第 7、8、9 条（chat_template、常量、backbone 特判）**不需要重复做**，因为它们是模型族级别而非算法级别的。

**练习 2**：为什么 `_get_target_backbone` 用 `getattr(target_model, "model", target_model)` 而不是直接 `target_model.model`？

**答案**：兜底那些顶层就是 backbone 的模型（没有 `CausalLM` 外壳包装的对象，如直接加载的 backbone 模型），避免 `AttributeError`。这是一种对「标准布局」的宽容处理——`ForCausalLM` 包装类有 `.model`，裸 backbone 没有。

### 4.2 modeling/&lt;family&gt;/ 新目录：draft config 派生与 initialize_embeddings_and_head

#### 4.2.1 概念说明

这个目录是接入工作的主体，包含两个文件：

- **`config.py` 的 `build_draft_config(target_config, model_args)`** 是一份「合同转换器」：输入目标模型 config 与训练配置里的 `model` 字典，输出草稿模型 config。它决定草稿模型「长什么样、叫什么名字、用哪几层目标特征」。
- **`modeling.py` 的草稿模型类**继承目标家族的 `PreTrainedModel`，复用 HF 原件（RMSNorm、RoPE、MLP），只重写注意力层以实现 DSpark 的双源 K/V（u4-l2 已详述）。

而 `initialize_embeddings_and_head` 是草稿模型必须提供的**形状契约方法**：训练开始前，`BaseTrainer` 会把目标模型的词嵌入和输出头权重拷进来并冻结（u3-l1 讲过其动机——钉死输入输出接口、省下可训练参数的主权重与动量）。新家族的模型类必须实现它，且形状断言必须成立：草稿模型与目标模型的 `vocab_size`、`hidden_size` 必须一致（这是 DSpark 复用目标 lm_head 做蒸馏对齐的前提）。

#### 4.2.2 核心流程

`build_draft_config` 的派生流水线（两族共同部分）：

```text
输入: target_config（目标模型 config）, model_args（训练配置的 model 字典）
  1. 取得文本 backbone config
       Qwen3:  直接用 target_config（扁平）
       Gemma4: 提取嵌套 text_config 并深拷贝
  2. 校验 model_args 必填项
       target_layer_ids ──► validate_target_layer_ids（升序、不含末层、-1 表示 embedding）
       markov_rank / confidence_head_alpha 的配套字段
  3. copy.deepcopy 后逐字段覆写:
       architectures    ← ["<Family>DSparkModel"]      ★ 评估分发的键
       num_hidden_layers← num_draft_layers             ★ 草稿层数（如 5）
       target_layer_ids / block_size / mask_token_id / num_anchors
       tie_word_embeddings ← False                     ★ 输出头必须独立权重对象
       _attn_implementation ← "flex_attention"
输出: draft_config（可被 <Family>DSparkModel(config) 直接消费）
```

#### 4.2.3 源码精读

**模板 A —— Qwen3 版（扁平 config 的最简形态）**：

- [deepspec/modeling/dspark/qwen3/config.py:L37-L47](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/config.py#L37-L47)：`copy.deepcopy(target_config)` 之后连续覆写。注意 [L38](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/config.py#L38) 写入 `architectures = ["Qwen3DSparkModel"]`——这个字符串将随 checkpoint 落盘，成为评估侧 `EVALUATORS` 查表的键；[L42](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/config.py#L42) 强制 `tie_word_embeddings = False`，因为草稿模型要独立持有（冻结的）embedding 与 lm_head 两个权重对象以便分别拷贝。
- [deepspec/modeling/dspark/common.py:L59-L73](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py#L59-L73)：`validate_target_layer_ids` 是模型无关的层号闸门：允许 `-1`（embedding 输出，u2-l5 讲过的哨兵层号）与 `[0, num_layers-2]` 区间，要求严格递增。注意上界是 `num_target_layers - 1` 减一——**末层被禁止**，因为末层隐状态会经过 final norm，与协议要求的 raw 层输出语义不一致（u6-l1 的 `assert_no_final_target_layer` 是评估侧的对称校验）。

**模板 B —— Gemma4 版（嵌套 config 的处理方式）**：

- [deepspec/modeling/dspark/gemma4/config.py:L9-L19](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/gemma4/config.py#L9-L19)：`get_gemma4_text_config` 从顶层 config 提取 `text_config` 并断言 `model_type`——多模态/嵌套 config 的家族必须先「剥壳」拿到纯文本 backbone config，草稿 config 才能直接继承文本建模超参。
- [deepspec/modeling/dspark/gemma4/config.py:L22-L49](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/gemma4/config.py#L22-L49)：`_validate_required_text_fields` 校验 21 个必填字段。这是「快速失败」设计：Gemma4 的 config 字段众多且可选，建模代码却无条件依赖它们（如 `attention_k_eq_v`、`global_head_dim`），入口处一次性验完，胜过训练中途 KeyError。
- [deepspec/modeling/dspark/gemma4/config.py:L82-L84](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/gemma4/config.py#L82-L84)：除 `architectures = ["Gemma4DSparkModel"]` 外，还写入 `target_model_type` / `target_text_model_type` 两个**溯源字段**——因为深拷贝把 `model_type` 变成了文本配置的类型，原始顶层类型需要另存，评估与缓存校验可据此追溯目标身份。

**模型本体的接入契约**：

- [deepspec/modeling/dspark/qwen3/modeling.py:L207-L224](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L207-L224)：`__init__` 开头对 5 个必填 config 字段做断言，并校验 `markov_rank > 0`、`enable_confidence_head` 的配套字段——与 `build_draft_config` 的写入端一一对应，构成「写入端保证、读取端复核」的双保险。你的新家族建模文件应保留同样的防御。
- [deepspec/modeling/dspark/qwen3/modeling.py:L270-L283](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L270-L283)：`initialize_embeddings_and_head` 的全部逻辑——形状断言、`torch.no_grad()` 下 `copy_`、按 `freeze` 调 `set_embedding_head_trainable(False)`。**这就是新模型族必须实现的接入点**：签名固定（关键字参数 `embed_tokens`、`lm_head`、`freeze`），由 `BaseTrainer.build_models` 统一调用。
- [deepspec/modeling/dspark/gemma4/modeling.py:L50-L81](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/gemma4/modeling.py#L50-L81)：Gemma4 注意力与新家族差异的典型样本——`scaling = 1.0`（注意力缩放融入权重初始化）、`attention_k_eq_v` 模式下 `v_proj = None`（V 直接复用 K）、新增 `v_norm`。这些差异点全部来自 HF 原模型定义，接入新家族时你需要逐项对照原模型的 `modeling_<family>.py` 找出对应实现（u4-l5 给出了完整的差异清单方法）。

#### 4.2.4 代码实践

**实践目标**：不写新家族代码，先用现有 Qwen3 链路验证「draft config 派生 → 模型实例化 → 权重拷贝」三步契约，确认你理解了每个接入点的输入输出。

**操作步骤**（示例代码，非项目原有）：

```python
# 示例代码：在仓库根目录用 python -i 交互执行
import torch
from transformers import AutoConfig, AutoModelForCausalLM
from deepspec.modeling.dspark.qwen3 import Qwen3DSparkModel, build_draft_config

target = "Qwen/Qwen3-4B"
target_config = AutoConfig.from_pretrained(target)
model_args = dict(
    num_draft_layers=5, target_layer_ids=[1, 9, 17, 25, 33],
    block_size=7, mask_token_id=151669, num_anchors=512,
    markov_rank=256, markov_head_type="vanilla",
    confidence_head_alpha=1.0, confidence_head_with_markov=True,
)
draft_config = build_draft_config(target_config, model_args)
print(draft_config.architectures, draft_config.num_hidden_layers,
      draft_config.tie_word_embeddings)

draft = Qwen3DSparkModel(draft_config)
target_model = AutoModelForCausalLM.from_pretrained(target, dtype=torch.bfloat16)
draft.initialize_embeddings_and_head(
    embed_tokens=target_model.get_input_embeddings(),
    lm_head=target_model.get_output_embeddings(),
    freeze=True,
)
print(draft.embed_tokens.weight.requires_grad, draft.lm_head.weight.requires_grad)
```

**需要观察的现象**：打印出的 `architectures`、草稿层数、`tie_word_embeddings=False`；拷贝后两个权重张量的 `requires_grad` 均为 `False`。

**预期结果**：三步全部通过即证明契约理解正确。随后可做破坏性实验：把 `model_args` 里的 `target_layer_ids` 改成含末层（如 `[1, 9, 35]`，Qwen3-4B 共 36 层，末层号 35）或乱序，观察 `validate_target_layer_ids` 的断言报错。**待本地验证**（需要能访问 Hugging Face Hub 下载 Qwen3-4B 权重；若只验证 config 派生，第一步 `AutoConfig.from_pretrained` 下载量很小）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `build_draft_config` 必须先 `copy.deepcopy(target_config)`，而不是直接在 `target_config` 上改？

**答案**：`target_config` 是 `AutoConfig.from_pretrained` 的返回对象，训练进程后续不会再用它，但深拷贝保证了派生操作的纯函数性——不同 rank 各自调用 `build_draft_config` 时不会互相污染，也不会意外修改可能被共享/缓存的目标 config 对象；Gemma4 版对 `text_config` 的深拷贝同理。

**练习 2**：新家族的 `mask_token_id` 应该怎么选？Qwen3 配置里是 151669、Gemma4 配置里是 4，这两个数字的来源是什么？

**答案**：应选择目标模型词表中一个**不会出现在正常生成文本里的保留 token**（通常是 tokenizer 自带的 mask/占位 token），因为 DSpark 用它构造噪声块（u4-l1）。来源是目标模型 tokenizer 的词表：配置文件里的数字必须与目标模型 tokenizer 中该保留 token 的 id 一致，且草稿模型 embedding 的这一行会随 `initialize_embeddings_and_head` 从目标模型拷贝过来，保证 mask token 有合法的 embedding 向量。

**练习 3**：如果新目标模型的 `hidden_size` 与 `vocab_size` 和 Qwen3 完全不同，`initialize_embeddings_and_head` 里会发生什么？

**答案**：两行形状断言会立刻失败。这是刻意设计——DSpark 的蒸馏对齐要求草稿与目标共用同一个 lm_head（`aligned_target_logits` 与 `draft_logits` 同 vocab、同 hidden），形状不一致说明草稿结构派生有误，应该在第一时间暴露而不是训练中途崩掉。

### 4.3 trainer 子类与训练配置文件

#### 4.3.1 概念说明

trainer 子类是「模型族进入训练框架」的适配器。`BaseTrainer` 用模板方法模式固化了整个训练流程（u3-l1、u3-l2），只留下两个抽象钩子：

- `_build_draft_model(*, target_config, model_args)`：给定目标 config 与模型参数，返回草稿模型实例——**这是模型族唯一必填的钩子**；
- `run_batch(batch)`：给定一个 batch，返回 loss——**这是算法必填的钩子**，DSpark 两族共享同一实现。

于是出现了一个漂亮的层级结构：`Qwen3DSparkTrainer` 填了两个钩子（既定模型族又定算法），`Gemma4DSparkTrainer` 只需在其上换掉 `_build_draft_model`。**接入新家族时，你的 trainer 子类大概率也只有 6 行**。

训练配置文件（`config/dspark/dspark_<family>_<size>.py`）则是把新家族「登记进系统」的入口：它指明目标模型名、`target_layer_ids`、`mask_token_id`、`trainer_cls`、`chat_template` 等模型族相关超参，其余（lr、batch、epochs）可原样照抄。

#### 4.3.2 核心流程

trainer 子类被使用的完整时序：

```text
train.py: spawn 子进程 ──► load_config("config/dspark/dspark_<family>.py")
        │                    配置里带着 trainer_cls = <Family>DSparkTrainer（类对象本身）
        ▼
        trainer_cls(local_rank=local_rank, args=cfg)   # 配置即代码：直接实例化
        │
        ▼
BaseTrainer.__init__  …  self.build_models()
        │
        ▼
self._build_draft_model(target_config=…, model_args=cfg.model)   # 你的钩子被回调
        │   build_draft_config(target_config, model_args) ──► <Family>DSparkModel(draft_config)
        ▼
BaseTrainer.build_models 继续执行 initialize_embeddings_and_head / FSDP / compile
        │
        ▼
train() 主循环 ──► self.run_batch(batch)   # DSpark 家族共享的实现
```

#### 4.3.3 源码精读

- [deepspec/trainer/dspark_trainer.py:L14-L39](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/dspark_trainer.py#L14-L39)：`Qwen3DSparkTrainer` 全文——类属性 `data_collator_cls = CacheCollator`（指定数据整理器），`_build_draft_model` 调 Qwen3 版 `build_draft_config` 并返回 `Qwen3DSparkModel`，`run_batch` 前向草稿模型后把输出交给模型无关的 `compute_dspark_loss`。注意 `run_batch` 读的四个 batch 键（`input_ids` / `target_hidden_states` / `loss_mask` / `target_last_hidden_states`）正是 u2-l6 缓存协议落盘的字段——**换模型族不需要动这里，换缓存协议才需要**。
- [deepspec/trainer/dspark_trainer.py:L42-L48](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/dspark_trainer.py#L42-L48)：`Gemma4DSparkTrainer`——新家族 trainer 的最小形态，仅 7 行。
- [deepspec/trainer/base_trainer.py:L261-L280](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L261-L280)：调用端视角——`build_models` 先经你的钩子拿到草稿模型搬到 GPU，再在 **CPU** 上加载完整目标模型、取出 embedding 与 lm_head、调用 `draft_model.initialize_embeddings_and_head(..., freeze=True)`，随后 `del target_model` 释放。注释点明动机：目标 checkpoint 仅用于初始化冻结的输入输出权重。新家族接入时这段骨架**完全不改**，只要你的模型实现了 4.2.3 的那个方法。
- [deepspec/trainer/base_trainer.py:L284-L285](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L284-L285)：`_build_draft_model` 的默认实现是 `raise NotImplementedError`——未填钩子的子类在构造时立即失败，而不是带着空模型跑起来。
- [config/dspark/dspark_qwen3_4b.py:L10-L30](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L10-L30) 与 [config/dspark/dspark_gemma4_12b.py:L11-L31](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_gemma4_12b.py#L11-L31)：两份配置的 `model` 字典逐字段对照，只有四处模型族差异——`target_model_name_or_path`、`target_layer_ids`（在哪几层抽特征）、`mask_token_id`、以及 markov/confidence/loss 超参原样共享。**算法超参（`block_size=7`、`num_draft_layers=5`、`markov_rank=256` 等）跨族不变**。
- [config/dspark/dspark_qwen3_4b.py:L32-L45](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L32-L45) 与 [config/dspark/dspark_gemma4_12b.py:L42-L46](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_gemma4_12b.py#L42-L46)：`train` 字典差异仅两处——`trainer_cls` 与 `torch_compile`（Gemma4 关闭编译，属工程调优而非结构差异）。
- [config/dspark/dspark_qwen3_4b.py:L52-L57](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L52-L57) 与 [config/dspark/dspark_gemma4_12b.py:L52-L58](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_gemma4_12b.py#L52-L58)：`data.chat_template` 分别为 `"qwen"` 与 `"gemma4"`——这个字符串将在数据侧查 `TEMPLATE_REGISTRY`（见 4.4）。
- [deepspec/utils/constant/public.py:L7-L12](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/constant/public.py#L7-L12)：模型名常量与 checkpoint/TensorBoard 根目录。接入新家族时在这里加一行 `<FAMILY>_<SIZE> = "org/name"`，配置文件引用它，避免散落的字符串。

#### 4.3.4 代码实践

**实践目标**：亲手写出「LlamaDSparkTrainer」的完整代码（不落地，仅作为设计稿），并对照两份现有配置写出新配置文件的 diff。

**操作步骤**：

1. 仿照 [dspark_trainer.py:L14-L39](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/dspark_trainer.py#L14-L39) 的 `Qwen3DSparkTrainer`，写出 `LlamaDSparkTrainer`：`_build_draft_model` 调 `build_llama_draft_config` 并返回 `LlamaDSparkModel`；`run_batch` 与 `data_collator_cls` 原样继承。
2. 复制 `config/dspark/dspark_qwen3_4b.py` 为 `config/dspark/dspark_llama_8b.py`（设计稿），逐字段标注「改 / 不改」。
3. 在 `deepspec/trainer/__init__.py`（[L1-L11](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/__init__.py#L1-L11)）中补一行导出，保证配置文件能 `from deepspec.trainer import LlamaDSparkTrainer`。

**需要观察的现象**：写完后数一数——你的新 trainer 是否不超过 10 行？新配置文件与 Qwen3 版的差异是否不超过 6 处？

**预期结果**：如果发现自己在 trainer 里写了 `run_batch` 的变体，大概率是把「模型族差异」与「算法差异」混在一起了，应退回 4.2 重新审视 modeling 层的设计。**待本地验证**（本实践是设计稿，不执行）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Gemma4DSparkTrainer` 继承的是 `Qwen3DSparkTrainer` 而不是 `BaseTrainer`？

**答案**：因为 `run_batch` 与 `data_collator_cls` 在「DSpark 算法」这一级就固定了，与模型族无关。继承 Qwen3 版意味着 Gemma4 版自动获得正确的训练步实现，只需替换模型构建钩子。这也提示了正确的抽象层级：`BaseTrainer`（流程）→ `Qwen3DSparkTrainer`（DSpark 算法步）→ 族子类（只换模型）。

**练习 2**：配置文件里 `trainer_cls=Qwen3DSparkTrainer` 存的是类对象而非字符串，这对断点续训有什么影响？

**答案**：保存 checkpoint 时配置被回写为 `train_config.py`（含 `--opts` 追加行，u3-l5），类对象经序列化后仍是可 import 的引用；恢复训练重新执行该文件时 `trainer_cls` 依然是同一个类，保证续训与首训走完全一致的构建路径。代价是配置文件依赖 `deepspec.trainer` 包可导入——这也是练习中要求补 `__init__.py` 导出的原因。

### 4.4 EVALUATORS 注册与 chat_template：评估与数据侧的最后拼图

#### 4.4.1 概念说明

前两节解决了「能训练」。训练产出的 checkpoint 要能被评估，还差两个注册点：

- **`eval.py` 的 `EVALUATORS` 字典**：评估入口按草稿 checkpoint 的 `config.architectures[0]` 查表决定用哪个 Evaluator（u1-l3、u6-l1 讲过分发机制）。新家族只需两步：在 `build_draft_config` 里写入架构名（4.2 已做），在字典里加 `"LlamaDSparkModel": LlamaDSparkEvaluator` 一行。
- **`parser.py` 的 `TEMPLATE_REGISTRY`**：数据侧按配置文件里 `data.chat_template` 字符串查表，决定如何渲染对话与计算 `loss_mask`（u2-l2）。新家族需要提供 assistant 头、结束符、system prompt、结束符等模板要素。

评估器子类本身又是一行代码：DSpark 的评估逻辑（四钩子、块提议、缓存维护，u6-l4）全部在 `Qwen3DSparkEvaluator` 里且模型族无关，Gemma4 版只是换了 `draft_model_cls` 类属性。

#### 4.4.2 核心流程

架构名作为「总线」串起训练与评估：

```text
训练侧:  build_draft_config 写 architectures = ["LlamaDSparkModel"]
              │
              ▼
          save_checkpoint 落盘 config.json（architectures 随之保存）
              │
              ▼
评估侧:  eval.py: AutoConfig.from_pretrained(draft_ckpt)
              draft_config.architectures[0] == "LlamaDSparkModel"
              │
              ▼
          EVALUATORS["LlamaDSparkModel"] ──► LlamaDSparkEvaluator
              │
              ▼
          build_models: draft_model_cls.from_pretrained(draft_ckpt)   # 类属性决定加载哪个类
```

注意一个细节：草稿模型能被 `from_pretrained` 加载，前提是它继承的 `PreTrainedModel` 提供了权重加载基础设施——这正是 4.2 要求建模文件继承家族 `PreTrainedModel` 的第二个理由（第一个是复用 HF 组件）。

#### 4.4.3 源码精读

- [eval.py:L10-L16](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval.py#L10-L16)：`EVALUATORS` 分发表——4 个架构键对应 4 个 Evaluator，外加 1 个 `Eagle3DraftModel` 兼容别名（u1-l3 讲过）。注意键名与各家族 `build_draft_config` 写入的 `architectures` 字符串严格一致，拼写错误不会在注册时报错，只会在评估时 `KeyError`。
- [eval.py:L53-L55](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval.py#L53-L55)：查表处——`AutoConfig.from_pretrained(args.draft_name_or_path)` 读 checkpoint 的 `config.json`，取 `architectures[0]` 查字典，实例化 `evaluator_cls(local_rank, args)` 后 `evaluate()`。
- [deepspec/eval/dspark/evaluator.py:L32-L34](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py#L32-L34)：`Qwen3DSparkEvaluator` 用类属性声明模型族依赖：`draft_model_cls = Qwen3DSparkModel`、`EVAL_ATTN_IMPLEMENTATION = "sdpa"`（评估用 sdpa 而非训练的 flex_attention，u6-l1 讲过原因）。
- [deepspec/eval/dspark/evaluator.py:L68-L83](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py#L68-L83)：`build_models` 钩子——目标模型走通用 `AutoModelForCausalLM`（不需要家族特判！transformers 自己按 architectures 分发），草稿模型走 `self.draft_model_cls.from_pretrained`（这里才需要家族信息），并做 `assert_no_final_target_layer` 校验。
- [deepspec/eval/dspark/evaluator.py:L224-L225](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py#L224-L225)：`Gemma4DSparkEvaluator(Qwen3DSparkEvaluator)` 全文——只覆盖 `draft_model_cls = Gemma4DSparkModel`。这就是新家族 evaluator 的最小实现：**两行**。
- [deepspec/data/parser.py:L18-L30](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/parser.py#L18-L30)：`TemplateRegistry`——`register` 断言名字不重复（防重复注册），`get` 按名取模板。注册表是模块级单例。
- [deepspec/data/parser.py:L32-L51](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/parser.py#L32-L51)：现有两个模板。`qwen` 有默认 system prompt；`gemma4` 无 system prompt 且带 `assistant_loss_prefix`（thought 通道前缀，计算 loss 时跳过，u2-l2 讲过）。新家族模板照此结构填四到五个字段即可，字段含义：`assistant_header`（assistant 回复的起始标记，正则定位用）、`user_header`、`system_prompt`（默认系统提示，可为 None）、`end_of_turn_token`（回合结束符，纳入监督区间让模型学会停止）、`assistant_loss_prefix`（可选，需要跳过的损失前缀）。

#### 4.4.4 代码实践

**实践目标**：验证「架构名总线」与「模板注册表」两条查找路径，方法是对现有系统做黑盒探测。

**操作步骤**：

1. 运行以下只读探测（示例代码，非项目原有）：

```python
# 示例代码：验证两条注册路径
from deepspec.data.parser import TEMPLATE_REGISTRY

tpl = TEMPLATE_REGISTRY.get("qwen")
print(tpl.assistant_header, "|", tpl.end_of_turn_token, "|", tpl.system_prompt)

# 架构名一致性检查：训练写入端 vs 评估查表端
from deepspec.eval.dspark.evaluator import Qwen3DSparkEvaluator, Gemma4DSparkEvaluator
import eval as eval_entry
import deepspec.modeling.dspark.qwen3.config as q3cfg
import deepspec.modeling.dspark.gemma4.config as g4cfg

written = {"qwen3": "Qwen3DSparkModel", "gemma4": "Gemma4DSparkModel"}  # 见两个 config.py 的 architectures 行
for name, arch in written.items():
    assert arch in eval_entry.EVALUATORS, f"{arch} 未注册!"
print("架构名总线一致 ✓  模板字段 ✓")
```

2. 把第 1 步的断言思路扩展成你自己的「接入检查清单」函数：输入新架构名与新模板名，输出各项注册是否就绪。

**需要观察的现象**：两个断言通过；`tpl.assistant_header` 打印 `<|im_start|>assistant\n`。

**预期结果**：确认「训练写入的架构名」与「评估查表的键」由 `EVALUATORS` 字典维系一致——你的新家族接入时，这个断言脚本就是最便宜的自检手段。**待本地验证**（脚本可在仓库根目录直接运行，无需 GPU 与模型权重）。

#### 4.4.5 小练习与答案

**练习 1**：为什么目标模型在评估侧用通用 `AutoModelForCausalLM` 加载，草稿模型却要家族专属的 `draft_model_cls`？

**答案**：目标模型是 HF 官方模型，其 `config.json` 的 `architectures`（如 `Qwen3ForCausalLM`）在 transformers 的官方注册表里能查到；草稿模型的架构名（`Qwen3DSparkModel`）是 DeepSpec 自定义的，transformers 不认识，所以 DeepSpec 用自己的 `EVALUATORS` 表 + `draft_model_cls` 类属性完成两级分发。

**练习 2**：如果新家族的 tokenizer 没有 mask token（词表里没有保留位），怎么办？

**答案**：这是接入前必须检查的硬约束。可选方案：用词表中其他确认不出现在训练数据里的特殊 token（如 finetune 保留位）充当 `mask_token_id`；或扩展词表并同步目标模型 embedding（代价是 vocab_size 变化影响 lm_head 形状契约，需要重训 embedding 行）。最稳妥的做法是像 Qwen3/Gemma4 一样选一个原厂保留 token——接入设计文档里应显式记录这个选择及其理由。

**练习 3**：注册 chat_template 时名字写错（如配置里写 `"llama"`、注册的是 `"llama3"`），会在哪个环节、以什么形式失败？

**答案**：不会在 import 时失败（注册表允许任意名字注册），而是在数据准备阶段 `TEMPLATE_REGISTRY.get(name)` 或其调用处以 `KeyError` 失败——属于「晚失败」。这也是为什么 4.4.4 的接入检查清单应该把「配置字符串 ↔ 注册名一致」作为独立检查项。

## 5. 综合实践

**毕业设计任务：为 Llama 接入 DeepSpec 写一份完整的接入设计文档（不落地写码）。**

把 4.1 的 9 条清单作为目录，产出一份 `design-llama-dspark.md`，必须包含以下小节。以 meta-llama/Llama-3.1-8B-Instruct（假设可获取）为目标，算法选 DSpark：

1. **文件清单**：逐条列出新建/修改的文件路径（对照 4.1.1），每条标注「新建 / 修改 / 无需改动」及理由。特别论证第 9 条：Llama 的 `LlamaForCausalLM` 是否能走 `_get_target_backbone` 的默认分支。
2. **modeling/dspark/llama/ 设计**：
   - `build_draft_config` 签名与 Llama 版差异表（Llama config 是扁平的，预期比 Gemma4 版简单、接近 Qwen3 版；列出你要覆写的全部字段）；
   - `LlamaDSparkModel` 的关键方法签名清单：`__init__(self, config)`、`initialize_embeddings_and_head(self, *, embed_tokens, lm_head, freeze=True)`、`set_embedding_head_trainable`、`compute_logits`、`sample_draft_tokens`、`sample_draft_token_step`、`predict_confidence_step`、`forward(self, input_ids, target_hidden_states, loss_mask, target_last_hidden_states) -> DSparkForwardOutput`（签名抄自 [qwen3/modeling.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/modeling.py#L201-L525)，forward 逻辑预期可逐行照抄）；
   - 与 Qwen3 的注意力差异预判：Llama 3 无 q_norm/k_norm、GQA 头数、RoPE 实现——逐项对照 HF `modeling_llama.py` 写出需要改的行。
3. **mask_token_id 决策**：查 Llama 3.1 tokenizer 词表，选出候选保留 token 并记录理由（见练习 2）。
4. **target_layer_ids 选择**：Qwen3-4B（36 层）用 `[1, 9, 17, 25, 33]` 均匀采样，Llama-3.1-8B（32 层）给出你的选层方案，校验其满足 `validate_target_layer_ids` 的升序与禁末层约束。
5. **trainer / evaluator / chat_template 三处注册**：给出每处的完整代码块（trainer 约 7 行、evaluator 2 行、`EVALUATORS` 一行、`ChatTemplate` 一个实例、常量一行），以及 Llama 3 对话模板的 `assistant_header`（`<|start_header_id|>assistant<|end_header_id|>\n\n`）与 `end_of_turn_token`（`<|eot_id|>`）字段值——以 HF tokenizer 实测为准，标注「待本地验证」。
6. **风险清单**：至少列出三项，例如 Llama 3.1 的 RoPE 缩放（rope_scaling）对草稿模型位置编码的影响、`torch_compile` 是否照开、词表 128256 对 markov 头 embedding 显存的放大。

**验收标准**：把文档交给一个没读过 DeepSpec 的同事，他能照着开 Github PR 而不需要再问你任何问题。

## 6. 本讲小结

- 接入一个新目标模型族是**加法**：新建 4 个文件（`modeling/dspark/<family>/` 三件套 + 一份训练配置），修改 4 处注册（trainer、`EVALUATORS`、`TEMPLATE_REGISTRY`、模型常量），外加检查 1 处布局特判（`_get_target_backbone`），标准布局家族可走默认分支。
- `build_draft_config` 是合同转换器：深拷贝目标 config 后覆写字段，其中 `architectures = ["<Family>DSparkModel"]` 是贯穿训练与评估的总线，`tie_word_embeddings = False` 是独立冻结权重的前提，`target_layer_ids` 受 `validate_target_layer_ids` 闸门约束（升序、禁末层、-1 表 embedding）。
- `initialize_embeddings_and_head` 是新家族必须实现的形状契约方法，由 `BaseTrainer.build_models` 在 CPU 上加载目标模型后统一调用，冻结复用 embedding 与 lm_head。
- trainer 子类最小只需覆盖 `_build_draft_model`（Gemma4 版 7 行），evaluator 子类最小只需覆盖 `draft_model_cls` 类属性（2 行）——模板方法模式把模型族差异压缩到了极致。
- 数据侧的 `TEMPLATE_REGISTRY` 与数据脚本里的 `model_type` 特判是两个「晚失败」风险点，接入检查清单应覆盖「配置字符串 ↔ 注册名」与「backbone 布局」两类一致性。
- 算法超参（block_size、num_draft_layers、markov/置信度/损失权重）跨模型族原样迁移，接入新家族时只换「目标身份」相关的少数字段。

## 7. 下一步学习建议

- **下一讲 u7-l2（性能工程）**：接入新家族后你迟早要面对吞吐问题，该讲汇总 `torch.compile(dynamic=True)`、flex_attention block mask、CUDA stream 预取与 `no_sync` 梯度累积——特别注意本讲提到的 Gemma4 关闭 `torch_compile` 的先例，说明编译兼容性是模型族相关的。
- **u7-l3（毕业实战）**：把本讲的设计文档（或直接用现有 Qwen3 配置）落到一次小规模端到端运行，验证你对三阶段流水线的整体把握。
- **延伸阅读源码**：`deepspec/modeling/eagle3/` 下的 qwen3/gemma4 双实现——对照本讲方法，自己列出「Eagle3 接入新家族」的清单，检验你是否真正掌握了这套扩展模式；再读 `deepspec/trainer/eagle3_trainer.py`，确认「只填两个钩子」的结论在第二种算法上依然成立。
