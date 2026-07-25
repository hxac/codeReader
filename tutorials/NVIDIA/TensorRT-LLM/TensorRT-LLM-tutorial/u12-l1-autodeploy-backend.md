# AutoDeploy 后端（图变换与编译）

## 1. 本讲目标

本讲是「二次开发与扩展」单元的第一讲，承接 u3-l3（ModelEngine 与模型前向）和 u5-l1（模型架构范式）。前面所有讲义默认的都是 TensorRT-LLM 的**默认 PyTorch 后端**——模型由人工编写的 `modeling_*.py` 实现，前向逻辑手工写死、手工优化。本讲要打开的是第二条执行路径——**AutoDeploy（Beta）后端**。

学完本讲，你应当能够：

1. 说清 AutoDeploy 与默认 PyTorch 后端在**入口**和**模型来源**上的根本差异。
2. 复述 AutoDeploy 的核心思路：把一个（几乎）未经修改的 HuggingFace 模型，`torch.export` 成 FX 计算图，再跑过一条**自动图变换流水线**，最后编译进同一个 PyExecutor 运行时。
3. 看懂图变换的注册机制（`@TransformRegistry.register`、import 即注册）、流水线编排（`InferenceOptimizer` + `Stages` 阶段排序），并以 `fuse_silu_mul` 为例追踪一次「检测子图 → 替换为融合算子」的完整过程。
4. 理解 COMPILE 阶段的 `CompileBackendRegistry` 与四种后端，以及 **piecewise（分片）CUDA Graph** 如何在动态形状下用「按桶分段录图 + 强制 padding」高效回放。

> 前置认知回顾：u3-l3 讲过 `ModelEngine` 是引擎的抽象契约（必须实现 `forward` 与 `get_max_num_sequences`），`PyTorchModelEngine` 是其默认实现；u3-l2 讲过 `PyExecutor` 单步循环把请求推进一个 token；u2-l3 讲过「Python 调度、C++ 加速」。AutoDeploy 不会推翻这套运行时，它只是换了一种方式来生产 `PyExecutor` 跑的那个 `model` 模块。

## 2. 前置知识

在进入源码前，先用通俗语言建立三个直觉。

### 2.1 什么是「图变换」

PyTorch 模型在执行时，本质上是一张**有向无环计算图**：节点是算子（加、乘、矩阵乘、注意力……），边是张量。所谓「图变换」，就是在这张图上做**等价改写**以提升性能。比如：

- 把相邻的 `silu` 和 `mul` 两个小算子合并（fuse）成一个 `silu_and_mul`，省掉一次 kernel 启动和一次显存读写；
- 把一个线性层的权重在多卡间切成多份（sharding），插入 `all_reduce`；
- 把标准注意力替换成带 KV cache 的分页注意力。

PyTorch 用 `torch.fx` 把 `nn.Module` 捕获成 `GraphModule`，于是你可以用 Python 直接增删改图里的节点。AutoDeploy 的全部魔法都建立在 `torch.fx` 之上。

### 2.2 FX 图、`torch.export` 与「export」是什么

`torch.export` 是 PyTorch 2.x 提供的「把一个 `nn.Module` 序列化成稳定计算图」的官方手段。它会把模型拆解成最基本的 ATen 算子（`aten.mm`、`aten.silu` 等）加上一些自定义算子，得到一份与具体输入形状解耦的图。AutoDeploy 在流水线最前面就用 `torch.export` 把 HF 模型变成图，后面所有变换都作用于这张图。

### 2.3 为什么需要「piecewise CUDA Graph」

u10-l4 讲过 CUDA Graph：把一串 kernel 调用录制成一张图，回放时一次性提交，省掉逐个 kernel 的 CPU 启动开销。但 CUDA Graph 要求**输入地址和形状在录图时固定**。问题是推理时 batch 里的 token 数是动态的（prefill 一长串、decode 一两个）。解决办法是「分片（piecewise）」：把模型在**动态算子**（如注意力，它的输出形状依赖 token 数）处切开，动态算子保持 eager（每次真算），而切出来的**静态段**（GEMM、RMSNorm 等形状只随 token 数线性变化的段）则按若干个 token 档位（bucket）分别录图，运行时把实际 token 数 **padding 到最近的档位**再回放。这就是 piecewise CUDA Graph 的核心思想。

## 3. 本讲源码地图

AutoDeploy 的代码集中在 `tensorrt_llm/_torch/auto_deploy/`，体量很大，本讲只聚焦以下文件：

| 文件 | 作用 |
|------|------|
| `tensorrt_llm/_torch/auto_deploy/llm.py` | AutoDeploy 的 `LLM` 高层入口，继承 `_TorchLLM`，把 `backend` 强制为 `_autodeploy` |
| `tensorrt_llm/_torch/auto_deploy/shim/ad_executor.py` | `ADEngine`（`ModelEngine` 的 AD 实现）+ `create_autodeploy_executor`（把 AD 接进 `PyExecutor` 的总装函数） |
| `tensorrt_llm/_torch/auto_deploy/transform/interface.py` | 图变换的统一契约：`Stages` 阶段枚举、`BaseTransform` 基类、`TransformRegistry` 注册表 |
| `tensorrt_llm/_torch/auto_deploy/transform/optimizer.py` | `InferenceOptimizer`：编排整条变换流水线 |
| `tensorrt_llm/_torch/auto_deploy/transform/library/fuse_silu_mul.py` | 一个具体图变换示例：融合 `narrow+silu+mul` |
| `tensorrt_llm/_torch/auto_deploy/transform/library/build_model.py` | `build_model` 变换：流水线起点，通过 factory 构建模型 |
| `tensorrt_llm/_torch/auto_deploy/transform/library/compile_model.py` | `compile_model` 变换：流水线终点，选择编译后端并编译 |
| `tensorrt_llm/_torch/auto_deploy/compile/compiler.py` | `CompileBackendRegistry` + `CompilerBackend` 抽象 |
| `tensorrt_llm/_torch/auto_deploy/compile/backends/torch_compile.py` | 最简单的编译后端：`torch.compile(model, dynamic=True)` |
| `tensorrt_llm/_torch/auto_deploy/compile/backends/torch_cudagraph.py` | CUDA Graph 后端，含 monolithic 与 piecewise 两套机制 |
| `tensorrt_llm/_torch/auto_deploy/compile/piecewise_runner.py` | `ADPiecewiseRunner`：单个静态段的 warmup→capture→replay |
| `tensorrt_llm/_torch/auto_deploy/config/default.yaml` | 默认变换流水线配置（"graph" 模式） |
| `docs/source/features/auto_deploy/auto-deploy.md` | AutoDeploy 官方总览文档 |

记住一句话定位：**`llm.py`/`ad_executor.py` 负责「把 AD 接进运行时」，`transform/` 负责「把模型变成优化图」，`compile/` 负责「把优化图编译成可高效执行的模块」**。

## 4. 核心概念与源码讲解

### 4.1 AutoDeploy shim：把 AD 接进 PyExecutor

#### 4.1.1 概念说明

「shim（垫片）」在这里的含义是：AutoDeploy 并没有另起炉灶写一套运行时，而是做了一层薄薄的适配，让自己生产的模型能塞进 `PyExecutor`——也就是 u3-l2 讲的那个单步循环发动机。

回忆 u3-l1/u3-l3：用户调用 `LLM.generate()` → 构造 Executor → `PyExecutor` 单步循环 → 调 `ModelEngine.forward()` → 模型前向。默认后端里 `ModelEngine` 的实现是 `PyTorchModelEngine`，它直接 `self.model.forward(...)` 调用手工写的 `modeling_*.py`。

AutoDeploy 的关键洞察是：**`ModelEngine` 是一个抽象契约，只要提供一个实现了 `forward` 和 `get_max_num_sequences` 的引擎类，就能替换掉 `PyTorchModelEngine`**。AutoDeploy 提供的就是 `ADEngine`。两者共享同一个 `PyExecutor`、同一套 `ResourceManager` / `Scheduler` / `Sampler`，唯一不同的是「那个被 forward 的 `model` 是怎么来的、长什么样」。

- 默认后端：`model` = 人工写的 `DecoderModelForCausalLM`（u5-l1）。
- AutoDeploy：`model` = HF 模型经 `torch.export` + 一串图变换 + 编译后得到的优化 `nn.Module`。

#### 4.1.2 核心流程

AutoDeploy 从用户入口到 `PyExecutor` 的总装过程：

```text
用户构造
  LLM(model=..., backend 自动设为 _autodeploy)        # llm.py
    └─ _build_model()  绕过 CachedModelLoader，走 factory
         └─ create_autodeploy_executor(ad_config)     # ad_executor.py 的入口函数
              ├─ build ADEngine:
              │     ADEngine.build_from_config(...)
              │       ├─ factory = ad_config.create_factory()
              │       ├─ cache_seq_interface = CachedSequenceInterface(...)
              │       └─ InferenceOptimizer(factory, config=ad_config.transforms)  ← 变换流水线
              └─ PyExecutor(model_engine=engine, sampler=..., scheduler=..., ...)
                   ↑ 与默认后端用的是同一个 PyExecutor 类
运行时单步
  PyExecutor._engine_loop → ADEngine.forward(scheduled_requests, ...) → self.model(**named_args)
```

也就是说：**入口和运行时都复用，AD 只替换了「模型生产」这一段**。

#### 4.1.3 源码精读

**入口 `LLM` 类**：[tensorrt_llm/_torch/auto_deploy/llm.py:136-150](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/llm.py#L136-L150) 中，`class LLM(_TorchLLM)` 继承自默认后端的 `_TorchLLM`（见 u3-l1 的三层继承），并在 `__init__` 里强制 `kwargs["backend"] = "_autodeploy"`，于是父类 `BaseLLM.__init__` 会按 AD 分支来构建。

[llm.py:187-203](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/llm.py#L187-L203) 的 `_build_model` 关键是 `super()._build_model()` 之前先 `self._prefetch_model()` 用 factory 拉取 checkpoint，注释明确写道「we bypass the regular LLM CachedModelLoader in _autodeploy backend」——因为 AD 用自己的 `ModelFactory` 和图变换来装载权重，不复用默认后端的 `CachedModelLoader`。

**真正的引擎 `ADEngine`**：[ad_executor.py:369-375](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/shim/ad_executor.py#L369-L375) 声明 `class ADEngine(ModelEngine)`，docstring 说明它遵循 `ModelEngine` 抽象、负责「构建 AD 优化模型、把 TRT-LLM 的 scheduled requests 翻译成 AD 原生的 PyTorch 输入、跑模型、返回 logits」。

它的工厂方法 [ad_executor.py:420-433](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/shim/ad_executor.py#L420-L433) 构造了 `InferenceOptimizer(factory=factory, config=ad_config.transforms, dist_config=dist_config)`——这就是图变换流水线对象。注意 `config=ad_config.transforms`：整条流水线来自 `LlmArgs.transforms`，而这个字段的默认值就是 `default.yaml`（见 4.2.3）。

**前向入口**：[ad_executor.py:1049-1079](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/shim/ad_executor.py#L1049-L1079) 是 `ADEngine.forward`，它被 `@maybe_pad_for_cuda_graph` 装饰（用 dummy 请求把 batch 补齐到 CUDA Graph 档位，呼应 u3-l2 讲的 is_dummy 假请求），内部先 `_prepare_inputs` 把 scheduled requests 翻译成 `cache_seq_interface`（缓存序列接口）里的张量，再调 `_run_forward`。[ad_executor.py:1017-1043](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/shim/ad_executor.py#L1017-L1043) 的 `_run_forward` 最关键的一行是：

```python
model_output = self.model(**csi.named_args, cache_seq_interface=csi)
```

这里的 `self.model` 就是经过图变换和编译后的 `nn.Module`。对比 u3-l3 的 `PyTorchModelEngine.forward` 也是汇聚到 `self.model.forward`——**两者最终都是在跑一个 `nn.Module`，区别只在那个模块是被人工写的还是被图变换造出来的**。

**总装函数 `create_autodeploy_executor`**：[ad_executor.py:1156-1344](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/shim/ad_executor.py#L1156-L1344) 是 AD 后端的对外入口，docstring 写明「This is the entrypoint API to the _autodeploy backend」。它做的事情几乎全部是「把 AD 引擎和默认后端的运行时组件拼到一起」：构造 `ADEngine`、`KVCacheManager`、`SeqSlotManager`、`ResourceManager`、`SimpleScheduler`（u8-l1 的两步调度）、`Sampler`（u8-l3），最后 `py_executor = PyExecutor(...)`。读这段代码会发现：**除了 `model_engine=engine` 换成 `ADEngine`，其余参数和默认后端构造 `PyExecutor` 时如出一辙**——这是「shim」二字最好的注脚。

#### 4.1.4 代码实践

**实践目标**：用「源码阅读 + 配置 dry-run」的方式，亲眼确认 AutoDeploy 与默认后端共用 `PyExecutor`，只是换了 `model_engine`。

**操作步骤**：

1. 先做静态阅读。打开 [ad_executor.py:1326-1343](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/shim/ad_executor.py#L1326-L1343)，列出 `PyExecutor(...)` 构造时传入的全部参数，标注哪些来自 AD 专属对象（`engine`）、哪些与默认后端同名同语义（`scheduler`、`sampler`、`resource_manager`、`dist`）。
2. 对比 u3-l2 / u3-l3 中默认后端构造 `PyExecutor` 的路径（`py_executor_creator.py` 的 `create_py_executor_instance`），列出两者的异同。
3. 用 `build_and_run_ad.py --dry-run` 打印最终配置而不真正加载模型（该脚本支持 `--dry-run` 标志，见 [examples/auto_deploy/build_and_run_ad.py:370-378](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/examples/auto_deploy/build_and_run_ad.py#L370-L378)）：

   ```bash
   cd examples/auto_deploy
   python build_and_run_ad.py --model "TinyLlama/TinyLlama-1.1B-Chat-v1.0" --dry-run
   ```

**需要观察的现象**：dry-run 会 dump 出完整的 `ExperimentConfig`（一个 YAML），其中 `args.transforms` 字段就是即将跑的图变换清单（来自 `default.yaml`），`args.compile_backend` 是编译后端。

**预期结果**：你能从输出的 YAML 里看到 `transforms` 是一个从 `build_model` 到 `compile_model` 的有序字典，验证「AD = 流水线 + 编译」的心智模型。

**说明**：若本机无 GPU 或无法下载模型权重，dry-run 仍可工作（它在构造模型前就 return）；但若 `transformers` 未安装则会报 import 错误——这种情况按「待本地验证」处理，重点放在静态阅读步骤。

#### 4.1.5 小练习与答案

**练习 1**：`ADEngine` 为什么必须继承 `ModelEngine` 而不能直接当 `nn.Module` 用？
**参考答案**：因为 `PyExecutor` 单步循环通过 `ModelEngine` 抽象契约（`forward` / `get_max_num_sequences`）与引擎交互。继承 `ModelEngine` 并实现这两个方法，AD 才能以「可替换引擎」的身份插进同一个 `PyExecutor`，从而复用调度、采样、资源管理等全部运行时设施。这正是 u3-l3 讲的「抽象使 PyExecutor 与具体后端解耦」。

**练习 2**：`ADEngine.forward` 上的 `@maybe_pad_for_cuda_graph` 装饰器（[ad_executor.py:132-233](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/shim/ad_executor.py#L132-L233)）和 u3-l2 讲的「is_dummy 假请求」是什么关系？
**参考答案**：它们是同一件事的两面。`maybe_pad_for_cuda_graph` 在 batch 不足某个 CUDA Graph 档位时，生成 `padding_dummy_request`（一个 `is_cuda_graph_dummy=True` 的请求）把 batch 补齐；这些假请求正是 u3-l2 讲的「attention-DP/CUDA Graph 造出的 is_dummy 占位请求」，会被排除出投机解码接受率统计。

### 4.2 图变换：transform/library 与 InferenceOptimizer 流水线

#### 4.2.1 概念说明

如果说 4.1 解决了「把 AD 接进运行时」，那 4.2 解决的是「AD 的模型是怎么被造出来的」。答案是：**一条分阶段的图变换流水线**。

这条流水线的设计哲学是「**一个变换只做一件事，按阶段排序串联**」。每个变换（transform）是一个继承 `BaseTransform` 的类，实现一个 `_apply` 方法，输入输出都是一个 `GraphModule`。变换之间靠「阶段（stage）」和「图清理（cleanup）」协作：

- **阶段**：每个变换声明自己属于哪个阶段，`InferenceOptimizer` 会按阶段把所有变换排成一条有序流水线。
- **图清理**：变换可能把图弄「脏」（留下死节点、shape 信息失效），所以 `BaseTransform.__call__` 会在每个变换前后自动跑 `canonicalize_graph`（图规范化）和 `run_shape_prop`（shape 传播），保证下一个变换拿到的是干净图。

这种「小变换 + 自动清理 + 注册表」的设计，让添加新优化极其容易——写一个类、加一个装饰器、在 YAML 里写一行，流水线就自动纳入。

#### 4.2.2 核心流程

变换流水线的九个阶段（按执行顺序）：

```text
FACTORY          构建模型骨架（meta 设备，不装权重）
  │              ── build_model / build_and_load_factory_model
EXPORT           torch.export 把 nn.Module 捕获成 GraphModule
  │              ── export_to_gm
POST_EXPORT      导出后的低层清理（去 noop slice/add）
  │
PATTERN_MATCHER  高层模式匹配，把千奇百怪的 HF 写法「标准化」成统一 IR
  │              ── match_rmsnorm_pattern / match_swiglu_pattern / match_rope_pattern ...
SHARDING         自动并行切分：切权重、插 all_reduce、插 MoE alltoall
  │              ── apply_sharding_hints / detect_sharding
WEIGHT_LOAD      装载真实权重到切分后的骨架
  │              ── load_weights / move_inputs_to_device
POST_LOAD_FUSION 装完权重后的融合优化（此时张量是真实的）
  │              ── fuse_silu_mul / fuse_fp8_linear / fuse_rmsnorm / fuse_moe ...
CACHE_INIT       插入带 KV cache 的分页注意力、初始化缓存池
  │              ── insert_cached_attention / initialize_cache / resize_kv_cache
VISUALIZE        （可选）可视化图
  │
COMPILE          最终编译：torch.compile + CUDA Graph
                 ── compile_model
```

`InferenceOptimizer.__call__` 的执行逻辑很直白：把 YAML 配置里的变换按阶段排序，逐个实例化、逐个调用：

```python
for idx, (t_name, t_config) in enumerate(sorted_config.items()):
    transform = TransformRegistry.get(t_name)(t_config)   # 从注册表取类、实例化
    mod = transform(mod, cm, factory, shared_config, idx)  # 跑这个变换
```

每个 `transform(...)` 调用背后，`BaseTransform.__call__` 会包一层样板代码：读历史的 `TransformInfo`、跑 pre-cleanup、跑 `_apply`、跑 post-cleanup、记录显存变化、dump 图（受 `AD_DUMP_GRAPHS_DIR` 控制）、写回元数据。

#### 4.2.3 源码精读

**阶段枚举**：[interface.py:108-130](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/transform/interface.py#L108-L130) 定义 `class Stages(Enum)`，并用 `@total_ordering` + `__lt__` 让枚举值「按定义顺序可排序」。这正是 `_clean_config` 能按阶段排序的依据。

**注册表**：[interface.py:822-835](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/transform/interface.py#L822-L835) 的 `TransformRegistry.register(name)` 是一个装饰器工厂，它把变换类存进 `_registry` 字典、并把名字写成类的 `_transform_key` 属性。用法形如：

```python
@TransformRegistry.register("fuse_silu_mul")
class FuseSiluMul(BaseTransform):
    ...
```

这与 u5-l2 讲的模型注册（`@register_auto_model`）是同一种「import 即注册」模式：变换类只要被 import 一次，就自动进入注册表。触发 import 的是 [transform/\_\_init\_\_.py:17-20](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/transform/__init__.py#L17-L20)，它 `from . import (library, pipeline_cache)`，其中 `library` 包的 `__init__` 会 import 所有具体变换模块——注释 `- ensure all transforms are registered` 直白说明了目的。

**BaseTransform 模板方法**：[interface.py:368-511](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/transform/interface.py#L368-L511) 的 `__call__` 是用 `@final` 锁定的模板方法，子类不能覆盖它，只能覆盖 `_apply`（[interface.py:770-783](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/transform/interface.py#L770-L783)）。这套模板把「日志、清理、shape 传播、显存统计、图 dump、元数据回写」全部收口在基类，子类只关心改图逻辑。注意 [interface.py:507-508](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/transform/interface.py#L507-L508) 的 `graph_writer.dump_graph(mod, t_name, self.config.stage.value)`——这就是环境变量 `AD_DUMP_GRAPHS_DIR` 控制的「每个变换之后 dump 一份图文本」机制，是调试变换的关键工具。

**编排器**：[optimizer.py:62-75](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/transform/optimizer.py#L62-L75) 的 `_clean_config` 把 YAML 里「名字 → 字典」的配置，按 `Stages(stage)` 排序，并用 `TransformRegistry.get_config_class(k)` 取每个变换专属的 Pydantic 配置类来校验，得到一个严格有序的 `StrictInferenceOptimizerConfig`。[optimizer.py:83-124](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/transform/optimizer.py#L83-L124) 的 `__call__` 就是上文伪代码的真实版，它还支持「从 pipeline cache 前缀恢复」（`_maybe_restore_from_cache`），这是为加速重复构建设计的缓存机制。

**默认配置**：[config/default.yaml](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/config/default.yaml#L18-L346) 是「graph」模式的默认流水线。它用一个顶层 `transforms:` 字典，把每个变换的名字映射到其配置（含 `stage` 字段）。注意 [default.yaml:340-346](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/config/default.yaml#L340-L346) 里 `compile_model` 的 `backend: torch-cudagraph`、`piecewise_enabled: true`——这就是为什么默认情况下 AD 会走 piecewise CUDA Graph 路径。这个 YAML 如何变成 `ad_config.transforms`？看 [llm_args.py:567-574](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/llm_args.py#L567-L574) 的 `_get_yaml_default_from_mode`，它把 `mode="graph"` 映射到 `default.yaml`，再由 `DynamicYamlMixIn`（`utils/_config.py`）合并用户 `yaml_extra` 覆盖项——优先级是「用户 yaml_extra > 默认 yaml」，与 u4-l2 讲的「用户显式优先」一脉相承。

#### 4.2.4 代码实践：追踪 `fuse_silu_mul` 的注册与执行

这是本讲的主线实践——完整追踪一个图变换「如何被发现、如何被调用、改了什么图」。

**实践目标**：理解一个变换从注册到执行的完整生命周期，并能用 `AD_DUMP_GRAPHS_DIR` 看到它对图的实际改写。

**操作步骤**：

1. **看注册**。打开 [fuse_silu_mul.py:186-187](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/transform/library/fuse_silu_mul.py#L186-L187)，确认 `@TransformRegistry.register("fuse_silu_mul")` 装饰器把 `FuseSiluMul` 注册到注册表，key 是字符串 `"fuse_silu_mul"`。

2. **看它在流水线里的位置**。在 [default.yaml:244-247](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/config/default.yaml#L244-L247) 看到 `fuse_silu_mul` 配置：`stage: post_load_fusion`、`enabled: true`、`backend: flashinfer`。这表示它要在「装完权重后」执行（因为此时张量是真实的，融合算子才能拿到真实 shape），且用 flashinfer 的融合 kernel。

3. **读它要匹配的子图模式**。[fuse_silu_mul.py:16-41](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/transform/library/fuse_silu_mul.py#L16-L41) 的模块 docstring 写得很清楚：GEMM 融合把 gate/up 投影合成一个 `gemm(x, gate_up_weight)`，于是图里出现 `narrow(...,0,size)` 与 `narrow(...,size,size)` 两个切片，再做 `silu(gate) * up`。这个变换的目标是把 `silu(gate)*up` 替换成单个 `silu_and_mul(fused_out)`。

4. **读改图核心**。[fuse_silu_mul.py:211-271](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/transform/library/fuse_silu_mul.py#L211-L271) 的 `_apply` 逻辑是：遍历图中所有 `aten.mul.Tensor` 节点；对每个 mul 调 `_try_fuse_mul` 判断它是不是 `silu(narrow)*narrow` 模式；若是，则在 mul 前插入一个新的 `silu_and_mul` 算子节点（`graph.call_function(target_op, ...)`），用 `node.replace_all_uses_with(fused_node)` 把所有对原 mul 的引用改指向新节点，最后 `eliminate_dead_code()` 清理孤儿节点。匹配校验在 [fuse_silu_mul.py:355-406](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/transform/library/fuse_silu_mul.py#L355-L406) 的 `_match_silu_narrow_mul`，它会确认两个 narrow 来自同一父节点、偏移分别是 0 和 `size`、且父节点最后一维恰好是 `2*size`（保证融合 kernel 从中点切分等价于两个 narrow）。

5. **运行并 dump 图**（需要 GPU 与可下载的小模型）：

   ```bash
   cd examples/auto_deploy
   AD_DUMP_GRAPHS_DIR=/tmp/ad_graphs python build_and_run_ad.py \
       --model "TinyLlama/TinyLlama-1.1B-Chat-v1.0" \
       --args.runtime demollm --args.skip-loading-weights False
   ```

   然后在 `/tmp/ad_graphs/` 下找到 `*_fuse_silu_mul_*.py`（变换「之前」）和紧随其后的下一个变换的 dump（变换「之后」），用 `grep` 搜 `silu_and_mul` 与 `aten.mul`，验证 mul 节点确实被替换。

**需要观察的现象**：在 `fuse_silu_mul` 之前的图 dump 里应能看到 `aten.silu.default` + `aten.mul.Tensor` + `aten.narrow` 的组合；之后的 dump 里这部分变成单个 `auto_deploy.flashinfer_silu_and_mul.default`。日志里 `[SUMMARY] matches=N` 行会告诉你这个变换在整个模型里匹配并融合了多少处。

**预期结果**：TinyLlama 每一层 FFN 有 1 处 gate/up，共 `num_hidden_layers` 处匹配，所以 `matches=` 应等于隐藏层数（TinyLlama-1.1B 是 22 层）。

**说明**：若本机无 GPU，步骤 1–4 的纯源码阅读完全可做，是本实践的核心；步骤 5 标注为「待本地验证」。即使不跑，你也可以在 `tests/` 下搜 `fuse_silu_mul` 的单测来验证上述断言（见第 5 节综合实践）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `fuse_silu_mul` 必须在 `stage: post_load_fusion`（装权重之后），而不能在 `pattern_matcher`（标准化阶段）就做？
**参考答案**：因为 `silu_and_mul` 融合算子要拿到真实的 shape 元信息（`node.meta["val"]`），而且 GEMM 融合（把 gate/up 投影合成单个 GEMM）要先发生、产生 `narrow` 切片模式，`fuse_silu_mul` 才有东西可匹配。GEMM 融合本身需要真实权重布局，所以整个链条必须在 `weight_load` 之后。`pattern_matcher` 阶段模型还在 meta 设备、且尚未做 GEMM 融合，此时图里还没有可匹配的 narrow+silu+mul 模式。

**练习 2**：如果用户想完全关掉 `fuse_silu_mul`，应该怎么改？改完会影响什么？
**参考答案**：通过 `yaml_extra` 覆盖 `transforms.fuse_silu_mul.enabled: false`（或直接改一份自定义 YAML）。影响是：FFN 里 `silu(gate)*up` 不再融合成单算子，会多出若干小 kernel 启动和一次显存往返，前向略慢；但数值结果不变（融合是等价改写）。这体现了「变换可插拔」的设计。

### 4.3 piecewise 编译：CompileBackend 与分片 CUDA Graph

#### 4.3.1 概念说明

流水线的最后一个阶段 `COMPILE` 负责把优化好的图「编译」成高效可执行的模块。AutoDeploy 用一个**编译后端注册表** `CompileBackendRegistry` 管理多种编译策略，默认提供四种：

| 后端名 | 做什么 | 适用场景 |
|--------|--------|----------|
| `torch-simple` | 不做编译，直接返回原模块 | 调试、最简基线 |
| `torch-compile` | 仅 `torch.compile(model, dynamic=True)` | 只要 torch.compile 的图优化、不要 CUDA Graph |
| `torch-cudagraph` | `torch.compile` + CUDA Graph（含 piecewise） | **默认**，生产路径，兼顾 prefill 与 decode |
| `torch-opt` | 更激进的优化后端 | 实验性 |

`torch-cudagraph`（默认）内部其实是**双模式**：decode-only 的 batch 用「整模型录一张大图（monolithic）」最快；prefill / 混合 batch 因为 token 数大且动态，用「分片（piecewise）CUDA Graph」。这个分流由一个 `DualModeCapturedGraph` 包装器在运行时根据 batch 性质自动派发（[torch_cudagraph.py:15-24](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/compile/backends/torch_cudagraph.py#L15-L24) 的模块 docstring 有明确说明）。

piecewise 的难点在于「地址稳定性」：CUDA Graph 录制时记住了所有输入/输出张量的 GPU 地址，回放时这些地址必须不变。但动态算子（注意力、mamba 元数据准备等）每次跑都会 `torch.empty` 新分配张量，地址漂移会破坏图。`ADPiecewiseRunner` 用一套「在录图时把动态算子的输出缓冲也一并从共享 graph pool 分配出来、运行时复用固定地址」的机制解决它——这就是 [piecewise_runner.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/compile/piecewise_runner.py) 文件顶部那段长 docstring 讲的全部内容。

#### 4.3.2 核心流程

`compile_model` 变换如何把模型切成段并分别录图：

```text
COMPILE 阶段：compile_model 变换（compile_model.py）
  │
  ├─ 读 CompileModelConfig：backend、piecewise_enabled、piecewise_num_tokens
  │    （num_tokens 档位未指定时，自动生成 [64,128,256,...,max_num_tokens]）
  │
  ├─ 从 CompileBackendRegistry.get(backend) 取后端类（如 TorchCudagraph）
  │
  └─ piecewise_enabled=True 时：
       1. split_graph_at_dynamic_ops：在动态算子（注意力等）处把模型切成多个子图
            ── 动态段（含注意力）→ 保持 eager，运行时每次真算
            ── 静态段（GEMM/Norm 等）→ 用 ADPiecewiseRunner 包起来
       2. 对每个 ADPiecewiseRunner，按每个 num_tokens 档位跑 warmup→capture：
            WARMUP : 多次 eager 跑子模块，让分配器状态稳定、发现动态算子输出 shape
            CAPTURE: torch.cuda.graph(...) 录图；同时把下游动态算子的输出缓冲
                     从共享 graph pool 分配，得到「确定性地址」
            REPLAY : 运行时把实际 token 数 padding 到档位，replay 录好的图；
                     动态算子从预分配缓冲拿 out=
       3. 返回 DualModeCapturedGraph：decode-only → monolithic，否则 → piecewise
```

「强制 padding 到档位」是 piecewise 驯服动态形状的关键：录图只录了 64/128/256/... 这些档位，运行时若 batch 是 100 个 token，就 pad 到 128 再 replay，多算的 28 个 token 的结果事后丢弃。代价是最多 2 倍的算力浪费，换来的是 CUDA Graph 的极低启动开销。档位用 2 的幂是为了让 padding 倍数最多 2×。

动态形状与 padding 的关系可这样理解：若实际 token 数为 \(n\)，档位集合为 \(B=\{64,128,256,...\}\)，则回放档位

\[
b(n)=\min\{b\in B \mid b\ge n\},
\]

浪费比为 \(\frac{b(n)-n}{n}\)，上界为 \(1\)（即最多翻倍）。

#### 4.3.3 源码精读

**编译后端注册表**：[compiler.py:29-48](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/compile/compiler.py#L29-L48) 的 `CompileBackendRegistry` 与变换注册表同构：`register(backend)` 装饰器把后端类存进 `_backend_registry`。抽象基类 [compiler.py:51-57](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/compile/compiler.py#L51-L57) 的 `CompilerBackend` 只要求实现 `compile() -> nn.Module`。后端的注册同样是 import 触发，见 [compile/backends/\_\_init\_\_.py:15](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/compile/backends/__init__.py#L15)，一行 import 四个后端模块，副作用就是把这四个后端全部注册。

**最简后端 torch-compile**：[torch_compile.py:24-32](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/compile/backends/torch_compile.py#L24-L32) 的 `TorchCompileCompiler.compile` 只有 `return torch.compile(self.model, dynamic=True)` 一行。读它能帮你建立「编译后端 = 拿到一个 nn.Module、还回一个 nn.Module」的最小心智模型——所有后端的契约都是这个。

**compile_model 变换**：[compile_model.py:67-103](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/transform/library/compile_model.py#L67-L103) 的 `CompileModelConfig` 定义了 `backend`（四种之一）、`piecewise_enabled`、`piecewise_num_tokens` 等字段，并用 `model_validator` 强制「piecewise 只能配 torch-cudagraph 或 torch-opt」。[compile_model.py:43-64](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/transform/library/compile_model.py#L43-L64) 的 `_generate_default_piecewise_num_tokens` 实现了「64 起步、2 的幂递增、直到 max_num_tokens」的自动档位生成。[compile_model.py:116-237](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/transform/library/compile_model.py#L116-L237) 的 `_apply_to_full_model` 是真正的编译入口：piecewise 开启时，它遍历模块树收集所有顶层 `GraphModule`（`compile_targets`），对每个 GM 调 `_compile_one`——`CompileBackendRegistry.get(self.config.backend)(target, **backend_kwargs).compile()`。

**单段录图器 ADPiecewiseRunner**：这是 piecewise 的灵魂。[piecewise_runner.py:272-287](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/compile/piecewise_runner.py#L272-L287) 类 docstring 说明它的行为由两个**类级**上下文控制——`_current_phase`（warmup/capture/replay）与 `_current_num_tokens`（用哪个档位），这两个上下文由编排器（`PiecewiseCapturedGraph`）在每次 forward 前设置。注意这是「类级」变量，意味着同一时刻所有 runner 共享同一个相位——这是靠编排器统一驱动的。

核心 [piecewise_runner.py:381-455](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/compile/piecewise_runner.py#L381-L455) 的 `forward` 三段式：

- **WARMUP**（[389-390](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/compile/piecewise_runner.py#L389-L390)）：直接 eager 跑 `self.submodule`，目的是让 PyTorch 分配器状态稳定，并让编排器通过 shape 发现拿到下游动态算子的输出 shape（存进 `_next_dynamic_out_infos`）。
- **CAPTURE**（[398-426](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/compile/piecewise_runner.py#L398-L426)）：`torch.cuda.graph(graph, pool=self._graph_pool)` 录图；关键是在录图上下文里**额外**为下游动态算子 `torch.empty(info.shape, ...)` 预分配输出缓冲——这些缓冲从共享 graph pool 拿地址，于是回放时地址固定。
- **REPLAY**（[428-455](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/compile/piecewise_runner.py#L428-L455)）：若该档位录过图，直接 `entry.cuda_graph.replay()`；若没录过（运行时 token 数不在档位里），则回退 eager（`self.submodule(...)`）。它还做了一次输入地址校验（[432-452](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/compile/piecewise_runner.py#L432-L452)），若地址与录图时不符会打 ERROR 日志——这是排查「图失效」的关键信号。

**动态算子包装器**：[piecewise_runner.py:466-500](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/compile/piecewise_runner.py#L466-L500) 的 `DynamicOpWrapper` 包住那些非 in-place 的动态算子，在 capture/replay 阶段从上游 runner 那里取回预分配缓冲、作为 `out=` 传进去，从而保证动态算子输出落在固定地址。文件里的 `MetadataWrapper` 则专门处理 mamba 这类「元数据准备」算子（输出虽小但地址会漂移），用「capture 时克隆稳定缓冲、replay 时 `copy_`」的方式保地址。这两个包装器共同保证了「动态段 eager、静态段录图」的混合执行不出错。

#### 4.3.4 代码实践

**实践目标**：理解四种编译后端的差异，并能根据日志判断一次运行是否真的走了 piecewise。

**操作步骤**：

1. **静态对比四个后端**。读 [torch_compile.py:30-32](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/compile/backends/torch_compile.py#L30-L32) 与 [torch_cudagraph.py:15-24](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/compile/backends/torch_cudagraph.py#L15-L24)，写一句话概括两者的差别（提示：一个只 compile、一个 compile+录图+piecewise）。

2. **追配置链**。在 [default.yaml:340-346](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/config/default.yaml#L340-L346) 里确认 `compile_model` 默认 `backend: torch-cudagraph`、`piecewise_enabled: true`、`piecewise_num_tokens: null`（即自动生成档位）。再读 [compile_model.py:147-164](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/compile/library/../../compile/library/compile_model.py#L147-L164)（注：实际路径为 `compile_model.py`）确认 `piecewise_num_tokens: null` 时会调 `_generate_default_piecewise_num_tokens(max_num_tokens)` 自动生成档位，并会 drop 掉 < 3 与超过 mixed-batch 容量的档位。

3. **运行并看日志**（需要 GPU）。用 `TLLM_LOG_LEVEL_BY_MODULE` 打开 AD 的 info 日志（见 AGENTS.md 的模块级日志说明），观察编译阶段是否打印类似 `Auto-generated piecewise_num_tokens from max_num_tokens=...: [64, 128, ...]` 与 `CompileModel: compiling N GraphModule(s)` 的行。

**需要观察的现象**：日志里应能看到自动生成的档位列表、被编译的 GraphModule 数量，以及 capture 阶段的耗时。若 `piecewise_enabled=false`（比如改用 `torch-compile` 后端），则不会出现 piecewise 档位生成与分段编译的日志。

**预期结果**：默认配置下，AD 会为模型的每个 transformer 层的静态段按若干档位录图；运行时 decode（小 batch）走 monolithic，prefill（大 batch）走 piecewise。

**说明**：步骤 1–2 是纯源码阅读，必做；步骤 3 标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ADPiecewiseRunner` 用「类级」上下文变量（`_current_phase`、`_current_num_tokens`）而不是把 phase 当参数传进 `forward`？
**参考答案**：因为 piecewise 模型是「静态段（runner）与动态段（DynamicOpWrapper）交错」嵌套在一个 `nn.Module` 树里，运行时是通过普通的 `module(*args)` 调用链触发的，没有额外通道把 phase 一层层传下去。用类级变量相当于一个「全局相位开关」，编排器（`PiecewiseCapturedGraph`）在整次 forward 前设好相位，所有 runner 与 wrapper 在各自的 forward 里读同一个相位，从而协同走 warmup/capture/replay。代价是不能并发跑多相位，但推理单步本来就是串行驱动的。

**练习 2**：运行时如果一个 batch 的 token 数恰好落在两个档位之间（比如档位是 [64,128]，实际是 100），会发生什么？会不会崩？
**参考答案**：不会崩。`ADPiecewiseRunner.forward` 在 replay 分支里，若 `entries.get(num_tokens)` 命中则 replay，否则在 capture 阶段会为新档位建条目录、replay 阶段若该档位未录过则回退 eager（`return self.submodule(...)`，见 [piecewise_runner.py:429-430](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/compile/piecewise_runner.py#L429-L430)）。实际工程上，编排器会在调用前把 token 数 padding 到档位（compile_model 的逻辑保证档位覆盖典型大小），所以多数情况是命中档位 replay；真正的未命中兜底是 eager，保证正确性。

## 5. 综合实践

设计一个贯穿本讲三块内容的「端到端追踪」任务：**用一份自定义 YAML 关掉若干变换并观察行为差异，从而把「shim → 流水线 → 编译」串起来。**

**任务**：写一份最小 YAML 覆盖文件 `my_override.yaml`，内容如下（示例代码，仅用于演示 yaml_extra 机制）：

```yaml
# 示例代码：关掉 silu 融合与 piecewise，仅作演示
transforms:
  fuse_silu_mul:
    enabled: false
  compile_model:
    backend: torch-compile
    piecewise_enabled: false
```

然后用 `yaml_extra` 加载它跑 dry-run：

```bash
cd examples/auto_deploy
python build_and_run_ad.py \
  --model "TinyLlama/TinyLlama-1.1B-Chat-v1.0" \
  --args.yaml-extra my_override.yaml \
  --dry-run
```

完成后做三件事：

1. **入口侧（4.1）**：从 dry-run 输出确认 `args.backend`（或等价的运行时字段）表明走的是 AD；在 `create_autodeploy_executor` 里指出 `model_engine` 是 `ADEngine`。
2. **流水线侧（4.2）**：对比覆盖前后 dump 的 `args.transforms`，确认 `fuse_silu_mul.enabled` 变成 `false`；在 [optimizer.py:111-115](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/transform/optimizer.py#L111-L115) 指出变换实例化与调用的那两行，说明「关掉一个变换 = 不实例化它」。
3. **编译侧（4.3）**：确认覆盖后 `compile_model.backend` 是 `torch-compile`、`piecewise_enabled` 是 `false`；在 [compile_model.py:97-103](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/transform/library/compile_model.py#L97-L103) 指出 `model_validator` 为何不再报错（因为 piecewise 关掉后 backend 不再受限）。

**无 GPU 的替代方案（源码阅读型）**：在仓库的 `tests/` 目录下搜 `fuse_silu_mul` 与 `compile_model` 相关单测（如 `tests/unittest/_torch/auto_deploy/` 下），阅读测试断言：它们通常构造一个包含 `narrow+silu+mul` 的小 GraphModule，跑一次变换，断言图里出现了 `silu_and_mul`、原 mul 消失。把这些断言当作「ground truth」写进你的笔记，效果等同于亲眼 dump 图。

## 6. 本讲小结

- AutoDeploy 是 Beta 后端，**入口与运行时都复用默认后端**：`LLM`（[llm.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/llm.py)）继承 `_TorchLLM`，`ADEngine` 实现 `ModelEngine` 后塞进同一个 `PyExecutor`；它替换的不是运行时，而是「模型模块的生产方式」。
- AD 的模型生产走一条**九阶段图变换流水线**（FACTORY → ... → COMPILE），由 `InferenceOptimizer` 按 `Stages` 排序编排，配置来自 `default.yaml`，可用 `yaml_extra` 覆盖。
- 图变换遵循统一契约 `BaseTransform`（模板方法 `__call__` + 子类 `_apply`），用 `@TransformRegistry.register` + import 即注册；`fuse_silu_mul` 是典型范例：检测 `narrow+silu+mul` 子图、替换为融合算子。
- 编译阶段用 `CompileBackendRegistry` 选后端，默认 `torch-cudagraph`；piecewise CUDA Graph 在动态算子处切图、静态段按 token 档位录图、运行时强制 padding 到档位回放，由 `ADPiecewiseRunner` 用「类级 phase 上下文 + 预分配输出缓冲」保证地址稳定。
- 三大调试工具：`build_and_run_ad.py --dry-run` 看最终配置、`AD_DUMP_GRAPHS_DIR` dump 每个变换后的图、`TLLM_LOG_LEVEL_BY_MODULE` 打开 AD 模块日志。
- 心智模型一句话：**默认后端是「人工写模型 + 直接跑」，AutoDeploy 是「拿现成模型 + 自动改图 + 编译再跑」**，二者殊途同归于同一个 `PyExecutor`。

## 7. 下一步学习建议

- **u12-l2 自定义算子与内核**：本讲反复出现的 `silu_and_mul`、`trtllm_quant_fp8_linear` 等都是挂在 `torch.ops.auto_deploy` 命名空间下的自定义算子（见 [fuse_silu_mul.py:51](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/auto_deploy/transform/library/fuse_silu_mul.py#L51) 的 import），下一讲会讲这些算子的四类实现来源（cpp/torch/triton/cute-dsl）与添加流程。
- **若想深入 AD 图变换**：阅读 `transform/library/` 下其它变换，推荐顺序 `load_weights.py`（权重装载）→ `sharding.py` / `sharding_ir.py`（并行切分，呼应 u9-l1）→ `kvcache.py`（KV cache 注入，呼应 u7-l1）→ `fused_moe.py`（MoE 融合，呼应 u10-l1）。
- **若想深入 piecewise**：读 `compile/piecewise_utils.py` 的 `split_graph_at_dynamic_ops`（切图策略）与 `compile/backends/torch_cudagraph.py` 的 `DualModeCapturedGraph`（decode/prefill 分流），并结合 u10-l4 的 CUDA Graph 知识对照。
- **官方文档**：`docs/source/features/auto_deploy/` 下有 `transforms/`（按类别的变换文档）、`advanced/workflow.md`（嵌入自家工作流）、`advanced/example_run.md`（运行参数表）、`pipeline_cache_design.md`（变换缓存），是本讲之外最成体系的进阶资料。
