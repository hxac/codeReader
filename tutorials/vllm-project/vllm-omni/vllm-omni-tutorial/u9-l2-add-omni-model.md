# 添加 Omni 多阶段模型

## 1. 本讲目标

本讲面向「想把自己研发的多阶段全模态模型接入 vLLM-Omni」的二次开发者。学完后你应当能够：

- 说出接入一个 Omni 多阶段模型需要新增哪几类文件、它们各自承担什么职责；
- 用 `PipelineConfig` + `StagePipelineConfig` 写出冻结的阶段拓扑（含 `stage_id`、`input_sources`、`execution_type`、`final_output`）；
- 解释阶段之间如何通过 `stage_input_processors` 把上游的 `prompt_embeds` / hidden states / codec codes 转译成下游的输入；
- 理解 `final_stage_id` 是如何由用户请求的输出 modality（text / audio）反向决定流水线在哪一段「收口」的；
- 仿照 Qwen3-Omni 的三阶段结构，为一个假想的两阶段模型编写拓扑与数据流说明。

本讲建立在 u3-l1（多阶段架构与编排）和 u4-l1（AR 模块继承体系）之上：u3-l1 解释了「请求如何在 Orchestrator 中跨阶段前推」，u4-l1 解释了「一个 AR stage 内部如何继承 vLLM」。本讲要回答的是**站在模型作者视角，怎样把一个由多个子模型组成的端到端模型，拆解成框架可编排的若干 stage**。

## 2. 前置知识

阅读本讲前，建议你已经掌握以下概念（不熟悉的可以回头看对应讲义）：

- **stage（阶段）**：vLLM-Omni 把一次端到端推理拆成若干顺序子任务，每个子任务是一个 stage，对应一个独立 EngineCore 子进程（见 u3-l1、u3-l3）。
- **Orchestrator 与「前推」**：后台编排线程把上游 stage 的输出路由到下游 stage（见 u3-l2）。本讲关注的是「前推时用的转译函数怎么写」。
- **`model_stage`**：多阶段模型在同一个统一类里，用 `model_stage` 字段（如 `"thinker"`/`"talker"`/`"code2wav"`）决定本次实例化加载哪个子模型（见 u4-l1、u3-l3）。
- **`execution_type`（StageExecutionType）**：每个 stage 声明自己的执行类型——`LLM_AR`（自回归）、`LLM_GENERATION`（单步生成）、`DIFFUSION`（扩散）——框架据此分派 scheduler、worker 与 model runner（见 u4-l2、u5-l2）。
- **冻结拓扑 vs 部署 YAML**：拓扑（哪个 stage 连到哪个 stage、各自的执行类型）写在代码里冻结，用户只能改部署参数（见 u2-l2）。

关键术语补充：

- **RVQ codec codes**：残差向量量化（Residual Vector Quantization）码本下标，是音频离散表示。Qwen3-Omni 的 Talker 阶段把文本语义压成一串 codec codes，再由 Code2Wav 解码成波形。
- **pipeline 拓扑注册表**：`OMNI_PIPELINES` 字典，把模型的 `model_type` 映射到它的 `PipelineConfig`（或一个根据 HF config 选择变体的 resolver）。
- **`stage_input_processors`（阶段输入处理器）**：一组函数，负责把上游 stage 的输出「翻译」成下游 stage 的 `prompt_token_ids` + `additional_information` + 多模态数据。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [docs/contributing/model/adding_omni_model.md](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/contributing/model/adding_omni_model.md) | 官方「如何接入多阶段模型」分步指南（以 Qwen3-Omni 为范本） |
| [examples/offline_inference/qwen3_omni/end2end.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/examples/offline_inference/qwen3_omni/end2end.py) | Qwen3-Omni 端到端离线推理示例，演示三阶段采样参数与输出路由 |
| [vllm_omni/model_executor/models/qwen3_omni/pipeline.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/model_executor/models/qwen3_omni/pipeline.py) | Qwen3-Omni 冻结的阶段拓扑（3 个 `StagePipelineConfig`） |
| [vllm_omni/model_executor/models/qwen3_omni/qwen3_omni.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/model_executor/models/qwen3_omni/qwen3_omni.py) | 统一模型类，按 `model_stage` 分派加载 Thinker/Talker/Code2Wav |
| [vllm_omni/model_executor/stage_input_processors/qwen3_omni.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/model_executor/stage_input_processors/qwen3_omni.py) | 阶段间转译函数（Thinker→Talker、Talker→Code2Wav） |
| [vllm_omni/config/stage_config.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/stage_config.py) | `StagePipelineConfig` / `PipelineConfig` 数据类定义 |
| [vllm_omni/config/pipeline_registry.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/pipeline_registry.py) | `OMNI_PIPELINES` 注册表与 `resolve_pipeline_config` |
| [vllm_omni/config/omni_config.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/omni_config.py) | 结构化配置：`BaseVllmOmniStageConfig` / `VllmOmniConfig` 与 `from_pipeline_config` |
| [vllm_omni/deploy/qwen3_omni_moe.yaml](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/deploy/qwen3_omni_moe.yaml) | Qwen3-Omni 生产部署 YAML（用户唯一可编辑层） |
| [vllm_omni/entrypoints/omni_base.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/omni_base.py) | `_compute_final_output_stage_ids`：按输出 modality 反推最终 stage |

## 4. 核心概念与源码讲解

### 4.1 接入总览：多阶段模型的三层结构

#### 4.1.1 概念说明

一个「多阶段全模态模型」通常对应**一个 HF 仓库**，里面装了**多个子模型**（例如 Qwen3-Omni 仓库里有 Thinker、Talker、Code2Wav 三套权重，共用一份根 `config.json`）。接入 vLLM-Omni 不是把整条流水线写死成一个类，而是把它拆成「框架可调度」的三层：

1. **统一模型类（unified model class）**：一个对外暴露的类（如 `Qwen3OmniMoeForConditionalGeneration`），它在 `__init__` 里根据 `model_stage` 字段**只加载其中一个子模型**。框架给每个 stage 各启动一个进程、各传一个不同的 `model_stage`，于是「同一个类」在不同进程里表现为不同子模型。
2. **阶段拓扑（pipeline topology）**：用 `PipelineConfig` 描述「有几个 stage、每个 stage 的执行类型、谁把数据传给谁」。这是**冻结**的，写在 `pipeline.py` 里，用户不能改。
3. **阶段输入处理器（stage input processors）**：一组函数，描述 stage 之间数据如何转译（例如把 Thinker 的 hidden states 变成 Talker 的输入 embedding）。这写在 `stage_input_processors/<model>.py` 里。

> 为什么不写成一个巨大的 `forward`？因为 Thinker 是自回归（多步生成）、Code2Wav 是单步生成，两者执行特性截然不同；拆成独立 stage 后，框架可以分别给它们配 scheduler、worker、显存、设备（甚至不同 GPU），并让它们**流水线重叠执行**以提升吞吐。这正是 vLLM-Omni 多阶段架构的价值（见 u3-l1）。

官方指南把这一接入流程总结为七步：建目录结构 → 写统一模型类 → 注册到 registry → 写阶段拓扑 → 写阶段输入处理器 → 写测试 → 写 recipe。

[docs/contributing/model/adding_omni_model.md:573-584](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/contributing/model/adding_omni_model.md#L573-L584) — 官方「接入总结」清单。

#### 4.1.2 核心流程

以 Qwen3-Omni 为例，接入后的端到端请求流如下：

```text
用户请求 (text/audio/video)
        │
        ▼
  ┌─────────────────────────────────────────────┐
  │ Stage 0  Thinker  (LLM_AR, 多模态理解+文本)  │  ← owns_tokenizer, requires_multimodal_data
  └─────────────────────────────────────────────┘
        │  产出: token_ids + hidden_states(layer 0/24) + tts embeddings
        │  转译: thinker2talker_*
        ▼
  ┌─────────────────────────────────────────────┐
  │ Stage 1  Talker   (LLM_AR, 文本embedding→codec codes) │
  └─────────────────────────────────────────────┘
        │  产出: code_predictor_codes (RVQ)
        │  转译: talker2code2wav_*
        ▼
  ┌─────────────────────────────────────────────┐
  │ Stage 2  Code2Wav (LLM_GENERATION, codec→波形) │  ← final_output_type="audio"
  └─────────────────────────────────────────────┘
        │
        ▼
  最终输出 (text 来自 stage 0，audio 来自 stage 2)
```

注意：stage 0 也被标了 `final_output=True`、`final_output_type="text"`。这意味着如果用户只要文本，流水线在 stage 0 就收口；只有要音频时才一路跑到 stage 2。这就是后面 4.4 要讲的「按输出 modality 路由」。

#### 4.1.3 源码精读

官方指南给出的目录结构约定（你的新模型要遵循这套布局）：

[docs/contributing/model/adding_omni_model.md:28-48](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/contributing/model/adding_omni_model.md#L28-L48) — 三类文件：模型目录（含 `pipeline.py`）、阶段输入处理器、部署 YAML。

其中拓扑的注册位置很关键：**树内模型**把 `PipelineConfig` 写进 `vllm_omni/config/pipeline_registry.py`；**自定义/树外**模型用 `--deploy-config` 指定部署 YAML，并用 `register_pipeline` 注册拓扑。

[docs/contributing/model/adding_omni_model.md:50-53](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/contributing/model/adding_omni_model.md#L50-L53) — 树内注册到 `pipeline_registry.py`，树外用 `--deploy-config` + `register_pipeline`。

统一模型类的核心是 `model_stage` 分派。下面这段是 Qwen3-Omni 真实代码，它根据 `model_stage` 决定加载哪个子模型，并据此配置该 stage 的运行时特性：

[vllm_omni/model_executor/models/qwen3_omni/qwen3_omni.py:131-217](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/model_executor/models/qwen3_omni/qwen3_omni.py#L131-L217) — 关键片段（精简）：

```python
self.model_stage = vllm_config.model_config.model_stage

if self.model_stage == "thinker":
    self.use_async_omni_output = True
    thinker_vllm_config = vllm_config.with_hf_config(
        thinker_config, architectures=["Qwen3OmniMoeThinkerForConditionalGeneration"])
    self.thinker = init_vllm_registered_model(...)
    self.model = self.thinker          # 统一类的 .model 指向真正干活的子模型
elif self.model_stage == "talker":
    # talker 只向 code2wav 投递 codec codes，跳过 latent hidden 的 D2H
    self.use_async_omni_output = True
    self.talker = init_vllm_registered_model(...)
    self.model = self.talker
elif self.model_stage == "code2wav":
    self.code2wav = init_vllm_registered_model(...)
    self.model = self.code2wav
else:
    raise ValueError(f"Invalid model_stage: {self.model_stage}. ...")
```

要点：

- **`vllm_config.with_hf_config(...)`**：用同一份根 config 派生出「指向不同子 config」的 vllm_config，再交给 vLLM 的 `init_vllm_registered_model` 装载对应架构。于是「一个 HF 仓库 → 多个 stage」得以实现。
- **`self.model = self.<子模型>`**：统一类把当前 stage 真正的子模型挂到 `self.model`，后续的 `forward`/`embed_input_ids`/`compute_logits` 都基于 `self.model` 分派（例如 `embed_input_ids` 在 stage=="talker" 时调用 `self.model.embed_input_ids(input_ids)`）。
- **每个 stage 的运行时开关不同**：thinker 开 `use_async_omni_output` 异步产出多模态输出；talker 还设了 `gpu_resident_buffer_keys` 让 codec codes 留在 GPU 避免 D2H 卡顿；code2wav 设 `requires_raw_input_tokens`。这些开关在 4.3、4.4 与 u4-l1/u4-l3 中有呼应。

#### 4.1.4 代码实践

**实践目标**：在真实源码里验证「同一个类、不同 stage、不同子模型」的分派机制。

**操作步骤**：

1. 打开 [qwen3_omni.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/model_executor/models/qwen3_omni/qwen3_omni.py)，定位 `__init__`（约 L104）。
2. 统计 `self.model_stage` 在该文件中出现的所有分支（forward/embed_input_ids/compute_logits/load_weights 等共 10+ 处）。
3. 注意 stage 0（thinker）独有 `self.tts_tokens`、stage 1（talker）独有 `self.suppressed_tokens`、stage 2（code2wav）独有的 `enable_update_additional_information`。

**需要观察的现象**：`self.model` 始终指向「当前 stage 的子模型」，而其他两个 stage 的属性（`self.thinker`/`self.talker`/`self.code2wav`）被显式置为 `None`——这是为了在权重加载（`load_weights`）时能正确按前缀分流，避免加载无关子模型的权重。

**预期结果**：你能画出一张表，列出 thinker/talker/code2wav 三个分支各自初始化了什么、置 None 了什么、设了哪些运行时开关。

> 本实践为「源码阅读型实践」，不依赖运行；如需运行验证，待本地具备 Qwen3-Omni 权重与 GPU 环境后进行，结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么不在统一类里一次性把 thinker/talker/code2wav 三个子模型都加载好？

**参考答案**：因为每个 stage 跑在独立进程（u3-l3），该进程只需要它那一份子模型。同时加载三者会浪费显存、且让 `forward` 的分派变复杂。通过 `model_stage` 单态化，每个进程只装一个子模型，`self.model` 直接指向它。

**练习 2**：`self.model = self.thinker` 这一句的作用是什么？

**参考答案**：把当前 stage 的子模型统一挂到 `self.model`，使下游通用方法（embed/forward/logits/load_weights）可以无差别地通过 `self.model` 访问，无需在每个方法里都写一套三路 `if/elif`（embed、logits 等仍按 `model_stage` 分派是因为签名/语义不同，而非「拿哪个对象」不同）。

---

### 4.2 stage 声明：用 StagePipelineConfig 写冻结拓扑

#### 4.2.1 概念说明

每个多阶段模型必须声明它的**拓扑**：有几个 stage、各自的执行类型、上下游关系、最终输出。这些信息用 `StagePipelineConfig`（单个 stage）和 `PipelineConfig`（整条流水线）两个**冻结** dataclass 表达。

冻结的含义是：拓扑由模型作者在代码里写死（`pipeline.py`），用户**只能改部署参数**（YAML/CLI），不能改阶段数量与连接关系。这与 u2-l2 的「拓扑（代码冻结）+ 部署 YAML + CLI 覆盖」三层合流完全一致。

`StagePipelineConfig` 的关键字段（节选）：

| 字段 | 含义 |
|------|------|
| `stage_id` | 阶段编号，从 0 开始，即该 stage 在 `stages` 元组里的下标 |
| `model_stage` | 与统一类的 `model_stage` 对应（如 `"thinker"`），决定该 stage 装载哪个子模型 |
| `execution_type` | 执行类型（`LLM_AR`/`LLM_GENERATION`/`DIFFUSION`），决定 scheduler/worker/runner |
| `input_sources` | 上游 stage 的 `stage_id` 元组；`()` 表示从用户输入开始（即 stage 0） |
| `final_output` | 该 stage 是否产出「对外可见」的最终结果 |
| `final_output_type` | 最终结果的 modality（`"text"`/`"audio"`/`"image"` 等） |
| `owns_tokenizer` | 是否持有 tokenizer（通常只有 stage 0 持有） |
| `requires_multimodal_data` | 是否需要多模态输入 |
| `hf_config_name` | 该子模型在根 HF config 里的子字段名（如 `"thinker_config"`） |
| `engine_output_type` | 该 stage 引擎产出的内部载荷类型（如 `"latent"`/`"audio"`/`"text"`） |
| `custom_process_next_stage_input_func` | 把本 stage 输出转译给下一 stage 的函数全限定名 |
| `sync_process_input_func` | orchestrator 侧为下一 stage 预留 prompt 槽位的同步函数 |
| `async_chunk_process_next_stage_input_func` | 流式（async_chunk）模式下逐块转译函数 |

[vllm_omni/config/stage_config.py:212-245](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/stage_config.py#L212-L245) — `StagePipelineConfig` 全字段定义（节选自上面的代码）。

#### 4.2.2 核心流程

声明拓扑的步骤：

1. 在 `vllm_omni/model_executor/models/<your_model>/pipeline.py` 里，用 `StagePipelineConfig` 逐个描述 stage，组成一个元组 `stages`。
2. 包进一个 `PipelineConfig`，填 `model_type`（要与 HF config 的 `model_type` 一致）、`model_arch`、可选的 `default_deploy_config_name` 与 `endpoint_restrictions`。
3. 在 `pipeline_registry.py` 的 `OMNI_PIPELINES` 字典里注册 `model_type → PipelineConfig`；若模型有多种变体（如 thinker-only），用一个 **resolver** 函数根据 HF config 选择。
4. 框架启动时用 `resolve_pipeline_config(model_type, hf_config)` 取出拓扑。

`input_sources` 表达的就是「边」。例如 stage 1 的 `input_sources=(0,)` 表示它的输入来自 stage 0。这是一张有向无环图，Orchestrator 据此决定前推方向（u3-l2）。

#### 4.2.3 源码精读

Qwen3-Omni 的完整三阶段拓扑是本讲最核心的范例：

[vllm_omni/model_executor/models/qwen3_omni/pipeline.py:22-75](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/model_executor/models/qwen3_omni/pipeline.py#L22-L75) — 逐段拆解：

```python
QWEN3_OMNI_PIPELINE = PipelineConfig(
    model_type="qwen3_omni_moe",
    default_deploy_config_name="qwen3_omni_moe.yaml",
    model_arch="Qwen3OmniMoeForConditionalGeneration",
    endpoint_restrictions=(
        EndpointRestriction(
            OmniServingCapability.COMPLETIONS,
            "Qwen3-Omni requires chat template structure ... Use /v1/chat/completions instead.",
        ),
    ),
    stages=(
        StagePipelineConfig(
            stage_id=0, model_stage="thinker",
            execution_type=StageExecutionType.LLM_AR,
            input_sources=(),               # 起点：吃用户输入
            final_output=True, final_output_type="text",
            owns_tokenizer=True, requires_multimodal_data=True,
            hf_config_name="thinker_config", engine_output_type="latent",
            custom_process_next_stage_input_func=f"{_PROC}.thinker2talker_full_payload",
            async_chunk_process_next_stage_input_func=f"{_PROC}.thinker2talker_async_chunk",
            sampling_constraints={"detokenize": True},
        ),
        StagePipelineConfig(
            stage_id=1, model_stage="talker",
            execution_type=StageExecutionType.LLM_AR,
            input_sources=(0,),             # 来自 thinker
            hf_config_name="talker_config", engine_output_type="latent",
            sync_process_input_func=f"{_PROC}.thinker2talker_token_only",
            custom_process_next_stage_input_func=f"{_PROC}.talker2code2wav_full_payload",
            async_chunk_process_next_stage_input_func=f"{_PROC}.talker2code2wav_async_chunk",
            sampling_constraints={"detokenize": False, "stop_token_ids": [2150]},
        ),
        StagePipelineConfig(
            stage_id=2, model_stage="code2wav",
            execution_type=StageExecutionType.LLM_GENERATION,   # 单步生成
            input_sources=(1,),             # 来自 talker
            final_output=True, final_output_type="audio",
            hf_config_name="thinker_config", engine_output_type="audio",
            sampling_constraints={"detokenize": True},
        ),
    ),
)
```

几个值得注意的设计：

- **stage 0 和 stage 2 都是 `final_output=True`**，但 `final_output_type` 不同（`"text"` vs `"audio"`）。这就是 4.4 要讲的「按 modality 收口」。
- **stage 1（talker）没有 `final_output=True`**：它的 codec codes 不是用户想要的最终产物，只是中间表示，所以对用户不可见。
- **`execution_type` 的差异**：stage 0/1 是 `LLM_AR`（自回归，多步生成，走 `OmniARScheduler`），stage 2 是 `LLM_GENERATION`（单步前向，走 `OmniGenerationScheduler`，见 u4-l2）。这种区分与 Code2Wav「一次性把 codec codes 解码成波形」的语义吻合。
- **`engine_output_type` 与 `final_output_type` 不同**：thinker/talker 的 `engine_output_type="latent"`（产 latent 向量供下游），但只有 thinker 的 `final_output_type="text"`（detokenize 后对外是文本）。
- **`sampling_constraints`**：把「本 stage 必须强制」的采样参数冻结进拓扑（如 talker 必须在 `stop_token_ids=[2150]` 处停、`detokenize=False`）。它会被合进该 stage 的默认采样参数，用户给的参数不能覆盖这些约束。
- **`endpoint_restrictions`**：声明该模型必须禁用 `/v1/completions`（因为 thinker-talker 交接依赖 chat 模板结构），运行时被改成返回 400 的拒绝处理器（见 u6-l1）。

模型有多种变体时，用 **resolver** 根据 HF config 动态选择拓扑。Qwen3-Omni 的 Captioner 变体没有音频输出，于是选 thinker-only 单阶段：

[vllm_omni/model_executor/models/qwen3_omni/pipeline.py:98-110](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/model_executor/models/qwen3_omni/pipeline.py#L98-L110) — `resolve_qwen3_omni_pipeline` 用 `@pipeline_cfg_resolver(config_type=Qwen3OmniMoeConfig)` 注册，按 `hf_config.enable_audio_output` 选三阶段或单阶段。

注册表则是这一切的总入口：

[vllm_omni/config/pipeline_registry.py:111-167](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/pipeline_registry.py#L111-L167) — `OMNI_PIPELINES` 字典。注意 `"qwen3_omni_moe"` 映射的是 resolver 函数（`resolve_qwen3_omni_pipeline`）而非静态 `PipelineConfig`，因为同一 model_type 可能对应不同拓扑。

[vllm_omni/config/pipeline_registry.py:192-201](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/pipeline_registry.py#L192-L201) — `resolve_pipeline_config` 的「值是 callable 就调用、否则直接返回」二分逻辑。

#### 4.2.4 代码实践

**实践目标**：亲手读懂一份冻结拓扑，并对照运行示例确认 `num_stages`。

**操作步骤**：

1. 阅读 [pipeline.py:22-75](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/model_executor/models/qwen3_omni/pipeline.py#L22-L75)，数清三阶段各自的 `input_sources`，画出有向图。
2. 打开 [end2end.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/examples/offline_inference/qwen3_omni/end2end.py)，看示例如何用 `omni.num_stages` 自动对齐采样参数数量：

[examples/offline_inference/qwen3_omni/end2end.py:333-340](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/examples/offline_inference/qwen3_omni/end2end.py#L333-L340) — 示例为三个 stage 各准备一份 `SamplingParams`（thinker/talker/code2wav），再用 `all_sampling_params[:num_stages]` 截取，保证「采样参数列表长度 == stage 数」。注意代码注释指出「code predictor is integrated into talker for Qwen3 Omni」——talker 的采样参数实际同时驱动了 code predictor。

**需要观察的现象**：示例里三份采样参数的 `max_tokens` 差异巨大（thinker 1200、talker 4096、code2wav `4096*16`）。这反映了各 stage 的生成长度需求：thinker 生成简短「思考文本」，talker 生成 codec codes（更长），code2wav 把 codes 解码成波形（最长）。

**预期结果**：你能在脑中画出 `(0)→(1)→(2)` 的有向图，并理解为何采样参数要按 stage 分开给。

> 运行该示例需要 Qwen3-Omni 权重与多 GPU；完整运行结果**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果 stage 1 的 `input_sources` 写成 `()` 而不是 `(0,)`，会发生什么？

**参考答案**：`()` 表示该 stage 直接从用户原始输入开始，于是 stage 0（thinker）和 stage 1（talker）就没有数据依赖关系，Orchestrator 不会把 thinker 的输出推给 talker。talker 会拿不到 hidden states / embeddings，从而无法工作。`input_sources` 是拓扑图的「边」，写错就断流。

**练习 2**：为什么 stage 1（talker）没有设置 `final_output=True`？

**参考答案**：talker 产出的 RVQ codec codes 是中间表示，不是用户期望的最终产物（用户要的是文本或音频波形）。把 `final_output=True` 留给 stage 0（文本）和 stage 2（音频），框架据此判断「请求是否已经产生用户可见结果」并决定是否收口（见 4.4）。

---

### 4.3 stage 间数据流：stage_input_processors 与张量转译

#### 4.3.1 概念说明

拓扑声明了「谁连到谁」，但没说「上游输出怎么变成下游输入」。这件事由 **stage input processors** 完成。

多阶段模型里，上游 stage 的输出往往**不是**下游 stage 能直接吃的 token ids，而是 latent 向量、hidden states、codec codes 等。例如：

- Thinker（stage 0）输出每步的 hidden states（layer 0 的 embedding、layer 24 的 hidden）和 TTS token 的 embedding；
- Talker（stage 1）需要把这些当作「prefill embedding」喂进去，并产出 codec codes；
- Code2Wav（stage 2）需要把 codec codes 拉平成一维输入解码成波形。

这些转译逻辑就是 `stage_input_processors/<model>.py` 里的一组函数。它们在 Orchestrator 把请求「前推」到下一 stage 时被调用（u3-l2 的 `_forward_to_next_stage`）。

官方指南特别强调：每条阶段间边应该提供**一组协同的处理器**而非单个大函数，并用后缀区分职责：

[docs/contributing/model/adding_omni_model.md:485-489](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/contributing/model/adding_omni_model.md#L485-L489) — 三个后缀的职责表。

| 后缀 | 角色 | 何时运行 |
|------|------|----------|
| `*_full_payload` | worker 侧完整载荷生产者 | `async_chunk=false`：累积张量经 connector 整包传输 |
| `*_async_chunk` | scheduler 侧流式分块生产者 | `async_chunk=true`：逐块产出载荷 |
| `*_token_only` | orchestrator 侧占位符构造器 | `async_chunk=false`：只为下游预留 prompt 槽位 |

关键约束：**当已声明 `sync_process_input_func`（即 `*_token_only`）时，不要再写一个无后缀的 `thinker2talker` 函数**——因为 `_select_processor_funcs()` 在非 async 模式下总是优先选 `*_token_only` 钩子，裸函数永远不会被调用。

[docs/contributing/model/adding_omni_model.md:539-540](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/contributing/model/adding_omni_model.md#L539-L540) — 这一「不要重复声明」的契约。

#### 4.3.2 核心流程

阶段转译的整体流程（非 async 路径）：

```text
Stage k 完成
   │
   ▼  (orchestrator.py: _forward_to_next_stage)
保存上游输出: stage_clients[k].set_engine_outputs([output])
   │
   ▼  调用 stage k 的 custom_process_next_stage_input_func
thinker2talker_full_payload(...)   ← worker 侧：把 hidden states/embedding 打包成 OmniPayload，经 connector 发送
   │
   ▼  调用 stage k+1 的 sync_process_input_func
thinker2talker_token_only(...)     ← orchestrator 侧：只为 talker 预留 prompt_token_ids 槽位（填 0 占位）
   │
   ▼  构造下一 stage 请求并投递
build_engine_core_request_from_tokens(...) → next_client.add_request_async(request)
```

注意「重数据走数据面（connector）、轻通知走控制面（stage 队列）」的分工（u3-l4）：

- **重张量**（hidden states、codec codes）由 `*_full_payload` 打包，经 OmniConnector（默认 SharedMemoryConnector）传到下一 stage 的 worker；
- **轻占位**（只是为了让 scheduler 预留 KV-cache 槽位的 `prompt_token_ids=[0]*N`）由 `*_token_only` 在 orchestrator 侧就地构造。

这与 u3-l4 的设计一致：重数据不进 msgpack 控制通道，避免序列化大张量。

模型 forward 如何把张量塞进 `multimodal_output` 供下游按键取：

[docs/contributing/model/adding_omni_model.md:436-462](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/contributing/model/adding_omni_model.md#L436-L462) — 模型 `forward` 用约定 key（如 `"0"`、`"24"`）把 hidden states 放进 `multimodal_outputs`，下游 processor 用同样的 key 取出。

#### 4.3.3 源码精读

以 Thinker→Talker 为例。

**① worker 侧完整载荷生产者**（`thinker2talker_full_payload`）：

[vllm_omni/model_executor/stage_input_processors/qwen3_omni.py:530-613](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/model_executor/stage_input_processors/qwen3_omni.py#L530-L613) — 关键逻辑：

```python
def thinker2talker_full_payload(transfer_manager, pooling_output, request):
    ...
    layers = {
        0: pooling_output.get("hidden_states.layer_0"),     # thinker 第 0 层 = word embedding
        24: pooling_output.get("hidden_states.layer_24"),   # thinker 第 24 层 = accept hidden
    }
    thinker_emb = _layer_tensor(layers, _EMBED_LAYER_KEY)    # "0"
    thinker_hid = _layer_tensor(layers, _HIDDEN_LAYER_KEY)   # "24"
    ...
    # 去掉末尾 stop-token 行，保证行数与 token 对齐
    thinker_emb_prefill = thinker_emb[:-1] if thinker_emb.shape[0] > 1 else thinker_emb
    thinker_hid_prefill = thinker_hid[:-1] if thinker_hid.shape[0] > 1 else thinker_hid
    ...
    payload: OmniPayload = {
        "embed": {
            "prefill": thinker_emb_prefill.detach().cpu(),
            "tts_bos": ..., "tts_eos": ..., "tts_pad": ...,
        },
        "hidden_states": {"output": thinker_hid_prefill.detach().cpu()},
        "ids": {"all": list(all_token_ids), "prompt": list(prompt_token_ids)},
        "meta": {"finished": torch.tensor(True, dtype=torch.bool)},
    }
    ...
    return payload
```

要点：

- **取数据的 key 是模型 forward 时约定的**：`hidden_states.layer_0` / `hidden_states.layer_24` 对应 embedding 与 hidden。这就是指南里强调的「key 在模型 forward 里定义、在 processor 里按键取」。
- **张量 `.detach().cpu()`**：因为重张量要跨进程/跨设备经 connector 传输（u3-l4），需要先搬到 CPU 序列化。
- **去掉末尾 stop-token 行**：保证 embedding 行数与下游 prompt token 数对齐——这是 stage 间对齐的关键细节，否则 talker 的 prefill 会对不齐。
- **payload 结构是 `OmniPayload`（字典）**：与 u2-l3 的 `additional_information`/`model_intermediate_buffer` 一脉相承，是跨阶段传输的标准线缆载荷。

**② orchestrator 侧占位符构造器**（`thinker2talker_token_only`）：

[vllm_omni/model_executor/stage_input_processors/qwen3_omni.py:616-671](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/model_executor/stage_input_processors/qwen3_omni.py#L616-L671) — 关键逻辑：

```python
def thinker2talker_token_only(source_outputs, prompt=None, requires_multimodal_data=False, ...):
    talker_inputs = []
    for thinker_output in source_outputs:
        output = thinker_output.outputs[0]
        prompt_token_ids = _ensure_list(thinker_output.prompt_token_ids)
        output_ids = _ensure_list(output.cumulative_token_ids)
        ...
        prompt_len = _compute_talker_prompt_ids_length(...)   # 算出 talker 需要多少个槽位
        additional_information = to_dict(OmniPayloadStruct(
            speaker=extract_speaker_from_prompt(prompt, index=i),
            language=extract_language_from_prompt(prompt, index=i),
        ))
        talker_inputs.append(OmniTokensPrompt(
            prompt_token_ids=[0] * prompt_len,        # 占位 0，仅用于预留 KV 槽位
            additional_information=additional_information or None,
            multi_modal_data=None, mm_processor_kwargs=None,
        ))
    return talker_inputs
```

要点：

- **`prompt_token_ids=[0]*prompt_len` 全是 0**：这里**不**真正传 token，只占位让 talker 的 scheduler 分配正确长度的 KV-cache。真正的 hidden states 走 connector（① 已发）。
- **`speaker`/`language` 经 `additional_information` 带过去**：因为这些是轻量元数据，且 connector 的 payload 可能不保留原始 prompt 元数据，所以在占位符里冗余一份（代码注释明确说「Keep this fallback until the connector reliably preserves voice metadata」）。
- 这里的 `OmniTokensPrompt`、`additional_information` 正是 u2-l3 讲的扩展 prompt 类型与跨阶段载荷通道。

**③ Talker→Code2Wav**（`talker2code2wav_full_payload`）：

[vllm_omni/model_executor/stage_input_processors/qwen3_omni.py:763-835](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/model_executor/stage_input_processors/qwen3_omni.py#L763-L835) — 把 talker 的 `code_predictor_codes` 按 output token ids 对齐、过滤无效行（codec codebook size 2048），再 `transpose(0,1).reshape(-1)` 拉平成一维，交给 code2wav 解码：

```python
return {
    "codes": {"audio": codec_codes},     # 拉平后的 RVQ codes
    "meta": {"finished": torch.tensor(True, dtype=torch.bool)},
}
```

注意过滤逻辑 `_extract_qwen3_full_payload_codec_rows` 会剔除大于等于 codebook size 的占位行，保证送进 code2wav 的都是合法 codec 码。

#### 4.3.4 代码实践

**实践目标**：跟踪一条数据从 Thinker 的 hidden states 变成 Code2Wav 的 codec 输入的全过程。

**操作步骤**：

1. 在 [stage_input_processors/qwen3_omni.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/model_executor/stage_input_processors/qwen3_omni.py) 里定位三个函数：`thinker2talker_full_payload`（L530）、`thinker2talker_token_only`（L616）、`talker2code2wav_full_payload`（L763）。
2. 列一张「张量变换」表：

| 边 | 上游产出 | 变换 | 下游吃到的载荷 |
|----|----------|------|----------------|
| 0→1 | hidden_states(layer 0/24) + tts embeddings | 去 stop 行、`cpu()` | `embed.prefill` + `hidden_states.output` |
| 1→2 | code_predictor_codes | 过滤无效行、转置拉平 | `codes.audio`（一维 list） |

3. 注意 `talker2code2wav_async_chunk`（L679）与 full 版本的区别：async 版本按 `codec_chunk_frames`（默认 25）切块流式发送，适合实时 TTS。

**需要观察的现象**：每个 `*_full_payload` 函数末尾都返回 `{"meta": {"finished": ...}}`。这个 `finished` 标志是 connector 消费端「等待门」的判据——如果返回 `None`（缺数据），消费端会挂起等待，日志会打印「consumer wait gate may hang」。

**预期结果**：你能解释清楚「Thinker 的 hidden states 行数为什么要和 talker 的 prompt token 数对齐」「为什么 codec codes 要拉平成一维」。

> 本实践为源码阅读型，运行验证**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `thinker2talker_token_only` 里 `prompt_token_ids` 全填 0，而真正的 hidden states 走另一条路？

**参考答案**：重张量（hidden states）太大，不应进 msgpack 控制通道，故经 OmniConnector（共享内存/RDMA）传输（u3-l4）。但 talker 的 scheduler 仍需要知道「要预留多长的 KV-cache」，所以 orchestrator 用一个等长的 0 占位列表告诉它长度。真正喂给 talker 模型的是 connector 送来的 `embed.prefill`，由 talker 的 prompt_embeds overlay 盖到 `inputs_embeds`（见 u4-l1）。

**练习 2**：`*_full_payload` 返回 `None` 会怎样？

**参考答案**：返回 `None` 表示「这一轮还没攒够数据」。connector 消费端有一个等待门（wait gate），它收不到载荷就会持续等待。如果是错误导致的永久 `None`，会触发挂起；代码里多处 `logger.warning(...)` 会在缺数据时提示「consumer wait gate may hang」。

---

### 4.4 多阶段结构化配置与 final_stage 路由

#### 4.4.1 概念说明

前面三节讲的是「模型作者要写的代码」。本节讲框架如何把这些冻结拓扑**编译成结构化配置**，以及最终输出如何在多个 `final_output=True` 的 stage 之间路由。

回到 u2-l2 的结论：配置是「拓扑（代码冻结）+ 部署 YAML + CLI 覆盖」三层合流。具体到多阶段模型：

- **拓扑层**：`PipelineConfig`（冻结，本模型作者写）。
- **结构化层**：`VllmOmniConfig.from_pipeline_config` 把拓扑 + 部署 YAML + CLI 覆盖编译成 `VllmOmniConfig`，内含每个 stage 的 `BaseVllmOmniStageConfig`（或其子类）。
- **部署层**：用户编辑 `vllm_omni/deploy/<model>.yaml`，按 stage 配显存、并行、设备、连接器。

`BaseVllmOmniStageConfig` 把单个 stage 的所有运行参数（模型/加载/缓存/调度/连接器/运行时/并行/量化）聚合成一个对象，并由 `execution_type` 决定选用哪个子类：

[vllm_omni/config/omni_config.py:41-45](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/omni_config.py#L41-L45) — 执行类型到 stage 类型的映射：

```python
_EXECUTION_TYPE_TO_STAGE_WORKER = {
    StageExecutionType.LLM_AR: (StageType.LLM, "ar"),
    StageExecutionType.LLM_GENERATION: (StageType.LLM, "generation"),
    StageExecutionType.DIFFUSION: (StageType.DIFFUSION, None),
}
```

于是 `LLM_AR` 选 `VllmOmniARStageConfig`、`LLM_GENERATION` 选 `VllmOmniGenerationStageConfig`、`DIFFUSION` 选 `VllmOmniDiffusionStageConfig`。

[vllm_omni/config/omni_config.py:936-954](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/omni_config.py#L936-L954) — 三个 stage config 子类；`VllmOmniDiffusionStageConfig` 额外聚合 `OmniStageDiffusionParallelConfig` 与 `_DiffusionConfigProjection`。

#### 4.4.2 核心流程

**编译流程**（`VllmOmniConfig.from_pipeline_config`）：

```text
PipelineConfig (冻结拓扑)
        +
DeployConfig (YAML/CLI)   ──►  按 stage 合并  ──►  VllmOmniConfig
        +                                            ├─ stage_configs: tuple[AR/Gen/Diffusion...]
   CLI overrides                                   ├─ pipeline_config
                                                   └─ orchestrator_config
```

关键点：对每个 stage，框架读 `deploy.stages[stage_id]`（用户的 YAML 段）+ 该 stage 的 CLI 覆盖，按字段类别（quant/model/load/cache/scheduler/runtime/parallel/diffusion）分发到对应子配置，再调用 `_select_processor_funcs` 解析出该 stage 该用哪个 input processor。

**final_stage 路由**：当一条流水线有多个 `final_output=True` 的 stage（如 Qwen3-Omni 同时有 text 与 audio 两个收口），框架根据**用户请求的输出 modality** 反向决定跑到哪一段收口。记 \(F\) 为「请求期望的最终 stage id」，\(S\) 为所有 `final_output=True` 的 stage 集合：

\[
F = \min\{\, s \in S \mid \text{final\_output\_type}(s) \in \text{requested\_modalities} \,\}
\]

即：在「产出用户期望 modality」的所有最终 stage 里，取最小的 stage_id 作为收口点。Orchestrator 推进到该 stage 即停（见 u3-l2「`stage_id < final_stage_id` 才前推」）。

#### 4.4.3 源码精读

**① 结构化 stage 配置的契约**：`BaseVllmOmniStageConfig` 把 `StagePipelineConfig`（拓扑）+ 一组子配置（运行参数）聚合：

[vllm_omni/config/omni_config.py:844-856](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/omni_config.py#L844-L856) — 八个子配置：`model/load/cache/scheduler/connector/runtime/parallel/quantization`，全部有默认值；同时用 `@property` 把拓扑字段（`stage_id`/`model_stage`/`input_sources`/`final_output`/`final_output_type`/`stage_type`/`worker_type`/`scheduler_cls`/`engine_output_type` 等）透传出来，使下游消费者只需面对一个对象。

注意 `worker_type` 与 `scheduler_cls` 都是从 `execution_type` 派生的（[omni_config.py:887-898](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/omni_config.py#L887-L898)）：这就是 u4-l2 讲的「同一 execution_type 串起 worker_type、scheduler、model runner」。

**② 编译入口** `from_pipeline_config`：

[vllm_omni/config/omni_config.py:1255-1311](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/omni_config.py#L1255-L1311) — 核心片段（精简）：

```python
@classmethod
def from_pipeline_config(cls, pipeline_cfg, *, user_deploy_config=None,
                         deploy_config_path=None, cli_overrides=None):
    deploy, loaded_deploy_config_path = _get_deploy_config(
        pipeline_cfg, user_deploy_config, deploy_config_path)
    ...
    if len(pipeline_cfg.stages) <= 1:
        deploy.async_chunk = False                 # 单 stage 不支持 async_chunk
    _validate_async_chunk_support(pipeline_cfg, deploy)
    deploy_by_id = {stage.stage_id: stage for stage in deploy.stages}

    stage_configs = tuple(
        _build_stage_config(
            pipeline_cfg, deploy, topology,
            deploy_by_id.get(topology.stage_id),   # 该 stage 的部署段
            _stage_engine_values(
                deploy_by_id.get(topology.stage_id),
                _stage_cli_overrides(topology.stage_id, cli_overrides)),  # CLI 覆盖
            model=model,
        )
        for topology in pipeline_cfg.stages        # 遍历冻结拓扑的每个 stage
    )
    ...
```

要点：

- **拓扑是源、部署是覆盖**：循环遍历 `pipeline_cfg.stages`（冻结拓扑），用 `deploy_by_id.get(topology.stage_id)` 取该 stage 的用户部署段，二者合流。
- **`_build_stage_config` 按 `execution_type` 分派子类**（[omni_config.py:1086-1106](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/omni_config.py#L1086-L1106)）：`LLM_AR`→`_build_ar_stage_config` 等。
- **单 stage 自动关 async_chunk**：`len(stages) <= 1` 时强制 `async_chunk=False`，因为流式跨阶段没有意义。

**③ 部署 YAML 的真实样貌**：Qwen3-Omni 的生产部署文件展示了用户能改什么：

[vllm_omni/deploy/qwen3_omni_moe.yaml:25-69](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/deploy/qwen3_omni_moe.yaml#L25-L69) — 每个 stage 段独立设置 `max_num_seqs`、`gpu_memory_utilization`、`devices`、`input_connectors`、`default_sampling_params` 等。例如：

```yaml
stages:
  - stage_id: 0          # thinker 在 cuda:0
    devices: "0"
    max_num_seqs: 64
    gpu_memory_utilization: 0.9
  - stage_id: 1          # talker 在 cuda:1，从 stage 0 经共享内存读数据
    devices: "1"
    gpu_memory_utilization: 0.6
    input_connectors:
      from_stage_0: connector_of_shared_memory
  - stage_id: 2          # code2wav 在 cuda:1，从 stage 1 读 codec
    devices: "1"
    gpu_memory_utilization: 0.1
    async_scheduling: false
```

关键观察：**三个 stage 可以分到不同 GPU、各自分配不同显存比例**（thinker 0.9、talker 0.6、code2wav 0.1）。这是多阶段架构的核心收益——按子模型的实际需求切分资源。文件顶部的 `async_chunk: true`（[L15](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/deploy/qwen3_omni_moe.yaml#L15)）启用流式跨阶段分块（对应 4.3 的 `*_async_chunk` 路径）。

**④ final_stage 路由的判定**：

[vllm_omni/entrypoints/omni_base.py:346-355](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/entrypoints/omni_base.py#L346-L355) — `_compute_final_output_stage_ids` 取出所有 `final_output=True` 且 `final_output_type in 请求模态` 的 stage：

```python
def _compute_final_output_stage_ids(self, output_modalities):
    requested_modalities = output_modalities or self.output_modalities
    requested_modalities = [m for m in requested_modalities if m in self.output_modalities]
    if not requested_modalities:
        requested_modalities = self.output_modalities
    return [
        sid
        for sid, stage in enumerate(self._stage_meta_list)
        if getattr(stage, "final_output", False)
        and stage.final_output_type in requested_modalities
    ]
```

落到 Qwen3-Omni：`final_output=True` 的 stage 是 `{0 (text), 2 (audio)}`。

- 用户请求只要文本（`modalities=["text"]`）→ `final_output_stage_ids={0}` → 请求跑到 stage 0 即收口（thinker 直接出文本，不进 talker/code2wav）。
- 用户请求要音频（`modalities=["audio"]`）→ `final_output_stage_ids={2}` → 请求一路跑到 stage 2（code2wav 出波形）。
- 两者都要 → `{0, 2}` 都算完成才算整体完成（Orchestrator 用 `final_output_stage_ids ⊆ finished_final_output_stage_ids` 判定整体完成，见 u3-l2）。

这正是 Qwen3-Omni「既能纯文本理解、又能语音对话」的开关：同一个冻结拓扑，按请求 modality 动态收口。

#### 4.4.4 代码实践

**实践目标**：验证「同一个拓扑，不同输出 modality，跑不同 stage 数量」。

**操作步骤**：

1. 打开 [end2end.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/examples/offline_inference/qwen3_omni/end2end.py)，看输出如何按 `final_output_type` 分流保存：

[examples/offline_inference/qwen3_omni/end2end.py:369-409](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/examples/offline_inference/qwen3_omni/end2end.py#L369-L409) — `final_output_type == "text"` 时把 `output.outputs[0].text` 存 `.txt`；`== "audio"` 时把 `output.outputs[0].multimodal_output["audio"]` 存 `.wav`（注意还处理了 async 模式下音频可能是「分块 list」需 `torch.cat` 的情况）。

2. 用 `--modalities text` 与 `--modalities audio` 分别（概念上）跑一次，对照 `_compute_final_output_stage_ids` 预测每次会经过哪些 stage。

**需要观察的现象**：

- `--modalities text`：每个请求只产生一份 `.txt`（来自 stage 0），不产生 `.wav`；
- `--modalities audio`：每个请求产生一份 `.wav`（来自 stage 2）；若同时要文本，还会同时产生 `.txt`。

**预期结果**：你能解释「为什么 `modalities` 参数会改变请求实际跑的 stage 数」，并能用本节的路由公式回答。

> 完整运行需 Qwen3-Omni 权重与多卡环境；上述现象**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：如果用户既不传 `modalities`，模型会跑到哪个 stage？

**参考答案**：若用户不指定，`requested_modalities` 回退为模型自身的 `output_modalities`（即所有声明的输出模态）。对全量 Qwen3-Omni 而言就是 text + audio，于是 `final_output_stage_ids={0,2}`，请求要跑到 stage 2 才整体完成，同时 stage 0 的文本也会作为中间结果产出。

**练习 2**：为什么 `VllmOmniConfig.from_pipeline_config` 在「单 stage」时强制 `async_chunk=False`？

**参考答案**：`async_chunk`（流式跨阶段分块）的意义是把上游输出**逐块**推向下游 stage，让下游提前开工。只有一个 stage 时没有「下游」，分块毫无意义，反而徒增开销，故强制关闭。`_validate_async_chunk_support` 还会校验：开了 `async_chunk` 却没声明任何 `async_chunk_process_next_stage_input_func` 的多阶段流水线会被拒。

## 5. 综合实践

**任务**：参考 Qwen3-Omni 的三阶段结构，为一个假想的「**AR 编码器 + DiT 生成器**」两阶段模型编写拓扑、数据流说明与部署要点，标注每个 stage 的输入来源。

**场景设定**：假设你要接入一个「文本/图像 → 图像编辑」模型：

- **Stage 0（AR Encoder）**：吃文本 + 可选参考图像，自回归地生成一段**隐式条件向量（latent condition）**，同时输出文本理解结果。
- **Stage 1（DiT Generator）**：吃 stage 0 的 latent condition，做扩散去噪，生成最终图像。

**要求完成**：

1. **拓扑声明骨架**（示例代码，标注 TODO）——仿照 [pipeline.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/model_executor/models/qwen3_omni/pipeline.py) 的写法：

```python
# 示例代码（非项目原有代码）
AR_DIT_PIPELINE = PipelineConfig(
    model_type="my_ar_dit_model",
    model_arch="MyArDitForConditionalGeneration",
    stages=(
        StagePipelineConfig(
            stage_id=0,
            model_stage="encoder",
            execution_type=StageExecutionType.LLM_AR,        # AR 编码器
            input_sources=(),                                  # 起点：用户输入
            final_output=True,                                 # 文本理解也算最终输出
            final_output_type="text",
            owns_tokenizer=True,
            requires_multimodal_data=True,                     # 吃参考图
            hf_config_name="encoder_config",
            engine_output_type="latent",
            custom_process_next_stage_input_func=f"{_PROC}.encoder2dit_full_payload",
            async_chunk_process_next_stage_input_func=f"{_PROC}.encoder2dit_async_chunk",
        ),
        StagePipelineConfig(
            stage_id=1,
            model_stage="generator",
            execution_type=StageExecutionType.DIFFUSION,        # DiT 生成器（扩散）
            input_sources=(0,),                                 # 来自 encoder
            final_output=True,
            final_output_type="image",
            hf_config_name="generator_config",
            engine_output_type="image",
            sync_process_input_func=f"{_PROC}.encoder2dit_token_only",
        ),
    ),
)
```

2. **数据流说明**（表格）——标注每个 stage 的输入来源：

| stage | execution_type | input_sources | 输入来源 | 产出 | 给下游传什么 |
|-------|----------------|---------------|----------|------|--------------|
| 0 encoder | LLM_AR | `()` | 用户文本 + 参考图（多模态） | 文本 + latent condition（layer N hidden） | `encoder2dit_full_payload`：hidden states 经 connector |
| 1 generator | DIFFUSION | `(0,)` | stage 0 的 latent condition（经 connector） | 图像 | 无下游，`final_output_type="image"` |

3. **stage input processor 要点**（列出函数签名与职责，参考 4.3）：
   - `encoder2dit_full_payload`：取 encoder 的 `hidden_states.layer_N`，`.cpu()` 后打包成 `OmniPayload`，去 stop-token 行；
   - `encoder2dit_token_only`：在 orchestrator 侧为 DiT 占位（DiT 是扩散 stage，槽位语义与 AR 不同，需参考 u5-x 扩散请求结构）；
   - `encoder2dit_async_chunk`：若要流式，逐块推 latent（本例 AR 一次性产出，可不实现 async，则 deploy 里 `async_chunk=false`）。

4. **部署要点**：仿照 [qwen3_omni_moe.yaml](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/deploy/qwen3_omni_moe.yaml)，给 stage 1（DiT）配扩散专属参数（如 `cache_strategy`、`tensor_parallel_size`、`sequence_parallel_size`，见 u5-x、u7-x）。

5. **验证清单**（对照本讲四个最小模块）：
   - [ ] 统一模型类按 `model_stage`（`"encoder"`/`"generator"`）分派加载两个子模型；
   - [ ] 拓扑的 `input_sources` 形成 `0→1` 的有向图；
   - [ ] stage 0 的 forward 把 latent 塞进 `multimodal_outputs["N"]`，processor 用同样 key 取；
   - [ ] `_validate_async_chunk_support` 不报错（若不开 async 则不要声明 async processor）。

**完成后**：你应当能向同事讲清「这个两阶段模型，用户请求 `modalities=image` 时跑 2 个 stage、`modalities=text` 时只跑 1 个 stage」的来龙去脉。

> 本综合实践以源码阅读 + 配置编写为主，不涉及实际运行；接入真实模型需结合 u9-l1（diffusion 模型接入）与 u5（diffusion 引擎）完成 DiT 侧实现。

## 6. 本讲小结

- 接入一个 Omni 多阶段模型 = 写**三层**：统一模型类（按 `model_stage` 单态分派子模型）、冻结拓扑（`PipelineConfig`）、阶段输入处理器（一组 `*_full_payload`/`*_async_chunk`/`*_token_only` 函数）。
- 拓扑用 `StagePipelineConfig` 声明，关键字段是 `stage_id`/`model_stage`/`execution_type`/`input_sources`/`final_output`/`final_output_type`；`input_sources` 是有向图的边，写错就断流。
- 阶段间数据靠「重张量走 connector（`*_full_payload`）、轻占位走控制面（`*_token_only`）」分工；模型 forward 用约定 key（如 `"0"`/`"24"`）把 hidden states 放进 `multimodal_output`，下游 processor 用同 key 取。
- 框架用 `VllmOmniConfig.from_pipeline_config` 把「冻结拓扑 + 部署 YAML + CLI 覆盖」编译成每个 stage 的结构化配置，`execution_type` 决定选 `VllmOmniARStageConfig`/`VllmOmniGenerationStageConfig`/`VllmOmniDiffusionStageConfig`。
- `final_stage_id` 由用户请求的输出 modality 反向决定：取「`final_output=True` 且 `final_output_type ∈ 请求模态`」的所有 stage；Qwen3-Omni 因 stage 0/2 都标了最终输出，故能按 text/audio 动态收口。
- 多 stage 可分到不同 GPU、配不同显存/并行/调度策略，这是多阶段架构相对「单一大 forward」的核心收益。

## 7. 下一步学习建议

- **若你的第二阶段是 Diffusion（如本讲综合实践）**：继续读 u9-l1（添加新 Diffusion 模型）学习 DiT pipeline 与 transformer adapter 的接入，以及 u7 系列（注意力后端、序列并行、缓存加速、批处理）。
- **若你接入的是 TTS**：读 u9-l3（添加 TTS 模型），它讲解了 `tts_adapters` 如何把 OpenAI speech 请求归一化成 stage 采样参数。
- **深入阶段间通信**：读 u3-l4（OmniConnector 体系）理解 SharedMemory/RDMA 后端与 D2H2D 传输细节；读 u3-l2（Orchestrator）理解 `_forward_to_next_stage` 如何调用本讲的 processor。
- **深入结构化配置**：读 u2-l2（配置体系）了解 `StageConfigFactory` 与 legacy stage_config 的两套并存迁移。
- **实验性联合执行**：若你想把 AR 与 Diffusion 跑在**同一个 engine/runner**（而非拆成两个 stage），读 u9-l4 的 `experimental/ar_diffusion`（engine.py/runner.py），它提供了与「多 stage 解耦」相对的另一条路径。
- **参考实现**：建议通读 [qwen3_omni/pipeline.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/model_executor/models/qwen3_omni/pipeline.py) 与 [stage_input_processors/qwen3_omni.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/model_executor/stage_input_processors/qwen3_omni.py) 这两个文件，它们是本讲所有概念的最完整落地范本。
