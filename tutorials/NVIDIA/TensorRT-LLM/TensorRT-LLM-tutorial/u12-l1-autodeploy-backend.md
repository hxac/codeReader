# AutoDeploy 后端（图变换与编译）

## 1. 本讲目标

前面几讲里，PyTorch 后端的「模型」都是人工写好的 `DecoderModelForCausalLM`（见 [u5-l1](u5-l1-model-architecture-pattern.md)），KV cache、融合、量化都要开发者手工接线。本讲换一个视角：**能不能不改模型源码，让它自动获得这些推理优化？** AutoDeploy 就是为此而生。

读完本讲，你应当能：

1. 说清 AutoDeploy 是什么、它与默认 PyTorch 后端在「入口」与「模型来源」上的本质差异。
2. 复述 AutoDeploy 的三段式工作流：`torch.export` 抽图 → 图变换（transform pipeline）优化 → 编译后端（compile backend）落地为 CUDA Graph。
3. 看懂 `BaseTransform` / `TransformRegistry` / `Stages` 这套「可注册、按阶段排序、统一调用」的图变换框架，并能读懂一个真实变换 `fuse_silu_mul` 的注册与执行。
4. 区分「整图 CUDA Graph」与「分片（piecewise）CUDA Graph」，理解分片为什么要在动态算子边界切图、如何按 `num_tokens` 分桶捕获。
5. 独立跟踪一条「装饰器注册 → YAML 配置 → InferenceOptimizer 调度执行」的完整链路。

## 2. 前置知识

本讲依赖 [u3-l3（ModelEngine 与模型前向）](u3-l3-model-engine-forward.md) 与 [u5-l1（模型架构范式）](u5-l1-model-architecture-pattern.md)。在进入正文前，先确认几个术语：

- **FX Graph / GraphModule**：PyTorch 把一个 `nn.Module` 的前向逻辑「追踪」下来，得到一张有向无环图（节点是算子调用，边是张量流动），存进 `torch.fx.GraphModule`。AutoDeploy 的所有优化都改的是这张图，而不是改原始模型代码。
- **torch.export**：PyTorch 官方推荐的、比旧版 `torch.jit.trace` 更稳的导出 API，能把模型（含自定义算子）导成一份标准 ATen IR 图。AutoDeploy 用它做「抽图」的第一步。
- **CUDA Graph**：把一串 CUDA kernel 调用录制成一张图，之后一次性回放（replay），省掉每次 kernel launch 的 CPU 开销。生成（decode）阶段形状固定，最适合用整图 CUDA Graph；但 prefill 阶段序列长度千变万化，整图录不下，于是有了「分片」方案。
- **ModelEngine / PyExecutor**：回顾 u3-l3，`ModelEngine` 是引擎抽象基类（只要求实现 `forward` 与 `get_max_num_sequences`），`PyExecutor` 是单步循环发动机。AutoDeploy 不重写发动机，只是换了一个 `ModelEngine` 的实现（`ADEngine`），仍复用整套 `PyExecutor` 调度、采样、in-flight batching。

一句话定位：**AutoDeploy 不是新引擎，而是「自动把任意 PyTorch 模型变成一个高效的 `ModelEngine`」的编译器，产出的引擎照样跑在 `PyExecutor` 上。**

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| `tensorrt_llm/_torch/auto_deploy/llm.py` | AutoDeploy 的 `LLM` 类，继承 `_TorchLLM`，是面向用户的入口与 shim |
| `tensorrt_llm/_torch/auto_deploy/shim/ad_executor.py` | `ADEngine`（`ModelEngine` 的 AutoDeploy 实现）+ `create_autodeploy_executor`（分布式入口） |
| `tensorrt_llm/_torch/auto_deploy/transform/interface.py` | 图变换的根基类 `BaseTransform`、注册表 `TransformRegistry`、阶段枚举 `Stages` |
| `tensorrt_llm/_torch/auto_deploy/transform/optimizer.py` | `InferenceOptimizer`：按阶段顺序串起所有变换的流水线 |
| `tensorrt_llm/_torch/auto_deploy/transform/library/export_to_gm.py` | `EXPORT` 阶段：用 `torch.export` 把模型抽成 `GraphModule` |
| `tensorrt_llm/_torch/auto_deploy/transform/library/fuse_silu_mul.py` | `POST_LOAD_FUSION` 阶段：融合 SiLU+Mul 的样例变换 |
| `tensorrt_llm/_torch/auto_deploy/transform/library/compile_model.py` | `COMPILE` 阶段：调起编译后端 |
| `tensorrt_llm/_torch/auto_deploy/compile/compiler.py` | `CompileBackendRegistry` + `CompilerBackend` 抽象基类 |
| `tensorrt_llm/_torch/auto_deploy/compile/backends/torch_cudagraph.py` | 默认后端 `torch-cudagraph`：整图 + 分片 CUDA Graph |
| `tensorrt_llm/_torch/auto_deploy/compile/backends/torch_opt.py` | `torch-opt` 后端：在 `torch-cudagraph` 之前叠加 `torch.compile` |
| `tensorrt_llm/_torch/auto_deploy/compile/piecewise_runner.py` | `ADPiecewiseRunner`：单个静态子段的 warmup→capture→replay 管理 |
| `tensorrt_llm/_torch/auto_deploy/config/default.yaml` | 默认变换流水线配置（哪些变换、什么阶段、是否启用） |
| `docs/source/features/auto_deploy/auto-deploy.md` | 官方功能文档 |

一句话定位：**`llm.py` / `ad_executor.py` 负责「把 AD 接进运行时」，`transform/` 负责「把模型变成优化图」，`compile/` 负责「把优化图编译成可高效执行的模块」。**

## 4. 核心概念与源码讲解

### 4.1 AutoDeploy shim：把编译后的模型塞进 PyExecutor

#### 4.1.1 概念说明

「shim」中文是「垫片」——夹在两块接口之间做适配。AutoDeploy 的 shim 要解决的问题是：

- 上层（`LLM` API、`trtllm-serve`）只认 `LLM` 这一套接口（`generate` / `generate_async` / tokenizer 托管）。
- 下层（`PyExecutor`）只认 `ModelEngine`（要 `forward`、要能调度）。
- AutoDeploy 真正想做的，是用 `torch.export + 图变换 + 编译` 造一个优化过的 `nn.Module`。

shim 就是把这三层粘起来：它继承默认后端的 `LLM`（`_TorchLLM`），在「构建模型」这一步偷偷换掉实现——不走手工模型加载，而是先 prefetch checkpoint，再让 `ADEngine` 跑一遍编译流水线，产出一个优化模型，最后把它装进 `PyExecutor`。

这样设计的妙处是「最小惊讶」：对用户而言，`LLM(model=..., backend="_autodeploy")` 的用法与默认后端完全一致，只是底层模型从「人工写」变成「自动编」。

#### 4.1.2 核心流程

```text
用户: LLM(model="xxx", backend="_autodeploy")
  │
  ├─ llm.py 的 LLM.__init__ 强制 backend="_autodeploy"，调 super().__init__
  │    → llmapi/llm.py 派发：backend=="_autodeploy" → 选用 AutoDeployLlmArgs
  │
  ├─ _TorchLLM._build_model() → auto_deploy/llm.py._build_model()
  │    ├─ _prefetch_model()：先把权重从 HF 下载到本地
  │    └─ super()._build_model() → 最终构造 ADEngine
  │
  └─ ADEngine.build_from_config()
       ├─ create_factory()：建模型骨架（HfModelFactory / 自定义）
       ├─ InferenceOptimizer(factory, config=ad_config.transforms)：组装流水线
       └─ ADEngine(get_inference_model=InferenceOptimizer实例, ...)
            └─ __init__ 里 self.model = get_inference_model(cache_seq_interface)
                 └─ 真正跑完 export → transforms → compile，产出优化模型
```

随后 `PyExecutor` 的单步循环（[u3-l2](u3-l2-pyexecutor-step-loop.md)）每次前向就调 `ADEngine.forward`，后者把 TRT-LLM 的 `ScheduledRequests` 翻译成 AutoDeploy 原生的张量输入，喂给优化模型。

#### 4.1.3 源码精读

AutoDeploy 的 `LLM` 只做两件关键小事：强制 backend 标记，并接管 `_build_model`：

[tensorrt_llm/_torch/auto_deploy/llm.py:136-150](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/llm.py#L136-L150) —— `class LLM(_TorchLLM)`：构造时把 `backend` 钉成 `"_autodeploy"`，其余全部复用父类。这就是「垫片」最薄的地方。

[tensorrt_llm/_torch/auto_deploy/llm.py:187-203](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/llm.py#L187-L203) —— `_build_model`：先 `_prefetch_model()`，再调 `super()._build_model()`，并注释「我们绕过默认后端的 `CachedModelLoader`」。注意最后一行 `self.input_processor = self._create_input_processor()`——AutoDeploy 自己接管多模态/聊天 token 化（见 `ADInputProcessor`），不沿用父类的处理器。

派发逻辑在 `llmapi/llm.py`：

[tensorrt_llm/llmapi/llm.py:185-189](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/llm.py#L185-L189) —— 见到 `backend == '_autodeploy'` 就改用 `AutoDeployLlmArgs`（与默认的 `TorchLlmArgs` 并列）。这就是 u4-l1 里提到的「`LLM.__init__` 按 backend 派发」的具体落点。

真正的编译发生在 `ADEngine`：

[tensorrt_llm/_torch/auto_deploy/shim/ad_executor.py:381-433](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/shim/ad_executor.py#L381-L433) —— `ADEngine.build_from_config`：先用 `ad_config.create_factory()` 建模型骨架，再用 `InferenceOptimizer(factory=factory, config=ad_config.transforms, dist_config=dist_config)` 组装流水线（L420），把这个**可调用对象**当作 `get_inference_model` 传进 `ADEngine`。

[tensorrt_llm/_torch/auto_deploy/shim/ad_executor.py:521-524](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/shim/ad_executor.py#L521-L524) —— `__init__` 里 `self.model = get_inference_model(self.cache_seq_interface)`：这一行才真正触发整条 export→transform→compile 流水线跑完，`self.model` 就是最终的优化模型。`InferenceOptimizer` 实现了 `__call__`，所以它本身就是「构造模型的函数」。

前向入口（单步循环每次调它）：

[tensorrt_llm/_torch/auto_deploy/shim/ad_executor.py:1017-1043](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/shim/ad_executor.py#L1017-L1043) —— `_run_forward`：`model_output = self.model(**csi.named_args, cache_seq_interface=csi)`。注意优化模型接受的是 `CacheSeqInterface` 整理好的命名张量，而非 TRT-LLM 的 `ScheduledRequests`——翻译工作在 `_prepare_inputs`（L728）里完成。

> 入口差异小结（实践任务要用）：默认 PyTorch 后端 `LLM(model=..., backend="pytorch")` → `TorchLlmArgs` → `ModelLoader` 装载**手写**的 `DecoderModelForCausalLM`；AutoDeploy `LLM(model=..., backend="_autodeploy")` → `AutoDeployLlmArgs` → `ADEngine` 用 `torch.export` 把**任意** PyTorch 模型抽图再自动优化。两者最终都产出一个喂给 `PyExecutor` 的 `ModelEngine`，因此 in-flight batching、调度、采样完全共享。

#### 4.1.4 代码实践

**目标**：确认 AutoDeploy 与默认后端在「派发」上的分叉点，并理解 `ADEngine` 如何复用 `PyExecutor`。

**步骤**：

1. 在 `tensorrt_llm/llmapi/llm.py:185` 处设断点或加日志，分别用 `backend="pytorch"` 与 `backend="_autodeploy"` 构造 `LLM`，观察 `llm_args_cls` 分别取到哪个类。
2. 打开 `tensorrt_llm/_torch/auto_deploy/shim/ad_executor.py`，定位 `create_autodeploy_executor`（L1156）——这是多卡（MPI worker）场景下构造执行器的统一入口。看它在 L1326 如何把 `engine`（即 `ADEngine`）当作 `model_engine` 传给 `PyExecutor(...)`。
3. 对照 u3-l2 的单步循环，确认 `PyExecutor` 调用的 `model_engine.forward` 在 AutoDeploy 下落到 `ADEngine.forward`（L1049），而 PyTorch 后端下落到 `PyTorchModelEngine.forward`。**同一个发动机，两个不同的引擎实现。**

**需要观察的现象**：

- 两种 backend 走不同的 `llm_args_cls`，但都进入 `PyExecutor`。
- `create_autodeploy_executor` 里 `resource_manager`、`scheduler`、`sampler` 的构造方式与 PyTorch 后端几乎一致——印证「AutoDeploy 只换引擎、不换发动机」。

**预期结果**：能画出「`LLM` → `build_from_config` → `InferenceOptimizer` → `ADEngine` → `PyExecutor`」的调用链，并指出与默认后端唯一本质不同的环节是「模型的来源与构建方式」。

> 说明：本机若无可用的 GPU 与模型权重，无法真正跑通 `LLM(...)` 构造（`torch.export` 需要在 GPU 上 trace）。以上为源码阅读型实践；若需运行，可参照官方 `examples/auto_deploy/build_and_run_ad.py` 用一个小模型（如 `TinyLlama/TinyLlama-1.1B-Chat-v1.0`）。

#### 4.1.5 小练习与答案

**练习 1**：AutoDeploy 的 `LLM` 为什么要继承 `_TorchLLM` 而不是直接继承 `BaseLLM`？

**参考答案**：因为 AutoDeploy 要复用 `_TorchLLM` 里所有与 PyTorch 后端共享的通用流程（tokenizer 托管、generate 的「提交 + 等待」语义、future 处理、`_build_model` 的整体骨架）。它只需覆盖「模型从哪来」这一点，继承 `_TorchLLM` 让它免费拿到这些能力，是「复用多、覆盖少」原则的体现。

**练习 2**：`ADEngine` 是 `ModelEngine` 的子类吗？它的 `forward` 接收什么、返回什么？

**参考答案**：是。`class ADEngine(ModelEngine)`。它的 `forward` 接收 TRT-LLM 的 `ScheduledRequests` + `ResourceManager`，内部 `_prepare_inputs` 把它们翻译成命名张量喂给优化模型，`_run_forward` 返回含 `logits`（已 squeeze 并转 float32）的字典。

---

### 4.2 图变换：把优化做成可注册、可排序的 pass

#### 4.2.1 概念说明

拿到 FX 图之后，AutoDeploy 要对它做一连串「改写」（pass）：清理冗余、自动分片（sharding）、插入 KV cache、融合算子、改 RMSNorm……每个 pass 都是「输入一张图、输出一张图」。如果把这些 pass 散落在各处硬编码调用，会难以维护、难以增删。AutoDeploy 的解法是经典的**注册表 + 阶段枚举**模式：

- 每个 pass 是一个 `BaseTransform` 子类，用 `@TransformRegistry.register("名字")` 装饰器自注册。
- 每个 pass 声明自己属于哪个**阶段**（`Stages`），阶段是有序的。
- 一份 YAML 配置（`default.yaml`）列出本次要跑哪些 pass、各自参数。
- `InferenceOptimizer` 按「阶段顺序」把 pass 排好，依次调用 `__call__`。

这套设计的好处：新增一个优化 = 写一个类 + 加一个装饰器 + 在 YAML 加一行，**完全不改流水线主框架**。这与 PyTorch 后端「每个优化都写死在 modeling 文件里」形成鲜明对比。

#### 4.2.2 核心流程

```text
default.yaml (transforms 配置)
  │  ad_config.transforms
  ▼
InferenceOptimizer.__init__
  └─ _clean_config: 把配置按键的 Stages 值排序，得到 strict_config
  │
  └─ __call__(cm, mod):
       for (t_name, t_config) in strict_config:      # 按阶段顺序
           transform = TransformRegistry.get(t_name)(t_config)
           mod = transform(mod, cm, factory, shared_config, idx)
                  │
                  └─ BaseTransform.__call__ (final, 统一外壳):
                       ├─ 取历史 TransformInfo（是否 clean、shape 是否有效）
                       ├─ pre-cleanup: canonicalize_graph + shape_prop（按需）
                       ├─ _apply_per_gm_or_whole_model → 子类的 _apply
                       ├─ post-cleanup
                       ├─ 记录 mem/时间/匹配数 → TransformInfo，写回图 meta
                       └─ 可视化 + graph_writer.dump_graph（调试用）
```

阶段顺序（关键，来自 `Stages` 枚举的定义次序）：

```
FACTORY → EXPORT → POST_EXPORT → PATTERN_MATCHER → SHARDING → WEIGHT_LOAD
       → POST_LOAD_FUSION → CACHE_INIT → VISUALIZE → COMPILE
```

直觉上是一条「从无到有再到优」的流水线：先建骨架（FACTORY），抽图（EXPORT），清洗（POST_EXPORT），识别可优化模式（PATTERN_MATCHER），切分到多卡（SHARDING），装权重（WEIGHT_LOAD），融合算子（POST_LOAD_FUSION），插 KV cache（CACHE_INIT），最后编译（COMPILE）。

#### 4.2.3 源码精读

**阶段枚举（有序）**：

[tensorrt_llm/_torch/auto_deploy/transform/interface.py:108-131](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/transform/interface.py#L108-L131) —— `class Stages(Enum)` 用 `@total_ordering` + `__lt__` 让枚举值「按定义顺序」可比较。这是后续排序的依据——哪个 pass 先跑，不是写死的，而是由它声明的 `stage` 在这个枚举里的位置决定。

**注册表**：

[tensorrt_llm/_torch/auto_deploy/transform/interface.py:822-850](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/transform/interface.py#L822-L850) —— `class TransformRegistry`：`register(name)` 返回装饰器，把类存进 `_registry[name]` 并把名字记到类的 `_transform_key` 属性上。`get(name)` 反查。

**统一外壳 `BaseTransform.__call__`**：

[tensorrt_llm/_torch/auto_deploy/transform/interface.py:368-511](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/transform/interface.py#L368-L511) —— 这是所有 pass 的「主入口」，被 `@final` 锁定不可覆盖。它做了大量统一工作：取上一 pass 的 `TransformInfo`、pre/post 清理、按 `skip_on_error` 包 try/except、统计耗时与显存、dump 图（`graph_writer.dump_graph`，受 `AD_DUMP_GRAPHS_DIR` 控制）。子类只需实现 `_apply`（改图并返回 `(gm, TransformInfo)`）。这种「模板方法 + 注册表」是可扩展架构的范本。

**流水线编排**：

[tensorrt_llm/_torch/auto_deploy/transform/optimizer.py:62-75](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/transform/optimizer.py#L62-L75) —— `_clean_config`：`sorted(nested_kwargs.keys(), key=lambda k: Stages(...))`。一句排序就保证了 pass 的执行顺序与 YAML 里书写的次序无关，只取决于阶段。

[tensorrt_llm/_torch/auto_deploy/transform/optimizer.py:110-115](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/transform/optimizer.py#L110-L115) —— 主循环：`transform = self._create_transform(...)` 然后 `mod = transform(mod, cm, ...)`。

**抽图（EXPORT 阶段样例）**：

[tensorrt_llm/_torch/auto_deploy/transform/library/export_to_gm.py:199-208](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/transform/library/export_to_gm.py#L199-L208) —— `torch_export_to_gm(sub_mod, args=(), kwargs=captured_kwargs, dynamic_shapes=..., strict=self.config.strict, ...)`：把子模块导成 `GraphModule`。注意默认 `strict=False`（用非严格导出，避免依赖字节码表示的脆弱性，详见字段注释 L42-47）。

**真实融合 pass：fuse_silu_mul**：

MLP 里常见的 SwiGLU 结构是 `hidden = silu(gate) * up`。当 `gate`、`up` 两个投影先被 GEMM 融合合并成一次 `gemm(x, gate_up_weight)`、再用 `narrow` 切回两半后，图里会留下：

```text
fused = gemm(x, gate_up_weight)
gate  = narrow(fused, -1, 0,         size)
up    = narrow(fused, -1, size,      size)
hidden = silu(gate) * up
```

这一坨可以用一个融合算子 `silu_and_mul(fused)` 替代，省掉两次 `narrow`、一次 `silu`、一次逐元素乘，改成一次 kernel。数学上：

\[
\mathrm{silu}(g)\cdot u = \frac{g}{1+e^{-g}}\cdot u
\]

融合算子把 \(g\) 与 \(u\) 在同一 kernel 里就地切片、算 silu、再乘 \(u\)。

[tensorrt_llm/_torch/auto_deploy/transform/library/fuse_silu_mul.py:186-225](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/transform/library/fuse_silu_mul.py#L186-L225) —— 注册与目标算子选择：`@TransformRegistry.register("fuse_silu_mul")` 自注册；按 `backend` 配置（默认 `flashinfer`，可选 `trtllm`）选 `target_op`（`flashinfer_silu_and_mul` 或 `trtllm_silu_and_mul`）。注意文件顶部的 `from ...custom_ops.linear.silu_mul import flashinfer_silu_and_mul, trtllm_silu_and_mul  # noqa: F401`（L51）——import 即注册自定义算子，和模型的「import 即注册」是同一套路（见 u5-l2）。

[tensorrt_llm/_torch/auto_deploy/transform/library/fuse_silu_mul.py:230-254](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/transform/library/fuse_silu_mul.py#L230-L254) —— 核心改写：遍历图节点找 `aten.mul.Tensor`，`_try_fuse_mul` 判定它是否匹配「silu(narrow) * narrow」模式；命中后 `graph.call_function(target_op, args=(fused_parent,))` 插入融合节点，`node.replace_all_uses_with(fused_node)` 把原 mul 的消费者改指向新节点，最后 `eliminate_dead_code()` 清掉死掉的 narrow/silu。

[tensorrt_llm/_torch/auto_deploy/transform/library/fuse_silu_mul.py:355-406](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/transform/library/fuse_silu_mul.py#L355-L406) —— `_match_silu_narrow_mul`：模式匹配的「守卫」。它要求 gate 的 narrow 在 offset 0、up 的 narrow 在 offset=size、两者 size 相等，且**两半合起来恰好覆盖父张量最后一维**（`parent_last_dim == 2 * gate_size`）。这个约束很重要：融合算子总是从「中点」切分输入，若两半没铺满最后一维，融合结果就会错位。

**YAML 配置（注册的另一半）**：

[tensorrt_llm/_torch/auto_deploy/config/default.yaml:244-247](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/config/default.yaml#L244-L247) —— 默认启用 `fuse_silu_mul`，阶段 `post_load_fusion`，backend `flashinfer`。`export_to_gm` 在 L27、`compile_model` 在 L340。这份 YAML 由 `LlmArgs` 在 [tensorrt_llm/_torch/auto_deploy/llm_args.py:571](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/llm_args.py#L571) 处加载（`"graph": str(config_path / "default.yaml")`）。

#### 4.2.4 代码实践

**目标**：完整跟踪 `fuse_silu_mul` 从注册到执行的链路，验证「装饰器注册 + YAML 启用 + InferenceOptimizer 调度」三段闭环。

**步骤**：

1. **注册环节**：打开 `transform/library/fuse_silu_mul.py`，定位 `@TransformRegistry.register("fuse_silu_mul")`（L186）。这是 import 时即生效的副作用。追溯 `transform/__init__.py` 与 `transform/library/__init__.py`，确认 `fuse_silu_mul` 被 import，从而被注册（若未被 import，注册就不会发生）。
2. **配置环节**：打开 `config/default.yaml`，找到 `fuse_silu_mul`（L244），记下它的 `stage`（`post_load_fusion`）与 `backend`（`flashinfer`）。
3. **排序环节**：在 `optimizer.py:69` 的 `sorted(...)` 处，按 `Stages` 枚举确认 `post_load_fusion` 排在 `weight_load` 之后、`cache_init` 之前——这正是「装完权重再融合」的合理时机。
4. **执行环节**：在 `interface.py:453` 的 `mod, info_apply = self._apply_per_gm_or_whole_model(...)` 处，确认它会调到 `fuse_silu_mul._apply`（L211），后者扫描 `aten.mul.Tensor` 节点并改写。
5. **可选 dump**：设环境变量 `AD_DUMP_GRAPHS_DIR=/tmp/ad_graphs`，跑一次 AutoDeploy 构造，在 `BaseTransform.__call__` 的 `graph_writer.dump_graph`（L508）处会写出每个 pass 之后的图。对比 `fuse_silu_mul` 前后两张图，应能看到 `aten.mul` + `aten.silu` + `narrow` 被替换成单个 `auto_deploy.flashinfer_silu_and_mul`。

**需要观察的现象**：

- 注释掉 `transform/library/__init__.py` 里对 `fuse_silu_mul` 的 import 后，`TransformRegistry.has("fuse_silu_mul")` 应返回 False——印证「import 即注册」。
- `TransformInfo.num_matches` 反映该模型里命中了几处 SiLU+Mul 模式（典型 MLP 每层 1 处）。

**预期结果**：能复述「装饰器把类塞进 `_registry` → YAML 声明启用 → `InferenceOptimizer` 按阶段排序后调 `__call__` → `BaseTransform` 外壳做清理与统计 → 子类 `_apply` 改图」这条完整链路。若本机无 GPU，第 5 步的 dump 标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果我新写了一个融合 pass，但忘了在 `default.yaml` 里加它，会发生什么？

**参考答案**：装饰器仍会在 import 时把类注册进 `TransformRegistry`，所以 `TransformRegistry.has(name)` 为真；但因为 `ad_config.transforms` 来自 YAML，未列出的 pass 不会进入 `InferenceOptimizer._clean_config` 的结果，也就不会被执行。即「注册 ≠ 启用」，启用必须经 YAML（或等价的配置注入）。

**练习 2**：为什么 `fuse_silu_mul` 的阶段是 `post_load_fusion` 而不是 `pattern_matcher`？

**参考答案**：这个融合依赖一个前置条件——`gate`/`up` 已经被 GEMM 融合成单个 `gate_up` 投影（产生 `narrow` 切分模式）。GEMM 融合需要权重已加载（`weight_load`），因此它必须在 `weight_load` 之后的 `post_load_fusion` 阶段；而 `pattern_matcher` 在更早的位置，那时还没有可融合的 narrow 模式。

**练习 3**：`BaseTransform.__call__` 被标记为 `@final`，子类不能覆盖它，那子类怎么自定义行为？

**参考答案**：通过实现抽象方法 `_apply`（或 `_apply_to_full_model`，当 `run_per_gm=False` 时）。`__call__` 是固定的「模板方法」，它负责清理、统计、日志、dump，把控制权在合适时机交给 `_apply`；可选的 `_post_init` 钩子允许自定义初始化。

---

### 4.3 piecewise 编译：用编译后端把优化图变成 CUDA Graph

#### 4.3.1 概念说明

图变换结束后，模型还是个普通的 FX `GraphModule`——能跑，但每次前向都要逐个 kernel launch，CPU 开销大。**编译（COMPILE）阶段**的任务是把它变成可高效回放的形态，最典型的就是 CUDA Graph。

但生成场景有个老大难：

- **decode（生成）阶段**：每步每个序列只产 1 个 token，batch 形状可控，可以把整个模型录成**一张整图（monolithic）CUDA Graph**，按 batch size 分桶捕获，回放最快。
- **prefill（上下文）阶段**：序列长度千变万化，整图录不下；而且图里有「动态算子」（attention、SSM 等，它们的输出形状依赖运行时的 batch_info），无法被静态捕获。

AutoDeploy 的解法是 **dual-mode（双模式）**：

- 默认开 `piecewise_enabled`：同时构造一个整图 `CapturedGraph`（管 decode）和一个分片 `PiecewiseCapturedGraph`（管 prefill/mixed）。
- 分片方案在动态算子边界把图切成「静态段」和「动态段」：静态段包进 `ADPiecewiseRunner` 录 CUDA Graph，动态段走 eager（或包进 wrapper 喂预分配 buffer）。
- 运行时 `DualModeCapturedGraph` 按 `batch_info` 判断当前是 decode-only 还是 prefill/mixed，分流到对应路径。

这套机制由一个独立的 `CompileBackendRegistry` 管理，提供 `torch-simple` / `torch-compile` / `torch-cudagraph` / `torch-opt` 四个后端，互可替换。

#### 4.3.2 核心流程

```text
compile_model (COMPILE 阶段 pass)
  └─ CompileModelConfig.backend ∈ {torch-simple, torch-compile, torch-cudagraph, torch-opt}
     └─ CompileBackendRegistry.get(backend)(target, **kwargs).compile()
        │
        ├─ torch-opt: 先 torch.compile(model, dynamic=True)，再走 torch-cudagraph
        │
        └─ torch-cudagraph (默认):
             ├─ monolithic = CapturedGraph(target_gm)
             │    └─ capture_graph(get_args_kwargs, cuda_graph_batch_sizes)
             │         对每个 batch_size: warmup → cuda.graph 捕获 → 存进 self.cudagraphs[shape]
             │
             ├─ piecewise = PiecewiseCapturedGraph(target_gm, piecewise_num_tokens)
             │    ├─ prepare(): split_graph_at_dynamic_ops(gm)
             │    │     静态段 → ADPiecewiseRunner；动态段 → DynamicOpWrapper/MetadataWrapper/留 eager
             │    └─ warmup_and_capture(get_args_kwargs):
             │         对每个 num_tokens 桶（大到小）:
             │           warmup → 发现动态算子输出形状 → capture → empty_cache
             │           （ADPiecewiseRunner 内部：warmup/capture/replay 三态由类变量切换）
             │
             └─ DualModeCapturedGraph(monolithic, piecewise)
                 运行时：decode-only → monolithic.replay；prefill/mixed → piecewise
```

**整图（monolithic）的运行时逻辑**（`CapturedGraph.forward`）：把输入张量按各自动态维 copy 进预捕获的 input buffer，查表找匹配的 `combined_shape`，`cudagraphs[shape].replay()`，从输出 buffer 截取实际 batch 长度返回。不匹配则回退 eager。

**分片（piecewise）的三态机**：`ADPiecewiseRunner` 用两个类级变量 `_current_phase`（warmup/capture/replay）与 `_current_num_tokens`（哪个桶）控制行为，由编排器 `PiecewiseCapturedGraph` 在每次前向前设置。

#### 4.3.3 源码精读

**编译后端注册表与抽象**：

[tensorrt_llm/_torch/auto_deploy/compile/compiler.py:29-57](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/compile/compiler.py#L29-L57) —— `CompileBackendRegistry`（与 `TransformRegistry` 同构）+ `CompilerBackend(ABC)`，后者只要求实现 `compile() -> nn.Module`。注意它和图变换的注册表是**两套独立系统**：图变换用 `TransformRegistry`，编译后端用 `CompileBackendRegistry`。

**COMPILE 阶段 pass**：

[tensorrt_llm/_torch/auto_deploy/transform/library/compile_model.py:81-103](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/transform/library/compile_model.py#L81-L103) —— `CompileModelConfig`：四个 backend 选项；`piecewise_enabled` 默认 False，但 YAML 里默认开 True；`piecewise_enabled` 要求 backend 必须是 `torch-cudagraph` 或 `torch-opt`（`validate_piecewise_backend` 校验器）。

[tensorrt_llm/_torch/auto_deploy/transform/library/compile_model.py:184-200](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/transform/library/compile_model.py#L184-L200) —— `_compile_one`：`CompileBackendRegistry.get(self.config.backend)(target, **backend_kwargs).compile()`。这就是 pass 与后端的接缝。

[tensorrt_llm/_torch/auto_deploy/transform/library/compile_model.py:43-64](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/transform/library/compile_model.py#L43-L64) —— `_generate_default_piecewise_num_tokens`：当用户未指定桶时，自动生成 2 的幂 `[64, 128, 256, ..., max_num_tokens]`，每桶最多 2× padding 开销。

**整图 CUDA Graph**：

[tensorrt_llm/_torch/auto_deploy/compile/backends/torch_cudagraph.py:175-274](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/compile/backends/torch_cudagraph.py#L175-L274) —— `class CapturedGraph` 与 `_capture_one_graph`：在 `with torch.cuda.graph(graph, pool=self._cuda_graph_mem_pool):` 里跑一次模型，把输出写进预分配 buffer；所有 batch size 共享同一个 graph memory pool。

[tensorrt_llm/_torch/auto_deploy/compile/backends/torch_cudagraph.py:276-398](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/compile/backends/torch_cudagraph.py#L276-L398) —— `capture_graph`：先用「max_batch 与 probe_batch 形状对比」**自动探测动态维**（L311-322），再对每个 batch size 把输入 copy 进 input buffer 后捕获，存进 `self.cudagraphs[combined_shape]`。运行时 `forward`（L399）用输入形状做 key 查表回放，不命中则 eager。

**分片 CUDA Graph 编排器**：

[tensorrt_llm/_torch/auto_deploy/compile/backends/torch_cudagraph.py:465-659](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/compile/backends/torch_cudagraph.py#L465-L659) —— `class PiecewiseCapturedGraph`。`prepare()`（L505）调 `split_graph_at_dynamic_ops(gm)` 切图，把静态段包进 `ADPiecewiseRunner`、动态段按策略包进 `DynamicOpWrapper`（喂 `out=` 预分配 buffer）或 `MetadataWrapper`（稳定地址）或留 eager。一个值得注意的优化：默认把尾部的 `lm_head` 静态段排除出捕获（L552-564），因为它产出的 `[num_tokens, vocab_size]` 张量在 graph pool 里代价巨大（注释举 256K vocab、nt=8192 时 4–6 GiB），让它 eager 跑更划算——与 PyTorch 后端 piecewise 行为一致。

[tensorrt_llm/_torch/auto_deploy/compile/backends/torch_cudagraph.py:793-864](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/compile/backends/torch_cudagraph.py#L793-L864) —— `warmup_and_capture`：对每个 num_tokens 桶（大到小）做 warmup → 形状发现 → capture → `gc.collect() + empty_cache()`。捕获后转 replay 态。

**分片运行器（三态机）**：

[tensorrt_llm/_torch/auto_deploy/compile/piecewise_runner.py:272-300](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/compile/piecewise_runner.py#L272-L300) —— `class ADPiecewiseRunner`：用类级 `_current_phase` 与 `_current_num_tokens` 控制三态。这种「用类变量当全局开关」的设计让被包的子模块完全无感——它只管 `forward`，由外部编排器告诉它现在该 warmup / capture / replay。

[tensorrt_llm/_torch/auto_deploy/compile/piecewise_runner.py:397-426](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/compile/piecewise_runner.py#L397-L426) —— capture 态：`with torch.cuda.graph(graph, pool=self._graph_pool):` 录制子段，并在录制**内部**为后续动态算子预分配输出 buffer（`dynamic_out_bufs`），保证它们从共享 graph pool 拿到确定性地址。这是「不需要 copy-back」的关键：输入来自稳定 InputBuffer、权重地址固定、动态输出在 capture 内预分配——所有地址在 replay 时都不变。

[tensorrt_llm/_torch/auto_deploy/compile/piecewise_runner.py:428-455](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/compile/piecewise_runner.py#L428-L455) —— replay 态：`entry.cuda_graph.replay()`，返回预存的 `static_output`。还内置了一次性「地址校验」：首次 replay 时比对运行时输入地址与捕获时记录的地址，不一致则打 error 日志（帮助排查 InputBuffer 地址漂移）。

**双模式分流**：

[tensorrt_llm/_torch/auto_deploy/compile/backends/torch_cudagraph.py:910-1071](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/compile/backends/torch_cudagraph.py#L910-L1071) —— `class DualModeCapturedGraph`：`forward`（L1037）先用 `_is_decode_only`（读 `batch_info_host[0]` 即 num_prefill）判断；decode-only 走 monolithic；否则按 `_get_num_tokens` 找 `>= num_tokens` 的最小桶走 piecewise，并对输出做截断（`_truncate_output` 把 padding 桶截回真实长度）；找不到桶则 eager 回退。

**叠加 torch.compile 的后端**：

[tensorrt_llm/_torch/auto_deploy/compile/backends/torch_opt.py:25-41](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/compile/backends/torch_opt.py#L25-L41) —— `torch-opt` 继承 `torch-cudagraph`，`compile()` 里先 `torch.compile(self.model, dynamic=True)` 再 `super().compile()`。即先让 PyTorch 的 inductor 做一轮图级融合/优化，再录 CUDA Graph，两者叠加。

#### 4.3.4 代码实践

**目标**：理清「编译 pass → 后端注册表 → dual-mode 分流」的关系，并验证分片捕获对 num_tokens 分桶的行为。

**步骤**：

1. **后端二选一**：在 `transform/library/compile_model.py:196` 处，把 `self.config.backend` 分别设想为 `torch-cudagraph` 与 `torch-opt`，确认两者都经 `CompileBackendRegistry.get(...)` 拿到对应类（`torch_opt.py:26` 继承自 `torch_cudagraph.py:1155` 的 `TorchCudagraphCompiler`）。
2. **桶的生成**：读 `compile_model.py:43-64` 的 `_generate_default_piecewise_num_tokens`。假设 `max_num_tokens=2048`，手算默认桶序列应为 `[64, 128, 256, 512, 1024, 2048]`。
3. **分流判定**：在 `DualModeCapturedGraph.forward`（`torch_cudagraph.py:1037`）设断点，构造三种 batch：纯 decode（num_prefill=0）、纯 prefill、混合，观察分别走哪条路径。注意 `_is_decode_only` 读的是 `batch_info_host[0]`。
4. **分桶查找**：跟踪 `_find_nearest_bucket`（L990）。若某次 prefill 总 token 数 = 300，桶序列如上，应选到 512 这档（`>=300` 的最小桶），输出最后被 `_truncate_output` 截回 300。
5. **可选运行验证**：用 `examples/auto_deploy/build_and_run_ad.py` 跑一个小模型，开 `piecewise_enabled`，在日志里搜 `PiecewiseCapturedGraph: captured graphs for num_tokens=` 与 `Capturing graph for batch size:`，对比分片桶与整图 batch size 两套捕获日志。

**需要观察的现象**：

- 日志里同时出现整图捕获（按 batch_size）与分片捕获（按 num_tokens）两套——印证 dual-mode。
- `prepare()` 日志会打印 `N static runners, M dynamic wrapped, K dynamic eager`，反映切图结果。
- 尾部 lm_head 段被排除捕获时会有一行 `excluding trailing static submod_* (lm_head) from capture`。

**预期结果**：能解释「为什么 decode 用整图、prefill 用分片」「为什么按 num_tokens 分桶而不是按 batch size」「为什么要在动态算子处切图」。第 5 步若无 GPU，标注「待本地验证」，仅做源码阅读。

#### 4.3.5 小练习与答案

**练习 1**：为什么不能直接对 prefill 阶段也录整图 CUDA Graph？

**参考答案**：prefill 的序列长度随请求变化，输入形状动态；整图 CUDA Graph 要求录制的输入形状与回放时完全一致（地址也要稳定）。形状一变就要重新捕获，而运行时捕获会引入毛刺且可能因地址失效出错。分片方案把可静态化的部分（norm、GEMM 等）录图、把形状依赖运行时的动态算子（attention 等）留在 eager，兼顾了性能与灵活性。

**练习 2**：`ADPiecewiseRunner` 用「类级变量」`_current_phase` 控制三态，这种全局状态有什么风险？

**参考答案**：类级变量是所有实例共享的「隐式全局」。如果同一时刻有多个 `ADPiecewiseRunner` 并发执行（比如多线程），它们会互相覆盖 `_current_phase` / `_current_num_tokens`，导致行为错乱。AutoDeploy 通过「由单一编排器 `PiecewiseCapturedGraph` 在每次 forward 前统一设置、且单步内不并发」来规避——这是一种「靠调用纪律保证安全」的设计，使用时要遵守该约定。

**练习 3**：`torch-opt` 与 `torch-cudagraph` 的关系是什么？

**参考答案**：`TorchOptCompiler` 继承 `TorchCudagraphCompiler`，`compile()` 先 `torch.compile(model, dynamic=True)` 再调 `super().compile()`。即 `torch-opt = torch.compile（inductor 图级优化）+ torch-cudagraph（CUDA Graph 录制）`，是叠加关系而非并列。这与 u10-l4 讲的「piecewise 建在 torch.compile 的 fullgraph 追踪之上」一脉相承。

---

## 5. 综合实践

把三个模块串起来，做一次「端到端追踪」。

**任务**：选定一个 pass（推荐 `fuse_silu_mul`）与一个 compile 后端（推荐 `torch-cudagraph`），从用户构造 `LLM` 开始，一路追到该 pass 执行、再到编译后端产出 CUDA Graph，画出一张包含下列要素的完整时序图：

1. `LLM(__init__)` → `llmapi/llm.py:185` 派发 → `AutoDeployLlmArgs`。
2. `_build_model` → `ADEngine.build_from_config` → `InferenceOptimizer(...)`。
3. `InferenceOptimizer.__call__` 按 `Stages` 排序后遍历：`export_to_gm`（EXPORT）→ `fuse_silu_mul`（POST_LOAD_FUSION）→ `compile_model`（COMPILE）。
4. 在 `fuse_silu_mul` 节点标注：装饰器注册（`fuse_silu_mul.py:186`）→ YAML 启用（`default.yaml:244`）→ `BaseTransform.__call__` 外壳 → `_apply` 改图。
5. 在 `compile_model` 节点标注：`CompileBackendRegistry.get("torch-cudagraph")` → `DualModeCapturedGraph`（monolithic + piecewise）。

**进阶（可选）**：对照 [u3-l2 PyExecutor 单步循环](u3-l2-pyexecutor-step-loop.md) 与 [u3-l3 ModelEngine](u3-l3-model-engine-forward.md)，在时序图末尾画一条「`PyExecutor` 单步 → `ADEngine.forward` → `DualModeCapturedGraph.forward` → 整图/分片 replay」的运行时链路，体现「编译期产物在运行期被回放」。

**交付物**：一张时序图 + 一段说明，指出 AutoDeploy 与默认 PyTorch 后端在「编译期做什么」「运行期共享什么」上的异同。

> 若本机无 GPU/模型权重无法实跑，可纯做源码追踪；凡涉及实际运行结果处明确标注「待本地验证」。

## 6. 本讲小结

- **AutoDeploy 是编译器，不是新引擎**：它用 `torch.export` 把任意 PyTorch 模型抽成 FX 图，自动做推理优化，最后产出一个 `ADEngine`（`ModelEngine` 的实现），照样跑在共享的 `PyExecutor` 单步循环上——in-flight batching、调度、采样全部复用。
- **shim 极薄**：AutoDeploy 的 `LLM` 继承 `_TorchLLM`，只强制 `backend="_autodeploy"` 并接管 `_build_model`（绕过 `CachedModelLoader`），其余全部复用；`llmapi/llm.py:185` 是派发分叉点。
- **图变换是「注册表 + 阶段枚举」架构**：`@TransformRegistry.register(name)` 自注册，`Stages` 枚举定义执行顺序，`InferenceOptimizer._clean_config` 按阶段排序，`BaseTransform.__call__` 是统一的清理/统计/dump 外壳，子类只实现 `_apply`。新增优化 = 一个类 + 一个装饰器 + 一行 YAML。
- **`fuse_silu_mul` 是典型 pass**：在 `post_load_fusion` 阶段把「silu(narrow) * narrow」模式替换成单个融合算子；改图靠 FX 的 `graph.call_function` + `replace_all_uses_with` + `eliminate_dead_code`。
- **编译阶段产出 dual-mode CUDA Graph**：`compile_model` pass 经 `CompileBackendRegistry` 选后端；默认 `torch-cudagraph` 同时构造整图（管 decode）与分片（管 prefill/mixed）两套，由 `DualModeCapturedGraph` 按 `batch_info` 分流。
- **分片核心是「在动态算子边界切图 + 三态机捕获」**：`split_graph_at_dynamic_ops` 切图，静态段包进 `ADPiecewiseRunner`（warmup/capture/replay 三态由类变量切换），按 num_tokens 分桶捕获；动态输出在 capture 内预分配以拿到稳定地址，故 replay 无需 copy-back。

## 7. 下一步学习建议

- **往下读自定义算子**：本讲多次出现 `torch.ops.auto_deploy.*`（如 `flashinfer_silu_and_mul`、`trtllm_quant_fp8_linear`）。这些算子的定义、`torch.library.custom_op` 的纯函数约定、与 CUDA Graph 的兼容性，见 [u12-l2 自定义算子与内核](u12-l2-custom-ops-and-kernels.md)。
- **对比 PyTorch 后端的 piecewise**：[u10-l4 CUDA Graph 与 torch.compile / piecewise](u10-l4-cuda-graph-and-compile.md) 讲的是默认后端的同源机制，对照阅读能看清「自动编译」与「手工接线」两种范式的取舍。
- **深入分布式 sharding pass**：本讲把 `SHARDING` 阶段当作黑盒一笔带过。AutoDeploy 的自动分片（`transform/library/sharding.py` / `sharding_ir.py`）是其相对 PyTorch 后端最具差异化的能力之一，建议结合 [u9-l1 Mapping 与并行策略](u9-l1-mapping-and-parallelism.md) 阅读。
- **跑官方 demo**：`examples/auto_deploy/build_and_run_ad.py` 与 `examples/auto_deploy/README.md` 是最低成本的实跑入口；`docs/source/features/auto_deploy/` 下的 `advanced/` 目录有 KV cache 架构、测试策略、benchmark 等专题。
- **动手加一个 pass**：参照 `fuse_silu_mul.py` 的结构（继承 `BaseTransform` + 装饰器 + 实现 `_apply` + 在 `default.yaml` 加一行），试着写一个最小改写（如把某个 `aten` 序列替换成单算子），是检验是否真正理解本讲的最佳方式。
