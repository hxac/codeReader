# 图编译：ACL Graph 与 GE 后端

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 vLLM 的 `cuda_graph` 概念如何在昇腾上映射为 ACL Graph：`CUDAGraphMode` 枚举原封不动复用，捕获/重放由 `torch.npu.NPUGraph` 完成。
2. 读懂 `ACLGraphWrapper` 的捕获-缓存-重放三段式工作流，以及它如何在重放后用旁路流更新注意力算子的动态参数（graph task 更新机制）。
3. 说出两套「编译」路线的差异：ACL Graph 捕获（运行时录图）与 GE 全图编译（`TORCH_COMPILE_GE` + torchair + gear 档位），以及 `NpuGraphExAdaptor` 编译后端和 `GraphPassManager` 在链路中的位置。
4. 理解 `NPUGraphDispatcher` 如何把一个真实的 batch（token 数、请求）翻译成「图模式 + BatchDescriptor」的分发决策。
5. 对比 `--enforce-eager` 与图模式的性能特征：启动时间、显存、decode 延迟，并能设计实验验证。

本讲承接 u2-l2（NPUPlatform 配置改写）与 u2-l3（NPUModelRunner 生命周期）。在 u2-l3 中我们曾留下伏笔：`NPUModelRunner` 会把模型包进 `ACLGraphWrapper`、`capture_model` 会消费重捕获标志。本讲就把这条线讲透。

## 2. 前置知识

### 2.1 为什么需要「图」

深度学习推理的每次前向，本质是几百上千个算子（kernel）在设备上依次执行。若每个算子都由 CPU 逐个下发（launch），那么算子间隙会被「CPU 下发开销」填满。对 decode 这种每步只算一两个 token、单算子极小的场景，下发开销甚至可能超过算子本身执行时间。

**静态图捕获（Graph Capture）** 解决这个问题：第一次执行时把整段前向「录制」成一张设备端图，之后每步只需一条「重放（replay）」指令，设备端自己按图执行，CPU 几乎不再参与。

- NVIDIA 上的技术叫 **CUDA Graph**，PyTorch 接口是 `torch.cuda.CUDAGraph`。
- 昇腾上的对应技术叫 **ACL Graph**，torch_npu 提供同名接口 `torch.npu.NPUGraph`。

vLLM 的代码以 CUDA 术语为中心（配置项叫 `cudagraph_mode`、类叫 `CUDAGraphOptions`），omni-npu 的做法是**复用 vLLM 的全部概念和枚举，只把底层实现换成 NPU**。所以你在本讲会看到大量 `cudagraph` 字样的 NPU 代码——它们操作的其实是 ACL Graph。

### 2.2 vLLM 的两个关键抽象

- **`CUDAGraphMode`（枚举）**：`NONE`（纯 eager）、`PIECEWISE`（分段图：把模型切成若干段子图，注意力等动态部分留在段外）、`FULL`（整模型一张图）、`FULL_DECODE_ONLY`（仅 decode 用整图）。本部署的 decode 侧用的是 `FULL`。
- **`BatchDescriptor`（批描述符）**：一个 hashable 的 key，包含 `num_tokens`（本步 token 数）、`num_reqs`（请求数）、`uniform`（是否均匀 decode：每请求 query 长度相同）、`has_lora`。一张静态图只对固定的 `BatchDescriptor` 有效，所以它天然是「图的缓存键」。

### 2.3 torch.compile / dynamo / FX 与 GE

「捕获」之外还有另一条优化路线：**编译**。用 `torch.compile`（底层 dynamo）把 Python 前向追踪成 FX 图（一张算子级别的中间表示），再交给后端做算子融合、内存规划、甚至整体编译成设备执行文件。昇腾的 **GE（Graph Engine）** 就是这样一个后端，华为的 `torchair` 库提供 `torchair.get_npu_backend()` 把 FX 图交给 GE 编译。omni-npu 通过环境变量 `TORCH_COMPILE_GE=true` 启用这条路线（源码中称为 `use_gegraph`）。

> 术语速查：**eager** = 逐算子即时执行；**捕获（capture）** = 运行时录图；**编译（compile）** = 离线把 FX 图交给编译器。三者的关系是本讲主线。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `components/omni-npu/src/omni_npu/compilation/acl_graph.py` | ACLGraphWrapper：ACL Graph 捕获/重放核心，以及 graph task 更新机制 |
| `components/omni-npu/src/omni_npu/compilation/utils.py` | 注意力算子的 `OpDescriptor` 定义与 `capture_graph_task`（录图时登记注意力任务） |
| `components/omni-npu/src/omni_npu/compilation/npugraph_ex.py` | `NpuGraphExAdaptor`：vLLM 编译后端适配器，把 FX 图交给 npugraph_ex 编译 |
| `components/omni-npu/src/omni_npu/compilation/npugraph_ex_config.py` | `--additional-config` 中 `npugraph_ex_config` 的全局配置单例 |
| `components/omni-npu/src/omni_npu/compilation/pass_manager.py` | `GraphPassManager`：NPU 图优化 pass 的注册与执行 |
| `components/omni-npu/src/omni_npu/compilation/ge_compile_config.py` | `NPUCompilationConfig`：GE 路线的配置（gear 档位、缓存、backend） |
| `components/omni-npu/src/omni_npu/compilation/ge_wrapper.py` | GE 路线的模型包装器：输入 padding、按档位分发到编译图 |
| `components/omni-npu/src/omni_npu/compilation/decorators.py` | `patch_compile_decorators`：按 `TORCH_COMPILE_GE` 二选一地改写 vLLM 编译装饰器 |
| `components/omni-npu/src/omni_npu/worker/npu_graph_dispatcher.py` | `NPUGraphDispatcher`：batch → 图模式 + BatchDescriptor 的分发决策 |
| `components/omni-npu/src/omni_npu/worker/npu_model_runner.py` | 把上述部件组装进模型加载与捕获生命周期 |
| `components/omni-npu/src/omni_npu/platform.py` | 把各类路径声明给 vLLM（u2-l2 回顾） |
| `components/omni-npu/docs/compilation.md` | pass manager 的官方使用文档（如何新增图优化 pass） |
| `tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml` | 生产模板：P 侧 `--enforce-eager`、D 侧 FULL 图模式的真实参数 |

## 4. 核心概念与源码讲解

### 4.1 静态图捕获：ACLGraphWrapper

#### 4.1.1 概念说明

`ACLGraphWrapper` 是 omni-npu 对 vLLM「静态图包装器」的 NPU 实现。它包住模型的可执行体（`runnable`），对外仍像一个可调用对象（通过 `__getattr__` 透传属性），内部按需做三件事：

1. **直通**：当前不需要图（profile run、warmup、或运行模式不匹配）→ 原样调用 runnable。
2. **捕获**：这个 batch 形状第一次出现 → 创建 `NPUGraph`，在 `torch.npu.graph` 上下文里跑一遍前向完成录制，结果按 `BatchDescriptor` 缓存。
3. **重放**：这个 batch 形状已缓存 → 一句 `entry.aclgraph.replay()`，然后做一次 graph task 更新。

它解决的问题是：decode 阶段每步前向的算子小而多，CPU 下发开销占比高；录成整图后每步只有一次下发。代价是每个 batch 形状都要录一张图、占一份显存，所以 batch 形状需要被「对齐到有限档位」（见 4.4 的 padding 与 capture sizes）。

类文档字符串明确说明了设计取舍——wrapper **不**负责准备持久输入缓冲区，这件事由外部（ModelRunner 的 input buffers）完成，以此保持与编译逻辑正交：

[components/omni-npu/src/omni_npu/compilation/acl_graph.py:109-131](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/compilation/acl_graph.py#L109-L131)

这段文档（英文明注释）描述了工作流：初始化时赋予 FULL 或 PIECEWISE 之一；运行时从 forward context 拿到 `runtime_mode` 与 `batch_descriptor` 并「盲信」它们做分发。

#### 4.1.2 核心流程

一次 `__call__` 的决策流程（伪代码）：

```text
mode = forward_context.cudagraph_runtime_mode
if mode == NONE or mode != self.runtime_mode:
    return runnable(*args)            # 直通：profile/warmup/别的 wrapper 负责
entry = cache.get(batch_descriptor)   # 没有则新建
if entry.aclgraph is None or entry.recapture:
    （可选）先 eager 跑一遍为静态内核编译做准备
    graph = torch.npu.NPUGraph()
    with torch.npu.graph(graph, pool=共享图内存池):
        output = runnable(*args)      # 录制
    entry.output = weak_ref(output)   # 弱引用输出，防显存泄漏
    return output
else:
    断言输入地址与捕获时一致（仅 DEBUG 级别）
    entry.aclgraph.replay()           # 一条指令重放整图
    _update_graph_tasks(update_stream, forward_context)  # 旁路流刷新注意力动态参数
    return entry.output
```

关键的「cuda_graph ↔ acl_graph 映射」体现在三处：

- 类型：`torch.npu.NPUGraph()` 对应 `torch.cuda.CUDAGraph()`；
- 计数器：vLLM 的 `compilation_counter.num_cudagraph_captured` 原样累加；
- 模式枚举：`CUDAGraphMode.FULL/PIECEWISE/NONE` 原样比较。

**graph task 更新机制**是 NPU 特有的难点，值得单独讲。静态图有一个天然矛盾：注意力算子（如 `npu_fused_infer_attention_score`）的 `actual_seq_lengths`（每个请求的实际 KV 长度）每步都在变，但图是静态的。omni-npu 的解法是：捕获时把这些算子单独登记为 **graph task**（携带 handle 与 kwargs 快照），重放后在一条**旁路流**（`update_stream`）上用 `torch.npu.graph_task_update_begin/end` 包住重新下发的 `.out` 调用，只更新动态参数；时序由每任务一个 `ExternalEvent` 保证。这样主计算流不被阻塞，注意力元数据每步都能刷新。

#### 4.1.3 源码精读

**入口挂载。** vLLM 通过平台钩子拿到 wrapper 类路径，omni-npu 返回 ACLGraphWrapper：

[components/omni-npu/src/omni_npu/platform.py:206-211](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/platform.py#L206-L211)

`get_static_graph_wrapper_cls` 返回 `"omni_npu.compilation.acl_graph.ACLGraphWrapper"` 字符串（延迟 import，与 u2-l1 讲过的 entry point「模块：属性」机制同构）。同一文件里 `get_pass_manager_cls` 与 `get_compile_backend` 分别指向 4.2 的两个类（platform.py:213-225）。

**初始化。** 构造函数读取 vLLM 配置、共享图内存池，并从 `--additional-config` 的 `npugraph_ex_config` 里解析两个编译开关：

[components/omni-npu/src/omni_npu/compilation/acl_graph.py:134-177](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/compilation/acl_graph.py#L134-L177)

其中 [acl_graph.py:152-163](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/compilation/acl_graph.py#L152-L163) 说明 `super_kernel_optimize`（超内核优化）开启时自动连带 `static_kernel_compile`（静态内核编译）。图内存池来自 `current_platform.get_global_graph_pool()`，多张图共享一块池以省显存。

**模式分流。** `__call__` 开头判断当前运行模式：

[components/omni-npu/src/omni_npu/compilation/acl_graph.py:196-213](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/compilation/acl_graph.py#L196-L213)

`NONE` 意味着 profile run、warmup 或干脆没开图；模式不匹配则放行——这允许 FULL 与 PIECEWISE 两个 wrapper 嵌套时各自认领自己的调用。

**捕获主体。**

[components/omni-npu/src/omni_npu/compilation/acl_graph.py:241-288](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/compilation/acl_graph.py#L241-L288)

几个值得注意的细节：

- [L243-L253](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/compilation/acl_graph.py#L243-L253)：PIECEWISE 模式下每层都要录一张图，反复触发 Python GC 会让录制极慢，所以用 `unittest.mock.patch` 临时把 `gc.collect` 和 `torch.npu.empty_cache` 换成空函数（只对第一张图执行 GC）。
- [L256-L267](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/compilation/acl_graph.py#L256-L267)：`forward_context.capturing = True` 告知下游「正在录制」；输出转弱引用（`weak_ref_tensors`），因为重放只需要缓冲区指针，不需要保活 Python 对象（[L43-L75](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/compilation/acl_graph.py#L43-L75) 的 `weak_ref_tensor` 底层用 `torch.ops._C_ascend.weak_ref_tensor`）。
- [L286](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/compilation/acl_graph.py#L286)：`compilation_counter.num_cudagraph_captured += 1`——这就是启动日志里「捕获了多少张图」的计数来源。

**重放与 graph task 更新。**

[components/omni-npu/src/omni_npu/compilation/acl_graph.py:308-328](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/compilation/acl_graph.py#L308-L328)

DEBUG 日志级别下会校验重放时输入张量的 `data_ptr()` 与捕获时一致（静态图绑定的是地址，地址变了结果就错——这是排查图模式 bug 的第一手段）。随后 `_update_graph_tasks` 在旁路流上刷新注意力任务：

[components/omni-npu/src/omni_npu/compilation/acl_graph.py:330-379](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/compilation/acl_graph.py#L330-L379)

核心是 [L372-L378](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/compilation/acl_graph.py#L372-L378)：`graph_task_update_begin(handle)` → 用合并后的 kwargs 重新调用算子的 `.out` 变体 → `graph_task_update_end`，全程在 `update_stream` 上，结束时 `event.record` 建立与主流的时序依赖。

那么 task entry 是谁登记的？答案在注意力后端：u3-l2 讲过的稀疏注意力在图捕获期间调用 `capture_graph_task` 把 FIA（Fused Infer Attention）系列算子录进任务组：

[components/omni-npu/src/omni_npu/compilation/utils.py:204-249](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/compilation/utils.py#L204-L249)

它用 `torch.npu.graph_task_group_begin/end` 拿到 handle，连同 kwargs 快照、输出张量、`ExternalEvent` 一起存进 `GraphTaskEntry`。调用方分布在 `attention/backends/attention.py`、`mla.py` 与 `v1/layers/attention/npu_pangu.py`（可用 Grep 验证）。算子清单以 `OpDescriptor` 形式预定义：

[components/omni-npu/src/omni_npu/compilation/utils.py:113-170](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/compilation/utils.py#L113-L170)

`OP_FIA_V1/V2/SINK/PIONEER` 四个描述符分别对应不同版本的融合注意力算子，每个都带 `compute_dynamic_kwargs`——即「重放后如何从 attn_metadata 重新计算序列长度参数」的函数（try/except 兜底为 DUMMY，说明旧版 torch_npu 缺算子时优雅降级）。

**重捕获信号。** 权重热更新（如量化参数修改、弹性 EP 扩容）会让已录制的图失效。omni-npu 用一个模块级全局标志解决：

[components/omni-npu/src/omni_npu/compilation/acl_graph.py:27-40](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/compilation/acl_graph.py#L27-L40)

`set_aclgraph_recapture(True)` 的调用点在 MoE 层（`layers/fused_moe/layer.py:391`）、多个线性层（`v1/layers/linear.py` 六处）和 `npu_worker.py:440`——都是权重可能被原地改写的地方；`consume_aclgraph_recapture()` 在 `NPUModelRunner.capture_model` 里被消费（见 4.1.3 最后一段）。

**组装点。** `NPUModelRunner.load_model` 在 `cudagraph_mode` 含整图时完成包装：

[components/omni-npu/src/omni_npu/worker/npu_model_runner.py:679-696](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/worker/npu_model_runner.py#L679-L696)

注意 [L682](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/worker/npu_model_runner.py#L682) 的 `set_graph_params(cudagraph_capture_sizes)`——按捕获档位初始化 4.1 里的 task/workspaces 容器（[acl_graph.py:397-412](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/compilation/acl_graph.py#L397-L412)）；[L685-L688](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/worker/npu_model_runner.py#L685-L688) 收集注意力层名并排除 drafter（MTP）的层——主模型与 draft 层各自包一层 wrapper（[L697-L728](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/worker/npu_model_runner.py#L697-L728)，多个 MTP 层时按层号逐层包装，呼应 u3-l5）。

**捕获时机。** vLLM 生命周期里 `capture_model` 在 warmup 阶段执行：

[components/omni-npu/src/omni_npu/worker/npu_model_runner.py:759-769](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/worker/npu_model_runner.py#L759-L769)

GE 路线只做一次 dummy run；ACL 路线先消费重捕获标志并重置 input batch，再委托给 vLLM 原生流程（在 `switch_torch_device` 上下文里把 torch.cuda 临时换成 torch.npu，u2-l3 讲过这个手法）。

**部署侧实况。** 92B BF16 模板里，P 侧显式 `--enforce-eager`，D 侧走 FULL 图模式：

- [tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml:92](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L92)：P 侧 `EXTRA_ARGS` 含 `--enforce-eager`——prefill 输入形状多变（不同 prompt 长度），录图收益低、显存代价高，干脆 eager。
- [tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml:202](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L202)：D 侧 `--compilation-config {"level": 3, "cudagraph_mode":"FULL", "cudagraph_capture_sizes":[12], "backend":"", "compile_sizes":[12]}`——decode 批小而齐（`--max-num-seqs 3` × MTP 3 个 draft token + 1 = 12 token 的档位），是最典型的图模式受益者。
- [tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml:216](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L216)：D 侧 `--additional-config` 同时带 `npugraph_ex_config.enable=true`（4.2 详述）。

此外两侧都 `export TORCH_COMPILE_DISABLE=1`（模板 L86/L175）且未设置 `TORCH_COMPILE_GE`——即生产模板实际启用的是「ACL Graph 捕获 + npugraph_ex 编译后端」，GE 全图编译路线（4.3）是源码提供但模板未开启的能力。`TORCH_COMPILE_DISABLE` 与 `--compilation-config level 3` 同时出现的实际交互效果，建议本地从日志确认（见 4.4.4 实践第 5 步），此处标注**待本地验证**。

#### 4.1.4 代码实践

**实践目标**：从日志确认 ACL Graph 捕获真的发生了，并数出捕获了多少张图。

**操作步骤**（源码阅读型 + 可选运行型）：

1. 在部署机 D 节点容器内把日志级别调到 DEBUG（环境变量 `VLLM_LOGGING_LEVEL=DEBUG`，或临时改模板 decode 命令），重启 decode 服务。
2. 观察启动日志中以下三行是否出现（均来自本讲源码，可用 `grep` 定位）：
   - `Wrapped original model with ACLGraphWrapper`（npu_model_runner.py:696）
   - `Capturing a aclgraph on (FULL,...)`（acl_graph.py:230-231，需 `debug_log_enable`）
   - ACLGraphWrapper 被调用的 `<<< ACLGraphWrapper is being called.`（acl_graph.py:197）
3. 对照 `cudagraph_capture_sizes=[12]`，预测日志中 FULL 模式捕获的 batch descriptor 数量。
4. 可选进阶：在容器里 `python -c "import torch, torch_npu; print(hasattr(torch.npu, 'NPUGraph'))"` 验证运行时具备 ACL Graph 能力。

**需要观察的现象**：decode 侧 server_0.log 在 `Application startup complete` 之前出现捕获相关日志；prefill 侧（`--enforce-eager`）则完全没有。

**预期结果**：捕获档位与 `cudagraph_capture_sizes` 一一对应；每档一条捕获记录。实际条数与日志措辞**待本地验证**（`debug_log_enable` 默认关闭时，逐形状日志可能不打印，只有计数器可查）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ACLGraphWrapper.__call__` 在捕获分支返回 `output`（强引用），而缓存里存 `entry.output = weak_ref_tensors(output)`？

**答案**：注释（acl_graph.py:303-306）说明：返回强引用是为了让 PyTorch 在捕获期间正确管理内存生命周期；而缓存只需要缓冲区指针用于重放，弱引用让原始张量可被 GC 回收，避免每个 batch 档位都泄漏一份输出显存。

**练习 2**：如果重放时输入张量的地址与捕获时不同，会发生什么？代码如何帮你发现？

**答案**：静态图绑定的是捕获时的输入地址，地址不同会导致图读到旧缓冲区的数据，输出错误且通常无报错。代码在 `VLLM_LOGGING_LEVEL == "DEBUG"` 时（acl_graph.py:151、308-316）断言两次 `data_ptr()` 列表一致，不一致直接抛 AssertionError 指明 expected/got。

**练习 3**：MoE 层权重更新后为什么要调 `set_aclgraph_recapture(True)`？这个标志最终在哪里被消费？

**答案**：录好的 ACL Graph 内部已绑定旧权重缓冲区与算子配置，权重原地改写后图内容过期。全局标志置位后，`NPUModelRunner.capture_model`（npu_model_runner.py:765-767）调用 `consume_aclgraph_recapture()` 消费它，重置 input batch 并把所有缓存 entry 标记 `recapture=True`，下次调用时重新录制。

### 4.2 编译后端：NpuGraphExAdaptor 与 GraphPassManager

#### 4.2.1 概念说明

4.1 讲的是「运行时录图」。本模块讲「编译」：vLLM 允许平台注册一个**编译后端**（实现 `CompilerInterface`），在 torch.compile 的 FX 图产出后接管编译。omni-npu 提供两个部件：

- **`NpuGraphExAdaptor`**：把 FX 图交给华为 `npugraph_ex` 库编译（昇腾图编译器，带静态内核、超内核、算子融合等优化），所有开关从 `--additional-config` 的 `npugraph_ex_config` 读取。
- **`GraphPassManager`**：继承 vLLM 的 `PostGradPassManager`，在 FX 图上跑 NPU 专属优化 pass（如 `merge_dynamic_quant` 动态量化算子融合），同样由 `npugraph_ex_config` 里的开关控制。

两者解决的问题是：光录图只消除了下发开销，算子本身还是「有什么跑什么」；编译后端能在图层面做融合、去冗余、静态内核生成，进一步压缩单算子时延——对 decode 这种时延敏感场景是第二级加速。

#### 4.2.2 核心流程

整条链路（与 `docs/compilation.md` 一致）：

```text
vLLM 编译阶段
  ├─ NPUPlatform.get_pass_manager_cls() → GraphPassManager
  │      └─ vLLM 注入 post_grad_custom_post_pass
  ├─ NPUPlatform.get_compile_backend() → "npugraph_ex" 后端
  │      └─ NpuGraphExAdaptor.compile(graph, ...)
  │             ├─ npugraph_ex_config.enable=false → 原样返回 FX 图（不编译）
  │             ├─ 输出包一层 tuple（后端要求）
  │             ├─ 按 config 开关填充 npugraph_ex.CompilerConfig
  │             └─ npugraph_ex.get_npu_backend(config)(graph, example_inputs)
  └─ GraphPassManager(graph, example_inputs, config) 逐 pass 改写 FX 图
```

配置来源是一个进程级单例：`NPUModelRunner.__init__` 里 `init_aclgraph_config(vllm_config)`（[npu_model_runner.py:146-148](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/worker/npu_model_runner.py#L146-L148)）把 `additional_config` 缓存到 [npugraph_ex_config.py:10-36](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/compilation/npugraph_ex_config.py#L10-L36)，之后 adaptor 在编译时随时 `get_aclgraph_config()` 取用——这是「启动参数 → 深处编译代码」的传参通道，避免层层透传。

#### 4.2.3 源码精读

**后端声明。** 平台把 adaptor 的路径交给 vLLM：

[components/omni-npu/src/omni_npu/platform.py:220-225](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/platform.py#L220-L225)

**编译入口与开关总表。** `NpuGraphExAdaptor.compile` 是核心：

[components/omni-npu/src/omni_npu/compilation/npugraph_ex.py:34-58](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/compilation/npugraph_ex.py#L34-L58)

[L48-L49](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/compilation/npugraph_ex.py#L48-L49)：`enable` 未开时直接 `return graph, None`，即该后端默认是「透明」的。L52-L58 把单值输出改写为 tuple 输出，满足 npugraph_ex 的接口约定。

**开关到 CompilerConfig 的映射。**

[components/omni-npu/src/omni_npu/compilation/npugraph_ex.py:60-109](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/compilation/npugraph_ex.py#L60-L109)

可读出完整的旋钮清单（默认值取反即默认行为）：

| `npugraph_ex_config` 键 | 作用 | 默认 |
| --- | --- | --- |
| `enable` | 总开关，false 则该后端透传 | false |
| `static_kernel_compile` | 静态内核编译，适合形状稳定的场景 | false |
| `super_kernel_optimize` | 超内核优化（dcci 指令调度），附带开启静态内核 | false |
| `capture_limit` | 捕获档位数量上限 | 64 |
| `clone_input` / `clone_output` | 是否克隆输入/输出（正确性排查用） | true / false |
| `remove_noop_ops` | 消除冗余空算子 | true |
| `inplace_pass` / `input_inplace_pass` | 算子原地化改写 | true |
| `pattern_fusion_pass` | 模式匹配算子融合 | true |
| `frozen_parameter` | 冻结权重与计算图防动态变化 | false |

[L105-L109](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/compilation/npugraph_ex.py#L105-L109) 把 vLLM 传入的 `post_grad_custom_pre_pass / post_grad_custom_post_pass`（即 GraphPassManager）塞进 CompilerConfig——这就是 pass manager 进入 torchair/npugraph_ex 的入口。最后 [L123-L125](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/compilation/npugraph_ex.py#L123-L125) 用 `npugraph_ex.get_npu_backend` 完成编译并返回可调用图。

**GraphPassManager。**

[components/omni-npu/src/omni_npu/compilation/pass_manager.py:18-36](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/compilation/pass_manager.py#L18-L36)

`__call__` 的签名 `(graph, example_inputs, config) -> graph` 与 torchair 的 post-pass 契约对齐；它按 `get_pass_context().compile_range`（当前编译的形状区间）过滤适用 pass 后逐个执行、`recompile()` 生效。pass 的注册在 `configure()`：

[components/omni-npu/src/omni_npu/compilation/pass_manager.py:41-53](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/compilation/pass_manager.py#L41-L53)

目前仅 `merge_dynamic_quant` 一个实际注册的 pass（`enable_moe_multistream` 是预留空位）。新增 pass 的三步流程（新建 `VllmInductorPass` 子类 → 在 `configure()` 挂开关 → `--additional-config` 打开）官方文档写得很清楚：

[components/omni-npu/docs/compilation.md:23-43](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/docs/compilation.md#L23-L43)

**与 4.1 的联动。** `ACLGraphWrapper.__init__` 读取的 `super_kernel_optimize / static_kernel_compile`（acl_graph.py:152-163）与 adaptor 里的同名键（npugraph_ex.py:66-70）是同一份 `npugraph_ex_config`：编译后端负责生成静态/超内核，wrapper 负责在捕获前先跑一遍触发编译（acl_graph.py:232-235）、捕获后调用 `aclgraph.super_kernel_optimize(...)` 做 dcci 调度优化（[acl_graph.py:290-301](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/compilation/acl_graph.py#L290-L301)，其中正则列表点名了 `GroupedMatmul`、稀疏 FA 等算子——正是 u3-l2/u3-l3 讲过的 MoE 与 DSA 核心算子）。

#### 4.2.4 代码实践

**实践目标**：走通 docs/compilation.md 描述的「开关 → pass 注册」链路，验证配置确实能改变编译行为。

**操作步骤**：

1. 阅读 [components/omni-npu/docs/compilation.md:13-21](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/docs/compilation.md#L13-L21)，记下启用 `merge_dynamic_quant` 的启动参数写法。
2. 在测试环境的 decode 启动命令里，把 `--additional-config` 的 `npugraph_ex_config` 扩展为 `{"enable": true, "merge_dynamic_quant": true}`（其余键保持模板原值），重启服务。
3. 在日志里搜索 `static_kernel_compile:` / `remove_noop_ops:` 等 DEBUG 行（来自 npugraph_ex.py:111-121），核对每个开关的生效值。
4. 反向验证：把 `enable` 改为 `false` 重启，确认上述 DEBUG 行消失（后端透传）。

**需要观察的现象**：第 3 步与第 4 步的日志差异；服务功能（生成结果）不变。

**预期结果**：开关值与 `--additional-config` 逐项一致。`merge_dynamic_quant` 对端到端时延的影响方向与幅度**待本地验证**（该 pass 影响的是量化算子融合，u8 量化讲义后会更容易评估）。

#### 4.2.5 小练习与答案

**练习 1**：`npugraph_ex_config.enable=false` 时 `NpuGraphExAdaptor.compile` 返回什么？这意味着什么？

**答案**：返回 `(graph, None)`，即原 FX 图原样透传（npugraph_ex.py:48-49）。意味着该后端完全透明，编译链退回 vLLM 默认行为，所有 npugraph_ex 专属优化（静态内核、超内核、融合 pass）都不发生。

**练习 2**：`GraphPassManager` 的 pass 是从哪里知道「当前编译的是哪个形状区间」的？

**答案**：`get_pass_context().compile_range`（pass_manager.py:31），再由每个 pass 的 `is_applicable_for_range(compile_range)` 决定是否适用——这样同一 pass 可以只在 decode 尺寸区间启用而在 prefill 区间跳过。

**练习 3**：为什么 `super_kernel_optimize` 开启时 `static_kernel_compile` 必然为 true？

**答案**：acl_graph.py:160-163 的 `need_static_compile = need_super_kernel_optimize or ...static_kernel_compile`，npugraph_ex.py:66-70 同样处理。超内核优化是把多个内核合并成超大内核调度，前提是这些内核已完成静态编译，因此后者是前者的必要条件。

### 4.3 GE 编译配置：NPUCompilationConfig 与 ge_wrapper

#### 4.3.1 概念说明

第三条路线是 **GE 全图编译**（源码称 `use_gegraph`）：用 torchair 把整个模型前向编译成一张 GE 图，彻底绕过逐算子下发。它与 4.1 的关系是「二选一的实现路线」，开关是环境变量 `TORCH_COMPILE_GE=true`。它与 4.2 的区别在于接管层次：npugraph_ex 是 vLLM 编译后端（作用于 dynamo 切出的 FX 图），GE 路线则改写 vLLM 的模型装饰器、让整个模型类以 torchair 编译产物运行。

GE 图要求**静态 shape**，而 decode 的 batch 每步都在变。解法是 **gear（档位）**：预先选定有限个 batch 档位（`decode_gear_list`，最多 6 档），每档编译/缓存一张图；真实 batch 通过 **padding** 补齐到档位。这正是 [ge_compile_config.py:19](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/compilation/ge_compile_config.py#L19) `MAX_GEAR_NUM = 6` 的含义——档位越多显存与编译时间开销越大，6 是当前权衡的上限。

#### 4.3.2 核心流程

```text
启动
  ├─ NPUPlatform.check_and_update_config
  │     └─ ConfigUpdater.update_vllm_config：
  │          vllm_config.npu_compilation_config = NPUCompilationConfig()
  │          use_gegraph = (env TORCH_COMPILE_GE == "true")
  │          additional_config["graph_model_compile_config"] → build_from_cli
  ├─ NPUPlatform.import_kernels → patch_compile_decorators()
  │     ├─ use_gegraph=true  → 把 vLLM 的 _support_torch_compile 换成 support_ge_compile
  │     └─ use_gegraph=false → 打 piecewise/mark_dynamic 补丁（走 4.1 路线）
加载模型
  ├─ use_gegraph=true：NPUModelRunner.load_model 走 original_get_model（模型类已被
  │   support_ge_compile 注入 TorchNpuCompilerWrapperWithCustomDispatcher 基类）
  └─ use_gegraph=false：走 vLLM 正常流程 + ACLGraphWrapper 包装（4.1）
每步前向（GE 路线）
  __call__ → prefill 或非 uniform？→ 原生 forward（eager）
           → 否则按 gear padding → dispatch 到 compiled_model 或 cached_compiled_models[gear]
```

#### 4.3.3 源码精读

**配置对象与解析。**

[components/omni-npu/src/omni_npu/platform.py:30-41](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/platform.py#L30-L41)

vLLM 配置对象上被挂了一个 NPU 专属属性 `npu_compilation_config`；`use_gegraph` 只由环境变量决定，`graph_model_compile_config`（来自 `--additional-config`）则携带 backend、缓存与档位设置。

字段与解析：

[components/omni-npu/src/omni_npu/compilation/ge_compile_config.py:33-64](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/compilation/ge_compile_config.py#L33-L64)

**档位推导。** `update_gear_options` 是本模块最有业务含金量的函数：

[components/omni-npu/src/omni_npu/compilation/ge_compile_config.py:66-87](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/compilation/ge_compile_config.py#L66-L87)

三个规则：① 档数超过 6 直接报错；② 超过最大档位的档被裁掉（`max_gear_size` 默认取 `max_num_batched_tokens`，但 **kv_consumer（decode 侧）或 `enable_hybrid_graph_mode` 时改取 `max_num_seqs`**（含投机解码则乘 `1 + num_speculative_tokens`），L71-L72——这正对应部署模板里 D 侧 `--max-num-seqs 3` + MTP 3 的 12 token 档位来源）；③ 空则兜底为 `[max_gear_size]`，且不足 6 档时自动补一档最大值。

**torchair 后端。**

[components/omni-npu/src/omni_npu/compilation/ge_compile_config.py:23-30](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/compilation/ge_compile_config.py#L23-L30)

`get_torchair_config` 打 `patch_for_hcom()`（让 HCCL 集合通信可被 GE 编译）、开 tiling 调度优化、默认冻结权重参数，并 `set_compile_mode(jit_compile=False)`（关闭即时编译）。`init_backend`（[L89-L100](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/compilation/ge_compile_config.py#L89-L100)）在未指定自定义 backend 时返回 `torchair.get_npu_backend(compiler_config=config)`。

**装饰器改写。** GE 路线的「侵入点」是 vLLM 给模型类打编译装饰器的地方：

[components/omni-npu/src/omni_npu/compilation/decorators.py:158-177](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/compilation/decorators.py#L158-L177)

`patch_compile_decorators` 由 `NPUPlatform.import_kernels` 调用（[platform.py:82-88](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/platform.py#L82-L88)）。GE 分支把 `_support_torch_compile` 整个换成 `support_ge_compile`：

[components/omni-npu/src/omni_npu/compilation/decorators.py:23-49](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/compilation/decorators.py#L23-L49)

手法是**动态注入基类**（`cls.__bases__ += (TorchNpuCompilerWrapperWithCustomDispatcher,)`）并替换 `__init__`/`__call__`——与 u2-l4 的 monkey patch、u3-l3 的 `@register_oot` 一脉相承。非 GE 分支则打 piecewise 回退补丁（未知形状动态创建 range entry）与 `maybe_mark_dynamic` 补丁，并包一层 `_wrap_call` 让 prefill 批次绕开编译图。

**GE 包装器。**

[components/omni-npu/src/omni_npu/compilation/ge_wrapper.py:106-146](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/compilation/ge_wrapper.py#L106-L146)

`do_not_compile = not use_gegraph`——即使被注入了基类，GE 未开启时包装器完全短路。`compile_dispatcher` 两种模式：普通模式 `torch.compile(forward, dynamic=False, fullgraph=True, backend=torchair后端)`；缓存模式（`use_ge_graph_cached`）对 `decode_gear_list` 的**每个档位**用 `torchair.inference.cache_compile` 生成一张缓存图——注意 L131-L137 用 `code.replace(co_name=...)` 动态克隆出「每档一个独立函数对象」，这是为了绕开 `cache_compile` 对同一函数的缓存冲突，是个值得学习的技巧。

**每步分发。**

[components/omni-npu/src/omni_npu/compilation/ge_wrapper.py:164-197](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/compilation/ge_wrapper.py#L164-L197)

`__call__` 先判断 prefill 或非 uniform decode → 直接 `self.forward`（eager）；否则按 TP 对齐或补到 `max_num_seqs` 做 `GE_graph_padding`（[L29-L103](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/compilation/ge_wrapper.py#L29-L103)：input_ids/positions/slot_mapping/block_table 全部 concat 补齐，padding 槽位用 `PAD_SLOT_ID` 防止污染 KV Cache），再按 `gear_size = inputs.shape[0]` 查表分发（[L148-L162](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/compilation/ge_wrapper.py#L148-L162)）。

**加载分支。** GE 开启时模型加载与捕获走捷径：

[components/omni-npu/src/omni_npu/worker/npu_model_runner.py:659-669](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/worker/npu_model_runner.py#L659-L669)（`original_get_model` 直接拿模型，不再叠 ACLGraphWrapper）

[components/omni-npu/src/omni_npu/worker/npu_model_runner.py:759-764](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/worker/npu_model_runner.py#L759-L764)（`capture_model` 退化为一次 `max_num_reqs` 的 dummy run，触发 GE 编译）

另外 [src/omni_npu/model_config/config_loader/loader.py:45](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/config_loader/loader.py#L45) 显示 u5-l1 讲过的最佳实践配置系统也感知 `use_gegraph`——GE 开启时走不同的配置分支（具体差异留作练习 3）。

#### 4.3.4 代码实践

**实践目标**：不改代码，仅凭源码推导「打开 GE 路线」需要的全部配置项，并核对生产模板为什么没开。

**操作步骤**：

1. 在纸上列出启用 GE 需要的三件事：`TORCH_COMPILE_GE=true` 环境变量、（可选）`--additional-config '{"graph_model_compile_config": {...}}'` 携带 backend/档位、模型类被 vLLM 编译装饰器装饰。
2. 用 Grep 在 `tools/ansible/` 下搜索 `TORCH_COMPILE_GE`，确认所有模板均未设置（即默认 false，GE 关闭）。
3. 再搜索 `graph_model_compile_config`，确认部署链路也未传递该配置。
4. 假设要为 decode 侧（`--max-num-seqs 3`、`--num-speculative-tokens 3`）手工构造档位：按 `update_gear_options` 的规则推导 `max_gear_size` 与最终 `decode_gear_list`。

**需要观察的现象**：这是一次纯推导练习；第 4 步的答案应能代入公式复算。

**预期结果**：`max_gear_size = max_num_seqs × (1 + num_speculative_tokens) = 3 × 4 = 12`；若用户传 `decode_gear_list=[4, 8]`，因不足 6 档且最大值 8 < 12，最终变成 `[4, 8, 12]`（自动补最大档）。GE 在真实 NPU 环境的端到端收益**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`decode_gear_list=[8, 16, 32, 64, 128, 256, 512]`（7 档）在 kv_consumer、`max_num_seqs=512` 的部署下会发生什么？

**答案**：先因档数 7 > `MAX_GEAR_NUM=6` 抛 `ValueError("Max gear num supported is 6 now.")`（ge_compile_config.py:74-75）。注意顺序：档数检查在裁剪之前，所以即使有档超限也会先报错。

**练习 2**：GE 路线里 prefill 请求为什么不走编译图？

**答案**：两处证据：`ge_wrapper.__call__` L185-L195 检测 `num_prefills > 0` 或非 uniform 即返回原生 `forward`；`decorators._bypass_prefill`（L52-65）注释说明 prefill 时 MoE 层会走 `torch.all_to_all_single` 等 CPU 参与的通信，无法被编译。prefill 形状多变，本就不是 GE 静态图的目标场景。

**练习 3**：`use_ge_graph_cached=true` 与 false 的行为差异是什么？为什么缓存模式要为每个档位克隆一个函数？

**答案**：false 时只有一个 `torch.compile` 的 `compiled_model`，靠 dynamo 的动态 shape 重编译适配不同 batch；true 时对每个 gear 用 `torchair.inference.cache_compile(ge_cache=True)` 预生成缓存图，运行时按 `args[0].shape[0]` 精确查表（L157-L159）。克隆函数是因为 `cache_compile` 以函数对象为缓存键，同一函数多个档位会互相覆盖，`co_name` 替换制造了独立键（L131-L137）。

### 4.4 图分发：NPUGraphDispatcher

#### 4.4.1 概念说明

前三个模块回答了「图怎么录、怎么编译」；本模块回答「每一步前向到底用不用图、用哪张图」。`NPUGraphDispatcher` 继承 vLLM 的 `CudagraphDispatcher`，负责把真实 batch（token 数、是否均匀 decode、是否带 LoRA）翻译成 `(CUDAGraphMode, BatchDescriptor)` 二元组。`NPUModelRunner` 在初始化时创建它（[npu_model_runner.py:200](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/worker/npu_model_runner.py#L200)），把结果写进 forward context，随后被 4.1 的 `ACLGraphWrapper.__call__` 消费——分发器与包装器一问一答，构成图模式的运行时闭环。

它要解决的核心矛盾是：**batch 形状空间无限，图缓存有限**。手段有二：padding（把 token 数补齐到档位）与 relax（放松 `num_reqs/uniform` 维度，让一张「混合批」图服务多种请求组合）。

#### 4.4.2 核心流程

```text
初始化（注意力后端就绪后）
  initialize_cudagraph_keys(mode, uniform_decode_query_len):
    对每个 capture_size × lora_case：
      padded = pad(num_tokens=bs)                       # 对齐档位
      relaxed = relax(padded)                           # num_reqs=min(max_seqs, tokens), uniform=False
      注册 mixed 模式 key(relaxed)
    若 decode 为 FULL 且独立例程：
      再为「均匀 decode」注册 FULL 模式 key(padded, uniform=True)

每步分发 dispatch(num_tokens, uniform_decode, has_lora):
  未初始化 / 模式 NONE / tokens > 最大档位 → (NONE, 原样 descriptor)   # 走 eager
  batch_desc = padded(num_tokens)
  relaxed    = relax(batch_desc)
  FULL 表里有 batch_desc   → (FULL, batch_desc)       # 精确命中整图
  FULL 表里有 relaxed      → (FULL, relaxed)          # 放松后命中整图
  PIECEWISE 表里有 relaxed → (PIECEWISE, relaxed)     # 分段图
  都没有                   → (NONE, 原样 descriptor)   # 走 eager
```

#### 4.4.3 源码精读

**padding 与 relax。**

[components/omni-npu/src/omni_npu/worker/npu_graph_dispatcher.py:15-47](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/worker/npu_graph_dispatcher.py#L15-L47)

`_create_padded_batch_descriptor` 用 vLLM 的 `pad_for_cudagraph` 把 token 数补到档位；均匀 decode（每请求 query 长度相同，如 MTP 的 k+1）时 `num_reqs = tokens / uniform_decode_query_len` 精确可算。`_relax_batch_descriptor_for_mixed_batch_cudagraphs` 是 NPU 的关键改动（注释 `# Adapt start/end` 包裹的正是相对上游的差异化代码）：把 `num_reqs` 压成 `min(max_num_seqs, num_tokens)`、`uniform` 强制 False——效果是**同一张混合图可以服务任意请求组合**，只要 token 数对齐，图缓存条目从「请求数 × token 数」坍缩为「token 数」，大幅减少捕获数量与显存。

**键的预注册。**

[components/omni-npu/src/omni_npu/worker/npu_graph_dispatcher.py:50-104](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/worker/npu_graph_dispatcher.py#L50-L104)

mixed 模式按 `cudagraph_capture_sizes × lora_cases` 全量预注册（relaxed key）；FULL decode 模式只保留 `uniform_decode_query_len ≤ size ≤ max_num_tokens` 的档位（L93-L97），因为均匀 decode 的 token 数必然是 `query_len × num_reqs` 的倍数。

**分发决策。**

[components/omni-npu/src/omni_npu/worker/npu_graph_dispatcher.py:106-147](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/worker/npu_graph_dispatcher.py#L106-L147)

注意兜底的三个 NONE 条件（L119-L124）：未初始化、整体模式 NONE（`--enforce-eager` 时即如此）、token 数超过最大档位（典型如长 prompt 的 prefill）——这解释了部署形态：**prefill 大批量天然落在 NONE 分支走 eager，decode 小批量命中 FULL 图**。函数文档（L113-L118）说明返回新 descriptor 的原因：均匀批可能被分派给支持更一般批的图。

**与 enforce-eager 的对照。** `--enforce-eager` 使 vLLM 的 `cudagraph_mode = NONE`，于是：分发器恒返回 NONE → wrapper 恒直通 runnable → 启动时无捕获（省显存、启动快）、每步前向全部逐算子下发（decode 时延高）。这就是本讲综合实践要量化的对比。

#### 4.4.4 代码实践

**实践目标**：手工模拟一次 dispatch，验证你能预测每步前向走 eager 还是图。

**操作步骤**：

1. 设定场景：`cudagraph_capture_sizes=[12]`、`max_num_seqs=3`、`uniform_decode_query_len=4`（MTP 3 + 1）、`max_cudagraph_capture_size=12`、无 LoRA、模式 FULL。
2. 依 `initialize_cudagraph_keys` 写出注册表：FULL 表应包含 `(num_tokens=12, num_reqs=3, uniform=True)`。
3. 对以下五个输入逐个调用你脑中的 `dispatch`，写下返回值：
   - a. decode 步：`num_tokens=12, uniform_decode=True`
   - b. decode 步：`num_tokens=10, uniform_decode=True`（有请求提前结束）
   - c. prefill 步：`num_tokens=5000, uniform_decode=False`
   - d. decode 步：`num_tokens=13, uniform_decode=True`（超配 1 个 token）
   - e. 刚启动 warmup：`keys_initialized=False`
4. 用源码 L119-L144 核对答案（注意 b 会被 `pad_for_cudagraph` 补到 12；d 超过 12 后 padding 也救不了——取决于 `pad_for_cudagraph` 的实现，此处需翻 vLLM 源码确认，标注**待确认**）。

**需要观察的现象**：纯桌面推演；若在真实服务上，可在 DEBUG 日志看 `Replaying aclgraph, batch_descriptor=...` 与 eager 直通的比例。

**预期结果**：a → `(FULL, (12,3,True))`；b → padding 后同 a；c → `(NONE, (5000,))`；d/e → NONE。

#### 4.4.5 小练习与答案

**练习 1**：为什么 relax 时 `uniform` 必须置 False、`num_reqs` 压到 `min(max_num_seqs, num_tokens)`？

**答案**：relax 的目的是让键「泛化」：把均匀性与精确请求数从键里抹掉后，任何 `num_tokens` 相同的批（无论 3 个请求还是 12 个请求、是否均匀）都能命中同一张混合图。代价是该图必须按最一般的情况编译（不能利用均匀性特化），所以精确 FULL 键（uniform=True）优先级更高。

**练习 2**：`dispatch` 的第一个参数为什么是 `num_tokens` 而不是请求数？

**答案**：图的静态形状由张量第一维（token 数）决定，`num_reqs/uniform` 只影响注意力的元数据（可由 graph task 更新机制在重放后刷新，见 4.1）。所以 token 数是硬约束（必须 padding 对齐），批结构是软约束（可以 relax）。

**练习 3**：`--enforce-eager` 从配置到行为要经过哪几站？

**答案**：vLLM 把 `cudagraph_mode` 置 NONE → `NPUModelRunner.load_model` 中 `has_full_cudagraphs()` 为 False，不创建 ACLGraphWrapper（npu_model_runner.py:681）→ 每步 `dispatch` 命中 NONE 分支（npu_graph_dispatcher.py:119-124）→ 即使存在 wrapper 也走直通（acl_graph.py:203-213）。三站任何一站都保证 eager 语义，形成双保险。

## 5. 综合实践

**任务**：在同一套 1P1D 部署上，对比 `--enforce-eager` 与默认图模式的启动日志与 decode 延迟，产出一份一页纸对比结论。这是本讲规格指定的代码实践任务。

**实验设计**（基于 [tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml:202](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L202) 的 decode 侧配置）：

1. **基线（图模式）**：按 u1-l4 流程原样拉起服务。等 D 侧 server_0.log 出现 `Application startup complete`，记录：
   - 从进程启动到就绪的耗时；
   - 日志中 ACL Graph 相关记录（`Wrapped original model with ACLGraphWrapper`、捕获计数等）；
   - 图缓存目录 `VLLM_CACHE_ROOT='./graph_cache'`（模板 L179）是否产生文件。
2. **对照组（eager）**：把 decode 侧 `EXTRA_ARGS` 中的 `--compilation-config {...}` 整段替换为 `--enforce-eager`（即复用 P 侧 L92 的做法），重跑 `--tags run_server`。记录同样的三项——预期捕获相关日志全部消失、启动显著变快。
3. **压测**：用 u1-l5 的 curl 方法向 proxy 7000 端口发送相同的一组短 prompt（固定并发、固定输出长度），分别记录两种模式下的每 token 延迟（流式响应的时间戳差）与吞吐。
4. **结论模板**：三列表格——启动时间 / decode 每步时延 / 显存占用（`npu-smi info` 对比图缓存额外显存），加一段文字解释原因（下发开销消除 vs 捕获与显存代价）。
5. **附加观察**：核对模板同时存在的 `TORCH_COMPILE_DISABLE=1`（L175）与 `--compilation-config level 3`（L202）的实际交互——在两种模式的日志中分别搜索编译相关记录（如 4.2.4 的 `static_kernel_compile:` DEBUG 行），确认 npugraph_ex 后端是否真的被调用。此问题源码层面无法完全判定，**待本地验证**。

**安全提示**：只改 decode 侧命令、只重跑 `run_server` tag，不动 inventory 与 docker；回退只需还原模板变量。

## 6. 本讲小结

- vLLM 的 cuda_graph 概念（`CUDAGraphMode`、`BatchDescriptor`、capture sizes）被 omni-npu 原样复用，底层换成 `torch.npu.NPUGraph`；`ACLGraphWrapper` 实现直通/捕获/重放三态，按 `BatchDescriptor` 缓存图，捕获数由 vLLM 的 `compilation_counter` 统计。
- 重放后用**旁路流 + graph task 更新**（`graph_task_update_begin/end` + `ExternalEvent`）刷新 FIA 注意力算子的序列长度参数，解决「图静态、元数据动态」的矛盾；注意力算子以 `OpDescriptor` 预描述、在捕获时经 `capture_graph_task` 登记。
- 第二级优化是**编译**：`NpuGraphExAdaptor`（`get_compile_backend` 声明）把 FX 图交给 npugraph_ex，开关全部来自 `--additional-config` 的 `npugraph_ex_config`（enable/static_kernel_compile/super_kernel_optimize/…）；`GraphPassManager`（`get_pass_manager_cls` 声明）在同一配置下注册 NPU 图优化 pass，新增 pass 的流程见 `docs/compilation.md`。
- 第三条路线 **GE 全图编译**（`TORCH_COMPILE_GE` + torchair）：`NPUCompilationConfig` 管档位（`decode_gear_list`，≤6 档，decode 侧最大档 = `max_num_seqs × (1+num_speculative_tokens)`），`TorchNpuCompilerWrapperWithCustomDispatcher` 做输入 padding 与按档分发；生产模板未开启此路线。
- `NPUGraphDispatcher` 决定每步走 eager 还是图：token 数 padding 对齐档位、批结构 relax 泛化键；`--enforce-eager` 或超档位则回落 NONE。生产模板 P 侧 eager、D 侧 FULL 图，与两侧负载特征（prefill 形状多变、decode 小而齐）精确对应。
- 三类「不走图」的兜底贯穿全链路：分发器 NONE、wrapper 模式不匹配直通、GE 包装器 `do_not_compile` 短路——图模式所有能力都是增量 opt-in。

## 7. 下一步学习建议

- **u5-l3（LOPT 并行 tokenizer）**：同属「性能机制」单元，从部署侧另一个瓶颈（prefill 的 tokenize）入手，注意它只作用于 P 侧，与本讲「P eager / D 图模式」的分工互为对照。
- **回读 u3-l2 的注意力注册表**：带着本讲的 graph task 机制重看 `npu_pangu.py:1459` 附近 `capture_graph_task` 的调用，理解 DSA 稀疏注意力为何必须在图捕获期间登记 task 才能在重放后拿到正确的 top-k 元数据。
- **通读 vLLM 侧对应实现**（容器内 `vllm/worker/cudagraph_dispatcher.py`、`vllm/compilation/`）：本讲大量「NPU 只是替换实现」的判断，需要在对照上游源码后才能真正内化。
- 若继续深入编译路线，可实践 `docs/compilation.md` 的新增 pass 三步流程，写一个只打日志的 pass 验证链路——这也是 u10-l3（二次开发）的预习。
