# GEMM 全景与 mm_\* API

## 1. 本讲目标

本讲是「GEMM 与低精度计算」单元（第 5 单元）的第一篇，目标是让你对 `flashinfer.gemm` 包建立一个**全景式**的认识，而不纠缠某个具体 kernel 的实现细节。

学完后你应该能够：

1. 说出 GEMM 包的三类入口——`mm_*`（单矩阵乘）、`bmm_*`（批矩阵乘）、`group_*` / `SegmentGEMMWrapper`（变长分组矩阵乘）——各自服务的场景，以及 `bf16/fp8/fp4/mxfp8` 这几条低精度支线的位置。
2. 看懂「同一算子、多后端」的统一封装：理解 `@backend_requirement` 与 `@supported_compute_capability` 两个装饰器如何把「能力查询（能不能跑）」与「后端选择（用哪个跑）」声明式地焊进每一个 GEMM API。
3. 画出从 Python 的 `mm_bf16(...)` 出发，经 `bf16_gemm_sm100` 调度器、`TunableRunner` 列表、`AutoTuner`，最终落到 TVM-FFI 绑定 `csrc/flashinfer_gemm_binding.cu` 的完整调用链。

本讲只做「地图课」与「机制课」：先认清 GEMM 包有哪些入口、它们如何被装饰器统一管理、再串起一条到 C++ kernel 的数据流。具体的 FP8/FP4 量化与反量化、grouped GEMM 的细枝末节、各后端 kernel 内部实现，分别留给本单元后续讲义（u5-l2 ~ u5-l5）。

## 2. 前置知识

在进入 GEMM 之前，请确认你已经具备以下认知（它们在前置讲义中建立）：

- **JIT 三层架构**（u2-l1 ~ u2-l5）：理解 `gen_*_module()` → `JitSpec` → `build_and_load()` 的代码生成与编译加载链路，以及 `@functools.cache` 进程内缓存 + 磁盘 `.so` 两级缓存。GEMM 包里大量出现的 `get_gemm_module()`、`get_gemm_sm100_module_cutlass_bf16()` 等函数，本质都是「调对应 `gen_*` 生成器、`.build_and_load()` 出一个 TVM-FFI 模块」并缓存。
- **后端选择机制**（u3-l5）：理解「多后端」是贯穿 FlashInfer 的设计——同一算子提供 FlashAttention-2/3、cuDNN、CUTLASS、TensorRT-LLM 等多种实现，按硬件与数据类型选优。本讲会把这套思想从注意力推广到 GEMM，并引入比注意力 wrapper 更新式的 `@backend_requirement` 装饰器。

下面补充几个本讲会用到的、属于 GEMM 领域本身的术语：

- **GEMM**：General Matrix Multiply，通用矩阵乘 \(C = A \times B\)。在 LLM 推理里，全连接层、投影、门控 FFN 的核心都是它。
- **NT 布局**：输入 `a` 行主序（row-major，shape `(m, k)`），权重 `b` 列主序（column-major，shape `(k, n)`）。FlashInfer 的 GEMM API 几乎都要求权重以「转置后的列主序」传入，即 `b = torch.randn([n, k]).transpose(-2, -1)`，这样 `b` 在内存里恰好是 `(k, n)` 列主序。函数名里的 `_nt` 后缀就指这个约定。
- **BF16 / FP8 / FP4**：浮点数位宽。BF16（brain float 16）是训练/推理主力；FP8（8 比特）与 FP4（4 比特）是低精度量化格式，靠张量核（Tensor Core）加速但需要携带缩放因子（scale）来保精度。
- **groupwise / blockscaled**：缩放因子的粒度。per-tensor 是整张张量一个 scale；groupwise 是每 `128×128`（或类似）块一个 scale；FP4 还有 NVFP4 / MXFP4 两种 4 比特变体。这些细节后续讲义会展开，本讲只需记住「缩放粒度」是低精度 GEMM 的一个核心维度。

> 提示：如果对 FP8/FP4 完全陌生，本讲的第 4.1 节只需理解「存在 bf16/fp8/fp4 三条支线」即可，不必纠结缩放张量的形状。

## 3. 本讲源码地图

本讲涉及的关键文件如下表。注意第 5 单元共用的「入口 + 装饰器 + 绑定」三件套在本讲集中讲解，后续讲义会按精度支线分别深入各自的 `gemm_*.py` 与 `csrc/*_cutlass.cu`。

| 文件 | 作用 | 本讲用它讲什么 |
|------|------|----------------|
| [flashinfer/gemm/\_\_init\_\_.py](https://github.com/flashinfer-ai/flashinfer/blob/3fd5c55bc84bc00b27bed5099031fa3aab8a4fb2/flashinfer/gemm/__init__.py) | GEMM 包的对外出口，把 `gemm_base.py` 等模块里的符号重新导出，组装 `__all__` | GEMM 入口全景 |
| [flashinfer/gemm/gemm_base.py](https://github.com/flashinfer-ai/flashinfer/blob/3fd5c55bc84bc00b27bed5099031fa3aab8a4fb2/flashinfer/gemm/gemm_base.py) | GEMM 包的主体（约 9000 行），承载 `mm_bf16/bmm_bf16/mm_fp8/mm_fp4/...` 全部 API、各后端 runner、调度器、cuDNN 图构造 | 三大模块都重度引用 |
| [flashinfer/utils.py](https://github.com/flashinfer-ai/flashinfer/blob/3fd5c55bc84bc00b27bed5099031fa3aab8a4fb2/flashinfer/utils.py) | 通用工具，定义 `@backend_requirement`、`@supported_compute_capability`、`determine_gemm_backend` | 统一封装机制 |
| [csrc/flashinfer_gemm_binding.cu](https://github.com/flashinfer-ai/flashinfer/blob/3fd5c55bc84bc00b27bed5099031fa3aab8a4fb2/csrc/flashinfer_gemm_binding.cu) | TVM-FFI 绑定层，用 `TVM_FFI_DLL_EXPORT_TYPED_FUNC` 把 C++ 的 `bmm_fp8`、`CutlassSegmentGEMM` 导出给 Python | 统一绑定链路终点 |

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：**GEMM 入口**（有哪些 API）、**backend_requirement 统一封装**（这些 API 如何被声明式管理）、**统一绑定**（一次调用如何从 Python 走到 C++ kernel）。

### 4.1 GEMM 入口：mm_\* / bmm_\* / group_\* 全景

#### 4.1.1 概念说明

「GEMM 入口」回答一个最朴素的问题：**用户能在 `flashinfer.gemm` 里调到哪些函数？** 它们按两个正交维度分类：

- **形状维度**：
  - `mm_*`：单矩阵乘，输入二维 \((m, k) \times (k, n) \to (m, n)\)。
  - `bmm_*`：批矩阵乘（batched），输入三维 \((b, m, k) \times (b, k, n) \to (b, m, n)\)，即一个 batch 内做 \(b\) 次独立同尺寸的矩阵乘。
  - `group_*` / `SegmentGEMMWrapper`：变长分组矩阵乘（grouped/segmented），每段长度不同、甚至可带不同权重，用前缀和数组 `indptr`/`seg_lens` 描述。这是 MoE 多专家、LoRA 场景的核心（详见 u5-l4）。
- **精度维度**：`bf16` / `fp8` / `mxfp8` / `fp4`（含 NVFP4、MXFP4）/ `mxfp4`。低精度需要额外传入缩放张量 `scale` / `descale` / `block_scale`。

这两个维度相乘，就得到了 `__init__.py` 里那一长串导出符号。初学者不必背全名，记住「形状前缀 + 精度后缀」即可：`mm_<精度>`、`bmm_<精度>`、`group_gemm_<精度>_<布局>`。

#### 4.1.2 核心流程

GEMM API 的命名遵循一套可解析的规则，掌握规则就能「见名知意」：

```
mm   ──┐
bmm  ──┼──→ 形状前缀（单 / 批 / 变长分组）
group ─┘
   +
bf16 / fp8 / mxfp8 / fp4 / mxfp4 / nvfp4  ──→ 精度后缀
   +
（可选）_nt_groupwise / _nt_blockscaled  ──→ 缩放粒度 / 布局
```

对应到三类数学语义（行内公式用 \( \) 包裹）：

- 单矩阵乘：\(C_{m,n} = \sum_{k} A_{m,k} \cdot B_{k,n}\)
- 批矩阵乘：对每个 \(b\)，\(C^{(b)}_{m,n} = \sum_{k} A^{(b)}_{m,k} \cdot B^{(b)}_{k,n}\)
- 变长分组：把 \(\sum_i m_i\) 个 token 拼成一条长输入，按段切分后分别乘以各自权重 \(W_i\)，输出再拼回。

#### 4.1.3 源码精读

入口最直接的体现就是 `__init__.py` 的导出表。[flashinfer/gemm/\_\_init\_\_.py:1-24](https://github.com/flashinfer-ai/flashinfer/blob/3fd5c55bc84bc00b27bed5099031fa3aab8a4fb2/flashinfer/gemm/__init__.py#L1-L24) 把 `gemm_base.py` 里的核心 API 全部重新导出，这一段代码就是「GEMM 入口全景」的目录：

- `mm_bf16 / mm_fp8 / mm_fp4 / mm_mxfp8`：**单**矩阵乘的 4 条精度支线（第 1-8 行）。
- `bmm_bf16 / bmm_fp8 / bmm_mxfp8`：**批**矩阵乘（第 2-4 行）。
- `group_gemm_*`、`batch_deepgemm_*`、`group_deepgemm_*`：**变长分组**，其中 `deepgemm` 是 DeepSeek 风格的 FP8 分组 GEMM（第 10-23 行）。
- `gemm_fp8_nt_groupwise / gemm_fp8_nt_blockscaled`：FP8 的两种缩放粒度（第 21-22 行）。

末尾的 `__all__` 元组（[flashinfer/gemm/\_\_init\_\_.py:89-118](https://github.com/flashinfer-ai/flashinfer/blob/3fd5c55bc84bc00b27bed5099031fa3aab8a4fb2/flashinfer/gemm/__init__.py#L89-L118)）显式列出了对外公开的符号，并拼上可选的 `_cute_dsl_kernels` 与 `_cuda_tile_kernels`——这两个列表说明 GEMM 包还**有条件地**导入一些依赖外部包（`nvidia-cutlass-dsl`、`cuda.tile`）的实验后端，导入失败则静默跳过。这正是「多后端」架构在包入口处的体现：能跑的后端才进 `__all__`。

再看 `mm_bf16` 的函数签名（[flashinfer/gemm/gemm_base.py:530-540](https://github.com/flashinfer-ai/flashinfer/blob/3fd5c55bc84bc00b27bed5099031fa3aab8a4fb2/flashinfer/gemm/gemm_base.py#L530-L540)）：`a: (m, k)` 行主序、`b: (k, n)` 列主序、可选 `bias`、`pdl`、`out`、`out_dtype`，以及 `backend` 参数。它的 docstring（[flashinfer/gemm/gemm_base.py:582-613](https://github.com/flashinfer-ai/flashinfer/blob/3fd5c55bc84bc00b27bed5099031fa3aab8a4fb2/flashinfer/gemm/gemm_base.py#L582-L613)）直接给了四种后端的调用示例，是上手 `mm_bf16` 最好的参考。

低精度入口对照看签名即可，不必深究字段：

- [flashinfer/gemm/gemm_base.py:4077-4084](https://github.com/flashinfer-ai/flashinfer/blob/3fd5c55bc84bc00b27bed5099031fa3aab8a4fb2/flashinfer/gemm/gemm_base.py#L4077-L4084)：`mm_fp8`，注意它目前**只**有 `trtllm_low_latency` 一个后端（为小 M 维优化），权重需 `prepare_low_latency_gemm_weights` 预处理成 `(k//block_size, n, block_size)`。
- [flashinfer/gemm/gemm_base.py:6341-6353](https://github.com/flashinfer-ai/flashinfer/blob/3fd5c55bc84bc00b27bed5099031fa3aab8a4fb2/flashinfer/gemm/gemm_base.py#L6341-L6353)：`mm_fp4`，后端选择更丰富（`cudnn/trtllm/cutlass/cute-dsl/b12x/auto`），并带 `block_size`、`use_8x4_sf_layout`、`use_nvfp4` 等缩放/格式旋钮。
- [flashinfer/gemm/gemm_base.py:6789-6799](https://github.com/flashinfer-ai/flashinfer/blob/3fd5c55bc84bc00b27bed5099031fa3aab8a4fb2/flashinfer/gemm/gemm_base.py#L6789-L6799)：`gemm_fp8_nt_groupwise`，FP8 groupwise 缩放的「标准」入口，`scale_granularity_mnk` 默认 `(1, 128, 128)`。
- [flashinfer/gemm/gemm_base.py:794-800](https://github.com/flashinfer-ai/flashinfer/blob/3fd5c55bc84bc00b27bed5099031fa3aab8a4fb2/flashinfer/gemm/gemm_base.py#L794-L800)：`bmm_bf16`，比 `mm_bf16` 多一个 batch 维 `b`，且后端集合更小（无 cublaslt/tinygemm）。

变长分组入口是 `SegmentGEMMWrapper`，它把 workspace 与运行解耦：构造时给 workspace，`run` 时传拼接输入与权重堆栈。[flashinfer/gemm/gemm_base.py:1931-1980](https://github.com/flashinfer-ai/flashinfer/blob/3fd5c55bc84bc00b27bed5099031fa3aab8a4fb2/flashinfer/gemm/gemm_base.py#L1931-L1980) 的 docstring 给了完整可运行示例（含按 `weight_indices` 复用权重的用法），本讲的综合实践之外，它也是 u5-l4 的起点。

#### 4.1.4 代码实践

**实践目标**：通过阅读 `__init__.py` 的导出表，亲手验证「形状前缀 × 精度后缀」的命名规律，并定位每个 API 在 `gemm_base.py` 里的真实定义行号。

**操作步骤**：

1. 打开 [flashinfer/gemm/\_\_init\_\_.py](https://github.com/flashinfer-ai/flashinfer/blob/3fd5c55bc84bc00b27bed5099031fa3aab8a4fb2/flashinfer/gemm/__init__.py)，把 `__all__`（第 89-118 行）里的符号抄成一张二维表：行 = 形状前缀（mm/bmm/group/segment），列 = 精度（bf16/fp8/fp4/mxfp8/nvfp4）。
2. 对 `mm_*` 这一行，用编辑器跳转每个函数到 `gemm_base.py`，记录它的 `def` 行号。
3. 比较 `mm_bf16` 与 `bmm_bf16` 的参数列表，找出 `bmm_bf16` **缺少**的参数（答案应包含 `bias`、`pdl`）。

**需要观察的现象**：

- 不是每个「形状 × 精度」组合都存在；比如没有 `mm_mxfp4`、也没有 `bmm_fp4`。这反映了**按需实现**——某组合在主流模型里用不到，就不提供。
- 低精度 API 普遍多出 `scale` / `descale` / `block_scale` 之类的缩放张量参数，而 BF16 没有。

**预期结果**：你会得到一张稀疏矩阵，空白处即「未实现」的组合。这是理解 GEMM 包「全景」最直观的方式。

**运行结果**：待本地验证（本实践为源码阅读型，不涉及执行）。

#### 4.1.5 小练习与答案

**练习 1**：`mm_bf16` 的 docstring 里，权重 `b` 要求 `(k, n)` 且列主序。如果直接 `b = torch.randn([k, n])`（行主序）传进去会怎样？

**参考答案**：结果数值会错（相当于乘了一个布局错误的矩阵）。正确写法是先按 `(n, k)` 生成再转置：`b = torch.randn([n, k]).transpose(-2, -1)`，这样 `.transpose` 不拷贝数据、只改 stride，`b` 在内存里就是 `(k, n)` 列主序，正合 `mm_bf16` 的 NT 约定。这也是为什么示例里都写 `.transpose(-2, -1)`。

**练习 2**：`bmm_bf16` 与 `mm_bf16` 相比，少了哪几个后端？少了哪些功能参数？

**参考答案**：少了 `cublaslt`、`tinygemm` 两个后端（见 4.2.3 中两者 `@backend_requirement` 的 backend 字典差异）；功能参数少了 `bias` 与 `pdl`。原因是批矩阵乘的典型场景（如注意力里的投影）通常不需要 bias 融合，且 cublaslt/tinygemm 主要服务单矩阵的小 M 场景。

---

### 4.2 backend_requirement 统一封装

#### 4.2.1 概念说明

如果说 4.1 讲的是「有哪些 API」，那么 4.2 讲的是「这些 API 如何被**统一**地管理」。GEMM 包面临一个工程难题：同一个 `mm_bf16` 要在 SM80 的 A100、SM90 的 H100、SM100 的 B200 上都能跑，且每个架构上「最优后端」不同（Hopper 上可能是 cuDNN 或 cuTile，Blackwell 上可能是 CUTLASS/TGV），还要支持用户显式指定后端或让框架自动选。

朴素做法是在每个 API 里写一堆 `if backend == "cutlass": ... elif ...`，再各自校验硬件。FlashInfer 把这套逻辑抽成了两个**声明式装饰器**，让每个 API 只需「声明」自己支持哪些后端、每个后端支持哪些架构与约束，校验与选择由装饰器统一完成：

- `@supported_compute_capability([100, 103])`：贴在「单个后端的约束函数」上，声明这个后端只能在哪些 compute capability 上跑。
- `@backend_requirement({...}, common_check=..., heuristic_func=...)`：贴在**对外 API**（如 `mm_bf16`）上，把所有后端的约束函数登记成一张表，并指定「跨后端公共校验」与「auto 模式的启发式排序」。

这与注意力 wrapper 用的老式 `determine_attention_backend`（u3-l5）形成对照——GEMM 用的是更新、更声明式的机制。装饰器还为 API 挂上三个**能力查询**方法：`is_backend_supported`、`is_compute_capability_supported`、`suitable_auto_backends`，让用户在调用前就能问「我这个 GPU 能不能跑 cutlass 后端」。

#### 4.2.2 核心流程

`@backend_requirement` 包装后的 API，在一次调用里会按下面顺序工作（无论 backend 是显式指定还是 `auto`）：

```
用户调用 mm_bf16(a, b, backend="cutlass")
        │
        ▼
@backend_requirement 的 wrapper 拦截（除非 skip_check=True）
        │
        ├─ 1. _get_capability：从第一个 torch.Tensor 参数取 device，
        │     get_compute_capability → cc = major*10 + minor
        │
        ├─ 2a. 若 backend == "auto":
        │     suitable_auto_backends(cc, ...)
        │       → 遍历每个后端约束函数，过滤出「通过校验 & cc 支持」的
        │       → 再用 heuristic_func 排序（偏好优先）
        │       → 结果存到 mm_bf16.suitable_auto_backends
        │
        ├─ 2b. 若 backend 显式指定:
        │     is_backend_supported(backend, cc)  → 不支持就抛 BackendSupportedError
        │     _is_problem_size_supported(...)    → 跑 common_check + 该后端约束
        │
        └─ 3. 校验全过 → 调用真正的 mm_bf16 函数体
```

关键在于**校验逻辑与函数体彻底分离**：函数体（如 `mm_bf16` 的第 616-662 行）只负责「分配 out、选 backends 列表、调用调度器」，完全不操心硬件兼容性——那都是装饰器在函数体执行**之前**保证好的。`skip_check=True` 可在确认安全的性能热路径上跳过校验。

#### 4.2.3 源码精读

先看 `mm_bf16` 头上的两层装饰器（[flashinfer/gemm/gemm_base.py:517-540](https://github.com/flashinfer-ai/flashinfer/blob/3fd5c55bc84bc00b27bed5099031fa3aab8a4fb2/flashinfer/gemm/gemm_base.py#L517-L540)）：外层 `@backend_requirement` 登记了 6 个后端及其约束函数，并指定 `common_check=_check_mm_bf16_problem_size`（所有后端共享的尺寸/dtype 校验）与 `heuristic_func=_heuristic_func_mm_bf16`（auto 排序）；内层 `@flashinfer_api(trace=mm_bf16_trace)` 是 API 日志与 trace（见 u10-l1/u9-l5）。注意装饰器是**自底向上**应用的，`@backend_requirement` 先把 `mm_bf16` 包成带校验的版本，`@flashinfer_api` 再包一层日志。

每个后端约束函数都用 `@supported_compute_capability` 声明自己的架构范围，并做该后端特有的参数校验。对照看 `mm_bf16` 的几个后端：

- [flashinfer/gemm/gemm_base.py:279-302](https://github.com/flashinfer-ai/flashinfer/blob/3fd5c55bc84bc00b27bed5099031fa3aab8a4fb2/flashinfer/gemm/gemm_base.py#L279-L302)：`_cutlass_mm_bf16_requirement`，`@supported_compute_capability([100, 103])`——CUTLASS BF16 GEMM 只在 Blackwell（SM100/103）上跑；且明确拒绝 `bias` 与 `pdl`（这俩得用 TGV 后端）。
- [flashinfer/gemm/gemm_base.py:306-326](https://github.com/flashinfer-ai/flashinfer/blob/3fd5c55bc84bc00b27bed5099031fa3aab8a4fb2/flashinfer/gemm/gemm_base.py#L306-L326)：`_cublaslt_mm_bf16_requirement`，架构范围最广（SM80 起全支持），同样拒绝 bias/pdl。
- [flashinfer/gemm/gemm_base.py:329-342](https://github.com/flashinfer-ai/flashinfer/blob/3fd5c55bc84bc00b27bed5099031fa3aab8a4fb2/flashinfer/gemm/gemm_base.py#L329-L342)：`_cudnn_mm_bf16_requirement`，cuDNN 后端，唯一额外做的是 `_cudnn_available_or_raise_for_backend(backend)`——检查外部 `cudnn` 包是否真的装了。
- [flashinfer/gemm/gemm_base.py:345-375](https://github.com/flashinfer-ai/flashinfer/blob/3fd5c55bc84bc00b27bed5099031fa3aab8a4fb2/flashinfer/gemm/gemm_base.py#L345-L375)：`_tgv_gemm_requirement`，TGV 后端只支持 `[100, 103]`，且若走默认的 CuTeDSL 实现会检查 `nvidia-cutlass-dsl` 是否安装。

把这几张架构表横着看，就能理解为什么「同一个 `mm_bf16` 在不同 GPU 上行为不同」：装饰器会按你机器的 `cc` 自动剔除不支持的后端。例如在 A100（cc=80）上，cutlass/tgv 会被过滤掉，只剩 cublaslt/cudnn/tinygemm。

`heuristic_func` 决定 `backend="auto"` 时的优先级。[flashinfer/gemm/gemm_base.py:482-514](https://github.com/flashinfer-ai/flashinfer/blob/3fd5c55bc84bc00b27bed5099031fa3aab8a4fb2/flashinfer/gemm/gemm_base.py#L482-L514)：若带 `bias`/`pdl`，只保留 tgv/cudnn；否则按 cutlass → tgv → cudnn → cublaslt 的顺序排，tinygemm 单独视 out_dtype 决定。这张「偏好序」会在后续调度器里被 AutoTuner 用来挑选最优 tactic。

装饰器本身的实现在 `utils.py`。[flashinfer/utils.py:930-1010](https://github.com/flashinfer-ai/flashinfer/blob/3fd5c55bc84bc00b27bed5099031fa3aab8a4fb2/flashinfer/utils.py#L930-L1010) 是 `supported_compute_capability`：它把传入的 `[100, 103]` 存成函数的 `_supported_ccs` 集合，并挂一个 `is_compute_capability_supported(cc)` 方法。注意它把 `cc` 折算成整数 `major*10 + minor`（SM 10.0 = 100、SM 8.0 = 80）。

[flashinfer/utils.py:1013-1017](https://github.com/flashinfer-ai/flashinfer/blob/3fd5c55bc84bc00b27bed5099031fa3aab8a4fb2/flashinfer/utils.py#L1013-L1017) 是 `backend_requirement` 的签名，三个参数 `backend_checks / common_check / heuristic_func` 对应「后端表 / 公共校验 / 启发式排序」。核心的 `suitable_auto_backends` 闭包在 [flashinfer/utils.py:1187-1207](https://github.com/flashinfer-ai/flashinfer/blob/3fd5c55bc84bc00b27bed5099031fa3aab8a4fb2/flashinfer/utils.py#L1187-L1207)：它遍历每个后端，对能通过 `req_checker(...)` 且 `req_checker.is_compute_capability_supported(cc)` 的，收进列表；失败的 `ValueError` 直接 `continue` 跳过（这正是「某后端在当前参数下不可用」被优雅忽略的原因）；最后用 `heuristic_func` 排序，并把结果挂到 `wrapper.suitable_auto_backends` 供函数体读取。

装饰器最后把三个查询方法挂到包装后的 API 上（[flashinfer/utils.py:1289-1293](https://github.com/flashinfer-ai/flashinfer/blob/3fd5c55bc84bc00b27bed5099031fa3aab8a4fb2/flashinfer/utils.py#L1289-L1293)）：`is_backend_supported`、`is_compute_capability_supported`、`has_backend`、`has_backend_choices`。所以你可以直接写 `mm_bf16.is_backend_supported("cutlass", 100)` 来做能力查询——这正是本讲综合实践要用到的。

作为对照，老式的 `determine_gemm_backend` 仍在 `utils.py` 中（[flashinfer/utils.py:379-384](https://github.com/flashinfer-ai/flashinfer/blob/3fd5c55bc84bc00b27bed5099031fa3aab8a4fb2/flashinfer/utils.py#L379-L384)）：它只是个朴素的 `if major == 9 ... else ...` 返回 `"sm90"/"sm80"` 字符串，被部分 JIT 生成器用来决定编译目标。新式 `@backend_requirement` 比它表达力强得多，是 GEMM/MoE/norm 等新 API 的首选。

#### 4.2.4 代码实践

**实践目标**：用装饰器挂上的能力查询方法，在不实际运行 kernel 的前提下，探测 `mm_bf16` 在「你本机 GPU」上支持哪些后端、哪些架构。

**操作步骤**：

1. 先拿到本机的 compute capability 整数：

```python
import torch
from flashinfer.utils import get_compute_capability

cc = sum(x * (10 if i == 0 else 1) for i, x in enumerate(get_compute_capability(torch.device("cuda"))))
# 等价于 cc = major * 10 + minor，例如 H100 → 90，B200 → 100
print("本机 cc =", cc)
```

2. 查询 `mm_bf16` 的能力：

```python
import flashinfer

# 是否支持某个 cc（任一后端支持即 True）
print("cc supported:", flashinfer.mm_bf16.is_compute_capability_supported(cc))

# 逐个后端查询：在当前 cc 下能否跑
for bk in ["cudnn", "cutlass", "tgv", "cublaslt", "tinygemm", "cutile"]:
    has = flashinfer.mm_bf16.has_backend(bk)                       # 该后端是否存在
    sup = flashinfer.mm_bf16.is_backend_supported(bk, cc)          # 且在 cc 下是否支持
    print(f"{bk:9s} has_backend={has}  supported_on_cc={sup}")
```

**需要观察的现象**：

- `has_backend` 只看后端名是否在装饰器的表里（与硬件无关），而 `is_backend_supported(bk, cc)` 还要叠加该后端的 `@supported_compute_capability` 判定。两者区别正是「声明了」与「能跑」的区别。
- 在 A100（cc=80）上，`cutlass`/`tgv` 的 `supported_on_cc` 应为 `False`；在 B200（cc=100）上应为 `True`。

**预期结果**：你会得到一张「后端 × 当前架构」的布尔表，它就是装饰器根据 `@supported_compute_capability` 自动算出来的支持矩阵。

**运行结果**：具体取值取决于本机 GPU，待本地验证；但「`has_backend` 与 `is_backend_supported(cc)` 的差异」这条现象在任何机器上都成立。

#### 4.2.5 小练习与答案

**练习 1**：`is_backend_supported("cutlass")`（不传 cc）与 `is_backend_supported("cutlass", 80)` 返回值分别是什么含义？

**参考答案**：不传 `cc` 时（见 [flashinfer/utils.py:1136-1137](https://github.com/flashinfer-ai/flashinfer/blob/3fd5c55bc84bc00b27bed5099031fa3aab8a4fb2/flashinfer/utils.py#L1136-L1137)），只要 `"cutlass"` 在 `backend_checks` 字典里就返回 `True`，回答的是「这个 API 有没有 cutlass 后端」；传 `cc=80` 后，会进一步调 `req_checker.is_compute_capability_supported(80)`，回答的是「cutlass 后端能不能在 SM80 上跑」。CUTLASS BF16 声明的是 `[100, 103]`，所以前者 `True`、后者 `False`。

**练习 2**：为什么 `mm_bf16` 的函数体里几乎看不到任何 `if cc == 90` 之类的硬件判断？

**参考答案**：因为硬件兼容性已被 `@backend_requirement` + `@supported_compute_capability` 在函数体执行**之前**校验完毕——能进入函数体的后端，必然在当前 cc 下可用。函数体只需把通过校验的 `backends` 列表交给调度器（见 4.3），职责清晰分离。这也是 `skip_check=True` 能安全跳过校验的前提：调用方已确认环境合法。

---

### 4.3 统一绑定：从 Python 到 C++ kernel 的数据流

#### 4.3.1 概念说明

前两节讲了「有哪些 API」和「它们怎么被装饰器管理」，这一节回答最后一个问题：**一次 `mm_bf16(a, b)` 调用，数据是怎么从 Python 的 `torch.Tensor` 走到 GPU 上真正的 CUDA kernel 的？** 这条链路就是「统一绑定」。

FlashInfer 的 GEMM 绑定链路有几个关键设计：

1. **统一调度器 + Runner 列表**：`mm_bf16` 不直接调某个后端，而是把「候选后端」转成一个 `TunableRunner` 对象列表（一个后端一个 runner），交给全局 `AutoTuner` 选最快的那个。这让「多后端」从「一堆 if-else」变成「一堆可比较的 runner」。
2. **JIT 模块作为后端实现**：每个 runner 内部持有一个 TVM-FFI 模块（由 `gen_*_module().build_and_load()` 得到，见 u2 单元），runner 的 `forward` 就是把张量喂给这个模块。
3. **TVM-FFI 跨语言 ABI**：C++ 侧的 kernel 用 `TVM_FFI_DLL_EXPORT_TYPED_FUNC` 导出，Python 经 TVM-FFI 路由过去，参数是框架无关的 `TensorView`（指针+形状+dtype）。这与注意力 wrapper 的 `plan/run/workspace_size` 三件套是同一套机制（u1-l3、u9-l2）。

理解这条链路后，你会明白：**改一个 GEMM kernel，改的是 `include/` 下的 `.cuh` 模板或 `csrc/` 下的 launcher；Python 侧的 `mm_bf16` 签名、装饰器、调度器通常不用动。**

#### 4.3.2 核心流程

BF16 GEMM 的完整调用链（cutile 后端是纯 Python，走单独支线）：

```
mm_bf16(a, b, backend="cutlass")          ← Python API（被 @backend_requirement 包过）
   │  1. 分配 out 张量
   │  2. 选 backends 列表（如 ["cutlass"]）
   ▼
bf16_gemm_sm100(a, b, bias, pdl, out, workspace, backends)   ← 统一调度器
   │  3. 据 backends 实例化 runner 列表：
   │       "cudnn"   → _cudnn_gemm_bf16_runner(...)
   │       "cublaslt"→ get_mm_bf16_cublaslt_module().cublaslt_bf16_gemm_runner()
   │       "cutlass" → get_gemm_sm100_module_cutlass_bf16().cutlass_bf16_gemm_runner()
   │       "tgv"     → _tgv_gemm_runner(...)
   │       "tinygemm"→ _tinygemm_bf16_gemm_runner()
   │     （每个 runner 内部 .build_and_load() 出 JIT 模块）
   │
   │  4. tuner.choose_one("bf16_gemm", runners, tuning_config, inputs)
   │       → AutoTuner 在 runners 间挑最快 tactic
   ▼
runner(inputs=inputs, tactic=tactic)      ← 调用选中的 runner.forward
   │  5. runner.forward → TVM-FFI 模块符号（如 module.bmm_fp8 / module.cutlass_...）
   ▼
TVM-FFI 路由：torch.Tensor → TensorView → C++ 函数
   │
   ▼
csrc/*.cu 里的 launcher（接受原始指针）→ include/flashinfer/gemm/*.cuh 的 CUDA kernel
```

注意第 3 步：调度器叫 `bf16_gemm_sm100`，但它**同时**处理 cudnn/cublaslt/cutlass/tgv/tinygemm 五个后端——名字里的 `sm100` 只是历史命名，真正决定跑哪个的是传入的 `backends` 列表。`mm_fp4`、`mm_mxfp8` 各有自己对应的调度器（如 `fp8_gemm_sm100`、`mxfp8_gemm_sm100`），结构同构。

#### 4.3.3 源码精读

入口在 `mm_bf16` 函数体末尾。[flashinfer/gemm/gemm_base.py:616-662](https://github.com/flashinfer-ai/flashinfer/blob/3fd5c55bc84bc00b27bed5099031fa3aab8a4fb2/flashinfer/gemm/gemm_base.py#L616-L662)：先分配 `out`（若未提供），`cutile` 后端是纯 Python 提前分流；其余路径取一个 32MB 的 workspace（`_get_cache_buf("mm_bf16_workspace", DEFAULT_WORKSPACE_SIZE, ...)`，`DEFAULT_WORKSPACE_SIZE = 32 * 1024 * 1024` 见第 109 行），把 `backend`（或 `"auto"` 时的 `suitable_auto_backends`）解析成 `backends` 列表，最后调 `bf16_gemm_sm100(a, b, bias, pdl, out, workspace_buffer, backends)`。

调度器 `bf16_gemm_sm100` 是理解「统一绑定」的核心（[flashinfer/gemm/gemm_base.py:1407-1453](https://github.com/flashinfer-ai/flashinfer/blob/3fd5c55bc84bc00b27bed5099031fa3aab8a4fb2/flashinfer/gemm/gemm_base.py#L1407-L1453)）：

- 它先捕获 `a`/`b` 的真实 stride（`is_a_k_major`/`is_b_k_major`），让 autotune profiling 用的图与运行时图一致。
- 然后按 `runner_names`（即 `backends`）逐个构造 runner——**一个后端对应一个 `TunableRunner`**。例如 `"cutlass"` 对应 `get_gemm_sm100_module_cutlass_bf16().cutlass_bf16_gemm_runner()`，`"cublaslt"` 对应 `get_mm_bf16_cublaslt_module().cublaslt_bf16_gemm_runner()`。
- 断言 `runners` 非空后，调 `tuner.choose_one("bf16_gemm", runners, _BF16_GEMM_SM100_TUNING_CONFIG, inputs)` 让 AutoTuner 挑选，最后 `runner(inputs=inputs, tactic=tactic)` 执行。
- 传给 `choose_one` 的 `_BF16_GEMM_SM100_TUNING_CONFIG`（[flashinfer/gemm/gemm_base.py:1286-1288](https://github.com/flashinfer-ai/flashinfer/blob/3fd5c55bc84bc00b27bed5099031fa3aab8a4fb2/flashinfer/gemm/gemm_base.py#L1286-L1288)）控制 AutoTuner 的 profiling 方式：它声明输入 `a` 的 M 维动态分桶（`dynamic_tensor_specs`），让一个 tactic 覆盖一段 token 数区间，而不必逐形状重测。自本次更新起，该配置新增了两个标志——`use_cuda_graph=True` 让 profiling 在 CUDA Graph 捕获下执行（消除 launch 抖动、测得更稳的 kernel 时间），`use_cold_l2_cache=True` 让 autotuner 为每次测量准备一组克隆输入缓冲做环形轮换以排空 L2、模拟冷缓存延迟（字段定义见 [flashinfer/autotuner.py:329-338](https://github.com/flashinfer-ai/flashinfer/blob/3fd5c55bc84bc00b27bed5099031fa3aab8a4fb2/flashinfer/autotuner.py#L329-L338)，环形缓冲实现见 `_prepare_input_tensors_with_batches`）。这两个标志只影响 **autotune 选 tactic 的过程**，不改变 `mm_bf16` 的对外行为与计算结果；代价是首次 autotune 略慢，换来更稳定的最优 tactic 选择。

`TunableRunner` 是一个统一接口：每个后端实现 `forward(inputs, tactic, ...)` 与（可选）`get_valid_tactics`。一个典型例子是 cuBLASLt FP8 runner——[flashinfer/gemm/gemm_base.py:135-223](https://github.com/flashinfer-ai/flashinfer/blob/3fd5c55bc84bc00b27bed5099031fa3aab8a4fb2/flashinfer/gemm/gemm_base.py#L135-L223) 定义在 `get_gemm_module()` 内的 `CublasFp8GemmRunner`，它的 `forward` 在 tactic≥0 时调 `module.bmm_fp8_run_with_algo(...)`、否则调 `module.bmm_fp8(...)`。这里的 `module` 正是 JIT 加载的 TVM-FFI 模块。

JIT 模块从哪来？看 `get_gemm_module()`（[flashinfer/gemm/gemm_base.py:131-133](https://github.com/flashinfer-ai/flashinfer/blob/3fd5c55bc84bc00b27bed5099031fa3aab8a4fb2/flashinfer/gemm/gemm_base.py#L131-L133)）：`module = gen_gemm_module().build_and_load()`——调用 `flashinfer/jit/gemm.py` 里的生成器（u2-l3 的五步模式），生成 `.cu`、用 ninja/nvcc 编出 `.so`、由 TVM-FFI 加载回 Python。整个函数被 `@functools.cache` 装饰，所以一个进程里只编译加载一次（u2-l5 两级缓存）。`get_gemm_sm100_module_cutlass_bf16()`、`get_mm_bf16_cublaslt_module()` 等都是同构的「生成器 + build_and_load + functools.cache」。

模块加载后，它的符号经 TVM-FFI 路由到 C++。绑定层就在 [csrc/flashinfer_gemm_binding.cu](https://github.com/flashinfer-ai/flashinfer/blob/3fd5c55bc84bc00b27bed5099031fa3aab8a4fb2/csrc/flashinfer_gemm_binding.cu)。文件先声明 C++ 函数签名（[csrc/flashinfer_gemm_binding.cu:19-32](https://github.com/flashinfer-ai/flashinfer/blob/3fd5c55bc84bc00b27bed5099031fa3aab8a4fb2/csrc/flashinfer_gemm_binding.cu#L19-L32)），如 `void bmm_fp8(TensorView A, TensorView B, ...)`、`void CutlassSegmentGEMM(...)`，再用宏导出（[csrc/flashinfer_gemm_binding.cu:34-37](https://github.com/flashinfer-ai/flashinfer/blob/3fd5c55bc84bc00b27bed5099031fa3aab8a4fb2/csrc/flashinfer_gemm_binding.cu#L34-L37)）：

```cpp
TVM_FFI_DLL_EXPORT_TYPED_FUNC(cutlass_segment_gemm, CutlassSegmentGEMM);
TVM_FFI_DLL_EXPORT_TYPED_FUNC(bmm_fp8, bmm_fp8);
TVM_FFI_DLL_EXPORT_TYPED_FUNC(bmm_fp8_get_algos, bmm_fp8_get_algos);
TVM_FFI_DLL_EXPORT_TYPED_FUNC(bmm_fp8_run_with_algo, bmm_fp8_run_with_algo);
```

这正是 runner 里 `module.bmm_fp8(...)`、`module.cutlass_segment_gemm(...)` 能在 Python 侧被调到的根本原因——TVM-FFI 把 `torch.Tensor` 自动 marshaling 成 `TensorView`，调到上面这些 C++ 函数。注意参数都是 `TensorView`（框架无关），不含任何 torch 类型，遵守 `include/` 与 `csrc/` 的框架分离红线（u1-l3）。

把整条链路串起来：`mm_bf16`（Python API）→ `bf16_gemm_sm100`（调度器，组 runner 列表）→ `AutoTuner.choose_one`（选最优）→ `runner.forward`（调 `module.xxx`）→ TVM-FFI → `csrc/flashinfer_gemm_binding.cu` 导出的 C++ 函数 → `include/flashinfer/gemm/*.cuh` 的 CUDA kernel。改 kernel 改 `include/`、改绑定改 `csrc/`、加后端改 `gemm_base.py` 的调度器与 runner，三者分层清晰。

#### 4.3.4 代码实践

**实践目标**：跟踪一次 `mm_bf16` 调用，亲眼看到它最终通过 TVM-FFI 调到的 C++ 符号，并理解「调度器为每个后端构造一个 runner」的结构。

**操作步骤**：

1. 阅读源码型跟踪（无需运行）：

   - 从 [flashinfer/gemm/gemm_base.py:661](https://github.com/flashinfer-ai/flashinfer/blob/3fd5c55bc84bc00b27bed5099031fa3aab8a4fb2/flashinfer/gemm/gemm_base.py#L661) 的 `bf16_gemm_sm100(...)` 调用进入调度器。
   - 在调度器 [flashinfer/gemm/gemm_base.py:1426-1442](https://github.com/flashinfer-ai/flashinfer/blob/3fd5c55bc84bc00b27bed5099031fa3aab8a4fb2/flashinfer/gemm/gemm_base.py#L1426-L1442) 里，确认「`runner_names` 里每个名字」都对应「`runners.append(...)` 一次」——即一对一映射。
   - 跳到 `CublasFp8GemmRunner` 的 `forward`（[flashinfer/gemm/gemm_base.py:189-221](https://github.com/flashinfer-ai/flashinfer/blob/3fd5c55bc84bc00b27bed5099031fa3aab8a4fb2/flashinfer/gemm/gemm_base.py#L189-L221)），看到它调 `module.bmm_fp8(...)`。
   - 在 [csrc/flashinfer_gemm_binding.cu:35](https://github.com/flashinfer-ai/flashinfer/blob/3fd5c55bc84bc00b27bed5099031fa3aab8a4fb2/csrc/flashinfer_gemm_binding.cu#L35) 找到 `TVM_FFI_DLL_EXPORT_TYPED_FUNC(bmm_fp8, bmm_fp8)`，确认 `module.bmm_fp8` 经此宏导出。

2. 若本机已装 FlashInfer 并有可用 GPU，可启用详细日志观察实际走向：

```bash
export FLASHINFER_LOGLEVEL=1   # 见 u10-l1：打印 API 名（不打印张量）
python -c "import torch, flashinfer; \
a=torch.randn(48,64,device='cuda',dtype=torch.bfloat16); \
b=torch.randn(80,64,device='cuda',dtype=torch.bfloat16).transpose(-2,-1); \
print(flashinfer.mm_bf16(a,b,backend='cublaslt').shape)"
```

**需要观察的现象**：

- 源码侧：调度器里 `runner_names` 与 `runners.append` 严格一一对应；`forward` 里出现的每个 `module.xxx` 都能在 `csrc/flashinfer_gemm_binding.cu` 或对应 `*_jit_binding.cu` 里找到 `TVM_FFI_DLL_EXPORT_TYPED_FUNC(xxx, ...)`。
- 运行侧（若执行）：首次调用会触发 JIT 编译（打印 ninja/nvcc 日志），第二次调用走缓存（u2 单元）。

**预期结果**：你能画出一张「Python API → 调度器 → runner → TVM-FFI 模块符号 → C++ 导出宏 → CUDA kernel」的完整链路图，且图上每个箭头都能在源码里指出具体行号。

**运行结果**：源码跟踪部分可立即完成；运行部分取决于本机是否有 GPU 与已装 FlashInfer，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：调度器函数名叫 `bf16_gemm_sm100`，但它处理 cublaslt（SM80 起就支持）。这矛盾吗？为什么？

**参考答案**：不矛盾。`sm100` 只是历史命名，函数体本身不强制要求 SM100——它只是按传入的 `backends` 列表构造 runner。是否能跑某个后端，由 `@backend_requirement` 在调用 `bf16_gemm_sm100` **之前**就依据各后端的 `@supported_compute_capability` 过滤好了。所以在 A100（cc=80）上，`backends` 里根本不会出现 cutlass/tgv，调度器只会为 cublaslt/cudnn 构造 runner。命名是历史包袱，行为以装饰器声明为准。

**练习 2**：如果想为 `mm_bf16` 新增一个后端 `"mybackend"`，按本讲理解，至少要改哪几处？

**参考答案**：至少四处——(1) `gemm_base.py` 里写一个 `_mybackend_mm_bf16_requirement` 约束函数（带 `@supported_compute_capability`）；(2) 把它登记进 `mm_bf16` 头上 `@backend_requirement` 的字典，并在 `heuristic_func` 里给出偏好序；(3) 在调度器 `bf16_gemm_sm100` 里加 `if "mybackend" in runner_names: runners.append(...)` 构造它的 runner（runner 内部 `.build_and_load()` 出 JIT 模块）；(4) 若用了新 C++ kernel，还要在对应 `*_binding.cu` 里加 `TVM_FFI_DLL_EXPORT_TYPED_FUNC` 导出。这正是 u9 单元「扩展 FlashInfer」的主题。

---

## 5. 综合实践

把三个模块串起来，完成下面这个贯穿本讲的小任务：**调用 `mm_bf16` 做一次真实的矩阵乘，核验结果正确，并用能力查询解释「为什么这个后端在你的机器上可用」。**

```python
import torch
import flashinfer
from flashinfer.utils import get_compute_capability

# ---- 第 1 步：探测本机架构 ----
major, minor = get_compute_capability(torch.device("cuda"))
cc = major * 10 + minor
print(f"本机 compute capability = {cc} (SM{major}.{minor})")

# ---- 第 2 步：用能力查询挑一个可用的后端 ----
# 候选按「优先尝试高性能后端」排序
candidates = ["cutlass", "tgv", "cudnn", "cublaslt", "tinygemm"]
chosen = None
for bk in candidates:
    if flashinfer.mm_bf16.has_backend(bk) and flashinfer.mm_bf16.is_backend_supported(bk, cc):
        chosen = bk
        break
assert chosen is not None, f"当前 cc={cc} 下没有任何 mm_bf16 后端可用"
print(f"选用后端: {chosen}")
# 解释：例如 cc=100 时 chosen 多半是 "cutlass"；cc=80 时多半是 "cublaslt"

# ---- 第 3 步：构造 NT 布局的输入并计算 ----
m, k, n = 48, 64, 80
a = torch.randn(m, k, device="cuda", dtype=torch.bfloat16)
b = torch.randn(n, k, device="cuda", dtype=torch.bfloat16).transpose(-2, -1)  # (k,n) 列主序

out = flashinfer.mm_bf16(a, b, backend=chosen)
print("输出 shape:", out.shape, "dtype:", out.dtype)   # 期望 torch.Size([48, 80]) bfloat16

# ---- 第 4 步：用 PyTorch 参考实现核验 ----
ref = torch.matmul(a.float(), b.float()).to(torch.bfloat16)
max_err = (out.float() - ref.float()).abs().max().item()
print(f"与 torch.matmul 最大绝对误差: {max_err:.4f}")    # bf16 下应为小量（~1e-2 量级）
assert out.shape == (m, n) and out.dtype == torch.bfloat16
```

**完成判定**：

- 你能解释第 2 步里 `has_backend` 与 `is_backend_supported(bk, cc)` 各自的含义（回顾 4.2.5 练习 1）。
- 你能回答：如果把 `backend=chosen` 改成 `backend="auto"`，`mm_bf16` 内部会走 `mm_bf16.suitable_auto_backends`（4.2.2 第 2a 步），最终由 `bf16_gemm_sm100` 里的 AutoTuner 在多个候选 runner 间挑最快的（4.3）。
- 你能画出这次调用从 `flashinfer.mm_bf16` 到 `csrc/flashinfer_gemm_binding.cu` 的完整链路（4.3.4）。

**运行结果**：具体后端选择与误差取决于本机 GPU；BF16 下误差应在 `1e-2` 量级以内（受 BF16 精度限制），待本地验证。

## 6. 本讲小结

- **GEMM 入口分三类**：`mm_*`（单矩阵乘）、`bmm_*`（批矩阵乘）、`group_*` / `SegmentGEMMWrapper`（变长分组）；每类再按精度分 `bf16/fp8/fp4/mxfp8` 支线，命名遵循「形状前缀 + 精度后缀 +（可选）缩放粒度」的规律，且不是所有组合都已实现。
- **统一封装靠两个装饰器**：`@supported_compute_capability` 声明单后端的架构范围，`@backend_requirement` 把所有后端登记成表并指定公共校验与 auto 启发式；它们把「能力查询」与「后端选择」从函数体里抽离，函数体只管计算。
- **能力查询可直接调**：每个被 `@backend_requirement` 包过的 API 都挂了 `is_backend_supported` / `is_compute_capability_supported` / `has_backend` 方法，可在不运行 kernel 的前提下探测支持矩阵。
- **统一绑定走调度器 + runner + TVM-FFI**：`mm_bf16` → `bf16_gemm_sm100`（为每个后端构造一个 `TunableRunner`）→ `AutoTuner.choose_one` 选最优 → `runner.forward` 调 JIT 模块符号 → TVM-FFI 路由到 `csrc/flashinfer_gemm_binding.cu` 的 `TVM_FFI_DLL_EXPORT_TYPED_FUNC` 导出的 C++ 函数 → CUDA kernel。
- **分层清晰**：改 kernel 改 `include/`、改绑定改 `csrc/`、加后端改 `gemm_base.py` 的装饰器表与调度器，三者职责分离。
- **与注意力的关系**：GEMM 用的是比注意力 wrapper 更新式、更声明式的 `@backend_requirement` 机制（对照 u3-l5 的 `determine_attention_backend`），是 MoE/norm/新式 API 的统一范式。

## 7. 下一步学习建议

本讲建立了 GEMM 包的全景与机制框架，后续讲义按精度支线逐层深入：

- **u5-l2（FP8 GEMM）**：深入 `mm_fp8` / `gemm_fp8_nt_groupwise` 的 per-tensor 与 groupwise 缩放，以及 CUTLASS fp8 后端 `include/flashinfer/gemm/fp8_gemm_cutlass.h`。建议先复习 4.2 的后端校验，理解 FP8 各后端的架构门控。
- **u5-l3（FP4 GEMM）**：进入 NVFP4/MXFP4 的 4 比特世界，理解 `mm_fp4` 的 block-scale 重排（interleave）与 cuDNN/CuTe-DSL 后端选择。
- **u5-l4（Grouped GEMM）**：以本讲的 `SegmentGEMMWrapper` 为起点，深入 `flashinfer/grouped_mm/core.py` 与 LoRA/多专家场景，以及 router gemm。
- **u5-l5（量化算子）**：`flashinfer/quantization/` 下的 fp8/fp4 量化与反量化，是 u5-l2/u5-l3 的前置数据准备。

如果想横向扩展到「如何新增一个 GEMM 后端」，可直接跳到第 9 单元（u9-l1 添加新算子、u9-l2 TVM-FFI 绑定），那里把本讲 4.3.5 练习 2 的「加后端」步骤展开成完整流程。性能与调优则见 u10-l2（autotuner）与 u10-l3（benchmarking），它们正好对应本讲调度器里的 `AutoTuner` 与 `bench_gpu_time`。
