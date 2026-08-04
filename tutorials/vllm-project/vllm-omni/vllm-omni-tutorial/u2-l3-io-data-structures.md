# 输入输出数据结构：OmniPromptType 与 OmniRequestOutput

## 1. 本讲目标

本讲聚焦 vLLM-Omni 里**数据怎么流动**。学完本讲，你应当能够：

1. 说出 `OmniPromptType` 联合类型包含哪些成员，以及它们各自适合描述什么样的输入。
2. 解释 `prompt_embeds`、`additional_information`、`model_intermediate_buffer` 三个"跨阶段载荷"分别在阶段之间传递什么、有什么区别。
3. 看懂 `OmniRequestOutput / OmniModelRunnerOutput / OmniConnectorOutput` 三层输出结构的职责分工。
4. 手工跟踪一段"带文本 + 张量载荷"的 prompt 字典，是怎样被**序列化**成一个 `OmniEngineCoreRequest` 并跨进程投递到 stage 的。

本讲不涉及具体模型的生成逻辑，只讲"数据容器长什么样、怎么被搬运"。

## 2. 前置知识

本讲需要你先理解以下几个概念（若不熟悉，建议先看 `u1-l4` 与 `u2-l1`）：

- **stage（阶段）**：vLLM-Omni 把一次完整生成拆成多个顺序子任务，每个子任务由一个独立的 EngineCore 进程执行。例如 Qwen3-Omni 拆成 Thinker / Talker / Code2wav 三个 stage。
- **多阶段请求**：一个用户请求在 stage0 产出中间结果（如隐藏态、token、latent），再被"前推"给 stage1 继续处理。这意味着数据必须能被序列化、跨进程（甚至跨节点）搬运。
- **TypedDict 与 dataclass**：两者都是 Python 描述"结构化数据"的方式。
  - `TypedDict` 本质仍是 `dict`，运行时不额外占用类型信息，靠"键名"约束结构，常用于和 vLLM 原生 prompt 兼容。
  - `dataclass` 是真正的类实例，有属性、方法，适合承载大量字段和行为的"参数口袋"。
- **msgspec.Struct**：一个高性能序列化库 `msgspec` 提供的结构体，类似 `dataclass`，但可被高效编码为二进制（配合 msgpack），用于跨进程传输。
- **monkey-patch（猴子补丁）**：在运行时替换某个类/方法。`u2-l1` 讲过 vLLM-Omni 用 `patch.py` 把 vLLM 的 `Request` 整体替换为 `OmniRequest` 子类，这正是本讲"请求"能多出额外字段的根因。

一句话总结：**本讲讲的是"盒子"——prompt 怎么装、中间产物怎么打包、最终结果怎么取。**

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [`vllm_omni/inputs/data.py`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/inputs/data.py) | 输入侧数据结构 | `OmniTextPrompt` 等各 Prompt、`OmniPromptType` 联合类型、`OmniDiffusionSamplingParams` |
| [`vllm_omni/outputs/__init__.py`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/outputs/__init__.py) | 输出侧数据结构 | `OmniRequestOutput`、`OmniModelRunnerOutput`、`OmniConnectorOutput` |
| [`vllm_omni/engine/__init__.py`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/__init__.py) | 引擎层载荷与请求 | `PromptEmbedsPayload`、`AdditionalInformationPayload`、`OmniEngineCoreRequest` |
| [`vllm_omni/data_entry_keys.py`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/data_entry_keys.py) | 结构化载荷的序列化 | `OmniPayload`、`serialize_payload`、`flatten_payload` |
| [`vllm_omni/engine/serialization.py`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/serialization.py) | 序列化薄封装 | `serialize_additional_information` / `deserialize_additional_information` |
| [`vllm_omni/engine/async_engine_utils.py`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/async_engine_utils.py) | prompt → 请求的升级桥 | `upgrade_to_omni_request`、`inject_global_id` |
| [`vllm_omni/request.py`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/request.py) | 引擎内部请求类 | `OmniRequest`、`_maybe_decode_prompt_embeds` |

阅读建议：先读 `inputs/data.py` 与 `outputs/__init__.py` 建立"输入/输出两端"的直觉，再读 `engine/__init__.py` 理解"中间怎么打包"，最后用 `async_engine_utils.py` 把两端串起来。

## 4. 核心概念与源码讲解

### 4.1 输入数据结构：OmniPromptType 联合类型与各 Prompt

#### 4.1.1 概念说明

vLLM 原生用 `PromptType`（一个联合类型）描述用户输入：既可以是裸字符串 `"一只猫"`，也可以是带 token 的字典 `{"prompt_token_ids": [...]}`。vLLM-Omni 要支持**多阶段流水线**与**扩散模型**，需要在 prompt 里多塞两样东西：

- **`prompt_embeds`**：上一个阶段算好的嵌入张量（`torch.Tensor`），让下一个阶段直接拿来用，而不必重新编码。
- **`additional_information`**：一个字典，装着任意"要在阶段间传话"的张量、列表或标量（例如隐藏态、音频码、流控标志）。

因此 vLLM-Omni 定义了一组**继承自 vLLM 原生 prompt** 的扩展类型，再合并成 `OmniPromptType`。继承而非重写是一个关键设计：它保证这些扩展类型只**新增字段、不删除字段**，从而仍然能安全地交给 vLLM 原生的 `LLM.generate()` 处理。

#### 4.1.2 核心流程

输入类型的组合关系如下：

```text
OmniPromptType = PromptType                          # vLLM 原生（str / TextPrompt / TokensPrompt / EmbedsPrompt）
              | OmniTextPrompt                       # 扩展：文本 prompt + 可选 embeds/info
              | OmniTokensPrompt                     # 扩展：token prompt + 可选 embeds/info
              | OmniEmbedsPrompt                     # 扩展：嵌入 prompt + 可选 info
              | OmniCustomPrompt                     # 扩散专用：跳过 tokenization 的预制输入
```

三个 `Omni*Prompt` 都额外提供同一组可选字段：`prompt_embeds`、`negative_prompt(_embeds)`、`additional_information`、`model_intermediate_buffer`。而 `OmniCustomPrompt` 是为扩散 pipeline 准备的"成品输入"——它直接携带已经 tokenize 好的 `prompt_ids`、`prompt_mask`，绕过 pipeline 内部的 tokenize 步骤。

#### 4.1.3 源码精读

`OmniPromptType` 的定义只有两行，但它把"扩展"与"兼容"的边界写得很清楚：

[`vllm_omni/inputs/data.py:148-151`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/inputs/data.py#L148-L151) —— 用 `TypeAlias` 把 vLLM 原生 `PromptType` 与 omni 扩展类型合并成联合类型。

`OmniTextPrompt` 是最常用的扩展 prompt，它继承 vLLM 的 `TextPrompt` 并用 `NotRequired` 标注新增可选字段：

[`vllm_omni/inputs/data.py:15-36`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/inputs/data.py#L15-L36) —— 关键字段包括：
- `prompt_embeds`：可选的提示嵌入张量，阶段间直接传递。
- `negative_prompt` / `negative_prompt_embeds`：扩散模型 CFG（分类器自由引导）所需的负提示。
- `additional_information`：传话用的字典（值可以是 `torch.Tensor` 或 list）。
- `model_intermediate_buffer`：runner 拥有的阶段载荷（见 4.3 节，它是 `additional_information` 的继任者）。

`OmniCustomPrompt` 则面向扩散 pipeline 的"预制输入"场景：

[`vllm_omni/inputs/data.py:124-142`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/inputs/data.py#L124-L142) —— 允许用户直接传入 `prompt_ids`、`negative_prompt_ids`、`prompt_mask` 以及 pipeline 专属 `extra_args`，跳过 tokenize。

此外，文件还提供了一个便捷构造器 `token_inputs_omni`，按需把可选字段填进 `OmniTokenInputs`：

[`vllm_omni/inputs/data.py:154-192`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/inputs/data.py#L154-L192) —— 只在对应参数非 `None` 时才写入键，保持字典精简。

> 小提示：`TypedDict` 在运行时仍是普通 `dict`，无法用 `isinstance` 区分。源码里有一句注释明确提醒这一点——这也是为什么"必须继承 vLLM 原生类型而非另起炉灶"，以保证路由到 `LLM.generate()` 时不出错。

#### 4.1.4 代码实践

**实践目标**：亲手构造几种合法的 `OmniPromptType`，体会"扩展类型 = 原生类型 + 可选新字段"。

**操作步骤**（源码阅读 + 类型构造，无需运行模型）：

1. 在 Python 中 `import` 相关符号（示例代码，不依赖加载模型）：

   ```python
   # 示例代码：仅演示数据结构，不需要加载任何模型
   from vllm_omni.inputs.data import (
       OmniTextPrompt, OmniDiffusionSamplingParams,
   )
   ```

2. 构造一个最简文本 prompt：`prompt = {"prompt": "一只在月球上的猫"}`（它天然满足 `OmniTextPrompt`，因为字段都是可选的）。
3. 再构造一个带跨阶段载荷的 prompt（示例代码）：

   ```python
   # 示例代码：演示 additional_information 的结构，tensor 仅为占位
   import torch
   prompt_with_payload: OmniTextPrompt = {
       "prompt": "继续生成",
       "prompt_embeds": torch.zeros(4, 8),               # 假装是上一阶段的嵌入
       "additional_information": {
           "meta": {"next_stage_prompt_len": 4},          # 结构化字典
           "embed": {"prefill": torch.zeros(4, 8)},       # 张量值
       },
   }
   ```

**需要观察的现象**：上述字典在语法上完全合法，且因为只新增字段，可以直接交给 vLLM 原生流程而不会报"未知字段"错误。

**预期结果**：你能用同一个变量名承载"纯文本"和"带载荷"两种 prompt；后者多出的键就是 omni 扩展能力的入口。

（运行时若缺少 vLLM/torch 依赖会 import 失败，这属于环境问题，标注「待本地验证」。）

#### 4.1.5 小练习与答案

**练习 1**：`OmniPromptType` 里为什么同时保留 `PromptType`（vLLM 原生）和一堆 `Omni*Prompt`？只用 `Omni*Prompt` 行不行？

> **参考答案**：保留 `PromptType` 是为了兼容"不知道 omni 扩展"的上游调用方（如直接传字符串）。只用 `Omni*Prompt` 会破坏 vLLM 原生类型签名，导致类型检查不通过、且违背"增量扩展"原则。

**练习 2**：`OmniTextPrompt` 用的是 `class OmniTextPrompt(TextPrompt)` + `NotRequired` 字段，而不是 `dataclass`。这样做有什么好处？

> **参考答案**：`TypedDict` 子类在运行时仍是 `dict`，能被 vLLM 期望 `dict` 的代码路径直接消费；`NotRequired` 保证新增字段可省略。若改用 `dataclass`，则无法与 vLLM 的 prompt 字典约定兼容。

---

### 4.2 扩散采样参数：OmniDiffusionSamplingParams

#### 4.2.1 概念说明

vLLM 原生 AR（自回归）模型用 `SamplingParams` 控制采样（temperature、top_p、max_tokens 等）。但扩散模型（DiT）的"采样"完全是另一回事——它需要去噪步数、分辨率、CFG 强度、潜在张量、甚至 KV 缓存迁移信息。这些字段太多、太异质，塞进 `SamplingParams` 既不优雅也不现实。

于是 vLLM-Omni 设计了 `OmniDiffusionSamplingParams`：一个**超大号的 `dataclass` 参数口袋**，把扩散 pipeline 执行所需的几乎全部信息打包在一起。它的设计哲学是：与其在函数间传递几十个零散参数，不如传递这一个对象，pipeline 内部按需读写其中的字段。

#### 4.2.2 核心流程

`OmniDiffusionSamplingParams` 的字段可以按用途分组理解：

| 分组 | 代表字段 | 作用 |
| --- | --- | --- |
| 文本 / CFG | `do_classifier_free_guidance`、`cfg_normalize`、`true_cfg_scale` | 控制是否做无分类器引导及其强度 |
| 批次 / 随机性 | `num_outputs_per_prompt`、`seed`、`generator` | 每条 prompt 生成几份、随机种子 |
| 分辨率 / 帧数 | `height`、`width`、`num_frames`、`fps`、`frame_rate` | 输出图像/视频的几何尺寸 |
| 时间步 | `num_inference_steps`、`timesteps`、`step_index` | 去噪循环的步数与当前进度 |
| 潜在张量 | `latents`、`noise_pred`、`image_latent`、`audio_latents` | 去噪过程中的中间张量 |
| KV 缓存迁移 | `past_key_values`、`kv_metadata`、`need_kv_receive` | 跨阶段 KV 缓存（详见 `u3-l4`） |
| 多 KV / CFG 分支 | `cfg_text_past_key_values`、`cfg_branch_roles` | CFG 双分支的 KV 管理 |
| 轨迹 / 调试 | `return_trajectory_latents`、`debug`、`profile` | 记录每步 latent 或性能剖析 |

其中 `batch_size` 被改写为一个属性——它只代表"单条 prompt 的输出份数"，而不是请求批大小：

\[ \text{batch\_size} = \text{num\_outputs\_per\_prompt} \]

这是因为该类被重新定义为"只代表单条 prompt 请求"（源码注释明确说明），真正的多请求批处理由 scheduler 负责（见 `u5-l2`）。

`from_params` 类方法负责把多种外部参数**归一化**成该类型，是它的对外"入口适配器"。

#### 4.2.3 源码精读

`OmniDiffusionSamplingParams` 是一个标注了字段含义的大型 `@dataclass`：

[`vllm_omni/inputs/data.py:195-339`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/inputs/data.py#L195-L339) —— 注意几个关键设计：
- `num_inference_steps`、`guidance_scale` 的注释写明"未显式设置时为 `None`，由各 pipeline 的 `forward()` 自行决定默认值"，体现了"模型自治"。
- KV 缓存相关字段以注释 `[Omni]` 标注，说明这是 omni 相对 diffusers 原生 pipeline 的新增能力。

`batch_size` 属性体现"单 prompt"语义：

[`vllm_omni/inputs/data.py:340-344`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/inputs/data.py#L340-L344) —— 只按 `num_outputs_per_prompt` 调整。

`from_params` 是归一化入口，能从 `OmniDiffusionSamplingParams` 或 vLLM `SamplingParams` 两种来源构造：

[`vllm_omni/inputs/data.py:375-398`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/inputs/data.py#L375-L398) —— 它会：把 `SamplingParams.extra_args` 里"恰好是本 dataclass 已知字段"的键提升为顶层字段，剩下的留在 `extra_args`；并把 `seed` 带过来；遇到不支持的类型则抛 `TypeError`。

最后，`OmniSamplingParams` 把扩散参数与 vLLM 原生参数合并成一个联合类型，方便上层统一引用：

[`vllm_omni/inputs/data.py:401-401`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/inputs/data.py#L401-L401) —— `OmniSamplingParams = SamplingParams | OmniDiffusionSamplingParams`。

#### 4.2.4 代码实践

**实践目标**：体会 `from_params` 如何把"杂乱的外部参数"归一化成统一对象。

**操作步骤**（源码阅读 + 构造，可不必运行模型）：

1. 阅读上面的 `from_params` 源码。
2. 构造一个 vLLM `SamplingParams`，故意在 `extra_args` 里放一个与 dataclass 同名的键（如 `height`）和一个同名以外的键（如 `my_custom`）：

   ```python
   # 示例代码：演示 from_params 的归一化逻辑
   from vllm.sampling_params import SamplingParams
   from vllm_omni.inputs.data import OmniDiffusionSamplingParams

   sp = SamplingParams(temperature=0.7, seed=42)
   sp.extra_args = {"height": 768, "my_custom": "x"}
   omni_sp = OmniDiffusionSamplingParams.from_params(sp)
   print(omni_sp.height)         # 期望：768（被提升为顶层字段）
   print(omni_sp.extra_args)     # 期望：{"my_custom": "x"}（剩余留在 extra_args）
   print(omni_sp.seed)           # 期望：42
   ```

**需要观察的现象**：与 dataclass 字段同名的键被"提升"到顶层，其余键被原样收进 `extra_args`。

**预期结果**：`height` 变成对象属性，`my_custom` 仍嵌套在 `extra_args`。这一行为正是 `from_params` 的设计目的——让扩散 pipeline 能透明地接收来自不同上层的参数。

（依赖 vLLM 环境才能实际执行；若未安装则标注「待本地验证」。）

#### 4.2.5 小练习与答案

**练习 1**：为什么 `OmniDiffusionSamplingParams.batch_size` 只等于 `num_outputs_per_prompt`，而不是请求批大小？

> **参考答案**：该类被重新定义为"仅代表单条 prompt 请求"。真正的多请求批处理（把若干兼容请求合并进一次 `pipeline.forward(batch)`）由 diffusion scheduler 负责（见 `u5-l2`、`u7-l5`），不应混入采样参数。

**练习 2**：`from_params` 收到一个普通 `SamplingParams` 时，`extra_args` 里既包含 dataclass 已知字段、又包含未知字段，分别会怎样处理？

> **参考答案**：已知字段被"弹出"并映射为 dataclass 顶层参数；剩余未知字段整体作为一个 dict 放回 `extra_args`；`seed` 若存在则带过来；都不是上述类型则抛 `TypeError`。

---

### 4.3 跨阶段载荷与引擎请求：Payload 与 OmniEngineCoreRequest

#### 4.3.1 概念说明

阶段之间要传的东西有两类，性质完全不同：

1. **提示嵌入 `prompt_embeds`**：一个规整的 `torch.Tensor`，形状通常是 `[seq_len, hidden_size]`，用于让下一阶段把"上一阶段的语义向量"当作输入嵌入。
2. **附加信息 `additional_information`**：一个**异构字典**，值可能是张量、列表或标量，键名也不固定（不同模型塞不同的东西）。

跨进程传输要求**二进制可序列化**，而 `torch.Tensor` 不是 JSON 友好的。于是 vLLM-Omni 用 `msgspec.Struct` 定义了三组"线缆载荷（wire payload）"：

- `PromptEmbedsPayload`：把一个张量拆成 `data`(原始字节) + `shape` + `dtype`。
- `AdditionalInformationEntry`：单个值的三态封装（张量 / 列表 / 标量）。
- `AdditionalInformationPayload`：一组 `AdditionalInformationEntry` 的字典。

它们最终被装进 `OmniEngineCoreRequest`——一个继承自 vLLM `EngineCoreRequest` 的扩展请求类，多出"载荷"字段。

> 工程演进提示：源码注释明确把 `additional_information` 标为 **legacy（遗留）** 的"请求级传输通道"，而 `model_intermediate_buffer` 是**新的 runner 拥有载荷**，会被直接物化进 `GPUModelRunner.model_intermediate_buffer`。两者并存、逐步迁移。

#### 4.3.2 核心流程

一个张量从"Python 对象"到"可跨进程字节"的拆包过程：

\[ \text{Tensor} \;\xrightarrow{\text{detach/cpu/contiguous}}\; \text{numpy} \;\xrightarrow{\text{tobytes}}\; \text{raw bytes} + (\text{shape}, \text{dtype}) \]

反序列化则相反：用 `numpy.frombuffer` 按指定 `dtype` 读回，再 `reshape` 成原形状。

载荷与请求的组装关系：

```text
prompt_embeds(Tensor) ──► PromptEmbedsPayload(bytes,shape,dtype)
                                     │
additional_information(dict) ──► AdditionalInformationPayload(entries)
                                     │
                                     ▼
                         OmniEngineCoreRequest（继承 EngineCoreRequest）
                         ├─ prompt_embeds: Tensor | None     （解码后的张量）
                         ├─ additional_information: Payload | None
                         └─ model_intermediate_buffer: dict | None
```

注意：`OmniEngineCoreRequest.prompt_embeds` 字段本身存的是**已解码回的 `torch.Tensor`**（继承自上游），`PromptEmbedsPayload` 应在构造请求前先解码成张量。

#### 4.3.3 源码精读

`PromptEmbedsPayload` 是最简单的线缆载荷：

[`vllm_omni/engine/__init__.py:16-27`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/__init__.py#L16-L27) —— 三字段：`data`(bytes)、`shape`(list[int])、`dtype`(str，如 "float16")。

`AdditionalInformationEntry` 用"三选一非空"表达三种值形态：

[`vllm_omni/engine/__init__.py:29-48`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/__init__.py#L29-L48) —— 张量形态用 `tensor_data/shape/dtype`；列表形态用 `list_data`；标量形态用 `scalar_data`。文档要求三者恰有一个非 `None`。

`AdditionalInformationPayload` 只是把若干 entry 收成字典：

[`vllm_omni/engine/__init__.py:51-57`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/__init__.py#L51-L57) —— `entries: dict[str, AdditionalInformationEntry]`。

`OmniEngineCoreRequest` 是引擎层扩展请求：

[`vllm_omni/engine/__init__.py:60-81`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/__init__.py#L60-L81) —— 继承 `EngineCoreRequest`，新增 `additional_information` 与 `model_intermediate_buffer`；注释说明 `prompt_embeds` 继承自上游且已是 `Tensor | None`。

它的构造入口是类方法 `from_request`——以"克隆 + 覆盖载荷字段"的方式从普通 `EngineCoreRequest` 升级而来：

[`vllm_omni/engine/__init__.py:83-124`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/__init__.py#L83-L124) —— 逐字段搬运上游请求的所有属性（request_id、prompt_token_ids、sampling_params 等），并在末尾接上三个载荷字段。注意它对未提供的载荷会回退读取原请求上的同名属性，保证"幂等升级"。

同文件还定义了输出侧的 `OmniEngineCoreOutput` / `OmniEngineCoreOutputs`（多模态输出通道、流式分段结束标志），将在 4.5 节与输出结构一并提及：

[`vllm_omni/engine/__init__.py:127-139`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/__init__.py#L127-L139) —— `OmniEngineCoreOutput` 新增 `multimodal_output`、`is_segment_finished`、`new_prompt_len_snapshot`。

#### 4.3.4 代码实践

**实践目标**：手工把一个张量与一个混合字典"打包"成线缆载荷，体会三态封装。

**操作步骤**（纯数据结构操作，可独立运行，无需模型）：

```python
# 示例代码：演示 Payload 的三态封装
import torch
from vllm_omni.engine import (
    PromptEmbedsPayload, AdditionalInformationEntry, AdditionalInformationPayload,
)

# 1) 张量 → PromptEmbedsPayload
t = torch.zeros(3, 4, dtype=torch.float16).contiguous()
pe = PromptEmbedsPayload(
    data=t.numpy().tobytes(),
    shape=list(t.shape),
    dtype="float16",
)
print(len(pe.data), pe.shape, pe.dtype)   # 期望：24 [3, 4] float16

# 2) 三态值 → AdditionalInformationEntry
e_tensor = AdditionalInformationEntry(
    tensor_data=t.numpy().tobytes(), tensor_shape=[3, 4], tensor_dtype="float16")
e_list   = AdditionalInformationEntry(list_data=[1, 2, 3])
e_scalar = AdditionalInformationEntry(scalar_data=7)

# 3) 组成 Payload
payload = AdditionalInformationPayload(entries={
    "hidden": e_tensor,
    "ids": e_list,
    "left_context_size": e_scalar,
})
print(list(payload.entries.keys()))   # 期望：['hidden', 'ids', 'left_context_size']
```

**需要观察的现象**：一个 `float16` 的 `[3,4]` 张量被编码为 24 字节（\(3 \times 4 \times 2 = 24\)）；标量与列表分别走 `scalar_data` / `list_data` 通道。

**预期结果**：打印出的字节长度、形状、键名与注释一致。

（依赖 `msgspec`/`torch`，若环境缺包则标注「待本地验证」。）

#### 4.3.5 小练习与答案

**练习 1**：`PromptEmbedsPayload` 为什么要把 `dtype` 存成字符串（如 `"float16"`）而不是直接存 `torch.float16`？

> **参考答案**：跨进程/跨语言传输需要可序列化的纯数据类型，`torch.dtype` 对象无法直接编码为二进制或 JSON。存成字符串后，接收端用 `numpy.dtype("float16")` 或 `getattr(np, "float16")` 还原。

**练习 2**：`additional_information` 与 `model_intermediate_buffer` 都能传"阶段间的字典载荷"，它们的主要区别是什么？

> **参考答案**：`additional_information` 是 legacy 的"请求级传输通道"，值需经过序列化（张量拆字节）随请求在线缆上走；`model_intermediate_buffer` 是新的 runner 拥有载荷，被直接物化进 `GPUModelRunner.model_intermediate_buffer`，不经过（或弱化）请求序列化路径，是迁移方向。

---

### 4.4 序列化链路：从 prompt 字典到 OmniEngineCoreRequest

> 本节是本讲的"主干"，也是综合实践（第 5 节）的基础。

#### 4.4.1 概念说明

前一节我们知道了"载荷长什么样"，但还没回答最关键的问题：**用户传进来的一个 prompt 字典，是怎么自动变成带载荷的 `OmniEngineCoreRequest` 的？** 这条链路涉及四个文件分工合作：

1. **结构化字典约定**（`data_entry_keys.py`）：用 `OmniPayload` 这个 `TypedDict` 规定"阶段间该传哪些键"，分为 `hidden_states / embed / ids / codes / meta` 等类别。
2. **序列化实现**（同文件 + `serialization.py`）：把嵌套字典"拍平成带点号的键"，再把每个值按类型（张量/列表/标量）编码成 `AdditionalInformationEntry`。
3. **升级桥**（`async_engine_utils.py`）：在输入处理之后，把 prompt 字典里被"上游 input processor 丢掉"的 omni 字段（`prompt_embeds`/`additional_information`/`model_intermediate_buffer`）捡回来，重新挂到请求上。
4. **请求落点**（`request.py`）：`OmniRequest` 在构造时把 `PromptEmbedsPayload` 解码回 `torch.Tensor`，并保存载荷。

#### 4.4.2 核心流程

完整链路（以一次 `Omni.generate(prompt)` 在 stage0 的处理为例）：

```text
prompt(dict)
   │  ① inject_global_id: 往 prompt["additional_information"] 注入 global_request_id
   ▼
input_processor.process_inputs(prompt)
   │  产出普通 EngineCoreRequest（vLLM 原生，不含 omni 载荷）
   ▼
upgrade_to_omni_request(request, raw_prompt)        ← async_engine_utils.py
   │  ② 从 raw_prompt 取回 prompt_embeds / additional_information / buffer
   │  ③ serialize_additional_information → AdditionalInformationPayload
   │  ④ OmniEngineCoreRequest.from_request(...) 重新组装
   ▼
OmniEngineCoreRequest（带载荷，可跨进程投递到 Orchestrator/stage）
   │  到达 stage 进程后：
   ▼
OmniRequest.from_engine_core_request(request)       ← request.py
   │  ⑤ _maybe_decode_prompt_embeds: PromptEmbedsPayload → torch.Tensor
   │  ⑥ 保存 additional_information / model_intermediate_buffer
   ▼
OmniRequest（runner 可直接读张量与载荷）
```

序列化内部的"拍平 → 编码"两步：

\[ \text{嵌套 dict} \;\xrightarrow{\text{flatten}}\; \text{点号键 dict} \;\xrightarrow{\text{per-value encode}}\; \text{AdditionalInformationPayload} \]

例如 `{"codes": {"audio": Tensor}}` 会被拍平成 `{"codes.audio": Tensor}`，再编码成 `entries["codes.audio"] = AdditionalInformationEntry(tensor_data=...)`。

#### 4.4.3 源码精读

**① `OmniPayload` 结构化字典约定**。它把阶段间传递的内容分成清晰的类别，是"传话协议"的规范：

[`vllm_omni/data_entry_keys.py:96-108`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/data_entry_keys.py#L96-L108) —— 顶层类别包括：`hidden_states`（中间/输出隐藏态）、`embed`（各类嵌入：prefill/decode/voice/speech_token…）、`ids`（token 序列）、`codes`（音频码）、`meta`（标量元数据与控制标志），外加 `latent`、`generated_len` 等。

**② 序列化的核心实现**。先看"拍平"：

[`vllm_omni/data_entry_keys.py:305-326`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/data_entry_keys.py#L305-L326) —— `_NESTED_KEYS` 列出哪些键是嵌套子字典（`hidden_states/embed/ids/codes/meta`），拍平时把它们展开为 `"类别.子键"`；其中 `hidden_states["layers"]` 还会进一步展开为 `hidden_states.layer_N`。

再看"按值类型编码"：

[`vllm_omni/data_entry_keys.py:369-393`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/data_entry_keys.py#L369-L393) —— `serialize_payload` 遍历拍平后的键值：张量走 `_serialize_tensor`，列表走 `AdditionalInformationEntry(list_data=...)`，其余非 `None` 值走 `scalar_data`。

张量编码的细节（detach → cpu → contiguous → tobytes）：

[`vllm_omni/data_entry_keys.py:351-359`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/data_entry_keys.py#L351-L359) —— 强制搬到 CPU 并连续化，保证字节布局可预测。

**③ 升级桥 `upgrade_to_omni_request`**——这条链路的"灵魂"。上游 input processor 处理 prompt 时，并不认识 omni 的扩展字段，因此产出的是不含载荷的普通请求；这个函数负责"捡回"它们：

[`vllm_omni/engine/async_engine_utils.py:40-74`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/async_engine_utils.py#L40-L74) —— 关键逻辑：
- 若 `raw_prompt` 是字典，尝试从中取回 `prompt_embeds`（仅当它是 `torch.Tensor`）、`additional_information`、`model_intermediate_buffer`。
- 用 `serialize_additional_information` 把字典序列化成 `AdditionalInformationPayload`。
- 如果三者都为空，直接返回原请求（避免无谓升级）。
- 否则用 `OmniEngineCoreRequest.from_request` 组装新请求。

`inject_global_id` 在更早一步向 prompt 注入全局请求 ID，用于跨阶段追踪：

[`vllm_omni/engine/async_engine_utils.py:29-37`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/async_engine_utils.py#L29-L37) —— 把 `global_request_id` 作为列表写进 `additional_information["meta"]` 风格的位置（实际写在 `additional_information` 顶层键）。

调用时机在 `AsyncOmniEngine._build_add_request_message` 中——先注入 ID、跑 input processor，再升级：

[`vllm_omni/engine/async_omni_engine.py:702-738`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/async_omni_engine.py#L702-L738) —— 注意 line 738 的注释「TODO (Peiqi): add this for Qwen3-TTS only」，说明 `additional_information` 这条 prompt 通道目前主要被特定模型（如 Qwen3-TTS）使用。

**④ 请求落点 `OmniRequest`**。当请求到达 stage 进程，需要从线缆形态回到"可读张量"形态：

[`vllm_omni/request.py:31-53`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/request.py#L31-L53) —— 构造时若传入 `PromptEmbedsPayload`，先用 `_maybe_decode_prompt_embeds` 解码成张量再交给父类 `Request`；同时保存 `additional_information`、`model_intermediate_buffer` 与原始 payload。

解码逻辑（bytes → numpy → reshape → tensor）：

[`vllm_omni/request.py:55-64`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/request.py#L55-L64) —— 与序列化严格对称。

> 旁注：`serialization.py` 只是把上面的 `serialize_payload` 包了一层薄封装（区分"已是 Payload"与"还是 dict"两种输入），见 [`vllm_omni/engine/serialization.py:15-42`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/serialization.py#L15-L42)。它存在的意义是给调用方一个稳定的、与 `AdditionalInformationPayload` 名字对齐的 API。

#### 4.4.4 代码实践

**实践目标**：手工复现"prompt 字典 → OmniEngineCoreRequest"的关键步骤，写出字段映射关系。

**操作步骤**（纯数据结构 + 序列化，可独立运行）：

```python
# 示例代码：复现 upgrade_to_omni_request 的核心序列化逻辑
import torch
from vllm_omni.data_entry_keys import serialize_payload, flatten_payload

# 1) 构造一个"带文本 + 多模态张量 + additional_information"的 prompt 字典
raw_prompt = {
    "prompt": "继续这段语音",
    "prompt_embeds": torch.ones(2, 3),                  # 假装是上一阶段嵌入
    "additional_information": {
        "hidden_states": {"output": torch.zeros(2, 3)}, # 嵌套类别：隐藏态
        "meta": {"next_stage_prompt_len": 2},           # 标量元数据
        "ids": {"prompt": [10, 20]},                    # 列表值
    },
}

# 2) 复现 upgrade_to_omni_request 内部对 additional_information 的处理
info = raw_prompt["additional_information"]
payload = serialize_payload(info)                       # -> AdditionalInformationPayload | None

# 3) 观察拍平后的键名
flat = flatten_payload(info)
print(sorted(flat.keys()))
```

**需要观察的现象**：
- 嵌套键被拍平为点号键：`hidden_states.output`、`meta.next_stage_prompt_len`、`ids.prompt`。
- 三种值分别进入 `tensor_data` / `scalar_data` / `list_data`。

**预期结果**（字段映射表）：

| prompt 字典中的位置 | 拍平后的键 | 编码到的 Entry 字段 |
| --- | --- | --- |
| `additional_information.hidden_states.output` | `hidden_states.output` | `tensor_data/shape/dtype` |
| `additional_information.meta.next_stage_prompt_len` | `meta.next_stage_prompt_len` | `scalar_data` |
| `additional_information.ids.prompt` | `ids.prompt` | `list_data` |
| `prompt_embeds`（顶层 Tensor） | （不进 additional_information） | 由 `from_request` 直接作为 `prompt_embeds` 字段 |
| `prompt`（字符串） | （由 vLLM tokenize） | `prompt_token_ids` |

（依赖 `msgspec`/`torch`；环境缺包则标注「待本地验证」。）

#### 4.4.5 小练习与答案

**练习 1**：为什么需要 `upgrade_to_omni_request` 这个"捡回字段"的步骤？直接让 input processor 输出 omni 载荷不行吗？

> **参考答案**：input processor 是 vLLM 原生组件，不认识 omni 扩展字段，会"丢掉"它们。`upgrade_to_omni_request` 在 input processor 之后、用原始 prompt 字典把这些字段补回来，从而既复用 vLLM 的 tokenize/多模态处理，又不丢失 omni 载荷。

**练习 2**：`flatten_payload` 为什么要把 `hidden_states.layers` 单独展开成 `hidden_states.layer_N`，而不是保留为嵌套 dict？

> **参考答案**：`layers` 是"层号 → 张量"的字典，键是动态整数。拍平成 `hidden_states.layer_N` 后能用统一的"点号键 → Entry"机制处理，避免再为嵌套字典写特例；反序列化时 `unflatten_payload` 再把它们收回 `layers`。

**练习 3**：`OmniRequest._maybe_decode_prompt_embeds` 接收 `PromptEmbedsPayload` 时做了什么？为什么和 `serialize_payload` 的张量编码必须严格对称？

> **参考答案**：用 `numpy.frombuffer` 按 `dtype` 读字节、`reshape` 成 `shape`、再转 `torch.Tensor`。必须对称是因为编码端约定了"row-major 字节 + shape + dtype"，任何一端不一致都会导致形状错乱或数值错误。

---

### 4.5 输出数据结构：OmniRequestOutput / OmniModelRunnerOutput / OmniConnectorOutput

#### 4.5.1 概念说明

输入讲完，看输出。vLLM-Omni 的输出分**三个层级**，对应生成流水线的三个观察点：

| 层级 | 类 | 观察点 | 谁生产 / 谁消费 |
| --- | --- | --- | --- |
| 引擎内部（细粒度） | `OmniConnectorOutput` | Model Runner → Scheduler | worker 的 model runner 产出，scheduler 据此调度 |
| 引擎内部（结果） | `OmniModelRunnerOutput` | Model Runner 的一批结果 | model runner 产出，输出处理器消费 |
| 用户面向（统一） | `OmniRequestOutput` | `Omni.generate` 的返回 | 引擎产出，用户/服务层消费 |

设计要点：`OmniRequestOutput` 是**统一容器**——它要同时描述两种模式：
- **扩散模式**：`images`（PIL 图像列表）、`latents`、`metrics`…
- **流水线模式**：`stage_id`、`final_output_type`、内嵌的 `request_output`。

一个类服务两种模式，靠一组判别属性（`is_diffusion_output` / `is_pipeline_output`）区分。

#### 4.5.2 核心流程

三个类的层级与流向：

```text
worker model runner
   │  产出
   ▼
OmniModelRunnerOutput（继承 vLLM ModelRunnerOutput）
   ├─ multimodal_outputs     # 面向用户的多模态结果（按 req_index）
   ├─ inter_stage_outputs    # 阶段间载荷（connector 运输用，不外发）
   └─ omni_connector_output  # 嵌套的调度信号
           │
           ▼
        OmniConnectorOutput
        ├─ chunk_ready_req_ids / chunk_finished_req_ids   # 分块到达信号
        ├─ request_metadata                                # 轻量调度元数据
        └─ kv_sent_req_ids / has_pending_kv_work           # KV 迁移状态
   │  经输出处理器/编排后
   ▼
OmniRequestOutput（用户/服务层最终拿到）
   ├─ from_diffusion(...)   # 扩散模式工厂方法
   ├─ from_pipeline(...)    # 流水线模式工厂方法
   └─ from_error(...)       # 错误终端输出
```

`OmniRequestOutput` 用三个类方法工厂（`from_diffusion` / `from_pipeline` / `from_error`）保证不同来源的输出都被正确装配，避免散落的构造代码。

#### 4.5.3 源码精读

**`OmniConnectorOutput`**——最底层的"调度信号包"，让 Scheduler 不直接调用 `connector.put/get` 也能做决策：

[`vllm_omni/outputs/__init__.py:13-36`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/outputs/__init__.py#L13-L36) —— 字段：`chunk_ready_req_ids`/`chunk_finished_req_ids`（哪些请求来了新分块/分块收尾）、`request_metadata`（如 `next_stage_prompt_len`）、`kv_sent_req_ids`、`has_pending_kv_work`。注释强调"完整载荷由 model runner 本地缓存拥有"，这里只放轻量信号。

**`OmniModelRunnerOutput`**——继承 vLLM `ModelRunnerOutput`，多出多模态与阶段间通道：

[`vllm_omni/outputs/__init__.py:39-71`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/outputs/__init__.py#L39-L71) —— `multimodal_outputs`（面向客户端，按 `req_index`）、`inter_stage_outputs`（connector 运输用，**不**转发到编排输出处理器）、`kv_extracted_req_ids`、`omni_connector_output`（嵌套上一类）。`with_kv_conn_output_only` 是只携带 KV 连接器信号的轻量构造路径。

**`OmniRequestOutput`**——用户面向的统一容器，是 `Omni.generate` 的返回元素类型：

[`vllm_omni/outputs/__init__.py:74-125`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/outputs/__init__.py#L74-L125) —— 同时容纳流水线字段（`stage_id`/`replica_id`/`final_output_type`/`request_output`）与扩散字段（`images`/`prompt`/`latents`/`trajectory_*`/`metrics`），还有性能（`stage_durations`/`peak_memory_mb`）与错误（`error`/`error_status_code`/`error_type`）字段。

三个工厂方法各自装配一种来源：

- [`from_pipeline(...)`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/outputs/__init__.py#L153-L178) —— 从某个 stage 的 `RequestOutput` 构造，记录 `stage_id`/`final_output_type`。
- [`from_diffusion(...)`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/outputs/__init__.py#L180-L235) —— 从扩散结果构造，装入图像/latent/轨迹/度量。
- [`from_error(...)`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/outputs/__init__.py#L127-L151) —— 终端错误输出，`finished=True`。

模式判别属性：

[`vllm_omni/outputs/__init__.py:331-339`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/outputs/__init__.py#L331-L339) —— `is_diffusion_output`（有图或类型为 image）、`is_pipeline_output`（有 stage_id 且有内嵌 request_output）。

`unwrap` / `unwrap_result` 处理"流水线输出层层嵌套 `OmniRequestOutput`"的常见模式：

[`vllm_omni/outputs/__init__.py:341-405`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/outputs/__init__.py#L341-L405) —— 递归剥到最内层含实际内容（图像/文本）的输出。

> 兼容性细节：类里有一组透传属性（`prompt_token_ids`、`outputs`、`prompt_logprobs` 等，见 [`outputs/__init__.py:281-329`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/outputs/__init__.py#L281-L329)），注释标明是为兼容 vLLM 的流式 chat 生成器（Issue #345）。这是"统一容器"付出的代价——要让 vLLM serving 代码以为它在操作普通 `RequestOutput`。

#### 4.5.4 代码实践

**实践目标**：用三个工厂方法分别构造输出，观察模式判别属性。

**操作步骤**（纯数据结构，无需模型）：

```python
# 示例代码：演示 OmniRequestOutput 的三种构造与判别
from vllm_omni.outputs import OmniRequestOutput

# 1) 扩散模式
out_diff = OmniRequestOutput.from_diffusion(
    request_id="0_abc",
    images=[],                         # 真实场景是 PIL Image 列表
    final_output_type="image",
)
print(out_diff.is_diffusion_output, out_diff.is_pipeline_output)   # 期望：True False

# 2) 错误模式
out_err = OmniRequestOutput.from_error("0_abc", "OOM")
print(out_err.finished, out_err.error)                            # 期望：True OOM

# 3) 透传属性在无内嵌 request_output 时为空
print(out_diff.outputs, out_diff.prompt_token_ids)               # 期望：[] None
```

**需要观察的现象**：扩散输出被判为 `is_diffusion_output`，流水线判别为 `False`；错误输出直接 `finished=True`；无内嵌 `request_output` 时透传属性返回空值。

**预期结果**：与注释及工厂方法逻辑一致。

（`from_pipeline` 需要一个真实的 `RequestOutput` 对象，构造较繁琐，可留作阅读型练习；环境缺包则标注「待本地验证」。）

#### 4.5.5 小练习与答案

**练习 1**：`OmniModelRunnerOutput` 里 `multimodal_outputs` 与 `inter_stage_outputs` 都是"每请求的 dict 列表"，为什么后者"不转发到编排输出处理器"？

> **参考答案**：`inter_stage_outputs` 是给 connector 运输阶段间完整载荷用的（如 `save_async`/`full_payload`），属于内部传输细节；转发到编排/用户层既无必要也可能泄漏大张量。`multimodal_outputs` 才是面向客户端的结果。

**练习 2**：`OmniRequestOutput` 为什么要提供 `unwrap` / `unwrap_result`？

> **参考答案**：流水线模式下，外层 `OmniRequestOutput` 的 `request_output` 可能又是另一个 `OmniRequestOutput`（层层包装中间阶段结果）。`unwrap` 递归剥到含实际内容（图像/文本）的最内层，避免用户手动层层解包。

**练习 3**：`OmniRequestOutput` 暴露 `prompt_token_ids`、`outputs` 等"透传属性"，体现了什么设计权衡？

> **参考答案**：为了让 vLLM 的 serving/streaming 代码把 `OmniRequestOutput` 当作普通 `RequestOutput` 使用（兼容性，Issue #345）。代价是这些属性需手动委托给内嵌的 `request_output`，属于"统一容器"为兼容上游付出的样板代码。

---

## 5. 综合实践

**任务**：构造一个"文本 + 多模态张量 + additional_information"的 `OmniPromptType` 字典，跟踪它被序列化为 `OmniEngineCoreRequest` 的全过程，并产出一张字段映射表。

**操作步骤**：

1. 构造输入（示例代码）：

   ```python
   # 示例代码：综合实践输入
   import torch
   prompt = {
       "prompt": "把这段文字转成语音",
       "prompt_embeds": torch.randn(5, 8),          # 上一阶段算好的嵌入
       "additional_information": {
           "hidden_states": {"output": torch.randn(5, 8)},   # 隐藏态
           "embed": {"voice": torch.randn(1, 4)},            # 说话人嵌入
           "ids": {"prompt": [1, 2, 3, 4, 5]},               # token 序列
           "meta": {"next_stage_prompt_len": 5, "omni_task": ["tts"]},
       },
   }
   ```

2. **模拟链路**（不必启动引擎，只复现关键序列化步骤）：
   - 调 `inject_global_id` 的等价操作：`prompt["additional_information"]["global_request_id"] = ["0_test"]`。
   - 调 `serialize_additional_information(prompt["additional_information"])` 得到 `AdditionalInformationPayload`。
   - 用 `flatten_payload` 查看拍平键，确认 `hidden_states.output`、`embed.voice`、`ids.prompt`、`meta.next_stage_prompt_len`、`meta.omni_task` 都在。
   - 用 `OmniEngineCoreRequest.from_request(...)`（需一个上游 `EngineCoreRequest`，可阅读 `from_request` 源码理解字段搬运）了解它如何把 `prompt_embeds` 与 payload 装到请求上。

3. **产出字段映射表**（填空）：

   | prompt 字典位置 | 经 flatten 后的键 | 编码到的 Entry 字段 | 进入请求的字段 |
   | --- | --- | --- | --- |
   | `prompt_embeds` | —（顶层张量） | — | `prompt_embeds` |
   | `additional_information.hidden_states.output` | `hidden_states.output` | `tensor_data/shape/dtype` | `additional_information.entries[...]` |
   | `additional_information.embed.voice` | ? | ? | ? |
   | `additional_information.ids.prompt` | ? | `list_data` | ? |
   | `additional_information.meta.next_stage_prompt_len` | ? | ? | ? |
   | `additional_information.global_request_id` | ? | ? | ? |

4. **反向验证**：调用 `deserialize_additional_information(payload)`（或 `deserialize_payload`）还原成字典，确认它与原始 `additional_information` 在张量形状、列表内容、标量值上一致。

**需要观察的现象**：序列化前后信息无损；拍平键用点号分层；张量被拆成字节+形状+类型，列表与标量各走各的通道。

**预期结果**：填好的映射表与 4.4.4 节的预期一致；反序列化还原的字典与原字典等价（张量 `torch.equal` 为 `True`，列表/标量相等）。

**如果无法运行**：环境缺少 `vllm`/`torch`/`msgspec` 时，至少完成"字段映射表"的纸面推导，并标注「待本地验证」——这正是源码阅读型实践的价值。

## 6. 本讲小结

- `OmniPromptType` 是 vLLM `PromptType` 与 omni 扩展类型的**联合**；扩展类型一律继承 vLLM 原生 prompt 并只新增可选字段（`prompt_embeds`/`additional_information`/`model_intermediate_buffer`），从而兼容上游。
- `OmniDiffusionSamplingParams` 是扩散阶段的大型 `dataclass` "参数口袋"，`from_params` 负责把 `SamplingParams` 或已有对象归一化；它只代表**单条 prompt**，`batch_size = num_outputs_per_prompt`。
- 跨阶段传输靠三组 `msgspec.Struct` 线缆载荷：`PromptEmbedsPayload`（张量拆字节）、`AdditionalInformationEntry`（张量/列表/标量三态）、`AdditionalInformationPayload`（entry 字典），最终装进 `OmniEngineCoreRequest`。
- 核心序列化链路是"拍平（`flatten_payload`，类别.子键）→ 按值类型编码"，由 `upgrade_to_omni_request` 在 input processor 之后把 omni 字段"捡回"并升级请求；`OmniRequest` 在 stage 端把 payload 解码回张量。
- 输出分三层：`OmniConnectorOutput`（调度信号）→ `OmniModelRunnerOutput`（一批结果）→ `OmniRequestOutput`（用户统一容器，用工厂方法与判别属性同时服务扩散模式与流水线模式）。
- 工程演进：`additional_information` 是 legacy 请求级通道，`model_intermediate_buffer` 是新的 runner 拥有载荷，两者并存逐步迁移。

## 7. 下一步学习建议

- **进入运行时**：本讲只讲了"数据容器"，接下来 `u3-l1`（AsyncOmni 多阶段架构）会把这些容器放进真实的请求队列与编排线程，看它们怎么在 `request_queue`/`output_queue` 间流动。
- **看编排如何前推**：`u3-l2`（Orchestrator）会讲 `OmniRequestOutput` 与阶段间载荷是如何被读取并路由到下一阶段的，正好消费本讲的 `additional_information`。
- **深入扩散执行**：`u5-l1`（Diffusion 引擎）会讲 `OmniDiffusionSamplingParams` 在去噪循环里被各 pipeline 字段读写的过程，`u5-l4` 会讲 `OmniDiffusionRequest` 的字段细节。
- **多模态输出累积**：`u4-l3`（多模态输出处理）会讲 `OmniModelRunnerOutput.multimodal_outputs` 与跨步张量累积，是本讲输出结构的自然延续。
- 建议阅读源码：构造一个最小 prompt 字典后，沿着 `upgrade_to_omni_request` → `serialize_payload` → `OmniRequest.from_engine_core_request` 通读一遍，把本讲的链路在真实代码里"走一遍"。
