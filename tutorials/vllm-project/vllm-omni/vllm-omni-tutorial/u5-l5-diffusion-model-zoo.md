# Diffusion 模型库与 diffusers 适配器

## 1. 本讲目标

学完本讲，你应当能够：

1. 说出 `vllm_omni/diffusion/models/` 下覆盖了哪些模型，并能按「图像 / 视频 / 音频 / 全模态」给它们分类。
2. 看懂「模型注册表（registry）」是如何把一个模型架构名（architecture）映射到一个具体 `Pipeline` 实现类的，并理解 pre/post 处理函数的挂载机制。
3. 理解 `diffusers_adapter`（`DiffusersAdapterPipeline`）作为「黑盒通用适配器」的作用、它的能力边界（不支持哪些高级特性），以及它如何把任意 🤗 Diffusers pipeline 直接服务化。
4. 掌握自定义 pipeline 的三种扩展入口（`diffusion_load_format`、`CustomPipelineWorkerExtension`、`worker_extension_cls`）。
5. 能够对比一个图像模型和一个音频模型的「输入 prompt 类型」与「输出 modality」。

本讲是 U5（Diffusion 模块）的最后一讲，承接 [u5-l4](u5-l4-diffusion-pipeline.md) 讲过的「单条 pipeline 内部去噪数据流」，把视角拉高到「整个模型库 + 通用适配器」这一层。

## 2. 前置知识

在进入正题前，先用三段话补齐概念。

**Pipeline（流水线）。** 在扩散模型里，一次完整生成不是「一个模型」做出来的，而是一条由多个子组件串成的流水线：文本编码器（text encoder）把 prompt 变成 embedding → DiT（扩散 Transformer）在 latent 空间里多步去噪 → VAE 解码器把 latent 还原成像素/波形/视频帧。vLLM-Omni 把这一整条流水线封装成一个 Python 类，命名为 `XxxPipeline`。

**注册表（registry）。** 一个项目要支持几十个模型，就需要一张「名字 → 实现类」的对照表。vLLM-Omni 的扩散模型注册表用「架构名（architecture）」作 key，例如 `"QwenImagePipeline"`，value 是一个三元组 `(mod_folder, mod_relname, cls_name)`，指明这个类在哪个文件里、叫什么名字。加载模型时，框架先从模型的 `model_index.json` / `config.json` 读出 architecture 名，再去注册表里查到实现类。

**黑盒适配器 vs 原生实现。** vLLM-Omni 有两种方式接入一个扩散模型：
- **原生实现（native pipeline）**：在 `diffusion/models/<模型名>/` 下手写一个 `Pipeline` 类，可以精细控制注意力后端、CFG 并行、序列并行、TeaCache 等加速特性（参考 u5-l3、u5-l4、u7 系列）。
- **黑盒适配器（diffusers adapter）**：不写任何模型专属代码，直接把 🤗 Diffusers 库的 `DiffusionPipeline.from_pretrained()` 当作一个「黑盒」整体调用。零代码即可上线，但拿不到那些需要「钻进 transformer 内部」的高级特性。

本讲就是讲清楚「模型库怎么组织」和「黑盒适配器怎么工作」。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|---|---|
| `vllm_omni/diffusion/registry.py` | 扩散模型注册表：架构名 → 实现类，以及 pre/post 处理函数的挂载；还有插件式注册接口 `register_diffusion_model`。 |
| `vllm_omni/diffusion/models/interface.py` | Pipeline 的「能力协议」：用 `Protocol` 声明一个 pipeline 支持哪些能力（图像输入、音频输出、步级执行、组件发现）。 |
| `vllm_omni/diffusion/models/diffusers_adapter/pipeline_diffusers_adapter.py` | 黑盒通用适配器 `DiffusersAdapterPipeline` 的全部实现。 |
| `vllm_omni/diffusion/models/diffusers_adapter/pipeline_utils.py` | 少数需要「后处理」的 diffusers pipeline（如 Wan 系列）的钩子工具类。 |
| `vllm_omni/diffusion/data.py` | 配置项 `OmniDiffusionConfig`：包含 `diffusion_load_format`、`diffusers_load_kwargs`、`diffusers_call_kwargs` 等字段，以及把模型解析成 architecture 名的 `resolve_model_class_name` / `enrich_config`。 |
| `docs/features/custom_pipeline.md` | 自定义 pipeline 的官方使用指南。 |
| `examples/online_serving/diffusers_pipeline_adapter/README.md` | 在线服务用 diffusers 后端的命令行示例。 |

---

## 4. 核心概念与源码讲解

### 4.1 模型库全景：从 architecture 名到 Pipeline 注册表

#### 4.1.1 概念说明

vLLM-Omni 的 `vllm_omni/diffusion/models/` 目录下，**每个子目录对应一个（或一族）原生实现的扩散模型**。截至当前 HEAD，这个目录下共有 40 多个模型子目录，加上注册表里多对一/多对多的架构别名，注册表 `_DIFFUSION_MODELS` 一共登记了 **50+ 个 architecture 名**。

这里要区分三个容易混淆的「名字」：

- **模型仓库（model / repo）**：HuggingFace 上的权重仓库，例如 `Qwen/Qwen-Image`、`stable-diffusion-v1-5/stable-diffusion-v1-5`。这是用户在命令行 `vllm serve` 后面填的字符串。
- **architecture（架构名）**：写在模型仓库 `model_index.json` 或 `config.json` 里的 `_class_name` / `architectures`，例如 `QwenImagePipeline`。框架据此判断「这是哪一类模型」。
- **实现类（cls_name）**：vLLM-Omni 源码里真正执行推理的 Python 类，例如 `QwenImagePipeline`（位于 `qwen_image/pipeline_qwen_image.py`）。

注册表就是把后两者绑在一起。

#### 4.1.2 核心流程

模型加载时，「模型仓库 → architecture → 实现类」的解析流程是：

1. 框架从模型的 `model_index.json` 读 `_class_name`，或从 `config.json` 读 `model_type` / `architectures`。
2. `resolve_model_class_name()` 把它规约成一个 architecture 名。
3. `OmniDiffusionConfig.enrich_config()` 把这个 architecture 名写入 `model_class_name` 字段。
4. `initialize_model()` 用注册表把 architecture 名映射到实现类并实例化。
5. 注册表还会顺带查到这个模型对应的 **post_process_func**（把 latent/张量变成可返回的图像/音频）和可选的 **pre_process_func**。

下面这张表把 `diffusion/models/` 下的目录按**主要输出模态**做了一个分类（依据是目录名与各 pipeline 声明的能力协议；具体每个 pipeline 的确切能力以源码为准）：

| 类别 | 代表目录 | 代表 architecture |
|---|---|---|
| 图像（文生图 / 图生图 / 图像编辑） | `qwen_image`、`z_image`、`glm_image`、`flux`、`flux2`、`flux2_klein`、`sdxl`、`sd3`、`hidream_image`、`ernie_image`、`longcat_image`、`boogu_image`、`ovis_image`、`lance`、`ming_flash_omni`、`helios`、`krea2`、`nextstep_1_1`、`omnigen2`、`sensenova_u1`、`dreamid_omni`、`hunyuan_image3` | `QwenImagePipeline`、`ZImagePipeline`、`FluxPipeline`、`StableDiffusionXLPipeline` … |
| 视频（文生视频 / 图生视频） | `wan2_2`、`ltx2`、`hunyuan_video`、`lingbot_video`、`dreamzero` | `WanPipeline`、`LTX2Pipeline`、`HunyuanVideo15Pipeline` … |
| 音频（文生音频 / 语音 / 歌声） | `audiox`、`stable_audio`、`omnivoice`、`soulx_singer` | `AudioXPipeline`、`StableAudioPipeline`、`OmniVoicePipeline` … |
| 全模态 / 世界模型 | `cosmos3`、`minimax_h3`、`magi_human` | `Cosmos3OmniPipeline`、`MiniMaxH3Pipeline` … |
| AR 为主 / 具身动作（VLA） | `bagel`、`internvla_a1`、`gr00t` | `BagelPipeline`、`InternVLAA1Pipeline`、`Gr00tN1d7Pipeline` … |
| 通用适配器（本讲重点） | `diffusers_adapter` | `DiffusersAdapterPipeline` |

> 说明：表里的「主要输出模态」是按目录/架构名做的粗分。少数模型同时输出多种模态（例如全模态模型），以源码里 pipeline 声明的能力协议为准（见 4.1.3 的能力协议）。

#### 4.1.3 源码精读

**注册表本体 `_DIFFUSION_MODELS`。** 这是一个大字典，key 是 architecture 名，value 是三元组 `(mod_folder, mod_relname, cls_name)`：

[registry.py:22-L22-L314](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/registry.py#L22-L314) — 注释明确写出格式为 `# arch:(mod_folder, mod_relname, cls_name)`。例如：

```python
"QwenImagePipeline": ("qwen_image", "pipeline_qwen_image", "QwenImagePipeline"),
"AudioXPipeline":    ("audiox",     "pipeline_audiox",     "AudioXPipeline"),
"DiffusersAdapterPipeline": ("diffusers_adapter", "pipeline_diffusers_adapter", "DiffusersAdapterPipeline"),
```

注意 AudioX 的 architecture 名是 `AudioXPipeline`，但实现类位于 `audiox/pipeline_audiox.py`，是少数音频模型的代表。

**把字典变成 vLLM 风格的懒加载注册表。** 借助上游 vLLM 的 `_ModelRegistry` + `_LazyRegisteredModel`，让每个模型类**按需 import**（只在真正用到时才加载对应文件，避免启动时全量 import）：

[registry.py:317-L317-L325](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/registry.py#L317-L325) — `module_name=f"vllm_omni.diffusion.models.{mod_folder}.{mod_relname}"` 即每个实现类的完整模块路径。

**实例化入口 `initialize_model`。** 它「查表 → 配置量化 → 在 `set_current_diffusion_config` 上下文里构造 → 配置 VAE → 应用序列并行」：

[registry.py:350-L350-L407](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/registry.py#L350-L407) — 关键两行：`model_class = DiffusionModelRegistry._try_load_model_cls(od_config.model_class_name)` 查表得到类，`model = model_class(od_config=od_config)` 构造实例。

**post_process 注册表 `_DIFFUSION_POST_PROCESS_FUNCS`。** 每个模型还要登记一个把「latent/张量」转成「可返回结果」的后处理函数名（例如把 latent 解码出的张量做成 PIL 图像、或把音频张量转成 numpy 数组）：

[registry.py:493-L493-L551](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/registry.py#L493-L551) — 例如 `"AudioXPipeline": "get_audiox_post_process_func"`、`"QwenImagePipeline": "get_qwen_image_post_process_func"`。`get_diffusion_post_process_func()` 会按当前 `model_class_name` 查表并通过 `_load_process_func` 动态 import 对应函数。

**能力协议（capability protocols）。** `interface.py` 用一组 `Protocol` 来声明 pipeline 支持哪些能力，框架据此判断（而非靠 if-else 硬编码）：

[interface.py:25-L25-L44](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/interface.py#L25-L44) — 例如 `SupportImageInput`（声明 `support_image_input=True` 与 `color_format`）、`SupportAudioOutput`（声明 `support_audio_output=True`）。一个 pipeline 只要继承/声明这些 `ClassVar`，就「自动具备」对应能力，框架用 `runtime_checkable` 的 `isinstance` 判断。

[interface.py:47-L47-L100](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/interface.py#L47-L100) — `SupportsStepExecution`（声明可拆成 `prepare_encode/denoise_step/step_scheduler/post_decode` 步级执行，对应 u5-l1 的 STEP_BATCH 模式）、`SupportsComponentDiscovery`（声明哪些子模块是 DiT/encoder/VAE，供 CPU offload、HSDP 切分使用）。这两条正是「原生 pipeline」才能提供、而「黑盒适配器」无法提供的能力。

**「哪些模型不支持缓存加速」白名单。** 少数模型因为结构特殊，明确不支持 TeaCache/Cache-DiT：

[registry.py:327-L327-L331](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/registry.py#L327-L331) — `_NO_CACHE_ACCELERATION = {"NextStep11Pipeline", "AudioXPipeline"}`。这也侧面说明 AudioX 这类音频模型目前没有缓存加速。

**插件式注册接口 `register_diffusion_model`。** 出于树（out-of-tree）的插件可以不修改源码就新增/替换一个模型实现：

[registry.py:590-L590-L657](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/registry.py#L590-L657) — 传入 `model_arch / module_name / class_name` 即可注册，`_DIFFUSION_MODELS[model_arch] = (module_name, "", class_name)` 用「空 mod_relname」标记「这是插件，不是内置相对路径」，`_load_process_func` 据此区分加载方式。

#### 4.1.4 代码实践

**实践目标**：亲手数一遍模型库的规模，并验证注册表的映射关系。

**操作步骤**：

1. 在仓库根目录运行（只读操作）：
   ```bash
   # 数一数 diffusion/models 下有多少个原生模型子目录
   ls -d vllm_omni/diffusion/models/*/ | grep -v diffusers_adapter | grep -v schedulers | wc -l
   # 数一数注册表里登记了多少个 architecture
   grep -cE '^\s+"[A-Za-z0-9]+Pipeline":\s*\(' vllm_omni/diffusion/registry.py
   ```
2. 在 Python 里验证一次懒加载查表（不加载权重，只解析类）：
   ```python
   from vllm_omni.diffusion.registry import DiffusionModelRegistry
   for arch in ["QwenImagePipeline", "AudioXPipeline", "DiffusersAdapterPipeline"]:
       cls = DiffusionModelRegistry._try_load_model_cls(arch)
       print(arch, "->", cls.__module__, cls.__name__)
   ```

**需要观察的现象**：第一步两个数字都应在 40 以上（architecture 数大于目录数，因为存在别名，如 `OmniVoice` 与 `OmniVoicePipeline` 都指向同一实现）。

**预期结果**：第二步应打印每个 architecture 对应的真实类路径，例如 `QwenImagePipeline -> vllm_omni.diffusion.models.qwen_image.pipeline_qwen_image QwenImagePipeline`。

#### 4.1.5 小练习与答案

**练习 1**：注册表里 `OmniVoice` 和 `OmniVoicePipeline` 两个 key 指向的是同一个实现类吗？为什么要有两个 key？

**参考答案**：是的，二者都映射到 `("omnivoice", "pipeline_omnivoice", "OmniVoicePipeline")`（见 registry.py 中两个相邻条目）。保留两个 key 通常是为了兼容不同来源模型在 `model_index.json` 里填写的不同 `_class_name`，让「类名」和「架构名」两种写法都能命中同一实现。

**练习 2**：如果某模型被放进 `_NO_CACHE_ACCELERATION` 集合，意味着什么？

**参考答案**：意味着该 pipeline 即使在配置里开启了 TeaCache 或 Cache-DiT，也不会获得缓存加速效果——因为这些加速需要钩入 transformer block 内部，而这几个模型结构暂不支持。

---

### 4.2 diffusers_adapter：黑盒通用适配器

#### 4.2.1 概念说明

并不是所有模型都值得（或来得及）写一套原生实现。`diffusers_adapter` 给出了一条**零代码上线**的捷径：只要这个模型能用 🤗 Diffusers 的 `DiffusionPipeline.from_pretrained()` 加载，vLLM-Omni 就能用 `DiffusersAdapterPipeline` 把它整体包起来服务化。

`DiffusersAdapterPipeline` 的设计哲学写在它的模块 docstring 里——「**黑盒封装（black-box wrapper）**」「**几乎无需为每个模型单独编写代码（near-zero per-model code）**」，它把整条流水线的执行**完整委托**给 diffusers 自己的 `__call__()`。代价是它明确放弃了若干需要「钻进 transformer 内部」才能实现的高级特性。

#### 4.2.2 核心流程

适配器的工作流程分三段：

**① 加载（`load_weights`）**
```
读 od_config.model / dtype
  → convert_diffusers_quantization_config（把 omni 量化配置翻译成 diffusers 的）
  → 取该 pipeline 专属的 PipelineUtils 钩子（如 Wan 系列）
  → DiffusionPipeline.from_pretrained(model_id, **load_kwargs)
  → pipeline.to(device)
  → 按 od_config 开启 CPU offload / VAE slicing / VAE tiling
  → _set_attention_backend（把 omni 的注意力后端名翻译成 diffusers 的）
```

**② 前向（`forward`）**
```
_build_call_kwargs(req)
  → _extract_input：把 OmniPromptType 列表拆成 prompt / negative_prompt / 多模态数据
  → 合并 三层默认值：load_time 默认 → input → request-time 采样参数
  → 对齐 __call__ 签名（用 inspect 检查哪些 kwarg 真正被接受）
  → 处理 output_type / num_*_per_prompt / generator / seed
torch.inference_mode() 下 self._pipeline(**kwargs)
  → _wrap_output：按 diffusers 输出对象有 images / frames / audios 属性，包成 DiffusionOutput
```

**③ 拒绝步级执行**
- `prepare_encode / denoise_step / step_scheduler / post_decode` 四个方法全部 `raise NotImplementedError`，因为 diffusers 把整个去噪循环封装在 `__call__` 内部，无法拆给 vLLM-Omni 的连续批处理调度器。

**能力边界（在构造期 `_raise_unsupported_features` 里硬性拦截）**：

| 配置 | 是否支持 | 原因 |
|---|---|---|
| CFG 并行（`cfg_parallel_size > 1`） | ❌ | diffusers 内部用 `guidance_scale` 自己处理 CFG |
| 序列并行（`sequence_parallel_size > 1`） | ❌ | 需要对注意力做「模型专属的手术（surgery）」 |
| 缓存加速（TeaCache / Cache-DiT） | ❌ | 需要钩入 transformer block |
| Eager 执行 / 步级连续批处理 | ❌ | 同上，循环被封装在 `__call__` 内 |

需要这些特性时，应改用**原生 pipeline**。

#### 4.2.3 源码精读

**模块 docstring 直接写明能力边界**（这是判断「该不该用适配器」的最快依据）：

[pipeline_diffusers_adapter.py:3-L3-L14](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/diffusers_adapter/pipeline_diffusers_adapter.py#L3-L14) — 明确列出 NOT supported 的四项：CFG parallel / sequence parallel / TeaCache / step-wise execution。

**类声明与能力标记**：

[pipeline_diffusers_adapter.py:54-L54-L69](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/diffusers_adapter/pipeline_diffusers_adapter.py#L54-L69) — `supports_request_batch = False` 与 `supports_step_execution = False` 两个 ClassVar，告诉引擎「我只能请求级整批跑，不能步级跑」。

**加载逻辑 `load_weights`**：

[pipeline_diffusers_adapter.py:92-L92-L148](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/diffusers_adapter/pipeline_diffusers_adapter.py#L92-L148) — 核心是 `self._pipeline = DiffusionPipeline.from_pretrained(model_id, **load_kwargs)`，随后 `_set_attention_backend()` 把注意力后端设好。第 122 行 `inspect.signature(self._pipeline.__call__).parameters.keys()` 缓存了 `__call__` 的合法参数表，供后续 `_build_call_kwargs` 做输入校验。

**前向 `forward`：极简的全权委托**：

[pipeline_diffusers_adapter.py:181-L181-L189](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/diffusers_adapter/pipeline_diffusers_adapter.py#L181-L189) — 只有 `kwargs = self._build_call_kwargs(req)` → `output = self._pipeline(**kwargs)` → `return self._wrap_output(output)` 三步，真正的前向算子完全交给 diffusers。

**输入翻译 `_extract_input`**：

[pipeline_diffusers_adapter.py:398-L398-L485](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/diffusers_adapter/pipeline_diffusers_adapter.py#L398-L485) — 把 vLLM-Omni 的 `OmniPromptType`（字符串或 `OmniTextPrompt` 字典，可能带 `negative_prompt` 与 `multi_modal_data`）翻译成 diffusers 期望的 `prompt` / `negative_prompt` / 多模态键。多 prompt 时还要处理「负向 prompt 必须是 list[str] 不能含 None」的约束。

**输出包装 `_wrap_output`**：按 diffusers 输出对象的属性自动判别 modality：

[pipeline_diffusers_adapter.py:487-L487-L507](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/diffusers_adapter/pipeline_diffusers_adapter.py#L487-L507) — 有 `images` → 图像、有 `frames` → 视频、有 `audios` → 音频，分别包成 `DiffusionOutput(output=...)`。这就是适配器能同时服务图像/视频/音频模型的关键。

**配置阶段的路由 `enrich_config`**：当用户传 `--diffusion-load-format diffusers`，配置层就把 `model_class_name` 钉死为 `DiffusersAdapterPipeline`，并从 `model_index.json` 的 `_class_name` 读出真正的 diffusers pipeline 类：

[data.py:1147-L1147-L1169](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/data.py#L1147-L1169) — `self.model_class_name = "DiffusersAdapterPipeline"`；`self.diffusers_pipeline_cls = getattr(diffusers, diffusers_pipeline_cls_name)` 把真正的 pipeline 类（如 `FluxPipeline`）记下来，供 `load_weights` 使用。

**少数模型的「专属钩子」**：绝大部分 diffusers pipeline 不需要任何特殊处理，但 Wan 系列视频模型需要把 `boundary_ratio` / `flow_shift` 在**加载时**（而非推理时）注入，因此用一张小注册表挂上 `WanPipelineUtils`：

[pipeline_utils.py:51-L51-L64](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/diffusers_adapter/pipeline_utils.py#L51-L64) — `PIPELINE_UTILS_REGISTRY` 按 pipeline 类名查表，`get_pipeline_utils()` 找不到就返回空操作的 `BasePipelineUtils`。[pipeline_utils.py:25-L25-L48](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/diffusers_adapter/pipeline_utils.py#L25-L48) 即 Wan 的实现：`update_load_kwargs` 注入 `boundary_ratio`，`apply_post_load_updates` 用 `flow_shift` 重建 scheduler，`validate_runtime_sampling_params` 拒绝运行时再改这两个值。

#### 4.2.4 代码实践

**实践目标**：用 diffusers 后端零代码上线一个模型，并验证它与原生后端的命令差异。

**操作步骤**：

1. 阅读 [examples/online_serving/diffusers_pipeline_adapter/README.md](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/examples/online_serving/diffusers_pipeline_adapter/README.md) 的 Usage 与 Model Support 两节。
2. 启动一个用 diffusers 后端的服务（需要可用 GPU 与已下载的权重；若无环境，则只做命令阅读）：
   ```bash
   vllm serve "stable-diffusion-v1-5/stable-diffusion-v1-5" \
       --omni \
       --diffusion-load-format diffusers \
       --port 8091
   ```
3. 用 `curl` 向 `/v1/chat/completions` 发一条文生图请求（参考 u1-l5 的取图管道）。
4. 故意加上一个不兼容的开关，观察拦截行为：
   ```bash
   vllm serve "Qwen/Qwen-Image" --omni --diffusion-load-format diffusers \
       --diffusion-attention-config '{"sequence_parallel_size": 2}'   # 预期在构造期被拦截
   ```

**需要观察的现象**：第 4 步应在模型构造阶段抛出 `NotImplementedError: Sequence parallel is not supported with the diffusers backend.`

**预期结果**：能复现 `_raise_unsupported_features` 的拦截；正常启动（第 2 步）后能拿到 base64 图像。

**待本地验证**：第 2、3 步需要真实 GPU 与权重，若无环境请标注「待本地验证」，只完成第 1、4 步的命令与拦截逻辑阅读。

#### 4.2.5 小练习与答案

**练习 1**：为什么 diffusers 适配器要把 `_wrap_output` 按对象的属性来判别输出类型，而原生 pipeline 不需要？

**参考答案**：因为适配器是「黑盒」——它不知道调用的具体 diffusers pipeline 会返回什么类型，只能看返回对象身上有没有 `images`/`frames`/`audios` 属性来反推 modality。原生 pipeline 自己就是 `XxxPipeline`，作者清楚自己输出的是图像还是音频，直接构造对应 `DiffusionOutput` 即可。

**练习 2**：`_build_call_kwargs` 里合并默认值的顺序是「load_time → input → request-time」，这个顺序为什么重要？

**参考答案**：后合并的会覆盖先合并的。把 request-time 采样参数放在最后，意味着「用户每次请求指定的参数」优先级最高；load_time 默认值优先级最低。这与 README 里「vLLM-Omni 接口参数与 `diffusers_call_kwargs` 冲突时，前者（请求时）优先」的描述一致。

---

### 4.3 自定义 Pipeline：custom_pipeline 扩展机制

#### 4.3.1 概念说明

`diffusers_adapter` 解决的是「零代码上线已有模型」。但有时你想在**已有 pipeline 基础上加点东西**——比如记录每一步的 latent 轨迹、改写采样步数、注入自定义日志——又不想 fork 整个 pipeline 源码。这就是 `custom_pipeline` 扩展机制要解决的场景。

它由三个互相配合的特性组成（见 `docs/features/custom_pipeline.md`）：

1. **`diffusion_load_format`**：控制模型初始加载方式，关键取值有 `"default"`（走模型注册表的原生实现）、`"dummy"`（跳过初始加载，配合自定义 pipeline 使用）、`"diffusers"`（走 4.2 的黑盒适配器）。
2. **`CustomPipelineWorkerExtension`**：一个 mixin，给 worker 增加一个 `re_init_pipeline(custom_pipeline_args)` 方法，可在加载后用自定义实现**重建** pipeline。
3. **`WorkerWrapperBase`**：让 worker 类支持「动态继承」一个扩展类（`worker_extension_cls`），从而把任意自定义方法挂到 worker 上。

#### 4.3.2 核心流程

自定义 pipeline 的典型用法（离线推理）：

```
用户继承一个已有 pipeline（如 QwenImageEditPipeline）写 CustomPipeline
  → Omni(model=..., diffusion_load_format="dummy",
         custom_pipeline_args={"pipeline_class": "custom_pipeline.CustomPipeline"})
     ├─ "dummy" 让 worker 跳过标准模型加载
     └─ custom_pipeline_args 触发 CustomPipelineWorkerExtension.re_init_pipeline()
         → 用 CustomPipeline 重建 pipeline（替换掉占位实例）
  → omni.generate(...) 时实际跑的是 CustomPipeline.forward()
```

`forward` 里通常先 `super().forward(req)` 拿到正常输出，再往输出对象上**追加自定义字段**（例如 `output.trajectory_latents = ...`），随后在结果侧通过 `outputs[0].request_output` 读回。

#### 4.3.3 源码精读

**官方指南 `custom_pipeline.md` 给出三个特性的定位**：

[custom_pipeline.md:10-L10-L15](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/features/custom_pipeline.md#L10-L15) — 列出三大特性：`WorkerWrapperBase`、`diffusion_load_format`、`CustomPipelineWorkerExtension`。

**`diffusion_load_format` 的取值语义**：

[custom_pipeline.md:31-L31-L38](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/features/custom_pipeline.md#L31-L38) — `"default"` 走模型注册表；`"dummy"` 跳过初始加载（配合 `custom_pipeline_args["pipeline_class"]`）；`"diffusers"` 走 4.2 的黑盒适配器。位置标注在 `vllm_omni/diffusion/data.py` 的 `OmniDiffusionConfig`。

**自定义 pipeline 的最小骨架（示例代码）**：

[custom_pipeline.md:62-L62-L78](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/features/custom_pipeline.md#L62-L78) — 继承 `QwenImageEditPipeline`，在 `forward` 里先调 `super().forward(req=req)`，再追加 `output.trajectory_timesteps` 与 `output.trajectory_latents` 两个自定义字段。

**用 Omni 加载自定义 pipeline**：

[custom_pipeline.md:90-L90-L96](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/features/custom_pipeline.md#L90-L96) — 关键是 `diffusion_load_format="dummy"` 配合 `custom_pipeline_args={"pipeline_class": "custom_pipeline.CustomPipeline"}`，`pipeline_class` 用「模块名.类名」字符串指定（框架据此动态 import）。

**完整可运行示例**位于 `examples/offline_inference/custom_pipeline/image_to_image/`（含 `custom_pipeline.py`、`image_edit.py`、`run.sh`），对应 `custom_pipeline.md` Step 3 给出的运行命令。

#### 4.3.4 代码实践

**实践目标**：读懂一个真实的自定义 pipeline 示例，理清「继承 → 改 forward → 追加字段 → 读回」的链路。

**操作步骤**：

1. 打开 [examples/offline_inference/custom_pipeline/image_to_image/custom_pipeline.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/examples/offline_inference/custom_pipeline/image_to_image/custom_pipeline.py) 与同目录 `image_edit.py`。
2. 对照 `custom_pipeline.md` 的 Step 1/Step 2，标注：
   - `CustomPipeline` 继承自哪个原生 pipeline？
   - 它在 `forward` 里改写了什么（示例里是采样步数）？
   - 它往输出对象追加了哪些自定义字段？
   - `image_edit.py` 里是如何把 `custom_pipeline_args` 传给 `Omni(...)` 的？
3. （源码阅读型）假设你要把「每步去噪的噪声预测张量」也记录下来，思考：应该在 `super().forward()` 之前还是之后获取？为什么示例选择「之后」追加 trajectory？

**需要观察的现象**：示例的 `forward` 是「先拿到正常输出 → 再追加元数据」，因此不会干扰原生去噪逻辑。

**预期结果**：能画出 `Omni(custom_pipeline_args=...) → WorkerExtension.re_init_pipeline → CustomPipeline.forward → outputs[0].request_output` 的调用链。

**待本地验证**：真正运行 `run.sh` 需要权重与 GPU；若无可标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么自定义 pipeline 要配 `diffusion_load_format="dummy"`，而不是 `"default"`？

**参考答案**：`"default"` 会按注册表加载标准 pipeline 实例并占住位置；而自定义流程需要在 worker 启动后用 `CustomPipelineWorkerExtension.re_init_pipeline()` **重建** pipeline。`"dummy"` 跳过初始的标准加载，留出位置给自定义类去重建，避免加载两次权重。

**练习 2**：`custom_pipeline_args["pipeline_class"]` 用的是字符串 `"custom_pipeline.CustomPipeline"` 而不是直接传类对象，这样做的好处是什么？

**参考答案**：因为 worker 运行在**独立子进程**里（见 u5-l3），主进程的类对象无法直接跨进程传递。用「模块名.类名」字符串，由子进程自己 `importlib` 动态导入，既跨进程安全，也允许用户在不改动 vLLM-Omni 源码的前提下注入自己仓库里的类。

---

## 5. 综合实践：对比一个图像模型与一个音频模型

这是本讲的主干实践任务，把 4.1～4.3 串起来。请挑一个**图像**模型（推荐 `QwenImagePipeline`，目录 `qwen_image/`）和一个**音频**模型（推荐 `AudioXPipeline`，目录 `audiox/`），比较它们的「输入 prompt 类型」与「输出 modality」。

### 步骤 1：定位两者的实现类与能力声明

**图像 — QwenImagePipeline**：

[pipeline_qwen_image.py:266-L266-L274](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/qwen_image/pipeline_qwen_image.py#L266-L274) — 类声明为 `class QwenImagePipeline(nn.Module, QwenImageCFGParallelMixin, DiffusionPipelineProfilerMixin, SupportsComponentDiscovery)`，并声明 `supports_request_batch = True`、`supports_step_execution = True`，`_dit_modules=["transformer"]`、`_vae_modules=["vae"]`。这说明它是**原生实现**，支持请求级批处理、步级执行、CFG 并行，输出为**图像**（由 VAE 解码 latent 得到，post_process 把张量做成 PIL 图像，见 [pipeline_qwen_image.py:88-L88-L115](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/qwen_image/pipeline_qwen_image.py#L88-L115) 的 `get_qwen_image_post_process_func`，用 `VaeImageProcessor.postprocess`）。

**音频 — AudioXPipeline**：

[pipeline_audiox.py:370-L370-L377](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/audiox/pipeline_audiox.py#L370-L377) — 类声明为 `class AudioXPipeline(nn.Module, SupportAudioOutput, DiffusionPipelineProfilerMixin)`，`support_audio_output = True`、`audio_sample_rate = 44100`、`audio_channels = 2`，且 `supports_request_batch = False`。它直接声明了 `SupportAudioOutput` 协议，输出为**音频波形**（post_process 把音频张量转成 CPU numpy，见 [pipeline_audiox.py:58-L58-L76](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/audiox/pipeline_audiox.py#L58-L76) 的 `get_audiox_post_process_func`）。

### 步骤 2：对比输入 prompt 类型

- **QwenImagePipeline（文生图）**：输入主要是**文本 prompt**（经 Qwen2.5-VL 文本编码器编码）；编辑类（`QwenImageEditPipeline`）额外接受**参考图像**输入（声明 `SupportImageInput`）。
- **AudioXPipeline**：输入是**多模态条件**——文本（T5 编码）+ 可选的视频/图像参考 + 可选的音频参考。文件顶部 `_TEXT_VIDEO_TASKS` / `_VIDEO_CONDITIONED_TASKS` 等常量（[pipeline_audiox.py:30-L30-L38](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/audiox/pipeline_audiox.py#L30-L38)）说明它按「任务类型」决定需要哪些条件输入。

### 步骤 3：写一份对比说明（建议填入下表）

| 维度 | QwenImagePipeline（图像） | AudioXPipeline（音频） |
|---|---|---|
| 主要输入 | 文本 prompt（编辑类含参考图像） | 文本 + 视频/图像/音频参考（按任务类型） |
| 输出 modality | 图像（PIL，经 VAE 解码） | 音频波形（numpy，44.1kHz / 2 声道） |
| 能力协议 | `SupportsComponentDiscovery`、`supports_step_execution=True` | `SupportAudioOutput` |
| 请求级批处理 | 支持（`supports_request_batch=True`） | 不支持（`supports_request_batch=False`） |
| 缓存加速 | 支持（不在 `_NO_CACHE_ACCELERATION`） | **不支持**（在 `_NO_CACHE_ACCELERATION`，见 [registry.py:327-L327-L331](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/registry.py#L327-L331)） |
| post_process 作用 | `VaeImageProcessor.postprocess` 把张量变 PIL | 把音频张量 `.cpu().float().numpy()` |

### 步骤 4：用注册表验证两者确实被登记

```bash
grep -nE '"QwenImagePipeline"|"AudioXPipeline"' vllm_omni/diffusion/registry.py
grep -nE '"QwenImagePipeline"|"AudioXPipeline"' vllm_omni/diffusion/registry.py | head   # 同时确认 post_process 注册
```

**预期结果**：能在 `_DIFFUSION_MODELS`（约 registry.py:24 与 registry.py:234）和 `_DIFFUSION_POST_PROCESS_FUNCS`（约 registry.py:497 与 registry.py:514）里分别找到这两个 architecture 的登记行。

> 若无 GPU/权重环境，本实践以「源码阅读 + 填表」形式完成即可；切勿伪造运行结果。

---

## 6. 本讲小结

- vLLM-Omni 的扩散模型库由注册表 `_DIFFUSION_MODELS`（registry.py）统一管理，把「architecture 名」映射到 `(mod_folder, mod_relname, cls_name)`，覆盖图像、视频、音频、全模态、VLA 等 50+ 架构。
- 每个模型还会登记一个 `post_process_func`（把 latent/张量变成可返回结果），少数登记 `pre_process_func`；插件可通过 `register_diffusion_model` 出于树注册。
- pipeline 的「能力」通过 `interface.py` 的 `Protocol`（`SupportImageInput` / `SupportAudioOutput` / `SupportsStepExecution` / `SupportsComponentDiscovery`）声明，框架据此判断而非硬编码。
- `DiffusersAdapterPipeline` 是「黑盒通用适配器」：把整条流水线委托给 `DiffusionPipeline.from_pretrained() + __call__()`，零代码上线任意 diffusers 模型，但放弃 CFG 并行 / 序列并行 / TeaCache / 步级连续批处理。
- 适配器靠 `_extract_input`（翻译 prompt）/ `_build_call_kwargs`（对齐 `__call__` 签名）/ `_wrap_output`（按 images/frames/audios 属性判别 modality）三步在 vLLM-Omni 与 diffusers 之间做翻译；配置层 `--diffusion-load-format diffusers` 负责路由。
- 自定义 pipeline 通过 `diffusion_load_format="dummy"` + `custom_pipeline_args` + `CustomPipelineWorkerExtension` 实现「在已有 pipeline 上叠加逻辑」而无需 fork 源码。

## 7. 下一步学习建议

- 想了解「原生 pipeline 内部到底怎么去噪」，回看 [u5-l4](u5-l4-diffusion-pipeline.md)（去噪数据流）与 [u5-l3](u5-l3-diffusion-worker-loader.md)（worker 与模型加载）。
- 想动手**新增一个原生 diffusion 模型**（写 transformer adapter、声明 attention role、接入 TP/SP/缓存），进入 U9 的 [u9-l1](u9-l1-add-diffusion-model.md)（添加新 Diffusion 模型）。
- 想给原生 pipeline 启用 TeaCache / Cache-DiT / 并行策略等「黑盒适配器拿不到」的加速，进入 U7 的 [u7-l3](u7-l3-cache-acceleration.md)（缓存加速）与 [u7-l4](u7-l4-parallel-strategies.md)（并行策略）。
- 想理解注册表里的 `post_process_func` 产出的结果如何被在线 API 返回，可衔接 U6 的 [u6-l2](u6-l2-multimodal-endpoints.md)（多模态端点）。
