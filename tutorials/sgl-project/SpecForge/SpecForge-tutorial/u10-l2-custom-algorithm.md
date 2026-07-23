# 新增一个训练算法

## 1. 本讲目标

本讲回答一个工程问题：**我想给 SpecForge 增加一种全新的草稿训练算法（例如 `mydraft`），到底要动哪些文件、写哪些对象？**

学完后你应当能够：

- 说出新增算法必须交付的「三件套」：纯契约 `AlgorithmSpec`、可执行端口 `AlgorithmProviders`、训练策略 `DraftTrainStrategy`。
- 解释 `make_registration` 用哪几条 parity（对账）校验把契约与可执行代码焊在一起、任何漂移如何 fail-fast。
- 写出一个 `create_registration()` 工厂，并把它加入 `builtin_algorithm_registry()`，使 `training.strategy: mydraft` 能被组合根选中。
- 区分「算法契约里能放什么、不能放什么」，并理解这与离线捕获 layout 的绑定关系。

本讲是「扩展与二次开发」单元的核心一讲，承接 u4-l3（providers 端口体系）与 u6-l2（`DraftTrainStrategy` 抽象），把这两块拼成一个完整的「算法插件」。

## 2. 前置知识

本讲默认你已经读过并在脑中建立了下列认知（来自前置讲义，这里只点关键词、不重复展开）：

- **两根正交扩展轴**（u10-l1）：草稿架构（`@register_draft`）与训练算法（本讲）是两条独立的轴。新增架构只动 `modeling/draft/`；新增算法才动 `algorithms/` 与 `training/strategies/`。两者最终在装配阶段用一根架构名字符串交汇。
- **算法双半绑定**（u4-l2 / u4-l3）：`AlgorithmRegistration` 把纯契约 `spec`（`AlgorithmSpec`）与可执行 `providers`（`AlgorithmProviders`）绑成同一对象，避免维护两张会漂移的表。
- **FeatureContract 的身份键**（u4-l1）：`(mode, modality)` 二元组是特征契约的唯一身份；`OFFLINE` 必须带 `OfflineStorageContract`，`STREAMING` 不得带。
- **DraftTrainStrategy 是唯一知道「batch 如何算 loss」的地方**（u6-l2）：`TrainerCore.train_step` 无分支，算法差异全收敛进策略插件的 `forward_loss`。
- **组合根解析权唯一**（u3-l4）：`training.strategy` 字符串只在 `resolve_run` 经 `AlgorithmRegistry.resolve` 翻译一次，之后只传 `AlgorithmRegistration` 对象，不再传字符串。

若以上任何一点对你陌生，建议先读对应讲义再回来。

## 3. 本讲源码地图

本讲涉及的文件按「自顶向下」的调用顺序排列：

| 文件 | 作用 | 本讲用法 |
| --- | --- | --- |
| `specforge/algorithms/contracts.py` | 算法**纯数据契约**（不含可执行代码） | 第一件套：声明 `AlgorithmSpec` |
| `specforge/algorithms/common/providers.py` | 算法**可执行端口**容器 + `make_registration` 对账 | 第二件套：组装 `AlgorithmProviders` |
| `specforge/algorithms/dflash/providers.py` | 一个**完整真实范例**（最简洁的算法注册） | 当作模板逐行对照 |
| `specforge/training/strategies/base.py` | `DraftTrainStrategy` 抽象与各实现 | 第三件套：实现 `forward_loss` |
| `specforge/algorithms/registry.py` | `AlgorithmRegistry` 不可变目录与 `resolve` | 解释「按名查表」的落点 |
| `specforge/algorithms/builtin.py` | 内置算法的显式目录工厂 | 第四步：把新算法加进这里 |
| `specforge/application/planning.py` | `validate_resolved_run` 六道校验 | 解释何时 fail-fast |
| `specforge/training/assembly.py` / `specforge/launch.py` | 装配与 `step.build` 接线 | 解释 strategy 如何被构造 |

> 提示：`dflash` 是最干净的范例——它同时声明了 offline 与 streaming 两份契约、用硬标签算单标量 loss、没有 vocab mapping、没有 compact teacher。本讲大量篇幅以它为锚点。

## 4. 核心概念与源码讲解

### 4.1 算法的「三件套」骨架

#### 4.1.1 概念说明

SpecForge 不存在「方法专属的训练入口脚本」。所有草稿方法（eagle3 / peagle / dflash / domino / dspark）共用同一个命令 `specforge train --config <yaml>`，仅凭配置里的 `training.strategy` 字段区分。要新增一种算法，本质上是**向算法注册表里插入一个 `AlgorithmRegistration`**，而这个对象由三件套构成：

1. **`AlgorithmSpec`（纯契约）**：只含静态约束（算法叫什么名、能兼容哪些草稿架构、声明需要哪些特征张量、自身有哪些能力），**故意不含任何模型类、工厂函数或运行时对象**。
2. **`AlgorithmProviders`（可执行端口）**：算法「会动的代码」——怎么建草稿配置、怎么建训练模型、怎么解析捕获层、怎么造离线 reader/normalizer/collator、怎么构造训练 step。
3. **`DraftTrainStrategy`（训练策略）**：训练进程里唯一知道「一个归一化后的 `TrainBatch` 如何变成 loss」的对象。

三者经 `make_registration(spec, providers)` 焊成一个 `AlgorithmRegistration`，再放进 `builtin_algorithm_registry()`。`training.strategy: mydraft` 命中后，组合根把这三件套沿真实调用链分发出去。

#### 4.1.2 核心流程

一次 `specforge train --config mydraft.yaml`（其中 `training.strategy: mydraft`）里，新算法涉及的关键节点如下（粗体为本讲关注的三件套落点）：

```text
cli.main → _train
  → load_config                       # 解析 YAML，未知字段 fail-fast
  → resolve_run(cfg)                  # ★ 用 training.strategy 查注册表，得到 AlgorithmRegistration
      └─ builtin_algorithm_registry().resolve("mydraft")
         → AlgorithmRegistration(spec=【契约】, providers=【端口】)
  → bind_run(role_cfg, algorithm)
      └─ validate_resolved_run(...)   # ★ 六道校验：契约/能力/拓扑/vocab...
  → build_application_run(resolved).run()
      └─ build_training_run
          └─ providers.step.bind_runtime(...)   # 产出 StepRuntimeConfig(options, resume_contract)
          └─ launch._assemble_trainer
              └─ make_step_strategy = providers.step.build   # ★ 构造 DraftTrainStrategy
              └─ Trainer.fit → TrainerCore.train_step
                  └─ strategy.forward_loss(batch, ctx)       # ★ 算 loss
```

三件套分别在三个不同时刻被消费：**契约**在 `resolve_run`/`validate_resolved_run`（解析与校验期，无 GPU）被读；**端口**在 `build_training_run`（装配期）被读；**策略**在 `Trainer.fit`（执行期，有 GPU）被构造与调用。这种「声明—装配—执行」的三段分离，是新增算法时必须遵循的骨架。

#### 4.1.3 源码精读

`launch.py` 把 `providers.step.build` 作为策略工厂注入训练器，这是端口→策略的接线点：

- [launch.py:115](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L115)：`make_step_strategy=algorithm.providers.step.build` ——训练器拿到的是算法注册表里的 `step.build` 可调用对象，而不是任何硬编码的策略类。

而 `validate_resolved_run` 在装配前用契约做六道通用校验，确保不合法组合「炸在装配前、不炸在训练中」：

- [planning.py:189-208](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/planning.py#L189-L208)：依次校验「算法名一致」「特征契约匹配 modality」「草稿选项合法」「算法能力」「训练拓扑」「vocab mapping」。

#### 4.1.4 代码实践

**实践目标**：在真实源码里验证「三件套被分三段消费」。

**操作步骤**：

1. 打开 `specforge/algorithms/dflash/providers.py`，确认它同时定义了 `algorithm_spec()`（契约）、`algorithm_providers()`（端口）、并通过 `build_step`（第 42 行）引用 `DFlashTrainStrategy`（策略）。
2. 全局搜索 `providers.step.build`，确认它只在 `launch.py:115` 被当作 `make_step_strategy` 传入。
3. 全局搜索 `validate_resolved_run`，确认它在组合根 `bind_run` 阶段、训练真正开始之前被调用。

**需要观察的现象**：契约函数 `algorithm_spec()` 内部没有任何 `import torch` 或模型类引用；端口函数里的重依赖（`from specforge.training.strategies.base import ...`）都写在函数体内部、是懒导入。

**预期结果**：你会看到「契约轻、端口重、策略最重（依赖模型代码）」的清晰分层，这正是三件套设计的落点。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `AlgorithmSpec` 里塞进一个模型类（例如 `draft=LlamaForCausalLMEagle3`），会发生什么？
**答案**：`AlgorithmSpec.__post_init__` 末尾会调用 `_assert_pure_value`，检测到 `type`/`callable` 直接抛 `TypeError`。契约必须只含可序列化的纯值。

**练习 2**：为什么 `step.build` 不直接放在 `AlgorithmSpec` 里？
**答案**：因为契约要在「无 GPU、无 torch」的解析期被读（例如 `--plan` 预览）。把可执行代码塞进契约会强制解析期导入重依赖，破坏注册表的轻量性与可哈希性。

---

### 4.2 第一件套：声明 `AlgorithmSpec` 纯契约

#### 4.2.1 概念说明

`AlgorithmSpec` 是算法的「身份证 + 静态约束清单」。它回答四个问题，且只用纯数据回答：

- **name**：算法名，必须匹配 `^[a-z][a-z0-9_-]*$`（小写字母开头，只含小写字母、数字、下划线、连字符）。
- **draft**：`DraftRequirement`——能兼容哪些草稿架构名（只放名字、不放类）、默认架构、支持哪些配置覆盖。
- **feature_contracts**：若干 `FeatureContract`——以 `(mode, modality)` 为键，声明该模式下需要哪些张量。
- **capabilities**：`AlgorithmCapabilities`——算法自身的能力约束（注意力后端集合、是否支持 compact teacher / vocab mapping 等），与部署拓扑无关。

关键不变量：**契约里没有任何可执行对象**。`_assert_pure_value` 在构造末尾递归扫描，一旦发现 `type` 或 `callable` 立即报错。这使得契约在解析期可被自由读、可哈希、可序列化。

#### 4.2.2 核心流程

构造 `AlgorithmSpec` 的校验流程：

```text
AlgorithmSpec(name, draft, feature_contracts, capabilities)
  ├─ name 正则校验
  ├─ draft / capabilities 类型校验
  ├─ feature_contracts 非空、均为 FeatureContract
  ├─ (mode, modality) 键不可重复
  └─ _assert_pure_value(self)   # 递归拒绝一切 callable/type
```

其中 `FeatureContract` 自身的校验最为关键，三条铁律：

1. `OFFLINE` 契约**必须**带 `OfflineStorageContract`（声明离线落盘 schema）；`STREAMING` 契约**不得**带 storage。
2. `required_tensors` 与 `optional_tensors` 必须**不相交**。
3. 若声明了 `allowed_target_representations`，则 `default_target_representation` 必须在其中。

#### 4.2.3 源码精读

`AlgorithmSpec` 的完整定义与构造期校验：

- [contracts.py:265-309](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L265-L309)：`AlgorithmSpec` 数据类。注意第 297-307 行对 `(mode, modality)` 键去重的校验，以及第 309 行 `_assert_pure_value(self)` 这道最后的纯值守卫。

`FeatureContract` 的 storage 铁律（offline 必须有、streaming 禁止有）：

- [contracts.py:220-223](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L220-L223)：`if mode is FeatureMode.OFFLINE and self.storage is None: raise ...`；`if mode is FeatureMode.STREAMING and self.storage is not None: raise ...`。

真实范例——DFlash 的契约声明（offline + streaming 各一份，张量集合相同 `{"input_ids", "loss_mask", "hidden_states"}`）：

- [dflash/providers.py:133-162](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/dflash/providers.py#L133-L162)：`algorithm_spec()`。注意 offline 契约带了 `OfflineStorageContract(format="specforge_hidden_states_v1", required_tensors=ready, normalizer=NORMALIZER_ID)`，而 streaming 契约没有 storage。

`DraftRequirement` 只放架构名、不放类：

- [contracts.py:78-106](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/contracts.py#L78-L106)：`compatible_architectures` 是名字集合，`default_architecture` 必须在其中（第 103-106 行）。

#### 4.2.4 代码实践

**实践目标**：为 `mydraft` 写一份只含 offline 文本契约的最小 `AlgorithmSpec`（**示例代码**，非项目原有）。

```python
# 示例代码：mydraft 的纯契约，仅离线文本
from specforge.algorithms.contracts import (
    AlgorithmCapabilities, AlgorithmSpec, DraftRequirement,
    FeatureContract, FeatureMode, OfflineStorageContract,
)

MYDRAFT_READY = {"input_ids", "loss_mask", "hidden_states"}  # 与 DFlash 同形，便于复用捕获

def algorithm_spec() -> AlgorithmSpec:
    return AlgorithmSpec(
        name="mydraft",                                   # 必须匹配 ^[a-z][a-z0-9_-]*$
        draft=DraftRequirement(
            compatible_architectures={"DFlashDraftModel"}, # 复用一个已注册架构，省去新增架构轴
            default_architecture="DFlashDraftModel",
        ),
        feature_contracts=(
            FeatureContract(
                mode=FeatureMode.OFFLINE,
                modality="text",
                required_tensors=MYDRAFT_READY,
                storage=OfflineStorageContract(            # offline 必须带 storage
                    format="specforge_hidden_states_v1",
                    required_tensors=MYDRAFT_READY,
                    normalizer="mydraft_offline_v1",
                ),
            ),
            # 只做离线时，是否要再声明一份 STREAMING 契约？
            # 见 4.3.4 的讨论：provider 端必须与契约键集合完全相等。
        ),
        capabilities=AlgorithmCapabilities(
            attention_backends={"eager", "sdpa", "flex_attention"},
        ),
    )
```

**操作步骤**：

1. 把上面 `algorithm_spec()` 与真实 [dflash/providers.py:133-162](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/dflash/providers.py#L133-L162) 逐字段对比。
2. 尝试把 `required_tensors` 与某个 `optional_tensors` 设成相同集合，观察 `FeatureContract.__post_init__` 报什么错（见 contracts.py:203-208）。

**需要观察的现象**：若 offline 契约漏掉 `storage`，构造期立刻抛 `ValueError`；若 `name` 含大写字母，正则校验失败。

**预期结果**：你能独立写出一份通过构造期全部校验的纯契约。

#### 4.2.5 小练习与答案

**练习 1**：DFlash 的 offline 与 streaming 契约 `required_tensors` 完全相同，为什么还要分两份？
**答案**：因为 `(mode, modality)` 是身份键。offline 带 storage（离线落盘 schema），streaming 不带（在线由服务端实时捕获）。分两份让校验器能区分「这个算法是否支持在线」——`spec.supports_online` 就是看有没有 STREAMING 契约（contracts.py:319-323）。

**练习 2**：`normalizer="mydraft_offline_v1"` 这个字符串随后会被谁校验？
**答案**：会被 `make_registration` 校验——provider 端 `OfflineDataProvider.normalizer_id` 必须与契约 `storage.normalizer` 完全相等（见 4.3 节）。

---

### 4.3 第二件套：组装 `AlgorithmProviders` 可执行端口

#### 4.3.1 概念说明

`AlgorithmProviders` 是算法「会动的代码」的容器。它按端口分组，**只描述算法行为、不描述部署拓扑**：

- **step**：`StepProvider`——训练 step 的工厂与检查点策略（`build`/`options`/`resume_contract`/`allowed_missing_checkpoint_keys`/`uses_external_target_head`）。
- **model**：`ModelProvider`——草稿配置策略、建草稿模型、建训练模型、解析捕获层等（重依赖一律懒导入）。
- **offline**：若干 `OfflineDataProvider`——每个 modality 一份 reader/normalizer/collator/`OfflineCaptureLayout`。
- **server_streaming**：若干 `ServerStreamingProvider`——在线捕获的 collator 与输入适配器。
- **vocab_mapping_modes**：声明算法在哪些 mode 下用 vocab mapping（必须与 `capabilities.supports_vocab_mapping` 一致）。

端口的存在与缺席本身也是被强校验的属性——例如 P-EAGLE 只在线、不离线，故它没有 `offline` 端口。

#### 4.3.2 核心流程

`make_registration(spec, providers)` 在注册期跑一组 **parity（对账）校验**，把契约与端口焊死，任何漂移即 fail-fast。最关键的几条：

```text
make_registration(spec, providers):
  1. providers.algorithm_name == spec.name                      # 名字一致
  2. draft_config.architecture ∈ spec.draft.compatible_architectures
  3. providers 的 (mode,modality) 键集合 == spec.feature_contracts 键集合   # 完全相等
  4. vocab_mapping_modes 非空  ==  capabilities.supports_vocab_mapping
  5. 每个 offline provider:
       - normalizer_id == contract.storage.normalizer
       - capture_layout.output_names == contract.storage.required_tensors   # 完全相等
       - 若同 modality 也有 streaming：capture_method 必须相等
  6. 每个 streaming provider:
       - target_representation == contract.default_target_representation
       - layout 必须发出全部 required_tensors
```

第 3 条与第 5 条是「双面镜像」：契约声明的特征键与张量名，必须与端口实际提供的完全对齐。这保证了「数据准备脚本写的 schema」与「训练侧期待的 schema」永远不会静默漂移。

#### 4.3.3 源码精读

`(mode, modality)` 键集合必须完全相等的对账：

- [common/providers.py:659-672](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/common/providers.py#L659-L672)：把契约键集合 `expected` 与端口键集合 `provided` 比较，不等即抛错。**这条决定了「只声明 offline 契约时，端口也只能只提供 offline」**——如果你只写了 offline 契约却同时给了 server_streaming 端口，这里会报错。

离线捕获 layout 的物化名必须正好等于 storage 契约的 `required_tensors`：

- [common/providers.py:696-704](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/common/providers.py#L696-L704)：`emitted = set(provider.capture_layout.output_names)` 必须等于 `required = set(contract.storage.required_tensors)`。

`OfflineCaptureLayout` 如何把通用四类捕获源翻译成算法自己的 schema 名：

- [common/providers.py:440-463](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/common/providers.py#L440-L463)：`materialize()` 把 `aux_hidden_states`/`last_hidden_states` 这两个通用源映射到算法自己的特征名（如 DFlash 的 `hidden_states`，EAGLE3 的 `aux_hidden_state`+`hidden_state`），缺源即早失败。

DFlash 的完整端口组装（offline + streaming 各一份，`capture_method="dflash"`）：

- [dflash/providers.py:165-228](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/dflash/providers.py#L165-L228)：`algorithm_providers()`。注意 offline 的 `capture_layout`（第 198-206 行）只发出 `hidden_states`（aux）+ `input_ids`/`loss_mask`（passthrough），正好等于契约 `required_tensors`。

`StepProvider.bind_runtime` 产出 `StepRuntimeConfig`（options + resume_contract），它是策略构造参数与检查点恢复契约的唯一来源：

- [common/providers.py:291-345](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/common/providers.py#L291-L345)：`bind_runtime` 在装配期被调用（见 assembly.py:232），把 `step.options(config)` 与 `resume_contract(config, draft_model, training_model)` 合并成不可变映射。

#### 4.3.4 代码实践

**实践目标**：为 `mydraft` 列出端口清单，并解释「只做离线」时端口该长什么样。

**操作步骤**：

1. 对照 [dflash/providers.py:165-228](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/dflash/providers.py#L165-L228)，为 `mydraft` 画一张端口清单表（**示例**）：

   | 端口 | 取值 | 说明 |
   | --- | --- | --- |
   | `step.build` | `build_step`（懒导入 `MyDraftTrainStrategy`） | 构造策略 |
   | `step.options` | `empty_options` | 无额外策略参数 |
   | `step.resume_contract` | 自定义，记录影响 loss 的设置 | resume 时校验一致 |
   | `step.uses_external_target_head` | `False` | 不需要冻结目标头 |
   | `model.draft_config.architecture` | `"DFlashDraftModel"` | 复用已注册架构 |
   | `offline[0].capture_layout.capture_method` | `"dflash"` | **必须复用 eagle3/dflash 之一** |
   | `offline[0].capture_layout.output_names` | `{"input_ids","loss_mask","hidden_states"}` | 必须等于契约 required_tensors |
   | `server_streaming` | **缺省（空元组）** | 因契约里没有 STREAMING |

2. 验证一条硬约束：搜索 [capture.py:97-105](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/offline_capture/sglang_backend/capture.py#L97-L105)，确认离线 `capture_method` 只认 `'eagle3'` 与 `'dflash'` 两种 SGLang 捕获钩子。

**需要观察的现象**：如果 `mydraft` 的 offline 契约里没有 STREAMING，但端口里却给了 `server_streaming`，`make_registration` 会在第 659-672 行报键集合不等。

**预期结果**：你能说出「只做离线」的算法，端口侧 `offline` 非空、`server_streaming` 必须为空，否则 parity 校验失败。

> **重要现实约束**：`capture_method` 目前只有 `eagle3`/`dflash` 两种目标侧钩子（capture.py:101-105）。一个真正全新的算法要么复用这两种捕获方式之一（像上面 `mydraft` 复用 `dflash`），要么需要同步扩展 `offline_capture/` 与 SGLang 侧的捕获钩子——后者已超出「只改 `algorithms/`」的范围。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `step`、`model` 等端口里的重依赖（`import torch`、模型类）都写在函数体内部？
**答案**：为了让 `builtin_algorithm_registry()` 在解析期（如 `--plan` 预览，无 GPU）能安全构造注册表——注册时只存函数引用，不触发重依赖导入（见 u4-l2）。

**练习 2**：`capture_layout.output_names` 与 `contract.storage.required_tensors` 为什么必须「完全相等」而不是「子集」？
**答案**：因为离线捕获脚本写盘的 schema 与训练侧 reader 读取的 schema 必须精确对齐。若允许子集，捕获脚本可能漏写某个张量而在训练期才崩；完全相等把错误前移到注册期 fail-fast。

---

### 4.4 第三件套：实现 `DraftTrainStrategy`

#### 4.4.1 概念说明

`DraftTrainStrategy` 是训练进程里唯一知道「一个 `TrainBatch` 如何变成 loss」的对象。它有五个职责（前两个抽象、后三个可选覆盖）：

- `trainable_module()`（抽象）：返回优化器/后端拥有的可训练模块。
- `forward_loss(batch, ctx)`（抽象）：把归一化 batch 喂给模型，返回 `StepOutput(loss, metrics)`。
- `validate_batch(batch)`：检查 `batch.tensors` 是否含全部 `required_features`，缺则报错。
- `checkpoint_state_filter(state_dict)`：从完整 state_dict 里挑出「作为草稿权重存盘」的键。
- 类属性 `required_features`：声明本策略需要哪些张量名（必须与契约 `required_tensors` 对齐）。

`TrainerCore.train_step` 刻意保持无分支——它只调 `strategy.forward_loss(batch, ctx)`，算法差异全收敛进策略插件。这意味着新增算法的「训练数学」只写在一个地方：你的策略类的 `forward_loss`。

#### 4.4.2 核心流程

策略在训练器里的构造与调用：

```text
launch._assemble_trainer
  └─ make_step_strategy = providers.step.build
Trainer.fit
  ├─ wrapped = backend.prepare_model(model, optimizer_target=model.draft_model)  # FSDP 包裹
  ├─ strategy = step.build(wrapped, target_head=<仅当 uses_external_target_head>, **options)
  ├─ core = TrainerCore(strategy, backend, accumulation_steps)
  └─ TrainerController epoch 循环
      └─ 每步: strategy.validate_batch(batch) → strategy.forward_loss(batch, ctx) → backward
```

其中 `options` 来自 `step.options(config)`（经 `bind_runtime` 包成 `StepRuntimeConfig`），它就是策略构造函数的关键字参数。若你的策略需要可配参数（如 EAGLE3 的 `ploss_decay`），就通过 `step.options` 从 config 里取并透传。

DFlash 这类「硬标签、单标量 loss」的策略最简单，它的 loss 不涉及目标分布投影：

\[ \mathcal{L} = -\frac{1}{|\mathcal{M}|}\sum_{i\in\mathcal{M}} \log p_{\theta}(y_i \mid \cdots),\qquad \mathcal{M}=\{i:\text{loss\_mask}_i=1\} \]

其中 \(p_{\theta}\) 是草稿模型在目标特征条件下的预测分布，\(y_i\) 是真实 token 标签。

#### 4.4.3 源码精读

`DraftTrainStrategy` 抽象基类的五个职责：

- [base.py:63-86](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py#L63-L86)：`required_features` 类属性（第 65 行）、`validate_batch`（第 71-77 行，按 `required_features` 检查 `batch.tensors`）、抽象 `forward_loss`（第 79-82 行）、`checkpoint_state_filter`（第 84-86 行，默认原样返回）。

DFlash 策略——`required_features` 与契约对齐，`forward_loss` 返回单标量：

- [base.py:418-444](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py#L418-L444)：`DFlashTrainStrategy`。注意 `required_features = {"input_ids", "hidden_states", "loss_mask"}`（第 419 行）正好等于 DFlash 契约的 `required_tensors`；`forward_loss` 调 `self.dflash_model(...)` 得 `(loss, accuracy, model_metrics)` 并包成 `StepOutput`。

DFlash 的存盘过滤——只剥 `draft_model.` 前缀：

- [base.py:446-453](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py#L446-L453)：`checkpoint_state_filter` 只保留 `draft_model.` 前缀的键并去掉前缀，目标头/embedding 不作为草稿权重存盘。

DFlash 的 `build_step`——把端口与策略接起来的那一行懒导入：

- [dflash/providers.py:42-46](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/dflash/providers.py#L42-L46)：`build_step(wrapped_model, *, target_head=None, **_options)` 内部 `from specforge.training.strategies.base import DFlashTrainStrategy` 并返回 `DFlashTrainStrategy(wrapped_model)`。

`StepOutput` 值对象——所有策略共用同一个返回类型：

- [base.py:29-36](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py#L29-L36)：`StepOutput(loss, metrics, ratio_metrics)`，`frozen=True`，让 per-position（TTT 多步）与单标量策略共用同一条 trainer 循环。

#### 4.4.4 代码实践

**实践目标**：为 `mydraft` 写一个最小策略骨架，并标出 `required_features`（**示例代码**，非项目原有）。

```python
# 示例代码：mydraft 的训练策略（硬标签、单标量 loss，仿 DFlash）
import torch.nn as nn
from specforge.runtime.contracts import TrainBatch
from specforge.training.strategies.base import DraftTrainStrategy, StepOutput

class MyDraftTrainStrategy(DraftTrainStrategy):
    name = "mydraft"
    # 必须与 AlgorithmSpec 里 offline 契约的 required_tensors 对齐
    required_features = {"input_ids", "hidden_states", "loss_mask"}

    def __init__(self, mydraft_model: nn.Module) -> None:
        self.mydraft_model = mydraft_model

    def trainable_module(self) -> nn.Module:
        return self.mydraft_model

    def forward_loss(self, batch: TrainBatch, ctx=None) -> StepOutput:
        self.validate_batch(batch)                      # 复用基类：缺特征即报错
        t = batch.tensors
        device = next(self.mydraft_model.parameters()).device
        loss, accuracy, _metrics = self.mydraft_model(  # 模型自己算 loss
            input_ids=t["input_ids"].to(device),
            hidden_states=t["hidden_states"].to(device),
            loss_mask=t["loss_mask"].to(device),
        )
        return StepOutput(loss=loss, metrics={"accuracy": accuracy.detach()})

    def checkpoint_state_filter(self, state_dict):
        # 与 DFlash 一致：只保留草稿权重
        return {
            k.replace("draft_model.", ""): v
            for k, v in state_dict.items()
            if "draft_model." in k
        }
```

**操作步骤**：

1. 把上面 `required_features` 与 4.2.4 里 `MYDRAFT_READY` 对照，确认二者完全一致。
2. 对照 [base.py:430-444](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/strategies/base.py#L430-L444)，确认你的 `forward_loss` 返回的是 `StepOutput` 而非裸 tensor。

**需要观察的现象**：若你把 `required_features` 写成 `{"input_ids"}`（漏了 `hidden_states`），`validate_batch` 不会报错，但模型前向会在访问 `t["hidden_states"]` 时 KeyError——所以 `required_features` 必须如实声明，让错误在 `validate_batch` 处给出清晰信息。

**预期结果**：你能写出一个与契约张量集合严格对齐、返回 `StepOutput` 的最小策略。

#### 4.4.5 小练习与答案

**练习 1**：EAGLE3 策略的 `required_features` 比 DFlash 多了 `attention_mask` 和 `target`（base.py:127-134），为什么？
**答案**：EAGLE3 是特征式草拟，需要目标分布 `target`（教师 logits 或隐状态）做 KL/交叉熵监督，且用 `attention_mask` 处理 padding；DFlash 用硬标签、不需要目标分布。这正体现了「算法差异收敛进策略」。

**练习 2**：`checkpoint_state_filter` 在 EAGLE3 与 P-EAGLE 上有何不同（base.py:300-313 vs 398-406）？
**答案**：EAGLE3 额外剔除冻结的 embedding（`embed` 键）；P-EAGLE 因为训练自身 embedding 与 `mask_hidden`，完整保留所有 `draft_model.` 键。新增算法时需根据「哪些参数是可训练的」决定过滤策略。

---

### 4.5 第四步：注册到 builtin，被 `training.strategy` 选中

#### 4.5.1 概念说明

三件套写好后，最后一步是把它们焊成一个 `AlgorithmRegistration` 并放进内置目录。这一步有两小步：

1. 在算法包里写一个 `create_registration()` 工厂，调用 `make_registration(algorithm_spec(), algorithm_providers())`。
2. 在 `builtin.py` 里 import 这个工厂，并把它加入 `builtin_algorithm_registry()` 返回的元组。

**关键认知**：`training.strategy: mydraft` 之所以能命中，是因为组合根 `resolve_run` 用 `builtin_algorithm_registry().resolve(cfg.training.strategy)` 按名查表。注册表是**显式、不可变、按名字排序**的目录——没有自动扫描、没有插件发现机制。所以「在哪里注册」的答案就是：**`builtin_algorithm_registry()` 的元组里**。

#### 4.5.2 核心流程

```text
# specforge/algorithms/mydraft/providers.py
def create_registration():
    return make_registration(algorithm_spec(), algorithm_providers())

# specforge/algorithms/builtin.py
from specforge.algorithms.mydraft.providers import create_registration as mydraft
def builtin_algorithm_registry():
    return AlgorithmRegistry((eagle3(), peagle(), dflash(), domino(), dspark(), mydraft()))

# 用户 YAML: training.strategy: mydraft
# 组合根:
resolve_run(cfg)
  └─ builtin_algorithm_registry().resolve("mydraft")   # 命中新增的 registration
     → AlgorithmRegistration(spec=【契约】, providers=【端口+策略接线】)
```

`resolve` 的查表逻辑很简单——线性扫描、未命中抛 `KeyError` 并列出全部合法算法名（组合根再转成 `ValueError`）。

#### 4.5.3 源码精读

`create_registration` 工厂——契约与端口在此焊死：

- [dflash/providers.py:231-232](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/dflash/providers.py#L231-L232)：`def create_registration(): return make_registration(algorithm_spec(), algorithm_providers())`。

内置目录工厂——唯一的注册落点：

- [builtin.py:13-16](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/builtin.py#L13-L16)：`builtin_algorithm_registry()` 返回 `AlgorithmRegistry((eagle3(), peagle(), dflash(), domino(), dspark()))`。新增 `mydraft` 即在此元组追加一项。注意它每次返回**全新**对象，不做模块级单例可变（u4-l2）。

`AlgorithmRegistry.resolve`——按名查表、未命中列出全部合法名：

- [registry.py:78-84](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/registry.py#L78-L84)：线性扫描，未命中抛 `KeyError("unknown algorithm 'mydraft'; registered algorithms: [...]")`。

官方文档对「新增算法」的明确说明：

- [customization.md:130-136](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/advanced_features/customization.md#L130-L136)：原文明确「一个全新的训练算法还需要一个纯 `AlgorithmSpec`、可执行 `AlgorithmProviders`、以及 `specforge/algorithms` 下的一个不可变 `AlgorithmRegistration`……把内置注册加进应用组合根使用的显式目录；不要新增方法专属 launcher 或第二个可变注册表」。

#### 4.5.4 代码实践

**实践目标**：把 `mydraft` 三件套接入内置目录，确认 `training.strategy: mydraft` 能被解析。

**操作步骤**：

1. 新建 `specforge/algorithms/mydraft/providers.py`，放入 4.2.4 的 `algorithm_spec()`、4.3.4 的端口（复用 dflash 的 reader/normalizer/collator）、4.4.4 的策略，以及 `create_registration()`。
2. 在 `specforge/algorithms/builtin.py` 顶部加 `from specforge.algorithms.mydraft.providers import create_registration as mydraft`，并在元组里追加 `mydraft()`。
3. 写一份最小 YAML，设 `training.strategy: mydraft`，运行 `specforge train --plan --config mydraft.yaml`。

**需要观察的现象**：

- 若 `mydraft()` 未加入元组，`--plan` 会报 `unknown algorithm 'mydraft'; registered algorithms: [...]`（registry.py:82-83）。
- 若契约与端口 parity 不一致，错误发生在 import / registry 构造期（make_registration），早于 `--plan` 打印。
- `--plan` 成功打印计划即证明「解析 + 校验」两段通过（不占 GPU、不起训练）。

**预期结果**：`--plan` 能识别 `mydraft` 并渲染出与 DFlash 同形的进程计划。

> **待本地验证**：完整 `train` 需要真实草稿模型权重与离线特征文件，且 `mydraft_model` 的前向实现需要你自行完成。本实践以 `--plan` 通过为验证终点。

#### 4.5.5 小练习与答案

**练习 1**：为什么 SpecForge 不用「自动扫描 `algorithms/` 目录」来发现新算法？
**答案**：因为显式目录保证「注册即被对账校验、顺序确定（按名字排序）、无副作用」。自动扫描会引入隐式顺序与导入时机问题，破坏注册表的不可变性与确定性（见 u4-l2、customization.md:135-136 明确禁止「第二个可变注册表」）。

**练习 2**：新增 `mydraft` 后，需要改动组合根 `composition.py` 或校验器 `planning.py` 吗？
**答案**：不需要。组合根与六道校验器都是**算法无关**的——它们只消费 `AlgorithmRegistration` 的通用接口（u3-l4）。新增算法只改 `algorithms/`（providers + builtin）与 `training/strategies/`（策略类）。

---

## 5. 综合实践

**任务**：端到端设计算法 `mydraft`（一个离线、文本、硬标签、复用 DFlash 捕获的算法），交付三件套 + 注册，并用 `--plan` 验证可被 `training.strategy` 选中。

**交付物清单**：

1. **契约**：写一份 `AlgorithmSpec`，含一个 `OFFLINE` 文本 `FeatureContract`（`required_tensors={"input_ids","loss_mask","hidden_states"}`，带 `OfflineStorageContract`）。说明：是否需要再声明 `STREAMING` 契约？（答：若只做离线则不需要，且端口侧也不能给 `server_streaming`，否则 parity 失败。）
2. **端口清单**：列出 `step`/`model`/`offline` 三个端口的关键取值，特别注明：
   - `step.uses_external_target_head=False`（硬标签不需要目标头）；
   - `offline[0].capture_layout.capture_method="dflash"`（复用现有捕获钩子，因 capture.py 只认 eagle3/dflash）；
   - `offline[0].capture_layout.output_names` 必须等于契约 `required_tensors`。
3. **策略**：写一个 `MyDraftTrainStrategy`，`required_features` 与契约 `required_tensors` 严格对齐，`forward_loss` 返回 `StepOutput`，`checkpoint_state_filter` 只剥 `draft_model.` 前缀。
4. **注册**：写 `create_registration()`，并在 `builtin_algorithm_registry()` 元组里追加 `mydraft()`。
5. **验证**：运行 `specforge train --plan --config mydraft.yaml`（`training.strategy: mydraft`），确认计划能渲染、不报 `unknown algorithm`。

**自检问题**（做完后回答）：

- 你的契约里有没有混入任何模型类或函数？（若有，`_assert_pure_value` 会在构造期拒绝。）
- 你的端口 `(mode, modality)` 键集合是否与契约完全相等？
- 你的策略 `required_features` 是否与契约 `required_tensors` 一致？
- 你是否动了 `composition.py` 或 `planning.py`？（正确答案：不应动。）

## 6. 本讲小结

- 新增训练算法 = 交付「三件套」：纯契约 `AlgorithmSpec`、可执行端口 `AlgorithmProviders`、训练策略 `DraftTrainStrategy`，三者由 `make_registration` 焊成一个 `AlgorithmRegistration`。
- 契约必须**只含纯值**：`_assert_pure_value` 递归拒绝一切 `callable`/`type`，保证解析期轻量、可哈希；模型类与工厂只能放端口里、且需懒导入。
- 契约与端口是**双面镜像**：`(mode, modality)` 键集合、`capture_layout.output_names` 与 `storage.required_tensors`、`normalizer_id` 都必须完全相等，任何漂移在注册期 fail-fast。
- 算法的训练数学只写在一个地方：策略类的 `forward_loss`；`TrainerCore.train_step` 无分支，`required_features` 必须与契约张量集合对齐。
- `training.strategy: mydraft` 命中的唯一落点是 `builtin_algorithm_registry()` 的显式元组——组合根与校验器算法无关，新增算法只改 `algorithms/` 与 `training/strategies/`。
- 现实约束：离线 `capture_method` 目前只支持 `eagle3`/`dflash`；真正新的捕获方式需同步扩展 `offline_capture/` 与 SGLang 钩子。

## 7. 下一步学习建议

- **补测试**：参考 u10-l3，为 `mydraft` 在 `tests/algorithms/`、`tests/application/`、`tests/training/` 下补齐契约 parity、配置校验、策略前向的最少测试。
- **端到端门禁**：阅读 `scripts/gates/run_disaggregated_overfit_gate.sh`，理解 overfit gate 如何验证一个算法的「能前向、能反传、能存盘、能恢复」。
- **若需要新捕获方式**：转向 u5-l3（离线特征生成）与 `specforge/offline_capture/`，理解如何新增一个目标侧捕获钩子（并同步改 SGLang 侧）。
- **若需要新草稿架构**：回到 u10-l1，用 `@register_draft` 新增架构；记住架构轴与算法轴独立，可组合使用。
- **深入损失算子**：若 `mydraft` 需要更复杂的 loss（如接受率目标、TTT 多步），阅读 u6-l5 的 `core/loss.py`、`lk_loss.py`、`compact_teacher.py`，把数值实现收敛进 core、与并行/显存策略解耦。
