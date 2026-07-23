# 算法 providers 与扩展端口

## 1. 本讲目标

本讲承接 u4-l1（算法纯契约）与 u4-l2（算法注册表）。在那里我们学到：`AlgorithmSpec` 是一份**只含值、不含任何可执行对象**的纯契约，而 `AlgorithmRegistration` 把这份纯契约和它的「可执行半边」绑在一起。本讲就回答「这个可执行半边到底是什么、长什么样、由谁提供」。

读完本讲，你应当能够：

1. 说出 provider 端口（`model` / `offline` / `server_streaming` / `step`）各自的职责，并理解它们「只描述算法行为、不描述部署拓扑」的设计边界。
2. 理解 `resolve_capture_layers` 如何从「运行覆盖 → 草稿配置 → 目标模型结构」三级回退中决定要从目标模型的哪些层抽取隐藏状态，并能区分 EAGLE3 与 DFlash 家族在这一步上的根本差异。
3. 读懂 `OfflineCaptureLayout` / `ServerCaptureLayout` 如何把通用的四类捕获来源翻译成各算法自己的离线特征文件 schema，并解释为什么 `make_registration` 会在注册期就把契约与 provider 对齐（parity）。
4. 动手用内置注册表把任一算法的离线 capture layout 物化出来，对照其离线特征 schema。

---

## 2. 前置知识

本讲默认你已经读过：

- **u1-l4 EAGLE3 特征式草稿原理**：知道 EAGLE3 从目标模型多个深度层抽取隐藏状态拼接成输入，知道「捕获层」是什么。
- **u4-l1 算法契约 contracts**：知道 `AlgorithmSpec`、`FeatureContract(mode, modality)`、`FeatureMode.OFFLINE/STREAMING`、`OfflineStorageContract`、`_assert_pure_value` 的纯值约束。
- **u4-l2 算法注册表 registry 与 builtin**：知道 `AlgorithmRegistration(spec, providers)` 的「双半绑定」，知道 `providers` 字段类型被刻意标成 `object` 以保持 `registry.py` 不导入重依赖。

几个本讲会用到的术语，先用一句话定位：

- **provider 端口（port）**：一个算法「会动的代码」对外暴露的、名字固定的钩子（hook）插槽。组合根和装配层只认这些插槽，不认具体算法名。
- **懒导入（lazy import）**：把 `import torch` / `from transformers import ...` 这类重依赖放进函数体内部，使「解析注册表」这一步保持轻量、无副作用。
- **parity（对齐 / 奇偶校验）**：契约声明了哪些 `(mode, modality)` 特征，provider 就必须提供同样这些 `(mode, modality)` 的数据适配器，多一个少一个都会在注册期报错。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `specforge/algorithms/common/providers.py` | provider 端口的**共享抽象**：所有端口类型（`ModelProvider`/`OfflineDataProvider`/`ServerStreamingProvider`/`StepProvider` 等）的数据类定义、`AlgorithmProviders` 容器、以及 `make_registration` 的契约-provider 对齐校验。 |
| `specforge/algorithms/eagle3/providers.py` | EAGLE3 算法的 `algorithm_spec()`（纯契约）与 `algorithm_providers()`（可执行端口）的**具体接线**，是理解所有算法 provider 的范本。 |
| `specforge/algorithms/model_providers.py` | 各算法共用的「模型装配」实现：构造草稿模型、构造训练模型、`resolve_eagle_capture_layers` / `resolve_dflash_capture_layers` 等捕获层解析逻辑。 |
| `specforge/algorithms/dflash/providers.py` | DFlash 的 spec + providers，与 EAGLE3 形成对比（捕获层少、特征 schema 不同）。 |
| `specforge/algorithms/dspark/providers.py` / `domino/providers.py` | DFlash 家族的另外两个成员，复用同一套数据适配器，但 DSpark 多一份目标末层特征。 |
| `specforge/algorithms/peagle/providers.py` | P-EAGLE：**只声明 STREAMING 契约、没有 offline provider**，用来理解「端口缺失」也是合法且被强校验的。 |
| `specforge/algorithms/eagle3/data.py` / `common/dflash_family_data.py` | 算法拥有的 reader / normalizer / collator 工厂。 |
| `specforge/algorithms/common/defaults.py` | 多个算法共享的默认钩子（`empty_options`、`no_missing_checkpoint_keys`、`one_loss_token`、`online_needs_input_tools`）。 |

---

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：

1. **4.1 provider 端口体系与共享抽象**（`common/providers.py`）——先建立整体地图。
2. **4.2 model provider 与捕获层解析**——草稿模型怎么造、捕获哪些目标层。
3. **4.3 offline_for 与 capture_layout**——离线 / 在线特征文件 schema 由谁定。
4. **4.4 collation 与 defaults**——原始记录如何对齐成训练 batch。

---

### 4.1 provider 端口体系与共享抽象

#### 4.1.1 概念说明

`contracts.py` 里的 `AlgorithmSpec` 被 `_assert_pure_value` 严格守卫，**不能含任何 callable 或类**（详见 u4-l1）。那么算法真正「会动的代码」放哪？答案就是 provider。

`common/providers.py` 的模块 docstring 把这件事说得非常直白：

> The public `AlgorithmSpec` deliberately contains values only. This module is the **executable half** of one `AlgorithmRegistration`: step/model factories and data adapters selected by the application composition root.
>
> ——[specforge/algorithms/common/providers.py:1-12](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/common/providers.py#L1-L12)

所以一个 `AlgorithmRegistration` 由两半拼成：左半边 `spec`（纯值契约），右半边 `providers`（可执行端口）。`AlgorithmProviders` 就是这个右半边的**容器**，它把一个算法所有「会动的代码」按端口分组放好：

- `step`：训练步工厂与检查点语义（`StepProvider`）。
- `model`：草稿模型 / 训练模型的装配钩子（`ModelProvider`）。
- `offline`：离线特征的 reader / normalizer / collator / 捕获布局，一个元组（`OfflineDataProvider`）。
- `server_streaming`：在线服务端捕获特征的适配器，一个元组（`ServerStreamingProvider`）。
- `vocab_mapping_modes`：声明该算法在哪些 `FeatureMode` 下支持 vocab mapping。

有一条贯穿全模块的设计红线，docstring 也强调了：

> The provider types describe **algorithm-owned behavior, never deployment topology**. In particular, `ServerStreamingProvider` means that an algorithm *can consume* features captured by an external server; it does not select a backend, start a server, or construct a transport.

也就是说：provider 只回答「这个算法需要什么样的特征、怎么把它喂给训练」，**不回答**「特征从哪台机器、用什么传输送来」。后者是运行时（u7）的事。记住这条边界，下面所有端口都不会越界。

#### 4.1.2 核心流程

一个算法的 provider 容器是这样被构造和校验的：

1. 某算法包（如 `eagle3/providers.py`）定义 `algorithm_providers()`，返回一个填好所有钩子的 `AlgorithmProviders`。
2. 同一个包定义 `create_registration()`，调用 `make_registration(algorithm_spec(), algorithm_providers())`。
3. `make_registration` 把「契约声明」与「provider 实际提供」逐项比对（parity），任何不一致都立刻抛 `ValueError`。
4. 校验通过后，`AlgorithmRegistration(spec=..., providers=...)` 被注册进 `builtin_algorithm_registry()`（u4-l2）。

`AlgorithmProviders` 自己也在构造时做轻量校验：名字非空、`step`/`model` 类型正确、offline 与 server_streaming 两个元组里 modality 不能重复。然后提供两个按 modality 查找的方法 `offline_for(modality)` 与 `server_streaming_for(modality)`，找不到就抛 `KeyError` 并列出可用 modality。

关键不变量：**provider 容器在「解析注册表」这一刻就装配完毕且不可变**（所有端口都是 `frozen=True` dataclass）。因此注册表解析必须保持无副作用——这靠「重依赖全部懒导入」实现（见 4.1.3）。

#### 4.1.3 源码精读

**容器与按 modality 查找。** `AlgorithmProviders` 持有全部端口，并暴露按 modality 取 provider 的入口：

[specforge/algorithms/common/providers.py:581-637](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/common/providers.py#L581-L637) —— `AlgorithmProviders` 的定义与 `offline_for` / `server_streaming_for` / `_provider_for` 三个方法。注意构造时 `offline` 与 `server_streaming` 都被 `tuple(...)` 固化、modality 去重、`vocab_mapping_modes` 被转成 `frozenset`。

**契约-provider 对齐（parity）。** `make_registration` 是整个 provider 体系的「关卡」，它把契约与 provider 逐条对齐：

[specforge/algorithms/common/providers.py:640-680](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/common/providers.py#L640-L680) —— 这里做了四道关键校验：

- 算法名一致：`providers.algorithm_name == spec.name`。
- 草稿架构兼容：`providers.model.draft_config.architecture` 必须在 `spec.draft.compatible_architectures` 里。
- **特征键集合相等**：契约里所有 `(mode, modality)` 键，必须与 provider 的 offline+server_streaming 提供的键集合**完全相等**（第 659–672 行）。这就是 parity 的核心——契约声明了几种特征，provider 就必须正好提供这几种。
- vocab mapping 能力与 `vocab_mapping_modes` 必须一致（第 673–685 行）。

[specforge/algorithms/common/providers.py:687-750](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/common/providers.py#L687-L750) —— 对每个 offline provider 继续校验：normalizer 名必须与契约 `storage.normalizer` 一致；capture_layout 物化出的特征名集合必须**正好等于** `storage.required_tensors`；offline 与同 modality 的 server_streaming 的 `capture_method` 必须一致。这段直接保证了「离线特征文件 schema 不会偷偷漂移」。

**懒导入纪律。** 看 EAGLE3 的两个钩子就能体会「重依赖在函数体内」：

[specforge/algorithms/eagle3/providers.py:45-52](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/providers.py#L45-L52) —— `build_step` 把 `from specforge.training.strategies.base import Eagle3TrainStrategy` 放在函数体里，调用时才导入。

[specforge/algorithms/eagle3/providers.py:111-114](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/providers.py#L111-L114) —— `resolve_capture_layers` 同样把真正的 `resolve_eagle_capture_layers` 延迟到调用时才 import。

正因如此，`specforge train --plan` 这类不占 GPU、不导入 torch 的场景也能安全地完成注册表解析与拓扑计划（承接 u2-l3、u3-l2）。

**端口入口三件套。** EAGLE3 把自己的契约与端口各自封装成函数，再由 `create_registration` 拼装：

[specforge/algorithms/eagle3/providers.py:235-236](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/providers.py#L235-L236) —— `create_registration()` 只有一行：`return make_registration(algorithm_spec(), algorithm_providers())`。这是所有内置算法统一的注册入口形态。

#### 4.1.4 代码实践

**实践目标：** 亲手验证「契约与 provider 必须对齐」这条 parity 规则，并观察端口缺失时会发生什么。

**操作步骤（源码阅读型）：**

1. 打开 `specforge/algorithms/peagle/providers.py`，注意它的 `algorithm_spec()` **只声明了一个 STREAMING 契约**（没有 OFFLINE）：

   [specforge/algorithms/peagle/providers.py:72-102](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/peagle/providers.py#L72-L102)

2. 再看它的 `algorithm_providers()`，确认它**只填了 `server_streaming`、没有 `offline` 元组**：

   [specforge/algorithms/peagle/providers.py:105-152](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/peagle/providers.py#L105-L152)

3. 对照 4.1.3 里 `make_registration` 第 659–672 行的 parity 校验，回答：如果给 P-EAGLE 错误地加一个 `offline=` provider，但不同步在 `algorithm_spec()` 里加 OFFLINE 契约，`create_registration()` 会怎样？

**需要观察的现象 / 预期结果：**

- P-EAGLE 契约与 provider 都「只在线、不离线」，两者键集合相等 → 注册成功。
- 若只在一侧加 offline，`expected != provided` → 抛 `ValueError`，报错信息会同时打印 `contracts=...` 与 `providers=...` 两边的 `(mode, modality)` 列表，告诉你差在哪。
- 结论：**端口的「有」与「无」本身就是被强校验的算法属性**，不是可有可无的可选项。P-EAGLE 不支持离线训练这件事，是注册期就锁死的。

> 说明：本实践为源码阅读型，不依赖 GPU；若想在本地复现报错，可在虚拟环境里 `python -c "from specforge.algorithms.peagle.providers import create_registration; create_registration()"` 并临时给 `algorithm_providers()` 加一个 offline 元组观察抛错（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1：** 为什么 `AlgorithmProviders` 的 `providers` 字段在 `registry.py` 里被标注成 `object`（见 u4-l2），而在这里它其实是个非常具体的 `AlgorithmProviders`？

**答案：** 这是为了**打破循环导入**。`registry.py` 若导入 `AlgorithmProviders`，就会拉进 `contracts`、再拉进一堆 typing；把字段标成 `object` 让 `registry.py` 保持只依赖最小集合的叶子模块，而真正的类型定义与校验留在 `common/providers.py`。组合根和装配层在真正用到端口时，才依赖具体类型。

**练习 2：** `make_registration` 里有一句「offline normalizer 名必须等于契约 `storage.normalizer`」。请说明这条校验防止了什么故障。

**答案：** 它防止「数据准备脚本写的离线文件用了一种归一化约定（normalizer A），而算法 reader 期望另一种（normalizer B）」这种**静默错配**。因为 normalizer 名是一根字符串纽带，两端对不上就在注册期 fail-fast，而不是等训练时 loss 异常才被发现。

---

### 4.2 model provider 与捕获层解析

#### 4.2.1 概念说明

`ModelProvider` 是 provider 体系里最重要的一类端口，它回答两个问题：

1. **草稿模型怎么造？** 提供 `build_draft`（造草稿模型）与 `build_training_model`（把草稿模型包装成可训练的、含目标头/损失逻辑的训练模型）两个工厂。
2. **要从目标模型的哪些层抽取特征？** 提供 `resolve_capture_layers`，返回一组目标层的索引（整数列表）。

这里要特别区分两条容易混淆的「轴线」（承接 u4-l4）：

- **草稿架构（draft architecture）**：草稿模型本身的网络结构（如 `LlamaForCausalLMEagle3`、`DFlashDraftModel`），由 `DraftModelRegistry` / `@register_draft` 管理，是**独立于算法**的一条轴。
- **捕获层（capture layers）**：要从**目标模型**的哪些层抽隐藏状态喂给草稿。这是**算法决定**的——EAGLE3 要多层拼接，DFlash 只要少数几层。

`ModelProvider` 还顺带携带一些算法策略：`minimum_loss_tokens`（一个样本至少要有几个可监督 token）、`needs_input_tools`（是否需要加载 tokenizer 等输入工具）、`default_dataloader_num_workers`（数据加载默认并发数）、`allow_missing_warm_start_embedding`（热启动时是否允许缺 embedding 权重）。

#### 4.2.2 核心流程

**捕获层解析的三级回退（以 EAGLE3 为例）。** EAGLE3 默认要从目标模型的 3 个深度层各抽一份隐藏状态（承接 u1-l4）。具体取哪 3 层，`resolve_eagle_capture_layers` 按下面的优先级回退：

1. **运行覆盖优先**：如果用户在 run config 里填了 `model.aux_hidden_state_layer_ids`，直接用它。
2. **草稿配置次之**：否则读草稿配置里 `eagle_config.eagle_aux_hidden_state_layer_ids`。
3. **目标模型结构兜底**：都没有时，按目标模型层数 \(L\) 用固定公式推导：

\[
[\,1,\ \ L//2 - 1,\ \ L - 4\,]
\]

即「浅层 1、中层 \(L//2-1\)、深层 \(L-4\)」三个分辨率。最后强校验：必须正好 3 个非负整数，否则报错。

**DFlash 家族的捕获层解析。** DFlash / Domino / DSpark 走完全不同的路子：捕获层不是按公式推导，而是写在草稿配置 `dflash_config.target_layer_ids` 里；而这些 id 通常由「目标配置生成草稿配置」时的 `populate` 钩子自动填好（默认只取 1 层），用户可用 `model.draft_num_hidden_layers` 覆盖层数。换句话说：

- EAGLE3：**总是 3 层**（多层隐藏状态拼接），层数不可由用户随意改（校验死成 3）。
- DFlash 家族：**层数可配**（默认 1），选哪几层由 `build_target_layer_ids(L, n)` 决定，多层的隐藏状态在宽度上**拼接**进同一个 `hidden_states` 张量（宽度 = 层数 × 隐藏维）。

**草稿配置的「派生默认」。** 当草稿配置需要由目标配置生成时，`TargetDerivedDraftDefaults` 提供 `model_type`、`num_hidden_layers`、`draft_vocab_size`，以及一个可选的 `populate` 钩子——后者正是 DFlash 用来填 `target_layer_ids` 与 `block_size` 的接缝。

#### 4.2.3 源码精读

**ModelProvider 与 DraftConfigProvider。**

[specforge/algorithms/common/providers.py:348-386](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/common/providers.py#L348-L386) —— `ModelProvider`：注意它只持有**函数引用**（`build_draft`/`build_training_model`/`resolve_capture_layers`/`minimum_loss_tokens`/`needs_input_tools`），构造时用 `_factories_are_callable` 校验它们确实可调用；docstring 点明「把具体模型导入留在钩子函数里，注册表解析就不会导入 Torch/Transformers」。

[specforge/algorithms/common/providers.py:156-179](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/common/providers.py#L156-L179) —— `DraftConfigProvider`：只装「架构名 + 派生默认 + 覆盖钩子」，**不含模型/config 类，也不加载 config**。docstring 强调加载 config 与解析 model/config class 仍是 `DraftModelRegistry` 的职责。

[specforge/algorithms/common/providers.py:124-154](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/common/providers.py#L124-L154) —— `TargetDerivedDraftDefaults`：`populate` 是「算法拥有的派生值接缝」，比如 DFlash 的 target-layer ids。

**捕获层解析——EAGLE3 三级回退。**

[specforge/algorithms/model_providers.py:188-211](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/model_providers.py#L188-L211) —— `resolve_eagle_capture_layers`：依次看 `cfg.model.aux_hidden_state_layer_ids` → `eagle_config.eagle_aux_hidden_state_layer_ids` → 目标层数兜底公式 `[1, num_layers // 2 - 1, num_layers - 4]`，并在末尾强校验「正好 3 个非负整数」。

**捕获层解析——DFlash 家族（读配置、不推导）。**

[specforge/algorithms/model_providers.py:214-225](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/model_providers.py#L214-L225) —— `resolve_dflash_capture_layers`：直接读 `dflash_config.target_layer_ids`，为空就报错「draft config does not define target capture layer ids」。注意它**不**接受公式兜底，因为这些 id 必须由生成阶段写好。

**DFlash 的 populate 与覆盖。**

[specforge/algorithms/model_providers.py:447-462](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/model_providers.py#L447-L462) —— `populate_dflash_generated_config`：由目标 `num_hidden_layers` 生成草稿配置，固定 `block_size=16`，并用 `build_target_layer_ids(target_layers, 1)` 写入**默认 1 个**捕获层。

[specforge/algorithms/model_providers.py:465-475](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/model_providers.py#L465-L475) —— `apply_dflash_overrides`：当用户填了 `model.draft_num_hidden_layers` 时，用 `build_target_layer_ids(target_layers, n)` 重新生成 n 个捕获层。这就是 DFlash 家族「层数可配」的落点。

**两个训练模型工厂——体会算法差异。**

[specforge/algorithms/model_providers.py:239-272](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/model_providers.py#L239-L272) —— `build_eagle3_model`：把草稿模型包进 `OnlineEagle3Model`（携带 `ttt_length`、`lk_loss_type`、`kl_scale` 等 EAGLE3 专属目标参数），并在离线/消费者角色下额外加载一个冻结的 `TargetHead`（返回 `AlgorithmModelParts(model=..., target_head=...)`）。

[specforge/algorithms/model_providers.py:313-354](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/model_providers.py#L313-L354) —— `_build_dflash_family_model`：DFlash/Domino/DSpark 共用的装配逻辑——加载目标模型的 embedding 与 lm_head、解析 mask_token_id、把 `target_layer_ids` 写回草稿配置，最后把 `capture_layers` 一并塞进 `AlgorithmModelParts`（注意这里 `capture_layers=list(draft_model.target_layer_ids)`，与 4.2.2 的解析结果一致）。

**组合根如何调用这些钩子。**

[specforge/application/composition.py:91-93](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/composition.py#L91-L93) —— 离线特征准备路径里，组合根直接调 `model_provider.resolve_capture_layers(cfg, draft_config, target_config)` 得到要捕获的层，并随后强校验它们是「互不相同的非负整数且不超过目标模型深度」（第 94–115 行）。这正是 provider 端口被组合根消费的真实落点。

#### 4.2.4 代码实践

**实践目标：** 通过源码阅读，预测 EAGLE3 与 DFlash 在「同一个 32 层目标模型」上分别会捕获哪些层，并解释差异。

**操作步骤（源码阅读型 + 可选运行）：**

1. 假设目标模型 `num_hidden_layers = 32`，且用户**没有**填任何捕获层覆盖。
2. 套用 `resolve_eagle_capture_layers` 的兜底公式，算出 EAGLE3 的 3 个捕获层。
3. 套用 `populate_dflash_generated_config`（默认 `build_target_layer_ids(32, 1)`），说明 DFlash 默认会捕获几层。
4. （可选）在装好 specforge 的环境里，构造一个最小的假 `cfg`/`draft_config`/`target_config` 直接调用这两个函数验证（待本地验证：`resolve_eagle_capture_layers` 需要 `cfg.model.aux_hidden_state_layer_ids` 属性，需 mock）。

**需要观察的现象 / 预期结果：**

- EAGLE3：\([1,\ 32//2-1,\ 32-4] = [1, 15, 28]\)，固定 3 层。
- DFlash：默认 `build_target_layer_ids(32, 1)` → 仅 1 个捕获层（具体 id 由该函数决定，需读 `specforge/modeling/draft/dflash.py` 的 `build_target_layer_ids`）。
- **差异本质**：EAGLE3 靠「多层隐藏状态拼接」获得多分辨率输入（承接 u1-l4），所以强制 3 层；DFlash 家族靠「少数层的隐藏状态 + 自身结构」建模，捕获层数可配、默认更少，多层的特征是在**张量宽度上拼接**而非当作独立输入。

#### 4.2.5 小练习与答案

**练习 1：** 如果用户在 run config 里把 `model.aux_hidden_state_layer_ids` 填成 4 个整数，EAGLE3 会发生什么？

**答案：** `resolve_eagle_capture_layers` 末尾的校验（第 206–210 行）要求「正好 3 个非负整数」，4 个会抛 `ValueError`。也就是说，EAGLE3 的「3 层」是算法层面的硬约束，用户能改的是**取哪 3 层**，而不是**层数**。

**练习 2：** 为什么 `resolve_dflash_capture_layers` 在 `target_layer_ids` 为空时直接报错，而不像 EAGLE3 那样给个公式兜底？

**答案：** 因为 DFlash 的捕获层 id 必须由「目标配置生成草稿配置」阶段（`populate_dflash_generated_config`）写进 `dflash_config.target_layer_ids`。如果走到解析这一步发现它是空的，说明上游生成阶段没跑或配置被破坏，这是一种**不该静默兜底的异常状态**，所以选择 fail-fast 而不是猜测。

---

### 4.3 offline_for 与 capture_layout

#### 4.3.1 概念说明

`OfflineDataProvider` 与 `ServerStreamingProvider` 回答「特征数据怎么读、怎么归一化、怎么拼 batch」。其中最关键、也最容易被忽略的设计是 **capture layout（捕获布局）**。

无论哪个算法，本地特征捕获都会产出**同样四类**每样本来源：

- `input_ids`（输入 token id）
- `loss_mask`（损失掩码，标识哪些 token 可监督）
- `aux_hidden_states`（辅助隐藏状态——目标模型若干层的拼接）
- `last_hidden_states`（目标模型最后一层的隐藏状态）

但这四类是**通用名**。每个算法在持久化成离线文件时，要用**自己的名字**。`OfflineCaptureLayout` 就是这张「通用名 → 算法名」的翻译表；它的 `materialize(sources)` 方法把通用来源组装成一条按算法 schema 命名的记录。

这套设计的价值在于：数据准备脚本（`scripts/prepare_hidden_states.py`）只产出通用四类，**不需要知道每个算法叫什么名字**；算法自己的 reader 又只认自己的名字。两者通过 capture layout 这张表对接，且这张表会在注册期被 `make_registration` 校验——保证「物化出的特征名集合」正好等于「契约声明的 `storage.required_tensors`」。

在线场景对应 `ServerCaptureLayout`：它把外部服务器（SGLang）捕获的通用产物，映射成算法在 streaming 模式下需要的特征名，并额外允许带一个 `attention_mask_feature`。

#### 4.3.2 核心流程

**离线物化流程：**

1. 数据准备脚本对每个样本产出通用四类来源（`input_ids`/`loss_mask`/`aux_hidden_states`/`last_hidden_states`）。
2. 调用 `offline.capture_layout.materialize(sources)`：
   - 先按 `passthrough` 把 `(通用名 → 算法名)` 的直通字段拷过去（如 `input_ids → input_ids`、`loss_mask → loss_mask`）。
   - 若声明了 `aux_feature`，把通用 `aux_hidden_states` 重命名为算法名（如 EAGLE3 的 `aux_hidden_state`、DFlash 的 `hidden_states`）。
   - 若声明了 `last_hidden_feature`，把通用 `last_hidden_states` 重命名为算法名（如 EAGLE3 的 `hidden_state`、DSpark 的 `target_last_hidden_states`）。
   - 任何需要的来源缺失或为 `None`，立刻抛 `KeyError`/`ValueError`。
3. 物化出的记录里，特征名集合必须**正好等于**契约的 `storage.required_tensors`（注册期已校验）。

**output_names 的确定性顺序：** `passthrough` 在前，随后 `aux_feature`，最后 `last_hidden_feature`——这个顺序就是持久化特征在记录里的确定顺序，方便 reader 按相同顺序读回。

#### 4.3.3 源码精读

**OfflineCaptureLayout 的定义、校验与物化。**

[specforge/algorithms/common/providers.py:389-463](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/common/providers.py#L389-L463) —— 关键点：

- 第 400–403 行：四个字段 `capture_method` / `aux_feature` / `last_hidden_feature` / `passthrough`。
- 第 418–427 行：构造时校验「输出特征名无重复」且「至少输出一个特征」。
- 第 429–438 行 `output_names`：返回确定顺序的特征名列表。
- 第 440–463 行 `materialize`：按 passthrough + aux + last_hidden 的顺序组装记录，缺来源即报错（报错信息同时指出 source key 与 output feature name，便于排查）。

**OfflineDataProvider 与按 modality 查找。**

[specforge/algorithms/common/providers.py:466-491](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/common/providers.py#L466-L491) —— `OfflineDataProvider` 持有 `modality`/`normalizer_id`/三个工厂（reader/normalizer/collator）以及可选的 `capture_layout`。

[specforge/algorithms/common/providers.py:619-637](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/common/providers.py#L619-L637) —— `offline_for(modality)` / `server_streaming_for(modality)` 与底层 `_provider_for`：找不到时抛 `KeyError` 并列出可用 modality。

**ServerCaptureLayout（在线对称物）。**

[specforge/algorithms/common/providers.py:494-523](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/common/providers.py#L494-L523) —— 与离线版结构对称，但 `passthrough` 每项多带一个 `trailing_shape`（尾随形状，描述服务器返回负载的形状信息），并可带 `attention_mask_feature`。

**三个算法的具体 layout——差异一目了然。**

[specforge/algorithms/eagle3/providers.py:196-213](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/providers.py#L196-L213) —— EAGLE3 离线 layout：`aux_feature="aux_hidden_state"`、`last_hidden_feature="hidden_state"`，外加 `input_ids`/`loss_mask` 直通。即 EAGLE3 离线文件里有 4 个张量：`input_ids, loss_mask, aux_hidden_state, hidden_state`。

[specforge/algorithms/dflash/providers.py:194-211](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/dflash/providers.py#L194-L211) —— DFlash 离线 layout：`aux_feature="hidden_states"`、`last_hidden_feature=None`（**不要**最后一层），只有 3 个张量：`input_ids, loss_mask, hidden_states`。

[specforge/algorithms/dspark/providers.py:167-184](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/dspark/providers.py#L167-L184) —— DSpark 离线 layout：在 DFlash 基础上多了 `last_hidden_feature="target_last_hidden_states"`，共 4 个张量：`input_ids, loss_mask, hidden_states, target_last_hidden_states`。

**测试是最佳文档。** 仓库的测试把每个算法的「通用来源 → 算法特征名」映射写得清清楚楚：

[tests/test_algorithms/test_offline_capture_layout.py:18-76](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/tests/test_algorithms/test_offline_capture_layout.py#L18-L76) —— `test_builtin_offline_layouts_materialize_exact_storage_schemas`：用同一份通用 `sources` 喂给四个算法的 `capture_layout.materialize`，断言产出的记录键集合正好等于各自契约的 `storage.required_tensors`。`expected_sources`（第 19–42 行）就是一张完整的对照表。

**组合根如何消费 layout。**

[specforge/application/composition.py:117-129](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/composition.py#L117-L129) —— 组合根调 `offline_for(cfg.model.input_modality)` 拿到 provider，要求它必须有 `capture_layout`（否则报「能消费离线特征但没注册本地捕获布局」），然后把 `capture_method` 与 `layout` 一并交给离线捕获执行器。

#### 4.3.4 代码实践

**实践目标：** 用真实注册表，把四个算法的离线 capture layout 物化出来，亲眼看到它们离线特征文件 schema 的差异。

**操作步骤（可运行，不占 GPU，但需 specforge 环境）：**

```python
# 示例代码：对照 test_offline_capture_layout.py 改写
import torch
from specforge.algorithms.builtin import builtin_algorithm_registry

registry = builtin_algorithm_registry()

# 通用四类来源（数据准备脚本实际产出的就是这四类）
sources = {
    "input_ids": torch.tensor([1, 2, 3]),
    "loss_mask": torch.tensor([1, 1, 0]),
    "aux_hidden_states": torch.randn(1, 3, 5 * 8),   # 5 层 × 隐藏维 8
    "last_hidden_states": torch.randn(1, 3, 8),
}

for name in ("eagle3", "dflash", "domino", "dspark"):
    provider = registry.resolve(name).providers.offline_for("text")
    record = provider.capture_layout.materialize(sources)
    print(f"{name:8s} -> {sorted(record)}")
```

**需要观察的现象 / 预期结果：**

- `eagle3 ` → `['aux_hidden_state', 'hidden_state', 'input_ids', 'loss_mask']`
- `dflash ` → `['hidden_states', 'input_ids', 'loss_mask']`
- `domino ` → `['hidden_states', 'input_ids', 'loss_mask']`（与 dflash 同）
- `dspark ` → `['hidden_states', 'input_ids', 'loss_mask', 'target_last_hidden_states']`

可以看到：**同一份通用来源，经不同 layout 物化，得到不同的离线 schema**。EAGLE3 把辅助状态叫 `aux_hidden_state`、把末层叫 `hidden_state`；DFlash/Domino 把辅助状态合并叫 `hidden_states`、不要末层；DSpark 在 DFlash 基础上多要一份 `target_last_hidden_states`。

> 说明：本实践与仓库测试同源，逻辑可复现；但因需导入 `torch` 与 `specforge`，具体运行结果以本地环境为准（若环境就绪，输出应与上述一致）。

#### 4.3.5 小练习与答案

**练习 1：** 如果 DFlash 的离线文件里误存了一个额外的 `hidden_state`（单数）张量，会怎样？

**答案：** 不会被静默接受。`make_registration` 在注册期已校验「capture_layout 物化出的特征名集合 == 契约 `storage.required_tensors`」；而 reader 只读契约要求的那些键。多出来的 `hidden_state` 不在契约里，reader 不会读它；如果某处严格按 schema 校验文件，会因多键而报错。核心是：**离线文件 schema 由契约单方面声明，layout 只是保证物化端不漂移**。

**练习 2：** 为什么 `OfflineCaptureLayout` 同时支持 `aux_feature=None` 和 `last_hidden_feature=None`？

**答案：** 因为不同算法需要的特征子集不同。DFlash 不需要目标末层（`last_hidden_feature=None`），EAGLE3 两者都要，DSpark 还把末层单独命名成 `target_last_hidden_states`。允许这两个字段为 `None`，让一张通用翻译表能表达「只要其中一部分」的算法，而校验仍由 `make_registration` 的 required_tensors parity 兜底。

---

### 4.4 collation 与 defaults

#### 4.4.1 概念说明

到上一模块为止，我们拿到的是「按算法 schema 命名的单条原始记录」。但训练需要的是**一个 batch**，而且要把算法专属的字段对齐成模型前向期望的形状。这一步由三类工厂负责：

- **reader**（`build_reader`）：从离线特征文件（manifest + 张量）读出原始记录。
- **normalizer**（`build_normalizer`）：把原始记录映射成算法训练真正使用的张量名与形状（这一步会做截断、增维、TTT 长度处理等）。
- **collator**（`build_collator`）：把多条记录拼成一个 batch（padding / concatenate）。

`OfflineDataProvider` 同时持有这三个工厂；在线场景则由 `ServerStreamingProvider.build_collator` 负责把服务器返回的产物拼起来。

很多算法在这些钩子上的行为是**相同**的，于是有了 `common/defaults.py`：一组共享的默认钩子，避免每个算法都重写一遍。理解了 defaults，就能快速看懂「为什么有的算法字段填得很短」。

#### 4.4.2 核心流程

**离线样本 → 训练张量 的两步：**

1. **normalize**：reader 给出原始记录（键名是 4.3 物化出的算法名），normalizer 把它重映射成训练张量名。例如 EAGLE3 的 `normalize_offline_sample` 把 `aux_hidden_state → hidden_state`、`hidden_state → target`、补一个全 1 的 `attention_mask`，并按 `max_len` 截断、把 `loss_mask` 末位置零。
2. **collate**：collator 把一个 batch 的样本 padding / concatenate 到统一长度。EAGLE3 用 `DataCollatorWithPadding`；DFlash 家族用 `pad_and_concatenate_features`，并显式声明每个特征的序列轴与必需键。

**几个共享默认的含义：**

- `empty_options`：训练步没有额外选项（DFlash/Domino/DSpark/P-EAGLE 用）。
- `no_missing_checkpoint_keys`：检查点不允许缺任何权重（DFlash 家族用）。
- `one_loss_token`：每个样本最少 1 个可监督 token（EAGLE3/P-EAGLE 用）。
- `online_needs_input_tools`：在线模式才需要加载输入工具（`config.mode == "online"`）。
- DFlash 家族的 `minimum_loss_tokens = 2 * block_size`：因为 DFlash 以 block 为单位，一个样本至少要覆盖两个 block 才有意义。
- DFlash 家族的 `needs_input_tools`：在线模式**或**缺 mask_token_id 时都需要（因为可能要用 tokenizer 解析 mask token）。

#### 4.4.3 源码精读

**EAGLE3 的 normalizer——看一次「字段重映射」。**

[specforge/algorithms/eagle3/data.py:10-27](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/data.py#L10-L27) —— `normalize_offline_sample`：注意它把 `aux_hidden_state` 重命名成训练用的 `hidden_state`、把原始 `hidden_state`（末层）重命名成 `target`，并构造 `attention_mask`、截断到 `max_len`、把 `loss_mask` 最后一个 token 置 0。这正是「物化 schema 名」与「训练张量名」之间的最后一道翻译。

[specforge/algorithms/eagle3/data.py:30-48](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/data.py#L30-L48) —— `build_offline_reader`：返回 `OfflineManifestReader`，指定 `strategy="eagle3"`、`target_repr="hidden_state"`。reader 的重导入注释也再次强调「保持注册表解析无副作用」。

[specforge/algorithms/eagle3/data.py:82-87](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/data.py#L82-L87) —— `build_offline_collator`：返回 `DataCollatorWithPadding()`。

**DFlash 家族共享的 normalizer / collator。**

[specforge/algorithms/common/dflash_family_data.py:36-63](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/common/dflash_family_data.py#L36-L63) —— `normalize_offline_sample`：与 EAGLE3 不同，它**不做 target 投影**，只把 `hidden_states` 规整成 `[1, seq, width]`，并校验 `input_ids`/`loss_mask`/`hidden_states` 三者序列长度一致。

[specforge/algorithms/common/dflash_family_data.py:147-159](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/common/dflash_family_data.py#L147-L159) —— `build_collator`：用 `pad_and_concatenate_features`，显式声明 `sequence_axes`（每个特征在第 1 轴是序列）与 `required_keys`。DSpark 的 `build_dspark_collator`（第 162–180 行）只是在此基础上多加了 `target_last_hidden_states`。

**共享默认钩子。**

[specforge/algorithms/common/defaults.py:6-32](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/common/defaults.py#L6-L32) —— `empty_options` / `no_missing_checkpoint_keys` / `one_loss_token` / `online_needs_input_tools` 四个一行函数。它们让 EAGLE3 与 DFlash 的 `algorithm_providers()` 在这些字段上写得很短（直接引用即可）。

**两个算法级策略钩子（差异点）。**

[specforge/algorithms/model_providers.py:434-444](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/model_providers.py#L434-L444) —— `dflash_min_loss_tokens`：返回 `2 * block_size`，并强校验 `block_size` 是正整数。

[specforge/algorithms/model_providers.py:44-49](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/model_providers.py#L44-L49) —— `dflash_needs_input_tools`：在线**或**缺 mask_token_id 时返回 True。

**EAGLE3 providers 如何引用这些工厂。**

[specforge/algorithms/eagle3/providers.py:169-213](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/providers.py#L169-L213) —— `algorithm_providers()`：注意 `OfflineDataProvider` 里 `build_reader`/`build_normalizer`/`build_collator` 都指向 `eagle3/data.py` 的工厂，`ModelProvider` 里 `minimum_loss_tokens=one_loss_token`、`needs_input_tools=online_needs_input_tools`、`default_dataloader_num_workers=4`。这是一份「端口如何被填值」的完整范本。

#### 4.4.4 代码实践

**实践目标：** 体会 normalize 与 collate 两步的职责差异，并验证 DFlash 家族 collator 对 batch 的形状处理。

**操作步骤（源码阅读型 + 可选运行）：**

1. 阅读 `normalize_offline_sample`（EAGLE3 与 DFlash 家族各一份），记录它们各自输出记录的**键名集合**。
2. （可选）在 specforge 环境里，构造两条假记录，调用 DFlash 的 `build_collator()` 拼成 batch，观察 padding 后序列长度是否取了 batch 内最大值：

   ```python
   # 示例代码
   import torch
   from specforge.algorithms.common.dflash_family_data import build_collator

   collate = build_collator()
   feats = [
       {"input_ids": torch.tensor([1, 2]),    "loss_mask": torch.tensor([1, 1]),
        "hidden_states": torch.randn(1, 2, 8)},
       {"input_ids": torch.tensor([3, 4, 5]), "loss_mask": torch.tensor([1, 1, 1]),
        "hidden_states": torch.randn(1, 3, 8)},
   ]
   batch = collate(feats)
   print({k: tuple(v.shape) for k, v in batch.items()})
   ```

**需要观察的现象 / 预期结果：**

- EAGLE3 normalizer 输出键：`attention_mask, loss_mask, target, hidden_state, input_ids`。
- DFlash 家族 normalizer 输出键：`input_ids, loss_mask, hidden_states`（DSpark 多一个 `target_last_hidden_states`）。
- collate 后，`input_ids`/`loss_mask`/`hidden_states` 的序列维应被 pad 到 batch 内最大长度（这里是 3），batch 维为 2。

> 说明：collate 的具体 padding 行为以 `pad_and_concatenate_features` 实现为准；上述形状预期为按其语义推断，待本地验证。

#### 4.4.5 小练习与答案

**练习 1：** EAGLE3 的 normalizer 把 `hidden_state`（末层）重命名成了 `target`。为什么需要这一步？

**答案：** 因为 EAGLE3 训练用的是 LogSoftmax 类损失，需要把目标模型最后一层经 lm_head 投影后的分布当作监督目标（`target`）；而草稿模型的输入特征也叫 `hidden_state`（来自辅助层）。两者不能重名，所以 normalizer 在这里做一次有意义的重命名，把「末层隐藏态」翻译成训练语义上的「目标分布来源」。

**练习 2：** 为什么 DFlash 的 `minimum_loss_tokens` 是 `2 * block_size` 而不是常数 1？

**答案：** DFlash 以 block 为单位建模（默认 `block_size=16`），一个有意义的最小监督单元至少要跨越两个 block 才能让模型学到 block 间关系；少于这个量样本没有训练价值。所以这个阈值与算法结构强绑定，不能像 EAGLE3 那样用通用的 `one_loss_token`。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个贯穿性任务：**用源码证据，完整对比 EAGLE3 与 DFlash 两个算法在 provider 层面的差异，并把这些差异追到离线特征文件的 schema 上。**

请填写并解释下表（所有结论都要能指到具体源码行）：

| 对比维度 | EAGLE3 | DFlash |
| --- | --- | --- |
| 草稿架构名 | `LlamaForCausalLMEagle3` | `DFlashDraftModel` |
| 捕获层数量与来源 | ?（提示：4.2.2 / `resolve_eagle_capture_layers`） | ?（提示：`populate_dflash_generated_config` + `apply_dflash_overrides`） |
| 离线 layout 的 `aux_feature` / `last_hidden_feature` | ? | ? |
| 离线文件张量名集合 | ? | ? |
| normalizer 是否做 target 投影 | ? | ? |
| `minimum_loss_tokens` | ? | ? |
| 是否支持 compact teacher / vocab mapping | ?（看 `AlgorithmCapabilities`） | ? |

**操作步骤：**

1. 从 `eagle3/providers.py` 与 `dflash/providers.py` 的 `algorithm_spec()` 与 `algorithm_providers()` 抄出上表每一格的答案。
2. 用 4.3.4 的可运行脚本，**实际物化**两者的离线记录，验证你填的「离线文件张量名集合」是否与 `materialize` 输出一致。
3. 用一句话总结：**EAGLE3 与 DFlash 的 provider 差异，最终体现在离线特征文件 schema 上的两处关键不同是什么？**

**预期结论（供自检）：**

- 捕获层：EAGLE3 固定 3 层（公式 `[1, L//2-1, L-4]`，用户只能改取值不能改数量）；DFlash 默认 1 层、数量可由 `model.draft_num_hidden_layers` 配置，多层在宽度上拼接。
- 离线 schema：EAGLE3 持久化 `{input_ids, loss_mask, aux_hidden_state, hidden_state}`（4 张量，末层单列）；DFlash 持久化 `{input_ids, loss_mask, hidden_states}`（3 张量，无末层，辅助状态合并命名）。这两处不同，正是「同一份通用捕获来源，经不同 layout + normalizer，服务了两种截然不同的草稿建模思路」。

---

## 6. 本讲小结

- provider 是 `AlgorithmRegistration` 的「可执行半边」，由 `AlgorithmProviders` 容器按 `step / model / offline / server_streaming / vocab_mapping_modes` 五组端口组织；它只描述算法行为，**不描述部署拓扑**。
- `make_registration` 在注册期就做契约-provider 的 **parity 校验**：特征 `(mode, modality)` 键集合必须完全相等、capture layout 物化名必须正好等于 `storage.required_tensors`，任何漂移都 fail-fast。
- **捕获层解析**有两条不同路线：EAGLE3 三级回退到固定公式（强制 3 层，多层隐藏状态拼接）；DFlash 家族读 `dflash_config.target_layer_ids`（层数可配、默认 1，由 `populate` / `apply_overrides` 生成）。
- **capture layout** 把通用四类来源（`input_ids`/`loss_mask`/`aux_hidden_states`/`last_hidden_states`）翻译成各算法自己的离线 schema 名；`materialize()` 是这条翻译的执行点，测试 `test_offline_capture_layout.py` 是最佳对照表。
- **normalize / collate / defaults** 完成从原始记录到训练 batch 的最后加工；EAGLE3 在 normalize 里做 target 重命名，DFlash 家族共享 `dflash_family_data` 与 `defaults.py`，并以 `2 * block_size` 作为最小监督长度。

---

## 7. 下一步学习建议

- **横向读完所有算法的 providers**：把 `peagle/providers.py`、`domino/providers.py`、`dspark/providers.py` 与本讲的 EAGLE3/DFlash 对照阅读，验证你能否在 1 分钟内说出每个算法的捕获层策略与离线 schema。这是掌握 provider 体系最快的练法。
- **进入训练装配**：本讲的 `ModelProvider.build_draft` / `build_training_model` 钩子，实际由 `specforge/training/assembly.py` 的 `build_training_run` 调用。下一篇 **u6-l1 训练装配 assembly** 会把这些端口与优化器、数据加载、profiling 拼到一起，是 provider 端口被完整消费的现场。
- **草稿架构的另一条轴**：若你想理解「为什么 `build_draft` 里调的是 `AutoDraftModel.from_config`」，接着读 **u4-l4 草稿模型注册表 modeling draft registry**——那里讲 `@register_draft` 与 `AutoDraftModel`，与本讲的算法注册表形成两条独立轴线。
- **离线特征的产出端**：想知道通用四类来源是谁产出的，读 **u5-l3 离线特征生成 prepare_hidden_states** 与 `scripts/prepare_hidden_states.py`，那里会调用本讲的 `resolve_capture_layers` 与 `capture_layout`。
