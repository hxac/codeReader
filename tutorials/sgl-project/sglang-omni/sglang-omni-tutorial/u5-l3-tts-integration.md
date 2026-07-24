# TTS 模型接入流程

> 本讲承接 u5-l1（模型注册表与能力声明）与 u5-l2（Qwen3-Omni 端到端管线）。
> u5-l1 讲清了「框架如何凭 `architecture` 找到一个模型的 `PipelineConfig`」，
> u5-l2 讲清了「omni 模型如何把多阶段串成 DAG」。本讲聚焦**纯 TTS 模型**——
> 只有「文本 → 语音」单向、没有多模态理解输入——如何按一套固定模板接入 SGLang-Omni。

## 1. 本讲目标

读完本讲，你应当能够：

1. 说出 TTS 管线的**三阶段最小形状**（preprocessing → tts_engine → vocoder），并能对照真实源码指出每个阶段由哪类 scheduler 驱动。
2. 按官方 `tts_model_integration` 文档的步骤清单，为一个**假想 TTS 模型**列出需要新建与修改的文件。
3. 理解**请求边界陷阱**：为什么 HTTP 端点塞进来的采样默认值会「静默覆盖」模型自己的默认值，以及 `explicit_generation_params` 如何区分「用户显式传了」与「端点填的」。
4. 掌握 `payload_types` / `request_builders` / `vocoder` 三个模块的职责，理解它们如何用**类型化 dataclass**（而非自由 dict）在阶段间传状态。
5. 理解**张量设备/dtype 纪律**与「radix cache key 必须由 embedding 内容派生」这条硬规则，避免「一个用户的语音泄漏到另一个请求」。
6. 解释 **abort 清理为什么必须挂在多个 scheduler 上**（preprocessing、tts_engine 各一份）。

## 2. 前置知识

本讲默认你已经读过：

- **u5-l1**：`PIPELINE_CONFIG_REGISTRY` 如何凭 `architecture` ClassVar 匹配 HF config、`EntryClass` 如何成为模型包的入口。
- **u4-l1 / u4-l2**：调度器统一契约 `inbox / outbox / start / stop / abort`，以及 `OmniScheduler` 如何「组合而非继承」复用 SGLang 的 prefill/decode/KV cache。
- **u2-l5**：`PipelineConfig` / `StageConfig` 的字段含义（`factory` / `next` / `terminal` / `gpu` / `process`）。

几个本讲会用到的术语先对齐：

- **AR（自回归）**：模型一步步生成下一个 token。TTS 的 `tts_engine` 阶段就是 AR，它生成的是「语音 codec 码本」（一串离散整数），不是文本。
- **codec / 码本**：把连续语音压缩成一串离散码（类似文本的 token id）。vocoder 再把码本解压回波形。
- **vocoder**：「码 → 波形」的解码器，通常是无状态、可批处理的小模型。
- **radix cache（基数缓存）**：SGLang 的前缀 KV 缓存复用机制。相同的前缀 token id 序列会命中同一段 KV，避免重复 prefill。
- **dataclass**：Python 的 `@dataclass`，自动生成构造、比较等方法的「带类型注解的普通对象」。本讲里阶段间传递的状态都是 dataclass。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [docs/developer_reference/tts_model_integration.md](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/tts_model_integration.md) | 官方接入手册：8 步清单、三阶段形状、请求边界陷阱、abort 纪律、张量纪律。本讲的「方法论骨架」。 |
| [sglang_omni/models/qwen3_tts/config.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_tts/config.py) | Qwen3-TTS 的 `PipelineConfig` 子类与三个 `StageConfig`，是三阶段形状的真实落地。 |
| [sglang_omni/models/qwen3_tts/stages.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_tts/stages.py) | 三个 stage 的工厂函数：preprocessing、tts_engine（委托给 `engine_builder`）、vocoder。 |
| [sglang_omni/models/qwen3_tts/request_builders.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_tts/request_builders.py) | 请求边界核心：输入归一化、端点默认值过滤、AR 请求构造、abort 清理、embedding cache key。 |
| [sglang_omni/models/qwen3_tts/payload_types.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_tts/payload_types.py) | 阶段间类型化状态 `Qwen3TTSState`，用 `wire(...)` 声明序列化。 |
| [sglang_omni/models/qwen3_tts/engine_builder.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_tts/engine_builder.py) | AR 引擎构建器 `Qwen3TtsEngineBuilder`，组装 SGLang 基础设施并产出 `OmniScheduler`。 |
| [sglang_omni/scheduling/engine_factory.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/engine_factory.py) | 所有 TTS AR 引擎共享的模板基类 `TtsEngineBuilder`，固化 `build()` 的 15 步装配流水线。 |
| [sglang_omni/scheduling/vocoder_base.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/vocoder_base.py) | 批处理 vocoder 抽象 `BatchVocoderBase`，把「单条 / 批」两条路径封进 `SimpleScheduler`。 |
| [sglang_omni/model_runner/sglang_model_runner.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/sglang_model_runner.py) | `_register_omni_model`：把每个模型类手动注册进 SGLang `ModelRegistry`。 |
| [sglang_omni/serve/speech_service.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/speech_service.py) | `/v1/audio/speech` 端点：校验 OpenAI payload、填采样默认值、产出 `explicit_generation_params`。 |
| [tests/unit_test/qwen3_tts/test_pipeline.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/tests/unit_test/qwen3_tts/test_pipeline.py) | GPU-free 单测：请求边界、采样默认值保留、abort 三路径、设备/dtype 不变量。 |

---

## 4. 核心概念与源码讲解

### 4.1 TTS 三阶段管线与接入步骤清单

#### 4.1.1 概念说明

TTS（Text-to-Speech）任务的特点是**单向、异构**：输入是一段文本（可能带一段「参考音频」用于声音克隆），输出是语音波形。一次生成被性质迥异的三段工作接力完成：

1. **preprocessing（预处理）**——校验请求、拉取并 tokenize 参考音频、构造 prompt。这一步 CPU/轻量 GPU 活多、且**每个请求只做一次**，必须挪出 AR 循环，否则会把 AR 引擎拖慢。
2. **tts_engine（AR 引擎）**——自回归地生成语音 codec 码本。这一步要 KV cache、要批处理、要能 abort、要请求限额，所以用 `OmniScheduler` 复用 SGLang 的全部 AR 能力。
3. **vocoder（声码器）**——把码本解码成波形。通常是无状态、可批处理的小模型，用 `SimpleScheduler` + `batch_compute_fn` 即可；若要「生成没结束就开始吐音频」，才上流式调度器。

官方手册的原话（[tts_model_integration.md:L52-L66](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/tts_model_integration.md#L52-L66)）明确：**最小可用管线就是这三段**，Qwen3-TTS、Voxtral-TTS、S2-Pro 都保持这个形状。

> 两个值得知道的变体（不是本讲重点，但要知道它们存在）：
> - 在 preprocessing 与 tts_engine 之间**插入一个 `audio_encoder` 阶段**，当你需要在 AR 设备上对参考音频跑一次重编码器时（Higgs TTS 这么做）。
> - 想让引擎**逐 chunk 流式喂给 vocoder**，就在引擎的 `StageConfig` 上设 `stream_to=["vocoder"]`、在 vocoder 上设 `can_accept_stream_before_payload=True`（S2-Pro 是参考实现，见 [fishaudio_s2_pro/config.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/fishaudio_s2_pro/config.py#L42-L50)）。

#### 4.1.2 核心流程：八步接入清单

官方手册把接入一个新 TTS 模型拆成 8 步（[tts_model_integration.md:L9-L30](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/tts_model_integration.md#L9-L30)），可视为本模块的「施工总纲」：

```text
1. 选定 HF 架构字符串（config.json::architectures[0]）
2. 在 sglang_omni/models/<name>/ 建 __init__.py + config.py
   └─ 给 PipelineConfig 子类设 architecture，模块级导出 EntryClass
3. 若 HF config 不在 transformers 自带表里
   └─ import 时调 AutoConfig.register("<model_type>", <Config>)
4. 在 sglang_model.py 写 SGLang 模型类，并在
   _register_omni_model 加一行，让 SGLang 能按架构解析
5. 在 stages.py 写三个 stage 工厂；AR 工厂经
   build_sglang_server_args + create_sglang_infrastructure 产出 OmniScheduler
6. 写 request_builders.py 与 payload_types.py；
   └─ 在所有碰共享状态的 scheduler 上挂 abort 清理
7. 加 examples/configs/<name>.yaml，并在 docs/basic_usage/tts.md 列出
8. 补 GPU-free 单测（请求边界 / 调度器请求数据 / 阶段局部行为）
```

#### 4.1.3 源码精读：Qwen3-TTS 的三阶段声明

Qwen3-TTS 的 `config.py` 把三阶段形状**声明式**地写死在一个 `PipelineConfig` 子类里（[qwen3_tts/config.py:L20-L54](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_tts/config.py#L20-L54)）：

```python
class Qwen3TTSPipelineConfig(PipelineConfig):
    """3-stage Qwen3-TTS Base pipeline: preprocessing -> engine -> vocoder."""

    architecture: ClassVar[str] = "Qwen3TTSForConditionalGeneration"
    requires_model_capabilities: ClassVar[bool] = True

    model_path: str
    stages: list[StageConfig] = [
        StageConfig(
            name="preprocessing",
            process="pipeline",
            factory=f"{_PKG}.stages.create_preprocessing_executor",
            next="tts_engine",
        ),
        StageConfig(
            name="tts_engine",
            process="pipeline",
            factory=f"{_PKG}.stages.create_sglang_tts_engine_executor",
            factory_args={"dtype": "bfloat16"},
            gpu=0,
            next="vocoder",
        ),
        StageConfig(
            name="vocoder",
            process="pipeline",
            factory=f"{_PKG}.stages.create_vocoder_executor",
            factory_args={"dtype": "bfloat16"},
            gpu=0,
            terminal=True,
        ),
    ]
```

要点逐条对应接入清单：

- `architecture = "Qwen3TTSForConditionalGeneration"`（[L23](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_tts/config.py#L23)）——这就是清单第 1、2 步的「架构匹配键」，u5-l1 讲过，注册表凭它与 HF `config.json` 比对。
- 三个 `StageConfig` 的 `name` 正好是 `preprocessing / tts_engine / vocoder`，`factory` 全部指向同包 `stages` 模块里的工厂函数（清单第 5 步）。
- `next="tts_engine"`、`next="vocoder"` 串成线性链；只有 `vocoder` 设了 `terminal=True`（u2-l5 讲过终态）。
- 三个 stage 共用 `process="pipeline"`——意味着它们**共进程**（u3-l4 讲过进程拓扑），`tts_engine` 与 `vocoder` 都标 `gpu=0` 共卡。
- 模块末尾的 `EntryClass = Qwen3TTSPipelineConfig`（[L78](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_tts/config.py#L78)）是清单第 2 步要求的入口导出，缺失它注册表会 `AssertionError`（u5-l1）。

三个工厂函数在 [stages.py:L126-L234](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_tts/stages.py#L126-L234)。注意它们各自返回的 scheduler 类型正好对应三阶段的职责分工：

```python
def create_preprocessing_executor(model_path: str) -> SimpleScheduler:      # 非AR，SimpleScheduler
    return SimpleScheduler(preprocess_qwen3_tts_payload,
                           abort_callback=cleanup_prepared_qwen3_tts_request)

def create_sglang_tts_engine_executor(...) -> Any:                          # AR，委托给 builder
    return Qwen3TtsEngineBuilder(...).build(...)

def create_vocoder_executor(...) -> SimpleScheduler:                        # 非AR批处理，SimpleScheduler
    ...
    return _Qwen3TTSVocoder(tokenizer).build_scheduler(...)
```

`preprocessing` 与 `vocoder` 都用 `SimpleScheduler`（非 AR），`tts_engine` 走 `Qwen3TtsEngineBuilder.build()` 返回 `OmniScheduler`（AR）。这与官方手册「按职责挑 scheduler」的指导（[tts_model_integration.md:L152-L159](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/tts_model_integration.md#L152-L159)）完全一致。

#### 4.1.4 代码实践：画出 Qwen3-TTS 的三阶段链

1. **实践目标**：把声明式配置读成一张可视的管线图。
2. **操作步骤**：
   - 打开 [qwen3_tts/config.py:L31-L54](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_tts/config.py#L31-L54)，对每个 `StageConfig` 抄下 `name / factory / next / terminal / gpu`。
   - 打开 [examples/configs/qwen3_tts_1_7b.yaml](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/examples/configs/qwen3_tts_1_7b.yaml)，对照 u1-l5 讲过的「紧凑覆盖文件」结构。
3. **需要观察的现象**：yaml 只有 `config_cls` 与 `model_path` 两行，没有任何 stage 定义。
4. **预期结果**：手绘图应是 `preprocessing → tts_engine → vocoder(terminal)`，三者同 `process="pipeline"` 同 `gpu=0`。yaml 里没写 stage，说明拓扑全部继承自 `Qwen3TTSPipelineConfig` 默认值（u1-l5 的深度合并）。
5. 待本地验证：若你装好环境，可执行 `sgl-omni config export --model-path Qwen/Qwen3-TTS-12Hz-1.7B-Base`，观察导出的完整 YAML 是否补齐了三个 stage。

#### 4.1.5 小练习与答案

**练习 1**：若你想让 vocoder 在生成结束前就逐 chunk 吐音频，应该在 config.py 里改哪两个字段？参考谁？

> 答案：在 `tts_engine` 的 `StageConfig` 上加 `stream_to=["vocoder"]`，在 `vocoder` 的 `StageConfig` 上加 `can_accept_stream_before_payload=True`。参考实现是 S2-Pro（[fishaudio_s2_pro/config.py:L42-L50](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/fishaudio_s2_pro/config.py#L42-L50)）。

**练习 2**：为什么 `preprocessing` 阶段不直接用 `OmniScheduler`？

> 答案：preprocessing 是「每个请求做一次」的 CPU/轻量 GPU 活，没有 KV cache、不需要批 AR、不需要请求限额，用 `OmniScheduler` 是杀鸡用牛刀且会拖慢。`SimpleScheduler` 的「来一个算一个」循环（u4-l1）正好匹配。

---

### 4.2 request_builder：请求转换与边界纪律

#### 4.2.1 概念说明

`request_builder` 是 **HTTP 端点与 AR 调度器之间的翻译层**。它的职责是把外部 `GenerateRequest` 翻译成 AR scheduler 真正要消费的类型化对象（官方手册 [tts_model_integration.md:L112-L137](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/tts_model_integration.md#L112-L137)）。这一层有两个最容易踩的坑：

1. **端点默认值会静默覆盖模型默认值**：HTTP 层会给采样参数填一套默认值（当前是 S2-Pro 的值）。对其它模型来说，这套值看起来跟「用户明确要求」一模一样，于是模型自己的默认值被无声顶掉。
2. **输入是异构的**：TTS 客户端用各种名字传文本（`input` / `text` / chat 结构）、用各种形状传参考音频（`ref_audio` + `ref_text` 或 `references[]` 列表）。必须在 builder 里归一化、校验，且把这套脏活**封在 AR 阶段之外**——否则一个坏请求要等碰到 GPU 才失败。

#### 4.2.2 核心流程：从 payload 到 AR 请求

Qwen3-TTS 的请求转换分两步走（[request_builders.py:L923-L1040](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_tts/request_builders.py#L923-L1040)）：

```text
[preprocessing 阶段]                       [tts_engine 阶段]
  build_qwen3_tts_state(payload)             build_sglang_qwen3_tts_request(payload)
   ├─ normalize_qwen3_tts_inputs    ──►        ├─ pop_prepared_qwen3_tts_request   # 取回重张量
   │   (text / references 归一化)              │     (凭 _QWEN3_TTS_PREPARED_MARKER)
   ├─ normalize_qwen3_tts_task_type            ├─ 构造 SamplingParams
   │   (Base / CustomVoice / VoiceDesign)      ├─ 构造 SGLang Req
   ├─ build_generation_kwargs  ◄── 关键        ├─ 装进 Qwen3TTSSGLangRequestData
   │   (过滤端点默认值)                        └─ 返回给 OmniScheduler
   └─ 重张量(tokenize/embed)存进 _PREPARED_REQUESTS
```

注意一个关键设计：**重张量（tokenize、参考音频 embedding）在 preprocessing 阶段就算好并存进一个进程级字典 `_PREPARED_REQUESTS`**，AR 阶段只凭 payload 里的一个 marker（`_qwen3_tts_prepared_request`）把它取回（[request_builders.py:L177-L192](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_tts/request_builders.py#L177-L192)）。这样 AR 循环里完全不碰 tokenize，避免拖慢解码。

#### 4.2.3 源码精读：端点默认值陷阱与 explicit_generation_params

先看「病根」：HTTP 端点无条件填了一套采样默认值（[speech_service.py:L782-L798](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/speech_service.py#L782-L798)）：

```python
def _build_sampling_params(request: CreateSpeechRequest) -> SamplingParams:
    sampling = SamplingParams(
        temperature=0.8, top_p=0.8, top_k=30, repetition_penalty=1.1   # ← S2-Pro 的默认值
    )
    ...
```

对一个 Qwen3-TTS 请求，用户什么采样参数都没传，端点也会塞进 `temperature=0.8`。如果没有防御，AR 阶段会以为「用户要 0.8」，于是模型本该用的 `0.9` 被顶掉。

**解法是两段式**。第一段，端点用 Pydantic 的 `model_fields_set` 记下「用户真正传了哪些字段」，塞进 `explicit_generation_params`（[speech_service.py:L722-L734](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/speech_service.py#L722-L734)）：

```python
def _explicit_generation_params(request: CreateSpeechRequest) -> list[str]:
    return sorted(
        field for field in ("max_new_tokens", "temperature", "top_p",
                            "top_k", "repetition_penalty", "seed")
        if field in request.model_fields_set          # ← 只含「用户显式传的」
    )
```

> `model_fields_set` 是 Pydantic 的特性：它记录「实例化时**实际被传入**的字段」，与「字段最终取了默认值」是两回事。u2-l2 讲过这套 `explicit_generation_params` 机制在 chat 端也用。

第二段，模型自己的 `build_generation_kwargs` 用一个「隐式默认值集合」做反向过滤（[request_builders.py:L436-L466](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_tts/request_builders.py#L436-L466)）：

```python
_IMPLICIT_SAMPLING_DEFAULTS = {     # request_builders.py:L58-L63
    "temperature": {1.0, 0.8},      # 1.0=模型默认, 0.8=端点默认
    "top_p": {1.0, 0.8},
    "top_k": {-1, 30},
    "repetition_penalty": {1.0, 1.1},
}

def build_generation_kwargs(params, *, tts_params):
    explicit_fields = set(tts_params.get("explicit_generation_params") or [])
    selected_fields = set()
    for field in _GENERATION_FIELDS:
        value = params.get(field)
        if value is None:
            continue
        # 关键：若该字段未被显式声明，且取值恰是「默认值之一」→ 视为未设置，丢弃
        if field in _IMPLICIT_SAMPLING_DEFAULTS and field not in explicit_fields:
            if value in _IMPLICIT_SAMPLING_DEFAULTS[field]:
                continue
        selected_fields.add(field)
    ...
```

逻辑是：**只要用户没在 `explicit_generation_params` 里声明该字段，且它的取值恰好落在 `{模型默认, 端点默认}` 这个集合里，就当作「没传」丢掉**。于是 AR 阶段会回退到自己的默认（`build_sglang_qwen3_tts_request` 里 `temperature=0.9`，[L972](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_tts/request_builders.py#L972)）。两个值都过滤掉是为了同时兜住「端点填的 0.8」和「万一某处填了模型默认 1.0」两种情况。

再看**异构输入归一化**（[request_builders.py:L306-L318](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_tts/request_builders.py#L306-L318)）：

```python
def normalize_qwen3_tts_inputs(inputs: Any) -> tuple[str, list[dict[str, Any]]]:
    if isinstance(inputs, str):
        return inputs, []
    if isinstance(inputs, dict):
        text = inputs.get("text", inputs.get("input", ""))     # 文本两种名字都认
        references = inputs.get("references") or []
        ...
        return str(text), normalized_references
    ...
```

而**必填校验**也在这一层，比如 Base 任务必须有参考音频（[L243-L247](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_tts/request_builders.py#L243-L247)）、VoiceDesign 必须有 instructions（[L272-L273](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_tts/request_builders.py#L272-L273)）。坏请求在这里就被 `ValueError` 拦下，**根本到不了 GPU**。

#### 4.2.4 代码实践：阅读采样默认值保留的单测

1. **实践目标**：用真实测试断言验证「端点默认值会被丢弃」。
2. **操作步骤**：阅读三个测试（[test_pipeline.py:L262-L322](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/tests/unit_test/qwen3_tts/test_pipeline.py#L262-L322)）：
   - `test_qwen3_tts_maps_references_and_keeps_upstream_sampling_defaults`
   - `test_qwen3_tts_preserves_explicit_default_like_sampling_values`
   - `test_qwen3_tts_ignores_client_sampling_defaults`
3. **需要观察的现象**：第二个测试即使传了一个「值恰好等于默认」的参数，只要它出现在 `explicit_generation_params` 里，就会被**保留**；第三个测试不声明时，同样的值会被**丢弃**。
4. **预期结果**：能用自己的话讲清「显式声明」与「取值是否等于默认」这两个条件如何 AND 在一起决定一个采样参数去留。
5. 待本地验证：装好依赖后执行 `pytest tests/unit_test/qwen3_tts/test_pipeline.py::test_qwen3_tts_ignores_client_sampling_defaults -q` 应通过（GPU-free）。

#### 4.2.5 小练习与答案

**练习 1**：假设端点把 `top_k` 的默认值从 `30` 改成 `15`。Qwen3-TTS 这边需要改什么？

> 答案：需要把 `_IMPLICIT_SAMPLING_DEFAULTS["top_k"]`（[request_builders.py:L61](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_tts/request_builders.py#L61)）里的端点默认值同步成 `15`（集合变成 `{-1, 15}`），否则用户没传 `top_k` 时，端点塞来的 `15` 会被当成「用户显式要 15」而保留，悄悄改变模型行为。这正是手册说的「端点默认值静默覆盖」陷阱。

**练习 2**：为什么 request_builder 必须返回类型化 dataclass（`Qwen3TTSSGLangRequestData`），不能返回一个 dict？

> 答案：AR 阶段在**每一步 decode** 都要读这些字段（参考码本、prompt embedding、采样种子等）。类型化 dataclass 给出字段名与类型约束，IDE/测试能查；自由 dict 拼写错误要等运行时才炸，且无法表达设备/dtype 不变量。手册 [tts_model_integration.md:L135-L137](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/tts_model_integration.md#L135-L137) 明确要求传 typed dataclass。

---

### 4.3 payload_types：阶段间类型化状态

#### 4.3.1 概念说明

`payload_types.py` 定义**在阶段之间流动的「请求状态」**。它回答一个问题：preprocessing 算完之后，怎么把「文本、任务类型、参考音频、生成的码本」这些字段干净地交给下一个阶段？

答案是用一个 dataclass，配合框架提供的 `wire(...)` 声明每个字段如何序列化进 `StagePayload.data`（一个会被 msgpack 跨进程搬运的 dict）。官方手册把这种类型化状态称为 payload types（[tts_model_integration.md:L40-L41](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/tts_model_integration.md#L40-L41)，[L135-L137](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_tts/request_builders.py#L135-L137)）。

#### 4.3.2 核心流程：wire 与 DeclarativeStateBase

`DeclarativeStateBase` 是所有管线状态的基类（[pipeline_state.py:L203-L223](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/pipeline_state.py#L203-L223)）。它的 `to_dict()` 不靠手写，而是**遍历字段、读 `wire(...)` 元数据**自动生成。`wire()` 本身只是 `dataclasses.field` 的薄包装（[pipeline_state.py:L139-L158](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/pipeline_state.py#L139-L158)）：

```text
wire(default, *, default_factory, emit, codec)
  ├─ emit：何时输出该字段
  │    ├─ "always"（默认，非 None 字段）
  │    ├─ "not_none"（None 默认的字段只在非 None 时输出）
  │    └─ "truthy"（只在真值时输出，省带宽）
  └─ codec：如何编码值
       ├─ "raw"（原样）
       ├─ "int"/"str"/"bool"/"str_or"/"dict"
       └─ "tensor_list"（张量列表专用）
```

#### 4.3.3 源码精读：Qwen3TTSState

Qwen3-TTS 的请求状态定义在 [payload_types.py:L12-L35](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_tts/payload_types.py#L12-L35)：

```python
@dataclass
class Qwen3TTSState(DeclarativeStateBase):
    """Per-request state for Qwen3-TTS generation."""

    sample_rate: int = wire(24000, codec="int")
    text: str = wire("", codec="str")
    task_type: str = wire("Base", codec="str_or")
    task_type_explicit: bool = wire(False, codec="bool")
    language: str = wire("auto", codec="str_or")
    voice: str | None = None                  # None 默认 → 自动 not_none
    instructions: str | None = None
    ref_audio: Any | None = None
    ref_text: str | None = None
    ...
    generation_kwargs: dict[str, Any] = wire(default_factory=dict, codec="dict")
    seed: int | None = None
    audio_codes: Any | None = wire(None, codec="tensor_list")     # 张量列表专用 codec
    ref_code_len: int = wire(0, emit="truthy", codec="int")      # 只在 >0 时输出
    audio_samples: Any | None = wire(None, codec="tensor_list")
```

几个值得注意的设计：

- `text`、`task_type`、`language` 用 `str_or` codec——允许「空字符串表示未设置」，区别于 `str`（[pipeline_state.py:L139-L158](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/pipeline_state.py#L139-L158) 注释里说 codec 集合由 `_CODECS` 限定）。
- `voice`、`ref_audio` 等用裸字段（没写 `wire`），`DeclarativeStateBase` 默认把它们当 `always-emitted raw`，但**默认值是 None 的字段自动 `not_none`**（[pipeline_state.py:L173-L179](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/pipeline_state.py#L173-L179)）——没有参考音频的请求就不会把 `ref_audio: null` 塞进 payload。
- `ref_code_len` 用 `emit="truthy"`——`ref_code_len=0` 时根本不写进 payload，省一次跨进程传输。
- `audio_codes`、`audio_samples` 用 `tensor_list` codec——这是为张量跨阶段搬运专设的编码。

这个状态在阶段间的存取由两个小工具封装（[stages.py:L38-L43](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_tts/stages.py#L38-L43)）：

```python
def load_state(payload: StagePayload) -> Qwen3TTSState:
    return _load_pipeline_state(payload, Qwen3TTSState)

def store_state(payload: StagePayload, state: Qwen3TTSState) -> StagePayload:
    return _store_pipeline_state(payload, state)
```

#### 4.3.4 源码精读：radix cache key 必须由 embedding 内容派生

官方手册有一条硬规则（[tts_model_integration.md:L196-L200](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/tts_model_integration.md#L196-L200)）：**如果 prompt 里拼接了连续 embedding（Higgs 和 Qwen3-TTS 都这么做），radix cache key 必须由 embedding 内容派生**。否则两个内容不同、但占位 token id 相同的 prompt，会命中同一段 KV 前缀——于是「一个用户的语音泄漏到另一个请求」。

Qwen3-TTS 的落地是 `build_embedding_cache_key_ids`（[request_builders.py:L469-L476](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_tts/request_builders.py#L469-L476)）：

```python
def build_embedding_cache_key_ids(input_embeds: torch.Tensor) -> list[int]:
    """Build stable radix-cache token ids for a precomputed embedding prefix."""
    rows = input_embeds.detach().to(dtype=torch.float32, device="cpu")
    key_ids: list[int] = []
    for row in rows:
        digest = hashlib.blake2b(row.numpy().tobytes(), digest_size=8).digest()
        key_ids.append(int.from_bytes(digest, "little") & ((1 << 63) - 1))
    return key_ids
```

它对 **prompt embedding 的每一行**算一个 `blake2b` 哈希，压缩成一个 63 位整数，作为该位置的「token id」喂给 SGLang。这样 radix cache 看到的前缀 id 序列就唯一对应这段 embedding 内容——不同参考音频 → 不同 embedding → 不同 id 序列 → 不命中同一段 KV。注意这里为了算哈希才把张量 `.to(device="cpu")`，**只产出 metadata（一串 id）**，绝不替换 prefill/decode 用的设备张量（手册 [tts_model_integration.md:L192-L194](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/tts_model_integration.md#L192-L194) 的纪律）。

#### 4.3.5 代码实践：为假想字段选 wire 配置

1. **实践目标**：把 `wire` 的 `emit` 与 `codec` 语义落到具体字段。
2. **操作步骤**：假设你要给 `Qwen3TTSState` 加一个新字段 `emotion: str | None = None`（情绪标签，多数请求不传）。请决定它的声明方式。
3. **需要观察的现象**：对照 [payload_types.py:L22-L27](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_tts/payload_types.py#L22-L27) 的 `voice` 字段（None 默认、裸字段）。
4. **预期结果**：应写成 `emotion: str | None = None`（裸字段）。因为默认是 None，`DeclarativeStateBase` 会自动按 `not_none` 处理——没传情绪时不写进 payload，省带宽。若你想让空串也算「未设置」，则用 `wire(None, codec="str_or")`。
5. 待本地验证：可参考 [tests/unit_test/scheduling/test_pipeline_state.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/tests/unit_test/scheduling/test_pipeline_state.py)（[pipeline_state.py:L211-L212](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/pipeline_state.py#L211-L212) 注释提到的「字段完整往返」契约测试）验证 to_dict/load_state 的往返一致性。

#### 4.3.6 小练习与答案

**练习 1**：为什么 `audio_codes` 用 `tensor_list` codec，而不是 `raw`？

> 答案：`audio_codes` 是 AR 引擎产出的张量列表，要跨进程从 `tts_engine` 搬到 `vocoder`。`tensor_list` codec 知道如何把张量序列化/反序列化（可能走 u3-l3 讲的 relay 数据平面），而 `raw` 只能原样塞 dict、无法正确处理 torch 张量。

**练习 2**：若两个不同参考音频的 prompt 占位 token id 相同，但 `build_embedding_cache_key_ids` 没被调用，会发生什么？

> 答案：radix cache 会把这两个 prompt 的 KV 前缀当成同一段复用，导致第二个请求实际复用了第一个请求参考音频的 KV——表现为「语音串台」，即手册警告的「one user's audio leak into another's」。

---

### 4.4 vocoder、张量设备纪律与 abort 多调度器清理

#### 4.4.1 概念说明

本模块收尾三个相互关联的纪律：

1. **vocoder 的批处理抽象**：`BatchVocoderBase` 把「单条解码」与「批量解码」统一封进一个 `SimpleScheduler`。
2. **张量设备/dtype 纪律**：张量要尽量留在产出它的设备上，别「为保险」随手 `.cpu()`。
3. **abort 清理必须挂多个 scheduler**：任何按 `request_id` 存在 scheduler 外部的共享状态，都必须在所有碰它的 scheduler 上挂同一个清理回调。

#### 4.4.2 核心流程：BatchVocoderBase 模板方法

`BatchVocoderBase` 是个模板方法基类（[vocoder_base.py:L16-L64](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/vocoder_base.py#L16-L64)）。子类只实现三个语义钩子：`prepare_item`（从 payload 取状态与码本）、`decode_batch`（批量解码）、`store_result`（把波形写回 payload）。基类的 `build_scheduler` 自动产出两个闭包并塞进 `SimpleScheduler`：

```text
build_scheduler(max_batch_size, max_batch_wait_ms)
  ├─ _single(payload)  ──► prepare_item → decode_batch([1]) → store_result
  └─ _batch(payloads)  ──► [prepare_item ×N] → decode_batch(N) → [store_result ×N]
SimpleScheduler(_single, batch_compute_fn=_batch, max_batch_size, max_batch_wait_ms)
```

这就是 u4-l1 讲过的 `batch_compute_fn`：来一条就用 `_single`，攒够 `max_batch_size` 条或等满 `max_batch_wait_ms` 毫秒就用 `_batch` 一次性解码。

#### 4.4.3 源码精读：Qwen3-TTS 的 vocoder 与设备纪律

Qwen3-TTS 的 vocoder 实现 `_Qwen3TTSVocoder`（[stages.py:L159-L209](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_tts/stages.py#L159-L209)）只填三个钩子：

```python
class _Qwen3TTSVocoder(BatchVocoderBase):
    def prepare_item(self, payload):
        state = load_state(payload)
        codes = torch.as_tensor(state.audio_codes, dtype=torch.long)
        return state, codes

    async def decode_batch(self, items):
        wavs, sample_rate = self._tokenizer.decode(
            [{"audio_codes": codes} for _, codes in items])      # 一次性批量解码
        ...
        return [(wav, sample_rate) for wav in wavs]

    def store_result(self, payload, state, wav, sample_rate):
        ...
        audio_payload = audio_waveform_payload(wav, source_hint="Qwen3-TTS")
        state.audio_samples = None
        state.sample_rate = int(sample_rate)
        ...
        return payload
```

`create_vocoder_executor` 把它接成 scheduler（[stages.py:L212-L234](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_tts/stages.py#L212-L234)）：默认 `max_batch_size=8`、`max_batch_wait_ms=2`。

**张量设备纪律**贯穿手册（[tts_model_integration.md:L176-L194](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/tts_model_integration.md#L176-L194)），核心一句话：**别在 request builder 里「为保险」调 `.cpu()`**——每次 `.cpu()` 触发一次设备同步，而 AR runner 马上又要把张量搬回 GPU。Qwen3-TTS 的做法是只在**明确的归属边界**才搬运，例如 preprocessing 产出时一次性搬到 feedback_buffer 的设备与 dtype（[request_builders.py:L888-L908](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_tts/request_builders.py#L888-L908)）：

```python
prompt_input_embeds = (
    input_embeds.squeeze(0).detach().to(
        device=feedback_buffer.device, dtype=feedback_buffer.dtype)   # 只在边界搬一次
)
```

而 result adapter 在把码本交给 vocoder 前，才统一 `.cpu()`（[request_builders.py:L1053-L1060](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_tts/request_builders.py#L1053-L1060)）——因为 vocoder 的 `decode_batch` 要的是 CPU 可处理的码本。这一点有专门单测钉死：`test_qwen3_tts_result_adapter_keeps_code_handoff_tensor_native`（[test_pipeline.py:L1005](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/tests/unit_test/qwen3_tts/test_pipeline.py#L1005)）和 `test_qwen3_tts_request_data_keeps_decode_tensors_on_prepared_device`（[L1023](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/tests/unit_test/qwen3_tts/test_pipeline.py#L1023)）。

#### 4.4.4 源码精读：abort 清理为何必须挂在多个 scheduler

这是手册点名「最容易咬人」的部分（[tts_model_integration.md:L161-L173](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/tts_model_integration.md#L161-L173)）。Qwen3-TTS 把 preprocessing 阶段算出的**重张量**存进进程级字典 `_PREPARED_REQUESTS`（key 是 `request_id`）。这块状态「悬在 scheduler 之外」，必须在**三个时机**都被释放：

```text
时机 A：preprocessing 阶段 abort（还没把 handoff 交给 AR）
时机 B：AR(tts_engine) 阶段 abort（在它消费 handoff 之前）
时机 C：preprocessing 在请求已被 abort 之后才跑完 → 产物要被丢弃
```

清理函数 `cleanup_prepared_qwen3_tts_request` 本身极简、且**幂等**（[request_builders.py:L195-L199](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_tts/request_builders.py#L195-L199)）：

```python
def cleanup_prepared_qwen3_tts_request(request_id: str) -> None:
    """Drop any prepared Qwen3-TTS handoff state for an aborted request."""
    with _PREPARED_REQUESTS_LOCK:
        _PREPARED_REQUESTS.pop(str(request_id), None)   # pop 带默认值 → 幂等
```

它被挂在**两个** scheduler 上：

1. **preprocessing 的 SimpleScheduler**——`create_preprocessing_executor` 把它作为 `abort_callback`（[stages.py:L126-L131](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_tts/stages.py#L126-L131)）。
2. **AR 的 OmniScheduler**——经 `Qwen3TtsEngineBuilder.make_abort_callback()` 返回**同一个函数**（[engine_builder.py:L110-L111](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_tts/engine_builder.py#L110-L111)），再由 `TtsEngineBuilder.make_scheduler` 透传给 `OmniScheduler(..., abort_callback=...)`（[engine_factory.py:L180-L211](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/scheduling/engine_factory.py#L180-L211)）。

为什么要挂两个？因为请求**可能停在任意一个阶段**：

- 停在 preprocessing（时机 A）→ preprocessing 的 abort 释放。
- 已进 AR、还没消费 handoff（时机 B）→ AR 的 abort 释放。
- 两者都可能发生，所以**两边都得挂同一个幂等回调**；手册强调「它会按设计被对同一 id 调用不止一次」。

至于时机 C（preprocessing 跑完了但请求已被 abort），靠的是 `pop` 在消费侧的语义：`build_sglang_qwen3_tts_request` 调 `pop_prepared_qwen3_tts_request` 取回（[request_builders.py:L962-L967](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_tts/request_builders.py#L962-L967)），若该 id 已被 abort 清掉则返回 None 并报错——产物不会泄漏。

这三个路径都被 GPU-free 单测钉死（[test_pipeline.py:L1298-L1373](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/tests/unit_test/qwen3_tts/test_pipeline.py#L1298-L1373)）：

- `test_qwen3_tts_preprocessing_abort_cleans_prepared_state`（时机 A）
- `test_qwen3_tts_preprocessing_abort_race_cleans_late_prepared_state`（时机 C，用线程模拟「abort 先于 preprocess 完成」的竞态）
- `test_qwen3_tts_ar_scheduler_abort_cleans_prepared_state`（时机 B）

#### 4.4.5 代码实践：阅读 abort 竞态测试

1. **实践目标**：理解「preprocessing 跑完时请求已 abort」这条最隐蔽的清理路径。
2. **操作步骤**：读 [test_pipeline.py:L1316-L1371](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/tests/unit_test/qwen3_tts/test_pipeline.py#L1316-L1371) 的 `test_qwen3_tts_preprocessing_abort_race_cleans_late_prepared_state`。
3. **需要观察的现象**：测试用 `threading.Event` 让 `fake_preprocess` 卡住→期间调 `scheduler.abort(request_id)`→再 `release.set()` 放行 preprocess 完成→断言 `_PREPARED_REQUESTS` 里**没有**该 id，且 `outbox` 为空（产物被丢弃，没流到下游）。
4. **预期结果**：能讲清「abort 在 preprocess 写入字典之前发生」与「之后发生」两种竞态为何都安全——前者写入后立刻被 abort 的 pop 清掉不会，其实测试覆盖的是后者：preprocess 在 abort 之后才写入，但由于 abort 已把请求标记为取消，`_run_single` 不会把结果放进 outbox，且清理回调已移除该条目。
5. 待本地验证：`pytest tests/unit_test/qwen3_tts/test_pipeline.py -k abort -q` 应全绿（GPU-free）。

#### 4.4.6 小练习与答案

**练习 1**：假如 Qwen3-TTS 还在 vocoder 阶段维护了一个「按 request_id 索引的波形缓冲」，需要在哪里挂清理？

> 答案：在 vocoder 的 `SimpleScheduler` 上也挂一个清理该缓冲的 `abort_callback`（与现有两个回调并列）。原则是手册 [tts_model_integration.md:L171-L173](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/tts_model_integration.md#L171-L173)：**每个触碰该共享状态的 scheduler 都要挂同一个幂等清理**。当前 Qwen3-TTS 的 vocoder 是无状态的（`BatchVocoderBase` 不按 id 存东西），所以不需要。

**练习 2**：为什么 `cleanup_prepared_qwen3_tts_request` 必须幂等？

> 答案：因为它挂在 preprocessing 与 AR 两个 scheduler 上，同一个 `request_id` 的 abort 信号会被 Coordinator 广播（u2-l4 讲过 PUB/SUB abort），两个 scheduler 都会各自调一次。`dict.pop(key, None)` 对不存在的 key 不报错，保证多次调用安全。

---

## 5. 综合实践：为假想 TTS 模型列出接入清单

把本讲四个模块串起来。假设要接入一个假想模型 `AcmeTTS`（HF 架构 `AcmeTTSForSpeech`，文本+参考音频输入，codec AR 生成，普通批处理 vocoder）。

**任务**：按官方 8 步清单，产出「需要新建 / 修改的文件清单」，并指出 abort 清理要挂在哪几个 scheduler 上。

**参考答案（新建文件）**：

| 文件 | 对应步骤 | 内容要点 |
| --- | --- | --- |
| `sglang_omni/models/acme_tts/__init__.py` | 步骤 2 | 包标记；声明 `CAPABILITIES`（若需，u5-l1）。 |
| `sglang_omni/models/acme_tts/config.py` | 步骤 2 | `AcmeTTSPipelineConfig`，`architecture="AcmeTTSForSpeech"`，三个 `StageConfig`（preprocessing→tts_engine→vocoder），末尾 `EntryClass = AcmeTTSPipelineConfig`。 |
| `sglang_omni/models/acme_tts/stages.py` | 步骤 5 | `create_preprocessing_executor`（`SimpleScheduler` + `abort_callback`）、`create_sglang_tts_engine_executor`（委托 `AcmeEngineBuilder`）、`create_vocoder_executor`（继承 `BatchVocoderBase`）。 |
| `sglang_omni/models/acme_tts/request_builders.py` | 步骤 6 | 输入归一化、必填校验、`_IMPLICIT_SAMPLING_DEFAULTS` 过滤、AR 请求构造、幂等 `cleanup_*` 清理函数、`build_embedding_cache_key_ids`。 |
| `sglang_omni/models/acme_tts/payload_types.py` | 步骤 6 | `AcmeTTSState(DeclarativeStateBase)`，用 `wire(...)` 声明字段。 |
| `sglang_omni/models/acme_tts/sglang_model.py` | 步骤 4 | SGLang 模型类 `AcmeTTSTalker`。 |
| `sglang_omni/models/acme_tts/engine_builder.py` | 步骤 5 | `AcmeEngineBuilder(TtsEngineBuilder)`，填 `generation_defaults` / `setup_model` / `make_model_runner` / `make_adapters` / `make_abort_callback`。 |
| `examples/configs/acme_tts.yaml` | 步骤 7 | `config_cls: AcmeTTSPipelineConfig` + `model_path: ...`。 |
| `tests/unit_test/acme_tts/test_pipeline.py` | 步骤 8 | 请求边界 / 设备不变量 / abort 三路径 / vocoder 批处理。 |

**修改文件**：

| 文件 | 改动 |
| --- | --- |
| `sglang_omni/model_runner/sglang_model_runner.py` | 在 `_register_omni_model` 的字典里加一行 `"AcmeTTSTalker": "sglang_omni.models.acme_tts.sglang_model:AcmeTTSTalker"`（[sglang_model_runner.py:L84-L99](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/model_runner/sglang_model_runner.py#L84-L99)）。若 HF 架构名与类名不一致，还要在 builder 里设 `model_arch_override`（参考 [engine_builder.py:L17](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_tts/engine_builder.py#L17)）。 |
| `docs/basic_usage/tts.md` | 步骤 7，列出模型与启动命令。 |

**abort 清理要挂在哪些 scheduler**：

AcmeTTS 若和 Qwen3-TTS 一样把 preprocessing 的重张量存进进程级字典，则**同一个幂等清理函数必须挂在两个 scheduler 上**：

1. preprocessing 的 `SimpleScheduler`（时机 A、C）；
2. tts_engine 的 `OmniScheduler`（经 `engine_builder.make_abort_callback()` → `TtsEngineBuilder.make_scheduler` 透传，时机 B）。

理由（[tts_model_integration.md:L161-L173](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/tts_model_integration.md#L161-L173)）：请求可能停在任意阶段，abort 由 Coordinator 广播给全体 stage，只有「每个碰共享状态的 scheduler 都挂同一回调」才能保证 `_PREPARED_REQUESTS` 这类悬空状态不泄漏。若 vocoder 也按 id 存状态（AcmeTTS 假设是无状态批 vocoder，则不需要），还要再挂第三个。

---

## 6. 本讲小结

- **TTS 最小管线是三阶段**：`preprocessing`（`SimpleScheduler`，CPU/轻量活，每请求一次）→ `tts_engine`（`OmniScheduler`，AR 生成 codec）→ `vocoder`（`SimpleScheduler` + `batch_compute_fn`，码→波形）。Qwen3-TTS 的 [config.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_tts/config.py#L20-L54) 把它声明式写死。
- **接入一个新 TTS 模型按官方 8 步清单**（架构→config→stages→runner→注册→request_builders/payload_types→yaml→测试），核心是 `config.py`（拓扑）+ `stages.py`（工厂）+ `request_builders.py`（边界）+ `payload_types.py`（状态）四件套。
- **端点默认值会静默覆盖模型默认值**：HTTP 层塞的 S2-Pro 采样默认值（`temp=0.8` 等），要靠 `explicit_generation_params`（Pydantic `model_fields_set`）+ `_IMPLICIT_SAMPLING_DEFAULTS` 反向过滤，区分「用户显式传」与「端点填的」。
- **`payload_types` 用 `wire(...)` + `DeclarativeStateBase` 声明序列化**：`emit` 控制何时输出、`codec` 控制如何编码；None 默认字段自动 `not_none`，`ref_code_len` 用 `truthy` 省带宽。
- **radix cache key 必须由 embedding 内容派生**（`build_embedding_cache_key_ids` 用 `blake2b` 把每行 embedding 压成 id），否则不同参考音频会命中同一段 KV，造成语音串台。
- **abort 清理必须挂在多个 scheduler**：preprocessing 与 tts_engine 各挂同一个幂等回调（`dict.pop(key, None)`），覆盖「停在 preprocessing」「停在 AR」「preprocessing 跑完时已 abort」三条路径；理由是 abort 由 Coordinator 广播、请求可能停在任意阶段。

## 7. 下一步学习建议

- **u5-l4（预处理与多模态输入）**：本讲的 preprocessing 阶段会调用 `resource_connector` 控制本地媒体可访问路径、用 `cache_key` 做参考音频缓存。这些机制在 u5-l4 系统讲解，是本讲 `preprocessing` 的「上游基础设施」。
- **u6-l5（参考音频编码缓存服务）**：本讲出现的 `ReferenceEncodeService` / `KeyedReferenceEncodeHook`（[request_builders.py:L571-L702](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_tts/request_builders.py#L571-L702)）做了同键 single-flight、失败不缓存，u6-l5 会深入其缓存键与字节预算。
- **u6-l2（量化、权值加载与校验）**：若你的 TTS 权重不能干净加载进 SGLang 模块，需要写 `weight_loader.py`（Higgs 的 `DiscreteWeightMapper` 是参考形状，见接入手册 [tts_model_integration.md:L108-L110](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/tts_model_integration.md#L108-L110)）。
- **u7-l4（基准测试与评测）**：接入完成后，用 `python -m benchmarks.eval.benchmark_tts_seedtts --help` 跑共享 TTS 基准，按 WER/CER/throughput/`rtf_mean` 判断是否可上线（手册 [tts_model_integration.md:L213-L222](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/docs/developer_reference/tts_model_integration.md#L213-L222)）。
- **u7-l5（综合实战：新增一个模型家族）**：把本讲的清单真正落地一遍，串联 registry/config/scheduler/model_runner/serve 各层。
