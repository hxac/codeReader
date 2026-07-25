# VisualGen 扩展

## 1. 本讲目标

TensorRT-LLM 主线是「LLM 自回归推理」，但你可能已经注意到 `import tensorrt_llm` 时，除了 `LLM` / `SamplingParams` 之外，顶层还导出了 `VisualGen` / `VisualGenArgs` / `VisualGenParams`。这一条产品线叫 **VisualGen**，专门做扩散模型（Diffusion Transformer, DiT）的图像与视频生成（text-to-image、text-to-video、image-to-video）。

它**不是** LLM 的一个后端或插件，而是一套**独立**的推理子系统：有自己的引擎、自己的请求/参数/输出类型、自己的工程准则。本讲的目标是让你在读完之后能够：

1. 说清 VisualGen 与 LLM 后端**为什么必须分开**（独立引擎 / 独立参数 / 独立输出 / 独立注册表），不再把它误当成 `LLM` 的一个配置项。
2. 掌握公共 API 三件套 `VisualGen` / `VisualGenArgs` / `VisualGenParams` 的职责边界与典型用法，能写一段最小可运行（或可读）的生成脚本。
3. 理解 VisualGen 的工程纪律：哪些代码是「公共 API 面」、哪些是「内部实现」，以及为什么修改公共面要格外谨慎。
4. 建立「public 门面 → internal worker → pipeline」的调用链心智，为以后深入 `tensorrt_llm/_torch/visual_gen/` 打下基础。

## 2. 前置知识

本讲依赖你已经建立的「TensorRT-LLM 三条产品线」认知（见 u1-l1）和「`import tensorrt_llm` 顶层导出」认知（见 u2-l2）。在进入 VisualGen 之前，先澄清几个本讲用到的核心概念。

- **扩散模型（Diffusion Model）与 DiT。** 与 LLM「逐 token 自回归」完全不同，扩散模型先把一张图/一段视频表示成一组「潜变量（latent）」，再通过反复「去噪（denoise）」把它逐步还原成干净内容，最后用 **VAE** 把潜变量解码成真正的像素。当这个去噪骨干网络（backbone）换成 Transformer 时，就叫 **Diffusion Transformer（DiT）**。一次生成大致是：

  \[
  \text{文本} \xrightarrow{\text{text encoder}} \text{embedding} \quad,\quad
  \text{噪声 latent} \xrightarrow{N\ \text{步去噪（DiT 前向）}} \text{干净 latent} \xrightarrow{\text{VAE}} \text{像素}
  \]

  注意它与 LLM 的根本区别：**LLM 一个前向产一个 token；DiT 一个前向只是去噪一步**，通常要走几十步（`num_inference_steps`）才得到结果。

- **CFG（Classifier-Free Guidance，无分类器引导）。** 用「正向提示」和「负向提示」两条路径一起前向，再按 `guidance_scale` 把它们的差值放大，从而让生成更贴合用户意图。因为要同时跑两条路径，所以天然可以被拆到两张卡上并行（CFG parallel）。

- **Pydantic 配置模型。** VisualGen 的所有公共类都用 Pydantic 编写，规则与 `TorchLlmArgs`（见 u4-l1）一脉相承：`StrictBaseModel`（`extra="forbid"`，拼错字段直接报错）、`Field(status=..., description=...)` 既做校验又做文档、`model_validator` 做跨字段校验。

- **API 成熟度标记。** 你会在源码里看到大量 `@set_api_status("prototype")` 装饰器——它标注 VisualGen 整个公共面**仍是 prototype（预览）阶段**，未来可能有破坏性改动。这一点直接决定了本讲「工程准则」一节的态度。

> 一句话记忆：**LLM 产出的是一串 token；VisualGen 产出的是一块像素张量（image / video / audio）。** 产出的东西不同，所以一切都要分开。

## 3. 本讲源码地图

本讲聚焦 **公共 API 面** `tensorrt_llm/visual_gen/`，并指向其背后的内部实现 `tensorrt_llm/_torch/visual_gen/`。

| 文件 | 作用 |
|---|---|
| [tensorrt_llm/visual_gen/__init__.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/visual_gen/__init__.py) | 公共门面：聚合导出所有公共类（`VisualGen` / `VisualGenArgs` / `VisualGenParams` 等） |
| [tensorrt_llm/visual_gen/visual_gen.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/visual_gen/visual_gen.py) | 高层入口 `VisualGen` 类：管理 worker、`generate()` / `generate_async()`、`VisualGenResult` 句柄 |
| [tensorrt_llm/visual_gen/args.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/visual_gen/args.py) | 引擎级配置 `VisualGenArgs` 及其子配置（并行、注意力、缓存、编译等） |
| [tensorrt_llm/visual_gen/params.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/visual_gen/params.py) | 每次生成请求的参数 `VisualGenParams` 与校验函数 |
| [tensorrt_llm/visual_gen/output.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/visual_gen/output.py) | 输出类型 `VisualGenOutput` / `VisualGenMetrics`，带 `.save()` 落盘 |
| [tensorrt_llm/__init__.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/__init__.py) | 顶层包，把 VisualGen 公共类 re-export 到 `tensorrt_llm` 命名空间 |
| [tensorrt_llm/_torch/visual_gen/pipeline_registry.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/visual_gen/pipeline_registry.py) | （内部）流水线注册表 `PIPELINE_REGISTRY`，靠 `@register_pipeline` 自注册 |
| [tensorrt_llm/_torch/visual_gen/pipeline.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/visual_gen/pipeline.py) | （内部）`BasePipeline` 基类：去噪主循环、CFG、TeaCache/Cache-DiT、`ExtraParamSchema` |
| [tensorrt_llm/_torch/visual_gen/executor.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/visual_gen/executor.py) | （内部）多 GPU 执行器 `DiffusionExecutor` 与客户端 `DiffusionRemoteClient` |
| [tensorrt_llm/_torch/visual_gen/ENGINEERING_CRITERIA.md](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/visual_gen/ENGINEERING_CRITERIA.md) | 工程准则：新模型、API 纪律、特性、示例与文档、测试分层 |
| [docs/source/models/visual-generation.md](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/models/visual-generation.md) | 用户文档：支持的模型矩阵、量化、并行、step caching、开发者指南 |

## 4. 核心概念与源码讲解

### 4.1 VisualGen 公共 API：与 LLM 后端的彻底分离

#### 4.1.1 概念说明

本节回答第一个问题：**VisualGen 到底是什么？** 它是 TensorRT-LLM 内部一条与 LLM 推理**并行**的子系统，专门服务于扩散模型。官方文档把它定位为「统一的扩散模型推理栈」，并且用一句话划清了边界：

> VisualGen is a parallel inference subsystem within TensorRT-LLM. It shares low-level primitives (`Mapping`, `QuantConfig`, `Linear`, `RMSNorm`, `ZeroMqQueue`, `TrtllmAttention`) but has its own executor, scheduler (diffusers-based), request types, and pipeline architecture separate from the LLM autoregressive decode path.

这句话是理解整个 VisualGen 的钥匙，拆开看有三层意思：

1. **它不是新引擎造完一切，而是「复用底层、重做上层」。** 低层原语——并行拓扑 `Mapping`（见 u9-l1）、量化描述 `QuantConfig`（见 u4-l3 / u10-l2）、线性层 `Linear`、`RMSNorm`、注意力内核 `TrtllmAttention`——都和 LLM 后端**共用**。这也正是「VisualGen 扩展」这个名字的由来：它扩在 LLM 之上，但不重复造轮子。
2. **但执行器、调度器、请求类型、流水线架构全是独立的。** LLM 后端的 `PyExecutor` 单步循环、KV cache、in-flight batching（见 u3-l2 / u8-l1）在这里**完全用不上**——扩散模型没有「自回归解码」这回事。
3. **正因如此，它需要一套独立的公共 API**，而不是塞进 `LLM` 的某个参数里。这就是为什么顶层会同时导出 `LLM` 和 `VisualGen` 两个对等的高层类。

#### 4.1.2 核心流程

VisualGen 的高层调用链是一条「public 门面 → internal 客户端 → worker 进程 → pipeline」的委托链：

```text
用户脚本
  │  VisualGen(model, args)        # 公共门面（tensorrt_llm/visual_gen/visual_gen.py）
  │  ├── 自动探测是否被 torchrun/srun 外部启动
  │  └── 构造 DiffusionRemoteClient  # internal 客户端/协调器
  │
  │  visual_gen.generate_async(prompt, params) → VisualGenResult
  │     ├── 在协调器进程解析 seed、校验 params
  │     └── 把 DiffusionRequest 经 ZeroMQ IPC 发给 worker
  │
  ▼
DiffusionExecutor (worker 进程，每个 GPU 一个)   # tensorrt_llm/_torch/visual_gen/executor.py
  │  PipelineLoader.load() → AutoPipeline 选 pipeline → BasePipeline 子类
  │  pipeline.infer(req)：
  │     文本编码 → 构造噪声 latent → denoise 主循环（DiT 前向 N 步）→ VAE 解码
  ▼
DiffusionResponse → DiffusionRemoteClient → VisualGenOutput（给用户）
```

关键设计点：

- **多 GPU 用「进程」而非「线程」。** 每个 GPU 跑一个独立的 worker 进程，靠 ZeroMQ IPC 通信。这和 LLM 后端「单进程多 rank + 进程组」的分布式风格不同。
- **协调器与 worker 可被外部启动器复用。** 默认情况下 `DiffusionRemoteClient` 自己用 `mp.Process` 在本地拉起所有 worker；但如果是多机场景，用户用 `torchrun` / `srun` 启动，那么 rank 0 当协调器，rank 1..N-1 直接进 `run_diffusion_worker` 然后 `sys.exit(0)`，**永远不会返回到用户代码**。
- **seed 在公共边界就定死。** `generate_async` 会在协调器进程一次性生成 seed 并广播，保证多 rank 并行（CFG / Ulysses）下的确定性。

#### 4.1.3 源码精读

先看顶层包怎么把 VisualGen 和 LLM 平起平坐地导出。`tensorrt_llm/__init__.py` 在导入 LLM 一族之后，紧接着导入 VisualGen 一族：

顶层 re-export（把 VisualGen 公共类放到 `tensorrt_llm` 命名空间）：

[tensorrt_llm/__init__.py:127-129](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/__init__.py#L127-L129) —— 把 `VisualGen` / `VisualGenArgs` / `VisualGenParams` / `VisualGenResult` / `VisualGenOutput` / `VisualGenMetrics` / `ExtraParamSchema` 从 `.visual_gen` 子包 re-export，与 `LLM` 并列进 `__all__`。这就是 `from tensorrt_llm import VisualGen` 能直接生效的原因。

公共门面 `__init__.py` 再做一层聚合与文档说明：

[tensorrt_llm/visual_gen/__init__.py:15-23](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/visual_gen/__init__.py#L15-L23) —— 模块 docstring 明确「入口类同时也从 `tensorrt_llm` 顶层 re-export」，并强调「跨切面子配置只存在于本子包」。这界定了公共面的范围。

[tensorrt_llm/visual_gen/__init__.py:27-44](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/visual_gen/__init__.py#L27-L44) —— 从 `args.py`、`output.py`、`params.py`、`visual_gen.py` 把公共类聚拢，再 re-export `QuantConfig` 便于用户直接配量化。注意它从 **internal** 包 `tensorrt_llm._torch.visual_gen` 反向导入了 `ExtraParamSchema`——这是少数「内部定义、公共面暴露」的例外。

核心入口类 `VisualGen` 本体非常薄，主要做三件事：探测外部启动、构造客户端、提供 `generate` 系列方法：

[tensorrt_llm/visual_gen/visual_gen.py:176-177](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/visual_gen.py#L176-L177) —— `class VisualGen: """High-level API for visual generation."""` 类定义。

[tensorrt_llm/visual_gen/visual_gen.py:225-273](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/visual_gen.py#L225-L273) —— 构造函数。它先把 `model` 写进 `args`，再调用 `_detect_external_launch()` 判断是否处于 `torchrun`/`srun` 外部启动模式；如果是且当前 `rank != 0`，就直接调用 `run_diffusion_worker(...)` 后 `sys.exit(0)`——这就是「rank 1..N-1 永不返回用户代码」的实现。只有 rank 0（或单机默认分支）才会继续构造 `DiffusionRemoteClient` 当协调器。

`_detect_external_launch` 与 worker 角色的判定：

[tensorrt_llm/visual_gen/visual_gen.py:236-266](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/visual_gen.py#L236-L266) —— 外部启动模式分支：校验 `world_size == n_workers`，对非零 rank 打日志后转 worker 并退出。

`generate` / `generate_async` 的「同步 = 异步 + 阻塞」模式（与 `LLM.generate` 同构，见 u3-l1）：

[tensorrt_llm/visual_gen/visual_gen.py:311-336](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/visual_gen.py#L311-L336) —— `generate()` 内部就是 `self.generate_async(...).result(timeout=None)`，把异步句柄阻塞成同步返回。

[tensorrt_llm/visual_gen/visual_gen.py:338-417](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/visual_gen.py#L338-L417) —— `generate_async()` 是真正干活的入口。三段逻辑值得细看：(1) 把单个字符串 prompt 归一化成 `List[str]` 并记住「是不是 batch」以便返回正确形状；(2) 对用户传入的 `params` 做**深拷贝快照**并同步校验（这样后续用户修改不影响已入队请求）；(3) 关键的 **seed 具象化**——见下条。

seed 在公共边界一次性定死（保证多 rank 确定性）：

[tensorrt_llm/visual_gen/visual_gen.py:402-417](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/visual_gen.py#L402-L417) —— 当 `seed is None` 时，用 `secrets.randbits(63)` 在协调器进程生成一个 63 位随机种子，连同 `DiffusionRequest` 一起广播，使下游每个 rank、每次 CFG/Ulysses 分支都看到同一个确定值。

异步句柄 `VisualGenResult`（future-like，三种等待姿势）：

[tensorrt_llm/visual_gen/visual_gen.py:47-66](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/visual_gen.py#L47-L66) —— `VisualGenResult` 同时支持 `await handle`（异步）、`aresult(timeout=...)`（显式协程）、`result(timeout=...)`（阻塞，供非异步调用方）三种姿势；同一个实例同时支撑单 prompt（解析出单个 `VisualGenOutput`）和 batch（解析出 `List[VisualGenOutput]`）。

#### 4.1.4 代码实践

**实践目标：** 看懂 VisualGen 最小用法，并体会它「和 LLM 长得像、但产物不同」。

仓库自带 quickstart 脚本是最佳起点：

[examples/visual_gen/quickstart_example.py:1-26](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/examples/visual_gen/quickstart_example.py#L1-L26) —— 整个脚本只有三步：构造 `VisualGen(model=...)`、构造 `VisualGenParams(height=480, width=832, num_frames=81, ...)`、`generate(...)` 后 `output.save("output.avi")`。

**操作步骤：**

1. 阅读上面这个 quickstart 脚本，注意它 `from tensorrt_llm import VisualGen, VisualGenParams`，与 `from tensorrt_llm import LLM, SamplingParams` 在导入形态上完全对称。
2. 在有 GPU 的机器上尝试运行（需先安装好 TensorRT-LLM 并能下载 Wan 2.1 1.3B 权重）：

   ```bash
   python examples/visual_gen/quickstart_example.py
   ```

3. 如果没有 GPU 或无权下载模型，做「源码阅读型实践」：对照 u3-l1 里 LLM 的 `generate` 用法，在脚本里把 `VisualGen` / `VisualGenParams` / `VisualGenOutput` 与 `LLM` / `SamplingParams` / `RequestOutput` 三组做一张对照表。

**需要观察的现象：**

- 成功时，`output.save("output.avi")` 会落盘一个 480×832、81 帧的视频文件。
- 注意产物类型：LLM 返回的是 token 序列，VisualGen 返回的是 `VisualGenOutput`，其 `.video` 字段是一个 `torch.Tensor`——这就是「像素张量 vs token」的本质差别。

**预期结果 / 待本地验证：** 命令实际运行需要 GPU 与模型权重，**待本地验证**；源码阅读部分可立即完成。

#### 4.1.5 小练习与答案

**练习 1：** 为什么 VisualGen 不直接复用 LLM 后端的 `PyExecutor` 单步循环？
**参考答案：** 因为扩散模型不是自回归解码。LLM 单步循环每「步」产出 token 并维护 KV cache，依赖 in-flight batching；而 DiT 每「步」只是对整段 latent 做一次去噪前向，没有 KV cache、没有 token 间依赖、没有「逐 token」概念。两者的执行模型（execution model）根本不同，所以执行器、调度器、请求类型都得重做。

**练习 2：** 在多机 `torchrun` 启动下，rank 1 的进程会执行到 `visual_gen.generate(...)` 吗？为什么？
**参考答案：** 不会。`VisualGen.__init__` 探测到外部启动且 `rank != 0` 时，会直接调用 `run_diffusion_worker(...)` 然后 `sys.exit(0)`，所以非零 rank 永不返回用户代码，只当纯 worker。

### 4.2 参数与输出三件套

#### 4.2.1 概念说明

本节回答第二个问题：**VisualGen 怎么配、传什么、拿到什么？** 对应三个公共类，它们的分工与 LLM 一族严格对称，理解了映射就抓住了主干：

| VisualGen 类 | 对应 LLM 类 | 角色 | 粒度 |
|---|---|---|---|
| `VisualGenArgs` | `TorchLlmArgs` | **引擎级**配置：模型、量化、并行、编译、缓存……加载一次，全程生效 | 进程级 |
| `VisualGenParams` | `SamplingParams` | **单次请求**参数：分辨率、帧数、guidance、seed……每次 generate 可不同 | 请求级 |
| `VisualGenOutput` | `RequestOutput` | **返回**结果：像素张量 + 指标，可 `.save()` 落盘 | 请求级 |

一个贯穿全节的重要原则是 **「通用概念上浮、模型特有下沉」**（见 4.3 工程准则 §2）：

- 能跨模型泛化的概念（`quant_config` / `parallel_config` / `attention_config`）才配成为 `VisualGenArgs` 或 `VisualGenParams` 的**顶层字段**；
- 只对某一个模型有意义的旋钮（如 LTX-2 的 `stg_scale`、Wan 的 `guidance_scale_2`），绝不进顶层，而是走 `extra_params`（请求级）或 `pipeline_config`（引擎级），由注册表声明。

#### 4.2.2 核心流程

请求参数的生命周期是「声明 → 合并默认 → 校验 → 注入 seed」：

```text
VisualGenParams(height=480, ...)            # 用户构造，未设字段=None
   │
   ├── 若 generate 未传 params：default_params 取流水线 DEFAULT_GENERATION_PARAMS + extra_param_specs 默认值
   ├── 若传了：model_copy(deep=True) 快照 → validate_visual_gen_params(...)
   │     · 校验 extra_params 未知键
   │     · 校验「用户设了但流水线不支持」的通用字段
   │     · 校验 extra_params 类型 / 范围
   └── seed is None → secrets.randbits(63) 具象化
        │
        ▼
   DiffusionRequest(prompt=..., params=...)  → 入队 → worker pipeline.infer()
```

理解关键约定：**`VisualGenParams` 的字段默认是 `None`，含义是「用模型默认」**，而不是「0」或「关闭」。`None` 会在加载侧被流水线声明的 `DEFAULT_GENERATION_PARAMS` 自动合并补齐。校验函数因此能发现「用户设了某字段、但该流水线根本不支持」的情况，避免静默丢弃。

#### 4.2.3 源码精读

**(1) `VisualGenArgs`——引擎级配置。**

[tensorrt_llm/visual_gen/args.py:523-538](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/visual_gen/args.py#L523-L538) —— `class VisualGenArgs(StrictBaseModel)`，docstring 点明这是「用户面向的扩散模型加载与推理配置」，会被 `PipelineLoader` 转成内部的 `DiffusionModelConfig`。

关键字段集中在一处，每个字段都是「通用概念」：

[tensorrt_llm/visual_gen/args.py:539-604](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/visual_gen/args.py#L539-L604) —— 顶层字段：`model`（HF id 或本地路径）、`revision`、`quant_config`（量化，支持 `QuantConfig` 实例或 ModelOpt dict）、`compilation_config`（torch.compile / CUDA Graph 的 warmup 形状）、`torch_compile_config`、`cuda_graph_config`、`attention_config`（注意力后端 + 量化注意力 + 稀疏注意力）、`parallel_config`（多 GPU 并行）、`cache_config`（TeaCache / Cache-DiT）、`pipeline_config`（逐架构的严格校验旋钮字典）、`enable_layerwise_nvtx_marker`。

`from_yaml` 提供与 CLI 对称的 YAML 加载（呼应「离线 + serve 对称」）：

[tensorrt_llm/visual_gen/args.py:642-661](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/visual_gen/args.py#L642-L661) —— `VisualGenArgs.from_yaml(path, **overrides)` 读 YAML 后用 `**` 构造，`extra="forbid"` 保证未知字段立即报错。

**并行配置 `ParallelConfig`——DiT 形状专用，与 LLM 的 `Mapping` 不同。**

[tensorrt_llm/visual_gen/args.py:210-301](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/visual_gen/args.py#L210-L301) —— `ParallelConfig` 暴露的是 DiT 特有并行轴：`cfg_size`（把正/负 CFG 路径分到两张卡）、`ulysses_size`（按 head 切序列并行）、`async_ulysses`（投影算子与跨卡 all-to-all 重叠）、`ring_size`（Ring Attention）、`attn2d_size`（2D mesh 的 Attention2D）、`tp_size`（张量并行）、`parallel_vae_size`（VAE 解码空间切分）。注意 `n_workers` property：`cfg_size * seq_parallel_size * tp_size`——它就是实际要拉起的 worker 进程数。

[tensorrt_llm/visual_gen/args.py:307-322](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/visual_gen/args.py#L307-L322) —— 跨字段校验 `_validate_async_ulysses`：`async_ulysses=True` 要求 `ulysses_size > 1` 且与 `ring_size > 1` 互斥。这正是 Pydantic `model_validator(mode="after")` 的典型用法（与 u4-l1 一致）。

**注意力配置 `AttentionConfig`——量化/稀疏注意力配方校验。**

[tensorrt_llm/visual_gen/args.py:97-172](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/visual_gen/args.py#L97-L172) —— `AttentionConfig.backend` 在 `VANILLA / TRTLLM / FA4 / CUTEDSL` 四选一；`quant_attention_config` 用一组「配方元组」`(qk_dtype, v_dtype, (q_block, k_block, v_block))` 在运行时校验，TRTLLM 后端只认 5 种 SAGE 配方，CUTEDSL 后端只认 5 种 QK16PV8/mxfp8/nvfp4 配方，配错立刻 `ValueError`。这是「把可接受组合用枚举+校验固化」的范例。

**(2) `VisualGenParams`——请求级参数。**

[tensorrt_llm/visual_gen/params.py:22-62](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/visual_gen/params.py#L22-L62) —— `class VisualGenParams`。核心字段 `height / width / num_inference_steps / guidance_scale / max_sequence_length / seed` 默认全是 `None`，含义「用模型默认」。`seed=None` 时由引擎在协调器侧现抽——这一点要和 4.1.3 的 seed 具象化连起来读。

[tensorrt_llm/visual_gen/params.py:64-84](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/visual_gen/params.py#L64-L84) —— 视频字段 `num_frames / frame_rate`、条件输入 `negative_prompt / image`（用于 I2V/I2I）、`num_images_per_prompt`、以及关键的 **`extra_params`**——模型特有参数的唯一入口。

校验函数把「无效参数」挡在入队之前（让 `ValueError` 直接抛到用户进程）：

[tensorrt_llm/visual_gen/params.py:115-188](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/visual_gen/params.py#L115-L188) —— `validate_visual_gen_params` 做四类检查：未知 `extra_params` 键、用户设了但流水线未声明的通用字段、类型不匹配、数值越界，并把所有违例汇总成多行 `ValueError` 一次抛出。

**如何发现某个模型有哪些 `extra_params`？** 公共 API 提供两个「自省」入口：

[tensorrt_llm/visual_gen/visual_gen.py:275-309](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/visual_gen/visual_gen.py#L275-L309) —— `extra_param_specs` 属性返回 `Dict[str, ExtraParamSchema]`（声明合法键、类型、范围）；`default_params` 属性返回一个已合并好所有默认值（含 `extra_params` 默认）的 `VisualGenParams`，用户拷贝一份改改就能用。

**(3) `VisualGenOutput`——返回结果。**

[tensorrt_llm/visual_gen/output.py:79-102](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/visual_gen/output.py#L79-L102) —— `VisualGenOutput` 是 `@dataclass`，按模态填 `image / video / audio` 张量及对应率（`frame_rate` / `audio_sample_rate`）；失败时所有媒体张量与 `metrics` 置 `None`，`error` 填错误信息。`request_id` 把输出和请求对齐。

[tensorrt_llm/visual_gen/output.py:104-213](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/visual_gen/output.py#L104-L213) —— `save(path, ...)` 按扩展名分派：`.png/.jpg/.webp` 走图像编码、`.mp4/.avi` 走视频编码（视频需 `frame_rate`）、`.safetensors/.pt` 把所有模态+标量元数据打包成张量载荷。这正是 quickstart 里 `output.save("output.avi")` 的落点。

[tensorrt_llm/visual_gen/output.py:45-77](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/visual_gen/output.py#L45-L77) —— `VisualGenMetrics` 给出引擎侧三段计时（`pre_denoise` / `denoise` / `post_denoise`），用 `torch.cuda.Event` 在 GPU 流上异步记录，便于性能分析（呼应 perf 相关讲义）。

#### 4.2.4 代码实践

**实践目标：** 不靠死记，用 API 的「自省」能力发现某模型支持哪些参数；并对比 `VisualGenArgs` 与 `VisualGenParams` 的层级。

**操作步骤：**

1. 阅读官方文档列出的 SageAttention 用法 [docs/source/models/visual-generation.md:128-164](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/models/visual-generation.md#L128-L164)，注意 `attention_config.quant_attention_config` 的 `(q,k,v)` 块尺寸组合。
2. 起草一段「图像生成」脚本（基于 quickstart 改写，把 T2V 换成 T2I 模型，示例代码）：

   ```python
   # 示例代码：基于 quickstart 改写，目标模型为 FLUX.1（T2I）
   from tensorrt_llm import VisualGen, VisualGenArgs, VisualGenParams
   from tensorrt_llm.visual_gen import ParallelConfig

   args = VisualGenArgs(
       model="black-forest-labs/FLUX.1-dev",
       parallel_config=ParallelConfig(ulysses_size=2),
   )
   with VisualGen(args=args) if False else VisualGen(model="black-forest-labs/FLUX.1-dev") as vg:
       # 先自省：这个流水线有哪些模型特有 extra_params？
       print("specs:", vg.extra_param_specs)
       params = vg.default_params   # 拿到合并默认后的 params
       params.height, params.width = 1024, 1024
       params.num_inference_steps = 4   # FLUX.1 少步即可
       out = vg.generate(inputs="a neon cat", params=params)
       out.save("cat.png")
   ```

   > 注：上面是**示例代码**（非仓库原文件），用以演示 `extra_param_specs` / `default_params` 两个自省入口的用法。`with ... as vg` 会保证退出时调用 `shutdown()`（见 visual_gen.py 的 `__enter__/__exit__`）。

3.（源码阅读型）对照 `params.py` 里的 `_GENERATION_CONFIG_FIELDS` 元组，理解为什么 `image` 和 `negative_prompt` 不在该集合里。

**需要观察的现象：**

- `vg.extra_param_specs` 会打印出该模型支持的 `extra_params` 键及其类型/范围（如 LTX-2 会有 `stg_scale`）。
- 如果故意设一个该流水线不支持的通用字段（例如给纯图像模型设 `num_frames=50`），`validate_visual_gen_params` 会在 `generate_async` 入口就抛 `ValueError`，而不是静默丢弃。

**预期结果 / 待本地验证：** 实际运行需要 GPU 与 FLUX 权重，**待本地验证**；自省与校验逻辑可通过读源码立即理解。

#### 4.2.5 小练习与答案

**练习 1：** `VisualGenParams.num_inference_steps` 默认值是多少？为什么不是 `0`？
**参考答案：** 默认是 `None`，含义「用模型默认」。在加载侧由流水线声明的 `DEFAULT_GENERATION_PARAMS` 合并补齐。如果是 `0`，会和「显式设 0 步」歧义；`None` 让校验函数能区分「用户没设」与「用户设了」。

**练习 2：** 某模型想新增一个只对它有意义的旋钮「`my_knob`」，应该加到 `VisualGenArgs`、`VisualGenParams` 顶层字段，还是别处？
**参考答案：** 都不应加到顶层。应通过注册表的 `extra_param_specs` 声明为 `extra_params`（请求级）或 `pipeline_config`（引擎级），由 `validate_visual_gen_params` 校验。只有当它「能跨模型泛化」时，才考虑晋升为顶层字段——这是 4.3 工程准则 §2 的硬性要求。

### 4.3 工程准则与内部实现位置

#### 4.3.1 概念说明

本节回答第三个问题：**改 VisualGen 时，纪律是什么、代码放哪？** 这对一个尚处于 prototype 阶段、且正在快速演进的子系统尤其重要。仓库用一份与代码同目录的 `ENGINEERING_CRITERIA.md` 把规矩写死，并被 `AGENTS.md` 明确要求「修改 `tensorrt_llm/visual_gen/` 或 `tensorrt_llm/_torch/visual_gen/` 之前必须读」。

核心是**公共面 vs 内部面的二分**：

- **公共 API 面**：`tensorrt_llm/visual_gen/`（本讲主角）。这是用户可见的表面，改动需格外谨慎——`AGENTS.md` 明确写「在修改这里的任何东西之前，先和用户确认公共 API 变更是否真的有意为之；不要从手头任务推断出来」。
- **内部实现面**：`tensorrt_llm/_torch/visual_gen/`。所有非用户可见的代码都应落在这里：执行器、流水线、注意力后端、缓存加速、权重加载等。

#### 4.3.2 核心流程

`ENGINEERING_CRITERIA.md` 用五节给出可执行的红线，这里提炼与本讲最相关的四条：

| 准则 | 核心要求 | 落地位置 |
|---|---|---|
| §1 新模型 | 一个 PR 落齐所有要求；把每个支持的 checkpoint 注册进 `hf_ids`；模型特有旋钮走 `pipeline_config` / `extra_params`；至少一个示例进 CI；每个 task 一个 E2E+LPIPS 对照 | `pipeline_registry.py` + `examples/visual_gen/` |
| §2 API | 公共面改动需**团队评审**（不只 PR approval）；顶层字段只放通用概念；能从平台/模型/负载推断的就让引擎自己选；遵循 Pydantic 规范；离线与 serve 对称 | `tensorrt_llm/visual_gen/` |
| §3 特性 | 有损工作（量化/稀疏/近似 kernel）必须在 PR 报 LPIPS/VBench 等精度指标并经 sync 评审；无损工作报与 golden 的 LPIPS | PR 描述 + 链接报告 |
| §5 测试 | 测试随 PR 一起进、按特性而非行数定规模；分 sanity（形状/非黑）与 quality gate（LPIPS/VBench）两档 | 见下表 |

测试落点有明确目录约定：

| 测试类别 | 目标目录 |
|---|---|
| 示例测试 | `tests/integration/defs/examples/visual_gen/` |
| E2E 测试 | `tests/integration/defs/visual_gen/` |
| 性能测试 | `tests/integration/defs/perf/` |
| 公共面单元测试 | `tests/unittest/visual_gen/` |
| 内部面单元测试 | `tests/unittest/_torch/visual_gen/` |

#### 4.3.3 源码精读

先看 `ENGINEERING_CRITERIA.md` 的 API 纪律原文（这是改公共面前的必读条款）：

[tensorrt_llm/_torch/visual_gen/ENGINEERING_CRITERIA.md:34-49](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/visual_gen/ENGINEERING_CRITERIA.md#L34-L49) —— §2 列出 6 条：公共面改动需团队 sync 评审；顶层字段命名要跨模型通用、一句话 docstring 能说清；模型特有旋钮留在 `pipeline_config` / `extra_params`；可推断的值让引擎自选；遵循 Pydantic 规范；离线与 serve 必须对称。

「模型特有 vs 通用」如何在代码里落地？靠的是「注册表 + 自省」这套机制，而不是 if-else 散落各处。注册表本身：

[tensorrt_llm/_torch/visual_gen/pipeline_registry.py:62-75](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/visual_gen/pipeline_registry.py#L62-L75) —— `_PipelineEntry` dataclass 携带 `pipeline_cls`、`hf_ids`（ canonical HF id 列表）、`defaults`（`pipeline_config` 默认）、`doc`；`PIPELINE_REGISTRY` 以 Diffusers `_class_name` 为键。注释强调「约 3-5 条——每个流水线家族一条，而非每个 checkpoint 一条」，微调 checkpoint 靠继承的 `_class_name` 自动派发。

[tensorrt_llm/_torch/visual_gen/pipeline_registry.py:78-119](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/visual_gen/pipeline_registry.py#L78-L119) —— `@register_pipeline(name, hf_ids=..., defaults=..., doc=...)` 装饰器把 pipeline 类注册进全局表，与 LLM 后端的 `@register_auto_model`（见 u5-l2）「import 即注册」思路一致。

公共门面如何只读这张表而不暴露它：

[tensorrt_llm/visual_gen/visual_gen.py:179-223](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/visual_gen.py#L179-L223) —— `supported_models()` 与 `pipeline_config(model)` 两个 classmethod 是注册表对用户的唯一投影：前者返回所有 `hf_ids` 的字母序并集，后者按「HF id → `_class_name` → 本地路径」三级解析返回默认 `pipeline_config` 的拷贝。注册表本身（`PIPELINE_REGISTRY`）刻意设为私有，用户不直接碰。

模型特有参数的「schema 声明」定义在 `BasePipeline` 旁边：

[tensorrt_llm/_torch/visual_gen/pipeline.py:23-35](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/visual_gen/pipeline.py#L23-L35) —— `ExtraParamSchema`（注意它被公共 `__init__.py` 反向 re-export）声明 `type / default / description / range`，正是 `validate_visual_gen_params` 做 duck-type 校验时所依据的形态。

[tensorrt_llm/_torch/visual_gen/pipeline.py:104-129](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/visual_gen/pipeline.py#L104-L129) —— `class BasePipeline(nn.Module)`，注释点明「统一缓存加速（TeaCache、Cache-DiT）」。它提供去噪主循环、CFG 处理、缓存加速，子类只实现模型特有逻辑。

[tensorrt_llm/_torch/visual_gen/pipeline.py:356-363](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/visual_gen/pipeline.py#L356-L363) —— `infer` / `forward` 在基类里是 `raise NotImplementedError`，强制子类覆盖；去噪主循环由 `denoise(...)` 承担：

[tensorrt_llm/_torch/visual_gen/pipeline.py:997-1013](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/visual_gen/pipeline.py#L997-L1013) —— `denoise(latents, scheduler, prompt_embeds, guidance_scale, forward_fn, ...)` 是扩散去噪主循环的统一签名，支持可选的 CFG 并行、双流（如视频+音频）、guidance interval、post_step 钩子。子类的 `infer` 调用它完成「文本编码 → 构造 latent → denoise → VAE 解码」。

最后是协调器侧的客户端与执行器（多 GPU 的真正实现，留在 internal 包）：

[tensorrt_llm/_torch/visual_gen/executor.py:552-578](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/visual_gen/executor.py#L552-L578) —— `DiffusionRemoteClient` docstring 完整描述了两种启动模式：单机默认用 `mp.Process` 本地拉起 worker；多机用 `torchrun`/`srun`，rank 0 当协调器+worker、rank>0 在 `VisualGen.__init__` 阶段就转 worker 退出。它明确「不是公共 API 的一部分」。

[tensorrt_llm/_torch/visual_gen/executor.py:459](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/visual_gen/executor.py#L459) —— `run_diffusion_worker(...)` 是非零 rank 进入的 worker 主函数（被 4.1.3 的 `__init__` 调用）。

#### 4.3.4 代码实践

**实践目标：** 用「公开自省」回答两个工程问题，体会「specifics 在代码（注册表）、不在文档」这条准则。

**操作步骤：**

1. 在能下载权重的环境运行（示例代码）：

   ```python
   # 示例代码：用公共自省 API 列出全部支持模型
   from tensorrt_llm import VisualGen
   for mid in VisualGen.supported_models():
       print(mid, "→", sorted(VisualGen.pipeline_config(mid).keys()))
   ```

2. 对照文档的支持模型表 [docs/source/models/visual-generation.md:25-41](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/models/visual-generation.md#L25-L41)，验证 `supported_models()` 的输出是否与文档一致；并思考：准则 §4 说「把 specifics 写进文档是 rot path」，那么文档这张表和 `supported_models()` 谁是真相源？（答案：注册表是真相源，文档只是导览。）
3.（源码阅读型）打开 `tensorrt_llm/_torch/visual_gen/models/` 目录，挑一个 pipeline（如 Wan）的注册处，确认它用了 `@register_pipeline(..., hf_ids=[...])`，且其 `extra_param_specs` 声明的键，正好是你在步骤 1 看到的模型特有旋钮。

**需要观察的现象：**

- `supported_models()` 返回的是注册表里所有 `hf_ids` 的并集，与文档表大致吻合（文档会略滞后于代码）。
- 每个 `pipeline_config(mid)` 返回的键集因模型而异——这就是「模型特有旋钮」的可观测证据。

**预期结果 / 待本地验证：** 运行需可下载权重，**待本地验证**；步骤 3 的源码核对可立即完成。

#### 4.3.5 小练习与答案

**练习 1：** 你想给 FLUX 加一个新旋钮 `my_flux_knob`，只对 FLUX 有意义。直接给 `VisualGenArgs` 加一个顶层字段对吗？正确的做法是什么？
**参考答案：** 不对。顶层字段必须跨模型通用（准则 §2）。正确做法是在 FLUX 的 pipeline 里通过 `extra_param_specs` 声明它，用户经 `VisualGenParams.extra_params={"my_flux_knob": ...}` 传入，由 `validate_visual_gen_params` 校验类型/范围。等它被证明能泛化到多个模型后，再考虑晋升为顶层字段。

**练习 2：** 为什么 `AGENTS.md` 要求修改 `tensorrt_llm/visual_gen/` 前要先确认「公共 API 变更真的有意为之」？
**参考答案：** 因为 `tensorrt_llm/visual_gen/` 是公共面，其改动会直接改变用户可见的 `VisualGen` / `VisualGenArgs` / `VisualGenParams` 行为。准则 §2 进一步要求这类改动需团队 sync 评审，PR approval 不足以放行。这种「先确认意图、再动手」的纪律是为了避免从手头局部任务误推出一次破坏性的公共面改动。

## 5. 综合实践

把本讲三节串起来，完成一个「**画一张 VisualGen 与 LLM 的对照图 + 起草一份可读的生成脚本 + 给出一份改公共面的自查清单**」的综合任务。

**任务 A：双栏对照表。** 在一份笔记里，按「引擎级配置 / 请求级参数 / 返回类型 / 高层入口 / 分布式单位 / 产物形态 / 注册机制 / 公共面位置」八个维度，把 `LLM` 一族与 `VisualGen` 一族逐项对照。要求每项都给出对应的类名或文件（例如「请求级参数：`SamplingParams` ↔ `VisualGenParams`」、「分布式单位：进程组 rank ↔ worker 进程」）。

**任务 B：起草一段「带自省 + 量化 + 并行」的脚本骨架（示例代码）。** 综合用到本讲的 `VisualGenArgs.quant_config`、`parallel_config`、`VisualGenParams.extra_params`、`VisualGen.extra_param_specs`，写出骨架并**用注释**说明每一处为何这样写、产物为何是像素张量而非 token。不要求能跑（无 GPU 时），要求逻辑自洽、字段名与源码一致。

**任务 C：改公共面的自查清单。** 假设你要给 `VisualGenParams` 新增一个字段，按 `ENGINEERING_CRITERIA.md` §2 写一份自查清单，至少覆盖：(1) 它是否真的跨模型通用？(2) docstring 是否一句话说清？(3) 离线与 serve 是否对称？(4) 是否需要团队 sync？(5) 测试落在 `tests/unittest/visual_gen/` 还是别处？

**验收标准：** 任务 A 的对照表能让一个没读过 VisualGen 的同学快速建立心智；任务 B 的脚本骨架字段名与 4.2.3 引用的源码完全对应；任务 C 的清单能把「不该进顶层」的旋钮挡住。

## 6. 本讲小结

- **VisualGen 是与 LLM 后端并行的独立子系统**，不是 `LLM` 的配置项；它复用底层原语（`Mapping`/`QuantConfig`/`Linear`/`RMSNorm`/`TrtllmAttention`），但执行器、调度器、请求/参数/输出、流水线架构全部独立，因为它服务的是扩散去噪而非自回归解码。
- **公共 API 三件套与 LLM 严格对称**：`VisualGenArgs`（引擎级，≈`TorchLlmArgs`）、`VisualGenParams`（请求级，≈`SamplingParams`，字段默认 `None` 表「用模型默认」）、`VisualGenOutput`（返回，≈`RequestOutput`，带 `.save()` 落盘）；产物是像素张量而非 token。
- **多 GPU 用进程而非线程**：每个 GPU 一个 worker 进程，ZeroMQ IPC 通信；单机默认 `mp.Process` 本地拉起，多机 `torchrun`/`srun` 时 rank 0 当协调器、rank>0 在 `__init__` 阶段转 worker 后 `sys.exit(0)`。
- **调用链是委托链**：`VisualGen`（公共门面）→ `DiffusionRemoteClient`（协调器）→ `DiffusionExecutor`（worker）→ `BasePipeline.infer` → `denoise` 主循环 → VAE 解码；seed 在公共边界一次性具象化以保证多 rank 确定性。
- **「通用上浮、特有下沉」是硬纪律**：跨模型通用概念才配成顶层字段，模型特有旋钮走 `pipeline_config`（引擎级）或 `extra_params`（请求级），由注册表 + `ExtraParamSchema` 声明、由 `validate_visual_gen_params` 校验。
- **公共面 vs 内部面二分**：`tensorrt_llm/visual_gen/` 是公共面（改动需团队 sync，`AGENTS.md` 要求先确认意图），`tensorrt_llm/_torch/visual_gen/` 是内部实现；所有规矩写在同目录的 `ENGINEERING_CRITERIA.md`。

## 7. 下一步学习建议

- **深入内部实现**：本讲只到「公共门面」。若想理解去噪主循环与缓存加速，读 `tensorrt_llm/_torch/visual_gen/pipeline.py` 的 `BasePipeline.denoise` 与 `cache/` 下的 `TeaCacheAccelerator` / `CacheDiTAccelerator`，官方文档 [docs/source/models/visual-generation.md](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/models/visual-generation.md) 有 TeaCache / Cache-DiT 各参数详解。
- **VisualGen 的注意力后端与量化**：可与 u6-l2（注意力后端家族）、u10-l2（量化机制）对照阅读——VisualGen 的 `AttentionConfig.backend ∈ {VANILLA, TRTLLM, FA4, CUTEDSL}` 与 LLM 后端的 `attn_backend` 是两套独立但同源的设计；`QuantAttentionConfig` 的 SAGE / QK16PV8 配方是量化注意力在 DiT 上的特化。
- **VisualGen 的并行**：可与 u9-l1（Mapping 与并行策略）、u9-l2（分布式通信原语）对照，理解 `ParallelConfig` 的 CFG / Ulysses / Ring / Attention2D / TP 与 LLM 后端 TP/PP/CP 的异同。
- **trtllm-serve 中的 VisualGen**：当 checkpoint 目录含 `model_index.json` 时，`trtllm-serve` 会自动切到 VisualGen 模式并暴露 `/v1/images/generations`、`/v1/videos` 等 OpenAI 兼容端点，可与 u11-l1（trtllm-serve 与 OpenAI 兼容服务）对照。
- **添加新扩散模型**：若要接入自定义 DiT，按文档「Implementing a New Diffusion Model」四步（建 transformer 模块 → 建 pipeline 子类 → `@register_pipeline` → 更新 `AutoPipeline` 检测），其思路与 u5-l3（添加新 LLM 模型）高度同构，可互相印证。
