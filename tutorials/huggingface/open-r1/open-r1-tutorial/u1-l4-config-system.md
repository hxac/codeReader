# 配置系统与 YAML 训练配方

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 open-r1 的「三元组配置」是什么：`ScriptArguments`（脚本/数据参数）、`TrainingConfig`（`SFTConfig`/`GRPOConfig`，训练参数）、`ModelConfig`（模型参数）。
- 解释 `TrlParser.parse_args_and_config()` 是如何把 **YAML 配方** 和 **命令行参数** 合并、再分发到这三个数据类的。
- 读懂 `src/open_r1/configs.py` 里 open-r1 在 trl 基类之上「加了哪些自己的字段」，特别是 `dataset_mixture`（数据集混合）、`callbacks`/`benchmarks`/`wandb_*`（回调/评测/实验追踪）以及 GRPO 的一堆奖励超参。
- 拿到一份 `recipes/.../config_*.yaml` 配方时，能判断每个字段最终被谁读取。

承接前两讲：[u1-l1](./u1-l1-project-overview.md) 介绍了「三脚本 + 三元组配置」的全景，[u1-l2](./u1-l2-repo-structure.md) 指出 `TrlParser` 把命令行与 YAML 合并、`recipes/` 存配方。本讲就把这套配置机制彻底拆开。

## 2. 前置知识

在进入源码前，先用大白话建立三个直觉。

**1）什么是「数据类（dataclass）」。**
Python 的 `@dataclass` 是一种「声明字段即可」的写法。你写一行 `learning_rate: float = 4e-5`，Python 就帮你生成构造函数。open-r1 的所有配置类都是 dataclass，好处是：字段名 = 参数名，类型清晰，还能自动转成命令行帮助文本。

**2）什么是「继承配置」。**
open-r1 自己并不从零造配置，而是「站在 trl 肩膀上」：

```
trl.ScriptArguments        ← HuggingFace 官方库提供的「脚本参数」基类（含 dataset_name 等）
   └── ScriptArguments     ← open-r1 继承它，新增 dataset_mixture
        └── GRPOScriptArguments  ← GRPO 专用，再加一堆奖励超参

trl.SFTConfig / trl.GRPOConfig  ← 训练参数基类（含 learning_rate、bf16、num_generations……）
   └── SFTConfig / GRPOConfig   ← open-r1 继承它，新增 callbacks / benchmarks / wandb 等

trl.ModelConfig            ← 模型参数，open-r1 直接复用，不继承改造
```

所以一份 open-r1 配置里，**大部分字段是 trl/transformers 自带的，少数字段是 open-r1 自己加的**。本讲的重点就是「open-r1 加了什么、为什么加」。

**3）为什么要把参数分成「三元组」。**
因为这三类参数的生命周期完全不同：模型参数只在加载模型时用一次（`model_name_or_path`、`torch_dtype`）；训练参数贯穿整个训练循环（`learning_rate`、`per_device_train_batch_size`）；脚本参数是 open-r1 自己的业务逻辑（用哪个数据集、用哪些奖励函数）。分开后，`sft.py` 的 `main()` 拿到三个干净的对象，互不干扰：

```python
def main(script_args, training_args, model_args): ...
```

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/open_r1/configs.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py) | 本讲主角。定义 `DatasetConfig`、`DatasetMixtureConfig`、`ScriptArguments`、`GRPOConfig`、`SFTConfig`、`GRPOScriptArguments` 六个数据类。 |
| [src/open_r1/sft.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/sft.py) | SFT 入口。末尾用 `TrlParser((ScriptArguments, SFTConfig, ModelConfig))` 解析三元组。 |
| [src/open_r1/grpo.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/grpo.py) | GRPO 入口。三元组换成 `(GRPOScriptArguments, GRPOConfig, ModelConfig)`。 |
| [recipes/OpenR1-Distill-7B/sft/config_distill.yaml](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/OpenR1-Distill-7B/sft/config_distill.yaml) | 真实的 SFT 配方，蒸馏 7B 模型用。 |
| [recipes/Qwen2.5-1.5B-Instruct/grpo/config_demo.yaml](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/Qwen2.5-1.5B-Instruct/grpo/config_demo.yaml) | 真实的 GRPO demo 配方，1.5B 小模型可用。 |

## 4. 核心概念与源码讲解

### 4.1 三元组配置与 TrlParser 解析机制

#### 4.1.1 概念说明

open-r1 不自己写命令行解析器，而是直接用 trl 提供的 `TrlParser`。`TrlParser` 接受一个**元组**，元组里是 1 到 3 个 dataclass。约定俗成的顺序是：

```
(脚本参数类, 训练参数类, 模型参数类)
```

解析后，`parse_args_and_config()` 按顺序返回三个对象，于是脚本里可以写：

```python
script_args, training_args, model_args = parser.parse_args_and_config()
```

`TrlParser` 的「聪明之处」在于：它不是简单地把所有参数塞进第一个类，而是**按字段名把每个参数路由到定义了该字段的那一个类**。所以你在 YAML 里写 `learning_rate`，它会进 `training_args`；写 `model_name_or_path`，它会进 `model_args`；写 `dataset_name`，它会进 `script_args`。

#### 4.1.2 核心流程

```
命令行 (CLI) ─┐
              ├─► TrlParser.parse_args_and_config() ─► 三元组返回
YAML 配方 ────┘
   1. 先读 --config 指向的 YAML，把里面的键值作为「默认值」
   2. 再读命令行参数，同名键「覆盖」YAML
   3. 逐字段路由到 (ScriptArguments, TrainingConfig, ModelConfig)
   4. 触发各 dataclass 的 __post_init__ 校验
```

#### 4.1.3 源码精读

两个入口脚本的末尾几乎一样，区别只在三元组里用的是哪个类：

SFT 入口用 `ScriptArguments + SFTConfig`：

[sft.py:166-169](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/sft.py#L166-L169) —— 创建 `TrlParser`，元组是 `(ScriptArguments, SFTConfig, ModelConfig)`，解析后把三个对象交给 `main()`。这里 `ModelConfig` 直接 `from trl import`，没有继承改造。

GRPO 入口换成 `GRPOScriptArguments + GRPOConfig`：

[grpo.py:178-180](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/grpo.py#L178-L180) —— 元组是 `(GRPOScriptArguments, GRPOConfig, ModelConfig)`。GRPO 因为需要奖励函数等业务字段，所以脚本参数类换成了 `GRPOScriptArguments`（见 4.4）。

注意两个 `--config` 不要混淆，这是初学者最容易踩的坑：

- `accelerate launch --config_file recipes/accelerate_configs/zero3.yaml` —— 这个 `--config_file` 属于 **accelerate**，控制的是多卡/多机的并行拓扑（见 u7）。
- `src/open_r1/sft.py --config recipes/.../config_distill.yaml` —— 这个 `--config` 属于 **TrlParser**，加载的就是本讲的 YAML 训练配方。

[README.md:125-126](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/README.md#L125-L126) 给出了把它们拼在一起的真实命令。

#### 4.1.4 代码实践

**实践目标：** 直观感受「同一份 YAML，路由到三个对象」。

**操作步骤：**

1. 在仓库根目录写一段最小调用（示例代码，不必保存到仓库）：

   ```python
   # 示例代码：演示 TrlParser 路由
   from open_r1.configs import ScriptArguments, SFTConfig
   from trl import ModelConfig, TrlParser

   parser = TrlParser((ScriptArguments, SFTConfig, ModelConfig))
   script_args, training_args, model_args = parser.parse_args_and_config(
       args=[
           "--config", "recipes/OpenR1-Distill-7B/sft/config_distill.yaml",
           "--learning_rate", "1e-5",          # 命令行覆盖 YAML 里的 4e-5
       ]
   )
   print(type(script_args).__name__, type(training_args).__name__, type(model_args).__name__)
   print("learning_rate =", training_args.learning_rate)
   print("model_name_or_path =", model_args.model_name_or_path)
   ```

2. 在已安装 open-r1 的环境里运行它（需要能 import trl，见 [u1-l3](./u1-l3-installation-setup.md)）。

**需要观察的现象：**

- 三个对象的类型分别是 `ScriptArguments` / `SFTConfig` / `ModelConfig`。
- `learning_rate` 打印为 `1e-5`（命令行覆盖了 YAML 里的 `4.0e-05`），证明命令行优先级更高。
- `model_name_or_path` 来自 YAML，落在 `model_args` 上而非 `training_args`。

**预期结果：** 命令行同名键覆盖 YAML；不同字段按定义位置自动归位。**若本地没有装好 trl 环境，此步标注为「待本地验证」。**

#### 4.1.5 小练习与答案

**Q1：** 如果在 YAML 和命令行里同时写了 `learning_rate`，最终用哪个值？
**A：** 用命令行的值。`TrlParser` 先把 YAML 当默认值载入，再用命令行参数覆盖同名键。

**Q2：** 为什么 SFT 用 `ScriptArguments` 而 GRPO 用 `GRPOScriptArguments`？
**A：** 因为 GRPO 需要额外的业务字段（奖励函数列表、cosine 调度参数、代码评测参数等），这些放在训练参数类里不合适，所以 open-r1 用 `GRPOScriptArguments(ScriptArguments)` 单独扩展（见 4.4）。

---

### 4.2 ScriptArguments 与数据集混合配置（DatasetMixtureConfig）

#### 4.2.1 概念说明

`ScriptArguments` 描述「这次训练喂什么数据」。open-r1 在 trl 的 `trl.ScriptArguments` 之上做了两件事：

1. 把基类里必填的 `dataset_name` 改成**可选**（因为可以改用「数据集混合」）。
2. 新增 `dataset_mixture` 字段，支持把多个数据集按权重拼到一起。

围绕 `dataset_mixture`，open-r1 又定义了两个辅助数据类：`DatasetConfig`（单个数据集的配置）和 `DatasetMixtureConfig`（一组数据集的混合配置）。这是 open-r1 区别于「只跑单数据集」的朴素用法的关键扩展，也是 `Mixture-of-Thoughts` 这类混合数据集得以配置的地方。

#### 4.2.2 核心流程

`ScriptArguments` 用 `__post_init__` 在构造完成时做校验和转换：

```
构造 ScriptArguments(dataset_name=?, dataset_mixture=?)
        │
        ▼ __post_init__
┌───────────────────────────────────────────┐
│ 1. dataset_name 与 dataset_mixture 必须二选一 │
│ 2. 若给了 dataset_mixture（dict）：          │
│    - 校验它是 dict 且含 'datasets' 键        │
│    - 把每个子 dict 转成 DatasetConfig 对象   │
│    - 组装成 DatasetMixtureConfig            │
│    - 校验所有数据集的 columns 列名一致        │
└───────────────────────────────────────────┘
```

其中「按权重采样」的直觉是：设两个数据集权重为 \(w_1, w_2\)，则最终混合集中来自各数据集的期望比例约为：

\[
\text{占比}_i = \frac{w_i}{\sum_j w_j}
\]

真正的采样/拼接逻辑在 `utils/data.py`（详见 [u2-l2](./u2-l2-dataset-loading.md)），本讲只讲配置怎么写、怎么校验。

#### 4.2.3 源码精读

`DatasetConfig` 是单个数据集的配置（id、split、列名、权重）：

[configs.py:22-30](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L22-L30) —— 注意 `weight` 是可选的，缺省时 `__post_init__` 里会按 1.0 处理。

`DatasetMixtureConfig` 把一列表的 `DatasetConfig` 串起来，外加 `seed`（采样随机种子）和 `test_split_size`（切出测试集的比例）：

[configs.py:33-39](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L33-L39)。

`ScriptArguments` 把基类的 `dataset_name` 改成可选，并新增 `dataset_mixture`：

[configs.py:70-76](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L70-L76) —— `dataset_name` 默认 `None`，注释明确「用了 dataset_mixture 时可以省略 dataset_name」。

校验与转换的核心在 `__post_init__`：

[configs.py:78-110](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L78-L110) —— 关键三步：(1) 二选一检查（L79-80）；(2) 把原始 dict 里的每一项构造成 `DatasetConfig`（L94-101，缺省 `weight=1.0`、`split="train"`）；(3) 用 `DatasetMixtureConfig(...)` 替换掉原来的 dict（L106-110），之后 `script_args.dataset_mixture` 就是一个对象而非裸 dict。

最后还有一道「列名一致性」校验：

[configs.py:112-120](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L112-L120) —— 混合的各数据集必须列名一致（否则 `concatenate` 会失败），不一致直接抛错。

#### 4.2.4 代码实践

**实践目标：** 亲手写一个 `dataset_mixture` 配置，触发 `__post_init__` 的校验与转换。

**操作步骤：**

1. 写一段示例代码（示例代码）：

   ```python
   # 示例代码：直接构造 ScriptArguments，验证 dataset_mixture 被转换
   from open_r1.configs import ScriptArguments, DatasetMixtureConfig

   args = ScriptArguments(dataset_mixture={
       "datasets": [
           {"id": "trl-internal-testing/zen", "weight": 0.7},
           {"id": "trl-internal-testing/zen2", "weight": 0.3},
       ],
       "seed": 42,
       "test_split_size": 0.1,
   })
   print(type(args.dataset_mixture))                 # DatasetMixtureConfig
   print(args.dataset_mixture.datasets[0].weight)    # 0.7
   ```

2. 再故意把两个数据集的 `columns` 写成不一样的列名，观察是否会抛 `Column names must be consistent...` 的错误。

**需要观察的现象：**

- 构造后 `dataset_mixture` 从 `dict` 变成了 `DatasetMixtureConfig` 对象。
- 列名不一致时，构造阶段就报错，而不是训练到一半才崩。

**预期结果：** `__post_init__` 把裸 dict 转成结构化对象，并守住「列名一致」「二选一」两条不变量。**无 trl 环境时标注「待本地验证」。**

#### 4.2.5 小练习与答案

**Q1：** 如果既不传 `dataset_name` 也不传 `dataset_mixture`，会发生什么？
**A：** `__post_init__` 第 79-80 行直接抛 `ValueError("Either dataset_name or dataset_mixture must be provided")`。

**Q2：** 两个数据集权重分别是 0.2 和 0.8，混合后各自占比约为多少？
**A：** 按权重归一化：\(0.2/(0.2+0.8)=0.2\)，\(0.8/1.0=0.8\)，即约 20% 与 80%。

---

### 4.3 SFTConfig / GRPOConfig 的扩展字段

#### 4.3.1 概念说明

`SFTConfig` 和 `GRPOConfig` 是训练参数类，继承自 trl 的 `trl.SFTConfig` / `trl.GRPOConfig`（后者又继承 transformers 的 `TrainingArguments`）。所以它们既包含大量「通用训练超参」（`learning_rate`、`bf16`、`per_device_train_batch_size`、`save_strategy`……），又包含 trl 专属字段（SFT 的 `max_length`、`packing`、`use_liger_kernel`；GRPO 的 `num_generations`、`use_vllm`、`max_completion_length`）。

open-r1 在这些之上**又加了一组自己的字段**，集中解决三件事：

- **回调（callbacks）**：训练过程中要不要挂回调，比如「每次保存就推一个 Hub 分支」。
- **评测（benchmarks）**：训练结束后要不要顺手跑一组基准。
- **实验追踪（wandb）与 Hub 推送**：往哪个 wandb project/entity/group 写，模型推到 Hub 的哪个分支。

#### 4.3.2 核心流程

两个字段的「形状」几乎一致——SFT 和 GRPO 各自定义相同语义的字段：

| 字段 | 含义 | 在 SFT/GRPO 中都有 |
| --- | --- | --- |
| `benchmarks: list[str]` | 训练后要跑的基准名列表 | ✅ |
| `callbacks: list[str]` | 训练时要挂的回调名列表 | ✅ |
| `chat_template: Optional[str]` | 覆盖分词器的聊天模板（Jinja） | ✅ |
| `system_prompt: Optional[str]` | 系统提示词 | ✅ |
| `hub_model_revision: str` | 推送到 Hub 的分支名（默认 `main`） | ✅ |
| `push_to_hub_revision: bool` | 是否推到「分支/版本」而非覆盖 | ✅ |
| `overwrite_hub_revision: bool` | 是否覆盖已有分支 | ✅ |
| `wandb_entity/project/run_group` | wandb 的三个层级 | ✅ |
| `num_completions_to_print: int` | **仅 GRPO**：打印多少条生成 | ❌（仅 GRPOConfig） |

`benchmarks` 和 `callbacks` 都是「**名字字符串**」而不是对象——open-r1 在别处维护「名字 → 实现」的注册表（如 `get_callbacks`、评测任务注册），配置里只声明「用哪些」。这是一种典型的「声明式配置 + 注册表查找」模式，后续讲义（u7-l3 回调、u8-l1 评测）会展开。

#### 4.3.3 源码精读

`GRPOConfig` 的 open-r1 扩展字段：

[configs.py:130-166](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L130-L166) —— 注意 `benchmarks`/`callbacks` 用 `default_factory=lambda: []`（dataclass 里可变默认值必须用 factory，否则所有实例会共享同一个列表，这是 Python 常见坑）。`hub_model_revision` 默认 `"main"`（L139-141），`num_completions_to_print`（L142）和 `wandb_log_unique_prompts`（L149-154）是 GRPO 独有。

`SFTConfig` 的 open-r1 扩展字段：

[configs.py:175-205](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L175-L205) —— 与 GRPOConfig 高度对称，但没有 `num_completions_to_print` / `wandb_log_unique_prompts`（SFT 不需要「按 prompt 去重记录」）。

在真实配方里能看到这些字段被实际使用。GRPO demo 配方设置了 `system_prompt`：

[config_demo.yaml:10](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/Qwen2.5-1.5B-Instruct/grpo/config_demo.yaml#L10) —— 这段 `system_prompt` 告诉模型用 `<think>...</think><answer>...</answer>` 格式回答，它会被 `grpo.py` 的 `make_conversation` 读走（见 [u3-l1](./u3-l1-grpo-script-walkthrough.md)）。

而 SFT 蒸馏配方里最显眼的是 `chat_template`——一整段 Jinja 模板，正是 open-r1 的 `SFTConfig.chat_template` 字段：

[config_distill.yaml:9](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/OpenR1-Distill-7B/sft/config_distill.yaml#L9) —— 它定义了如何把多轮对话渲染成模型输入。README 也特别提醒：蒸馏自 DeepSeek 的模型需要覆盖默认 chat template（见 [README.md:228](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/README.md#L228)）。

#### 4.3.4 代码实践

**实践目标：** 确认 open-r1 扩展字段确实「长在」trl 基类之上，且能被 YAML 覆盖。

**操作步骤：**

1. 阅读上面两段源码，圈出 open-r1 自定义的字段名。
2. 在 `config_demo.yaml` 里搜索 `system_prompt`，确认它就是 `GRPOConfig.system_prompt`（configs.py:145）。
3. 试着自己加一行 `num_completions_to_print: 4` 到 `config_demo.yaml`（**仅用于理解，不要提交**），想清楚：这个字段只有 `GRPOConfig` 有，`SFTConfig` 没有，所以它**不能**出现在 SFT 的配方里。

**需要观察的现象：**

- `system_prompt` 在 GRPO 配方里出现，对应 `GRPOConfig` 字段。
- 把 `num_completions_to_print` 错放进 SFT 配方时，`TrlParser` 会因为 `SFTConfig` 没有这个字段而报「未知参数」类错误（待本地验证）。

**预期结果：** open-r1 扩展字段与 trl 基类字段「共存」于同一个 `training_args` 对象里；扩展字段在不同 Trainer（SFT vs GRPO）间不可混用。

#### 4.3.5 小练习与答案

**Q1：** 为什么 `benchmarks` 要写成 `default_factory=lambda: []` 而不是 `default=[]`？
**A：** 因为 `[]` 是可变默认值，dataclass 若直接用它，所有实例会共享同一个列表对象；用 `default_factory` 让每个实例得到独立的新列表。

**Q2：** `hub_model_revision` 和 `push_to_hub_revision` 各自管什么？
**A：** `hub_model_revision` 指定推送到 Hub 的「分支名」（默认 `main`）；`push_to_hub_revision` 是个开关，决定到底「推到一个版本分支」还是「直接覆盖主分支」。两者配合实现「按版本归档模型」，详见 [u7-l3](./u7-l3-callbacks-hub-wandb.md)。

---

### 4.4 GRPOScriptArguments 的奖励超参数

#### 4.4.1 概念说明

GRPO 是强化学习，核心是「**奖励函数**给模型生成打分」。open-r1 把「用哪些奖励函数、这些奖励函数的参数是多少」也做成了配置。这就是 `GRPOScriptArguments` 存在的理由：它继承 `ScriptArguments`，再加一整组奖励相关字段。

`GRPOScriptArguments` 的字段可分四组：

- **奖励函数选择**：`reward_funcs`（用哪些奖励，如 `accuracy`/`format`/`tag_count`）。
- **cosine 长度调度**：`cosine_min_value_wrong`、`cosine_max_value_wrong`、`cosine_min_value_correct`、`cosine_max_value_correct`、`cosine_max_len`。
- **重复与超长惩罚**：`repetition_n_grams`、`repetition_max_penalty`、`max_completion_len`、`soft_punish_cache`。
- **代码评测**：`code_language`、`code_eval_test_batch_size`、`code_eval_scoring_mode`、`parallel_code_exec_per_proc`、`code_provider`、`ioi_provider`、`e2b_router_url`、`morph_router_url`。
- **数据列**：`dataset_prompt_column`（默认 `prompt`）。

这些字段会被 [rewards.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py) 里的奖励函数读取（详见 u3-l2 ~ u3-l4、u5、u6）。

#### 4.4.2 核心流程

```
YAML: reward_funcs: [accuracy, format, tag_count]
            │  TrlParser 路由
            ▼
GRPOScriptArguments.reward_funcs = ["accuracy","format","tag_count"]
            │  grpo.py 调用
            ▼
get_reward_funcs(script_args) ─► 按名字从注册表取函数 ─► GRPOTrainer(reward_funcs=...)
```

注意：`reward_funcs` 是 open-r1 在 `GRPOScriptArguments` 里定义的（脚本参数），而 `reward_weights`（各奖励的权重）则来自 trl 的 `GRPOConfig`（训练参数）。两者一前一后出现在同一份配方里。

#### 4.4.3 源码精读

`GRPOScriptArguments` 继承 `ScriptArguments`，并定义 `reward_funcs`：

[configs.py:234-239](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L234-L239) —— 默认 `["accuracy", "format", "tag_count"]`，帮助文本列出了所有可选奖励名。

cosine 调度参数（控制「正确答案的长奖励」与「错误答案的长惩罚」区间）：

[configs.py:240-259](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L240-L259) —— 注意默认值的设计意图：正确答案奖励在 `[0.5, 1.0]`，错误答案在 `[-0.5, 0.0]`（`cosine_max_value_wrong` 默认 `-0.5`，即「错得越离谱惩罚越大」）。

代码评测相关（语言、评分模式、provider）：

[configs.py:268-322](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L268-L322) —— `code_eval_scoring_mode` 是个 `Literal["pass_fail","partial","weighted_sum"]`，默认 `weighted_sum`（按通过率给分）；`code_provider`/`ioi_provider` 选择沙箱后端（`e2b`/`local`/`morph`、`piston`/`morph`）。

数据列字段：

[configs.py:293-296](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L293-L296) —— `dataset_prompt_column` 默认 `"prompt"`；但 GRPO demo 配方把它改成了 `"problem"`，因为 NuminaMath 数据集里题目列就叫 `problem`：[config_demo.yaml:9](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/Qwen2.5-1.5B-Instruct/grpo/config_demo.yaml#L9)。

#### 4.4.4 代码实践

**实践目标：** 理解「奖励函数名 → 实际函数」的映射，以及超参如何被读取。

**操作步骤（源码阅读型）：**

1. 打开 `recipes/Qwen2.5-1.5B-Instruct/grpo/config_demo.yaml`，找到 `reward_funcs`、`reward_weights` 两个块（[L41-48](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/Qwen2.5-1.5B-Instruct/grpo/config_demo.yaml#L41-L48)）。
2. 在 `src/open_r1/grpo.py` 里找 `get_reward_funcs(script_args)`（[grpo.py:88](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/grpo.py#L88)），确认它读的就是 `script_args.reward_funcs`。
3. 追一步到 `src/open_r1/rewards.py` 的 `REWARD_FUNCS_REGISTRY`（详见 u3-l2），看 `accuracy`/`format`/`tag_count` 三个名字分别绑定到哪个函数。

**需要观察的现象：** 配方里的字符串名字，经过「注册表」变成可调用的 Python 函数，再注入 `GRPOTrainer`。

**预期结果：** 你能画出 `YAML 字符串 → GRPOScriptArguments → get_reward_funcs → GRPOTrainer(reward_funcs=...)` 这条链路。

#### 4.4.5 小练习与答案

**Q1：** 为什么 `reward_funcs` 放在 `GRPOScriptArguments`，而 `reward_weights` 放在（trl 的）`GRPOConfig`？
**A：** `reward_funcs` 是 open-r1 的业务选择（用哪些自定义奖励），属于脚本参数；`reward_weights` 是 trl 训练器原生支持的「各奖励加权」训练超参，属于训练参数。open-r1 没必要重复造 `reward_weights`，直接复用 trl 的。

**Q2：** `code_eval_scoring_mode` 的三种取值，哪种「最鼓励部分正确」？
**A：** `weighted_sum`（默认）——它按通过测试的比例线性给分，通过 8/10 就得 0.8 分；`pass_fail` 则是全过才得分、否则 0；`partial` 介于两者。具体公式与差异见 [u6-l2](./u6-l2-codeforces-scoring.md)。

---

### 4.5 YAML 配方与命令行的合并（TrlParser.parse_args_and_config）

#### 4.5.1 概念说明

前几节讲的是「配置类长什么样」，这一节讲「配置类怎么被填满」。open-r1 的做法是：**把一份 YAML 作为基线，再用命令行做覆盖**。这样既能让「复现实验」固化在一个文件里（YAML），又能让「临时调参」不必每次改文件（命令行）。

合并规则很简单：

1. `--config <file>.yaml` 里的键值，作为各 dataclass 字段的默认值。
2. 命令行里出现的同名参数，覆盖 YAML。
3. 逐字段路由到对应 dataclass；触发 `__post_init__` 校验。
4. 返回三元组。

#### 4.5.2 核心流程

```
recipes/.../config_demo.yaml        # 基线（固化实验）
        +
命令行 --learning_rate 3e-5 ...     # 临时覆盖
        │
        ▼  TrlParser.parse_args_and_config()
按字段名路由 ─► (GRPOScriptArguments, GRPOConfig, ModelConfig)
        │
        ▼  __post_init__ 校验（如 ScriptArguments 的二选一检查）
        ▼
返回 (script_args, training_args, model_args)
```

slurm 批处理脚本把这套封装得更简洁：`--config` 只传一个「后缀」（如 `demo`、`distill`），脚本自动拼成 `recipes/<model>/<task>/config_<后缀>.yaml`：

[slurm/train.slurm:147](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/train.slurm#L147) —— `src/open_r1/$TASK.py --config $CONFIG_FILE ...`，其中 `$CONFIG_FILE` 由模型名 + task + 后缀拼出来。

#### 4.5.3 源码精读

合并的「入口」就是这一行，SFT 和 GRPO 各一处：

[sft.py:167-168](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/sft.py#L167-L168) 与 [grpo.py:179-180](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/grpo.py#L179-L180) —— `parse_args_and_config()` 同时吃 YAML 和命令行，按字段路由后返回三元组。

合并行为的真实样例在 README：先 `--config` 给 YAML，再跟若干命令行参数覆盖：

[README.md:141-142](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/README.md#L141-L142) —— `--config recipes/OpenR1-Distill-7B/sft/config_distill.yaml` 之后再叠加命令行参数。

GRPO 配方本身就是「YAML 合并」的典型产物——同一个文件里既有 ModelConfig 字段、GRPOConfig 字段，也有 GRPOScriptArguments 字段，TrlParser 会自动把它们分拣开：

[config_demo.yaml:1-52](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/Qwen2.5-1.5B-Instruct/grpo/config_demo.yaml) —— 一份文件、三个归宿。

#### 4.5.4 代码实践：给 config_distill.yaml 做字段归档

**实践目标（即本讲主实践任务）：** 阅读 `recipes/OpenR1-Distill-7B/sft/config_distill.yaml`，把其中每个字段归类到 **ModelConfig / SFTConfig / ScriptArguments**（SFTConfig 含其继承的 transformers `TrainingArguments` 与 trl `SFTConfig` 字段），然后修改 `learning_rate` 并说明它被谁读取。

**操作步骤：**

1. 打开 [config_distill.yaml](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/OpenR1-Distill-7B/sft/config_distill.yaml)，对照 [configs.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py) 逐字段归类。
2. 把 `learning_rate` 从 `4.0e-05` 改成 `1.0e-05`（**仅本地试验，不要提交**）。
3. 追踪它被谁读取：`learning_rate` 是 transformers `TrainingArguments` 的字段 → 落到 `training_args`（`SFTConfig` 实例）→ `SFTTrainer` 内部用它构造优化器（默认 AdamW）和学习率调度器（`lr_scheduler_type`，本配方是 `cosine_with_min_lr`）。

**归档答案表（参考）：**

| YAML 字段 | 归属 | 说明 |
| --- | --- | --- |
| `model_name_or_path` / `model_revision` / `torch_dtype` / `attn_implementation` | **ModelConfig** | trl 的 `ModelConfig`，加载模型时用 |
| `chat_template` | **SFTConfig**（open-r1 扩展，configs.py:183） | 覆盖分词器聊天模板 |
| `dataset_name` / `dataset_config` / `dataset_num_proc` | **ScriptArguments**（trl 基类） | 单数据集加载参数 |
| `eos_token` | **ScriptArguments**（继承自 trl 基类） | 指定结束符 |
| `bf16` / `do_eval` / `eval_strategy` / `gradient_accumulation_steps` / `gradient_checkpointing` / `gradient_checkpointing_kwargs` / `hub_model_id` / `hub_strategy` / `learning_rate` / `log_level` / `logging_steps` / `logging_strategy` / `lr_scheduler_type` / `lr_scheduler_kwargs` / `max_grad_norm` / `max_steps` / `num_train_epochs` / `output_dir` / `overwrite_output_dir` / `per_device_eval_batch_size` / `per_device_train_batch_size` / `push_to_hub` / `report_to` / `save_strategy` / `save_total_limit` / `seed` / `warmup_ratio` | **SFTConfig**（继承自 transformers `TrainingArguments`） | 通用训练超参 |
| `packing` / `max_length` / `use_liger_kernel` | **SFTConfig**（继承自 trl `SFTConfig`） | trl 的 SFT 专属字段 |

> 注：上表中标注「继承自 trl 基类」的字段，并不出现在 `configs.py` 里——它们是 trl/transformers 提供的，open-r1 只是「白拿」。`configs.py` 里**只有 open-r1 自己新加的字段**（如 `chat_template`、`callbacks`、`benchmarks`、`wandb_*`）。

**需要观察的现象：**

- 整份 YAML 的字段被干净地分成三堆：模型、数据、训练。
- 改 `learning_rate` 只影响 `training_args`，不会动到 `model_args` 或 `script_args`。

**预期结果：** 你能对任意一份 `config_*.yaml` 快速做字段归档，并能解释 `learning_rate` 最终流向优化器与学习率调度器。**无 GPU/trl 环境时，归档本身可纯靠阅读源码完成，运行验证标注「待本地验证」。**

#### 4.5.5 小练习与答案

**Q1：** 我在命令行加了 `--per_device_train_batch_size 4`，同时 YAML 里也写了 `per_device_train_batch_size: 2`，最终用哪个？
**A：** 用命令行的 `4`。命令行覆盖 YAML。

**Q2：** 为什么 `config_distill.yaml` 里没有 `reward_funcs`？
**A：** 因为这是 **SFT** 配方，SFT 不需要奖励函数；`reward_funcs` 是 `GRPOScriptArguments` 才有的字段，只会出现在 `grpo/` 下的配方里（如 `config_demo.yaml`）。

---

## 5. 综合实践

把本讲的知识串起来，完成一次「从零读懂并改造一份配方」：

1. **选一份配方**：以 `recipes/Qwen2.5-1.5B-Instruct/grpo/config_demo.yaml` 为对象。
2. **画三栏归档表**：把它的所有字段分到 `ModelConfig` / `GRPOConfig` / `GRPOScriptArguments` 三栏，并标注每个字段是「trl/transformers 继承」还是「open-r1 在 configs.py 里自定义」（自定义的要写出行号）。
3. **解释一条数据链路**：`dataset_name`（L8）→ `GRPOScriptArguments.dataset_name` → `get_dataset(script_args)`（grpo.py:74）→ 训练集；`reward_funcs`（L41-44）→ `GRPOScriptArguments.reward_funcs` → `get_reward_funcs`（grpo.py:88）→ `GRPOTrainer`。
4. **做一次安全改造**：把 `learning_rate` 从 `2.0e-05` 调成 `1.0e-05`，并说明它最终被 `GRPOTrainer` 内部的优化器/调度器读取；再试着用命令行 `--learning_rate 3e-5` 覆盖它，验证覆盖生效（待本地验证）。
5. **触发一次校验**：复制该配方为新文件，故意删掉 `dataset_name` 且不补 `dataset_mixture`，运行时观察 `ScriptArguments.__post_init__` 是否抛出「Either dataset_name or dataset_mixture must be provided」。

完成上述五步后，你就真正掌握了 open-r1 的「三元组 + YAML 合并」配置体系。

## 6. 本讲小结

- open-r1 用 **三元组配置**：`(ScriptArguments, TrainingConfig, ModelConfig)`，由 trl 的 `TrlParser.parse_args_and_config()` 按字段名自动路由。
- `TrlParser` 先读 `--config` 指向的 YAML 作为默认值，再用命令行覆盖同名键，最后触发各 dataclass 的 `__post_init__` 校验。
- `ScriptArguments` 把 `dataset_name` 改可选，并新增 `dataset_mixture`；后者经 `__post_init__` 转成 `DatasetMixtureConfig`，并守住「二选一」「列名一致」两条不变量。
- `SFTConfig`/`GRPOConfig` 继承 trl/transformers，open-r1 在其上统一加了 `callbacks`、`benchmarks`、`chat_template`、`system_prompt`、`hub_model_revision`、`push_to_hub_revision`、`wandb_*` 等字段。
- `GRPOScriptArguments` 专门承载 GRPO 的奖励业务：`reward_funcs` 选择奖励、`cosine_*`/`repetition_*`/`max_completion_len`/`soft_punish_cache` 控制长度与重复、`code_*`/`*_provider`/`*_router_url` 控制代码评测沙箱。
- 配方里的字段大多是 trl/transformers 继承来的，**只有少数是 open-r1 在 `configs.py` 里自定义的**——分清这两类，是读懂任意 `config_*.yaml` 的关键。

## 7. 下一步学习建议

- 想看「数据混合的采样与拼接」到底怎么落地：进入 **u2-l2 数据集加载与混合**，读 `src/open_r1/utils/data.py` 的 `get_dataset`。
- 想看「回调/评测名字」如何被解析成真实对象：进入 **u7-l3 回调、Hub 版本推送与实验追踪** 与 **u8-l1 LightEval 基准评估**。
- 想看「奖励函数名」如何映射到打分函数：进入 **u3-l2 奖励函数注册表与数学正确性奖励**，读 `src/open_r1/rewards.py`。
- 建议先把本讲的字段归档表存下来——后续每一讲在引用某份 `config_*.yaml` 时，你都可以随时回来对照「这个字段属于谁、被谁读取」。
