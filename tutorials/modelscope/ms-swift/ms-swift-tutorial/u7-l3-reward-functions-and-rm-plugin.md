# 奖励函数与 RM 插件

## 1. 本讲目标

本讲承接 [u7-l2 GRPO 算法核心](u7-l2-grpo-algorithm-core.md)，把视角从「奖励如何聚合成 advantage」下沉到「奖励分数本身从哪里来」。

读完本讲，你应当能够：

- 说清 ms-swift 中三类奖励信号——**规则型奖励函数（ORM/AsyncORM）**、**判别式奖励模型（带 value head 的分类模型）**、**生成式奖励模型（LLM-as-judge）**——的区别与各自适用场景。
- 看懂 `swift/rewards/` 三个文件（`orm.py` / `prm.py` / `rm_plugin.py`）的注册表与基类契约，并能仿照内置奖励写出自己的奖励函数。
- 理解 `GRPOTrainer._prepare_rewards` 如何把命令行里的 `--reward_funcs` / `--reward_model` / `--reward_model_plugin` 字符串，统一拼装成一张「奖励函数列表 + 插件列表 + 权重列表」并最终汇入 advantage。
- 能用 `--external_plugins` 注册一个自定义奖励函数，跑通一次小规模 GRPO，并能选用 `default` 或 `genrm` 插件接入奖励模型。

## 2. 前置知识

本讲默认你已掌握 u7-l2 的结论，这里只做最小回顾，并补充三个本讲会用到的术语。

**回顾（来自 u7-l2）**：GRPO 对同一个 prompt 采样 \(G\) 条回答（completions），对每条回答用若干个**奖励函数（reward function）**打分，得到形状为 `[N, n_funcs]` 的张量（\(N\) 是采样到的回答总数，\(n_funcs\) 是奖励函数个数），再用 `reward_weights` 加权求和成单列 reward，最后做组内归一化得到 advantage。换句话说，奖励函数是 GRPO 唯一的「监督信号来源」——没有它，RL 就没有方向。

**三个新术语**：

| 术语 | 含义 | 例子 |
| --- | --- | --- |
| ORM（Outcome Reward Model） | 只看**最终结果**给一个标量奖励。在 ms-swift 里它既指「结果型奖励」这个概念，也指 `swift/rewards/orm.py` 里**所有规则型奖励函数的基类**。 | 答案对给 1、错给 0 的 `accuracy` |
| PRM（Process Reward Model） | 看**推理过程**每一步给奖励。ms-swift 里 `prm.py` 目前仅用于采样，**不支持 GRPO 训练**（文件头有明确注释）。 | 让大模型逐步打分的 `qwen_max` |
| RM 插件（reward_model_plugin） | 当奖励源是一个**神经网络模型**（而不是纯 Python 函数）时，插件负责「把对话喂进模型、把模型输出翻译成 reward」。 | `default`（取 logits）、`genrm`（让 LLM 输出分数） |

还有一个关键点：**奖励函数的输入不只是 completions**。训练数据集里的每一列（如标准答案 `solution`、可用数字 `nums`）都会被打包成关键字参数（`kwargs`）一并传给奖励函数。这样 `accuracy` 类奖励才能拿到标准答案去比对。我们会在 4.1 讲清这条「列→kwargs」的传递链。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [swift/rewards/orm.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rewards/orm.py) | 规则型奖励函数的大本营：定义 `ORM`/`AsyncORM` 基类、一堆内置奖励（数学正确性、格式、重复惩罚、长度余弦等），以及注册表 `orms`。 |
| [swift/rewards/prm.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rewards/prm.py) | 过程奖励模型（PRM）实现，通过调用外部大模型（如 qwen-max）给整段回答打分；注册表 `prms`。**仅用于采样，不支持 GRPO。** |
| [swift/rewards/rm_plugin.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rewards/rm_plugin.py) | 奖励**模型**插件：`DefaultRMPlugin`（判别式，取 logits）与 `GenRMPlugin`（生成式，LLM-as-judge）；注册表 `rm_plugins`。 |
| [swift/rl_core/grpo_algorithm.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rl_core/grpo_algorithm.py) | 算法层：`compute_rewards_per_func` 统一调度同步/异步/模型三类奖励函数，产出 `[N, n_funcs]` 张量。 |
| [swift/rlhf_trainers/grpo_trainer.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rlhf_trainers/grpo_trainer.py) | 训练器层：`_prepare_rewards` 把命令行参数装配成奖励函数/插件/权重三张表。 |
| [swift/rl_core/data.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rl_core/data.py) | `GRPOSample.to_reward_row` 把样本拍平成奖励函数消费的字典（含数据集列）。 |
| [examples/train/grpo/plugin/plugin.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/train/grpo/plugin/plugin.py) | 官方自定义奖励函数示例（Countdown 游戏），示范「写类 → 写进 `orms` → 用 `--external_plugins` 加载」三步法。 |

---

## 4. 核心概念与源码讲解

### 4.1 奖励函数在 GRPO 中的位置：从打分到 advantage 的统一通道

#### 4.1.1 概念说明

GRPO 训练里，「奖励」可能来自非常异质的来源：一段 Python 正则、一次本地神经网络前向、一次远程 HTTP 调用、甚至一个 gym 环境的返回值。如果让训练循环去逐一适配这些来源，代码会非常混乱。

ms-swift 的做法是**统一契约**：不管奖励来自哪里，最终都要变成「对一批 completions 返回一个等长的浮点数列表」。算法层（`swift/rl_core/`）只认这个契约，不关心你内部是正则还是大模型；具体实现（`swift/rewards/`）只负责兑现这个契约。这就是为什么 `orm.py`、`prm.py`、`rm_plugin.py` 三个文件能各自独立演化，却都能无缝接入 GRPO。

#### 4.1.2 核心流程

一条 completion 从「被生成」到「贡献到 loss」要经过这样一条链路：

```text
采样得到 N 条 completion（封装成 GRPOSample）
        │
        ▼
GRPOSample.to_reward_row()       # 拍平成 dict，含 messages + 数据集列(solution/target…)
        │
        ▼
RowPreprocessor.rows_to_batched() # 多行 → 批量 kwargs：completions、solution、target…
        │
        ▼
compute_rewards_per_func()        # 遍历每个奖励函数，分三类调度：
        │                         #   ① nn.Module  →  调用对应的 rm_plugin
        │                         #   ② async 函数 →  asyncio.gather 并发执行
        │                         #   ③ 普通 callable → 直接 __call__(completions, **kwargs)
        ▼
rewards_per_func: [N, n_funcs]    # 每个奖励函数贡献一列
        │
        ▼
rewards = (rewards_per_func * reward_weights).nansum(dim=1)   # 加权求和成单列
        │
        ▼
组内归一化 → advantage → 策略梯度 loss（详见 u7-l2）
```

注意第 ④ 步的 `nansum`：它和普通 `sum` 的区别在于把 `NaN` 当 0 处理。这配合「奖励函数可以返回 `None`」的约定——某个奖励函数对某条样本给不出分数时返回 `None`，调度器会把 `None` 替换成 `NaN`，最终在加权求和时被忽略，而不会污染整批奖励。

#### 4.1.3 源码精读

调度的总入口是 `compute_rewards_per_func`，它先用 `to_reward_row` + `rows_to_batched` 把数据集列铺成 kwargs，再按奖励函数类型三分支处理：

[swift/rl_core/grpo_algorithm.py:L45-L67](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rl_core/grpo_algorithm.py#L45-L67) —— 先准备 `rewards_per_func` 张量、抽取 `completions`、把每个样本拍平成 `reward_row` 并批量展开成 `reward_kwargs`（这就是 `solution`/`target` 等列能流进奖励函数的根因）；随后三分支：模型走插件、异步先跳过、普通函数直接调用。返回的 `None` 一律替换为 `NaN`。

异步奖励函数随后被统一并发执行：

[swift/rl_core/grpo_algorithm.py:L69-L78](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rl_core/grpo_algorithm.py#L69-L78) —— 用 `asyncio.gather` 并发跑所有异步奖励函数。这就是为什么 `AsyncORM` 的文档强调「多个 API 调用可以并发」——网络型奖励（如调远程判分服务）写成 `async def` 能显著加速。

如果一个样本在**所有**奖励函数上都返回 `None`（即某一行全是 `NaN`），会打一条警告，提示你至少要有一个奖励函数给出有效分数：

[swift/rl_core/grpo_algorithm.py:L80-L85](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rl_core/grpo_algorithm.py#L80-L85) —— 全 `NaN` 行检测与告警，便于排查「奖励函数没生效」的问题。

最后，加权求和那一步在算法层和训练器层各有一份（训练器层还会叠加 KL 惩罚）：

[swift/rl_core/grpo_algorithm.py:L130-L139](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rl_core/grpo_algorithm.py#L130-L139) —— 算法层把 `[N, n_funcs]` 与 `reward_weights` 加权 `nansum` 成 `[N]`。

[swift/rlhf_trainers/grpo_trainer.py:L441-L443](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rlhf_trainers/grpo_trainer.py#L441-L443) —— 训练器层同样的加权求和，并在开启 `kl_in_reward` 时减去 \(\beta \cdot \mathrm{KL}\)。即最终有效奖励为：

\[
r_i = \mathrm{nansum}_f\!\left(R_{i,f}\cdot w_f\right) - \beta \cdot \mathrm{KL}_i
\]

#### 4.1.4 代码实践：跟踪奖励参数的装配

这是一个**源码阅读型实践**，目标是让你看清「命令行字符串 → 奖励函数对象」的完整装配过程，不必真正跑训练。

1. **实践目标**：理清 `_prepare_rewards` 如何把 `args.reward_funcs`、`args.reward_model`、`args.reward_model_plugin` 三组参数拼成统一的三张表。
2. **操作步骤**：
   - 打开 `swift/rlhf_trainers/grpo_trainer.py`，跳到 `_prepare_rewards`（2121 行起）。
   - 先看 `args.reward_funcs` 的字符串如何被 `orms` 字典翻译成实例（2130 行附近）。
   - 再看 `reward_model` 不为 `None` 时，如何为每个奖励模型实例化插件、并把模型本身追加进 `reward_funcs`（2147 行附近）。
   - 最后看 `reward_weights` 的长度校验与默认全 1（2172 行附近）。
3. **需要观察的现象**：`reward_funcs` 列表、`reward_model_plugins` 列表、`reward_func_names` 列表三者**等长且逐位置对齐**——这是「奖励函数 + 它的插件（可能为 None）+ 它的名字」的三元组。
4. **预期结果**：你能用自己的话说出「为什么文档强调 `reward_weights` 的顺序对应 `[reward_funcs, reward_model]`」——因为奖励模型被**追加**在奖励函数列表末尾。

参考装配入口：

[swift/rlhf_trainers/grpo_trainer.py:L2121-L2191](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rlhf_trainers/grpo_trainer.py#L2121-L2191) —— `_prepare_rewards` 全流程：字符串→实例、插件实例化、权重装配、模型分布式准备（DeepSpeed/FSDP）。

而 `args.reward_funcs` 是在管道层透传进训练器的：

[swift/pipelines/train/rlhf.py:L232](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/rlhf.py#L232) —— 把命令行 `--reward_funcs` 原样塞进 `trainer_kwargs`。

> 说明：本实践不涉及运行命令，结果可在阅读源码后直接得出。

#### 4.1.5 小练习与答案

**练习 1**：如果某个奖励函数对某条 completion 返回 `None`，最终这条样本的 reward 会变成多少？
**答案**：调度器把 `None` 替换成 `NaN`，加权求和用 `nansum`，等价于该函数对该样本贡献 0；只要还有别的奖励函数给出有效分数，这条样本仍有 reward。只有「所有函数都返回 None」时才会触发告警，此时该样本 reward 实际无效。

**练习 2**：为什么异步奖励函数要用 `asyncio.gather` 而不是顺序 `await`？
**答案**：奖励函数常涉及网络 I/O（调远程判分 API）。顺序 `await` 会等前一条返回再发下一条，总耗时是各次延迟之和；`gather` 并发发出所有请求，总耗时近似于最慢的一次，大幅缩短打分阶段耗时。

---

### 4.2 ORM 与 PRM：规则型与模型型奖励函数

> 对应最小模块：**orm/prm 奖励**。

#### 4.2.1 概念说明

这是最常见的一类奖励：**用纯 Python 逻辑给 completions 打分**。它的基类叫 `ORM`（Outcome Reward Model），名字里带「Model」其实是历史命名——它根本不需要任何神经网络，只是一个实现了 `__call__` 的普通类。

`ORM` 解决的问题是「规则可判定的奖励」：格式对不对（`format`）、答案算出来等不等（`accuracy`）、是不是在循环重复（`repetition`）。这类奖励**精确、零成本、可复现**，是 GRPO 的首选信号。

与之相对的 `PRM`（Process Reward Model）则把「打分」这件事外包给一个会推理的大模型，让它评估回答的**过程质量**——比如逻辑是否连贯、步骤是否合理。ms-swift 的 `prm.py` 走的是「调外部大模型 API」的路线（LLM-as-judge 的一种）。需要特别注意：**`prm.py` 目前只用于采样，不支持 GRPO 训练**（见文件头注释），本讲介绍它主要是为了和 ORM 做对照，并衔接 4.4 的生成式奖励。

#### 4.2.2 核心流程

一个奖励函数的生命周期非常简单：

```text
GRPOTrainer._prepare_rewards()
   │  读 args.reward_funcs = ['format', 'accuracy', ...]
   │  对每个名字查 orms 字典 → 拿到类 → orms[name](args=args) 实例化
   ▼
reward_funcs = [Format(args), MathAccuracy(args), ...]   # 一列实例
   │
   ▼ （训练循环每一步）
func(completions, solution=..., target=..., **kwargs)    # completions 是 List[str]
   │
   ▼
返回 List[float]，长度 = len(completions)
```

两个关键约定：

1. **第一个位置参数永远是 `completions`**：它是当前 batch 里所有模型回答文本的列表。
2. **其余数据列以关键字参数传入**：`solution`、`target`、`nums` 等是数据集列名（由 4.1 的 `rows_to_batched` 注入）。所以你的奖励函数签名应当写成 `def __call__(self, completions, solution, **kwargs)`，只声明你真正要用到的列，其余收进 `**kwargs` 兜底。

#### 4.2.3 源码精读

先看两个基类。同步基类 `ORM` 只规定 `__init__` 接受可选的 `args`、`__call__` 必须由子类实现：

[swift/rewards/orm.py:L16-L31](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rewards/orm.py#L16-L31) —— `ORM` 基类。注意它的 docstring 就是一个最小范例：`__call__` 收 `completions`、返回 `List[float]`。

异步基类 `AsyncORM` 的 `__call__` 是 `async def`，会被 4.1 的 `asyncio.gather` 通道并发执行：

[swift/rewards/orm.py:L34-L66](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rewards/orm.py#L34-L66) —— `AsyncORM` 基类，docstring 给了一个用 `aiohttp` 并发调外部打分 API 的完整模板，是写「远程判分类奖励」的范本。

再看三个有代表性的内置实现，它们展示了三种典型的打分思路：

[swift/rewards/orm.py:L123-L129](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rewards/orm.py#L123-L129) —— `Format`：用正则检查回答是否严格符合 `<think>...</think><answer>...</answer>` 格式，符合给 1 否则 0。这是最便宜的「格式奖励」。

[swift/rewards/orm.py:L69-L120](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rewards/orm.py#L69-L120) —— `MathAccuracy`：依赖 `math_verify` 包，从 `<answer>` 标签里抽取答案、与标准答案 `solution` 做数学等价比对。注意它的签名是 `__call__(self, completions, solution, **kwargs)`——`solution` 就是数据集列注入的。构造期还会 `assert` 检查 `math_verify` 是否安装，这是「可选依赖按需校验」的范例。

[swift/rewards/orm.py:L176-L213](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rewards/orm.py#L176-L213) —— `RepetitionPenalty`：用 n-gram 重复率给一个非正的惩罚分，重复越严重惩罚越大。它不需要数据集列，签名只有 `__call__(self, completions, **kwargs)`。

所有内置奖励在文件末尾汇总成注册表 `orms`，键就是 `--reward_funcs` 要填的名字：

[swift/rewards/orm.py:L455-L464](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rewards/orm.py#L455-L464) —— `orms` 注册表。`toolbench`/`math`/`accuracy`/`format`/`react_format`/`cosine`/`repetition`/`soft_overlong` 即命令行可选的内置奖励名。

`_prepare_rewards` 里「字符串→实例」就是查这张表：

[swift/rlhf_trainers/grpo_trainer.py:L2128-L2134](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rlhf_trainers/grpo_trainer.py#L2128-L2134) —— 名字在 `orms` 里就实例化（传入 `args`），既不在表里又不可调用就直接报错。

至于 PRM，它的基类同样只规定 `__call__`，但实现走「调外部大模型」路线：

[swift/rewards/prm.py:L12-L15](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rewards/prm.py#L12-L15) —— `PRM` 基类。

[swift/rewards/prm.py:L96-L150](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rewards/prm.py#L96-L150) —— `ClientPRM`：用 `InferClient`（OpenAI 兼容客户端）调一个远程大模型（默认 qwen-plus），把对话拼成一个「请给这段回答打 -1~1 分」的 query，再从模型回复里正则抽取 `Reward: xxx`。它本质就是「把判分外包给 LLM」。

[swift/rewards/prm.py:L153-L156](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rewards/prm.py#L153-L156) —— `prms` 注册表，目前有 `qwen_max` 与 `client` 两项。

> 小结：`ORM` 偏「规则/本地计算」，`PRM` 偏「调外部大模型判过程分」。两者都返回 `List[float]`，但 PRM 仅用于采样。下一节的 `DefaultRMPlugin` 才是 GRPO 训练中接入「奖励模型」的正路。

#### 4.2.4 代码实践：写一个「长度奖励」并注册

这是本讲的核心动手实践：仿照内置 `Format`，写一个根据输出长度打分的自定义奖励函数，并通过 `--external_plugins` 注册进 GRPO。

1. **实践目标**：掌握「写类 → 写进 `orms` → 命令行启用」三步法，并理解数据集列如何流进奖励函数。
2. **操作步骤**：
   - 新建文件 `my_reward.py`，内容如下（**示例代码**，非项目原有文件）：

     ```python
     # my_reward.py —— 示例代码
     from swift.rewards import ORM, orms

     class LengthReward(ORM):
         """对长度落在 [low, high] 区间的回答给 1，否则线性衰减到 0。"""

         def __init__(self, args=None, low=20, high=200, **kwargs):
             super().__init__(args, **kwargs)
             self.low = low
             self.high = high

         def __call__(self, completions, **kwargs) -> list:
             rewards = []
             for c in completions:
                 n = len(c.split())
                 if self.low <= n <= self.high:
                     rewards.append(1.0)
                 elif n < self.low:
                     rewards.append(n / self.low)            # 太短，部分分
                 else:
                     rewards.append(max(0.0, self.high / n))  # 太长，衰减
             return rewards

     # 关键一步：把名字写进注册表，命令行才能用 --reward_funcs length_reward
     orms['length_reward'] = LengthReward
     ```

   - 用一条最小 GRPO 命令加载它（**示例命令**，需替换模型/数据集；完整可参考官方示例）：

     ```bash
     # 示例命令
     CUDA_VISIBLE_DEVICES=0 NPROC_PER_NODE=1 \
     swift rlhf \
         --rlhf_type grpo \
         --model Qwen/Qwen2.5-1.5B-Instruct \
         --external_plugins /abs/path/to/my_reward.py \
         --reward_funcs length_reward format \
         --reward_weights 0.3 0.7 \
         --dataset <你的数据集> \
         --num_generations 4 \
         --max_completion_length 512 \
         --per_device_train_batch_size 4 \
         --learning_rate 1e-6 \
         --max_steps 20
     ```

   - 官方更完整的自定义奖励示例（Countdown 游戏）可对照阅读：

     [examples/train/grpo/plugin/plugin.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/train/grpo/plugin/plugin.py) —— 文件顶部 docstring 写明了自定义奖励的三步法，`CountdownORM` 示范了如何用 `target`/`nums` 数据集列做规则判定。

3. **需要观察的现象**：
   - 启动日志应出现 `Successfully imported external_plugins: [...]`（由 `_import_external_plugins` 打印）。
   - 训练日志里每个奖励函数会按 `reward_func_names`（`length_reward`、`format`）分别打印均值，便于确认两个奖励都在生效。
4. **预期结果**：训练能正常跑完 20 步，且日志里 `length_reward` 列的均值不为 0、随训练有变化；说明自定义奖励已被正确注册和调用。
5. 若本地无 GPU 或无合适数据集，**待本地验证**：可退化为只做 4.1.4 的源码阅读实践。

`--external_plugins` 加载插件本身的机制（把 `.py` 文件 import 进来，从而触发你对 `orms` 的赋值）在这里：

[swift/arguments/base_args/base_args.py:L142-L155](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/base_args.py#L142-L155) —— `_import_external_plugins` 逐个 `import_external_file`，这正是「写进 `orms` 的赋值得以执行」的根因。

#### 4.2.5 小练习与答案

**练习 1**：为什么奖励函数签名通常写成 `def __call__(self, completions, solution, **kwargs)` 而不是固定列全列出来？
**答案**：因为不同 batch、不同数据集携带的列不一样；只声明你需要的列、其余用 `**kwargs` 兜底，可以避免「该数据集没有某一列就报参数错误」。`completions` 是唯一固定位置参数。

**练习 2**：`PRM`（如 `ClientPRM`）能否直接放进 `--reward_funcs` 用于 GRPO？
**答案**：不能。`prm.py` 文件头明确注释「GRPO training is not currently supported」。`_prepare_rewards` 只查 `orms` 表，不查 `prms`。若想在 GRPO 里用「LLM 打分」，应走 4.4 的生成式 RM 插件，或把判分逻辑自己包成一个 `ORM`（可参考 4.4.3 末尾的外部部署范式）。

**练习 3**：`MathAccuracy` 在 `__init__` 里 `assert importlib.util.find_spec('math_verify')`，这种写法的好处是什么？
**答案**：把「只在用到时才需要」的重依赖（`math_verify`）做成**按需校验**——不用 `accuracy` 奖励的用户不必安装它，安装包不必强制拉入；只有真正选用该奖励时才报错提示安装，错误信息也更精准。

---

### 4.3 DefaultRMPlugin：判别式奖励模型

> 对应最小模块：**DefaultRMPlugin 判别式 RM**。

#### 4.3.1 概念说明

规则奖励虽好，但很多任务「对错」无法用正则判定（比如回答是否流畅、是否有害、是否礼貌）。这时需要一个**奖励模型（Reward Model）**——一个专门训练过「给回答打分」的神经网络。

ms-swift 默认假设奖励模型是**判别式**的：即在普通语言模型主干顶上接一个输出维度为 1 的 **value head**（分类头），模型对一条回答前向后吐出一个标量，这个标量就是奖励分。这类模型常被称为 ORM（Output/Outcome Reward Model），与 4.2 的规则基类同名但含义略不同——这里强调的是「它是一个带 value head 的网络」。

`DefaultRMPlugin` 就是这种判别式 RM 的**默认胶水**：它知道如何把一批对话喂进模型、如何取出那个标量。它的存在让你无需改任何代码，只要 `--reward_model <模型id>` 就能接入一个 HuggingFace 风格的奖励模型。

#### 4.3.2 核心流程

```text
命令行: --reward_model <RM路径>            （不指定 plugin 时默认 'default'）
              │
              ▼
GRPOTrainer.__init__ 收到 reward_model（已加载的 nn.Module 列表）+ reward_template
              │
              ▼
_prepare_rewards():  对每个 RM：
   rm_template.set_mode('train')            # 用训练式编码（带 labels 位置语义）
   rm_template.max_length = None            # 关闭截断（输入早已在上游截断过）
   rm_plugins['default'](model=rm, template=rm_template)   # 实例化 DefaultRMPlugin
   把 RM 本身追加进 reward_funcs 列表        # 这样算法层 isinstance(nn.Module) 命中
              │
              ▼ （每步打分时）
compute_rewards_per_func 遇到 nn.Module 分支：
   plugin(inputs=reward_rows, **reward_kwargs)
       ├─ 对每个 infer_request 调 template.encode → data_collator 成 batch
       ├─ to_device(batch, model.device)
       └─ with torch.inference_mode(): model(**batch).logits[:, 0]   # 取第一个 logit 当 reward
              │
              ▼
返回形状 [N] 的张量，作为该 RM 这一列的奖励
```

注意「RM 被追加进 `reward_funcs`」这一步的妙处：算法层 `compute_rewards_per_func` 用 `isinstance(reward_func, nn.Module)` 来识别「这是个模型」，进而走插件分支而不是普通函数分支。所以**奖励模型和规则函数在同一个列表里、用同一套加权机制混合**，只是调度时各走各的通道。

#### 4.3.3 源码精读

插件本身只有短短几行，但浓缩了「编码 → 前向 → 取标量」三件事：

[swift/rewards/rm_plugin.py:L20-L38](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rewards/rm_plugin.py#L20-L38) —— `DefaultRMPlugin`。docstring 明确假设「self.model 是带 value head（输出维 1）的分类模型，取第一个 logit 当 reward」。`__call__` 里用 `deepcopy` 防止修改原始输入，`template.encode` + `data_collator` 完成 token 化与对齐，`torch.inference_mode()` 关闭梯度，最后 `.logits[:, 0]` 取每条样本第一个位置的输出作为分数。

装配侧的关键代码（模板设置 + 插件实例化 + 追加进列表）：

[swift/rlhf_trainers/grpo_trainer.py:L2147-L2164](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rlhf_trainers/grpo_trainer.py#L2147-L2164) —— `reward_model` 分支：`reward_model_plugin` 默认 `['default'] * len(reward_model)`；逐个把模板切到 train 模式、关掉截断、查 `rm_plugins` 表实例化插件，并把 RM 追加进 `reward_funcs` 与 `reward_func_names`（名字取 `model.config._name_or_path` 的末段）。这就解释了文档里「`reward_weights` 顺序对应 `[reward_funcs, reward_model]`」——RM 永远在列表末尾。

权重装配与默认值：

[swift/rlhf_trainers/grpo_trainer.py:L2172-L2179](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rlhf_trainers/grpo_trainer.py#L2172-L2179) —— `reward_weights` 不指定时默认全 1（等权）；指定时长度必须等于 `reward_func_names`（含追加的 RM 与 gym_reward）。

相关命令行参数的文档：

[swift/rlhf_trainers/args_mixin.py:L265-L267](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rlhf_trainers/args_mixin.py#L265-L267) —— `reward_model` 与 `reward_model_plugin` 参数说明；`reward_model_plugin` 默认即 `default`（ORM 取 logits 逻辑）。

注册表只有两项：

[swift/rewards/rm_plugin.py:L230-L233](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rewards/rm_plugin.py#L230-L233) —— `rm_plugins = {'default': DefaultRMPlugin, 'genrm': GenRMPlugin}`。

#### 4.3.4 代码实践：接入一个判别式奖励模型

1. **实践目标**：用 `--reward_model` + 默认 `default` 插件，把一个带 value head 的奖励模型接入 GRPO，并与规则奖励混合。
2. **操作步骤**（**示例命令**，需自备一个带 value head 的 RM，如社区训练的 `RewardModel`）：

   ```bash
   # 示例命令
   CUDA_VISIBLE_DEVICES=0,1 NPROC_PER_NODE=2 \
   swift rlhf \
       --rlhf_type grpo \
       --model Qwen/Qwen2.5-1.5B-Instruct \
       --reward_funcs format \
       --reward_model <你的判别式RM路径> \
       --reward_weights 0.5 0.5 \
       --dataset <你的偏好/问答数据集> \
       --num_generations 4 \
       --max_completion_length 512 \
       --per_device_train_batch_size 2 \
       --learning_rate 1e-6 \
       --max_steps 20
   ```

3. **需要观察的现象**：训练日志的 `reward_func_names` 应同时含 `format` 与你的 RM 名（来自 `config._name_or_path` 末段）；两列奖励均值都会打印。
4. **预期结果**：训练正常启动，RM 被加载（若显存吃紧可加 `--deepspeed zero2`，因为 `_prepare_rewards` 末尾会按需把 RM 包进 DeepSpeed/FSDP）。
5. 若本地无合适的判别式 RM，**待本地验证**：可只阅读 `DefaultRMPlugin.__call__` 与 `_prepare_rewards` 的 RM 分支，确认自己理解「`.logits[:, 0]` 为何能当 reward」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `DefaultRMPlugin.__call__` 要对每个 `infer_request` 做 `deepcopy` 再 `encode`？
**答案**：`template.encode` 可能就地修改输入（如多模态占位符替换、裁剪）；奖励样本和训练样本共享同一份 `messages` 对象，若被插件改坏会影响主训练。`deepcopy` 保证打分过程对主流程无副作用。

**练习 2**：`rm_template.set_mode('train')` 和 `rm_template.max_length = None` 各自的用意？
**答案**：`set_mode('train')` 让模板按训练语义编码（与该 RM 训练时的格式一致，避免推理/训练分布不一致）；`max_length = None` 关闭模板内的截断，因为输入序列在更上游（rollout 阶段）已经按 `max_completion_length` 处理过，这里再截断反而可能切掉需要打分的内容。

---

### 4.4 GenRMPlugin 与外部部署：生成式奖励模型（LLM-as-judge）

> 对应最小模块：**生成式 RM 接入**。

#### 4.4.1 概念说明

判别式 RM 需要「专门训练一个带 value head 的模型」，成本高、通用性差。另一种流行做法是**生成式奖励模型（Generative RM）**：直接拿一个**普通指令模型**（甚至是 GPT-4、Qwen-Max 这类大模型）当裁判，让它读对话、**输出一段文字**，文字里带上一个分数。这就是所谓的 **LLM-as-judge**。

`GenRMPlugin` 是 ms-swift 内置的生成式 RM 插件范例：它在插件内部包了一个 `TransformersEngine`，把「待打分的对话」改写成「请评分」的 prompt 喂给裁判模型，再用正则从裁判的回复里抠出 `Reward: 0.85` 这样的分数。

接入生成式 RM 有两条路（详见官方文档）：

- **内置插件（`genrm`）**：裁判模型嵌在 Trainer 进程里，用 `TransformersEngine` 跑生成。优点是零额外部署、与训练同进程；缺点是生成慢，只适合**小参数**裁判模型。
- **外部部署**：裁判模型用 `swift deploy` / `vllm serve` 单独部署成 OpenAI 兼容服务，奖励函数里用 OpenAI 客户端去调。适合**大参数**裁判模型，速度更快，但要额外硬件。

#### 4.4.2 核心流程

内置 `genrm` 的打分流程：

```text
__call__(inputs):
   prepare_rm_inputs(inputs):
       对每条 infer_request：
         messages_to_query(messages)        # 把多轮对话压成 "User: ...\nAssistant: ..." 纯文本
         组装新 messages = [system(评分说明), user(压平的对话)]
   engine.infer(rm_inputs, request_config)  # TransformersEngine 生成裁判回复
   compute_rewards(results):
       对每条回复：extract_reward(text)     # 正则匹配 'Reward: 0.xx'
       多个 choice 取平均；抽取失败给 0
   返回 torch.tensor(rewards)
```

裁判模型被要求在回复**结尾**按固定格式 `Reward: {reward}` 给分，`extract_reward` 才能用一个简单正则稳定抽出分数。这种「让 LLM 把结构化结果放到固定锚点」是 LLM-as-judge 工程化的常见套路。

#### 4.4.3 源码精读

`GenRMPlugin` 继承 `DefaultRMPlugin`（复用 `__init__` 里对 `model`/`template` 的持有），但完全重写了 `__call__`：

[swift/rewards/rm_plugin.py:L41-L92](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rewards/rm_plugin.py#L41-L92) —— `GenRMPlugin`。构造期建 `TransformersEngine`（`max_batch_size=0` 表示不限批量）、默认 `RequestConfig`，并定义一段 `system` 提示，要求模型「分析准确性/完整性/相关性后，在结尾按 `Reward: {reward}` 给一个 0~1 的分」。`__call__` 三步走：`prepare_rm_inputs` → `engine.infer` → `compute_rewards`，最后转成 `torch.float32` 张量。

正则抽分与容错：

[swift/rewards/rm_plugin.py:L121-L140](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rewards/rm_plugin.py#L121-L140) —— `extract_reward`：用 `re.search(r'Reward:\s*([0-1](?:\.\d+)?)', ...)` 抠出分数；匹配不到返回 `None`（后续被当作抽取失败处理），并打 warning。注意正则限定 `[0-1]` 范围，与 system prompt 里「0 到 1」的约定一致。

把多轮对话压平成单段 query：

[swift/rewards/rm_plugin.py:L142-L196](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rewards/rm_plugin.py#L142-L196) —— `messages_to_query`：把每条 message 格式化成 `Role: content` 再用换行拼接，让裁判模型看到完整对话上下文。

对一批裁判回复聚合出最终奖励（含多重容错）：

[swift/rewards/rm_plugin.py:L198-L227](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rewards/rm_plugin.py#L198-L227) —— `compute_rewards`：每条输出可能有多个 choice，对每个 choice 抽分、过滤掉 `None`、取平均；若全失败给 0.0 并告警；任何异常都兜底成 0.0。这种「层层兜底」保证裁判模型偶发抽风不会让整批奖励崩成异常。

**外部部署** 则不走插件，而是自己写一个 `ORM`，在 `__call__` 里用 OpenAI 客户端调你部署的服务。官方文档给出了完整模板：

[docs/source_en/Instruction/GRPO/DeveloperGuide/reward_model.md:L104-L142](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/docs/source_en/Instruction/GRPO/DeveloperGuide/reward_model.md#L104-L142) —— 外部部署范例：`RMReward(ORM)` 在 `__init__` 里建 `OpenAI(base_url='http://127.0.0.1:8000/v1')` 客户端，在 `__call__` 里构造判分 prompt、调 `chat.completions.create`、从回复里提取分数。

> 提示：这种「外部部署 + 自写 ORM」的范式，本质上把 4.2 的规则函数和 4.4 的 LLM 判分合二为一——你完全可以把 `ClientPRM`（4.2.3）的思路搬过来写成一个 `ORM`，从而让 LLM 判分也能进 GRPO（因为 `_prepare_rewards` 只认 `orms`）。

#### 4.4.4 代码实践：切换到生成式 RM 插件

1. **实践目标**：对比「不加 RM 插件（纯规则）」与「加 `genrm` 生成式裁判」的效果差异，理解 LLM-as-judge 的接入方式。
2. **操作步骤**（**示例命令**，需自备一个可当裁判的指令模型）：
   - 基线（纯规则奖励）：

     ```bash
     # 示例命令
     swift rlhf --rlhf_type grpo --model <policy> \
         --reward_funcs format accuracy \
         --dataset <数学数据集> --max_steps 20 ...
     ```

   - 加生成式裁判（把裁判模型作为 reward_model、插件指定为 `genrm`）：

     ```bash
     # 示例命令
     swift rlhf --rlhf_type grpo --model <policy> \
         --reward_funcs format \
         --reward_model <裁判模型，如 Qwen/Qwen2.5-7B-Instruct> \
         --reward_model_plugin genrm \
         --reward_weights 0.3 0.7 \
         --dataset <数据集> --max_steps 20 ...
     ```

3. **需要观察的现象**：
   - 开启 `genrm` 后，每个打分步会多出「裁判模型生成」的耗时，训练明显变慢（这是文档强调「大模型请用外部部署」的原因）。
   - 日志里裁判模型这一列的奖励分布应更「平滑」（连续分），而 `format` 仍是 0/1 离散分。
   - 可关注 `extract_reward` 抽分失败时的 warning 频率——若频繁出现，说明裁判模型没遵守 `Reward: x` 格式，需要调 system prompt。
4. **预期结果**：两条命令都能跑完；加 `genrm` 的版本每步更慢但奖励信号更丰富。若裁判模型太大导致 OOM，改用「外部部署 + 自写 ORM」方案。
5. 若本地无足够显存同时跑 policy + 裁判模型，**待本地验证**：可仅阅读 `GenRMPlugin.__call__` 与 `compute_rewards`，理解「LLM 输出文本如何被还原成浮点奖励」。

#### 4.4.5 小练习与答案

**练习 1**：`GenRMPlugin` 为什么在 `system` prompt 里强约束「结尾用 `Reward: {reward}` 格式」？
**答案**：因为后续 `extract_reward` 用固定正则 `Reward:\s*([0-1]...)` 抽分。没有强约束，LLM 可能把分数写进叙述里（如「我认为可以打 0.8 分」），正则就抽不到。固定锚点是让「自由文本 → 结构化分数」可解析的关键。

**练习 2**：什么场景该选内置 `genrm`，什么场景该选外部部署？
**答案**：裁判模型小（如 1.5B/3B）、不想额外管部署 → 内置 `genrm`，同进程简单。裁判模型大（如 72B）或要追求打分吞吐 → 外部部署（`swift deploy`/`vllm serve` 独立显卡），用 OpenAI 客户端调，避免大模型生成拖慢训练主循环。

**练习 3**：如何让一个**远程大模型**（如 Qwen-Max API）给 GRPO 当裁判？
**答案**：不走 `reward_model`/`reward_model_plugin`，而是自写一个 `ORM`，在 `__call__` 里用 OpenAI 兼容客户端调用远程 API、解析分数，再通过 `--external_plugins` 注册到 `orms`、用 `--reward_funcs` 启用（参考 `ClientPRM` 的实现与官方外部部署模板）。

---

## 5. 综合实践

把本讲三个最小模块串起来：为一个「简短问答」任务设计一套**混合奖励**，并对比不同奖励组合的训练行为。

任务背景：假设你希望模型回答**简洁且礼貌**。请完成以下设计与验证：

1. **写两个自定义 `ORM`**（放进 `my_reward.py`，通过 `--external_plugins` 加载）：
   - `BrevityReward`：回答词数 ≤ 30 给 1，否则按 30/词数 衰减（练习 4.2 的长度奖励变体）。
   - `PolitenessReward`：回答含「你好/请/谢谢」等关键词给 1，否则 0。
2. **跑三组对照实验**（`--max_steps 30`，其余参数固定）：
   - A 组：`--reward_funcs format brevity_reward politeness_reward`（纯规则）。
   - B 组：在 A 组基础上加一个判别式 `--reward_model`（若无可用的，跳过本组并记录原因）。
   - C 组：把 `--reward_model` 的插件换成 `genrm`（用一个小指令模型当裁判）。
3. **记录与观察**：
   - 用 `--reward_weights` 调整三个规则奖励的权重，观察训练日志里各列 reward 均值的变化。
   - 对比 A/B/C 三组在相同 prompt 上的生成风格（是否真的变简洁/礼貌）。
   - 若 `genrm` 组出现抽分失败 warning，尝试修改 `GenRMPlugin` 的 system prompt（在自己的插件里继承 `GenRMPlugin` 覆写 `self.system`）再跑。
4. **交付物**：一份表格，列出「奖励组合 / 可训练参数 / 每步打分耗时 / 最终回答平均词数 / 是否含礼貌词」，并用自己的话解释哪类奖励对行为改变最直接。

> 若本地无多卡或无合适模型，可降级为「源码阅读型」综合实践：通读 `_prepare_rewards` → `compute_rewards_per_func` → `DefaultRMPlugin`/`GenRMPlugin`，画一张「命令行参数到 advantage」的数据流图，标注每一处 `None→NaN`、`nansum`、`logits[:,0]`、`extract_reward` 发生的位置。

## 6. 本讲小结

- **三类奖励信号，一个契约**：规则函数（`ORM`/`AsyncORM`）、判别式 RM（带 value head）、生成式 RM（LLM-as-judge）实现各异，但都收敛到「对一批 completions 返回 `List[float]`」，由 `compute_rewards_per_func` 统一调度成 `[N, n_funcs]` 张量。
- **数据集列自动流入奖励函数**：`GRPOSample.to_reward_row` + `RowPreprocessor.rows_to_batched` 把 `solution`/`target` 等列铺成 kwargs，这就是奖励函数能拿到标准答案的根因。
- **`orms` / `prms` / `rm_plugins` 三张注册表**：分别管理规则奖励、过程奖励、奖励模型插件；自定义奖励只需把类写进 `orms` 并用 `--external_plugins` 加载。
- **`DefaultRMPlugin` 是判别式 RM 的默认胶水**：取 `.logits[:, 0]` 当 reward；`--reward_model` 不指定插件时默认用它，且 RM 会被追加进 `reward_funcs` 末尾，故 `reward_weights` 顺序为 `[reward_funcs, reward_model]`。
- **`GenRMPlugin` 是生成式 RM 范例**：内部用 `TransformersEngine` 让裁判模型按 `Reward: x` 格式输出分数，正则抽分并层层容错；大模型裁判应改用「外部部署 + 自写 ORM」。
- **`None→NaN` + `nansum` 的容错设计**：奖励函数返回 `None` 不会让训练崩溃，只会在加权求和时被忽略；全 `None` 才告警。

## 7. 下一步学习建议

- **横向**：阅读 [swift/rewards/orm.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/rewards/orm.py) 中尚未细讲的 `CosineReward`、`SoftOverlong`、`ReactORM`，它们分别对应「长度相关奖励」「超长惩罚」「ReAct 工具调用奖励」，是 DAPO、ReAct 等高级配方的关键部件，可结合 `docs/source_en/Instruction/GRPO/AdvancedResearch/DAPO.md` 一起读。
- **纵向（下一讲 u7-l4）**：[多轮 Rollout 与环境交互](u7-l4-multi-turn-rollout-and-env.md) 将讲解 `swift/rollout/` 模块——当奖励来自**外部环境/工具调用**（而不是一次性打分）时，`gym_env`、`RolloutScheduler`、`agent_loop` 如何与本章的奖励体系衔接（提示：`score_completions` 在 `use_gym_env` 时会把 `rollout_infos['total_reward']` 作为额外一列追加进奖励矩阵）。
- **实践延伸**：尝试把一个远程大模型 API 封装成 `ORM`（参考 `ClientPRM` + 外部部署模板），体会「LLM-as-judge 进 GRPO」的完整闭环；再阅读 `GRPOTrainer` 里 `reward_weights` 与 `scale_rewards` 的交互，理解加权奖励如何影响 advantage 归一化。
