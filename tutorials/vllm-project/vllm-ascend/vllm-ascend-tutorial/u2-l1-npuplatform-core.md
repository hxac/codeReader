# NPUPlatform：平台核心能力

## 1. 本讲目标

上一讲（u1-l5）我们追踪了插件「被发现 → 被注册 → 被选中」的链路：vLLM 通过 entry points 调用 `register()`，拿到字符串路径 `vllm_ascend.platform.NPUPlatform`，于是把 `NPUPlatform` 设为当前平台（`current_platform`）。那么被选中之后呢？

本讲就回答这个问题：**`NPUPlatform` 到底替上游 vLLM 的 `Platform` 基类实现了哪些方法？这些方法在推理启动的哪个阶段被调用？**

学完本讲你应当能够：

- 说出 `NPUPlatform` 的「身份属性」（设备名、dispatch key、可见设备环境变量等）从何而来、为什么这么设。
- 理解 `pre_register_and_update` → `apply_config_platform_defaults` → `check_and_update_config` 这条配置改写链的**时机与先后**。
- 读懂 `get_attn_backend_cls` / `get_compile_backend` / `get_pass_manager_cls` 三个关键运行期钩子各自把哪条上游路径改道到了 Ascend。
- 理解 `_fix_incompatible_config` 这个「配置守门员」如何把 GPU 专属参数静默重置为安全值。
- **区分类方法与模块级助手**：理解 `#13484` 重构后 `_fix_incompatible_config` / `_validate_parallel_config` / `_prune_capture_sizes_for_950` 等如何从 `NPUPlatform` 的类方法变为模块级函数，以及为何这种拆分不改变功能。

## 2. 前置知识

- **Platform 抽象基类**：上游 vLLM 定义了一个 `Platform` 基类，用一组类方法和类属性来描述「当前硬件长什么样、能做什么」。GPU 有 `CudaPlatform`、CPU 有 `CpuPlatform`，vllm-ascend 提供 `NPUPlatform`。vLLM 在启动时会依据 `current_platform` 调用其中的钩子方法来「问硬件要答案」。
- **钩子（hook）**：就是基类预留、由子类填写的回调方法。例如 `get_attn_backend_cls` 是「请告诉我该用哪个注意力后端类」的钩子。
- **类属性 vs 类方法**：`device_type: str = "npu"` 是静态属性，启动即定；`check_and_update_config(...)` 是带参数的方法，vLLM 在特定阶段带着配置对象来调用它。
- **`PlatformEnum.OOT`**：OOT 即 *out-of-tree*（树外），是 vLLM 给「第三方插件平台」打的标记，区别于内置的 GPU/CPU 平台。
- **dispatch key**：PyTorch 内部用 dispatch key 决定一个算子该由哪个后端执行。`torch_npu` 把 NPU 注册在 `PrivateUse1` 这个「自定义后端」dispatch key 下。

承接 u1-l5：`register()` 只返回了**类路径字符串**，真正的初始化工作全部发生在 `NPUPlatform` 的各个钩子里——本讲就是把这些钩子逐一摊开。

## 3. 本讲源码地图

本讲几乎全部围绕单一文件：

| 文件 | 作用 |
| --- | --- |
| [vllm_ascend/platform.py](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py) | 定义 `NPUPlatform`，是 Ascend 平台的「身份证 + 总调度台」。文件分两大块：上半部分是 `NPUPlatform` 类（类属性 + 一组钩子方法）；下半部分（`#13484` 重构后）是一组**模块级助手函数**——`_fix_incompatible_config`、`_validate_parallel_config`、`_validate_draft_decode_context_parallel_config`、`_validate_kv_load_failure_policy`、`_get_default_max_cudagraph_capture_size`、`_prune_capture_sizes_for_950`、`_config_deprecated_logging`、`_validate_fa3_backend`，它们以前是类的 `@classmethod`/`@staticmethod`，现在被抽到模块级（详见 4.5）。 |

调用关系上，本文件被两类入口触及：

1. **插件注册期**：u1-l5 讲过的 `register()` 路径 → vLLM 选中 `NPUPlatform` → 调用 `pre_register_and_update`。
2. **配置解析期 / 运行期**：vLLM 的 `EngineCoreProcess` / `Worker` 在构造时反复调用 `apply_config_platform_defaults`、`check_and_update_config`、`get_attn_backend_cls` 等。

辅助依赖（本讲只引用、不深入）：`vllm_ascend.ascend_config.init_ascend_config`（解析 `additional_config`，详见 u2-l2）、`vllm_ascend.utils.adapt_patch`（两阶段补丁分发，详见 u3-l1）。

## 4. 核心概念与源码讲解

### 4.1 NPUPlatform 的「身份」与设备能力

#### 4.1.1 概念说明

被 vLLM 选中之后，`NPUPlatform` 第一件事是回答「我是谁」：设备叫什么名字、用什么 dispatch key、靠哪个环境变量选卡、默认要不要开 `torch.compile`、支持哪些量化方法。这些是**静态类属性**，启动即固定，不需要看任何配置。除此之外，还有一组「设备能力」方法，回答 NPU 的显存、算力核心数、是否支持睡眠模式等问题。

#### 4.1.2 核心流程

```
vLLM 启动
  └─ current_platform = NPUPlatform（实例化，类属性随之固定）
        ├─ device_type = "npu"          → 决定 DeviceConfig.device_type
        ├─ dispatch_key = "PrivateUse1" → 决定算子分派到 torch_npu
        ├─ device_control_env_var       → 决定读哪个环境变量做卡选择
        └─ simple_compile_backend="eager" → 默认关闭 torch.compile
vLLM 运行期按需查询设备能力：
  get_device_name / num_compute_units / get_current_memory_usage ...
```

#### 4.1.3 源码精读

类定义与一组身份属性：

[vllm_ascend/platform.py:78-95](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py#L78-L95) — 定义 `NPUPlatform(Platform)`，标注 `_enum = PlatformEnum.OOT`（树外插件）、`device_type="npu"`、`dispatch_key="PrivateUse1"`，并列出 `supported_quantization` 支持的量化方法。

几个关键属性的含义：

- `device_control_env_var = "ASCEND_RT_VISIBLE_DEVICES"`：相当于 GPU 的 `CUDA_VISIBLE_DEVICES`，是 NPU 选卡的环境变量。
- `simple_compile_backend = "eager"`：默认走 eager（不编译），因为 Ascend 有自己的图方案（ACL Graph，见 u8-l3），不用 PyTorch 原生 `torch.compile`。
- `dispatch_key = "PrivateUse1"`：让 vLLM 知道 NPU 算子挂在 PyTorch 的自定义后端 dispatch key 上。

设备能力方法中，有两个值得专门点出。一个是**故意不实现**的 `get_device_total_memory`：

[vllm_ascend/platform.py:206-213](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py#L206-L213) — `get_device_total_memory` 直接 `raise NotImplementedError`。注释解释：实现它会提前触发 `get_device_name()`，导致 `torch_npu` 过早全局初始化，而 `torch_npu` 全局只能初始化一次，重复初始化会报错。

另一个是 `num_compute_units`，它把 NPU 的 Cube 核心数类比成 GPU 的 SM 数：

[vllm_ascend/platform.py:299-321](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py#L299-L321) — 返回 `cube_core_num`（矩阵计算单元数，语义上最接近 CUDA 的 SM 数），供 vLLM 的 `layernorm_guard` 估算 Triton kernel 的 launch 网格大小；老版本 torch-npu 缺该字段时回退到 `vector_core_num`，再不行给安全默认值 24。

睡眠模式相关的能力声明（睡眠模式详见 u10-l3）：

[vllm_ascend/platform.py:110-119](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py#L110-L119) — `is_sleep_mode_available` 与 `is_cumem_allocator_available` 都返回 `True`。后者注释说明：vLLM 用「平台是否报告可用 cumem allocator」来决定是否放行睡眠模式，而 NPU 有自己的 `CaMemAllocator`，所以这里声明可用（且为避免过早 import 扩展、只声明不真去 import）。

#### 4.1.4 代码实践

> **实践类型：源码阅读型（无 NPU 也可完成）**

1. **实践目标**：搞清楚 `NPUPlatform` 用了哪些类属性把 NPU「注册」进 vLLM 的硬件抽象。
2. **操作步骤**：
   - 打开 [vllm_ascend/platform.py](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py)，定位 L127–L144 的类属性。
   - 对每个属性，回答：「它替换了上游 GPU 平台里的哪个对应值？」例如 `device_control_env_var` 在 GPU 平台里应当是 `CUDA_VISIBLE_DEVICES`。
3. **需要观察的现象**：你会注意到 Ascend 没有简单地「复用」GPU 值，而是把每一个 CUDA 概念都映射到了 NPU 对应物（dispatch key、可见设备变量、ray 设备键）。
4. **预期结果**：能写出一张「GPU 概念 → NPU 概念」的对照表，至少 4 行。
5. （无需运行命令，纯阅读即可。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `get_device_total_memory` 要故意抛 `NotImplementedError`，而不是正常返回显存大小？

> **参考答案**：因为实现它会顺带调用 `get_device_name()`，从而提前触发 `torch_npu` 的全局初始化；而 `torch_npu` 只允许全局初始化一次，过早初始化会导致后续真正初始化时报错。所以宁可「不实现」，由其他更晚的路径来安全地获取显存。

**练习 2**：`dispatch_key = "PrivateUse1"` 这个值如果不设对，会出现什么问题？

> **参考答案**：vLLM 会用 dispatch key 来判断算子该由哪个后端执行。NPU 算子由 `torch_npu` 注册在 `PrivateUse1` 下，若 dispatch key 设错，vLLM 的算子分派/自定义算子注册路径就找不到 NPU 后端，导致算子落到错误的实现或直接报错。

---

### 4.2 配置改写的三阶段生命周期（核心）

#### 4.2.1 概念说明

这是本讲最核心的部分。`NPUPlatform` 对配置的改写不是一次性完成的，而是分布在**三个先后阶段**，每个阶段 vLLM 都带着不同成熟度的 `VllmConfig` 来敲门：

1. **`pre_register_and_update`**：最早。平台刚被选中、参数还在解析时调用。负责打全局补丁、往命令行参数里追加 Ascend 量化选项。
2. **`apply_config_platform_defaults`**：配置默认值注入阶段，**早于** `check_and_update_config`。负责注入平台默认值（如 `max_cudagraph_capture_size`）。
3. **`check_and_update_config`**：配置校验与更新阶段，最重的一环。做大量校验、修正、并把 Ascend 专属调度器/编译配置写进 `VllmConfig`。

理解「时机」是关键：有些默认值必须在第 2 阶段就注入，否则第 3 阶段的下游逻辑会基于空值做错误推导。

#### 4.2.2 核心流程

```
[阶段1] pre_register_and_update(parser)
   ├─ adapt_patch(is_global_patch=True)     # 打全局平台补丁（u3-l1）
   ├─ 往 parser 追加 --quantization 的 ascend 选项
   └─ import 各量化 Config 类 / 配置 deprecated 日志
        │
        ▼  vLLM 解析参数、构造 VllmConfig
[阶段2] apply_config_platform_defaults(vllm_config)
   ├─ 若启用 SP 且未设：注入 sp_min_token_num
   └─ 若用户未设：注入默认 max_cudagraph_capture_size
        │
        ▼  vLLM 开始校验/更新配置
[阶段3] check_and_update_config(vllm_config)
   ├─ maybe_auto_detect_quantization          # 自动探测量化
   ├─ _validate_parallel_config / _validate_draft_decode_context_parallel_config
   ├─ _fix_incompatible_config                 # 4.4 讲
   ├─ init_ascend_config(vllm_config)          # 解析 additional_config（u2-l2）
   ├─ 编译/图模式 rewriting（enforce_eager、cudagraph_mode、splitting_ops）
   ├─ 选 worker_cls（310P / xlite / NPUWorker）
   ├─ 选调度器（balance / short_request_first / recompute / profiling_chunk / batch_job）
   └─ 设 PYTORCH_NPU_ALLOC_CONF、检查 mc2 冲突等
```

阶段 2 先于阶段 3，这一点在源码里有明确注释佐证（见下）。

#### 4.2.3 源码精读

**阶段 1：`pre_register_and_update`**

[vllm_ascend/platform.py:260-281](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py#L260-L281) — 调用 `adapt_patch(is_global_patch=True)` 打全局平台补丁；若传入了 argparse `parser`，就把 `ascend` 量化方法追加到 `--quantization` 的可选值里（这样 `vllm serve --quantization ascend` 才合法）；按是否 310P 导入不同的量化 Config 类；最后调用模块级助手 `_config_deprecated_logging()` 配置 deprecated 日志格式（`#13484` 后该助手从类方法 `config_deprecated_logging()` 改名下移到模块级）。

> 这正是 u1-l5 提到的「选中 NPUPlatform 后调用 `pre_register_and_update` 应用平台级补丁」的落点。

**阶段 2：`apply_config_platform_defaults` 与默认值注入**

[vllm_ascend/platform.py:283-297](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py#L283-L297) — 在启用序列并行（SP）且未设 `sp_min_token_num` 时注入它；并调用模块级助手 `_get_default_max_cudagraph_capture_size(vllm_config)` 注入默认图捕获上限（`#13484` 后该助手不再是 `cls.` 类方法，而是模块级函数）。

默认上限的计算逻辑在辅助方法里，且**显式说明与上游的差异**：

[vllm_ascend/platform.py:1116-1150](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py#L1116-L1150) — 模块级函数 `_get_default_max_cudagraph_capture_size` 镜像上游 `_set_cudagraph_sizes()` 的默认分支，但**去掉了 CUDA 那个尾部的 `* 2`**，改为以 `max_num_seqs * decode_query_len` 为上界、封顶 512：

\[ \text{default} = \min(\text{max\_num\_seqs} \times \text{decode\_query\_len},\ 512) \]

其中 `decode_query_len = 1 + num_speculative_tokens`（投机解码时每步要画多个 token）。若用户已显式设置 `max_cudagraph_capture_size` 或 `cudagraph_capture_sizes`，则返回 `None` 表示「不注入」。

**阶段 3：`check_and_update_config`（重点节选）**

这是一个近 370 行的大方法，本讲只点出它的骨架与「时机」证据，细节留给后续相关讲义。

[vllm_ascend/platform.py:345-369](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py#L345-L369) — 开头先做设备类型守卫（非 npu 直接跳过）、自动探测量化，再依次调用**模块级**校验函数 `_validate_draft_decode_context_parallel_config(vllm_config)`、`_validate_parallel_config(vllm_config)`、`_fix_incompatible_config(vllm_config)`（注意：`#13484` 后它们不再以 `cls.` 形式调用），然后调用 `init_ascend_config(vllm_config)` 解析 `additional_config`。

「阶段 2 先于阶段 3」的直接证据在方法中段：

[vllm_ascend/platform.py:436-440](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py#L436-L440) — 注释明确写道：「平台默认 max 已在更早的 `apply_config_platform_defaults` 中注入，因此这里的晚趟处理只应在上述模式调整后，遵循当前 max/size 输入」，随后调用 `vllm_config._set_cudagraph_sizes()` 重算。

`check_and_update_config` 还承担**选 worker 类**的职责——这是把执行主链路接到 Ascend 的关键一步：

[vllm_ascend/platform.py:544-554](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py#L544-L554) — 当 `worker_cls == "auto"` 时，按硬件/特性把 worker 类分别指向 `NPUWorker310`（310P）、`XliteWorker`（xlite 分层推理）或 `NPUWorker`（默认）。这一步决定了 vLLM 接下来 spawn 的 worker 是 Ascend 版（详见 u4-l1）。

#### 4.2.4 代码实践

> **实践类型：源码阅读型**

1. **实践目标**：验证「阶段 2 注入的默认值，会被阶段 3 的下游逻辑消费」这条因果链。
2. **操作步骤**：
   - 在 [platform.py:283-297](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py#L283-L297) 找到 `apply_config_platform_defaults` 注入的 `max_cudagraph_capture_size`。
   - 在 [platform.py:436-462](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py#L436-L462) 找到 `check_and_update_config` 里 `_set_cudagraph_sizes()` 与 SP 尺寸过滤两段，确认它们读取的正是阶段 2 注入的那个值。
3. **需要观察的现象**：阶段 3 的代码并不重新计算「默认上限」，而是依赖阶段 2 已经写进 `vllm_config.compilation_config.max_cudagraph_capture_size` 的值。
4. **预期结果**：能用一句话解释「为什么 Ascend 必须在第 2 阶段就注入默认值，而不能拖到第 3 阶段」——因为第 3 阶段的 `_set_cudagraph_sizes()` 等逻辑会基于该默认值进一步推导 capture size 列表。
5. （纯源码阅读，无需运行。）

#### 4.2.5 小练习与答案

**练习 1**：`_get_default_max_cudagraph_capture_size` 相比上游去掉了什么？为什么？

> **参考答案**：去掉了上游 CUDA 那个尾部的 `* 2`（即不再把上界翻倍）。Ascend 希望默认的图捕获上界贴近 `max_num_seqs * decode_query_len` 并封顶 512，而不是照搬 GPU 的放大系数。

**练习 2**：如果用户在命令行同时指定了 `--max-cudagraph-capture-size`，`_get_default_max_cudagraph_capture_size` 会怎样？

> **参考答案**：方法开头判断到 `compilation_config.max_cudagraph_capture_size is not None` 时直接返回 `None`，表示「平台不再注入默认值」，把决定权完全交还给用户。

---

### 4.3 运行期能力钩子：注意力后端 / 编译后端 / Pass Manager

#### 4.3.1 概念说明

阶段 1–3 解决的是「配置长什么样」。但 vLLM 真正跑起来时，还需要平台在三个关键岔路口给出 Ascend 的答案：

- **注意力后端**：用哪个注意力实现（普通 / MLA / SFA / DSA / FA3）？
- **编译后端**：`torch.compile` 走哪个自定义后端？
- **融合 Pass Manager**：图编译时谁来跑算子融合 pass？

这三者都是「返回一个类路径字符串」的钩子——和 u1-l5 里 `register()` 返回 `NPUPlatform` 字符串是同一种「延迟 import」风格：避免重型 import 与循环依赖。

#### 4.3.2 核心流程

```
vLLM 构造注意力后端
  └─ get_attn_backend_cls(selected_backend, attn_selector_config)
        └─ 依 (use_mla, use_sparse, use_compress) 查表 → 返回 Ascend 后端类路径

vLLM 设置编译后端
  └─ get_compile_backend() → "vllm_ascend.compilation.compiler_interface.AscendCompiler"

vLLM 注册融合 pass
  └─ get_pass_manager_cls() → "...graph_fusion_pass_manager.GraphFusionPassManager"
        └─ 挂在 pass_key（COMPILATION_PASS_KEY）名下
```

#### 4.3.3 源码精读

**注意力后端选择**——本讲最值得精读的一张「路由表」：

[vllm_ascend/platform.py:215-242](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py#L215-L242) — `get_attn_backend_cls` 以 `(use_mla, use_sparse, use_compress)` 三元组查 `backend_map`，分别路由到：

| `(use_mla, use_sparse, use_compress)` | 返回后端 | 适用 |
| --- | --- | --- |
| `(True, False, False)` | `AscendMLABackend` | DeepSeek 等 MLA 模型 |
| `(False, False, False)` | `AscendAttentionBackend` | 普通注意力 |
| `(True, True, False)` | `AscendSFABackend` | 稀疏 MLA（SFA） |
| `(True, False, True)` | `AscendDSABackend` | 压缩 MLA（DSA） |

此外，当 `selected_backend == FLASH_ATTN` 且通过 FA3 校验时，单独返回 `AscendFABackend`（训练-推理一致性场景，见 [platform.py:220-221](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py#L220-L221) 与模块级函数 [_validate_fa3_backend（platform.py:1089-1113）](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py#L1089-L1113)）；310P 走另一张更小的表（详见 u11-l2）。这些后端的具体实现见 u5-l1、u5-l2。

**编译后端与 Pass Manager**——把图编译改道到 Ascend：

[vllm_ascend/platform.py:163-177](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py#L163-L177) — `get_pass_manager_cls` 返回 `GraphFusionPassManager`，`get_compile_backend` 返回 `AscendCompiler`。注释说明：早期用 EagerAdaptor，后来为了用上算子图融合，自定义了编译后端。

[platform.py:97-104](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py#L97-L104) — `pass_key` 返回 `COMPILATION_PASS_KEY`，即 PassManager 作为自定义 pass 注册到 inductor config 时所用的键名。

**其他运行期能力钩子**（一句话带过，对应后续讲义）：

- [platform.py:188-190](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py#L188-L190) `get_device_communicator_cls` → `NPUCommunicator`（HCCL，u7-l2）。
- [platform.py:179-181](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py#L179-L181) `get_punica_wrapper` → `PunicaWrapperNPU`（LoRA，u10-l5）。
- [platform.py:192-197](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py#L192-L197) `get_static_graph_wrapper_cls` → `ACLGraphWrapper`（分段图，u8-l3）。
- [platform.py:244-258](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py#L244-L258) `import_kernels` → 懒加载自定义算子环境（用 `_CUSTOM_OP_REGISTERED` 保证每进程只做一次）。
- [platform.py:717-853](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py#L717-L853) `set_additional_forward_context` → 每次前向前注入 MoE 通信方式、mc2 mask 等运行期信息（进阶，关联 u2-l3）。

#### 4.3.4 代码实践

> **实践类型：源码阅读型**

1. **实践目标**：理解注意力后端是如何被「查表」选出来的。
2. **操作步骤**：
   - 阅读 [platform.py:215-242](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py#L215-L242)。
   - 假设一个 DeepSeek MLA 模型（`use_mla=True`）且未启用稀疏/压缩，回答 `get_attn_backend_cls` 会返回哪个字符串。
3. **需要观察的现象**：返回值是**类路径字符串**而非类对象；vLLM 拿到字符串后再延迟 import 真正的后端类。
4. **预期结果**：`(True, False, False)` → `vllm_ascend.attention.mla_v1.AscendMLABackend`。
5. （无需运行；若本地有 NPU，可在启动日志里搜 `AscendMLABackend` 验证——待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：`get_attn_backend_cls` / `get_compile_backend` / `get_pass_manager_cls` 为什么都返回「字符串」而不是直接 import 并返回类？

> **参考答案**：延迟 import。直接 import 会把重型模块（注意力后端、编译器、融合 pass）在平台选中时就全部加载，既拖慢启动、又容易触发循环依赖。返回字符串让 vLLM 在真正需要时才 import。

**练习 2**：FA3 后端在什么条件下才会被选中？

> **参考答案**：仅当 `selected_backend == FLASH_ATTN` 且 `_validate_fa3_backend` 通过——即处于训练-推理一致性场景（`use_batch_invariant=True`）、不是 MLA/SFA、且本机装好了带 `flash_attn_with_kvcache` 的 `flash_attn_npu_v3`。

---

### 4.4 配置守门员：`_fix_incompatible_config`

#### 4.4.1 概念说明

vLLM 的参数里混着大量 GPU/ROCm 专属选项（如 `disable_cascade_attn`、`enable_layerwise_nvtx_tracing`、`numa_bind`、`use_inductor_graph_partition` 等）。如果用户照搬 GPU 配置，这些选项在 NPU 上要么无效、要么有害。`_fix_incompatible_config` 就是个**守门员**：在 `check_and_update_config` 中段被调用，逐项检查并把这些不兼容参数**静默重置为安全值**，同时打 warning/info 告诉用户「我改了什么」。

这是一个非常典型的「插件健壮性」设计：不报错中断，而是尽可能把无效配置修正后继续跑。

#### 4.4.2 核心流程

```
check_and_update_config
  └─ _fix_incompatible_config(vllm_config)
        ├─ 1. ModelConfig:   disable_cascade_attn → False
        ├─ 2. CacheConfig:   cpu_kvcache_space_bytes → None；calculate_kv_scales → False
        ├─ 3. MultiModal:    mm_encoder_attn_backend → None
        ├─ 4. Observability: enable_layerwise_nvtx_tracing → False
        ├─ 5. Scheduler:     max_num_partial_prefills → 1
        ├─ 6. Speculative:   quantization → None（自动继承主模型）
        ├─ 7. KVTransfer:    kv_buffer_size → 1e9；enable_permute_local_kv → False
        ├─ 8. Attention:     一组 force_false_flags；flash_attn_version → None；backend → None ...
        ├─ 9. Parallel:      ray_workers_use_nsight → False；numa_bind → enable_cpu_binding；enable_dbo/ubatch_size → 重置
        ├─10. Compilation:   use_inductor_graph_partition → False
        └─11. envs:          VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS < 1836 → 3000
```

#### 4.4.3 源码精读

整体方法签名与说明：

[vllm_ascend/platform.py:856-861](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py#L856-L861) — `_fix_incompatible_config` 是**模块级函数**（`#13484` 前是 `NPUPlatform` 的 `@staticmethod`，现已下移到类外），逐段检查并修正不兼容参数。它在 `check_and_update_config` 中以 `_fix_incompatible_config(vllm_config)` 形式被调用。

挑三个有代表性的修正来看：

**示例一：把 GPU 专属注意力特性关掉**

[vllm_ascend/platform.py:952-998](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py#L952-L998) — Attention 段把一组 NVIDIA 专属布尔标记（`use_trtllm_attention`、`use_cudnn_prefill` 等）强制置 False，把 `flash_attn_version` 置 None，并提示「Ascend 会用自己的插件后端」。

**示例二：把 `--numa-bind` 转译成 Ascend 原生 CPU 绑定**

[vllm_ascend/platform.py:1017-1028](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py#L1017-L1028) — 因为 Ascend 不支持 GPU-to-NUMA 拓扑探测，所以把 `numa_bind=False`，转而在 `additional_config` 里写入 `enable_cpu_binding=True`，用 Ascend 自带的拓扑亲和 CPU 绑定替代。

**示例三：修正 HCCL 超时**

[vllm_ascend/platform.py:1069-1076](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py#L1069-L1076) — HCCL 算子的超时阈值是 1836 秒，因此多进程 `execute_model` 的 RPC 超时必须大于 1836 秒；若用户设的 `VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS < 1836`，就抬高到 3000。

#### 4.4.4 代码实践

> **实践类型：源码阅读型**

1. **实践目标**：体会「守门员」如何把无效配置转译为有效配置，而不是简单报错。
2. **操作步骤**：
   - 阅读 [platform.py:856-1076](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py#L856-L1076) 全文，数一下它一共分了几大段（文件里用注释 `==== N. Xxx Config ====` 分段）。
   - 挑 `numa_bind → enable_cpu_binding` 这一处，思考：为什么不直接报错说「不支持」？
3. **需要观察的现象**：每段修正都配套一条 `logger.warning` 或 `logger.info`，即「改了什么 + 为什么改」。
4. **预期结果**：能复述至少 3 处「转译式」修正（不只是置 False，而是换成了 Ascend 的等价机制），如 `numa_bind → enable_cpu_binding`。
5. （纯阅读。）

#### 4.4.5 小练习与答案

**练习 1**：`_fix_incompatible_config` 为什么选择「重置 + 打 warning」而不是直接 `raise`？

> **参考答案**：为了让用户能尽量照搬 GPU 配置直接在 NPU 上跑起来，降低迁移成本。直接报错会让任何带 GPU 专属参数的配置都启动失败；而静默重置为安全值并告知用户，既保证正确性又保留可用性。

**练习 2**：`VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS` 被抬高到 3000 的硬性下限是多少？为什么是这个数？

> **参考答案**：下限是 1836 秒，因为 HCCL 算子自身的超时阈值就是 1836 秒；多进程 `execute_model` 的 RPC 超时若小于它，会在 HCCL 还没超时前就先误判 worker 卡死，所以必须大于 1836 秒。

---

### 4.5 类方法 vs 模块级助手：`#13484` 重构

> 这是本讲的「阅读地图」模块：读懂 `platform.py` 的代码组织，你才能在千行大文件里快速定位逻辑。

#### 4.5.1 概念说明

在 `#13484` 之前，本讲 4.4 讲的 `_fix_incompatible_config`、4.2 讲的 `_validate_parallel_config`、`_get_default_max_cudagraph_capture_size`、`prune_capture_sizes_for_950` 等都是 `NPUPlatform` 的 `@classmethod` / `@staticmethod`。它们虽然挂在类里，但**都是无状态纯函数**——只读写传入的 `vllm_config`，从不访问 `cls` 的任何类属性。挂在类里只是为了「就近」，代价是 `NPUPlatform` 类膨胀到上千行，新人很难一眼分清「哪些是 vLLM 要求实现的钩子、哪些是内部助手」。

`#13484` 做了一次**纯组织性重构**（不改变任何功能）：

1. 把这一批内部助手从类里**抽到模块级**（移到文件下半部分 `class NPUPlatform` 定义之后），统一加上 `_` 前缀表示「模块私有」。
2. 类内只保留两类东西：**vLLM 钩子契约**（`get_attn_backend_cls`、`check_and_update_config` 等）+ **设备能力声明**（`is_pin_memory_available`、`opaque_attention_op`、`support_hybrid_kv_cache`、`support_static_graph_mode`、`use_custom_op_collectives`、`register_custom_kv_cache_specs` 等一组返回常量的 `@classmethod`，也被集中重排到类体顶部）。
3. 调用点相应从 `cls._fix_incompatible_config(vllm_config)` 改为 `_fix_incompatible_config(vllm_config)`。

收益：类聚焦于「钩子契约」，助手可被独立单元测试，文件读起来「上半部分=对外钩子、下半部分=内部助手」，边界清晰。

#### 4.5.2 核心流程

```
#13484 前                                 #13484 后
─────────────────────────────             ─────────────────────────────
NPUPlatform:                              NPUPlatform:
  device_type = "npu"        (类属性)       device_type = "npu"        (类属性)
  ... 钩子与助手混杂排列 ...                设备能力 classmethod 组（顶部）
  @classmethod                              ... vLLM 钩子（重排） ...
  def _fix_incompatible_config(cls, ...)    check_and_update_config:
  @classmethod                                _validate_parallel_config(...)   ← 不带 cls
  def _validate_parallel_config(cls, ...)     _fix_incompatible_config(...)    ← 不带 cls
  @classmethod                                ...
  def _get_default_max_cudagraph_capture_size
  ...                                      # 模块级助手（类外，文件下半部分）
                                           def _fix_incompatible_config(...):
                                           def _validate_parallel_config(...):
                                           def _get_default_max_cudagraph_capture_size(...):
                                           def _prune_capture_sizes_for_950(...):
                                           def _config_deprecated_logging(...):
                                           def _validate_kv_load_failure_policy(...):
                                           def _validate_fa3_backend(...):
                                           def _validate_draft_decode_context_parallel_config(...):
```

关键判别法：**带不带 `cls`/`@classmethod`、能不能脱离 `NPUPlatform` 单独调用**。能脱离的就是模块级助手。

#### 4.5.3 源码精读

**文件下半部分的模块级助手群**（`class NPUPlatform` 在 L855 之前结束，之后全部是模块级函数）：

[vllm_ascend/platform.py:856-1268](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py#L856-L1268) — 自上而下依次定义：`_fix_incompatible_config`（L856，4.4 详讲）、`_validate_kv_load_failure_policy`（L1079）、`_validate_fa3_backend`（L1089）、`_get_default_max_cudagraph_capture_size`（L1116，4.2 详讲）、`_config_deprecated_logging`（L1153）、`_prune_capture_sizes_for_950`（L1183）、`_validate_parallel_config`（L1202）、`_validate_draft_decode_context_parallel_config`（L1212）。注意它们都没有 `@classmethod` 装饰器、首参数是 `vllm_config` 而非 `cls`。

**调用点的变化**——以 `check_and_update_config` 为例，它现在直接用「裸函数名」调用这些助手：

[vllm_ascend/platform.py:363-367](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py#L363-L367) — `_validate_draft_decode_context_parallel_config(vllm_config)` / `_validate_parallel_config(vllm_config)` / `_fix_incompatible_config(vllm_config)` 三连调用，均无 `cls.` 前缀。重构前这里是 `cls._validate_parallel_config(vllm_config)` 等。

类似地，`_prune_capture_sizes_for_950` 在图模式分支里被裸调用：

[vllm_ascend/platform.py:515-516](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py#L515-L516) — A5 设备分支里 `_prune_capture_sizes_for_950(vllm_config)`（裁剪 capture sizes，详见 u8-l3）。

**类体顶部的设备能力声明**（重构后被集中重排到这里的一组简单 `@classmethod`）：

[vllm_ascend/platform.py:121-161](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py#L121-L161) — `is_pin_memory_available`、`opaque_attention_op`、`support_hybrid_kv_cache`、`support_static_graph_mode`、`use_custom_op_collectives`、`get_device_capability`、`get_device_name`、`inference_mode`、`set_device`、`register_custom_kv_cache_specs` 等，几乎都只返回一个常量（`True`/`None`），用来回答 vLLM 对「NPU 支不支持某能力」的询问。

#### 4.5.4 代码实践

> **实践类型：源码阅读型（对应本讲 `practice_task` 的一部分）**

1. **实践目标**：验证「`#13484` 把配置校验助手从类方法挪到了模块级函数」这一结论。
2. **操作步骤**：
   - 打开 [platform.py](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py)，跳到 L856 之后，确认 `_fix_incompatible_config`、`_validate_parallel_config`、`_prune_capture_sizes_for_950` 等都定义在 `class NPUPlatform` **之外**、且无 `@classmethod`。
   - 再在 `check_and_update_config`（L345 起）里全局搜索这些名字，确认调用处**不带 `cls.`**。
3. **需要观察的现象**：助手函数首参数一律是 `vllm_config`，而不是 `cls`；它们与类的唯一耦合就是「被类的某个方法调用」。
4. **预期结果**：能列出至少 3 个「由类方法变成模块级函数」的助手，并解释这种拆分不影响功能（因为它们本就不依赖 `cls`）。
5. （纯阅读。若有 git 仓库，可执行 `git log --oneline -- vllm_ascend/platform.py` 找到 `#13484` 提交，再用 `git show` 看其 diff 印证——待本地验证。）

#### 4.5.5 小练习与答案

**练习 1**：为什么 `_fix_incompatible_config` 适合被抽成模块级函数，而 `get_attn_backend_cls` 不适合？

> **参考答案**：`_fix_incompatible_config` 是无状态纯函数，只读写传入的 `vllm_config`，完全不碰 `cls` 的类属性，脱离类也能独立调用和测试，所以适合外移。`get_attn_backend_cls` 是 vLLM **平台钩子契约**的一部分——vLLM 通过 `current_platform.get_attn_backend_cls(...)` 来调用它，必须作为 `Platform` 子类的方法存在，不能下移到模块级。

**练习 2**：重构后，`check_and_update_config` 里调用 `_validate_parallel_config` 的写法是 `cls._validate_parallel_config(vllm_config)` 还是 `_validate_parallel_config(vllm_config)`？为什么？

> **参考答案**：是 `_validate_parallel_config(vllm_config)`（不带 `cls.`）。因为该函数已从类方法变成模块级函数，调用时直接用模块作用域里的函数名即可，无需也不能用 `cls.` 前缀。

---

## 5. 综合实践

把本讲五个模块串起来，完成下面这张「`NPUPlatform` 重写方法 × 启动阶段」对照表（这是本讲的 `practice_task` 第一部分）。

**任务一**：在 [vllm_ascend/platform.py](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py) 中至少挑出 **5 个**对上游 `Platform` 的重写方法/属性，填入下表，并标注它**在推理启动的哪个阶段**被调用。

| # | 重写项（方法/属性） | 所在行 | 调用阶段 | 一句话作用 |
| --- | --- | --- | --- | --- |
| 1 | `pre_register_and_update` | L260 | 阶段 1：平台注册期 | 打全局补丁、追加 ascend 量化选项 |
| 2 | `apply_config_platform_defaults` | L283 | 阶段 2：默认值注入期 | 注入 sp_min_token_num、max_cudagraph_capture_size |
| 3 | `check_and_update_config` | L345 | 阶段 3：配置校验/更新期 | 校验并行、选 worker_cls、改写编译/调度配置 |
| 4 | `get_attn_backend_cls` | L215 | 运行期：注意力后端构造时 | 按 (mla,sparse,compress) 查表选 Ascend 注意力后端 |
| 5 | `get_compile_backend` | L171 | 运行期：编译后端接入时 | 返回 `AscendCompiler` 自定义编译后端 |
| 6 | `_fix_incompatible_config` | L856（模块级函数） | 阶段 3 内部（被 check_and_update_config 调用） | 把 GPU 专属参数重置为 Ascend 安全值 |

**任务二**（对应 `practice_task` 第二部分）：指出 `#13484` 后哪些配置校验逻辑**从类方法变成了模块级函数**。请在文件里确认下列函数都已落在 `class NPUPlatform` 之外、且调用处不带 `cls.`：

- `_validate_parallel_config`（[L1202](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py#L1202-L1209)）
- `_fix_incompatible_config`（[L856](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py#L856-L861)）
- `_validate_draft_decode_context_parallel_config`（[L1212](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py#L1212-L1218)）
- `_get_default_max_cudagraph_capture_size`（[L1116](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py#L1116-L1150)）
- `_prune_capture_sizes_for_950`（[L1183](https://github.com/vllm-project/vllm-ascend/blob/3829122510c00dfc6b4b94d6f96c947a7590043c/vllm_ascend/platform.py#L1183-L1199)）

**进阶思考**（可选）：如果让你新增一个 Ascend 专属的启动期行为（例如「当检测到某型号 NPU 时强制开启某个编译选项」），你会把它放进上面三个阶段中的哪一个？为什么？

> **参考思路**：若依赖用户配置（如 `max_num_seqs`），应放阶段 3 `check_and_update_config`；若是纯平台默认值且下游会消费，放阶段 2 `apply_config_platform_defaults`；若需要在参数解析阶段就让某个命令行选项可用，放阶段 1 `pre_register_and_update`。

## 6. 本讲小结

- `NPUPlatform` 用一组**类属性**完成「身份注册」：`device_type="npu"`、`dispatch_key="PrivateUse1"`、`device_control_env_var="ASCEND_RT_VISIBLE_DEVICES"` 等，把 CUDA 概念逐个映射到 NPU。
- 配置改写分布在**三个阶段**：`pre_register_and_update`（注册期，打全局补丁 + 追加量化选项）→ `apply_config_platform_defaults`（默认值注入，早于校验）→ `check_and_update_config`（校验 + 选 worker + 改写编译/调度配置）。
- 阶段 2 必须先注入 `max_cudagraph_capture_size` 默认值，阶段 3 的 `_set_cudagraph_sizes()` 才能正确推导 capture size 列表——这是理解「时机」的关键。
- `get_attn_backend_cls` 用 `(use_mla, use_sparse, use_compress)` 三元组查表选注意力后端；`get_compile_backend` / `get_pass_manager_cls` 把图编译改道到 `AscendCompiler` 与 `GraphFusionPassManager`。这些钩子统一返回**类路径字符串**以延迟 import。
- `_fix_incompatible_config` 是「配置守门员」：逐段把 GPU/ROCm 专属参数重置为安全值（如 `numa_bind → enable_cpu_binding`、超时抬高到 3000），并打日志告知用户，让 GPU 配置能尽量平滑迁移到 NPU。
- `#13484` 是一次**纯组织性重构**：`_fix_incompatible_config` / `_validate_parallel_config` / `_validate_draft_decode_context_parallel_config` / `_get_default_max_cudagraph_capture_size` / `_prune_capture_sizes_for_950` / `_config_deprecated_logging` / `_validate_kv_load_failure_policy` / `_validate_fa3_backend` 这批无状态助手从 `NPUPlatform` 的类方法（`cls.xxx`）下移为模块级函数（`xxx`，统一加 `_` 前缀），类体顶部则集中重排了一组设备能力声明 `@classmethod`；功能完全不变，只是「上半部分=对外钩子、下半部分=内部助手」边界更清晰。
- 部分钩子是「故意不实现」或「故意返回 None」：如 `get_device_total_memory` 抛 `NotImplementedError` 以避免 `torch_npu` 过早初始化。

## 7. 下一步学习建议

- **u2-l2 配置体系：AscendConfig 与 envs**：本讲多次出现 `init_ascend_config(vllm_config)` 与 `additional_config`，下一讲拆开看 `AscendConfig` 如何解析 JSON、`envs.py` 如何管理 `VLLM_ASCEND_*` 环境变量。
- **u2-l3 前向上下文与 MoE 通信类型**：本讲的 `set_additional_forward_context`（L717）只是点到，下一讲深入它如何选择 MoE 通信方式（ALLTOALL / MC2 / FUSED_MC2）。
- **u3-l1 Patch 机制总览**：本讲阶段 1 调用的 `adapt_patch(is_global_patch=True)` 是 patch 框架的入口，建议接着读 patch 机制全貌。
- **u5-1 注意力后端**：本讲只讲了 `get_attn_backend_cls` 的「路由」，后端本身（`AscendMLABackend` 等）的实现留给 u5 系列。
