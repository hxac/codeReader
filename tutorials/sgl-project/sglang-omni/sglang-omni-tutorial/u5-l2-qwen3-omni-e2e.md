# Qwen3-Omni 端到端管线

## 1. 本讲目标

学完本讲，你应该能够：

- 读懂 `Qwen3OmniPipelineConfig` 的 stage 列表，画出 Qwen3-Omni 从输入到文本/音频输出的完整 stage DAG。
- 区分三种边：普通 `next`（最终结果路由）、`stream_to`（每步流式 chunk）、`stream_done_to_fn`（流结束信号），并说清它们各自由谁触发。
- 理解 thinker→talker 的「双 AR」关系：thinker 把每 token 的 hidden state 流式喂给 talker，让语音生成与思考生成重叠执行（partial-start）。
- 说清 colocated（单卡共置）与 disaggregated（多卡分离）两种拓扑在 GPU 放置、进程划分、显存预算、partial-start 上的差异。

本讲是 u5 单元「模型家族集成」的核心篇，承接 u5-l1（注册表）与 u4-l2（OmniScheduler），把前面学过的「stage 抽象、调度器、配置契约」全部落到一个真实模型上。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：Qwen3-Omni 是「双 AR + 多模态编码器」的 omni 模型。** 它内部其实有两个自回归（AR）模型：

- **thinker**：大模型，负责「思考」——读多模态输入、生成文本 token（也就是对话内容）。
- **talker**：小模型，负责「说话」——把 thinker 产出的文本表示变成语音 codec 码本，再交给 vocoder 合成波形。

为了让语音不等到文本全部生成才开始，talker 会在 thinker 还在生成时就「边读边说」。

**直觉二：本框架里「一条请求 = 一张 stage 有向图」。** 这张图里：

- 每个节点是一个 stage（preprocessing / image_encoder / audio_encoder / mm_aggregate / thinker / talker_ar / decode / code2wav）。
- 每条边是一次数据搬运，搬运方式分三类（见 4.1）。
- 一条请求可以有**多个终点**（terminal）：文本终点 `decode`、音频终点 `code2wav`，由 Coordinator 合并（回顾 u2-l4）。

**直觉三：配置只描述「拓扑 + 放置意图」，不描述「怎么算」。** `config.py` 用 `StageConfig` 声明「谁连到谁、放在哪块卡、共用哪个进程」；真正「怎么算」由 `stages.py` 的工厂函数在运行时构造（回顾 u2-l5 的「配置是模型定义与运行时之间的契约」）。

> 术语提示：本讲频繁出现 `next` / `route_fn` / `stream_to` / `stream_done_to_fn` / `wait_for` / `merge_fn` / `project_payload`，它们都是 `StageConfig` 的字段，含义在 u2-l5 已建立，本讲只看 Qwen3-Omni 如何**使用**它们。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [sglang_omni/models/qwen3_omni/config.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py) | 声明三个 `PipelineConfig` 子类（text / speech / speech-colocated）与每个 stage 的 `StageConfig`。**本讲主战场。** |
| [sglang_omni/models/qwen3_omni/stages.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/stages.py) | stage 工厂函数：简单阶段返回 `SimpleScheduler`，AR 阶段返回 `OmniScheduler`。 |
| [sglang_omni/models/qwen3_omni/request_builders.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/request_builders.py) | 请求感知路由函数（`resolve_*`）、投影函数（`project_*`）与 thinker 流式输出构造器。 |
| [sglang_omni/models/qwen3_omni/merge.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/merge.py) | `merge_for_thinker`：把 preprocessing + 各 encoder 的产物聚合成 thinker 输入。 |
| [sglang_omni/models/qwen3_omni/payload_types.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/payload_types.py) | `Qwen3OmniPipelineState`：贯穿整条管线的「每请求状态」数据类。 |
| [sglang_omni/models/qwen3_omni/placement.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/placement.py) | `Qwen3OmniPlacementPolicy`：校验 colocated 拓扑与显存预算的模型专属规则。 |
| [examples/configs/qwen3_omni_colocated_h100_bf16.yaml](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/examples/configs/qwen3_omni_colocated_h100_bf16.yaml) | 真实部署用的 colocated YAML：只覆盖每个 stage 的 `total_gpu_memory_fraction`。 |

---

## 4. 核心概念与源码讲解

### 4.1 Qwen3-Omni 管线全貌与 stage DAG

#### 4.1.1 概念说明

Qwen3-Omni 在配置层提供**两套 stage 列表**，对应两种输出形态：

- **text-only（6 阶段）**：只产出文本。stage 为 `preprocessing → image_encoder / audio_encoder → mm_aggregate → thinker → decode`。
- **speech（8 阶段）**：同时产出文本与音频。在 text 基础上**多了 talker_ar 与 code2wav 两个阶段**，并让 thinker 把 hidden state 流式喂给 talker_ar。

为什么 speech 多两个阶段？因为「文本→语音」是一条独立的子链路：thinker 产出的文本表示要先经 talker_ar（一个小 AR 模型）转成 codec 码本，再经 code2wav（vocoder）合成波形。这条子链路有自己的终点 `code2wav`，所以 speech 管线有**两个 terminal**：`decode`（文本）与 `code2wav`（音频）。

> 回顾 u1-l1：一次生成被建模为七大阶段类别（preprocessing/encoders/AR engines/talkers/decoders/vocoder/aggregators）。Qwen3-Omni 的 8 个 stage 与之一一对应——preprocessing、image/audio_encoder、thinker、talker_ar、decode、code2wav、mm_aggregate。

#### 4.1.2 核心流程

先用伪代码画出两套管线的拓扑。注意：**preprocessing 的扇出是「请求感知」的**——只有当请求里真有图片/音频时，才会走向 image_encoder / audio_encoder；纯文本请求会跳过它们。

```
text-only（Qwen3OmniPipelineConfig，6 阶段）：

            ┌──(若有图)──> image_encoder ──┐
preprocessing ┤                            ├──> mm_aggregate ──merge──> thinker ──next──> decode (terminal: 文本)
            ├──(若有音)──> audio_encoder ──┤
            └──────────────────────────────┘
```

```
speech（Qwen3OmniSpeechPipelineConfig，8 阶段）：

            ┌──(若有图)──> image_encoder ──┐
preprocessing ┤                            ├──> mm_aggregate ──merge──┬──next──> thinker
            ├──(若有音)──> audio_encoder ──┤                            │
            └──────────────────────────────┘                            └──next──> talker_ar
                                                                                  │
   thinker ──stream_to──> talker_ar   （每 token 的 hidden state，喂给 talker 生成 codec）
   thinker ──stream_to──> decode       （每 token id，流式文本用）
   thinker ──stream_done──> {talker_ar, decode}   （thinker 结束信号）
   thinker ──next──> decode            （thinker 最终结果只送文本终点）
   talker_ar ──stream_to──> code2wav   （每 chunk 的 codec 码本）
   talker_ar ──next──> code2wav        （talker 最终结果）

   decode   = terminal（文本）
   code2wav = terminal（音频）
```

三类边的一句话区分（这是本讲最重要的认知）：

| 边类型 | 字段 | 触发时机 | 携带内容 |
|--------|------|----------|----------|
| 最终结果 | `next` / `route_fn` | 一个 stage **算完整个请求** | 完整 payload（投影后） |
| 流式 chunk | `stream_to` | AR 阶段**每生成一个 token** | 单步增量（hidden state / token id / codec） |
| 流结束 | `stream_done_to_fn` | AR 阶段**生成终止** | 「没有更多 chunk 了」信号 |

#### 4.1.3 源码精读

两套 stage 列表由两个工厂函数生成。**text 列表**把所有 stage 压进同一个进程 `pipeline`、共用 GPU 0：

```python
def _text_stages() -> list[StageConfig]:
    return [
        _preprocessing_stage(process="pipeline"),
        _image_encoder_stage(gpu=0, process="pipeline"),
        _audio_encoder_stage(gpu=0, process="pipeline"),
        _aggregate_stage(process="pipeline", gpu=0, speech_enabled=False),
        _thinker_stage(gpu=0, speech_enabled=False, process="pipeline"),
        _decode_stage(process="pipeline"),
    ]
```

见 [config.py:212-220](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L212-L220)。注意 `_thinker_stage` 传了 `speech_enabled=False`，所以 thinker 不会流式给 talker（text 模式根本没有 talker）。

**speech 列表**则由 `process_by_stage` 字典决定每个 stage 的进程归属，并允许 thinker / talker 落在不同 GPU：

```python
def _speech_stages(*, thinker_gpu, talker_gpu, process_by_stage, enable_partial_start):
    return [
        _preprocessing_stage(process=process_by_stage["preprocessing"]),
        _image_encoder_stage(gpu=thinker_gpu, process=process_by_stage["image_encoder"]),
        _audio_encoder_stage(gpu=thinker_gpu, process=process_by_stage["audio_encoder"]),
        _aggregate_stage(process=process_by_stage["mm_aggregate"], gpu=thinker_gpu, speech_enabled=True),
        _thinker_stage(gpu=thinker_gpu, speech_enabled=True, process=process_by_stage["thinker"]),
        _decode_stage(process=process_by_stage["decode"]),
        _talker_stage(gpu=talker_gpu, process=process_by_stage["talker_ar"], enable_partial_start=enable_partial_start),
        _code2wav_stage(gpu=talker_gpu, process=process_by_stage["code2wav"]),
    ]
```

见 [config.py:223-257](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L223-L257)。`thinker_gpu` 与 `talker_gpu` 是否相同，正是「disaggregated」与「colocated」的分水岭（详见 4.4）。

三个 `PipelineConfig` 子类把上面两套列表接成最终配置：

- `Qwen3OmniPipelineConfig`（6 阶段 text）：[config.py:286-304](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L286-L304)
- `Qwen3OmniSpeechPipelineConfig`（8 阶段 speech，默认 disaggregated）：[config.py:307-349](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L307-L349)
- `Qwen3OmniSpeechColocatedPipelineConfig`（8 阶段 speech，单卡共置）：[config.py:352-373](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L352-L373)

它们的公共基类 `_Qwen3OmniBasePipelineConfig` 用 `ClassVar` 声明了与 HuggingFace `config.json` 匹配的架构名，这是注册表识别本模型的钥匙（回顾 u5-l1）：

```python
class _Qwen3OmniBasePipelineConfig(PipelineConfig):
    architecture: ClassVar[str] = "Qwen3OmniMoeForConditionalGeneration"
```

见 [config.py:272-274](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L272-L274)。

而 `EntryClass` 指向 speech 版本，说明 Qwen3-Omni 的**默认入口是语音管线**；`Variants` 字典则把 text / speech / speech-colocated 三个子类登记为可选变体：

```python
EntryClass = Qwen3OmniSpeechPipelineConfig

Variants = {
    "text": Qwen3OmniPipelineConfig,
    "speech": Qwen3OmniSpeechPipelineConfig,
    "speech-colocated": Qwen3OmniSpeechColocatedPipelineConfig,
}
```

见 [config.py:376-382](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L376-L382)。

#### 4.1.4 代码实践

**实践目标**：在不启动 GPU 服务的前提下，用纯 Python 把三个配置类的 stage 名字与连接关系打印出来，验证你对 DAG 的理解。

**操作步骤**（示例代码，非项目原有）：

```python
# 示例代码：只读配置，不需要 GPU/权重
from sglang_omni.models.qwen3_omni.config import (
    Qwen3OmniPipelineConfig,
    Qwen3OmniSpeechPipelineConfig,
    Qwen3OmniSpeechColocatedPipelineConfig,
)

for cls in (Qwen3OmniPipelineConfig, Qwen3OmniSpeechPipelineConfig, Qwen3OmniSpeechColocatedPipelineConfig):
    cfg = cls(model_path="Qwen/Qwen3-Omni-30B-A3B-Instruct")
    print(f"\n=== {cls.__name__}（{len(cfg.stages)} 阶段）===")
    for s in cfg.stages:
        terminal = " [TERMINAL]" if s.terminal else ""
        print(f"  {s.name}: next={s.next} stream_to={s.stream_to}{terminal}")
```

**需要观察的现象**：

- text 配置应打印 6 个 stage，`thinker` 的 `stream_to=['decode']`、`decode` 是 terminal。
- speech 与 colocated 都应打印 8 个 stage，`thinker` 的 `stream_to=['talker_ar', 'decode']`，且 `decode` 与 `code2wav` 都是 terminal。

**预期结果**：三份输出与 4.1.2 的伪代码拓扑完全一致。若 `stream_to` 不符，说明你拿到的配置类版本与本讲 HEAD 不一致。

> 说明：本实践只实例化 `PipelineConfig`，构造时会跑一遍静态校验（见 4.2.3），所以非法拓扑会直接抛 `ValueError`，这也是一个免费的正确性检查。运行结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：text 配置里 `thinker` 的 `stream_to` 为什么是 `['decode']` 而不是空？

> **答案**：因为文本需要「边生成边吐字」。thinker 每生成一个 token，就把 token id 流式送给 `decode` 阶段做增量解码（detokenize），从而实现流式文本输出。`next='decode'` 负责最终完整结果，`stream_to=['decode']` 负责每步增量。

**练习 2**：speech 配置比 text 多了哪两个 stage？它们各自是什么角色？

> **答案**：多了 `talker_ar`（小 AR 模型，把 thinker 的 hidden state 转成语音 codec 码本）与 `code2wav`（vocoder，把 codec 码本合成波形）。`code2wav` 是音频终点 terminal。

---

### 4.2 请求感知路由：preprocessing 扇出与 mm_aggregate fan-in

#### 4.2.1 概念说明

Qwen3-Omni 的 DAG 不是「写死的静态图」，而是「**静态全集 + 请求感知子集**」（回顾 u2-l5）。最典型的两处：

1. **preprocessing 扇出（fan-out）**：`next` 静态列出 `['image_encoder', 'audio_encoder', 'mm_aggregate']` 三个去处，但运行时由 `route_fn` 按请求内容裁剪——纯文本请求只送 `mm_aggregate`，跳过两个 encoder。
2. **mm_aggregate 汇聚（fan-in）**：`wait_for` 静态列出 `['preprocessing', 'image_encoder', 'audio_encoder']`，运行时由 `wait_for_fn` 算出「这次请求真正需要等哪些上游」——纯文本请求只等 `preprocessing`。

为什么要这样设计？因为不同请求携带的模态不同。如果静态地把所有请求都强制送进 image/audio encoder，纯文本请求就会白跑两个 GPU 编码器；如果静态地让 aggregate 等齐三个上游，纯文本请求就会永远等不到 audio_encoder 而 hang。所以路由必须是「请求感知」的。

#### 4.2.2 核心流程

preprocessing 阶段决定扇出子集的逻辑：

```
resolve_preprocessing_next_stages(state):
    return [带有 model_inputs 的 encoder] + [mm_aggregate]
```

即：检查 `state.encoder_inputs` 里 image_encoder / audio_encoder 各自是否真的有像素值 / 音频特征，有才加入下一跳；`mm_aggregate` 永远加入。

mm_aggregate 决定等待子集的逻辑（注意它只对 `from_stage == 'preprocessing'` 这一路生效）：

```
resolve_mm_aggregate_wait_sources(from_stage, payload):
    if from_stage != 'preprocessing': return None   # 暂挂，等 preprocessing 这一路到了再说
    return ['preprocessing'] + [活跃的 encoder]
```

`return None` 的语义是「我现在还不知道要等谁，先挂起」——这是 u2-l5「`_fn` 返回 None 则暂挂等待」的具体体现。只有当 preprocessing 那一路到达时，aggregate 才能从它携带的 `encoder_inputs` 元数据里看出「这个请求激活了哪些 encoder」，从而确定完整的等待集合；之后再到的 encoder 产物各自命中对应的等待项，集合相等时触发 `merge_fn`。

数学上，fan-in 的触发条件是一个集合相等判定。设静态全集为 \( S = \{\text{preprocessing}, \text{image\_encoder}, \text{audio\_encoder}\} \)，请求感知子集为 \( A \subseteq S \)，已到达上游集合为 \( R \)。则：

\[
\text{merge 触发} \iff R = A
\]

既不能多（\( R \supsetneq A \)，说明收到了不该来的上游，配置有误），也不能少（\( R \subsetneq A \)，还要继续等）。

#### 4.2.3 源码精读

preprocessing 的 stage 定义里，`next` 列出三个静态去处，`route_fn` 与三个 `project_payload` 各自对应：

```python
def _preprocessing_stage(*, process: str) -> StageConfig:
    return StageConfig(
        name="preprocessing",
        process=process,
        factory=f"{_PKG}.stages.create_preprocessing_executor",
        ...
        next=["image_encoder", "audio_encoder", "mm_aggregate"],
        route_fn=f"{_PKG}.request_builders.resolve_preprocessing_next_stages",
        project_payload={
            "image_encoder": f"...project_preprocessing_to_image_encoder",
            "audio_encoder": f"...project_preprocessing_to_audio_encoder",
            "mm_aggregate": f"...project_preprocessing_to_mm_aggregate",
        },
    )
```

见 [config.py:35-58](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L35-L58)。`project_payload` 的作用是「给每个下游定制一份精简 payload」——比如送 image_encoder 的只带图像输入，送 mm_aggregate 的只带轻量元数据（不带大张量），避免把整份状态无差别广播。

路由函数本身极简，它把决定权转给 `_encoder_stages_with_model_inputs`：

```python
def resolve_preprocessing_next_stages(request_id, output):
    state = Qwen3OmniPipelineState.from_dict(output.data)
    return [
        *_encoder_stages_with_model_inputs(state.encoder_inputs),
        MM_AGGREGATE_STAGE,
    ]
```

见 [request_builders.py:112-120](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/request_builders.py#L112-L120)。而「是否有 model_inputs」最终落到 `_has_encoder_model_input`——它按 stage 名查具体字段：

```python
def _has_encoder_model_input(stage_name, stage_inputs):
    ...
    if stage_name == IMAGE_STAGE:
        return (stage_inputs.get("pixel_values") is not None
                or stage_inputs.get("pixel_values_videos") is not None)
    if stage_name == AUDIO_STAGE:
        return stage_inputs.get("input_features") is not None
    return False
```

见 [request_builders.py:391-403](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/request_builders.py#L391-L403)。也就是说：有 `pixel_values`/`pixel_values_videos` 才走图像编码器；有 `input_features` 才走音频编码器。这就是「请求感知扇出」的物理判定。

mm_aggregate 这边（speech 模式）同时声明了静态 `wait_for`、请求感知 `wait_for_fn`、合并函数 `merge_fn`：

```python
return StageConfig(
    name="mm_aggregate",
    ...
    wait_for=["preprocessing", "image_encoder", "audio_encoder"],
    wait_for_fn=f"{_PKG}.request_builders.resolve_mm_aggregate_wait_sources",
    merge_fn=f"{_PKG}.merge.merge_for_thinker",
    next=["thinker", "talker_ar"],
    route_fn=f"{_PKG}.request_builders.resolve_mm_aggregate_next_stages",
    disable_direct_cuda_ipc_payload=True,
    project_payload={"talker_ar": f"...project_mm_aggregate_to_talker_ar"},
)
```

见 [config.py:94-111](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L94-L111)。注意 `next=["thinker", "talker_ar"]`——aggregate 的最终结果会同时送 thinker 和 talker_ar（talker_ar 那一路是「提前提交」，详见 4.3）。

`merge_for_thinker` 把多路上游产物聚合成 thinker 输入，关键动作是把各 encoder 的 embedding 收进 `thinker_inputs`，并**清空 `encoder_outs`** 以免重复搬运大张量：

```python
def merge_for_thinker(payloads):
    base = payloads.get("preprocessing") or next(iter(payloads.values()))
    state = Qwen3OmniPipelineState.from_dict(base.data)
    encoder_outs = {}
    ...  # 收集每个上游 stage 的 encoder 输出
    state.thinker_inputs = build_thinker_inputs(state, encoder_outs)
    state.encoder_outs = {}      # 已被消费进 thinker_inputs，清空避免重复传输
    return base
```

见 [merge.py:35-60](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/merge.py#L35-L60)。

> 配置层的安全网：`StageConfig` 在构造时跑静态校验，确保 `wait_for` 必带 `merge_fn`、`stream_done_to_fn` 必带 `stream_to`、`next` 与 `terminal` 互斥等。见 [schema.py:341-391](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L341-L391)。非法拓扑会在配置实例化时立即报错，而不是等到运行时 hang。

#### 4.2.4 代码实践

**实践目标**：通过阅读源码确认「纯文本请求跳过 encoder」这条调用链，并理解 `wait_for_fn` 返回 `None` 的暂挂语义。

**操作步骤**（源码阅读型实践）：

1. 打开 [request_builders.py:123-132](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/request_builders.py#L123-L132) 的 `resolve_mm_aggregate_wait_sources`。
2. 追踪：若一个请求**没有图像也没有音频**，`_active_encoder_stages` 会返回什么？（提示：两个 encoder 都不活跃 → 返回空列表）
3. 推导：此时 `resolve_mm_aggregate_wait_sources`（当 `from_stage == 'preprocessing'`）返回 `['preprocessing']`，即 aggregate 只等 preprocessing 一路。
4. 再追踪：若 image_encoder 先到达 aggregate（早于 preprocessing），`from_stage != 'preprocessing'`，函数返回 `None`，aggregate 暂挂——直到 preprocessing 到达才能算出等待集合。

**需要观察的现象**：`return None` 不是 bug，而是「我还需要等关键信息（preprocessing 那一路）到达，才能确定要等谁」的合法暂挂。

**预期结果**：你能用一句话解释「为什么 aggregate 必须等 preprocessing 到了才开始等待 encoder，而不是一开始就等三个」。

> 待本地验证：可在 `resolve_mm_aggregate_wait_sources` 入口加一行 `print(from_stage, ...)` 跟踪真实请求的上游到达顺序。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `next` 要静态列出全部三个去处，再用 `route_fn` 裁剪，而不是直接让 `route_fn` 返回结果、`next` 留空？

> **答案**：因为静态校验（[schema.py:375-381](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/config/schema.py#L375-L381)）需要 `next` 列出所有可能的目标来校验「这些 stage 真的存在」；同时 `project_payload` 的键也必须对齐 `next` 的目标。`next` 是「全集声明」，`route_fn` 是「运行时裁剪」，两者配合既保证配置期可校验、又保证运行时可裁剪。

**练习 2**：`merge_for_thinker` 末尾为什么要 `state.encoder_outs = {}`？

> **答案**：encoder 输出已经被消费、组装进 `thinker_inputs`。如果不清空，下一跳（thinker）会同时收到 `thinker_inputs` 和重复的 `encoder_outs`，相当于把多模态张量传了两遍，白白增加跨阶段传输开销。

---

### 4.3 thinker→talker 的 hidden state 流式与 partial-start

#### 4.3.1 概念说明

这是整条管线最精妙的一跳。talker_ar 是一个独立的小 AR 模型，它的输入不是「文本字符串」，而是 **thinker 最后一层（或指定层）的 hidden state**——也就是 thinker 对每个生成 token 的内部表示。talker 读着这些表示，同步产出语音 codec 码本。

如果等 thinker 把整段文本生成完，再把全部 hidden state 交给 talker，talker 才开始生成语音——那么语音的首音频时延（TTFA）就会包含完整的 thinker 生成时间，体验很差。所以 Qwen3-Omni 用**流式 + 提前启动（partial-start）**：

- **流式**：thinker 每生成一个 token，立刻把这个 token 的 hidden state 作为一条 `stream` 消息发给 talker_ar。
- **提前启动**：talker_ar 不等 thinker 结束，只要攒够少量 hidden state（`partial_start_min_chunks`）就开始解码、产出 codec、喂给 code2wav 合成音频。

这样 thinker 的「思考」与 talker 的「说话」**重叠执行**，大幅降低首音频时延。

#### 4.3.2 核心流程

talker_ar 收到的数据来自**三个不同的源、三种不同的边**，务必区分清楚：

| 来源 | 边类型 | 内容 | 作用 |
|------|--------|------|------|
| mm_aggregate | `next`（最终结果） | prompt + thinker_inputs（投影后） | **建立 talker 的请求上下文 / prefill**（提前提交） |
| thinker | `stream_to`（每步 chunk） | 单 token 的 hidden state | talker 边读边解码，产 codec |
| thinker | `stream_done_to_fn`（流结束） | 「thinker 结束」信号 | 通知 talker 没有更多 hidden state，可以收尾 |

第一条（mm_aggregate → talker_ar）是「提前提交」：注意 mm_aggregate 的 `next` 同时包含 thinker 和 talker_ar（[config.py:103](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L103)）。talker_ar 拿到的是 `project_mm_aggregate_to_talker_ar` 投影出的**轻量 payload**（prompt + thinker_inputs），用它先做 prefill、建好上下文。这样当 thinker 的 hidden state 开始流入时，talker 已经准备好，可以立即消费。

thinker 这边的配置（speech 模式）声明了全部三类边：

```python
def _thinker_stage(*, gpu, speech_enabled, process):
    return StageConfig(
        name="thinker",
        ...
        next="decode",
        stream_to=["talker_ar", "decode"] if speech_enabled else ["decode"],
        route_fn=...resolve_thinker_next_stages if speech_enabled else None,
        stream_done_to_fn=...resolve_thinker_stream_done_targets if speech_enabled else None,
        project_payload={"decode": ...project_thinker_to_decode},
    )
```

见 [config.py:130-152](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L130-L152)。关键点：

- `next="decode"`：thinker 的**最终结果只送文本终点 decode**，不送 talker（talker 的上下文由 mm_aggregate 提前建立）。
- `stream_to=["talker_ar", "decode"]`：每生成一个 token，hidden state 流给 talker_ar，token id 流给 decode（流式文本）。
- `stream_done_to_fn`：thinker 生成结束时，向 talker_ar 与 decode 各发一个「流结束」信号。

talker_ar 的终点是 code2wav（音频终点）：

```python
def _talker_stage(*, gpu, process, enable_partial_start):
    return StageConfig(
        name="talker_ar",
        ...
        next="code2wav",
        stream_to=["code2wav"],
        project_payload={"code2wav": ...project_talker_to_code2wav},
        can_accept_stream_before_payload=True,
    )
```

见 [config.py:165-197](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L165-L197)。`can_accept_stream_before_payload=True` 表示 talker 允许「stream chunk 先到、主 payload 后到」（回顾 u4-l4 的流式边与 `_pending_done` 兜底）。

> 注意 `project_talker_to_code2wav` 故意只发一个**空 latch**（`data={}`）：

```python
def project_talker_to_code2wav(payload):
    return StagePayload(request_id=payload.request_id, request=payload.request, data={})
```

见 [request_builders.py:164-170](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/request_builders.py#L164-L170)。注释写得很清楚：「code tensors arrive by stream」——code2wav 真正需要的 codec 张量是靠 talker 的 `stream_to` 流进来的，最终的 `next` payload 只是个「请求闩锁」，用来建立 code2wav 的请求上下文。这是「流式优先、终态兜底」的设计。

#### 4.3.3 源码精读

真正产生「每 token hidden state 流」的代码在 `make_thinker_stream_output_builder` 返回的 `_build_stream_output` 里。thinker 每完成一个 decode step，OmniScheduler 就调它一次，由它决定往哪些 target 发 `OutgoingMessage`：

```python
def _build_stream_output(request_id, req_data, req_output):
    req = getattr(req_data, "req", None)
    if req is not None and int(getattr(req, "is_chunked", 0) or 0) > 0:
        # chunked prefill 还在吃 prompt token 时，抑制 hidden state 流，
        # 避免把用户/参考文本的 prompt 状态伪装成第一个 assistant token 泄漏进 TTS。
        return []
    ...
    token_id = int(req_output.data)
    messages = []
    is_streaming = bool((stage_payload.request.params or {}).get("stream", False))
    if is_streaming:
        # 流式文本：把 token id 包成张量送给 decode
        messages.append(OutgoingMessage(request_id=request_id, type="stream",
            data=torch.tensor([token_id], dtype=torch.long), target="decode",
            metadata={"token_id": token_id}))

    if not should_generate_audio_output(stage_payload):
        return messages

    # 语音模式：额外把 hidden state 送给 talker 用于 codec 生成
    extra = req_output.extra
    if isinstance(extra, dict) and "hidden_states" in extra:
        embed, layer_hidden = _split_dual_layer_hidden(extra["hidden_states"])
        if embed is not None:
            metadata = {"token_id": token_id}
            if layer_hidden is not None:
                metadata["layer_hidden"] = layer_hidden
            messages.append(OutgoingMessage(request_id=request_id, type="stream",
                data=embed, target="talker_ar", metadata=metadata))
    return messages
```

见 [request_builders.py:862-928](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/request_builders.py#L862-L928)。读这段代码要抓住三点：

1. **chunked prefill 期间不流**：当 thinker 还在分段吃 prompt（`is_chunked > 0`）时，函数直接返回空列表，避免把 prompt 侧的状态误当成首个 assistant token 流给 talker（那会让 TTS 把用户输入「念」出来）。这是流式正确性的关键保护。
2. **token id 只在 `stream=True` 时送 decode**：非流式请求不需要逐 token 文本增量，所以跳过；但 hidden state 给 talker 是**无条件**的（语音总要生成）。
3. **hidden state 可能是双层**：`_split_dual_layer_hidden` 把 embed 层与某一层中间层拆开，`layer_hidden` 作为 metadata 附带，供 talker 条件生成。

「提前提交」的投影函数 `project_mm_aggregate_to_talker_ar` 把 thinker_inputs 精简后送给 talker（去掉 talker 用不到的 deepstack 多尺度视觉向量）：

```python
def project_mm_aggregate_to_talker_ar(payload):
    state = Qwen3OmniPipelineState.from_dict(payload.data)
    thinker_inputs = dict(state.thinker_inputs) if isinstance(state.thinker_inputs, dict) else {}
    model_inputs = thinker_inputs.get("model_inputs")
    if isinstance(model_inputs, dict):
        thinker_inputs["model_inputs"] = {
            k: v for k, v in model_inputs.items()
            if k not in _TALKER_UNUSED_MODEL_INPUT_KEYS   # 去掉 deepstack 等 talker 不用的键
        }
    projected = Qwen3OmniPipelineState(
        prompt=dict(state.prompt) if isinstance(state.prompt, dict) else None,
        thinker_inputs=thinker_inputs,
    )
    return _payload_with_state(payload, projected)
```

见 [request_builders.py:271-288](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/request_builders.py#L271-L288)。注意 `mm_aggregate` 的 stage 配置里那条注释直接点明了意图：「Route the merged payload to talker_ar so partial-start can fire」（[config.py:92-94](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L92-L94)）——partial-start 要先有 new_request 才能触发。

partial-start 的开关与门槛在 talker 工厂的 `factory_args` 里：

```python
factory_args={
    "talker_max_seq_len": 32768,
    "speech_enabled": True,
    "feedback_enabled": True,
    "enable_partial_start": enable_partial_start,
    "partial_start_min_chunks": 5,
}
```

见 [config.py:175-188](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L175-L188)。`partial_start_min_chunks=5` 表示 talker 至少要收到 5 个 hidden state chunk 才允许开始解码；模块顶部还定义了 `MIN_PARTIAL_START_CHUNKS = 3`（[config.py:15](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L15)）作为另一处下限。这个门槛是为了避免 talker 在太少的上下文上贸然起步，导致首音频质量差。

> 补充：`feedback_enabled=True` 让 talker 走「反馈式 AR」——它把自己上一步产出的 codec embedding 回填进下一步输入，形成闭环（回顾 u4-l3 的 FeedbackStrategy）。

#### 4.3.4 代码实践

**实践目标**：理解 thinker 的三类边分别由什么触发，并能在配置里找到它们。

**操作步骤**（源码阅读型实践）：

1. 在 [config.py:130-152](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L130-L152) 找到 `_thinker_stage`，填下面这张表：

   | 边 | 字段 | speech 模式的目标 | 何时触发 |
   |---|------|------------------|----------|
   | 最终结果 | `next`/`route_fn` | ? | thinker 算完整个请求 |
   | 流式 chunk | `stream_to` | ? | 每生成一个 token |
   | 流结束 | `stream_done_to_fn` | ? | thinker 生成终止 |

2. 在 [request_builders.py:81-103](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/request_builders.py#L81-L103) 读 `resolve_thinker_next_stages` 与 `resolve_thinker_stream_done_targets`，确认它们都按 `should_generate_audio_output` 分语音/文本两支。

**需要观察的现象**：

- `resolve_thinker_next_stages` 无论语音/文本都返回 `decode`（最终结果只送文本终点）。
- `resolve_thinker_stream_done_targets` 在语音模式返回 `[talker_ar, decode]`，文本模式只返回 `[decode]`。

**预期结果**：你能解释「为什么 thinker 的 `next` 不包含 talker_ar，但 `stream_done_to_fn` 却包含 talker_ar」——因为 talker 的上下文由 mm_aggregate 提前建立，thinker 只需在结束时通知 talker「没有更多 hidden state 了」。

> 待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`_build_stream_output` 在 `is_chunked > 0` 时为什么返回空列表？

> **答案**：chunked prefill 阶段 thinker 还在分段消化 prompt（用户输入 / 参考音频文本），此时若把 prompt 侧的 hidden state 流给 talker，会让 TTS 把 prompt 内容当成模型回答「念」出来，造成泄漏与错误。所以必须等到 prefill 完成、进入真正的 decode 生成后才开始流。

**练习 2**：为什么 `project_talker_to_code2wav` 发送的是空 `data={}`？

> **答案**：code2wav 真正需要的 codec 张量是 talker 通过 `stream_to` 逐 chunk 流进来的。最终的 `next` payload 只承担「建立 code2wav 请求上下文」的闩锁作用，不需要再带数据。这体现了「流式优先、终态兜底」的设计——真正的内容走流，终态只负责生命周期。

---

### 4.4 colocated 与 disaggregated 拓扑

#### 4.4.1 概念说明

同样是 8 阶段 speech 管线，Qwen3-Omni 提供两种部署形态：

- **disaggregated（分离式）**：thinker 与 talker 放在**不同 GPU**上（默认 `thinker_gpu=0, talker_gpu=1`）。两个 AR 引擎可以真正并行——thinker 在 GPU0 生成 token 的同时，talker 在 GPU1 消费 hidden state 生成语音。代价是需要 ≥2 张卡，且 hidden state 要跨卡传输。这种形态默认开启 `enable_partial_start=True`。
- **colocated（共置式）**：所有 GPU 阶段挤在**同一张卡**上（`thinker_gpu=0, talker_gpu=0`）。适合单卡部署（如一张 H100）。因为共享一张卡，多个阶段时分复用 GPU，partial-start 的并行收益消失，所以默认 `enable_partial_start=False`。此时关键是**显存预算划分**：thinker、talker、两个 encoder、code2wav 五个阶段同抢一张卡的显存，必须给每个阶段分配 `total_gpu_memory_fraction`。

注意「colocated 共置的是 GPU，不是进程」。即便 colocated，`_SPEECH_DEFAULT_PROCESSES` 仍给每个 stage 分配独立的进程名，所以仍是 8 个 OS 进程跑在同一张卡上（回顾 u3-l4：进程共置与 GPU 共置是正交两维）。

#### 4.4.2 核心流程

三个配置类的差异可以用一张表概括：

| 配置类 | stage 数 | thinker GPU | talker GPU | partial-start | 显存预算 |
|--------|----------|-------------|------------|---------------|----------|
| `Qwen3OmniPipelineConfig` | 6（text） | 0 | —（无 talker） | — | 单进程，不细分 |
| `Qwen3OmniSpeechPipelineConfig` | 8（speech） | 0 | 1 | 开 | 各 stage 独占各自卡的预算 |
| `Qwen3OmniSpeechColocatedPipelineConfig` | 8（speech） | 0 | 0 | 关 | **必须**为每个 GPU stage 显式分配 `total_gpu_memory_fraction` |

colocated 的显存预算求和必须落在单卡容量内。以 H100 bf16 配置为例，五个 GPU stage 的预算加起来约 0.94（≤1.0）：

\[
\underbrace{0.02}_{\text{image}} + \underbrace{0.02}_{\text{audio}} + \underbrace{0.78}_{\text{thinker}} + \underbrace{0.10}_{\text{talker}} + \underbrace{0.02}_{\text{code2wav}} = 0.94
\]

thinker 占绝大头（0.78），因为它是最重的 MoE 大模型；talker 其次（0.10）；其余三个各 0.02。

#### 4.4.3 源码精读

两个 speech 配置类的区别几乎只在默认参数上。`Qwen3OmniSpeechColocatedPipelineConfig` 直接继承 speech 版，只覆盖三处：

```python
class Qwen3OmniSpeechColocatedPipelineConfig(Qwen3OmniSpeechPipelineConfig):
    env_defaults: dict[str, str] = Field(
        default_factory=lambda: dict(_COLOCATED_STAGE_ENV_DEFAULTS)
    )
    stages: list[StageConfig] = Field(
        default_factory=lambda: _speech_stages(
            thinker_gpu=0,
            talker_gpu=0,                       # ← 与 thinker 同卡
            process_by_stage=_SPEECH_DEFAULT_PROCESSES,
            enable_partial_start=False,         # ← 关闭 partial-start
        )
    )
```

见 [config.py:362-373](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L362-L373)。而 disaggregated 版的默认是 `thinker_gpu=0, talker_gpu=1, enable_partial_start=True`（[config.py:342-348](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L342-L348)）。

colocated 额外的 `env_defaults` 限制每个进程的 OpenMP 线程数（`OMP_NUM_THREADS=8`），因为单卡要起 8 个 stage 进程，若每个都按全机 CPU 开线程池会严重争抢：

```python
_COLOCATED_STAGE_ENV_DEFAULTS = {
    **_DEEPGEMM_PRECOMPILE_ENV_DEFAULTS,
    "OMP_NUM_THREADS": "8",
    "TOKENIZERS_PARALLELISM": "false",
}
```

见 [config.py:28-32](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py#L28-L32)。

真正强制「单卡共置语义」的是模型专属的放置校验策略 `Qwen3OmniPlacementPolicy`。它首先要求 speech 管线**必须恰好是这 8 个 stage**，不多不少：

```python
def _validate_speech_topology(self, stage_map):
    names = set(stage_map)
    if names != _SPEECH_STAGE_SET:
        missing = sorted(_SPEECH_STAGE_SET - names)
        extra = sorted(names - _SPEECH_STAGE_SET)
        raise ValueError(
            "Qwen speech must use the eight configured stages; "
            f"missing={missing}, extra={extra}")
```

见 [placement.py:76-84](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/placement.py#L76-L84)。接着，**只有 colocated 配置类**才允许 thinker 与 talker 共享 GPU，否则报错：

```python
def validate(self, config, plan):
    ...
    if type(config).__name__ == _COLOCATED_CONFIG_CLASS:
        self._validate_colocated_qwen_parallelism(stage_map)
        self._validate_colocated_qwen_topology(plan)
        self._validate_colocated_qwen_runtime(stage_map)
        return
    ...
    if not set(thinker.gpu_ids).intersection(talker.gpu_ids):
        return   # thinker/talker 在不同卡，OK
    raise ValueError(
        "Qwen thinker and talker_ar may share a GPU only with "
        f"{_COLOCATED_CONFIG_CLASS}")
```

见 [placement.py:48-66](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/placement.py#L48-L66)。`_validate_colocated_qwen_topology` 进一步要求五个 GPU 阶段（image/audio encoder、thinker、talker_ar、code2wav）**全部落在同一张卡**上：

```python
def _validate_colocated_qwen_topology(self, plan):
    gpu_ids = set()
    for stage_name in sorted(_COLOCATED_BUDGET_STAGES):
        placement = plan.stages.get(stage_name)
        if placement is None or len(placement.gpu_ids) != 1:
            invalid.append(stage_name)
            continue
        gpu_ids.add(placement.gpu_ids[0])
    ...
    if len(gpu_ids) != 1:
        raise ValueError(
            "Qwen colocated speech requires image_encoder, audio_encoder, "
            "thinker, talker_ar, and code2wav to share one GPU")
```

见 [placement.py:86-104](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/placement.py#L86-L104)。最后 `_validate_colocated_qwen_runtime` 强制这五个 stage **必须**声明 `total_gpu_memory_fraction`，否则单卡显存无法切分：

```python
def _validate_colocated_qwen_runtime(self, stage_map):
    missing_budgets = [
        stage_name for stage_name in sorted(_COLOCATED_BUDGET_STAGES)
        if stage_map[stage_name].runtime.resources.total_gpu_memory_fraction is None
    ]
    if missing_budgets:
        raise ValueError(
            "Qwen colocated speech requires total_gpu_memory_fraction for "
            f"{missing_budgets}")
```

见 [placement.py:106-119](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/placement.py#L106-L119)。这正好对应 H100 bf16 YAML 里那五个 `total_gpu_memory_fraction` 覆盖：

```yaml
config_cls: Qwen3OmniSpeechColocatedPipelineConfig
stage_overrides:
  image_encoder:  {runtime: {resources: {total_gpu_memory_fraction: 0.02}}}
  audio_encoder:  {runtime: {resources: {total_gpu_memory_fraction: 0.02}}}
  thinker:        {runtime: {resources: {total_gpu_memory_fraction: 0.78}}}
  talker_ar:      {runtime: {resources: {total_gpu_memory_fraction: 0.10}}}
  code2wav:       {runtime: {resources: {total_gpu_memory_fraction: 0.02}}}
```

见 [examples/configs/qwen3_omni_colocated_h100_bf16.yaml](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/examples/configs/qwen3_omni_colocated_h100_bf16.yaml)。注意 preprocessing / mm_aggregate / decode **没有**显存预算——它们是 CPU 阶段（`StageConfig` 里没设 `gpu`），不占 GPU 显存。

> 这条预算如何在 AR 阶段落地？`stages.py` 的 `create_sglang_thinker_executor_from_config` 会把 `total_gpu_memory_fraction` 翻译成 SGLang 的 `mem_fraction_static`（扣除 `encoder_mem_reserve` 后），见 [stages.py:935-1047](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/stages.py#L935-L1047) 中 `_apply_colocated_ar_memory_contract` 的逻辑——这把 u1-l5 讲的「`total_gpu_memory_fraction` 是放置资源意图」落到了具体的 SGLang server arg 上。

#### 4.4.4 代码实践

**实践目标**：亲手触发 colocated 的放置校验，理解它为什么必须强制五个 stage 同卡 + 必须有显存预算。

**操作步骤**（示例代码，非项目原有）：

```python
# 示例代码：构造一个「缺预算」的 colocated 配置，观察放置校验报错
from sglang_omni.models.qwen3_omni.config import Qwen3OmniSpeechColocatedPipelineConfig

cfg = Qwen3OmniSpeechColocatedPipelineConfig(model_path="Qwen/Qwen3-Omni-30B-A3B-Instruct")
# 注意：这里没有用 YAML 覆盖 total_gpu_memory_fraction，五个 GPU stage 的预算都是 None
# 真实启动时，放置规划会调用 Qwen3OmniPlacementPolicy.validate 抛出
# "Qwen colocated speech requires total_gpu_memory_fraction for [...]"
```

**需要观察的现象**：若不通过 YAML 给五个 GPU stage 提供 `total_gpu_memory_fraction`，放置阶段会报错并列出缺失预算的 stage 名。

**预期结果**：报错信息恰好列出 `['code2wav', 'image_encoder', 'talker_ar', 'thinker', 'audio_encoder']` 五项（排序后），与 `_COLOCATED_BUDGET_STAGES` 一致。这验证了「colocated 必须显式切分显存」这条硬约束。

> 待本地验证：完整触发需要跑放置规划（涉及 `StagePlacementPlan`），若只想验证 stage 列表，可直接打印 `cfg.stages` 查看 GPU 分配。

#### 4.4.5 小练习与答案

**练习 1**：为什么 disaggregated 默认开 `enable_partial_start`，而 colocated 默认关？

> **答案**：disaggregated 下 thinker 与 talker 在不同 GPU，可以真正并行——partial-start 让 talker 在 thinker 还在生成时就起步，能显著降低首音频时延。colocated 下所有 GPU 阶段共享一张卡，时分复用同一块 GPU，partial-start 带来的并行收益消失，反而增加复杂度，所以默认关闭。

**练习 2**：colocated 配置里 preprocessing / mm_aggregate / decode 为什么不需要 `total_gpu_memory_fraction`？

> **答案**：因为它们的 `StageConfig` 没有设置 `gpu` 字段，是 CPU 阶段，不分配 GPU 显存，自然不需要显存预算。只有 image/audio encoder、thinker、talker_ar、code2wav 这五个设了 `gpu=` 的阶段才需要切分单卡显存。

---

## 5. 综合实践

**任务**：对照 `config.py` 与 `stages.py`，手画 Qwen3-Omni **speech 管线**的完整 stage DAG，并在每条边上标注边的类型（`next` / `stream_to` / `stream_done`）与触发条件。

**要求**：

1. 列出全部 8 个 stage，标出哪些是 terminal。
2. 至少画出以下边并标注类型：
   - preprocessing → image_encoder / audio_encoder / mm_aggregate（注意请求感知扇出）
   - mm_aggregate → thinker / talker_ar（注意「提前提交」与 `disable_direct_cuda_ipc_payload`）
   - thinker → talker_ar（hidden state 流）、thinker → decode（token id 流 + 最终结果）、thinker 的 stream_done
   - talker_ar → code2wav（codec 流 + 最终结果）
3. 用一段话说明：**talker_ar 从哪三个源、经哪三种边拿到数据**，以及 partial-start 为什么能降低首音频时延。
4. 进阶：把同一段 DAG 分别套到 `Qwen3OmniSpeechPipelineConfig`（disaggregated）与 `Qwen3OmniSpeechColocatedPipelineConfig`（colocated）上，标注两者的 GPU 分配与 partial-start 差异。

**参考答案要点**：

- 边的类型应与本讲 4.1.2 / 4.3.2 的表格一致。
- talker_ar 三源：mm_aggregate（`next`，建 prefill 上下文）、thinker（`stream_to`，hidden state）、thinker（`stream_done_to_fn`，结束信号）。
- partial-start 降时延的原因：talker 不必等 thinker 全部生成完，攒够少量 hidden state 即开始解码产 codec，使「思考」与「说话」重叠。
- disaggregated：thinker GPU0、talker GPU1、partial-start 开；colocated：五 GPU 阶段同卡、partial-start 关、需显存预算切分。

## 6. 本讲小结

- Qwen3-Omni 有两套 stage 列表：text（6 阶段，单 terminal `decode`）与 speech（8 阶段，双 terminal `decode` + `code2wav`）。三个 `PipelineConfig` 子类（text / speech / speech-colocated）复用同一套 `_speech_stages` 工厂，仅靠默认参数区分形态。
- 管线边分三类：`next`/`route_fn`（最终结果）、`stream_to`（每步流式 chunk）、`stream_done_to_fn`（流结束信号）。区分这三类是读懂 DAG 的关键。
- 路由是「静态全集 + 请求感知子集」：preprocessing 用 `route_fn` 按请求内容扇出（纯文本跳过 encoder），mm_aggregate 用 `wait_for_fn` 按请求内容确定 fan-in 等待集合，`wait_for_fn` 返回 `None` 表示「暂挂等关键上游」。
- thinker→talker 是「双 AR + 流式」关系：thinker 每生成一个 token，把 hidden state 流给 talker_ar；mm_aggregate 还会**提前**把 prompt + thinker_inputs 投影给 talker_ar 建好 prefill 上下文，使 partial-start 成为可能。
- partial-start 让 talker 攒够少量 hidden state（`partial_start_min_chunks`）就开始解码，重叠 thinker 的思考与 talker 的说话，降低首音频时延；且 chunked prefill 期间抑制流，避免泄漏 prompt。
- colocated 与 disaggregated 的差异落在 GPU 放置、partial-start 开关与显存预算上；colocated 由 `Qwen3OmniPlacementPolicy` 强制五个 GPU 阶段同卡且必须声明 `total_gpu_memory_fraction`。

## 7. 下一步学习建议

- **往调度器内部走**：本讲的 AR 阶段（thinker / talker_ar）都返回 `OmniScheduler`。想看清「每 token 的 hidden state 到底怎么从 ModelRunner 流到 outbox」，请读 u4-l2（OmniScheduler）与 u4-l3（ModelRunner 前向路径），重点看 talker 的反馈式 AR（`feedback_enabled`）。
- **往流式 vocoder 走**：本讲的 `code2wav` 是音频终点，它如何边收 codec 边合成波形、首音频何时触发，请读 u4-l4（流式调度器与流式 vocoder）。
- **往传输层走**：thinker→talker 的 hidden state 跨 GPU/进程是怎么搬的？`disable_direct_cuda_ipc_payload` 又意味着什么？请读 u3-l3（relay 数据平面）与 u6-l1（通信路由与传输选择）。
- **横向对比模型家族**：把本讲的 Qwen3-Omni 与 u5-l3 的 TTS 三阶段接入流程对比，理解「为什么 omni 双 AR 管线比纯 TTS 三阶段复杂」。
