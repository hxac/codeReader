# 工作流与执行：driver、workflow 与集群启动

> 本讲对应 MILES 单元第三讲。上一讲（u8-l2）我们已经看过 TOML run 文件如何被 `RunSpec` 「加载即编译」成 `RunConfig`；本讲顺着这条链往下走：**编译产物交给谁、在 GPU 集群上如何被执行、中断之后如何恢复、以及一次 run 何时才算「完成」**。

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出一次 MILES run 的三个阶段（准备 / 训练 / 导出）各自的输入输出，以及哪些阶段在中断后可以复用、哪些必须重算。
2. 解释「指纹（fingerprint）」如何支撑 resume：配置身份如何计算、哪些字段的改动被允许、哪些会被拒绝。
3. 读懂 `driver.train` 的协调循环：rollout 收集 → 优化器更新 → 权重发布 → 存档边界，以及时间停 / debug 退出 / 池耗尽三种提前退出方式。
4. **掌握本轮更新的核心变化**：run 完成判定改用 driver 返回值中的 `num_rollout`；`training.num_epoch` 预算的 run 由原生运行时在数据加载后才解析出 rollout 总数，并通过 `core.save_final_checkpoint` 在最后一次更新时落盘最终存档。
5. 理解 `cluster.Supervisor` 如何在 Beaker 副本内做受监管启动（心跳、健康探针、启动预算）。

## 2. 前置知识

- **run 文件与 RunSpec（承接 u8-l2）**：一份 TOML 描述模型、数据、训练器、推理引擎与集群分配；`RunSpec.compile()` 把结构化小节翻译成 `CoreConfig`（经 `--olmo-core-config` 传入）和原生 MILES 参数（`--num-rollout`、`--num-epoch` 等）。本讲只取其中与「训练预算」和「存档节奏」相关的编译产出。
- **rollout（采样回合）**：GRPO 类算法以「一轮采样 + 若干次优化器更新」为基本节奏。一轮收集 `rollout_batch_size × n_samples_per_prompt` 条回答，`global_batch_size` 决定每次更新消耗多少条。
- **幂等与指纹**：分布式训练会被抢占、被重启。为了让重启后的进程能安全复用上一轮的产物，系统给每个产物记录「身份」（输入的哈希或清单），重启时先核对身份，一致才复用——这就是指纹。
- **Beaker 副本（replica）**：MILES 的多机 run 会被 Beaker 调成多个副本，每个副本在各自节点上跑一个 `cluster` 进程；其中 IP 最小的节点成为 head，只有 head 运行训练 workflow。
- **原生运行时（pinned MILES runtime）**：open-instruct 通过 `--miles` 镜像使用一版钉死的 MILES/Core 运行时。数据集加载、rollout 引擎与优化器执行都发生在那个运行时里；open-instruct 侧的 driver 只做编排。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [open_instruct/miles/execution/workflow.py](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/workflow.py) | 一次 run 的总编排：模型/数据准备、身份记录、run 目录锁、训练调用与完成判定 |
| [open_instruct/miles/execution/driver.py](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/driver.py) | 单次训练尝试的协调循环：rollout、更新、发布、存档、清理，并返回结果字典 |
| [open_instruct/miles/execution/cluster.py](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/cluster.py) | Beaker 副本内的 `Supervisor`：rendezvous、心跳、judge 健康探针、Ray 启动与 workflow 拉起 |
| [open_instruct/miles/execution/timing.py](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/timing.py) | `stage()` 上下文管理器：把 driver 各阶段耗时写成 `driver_timing.jsonl` |
| [open_instruct/miles/configuration/run_spec.py](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/configuration/run_spec.py) | 编译链中与本讲相关的两处：`num_epoch`/`num_rollout` 预算选择、`save_final_checkpoint` 默认值 |
| [open_instruct/miles/configuration/config.py](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/configuration/config.py) | `CoreConfig.save_final_checkpoint` 字段定义与 `num_epoch` 正整数校验 |
| [open_instruct/test_miles_epoch_budget.py](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/test_miles_epoch_budget.py) | epoch 预算编译行为的单测（本讲实践的验证工具） |
| [docs/miles/workflow.md](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/docs/miles/workflow.md) | 研究者视角的工作流文档：plan/validate/train/run/status 与恢复语义 |

## 4. 核心概念与源码讲解

### 4.1 workflow 阶段与指纹恢复

#### 4.1.1 概念说明

`workflow.py` 回答的问题是：**「一份 run 规格，如何变成磁盘上一组可恢复的产物？」** 它把一次 run 划分为三个阶段：

1. **准备（prepare）**：把模型源转换/暂存成 HF 目录、把数据准备成带验证目标的提示集，并给两个产物各写一份「身份清单」。
2. **训练（train）**：把编译好的配置交给 driver（见 4.2）执行。
3. **导出/收尾**：driver 返回结果字典，workflow 据此把 run 状态写成 `complete` 或 `stopped`。

「指纹」是这一切的安全网：run 目录里记录了启动时规格的哈希，重启时新规格必须与记录匹配（或在允许的「运维字段」上等价），否则拒绝恢复，逼你换一个新的 `output.root`。这防止了「换数据续训旧目录」这类静默事故。

#### 4.1.2 核心流程

```text
execute(spec)
  │
  ├─ run_directory(spec)            # 独占锁 + 身份校验 + attempt 记账
  │    ├─ flock(.workflow.lock)     # 多进程互斥
  │    ├─ fingerprint(spec) == 旧 spec_sha256 ?  # 否则按白名单归一化后再比
  │    ├─ 旧 status == complete ? → 拒绝（已完成 run 不可覆盖）
  │    └─ auto_resume=false 且目录已存在 ? → 拒绝
  │
  ├─ 阶段1 准备：prepare_model → prepare_data → 判分注册表校验
  │    （模型/数据产物带身份清单，resume 时直接复用）
  │
  ├─ 阶段2 训练：auto_resume 且有 core-latest.json → 挂载 load；
  │    train_config() → parse_runtime() → driver.train()
  │
  └─ 阶段3 收尾：按结果字典判定 complete / stopped，写 workflow.json
```

指纹的数学定义很直接——对规格字典做「按键排序后的 JSON 序列化」再取 SHA-256：

\[
\mathrm{fingerprint}(d) = \mathrm{SHA256}\big(\mathrm{json\_dumps}(d,\ \mathrm{sort\_keys}=\text{true})\big)
\]

注意 `sort_keys=True` 保证了同一逻辑规格（字典键顺序不同）得到同一指纹。

#### 4.1.3 源码精读

**run 目录的锁与身份校验**。[open_instruct/miles/execution/workflow.py:306-346](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/workflow.py#L306-L346) 是 `run_directory` 上下文管理器：先用 `fcntl.flock` 对 `.workflow.lock` 加非阻塞独占锁（防止两个进程同时写同一 run 目录）；然后计算当前规格的指纹，与 `workflow.json` 里记录的 `spec_sha256` 比对；若旧状态是 `complete` 直接拒绝（完成的 run 不可覆盖）；若 `launch.auto_resume=false` 且目录已有 workflow 也拒绝。任何阶段抛异常时，把状态更新为 `failed` 并记录错误再上抛。

**指纹计算（含 OPD 历史兼容）**。[open_instruct/miles/execution/workflow.py:38-44](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/workflow.py#L38-L44) 在哈希前对 OPD 算法的 run 做了两处剔除（空的 `miles` 覆盖表、`keep_checkpoints`），让历史上可选的字段不改变 run 身份。

**允许 resume 时改动的字段白名单**。[open_instruct/miles/execution/workflow.py:58-97](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/workflow.py#L58-L97) 的 `recovery_configuration_matches`：先严格比对原始哈希；不一致时读取磁盘上的 `run-spec.json` 做认证（记录的规格也要能算出旧指纹，防篡改），然后把**运维性字段**从新规格「领养」进记录再比较——包括 `launch.auto_resume` / `max_retries` / `memory`、编译器缓存各项、`training.save_interval` 与 `evaluation.interval`。换句话说：**恢复策略、缓存位置、存档/评估节奏可以改；模型、数据、训练视野（horizon）、算法配方不能改**。

**重启记账**。[open_instruct/miles/execution/workflow.py:107-128](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/workflow.py#L107-L128) 的 `record_attempt`：Beaker 对被抢占任务会无限重启，一个总在存档前被抢占的 run 会永远空转。每次启动向 `attempts.json` 追加一条记录，超过 `launch.max_retries` 即拒绝再启动（`-1` 关闭上限，且「重试次数」不含首次启动）。

**模型身份清单与准备复用**。[open_instruct/miles/execution/workflow.py:131-149](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/workflow.py#L131-L149) 的 `model_identity` 不对几十 GB 的权重做哈希，而是记录每个文件的路径、大小、纳秒级 mtime，仅对小文件（`.json` / `.jinja` / `.metadata`）算 SHA-256；[open_instruct/miles/execution/workflow.py:179-190](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/workflow.py#L179-L190) 里 `prepare_model` 用目标目录下的 `workflow-model.json` 标记：身份一致且产物清单完整 → 直接复用；身份变化或目录不完整 → 报错要求换新 `output.root`。数据准备（`prepare_data`，见 u8-l4）走同样的「清单 + 标记」思路。

**三阶段总编排**。[open_instruct/miles/execution/workflow.py:349-426](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/workflow.py#L349-L426) 的 `execute`：

- L352-353 `prepare_model`（阶段一·模型），L356-378 `prepare_data` + 判分注册表校验（阶段一·数据）；
- L396-398：`auto_resume=true` 且 checkpoint 目录存在 `core-latest.json` 时，把它挂到 `config.miles["load"]`，实现「从最新完成的 Core checkpoint 续训」；
- L399-403：写 `resolved-plan.json`、发 ad­visories，状态改为 `training`；
- L404-412：调用 `train_config`（内含 `parse_runtime` 与 `driver.train`，见 4.2/4.3）；
- L413-423：完成判定（本讲更新重点，见 4.3.3）。

**训练前的运行时解析**。[open_instruct/miles/execution/workflow.py:267-303](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/workflow.py#L267-L303)：`parse_runtime` 临时替换 `sys.argv` 调用原生 MILES 的 `parse_args`，并要求后端必须是 `olmo_core`；若带 `--load` 恢复，则从 checkpoint 的 resume manifest 里取出 `clock.next_rollout_id` 作为 `start_rollout_id`——这就是「rollout 游标不重不漏」的来源。

#### 4.1.4 代码实践

**实践目标**：不改任何代码，梳理一次 run 会在 `output.root` 里留下哪些文件，并验证 CPU 侧的配置检查可用。

1. 通读 `execute`（上文链接），把下表填完整（参考要点见第 5 节综合实践的任务 A）：

   | 文件 | 写入者（函数/行号） | 阶段 | resume 时的用途 |
   | --- | --- | --- | --- |
   | `run-spec.json` | ? | ? | ? |
   | `plan.json` / `resolved-plan.json` | ? | ? | ? |
   | `workflow.json` | ? | ? | ? |
   | `attempts.json` | ? | ? | ? |
   | `prepared/`、`checkpoints/`、`inference-profile.json` | ? | ? | ? |

2. 在仓库根目录运行（CPU 即可，不需要 GPU/WEKA）：

   ```bash
   python -m open_instruct.miles plan configs/miles/examples/dev.toml
   python -m open_instruct.miles validate configs/miles/examples/dev.toml
   ```

3. **需要观察的现象**：`plan` 打印解析后的 argv、批几何与放置；`validate` 只做 CPU 侧检查，不证明显存可容纳（见 [docs/miles/workflow.md:33-38](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/docs/miles/workflow.md#L33-L38)）。
4. **预期结果**：两条命令都以退出码 0 结束；`plan` 输出里能看到 `--num-rollout`（dev.toml 用显式 rollout 预算，而非 epoch 预算）。
5. 本地未实际运行以上命令，具体输出格式**待本地验证**（行为依据源码与文档）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `recovery_configuration_matches` 允许改 `training.save_interval`，却不允许改 `training.num_epoch`？
**答案**：`save_interval` 只影响「未来产物的节奏」，不改变已经训出的权重语义；而 `num_epoch` 属于训练视野（horizon）——改了它等于改变这次 run 要训多少数据，与目录里已记录的指纹身份冲突。源码注释明确写了「Keep training horizon, evaluation tasks/scorers, and all recipe fields strict」（[workflow.py:86-88](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/workflow.py#L86-L88)）。

**练习 2**：一个 run 在第 3 次被抢占后重启，`launch.max_retries=2`，会发生什么？
**答案**：`record_attempt` 读到 `attempts.json` 已有 3 条记录，`len(attempts) > limit`（3 > 2）成立，抛出 `InputError`，提示要么调大 `launch.max_retries` 要么换新 `output.root`（[workflow.py:119-125](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/workflow.py#L119-L125)）。注意上限按「重启次数」计，首次启动不会被拒。

**练习 3**：为什么 `model_identity` 不对 safetensors 权重整体做 SHA-256？
**答案**：权重动辄几十 GB，每次启动都全量哈希会把恢复拖慢到不可用。它退而记录（路径, 大小, mtime_ns）三元组，只对小体积的配置/模板文件做内容哈希——以极小概率漏检「同大小同时间的权重替换」换取可用的启动速度（[workflow.py:131-149](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/workflow.py#L131-L149)，函数 docstring 自述 "without hashing tens of GB"）。

### 4.2 driver 协调循环

#### 4.2.1 概念说明

`driver.py` 的 `train()` 是**一次训练尝试（attempt）**的协调者。它自己不执行模型前向、不搬运权重——那些属于钉死的 MILES 运行时（SGLang 推理引擎 + OLMo-core 训练器）。driver 的职责是把各角色连起来并在正确的时机放置边界：

- rollout 收集（推理引擎按当前策略采样）；
- 优化器更新（训练器消费一个 batch）；
- 权重发布（把训练器的新权重推给推理引擎，支持 barrier / refresh / engine-drain 三种模式）；
- 存档边界（周期存档、最终存档、哨兵文件触发）；
- 评估与后台评估的分发；
- 无论成功失败都要做的有序清理。

#### 4.2.2 核心流程

`driver.train` 的主干可以概括成下面的伪代码（省略 async 细节）：

```text
train(args):
    解析时间预算 max_run_seconds → deadline；判定时间停是否可恢复(resumable)
    启动 Ray → 初始化 W&B → 启动 worker manager
    （colocate/offload 时序约束下）启动 serving 与 trainer；拿到 rollouts_per_epoch
    首次权重发布 publish()；等待 judge 就绪
    for rollout_id in range(start_rollout_id, num_rollout):     # ← 主循环
        batch = manager.get(rollout_id)            # 池耗尽则存档并 break
        learner.train(rollout_id, batch)           # 优化器更新
        completed.append(rollout_id)
        若满足存档条件: manager.save + learner.save_model + finalize_checkpoint
        若 refresh 模式 / 到更新间隔: publish(rollout_id)
        若到评估间隔: evaluation.dispatch(rollout_id)
        若到时间预算: break（resumable 与否决定 final_stop）
        若 debug_exit_after_rollout 达到: break
    收尾：评估 drain、最终 HF 导出、后台评估 final 分发
    finally：按序清理（prepared batch → 生成停止 → 权重传输关闭 → 各角色 dispose）
    return {completed_rollout_ids, num_rollout, pools_exhausted, ...}
```

存档条件的判定（对应 [driver.py:299-317](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/driver.py#L299-L317)）是四项的析取：

\[
\text{save} \iff (\text{到时且可存}) \ \lor\ \text{sentinel} \ \lor\ (\text{save\_final\_checkpoint} \land \text{最后一个 rollout}) \ \lor\ \text{周期 interval 到达}
\]

#### 4.2.3 源码精读

**入口与预算**。[open_instruct/miles/execution/driver.py:39-59](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/driver.py#L39-L59)：`train(args, *, export_hf=None)` 先校验 Ray 后端、检查命名池（`prompt_provider=pools`）需要配套运行时能力；然后把 `max_run_seconds` 换算成单调钟 deadline，并判定时间停是否可恢复——`resumable = not max_run_final and bool(save)`：**有存档路径且未声明「到时即终局」的时间停，等价于一次可恢复的中断，而不是 run 完成**。

**启动：serving 与 trainer 的顺序约束**。[open_instruct/miles/execution/driver.py:95-138](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/driver.py#L95-L138)：默认先 `create_rollout_components`（serving，顺带拿到 `rollouts_per_epoch`——epoch 派生的节奏需要先知道 rollout 数据集长度），再建训练模型；只有「非 colocate、非 offload、且 `args.num_rollout is not None`」时才允许两者并行启动。**注意 L100 那个 `args.num_rollout is not None` 检查：epoch 预算的 run 在启动时 `num_rollout` 还未解析出来（由原生运行时在数据加载后填上），所以并行启动被自动关闭**（这是 4.3 更新点在 driver 侧的一个伏笔）。

**主循环**。[open_instruct/miles/execution/driver.py:231-291](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/driver.py#L231-L291)：

- L233-255 取 batch；若命名池耗尽（`PromptPoolExhausted`），记录 `exhausted = final_stop = True`，若已有完成集则补一次存档再 `break`——**池耗尽是正常完成**；
- L272-273 `learner.train(rollout_id, batch)` 是唯一推动参数前进的调用；
- L274-277 命名池模式下提交曝光计数并打点 pool 指标（配合 u8-l5 的排空机制）；
- L283-287 后台评估模式下，按「每次 collection 产生的优化器步数」收集已完成的导出快照。

**存档边界（本轮更新之一）**。[open_instruct/miles/execution/driver.py:299-317](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/driver.py#L299-L317)：四项存档触发里，新增的是 **L304** 的 `save_final_checkpoint and rollout_id == args.num_rollout - 1`——epoch 预算的 run 没有周期 `save_interval`，靠这一项在最后一个 rollout 把最终权重落盘。存档动作固定三步：`manager.save`（rollout 游标 + 待处理提示账本在同一把锁下快照）、`learner.save_model(force_sync=True)`、`learner.finalize_checkpoint`。注释强调：已完成但未消费、以及 in-flight 的组都会在 resume 时再生，推理无需暂停。

**提前退出的三种路径与最终导出**。[open_instruct/miles/execution/driver.py:326-355](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/driver.py#L326-L355)：时间到 → 若不可恢复则 `final_stop=True`；`debug_exit_after_rollout` 达到 → break（注释明确：debug 停是「成功的执行」而非「完成的 run」，给 resume 留出目录）。L347 的 `reached_end = (completed[-1] + 1 ...) == args.num_rollout` 与 workflow 侧完成判定同构；L351-355 只有 `reached_end or final_stop`（且不是「零更新的池耗尽」）才做最终 HF 导出。

**有序清理**。[open_instruct/miles/execution/driver.py:373-429](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/driver.py#L373-L429)：`finally` 块里按表驱动顺序清理（先取消 prepared batch，再停止生成，再关权重传输，最后各角色 dispose），每步都有独立超时且失败不吞掉前面的错误；W&B 在失败时以退出码 1 显式收尾。

**结果字典（本轮更新之二）**。[open_instruct/miles/execution/driver.py:430-438](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/driver.py#L430-L438)：`train` 返回的字典现在包含 `"num_rollout": args.num_rollout`——把**原生运行时解析后的 rollout 总数**带回给 workflow。旧版只有 `completed_rollout_ids` / `pools_exhausted` / `start_rollout_id` / `stopped_for_time` 等字段。

#### 4.2.4 代码实践

**实践目标**：不改代码，通过 `stage()` 计时点在脑中「重放」一次 driver 的执行轨迹，理解每个阶段会留下什么证据。

1. 阅读 [open_instruct/miles/execution/timing.py:16-42](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/timing.py#L16-L42)：每个 `stage(args, name)` 把一条 JSONL 追加到 `<save>/driver_timing.jsonl`。
2. 在 `driver.py` 里全局搜索 `stage(args,`，按出现顺序抄下阶段名。启动期为：`startup_cache_prepare` → `ray_readiness_wait` → `placement` → `serving_startup` → `trainer_startup` → `initial_publication` → `judge_readiness_wait`（engine_drain 模式另有 `engine_drain_startup`）；每个 rollout 期为：`generation_wait` → `training` →（命中时）`checkpoint` / `publication` / `evaluation`；收尾期为 `final_hf_export` 与七个 `*_dispose` 清理阶段。
3. **需要观察的现象**（如果有真实 run 的 WEKA 目录）：`driver_timing.jsonl` 中 `generation_wait` 的秒数在 async 模式下是「driver 等待时间」，与 GPU 实际生成时间并不相等（[timing.py:1-6](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/timing.py#L1-L6) 的模块 docstring 专门解释了这一点）。
4. **预期结果**：你能画出一行 rollout 对应的 JSONL 序列：`generation_wait(rollout_id=k)` → `training(rollout_id=k)` → 每逢 `save_interval` 出现 `checkpoint(rollout_id=k)`。
5. 无 GPU 环境时这是一次源码阅读型实践；真实轨迹**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：时间停（`max_run_seconds` 到达）什么时候算 run 完成、什么时候算可恢复中断？
**答案**：driver L58 `resumable = not max_run_final and bool(save)`。有存档路径且未设 `max_run_final` 时，时间停保存后 break，返回 `stopped_for_time=True, time_stop_resumable=True`，workflow 判定为 `stopped`（可 resume）；反之 `final_stop=True`，会做最终导出，workflow 判定为 `complete`。

**练习 2**：为什么 debug 退出（`debug_exit_after_rollout`）不触发最终 HF 导出？
**答案**：L344-346 注释说明：debug 停留下的是「可恢复的 workflow」；如果第一个进程创建了最终导出目录，被 resume 的第二个进程就无法把自己的新权重导到同一目录（导出器是独占的）。所以导出条件是 `reached_end or final_stop`，debug 停两者都不满足。

**练习 3**：命名池耗尽发生在第 0 个 rollout 之前（`completed` 为空、`start_rollout_id=0`），run 会怎样？
**答案**：这是「零更新的耗尽」：`exhausted=True` 使 workflow 判定 complete（成功），但 `untrained_exhaustion=True` 使 driver 跳过最终 HF 导出——没有训过的权重不值得导出（[driver.py:348-355](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/driver.py#L348-L355)）。

### 4.3 完成判定与最终存档：epoch 预算如何被判定为 complete（本轮更新重点）

#### 4.3.1 概念说明

本轮代码更新（896a734 → bae4209）引入了 **epoch 预算**：研究者可以用 `training.num_epoch = N` 声明「把数据训 N 遍」，而不必先算出这对应多少个 rollout。这带来两个必须解决的工程问题：

1. **完成判定**：workflow 之前拿 `config.miles["num_rollout"]` 与完成计数比对。epoch 预算的编译产物里**故意没有** `num_rollout`（数据集没加载，总数未知），旧的比较永远比不出「到达终点」。解法：原生运行时在数据加载后把解析出的总数写回 `args.num_rollout`，driver 在返回字典里带上它，workflow 优先使用返回值。
2. **最终存档**：rollout 预算的 run 靠 `save_interval`（默认等于 `num_rollout`，即只在结尾存一次）拿到最终权重；epoch 预算没有这个锚点，于是新增 `core.save_final_checkpoint`，由编译器默认置真，driver 在最后一个 rollout 落盘。

#### 4.3.2 核心流程

```text
TOML [training] num_epoch=2
   │ run_spec.py L551 兜底 put("miles.num_epoch")   （num_epoch 不在 FIELD_MAP/CORE_FIELDS）
   ▼
miles.num_epoch=2，且 miles.num_rollout 不存在
   │ L735-739：两者同设 → InputError；只设 epoch → 不再默认补 num_rollout=100
   ▼
L780-787：save_checkpoints=true（默认）且无 num_rollout
   → core.save_final_checkpoint = True（不发明周期 save_interval）
   ▼
RunConfig.arguments()：--num-epoch 2（无 --num-rollout）；
save_final_checkpoint 随 --olmo-core-config JSON 进入 args.olmo_core
   ▼
原生运行时加载数据集 → 解析 num_epoch 为 num_rollout（写入 args.num_rollout）
   ▼
driver 主循环跑满 num_rollout：L304 在最后一个 rollout 存档
   ▼
driver 返回 {"num_rollout": args.num_rollout, ...}
   ▼
workflow.execute：completed[-1]+1 == result["num_rollout"] → status=complete
```

#### 4.3.3 源码精读

**预算二选一**。[open_instruct/miles/configuration/run_spec.py:735-739](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/configuration/run_spec.py#L735-L739)：`num_epoch` 与 `num_rollout` 同时设置直接 `InputError`（"Choose num_epoch or num_rollouts; MILES would otherwise ignore num_epoch"——防止用户以为 epoch 生效实际被忽略）；只设 `num_epoch` 时**不再**补默认 `num_rollout=100`（旧版这个默认值无条件写入，正是 epoch 预算此前不可用的原因）。

**存档节奏的编译**。[open_instruct/miles/configuration/run_spec.py:780-792](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/configuration/run_spec.py#L780-L792)：`save_checkpoints=true` 时——有 `num_rollout` 则 `save_interval` 默认取它（只在结尾存）；否则（epoch 预算）`core.setdefault("save_final_checkpoint", True)`，注释写明「Native MILES resolves the epoch budget after loading the dataset. Save the final update without inventing a periodic cadence here.」`save_checkpoints=false` 与显式 `save_interval` 冲突报错；与 `max_run_seconds` 组合则把时间停标记为终局（`max_run_final=True`，无可恢复之物）。

**新 CoreConfig 字段**。[open_instruct/miles/configuration/config.py:72-73](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/configuration/config.py#L72-L73)：`save_final_checkpoint: bool = False`，注释定位为「epoch 派生预算、无周期存档节奏时的 driver 策略」；它在 [config.py:226-243](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/configuration/config.py#L226-L243) 的布尔字段校验表中，并经 [config.py:801-822](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/configuration/config.py#L801-L822) 的 `arguments()` 随 `--olmo-core-config` 的 JSON 传给运行时，最终出现在 driver 眼中的 `args.olmo_core.save_final_checkpoint`。`num_epoch` 的正整数校验在 [config.py:635-637](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/configuration/config.py#L635-L637)（`0`、负数、`true` 都被拒——注意 Python 里 `True` 是 `int`，所以这里显式检查 `type(...) is not int`）。

**driver 侧的最终存档与返回值**（已在 4.2.3 展开，这里只点名）：[driver.py:304](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/driver.py#L304) 的第三个存档触发条件与 [driver.py:430-438](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/driver.py#L430-L438) 返回字典中的 `num_rollout`。

**workflow 的完成判定**。[open_instruct/miles/execution/workflow.py:413-423](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/workflow.py#L413-L423)：

```python
completed = result.get("completed_rollout_ids") if result else None
complete = (
    completed is None
    or bool(result.get("pools_exhausted"))
    or (bool(result.get("stopped_for_time")) and not result.get("time_stop_resumable"))
    or (
        (completed[-1] + 1 if completed else result["start_rollout_id"])
        == result.get("num_rollout", config.miles.get("num_rollout"))
    )
)
```

四个条件的语义：① driver 没有报告完成清单（视为完成，兼容旧返回）；② 命名池耗尽（数据用尽即目标达成）；③ 不可恢复的时间停（终局停）；④ 完成计数 +1 达到总数。**关键变化在第④项右侧**：`result.get("num_rollout", config.miles.get("num_rollout"))`——driver 返回的、由原生运行时在数据加载后解析出的总数优先；编译配置里的 `num_rollout` 只是旧运行时的回退。epoch 预算 run 正是靠第④项右侧的新值被正确判为 complete。判定后 [workflow.py:424-425](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/workflow.py#L424-L425) 把 `complete`/`stopped` 与整个结果字典写进 `workflow.json`——而 4.1 里「已 complete 的 run 不可重启」正是消费这个状态。

**测试佐证**。[open_instruct/test_miles_epoch_budget.py:10-36](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/test_miles_epoch_budget.py#L10-L36) 是这条链的验收：`num_epoch=2` 编译后 `miles["num_epoch"]==2` 且**没有** `num_rollout`；argv 含 `--num-epoch` 不含 `--num-rollout`；`compiled.core.save_final_checkpoint` 为真且无 `save_interval`；`training.num_rollouts` 与 `num_epoch` 并存、`num_epoch ∈ {0,-1,True}` 均抛 `ValueError`；`save_checkpoints=false` 时 `save_final_checkpoint` 不再默认为真。

#### 4.3.4 代码实践

**实践目标**：亲手跑通 epoch 预算的编译与校验行为，并用单测固化理解。

1. 运行单测（CPU 即可，测试只依赖 `configuration` 包）：

   ```bash
   uv run pytest open_instruct/test_miles_epoch_budget.py -v
   ```

2. 用 `--set` 在示例配置上做三组实验（`plan` 不分配 GPU、不要求路径存在）：

   ```bash
   # a) epoch 预算：应编译成功，argv 出现 --num-epoch，无 --num-rollout
   python -m open_instruct.miles plan configs/miles/examples/dev.toml --set training.num_epoch=2

   # b) 预算冲突：应报 "Choose num_epoch or num_rollouts"
   python -m open_instruct.miles plan configs/miles/examples/dev.toml \
     --set training.num_epoch=2 --set training.num_rollouts=20

   # c) 非法值：应报 num_epoch 相关校验错误（注意 0/-1/true 都非法）
   python -m open_instruct.miles plan configs/miles/examples/dev.toml --set training.num_epoch=0
   ```

3. **需要观察的现象**：实验 a 的 plan 输出里 `resolved-plan`/argv 段出现 `--num-epoch 2`，且 checkpoint 计划不再依赖周期 `save_interval`；实验 b、c 退出码为 2 并给出可行动的错误信息（错误分类见 u8-l2）。
4. **预期结果**：与 `test_miles_epoch_budget.py` 的断言一一对应（见上文「测试佐证」）。
5. 以上命令本地未实际执行，输出细节**待本地验证**；单测的通过与否可直接作为判据。

#### 4.3.5 小练习与答案

**练习 1**：为什么 workflow 不干脆在编译时把 `num_epoch` 换算成 `num_rollout`？
**答案**：换算需要知道 rollout 数据集的长度（每个 epoch 含多少个 rollout），而数据集在**准备/加载之后**才知道大小——编译发生在 CPU 提交机上，连数据都可能没挂载（`plan` 阶段不要求路径存在）。源码注释直接陈述了这个分工：「Native MILES resolves the epoch budget after loading the dataset」（[run_spec.py:785](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/configuration/run_spec.py#L785)）。

**练习 2**：一个 epoch 预算 run 在最后一个 rollout 保存了最终权重。如果磁盘上这个存档损坏，resume 会发生什么？
**答案**：`execute` 只在 `core-latest.json` 存在时才挂载 `load`（[workflow.py:396-398](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/workflow.py#L396-L398)）；损坏的存档在原生运行时加载时报错，本次 attempt 失败、状态记为 `failed`。由于完成状态依赖 driver 实际跑完（而不是存档存在），它不会被误判为 complete。

**练习 3**：`result.get("num_rollout", config.miles.get("num_rollout"))` 为什么要保留后面的回退项？
**答案**：driver 与 workflow 可能来自不同版本的运行时（例如旧镜像里 driver 的返回字典还没有 `num_rollout` 字段）。此时回退到编译配置里的 `num_rollout`——rollout 预算 run 的值编译时已确定，行为与旧版完全一致。这是「新字段优先、旧字段兜底」的兼容式演进。

### 4.4 Supervisor 启动监管

#### 4.4.1 概念说明

`cluster.py` 回答的问题是：**「一个 Beaker 副本内，如何把 Ray、judge 服务和训练 workflow 安全地拉起来，并在任何一方挂掉时让全体有序失败？」** `Supervisor` 是每个副本各一个的监管线程 + 子进程管理者，它提供四样东西：

- **启动预算（startup budget）**：rank 0 发布一个统一的 deadline，所有后续启动工作（inventory、judge、Ray、workflow）共享这一个时钟；
- **心跳**：每 2 秒写 `heartbeat-{rank}.json` 并检查对端心跳，超时即失败——这是「对端节点消失」的检测器；
- **子进程监管**：被监管子进程非零退出、judge 连续 3 次健康探针失败，都会让本副本失败；
- **失败可见化**：失败写 `failed-{rank}.json`，对端在 `check()` 里发现即跟着失败。

#### 4.4.2 核心流程

```text
run(config)                                  # 每个 Beaker 副本一个进程
  ├─ 编译 + topology.plan；校验副本数/排名与提交一致
  ├─ rendezvous.join(output.root/cluster/<attempt>, rank, count)
  │    → 每次启动进入全新 attempt 目录（旧的 attempt 留档）
  ├─ Supervisor(...)：rank0 写 startup.json（唯一 deadline）；其余副本等它出现
  ├─ 心跳线程启动；SIGTERM/SIGINT → 抛错（让清理路径执行）
  ├─ 各副本写 node-{rank}.json → 等全部到齐 → topology.assign 得到本机分工
  ├─ GPU inventory 校验（可见设备数 == 计划数）；rollout GPU 还要过 profile 硬件校验
  ├─ 按分工起 judge 服务（各占自己的 GPU），端点写盘；head 额外占 3 个 Ray 端口
  ├─ ray start --block（head 带 --head，worker 带 --address）
  └─ head：start_workflow（ray-readiness/judge-readiness 两个探针任务 + driver 子进程）
     其余：等 complete.json → 各写 done-{rank}.json → head 收到全部 done 后收尾
```

#### 4.4.3 源码精读

**Supervisor 的构造与启动预算**。[open_instruct/miles/execution/cluster.py:80-107](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/cluster.py#L80-L107)：rank 0 把 `deadline = now + startup_timeout` 写进 `startup.json`，其余副本轮询该文件直到出现（超时即失败）——**整个集群对「启动要多久」只有一个预算**，避免各副本各算各的。

**心跳线程**。[open_instruct/miles/execution/cluster.py:109-154](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/cluster.py#L109-L154)：每 2 秒写自己的心跳、检查对端心跳与子进程、按需探针 judge 健康。`check_peer_heartbeats` 有两个值得注意的细节：共享文件系统上的瞬时缺文件不算对端死亡（保留最后一次观测值，但不延长其期限）；心跳超过 `heartbeat_timeout` 即抛错。被监管子进程退出码非零同样抛错——但仅当 `complete.json` 还不存在（训练完成后的退出不是故障）。

**judge 健康探针**。[open_instruct/miles/execution/cluster.py:156-184](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/cluster.py#L156-L184)：judge 服务在真正 HTTP 200 之前只算「待就绪」并受启动 deadline 约束；进入稳态后每次探针失败计一次，**连续 3 次**失败才判死——容忍单次网络抖动。

**check / wait**。[open_instruct/miles/execution/cluster.py:195-227](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/cluster.py#L195-L227)：`check()` 汇总所有失败源（自身线程异常、startup 任务退出、对端 `failed-*.json`）并转发子进程日志；`wait()` 在 deadline 内轮询谓词，每 30 秒打一条「还在等」的警告。注意 L206-209 的注释：**显式 timeout 只用于训练后的完成等待**——训练阶段不受启动 deadline 约束（那会误杀长训练），由 Beaker 的 run timeout 与心跳兜底。

**run：从 rendezvous 到 workflow**。[open_instruct/miles/execution/cluster.py:270-411](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/cluster.py#L270-L411)：

- L283-289：以 `OI_MILES_LAUNCH_ID` 为 attempt 标识 `rendezvous.join` 到全新目录；
- L301-306：等所有 `node-{rank}.json` 到齐后由 `topology.assign` 定分工（head 取 IP 最小者）；
- L307-317：本机 GPU inventory 必须与 Beaker 分配一致，rollout GPU 还要过推理 profile 的硬件校验；
- L331-349：judge 服务逐个拉起（`SGLANG_EXTERNAL_MODEL_PACKAGE` 被特意移除——judge 是标准 Qwen 模型而非 Olmo 扩展），端点写入 `judge-{name}.json` 并汇入注册表环境变量；
- L357-392：`ray start --block`，head 与 worker 参数不同；
- L394-405：**只有 head 调用 `start_workflow` 并写 `complete.json`**；其余副本轮询 `complete.json`，最后全体各写 `done-{rank}.json`，head 等全部 done 再停 Ray——保证没有副本在 head 收尾时还在用 Ray。

**start_workflow：准备与就绪解耦**。[open_instruct/miles/execution/cluster.py:414-427](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/cluster.py#L414-L427)：head 用 `start_task` 拉起两个**有限期**探针子进程（`--probe` 查 Ray 布局、`--probe-judges` 查 judge 就绪与 canary，见 [cluster.py:430-470](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/cluster.py#L430-L470)），同时用 `run_child` 运行 `python -m open_instruct.miles train <config>`——即 4.1/4.2 的 workflow+driver。driver 在 `judge_readiness_wait` 阶段（[driver.py:188-189](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/driver.py#L188-L189)）等这些探针放行，期间可以并行做模型/数据准备。

#### 4.4.4 代码实践

**实践目标**：源码阅读型实践——推演「一个 worker 副本心跳超时」的故障传播路径。

1. 假设副本 1 的节点在训练中途失联。从 [cluster.py:125-154](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/cluster.py#L125-L154) 出发，写出传播链。
2. 再回答：head 的 driver 此刻在做什么？它能否感知？（提示：driver 的 `ray_readiness_wait` 只在启动期；训练期对端失联靠谁发现？）
3. **需要观察的现象**（纸上推演）：心跳线程抛 `RuntimeError("Replica 1 heartbeat expired")` → `self.failed` 被置位 → 主循环下一次 `supervisor.check()` 抛出 → `run` 的 `except` 写 `failed-0.json` → 对端（如果还活着）的 `check()` 读到 `failed-*.json` 也失败 → Beaker 层面任务失败，触发重试（`auto_resume` 时回到 4.1 的指纹恢复流程）。
4. **预期结果**：任何单副本失联都会在 `heartbeat_timeout` 内让全体副本失败，且 `cluster/<attempt>/` 下留下 `failed-{rank}.json` 与心跳文件作为尸检证据。
5. 无集群环境，属源码阅读型实践，结论**待本地验证**（可对照 `docs/miles/workflow.md` 的 "A killed process may leave the last recorded workflow state" 一节，[docs/miles/workflow.md:72-79](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/docs/miles/workflow.md#L72-L79)）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 startup deadline 由 rank 0 统一发布，而不是各副本各自计算？
**答案**：各副本启动时刻略有先后，若各自算 deadline，先启动的先到期，会出现「A 认为 B 超时而 B 认为自己还有时间」的分歧。统一预算（[cluster.py:84-93](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/cluster.py#L84-L93)）让所有副本对「启动窗口何时关闭」有一致视图。

**练习 2**：`start_task` 与 `start(..., monitor=True)` 有什么区别？为什么 ray-readiness 用前者、driver 用后者（`run_child`）？
**答案**：`monitor=True` 的子进程一旦非零退出就被视为崩溃（[cluster.py:238-240](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/cluster.py#L238-L240)）；`start_task` 专门承载「有限期的启动子进程」，其退出码由 `check()` 单独汇报（[cluster.py:199-201](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/cluster.py#L199-L201)）。driver 走 `run_child`（不进 children 列表、逐秒 `check()`）：driver 的失败要有自己的清理与失败记录路径，而不是被当作「被监管子进程崩溃」一刀切。

**练习 3**：driver 为什么要专门 `await startup.wait_ready("judges")`，而不是在起 judge 的地方同步等待？
**答案**：准备模型/数据是 CPU+IO 工作，与 judge 加载（占自己的 GPU）互不依赖。cluster 侧用探针子进程并行检测就绪（[cluster.py:422-425](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/cluster.py#L422-L425)），driver 侧在首次发布后才等 judge（[driver.py:186-189](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/driver.py#L186-L189)，注释："GPU initialization and initial publication can finish while qualification runs"），把串行等待压缩成并行流水。

## 5. 综合实践

把本讲四个模块串成一份「run 生命周期 Runbook」。产物是一份 Markdown 笔记，包含三部分：

**任务 A：三阶段输入输出表**。对照 `execute`（[workflow.py:349-426](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/workflow.py#L349-L426)）与 [docs/miles/workflow.md](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/docs/miles/workflow.md)，填写：

| 阶段 | 输入 | 输出（写到哪里） | 中断后可否复用 | 判据 |
| --- | --- | --- | --- | --- |
| 模型准备 | `model.source`（HF 或 native） | `<conversion.hf_output>` + `workflow-model.json` | 身份一致即复用 | `model_identity` 清单 |
| 数据准备 | `data` + HF 目录 | `prepared/data/` + manifest | （结合 u8-l4 的清单与标记） | ? |
| 训练 | `RunConfig` + resume checkpoint | `checkpoints/`、`rollouts/`、`workflow.json` | 从 `core-latest.json` 续训 | 指纹 + `clock.next_rollout_id` |

并用自己的话回答：**中断后哪些阶段可跳过、哪些必须重算？**（要点：指纹匹配 + 产物标记完整 → 模型/数据直接复用；训练阶段从最新 checkpoint 恢复，未消费的提示组再生。）

**任务 B：完成判定追踪**。写一份 200 字左右的说明：epoch 预算 run（无显式 `num_rollouts`）如何被判定为 complete。必须覆盖四个数据点：编译期不补 `num_rollout` 默认值、`save_final_checkpoint` 的默认链、driver 返回字典里的 `num_rollout` 从哪来、workflow L414-422 四个析取条件各对应什么场景。

**任务 C：验证**。运行：

```bash
uv run pytest open_instruct/test_miles_epoch_budget.py -v
```

把每条断言映射回任务 B 说明中的一个数据点（例如 `assert "save_interval" not in compiled.miles` ↔ 「不发明周期存档节奏」）。若断言失败，回到 4.3.3 对照源码找原因。

## 6. 本讲小结

- `workflow.execute` 把一次 run 分成准备/训练/收尾三阶段；run 身份 = 规格字典排序 JSON 的 SHA-256，resume 时只有运维字段（重试、缓存、存档/评估节奏）允许变动，模型/数据/训练视野必须严格一致。
- `driver.train` 是单次尝试的协调循环：取 batch → 优化器更新 → 存档边界 → 权重发布 → 评估分发；池耗尽、不可恢复的时间停、跑满预算三种情况算完成，debug 退出与可恢复时间停只算 stopped。
- **本轮更新**：完成判定的右侧从 `config.miles["num_rollout"]` 改为 `result.get("num_rollout", ...)`——epoch 预算的 rollout 总数由原生运行时在数据加载后解析，driver 把它带回；`training.num_epoch` 与 `num_rollouts` 互斥，编译期不再补默认 100。
- **本轮更新**：`core.save_final_checkpoint`（新 CoreConfig 字段）在 epoch 预算 + `save_checkpoints=true` 时默认置真，driver 在最后一个 rollout 落盘最终存档，替代缺失的周期 `save_interval`。
- `cluster.Supervisor` 用统一启动预算、双贴心跳（写自己 + 查对端）、judge 三连失败判死和失败文件传播，把「多副本任何一个失联」变成全体有序失败，再交给 Beaker 重试与指纹恢复闭环。

## 7. 下一步学习建议

- **u8-l4（数据源、混合、超长排除与源准备）**：本讲把 `prepare_data` 当黑盒，下一讲拆开它——mixture 混合、验证目标绑定与 `data.over_length` 排除策略。
- **u8-l11（历史优先级提示采样与 epoch 预算）**：本轮更新同属一个 PR 系列的 `priority` 提示提供者，与 epoch 预算共享「组序遍历 + 确定性洗牌」语义。
- **u8-l8（启动、提交与运维）**：本讲的 `cluster.run` 之上还有 launch/submit 把 run 编译成 Beaker 实验；`status` 与回执目录是运维视角的完成判据。
- **源码延伸**：[open_instruct/miles/execution/rendezvous.py](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/rendezvous.py) 与 [open_instruct/miles/execution/startup.py](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/startup.py)（attempt 会合与就绪门槛），配合 [docs/miles/operations.md](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/docs/miles/operations.md) 判断 run 是否真正完成。
