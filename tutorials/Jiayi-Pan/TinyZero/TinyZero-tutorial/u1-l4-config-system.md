# 配置系统：Hydra 与 ppo_trainer.yaml

## 1. 本讲目标

上一讲我们跑通了 countdown 任务的训练，但你可能注意到：`train_tiny_zero.sh` 里那一长串 `xxx.yyy.zzz=value` 参数，到底是怎么生效的？它们覆盖了什么默认值？默认值又藏在哪？

本讲学完后，你应该能够：

- 说出 verl 的配置由「**一份 yaml 默认值 + 命令行覆盖**」两部分拼出来的机制；
- 读懂 `ppo_trainer.yaml` 的顶层分组（`data` / `actor_rollout_ref` / `critic` / `reward_model` / `algorithm` / `trainer`）各自管什么；
- 区分三种「等号写法」：**覆盖已有键**（`a.b.c=v`）、**新增不存在的键**（`+a.b.c=v`）、**Shell 变量替换**（`$VAR`，与 Hydra 无关）；
- 看懂 `${...}` 这种变量插值（interpolation）如何让配置自动同步、避免重复填写。

本讲不涉及任何算法逻辑，只讲「配置怎么流动」，是后续所有源码阅读篇（训练主循环、Worker 编排）的前置导航。

## 2. 前置知识

### 2.1 什么是配置（Config）

训练一个大模型需要几百个参数：学习率、批大小、模型路径、最大序列长度、KL 系数、保存频率……如果全写死在代码里，每次改一个就要改源码，极难维护。所以业界普遍做法是：把这些可调参数集中到一个**配置文件**里，代码只负责「读配置、按配置执行」。

TinyZero / verl 用的配置文件格式是 **YAML**（一种缩进表示层级的文本格式，`.yaml` 后缀），配置框架是 **Hydra**（Facebook 开源），它底层的配置解析库叫 **OmegaConf**。你不需要预先了解它们，本讲会从零讲清楚用到的那一点点。

### 2.2 三层缩进就是三层映射

YAML 用缩进表示父子关系。例如：

```yaml
algorithm:        # 顶层 key
  adv_estimator: gae      # algorithm 下面的一个字段
  kl_ctrl:                # algorithm 下面的又一个字段，但它本身还是个字典
    type: fixed
    kl_coef: 0.001
```

这段配置在 Python 里就是一个嵌套字典：

```python
{"algorithm": {"adv_estimator": "gae", "kl_ctrl": {"type": "fixed", "kl_coef": 0.001}}}
```

所以你在命令行写 `algorithm.kl_ctrl.kl_coef=0.001`，本质就是用「点号路径」去定位这个嵌套字典最深处的一个键。这就是 Hydra 的核心直觉：**把命令行的点号路径，翻译成对 yaml 字典某条路径的赋值**。

### 2.3 关键术语速查

| 术语 | 含义 |
|------|------|
| Hydra | 配置框架，负责「加载 yaml + 解析命令行覆盖 + 把最终配置传给函数」 |
| OmegaConf | Hydra 底层库，提供 yaml 读写、变量插值 `${}`、`resolve()` 等 |
| 配置覆盖（override） | 用命令行参数修改 yaml 里已有的默认值 |
| 变量插值（interpolation） | yaml 里用 `${别的键}` 引用另一个键的值，避免重复 |
| `config_name` | Hydra 启动时默认加载的 yaml 文件名（不含后缀） |

## 3. 本讲源码地图

本讲涉及三个文件：

| 文件 | 作用 |
|------|------|
| [verl/trainer/config/ppo_trainer.yaml](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml) | **配置默认值的「单一数据源」**，六大分组的默认参数都在这里 |
| [scripts/train_tiny_zero.sh](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/scripts/train_tiny_zero.sh) | TinyZero 的训练入口脚本，用一串命令行参数覆盖 yaml 默认值 |
| [verl/trainer/main_ppo.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py) | Hydra 的真正入口，`@hydra.main` 装饰器声明加载哪个 yaml、并把配置交给 `main_task` |

## 4. 核心概念与源码讲解

### 4.1 配置入口：Hydra 如何加载 ppo_trainer.yaml

#### 4.1.1 概念说明

你执行 `python3 -m verl.trainer.main_ppo` 时，Python 会运行 `verl/trainer/main_ppo.py` 里的 `main`。这个函数被 Hydra 的 `@hydra.main` 装饰器包住了。装饰器做的事情可以概括成三步：

1. **找默认配置文件**：根据 `config_path` 和 `config_name` 定位一个 yaml 文件，加载成字典；
2. **应用命令行覆盖**：把你写在 `python3 -m ...` 后面的 `a.b.c=value` 参数，逐条覆盖/新增到字典里；
3. **调用你的函数**：把最终拼好的配置字典，作为 `config` 参数传给被装饰的函数。

所以 Hydra 把「拼配置」这件麻烦事从业务代码里彻底剥离了，`main(config)` 拿到的永远是「默认值 + 你所有覆盖」之后的最终结果。

#### 4.1.2 核心流程

```
python3 -m verl.trainer.main_ppo   data.train_batch_size=256  algorithm.kl_ctrl.kl_coef=0.001
            │
            ▼
   @hydra.main(config_path='config', config_name='ppo_trainer')
            │
            ├── 1. 加载 verl/trainer/config/ppo_trainer.yaml → 默认字典
            │
            ├── 2. 命令行覆盖：
            │       data.train_batch_size: 1024 → 256
            │       algorithm.kl_ctrl.kl_coef: 0.001 → 0.001（值相同，但仍是覆盖）
            │
            └── 3. 调用 main(config=最终字典)
                    │
                    ▼
            ray.get(main_task.remote(config))   # 交给 Ray 分布式执行
```

#### 4.1.3 源码精读

入口函数与装饰器声明在 [verl/trainer/main_ppo.py:L97-L103](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L97-L103)，这里中文说明：`config_name='ppo_trainer'` 告诉 Hydra 去加载 `config/ppo_trainer.yaml`，`config_path='config'` 是相对于该 `.py` 文件所在目录的子目录路径；`main(config)` 拿到的就是拼好的最终配置，随后用 `ray.get(main_task.remote(config))` 把它交给 Ray 远端执行。

在 `main_task` 一开始，配置会被「打印 + 强制解析」一遍，见 [verl/trainer/main_ppo.py:L112-L115](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L112-L115)：

- `pprint(OmegaConf.to_container(config, resolve=True))` 把配置转成普通字典并**解析掉所有 `${}` 插值**后打印，方便你在日志里确认实际生效的值；
- `OmegaConf.resolve(config)` 把解析结果真正写回 config 对象，保证后续代码读到的都是最终值，不再有未解析的 `${}` 引用。

> 为什么要在 `main_task` 里（而不是 `main` 里）做 `resolve`？因为 `main_task` 是 `@ray.remote` 标注的，它在 Ray worker 进程里执行；在跨进程序列化之后重新 `resolve` 一次，能避免插值在某些环境下「没跟上」的隐患。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「命令行覆盖确实进了最终配置」。

**操作步骤**：

1. 打开 [verl/trainer/main_ppo.py:L112-L115](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L112-L115)，确认有一行 `pprint(OmegaConf.to_container(config, resolve=True))`——这就是训练启动时会打印完整配置的地方。
2. 在 `train_tiny_zero.sh` 末尾追加一个**故意写错**的覆盖，例如 `data.train_batch_size=999`，然后按上一讲的方式启动训练（哪怕因显存不足跑不起来也没关系，配置打印在第一步就会发生）。
3. 在 `verl_demo.log`（由 `tee verl_demo.log` 产生）里搜索 `train_batch_size`。

**需要观察的现象**：日志里打印的 `train_batch_size` 应该是 `999`，而不是 yaml 默认的 `1024`。

**预期结果**：这验证了「命令行覆盖 > yaml 默认值」的优先级。如果看到的是 `1024`，说明你的覆盖写法或路径有误。

**待本地验证**：实际启动需要 GPU 与模型权重；若无环境，可只做步骤 1 的源码阅读。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `@hydra.main` 的 `config_name='ppo_trainer'` 改成 `config_name='ppo_megatron_trainer'`，会发生什么？
**参考答案**：Hydra 会改为加载同目录下的 `ppo_megatron_trainer.yaml`（该文件确实存在于 `verl/trainer/config/`），默认值集合随之换成 Megatron 后端的那一套。这就是「换 config_name = 换默认配置」。

**练习 2**：`main(config)` 与 `main_task(config)` 哪个是真正干活、组装 Worker 的函数？为什么拆成两个？
**参考答案**：`main_task` 是真正干活的（组装 Worker、启动训练）。`main` 只负责初始化本地 Ray 集群，然后用 `ray.get(main_task.remote(config))` 把活派发出去。这样拆是因为训练要在 Ray 管理的分布式进程里跑，而 Ray 集群的初始化需要在驱动进程里完成。

---

### 4.2 ppo_trainer.yaml 的分组结构与默认值

#### 4.2.1 概念说明

`ppo_trainer.yaml` 是整个 PPO 训练的「默认值大全」。它的每一个**顶层 key**（顶格、不缩进的 key）就是一个配置分组，对应训练里的一个大模块。读懂这六个分组，你就建立了「训练有哪些可调旋钮」的全局地图。

需要特别说明：本讲的标题和主题里常说「五大分组」，但仓库里 `ppo_trainer.yaml` 实际有**六个**顶层分组——多出来的是 `reward_model`。不过在 TinyZero 里它默认关闭（`enable: False`），因为 TinyZero 用的是规则奖励（rule-based reward，见下一单元 u2-l4），所以日常你能感知到的主要是五个。下表六个都列出，避免你阅读源码时困惑。

#### 4.2.2 核心流程：六大分组一览

| 分组 | 行范围 | 管什么 | TinyZero 里的典型取值 |
|------|--------|--------|----------------------|
| `data` | L1–L11 | 数据路径、批大小、prompt/响应最大长度 | `train_batch_size=256`、`max_response_length=1024`（脚本覆盖） |
| `actor_rollout_ref` | L13–L84 | Actor + Rollout + Ref 三合一混合引擎（复用同一个模型） | 模型路径、采样温度、PPO 批大小 |
| `critic` | L86–L119 | 价值网络（value head）的模型与优化器 | `lr=1e-5`、`cliprange_value=0.5` |
| `reward_model` | L121–L136 | 神经网络奖励模型（model-based RM） | **`enable: False`**（TinyZero 不用） |
| `algorithm` | L138–L145 | RL 算法核心超参：折扣因子、优势估计器、KL 控制 | `adv_estimator=gae`、`kl_coef=0.001` |
| `trainer` | L147–L160 | 训练循环控制：epoch、日志、checkpoint、GPU 数 | `total_epochs=15`（脚本覆盖默认 30） |

#### 4.2.3 源码精读

**`data` 分组**——数据与序列长度，见 [ppo_trainer.yaml:L1-L11](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L1-L11)。中文说明：默认 `train_files` 指向 gsm8k（这是上游 veRL 的默认值），TinyZero 训练时会被 `train_tiny_zero.sh` 覆盖成自己的 countdown parquet；`max_prompt_length` / `max_response_length` 决定序列多长会被截断。

**`actor_rollout_ref` 分组**——本文件最庞大的分组，因为它把 Actor（策略，要训练）、Rollout（生成，用 vLLM 推理）、Ref（参考策略，冻结不训练）三种角色塞进同一个模型，见 [ppo_trainer.yaml:L13-L84](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L13-L84)。中文说明：里面又分 `model`（基座路径、是否梯度检查点）、`actor`（学习率 `1e-6`、clip 比率 `0.2`、KL loss 开关）、`ref`（冻结参考策略的微批大小）、`rollout`（vLLM 生成参数：温度、top_p、张量并行、采样次数 `n`）。其中 `rollout.n` 在 [L84](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L84) 默认为 `1`，注释明确写着 `# > 1 for grpo`——这是 GRPO 算法的关键开关（每个 prompt 采样多次做组内归一化，详见 u5-l5）。

**`critic` 分组**——价值网络，见 [ppo_trainer.yaml:L86-L119](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L86-L119)。中文说明：critic 学习率 `1e-5`（比 actor 的 `1e-6` 大十倍，因为 value head 是从头训的），`cliprange_value=0.5` 用于价值损失的裁剪以稳定训练。注意 `adv_estimator=gae` 时才需要 critic；如果用 GRPO（`adv_estimator=grpo`），则不需要 critic（这点会在 u4-l2 详解）。

**`algorithm` 分组**——RL 算法核心，见 [ppo_trainer.yaml:L138-L145](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L138-L145)。中文说明：这是本讲最重要的分组之一：
- `adv_estimator: gae`（[L141](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L141)）选择**优势函数估计器**，可选 `gae`（需 critic）或 `grpo`（组内归一化，无需 critic）；
- `kl_ctrl`（[L143-L145](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L143-L145)）本身是个**嵌套字典**，含 `type: fixed`（固定 KL 系数）和 `kl_coef: 0.001`（KL 惩罚系数，即上一讲提到的「缰绳」）。这也解释了为什么 `train_tiny_zero.sh` 里写的是 `algorithm.kl_ctrl.kl_coef=0.001` 这种三层点号路径——因为它要定位到嵌套字典的最深处。

**`trainer` 分组**——训练循环控制，见 [ppo_trainer.yaml:L147-L160](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L147-L160)。中文说明：默认 `total_epochs=30`（TinyZero 脚本覆盖为 `15`）、`logger: ['console', 'wandb']`（同时打印到控制台和写 wandb）、`n_gpus_per_node=8`（默认 8 卡，TinyZero 按机器覆盖为 1 或 2）、`save_freq`/`test_freq` 控制 checkpoint 与评测频率。

#### 4.2.4 代码实践

**实践目标**：定位本讲要求理解的三个配置项，并解释它们控制的行为。

**操作步骤**：

1. 打开 [ppo_trainer.yaml](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml)，按 `Ctrl+F` 搜索：
   - `actor_rollout_ref.rollout.n` → 跳到 [L84](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L84)，默认 `n: 1`。
   - `algorithm.adv_estimator` → 跳到 [L141](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L141)，默认 `gae`。
   - `algorithm.kl_ctrl` → 跳到 [L143-L145](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L143-L145)，是 `type: fixed` + `kl_coef: 0.001` 的嵌套字典。
2. 在你的笔记里为每一项写一句话说明它控制什么（见下方「预期结果」）。

**需要观察的现象**：注意这三个项**都没有**被 `train_tiny_zero.sh` 覆盖，也就是说 TinyZero 在这三处完全沿用 yaml 默认值。

**预期结果**：
- `actor_rollout_ref.rollout.n`：每个 prompt 采样多少条回答。`n=1` 是 PPO 的常规设置；改成 `>1` 是 GRPO 的入口（每个 prompt 采样多条，做组内归一化）。
- `algorithm.adv_estimator`：优势函数怎么估计。`gae` 用 critic 估计；`grpo` 用组内奖励均值/方差归一化、不需要 critic。
- `algorithm.kl_ctrl`：KL 惩罚怎么控制。`type=fixed` 表示用固定系数 `kl_coef=0.001`（另一种 `adaptive` 会按当前 KL 与目标的偏差自动调系数，见 u5-l4）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `actor_rollout_ref` 这个名字里有 `actor`、`rollout`、`ref` 三个词？
**参考答案**：因为这个分组用一个**混合引擎（HybridEngine）**把三种角色复用到同一个模型上：Actor（策略，要训练）、Rollout（用 vLLM 生成回答）、Ref（参考策略，冻结）。分组名就是它管理的三个角色。`hybrid_engine: True`（[L14](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L14)）就是这个机制的开关，详见 u6-l1。

**练习 2**：`reward_model.enable` 默认是 `False`，那 TinyZero 还怎么给回答打分？
**参考答案**：靠**规则奖励函数（rule-based reward）**。TinyZero 不用神经网络 RM，而是用 `verl/utils/reward_score/` 下的 `countdown.py`、`multiply.py` 等函数，按规则直接判断答案对错给分。这条规则奖励路径在 `main_ppo.py` 的 `RewardManager` 里（见 u4-l4）。

---

### 4.3 变量插值：用 `${...}` 让配置自动同步

#### 4.3.1 概念说明

配置里常有「重复值」：比如 `ref` 的微批大小想跟 `actor` 保持一致、`rollout` 的 prompt 长度想等于 `data.max_prompt_length`。如果每个地方都写死一份，改一处忘改另一处就会出 bug。

OmegaConf 提供了**变量插值**：在 yaml 里写 `${路径}`，它会在解析时自动替换成那个路径指向的值。这样「一改全改」，单一数据源。

#### 4.3.2 核心流程

```
yaml 里写：  ref.log_prob_use_dynamic_bsz: ${actor_rollout_ref.actor.use_dynamic_bsz}
                                    │
            OmegaConf.resolve(config) 执行时
                                    ▼
            查找 actor_rollout_ref.actor.use_dynamic_bsz 的当前值
                                    │
                                    ▼
            替换进去，得到最终值（覆盖后也会反映覆盖值）
```

关键性质：**插值是在所有命令行覆盖之后才解析的**，所以它引用的是「覆盖后的最终值」。这也是 `main_task` 里要调用 `OmegaConf.resolve(config)` 的原因——强制把所有 `${}` 算成具体值。

#### 4.3.3 源码精读

`ppo_trainer.yaml` 里有大量插值，最典型的几处：

- `ref` 分组复用 `actor` 的设置，见 [ppo_trainer.yaml:L58-L60](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L58-L60)。中文说明：`ref` 的 `log_prob_use_dynamic_bsz`、`log_prob_max_token_len_per_gpu`、`ulysses_sequence_parallel_size` 全部用 `${actor_rollout_ref.actor.xxx}` 引用 actor 的对应字段，保证参考策略与策略用相同的动态批/序列并行设置，你只需在 `actor` 改一处。
- `rollout` 的序列长度来自 `data`，见 [ppo_trainer.yaml:L66-L67](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L66-L67)。中文说明：`rollout.prompt_length: ${data.max_prompt_length}`、`rollout.response_length: ${data.max_response_length}`——所以你在命令行改 `data.max_response_length=1024`，vLLM 生成的最大长度会**自动跟着变**，无需再单独配 rollout。
- `critic` 复用 `actor` 的批参数，见 [ppo_trainer.yaml:L109-L117](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L109-L117)。中文说明：`critic.ppo_mini_batch_size`、`ppo_epochs`、`shuffle`、`use_dynamic_bsz` 都引用了 `${actor_rollout_ref.actor.xxx}`，保证 critic 与 actor 的训练循环节奏一致。
- `trainer` 内部也用了跨字段插值，见 [ppo_trainer.yaml:L158](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L158)。中文说明：`default_hdfs_dir` 里嵌了 `${trainer.experiment_name}`，所以改实验名时保存路径会自动拼接更新。

解析动作在 [main_ppo.py:L114-L115](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L114-L115)，其中 `resolve=True` / `OmegaConf.resolve(config)` 就是把这些 `${}` 真正求值的地方。

#### 4.3.4 代码实践

**实践目标**：跟踪一条插值链，确认「改一处、多处自动变」。

**操作步骤**：

1. 在 [ppo_trainer.yaml:L79](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L79) 找到 `rollout.log_prob_use_dynamic_bsz: ${actor_rollout_ref.actor.use_dynamic_bsz}`。
2. 在 [ppo_trainer.yaml:L25](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L25) 确认 `actor_rollout_ref.actor.use_dynamic_bsz` 默认是 `False`。
3. 在 [train_tiny_zero.sh:L10](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/scripts/train_tiny_zero.sh#L10) 看到 `actor_rollout_ref.actor.use_dynamic_bsz=True` 把它覆盖成 `True`。

**需要观察的现象**：因为插值是「覆盖后解析」，最终 `rollout.log_prob_use_dynamic_bsz` 和 `ref.log_prob_use_dynamic_bsz` 都会变成 `True`，哪怕脚本里并没有显式写这两条。

**预期结果**：你在 `verl_demo.log` 打印的最终配置里，应该看到三处 `use_dynamic_bsz` 全是 `True`。这验证了插值会**传播覆盖值**。

**待本地验证**：需要实际启动训练查看打印日志；若无环境，按上述源码链路推演即可。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `train_tiny_zero.sh` 里的 `actor_rollout_ref.actor.use_dynamic_bsz=True` 删掉，`ref` 和 `rollout` 的 `use_dynamic_bsz` 会变成什么？
**参考答案**：变成 `False`。因为它们通过 `${...}` 引用 `actor` 的值，而 `actor` 不被覆盖时取 yaml 默认 `False`（[L25](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L25)），插值随之解析为 `False`。

**练习 2**：插值 `${a.b}` 在什么时候被求值？为什么需要 `OmegaConf.resolve(config)`？
**参考答案**：默认是「惰性」的，读的时候才算；`OmegaConf.resolve(config)` 会**立即**把整棵配置树里所有插值一次性求值并固化下来。训练代码里会反复读 config，提前 resolve 能避免「读到一半值变了」或「跨进程序列化后插值失效」等问题。

---

### 4.4 命令行覆盖：覆盖、新增与 Shell 变量

#### 4.4.1 概念说明

这是本讲最容易踩坑、也最实用的一节。`train_tiny_zero.sh` 里那些 `key=value` 看起来一样，其实有**三种完全不同的语义**，务必区分：

| 写法 | 例子 | 含义 |
|------|------|------|
| `a.b.c=value` | `trainer.total_epochs=15` | **覆盖** yaml 里**已存在**的键。若键不存在，Hydra 会**报错** |
| `+a.b.c=value` | `+trainer.val_before_train=False` | **新增**一个 yaml 里**不存在**的键 |
| `~a.b.c` | （本脚本未用） | **删除**一个键 |
| `$VAR` | `data.train_files=$DATA_DIR/train.parquet` | **Shell 变量替换**，与 Hydra 完全无关，由 bash 在 Hydra 之前完成 |

最后一种尤其要分清：`$DATA_DIR`、`$BASE_MODEL` 这些是 **bash 变量**，bash 会先把它们替换成具体字符串，Hydra 看到的已经是替换后的字面值。它们**不是** Hydra 的配置路径。

#### 4.4.2 核心流程

一条 `train_tiny_zero.sh` 命令的完整生效过程：

```
bash ./scripts/train_tiny_zero.sh
        │
        │  ① bash 先做 Shell 变量替换
        ▼
$DATA_DIR → /home/you/data/countdown
$BASE_MODEL → Qwen/Qwen2.5-3B
$N_GPUS    → 2
$ROLLOUT_TP_SIZE → 2
        │
        │  ② 替换后的命令交给 Hydra
        ▼
python3 -m verl.trainer.main_ppo \
    data.train_files=/home/you/data/countdown/train.parquet \
    ...
    trainer.total_epochs=15 \        ← 覆盖已有键（默认 30）
    +trainer.val_before_train=False  ← 新增键（yaml 里没有）
        │
        │  ③ Hydra 加载 ppo_trainer.yaml + 应用覆盖/新增
        ▼
        最终 config
```

#### 4.4.3 源码精读

`train_tiny_zero.sh` 整个文件就是一条 `python3 -m verl.trainer.main_ppo` 命令加一堆覆盖，见 [scripts/train_tiny_zero.sh:L1-L31](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/scripts/train_tiny_zero.sh#L1-L31)。中文说明：脚本本身**不含任何训练逻辑**，只是一个「配置覆盖清单」。注意它引用了 `$DATA_DIR`、`$BASE_MODEL`、`$ROLLOUT_TP_SIZE`、`$N_GPUS`、`$EXPERIMENT_NAME` 五个 Shell 变量——它们**不在脚本里定义**，需要你在运行前 `export`。

这些 Shell 变量的来源在 README，单卡示例见 [README.md:L60-L67](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/README.md#L60-L67)，中文说明：运行脚本前先 `export N_GPUS=1`、`export BASE_MODEL=...`、`export DATA_DIR=...`、`export ROLLOUT_TP_SIZE=1`、`export EXPERIMENT_NAME=...`，再 `bash ./scripts/train_tiny_zero.sh`。3B 模型同理但 `N_GPUS=2`、`ROLLOUT_TP_SIZE=2`（见 [README.md:L73-L80](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/README.md#L73-L80)）。

下面把脚本里的三类写法各挑一例对照：

- **覆盖已有键**（最常见）：[train_tiny_zero.sh:L31](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/scripts/train_tiny_zero.sh#L31) `trainer.total_epochs=15`，覆盖了 yaml 默认的 `30`（[L148](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L148)）；同理 [L4](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/scripts/train_tiny_zero.sh#L4) 的 `data.train_batch_size=256` 覆盖了默认 `1024`（[L8](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L8)）。
- **新增键**（带 `+`）：[train_tiny_zero.sh:L23](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/scripts/train_tiny_zero.sh#L23) `+trainer.val_before_train=False`。在 [trainer 分组 L147-L160](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L147-L160) 里你**找不到** `val_before_train`，所以必须用 `+`；如果直接写 `trainer.val_before_train=False`，Hydra 会因「键不存在」而报错。
- **Shell 变量替换**（与 Hydra 无关）：[train_tiny_zero.sh:L8](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/scripts/train_tiny_zero.sh#L8) `actor_rollout_ref.model.path=$BASE_MODEL`、[L25](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/scripts/train_tiny_zero.sh#L25) `trainer.n_gpus_per_node=$N_GPUS`，bash 先把 `$BASE_MODEL`/`$N_GPUS` 替换成字符串，Hydra 拿到的是普通覆盖。
- **嵌套字典的深层覆盖**：[train_tiny_zero.sh:L21](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/scripts/train_tiny_zero.sh#L21) `algorithm.kl_ctrl.kl_coef=0.001`，三层点号直达 [L145](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L145) 的 `kl_coef`。

> 小贴士：列表值的覆盖要用方括号，例如 [train_tiny_zero.sh:L22](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/scripts/train_tiny_zero.sh#L22) `trainer.logger=['wandb']` 把默认的 `['console', 'wandb']` 改成只剩 wandb。

#### 4.4.4 代码实践

**实践目标**：从脚本里找出本讲要求的「对 yaml 默认值的覆盖示例」，并区分三种写法。

**操作步骤**：

1. 打开 [scripts/train_tiny_zero.sh](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/scripts/train_tiny_zero.sh)，逐行判断每条 `key=value` 属于哪一类（覆盖 / 新增 `+` / Shell 变量）。
2. 重点验证 [L23](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/scripts/train_tiny_zero.sh#L23) 的 `+trainer.val_before_train=False`：去 [trainer 分组](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L147-L160) 确认确实没有 `val_before_train` 键，所以非用 `+` 不可。
3. 选一条覆盖项（如 [L7](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/scripts/train_tiny_zero.sh#L7) `data.max_response_length=1024`），去 yaml 找到它覆盖前的默认值（应为 `512`，见 [L7](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L7)）。

**需要观察的现象**：每条覆盖项都能在 yaml 里找到「被覆盖的默认值」；唯一找不到的是带 `+` 的那条。

**预期结果**（覆盖示例各一例）：
- 覆盖：`trainer.total_epochs=15`（默认 `30`）、`data.train_batch_size=256`（默认 `1024`）、`data.max_response_length=1024`（默认 `512`）。
- 新增：`+trainer.val_before_train=False`（yaml 无此键）。
- Shell 变量：`data.train_files=$DATA_DIR/train.parquet`、`actor_rollout_ref.model.path=$BASE_MODEL`。

#### 4.4.5 小练习与答案

**练习 1**：把 `+trainer.val_before_train=False` 的 `+` 去掉，直接写 `trainer.val_before_train=False`，会发生什么？
**参考答案**：Hydra 启动时会**报错**，提示 `val_before_train` 这个键在配置里不存在。Hydra 默认禁止「隐式新增」键，正是为了防止你把键名拼错却悄悄生效。`+` 是你显式声明「我知道这是新键」。

**练习 2**：`data.train_files=$DATA_DIR/train.parquet` 这条里，`$DATA_DIR` 是 Hydra 的配置路径吗？
**参考答案**：不是。`$DATA_DIR` 是 **bash 的 Shell 变量**，由 bash 在把命令交给 Hydra 之前就替换成了具体路径字符串。Hydra 收到的已经是类似 `data.train_files=/home/you/data/countdown/train.parquet` 的普通覆盖。区分这一点能避免你误以为 Hydra 有个叫 `DATA_DIR` 的配置项。

**练习 3**：如果你想关掉默认开启的 `actor_rollout_ref.hybrid_engine`（[L14](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L14)），该在脚本里怎么写？
**参考答案**：`actor_rollout_ref.hybrid_engine=False`（普通覆盖，因为它在 yaml 里已存在）。注意：TinyZero 依赖混合引擎，关掉会导致 Worker 结构不匹配，这里只是练习语法。

---

## 5. 综合实践

把本讲全部知识串起来，完成下面这个「配置考古」任务。

**任务背景**：假设你要把一个 TinyZero 训练从「countdown + GAE（带 critic）」改成「multiply + GRPO（不带 critic）」的实验，请基于本讲的配置知识，规划需要改动哪些配置项，并分别给出 yaml 默认值和你打算设置的值。

**操作步骤**：

1. **数据与任务路由**：
   - 在脚本里把 `data.train_files` / `data.val_files` 指向 multiply 的 parquet（参考上一讲 u1-l3 的数据预处理）。
   - 回顾 `main_ppo.py` 的 [\_select_rm_score_fn](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L24-L34)：multiply 的 `data_source` 含 `"multiply"` 即可命中 `multiply.compute_score`，所以保证 parquet 里 `data_source` 字段正确即可，无需改源码。

2. **切换到 GRPO**：参考 [rollout.n 的注释](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L84)（`# > 1 for grpo`）和 [actor.use_kl_loss 注释](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L30)（`# True for GRPO`），在脚本里新增/覆盖：
   - `algorithm.adv_estimator=grpo`（覆盖默认 `gae`，见 [L141](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L141)）
   - `actor_rollout_ref.rollout.n=8`（覆盖默认 `1`，见 [L84](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L84)）——每个 prompt 采样 8 条做组内归一化
   - `actor_rollout_ref.actor.use_kl_loss=True`（覆盖默认 `False`，见 [L30](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L30)）

3. **填写对照表**（答案示例）：

   | 配置项（点号路径） | yaml 默认值 | 你要设置的值 | 写法 |
   |------|------|------|------|
   | `data.train_files` | `~/data/rlhf/gsm8k/train.parquet` | `$DATA_DIR/train.parquet`（multiply） | Shell 变量 + 覆盖 |
   | `algorithm.adv_estimator` | `gae` | `grpo` | 覆盖 |
   | `actor_rollout_ref.rollout.n` | `1` | `8` | 覆盖 |
   | `actor_rollout_ref.actor.use_kl_loss` | `False` | `True` | 覆盖 |
   | `+trainer.val_before_train` | （不存在） | `False` | 新增 `+` |

4. **验证理解**：解释为什么切换到 GRPO 后可以不配 critic（提示：`adv_estimator=grpo` 时 `init_workers` 不创建 critic——这点会在 u4-l2 详解，本讲只需建立「adv_estimator 决定是否需要 critic」的初步印象）。

**预期结果**：你能独立写出一份针对 GRPO 的 `train_tiny_zero.sh` 覆盖清单，并清楚每条是覆盖、新增还是 Shell 变量，以及它改的是 yaml 里哪个默认值。完整 GRPO 的参数细节会在 u5-l5 专门讲解，本综合实践只做配置层的「换开关」练习。

## 6. 本讲小结

- verl 的配置 = **一份 yaml 默认值（`ppo_trainer.yaml`）+ 命令行覆盖**，由 Hydra 在 [main_ppo.py:L97](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L97) 的 `@hydra.main` 处拼合，最终在 [L114-L115](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L114-L115) `resolve` 后交给训练逻辑。
- `ppo_trainer.yaml` 有六个顶层分组：`data`（数据/序列长度）、`actor_rollout_ref`（策略+生成+参考三合一）、`critic`（价值网络）、`reward_model`（神经网络 RM，TinyZero 默认关）、`algorithm`（RL 算法核心）、`trainer`（训练循环控制）。
- 三个关键配置项：`rollout.n`（每 prompt 采样数，GRPO 入口）、`adv_estimator`（gae / grpo，决定是否需要 critic）、`kl_ctrl`（嵌套字典，控制 KL 惩罚系数）。
- 命令行三种写法要分清：`a.b.c=v` 覆盖已有键、`+a.b.c=v` 新增键、`$VAR` 是 bash 变量替换（与 Hydra 无关）。
- `${...}` 变量插值让配置「改一处、多处自动同步」，且在覆盖之后才解析，所以会传播命令行覆盖的值。

## 7. 下一步学习建议

本讲你掌握了「配置怎么流动」，但这套配置最终驱动的是一个庞大的分布式训练系统。接下来的学习路线建议：

- **进入 u2（数据与任务定义）**：先看 `data` 分组背后的数据到底长什么样——精读 `countdown.py` 的数据生成与 prompt 模板（u2-l1），以及规则奖励函数 `compute_score`（u2-l4），理解 `data_source` 字段如何驱动 `main_ppo._select_rm_score_fn` 的奖励路由。
- **进入 u3（数据协议与单控制器）**：理解 `actor_rollout_ref` 分组里「三角色合一」对应的 `ActorRolloutRefWorker`，以及 Ray single-controller 如何按 `trainer.n_gpus_per_node` 调度 GPU。
- **进入 u4（PPO 训练主流程）**：顺着 [main_ppo.py:main_task](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/main_ppo.py#L106-L189) 这条真实调用链，看配置如何变成 `RayPPOTrainer` 并跑起 `fit()` 主循环；届时你会真正理解 `algorithm` 分组每个参数的作用。

建议在进入下一讲前，先把本讲的「综合实践」对照表填一遍——能独立写出一份配置覆盖清单，才算真正读懂了这套配置系统。
