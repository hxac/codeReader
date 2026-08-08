# Megatron 权重服务端：/generate 与 /update_weights_from_disk

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 slime 为什么会把一个 Megatron 模型「反过来」当成 HTTP 服务暴露出去，以及这种「反向复用」解决什么问题。
- 看懂 `megatron_server.py` 暴露的六个 HTTP 路由，重点掌握 `/generate`（对数概率/采样）与 `/update_weights_from_disk`（热加载权重）两个核心端点。
- 理解 `SampleManager` 如何充当 HTTP 层与 Megatron 训练工人之间的请求队列，以及 `run_megatron_dp_models_loop_worker` 如何用双缓冲流水线把请求喂进 Megatron 前向。
- 读懂 `TeacherLogpRayActor.compute_logp` 如何在流水线（PP）末段收集结果、在上下文并行（CP）下拼接序列。
- 掌握 `logprob_utils.py` 中两个 TP 感知函数：无全量 gather 的两阶段采样、label-token 对数概率计算。

## 2. 前置知识

在进入本讲前，你需要已经理解以下概念（对应前置讲义）：

- **训练主循环与三模块闭环**（u1-l6、u2-l1）：rollout 产 Sample → data buffer → training 训练 → 权重单向同步回 rollout。
- **train_one_step / forward_only**（u4-l2）：slime 不重写流水线引擎，而是把 Megatron 的 `get_forward_backward_func()` 当执行器，用 `forward_step` 闭包注入 RL 损失；`forward_only` 是只前向、`@torch.no_grad()` 的只读路径，用来算 logprob/entropy/value。
- **权重同步全景与传输模式**（u5-l1、u5-l2、u5-l3）：训练后把 Megatron 分片权重单向注入 SGLang 引擎，换权重前要先 `flush_cache`；SGLang 引擎暴露 `update_weights_from_disk` 等「按钮」。

本讲的关键直觉是：在前面几讲里，**Megatron 始终是闭环里的「消费者」**——它吃 rollout 产的数据、产出权重。而本讲要讲的 `megatron_server.py`，把 Megatron **「反过来」变成「生产者」**：一个 Megatron 模型被包成 HTTP 服务，对外提供对数概率（logprob）与采样能力，谁需要就发 HTTP 请求来取。这就是标题里说的「反向复用 Megatron」。

为什么要这样做？一个典型场景是 **在线策略蒸馏（On-Policy Distillation, OPD）**。在 OPD 的 `sglang` 模式里，奖励函数需要对每条样本去问「教师模型」要 token 级 logprob。这个「教师」既可以用一个独立的 SGLang 服务承担（见 `examples/on_policy_distillation/run-qwen3-8B-opd.sh`，它启动 `sglang.launch_server` 当教师），也可以用本讲的 Megatron 服务端承担——两者说同一套 HTTP 协议（都暴露 `/generate`）。当教师本身就是 Megatron 格式的超大模型、或希望与训练侧严格同构、避免再做一次格式转换时，用 Megatron 服务端当教师更省事。

> 术语约定：本讲中 **TP** = 张量并行（tensor parallel），**PP** = 流水线并行（pipeline parallel），**CP** = 上下文并行（context parallel），**DP** = 数据并行（data parallel）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [slime/backends/megatron_utils/server/megatron_server.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/server/megatron_server.py) | 服务端本体：HTTP 应用、`SampleManager` 请求队列、`run_megatron_dp_models_loop_worker` 异步流水线工人、`launch` 启动流程。 |
| [slime/backends/megatron_utils/server/logprob_utils.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/server/logprob_utils.py) | `TeacherLogpRayActor`（Megatron 工人子类）、`compute_logp` 编排、两个 TP 感知的 logprob/采样函数。 |
| [slime/backends/megatron_utils/server/arguments.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/server/arguments.py) | 服务端专属参数（端口、超时、分块大小）与一组「只读不训」的强约束。 |
| slime/backends/megatron_utils/model.py | 提供 `forward_only`（u4-l2 已讲），本讲 `compute_logp` 复用它。 |
| slime/backends/megatron_utils/loss.py | 提供 `get_log_probs_and_entropy` / `get_responses`，是 logprob 计算的复用入口。 |
| slime/backends/megatron_utils/actor.py | `MegatronTrainRayActor.compute_log_prob` 与 `load_other_checkpoint`，被本讲子类继承/复用。 |

## 4. 核心概念与源码讲解

### 4.1 服务端定位、启动与「只读不训」参数约束

#### 4.1.1 概念说明

`megatron_server.py` 的目标是：拿一套和训练完全相同的 Megatron 模型构建与分布式初始化代码，但不做任何反向/优化器步骤，只把它当成一个 **只读的前向推理服务** 跑起来，对外提供两条核心能力：

1. **读**：`/generate` —— 给定 `input_ids`，返回教师模型对每个位置的 token 级对数概率（可选还返回采样 token 与 label-token 对数概率）。
2. **写（热加载）**：`/update_weights_from_disk` —— 在不重启进程的前提下，从磁盘换一份新检查点到 Megatron 工人里。

这与 u5-l3 讲的 SGLang 引擎「按钮」是对称的：SGLang 有 `update_weights_from_disk`（checkpoint reload），这里 Megatron 服务端也有同名的端点。换句话说，slime 让 Megatron 服务端 **模仿 SGLang 的服务契约**，从而可以「替换」推理引擎的位置——这正是本讲练习任务要讨论的「不重启 SGLang、用 Megatron 服务端提供 logprob/采样能力」。

#### 4.1.2 核心流程

启动流程集中在 [launch(args)](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/server/megatron_server.py#L721-L759)，步骤如下：

1. 先 `configure_megatron_server_args` + `validate_megatron_server_args`，把一堆训练相关开关强行关掉并校验。
2. `create_placement_groups(args)` 分配 GPU（u2-l2 已讲）。
3. 起一个不占 GPU 的 `SampleManager`（Ray actor），充当请求队列。
4. `create_training_models(..., actor_cls=TeacherLogpRayActor)` 建出 Megatron 工人，但工人类换成专做 logprob 的 `TeacherLogpRayActor`。
5. 为每个 DP rank 起一个异步流水线工人 `run_megatron_dp_models_loop_worker`。
6. 可选 warmup（用一个极小请求跑通整条链路），最后 `_start_http_server` 暴露 HTTP 端口。

#### 4.1.3 源码精读

`configure_megatron_server_args` 把训练语义全部关掉，确保这是一个纯推理服务：

[server/arguments.py:L87-L101](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/server/arguments.py#L87-L101) —— `debug_train_only=True`、`use_kl_loss=False`、`offload_train=False`、`use_critic=False`、`no_load_optim=True`、`no_load_rng=True`，并把 `only_train_params_name_list` 设成 `["nothing_to_train"]`（注释特别提醒要保持为 list，否则冻结逻辑会逐字符迭代）。

[server/arguments.py:L104-L131](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/server/arguments.py#L104-L131) —— `validate_megatron_server_args` 是一组硬约束：若 `only_train_params_name_list` 不是 `["nothing_to_train"]` 直接报错「Megatron server must not train any parameters」；若开了 `use_kl_loss`/`use_opd`/`use_critic` 也报错，明确说明本服务「只支持 teacher logprob prefill 模式」。

入口与启动：

[megatron_server.py:L762-L770](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/server/megatron_server.py#L762-L770) —— `main()` 解析参数后调 `launch(args)`，`if __name__ == "__main__"` 触发。它可作为独立脚本/模块启动（实际启动命令随部署环境而定，**待本地验证**）。

[megatron_server.py:L721-L735](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/server/megatron_server.py#L721-L735) —— `launch` 起好 `SampleManager` 后，用 `actor_cls=TeacherLogpRayActor` 建工人，把 `sample_manager` 当作 `rollout_manager` 传进去（复用训练侧的工人创建链路）。

[megatron_server.py:L741-L749](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/server/megatron_server.py#L741-L749) —— 按 DP rank 分组，给每个 DP rank 起一个异步工人，把「同 DP rank 的所有 TP/PP 工人句柄」传给它。

[megatron_server.py:L752-L757](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/server/megatron_server.py#L752-L757) —— warmup 默认开启（`--megatron-server-warmup`，默认 True），跑通后再 `_start_http_server`。

#### 4.1.4 代码实践

**实践目标**：通过阅读参数约束，理解「这个进程绝对不会训练」这一不变量是如何被代码强制的。

**操作步骤**：

1. 打开 `slime/backends/megatron_utils/server/arguments.py`，对照 `configure_megatron_server_args` 与 `validate_megatron_server_args`。
2. 设想有人误传了 `--use-critic`，找到会触发哪条 `raise ValueError`。
3. 查 `add_megatron_server_arguments` 里 `--teacher-port` 的默认值与对应的环境变量名。

**需要观察的现象**：服务端参数把「优化器、KL、critic、offload、动态批、wandb」全部显式关闭，且用断言式校验兜底。

**预期结果**：你会得出结论——这个进程从参数层面就被锁死成「只前向、不反传、不存优化器」的只读服务。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `only_train_params_name_list` 必须是 list 而不能是 str？

**参考答案**：因为参数冻结逻辑会迭代这个字段；若是字符串 `"abc"`，迭代会逐字符取 `'a'、'b'、'c'` 当正则去匹配参数名，语义完全错误。注释（arguments.py:L99）明确提示了这一点。

**练习 2**：服务端为什么把 `no_load_optim`、`no_load_rng` 都设成 True？

**参考答案**：服务端不训练，既不需要优化器状态也不需要 RNG 状态；加载检查点时跳过它们能省显存并加快启动。同时它只关心模型权重本身（用来前向算 logprob）。

---

### 4.2 SampleManager 请求队列与异步流水线工人

#### 4.2.1 概念说明

HTTP 是「一来一回」的同步语义，而 Megatron 前向是「一组 TP/PP 工人协作、跑流水线」的分布式过程。两者之间需要一个 **缓冲与解耦层**，这就是 `SampleManager`——它的 docstring 直白地写着「Minimal rollout-manager surface plus request queue for teacher-server mode」（[megatron_server.py:L82-L84](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/server/megatron_server.py#L82-L84)）。

它实现了一个极简的「rollout 管理者」接口（`get_input_data`/`save_log_probs` 等），这样就能复用训练侧 `create_training_models` 里「工人从 rollout_manager 取数据」的链路，而不必另写一套。真正驱动 Megatron 前向的是另一个异步工人 `run_megatron_dp_models_loop_worker`，它不断「取数 → fan-out 给工人 → 收结果 → 回写」。

#### 4.2.2 核心流程

请求的生命周期是一个「提交—轮询」环：

```
HTTP /generate
   │ submit(input_ids) ──► SampleManager._pending_requests（队列）
   │                                   │ get_input_data(worker_id)
   ▼                                   ▼
轮询 get_result(rid)  ◄── _results[rid] ◄── save_log_probs(worker, outputs)
                                            ▲
                          run_megatron_dp_models_loop_worker（每个 DP rank 一个）
                          取数 → actor.compute_logp.remote(...) fan-out → 合并 → 回写
```

异步流水线工人用 **双缓冲** 思想保持流水线满载：最多让 `pp_size + 1` 个微批同时在途，这样当一段流水线在算时，下一段的数据已经准备好。

#### 4.2.3 源码精读

`SampleManager.submit` 把请求塞进 `_pending_requests` 队列，返回 `request_id`：

[megatron_server.py:L102-L144](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/server/megatron_server.py#L102-L144) —— 校验 `input_ids` 非空、`sample_n` 合法、`response_length`/`loss_mask` 长度匹配；若没传 `request_id` 则自增生成 `req_N`。

`get_input_data` 是工人「领任务」的入口，把单条请求打包成训练侧熟悉的 `rollout_data` 字典：

[megatron_server.py:L193-L234](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/server/megatron_server.py#L193-L234) —— 关键是 `data_refs = [Box(ray.put(rollout_data))]`：把数据放进 Ray 对象存储，返回引用，这样多个 TP/PP 工人可以共享同一份数据而不必重复序列化；同时把这条任务的元信息记到 `_inflight[worker_id]`，等结果回来时对账。

`save_log_probs` 把工人算出的结果按 `request_id` 存进 `_results`，并维护两个全局计数（完成请求数、完成 token 数）供统计打印：

[megatron_server.py:L239-L280](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/server/megatron_server.py#L239-L280) —— 注意它兼容两种输出格式：新格式是 dict（含 `log_probs`/`sampled_token_ids`/`sampled_log_probs`/`label_token_log_probs`），旧格式直接是 log_probs 张量。

`get_result` 用 `pop` 取走结果（取完即删），HTTP 层靠轮询它判断是否完成：

[megatron_server.py:L282-L283](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/server/megatron_server.py#L282-L283) —— `return self._results.pop(request_id, None)`。

异步流水线工人的双缓冲循环：

[megatron_server.py:L671-L705](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/server/megatron_server.py#L671-L705) —— 两个阶段：

- **提交阶段**：若在途批次 `< MAX_INFLIGHT_BATCHES`，就 `get_input_data` 领一条任务，`[actor.compute_logp.remote(rollout_data_ref) for actor in actor_models]` fan-out 给同 DP rank 的所有 TP/PP 工人，把这一组 ObjectRef 追加进 `futures_queue`。`MAX_INFLIGHT_BATCHES = pp_size + 1`（L676）正是双缓冲：让流水线始终有下一段数据可吃。
- **收集阶段**：对最老的那批用 `ray.wait`；当队列已满（`should_block=True`）时阻塞等全部完成，否则非阻塞试探。全部完成后 `ray.get` 取回各 rank 的结果，`_merge_log_probs` 合并，`save_log_probs.remote` 回写。

`_merge_log_probs` 的合并规则：

[megatron_server.py:L286-L303](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/server/megatron_server.py#L286-L303) —— 按 `dp_rank` 排序，丢弃「log_prob / sampled / label 全为 None」的分片（即非 PP 末段、或非 tp/cp rank0 的工人返回的空结果），只保留真正产出数据的那一份。

#### 4.2.4 代码实践

**实践目标**：跟踪一条请求从「入队」到「结果回写」的完整路径，画出时序。

**操作步骤**：

1. 在 `submit`（L102）、`get_input_data`（L193）、`save_log_probs`（L239）、`get_result`（L282）四处各想象打一条日志。
2. 对照 `run_megatron_dp_models_loop_worker`（L671）的提交/收集两阶段，标注数据在哪一步从队列走到工人、又从工人走回 `_results`。
3. 回答：一个 `pp_size=4` 的模型，`MAX_INFLIGHT_BATCHES` 是多少？为什么取这个值？

**需要观察的现象**：提交与收集是 **交错** 进行的——不必等一批算完才能提交下一批，只要在途数没到上限。

**预期结果**：`MAX_INFLIGHT_BATCHES = pp_size + 1 = 5`。取 `pp_size + 1` 是经典流水线双缓冲：当第 1 个微批正在流水线的最后一段时，第 2 个微批已进入第一段，使流水线不空转。

#### 4.2.5 小练习与答案

**练习 1**：`get_input_data` 为什么用 `ray.put` + `Box` 包装数据，而不是直接把 list 传给工人？

**参考答案**：一份 `rollout_data` 要被同 DP rank 的多个 TP/PP 工人共同读取。若直接传 list，Ray 会为每个工人各序列化一份；先 `ray.put` 进对象存储拿到单个引用，多个工人共享同一引用，省去重复序列化与显存拷贝。`Box` 是 slime 用来标记「这是一个已 put 的对象引用」的轻量封装（u2-l3 提到过）。

**练习 2**：`_merge_log_probs` 为什么要丢弃某些分片？

**参考答案**：一次 `compute_logp` 会被同 DP rank 的所有 TP/PP 工人执行，但只有 **PP 末段** 且 **tp_rank==0、cp_rank==0** 的工人才真正把结果序列化成 list（其余返回 None，见 4.4.3）。合并时必须把这些 None 分片过滤掉，只留下真正有数据的那一份。

---

### 4.3 /generate 与 /update_weights_from_disk 两个 HTTP 端点

#### 4.3.1 概念说明

服务端共注册六个路由（[megatron_server.py:L597-L602](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/server/megatron_server.py#L597-L602)）：`/detect`、`/healthz`、`/info`、`/get_loads` 是辅助探针；真正干活的是两个 POST 端点：

- `/generate`：算 logprob/采样，对应闭环里的「读」方向。
- `/update_weights_from_disk`：热加载一份磁盘检查点，对应闭环里的「写（换权重）」方向。

这两个端点互斥：换权重期间 `/generate` 会直接返回 503，避免用半新半旧的权重算 logprob。

#### 4.3.2 核心流程

`/generate` 流程：解析 body → 长度校验 → `submit` 入队 → 每 50ms 轮询 `get_result` 直到拿到结果 → 裁剪到期望长度后返回。客户端断连时（`asyncio.CancelledError`）会主动 `cancel_request`，避免无主请求继续占用算力。

`/update_weights_from_disk` 流程：取 `model_path` → 若与当前 `args.load` 相同则跳过 → 用 `update_lock` 保证全局只有一个换权重动作在进行 → `_wait_until_idle` 等队列清空 → 调用真正的换权重函数 → 把 `args.load`/`args.ref_load` 更新为新路径。若多个请求同时换到 **同一个** `model_path`，会 **合并（coalesce）** 到同一个 future 上，避免重复加载。

#### 4.3.3 源码精读

`/generate` 端点：

[megatron_server.py:L517-L595](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/server/megatron_server.py#L517-L595) —— 关键片段：

- L518-L519：换权重进行中直接 503。
- L527-L538：`--megatron-server-max-length` 校验，超长返回 413（设 0 关闭）。
- L541-L543：解析 `sample_n` 与 `label_token_ids`（`label_token_ids` 长度必须等于 `len(input_ids)-1`，因为 next-token 预测少一个位置）。
- L570-L578：`submit` 入队。
- L580-L584：`while True` 轮询 `get_result`，每 50ms 一次。
- L585-L589：客户端断连时 `cancel_request`。

响应构造与裁剪：

[megatron_server.py:L379-L388](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/server/megatron_server.py#L379-L388) —— `_build_generate_response` 把 `log_probs` 以及可选的 `label_token_log_probs`/`sampled_token_ids`/`sampled_log_probs` 裁到 `expected_len = len(input_ids) - 1`，防止 CP 切片带来的长度偏差。

`/update_weights_from_disk` 端点：

[megatron_server.py:L443-L515](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/server/megatron_server.py#L443-L515) —— 关键片段：

- L452-L468：进入 `update_lock` 后分三种情况——已在加载同一 `model_path` 则 `coalesced=True` 复用 future；在加载别的路径则返回 409；否则新建 future 并标记 `in_progress`。
- L478-L483：`_wait_until_idle` 等队列与在途都为 0（防换权重时还有请求在用旧权重算），再 `asyncio.to_thread(update_from_disk_fn, model_path)` 真正换权重（放线程里避免阻塞事件循环）。
- L501-L502：成功后把 `args.load`、`args.ref_load` 同步为新路径，使 `/info` 反映最新状态。

真正的换权重函数：

[megatron_server.py:L708-L718](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/server/megatron_server.py#L708-L718) —— `_build_update_from_disk_fn` 对每个工人句柄调 `actor.update_from_disk.remote(model_path)`，`ray.get` 收齐。工人侧 `update_from_disk` 调 `load_other_checkpoint("actor", model_path)`（见 4.4.3），后者复用训练侧的检查点加载逻辑。

#### 4.3.4 代码实践

**实践目标**：理解两个端点为何互斥，以及换权重的「去重/合并」设计。

**操作步骤**：

1. 在 `/generate` 里找到两处「`server is updating from disk`」的 503 返回（L518、L565），说明为什么提交前后各检查一次。
2. 在 `/update_weights_from_disk` 里追踪 `coalesced` 变量：什么条件下为 True？为 True 时这个请求怎么拿到结果？
3. 读 `_wait_until_idle`（[L391-L399](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/server/megatron_server.py#L391-L399)），回答它等待的是哪两个量为 0。

**需要观察的现象**：换权重是一个「先排空、再换、再放开」的临界区；并发换同一份权重不会重复加载。

**预期结果**：

- 提交前后各查一次 `in_progress`，是为了在入队前就拒绝，避免请求进了队却在换权重期间被卡住。
- `coalesced=True` 当且仅当「正在加载的目标路径 == 本次请求的路径」；此时请求 `await asyncio.shield(update_future)` 等同一个 future 完成，复用结果。
- `_wait_until_idle` 等 `queue_size == 0` 且 `inflight_size == 0`，即没有排队、也没有在途请求。

#### 4.3.5 小练习与答案

**练习 1**：为什么换权重函数要用 `asyncio.to_thread(...)` 包一层，而不是直接 `await`？

**参考答案**：`update_from_disk_fn` 内部是 `ray.get(refs)` 同步阻塞调用（要等所有工人把新检查点加载完，耗时可能很长）。直接在 async handler 里同步阻塞会卡死整个事件循环，导致 `/healthz`、其他 `/generate` 的轮询等都无响应。用 `asyncio.to_thread` 把它丢到线程池，事件循环得以继续服务其他请求。

**练习 2**：`/generate` 里 `expected_log_probs_len = max(original_input_len - 1, 0)`，为什么减 1？

**参考答案**：语言模型做的是 next-token 预测：长度为 `L` 的输入序列只产生 `L-1` 个「位置→下一个 token」的对数概率（第一个位置没有前文）。所以 logprob 序列比输入短 1。

---

### 4.4 compute_logp：前向只读与 PP/CP 结果收集

#### 4.4.1 概念说明

`TeacherLogpRayActor`（[logprob_utils.py:L453](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/server/logprob_utils.py#L453)）是 `MegatronTrainRayActor` 的子类，复用全部模型构建、并行初始化、检查点加载逻辑，只新增一个 `compute_logp` RPC：吃一条请求数据，跑一次只读前向，返回 token 级 logprob（以及可选采样、label logprob）。它的核心还是 u4-l2 讲过的 `forward_only`——只是把「post-forward 回调」换成了 logprob/采样专用版本。

#### 4.4.2 核心流程

`compute_logp` 内部三步：

1. **备数**：`_prepare_rollout_data` 从对象存储取回数据、搬到 CUDA、补上微批元信息（单条请求当作 1 个微批）。
2. **前向**：根据是否要采样/label，选两条回调路径之一调 `forward_only`。
3. **收集**：在 PP 末段把结果合并；CP 下拼接被切开的序列；只在 `tp_rank==0 and cp_rank==0` 把张量转成 Python list（其余 rank 返回 None，由 4.2 的 `_merge_log_probs` 过滤）。

#### 4.4.3 源码精读

`compute_logp` 主体：

[logprob_utils.py:L472-L501](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/server/logprob_utils.py#L472-L501) —— 两条路径：

- 若 `sample_n > 0` 或带 `label_token_ids`：用 `forward_only(partial(_get_log_probs_and_optional_samples, sample_n=..., label_token_ids=...), ...)`，回调里同时算 logprob + 采样/label。
- 否则（纯 logprob）：复用父类的 `self.compute_log_prob(...)`，它内部就是 `forward_only(get_log_probs_and_entropy, ...)`（见 [actor.py:L350-L366](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L350-L366)）。

备数与微批元信息：

[logprob_utils.py:L229-L245](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/server/logprob_utils.py#L229-L245) —— `_prepare_rollout_data` 把 `tokens`/`loss_masks`/`label_token_ids` 转 CUDA 张量，并把单条请求伪装成「1 个微批、批大小 = 样本数」的结构（`micro_batch_indices`/`num_microbatches`/`global_batch_sizes`），从而直接喂给训练侧的 `get_data_iterator`。

PP 末段收集与 CP 合并：

[logprob_utils.py:L507-L543](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/server/logprob_utils.py#L507-L543) —— 只在 `is_pipeline_last_stage()` 内处理：

- 对 `log_probs`、`sampled_*`、`label_token_log_probs` 分别用 `_merge_tensors_with_cp` 在 CP>1 时把各 CP rank 的片段 `all_gather` 回完整序列。
- L539：再判 `cp_rank==0 and tp_rank==0` 才 `.tolist()` 序列化；其余返回 None。

CP 合并工具：

[logprob_utils.py:L248-L264](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/server/logprob_utils.py#L248-L264) —— `_merge_tensors_with_cp` 在 `cp_size==1` 时直接返回；否则对每条样本调 `all_gather_with_cp(tensor, total_length, response_length)` 把本 rank 持有的片段拼成完整 response。

回调里「同时算 logprob + 采样/label」：

[logprob_utils.py:L351-L450](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/server/logprob_utils.py#L351-L450) —— `_get_log_probs_and_optional_samples` 先调 `get_log_probs_and_entropy` 拿 response 的 logprob（复用 loss.py）；若带 `label_token_ids`，逐样本用 `get_responses` 取出 response 段 logits，调 `get_label_token_log_probs_from_vocab_parallel_logits`（4.5）；若 `sample_n>0`，逐样本调 `sample_from_vocab_parallel_logits_without_full_gather`（4.5）。

热加载权重的工人侧入口：

[logprob_utils.py:L566-L571](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/server/logprob_utils.py#L566-L571) —— `update_from_disk(model_path)` 调 `self.load_other_checkpoint("actor", model_path)`，后者（[actor.py:L634-L662](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L634-L662)）临时改写 `args.load`、`no_load_optim`、`finetune`，调 Megatron 原生 `load_checkpoint` 把新权重灌进现有模型骨架，再恢复原参数——和训练侧加载 ref/teacher 检查点同源。

#### 4.4.4 代码实践

**实践目标**：弄清「为什么只有 PP 末段、且 tp/cp rank0 才序列化结果」，并理解这与服务端合并逻辑的配合。

**操作步骤**：

1. 读 `compute_logp` 的 else 分支（L549-L553）：非 PP 末段的工人返回什么？
2. 读 L539-L548：`tp_rank==0 and cp_rank==0` 不满足时返回什么？
3. 把这两点与 4.2 的 `_merge_log_probs`（按 `dp_rank` 排序、丢弃全 None 分片）对应起来：一次 fan-out 里到底有几份「有用」结果？

**需要观察的现象**：一次 `compute_logp` 被 N 个工人执行，但只有 1 份非空结果。

**预期结果**：非 PP 末段返回全 None；非 `(cp_rank0, tp_rank0)` 也返回全 None。因此一次 fan-out 中，每个 DP rank 恰好有 1 个工人（PP 末段 × tp_rank0 × cp_rank0）产出有效 list，合并时按 `dp_rank` 排序后只保留这 1 份。这是「分布式前向 + 单点序列化」的标准做法：让一个 rank 代表整组把结果交出去，避免重复。

#### 4.4.5 小练习与答案

**练习 1**：`compute_logp` 在「纯 logprob」和「要采样/label」时分别走哪条回调？为什么不统一？

**参考答案**：纯 logprob 走父类 `compute_log_prob`（回调 `get_log_probs_and_entropy`，u4-l4）；要采样/label 走 `_get_log_probs_and_optional_samples`。不统一是因为采样/label 需要 **逐样本** 对 response 段 logits 做额外处理（采样或取特定 token 的 logprob），而纯 logprob 可以在整批 `[T,V]` 上一次性算完更高效，二者代码结构差异较大。

**练习 2**：`_prepare_rollout_data` 为什么要把单条请求的 `num_microbatches` 设成 `[1]`？

**参考答案**：服务端一次只处理一条请求（`get_input_data` 每次弹一条），这条请求的所有 token 当作一个微批送进 Megatron 前向。设 `num_microbatches=[1]` 告诉 `forward_only`：「这一批就 1 个微批」，无需切分。

---

### 4.5 logprob_utils 的 TP 感知采样与 label-token logprob

#### 4.5.1 概念说明

大词表（如 15 万+）下，TP 把 logits 切到各 rank，每个 rank 只持有一部分词表的 logits `[num_tokens, vocab_per_tp]`。如果按最朴素做法先把全词表 logits gather 到一起再 softmax 采样，每个 rank 都要物化 `[num_tokens, full_vocab]` 的概率，显存爆炸。`logprob_utils.py` 给出两个 **不物化全词表** 的 TP 感知算法：

- `sample_from_vocab_parallel_logits_without_full_gather`：两阶段采样，先选「哪个 rank 拥有这个采样槽」，再由被选中的 rank 在本地词表里采样。
- `get_label_token_log_probs_from_vocab_parallel_logits`：只取指定 label token 的 logprob，每个 rank 只贡献落在自己词表段的那部分。

#### 4.5.2 核心流程（两阶段采样的数学）

设 TP 把词表切成 `tp_size` 段，第 `r` 个 rank 持有 logits 片段 \(\ell^{(r)}\in\mathbb{R}^{T\times V_{\text{tp}}}\)，覆盖词表区间 \([rV_{\text{tp}},(r+1)V_{\text{tp}})\)。全局 softmax 的分母是：

\[
Z = \sum_{r=0}^{tp\_size-1}\sum_{j} \exp(\ell^{(r)}_{\cdot,j} - m),\qquad m=\max_{r,j}\ell^{(r)}_{\cdot,j}
\]

其中 \(m\) 是全局最大值（用 `all_reduce(MAX)` 得到，用于数值稳定）。

**阶段一：选 owner rank。** 每个 rank 算出自己占的概率质量 \(p^{(r)}=\sum_j\exp(\ell^{(r)}_{\cdot,j}-m)/Z\)，`all_gather` 后得到一个 `tp_size` 维的分类分布；rank0 用 `multinomial` 抽出每个采样槽归哪个 rank，再 `broadcast` 给所有 rank：

\[
\text{owner}(t,s) \sim \text{Categorical}(p^{(0)},\dots,p^{(tp\_size-1)})
\]

**阶段二：被选中的 rank 本地采样。** 对分给本 rank 的槽，在本 rank 的局部词表上用本地权重 \(w^{(r)}_j=\exp(\ell^{(r)}_{\cdot,j}-\text{local\_max})\) 做 `multinomial`，得到局部 id，加 `vocab_start` 还原成全局 token id，并算其 logprob：

\[
\log p(\text{token}) = \ell^{(r)}_{\cdot,j} - m - \log Z
\]

最后 `all_reduce(MAX)` 把「正确的 token id 与 logprob」汇聚到所有 rank（未被选中的槽贡献 -1/-inf，取 max 后被覆盖）。

label-token logprob 更简单：每个 rank 只对落在自己词表段的 label token `gather` 出 logit，其余置 0，`all_reduce(SUM)` 后只有 owner rank 的贡献留下，再减全局 \(\log Z\)。

#### 4.5.3 源码精读

两阶段采样的分母与 owner 选择：

[logprob_utils.py:L66-L111](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/server/logprob_utils.py#L66-L111) —— L66-L78 算每 rank 有效词表段（`global_vocab_size` 用来排除 padding 词表）；L80-L95 用分块（`reduction_chunk_size`）+ `all_reduce` 得到全局 `max` 与 `denom`；L97-L103 `all_gather` 各 rank 的概率质量 `tp_masses`；L105-L111 rank0 用 `multinomial(tp_probs, sample_n, replacement=True)` 选 owner，`broadcast` 同步。

本地采样与合并：

[logprob_utils.py:L113-L153](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/server/logprob_utils.py#L113-L153) —— L113 `owner_slots = tp_assignments.eq(tp_rank)` 算出哪些槽归本 rank；L121-L144 按「每个 row 被分到几个槽」分组，在本地词表 `multinomial` 采样，算 `global_ids = local_ids + vocab_start` 与 `selected_log_probs`；L146-L148 `all_reduce(MAX)` 合并；L150-L151 校验没有遗漏槽（否则报「incomplete sample slots」）。

label-token logprob：

[logprob_utils.py:L156-L222](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/server/logprob_utils.py#L156-L222) —— L196-L211 算全局 `max` 与 `log_denom`（与采样共享同一套分母计算）；L213-L215 用 `VocabUtility.vocab_range_from_per_partition_vocab_size` 算本 rank 词表段，`local_mask` 标出哪些 label 落在本段；L217-L220 `gather` 出本段 label 的 logit，非本段置 0，`all_reduce(SUM)` 汇聚；L222 返回 \(\text{logit} - \text{global\_max} - \log\_denom\)。

这两个函数在回调里的调用：

[logprob_utils.py:L374-L438](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/server/logprob_utils.py#L374-L438) —— `_get_log_probs_and_optional_samples` 对每条样本的 response 段 logits 调用上述两函数（label 分支 L374-L407，采样分支 L409-L438），分别用 `--teacher-label-reduction-chunk-size` 与 `--teacher-sample-reduction-chunk-size` 控制分母计算的分块大小（见 arguments.py:L54-L65）。

#### 4.5.4 代码实践

**实践目标**：用一个最小数值例子验证「两阶段采样 = 直接对全词表 softmax 采样」的等价性。

**操作步骤**（源码阅读 + 推理型，**待本地验证** 数值）：

1. 假设 `tp_size=2`、`vocab_per_tp=3`，rank0 持有 logits `[2,1,0]`（对应全局 token 0/1/2），rank1 持有 `[1,1,3]`（token 3/4/5）。
2. 手算全局 softmax：`m=3`，未归一化指数 `[e^{-1},e^{-2},e^{-3},e^{-2},e^{-2},e^{0}]`，`Z` 为其和；rank0 的概率质量 \(p^{(0)}=(e^{-1}+e^{-2}+e^{-3})/Z\)，rank1 的 \(p^{(1)}=(e^{-2}+e^{-2}+e^{0})/Z\)。
3. 验证：先按 \((p^{(0)},p^{(1)})\) 选 rank，再在被选 rank 内按本地权重采样，等价于直接在 6 个 token 上 softmax 采样。
4. （可选）在本地用 PyTorch 复现 `sample_from_vocab_parallel_logits_without_full_gather`（令 `tp_group=None` 单进程）对比 `torch.multinomial(softmax(full_logits))` 的分布。

**需要观察的现象**：两阶段采样的边缘分布与直接 softmax 采样一致；显存峰值只与 `vocab_per_tp` 相关，不随 `tp_size` 增长而爆炸。

**预期结果**：两种方法在大量采样下统计分布收敛到同一组概率；这正是函数 docstring 所 claims 的「no rank materializes full-vocab probabilities」（[L28-L34](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/server/logprob_utils.py#L28-L34)）。

#### 4.5.5 小练习与答案

**练习 1**：阶段一选 owner 时，为什么用 `all_reduce(MAX)` 而不是 `all_reduce(SUM)` 来合并最终结果？

**参考答案**：每个采样槽只被一个 rank 实际采样，其余 rank 在该槽填的是占位值（`-1` 的 token id、`-inf` 的 logprob）。用 `all_reduce(MAX)` 后，占位的 `-inf` 会被真正采到的 logprob 覆盖、`-1` 会被真正的 token id 覆盖，从而每个 rank 都拿到完整且一致的 `[num_tokens, sample_n]` 结果。若用 SUM 会把占位 0/−inf 混入，破坏结果。

**练习 2**：`get_label_token_log_probs_from_vocab_parallel_logits` 里 `local_mask` 的作用是什么？

**参考答案**：一个 label token 全局只有一个，只落在某一个 TP rank 的词表段内。`local_mask` 标出「这个 label 在不在本 rank 段」，在段内才 `gather` 出真实 logit，不在段内置 0；`all_reduce(SUM)` 后只有 owner rank 的贡献留下。这样既避免越界 gather，又保证各 label 的 logprob 只被算一次。

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「端到端追踪」任务：

**场景**：有人用 OPD-sglang 模式做蒸馏，但把教师从一个 SGLang 服务换成了本讲的 Megatron 服务端。请你作为读者，在不实际运行的前提下，推断并写清楚：

1. **请求怎么进来**：rollout 的奖励函数带着一条样本（`input_ids`）POST 到 `/generate`。从 [megatron_server.py:L517](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/server/megatron_server.py#L517) 开始，依次列出这条请求经过的对象与方法，直到 `get_result` 返回。
2. **谁在算**：请求被 `run_megatron_dp_models_loop_worker` 领走后，如何 fan-out 给 `TeacherLogpRayActor.compute_logp`（[logprob_utils.py:L472](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/server/logprob_utils.py#L472)）？结果在哪个 rank 被序列化、在 `_merge_log_probs` 里如何收敛成一份？
3. **换教师**：训练中途想换一个更强的教师检查点。描述发一次 `/update_weights_from_disk`（[megatron_server.py:L443](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/server/megatron_server.py#L443)）后，系统经历的「排空 → 换权重 → 放开」全过程，并说明此期间 `/generate` 会怎样。
4. **好处与代价**：相对 SGLang 教师，用 Megatron 服务端当教师的好处（复用 TP/CP/EP、与训练同构、免二次格式转换）与代价（Megatron 非推理优化引擎、无 continuous batching / paged KV cache、单请求吞吐较低、进程更重）各列 2 点。

把以上四点写成一份 300 字左右的「部署说明」，标注你引用的源码行号。

> 提示：第 4 点要紧扣「反向复用」的本质——Megatron 本是训练消费者，这里临时充当推理生产者，能力上继承了 Megatron 的并行与格式一致性，但缺少专用推理引擎的吞吐优化。

## 6. 本讲小结

- slime 通过 `megatron_server.py` 把 Megatron 模型「反向」包成一个 HTTP 服务，对外提供 logprob/采样（`/generate`）与热加载权重（`/update_weights_from_disk`），这是「反向复用 Megatron」的关键。
- `SampleManager` 是 HTTP 层与 Megatron 工人之间的请求队列，用 `submit`/`get_input_data`/`save_log_probs`/`get_result` 把同步 HTTP 语义解耦成异步分布式前向。
- `run_megatron_dp_models_loop_worker` 用 `MAX_INFLIGHT_BATCHES = pp_size + 1` 的双缓冲循环保持流水线满载，每个 DP rank 一个工人独立提交/收集。
- `TeacherLogpRayActor.compute_logp` 复用 `forward_only`，在 PP 末段、`tp_rank0 & cp_rank0` 单点序列化结果，CP 下用 `all_gather` 拼回完整序列。
- `logprob_utils.py` 的两阶段 TP 采样与 label-token logprob 都「不物化全词表」，靠 `all_reduce(MAX/SUM)` 汇聚，让显存只随 `vocab_per_tp` 增长。
- 两个核心端点互斥：换权重期间 `/generate` 返回 503，且换权重会先 `_wait_until_idle` 排空、对同一目标的并发请求做 coalesce 去重。

## 7. 下一步学习建议

- **回到训练侧的 logprob**：本讲的 `compute_logp` 与 u4-l4 的 `get_log_probs_and_entropy` 是同一套前向只读路径的两个入口，建议对照阅读 [loss.py:L470](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L470)，体会「训练算 logprob」与「服务算 logprob」的复用关系。
- **对比 SGLang 的同款按钮**：u5-l3 讲了 SGLang 引擎的 `update_weights_from_disk`，本讲 Megatron 服务端有同名端点；建议并排比较两者「换权重仪式」的差异（SGLang 要 flush KV cache，Megatron 服务端要 `_wait_until_idle`）。
- **进入部署拓扑**：U8 的 u8-l1（sglang-config 拓扑）与 u8-l2（PD 分离与外部引擎）会把「推理后端如何被编排」讲透，本讲是理解「Megatron 也能当推理后端」的铺垫。
- **动手验证**：若有多卡环境，可参照 `examples/on_policy_distillation/` 把教师从 SGLang 服务切到 Megatron 服务端，实测 `/generate` 的返回结构与吞吐，验证本讲的两阶段采样等价性。
