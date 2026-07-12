# RLHF 训练流程

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `swift rlhf` 与 `swift sft` 在代码层面共享了什么、又各自补了什么，理解 `SwiftRLHF` 通过「覆写钩子」复用 `SwiftSft` 的设计。
- 掌握 `RLHFArguments` 的多继承组合方式，以及 `rlhf_type` 这一个字段如何同时驱动参数推断、模型准备、模板模式与训练器派发。
- 理解 `TrainerFactory` 如何用 `rlhf_type`（优先）或 `task_type`（兜底）作为路由键，把请求派发到 `DPOTrainer`/`KTOTrainer`/`GRPOTrainer` 等具体训练器。
- 能动手跑通一个最小 DPO 实验，并对比它与 SFT 在数据格式、损失函数上的差异；知道哪些 `rlhf_type` 才能用 vLLM 做 rollout。

本讲是「强化学习与 GRPO」单元的第一篇，定位是「把 RLHF 的脚手架搭起来」——先讲训练流程与派发机制，再由 u7-l2 深入 GRPO 算法本身。

## 2. 前置知识

在进入 RLHF 之前，请确认你已经理解下面这些概念（它们都在前序讲义中建立）：

- **SFT 主流程（u5-l4）**：`SwiftPipeline` 用模板方法模式在 `main()` 里固定「解析参数 → 设种子 → 调 `run()`」骨架；`SwiftSft.run()` 编排「准备数据集 → `save_args` → `prepare_model` → `TrainerFactory` 装配 → `trainer.train()`」。本讲会看到 `SwiftRLHF` 几乎不碰这个骨架，只覆写其中的几个「钩子方法」。
- **Arguments 数据类（u2-l1）**：`BaseArguments` 用多继承把多个 mixin 扁平拼成统一参数对象，`__post_init__` 需显式调用各父类后置逻辑。本讲的 `RLHFArguments` 仍是同一个套路。
- **TrainerFactory（u5-l1）**：用 `TRAINER_MAPPING` 与 `TRAINING_ARGS_MAPPING` 两张键对齐的字典，以 `task_type` 为路由键动态导入训练器。本讲会看到它在 RLHF 场景下改用 `rlhf_type` 作路由键。
- **Template 的 mode（u3-l3 / u6-l1）**：`template.set_mode(...)` 决定模板在「训练 / 推理 / rlhf / kto」等不同模式下如何编码与是否带 label。本讲会看到 RLHF 如何按 `rlhf_type` 切换模板模式。

如果你对 RLHF 算法本身（DPO/KTO/PPO/GRPO 的原理）还不熟悉，没关系——本讲聚焦「框架如何把算法跑起来」，算法细节会顺带提一句，深入推导留给 u7-l2。

下面用一个通俗类比建立直觉：

> SFT 是「老师手把手教学生写标准答案」——给一条 prompt，模型学着复刻示范回答。
> RLHF 是「老师比较学生的两个答案谁更好」——不再告诉模型标准答案，而是告诉它「这个回答比那个好」，让模型自己往「更好」的方向挪。
> 不同 RLHF 算法（DPO/KTO/PPO/GRPO）的区别，本质是「怎么用偏好信号构造损失」。ms-swift 的做法是：**训练流程不变，只换训练器和损失**。

## 3. 本讲源码地图

本讲涉及的关键源码文件如下：

| 文件 | 作用 |
| --- | --- |
| `swift/pipelines/train/rlhf.py` | 定义 `SwiftRLHF` 管道与 `rlhf_main` 入口；通过覆写 `SwiftSft` 的若干钩子接入 RLHF |
| `swift/pipelines/train/sft.py` | `SwiftSft` 基类，提供 `run()`/`train()` 等被复用的骨架 |
| `swift/pipelines/base.py` | `SwiftPipeline` 抽象基类，定义 `main()` 模板方法 |
| `swift/arguments/rlhf_args.py` | `RLHFArguments` 及 `RewardModelArguments`/`TeacherModelArguments`/`PPOArguments`/`GRPOArguments` 等参数组 |
| `swift/trainers/trainer_factory.py` | `TrainerFactory`：以 `rlhf_type`/`task_type` 为键派发训练器与训练参数 |
| `swift/cli/rlhf.py` | CLI 入口，调用 `rlhf_main()` |
| `examples/train/rlhf/dpo/lora.sh` | DPO + LoRA 的官方训练脚本，本讲实践的依据 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **SwiftRLHF 复用 SwiftSft**：看 `SwiftRLHF` 如何以最小改动把 SFT 管道改造成 RLHF 管道。
2. **RLHFArguments 与 rlhf_type**：看一个 `rlhf_type` 字段如何串联起参数推断、ref 模型、奖励模型与模板模式。
3. **TrainerFactory 的 RLHF 派发**：看 `rlhf_type` 如何成为训练器与损失函数的最终路由键。

### 4.1 SwiftRLHF 复用 SwiftSft

#### 4.1.1 概念说明

`swift rlhf` 与 `swift sft` 在命令行上是两个子命令，但在代码里它们共享绝大部分执行流程。`SwiftRLHF` 直接继承自 `SwiftSft`，把「解析参数、设种子、准备数据、装配 Trainer、开训、保存状态」这一整条骨架**原样继承**，只在少数几个关键节点上「插队」做 RLHF 特有的事。

这是典型的**模板方法模式 + 钩子覆写**：基类 `SwiftSft` 定义了流程骨架与若干可覆写的钩子方法（如 `_prepare_model_tokenizer`、`_prepare_template`、`_get_dataset`、`_get_trainer_kwargs`、`prepare_model`），`SwiftRLHF` 只覆写这些钩子，往里塞 RLHF 的多模型准备、模板模式切换等逻辑。

RLHF 相比 SFT，在「准备阶段」多出的事情主要是：

- **多挂几个模型**：偏好类算法（DPO/KTO）需要一个冻结的 **参考模型（ref model）** 用来计算 KL 约束；PPO 还需要一个 **价值模型（value model）**；GRPO/GKD 可能需要一个 **教师模型（teacher model）**；任何算法都可以挂一个或多个 **奖励模型（reward model）**。
- **切换模板模式**：不同算法要求模板以不同方式编码（如 KTO 要产 KL 样本、PPO 要走生成式编码）。
- **给训练器喂额外参数**：把上面准备好的 ref/reward/value/teacher 模型通过 `_get_trainer_kwargs` 传给训练器。

#### 4.1.2 核心流程

`SwiftRLHF` 的执行流程可以拆成「继承的部分」和「覆写的部分」两层：

```text
rlhf_main(args)                          # CLI 入口
  └─ SwiftRLHF(args).main()              # 继承自 SwiftPipeline：计时 + 调 run()
       └─ run()                          # ❰ 继承自 SwiftSft，完全不改 ❱
            ├─ _prepare_dataset()        #   继承：但内部编码策略对 grpo/gkd 改为延迟
            ├─ args.save_args()          #   继承：落盘 args.json
            ├─ prepare_model(...)        #   ❰ 覆写：额外处理 ref_adapters ❱
            │     └─ (主模型准备逻辑继承自 TunerMixin)
            ├─ TrainerFactory.get_trainer_cls(args)   # 用 rlhf_type 派发训练器
            ├─ trainer = trainer_cls(..., **_get_trainer_kwargs())
            │                               # ❰ 覆写：塞入 ref/reward/value/teacher ❱
            └─ self.train(trainer)        #   继承：trainer.train() + 保存状态

  构造期 __init__() 还会调用三个钩子（均覆写）：
    ├─ _prepare_model_tokenizer()   # 准备 ref/value/teacher/reward 模型，再 super() 载主模型
    ├─ _prepare_template()          # 按 rlhf_type 切换 template 模式
    └─ _prepare_flash_ckpt()        # 继承，不改
```

关键结论：**`run()`、`train()`、数据集装配、`save_args`、训练循环全部继承自 `SwiftSft`，一行不改**；`SwiftRLHF` 的全部个性都集中在构造期的准备钩子和 `_get_trainer_kwargs` 里。这就是「复用」二字的落点。

#### 4.1.3 源码精读

先看类定义——一句继承就把整套 SFT 骨架搬了过来，只换了参数类：

[swift/pipelines/train/rlhf.py:23-25](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/rlhf.py#L23-L25) —— `SwiftRLHF` 继承 `SwiftSft`，并把 `args_class` 指向 `RLHFArguments`（这样 `SwiftPipeline._parse_args` 会用 `RLHFArguments` 解析命令行）。

入口函数同样是「构造 + main」的薄封装，与 `sft_main` 完全对称：

[swift/pipelines/train/rlhf.py:247-248](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/rlhf.py#L247-L248) —— `rlhf_main` 实例化 `SwiftRLHF` 并调用继承来的 `main()`。

覆写的核心是构造期准备的三个钩子。最重要的是 `_prepare_model_tokenizer`，它在调用父类（载入主模型）**之前**，先把 ref/value/teacher/reward 这些辅助模型准备好：

[swift/pipelines/train/rlhf.py:111-167](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/rlhf.py#L111-L167) —— 按 `rlhf_type` 决定要准备哪些辅助模型，最后一句 `super()._prepare_model_tokenizer()` 才载入真正要训练的主模型。

这段逻辑里有一个「按算法裁剪模型清单」的精巧设计：

```python
for key in ['ref', 'value', 'teacher']:
    setattr(self, f'{key}_model', None)
    if key == 'ref' and args.rlhf_type == 'gkd':
        continue                       # GKD 不需要 ref 模型
    if key == 'value' and args.rlhf_type != 'ppo':
        continue                       # 只有 PPO 需要 value 模型
    if key == 'teacher' and args.rlhf_type not in ['gkd', 'grpo']:
        continue                       # 只有 GKD/GRPO 需要 teacher 模型
```

也就是说：**用一组 `if continue` 把「这个算法不需要这个模型」的情况跳过**，剩下的才调用 `_prepare_single_model` 去下载、加载并冻结。对 ref/reward/teacher 这类「只读不训」的模型，统一在 `_prepare_single_model` 里 `requires_grad_(False).eval()` 冻结：

[swift/pipelines/train/rlhf.py:96-100](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/rlhf.py#L96-L100) —— `origin_key in {'ref', 'reward', 'teacher'}` 时冻结并切 eval；只有 value 模型走 `self.prepare_model`（PPO 里 value 模型是要训练的）。

另一个覆写点是模板模式切换。RLHF 不同算法要求模板以不同方式编码，这里用一张映射表把 `rlhf_type` 翻成模板模式：

[swift/pipelines/train/rlhf.py:188-195](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/rlhf.py#L188-L195) —— `mode_mapping = {'kto': 'kto', 'gkd': 'train', 'ppo': 'transformers', 'grpo': 'train'}`，其余算法（DPO/ORPO/CPO/SimPO）走默认 `'rlhf'` 模式。

数据集钩子只有 KTO 需要特殊处理（因为 KTO 要额外构造一批「KL 样本」），其余算法直接复用父类：

[swift/pipelines/train/rlhf.py:197-202](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/rlhf.py#L197-L202) —— 当 `rlhf_type == 'kto'` 时，用 `prepare_kto_dataset` 对数据做移位补一份 KL 样本，否则原样返回父类结果。

最后是 `_get_trainer_kwargs`——它负责把准备好的辅助模型「打包」传给训练器：

[swift/pipelines/train/rlhf.py:220-244](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/rlhf.py#L220-L244) —— 把 `ref_model`/`reward_model`/`value_model`/`teacher_model`、GRPO 的 `reward_funcs`、GKD 的 `gkd_logits_topk` 等塞进 `trainer_kwargs`，最终在 `SwiftSft.run()` 里通过 `**self._get_trainer_kwargs()` 展开传给训练器构造函数。

> 顺带一提：`SwiftSft.run()` 完全感知不到这些额外参数——它只调用 `self._get_trainer_kwargs()`，子类覆写即可注入。这是钩子模式的典型用法。参见 [swift/pipelines/train/sft.py:176-185](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py#L176-L185)。

#### 4.1.4 代码实践

**实践目标**：通过阅读源码，画出 `swift rlhf --rlhf_type dpo` 从 CLI 到 `trainer.train()` 的完整调用链，标出哪些方法是继承的、哪些是覆写的。

**操作步骤**：

1. 打开 [swift/cli/rlhf.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/rlhf.py)，确认它只做一件事：调用 `rlhf_main()`。
2. 跟到 `rlhf_main` → `SwiftRLHF(args).main()`，`main()` 定义在 [swift/pipelines/base.py:49-54](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/base.py#L49-L54)，它调用 `self.run()`。
3. `run()` 定义在 [swift/pipelines/train/sft.py:159-185](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py#L159-L185)——注意它是 `SwiftSft` 的方法，`SwiftRLHF` 没有覆写 `run()`。
4. 在脑中（或纸上）标注：构造期调用的 `_prepare_model_tokenizer`/`_prepare_template` 是**覆写**的（见 rlhf.py），而 `run`/`train`/`_prepare_dataset` 的主体是**继承**的。

**需要观察的现象**：

- DPO 场景下，构造期 `_prepare_model_tokenizer` 会准备 `ref_model`（因为 DPO 在 full 训练时需要 ref），但会跳过 value/teacher（因为 `rlhf_type != 'ppo'` 且不在 `['gkd','grpo']`）。
- `_get_trainer_kwargs` 会把 `ref_model` 塞进 trainer_kwargs，从而传给 `DPOTrainer`。

**预期结果**：你会得到一张「继承 vs 覆写」分两色的调用链图，能清楚指出 `SwiftRLHF` 真正改写的只有 5 个方法（`args_class`、`_prepare_model_tokenizer`、`_prepare_template`、`_get_dataset`、`_get_trainer_kwargs`、`prepare_model`），其余全部继承。

> 待本地验证：若想确认运行期行为，可在 `_prepare_single_model` 入口处加一行 `logger.info(f'preparing {key} model for {args.rlhf_type}')`，跑一次 DPO 观察 ref 模型被准备、而 value/teacher 被跳过。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `SwiftRLHF` 不需要自己实现 `run()` 和 `train()`？

> **参考答案**：因为 `run()`/`train()` 的流程对所有训练任务（SFT/RLHF）是通用的——都是「准备数据 → 落盘参数 → 准备模型 → 装配 Trainer → 开训 → 存状态」。RLHF 的个性全在「准备哪些模型」「用什么模板模式」「给训练器传什么参数」这三件事上，而这三件事恰恰是 `SwiftSft` 暴露给子类覆写的钩子（`_prepare_model_tokenizer`/`_prepare_template`/`_get_trainer_kwargs`）。覆写钩子就够，不必重写骨架。

**练习 2**：`_prepare_model_tokenizer` 末尾为什么要调用 `super()._prepare_model_tokenizer()`？如果不调会怎样？

> **参考答案**：父类的 `_prepare_model_tokenizer` 负责载入并保存「真正要训练的主模型」（赋值给 `self.model`/`self.processor`）。`SwiftRLHF` 在前面只准备了辅助模型（ref/reward 等），主模型仍由父类载入。不调 `super()` 则 `self.model` 永远是 `None`，后续 `prepare_model` 与训练器装配都会失败。

---

### 4.2 RLHFArguments 与 rlhf_type

#### 4.2.1 概念说明

`RLHFArguments` 是 `swift rlhf` 的参数对象。它延续 u2-l1 讲过的「多继承 mixin 组合」套路，把 5 个参数组拼到一起：

```python
class RLHFArguments(TeacherModelArguments, GRPOArguments, PPOArguments,
                    RewardModelArguments, SftArguments):
```

其中最关键的一个字段是 `rlhf_type`——它是一个枚举，取值 `'dpo'/'orpo'/'simpo'/'kto'/'cpo'/'rm'/'ppo'/'grpo'/'gkd'`，默认 `'dpo'`。**这一个字段是整个 RLHF 流程的「总开关」**，它同时影响：

- **参数推断**：`__post_init__` 里一连串 `_init_*`/`_check_*` 方法都会按 `rlhf_type` 给 `beta`、`loss_type`、`loss_scale` 等填默认值或做校验。
- **ref 模型策略**：哪些算法强制要有 ref 模型、哪些禁止传 ref 模型，由 `rlhf_type` + `tuner_type` 共同决定。
- **模板模式**：见 4.1.3 的 `mode_mapping`。
- **训练器派发**：见 4.3，`TrainerFactory` 直接拿 `rlhf_type` 当路由键。

#### 4.2.2 核心流程

`RLHFArguments.__post_init__` 的执行流程是一串「初始化 + 校验」钩子：

```text
__post_init__():
  ├─ _process_loss_type()      # 处理多 loss 混合（MPO）：仅 DPO 允许多个 loss_type
  ├─ _init_grpo()              # grpo 专属：beta 默认 0.04、truncation_strategy、scale_rewards 等
  ├─ _init_rm()                # rm 专属：把 task_type 改成 seq_cls、num_labels=1
  ├─ _init_simpo()             # simpo -> cpo + loss_type='simpo' + beta=2.0（改写 rlhf_type！）
  ├─ _init_max_completion_length()
  ├─ _init_padding_side()      # ppo/gkd 强制 left padding
  ├─ _set_default()            # beta/loss_type/gradient_accumulation_steps 默认值
  ├─ _init_rollout()           # 仅 grpo/gkd：接 vLLM rollout 的准备
  ├─ _init_teacher_deepspeed()
  ├─ GRPOArguments.__post_init__()   # 显式调用父类后置（mixin 不自动链式调用）
  ├─ SftArguments.__post_init__()    # 显式调用父类后置
  ├─ _check_sequence_parallel() / _check_teacher() / _check_grpo() / _check_gkd()
  └─ ref 模型策略裁定（见下）
```

`ref 模型策略裁定`是理解 RLHF 参数的关键，逻辑很清晰：

```python
if self.rlhf_type == 'grpo' and self.beta == 0.0:
    self.ref_model = None                       # GRPO 关掉 KL（beta=0）就不需要 ref
elif self.rlhf_type in ['dpo', 'kto', 'ppo', 'grpo'] and self.tuner_type == 'full':
    self.ref_model = self.ref_model or self.model   # 全参训练必须有独立 ref 模型
    ...
elif self.ref_model is not None:
    raise ValueError('CPO/ORPO or LoRA training does not require a ref_model ...')
```

这里有个精妙之处：**LoRA 训练时不需要单独的 ref 模型**——因为 LoRA 只挂了增量参数，把增量关掉（`disable_adapter`）就能得到「参考策略」，无需再加载一份完整模型。这就是为什么 DPO+LoRA 比 DPO+full 省显存。

DPO 的损失函数本身也体现了 ref 模型的作用。设 \(\pi_\theta\) 为待训练策略、\(\pi_{\text{ref}}\) 为参考策略、\(y_w/y_l\) 为偏好/被拒回答，DPO 损失为：

\[
\mathcal{L}_{\text{DPO}} = -\log\sigma\left(\beta\left[\log\frac{\pi_\theta(y_w\mid x)}{\pi_{\text{ref}}(y_w\mid x)} - \log\frac{\pi_\theta(y_l\mid x)}{\pi_{\text{ref}}(y_l\mid x)}\right]\right)
\]

其中 \(\beta\) 就是参数里的 `beta`，控制策略偏离 ref 模型的强度。对比之下，SFT 的损失只是对示范回答的负对数似然：

\[
\mathcal{L}_{\text{SFT}} = -\sum_{t}\log\pi_\theta(y_t\mid y_{<t},x)
\]

这就是「SFT 教标准答案、DPO 比较好坏」在损失层面的差别。ms-swift 还允许通过 `rpo_alpha` 把一份 SFT 损失混进 DPO 损失（\(\mathcal{L}=\mathcal{L}_{\text{DPO}}+\text{rpo\_alpha}\cdot\mathcal{L}_{\text{SFT}}\)）以稳定训练。

#### 4.2.3 源码精读

先看 `rlhf_type` 字段本身——它就是一张允许取值的清单，默认 DPO：

[swift/arguments/rlhf_args.py:232-237](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/rlhf_args.py#L232-L237) —— `rlhf_type: Literal[...] = 'dpo'`，同时定义了 `ref_model`/`ref_adapters`/`beta` 等核心字段。

`__post_init__` 是参数推断的总入口，注意它**显式调用**了 `GRPOArguments.__post_init__` 和 `SftArguments.__post_init__`（这是 mixin 多继承的必备写法，dataclass 不会自动链式调用父类后置）：

[swift/arguments/rlhf_args.py:272-298](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/rlhf_args.py#L272-L298) —— 一串 `_init_*`/`_check_*` 之后，是上面讲过的 ref 模型策略裁定。

几个有代表性的 `_init_*` 片段能说明 `rlhf_type` 如何驱动默认值。`_set_default` 给不同算法填不同的 `beta` 与 `loss_type`：

[swift/arguments/rlhf_args.py:514-530](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/rlhf_args.py#L514-L530) —— DPO/CPO 默认 `loss_type='sigmoid'`、KTO 默认 `'kto'`、GRPO 默认 `'grpo'`；GKD 的 `beta` 默认 0.5，其余默认 0.1。

`_init_simpo` 是个有趣的细节——**它在运行期把 `rlhf_type` 从 `'simpo'` 改写成 `'cpo'`**，因为 SimPO 在实现上是 CPO 的一种 loss 变体：

[swift/arguments/rlhf_args.py:482-490](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/rlhf_args.py#L482-L490) —— `self.rlhf_type = 'cpo'`，`loss_type = 'simpo'`，`beta = 2.`。

`_init_rm` 同样改写状态——把任务类型从生成式改成判别式，因为奖励模型训练本质是一个回归/打分任务：

[swift/arguments/rlhf_args.py:492-495](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/rlhf_args.py#L492-L495) —— `self.task_type = 'seq_cls'`，`self.num_labels = 1`。

关于奖励模型参数，`RewardModelArguments` 是一个独立的 mixin，字段都支持**列表**——这是为 GRPO 的多奖励模型场景设计的：

[swift/arguments/rlhf_args.py:17-37](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/rlhf_args.py#L17-L37) —— `reward_model`/`reward_model_type`/`reward_template` 都是 `Optional[List[...]]`，`reward_adapters` 允许把 SFT 产出的 LoRA 当作奖励模型。

最后回答规格里要求讲清的 `rlhf_support_vllm_types`——它定义在文件顶部，就是一份「哪些 RLHF 算法允许用 vLLM 做 rollout 生成」的白名单：

[swift/arguments/rlhf_args.py:13](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/rlhf_args.py#L13) —— `rlhf_support_vllm_types = ['grpo', 'gkd']`。

只有 GRPO 和 GKD 需要在训练过程中**实时生成**（GRPO 要为每个 prompt 采样多条回答算奖励，GKD 要用教师生成 logits），所以只有它们值得引入 vLLM 加速生成；DPO/KTO/PPO 等不在此列。这个白名单在 `_init_rollout`/`_init_external_vllm` 里被反复用作「是否走 vLLM 准备分支」的闸门，例如：

[swift/arguments/rlhf_args.py:435-437](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/rlhf_args.py#L435-L437) —— `if self.rlhf_type not in rlhf_support_vllm_types: return`，非白名单算法直接跳过 rollout 初始化。

#### 4.2.4 代码实践

**实践目标**：对比 DPO 与 SFT 在数据格式与损失上的差异，亲手准备一份最小 DPO 数据并跑通。

**操作步骤**：

1. **准备数据**。新建 `dpo.jsonl`，每行一条偏好样本——`messages` 里最后一条 assistant 是「偏好回答（chosen）」，`rejected_response` 是「被拒回答（rejected）」：

   ```json
   {"messages":[{"role":"user","content":"1+1等于几？"},{"role":"assistant","content":"等于2。"}],"rejected_response":"我不知道。"}
   {"messages":[{"role":"user","content":"中国的首都是？"},{"role":"assistant","content":"北京。"}],"rejected_response":"上海。"}
   ```
   > 这是 ms-swift 的 DPO 标准格式：框架会找到 `messages` 里最后一条 user 之后的内容作为 chosen，把 `rejected_response` 拼到同一位置形成 rejected。详见 [docs/source_en/Customization/Custom-dataset.md](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/docs/source_en/Customization/Custom-dataset.md) 的 RLHF 一节。

2. **运行训练**（参考 [examples/train/rlhf/dpo/lora.sh](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/train/rlhf/dpo/lora.sh)）。单卡 LoRA 版本（示例命令，需本地有 GPU）：

   ```bash
   CUDA_VISIBLE_DEVICES=0 swift rlhf \
       --rlhf_type dpo \
       --model Qwen/Qwen2.5-0.5B-Instruct \
       --tuner_type lora --target_modules all-linear --lora_rank 8 --lora_alpha 32 \
       --dataset dpo.jsonl \
       --max_length 1024 --learning_rate 1e-4 --num_train_epochs 1 \
       --per_device_train_batch_size 1 --gradient_accumulation_steps 4 \
       --output_dir output_dpo --logging_steps 2
   ```

3. **对比 SFT**。用同样模型、同样数据（去掉 `rejected_response`）跑一次 `swift sft`，对比训练日志。

**需要观察的现象**：

- DPO 的训练日志里会出现 `rewards/accuracies`、`rewards/margins`、`logps/chosen`、`logps/rejected` 这类 SFT 里**没有**的指标——它们正是偏好损失派生出来的。
- DPO+LoRA 不会去额外下载 ref 模型（日志里没有 ref 模型加载信息）；若把 `--tuner_type full`，则会看到框架用 `--model` 同一个权重作为 ref 模型（对应 4.2.2 讲的 ref 策略裁定）。
- `beta` 默认 0.1、`loss_type` 默认 `sigmoid`（对应 `_set_default`）。

**预期结果**：DPO 能正常收敛（loss 下降、`rewards/accuracies` 上升趋近 1），且训练日志印证了「损失函数不同、多出偏好指标」的判断。

> 待本地验证：受限于本机是否有 GPU 与模型下载，上述命令的实际输出需在本地确认；若仅做源码阅读，可跳到 4.2.5 的练习，靠读 `_set_default`/`_process_loss_type` 也能答出差异。

#### 4.2.5 小练习与答案

**练习 1**：为什么 DPO+LoRA 不需要 `--ref_model`，而 DPO+full 需要？

> **参考答案**：DPO 损失里需要参考策略 \(\pi_{\text{ref}}\)。LoRA 训练时模型主体冻结、只挂了增量 adapter，把 adapter 关掉（`disable_adapter`）就得到参考策略，所以不必额外加载 ref 模型；而 full 全参训练会改动所有权重，无法「关掉增量」还原参考策略，必须额外加载一份冻结的 ref 模型。源码里这正对应 `elif self.rlhf_type in [...] and self.tuner_type == 'full': self.ref_model = self.ref_model or self.model`。

**练习 2**：`rlhf_support_vllm_types` 包含哪些类型？为什么是它们？

> **参考答案**：只包含 `['grpo', 'gkd']`。因为只有 GRPO 和 GKD 需要在训练循环中**实时生成**序列（GRPO 要对每个 prompt 采样 N 条回答算奖励，GKD 要用教师模型生成 logits），生成开销大、值得用 vLLM 加速；而 DPO/KTO/CPO/ORPO 只消费已有的偏好对，PPO 的生成走的是自己的 transformers 生成路径，都不接入 vLLM rollout。

**练习 3**：用户传 `--rlhf_type simpo`，最终的 `rlhf_type` 会变成什么？为什么？

> **参考答案**：会变成 `'cpo'`，但 `loss_type` 被设为 `'simpo'`、`beta` 设为 2.0。因为 SimPO 在 trl 实现里是 CPOTrainer 的一种 loss 变体，框架在 `_init_simpo` 里做了「能力映射」，把对外暴露的 `simpo` 这个名字归一到内部真正派发的 `cpo` 训练器上。

---

### 4.3 TrainerFactory RLHF 派发

#### 4.3.1 概念说明

u5-l1 讲过，`TrainerFactory` 用两张键对齐的字典 `TRAINER_MAPPING`（算法 → 训练器类）和 `TRAINING_ARGS_MAPPING`（算法 → 训练参数类）做派发，路由键默认是 `task_type`。RLHF 场景下，路由键**自动切换**为 `rlhf_type`——这是 `get_cls` 里一个 `hasattr` 判断完成的：只要参数对象有 `rlhf_type` 字段（`RLHFArguments` 必然有），就用它作路由键，否则才退回 `task_type`。

这意味着 RLHF 的训练器派发与 SFT 共用同一套 `TrainerFactory` 机制，只是换了个键。这也是为什么 `SwiftRLHF` 能原样继承 `SwiftSft.run()`——`run()` 里那句 `TrainerFactory.get_trainer_cls(args)` 对 SFT 和 RLHF 都成立，键的选取被封装在 `get_cls` 内部。

#### 4.3.2 核心流程

派发流程很简单：

```text
TrainerFactory.get_trainer_cls(args):
  └─ get_cls(args, TRAINER_MAPPING):
       ├─ train_method = args.rlhf_type  if hasattr(args, 'rlhf_type') else args.task_type
       ├─ module_path, class_name = TRAINER_MAPPING[train_method].rsplit('.', 1)
       │        # 例：'swift.rlhf_trainers.DPOTrainer' -> ('swift.rlhf_trainers', 'DPOTrainer')
       ├─ module = importlib.import_module(module_path)   # 懒加载：用到才导入
       └─ return getattr(module, class_name)              # 返回训练器类
```

训练参数类的派发（`get_training_args`）走同样的键，但多一步：用 `inspect.signature` 取出训练参数类的构造签名，把 `args` 里**多余的字段过滤掉**，只保留训练参数类认识的字段，避免传参报错。

对应到 RLHF，路由表如下（节选）：

| `rlhf_type` | 训练器 | 训练参数类 |
| --- | --- | --- |
| `dpo` | `DPOTrainer` | `DPOConfig` |
| `orpo` | `ORPOTrainer` | `ORPOConfig` |
| `kto` | `KTOTrainer` | `KTOConfig` |
| `cpo` | `CPOTrainer`（注意 `simpo` 也走这里） | `CPOConfig` |
| `rm` | `RewardTrainer` | `RewardConfig` |
| `ppo` | `PPOTrainer` | `PPOConfig` |
| `grpo` | `GRPOTrainer` | `GRPOConfig` |
| `gkd` | `GKDTrainer` | `GKDConfig` |

注意两个细节：

- `rm`（奖励模型训练）虽然 `_init_rm` 把 `task_type` 改成了 `seq_cls`，但 `get_cls` 优先用 `rlhf_type='rm'`，所以它派发到 `RewardTrainer` 而不是判别式 `Trainer`——这正是「`rlhf_type` 优先于 `task_type`」的意义。
- `simpo` 在 `_init_simpo` 里已被改写成 `cpo`，所以派发时自然落到 `CPOTrainer`。

#### 4.3.3 源码精读

`TRAINER_MAPPING` 里 SFT 与 RLHF 的条目共存在一张表里，RLHF 部分用注释隔开：

[swift/trainers/trainer_factory.py:13-28](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/trainer_factory.py#L13-L28) —— `causal_lm`/`seq_cls` 等是 SFT 的 `task_type` 键；`dpo`/`orpo`/`kto`/`cpo`/`rm`/`ppo`/`grpo`/`gkd` 是 RLHF 的 `rlhf_type` 键，两类键互不冲突。

`TRAINING_ARGS_MAPPING` 与之严格对齐：

[swift/trainers/trainer_factory.py:30-45](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/trainer_factory.py#L30-L45) —— 每个算法都配了一个对应的 Config 类（如 `DPOConfig`/`GRPOConfig`）。

派发的核心——`get_cls` 的「键选取」逻辑只有一行 `hasattr`：

[swift/trainers/trainer_factory.py:47-55](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/trainer_factory.py#L47-L55) —— 有 `rlhf_type` 就用它，否则用 `task_type`；然后 `rsplit('.', 1)` 拆出模块路径与类名，`importlib.import_module` 懒加载后 `getattr` 取类。

训练参数类的构造则多一道「按签名过滤」的工序，保证只把训练参数类认识的字段传进去：

[swift/trainers/trainer_factory.py:61-73](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/trainer_factory.py#L61-L73) —— `inspect.signature(training_args_cls).parameters` 取合法形参，把 `asdict(args)` 里不在其中的键 `pop` 掉，再 `_prepare_training_args`（RLHF 里 PPO 会注入 `world_size`）后构造训练参数对象。

> 这些 Config/Trainer 类都注册在 `swift.rlhf_trainers` 包下，参见 [swift/rlhf_trainers/__init__.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rlhf_trainers/__init__.py)，底层大多复用 trl 的实现并做了 ms-swift 的 template 适配。深入 GRPOTrainer 内部留待 u7-l2。

#### 4.3.4 代码实践

**实践目标**：通过源码阅读，回答「给定一个 `rlhf_type`，框架最终会用哪个训练器、哪个 Config」，并验证 `rlhf_type` 优先于 `task_type`。

**操作步骤**：

1. 打开 [swift/trainers/trainer_factory.py:13-45](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/trainer_factory.py#L13-L45)，把 `TRAINER_MAPPING` 与 `TRAINING_ARGS_MAPPING` 两张表对齐抄一遍（见 4.3.2 的表格）。
2. 跟读 `get_cls`（47-55 行）确认键选取：`hasattr(args, 'rlhf_type')` 为真 → 用 `rlhf_type`。
3. 思考这个场景：`swift rlhf --rlhf_type rm`，`_init_rm` 把 `task_type` 改成了 `seq_cls`。问：最终用 `RewardTrainer` 还是 `Trainer`？

**需要观察的现象 / 预期结果**：

- 因为 `RLHFArguments` 必有 `rlhf_type` 字段，`get_cls` 取 `rlhf_type='rm'`，查表得 `swift.rlhf_trainers.RewardTrainer`——尽管此时 `task_type='seq_cls'`，`rlhf_type` 仍占优先。
- 这说明：**`task_type` 在 RLHF 场景下并不参与训练器派发**，它只影响模型加载（如 rm 要按 `seq_cls` 加载带 value head 的结构）。

**预期结论**：`TrainerFactory` 是 SFT 与 RLHF 共用的统一派发器，区别仅在路由键（`task_type` vs `rlhf_type`），而键的选取由 `get_cls` 里的 `hasattr` 一句决定。

#### 4.3.5 小练习与答案

**练习 1**：`get_cls` 为什么要用 `importlib.import_module` 而不是在文件顶部直接 `from swift.rlhf_trainers import DPOTrainer`？

> **参考答案**：为了**懒加载**和保护未安装可选依赖的用户。`swift.rlhf_trainers` 依赖 trl，而 trl 是较重的可选依赖；如果在 `trainer_factory.py` 顶部直接 import，那么任何只用 SFT 的用户在首次 import `TrainerFactory` 时都会被迫加载 trl。用 `importlib.import_module` 把导入推迟到「真正要用某个训练器」的时刻，SFT 用户就完全不会触发 rlhf_trainers 的导入。这与项目里 `_LazyModule`、加速推理后端懒加载的思路一致。

**练习 2**：`get_training_args` 里那段「按 `inspect.signature` 过滤字段」的代码，如果不写会怎样？

> **参考答案**：`asdict(args)` 会把 `RLHFArguments` 的**所有**字段（包括 `model`、`dataset`、`template` 等几百个与训练参数无关的字段）摊成字典，直接传给 `DPOConfig(**args_dict)` 会因为「意外关键字参数」而报 `TypeError`。过滤一遍只保留 `DPOConfig` 构造函数认识的字段，才能把「一个超大参数对象」安全地裁成「训练器需要的训练参数子集」。

---

## 5. 综合实践

把三个模块串起来，完成下面这个「端到端追踪 + 动手」任务：

**任务**：用 `swift rlhf --rlhf_type dpo` 跑通一个最小偏好对齐实验，并产出一份「DPO vs SFT 差异报告」。

**步骤**：

1. **准备数据**：按 4.2.4 的格式写 5～10 条 `dpo.jsonl`（含 `messages` + `rejected_response`）。
2. **跑 DPO**：用 4.2.4 给的命令（小模型 + LoRA）跑 1 个 epoch，记录训练日志中的 `loss`、`rewards/accuracies`、`rewards/margins`。
3. **跑对照 SFT**：把 `dpo.jsonl` 里的 `rejected_response` 删掉（变成纯 messages），用 `swift sft` 跑同样模型与超参，记录 `loss`。
4. **源码追踪**：在日志里找到 `run sh:` 那一行（框架最终执行的真实命令），对照 4.1.2 的调用链图，标注出：
   - `rlhf_type=dpo` 是如何让 `_prepare_model_tokenizer` 准备 ref 模型、跳过 value/teacher 的；
   - `rlhf_type=dpo` 是如何让 `_set_default` 把 `beta` 填成 0.1、`loss_type` 填成 `sigmoid` 的；
   - `rlhf_type=dpo` 是如何让 `TrainerFactory.get_cls` 派发到 `DPOTrainer` + `DPOConfig` 的。
5. **撰写报告**：从「数据格式」「损失函数」「训练指标」「ref 模型」「训练器」五个维度对比 DPO 与 SFT。

**验收标准**：

- 能指出 `SwiftRLHF` 真正覆写的方法清单，并解释为何 `run()` 不在其中。
- 能说清 `rlhf_type` 这一个字段在三处（参数推断、模板模式、训练器派发）分别起了什么作用。
- 能解释 `rlhf_support_vllm_types=['grpo','gkd']` 的成因。

> 待本地验证：步骤 2、3 的实际日志数值需在本地 GPU 环境确认；若无 GPU，可只做步骤 4、5 的源码追踪与报告撰写。

## 6. 本讲小结

- `SwiftRLHF` 继承 `SwiftSft`，原样复用 `main()`/`run()`/`train()` 骨架，只覆写 5 个钩子（`_prepare_model_tokenizer`/`_prepare_template`/`_get_dataset`/`_get_trainer_kwargs`/`prepare_model`）来接入 RLHF 的多模型准备、模板模式切换与训练器额外参数。
- RLHF 在准备阶段比 SFT 多出的核心工作是「挂辅助模型」：ref（偏好类）、value（PPO）、teacher（GKD/GRPO）、reward（任意算法），每个算法按 `rlhf_type` 裁剪需要哪些，只读模型统一 `requires_grad_(False).eval()` 冻结。
- `RLHFArguments` 用多继承把 `TeacherModelArguments`/`GRPOArguments`/`PPOArguments`/`RewardModelArguments`/`SftArguments` 拼到一起；`rlhf_type` 是总开关，驱动 `__post_init__` 里一串 `_init_*`/`_check_*` 给 `beta`/`loss_type`/`loss_scale` 等填默认值。
- ref 模型策略由 `rlhf_type` + `tuner_type` 共同裁定：full 训练强制要 ref 模型，LoRA 不需要（关掉 adapter 即得参考策略）；CPO/ORPO 禁止传 ref 模型。
- `TrainerFactory` 是 SFT 与 RLHF 共用的派发器，`get_cls` 用 `hasattr(args, 'rlhf_type')` 决定路由键——有 `rlhf_type` 就用它，否则退回 `task_type`；故 `rm` 派发到 `RewardTrainer` 而非判别式 `Trainer`。
- `rlhf_support_vllm_types = ['grpo', 'gkd']`，只有需要实时生成的算法才接入 vLLM rollout；DPO/KTO 等偏好对算法不在此列。
- DPO 与 SFT 的本质差异在损失：SFT 是对示范回答的负对数似然，DPO 是基于 ref 模型对数比的偏好损失（\(\beta\) 控制 KL 强度），可用 `rpo_alpha` 混入一份 SFT 损失稳定训练。

## 7. 下一步学习建议

本讲把 RLHF 的「脚手架」搭好了——你已知 `swift rlhf` 如何复用 SFT 管道、`rlhf_type` 如何驱动一切、训练器如何派发。接下来：

- **u7-l2 GRPO 算法核心**：本讲只是点到 `GRPOTrainer`，下一讲深入 `swift/rl_core/` 讲清 GRPO 的优势（advantage）计算、多奖励函数聚合与 rollout+训练循环，这是当前最主流的强化学习算法。
- **u7-l3 奖励函数与 RM 插件**：本讲的 `reward_model` 字段下一讲会展开——讲清 orm/prm 奖励函数与判别式/生成式奖励模型的接入。
- **u7-l4 多轮 Rollout 与环境交互**：讲 GRPO 如何支撑 agent 工具调用训练，对应 `swift/rollout/` 模块。

如果想立刻巩固本讲，建议回头再读一遍 [swift/pipelines/train/rlhf.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/rlhf.py) 全文（只有 249 行），把它与 [swift/pipelines/train/sft.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py) 对照看，体会「覆写钩子而非重写骨架」的工程价值。
