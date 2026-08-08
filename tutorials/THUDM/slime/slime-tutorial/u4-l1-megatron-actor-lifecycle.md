# MegatronTrainRayActor 训练工人生命周期

## 1. 本讲目标

本讲带你走进 slime 训练后端的「心脏」——`MegatronTrainRayActor`。它是运行在每张训练 GPU 上的一个 Ray actor（训练工人），负责把 rollout 采样来的数据真正变成模型权重的更新。

读完本讲，你应当能够：

1. 说清 `TrainRayActor`（抽象基类）与 `MegatronTrainRayActor`（唯一实现）的继承关系，以及一个训练工人从「诞生」到「就绪」要经历哪些步骤。
2. 逐步复述 `train_actor` 内部的执行顺序：先切到 ref/teacher 模型算参考对数概率 → 切回 actor 算优势（advantage）→ 跑训练步，并能指出每一步分别调用了 `model.py` / `loss.py` 的哪个函数。
3. 理解 `with_ref`（KL 参考模型）和 `with_opd_teacher`（在线蒸馏教师模型）这两种「辅助模型」是如何被加载与切换的。
4. 解释 `update_weights` 如何把训练好的权重推送到 SGLang 推理引擎，从而闭合「训练 → 推理」的同步回路。

## 2. 前置知识

本讲假设你已经学过：

- **u2-l3 三大对象**：知道 `train.py` 操控的 `actor_model` / `critic_model` 本质上是本地门面 `RayTrainGroup`，内部 fan-out 到一组远程训练工人；`async_train` 返回 `ObjectRef` 列表不阻塞，其余方法内部已 `ray.get`。
- **u1-l6 训练主循环**：知道训练一轮的时序是 `generate → async_train → save_model → update_weights → eval`。
- **Megatron 基本概念**：知道张量并行（TP）、流水线并行（PP）、数据并行（DP）、专家并行（EP）大致是什么。本讲不会深入并行细节，但会频繁出现 `mpu`（Megatron 并行单元）相关的调用。

几个本讲会用到的术语，先建立直觉：

| 术语 | 含义 |
|------|------|
| Ray actor | 一个跑在远端、持有自己状态的「工人对象」，方法调用通过 Ray 消息传递 |
| rank | 进程在分布式通信组里的编号，0 号通常是主进程（负责落盘、日志） |
| `mpu` | Megatron 的并行单元（Model Parallel Utility），查询「我是第几个 TP/PP/DP/EP rank」 |
| offload_train | 训练完成后把模型权重/优化器状态从 GPU 显存「卸载」到 CPU，给推理腾位子 |
| loss_mask | 标记哪些 token 是模型生成的（算 loss，值为 1）、哪些是环境注入的（不算 loss，值为 0） |
| log_probs | 模型对每个 token 输出的对数概率，是 PPO/GRPO 重要性采样的核心量 |

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| [slime/ray/train_actor.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/train_actor.py) | 定义抽象基类 `TrainRayActor`：分布式初始化 + 抽象方法契约 |
| [slime/backends/megatron_utils/actor.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py) | 唯一实现 `MegatronTrainRayActor`：本讲的主角 |
| [slime/backends/megatron_utils/model.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py) | 训练引擎：`forward_only` / `train` / `train_one_step` / `save` / `initialize_model_and_optimizer` |
| [slime/backends/megatron_utils/loss.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py) | 损失与优势计算：`get_log_probs_and_entropy` / `get_values` / `compute_advantages_and_returns` |
| [slime/ray/actor_group.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/actor_group.py) | 本地门面 `RayTrainGroup`：fan-out 远程方法（承上启下，回顾用） |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：先看工人如何诞生（`init`），再看它如何干活（`train_actor`），最后看它如何把成果送出去（`update_weights`）。

### 4.1 MegatronTrainRayActor：一个训练工人的诞生

#### 4.1.1 概念说明

slime 的训练工人是一组 Ray actor，每张 GPU 上跑一个。它们都被 `RayTrainGroup`（u2-l3 讲过的本地门面）统一编排。但「做什么活、怎么干活」这件事，是由工人自身的类决定的。

slime 用「抽象基类 + 具体实现」来解耦：

- 抽象基类 [TrainRayActor](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/train_actor.py#L28-L129) 只规定「所有训练后端都必须实现的契约」和「与后端无关的公共逻辑」（如初始化进程组、设置 NUMA 亲和性），不关心你用 Megatron 还是别的框架。
- 具体实现 [MegatronTrainRayActor](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L51-L52) 继承它，把所有抽象方法替换成 Megatron 的真实实现。

这样做的好处是：未来若要接其它训练后端（比如用 PyTorch FSDP），只需写一个新子类，`RayTrainGroup` 和 `train.py` 一行都不用改。

#### 4.1.2 核心流程

一个工人从「创建」到「能训练」的生命周期如下：

```
RayTrainGroup.create()
   │  对每个 rank 调一次 actor.init.remote(...)
   ▼
MegatronTrainRayActor.init(args, role, with_ref, with_opd_teacher)
   │
   ├─ 1. monkey_patch_torch_dist()          # 给 torch.distributed 打补丁，支持进程组销毁重建
   ├─ 2. super().init(...)                  # 父类：建 NCCL 进程组、Gloo 组、设 NUMA
   ├─ 3. init(args)                         # Megatron 并行初始化（TP/PP/CP/EP/DP 通信组）
   ├─ 4. 读 hf_config + tokenizer           # 每张卡依次读，避免并发写冲突
   ├─ 5. initialize_model_and_optimizer()   # 搭模型 + 优化器 + 加载检查点
   ├─ 6. 算 train_parallel_config           # 上报 DP/CP/VPP 规模给 rollout_manager
   ├─ 7. weights_backuper.backup("actor")   # 把当前 actor 权重存到 CPU 备份槽
   ├─ 8. （可选）load_other_checkpoint       # ref / teacher / old_actor 辅助模型
   ├─ 9. 选 weight_updater 类               # 按模式/传输方式选权重同步实现
   └─10. （可选）sleep()                     # 若 offload_train，立即卸载显存
        返回 start_rollout_id               # 告诉主循环从第几轮开始训
```

注意：`init` 既是基类的构造初始化，又被子类**重写**。子类先调 `super().init(...)` 完成父类逻辑，再做 Megatron 特有的初始化。

#### 4.1.3 源码精读

**抽象基类 `TrainRayActor.init`** —— 与后端无关的分布式初始化：

[slime/ray/train_actor.py:50-66](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/train_actor.py#L50-L66) 这段代码设置了进程的环境变量（`MASTER_ADDR`/`MASTER_PORT`/`WORLD_SIZE`/`RANK`），并调用 `dist.init_process_group` 建立全局 NCCL 通信组、`init_gloo_group` 建立 CPU 端 Gloo 组。`backend=args.distributed_backend` 决定用 NCCL（GPU）还是其它。

> 关键点：`init` 接收 `with_ref` 和 `with_opd_teacher` 两个布尔参数，子类据此决定是否加载辅助模型（见 4.2.3）。

基类还声明了所有训练工人必须实现的抽象方法（`abc.abstractmethod`），这是 slime 训练后端的「契约」：

[slime/ray/train_actor.py:109-119](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/train_actor.py#L109-L119) 规定了 `train` / `save_model` / `update_weights` / `sleep` / `wake_up` / `_get_parallel_config` 这六个抽象方法，任何训练后端子类都必须实现。

**子类 `MegatronTrainRayActor.init` 的 Megatron 特有部分**：

[slime/backends/megatron_utils/actor.py:73-91](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L73-L91) 调用 `init(args)` 完成 Megatron 并行初始化（这一步在 `initialize.py` 里建 TP/PP/CP/EP/DP 通信组），逐卡读取 HF 配置和 tokenizer（用 `dist.barrier` 串行化避免并发写文件 bug），最后 `initialize_model_and_optimizer` 搭出模型与优化器并返回已加载的 `loaded_rollout_id`。

接着，工人会算出自己的并行规模并上报：

[slime/backends/megatron_utils/actor.py:100-107](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L100-L107) `train_parallel_config` 记录了 DP/CP/VPP 规模，它会通过 `set_rollout_manager` 传给 rollout_manager，让采样数据按 `dp_size` 正确切分份数（u2-l3 讲过的「两次握手」）。

然后是本讲的核心数据结构之一——权重备份器 `weights_backuper`：

[slime/backends/megatron_utils/actor.py:114-123](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L114-L123) `TensorBackuper` 在 CPU 内存里维护多个「标签槽」（tag），比如 `"actor"`、`"ref"`、`"teacher"`、`"old_actor"`。`backup("actor")` 把当前 GPU 上的 actor 权重拷一份到 CPU。这是 slime 在单卡内切换多个同结构模型（actor/ref/teacher）的机制——它们共享同一套 GPU 模型骨架，切换时只是把不同标签的权重 `restore` 进去（见 4.2.3）。

**辅助模型加载** —— 这是 `with_ref` / `with_opd_teacher` 落地的地方：

[slime/backends/megatron_utils/actor.py:125-137](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L125-L137)：

- `with_ref=True` → 加载参考模型到 `"ref"` 槽（用于 KL 约束，防止训练偏离太远）。
- `with_opd_teacher=True` → 加载教师模型到 `"teacher"` 槽（用于 Megatron 在线策略蒸馏 OPD）。
- `keep_old_actor=True` → 加载 old_actor，并把当前 actor 拷成 `rollout_actor`（用于异步训练时的 off-policy 修正）。

这些都走 `load_other_checkpoint`（见 4.1.4 末尾）。

**选择权重同步实现** —— `init` 还要根据用户参数挑出 `update_weights` 要用的具体类：

[slime/backends/megatron_utils/actor.py:148-175](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L148-L175) 这段 if/elif 根据 `update_weight_mode`（full/delta）和 `update_weight_transport`（nccl/disk）以及 `colocate` 选出四种类之一：`UpdateWeightFromDiskDelta` / `UpdateWeightFromDisk` / `UpdateWeightFromTensor`（colocate 时用，IPC 传输） / `UpdateWeightFromDistributed`（NCCL 全量）。这部分细节是下一单元 U5 的主题，本讲只需知道 `init` 会把它实例化成 `self.weight_updater`。

最后，如果开启了 `offload_train`，工人初始化完会立即 `sleep()` 把显存让出来：

[slime/backends/megatron_utils/actor.py:181-184](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L181-L184) 先切回 actor 权重再 sleep。`sleep` 的实现见 4.2.3。

#### 4.1.4 代码实践

**实践目标**：理解 `init` 中 `super().init()` 调用链与辅助模型加载的触发条件，并验证「ref 模型只在 `with_ref=True` 时加载」。

**操作步骤**：

1. 打开 [slime/ray/train_actor.py:50](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/train_actor.py#L50)，确认基类 `init` 接收 `with_ref=False, with_opd_teacher=False` 默认值。
2. 打开 [slime/ray/actor_group.py:187-202](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/actor_group.py#L187-L202)，看 `RayTrainGroup.create` 如何把 `self._with_ref` / `self._with_opd_teacher` 通过 `actor.init.remote(...)` 传给工人。
3. 在 [slime/backends/megatron_utils/actor.py:125-130](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L125-L130) 两处加一行日志：`logger.info(f"loading other checkpoint: model_tag={model_tag}")`（加在 `load_other_checkpoint` 内部即可）。
4. 阅读 `load_other_checkpoint` 的实现 [slime/backends/megatron_utils/actor.py:634-662](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L634-L662)，注意它如何临时改写 `self.args.load` 指向辅助检查点、用 `load_checkpoint` 载入、再恢复原参数，最后 `backup(model_tag)` 存到对应槽。

**需要观察的现象**：

- 如果一个训练配置里既没开 `--use-kl`（对应 `with_ref`）也没开 OPD，启动日志里**不应**出现 `loading other checkpoint: model_tag=ref/teacher`。
- `weights_backuper.backup_tags` 在 `init` 结束后应只含 `{"actor"}`；开了 KL 后应多出 `{"ref"}`。

**预期结果**：你能口头说清「辅助模型不是另开一个模型实例，而是复用同一套 GPU 骨架、用标签槽切换权重」，这是 slime 节省显存的关键设计。

> 注：本实践为源码阅读型实践，不需要真的起一个分布式训练即可完成推理。

#### 4.1.5 小练习与答案

**练习 1**：为什么基类 `TrainRayActor.init` 要设置 `MASTER_ADDR`/`RANK` 等环境变量，而不是直接传参？

**答案**：因为 Megatron/PyTorch 的 `dist.init_process_group` 默认从环境变量读取这些值，slime 把 Ray 分配给每个 actor 的 rank/world_size 显式写入环境，让底层分布式库「无感」地正确初始化。

**练习 2**：`init` 返回的 `start_rollout_id = loaded_rollout_id + 1` 有什么用？

**答案**：它告诉主循环「从已保存检查点的下一轮开始训」，这样断点续训时不会重训已经训过的 rollout。

---

### 4.2 train → train_actor：从数据到权重更新的内部步骤

#### 4.2.1 概念说明

`train` 是工人对外暴露的训练入口，由 `RayTrainGroup.async_train` fan-out 调用（见 u2-l3）。但 `train` 本身很薄，它只做三件事：唤醒（若 offload）、取数据、按角色分流——真正的训练逻辑在 `train_actor`（actor 角色）或 `train_critic`（critic 角色）里。

`train_actor` 是本讲最核心的方法。它要回答一个关键问题：**RL 训练和普通 SFT 不同，loss 依赖于「当前策略」和「参考策略」的对数概率之比，那么这些概率从哪来？**

答案是：`train_actor` 在真正跑梯度更新之前，会先做几次**前向（forward_only）**，分别用 ref/teacher/actor 模型算出参考对数概率，再据此算出优势（advantage），最后才进入带反向传播的训练步（`train`）。

#### 4.2.2 核心流程

`train` 的分流结构：

```
train(rollout_id, rollout_data_ref, external_data)
   ├─ (若 offload_train) wake_up()
   ├─ _get_rollout_data()           # 把 ObjectRef 取回本地 + 搬到 GPU
   ├─ if role == "critic":  train_critic(...)
   │  else:                 train_actor(rollout_id, rollout_data, external_data)
   └─ (若 offload_train) sleep()
```

`train_actor` 内部的步骤（这正是本讲实践任务要梳理的对象）：

```
train_actor(rollout_id, rollout_data, external_data)
   ├─ ① get_data_iterator(rollout_data)                  # data.py：构造微批迭代器
   ├─ ② (可选) fill_routing_replay(...)                  # MoE 路由复现准备
   │
   │   ─── 以下在 with inverse_timer("train_wait"), timer("train") 内 ───
   ├─ ③ 若 compute_advantages_and_returns：
   │     ├─ 若有 "ref" 槽： _switch_model("ref")
   │     │                   → compute_log_prob()          # 调 model.forward_only + loss.get_log_probs_and_entropy
   │     ├─ 若有 "teacher" 槽：_switch_model("teacher")
   │     │                   → compute_log_prob()          # OPD 教师对数概率
   │     ├─ _switch_model("old_actor"/"actor")
   │     ├─ (若不复用) compute_log_prob()                  # 当前 actor 的对数概率
   │     ├─ (若 use_critic) 从 external_data 取 values      # 来自 critic 工人
   │     ├─ _switch_model("actor")
   │     └─ compute_advantages_and_returns(args, rollout_data)  # loss.py：算优势
   ├─ ④ (可选) rollout_data_postprocess(...)
   ├─ ⑤ log_rollout_data(...)                            # data.py：日志
   ├─ ⑥ train(rollout_id, model, optimizer, ...)          # model.py：真正的前后向 + optimizer.step
   ├─ ⑦ prof.step(...)                                    # profiler 记一步
   ├─ ⑧ save_debug_train_data(...)
   ├─ ⑨ weights_backuper.backup("actor")                 # 把新权重存回 CPU 槽
   ├─ ⑩ (可选) weights_backuper.backup("ref")            # 周期性更新参考模型
   └─ ⑪ log_perf_data(...)
```

关键直觉：**步骤 ③ 全是「前向」（`forward_only`），用来准备 loss 需要的量；步骤 ⑥ 才是真正会反向传播更新权重的训练步。** 这两阶段用的是同一个 `train(...)`/`forward_only(...)` 引擎，但回调函数不同。

#### 4.2.3 源码精读

**`train` 的分流**：

[slime/backends/megatron_utils/actor.py:368-388](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L368-L388) 可以看到 `wake_up` → `_get_rollout_data` → 按 `role` 分流 → `sleep` 的结构。`_get_rollout_data` 把 rollout_manager 传来的 `ObjectRef` 经 `process_rollout_data` 取回，并按 DP rank 切出本卡负责的那份数据，再把 token/loss_mask/log_prob 搬到 GPU。

**对比：`train_critic`（理解 actor 的对照）**：

[slime/backends/megatron_utils/actor.py:390-416](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L390-L416) critic 的训练更简单：先用 `forward_only(get_values, ...)`（**loss.py 的 `get_values`**）算当前 value 预测，再 `compute_advantages_and_returns` 算优势，最后设 `loss_type="value_loss"` 跑一次 `train(...)`（**model.py**），并把算好的 values 以 CPU 张量返回。这些 values 会作为 `external_data` 流给 actor（见 `train.py` 第 63-65 行）。

**`train_actor` 的核心** —— 先算 ref 对数概率（KL 约束需要）：

[slime/backends/megatron_utils/actor.py:429-439](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L429-L439) `_switch_model("ref")` 把 ref 权重从 CPU 槽灌进 GPU 骨架，然后 `compute_log_prob(store_prefix="ref_")` 算出 `ref_log_probs` 写回 `rollout_data`。`compute_log_prob` 的实现 [actor.py:350-366](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L350-L366) 只是 `forward_only(get_log_probs_and_entropy, ...)` 的薄封装——即 **model.py 的 `forward_only` 引擎 + loss.py 的 `get_log_probs_and_entropy` 回调**。

接着算 teacher 对数概率（OPD 蒸馏需要）：

[slime/backends/megatron_utils/actor.py:442-452](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L442-L452) 同样的套路，只是切到 `"teacher"` 槽，结果存为 `teacher_log_probs`。

切回 actor，算当前策略对数概率（或复用 rollout 阶段已算好的）：

[slime/backends/megatron_utils/actor.py:454-481](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L454-L481) 这里有个重要的优化 `can_reuse_log_probs_in_loss`：如果满足一系列条件（单微批、纯 policy_loss、无 KL、不使用 rollout logprob 等），可以直接复用，省掉一次前向。否则重新 `compute_log_prob()` 算当前 actor 的对数概率。

> 设计要点：rollout 阶段（u3-l2）其实已经算过一遍 `rollout_log_probs` 并存在 Sample 里。这里之所以「可能要重算」，是因为训练时的数值精度/温度可能与 rollout 时不同。`use_rollout_logprobs=True` 表示信任 rollout 阶段那个值，就不重算。

注入 critic 的 values，然后算优势：

[slime/backends/megatron_utils/actor.py:485-497](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L485-L497) 若用 critic，把 `external_data["values"]`（来自 train_critic 的返回）搬上 GPU；切回 actor；最后调用 **loss.py 的 `compute_advantages_and_returns`** 在 `rollout_data` 上就地（in-place）写入 `advantages` 和 `returns`。

**真正的训练步**：

[slime/backends/megatron_utils/actor.py:508-520](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L508-L520) 调用 **model.py 的 `train(...)`**。它内部会对 rollout 的每个 step 调 `train_one_step`（model.py:509），执行 `zero_grad → forward_backward → grad_norm 检查 → optimizer.step`（这是 u4-l2 的主题）。注意此时 `rollout_data` 里已经备齐了 `log_probs`/`ref_log_probs`/`advantages`/`returns` 等所有 loss 需要的量，`train_one_step` 的 forward 回调会用它们算出 PPO clip loss。

**训练后的收尾**：

[slime/backends/megatron_utils/actor.py:530-541](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L530-L541) `backup("actor")` 把**更新后**的新权重存回 CPU 槽（供 `update_weights` 同步给推理引擎）；若 `ref_update_interval` 周期到了，还会把新 actor 拷一份当新的 ref 模型（即「移动的 KL 参考」）。

**附：`sleep`/`wake_up` 的实现**（offload_train 机制）：

[slime/backends/megatron_utils/actor.py:198-237](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L198-L237) `sleep` 调 `torch_memory_saver.pause()` 把 CUDA 显存页换出 + `destroy_process_groups()` 销毁 NCCL 通信组；`wake_up` 做逆操作：`torch_memory_saver.resume()` + `reload_process_groups()`。这就是 slime 让训练与推理轮流用同一组 GPU（colocate/offload）的底层机制。

#### 4.2.4 代码实践

**实践目标**：完成本讲指定的实践任务——列出 `train_actor` 方法内部的执行步骤顺序，并标注每步调用了 `model.py` / `loss.py` 的哪个函数。

**操作步骤**：

1. 打开 [slime/backends/megatron_utils/actor.py:418](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L418)，从 `def train_actor` 开始逐行读。
2. 对每个函数调用，判断它来自哪个模块。下面的对照表是你要产出的成果（可直接抄到笔记，但请逐行回原文核对）：

| 步骤 | train_actor 中的代码 | 来源模块 / 函数 | 作用 |
|------|----------------------|------------------|------|
| ① | `get_data_iterator(rollout_data)` | `data.py` | 构造微批迭代器 |
| ③a | `_switch_model("ref")` + `compute_log_prob(store_prefix="ref_")` | 内部调 `model.py: forward_only` + `loss.py: get_log_probs_and_entropy` | 算参考策略对数概率 |
| ③b | `_switch_model("teacher")` + `compute_log_prob(store_prefix="teacher_")` | 同上 | 算教师对数概率（OPD） |
| ③c | `compute_log_prob(store_prefix="")` | `model.py: forward_only` + `loss.py: get_log_probs_and_entropy` | 算当前 actor 对数概率（若不复用） |
| ③d | 注入 `external_data["values"]` | （来自 `train_critic` 的 `model.py: forward_only` + `loss.py: get_values`） | 拿到 critic 的 value 预测 |
| ③e | `compute_advantages_and_returns(args, rollout_data)` | `loss.py: compute_advantages_and_returns` | 算优势与回报 |
| ⑥ | `train(rollout_id, model, optimizer, ...)` | `model.py: train` → `train_one_step` | 真正的前后向 + 优化器更新 |

3. **验证小技巧**：在 `train_actor` 第 497 行（`compute_advantages_and_returns` 调用前）和第 512 行（`train(...)` 调用前）各加一行 `logger.info`，打印 `rollout_data.keys()`。你会观察到：训练步前，`rollout_data` 里多了出 `advantages`、`returns` 这两个 key——这正好印证「优势必须先于训练步算好」。

**需要观察的现象**：

- `train_actor` 在调用 `train(...)` 之前，`rollout_data` 必然已包含 `advantages`。
- 若关闭 KL（`kl_coef=0` 且无 ref 槽），步骤 ③a 不会执行。

**预期结果**：你能不看源码，口述「train_actor = 若干次 forward_only（算 ref/teacher/actor 的 logp）→ compute_advantages_and_returns → train」三段式。

> 若本地无 GPU，步骤 3 的日志验证为「待本地验证」；步骤 1-2 的对照表纯源码阅读即可完成。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `compute_advantages_and_returns` 必须在 `train(...)` **之前**调用，而不能在 `train_one_step` 内部边算边用？

**答案**：因为某些 advantage estimator（如 GRPO 的组内归一化、全局白化）需要看到整批数据的统计量（均值、方差），而 `train_one_step` 是按微批（microbatch）跑的，单个微批看不到全局。所以必须先在全量数据上算好 per-token 的 advantage，再分微批进训练步。

**练习 2**：`can_reuse_log_probs_in_loss` 这个优化为什么在 `use_critic=True` 时会被禁用？

**答案**：因为它要求 `not self.args.use_critic`。带 critic 的算法（如 PPO）需要 value 网络，loss 结构更复杂，复用条件不成立；此外复用条件还要求 `loss_type == "policy_loss"`、`kl_coef == 0` 等多项同时满足，任何一项不满足都会落到重算分支，宁可慢一点也要保证正确。

---

### 4.3 update_weights：把训练好的权重推给推理引擎

#### 4.3.1 概念说明

训练完一轮后，新权重只在训练工人的 GPU 里。但下一轮 rollout（采样）要用**新**权重，否则就是 off-policy 了。`update_weights` 就是把训练好的 actor 权重同步到 SGLang 推理引擎的环节（见 u2-l1 的闭环图：权重同步是 training→rollout 单向）。

这个方法本身依然是个「编排者」：它向 rollout_manager 要来可更新的引擎列表，然后委托给 `self.weight_updater`（在 init 阶段选好的具体同步类）去执行真正的传输。具体的传输机制（NCCL / disk / delta）是 U5 的主题，本讲只关注「调用顺序与编排」。

#### 4.3.2 核心流程

```
update_weights()
   ├─ (debug 模式) 直接返回
   ├─ (若 use_fault_tolerance) 让 rollout_manager 恢复可更新引擎
   ├─ ray.get(rollout_manager.get_updatable_engines_and_lock)  # 拿引擎列表 + 锁
   ├─ 判断 reconnect_rollout_engines（offload_train + critic + 非 colocate）
   ├─ 若无引擎且无需重连 → 直接 return
   ├─ wake_up() 或 reload_process_groups()   # 恢复 NCCL 通信
   ├─ 若有新引擎 → weight_updater.connect_rollout_engines(...)  # 建立连接
   ├─ weight_updater.update_weights()          # ★ 真正的权重传输（U5 详解）
   ├─ (若 keep_old_actor) 轮转权重队列
   └─ sleep() 或 destroy_process_groups()      # 再次让出显存
```

#### 4.3.3 源码精读

[slime/backends/megatron_utils/actor.py:570-616](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L570-L616) 这段是 `update_weights` 的主体。注意几个要点：

- **从 rollout_manager 拿引擎**：[L580-L587](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L580-L587) 通过 `ray.get(self.rollout_manager.get_updatable_engines_and_lock.remote())` 取回 SGLang 引擎句柄、引擎锁、各引擎的 GPU 数量/偏移/并行配置。这就是 u2-l3 讲的「actor 向 rollout_manager 要引擎」。
- **判断是否需要重连**：[L589](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L589) `reconnect_rollout_engines` 在「offload_train + 用 critic + 非 colocate」时为真——因为这种配置下 sleep 时会断开引擎 NCCL 连接，需要重连。
- **真正传输**：[L613-L616](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L613-L616) `self.weight_updater.update_weights()` 是核心一行。`weight_updater` 的 `weights_getter` 在 init 阶段被设成 `lambda: self.weights_backuper.get("actor")`，所以它取的就是 train_actor 步骤 ⑨ 里 `backup("actor")` 存下的最新权重。
- **keep_old_actor 队列轮转**：[L618-L627](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L618-L627) 异步训练时维护一个 `rollout_actor → old_actor` 的权重队列，用于 off-policy 修正。

**承上启下：本地门面如何调用它**。回顾 u2-l3，`train.py` 调的是 `actor_model.update_weights()`，它走 `RayTrainGroup.update_weights`：

[slime/ray/actor_group.py:161-172](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/actor_group.py#L161-L172) 对每个工人 `actor.update_weights.remote()` 然后 `ray.get` 同步等待。注意 `_full_disk_weight_update_enabled()` 分支：全量 disk 模式下会走单独的 `_reload_rollout_weights_from_disk` 路径，让引擎直接从磁盘重载而非走 NCCL——这是 U5 会展开的优化。

#### 4.3.4 代码实践

**实践目标**：追踪一次 `update_weights` 调用的完整数据来源，确认「同步给推理引擎的权重 = train_actor 最后 backup 的那个 actor」。

**操作步骤**：

1. 在 [slime/backends/megatron_utils/actor.py:530](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L530)（train_actor 末尾的 `self.weights_backuper.backup("actor")`）旁加注释 `# 此权重将被 update_weights 同步`。
2. 追踪 `weight_updater` 的构造（[actor.py:169-175](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L169-L175)），注意 `weights_getter=lambda: self.weights_backuper.get("actor")`。
3. 跟到 [actor.py:615](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L615) 的 `self.weight_updater.update_weights()`，确认它内部会调用上面那个 `weights_getter` 取到最新 actor 权重。

**需要观察的现象**：

- 数据流链是：`train_one_step 更新 GPU 权重` → `backup("actor") 存 CPU` → `weight_updater.get("actor") 取回` → `传输给 SGLang`。
- 这条链上没有任何地方会取 `ref` 或 `teacher` 槽——同步的永远是 actor。

**预期结果**：你能解释「为什么权重同步是单向 training→rollout」——因为只有 actor 是被优化器更新的真理之源，推理引擎只是它的副本，反向写回会污染优化器状态。

#### 4.3.5 小练习与答案

**练习 1**：`update_weights` 为什么要先 `wake_up()` 或 `reload_process_groups()`，更新完又要 `sleep()` 或 `destroy_process_groups()`？

**答案**：因为权重同步常走 NCCL 通道，需要进程组活着；而 offload_train 模式下训练空闲时进程组是被销毁的（sleep 过）。所以更新前要重建通信，更新完再销毁，把显存让回给推理。

**练习 2**：如果 `--update-weight-transport=disk` 且 `--update-weight-mode=full`，`update_weights` 走的和 NCCL 模式有什么不同？

**答案**：全量 disk 模式下（`_full_disk_weight_update_enabled`），`RayTrainGroup.update_weights` 不会让每个工人各自推权重，而是把权重落盘成一个版本目录，然后由 rollout_manager 让引擎从磁盘重载（`_reload_rollout_weights_from_disk`）。这种模式省 NCCL 带宽，适合大规模集群——细节在 U5。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「全生命周期追踪」：

**任务**：给定一个开启了 `--use-kl`（with_ref）和 `--use-critic` 的训练配置，画出**单张训练 GPU 上、一个 rollout 轮次内** `MegatronTrainRayActor` 的方法调用时序，并标注每步是否涉及 GPU 显存的权重切换。

**提示步骤**：

1. 从 [train.py:49-85](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py#L49-L85) 出发，列出主循环对 actor 的两次调用：`async_train`（第 65 行）和 `update_weights`（第 85 行）。
2. 展开 `async_train` → `RayTrainGroup.async_train`（[actor_group.py:130](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/actor_group.py#L130)）→ 工人 `train`（[actor.py:368](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L368)）→ `train_actor`（[actor.py:418](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L418)）。
3. 在时序图里标出 4 次权重切换：`_switch_model("ref")` → 算 ref logp → `_switch_model("actor")` → 算 actor logp/advantage → `train` 反向更新 → `backup("actor")`。
4. 接上 `update_weights`（[actor.py:571](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L571)）：取 `actor` 槽 → 传给 SGLang。
5. 思考：如果同时开了 `--offload-train`，在 `train` 和 `update_weights` 的首尾各多出哪两个方法？（答：`wake_up` / `sleep`。）

**交付物**：一张时序图（手绘或文字版均可），能体现「GPU 骨架上的活跃权重」随时间在 `actor ↔ ref` 之间切换，最后同步给推理引擎。

## 6. 本讲小结

- **抽象与实现分离**：`TrainRayActor` 定义训练工人的统一契约（`train`/`save_model`/`update_weights`/`sleep`/`wake_up`），`MegatronTrainRayActor` 是当前唯一实现；`RayTrainGroup` 作为本地门面 fan-out 调用，`train.py` 完全不感知后端细节。
- **`init` 是工人的诞生**：建进程组 → Megatron 并行初始化 → 搭模型与优化器 → 上报并行规模 → 备份 actor 权重 → 按 `with_ref`/`with_opd_teacher` 加载辅助模型 → 选 `weight_updater` → 可选 `sleep`。
- **辅助模型复用骨架**：ref/teacher/old_actor 不是独立模型实例，而是用 `weights_backuper` 的标签槽在同一个 GPU 骨架上切换权重，省显存。
- **`train_actor` 是三段式**：① 若干次 `forward_only`（`model.py`）配 `get_log_probs_and_entropy`/`get_values`（`loss.py`）算 ref/teacher/actor 的对数概率 → ② `compute_advantages_and_returns`（`loss.py`）算优势 → ③ `train`（`model.py`）跑真正的反向更新。
- **`update_weights` 闭合回路**：把 `train_actor` 末尾 `backup("actor")` 存的最新权重，经 `weight_updater` 同步给 SGLang 引擎；权重同步永远是 training→rollout 单向。
- **offload 机制**：`sleep`/`wake_up` 用 `torch_memory_saver` + 销毁/重建进程组，让训练与推理轮流占卡。

## 7. 下一步学习建议

本讲只讲了训练工人「怎么被驱动」，但几个关键内部还没展开：

- **u4-l2 train_one_step 与 pipeline 前后向**：深入 `model.py` 的 `train` → `train_one_step`，看 Megatron 流水线前后向、梯度检查与 `optimizer.step` 的细节。
- **u4-l4 RL 损失与优势估计**：深入 `loss.py` 的 `compute_advantages_and_returns` 与 `policy_loss_function`，理解 PPO clip / KL / GRPO 优势白化的数学。
- **U5 权重同步与推理后端**：本讲把 `weight_updater` 当黑盒，U5 会拆开 `UpdateWeightFromTensor`/`FromDisk`/`FromDiskDelta` 的真实传输实现，以及 SGLang 引擎如何接收这些权重。
