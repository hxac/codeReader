# 添加新 Diffusion 模型

## 1. 本讲目标

本讲面向「想把一个新的扩散（Diffusion）模型接入 vLLM-Omni」的二次开发者。读完本讲后，你应当能够：

- 说出接入一个新扩散模型需要改动/新增哪些文件，并能独立写出一一份「接入清单」。
- 把 HuggingFace **diffusers** 里的 Transformer 与 Pipeline 改造成 vLLM-Omni 的原生实现：去掉 Mixin、换成 `Attention` 层、声明 attention `role`、增加 `od_config` 支持。
- 理解「能力声明」与「注册」两条接入主线：用 Protocol 声明能力（图像输入、步级执行、组件发现），在 `registry.py` 注册 pipeline 类与 pre/post 处理函数。
- 区分「原生实现（高收益但需改代码）」与「diffusers_adapter 黑盒适配（零代码但能力受限）」两条接入路径。
- 知道如何为新模型可选地启用 TP / SP / CFG 并行与 TeaCache / Cache-DiT 缓存加速。

本讲承接 **u5-l4（Diffusion Pipeline 与去噪数据流）**：那里讲的是「pipeline 内部如何去噪」，本讲讲的是「如何把一个全新的 pipeline 塞进框架并被正确调用」。

## 2. 前置知识

- **diffusers**：HuggingFace 的扩散模型库。大多数开源扩散模型的参考实现都来自这里，我们改写的就是它的 `Pipeline` 与 `Transformer` 类。
- **DiT（Diffusion Transformer）**：用 Transformer 做去噪主干的一类扩散模型，本讲默认新模型是 DiT 结构。
- **CFG（Classifier-Free Guidance）**：用「正/负 prompt 双前向再合并」提升生成质量的技巧，详见 u5-l4。
- **pipeline 与 transformer 的分工**：`pipeline` 编排「文本编码 → 多步去噪 → VAE 解码」整条流水线；`transformer`（DiT）只负责每一步「预测噪声」的前向。这是本讲最关键的一组名词。
- **role（注意力角色）**：给每个注意力位点起一个字符串名字，便于用户在不改模型代码的情况下按名字切换注意力后端（详见 u7-l1）。
- **注册表（registry）**：一个「架构名 → 实现类」的映射表，框架用它按名字发现并加载模型。

> 如果你还不熟悉 vLLM-Omni 的扩散子系统分层（Engine / Scheduler / Executor / Worker / Pipeline），建议先读 u5-l1～u5-l4。本讲聚焦在 Pipeline 这一层「往上」的接入工作。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `docs/contributing/model/adding_diffusion_model.md` | 官方接入指南，本讲的主骨架。讲义中的步骤号与它对齐。 |
| `vllm_omni/diffusion/registry.py` | 模型注册表。pipeline 类、pre/post 处理函数的发现与加载都在这里。 |
| `vllm_omni/diffusion/attention/layer.py` | 扩散注意力层 `Attention`，改写 transformer 时要用的核心组件。 |
| `vllm_omni/diffusion/attention/selector.py` | 按 `role` 解析注意力后端的四级优先级选择器。 |
| `vllm_omni/diffusion/attention/backends/abstract.py` | `AttentionBackend` / `AttentionMetadata` 抽象定义。 |
| `vllm_omni/diffusion/models/interface.py` | 能力协议（Protocol）：`SupportImageInput` / `SupportsStepExecution` / `SupportsComponentDiscovery`。 |
| `vllm_omni/diffusion/models/diffusers_adapter/pipeline_diffusers_adapter.py` | 零代码黑盒适配器 `DiffusersAdapterPipeline`。 |
| `vllm_omni/diffusion/models/qwen_image/` | 最佳参考实现：`pipeline_qwen_image_edit.py`、`qwen_image_transformer.py`、`cfg_parallel.py`。 |
| `vllm_omni/diffusion/worker/request_batch.py` | `DiffusionRequestBatch`，pipeline `forward` 的输入容器。 |
| `examples/offline_inference/image_to_image/image_edit.py` | 离线图像编辑示例脚本，实践任务依据。 |

## 4. 核心概念与源码讲解

接入一个新扩散模型，本质上是在回答三个问题：

1. **模型长什么样？** → 改写 Transformer（4.2）。
2. **怎么把 prompt 变成图像/视频/音频？** → 改写 Pipeline（4.3）。
3. **框架怎么发现并加载它？** → 注册表与加载器（4.4）。

外加两条「捷径/增强」：完全不改代码的黑盒适配器（4.5 上半），以及让原生模型跑得更快的并行与缓存（4.5 下半）。我们先从「全景」建立这份心智地图。

---

### 4.1 全景：新模型的接入清单与目录约定

#### 4.1.1 概念说明

vLLM-Omni 的扩散推理遵循一条固定链路：

> 用户 prompt → `OmniDiffusionRequest` →（可选预处理）→ **Pipeline 执行** →（后处理）→ 图像/视频/音频

新模型要做的，就是在这条链路里**替换掉「Pipeline 执行」这一环**，并把自己注册到框架的发现表里。其余的请求管理、调度、多进程执行、批处理都已经由 Engine/Scheduler/Executor/Worker（u5-l1～u5-l3）准备好，**模型开发者不需要碰**。

这是 vLLM-Omni「编排者不下场」哲学的直接体现：模型只负责「前向」，框架负责「跑」。

#### 4.1.2 核心流程

官方指南把接入拆成 5 步，本讲按这个骨架展开：

1. **改写 Transformer** —— 去 diffusers Mixin、换 `Attention` 层、声明 `role`、加 `od_config`。
2. **改写 Pipeline** —— 换基类、改 `__init__`/`forward`、抽 pre/post 处理、加权重加载。
3. **注册模型** —— 在 `registry.py` 登记架构名与处理函数。
4. **加示例脚本** —— 复用现成的 `examples/offline_inference/*`。
5. **测试** —— 与 diffusers 基线对比质量/速度，并补 L4 功能测试。

一张「接入清单」速记表（本讲的中心产出物）：

| 维度 | 需要做的事 | 涉及文件 |
|---|---|---|
| Transformer | 去 Mixin、换 `Attention`、声明 `role`、（可选）`_sp_plan`/`_repeated_blocks` | `diffusion/models/<name>/<name>_transformer.py` |
| Pipeline | 改基类、`__init__` 加 `od_config`、`forward(DiffusionRequestBatch)` 返回 `DiffusionOutput` | `diffusion/models/<name>/pipeline_<name>.py` |
| 处理函数 | 写 `get_<name>_post_process_func`（必需）/`get_<name>_pre_process_func`（图像编辑等） | 同 pipeline 文件 |
| 能力声明 | 按需继承 `SupportImageInput` / `SupportsComponentDiscovery` / `SupportsStepExecution` | pipeline 文件 |
| 注册 | `_DIFFUSION_MODELS` + `_DIFFUSION_POST_PROCESS_FUNCS`（+ `_DIFFUSION_PRE_PROCESS_FUNCS`） | `diffusion/registry.py` |
| 导出 | `__init__.py` 暴露 pipeline/transformer/处理函数 | `diffusion/models/<name>/__init__.py` |

#### 4.1.3 源码精读

官方指南规定的目录结构（命名约定）：

[`docs/contributing/model/adding_diffusion_model.md:43-53`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/contributing/model/adding_diffusion_model.md#L43-L53) —— 规定每个模型是一个独立目录，含 `__init__.py`、`pipeline_xxx.py`、`xxx_transformer.py`。

命名约定要点：

- **目录名**：小写 + 下划线，如 `qwen_image`、`flux`、`wan2_2`。
- **pipeline 文件**：`pipeline_<任务>.py`，如 `pipeline_qwen_image_edit.py`。
- **transformer 文件**：`<类名小写>_transformer.py`，如 `qwen_image_transformer.py`。

pipeline 的「输入输出契约」由两个数据结构锁定，它们决定了 `forward` 的签名：

[`vllm_omni/diffusion/worker/request_batch.py:60-122`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/worker/request_batch.py#L60-L122) —— `DiffusionRequestBatch` 把「一条或多条独立的 `OmniDiffusionRequest`」打包，对外暴露 `.prompts`、`.sampling_params`、`.request_ids` 等兼容属性。pipeline 的 `forward` 收到的就是这个对象，而不是裸 prompt。

> 注意：`DiffusionRequestBatch.prompts` 是一个 list，每个元素代表**一条逻辑请求的 prompt**。是否真的把多条请求合并进一次前向（批处理），取决于 pipeline 是否声明 `supports_request_batch = True`（见 4.3 与 u7-l5）。不声明则框架每次只塞一条请求进来。

#### 4.1.4 代码实践

**实践目标**：把「接入清单」这张表变成你自己的项目笔记，并对照一个真实模型目录核对完整性。

**操作步骤**：

1. 在 `vllm_omni/diffusion/models/` 下任选一个模型目录（推荐 `qwen_image/`）。
2. 用 `ls` 列出该目录下的文件，逐个对应到上面「接入清单」表格的「涉及文件」列。
3. 找出：哪些文件是 transformer、哪些是 pipeline、`__init__.py` 导出了哪些符号。

**需要观察的现象**：你会看到 `qwen_image/` 目录里既有 `pipeline_qwen_image.py`（文生图），又有 `pipeline_qwen_image_edit.py`（图像编辑）——**同一个模型目录可以容纳多个 pipeline 文件**，对应不同任务，每个都独立注册。

**预期结果**：你能说出「接入清单」每一行在 `qwen_image/` 里分别对应哪个文件。如果你选的模型缺少某一类文件（例如没有 `_transformer.py`，而是直接复用上游），记录下来——那是该模型的特殊之处。

#### 4.1.5 小练习与答案

**练习 1**：如果要接入一个「文生音频」模型，它的 pipeline 文件和 transformer 文件应该叫什么名字（按命名约定）？

> **参考答案**：pipeline 文件可命名为 `pipeline_<name>.py`（如 `pipeline_audiox.py`），transformer 文件按 transformer 类名小写命名为 `<name>_transformer.py`。关键是「目录名小写下划线、pipeline 文件以 `pipeline_` 开头、transformer 文件以 `_transformer` 结尾」。

**练习 2**：为什么 pipeline 的 `forward` 收到的是 `DiffusionRequestBatch` 而不是一个 `str` prompt？

> **参考答案**：因为框架需要统一接口来支持「单请求」与「多请求批处理」两种模式。`DiffusionRequestBatch` 把 prompt、采样参数、request_id 打包，pipeline 通过 `.prompts`/`.sampling_params` 等属性读取，框架在背后决定是否合并多个请求。直接传 `str` 会丢失 request_id、采样参数和批处理能力。

---

### 4.2 改造 Transformer：从 diffusers 到 vLLM-Omni

#### 4.2.1 概念说明

Transformer（DiT）是扩散模型里最核心、也最耗时的去噪网络。diffusers 的实现通常继承若干 `Mixin`（工具基类）并用自带的 attention 计算。接入 vLLM-Omni 时，我们要做两件事：

- **去掉 diffusers 的 Mixin 与训练专用代码**：换成纯 `nn.Module`，让 vLLM-Omni 自己的权重加载器接管。
- **把注意力计算换成 vLLM-Omni 的 `Attention` 层**：这是接入的「心脏」，它带来 role 感知的后端选择、序列并行、KV 缓存量化等能力（详见 u7-l1、u7-l2）。

#### 4.2.2 核心流程

改写 Transformer 的固定动作（对应指南 Step 1）：

1. **去 Mixin**：`ModelMixin`/`AttentionModuleMixin`/`ConfigMixin` 等全部去掉，基类改为 `nn.Module`。
2. **换 Attention**：在 `__init__` 里构造 `vllm_omni.diffusion.attention.layer.Attention`，**不要在 forward 里每次新建**。
3. **声明 role**：每个 `Attention` 位点传一个 `role` 字符串（如 `"self"`/`"cross"`），可选 `role_category` 做类别回退。
4. **加 `od_config`**：构造函数增加 `od_config: OmniDiffusionConfig | None = None`，保存 `parallel_config` 供并行使用。
5. **去训练代码**：删掉梯度检查点、dropout 等。
6. **（可选）声明并行/编译计划**：`_sp_plan`、`_repeated_blocks`、`packed_modules_mapping`。

注意力的张量形状契约很重要：vLLM-Omni 的 `Attention` 要求 Q/K/V 形状为 `[batch, seq_len, num_heads, head_dim]`，与 diffusers 默认布局可能不同。

#### 4.2.3 源码精读

`Attention` 层是改写 transformer 的核心。它的 `__init__` 接收 `role`/`role_category`，并在构造期就把后端解析好：

[`vllm_omni/diffusion/attention/layer.py:40-98`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/layer.py#L40-L98) —— `Attention.__init__` 把 `role` 存为属性，调用 `get_attn_backend_for_role(...)` 解析后端，并据此实例化 `AttentionImpl`。这里还体现一个关键点：**后端选择只发生在构造期，换后端等于重建模型**。

role 感知选择的四级优先级（exact role → role_category → default → 平台默认）由 selector 实现：

[`vllm_omni/diffusion/attention/selector.py:97-153`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/selector.py#L97-L153) —— `get_attn_backend_for_role`。前三级在 `attention_config.resolve_with_source`，第四级委托平台（CUDA 上逐级降级 TRTLLM→…→FLASH_ATTN→SDPA）。模型作者只需声明 `role`，**「能用谁/默认选谁」完全归平台**。

`AttentionMetadata` 是 forward 时传给 `Attention` 的元数据容器，至少要传 `attn_mask`：

[`vllm_omni/diffusion/attention/backends/abstract.py:62-75`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/backends/abstract.py#L62-L75) —— `AttentionMetadata`。最常用的就是 `attn_mask=...`，框架会把 mask 包在这里传进去。

指南对 role 声明的约定表（self / cross 的判别）：

[`docs/contributing/model/adding_diffusion_model.md:148-174`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/contributing/model/adding_diffusion_model.md#L148-L174) —— 指出 `self`（Q/K/V 同源）、`cross`（K/V 来自 encoder）两种约定；多模态特殊位点可用「点命名 role + role_category」组合；跨注意力若 K/V 在各 rank 上复制，需传 `skip_sequence_parallel=True` 跳过 SP 切分。

参考实现里，`QwenImageTransformer2DModel` 用类属性声明了三件事，分别对应「编译加速」「量化打包」「序列并行」：

[`vllm_omni/diffusion/models/qwen_image/qwen_image_transformer.py:933-941`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/qwen_image/qwen_image_transformer.py#L933-L941) —— `_repeated_blocks = ["QwenImageTransformerBlock"]`（告诉框架哪些重复块可做 `torch.compile`）、`_layerwise_offload_blocks_attrs`（逐块 CPU offload）、`packed_modules_mapping`（QKV 合并后量化映射）。

[`vllm_omni/diffusion/models/qwen_image/qwen_image_transformer.py:954-971`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/qwen_image/qwen_image_transformer.py#L954-L971) —— `_sp_plan`：序列并行计划。声明在哪些子模块边界上对哪些输出/输入张量按哪一维切分（`split_dim`/`split_output`/`auto_pad`）与在哪一层 gather（`proj_out`）。框架会在加载期读这个计划自动挂上 SP 钩子（见 4.4），模型代码本身无需手写 SP 通信。

#### 4.2.4 代码实践

**实践目标**：理解「声明 role」如何影响后端选择，而不改模型代码。

**操作步骤**：

1. 阅读 `vllm_omni/diffusion/attention/selector.py` 的 `get_attn_backend_for_role`。
2. 假设你的 transformer 有两个注意力位点：一个 `role="self"`（DiT 自注意力），一个 `role="cross"`（文本条件交叉注意力）。
3. 写出用户在 CLI 传 `--diffusion-attention-config.per_role.cross.backend TORCH_SDPA` 后，两个位点分别命中哪一级、走哪个后端。

**需要观察的现象**：`self` 位点没有显式配置，会落到「平台默认」第四级；`cross` 位点命中「exact match」第一级，用 `TORCH_SDPA`。

**预期结果**：你能画出 selector 的四级判定流程，并解释「role 只影响配置命中、不影响数值」。**待本地验证**：在装有 FlashAttention 的机器上启动一个含 cross-attention 的模型，观察日志中两处 `Resolved diffusion attention backend ... for role=...` 是否如你所判。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Attention` 必须在 `__init__` 里构造，而不能在 `forward` 里每次新建？

> **参考答案**：后端选择与 `AttentionImpl` 实例化发生在构造期（`layer.py` 的 `get_attn_backend_for_role` 调用）。在 forward 里新建会：(1) 每步重建模型，开销巨大；(2) 破坏 `torch.compile` 对重复块的图捕获；(3) 让 SP/Ring 等需要持久 `ring_runner` 的策略失效。

**练习 2**：`_repeated_blocks` 和 `_sp_plan` 各自服务于哪项加速？

> **参考答案**：`_repeated_blocks` 服务于 **`torch.compile`**——框架自动编译列表中的重复块。`_sp_plan` 服务于 **序列并行（SP）**——声明张量在哪些边界切分/gather，框架据此挂上 SP 钩子，让长序列拆到多卡。

---

### 4.3 改造 Pipeline：生命周期与输入输出契约

#### 4.3.1 概念说明

Pipeline 编排「文本编码 → 多步去噪 → VAE 解码」。改造它，本质是**把 diffusers 的 `__call__` 接口，换成 vLLM-Omni 的 `forward(DiffusionRequestBatch) -> DiffusionOutput` 契约**，并把图片预处理/后处理拆出来单独注册，让框架能在合适时机调用。

此外，pipeline 还通过**继承协议（Protocol）**来声明自己具备哪些能力（图像输入、步级执行、组件发现等），框架据此决定如何调度。

#### 4.3.2 核心流程

改写 Pipeline 的固定动作（对应指南 Step 2）：

1. **换基类**：去掉 `DiffusionPipeline`/`LoraLoaderMixin`，改为 `nn.Module`。
2. **改 `__init__`**：增加 `od_config` 与 `prefix` 参数；**手动加载**各组件（scheduler/text_encoder/vae/tokenizer）而非 `register_modules`；用 `weights_sources` 声明权重来源；用 `get_transformer_config_kwargs` 构造 transformer。
3. **改 `__call__` → `forward`**：签名改为 `forward(req: DiffusionRequestBatch) -> DiffusionOutput`（或 `list[DiffusionOutput]`）；从 `req.prompts`/`req.sampling_params` 取参数；用 `DiffusionOutput(output=...)` 包裹输出。
4. **抽 pre/post 处理**：写 `get_<name>_post_process_func`（必需，latent→PIL 图）与 `get_<name>_pre_process_func`（图像编辑等场景，输入图预处理并写回 request）。
5. **加权重加载**：`load_weights` 方法 + `weights_sources` 列表。
6. **（可选）声明能力**：继承 `SupportImageInput` / `SupportsComponentDiscovery` / `SupportsStepExecution`；声明 `supports_request_batch`。

> 一个常被忽略但很重要的点：**post-processing 之所以要拆出来**，是因为 VAE 解码后的「latent → PIL」是 CPU/IO 密集的，框架把它独立成可在主进程跑的函数，避免占用 GPU worker；pre-processing 同理（在请求进入 worker 前完成）。

#### 4.3.3 源码精读

指南给出的 `__call__ → forward` 改写模板：

[`docs/contributing/model/adding_diffusion_model.md:343-417`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/contributing/model/adding_diffusion_model.md#L343-L417) —— 签名从 `@torch.no_grad() def __call__(self)` 改为 `def forward(self, req: DiffusionRequestBatch) -> list[DiffusionOutput]`；展示如何从 `req.prompts`/`req.sampling_params` 提取 prompt、步数、guidance、尺寸，以及图像编辑场景如何从 `prompt["multi_modal_data"]["image"]` 取输入图；最后用 `DiffusionOutput(output=images)` 包裹。

最佳参考实现 `QwenImageEditPipeline` 的类声明，体现「基类 + 能力协议」的组合写法：

[`vllm_omni/diffusion/models/qwen_image/pipeline_qwen_image_edit.py:223-245`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/qwen_image/pipeline_qwen_image_edit.py#L223-L245) —— 继承 `nn.Module, SupportImageInput, QwenImageCFGParallelMixin, DiffusionPipelineProfilerMixin, SupportsComponentDiscovery`，并用 `_dit_modules/_encoder_modules/_vae_modules` 三个类属性声明组件位置（供 CPU offload / HSDP 使用）。

`__init__` 里手动加载各组件、声明 `weights_sources`、用 `od_config` 构造 transformer：

[`vllm_omni/diffusion/models/qwen_image/pipeline_qwen_image_edit.py:230-310`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/qwen_image/pipeline_qwen_image_edit.py#L230-L310) —— 关键点：`self.weights_sources = [DiffusersPipelineLoader.ComponentSource(...)]` 声明 transformer 权重来源；`from_pretrained` 手动加载 scheduler/text_encoder/vae；`QwenImageTransformer2DModel(od_config=od_config, quant_config=..., **transformer_kwargs)` 把配置透传给 transformer。

`forward` 的实际签名与「从 request 取参数」模式：

[`vllm_omni/diffusion/models/qwen_image/pipeline_qwen_image_edit.py:679-731`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/qwen_image/pipeline_qwen_image_edit.py#L679-L731) —— 从 `req.prompts[0]` 取 prompt/negative_prompt；从 `req.sampling_params` 取 `num_inference_steps`/`true_cfg_scale`/`guidance_scale`/`height`/`width`；图像编辑特有的「从 `additional_information` 取预处理后的图」（说明 pre-process 已在前面把结果写回 request）。

post-process 函数模板（latent 张量 → PIL 图像）：

[`vllm_omni/diffusion/models/qwen_image/pipeline_qwen_image_edit.py:132-153`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/qwen_image/pipeline_qwen_image_edit.py#L132-L153) —— 闭包工厂模式：`get_qwen_image_edit_post_process_func(od_config)` 返回一个 `post_process_func(images)`。它先读 VAE config 算 `vae_scale_factor`，再用 diffusers 的 `VaeImageProcessor.postprocess` 转格式。

pre-process 函数（图像编辑场景，把输入图预处理后写回 request）：

[`vllm_omni/diffusion/models/qwen_image/pipeline_qwen_image_edit.py:57-129`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/qwen_image/pipeline_qwen_image_edit.py#L57-L129) —— `pre_process_func(request: OmniDiffusionRequest)`：从 prompt 的 `multi_modal_data` 取原始图 → resize/预处理 → 把结果塞进 `prompt["additional_information"]["preprocessed_image"]`，并校正 `sampling_params.height/width`。注意它**返回被改写后的 request**，框架会把这个 request 继续往下游传。

能力协议定义在 `interface.py`，pipeline 通过继承来「声明」而非「实现」：

[`vllm_omni/diffusion/models/interface.py:25-28`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/interface.py#L25-L28) —— `SupportImageInput`：声明支持图像输入（带默认颜色格式 RGB）。

[`vllm_omni/diffusion/models/interface.py:48-76`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/interface.py#L48-L76) —— `SupportsStepExecution`：声明支持步级执行（要求实现 `prepare_encode`/`denoise_step`/`step_scheduler`/`post_decode` 四段，对应 u5-l4 的步级数据流与 u7-l5 的连续批处理）。

[`vllm_omni/diffusion/models/interface.py:78-100`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/interface.py#L78-L100) —— `SupportsComponentDiscovery`：用 `_dit_modules`/`_encoder_modules`/`_vae_modules`/`_resident_modules` 告诉框架 pipeline 内部结构，供 CPU offload 与 HSDP 切分定位子模块。

> 这些协议是 `runtime_checkable` 的 `Protocol`，框架用 `isinstance(pipeline, SomeProtocol)` 来探测能力，**不强制实现具体方法签名之外的逻辑**（步级执行协议除外，它要求实现四个方法）。

#### 4.3.4 代码实践

**实践目标**：理解 pipeline 的输入输出契约，能看懂一个陌生 pipeline 的 forward。

**操作步骤**：

1. 打开 `vllm_omni/diffusion/models/qwen_image/pipeline_qwen_image_edit.py` 的 `forward`（L679 起）。
2. 列出它从 `req`（`DiffusionRequestBatch`）读取了哪些字段、又从 `req.sampling_params` 读取了哪些字段。
3. 找到它最终 `return DiffusionOutput(...)` 的地方，确认输出张量的来源（VAE 解码后的 latent→image）。

**需要观察的现象**：图像编辑 pipeline 的「输入图」不是直接从 prompt 读的，而是从 `additional_information["preprocessed_image"]` 读——这正是 pre-process 函数提前写进去的。这说明 **pre-process 与 forward 之间靠 request 上的 `additional_information` 传参**。

**预期结果**：你能画出「prompt dict → pre_process（写 additional_information）→ forward（读 additional_information）→ post_process」的数据传递链。

#### 4.3.5 小练习与答案

**练习 1**：post-process 函数为什么用「闭包工厂 `get_xxx_post_process_func(od_config)` 返回一个函数」，而不是直接定义一个普通函数？

> **参考答案**：因为 post-process 需要 `od_config`（至少要 `model` 路径去读 VAE config 算 `vae_scale_factor`）。用闭包工厂可以在加载期把 `vae_scale_factor`、`image_processor` 等一次性算好并捕获进闭包，之后每次调用 post-process 不再重复读配置。框架也用统一的「函数名字符串」从注册表加载它（见 4.4），闭包工厂签名一致便于统一调用。

**练习 2**：一个 pipeline 同时继承 `SupportImageInput` 和 `SupportsComponentDiscovery` 意味着什么？

> **参考答案**：`SupportImageInput` 告诉框架「这个 pipeline 接受图像输入」（API 层据此允许 image content）；`SupportsComponentDiscovery` 告诉框架「pipeline 内部的 DiT/encoder/VAE 分别在哪些属性上」，框架据此做 CPU offload（DiT 与 encoder 互斥占 GPU）与 HSDP 切分。两者声明的是不同维度的能力。

---

### 4.4 注册与自动加载：registry.py 的发现机制

#### 4.4.1 概念说明

改完 transformer 和 pipeline 后，框架并不会自动认识它们——必须**注册**。`vllm_omni/diffusion/registry.py` 是扩散模型的「发现中心」：它维护三张表（pipeline 类、pre 处理、post 处理），并提供 `initialize_model` 在 worker 进程里按 `model_class_name` 把模型实例化出来。

理解注册表的关键，是搞清「架构名（arch）」的流转：模型的 HuggingFace config 里有一个字段决定了 `model_class_name`，框架用这个名字去三张表里查实现。

#### 4.4.2 核心流程

注册与加载的两条主线：

**注册（开发时填表）**：

1. 在 `_DIFFUSION_MODELS` 里加一行：`"架构名": (目录, 文件名, 类名)`。
2. 在 `_DIFFUSION_POST_PROCESS_FUNCS` 里加一行：`"架构名": "处理函数名字符串"`。
3. （图像编辑等）在 `_DIFFUSION_PRE_PROCESS_FUNCS` 里加一行。
4. 在模型目录的 `__init__.py` 导出 pipeline/transformer/处理函数。

**加载（运行时）**：

1. worker 进程拿到 `od_config.model_class_name`。
2. `initialize_model(od_config)` → `DiffusionModelRegistry._try_load_model_cls(...)` 懒加载类。
3. 配置量化 → 在 `set_current_diffusion_config` 上下文里 `model_class(od_config=od_config)` 实例化。
4. 配置 VAE 优化 → 应用 SP 钩子（若 transformer 有 `_sp_plan`）。
5. 处理函数则由 `get_diffusion_pre/post_process_func(od_config)` 经 `_load_process_func` 按名字加载。

#### 4.4.3 源码精读

`_DIFFUSION_MODELS` 三元组注册表（节选 Qwen 系列）：

[`vllm_omni/diffusion/registry.py:22-43`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/registry.py#L22-L43) —— 每个 arch 映射到 `(mod_folder, mod_relname, cls_name)`。注意 Qwen 系列有 4 个 pipeline 共享 `qwen_image` 目录、各用不同文件——印证「一个目录多 pipeline」。

`DiffusionModelRegistry` 把上面的字典转成 vLLM 的懒加载注册表：

[`vllm_omni/diffusion/registry.py:317-325`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/registry.py#L317-L325) —— 用 `_LazyRegisteredModel` 包装，类真正被 import 是在第一次 `_try_load_model_cls` 时（懒加载，避免启动时拖入所有重依赖）。

`initialize_model` 是 worker 侧的总加载入口：

[`vllm_omni/diffusion/registry.py:350-408`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/registry.py#L350-L408) —— 关键三步：(1) `_try_load_model_cls` 按 `model_class_name` 取类；(2) `_prepare_diffusion_quant_config` 配置量化（含平台 packed_modules_mapping）；(3) **在 `set_current_diffusion_config(od_config)` 上下文里** `model_class(od_config=od_config)` 实例化——这个上下文就是 u5-l4 提到的「让 Attention 层构造期能读到 diffusion config」的机制。之后还配置 VAE slicing/tiling 与分布式 VAE、并调用 `_apply_sequence_parallel_if_enabled` 挂 SP 钩子。

SP 钩子是「声明式」的：模型只写 `_sp_plan`，框架自动应用：

[`vllm_omni/diffusion/registry.py:410-490`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/registry.py#L410-L490) —— `_apply_sequence_parallel_if_enabled`：在 `transformer`/`transformer_2`/`dit`/`unet` 等属性上找 `_sp_plan`，按 `ulysses_degree`/`ring_degree`/`allgather_degree` 构造 `SequenceParallelConfig` 并 `apply_sequence_parallel(transformer, sp_config, plan)`。这就是 u7-l2 序列并行的落地点——**模型只声明切分计划，通信钩子由框架挂**。

post/pre 处理函数的注册表与按名字加载：

[`vllm_omni/diffusion/registry.py:493-551`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/registry.py#L493-L551) —— `_DIFFUSION_POST_PROCESS_FUNCS`（每个 pipeline 必有一个 post 处理）。

[`vllm_omni/diffusion/registry.py:561-587`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/registry.py#L561-L587) —— `_DIFFUSION_PRE_PROCESS_FUNCS`（只有图像编辑等需要输入预处理的模型才登记）。

[`vllm_omni/diffusion/registry.py:660-692`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/registry.py#L660-L692) —— `_load_process_func`：用 `_DIFFUSION_MODELS` 的 `(mod_folder, mod_relname)` 定位模块，`importlib.import_module` 后 `getattr(module, func_name)` 取函数并 `func(od_config)` 调用（即 4.3 的闭包工厂）。**这解释了为什么处理函数必须放在 pipeline 同文件里**。

> 小细节：`_NO_CACHE_ACCELERATION`（registry.py:327-331）登记了 `NextStep11Pipeline`、`AudioXPipeline` 等「不支持缓存加速」的 pipeline，框架会据此强制关闭 TeaCache/Cache-DiT。

#### 4.4.4 代码实践

**实践目标**：跟踪一次「按架构名加载模型」的完整路径。这是本讲的核心代码实践。

**操作步骤**：

1. 在 `registry.py` 的 `_DIFFUSION_MODELS` 里找到 `"QwenImageEditPipeline"` 那一行，记下它的三元组 `("qwen_image", "pipeline_qwen_image_edit", "QwenImageEditPipeline")`。
2. 读 `initialize_model`（L350-L408），确认它如何用 `od_config.model_class_name` 找到这个类。
3. 读 `_load_process_func`（L660-L692），确认它如何用同样的三元组定位到 `get_qwen_image_edit_pre_process_func` 与 `get_qwen_image_edit_post_process_func`。
4. 对照 4.1 的「接入清单」，补全你自己的清单：在 `registry.py` 里要加哪几行？

**需要观察的现象**：pipeline 类、post 函数、pre 函数**都用同一个 arch 名作为 key**，且 pre/post 函数必须与 pipeline 类**在同一文件**（因为 `_load_process_func` 用 pipeline 的 `(mod_folder, mod_relname)` 去定位函数所在模块）。

**预期结果**：你能写出接入一个新模型 `MyPipeline` 时，`registry.py` 需要新增的 3 行（或 2 行，若无 pre 处理）确切代码。

#### 4.4.5 小练习与答案

**练习 1**：如果你的 pre-process 函数放在了 `pipeline_<name>.py`，但 post-process 函数放在了另一个文件 `<name>_utils.py`，会发生什么？

> **参考答案**：post 函数加载会失败。因为 `_load_process_func` 用 `_DIFFUSION_MODELS[arch]` 的 `(mod_folder, mod_relname)` 拼 module 路径 `vllm_omni.diffusion.models.<mod_folder>.<mod_relname>`，只在这个模块里 `getattr`。约定就是「pre/post 处理函数必须与 pipeline 类在同一文件」。

**练习 2**：为什么 `initialize_model` 要在 `with set_current_diffusion_config(od_config):` 上下文里实例化模型？

> **参考答案**：transformer 的 `Attention.__init__`（4.2）会调用 `get_current_diffusion_config_or_none()` 读全局 diffusion config 来解析注意力后端。只有在这个上下文里实例化，Attention 构造期才能拿到正确的 `diffusion_attention_config`。这就是 u5-l4 提到的「保存-恢复」上下文机制的落地点。

---

### 4.5 黑盒适配器与可选加速

#### 4.5.1 概念说明

不是所有模型都值得花力气写原生实现。vLLM-Omni 提供了两条接入路径：

- **原生实现**（4.2～4.4）：改 transformer/pipeline、注册。能拿到全部加速（TP/SP/CFG/Cache/步级批处理），但要写代码。
- **黑盒适配器 `DiffusersAdapterPipeline`**：把整条 diffusers pipeline 直接包起来，**零代码上线**任意 diffusers 模型，但放弃 CFG 并行、序列并行、缓存加速、步级连续批处理。

接入后，原生模型还可**可选地**启用加速（对应指南的 Advanced Features）：TP、SP、CFG 并行、TeaCache/Cache-DiT、CPU offload、`torch.compile`、步级执行。

#### 4.5.2 核心流程

**黑盒适配路径**：

1. 用 `--diffusion-load-format diffusers` 让框架路由到 `DiffusersAdapterPipeline`。
2. `load_weights` 里调 `DiffusionPipeline.from_pretrained()` 把整条 pipeline 拉起来。
3. `forward` 里把 `DiffusionRequestBatch` 翻译成 diffusers `__call__` 的 kwargs，调用后把 `images`/`frames`/`audios` 包成 `DiffusionOutput`。
4. 在 `_raise_unsupported_features` 里**主动拒绝**不兼容的特性（CFG/SP/Cache/eager）。

**原生加速路径（声明式，大部分不用写代码）**：

| 加速 | 模型侧动作 | 启用方式 |
|---|---|---|
| `torch.compile` | transformer 加 `_repeated_blocks` | 默认开启（非 eager） |
| TP | Linear 换 vLLM 并行层 | `tensor_parallel_size=N` |
| CFG 并行 | pipeline 继承 `CFGParallelMixin` 实现 `diffuse()` | `cfg_parallel_size=N` |
| SP | transformer 加 `_sp_plan` | `ulysses_degree`/`ring_degree` |
| TeaCache | 写 extractor + 注册多项式系数 | `cache_backend="tea_cache"` |
| Cache-DiT | 标准模型自动；复杂架构写自定义 cache config | `cache_backend="cache_dit"` |
| 步级执行 | 实现 `prepare_encode/denoise_step/step_scheduler/post_decode` | `step_execution=True` |

#### 4.5.3 源码精读

`DiffusersAdapterPipeline` 的定位与能力边界（类注释明确列出**不支持**项）：

[`vllm_omni/diffusion/models/diffusers_adapter/pipeline_diffusers_adapter.py:54-86`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/diffusers_adapter/pipeline_diffusers_adapter.py#L54-L86) —— 类声明：`supports_request_batch = False`、`supports_step_execution = False`。注释点明不支持 CFG 并行、序列并行、TeaCache/Cache-DiT、步级执行。

`load_weights` 直接调 diffusers 的 `from_pretrained`：

[`vllm_omni/diffusion/models/diffusers_adapter/pipeline_diffusers_adapter.py:92-123`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/diffusers_adapter/pipeline_diffusers_adapter.py#L92-L123) —— `DiffusionPipeline.from_pretrained(model_id, **load_kwargs)` 拉起整条 pipeline，再 `.to(device)`，并按需开 VAE slicing/tiling、CPU offload、设注意力后端。

`forward` 全权委托给 diffusers `__call__`：

[`vllm_omni/diffusion/models/diffusers_adapter/pipeline_diffusers_adapter.py:181-189`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/diffusers_adapter/pipeline_diffusers_adapter.py#L181-L189) —— `_build_call_kwargs(req)` 把请求翻译成 kwargs，`self._pipeline(**kwargs)` 调用，`_wrap_output` 把 `images`/`frames`/`audios` 包成 `DiffusionOutput`。

不兼容特性的「前置拒绝」：

[`vllm_omni/diffusion/models/diffusers_adapter/pipeline_diffusers_adapter.py:195-232`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/diffusers_adapter/pipeline_diffusers_adapter.py#L195-L232) —— `_raise_unsupported_features` 在 `__init__` 里就检查：若用户同时开了 `cfg_parallel_size>1` / `sequence_parallel_size>1` / `cache_backend != none` / `enforce_eager`，直接 `NotImplementedError`。这是「fail-fast」设计——不让用户在跑了一半才发现特性不被支持。

CFG 并行的参考实现（`diffuse` 方法 + mixin）：

[`vllm_omni/diffusion/models/qwen_image/cfg_parallel.py:28-68`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/qwen_image/cfg_parallel.py#L28-L68) —— `QwenImageCFGParallelMixin.diffuse(...)`：去噪循环里对正/负 prompt 双前向再合并。通用动作（all_gather 分摊、合并）在基类 `CFGParallelMixin`，各模型只定制 `diffuse` 骨架。这正是 u5-l4 讲的 CFG 双前向落地。

指南对各项高级特性的「快速设置」清单：

[`docs/contributing/model/adding_diffusion_model.md:692-823`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/contributing/model/adding_diffusion_model.md#L692-L823) —— 逐项给出 TP / CFG / SP / 步级执行 / TeaCache / Cache-DiT / CPU offload 的模型侧动作与启用参数。

#### 4.5.4 代码实践

**实践目标**：学会判断「这个模型该走原生还是黑盒适配」，并对照示例脚本看加速参数怎么传。

**操作步骤**：

1. 读 `examples/offline_inference/image_to_image/image_edit.py` 的 `main`（约 L486-L592）。
2. 找到它如何构造 `DiffusionParallelConfig(ulysses_degree=..., ring_degree=..., cfg_parallel_size=..., tensor_parallel_size=...)`、如何按 `--cache-backend` 组 `cache_config`、如何传进 `Omni(...)`。
3. 想清楚：这些加速参数对**黑盒适配器**有效吗？为什么？

**需要观察的现象**：脚本里 `--ulysses-degree`、`--cfg-parallel-size`、`--cache-backend` 等参数都是给**原生 pipeline**（如 Qwen-Image-Edit）用的；如果你把模型换成走 `diffusers_adapter` 的黑盒路径并开这些参数，会在初始化时被 `_raise_unsupported_features` 直接拒绝。

**预期结果**：你能口述「黑盒适配器 = 快速验证可用性；要上生产拿性能，必须转原生实现」这条决策路径。

#### 4.5.5 小练习与答案

**练习 1**：一个刚开源的 diffusers 文生图模型，你想先用最低成本在 vLLM-Omni 里跑通，该选哪条路径？之后想压榨多卡性能又该怎么做？

> **参考答案**：先走**黑盒适配器**（`--diffusion-load-format diffusers`），零代码上线验证可用性与质量。确认有价值后，再转**原生实现**：改 transformer 换 `Attention` 声明 `_sp_plan`、改 pipeline 继承 `CFGParallelMixin`、注册，从而拿到 SP/CFG/Cache 等加速。

**练习 2**：为什么黑盒适配器「无法支持 TeaCache」？

> **参考答案**：TeaCache 需要在 transformer 的每个 block 上挂 forward hook 来抽取残差并判定是否复用（见 u7-l3）。黑盒适配器把整条 pipeline 当黑盒委托给 diffusers `__call__`，框架无法介入到 diffusers 内部的 transformer block 层面，所以无法挂 hook。CFG 并行、SP 同理——都需要「注意力手术」，而黑盒路径碰不到注意力层。

---

## 5. 综合实践

**任务**：为一个简单的 diffusers **文生图**模型（例如 SDXL 或任意一个你熟悉的 `DiffusionPipeline` 子类）写一份完整的「接入清单」，并标注每一步对应的源码依据。**不必完整实现，只要清单正确且可执行**。

请按下列模板产出（这是本讲的核心交付物，也是官方指南 practice_task 的落地）：

```text
# 接入清单：<你的模型名>

## 0. 目录约定
- 新建目录：vllm_omni/diffusion/models/<name>/
- 文件：
  - __init__.py          （导出 pipeline / transformer / 处理函数）
  - pipeline_<name>.py    （pipeline + get_<name>_post_process_func）
  - <name>_transformer.py （transformer）

## 1. Transformer 改造（依据 4.2）
- 去掉的 Mixin：<列出>
- 基类改为：nn.Module
- Attention 位点与 role 声明：
    * <位点1> role="self"   （DiT 自注意力，Q/K/V 同源）
    * <位点2> role="cross"  （若有文本条件交叉注意力；K/V 复制则加 skip_sequence_parallel=True）
- 构造函数新增参数：od_config: OmniDiffusionConfig | None = None
- （可选）_repeated_blocks = ["<Block类名>"]   # torch.compile
- （可选）_sp_plan = {...}                      # 序列并行

## 2. Pipeline 改造（依据 4.3）
- 基类：nn.Module（+ 按需 SupportImageInput / SupportsComponentDiscovery）
- __init__ 参数：*, od_config: OmniDiffusionConfig, prefix: str = ""
- 手动加载组件：scheduler / text_encoder / vae / tokenizer
- weights_sources = [DiffusersPipelineLoader.ComponentSource(subfolder="transformer", prefix="transformer.", ...)]
- forward 签名：def forward(self, req: DiffusionRequestBatch) -> DiffusionOutput
- 返回：DiffusionOutput(output=<VAE解码后的图像张量>)
- post_process：get_<name>_post_process_func(od_config) → 闭包，latent→PIL
- pre_process：文生图通常不需要；图像编辑才需要

## 3. 注册（依据 4.4，编辑 vllm_omni/diffusion/registry.py）
- _DIFFUSION_MODELS["<Arch名>"] = ("<name>", "pipeline_<name>", "<Pipeline类名>")
- _DIFFUSION_POST_PROCESS_FUNCS["<Arch名>"] = "get_<name>_post_process_func"
- （若有 pre）_DIFFUSION_PRE_PROCESS_FUNCS["<Arch名>"] = "get_<name>_pre_process_func"
- __init__.py 导出上述符号

## 4. 示例与测试
- 复用 examples/offline_inference/text_to_image/text_to_image.py
- 补 L4 功能测试（与 diffusers 基线同 seed/步数/分辨率对比）

## 5. 可选加速（依据 4.5）
- TP：tensor_parallel_size=N（需 hidden_dim/heads 整除）
- CFG：pipeline 继承 CFGParallelMixin + diffuse()，cfg_parallel_size=N
- SP：transformer 加 _sp_plan，ulysses_degree/ring_degree
- Cache：cache_backend="cache_dit" 或 "tea_cache"
```

**如何自检清单正确性**：

1. 把你的清单里「Arch 名」与 `registry.py` 现有条目对比，确认格式一致（三元组、字符串函数名）。
2. 确认 post/pre 处理函数名与你计划放在 `pipeline_<name>.py` 里的函数名**逐字一致**（`_load_process_func` 用字符串 `getattr`）。
3. 确认 transformer 的每个 `Attention` 位点都声明了 `role`。
4. 确认 `forward` 返回的是 `DiffusionOutput`，而不是 diffusers 那种 dict。

**预期结果**：产出一份能交给协作者「照着改就能接入」的清单。如果你不确定某一步（例如你的模型没有独立 transformer 类），在对应行标注「待确认」并说明原因——不编造。

## 6. 本讲小结

- 接入新扩散模型 = 改写 Transformer + 改写 Pipeline + 注册 + 示例/测试；框架负责调度执行，模型只管前向。
- Transformer 改造的核心是**把注意力换成 `Attention` 层并声明 `role`**；后端选择（四级优先级）与 SP/编译计划（`_sp_plan`/`_repeated_blocks`）都是声明式、构造期完成的。
- Pipeline 改造的核心是**换成 `forward(DiffusionRequestBatch) -> DiffusionOutput` 契约**，并把 pre/post 处理拆成闭包工厂单独注册；能力用 Protocol 声明。
- 注册在 `registry.py`：三张表（pipeline 类 / pre / post）共用 arch 名，处理函数必须与 pipeline 同文件，运行时由 `initialize_model` + `_load_process_func` 懒加载并实例化。
- 两条路径二选一：原生实现拿全部加速（TP/SP/CFG/Cache/步级），黑盒适配器 `DiffusersAdapterPipeline` 零代码上线但能力受限并 fail-fast 拒绝不兼容特性。
- 模型实例化必须在 `set_current_diffusion_config` 上下文里，SP 钩子由框架根据 `_sp_plan` 自动挂——模型代码不写通信。

## 7. 下一步学习建议

- **深入并行**：本讲只讲了「声明 `_sp_plan`」，序列并行的 Ring/Ulysses 通信细节见 **u7-l2（并行注意力）** 与 **u7-l4（TP/SP/DP/CFG/PP/HSDP/VAE）**；CFG 并行的 all_gather 合并见 u7-l4。
- **深入缓存**：TeaCache 的 extractor/多项式系数怎么写、Cache-DiT 的子策略怎么配，见 **u7-l3（缓存加速）**。
- **多阶段/Omni 模型**：如果你接入的是「理解 + 生成」的多阶段模型（如 Qwen3-Omni，Thinker/Talker/DiT 多 stage），本讲的「单 diffusion stage」不够用，请继续读 **u9-l2（添加 Omni 多阶段模型）**。
- **在线服务**：原生 pipeline 接入后如何通过 `vllm serve --omni` 暴露 OpenAI 兼容端点，见 **u6（在线服务）**。
- **测试**：接入后必补的 L4 功能测试怎么写、放哪里、用什么 marker，见 **u10-l1（多级测试体系）**。
