# 执行后端与 kernel adapter

> 讲义 id：u7-l1 ｜ 阶段：advanced ｜ 依赖：u4-l2（jit 装饰器与 lazy/eager 执行模式）、u6-l3（设备代码生成、模板与 tile op lowering）

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `tilelang.lower()` 产出的 `CompiledArtifact` 与「一个能用 `torch.Tensor` 调用的函数」之间存在的那道鸿沟，以及 `execution_backend` + `adapter` 是如何把它填平的。
- 读懂 `tilelang.backend.execution_backend` 这套「按 target kind 注册 + 惰性加载 + 谓词匹配」的注册表，并解释 `execution_backend='auto'` 到底选出了什么。
- 理解 `BaseKernelAdapter` 这个抽象基类定下的契约：`_convert_torch_func()`、`__call__`、`result_idx`、stream/device thunk。
- 区分五种 adapter（`tvm_ffi` / `cython` / `nvrtc` / `torch` / `cutedsl`）背后的**两种产物策略**——「TVM 运行时模块（rt_mod）」路线 vs「源码自编译」路线，并知道它们各自需要 `lower()` 产出什么。
- 把同一个 kernel 用不同 `execution_backend` 编译，对比得到的可调用对象类型与调用开销。

## 2. 前置知识

本讲建立在前面几讲已经建立的概念之上，先做一句话回顾，不展开：

- **`CompiledArtifact`（u5-l3）**：`tilelang.lower()` 的产物，字段有 `host_mod`（主机 IR）、`device_mod`（设备 IR）、`params`（一组 `KernelParam`，描述每个张量/标量参数的 dtype 与 shape）、`kernel_source`（生成的设备源码字符串）、`rt_mod`（TVM 运行时模块，**可能为 None**）、`target`、`target_host`。
- **`JITKernel`（u4-l2）**：tilelang 对「一个已编译 kernel」的封装，其 `__call__` 最终委托给 `self.adapter.func`。
- **`target`（u4-l4）**：描述目标硬件的 TVM `Target` 对象，如 `cuda`、`hip`、`llvm`、`metal`、`c`，以及带 `cutedsl` 标签的 cuda 变体。
- **DLPack**：跨深度学习框架共享张量内存的标准格式。PyTorch 的 `torch.Tensor` 与 TVM 的 tensor 之间正是靠 DLPack 零拷贝互转。

**本讲要回答的核心问题**：`lower()` 给你的是一堆 IR 和源码字符串，而用户手里攥着的是 `torch.Tensor`。中间这一段「把编译产物变成『喂 torch.Tensor 进去、吐 torch.Tensor 出来』的 Python 函数」的胶水，是谁写的？答案就是 **adapter（适配器）**；而「该用哪一种 adapter、`lower()` 该不该真把设备代码编译成二进制」这两个决定，则由 **execution_backend（执行后端）** 统一指挥。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [tilelang/backend/execution_backend.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/execution_backend.py) | 执行后端注册表与 `resolve_execution_backend_spec`（`auto` 推断）。 |
| [tilelang/backend/\_\_init\_\_.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/__init__.py) | 为每个 target kind 登记惰性加载入口。 |
| [tilelang/cuda/execution_backend.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/execution_backend.py) | CUDA target 下注册 tvm_ffi / nvrtc / cython / cutedsl 四种后端。 |
| [tilelang/cpu/execution_backend.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cpu/execution_backend.py)、[tilelang/rocm/execution_backend.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/rocm/execution_backend.py)、[tilelang/metal/execution_backend.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/metal/execution_backend.py) | CPU（c/llvm）/ HIP / Metal target 下的后端注册。 |
| [tilelang/jit/adapter/base.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/base.py) | `BaseKernelAdapter` 抽象基类，定义 adapter 契约。 |
| [tilelang/jit/adapter/\_\_init\_\_.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/__init__.py) | 统一导出五种 adapter 类。 |
| [tilelang/jit/adapter/tvm_ffi.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/tvm_ffi.py) | `TVMFFIKernelAdapter`：默认后端，基于 TVM 运行时 `Executable`。 |
| [tilelang/jit/adapter/nvrtc/adapter.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/nvrtc/adapter.py) | `NVRTCKernelAdapter`：用 cuda-python + NVRTC 源码自编译路线。 |
| [tilelang/jit/adapter/cython/adapter.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/cython/adapter.py) | `CythonKernelAdapter`：用 ctypes + Cython 包装的源码自编译路线。 |
| [tilelang/jit/adapter/torch/metal.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/torch/metal.py) | `MetalKernelAdapter`：Metal 后端，借助 `torch.mps.compile_shader`。 |
| [tilelang/jit/kernel.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py) | `JITKernel`：解析 execution_backend、读取 spec 标志、按名称分流构造 adapter 的「工厂」。 |

---

## 4. 核心概念与源码讲解

### 4.1 执行后端注册表与 `auto` 推断（tilelang.backend.execution_backend）

#### 4.1.1 概念说明

`execution_backend` 回答的问题是：**这个 kernel 用哪一套运行时/编译策略来装载与启动？**

tilelang 并没有把「怎么跑 kernel」写死。同一个 target（比如 `cuda`）下，你可以选择：

- `tvm_ffi`：走完整的 TVM 运行时（生成 `rt_mod`，用 DLPack 和 PyTorch 互通）。**这是默认选择。**
- `nvrtc`：用 NVIDIA 的 NVRTC 在运行时把 CUDA 源码编译成 cubin，靠 cuda-python 直接 `cuLaunchKernel`，绕开 TVM 运行时。
- `cython`：把设备源码用 nvcc/hipcc/c++ 编译成 `.so`，再用 ctypes 调用。
- `torch`（实为 Metal 路线）：用 PyTorch 自带的 `torch.mps.compile_shader` 编译 Metal shader。
- `cutedsl`：CuTeDSL 后端变体。

为了让「选哪一种」这件事可扩展、可探测，tilelang 设计了一张**注册表**：每个 target kind 维护一组「候选后端」，每个候选带三个属性——是否可用（`is_available`）、是否支持当前 target（`supports_target`）、是否需要 `lower()` 真的去编译设备代码（`enable_host_codegen` / `enable_device_compile`）。`resolve_execution_backend_spec` 就是查表 + 过滤 + 选第一个的解析器。

引入三个关键术语：

- **target kind**：`target.kind.name`，例如 `cuda`、`hip`、`llvm`、`c`、`metal`。注册表按它分桶。
- **谓词（predicate）**：`Callable[[Target], bool]`，用来在同一个 kind 内进一步细分（比如 cuda 下区分「普通 cuda」与「cutedsl 变体」）。
- **可用性检查（availability check）**：`Callable[[], bool]`，用来表达「这个后端需要额外依赖（如 cuda-python）才可用」。

#### 4.1.2 核心流程

整个解析过程可以概括为「**登记 → 惰性加载 → 匹配过滤 → 选第一个**」四步：

```
import tilelang
   │
   ├─ tilelang/backend/__init__.py 顶部执行
   │     register_lazy_execution_backends("cuda", "tilelang.cuda.execution_backend")
   │     register_lazy_execution_backends("hip",  "tilelang.rocm.execution_backend")
   │     register_lazy_execution_backends("c",    "tilelang.cpu.execution_backend")
   │     register_lazy_execution_backends("llvm", "tilelang.cpu.execution_backend")
   │     register_lazy_execution_backends("metal","tilelang.metal.execution_backend")
   │   （此刻只登记「import 路径」，并不真正 import，避免启动开销）
   │
   └─ 用户调用 tilelang.compile(..., execution_backend=None/"auto", target="cuda")
         │
         └─ resolve_execution_backend_spec(requested, target):
               1. canonicalize：大小写归一 + 别名（"dlpack"→"tvm_ffi"）
               2. _ensure_execution_backends_loaded("cuda")
                    → import_module("tilelang.cuda.execution_backend")
                    → 该模块顶部执行 4 个 register_execution_backend(...)，真正填表
               3. _matching_specs(target, include_unavailable=True/False)
                    → 先按 supports_target 过滤
                    → 再（可选）按 is_available 过滤
               4. 若 requested ∈ {None, "auto"}：返回「可用列表」的第 0 个
                  否则校验 requested 在「允许列表」内且可用，返回对应 spec
```

`auto` 推断的关键规则是：**在「既匹配 target、又确认可用」的候选里取第一个**。因此每个后端注册文件里，`register_execution_backend` 的**书写顺序**直接决定了 `auto` 的默认偏好——CUDA 下 `tvm_ffi` 被写在最前面，所以 `auto` 永远选 `tvm_ffi`。

#### 4.1.3 源码精读

先看数据结构。`ExecutionBackendSpec` 是一个 frozen + slots 的 dataclass：

[tilelang/backend/execution_backend.py:L28-L37](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/execution_backend.py#L28-L37) —— 定义一个执行后端候选的全部属性：`name`、可用性检查、target 谓词，以及两个控制 `lower()` 行为的开关 `enable_host_codegen` / `enable_device_compile`。`matches()` 用谓词判断该 spec 是否适配给定 target。

`enable_host_codegen=True` 与 `enable_device_compile=True` 是后端给 `lower()` 的**指令**：告诉它「请真的把设备代码编译成二进制、生成可用的 `rt_mod`」。`tvm_ffi` 后端需要它，因为它要直接拿 `rt_mod` 来跑；`nvrtc`/`cython` 后端不需要它，因为它们自己会在 adapter 构造时把源码编译成库。

再看注册与惰性加载：

[tilelang/backend/execution_backend.py:L45-L70](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/execution_backend.py#L45-L70) —— `register_execution_backend` 把 spec 追加到 `_EXECUTION_BACKENDS[target_kind]` 列表；`override=True` 时先移除同名旧 spec（这就是各后端注册文件都带 `override=True` 的原因——允许重复 import 时幂等）。`register_lazy_execution_backends` 只记下「import 路径」，`_ensure_execution_backends_loaded` 在真正需要时才 `import_module`，实现「用到才加载」。

惰性入口在包初始化时一次性登记好：

[tilelang/backend/\_\_init\_\_.py:L36-L40](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/__init__.py#L36-L40) —— 五个 target kind 对应五个后端注册模块。注意 `c` 与 `llvm` 都指向 `tilelang.cpu.execution_backend`。

匹配与解析的核心：

[tilelang/backend/execution_backend.py:L73-L79](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/execution_backend.py#L73-L79) —— `_matching_specs` 先确保该 kind 的后端已加载，再用 `spec.matches(target)`（即 `supports_target` 谓词）过滤；`include_unavailable=False` 时再叠加一次 `spec.is_available()` 过滤。

[tilelang/backend/execution_backend.py:L94-L116](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/execution_backend.py#L94-L116) —— `resolve_execution_backend_spec` 的全部分支：`auto`/`None` 取可用列表第 0 个；请求的名字不在允许列表就报「非法后端」；在允许列表但不可用就报「缺依赖」。报错信息里都贴心地列出 `Allowed: ...`，并提示 `execution_backend='auto'`。

最后看一个具体的注册文件，理解谓词如何区分 cuda 与 cutedsl：

[tilelang/cuda/execution_backend.py:L34-L58](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/execution_backend.py#L34-L58) —— CUDA 下注册四个后端。`tvm_ffi`、`cython` 用 `_is_plain_cuda_target`（kind 为 cuda 且不含 `cutedsl` 标签），`nvrtc` 还叠加 `_is_nvrtc_available`（探测 cuda-python 是否可 import），`cutedsl` 用 `_is_cutedsl_target`（kind 为 cuda 且含 `cutedsl` 标签）。书写顺序决定了 `auto` 偏好 `tvm_ffi`。

> 顺带一提：[tilelang/cuda/execution_backend.py:L8-L31](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/execution_backend.py#L8-L31) 里 `_is_nvrtc_available` / `_is_cutedsl_available` 都把探测包在 `try/except ImportError` 里，这正是「无 GPU / 缺 cuda-python 的机器也能 import tilelang」的关键——缺失依赖只会让该后端在可用性过滤时被剔除，而不会让整个 import 失败。

#### 4.1.4 代码实践

**实践目标**：亲手看到注册表的内容与 `auto` 推断的结果，验证「书写顺序决定默认偏好」。

**操作步骤**（源码阅读型 + 极少量运行）：

1. 写一个小脚本 `probe_backends.py`（**示例代码**，非项目原有）：

   ```python
   # 示例代码：探测各 target 下允许的执行后端
   from tvm.target import Target
   from tilelang.backend.execution_backend import (
       allowed_backends_for_target,
       resolve_execution_backend_spec,
   )

   for kind in ["cuda", "hip", "c", "llvm", "metal"]:
       tgt = Target(kind)
       all_bk = allowed_backends_for_target(tgt, include_unavailable=True)
       avail_bk = allowed_backends_for_target(tgt, include_unavailable=False)
       try:
           auto = resolve_execution_backend_spec(None, tgt).name
       except ValueError as e:
           auto = f"<none: {e}>"
       print(f"{kind:6} all={all_bk}  available={avail_bk}  auto->{auto}")
   ```

2. 运行 `python probe_backends.py`。

**需要观察的现象**：

- 每个目标下 `all` 与 `available` 的差异（`available` 通常更少，因为 `nvrtc`/`cutedsl` 依赖 cuda-python/CuTeDSL）。
- 各目标的 `auto->...` 应分别指向其注册表里**第一个可用**的后端（cuda→`tvm_ffi`、metal→`tvm_ffi`、llvm/c→`tvm_ffi` 或 `cython`，取决于顺序与可用性）。

**预期结果**：CUDA 目标下 `auto` 解析为 `tvm_ffi`；若本机未装 cuda-python，`nvrtc` 仅出现在 `all` 而不出现在 `available`。

**待本地验证**：若当前环境无 CUDA，`resolve_execution_backend_spec(None, Target("cuda"))` 会因无可用后端而抛 `ValueError`——这正对应源码 [execution_backend.py:L101-L103](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/execution_backend.py#L101-L103) 的分支。

#### 4.1.5 小练习与答案

**练习 1**：若把 [tilelang/cuda/execution_backend.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/execution_backend.py) 里 `nvrtc` 的注册移到 `tvm_ffi` 之前，且本机装了 cuda-python，`execution_backend='auto'` 会选谁？为什么？

> **答案**：会选 `nvrtc`。因为 `resolve_execution_backend_spec` 在 `auto` 分支取「可用列表的第 0 个」（[L101-L104](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/execution_backend.py#L101-L104)），而列表顺序由 `register_execution_backend` 的书写顺序决定。

**练习 2**：`canonicalize_execution_backend("dlpack")` 返回什么？为什么要这个别名？

> **答案**：返回 `"tvm_ffi"`（[L12-L25](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/execution_backend.py#L12-L25)）。因为 `tvm_ffi` 后端正是靠 DLPack 与 PyTorch 张量互通的，`dlpack` 是一个语义更直观的旧称/别名，归一化后映射到内部统一名 `tvm_ffi`。

---

### 4.2 Adapter 抽象基类与 JITKernel 工厂（tilelang.jit.adapter）

#### 4.2.1 概念说明

`CompiledArtifact` 是「死的」——它有 IR、源码、（可能的）运行时模块，但你不能直接 `artifact(torch_tensor)`。**adapter（适配器）** 的职责就是把它变成「活的」Python 可调用对象：

> adapter = f(CompiledArtifact, params, result_idx) → 一个 `(*torch_tensors) -> torch_tensors` 的函数

`BaseKernelAdapter` 定义了这个转换的**契约**。所有五种 adapter 都继承它，实现同一个抽象方法 `_convert_torch_func()`，该方法返回真正的执行函数并赋给 `self.func`；adapter 的 `__call__` 再委托给 `self.func`。这是一种典型的**模板方法（template method）模式**：基类搭好骨架（参数校验、stream/device 捕获、源码缓存、`__call__`），子类只填「怎么把这套产物跑起来」这一个洞。

而**选用哪种子类**的工作不在 adapter 包内完成，而在 `JITKernel._compile_and_create_adapter` 这个「工厂方法」里，用一串 `if/elif` 按 `execution_backend` 的名字分流。`JITKernel` 同时还做另一件事：把 spec 上的 `enable_host_codegen` / `enable_device_compile` 标志读出来，传给 `tilelang.lower()`——也就是说，**`execution_backend` 在两处被消费**：先决定 `lower()` 产出什么，再决定用哪个 adapter 包装它。

引入术语：

- **`result_idx`**：输出张量在 `params` 列表中的下标（可负、可单值、可列表）。adapter 会据此**自动分配输出张量**，因此调用方只需传入「输入」张量。
- **thunk（ thunk / 延迟求值体）**：返回 `lambda` 而非立即取值，确保「调用 kernel 那一刻」才读取 PyTorch 当前的 CUDA stream/device，而不是构造 adapter 那一刻。

#### 4.2.2 核心流程

adapter 的生命周期与 JITKernel 的工厂流程：

```
JITKernel.__init__(func, out_idx, execution_backend, target, ...)
   │
   ├─ self.target = determine_target(target)                    # 解析 target
   ├─ self.execution_backend_spec = resolve_execution_backend_spec(execution_backend, self.target)
   ├─ self.execution_backend = spec.name                        # 选定后端名
   │
   └─ adapter = self._compile_and_create_adapter(func, out_idx):
         │
         ├─ 读 spec.enable_host_codegen / enable_device_compile
         ├─ with PassContext(...):
         │     artifact = tilelang.lower(func, target=...,
         │                              enable_host_codegen=...,
         │                              enable_device_compile=...)
         │
         └─ if execution_backend == "tvm_ffi":  → TVMFFIKernelAdapter(...)
            elif "cython":                      → CythonKernelAdapter(...)
            elif "nvrtc":                       → NVRTCKernelAdapter(...)
            elif "torch":                       → MetalKernelAdapter(...)   # 断言 is_metal
            elif "cutedsl":                     → CuTeDSLKernelAdapter(...) # 断言 is_cutedsl

   adapter 构造时（_post_init → _convert_torch_func）把产物包成 self.func
   self.torch_function = adapter.func
   JITKernel.__call__ → self.torch_function(*args)
```

注意一个微妙点：构造 adapter 的参数清单**因后端而异**。`tvm_ffi` 必须传 `rt_mod`（因此 `lower()` 必须真编译）；`torch`(Metal) 干脆不传 target/host_mod；`nvrtc`/`cython`/`cutedsl` 传 device 源码与 host/device IR 但不需要 rt_mod。工厂的每个分支正好对应「该后端需要哪些产物」。

#### 4.2.3 源码精读

先看契约本身。`BaseKernelAdapter` 的骨架：

[tilelang/jit/adapter/base.py:L24-L31](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/base.py#L24-L31) —— 构造函数接收 `mod`、`params`、`result_idx`，并调用 `_post_init()`。`_post_init` 又调用抽象方法 `_convert_torch_func()`，把返回值赋给 `self.func`。

[tilelang/jit/adapter/base.py:L55-L57](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/base.py#L55-L57) 与 [L108-L109](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/base.py#L108-L109) —— 模板方法的核心：`_convert_torch_func` 是抽象方法，`_post_init` 调用它。子类实现它来产出真正的可调用对象。

[tilelang/jit/adapter/base.py:L99-L100](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/base.py#L99-L100) —— `__call__` 直接委托 `self.func`。这就是「adapter 即可调用对象」的实现。

接着看 stream/device 的 thunk 设计——这是五种 adapter 共享、用来对齐 PyTorch 流语义的关键：

[tilelang/jit/adapter/base.py:L60-L97](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/base.py#L60-L97) —— `get_current_stream_functor` / `get_current_device_functor` 返回的是 **lambda**（thunk），不是值。它在每次 kernel 调用时才读取 PyTorch 当前的 CUDA stream/device 指针，从而尊重用户在调用前可能做的 `with torch.cuda.stream(...)` 切换。CPU 或无 CUDA 时回落到 `0` / `torch.device("cpu")`。

再看 `result_idx` 的合法化，它定义了「输出张量自动分配」的语义：

[tilelang/jit/adapter/base.py:L33-L53](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/base.py#L33-L53) —— 把 `None`/`int`/`list` 统一规范成非负索引列表，支持负索引（如 `-1` 表示最后一个参数是输出）。

现在看 `JITKernel` 这个工厂。先看它在构造时如何解析后端并读取 spec 标志：

[tilelang/jit/kernel.py:L111-L114](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L111-L114) —— `resolve_execution_backend_spec` 解析出 spec，其 `.name` 就是最终后端名。注意 `JITKernel.__init__` 的形参默认值写作 `"tvm_ffi"`（[L70](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L70)），但经由 `tilelang.compile` 的缓存层进入时，`None` 会先被环境变量 `TILELANG_EXECUTION_BACKEND`（默认 `"auto"`）补上，再交给这里解析。

[tilelang/jit/kernel.py:L235-L283](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L235-L283) —— 读 `enable_host_codegen` / `enable_device_compile` 并传给 `tilelang.lower()`。**这是 execution_backend 的第一次消费**：它决定了 `artifact.rt_mod` 是否为 `None`。

最后是工厂的主体——按后端名分流构造 adapter：

[tilelang/jit/kernel.py:L291-L372](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L291-L372) —— **execution_backend 的第二次消费**：`if/elif` 选 adapter 类，并按该后端所需字段组装构造参数。注意每个分支的「断言」：`tvm_ffi` 断言 `artifact.rt_mod is not None`（[L295](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L295)）；`torch` 断言 `is_metal_target(target)`（[L341](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L341)）；`cutedsl` 断言 `is_cutedsl_target(target)`（[L356](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L356)）。这些断言与 [execution_backend.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/execution_backend.py) 的 `supports_target` 谓词互为双保险。

> 缓存命中时走另一条平行的工厂分支 [tilelang/jit/kernel.py:L379-L448](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L379-L448)，每个 adapter 各有一个 `from_database` 类方法，直接从磁盘 `.so` 复原，跳过 `lower()`/codegen（详见 u4-l3 编译缓存）。

五种 adapter 的统一出口在：

[tilelang/jit/adapter/\_\_init\_\_.py:L1-L6](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/__init__.py#L1-L6) —— 一次性导出 `BaseKernelAdapter`、`CachedTextSource` 与五个具体类。注意命名上的小坑：**`torch` 后端对应的类叫 `MetalKernelAdapter`**（因为目前 torch 后端只服务 Metal）。

#### 4.2.4 代码实践

**实践目标**：确认 adapter 的契约——同一个 `JITKernel` 实例的 `__call__` 确实委托给 `adapter.func`，且 `adapter` 是 `BaseKernelAdapter` 的子类。

**操作步骤**（源码阅读型）：

1. 打开 [tilelang/jit/kernel.py:L188-L204](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L188-L204)，确认 `JITKernel.__call__` 调用的是 `self.torch_function`。
2. 打开 [tilelang/jit/kernel.py:L142-L143](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L142-L143)，确认 `self.torch_function = adapter.func`。
3. 追溯 `adapter.func` 的来源：[base.py:L108-L109](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/base.py#L108-L109) 的 `_post_init` → `_convert_torch_func()`。

**需要观察的现象**：调用链是一条直线 `JITKernel.__call__ → torch_function → adapter.func → 子类的 _convert_torch_func 返回值`。

**预期结果**：你能用一句话说清「adapter 的全部价值就是 `_convert_torch_func` 这个方法」。

**待本地验证**：若手头有 CUDA 环境，可在编译一个 kernel 后 `print(type(kernel.adapter).__name__)` 与 `print(type(kernel.adapter.func))`，前者是 `TVMFFIKernelAdapter`（默认），后者是一个 Python 闭包函数。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `BaseKernelAdapter.get_current_stream_functor` 返回 `lambda` 而不是直接返回 stream 指针？

> **答案**：因为 adapter 在**构造时**无法预知用户**调用时**会处于哪个 CUDA stream。返回 thunk（[base.py:L60-L79](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/base.py#L60-L79)）使得 stream 在每次 `func(...)` 调用时即时读取，从而尊重用户在调用前用 `with torch.cuda.stream(guard):` 做的流切换。

**练习 2**：`execution_backend` 在 `JITKernel` 里被消费了几次？分别在做什么？

> **答案**：两次。第一次读 `spec.enable_host_codegen` / `enable_device_compile` 传给 `tilelang.lower()`（[kernel.py:L235-L283](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L235-L283)），决定产物里 `rt_mod` 是否非空；第二次在 `if/elif` 工厂里（[L291-L372](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L291-L372)），决定用哪个 adapter 类包装产物。

---

### 4.3 五种 adapter 与两种产物策略

#### 4.3.1 概念说明

五种 adapter 看似复杂，其实只有**两种根本策略**（外加 Metal 的特例）：

| 策略 | 代表后端 | `lower()` 是否编译 | adapter 拿到什么 | 怎么跑 |
|---|---|---|---|---|
| **A：TVM 运行时模块** | `tvm_ffi` | 是（`enable_*_codegen=True`） | 真实的 `rt_mod`（含 cubin） | `runtime.Executable(rt_mod)(*tensors)`，靠 DLPack 通 PyTorch |
| **B：源码自编译** | `nvrtc`、`cython` | 否（默认） | 设备源码字符串 + host/device IR | adapter 自己在构造时把源码编成 `.so`/cubin，再用 cuda-python 或 ctypes 调 |
| **特例** | `torch`（Metal） | 否 | Metal 源码字符串 | 直接交给 `torch.mps.compile_shader` |
| **特例** | `cutedsl` | 视情况 | CuTeDSL 源码 | 走 CuTeDSL 工具链 |

策略 A 与 B 的本质区别：**「谁来把源码变成可执行二进制」**。

- 策略 A 把这件事交给 TVM 的 `device_compile` + `host_codegen`（u6-l3 讲过的 `BuildTileLangCUDA` 走 nvcc），结果是一个自包含的 `rt_mod`，adapter 只是「薄包装」。
- 策略 B 让 `lower()` **只产源码、不编译**（`rt_mod=None`），把编译推迟到 adapter 构造时：`nvrtc` 用 cuda-python 的 NVRTC API 在进程内编译，`cython` 用 `subprocess` 调 nvcc/hipcc/c++ 生成 `.so` 再用 ctypes 加载。

为什么要有策略 B？因为它**解耦了「编译」与「TVM 运行时」**——`nvrtc` 路线完全不依赖 TVM runtime module 的 launch 机制，可以更直接地控制 kernel 启动（自己 `cuLaunchKernel`），对调试、与 PyTorch 流的对接、以及某些特殊指令（如多 kernel 的 PDL）更灵活。`cython` 路线则提供了更贴近「手写 C++ kernel」的 ctypes 接口，并支持静态 shape/stride 的快速校验。

#### 4.3.2 核心流程

**策略 A（tvm_ffi）的构造与调用**：

```
TVMFFIKernelAdapter.__init__(..., rt_mod=artifact.rt_mod, ...)
   ├─ self._process_dynamic_symbolic()      # 建立「符号维 → 运行时取值来源」的映射
   ├─ self._post_init() → _convert_torch_func():
   │     └─ 返回 func(*inputs):
   │          1. 校验输入数量 = len(params) - len(result_idx)
   │          2. 拼装完整位置参数：输出位用 torch.empty(按符号维解析形状) 填，输入位依次填
   │          3. executable = self._get_executable()   # 双检锁懒构造 runtime.Executable(rt_mod)
   │          4. executable(*tensor_list)              # TVM 运行时启动 kernel
   │          5. 按 result_idx 返回单个或列表 tensor
```

**策略 B（nvrtc）的构造与调用**：

```
NVRTCKernelAdapter.__init__(..., device_kernel_source=..., host_mod=..., device_mod=...)
   ├─ self.wrapper = TLPyWrapper(target); wrapper.wrap(...)   # 生成 host 启动函数源码 host_func
   ├─ self.lib_generator = NVRTCLibraryGenerator(target)
   │     update_lib_code(device_kernel_source) + update_host_func(host_func)
   │     compile_lib()   # 用 NVRTC 把源码编成 cubin 库
   │     load_lib()      # 加载成 pymodule
   ├─ 对每个 function_name：cuda.cuLibraryGetKernel(culib, name)  # 拿 kernel 句柄
   └─ _post_init() → _convert_torch_func() 返回 _wrap_forward_from_prebuild_lib:
        └─ 校验输入 → 分配输出 → 解析符号维 → pymodule.call(self.kernels, *args, stream=current_stream)
```

**特例（Metal）**：最简单——直接把 Metal 源码交给 PyTorch：

```
MetalKernelAdapter._convert_torch_func():
   _kernel = getattr(torch.mps.compile_shader(kernel_global_source), kernel_name)
   return launcher(*args): _kernel(*args, threads=..., group_size=block_info)
```

#### 4.3.3 源码精读

**策略 A：`TVMFFIKernelAdapter`**。先看它的可执行对象如何懒构造：

[tilelang/jit/adapter/tvm_ffi.py:L121-L140](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/tvm_ffi.py#L121-L140) —— `_make_executable` 用 `rt_mod` 构造 `runtime.Executable`（macOS/Windows 下还会带额外编译参数 `.jit()`）；`_get_executable` 用 `threading.Lock` 做双检锁懒初始化，保证只构造一次。

动态形状的处理是 tvm_ffi adapter 最精巧的部分：

[tilelang/jit/adapter/tvm_ffi.py:L145-L175](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/tvm_ffi.py#L145-L175) —— `_process_dynamic_symbolic` 扫描 `prim_func` 的参数与 buffer，把每个符号 `Var` 映射到一个四元组 `(id, buffer_index, dim, stride_scale)`：`id=0` 表示该符号是某个 buffer 的 shape、`id=1` 表示是 stride、`id=2` 表示是标量参数本身。`stride_scale` 用来补偿 fp4 等子字节类型（torch 的 stride 以存储单元计，kernel 要的是逻辑元素步长）。

最关键的 `_convert_torch_func`：

[tilelang/jit/adapter/tvm_ffi.py:L224-L283](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/tvm_ffi.py#L224-L283) —— 闭包 `func(*inputs)` 的全部逻辑：先按 `expected_inputs = len(params) - len(result_idx)` 校验输入数量（[L226-L228](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/tvm_ffi.py#L226-L228)）；再遍历 `params`，遇到 `result_idx` 位就用 `torch.empty`（形状靠 `dynamic_symbolic_map` 从输入张量实时解析）分配输出，否则取下一个输入（[L242-L274](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/tvm_ffi.py#L242-L274)）；最后 `executable(*tensor_list)` 启动，按 `result_idx` 返回。这就是「输入张量进、输出张量出」的完整实现。

**策略 B：`NVRTCKernelAdapter`**。看它如何在构造时自编译：

[tilelang/jit/adapter/nvrtc/adapter.py:L75-L95](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/nvrtc/adapter.py#L75-L95) —— 构造体里：`TLPyWrapper.wrap(device_kernel_source)` 生成主机启动函数 `host_func`；`NVRTCLibraryGenerator` 把设备源码 + host_func 用 NVRTC 编成库并加载为 `pymodule`；再用 `cuda.cuLibraryGetKernel` 为每个函数名拿到 kernel 句柄存进 `self.kernels`。注意它**不接收 `rt_mod`**——印证了策略 B「lower 只产源码」。

调用入口：

[tilelang/jit/adapter/nvrtc/adapter.py:L218-L276](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/nvrtc/adapter.py#L218-L276) —— `_wrap_forward_from_prebuild_lib` 做与 tvm_ffi 类似的「校验→分配输出→解析符号维→取当前 stream」流程，但最后调用的是 `self.pymodule.call(self.kernels, *args, stream=stream)`（[L271](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/nvrtc/adapter.py#L271)），即 cuda-python 直接启动，不经 TVM 运行时。stream 默认取 `torch.cuda.current_stream().cuda_stream`（[L266-L269](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/nvrtc/adapter.py#L266-L269)）。

**策略 B：`CythonKernelAdapter`**。它走 subprocess 编译 + ctypes：

[tilelang/jit/adapter/cython/adapter.py:L120-L150](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/cython/adapter.py#L120-L150) —— `TLWrapper.wrap` 生成 host 源码，`LibraryGenerator` 用 nvcc/hipcc/c++ 把它编成 `.so`，`ctypes.CDLL` 加载后调 `lib.init()` 初始化，再由 `CythonKernelWrapper`（C 扩展）做带静态 shape/stride 校验的 forward。它还预计算了 `static_shape_map`/`static_strides_map`/`static_contiguous_list`（[L275-L310](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/cython/adapter.py#L275-L310)），用于在 forward 时快速校验输入张量是否符合编译期假设。

[tilelang/jit/adapter/cython/adapter.py:L338-L360](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/cython/adapter.py#L338-L360) —— 调用入口把 torch 张量转成 `ctypes.c_void_p(arr.data_ptr())` 指针，附加 stream 指针，调 `self.lib.call(*ctypes_args)`。这是五种 adapter 里最「底层」、最贴近手写 C kernel 启动的路线。

底层 subprocess 编译逻辑在 `libgen.py`：

[tilelang/jit/adapter/libgen.py:L59-L99](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/libgen.py#L59-L99) —— `LibraryGenerator.compile_lib` 按 target 拼 nvcc/hipcc/c++ 命令（含 `-gencode`、`--use_fast_math`、`-I CUTLASS_INCLUDE_DIR`、`-I TILELANG_TEMPLATE_PATH` 等），写临时 `.cu`/`.cpp`，`subprocess.run` 编出 `.so`。fast-math、ptxas 选项都从 `pass_configs` 读出（[L69-L72](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/libgen.py#L69-L72)、[L111-L114](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/libgen.py#L111-L114)）——这与 u4-l3 强调的「编译选项必须进缓存键」相呼应。

**特例：`MetalKernelAdapter`（torch 后端）**：

[tilelang/jit/adapter/torch/metal.py:L65-L80](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/torch/metal.py#L65-L80) —— 最简单的 adapter：把 Metal 源码交给 `torch.mps.compile_shader`，取出 kernel 名对应的可调用对象，包一个带 `threads`/`group_size` 的 launcher。block/grid 信息从 `device_mod` 函数的 `thread_extent` 属性读出（[L39-L47](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/torch/metal.py#L39-L47)）。这条路线完全复用 PyTorch 自带的 Metal shader 编译与启动，无需 TVM 运行时。

#### 4.3.4 代码实践

**实践目标**：把同一个 GEMM kernel 分别用 `execution_backend='tvm_ffi'` 与 `execution_backend='nvrtc'` 编译调用，对比 adapter 类型与一次调用的粗略开销。这是本讲的主实践。

**操作步骤**：

本实践参考项目已有的真实测试 [testing/python/jit/test_tilelang_jit_nvrtc.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/testing/python/jit/test_tilelang_jit_nvrtc.py) 与 [testing/python/jit/test_tilelang_jit_tvm_ffi.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/testing/python/jit/test_tilelang_jit_tvm_ffi.py)。写一个脚本 `compare_backends.py`（**示例代码**）：

```python
# 示例代码：对比 tvm_ffi 与 nvrtc 两种执行后端
import time
import torch
import tilelang
import tilelang.language as T


def matmul(M=1024, N=1024, K=1024, block_M=128, block_N=128, block_K=64,
           in_dtype="float16", out_dtype="float16", accum_dtype="float32",
           num_stages=3, threads=128):
    @T.prim_func
    def main(
        A: T.Tensor((M, K), in_dtype),
        B: T.Tensor((K, N), in_dtype),
        C: T.Tensor((M, N), out_dtype),
    ):
        with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=threads) as (bx, by):
            A_shared = T.alloc_shared((block_M, block_K), in_dtype)
            B_shared = T.alloc_shared((block_K, block_N), in_dtype)
            C_local = T.alloc_fragment((block_M, block_N), accum_dtype)
            T.clear(C_local)
            for k in T.Pipelined(T.ceildiv(K, block_K), num_stages=num_stages):
                T.copy(A[by * block_M, k * block_K], A_shared)
                T.copy(B[k * block_K, bx * block_N], B_shared)
                T.gemm(A_shared, B_shared, C_local)
            T.copy(C_local, C[by * block_M, bx * block_N])
    return main


program = matmul()
A = torch.randn(1024, 1024, dtype=torch.float16).cuda()
B = torch.randn(1024, 1024, dtype=torch.float16).cuda()

for backend in ["tvm_ffi", "nvrtc"]:
    kernel = tilelang.compile(program, out_idx=-1, execution_backend=backend, target="cuda")
    print(f"[{backend}] adapter type = {type(kernel.adapter).__name__}")
    print(f"[{backend}] func type    = {type(kernel.adapter.func).__name__}")
    # 正确性
    C = kernel(A, B)
    ref = (A.float() @ B.float()).half()
    print(f"[{backend}] max abs err = {(C.float() - ref.float()).abs().max().item():.4e}")
    # 粗略单次调用开销（含 launch，未预热到稳态；仅作相对比较）
    torch.cuda.synchronize()
    t0 = time.perf_counter()
    for _ in range(20):
        _ = kernel(A, B)
    torch.cuda.synchronize()
    t1 = time.perf_counter()
    print(f"[{backend}] ~per-call latency = {(t1 - t0) / 20 * 1e3:.4f} ms\n")
```

运行：`python compare_backends.py`。

**需要观察的现象**：

1. 两者的 `adapter type` 分别是 `TVMFFIKernelAdapter` 与 `NVRTCKernelAdapter`，`func type` 都是 `function`（闭包）。
2. 两者的 `max abs err` 应在同一量级（都正确）。
3. `per-call latency` 数值接近——说明两种后端**启动的是同一份设备 kernel**，差异主要在「怎么 launch」这一薄层。

**预期结果**：

- `tvm_ffi` 走 `runtime.Executable` + DLPack；`nvrtc` 走 cuda-python 的 `cuLaunchKernel`。
- 单次调用开销两者量级相当（精确比较应改用 `kernel.get_profiler().do_bench()`，见 u8-l3）。

**待本地验证**：

- 本实践**需要 CUDA GPU**。
- `nvrtc` 后端还需要 `pip install cuda-python`；若未装，`tilelang.compile(..., execution_backend="nvrtc")` 会在 [resolve_execution_backend_spec](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/execution_backend.py#L111-L115) 处报「requires extra dependencies」。
- 若想试 `torch`（Metal）后端，把 target 改成 `metal`、数据搬到 `torch.device("mps")`，并参考 [tilelang/jit/adapter/torch/metal.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/torch/metal.py)。**当前无法在 CUDA 机器上验证 Metal，标注待本地验证。**

#### 4.3.5 小练习与答案

**练习 1**：为什么 `tvm_ffi` 后端对应的 spec 带 `enable_host_codegen=True, enable_device_compile=True`，而 `nvrtc`/`cython` 不带？

> **答案**：`tvm_ffi` 需要一个真实可执行的 `rt_mod`（含 cubin），所以必须让 `lower()` 真去编译；它构造时还断言 `artifact.rt_mod is not None`（[kernel.py:L295](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L295)）。`nvrtc`/`cython` 自己在 adapter 构造时编译源码（用 NVRTC / subprocess），因此只需 `lower()` 产出源码字符串即可，`rt_mod` 可以为 `None`。

**练习 2**：`nvrtc` 与 `cython` 同属「源码自编译」策略，二者在「如何编译」与「如何调用」上有何区别？

> **答案**：编译上，`nvrtc` 用 cuda-python 的 NVRTC API **进程内编译**成 cubin（[nvrtc/adapter.py:L84-L92](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/nvrtc/adapter.py#L84-L92)）；`cython` 用 `subprocess` 调 nvcc/hipcc/c++ 生成 `.so`（[libgen.py:L59-L164](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/libgen.py#L59-L164)）。调用上，`nvrtc` 用 cuda-python `cuLaunchKernel`（`pymodule.call`），`cython` 用 `ctypes` 传 `data_ptr()` 指针调 `lib.call`（[cython/adapter.py:L338-L345](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/cython/adapter.py#L338-L345)）。

**练习 3**：`result_idx=[-1]`（如 `out_idx=-1`）在 adapter 内部被如何处理？

> **答案**：被 [base.py:L33-L53](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/base.py#L33-L53) 的 `_legalize_result_idx` 规范成非负索引列表（`len(params) - 1`）。随后 `_convert_torch_func` 据此知道「最后一个参数是输出、需自动分配」，并只向调用方索取前 `len(params)-1` 个输入张量（[tvm_ffi.py:L226-L228](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/tvm_ffi.py#L226-L228)）。

---

## 5. 综合实践

把本讲的三块知识串成一个任务：**绘制「用户函数 → 可调用 kernel」全链路的执行后端决策图，并用代码验证每个决策点**。

任务步骤：

1. **画出决策链**（纸笔即可）。从 `tilelang.compile(func, execution_backend="auto", target="cuda")` 出发，标出五个关键节点：
   - (a) `cache._resolve_cache_dispatch` 把 `None` 补成环境变量默认 `"auto"`（[cache/\_\_init\_\_.py:L37-L49](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cache/__init__.py#L37-L49)）；
   - (b) `resolve_execution_backend_spec` 惰性加载 cuda 后端表，`auto` 选第一个可用的 `tvm_ffi`；
   - (c) `JITKernel` 读 spec 的 `enable_host_codegen/device_compile` 传给 `lower()`；
   - (d) `lower()` 产出带非空 `rt_mod` 的 `CompiledArtifact`；
   - (e) 工厂 `if/elif` 选中 `TVMFFIKernelAdapter`，其 `_convert_torch_func` 产出闭包 `func`。

2. **用代码逐点验证**：
   - 打印 `resolve_execution_backend_spec("auto", Target("cuda")).name`，确认是 `"tvm_ffi"`。
   - 编译后打印 `type(kernel.adapter).__name__`，确认是 `"TVMFFIKernelAdapter"`。
   - 打印 `kernel.execution_backend_spec.enable_host_codegen`，确认是 `True`。
   - 打印 `kernel.artifact.rt_mod is not None`，确认 `True`。

3. **切换后端重做**：把 `execution_backend` 改成 `"nvrtc"`，重画 (c)→(e)：此时 spec 的 `enable_*` 应为 `False`，`artifact.rt_mod` 应为 `None`，adapter 变成 `NVRTCKernelAdapter`，并在构造时用 NVRTC 自编译。

4. **结论**：用一句话写出「execution_backend 同时决定了 lower 的产物形态与 adapter 的种类」。

> 完成此实践后，你应该能解释：为什么换一个 `execution_backend` 字符串，下游的编译与运行路径会完全不同——因为这一个字符串同时驱动了「是否真编译」和「用哪套胶水」两个开关。

## 6. 本讲小结

- `execution_backend` 是 tilelang 把「编译产物」变成「可调用对象」时的**策略选择**：`tvm_ffi` / `nvrtc` / `cython` / `torch`(Metal) / `cutedsl`。
- 注册表 [execution_backend.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/execution_backend.py) 采用「按 target kind 注册 + 惰性 import + `supports_target`/`is_available` 双重过滤」；`auto` 取**可用列表的第一个**，因此各后端注册文件的**书写顺序**决定默认偏好（CUDA 默认 `tvm_ffi`）。
- `ExecutionBackendSpec` 上的 `enable_host_codegen` / `enable_device_compile` 是给 `lower()` 的指令——`tvm_ffi` 要真编译（`rt_mod` 非空），`nvrtc`/`cython` 只要源码（`rt_mod` 可空）。
- `BaseKernelAdapter` 用模板方法模式定下契约：子类实现 `_convert_torch_func()` 返回真正的执行闭包，`__call__` 委托给它；`result_idx` 决定哪些参数是自动分配的输出，stream/device 用 thunk 在调用时即时读取。
- `JITKernel._compile_and_create_adapter` 是工厂，**两处**消费 execution_backend：先读 spec 标志传给 `lower()`，再用 `if/elif` 选 adapter 类。
- 五种 adapter 归结为**两种产物策略**：策略 A（tvm_ffi）薄包装 TVM `rt_mod`；策略 B（nvrtc/cython）在构造时自编译源码、用 cuda-python 或 ctypes 调；Metal 特例直接交给 `torch.mps`。

## 7. 下一步学习建议

- **host/device 拆分与库生成（u7-l2）**：本讲反复出现的 `rt_mod`、`host_func`、`.so` 是怎么从 `host_mod`/`device_mod` 真正生成的？下一讲深入 `split_host_device`、`host_codegen`、`libgen`/`wrapper` 与 `tilelang_callback_cuda_compile` 编译回调。
- **编译缓存（u4-l3）**：每个 adapter 的 `from_database` 类方法对应缓存命中路径，建议结合 [tilelang/cache/](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cache/) 复盘「磁盘 `.so` 如何秒级复原 adapter」。
- **Profiler 与基准（u8-l3）**：本讲的「粗略单次调用开销」只是示意，严谨测延迟请用 `kernel.get_profiler().do_bench()`，并理解它与 adapter 路径的关系。
- **CuTeDSL 后端**：`cutedsl` 是 cuda kind 下带 `cutedsl` 标签的变体，若对 H100 CuTeDSL 路线感兴趣，可阅读 [tilelang/jit/adapter/cutedsl/](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/cutedsl/) 与 [tilelang/cuda/execution_backend.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/execution_backend.py) 中 `_is_cutedsl_target` 的用法。
