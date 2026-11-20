# 配置系统：TOML → RunSpec → RunConfig

> 本讲为 update 增量版本。本轮（`2f7889da9 → 2063f854b`，#2289 与 #2293）**configuration 包的 Python 源码继续零改动**（唯一例外是数据文件 `options.json` 随 MILES runtime pin 从 `22d2fb4c` 升到 `9eff46a4`），变化依旧全部落在「维护示例基线」上：#2289 把学习示例的 checkpoint 策略固化为**有界恢复保存**——Tiny node/three-node 与 Small four/eight-node 统一改为 `save_interval = 10`、`checkpoint_keep_last = 2`、`checkpoint_keep_every`（Tiny node 与 Small 为 50、three-node 为 100）、`save_final_checkpoint = true`，Small four/eight-node 从 `save_checkpoints = false`、`auto_resume = false` 整体翻转为启用保存与自动恢复；#2293 再把三份 Small 配方的 `launch.coordination.heartbeat_timeout` 从 120 提到 480 秒。examples README 新增「Recovery checkpoints and preserved milestones」策略节，development-defaults 的「Native checkpoints」条目与 olmo35-small.md 的「Checkpoint settings in the current starters」一节同步改写，`test_miles_demo_baseline.py` 与 `test_miles_throughput.py` 补上断言——本讲新增 4.7 节专门讲这组 checkpoint 基线。上一轮（#2271/#2280）的 token-mean 翻转与 Tiny 学习基线（4.6）、SciCode `launch_warnings` 警告分支、score-centering 预检、判分输入声明、评估采样默认、priority/over_length/epoch 预算等内容全部保留，仅刷新永久链接与行号。

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂一份 MILES run 文件（TOML）的结构：`schema_version`、必备 section、`[data]` 选择器互斥规则。
2. 沿着 `RunSpec.load → from_dict → compile → RunConfig.validate` 这条编译链，说清每个环节各做什么、错误在哪一层被拦截。
3. 会用 `python -m open_instruct.miles` 的 `plan` / `validate` 子命令与 `--set` 覆盖，在不动 GPU 的情况下排查配置错误。
4. 掌握上轮引入的三组配置语义：
   - `core.prompt_provider = "priority"` 及其约束（priority 设置要求 provider=priority、必须提供 `prompt_priority_field`、需要 `rollout_global_dataset`）；
   - `data.over_length` 只允许 `"fail"`（默认）或 `"exclude"`；
   - `training.num_epoch` 与 `training.num_rollouts` 互斥，epoch 预算的 run 默认以 `save_final_checkpoint` 在最后一次更新落盘。
5. 掌握上一轮新增的编译期字段与默认值：
   - 判分输入声明：`core.reward_final_answer_only`（进入维护示例）与 `core.reward_reasoning_mode`（`required`/`none`）；
   - score-centering 参数族：`loss_type = "score_centering"`、`score_centering_is`（`tis`/`mis`）及 `trainer.fp32_lm_head` 强制要求；
   - 评估采样默认：`eval_temperature = 1.0`、`eval_top_p = 0.95`、`eval_max_response_len` 上限 32768；
   - 池采样优先级 `data.sampling.priority` 与 replay 互斥；
   - `launch_warnings` 的 SciCode 分支：`[evaluation]` 里 scicode 任务显式给出的 `h5py_host_path` 是占位符时，`plan` 只在 `launch_warnings` 里提示（保持可规划），真正的拒绝发生在启动边界。
6. 掌握上轮（#2271/#2280）的维护示例基线翻转：所有示例改用 token-mean loss（`miles.calculate_per_token_loss = true`，省略时库默认仍为 response-mean），Tiny 学习示例统一为 256 prompts × 8、batch 2048、恒定 LR 6e-6 无 warmup，`small/dev.toml` 占位被实测 B300 配方取代。
7. 掌握本轮（#2289/#2293）固化的学习示例 checkpoint 基线：`save_interval = 10`、`checkpoint_keep_last = 2`、`checkpoint_keep_every`（Tiny node 与 Small 为 50、three-node 为 100）、`save_final_checkpoint = true`；Small four/eight-node 从「禁用保存 + 不自动恢复」翻转为「启用保存 + `auto_resume = true`」，two-node-mech 力学检查保持无 checkpoint；三份 Small 配方心跳超时 120 → 480 秒。会解释 30 更新预算下 10/20/30 保存点与 50 里程碑的关系，以及为何首个完整恢复保存必须落在受保护最短运行时之内。

## 2. 前置知识

- **TOML**：一种「节（table）+ 键值」格式的配置文件，`[data]` 定义一个节，`[[data.tasks]]` 定义数组中的一个元素节。Python 3.11+ 内置 `tomllib` 可解析。
- **dataclass**：Python 用类声明字段、由解释器自动生成 `__init__` 等方法的机制。本讲的 `RunSpec` 与 `CoreConfig` 都是 frozen（不可变）dataclass，配置一旦构造完成就不允许再改字段。
- **两层配置**：MILES 把配置分成两层——研究者面对的**结构化 run 文件**（含 `model`/`data`/`launch` 等节，描述「我要什么」），和底层运行时消费的 **Core/Miles 选项**（`[core]` 与 `[miles]`，描述「引擎怎么跑」）。前者由 `RunSpec` 承载，后者由 `CoreConfig` + `RunConfig` 承载，中间靠 `compile()` 翻译。
- **InputError**：所有用户输入错误的统一异常类型（`ValueError` 子类，见 `open_instruct/miles/errors.py`）。CLI 捕获后打印面向字段的错误信息并以退出码 2 结束。
- **score centering（分数中心化）**：一种替代 PPO/GRPO 裁剪加权的训练目标——把每个 token 的优势减去同分布的期望（「中心化」），需要记录每个生成 token 的候选概率。它的两种重要性采样变体缩写为 TIS（truncated IS，带裁剪）与 MIS（multiply-importance IS，带上下界）。本讲只关心它的**配置入口与预检**，训练数学留到 u8-l14。
- **thinking / delivered response**：思考型模型先输出 `<think>…</think>` 推理段，再给出最终答案文本。判分时「给验证器看哪一段」正是 `reward_final_answer_only` 声明的内容（详见 u8-l12）。
- **Beaker 秘密（secret）**：Beaker 平台上按名引用的凭据/配置值，任务启动时由平台注入，提交机上看不到也不解析值。SciCode 参考数据默认走名为 `SCICODE_H5PY_HOST_PATH` 的秘密（完整链路见 u8-l9）。
- **response-mean 与 token-mean（per-token）损失加权**：GRPO 一个 batch 里有 \( N \) 条长度不一的响应。response-mean（`calculate_per_token_loss = false`）让**每条响应**对损失的贡献权重相同——长答案不会被放大；token-mean（`calculate_per_token_loss = true`）让**每个参与训练的 token** 权重相同——长响应天然贡献更多。示意图上：\[ \mathcal{L}_{\text{resp}} \propto \frac{1}{N}\sum_{i=1}^{N}\ell_i \qquad \mathcal{L}_{\text{token}} \propto \sum_{i=1}^{N}\frac{|y_i|}{\sum_j |y_j|}\,\ell_i \] 其中 \( \ell_i \) 是响应 \( i \) 的逐 token 平均损失、\( |y_i| \) 是其有效 token 数（示意记法，精确归一化在 MILES 运行时内实现）。上轮维护示例的基线从前者翻转为后者。
- **有界恢复保存（bounded recovery saves）与永久里程碑（preserved milestones）**：MILES 的原生 checkpoint 分两类用途——「恢复保存」是频繁落盘、**会滚动删除**的临时存档，只保留最近 `checkpoint_keep_last` 份，用于抢占后续跑；「里程碑」按 `checkpoint_keep_every` 的更新间隔**永久保留**，用于回看与评估对照；`save_final_checkpoint` 再在训练结束时追加最终存档。「有界」指恢复保存占用有上界（不会随训练时长无限增长），代价是只能回退到最近的保存点。
- **`auto_resume` 与受保护最短运行时**：`launch.auto_resume = true` 让任务被抢占后自动重启并从最新完整保存点续跑（workflow 侧的恢复契约见 u8-l3）；「受保护最短运行时」是 Beaker 的 minRuntime 概念——任务在此时长内不会被调度器回收。若**首个完整恢复保存**没能落在受保护时段内，一次抢占就意味着从零重来，这正是 checkpoint 间隔要按实测墙钟时间选定的原因。
- **心跳超时（`launch.coordination.heartbeat_timeout`）**：driver 与 worker 之间的存活检测耐心值（秒）。训练任务做长耗时操作（如 checkpoint 写盘、引擎排空）时可能暂时无心跳，超时过短会把健康任务误判为死亡；本轮三份 Small 配方把它从 120 提到 480，且 #2293 允许已认证的 resume 调整该值（见 u8-l3）。
- 建议先完成 u8-l1（MILES 总览与架构分层），了解「open-instruct 负责配置/数据/奖励集成，MILES 运行时负责 rollout 编排」的三层所有权划分。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [open_instruct/miles/configuration/run_spec.py](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/run_spec.py) | 结构化 run 文件的加载、节级校验与编译入口；`RunSpec.compile()` 产出 `RunConfig` |
| [open_instruct/miles/configuration/config.py](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/config.py) | 底层 `CoreConfig`（Core 引擎开关，含判分输入与 priority 字段）与 `RunConfig`（合体校验、参数列表编译） |
| [open_instruct/miles/configuration/score_centering.py](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/score_centering.py) | score-centering 目标的 CPU 预检（FP32 head、GRPO 优势、互斥开关、TIS/MIS 参数；`bae4209 → 8cd7c9eb` 引入） |
| [open_instruct/miles/configuration/validation.py](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/validation.py) | 通用输入检查：类型、取值、未知字段的「did you mean」提示、TOML/JSON 读取、`--set` 值解析、候选概率计数校验 |
| [open_instruct/miles/configuration/constraints.py](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/constraints.py) | 共享约束定义：错误三分类、各节的封闭字段表（`STRUCTURED_FIELDS`）、原生校验选项清单、弃用字段迁移指引 |
| [open_instruct/miles/configuration/options.py](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/options.py) | 用原生 MILES 参数解析器的快照（options.json）校验/编码 miles 选项，不装 GPU 栈也能查错 |
| [open_instruct/miles/rollout/pool_sampling.py](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/rollout/pool_sampling.py) | `data.sampling` 的策略/权重/priority 语法校验（加载期经 `run_data.validate_data` 调用） |
| [open_instruct/miles/evaluation/evaluation.py](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/evaluation/evaluation.py) | `[evaluation]` 节解析；`validate_scicode_reference_path` 同时被 `launch_warnings` 用于计划期 SciCode 占位路径警告 |
| [configs/miles/examples/tiny/dev.toml](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/tiny/dev.toml) | 单卡小模型冒烟示例（含判分声明、评估采样设置与上轮改为 true 的 token-mean 基线） |
| [configs/miles/examples/small/two-node-mech.toml](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/small/two-node-mech.toml) | OLMo 3.5 Small B300 两节点力学检查配方：保持无 checkpoint（`save_checkpoints = false`、`auto_resume = false`），心跳超时本轮升至 480（实测配方体系详见 u8-l15） |
| [configs/miles/examples/small/four-node.toml](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/small/four-node.toml) | OLMo 3.5 Small B300 四节点起步预算配方：本轮从禁用保存翻转为 `save_interval = 10` + 里程碑 + `auto_resume = true`（eight-node.toml 结构相同，仅拓扑更大） |
| [configs/miles/examples/README.md](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/README.md) | 示例目录说明：按模型家族分组（tiny 学习示例 / small B300 实测配方）、判分声明要求、token-mean 基线、评估采样协议，以及本轮新增的「Recovery checkpoints and preserved milestones」策略节 |
| [docs/miles/development-defaults.md](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/development-defaults.md) | 开发默认值指南；含「Tiny learning batch and LR」「Loss weighting」条目与本轮改写的「Native checkpoints」（每 10 更新 + 里程碑）条目 |
| [docs/miles/olmo35-small.md](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/olmo35-small.md) | Small 配方文档；本轮新增「Checkpoint settings in the current starters」一节（30 更新预算下 10/20/30 保存点与 50 里程碑的关系、历史计时不含保存开销的告诫） |
| [open_instruct/test_miles_demo_baseline.py](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/test_miles_demo_baseline.py) | 钉住维护示例基线的测试：Tiny 学习基线（batch 2048 / LR 6e-6 / token-mean）、省略时的 response-mean 兼容、显式覆盖仍可行、Small 配方共用基线；本轮为 `checkpoint_keep_every` 增加拓扑豁免 |
| [open_instruct/test_miles_throughput.py](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/test_miles_throughput.py) | 钉住示例预设的测试；本轮新增 checkpoint 基线断言（save_interval 10 / keep_last 2 / keep_every 50 或 100 / save_final_checkpoint） |
| [docs/miles/configuration-intro.md](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/configuration-intro.md) | 维护中的配置参考：接口、优先级、默认值交互 |
| [docs/miles/core.md](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/core.md) | Core 引擎文档，含 Score centering 一节 |
| [docs/miles/grading.md](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/grading.md) | 判分输入契约文档：`reward_final_answer_only` / `reward_reasoning_mode` 的语义（配合 u8-l12） |
| [docs/miles/grpo.md](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/grpo.md) | MILES GRPO 指南；「Truncated responses」一节记录 token-mean 示例基线 |

---

## 4. 核心概念与源码讲解

### 4.1 RunSpec：从 TOML 到结构化运行规格

#### 4.1.1 概念说明

`RunSpec` 是研究者面对的那层配置对象：它读入 TOML run 文件，做**节级与字段级**的结构校验（哪些节存在、字段名是否认识、路径是否解析成绝对路径），但**不做**任何需要 GPU 或数据文件的工作。模块 docstring 把职责说得很清楚：校验模型/数据/训练/服务/裁判/启动设置、解析路径与覆盖，并把各节翻译成底层 MILES/Core 选项——它只「描述」准备、GPU 分配与输出位置，不执行这些阶段，因此 CLI 可以在预约算力之前先 plan 审阅一遍。

#### 4.1.2 核心流程

```text
TOML 文件
  │ validation.read_document（TOML/JSON 解析）
  ▼
specs.from_dict ──training.algorithm=="opd" 且 trainer.backend=="megatron"──► OPDRunSpec（蒸馏，本讲不展开）
  │ 否则
  ▼
RunSpec.from_dict
  ├─ _apply_overrides        # --set SECTION.KEY=TOML_VALUE 逐个写入
  ├─ 顶层字段白名单 + schema_version==1 + name 字符集检查
  ├─ _table(model/output/...) # 封闭字段表（STRUCTURED_FIELDS），未知字段报错并给近似提示
  ├─ RunSpec._data           # [data]：选择器唯一性、over_length 取值、sampling 语法、seed/shuffle、路径解析
  ├─ RunSpec._launch         # [launch]：默认值合并、Beaker 时长/优先级/挂载/保留环境变量检查
  └─ result.compile()        # 立即编译一次，加载即校验
```

关键点：**加载即编译**。`from_dict` 在构造完成后马上调用 `compile()`（[open_instruct/miles/configuration/run_spec.py:262-264](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/run_spec.py#L262-L264)），所以 `RunSpec.load()` 返回时，配置已经通过了一轮完整编译校验。

#### 4.1.3 源码精读

**入口与 schema 检查**。[open_instruct/miles/configuration/run_spec.py:158-195](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/run_spec.py#L158-L195)：`load` 解析路径后调 `from_dict`；后者先深拷贝、应用覆盖，再用白名单检查顶层节名，并强制 `schema_version` 必须是整数 1，`name` 只能含字母、数字、点、下划线和连字符。这两条是最先被检查的「文件身份」。

**`--set` 覆盖的解析**。[open_instruct/miles/configuration/run_spec.py:118-136](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/run_spec.py#L118-L136)：每个覆盖按 `SECTION.KEY=TOML_VALUE` 拆分；键的分段要求小写开头（`[a-z][a-z0-9_]*`），唯一例外是三段式的 `launch.env.NAME` / `launch.secrets.NAME`——环境变量名天然大写。值本身用 `tomllib` 按 TOML 语法解析（[open_instruct/miles/configuration/validation.py:124-131](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/validation.py#L124-L131)），所以字符串要引号、布尔写裸的 `true/false`。对同一键的重复赋值，后写的覆盖先写的（普通字典赋值语义）。

**`[data]` 节：选择器互斥 + over_length 取值校验**。[open_instruct/miles/configuration/run_spec.py:313-327](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/run_spec.py#L313-L327)：

```python
selectors = [key for key in ("tasks", "sources", "recipe", "rl_manifest", "prompt_data") if key in data]
if len(selectors) != 1:
    raise InputError(
        f"data must select exactly one of tasks, sources, recipe, rl_manifest or prompt_data; found {selectors or 'none'}. ..."
    )
validation.choice(data.get("over_length", "fail"), "data.over_length", ("fail", "exclude"))
```

- `[data]` 必须且只能从五种数据选择器中挑一种；
- `over_length` 缺省为 `"fail"`，只接受 `"fail"` 或 `"exclude"`，写错值会得到形如 `data.over_length must be one of 'fail', 'exclude'; got 'exclud'.` 的报错。

`over_length` 与本轮的 `sampling` 之所以能出现在 `[data]`，是因为它们都在封闭字段表里：[open_instruct/miles/configuration/constraints.py:294-312](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/constraints.py#L294-L312) 中 `STRUCTURED_FIELDS["data"]` 包含 `"over_length"` 与 `"sampling"`。字段表是「文档 + 校验 + 覆盖率检查」共用的单一事实源——不在表里的字段会在加载时直接报「Unknown [data] fields」。`data.sampling`（含本轮新增的 `priority` 键）的语法在加载期由 [open_instruct/miles/datasets/run_data.py:221-224](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/datasets/run_data.py#L221-L224) 调 `pool_sampling.validate` 检查（要求配置了池，且 priority 只与 `uniform`/`exposure` 策略组合，见 [open_instruct/miles/rollout/pool_sampling.py:9-36](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/rollout/pool_sampling.py#L9-L36)）。

**`[launch]` 节的默认值合并**。[open_instruct/miles/configuration/run_spec.py:371-394](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/run_spec.py#L371-L394)：workspace/budget/cluster/priority/min_runtime/timeout 等有一整套内部默认值，用 `defaults | launch` 合并（TOML 里写了就用你的）；随后校验 Beaker 风格时长（`"45m"`、`"2h30m"`，见 [open_instruct/miles/configuration/validation.py:64-84](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/validation.py#L64-L84)）、保留环境变量冲突（`RAY_ADDRESS`、`CUDA_VISIBLE_DEVICES` 等由启动器拥有）以及 weka 挂载路径不重叠。

**未知字段的「did you mean」提示**。[open_instruct/miles/configuration/validation.py:99-108](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/validation.py#L99-L108)：`fields()` 用 `difflib.get_close_matches` 对每个未知字段找一个最接近的合法名。把 `model.source` 写成 `model.sorce`，报错会附带 `'sorce': did you mean 'source'?`。

**示例文件中的关键设置**。[configs/miles/examples/tiny/dev.toml:30-32](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/tiny/dev.toml#L30-L32) 在 `[data]` 中显式设置了 `over_length = "exclude"`；[configs/miles/examples/tiny/dev.toml:71-77](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/tiny/dev.toml#L71-L77) 在 `[core]` 中声明 `reward_final_answer_only = true` 与 `reward_reasoning_mode = "required"`（语义见 4.5）；[configs/miles/examples/tiny/dev.toml:84-87](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/tiny/dev.toml#L84-L87) 的 `[miles]` 节本轮起写 `calculate_per_token_loss = true`（连冒烟示例也翻转为 token-mean，见 4.6）。

#### 4.1.4 代码实践

**实践 A：最小 run 文件的加载与两类报错观察**（CPU 即可，无需 GPU 与数据）。

1. 实践目标：亲手触发「文件身份错误」与「未知字段 did-you-mean」两类报错，熟悉错误信息风格。
2. 操作步骤：
   - 复制 `configs/miles/examples/tiny/dev.toml` 为 `my-run.toml`，把 `model.source` 改成本地任意占位路径（如 `/tmp/models/tiny-policy-hf`），`output.root` 改成 `/tmp/runs/dev`；
   - 运行 `uv run python -m open_instruct.miles validate my-run.toml`，确认能通过（结构化 validate 是 CPU-safe 的）；
   - 把 `source` 改成 `sorce` 再运行一次，记录报错；
   - 恢复后再运行 `uv run python -m open_instruct.miles validate my-run.toml --set schema_version=2`，记录报错。
3. 需要观察的现象：第二次输出应包含 `Unknown [model] fields` 与 `did you mean 'source'?`；第三次应报 `schema_version must be 1`。
4. 预期结果：错误信息都以 `my-run.toml: ...` 前缀打印（CLI 的 `parser.error` 包装），退出码为 2。
5. 若无法本地运行（如未装依赖），标「待本地验证」，改为直接阅读 [open_instruct/miles/configuration/validation.py:99-108](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/validation.py#L99-L108) 推断输出。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `RunSpec.from_dict` 在返回前就要调用 `compile()`，而不是等用户显式调用？

**答案**：加载即校验。`RunSpec` 的职责就是保证「读进来的文件是可编译的」；若允许构造出一个未编译的非法对象，错误会推迟到 `plan`/`train` 才爆出来，排查成本更高。`compile()` 本身也承诺不加载权重和 GPU 库（见其 docstring），所以提前调用没有额外代价。

**练习 2**：`[data]` 同时写了 `tasks` 和 `prompt_data` 会发生什么？

**答案**：`_data` 中的选择器检查发现 `len(selectors) != 1`，抛出 `data must select exactly one of tasks, sources, recipe, rl_manifest or prompt_data; found ['tasks', 'prompt_data'].`，并提示移除竞争选择器或改用 `data.tasks`。

**练习 3**：`--set` 的值为什么字符串要加引号、布尔不用？

**答案**：`override_value` 把 `KEY=VALUE` 的右半部分拼成 `value=<raw>` 再用 `tomllib` 解析（[open_instruct/miles/configuration/validation.py:124-131](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/validation.py#L124-L131)）。TOML 语法里字符串必须带引号，而 `true`/`false` 本身就是合法的裸 TOML 布尔值。

---

### 4.2 编译链：RunSpec.compile 与 CoreConfig/RunConfig

#### 4.2.1 概念说明

`compile()` 是两层配置之间的翻译器：把结构化 run 文件里「研究者语言」的节（`[training]`、`[trainer]`、`[inference]`……）翻译成底层引擎消费的 `core.*` 与 `miles.*` 选项，套上工作流默认值，做跨字段的容量算术（GPU 整除、上下文长度、批大小），最后组装成 `RunConfig(CoreConfig(...), miles_options)` 并整体校验。`CoreConfig` 是 Core 训练引擎的开关集合（frozen dataclass），`RunConfig` 把它与 miles 选项字典合在一起做联合合法性检查，并能编译出真正的命令行参数列表。

#### 4.2.2 核心流程

```text
RunSpec.compile(prepared=None)
  ├─ put(target, value, origin)：一切设置的唯一入口
  │    ├─ miles 键经 options.resolve_option 归一化（别名→规范名）
  │    ├─ 路径键解析为绝对路径
  │    ├─ 逐值即时校验（miles 走选项快照 + runtime_values；core 试构造 CoreConfig）
  │    └─ origins 记录来源；同目标不同值 → 冲突报错
  ├─ RUN_SECTIONS 各键分发：SPECIAL_CONTROLS / FIELD_MAP / core.* / miles.*
  ├─ [core] 与 [miles] 显式选项、compiler_cache、records、selection 依次 put
  ├─ 拓扑算术：world、rollout GPU 数、TP 整除、colocate 一致性、上下文/响应长度
  ├─ defaults 字典批量 setdefault（含 wandb_group 默认）
  ├─ 预算判定：num_epoch 与 num_rollout 互斥；否则默认 num_rollout=100
  ├─ 异步校正：off_policy_correction 在 score-centering 下映射 score_centering_is（8cd7c9eb 轮改）
  ├─ 保存策略：save_checkpoints=true 时按预算二选一（save_interval 或 save_final_checkpoint）
  ├─ 互斥检查：data.sampling.priority 与 prompt replay 互斥（8cd7c9eb 轮新增）
  ├─ 评估收尾：eval 采样默认 temperature 1.0 / top_p 0.95 / 响应上限 32768（8cd7c9eb 轮改）
  └─ RunConfig(CoreConfig(**core), miles, inference_profile=profile) → result.validate()
（plan() 在编译产物之外另生成 launch_warnings——28c181666 轮起首查 SciCode 参考路径，只警告不拦截）
```

`bae4209 → 8cd7c9eb` 轮在这条链上改了四处：`off_policy_correction` 的 score-centering 分支、异步锚定条件扩展、池优先级与 replay 互斥检查、评估采样默认值。`8cd7c9eb → 28c181666` 轮的改动不在 compile 内部，而在 compile 之后的 `plan()` 装配环节：`launch_warnings` 的第一条变成 SciCode 参考路径检查（见 4.2.3）。最近两轮（`28c181666 → 2f7889da9`、`2f7889da9 → 2063f854b`）compile 链零改动——基线变化全部发生在示例 TOML 一侧（见 4.6 与 4.7）。更早一轮的四处变化（defaults 移除固定 `num_rollout=100`、新增 `wandb_group` 默认、epoch 预算的 `save_final_checkpoint`、launch_warnings 容忍 `num_rollout` 缺席）继续保留。

#### 4.2.3 源码精读

**`put()`：带来源追踪的唯一写入口**。[open_instruct/miles/configuration/run_spec.py:482-511](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/run_spec.py#L482-L511)：每个设置都要写明 `origin`（如 `training.num_rollouts`）。如果同一个目标被两个来源写了**不同的值**，`origins` 表会给出 `Conflicting settings for core.prompt_provider: core.prompt_provider and data.sources` 这样的精确冲突信息——这正是 priority provider 不能与 `data.sources` 混用的机制基础（`[data.sources]` 会自动 put `core.prompt_provider` 为 `pools`/`mixture`，见 [open_instruct/miles/configuration/run_spec.py:803-809](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/run_spec.py#L803-L809)；你再在 `[core]` 里写 `prompt_provider="priority"` 就会撞上冲突检查）。

**名字重映射 FIELD_MAP**。[open_instruct/miles/configuration/run_spec.py:40-67](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/run_spec.py#L40-L67)：结构化节里的名字与底层选项名并不总一致，例如 TOML 写 `training.num_rollouts`（复数），编译成 `miles.num_rollout`（单数）；`trainer.gpus` 编译成 `miles.actor_num_gpus_per_node`；`async.max_weight_staleness` 编译成 `core.max_policy_lag`。这张表就是「研究者命名 → 引擎命名」的对照表。

**默认值字典与 wandb_group 默认**。[open_instruct/miles/configuration/run_spec.py:681-735](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/run_spec.py#L681-L735)：批量 `setdefault` 填入工作流默认（LR 1e-6、8 prompts × 8 responses、上下文 6144 等）。其中：

```python
"wandb_group": miles.get("wandb_run_name", self.name),
```

即 `wandb_group` 缺省时优先跟随已解析的 `wandb_run_name`，否则用 run 名——多条同名 run 可以自然聚成一组对比实验。

**预算互斥：num_epoch vs num_rollouts**。[open_instruct/miles/configuration/run_spec.py:736-740](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/run_spec.py#L736-L740)：

```python
if miles.get("num_epoch") is not None:
    if miles.get("num_rollout") is not None:
        raise InputError("Choose num_epoch or num_rollouts; MILES would otherwise ignore num_epoch")
else:
    miles.setdefault("num_rollout", 100)
```

以前 `num_rollout=100` 是无条件默认，会**静默掩盖** `num_epoch`；现在把「rollout 预算」从 defaults 字典里拿出来，改成：设了 `num_epoch` 就原样透传给原生 MILES，两者都设则直接报错。原生运行时会在加载完数据后把 epoch 换算成 rollout 数（见 4.4.3）。

**异步校正的 score-centering 改写**。[open_instruct/miles/configuration/run_spec.py:751-767](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/run_spec.py#L751-L767)：结构化 `[async] off_policy_correction = "tis"` 的翻译分成两支：

```python
centering = score_centering.is_enabled(miles)
if "off_policy_correction" in controls and centering:
    put("miles.score_centering_is", "tis", controls["off_policy_correction"][1])   # 原生 score-centering TIS
elif "off_policy_correction" in controls:
    put("miles.use_tis", True, controls["off_policy_correction"][1])               # 旧版 PPO+TIS
miles.setdefault("use_tis", asynchronous and not centering and not miles["use_rollout_logprobs"])
```

启用了 score centering（`loss_type="score_centering"`）时，同一个 `async.off_policy_correction="tis"` 不再打开旧版 `use_tis`，而是选择 score centering 自己的 TIS 加权；`use_tis` 的自动默认也排除了 centering 的情形。紧接着的异步锚定条件同步扩展（[open_instruct/miles/configuration/run_spec.py:768-774](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/run_spec.py#L768-L774)）：异步训练现在要求 `use_tis`、`score_centering_is=tis/mis`、`use_rollout_logprobs` 三者之一作为「策略锚」。

**池优先级与 replay 互斥**。[open_instruct/miles/configuration/run_spec.py:801-802](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/run_spec.py#L801-L802)：

```python
if self.data.get("sampling", {}).get("priority") and core.get("prompt_replay_enabled", False):
    raise InputError("Pool priority ordering does not support replay")
```

`data.sampling.priority`（池内按优先级排序**新**提示）与 `[core] prompt_replay_enabled`（重放**旧**提示的历史层）语义冲突，编译期直接拒绝。它与独立的 priority provider（4.4）是两套机制，共享同一套 1–4 历史分层定义。

**保存策略的二分**。[open_instruct/miles/configuration/run_spec.py:788-800](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/run_spec.py#L788-L800)：

```python
if save_enabled:
    if miles.get("num_rollout") is not None:
        miles.setdefault("save_interval", miles["num_rollout"])   # 在最后一个 collection 保存
    else:
        # Native MILES resolves the epoch budget after loading the dataset.
        # Save the final update without inventing a periodic cadence here.
        core.setdefault("save_final_checkpoint", True)
elif "miles.save_interval" in origins:
    raise InputError("save_checkpoints=false conflicts with an explicit save_interval")
```

固定 rollout 预算时，`save_interval` 默认等于 `num_rollout`（保存最后一个 collection）；epoch 预算在编译期**无法**知道总 rollout 数（要等数据加载），所以改为设置 `core.save_final_checkpoint=True`，由驱动在最后一次更新落盘，而不是编造一个周期性保存节奏。

**评估采样默认对齐**。[open_instruct/miles/configuration/run_spec.py:864-871](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/run_spec.py#L864-L871)：存在 held-out 评估数据时，编译默认从「贪心解码」改为「与训练采样对齐的核采样」：

```python
miles.setdefault("eval_temperature", 1.0)
miles.setdefault("eval_top_p", 0.95)
miles.setdefault("n_samples_per_eval_prompt", 1)
miles.setdefault("eval_max_response_len", min(response, 32768))
```

此前 `eval_temperature` 默认 0.0（贪心）、没有 `eval_top_p` 默认、`eval_max_response_len` 直接等于训练响应长度；现在温度默认 1.0、top-p 0.95，响应预算取训练值与 32768 的较小者。显式写出的值仍然胜过默认（`miles.eval_*` 覆盖）。

**CoreConfig：本轮字段的引入位置**。[open_instruct/miles/configuration/config.py:43-51](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/config.py#L43-L51)（判分输入声明，见 4.5）、[config.py:84-85](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/config.py#L84-L85)（`save_final_checkpoint`）、[config.py:129-133](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/config.py#L129-L133)（priority 三件套）。

**RunConfig 与最终产物**。[open_instruct/miles/configuration/config.py:355-358](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/config.py#L355-L358)：`RunConfig = (core, miles, inference_profile)`。`arguments()` 把 core 序列化成 JSON 塞进 `--olmo-core-config`，把 miles 编码成普通命令行旗标——「不 import CUDA/MILES/Core、不下载模型」就能产出 argv，这是 CPU 规划的基石。`plan()`（[run_spec.py:938-961](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/run_spec.py#L938-L961)）再包上 launch_warnings、评估计划与 GPU 拓扑分配，输出一份完整 JSON 计划。

**launch_warnings 的 SciCode 首查**。[run_spec.py:904-909](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/run_spec.py#L904-L909)：`launch_warnings` 的第一条是对 SciCode 参考路径的计划期检查：

```python
def launch_warnings(self, miles, core=None) -> list[str]:
    warnings = []
    try:
        evaluation.validate_scicode_reference_path(self.evaluation)
    except InputError as error:
        warnings.append(str(error))
```

被调用的 `validate_scicode_reference_path`（[evaluation.py:241-260](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/evaluation/evaluation.py#L241-L260)）扫描 `[evaluation]` 里 `kind = "external"` 且 `task = "scicode"` 的条目：未写 `arguments.h5py_host_path` 的直接跳过（默认走 Beaker 秘密，由评估器在远端解析）；写了的必须是**非空、无首尾空白、绝对**的路径，且不能命中 `YOUR_[A-Z_]+`、`$`、`<>` 等占位形态。关键在 `try/except` 把 `InputError` **降级成警告**——docstring 明说「Examples remain loadable for planning」：模板与示例保持可加载、可 plan，真正的拒绝发生在三个启动边界——`launch.specification`（[launch.py:34-39](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/execution/launch.py#L34-L39)，在拓扑规划之前）、`workflow.execute`（[workflow.py:349-350](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/execution/workflow.py#L349-L350)）与提交评估任务时的 `evaluation.specification`（[evaluation.py:367-369](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/evaluation/evaluation.py#L367-L369)）（完整链路见 u8-l9）。配套地，`evaluation.parse` 现在会在存在「未显式给路径的 scicode 任务」时自动注入默认秘密（[evaluation.py:232-236](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/evaluation/evaluation.py#L232-L236)：`value["secrets"].setdefault(SCICODE_REFERENCE_SECRET, SCICODE_REFERENCE_SECRET)`，秘密名常量见 [evaluation.py:34](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/evaluation/evaluation.py#L34)），[three-node.toml:157-162](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/tiny/three-node.toml#L157-L162) 因此删去了原来的 `h5py_host_path = "/weka/.../YOUR_USERNAME/scicode/test_data.h5"` 占位参数（本轮该文件的 `[training]` 与 `[core]` 节各插入若干 checkpoint 行，其后行号整体下移，引用时注意对照）。

#### 4.2.4 代码实践

**实践 B：用 Python 对比两种预算与两种校正的编译产物**（纯 CPU）。

1. 实践目标：直观看到 epoch 预算与 rollout 预算、PPO+TIS 与 score-centering 编译出的参数差异。
2. 操作步骤（示例代码，非项目原有代码）：

   ```python
   from open_instruct.miles.configuration import run_spec

   base = {
       "schema_version": 1,
       "name": "budget-demo",
       "model": {"source": "model"},
       "output": {"root": "/tmp/runs/budget-demo"},
       "data": {"tasks": [{"task": "gsm8k", "train_count": 32, "eval_count": 8}]},
   }
   for budget in ({"training": {"num_epoch": 2}}, {"training": {"num_rollouts": 4}}):
       spec = run_spec.RunSpec.from_dict(base | budget)
       argv = spec.compile().arguments()
       print([a for a in argv if a in ("--num-epoch", "--num-rollout", "--save-interval", "--save-final-checkpoint")])
       print("save_final_checkpoint =", spec.compile().core.save_final_checkpoint)
   ```

   对照断言与 [open_instruct/test_miles_epoch_budget.py:21-26](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/test_miles_epoch_budget.py#L21-L26)：epoch 版 argv 含 `--num-epoch`、不含 `--num-rollout` 与 `--save-interval`，且 `core.save_final_checkpoint` 为 True。
3. 需要观察的现象：rollout 版 argv 含 `--num-rollout` 与 `--save-interval`，`save_final_checkpoint` 为 False；epoch 版相反。
4. 预期结果：与测试文件断言一致。
5. 无法运行时标「待本地验证」，转而精读 [run_spec.py:736-740](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/run_spec.py#L736-L740) 与 [run_spec.py:788-800](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/run_spec.py#L788-L800) 推演结果。

#### 4.2.5 小练习与答案

**练习 1**：为什么 epoch 预算不在编译期把 `num_epoch` 换算成 `num_rollout`？

**答案**：换算公式是 \[ \text{num\_rollout} = \text{num\_epoch} \times \left\lfloor \frac{\text{dataset\_size}}{\text{rollout\_batch\_size}} \right\rfloor \] 其中 `dataset_size` 要等准备好的数据加载之后才知道。编译承诺不读数据文件，所以只能把 `num_epoch` 原样透传，由原生 MILES 在数据加载后换算；保存节奏也因此退化为 `save_final_checkpoint`（只保最终更新）而不是编造 `save_interval`。

**练习 2**：`async.off_policy_correction = "tis"` 在 `loss_type = "score_centering"` 开启时编译出什么？

**答案**：编译出 `miles.score_centering_is = "tis"`（原生 score-centering 的 TIS 加权），而**不是**旧版 `miles.use_tis = true`（[run_spec.py:756-762](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/run_spec.py#L756-L762)）。若你同时显式设了 `score_centering_is="mis"`，两个来源对同一目标写入不同值，会被 origins 冲突检查拒绝——这正是 [open_instruct/test_miles_score_centering.py:80-90](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/test_miles_score_centering.py#L80-L90) 钉住的行为。

**练习 3**：`launch_warnings` 里周期性 checkpoint 的判定为什么容忍 `num_rollout` 缺席？

**答案**：epoch 预算下 `miles["num_rollout"]` 是 None，旧式 `save_interval < num_rollout` 比较会出错。新代码（[run_spec.py:922-924](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/run_spec.py#L922-L924)）改为 `periodic = interval is not None and (total is None or interval < total)`，没有总 rollout 数时只要有 `save_interval` 就算周期性保存。顺带一提：`8cd7c9eb → 28c181666` 轮在 `launch_warnings` 开头插入 SciCode 检查后，这段代码及其后所有行号整体下移了 4 行——给行号敏感的引用对版本时别忘了这一点。

---

### 4.3 校验与约束：validation、constraints 与错误三分类

#### 4.3.1 概念说明

校验分两层：`validation.py` 提供**无状态的单值检查**（布尔、整数、时长、取值枚举、未知字段），`constraints.py` 定义**共享的约束知识**（错误分类、封闭字段表、原生校验选项清单、弃用选项的迁移指引），而涉及多字段组合的规则留在 `CoreConfig.__post_init__` 与 `RunConfig.validate` 里。`score_centering.py` 是第三种形态：**专题预检函数**——针对一个功能族（score centering）集中检查十余条规则，在 `RunConfig.resolved_miles()` 里被调用。所有检查都是 CPU-only：目标是让你在预约 GPU 之前就发现配置错误。

错误三分类（[docs/miles/configuration-intro.md:40-48](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/configuration-intro.md#L40-L48)）：

| 类别 | 含义 | 例子 |
| --- | --- | --- |
| Invalid configuration | 自相矛盾的设置或用错接口 | priority 字段没配 provider |
| Not implemented | 适配器尚未支持的功能 | `lora_rank > 0` |
| Not validated | 未经验证的运行时组合 | `offload_train=true` |

三类都会在训练前拦下，**不是可以忽略的警告**。

#### 4.3.2 核心流程

```text
一个值的完整旅程（以 core.prompt_provider="priority" 为例）
  ├─ TOML [core] 节 → CORE_FIELDS 白名单（dataclasses.fields(CoreConfig) 的名字集）
  ├─ CoreConfig(**core) 构造 → __post_init__：
  │     choice 校验 provider ∈ {shuffle, remind, mixture, pools, priority}
  │     choice 校验 reward_reasoning_mode ∈ {required, none}（8cd7c9eb 轮新增，且排在最前）
  │     priority 三件套的类型检查（文本、非空权重表、正权重）
  ├─ RunConfig.validate()：
  │     resolved_miles() 归一化 miles 选项
  │     score_centering.validate(options, fp32_lm_head=...)（8cd7c9eb 轮新增挂载点）
  │     跨字段约束（provider 与 priority 字段、rollout_global_dataset、selection/replay 互斥…）
  └─ CLI 打印 "Invalid configuration: …" 并退出码 2
```

#### 4.3.3 源码精读

**错误工厂**。[open_instruct/miles/configuration/constraints.py:16-41](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/constraints.py#L16-L41)：`constraints.error(message, category)` 生成带类别前缀的 `InputError`；`NOT_IMPLEMENTED` 会追加「欢迎带测试的实现贡献」，`NOT_VALIDATED` 会追加「欢迎用聚焦 run 帮助验证」。约束文本放在这里而不是散落在校验器里，是为了让生成式文档（`scripts/miles/generate_docs.py`）不必解析 Python 代码就能引用同一份定义。

**原生校验选项清单的扩容**。[open_instruct/miles/configuration/constraints.py:233-238](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/constraints.py#L233-L238)：`NATIVE_VALIDATION_OPTIONS` 在 8cd7c9eb 轮加入五个 score-centering 选项——`rollout_top_logprobs_num`、`score_centering_is`、`score_centering_tis_clip`、`score_centering_mis_low`、`score_centering_mis_high`。这个清单声明哪些 `[miles]` 键属于「原生运行时会自行校验」的选项，open-instruct 侧的 CPU 预检据此放行并交给原生解析器复核。

**CoreConfig.__post_init__：reward_reasoning_mode 排在最前**。[open_instruct/miles/configuration/config.py:148-163](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/config.py#L148-L163)：本轮在 `__post_init__` 的第一行加入 `validation.choice(self.reward_reasoning_mode, "core.reward_reasoning_mode", ("required", "none"))`——写 `reward_reasoning_mode = "reqired"`（拼错）会在构造 `CoreConfig` 的瞬间被拦下，报 `core.reward_reasoning_mode must be one of 'required', 'none'`。`prompt_provider` 的合法取值维持五个（`shuffle/remind/mixture/pools/priority`），priority 三件套逐一检查——`prompt_priority_field` / `prompt_priority_source_field` 必须是非空文本，`prompt_priority_source_weights` 必须是非空的「源名 → 权重」表，每个权重要严格为正，权重总和也要严格为正（拒绝全零表）。

**score_centering.validate 的挂载点**。[open_instruct/miles/configuration/config.py:360-364](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/config.py#L360-L364)：`resolved_miles()` 归一化选项后立即调用 `score_centering.validate(options, fp32_lm_head=self.core.fp32_lm_head)`——在 Core 解析服务精度**之前**就把 score-centering 的资格条件查完（具体规则见 4.5.3）。这保证 `validate` / `plan` / `train` 三个入口拿到的是同一份判定。

**refresh 约束的放宽**。[open_instruct/miles/configuration/config.py:781-786](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/config.py#L781-L786)：refresh（分离式引擎刷新）此前硬性要求 `miles.use_tis=true`；8cd7c9eb 轮改为 `use_tis` **或** score-centering 的重要性采样（`score_centering_is` 为 `tis`/`mis`）任一即可：

```python
if not (options.get("use_tis", False) or score_centering.uses_importance_sampling(options)) or options.get(
    "use_rollout_logprobs", False
):
    raise constraints.error(
        "refresh requires miles.use_tis=true or score_centering_is=tis/mis, and miles.use_rollout_logprobs=false"
    )
```

**候选概率收集的跨字段校验**。[open_instruct/miles/configuration/validation.py:164-173](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/validation.py#L164-L173)：`runtime_values` 在 8cd7c9eb 轮新增一组检查——只要收集候选概率（`rollout_top_logprobs_num > 0`），就要求 `rollout_top_logprobs_num >= rollout_top_k`（对任意 loss 生效，不限于 score centering）；`rollout_top_k` 必须是 `-1` 或正整数。这条规则防止「过滤采样只看前 k 个候选、记录却不足 k 个」的静默错配。

**RunConfig.validate 中的 priority 联合约束**。[open_instruct/miles/configuration/config.py:534-551](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/config.py#L534-L551)：

- 反向约束：provider 不是 `priority` 时，任何 priority 字段被设置都报 `Priority settings require core.prompt_provider=priority`（防止字段拼错被静默忽略）；
- 正向约束（provider 为 `priority` 时）：
  1. `source_field` 与 `source_weights` 必须成对出现（要么都设要么都不设）；
  2. `prompt_priority_field` 必须提供；
  3. `rollout_global_dataset` 必须为 true（priority 提供者要全局数据集序）；
  4. 不能与冻结排除表（`[selection]`）或 prompt replay 组合。

**`num_epoch` 的正整数检查**。[open_instruct/miles/configuration/config.py:649-651](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/config.py#L649-L651)：`num_epoch` 与 `num_rollout`、`rollout_batch_size` 等同列「必须为正整数」名单，传 `0`、负数或布尔值都会被拒——注意 Python 里 `True` 是 `int` 子类，所以这里用 `type(options[name]) is not int` 严格排除了布尔。

**miles 选项快照校验**。[open_instruct/miles/configuration/options.py:95-122](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/options.py#L95-L122)：`resolve_option` 用 options.json（原生解析器的快照）确认选项名存在并归一化别名；未知选项报 `Unknown MILES option: xxx; did you mean yyy?`。这让 `[training]` 节里的拼写错误（如 `num_epochx`）也能在 CPU 上立刻被抓到。

**CLI 的 validate 分支**。[open_instruct/miles/__main__.py:96-104](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/__main__.py#L96-L104)：结构化文件的 `validate` 实际执行 `config.plan()`——也就是说 validate 检查的范围等于 plan 减去 JSON 输出，连物理 GPU 分配（topology.plan）一起验了；低层 `[core]`/`[miles]` 文件的 validate 则调用原生解析器（`workflow.parse_runtime`）。两种情况都会打印一行确认语，输入和运行时检查留到 `train` 阶段。

#### 4.3.4 代码实践

**实践 C：体验错误三分类**（CPU）。

1. 实践目标：区分三类错误信息的措辞差异，并观察 reward_reasoning_mode 的 choice 校验。
2. 操作步骤：在实践 A 的 `my-run.toml` 基础上分别做四次 `validate`：
   - `--set core.prompt_provider="priority"`（缺 `prompt_priority_field`，Invalid configuration）；
   - `--set core.reward_reasoning_mode="reqired"`（choice 校验，见 4.5）；
   - `--set miles.lora_rank=8`（Not implemented）；
   - `--set miles.offload_train=true`（Not validated）。
3. 需要观察的现象：四条错误信息的前缀分别是 `Invalid configuration:`（×2，第二条形如 `core.reward_reasoning_mode must be one of 'required', 'none'`）、`Not implemented:`、`Not validated:`，后两类各自带一句「欢迎贡献/欢迎验证」的尾注。
4. 预期结果：四次都以退出码 2 失败，错误信息可读且指向具体字段。若与预期不符，标「待本地验证」并回读 [constraints.py:16-41](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/constraints.py#L16-L41) 与 [config.py:148-149](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/config.py#L148-L149)。

#### 4.3.5 小练习与答案

**练习 1**：为什么 priority 的联合约束放在 `RunConfig.validate` 而不是 `CoreConfig.__post_init__`？

**答案**：`__post_init__` 只看得到 CoreConfig 自己的字段；而「provider=priority 需要 `rollout_global_dataset=true`」「不能与 selection_table / prompt_replay 组合」这些规则横跨 core 与 miles 两个命名空间，必须在 `RunConfig.validate`（通过 `resolved_miles()` 拿到归一化后的 miles 选项）里检查。

**练习 2**：`STRUCTURED_FIELDS` 除了校验还有什么用途？

**答案**：它是文档、校验与文档覆盖率检查共用的单一事实源——`constraints.py` 的模块注释说明这些定义驱动 validation 和生成的参考文档（`docs/miles/configuration-intro.md` 的表格由 `scripts.miles.generate_docs` 生成），改一处即可同时影响校验行为与文档。

**练习 3**：`type(value) is not int` 与 `isinstance(value, int)` 在这里有什么区别？

**答案**：`isinstance(True, int)` 是 True，布尔会混过整数检查；`type(True) is not int` 为 True，能拒掉布尔。校验代码普遍用 `type(...) is int`/`type(...) is bool` 的严格形式（如 [run_spec.py:324](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/run_spec.py#L324)、[config.py:650](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/config.py#L650)），防止 `num_epoch=true` 之类被静默当成 1。

---

### 4.4 采样与预算：priority 提供者、over_length 与 epoch 预算

#### 4.4.1 概念说明

上轮（`896a734 → bae4209`）引入的三组变化，本质都是「把一个以前只能在运行时发现（或被静默忽略）的决定，提前到配置期用约束固定下来」；本轮又给第一组补上了池采样侧的对应物：

1. **priority 提示提供者**：数据行带有历史奖励优先级元数据（组 1–4），提供者按组序遍历数据。配置层的工作是用三件套字段 + 联合约束，把「用 priority 就必须说清字段名、必须全局数据集、不能叠排除/回放」说死。本轮新增的 `data.sampling.priority` 把同一套分层引入**池采样**（只排序池内新提示，不切换提供者），并新增与 replay 的互斥。
2. **`data.over_length`**：准备阶段遇到超过提示预算的训练行时，`"fail"`（默认）整体失败，`"exclude"` 跳过并记录计数。配置层负责取值枚举校验与字段入表。
3. **epoch 预算**：`num_epoch` 成为与 `num_rollouts` 平级的预算选择，编译层负责互斥检查与「没有总 rollout 数时的最终存档默认」。

#### 4.4.2 核心流程

priority 提供者的组序遍历（运行时语义，配置只是入口）：

```text
每个 sampler epoch：
  组 1（历史奖励有分歧）→ 组 2（未知/无历史）→ 组 3（全零且频繁截断）→ 组 4（其他常数奖励）
  ├─ 组内：data.seed 固定确定性洗牌（data.shuffle=false 则保持原序）
  ├─ 可选组内混源加权：按 source 权重抽下一个源；耗尽的源被跳过并重新归一化权重
  ├─ 空组跳过；请求可以跨组边界
  └─ 预算用普通 num_epoch / num_rollouts；priority 不强加一遍历即停
```

epoch 预算的编译期 → 运行期接力：

```text
编译期（本讲）                     运行期（原生 MILES）
num_epoch=2 ──透传 --num-epoch──►  加载准备数据后换算
save_final_checkpoint=True  ──►   最后一次完成的更新落盘
（不设 num_rollout，不造 save_interval）
```

#### 4.4.3 源码精读

**priority 的推荐配置写法**。[docs/miles/data-and-evaluation.md:239-243](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/data-and-evaluation.md#L239-L243)（最小形式）与 [docs/miles/data-and-evaluation.md:265-271](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/data-and-evaluation.md#L265-L271)（带组内混源加权）：

```toml
[core]
prompt_provider = "priority"
prompt_priority_field = "reward_priority_olmo35_sft_hero"
# 可选：组内按源加权
prompt_priority_source_field = "workbench.source.candidate_id"
prompt_priority_source_weights = { C036 = 1.0, C046 = 2.0 }
```

组 1–4 的历史证据定义见 [docs/miles/data-and-evaluation.md:245-250](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/data-and-evaluation.md#L245-L250)：组 1 = 至少一个完整打分组奖励有分歧；组 2 = 无匹配历史；组 3 = 全零且过半命中 token 上限；组 4 = 其他常数奖励。组的划分在训练中保持固定，不影响打分，也不与在线零方差过滤互相干扰（[docs/miles/data-and-evaluation.md:290-292](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/data-and-evaluation.md#L290-L292)）。

**epoch 预算的文档契约**。[docs/miles/data-and-evaluation.md:294-311](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/data-and-evaluation.md#L294-L311)：`[training] num_epoch = 2` 或 `num_rollouts` 二选一；两者都设会被结构化 run 拒绝（正是 [run_spec.py:736-740](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/run_spec.py#L736-L740) 的报错）；都不设沿用 100 rollouts 默认。换算与「epoch 预算默认保存最终更新、显式 `save_interval` 仍可启用周期保存」的约定也写在这里。

**over_length 的运行时语义边界**。[docs/miles/data-and-evaluation.md:531-560](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/data-and-evaluation.md#L531-L560)：

- 排除只作用于**训练**源；held-out（评测）提示超长仍然失败；
- 保留提示文本、顺序、身份与配置的 source 比例/pool 权重；命名任务的 `train_count` 只选初始候选行，排除后不回填；
- 其余校验（token-ID/模板兼容、verifier 约束、训练/held-out 重叠检查）对包括被排除行在内的所有候选行照常执行；
- `prepared/data/manifest.json` 的 `prompt_length` 记录生效策略、整数提示上限、training 作用域，以及按 source 的 `original/excluded/retained` 计数（池计数在有池配置时一并记录）。

**池采样优先级的语法与互斥**。[open_instruct/miles/rollout/pool_sampling.py:24-35](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/rollout/pool_sampling.py#L24-L35)：`data.sampling.priority` 只接受 `field` 一个键（非空元数据字段名），且要求 `policy` 为 `uniform` 或 `exposure`；编译期的 replay 互斥见 4.2.3。运行时语义与测量记录详见 u8-l5 与 u8-l11。

**测试如何钉住这些行为**：

- [open_instruct/test_miles_epoch_budget.py:14-44](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/test_miles_epoch_budget.py#L14-L44)：断言 epoch 版编译产物含 `--num-epoch` 不含 `--num-rollout`/`--save-interval`、`core.save_final_checkpoint` 为真、`training` 与 `miles` 两个位置设双预算都报 `Choose num_epoch or num_rollouts`、`save_checkpoints=false` 时不设最终存档、`num_epoch` 传 `0/-1/True` 被拒。
- [open_instruct/test_miles_priority_provider.py:125-146](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/test_miles_priority_provider.py#L125-L146)：断言 priority 配置可编译、缺 `prompt_priority_field` 报 requires、只设 priority 字段不设 provider 报 require、缺 `rollout_global_dataset` 与叠加 selection/replay 分别报错。
- [open_instruct/test_miles_over_length.py:33-57](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/test_miles_over_length.py#L33-L57)：断言 `"exclude"` 策略下保留行、`manifest["prompt_length"]` 的 policy/max/scope/sources 计数、原始输入字节不被改写、结果可复现且改策略或改上限会触发 resume 契约变化报错。
- [open_instruct/test_miles_pool_priority.py](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/test_miles_pool_priority.py)：池采样优先级的断言（加载期语法、编译期 replay 互斥与运行时组序），详见 u8-l11。

#### 4.4.4 代码实践

**实践 D：三个「故意写错」实验**（本讲综合实践的子任务，可独立做）。

1. 实践目标：亲眼看到三组约束的报错文本，建立肌肉记忆。
2. 操作步骤（在实践 A 的 `my-run.toml` 上依次做，每次先恢复）：
   - 实验 1：`--set core.prompt_provider="priority"`（不提供 `prompt_priority_field`），记录报错；再补上 `--set core.prompt_priority_field="reward_priority_demo"` 看是否通过（注意还需满足单一数据输入与 `rollout_global_dataset`，tiny/dev.toml 的 `[data]` 用 `tasks` 是允许的，因为 provider 由你显式设置而 `data.sources` 未用，不会触发 put 冲突）；
   - 实验 2：`--set data.over_length="exclud"`，记录报错；
   - 实验 3：`--set training.num_epoch=2 --set training.num_rollouts=4`，记录报错。
3. 需要观察的现象：
   - 实验 1 应报 `Invalid configuration: Priority sampling requires core.prompt_priority_field`；
   - 实验 2 应报 `data.over_length must be one of 'fail', 'exclude'; got 'exclud'.`；
   - 实验 3 应报 `Choose num_epoch or num_rollouts; MILES would otherwise ignore num_epoch`。
4. 预期结果：三条都与源码/测试断言一致。实验 1 补字段后若因其他联合约束失败（例如与 `[selection]` 冲突），对照 [config.py:534-551](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/config.py#L534-L551) 逐条排查。
5. 无法运行时标「待本地验证」，以 [test_miles_priority_provider.py:125-146](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/test_miles_priority_provider.py#L125-L146) 等测试断言作为行为依据。

#### 4.4.5 小练习与答案

**练习 1**：priority 组内混源加权中，一个源的行被抽完了会发生什么？

**答案**：该源被跳过，剩余源的权重重新归一化（[docs/miles/data-and-evaluation.md:277-278](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/data-and-evaluation.md#L277-L278)）。权重只影响抽取顺序与早期预算曝光，不影响一个完整 sampler epoch 内每行恰好出现一次。

**练习 2**：`over_length="exclude"` 会不会让某个 `data.sources` 配置源被「掏空」？

**答案**：会失败。每个配置的 `data.sources` 条目（及每个已填充的 pool）至少要保留一行以维持采样分配，否则准备阶段在发布数据前失败（[docs/miles/data-and-evaluation.md:556-560](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/data-and-evaluation.md#L556-L560)）。

**练习 3**：池采样优先级（`data.sampling.priority`）与独立 priority provider 的分工是什么？

**答案**：两者共享同一套 1–4 历史分层定义（`pool_sampling.priority_value` 与独立 provider 一致），但池采样优先级只在 `uniform`/`exposure` 策略下**排序每个池内的新提示**，不改变提供者本身；`core.prompt_provider="priority"` 则把整个提示提供者切换为按组序遍历。前者与 replay 互斥（[run_spec.py:801-802](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/run_spec.py#L801-L802)），后者与 selection/replay 互斥（[config.py:549-550](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/config.py#L549-L550)）。

---

### 4.5 编译期新字段：判分输入声明、score-centering 预检与评估采样默认

#### 4.5.1 概念说明

`bae4209 → 8cd7c9eb` 轮进入配置系统的三组内容（此后保持稳定）：

1. **判分输入声明**：`core.reward_final_answer_only`（字段此前已存在，本轮获得正式语义注释并进入全部维护示例）与新增的 `core.reward_reasoning_mode`。两者共同回答一个问题——**验证器应该看到模型输出的哪一段**：是完整历史文本，还是思考段之后的「交付响应」；本 run 是否预期思考段。库默认保持 `reward_final_answer_only=false`（旧行为），维护示例显式开启 `true` 以声明「按交付响应判分」；`reward_reasoning_mode` 默认 `"required"`（要求 `</think>` 闭合），非思考 run 必须显式写 `"none"`。注册表里的 `grading` 声明与消费者迁移的完整故事在 u8-l12，本讲只讲这两个 Core 字段的配置入口与校验。
2. **score-centering 参数族**：`[miles] loss_type = "score_centering"` 选择原生分数中心化目标，配合 `score_centering_is`（`none`/`tis`/`mis`）、`score_centering_tis_clip`、`score_centering_mis_low/high` 与 `rollout_top_logprobs_num`（候选概率记录数）。新文件 `score_centering.py` 在 CPU 上预检十余条资格规则，编译映射见 4.2.3。
3. **评估采样默认对齐**：in-loop 评估默认温度从 0.0（贪心）改为 1.0，新增 top-p 0.95 默认，响应预算上限 32768——与维护示例的评估协议对齐，显式覆盖仍然胜出。

#### 4.5.2 核心流程

score-centering 的编译期决策树：

```text
[miles] loss_type == "score_centering"？
 ├─ 否 → 旧路径：异步且无 rollout logprobs 时默认 use_tis=true
 └─ 是 → resolved_miles() 调 score_centering.validate：
       ├─ trainer/core fp32_lm_head 必须 true（保守资格策略）
       ├─ advantage_estimator 必须 grpo
       ├─ 与 use_tis / custom_tis / use_opsm / true_on_policy_mode /
       │  投机解码 / multi_lora / use_opd / 自定义转换器 全部互斥
       ├─ score_centering_is ∈ {none, tis, mis}；tis_clip > 0；0 < mis_low ≤ mis_high
       └─ sglang_enable_fp32_lm_head 显式 false 被拒
       编译后果：
       ├─ async.off_policy_correction="tis" → put score_centering_is="tis"（不再 put use_tis）
       ├─ use_tis 自动默认不再开启
       ├─ 异步锚定：use_tis ∨ rollout_logprobs ∨ score_centering_is∈{tis,mis}
       └─ refresh 资格：use_tis 或 score-centering IS 任一即可
```

候选概率收集的通用约束（对任意 loss 生效）：`rollout_top_logprobs_num > 0` 时必须 `>= rollout_top_k`。

#### 4.5.3 源码精读

**两个 Core 字段的定义与校验**。[open_instruct/miles/configuration/config.py:43-51](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/config.py#L43-L51)：

```python
reward_zero_truncated: bool = True
# False: historical consumer dispatch on full text. True: shared selection
# of the text after thinking; trusted per-verifier process exceptions apply.
# Selection preserves answer whitespace and does not extract answer wrappers.
reward_final_answer_only: bool = False
# Only used by final-answer grading. Non-thinking runs must opt into none.
reward_reasoning_mode: str = "required"
```

注释即契约：`false` 走历史消费者路径（完整文本分发）；`true` 由共享选择器切一次思考边界，把交付响应交给验证器（保留空白、不抽取答案包装），可信注册表里声明了例外的 process 验证器仍看全文。`reward_reasoning_mode` 只被 final-answer 判分使用：`"required"` 要求思考段闭合，`"none"` 接受无标签的完整响应（并把推理标签视作控制语法拒绝）。取值校验在 [config.py:149](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/config.py#L149)（`__post_init__` 第一行）。语义文档见 [docs/miles/grading.md:7-17](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/grading.md#L7-L17)：库默认保持 false，维护的 dev/node/three-node 示例显式开启；非思考 run 用 `"none"` 显式声明。

**score_centering.py 全文只有三个函数**（[open_instruct/miles/configuration/score_centering.py:1-48](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/score_centering.py#L1-L48)）：

- `is_enabled(options)`（[L7-8](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/score_centering.py#L7-L8)）：`loss_type == "score_centering"` 即启用；
- `validate(options, *, fp32_lm_head)`（[L11-44](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/score_centering.py#L11-L44)）：docstring 写明时机——「在 Core 解析服务精度之前」。规则依次为：FP32 head 必须 true（[docs/miles/core.md:378-389](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/core.md#L378-L389) 说明这是**保守的资格策略**而非数学要求：上游只自动开启 rollout 侧 FP32 head，不强制 trainer 侧，但未验证的组合先拒之门外）；候选数为整数；`score_centering_is` 枚举；`tis_clip` 严格为正；`0 < mis_low ≤ mis_high`；`advantage_estimator=grpo`；与十个旧开关互斥（`use_tis`、`custom_tis_function_path`、`use_opsm`、`true_on_policy_mode`、`recompute_logprobs_via_prefill`、`sglang_speculative_algorithm`、`custom_pg_loss_reducer_function_path`、`multi_lora`、`use_opd`、`custom_convert_samples_to_train_data_path`）；`sglang_enable_fp32_lm_head=False` 被拒；
- `uses_importance_sampling(options)`（[L47-48](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/score_centering.py#L47-L48)）：启用了且 `score_centering_is` 为 `tis`/`mis`——异步锚定与 refresh 资格都用它判断。

**推荐写法与文档**。[docs/miles/core.md:353-365](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/core.md#L353-L365) 给出最小配置：

```toml
[trainer]
fp32_lm_head = true

[miles]
loss_type = "score_centering"
rollout_top_logprobs_num = 20
score_centering_is = "tis"
score_centering_tis_clip = 2.0
```

并明确：省略旧版 `use_tis`，编译器自会选 score centering 的加权；显式 `async.off_policy_correction="tis"` 也选择原生 TIS 模式；同步 run 可用无加权（`score_centering_is="none"`）的中心化。

**测试钉住的行为**。[open_instruct/test_miles_score_centering.py:27-37](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/test_miles_score_centering.py#L27-L37) 断言 refresh 接受 `use_tis=False` + score-centering TIS；[L40-67](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/test_miles_score_centering.py#L40-L67) 参数化断言八类非法配置各自的报错（`use_tis` 同开、FP32 head 缺失、候选数为 0、clip 为 0、`mis_low > mis_high`、非 grpo 优势、自定义转换器、refresh 无 IS）；[L91](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/test_miles_score_centering.py#L91) 起断言 trainer 与 core 两个入口都要求显式 FP32 head。更底层的约束单测在 [tests/miles/test_score_centering.py](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/tests/miles/test_score_centering.py)。

**维护示例的基线声明**。[configs/miles/examples/README.md:31-42](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/README.md#L31-L42)：示例必须保持 token-mean loss 加权（`miles.calculate_per_token_loss=true`）、截断响应零奖励（`core.reward_zero_truncated=true`）与 presented-response grading（`core.reward_final_answer_only=true`）；吞吐实验不得静默改动前两条学习策略设置；示例统一声明 `reward_reasoning_mode="required"`，非思考策略/模板必须显式用 `"none"`；process 与边界格式奖励需要声明的例外见 grading.md。[README.md:313-327](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/README.md#L313-L327) 记录评估采样协议：策略评估用 temperature 1.0 / top-p 0.95，后台任务显式请求 32768 响应 token，in-loop 评估默认对齐同一采样并把继承的响应预算截到 32K，显式覆盖仍然胜出；裁判采样与训练 rollout 采样相互独立。

**示例目录按模型家族组织**。[configs/miles/examples/](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/) 分为 `tiny/`（dev/node/three-node 三份学习与冒烟示例）与 `small/`（本轮起为三份实测 OLMo 3.5 Small B300 配方：two-node-mech、four-node、eight-node，`small/dev.toml` 占位副本已删除）。家族划分表见 [README.md:8-15](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/README.md#L8-L15)，实测配方的完整布局与吞吐审计见 u8-l15，本讲只在 4.6 关注它们共享的学习基线。

#### 4.5.4 代码实践

**实践 E：score-centering 配置的 plan 与三类非法配置**（纯 CPU）。

1. 实践目标：看到 score-centering 编译产物长什么样，并亲手触发三类预检报错。
2. 操作步骤：
   - 在实践 A 的 `my-run.toml` 上运行：
     ```bash
     uv run python -m open_instruct.miles plan my-run.toml \
       --set trainer.fp32_lm_head=true \
       --set 'miles.loss_type="score_centering"' \
       --set miles.rollout_top_logprobs_num=20 \
       --set 'miles.score_centering_is="tis"' > plan-sc.json
     ```
   - 在 `plan-sc.json` 的 runtime 选项/argv 中找到 `--loss-type score_centering`、`--score-centering-is tis`、`--rollout-top-logprobs-num 20`，并确认**没有** `--use-tis`；
   - 依次制造三类非法配置并记录报错：
     1. 去掉 `trainer.fp32_lm_head=true`；
     2. 加 `--set miles.use_tis=true`；
     3. `--set miles.rollout_top_logprobs_num=2 --set miles.rollout_top_k=8`。
3. 需要观察的现象：
   - 报错 1 应含 `Score centering requires trainer.fp32_lm_head=true`；
   - 报错 2 应含 `Score centering is incompatible with miles.use_tis`；
   - 报错 3 应含 `rollout_top_logprobs_num must be >= miles.rollout_top_k`（这条来自 validation.py 的通用候选检查，对任意 loss 生效）。
4. 预期结果：与 [score_centering.py:11-44](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/score_centering.py#L11-L44)、[validation.py:164-173](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/validation.py#L164-L173) 及 [test_miles_score_centering.py:40-67](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/test_miles_score_centering.py#L40-L67) 的断言一致。
5. 无法运行时标「待本地验证」，以测试文件断言作为行为依据。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `score_centering.validate` 要挂在 `resolved_miles()` 里，而不是 `RunSpec.compile()` 里？

**答案**：`resolved_miles()` 是**所有入口**（validate/plan/train，包括低层 `[core]`+`[miles]` 文件直接构造 RunConfig 的路径）共同经过的归一化点（[config.py:360-364](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/config.py#L360-L364)）。挂在 compile 里只能保护结构化路线；挂在这里连绕过 RunSpec 直接构造 `RunConfig` 的调用也一并查到，且时机在 Core 解析服务精度之前，避免半初始化状态。

**练习 2**：`reward_final_answer_only` 的库默认为什么保持 `false`，而维护示例全部显式写 `true`？

**答案**：`false` 保留历史消费者分发（完整文本、边界抽取、token 输入、门控与裁判载荷都不变），保证存量 run 与注册表 `grading` 声明在被校验之外**不改变旧打分**（[docs/miles/grading.md:7](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/grading.md#L7)）。示例显式开启则是声明新 run 的推荐判分口径（按交付响应），把「改判分输入」变成一个必须显式做出的决定，而不是静默迁移。

**练习 3**：评估采样默认从「贪心 0.0」改成「1.0 / 0.95」后，想恢复贪心评估怎么办？

**答案**：默认值只经 `setdefault` 填入（[run_spec.py:868-869](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/miles/configuration/run_spec.py#L868-L869)），显式写优先。在 `[inference]`（或 `--set`）里给出 `eval_temperature = 0.0` 即可覆盖；`eval_top_p`、`eval_max_response_len` 同理。README 同时提醒：任务专属协议（如贪心 RULER、BFFL）应逐任务显式声明，不要全局覆盖整套 suite 的采样。

---

### 4.6 示例学习基线（一）：token-mean loss 与 Tiny batch/LR（#2271/#2280）

#### 4.6.1 概念说明

上轮（`28c181666 → 2f7889da9`，#2271 与 #2280）configuration 包的 Python 源码**一行未动**，但「维护示例 = 配置基线」这个契约本身被改写了。MILES 的约定是：demo configs 是配置选项的基线（除非另行说明），做验证 run 要从相应 demo 出发并记录有意偏离。因此示例 TOML 里的每一个学习策略字段，都是后续所有实验的对照点：

1. **损失加权翻转**：全部维护示例（tiny/dev、tiny/node、tiny/three-node、small 三份配方）从 per-response 翻转为 token-mean——`[miles] calculate_per_token_loss = true`。这条设置是**示例层声明**而非库默认：省略该字段时，库默认仍是 response-mean（`false`），保证既有 run 文件的客观目标不被静默改变。想改回去必须显式写 `false`，这本身就成了一个「被记录的实验」。
2. **Tiny 学习基线**：node 与 three-node 两份学习示例统一为 256 prompts × 8 responses（即每个 collection 2048 个样本）、`global_batch_size = 2048`（恰好一个 optimizer step 一个 collection）、恒定 LR 6e-6 无 warmup、policy lag 12。此前是 128 × 8、batch 1024、无显式 LR。
3. **small 家族换代**：`small/dev.toml`（tiny/dev 的占位拷贝）被删除，换入三份实测 OLMo 3.5 Small（A4B，KDA+MoE）B300 配方（two-node-mech / four-node / eight-node），它们**共用 Tiny 学习基线**。布局与吞吐审计属 u8-l15，本讲只关心其配置层含义：示例家族按模型划分（Tiny 力学/学习示例 vs Small B300 实测配方），而不是按规模放大同一份文件。
4. **历史计时的告诫**：文档明确记录 LR/batch 探索用的是 response-mean loss；保留另行选定的 token-mean 示例目标是**工程选择，不是联合最优**；已报告的 Small 计时数字用的是 batch 1024、LR 1e-6、response-mean——新旧数字不可直接对比。

#### 4.6.2 核心流程

`calculate_per_token_loss` 在编译链中的位置（它是一个透传的 miles 原生选项，不进 FIELD_MAP、不参与跨字段约束）：

```text
TOML [miles] calculate_per_token_loss = true
  │ put("miles.calculate_per_token_loss", ...)        # options.resolve_option 快照校验
  ▼
RunConfig.miles["calculate_per_token_loss"] = True
  │ arguments() 编码为命令行旗标 --calculate-per-token-loss
  ▼
原生 MILES 运行时按 token-mean 归一化损失

省略该字段 ──► 旗标不出现在 argv ──► 运行时默认 response-mean（兼容层）
显式写 false ─► 旗标同样不出现（argv 只在 true 时携带）── 显式实验
```

Tiny 学习基线的字段组合（node 与 three-node 除拓扑外共享，small 三份配方同样共用）：

| 字段 | 取值 | 位置 |
| --- | --- | --- |
| `learning_rate` | 6e-6（恒定、无 warmup） | `[training]` |
| `rollout_batch_size` × `samples_per_prompt` | 256 × 8 = 2048 样本/collection | `[inference]` |
| `global_batch_size` | 2048（= 1 个 optimizer step / collection） | `[inference]` |
| `calculate_per_token_loss` | true（token-mean） | `[miles]` |
| `max_policy_lag` | 12 | `[core]` |

#### 4.6.3 源码精读

**Tiny 学习示例的基线字段**。[configs/miles/examples/tiny/node.toml:47-51](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/tiny/node.toml#L47-L51) 的 `[training]` 上轮新增 `learning_rate = 6e-6`（此前学习示例不显式设 LR），本轮再把 `save_interval` 从 25 收紧到 10（见 4.7）；[node.toml:95-97](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/tiny/node.toml#L95-L97) 把 `rollout_batch_size` 从 128 提到 256、`global_batch_size` 从 1024 提到 2048（`samples_per_prompt` 维持 8）；[node.toml:137-138](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/tiny/node.toml#L137-L138) 把基线翻转为 token-mean，注释同步从「Keep response weighting independent of generation length」改为「Weight contributing tokens equally across responses」。three-node 的对应位置：[three-node.toml:62-66](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/tiny/three-node.toml#L62-L66)（`[training]`）、[three-node.toml:241-243](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/tiny/three-node.toml#L241-L243)（`[inference]`）、[three-node.toml:278-279](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/tiny/three-node.toml#L278-L279)（`[miles]`）；其完成组缓冲注释也换算为新基线（[three-node.toml:292](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/tiny/three-node.toml#L292)：四个 collection 缓冲 = 1,024 组 / 8,192 样本）。连单卡冒烟示例也翻转了：[tiny/dev.toml:84-87](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/tiny/dev.toml#L84-L87)。

**Small 配方共用基线**。[configs/miles/examples/small/two-node-mech.toml:82-88](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/small/two-node-mech.toml#L82-L88) 的 `[training]` 同样写 `learning_rate = 6e-6`（力学检查长度 `num_rollouts = 4`、`save_checkpoints = false`——力学示例保持无 checkpoint，见 4.7）；[two-node-mech.toml:99-101](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/small/two-node-mech.toml#L99-L101) 与 [two-node-mech.toml:148-149](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/small/two-node-mech.toml#L148-L149) 复用 256 × 8 / batch 2048 / token-mean。文件头注释（[L1-L13](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/small/two-node-mech.toml#L1-L13)）明确「Baseline matches Tiny…The historical mechanics run used batch 1,024, LR 1e-6 and response-mean loss」。

**文档契约的三处改写**。

- [docs/miles/development-defaults.md:18-21](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/development-defaults.md#L18-L21) 新增「Tiny learning batch and LR」条目：256 × 8、batch 2,048、恒定 6e-6 无 warmup，并写明「Tiny 的 LR/batch 探索用的是 response-mean；保留另行选定的 token-mean 示例目标是工程选择，不是测得的联合最优」。
- [docs/miles/development-defaults.md:23](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/development-defaults.md#L23) 的「Loss weighting」条目反转为 token mean：吞吐试验保持 demo 目标，response-mean 变成显式的学习策略覆盖；省略的库设置保留 response-mean 以兼容。
- [docs/miles/grpo.md:88-107](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/grpo.md#L88-L107) 的「Truncated responses」一节把示例基线改写为 token-mean（`calculate_per_token_loss = true` 的 TOML 片段就在这里）；[docs/miles/run-controls.md:104](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/run-controls.md#L104) 的「Policy objective」行同步（「demo baseline is token-mean loss; omitted library settings retain response-mean」）。[configs/miles/examples/README.md:21-25](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/README.md#L21-L25) 则记录三份 Small 配方共用 Tiny 学习基线、历史计时用的是旧基线。

**测试如何钉住双向语义**。[open_instruct/test_miles_demo_baseline.py:12-31](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/test_miles_demo_baseline.py#L12-L31) 是基线的正面断言：参数化（node/three-node）加载并编译后，`resolved_miles()` 必须给出 `rollout_batch_size == 256`、`global_batch_size == 2048`、`lr == 6e-6`、`lr_decay_style == "constant"`、`lr_warmup_iters == 0`、`calculate_per_token_loss is True`，且 argv 里**出现** `--calculate-per-token-loss`。反向兼容由 [test_miles_demo_baseline.py:54-68](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/test_miles_demo_baseline.py#L54-L68) 钉住：一个完全没有 `[miles]` 节的最小 run，编译后 `calculate_per_token_loss` 必须是 `False`（省略 ≠ 继承示例基线）；[test_miles_demo_baseline.py:71-78](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/test_miles_demo_baseline.py#L71-L78) 再钉住显式实验通道：`--set miles.calculate_per_token_loss=false` 后旗标从 argv 消失。[test_miles_demo_baseline.py:81-101](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/test_miles_demo_baseline.py#L81-L101) 用 `glob` 参数化扫 `configs/miles/examples/small/*.toml`，断言三份 B300 配方共用 Tiny 学习基线（含 `optimizer_steps_per_collection == 1`）——**新增 small 示例文件不改测试就会自动被扫到**，这是「家族基线」的机械保证。

#### 4.6.4 代码实践

**实践 F：观察基线翻转的编译产物与双向覆盖**（纯 CPU）。

1. 实践目标：在 plan 产物中亲眼看到 token-mean 基线与 Tiny batch/LR，并验证「显式 false / 省略」两条路径的 argv 差异。
2. 操作步骤：
   - 复制 `configs/miles/examples/tiny/node.toml` 为 `my-run.toml`（模型与数据路径换成占位即可，plan 不检查文件存在性），运行：
     ```bash
     uv run python -m open_instruct.miles plan my-run.toml > plan-baseline.json
     ```
   - 在 `plan-baseline.json` 的 runtime argv 中找到：`--calculate-per-token-loss`、`--rollout-batch-size 256`、`--global-batch-size 2048`、lr 6e-6、`lr_decay_style` 为 constant、warmup 0；
   - 再运行一次带覆盖的版本并对比：
     ```bash
     uv run python -m open_instruct.miles plan my-run.toml \
       --set miles.calculate_per_token_loss=false > plan-resp.json
     grep -c calculate-per-token-loss plan-baseline.json plan-resp.json
     ```
     （`plan-baseline.json` 应命中 1 次、`plan-resp.json` 应命中 0 次；也可以直接打开两份 JSON 目视对比 argv）；
   - 运行测试基线：`uv run pytest open_instruct/test_miles_demo_baseline.py -q`，记录四类用例（Tiny 基线、省略兼容、显式覆盖、Small 配方扫描）各断言了什么。
3. 需要观察的现象：`plan-baseline.json` 的 argv 含 `--calculate-per-token-loss`；`plan-resp.json` 不含；测试四类用例全绿，其中 Small 扫描用例的参数化 id 是 `eight-node`、`four-node`、`two-node-mech`（按文件名排序）。
4. 预期结果：与 [test_miles_demo_baseline.py:12-31](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/test_miles_demo_baseline.py#L12-L31)、[L71-78](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/test_miles_demo_baseline.py#L71-L78) 的断言一致。
5. 无法运行时（未装依赖/GPU 栈）标「待本地验证」，以测试文件断言作为行为依据。

#### 4.6.5 小练习与答案

**练习 1**：为什么所有示例都写 `calculate_per_token_loss = true`，而库默认（省略时）仍保持 `false`？

**答案**：这是「示例基线」与「向后兼容」的分工。省略的字段必须不改变既有 run 文件的客观目标——如果把库默认也翻转，所有没写这个字段的历史 run 复跑时目标会静默改变（response-mean → token-mean），对照实验全部失效。所以翻转只发生在维护示例（新 run 的推荐起点）里，由 [test_miles_demo_baseline.py:54-68](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/test_miles_demo_baseline.py#L54-L68) 钉住省略路径仍为 False，由 [L71-78](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/test_miles_demo_baseline.py#L71-L78) 保证想用 response-mean 必须显式声明（从而被记录为实验偏离）。

**练习 2**：node.toml 的 `rollout_batch_size = 256`、`samples_per_prompt = 8`、`global_batch_size = 2048` 三个数如何联动？

**答案**：每个 collection 生成 256 × 8 = 2,048 个样本；优化器 batch 也是 2,048，所以每个 collection 恰好一个 optimizer step（Small 配方测试直接断言 `optimizer_steps_per_collection == 1`，[test_miles_demo_baseline.py:95](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/test_miles_demo_baseline.py#L95)）。改 batch 2048 → 1024 而不改 rollout_batch_size，就变成两步一个 collection，采样与更新的节奏随之改变——这正是基线要求成组调整的原因。

**练习 3**：为什么文档要专门记录「Tiny 的 LR/batch 探索用的是 response-mean」「已报告的 Small 计时用的是 batch 1024 / LR 1e-6 / response-mean」？

**答案**：防止把不同基线下的数字直接对比。LR 6e-6 与 batch 2048 是在 response-mean 目标下探索选出的；现在示例保留 token-mean 是工程选择而非联合最优（[development-defaults.md:18-21](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/development-defaults.md#L18-L21)）。同理，B300 配方的历史吞吐计时在旧目标与旧 batch 下测得，切到新基线后吞吐数字需重测（[README.md:21-25](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/README.md#L21-L25)，另见 u8-l15 的 Recipe alignment）。

---

### 4.7 示例学习基线（二）：有界恢复保存与里程碑（#2289/#2293）

#### 4.7.1 概念说明

#2289（`27f868159`，"use bounded recovery saves and preserved milestones in learning starters"）把「示例 = 基线」契约扩展到 checkpoint 维度。此前四个学习模板的保存策略各自为政：Tiny `node` 是 `save_interval = 25` 加 `checkpoint_keep_last = 2`（无永久里程碑、无最终存档声明），Small four/eight-node 更是**纯计时配置**——`save_checkpoints = false` 且 `auto_resume = false`，一次抢占就意味着从零重来。#2289 之后：

1. **四个学习模板统一为有界恢复保存**：`tiny/node`、`tiny/three-node`、`small/four-node`、`small/eight-node` 一律 `save_interval = 10`（每 10 个优化器更新保存一次）、`checkpoint_keep_last = 2`（滚动保留最近 2 份恢复保存）、`checkpoint_keep_every`（永久里程碑间隔，node 与 Small 为 50、three-node 为 100）、`save_final_checkpoint = true`（结束时追加最终存档）。
2. **Small four/eight-node 整体翻转**：从「禁用保存 + 不自动恢复」变为「启用保存 + `auto_resume = true`」，把 30 更新的起步预算变成可抢占续跑的预算。
3. **力学与冒烟示例保持独立**：`small/two-node-mech`（4 更新力学检查）继续 `save_checkpoints = false`、`auto_resume = false`；`tiny/dev` 维持自己的 `save_interval = 2`。短检查不需要恢复能力，保存只拖慢迭代。
4. **#2293 的配套**：三份 Small 配方的 `launch.coordination.heartbeat_timeout` 从 120 提到 480 秒（含 two-node-mech），MILES runtime pin 从 `22d2fb4c` 升到 `9eff46a4`（`options.json` 快照同步更新，configuration 包的 Python 源码依旧零改动）。

`keep_every` 的 50/100 差异不是随手取值，而是各模板的评估/保留需求（见 4.7.3 的 README 表格）：node 对齐既有的 50 更新开发面板；three-node 保留更长学习里程碑（其评估仍是 initial/final）；Small 为「实测后扩展 30 更新预算」保留昂贵运行的里程碑。

#### 4.7.2 核心流程

以 Small four/eight-node 的 30 更新起步预算为例，保存时间线如下：

```text
优化器更新:  1 ────── 10 ────── 20 ────── 30 (num_rollouts 预算耗尽)
恢复保存:             ✓S10       ✓S20      ✓S30 + 最终存档(save_final_checkpoint)
滚动窗口:             {S10}    {S10,S20}   {S20,S30}   ← 任意时刻 ≤ checkpoint_keep_last = 2
里程碑:              (keep_every=50：30 更新内不触发)
扩展预算:                                    … ✓S40 … ✓S50★永久保留 … ✓S60 …
```

三个要点：

- **保存点序列**：`save_interval = 10` 且预算 30 时，恢复保存恰在更新 10/20/30 发生；滚动窗口保证磁盘上的恢复存档数 \[ \#\text{滚动存档}(u) \le \min\!\left(\left\lfloor u/10 \right\rfloor,\; 2\right) \] 即「有界」——占用不随训练时长增长，代价是只能回退到最近的保存点。
- **里程碑判定**：更新 \( u \) 是里程碑当且仅当 \( u \bmod \text{keep\_every} = 0 \)。30 更新预算内 50 里程碑不会出现；它是为扩展预算（如 60+ 更新）预留的永久锚点——「50-update retention becomes relevant when the run is extended」（olmo35-small.md）。
- **抢占续跑**：`auto_resume = true` 下任务被抢占后自动重启，从**最新完整**保存点恢复（workflow 侧恢复契约见 u8-l3；`heartbeat_timeout = 480` 给长耗时操作留足存活检测耐心）。

启动前的预算决策流（examples README 的策略节）：

```text
1. 用实测每更新墙钟时间选 save_interval（目标：约 1–2 小时一次保存；
   Tiny seed17 实测约 5–7 分钟/更新 ≈ 50–70 分钟/10 更新）
2. 分别预算 startup/编译、生成等待、训练、评估与保存的时间，
   确认首个完整恢复保存舒适地落在受保护最短运行时（min_runtime）之内；
   放不下 → 缩短间隔或修改 run 预算后再提交
3. 恢复后验证：完成清单 / 最新指针 / 数据游标哈希 /
   恢复后首个 optimizer 计数与游标身份 / 滚动删除确实发生而里程碑保留
```

#### 4.7.3 源码精读

**Tiny node：间隔收紧 + 三件套补齐**。[configs/miles/examples/tiny/node.toml:50-51](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/tiny/node.toml#L50-L51) 把 `save_interval` 从 25 收紧到 10，注释写明「Recovery target: roughly 1–2h; recheck wall time and first-save cost before launch」；[node.toml:118-121](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/tiny/node.toml#L118-L121) 在 `[core]` 补上 `checkpoint_keep_last = 2`、`checkpoint_keep_every = 50`、`save_final_checkpoint = true`，统一注释「Keep the latest two native saves plus permanent milestones and the final save」。`auto_resume = true` 原本就在（[node.toml:23](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/tiny/node.toml#L23)）。

**Tiny three-node：唯一 100 里程碑的模板**。[three-node.toml:65-66](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/tiny/three-node.toml#L65-L66) 同样 `save_interval = 10`；[three-node.toml:264-267](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/tiny/three-node.toml#L264-L267) 的三件套里 `checkpoint_keep_every = 100`——三节点模板预算更大（`num_rollouts = 200`），里程碑间隔随之放大。

**Small four-node：从禁用到启用的翻转**（eight-node 结构相同）。[four-node.toml:47](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/small/four-node.toml#L47) 的 `auto_resume = true`（#2289 前是 `false`）；[four-node.toml:100-102](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/small/four-node.toml#L100-L102) 用 `save_interval = 10` 替换掉原来的 `save_checkpoints = false`，注释从「Timing run length; raise num_rollouts and enable checkpoints for a learning run」改为「Short starting budget; extend only after measuring runtime, save cost and recovery」；[four-node.toml:148-152](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/small/four-node.toml#L148-L152) 是与 Tiny 相同的保留三件套（keep_every = 50）。#2293 把 [four-node.toml:52-54](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/small/four-node.toml#L52-L54) 的 `heartbeat_timeout` 提到 480（eight-node 对应 [L49](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/small/eight-node.toml#L49)）。注意 [four-node.toml:45](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/small/four-node.toml#L45) 的 `min_runtime = "4h"`：#2289 连预算注释也改了——「Historical budget used the older batch without saves; remeasure current wall time and save cost」，历史 3.3h 预算是在旧 batch、无保存的开销下测的。

**Small two-node-mech：力学检查的反例**。[two-node-mech.toml:85-87](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/small/two-node-mech.toml#L85-L87) 保持 `num_rollouts = 4` 与 `save_checkpoints = false`，[L36](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/small/two-node-mech.toml#L36) 保持 `auto_resume = false`；但 [L132](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/small/two-node-mech.toml#L132) 保留了 `checkpoint_keep_last = 2` 而没有 keep_every/save_final——`keep_last` 是 Core 的保留策略声明，`save_checkpoints = false` 时不产生保存所以不生效；保留它意味着一旦为学习 run 打开保存开关，保留行为立即与学习模板一致。心跳超时同样升到 480（[L42](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/small/two-node-mech.toml#L42)）。README 同时给出反方向的告诫：做**无 checkpoint 的计时实验**要显式禁用保存与自动恢复并删除 `save_interval`，且不要把这个覆盖带进学习 run。

**共享策略节：Recovery checkpoints and preserved milestones**。[configs/miles/examples/README.md:328-339](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/README.md#L328-L339) 是本轮新增的策略正文与模板对照表：

| 模板 | 恢复间隔 | 里程碑间隔 | 理由 |
| --- | ---: | ---: | --- |
| Tiny `node` | 10 更新 | 50 更新 | 对齐既有 50 更新开发面板 |
| Tiny `three-node` | 10 更新 | 100 更新 | 保留更长学习里程碑；其评估仍为 initial/final |
| Small `four-node` / `eight-node` | 10 更新 | 50 更新 | 为扩展 30 更新预算保留昂贵运行的里程碑 |

[README.md:341-348](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/README.md#L341-L348) 给出间隔选择方法：按实测墙钟更新时间取约 1–2 小时一次保存；Tiny seed17 数据（batch 2,048）近期约 5–7 分钟/更新（约 50–70 分钟/10 更新），且明说这是 workload 特定证据而非吞吐承诺——Small 的历史计时用旧 batch/目标测得，需重测而非当作 checkpoint 预测，原生保存还会额外增加旧计时未包含的 I/O 时间。[README.md:350-356](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/README.md#L350-L356) 是启动预算要求：分别预算 startup/编译、生成等待、训练、评估与保存时间，**确保首个完整恢复保存舒适地落在受保护最短运行时之内**，否则缩短间隔或修改预算。[README.md:358-364](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/README.md#L358-L364) 是恢复验证清单：首次真实保存的完成清单、最新指针与数据游标哈希；中断后恢复最新完整保存并检查恢复后首个 optimizer 计数与游标身份；确认普通保存滚动删除而里程碑与待评估输入保留——**自动任务重启本身不等于模型/优化器恢复**。

**development-defaults 的 Native checkpoints 条目**。[docs/miles/development-defaults.md:43](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/development-defaults.md#L43)（#2289 改写）：学习示例每 10 更新保存，目标约 1–2 小时的临时恢复保存间隔（用实测墙钟更新与保存时间）；保留最近 2 份、按模板指定的 50/100 更新永久里程碑与最终 checkpoint；检查首个完整保存舒适地落在受保护运行时之内要把启动/编译与评估开销算进去；验证实际保存、有界保留与恢复的 optimizer/cursor 身份——自动重启不是恢复。

**olmo35-small 的 Checkpoint settings 一节**。[docs/miles/olmo35-small.md:233-244](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/olmo35-small.md#L233-L244)（#2289 新增）：four/eight-node 启用 10 更新恢复保存、自动恢复、保留最近 2 份与 50 更新永久里程碑加最终存档；**30 更新起步预算因此保存在 10/20/30**，50 更新保留在 run 扩展时才相关；4 更新 two-node 力学检查仍禁用 checkpoint/恢复；已报告的历史计时用禁用 checkpoint 的 run 测得，不包含新保存开销、也未验证恢复正确性——重测当前 batch 2,048 的墙钟与 checkpoint 成本，目标 1–2 小时一次恢复保存，并在依赖这些设置跑长 run 前验证首次保存与恢复。

**测试如何钉住基线**。[open_instruct/test_miles_throughput.py:110-113](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/test_miles_throughput.py#L110-L113)（#2289 新增，位于 [L79](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/test_miles_throughput.py#L79) 参数化的 node/three-node 学习示例测试内）断言：`miles["save_interval"] == 10`、`core.checkpoint_keep_last == 2`、`core.checkpoint_keep_every == (50 if profile == "node" else 100)`、`core.save_final_checkpoint is True`。[open_instruct/test_miles_demo_baseline.py:34-46](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/test_miles_demo_baseline.py#L34-L46) 则从「node 与 three-node 除拓扑字段外必须全等」的角度处理同一事实：`[core]` 节的拓扑豁免集合里**只有 `checkpoint_keep_every`**——里程碑节奏跟随各模板的评估/保留需求（注释「Artifact milestone cadence follows each template's evaluation/retention needs」），是学习参数之外被显式豁免的唯一 core 字段；`save_interval` 与 `save_final_checkpoint` 不豁免，两个 Tiny 学习示例必须一致。

#### 4.7.4 代码实践

**实践 G：观察统一 checkpoint 基线并推演保存时间线**（纯 CPU，不需要 GPU 与真实模型）。

1. 实践目标：在编译产物中看到四个学习模板统一的 checkpoint 基线与 keep_every 的拓扑差异，并亲手推演 30 更新预算的保存点序列。
2. 操作步骤：
   - 对三个模板各跑一次编译检查（示例代码，非项目原有代码）：
     ```python
     from pathlib import Path
     from open_instruct.miles.configuration.run_spec import RunSpec

     for name in ("tiny/node", "tiny/three-node", "small/four-node"):
         config = RunSpec.load(Path(f"configs/miles/examples/{name}.toml")).compile()
         print(name, config.miles.get("save_interval"),
               config.core.checkpoint_keep_last, config.core.checkpoint_keep_every,
               config.core.save_final_checkpoint)
     ```
   - 再运行 `uv run python -m open_instruct.miles plan configs/miles/examples/small/four-node.toml > plan-4n.json`，在 `plan-4n.json` 的 runtime argv 里找到 `--save-interval 10`，并浏览 `launch` 部分的 `auto_resume` 与 `coordination.heartbeat_timeout`（480）；
   - 运行 `uv run pytest open_instruct/test_miles_throughput.py -q -k learning`，记录 checkpoint 断言（L110-113）；
   - 对照 [README.md:328-364](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/README.md#L328-L364) 与 [olmo35-small.md:233-244](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/olmo35-small.md#L233-L244)，写下 30 更新预算的推演：更新 10/20/30 各发生什么？`keep_last = 2` 下磁盘上任意时刻最多几份滚动存档？50 里程碑何时才出现？
3. 需要观察的现象：三行输出分别为 `(10, 2, 50, True)`、`(10, 2, 100, True)`、`(10, 2, 50, True)`；`plan-4n.json` 的 argv 含 `--save-interval 10`。
4. 预期结果：与 [test_miles_throughput.py:110-113](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/open_instruct/test_miles_throughput.py#L110-L113) 的断言一致；推演结论：30 更新下保存点 10/20/30，更新 30 时滚动窗口为 {S20, S30}（S10 已被挤掉），50 里程碑在预算内不出现、扩展后才永久保留。
5. 无法运行时（未装依赖）标「待本地验证」，以测试文件断言与 TOML 原文作为行为依据。

#### 4.7.5 小练习与答案

**练习 1**：为什么恢复保存只保留最近 2 份（`checkpoint_keep_last = 2`），而不是 1 份或 10 份？

**答案**：1 份没有冗余——最新保存若因中断而损坏，就没有更早的完整点可退；更多份则几乎无用（恢复几乎总是用最新的）却持续增加存储与写盘开销。「有界」正是这套策略的核心承诺：滚动窗口保证恢复存档占用有上界、不随训练时长增长（[development-defaults.md:43](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/development-defaults.md#L43) 的「Retain the latest two saves … bounded retention」）。需要长期回看的更新点交给 `checkpoint_keep_every` 里程碑与 `save_final_checkpoint`，不占恢复窗口。

**练习 2**：30 更新预算下 `checkpoint_keep_every = 50` 有什么意义？为什么 Small 配方不直接设成 30？

**答案**：30 更新内 50 里程碑不会触发（更新 30 不是 50 的倍数），它是为「实测当前 batch 墙钟与保存成本后**扩展预算**」预留的永久锚点——「50-update retention becomes relevant when the run is extended」（[olmo35-small.md:237-238](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/olmo35-small.md#L237-L238)）。设成 30 会让「最后一个预算内更新」变成永久里程碑，但那时 `save_final_checkpoint` 已经追加了最终存档，两者重叠；50 的价值在于扩展后的长 run 中留出间隔合理的永久回看点。

**练习 3**：为什么「首个完整恢复保存必须落在受保护最短运行时之内」？

**答案**：受保护最短运行时（`launch.min_runtime`，如 four-node 的 4h）内调度器不会回收任务；若首个完整保存在此之后才完成，一次抢占发生时 `auto_resume` 重启后**没有任何保存点可用**，只能从零重跑整个前缀——受保护时长白白耗尽。所以 README 要求分别预算 startup/编译、生成等待、训练、评估与保存时间，确认首存「comfortably inside」受保护时段，放不下就缩短间隔或修改预算（[README.md:350-356](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/README.md#L350-L356)）。

---

## 5. 综合实践

把本讲所有环节串成一次完整的「配置驱动」流程（全程 CPU，不需要 GPU 与真实数据）：

1. **准备**：复制 `configs/miles/examples/tiny/dev.toml` 为 `open-instruct-tutorial/` 之外的临时目录中的 `my-run.toml`（注意其中 [dev.toml:30-32](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/tiny/dev.toml#L30-L32) 已有 `over_length = "exclude"`，[dev.toml:71-77](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/tiny/dev.toml#L71-L77) 已有 `reward_final_answer_only = true` 与 `reward_reasoning_mode = "required"`，[dev.toml:84-87](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/tiny/dev.toml#L84-L87) 已有本轮翻转后的 `calculate_per_token_loss = true`）；把 `model.source`、`output.root` 换成本地占位路径，`launch.workspace/budget` 换成你有权限的值。
2. **validate**：`uv run python -m open_instruct.miles validate my-run.toml`，应打印 `Run schema, topology and MILES/Core options validated; inputs and runtime checked during train`。
3. **plan 与评估采样默认**：`uv run python -m open_instruct.miles plan my-run.toml > plan.json`，检查输出中的 runtime argv：找到 `--num-rollout 4` 与 `--save-interval 2`（对应 `num_rollouts=4`、`save_interval=2`）、以及本轮基线翻转的证据 `--calculate-per-token-loss`（再用 `--set miles.calculate_per_token_loss=false` 重跑一次 plan，该旗标应消失，见 4.6.4）；再找到评估采样默认的证据——`--eval-temperature 1.0`、`--eval-top-p 0.95`（本文件在 `[inference]` 显式写了同值，删掉这两行重跑 plan，默认值应原样出现），以及 `--eval-max-response-len 256`（= `min(max_response_length=256, 32768)`）。同时浏览 `launch_warnings`、`stages` 与 `allocation`。
4. **SciCode 计划期警告实验（上一轮新增行为的观察点）**：dev.toml 没有 `[evaluation]` 节，从 [three-node.toml:68-178](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/tiny/three-node.toml#L68-L178) 把 `[evaluation]` 节（至少含头部的 `mode = "background"` 等字段）连同 scicode 任务（[L157-162](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/tiny/three-node.toml#L157-L162)）抄进 `my-run.toml`，分两次 plan：
   - 原样抄入（scicode 任务不带 `h5py_host_path`）：plan 应**成功**且 `launch_warnings` 里**没有** SciCode 警告；检查 plan 输出的 `evaluation` 部分，`secrets` 里应出现自动注入的 `SCICODE_H5PY_HOST_PATH`（默认秘密，见 4.2.3）；
   - 给 scicode 任务的 `arguments` 加回占位路径 `h5py_host_path = "/weka/YOUR_USERNAME/scicode/test_data.h5"`：plan 仍应**成功**（模板保持可加载），但 `launch_warnings` 第一条变成 SciCode 占位路径的帮助文本；对照 4.2.3 解释为什么这里只警告、真正的拒绝在启动边界。
5. **错字段实验**：把 `[trainer] gpus = 1` 改成 `gpu = 1` 运行 validate，记录报错（会走 `Unknown MILES option` 路径并带 did-you-mean 提示）；再把 `model.source` 改成 `model.sorce` 记录另一类报错。
6. **三组约束实验**：按 4.4.4 的三个实验各做一次（priority 缺字段、over_length 非法值、num_epoch/num_rollouts 互斥），记录报错原文。
7. **score-centering 实验**：按 4.5.4 做一次合法 plan 与三类非法配置，记录编译产物与报错。
8. **示例家族对照（本轮换代）**：`ls configs/miles/examples/small/` 确认 `dev.toml` 已不存在、取而代之的是 `two-node-mech.toml` / `four-node.toml` / `eight-node.toml`；`diff configs/miles/examples/tiny/node.toml configs/miles/examples/small/two-node-mech.toml` 重点看 `[training]`/`[inference]`/`[miles]` 三个节——学习基线字段（LR 6e-6、256 × 8、batch 2048、token-mean）应一致，拓扑、准入与 run 长度字段（`num_rollouts = 4`、`save_checkpoints = false`）不同；阅读 [configs/miles/examples/README.md:8-25](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/README.md#L8-L25) 的家族划分表与「All three Small recipes use the Tiny learning baseline」声明，说明为什么「家族划分」是按模型（Tiny 力学/学习示例 vs Small B300 实测配方）而不是按规模放大同一份文件；再运行 `uv run pytest open_instruct/test_miles_demo_baseline.py -q` 确认 Small 配方被参数化扫描自动覆盖。
9. **checkpoint 基线对照（本轮 #2289/#2293 固化的基线）**：按 4.7.4 对 `tiny/node`、`small/four-node` 各做一次编译检查与 plan，记录统一基线（`save_interval = 10`、`checkpoint_keep_last = 2`、`checkpoint_keep_every` 为 50 或 100、`save_final_checkpoint = true`）与 Small 的 `auto_resume = true`、`heartbeat_timeout = 480`；对照 [configs/miles/examples/README.md:328-364](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/configs/miles/examples/README.md#L328-L364) 的「Recovery checkpoints and preserved milestones」策略节，解释 30 更新预算下 10/20/30 保存点与 50 里程碑的关系（预算内不触发、为扩展预留），以及为何首个完整恢复保存必须落在受保护最短运行时之内（否则首存完成前的抢占只能从零重来）；顺带确认 `two-node-mech` 保持无 checkpoint、`diff` four-node 与 eight-node 只有拓扑差异。
10. **交付物**：一份表格，左列是每次故意制造的错误/默认值观察，右列是报错原文或 plan 中的取值，以及「哪一层拦下的/哪一层填的」（`_data` / `fields()` / `CoreConfig.__post_init__` / `RunConfig.validate` / `score_centering.validate` / `launch_warnings` 的降级警告 / `compile` 的互斥、冲突与 setdefault / 示例 TOML 的基线声明与 `--set` 覆盖）。

## 6. 本讲小结

- 配置分两层：结构化 run 文件（`RunSpec`，研究者视角）与底层 `CoreConfig` + `RunConfig`（引擎视角），`RunSpec.compile()` 是两者之间带来源追踪（origins）与冲突检测的翻译器，全程 CPU-only。
- 加载即编译：`RunSpec.from_dict` 返回前就跑了一遍 `compile()`，绝大多数配置错误在 `plan`/`validate` 阶段就能暴露；未知字段有 did-you-mean 提示，miles 选项有解析器快照校验。
- 错误三分类（Invalid configuration / Not implemented / Not validated）由 `constraints.py` 统一定义，全部在训练前拦截，不是警告；`score_centering.validate` 挂在 `resolved_miles()`，保护所有入口。
- 采样与预算：priority 提供者三件套要求 provider=priority、字段必填、`rollout_global_dataset=true`、不与排除表/replay 组合；`data.over_length` 只取 `"fail"`/`"exclude"`；`num_epoch` 与 `num_rollouts` 互斥，epoch 预算默认 `save_final_checkpoint=true`，`wandb_group` 缺省跟随 `wandb_run_name` 或 run 名；池采样优先级 `data.sampling.priority` 与 replay 编译期互斥。
- 判分输入声明：`core.reward_final_answer_only`（进入全部维护示例，库默认仍 false）与 `core.reward_reasoning_mode`（`required`/`none`，choice 校验排在 `__post_init__` 首行）声明验证器看哪段输出、是否预期思考段。
- score centering：`loss_type="score_centering"` 参数族经 `score_centering.py` CPU 预检（FP32 head、GRPO 优势、十个旧开关互斥、TIS/MIS 参数）；`async.off_policy_correction="tis"` 在 centering 下改映射 `score_centering_is="tis"`，异步锚定与 refresh 资格扩展为「TIS 或 score-centering IS」；候选概率收集要求 `rollout_top_logprobs_num >= rollout_top_k`（任意 loss）。
- 评估采样：编译默认 `eval_temperature=1.0`、`eval_top_p=0.95`、`eval_max_response_len=min(response, 32768)`；维护示例按模型家族组织为 `tiny/` 与 `small/`，示例统一声明判分输入字段。
- SciCode 计划期警告（`8cd7c9eb → 28c181666`）：`launch_warnings` 首查 SciCode 参考路径——占位/非绝对的 `h5py_host_path` 在 plan 阶段**只警告不拦截**（模板与示例保持可加载），硬拒绝留给 `launch.specification` / `workflow.execute` / 评估提交三个启动边界；配套 `evaluation.parse` 对未显式给路径的 scicode 任务自动注入默认秘密 `SCICODE_H5PY_HOST_PATH`（秘密解析链路见 u8-l9）。
- **本轮更新（示例基线翻转，`28c181666 → 2f7889da9`，configuration 包零改动）**：全部维护示例改用 token-mean loss（`miles.calculate_per_token_loss = true`，省略时库默认仍为 response-mean 兼容既有 run，显式 `false` 是被记录的实验偏离）；Tiny 学习示例统一为 256 prompts × 8、batch 2048（一个 collection 恰好一个 optimizer step）、恒定 LR 6e-6 无 warmup；`small/dev.toml` 占位删除、换入三份共用 Tiny 学习基线的实测 B300 配方；LR/batch 探索用 response-mean、历史 Small 计时用旧基线的告诫写入 development-defaults 与 examples README，`test_miles_demo_baseline.py` 从正反两面钉住新基线。
- **本轮更新二（checkpoint 基线固化，#2289/#2293，configuration 包 Python 源码零改动——仅 options.json 随 MILES runtime pin `22d2fb4c → 9eff46a4` 同步）**：四个学习模板统一有界恢复保存——`save_interval = 10`（目标约 1–2 小时一次，按实测墙钟选定）、`checkpoint_keep_last = 2`（滚动窗口有界）、`checkpoint_keep_every`（node 与 Small 为 50、three-node 为 100 的永久里程碑）、`save_final_checkpoint = true`；Small four/eight-node 从「禁用保存 + 不自动恢复」翻转为「启用保存 + `auto_resume = true`」，two-node-mech 力学检查保持无 checkpoint；三份 Small 配方心跳超时 120 → 480。examples README 新增「Recovery checkpoints and preserved milestones」策略节（间隔选择、首个完整保存须落在受保护最短运行时内、恢复验证清单——自动重启≠恢复），development-defaults 的 Native checkpoints 条目与 olmo35-small 的「Checkpoint settings in the current starters」同步；`test_miles_throughput.py` 断言三件套，`test_miles_demo_baseline.py` 把 `checkpoint_keep_every` 列为 node 与 three-node 间唯一被豁免的 core 拓扑字段。

## 7. 下一步学习建议

- **u8-l3（工作流与执行）**：看 `compile()` 产出的配置如何被 `workflow.py`/`driver.py` 消费——特别是 driver 返回值携带 `num_rollout` 后完成判定的变化，以及 `save_final_checkpoint` 在最后一次更新时如何落盘；本轮起 `workflow.execute` 开头多了 SciCode 参考路径预检，与注册表预检并列构成启动期资产校验；#2293 还让已认证的 resume 可以调整 `launch.coordination.heartbeat_timeout`（与 Small 配方 120 → 480 的运维动机配套），恢复宽容键的完整判定在那一讲展开。
- **docs/miles/models-and-checkpoints.md 的 save-resume-and-export 一节**：4.7 的 `save_interval` / `checkpoint_keep_last` / `checkpoint_keep_every` 只是配置入口；恢复保存滚动删除、里程碑永久保留、最新指针与数据游标哈希等原生保留语义（README 恢复验证清单的契约底座）在这份文档里定义。
- **u8-l9（后台评估与推理记录）**：本轮的 `launch_warnings` SciCode 分支只是配置层的投影——默认秘密的评估器内解析、文件存在性校验、错误信息不回显秘密值、失败回执的可行动诊断都在那一讲展开。
- **u8-l12（统一判分输入）**：本讲的 `reward_final_answer_only` / `reward_reasoning_mode` 只是入口；ResponseParts 切分、GradingPolicy 的 objective×scope×require_response 契约、验证器 `selected_input` 直通与注册表校验都在那一讲展开。
- **u8-l14（score centering 专题）**：本讲只覆盖了配置入口与预检；训练目标的数学、TIS/MIS 的取值动机、refresh 集成与测量在那一讲深入，并演示 `test_miles_score_centering.py` 与 `tests/miles/test_score_centering.py` 的覆盖点。
- **u8-l5 / u8-l11（rollout 提供者与 priority 采样）**：`data.sampling.priority` 的运行时组序、与 uniform/exposure 策略的组合测量，以及独立 priority provider 的组序遍历实现。
- **u8-l15（B300 实测配方）**：本讲只把 `small/` 三份配方当作「共用 Tiny 学习基线的配置文件」看待；它们的 GPU 布局（trainer EP 划分、引擎数、裁判模型）、R=96 准入联动、fused-MoE tile 调优的分发链路与显存/吞吐审计，以及「历史计时用旧基线测得、需重测」的完整论证都在那一讲展开。
- 想系统了解每个选项的默认值与交互，通读 [docs/miles/configuration-intro.md](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/configuration-intro.md) 与 [docs/miles/core.md](https://github.com/allenai/open-instruct/blob/2063f854b37cd380e55f5509df7ea50b6d8a1033/docs/miles/core.md) 的 Score centering 一节，并用 `plan` 对照自己的 run 文件逐项核对。
