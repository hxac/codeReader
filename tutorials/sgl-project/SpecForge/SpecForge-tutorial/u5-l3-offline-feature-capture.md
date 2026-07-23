# 离线特征生成 prepare_hidden_states

## 1. 本讲目标

本讲解决一个具体问题：**离线训练时，草稿模型要「吃」的目标模型隐藏状态从哪里来、长什么样、怎么提前算好存盘**。

读完本讲，你应当能够：

- 说清 `scripts/prepare_hidden_states.py` 的参数分组、执行主流程，以及它为何只在「离线准备」阶段加载一次目标模型。
- 看懂 `OfflineCaptureLayout` 如何把「四类通用捕获源」翻译成各算法自己持有的离线特征 schema，并能背出 EAGLE3 / DFlash / Domino / DSpark 四种策略各自 `.ckpt` 记录里包含哪些 tensor。
- 理解 `specforge/offline_capture/` 包是一套「局部 SGLang 内核」，专门用来在本进程里冻结目标模型、抽取隐藏状态，并知道它为什么不参与在线训练。

## 2. 前置知识

本讲承接 **u5-l2（模板与预处理）** 与 **u4-l1（算法契约 contracts）**，你需要先记住下面几个已经建立的概念：

- **在线 vs 离线数据模式**（u2-l1 / u2-l2）：`data.hidden_states_path` 填了就是离线，训练时直接读预计算特征；不填就是在线，训练时实时捕获。本讲讲的就是「离线特征是怎么被预计算出来的」。
- **loss mask**（u5-l2）：与 `input_ids` 等长的 0/1 张量，标记 assistant 区间。它会被原样写进离线特征，供后续训练计算损失。
- **特征式草拟 / 隐藏状态 / 捕获层**（u1-l4）：草稿模型吃目标模型的隐藏状态当输入。不同算法从目标模型不同深度抽层，这就是「捕获层」。
- **算法契约与 provider**（u4-l1 / u4-l3）：算法有两半——纯契约 `AlgorithmSpec` 和可执行 `providers`；其中 `OfflineDataProvider.capture_layout` 就是本讲的主角之一。
- **控制面只传元数据、数据面才传张量**（u1-l5 / u5-l4）：离线特征文件是「数据面」的物理存储，存的就是张量。

一个关键直觉先建立起来：**离线训练的瓶颈不在算草稿，而在「养」一个目标模型**。目标模型通常和草稿模型一样大甚至更大，如果每次训练 step 都要把目标模型请进显存算一遍隐藏状态，既慢又费显存。`prepare_hidden_states.py` 的全部意义，就是把目标模型的隐藏状态**提前算好、落盘成一个个 `.ckpt` 文件**，之后离线训练就再也不用加载目标模型了。这就是脚本顶部 docstring 的一句话：

> Precomputing target features removes the target model's memory and latency cost from the later offline training run.

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [scripts/prepare_hidden_states.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/prepare_hidden_states.py) | 唯一的离线特征生成入口脚本；解析参数、装配目标模型、跑捕获循环、落盘 `.ckpt`。 |
| [specforge/offline_capture/sglang_backend/capture.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/offline_capture/sglang_backend/capture.py) | 局部 SGLang 捕获后端：在本进程内冻结目标模型，单次 prefill 抽出辅助层与最终层隐藏状态。 |
| [specforge/offline_capture/sglang.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/offline_capture/sglang.py) | `OfflineSGLangCapture` 适配层，把后端返回的「逐样本切片」拼回 batched 张量。 |
| [specforge/algorithms/common/providers.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/common/providers.py) | 定义 `OfflineCaptureLayout`，把通用捕获源映射成算法自己的离线 schema。 |
| [specforge/application/composition.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/application/composition.py) | `resolve_offline_capture`：复用组合根，解析出捕获层与 layout。 |
| [docs/basic_usage/data_preparation.md](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/basic_usage/data_preparation.md) | 官方数据准备文档，含各策略的真实命令样例与 schema 表。 |
| [specforge/runtime/data_plane/offline_reader.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/offline_reader.py) | 训练侧的离线读取器：读 `.ckpt`、按 schema 校验、产出 `SampleRef`。 |

> 说明：本讲引用的行号基于当前 HEAD `a4fca140`。`offline_capture/` 是「依赖懒加载」包，`__init__.py` 用 `__getattr__` 延迟导入 sglang，使无 GPU 场景也能 import。

## 4. 核心概念与源码讲解

### 4.1 prepare_hidden_states 参数与主流程

#### 4.1.1 概念说明

`scripts/prepare_hidden_states.py` 是一个**独立的预处理脚本**，和 `specforge train` 是两条互不干扰的路径：

- 它**只负责捕获并落盘**目标特征，**不训练**草稿模型。
- 它**临时**加载一份目标模型（仅本脚本生命周期内存在），跑完即销毁。
- 它通过 `--strategy` 选择算法，由算法自己决定「抽哪些层」「存成什么 schema」。

为什么需要这样一个独立脚本？因为离线训练要把「目标模型推理」这一步从训练循环里彻底摘掉。提前算好特征后，训练时只需要读文件，目标模型再也不会出现在 trainer 进程里。这也意味着：**离线特征的 schema 必须和训练时算法期望的 schema 严格一致**，否则训练读取器会直接报错（见 4.2）。

#### 4.1.2 核心流程

`main()` 的执行主轴可以拆成 7 步：

```
1. parse_args()                      解析命令行（model/data/inference/others/sglang 五组）
2. resolve_offline_capture_plan()    复用组合根，解析出 capture_layers + layout（OfflineCapturePlan）
3. _resolve_draft_vocab_size()       从 draft config JSON 读出 draft_vocab_size
4. init_distributed(tp_size=...)     建立 TP + DP 进程组
5. build_target_model()              加载局部 SGLang 目标，并 set_capture_layers(...)
6. build_eagle3_dataset() + 词表映射  把原始对话预处理成 (input_ids, loss_mask)，rank0 生成 vocab_mapping.pt
7. HiddenStatesGenerator.generate()  DP 分片 → 逐 batch 捕获 → layout.materialize → 异步落盘 .ckpt
```

其中第 2 步是「算法拥有」的解析：脚本把命令行参数伪装成一个最小的 `Config`（只填 `model`/`data`/`training.strategy`），交给组合根的 `resolve_offline_capture` 去查算法注册表，从而拿到该算法的捕获层与 schema。这套机制保证「预处理脚本」与「训练运行」用的是**同一套算法注册体系**，不会各说各话。

#### 4.1.3 源码精读

**参数分组**——`parse_args` 把参数分成五组，最关键的是 model 与 data 两组：

- `--target-model-path`（必填）：目标模型路径，即「老师」。
- `--strategy`（默认 `eagle3`）：选择算法，决定捕获层与 schema。见 [scripts/prepare_hidden_states.py:103-108](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/prepare_hidden_states.py#L103-L108)。
- `--draft-model-config`：草稿配置 JSON，用来解析捕获层；对于没有「目标结构兜底默认值」的策略（如 Domino / DSpark）是**必填**。见 [scripts/prepare_hidden_states.py:109-117](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/prepare_hidden_states.py#L109-L117)。
- `--data-path`（必填）、`--max-length`、`--chat-template`、`--is-preformatted`：数据侧，与 u5-l1/u5-l2 一致。
- inference 组的 `--tp-size` / `--batch-size`：张量并行度与捕获批大小。
- others 组的 `--output-path`、`--compress`、`--file-group-size`、`--num-io-threads`：控制落盘方式（是否 gzip 压缩、每子目录多少文件、异步 I/O 线程数）。

**解析捕获计划**——`resolve_offline_capture_plan` 构造最小 Config 后调用组合根，把结果收进一个不可变值对象 `OfflineCapturePlan`：

```python
# scripts/prepare_hidden_states.py:316-344
cfg = Config(model=model, data={...}, training={"strategy": strategy})
resolved = resolve_offline_capture(cfg, target_config=target_config)
return OfflineCapturePlan(
    strategy=resolved.run.algorithm.name,
    draft_config=resolved.draft_config,
    capture_method=resolved.capture_method,
    capture_layers=resolved.capture_layers,
    layout=resolved.layout,
)
```

这里的 `capture_method`（只能是 `"eagle3"` 或 `"dflash"`）和 `capture_layers`（层号元组）会传给目标模型，`layout` 会传给生成器决定 schema。

**词表映射**——离线训练用「缩减草稿词表」，需要一张 `d2t`/`t2d` 映射表。脚本只在全局 rank0 生成它，再把结果对象广播给所有 rank，任一 rank 失败都会让全体失败（fail-together）。见 [scripts/prepare_hidden_states.py:228-297](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/prepare_hidden_states.py#L228-L297)，产物落在 `${output_path}/vocab_mapping/vocab_mapping.pt`。

**主流程装配**——`main` 里依次完成解析、建分布式、建模型、建数据集、生成。关键三行：

```python
# scripts/prepare_hidden_states.py:775-796
capture_plan = resolve_offline_capture_plan(args, target_model_config)   # 算法解析
init_distributed(timeout=args.dist_timeout, tp_size=args.tp_size)        # TP+DP
target_model = build_target_model(                                       # 局部目标
    args, target_model_config,
    capture_layers=list(capture_plan.capture_layers),
    capture_method=capture_plan.capture_method,
)
```

#### 4.1.4 代码实践

**实践目标**：不运行脚本，靠读源码搞清「每个参数组分别控制什么」，建立参数到行为的映射。

**操作步骤**：

1. 打开 [scripts/prepare_hidden_states.py:97-195](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/prepare_hidden_states.py#L97-L195)，找到五个 `add_argument_group`。
2. 对每个参数，判断它影响的是：**(a) 算法捕获层**、**(b) 数据预处理**、**(c) 分布式与批大小**、还是 **(d) 落盘方式**。

**需要观察的现象 / 预期结果**（待本地验证）：你应该能填出类似下表——

| 参数 | 归类 |
| --- | --- |
| `--strategy` / `--draft-model-config` | (a) 算法捕获层 |
| `--data-path` / `--chat-template` / `--is-preformatted` | (b) 数据预处理 |
| `--tp-size` / `--batch-size` | (c) 分布式与批大小 |
| `--output-path` / `--compress` / `--file-group-size` | (d) 落盘方式 |

3. 进一步追问：为什么 `--draft-model-config` 对 EAGLE3 是「建议传」而对 Domino/DSpark 是「必须传」？（提示：EAGLE3 有目标结构兜底公式，DFlash 家族没有，见 4.2.3。）

#### 4.1.5 小练习与答案

**练习 1**：脚本顶部 docstring 说「Online training consumes features from an external server and never loads a target model in the trainer process」。结合本讲，离线训练时的目标模型在哪个进程、哪个阶段出现？

**参考答案**：只在 `prepare_hidden_states.py` 这个**预处理脚本进程**里出现，且仅在捕获循环执行期间；真正训练时（`specforge train` 离线模式）trainer 进程**完全不加载目标模型**，只读 `.ckpt` 文件。

**练习 2**：`--num-io-threads` 默认是多少？为什么默认值需要兜底处理？

**参考答案**：默认 `None`，在 `main` 里被兜底成 `max(1, os.cpu_count())`（见 [scripts/prepare_hidden_states.py:762-764](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/prepare_hidden_states.py#L762-L764)）。因为 `os.cpu_count()` 在某些容器/受限环境可能返回 `None`，直接相乘会出错，所以取 `or 1` 再 `max` 兜底。

---

### 4.2 各策略离线特征 schema 表（OfflineCaptureLayout）

#### 4.2.1 概念说明

不同算法需要的「目标特征」形状不同：EAGLE3 要多层辅助隐藏状态拼起来；DFlash 要选定层拼接；DSpark 还额外要目标模型最终层。如果让预处理脚本去「记住」每种算法存什么字段，脚本就会和算法耦合死，且容易和训练侧的读取器对不上。

SpecForge 的解法是 **`OfflineCaptureLayout`**（定义于 [specforge/algorithms/common/providers.py:389-403](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/common/providers.py#L389-L403)）：**捕获过程永远只产出四类通用源**，再由每个算法持有的 layout 把这四类源**翻译成它自己的持久化字段名**。

四类通用源是固定的：

| 通用源 | 含义 |
| --- | --- |
| `input_ids` | 输入 token id（透传） |
| `loss_mask` | 损失掩码（透传） |
| `aux_hidden_states` | 「辅助层」隐藏状态——由捕获层抽出的中间层，按算法拼接 |
| `last_hidden_states` | 目标模型**最终层**隐藏状态 |

算法的 layout 只描述三件事：

- `passthrough`：哪些通用源原样透传、改叫什么名（如 `input_ids`→`input_ids`）。
- `aux_feature`：`aux_hidden_states` 要不要存、存成什么名（如 EAGLE3 叫 `aux_hidden_state`，DFlash 家族叫 `hidden_states`，也可以是 `None` 不存）。
- `last_hidden_feature`：`last_hidden_states` 要不要存、存成什么名（EAGLE3 叫 `hidden_state`，DFlash/Domino 是 `None` 不存，DSpark 叫 `target_last_hidden_states`）。
- `capture_method`：捕获方法，只能是 `"eagle3"` 或 `"dflash"`，决定调用目标模型的哪个捕获钩子。

这样，**预处理脚本对所有算法都是同一套捕获代码**，差异只体现在 layout 这张「翻译表」上。

#### 4.2.2 核心流程

落盘一条记录的流程是：

```
目标模型 capture()  →  OfflineCaptureBatch{hidden_states(=aux), last_hidden_states(=last), ...}
                          │
                          ▼ 逐样本切片
layout.materialize({       把四类通用源翻译成算法字段名
   input_ids, loss_mask,
   aux_hidden_states,
   last_hidden_states,
})  →  record = { 算法自己的字段名: tensor }
                          │
                          ▼
_save_tensor_async(record, data_{idx}.ckpt)   异步落盘
```

`materialize` 的职责有二：**改名**（按 layout 把通用源映射成算法字段名）和**早失败**（某条源缺失或为 `None` 就立刻 `KeyError`/`ValueError`，绝不让残缺记录落盘）。

还有一道「对账」校验在注册期完成：`make_registration` 会断言 **layout 产出的字段名集合 == 契约里 `storage.required_tensors`**，任何漂移（多了或少了字段）直接 fail-fast。这保证「预处理写的 schema」与「训练读的 schema」是同一张表的两面。

#### 4.2.3 源码精读

**`materialize` 的改名逻辑**——先收集 passthrough，再按需追加 aux / last：

```python
# specforge/algorithms/common/providers.py:440-463
def materialize(self, sources):
    mappings = list(self.passthrough)
    if self.aux_feature is not None:
        mappings.append((self.aux_feature, "aux_hidden_states"))
    if self.last_hidden_feature is not None:
        mappings.append((self.last_hidden_feature, "last_hidden_states"))
    record = {}
    for feature_name, source_key in mappings:
        if source_key not in sources:
            raise KeyError(...)        # 缺源 → 早失败
        value = sources[source_key]
        if value is None:
            raise ValueError(...)      # 源为 None → 早失败
        record[feature_name] = value
    return record
```

脚本里就是这么调用的（[scripts/prepare_hidden_states.py:717-724](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/prepare_hidden_states.py#L717-L724)）：把 `aux_hidden_states` / `last_hidden_states` 两类源喂给 `capture_layout.materialize(...)`，拿回 record 再落盘。

**四种策略的 layout 与 schema 表**——直接读各算法 providers 的 `capture_layout` 字段：

| 策略 | capture_method | aux_feature | last_hidden_feature | `.ckpt` 记录字段 |
| --- | --- | --- | --- | --- |
| EAGLE3 | `eagle3` | `aux_hidden_state` | `hidden_state` | `input_ids`, `loss_mask`, `aux_hidden_state`, `hidden_state` |
| DFlash | `dflash` | `hidden_states` | `None` | `input_ids`, `loss_mask`, `hidden_states` |
| Domino | `dflash` | `hidden_states` | `None` | `input_ids`, `loss_mask`, `hidden_states` |
| DSpark | `dflash` | `hidden_states` | `target_last_hidden_states` | `input_ids`, `loss_mask`, `hidden_states`, `target_last_hidden_states` |

对应源码：

- EAGLE3 layout：[specforge/algorithms/eagle3/providers.py:200-208](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/providers.py#L200-L208)，其契约 `storage.required_tensors` 为 `{input_ids, loss_mask, hidden_state, aux_hidden_state}`（[eagle3/providers.py:143-148](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/providers.py#L143-L148)）。
- DFlash layout：[specforge/algorithms/dflash/providers.py:198-206](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/dflash/providers.py#L198-L206)。
- Domino layout：[specforge/algorithms/domino/providers.py:177-185](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/domino/providers.py#L177-L185)。
- DSpark layout：[specforge/algorithms/dspark/providers.py:171-179](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/dspark/providers.py#L171-L179)。

这张表和官方文档 [docs/basic_usage/data_preparation.md](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/basic_usage/data_preparation.md) 的「Tensors in each record」表完全一致，且不是手写同步的——而是由 `make_registration` 的对账校验强制保证（[providers.py:696-704](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/common/providers.py#L696-L704)）：`emitted = layout.output_names` 必须**正好等于** `contract.storage.required_tensors`。

**捕获层从哪来**——各算法的 `resolve_capture_layers` 决定抽目标模型哪几层：

- DFlash 家族（含 Domino / DSpark）读草稿配置里的 `dflash_config.target_layer_ids`，没定义就直接报错（[model_providers.py:214-225](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/model_providers.py#L214-L225)）。这正是「Domino / DSpark 必须传 `--draft-model-config`」的原因。
- EAGLE3 走「运行覆盖 → 草稿配置 → 目标结构兜底公式」三级回退，对未定义 `eagle_config.eagle_aux_hidden_state_layer_ids` 的旧配置用目标层数 \(L\) 推出默认捕获层（约为 `[1, L//2-1, L-4]`，强制 3 层，详见 u4-l3）。

**训练侧的校验**——离线读取器 `OfflineManifestReader` 默认按 EAGLE3 的四键 `_OFFLINE_EAGLE3_KEYS = ("input_ids", "loss_mask", "hidden_state", "aux_hidden_state")` 校验（[offline_reader.py:33](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/offline_reader.py#L33)），缺键就抛 `KeyError`（[offline_reader.py:40-42](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/offline_reader.py#L40-L42)）。也就是说，**用 DFlash 特征去喂 EAGLE3 读取器不会被静默适配，而是直接报错**。

#### 4.2.4 代码实践（本讲主任务）

**实践目标**：为 DFlash 写一条完整的 `prepare_hidden_states.py` 命令，并预测其输出 `.ckpt` 里包含哪些 tensor。

**操作步骤**：仓库根目录下，参照 [docs/basic_usage/data_preparation.md](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/basic_usage/data_preparation.md) 里 checked-in 的 Qwen3-8B DFlash 配方，执行（**待本地验证**——需先备好数据与显存）：

```bash
torchrun --nproc_per_node=8 \
    scripts/prepare_hidden_states.py \
    --strategy dflash \
    --target-model-path Qwen/Qwen3-8B \
    --draft-model-config configs/qwen3-8b-dflash.json \
    --data-path ./cache/dataset/sharegpt_train.jsonl \
    --output-path ./cache/hidden_states/qwen3-8b-dflash-sharegpt \
    --chat-template qwen \
    --max-length 3072 \
    --tp-size 1 \
    --batch-size 32
```

**需要观察的现象 / 预期结果**：

1. 启动日志会打印解析出的捕获方法与层号，形如 `Resolved dflash offline capture method 'dflash', layers: [...]`（层号来自 `configs/qwen3-8b-dflash.json` 的 `dflash_config.target_layer_ids`）。
2. 输出目录结构为 `rows_{0-2000}/data_{idx}.ckpt` 分组（受 `--file-group-size` 控制）。
3. **每条 `data_{idx}.ckpt` 记录里会包含三个 tensor**（对应 DFlash 的 layout：`aux_feature=hidden_states`、`last_hidden_feature=None`）：

   | 键 | 来源（通用源） | 说明 |
   | --- | --- | --- |
   | `input_ids` | `input_ids`（透传） | 输入 token id |
   | `loss_mask` | `loss_mask`（透传） | assistant 区间掩码 |
   | `hidden_states` | `aux_hidden_states` | 由 `dflash_config.target_layer_ids` 选定的目标层**拼接**而成的辅助隐藏状态 |

   注意：DFlash **不会**存 `target_last_hidden_states`（那是 DSpark 才有的），也**不会**像 EAGLE3 那样把 aux 与 last 分开存成 `aux_hidden_state` + `hidden_state` 两个字段——它把辅助层统称为 `hidden_states`，且不存最终层。
4. 同目录下还会生成 `vocab_mapping/vocab_mapping.pt`（含 `d2t` / `t2d` 两个一维张量）。

**预期结果**：若把任意一个 `.ckpt` 用 `torch.load(..., weights_only=True)` 读出，其 `keys()` 应恰为 `{'input_ids', 'loss_mask', 'hidden_states'}`。如果出现 `hidden_state`（单数）或 `aux_hidden_state`，说明 schema 对不上，训练读取器会报 `KeyError`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 EAGLE3 的记录有 `hidden_state` 和 `aux_hidden_state` **两个**字段，而 DFlash 只有一个 `hidden_states`？

**参考答案**：因为 EAGLE3 的 layout 把「辅助层」与「最终层」分别映射成两个字段（`aux_feature=aux_hidden_state`、`last_hidden_feature=hidden_state`），训练时草稿模型要用最终层做目标投影；DFlash 家族的 layout 设 `last_hidden_feature=None`（不需要目标最终层），只把辅助层拼成一个 `hidden_states`。

**练习 2**：如果误把 DSpark 的特征目录喂给 DFlash 的训练读取器，会发生什么？

**参考答案**：DFlash 期望 `{input_ids, loss_mask, hidden_states}`，而 DSpark 文件里多了 `target_last_hidden_states`。读取器按算法选定的 `feature_keys` 校验：缺键会抛 `KeyError`；多出的键一般被忽略，但因为 schema 不匹配（层选取也不同），训练结果不可信。所以官方文档强调「每种策略单独放一个输出目录」。

**练习 3**：`materialize` 为什么对「源为 `None`」也要报错，而不直接跳过该字段？

**参考答案**：因为 layout 里声明了某个 `aux_feature`/`last_hidden_feature` 就意味着该算法**训练时一定需要**这个张量。若源为 `None` 说明捕获没成功却还想落盘，会让训练时才发现缺数据；早失败能把问题挡在预处理阶段，避免产出「看似成功实则残缺」的特征文件。

---

### 4.3 offline_capture 局部 SGLang 内核

#### 4.3.1 概念说明

`specforge/offline_capture/` 是一个**局部 SGLang 内核**：它把 SGLang 的推理引擎（模型加载、调度、KV cache、前向）当成一个「只用来抽隐藏状态」的工具，钉死版本、限制在本脚本进程内使用。

它的边界非常清晰（见包 docstring [offline_capture/__init__.py:1-6](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/offline_capture/__init__.py#L1-L6)）：

> Online training never imports this package: it captures through an external SGLang server and the server-capture adapter. The local SGLang integration here exists only for the standalone hidden-state preparation script.

也就是说，**在线训练走的是另一条路**（外部 SGLang 服务 + `inference/` 适配器，见 u7-l5），完全不碰这个包。本包只服务于离线预处理这一件事。

为什么用 SGLang 而不是直接 transformers 前向？因为 SGLang 引擎在 prefill 路径上做了高度优化（PagedAttention、连续批处理、注意力后端等），用它来批量算目标模型的隐藏状态更快、更省显存，且和线上推理用的是同一套权重行为。

#### 4.3.2 核心流程

一次「局部捕获」的流程：

```
build_target_model()
  └─ load_offline_capture() → OfflineSGLangCaptureBackend.build(...)
        ├─ ServerArgs(enable_return_hidden_states=True, disable_cuda_graph=True, chunked_prefill_size=-1, tp_size=...)
        ├─ SGLangRunner(...)：加载权重、建 KV pool、建注意力后端
        └─ wrap_offline_eagle3_logits_processors(model)   打离线捕获补丁
  └─ set_capture_layers(layers, capture_method=...)        告诉模型抽哪几层
        └─ 映射到 set_eagle3_layers_to_capture / set_dflash_layers_to_capture

generate() 循环里每个 batch：
  └─ model.capture(input_ids, attention_mask, loss_mask)
        └─ backend.capture_eagle3(...)
              ├─ 把 batch 拆成逐条 Req
              ├─ _forward_extend(reqs)：CaptureHiddenMode.FULL 单次 prefill
              │     → output.aux_hidden_states（捕获层）+ output.last_hidden_states（最终层）
              └─ 按各自 input_lens 切回逐样本
```

两个关键设计：

- **只做 prefill，不生成新 token**：`capture_eagle3` 里 `SamplingParams(max_new_tokens=1)` 只是占位，真正要的是 `CaptureHiddenMode.FULL` 下一次前向吐出的隐藏状态，而不是 logits 采样结果。
- **`disable_cuda_graph=True` + `chunked_prefill_size=-1`**：关掉 CUDA Graph 与分块 prefill，保证隐藏状态捕获的确定性与完整性（不分块、不重算）。

#### 4.3.3 源码精读

**捕获方法只认两种**——`set_capture_layers` 把 `capture_method` 字符串映射到目标模型上的钩子方法，只接受 `eagle3` / `dflash`：

```python
# specforge/offline_capture/sglang_backend/capture.py:89-111
setter_name = {
    "eagle3": "set_eagle3_layers_to_capture",
    "dflash": "set_dflash_layers_to_capture",
}.get(capture_method)
if setter_name is None:
    raise ValueError("offline SGLang capture method must be 'eagle3' or 'dflash', ...")
setter = getattr(self.model_runner.model, setter_name, None)
if not callable(setter):
    raise RuntimeError(f"target model does not expose SGLang capture hook {setter_name!r}")
setter(layer_ids)
```

这解释了为什么 Domino / DSpark / DFlash 的 `capture_method` 都是 `"dflash"`——它们共用 DFlash 的目标侧捕获钩子，差异只在草稿侧与 schema。

**单次前向抽出双份隐藏状态**——`_forward_extend` 把 batch 标记成 `CaptureHiddenMode.FULL` 后跑一次 SGLang 前向：

```python
# specforge/offline_capture/sglang_backend/capture.py:154-158
batch.capture_hidden_mode = CaptureHiddenMode.FULL
forward_batch = ForwardBatch.init_new(batch, self.model_runner)
forward_batch.capture_hidden_mode = CaptureHiddenMode.FULL
output = self.model_runner.forward(forward_batch)
```

随后 `capture_eagle3` 从输出里取 `aux_hidden_states`（捕获的辅助层）与 `last_hidden_states`（最终层），二者缺一就直接报错（[capture.py:200-206](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/offline_capture/sglang_backend/capture.py#L200-L206)）。注意名字：后端返回的「aux」就是 layout 里的 `aux_hidden_states` 源，「last」就是 `last_hidden_states` 源。

**适配层拼回 batched 张量**——`OfflineSGLangCapture.capture` 把后端返回的「逐样本切片」沿 batch 维拼回去，供脚本统一处理（[offline_capture/sglang.py:62-84](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/offline_capture/sglang.py#L62-L84)）。脚本拿到后再按样本逐条切片喂给 `materialize`。

**NaN 防护与幂等续跑**——落盘前 `_save_tensor_sync` 会扫描浮点张量，发现 NaN 就跳过该记录并告警（[prepare_hidden_states.py:463-470](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/prepare_hidden_states.py#L463-L470)）；而 `generate` 在每个 batch 开始时让 tp_rank_0 检查目标文件是否已存在、再广播给 TP 组（[prepare_hidden_states.py:632-643](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/prepare_hidden_states.py#L632-L643)），已存在的样本直接跳过——这让脚本**可重入、可断点续跑**。

#### 4.3.4 代码实践

**实践目标**：跟踪一次 `model.capture(...)` 调用链，搞清「模型执行」「隐藏状态产出」「元数据/张量返回」分别发生在哪一层。

**操作步骤**：

1. 从 [scripts/prepare_hidden_states.py:680-684](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/scripts/prepare_hidden_states.py#L680-L684) 的 `self.model.capture(...)` 进入。
2. 跳到 [offline_capture/sglang.py:62-84](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/offline_capture/sglang.py#L62-L84) 的 `OfflineSGLangCapture.capture`，它调用 `self._backend.capture(...)`。
3. 再跳到 [capture.py:214-227](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/offline_capture/sglang_backend/capture.py#L214-L227) 的 `OfflineSGLangCaptureBackend.capture` → `capture_eagle3`（[capture.py:164-212](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/offline_capture/sglang_backend/capture.py#L164-L212)）。

**需要观察的现象 / 预期结果**：

- 「模型执行」发生在 `_forward_extend` 里的 `self.model_runner.forward(forward_batch)`。
- 「隐藏状态产出」由 `CaptureHiddenMode.FULL` 触发，SGLang 在前向时把捕获层与最终层挂到 `output.aux_hidden_states` / `output.last_hidden_states`。
- 这一切都在**同一个 trainer 预处理进程**里完成；返回的就是张量本身（不是元数据引用）——因为这是离线落盘场景，不涉及跨进程传输。

**预期结果**：你能画出 `capture()` → `backend.capture` → `capture_eagle3` → `_forward_extend` → `forward` 这条调用链，并指出隐藏状态在 `output` 的哪个属性上返回。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ServerArgs` 里要显式设 `disable_cuda_graph=True` 和 `chunked_prefill_size=-1`？

**参考答案**：CUDA Graph 会把前向「录制」成固定图，隐藏状态捕获需要灵活取中间层输出，开 CUDA Graph 可能拿不到或拿错；分块 prefill（chunked）会把一条序列拆成多块分别前向再拼，破坏「一条序列一次前向产出完整隐藏状态」的语义。两者都关掉，保证捕获结果完整、确定。

**练习 2**：`OfflineSGLangCaptureBackend.capture` 实现成直接转调 `capture_eagle3`，名字带 eagle3 是否意味着只能服务 EAGLE3？

**参考答案**：不是。虽然方法名沿用历史（`capture_eagle3`），但它返回的是**通用的 `aux_hidden_states` + `last_hidden_states`** 两份源；真正区分算法的是 `set_capture_layers(capture_method=...)` 时挂上的钩子（`set_eagle3_layers_to_capture` vs `set_dflash_layers_to_capture`），以及下游 `OfflineCaptureLayout` 的翻译。所以 DFlash 家族复用同一个 `capture` 入口，只是捕获的层不同。

---

## 5. 综合实践

**任务**：选定一个策略（建议 DSpark），从头预测它的「捕获层 → 输出 schema → 文件布局 → 训练读取校验」整条链，再和源码/文档对照验证。

**步骤**：

1. 读 [specforge/algorithms/dspark/providers.py:171-179](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/dspark/providers.py#L171-L179)，写下 DSpark 的 `capture_method`、`aux_feature`、`last_hidden_feature`，推出 `.ckpt` 记录应包含的 4 个 tensor 名。
2. 对照 [docs/basic_usage/data_preparation.md](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/basic_usage/data_preparation.md) 的 schema 表确认一致。
3. 解释 DSpark 为何比 DFlash 多一个 `target_last_hidden_states`：它的 layout 设了 `last_hidden_feature="target_last_hidden_states"`，用于其 L1 与置信度目标（教师最终层投影）。
4. 写一条 DSpark 的准备命令（`--strategy dspark`，`--draft-model-config configs/qwen3-4b-dspark.json`，`--target-model-path Qwen/Qwen3-4B`），指出其捕获层来自草稿配置的哪个字段（`dflash_config.target_layer_ids`）。
5. 思考：若用 `--strategy dspark` 生成特征，却把 `data.hidden_states_path` 指向它的训练 YAML 写成 `strategy: dflash`，会在哪一步、以什么错误失败？

**预期结果**（待本地验证）：第 5 步应在训练读取阶段失败——DFlash 读取器期望 `{input_ids, loss_mask, hidden_states}`，而 DSpark 文件里键集合不同（多了 `target_last_hidden_states`），`OfflineManifestReader` 的 `_inspect_feature_file` 会因缺键/类型不符抛错（[offline_reader.py:40-49](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/offline_reader.py#L40-L49)）。这印证了「预处理与训练必须用同一策略」的铁律。

## 6. 本讲小结

- `prepare_hidden_states.py` 是**独立的预处理脚本**：临时加载目标模型、捕获隐藏状态、落盘 `.ckpt`，让离线训练从此不再需要目标模型；在线训练完全不碰它。
- 脚本把命令行参数伪装成最小 `Config`，**复用组合根** `resolve_offline_capture` 解析出 `capture_layers` 与 `OfflineCaptureLayout`，保证预处理与训练共用同一套算法注册体系。
- 捕获过程对所有算法**只产出四类通用源**（`input_ids` / `loss_mask` / `aux_hidden_states` / `last_hidden_states`），由各算法持有的 `OfflineCaptureLayout.materialize` 翻译成自己的字段名。
- 四种策略的离线 schema：EAGLE3 存 `input_ids/loss_mask/hidden_state/aux_hidden_state`；DFlash 与 Domino 存 `input_ids/loss_mask/hidden_states`；DSpark 存 `input_ids/loss_mask/hidden_states/target_last_hidden_states`。该表由 `make_registration` 的对账校验强制保证。
- `offline_capture/` 是**局部 SGLang 内核**，`capture_method` 只认 `eagle3`/`dflash` 两种钩子；它只服务离线预处理，在线捕获走外部 SGLang 服务 + `inference/` 适配器。
- 脚本具备 **NaN 跳过、异步 I/O 背压、文件存在即跳过** 三项工程化能力，因此可重入、可断点续跑；产物含 `.ckpt` 特征文件与 `vocab_mapping.pt`。

## 7. 下一步学习建议

- 想看「这些 `.ckpt` 在训练时如何被读成 `SampleRef` 并参与 batch」：进入 **u5-l4（跨平面契约与 SampleRef）** 与 **u7-l3（数据平面 feature store 与传输）**，重点读 `OfflineManifestReader` 与 `FeatureDataLoader`。
- 想理解「在线训练如何不靠这个脚本、而靠外部服务器实时捕获」：进入 **u7-l5（推理平面与 SGLang 捕获）**，对照本讲的局部内核，体会「离线落盘」与「在线流式」两条路的差异。
- 想自己加一个需要新特征 schema 的算法：回到 **u4-l3（算法 providers 与扩展端口）** 与 **u10-l2（新增一个训练算法）**，新增算法时只要同时声明 `OfflineStorageContract.required_tensors` 与等价的 `OfflineCaptureLayout`，对账校验会替你把关一致性。
