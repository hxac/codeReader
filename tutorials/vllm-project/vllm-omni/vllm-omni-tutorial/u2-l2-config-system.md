# 配置体系：从单模型到多阶段 Stage 配置

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `vllm_omni/config/` 下三类配置的分工：**模型级**（`OmniModelConfig`）、**结构化多阶段**（`omni_config.py` 的 `VllmOmniConfig` + 各 `StageConfig` 子类）、**部署/拓扑**（`stage_config.py` 的 `PipelineConfig` + `DeployConfig`）。
- 指出 `OmniModelConfig` 相对 vLLM `ModelConfig` 新增了哪些字段（`stage_id`、`engine_output_type`、`stage_connector_config` 等），以及它们为什么是「多阶段」所必需的。
- 跟踪一次「从模型名 → 多个 stage 配置」的构建链路，理解 `StageConfigFactory` 如何把**冻结的流水线拓扑** + **用户编辑的 deploy YAML** + **CLI 覆盖**合并成运行时配置。
- 区分两套并存的配置体系：新的**结构化**视图（`VllmOmniConfig`，RFC #4021）与当前引擎仍消费的**遗留** `StageConfig`（OmegaConf 形态），以及它们各自的路由函数。

---

## 2. 前置知识

- **stage（阶段）**：vLLM-Omni 把一个请求拆成顺序执行的若干子任务，每个子任务跑在一个独立子进程里。例如 Qwen2.5-Omni 的「text → audio」请求会依次经过 Thinker、Talker、Code2Wav 三个 stage。这一点在 [u1-l1](u1-l1-project-overview.md) 已建立。
- **ModelConfig**：vLLM 里描述「一个模型怎么加载、怎么跑」的核心配置类（`vllm.config.ModelConfig`），包含模型路径、dtype、最大长度等。本讲的 `OmniModelConfig` 就是它的子类。
- **dataclass / pydantic**：本讲的配置类大量使用 `@dataclass` 与 vLLM 的 `@config` 装饰器（基于 pydantic 做字段校验）。你只需知道它们是「带默认值、可校验类型的字段容器」即可。
- **monkey-patch**：[u2-l1](u2-l1-patch-mechanism.md) 讲过 vLLM-Omni 用补丁改写 vLLM 行为；本讲关注的是**新增**的配置体系（🔴 Added），不碰补丁。

> 一个贯穿全讲的关键认知：vLLM-Omni 的配置**不是一张大表**，而是「**拓扑（代码里冻结）+ 部署（YAML 里编辑）+ 命令行覆盖（运行时叠加）**」三层合流的结果。记住这句话，后面的细节都会归位。

---

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [`vllm_omni/config/model.py`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/model.py) | 定义 `OmniModelConfig`（继承 vLLM `ModelConfig`），是**单个 stage 运行时**的模型级配置。 |
| [`vllm_omni/config/omni_config.py`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/omni_config.py) | **结构化多阶段配置**（RFC #4021）：顶层 `VllmOmniConfig` 与各 `StageConfig` 子类、以及一组 `OmniStage*Config` 子配置。 |
| [`vllm_omni/config/stage_config.py`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/stage_config.py) | **冻结拓扑**（`PipelineConfig`/`StagePipelineConfig`）+ **deploy YAML 解析**（`DeployConfig`/`StageDeployConfig`）+ **遗留 `StageConfig`** + 合并函数 `merge_pipeline_deploy`。 |
| [`vllm_omni/config/config_factory.py`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/config_factory.py) | `StageConfigFactory`：**构建入口**。把模型名解析成 `PipelineConfig`，再合流 deploy/CLI，产出结构化 `VllmOmniConfig` 或遗留 `StageConfig` 列表。 |
| [`vllm_omni/config/pipeline_registry.py`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/pipeline_registry.py) | `OMNI_PIPELINES` 字典：`model_type → PipelineConfig`（或 resolver）的注册表。 |
| `vllm_omni/model_executor/models/<model>/pipeline.py` | 每个模型各自声明自己的**冻结拓扑** `PipelineConfig`，再被注册表收录。 |

---

## 4. 核心概念与源码讲解

### 4.1 OmniModelConfig：单个 stage 的模型级配置

#### 4.1.1 概念说明

vLLM 的 `ModelConfig` 描述「**一个**模型」。但 vLLM-Omni 里一个请求要跑**多个** stage，每个 stage 都需要一个独立的 `ModelConfig`，而且要额外携带「我属于哪个阶段、我的输出该路由给谁、我和下一阶段用什么连接器」这类**多阶段专属信息**。

`OmniModelConfig` 就是「`ModelConfig` + 多阶段字段」。它在引擎真正加载某个 stage 的模型时被实例化，是该 stage 子进程里**实际运行**的模型配置。

> 注意区分两个名字相近的类：`OmniModelConfig`（本节，**运行时**的模型配置，继承 vLLM `ModelConfig`）与 `OmniStageModelConfig`（4.2 节，结构化视图里的一个**子配置**）。前者是引擎真用的，后者只是结构化视图里的一个投影。

#### 4.1.2 核心流程

`OmniModelConfig` 不走普通的 `__init__`，而是用类方法 [`from_vllm_model_config`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/model.py#L248-L281) 从一个**已经校验过**的 vLLM `ModelConfig` 转换而来。这样做的原因写在它的注释里：`ModelConfig.__post_init__` 很贵（要做大量校验），不想再跑一遍。于是它走了一条「绕过校验、手动搬字段」的快路径：

```text
已有的 vLLM ModelConfig（已校验）
        │  from_vllm_model_config(model_config, stage_id=..., model_stage=..., ...)
        ▼
1. add_defaults_to_omni_kwargs   # 给未传入的 omni 字段补默认值
2. _validate_omni_fields         # 只校验 omni 专属字段（用 TypeAdapter，快）
3. object.__new__(cls)           # 绕过 __init__/__post_init__
4. 复制 model_config.__dict__    # 搬 vLLM 字段
5. 覆盖 omni_kwargs              # 叠加 omni 字段
6. _maybe_override_text_config   # 多阶段模型按 hf_config_name 重算 text_config
        ▼
   OmniModelConfig 实例
```

#### 4.1.3 源码精读

**新增字段**集中在 [`OmniModelConfig`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/model.py#L120-L142) 的类体里，关键字段如下：

```python
stage_id: int = 0                       # 这个 stage 在流水线里的序号
model_stage: str = "thinker"            # 角色名，如 "thinker" / "talker" / "code2wav"
model_arch: str | None = None           # 覆盖 checkpoint 自报的 architectures
worker_type: str | None = None          # 模型类型，如 "ar" / "generation"
engine_output_type: str | None = None   # 输出类型，路由给对应处理器："image"/"audio"/"latents"
stage_connector_config: dict[str, Any] = field(
    default_factory=lambda: {"name": "SharedMemoryConnector", "extra": {}}
)                                       # 与下一阶段的连接器配置
```

- `stage_id` / `model_stage`：回答「我是第几阶段、扮演什么角色」。多阶段编排器（Orchestrator）靠它们路由数据。
- `engine_output_type`：回答「我吐出来的是什么」。引擎据此把输出交给对应的输出处理器（文本 / 图像 / 音频 / latent）。
- `stage_connector_config`：回答「我把结果交给下一阶段用什么通道」。默认是单机的 `SharedMemoryConnector`。
- `hf_config_name`：多阶段模型（如 Qwen-Omni）的 HF config 里嵌套了 `thinker_config`/`talker_config` 等子配置，这个字段告诉本 stage「去取哪个子配置」。

`architectures` 属性展示了 `model_arch` 的覆盖逻辑——给了就用覆盖值，没给就回退到 checkpoint 自报：

```python
@property
def architectures(self) -> list[str]:
    if self.model_arch:
        return [self.model_arch]
    return super().architectures
```
（见 [`model.py:149-155`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/model.py#L149-L155)）

构造快路径的核心是「手动搬字典」，见 [`model.py:265-267`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/model.py#L265-L267)：

```python
omni_cfg = object.__new__(cls)
omni_cfg.__dict__.update(model_config.__dict__)   # 搬 vLLM 字段
omni_cfg.__dict__.update(omni_kwargs)             # 叠 omni 字段
```

而为了避免「绕过 `__init__` 导致带 `default_factory` 的字段没被初始化」，[`add_defaults_to_omni_kwargs`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/model.py#L307-L325) 会显式补默认值。校验则只针对 omni 专属字段，由 [`_validate_omni_fields`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/model.py#L283-L305) 用「omni 字段集合 = 全部字段 − ModelConfig 字段」算出差集再做校验。

#### 4.1.4 代码实践

**实践目标**：确认 `OmniModelConfig` 相对 vLLM `ModelConfig` 多了哪些字段。

**操作步骤**：

1. 打开 [`vllm_omni/config/model.py`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/model.py)，定位 `OmniModelConfig`（约 83 行）。
2. 注意 `_validate_omni_fields` 里这一行（约 292 行）：

   ```python
   omni_fields = set(cls.__dataclass_fields__) - set(ModelConfig.__dataclass_fields__)
   ```

   它用「集合差」精确算出「omni 新增字段」。
3. 在仓库根目录用 Python 交互式地把这个差集打印出来（**示例代码，非项目原有**）：

   ```python
   # python -c "..."
   from vllm.config import ModelConfig
   from vllm_omni.config import OmniModelConfig
   omni_only = set(OmniModelConfig.__dataclass_fields__) - set(ModelConfig.__dataclass_fields__)
   print(sorted(omni_only))
   ```

**需要观察的现象**：打印出的列表应当包含 `stage_id`、`model_stage`、`engine_output_type`、`stage_connector_config`、`hf_config_name`、`omni_kv_config` 等。

**预期结果**：你得到的就是「OmniModelConfig 相对 ModelConfig 的全部新增字段」。待本地验证：若 vLLM 版本不同，`ModelConfig` 字段集可能有差异，导致差集略有变化。

#### 4.1.5 小练习与答案

**练习 1**：`stage_connector_config` 的默认值是什么？为什么默认选 `SharedMemoryConnector`？

> **参考答案**：默认是 `{"name": "SharedMemoryConnector", "extra": {}}`（见 [`model.py:131-136`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/model.py#L131-L136)）。单机多 stage 共享同一进程组，共享内存零拷贝、零配置即可用，是最稳的默认；跨节点才需要换成 Mooncake/Yuanrong 等 RDMA 连接器（见 4.3 节示例）。

**练习 2**：为什么 `from_vllm_model_config` 用 `object.__new__(cls)` 而不是直接 `OmniModelConfig(...)`？

> **参考答案**：直接构造会触发 `ModelConfig.__post_init__` 的全量校验，而传入的 `model_config` 已经校验过一次，重复校验既慢又可能因字段已经被改写而报错。用 `object.__new__` 绕过 `__init__`，再手动 `__dict__.update` 搬字段，既避免重复校验又能完整继承字段。

---

### 4.2 结构化多阶段配置：VllmOmniConfig 与各 StageConfig 子类

#### 4.2.1 概念说明

`OmniModelConfig` 是「**运行时、单 stage、模型级**」的配置。但站在更高视角，我们还想用一个对象描述「**整条流水线**长什么样、每个 stage 的调度/缓存/并行/连接器如何」——这就是 `omni_config.py` 提供的**结构化配置**。

它是 RFC #4021 的 Phase-2 **增量**产物（文件头注释明确写了 "additive for Phase 2 of RFC #4021"）。所谓「增量」，是指它与当前引擎仍消费的**遗留** `StageConfig`（4.3 节）并存：两套都从同一份「拓扑 + deploy + CLI」派生，只是结构化视图更类型化、更易于程序消费，目的是逐步替换遗留路径。

层级关系一目了然：

```text
VllmOmniConfig                         # 顶层容器：整条流水线
├── pipeline_config : PipelineConfig        # 冻结拓扑（4.3 节）
├── stage_configs  : tuple[StageConfigType] # 每个 stage 一个结构化配置
│     ├── VllmOmniARStageConfig            #   对应 LLM_AR（自回归）
│     ├── VllmOmniGenerationStageConfig    #   对应 LLM_GENERATION（单步生成）
│     └── VllmOmniDiffusionStageConfig     #   对应 DIFFUSION（扩散）
└── orchestrator_config : VllmOmniOrchestratorConfig  # 仅编排器进程消费
```

三个 stage 配置类的共同基类是 `BaseVllmOmniStageConfig`，它把「每个 stage 都需要的那些子配置」聚合到一起。

#### 4.2.2 核心流程

`VllmOmniConfig` 通过类方法 [`from_pipeline_config`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/omni_config.py#L1255-L1310) 构建，完成三层合流：

```text
PipelineConfig（冻结拓扑）   ─┐
DeployConfig（deploy YAML） ─┼─►  VllmOmniConfig
cli_overrides（命令行）      ─┘

内部步骤：
1. _get_deploy_config        # 选 user/默认/空 deploy
2. CLI 覆盖叠到 deploy 上     # async_chunk、dtype、quantization 等管线级字段
3. _apply_platform_overrides # 按 npu/xpu 平台覆盖 stage 字段
4. 单 stage 强制 async_chunk=False
5. 对每个 topology（stage 拓扑）：
     _stage_engine_values    # 把扁平的 deploy 引擎参数投影成 8 类子配置
     _build_stage_config     # 按 execution_type 选 builder → 选对应 StageConfig 子类
6. 组装 orchestrator_config
```

**关键**：选哪个 `StageConfig` 子类，完全由 stage 拓扑里的 `execution_type` 决定。分派表见 [`_STAGE_CONFIG_BUILDERS`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/omni_config.py#L1079-L1083)：

| `StageExecutionType` | 构建函数 | 产物类 |
| --- | --- | --- |
| `LLM_AR` | `_build_ar_stage_config` | `VllmOmniARStageConfig` |
| `LLM_GENERATION` | `_build_generation_stage_config` | `VllmOmniGenerationStageConfig` |
| `DIFFUSION` | `_build_diffusion_stage_config` | `VllmOmniDiffusionStageConfig` |

#### 4.2.3 源码精读

**顶层容器** [`VllmOmniConfig`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/omni_config.py#L1241-L1253) 只持有三块：

```python
class VllmOmniConfig:
    pipeline_config: PipelineConfig
    stage_configs: tuple[StageConfigType, ...]
    orchestrator_config: VllmOmniOrchestratorConfig = field(default_factory=...)
```

并提供 `stage_by_id(stage_id)` 按序号取 stage（见 [`omni_config.py:1249-1253`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/omni_config.py#L1249-L1253)）。

**公共基类** [`BaseVllmOmniStageConfig`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/omni_config.py#L845-L856) 聚合了 8 个子配置 + 拓扑：

```python
class BaseVllmOmniStageConfig:
    stage_pipeline_config: StagePipelineConfig          # 来自冻结拓扑
    model_config: OmniStageModelConfig                  # 模型行为
    load_config: OmniStageLoadConfig                    # 加载行为
    cache_config: OmniStageCacheConfig                  # 显存/前缀缓存
    scheduler_config: OmniStageSchedulerConfig          # 调度（max_num_seqs 等）
    connector_config: OmniStageConnectorConfig          # 跨阶段连接器
    runtime_config: OmniStageRuntimeConfig              # 进程放置/副本
    parallel_config: OmniStageParallelConfig            # 并行（TP/DP/PP）
    quantization_config: _QuantizationConfigType = None
```

注意它大量用 `@property` 把拓扑里的字段（`stage_id`、`stage_type`、`worker_type`、`scheduler_cls`、`final_output`…）「投影」出来，避免数据冗余（见 [`omni_config.py:858-933`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/omni_config.py#L858-L933)）。例如：

```python
@property
def stage_type(self) -> StageType:
    stage_type, _ = _resolve_execution_mode(self.stage_pipeline_config.execution_type)
    return stage_type
```

**三个子类的差异**：

- [`VllmOmniARStageConfig`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/omni_config.py#L937-L938) 与 [`VllmOmniGenerationStageConfig`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/omni_config.py#L942-L943) 目前是空壳，只继承基类（注释说后续迁移会逐步填充）。
- [`VllmOmniDiffusionStageConfig`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/omni_config.py#L947-L951) 多了两块扩散专属配置：

  ```python
  class VllmOmniDiffusionStageConfig(BaseVllmOmniStageConfig):
      parallel_config: OmniStageDiffusionParallelConfig   # 含 ulysses/ring/cfg/vae 并行
      diffusion_config: _DiffusionConfigProjection        # 扩散专属（cache、attention 等）
  ```

**连接器子配置** [`OmniStageConnectorConfig`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/omni_config.py#L323-L333) 把「本 stage 默认连接器」与「按边显式指定的输入/输出连接器」分开：

```python
class OmniStageConnectorConfig:
    stage_connector: dict[str, Any] = ...   # 默认 {"name": "SharedMemoryConnector", "extra": {}}
    output_connectors: dict[str, Any] | None = None
    input_connectors: dict[str, Any] | None = None
```

#### 4.2.4 代码实践

**实践目标**：用一个真实的冻结拓扑，验证「`execution_type` → `StageConfig` 子类」的分派。

**操作步骤**：

1. 打开 Qwen2.5-Omni 的拓扑声明 [`qwen2_5_omni/pipeline.py`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/../../vllm_omni/model_executor/models/qwen2_5_omni/pipeline.py#L18-L61)，记录三个 stage 的 `execution_type`：
   - stage 0 `thinker` → `StageExecutionType.LLM_AR`
   - stage 1 `talker` → `StageExecutionType.LLM_AR`
   - stage 2 `code2wav` → `StageExecutionType.LLM_GENERATION`
2. 对照 4.2.2 的分派表，预测每个 stage 会落到哪个 `StageConfig` 子类。
3. （可选）在仓库根目录写一段**示例代码**（非项目原有）调用工厂构造，打印每个 stage 的类名：

   ```python
   from vllm_omni.config.config_factory import StageConfigFactory
   cfg = StageConfigFactory.create_from_model(
       model="<你的 Qwen2.5-Omni 路径或 HF 名>",
       trust_remote_code=True,
       cli_overrides={},
       deploy_config_path="qwen2_5_omni.yaml",
   )
   for s in cfg.stage_configs:
       print(s.stage_id, s.model_stage, type(s).__name__)
   ```

**需要观察的现象**：stage 0/1 打印 `VllmOmniARStageConfig`，stage 2 打印 `VllmOmniGenerationStageConfig`。

**预期结果**：分派结果与拓扑里的 `execution_type` 一一对应。待本地验证：需要可访问的模型权重与对齐的 vLLM 版本才能跑通；若无环境，仅做步骤 1–2 的源码阅读对照即可得出结论。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `VllmOmniARStageConfig` 和 `VllmOmniGenerationStageConfig` 现在是空壳，却仍要分开成两个类？

> **参考答案**：它们对应两种不同的执行语义（多步自回归 vs 单步生成），即便当前字段相同，分开类可以让 `StageConfigType` 联合类型在类型层面精确表达「这条流水线里有哪些 stage」，也为后续把 AR 专属或 Generation 专属字段从基类下沉留出扩展点（见类注释的迁移说明）。

**练习 2**：`BaseVllmOmniStageConfig` 里为什么用 `@property` 投影 `stage_id`、`stage_type`，而不是直接存一份副本？

> **参考答案**：因为这些值的**唯一真相源**是 `stage_pipeline_config`（来自冻结拓扑）。存副本会造成两份可能不一致的数据；用 property 投影保证「改拓扑即改投影」，单一数据源。

---

### 4.3 部署配置与流水线拓扑：stage_config.py 与 StageConfigFactory

#### 4.3.1 概念说明

`omni_config.py` 解决「**怎么用类型化对象描述一条流水线**」，但「这条流水线的拓扑长什么样」和「用户想怎么部署它」是两件不同的事：

- **拓扑（PipelineConfig / StagePipelineConfig）**：**冻结在代码里**，由模型作者在 `model_executor/models/<model>/pipeline.py` 声明，`@dataclass(frozen=True)`。它定义「有几个 stage、谁连谁、谁是入口、谁是最终输出」。用户**不应**改它。
- **部署（DeployConfig / StageDeployConfig）**：**写在 deploy YAML 里**，是用户**唯一需要编辑**的配置文件。它定义「每个 stage 用哪张卡、多少副本、什么采样参数、什么连接器、并行度多少」。
- **CLI 覆盖**：运行时（如 `--omni` 服务的命令行参数）再叠一层，优先级最高。

`stage_config.py` 同时承载了拓扑定义、deploy 解析、以及**遗留**的 `StageConfig`（当前引擎实际消费的形态）。而 `config_factory.py` 的 `StageConfigFactory` 是把这三层合流的总入口。

> 历史包袱提醒：当前引擎还消费**遗留** `StageConfig`（OmegaConf 形态，见 [`stage_config.py:977`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/stage_config.py#L977)）；4.2 的结构化 `VllmOmniConfig` 是它的**继任者**。`StageConfigFactory` 同时提供两条路径，由消费方选择。

#### 4.3.2 核心流程

**入口与两条路径**。`StageConfigFactory` 提供两个对外方法（见 [`config_factory.py:311`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/config_factory.py#L311) 与 [`config_factory.py:344`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/config_factory.py#L344)）：

```text
                         StageConfigFactory
                                │
   get_pipeline_config(model, ...)   ← 解析模型名 → PipelineConfig
       │  try_infer_model_type()      （读 HF config 推断 model_type）
       │  查 OMNI_PIPELINES 注册表
       │  hf_architectures 兜底 / deploy.pipeline 显式覆盖
       ▼
   PipelineConfig（冻结拓扑）
       │
       ├──► create_from_model()           ──► VllmOmniConfig.from_pipeline_config()  （结构化，新）
       │
       └──► create_legacy_stage_configs_from_model()
                    └──► _create_legacy_from_registry()
                              └──► merge_pipeline_deploy() ──► list[StageConfig]  （遗留，当前引擎用）
```

**拓扑如何解析**。[`get_pipeline_config`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/config_factory.py#L220-L277) 按优先级确定用哪条拓扑：

1. deploy YAML 里 `pipeline:` 字段显式指定（最高优先级）；
2. 由 `model_type` 在 `OMNI_PIPELINES` 注册表里直接命中；
3. 用 HF config 的 `architectures` 与各 `PipelineConfig.hf_architectures` 做交集兜底（解决 `model_type` 碰撞，如 MiMo Audio 报告 `qwen2`）。

**遗留合并**。[`merge_pipeline_deploy`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/stage_config.py#L890-L974) 把「拓扑 + deploy + 平台覆盖 + CLI」合成一组遗留 `StageConfig`：对每个 stage 拓扑，拼装 `yaml_engine_args`（引擎参数）、`yaml_runtime`（进程/副本）、`yaml_extras`（采样参数/连接器）。

#### 4.3.3 源码精读

**两个枚举**定位阶段类型。`StageType` 是粗分类，`StageExecutionType` 是细分类（合并了旧的 stage 与 worker 概念，见 [`stage_config.py:168-182`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/stage_config.py#L168-L182)）：

```python
class StageType(str, Enum):
    LLM = "llm"
    DIFFUSION = "diffusion"

class StageExecutionType(str, Enum):
    LLM_AR = "llm_ar"            # 自回归
    LLM_GENERATION = "llm_generation"  # 单步生成
    DIFFUSION = "diffusion"      # 扩散
```

两者通过一张映射表互转（[`stage_config.py:770-774`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/stage_config.py#L770-L774)）：`LLM_AR→(LLM, "ar")`、`LLM_GENERATION→(LLM, "generation")`、`DIFFUSION→(DIFFUSION, None)`。

**冻结拓扑** [`PipelineConfig`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/stage_config.py#L248-L317) 的核心是 `stages: tuple[StagePipelineConfig, ...]`，并提供拓扑校验 [`validate()`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/stage_config.py#L299-L317)：检查 stage_id 不重复、`input_sources` 引用的源必须存在且不能自引用、至少有一个入口（`input_sources` 为空的 stage）。一个真实的 3 阶段拓扑例子见 [`qwen2_5_omni/pipeline.py:18-61`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/model_executor/models/qwen2_5_omni/pipeline.py#L18-L61)，关键片段：

```python
QWEN2_5_OMNI_PIPELINE = PipelineConfig(
    model_type="qwen2_5_omni",
    default_deploy_config_name="qwen2_5_omni.yaml",
    stages=(
        StagePipelineConfig(stage_id=0, model_stage="thinker",
            execution_type=StageExecutionType.LLM_AR, input_sources=(),
            final_output=True, final_output_type="text", ...),
        StagePipelineConfig(stage_id=1, model_stage="talker",
            execution_type=StageExecutionType.LLM_AR, input_sources=(0,), ...),
        StagePipelineConfig(stage_id=2, model_stage="code2wav",
            execution_type=StageExecutionType.LLM_GENERATION, input_sources=(1,),
            final_output=True, final_output_type="audio", ...),
    ),
)
```

`input_sources=(0,)` 表示「我的输入来自 stage 0」，这就是请求在 stage 间流动的「边」。

**deploy YAML**。用户编辑的 `DeployConfig`（[`stage_config.py:461`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/stage_config.py#L461)）分两块：管线级（`dtype`、`quantization`、`async_chunk`…，对所有 stage 生效）与每 stage 的 `StageDeployConfig`（`devices`、`num_replicas`、`max_num_seqs`、`output_connectors`…，可逐 stage 不同）。真实例子见 [`deploy/qwen2_5_omni.yaml`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/deploy/qwen2_5_omni.yaml#L18-L70)：每 stage 指定 `devices`、`max_num_seqs`、`default_sampling_params`，并通过 `platforms:` 块按 npu/xpu 覆盖。

**连接器在 YAML 里怎么写**。跨节点连接器（Mooncake/Yuanrong）在 `connectors:` 块定义具名连接器，再在 stage 的 `output_connectors` / `input_connectors` 按边引用（见 [`deploy/dynin_omni_multiconnector.yaml:12-63`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/deploy/dynin_omni_multiconnector.yaml#L12-L63)）：

```yaml
connectors:
  mooncake_connector:
    name: MooncakeConnector
    extra: { host: "...", master: "...", ... }
stages:
  - stage_id: 0
    output_connectors:
      to_stage_1: mooncake_connector   # 边 to_stage_1 用 mooncake
  - stage_id: 1
    input_connectors:
      from_stage_0: mooncake_connector
```

**遗留 `StageConfig`**（[`stage_config.py:977`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/stage_config.py#L977-L999)）是当前引擎实际吃的形态，关键方法 [`to_omegaconf()`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/stage_config.py#L1001-L1065) 把 `yaml_engine_args`/`yaml_runtime`/`yaml_extras` + CLI 覆盖拼成最终的 OmegaConf 配置字典。

#### 4.3.4 代码实践

**实践目标**：阅读 `merge_pipeline_deploy`，确认「单 stage 流水线会被强制关掉 async_chunk」。

**操作步骤**：

1. 打开 [`stage_config.py:902-904`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/stage_config.py#L902-L904)，阅读这段：

   ```python
   # async_chunk is irrelevant for single-stage pipelines, so we always disable it
   if len(pipeline.stages) <= 1:
       deploy.async_chunk = False
   ```

2. 紧接着（约 916-927 行）还有一段对**有跨 stage 边**的多阶段流水线的检查：若 `async_chunk=True` 却没有任何 stage 声明 `async_chunk_process_next_stage_input_func`，会抛 `ValueError`。

**需要观察的现象**：单阶段模型（如纯扩散文生图）即便 YAML 写了 `async_chunk: true`，运行时也会被改回 `False`。

**预期结果**：这是纯源码阅读型实践，结论可直接从源码得出——「async_chunk 只对有跨 stage 边的多阶段流水线有意义」。

#### 4.3.5 小练习与答案

**练习 1**：`PipelineConfig.validate()` 会报哪几类拓扑错误？

> **参考答案**：四类（见 [`stage_config.py:299-317`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/stage_config.py#L299-L317)）：没有 stage、stage_id 重复、`input_sources` 引用了不存在的 stage、`input_sources` 引用自己、没有任何入口 stage。

**练习 2**：`get_pipeline_config` 解析拓扑的三级优先级是什么？

> **参考答案**：① deploy YAML 的 `pipeline:` 字段显式覆盖（最高）；② `model_type` 在 `OMNI_PIPELINES` 注册表命中；③ 用 HF `architectures` 与各 `PipelineConfig.hf_architectures` 做交集兜底（见 [`config_factory.py:234-276`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/config_factory.py#L234-L276)）。

---

## 5. 综合实践

把本讲三块知识串起来：为一个**假想的「AR 编码器 + DiT 生成器」两阶段模型**，手写一份最小的「结构化 stage 配置」。注意：vLLM-Omni 里「结构化配置」不是一张手写的 JSON，而是由 **Python 拓扑声明** + **deploy YAML** 合流产生。所以本实践分两部分。

### 5.1 声明冻结拓扑（示例代码，非项目原有）

仿照 [`qwen2_5_omni/pipeline.py`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/model_executor/models/qwen2_5_omni/pipeline.py#L18-L61)，为「AR 编码器（stage 0）→ DiT 生成器（stage 1）」声明拓扑：

```python
# 示例代码（非项目原有）：my_ar_dit/pipeline.py
from vllm_omni.config.stage_config import (
    PipelineConfig, StageExecutionType, StagePipelineConfig,
)

MY_AR_DIT_PIPELINE = PipelineConfig(
    model_type="my_ar_dit",
    model_arch="MyArDitForConditionalGeneration",  # 你注册的模型架构名
    stages=(
        # stage 0：AR 编码器，自回归，入口，产出 latent
        StagePipelineConfig(
            stage_id=0,
            model_stage="ar_encoder",
            execution_type=StageExecutionType.LLM_AR,   # ← 决定 StageConfig 子类
            input_sources=(),                            # 入口：无上游
            engine_output_type="latent",
            owns_tokenizer=True,
        ),
        # stage 1：DiT 生成器，扩散，消费 stage 0 的 latent，产出图像
        StagePipelineConfig(
            stage_id=1,
            model_stage="dit_generator",
            execution_type=StageExecutionType.DIFFUSION,  # ← 决定 StageConfig 子类
            input_sources=(0,),                           # 边：来自 stage 0
            final_output=True,
            final_output_type="image",
            engine_output_type="image",
        ),
    ),
)
```

### 5.2 每个 stage 落到哪个 StageConfig 子类

对照 4.2.2 的分派表（[`omni_config.py:1079-1083`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/omni_config.py#L1079-L1083)）：

| stage | `execution_type` | 解析到的结构化 `StageConfig` 子类 | 理由 |
| --- | --- | --- | --- |
| 0 `ar_encoder` | `LLM_AR` | **`VllmOmniARStageConfig`** | `_build_ar_stage_config` |
| 1 `dit_generator` | `DIFFUSION` | **`VllmOmniDiffusionStageConfig`** | `_build_diffusion_stage_config`（多出 `OmniStageDiffusionParallelConfig` + `_DiffusionConfigProjection`） |

### 5.3 编写 deploy YAML（示例，非项目原有）

把 **并行（parallel）** 与 **连接器（connector）** 放在用户可编辑的 YAML 里（参照 [`dynin_omni_multiconnector.yaml`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/deploy/dynin_omni_multiconnector.yaml) 的结构）：

```yaml
# 示例（非项目原有）：my_ar_dit.yaml
async_chunk: false          # 管线级：两 stage 暂用同步交接
dtype: bfloat16             # 管线级：对所有 stage 生效

stages:
  - stage_id: 0             # AR 编码器：单卡
    devices: "0"
    max_num_seqs: 4
    tensor_parallel_size: 1

  - stage_id: 1             # DiT 生成器：2 路张量并行
    devices: "1,2"
    tensor_parallel_size: 2
    ulysses_degree: 2       # 扩散序列并行（仅 Diffusion stage 有意义）
    output_connectors: {}   # 最终输出 stage，无需向下游连
```

说明几个关键点：

- **parallel**：AR stage 用普通 `tensor_parallel_size`（落在 `OmniStageParallelConfig`）；DiT stage 还可用 `ulysses_degree`/`ring_degree` 等（仅 `VllmOmniDiffusionStageConfig` 的 `OmniStageDiffusionParallelConfig` 才有这些字段，见 [`omni_config.py:364-380`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/omni_config.py#L364-L380)）。
- **connector**：本例两 stage 同机，**不写连接器**即默认 `SharedMemoryConnector`（见 [`OmniStageConnectorConfig`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/omni_config.py#L323-L333) 与 [`OmniModelConfig.stage_connector_config`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/model.py#L131-L136)）。若跨节点，则在 stage 0 的 `output_connectors` 与 stage 1 的 `input_connectors` 按边引用一个具名 Mooncake 连接器，写法同 4.3.3 的 YAML 片段。

**验证方式（待本地验证）**：把这个拓扑注册到 `OMNI_PIPELINES`，用 `StageConfigFactory.create_from_model(...)` 构造，断言 `cfg.stage_configs[0]` 是 `VllmOmniARStageConfig`、`cfg.stage_configs[1]` 是 `VllmOmniDiffusionStageConfig`，并检查 DiT stage 的 `parallel_config.sequence_parallel_size == 2`。

---

## 6. 本讲小结

- vLLM-Omni 的配置是「**拓扑（代码冻结）+ 部署（YAML 编辑）+ CLI（运行时覆盖）**」三层合流，而非单张表。
- `OmniModelConfig`（继承 vLLM `ModelConfig`）是**单个 stage 运行时**的模型级配置，新增 `stage_id`、`model_stage`、`engine_output_type`、`stage_connector_config` 等多阶段字段，用 `from_vllm_model_config` 的「绕过校验、手动搬字段」快路径构造。
- 结构化视图 `VllmOmniConfig` 是顶层容器：`pipeline_config`（拓扑）+ `stage_configs`（每 stage 一个 `StageConfig` 子类）+ `orchestrator_config`；`BaseVllmOmniStageConfig` 聚合 8 个子配置，`execution_type` 决定子类（AR/Generation/Diffusion）。
- 拓扑 `PipelineConfig`/`StagePipelineConfig` 用 `input_sources` 表达 stage 间的边，`validate()` 保证拓扑合法；`DeployConfig`/`StageDeployConfig` 是用户编辑的部署层。
- `StageConfigFactory` 是总入口：`create_from_model()` 产出新的结构化 `VllmOmniConfig`，`create_legacy_stage_configs_from_model()` 经 `merge_pipeline_deploy` 产出当前引擎仍消费的遗留 `StageConfig`（OmegaConf 形态）。两套并存，逐步迁移。

---

## 7. 下一步学习建议

- 配置只是「声明」，真正把多个 stage 跑起来的是**编排器**。下一讲 [u3-l1 多阶段架构总览](u3-l1-async-omni-architecture.md) 会讲 `AsyncOmniEngine` 如何用本讲产出的 stage 配置启动多个子进程并编排请求流转。
- 想看连接器（本讲的 `stage_connector_config` / `output_connectors`）在运行时如何真正传输张量，可读 [u3-l4 OmniConnector 体系](u3-l4-omni-connectors.md)。
- 想深入扩散 stage 的并行字段（`ulysses_degree`/`ring_degree`/`cfg_parallel_size`）背后的机制，可读 [u7-l4 并行策略](u7-l4-parallel-strategies.md)。
- 建议继续精读的源码：`vllm_omni/config/config_factory.py` 的 `get_pipeline_config`（拓扑解析三级优先级）与 `stage_config.py` 的 `merge_pipeline_deploy`（三层合流的实现细节）。
