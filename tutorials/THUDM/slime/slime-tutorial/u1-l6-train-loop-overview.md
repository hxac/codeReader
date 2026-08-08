# 训练主循环 train.py 全景

## 1. 本讲目标

本讲是 slime 入门篇的收尾，目标是让你站在「指挥官」的视角，看清 slime 把一次完整的强化学习（RL）训练**跑成一个循环**的全过程。

学完本讲，你应该能够：

1. 逐行读懂根目录下 [train.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py) 的主循环，并能把每一行归类到「采样 / 训练 / 保存 / 同步 / 评估」五个阶段之一。
2. 看懂 [train_async.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train_async.py) 与 train.py 的关键区别：异步版本会**提前发起下一轮 rollout**，从而让采样与训练重叠。
3. 说清 `offload_rollout` / `offload_train` / `colocate` 三者如何协作，让训练和推理在同一组 GPU 上轮流使用显存。
4. 理解 `should_run_periodic_action` 这个工具函数如何决定「什么时候该存档、什么时候该评估」。

本讲只讲**主循环的骨架**，不深入每个阶段内部（采样内部、训练内部、权重同步内部都有后续专门讲义）。你要建立的是一张时序图，而不是每个函数的实现细节。

## 2. 前置知识

在继续之前，请确认你已经理解以下几个概念。它们来自前面的讲义。

- **三大模块与闭环**：slime 由 `rollout`（采样，用 SGLang 推理）、`training`（训练，用 Megatron）、`data buffer`（数据桥梁）三个模块组成。数据从 rollout 流向 training，训练完的权重再**单向**同步回 rollout。详见 u1-l1。
- **入口文件很薄**：[train.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py) 与 [train_async.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train_async.py) 都只是「装配工人 + 跑循环」，真正的逻辑在 `slime/ray/` 编排层和 `slime/backends/` 后端里。详见 u1-l2。
- **Ray 的远程调用**：slime 用 Ray 把 actor / critic / rollout 三类对象分散到不同 GPU 上运行。下面两个 Ray 用法会反复出现，请先记住：
  - `对象.方法名.remote(...)`：**异步**发起一次远程调用，立刻返回一个 `ObjectRef`（可以理解成「取货凭证」「future」），此时调用并不会等结果。
  - `ray.get(ref)`：**阻塞**等待，直到那个「取货凭证」对应的真实结果算出来并返回。
  - 于是「先 `.remote()` 再 `ray.get()`」就是一次同步调用；只 `.remote()` 不 `ray.get()`，就是在后台让它跑着，等会儿再来取。

> 名字提醒：slime 里的方法名叫 `async_train`，但它**不是** Python 的 `async/await` 异步，而是「返回 Ray 的 future、不阻塞」的意思。train.py 拿到这些 future 后，仍会用 `ray.get()` 阻塞等待训练结束。不要被名字误导。

- **参数三族**：slime 合并了 Megatron / SGLang / slime 自己三族参数。本讲涉及的 `--colocate`、`--offload-train`、`--offload-rollout`、`--release-train`、`--num-rollout`、`--eval-interval`、`--save-interval` 等都是 slime 自己的参数。详见 u1-l4。

## 3. 本讲源码地图

本讲只涉及仓库根目录的两个入口脚本，外加一个工具函数：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [train.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py) | 同步训练主循环 | 整个 `train(args)` 函数，重点是 49–91 行的循环 |
| [train_async.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train_async.py) | 异步训练主循环 | 「提前发起下一轮 rollout」与 `update_weights_interval` 逻辑 |
| [slime/utils/misc.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/misc.py) | 通用工具 | `should_run_periodic_action` 函数（107–128 行） |

辅助参考（仅引用方法名，不展开实现）：

- [slime/ray/actor_group.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/actor_group.py)：`RayTrainGroup`，定义 actor/critic 共用的 `async_train` / `update_weights` / `save_model` 等方法。
- [slime/ray/rollout.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py)：`RolloutManager`，定义 `generate` / `eval` / `save` / `offload` / `onload_*` 等方法。
- [slime/utils/arguments.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py)：本讲用到的参数定义都在这里。

## 4. 核心概念与源码讲解

### 4.1 train.py 主循环：采样→训练→保存→同步→评估

#### 4.1.1 概念说明

强化学习后训练和普通的有监督微调（SFT）最大的不同，在于它**自己生成训练数据**：每一轮训练前，都要先用当前模型去「采一批样本、算一遍奖励」，再拿这批带奖励的数据去做一步梯度更新。于是 slime 的训练天然是一个**闭环**：

```
        ┌── ① 采样(generate):用 rollout 引擎生成带 reward 的样本
        │
        │   ② 训练(async_train):Megatron 拿样本做一步更新
        ▼
        │   ③ 保存(save_model):按周期把检查点写盘
        │
        │   ④ 同步(update_weights):把新权重推给 rollout 引擎
        │
        └── ⑤ 评估(eval):按周期算指标，不更新参数
```

[train.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py) 的全部工作，就是把这个闭环**包在一个 `for` 循环里**重复执行 `num_rollout` 次。这里的「rollout」就是一轮闭环的计数单位，你可以把它理解为「一个训练 step」。

#### 4.1.2 核心流程

train.py 的 `train(args)` 可以拆成两大块：

1. **初始化阶段**（循环前，只执行一次）：
   - 分配 GPU（placement group）→ 创建 rollout 管理器 → 创建 actor / critic 模型 → 把初始权重同步给 rollout 引擎。

2. **主循环阶段**（重复 `num_rollout` 次）：每一轮依次走「采样 → 训练 → 保存 → 同步 → 评估」五步，其中「保存」和「评估」是按周期触发的，不是每轮都做。

用伪代码概括：

```text
train(args):
    pgs            = create_placement_groups(args)          # 分配 GPU
    rollout_manager= create_rollout_manager(args, ...)      # 创建采样引擎
    actor, critic  = create_training_models(args, ...)      # 创建训练工人
    actor.update_weights()                                  # 把初始权重推给 rollout
    for rollout_id in [start, num_rollout):
        data = rollout_manager.generate(rollout_id)         # ① 采样
        actor.async_train(rollout_id, data)                 # ② 训练(可能先训 critic)
        if 该存档了: actor.save_model(rollout_id)            # ③ 保存(周期性)
        actor.update_weights()                              # ④ 同步权重
        if 该评估了: rollout_manager.eval(rollout_id)        # ⑤ 评估(周期性)
    rollout_manager.dispose()
```

#### 4.1.3 源码精读

**入口与初始化**。train.py 的 `__main__` 只做两件事：解析参数，调用 `train(args)`。

[train.py:97-99](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py#L97-L99) —— 入口：解析参数后交给 `train`。

初始化阶段先分配 GPU、再建 rollout 管理器、最后建训练模型。注意一个细节：**rollout 管理器必须先建**，因为它会算出 `num_rollout_per_epoch`（每个 epoch 多少轮），训练模型创建时要用到它。

[train.py:14-21](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py#L14-L21) —— 分配 GPU、创建 rollout 管理器（带 SGLang 引擎）、创建 actor / critic 模型。

模型创建好后，第一件事就是**把 actor 的初始权重推一份给 rollout 引擎**，让两者从同一个起点开始：

[train.py:23-33](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py#L23-L33) —— 加载并同步初始权重。第 27 行的 `actor_model.update_weights()` 是循环里每轮也会调用的同一方法，这里只是「首次同步」。

接下来是真正的循环体。我们把 [train.py:49-91](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py#L49-L91) 这一整段按五阶段拆开看：

**① 采样**（第 53 行）。`rollout_manager.generate.remote(rollout_id)` 异步发起采样，外层 `ray.get` 阻塞等待结果。返回的 `rollout_data_ref` 实际是已经算好奖励、切分到各数据并行的训练数据。

[train.py:53](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py#L53) —— 采样：rollout 引擎生成并返回带 reward 的训练数据。

**② 训练**（第 58–69 行）。这里有两个分支。如果用了 critic（PPO 类算法需要价值模型），就**先训 critic、再训 actor**；否则只训 actor。注意 `critic_model.async_train(...)` 返回的 `value_refs`（价值估计结果）会作为 `external_data` 传给 actor 训练——这就是 critic 给 actor 提供优势估计所需信号的衔接点。

第 61 行的 `actor_trains` 判断「这一轮是不是 actor 来训练」：在开头的若干步（`num_critic_only_steps`）里可能只热身训练 critic，之后 actor 才加入。

[train.py:58-69](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py#L58-L69) —— 训练：可选地先训 critic，再训 actor；critic 的输出作为外部数据喂给 actor。

**③ 保存**（第 71–80 行）。存档是**周期性**的，由 `should_run_periodic_action(...)` 决定（4.4 节详解）。`force_sync` 表示是否强制同步写盘：在 `release_train` 模式或最后一轮时为真。如果开了 `rollout_global_dataset`，还会顺便把 rollout 的数据状态也存盘。

[train.py:71-80](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py#L71-L80) —— 保存：周期性地写 actor/critic 检查点，必要时存 rollout 数据。

**④ 同步**（第 85 行）。训练改变了 actor 权重，必须把新权重推回 rollout 引擎，否则下一轮采样用的还是旧模型。这就是闭环里「训练→推理」的单向箭头。

[train.py:82-88](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py#L82-L88) —— 第 82 行清理显存；第 85 行同步新权重到 rollout；中间穿插的 onload 调用属于 colocate 显存管理，4.2 节细讲。

**⑤ 评估**（第 90–91 行）。评估也是**周期性**的，且**只前向、不更新参数**。注意循环开头（第 50–51 行）还有一处评估：如果配置了且没跳过，会在训练开始前先评估一次基线。

[train.py:90-91](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py#L90-L91) —— 评估：周期性地在评估集上算指标。

循环结束后做收尾：销毁 rollout 引擎、结束追踪。

[train.py:93-94](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py#L93-L94) —— 收尾：dispose 销毁引擎，finish_tracking 结束日志追踪。

#### 4.1.4 代码实践

**实践目标**：把 train.py 的每一行归类到五个阶段，亲手把循环结构「摸」一遍。

**操作步骤**：

1. 打开 [train.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py)，准备一张表格，列为「行号 / 代码片段 / 所属阶段」。
2. 逐行阅读 49–91 行，把每一行填入表格，阶段从下列选一：`采样` / `训练` / `保存` / `同步` / `评估` / `显存管理` / `初始化` / `收尾`。
3. 用下面的时序图模板（你可以画在纸上或笔记里），把一轮循环填进去：

```
rollout_id = k:
  ├─[采样]  ray.get(rollout_manager.generate.remote(k))
  ├─[显存]  offload rollout（若开启）
  ├─[训练]  critic.async_train(...)  →  actor.async_train(...)
  ├─[保存]  save_model(k)（若该存档了）
  ├─[显存]  offload_train / onload_weights
  ├─[同步]  actor.update_weights()
  └─[评估]  rollout_manager.eval(k)（若该评估了）
```

**需要观察的现象**：你会发现「显存管理」调用夹在阶段之间（如采样后、同步前），这是因为 colocate 模式下训练和推理轮流用同一批 GPU，详见 4.2 节。

**预期结果**：你能拿这张表向别人解释「slime 每一轮做了什么」，并且能指出哪几行是**每轮必做**（采样/训练/同步），哪几行是**周期触发**（保存/评估）。

> 本实践为源码阅读型实践，不要求真实运行训练，故不涉及 GPU。若你想本地验证，可参考后续 u2 单元的 Ray 编排讲义。

#### 4.1.5 小练习与答案

**练习 1**：如果只想让模型评估、不训练，train.py 走的是哪条路径？

> **答案**：走 [train.py:36-37](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py#L36-L37) 的 eval-only 特例：当 `num_rollout == 0` 且设了 `eval_interval` 时，直接调一次 `rollout_manager.eval.remote(rollout_id=0)`，不进入下面的训练循环。

**练习 2**：循环里 `actor_model.async_train(...)` 返回的是多个 Ray ref，但紧接着就被 `ray.get` 包住。既然最后都要等，为什么不直接叫 `train` 而叫 `async_train`？

> **答案**：「async」指**返回 future 不阻塞**，而非 Python asyncio。这种设计允许把 critic 的 `value_refs` 作为 `external_data` 传给 actor 的训练，从而让 actor 训练**等待并消费** critic 的产物；如果 critic 训练是纯阻塞的，actor 就无法把「等 critic 结果」表达成一个依赖。参见 [actor_group.py:130-148](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/actor_group.py#L130-L148)。

### 4.2 colocate 与 offload：训练推理共卡的显存协作

#### 4.2.1 概念说明

GPU 很贵。slime 提供了一种省钱的方式：让训练（Megatron）和推理（SGLang）**共用同一组 GPU**，这就是 `--colocate`（共置）模式。但一张卡的显存装不下「训练模型 + 推理引擎」同时驻留，于是 slime 的办法是**轮流使用**：

- 采样阶段：推理引擎上卡，训练模型**让出显存**（offload 到 CPU）。
- 训练阶段：训练模型上卡，推理引擎**让出显存**。

这种「来回搬」由两组开关控制：

- `--offload-rollout`：在不采样时，把 rollout 引擎从 GPU 卸到 CPU。
- `--offload-train`：在不训练时，把训练模型从 GPU 卸到 CPU。

> 还有一个更激进的开关 `--release-train`：它不是「搬到 CPU」，而是**直接杀掉**训练进程，下一轮训练前再根据存档**重建**。这能省下更多显存，代价是重建开销，且必须配合 disk 权重同步。

#### 4.2.2 核心流程

colocate 时，参数校验层会把 offload 默认打开。关键规则在 [arguments.py:1875-1886](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1875-L1886)：colocate 且未显式指定时，`offload_train` 和 `offload_rollout` 都默认置为 `True`。

于是 colocate 模式下，一轮循环的显存流转大致是：

```text
采样前:  onload_weights + onload_kv  → 推理引擎上卡
采样中:  generate（推理占满显存，训练模型已在 CPU）
采样后:  offload                    → 推理引擎下卡，腾出显存
训练中:  async_train                → 训练模型上卡占满显存
训练后:  clear_memory / onload      → 训练让出，准备下一轮推理
同步前:  onload_weights             → 推理引擎重新加载权重
```

不开启 colocate（训练卡和推理卡是分开的物理卡）时，`offload_*` 默认都是 `False`，上面这些 onload/offload 调用基本是空操作，两边的显存互不干扰。

#### 4.2.3 源码精读

回到 train.py 循环体，那些不属于五阶段、看起来「碍事」的调用，就是 colocate 显存管理。

采样结束后立即把 rollout 卸下卡，给训练腾地方：

[train.py:55-56](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py#L55-L56) —— 采样后若开启了 offload_rollout，就把 rollout 引擎卸到 CPU。

`offload_train` 是个局部小帮手，作用是「在非 colocate、非自动卸载的情况下，手动清掉这一步没参与训练的那个模型的显存」。colocate + offload_train 时模型会**自动**在训练后卸载，所以这里只处理不自动卸载的情况：

[train.py:39-46](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py#L39-L46) —— `offload_train` 帮手：按本轮是 actor 还是 critic 训练，清理另一个模型的显存。

同步权重之前，要先把 rollout 引擎的**权重**重新加载上卡（onload_weights），更新完权重后还要把 **KV cache** 相关状态也加载回来（onload_kv）：

[train.py:83-88](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py#L83-L88) —— 同步阶段前后的 onload 调用：先加载权重到 rollout，更新权重，再加载 KV。

`release_train` 模式更特殊：训练工人是**临时的**，每轮训练前要 `create()`（重建），训练后会随 `update_weights` 自动释放。所以第 58–59 行有 `if release_train: actor_model.create()`。

> 这一小节只是让你「认得」这些调用，知道它们在干什么。offload/release 的内部实现（如何搬运、如何重建）属于进阶内容，本讲不展开。

#### 4.2.4 代码实践

**实践目标**：理解 colocate 开关如何改变 offload 默认值，并据此预测循环中哪些调用会真正生效。

**操作步骤**：

1. 阅读 [arguments.py:1873-1895](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1873-L1895) 的 colocate 校验逻辑。
2. 在笔记里画一张「参数 → 默认值」的表，针对三种场景填写 `offload_train` / `offload_rollout` 的最终取值：
   - 场景 A：`--colocate`，不显式指定 offload。
   - 场景 B：`--colocate --release-train`。
   - 场景 C：不 colocate，不显式指定 offload。
3. 对照 [train.py:49-91](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py#L49-L91) 的循环，在场景 C（不 colocate）下划出「实际什么都不会做」的行。

**需要观察的现象**：在场景 C 里，所有 `if args.offload_rollout:` 包起来的行都不会执行，循环会变得很干净——只剩采样、训练、保存、同步、评估。

**预期结果**：

| 场景 | offload_train | offload_rollout |
| --- | --- | --- |
| A：colocate | True | True |
| B：colocate + release_train | False（被 release 代替） | True |
| C：不 colocate | False | False |

（依据 arguments.py 第 1875–1895 行；其中 B 场景由 1876–1882 行强制设置。）

#### 4.2.5 小练习与答案

**练习 1**：为什么 `--release-train` 模式下 `offload_train` 会被强制设为 `False`？

> **答案**：release_train 是**直接杀掉训练进程**并在下一轮重建，比「搬到 CPU」更彻底。既然进程都不在了，就没必要再做 CPU 卸载。见 [arguments.py:1876-1879](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1876-L1879)。

**练习 2**：同步权重之前为什么要先 `onload_weights`、之后还要 `onload_kv`？这两者加载的是不是同一种东西？

> **答案**：不是。`onload_weights` 把**模型权重**重新放到推理引擎上卡（因为之前采样后 offload 了），以便接收更新后的权重；`onload_kv` 加载的是 **KV cache / 推理运行时状态**相关的部分，用于恢复引擎的推理能力。两者分别对应「权重」和「推理缓存」两类显存。

### 4.3 train_async.py 异步循环：提前发起下一轮 rollout

#### 4.3.1 概念说明

仔细看 4.1 的时序图你会发现一个浪费：**采样和训练是串行的**。第 k 轮训练时，GPU（如果是分开的卡）或推理引擎（如果空闲）其实可以在后台**提前采第 k+1 轮的数据**。

train_async.py 的核心思想就是这一点——**预取（prefetch）下一轮 rollout**：当 actor 还在训练第 k 轮时，rollout 引擎已经在生成第 k+1 轮的样本。这样采样时间和训练时间重叠，吞吐更高。

代价是：colocate（共卡）模式不支持，因为共卡时采样和训练本来就**抢同一块显存**，无法并行。所以 train_async.py 开头第一行就断言：

[train_async.py:11](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train_async.py#L11) —— 断言：异步训练不支持 colocate。

#### 4.3.2 核心流程

异步循环用「两个变量交替」来实现预取，像两条传送带：

```text
rollout_data_next_future = generate.remote(start_id)   # 进循环前：先采第 0 轮
for rollout_id in [start, num_rollout):
    curr_ref = ray.get(next_future)          # 取回「上一轮发起的」采样结果，作为本轮数据
    next_future = generate.remote(id + 1)    # 立刻发起「下一轮」的采样（后台跑）
    async_train(id, curr_ref)                # 用本轮数据训练（此时下一轮正在后台采）
    ...
    update_weights(id)                       # 同步权重（注意：不是每轮都同步！）
```

另一个重要区别：**同步权重不是每轮都做**，而是按 `--update-weights-interval` 周期性进行。原因是异步预取让「正在采样的轮次」和「刚训练完的轮次」错开了，如果每次训练后立刻同步权重，就可能**在采样进行到一半时换权重**，导致一批样本里前后用的不是同一个模型。所以更新前要先 `ray.get` 把正在后台跑的那批采样**收口**。

#### 4.3.3 源码精读

循环开始前，先发起第一轮采样，拿到它的 future：

[train_async.py:32](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train_async.py#L32) —— 进循环前先采第一轮，得到 `rollout_data_next_future`。

循环里第一件事是把「之前发起的」采样结果收回来，变成「本轮」要用的 `rollout_data_curr_ref`：

[train_async.py:35-36](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train_async.py#L35-L36) —— 收回上一轮发起的采样结果，作为本轮训练数据。

紧接着，如果还有下一轮，**立刻发起下一轮的采样**，不等本轮训练结束：

[train_async.py:38-40](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train_async.py#L38-L40) —— 提前发起下一轮 rollout，让它在本轮训练期间在后台生成。

训练部分和 train.py 几乎一样（critic/actor 分支），此处略。

最关键的区别在权重同步。它受 `update_weights_interval` 控制，且同步前必须先把后台正在跑的采样**收口**，避免「采到一半换权重」：

[train_async.py:66-70](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train_async.py#L66-L70) —— 周期性同步权重：先 `ray.get` 收口后台采样，再 update_weights。

注意第 66 行的条件：`release_train or (rollout_id + 1) % args.update_weights_interval == 0`。`update_weights_interval` 默认是 1（每轮都同步），所以默认行为和 train.py 一样；只有把它调大时才会出现「训练多轮、同步一次」。

同步后还要把 `rollout_data_next_future` 置为 `None`（第 69 行），表示这一轮的采样已经被收口了，下一轮循环开头不会重复 `ray.get`。

> 文件顶部注释提到 slime 还支持「fully async（全异步）」等更激进的方案，实现见 `examples/full_async`，本讲不展开。

#### 4.3.4 代码实践

**实践目标**：对比 train.py 与 train_async.py 在「采样发起时机」和「权重同步频率」上的差异。

**操作步骤**：

1. 把 [train.py:49-91](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py#L49-L91) 与 [train_async.py:33-73](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train_async.py#L33-L73) 并排打开。
2. 画两张时序图，分别画出 3 轮（rollout_id = 0,1,2）在两种循环下，「采样发起」「采样完成」「训练」「同步」这四类事件的时间先后。
3. 特别标注：在 async 版里，第 1 轮的训练与第 2 轮的采样是**重叠**的；在 sync 版里它们是**串行**的。

**需要观察的现象**：async 版在「同步权重」这一步前，会有一次额外的 `ray.get` 把后台采样收口——这是 sync 版没有的。

**预期结果**：你能画出类似下面的对比：

```
sync (train.py):
  R0采样 ─▶ R0训练 ─▶ 同步 ─▶ R1采样 ─▶ R1训练 ─▶ 同步 ─▶ ...

async (train_async.py):
  R0采样 ─▶ R0训练 ─┬─▶ R1采样(已提前) ─▶ R1训练 ─┬─▶ ...
                    └─同步(收口R1采样)            └─同步
```

#### 4.3.5 小练习与答案

**练习 1**：为什么 train_async.py 不支持 `--colocate`？

> **答案**：colocate 下采样和训练共用同一组 GPU 显存，必须**轮流**使用（一个 offload 给另一个腾地方），无法真正并行。预取的前提是「采样和训练能同时占用各自的资源」，这与共卡矛盾。见 [train_async.py:11](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train_async.py#L11)。

**练习 2**：把 `--update-weights-interval` 从 1 调到 3，对训练行为有什么影响？

> **答案**：actor 会连续训练 3 轮才同步一次权重给 rollout。好处是减少权重同步开销；副作用是这 3 轮里 rollout 用的是**旧权重**采的样，样本的 on-policy 程度下降（更 off-policy），可能影响训练稳定性。这正是 4.3.2 提到的「采样与训练错开」带来的权衡。

### 4.4 should_run_periodic_action：周期性 save/eval 的触发逻辑

#### 4.4.1 概念说明

「存档」和「评估」都挺贵（写大量检查点、跑一批推理），不适合每轮都做。slime 的做法是**按周期触发**：每训练 N 轮存一次档、每训练 M 轮评估一次。这个「要不要在这一轮做某件周期性的事」的判断，统一交给一个小工具函数 `should_run_periodic_action`。

它的设计目标有三点：

1. **可关闭**：不传间隔（`interval=None`）就完全不做。
2. **保证收尾**：无论间隔怎么设，**最后一轮**一定触发（避免训练完了没存最终检查点）。
3. **支持 epoch 边界**：除了按固定步数，还可以按「一个 epoch 结束」来触发。

#### 4.4.2 核心流程

函数接收四个参数：当前轮次 `rollout_id`、间隔 `interval`、可选的 `num_rollout_per_epoch`（一个 epoch 多少轮）、可选的 `num_rollout`（总共多少轮）。判断逻辑用伪代码表示：

```text
def should_run_periodic_action(rollout_id, interval, num_rollout_per_epoch, num_rollout):
    if interval is None:                      # 没配间隔 → 永不触发
        return False
    if num_rollout is not None and rollout_id == num_rollout - 1:
        return True                           # 最后一轮 → 一定触发
    step = rollout_id + 1                     # 转成「第几步」(从1开始)
    return (step % interval == 0)             # 按固定步数触发
        or (num_rollout_per_epoch and step % num_rollout_per_epoch == 0)  # 或按 epoch 边界触发
```

注意一个容易混淆的点：函数里用的是 `step = rollout_id + 1`（**第几步**，从 1 开始计数），而不是 `rollout_id`（从 0 开始）。所以「每 10 步触发」实际是在 `rollout_id == 9, 19, 29, ...` 时触发。

#### 4.4.3 源码精读

函数定义和完整实现：

[misc.py:107-128](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/misc.py#L107-L128) —— `should_run_periodic_action`：判断周期性动作（save/eval/checkpoint）是否应在这一轮执行。

逐行看关键判断：

- 第 121–122 行：`interval is None` 时返回 `False`，这就是 `--eval-interval` / `--save-interval` 不设就关闭评估/存档的来源。
- 第 124–125 行：最后一轮强制返回 `True`。注意这一条只有当调用方传了 `num_rollout` 时才生效——train.py 的**保存**调用传了 `num_rollout`（第 72 行），所以保存一定在最后一轮触发；而**评估**调用没传 `num_rollout`（第 90 行），评估在最后一轮是否触发取决于它是否正好命中 `eval_interval`。
- 第 127–128 行：核心的「取模」判断，支持两种节奏（固定步数 / epoch 边界）。

回到 train.py 看它怎么被用。**保存**的判断（注意这里多了一个 `release_train` 条件——release 模式必须每轮存档，否则下一轮没法重建）：

[train.py:71-73](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py#L71-L73) —— 保存判断：release_train 或周期命中时存档；注意这里传了 `num_rollout`，保证最后一轮一定存。

**评估**的判断（更简洁，不传 num_rollout）：

[train.py:90](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py#L90) —— 评估判断：只看 interval 与 epoch 边界。

#### 4.4.4 代码实践

**实践目标**：手工模拟 `should_run_periodic_action` 的判定，预测哪些轮次会存档/评估。

**操作步骤**：

1. 假设 `num_rollout = 20`，`save_interval = 5`，`num_rollout_per_epoch = 8`，`eval_interval = 3`。
2. 用 [misc.py:107-128](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/misc.py#L107-L128) 的逻辑，列出 `rollout_id` 从 0 到 19，每一轮 `should_run_periodic_action` 在「保存场景（带 num_rollout）」和「评估场景（不带 num_rollout）」下的返回值。
3. 标出每一轮到底是「存档 / 评估 / 都做 / 都不做」。

**需要观察的现象**：最后一轮（id=19）在保存场景下一定返回 `True`，但在评估场景下不一定。

**预期结果**（部分示例）：

| rollout_id | step | 保存(带num_rollout,save_interval=5,epoch=8) | 评估(eval_interval=3) |
| --- | --- | --- | --- |
| 0 | 1 | 否 | 否 |
| 2 | 3 | 否 | **是**（3%3==0） |
| 4 | 5 | **是**（5%5==0） | 否 |
| 7 | 8 | **是**（8%8==0，epoch 边界） | 否 |
| 9 | 10 | **是**（10%5==0） | 否 |
| 11 | 12 | **是**（12%8==0） | **是**（12%3==0） |
| 19 | 20 | **是**（最后一轮强制 + 20%5==0 + 20%8==0） | 否（20%3≠0，且未传 num_rollout） |

（注：第 11 轮既存档又评估；最后一轮 19 在评估场景下不触发，因为评估调用没传 `num_rollout`，第 124 行的「最后一轮强制」不生效。）

#### 4.4.5 小练习与答案

**练习 1**：为什么 train.py 的评估调用不传 `num_rollout`，而保存调用要传？

> **答案**：保存必须保证训练结束时有最终检查点可恢复，所以用「最后一轮强制」兜底，需要传 `num_rollout` 才能让第 124 行生效。评估是「锦上添花」的指标，最后一轮少评一次不影响训练产物，所以不需要强制。对照 [train.py:71-73](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py#L71-L73) 与 [train.py:90](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py#L90)。

**练习 2**：如果想让评估「只在每个 epoch 结束时做，且最后一轮也评」，当前函数能做到吗？

> **答案**：能做到一半。「只在 epoch 结束时做」可以通过 `eval_interval` 设成一个很大的数（远大于 num_rollout）来让固定步数永不命中，从而只剩 epoch 边界（`num_rollout_per_epoch`）触发。但「最后一轮也评」做不到——因为评估调用没传 `num_rollout`，第 124 行的强制收尾不生效。除非修改调用点把 `num_rollout` 传进去。

## 5. 综合实践

把本讲的三个核心模块串起来，完成下面这个**贯穿性任务**。

**任务**：假设你要给一位新同事讲解 slime 的训练主循环，请产出一页「训练主循环速查卡」，包含以下三部分：

1. **五阶段循环表**：基于 [train.py:49-91](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py#L49-L91)，列出每一阶段的行号、关键调用、是否每轮必做。
2. **同步 vs 异步对比图**：画出 train.py 与 train_async.py 在 3 轮迭代下的时序差异，并用一句话标出「异步版多出来的两个动作」（提前发起下一轮采样、同步前收口后台采样）。
3. **一个预测题**：给定 `num_rollout=12, save_interval=4, eval_interval=6, num_rollout_per_epoch=5, colocate=False, use_critic=True`，回答：
   - 哪几轮会存档？哪几轮会评估？
   - `offload_rollout` 最终是 True 还是 False？
   - 第 0 轮（rollout_id=0）训练时，critic 和 actor 谁先训？依据是哪一行？

**参考答案要点**：
- 存档轮次（带 num_rollout=12，最后一轮强制）：step∈{4,5,8,10,12} → rollout_id∈{3,4,7,9,11}。
- 评估轮次（不带 num_rollout）：step∈{5,6,10,12} → rollout_id∈{4,5,9,11}（其中 step=5 是 epoch 边界）。
- 不 colocate 且未显式指定 → `offload_rollout=False`。
- 第 0 轮训练时，由 [train.py:62-65](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py#L62-L65) 可知 critic 先训（`value_refs`），其结果作为 `external_data` 再训 actor。

## 6. 本讲小结

- train.py 把 RL 训练组织成 **「采样 → 训练 → 保存 → 同步 → 评估」** 的循环，重复 `num_rollout` 次；其中采样、训练、同步每轮必做，保存和评估按周期触发。
- `async_train` 的「async」指**返回 Ray future 不阻塞**，不是 Python asyncio；train.py 仍用 `ray.get` 等待结果，借此让 actor 训练能消费 critic 的输出。
- `--colocate` 让训练与推理共卡，通过 `offload_rollout` / `offload_train` **轮流让出显存**；`--release-train` 更激进，直接杀进程、每轮重建。
- train_async.py 的核心是**预取下一轮 rollout**，让采样与训练重叠以提升吞吐，代价是不支持 colocate；权重同步按 `update_weights_interval` 周期进行，且同步前要收口后台采样。
- `should_run_periodic_action` 统一决定「何时存档/评估」：可关闭、保证最后一轮存档（仅当调用方传 `num_rollout`）、支持固定步数与 epoch 边界两种节奏。
- 入口脚本本身很薄，真正逻辑在 `slime/ray/`（编排）和 `slime/backends/`（后端），本讲只看了主循环骨架。

## 7. 下一步学习建议

本讲只看了「指挥官视角」的循环骨架，循环里调用的每个方法都是一座冰山的水面之下。接下来建议进入 **U2 核心架构**：

- **u2-l1 三模块架构总览**：把本讲的「采样/训练/同步」和三模块的数据流对应起来，理解 Sample / 权重 / 指标如何在模块间流动。
- **u2-l2 Ray 编排：placement group 与资源分配**：搞清 `create_placement_groups` 如何把 GPU 分给训练和推理，以及 colocate 在物理层面如何实现。
- **u2-l3 三大对象：actor / critic / rollout_manager**：本讲里 `actor_model.async_train`、`rollout_manager.generate` 这些调用的「真身」是什么、返回 Ray ref 还是同步结果，都会在这里讲清。

读完 U2 后，再回头看本讲的循环，你会发现每一行都能「透视」到具体的实现。
