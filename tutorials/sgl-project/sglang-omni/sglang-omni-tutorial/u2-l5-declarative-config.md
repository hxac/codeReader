# 声明式配置 PipelineConfig 与 StageConfig

## 1. 本讲目标

本讲把视线从「配置怎么生成、查看、导出」（u1-l5）下沉到配置的**结构本身**。读完本讲，你应当能够：

- 读懂 `StageConfig` 的每一个关键字段，知道一个阶段「由谁构造、跑在哪、结果往哪走、是否参与 fan-in / streaming」。
- 区分**静态拓扑声明**与**请求感知路由**：理解 `next` / `wait_for` / `stream_to` 作为「静态全集」，而 `route_fn` / `wait_for_fn` / `stream_done_to_fn` 只在运行时按请求内容裁剪这个全集。
- 理解 `PipelineConfig` 如何把一组 `StageConfig` 组织成一条完整管线，以及 `entry_stage` / `terminal_stages_fn` / `fused_stages` 等管线级字段的作用。
- 掌握三个**派生字段**（`resolved_entry_stage` / `terminal_stages` / `gpu_placement`）——它们不是手填的，而是从 `stages` 里算出来的。
- 明白 `CommConfig` 只调通信缓冲池参数、**不**选传输后端。

承接 u1-l5 的结论：**配置是「模型定义（提供拓扑）」与「模型无关运行时」之间的契约**。本讲拆解的就是这份契约里每一行字段的含义。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：配置是「静态蓝图」，不是「运行时逻辑」。**
配置层被刻意做成静态的——它的职责是让拓扑、放置、阶段构造在运行时启动**之前**就完全可见。请求时刻的行为（采样、调度、流式吐 chunk）不属于配置层，而属于 stage / scheduler / model runner。所以你会看到配置里大量字段是「字符串点路径」（如 `factory`、`merge_fn`、`route_fn`），配置层只记录这些函数的**位置**，并不调用它们——真正调用发生在运行时 worker 导入工厂之后。

**直觉二：一条管线是一张「带特殊边」的有向图。**
普通 `next` 是「正常结果」边；`wait_for` + `merge_fn` 构成 fan-in（多入）汇合边；`stream_to` 是与正常结果**并行**的流式边（传 hidden state / codec code 等 chunk）。一条管线可以有多个「终态」（terminal），比如文本收口 `decode` 与语音收口 `code2wav` 并存，由上层 Coordinator 合并——这一点 u2-l4 已经讲过，本讲看它**在配置里如何声明**。

**直觉三：「静态全集 + 请求感知子集」是贯穿全篇的模式。**
很多字段成对出现：一个描述「所有可能」，一个在运行时描述「这一次实际用到哪些」。例如 `next` 列出所有可能的下游，`route_fn` 在每次请求时返回真正要发的那几个。记住这个二分法，本讲的字段表就不会乱。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [sglang_omni/config/schema.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py) | 配置 schema 的**唯一真相来源**。定义 `CommConfig` / `StageConfig` / `PipelineConfig` 及其派生属性与校验。 |
| [docs/developer_reference/config.md](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/config.md) | 官方字段参考表与设计说明，是 schema 的人类可读镜像。 |
| [sglang_omni/models/qwen3_omni/config.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py) | Qwen3-Omni 的真实 `PipelineConfig` 子类，给出每个 `StageConfig` 的实例化范本。 |
| [sglang_omni/models/qwen3_omni/request_builders.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/request_builders.py) | 被 `route_fn` / `wait_for_fn` / `terminal_stages_fn` / `project_payload` 引用的真实函数实现，用来观察「请求感知」如何落地。 |
| [examples/configs/qwen3_omni_colocated_h100_bf16.yaml](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/examples/configs/qwen3_omni_colocated_h100_bf16.yaml) | 一份「紧凑覆盖」YAML，用于实践任务中对照「拓扑在 config.py、运行时调参在 yaml」。 |

---

## 4. 核心概念与源码讲解

### 4.1 StageConfig：声明一个阶段

#### 4.1.1 概念说明

`StageConfig` 描述**一个逻辑阶段**：怎么构造它（factory）、跑在哪（gpu / process / tp_size）、正常结果往哪走（next / terminal）、是否参与 fan-in（wait_for / merge_fn）或流式（stream_to）、以及下发前是否要对 payload 做投影（project_payload）。

它是配置层的最小积木。理解它的关键是抓住两组对立：**「构造」vs「路由」**，以及**「静态全集」vs「请求感知子集」**。

#### 4.1.2 核心流程

一个 `StageConfig` 的信息可分成五组，运行时各取所需：

```text
┌─ 身份与构造 ───────────────────────────────────────┐
│  name, factory, factory_args                       │  → 子进程 import factory 建调度器
├─ 路由（正常结果）──────────────────────────────────┤
│  next (静态全集)  +  route_fn (请求感知子集)        │  → 决定结果发往哪个下游
│  terminal=True                                    │  → 结果直接发给 Coordinator
├─ GPU / 并行 / 进程 ────────────────────────────────┤
│  gpu, tp_size, parallelism, process               │  → 放置规划与进程拓扑
├─ fan-in（汇合）────────────────────────────────────┤
│  wait_for (静态全集) + wait_for_fn (请求感知子集)  │  → 收齐上游才能执行
│  merge_fn                                         │  → 把多路 payload 合成一路
├─ streaming（流式）─────────────────────────────────┤
│  stream_to (静态全集) + stream_done_to_fn (子集)   │  → 与正常结果并行的 chunk 边
└─ 下发投影 / 通信调参 ──────────────────────────────┘
   project_payload, comm, runtime, runtime_arg_map
```

两条贯穿全篇的硬规则（由校验保证，见 4.4）：

1. **路由互斥**：一个阶段必须且只能设置 `next` 或 `terminal=True` 之一。`route_fn` 不能用在 terminal 阶段，且只能作用于「已经声明了 `next`」的阶段——`next` 始终是静态拓扑声明，供校验用。
2. **fan-in 三件套**：设了 `wait_for` 就必须给 `merge_fn`；`wait_for_fn` 只能在已有 `wait_for` 时使用，用来按请求挑选「这次真正要等哪几个上游」。

#### 4.1.3 源码精读

`StageConfig` 用 Pydantic 建模，`model_config = ConfigDict(extra="forbid")` 意味着**写错字段名会直接报错**——这是配置层的自我保护。

**身份与构造**：[sglang_omni/config/schema.py:148-152](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L148-L152) 声明了 `name`、`factory`（点路径字符串）、`factory_args`（传给工厂的显式参数）。注意 `factory_args` 只放「与签名相关的显式参数」；像 `gpu_id` 这种由放置规划拥有的值会被**拒绝**写在这里（要用 `gpu`），而 `model_path` / `total_gpu_memory_fraction` 这类则由 worker 在导入工厂后再注入。

**路由字段**：[sglang_omni/config/schema.py:154-157](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L154-L157)。`next` 可以是单个字符串、字符串列表或 `None`；`terminal` 是布尔标志；`route_fn` 是点路径，其函数签名是 `(request_id, stage_output) -> str | list[str]`。

**GPU / 并行 / 进程**：[sglang_omni/config/schema.py:159-163](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L159-L163)。`gpu` 为 `None` 表示 CPU 放置，列表则用于张量并行（TP）各 rank；`tp_size` 在 `gpu` 为列表时必须等于 `len(gpu)`。

**fan-in 三件套**：[sglang_omni/config/schema.py:169-172](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L169-L172)。`wait_for` 列出所有可能的上游，`wait_for_fn` 的函数签名是 `(request_id, from_stage, payload) -> list[str] | None`——返回 `None` 表示「当前 payload 还不足以确定子集，请求继续挂起，等下一个上游 payload 再判」。

**流式三件套**：[sglang_omni/config/schema.py:174-177](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L174-L177)。`stream_to` 是流式目标的**静态全集**，运行时据此派生流接收方与同 GPU 流快路径；`stream_done_to_fn` 必须配合非空的 `stream_to` 使用；`can_accept_stream_before_payload` 允许阶段在正式 payload 到达前先吃 stream chunk（Qwen3-Omni 的 `decode` / `code2wav` 都开了它，见下方实例）。

**投影与通信**：[sglang_omni/config/schema.py:179-186](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L179-L186)。`project_payload` 是 `{目标阶段: 投影函数点路径}`，在**写下游 payload 之前**对数据做裁剪；`comm` 见 4.3。

来看真实实例。Qwen3-Omni 的 `mm_aggregate` 阶段同时展示了「静态全集 + 请求感知子集」与 fan-in：[sglang_omni/models/qwen3_omni/config.py:95-111](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L95-L111)

```python
StageConfig(
    name="mm_aggregate",
    process=process,
    factory=f"{_PKG}.stages.create_aggregate_executor",
    gpu=gpu,
    wait_for=["preprocessing", "image_encoder", "audio_encoder"],   # 静态全集
    wait_for_fn=f"{_PKG}.request_builders.resolve_mm_aggregate_wait_sources",
    merge_fn=f"{_PKG}.merge.merge_for_thinker",                      # fan-in 必备
    next=["thinker", "talker_ar"],                                   # 静态全集
    route_fn=f"{_PKG}.request_builders.resolve_mm_aggregate_next_stages",
    disable_direct_cuda_ipc_payload=True,
    project_payload={"talker_ar": f"...project_mm_aggregate_to_talker_ar"},
)
```

这段配置的含义是：`mm_aggregate` 可能要等三个上游，但具体等哪几个由 `resolve_mm_aggregate_wait_sources` 按请求决定（例如没有图像就不等 `image_encoder`）；收齐后用 `merge_for_thinker` 合成一路；合并结果**可能**发往 `thinker` 和 `talker_ar`，但真正发哪几个由 `resolve_mm_aggregate_next_stages` 按请求决定。

对应的 `wait_for_fn` 实现印证了「返回 `None` 表示尚未能确定」的语义：[sglang_omni/models/qwen3_omni/request_builders.py:123-132](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/request_builders.py#L123-L132)

```python
def resolve_mm_aggregate_wait_sources(request_id, from_stage, payload):
    del request_id
    if from_stage != "preprocessing":
        return None                         # 非 preprocessing 的到达，暂不裁剪
    state = Qwen3OmniPipelineState.from_dict(payload.data)
    return ["preprocessing", *_active_encoder_stages(state.encoder_inputs)]
```

只有当 `preprocessing` 的 payload 到达时，才能从输入里看出「这次有图、有音频还是有视频」，从而给出真正要等的上游子集。

#### 4.1.4 代码实践

**实践目标**：亲手验证「fan-in 三件套」的校验约束，理解 `merge_fn` 为何是 fan-in 的强制字段。

**操作步骤**：

1. 在已按 u1-l2 装好环境、`uv pip install -v -e .` 完成可编辑安装的 `.venv` 里，进入 Python 交互。
2. 构造一个**缺少 `merge_fn`** 的 fan-in 阶段，观察校验报错。

```python
# 示例代码：仅用于观察 schema 校验，不涉及 GPU
from sglang_omni.config import StageConfig, PipelineConfig

bad_aggregate = StageConfig(
    name="aggregate",
    process="aggregate",
    factory="pkg.create_aggregate",
    wait_for=["preprocessing", "image_enc"],   # 设了 wait_for
    # 故意不写 merge_fn
    next="thinker",
)
```

3. 把它连同必要的 `preprocessing` / `thinker` 放进一条 `PipelineConfig` 触发校验。

**需要观察的现象**：Pydantic 在构造 `PipelineConfig` 时会抛出类似 `Stage 'aggregate' has wait_for but no merge_fn` 的 `ValueError`（该检查位于 [sglang_omni/config/schema.py:365-367](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L365-L367)）。

**预期结果**：补上 `merge_fn="pkg.merge_for_thinker"` 后该错误消失。这印证了「fan-in 必须有合并函数」是一条**静态可校验**的契约——不需要 GPU、不需要权重，配置构造时就能拦下错误。

> 若本地暂无可运行 venv，可改为纯阅读型实践：打开 [sglang_omni/config/schema.py:365-372](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L365-L372)，逐行说出 `wait_for` 分支里的三条检查（缺 merge_fn、引用未知 stage、有 wait_for_fn 却无 wait_for），并解释为什么「未知上游名」也必须在这里拦住。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `route_fn` 不能写在 `terminal=True` 的阶段上？

> **答案**：terminal 阶段的结果直接发给 Coordinator（见 u2-l4），没有「下游 stage」可选，因此请求感知路由没有意义。校验在 [sglang_omni/config/schema.py:345-348](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L345-L348) 显式拒绝这种组合。

**练习 2**：一个阶段同时写了 `next=["a","b"]` 和 `route_fn`，合法吗？二者什么关系？

> **答案**：合法。`next` 是**静态全集**（供校验确认这些下游确实存在），`route_fn` 是**请求感知子集**（每次请求从全集里挑真正要发的）。校验只要求 `next` 引用的名字都已定义（[schema.py:375-381](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L375-L381)）；运行时由 `route_fn` 决定子集。

---

### 4.2 PipelineConfig：声明整条管线

#### 4.2.1 概念说明

`PipelineConfig` 把一串 `StageConfig` 组装成一条完整管线，并补充**管线级**信息：模型路径、入口阶段、阶段融合、运行时覆盖、放置策略、端点、以及「请求感知终态解析」。模型作者通常不直接用基类，而是**子类化**它——在子类里用 `ClassVar` 声明模型架构名、用 `Field(default_factory=...)` 给出该模型的默认 `stages`。

#### 4.2.2 核心流程

`PipelineConfig` 的字段可分三类：

```text
┌─ 身份 ──────────────────────────────────────────────┐
│  model_path  stages  name  config_cls               │
├─ 拓扑控制 ──────────────────────────────────────────┤
│  entry_stage  fused_stages  terminal_stages_fn      │
├─ 运行时调参 ────────────────────────────────────────┤
│  runtime_overrides  env_defaults  placement         │
│  placement_policy  endpoints                        │
└─ 模型族元信息（ClassVar，子类覆盖）─────────────────┘
   architecture  architecture_aliases  requires_model_capabilities ...
```

构造时 `model_post_init` 会做三件事（[schema.py:241-246](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L241-L246)）：调用 `_validate_general` 校验拓扑、调用 `_validate_fusion` 校验融合约束、自动写入 `config_cls = 类名`（这正是 u1-l5 讲过的「配置可回读」的关键——导出的 YAML 里会带上 `config_cls`）。

#### 4.2.3 源码精读

**身份与拓扑字段**：[sglang_omni/config/schema.py:228-239](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L228-L239)。其中几个值得特别说明：

- `entry_stage`：可选，不填则默认取 `stages[0].name`（见 4.4 的 `resolved_entry_stage`）。
- `fused_stages`：`list[list[str]]`，框架级「共置提示」，把相邻线性阶段合并进同一运行时进程；详见 `config.md` 的 Stage Fusion 一节。
- `terminal_stages_fn`：点路径，函数签名是 `(OmniRequest) -> list[str] | None`——按请求内容动态算出「这次应该等哪些终态」，返回 `None` 则退回静态终态。
- `runtime_overrides`：`{阶段名: {工厂参数: 值}}`，在运行时 prep 阶段覆盖工厂参数（区别于 `StageConfig.factory_args`，后者是声明期的默认）。

**模型族元信息（ClassVar）**：[sglang_omni/config/schema.py:220-226](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L220-L226)。这些是**类级常量**而非实例字段，最关键的是 `architecture`——它是模型注册表匹配 HF `config.json` 的钥匙（u5-l1 会详讲）。例如 Qwen3-Omni 把它设为 `"Qwen3OmniMoeForConditionalGeneration"`：[sglang_omni/models/qwen3_omni/config.py:273](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L273)。

来看「请求感知终态」如何落地。Qwen3-Omni 的 speech 管线声明了 `terminal_stages_fn`：[sglang_omni/models/qwen3_omni/config.py:336](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L336)

```python
terminal_stages_fn: str | None = f"{_PKG}.request_builders.resolve_terminal_stages",
```

对应实现按「是否要生成音频」返回不同终态集合：[sglang_omni/models/qwen3_omni/request_builders.py:106-109](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/request_builders.py#L106-L109)

```python
def resolve_terminal_stages(request: OmniRequest) -> list[str]:
    if should_generate_audio_output(request):
        return [DECODE_STAGE, CODE2WAV_STAGE]   # 文本 + 音频 两个终态
    return [DECODE_STAGE]                        # 仅文本 一个终态
```

这正好对应 u2-l4 讲的「Coordinator 必须等齐全部期望终态才结束请求」——期望终态子集就由这个函数算出，再与静态 `terminal=True` 的阶段（`decode` / `code2wav`）对照。

#### 4.2.4 代码实践

**实践目标**：体会「子类化 + 默认 stages」的声明式写法，以及 `config_cls` 如何被自动写入。

**操作步骤**：

1. 阅读基类的 `from_dict`：[sglang_omni/config/schema.py:474-476](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L474-L476)，确认它就是 `PipelineConfig(**data)`。
2. 阅读 Qwen3-Omni 的三个变体：text（6 阶段）、speech（8 阶段，`thinker_gpu=0, talker_gpu=1`）、speech-colocated（8 阶段但 `thinker_gpu=0, talker_gpu=0`），见 [sglang_omni/models/qwen3_omni/config.py:286-382](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L286-L382)。
3. 注意 `EntryClass` 与 `Variants` 字典（[config.py:376-382](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L376-L382)）：注册表通过 `EntryClass` 找到默认配置类，`Variants` 暴露多个拓扑变体。

**需要观察的现象**：三个变体**共享同一套 stage 工厂函数**（`_preprocessing_stage` 等），只是用不同的 `gpu` / `process_by_stage` / `enable_partial_start` 参数实例化。换句话说，拓扑的「形状」由这些 `_xxx_stage` 辅助函数固定，变体之间只差放置与共置参数。

**预期结果**：你能用一句话说出「同一个模型家族，如何仅靠改 `stages` 的 `gpu` / `process` 字段就派生出单卡共置与双卡分离两种部署形态」。若想确认 `config_cls` 自动写入，可在有 venv 时实例化任一变体后打印 `.config_cls`，应等于类名字符串（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`terminal_stages_fn` 返回 `None` 时会发生什么？

> **答案**：退回使用**静态终态**，即所有 `terminal=True` 的阶段（由 4.4 的 `terminal_stages` 派生属性给出）。这是「请求感知」失败或不需要时的安全回退。

**练习 2**：`runtime_overrides` 和 `StageConfig.factory_args` 都能影响工厂参数，区别在哪？

> **答案**：`factory_args` 是**声明期默认**，写在模型 config.py 里；`runtime_overrides` 是**运行时 prep 覆盖**，通常来自 YAML / 命令行，优先级更高（运行时 prep 会把二者合并）。这与 u1-l5 讲的「YAML 的 `stage_overrides` 覆盖模型默认」一脉相承。

---

### 4.3 CommConfig：通信调参（不选传输）

#### 4.3.1 概念说明

`CommConfig` 是**每个阶段的通信调参块**，只调缓冲池大小和后端连接选项（如 Mooncake 的协议 / 主机名）。它**不**负责选择传输后端——传输选择（`local_object` / 直接 CUDA IPC / `cuda_ipc` / 共享内存 / `mooncake`）是 `CommRouter` 根据阶段的局部性与放置**派生**出来的（u6-l1 详讲）。这是一个容易误解的点：很多人以为在配置里「指定用 nccl / nixl」，其实配置层只调参、不选路。

#### 4.3.2 核心流程

`CommConfig` 的字段全部是「池/连接」调参：

| 字段 | 默认 | 作用 |
| --- | --- | --- |
| `slot_size_mb` | 512 | 通信缓冲槽大小 |
| `credits` | 2 | 基于信用的流量控制额度 |
| `cuda_ipc_slot_size_kb` | 64 | CUDA IPC 单槽粒度 |
| `cuda_ipc_pool_size_mb` | None | CUDA IPC 发送池总大小 |
| `mooncake_protocol` | "rdma" | Mooncake 传输协议 |
| `mooncake_hostname` / `mooncake_device_name` | None / "" | Mooncake 连接定位 |

#### 4.3.3 源码精读

`CommConfig` 定义非常简短：[sglang_omni/config/schema.py:11-27](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L11-L27)。注意它的 docstring 明确写道：「Transport selection is owned by `CommRouter` from stage locality and placement. This config only tunes buffer pools and backend-specific connection options for transports the router selects.」

而 `StageConfig.comm` 字段本身是可选的：[sglang_omni/config/schema.py:186](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L186)（`comm: CommConfig | None = None`）。绝大多数阶段**不需要**显式写 `comm`，用默认值即可；只有要为大张量流式传输调大缓冲池、或为 RDMA 后端指定设备名时才需要写。

`config.md` 也反复强调这一点：「`CommConfig` … does not select a transport backend.」（[docs/developer_reference/config.md:133-135](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/config.md#L133-L135)）。

#### 4.3.4 代码实践

**实践目标**：确认「传输选择不在配置层」这一边界，避免日后误改。

**操作步骤**：

1. 在 `sglang_omni/config/schema.py` 与 `docs/developer_reference/config.md` 中搜索 `transport` 一词，确认 schema 层没有任何「选 nccl / nixl / mooncake」的字段。
2. 用 `Grep` 在 `sglang_omni/comm/` 下找到真正做传输选择的 `CommRouter`（提示：`sglang_omni/comm/router.py`），大致浏览它如何读取 stage 的局部性。

**需要观察的现象**：配置 schema 里**没有**任何形如 `transport_backend: "nixl"` 的字段；`CommRouter` 读取的是阶段之间的「是否同进程 / 同节点 / 同 GPU」局部性，而非某个配置开关。

**预期结果**：你能向同事解释「想在跨节点场景强制用 Mooncake，不能在 StageConfig 里写一个 backend 字段，而要靠放置（让两个阶段落在不同节点）让 `CommRouter` 自然派生出 mooncake 传输，再用 `CommConfig` 调它的协议/设备参数」。具体选择规则留待 u6-l1。

#### 4.3.5 小练习与答案

**练习**：某阶段跨节点传输延迟高，有人建议「在 `StageConfig` 里加一个 `transport="mooncake"` 字段」。这个建议对吗？

> **答案**：不对。schema 不提供传输选择字段；传输由 `CommRouter` 按局部性派生。正确做法是确保放置让该边跨节点（`CommRouter` 会选 mooncake），再用 `CommConfig` 的 `mooncake_protocol` / `mooncake_device_name` 调参。

---

### 4.4 派生字段与静态校验

#### 4.4.1 概念说明

配置层有大量值**不是手填的**，而是从 `stages` 算出来的——本讲聚焦三个派生属性：`resolved_entry_stage`、`terminal_stages`、`gpu_placement`。它们以 `@property` 形式暴露，保证「单一真相」：你改了 `stages`，派生值自动跟着变，不会出现手填与实际不一致。

与派生属性配套的是一套**静态校验**（`_validate_general` / `_validate_fusion`），在配置构造时就拦下非法拓扑——这是「配置是契约」最有力的体现。

#### 4.4.2 核心流程

三个派生属性的来源：

```text
resolved_entry_stage = entry_stage if 显式设置 else stages[0].name
terminal_stages      = [s.name for s in stages if s.terminal]
gpu_placement        = {s.name: s.gpu  for s in stages if s.gpu is not None}
```

放置规划还有一个隐含的「预算」约束：同一 GPU 上各阶段的显存份额之和不得超过上限。设第 \(i\) 个阶段的份额为 \(f_i\)，则同一 GPU 上需满足：

\[
\sum_{i \in \text{同GPU}} f_i \;\le\; L, \quad 0 < f_i \le 1,\; L = \texttt{max\_total\_gpu\_memory\_fraction\_per\_gpu}
\]

其中 \(f_i\) 来自 `StageConfig.runtime.resources.total_gpu_memory_fraction`，\(L\) 来自 `PlacementConfig`（[schema.py:116](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L116)）。在共置（colocated）部署里，多个阶段挤进一张卡，这个不等式就是「放不放得下」的判据。

#### 4.4.3 源码精读

**`resolved_entry_stage`**：[sglang_omni/config/schema.py:248-252](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L248-L252)。优先用显式 `entry_stage`，否则取第一个阶段名。Coordinator 据此把新请求 PUSH 给入口阶段（u2-l4）。

**`terminal_stages`**：[sglang_omni/config/schema.py:254-256](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L254-L256)。遍历 `stages` 取所有 `terminal=True` 的名字。注意它给出的是**静态全集**；若配了 `terminal_stages_fn`，运行时会再按请求裁剪（4.2 已述）。

**`gpu_placement`**：[sglang_omni/config/schema.py:316-322](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L316-L322)。返回 `{阶段名: gpu 或 gpu 列表}`，只含显式设了 `gpu` 的阶段（`gpu=None` 即 CPU 阶段不出现）。

**静态校验 `_validate_general`**：[sglang_omni/config/schema.py:324-406](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L324-L406) 是整份契约的「守门人」，关键检查包括：

- `model_path` 必填、`stages` 非空、阶段名唯一、入口阶段确实存在（[schema.py:325-335](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L325-L335)）。
- 每个阶段「必须且只能设置 `next` 或 `terminal` 之一」（[schema.py:340-344](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L340-L344)）。
- `tp_size` 与 `gpu` 列表长度一致（[schema.py:360-364](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L360-L364)）。
- fan-in 三件套一致性（[schema.py:365-374](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L365-L374)）。
- `next` / `stream_to` / `project_payload` 引用的下游都必须存在（[schema.py:375-391](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L375-L391)）。
- **所有非 TP 阶段必须显式声明 `process`**（[schema.py:399-406](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L399-L406)）——这条与 u3-l4 的进程拓扑直接相关：没有隐式默认，共享 `process` 值即共进程。

来看真实预算。Qwen3-Omni 共置 YAML 给出了一张卡上五个阶段的份额：[examples/configs/qwen3_omni_colocated_h100_bf16.yaml:7-31](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/examples/configs/qwen3_omni_colocated_h100_bf16.yaml#L7-L31)，`thinker` 占 0.78、`talker_ar` 占 0.10、两个编码器各 0.02、`code2wav` 占 0.02，合计远小于 1.0，正好是上面那个不等式的一个实例。注意这份 YAML **只覆盖 `runtime`**，拓扑形状仍在 config.py 里——这是 u1-l5「紧凑覆盖文件」结论的直接体现。

#### 4.4.4 代码实践

**实践目标**：用一份真实的「紧凑覆盖 YAML」反推拓扑，把本讲四个模块串起来。

**操作步骤**：

1. 打开 [examples/configs/qwen3_omni_colocated_h100_bf16.yaml](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/examples/configs/qwen3_omni_colocated_h100_bf16.yaml)，读到 `config_cls: Qwen3OmniSpeechColocatedPipelineConfig`。
2. 顺藤摸瓜到 [sglang_omni/models/qwen3_omni/config.py:352-373](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L352-L373)，看到它的 `stages` 由 `_speech_stages(thinker_gpu=0, talker_gpu=0, ...)` 生成（单卡共置）。
3. 逐个读 `_preprocessing_stage` / `_image_encoder_stage` / `_audio_encoder_stage` / `_aggregate_stage` / `_thinker_stage` / `_decode_stage` / `_talker_stage` / `_code2wav_stage`（[config.py:35-209](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L35-L209)），记下每个阶段的 `next` / `terminal` / `stream_to` / `wait_for`。

**需要观察的现象与预期结果**：在纸上画出如下拓扑（节点 = 阶段，边类型见图例）：

```text
                    preprocessing
                   /      |        \          ← fan-out: next=[image_encoder, audio_encoder, mm_aggregate]
          image_encoder  audio_encoder        （route_fn 按请求裁剪）
                   \      |        /
                    mm_aggregate             ← fan-in: wait_for=[preprocessing, image_encoder, audio_encoder]
                       |      \                 merge_fn = merge_for_thinker        ★本题答案
                    thinker   \              ← stream_to=[talker_ar, decode]（hidden state 流式边）
                  /     |      \
            decode   (route)  talker_ar       ← next 静态全集, route_fn 裁剪
              |                   | stream_to=[code2wav]
           terminal             code2wav
        (文本收口)              terminal
                              (音频收口)
```

**关键提问**：哪个字段声明了 fan-in 的 merge 函数？

> **答案**：`mm_aggregate` 阶段的 `merge_fn` 字段，值为 `sglang_omni.models.qwen3_omni.merge.merge_for_thinker`（[config.py:102](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L102)）。它把 `preprocessing` + 活跃编码器三路 payload 合成喂给 thinker 的输入。fan-in 边由 `wait_for`（要等谁）+ `merge_fn`（怎么合）共同声明。

画完图后，再用 4.4.2 的不等式核对：YAML 里五个 GPU 阶段同在 GPU 0，份额之和 \(0.02+0.02+0.78+0.10+0.02 = 0.94 \le 1.0\)，放置合法。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `thinker` 的 `terminal` 误设为 `True`，同时又保留了 `next="decode"`，会发生什么？

> **答案**：触发 [schema.py:340-344](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L340-L344) 的检查，报「must set exactly one of 'next' or 'terminal'」。配置构造即失败，无需等到运行。

**练习 2**：`resolved_entry_stage` 与 `terminal_stages` 为什么做成 `@property` 而不是普通字段？

> **答案**：保证它们永远从 `stages` 派生，是「单一真相」。若做成字段，用户可能手填一个与 `stages` 不一致的值，导致 Coordinator 把请求送错入口或等错终态。

**练习 3**：一份新管线报错「Non-TP stages must declare process; missing process for ['foo']」。这说明作者漏写了哪个字段？为什么 TP 阶段不报这个错？

> **答案**：漏写了 `StageConfig.process`。校验要求所有 `tp_size==1` 的阶段显式声明 `process`（[schema.py:399-406](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L399-L406)）。TP 阶段的进程名由 `{process}_tp{rank}` 派生、`process` 可选，故不在此列（u6-l6 详讲）。

---

## 5. 综合实践

**任务**：为「新增一个最小三阶段 TTS 管线」起草一份 `PipelineConfig` 草图，把本讲四个模块全部用上。

假设阶段为：`preprocessing` → `tts_engine`（GPU）→ `vocoder`（GPU，terminal）。要求：

1. **StageConfig**：为三个阶段各写一份字段草图，注意：
   - 每个非 TP 阶段都要写 `process`；
   - `tts_engine` 用 `next="vocoder"`、`stream_to=["vocoder"]`（边生成边把中间码本流式喂给 vocoder）；
   - `vocoder` 设 `terminal=True` 且 `can_accept_stream_before_payload=True`；
   - 给 `tts_engine` 配一个 `project_payload={"vocoder": "...project_to_vocoder"}`。
2. **PipelineConfig**：子类化基类，设 `architecture` ClassVar、用 `Field(default_factory=...)` 给出 `stages`，并显式设 `placement` 与 `placement_policy`。
3. **CommConfig**：说明你会**在哪个阶段**写 `comm`（提示：跨阶段流式大张量的那一跳），以及为什么**不**在这里指定传输后端。
4. **派生字段**：写出你这条管线的 `resolved_entry_stage`、`terminal_stages`、`gpu_placement` 三个值（手算，不实例化）。
5. **自检**：逐条对照 [schema.py:324-406](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L324-L406) 的校验清单，确认你的草图能通过。

**验收标准**：草图里每条 `next` / `wait_for` / `stream_to` / `project_payload` 引用的下游都已定义；fan-in（如果加了）必带 `merge_fn`；所有非 TP 阶段都有 `process`。可参照 [docs/developer_reference/config.md:25-67](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/config.md#L25-L67) 的官方示例对照。完整接入流程（含注册、YAML、测试）见 u7-l5。

## 6. 本讲小结

- **StageConfig** 是配置层最小积木，字段分五组：身份构造、路由（`next`/`terminal`/`route_fn`）、GPU 并行进程、fan-in（`wait_for`/`wait_for_fn`/`merge_fn`）、streaming（`stream_to`/`stream_done_to_fn`），外加 `project_payload` 与 `comm`。
- **静态全集 + 请求感知子集**是贯穿模式：`next`/`wait_for`/`stream_to` 描述所有可能，`route_fn`/`wait_for_fn`/`stream_done_to_fn` 在运行时按请求裁剪。
- **PipelineConfig** 组装阶段并提供管线级控制：`entry_stage`、`fused_stages`、`terminal_stages_fn`（请求感知终态）、`runtime_overrides` 等；模型作者通过子类化 + `ClassVar`（如 `architecture`）接入。
- **CommConfig 只调参、不选传输**：传输后端由 `CommRouter` 按局部性派生，配置层不提供「选 nccl/nixl」的开关。
- **三个派生属性**（`resolved_entry_stage` / `terminal_stages` / `gpu_placement`）从 `stages` 自动算出，保证单一真相。
- **静态校验**（`_validate_general` / `_validate_fusion`）在构造时就拦下非法拓扑，包括「next/terminal 互斥」「fan-in 必带 merge_fn」「非 TP 阶段必声明 process」「tp_size 与 gpu 列表长度一致」等。

## 7. 下一步学习建议

配置只是「蓝图」，真正让它跑起来的是运行时。建议接下来：

- **u3-l1（Stage 抽象与 IO 外壳）**：看 `StageConfig` 被如何实例化成一个真实的 `Stage`，理解 `next` / `wait_for` / `stream_to` 在运行时如何变成控制消息与 relay 搬运。
- **u3-l4（进程拓扑与多进程 Runner）**：看 `_validate_general` 里「非 TP 阶段必声明 process」这条约束如何被 `MultiProcessPipelineRunner` 消费，把 `process` 字段解析成真实的 OS 进程布局。
- **u6-l1（通信路由与传输选择）**：补上本讲刻意留白的「`CommRouter` 如何根据阶段局部性派生出 local_object / cuda_ipc / shm / mooncake 传输」，与本讲的 `CommConfig` 形成闭环。
- 若你想立刻看到一条真实管线的全貌，可先跳读 **u5-l2（Qwen3-Omni 端到端管线）**，把本讲画的 DAG 与 thinker→talker 的流式细节对上。
