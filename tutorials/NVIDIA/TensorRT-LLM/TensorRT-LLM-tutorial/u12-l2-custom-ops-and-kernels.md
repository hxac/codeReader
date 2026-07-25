# 自定义算子与内核

## 1. 本讲目标

在前面的讲义里（尤其 u6-l2 注意力后端家族、u10-l2 量化机制、u10-l4 CUDA Graph/torch.compile），我们反复提到一句话：模型代码从不直接调用 GPU kernel，而是调用形如 `torch.ops.trtllm.<name>(...)` 的「PyTorch 自定义算子（custom op）」。本讲就打开这个被反复引用却一直没拆开的黑盒。

学完本讲，你应当能够：

- 说清楚 `custom_ops` 这一层在 PyTorch 后端里扮演的「统一门面」角色，以及它把哪四种来源的内核统一成同一种调用形式。
- 区分四种内核实现来源（C++/CUDA、Triton、CuTe DSL、cuTile）的编写语言、构建时机、存放目录、注册方式与适用场景，并能为一个新算子选出合适的来源。
- 看懂 `torch_custom_ops.py` 里三种最常见的 op 形态：纯 Python op、`TunableRunner` + AutoTuner 外壳、多后端统一路由器。
- 区分「自己用 `@triton.jit` 写的单文件 Triton 内核」与「仓库根目录 `triton_kernels/` 这个 vendored 第三方高性能内核库」，不再把二者混为一谈。
- 按照 `docs/source/torch/adding_custom_kernels.md` 的「四产物」流程，独立跟踪（甚至起草）一个新内核的完整落链：kernel → binding → integration → tests。

## 2. 前置知识

本讲是「二次开发与扩展」单元（u12）的一篇，默认你已读过：

- **u2-l3（C++ 核心与 nanobind 绑定）**：知道 `cpp/` 是 C++/CUDA 加速层，经 nanobind 暴露；本讲会用到「Python 调度、C++ 加速」这条主线。
- **u6-l2（注意力后端家族）**：知道后端用 `support_*` 类方法声明能力、`mutates_args=()` 纯函数语义；本讲会把同样的契约推广到所有 custom op。
- **u10-l2（量化机制）**：见过 `torch.library.custom_op` 与 `mutates_args=()` 的写法，知道量化 op 挂在 `trtllm::` 命名空间。
- **u10-l4（CUDA Graph / torch.compile）**：理解为什么 op 必须注册 fake/meta 实现才能被 `torch.compile` 与 FakeTensor 形状推断接受——这是本讲反复强调的一条硬约束。

几个名词先统一：

- **custom op（自定义算子）**：注册在 `torch.ops.trtllm` 命名空间下的一个 PyTorch 算子，是模型代码与 GPU 内核之间唯一的正式调用通道。
- **kernel（内核）**：真正在 GPU 上跑的那段代码，可以是 `.cu` 里的 `__global__` 函数，也可以是 `@triton.jit` / `@cute.kernel` / `@ct.kernel` 装饰的 Python 函数。
- **binding（绑定）**：把 kernel 包成一个 custom op 的那层薄胶水——校验输入、取 stream、启动 kernel、向 PyTorch 注册算子。
- **fake / meta 实现**：一个不真正算数、只根据输入形状推导输出形状的 Python 函数，供 `torch.compile` 与 FakeTensor 使用。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| :--- | :--- |
| `docs/source/torch/adding_custom_kernels.md` | 官方「如何添加自定义内核」指南，是本讲的主干参考 |
| `tensorrt_llm/_torch/custom_ops/__init__.py` | custom_ops 包的聚合门面，按 availability flag 条件导出各流派 |
| `tensorrt_llm/_torch/custom_ops/cpp_custom_ops.py` | 为 C++ 内核集中注册 fake/meta 实现（`_register_fake()`） |
| `tensorrt_llm/_torch/custom_ops/torch_custom_ops.py` | 数量最多的一族 op：Triton 内核包装、TunableRunner 外壳、多后端路由 |
| `tensorrt_llm/_torch/custom_ops/fast_custom_op.py` | 低开销版的 `@torch.library.custom_op` 替代品 |
| `tensorrt_llm/_torch/custom_ops/cuda_tile_custom_ops.py` | cuTile 流派的 op 包装范例 |
| `tensorrt_llm/_torch/modules/swiglu.py` | 一个自写的 `@triton.jit` 单文件内核（被 custom op 包装） |
| `tensorrt_llm/_torch/cuda_tile_kernels/rms_norm.py` | cuTile 内核本体范例 |
| `triton_kernels/__init__.py` | 仓库 vendored 的第三方 Triton 内核库入口 |
| `tensorrt_llm/_common.py` | `_init()` 里加载 `libth_common.so` 并调用 `_register_fake()` |
| `cpp/tensorrt_llm/thop/IndexerKCacheScatterOp.cpp` | C++/CUDA 流派的绑定范例 |
| `cpp/tensorrt_llm/kernels/indexerKCacheScatter.cu` | C++/CUDA 流派的内核范例 |
| `tensorrt_llm/_torch/attention_backend/sparse/dsa.py` | 上述范例 op 的真实调用点 |

## 4. 核心概念与源码讲解

### 4.1 custom_ops 分层：把四种内核来源统一成一道门

#### 4.1.1 概念说明

TensorRT-LLM 的 PyTorch 后端有一句设计口诀：**模型代码永远不直接调 kernel，而是调 `torch.ops.trtllm.<name>(...)`**。这样做有三个好处：

1. **统一调用形式**：不管内核是 C++ 写的还是 Python 写的，模型代码看到的都是同一个 `torch.ops.trtllm.xxx` 接口，模型代码因此与内核实现语言解耦。
2. **可被 torch.compile / CUDA Graph 接管**：只要这个 op 注册了 fake 实现、且声明了 `mutates_args`，它就成了一个「PyTorch 可见的算子边界」，可以被 `torch.compile` 当作图里的节点、也可以被 piecewise CUDA Graph 当作切段边界（呼应 u10-l4）。如果模型代码直接调一个裸 CUDA 函数，编译器看不见它，整张图就断了。
3. **统一注册与发现**：所有 op 都挂在 `trtllm` 命名空间下，命名一致、行为一致（校验、stream、fake）。

`tensorrt_llm/_torch/custom_ops/` 这个包就是这道「门」。它把仓库里 **四种来源** 的内核统一包装成 `torch.ops.trtllm.<name>`：

| 来源 | 编写语言 | 何时编译 | 存放目录 | availability flag |
| :--- | :--- | :--- | :--- | :--- |
| **C++/CUDA**（cpp） | `.cu` / `.h` | wheel 构建期（CMake → nvcc） | `cpp/tensorrt_llm/kernels/` | 无（编进 `libth_common.so`） |
| **Triton**（torch） | `@triton.jit` Python | 运行期 JIT | `tensorrt_llm/_torch/modules/` 等各处 | 无（triton 是硬依赖） |
| **CuTe DSL**（cute-dsl） | `@cute.jit` / `@cute.kernel` Python | 运行期 JIT（进程内缓存） | `tensorrt_llm/_torch/cute_dsl_kernels/` | `IS_CUTLASS_DSL_AVAILABLE` |
| **cuTile**（cutile） | `cuda.tile` / `@ct.kernel` Python | 运行期 JIT（进程内缓存） | `tensorrt_llm/_torch/cuda_tile_kernels/` | `IS_CUDA_TILE_AVAILABLE` |

注意这里的 availability flag（可用性标志）：CuTe DSL 与 cuTile 都依赖各自的可选 Python 包，机器上没装就不能注册。所以这两个流派是「可选的」——缺了不影响主流程，调用方自己 fallback。

还有一个容易混的点：仓库根目录有一个独立的 `triton_kernels/` 包（带下划线），那是 **vendored 的第三方高性能内核库**，与「用 `@triton.jit` 自写的单文件 Triton 内核」是两回事，我们在 4.3 节专门讲。

#### 4.1.2 核心流程

不管哪种来源，定义一个新 op 都要产出 **四样东西**（官方指南的原话）：

1. **kernel**：内核源码本身（C++ 或 Python DSL）。
2. **binding**：一层薄包装，把 kernel 注册成 Torch op，校验输入，在正确的 stream 上启动它。
3. **integration**：在现有 forward 路径里用 `torch.ops.trtllm.<name>(...)` 接上，改动尽量小、尽量就地。
4. **tests**：一个对比 PyTorch 参考实现的单元测试。

整条流水线可以用下面这张图概括：

```text
四种 kernel 来源
  ├── C++/CUDA   ──┐
  ├── Triton     ──┤
  ├── CuTe DSL   ──┼──► binding（注册为 trtllm::<name>）──► torch.ops.trtllm.<name>(...)
  └── cuTile     ──┘                                          │
                                                              ├── fake/meta 实现（torch.compile 用）
                                                              └── 模型代码调用
```

注册的时机有一条铁律：**import 即注册**。Python 流派（Triton / CuTe / cuTile）靠 `@torch.library.custom_op` / `@torch.library.register_fake` 装饰器，模块被 import 的那一刻 op 就进了 `trtllm` 命名空间。C++ 流派靠 `TORCH_LIBRARY_FRAGMENT` + `TORCH_LIBRARY_IMPL` 宏，在 `libth_common.so` 被 `torch.classes.load_library(...)` 加载时注册。两者最终都汇聚到「调用 `_register_fake()` 把 C++ op 的 fake 也补上」这一步。

#### 4.1.3 源码精读

**门面：按 availability flag 条件导出。** custom_ops 包的 `__init__.py` 是一道聚合门面，无条件导出 C++ 与 Triton 流派，对 CuTe / cuTile 流派则用 `if IS_xxx_AVAILABLE:` 守卫：

[tensorrt_llm/_torch/custom_ops/__init__.py:50-79](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/custom_ops/__init__.py#L50-L79) —— CuTe DSL 与 cuTile 两族 op 仅在对应 availability flag 为真时才被 import 与导出；flag 为假时这些 op 根本不存在，调用方需自行回退。这段代码还顺带说明了一个细节：注释里写明 **attention / MLA 的 custom op 不在这里 re-export**，因为 custom_ops 不能依赖 modules.attention（会循环导入）。

**C++ op 的 fake 集中注册。** C++ 流派的内核在 `.so` 里、实现也在 C++，但它的 fake/meta 实现却写在 Python 侧，集中在 `cpp_custom_ops.py` 的一个函数里：

[tensorrt_llm/_torch/custom_ops/cpp_custom_ops.py:14-16](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/custom_ops/cpp_custom_ops.py#L14-L16) —— `_register_fake()` 是一个**定义但不立即调用**的函数，内部用一堆 `@torch.library.register_fake("trtllm::xxx")` 给每个 C++ op 绑上 fake。它之所以是个函数，是因为注册 C++ op 的 fake 必须在 `libth_common.so` 加载之后（否则 `trtllm` 命名空间里还没有这些 op）。

**调用 `_register_fake()` 的唯一位置。** 这个函数在哪被调？答案是在整个库的初始化函数 `_init()` 里：

[tensorrt_llm/_common.py:63-67](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_common.py#L63-L67) —— `_init()` 先用 `torch.classes.load_library(...)` 加载 `libth_common.so`（这一步把所有 C++ op 注册进 `trtllm` 命名空间，也把 `torch.classes.trtllm.*` 那些 TorchBind 类暴露出来），紧接着 `from ._torch.custom_ops import _register_fake` 并调用它，给刚注册的 C++ op 补上 fake。如果这一步失败，会抛出 `FATAL: Decoding operators failed to load`——这是排查「PyTorch 与 TRT-LLM ABI 不匹配」的经典报错。回顾 u2-l2：`import tensorrt_llm` 有副作用，调用的就是这个 `_init()`，所以**「import 即注册」对 C++ op 同样成立**，只是注册发生在 `_init()` 内部。

**官方四产物清单。** 整套流程的最佳权威说明就是指南本身：

[docs/source/torch/adding_custom_kernels.md:31-38](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/torch/adding_custom_kernels.md#L31-L38) —— 「Adding a new `torch.ops.trtllm.<name>` means producing four things: kernel / binding / integration / tests」，这正是 4.1.2 节那张图的文字来源。

#### 4.1.4 代码实践

**实践目标**：直观感受「import 即注册」与「四来源统一门面」。

**操作步骤**（本机无 GPU 也能做前两步的源码阅读部分；运行部分需 GPU）：

1. 在仓库根目录读 `tensorrt_llm/_torch/custom_ops/__init__.py`，数一下 `if IS_xxx_AVAILABLE:` 守卫的块各有几个，列出每个守卫块导出的 op 名。
2. 读 `tensorrt_llm/__common.py` 第 55–73 行，确认 `_register_fake()` 的调用夹在 `load_library` 之后、`MpiComm.local_init()` 之前。
3. （需 GPU，待本地验证）在有 GPU 与正确安装的环境里运行下面这段脚本，枚举 `trtllm` 命名空间下所有 op，并按「猜测来源」粗分类：

   ```python
   # 示例代码：枚举 trtllm 命名空间下的 custom op
   import tensorrt_llm  # 触发 _init()，注册全部 op
   import torch

   names = [n for n in dir(torch.ops.trtllm) if not n.startswith("_")]
   print(f"trtllm 命名空间下共 {len(names)} 个 op")
   # 例如：silu_and_mul / fused_moe / nvfp4_gemm / indexer_k_cache_scatter_op / cuda_tile_rms_norm ...
   ```

**需要观察的现象**：

- 步骤 1 应看到三个守卫块：`IS_FLASHINFER_AVAILABLE`、`IS_CUTLASS_DSL_AVAILABLE`、`IS_CUDA_TILE_AVAILABLE`。前两个对应「可选高性能库」，第三个对应 cuTile。
- 步骤 3 的输出里，`cuda_tile_rms_norm` 只有在 `IS_CUDA_TILE_AVAILABLE` 为真时才会出现；卸载 `cuda.tile` 后它会从列表里消失——这就是 availability flag 的可见效果。

**预期结果**：你会看到 `trtllm` 命名空间下有上百个 op，但它们的调用形式完全一致（都是 `torch.ops.trtllm.<name>(...)`），无法从调用点看出内核是 C++ 还是 Python 写的——这正是「统一门面」的目的。运行结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 CuTe DSL 和 cuTile 流派需要 availability flag，而 C++/CUDA 与 Triton 不需要？

**参考答案**：CuTe DSL（`cutlass.cute`）与 cuTile（`cuda.tile`）是可选的 Python 包，机器上可能没装；没装时对应的内核无法 JIT 编译，所以必须用 `if IS_xxx_AVAILABLE:` 守卫，缺了就跳过注册、由调用方回退。而 C++/CUDA 内核在 wheel 构建期就已编进 `libth_common.so`，Triton 则是 TRT-LLM 的硬依赖（`requirements.txt` 钉版），二者在运行环境中必然存在，故不需要守卫。

**练习 2**：`_register_fake()` 为什么是一个「定义后不立即调用」的函数，而不是模块顶层直接执行？

**参考答案**：因为它要给 C++ op 注册 fake，而 C++ op 此时必须已经存在于 `trtllm` 命名空间里——这要求 `libth_common.so` 已被 `torch.classes.load_library(...)` 加载。所以它必须推迟到 `_init()` 里「加载 .so 之后」才调用，不能在模块 import 顶层执行（那时 .so 还没加载，注册会失败）。

### 4.2 torch_custom_ops.py：三种 op 形态

#### 4.2.1 概念说明

`torch_custom_ops.py` 是 custom_ops 包里最大的一个文件，集中了数量最多的一族 op。读懂它你会发现，这里的 op 虽然都长成 `@torch.library.custom_op("trtllm::xxx")` 的样子，但按「内核从哪来」可以分成 **三种形态**：

1. **纯 Python op（包一个自写 Triton 内核）**：op 体里直接调一个 `@triton.jit` 内核。最典型的是 `silu_and_mul`，它包装 `modules/swiglu.py` 里的 `silu_and_mul_kernel`。这是「把一个 Python Triton 内核变成 custom op」的最短路径。

2. **`TunableRunner` + AutoTuner 外壳（包一个 C++ TorchBind 类）**：op 体里不直接调内核，而是构造一个 `TunableRunner` 子类（它内部持有 `torch.classes.trtllm.<XxxRunner>` 这个 C++ 对象），再交给 `AutoTuner` 在多个 tactic 里挑最优。最典型的是 `fp8_rowwise_gemm`、`fused_moe`。这是「C++ 高性能内核 + 运行期自动调优」的标准包装。

3. **多后端统一路由器**：一个 op 在内部维护「若干个 backend，每个 backend 是一个 TunableRunner」，按硬件能力与用户给的 `allowed_backends` 列表动态拼出候选 tactic 集，再交给 AutoTuner 选。最典型的是 `nvfp4_gemm`——它在 cutlass / cublaslt / cutedsl / cuda_core / marlin 五个后端里自动选。这正是 u10-l2 讲过的「按量化方案 × SM 代际匹配后端」在 op 层面的落地。

这三种形态是「由简到繁」的递进：形态 1 是纯 Python、形态 2 引入 C++ 与调优、形态 3 再加上多后端路由。几乎所有 `torch_custom_ops.py` 里的 op 都能归入这三类。

#### 4.2.2 核心流程

**形态 1（纯 Python / Triton）** 的执行流程：

```text
torch.ops.trtllm.silu_and_mul(x, ...)
   └─► @custom_op 装饰的 Python 函数
         └─► 分配输出张量 o
         └─► 计算 grid
         └─► silu_and_mul_kernel[grid](...)   # @triton.jit 内核，JIT 编译并启动
```

**形态 2（TunableRunner + AutoTuner）** 的执行流程：

```text
torch.ops.trtllm.fp8_rowwise_gemm(act, weight, ...)
   └─► @custom_op 函数
         ├─► 构造 FP8RowwiseGemmRunner(TunableRunner)
         │      └─► 内部持有 torch.classes.trtllm.FP8RowwiseGemmRunner（C++ 对象）
         ├─► AutoTuner.get().choose_one(...)   # 第一次跑会 profile 多个 tactic 并缓存最优
         └─► runner(inputs, tactic=best)        # 用选中的 tactic 调 C++ 内核
```

`TunableRunner` 是一个抽象基类，子类必须实现 `unique_id()`（缓存键）、`get_valid_tactics()`（返回哪些 tactic 合法）、`forward()`（用某个 tactic 真正跑）。`AutoTuner` 则负责：对每个 `(unique_id, 输入形状)` 组合，把所有合法 tactic 都试一遍、计时、把最优 tactic 持久化缓存；之后命中缓存就零开销直接跑。这套机制让「运行期自动调优」对模型代码完全透明——模型只看到一个普通的 op。

**形态 3（多后端路由）** 在形态 2 之上加一层：`get_valid_tactics()` 不再只返回 `[0,1,2,...]`，而是返回 `(backend_name, sub_tactic)` 二元组列表，每个 backend 探测自己的硬件能力（SM 版本、M 维度上限等），不满足就返回空。`forward()` 收到 `(backend, sub_tactic)` 后 dispatch 到对应 runner。

#### 4.2.3 源码精读

**形态 1：`silu_and_mul` 包装一个 Triton 内核。** 先看被包装的内核本体——一个标准的 `@triton.jit` 函数：

[tensorrt_llm/_torch/modules/swiglu.py:28-55](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/swiglu.py#L28-L55) —— `silu_and_mul_kernel` 是 `@triton.jit` 装饰的 device 内核，按 `program_id` 取一行，加载 `a` 与 `b` 两段，算 `sigmoid(a)*a*b`（即 SwiGLU），可选地带量化 scale 与上限。注意它的所有「配置」都用 `tl.constexpr`（`BLOCK_SIZE`、`HAS_O_SCALE`），这是 Triton 的编译期常量，会影响代码生成——所以缓存键必须包含它们。

再看 custom op 外壳：

[tensorrt_llm/_torch/custom_ops/torch_custom_ops.py:2040-2069](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/custom_ops/torch_custom_ops.py#L2040-L2069) —— `@torch.library.custom_op("trtllm::silu_and_mul", mutates_args=())` 把一个普通 Python 函数注册成 op；函数体里分配输出、算 grid、调内核。`mutates_args=()` 声明它是纯函数（不改输入），这是能进 torch.compile / CUDA Graph 的前提（呼应 u10-l4）。紧接着的 `@silu_and_mul.register_fake` 只根据 `x.shape` 推输出形状，不真正算数——这是 fake/meta 实现。

**形态 2：`fp8_rowwise_gemm` 与它的 TunableRunner。** 先看 runner 怎么封装一个 C++ TorchBind 类：

[tensorrt_llm/_torch/custom_ops/torch_custom_ops.py:457-510](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/custom_ops/torch_custom_ops.py#L457-L510) —— `FP8RowwiseGemmRunner(TunableRunner)`：`__init__` 里用 `torch.classes.trtllm.FP8RowwiseGemmRunner(...)` 创建 C++ 对象（这个类由 `libth_common.so` 的 TorchBind 暴露）；`get_valid_tactics` 返回 `range(get_num_configs())`；`forward` 调 C++ 的 `run_gemm`。`unique_id()` 决定 AutoTuner 的缓存粒度。

再看 op 外壳如何用 AutoTuner 串起来：

[tensorrt_llm/_torch/custom_ops/torch_custom_ops.py:513-552](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/custom_ops/torch_custom_ops.py#L513-L552) —— `fp8_rowwise_gemm` op 构造 runner、调 `tuner.choose_one(...)` 拿到最优 tactic、再用它跑。`register_fake` 只算输出形状 `[act.size(0), weight.size(0)]`。

**形态 3：`nvfp4_gemm` 多后端统一路由。** 这是最复杂也最能体现「统一门面」价值的形态。它的统一 runner 在 `get_valid_tactics` 里逐个探测 backend：

[tensorrt_llm/_torch/custom_ops/torch_custom_ops.py:1020-1143](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/custom_ops/torch_custom_ops.py#L1020-L1143) —— `NVFP4GemmUnifiedRunner.get_valid_tactics` 遍历 `marlin` / `cuda_core` / `cutlass` / `cublaslt` / `cutedsl` 五个 backend，每个都构造对应的 runner、探测硬件能力（如 `CudaCoreNVFP4Runner` 要求 SM≥100 且 M≤8，`MarlinNVFP4Runner` 要求 SM 90–99），满足才把 `(backend_name, tactic)` 加入候选；若用户只显式指定了某一个 backend 却不满足条件，则直接抛错（fail loud 而非静默回退）。这就是 u10-l2 所说「按量化方案 × SM 代际匹配后端」的实现现场。

op 外壳用一个字符串 `allowed_backends` 接收候选列表：

[tensorrt_llm/_torch/custom_ops/torch_custom_ops.py:1202-1246](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/custom_ops/torch_custom_ops.py#L1202-L1246) —— 注意这里用的不是 `@torch.library.custom_op`，而是 `@fast_custom_op`（见 4.2.4）。docstring 明确写出默认 `allowed_backends="cutlass,cublaslt,cuda_core"`，并提示「加 cutedsl 换极致性能但构建更慢」。

**低开销注册器 `fast_custom_op`。** 形态 3 用的 `@fast_custom_op` 是一个「 ergonomics 不变、开销更低」的替代品：

[tensorrt_llm/_torch/custom_ops/fast_custom_op.py:55-87](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/custom_ops/fast_custom_op.py#L55-L87) —— `fast_custom_op` 绕过 `@torch.library.custom_op` 每次调用约 6–7us 的 dispatcher 税（schema 重校验、DispatchKeySet 遍历等），改用底层 `torch.library.Library.define + impl`（与内置 ATen op 同路径）。对 `nvfp4_gemm` 这种每层、每步都被调用的热点 op，省下这 6–7us 很可观。它的 caveat 也列得很清楚：不支持 autograd、`mutates_args` 必须是具体元组、默认只注册 CUDA。

#### 4.2.4 代码实践

**实践目标**：学会用「三种形态」这把尺子去读 `torch_custom_ops.py`，并能解释每个 op 属于哪种。

**操作步骤**：

1. 在 `torch_custom_ops.py` 里定位下列 op，判断它们各属哪种形态，并各找一行证据：
   - `silu_and_mul`（形态 1）
   - `weight_only_quant_gemm`（形态 2，找它的 `WeightOnlyQuantGemmRunner` 与 `tuner.choose_one`）
   - `nvfp4_gemm`（形态 3，找它的 `allowed_backends` 与 `NVFP4GemmUnifiedRunner`）
2. 对比 `silu_and_mul`（用 `@torch.library.custom_op`）与 `nvfp4_gemm`（用 `@fast_custom_op`）的装饰器差异，结合 `fast_custom_op.py` 的 docstring 说明为什么热点 op 要换用后者。
3. 找到 `FP8RowwiseGemmRunner`（[torch_custom_ops.py:457-483](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/custom_ops/torch_custom_ops.py#L457-L483)），说明 `torch.classes.trtllm.FP8RowwiseGemmRunner(...)` 这一行里的 `torch.classes.trtllm` 是从哪来的（提示：回顾 4.1.3 的 `load_library`）。

**需要观察的现象**：

- 形态 1 的 op 体里能看到 `xxx_kernel[grid](...)` 这样的 Triton 启动调用；形态 2/3 的 op 体里看不到任何 kernel 启动，只看到 `runner(...)` 与 `tuner.choose_one(...)`。
- 三种形态的 `register_fake` 都只算形状、不真正算数。

**预期结果**：你会确认「三种形态」这个分类能覆盖文件里绝大多数 op；遇到新 op也能秒判它属于哪类。源码阅读型实践，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：`silu_and_mul` 的 Triton 内核里 `BLOCK_SIZE` / `HAS_O_SCALE` 为什么要用 `tl.constexpr`？这对 AutoTuner 缓存有什么影响？

**参考答案**：`tl.constexpr` 是 Triton 的编译期常量，会直接影响生成的 PTX 代码（不同的 `BLOCK_SIZE` 编出不同的 kernel）。因此任何包含这些值的缓存键都必须把它们算进去，否则同一段缓存会喂给配置不同的调用，结果出错。这正是指南 4.4 节「JIT compile-cache key bugs」警告的典型场景。

**练习 2**：形态 2 与形态 3 都用 AutoTuner，二者 `get_valid_tactics` 的返回类型有什么本质区别？

**参考答案**：形态 2 返回 `List[int]`（tactic 编号），因为只有一个固定 backend；形态 3 返回 `List[Tuple[str, int]]`（`(backend_name, sub_tactic)`），因为要在多个 backend 间选。形态 3 的 `forward` 收到 tactic 后要先解包 `backend, sub_tactic = tactic` 再 dispatch 到对应 runner。

**练习 3**：什么情况下应优先用 `@fast_custom_op` 而不是 `@torch.library.custom_op`？

**参考答案**：当 op 是「无 autograd 需求的推理热点、每步每层都调」（如 GEMM）时，`@fast_custom_op` 能省下每次调用约 6–7us 的 dispatcher 税，值得用。代价是放弃 autograd 支持、`mutates_args` 必须是具体元组。如果 op 不在热点路径，或需要 autograd，仍用标准的 `@torch.library.custom_op`。

### 4.3 triton_kernels：vendored 第三方高性能内核库

#### 4.3.1 概念说明

这一节专门澄清一个极易混淆的点。本仓库里存在 **两个** 都叫「triton」的东西：

1. **自写的 `@triton.jit` 单文件内核**：散落在 `tensorrt_llm/_torch/` 各处（如 `modules/swiglu.py` 的 `silu_and_mul_kernel`）。它们是 TRT-LLM 自己写的、用 `import triton` + `@triton.jit`，由 custom op 包装后挂在 `trtllm::` 命名空间。这是 4.2 节形态 1。

2. **仓库根目录的 `triton_kernels/` 包**：一个 **vendored（内嵌）的第三方高性能内核库**，从上游 Triton 项目（`triton-lang/triton`）的 `python/triton_kernels/` 目录原样拷贝而来。它是一个完整的 Python 包，提供 `matmul_ogs`（ grouped/SplitK GEMM）、`swiglu`、`topk`、`reduce`、`compaction` 等通用高性能内核，面向**跨项目复用**，不是 TRT-LLM 专属。

二者的关系：前者（自写单文件）是「为某个具体 op 临时写的小 Triton kernel」；后者（vendored 包）是「别人写好、经过广泛调优、可直接 import 的一整套内核库」。TRT-LLM 把后者内嵌进仓库，是为了在不强依赖上游包发布节奏的前提下，稳定地用到这些高性能内核（尤其在某些 MoE / 量化路径里）。

`triton_kernels/` 里的内核**不**挂在 `trtllm::` 命名空间——它们是普通的 Python 函数/类，调用方直接 `from triton_kernels.matmul_ogs import matmul_ogs` 来用。这与 custom_ops 包里的 op 形成对照：custom_ops 是「PyTorch 算子边界」（可被 torch.compile 看到），而 `triton_kernels/` 是「可直接调用的内核库」（是否包成 custom op 由调用方决定）。

#### 4.3.2 核心流程

`triton_kernels/` 的使用模式通常是：

```text
调用方代码
   └─► from triton_kernels.matmul_ogs import matmul_ogs   # 直接 import 第三方内核
         └─► matmul_ogs(x, w, ...)                          # 内部用 @triton.jit 内核 + autotune
```

它内部的执行与 4.2 节形态 1 类似（都是 Triton JIT），但有两点不同：

- 它自带一套更重的 autotune / layout 推导机制（见 `matmul_ogs_details/`、`tensor_details/` 子包），面向「任意形状的通用 GEMM」而非「某个固定 op」。
- 它由上游 Triton 项目维护，TRT-LLM **不修改**（文件头明确写 `DO NOT EDIT THIS FILE DIRECTLY`），升级靠换 vendor 版本。

#### 4.3.3 源码精读

**vendored 声明。** `triton_kernels/` 的包入口文件头清清楚楚写明来源与「禁止直接编辑」：

[triton_kernels/__init__.py:1-3](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/triton_kernels/__init__.py#L1-L3) —— 「This file is vendored from the Triton project. DO NOT EDIT THIS FILE DIRECTLY. Source: https://github.com/triton-lang/triton/tree/v3.6.0/...」。这几行注释是判断「这是 vendored 第三方库」的决定性证据：固定到上游 v3.6.0 版本，MIT 协议。

**库的组成。** 从本讲源码地图与目录结构可见，`triton_kernels/` 是个像模像样的大包：`matmul_ogs.py`（主要 GEMM）、`swiglu.py`、`topk.py`、`reduce.py`、`compaction.py`、`distributed.py`，外加一堆 `*_details/` 子包做布局推导（`tensor_details/layout.py`、`numerics_details/`）。这与 `modules/swiglu.py` 里那个几十行的单文件 `silu_and_mul_kernel` 形成鲜明对比——前者是重型通用库，后者是轻量专用 kernel。

**与 custom_ops 的对照。** 回顾 4.1.3：custom_ops 包把内核统一成 `torch.ops.trtllm.<name>`；而 `triton_kernels/` 的函数不走这道门，它们是「裸」的 Python 入口。这意味着：如果一个模型想用 `triton_kernels/` 的某个内核，又想让 torch.compile 看见它，调用方需要**自己**再包一层 custom op（这正是 adding_custom_kernels 指南 4.3 节描述的「wrapping the JIT kernel as a Torch custom op」流程）。

#### 4.3.4 代码实践

**实践目标**：从仓库里找出 `triton_kernels/` 的真实消费者，验证「它是被直接 import 的第三方库」。

**操作步骤**：

1. 在仓库根目录执行（用你的搜索工具）查找谁 import 了 `triton_kernels`，例如搜索 `from triton_kernels` 与 `import triton_kernels`。
2. 列出 `triton_kernels/` 顶层提供的内核模块名（`matmul_ogs`、`swiglu`、`topk`、`reduce`、`compaction`、`distributed`）。
3. 对比两处「swiglu」：`triton_kernels/swiglu.py`（vendored 库）与 `tensorrt_llm/_torch/modules/swiglu.py`（自写单文件），说明它们的角色差异。

**需要观察的现象**：

- 步骤 1 应发现消费者数量不多，且集中在少数 MoE / 量化 / 通信路径（具体位置待本地确认）。
- 步骤 3 应看到：`modules/swiglu.py` 的内核被 custom op 包装成 `trtllm::silu_and_mul`；`triton_kernels/swiglu.py` 则是一个独立的、带 `*_details/_swiglu.py` 子模块的通用实现，不被 `trtllm::` 命名空间直接收纳。

**预期结果**：你能用一句话向外人解释「`triton_kernels/` 是 vendored 的上游 Triton 内核库，与 TRT-LLM 自写的 `@triton.jit` 内核是两回事」。源码阅读型实践。

#### 4.3.5 小练习与答案

**练习 1**：如果发现 `triton_kernels/` 里某个内核有 bug，正确的修复流程是什么？

**参考答案**：**不能**直接改 `triton_kernels/` 里的文件（文件头明确 `DO NOT EDIT`）。正确做法是去上游 `triton-lang/triton` 仓库修 bug、等合并发版后，再把新版（如 v3.7.0）重新 vendor 进来、更新文件头的来源链接与版本号。

**练习 2**：为什么 `triton_kernels/` 的函数不像 custom_ops 那样自动出现在 `torch.ops.trtllm.*`？

**参考答案**：因为它只是「被 vendored 进来的普通 Python 库」，没有人给它写过 `@torch.library.custom_op("trtllm::...")` 绑定。它不是 PyTorch 算子边界，所以 torch.compile 看不见它。若要在编译图里用，调用方需按指南 4.3 节自己包一层 custom op。

### 4.4 添加内核流程：四产物与完整 walkthrough

#### 4.4.1 概念说明

把前面三节合起来，就得到了「添加一个新内核」的完整方法论。官方指南把它分成两条路径：

- **CUDA 自定义内核路径（C++/CUDA 流派）**：内核在 wheel 构建期编进 `.so`，适合「需要极致性能、算子成熟稳定、愿意走完整 CMake 构建流程」的场景。
- **CuTe DSL / cuTile JIT 路径（Python DSL 流派）**：内核用 Python 写、运行期 JIT，适合「快速迭代、不想重编 wheel、面向 Blackwell（sm_100+）新特性」的场景。

两条路径都要产出「四产物」（kernel / binding / integration / tests），只是 binding 的写法不同：C++ 路径用 `TORCH_LIBRARY_FRAGMENT` + `TORCH_LIBRARY_IMPL` 宏写在一个 `.cpp` 里；JIT 路径用 `@torch.library.custom_op` 写在一个 Python 文件里。

选哪条路？一个实用的决策顺序（综合指南与 `torch_custom_ops.py` 的实际做法）：

1. **先扫现有内核**：`cpp/tensorrt_llm/kernels/` 与 `tensorrt_llm/_torch/` 下有没有接近的。复用永远优先于新写。
2. **能纯 Python 就别上 C++**：如果用 Triton / CuTe / cuTile 能表达、且性能可接受，优先 Python DSL（迭代快、不需重编 wheel）。
3. **必须 C++ 时再 C++**：只有当内核需要 C++ 才能表达（如与硬件特性深度耦合、或 Python DSL 表达不了），或性能要求使然，才走 CUDA C++ 路径。
4. **热点 op 上 `fast_custom_op`**：若 op 在每步每层都被调，用 `@fast_custom_op` 替代 `@torch.library.custom_op` 省开销。

#### 4.4.2 核心流程

**C++/CUDA 路径**（四产物落点）：

```text
1. kernel:    cpp/tensorrt_llm/kernels/MyKernel.{h,cu}      # 被 CMake glob 自动收集
2. binding:   cpp/tensorrt_llm/thop/MyKernelOp.cpp          # 需手动加入 thop/CMakeLists.txt 的 th_common
3. fake:      tensorrt_llm/_torch/custom_ops/cpp_custom_ops.py 的 _register_fake() 里加一个
                @torch.library.register_fake("trtllm::my_kernel_op")
4. integration: 调用点写 torch.ops.trtllm.my_kernel_op(...)
5. tests:     tests/unittest/_torch/... 下加单测，对比 PyTorch 参考
```

**JIT 路径（以 cuTile 为例）**（四产物落点）：

```text
1. kernel:    tensorrt_llm/_torch/cuda_tile_kernels/my_kernel.py   # @ct.kernel，gate 在 IS_CUDA_TILE_AVAILABLE
2. binding+fake: tensorrt_llm/_torch/custom_ops/cuda_tile_custom_ops.py
                   @torch.library.custom_op("trtllm::my_kernel", mutates_args=())
                   + @my_kernel.register_fake
3. export:    custom_ops/__init__.py 的 if IS_CUDA_TILE_AVAILABLE: 块里 re-export
4. integration: 调用点写 torch.ops.trtllm.my_kernel(...)
5. tests:     同上
```

两条路径的关键差异：C++ 路径的「binding」与「fake」分处 C++ 与 Python 两地、且必须改两处 CMake（kernels 是 glob、thop 是显式列表）；JIT 路径的「binding」与「fake」都在同一个 Python 文件里、不需要改 CMake。

#### 4.4.3 源码精读

**C++ 路径完整 walkthrough：`indexer_k_cache_scatter_op`。** 指南用一个真实 op 串起整条链。它把 DeepSeek FP8 的 indexer K-cache 条目（逐 token 量化 key + 逐 block scale）一次 scatter 进分页 K-cache。

kernel 与 binding 的文件位置：

[docs/source/torch/adding_custom_kernels.md:289-294](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/torch/adding_custom_kernels.md#L289-L294) —— 指南列出四产物的真实落点：kernel 在 `cpp/tensorrt_llm/kernels/IndexerKCacheScatter.{h,cu}`（被 `kernels/CMakeLists.txt` 的 `GLOB_RECURSE *.cu` 自动收集）；binding 在 `cpp/tensorrt_llm/thop/IndexerKCacheScatterOp.cpp`（需手动加进 `thop/CMakeLists.txt` 的 `th_common` 库，因为 thop 目录**不**用 glob）；integration 在 sparse 注意力后端 `dsa.py`；tests 在 `tests/unittest/_torch/attention/sparse/test_dsa_indexer.py`。

**integration 的真实调用点。** 上面说的 integration 在 `dsa.py` 的 `_update_k_cache` 里，正是下面这三行：

[tensorrt_llm/_torch/attention_backend/sparse/dsa.py:2296-2299](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/sparse/dsa.py#L2296-L2299) —— 模型代码侧只看到 `torch.ops.trtllm.indexer_k_cache_scatter_op(...)` 这一个调用，完全看不出里面是 C++ 内核。注释说明 C++ op 内部把 FP8 张量与 float32 scale 当原始字节读，避开 Python 侧 view/slice 开销。这就是「统一门面」的样子：调用点干净、内核复杂性被封装。

**thop CMake 的显式列表。** 指南反复强调 thop 目录不用 glob，必须显式登记：

`cpp/tensorrt_llm/thop/CMakeLists.txt` 第 114 行就是 `IndexerKCacheScatterOp.cpp` 这一项（[cpp/tensorrt_llm/thop/CMakeLists.txt:114](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/cpp/tensorrt_llm/thop/CMakeLists.txt#L114)）。忘加这一行就会得到「undefined reference」或「no implementation for op」错——指南第 7 节把它列为常见错误。

**kernels CMake 的 glob。** 相对地，kernels 目录是自动收集的：

[cpp/tensorrt_llm/kernels/CMakeLists.txt:36-37](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/cpp/tensorrt_llm/kernels/CMakeLists.txt#L36-L37) —— `file(GLOB_RECURSE SRC_CPP *.cpp)` 与 `file(GLOB_RECURSE SRC_CU *.cu)`，所以新加的 `.cu` 文件会被自动纳入编译，无需登记。

**JIT 路径完整 walkthrough：cuTile 版 RMSNorm。** 对照地看 Python DSL 路径。内核本体：

[tensorrt_llm/_torch/cuda_tile_kernels/rms_norm.py:22-32](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/cuda_tile_kernels/rms_norm.py#L22-L32) —— `@ct.kernel def rms_norm_kernel(...)`，用 `cuda.tile` 的 `ct.bid` / `ct.load` / `ct.store` 原语写设备代码，配置项用 `ct.Constant[...]`。整个文件 gate 在 `if IS_CUDA_TILE_AVAILABLE:`。

binding + fake 都在同一个 Python 文件里：

[tensorrt_llm/_torch/custom_ops/cuda_tile_custom_ops.py:35-114](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/custom_ops/cuda_tile_custom_ops.py#L35-L114) —— `@torch.library.custom_op("trtllm::cuda_tile_rms_norm", mutates_args=())` 注册 op，函数体里调 `ct.launch(torch.cuda.current_stream(), grid, rms_norm_kernel, (...))` 启动 JIT 内核；`@cuda_tile_rms_norm.register_fake` 紧随其后给 fake。注意 op 体里先 `x.contiguous()` 再算——这正是指南第 7 节「别假设连续性」警告的正面示范。

**「常见错误」清单。** 指南第 7 节把踩坑点列得很全，值得逐条记住：

[docs/source/torch/adding_custom_kernels.md:340-349](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/torch/adding_custom_kernels.md#L340-L349) —— 漏注册（op 不可见或模块没被 import）、假设连续性（paged KV cache 切片是非连续 view）、漏加 thop CMake 项、schema 与签名不符（`Tensor(a!)` 标注 in-place 输出）、漏 dtype/device/contiguity 校验、漏 fake 注册（导致 torch.compile 崩）、JIT 缓存键不全（漏 dtype 等会埋雷）。

#### 4.4.4 代码实践（本讲的综合代码实践）

**实践目标**：按「四产物」流程，为假想的新算子选择实现来源并起草骨架，检验你是否真的吃透了本讲。这也是本讲规格指定的实践任务。

**操作步骤**：

1. **场景设定**：假设你要加一个新算子 `my_quantize_and_pack`，输入 `[M, K]` 的 bf16 张量，输出 FP8 张量 + 一份 swizzled scale，行为类似已有的 `trtllm::fp8_quantize_1x128`（可参考 [cpp_custom_ops.py:691-701](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/custom_ops/cpp_custom_ops.py#L691-L701) 的 fake 形状推导）。目标硬件是 Blackwell（sm_100+），且你希望快速迭代、不想每次改 kernel 都重编 wheel。

2. **选来源**：根据 4.4.1 的决策顺序写下你的选择与理由。参考答案见下方。

3. **起草骨架（示例代码，非项目原有代码）**：按你选的来源，写出四产物的文件落点与最小骨架。下面给一份 cuTile 路径的骨架示范：

   ```python
   # 示例代码：tensorrt_llm/_torch/cuda_tile_kernels/my_quantize_and_pack.py
   from ..cuda_tile_utils import IS_CUDA_TILE_AVAILABLE
   if IS_CUDA_TILE_AVAILABLE:
       import cuda.tile as ct

       @ct.kernel
       def my_quantize_kernel(x, y, sf, K: ct.Constant[int], TILE: ct.Constant[int]):
           # ... device 代码：读 x、算 absmax、量化、写 y/sf ...
           pass
   ```

   ```python
   # 示例代码：tensorrt_llm/_torch/custom_ops/cuda_tile_custom_ops.py（追加）
   @torch.library.custom_op("trtllm::my_quantize_and_pack", mutates_args=())
   def my_quantize_and_pack(x: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
       x = x.contiguous()                      # 不假设连续性
       y = torch.empty_like(x, dtype=torch.float8_e4m3fn)
       # sf 形状参考 fp8_quantize_1x128 的 fake 推导
       sf = ...                                 # 待按真实布局计算
       ct.launch(torch.cuda.current_stream(), (x.shape[0],),
                 my_quantize_kernel, (x, y, sf, x.shape[1], 128))
       return y, sf

   @my_quantize_and_pack.register_fake
   def _(x: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
       # 只算形状，不真正算数；具体形状待本地验证
       return (x.new_empty(x.shape, dtype=torch.float8_e4m3fn),
               x.new_empty((x.shape[0], ), dtype=torch.float32))
   ```

   ```python
   # 示例代码：custom_ops/__init__.py 的 if IS_CUDA_TILE_AVAILABLE: 块里追加
   from .cuda_tile_custom_ops import my_quantize_and_pack
   __all__ += ['my_quantize_and_pack']
   ```

4. **自检**：对照指南第 7 节的常见错误清单，逐条检查你的骨架：是否注册了？是否假设连续性？是否给了 fake？JIT 缓存键是否包含影响代码生成的所有参数？

**参考答案（选来源）**：应选 **cuTile（或 CuTe DSL）JIT 路径**。理由：(a) 目标是 Blackwell sm_100+，正好命中 cuTile / CuTe DSL 的支持范围；(b) 要求快速迭代、不想重编 wheel，排除 C++/CUDA 路径；(c) 算子是逐 token 量化、规则明确，Python DSL 完全可表达，无需 C++。选 Triton 也可接受（同样是 JIT、纯 Python），但若想用到 Blackwell 的 TMA 等新特性，cuTile/CuTe DSL 更贴近硬件。

**需要观察的现象 / 预期结果**：你能对着骨架说清每一产物落在哪个文件、为什么 fake 必须有、`mutates_args=()` 的意义、以及为什么没选 C++ 路径。骨架本身不含可运行内核（`pass`/`...`），属源码阅读 + 设计型实践，真正的 kernel 实现与性能待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：C++ 路径里，为什么 kernel 文件（`kernels/*.cu`）不用登记 CMake，而 binding 文件（`thop/*.cpp`）必须登记？

**参考答案**：`kernels/CMakeLists.txt` 用 `file(GLOB_RECURSE *.cu *.cpp)` 自动收集所有源文件；而 `thop/CMakeLists.txt` 维护 `th_common` 库时**显式列出**每个 `.cpp`（不用 glob）。所以新加 kernel 文件会被自动编译，新加 binding 文件则必须手动加进 `thop/CMakeLists.txt`，否则链接时找不到符号。这是指南第 7 节「Missing the thop CMake entry」错误 的根因。

**练习 2**：你在 C++ 路径里写了一个返回新张量的 op，但忘了在 `cpp_custom_ops.py` 的 `_register_fake()` 里加 fake。会在什么场景下出问题？

**参考答案**：直接 eager 调用没问题（fake 只在形状推断时用）。但一旦这个 op 出现在被 `torch.compile` 追踪的图里、或被 FakeTensor 形状推断碰到，就会报「no meta implementation」之类的错，piecewise CUDA Graph 也会抓不住它（呼应 u10-l4）。指南第 7 节明确：「Ops that don't have a fake registered will break under torch.compile」。

**练习 3**：指南建议「integration 遵循 minimal, in-place change 原则」。结合 `indexer_k_cache_scatter_op` 在 `dsa.py` 的接入方式，说明这条原则具体指什么。

**参考答案**：指在 `nn.Module` 的现有 forward 里、找到 op 所处的边界、用 `torch.ops.trtllm.xxx(...)` **就地替换**原来那段逻辑（这里是替换原先的 Python scatter 循环），并尽量保留一个 fallback；如果有现成 op 处在类似边界，就照它的接入方式来。`dsa.py:2296` 正是「把一段 Python 散列循环替换成一个 op 调用」的最小改动，周边代码几乎不动。

## 5. 综合实践

把本讲四节串起来，设计一个「**算子溯源**」小任务，检验你是否能独立运用本讲的全部概念。

**任务**：在 `torch.ops.trtllm` 命名空间里任选三个 op（建议一个来自 `torch_custom_ops.py`、一个是 C++ op、一个是 cuTile/CuTe op，例如 `silu_and_mul`、`indexer_k_cache_scatter_op`、`cuda_tile_rms_norm`），为每个 op 产出一张「溯源表」，包含以下字段：

| 字段 | 说明 |
| :--- | :--- |
| 来源流派 | cpp / torch(triton) / cute-dsl / cutile 之一 |
| 内核位置 | kernel 源码的精确文件路径（与行号区间，给永久链接） |
| binding 位置 | 注册为 op 的那层胶水在哪（C++ 的 thop 文件，或 Python 的 custom_ops 文件） |
| fake 位置 | fake/meta 实现在哪（`cpp_custom_ops.py` 的 `_register_fake` 内，或同文件的 `register_fake`） |
| op 形态（仅 torch_custom_ops.py 的 op） | 形态 1 / 2 / 3 |
| 是否走 AutoTuner | 是 / 否 |
| 四产物里的「integration」 | 找到一个真实调用点（grep `torch.ops.trtllm.<name>`） |

**操作建议**：

1. 用 4.1.4 的脚本枚举 op 名。
2. 对每个 op，用搜索工具在 `tensorrt_llm/_torch/custom_ops/` 与 `cpp/tensorrt_llm/thop/` 里定位 binding 与 fake。
3. 对 integration，全仓库 grep 该 op 的调用点，挑一个最有代表性的（如 `dsa.py:2296` 之于 `indexer_k_cache_scatter_op`）。
4. 把三张表写进你的学习笔记，并标注哪些字段「待本地确认」（例如某些 op 的调用点可能很多）。

**预期成果**：做完后，你应当能对仓库里**任意**一个 `trtllm::` op 在几分钟内说出它的「内核是谁、binding 在哪、怎么注册的、谁在调它」。这就是本讲想给你的「算子地图读取能力」——它也是后续阅读 u12-l1（AutoDeploy 图变换会把 fusion 目标落到这些 op 上）、u10（MoE/量化/CUDA Graph 都建立在 op 是良好边界的前提上）的基础。

## 6. 本讲小结

- **custom_ops 是统一门面**：它把 C++/CUDA、Triton、CuTe DSL、cuTile 四种来源的内核统一包装成 `torch.ops.trtllm.<name>`，让模型代码与内核语言解耦，并使每个 op 成为 torch.compile / CUDA Graph 可见的算子边界。
- **四产物模型**：定义一个新 op 必须产出 kernel、binding、integration、tests 四样；C++ 路径的 binding 与 fake 分处 C++/Python 两地且要改两处 CMake，JIT 路径则全在一个 Python 文件里、无需改 CMake。
- **import 即注册**：Python 流派靠装饰器、C++ 流派靠 `load_library` 加载 `.so`；C++ op 的 fake 集中在 `cpp_custom_ops.py:_register_fake()`，由 `_common.py:_init()` 在加载 `libth_common.so` 后调用。
- **三种 op 形态**：`torch_custom_ops.py` 里的 op 可分为「纯 Python（包 Triton 内核）」「TunableRunner + AutoTuner 外壳（包 C++ TorchBind 类）」「多后端统一路由器（如 `nvfp4_gemm`）」三类，由简到繁。
- **两个 triton 别混**：散落各处的 `@triton.jit` 单文件内核是 TRT-LLM 自写的；仓库根目录的 `triton_kernels/` 是从上游 Triton 项目 v3.6.0 vendored 的第三方高性能内核库，禁止直接编辑、不走 `trtllm::` 命名空间。
- **选型与陷阱**：优先复用、能 Python 就别 C++、热点 op 用 `fast_custom_op`；常见陷阱是漏注册、假设连续性、漏 thop CMake 项、漏 fake、JIT 缓存键不全。

## 7. 下一步学习建议

- **回到 u10-l4（CUDA Graph / torch.compile）**：带着「op 是编译图的边界节点」这层认识重读 piecewise CUDA Graph，你会更明白为什么 attention/MoE 必须包成 in-place 的「黑盒」custom op 才能被分段捕获。
- **读 u12-l1（AutoDeploy 图变换）**：AutoDeploy 的 fusion transform（如 `fuse_silu_mul`）正是在 FX 图上把若干个 `trtllm::` op 合并成一个新的 custom op；本讲给出的「四产物」就是它产出的目标形态。
- **动手跟踪一个真实 op**：选 `indexer_k_cache_scatter_op`，从 `dsa.py:2296` 一路读到 `indexerKCacheScatter.cu` 的 `__global__` 内核，再把 `tests/unittest/_torch/attention/sparse/test_dsa_indexer.py` 的断言对照着看——这是把本讲四节一次走通的最短路径。
- **继续本单元**：下一篇 u12-l3 讲 VisualGen 这条独立产品线，与本讲的 LLM custom ops 体系相对照，理解「共享 kernel、独立引擎」的边界。
