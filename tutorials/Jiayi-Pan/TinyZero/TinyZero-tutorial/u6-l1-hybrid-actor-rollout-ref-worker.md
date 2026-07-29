# ActorRolloutRefWorker 混合引擎

## 1. 本讲目标

在前面几讲里，我们已经知道 veRL 用「单控制器」把训练拆成 driver 编排 + Ray worker 计算，也知道了 driver 通过 `@register` 装饰器把方法分发（dispatch）到各 worker。但那些 worker 内部到底是什么样子？一个 GPU 进程里到底放了哪些模型？本讲就钻进最核心的一个 worker 类——`ActorRolloutRefWorker`，读完你应该能够：

- 理解「混合引擎（HybridEngine）」的设计思想：**一个 worker 类、三个角色标志**，让 actor（策略网络）、rollout（采样生成）、ref（参考策略）复用同一份代码甚至同一份权重。
- 读懂 `init_model` 在 `role='actor_rollout_ref'` 下依次构建了哪些子模块（`actor_module_fsdp`、`rollout`、`ref_module_fsdp`），以及为什么 actor 与 rollout 共用同一个 FSDP 模块、而 ref 是另一个独立模块。
- 说清 `generate_sequences` 末尾为什么要用 actor 自己的前向重新计算一遍 `old_log_probs`。
- 一眼判断 `update_actor` / `generate_sequences` / `compute_ref_log_prob` 各自用了哪种 Dispatch 模式，并能解释原因。

## 2. 前置知识

本讲是 advanced 阶段，默认你已经掌握以下内容（否则建议先读对应讲义）：

- **PPO 三大角色的分工**（可回看 u4-l2、u4-l3）：actor 是被训练的策略网络；rollout 用当前策略采样出回答；ref 是冻结的参考策略，用来算 KL 惩罚，把策略「拴」在基座模型附近。
- **DataProto 协议**（u3-l1）：worker 方法的输入输出基本都是 `DataProto`，`chunk`/`concat` 实现数据在 driver 与 worker 之间的切分与合并。
- **Dispatch 装饰器**（u3-l3）：`@register(dispatch_mode=...)` 给方法打标签；`ONE_TO_ALL` 是广播（无数据输入、全员同构执行），`DP_COMPUTE_PROTO` 是数据并行（带 `data: DataProto`，先 `chunk` 切分再 `concat` 拼回）。
- **FSDP**（Fully Sharded Data Parallel）：PyTorch 的分布式并行方案，把模型参数分片到各 GPU 上，是 veRL 默认的训练后端（u1-l4 提到的 `strategy=fsdp`）。
- **vLLM**：高速推理引擎，veRL 用它做 rollout 生成；它与 FSDP 训练态是两套权重，靠 sharding manager 同步（详见 u6-l5）。

一个关键直觉：在 RL 训练里，**采样用的策略**和**训练用的策略**本来就该是同一个（都是 actor）。传统做法是把 actor 和 rollout 拆成两个进程、各放一份模型；而「混合引擎」反其道而行——把它们塞进同一个进程、共享同一份权重，省一份显存。这就是本讲名字里「Hybrid」的由来。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [verl/workers/fsdp_workers.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py) | 本讲主角。定义 `ActorRolloutRefWorker`（以及 `CriticWorker`、`RewardModelWorker`），是 FSDP 后端下所有角色的实体 worker。 |
| [verl/workers/sharding_manager/base.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/sharding_manager/base.py) | `BaseShardingManager`，定义 sharding manager 的上下文管理器协议（`__enter__`/`__exit__`/`preprocess_data`/`postprocess_data`）。HF rollout 用它做空操作；vLLM rollout 用其子类做权重同步。 |
| [verl/workers/actor/dp_actor.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py) | `DataParallelPPOActor`，被 `ActorRolloutRefWorker` 用来包一层 FSDP 模型，提供 `compute_log_prob` / `update_policy`。actor 与 ref 都复用它（ref 不传 optimizer）。 |
| [verl/trainer/ppo/ray_trainer.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py) | `init_workers` 里把 `ActorRolloutRefWorker` 按 `role` 实例化并 colocate 的接线点（承接 u4-l2）。 |

## 4. 核心概念与源码讲解

### 4.1 混合引擎设计：一个类承担三种角色

#### 4.1.1 概念说明

「混合引擎」要解决的问题是：RL 训练循环里，actor、rollout、ref 三件事都要 transformer 模型，但它们的生命周期、显存占用、是否需要优化器各不相同。

- **actor**：需要模型 + 优化器 + 学习率调度器，要反向传播更新权重。
- **rollout**：需要模型做推理生成，不需要优化器、不需要梯度。
- **ref**：需要模型做前向算 log-prob，完全冻结，不需要优化器、不需要梯度。

如果为每个角色各写一个 worker 类，会有大量重复代码（加载 HF 模型、包 FSDP、算 log-prob 的逻辑几乎一样）。veRL 的做法是写**一个** `ActorRolloutRefWorker` 类，用一个字符串 `role` 告诉它「你这一个实例要同时扮演哪几个角色」，再用三个布尔标志 `_is_actor` / `_is_rollout` / `_is_ref` 在每个方法和每段初始化里做条件分支。

更妙的是：当 actor 和 rollout 在同一个实例里时，它们**共用同一个 FSDP 模块** `actor_module_fsdp`——训练用的策略权重和采样用的策略权重是同一份物理权重，既省显存，又天然保证「采样策略 = 训练策略」。

> 名词解释：**colocate（共置）** 指把多个角色进程塞进同一组 GPU；**HybridEngine** 进一步指在「同一个 worker 实例/进程」内用标志位复用代码与权重。本讲的 `ActorRolloutRefWorker` 是 HybridEngine 的载体。

#### 4.1.2 核心流程

```text
构造时传入 role 字符串
        │
        ▼
assert role ∈ {actor, rollout, ref, actor_rollout, actor_rollout_ref}
        │
        ▼
派生三个布尔标志：_is_actor / _is_rollout / _is_ref
        │
        ▼
后续每个方法/每段初始化都用这三个标志做开关：
  - _is_actor  → 建优化器、可调用 update_actor、可算 old_log_probs
  - _is_rollout → 建 rollout 引擎、可调用 generate_sequences
  - _is_ref    → 建 ref 模型、可调用 compute_ref_log_prob
```

五种合法 `role` 与三个标志的对应关系：

| role | _is_actor | _is_rollout | _is_ref | 含义 |
| --- | :---: | :---: | :---: | --- |
| `actor` | ✓ | | | 纯策略训练 |
| `rollout` | | ✓ | | 纯采样生成 |
| `ref` | | | ✓ | 纯参考策略 |
| `actor_rollout` | ✓ | ✓ | | actor + rollout 共一份权重（TinyZero 默认走这条） |
| `actor_rollout_ref` | ✓ | ✓ | ✓ | 三合一，一个实例全包 |

#### 4.1.3 源码精读

类的文档字符串开宗明义：这个 worker 既可以单独做 actor、rollout、ref，也可以根据配置做成混合引擎。

[verl/workers/fsdp_workers.py:47-51](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L47-L51) —— 类定义与设计意图。

真正的「角色开关」在 `__init__` 里，用一个 `role` 字符串派生出三个布尔标志：

[verl/workers/fsdp_workers.py:77-82](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L77-L82) —— `role` 断言与三个标志的派生。这段是整个混合引擎的「总开关」。

紧跟着的几行用标志决定**显存 offload 策略**：只有 actor 才需要考虑 param/grad/optimizer 三级 offload；ref 只考虑 param offload（因为它既无梯度也无优化器）。

[verl/workers/fsdp_workers.py:84-93](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L84-L93) —— 按 `_is_actor` / `_is_ref` 分别读 offload 配置。

`__init__` 还会按角色**归一化 batch size 配置**：把全局的 `ppo_mini_batch_size` 等除以数据并行宽度，再乘以 `rollout.n`（每个 prompt 采样 n 条），得到每个 rank 的本地 batch size。

[verl/workers/fsdp_workers.py:96-109](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L96-L109) —— 注意 ref 分支也乘了 `rollout.n`，因为 ref 要对每条采样回答都算 log-prob。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：验证你对五个 role 与三个标志映射的掌握。

**操作步骤**：

1. 打开 [verl/workers/fsdp_workers.py:78](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L78) 的 `assert` 语句，确认合法 role 集合。
2. 在本地起一个 Python REPL，把第 80–82 行的派生逻辑抄进去，遍历五个 role 打印三个标志：

   ```python
   # 示例代码：仅复刻标志派生逻辑，不依赖真实环境
   roles = ['actor', 'rollout', 'ref', 'actor_rollout', 'actor_rollout_ref']
   for role in roles:
       is_actor = role in ['actor', 'actor_rollout', 'actor_rollout_ref']
       is_rollout = role in ['rollout', 'actor_rollout', 'actor_rollout_ref']
       is_ref = role in ['ref', 'actor_rollout_ref']
       print(f'{role:20s} actor={is_actor} rollout={is_rollout} ref={is_ref}')
   ```

**需要观察的现象**：输出应与上面 4.1.2 的表格一一对应；特别注意 `ref` 只有在 `actor_rollout_ref` 这一个 role 下才与 actor/rollout 同时为 True。

**预期结果**：`actor_rollout_ref` 是唯一三个标志全为 True 的 role。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `role='rollout'` 时，`update_actor` 方法不应该是可用的？  
**答案**：`update_actor` 内部有 `assert self._is_actor`（见 4.4 节源码）。纯 rollout 实例没有 actor 优化器、没有 `self.actor`，调用会直接断言失败。设计上靠标志位把「不该有的能力」在运行时关掉。

**练习 2**：如果我想让一个实例只做参考策略（不采样、不训练），该传哪个 role？它会不会构建优化器？  
**答案**：传 `role='ref'`。它不会构建优化器——`_build_model_optimizer` 里只有 `self._is_actor` 为真时才创建 `actor_optimizer`，否则返回 `None`。

---

### 4.2 init_model：三种角色的构建顺序

#### 4.2.1 概念说明

`init_model` 是每个 worker 实例被 spawn 出来后第一个被调用的方法（由 u4-l2 的 `init_workers()` 触发）。它负责把「模型权重 + 优化器 + 推理引擎」从磁盘加载到 GPU 上。

它身上有两个值得注意的设计：

1. **它是 `ONE_TO_ALL`**：没有 `DataProto` 输入，每个 rank 跑**完全相同**的逻辑去构建自己那份本地模型。driver 只是把同一个调用广播 `world_size` 份（回顾 u3-l3）。
2. **构建顺序有讲究**：先 actor 模型（顺带 optimizer），再 rollout 引擎（复用 actor 模型），最后 ref 模型。这个顺序和「rollout 必须最后初始化」的显存约束直接相关（见 u4-l2：vLLM 要基于剩余显存估算 KV cache）。

最关键的一点：**actor 和 rollout 共用同一个 FSDP 模块 `actor_module_fsdp`**，而 ref 是另一个独立的 FSDP 模块 `ref_module_fsdp`。`_build_model_optimizer` 因此会被调用两次——一次给 actor/rollout（带 optimizer、默认 fp32），一次给 ref（不带 optimizer、bf16、可独立 offload）。

#### 4.2.2 核心流程

`role='actor_rollout_ref'`（三标志全真）时，`init_model` 的执行顺序：

```text
1. _is_actor or _is_rollout  为真
   └─ _build_model_optimizer(actor 配置 + optim 配置)
        → self.actor_module_fsdp        # FSDP 模型（actor 与 rollout 共用）
        → self.actor_optimizer          # 仅 actor 有
        → self.actor_lr_scheduler       # 仅 actor 有
        → self.actor_model_config
   └─ self.actor_module = actor_module_fsdp._fsdp_wrapped_module  # 解包出原始 HF 模块

2. _is_actor 为真
   └─ self.actor = DataParallelPPOActor(actor_module_fsdp, actor_optimizer)  # 包一层，提供 update_policy/compute_log_prob

3. _is_rollout 为真
   └─ self.rollout, self.rollout_sharding_manager = _build_rollout()
        └─ vLLMRollout(actor_module=self.actor_module_fsdp, ...)  # 关键：复用同一个 FSDP 模块！

4. _is_ref 为真
   └─ self.ref_module_fsdp = _build_model_optimizer(ref 配置, optim=None)[0]  # 再调一次，独立模型、无优化器
   └─ self.ref_policy = DataParallelPPOActor(ref_module_fsdp)   # 注意：没传 optimizer → 当作参考策略

5. _is_actor 为真
   └─ self.flops_counter = FlopsCounter(self.actor_model_config)  # 算 MFU 用
```

要点：第 1 步的模型既给 actor 训练用，又给第 3 步 rollout 采样用——这是「混合」的物理体现；第 4 步的 ref 模型是完全独立的另一份权重，只读不改。

#### 4.2.3 源码精读

`init_model` 的装饰器与签名——注意是 `ONE_TO_ALL`，且无 `data` 参数：

[verl/workers/fsdp_workers.py:284-285](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L284-L285) —— `@register(dispatch_mode=Dispatch.ONE_TO_ALL)` 标记全员同构执行。

第一段：actor 或 rollout 需要模型，于是调 `_build_model_optimizer`。是否带 optimizer 由 `_is_actor` 决定（纯 rollout 不需要 optimizer，故传 `optim_config=None`）。

[verl/workers/fsdp_workers.py:295-313](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L295-L313) —— 构建 actor/rollout 共用的 FSDP 模型，并解包出原始模块存到 `self.actor_module`。

`_build_model_optimizer` 内部有两处反映「actor 与 ref 的差异」：一是默认 dtype，actor 用 fp32（保证 optimizer 状态精度），ref 用 bf16（省显存、不求梯度）；二是只在 `_is_actor` 时创建 optimizer 与 lr scheduler。

[verl/workers/fsdp_workers.py:132-134](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L132-L134) —— dtype 选择：actor 默认 fp32，其余 bf16。

[verl/workers/fsdp_workers.py:227-244](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L227-L244) —— 仅 actor 创建 AdamW 优化器与 warmup 调度器，否则返回 `None`。

第二段：actor 模型被 `DataParallelPPOActor` 包一层，得到 `self.actor`（具备 `update_policy` / `compute_log_prob`）。

[verl/workers/fsdp_workers.py:323-329](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L323-L329) —— 用 `open_dict` 往 actor 配置里塞 `use_remove_padding`，再实例化 `self.actor`。

第三段：构建 rollout。关键看 `_build_rollout`——它把 `self.actor_module_fsdp` 直接交给 vLLM 引擎，让推理引擎与训练模型共享权重。

[verl/workers/fsdp_workers.py:331-332](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L331-L332) —— 触发 `_build_rollout`。

[verl/workers/fsdp_workers.py:264-279](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L264-L279) —— `vLLMRollout(actor_module=self.actor_module_fsdp, ...)` 复用 actor 的 FSDP 模型；并构造 `FSDPVLLMShardingManager` 负责训练态↔推理态的权重同步。

第四段：构建 ref。再调一次 `_build_model_optimizer`（传 ref 配置、无 optimizer），拿到独立的 `ref_module_fsdp`，同样用 `DataParallelPPOActor` 包，但**不传 optimizer**——`DataParallelPPOActor` 的注释明确写着「When optimizer is None, it is Reference Policy」。

[verl/workers/fsdp_workers.py:334-348](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L334-L348) —— ref 模型独立构建，包成 `self.ref_policy`。

> 真实细节提醒：在 TinyZero 默认的 main_ppo 配置里，driver 并没有用一个 `actor_rollout_ref` 实例包打天下，而是把同一个 `ActorRolloutRefWorker` 类实例化**两次**后 colocate 到同一组 GPU——一次 `role='actor_rollout'`（actor+rollout 共享一份权重），一次 `role='ref'`（独立参考策略）。两种做法都成立，因为类本身就是靠标志位驱动的；本讲为了讲清「三合一」的设计，统一用 `role='actor_rollout_ref'` 作为分析对象。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：梳理 `role='actor_rollout_ref'` 下 `init_model` 到底初始化了哪些子模块，并区分共用与独立。

**操作步骤**：

1. 读 [verl/workers/fsdp_workers.py:285-353](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L285-L353) 全文。
2. 在纸上画一张表，左列是 `self.xxx` 属性，右列标注「actor/rollout 共用」还是「ref 独立」还是「仅 actor」。

**需要观察的现象**：`actor_module_fsdp` 既被 `self.actor`（训练）引用，又被 `_build_rollout`（采样）引用；`ref_module_fsdp` 只被 `self.ref_policy` 引用，与前者互不相干。

**预期结果**：应得到类似下表（待本地验证：可在 `init_model` 末尾临时打印 `self.__dict__.keys()` 核对）：

| 属性 | 来源 | 归属 |
| --- | --- | --- |
| `actor_module_fsdp` | `_build_model_optimizer` 第 1 次 | actor + rollout 共用 |
| `actor_optimizer` / `actor_lr_scheduler` | 第 1 次，仅 actor | 仅 actor |
| `actor` | `DataParallelPPOActor(...)` | 仅 actor |
| `rollout` / `rollout_sharding_manager` | `_build_rollout` | 仅 rollout（但复用 actor 模型） |
| `ref_module_fsdp` | `_build_model_optimizer` 第 2 次 | ref 独立 |
| `ref_policy` | `DataParallelPPOActor(ref_module_fsdp)` | ref 独立 |
| `flops_counter` | `FlopsCounter(...)` | 仅 actor |

#### 4.2.5 小练习与答案

**练习 1**：为什么 ref 要单独再调一次 `_build_model_optimizer`，而不是和 actor 共用 `actor_module_fsdp`？  
**答案**：因为 ref 必须是**冻结**的参考策略——它的权重在整个训练过程中不能被 actor 的梯度更新改动。如果共用同一对象，actor 一更新权重，ref 算出的 log-prob 就跟着变了，KL 惩罚就失去意义。所以 ref 要一份独立的、不挂优化器的模型副本。

**练习 2**：`_build_model_optimizer` 被调用两次时，哪一次会创建 optimizer？为什么？  
**答案**：第一次（actor/rollout）会创建，第二次（ref）不会。因为函数内部用 `if self._is_actor:` 守卫 optimizer 创建逻辑；第二次调用发生在 `_is_ref` 分支，此时虽仍 `_is_actor` 为真（在三合一情况下），但传入了 `optim_config=None`。更本质地，ref 不需要训练，所以即便包成 `DataParallelPPOActor` 也不传 optimizer。

---

### 4.3 generate_sequences：生成与 old_log_probs 重算

#### 4.3.1 概念说明

`generate_sequences` 是 rollout 角色的核心方法：吃进一批 prompt（`DataProto`），吐出一批 prompt+response。它的特别之处不在生成本身，而在**生成完之后多做了一步——用 actor 自己的前向把 `old_log_probs` 重新算一遍**。

为什么非要重算？这要从 PPO 的目标函数说起。PPO 用重要性采样比来纠正「用旧策略采的数据估计新策略梯度」的偏差：

\[ \text{ratio} = \exp(\log\pi_{\text{new}}(a|s) - \log\pi_{\text{old}}(a|s)) \]

这里的 \(\log\pi_{\text{old}}\) 就是 `old_log_probs`。理论上，「采样用的策略」和「old_log_probs 对应的策略」必须是同一个，否则 ratio 在更新起点就该偏离 1。

在 HybridEngine 里，采样由 vLLM 完成。vLLM 是一个独立的推理引擎，它的权重是从 FSDP 同步过来的一份副本，运行在 bf16、用 PagedAttention 等完全不同的 kernel。如果直接拿 vLLM 内部算的 logprob 当 `old_log_probs`，它会和随后 `update_policy` 里 actor FSDP 前向算出的 `log_prob` 在数值上不一致（不同 kernel、不同 dtype、不同舍入），导致 ratio 一开始就不等于 1，引入系统性偏差。

因此 HybridEngine 的做法是：**vLLM 只负责生成 token，log-prob 一律由 actor 的 FSDP 模型重新前向计算**。这样 `old_log_probs` 和 `update_policy` 里的 `log_prob` 用的是同一套前向、同一套数值，更新起点 ratio 严格为 1。源码注释一句话点破：「we should always recompute old_log_probs when it is HybridEngine」。

还有一个温度（temperature）细节：采样时 logits 会除以 temperature，所以重算时也必须传同一个 temperature（见 `compute_log_prob` 里 `logits.div_(temperature)`），否则 log-prob 对应的不是真实采样分布。

#### 4.3.2 核心流程

```text
generate_sequences(prompts: DataProto)        # Dispatch: DP_COMPUTE_PROTO，按 dp 切分 prompts
   │
   ├─ 读 recompute_log_prob（默认 True；验证时为 False）
   ├─ assert self._is_rollout
   │
   ├─ with self.rollout_sharding_manager:     # 进入上下文：vLLM 后端会在此同步 FSDP→vLLM 权重
   │     ├─ preprocess_data(prompts)          # 按 tp 组做 allgather/broadcast 对齐
   │     ├─ output = self.rollout.generate_sequences(prompts)   # vLLM 真正生成
   │     └─ postprocess_data(output)          # 按 tp 组把结果 chunk 回去
   │
   ├─ if self._is_actor and recompute_log_prob:    # HybridEngine 的关键一步
   │     ├─ 往 output.meta_info 塞 micro_batch_size / temperature / max_token_len / use_dynamic_bsz
   │     ├─ with self.ulysses_sharding_manager:
   │     │     ├─ preprocess_data(output)
   │     │     ├─ old_log_probs = self.actor.compute_log_prob(output)   # 用 actor FSDP 重新前向
   │     │     ├─ output.batch['old_log_probs'] = old_log_probs         # 写回 DataProto
   │     │     └─ postprocess_data(output)
   │
   ├─ output.to('cpu')                        # 搬回 CPU 释放显存
   └─ torch.cuda.empty_cache()
```

#### 4.3.3 源码精读

方法开头的 `recompute_log_prob` 默认为 True，只在验证（`_validate`）时被 driver 设成 False——因为验证不需要 `old_log_probs` 做训练。

[verl/workers/fsdp_workers.py:400-414](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L400-L414) —— `generate_sequences` 装饰器（`DP_COMPUTE_PROTO`）、读取重算开关、把 eos/pad token 注入 meta_info。

生成在 sharding manager 上下文里完成。`with self.rollout_sharding_manager:` 对应 [base.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/sharding_manager/base.py#L21-L33) 定义的协议：`__enter__`/`__exit__`/`preprocess_data`/`postprocess_data`。HF rollout 用 `BaseShardingManager`（全是空操作）；vLLM rollout 用 `FSDPVLLMShardingManager`（在 `__enter__` 把 FSDP 权重同步到 vLLM，`__exit__` 卸载，详见 u6-l5）。

[verl/workers/fsdp_workers.py:415-423](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L415-L423) —— 在上下文内做 preprocess → 生成 → postprocess。

接下来就是本讲的「点睛之笔」——重算 `old_log_probs`。注释直说 HybridEngine 必须重算：

[verl/workers/fsdp_workers.py:425-436](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L425-L436) —— 注入 temperature 等参数后，用 `self.actor.compute_log_prob` 重算并写回 `output.batch['old_log_probs']`。

注意守卫条件是 `self._is_actor and recompute_log_prob`：纯 rollout 实例（`role='rollout'`）没有 `self.actor`，无法重算，这一步被跳过——这也解释了为什么「采样」和「算 old_log_probs」最好放在同一个持有 actor 的实例里，正是 HybridEngine 的动机之一。

`compute_log_prob` 本身在 `DataParallelPPOActor` 里：它把模型置 eval、按 micro batch 前向、用 `logits.div_(temperature)` 后取 log-softmax。

[verl/workers/actor/dp_actor.py:153-175](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L153-L175) —— `compute_log_prob` 签名与对 temperature 的强约束（注释提醒 temperature 必须在 meta_info 里以避免「silent error」）。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：说清 `generate_sequences` 末尾重算 `old_log_probs` 的原因，并定位相关代码。

**操作步骤**：

1. 读 [verl/workers/fsdp_workers.py:425-436](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L425-L436) 的注释与实现。
2. 追一条调用链：`generate_sequences` → `self.actor.compute_log_prob` → `_forward_micro_batch`，确认 log-prob 是用 FSDP 的 actor 模型算的，而不是 vLLM。

**需要观察的现象**：vLLM 生成的 token 序列先被写进 `output`，随后 `old_log_probs` 由 `self.actor`（FSDP）独立算出并覆盖进同一个 `output.batch`；temperature 从 `self.config.rollout.temperature` 一路传到 `logits.div_(temperature)`。

**预期结果**：能用一句话回答——「因为 vLLM 与 FSDP 的前向数值不一致，必须用 actor 自己的前向重算 old_log_probs，保证 PPO 的 importance ratio 在更新起点为 1」。

#### 4.3.5 小练习与答案

**练习 1**：如果 `recompute_log_prob=False`（验证场景），`old_log_probs` 还会被计算吗？为什么验证可以不算？  
**答案**：不会，重算分支被 `recompute_log_prob` 守卫跳过。验证阶段只关心生成的回答和奖励（用来算 test score），不更新策略，自然不需要 `old_log_probs` 这个训练专用量。

**练习 2**：为什么重算时一定要把 `temperature` 传进 meta_info？  
**答案**：因为采样分布是 softmax(logits/T)，对应的 log-prob 也是基于 logits/T 计算的。`_forward_micro_batch` 里有 `logits.div_(temperature)`；不传或传错 temperature，算出的 `old_log_probs` 就对不上真实采样分布，ratio 失真。源码注释把 temperature 缺失称作「silent error」，故强制要求显式传入。

---

### 4.4 三类计算方法的 Dispatch 标记

#### 4.4.1 概念说明

`ActorRolloutRefWorker` 暴露给 driver 的方法都带 `@register(dispatch_mode=...)` 标签（机制见 u3-l3）。判断用哪种 Dispatch 的规则很简单：

- 方法签名里**有 `data: DataProto` 要被并行处理** → 用 `DP_COMPUTE_PROTO`（driver 先 `chunk(world_size)` 切分，各 rank 处理自己的分片，再 `concat` 拼回）。
- 方法**没有 DataProto 输入**（如初始化、存盘），全员跑同样逻辑 → 用 `ONE_TO_ALL`（广播，输出原样返回列表）。

#### 4.4.2 核心流程

| 方法 | Dispatch | 输入 | 为什么是这个 |
| --- | --- | --- | --- |
| `init_model` | `ONE_TO_ALL` | 无 | 每个 rank 各自建本地模型，无数据切分 |
| `update_actor(data)` | `DP_COMPUTE_PROTO` | `DataProto` | 把训练数据切给各 rank 各自反传，再拼回 metrics |
| `generate_sequences(prompts)` | `DP_COMPUTE_PROTO` | `DataProto` | 把 prompt 切给各 rank 各自采样，再拼回结果 |
| `compute_ref_log_prob(data)` | `DP_COMPUTE_PROTO` | `DataProto` | 把数据切给各 rank 各自前向，再拼回 ref_log_prob |
| `save_checkpoint` | `ONE_TO_ALL` | 无（仅路径） | 每个 rank 参与存盘，无数据切分 |

#### 4.4.3 源码精读

`init_model` 与 `save_checkpoint` 是 `ONE_TO_ALL`：

[verl/workers/fsdp_workers.py:284](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L284) —— `init_model` 用 `ONE_TO_ALL`。

[verl/workers/fsdp_workers.py:477](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L477) —— `save_checkpoint` 同样 `ONE_TO_ALL`。

三个计算方法都是 `DP_COMPUTE_PROTO`，且都先 `data.to('cuda')`，处理完 `output.to('cpu')`，中间夹着 offload 的 load/offload 对（如果开了 CPU offload）：

[verl/workers/fsdp_workers.py:355-356](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L355-L356) —— `update_actor` 用 `DP_COMPUTE_PROTO`，输入 `data: DataProto`。

[verl/workers/fsdp_workers.py:400-401](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L400-L401) —— `generate_sequences` 用 `DP_COMPUTE_PROTO`。

[verl/workers/fsdp_workers.py:448-449](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L448-L449) —— `compute_ref_log_prob` 用 `DP_COMPUTE_PROTO`。

`update_actor` 内部还展示了「offload 配对」模式：进入时 `load_fsdp_param_and_grad` / `load_fsdp_optimizer` 把参数搬回 GPU，结束时再 `offload_fsdp_*` 搬回 CPU，最后 `torch.cuda.empty_cache()`。这是 FSDP offload 的标准节拍。

[verl/workers/fsdp_workers.py:360-365](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L360-L365) —— update_actor 开头的 load；与之对应的是末尾 [393-397](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L393-L397) 的 offload。

#### 4.4.4 代码实践（源码阅读型）

**实践目标**：不看本讲表格，仅凭方法签名推断 Dispatch 模式。

**操作步骤**：

1. 打开 [verl/workers/fsdp_workers.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py)，找到 `ActorRolloutRefWorker` 的全部 `@register`。
2. 对每个方法，只看参数列表里有没有 `data: DataProto` / `prompts: DataProto`，先猜 Dispatch，再核对装饰器。

**需要观察的现象**：凡是有 `DataProto` 入参的都是 `DP_COMPUTE_PROTO`；没有的都是 `ONE_TO_ALL`。规则零例外。

**预期结果**：5 个被注册方法的 Dispatch 与 4.4.2 表格完全一致。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `update_actor` 必须是 `DP_COMPUTE_PROTO` 而不能用 `ONE_TO_ALL`？  
**答案**：`update_actor(data)` 要把一整批训练数据分给各 rank 各自做反向传播。若用 `ONE_TO_ALL`，每个 rank 都会拿到完整数据重复计算，既浪费又会重复更新梯度，语义错误。`DP_COMPUTE_PROTO` 先 `chunk` 切分、各算各的、最后 `concat` metrics，才是正确的数据并行。

**练习 2**：`save_checkpoint` 用 `ONE_TO_ALL`，但存盘显然要协调多个 rank（FSDP 要 gather 完整 state_dict）。这两者矛盾吗？  
**答案**：不矛盾。`ONE_TO_ALL` 只管「driver→worker 的调用与数据搬运方式」（无 DataProto、全员执行），而多 rank 之间的权重 gather 是方法**内部**用 `FSDP.state_dict_type(..., FullStateDictConfig(rank0_only=True))` 完成的（见 [verl/workers/fsdp_workers.py:488-491](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L488-L491)）。Dispatch 描述的是 driver↔worker 的数据流，rank 间的分布式协调是方法体自己的事。

---

## 5. 综合实践

把本讲三块知识（角色标志、init_model 顺序、generate_sequences 重算）串起来，完成下面这个「读源码 + 画图」任务。

**任务背景**：假设你拿到一个 `role='actor_rollout_ref'` 的 `ActorRolloutRefWorker` 实例（三标志全真），要向同事讲清「它内部到底有什么、训练一步时数据怎么流过它」。

**步骤 1 — 列子模块**：对照 [init_model](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L285-L353)，列出该实例初始化后拥有的全部 `self.xxx` 属性，并标注：

- 哪些来自第一次 `_build_model_optimizer`（actor/rollout 共用：`actor_module_fsdp`、`actor_optimizer`、`actor_lr_scheduler`、`actor_model_config`、`actor_module`）；
- `self.actor`、`self.rollout` / `self.rollout_sharding_manager` 各自由哪段代码产生；
- 哪些来自第二次 `_build_model_optimizer`（ref 独立：`ref_module_fsdp`、`ref_policy`）。

**步骤 2 — 画共用关系图**：画出 `actor_module_fsdp` 同时被 `self.actor`（训练）和 `self.rollout`（vLLM 采样）引用的关系，并在两者之间标出 `FSDPVLLMShardingManager` 负责「训练态↔推理态权重同步」。再单独画出 `ref_module_fsdp → self.ref_policy` 这条独立支线。体会「actor/rollout 共用一份权重、ref 另起一份」的设计。

**步骤 3 — 解释重算**：用一句话回答——`generate_sequences` 末尾为什么要在 `if self._is_actor and recompute_log_prob:` 里调 `self.actor.compute_log_prob` 重算 `old_log_probs`？要求点出三个关键词：**vLLM 与 FSDP 数值不一致**、**PPO importance ratio 起点应为 1**、**temperature 必须一致**。

**步骤 4（可选，待本地验证）**：如果你有单卡环境，可在 [generate_sequences](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/fsdp_workers.py#L400-L446) 的重算前后各加一行 `print(old_log_probs.mean())`（标注为示例代码，务必说明这是你临时加的调试日志，不属于项目原代码），观察重算前（若 vLLM 返回了 logprob）后数值差异；无环境则跳过，不要假装运行过。

**交付物**：一张子模块清单表 + 一张共用关系图 + 一段重算原因说明。

## 6. 本讲小结

- `ActorRolloutRefWorker` 是 veRL FSDP 后端下的「万能角色」类，靠一个 `role` 字符串派生出 `_is_actor` / `_is_rollout` / `_is_ref` 三个布尔标志，用标志位在方法与初始化里做条件分支，从而一个类同时支持五种角色组合。
- 在 `role='actor_rollout_ref'`（或默认 colocate 的 `actor_rollout` + `ref`）下，`init_model` 先建 actor/rollout **共用**的 `actor_module_fsdp`（带 optimizer），再用 `_build_rollout` 把同一份 FSDP 模型交给 vLLM 复用，最后**独立**构建冻结的 `ref_module_fsdp`——actor 与 rollout 共享权重是「混合引擎」的物理体现。
- `init_model` 是 `ONE_TO_ALL`（无数据、全员同构）；`_build_model_optimizer` 被调两次，actor 默认 fp32 带 optimizer，ref 用 bf16 无 optimizer。
- `generate_sequences` 是 `DP_COMPUTE_PROTO`；它末尾在 `self._is_actor and recompute_log_prob` 守卫下，用 actor 自己的 FSDP 前向重算 `old_log_probs`，原因是 vLLM 与 FSDP 前向数值不一致，必须保证 PPO 的 importance ratio 在更新起点为 1，且 temperature 要与采样时一致。
- `update_actor` / `generate_sequences` / `compute_ref_log_prob` 都因带 `DataProto` 入参而是 `DP_COMPUTE_PROTO`；`init_model` / `save_checkpoint` 因无数据入参而是 `ONE_TO_ALL`——判断规则零例外。
- sharding manager 遵循 [base.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/sharding_manager/base.py#L21-L33) 定义的 `__enter__`/`__exit__`/`preprocess_data`/`postprocess_data` 协议；HF rollout 用空操作的 `BaseShardingManager`，vLLM rollout 用其子类做权重同步。

## 7. 下一步学习建议

本讲只讲了 `ActorRolloutRefWorker` 的「外壳与角色编排」，还没深入它内部各个引擎的细节。建议按以下顺序继续：

- **u6-l2 Actor 策略前向与更新**：钻进 `DataParallelPPOActor.update_policy` / `compute_log_prob` / `_forward_micro_batch`，看清 micro/mini batch 切分、temperature 缩放、`remove_padding` 优化。
- **u6-l4 vLLM Rollout 生成**：精读 `vLLMRollout.generate_sequences`，搞清左填充去除、SamplingParams、attention_mask 重建。
- **u6-l5 FSDP↔vLLM 权重同步**：本讲反复提到的 `FSDPVLLMShardingManager` 到底如何在 `__enter__`/`__exit__` 里搬权重，这一讲给出完整答案。
- 若想了解 critic 一侧的对称设计，可读 **u6-l3 Critic 价值估计与更新**，对比 `CriticWorker` 与本讲的差异（critic 不需要 ratio/clip，不需要重算 old_log_probs）。
